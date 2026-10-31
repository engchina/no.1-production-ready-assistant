"""システムテーブル（製品の versioned Oracle system schema）の管理の骨格（3製品共通。#325 段階 4）。

RAG と NL2SQL が別々に持っていた schema manager の共通部分を置く。

- 公開状態の分類（`missing` / `partial` / `outdated` / `ready`）
- 操作の lease（`<製品>_SCHEMA_OPERATIONS` の 1 行を、複数 replica の間で排他する）と
  `schema_epoch`（成功した操作ごとに増える。別 replica の cache の破棄に使う）
- 適用済み migration の台帳（`<製品>_SCHEMA_MIGRATIONS`。checksum が一致したものだけを適用済みに
  する）
- 全再作成の確認語の検証（DB に触る前に、完全一致だけを通す）
- 操作の失敗の安全化（公開するのは ORA コードだけ。SQL・資格情報は返さない）と、ORA-00054 の 409。
  ORA でない失敗（設定の読み込みなど）は、CLI に secret を含まない手がかり（例外の message の
  1 行目。値は出さない）を出す（#1701）
- API の型（操作の状態・初期化の request）と、状態を取得できないときの 503
- 外部キーの差分（正本の `CREATE TABLE` にあるが既存の表に無い FK の検出・追加と、参照先の無い
  既存の行の件数。#505。削除規則の違い・無効化（DISABLED）の検出と修正、利用者の明示操作による
  参照先の無い行の削除と VALIDATE。#511）
- 列の欠落（正本の DDL から求めた列と USER_TAB_COLUMNS の比較。台帳が適用済みでも欠けた列を状態に
  出し、作成・更新の後も欠けていれば止める。#2450・#2458）
- データを消す（破壊的な）未適用の migration の確認。状態に出し、作成・更新は明示の承認
  （`allow_destructive`）が無ければ DB に触る前に止める（#619）

製品に残すもの（ここには置かない）:

- 必要なテーブル・索引などの一覧（manifest）と DDL の正本、migration の適用方法
- 状態の組み立て（`_status_on`）と初期化の手順（`_initialize_on`）
- 実行中の job の確認、接続 pool、製品名が入る文言と確認語

アプリの起動時には DDL を実行しない。管理 API / CLI から明示されたときだけ操作する。
共有パッケージは oracledb に依存しない（接続は製品が `connection_factory` で渡す）。
"""

from __future__ import annotations

import logging
import re
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal

from fastapi import HTTPException
from pr_backend_core.oracle_connect_diagnosis import (
    ConnectKwargsFactory,
    OracleConnectDiagnosis,
    diagnose_oracle_connect_failure,
)
from pydantic import BaseModel, Field, ValidationError

from .database_status import clear_database_status_cache

SystemSchemaStatus = Literal["missing", "partial", "outdated", "ready"]
SystemSchemaOperation = Literal["no_op", "initialized", "migrated", "recreated"]
SystemSchemaOperationStatus = Literal["idle", "running", "failed"]
SystemSchemaOperationKind = Literal["initialize", "recreate"]
# 参照先の無い行の削除（#511）の結果（削除も検査も要らなかったときは `no_op`）。
SystemSchemaOrphanOperation = Literal["no_op", "orphans_deleted"]

# 制御テーブルの行のキー（製品ごとに 1 行）。
CONTROL_KEY = "system_schema"
DEFAULT_LEASE_SECONDS = 900
MIN_LEASE_SECONDS = 30
MAX_DDL_LOCK_TIMEOUT_SECONDS = 120
# ORA-00054（DDL のロック待ちの時間切れ）を返すときの Retry-After（秒）。
LOCK_RETRY_AFTER_SECONDS = 5

RECREATE_CONFIRMATION_REQUIRED = "SCHEMA_RECREATE_CONFIRMATION_REQUIRED"
# データを消す未適用の migration があるのに、作成・更新の承認（`allow_destructive`）が無い（#619）。
DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED = "SCHEMA_DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED"
SCHEMA_OPERATION_FAILED = "SCHEMA_OPERATION_FAILED"
# 作成・更新の後の状態が ready でない（必須の object・列が揃わない）。
SCHEMA_POSTCONDITION_FAILED = "SCHEMA_POSTCONDITION_FAILED"
# 状態を読めない（DB に接続できない・ORA でない失敗）。配備の確認の要約
# （`deployment_check_error_summary`）の先頭の機械が読める印でもある（#1768。配備の通しの
# .github/scripts/deploy-run-through.sh が、この印と接続の失敗のコードで「DB に繋がらない
# だけ」を判定する）。
SCHEMA_STATUS_UNAVAILABLE = "SCHEMA_STATUS_UNAVAILABLE"
ORA_RESOURCE_BUSY = "ORA-00054"

# 冪等な適用で読み飛ばす Oracle のエラー。
IGNORED_APPLY_CODES = frozenset(
    {
        "ORA-00001",  # migration の台帳・seed の重複
        "ORA-00955",  # object already exists
        "ORA-01430",  # column already exists
        "ORA-01442",  # column is already NOT NULL
        "ORA-01451",  # column is already NULL
    }
)
# DROP で読み飛ばす Oracle のエラー（既に無い）。
IGNORED_DROP_CODES = frozenset({"ORA-00942", "ORA-01418"})
# 外部キーの追加で読み飛ばすエラー（同じ列・参照先の FK が既にある。別の操作が先に追加した）。
ORA_FOREIGN_KEY_EXISTS = "ORA-02275"
# 検査付きの外部キーの追加が、参照先の無い既存の行で失敗した（parent keys not found）。
ORA_ORPHAN_ROWS = "ORA-02298"
# 削除しようとした行を、別の表の NO ACTION の外部キーが参照している（child record found）。
ORA_CHILD_RECORD_FOUND = "ORA-02292"
# 外部キーの削除で読み飛ばすエラー（既に無い。別の操作が先に削除した）。
ORA_CONSTRAINT_NOT_FOUND = "ORA-02443"

FOREIGN_KEY_NOT_FOUND = "SCHEMA_FOREIGN_KEY_NOT_FOUND"
FOREIGN_KEY_OUTDATED = "SCHEMA_FOREIGN_KEY_OUTDATED"
ORPHAN_ROWS_CHANGED = "SCHEMA_ORPHAN_ROWS_CHANGED"
# 失敗のログの `operation`（参照先の無い行の削除。#511）。
ORPHAN_DELETION_OPERATION = "delete_orphaned_rows"

_ORA_CODE_PATTERN = re.compile(r"ORA-\d{5}", flags=re.IGNORECASE)
# 失敗の手がかり（#1701）で伏せる値: `password=…` のような代入と、URL の中の資格情報。
_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b([\w.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|credential|private[_-]?key)"
    r"[\w.-]*)(\s*[=:]\s*)\S+"
)
_URL_CREDENTIAL_PATTERN = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@")
FAILURE_HINT_MAX_CHARS = 300
FAILURE_HINT_MAX_DEPTH = 4

ConnectionFactory = Callable[[], AbstractContextManager[Any]]


class SystemSchemaError(RuntimeError):
    """secret や SQL を含めない schema operation error。"""

    def __init__(self, code: str, public_message: str, *, status_code: int = 500) -> None:
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        """ロック待ちの時間切れ（ORA-00054）だけは、待てば再試行できる。"""

        return self.code == ORA_RESOURCE_BUSY

    @property
    def retry_headers(self) -> dict[str, str] | None:
        return {"Retry-After": str(LOCK_RETRY_AFTER_SECONDS)} if self.retryable else None


class SystemSchemaBusyError(SystemSchemaError):
    def __init__(self) -> None:
        super().__init__(
            "SCHEMA_OPERATION_IN_PROGRESS",
            "別のシステムテーブル操作が実行中です。完了後に状態を再取得してください。",
            status_code=409,
        )


class SystemSchemaActiveJobsError(SystemSchemaError):
    """全再作成の前に、実行中の job がある（文言は製品が渡す）。"""

    def __init__(self, public_message: str) -> None:
        super().__init__("SCHEMA_JOBS_RUNNING", public_message, status_code=409)


class SystemSchemaPreconditionError(SystemSchemaError):
    """操作の前提の確認で止めた（DB を変えていない）。lease は返し、失敗として記録しない。"""


class SystemTableOperationState(BaseModel):
    """複数 replica が共有する schema operation lease の状態と直近の操作。"""

    status: SystemSchemaOperationStatus
    operation_kind: SystemSchemaOperationKind | None = None
    lease_expires_at: str | None = None
    last_error_code: str | None = None
    schema_epoch: int = 0
    updated_at: str | None = None


class SystemTableColumnData(BaseModel):
    """既存の表に無い正本の列の 1 件（#2450）。"""

    table_name: str
    column_name: str


class SystemTableForeignKeyData(BaseModel):
    """外部キーの差分の 1 件（不足している FK、または参照先の無い既存の行が残る FK）。"""

    name: str
    table_name: str
    columns: list[str]
    referenced_table_name: str
    referenced_columns: list[str]
    delete_rule: str
    # 参照先の無い既存の行の件数（数えられなかったときは null）。
    orphan_rows: int | None = None
    # 削除規則が正本と違う FK（#511）の、既存の FK の名前と削除規則（それ以外は null）。
    current_name: str | None = None
    current_delete_rule: str | None = None


class SystemTableDestructiveMigrationData(BaseModel):
    """未適用の、データを消す（テーブルの DROP・行の DELETE を含む）migration の 1 件（#619）。

    `description` は消えるデータと、適用の前にすること（書き出しなど）の説明。
    """

    name: str
    description: str


class SystemTablesInitializeRequest(BaseModel):
    """作成・更新、または全再作成（`recreate` と確認語）の request。

    `allow_destructive` は、データを消す未適用の migration（状態の
    `pending_destructive_migrations`）を当てることの承認（#619）。無ければ作成・更新を止める。
    """

    recreate: bool = False
    confirmation: str | None = Field(default=None, max_length=128)
    allow_destructive: bool = False


