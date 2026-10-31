"""回答前の確認の LLM の呼び出しを実行履歴の利用量に合算する（#3552）。

確認の要求（画面の session の作成・回答、MCP の `nl2sql_query` の確認の工程）はジョブの trace の
外で LLM を呼ぶ。確認の session が利用量を集めて反映した要件（`sql_requirement.usage`）でジョブへ
渡し、実行履歴の行でジョブの利用量と合算することを確かめる。

- 確認の LLM は決定論のスタブで、`gen_ai.chat` の span と合成の usage を出す。
- ジョブは Enterprise AI Direct の実の client を httpx の mock で動かす（usage は合成）。
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pr_backend_core.observability.genai import record_genai_usage
from pr_backend_core.observability.model_usage import RunModelUsage
from pr_backend_core.observability.tracing import observed_span
from test_enterprise_ai_client import _direct_settings, _mock_http
from test_nl2sql_guided_clarification import (
    _ambiguous_status_output,
    _answer,
    _create,
    _disable_profile_confirmation,
    _from_orders,
    _mcp,
    _McpJobs,
    _output,
    _runtime,
    _ScriptedLlm,
    _select_total,
    _where_shipped,
    mcp_requirement,
)
from test_nl2sql_job_runtime import _GENERATED, _PROFILE_ID, _repository, _worker
from test_nl2sql_job_runtime import _request as _job_request

import app.features.nl2sql.ontology_router as ontology_router_module
from app.features.nl2sql import enterprise_ai_client
from app.features.nl2sql import router as nl2sql_router
from app.features.nl2sql.models import (
    STORED_JOB_REQUEST_CONTEXT,
    AllowedObjects,
    HistoryItem,
    JobCreateRequest,
    JobStatus,
    Nl2SqlEngine,
    SqlRequirementSpec,
)
from app.features.nl2sql.ontology_models import QuerySession
from app.features.nl2sql.ontology_router import RequirementApplyRequest
from app.features.nl2sql.service import _with_requirement_usage
from app.settings import get_settings

# pytest の fixture を読み込む（推薦の確認を既定で外す autouse と、MCP を確認のスタブと fake の
# ジョブで動かすもの）。
__all__ = ["_disable_profile_confirmation", "mcp_requirement"]

_REQUIREMENT_MODEL = "requirement-model"


class _MeteredLlm(_ScriptedLlm):
    """呼び出しごとに `gen_ai.chat` の span と合成の usage を出す、確認の LLM のスタブ。"""

    def __init__(self, *responses: dict[str, Any], usages: list[dict[str, Any]]) -> None:
        super().__init__(*responses)
        self.usages = list(usages)

    def generate(self, *, prompt: str, context: str, system_prompt: str, **kwargs: Any) -> str:
        with observed_span(
            "gen_ai.chat", kind="client", attributes={"gen_ai.request.model": _REQUIREMENT_MODEL}
        ) as span:
            text = super().generate(
                prompt=prompt, context=context, system_prompt=system_prompt, **kwargs
            )
            record_genai_usage(span, self.usages.pop(0))
            return text


def _clear_output() -> dict[str, Any]:
    return _output(_from_orders(), _select_total(), _where_shipped(), clarified="出荷済みの合計")


def _run_job(
    monkeypatch: pytest.MonkeyPatch, request: JobCreateRequest, usage: dict[str, int]
) -> HistoryItem:
    """Enterprise AI Direct の SQL 生成のジョブを 1 回実行し、実行履歴の行を返す。"""
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    pending = [httpx.Response(200, json={"output_text": _GENERATED, "usage": usage})]

    def handler(_request: httpx.Request) -> httpx.Response:
        return pending.pop(0)

    _mock_http(monkeypatch, handler)
    client = enterprise_ai_client.OciEnterpriseAiDirectClient(_direct_settings())
    service = _worker(_repository(), client)  # type: ignore[arg-type]
    created = service.start_job(request, actor_user_uuid="user-1", actor_is_system_admin=True)
    assert service.run_next_nl2sql_job(worker_id="requirement-usage", job_id=created.job_id)
    job = service.get_job(created.job_id, actor_user_uuid="user-1")
    assert job is not None and job.status == JobStatus.DONE, job
    [item] = service.list_history().items
    assert not pending
    return item


def _spec(usage: RunModelUsage | None = None, *, session_id: str = "s") -> SqlRequirementSpec:
    return SqlRequirementSpec(
        session_id=session_id, profile_id=_PROFILE_ID, clarified_question="合成の質問", usage=usage
    )


def _job_with(spec: SqlRequirementSpec | None) -> JobCreateRequest:
    """確認を反映した要件を、この test の Enterprise AI Direct のジョブに付ける。"""
    return _job_request().model_copy(update={"sql_requirement": spec})


# --- 画面: session の作成・回答の利用量を足し、反映した要件でジョブへ写す ----------------------


def test_screen_requirement_usage_is_added_to_the_job_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = _MeteredLlm(
        _ambiguous_status_output(),
        _output(_from_orders(), _select_total(), clarified="出荷済みの合計"),
        usages=[
            {"input_tokens": 300, "output_tokens": 40},
            {
                "input_tokens": 200,
                "output_tokens": 30,
                "input_tokens_details": {"cached_tokens": 100},
            },
        ],
    )
    runtime = _runtime(llm)
    created = _create(runtime)
    # 下書きの呼び出し（session の作成）。
    assert created.session.requirement_usage is not None
    assert created.session.requirement_usage.total_tokens == 340
    answered = _answer(runtime, created)
    applied = runtime.apply_requirement(
        answered.session.id,
        RequirementApplyRequest(base_version=answered.session.current_intent_version),
    )
    # 再検査の呼び出し（回答）を足した、session の利用量。
    assert applied.session.requirement_usage == RunModelUsage(
        models=[_REQUIREMENT_MODEL],
        requests=2,
        input_tokens=500,
        output_tokens=70,
        total_tokens=570,
        cached_input_tokens=100,
        measurement="complete",
        measured_requests=2,
    )
    # session は保存した記録から読み直しても利用量を持つ。
    runtime.sessions._sessions.pop(applied.session.id)
    assert runtime.get_session(applied.session.id).session.requirement_usage is not None

    monkeypatch.setattr(ontology_router_module, "ontology_runtime", runtime)
    resolved = nl2sql_router._with_requirement(
        JobCreateRequest(
            question="出荷済みの受注金額の合計",
            profile_id="sales",
            requirement_session_id=applied.session.id,
        ),
        SimpleNamespace(user_uuid="user-1", is_system_admin=False),
    )
    assert resolved.sql_requirement is not None
    assert resolved.sql_requirement.usage == applied.session.requirement_usage
    # 利用量は生成の prompt に入れない。
    assert "570" not in resolved.sql_requirement.prompt_block()

    item = _run_job(
        monkeypatch,
        _job_with(resolved.sql_requirement),
        {"input_tokens": 1000, "output_tokens": 50},
    )
    assert item.usage == RunModelUsage(
        # 生成のモデルを先頭にし、確認のモデルを続ける。
        models=["openai.gpt-oss-120b", _REQUIREMENT_MODEL],
        requests=3,
        input_tokens=1500,
        output_tokens=120,
        total_tokens=1620,
        cached_input_tokens=100,
        measurement="complete",
        measured_requests=3,
    )


# --- MCP: 確認の工程の呼び出しも同じく足す --------------------------------------------------


def test_mcp_requirement_usage_is_added_to_the_job_usage(
    monkeypatch: pytest.MonkeyPatch, mcp_requirement: Callable[[_ScriptedLlm], _McpJobs]
) -> None:
    llm = _MeteredLlm(
        _ambiguous_status_output(),
        _output(_from_orders(), _select_total(), clarified="出荷済みの合計"),
        usages=[{"input_tokens": 300, "output_tokens": 40}, {"input_tokens": 200}],
    )
    jobs = mcp_requirement(llm)
    question = {"question": "出荷済みの受注金額の合計", "profile_id": "sales"}
    is_error, first = _mcp("nl2sql_query", question)
    assert is_error is False, first
    asked = first["requirement"]["question"]
    is_error, done = _mcp(
        "nl2sql_query",
        {
            **question,
            "requirement_session_id": first["requirement"]["session_id"],
            "answer": {
                "item_id": asked["item_id"],
                "selected_candidate_ids": [asked["candidates"][0]["id"]],
            },
        },
    )
    assert is_error is False, done
    [started] = jobs.started
    spec = started.sql_requirement
    assert spec is not None and spec.usage is not None
    # 再検査の応答が出力の token を返さなかったので一部計測（#3543 の規則のまま）。
    assert (spec.usage.requests, spec.usage.measured_requests) == (2, 1)
    assert (spec.usage.total_tokens, spec.usage.measurement) == (540, "partial")

    item = _run_job(monkeypatch, _job_with(spec), {"input_tokens": 1000, "output_tokens": 50})
    assert item.usage is not None
    assert (item.usage.requests, item.usage.measured_requests) == (3, 2)
    assert (item.usage.total_tokens, item.usage.measurement) == (1590, "partial")


def test_mcp_requirement_without_questions_counts_the_draft_call(
    mcp_requirement: Callable[[_ScriptedLlm], _McpJobs],
) -> None:
    jobs = mcp_requirement(
        _MeteredLlm(_clear_output(), usages=[{"input_tokens": 10, "output_tokens": 2}])
    )
    is_error, content = _mcp(
        "nl2sql_query", {"question": "出荷済みの受注金額の合計", "profile_id": "sales"}
    )
    assert is_error is False, content
    [started] = jobs.started
    assert started.sql_requirement is not None and started.sql_requirement.usage is not None
    assert started.sql_requirement.usage.total_tokens == 12


# --- 確認を経ない実行・記録の無い確認は今のまま -----------------------------------------------


def test_runs_without_requirement_usage_keep_the_job_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_usage = RunModelUsage(
        models=["model-a"],
        requests=1,
        input_tokens=10,
        output_tokens=2,
        total_tokens=12,
        measured_requests=1,
    )
    without_check = _job_request()
    # この機能より前の session から作った要件（利用量の記録が無い）。
    older_check = _job_with(_spec())
    for request in (without_check, older_check):
        assert _with_requirement_usage(job_usage, request) is job_usage
        assert _with_requirement_usage(None, request) is None

    item = _run_job(monkeypatch, without_check, {"input_tokens": 10, "output_tokens": 2})
    assert item.usage is not None
    assert (item.usage.requests, item.usage.total_tokens) == (1, 12)
    assert item.usage.measurement == "complete"


def test_job_without_its_own_record_keeps_the_requirement_usage_as_partial() -> None:
    """ジョブの記録が無い（trace の前に止まった）ときは、確認の分だけを一部計測で残す。"""
    requirement = RunModelUsage(
        models=[_REQUIREMENT_MODEL],
        requests=1,
        input_tokens=10,
        output_tokens=2,
        total_tokens=12,
        measured_requests=1,
    )
    request = _job_with(_spec(requirement))
    usage = _with_requirement_usage(None, request)
    assert usage is not None
    assert (usage.requests, usage.total_tokens, usage.measurement) == (1, 12, "partial")


# --- 読めない利用量の記録 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [{"requests": -1}, {"measurement": "unknown"}, "broken", 3],
    ids=["negative", "unknown-measurement", "string", "number"],
)
def test_unreadable_requirement_usage_is_dropped_and_keeps_the_records(stored: object) -> None:
    """形の違う利用量は無いものとし、session・保存したジョブの要求そのものは読む。"""
    llm = _MeteredLlm(_clear_output(), usages=[{"input_tokens": 1, "output_tokens": 1}])
    created = _create(_runtime(llm))
    payload = created.session.model_dump(mode="json")
    payload["requirement_usage"] = stored
    session = QuerySession.model_validate(payload)
    assert session.requirement_usage is None
    assert session.id == created.session.id

    request = _job_with(_spec(session_id=session.id))
    stored_request = request.model_dump(mode="json")
    stored_request["sql_requirement"]["usage"] = stored
    loaded = JobCreateRequest.model_validate(
        stored_request, context={STORED_JOB_REQUEST_CONTEXT: True}
    )
    assert loaded.sql_requirement is not None
    assert loaded.sql_requirement.usage is None
    assert loaded.sql_requirement.session_id == session.id
    assert _with_requirement_usage(None, loaded) is None


def test_job_request_with_requirement_usage_round_trips_through_storage() -> None:
    usage = RunModelUsage(requests=1, input_tokens=3, output_tokens=1, total_tokens=4)
    request = JobCreateRequest(
        question="合成の質問",
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        profile_id=_PROFILE_ID,
        allowed_objects=AllowedObjects(),
        sql_requirement=_spec(usage),
    )
    loaded = JobCreateRequest.model_validate(
        request.model_dump(mode="json"), context={STORED_JOB_REQUEST_CONTEXT: True}
    )
    assert loaded.sql_requirement is not None and loaded.sql_requirement.usage == usage
