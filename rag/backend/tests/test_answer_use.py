"""回答に使った箇所の使われ方（確かめた説明 / 参考）を、引用の metadata と MCP の evidence に
載せる（#2040）。
"""

from typing import Any

from app.mcp import tools as mcp_tools
from app.rag.answer_engine import _outcome_from_result, _SearchState
from app.schemas.search import RetrievedChunk


class _EngineResult:
    """rag_engine の回答の結果（``_outcome_from_result`` が読む属性だけ）。"""

    def __init__(self, children: list[dict[str, Any]]) -> None:
        self.evidence_items = [{"chunk_id": "p", "children": children}]
        self.generation_trace: dict[str, Any] = {}
        self.execution_steps: list[Any] = []
        self.generated_queries: list[str] = []
        self.text_search_tokens: list[str] = []
        self.crag_attempts: list[Any] = []
        self.envelope: dict[str, Any] = {}
        self.external_data_items: list[Any] = []
        self.question_type: list[str] = []
        self.answer_text = "回答"

    def __getattr__(self, name: str) -> Any:
        return None


def _chunk(chunk_id: str) -> RetrievedChunk:
    return RetrievedChunk(document_id="d1", chunk_id=chunk_id, text="本文", score=0.5, metadata={})


def test_outcome_carries_answer_use_to_citation_metadata_and_mcp_evidence() -> None:
    state = _SearchState()
    state.chunks = {
        cid: _chunk(cid) for cid in ("c-confirmed", "c-reference", "c-unknown", "c-unused")
    }
    children = [
        {
            "chunk_id": "c-confirmed",
            "is_model_used": True,
            "model_usage_rank": 1,
            "answer_use": "confirmed",
        },
        {
            "chunk_id": "c-reference",
            "is_model_used": True,
            "model_usage_rank": 2,
            "answer_use": "reference",
        },
        # 使われ方の分からない流れ（決まった値でないもの）は載せない。
        {
            "chunk_id": "c-unknown",
            "is_model_used": True,
            "model_usage_rank": 3,
            "answer_use": "other",
        },
        # 回答に使っていない箇所には、値があっても使われ方を付けない。
        {"chunk_id": "c-unused", "is_model_used": False, "answer_use": "confirmed"},
    ]
    outcome = _outcome_from_result(_EngineResult(children), state)

    uses = {
        chunk.chunk_id: chunk.metadata.get("evidence_answer_use") for chunk in outcome.citations
    }
    assert uses == {
        "c-confirmed": "confirmed",
        "c-reference": "reference",
        "c-unknown": None,
        "c-unused": None,
    }
    tree_uses = {
        child["chunk_id"]: child["answer_use"]
        for child in outcome.diagnostics["evidence_tree"][0]["children"]
    }
    assert tree_uses == uses

    evidence = {chunk.chunk_id: mcp_tools._evidence(chunk) for chunk in outcome.citations}
    assert {cid: (item.used_in_answer, item.answer_use) for cid, item in evidence.items()} == {
        "c-confirmed": (True, "confirmed"),
        "c-reference": (True, "reference"),
        "c-unknown": (True, None),
        "c-unused": (False, None),
    }


class _TreeEngineResult(_EngineResult):
    """まとまり（親）が複数ある回答の結果。"""

    def __init__(self, parents: list[dict[str, Any]]) -> None:
        super().__init__([])
        self.evidence_items = parents


def test_evidence_tree_children_match_citations_one_to_one() -> None:
    """根拠の木の子の合計は「AI に渡した N 件」（citations の件数）と一致する（#2097）。

    同じ chunk が 2 つのまとまりにあるときは引用に使った方の 1 回だけ、本文を読めない chunk
    （state に無い）は引用にも木にも出さず、箇所の無くなったまとまりは落とす。
    """
    state = _SearchState()
    state.chunks = {cid: _chunk(cid) for cid in ("a1", "a2", "b1", "b2")}
    parents: list[dict[str, Any]] = [
        {
            "chunk_id": "parent-a",
            "children": [
                {"chunk_id": "a1", "retrieval_role": "retrieved_anchor"},
                {"chunk_id": "a2", "retrieval_role": "neighbor_context"},
                # 本文を読めない chunk。
                {"chunk_id": "missing", "retrieval_role": "neighbor_context"},
            ],
        },
        {
            "chunk_id": "parent-b",
            "children": [
                # 回答に使ったので、引用の並びでは先になる（木でもこちらに残す）。
                {
                    "chunk_id": "b1",
                    "retrieval_role": "retrieved_anchor",
                    "is_model_used": True,
                    "model_usage_rank": 1,
                },
                {"chunk_id": "b2", "retrieval_role": "operation_context"},
            ],
        },
        {
            "chunk_id": "parent-c",
            "children": [
                # parent-b と同じ chunk（引用は 1 回）。
                {"chunk_id": "b1", "retrieval_role": "operation_context"},
                {"chunk_id": "missing", "retrieval_role": "operation_context"},
            ],
        },
    ]
    outcome = _outcome_from_result(_TreeEngineResult(parents), state)

    cited = [chunk.chunk_id for chunk in outcome.citations]
    tree = outcome.diagnostics["evidence_tree"]
    children = [child["chunk_id"] for parent in tree for child in parent["children"]]
    assert len(children) == len(cited) == 4
    assert sorted(children) == sorted(cited)
    assert [parent["parent_id"] for parent in tree] == ["parent-a", "parent-b"]
    # 重複した chunk は、引用に使った方（回答に使った parent-b の検索で見つかった箇所）に残る。
    kept = next(child for child in tree[1]["children"] if child["chunk_id"] == "b1")
    assert kept["role"] == "retrieved_anchor"
    assert kept["is_model_used"] is True
