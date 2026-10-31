"""LLM の応答が送り直しても未完了だった回答の失敗の返し方（#2023）。

検索（REST・SSE）・チャット・MCP・品質評価の 1 ケースは、未完了の理由ごとの文（何が起きたかと
直し方）を返し、技術的な値（理由・出力の上限・使った token・推論の量）は「詳細」の項目に分ける。
予期しない例外は定型文のまま（内部の文を出さない）。未完了の応答は adapter
（`rag_engine.adapters.oci`）が作る形のスタブで再現する。
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest
from pr_backend_core.mcp import mcp_error_from_exception
from pytest import LogCaptureFixture, MonkeyPatch
from rag_engine.adapters import oci
from rag_engine.generation.execution_record import AnswerExecutionError

from app.api.routes import chat as chat_route
from app.api.routes import search as search_route
from app.main import app
from app.rag.answer_failure import (
    ANSWER_INCOMPLETE_ERROR_CODE,
    ANSWER_INCOMPLETE_ERROR_TYPE,
    answer_error_identity,
    describe_answer_api_failure,
    describe_answer_failure,
)
from app.rag.evaluation import EVALUATION_CASE_ERROR_MESSAGE, EvaluationRunner
from app.schemas.evaluation import EvaluationCase
from app.schemas.search import SearchResponse
from tests.support import AsgiTestClient

client = AsgiTestClient(app)

_PROVIDER = SimpleNamespace(model="stub-model", label="stub", reasoning_effort="low")
_USAGE = SimpleNamespace(
    input_tokens=100,
    output_tokens=2000,
    input_tokens_details=None,
    output_tokens_details=SimpleNamespace(reasoning_tokens=1990),
)


def _answer_failure(reason: str = "max_output_tokens") -> AnswerExecutionError:
    """adapter が送り直しても未完了で送出する失敗を、回答の実行記録の例外で包んだもの。

    本番と同じ例外の連鎖にする。
    """
    response = SimpleNamespace(
        status="incomplete", incomplete_details=SimpleNamespace(reason=reason), usage=_USAGE
    )
    raw = oci.IncompleteResponse(f"stub の Responses API 応答が未完了です: {reason}", reason)
    final = oci._incomplete_error(
        raw,
        response,
        _PROVIDER,  # type: ignore[arg-type]
        2000,
        retried=True,
        lowered_effort=True,
        reasoning_effort="low",
    )
    final.__cause__ = raw
    error = AnswerExecutionError(str(final), "質問")
    error.execution_steps = (  # type: ignore[attr-defined]
        {"name": "回答の作成", "status": "エラーで中断"},
        {"name": "根拠の評価", "status": "complete"},
    )
    error.__cause__ = final
    return error


class _IncompletePipeline:
    def __init__(self, **_kwargs: object) -> None:
        pass

    async def run(self, *_args: object, **_kwargs: object) -> SearchResponse:
        raise _answer_failure()


def _error_event(text: str) -> dict[str, object]:
    block = text.split("event: error\n", 1)[1]
    event: dict[str, object] = json.loads(block.split("\n", 1)[0].removeprefix("data: "))
    return event


def test_incomplete_failure_has_the_reason_specific_message_and_details() -> None:
    failure = describe_answer_failure(_answer_failure(), "定型文")

    assert failure.incomplete
    assert failure.error_type == ANSWER_INCOMPLETE_ERROR_TYPE
    assert failure.reason == "max_output_tokens"
    assert failure.stage == "回答の作成"
    assert "回答が長くなり" in failure.message
    assert "出力の上限を広げ、推論の量を下げて送り直しても" in failure.message
    assert "質問を分けて" in failure.message
    # 技術的な値は「詳細」の文だけに入れる（messaging.md §10.3）。
    assert "tokens（うち推論" not in failure.message
    assert failure.detail is not None
    assert "出力の上限=2000 tokens" in failure.detail
    assert "使った出力=2000 tokens（うち推論 1990）" in failure.detail
    assert "推論の量=low" in failure.detail


def test_content_filter_failure_says_the_safety_check_stopped_it() -> None:
    failure = describe_answer_failure(_answer_failure("content_filter"), "定型文")

    assert failure.reason == "content_filter"
    assert "安全の判定" in failure.message


def test_unexpected_failure_keeps_the_fixed_message() -> None:
    failure = describe_answer_failure(RuntimeError("oracle dsn=secret-host"), "定型文")

    assert (failure.message, failure.error_type, failure.reason) == ("定型文", "RuntimeError", None)
    assert failure.event_fields() == {"message": "定型文", "error_type": "RuntimeError"}


def test_stream_returns_the_reason_and_details_and_logs_the_reason(
    monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    monkeypatch.setattr(search_route, "RagPipeline", _IncompletePipeline)

    with caplog.at_level(logging.ERROR, logger=search_route.__name__):
        response = client.post("/api/search/stream", json={"query": "条件の多い質問"})

    assert response.status_code == 200
    event = _error_event(response.text)
    assert event["error_type"] == ANSWER_INCOMPLETE_ERROR_TYPE
    assert event["reason"] == "max_output_tokens"
    assert event["stage"] == "回答の作成"
    assert event["trace_id"]
    assert "回答が長くなり" in str(event["message"])
    assert search_route.STREAM_ERROR_MESSAGE not in str(event["message"])
    assert "出力の上限=2000 tokens" in str(event["detail"])
    # journal に理由が残る（#2023）。
    record = next(r for r in caplog.records if r.getMessage() == "rag_search_stream_failed")
    assert record.__dict__["incomplete_reason"] == "max_output_tokens"
    assert "出力の上限=2000 tokens" in record.__dict__["incomplete_detail"]


def test_rest_search_returns_502_with_the_reason_and_mcp_gets_the_code(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(search_route, "RagPipeline", _IncompletePipeline)

    response = client.post("/api/search", json={"query": "条件の多い質問"})

    assert response.status_code == 502
    [message] = response.json()["error_messages"]
    assert "回答が長くなり" in message
    # MCP の rag_search（同じ `_run_search_with_timeout`）は、共通の error code の
    # ツールのエラーになる。
    converted = mcp_error_from_exception(search_route.AnswerIncompleteHTTPError(message))
    assert converted is not None
    assert (converted.code, converted.status, converted.message) == (
        ANSWER_INCOMPLETE_ERROR_CODE,
        502,
        message,
    )


def test_chat_error_message_names_the_reason() -> None:
    assert "回答が長くなり" in chat_route._chat_error_message(_answer_failure())
    assert (
        chat_route._chat_error_message(RuntimeError("secret-host"))
        == search_route.STREAM_ERROR_MESSAGE
    )


@pytest.mark.parametrize(
    ("raises_incomplete", "expected_type"),
    [(True, ANSWER_INCOMPLETE_ERROR_TYPE), (False, "RuntimeError")],
    ids=["incomplete", "unexpected"],
)
async def test_evaluation_case_error_keeps_the_reason(
    raises_incomplete: bool, expected_type: str
) -> None:
    class FailingPipeline:
        async def run(self, *_args: object, **_kwargs: object) -> SearchResponse:
            if raises_incomplete:
                raise _answer_failure()
            raise RuntimeError("raw secret detail")

    metrics = await EvaluationRunner(pipeline=FailingPipeline()).run(
        cases=[
            EvaluationCase(id="case-a", query="条件の多い質問", relevant_document_ids=["doc-1"])
        ],
        top_k=5,
    )

    [result] = metrics.case_results
    assert result.error_type == expected_type
    if raises_incomplete:
        assert result.error_message is not None
        assert "回答が長くなり" in result.error_message
        assert result.error_stage == "回答の作成"
    else:
        assert result.error_message == EVALUATION_CASE_ERROR_MESSAGE
        assert "raw secret detail" not in str(result.model_dump(mode="json"))


def _wrapped(cause: BaseException) -> AnswerExecutionError:
    try:
        raise AnswerExecutionError(str(cause), "表示") from cause
    except AnswerExecutionError as exc:
        return exc


def test_api_failure_is_classified_by_kind_without_body() -> None:
    """AI の API の失敗は種類・status・例外の型・呼び先の error code だけに分ける（#2475）。"""
    import httpx
    import openai

    request = httpx.Request("POST", "https://ai.invalid/v1/responses")
    limited = openai.RateLimitError(
        "本文の秘密",
        response=httpx.Response(429, request=request),
        body={"code": "rate_limit_exceeded"},
    )
    failure = describe_answer_api_failure(_wrapped(limited))
    assert failure is not None
    assert (failure.kind, failure.status, failure.provider_status) == ("rate_limited", 503, 429)
    assert failure.details() == {
        "kind": "rate_limited",
        "error_type": "RateLimitError",
        "provider_status": 429,
        "provider_code": "rate_limit_exceeded",
    }
    assert "時間をおいて再試行" in failure.message
    assert "本文の秘密" not in failure.message

    timeout = describe_answer_api_failure(_wrapped(openai.APITimeoutError(request)))
    assert timeout is not None
    assert (timeout.kind, timeout.status) == ("timeout", 504)

    # 安全でない形の error code は残さない。認証の誤りは設定を確かめる案内。
    denied = openai.AuthenticationError(
        "x", response=httpx.Response(401, request=request), body={"code": "bad code with spaces"}
    )
    auth = describe_answer_api_failure(_wrapped(denied))
    assert auth is not None
    assert (auth.kind, auth.status, auth.provider_code) == ("authentication", 502, None)
    assert "管理者" in auth.message


