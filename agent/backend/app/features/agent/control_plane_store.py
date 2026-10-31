"""画面・API で変えた Control Plane の定義の保存と復元（#764）。

対象は、画面・API で追加・変更した Skill（runtime）・MCP 接続・ツール権限など。
`.env` の宣言（`AGENT_SKILLS_DIR` / `AGENT_EXTERNAL_MCP_SERVERS_JSON` など）と組み込みの定義は
起動のたびに読み込むので保存しない。

- 保存先は Run の保存先（`AGENT_RUNTIME_REPOSITORY_BACKEND`。`auto` は `storage_backend` が
  決める）に合わせる。`oracle_*` は
  `AGENT_CONTROL_PLANE_ITEMS`（共通の `PLATFORM_ORACLE_*`。テーブルはシステムテーブルが作る）、
  `file` は snapshot の隣の JSON ファイル、`memory` は保存しない（従来どおりプロセス内だけ）。
- MCP 接続の API キー・OAuth の client secret・セッション ID は `app.secret_box` で暗号化して
  保存する。読み込みでは暗号文と平文（#1101 より前の行）の両方を受け付け、起動時の復元で
  平文の秘密を見つけたら暗号化して保存し直す。
- 起動時（`app.main` の startup）に `restore_control_plane()` で読み込み、宣言の後に重ねる。
- データを書き換える migration の後は `reload_control_plane()` で保存先の内容に置き換える。
  書き換えの間の保存は `writes_paused()` で止める（古いメモリの値で上書きしない。#1844）。
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from importlib import import_module
from pathlib import Path
from threading import Condition, Lock
from typing import Any, Literal, Protocol, cast

from pr_backend_core.logging import safe_exception_fields

from app.features.agent import storage_backend
from app.oracle_connection import connect_platform_oracle
from app.secret_box import SEALED_PREFIX, SecretBoxError, open_secret, seal_secret
from app.settings import get_settings

logger = logging.getLogger(__name__)

JsonObject = dict[str, Any]
ItemKind = Literal[
    "skill",
    "mcp_connection",
    "tool_policy",
    "evaluation_set",
    "evaluation_job",
    "automation",
]
ITEM_KINDS: tuple[ItemKind, ...] = (
    "mcp_connection",
    "tool_policy",
    "skill",
    # 業務 Agent の自動実行（#784。スケジュールで実行する）。
    "automation",
    # 品質評価の評価セットと評価の job（#776）。
    "evaluation_set",
    "evaluation_job",
)
_EVALUATION_KINDS = {"evaluation_set", "evaluation_job"}
ITEMS_TABLE = "AGENT_CONTROL_PLANE_ITEMS"
# MCP 接続の構成（`McpConnectionConfig`）のうち暗号化して保存する項目。
_SECRET_FIELDS = ("api_key", "oauth_client_secret", "session_id")
# 秘密を持ちうる（MCP 接続の構成を含む）保存の種類。
_SECRET_KINDS: frozenset[ItemKind] = frozenset({"mcp_connection"})


class ControlPlaneStoreError(RuntimeError):
    """定義を保存できない（画面に出す日本語の文言）。"""


class ControlPlaneItemStore(Protocol):
    @property
    def persistent(self) -> bool: ...
    def load(self) -> dict[str, dict[str, JsonObject]]: ...
    def put(self, kind: ItemKind, item_id: str, document: JsonObject) -> None: ...
    def delete(self, kind: ItemKind, item_id: str) -> None: ...


class MemoryItemStore:
    """保存しない（`memory` の構成。再起動で `.env` の宣言だけに戻る）。"""

    persistent = False

    def load(self) -> dict[str, dict[str, JsonObject]]:
        return {}

    def put(self, kind: ItemKind, item_id: str, document: JsonObject) -> None:
        return None

    def delete(self, kind: ItemKind, item_id: str) -> None:
        return None


class FileItemStore:
    """JSON ファイル（`file` の構成。開発・検証用）。"""

    persistent = True

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = Lock()

    def load(self) -> dict[str, dict[str, JsonObject]]:
        with self._lock:
            return self._read_locked()

    def put(self, kind: ItemKind, item_id: str, document: JsonObject) -> None:
        with self._lock:
            items = self._read_locked()
            items.setdefault(kind, {})[item_id] = document
            self._write_locked(items)

    def delete(self, kind: ItemKind, item_id: str) -> None:
        with self._lock:
            items = self._read_locked()
            if items.get(kind, {}).pop(item_id, None) is not None:
                self._write_locked(items)

    def _read_locked(self) -> dict[str, dict[str, JsonObject]]:
        if not self._path.exists():
            return {}
        data = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        return {
            str(kind): {str(item_id): doc for item_id, doc in docs.items()}
            for kind, docs in data.items()
            if isinstance(docs, dict)
        }

    def _write_locked(self, items: dict[str, dict[str, JsonObject]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_name(f".{self._path.name}.tmp")
        temp_path.write_text(
            json.dumps(items, ensure_ascii=False, sort_keys=True, default=str), encoding="utf-8"
        )
        os.replace(temp_path, self._path)


class OracleItemStore:
    """`AGENT_CONTROL_PLANE_ITEMS`（共通の `PLATFORM_ORACLE_*`）。"""

    persistent = True

    def __init__(self, connect_factory: Callable[[], Any] | None = None) -> None:
        self._connect = connect_factory or connect_platform_oracle

    def load(self) -> dict[str, dict[str, JsonObject]]:
        items: dict[str, dict[str, JsonObject]] = {}
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute(f"SELECT ITEM_KIND, ITEM_ID, ITEM_JSON FROM {ITEMS_TABLE}")
                # CLOB は接続を閉じる前に読む（閉じた後に読むと DPY-1001）。
                rows = [
                    (kind, item_id, _lob_text(item_json))
                    for kind, item_id, item_json in cursor.fetchall()
                ]
        except Exception as exc:
            if _is_table_missing(exc):
                # システムテーブルを作る前（画面はシステムテーブルの作成へ案内する）。
                logger.warning("agent_control_plane_items_missing")
                return {}
            raise
        for kind, item_id, item_json in rows:
            try:
                document = json.loads(item_json)
            except ValueError:
                logger.warning(
                    "agent_control_plane_item_invalid", extra={"kind": kind, "item_id": item_id}
                )
                continue
            if isinstance(document, dict):
                items.setdefault(str(kind), {})[str(item_id)] = document
        return items

    def put(self, kind: ItemKind, item_id: str, document: JsonObject) -> None:
        statement = f"""
        MERGE INTO {ITEMS_TABLE} target
        USING (
            SELECT :item_kind AS item_kind, :item_id AS item_id, :item_json AS item_json
            FROM dual
        ) source
        ON (target.item_kind = source.item_kind AND target.item_id = source.item_id)
        WHEN MATCHED THEN UPDATE SET
            target.item_json = source.item_json,
            target.updated_at = SYSTIMESTAMP
        WHEN NOT MATCHED THEN INSERT (item_kind, item_id, item_json, updated_at)
        VALUES (source.item_kind, source.item_id, source.item_json, SYSTIMESTAMP)
        """  # nosec B608 - テーブル名は固定
        self._execute(
            statement,
            # 定義の JSON（長い指示の Skill など）は 4,000 byte を超えうる。型を指定しないと
            # `SELECT :item_json ... FROM dual` の bind が VARCHAR2 になり ORA-01461（#841）。
            clob_params=("item_json",),
            item_kind=kind,
            item_id=item_id,
            item_json=json.dumps(document, ensure_ascii=False, sort_keys=True, default=str),
        )

    def delete(self, kind: ItemKind, item_id: str) -> None:
        self._execute(
            f"DELETE FROM {ITEMS_TABLE} WHERE item_kind = :item_kind AND item_id = :item_id",  # nosec B608
            item_kind=kind,
            item_id=item_id,
        )

    def _execute(self, statement: str, *, clob_params: tuple[str, ...] = (), **params: Any) -> None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                if clob_params:
                    clob = import_module("oracledb").DB_TYPE_CLOB
                    cursor.setinputsizes(**{name: clob for name in clob_params})
                cursor.execute(statement, **params)
                connection.commit()
        except Exception as exc:
            if _is_table_missing(exc):
                raise ControlPlaneStoreError(
                    "定義を保存するテーブルがありません。"
                    "運用設定 > システムテーブルで作成・更新してください。"
                ) from exc
            raise


def _is_table_missing(exc: BaseException) -> bool:
    text = str(exc)
    return "ORA-00942" in text or "table or view does not exist" in text


def _lob_text(value: object) -> str:
    read = getattr(value, "read", None)
    if callable(read):
        return str(read())
    return str(value)


def build_control_plane_store() -> ControlPlaneItemStore:
    settings = get_settings()
    # Run の保存先と同じ決定に従う（`auto` は起動時に 1 回だけ判定する。#839）。
    backend = storage_backend.resolved_backend()
    if backend in storage_backend.ORACLE_BACKENDS:
        return OracleItemStore()
    if backend in storage_backend.FILE_BACKENDS and settings.agent_runtime_snapshot_path:
        snapshot = Path(settings.agent_runtime_snapshot_path)
        return FileItemStore(snapshot.with_name(f"{snapshot.stem}.control-plane.json"))
    return MemoryItemStore()


_store: ControlPlaneItemStore | None = None
_restoring = False
# 保存先を外から書き換える間（データを書き換える migration。#1844）の保存の停止。保存は短い間だけ
# 「書き込み中」に数え、止める側は書き込み中の保存が終わるのを待ってから書き換える。
_writes = Condition(Lock())
_active_writes = 0
_writes_paused = False
WRITES_PAUSED_MESSAGE = (
    "システムテーブルを更新しています。定義は保存していません。"
    "しばらくしてから、もう一度実行してください。"
)


def get_control_plane_store() -> ControlPlaneItemStore:
    global _store
    if _store is None:
        _store = build_control_plane_store()
    return _store


def set_control_plane_store(store: ControlPlaneItemStore | None) -> None:
    """テスト用に保存先を差し替える（None で設定から作り直す）。"""
    global _store
    _store = store


@contextmanager
def _restoring_items() -> Iterator[None]:
    global _restoring
    _restoring = True
    try:
        yield
    finally:
        _restoring = False


@contextmanager
def _write_slot() -> Iterator[None]:
    """保存の 1 回。保存を止めている間は待たずに `ControlPlaneStoreError`（API は 503）。

    待たせると、止める前のメモリの値から作った定義を、読み直しの後に保存してしまう。
    """
    global _active_writes
    with _writes:
        if _writes_paused:
            raise ControlPlaneStoreError(WRITES_PAUSED_MESSAGE)
        _active_writes += 1
    try:
        yield
    finally:
        with _writes:
            _active_writes -= 1
            _writes.notify_all()


@contextmanager
def writes_paused() -> Iterator[None]:
    """保存先を外から書き換える間、定義の保存を止める（#1844）。

    書き込み中の保存が終わるのを待ってから入る。抜けるまでの保存は `ControlPlaneStoreError`。
    """
    global _writes_paused
    with _writes:
        while _writes_paused:
            _writes.wait()
        _writes_paused = True
        while _active_writes:
            _writes.wait()
    try:
        yield
    finally:
        with _writes:
            _writes_paused = False
            _writes.notify_all()


def _put(kind: ItemKind, item_id: str, document: JsonObject) -> None:
    if _restoring:
        return
    with _write_slot():
        get_control_plane_store().put(kind, item_id, document)


def _delete(kind: ItemKind, item_id: str) -> None:
    if _restoring:
        return
    with _write_slot():
        get_control_plane_store().delete(kind, item_id)


# ---- 保存（router の変更の後に呼ぶ） ---------------------------------------------


def save_skill(skill: Any) -> None:
    _put("skill", skill.id, skill.model_dump(mode="json"))


def delete_skill(skill_id: str) -> None:
    _delete("skill", skill_id)


def save_mcp_connection(config: Any) -> None:
    document = config.model_dump(mode="json")
    _put(
        "mcp_connection",
        config.server_id,
        _with_secrets("mcp_connection", document, _sealed_secrets),
    )


def delete_mcp_connection(server_id: str) -> None:
    _delete("mcp_connection", server_id)


def save_tool_policy(policy: Any) -> None:
    _put(
        "tool_policy",
        "default",
        {
            "default_mode": policy.default_mode,
            "allow": sorted(policy.allow),
            "ask": sorted(policy.ask),
            "deny": sorted(policy.deny),
        },
    )


def save_automation(automation_id: str, document: JsonObject) -> None:
    _put("automation", automation_id, document)


def delete_automation(automation_id: str) -> None:
    _delete("automation", automation_id)


def save_evaluation_item(kind: str, item_id: str, document: JsonObject) -> None:
    """品質評価の評価セット・job を保存する（#776）。"""
    if kind not in _EVALUATION_KINDS:
        raise ValueError(kind)
    _put(cast(ItemKind, kind), item_id, document)


