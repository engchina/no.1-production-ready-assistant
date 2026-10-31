"""Select AI の Credential が前提の操作の事前条件（#1688）。

業務プロファイルの保存（#1683）と同じ確認（`select_ai_readiness`）を、Select AI / Select AI Agent
での SQL の生成（画面・Agent の MCP）・合成データ・SQL 生成評価・Select AI の資産の操作に使う。
Credential が無いと分かったら、Oracle に触る処理（ジョブ・run・評価の job）を作る前に 409
`SELECT_AI_CREDENTIAL_MISSING` で止める。LLM と Oracle は呼ばない。
"""

from __future__ import annotations

import io
import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from test_nl2sql_job_runtime import _PROFILE_ID, _repository, _worker
from test_nl2sql_operation_profile_access import _request as _principal_request

from app.features.nl2sql import router as nl2sql_router
from app.features.nl2sql import synthetic_router
from app.features.nl2sql.models import JobCreateRequest, Nl2SqlEngine
from app.features.nl2sql.oracle_adapter import SelectAiCredentialMissingError
from app.features.nl2sql.select_ai_readiness import (
    check_select_ai_credential,
    is_credential_missing_oracle_error,
    uses_select_ai_credential,
)
from app.features.nl2sql.service import Nl2SqlService
from app.security.permissions import QUERY_GENERATE_PERMISSION, SQL_EXECUTE_PERMISSION
from app.settings import get_settings


class _CredentialAdapter:
    def __init__(self, exists: bool | None) -> None:
        self.exists = exists
        self.calls = 0

    def get_select_ai_credential_status(self, credential_name: str) -> tuple[str, bool]:
        self.calls += 1
        assert credential_name == "NL2SQL_OCI_CRED"
        if self.exists is None:
            raise RuntimeError("DPY-6005: cannot connect to database")
        return "ADMIN", self.exists


def _oracle_settings() -> SimpleNamespace:
    return SimpleNamespace(
        nl2sql_runtime_mode="oracle", nl2sql_select_ai_credential_name="NL2SQL_OCI_CRED"
    )


@pytest.fixture
def oracle_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.features.nl2sql.select_ai_readiness.get_settings", _oracle_settings)


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Nl2SqlService:
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    service = _worker(_repository())
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    return service


def _job(engine: Nl2SqlEngine) -> JobCreateRequest:
    return JobCreateRequest(
        question="注文一覧を確認したい",
        engine=engine,
        profile_id=_PROFILE_ID,
        generation_only=True,
    )


@pytest.mark.parametrize(
    "engine",
    [Nl2SqlEngine.SELECT_AI, Nl2SqlEngine.SELECT_AI_AGENT],
    ids=["select-ai", "select-ai-agent"],
)
def test_select_ai_job_without_credential_returns_409_before_creating_job(
    service: Nl2SqlService, oracle_settings: None, engine: Nl2SqlEngine
) -> None:
    adapter = _CredentialAdapter(exists=False)
    service._oracle_adapter = adapter  # type: ignore[assignment]  # noqa: SLF001
    request = _principal_request({_PROFILE_ID}, permissions={QUERY_GENERATE_PERMISSION})

    job = _job(engine).model_copy(update={"client_job_id": uuid.uuid4()})
    with pytest.raises(HTTPException) as exc_info:
        nl2sql_router.create_job(job, request)

    assert exc_info.value.status_code == 409
    detail = cast(dict[str, str], exc_info.value.detail)
    assert detail["code"] == "SELECT_AI_CREDENTIAL_MISSING"
    assert "運用設定の「Select AI Credential」で作成してから" in detail["message_ja"]
    assert adapter.calls == 1
    # ジョブを作らない（同じ client_job_id のジョブは無い）。
    assert service.get_job(str(job.client_job_id)) is None


def test_select_ai_job_with_credential_and_direct_engine_are_not_blocked(
    service: Nl2SqlService, oracle_settings: None
) -> None:
    adapter = _CredentialAdapter(exists=True)
    service._oracle_adapter = adapter  # type: ignore[assignment]  # noqa: SLF001
    request = _principal_request(
        {_PROFILE_ID}, permissions={QUERY_GENERATE_PERMISSION, SQL_EXECUTE_PERMISSION}
    )

    created = nl2sql_router.create_job(_job(Nl2SqlEngine.SELECT_AI), request).data
    assert created is not None and created.job_id
    assert adapter.calls == 1

    # OCI Enterprise AI の直接の生成は Select AI の Credential を使わないので確かめない。
    adapter.exists = False
    direct = nl2sql_router.create_job(_job(Nl2SqlEngine.ENTERPRISE_AI_DIRECT), request).data
    assert direct is not None and direct.job_id
    assert adapter.calls == 1


