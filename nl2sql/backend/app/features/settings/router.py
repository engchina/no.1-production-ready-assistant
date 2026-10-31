"""RAG 由来のシステム設定 API。

OCI 認証、アップロード保存先、モデル、データベース設定画面が期待する
契約を NL2SQL プロジェクトにも提供する。値は現在の Settings インスタンスへ
反映し、secret 本文はレスポンスに返さない。
"""

import logging
import re
import stat
import time
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import APIRouter, HTTPException, Request
from pr_backend_core import ApiResponse
from pr_backend_core.observability.model_purpose import async_call_with_model_purpose
from pr_system_settings.answer_notice import build_answer_notice_router
from pr_system_settings.chat_attachments import build_chat_attachment_settings_router
from pr_system_settings.database import build_database_router
from pr_system_settings.model import build_model_router
from pr_system_settings.model_test_input import (
    MODEL_TEST_EMBEDDING_INPUT,
    MODEL_TEST_IMAGE_BYTES,
    MODEL_TEST_IMAGE_MIME_TYPE,
    MODEL_TEST_RERANK_DOCUMENTS,
    MODEL_TEST_RERANK_QUERY,
    MODEL_TEST_TEXT_PROMPT,
    MODEL_TEST_VISION_PROMPT,
    embedding_test_details,
    model_test_text_context,
    rerank_test_details,
)
from pr_system_settings.oci import build_oci_router
from pr_system_settings.oci import oci_config_file as _oci_config_file
from pr_system_settings.oci import oci_profile as _oci_profile
from pr_system_settings.oci import parse_oci_config as _parse_oci_config
from pr_system_settings.oci import read_oci_config_text as _read_oci_config_text
from pr_system_settings.system_schema import oracle_error_code, system_tables_status_error
from pr_system_settings.trace_recording import build_trace_recording_router
from pr_system_settings.upload_storage import (
    build_upload_storage_router,
)
from starlette.responses import JSONResponse

from app import settings as app_settings
from app.api.concurrency import run_sync_io
from app.api.problems import api_problem_response
from app.clients.oci_auth import (
    pem_file_is_encrypted,
    resolve_oci_key_file,
)
from app.clients.oci_enterprise_ai import OciEnterpriseAiClient
from app.clients.oci_genai import OciGenAiClient
from app.clients.oracle import close_oracle_pool, test_oracle_connection
from app.clients.oracle_diagnostics import oracle_connection_diagnostics
from app.env_file import locked_env_file, replace_env_file
from app.features.nl2sql.oracle_adapter import (
    OracleNl2SqlAdapter,
    SelectAiCredentialExistsError,
)
from app.features.settings.system_schema import SystemSchemaError, system_schema_manager
from app.features.settings.system_schema_runtime import reset_system_schema_runtime
from app.readiness import deepsec_readiness
from app.schemas.settings import (
    ModelSettingsTestRequest,
    SelectAiCredentialCreateRequest,
    SelectAiCredentialData,
    SystemTablesInitializeRequest,
    SystemTablesOperationData,
    SystemTablesStatusData,
)
from app.security.service import get_security_service
from app.settings import (
    MODEL_SETTINGS_STORE,
    Settings,
    get_settings,
)

router = APIRouter(prefix="/settings", tags=["settings"])
# アップロード保存先は3製品共通の実装（platform の pr_system_settings。#97）。
# 保存先は3製品共通の `.env`（platform/.env。#211）。
# テストで get_settings / PLATFORM_ENV_FILE を差し替えられるよう、呼出時に module の値を参照する。
router.include_router(
    build_upload_storage_router(
        get_settings=lambda: get_settings(),
        env_file=lambda: app_settings.PLATFORM_ENV_FILE,
    )
)
# OCI 認証も3製品共通の実装（pr_system_settings.oci。#100）。
# 権限は既存の RBAC（route manifest）が判定する。
router.include_router(
    build_oci_router(
        get_settings=lambda: get_settings(),
        env_file=lambda: app_settings.PLATFORM_ENV_FILE,
    )
)
# モデル設定も3製品共通の実装（pr_system_settings.model。#103）。
router.include_router(
    build_model_router(
        get_settings=lambda: get_settings(),
        store=MODEL_SETTINGS_STORE,
        run_model_test=lambda settings, request: _run_model_settings_test(settings, request),
    )
)


