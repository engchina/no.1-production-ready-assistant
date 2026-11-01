"""処理の経過の span の親子（#3475）。

工程の中の処理（検索・並べ替え・モデルの呼び出し）は、その工程の子。本物の `RagPipeline` →
`AnswerEngine` → rag_engine の回答フロー（注入した backend の検索・rerank）を通し、OCI・DB の
代役が本物と同じ名前の span を開く。span の木（親の名前）を確かめる。
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from pr_backend_core.chat_progress import fold_chat_progress_events
from pr_backend_core.observability.tracing import observed_span
from rag_engine.resources.tracing import observe_call

from app.config import Settings
from app.rag.pipeline import RagPipeline
from app.schemas.search import RetrievedChunk, SearchMode, SearchRequest
from tests.span_progress_support import RecordingTrace
from tests.test_answer_engine import FakeGenAi, FakeOracle, _fake_llm


class _TracedOracle(FakeOracle):
    """検索を DB の検索の span（本物の `OracleClient` の「ベクトル検索」と同じ位置）で包む代役。"""

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        with observed_span("synthetic.db_search"):
            return await super().hybrid_search(query, embedding, top_k, mode, filters)


class _TracedGenAi(FakeGenAi):
    """埋め込み・rerank を本物の `OciGenAiClient` と同じ名前の span で包む代役。"""

    async def embed(
        self, texts: list[str], *, input_type: str = "SEARCH_DOCUMENT"
    ) -> list[list[float]]:
        with observed_span("gen_ai.embeddings", kind="client"):
            return await super().embed(texts, input_type=input_type)

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        with observed_span("gen_ai.rerank", kind="client"):
            return await super().rerank(query, documents, top_n)


def _traced_llm(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
    # rag_engine の OCI の呼び出し（`adapters/oci.py`）と同じ観測。
    with observe_call("chat", "synthetic-model"):
        return _fake_llm(system, prompt, settings, schema, **options)


def _name(step: Any) -> str:
    params: Mapping[str, Any] = step.params or {}
    return str(params.get("name") or (step.span or {}).get("name") or step.step_id)


async def test_spans_are_nested_under_the_step_that_did_the_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rag_engine.adapters.oci as engine_oci

    monkeypatch.setattr(engine_oci, "parse_text_response", _traced_llm)
    pipeline = RagPipeline(
        # 質問の言い換えに LLM を使う戦略（元の質問の検索を並行に先読みする。#1499）。
        settings=Settings(
            rag_query_strategy="auto_routing",
            oci_enterprise_ai_endpoint="https://synthetic.test/v1",
        ),
        oracle=_TracedOracle(),  # type: ignore[arg-type]
        genai=_TracedGenAi(),  # type: ignore[arg-type]
    )
    tracker = RecordingTrace()

    await pipeline.run(SearchRequest(query="受注の登録方法は？"), trace=tracker)

    steps = fold_chat_progress_events(tracker.events).step_list()
    by_id = {step.step_id: step for step in steps}

    def parents(span_name: str) -> list[str]:
        return [
            _name(by_id[str((step.span or {}).get("parent_id"))])
            for step in steps
            if (step.span or {}).get("name") == span_name
        ]

    def parent_of(step_name: str) -> str:
        step = next(step for step in steps if _name(step).startswith(step_name))
        return _name(by_id[str((step.span or {}).get("parent_id"))])

    # 並べ替えのモデルの呼び出しは「検索結果の並べ替え」（Rerank）の子。
    assert parents("gen_ai.rerank")
    assert set(parents("gen_ai.rerank")) == {"Rerank"}
    # 並べ替え・文書の絞り込み・起点の上限は、文書の検索の中の別の工程（兄弟）。
    assert parent_of("Rerank") == "文書検索"
    assert parent_of("文書ごとの起点の上限") == "文書検索"
    # 元の質問の先読み（埋め込み・DB の検索）は、言い換えの LLM と並行に読む「質問の言い換え」の子。
    # 全体の工程（質問の読み取りから回答の作成まで）の直下に出さない。
    db_parents = parents("synthetic.db_search")
    assert db_parents
    assert set(db_parents) <= {"質問拡張戦略", "文書検索", "前後の文脈の読み込み"}
    assert "質問拡張戦略" in db_parents
    assert set(parents("gen_ai.embeddings")) <= {"質問拡張戦略", "文書検索"}
    # モデルの呼び出しは、呼び出した工程の子（言い換えの判定・回答の作成と確認の回）。
    chat_parents = parents("gen_ai.chat")
    assert "質問拡張戦略" in chat_parents
    assert any(parent.startswith("回答文の生成と根拠確認") for parent in chat_parents)
    assert "rag.answer" not in {*db_parents, *chat_parents}


async def test_attachment_evidence_is_its_own_pipeline_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """会話の添付の根拠の選択（添付の読み込みと rerank）は、全体の直下ではなく自分の段の子。"""
    import rag_engine.adapters.oci as engine_oci

    from app.rag import chat_attachments

    async def attachment_evidence(question: str, conversation_id: str, **_: Any) -> list[Any]:
        with observed_span("gen_ai.rerank", kind="client"):
            return []

    monkeypatch.setattr(chat_attachments, "attachment_evidence", attachment_evidence)
    monkeypatch.setattr(
        chat_attachments,
        "chat_attachment_service",
        lambda: SimpleNamespace(reading_status=lambda: "done"),
    )
    monkeypatch.setattr(engine_oci, "parse_text_response", _traced_llm)
    pipeline = RagPipeline(
        settings=Settings(),
        oracle=_TracedOracle(),  # type: ignore[arg-type]
        genai=_TracedGenAi(),  # type: ignore[arg-type]
        attachment_conversation_id="conversation-1",
    )
    tracker = RecordingTrace()

    await pipeline.run(SearchRequest(query="受注の登録方法は？"), trace=tracker)

    steps = fold_chat_progress_events(tracker.events).step_list()
    by_id = {step.step_id: step for step in steps}
    rerank = next(step for step in steps if (step.span or {}).get("name") == "gen_ai.rerank")
    parent = by_id[str((rerank.span or {}).get("parent_id"))]
    assert (parent.span or {}).get("name") == "rag.attachment_evidence"


async def test_disabled_attachment_step_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    import rag_engine.adapters.oci as engine_oci
    from pr_system_settings.chat_attachments import (
        ChatAttachmentService,
        InMemoryChatAttachmentStore,
    )

    from app.rag import chat_attachments

    service = ChatAttachmentService(
        InMemoryChatAttachmentStore(),
        product="rag",
        settings=lambda: SimpleNamespace(),
        enabled=lambda: False,
    )
    monkeypatch.setattr(chat_attachments, "chat_attachment_service", lambda: service)
    monkeypatch.setattr(engine_oci, "parse_text_response", _traced_llm)
    pipeline = RagPipeline(
        settings=Settings(),
        oracle=_TracedOracle(),  # type: ignore[arg-type]
        genai=_TracedGenAi(),  # type: ignore[arg-type]
        attachment_conversation_id="synthetic-conversation",
    )
    tracker = RecordingTrace()
    await pipeline.run(SearchRequest(query="合成の設定方法を確認する"), trace=tracker)
    steps = fold_chat_progress_events(tracker.events).step_list()
    attachment = [
        step for step in steps if (step.span or {}).get("name") == "rag.attachment_evidence"
    ]
    assert len(attachment) == 1
    assert attachment[0].status == "skipped"
