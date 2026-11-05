"""ヘルスチェックの疎通テスト。"""

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pytest import LogCaptureFixture, MonkeyPatch

from app import main as main_module
from app.api.routes import health as health_route
from app.config import (
    DEFAULT_LOCAL_STORAGE_DIR,
    EnterpriseAiConfiguredModel,
    Settings,
    get_settings,
)
from app.main import UNHANDLED_ERROR_MESSAGE, app, create_app
from app.rag.system_schema import system_schema_manager
from app.readiness import pending_legacy_local_storage_dir
from tests.support import AsgiTestClient

client = AsgiTestClient(app)
LLM_TEMPLATE = '{"input":"${user_message}"}'
VLM_TEMPLATE = '{"input":"${data_base64}"}'


async def _run_inline(
    operation: Callable[..., Any],
    *args: object,
    **kwargs: object,
) -> Any:
    return operation(*args, **kwargs)


def test_health() -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status"] == "ok"
    assert body["data"]["version"] == "0.1.0"


def test_request_id_header_is_generated_and_preserved() -> None:
    generated = client.get("/api/health")
    assert generated.headers["x-request-id"]

    preserved = client.get("/api/health", headers={"X-Request-ID": "client-request-id"})
    assert preserved.headers["x-request-id"] == "client-request-id"


def test_unsafe_request_id_header_is_not_reflected() -> None:
    """安全でない request id はレスポンスへ反射せず、新しく採番する。"""
    resp = client.get("/api/health", headers={"X-Request-ID": "bad request id"})

    assert resp.status_code == 200
    assert resp.headers["x-request-id"]
    assert resp.headers["x-request-id"] != "bad request id"
    assert " " not in resp.headers["x-request-id"]


