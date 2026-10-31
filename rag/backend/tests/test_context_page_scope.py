"""検索のページ範囲を親・近傍と既読原文へ引き継ぐ。"""

import json
from typing import Any, cast

import pytest
from rag_engine.generation.answer_records import _stored_chunk_answer_record

from app.config import Settings
from app.rag.answer_engine import (
    AnswerEngine,
    _SearchState,
    _stored_child,
    _stored_parents,
    _with_context_scope,
)
from app.rag.context_page_scope import contained
from app.schemas.search import RetrievedChunk, SearchRequest
from tests.test_answer_engine import FakeGenAi, FakeOracle


def scope(start: int = 2, end: int = 2) -> str:
    return json.dumps([{"document_id": "doc-1", "page_start": start, "page_end": end}])


def chunk(
    uid: str, start: int | None, end: int | None = None, *, parent: bool = False
) -> RetrievedChunk:
    metadata: dict[str, Any] = {
        "chunk_set_id": "set-1",
        "recipe_id": "recipe-1",
        "chunk_strategy": "small_to_big" if parent else "fixed",
        "chunk_group_id": "group-1",
        "engine_chunk_seq": int(uid[-1]),
    }
    if start is not None:
        metadata.update(page_start=start, page_end=end or start)
    if parent:
        metadata.update(parent_text="親の全文", parent_page_start=1, parent_page_end=3)
    return RetrievedChunk(
        document_id="doc-1",
        chunk_id=uid,
        text=f"合成の本文 {uid}",
        score=1,
        file_name="synthetic.pdf",
        metadata=metadata,
    )


@pytest.mark.parametrize(
    "start,end,accepted",
    [(2, 2, True), (1, 2, False), (2, 3, False), (None, None, False), (1, 1, False)],
)
def test_full_text_must_be_contained(start: int | None, end: int | None, accepted: bool) -> None:
    value = chunk("chunk-1", start, end)
    assert contained(value.document_id, value.metadata, scope()) is accepted
    assert contained(value.document_id, value.metadata, "")


@pytest.mark.parametrize("parent", [False, True])
async def test_delayed_context_filters_neighbors_and_unproven_parent(
    parent: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = [
        chunk("chunk-1", 1, parent=parent),
        chunk("chunk-2", 2, parent=parent),
        chunk("chunk-3", 3, parent=parent),
        chunk("chunk-4", None, parent=parent),
    ]
    oracle = FakeOracle()
    calls: list[list[str]] = []

    async def siblings(anchors: list[RetrievedChunk], **kwargs: Any) -> list[RetrievedChunk]:
        calls.append([item.chunk_id for item in anchors])
        return values

    monkeypatch.setattr(oracle, "context_group_siblings", siblings)
    state = _SearchState(chunks={item.chunk_id: item for item in values}, filters={})
    engine = AnswerEngine(Settings(), oracle=cast(Any, oracle), genai=cast(Any, FakeGenAi()))
    anchor = _stored_chunk_answer_record(
        _with_context_scope(_stored_child(values[1], rrf_score=1), scope())
    )
    rows = await engine._context_chunks(state, anchors=[anchor])
    assert [item.chunk_uid for item in rows] == ["chunk-2"]
    # 同じ回答内でも、別の検索範囲の既読を使わない。
    wider = _stored_chunk_answer_record(
        _with_context_scope(_stored_child(values[1], rrf_score=1), scope(1, 3))
    )
    wide_rows = await engine._context_chunks(state, anchors=[wider])
    assert len(calls) == 2
    assert {item.chunk_uid for item in wide_rows if item.chunk_level == "child"} == {
        "chunk-1",
        "chunk-2",
        "chunk-3",
    }
    if parent:
        result = next(item for item in wide_rows if item.chunk_level == "parent")
        assert (result.page_start, result.page_end) == (1, 3)
    again = await engine._context_chunks(state, anchors=[anchor])
    assert [item.chunk_uid for item in again] == ["chunk-2"]
    assert len(calls) == 2


@pytest.mark.parametrize(
    "bounds",
    [
        {},
        {"parent_page_start": 1, "parent_page_end": 3},
        {"parent_page_start": 2, "parent_page_end": 2},
    ],
)
def test_parent_scope_requires_its_own_provenance(bounds: dict[str, int]) -> None:
    value = chunk("chunk-2", 2, parent=True)
    value.metadata.pop("parent_page_start")
    value.metadata.pop("parent_page_end")
    value.metadata.update(bounds)
    state = _SearchState(chunks={value.chunk_id: value}, filters={"page_ranges": scope()})
    child = _with_context_scope(_stored_child(value, rrf_score=1), scope())
    assert bool(_stored_parents([child], state)) is (bounds.get("parent_page_start") == 2)
    state.filters = {}
    assert _stored_parents([_stored_child(value, rrf_score=1)], state)


@pytest.mark.parametrize("defer", [False, True])
async def test_search_propagates_effective_page_scope(defer: bool) -> None:
    oracle = FakeOracle()
    oracle.chunks = [chunk("chunk-2", 2, parent=True), chunk("chunk-3", 3, parent=True)]
    engine = AnswerEngine(Settings(), oracle=cast(Any, oracle), genai=cast(Any, FakeGenAi()))
    state = _SearchState()
    result = await engine._search(
        SearchRequest(query="合成の質問"),
        state,
        retrieval_queries=["合成の質問"],
        page_ranges=[{"document_id": "doc-1", "page_start": 2, "page_end": 2}],
        defer_context=defer,
    )
    assert result.child_chunks
    assert all(
        json.loads(item.metadata["context_page_ranges"])[0]["page_start"] == 2
        for item in result.child_chunks
    )
    assert all(item.chunk_level == "child" and item.page_start == 2 for item in result.all_chunks)


def test_disjoint_ranges_do_not_authorize_the_gap() -> None:
    ranges = json.dumps(
        [
            {"document_id": "doc-1", "page_start": 1, "page_end": 1},
            {"document_id": "doc-1", "page_start": 3, "page_end": 3},
        ]
    )
    assert contained("doc-1", chunk("chunk-1", 1).metadata, ranges)
    assert contained("doc-1", chunk("chunk-3", 3).metadata, ranges)
    assert not contained("doc-1", chunk("chunk-2", 2).metadata, ranges)
    assert not contained("doc-1", chunk("chunk-1", 1, 3).metadata, ranges)


def test_scope_removes_first_page_background() -> None:
    value = chunk("chunk-2", 2)
    child = _stored_child(
        value, rrf_score=1, first_page_context={"status": "available", "text": "範囲外の背景"}
    )
    assert "first_page_context" in child.metadata["document"]
    scoped = _with_context_scope(child, scope())
    assert "first_page_context" not in scoped.metadata["document"]
    assert "first_page_context" in child.metadata["document"]


async def test_unscoped_search_keeps_original_context() -> None:
    oracle = FakeOracle()
    values = [chunk("chunk-1", 1, parent=True), chunk("chunk-2", 2, parent=True)]
    oracle.chunks = values
    state = _SearchState(chunks={item.chunk_id: item for item in values})
    engine = AnswerEngine(Settings(), oracle=cast(Any, oracle), genai=cast(Any, FakeGenAi()))
    rows = await engine._context_chunks(
        state, anchors=[_stored_chunk_answer_record(_stored_child(values[0], rrf_score=1))]
    )
    assert any(item.chunk_level == "parent" and item.text == "親の全文" for item in rows)
    assert {item.chunk_uid for item in rows if item.chunk_level == "child"} == {
        "chunk-1",
        "chunk-2",
    }