class SystemTablesDeleteOrphansRequest(BaseModel):
    """参照先の無い行の削除（#511）の request。

    `expected_orphan_rows` は利用者が確認した件数。削除の直前に数え直し、これより増えていたら
    削除せずに 409 を返す（確認していない行を消さない）。
    """

    constraint_name: str = Field(min_length=1, max_length=128)
    expected_orphan_rows: int = Field(ge=0)


def oracle_error_code(exc: BaseException) -> str:
    """例外から ORA コードだけを取り出す（無ければ `SCHEMA_OPERATION_FAILED`）。"""

    match = _ORA_CODE_PATTERN.search(str(exc))
    return match.group(0).upper() if match else SCHEMA_OPERATION_FAILED


def operation_failure_hint(exc: BaseException) -> str | None:
    """ORA でない失敗（設定の読み込みなど）の原因の手がかり（#1701）。ORA の失敗は None。

    配備の log から原因を辿れるよう、例外の型と message の 1 行目を、原因（`__cause__` /
    `__context__`）の順に `←` でつなぐ。SQL・資格情報を出さないよう、2 行目以降は捨て、
    `password=…` のような代入の値と URL の資格情報は伏せる。pydantic の `ValidationError` は
    値（`input_value`）を出さず、件数と項目の位置だけを出す。
    """

    if oracle_error_code(exc) != SCHEMA_OPERATION_FAILED:
        return None
    lines: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(lines) < FAILURE_HINT_MAX_DEPTH:
        seen.add(id(current))
        lines.append(_exception_line(current))
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return " ← ".join(lines)


def operation_failure_fields(
    exc: BaseException,
    *,
    connect_kwargs: ConnectKwargsFactory | None = None,
) -> dict[str, str]:
    """CLI の失敗の項目（`code` と、あれば `hint` / `detail` / `cause`）。secret は含めない。

    DB に接続できない失敗は、理由と対処を日本語で返す（`diagnose_oracle_connect_failure`。#1744）。
    pool の時間切れ（`DPY-4005`）なら、`connect_kwargs` で pool を使わずに 1 回だけ直接接続して、
    本当の原因（ADB の停止・password の誤り・account lock・Wallet など）を確かめる。それ以外は
    ORA コードと、ORA でない失敗の手がかり（`operation_failure_hint`）を返す。
    """

    diagnosis = connect_failure_diagnosis(exc, connect_kwargs=connect_kwargs)
    if diagnosis is not None:
        return {"code": diagnosis.code or oracle_error_code(exc), **diagnosis.as_fields()}
    fields = {"code": oracle_error_code(exc)}
    hint = operation_failure_hint(exc)
    if hint:
        fields["hint"] = hint
    return fields


def connect_failure_diagnosis(
    exc: BaseException,
    *,
    connect_kwargs: ConnectKwargsFactory | None = None,
) -> OracleConnectDiagnosis | None:
    """DB に接続できない失敗の診断（それ以外・`SystemSchemaError` は None）。"""

    if isinstance(exc, SystemSchemaError):
        return None
    try:
        return diagnose_oracle_connect_failure(exc, connect_kwargs=connect_kwargs)
    except Exception:  # noqa: BLE001 - 診断の失敗で元の失敗の報告を止めない
        logging.getLogger(__name__).warning("oracle_connect_diagnosis_failed")
        return None


def _exception_line(exc: BaseException) -> str:
    name = type(exc).__name__
    if isinstance(exc, ValidationError):
        locations = [
            ".".join(str(part) for part in error["loc"]) or "(root)"
            for error in exc.errors(include_url=False, include_input=False, include_context=False)
        ]
        shown = ", ".join(locations[:5]) + (", …" if len(locations) > 5 else "")
        return f"{name}: {exc.title} の検証エラー {exc.error_count()} 件（{shown}）"
    text = next((line.strip() for line in str(exc).splitlines() if line.strip()), "")
    if not text:
        return name
    text = _SECRET_ASSIGNMENT_PATTERN.sub(r"\1\2***", text)
    text = _URL_CREDENTIAL_PATTERN.sub(r"\1***@", text)
    if len(text) > FAILURE_HINT_MAX_CHARS:
        text = f"{text[:FAILURE_HINT_MAX_CHARS]}…"
    return f"{name}: {text}"


def iso_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        current = value if value.tzinfo else value.replace(tzinfo=UTC)
        return current.isoformat()
    return str(value)


def bind_list(prefix: str, values: Sequence[str]) -> tuple[str, dict[str, str]]:
    """`IN (...)` の bind 変数の並びと値（manifest の固定値だけに使う）。"""

    binds = {f"{prefix}{index}": value for index, value in enumerate(values)}
    return ", ".join(f":{name}" for name in binds), binds


def clamp_ddl_lock_timeout(value: int) -> int:
    return max(0, min(MAX_DDL_LOCK_TIMEOUT_SECONDS, int(value)))


def require_recreate_confirmation(
    *,
    recreate: bool,
    confirmation: str | None,
    expected: str,
) -> None:
    """全再作成は確認語の完全一致だけを通す（DB に触る前に確かめる）。"""

    if recreate and confirmation != expected:
        raise SystemSchemaError(
            RECREATE_CONFIRMATION_REQUIRED,
            "すべて再作成するには確認値を正確に入力してください。",
            status_code=422,
        )


def require_destructive_migration_confirmation(
    pending: Sequence[Mapping[str, Any]],
    *,
    allow_destructive: bool,
) -> None:
    """データを消す未適用の migration は、明示の承認があるときだけ通す（DB に触る前。#619）。"""

    if not pending or allow_destructive:
        return
    names = ", ".join(str(item.get("name", "")) for item in pending)
    raise SystemSchemaError(
        DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED,
        f"データを削除する migration が未適用です（{names}）。削除されるデータを確認し、"
        "必要なデータを書き出してから、削除を承認して再実行してください。",
        status_code=409,
    )


def classify_system_schema_status(
    objects: Collection[tuple[str, str]],
    *,
    domain_tables: Iterable[str],
    managed_objects: Iterable[tuple[str, str]],
    migrations_current: bool,
    retired_objects: Iterable[tuple[str, str]] = (),
    foreign_keys_current: bool = True,
    columns_current: bool = True,
) -> SystemSchemaStatus:
    """Dictionary / 台帳の snapshot を四つの公開状態へ決定論的に分類する。

    - `missing`: 業務テーブル（制御テーブル以外）が 1 つも無い
    - `partial`: 必須の object のどれかが無い
    - `outdated`: 廃止した object が残っている、未適用 / checksum 不一致の migration がある、
      既存の表に正本の外部キーが無い（`foreign_keys_current=False`）、
      または既存の表に正本の列が無い（`columns_current=False`。台帳が適用済みでも。#2450）
    - `ready`: それ以外
    """

    present = set(objects)
    if not any((name, "TABLE") in present for name in domain_tables):
        return "missing"
    if set(managed_objects) - present:
        return "partial"
    if set(retired_objects) & present:
        return "outdated"
    if not migrations_current or not foreign_keys_current or not columns_current:
        return "outdated"
    return "ready"


def idle_operation_state() -> dict[str, Any]:
    """制御テーブルが無い・行が無いときの操作の状態。"""

    return {
        "status": "idle",
        "operation_kind": None,
        "lease_expires_at": None,
        "last_error_code": None,
        "schema_epoch": 0,
        "updated_at": None,
    }


# ---- 配備の確認（#1648） --------------------------------------------------------
#
# 配備の最後の確認（platform/deploy/suite-health.sh の suite_check_deployment）が、各製品の
# schema の CLI の `check` を実行し、終了コード（0 = ready）と 1 行の要約を log に出す。
# init_script.sh は初期化に失敗しても backend を起動する（画面から直せる）ため、ここで NG にして
# 配備の失敗として見せる。

# 要約に出す不足の object・承認が要る削除の名前の上限（残りは件数だけ）。
DEPLOYMENT_CHECK_NAME_LIMIT = 5


def _limited_names(names: Sequence[str]) -> str:
    shown = ", ".join(names[:DEPLOYMENT_CHECK_NAME_LIMIT])
    return f"{shown}, …" if len(names) > DEPLOYMENT_CHECK_NAME_LIMIT else shown


def deployment_check_summary(status: Mapping[str, Any]) -> tuple[bool, str]:
    """状態から、配備の確認の結果（ready か）と 1 行の要約を返す。

    ready で、操作が実行中でないときだけ OK。NG の要約には、状態・不足の object・未適用の
    migration・承認が要る削除・前回の操作の失敗（ORA コードだけ）を入れる。SQL・資格情報は
    入れない。
    """

    state = str(status.get("status") or "unknown")
    operation = status.get("operation_state") or {}
    operation_status = str(operation.get("status") or "idle")
    ok = state == "ready" and operation_status != "running"
    parts = [f"status={state}"]
    if ok:
        return True, parts[0]
    missing = [str(item.get("name", "")) for item in status.get("missing_objects") or []]
    if missing and state != "missing":
        parts.append(f"不足 {len(missing)} 件（{_limited_names(missing)}）")
    missing_columns = [
        f"{item.get('table_name', '')}.{item.get('column_name', '')}"
        for item in status.get("missing_columns") or []
    ]
    if missing_columns and state != "missing":
        parts.append(f"不足の列 {len(missing_columns)} 件（{_limited_names(missing_columns)}）")
    pending = list(status.get("pending_versions") or [])
    if pending and state != "missing":
        parts.append(f"未適用の migration {len(pending)} 件")
    destructive = [
        str(item.get("name", "")) for item in status.get("pending_destructive_migrations") or []
    ]
    if destructive:
        parts.append(
            f"承認が要る削除 {len(destructive)} 件（{_limited_names(destructive)}。"
            "書き出してから --allow-destructive で初期化）"
        )
    if operation_status != "idle":
        parts.append(f"操作 {operation_status}")
    last_error = operation.get("last_error_code")
    if last_error:
        parts.append(f"前回の操作の失敗 {last_error}")
    return False, "・".join(parts)


