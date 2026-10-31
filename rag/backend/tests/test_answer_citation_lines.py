"""本文の出典行（「根拠：」）を引用の chunk に結ぶ（#1330・#2710）。"""

from typing import Any

from app.rag.answer_engine import (
    ANSWER_CITATION_CANDIDATE_LINES_KEY,
    ANSWER_CITATION_LINES_KEY,
    ANSWER_CITATION_PROVENANCE_CHILD,
    ANSWER_CITATION_PROVENANCE_KEY,
    _with_citation_lines,
)
from app.schemas.search import RetrievedChunk


def _chunk(chunk_id: str, page_start: int, page_end: int | None = None) -> RetrievedChunk:
    return RetrievedChunk(
        document_id="d1",
        chunk_id=chunk_id,
        text="本文",
        score=0.5,
        file_name="文書 A.pdf",
        metadata={"page_start": page_start, "page_end": page_end or page_start},
    )


def _parent(parent_uid: str, *children: tuple[str, bool]) -> dict[str, Any]:
    return {
        "chunk_uid": parent_uid,
        "children": [{"chunk_id": chunk_id, "is_model_used": used} for chunk_id, used in children],
    }


def _lines(citations: list[RetrievedChunk], key: str = ANSWER_CITATION_LINES_KEY) -> dict[str, Any]:
    return {chunk.chunk_id: chunk.metadata[key] for chunk in citations if key in chunk.metadata}


def test_citation_lines_map_only_to_the_children_that_support_the_line() -> None:
    citations = [_chunk("c1", 2), _chunk("c2", 2), _chunk("c3", 5, 6), _chunk("c4", 9)]
    parents = [
        _parent("d1:g1", ("c1", False), ("c2", True)),
        _parent("d1:g2", ("c3", True), ("c4", True)),
    ]
    refs = [
        # 引用の位置が 1 つの子に決まっている（同じ頁の c1 と c2 を取り違えない）。
        {"source_id": "d1:g1", "scope_source_ids": ["c1"], "page": 2},
        # 同じ出典行にまとめた説明が 2 つの子に支えられていれば、両方に番号を付ける。
        {"source_ids": ["d1:g2"], "scope_source_ids": ["c3", "c4"], "page": 5, "page_end": 9},
    ]

    result = _with_citation_lines(citations, parents, refs)

    assert _lines(result) == {"c1": [1], "c3": [2], "c4": [2]}
    assert _lines(result, ANSWER_CITATION_CANDIDATE_LINES_KEY) == {}
    # 確定の番号には決め方の印を付ける（以前の回答の番号と見分ける）。
    assert {
        chunk.chunk_id for chunk in result if chunk.metadata.get(ANSWER_CITATION_PROVENANCE_KEY)
    } == {
        "c1",
        "c3",
        "c4",
    }
    assert result[0].metadata[ANSWER_CITATION_PROVENANCE_KEY] == ANSWER_CITATION_PROVENANCE_CHILD
    # 元の引用の metadata は変えない（新しい chunk を返す）。
    assert ANSWER_CITATION_LINES_KEY not in citations[0].metadata


def test_unresolved_line_is_not_bound_to_the_first_or_used_child() -> None:
    """子の決まらない出典行は、回答に使った印・頁・引用一覧の順で 1 つを選ばない（#2710）。

    候補として残す。
    """
    # 同じ親・同じ頁の見出し H と図の説明 F。どちらも回答に使った印があり、引用一覧は H が先。
    citations = [_chunk("h", 3), _chunk("f", 3), _chunk("other", 7)]
    parents = [_parent("d1:p", ("h", True), ("f", True), ("other", True))]
    refs = [
        # rag_engine が候補を挙げた。
        {
            "source_id": "d1:p",
            "scope_source_ids": [],
            "candidate_source_ids": ["h", "f"],
            "page": 3,
        },
        # 候補も無ければ、親の子のうち出典行の頁に重なるもの。
        {"source_id": "d1:p", "scope_source_ids": [], "page": 3},
    ]

    result = _with_citation_lines(citations, parents, refs)

    assert _lines(result) == {}
    assert _lines(result, ANSWER_CITATION_CANDIDATE_LINES_KEY) == {"h": [1, 2], "f": [1, 2]}
    assert not any(ANSWER_CITATION_PROVENANCE_KEY in chunk.metadata for chunk in result)


def test_unknown_parent_or_missing_refs_leave_citations_unchanged() -> None:
    citations = [_chunk("c1", 1)]

    assert _with_citation_lines(citations, [], None) == citations
    assert _with_citation_lines(citations, [], [{"source_id": "x", "page": 1}]) == citations
    # 引用に無い子は選ばない。
    unchanged = _with_citation_lines(
        citations, [_parent("p", ("gone", True))], [{"source_id": "p", "page": 1}]
    )
    assert _lines(unchanged) == {}
    assert _lines(unchanged, ANSWER_CITATION_CANDIDATE_LINES_KEY) == {}


def test_each_item_has_its_own_citation_line_even_when_reusing_same_page_or_child() -> None:
    citations = [_chunk("a", 1), _chunk("b", 1), _chunk("candidate", 1)]
    parents = [_parent("parent", ("a", True), ("b", True), ("candidate", False))]
    refs = [
        {"source_id": "parent", "scope_source_ids": ["a"], "page": 1},
        {"source_id": "parent", "scope_source_ids": ["b"], "page": 1},
        {"source_id": "parent", "scope_source_ids": ["a", "b"], "page": 1},
        {
            "source_id": "parent",
            "scope_source_ids": [],
            "candidate_source_ids": ["candidate"],
            "page": 1,
        },
    ]
    result = _with_citation_lines(citations, parents, refs)
    assert _lines(result) == {"a": [1, 3], "b": [2, 3]}
    assert _lines(result, ANSWER_CITATION_CANDIDATE_LINES_KEY) == {"candidate": [4]}
    assert result[2].metadata.get(ANSWER_CITATION_PROVENANCE_KEY) is None