# データベース設定も3製品共通の実装（pr_system_settings.database。#108）。
# 接続そのもの（Thin / DeepSec 対応の adapter）と接続 pool の後始末は NL2SQL のものを渡す。
router.include_router(
    build_database_router(
        get_settings=lambda: get_settings(),
        env_file=lambda: app_settings.PLATFORM_ENV_FILE,
        test_connection=lambda candidate: test_oracle_connection(candidate),
        on_saved=lambda _settings: close_oracle_pool(),
        extra_readiness=deepsec_readiness,
        connection_failure_log_extra=lambda exc: oracle_connection_diagnostics(exc),
        connection_security_enabled=True,
        password_reveal_enabled=True,
    )
)
# 回答の注意書きも3製品共通の実装（pr_system_settings.answer_notice。#2405）。
# 保存先は共通の表のこの製品の行（#2449）。
router.include_router(
    build_answer_notice_router(
        load=lambda: get_security_service().load_answer_notice(),
        save=lambda record: get_security_service().save_answer_notice(record),
    )
)
router.include_router(
    build_trace_recording_router(provider=lambda: get_security_service().trace_recording),
)
# チャットの添付を使うか（運用設定 › チャットの添付。設定は製品ごと。既定は無効。#2517）。
router.include_router(
    build_chat_attachment_settings_router(
        load=lambda: get_security_service().chat_attachment_settings.load(),
        save=lambda record: get_security_service().chat_attachment_settings.save(record),
        get_settings=lambda: get_settings(),
    )
)
logger = logging.getLogger(__name__)
run_in_threadpool = run_sync_io
ENV_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
OCI_DIRECTORY_MODE = 0o700
OCI_PRIVATE_KEY_MAX_BYTES = 64 * 1024
SELECT_AI_CREDENTIAL_NAME = "NL2SQL_OCI_CRED"
SELECT_AI_CREDENTIAL_REGIONS = frozenset({"ap-osaka-1", "us-chicago-1"})
OCI_USER_OCID_RE = re.compile(r"^ocid1\.user\.[A-Za-z0-9.-]+$", re.IGNORECASE)
OCI_TENANCY_OCID_RE = re.compile(r"^ocid1\.tenancy\.[A-Za-z0-9.-]+$", re.IGNORECASE)
OCI_FINGERPRINT_RE = re.compile(
    r"^(?:[0-9a-f]{2}:){15}[0-9a-f]{2}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _SelectAiSigningMaterial:
    user_ocid: str
    tenancy_ocid: str
    fingerprint: str
    private_key: str


class _SelectAiCredentialConfigurationError(RuntimeError):
    def __init__(self, code: str, public_message: str, missing_fields: list[str]) -> None:
        self.code = code
        self.public_message = public_message
        self.missing_fields = list(dict.fromkeys(missing_fields))
        super().__init__(public_message)


@router.get(
    "/database/select-ai-credential",
    response_model=ApiResponse[SelectAiCredentialData],
)
def get_select_ai_credential(
    request: Request,
) -> ApiResponse[SelectAiCredentialData] | JSONResponse:
    """現 Oracle schema と OCI signing material の安全な readiness を返す。"""
    settings = get_settings()
    try:
        schema_name, exists = OracleNl2SqlAdapter(
            settings=settings
        ).get_select_ai_credential_status(SELECT_AI_CREDENTIAL_NAME)
    except Exception as exc:
        logger.warning(
            "select_ai_credential_status_failed",
            exc_info=True,
            extra={
                **oracle_connection_diagnostics(exc),
                "credential_name": SELECT_AI_CREDENTIAL_NAME,
            },
        )
        return _select_ai_credential_error_response(
            request,
            status_code=503,
            code="SELECT_AI_CREDENTIAL_STATUS_UNAVAILABLE",
            message=(
                "Select AI Credential の状態を Oracle から取得できませんでした。"
                "データベース接続を確認して再試行してください。"
            ),
        )

    missing_fields: list[str] = []
    try:
        _load_select_ai_signing_material(settings)
    except _SelectAiCredentialConfigurationError as exc:
        missing_fields = exc.missing_fields
    return ApiResponse(
        data=SelectAiCredentialData(
            schema_name=schema_name or settings.oracle_user.strip().upper(),
            exists=exists,
            region=_select_ai_region(settings),
            oci_auth_ready=not missing_fields,
            missing_fields=missing_fields,
        )
    )


@router.post(
    "/database/select-ai-credential",
    response_model=ApiResponse[SelectAiCredentialData],
)
def create_select_ai_credential(
    payload: SelectAiCredentialCreateRequest,
    request: Request,
) -> ApiResponse[SelectAiCredentialData] | JSONResponse:
    """管理者の明示操作で OCI signing key Credential を作成または再作成する。"""
    if payload.confirmation != "ADMIN_EXECUTE":
        return _select_ai_credential_error_response(
            request,
            status_code=422,
            code="SELECT_AI_CONFIRMATION_REQUIRED",
            message="実行するには確認語 ADMIN_EXECUTE を入力してください。",
        )

    settings = get_settings()
    adapter = OracleNl2SqlAdapter(settings=settings)
    try:
        _schema_name, already_exists = adapter.get_select_ai_credential_status(
            SELECT_AI_CREDENTIAL_NAME
        )
    except Exception as exc:
        _log_select_ai_credential_failure(
            request=request,
            action="status",
            region=payload.region,
            exc=exc,
            adapter=adapter,
        )
        return _select_ai_credential_error_response(
            request,
            status_code=503,
            code="SELECT_AI_CREDENTIAL_STATUS_UNAVAILABLE",
            message=(
                "Select AI Credential の状態を Oracle から取得できませんでした。"
                "データベース接続を確認して再試行してください。"
            ),
        )
    if already_exists and not payload.recreate:
        return _select_ai_credential_error_response(
            request,
            status_code=409,
            code="SELECT_AI_CREDENTIAL_EXISTS",
            message=(
                "NL2SQL_OCI_CRED は既に存在します。上書きせず、必要な場合だけ"
                "「Credential を再作成」を実行してください。"
            ),
        )
    try:
        material = _load_select_ai_signing_material(settings)
    except _SelectAiCredentialConfigurationError as exc:
        return _select_ai_credential_error_response(
            request,
            status_code=422,
            code=exc.code,
            message=exc.public_message,
        )

    try:
        operation = adapter.create_select_ai_credential(
            credential_name=SELECT_AI_CREDENTIAL_NAME,
            user_ocid=material.user_ocid,
            tenancy_ocid=material.tenancy_ocid,
            fingerprint=material.fingerprint,
            private_key=material.private_key,
            recreate=payload.recreate,
        )
    except SelectAiCredentialExistsError:
        return _select_ai_credential_error_response(
            request,
            status_code=409,
            code="SELECT_AI_CREDENTIAL_EXISTS",
            message=(
                "NL2SQL_OCI_CRED は既に存在します。上書きせず、必要な場合だけ"
                "「Credential を再作成」を実行してください。"
            ),
        )
    except Exception as exc:
        _log_select_ai_credential_failure(
            request=request,
            action="recreate" if payload.recreate else "create",
            region=payload.region,
            exc=exc,
            adapter=adapter,
        )
        return _select_ai_credential_error_response(
            request,
            status_code=502,
            code="SELECT_AI_CREDENTIAL_CREATE_FAILED",
            message=(
                "Select AI Credential を Oracle に作成できませんでした。"
                "データベース権限と OCI 認証設定を確認して再試行してください。"
            ),
        )

    try:
        _persist_select_ai_credential_settings(settings, payload.region)
    except HTTPException:
        logger.error(
            "select_ai_credential_settings_persist_failed",
            exc_info=True,
            extra={
                "actor": _request_actor(request),
                "action": operation,
                "credential_name": SELECT_AI_CREDENTIAL_NAME,
                "region": payload.region,
            },
        )
        return _select_ai_credential_error_response(
            request,
            status_code=500,
            code="SELECT_AI_CREDENTIAL_SETTINGS_PERSIST_FAILED",
            message=(
                "Credential は Oracle に作成されましたが、Select AI の既定設定を"
                "保存できませんでした。状態を再取得して管理者ログを確認してください。"
            ),
        )

    # 作成は確定している。読み直しが失敗しても失敗の応答にせず、作成済みとして返す
    # （画面は次の状態の取得で実際の状態に合わせる）。
    try:
        schema_name, exists = adapter.get_select_ai_credential_status(SELECT_AI_CREDENTIAL_NAME)
    except Exception as exc:
        logger.warning(
            "select_ai_credential_status_after_change_failed",
            exc_info=True,
            extra={
                **oracle_connection_diagnostics(exc),
                "credential_name": SELECT_AI_CREDENTIAL_NAME,
            },
        )
        schema_name, exists = "", True
    logger.info(
        "select_ai_credential_changed",
        extra={
            "actor": _request_actor(request),
            "action": operation,
            "credential_name": SELECT_AI_CREDENTIAL_NAME,
            "region": payload.region,
        },
    )
    return ApiResponse(
        data=SelectAiCredentialData(
            schema_name=schema_name or settings.oracle_user.strip().upper(),
            exists=exists,
            region=payload.region,
            oci_auth_ready=True,
            missing_fields=[],
            operation=operation,
        )
    )


@router.get(
    "/database/system-tables",
    response_model=ApiResponse[SystemTablesStatusData],
)
def get_system_tables_status() -> ApiResponse[SystemTablesStatusData]:
    """NL2SQL system table の状態を DDL なしで取得する。"""

    try:
        data = system_schema_manager.status()
    except Exception as exc:
        raise system_tables_status_error(exc) from exc
    return ApiResponse(data=SystemTablesStatusData.model_validate(data))


@router.post(
    "/database/system-tables/initialize",
    response_model=ApiResponse[SystemTablesOperationData],
)
def initialize_system_tables(
    request: Request,
    payload: SystemTablesInitializeRequest,
) -> ApiResponse[SystemTablesOperationData] | JSONResponse:
    """明示操作として system table を作成・更新または全再作成する。"""

    try:
        data = system_schema_manager.initialize(
            recreate=payload.recreate,
            confirmation=payload.confirmation,
        )
    except SystemSchemaError as exc:
        return api_problem_response(
            request,
            status_code=exc.status_code,
            detail=exc.public_message,
            code=exc.code,
            retryable=True if exc.retryable else None,
            headers=exc.retry_headers,
        )
    try:
        reset_system_schema_runtime(
            schema_epoch=int(data["operation_state"]["schema_epoch"]),
        )
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "システムテーブルは更新されましたが、実行中 API の再接続に失敗しました。"
                "状態を再取得してから再試行してください。"
            ),
        ) from exc
    return ApiResponse(data=SystemTablesOperationData.model_validate(data))


