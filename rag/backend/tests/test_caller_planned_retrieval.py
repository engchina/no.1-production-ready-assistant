"""呼び出し元（業務 Agent）が検索文を作る根拠の取得（planning=caller。#2651）のテスト。

1 回の取得で、RAG 側の推論用の LLM / VLM（検索戦略の判定・質問拡張・画面目録の
連携・抽出項目の条件の読み取り・CRAG・回答の生成と監査・根拠の画像の添付）が呼ばれないことを、
呼ばれたら失敗するスタブで確かめる。embedding と rerank は別に数え、検索の品質を作る工程
（hybrid search・rerank・前後の文脈）は通常の検索と同じに動くことを確かめる。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app.config import EnterpriseAiConfiguredModel, Settings
from app.rag.answer_engine import AnswerEngine
from app.rag.pipeline import RagPipeline
from app.schemas.search import RetrievedChunk, SearchMode, SearchRequest
from tests.test_answer_engine import FakeGenAi, FakeOracle

# 主の検索文に無い業務 ID を含む検索文（前の根拠で分かった ID で引く多段の検索）。RAG の LLM の
# 言い換えなら外す照合に掛かるが、呼び出し元の検索文は外さない。
CALLER_QUERIES = ["SYS-101 担当部署", "受注 登録 手順"]


# 呼ばれた推論用の LLM / VLM（失敗を縮退で飲み込む工程があるので、例外だけでなく記録でも確かめる）。
GENERATION_CALLS: list[str] = []


def _fail_generation(*args: Any, **kwargs: Any) -> Any:
    schema = args[3] if len(args) > 3 else kwargs.get("schema")
    GENERATION_CALLS.append(getattr(schema, "__name__", str(schema)))
    raise AssertionError("推論用の LLM / VLM が呼ばれた")


class FailingLlm:
    """pipeline の LLM（会話の書き換え・抽出項目の条件の読み取り）。呼ばれたら失敗する。"""

    def __getattr__(self, name: str) -> Any:
        return _fail_generation


class CountingGenAi(FakeGenAi):
    """embedding と rerank の呼び出しを数える（推論用の LLM ではない）。"""

    def __init__(self) -> None:
        self.embed_calls = 0
        self.rerank_calls = 0

    async def embed(
        self, texts: list[str], *, input_type: str = "SEARCH_DOCUMENT"
    ) -> list[list[float]]:
        self.embed_calls += 1
        return await super().embed(texts, input_type=input_type)

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        self.rerank_calls += 1
        return await super().rerank(query, documents, top_n)


class QueryOracle(FakeOracle):
    def __init__(self) -> None:
        super().__init__()
        self.queries: list[str] = []

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        self.queries.append(query)
        return await super().hybrid_search(query, embedding, top_k, mode, filters)


def _reasoning_settings() -> Settings:
    """質問に応じた推論の工程をすべて有効にした設定（caller ではどれも使わないことを確かめる）。"""
    return Settings(
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        rag_query_strategy="auto_routing",
        rag_answer_flow="crag",
        rag_screen_linking_enabled=True,
        rag_auto_field_filter_enabled=True,
        # 回答のモデルが画像を読めても、回答を作らない検索では根拠の画像を切り出さない（#3444）。
        oci_enterprise_ai_models=[
            EnterpriseAiConfiguredModel(model_id="vlm", display_name="VLM", vision_enabled=True)
        ],
        oci_enterprise_ai_default_text_model="vlm",
    )


async def _fail_source(oracle: object, document_id: str) -> bytes:
    raise AssertionError("回答を作らない検索では根拠の画像を切り出さない")


@pytest.fixture
def no_generation(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    import rag_engine.adapters.oci as engine_oci

    GENERATION_CALLS.clear()
    monkeypatch.setattr(engine_oci, "parse_text_response", _fail_generation)
    monkeypatch.setattr(engine_oci, "parse_multimodal_response", _fail_generation)
    # 根拠の画像を切り出す原本の読み込み（回答を作らない検索では行わない。#3444）。
    monkeypatch.setattr("app.rag.answer_engine.load_parsed_source", _fail_source)
    yield GENERATION_CALLS
    GENERATION_CALLS.clear()


async def test_caller_planned_retrieval_calls_no_reasoning_model(
    no_generation: list[str],
) -> None:
    oracle = QueryOracle()
    genai = CountingGenAi()
    pipeline = RagPipeline(
        settings=_reasoning_settings(),
        oracle=oracle,  # type: ignore[arg-type]
        genai=genai,  # type: ignore[arg-type]
        llm=FailingLlm(),  # type: ignore[arg-type]
        caller_queries=CALLER_QUERIES,
    )

    response = await pipeline.run(
        SearchRequest(
            query="受注 登録方法",
            search_answer_profile_id="sap-1",
            generate_answer=False,
            original_question="受注の登録方法と担当部署を教えてください",
        )
    )

    assert response.answer == ""
    assert no_generation == []
    # 呼び出し元の検索文は、主の検索文に無い ID を含んでも検索に使う。
    searched = set(oracle.queries)
    assert "受注 登録方法" in searched
    assert set(CALLER_QUERIES) <= searched
    # 検索の品質の工程は通常の検索と同じ（embedding・rerank・前後の文脈）。
    assert genai.embed_calls >= 1
    assert genai.rerank_calls >= 1
    roles = {citation.metadata.get("evidence_role") for citation in response.citations}
    assert "retrieved_anchor" in roles
    assert {citation.chunk_id for citation in response.citations} == {"doc-1:c1"}
    anchor = next(c for c in response.citations if c.chunk_id == "doc-1:c1")
    assert anchor.rerank_score is not None
    assert anchor.metadata.get("evidence_retrieval_rank") == 1
    # 実行記録の質問拡張の工程は、呼び出し元の検索文で LLM を使っていない。
    answer: dict[str, Any] = dict(response.diagnostics.answer or {})
    steps: list[dict[str, Any]] = list(answer.get("execution_steps") or [])
    assert steps
    assert all(not step.get("llm_calls") for step in steps)


async def test_rag_planned_retrieval_still_uses_query_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """planning=rag（既定）は今までどおり RAG が検索戦略の判定（LLM）を行う（意味を変えない）。"""
    import rag_engine.adapters.oci as engine_oci
    from rag_engine.models.llm import QueryRoutingOutput

    schemas: list[str] = []

    def routing_only(system: str, prompt: str, settings: Any, schema: type, **_: Any) -> Any:
        schemas.append(schema.__name__)
        if schema is QueryRoutingOutput:
            return QueryRoutingOutput(strategy="simple_retrieval", reason="単純", queries=[])
        raise AssertionError(f"unexpected schema: {schema.__name__}")

    monkeypatch.setattr(engine_oci, "parse_text_response", routing_only)
    engine = AnswerEngine(
        Settings(rag_query_strategy="auto_routing"),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )

    outcome = await engine.run(SearchRequest(query="受注の登録方法は？", generate_answer=False))

    assert outcome.citations
    assert schemas == ["QueryRoutingOutput"]


async def test_caller_queries_are_only_for_retrieval_without_answer(
    no_generation: list[str],
) -> None:
    pipeline = RagPipeline(
        settings=Settings(),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        llm=FailingLlm(),  # type: ignore[arg-type]
        caller_queries=[],
    )

    with pytest.raises(ValueError, match="回答を作らない検索"):
        await pipeline.run(SearchRequest(query="受注の登録方法は？"))


def test_trusted_queries_survive_the_goal_check_in_candidate_order() -> None:
    from rag_engine.generation.answering import _keep_trusted_queries

    candidates = ("原質問", "SYS-9 担当", "逸脱した言い換え", "語彙の補助")
    accepted = ("原質問", "語彙の補助")
    rejected = (
        {"query": "SYS-9 担当", "reason": "原質問・明示別名にない業務識別子"},
        {"query": "逸脱した言い換え", "reason": "目的の変化"},
    )

    kept, still_rejected = _keep_trusted_queries(candidates, accepted, rejected, ["sys-9  担当"])

    assert kept == ("原質問", "SYS-9 担当", "語彙の補助")
    assert still_rejected == (rejected[1],)
