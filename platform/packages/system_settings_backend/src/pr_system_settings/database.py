"""データベース設定の API（3製品共通。NL2SQL の実装を基準に移設。#108）。

- Oracle AI Database の接続設定（`.env` へ保存。secret はレスポンスに返さない）
- Wallet ZIP の安全な展開と差し替え（権限 0700 / 0600、失敗時は元に戻す、同時実行は 409）
- readiness（Wallet のファイル・Wallet パスワード・DSN の別名）と接続テストの結果
- Autonomous Database の情報取得・起動・停止と、OCI からの Wallet 取得

実際の DB 接続（python-oracledb の使い方）と保存後の後始末（接続 pool を閉じる等）は
製品ごとに違うため、`build_database_router` の hook で受け取る。共有パッケージは
oracledb に依存しない。
"""

from __future__ import annotations

import fcntl
import io
import logging
import os
import re
import shutil
import stat
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from uuid import uuid4
from zipfile import BadZipFile, ZipFile

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import APIRouter, File, HTTPException, Response, UploadFile, params
from fastapi.concurrency import run_in_threadpool
from pr_backend_core.schemas import ApiResponse
from pydantic import BaseModel, Field, field_validator

from .env_file import write_env_values
from .oci import oci_config_file, oci_profile, read_runtime_oci_config
from .oci_auth import pem_file_is_encrypted, resolve_oci_key_file
from .oci_database import AutonomousDatabaseInfo, OciDatabaseClient, WalletDownloadTooLargeError

logger = logging.getLogger(__name__)

ORACLE_WALLET_MAX_BYTES = 20 * 1024 * 1024
ORACLE_WALLET_MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
ORACLE_WALLET_DIRECTORY_MODE = 0o700
ORACLE_WALLET_FILE_MODE = 0o600
ORACLE_WALLET_THIN_REQUIRED_FILES = frozenset({"tnsnames.ora", "ewallet.pem"})
ORACLE_WALLET_THICK_REQUIRED_FILES = frozenset({"tnsnames.ora", "sqlnet.ora", "cwallet.sso"})
ORACLE_WALLET_SKIPPED_FILES = frozenset(
    {"readme", "keystore.jks", "truststore.jks", "ojdbc.properties", "ewallet.p12"}
)
ORACLE_ERROR_CODE_RE = re.compile(r"\b(?:ORA|DPY|DPI)-\d{4,5}\b", re.IGNORECASE)
SECRET_REVEAL_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache"}

DatabaseConnectionTestStatus = Literal["success", "failed"]
DatabaseConnectionSecurity = Literal["wallet_mtls", "walletless_tls"]
DatabaseWalletDownloadStatus = Literal["downloaded", "already_configured"]
AdbOperationStatus = Literal[
    "success",
    "not_configured",
    "error",
    "accepted",
    "already_available",
    "already_stopped",
    "cannot_start",
    "cannot_stop",
]


# --------------------------------------------------------------------------- schemas


class DatabaseSettingsData(BaseModel):
    """Oracle AI Database の接続設定の表示用データ。"""

    user: str
    dsn: str
    driver_mode: Literal["thin", "thick"]
    connection_security: DatabaseConnectionSecurity
    client_lib_dir: str
    wallet_dir: str
    wallet_uploaded: bool
    available_services: list[str]
    has_password: bool
    has_wallet_password: bool
    readiness: str
    embedding_dimension: int
    vector_column: str
    adb_ocid: str
    region: str
    config_source: Literal["runtime"]


class DatabasePasswordRevealData(BaseModel):
    """明示操作でのみ返す Oracle DB password。通常の設定取得には含めない。"""

    password: str = Field(default="", max_length=4096)


class DatabaseWalletDownloadData(BaseModel):
    """OCI からの Wallet 取得結果。ZIP や生成 password は含めない。"""

    status: DatabaseWalletDownloadStatus
    settings: DatabaseSettingsData


class AdbSettingsUpdate(BaseModel):
    """Autonomous Database 操作対象の OCID と region の更新 payload。"""

    adb_ocid: str = Field(default="", max_length=512)
    region: str = Field(default="", max_length=128)

    @field_validator("adb_ocid", "region")
    @classmethod
    def strip_text(cls, value: str) -> str:
        """前後空白を設定値へ混入させない。"""
        return value.strip()


class AdbInfoData(BaseModel):
    """Autonomous Database の情報 / 操作結果の表示用データ。"""

    status: AdbOperationStatus
    message: str
    error_code: str | None = None
    id: str | None = None
    display_name: str | None = None
    lifecycle_state: str | None = None
    db_name: str | None = None
    cpu_core_count: int | None = None
    data_storage_size_in_tbs: float | None = None
    region: str | None = None


class DatabaseSettingsUpdate(BaseModel):
    """Oracle AI Database の接続設定の更新 payload。

    password / wallet_password は未指定または空文字なら既存値を保持する。
    clear_* が true の場合だけ保存済み secret を削除する。
    """

    user: str = Field(default="", max_length=256)
    dsn: str = Field(default="", max_length=1024)
    connection_security: DatabaseConnectionSecurity | None = None
    wallet_dir: str = Field(default="", max_length=1024)
    password: str | None = Field(default=None, max_length=4096)
    wallet_password: str | None = Field(default=None, max_length=4096)
    clear_password: bool = False
    clear_wallet_password: bool = False

    @field_validator("user", "dsn", "wallet_dir")
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


class DatabaseWalletOperationError(HTTPException):
    """Wallet の server-side install が失敗した段階を API 境界へ伝える。

    HTTPException（500）なので、製品が専用の handler を登録していなければ
    `public_message` をそのまま返す。
    """

    def __init__(
        self,
        *,
        code: str,
        public_message: str,
        stage: str,
        retryable: bool = True,
    ) -> None:
        super().__init__(status_code=500, detail=public_message)
        self.code = code
        self.public_message = public_message
        self.stage = stage
        self.retryable = retryable


# --------------------------------------------------------------------------- Settings access


def _s(settings: Any, name: str) -> str:
    """Settings の文字列属性（Agent は None を持つ）を空文字へそろえる。"""
    return str(getattr(settings, name, "") or "")


def driver_mode(settings: Any) -> str:
    """python-oracledb の driver mode（`oracle_driver_mode`。既定 thin）。"""
    return _s(settings, "oracle_driver_mode").strip().lower() or "thin"


