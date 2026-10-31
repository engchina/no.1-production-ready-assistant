"""生成時の引用記録を参照全体の現在の権限で再認可する。"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from typing import Any

import pytest

from app.rag.saved_evidence import prune_unreadable, referenced_document_ids, withhold_saved_answer
from app.schemas.search import RetrievedChunk


class ReadableDocuments:
    def __init__(self, readable: set[str]) -> None:
        self.readable = readable
        self.calls: list[list[str]] = []

    async def accessible_document_ids(self, document_ids: Sequence[str]) -> set[str]:
        self.calls.append(list(document_ids))
        return self.readable.intersection(document_ids)


def capture(*ids: str, scope: str = "complete") -> dict[str, Any]:
    return dict(
        capture_scope="generation_citation_trace",
        document_scope_status=scope,
        document_ids=list(ids),
        raw_response={"text": "匿名応答", "unused": "未引用抜粋"},
        raw_request={"evidence": "送信原文"},
    )


def citation(document_id: str) -> RetrievedChunk:
    return RetrievedChunk(document_id=document_id, chunk_id="chunk-a", text="匿名抜粋", score=1.0)


def test_referenced_ids_include_uncited_sources() -> None:
    assert referenced_document_ids(
        {"citation": {"document_id": "doc-a"}, "capture": capture("doc-a", "doc-b")}
    ) == {"doc-a", "doc-b"}


@pytest.mark.asyncio
async def test_all_readable_preserves_raw_capture_without_mutation() -> None:
    value = {"capture": capture("doc-a", "doc-b"), "other": {"status": "not_run"}}
    original = deepcopy(value)
    oracle = ReadableDocuments({"doc-a", "doc-b"})
    citations = [citation("doc-a")]
    kept, count, result = await withhold_saved_answer(oracle, citations, value)
    assert (kept, count, result) == (citations, 0, original)
    assert oracle.calls == [["doc-a", "doc-b"]]
    assert value == original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,readable", [("complete", {"doc-a"}), ("unknown", {"doc-a", "doc-b"})]
)
async def test_partial_acl_or_unknown_scope_removes_capture_only(
    scope: str, readable: set[str]
) -> None:
    value = {
        "answer": "保持する回答",
        "capture": capture("doc-a", "doc-b", scope=scope),
        "other": {"status": "not_run"},
    }
    original = deepcopy(value)
    oracle = ReadableDocuments(readable)
    citations = [citation("doc-a")]
    kept, count, result = await withhold_saved_answer(oracle, citations, value)
    assert (kept, count) == (citations, 0)
    assert result == {"answer": value["answer"], "other": value["other"]}
    assert value == original
    assert oracle.calls == [["doc-a", "doc-b"]]


def test_root_and_list_capture_withhold_all_raw_fields() -> None:
    value = capture("doc-a", "doc-b")
    original = deepcopy(value)
    assert prune_unreadable(value, {"doc-a"}) == {
        "capture_scope": "generation_citation_trace",
        "status": "withheld",
    }
    assert prune_unreadable([value, {"status": "error"}], {"doc-a"}) == [{"status": "error"}]
    assert value == original


@pytest.mark.asyncio
async def test_attachment_policy_unchanged_without_document_acl_query() -> None:
    value = {"capture": capture("attachment:synthetic-a")}
    oracle = ReadableDocuments(set())
    citations = [citation("attachment:synthetic-a")]
    assert await withhold_saved_answer(oracle, citations, value) == (citations, 0, value)
    assert oracle.calls == []
    assert referenced_document_ids(value) == set()


@pytest.mark.asyncio
async def test_single_acl_query_unions_capture_citations_and_other_diagnostics() -> None:
    oracle = ReadableDocuments({"doc-a", "doc-c"})
    value = {
        "capture": capture("doc-b", "doc-a", "attachment:synthetic-a"),
        "other": {"document_id": "doc-c", "status": "not_run"},
    }
    original = deepcopy(value)
    citations = [citation("doc-a"), citation("doc-b"), citation("attachment:synthetic-a")]
    kept, count, result = await withhold_saved_answer(oracle, citations, value)
    assert oracle.calls == [["doc-a", "doc-b", "doc-c"]]
    assert [item.document_id for item in kept] == ["doc-a", "attachment:synthetic-a"]
    assert count == 1
    assert result == {"other": value["other"]}
    assert value == original


@pytest.mark.asyncio
async def test_unscoped_call_snapshot_is_not_exposed_to_regular_viewer() -> None:
    from pr_backend_core.chat_progress import ChatProgressStepDetails
    from pr_backend_core.observability.call_details import call_input

    call = call_input({"input": "private-synthetic-input"}).model_copy(
        update={"visibility": "system_admin"}
    )
    diagnostics = {
        "trace": {"span_details": {"a": ChatProgressStepDetails(call=call).model_dump()}}
    }
    _, _, visible = await withhold_saved_answer(ReadableDocuments(set()), [], diagnostics)
    assert "private-synthetic-input" not in str(visible)
    assert "withheld" in str(visible)
    assert "private-synthetic-input" in str(diagnostics)