def delete_evaluation_item(kind: str, item_id: str) -> None:
    if kind not in _EVALUATION_KINDS:
        raise ValueError(kind)
    _delete(cast(ItemKind, kind), item_id)


# ---- 復元（起動時） ---------------------------------------------------------------


def restore_control_plane() -> dict[str, int]:
    """保存した定義を読み込み、宣言の後に重ねる（読めない項目は飛ばして記録する）。"""
    items = get_control_plane_store().load()
    restored: dict[str, int] = dict.fromkeys(ITEM_KINDS, 0)
    with _restoring_items():
        for kind in ITEM_KINDS:
            for item_id, document in sorted(items.get(kind, {}).items()):
                try:
                    _restore_item(kind, document)
                except Exception as exc:  # noqa: BLE001 - 1 件の不正で起動を止めない
                    logger.warning(
                        "agent_control_plane_restore_failed",
                        extra={"kind": kind, "item_id": item_id, **safe_exception_fields(exc)},
                    )
                    continue
                restored[kind] += 1
                _reseal_plaintext_secrets(kind, item_id, document)
    return restored


def _reseal_plaintext_secrets(kind: ItemKind, item_id: str, document: JsonObject) -> None:
    """平文で保存した秘密（#1101 より前の行）を暗号化して保存し直す（1 回だけの移行）。

    暗号化済みの値は変えない（暗号文を作り直さない）ので、移行の済んだ行は書き換えない。
    署名鍵が無い・保存できないときは平文のまま読み続け、起動は止めない（次の保存で暗号化する）。
    """
    if kind not in _SECRET_KINDS:
        return
    store = get_control_plane_store()
    if not store.persistent:
        return
    try:
        resealed = _with_secrets(kind, document, _sealed_plaintext_secrets)
        if resealed != document:
            store.put(kind, item_id, resealed)
    except Exception as exc:  # noqa: BLE001 - 移行の失敗で起動を止めない
        logger.warning(
            "agent_control_plane_secret_reseal_failed",
            extra={"kind": kind, "item_id": item_id, **safe_exception_fields(exc)},
        )