def deployment_check_error_summary(
    exc: BaseException,
    *,
    connect_kwargs: ConnectKwargsFactory | None = None,
) -> str:
    """状態を読めないとき（接続・権限・設定）の要約。

    DB に接続できない失敗は、`SCHEMA_STATUS_UNAVAILABLE` の印に続けて、本当の原因のコードと
    理由・対処を出す（#1744。`connect_failure_diagnosis`。例
    `状態を読めません（SCHEMA_STATUS_UNAVAILABLE: DPY-6005: ADB が…）`）。印は機械の判定用で
    （#1768。配備の通しの .github/scripts/deploy-run-through.sh の
    `schema_summary_is_db_unreachable`）、理由・対処の文は変えずに出す。ORA の失敗（権限の
    不足など）は ORA コードだけを出す。ORA でない失敗は、印に続けて secret を含まない手がかり
    （`operation_failure_hint`。例外の型と message の 1 行目）を出す（#1701）。
    """

    diagnosis = connect_failure_diagnosis(exc, connect_kwargs=connect_kwargs)
    if diagnosis is not None:
        prefix = f"{diagnosis.code}: " if diagnosis.code else ""
        return f"状態を読めません（{SCHEMA_STATUS_UNAVAILABLE}: {prefix}{diagnosis.hint}）"
    if isinstance(exc, SystemSchemaError):
        code = exc.code
    else:
        code = oracle_error_code(exc)
        if code == SCHEMA_OPERATION_FAILED:
            code = f"{SCHEMA_STATUS_UNAVAILABLE}: {operation_failure_hint(exc)}"
    return f"状態を読めません（{code}）"


def system_tables_status_error(exc: BaseException) -> HTTPException:
    """状態を取得できないときの 503（公開するのは ORA コードだけ）。"""

    code = oracle_error_code(exc)
    safe_code = code if code.startswith("ORA-") else SCHEMA_STATUS_UNAVAILABLE
    return HTTPException(
        status_code=503,
        detail=f"システムテーブルの状態を取得できませんでした ({safe_code})。",
    )


# ---- 外部キーの差分（#505） ---------------------------------------------------
#
# 表の作成は「無ければ作る」なので、古い版で作った表には、後から正本に足した FK が無いまま残る。
# 正本の `CREATE TABLE` の FK と USER_CONSTRAINTS を、名前ではなく定義（表・列・参照先の表・列）で
# 比べる（旧版の migration が別名で作った FK を、重複して足さないため）。
#
# 参照先の無い既存の行（孤立した行）があると、検査付きの追加は ORA-02298 で失敗する。利用者の
# 操作なしに既存の行は消さないため、そのときは `ENABLE NOVALIDATE`（新しい行と更新から強制し、
# 既存の行は検査しない）で追加し、孤立した行の件数を状態に出す。
#
# 同じ定義の FK があっても、削除規則が正本と違う・無効化（DISABLED）されているものは差分にする
# （#511）。更新では、削除規則の違いは DROP と正本の ADD、無効化は ENABLE で直す（孤立した行の
# 扱いは追加と同じ）。孤立した行は、利用者が件数を確認して明示したときだけ削除し、その後に FK を
# VALIDATE する。

_IDENTIFIER_PATTERN = re.compile(r"^[A-Z][A-Z0-9_$#]*$")
_IDENTIFIER = r"[A-Za-z][A-Za-z0-9_$#]*"
_CREATE_TABLE_NAME_PATTERN = re.compile(
    rf"^\s*CREATE\s+TABLE\s+({_IDENTIFIER})\b",
    flags=re.IGNORECASE,
)
_TABLE_FOREIGN_KEY_PATTERN = re.compile(
    rf"CONSTRAINT\s+({_IDENTIFIER})\s+FOREIGN\s+KEY\s*\(([^)]*)\)\s*"
    rf"REFERENCES\s+({_IDENTIFIER})\s*\(([^)]*)\)"
    r"(?:\s+ON\s+DELETE\s+(CASCADE|SET\s+NULL))?",
    flags=re.IGNORECASE,
)
FOREIGN_KEY_DELETE_RULES = frozenset({"CASCADE", "SET NULL", "NO ACTION"})


def _identifier(value: str) -> str:
    name = value.strip().upper()
    if not _IDENTIFIER_PATTERN.match(name):
        raise ValueError(f"Oracle の識別子として扱えません: {value!r}")
    return name


def _identifiers(value: str) -> tuple[str, ...]:
    return tuple(_identifier(item) for item in value.split(","))


@dataclass(frozen=True, slots=True)
class ForeignKeySpec:
    """1 つの外部キーの定義（識別子は大文字。DDL に埋め込むため、識別子の形だけを許す）。"""

    name: str
    table_name: str
    columns: tuple[str, ...]
    referenced_table_name: str
    referenced_columns: tuple[str, ...]
    delete_rule: str = "NO ACTION"

    def __post_init__(self) -> None:
        identifiers = (
            self.name,
            self.table_name,
            self.referenced_table_name,
            *self.columns,
            *self.referenced_columns,
        )
        for value in identifiers:
            if not _IDENTIFIER_PATTERN.match(value):
                raise ValueError(f"Oracle の識別子として扱えません: {value!r}")
        if not self.columns or len(self.columns) != len(self.referenced_columns):
            raise ValueError(f"外部キー {self.name} の列と参照先の列の数が一致しません。")
        if self.delete_rule not in FOREIGN_KEY_DELETE_RULES:
            raise ValueError(f"外部キー {self.name} の削除規則が不正です: {self.delete_rule!r}")

    @property
    def signature(self) -> tuple[str, tuple[str, ...], str, tuple[str, ...]]:
        """名前を除いた定義（同じ FK かどうかの判定に使う）。"""

        return (self.table_name, self.columns, self.referenced_table_name, self.referenced_columns)

    def payload(self, *, orphan_rows: int | None) -> dict[str, Any]:
        """状態の API に出す形（`SystemTableForeignKeyData`）。"""

        return {
            "name": self.name,
            "table_name": self.table_name,
            "columns": list(self.columns),
            "referenced_table_name": self.referenced_table_name,
            "referenced_columns": list(self.referenced_columns),
            "delete_rule": self.delete_rule,
            "orphan_rows": orphan_rows,
        }


def foreign_keys_from_create_table(statement: str) -> list[ForeignKeySpec]:
    """`CREATE TABLE` 文の表制約 `CONSTRAINT <名前> FOREIGN KEY (...) REFERENCES ...` を取り出す。

    `CREATE TABLE` 以外の文（ALTER TABLE・PL/SQL の block）は空を返す。
    """

    table_match = _CREATE_TABLE_NAME_PATTERN.match(statement)
    if table_match is None:
        return []
    table_name = _identifier(table_match.group(1))
    specs: list[ForeignKeySpec] = []
    for match in _TABLE_FOREIGN_KEY_PATTERN.finditer(statement):
        name, columns, referenced_table, referenced_columns, delete_rule = match.groups()
        specs.append(
            ForeignKeySpec(
                name=_identifier(name),
                table_name=table_name,
                columns=_identifiers(columns),
                referenced_table_name=_identifier(referenced_table),
                referenced_columns=_identifiers(referenced_columns),
                delete_rule=" ".join(delete_rule.upper().split()) if delete_rule else "NO ACTION",
            )
        )
    return specs


# 表制約の行（列ではない）の先頭の語。
_TABLE_CONSTRAINT_KEYWORDS = frozenset({"CONSTRAINT", "PRIMARY", "UNIQUE", "FOREIGN", "CHECK"})
_ALTER_TABLE_PATTERN = re.compile(
    rf"^\s*ALTER\s+TABLE\s+({_IDENTIFIER})\s+(ADD|DROP|RENAME)\b",
    flags=re.IGNORECASE,
)
_DROP_TABLE_PATTERN = re.compile(rf"^\s*DROP\s+TABLE\s+({_IDENTIFIER})\b", flags=re.IGNORECASE)
_DROP_COLUMN_PATTERN = re.compile(rf"^\s*COLUMN\s+({_IDENTIFIER})\b", flags=re.IGNORECASE)
_RENAME_COLUMN_PATTERN = re.compile(
    rf"^\s*COLUMN\s+({_IDENTIFIER})\s+TO\s+({_IDENTIFIER})\b",
    flags=re.IGNORECASE,
)
_PLSQL_BLOCK_PATTERN = re.compile(r"^\s*(?:BEGIN|DECLARE)\b", flags=re.IGNORECASE)
_EXECUTE_IMMEDIATE_PATTERN = re.compile(r"\bEXECUTE\s+IMMEDIATE\s+", flags=re.IGNORECASE)
_Q_QUOTE_CLOSERS = {"[": "]", "(": ")", "{": "}", "<": ">"}


def _strip_sql_comments(sql: str) -> str:
    """`--` と `/* */` の注釈を除く（文字列リテラルの中の `--` は残す）。"""

    result: list[str] = []
    index = 0
    while index < len(sql):
        char = sql[index]
        if char == "'":
            end = _quoted_literal_end(sql, index)
            result.append(sql[index:end])
            index = end
        elif sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = len(sql) if newline < 0 else newline
        elif sql.startswith("/*", index):
            close = sql.find("*/", index + 2)
            index = len(sql) if close < 0 else close + 2
            result.append(" ")
        else:
            result.append(char)
            index += 1
    return "".join(result)


def _quoted_literal_end(sql: str, start: int) -> int:
    """`'...'`（`''` は引用符 1 つ）の終わりの次の位置。閉じていなければ末尾。"""

    index = start + 1
    while index < len(sql):
        if sql[index] == "'":
            if sql.startswith("''", index):
                index += 2
                continue
            return index + 1
        index += 1
    return len(sql)


def _string_literal_at(sql: str, start: int) -> tuple[str, int] | None:
    """`start` から始まる文字列リテラル（`'...'` と `q'[...]'`）の値と、終わりの次の位置。"""

    if sql[start : start + 2].upper() == "Q'" and start + 2 < len(sql):
        opener = sql[start + 2]
        closer = _Q_QUOTE_CLOSERS.get(opener, opener) + "'"
        close = sql.find(closer, start + 3)
        if close < 0:
            return None
        return sql[start + 3 : close], close + 2
    if sql[start : start + 1] != "'":
        return None
    end = _quoted_literal_end(sql, start)
    return sql[start + 1 : end - 1].replace("''", "'"), end