def test_non_api_failure_and_incomplete_response_are_not_api_failures() -> None:
    """AI の API の例外が原因に無い失敗と未完了の応答は分類しない（別の返し方。#2475）。"""
    assert describe_answer_api_failure(_wrapped(RuntimeError("x"))) is None
    assert describe_answer_api_failure(_answer_failure()) is None


# ---- 割り当ての使い切り（HTTP status の無い SDK の例外。#2667） ---------------------------


def _stream_api_failure(provider_error: dict[str, object]) -> AnswerExecutionError:
    """stream の error event の SDK の `APIError`（HTTP status 無し）を実行記録で包んだもの。

    呼び先のエラーを JSON の文字列にして `body.message` に入れる（合成の値）。
    """
    import httpx
    import openai

    text = json.dumps({"error": provider_error})
    cause = openai.APIError(
        text,
        httpx.Request("POST", "https://ai.invalid/v1/responses"),
        body={"code": None, "message": text},
    )
    error = AnswerExecutionError(str(cause), "質問")
    error.execution_steps = (  # type: ignore[attr-defined]
        {"name": "根拠の評価", "status": "complete"},
        {"name": "回答の作成", "status": "エラーで中断"},
    )
    error.__cause__ = cause
    return error


_QUOTA_ERROR: dict[str, object] = {
    "code": 429,
    "status": "resource-exhausted",
    "message": "synthetic token quota per hour exceeded",
}