def _restore_item(kind: ItemKind, document: JsonObject) -> None:
    # 登録先は遅延 import（このモジュールは config / skills から import されない）。
    from app.features.agent.automations import automation_store
    from app.features.agent.config import runtime_config_store
    from app.features.agent.evaluation import evaluation_set_store, evaluation_store
    from app.features.agent.skills import skill_registry

    item = _parse_item(kind, document)
    if kind == "mcp_connection":
        runtime_config_store.restore_mcp_server(item)
    elif kind == "tool_policy":
        runtime_config_store.patch_tool_policy(
            default_mode=item.default_mode,
            allow=sorted(item.allow),
            ask=sorted(item.ask),
            deny=sorted(item.deny),
        )
    elif kind == "skill":
        skill_registry.upsert_custom(item)
    elif kind == "automation":
        automation_store.restore(item)
    elif kind == "evaluation_set":
        evaluation_set_store.restore(item)
    elif kind == "evaluation_job":
        evaluation_store.restore(item)


def _parse_item(kind: ItemKind, document: JsonObject) -> Any:
    """保存した 1 件を、登録先の型にする（秘密は復号する。不正なら例外）。"""
    from app.features.agent.config import McpConnectionConfig, ToolPolicyRuntimeConfig
    from app.features.agent.skills import AgentSkillDefinition

    document = _with_secrets(kind, document, _opened_secrets)
    if kind == "mcp_connection":
        return McpConnectionConfig.model_validate(document)
    if kind == "tool_policy":
        from app.features.agent.profile_name_migration import migrate_tool_name

        names = {
            key: [
                migrate_tool_name(item) if isinstance(item, str) else item
                for item in (document.get(key) or [])
            ]
            for key in ("allow", "ask", "deny")
        }
        return ToolPolicyRuntimeConfig(
            default_mode=str(document.get("default_mode") or "approval"),
            allow=set(names["allow"]),
            ask=set(names["ask"]),
            deny=set(names["deny"]),
        )
    if kind == "skill":
        return AgentSkillDefinition.model_validate(document)
    if kind == "automation":
        from app.features.agent.automations import Automation

        return Automation.model_validate(document)
    if kind == "evaluation_set":
        from app.features.agent.evaluation import EvaluationSet

        return EvaluationSet.model_validate(document)
    from app.features.agent.evaluation import EvaluationJob

    return EvaluationJob.model_validate(document)