def _execute_immediate_statements(block: str) -> list[str]:
    """PL/SQL の block の `EXECUTE IMMEDIATE` に渡す文字列リテラルの文（`||` の連結を含む）。

    変数・関数で組み立てる文は読めないので飛ばす。
    """

    statements: list[str] = []
    for match in _EXECUTE_IMMEDIATE_PATTERN.finditer(block):
        parts: list[str] = []
        position = match.end()
        while (literal := _string_literal_at(block, position)) is not None:
            parts.append(literal[0])
            rest = block[literal[1] :]
            concat = re.match(r"\s*\|\|\s*", rest)
            if concat is None:
                break
            position = literal[1] + concat.end()
        if parts:
            statements.append("".join(parts))
    return statements


def _parenthesized_items(sql: str, start: int) -> list[str]:
    """`sql[start]` の `(` から対応する `)` までを、最上位の `,` で分けた項目。"""

    items: list[str] = []
    depth = 0
    current: list[str] = []
    index = start + 1
    while index < len(sql):
        char = sql[index]
        if char == "'":
            end = _quoted_literal_end(sql, index)
            current.append(sql[index:end])
            index = end
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
        elif char == "," and depth == 0:
            items.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    items.append("".join(current))
    return items


def _column_names(items: Iterable[str]) -> list[str]:
    """列の定義の項目から列名（表制約の項目は飛ばす）。"""

    columns: list[str] = []
    for item in items:
        words = item.split()
        if not words or words[0].upper() in _TABLE_CONSTRAINT_KEYWORDS:
            continue
        columns.append(_identifier(words[0]))
    return columns


def columns_from_create_table(statement: str) -> tuple[str, tuple[str, ...]] | None:
    """`CREATE TABLE` 文の表名と列名（大文字。定義の順）を取り出す（#2450）。

    表は「無ければ作る」ので、既存の表に正本の列が揃っているかは台帳では分からない（取り消した
    migration が列を改名・削除した表など）。状態の確認で USER_TAB_COLUMNS と比べるために使う。
    表制約（CONSTRAINT・PRIMARY KEY・CHECK など）の行と注釈は飛ばす。`CREATE TABLE`
    以外の文は None を返す。
    """

    body = _strip_sql_comments(statement)
    table_match = _CREATE_TABLE_NAME_PATTERN.match(body)
    if table_match is None:
        return None
    start = body.find("(", table_match.end())
    if start < 0:
        return None
    columns = _column_names(_parenthesized_items(body, start))
    return _identifier(table_match.group(1)), tuple(columns)


def columns_from_statements(statements: Iterable[str]) -> dict[str, tuple[str, ...]]:
    """DDL の文を順に当てたときの、表の列（表名 → 列名。定義の順。#2450）。

    正本が「最新の形の CREATE TABLE」でなく、版ごとの migration（後の版の `ALTER TABLE` で列を
    足す）のときに、全部を当てた後の列を求める。

    - `CREATE TABLE`: まだ無い表だけを作る（既にあれば ORA-00955 で読み飛ばされる）
    - `ALTER TABLE <表> ADD (<列> ..., ...)` / `ADD <列> ...`: 列を足す（表制約の追加は飛ばす）
    - `ALTER TABLE <表> DROP COLUMN <列>` / `DROP (<列>, ...)`: 列を除く
    - `ALTER TABLE <表> RENAME COLUMN <旧> TO <新>`: 列名を変える
    - `DROP TABLE <表>`: 表を除く
    - PL/SQL の block（`BEGIN` / `DECLARE`）: `EXECUTE IMMEDIATE` の文字列リテラルの文に同じ規則を
      当てる

    正本で作っていない表への `ALTER TABLE` は追わない。
    """

    columns: dict[str, list[str]] = {}
    for statement in statements:
        _apply_column_ddl(columns, statement)
    return {table: tuple(names) for table, names in columns.items()}


def _apply_column_ddl(columns: dict[str, list[str]], statement: str) -> None:
    sql = _strip_sql_comments(statement)
    if _PLSQL_BLOCK_PATTERN.match(sql):
        for nested in _execute_immediate_statements(sql):
            _apply_column_ddl(columns, nested)
        return
    created = columns_from_create_table(sql)
    if created is not None:
        columns.setdefault(created[0], list(created[1]))
        return
    if (dropped := _DROP_TABLE_PATTERN.match(sql)) is not None:
        columns.pop(_identifier(dropped.group(1)), None)
        return
    altered = _ALTER_TABLE_PATTERN.match(sql)
    if altered is None:
        return
    current = columns.get(_identifier(altered.group(1)))
    if current is None:
        return
    action = altered.group(2).upper()
    rest = sql[altered.end() :]
    if action == "ADD":
        stripped = rest.lstrip()
        items = _parenthesized_items(stripped, 0) if stripped.startswith("(") else [stripped]
        current.extend(name for name in _column_names(items) if name not in current)
    elif action == "DROP":
        stripped = rest.lstrip()
        if (single := _DROP_COLUMN_PATTERN.match(stripped)) is not None:
            names = [_identifier(single.group(1))]
        elif stripped.startswith("("):
            names = [_identifier(item) for item in _parenthesized_items(stripped, 0)]
        else:
            return
        current[:] = [name for name in current if name not in names]
    elif (renamed := _RENAME_COLUMN_PATTERN.match(rest)) is not None:
        old, new = _identifier(renamed.group(1)), _identifier(renamed.group(2))
        current[:] = [new if name == old else name for name in current]


def load_existing_columns(connection: Any, table_names: Iterable[str]) -> dict[str, set[str]]:
    """指定した表の列名（接続ユーザーの schema の USER_TAB_COLUMNS。無い表は含めない）。"""

    names = sorted(set(table_names))
    if not names:
        return {}
    placeholders, binds = bind_list("column_table_", names)
    columns: dict[str, set[str]] = {}
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT TABLE_NAME, COLUMN_NAME FROM USER_TAB_COLUMNS "
            f"WHERE TABLE_NAME IN ({placeholders})",  # nosec B608 - 固定の bind
            binds,
        )
        for table_name, column_name in cursor.fetchall():
            columns.setdefault(str(table_name).upper(), set()).add(str(column_name).upper())
    return columns


def missing_columns(
    expected: Mapping[str, Sequence[str]],
    existing: Mapping[str, Collection[str]],
) -> list[dict[str, str]]:
    """正本の列のうち、既存の表に無いもの（表が無いときは object の不足で出すので含めない）。"""

    return [
        {"table_name": table_name, "column_name": column_name}
        for table_name, columns in expected.items()
        if table_name in existing
        for column_name in columns
        if column_name not in existing[table_name]
    ]


def add_foreign_key_sql(spec: ForeignKeySpec, *, validate: bool) -> str:
    """既存の表に FK を足す DDL（`validate=False` は既存の行を検査しない `ENABLE NOVALIDATE`）。"""

    on_delete = "" if spec.delete_rule == "NO ACTION" else f" ON DELETE {spec.delete_rule}"
    state = "" if validate else " ENABLE NOVALIDATE"
    return (
        f"ALTER TABLE {spec.table_name} ADD CONSTRAINT {spec.name} "
        f"FOREIGN KEY ({', '.join(spec.columns)}) "
        f"REFERENCES {spec.referenced_table_name} ({', '.join(spec.referenced_columns)})"
        f"{on_delete}{state}"
    )


def drop_foreign_key_sql(spec: ForeignKeySpec) -> str:
    """既存の FK を削除する DDL（表の行は消えない。削除規則を正本に直す前に使う）。"""

    return f"ALTER TABLE {spec.table_name} DROP CONSTRAINT {spec.name}"


def enable_foreign_key_sql(spec: ForeignKeySpec, *, validate: bool) -> str:
    """無効化された FK を有効にする DDL（`validate=False` は既存の行を検査しない）。"""

    state = "ENABLE VALIDATE" if validate else "ENABLE NOVALIDATE"
    return f"ALTER TABLE {spec.table_name} MODIFY CONSTRAINT {spec.name} {state}"


def validate_foreign_key_sql(spec: ForeignKeySpec) -> str:
    """有効な FK で既存の行も検査する（検査済み・VALIDATED にする）DDL。"""

    return f"ALTER TABLE {spec.table_name} MODIFY CONSTRAINT {spec.name} VALIDATE"


def _orphan_predicate(spec: ForeignKeySpec) -> str:
    not_null = " AND ".join(f"child.{column} IS NOT NULL" for column in spec.columns)
    join = " AND ".join(
        f"parent.{referenced} = child.{column}"
        for column, referenced in zip(spec.columns, spec.referenced_columns, strict=True)
    )
    return (
        f"WHERE {not_null} AND NOT EXISTS "
        f"(SELECT 1 FROM {spec.referenced_table_name} parent WHERE {join})"  # nosec B608 - 検証済みの識別子
    )


def orphan_rows_sql(spec: ForeignKeySpec) -> str:
    """参照先の無い既存の行を数える SQL（FK の列のどれかが NULL の行は Oracle も検査しない）。"""

    return f"SELECT COUNT(*) FROM {spec.table_name} child {_orphan_predicate(spec)}"  # nosec B608 - 検証済みの識別子


def delete_orphan_rows_sql(spec: ForeignKeySpec) -> str:
    """参照先の無い既存の行を削除する SQL（数える SQL と同じ条件。#511）。"""

    return f"DELETE FROM {spec.table_name} child {_orphan_predicate(spec)}"  # nosec B608 - 検証済みの識別子


def count_orphan_rows(connection: Any, spec: ForeignKeySpec) -> int:
    """参照先の無い既存の行の件数。"""

    with connection.cursor() as cursor:
        cursor.execute(orphan_rows_sql(spec))
        row = cursor.fetchone()
    return int(row[0] or 0) if row else 0


@dataclass(frozen=True, slots=True)
class ExistingForeignKey:
    """USER_CONSTRAINTS にある FK と、その状態（ENABLED / VALIDATED）。"""

    spec: ForeignKeySpec
    enabled: bool
    validated: bool


def load_existing_tables(connection: Any, table_names: Iterable[str]) -> set[str]:
    """指定した表のうち、接続ユーザーの schema にあるもの。"""

    names = sorted(set(table_names))
    if not names:
        return set()
    placeholders, binds = bind_list("fk_table_", names)
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT TABLE_NAME FROM USER_TABLES WHERE TABLE_NAME IN ({placeholders})",  # nosec B608 - 固定の bind
            binds,
        )
        return {str(row[0]).upper() for row in cursor.fetchall()}