def connection_security(settings: Any) -> str:
    """接続セキュリティ（`oracle_connection_security`。既定 wallet_mtls）。"""
    return _s(settings, "oracle_connection_security").strip().lower() or "wallet_mtls"


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
    wallet_dir = _s(settings, "resolved_oracle_wallet_dir")
    wallet_path = Path(wallet_dir).expanduser() if wallet_dir else None
    if wallet_path is not None:
        _sanitize_database_wallet_dir(wallet_path)
    wallet_configured = bool(
        wallet_path
        and _database_wallet_is_configured(wallet_path, driver_mode=driver_mode(settings))
    )
    embedding_dim = int(getattr(settings, "oci_genai_embedding_dim", 1536) or 1536)
    return DatabaseSettingsData(
        user=_s(settings, "oracle_user"),
        dsn=_s(settings, "oracle_dsn"),
        driver_mode="thick" if driver_mode(settings) == "thick" else "thin",
        connection_security=(
            "walletless_tls" if connection_security(settings) == "walletless_tls" else "wallet_mtls"
        ),
        client_lib_dir=_s(settings, "oracle_client_lib_dir"),
        wallet_dir=wallet_dir,
        wallet_uploaded=wallet_configured,
        available_services=(
            _extract_wallet_services(wallet_path) if wallet_path and wallet_configured else []
        ),
        has_password=bool(_s(settings, "oracle_password")),
        has_wallet_password=bool(_s(settings, "oracle_wallet_password")),
        readiness=database_readiness(settings, extra_readiness),
        embedding_dimension=embedding_dim,
        vector_column=f"VECTOR({embedding_dim}, FLOAT32)",
        adb_ocid=_s(settings, "oracle_adb_ocid"),
        region=_s(settings, "resolved_oracle_adb_region"),
        config_source="runtime",
    )


def _sanitize_database_wallet_dir(wallet_path: Path) -> None:
    if not wallet_path.is_dir():
        return
    for file_name in ORACLE_WALLET_SKIPPED_FILES:
        try:
            path = wallet_path / file_name
            if path.is_file():
                path.unlink()
        except OSError:
            continue


def database_wallet_required_files(mode: str) -> frozenset[str]:
    """driver mode に応じた mTLS Wallet の必須ファイルを返す。"""
    if mode.strip().lower() == "thin":
        return ORACLE_WALLET_THIN_REQUIRED_FILES
    return ORACLE_WALLET_THICK_REQUIRED_FILES


def _database_wallet_is_configured(wallet_path: Path, *, driver_mode: str = "thin") -> bool:
    """接続に必須の mTLS Wallet ファイルが通常ファイルとして揃っているか判定する。"""
    required_files = database_wallet_required_files(driver_mode)
    return wallet_path.is_dir() and all(
        (wallet_path / file_name).is_file() for file_name in required_files
    )


def _database_wallet_password_is_usable(
    wallet_path: Path,
    wallet_password: str,
    *,
    driver_mode: str = "thin",
) -> bool:
    """mTLS Wallet の認証ファイルが保存済み password で使えるか判定する。"""
    if driver_mode.strip().lower() != "thin":
        return (wallet_path / "cwallet.sso").is_file()
    pem_path = wallet_path / "ewallet.pem"
    if not pem_path.is_file():
        return False
    if not pem_file_is_encrypted(pem_path):
        return True
    if not wallet_password:
        return False
    try:
        load_pem_private_key(pem_path.read_bytes(), password=wallet_password.encode("utf-8"))
    except (OSError, TypeError, ValueError):
        return False
    return True


def database_readiness(settings: Any, extra_readiness: ExtraReadiness | None = None) -> str:
    """接続テスト前に分かる設定の不足を返す（`ok` なら接続を試す）。"""
    if extra_readiness is not None:
        extra = extra_readiness(settings)
        if extra:
            return extra
    if not _s(settings, "oracle_user").strip() or not _s(settings, "oracle_dsn").strip():
        return "missing"
    mode = driver_mode(settings)
    wallet_dir = _s(settings, "resolved_oracle_wallet_dir").strip()
    wallet_path = Path(wallet_dir).expanduser() if wallet_dir else None
    wallet_configured = bool(
        wallet_path and _database_wallet_is_configured(wallet_path, driver_mode=mode)
    )
    dsn = _s(settings, "oracle_dsn")
    if connection_security(settings) == "walletless_tls":
        if _database_dsn_uses_wallet_alias(dsn):
            return "walletless_tls_dsn_required"
        return "ok" if _s(settings, "oracle_password").strip() else "missing_credentials"
    if not wallet_path or not wallet_configured:
        return "wallet_not_found"
    if not _database_wallet_password_is_usable(
        wallet_path,
        _s(settings, "oracle_wallet_password").strip() or _s(settings, "oracle_password").strip(),
        driver_mode=mode,
    ):
        return "wallet_password_invalid"
    if _database_dsn_uses_wallet_alias(dsn) and not _database_wallet_service_exists(
        wallet_path, dsn
    ):
        return "invalid"
    return "ok"


def _database_dsn_uses_wallet_alias(dsn: str) -> bool:
    """TNS alias らしい短い DSN だけを Wallet service 検査の対象にする。"""
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+", dsn.strip()))


def _database_wallet_service_exists(wallet_path: Path, dsn: str) -> bool:
    """tnsnames.ora に DSN alias が登録されているかを大小無視で判定する。"""
    normalized = dsn.strip().lower()
    return any(service.lower() == normalized for service in _extract_wallet_services(wallet_path))