# ---- 読み直し（データを書き換える migration の後。#1844） ----------------------------


def reload_control_plane() -> dict[str, int] | None:
    """保存先から読み直し、画面・API で変えた定義を保存先の内容で置き換える。

    起動時の復元（宣言の後に重ねる）と違い、保存先から消えた定義はメモリからも消え、
    宣言の接続・ツール権限は宣言の値に戻る。保存しない構成（`memory`）は何もしない（None）。
    読めない項目は飛ばして記録する（起動時の復元と同じ）。
    """
    from app.features.agent.automations import automation_store
    from app.features.agent.config import runtime_config_store
    from app.features.agent.evaluation import evaluation_set_store, evaluation_store
    from app.features.agent.skills import skill_registry

    store = get_control_plane_store()
    if not store.persistent:
        return None
    items = store.load()
    parsed: dict[ItemKind, list[Any]] = {kind: [] for kind in ITEM_KINDS}
    for kind in ITEM_KINDS:
        for item_id, document in sorted(items.get(kind, {}).items()):
            try:
                parsed[kind].append(_parse_item(kind, document))
            except Exception as exc:  # noqa: BLE001 - 1 件の不正で読み直しを止めない
                logger.warning(
                    "agent_control_plane_reload_failed",
                    extra={"kind": kind, "item_id": item_id, **safe_exception_fields(exc)},
                )
    policies = parsed["tool_policy"]
    runtime_config_store.replace_restored(
        parsed["mcp_connection"], policies[-1] if policies else None
    )
    for skill_id in skill_registry.replace_runtime(parsed["skill"]):
        logger.warning(
            "agent_control_plane_reload_failed",
            extra={"kind": "skill", "item_id": skill_id, "reason": "builtin_id"},
        )
    automation_store.replace_all(parsed["automation"])
    evaluation_set_store.replace_all(parsed["evaluation_set"])
    evaluation_store.replace_all(parsed["evaluation_job"])
    return {kind: len(values) for kind, values in parsed.items()}