async def _run_model_settings_test(
    settings: Settings,
    request: ModelSettingsTestRequest,
) -> dict[str, str | int | float | bool | None]:
    """対象モデルの実 API 呼び出しを行い、表示用 details を返す（共有 router の hook）。"""
    if request.target_type == "enterprise_text":
        text = await async_call_with_model_purpose(
            "connection.check",
            OciEnterpriseAiClient(settings=settings).generate,
            MODEL_TEST_TEXT_PROMPT,
            model_test_text_context("Production Ready NL2SQL"),
        )
        return {"response_chars": len(text), "surface": "llm"}
    if request.target_type == "enterprise_vision":
        text = await async_call_with_model_purpose(
            "connection.check",
            OciEnterpriseAiClient(settings=settings).generate_from_image,
            MODEL_TEST_IMAGE_BYTES,
            MODEL_TEST_VISION_PROMPT,
            mime_type=MODEL_TEST_IMAGE_MIME_TYPE,
        )
        return {
            "surface": "vision",
            "response_chars": len(text),
        }
    if request.target_type == "embedding":
        vectors = await async_call_with_model_purpose(
            "connection.check",
            OciGenAiClient(settings=settings).embed,
            [MODEL_TEST_EMBEDDING_INPUT],
            input_type="SEARCH_QUERY",
        )
        # 次元が設定の値（Oracle の VECTOR 列）と同じかを確かめる（#45）。
        return dict(
            embedding_test_details(vectors, expected_dim=int(settings.oci_genai_embedding_dim))
        )
    # 固定の query と 2 文書を並べ、index と relevance_score が返るかを確かめる（#45）。
    ranks = await async_call_with_model_purpose(
        "connection.check",
        OciGenAiClient(settings=settings).rerank,
        MODEL_TEST_RERANK_QUERY,
        list(MODEL_TEST_RERANK_DOCUMENTS),
        top_n=len(MODEL_TEST_RERANK_DOCUMENTS),
    )
    return dict(rerank_test_details(ranks))