def load_foreign_keys(connection: Any, table_names: Iterable[str]) -> list[ExistingForeignKey]:
    """指定した表にある FK（参照先も同じ schema のもの）を、列の順番どおりに読む。"""

    names = sorted(set(table_names))
    if not names:
        return []
    placeholders, binds = bind_list("fk_child_", names)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT child.CONSTRAINT_NAME, child.TABLE_NAME, parent.TABLE_NAME, "
            "child.DELETE_RULE, child.STATUS, child.VALIDATED, "
            "child_column.COLUMN_NAME, parent_column.COLUMN_NAME "
            "FROM USER_CONSTRAINTS child "
            "JOIN USER_CONSTRAINTS parent "
            "ON parent.CONSTRAINT_NAME = child.R_CONSTRAINT_NAME "
            "JOIN USER_CONS_COLUMNS child_column "
            "ON child_column.CONSTRAINT_NAME = child.CONSTRAINT_NAME "
            "JOIN USER_CONS_COLUMNS parent_column "
            "ON parent_column.CONSTRAINT_NAME = child.R_CONSTRAINT_NAME "
            "AND parent_column.POSITION = child_column.POSITION "
            "WHERE child.CONSTRAINT_TYPE = 'R' AND child.R_OWNER = USER "
            f"AND child.TABLE_NAME IN ({placeholders}) "  # nosec B608 - 固定の bind
            "ORDER BY child.CONSTRAINT_NAME, child_column.POSITION",
            binds,
        )
        rows = cursor.fetchall()
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = grouped.setdefault(
            str(row[0]).upper(),
            {
                "table_name": str(row[1]).upper(),
                "referenced_table_name": str(row[2]).upper(),
                "delete_rule": str(row[3] or "NO ACTION").upper(),
                "enabled": str(row[4]).upper() == "ENABLED",
                "validated": str(row[5]).upper() == "VALIDATED",
                "columns": [],
                "referenced_columns": [],
            },
        )
        entry["columns"].append(str(row[6]).upper())
        entry["referenced_columns"].append(str(row[7]).upper())
    existing: list[ExistingForeignKey] = []
    for name, entry in grouped.items():
        try:
            spec = ForeignKeySpec(
                name=name,
                table_name=entry["table_name"],
                columns=tuple(entry["columns"]),
                referenced_table_name=entry["referenced_table_name"],
                referenced_columns=tuple(entry["referenced_columns"]),
                delete_rule=entry["delete_rule"],
            )
        except ValueError:
            # 引用符付きの名前など、正本の定義と一致しえない FK は比べない。
            continue
        existing.append(
            ExistingForeignKey(spec=spec, enabled=entry["enabled"], validated=entry["validated"])
        )
    return existing


@dataclass(frozen=True, slots=True)
class ForeignKeyMismatch:
    """同じ定義（表・列・参照先）の FK があるが、削除規則が正本と違う（#511）。"""

    expected: ForeignKeySpec
    current: ForeignKeySpec
    orphan_rows: int | None

    def payload(self) -> dict[str, Any]:
        return {
            **self.expected.payload(orphan_rows=self.orphan_rows),
            "current_name": self.current.name,
            "current_delete_rule": self.current.delete_rule,
        }


@dataclass(frozen=True, slots=True)
class ForeignKeyDrift:
    """正本の FK と既存の表の差分。

    - `missing`: 子・参照先の表はあるのに、同じ定義の FK が無い（更新で追加する）。孤立した行の
      件数付き（数えられなかったときは None）
    - `mismatched`: 同じ定義の FK があるが、削除規則が正本と違う（更新で DROP と ADD。#511）
    - `disabled`: 同じ定義・削除規則の FK があるが、無効化されている（更新で ENABLE。#511）
    - `orphaned`: 既存の FK が既存の行を検査しておらず（NOVALIDATE）、孤立した行が残っている
    """

    missing: list[tuple[ForeignKeySpec, int | None]] = field(default_factory=list)
    orphaned: list[tuple[ForeignKeySpec, int]] = field(default_factory=list)
    mismatched: list[ForeignKeyMismatch] = field(default_factory=list)
    disabled: list[tuple[ForeignKeySpec, int | None]] = field(default_factory=list)

    @property
    def current(self) -> bool:
        """更新で直す差分が無い（孤立した行の警告は状態を変えない）。"""

        return not (self.missing or self.mismatched or self.disabled)

    def status_fields(self) -> dict[str, list[dict[str, Any]]]:
        """状態の `missing_foreign_keys` / `mismatched_foreign_keys` / `disabled_foreign_keys` /
        `orphaned_foreign_keys`。"""

        return {
            "missing_foreign_keys": [
                spec.payload(orphan_rows=orphans) for spec, orphans in self.missing
            ],
            "mismatched_foreign_keys": [item.payload() for item in self.mismatched],
            "disabled_foreign_keys": [
                spec.payload(orphan_rows=orphans) for spec, orphans in self.disabled
            ],
            "orphaned_foreign_keys": [
                spec.payload(orphan_rows=orphans) for spec, orphans in self.orphaned
            ],
        }


def _count_orphan_rows_or_none(connection: Any, spec: ForeignKeySpec) -> int | None:
    """状態の取得では、数えられない（列がまだ無い等）ときに状態ごと失敗させない。"""

    try:
        return count_orphan_rows(connection, spec)
    except Exception:
        return None


def _comparable_foreign_keys(
    connection: Any, expected: Sequence[ForeignKeySpec]
) -> tuple[list[ForeignKeySpec], dict[tuple[Any, ...], ExistingForeignKey]]:
    """子と参照先の表がある正本の FK と、既存の FK（定義 → FK）。"""

    if not expected:
        return [], {}
    tables = load_existing_tables(
        connection,
        (name for spec in expected for name in (spec.table_name, spec.referenced_table_name)),
    )
    comparable = [
        spec
        for spec in expected
        if spec.table_name in tables and spec.referenced_table_name in tables
    ]
    if not comparable:
        return [], {}
    existing = load_foreign_keys(connection, (spec.table_name for spec in comparable))
    return comparable, {item.spec.signature: item for item in existing}


def inspect_foreign_keys(connection: Any, expected: Sequence[ForeignKeySpec]) -> ForeignKeyDrift:
    """正本の FK と USER_CONSTRAINTS を比べる（DDL は実行しない）。

    子か参照先の表が無い FK は比べない（表の作成で FK ごと作られる）。孤立した行は、不足している
    FK、削除規則が違う・無効化されている FK と、既存の行を検査していない FK だけで数える（検査済みの
    FK では 0 件のため）。削除規則の違いと無効化が重なるときは、削除規則の違いとして扱う（DROP と
    ADD で両方が直る）。
    """

    comparable, by_signature = _comparable_foreign_keys(connection, expected)
    drift = ForeignKeyDrift()
    for spec in comparable:
        current = by_signature.get(spec.signature)
        if current is None:
            drift.missing.append((spec, _count_orphan_rows_or_none(connection, spec)))
            continue
        if current.spec.delete_rule != spec.delete_rule:
            orphans = _count_orphan_rows_or_none(connection, spec)
            drift.mismatched.append(ForeignKeyMismatch(spec, current.spec, orphans))
            continue
        if not current.enabled:
            drift.disabled.append(
                (current.spec, _count_orphan_rows_or_none(connection, current.spec))
            )
            continue
        if not current.validated:
            orphans = _count_orphan_rows_or_none(connection, current.spec)
            if orphans:
                drift.orphaned.append((current.spec, orphans))
    return drift