# ---- 秘密の暗号化・復号 ------------------------------------------------------------

SecretTransform = Callable[[JsonObject], JsonObject]


def _with_secrets(kind: ItemKind, document: JsonObject, transform: SecretTransform) -> JsonObject:
    """保存する文書の中の MCP 接続の構成に `transform`（暗号化・復号）を当てた写し。"""
    if kind == "mcp_connection":
        return transform(document)
    return document


def _sealed_secrets(document: JsonObject, *, only_plaintext: bool = False) -> JsonObject:
    """秘密を暗号化した写し。保存しない構成（`memory`）では平文のまま（どこにも書かない）。

    暗号化の鍵（`PLATFORM_SERVICE_TOKEN_SECRET`）が無ければ保存しない（平文を残さない）。
    """
    if not get_control_plane_store().persistent:
        return document
    sealed = dict(document)
    for field in _SECRET_FIELDS:
        value = sealed.get(field)
        if not value:
            continue
        if only_plaintext and isinstance(value, str) and value.startswith(SEALED_PREFIX):
            continue
        try:
            sealed[field] = seal_secret(str(value))
        except SecretBoxError as exc:
            raise ControlPlaneStoreError(str(exc)) from exc
    return sealed


def _sealed_plaintext_secrets(document: JsonObject) -> JsonObject:
    return _sealed_secrets(document, only_plaintext=True)


def _opened_secrets(document: JsonObject) -> JsonObject:
    """保存した秘密を平文に戻した写し（平文の値はそのまま。復号できない値は None）。"""
    opened = dict(document)
    for field in _SECRET_FIELDS:
        value = opened.get(field)
        if isinstance(value, str) and value:
            opened[field] = open_secret(value)
    return opened
