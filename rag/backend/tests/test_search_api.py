"""検索 API の HTTP 境界テスト。"""

import asyncio
import json
import logging
from typing import Any, cast

import pytest
from pydantic import ValidationError
from pytest import LogCaptureFixture, MonkeyPatch

from app.api.routes import search as search_route
from app.config import get_settings
from app.main import app
from app.rag.answer_timeout import answer_timeout_message
from app.rag.audit import record_rag_search_audit
from app.rag.diagnostics import build_search_diagnostics
from app.schemas.search import SearchRequest, SearchResponse
from tests.span_progress_support import answer_trace, pipeline_stage
from tests.support import AsgiTestClient
from tests.trace_store_support import FakeTraceDb

client = AsgiTestClient(app)


def test_search_api_returns_504_when_pipeline_times_out(
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
) -> None:
    """通常検索は pipeline timeout を ApiResponse 形式の 504 にして監査へ残す。"""
    _force_search_timeout(monkeypatch)

    with caplog.at_level(logging.INFO, logger="app.audit"):
        response = client.post("/api/search", json={"query": "INV-SECRET の承認条件"})

    assert response.status_code == 504
    body = response.json()
    assert body["data"] is None
    # どの工程で時間切れになったか（最後に始まった工程）と、再試行の案内を返す（#375）。
    assert body["error_messages"] == [answer_timeout_message("answer", 0.05)]
    assert "質問の条件を整理して回答を作る" in body["error_messages"][0]
    assert "もう一度送信してください" in body["error_messages"][0]

    audit_record = next(record for record in caplog.records if record.message == "rag_search_audit")
    audit_event = cast(Any, audit_record).audit_event
    assert audit_event["outcome"] == "error"
    assert audit_event["error_stage"] == "timeout"
    assert audit_event["error_type"] == "TimeoutError"
    assert audit_event["retrieved_count"] == 0
    assert audit_event["citation_count"] == 0
    assert audit_event["mode"] == "hybrid"
    assert audit_event["config_fingerprint"]
    assert audit_event["trace_id"]
    assert "INV-SECRET" not in str(audit_event)