def database_readiness_message(readiness: str) -> str:
    if readiness == "invalid_configuration":
        return (
            "Oracle Deep Data Security は python-oracledb Thin mode のみ対応しています。"
            "NL2SQL_ORACLE_DEEPSEC_ENABLED=true の場合は "
            "PLATFORM_ORACLE_DRIVER_MODE=thin にしてください。"
        )
    if readiness == "invalid":
        return (
            "Oracle AI Database の接続設定を確認してください。"
            "サービス名 / DSN が現在の Wallet の tnsnames.ora に存在しません。"
        )
    if readiness == "wallet_not_found":
        return (
            "Oracle AI Database への接続に必要な Wallet を確認してください。"
            "現在の Wallet 保存先に接続用ファイルが揃っていません。"
        )
    if readiness == "wallet_password_invalid":
        return (
            "Oracle AI Database への接続に必要な Wallet パスワードを確認してください。"
            "暗号化 Wallet の ewallet.pem を現在の "
            "PLATFORM_ORACLE_WALLET_PASSWORD で復号できません。"
        )
    if readiness == "walletless_tls_dsn_required":
        return (
            "Walletless TLS では Wallet サービス名ではなく、"
            "ADB の TCPS 接続文字列または Easy Connect DSN を指定してください。"
        )
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
    if "ORA-12154" in code_set:
        return prefix + "Wallet サービス名が tnsnames.ora に存在するか確認してください。"
    if "ORA-12506" in code_set:
        return (
            prefix + "ADB のアクセス制御リストまたは network ACL が"
            "この接続元を許可しているか確認してください。"
        )
    if code_set & {"ORA-12514", "ORA-12505"}:
        return prefix + "Wallet サービス名と ADB の稼働状態を確認してください。"
    if code_set & {"ORA-12541", "DPY-6005", "DPY-6000"}:
        return prefix + "ADB の listener と TCPS 1522 への到達性を確認してください。"
    if "DPY-4011" in code_set:
        return prefix + "Wallet ZIP と Wallet パスワードを確認してください。"
    if "DPY-4000" in code_set:
        return (
            prefix
            + "サービス名 / DSN が現在の Wallet の tnsnames.ora に存在するか確認してください。"
        )
    if code_set & {"DPI-1047", "DPI-1072"}:
        return (
            prefix + "標準の Thin mode では Oracle Instant Client は不要です。"
            "Thick 互換設定を DeepSec 無効で使う場合のみ、"
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
        tips.append(
            "ユーザー名、Wallet サービス名、Wallet ZIP が入力・アップロード済みか確認してください。"
        )
    if readiness == "missing_credentials":
        tips.append("DB パスワードまたは Wallet パスワードが保存済みか確認してください。")
    if readiness == "wallet_not_found":
        tips.append("ADB からダウンロードした Wallet ZIP をアップロードし直してください。")
    if readiness == "wallet_password_invalid":
        tips.append(
            "Wallet パスワードが一致していません。OCI から Wallet を再取得するか、"
            "現在の Wallet ZIP 作成時に指定した Wallet パスワードを保存してください。"
        )
    if readiness == "invalid_configuration":
        tips.append(
            "DeepSec を有効にする場合は PLATFORM_ORACLE_DRIVER_MODE=thin とし、"
            "Thin mTLS 用の tnsnames.ora と ewallet.pem を "
            "PLATFORM_ORACLE_WALLET_DIR に配置してください。"
        )
    if readiness == "invalid":
        tips.append("Wallet の tnsnames.ora とサービス名の形式を確認してください。")
    if readiness == "walletless_tls_dsn_required":
        tips.append(
            "Walletless TLS の DSN は Wallet alias ではなく、ADB の TCPS 接続文字列または "
            "host:port/service_name 形式で指定してください。"
        )

    combined = f"{error_text} {error_type}".lower()
    if any(token in combined for token in ("timeout", "timed out", "oracleconnectiontimeouterror")):
        tips.append(
            "接続テストがタイムアウトしました。ADB が起動中か、VCN/VPN/プロキシ経路から "
            "TCPS 1522 に到達できるか確認してください。"
        )
    if "ora-01017" in combined:
        tips.append("ユーザー名または DB パスワードが正しいか確認してください。")
    if "ora-12154" in combined or "dpy-4000" in combined or "tns" in combined:
        tips.append("Wallet サービス名が tnsnames.ora に存在するか確認してください。")
    if "ora-12506" in combined:
        tips.append(
            "ADB の Network Access / ACL で、この実行ホストの public IP または VCN 経路を"
            "許可してください。"
        )
    if "ora-12514" in combined or "ora-12505" in combined:
        tips.append("Wallet サービス名が ADB の接続文字列として有効か確認してください。")
    if "ora-12541" in combined or "dpy-6005" in combined or "dpy-6000" in combined:
        tips.append(
            "データベースが停止していないか、ADB の listener に到達できるか確認してください。"
        )
    if "wallet" in combined or "dpy-4011" in combined:
        tips.append(
            "Wallet ZIP の内容、Wallet パスワード、PLATFORM_ORACLE_WALLET_DIR を確認してください。"
        )
    if "dpi-1047" in combined or "dpi-1072" in combined:
        tips.append(
            "PLATFORM_ORACLE_DRIVER_MODE=thin の標準構成へ戻してください。"
            "Thick 互換設定を DeepSec 無効で使う場合のみ、"
            "Oracle Instant Client と PLATFORM_ORACLE_CLIENT_LIB_DIR を確認してください。"
        )
    if "operationalerror" in combined and not tips:
        tips.append(
            "backend ログに出ている ORA/DPY/DPI エラーコードを確認し、"
            "Wallet、サービス名、認証情報、ネットワーク経路を切り分けてください。"
        )

    if not tips:
        tips.append(
            "バックエンドログの Oracle エラーコードと Wallet / DSN /"
            "ネットワーク設定を確認してください。"
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


def _same_path(requested: str, configured: str) -> bool:
    """利用者が送った path が、server の設定の path と同じか。

    利用者の path は filesystem を参照せず、字句だけで正規化して比べる（`~` の展開・絶対 path 化・
    `..` の解消）。symlink を辿る `resolve()` は server の設定の path にだけ使う（#1646）。
    """
    if not requested or not configured:
        return False
    requested_path = os.path.normpath(os.path.abspath(os.path.expanduser(requested)))
    configured_path = Path(configured).expanduser()
    return requested_path in {
        os.path.normpath(os.path.abspath(configured_path)),
        str(configured_path.resolve()),
    }


def _requested_wallet_dir(base: Any, payload: DatabaseSettingsUpdate) -> str:
    """payload の Wallet の保存先は、今の保存先と同じときだけ受け取る。

    Wallet の設置（アップロード・OCI からの取得）は保存先の既存のディレクトリを置き換えて削除する
    ため、API から任意の path を保存させない（画面は今の保存先を送るだけで、変更する欄は無い）。
    保存先は platform/.env の PLATFORM_ORACLE_WALLET_DIR（Thick は CLIENT_LIB_DIR）で決める。
    """
    requested = payload.wallet_dir.strip()
    current = _s(base, "oracle_wallet_dir").strip()
    resolved = _s(base, "resolved_oracle_wallet_dir").strip()
    if not requested or _same_path(requested, current):
        return current or resolved
    if _same_path(requested, resolved):
        return resolved
    raise HTTPException(
        status_code=422,
        detail=(
            "Wallet の保存先は画面・API からは変更できません。"
            "platform/.env の PLATFORM_ORACLE_WALLET_DIR で設定してください。"
        ),
    )


def database_settings_candidate(
    base: Any, payload: DatabaseSettingsUpdate, *, connection_security_enabled: bool
) -> Any:
    wallet_dir = _requested_wallet_dir(base, payload)
    updates: dict[str, Any] = {
        "oracle_user": payload.user.strip(),
        "oracle_dsn": payload.dsn.strip(),
        "oracle_wallet_dir": wallet_dir,
        "oracle_password": secret_value(
            current=_s(base, "oracle_password"),
            update=payload.password,
            clear=payload.clear_password,
        ),
        "oracle_wallet_password": secret_value(
            current=_s(base, "oracle_wallet_password"),
            update=payload.wallet_password,
            clear=payload.clear_wallet_password,
        ),
    }
    if connection_security_enabled and payload.connection_security:
        updates["oracle_connection_security"] = payload.connection_security
    return base.model_copy(update=updates)


_DATABASE_FIELDS = (
    "oracle_user",
    "oracle_password",
    "oracle_dsn",
    "oracle_connection_security",
    "oracle_client_lib_dir",
    "oracle_wallet_dir",
    "oracle_wallet_password",
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
        "PLATFORM_ORACLE_WALLET_DIR": _s(settings, "oracle_wallet_dir"),
        "PLATFORM_ORACLE_WALLET_PASSWORD": _s(settings, "oracle_wallet_password"),
    }
    # driver mode / 接続セキュリティを設定項目として持つ製品（NL2SQL）だけ保存する。
    if _has_field(settings, "oracle_driver_mode"):
        values["PLATFORM_ORACLE_DRIVER_MODE"] = driver_mode(settings)
    if _has_field(settings, "oracle_connection_security"):
        values["PLATFORM_ORACLE_CONNECTION_SECURITY"] = connection_security(settings)
    _write_env(
        env_file,
        values,
        section_comment="# Oracle AI Database",
        error_detail="Oracle AI Database の接続設定を platform/.env へ保存できませんでした。",
    )


def _persist_adb_settings(settings: Any, env_file: Path) -> None:
    _write_env(
        env_file,
        {
            "PLATFORM_ORACLE_ADB_OCID": _s(settings, "oracle_adb_ocid"),
            "PLATFORM_ORACLE_ADB_REGION": _s(settings, "oracle_adb_region"),
        },
        section_comment="# Oracle Autonomous Database 管理",
        error_detail="ADB 設定を platform/.env へ保存できませんでした。",
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


# --------------------------------------------------------------------------- wallet


def _wallet_storage_root(settings: Any) -> Path:
    """アップロード Wallet の固定保存先（製品の `resolved_oracle_wallet_dir`）。"""
    wallet_dir = _s(settings, "resolved_oracle_wallet_dir").strip()
    if not wallet_dir:
        raise HTTPException(
            status_code=422,
            detail=(
                "PLATFORM_ORACLE_WALLET_DIR または PLATFORM_ORACLE_CLIENT_LIB_DIR が未設定のため "
                "Wallet 保存先を決定できません。"
            ),
        )
    return Path(wallet_dir).expanduser().resolve()


@contextmanager
def database_wallet_install_lock(settings: Any) -> Iterator[None]:
    """複数 worker の Wallet 設置を非ブロッキング排他する。"""
    target = _wallet_storage_root(settings)
    lock_path = target.parent / f".{target.name}.install.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = lock_path.open("a+b")
        lock_path.chmod(ORACLE_WALLET_FILE_MODE)
    except OSError as exc:
        logger.exception(
            "database_wallet_install_stage_failed",
            extra={"wallet_stage": "lock_open", "wallet_error_code": "WALLET_STORAGE_UNAVAILABLE"},
        )
        raise DatabaseWalletOperationError(
            code="WALLET_STORAGE_UNAVAILABLE",
            public_message=(
                "Wallet 保存領域を使用できません。"
                "管理者に保存領域の書き込み権限を確認するよう依頼してから再試行してください。"
            ),
            stage="lock_open",
        ) from exc

    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HTTPException(
                status_code=409,
                detail=(
                    "別の Wallet 取得またはアップロードを処理中です。"
                    "完了後にもう一度お試しください。"
                ),
            ) from exc
        logger.info(
            "database_wallet_install_stage_completed", extra={"wallet_stage": "lock_acquired"}
        )
        yield
    finally:
        with suppress(OSError):
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def install_database_wallet(
    settings: Any,
    data: bytes,
    file_name: str | None,
    *,
    post_install: Callable[[Path], None] | None = None,
) -> Path:
    """Wallet ZIP を `resolved_oracle_wallet_dir` へ展開し、元の Wallet と原子的に差し替える。"""
    safe_name = _safe_wallet_filename(file_name)
    if not safe_name.lower().endswith(".zip"):
        raise HTTPException(
            status_code=415, detail="Oracle Wallet は ZIP ファイルを選択してください。"
        )
    if not data:
        raise HTTPException(status_code=400, detail="空の Wallet ZIP はアップロードできません。")

    target = _wallet_storage_root(settings)
    tmp_dir = target.parent / f".{target.name}.tmp-{uuid4().hex}"
    backup = target.parent / f".{target.name}.backup-{uuid4().hex}"
    previous_moved = False
    target_installed = False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        wallet_dir = extract_wallet_zip(data, tmp_dir, driver_mode=driver_mode(settings))
        if target.exists():
            target.replace(backup)
            previous_moved = True
        wallet_dir.replace(target)
        target_installed = True
        _secure_database_wallet(target)
        if post_install is not None:
            post_install(target)
        _remove_wallet_path(backup)
        logger.info(
            "database_wallet_install_stage_completed", extra={"wallet_stage": "atomic_replace"}
        )
        return target
    except HTTPException:
        _rollback_database_wallet_install(
            target, backup, previous_moved=previous_moved, target_installed=target_installed
        )
        raise
    except OSError as exc:
        _rollback_database_wallet_install(
            target, backup, previous_moved=previous_moved, target_installed=target_installed
        )
        logger.exception(
            "database_wallet_install_stage_failed",
            extra={"wallet_stage": "atomic_replace", "wallet_error_code": "WALLET_INSTALL_FAILED"},
        )
        raise DatabaseWalletOperationError(
            code="WALLET_INSTALL_FAILED",
            public_message=(
                "Wallet を安全に設置できませんでした。"
                "Wallet は変更前の状態に戻されています。"
                "管理者に保存領域の空き容量と書き込み権限を確認するよう依頼してから再試行してください。"
            ),
            stage="atomic_replace",
        ) from exc
    finally:
        _remove_tmp_wallet_dir(tmp_dir)


def _rollback_database_wallet_install(
    target: Path,
    backup: Path,
    *,
    previous_moved: bool,
    target_installed: bool,
) -> None:
    """atomic install 失敗時だけ今回の変更を戻す。rollback 失敗は server log に残す。"""
    try:
        if target_installed:
            _remove_wallet_path(target)
        if previous_moved and backup.exists():
            backup.replace(target)
        logger.info("database_wallet_install_stage_completed", extra={"wallet_stage": "rollback"})
    except OSError:
        logger.exception(
            "database_wallet_install_rollback_failed",
            extra={"wallet_stage": "rollback", "wallet_error_code": "WALLET_INSTALL_FAILED"},
        )


def _secure_database_wallet(wallet_dir: Path) -> None:
    """Wallet 配下を所有者だけが読める権限へ補正する。"""
    wallet_dir.chmod(ORACLE_WALLET_DIRECTORY_MODE)
    for path in wallet_dir.rglob("*"):
        if path.is_dir():
            path.chmod(ORACLE_WALLET_DIRECTORY_MODE)
        elif path.is_file():
            path.chmod(ORACLE_WALLET_FILE_MODE)


def _remove_wallet_path(path: Path) -> None:
    """今回の atomic install が管理する path だけを削除する。"""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def extract_wallet_zip(data: bytes, target_dir: Path, *, driver_mode: str = "thin") -> Path:
    """ZIP を検証しながら展開し、config_dir として使うディレクトリを返す。"""
    extracted_files: list[Path] = []
    total_uncompressed = 0
    try:
        target_dir.mkdir(mode=ORACLE_WALLET_DIRECTORY_MODE, parents=False, exist_ok=False)
        target_dir.chmod(ORACLE_WALLET_DIRECTORY_MODE)
        with ZipFile(io.BytesIO(data)) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            if not members:
                raise HTTPException(
                    status_code=400, detail="Wallet ZIP にファイルが含まれていません。"
                )
            for member in members:
                total_uncompressed += member.file_size
                if total_uncompressed > ORACLE_WALLET_MAX_EXTRACTED_BYTES:
                    raise HTTPException(
                        status_code=413, detail="Wallet ZIP の展開後サイズが上限を超えています。"
                    )
                destination = _wallet_member_destination(target_dir, member.filename)
                if destination.name.lower() in ORACLE_WALLET_SKIPPED_FILES:
                    continue
                if _zip_member_is_symlink(member.external_attr):
                    raise HTTPException(
                        status_code=400, detail="Wallet ZIP にシンボリックリンクは含められません。"
                    )
                destination.parent.mkdir(
                    mode=ORACLE_WALLET_DIRECTORY_MODE, parents=True, exist_ok=True
                )
                destination.parent.chmod(ORACLE_WALLET_DIRECTORY_MODE)
                with archive.open(member) as src, destination.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                destination.chmod(ORACLE_WALLET_FILE_MODE)
                extracted_files.append(destination)
    except (BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise HTTPException(
            status_code=400, detail="Wallet ZIP の形式を確認してください。"
        ) from exc

    required_files = database_wallet_required_files(driver_mode)
    wallet_dir = _find_wallet_config_dir(extracted_files, required_files=required_files)
    if wallet_dir is None:
        required = ", ".join(sorted(required_files))
        raise HTTPException(
            status_code=400,
            detail=f"Wallet ZIP に {required} が含まれているか確認してください。",
        )
    return wallet_dir


def _wallet_member_destination(root: Path, member_name: str) -> Path:
    """Zip Slip を防ぎながら member の展開先を決める。"""
    path = PurePosixPath(member_name.replace("\\", "/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise HTTPException(
            status_code=400, detail="Wallet ZIP に安全でないファイルパスが含まれています。"
        )
    destination = (root.joinpath(*path.parts)).resolve()
    resolved_root = root.resolve()
    if resolved_root != destination and resolved_root not in destination.parents:
        raise HTTPException(
            status_code=400, detail="Wallet ZIP に安全でないファイルパスが含まれています。"
        )
    return destination


def _find_wallet_config_dir(
    extracted_files: list[Path],
    *,
    required_files: frozenset[str] = ORACLE_WALLET_THIN_REQUIRED_FILES,
) -> Path | None:
    """driver mode に必要な Oracle Net / 認証ファイルが揃うディレクトリを探す。"""
    candidates = {path.parent for path in extracted_files}
    for candidate in sorted(candidates, key=lambda path: len(path.parts)):
        names = {path.name.lower() for path in extracted_files if path.parent == candidate}
        if required_files.issubset(names):
            return candidate
    return None


_TNS_RESERVED_NAMES = frozenset(
    {
        "ADDRESS",
        "ADDRESS_LIST",
        "CONNECT_DATA",
        "DESCRIPTION",
        "DESCRIPTION_LIST",
        "HOST",
        "PORT",
        "PROTOCOL",
        "SECURITY",
        "SERVICE_NAME",
        "SSL_SERVER_CERT_DN",
    }
)


def _extract_wallet_services(wallet_dir: Path) -> list[str]:
    """tnsnames.ora からトップレベルの TNS alias を抽出する。"""
    tnsnames = wallet_dir / "tnsnames.ora"
    if not tnsnames.is_file():
        return []
    try:
        content = tnsnames.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    services: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"(?m)^([A-Za-z0-9_.-]+)\s*=", content):
        service = match.group(1)
        normalized = service.upper()
        if normalized in _TNS_RESERVED_NAMES or normalized in seen:
            continue
        seen.add(normalized)
        services.append(service)
    return services


def _zip_member_is_symlink(external_attr: int) -> bool:
    """ZIP metadata 上の symlink を拒否する。"""
    mode = external_attr >> 16
    return bool(mode and stat.S_ISLNK(mode))


def _safe_wallet_filename(file_name: str | None) -> str:
    """表示名由来の ZIP ファイル名を basename に丸める。"""
    name = PurePosixPath((file_name or "wallet.zip").replace("\\", "/")).name.strip()
    name = re.sub(r"[\x00-\x1f\x7f]+", "_", name).strip(" .")
    return name[:255] if name else "wallet.zip"


def _remove_tmp_wallet_dir(path: Path) -> None:
    """失敗時に今回作成した一時展開先だけを片付ける。"""
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


async def _read_upload_file(file: UploadFile, max_bytes: int) -> bytes:
    """アップロードファイルを 1 MB ずつ、上限を確認しながら読み込む。"""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=413, detail="Wallet ZIP のサイズが上限を超えています。")
        chunks.append(chunk)
    return b"".join(chunks)


def _require_wallet_download_configuration(settings: Any) -> None:
    """Wallet 取得前に不足を 422 として説明可能な範囲で検出する。"""
    if not _s(settings, "oracle_adb_ocid").strip():
        raise HTTPException(
            status_code=422,
            detail=(
                "ADB OCID が未設定のため Wallet を自動取得できません。"
                "データベース設定で ADB OCID を保存するか、"
                "Wallet ZIP を手動アップロードしてください。"
            ),
        )
    config_file = oci_config_file(settings)
    parsed = read_runtime_oci_config(config_file, oci_profile(settings))
    required_values = (
        parsed.user if parsed is not None else "",
        parsed.fingerprint if parsed is not None else "",
        parsed.tenancy if parsed is not None else "",
        parsed.key_file if parsed is not None else "",
    )
    region = _s(settings, "resolved_oracle_adb_region") or (
        parsed.region if parsed is not None else ""
    )
    key_path = (
        resolve_oci_key_file(parsed.key_file, config_file)
        if parsed is not None and parsed.key_file
        else None
    )
    if (
        not all(value.strip() for value in required_values)
        or not region.strip()
        or not (key_path and key_path.is_file())
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "OCI 認証設定または region が不足しているため Wallet を自動取得できません。"
                "OCI 認証設定を完了するか、Wallet ZIP を手動アップロードしてください。"
            ),
        )


def wallet_download_password(settings: Any) -> str:
    """OCI Wallet 生成 password は明示 Wallet password、未設定なら DB password を使う。"""
    password = (
        _s(settings, "oracle_wallet_password").strip() or _s(settings, "oracle_password").strip()
    )
    if not password:
        raise HTTPException(
            status_code=422,
            detail=(
                "Wallet を自動取得するには DB パスワードが必要です。"
                "PLATFORM_ORACLE_WALLET_PASSWORD が空の場合は PLATFORM_ORACLE_PASSWORD を "
                "Wallet password として使用し、"
                "PLATFORM_ORACLE_WALLET_PASSWORD にも保存します。"
            ),
        )
    return password


def install_uploaded_database_wallet(
    settings: Any,
    content: bytes,
    file_name: str | None,
    *,
    on_saved: Callable[[Any], None],
    extra_readiness: ExtraReadiness | None = None,
) -> DatabaseSettingsData:
    """アップロードされた Wallet ZIP を設置し、runtime の Wallet 保存先を更新する。"""
    with database_wallet_install_lock(settings):
        wallet_dir = install_database_wallet(settings, content, file_name)
    settings.oracle_wallet_dir = str(wallet_dir)
    on_saved(settings)
    return database_settings_data(settings, extra_readiness)


def prepare_database_wallet_download(
    settings: Any, *, extra_readiness: ExtraReadiness | None = None
) -> DatabaseWalletDownloadData | None:
    """使える Wallet が既にあれば返す。取得に必要な設定が足りなければ 422。"""
    with database_wallet_install_lock(settings):
        target = _wallet_storage_root(settings)
        mode = driver_mode(settings)
        password = (
            _s(settings, "oracle_wallet_password").strip()
            or _s(settings, "oracle_password").strip()
        )
        if _database_wallet_is_configured(
            target, driver_mode=mode
        ) and _database_wallet_password_is_usable(target, password, driver_mode=mode):
            return DatabaseWalletDownloadData(
                status="already_configured",
                settings=database_settings_data(settings, extra_readiness),
            )
        _require_wallet_download_configuration(settings)
    return None


def install_downloaded_database_wallet(
    settings: Any,
    wallet_zip: bytes,
    password: str,
    *,
    env_file: Path,
    on_saved: Callable[[Any], None],
    extra_readiness: ExtraReadiness | None = None,
) -> DatabaseSettingsData:
    """OCI から取得した Wallet を設置し、Wallet の保存先と password を `.env` に保存する。"""
    candidate = settings.model_copy(
        update={
            "oracle_wallet_dir": str(_wallet_storage_root(settings)),
            "oracle_wallet_password": password,
        }
    )

    def persist_settings(_wallet_dir: Path) -> None:
        try:
            _persist_database_settings(candidate, env_file)
        except HTTPException as exc:
            if exc.status_code != 500:
                raise
            logger.exception(
                "database_wallet_install_stage_failed",
                extra={
                    "wallet_stage": "configuration_persist",
                    "wallet_error_code": "WALLET_INSTALL_FAILED",
                },
            )
            raise DatabaseWalletOperationError(
                code="WALLET_INSTALL_FAILED",
                public_message=(
                    "Wallet の設定を安全に保存できませんでした。"
                    "Wallet は変更前の状態に戻されています。"
                    "管理者に設定保存領域の権限を確認するよう依頼してから再試行してください。"
                ),
                stage="configuration_persist",
            ) from exc

    with database_wallet_install_lock(settings):
        wallet_dir = install_database_wallet(
            settings, wallet_zip, "wallet.zip", post_install=persist_settings
        )
    settings.oracle_wallet_dir = str(wallet_dir)
    settings.oracle_wallet_password = password
    on_saved(settings)
    return database_settings_data(settings, extra_readiness)


# --------------------------------------------------------------------------- ADB

AdbFailureOperation = Literal["info", "start", "stop"]
_ADB_FAILURE_RESPONSES: dict[AdbFailureOperation, tuple[str, str]] = {
    "info": (
        "ADB_INFO_UNAVAILABLE",
        "ADB 情報を取得できませんでした。"
        "OCI 認証、リージョン、ADB OCID を確認して再試行してください。",
    ),
    "start": (
        "ADB_START_FAILED",
        "ADB の起動を開始できませんでした。"
        "OCI 権限、リージョン、ADB OCID を確認して再試行してください。",
    ),
    "stop": (
        "ADB_STOP_FAILED",
        "ADB の停止を開始できませんでした。"
        "OCI 権限、リージョン、ADB OCID を確認して再試行してください。",
    ),
}


def oci_failure_log_extra(exc: BaseException) -> dict[str, Any]:
    """OCI の API の失敗を切り分ける項目（例外の種類・HTTP status・OCI の code・request id）。

    OCI SDK の `ServiceError` の属性だけを読み、メッセージ本文や request の内容（Wallet の
    password 等）は含めない。
    """
    extra: dict[str, Any] = {"exception_type": type(exc).__name__}
    status = getattr(exc, "status", None)
    if isinstance(status, int):
        extra["oci_status"] = status
    for key, attr in (("oci_code", "code"), ("oci_operation", "operation_name")):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value:
            extra[key] = value
    headers = getattr(exc, "headers", None)
    if isinstance(headers, Mapping):
        request_id = headers.get("opc-request-id")
        if isinstance(request_id, str) and request_id:
            extra["opc_request_id"] = request_id
    return extra


def _adb_not_configured(settings: Any) -> AdbInfoData:
    region = _s(settings, "resolved_oracle_adb_region")
    return AdbInfoData(
        status="not_configured",
        message="ADB OCID が設定されていません。",
        error_code="ADB_NOT_CONFIGURED",
        region=region or None,
    )


def _apply_adb_settings(settings: Any, payload: AdbSettingsUpdate) -> None:
    """ADB 操作対象 OCID と region を反映する。"""
    settings.oracle_adb_ocid = payload.adb_ocid.strip()
    settings.oracle_adb_region = payload.region.strip()


def _adb_info_data(
    status: AdbOperationStatus,
    message: str,
    info: AutonomousDatabaseInfo,
    region: str | None,
    *,
    lifecycle_override: str | None = None,
    error_code: str | None = None,
) -> AdbInfoData:
    """ADB 情報スナップショットを表示用データへ変換する。"""
    return AdbInfoData(
        status=status,
        message=message,
        error_code=error_code,
        id=info.id,
        display_name=info.display_name,
        lifecycle_state=lifecycle_override or info.lifecycle_state,
        db_name=info.db_name,
        cpu_core_count=info.cpu_core_count,
        data_storage_size_in_tbs=info.data_storage_size_in_tbs,
        region=region,
    )


def _adb_failure_data(
    operation: AdbFailureOperation,
    exc: Exception,
    *,
    adb_ocid: str,
    region: str | None,
    info: AutonomousDatabaseInfo | None = None,
) -> AdbInfoData:
    """OCI SDK の詳細をレスポンスへ出さず ADB 操作失敗を正規化する。"""
    error_code, message = _ADB_FAILURE_RESPONSES[operation]
    logger.warning(
        "adb_operation_failed",
        extra={
            **oci_failure_log_extra(exc),
            "operation": operation,
            "error_code": error_code,
            "region": region,
            "adb_ocid_configured": bool(adb_ocid),
        },
    )
    if info is not None:
        return _adb_info_data("error", message, info, region, error_code=error_code)
    return AdbInfoData(
        status="error", message=message, error_code=error_code, id=adb_ocid, region=region
    )


async def load_adb_info(settings: Any) -> AdbInfoData:
    """ADB の情報を取得する。設定不足や OCI エラーは status へ載せて返す。"""
    region = _s(settings, "resolved_oracle_adb_region") or None
    adb_ocid = _s(settings, "oracle_adb_ocid").strip()
    if not adb_ocid:
        return _adb_not_configured(settings)
    try:
        info = await OciDatabaseClient(settings=settings).get_autonomous_database(adb_ocid)
    except Exception as exc:  # noqa: BLE001 - OCI SDK の多様な例外を表示用に正規化する
        return _adb_failure_data("info", exc, adb_ocid=adb_ocid, region=region)
    return _adb_info_data("success", "データベース情報を取得しました。", info, region)


async def control_adb(settings: Any, *, action: Literal["start", "stop"]) -> AdbInfoData:
    """ADB の起動/停止を OCI Database API 経由で行う。"""
    region = _s(settings, "resolved_oracle_adb_region") or None
    adb_ocid = _s(settings, "oracle_adb_ocid").strip()
    if not adb_ocid:
        return _adb_not_configured(settings)

    client = OciDatabaseClient(settings=settings)
    try:
        info = await client.get_autonomous_database(adb_ocid)
    except Exception as exc:  # noqa: BLE001
        return _adb_failure_data("info", exc, adb_ocid=adb_ocid, region=region)

    state = info.lifecycle_state
    if action == "start":
        if state == "AVAILABLE":
            return _adb_info_data(
                "already_available", "データベースは既に起動しています。", info, region
            )
        if state not in ("STOPPED", "UNAVAILABLE"):
            return _adb_info_data(
                "cannot_start",
                f"データベースの現在の状態 ({state}) では起動できません。",
                info,
                region,
            )
        try:
            await client.start_autonomous_database(adb_ocid)
        except Exception as exc:  # noqa: BLE001
            return _adb_failure_data("start", exc, adb_ocid=adb_ocid, region=region, info=info)
        return _adb_info_data(
            "accepted",
            f"データベース '{info.display_name}' の起動を開始しました。",
            info,
            region,
            lifecycle_override="STARTING",
        )

    if state == "STOPPED":
        return _adb_info_data("already_stopped", "データベースは既に停止しています。", info, region)
    if state != "AVAILABLE":
        return _adb_info_data(
            "cannot_stop",
            f"データベースの現在の状態 ({state}) では停止できません。",
            info,
            region,
        )
    try:
        await client.stop_autonomous_database(adb_ocid)
    except Exception as exc:  # noqa: BLE001
        return _adb_failure_data("stop", exc, adb_ocid=adb_ocid, region=region, info=info)
    return _adb_info_data(
        "accepted",
        f"データベース '{info.display_name}' の停止を開始しました。",
        info,
        region,
        lifecycle_override="STOPPING",
    )


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
    connection_security_enabled: bool = False,
    password_reveal_enabled: bool = False,
    write_dependencies: Sequence[params.Depends] = (),
    action_dependencies: Sequence[params.Depends] = (),
) -> APIRouter:
    """`/database*` の router を作る。製品側で `/settings` 配下に include する。

    - `test_connection(candidate)`：保存前の値で Oracle に接続し、失敗なら例外を送出する
    - `on_saved(settings)`：接続設定・Wallet を変えたあとに呼ぶ（接続 pool を閉じる等）
    - `extra_readiness(settings)`：製品固有の readiness（NL2SQL の DeepSec）。問題がなければ None
    - `connection_security_enabled`：Walletless TLS を選べるか（接続処理が対応している製品だけ）
    - `password_reveal_enabled`：保存済み DB パスワードの表示（`write_dependencies` を付ける）
    - `write_dependencies`：設定を書き換える操作（保存・Wallet・ADB の保存/起動/停止）
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
        candidate = database_settings_candidate(
            settings, payload, connection_security_enabled=connection_security_enabled
        )
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
        "/database/wallet", response_model=ApiResponse[DatabaseSettingsData], dependencies=write
    )
    async def upload_database_wallet(
        file: Annotated[UploadFile, File(...)],
    ) -> ApiResponse[DatabaseSettingsData]:
        settings = get_settings()
        content = await _read_upload_file(file, ORACLE_WALLET_MAX_BYTES)
        return ApiResponse(
            data=await run_in_threadpool(
                install_uploaded_database_wallet,
                settings,
                content,
                file.filename,
                on_saved=saved,
                extra_readiness=extra_readiness,
            )
        )

    @router.post(
        "/database/wallet/download",
        response_model=ApiResponse[DatabaseWalletDownloadData],
        dependencies=write,
    )
    async def download_database_wallet() -> ApiResponse[DatabaseWalletDownloadData]:
        """OCI から Wallet を取得し、ブラウザへ ZIP を返さずサーバーへ設置する。"""
        settings = get_settings()
        precheck = await run_in_threadpool(
            prepare_database_wallet_download, settings, extra_readiness=extra_readiness
        )
        if precheck is not None:
            return ApiResponse(data=precheck)

        adb_ocid = _s(settings, "oracle_adb_ocid").strip()
        client = OciDatabaseClient(settings=settings)
        password = wallet_download_password(settings)
        try:
            info = await client.get_autonomous_database(adb_ocid)
            generate_type = None if info.is_dedicated is True else "SINGLE"
            wallet_zip = await client.download_autonomous_database_wallet(
                adb_ocid, password, generate_type, ORACLE_WALLET_MAX_BYTES
            )
        except WalletDownloadTooLargeError as exc:
            raise HTTPException(
                status_code=413,
                detail=(
                    "OCI から取得した Wallet ZIP が 20 MB の上限を超えました。"
                    "OCI 側の Wallet を確認するか、Wallet ZIP を手動アップロードしてください。"
                ),
            ) from exc
        except Exception as exc:
            # 画面には OCI の応答をそのまま返さないため、切り分けの手がかりはログに残す。
            logger.warning(
                "database_wallet_download_failed",
                extra={
                    **oci_failure_log_extra(exc),
                    "wallet_error_code": "WALLET_DOWNLOAD_FAILED",
                    "region": _s(settings, "resolved_oracle_adb_region") or None,
                },
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    "OCI から Wallet を取得できませんでした。OCI 認証、region、ADB OCID、"
                    "IAM 権限を確認して再試行するか、Wallet ZIP を手動アップロードしてください。"
                ),
            ) from exc

        try:
            settings_data = await run_in_threadpool(
                install_downloaded_database_wallet,
                settings,
                wallet_zip,
                password,
                env_file=env_file(),
                on_saved=saved,
                extra_readiness=extra_readiness,
            )
        except DatabaseWalletOperationError:
            raise
        except HTTPException as exc:
            if exc.status_code in {400, 415}:
                # 検証で落ちた理由（必須ファイルの不足など。自前の文言）をログに残す。
                logger.warning(
                    "database_wallet_download_invalid",
                    extra={
                        "wallet_error_code": "WALLET_DOWNLOAD_INVALID",
                        "status_code": exc.status_code,
                        "reason": exc.detail,
                    },
                )
                raise HTTPException(
                    status_code=502,
                    detail=(
                        "OCI から取得した Wallet の内容を検証できませんでした。"
                        "OCI 側で Wallet を再生成して再試行するか、"
                        "Wallet ZIP を手動アップロードしてください。"
                    ),
                ) from exc
            raise
        return ApiResponse(
            data=DatabaseWalletDownloadData(status="downloaded", settings=settings_data)
        )

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
        candidate = (
            database_settings_candidate(
                base, payload, connection_security_enabled=connection_security_enabled
            )
            if payload is not None
            else base
        )
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

    @router.get("/database/adb", response_model=ApiResponse[AdbInfoData])
    async def get_adb_info() -> ApiResponse[AdbInfoData]:
        return ApiResponse(data=await load_adb_info(get_settings()))

    @router.post(
        "/database/adb/settings", response_model=ApiResponse[AdbInfoData], dependencies=write
    )
    async def update_adb_settings(payload: AdbSettingsUpdate) -> ApiResponse[AdbInfoData]:
        settings = get_settings()
        candidate = settings.model_copy(deep=True)
        _apply_adb_settings(candidate, payload)
        await run_in_threadpool(_persist_adb_settings, candidate, env_file())
        _apply_adb_settings(settings, payload)
        return ApiResponse(data=await load_adb_info(settings))

    @router.post("/database/adb/start", response_model=ApiResponse[AdbInfoData], dependencies=write)
    async def start_adb() -> ApiResponse[AdbInfoData]:
        return ApiResponse(data=await control_adb(get_settings(), action="start"))

    @router.post("/database/adb/stop", response_model=ApiResponse[AdbInfoData], dependencies=write)
    async def stop_adb() -> ApiResponse[AdbInfoData]:
        return ApiResponse(data=await control_adb(get_settings(), action="stop"))

    return router
