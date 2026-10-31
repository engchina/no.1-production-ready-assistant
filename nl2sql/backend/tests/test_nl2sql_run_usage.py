"""実行履歴の行のモデルの利用量（#3543）。

ジョブの trace の span の知らせから、LLM の呼び出しの回数と入力・出力・合計の token を集め、
実行履歴の行（`usage`）に残すことと、一覧・詳細の API が合計と計測の状態を返すことを確かめる。

- Enterprise AI Direct: OCI Enterprise AI の実の client を httpx の mock で動かす（usage は合成）。
- Select AI: DB の中でモデルを呼ぶので数を作らず「未計測」（DB は fake の接続）。
- 利用量の記録の無い行（この機能より前）は、一覧・詳細とも未計測（0 にしない）。
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pr_backend_core.observability.model_usage import RunModelUsage
from test_enterprise_ai_client import _direct_settings, _mock_http
from test_health import (
    _FakeOracleDb,
    _FakeRuntimeOracleAdapter,
    _OracleRuntimeNl2SqlService,
    _wait_for_job,
)
from test_nl2sql_history_paging import (  # noqa: F401 - fixture
    _history,
    _incremental_service,
    _principal,
    _request,
    _route_page,
    history_security,
)
from test_nl2sql_job_runtime import _GENERATED, _repository, _worker
from test_nl2sql_job_runtime import _request as _job_request

from app.features.nl2sql import enterprise_ai_client
from app.features.nl2sql import router as nl2sql_router
from app.features.nl2sql.models import (
    HistoryItem,
    JobCreateRequest,
    JobStatus,
    Nl2SqlEngine,
    Nl2SqlProfile,
)
from app.features.nl2sql.service import Nl2SqlService
from app.settings import get_settings


def _run_direct_job(
    monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]
) -> tuple[Nl2SqlService, HistoryItem]:
    """Enterprise AI Direct の SQL 生成のジョブを 1 回実行し、実行履歴の行を返す。"""
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    pending = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return pending.pop(0)

    _mock_http(monkeypatch, handler)
    client = enterprise_ai_client.OciEnterpriseAiDirectClient(_direct_settings())
    service = _worker(_repository(), client)  # type: ignore[arg-type]
    created = service.start_job(
        _job_request(), actor_user_uuid="user-1", actor_is_system_admin=True
    )
    assert service.run_next_nl2sql_job(worker_id="usage-test", job_id=created.job_id)
    job = service.get_job(created.job_id, actor_user_uuid="user-1")
    assert job is not None and job.status == JobStatus.DONE, job
    [item] = service.list_history().items
    assert not pending
    return service, item


def test_direct_job_records_the_llm_usage_of_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """SQL の生成の呼び出しの入力・出力・合計の token と、呼んだモデルを行に残す。"""
    _service, item = _run_direct_job(
        monkeypatch,
        [
            httpx.Response(
                200,
                json={
                    "output_text": _GENERATED,
                    "usage": {
                        "input_tokens": 1200,
                        "output_tokens": 80,
                        "input_tokens_details": {"cached_tokens": 200},
                    },
                },
            )
        ],
    )

    assert item.usage == RunModelUsage(
        models=["openai.gpt-oss-120b"],
        requests=1,
        input_tokens=1200,
        output_tokens=80,
        total_tokens=1280,
        cached_input_tokens=200,
        measurement="complete",
        measured_requests=1,
    )


def test_direct_job_with_a_failed_attempt_is_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """応答の返らなかった試行（再試行した 503）は数え、利用量は作らずに一部計測にする。"""
    _service, item = _run_direct_job(
        monkeypatch,
        [
            httpx.Response(503, text="busy"),
            httpx.Response(
                200,
                json={"output_text": _GENERATED, "usage": {"input_tokens": 10, "output_tokens": 5}},
            ),
        ],
    )

    assert item.usage is not None
    assert (item.usage.requests, item.usage.measured_requests) == (2, 1)
    assert (item.usage.total_tokens, item.usage.measurement) == (15, "partial")


def test_select_ai_job_is_unmeasured_without_numbers() -> None:
    """Select AI は DB の中でモデルを呼ぶので、数を作らず未計測（出どころを残す）にする。"""
    service = _OracleRuntimeNl2SqlService(_FakeRuntimeOracleAdapter(_FakeOracleDb()))
    profile = service.create_profile(
        Nl2SqlProfile(id="usage_select_ai", name="プロファイル A", allowed_tables=["INVOICES"])
    )

    created = service.start_job(
        JobCreateRequest(
            question="合成の質問", engine=Nl2SqlEngine.SELECT_AI, profile_id=profile.id
        )
    )
    job = _wait_for_job(service, created.job_id)

    assert job is not None and job.status == JobStatus.DONE, job
    [item] = service.list_history().items
    assert item.usage is not None
    assert item.usage.measurement == "unmeasured"
    assert item.usage.unmeasured_sources == ["select_ai"]
    assert (item.usage.requests, item.usage.total_tokens) == (0, 0)


def _with_usage(item: HistoryItem, usage: dict[str, Any] | None) -> HistoryItem:
    payload = item.model_dump(mode="json")
    payload["usage"] = usage
    return HistoryItem.model_validate(payload)


_USAGE_ITEMS = [
    # この機能より前の行（利用量の記録が無い）。
    _history(1, "user-1"),
    _with_usage(
        _history(2, "user-1"),
        {"requests": 2, "input_tokens": 900, "output_tokens": 100, "total_tokens": 1000},
    ),
    _with_usage(
        _history(3, "user-1"),
        {
            "requests": 2,
            "input_tokens": 90,
            "output_tokens": 10,
            "total_tokens": 100,
            "measurement": "partial",
            "measured_requests": 1,
        },
    ),
    _with_usage(
        _history(4, "user-1"),
        {"measurement": "unmeasured", "unmeasured_sources": ["select_ai"]},
    ),
    # LLM を呼ばなかった実行（チャットの「SQL を実行」）は実測の 0。
    _with_usage(_history(5, "user-1"), {}),
]


def test_history_route_returns_total_tokens_and_measurement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一覧の「token」の列: 合計と計測の状態。記録の無い行・Select AI だけの行は数を返さない。"""
    service = _incremental_service(_USAGE_ITEMS)
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    page = _route_page(_principal(admin=False, user_uuid="user-1"))
    totals = {item.id: (item.total_tokens, item.usage_measurement) for item in page.items}
    assert totals == {
        "hist-001": (None, None),
        "hist-002": (1000, "complete"),
        "hist-003": (100, "partial"),
        "hist-004": (None, "unmeasured"),
        "hist-005": (0, "complete"),
    }

    detail = nl2sql_router.history_item(
        _request(_principal(admin=False, user_uuid="user-1")),  # type: ignore[arg-type]
        "hist-003",
    ).data
    assert detail is not None and detail.usage is not None
    assert (detail.total_tokens, detail.usage_measurement) == (100, "partial")
    assert detail.usage.measured_requests == 1


def test_unreadable_usage_is_unmeasured_and_keeps_the_row() -> None:
    """形の違う利用量の記録は未計測にし、行そのものは読む。"""
    payload = _history(1, "user-1").model_dump(mode="json")
    payload["usage"] = {"requests": -1}
    item = HistoryItem.model_validate(payload)
    assert item.usage is None
    assert item.id == "hist-001"