def _select_ai_region(settings: Settings) -> Literal["ap-osaka-1", "us-chicago-1"]:
    candidate = (
        settings.nl2sql_select_ai_region.strip() or settings.oci_region.strip() or "us-chicago-1"
    )
    return "ap-osaka-1" if candidate == "ap-osaka-1" else "us-chicago-1"


def _load_select_ai_signing_material(settings: Settings) -> _SelectAiSigningMaterial:
    """OCI config と非暗号化秘密鍵を検証し、Oracle bind 用の最小値だけ返す。"""
    config_file = _oci_config_file(settings)
    profile = _oci_profile(settings)
    try:
        parsed = _parse_oci_config(_read_oci_config_text(config_file), profile)
    except HTTPException as exc:
        raise _SelectAiCredentialConfigurationError(
            "SELECT_AI_OCI_CONFIG_INCOMPLETE",
            "OCI config を読み取れません。OCI 認証設定を保存してから再試行してください。",
            ["config_file"],
        ) from exc

    missing_fields = [
        name
        for name, value in (
            ("user", parsed.user),
            ("tenancy", parsed.tenancy),
            ("fingerprint", parsed.fingerprint),
            ("key_file", parsed.key_file),
        )
        if not value.strip()
    ]
    if not OCI_USER_OCID_RE.fullmatch(parsed.user.strip()):
        missing_fields.append("user")
    if not OCI_TENANCY_OCID_RE.fullmatch(parsed.tenancy.strip()):
        missing_fields.append("tenancy")
    if not OCI_FINGERPRINT_RE.fullmatch(parsed.fingerprint.strip()):
        missing_fields.append("fingerprint")
    if missing_fields:
        raise _SelectAiCredentialConfigurationError(
            "SELECT_AI_OCI_CONFIG_INCOMPLETE",
            "OCI 認証設定の必須項目が不足しているか、形式が正しくありません。",
            missing_fields,
        )

    key_path = resolve_oci_key_file(parsed.key_file, config_file)
    try:
        if not key_path.is_file():
            raise FileNotFoundError(key_path)
        if key_path.stat().st_size > OCI_PRIVATE_KEY_MAX_BYTES:
            raise ValueError("private key too large")
        key_mode = stat.S_IMODE(key_path.stat().st_mode)
        if key_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise PermissionError("private key permissions are too broad")
        key_bytes = key_path.read_bytes()
    except PermissionError as exc:
        raise _SelectAiCredentialConfigurationError(
            "SELECT_AI_PRIVATE_KEY_INVALID",
            "OCI 秘密鍵の権限を所有者だけが読み書きできる 0600 にしてください。",
            ["key_file_permissions"],
        ) from exc
    except (OSError, ValueError) as exc:
        raise _SelectAiCredentialConfigurationError(
            "SELECT_AI_PRIVATE_KEY_INVALID",
            "OCI 秘密鍵ファイルを読み取れません。OCI 認証設定を確認してください。",
            ["key_file"],
        ) from exc

    if pem_file_is_encrypted(key_path) or b"ENCRYPTED" in key_bytes.upper():
        raise _SelectAiCredentialConfigurationError(
            "SELECT_AI_PRIVATE_KEY_INVALID",
            "Select AI Credential には非暗号化 OCI 秘密鍵が必要です。",
            ["private_key_encrypted"],
        )
    try:
        load_pem_private_key(key_bytes, password=None)
    except (TypeError, ValueError) as exc:
        raise _SelectAiCredentialConfigurationError(
            "SELECT_AI_PRIVATE_KEY_INVALID",
            "OCI 秘密鍵の PEM 形式が正しくありません。鍵を再アップロードしてください。",
            ["private_key"],
        ) from exc

    private_key = "".join(
        line.strip()
        for line in key_bytes.decode("ascii", errors="strict").splitlines()
        if line.strip() and not line.strip().startswith("-----")
    )
    if not private_key:
        raise _SelectAiCredentialConfigurationError(
            "SELECT_AI_PRIVATE_KEY_INVALID",
            "OCI 秘密鍵の PEM 形式が正しくありません。鍵を再アップロードしてください。",
            ["private_key"],
        )
    return _SelectAiSigningMaterial(
        user_ocid=parsed.user.strip(),
        tenancy_ocid=parsed.tenancy.strip(),
        fingerprint=parsed.fingerprint.strip(),
        private_key=private_key,
    )


