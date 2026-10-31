"""DB の状態 API（`GET /ready/database`。3製品共通。#325）。

画面の DB ゲートが「設定画面以外を開く前」に参照する。常に HTTP 200 で返し、
`status` で次の 4 つを区別する。

- `ok`：設定済みで接続でき、製品の準備（システムテーブル等）もできている
- `not_configured`：接続設定が不足している（まず設定が必要）
- `unreachable`：設定済みだが、DB が起動していない・到達できない
- `setup_required`：接続できるが、製品のシステムテーブルの作成・更新が必要

判定の順（固定）:

1. `short_circuit(settings)`：DB を使わない構成（NL2SQL の memory モード）なら、その応答を返す
2. `database_readiness(settings, extra_readiness)`：システム設定画面と同じ判定。`ok` 以外は
   `not_configured`（接続は試さない）
3. `test_connection(settings)`：bounded な接続確認。失敗は `unreachable`
4. `schema_probe(settings)`：製品の準備状態の確認。`ok` 以外はその結果を返す。例外は、接続・pool の
   エラー（`pr_backend_core.oracle_errors.is_oracle_connection_error`）なら `unreachable`、それ以外
   （辞書・DDL・権限など）だけを `setup_required` にする（#820。接続の失敗を「初期化が必要」と
   案内しない）
5. `ok`

`unreachable` のときは、ADB OCID が設定されていれば ADB のライフサイクル状態（`STOPPED` /
`STARTING` など）を上限付きで取得して `adb_lifecycle_state` に入れる（#820。画面が「停止しています」
「起動中です」を出し分ける）。取得できなくても状態の判定は変えない。

`ok` の結果だけを、接続先と資格情報の指紋ごとに短時間（既定 30 秒）サーバー側で cache する（#793）。
画面の cache（15 秒）は全画面の再読み込みで消えるため、そのたびに接続確認とシステムテーブルの確認を
やり直さないためのもの。1・2 は毎回行い（設定の不足はすぐ出す）、`unreachable` / `setup_required` は
cache しない（直したらすぐ通す）。システムテーブルの操作と DB 設定の保存で cache を捨てる
（`clear_database_status_cache`）。DB が止まったことに気づくのは最大で cache の時間だけ遅れる。

ログイン不要の path なので、`detail` には接続先・資格情報・Wallet の path を含めない
（ORA / DPY / DPI のコードだけ。#320）。共有パッケージは oracledb に依存しないため、
接続確認と準備状態の確認は製品から注入する。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from pr_backend_core.oracle_errors import is_oracle_connection_error, oracle_error_codes
from pr_backend_core.schemas import ApiResponse
from pydantic import BaseModel

from .database import ExtraReadiness, TestConnection, database_readiness
from .oci_database import OciDatabaseClient

logger = logging.getLogger(__name__)

DatabaseAvailability = Literal["ok", "not_configured", "unreachable", "setup_required"]
DatabaseSchemaProbeStatus = Literal["ok", "setup_required", "unreachable"]

READINESS_OK = "ok"
# schema_probe が例外を送出したときの check。
SCHEMA_CHECK_FAILED = "schema_check_failed"


class DatabaseStatusData(BaseModel):
    """データベース（Oracle AI Database）の利用可否（`GET /ready/database` の応答）。"""

    status: DatabaseAvailability
    # 設定の判定（`database_readiness`）の値。製品の準備状態の確認が上書きすることがある
    # （NL2SQL の `migration_required` 等）。
    check: str
    # 公開してよい補足（ORA コード等）。接続先・資格情報は含めない。
    detail: str | None = None
    # 接続先が変わったことを画面が知るための識別子（接続先の値の hash。生値は返さない）。
    context_id: str = ""
    # 製品のシステムテーブルの状態（RAG の `missing` / `partial` / `outdated` / `ready`）。
    schema_status: str | None = None
    # ADB のライフサイクル状態（`unreachable` のときだけ。ADB OCID が未設定・取得できなければ None。
    # #820）。
    adb_lifecycle_state: str | None = None


@dataclass(frozen=True)
class DatabaseSchemaProbeResult:
    """製品の準備状態の確認（`schema_probe`）の結果。"""

    status: DatabaseSchemaProbeStatus = "ok"
    # 指定すると応答の `check` を上書きする（例: `migration_required`）。
    check: str | None = None
    detail: str | None = None
    schema_status: str | None = None


SchemaProbe = Callable[[Any], Awaitable[DatabaseSchemaProbeResult]]
AdbLifecycleProbe = Callable[[Any], Awaitable[str | None]]
ShortCircuit = Callable[[Any], DatabaseStatusData | None]
ContextFields = Callable[[Any], Sequence[object]]


def default_context_fields(settings: Any) -> list[object]:
    """接続先を表す設定（DSN・ユーザー・Wallet の配置先）。"""
    return [
        str(getattr(settings, name, "") or "")
        for name in ("oracle_dsn", "oracle_user", "resolved_oracle_wallet_dir")
    ]


def database_context_id(fields: Sequence[object]) -> str:
    """接続先の値から、生値を推測できない識別子（SHA-256）を作る。"""
    payload = json.dumps(list(fields), ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# `ok` の結果を cache する秒数（0 以下なら cache しない）。
DEFAULT_OK_CACHE_SECONDS = 30.0

# cache の key に含める接続の設定（生値は保持せず、hash だけを key にする）。
_CONNECTION_FINGERPRINT_FIELDS = (
    "oracle_user",
    "oracle_dsn",
    "oracle_password",
    "resolved_oracle_wallet_dir",
    "oracle_wallet_password",
    "oracle_connection_security",
    "oracle_driver_mode",
    "oracle_client_lib_dir",
)


def database_status_cache_key(settings: Any, context_id: str) -> str:
    """接続先と資格情報が同じときだけ一致する key（資格情報は hash にしか使わない）。"""
    fields = [context_id]
    for name in _CONNECTION_FINGERPRINT_FIELDS:
        try:
            value = getattr(settings, name, "")
        except Exception:  # noqa: BLE001 - property の失敗は空として扱う
            value = ""
        fields.append(str(value or ""))
    return database_context_id(fields)


class DatabaseStatusCache:
    """`ok` の DB の状態を、key ごとに `ttl_seconds` だけ覚える（スレッド安全）。"""

    def __init__(
        self,
        ttl_seconds: float = DEFAULT_OK_CACHE_SECONDS,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, DatabaseStatusData]] = {}
        _CACHES.add(self)

    @property
    def enabled(self) -> bool:
        return self.ttl_seconds > 0

    def get(self, key: str) -> DatabaseStatusData | None:
        if not self.enabled:
            return None
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, data = entry
            if expires_at <= now:
                self._entries.pop(key, None)
                return None
            return data.model_copy()

    def put(self, key: str, data: DatabaseStatusData) -> None:
        if not self.enabled or data.status != "ok":
            return
        with self._lock:
            # 接続先が変わったら古い key は使わないため、最新の 1 件だけを持つ。
            self._entries = {key: (self._clock() + self.ttl_seconds, data.model_copy())}

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_CACHES: weakref.WeakSet[DatabaseStatusCache] = weakref.WeakSet()


def clear_database_status_cache() -> None:
    """DB の状態の cache をすべて捨てる（システムテーブルの操作・DB 設定の保存の後。テスト）。"""
    for cache in list(_CACHES):
        cache.clear()


# ADB の状態の取得（OCI の API）を待つ上限（秒）。DB ゲートの応答を長く止めない。
ADB_LIFECYCLE_TIMEOUT_SECONDS = 5.0


async def adb_lifecycle_state(settings: Any) -> str | None:
    """ADB のライフサイクル状態（`STOPPED` 等）。ADB OCID が未設定・取得できなければ None。"""
    adb_ocid = str(getattr(settings, "oracle_adb_ocid", "") or "").strip()
    if not adb_ocid:
        return None
    try:
        info = await asyncio.wait_for(
            OciDatabaseClient(settings=settings).get_autonomous_database(adb_ocid),
            timeout=ADB_LIFECYCLE_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 - 補足の情報なので、取れなくても判定は変えない
        logger.info(
            "database_status_adb_lifecycle_unavailable",
            extra={"exception_type": type(exc).__name__},
        )
        return None
    state = str(info.lifecycle_state or "").strip().upper()
    return state or None


def _is_timeout(exc: BaseException) -> bool:
    # 製品の OracleConnectionTimeoutError は TimeoutError 派生とは限らない（Agent）。
    return isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower()


def safe_oracle_error_code(exc: BaseException) -> str | None:
    """例外（原因の連鎖を含む）から、公開してよいエラーコードを 1 つ返す（ORA を優先）。"""
    codes = oracle_error_codes(exc)
    for code in codes:
        if code.startswith("ORA-"):
            return code
    return codes[0] if codes else None


def safe_connection_error_detail(exc: BaseException) -> str:
    """接続確認の失敗を、接続先・資格情報を含まない文字列にする。"""
    if _is_timeout(exc):
        return "Oracle connection probe timed out."
    code = safe_oracle_error_code(exc)
    if code:
        return f"Oracle connection probe failed ({code})."
    return "Oracle connection probe failed."


def safe_schema_error_detail(exc: BaseException) -> str:
    """準備状態の確認の失敗を、接続先を含まない文字列にする。"""
    code = safe_oracle_error_code(exc)
    if code:
        return f"システムテーブルの状態を確認できませんでした ({code})。"
    return "システムテーブルの状態を確認できませんでした。"


async def database_status(
    settings: Any,
    *,
    test_connection: TestConnection,
    extra_readiness: ExtraReadiness | None = None,
    schema_probe: SchemaProbe | None = None,
    short_circuit: ShortCircuit | None = None,
    context_fields: ContextFields | None = None,
    connection_failure_log_extra: Callable[[Exception], Mapping[str, Any]] | None = None,
    cache: DatabaseStatusCache | None = None,
    adb_lifecycle: AdbLifecycleProbe | None = None,
) -> DatabaseStatusData:
    """DB の状態を判定する（判定の順はモジュールの docstring を参照）。"""
    context_id = database_context_id((context_fields or default_context_fields)(settings))

    if short_circuit is not None:
        shortcut = short_circuit(settings)
        if shortcut is not None:
            if not shortcut.context_id:
                shortcut = shortcut.model_copy(update={"context_id": context_id})
            return shortcut

    # Wallet のファイル読込と暗号化 PEM の復号確認を含むため、event loop を塞がない。
    check = await run_in_threadpool(database_readiness, settings, extra_readiness)
    if check != READINESS_OK:
        # 設定が不足している：接続を試さない。
        return DatabaseStatusData(status="not_configured", check=check, context_id=context_id)

    cache_key = database_status_cache_key(settings, context_id) if cache is not None else ""
    if cache is not None and (cached := cache.get(cache_key)) is not None:
        return cached

    result = await _probe_database(
        settings,
        check=check,
        context_id=context_id,
        test_connection=test_connection,
        schema_probe=schema_probe,
        connection_failure_log_extra=connection_failure_log_extra,
    )
    if result.status == "unreachable" and adb_lifecycle is not None:
        result = result.model_copy(update={"adb_lifecycle_state": await adb_lifecycle(settings)})
    if cache is not None:
        cache.put(cache_key, result)
    return result


async def _probe_database(
    settings: Any,
    *,
    check: str,
    context_id: str,
    test_connection: TestConnection,
    schema_probe: SchemaProbe | None,
    connection_failure_log_extra: Callable[[Exception], Mapping[str, Any]] | None,
) -> DatabaseStatusData:
    """接続確認と製品の準備状態の確認（判定の 3〜5）。"""
    # 接続確認の timeout は製品の test_connection が持つ（Oracle 用の timeout）。ここで
    # 閲覧 API 用の短い timeout をかけると、起動済み ADB の初回接続を unreachable と誤判定する。
    try:
        await test_connection(settings)
    except Exception as exc:  # noqa: BLE001 - DB の不通を status へ正規化する境界
        log_extra = dict(connection_failure_log_extra(exc)) if connection_failure_log_extra else {}
        log_extra.setdefault("exception_type", type(exc).__name__)
        logger.warning("database_status_unreachable", extra=log_extra, exc_info=True)
        return DatabaseStatusData(
            status="unreachable",
            check=check,
            detail=safe_connection_error_detail(exc),
            context_id=context_id,
        )

    if schema_probe is not None:
        try:
            probe = await schema_probe(settings)
        except Exception as exc:  # noqa: BLE001 - 準備の案内へ正規化する境界
            if is_oracle_connection_error(exc):
                # 接続確認の後に接続が切れた・pool が接続を返さない（#820）。初期化の不足ではない。
                logger.warning(
                    "database_schema_probe_unreachable",
                    extra={
                        "error_code": safe_oracle_error_code(exc),
                        "exception_type": type(exc).__name__,
                    },
                )
                return DatabaseStatusData(
                    status="unreachable",
                    check=check,
                    detail=safe_connection_error_detail(exc),
                    context_id=context_id,
                )
            logger.warning(
                "database_schema_status_unavailable",
                extra={
                    "error_code": safe_oracle_error_code(exc),
                    "exception_type": type(exc).__name__,
                },
            )
            return DatabaseStatusData(
                status="setup_required",
                check=SCHEMA_CHECK_FAILED,
                detail=safe_schema_error_detail(exc),
                context_id=context_id,
            )
        return DatabaseStatusData(
            status=probe.status,
            check=probe.check or check,
            detail=probe.detail,
            context_id=context_id,
            schema_status=probe.schema_status,
        )

    return DatabaseStatusData(status="ok", check=check, context_id=context_id)


def build_database_status_router(
    *,
    get_settings: Callable[[], Any],
    test_connection: TestConnection,
    extra_readiness: ExtraReadiness | None = None,
    schema_probe: SchemaProbe | None = None,
    short_circuit: ShortCircuit | None = None,
    context_fields: ContextFields | None = None,
    connection_failure_log_extra: Callable[[Exception], Mapping[str, Any]] | None = None,
    ok_cache_seconds: float = DEFAULT_OK_CACHE_SECONDS,
    adb_lifecycle: AdbLifecycleProbe | None = adb_lifecycle_state,
) -> APIRouter:
    """`GET /ready/database` の router を作る。製品側で `/api` 配下に include する。

    - `test_connection(settings)`：bounded な接続確認。失敗なら例外を送出する
      （接続 pool は製品が持つ）
    - `extra_readiness(settings)`：製品固有の設定の判定（NL2SQL の DeepSec）。システム設定画面の
      `build_database_router` と同じものを渡す
    - `schema_probe(settings)`：製品の準備状態の確認
      （RAG の system schema、NL2SQL の incremental store）
    - `short_circuit(settings)`：DB を使わない構成なら応答を返す（NL2SQL の memory モード）
    - `context_fields(settings)`：`context_id` の元にする接続先の値（既定は DSN・ユーザー・Wallet）
    - `connection_failure_log_extra(exc)`：接続確認の失敗をログに出すときの追加項目
    - `ok_cache_seconds`：`ok` の結果を cache する秒数（0 で cache しない。#793）
    - `adb_lifecycle(settings)`：`unreachable` のときの ADB のライフサイクル状態（既定は OCI の
      API。None で取得しない。#820）

    ログイン不要の path にするため、製品の公開 path の一覧に `/ready/database` を入れる。
    """
    router = APIRouter()
    cache = DatabaseStatusCache(ok_cache_seconds)

    @router.get("/ready/database", response_model=ApiResponse[DatabaseStatusData])
    async def get_database_status() -> ApiResponse[DatabaseStatusData]:
        """DB ゲートが使う設定の判定と bounded な接続確認。常に HTTP 200。"""
        return ApiResponse(
            data=await database_status(
                get_settings(),
                test_connection=test_connection,
                extra_readiness=extra_readiness,
                schema_probe=schema_probe,
                short_circuit=short_circuit,
                context_fields=context_fields,
                connection_failure_log_extra=connection_failure_log_extra,
                cache=cache,
                adb_lifecycle=adb_lifecycle,
            )
        )

    return router