class SystemSchemaManagerBase(ABC):
    """状態の取得・作成 / 更新・全再作成を、同じ manifest と lease で提供する骨格。

    製品は制御テーブル・台帳の名前と確認語を class 属性で渡し、次を実装する。
    `_status_on`（状態の組み立て）・`_ensure_control_schema`（制御テーブルの作成）・
    `_initialize_on`（lease を取った後の手順）。
    """

    control_table: ClassVar[str]
    migration_table: ClassVar[str]
    # 台帳のキー列（RAG は `MIGRATION_NAME`、NL2SQL は `VERSION_NO`）。
    migration_key_column: ClassVar[str]
    # 状態の `tables` に並べるテーブル（作成の依存順）。
    managed_tables: ClassVar[tuple[str, ...]]
    recreate_confirmation: ClassVar[str]
    # ログのイベント名の接頭辞（`rag` → `rag_system_schema_operation_succeeded`）。
    log_prefix: ClassVar[str]
    # ORA-00054 のときの製品固有の対処（「〜してから、」まで。後に「状態を再取得…」が続く）。
    lock_timeout_guidance: ClassVar[str] = ""
    control_key: ClassVar[str] = CONTROL_KEY
    ignored_drop_codes: ClassVar[frozenset[str]] = IGNORED_DROP_CODES
    # 正本の外部キー（既存の表に無いものを状態に出し、更新で足す。#505）。空なら比べない。
    managed_foreign_keys: ClassVar[tuple[ForeignKeySpec, ...]] = ()
    # 正本の表の列（表名 → 列名。`columns_from_statements` で DDL の正本から作る）。台帳が適用済み
    # でも既存の表に欠けた列を状態の `missing_columns` に出す（#2450）。空なら比べない。
    managed_columns: ClassVar[Mapping[str, tuple[str, ...]]] = {}
    # 状態に `pending_destructive_migrations`（データを消す未適用の migration）を出す製品は True に
    # する。作成・更新の前に状態を読み、承認が無ければ止める（#619）。False なら確かめない。
    guards_destructive_migrations: ClassVar[bool] = False
    logger: ClassVar[logging.Logger] = logging.getLogger(__name__)

    def __init__(
        self,
        connection_factory: ConnectionFactory,
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        ddl_lock_timeout_seconds: int,
    ) -> None:
        self._connection_factory = connection_factory
        self._lease_seconds = max(MIN_LEASE_SECONDS, int(lease_seconds))
        self._ddl_lock_timeout_seconds = clamp_ddl_lock_timeout(ddl_lock_timeout_seconds)

    # ---- 製品が実装するもの -------------------------------------------------

    @abstractmethod
    def _status_on(self, connection: Any) -> dict[str, Any]:
        """USER_* dictionary と台帳から公開の状態を組み立てる（DDL は実行しない）。"""

    @abstractmethod
    def _ensure_control_schema(self) -> None:
        """lease を取る前に、制御テーブルと台帳を冪等に用意する。"""

    @abstractmethod
    def _initialize_on(self, connection: Any, owner: str, *, recreate: bool) -> dict[str, Any]:
        """lease を取った後の作成・更新 / 全再作成。成功時は状態と操作の結果を返す。"""

    def _operation_kind(self, recreate: bool) -> str:
        """制御テーブルの OPERATION_KIND に書く値（CHECK 制約に合わせて製品が変えてよい）。"""

        return "recreate" if recreate else "initialize"

    def _migration_key(self, value: Any) -> Any:
        """台帳のキー列の値を、manifest の migration のキーの型にそろえる。"""

        return str(value)

    def _table_identity_fields(self, name: str, owner: str) -> dict[str, Any]:
        """状態の `tables` の各行に足す項目（NL2SQL は所有者付きの名前）。"""

        return {}

    # ---- 公開の操作 ---------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """USER_* dictionary と台帳だけを読む。DDL は実行しない。"""

        with self._connection_factory() as connection:
            return self._status_on(connection)

    def is_ready(self) -> bool:
        """worker / readiness gate 向けの副作用なし判定。"""

        data = self.status()
        return bool(data["status"] == "ready" and data["operation_state"]["status"] != "running")

    def initialize(
        self,
        *,
        recreate: bool = False,
        confirmation: str | None = None,
        allow_destructive: bool = False,
    ) -> dict[str, Any]:
        """作成・更新（不足分と未適用の migration）、または確認語付きの全再作成。

        作成・更新は、データを消す未適用の migration（状態の `pending_destructive_migrations`。
        製品が返す）があれば、`allow_destructive` が無い限り DB を変えずに 409 で止める（#619）。
        全再作成は確認語で承認済みのため確かめない。
        """

        require_recreate_confirmation(
            recreate=recreate,
            confirmation=confirmation,
            expected=self.recreate_confirmation,
        )
        if self.guards_destructive_migrations and not recreate and not allow_destructive:
            # 状態の読み取りだけ（DDL なし・lease なし）。止めても失敗として記録しない。
            require_destructive_migration_confirmation(
                self.status().get("pending_destructive_migrations") or [],
                allow_destructive=False,
            )
        owner = uuid.uuid4().hex
        kind = self._operation_kind(recreate)
        self._ensure_control_schema()
        self._claim_lease(owner, kind)
        # DB ゲートの `ok` の cache を、操作の間（実行中は setup_required）と後に使わない（#793）。
        clear_database_status_cache()
        try:
            with self._connection_factory() as connection:
                return self._initialize_on(connection, owner, recreate=recreate)
        except SystemSchemaError as exc:
            self._record_failure(owner, exc.code)
            self._log_operation_failed(kind, exc.code, exc)
            raise
        except Exception as exc:
            code = oracle_error_code(exc)
            self._record_failure(owner, code)
            self._log_operation_failed(kind, code, exc)
            raise self._operation_error(code) from exc
        finally:
            clear_database_status_cache()

    # ---- 手順の部品 ---------------------------------------------------------

    def _no_op_result(self, connection: Any, owner: str) -> dict[str, Any]:
        """最新のときは epoch を増やさずに lease を返し、変更なしの結果にする。"""

        self._finish_operation(connection, owner, increment_epoch=False)
        return {
            **self._status_on(connection),
            "operation": "no_op",
            "dropped_object_count": 0,
            "created_object_count": 0,
        }

    @staticmethod
    def _operation_name(before_status: str, *, recreate: bool) -> SystemSchemaOperation:
        if recreate:
            return "recreated"
        if before_status == "missing":
            return "initialized"
        return "migrated"

    def _log_operation_succeeded(
        self,
        operation: SystemSchemaOperation,
        after: Mapping[str, Any],
        *,
        dropped_object_count: int,
        **extra: Any,
    ) -> None:
        self.logger.info(
            f"{self.log_prefix}_system_schema_operation_succeeded",
            extra={
                "operation": operation,
                "schema_epoch": after["operation_state"]["schema_epoch"],
                **extra,
                "dropped_object_count": dropped_object_count,
            },
        )

    def _log_operation_failed(self, kind: str, code: str, exc: BaseException) -> None:
        self.logger.error(
            f"{self.log_prefix}_system_schema_operation_failed",
            extra={
                "operation": kind.lower(),
                "error_code": code,
                "exception_type": type(exc).__name__,
            },
        )

    def _operation_error(self, code: str) -> SystemSchemaError:
        """想定外の例外を、SQL や資格情報を含まない公開のエラーにする。"""

        if code == ORA_RESOURCE_BUSY:
            return SystemSchemaError(
                code,
                "Oracle の対象オブジェクトのロックが "
                f"{self._ddl_lock_timeout_seconds} 秒以内に解放されませんでした "
                f"(ORA-00054)。{self.lock_timeout_guidance}"
                "状態を再取得して再試行してください。",
                status_code=409,
            )
        return SystemSchemaError(
            code,
            f"システムテーブル操作に失敗しました ({code})。状態を再取得して再試行してください。",
        )

    # ---- 操作の lease -------------------------------------------------------

    def _operation_payload(
        self,
        connection: Any,
        objects: Mapping[tuple[str, str], Any],
    ) -> dict[str, Any]:
        if (self.control_table, "TABLE") not in objects:
            return idle_operation_state()
        return self._load_operation(connection)

    def _load_operation(self, connection: Any) -> dict[str, Any]:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT STATUS, OPERATION_KIND, LEASE_EXPIRES_AT, LAST_ERROR_CODE, "
                f"SCHEMA_EPOCH, UPDATED_AT FROM {self.control_table} "  # nosec B608 - 固定の識別子
                "WHERE OPERATION_KEY = :operation_key",
                {"operation_key": self.control_key},
            )
            row = cursor.fetchone()
        if row is None:
            return idle_operation_state()
        return {
            "status": str(row[0]).lower(),
            "operation_kind": str(row[1]).lower() if row[1] else None,
            "lease_expires_at": iso_timestamp(row[2]),
            "last_error_code": str(row[3]) if row[3] else None,
            "schema_epoch": int(row[4] or 0),
            "updated_at": iso_timestamp(row[5]),
        }

    def _claim_lease(self, owner: str, operation_kind: str) -> None:
        """実行中でない（または期限切れの）ときだけ lease を取る。取れなければ 409。"""

        with self._connection_factory() as connection, connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {self.control_table}
                   SET STATUS = 'RUNNING',
                       OPERATION_KIND = :operation_kind,
                       LEASE_OWNER = :lease_owner,
                       LEASE_EXPIRES_AT = SYSTIMESTAMP
                           + NUMTODSINTERVAL(:lease_seconds, 'SECOND'),
                       LAST_ERROR_CODE = NULL,
                       UPDATED_AT = SYSTIMESTAMP
                 WHERE OPERATION_KEY = :operation_key
                   AND (
                       STATUS <> 'RUNNING'
                       OR LEASE_EXPIRES_AT IS NULL
                       OR LEASE_EXPIRES_AT < SYSTIMESTAMP
                   )
                """,  # nosec B608 - 固定の識別子
                {
                    "operation_kind": operation_kind,
                    "lease_owner": owner,
                    "lease_seconds": self._lease_seconds,
                    "operation_key": self.control_key,
                },
            )
            claimed = int(cursor.rowcount or 0) == 1
            connection.commit()
        if not claimed:
            raise SystemSchemaBusyError()

    def _heartbeat(self, connection: Any, owner: str) -> None:
        """長い操作の途中で lease を延長する。他者に奪われていたら 409。"""

        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE {self.control_table}
                   SET LEASE_EXPIRES_AT = SYSTIMESTAMP
                           + NUMTODSINTERVAL(:lease_seconds, 'SECOND'),
                       UPDATED_AT = SYSTIMESTAMP
                 WHERE OPERATION_KEY = :operation_key
                   AND STATUS = 'RUNNING'
                   AND LEASE_OWNER = :lease_owner
                """,  # nosec B608 - 固定の識別子
                {
                    "lease_seconds": self._lease_seconds,
                    "operation_key": self.control_key,
                    "lease_owner": owner,
                },
            )
            renewed = int(cursor.rowcount or 0) == 1
            connection.commit()
        if not renewed:
            raise SystemSchemaBusyError()

    def _finish_operation(
        self,
        connection: Any,
        owner: str,
        *,
        increment_epoch: bool,
    ) -> None:
        epoch_sql = "SCHEMA_EPOCH + 1" if increment_epoch else "SCHEMA_EPOCH"
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {self.control_table} "  # nosec B608 - 固定の識別子と式
                "SET STATUS = 'IDLE', OPERATION_KIND = NULL, LEASE_OWNER = NULL, "
                "LEASE_EXPIRES_AT = NULL, LAST_ERROR_CODE = NULL, "
                f"SCHEMA_EPOCH = {epoch_sql}, UPDATED_AT = SYSTIMESTAMP "
                "WHERE OPERATION_KEY = :operation_key AND LEASE_OWNER = :lease_owner",
                {"operation_key": self.control_key, "lease_owner": owner},
            )
            finished = int(cursor.rowcount or 0) == 1
            connection.commit()
        if not finished:
            raise SystemSchemaBusyError()

    def _record_failure(self, owner: str, error_code: str) -> None:
        """失敗を記録して lease を返す（記録自体の失敗は握りつぶす）。"""

        with (
            suppress(Exception),
            self._connection_factory() as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute(
                f"""
                UPDATE {self.control_table}
                   SET STATUS = 'FAILED', OPERATION_KIND = NULL, LEASE_OWNER = NULL,
                       LEASE_EXPIRES_AT = NULL, LAST_ERROR_CODE = :error_code,
                       UPDATED_AT = SYSTIMESTAMP
                 WHERE OPERATION_KEY = :operation_key AND LEASE_OWNER = :lease_owner
                """,  # nosec B608 - 固定の識別子
                {
                    "error_code": error_code[:64],
                    "operation_key": self.control_key,
                    "lease_owner": owner,
                },
            )
            connection.commit()

    # ---- 台帳 ---------------------------------------------------------------

    def _load_migrations(
        self,
        connection: Any,
        objects: Mapping[tuple[str, str], Any],
    ) -> dict[Any, str]:
        """適用済み migration のキー → checksum（台帳が無ければ空）。"""

        if (self.migration_table, "TABLE") not in objects:
            return {}
        key = self.migration_key_column
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT {key}, CHECKSUM FROM {self.migration_table} "  # nosec B608 - 固定の識別子
                f"ORDER BY {key}"
            )
            return {self._migration_key(row[0]): str(row[1]) for row in cursor.fetchall()}

    def _merge_migration(
        self,
        cursor: Any,
        *,
        key: Any,
        description: str,
        checksum: str,
    ) -> None:
        """台帳に 1 migration を記録する（同じキーは checksum と適用日時を更新する）。"""

        column = self.migration_key_column
        bind = column.lower()
        cursor.execute(
            f"""
            MERGE INTO {self.migration_table} target
            USING (
                SELECT :{bind} AS {column},
                       :description AS DESCRIPTION,
                       :checksum AS CHECKSUM
                FROM DUAL
            ) source
            ON (target.{column} = source.{column})
            WHEN MATCHED THEN UPDATE SET
                target.DESCRIPTION = source.DESCRIPTION,
                target.CHECKSUM = source.CHECKSUM,
                target.APPLIED_AT = SYSTIMESTAMP
            WHEN NOT MATCHED THEN INSERT
                ({column}, DESCRIPTION, CHECKSUM, APPLIED_AT)
            VALUES
                (source.{column}, source.DESCRIPTION, source.CHECKSUM, SYSTIMESTAMP)
            """,  # nosec B608 - 固定の識別子
            {bind: key, "description": description, "checksum": checksum},
        )

    # ---- dictionary と DDL の部品 -------------------------------------------

    def _load_table_metadata(
        self,
        connection: Any,
        objects: Mapping[tuple[str, str], Any],
        *,
        owner: str = "",
    ) -> list[dict[str, Any]]:
        """管理対象テーブルの存在・推定行数・作成日時・統計日時（USER_TABLES の概算）。"""

        existing_names = [name for name in self.managed_tables if (name, "TABLE") in objects]
        metadata: dict[str, tuple[Any, Any]] = {}
        if existing_names:
            placeholders, binds = bind_list("table_name_", existing_names)
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT TABLE_NAME, NUM_ROWS, LAST_ANALYZED FROM USER_TABLES "
                    f"WHERE TABLE_NAME IN ({placeholders})",  # nosec B608 - 固定の bind
                    binds,
                )
                metadata = {str(row[0]).upper(): (row[1], row[2]) for row in cursor.fetchall()}
        return [
            {
                "name": name,
                **self._table_identity_fields(name, owner),
                "exists": (name, "TABLE") in objects,
                "estimated_rows": (
                    int(metadata[name][0])
                    if name in metadata and metadata[name][0] is not None
                    else None
                ),
                "created_at": iso_timestamp(objects.get((name, "TABLE"))),
                "last_analyzed_at": iso_timestamp(metadata[name][1]) if name in metadata else None,
            }
            for name in self.managed_tables
        ]

    def _configure_ddl_lock_timeout(self, connection: Any) -> None:
        """schema 操作の session に上限付きの DDL のロック待ちを設定する。"""

        with connection.cursor() as cursor:
            cursor.execute(
                f"ALTER SESSION SET DDL_LOCK_TIMEOUT = {self._ddl_lock_timeout_seconds}"  # nosec B608 - 上限付きの整数
            )

    @staticmethod
    def _execute_statements(
        connection: Any,
        statements: Sequence[str],
        *,
        ignored_codes: frozenset[str] = IGNORED_APPLY_CODES,
    ) -> None:
        """DDL を順に実行する（既にある等の `ignored_codes` は読み飛ばす）。"""

        with connection.cursor() as cursor:
            for statement in statements:
                try:
                    cursor.execute(statement)
                except Exception as exc:
                    if oracle_error_code(exc) in ignored_codes:
                        continue
                    raise
            connection.commit()

    # ---- 列の欠落（#2450） ---------------------------------------------------

    def _missing_columns(
        self, connection: Any, objects: Collection[tuple[str, str]]
    ) -> list[dict[str, str]]:
        """`managed_columns` のうち既存の表に無い列（DDL は実行しない）。

        表は「無ければ作る」ので、台帳が適用済みでも既存の表に列が欠けることがある（取り消した
        migration が列を改名・削除した表など）。表そのものが無いときは object の不足で出す。
        """

        if not self.managed_columns:
            return []
        present = [name for name in self.managed_columns if (name, "TABLE") in objects]
        return missing_columns(self.managed_columns, load_existing_columns(connection, present))

    @staticmethod
    def _require_columns_present(status: Mapping[str, Any]) -> None:
        """作成・更新の後も列が欠けていれば、欠けた列の名前を出して止める（#2450）。

        列を足す migration が台帳で適用済みなので、更新を再試行しても直らない。
        """

        absent = status.get("missing_columns") or []
        if not absent:
            return
        columns = ", ".join(f"{item['table_name']}.{item['column_name']}" for item in absent)
        raise SystemSchemaError(
            SCHEMA_POSTCONDITION_FAILED,
            f"システムテーブル操作後も必須の列が不足しています（{columns}）。"
            "更新を再試行しても直りません。配備の手順の既存環境の更新手順を確認してください。",
        )

    # ---- 外部キーの差分（#505） ---------------------------------------------

    def _foreign_key_drift(self, connection: Any) -> ForeignKeyDrift:
        """`managed_foreign_keys` と既存の表の差分（DDL は実行しない）。"""

        return inspect_foreign_keys(connection, self.managed_foreign_keys)

    def _apply_missing_foreign_keys(
        self,
        connection: Any,
        owner: str,
        drift: ForeignKeyDrift | None = None,
    ) -> list[dict[str, Any]]:
        """不足している FK を足す。既存の行は削除しない。

        孤立した行が無ければ既存の行も検査して足す。孤立した行があれば（または検査付きの追加が
        ORA-02298 で失敗したら）、`ENABLE NOVALIDATE` で足す（新しい行と更新から強制する）。
        戻り値は足した FK（`validated` は既存の行を検査したか、`orphan_rows` は孤立した行の件数）。
        """

        added: list[dict[str, Any]] = []
        current = drift if drift is not None else self._foreign_key_drift(connection)
        for spec, orphans in current.missing:
            self._heartbeat(connection, owner)
            validated = self._add_foreign_key(connection, spec, validate=not orphans)
            if validated is None:
                continue
            added.append({**spec.payload(orphan_rows=orphans), "validated": validated})
        return added

    def _repair_foreign_keys(self, connection: Any, owner: str) -> dict[str, list[dict[str, Any]]]:
        """外部キーの差分を直す（#505 / #511）。既存の行は削除しない。

        - 不足: 追加する（`_apply_missing_foreign_keys`）
        - 削除規則の違い: 既存の FK を DROP して、正本の定義で ADD する
        - 無効化: ENABLE する
        いずれも孤立した行があれば既存の行を検査しない（NOVALIDATE）。戻り値は直した FK の一覧
        （`added` / `recreated` / `enabled`。各項目に `validated`）。
        """

        drift = self._foreign_key_drift(connection)
        added = self._apply_missing_foreign_keys(connection, owner, drift)
        recreated: list[dict[str, Any]] = []
        for mismatch in drift.mismatched:
            self._heartbeat(connection, owner)
            self._drop_foreign_key(connection, mismatch.current)
            validated = self._add_foreign_key(
                connection, mismatch.expected, validate=not mismatch.orphan_rows
            )
            if validated is None:
                continue
            recreated.append({**mismatch.payload(), "validated": validated})
        enabled: list[dict[str, Any]] = []
        for spec, orphans in drift.disabled:
            self._heartbeat(connection, owner)
            validated = self._enable_foreign_key(connection, spec, validate=not orphans)
            enabled.append({**spec.payload(orphan_rows=orphans), "validated": validated})
        return {"added": added, "recreated": recreated, "enabled": enabled}

    @staticmethod
    def _add_foreign_key(connection: Any, spec: ForeignKeySpec, *, validate: bool) -> bool | None:
        """FK を 1 つ足す。検査して足せたら True、NOVALIDATE なら False、既にあれば None。"""

        with connection.cursor() as cursor:
            try:
                cursor.execute(add_foreign_key_sql(spec, validate=validate))
            except Exception as exc:
                code = oracle_error_code(exc)
                if code == ORA_FOREIGN_KEY_EXISTS:
                    return None
                if not (validate and code == ORA_ORPHAN_ROWS):
                    raise
                # 数えた後に孤立した行が増えた。既存の行は検査せずに足す。
                cursor.execute(add_foreign_key_sql(spec, validate=False))
                validate = False
            connection.commit()
        return validate

    @staticmethod
    def _drop_foreign_key(connection: Any, spec: ForeignKeySpec) -> None:
        """既存の FK を削除する（既に無いときは何もしない）。表の行は消えない。"""

        with connection.cursor() as cursor:
            try:
                cursor.execute(drop_foreign_key_sql(spec))
            except Exception as exc:
                if oracle_error_code(exc) != ORA_CONSTRAINT_NOT_FOUND:
                    raise
            connection.commit()

    @staticmethod
    def _enable_foreign_key(connection: Any, spec: ForeignKeySpec, *, validate: bool) -> bool:
        """無効化された FK を有効にする。検査できたら True、NOVALIDATE なら False。"""

        with connection.cursor() as cursor:
            try:
                cursor.execute(enable_foreign_key_sql(spec, validate=validate))
            except Exception as exc:
                if not (validate and oracle_error_code(exc) == ORA_ORPHAN_ROWS):
                    raise
                # 数えた後に孤立した行が増えた。既存の行は検査せずに有効にする。
                cursor.execute(enable_foreign_key_sql(spec, validate=False))
                validate = False
            connection.commit()
        return validate

    # ---- 参照先の無い行の削除（#511） ----------------------------------------

    def delete_orphaned_rows(
        self,
        *,
        constraint_name: str,
        expected_orphan_rows: int,
    ) -> dict[str, Any]:
        """管理対象の FK の、参照先の無い行を削除して FK を VALIDATE する（利用者の明示操作）。

        作成・更新と同じ lease を取る（制御テーブルの OPERATION_KIND の CHECK 制約に合わせ、
        種類は作成・更新と同じにする）。削除の直前に件数を数え直し、`expected_orphan_rows`
        （利用者が確認した件数）より多ければ削除しない。FK が無い・無効・削除規則が違うときは、
        先に作成・更新で直すよう 409 を返す。
        """

        try:
            target_name = _identifier(constraint_name)
        except ValueError:
            raise self._foreign_key_not_found(constraint_name) from None
        owner = uuid.uuid4().hex
        self._ensure_control_schema()
        self._claim_lease(owner, self._operation_kind(False))
        clear_database_status_cache()
        try:
            with self._connection_factory() as connection:
                return self._delete_orphaned_rows_on(
                    connection,
                    owner,
                    target_name,
                    expected_orphan_rows=max(0, int(expected_orphan_rows)),
                )
        except SystemSchemaPreconditionError:
            # 前提の確認で止めた（DB は変えていない）。lease は返してあり、失敗として記録しない。
            raise
        except SystemSchemaError as exc:
            self._record_failure(owner, exc.code)
            self._log_operation_failed(ORPHAN_DELETION_OPERATION, exc.code, exc)
            raise
        except Exception as exc:
            code = oracle_error_code(exc)
            self._record_failure(owner, code)
            self._log_operation_failed(ORPHAN_DELETION_OPERATION, code, exc)
            if code == ORA_CHILD_RECORD_FOUND:
                raise SystemSchemaError(
                    code,
                    "参照先のない行を、別の表の行が参照しているため削除できません "
                    f"({code})。参照している表の外部キーの、参照先のない行を先に削除してから、"
                    "状態を再取得して再試行してください。",
                    status_code=409,
                ) from exc
            raise self._operation_error(code) from exc

    def _delete_orphaned_rows_on(
        self,
        connection: Any,
        owner: str,
        constraint_name: str,
        *,
        expected_orphan_rows: int,
    ) -> dict[str, Any]:
        self._configure_ddl_lock_timeout(connection)
        try:
            target = self._orphan_deletion_target(connection, constraint_name)
            orphans = count_orphan_rows(connection, target.spec)
            if orphans > expected_orphan_rows:
                raise SystemSchemaPreconditionError(
                    ORPHAN_ROWS_CHANGED,
                    "参照先のない行の件数が確認したときより増えています"
                    f"（確認時 {expected_orphan_rows} 件、現在 {orphans} 件）。"
                    "状態を再取得し、件数を確認してから再実行してください。",
                    status_code=409,
                )
        except SystemSchemaPreconditionError:
            self._finish_operation(connection, owner, increment_epoch=False)
            raise
        if orphans == 0 and target.validated:
            self._finish_operation(connection, owner, increment_epoch=False)
            return self._orphan_deletion_result(
                connection, target.spec, operation="no_op", deleted=0
            )

        deleted = self._delete_orphans_and_validate(connection, owner, target.spec)
        self._finish_operation(connection, owner, increment_epoch=True)
        result = self._orphan_deletion_result(
            connection, target.spec, operation="orphans_deleted", deleted=deleted
        )
        # 監査のため、対象（表・FK）と、確認した件数・削除した件数を残す。
        self.logger.info(
            f"{self.log_prefix}_system_schema_operation_succeeded",
            extra={
                "operation": "orphans_deleted",
                "schema_epoch": result["operation_state"]["schema_epoch"],
                "constraint_name": target.spec.name,
                "table_name": target.spec.table_name,
                "referenced_table_name": target.spec.referenced_table_name,
                "expected_orphan_rows": expected_orphan_rows,
                "deleted_row_count": deleted,
            },
        )
        return result

    def _orphan_deletion_target(self, connection: Any, constraint_name: str) -> ExistingForeignKey:
        """名前で指定された、管理対象（正本と同じ定義）の既存の FK。"""

        comparable, by_signature = _comparable_foreign_keys(connection, self.managed_foreign_keys)
        for spec in comparable:
            current = by_signature.get(spec.signature)
            if current is None or current.spec.name != constraint_name:
                continue
            if not current.enabled or current.spec.delete_rule != spec.delete_rule:
                raise SystemSchemaPreconditionError(
                    FOREIGN_KEY_OUTDATED,
                    f"外部キー {constraint_name} は無効化されているか、削除規則が正本と"
                    "異なります。先に「作成・更新」を実行してから、状態を再取得してください。",
                    status_code=409,
                )
            return current
        raise self._foreign_key_not_found(constraint_name)

    @staticmethod
    def _foreign_key_not_found(constraint_name: str) -> SystemSchemaPreconditionError:
        # 識別子の形でない入力は、文言にそのまま出さない。
        name = constraint_name.strip().upper()
        shown = name if len(name) <= 128 and _IDENTIFIER_PATTERN.match(name) else "-"
        return SystemSchemaPreconditionError(
            FOREIGN_KEY_NOT_FOUND,
            f"管理対象の外部キー {shown} が見つかりません。状態を再取得してください。",
            status_code=404,
        )

    def _delete_orphans_and_validate(
        self, connection: Any, owner: str, spec: ForeignKeySpec
    ) -> int:
        """孤立した行を削除して commit し、FK を VALIDATE する。削除した行数を返す。

        VALIDATE が ORA-02298 になったとき（数えた後に孤立した行ができた）は、削除と VALIDATE を
        1 回だけやり直す。
        """

        deleted = 0
        attempts = 2
        for attempt in range(attempts):
            self._heartbeat(connection, owner)
            with connection.cursor() as cursor:
                cursor.execute(delete_orphan_rows_sql(spec))
                deleted += int(cursor.rowcount or 0)
                connection.commit()
            self._heartbeat(connection, owner)
            with connection.cursor() as cursor:
                try:
                    cursor.execute(validate_foreign_key_sql(spec))
                except Exception as exc:
                    if attempt + 1 < attempts and oracle_error_code(exc) == ORA_ORPHAN_ROWS:
                        continue
                    raise
                connection.commit()
            break
        return deleted

    def _orphan_deletion_result(
        self,
        connection: Any,
        spec: ForeignKeySpec,
        *,
        operation: SystemSchemaOrphanOperation,
        deleted: int,
    ) -> dict[str, Any]:
        return {
            **self._status_on(connection),
            "operation": operation,
            "deleted_row_count": deleted,
            "foreign_key": spec.payload(orphan_rows=0),
        }

    def _execute_drop(self, connection: Any, statement: str) -> int:
        """manifest にある object を 1 つ削除する（既に無いときは 0）。"""

        with connection.cursor() as cursor:
            try:
                cursor.execute(statement)
            except Exception as exc:
                if oracle_error_code(exc) in self.ignored_drop_codes:
                    return 0
                raise
            connection.commit()
        return 1


