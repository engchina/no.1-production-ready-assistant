"""根拠 0 件で自動の検索条件を外しても、利用者が指定した条件・基準日・認可の範囲が残る(#3260)。

回答の検索の入口(``AnswerEngine.run``)から、自動の条件(質問から読み取った抽出項目の条件・質問が
名指しした文書名とページ)を外した後の検索(CRAG の補正検索を含む)と、実体・関係情報の拡張の範囲に、
利用者が指定した条件(文書名・分類・手の項目の条件・ナレッジベース・旧版の扱い)・基準日・認可の
範囲(監査 context の predicate)が残ることを確かめる。本文が明記した参照を辿る交差参照(#1280)は、
意図して文書名の絞り込みを越える。DB と LLM は呼ばない(決定論の代役。内容は合成)。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from typing import Any, cast

import pytest
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.models.llm import CragRetrievalGradeOutput

import app.rag.answer_engine as answer_engine
from app.clients.oracle import _oracle_retrieval_where
from app.config import Settings
from app.rag.answer_engine import AnswerEngine, _SearchState
from app.rag.cross_references import ReferenceTarget
from app.rag.request_context import (
    AuditRequestContext,
    current_audit_request_context,
    reset_audit_request_context,
    set_audit_request_context,
)
from app.schemas.search import ExtractionFieldCondition, RetrievedChunk, SearchMode, SearchRequest
from tests.test_answer_engine import CoverageOracle, FakeGenAi, FakeOracle, _fake_llm
from tests.test_cross_references import FakeGenAi as ReferenceGenAi
from tests.test_cross_references import ReferenceOracle, _retrieved

# 認可の範囲(文書・分類・ナレッジベース)。検索条件ではなく SQL の述語で当たる。
ACCESS = AuditRequestContext(
    request_id="request-3260",
    allowed_document_ids=frozenset({"doc-1", "doc-a", "doc-b"}),
    allowed_category_names=frozenset({"分類a"}),
    allowed_knowledge_base_ids=frozenset({"kb-1", "kb-a"}),
)
# 利用者が手で指定した項目の条件と、質問から読み取った(自動の)項目の条件。自動の条件の 1 つは
# 手の条件と同じ項目で別の値(手の条件を優先し、上書きしない)。
MANUAL_FIELD = ExtractionFieldCondition(name="区分", value_type="string", op="eq", value="甲")
AUTO_FIELD = ExtractionFieldCondition(name="金額", value_type="number", op="gte", value="1")
AUTO_SAME_FIELD = ExtractionFieldCondition(name="区分", value_type="string", op="eq", value="乙")


def _manual_fields() -> str:
    return json.dumps([MANUAL_FIELD.model_dump()], ensure_ascii=False, separators=(",", ":"))


def _field_names(filters: dict[str, str]) -> set[str]:
    return {item["name"] for item in json.loads(filters.get("extraction_fields") or "[]")}


def _field_values(filters: dict[str, str]) -> list[tuple[str, str]]:
    return [
        (item["name"], item["value"])
        for item in json.loads(filters.get("extraction_fields") or "[]")
    ]


def _access_binds(filters: dict[str, str]) -> dict[str, object]:
    """検索の SQL に渡る認可の範囲の bind(``_oracle_retrieval_where`` が監査 context から作る)。"""
    _sql, binds = _oracle_retrieval_where(dict(filters))
    return {key: value for key, value in binds.items() if key.startswith("access_")}


@pytest.fixture
def access() -> Iterator[AuditRequestContext]:
    token = set_audit_request_context(ACCESS)
    try:
        yield ACCESS
    finally:
        reset_audit_request_context(token)


class AutoBlockedOracle(FakeOracle):
    """自動の条件(質問のページ・文書名・読み取った項目)が残る間は 0 件を返す代役。

    検索ごとに、渡った条件と、その時点の監査 context(認可の範囲)を記録する。
    """

    def __init__(self) -> None:
        super().__init__()
        self.contexts: list[AuditRequestContext] = []
        self.queries: list[str] = []

    async def has_retrieval_chunks(self, filters: dict[str, str]) -> bool:
        # 質問が名指しした文書名は範囲にある(自動の文書名の条件を足す)。
        self.probes.append(dict(filters))
        return True

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        scoped = dict(filters or {})
        self.filters.append(scoped)
        self.queries.append(query)
        self.contexts.append(current_audit_request_context())
        if (
            scoped.get("page_number_min") == "12"
            or scoped.get("file_name") == "sample.pdf"
            or AUTO_FIELD.name in _field_names(scoped)
        ):
            return []
        return self.chunks[:1]


class PlanRecorder:
    """実体・関係情報の拡張の計画の代役。渡された範囲と監査 context を記録し、何も足さない。"""

    def __init__(self) -> None:
        self.scopes: list[dict[str, str]] = []
        self.contexts: list[AuditRequestContext] = []

    async def __call__(self, store: Any, filters: dict[str, str], **_: Any) -> list[Any]:
        self.scopes.append(dict(filters))
        self.contexts.append(current_audit_request_context())
        return []


@pytest.fixture
def plans(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[PlanRecorder, PlanRecorder]]:
    entity, graph = PlanRecorder(), PlanRecorder()
    monkeypatch.setattr(answer_engine, "plan_entity_expansion", entity)
    monkeypatch.setattr(answer_engine, "plan_graph_expansion", graph)
    yield entity, graph


def _crag_llm(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
    """CRAG の評価で「不足」とし、補正検索を 1 回させる(外した後の検索も確かめる)。"""
    if schema is CragRetrievalGradeOutput:
        return CragRetrievalGradeOutput(
            reason="後続の手順が不足",
            sufficient=False,
            confidence=0.2,
            rewritten_query="登録の手順",
            # 補正検索に渡す認可の範囲を試すため、意味評価の代役が明確な不足を返す。
            # 空の観点は未検証であり、補正検索を要求する入力には使わない。
            aspect_checks=[
                {
                    "aspect": "procedure",
                    "status": "missing",
                    "source_ids": [],
                    "reason": "登録後の確認手順の説明が取得できていない。",
                }
            ],
        )
    return _fake_llm(system, prompt, settings, schema, **options)


def _engine(oracle: FakeOracle, **settings: Any) -> AnswerEngine:
    return AnswerEngine(
        Settings(
            rag_answer_flow="crag",
            rag_reference_expansion_enabled=True,
            rag_entity_expansion_enabled=True,
            rag_graph_expansion_enabled=True,
            **settings,
        ),
        oracle=cast(Any, oracle),
        genai=cast(Any, FakeGenAi()),
        auto_field_conditions=[AUTO_SAME_FIELD, AUTO_FIELD],
        entity_store=cast(Any, object()),
        graph_store=cast(Any, object()),
    )


@pytest.mark.parametrize(
    ("question", "user_filters", "relaxed"),
    [
        (
            # 利用者が文書名を指定したので、質問の文書名・ページは足さない(外すのは項目だけ)。
            "sample.pdf の12ページの登録手順は？",
            {
                "knowledge_base_id": "kb-1",
                "file_name": "manual",
                "large_category": "分類A",
                "include_superseded": "true",
                "as_of": "2020-06-30",
            },
            ["auto_field"],
        ),
        (
            # 利用者は文書名を指定しない。質問の文書名・ページと項目を、項目 → ページ → 文書名の
            # 順に外す。
            "sample.pdf の12ページの登録手順は？",
            {"knowledge_base_id": "kb-1", "middle_category": "分類B"},
            ["auto_field", "question_page", "question_file"],
        ),
        (
            # 利用者がページを指定したので、質問のページは足さない(質問の文書名と項目を外す)。
            "sample.pdf の12ページの登録手順は？",
            {"knowledge_base_id": "kb-1", "page_number_min": "1", "page_number_max": "3"},
            ["auto_field", "question_file"],
        ),
    ],
    ids=["user-file-name", "question-file-name", "user-page"],
)
async def test_relaxation_keeps_user_conditions_as_of_and_access_in_every_search(
    monkeypatch: pytest.MonkeyPatch,
    access: AuditRequestContext,
    plans: tuple[PlanRecorder, PlanRecorder],
    question: str,
    user_filters: dict[str, str],
    relaxed: list[str],
) -> None:
    """自動の条件を外した後のすべての検索と、実体・関係情報の拡張に、利用者の条件・基準日・認可が残る。"""
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _crag_llm)
    monkeypatch.setattr(answer_engine, "_today", lambda: date(2026, 4, 1))
    oracle = AutoBlockedOracle()
    filters = {**user_filters, "extraction_fields": _manual_fields()}
    request = SearchRequest(query=question, filters=filters)

    outcome = await _engine(oracle).run(request)

    # 利用者の条件 + 回答の初めに決めた基準日(指定があればその日)。
    expected = {"as_of": "2026-04-01", **filters}
    assert outcome.diagnostics["filter_relaxation"]["relaxed"] == relaxed
    assert outcome.citations
    # 最初の検索は自動の条件を足し、利用者が手で指定した項目の条件は自動の条件で上書きしない。
    first = oracle.filters[0]
    assert _field_values(first) == [
        (MANUAL_FIELD.name, MANUAL_FIELD.value),
        (AUTO_FIELD.name, AUTO_FIELD.value),
    ]
    # 外した後の検索(外し終えた検索と、CRAG の補正検索)は、利用者の条件だけ(自動の条件はすべて
    # 外れ、利用者の値はそのまま)。外した後に自動の条件が戻ることもない。
    start = oracle.filters.index(expected)
    searches_after = oracle.filters[start:]
    assert all(item == expected for item in searches_after)
    # CRAG の補正検索(評価が書き直した検索文)も、外した後の条件で検索する。
    assert "登録の手順" in oracle.queries[start:]
    # どの検索にも、利用者の条件・基準日が残る(外す段の途中も含む)。
    for item in oracle.filters:
        assert {key: item.get(key) for key in expected if key != "extraction_fields"} == {
            key: value for key, value in expected.items() if key != "extraction_fields"
        }
        assert (MANUAL_FIELD.name, MANUAL_FIELD.value) in _field_values(item)
    # 実体・関係情報の拡張は、主の検索と同じ利用者の範囲の中で広げる(文書名・分類・項目も残す)。
    entity, graph = plans
    assert entity.scopes and graph.scopes
    assert all(scope == expected for scope in [*entity.scopes, *graph.scopes])
    # 認可の範囲は、すべての検索と拡張で同じ監査 context の述語で当たる。
    assert oracle.contexts
    assert set(oracle.contexts) == {access}
    assert set(entity.contexts) == set(graph.contexts) == {access}
    # その context で組む検索の SQL は、文書・分類・ナレッジベースの認可の述語を持つ。
    assert {"access_document_id_0", "access_category_name_0", "access_knowledge_base_id_0"} <= set(
        _access_binds(searches_after[-1])
    )


class ContextReferenceOracle(ReferenceOracle):
    """交差参照の代役。参照先を読むときの監査 context(認可の範囲)も記録する。"""

    def __init__(self, hits: list[RetrievedChunk], sections: list[RetrievedChunk]) -> None:
        super().__init__(hits, sections)
        self.contexts: list[AuditRequestContext] = []

    async def retrieval_reference_documents(
        self, filters: dict[str, str], *, title: str, limit: int
    ) -> list[tuple[str, str, tuple[str, ...]]]:
        self.contexts.append(current_audit_request_context())
        return await super().retrieval_reference_documents(filters, title=title, limit=limit)

    async def retrieval_reference_chunks(
        self,
        filters: dict[str, str],
        *,
        document_id: str,
        section_path: str,
        chunk_set_id: str | None = None,
        limit: int,
    ) -> list[RetrievedChunk]:
        self.contexts.append(current_audit_request_context())
        return await super().retrieval_reference_chunks(
            filters,
            document_id=document_id,
            section_path=section_path,
            chunk_set_id=chunk_set_id,
            limit=limit,
        )


def _reference_corpus() -> ContextReferenceOracle:
    """利用者が指定した文書名の文書 A の候補が、本文で文書 B の節を明記して参照する。"""
    target = ReferenceTarget("「手順」", "title", "手順", "合成文書B")
    anchor = _retrieved(
        "doc-a:cs-1:0",
        "合成文書Bの「手順」を参照。",
        "第1章 概要",
        document_id="doc-a",
        file_name="合成文書A.pdf",
        targets=[target],
    )
    referenced = _retrieved(
        "doc-b:cs-9:0",
        "合成の手順の本文。",
        "第2章 手順",
        document_id="doc-b",
        file_name="合成文書B.pdf",
        chunk_set_id="cs-9",
    )
    return ContextReferenceOracle([anchor], [referenced])


async def test_reference_expansion_crosses_the_file_name_but_entity_and_graph_do_not(
    access: AuditRequestContext, plans: tuple[PlanRecorder, PlanRecorder]
) -> None:
    """本文が明記した参照は利用者の文書名の外へも辿る(#1280)。実体・関係情報の拡張は文書名を残す。

    どちらの拡張も、ナレッジベース・旧版の扱い・基準日と認可の範囲は残す。
    """
    oracle = _reference_corpus()
    engine = AnswerEngine(
        Settings(
            rag_reference_expansion_enabled=True,
            rag_entity_expansion_enabled=True,
            rag_graph_expansion_enabled=True,
        ),
        oracle=cast(Any, oracle),
        genai=cast(Any, ReferenceGenAi()),
        entity_store=cast(Any, object()),
        graph_store=cast(Any, object()),
    )
    filters = {
        "knowledge_base_id": "kb-a",
        "file_name": "合成文書A",
        "large_category": "分類A",
        "include_superseded": "true",
        "as_of": "2020-06-30",
    }
    request = SearchRequest(query="合成の手順", filters=filters)

    result = await engine._search(request, _SearchState(), retrieval_queries=["合成の手順"])

    # 参照先(文書名の外の文書 B)を辿って足し、参照先を探す範囲は文書名・分類の絞り込みを外す。
    assert "doc-b:cs-9:0" in [child.chunk_uid for child in result.child_chunks]
    reference_filters = [
        *(call["filters"] for call in oracle.document_batch_calls),
        *(call["filters"] for call in oracle.reference_calls),
    ]
    assert reference_filters
    kept = {"knowledge_base_id": "kb-a", "include_superseded": "true", "as_of": "2020-06-30"}
    assert all(item == kept for item in reference_filters)
    # 実体・関係情報の拡張は、利用者が指定した文書名・分類を残す。
    entity, graph = plans
    assert entity.scopes == [filters]
    assert graph.scopes == [filters]
    # 認可の範囲は、参照を辿るときも同じ監査 context の述語で当たる。
    assert oracle.contexts
    assert set(oracle.contexts) == {access}
    assert set(entity.contexts) == set(graph.contexts) == {access}


class RelaxedCoverageOracle(CoverageOracle):
    """要求ごとの再検索の代役(#1279)。自動の項目の条件が残る間は 0 件を返し、条件を記録する。"""

    def __init__(self) -> None:
        super().__init__()
        self.searches: list[tuple[str, dict[str, str], AuditRequestContext]] = []

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        scoped = dict(filters or {})
        self.searches.append((query, scoped, current_audit_request_context()))
        if AUTO_FIELD.name in _field_names(scoped):
            return []
        return await super().hybrid_search(query, embedding, top_k, mode, scoped)


async def test_request_coverage_re_search_keeps_user_conditions_after_relaxation(
    monkeypatch: pytest.MonkeyPatch, access: AuditRequestContext
) -> None:
    """自動の条件を外した後の、根拠の無い要求だけの再検索も、利用者の条件・基準日・認可で検索する。"""
    import rag_engine.adapters.model_api as engine_api
    from rag_engine.models.llm import GroundedDraft

    def explicit_gap(system: str, prompt: str, config: Any, schema: type, **options: Any) -> Any:
        result = _fake_llm(system, prompt, config, schema, **options)
        if (
            schema in (GroundedDraft, generation_draft_wire_model())
            and "請求書の再発行は、請求一覧" not in prompt
        ):
            data = result.model_dump(exclude_unset=True)
            data["items"].append(
                {
                    "kind": "gap",
                    "text": "追加操作の根拠がありません。",
                    "request_id": "Q2",
                    "citations": [],
                }
            )
            return GroundedDraft.model_validate(data)
        return result

    monkeypatch.setattr(engine_api, "parse_text_response", explicit_gap)
    oracle = RelaxedCoverageOracle()
    filters = {
        "knowledge_base_id": "kb-1",
        "large_category": "分類A",
        "as_of": "2020-06-30",
        "extraction_fields": _manual_fields(),
    }
    engine = AnswerEngine(
        Settings(
            rag_query_strategy="simple_retrieval",
            rag_answer_audit_mode="none",
            rag_answer_flow="standard_rag",
            rag_rerank_enabled=False,
            rag_request_coverage_retrieval_enabled=True,
        ),
        oracle=cast(Any, oracle),
        genai=cast(Any, FakeGenAi()),
        auto_field_conditions=[AUTO_FIELD],
    )

    outcome = await engine.run(
        SearchRequest(query="受注の登録方法は？請求書の再発行方法は？", filters=filters)
    )

    assert outcome.diagnostics["filter_relaxation"]["relaxed"] == ["auto_field"]
    assert (
        outcome.diagnostics["request_coverage"]["after_generation"][0]["re_retrieval"]["applied"]
        is True
    )
    re_searches = [item for item in oracle.searches if item[0] == "請求書の再発行方法は？"]
    assert re_searches
    assert all(scoped == filters for _query, scoped, _context in re_searches)
    assert {context for _query, _scoped, context in oracle.searches} == {access}