def test_readiness_missing_config_is_degraded(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    for field in (
        "enterprise_ai_endpoint",
        "enterprise_ai_project_id",
        "enterprise_ai_api_key",
        "enterprise_ai_default_text_model",
        "enterprise_ai_default_vision_model",
        "genai_embedding_model",
        "genai_rerank_model",
        "oracle_user",
        "oracle_password",
        "oracle_dsn",
    ):
        monkeypatch.setattr(settings, field, "")
    monkeypatch.setattr(settings, "enterprise_ai_models", [])

    resp = client.get("/api/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["data"]["status"] == "degraded"
    assert body["data"]["checks"] == {
        "enterprise_ai": "missing",
        "genai": "missing",
        "oracle": "missing",
        "local_storage": "ok",
    }


def test_readiness_complete_config_is_ok(monkeypatch: MonkeyPatch) -> None:
    _configure_llm_readiness(
        monkeypatch,
        oracle_password="super-secret-password",
    )

    resp = client.get("/api/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status"] == "ok"
    assert body["data"]["checks"] == {
        "enterprise_ai": "ok",
        "genai": "ok",
        "oracle": "ok",
        "local_storage": "ok",
    }
    assert "super-secret-password" not in str(body)


def test_readiness_does_not_require_project_id(monkeypatch: MonkeyPatch) -> None:
    """Project ID は任意（#9）。無い接続（DeepSeek・Aliyun など）でも ready にする（#81）。"""
    _configure_llm_readiness(monkeypatch)
    monkeypatch.setattr(get_settings(), "enterprise_ai_project_id", "")

    resp = client.get("/api/ready")

    assert resp.status_code == 200
    assert resp.json()["data"]["checks"]["enterprise_ai"] == "ok"


def test_readiness_with_local_upload_storage_checks_local_storage(
    monkeypatch: MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_llm_readiness(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "local_storage_dir", str(tmp_path / "upload-storage"))

    resp = client.get("/api/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["checks"] == {
        "enterprise_ai": "ok",
        "genai": "ok",
        "oracle": "ok",
        "local_storage": "ok",
    }


def test_readiness_requires_enterprise_ai_model_catalog(
    monkeypatch: MonkeyPatch,
) -> None:
    _configure_llm_readiness(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "enterprise_ai_models", [])
    monkeypatch.setattr(settings, "enterprise_ai_default_text_model", "")
    monkeypatch.setattr(settings, "enterprise_ai_default_vision_model", "")
    monkeypatch.setattr(settings, "enterprise_ai_llm_payload_template", LLM_TEMPLATE)
    monkeypatch.setattr(settings, "enterprise_ai_vlm_payload_template", VLM_TEMPLATE)

    resp = client.get("/api/ready")

    assert resp.status_code == 503
    assert resp.json()["data"]["checks"]["enterprise_ai"] == "missing"


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_readiness_production_requires_audit_salt(
    monkeypatch: MonkeyPatch, environment: str
) -> None:
    _configure_llm_readiness(
        monkeypatch,
        environment=environment,
        audit_context_hash_salt="",
    )

    resp = client.get("/api/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["data"]["status"] == "degraded"
    assert body["data"]["checks"] == {
        "enterprise_ai": "ok",
        "genai": "ok",
        "oracle": "ok",
        "local_storage": "ok",
        "audit_context_salt": "missing",
    }


@pytest.mark.parametrize("environment", ["staging", "production"])
def test_readiness_production_complete_config_is_ok(
    monkeypatch: MonkeyPatch, environment: str
) -> None:
    _configure_llm_readiness(
        monkeypatch,
        environment=environment,
        audit_context_hash_salt="production-audit-salt",
    )

    resp = client.get("/api/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status"] == "ok"
    assert body["data"]["checks"] == {
        "enterprise_ai": "ok",
        "genai": "ok",
        "oracle": "ok",
        "local_storage": "ok",
        "audit_context_salt": "ok",
    }
    assert "production-audit-salt" not in str(body)


def test_readiness_without_password_is_degraded(
    monkeypatch: MonkeyPatch,
) -> None:
    """DB パスワードが無ければ ok にしない（システム設定画面と同じ判定。#325）。"""
    _configure_llm_readiness(monkeypatch, oracle_password="")

    resp = client.get("/api/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["data"]["status"] == "degraded"
    assert body["data"]["checks"]["oracle"] == "missing_credentials"


def test_readiness_invalid_embedding_dim_is_degraded(monkeypatch: MonkeyPatch) -> None:
    _configure_llm_readiness(monkeypatch, genai_embedding_dim=1536)

    resp = client.get("/api/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["data"]["status"] == "degraded"
    assert body["data"]["checks"]["genai"] == "invalid"


def _configure_oracle_only(
    monkeypatch: MonkeyPatch,
    *,
    password: str = "oracle-password",
    dsn: str = "adb.example.com/rag",
) -> None:
    """DB ステータス API 用に Oracle 接続情報だけ設定する。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_user", "rag_app")
    monkeypatch.setattr(settings, "oracle_dsn", dsn)
    monkeypatch.setattr(settings, "oracle_password", password)
    monkeypatch.setattr(settings, "oracle_client_lib_dir", "")


def _schema_status(status: str = "ready") -> dict[str, object]:
    return {
        "status": status,
        "operation_state": {"status": "idle"},
    }


def test_database_status_not_configured_skips_probe(monkeypatch: MonkeyPatch) -> None:
    """接続情報未設定なら実接続を試さず not_configured を返す。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "oracle_user", "")
    monkeypatch.setattr(settings, "oracle_dsn", "")

    async def _must_not_probe(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("未設定時は実接続を試さない")

    monkeypatch.setattr(health_route, "test_oracle_connection", _must_not_probe)

    resp = client.get("/api/ready/database")

    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["status"] == "not_configured"
    assert body["data"]["check"] == "missing"


def test_database_status_ok_when_probe_succeeds(monkeypatch: MonkeyPatch) -> None:
    """設定済み + 実接続成功なら ok を返す。"""
    _configure_oracle_only(monkeypatch)

    async def _probe_ok(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(health_route, "test_oracle_connection", _probe_ok)
    monkeypatch.setattr(
        system_schema_manager,
        "status",
        lambda: _schema_status(),
    )
    monkeypatch.setattr(asyncio, "to_thread", _run_inline)

    resp = client.get("/api/ready/database")

    body = resp.json()
    assert body["data"]["status"] == "ok"
    assert body["data"]["check"] == "ok"


def test_database_status_uses_oracle_probe_timeout(monkeypatch: MonkeyPatch) -> None:
    """閲覧 API 用 timeout ではなく Oracle 接続テスト側の timeout に委ねる。"""
    _configure_oracle_only(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "db_read_timeout_seconds", 0.001)

    async def _probe_ok(*_args: object, **_kwargs: object) -> None:
        await asyncio.sleep(0.01)

    monkeypatch.setattr(health_route, "test_oracle_connection", _probe_ok)
    monkeypatch.setattr(
        system_schema_manager,
        "status",
        lambda: _schema_status(),
    )
    monkeypatch.setattr(asyncio, "to_thread", _run_inline)

    resp = client.get("/api/ready/database")

    body = resp.json()
    assert body["data"]["status"] == "ok"


def test_database_status_requires_system_schema_setup(monkeypatch: MonkeyPatch) -> None:
    """接続成功でも RAG table が不足していれば setup_required を返す。"""
    _configure_oracle_only(monkeypatch)

    async def _probe_ok(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(health_route, "test_oracle_connection", _probe_ok)
    monkeypatch.setattr(
        system_schema_manager,
        "status",
        lambda: _schema_status("missing"),
    )
    monkeypatch.setattr(asyncio, "to_thread", _run_inline)

    resp = client.get("/api/ready/database")

    body = resp.json()
    assert body["data"]["status"] == "setup_required"
    assert body["data"]["schema_status"] == "missing"


def test_database_status_unreachable_when_probe_fails(monkeypatch: MonkeyPatch) -> None:
    """設定済みでも起動していなければ unreachable を返す。"""
    _configure_oracle_only(monkeypatch)

    async def _probe_fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("listener does not currently know of service")

    monkeypatch.setattr(health_route, "test_oracle_connection", _probe_fail)

    resp = client.get("/api/ready/database")

    body = resp.json()
    assert body["data"]["status"] == "unreachable"
    assert body["data"]["check"] == "ok"
    assert body["data"]["detail"]


def test_database_status_unreachable_does_not_leak_connection_details(
    monkeypatch: MonkeyPatch,
) -> None:
    """ログイン不要の path なので、接続先・サービス名を含む例外の文字列を返さない(#320)。"""
    _configure_oracle_only(monkeypatch)

    async def _probe_fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(
            "DPY-6005: cannot connect to database (CONNECTION_ID=abc). "
            "ORA-12514: listener at adb.example.oraclecloud.com:1522 does not know of "
            "service secret_service_high"
        )

    monkeypatch.setattr(health_route, "test_oracle_connection", _probe_fail)

    body = client.get("/api/ready/database").json()

    assert body["data"]["status"] == "unreachable"
    assert body["data"]["detail"] == "Oracle connection probe failed (ORA-12514)."
    for secret in ("adb.example.oraclecloud.com", "1522", "secret_service_high", "CONNECTION_ID"):
        assert secret not in json.dumps(body, ensure_ascii=False)


def test_database_status_without_password_is_not_configured(
    monkeypatch: MonkeyPatch,
) -> None:
    """パスワードが無ければ接続を試さず not_configured（#325）。"""
    _configure_oracle_only(monkeypatch, password="")

    async def _must_not_probe(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("設定が不足しているときは実接続を試さない")

    monkeypatch.setattr(health_route, "test_oracle_connection", _must_not_probe)

    body = client.get("/api/ready/database").json()

    assert body["data"]["status"] == "not_configured"
    assert body["data"]["check"] == "missing_credentials"


def test_database_status_alias_dsn_is_not_configured(
    monkeypatch: MonkeyPatch,
) -> None:
    """DSN が別名の形なら接続を試さず not_configured（システム設定画面と同じ）。"""
    _configure_oracle_only(monkeypatch, dsn="otherdb_high")

    async def _must_not_probe(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("設定が不足しているときは実接続を試さない")

    monkeypatch.setattr(health_route, "test_oracle_connection", _must_not_probe)

    body = client.get("/api/ready/database").json()

    assert body["data"]["status"] == "not_configured"
    assert body["data"]["check"] == "invalid"


def test_database_status_schema_status_failure_is_setup_required(
    monkeypatch: MonkeyPatch,
) -> None:
    """system schema の状態を読めなければ setup_required（接続先は返さない）。"""
    _configure_oracle_only(monkeypatch)

    async def _probe_ok(*_args: object, **_kwargs: object) -> None:
        return None

    def _status_fail() -> dict[str, object]:
        raise RuntimeError("ORA-00942: table SECRET_OWNER.RAG_SYSTEM_SCHEMA does not exist")

    monkeypatch.setattr(health_route, "test_oracle_connection", _probe_ok)
    monkeypatch.setattr(system_schema_manager, "status", _status_fail)
    monkeypatch.setattr(asyncio, "to_thread", _run_inline)

    body = client.get("/api/ready/database").json()

    assert body["data"]["status"] == "setup_required"
    assert body["data"]["detail"] == "システムテーブルの状態を確認できませんでした (ORA-00942)。"
    assert "SECRET_OWNER" not in json.dumps(body, ensure_ascii=False)


def test_not_found_uses_api_response_shape() -> None:
    resp = client.get("/api/missing", headers={"X-Request-ID": "not-found-request"})

    assert resp.status_code == 404
    assert resp.headers["x-request-id"] == "not-found-request"
    body = resp.json()
    assert body["data"] is None
    assert body["error_messages"] == ["リソースが見つかりません。"]
    assert body["warning_messages"] == []


def test_method_not_allowed_uses_api_response_shape_and_allow_header() -> None:
    resp = client.post("/api/health", headers={"X-Request-ID": "method-request"})

    assert resp.status_code == 405
    assert resp.headers["x-request-id"] == "method-request"
    assert "GET" in resp.headers["allow"]
    body = resp.json()
    assert body["data"] is None
    assert body["error_messages"] == ["許可されていない HTTP メソッドです。"]


def test_validation_error_preserves_request_id() -> None:
    """検証エラーでもクライアント指定の request id を返す。"""
    resp = client.post(
        "/api/search",
        json={"query": "社内規程", "top_k": 0},
        headers={"X-Request-ID": "validation-request"},
    )

    assert resp.status_code == 422
    assert resp.headers["x-request-id"] == "validation-request"
    assert resp.json()["data"] is None


def test_unhandled_exception_uses_api_response_shape_and_logs_request_id(
    caplog: LogCaptureFixture,
) -> None:
    """未処理例外でも内部詳細を隠し、request id 付きでログに残す。"""
    failing_app = create_app()

    @failing_app.get("/api/fail")
    async def fail() -> None:
        raise RuntimeError("secret boom")

    failing_client = AsgiTestClient(failing_app, raise_app_exceptions=False)

    with caplog.at_level(logging.ERROR, logger="app.main"):
        resp = failing_client.get("/api/fail", headers={"X-Request-ID": "failure-request"})

    assert resp.status_code == 500
    assert resp.headers["x-request-id"] == "failure-request"
    body = resp.json()
    assert body["data"] is None
    assert body["error_messages"] == [UNHANDLED_ERROR_MESSAGE]
    assert "secret boom" not in str(body)

    record = next(item for item in caplog.records if item.message == "unhandled_api_error")
    record_data = record.__dict__
    assert record_data["request_id"] == "failure-request"
    assert record_data["method"] == "GET"
    assert record_data["path"] == "/api/fail"
    assert record_data["exception_type"] == "RuntimeError"


def _configure_llm_readiness(
    monkeypatch: MonkeyPatch,
    *,
    oracle_password: str = "oracle-password",
    oracle_client_lib_dir: str = "",
    genai_embedding_dim: int = 1024,
    environment: str = "local",
    audit_context_hash_salt: str = "",
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "environment", environment)
    monkeypatch.setattr(settings, "enterprise_ai_endpoint", "https://enterprise-ai.example")
    monkeypatch.setattr(
        settings,
        "enterprise_ai_project_id",
        "proj-example",
    )
    monkeypatch.setattr(settings, "enterprise_ai_default_vision_model", "enterprise-vlm")
    monkeypatch.setattr(
        settings,
        "enterprise_ai_models",
        [
            EnterpriseAiConfiguredModel(model_id="enterprise-llm", display_name="標準 LLM"),
            EnterpriseAiConfiguredModel(
                model_id="enterprise-vlm",
                display_name="Vision LLM",
                vision_enabled=True,
            ),
        ],
    )
    monkeypatch.setattr(settings, "enterprise_ai_default_text_model", "enterprise-llm")
    monkeypatch.setattr(settings, "enterprise_ai_api_key", "sk-test-secret")
    monkeypatch.setattr(settings, "enterprise_ai_llm_path", "/v1/llm/generate")
    monkeypatch.setattr(settings, "enterprise_ai_vlm_path", "/v1/vlm/extract")
    monkeypatch.setattr(settings, "enterprise_ai_llm_payload_template", "")
    monkeypatch.setattr(settings, "enterprise_ai_vlm_payload_template", "")
    monkeypatch.setattr(settings, "genai_embedding_model", "cohere.embed-v4.0")
    monkeypatch.setattr(settings, "genai_embedding_dim", genai_embedding_dim)
    monkeypatch.setattr(settings, "genai_rerank_model", "cohere.rerank-v4.0-fast")
    monkeypatch.setattr(settings, "oracle_user", "rag_app")
    monkeypatch.setattr(settings, "oracle_password", oracle_password)
    monkeypatch.setattr(settings, "oracle_dsn", "adb.example.com/rag")
    monkeypatch.setattr(settings, "oracle_client_lib_dir", oracle_client_lib_dir)
    monkeypatch.setattr(settings, "audit_context_hash_salt", audit_context_hash_salt)


def _legacy_storage_settings(local_storage_dir: Path) -> Settings:
    return Settings(local_storage_dir=str(local_storage_dir))


def test_pending_legacy_local_storage_dir_detects_legacy_data_with_default_dir(
    tmp_path: Path,
) -> None:
    """既定保存先で起動し旧ディレクトリに原本が残る場合だけ旧パスを返す。"""
    default_dir = tmp_path / "data" / "production-ready-rag"
    legacy_dir = tmp_path / "production-ready-rag"
    (legacy_dir / "objects").mkdir(parents=True)

    assert pending_legacy_local_storage_dir(
        _legacy_storage_settings(default_dir),
        legacy_dir=str(legacy_dir),
        default_dir=str(default_dir),
    ) == str(legacy_dir)
    # PLATFORM_LOCAL_STORAGE_DIR を明示設定した環境では案内しない。
    assert (
        pending_legacy_local_storage_dir(
            _legacy_storage_settings(tmp_path / "custom"),
            legacy_dir=str(legacy_dir),
            default_dir=str(default_dir),
        )
        is None
    )


def test_pending_legacy_local_storage_dir_ignores_missing_empty_or_linked_legacy_dir(
    tmp_path: Path,
) -> None:
    """旧ディレクトリが無い・空・新ディレクトリへの symlink の場合は案内しない。"""
    default_dir = tmp_path / "data" / "production-ready-rag"
    default_dir.mkdir(parents=True)
    legacy_dir = tmp_path / "production-ready-rag"
    settings = _legacy_storage_settings(default_dir)

    def pending() -> str | None:
        return pending_legacy_local_storage_dir(
            settings, legacy_dir=str(legacy_dir), default_dir=str(default_dir)
        )

    assert pending() is None
    legacy_dir.mkdir()
    assert pending() is None
    legacy_dir.rmdir()
    (default_dir / "objects").mkdir()
    legacy_dir.symlink_to(default_dir)
    assert pending() is None


def test_startup_warns_pending_legacy_local_storage_dir_without_moving_files(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
) -> None:
    """起動時は警告ログで移行を案内し、旧ディレクトリのファイルは移動しない。"""
    legacy_file = tmp_path / "production-ready-rag" / "objects" / "doc.pdf"
    legacy_file.parent.mkdir(parents=True)
    legacy_file.write_bytes(b"legacy")
    monkeypatch.setattr(
        main_module,
        "pending_legacy_local_storage_dir",
        lambda _settings: str(tmp_path / "production-ready-rag"),
    )

    with caplog.at_level(logging.WARNING, logger="app.main"):
        main_module._warn_pending_legacy_local_storage_dir(Settings())

    record = next(r for r in caplog.records if r.message == "legacy_local_storage_dir_detected")
    assert record.__dict__["legacy_local_storage_dir"] == str(tmp_path / "production-ready-rag")
    assert record.__dict__["local_storage_dir"] == DEFAULT_LOCAL_STORAGE_DIR
    assert legacy_file.read_bytes() == b"legacy"