def test_quota_exhausted_without_status_is_classified_with_stage_and_advice() -> None:
    """status の無い SDK の例外でも、本文の構造化したエラーから割り当ての使い切りに分ける。"""
    failure = describe_answer_api_failure(_stream_api_failure(_QUOTA_ERROR))

    assert failure is not None
    assert (failure.kind, failure.status, failure.provider_status) == ("quota_exhausted", 503, None)
    assert failure.details() == {
        "kind": "quota_exhausted",
        "error_type": "APIError",
        "stage": "回答の作成",
    }
    assert "割り当ての使い切り。中断した工程: 回答の作成" in failure.message
    assert "割り当てが戻るまで時間をおいて再試行" in failure.message
    # 呼び先の応答の本文は出さない。
    assert "synthetic" not in failure.message

    limited = describe_answer_api_failure(
        _stream_api_failure({"status": "RESOURCE_EXHAUSTED", "message": "synthetic"})
    )
    assert limited is not None
    assert (limited.kind, limited.status) == ("rate_limited", 503)
    assert "時間をおいて再試行" in limited.message

    unknown = describe_answer_api_failure(_stream_api_failure({"message": "synthetic"}))
    assert unknown is not None
    assert (unknown.kind, unknown.status) == ("unknown", 502)


class _QuotaPipeline:
    def __init__(self, **_kwargs: object) -> None:
        pass

    async def run(self, *_args: object, **_kwargs: object) -> SearchResponse:
        raise _stream_api_failure(_QUOTA_ERROR)


