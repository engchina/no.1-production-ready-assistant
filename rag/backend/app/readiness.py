"""起動前・staging gate 用の依存設定チェック。"""

from collections.abc import Mapping
from pathlib import Path

from pr_system_settings.database import database_readiness
from pr_system_settings.embedding import EMBEDDING_DIMENSIONS

from app.config import (
    DEFAULT_LOCAL_STORAGE_DIR,
    Settings,
    enterprise_ai_connection_for_model,
    enterprise_ai_default_model_id,
    enterprise_ai_model_catalog,
    enterprise_ai_vision_model_id,
)

READINESS_OK = "ok"
READINESS_MISSING = "missing"
READINESS_INVALID = "invalid"
READINESS_MISSING_CREDENTIALS = "missing_credentials"
# 既定値を /u01/data/production-ready-rag へ移す前の保存先。自動移行はせず、起動時に案内だけ出す。
LEGACY_LOCAL_STORAGE_DIR = "/u01/production-ready-rag"


def readiness_checks_are_ok(checks: Mapping[str, str]) -> bool:
    """readiness checks がすべて成功しているか判定する。"""
    return all(value == READINESS_OK for value in checks.values())


def readiness_checks(settings: Settings) -> dict[str, str]:
    """OCI / Oracle 前提の readiness check を実行する。"""
    checks = {
        "oci_common": _required_values_check(
            settings.oci_region,
            settings.oci_compartment_id,
        ),
        "enterprise_ai": _enterprise_ai_check(settings),
        "genai": _genai_check(settings),
        "oracle": _oracle_check(settings),
    }
    checks.update(_upload_storage_checks(settings))
    checks.update(_deployed_safety_checks(settings))
    return checks


def pending_legacy_local_storage_dir(
    settings: Settings,
    *,
    legacy_dir: str = LEGACY_LOCAL_STORAGE_DIR,
    default_dir: str = DEFAULT_LOCAL_STORAGE_DIR,
) -> str | None:
    """既定保存先で起動し、旧既定ディレクトリにデータが残っていればそのパスを返す。

    PLATFORM_LOCAL_STORAGE_DIR を明示設定している環境、旧ディレクトリが新ディレクトリへの
    symlink 済み・空・存在しない環境では None を返す。ファイルの移動は行わない。
    """
    try:
        configured = Path(settings.local_storage_dir).expanduser().resolve()
        if configured != Path(default_dir).resolve():
            return None
        legacy = Path(legacy_dir)
        if legacy.is_symlink() or not legacy.is_dir():
            return None
        if not any(legacy.iterdir()):
            return None
    except OSError:
        return None
    return str(legacy)


def _deployed_safety_checks(settings: Settings) -> dict[str, str]:
    """staging / production 環境で必須にする安全設定を確認する。"""
    if not settings.is_deployed:
        return {}
    return {
        "audit_context_salt": (
            READINESS_OK if _is_present(settings.audit_context_hash_salt) else READINESS_MISSING
        ),
    }


def _local_storage_check(settings: Settings) -> str:
    """アップロード原本の local 保存先が作成・書き込み可能か確認する。"""
    try:
        root = Path(settings.local_storage_dir).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".readiness"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError:
        return "error"
    return READINESS_OK


def _upload_storage_checks(settings: Settings) -> dict[str, str]:
    """アップロード原本の保存先設定を確認する。"""
    if settings.upload_storage_backend == "oci":
        return {
            "object_storage": _required_values_check(
                settings.object_storage_region,
                settings.object_storage_namespace,
                settings.object_storage_bucket,
            )
        }
    return {"local_storage": _local_storage_check(settings)}


def _genai_check(settings: Settings) -> str:
    """OCI Generative AI の embedding/rerank 設定を確認する。"""
    required_status = _required_values_check(
        settings.oci_genai_embedding_model,
        settings.oci_genai_rerank_model,
    )
    if required_status != READINESS_OK:
        return required_status
    if settings.oci_genai_embedding_dim != EMBEDDING_DIMENSIONS:
        return READINESS_INVALID
    return READINESS_OK


def _enterprise_ai_check(settings: Settings) -> str:
    """OCI Enterprise AI の endpoint / model catalog を確認する。

    接続は既定のテキストモデルと既定の Vision モデルそれぞれの接続を確かめる(#533)。
    """
    api_path = settings.oci_enterprise_ai_llm_path or settings.oci_enterprise_ai_vlm_path
    default_model = enterprise_ai_default_model_id(settings)
    connections = {
        connection.connection_id: connection
        for connection in (
            enterprise_ai_connection_for_model(settings, default_model),
            enterprise_ai_connection_for_model(settings, enterprise_ai_vision_model_id(settings)),
        )
    }
    for connection in connections.values():
        required_status = _required_values_check(
            connection.endpoint,
            connection.project_ocid,
            api_path,
        )
        if required_status != READINESS_OK:
            return required_status
    if not all(_is_present(connection.api_key) for connection in connections.values()):
        return READINESS_MISSING_CREDENTIALS
    model_ids = {model.model_id for model in enterprise_ai_model_catalog(settings)}
    if not model_ids or not _is_present(default_model):
        return READINESS_MISSING
    if default_model not in model_ids:
        return READINESS_INVALID
    if not _is_present(enterprise_ai_vision_model_id(settings)):
        return READINESS_MISSING
    return READINESS_OK


def _oracle_check(settings: Settings) -> str:
    """Oracle AI Database の接続設定を確認する。

    システム設定画面・DB の状態 API（`/api/ready/database`）と同じ platform の判定を使う（#325）。
    Wallet mTLS では DB パスワードがあっても、Wallet のファイル・Wallet パスワード・
    サービス名（tnsnames.ora）がそろっていなければ ok にしない。
    """
    return database_readiness(settings)


def _required_values_check(*values: str) -> str:
    """必須文字列がすべて設定済みか確認する。"""
    if all(_is_present(value) for value in values):
        return READINESS_OK
    return READINESS_MISSING


def _is_present(value: str) -> bool:
    """空白のみの値を未設定として扱う。"""
    return bool(value.strip())
