"""データベース設定の API（3製品共通。NL2SQL の実装を基準に移設。#108）。

- Oracle AI Database の接続設定（`.env` へ保存。secret はレスポンスに返さない）
- readiness（ユーザー・パスワード・DSN の形式）と接続テストの結果

実際の DB 接続（python-oracledb の使い方）と保存後の後始末（接続 pool を閉じる等）は
製品ごとに違うため、`build_database_router` の hook で受け取る。共有パッケージは
oracledb に依存しない。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Response, params
from pr_backend_core.schemas import ApiResponse
from pydantic import BaseModel, Field, field_validator

from .embedding import EMBEDDING_DIMENSIONS, EMBEDDING_VECTOR_COLUMN_TYPE
from .env_file import write_env_values

logger = logging.getLogger(__name__)

ORACLE_ERROR_CODE_RE = re.compile(r"\b(?:ORA|DPY|DPI)-\d{4,5}\b", re.IGNORECASE)
SECRET_REVEAL_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}

DatabaseConnectionTestStatus = Literal["success", "failed"]


# --------------------------------------------------------------------------- schemas


class DatabaseSettingsData(BaseModel):
    """Oracle AI Database の接続設定の表示用データ。"""

    user: str
    dsn: str
    driver_mode: Literal["thin", "thick"]
    client_lib_dir: str
    has_password: bool
    readiness: str
    embedding_dimension: int
    vector_column: str
    config_source: Literal["runtime"]


class DatabasePasswordRevealData(BaseModel):
    """明示操作でのみ返す Oracle DB password。通常の設定取得には含めない。"""

    password: str = Field(default="", max_length=4096)


class DatabaseSettingsUpdate(BaseModel):
    """Oracle AI Database の接続設定の更新 payload。

    password は未指定または空文字なら既存値を保持する。
    clear_password が true の場合だけ保存済み secret を削除する。
    """

    user: str = Field(default="", max_length=256)
    dsn: str = Field(default="", max_length=1024)
    password: str | None = Field(default=None, max_length=4096)
    clear_password: bool = False

    @field_validator("user", "dsn")
    @classmethod
    def strip_text(cls, value: str) -> str:
        """前後空白を設定値へ混入させない。"""
        return value.strip()


class DatabaseConnectionTestResult(BaseModel):
    """Oracle AI Database の接続テスト結果。"""

    status: DatabaseConnectionTestStatus
    readiness: str
    message: str
    elapsed_ms: int
    troubleshooting: list[str] = Field(default_factory=list)
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    error_type: str | None = None


# --------------------------------------------------------------------------- Settings access


def _s(settings: Any, name: str) -> str:
    """Settings の文字列属性（Agent は None を持つ）を空文字へそろえる。"""
    return str(getattr(settings, name, "") or "")


def driver_mode(settings: Any) -> str:
    """python-oracledb の driver mode（`oracle_driver_mode`。既定 thin）。"""
    return _s(settings, "oracle_driver_mode").strip().lower() or "thin"


def _has_field(settings: Any, name: str) -> bool:
    return name in getattr(type(settings), "model_fields", {})


def secret_value(*, current: str, update: str | None, clear: bool) -> str:
    """secret の保持・更新・削除を判定する。"""
    if clear:
        return ""
    if update:
        return update
    return current


# --------------------------------------------------------------------------- data / readiness

ExtraReadiness = Callable[[Any], str | None]


def database_settings_data(
    settings: Any, extra_readiness: ExtraReadiness | None = None
) -> DatabaseSettingsData:
    return DatabaseSettingsData(
        user=_s(settings, "oracle_user"),
        dsn=_s(settings, "oracle_dsn"),
        driver_mode="thick" if driver_mode(settings) == "thick" else "thin",
        client_lib_dir=_s(settings, "oracle_client_lib_dir"),
        has_password=bool(_s(settings, "oracle_password")),
        readiness=database_readiness(settings, extra_readiness),
        # ベクトル列の型はコードの正本（#46）。設定の値ではなく DDL と同じ値を出す。
        embedding_dimension=EMBEDDING_DIMENSIONS,
        vector_column=EMBEDDING_VECTOR_COLUMN_TYPE,
        config_source="runtime",
    )


def database_readiness(settings: Any, extra_readiness: ExtraReadiness | None = None) -> str:
    """接続テスト前に分かる設定の不足を返す（`ok` なら接続を試す）。"""
    if extra_readiness is not None:
        extra = extra_readiness(settings)
        if extra:
            return extra
    if not _s(settings, "oracle_user").strip() or not _s(settings, "oracle_dsn").strip():
        return "missing"
    if not _s(settings, "oracle_password").strip():
        return "missing_credentials"
    # 記号の無い文字列は接続先を解決できないので、host:port/service か接続記述子だけを受け付ける。
    if re.fullmatch(r"[A-Za-z0-9_.-]+", _s(settings, "oracle_dsn").strip()):
        return "invalid"
    return "ok"


def database_readiness_message(readiness: str) -> str:
    if readiness == "invalid_configuration":
        return (
            "接続設定の組み合わせがこの製品で使えません。"
            "PLATFORM_ORACLE_DRIVER_MODE の設定を確認してください。"
        )
    if readiness == "invalid":
        return (
            "Oracle AI Database の接続設定を確認してください。"
            "DSN は host:port/service_name か接続記述子で指定してください。"
        )
    if readiness == "missing_credentials":
        return "Oracle AI Database への接続に必要な DB パスワードを設定してください。"
    return "Oracle AI Database への接続に必要な設定が不足しています。"


# --------------------------------------------------------------------------- connection test


def oracle_error_codes(error_text: str) -> list[str]:
    """Oracle / python-oracledb の公開してよいエラーコードだけを抽出する。"""
    return list(dict.fromkeys(match.upper() for match in ORACLE_ERROR_CODE_RE.findall(error_text)))


def database_connection_error_message(exc: Exception, error_codes: list[str]) -> str:
    """secret を含めず、Oracle 接続エラーの原因カテゴリをユーザーへ返す。"""
    if getattr(exc, "safe_for_user", False):
        return str(exc)

    code_label = f"（{', '.join(error_codes)}）" if error_codes else ""
    code_set = set(error_codes)
    prefix = f"Oracle AI Database へ接続できませんでした{code_label}。"
    if "ORA-01017" in code_set:
        return prefix + "ユーザー名または DB パスワードを確認してください。"
    if "ORA-12154" in code_set or "DPY-4000" in code_set:
        return prefix + "DSN が host:port/service_name か接続記述子か確認してください。"
    if "ORA-12506" in code_set:
        return (
            prefix + "ADB のアクセス制御リストまたは network ACL が"
            "この接続元を許可しているか確認してください。"
        )
    if code_set & {"ORA-12514", "ORA-12505"}:
        return prefix + "DSN のサービス名とデータベースの稼働状態を確認してください。"
    if code_set & {"ORA-12541", "DPY-6005", "DPY-6000"}:
        return (
            prefix + "データベースの稼働状態と、DSN の host と port への到達性を確認してください。"
        )
    if "DPY-4011" in code_set:
        return prefix + "DSN の port とプロトコルが接続先に合っているか確認してください。"
    if code_set & {"DPI-1047", "DPI-1072"}:
        return (
            prefix + "標準の Thin mode では Oracle Instant Client は不要です。"
            "Thick 互換設定を使う場合のみ、"
            "Oracle Instant Client の配置と PLATFORM_ORACLE_CLIENT_LIB_DIR を確認してください。"
        )
    if error_codes:
        return prefix + "下の確認ポイントと backend ログを確認してください。"
    return (
        "Oracle AI Database へ接続できませんでした。"
        "下の確認ポイントと backend ログの Oracle エラーコードを確認してください。"
    )


def database_connection_troubleshooting(
    *,
    readiness: str,
    error_text: str = "",
    error_type: str = "",
) -> list[str]:
    """Oracle 接続テストの結果から次に確認するポイントを返す。"""
    tips: list[str] = []
    if readiness == "missing":
        tips.append("ユーザー名と DSN が入力済みか確認してください。")
    if readiness == "missing_credentials":
        tips.append("DB パスワードが保存済みか確認してください。")
    if readiness == "invalid_configuration":
        tips.append("PLATFORM_ORACLE_DRIVER_MODE=thin を指定してください。")
    if readiness == "invalid":
        tips.append("DSN を host:port/service_name か接続記述子で指定してください。")

    combined = f"{error_text} {error_type}".lower()
    if any(token in combined for token in ("timeout", "timed out", "oracleconnectiontimeouterror")):
        tips.append(
            "接続テストがタイムアウトしました。データベースが起動中か、"
            "DSN の host と port に到達できるか確認してください。"
        )
    if "ora-01017" in combined:
        tips.append("ユーザー名または DB パスワードが正しいか確認してください。")
    if "ora-12154" in combined or "dpy-4000" in combined or "tns" in combined:
        tips.append("DSN が host:port/service_name か接続記述子か確認してください。")
    if "ora-12506" in combined:
        tips.append(
            "ADB の Network Access / ACL で、この実行ホストの public IP または VCN 経路を"
            "許可してください。"
        )
    if "ora-12514" in combined or "ora-12505" in combined:
        tips.append("DSN のサービス名が接続先で有効か確認してください。")
    if "ora-12541" in combined or "dpy-6005" in combined or "dpy-6000" in combined:
        tips.append(
            "データベースが停止していないか、DSN の host と port に到達できるか確認してください。"
        )
    if "dpy-4011" in combined:
        tips.append("DSN の port とプロトコルが接続先に合っているか確認してください。")
    if "dpi-1047" in combined or "dpi-1072" in combined:
        tips.append(
            "PLATFORM_ORACLE_DRIVER_MODE=thin の標準構成へ戻してください。"
            "Thick 互換設定を使う場合のみ、"
            "Oracle Instant Client と PLATFORM_ORACLE_CLIENT_LIB_DIR を確認してください。"
        )
    if "operationalerror" in combined and not tips:
        tips.append(
            "backend ログに出ている ORA/DPY/DPI エラーコードを確認し、"
            "DSN、認証情報、ネットワーク経路を切り分けてください。"
        )

    if not tips:
        tips.append(
            "バックエンドログの Oracle エラーコードと DSN・ネットワークの設定を確認してください。"
        )
    return list(dict.fromkeys(tips))


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def _timeout_details(settings: Any) -> dict[str, str | int | float | bool | None]:
    details: dict[str, str | int | float | bool | None] = {}
    for key, attr in (
        ("timeout_seconds", "oracle_db_test_timeout_seconds"),
        ("tcp_connect_timeout_seconds", "oracle_tcp_connect_timeout_seconds"),
    ):
        value = getattr(settings, attr, None)
        if isinstance(value, int | float):
            details[key] = value
    return details


# --------------------------------------------------------------------------- persistence


def database_settings_candidate(base: Any, payload: DatabaseSettingsUpdate) -> Any:
    return base.model_copy(
        update={
            "oracle_user": payload.user.strip(),
            "oracle_dsn": payload.dsn.strip(),
            "oracle_password": secret_value(
                current=_s(base, "oracle_password"),
                update=payload.password,
                clear=payload.clear_password,
            ),
        }
    )


_DATABASE_FIELDS = (
    "oracle_user",
    "oracle_password",
    "oracle_dsn",
    "oracle_client_lib_dir",
)


def _apply_database_settings(target: Any, source: Any) -> None:
    for name in _DATABASE_FIELDS:
        if _has_field(target, name):
            setattr(target, name, getattr(source, name))


def _persist_database_settings(settings: Any, env_file: Path) -> None:
    values: dict[str, str | None] = {
        "PLATFORM_ORACLE_USER": _s(settings, "oracle_user"),
        "PLATFORM_ORACLE_PASSWORD": _s(settings, "oracle_password"),
        "PLATFORM_ORACLE_DSN": _s(settings, "oracle_dsn"),
        "PLATFORM_ORACLE_CLIENT_LIB_DIR": _s(settings, "oracle_client_lib_dir"),
    }
    # driver mode を設定項目として持つ製品（NL2SQL）だけ保存する。
    if _has_field(settings, "oracle_driver_mode"):
        values["PLATFORM_ORACLE_DRIVER_MODE"] = driver_mode(settings)
    _write_env(
        env_file,
        values,
        section_comment="# Oracle AI Database",
        error_detail="Oracle AI Database の接続設定を platform/.env へ保存できませんでした。",
    )


def _write_env(
    env_file: Path,
    values: Mapping[str, str | None],
    *,
    section_comment: str,
    error_detail: str,
) -> None:
    try:
        write_env_values(env_file, values, section_comment=section_comment)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=error_detail) from exc


# --------------------------------------------------------------------------- router

TestConnection = Callable[[Any], Awaitable[None]]


def build_database_router(
    *,
    get_settings: Callable[[], Any],
    env_file: Callable[[], Path],
    test_connection: TestConnection,
    on_saved: Callable[[Any], None] = lambda _settings: None,
    extra_readiness: ExtraReadiness | None = None,
    connection_failure_log_extra: Callable[[Exception], Mapping[str, Any]] | None = None,
    password_reveal_enabled: bool = False,
    write_dependencies: Sequence[params.Depends] = (),
    action_dependencies: Sequence[params.Depends] = (),
) -> APIRouter:
    """`/database*` の router を作る。製品側で `/settings` 配下に include する。

    - `test_connection(candidate)`：保存前の値で Oracle に接続し、失敗なら例外を送出する
    - `on_saved(settings)`：接続設定を変えたあとに呼ぶ（接続 pool を閉じる等）
    - `extra_readiness(settings)`：製品固有の readiness。問題がなければ None
    - `password_reveal_enabled`：保存済み DB パスワードの表示（`write_dependencies` を付ける）
    - `write_dependencies`：設定を書き換える操作（保存）
    - `action_dependencies`：接続テスト
    """
    router = APIRouter()
    write = list(write_dependencies)
    action = list(action_dependencies)

    def saved(settings: Any) -> None:
        """製品の `on_saved` の前に、DB の状態の cache も捨てる（#793）。"""
        # database_status は本 module を import するため、循環を避けて呼び出し時に読む。
        from .database_status import clear_database_status_cache

        clear_database_status_cache()
        on_saved(settings)

    def data(settings: Any) -> DatabaseSettingsData:
        return database_settings_data(settings, extra_readiness)

    @router.get("/database", response_model=ApiResponse[DatabaseSettingsData])
    def get_database_settings() -> ApiResponse[DatabaseSettingsData]:
        return ApiResponse(data=data(get_settings()))

    @router.patch("/database", response_model=ApiResponse[DatabaseSettingsData], dependencies=write)
    def update_database_settings(
        payload: DatabaseSettingsUpdate,
    ) -> ApiResponse[DatabaseSettingsData]:
        settings = get_settings()
        # 保存に成功してから runtime へ反映する。
        candidate = database_settings_candidate(settings, payload)
        _persist_database_settings(candidate, env_file())
        _apply_database_settings(settings, candidate)
        saved(settings)
        return ApiResponse(data=data(settings))

    if password_reveal_enabled:

        @router.post(
            "/database/password/reveal",
            response_model=ApiResponse[DatabasePasswordRevealData],
            dependencies=write,
        )
        def reveal_database_password(
            response: Response,
        ) -> ApiResponse[DatabasePasswordRevealData]:
            """保存済み DB password を明示操作時だけ返す。"""
            response.headers.update(SECRET_REVEAL_HEADERS)
            password = _s(get_settings(), "oracle_password")
            if not password:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "保存済み DB パスワードがありません。パスワードを入力して保存してください。"
                    ),
                    headers=SECRET_REVEAL_HEADERS,
                )
            return ApiResponse(data=DatabasePasswordRevealData(password=password))

    @router.post(
        "/database/test",
        response_model=ApiResponse[DatabaseConnectionTestResult],
        dependencies=action,
    )
    async def test_database_settings(
        payload: DatabaseSettingsUpdate | None = None,
    ) -> ApiResponse[DatabaseConnectionTestResult]:
        started = time.perf_counter()
        base = get_settings()
        candidate = database_settings_candidate(base, payload) if payload is not None else base
        readiness = database_readiness(candidate, extra_readiness)
        if readiness != "ok":
            return ApiResponse(
                data=DatabaseConnectionTestResult(
                    status="failed",
                    readiness=readiness,
                    message=database_readiness_message(readiness),
                    elapsed_ms=_elapsed_ms(started),
                    troubleshooting=database_connection_troubleshooting(readiness=readiness),
                )
            )

        try:
            await test_connection(candidate)
        except Exception as exc:  # noqa: BLE001 - 接続エラーは結果として返す
            log_extra = (
                dict(connection_failure_log_extra(exc)) if connection_failure_log_extra else {}
            )
            log_extra.setdefault("exception_type", type(exc).__name__)
            logger.error("database_connection_test_failed", extra=log_extra)
            codes = oracle_error_codes(str(exc))
            return ApiResponse(
                data=DatabaseConnectionTestResult(
                    status="failed",
                    readiness=readiness,
                    message=database_connection_error_message(exc, codes),
                    elapsed_ms=_elapsed_ms(started),
                    troubleshooting=database_connection_troubleshooting(
                        readiness=readiness,
                        error_text=str(exc),
                        error_type=type(exc).__name__,
                    ),
                    details={
                        **_timeout_details(candidate),
                        "oracle_error_codes": ", ".join(codes) or None,
                    },
                    error_type=type(exc).__name__,
                )
            )

        return ApiResponse(
            data=DatabaseConnectionTestResult(
                status="success",
                readiness=readiness,
                message="Oracle AI Database への接続に成功しました。",
                elapsed_ms=_elapsed_ms(started),
                details=_timeout_details(candidate),
            )
        )

    return router