def test_rest_search_returns_the_classified_api_failure_instead_of_500(
    monkeypatch: MonkeyPatch, caplog: LogCaptureFixture
) -> None:
    monkeypatch.setattr(search_route, "RagPipeline", _QuotaPipeline)

    with caplog.at_level(logging.WARNING, logger=search_route.__name__):
        response = client.post("/api/search", json={"query": "条件の多い質問"})

    assert response.status_code == 503
    [message] = response.json()["error_messages"]
    assert "割り当ての使い切り。中断した工程: 回答の作成" in message
    assert "synthetic" not in response.text
    record = next(r for r in caplog.records if r.getMessage() == "rag_search_answer_api_failed")
    assert (record.__dict__["kind"], record.__dict__["stage"]) == ("quota_exhausted", "回答の作成")


def test_rest_search_passes_the_provider_retry_after(monkeypatch: MonkeyPatch) -> None:
    import httpx
    import openai

    class LimitedPipeline:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def run(self, *_args: object, **_kwargs: object) -> SearchResponse:
            request = httpx.Request("POST", "https://ai.invalid/v1/responses")
            cause = openai.RateLimitError(
                "synthetic",
                response=httpx.Response(429, request=request, headers={"retry-after": "30"}),
                body={"code": "rate_limit_exceeded"},
            )
            raise _wrapped(cause)

    monkeypatch.setattr(search_route, "RagPipeline", LimitedPipeline)

    response = client.post("/api/search", json={"query": "条件の多い質問"})

    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert "30 秒ほど待ってから再試行" in response.json()["error_messages"][0]


async def test_mcp_search_turns_the_classified_failure_into_a_tool_error(
    monkeypatch: MonkeyPatch,
) -> None:
    from pr_backend_core.mcp import McpToolError

    from app.mcp import tools as mcp_tools
    from app.schemas.search import SearchRequest

    monkeypatch.setattr(search_route, "RagPipeline", _QuotaPipeline)

    with pytest.raises(McpToolError) as caught:
        await mcp_tools._run_search(SearchRequest(query="条件の多い質問"))

    assert (caught.value.code, caught.value.status) == ("RAG_ANSWER_AI_API_FAILED", 503)
    assert caught.value.details == {
        "kind": "quota_exhausted",
        "error_type": "APIError",
        "stage": "回答の作成",
    }


def test_wrapped_failure_identity_keeps_the_cause_without_its_message() -> None:
    class SyntheticFailure(RuntimeError):
        code = "SYNTHETIC_FAILURE"

    cause = SyntheticFailure("秘密の本文を記録しない")
    cause.code = "SYNTHETIC_FAILURE"
    wrapped = AnswerExecutionError("秘密の本文を記録しない", "非公開の質問")
    wrapped.__cause__ = cause
    failure = describe_answer_failure(wrapped, "定型文")
    assert failure.error_type == "SyntheticFailure"
    assert failure.error_code == "SYNTHETIC_FAILURE"
    assert failure.message == "定型文"
    assert "秘密" not in json.dumps(failure.event_fields(), ensure_ascii=False)
    # 原因の参照が循環しても、例外の本文を使わずに終える。
    cause.__cause__ = wrapped
    assert answer_error_identity(wrapped) == ("SyntheticFailure", "SYNTHETIC_FAILURE")