def test_select_ai_job_is_not_blocked_when_status_is_unavailable_or_runtime_is_deterministic(
    service: Nl2SqlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = _CredentialAdapter(exists=None)
    service._oracle_adapter = adapter  # type: ignore[assignment]  # noqa: SLF001
    request = _principal_request({_PROFILE_ID}, permissions={QUERY_GENERATE_PERMISSION})
    monkeypatch.setattr("app.features.nl2sql.select_ai_readiness.get_settings", _oracle_settings)
    assert nl2sql_router.create_job(_job(Nl2SqlEngine.SELECT_AI), request).data is not None
    monkeypatch.setattr(
        "app.features.nl2sql.select_ai_readiness.get_settings",
        lambda: SimpleNamespace(
            nl2sql_runtime_mode="deterministic", nl2sql_select_ai_credential_name="NL2SQL_OCI_CRED"
        ),
    )
    calls = adapter.calls
    assert nl2sql_router.create_job(_job(Nl2SqlEngine.SELECT_AI), request).data is not None
    assert adapter.calls == calls


def _app(*routers: Any, prefix: str = "/api") -> FastAPI:
    app = FastAPI()
    for item in routers:
        app.include_router(item, prefix=prefix)
    app.dependency_overrides[nl2sql_router._require_persistence] = lambda: None  # noqa: SLF001
    return app


def test_credential_readiness_api_reports_the_state(
    service: Nl2SqlService, oracle_settings: None
) -> None:
    service._oracle_adapter = _CredentialAdapter(exists=False)  # type: ignore[assignment]  # noqa: SLF001
    with TestClient(_app(nl2sql_router.router)) as client:
        response = client.get("/api/nl2sql/select-ai/credential-readiness")
    assert response.status_code == 200
    assert response.json()["data"] == {
        "credential_required": True,
        "credential_name": "NL2SQL_OCI_CRED",
        "credential_exists": False,
    }


def test_quality_evaluation_with_select_ai_engine_returns_409_before_submit(
    service: Nl2SqlService, oracle_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    service._oracle_adapter = _CredentialAdapter(exists=False)  # type: ignore[assignment]  # noqa: SLF001
    monkeypatch.setattr(nl2sql_router, "_assert_profile_access", lambda *_a, **_k: None)
    submitted: list[Any] = []
    monkeypatch.setattr(
        "app.features.nl2sql.router.quality_evaluation_service.submit",
        lambda **kw: submitted.append(kw),
    )
    files = {"file": ("evaluation.xlsx", io.BytesIO(b"x"), "application/octet-stream")}

    with TestClient(_app(nl2sql_router.router)) as client:
        response = client.post(
            "/api/nl2sql/quality-evaluations",
            data={"profile_id": _PROFILE_ID, "engines": ["select_ai_agent"], "repeat_count": "1"},
            files=files,
        )

    assert response.status_code == 409
    assert "SELECT_AI_CREDENTIAL_MISSING" in response.text
    assert submitted == []


def test_synthetic_run_returns_409_before_creating_run(
    oracle_settings: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    created: list[Any] = []
    fake = SimpleNamespace(
        adapter=_CredentialAdapter(exists=False),
        create=lambda *a, **k: created.append(a),
    )
    monkeypatch.setattr(synthetic_router, "get_synthetic_service", lambda: fake)

    with TestClient(_app(synthetic_router.router, prefix="/api/nl2sql")) as client:
        response = client.post(
            "/api/nl2sql/synthetic-data/runs",
            json={
                "profile_name": "SALES",
                "table_name": "APP.ORDERS",
                "confirmation": "ORDERS",
                "idempotency_key": "synthetic-credential-1",
            },
        )

    assert response.status_code == 409
    assert "SELECT_AI_CREDENTIAL_MISSING" in response.text
    assert created == []


@pytest.mark.parametrize(
    "path",
    ["/api/nl2sql/select-ai/profiles/refresh", "/api/nl2sql/select-ai-agent/assets/refresh"],
    ids=["select-ai-profile", "select-ai-agent-assets"],
)
def test_select_ai_asset_refresh_returns_409_without_credential(
    service: Nl2SqlService, oracle_settings: None, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    service._oracle_adapter = _CredentialAdapter(exists=False)  # type: ignore[assignment]  # noqa: SLF001
    monkeypatch.setattr(nl2sql_router, "_assert_profile_access", lambda *_a, **_k: None)
    with TestClient(_app(nl2sql_router.router)) as client:
        response = client.post(path, params={"profile_id": _PROFILE_ID})
    assert response.status_code == 409
    assert "SELECT_AI_CREDENTIAL_MISSING" in response.text


def test_helpers_classify_engines_and_ora() -> None:
    assert uses_select_ai_credential(["select_ai", "enterprise_ai_direct"]) is True
    assert uses_select_ai_credential([Nl2SqlEngine.ENTERPRISE_AI_DIRECT]) is False
    error = RuntimeError("wrapped")
    error.__cause__ = RuntimeError(
        'ORA-20004: Credential "ADMIN"."NL2SQL_OCI_CRED" does not exist\nORA-06512: at line 1'
    )
    assert is_credential_missing_oracle_error(error) is True
    assert is_credential_missing_oracle_error(RuntimeError("ORA-20004: other")) is False
    check = check_select_ai_credential(_CredentialAdapter(exists=False), _oracle_settings())
    assert check.missing is True
    with pytest.raises(SelectAiCredentialMissingError):
        from app.features.nl2sql.select_ai_readiness import assert_select_ai_credential_ready

        assert_select_ai_credential_ready(_CredentialAdapter(exists=False), _oracle_settings())
