"""検索の基準日(as_of)を、主検索と交差参照・実体・関係情報の拡張で同じにする(#2602)。

有効期間の判定は ``OracleClient`` の述語(開始日 <= 基準日 < 終了日。終了日は排他的。基準日が無ければ
今日)で行う。代役の Oracle は、実際の SQL の bind(``_oracle_retrieval_where`` の ``filter_as_of``)を
読んで同じ判定をする。DB と LLM は呼ばない(実 Oracle での確認は別)。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from typing import Any, cast

import pytest

import app.clients.oracle as oracle_module
import app.rag.answer_engine as answer_engine
from app.clients.oracle import _oracle_retrieval_where
from app.config import Settings
from app.rag.answer_engine import AnswerEngine, _SearchState, _with_effective_as_of
from app.rag.cross_references import ReferenceTarget
from app.schemas.search import RetrievedChunk, SearchMode, SearchRequest
from tests.test_cross_references import FakeGenAi, ReferenceOracle, _retrieved

# 文書ごとの有効期間(開始日, 終了日)。None は期間の端が無い。
PERIODS: dict[str, tuple[str | None, str | None]] = {
    "doc-a": (None, None),
    "doc-b": ("2020-01-01", "2021-01-01"),
}


def _bound_as_of(filters: dict[str, str]) -> str:
    """検索の SQL に渡る基準日(``OracleClient`` が bind する値)。"""
    _sql, binds = _oracle_retrieval_where(dict(filters))
    return str(binds["filter_as_of"])


def _effective(filters: dict[str, str], document_id: str) -> bool:
    """``OracleClient`` の有効期間の述語と同じ判定。"""
    as_of = _bound_as_of(filters)
    start, end = PERIODS[document_id]
    return (start or as_of) <= as_of < (end or "9999-12-31")


class PeriodOracle(ReferenceOracle):
    """文書の有効期間で絞る代役。主検索と参照先の読み取りの条件を記録する。"""

    def __init__(self, hits: list[RetrievedChunk], sections: list[RetrievedChunk]) -> None:
        super().__init__(hits, sections)
        self.main_calls: list[dict[str, str]] = []
        self.category_calls: list[dict[str, str]] = []

    def _in_scope(self, filters: dict[str, str], chunk: RetrievedChunk) -> bool:
        return super()._in_scope(filters, chunk) and _effective(filters, chunk.document_id)

    async def retrieval_large_categories(self, filters: dict[str, str]) -> list[str]:
        self.category_calls.append(dict(filters))
        return []

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        scoped = dict(filters or {})
        if scoped.get("document_id") is not None:
            return await super().hybrid_search(query, embedding, top_k, mode, scoped)
        self.main_calls.append(scoped)
        return [hit for hit in self.hits if _effective(scoped, hit.document_id)][:top_k]

    def all_filters(self) -> list[dict[str, str]]:
        return [
            *self.main_calls,
            *(call["filters"] for call in self.document_calls),
            *(call["filters"] for call in self.document_batch_calls),
            *self.section_calls,
            *(call["filters"] for call in self.reference_calls),
        ]


class PlanRecorder:
    """実体・関係情報の拡張の計画の代役。渡された範囲を記録し、何も足さない。"""

    def __init__(self) -> None:
        self.scopes: list[dict[str, str]] = []

    async def __call__(self, store: Any, filters: dict[str, str], **_: Any) -> list[Any]:
        self.scopes.append(dict(filters))
        return []


@pytest.fixture
def plans(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[PlanRecorder, PlanRecorder]]:
    entity, graph = PlanRecorder(), PlanRecorder()
    monkeypatch.setattr(answer_engine, "plan_entity_expansion", entity)
    monkeypatch.setattr(answer_engine, "plan_graph_expansion", graph)
    yield entity, graph


def _engine(oracle: PeriodOracle) -> AnswerEngine:
    return AnswerEngine(
        Settings(
            rag_reference_expansion_enabled=True,
            rag_entity_expansion_enabled=True,
            rag_graph_expansion_enabled=True,
        ),
        oracle=cast(Any, oracle),
        genai=cast(Any, FakeGenAi()),
        entity_store=cast(Any, object()),
        graph_store=cast(Any, object()),
    )


def _corpus() -> PeriodOracle:
    """期間の無い文書 A の候補が、期間のある文書 B の節を参照する。文書 B は主検索にも当たる。"""
    target = ReferenceTarget("「手順」", "title", "手順", "合成文書B")
    anchor = _retrieved(
        "doc-a:cs-1:0",
        "合成文書Bの「手順」を参照。",
        "第1章 概要",
        document_id="doc-a",
        file_name="合成文書A.pdf",
        targets=[target],
    )
    direct = _retrieved(
        "doc-b:cs-9:9",
        "合成の概要の本文。",
        "第1章 概要",
        document_id="doc-b",
        file_name="合成文書B.pdf",
        chunk_set_id="cs-9",
    )
    referenced = _retrieved(
        "doc-b:cs-9:0",
        "合成の手順の本文。",
        "第2章 手順",
        document_id="doc-b",
        file_name="合成文書B.pdf",
        chunk_set_id="cs-9",
    )
    return PeriodOracle([anchor, direct], [referenced])


@pytest.mark.parametrize(
    ("as_of", "effective"),
    [
        ("2019-12-31", False),
        ("2020-01-01", True),
        ("2020-06-30", True),
        ("2020-12-31", True),
        ("2021-01-01", False),
        ("2030-01-01", False),
    ],
    ids=["before-start", "start-day", "inside", "last-day", "end-day-exclusive", "future"],
)
async def test_main_search_and_expansions_use_the_same_as_of(
    plans: tuple[PlanRecorder, PlanRecorder], as_of: str, effective: bool
) -> None:
    """指定した基準日で、主検索と参照先の有効期間の判定が一致する(開始日は含み、終了日は含まない)。"""
    oracle = _corpus()
    request = SearchRequest(
        query="合成の手順",
        filters={
            "as_of": as_of,
            "knowledge_base_id": "kb-a",
            "include_superseded": "true",
            "file_name": "合成",
        },
    )

    result = await _engine(oracle)._search(
        request, _SearchState(), retrieval_queries=["合成の手順"]
    )

    ids = [child.chunk_uid for child in result.child_chunks]
    # 主検索の文書 B と、参照先の文書 B の節は、同じ基準日で同じに判定される。
    assert ("doc-b:cs-9:9" in ids) is effective
    assert ("doc-b:cs-9:0" in ids) is effective
    # 主検索と参照先の読み取りは、すべて指定した基準日で SQL を組む(今日で補わない)。
    assert {_bound_as_of(filters) for filters in oracle.all_filters()} == {as_of}
    assert oracle.reference_calls or not effective
    # 実体・関係情報の拡張の範囲は、利用者が指定した条件(文書名も)をすべて保つ(#3260)。
    entity, graph = plans
    assert entity.scopes == [request.filters]
    assert graph.scopes == [request.filters]
    # 交差参照は KB・旧版の扱い・基準日だけを保ち、文書名の絞り込みは外す(#1280)。
    expected = {"as_of": as_of, "knowledge_base_id": "kb-a", "include_superseded": "true"}
    assert {frozenset(call["filters"].items()) for call in oracle.document_batch_calls} <= {
        frozenset(expected.items())
    }


async def test_expansion_plans_are_not_reused_across_as_of(
    plans: tuple[PlanRecorder, PlanRecorder],
) -> None:
    """同じ起点でも基準日が違えば、実体・関係情報の拡張の計画を使い回さない。"""
    entity, graph = plans
    engine = _engine(_corpus())
    state = _SearchState()
    seeds = [_corpus().hits[0]]

    for as_of in ("2020-01-01", "2021-01-01", "2020-01-01"):
        request = SearchRequest(query="q", filters={"as_of": as_of, "knowledge_base_id": "kb-a"})
        await engine._entity_expansion(request, state, seeds)
        await engine._graph_expansion(request, state, seeds, exclude=set())

    assert [scope["as_of"] for scope in entity.scopes] == ["2020-01-01", "2021-01-01"]
    assert [scope["as_of"] for scope in graph.scopes] == ["2020-01-01", "2021-01-01"]
    assert len(state.entity_plans) == len(state.graph_plans) == 2


class _NextDay(date):
    """``OracleClient`` が基準日を補うときの「今日」(日付が変わった後)。"""

    @classmethod
    def today(cls) -> _NextDay:
        return cls(2026, 4, 2)


async def test_missing_as_of_is_resolved_once_even_when_the_date_changes(
    plans: tuple[PlanRecorder, PlanRecorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    """基準日の無い要求は最初に 1 回だけ今日に決め、途中で日付が変わっても全検索で同じ日を使う。"""
    days = iter([date(2026, 4, 1), date(2026, 4, 2)])
    monkeypatch.setattr(answer_engine, "_today", lambda: next(days))
    # 補わずに SQL まで届いた検索は、日付が変わった後の今日(2026-04-02)で判定される。
    monkeypatch.setattr(oracle_module, "date", _NextDay)
    oracle = _corpus()
    request = _with_effective_as_of(
        SearchRequest(query="合成の手順", filters={"knowledge_base_id": "kb-a"})
    )

    await _engine(oracle)._search(request, _SearchState(), retrieval_queries=["合成の手順"])

    assert request.filters["as_of"] == "2026-04-01"
    assert oracle.all_filters()
    assert {_bound_as_of(filters) for filters in oracle.all_filters()} == {"2026-04-01"}
    entity, graph = plans
    assert [scope["as_of"] for scope in [*entity.scopes, *graph.scopes]] == ["2026-04-01"] * 2


def test_explicit_as_of_is_kept() -> None:
    request = SearchRequest(query="q", filters={"as_of": "2020-01-01"})

    assert _with_effective_as_of(request) is request


async def test_answer_run_searches_with_the_resolved_as_of_and_keeps_the_request(
    plans: tuple[PlanRecorder, PlanRecorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    """回答は検索の条件に今日の基準日を補い、回答の分類の条件と要求そのものは変えない。"""
    import rag_engine.generation.answering as answering

    captured: dict[str, Any] = {}

    def fake_answer(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(answering, "answer_question_result", fake_answer)
    monkeypatch.setattr(answer_engine, "_today", lambda: date(2026, 4, 1))
    oracle = _corpus()
    request = SearchRequest(query="q", filters={"knowledge_base_id": "kb-a"})

    with pytest.raises(RuntimeError, match="stop"):
        await _engine(oracle).run(request)

    assert [_bound_as_of(filters) for filters in oracle.category_calls] == ["2026-04-01"]
    assert captured["classification_filter"].as_of == ""
    assert request.filters == {"knowledge_base_id": "kb-a"}


async def test_retrieve_searches_with_the_resolved_as_of(
    plans: tuple[PlanRecorder, PlanRecorder], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(answer_engine, "_today", lambda: date(2026, 4, 1))
    oracle = _corpus()

    await _engine(oracle).retrieve(SearchRequest(query="q", filters={"knowledge_base_id": "kb-a"}))

    assert [call["as_of"] for call in oracle.main_calls] == ["2026-04-01"]