def _persist_select_ai_credential_settings(settings: Settings, region: str) -> None:
    _write_env_values(
        app_settings.BACKEND_ENV_FILE,
        {
            "NL2SQL_SELECT_AI_CREDENTIAL_NAME": SELECT_AI_CREDENTIAL_NAME,
            "NL2SQL_SELECT_AI_REGION": region,
        },
        section_comment="# Select AI Credential",
        error_detail="Select AI Credential 設定を backend/.env へ保存できませんでした。",
    )
    settings.nl2sql_select_ai_credential_name = SELECT_AI_CREDENTIAL_NAME
    settings.nl2sql_select_ai_region = region


def _select_ai_credential_error_response(
    request: Request, *, status_code: int, code: str, message: str
) -> JSONResponse:
    return api_problem_response(
        request,
        status_code=status_code,
        detail=message,
        code=code,
    )


def _request_actor(request: Request) -> str:
    principal = getattr(request.state, "principal", None)
    return str(
        getattr(principal, "login_user_id", "") or getattr(principal, "user_uuid", "") or "system"
    )[:256]


def _log_select_ai_credential_failure(
    *,
    request: Request,
    action: str,
    region: str,
    exc: Exception,
    adapter: OracleNl2SqlAdapter,
) -> None:
    actual_exists: bool | None = None
    with suppress(Exception):
        _schema_name, actual_exists = adapter.get_select_ai_credential_status(
            SELECT_AI_CREDENTIAL_NAME
        )
    logger.warning(
        "select_ai_credential_change_failed",
        exc_info=(type(exc), exc, exc.__traceback__),
        extra={
            "actor": _request_actor(request),
            "action": action,
            "credential_name": SELECT_AI_CREDENTIAL_NAME,
            "region": region,
            "oracle_error_code": oracle_error_code(exc),
            "credential_exists_after_failure": actual_exists,
        },
    )