def test_stream_search_api_emits_error_event_when_pipeline_times_out(
    monkeypatch: MonkeyPatch, trace_db: FakeTraceDb
) -> None:
    """SSE 検索は stream 開始後の timeout を error event として返す。"""
    _force_search_timeout(monkeypatch)

    response = client.post("/api/search/stream", json={"query": "承認条件"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: error" in response.text
    assert answer_timeout_message("answer", 0.05) in response.text
    # 最初に実行の id を送り、処理の経過は配信では送らない（共通の span の表に書く。#3226）。
    assert response.text.startswith("event: run\n")
    (run,) = _sse_payloads(response.text, "run")
    assert "event: chat_progress" not in response.text
    rows = trace_db.spans()
    assert [row["name"] for row in rows] == ["rag.answer", "rag.answer"]
    assert rows[0]["is_root"] == 1
    assert {row["operation_id"] for row in rows} == {run["trace_id"]}
    # 時間切れで止まった段は実行中のまま残さない（取り消しの印）。実行の trace の API で読める。
    assert "running" not in {row["status"] for row in rows}

    # 実行履歴に残らなかった（実行中・保存できなかった）実行は、表を実行の id で引く。
    class NoRunStore:
        def __init__(self, _oracle: object) -> None:
            pass

        async def get_run(self, _run_id: str) -> None:
            return None

    monkeypatch.setattr(search_route, "RunHistoryStore", NoRunStore)
    events = client.get(f"/api/search/runs/{run['trace_id']}/trace").json()["data"]["events"]
    assert [event["name"] for event in events if event["type"] == "step"] == [
        "rag.answer",
        "rag.answer",
    ]
    assert '"error_type": "TimeoutError"' in response.text
    assert '"stage": "answer"}' in response.text


def _sse_payloads(text: str, name: str) -> list[dict[str, Any]]:
    """SSE の本文の、名前が ``name`` のイベントの data を順に取り出す。"""
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        lines = block.strip().splitlines()
        if f"event: {name}" not in lines:
            continue
        data = "".join(line.removeprefix("data: ") for line in lines if line.startswith("data: "))
        events.append(json.loads(data))
    return events


@pytest.mark.parametrize("path", ["/api/search", "/api/search/stream"])
def test_search_answer_uses_answer_timeout_not_search_timeout(
    monkeypatch: MonkeyPatch, path: str
) -> None:
    """回答を LLM で作る検索は、検索だけの上限（旧 30 秒）ではなく回答生成の上限で打ち切る。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_answer_timeout_seconds", 5.0)
    monkeypatch.setattr(search_route, "RagPipeline", _SlowAnswerPipeline)

    response = client.post(path, json={"query": "承認条件"})

    assert response.status_code == 200
    if path.endswith("/stream"):
        assert "event: done" in response.text
        assert "event: error" not in response.text
    else:
        assert response.json()["data"]["answer"] == "遅い回答"


class _SlowAnswerPipeline:
    """検索だけの上限より長く、回答生成の上限より短くかかる pipeline。"""

    def __init__(self, **_kwargs: object) -> None:
        pass

    async def run(
        self, request: SearchRequest, trace_id: str | None = None, **_kwargs: object
    ) -> SearchResponse:
        await asyncio.sleep(0.1)
        return SearchResponse(answer="遅い回答", trace_id=trace_id or "trace", elapsed_ms=100.0)


def test_search_uses_selected_answer_model_only_from_choices(monkeypatch: MonkeyPatch) -> None:
    """RAG 検索の model_id は既定のテキスト / Vision モデルのときだけ回答のモデルにする(#675)。"""
    answer_models: list[object] = []

    class CapturingPipeline:
        def __init__(self, **kwargs: object) -> None:
            answer_models.append(kwargs.get("answer_model_id"))

        async def run(
            self, request: SearchRequest, trace_id: str | None = None, **_kwargs: object
        ) -> SearchResponse:
            return SearchResponse(answer="回答", trace_id=trace_id or "trace", elapsed_ms=1.0)

    monkeypatch.setattr(search_route, "RagPipeline", CapturingPipeline)
    monkeypatch.setattr(search_route, "enterprise_ai_model_catalog", lambda _s: [])
    monkeypatch.setattr(search_route, "enterprise_ai_default_model_id", lambda _s: "text-m")
    monkeypatch.setattr(search_route, "enterprise_ai_vision_model_id", lambda _s: "vision-m")

    for model_id in ("vision-m", "other-m", None):
        response = client.post("/api/search", json={"query": "承認条件", "model_id": model_id})
        assert response.status_code == 200
    assert answer_models == ["vision-m", None, None]

    models = client.get("/api/search/models").json()["data"]
    assert [(m["model_id"], m["kind"]) for m in models] == [
        ("text-m", "text"),
        ("vision-m", "vision"),
    ]

    # 既定のテキストモデルが画像対応モデルも兼ねるときは 1 件で、kind は text_vision(#888)。
    # 選ぶと回答のモデルになる。
    monkeypatch.setattr(search_route, "enterprise_ai_vision_model_id", lambda _s: "text-m")
    models = client.get("/api/search/models").json()["data"]
    assert [(m["model_id"], m["kind"]) for m in models] == [("text-m", "text_vision")]
    answer_models.clear()
    response = client.post("/api/search", json={"query": "承認条件", "model_id": "text-m"})
    assert response.status_code == 200
    assert answer_models == ["text-m"]


def test_search_api_accepts_and_ignores_removed_standard_options(
    monkeypatch: MonkeyPatch,
) -> None:
    """旧 standard の指定(mode など。#595 で削除)は 422 にせず、読み捨てて検索する。"""
    observed: list[SearchRequest] = []

    class CapturingPipeline:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def run(
            self, request: SearchRequest, trace_id: str | None = None, **_kwargs: object
        ) -> SearchResponse:
            observed.append(request)
            return SearchResponse(answer="回答", trace_id=trace_id or "trace", elapsed_ms=1.0)

    monkeypatch.setattr(search_route, "RagPipeline", CapturingPipeline)

    response = client.post(
        "/api/search",
        json={
            "query": "承認条件",
            "top_k": 3,
            "rerank_top_n": 50,
            "mode": "graph",
            "strategy": "graph_global",
            "generation_profile": "structured_json",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["answer"] == "回答"
    dumped = observed[0].model_dump()
    assert dumped["top_k"] == 3
    for removed in ("rerank_top_n", "mode", "strategy", "generation_profile"):
        assert removed not in dumped


def test_search_request_top_k_defaults_to_50_and_caps_at_100() -> None:
    """検索する件数の既定は 50、上限は 100（省略時は現在の設定で解決。#3679）。"""
    assert SearchRequest(query="q").top_k == 50
    assert SearchRequest(query="q", top_k=100).top_k == 100
    for invalid in (0, 101):
        with pytest.raises(ValidationError):
            SearchRequest(query="q", top_k=invalid)


def test_stream_unexpected_failure_logs_traceback_and_hides_detail(
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
) -> None:
    """stream 中の予期しない失敗は traceback をログに残し、利用者へは定型文だけを返す（#285）。"""

    class BrokenPipeline:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def run(self, *_args: object, **_kwargs: object) -> SearchResponse:
            raise RuntimeError("oracle dsn=secret-host")

    monkeypatch.setattr(search_route, "RagPipeline", BrokenPipeline)

    with caplog.at_level(logging.ERROR, logger=search_route.__name__):
        response = client.post("/api/search/stream", json={"query": "承認条件"})

    assert response.status_code == 200
    assert "event: error" in response.text
    assert search_route.STREAM_ERROR_MESSAGE in response.text
    assert "secret-host" not in response.text
    records = [r for r in caplog.records if r.getMessage() == "rag_search_stream_failed"]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert records[0].__dict__["exception_type"] == "RuntimeError"


def test_stream_search_api_buffers_answer_after_answer_check(
    monkeypatch: MonkeyPatch,
) -> None:
    """検査前の token callback を backend から渡さず、回答全体を 1 回で送る。"""
    monkeypatch.setattr(search_route, "RagPipeline", RealtimeStreamingPipeline)

    response = client.post("/api/search/stream", json={"query": "承認条件"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("event: delta") == 1
    assert '{"text": "承認条件は 120000 円です。"}' in response.text
    assert "event: metadata" in response.text
    assert '"retrieval_strategy": "hybrid"' in response.text
    assert '"confidence": "high"' in response.text
    assert '"retrieval_breakdown"' not in response.text
    assert "event: citations" in response.text
    assert "event: done" in response.text


def test_stream_search_api_never_emits_precheck_answer_bytes(
    monkeypatch: MonkeyPatch,
) -> None:
    """生回答を送らず、最終マスク済み回答だけを delta 化する。"""
    monkeypatch.setattr(search_route, "RagPipeline", RealtimeMaskingPipeline)

    response = client.post("/api/search/stream", json={"query": "口座番号"})

    assert response.status_code == 200
    assert "1234567" not in response.text
    assert "event: replace" not in response.text
    assert '{"text": "口座番号は [機微情報] です。"}' in response.text
    assert response.text.count("event: delta") == 1


def test_search_api_hashes_tenant_and_user_headers_into_audit(
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
) -> None:
    """HTTP header の tenant/user id は hash として RAG 監査へ相関される。"""
    monkeypatch.setattr(search_route, "RagPipeline", AuditingPipeline)

    with caplog.at_level(logging.INFO, logger="app.audit"):
        response = client.post(
            "/api/search",
            json={"query": "存在しない社内規程"},
            headers={
                "X-Tenant-ID": "tenant-a",
                "X-User-ID": "user@example.com",
            },
        )

    assert response.status_code == 200
    audit_record = next(record for record in caplog.records if record.message == "rag_search_audit")
    audit_event = cast(Any, audit_record).audit_event
    assert audit_event["request_id"] == response.headers["x-request-id"]
    assert len(audit_event["tenant_id_hash"]) == 64
    assert len(audit_event["user_id_hash"]) == 64
    assert "tenant-a" not in str(audit_event)
    assert "user@example.com" not in str(audit_event)


def test_search_api_records_knowledge_base_scope_in_audit(
    monkeypatch: MonkeyPatch,
    caplog: LogCaptureFixture,
) -> None:
    """検索 API の KB スコープは diagnostics と audit に残る。"""
    monkeypatch.setattr(search_route, "RagPipeline", AuditingPipeline)

    with caplog.at_level(logging.INFO, logger="app.audit"):
        response = client.post(
            "/api/search",
            json={
                "query": "存在しない社内規程",
                "knowledge_base_ids": ["kb-1", "kb-2"],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["diagnostics"]["knowledge_base_count"] == 2
    audit_record = next(record for record in caplog.records if record.message == "rag_search_audit")
    audit_event = cast(Any, audit_record).audit_event
    assert audit_event["knowledge_base_ids"] == ["kb-1", "kb-2"]


def test_search_request_accepts_chunk_metadata_filters() -> None:
    """構造化 chunk metadata filter は検索リクエストとして受け付ける。"""
    request = SearchRequest(
        query="料金表",
        filters={
            "content_kind": "figure",
            "section_title": "料金",
            "section_path": "経費申請",
            "source_acl": "support",
            "document_version": "2024.05",
        },
    )

    assert request.filters == {
        "content_kind": "figure",
        "section_title": "料金",
        "section_path": "経費申請",
        "source_acl": "support",
        "document_version": "2024.05",
    }


def test_search_request_normalizes_content_kind_filter_case() -> None:
    """content_kind filter は API 利用者の大小文字揺れを低 cardinality 値へ寄せる。"""
    request = SearchRequest(query="料金表", filters={"content_kind": " Table "})

    assert request.filters == {"content_kind": "table"}


def test_search_request_rejects_unknown_content_kind_filter() -> None:
    """未知の content_kind は空振りではなく 422 相当の検証エラーにする。"""
    with pytest.raises(ValidationError, match="未対応の内容種別フィルターです"):
        SearchRequest(query="料金表", filters={"content_kind": "chart"})


def test_search_request_accepts_synthetic_content_kind_filters() -> None:
    """取込機能が生む合成 chunk(抽出項目/章節要約)も filter に指定できる。"""
    for kind in ("field", "section_summary"):
        request = SearchRequest(query="料金表", filters={"content_kind": kind})
        assert request.filters == {"content_kind": kind}


def test_search_request_normalizes_knowledge_base_ids() -> None:
    """knowledge_base_ids は重複排除され、既存 filters 経路にも同期される。"""
    request = SearchRequest(
        query="料金表",
        knowledge_base_ids=[" kb-1 ", "kb-2", "kb-1"],
    )

    assert request.knowledge_base_ids == ["kb-1", "kb-2"]
    assert request.filters["knowledge_base_id"] == "kb-1,kb-2"


def test_search_request_accepts_legacy_knowledge_base_filter() -> None:
    """既存 filters.knowledge_base_id 指定も新しい配列 field へ反映する。"""
    request = SearchRequest(
        query="料金表",
        filters={"knowledge_base_id": "kb-1, kb-2"},
    )

    assert request.knowledge_base_ids == ["kb-1", "kb-2"]
    assert request.filters["knowledge_base_id"] == "kb-1,kb-2"


def test_search_request_rejects_conflicting_knowledge_base_scope() -> None:
    """配列 field と legacy filter が食い違う場合は曖昧に検索しない。"""
    with pytest.raises(ValidationError, match="knowledge_base_ids"):
        SearchRequest(
            query="料金表",
            filters={"knowledge_base_id": "kb-1"},
            knowledge_base_ids=["kb-2"],
        )


def _force_search_timeout(monkeypatch: MonkeyPatch) -> None:
    """検索 route を低い回答生成の上限 + 遅い pipeline に差し替える。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_answer_timeout_seconds", 0.05)
    monkeypatch.setattr(search_route, "RagPipeline", SlowPipeline)


class SlowPipeline:
    """timeout を再現するテスト用 pipeline。"""

    def __init__(self, *, settings: object | None = None, **_kwargs: object) -> None:
        self._settings = settings

    async def run(
        self,
        _request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: Any | None = None,
        token_callback: object | None = None,
        trace: Any | None = None,
    ) -> SearchResponse:
        _ = token_callback
        assert trace_id
        assert progress_callback is not None
        # 回答フロー（LLM）の途中で時間切れになる（処理の経過は実行中の段の span。#3226）。
        with answer_trace(trace, trace_id=trace_id):
            await pipeline_stage(
                "answer", asyncio.sleep(1), trace_id=trace_id, progress_callback=progress_callback
            )
        raise AssertionError("timeout 前に完了しない")


class RealtimeStreamingPipeline:
    """route が token_callback を渡さないことを確認するテスト用 pipeline。"""

    def __init__(self, *, settings: object | None = None, **_kwargs: object) -> None:
        self._settings = settings

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: object | None = None,
        token_callback: Any | None = None,
        trace: Any | None = None,
    ) -> SearchResponse:
        _ = progress_callback
        assert trace_id
        assert token_callback is None
        return SearchResponse(
            answer="承認条件は 120000 円です。",
            citations=[],
            trace_id=trace_id,
            elapsed_ms=1.0,
            diagnostics=build_search_diagnostics(
                request,
                settings=get_settings(),
                retrieval_strategy_adapter="grounded",
                answer={"confidence": "high", "answer_flow": "crag"},
            ),
        )


class RealtimeMaskingPipeline:
    """検査前本文と最終マスク本文を区別するテスト用 pipeline。"""

    def __init__(self, *, settings: object | None = None, **_kwargs: object) -> None:
        self._settings = settings

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: object | None = None,
        token_callback: Any | None = None,
        trace: Any | None = None,
    ) -> SearchResponse:
        _ = progress_callback
        assert trace_id
        assert token_callback is None
        return SearchResponse(
            answer="口座番号は [機微情報] です。",
            citations=[],
            trace_id=trace_id,
            elapsed_ms=1.0,
            diagnostics=build_search_diagnostics(
                request, settings=get_settings(), retrieval_strategy_adapter="grounded"
            ),
        )


class AuditingPipeline:
    """監査ログだけを記録して空検索結果を返すテスト用 pipeline。"""

    def __init__(self, *, settings: object | None = None, **_kwargs: object) -> None:
        self._settings = settings

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: object | None = None,
        token_callback: object | None = None,
        trace: Any | None = None,
    ) -> SearchResponse:
        _ = progress_callback, token_callback
        assert trace_id
        diagnostics = build_search_diagnostics(
            request, settings=get_settings(), retrieval_strategy_adapter="grounded"
        )
        record_rag_search_audit(
            trace_id=trace_id,
            outcome="no_results",
            query=request.query,
            filters=request.filters,
            retrieved_count=0,
            citations=[],
            elapsed_ms=1.0,
            diagnostics=diagnostics,
        )
        return SearchResponse(
            answer="該当する文書は見つかりませんでした。",
            citations=[],
            trace_id=trace_id,
            elapsed_ms=1.0,
            diagnostics=diagnostics,
        )