__all__ = [
    "CONTROL_KEY",
    "DEFAULT_LEASE_SECONDS",
    "DEPLOYMENT_CHECK_NAME_LIMIT",
    "DESTRUCTIVE_MIGRATIONS_CONFIRMATION_REQUIRED",
    "FOREIGN_KEY_DELETE_RULES",
    "FOREIGN_KEY_NOT_FOUND",
    "FOREIGN_KEY_OUTDATED",
    "IGNORED_APPLY_CODES",
    "IGNORED_DROP_CODES",
    "LOCK_RETRY_AFTER_SECONDS",
    "MAX_DDL_LOCK_TIMEOUT_SECONDS",
    "MIN_LEASE_SECONDS",
    "ORA_CHILD_RECORD_FOUND",
    "ORA_CONSTRAINT_NOT_FOUND",
    "ORA_FOREIGN_KEY_EXISTS",
    "ORA_ORPHAN_ROWS",
    "ORA_RESOURCE_BUSY",
    "ORPHAN_DELETION_OPERATION",
    "ORPHAN_ROWS_CHANGED",
    "RECREATE_CONFIRMATION_REQUIRED",
    "SCHEMA_OPERATION_FAILED",
    "SCHEMA_POSTCONDITION_FAILED",
    "SCHEMA_STATUS_UNAVAILABLE",
    "ConnectionFactory",
    "ExistingForeignKey",
    "ForeignKeyDrift",
    "ForeignKeyMismatch",
    "ForeignKeySpec",
    "SystemSchemaActiveJobsError",
    "SystemSchemaBusyError",
    "SystemSchemaError",
    "SystemSchemaManagerBase",
    "SystemSchemaOperation",
    "SystemSchemaOperationKind",
    "SystemSchemaOperationStatus",
    "SystemSchemaOrphanOperation",
    "SystemSchemaPreconditionError",
    "SystemSchemaStatus",
    "SystemTableColumnData",
    "SystemTableDestructiveMigrationData",
    "SystemTableForeignKeyData",
    "SystemTableOperationState",
    "SystemTablesDeleteOrphansRequest",
    "SystemTablesInitializeRequest",
    "add_foreign_key_sql",
    "bind_list",
    "clamp_ddl_lock_timeout",
    "classify_system_schema_status",
    "columns_from_create_table",
    "columns_from_statements",
    "connect_failure_diagnosis",
    "count_orphan_rows",
    "delete_orphan_rows_sql",
    "deployment_check_error_summary",
    "deployment_check_summary",
    "drop_foreign_key_sql",
    "enable_foreign_key_sql",
    "foreign_keys_from_create_table",
    "idle_operation_state",
    "inspect_foreign_keys",
    "iso_timestamp",
    "load_existing_columns",
    "load_existing_tables",
    "load_foreign_keys",
    "missing_columns",
    "operation_failure_fields",
    "operation_failure_hint",
    "oracle_error_code",
    "orphan_rows_sql",
    "require_destructive_migration_confirmation",
    "require_recreate_confirmation",
    "system_tables_status_error",
    "validate_foreign_key_sql",
]