def _expand(path: str) -> Path:
    return Path(path).expanduser()


def _write_env_values(
    path: Path,
    values: Mapping[str, str | None],
    *,
    section_comment: str,
    error_detail: str,
) -> None:
    """既存 .env のコメントや無関係な値を保ったまま指定 key だけ更新する。"""
    try:
        with locked_env_file(path) as env_path:
            lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
            next_lines: list[str] = []
            written: set[str] = set()
            for line in lines:
                key = _env_assignment_key(line)
                if key is None or key not in values:
                    next_lines.append(line)
                    continue
                if key in written:
                    continue
                value = values[key]
                if value is None:
                    written.add(key)
                    continue
                next_lines.append(f"{key}={_format_env_value(value)}")
                written.add(key)

            missing = [
                key for key, value in values.items() if key not in written and value is not None
            ]
            if missing:
                if next_lines and next_lines[-1].strip():
                    next_lines.append("")
                next_lines.append(section_comment)
                for key in missing:
                    value = values[key]
                    if value is not None:
                        next_lines.append(f"{key}={_format_env_value(value)}")

            content = "\n".join(next_lines).rstrip() + "\n"
            replace_env_file(env_path, content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=error_detail) from exc


def _env_assignment_key(line: str) -> str | None:
    """通常の .env 代入行から key を取り出す。コメント行は対象外。"""
    if line.lstrip().startswith("#"):
        return None
    match = ENV_ASSIGNMENT_RE.match(line)
    return match.group(1) if match else None


def _format_env_value(value: str) -> str:
    """python-dotenv と shell の両方で読みやすい .env value へ整形する。"""
    normalized = value.strip()
    if not normalized:
        return ""
    if re.search(r"[\s#\"']", normalized):
        return '"' + normalized.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return normalized


def _secret_value(*, current: str, update: str | None, clear: bool) -> str:
    """secret の保持・更新・削除を判定する。"""
    if clear:
        return ""
    if update is not None and update != "":
        return update
    return current


def _elapsed_ms(started: float) -> int:
    """perf_counter の開始時刻から経過 ms を返す。"""
    return max(0, round((time.perf_counter() - started) * 1000))
