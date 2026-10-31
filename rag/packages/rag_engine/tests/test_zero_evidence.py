"""根拠 0 件で終えたときの段の判定（#2401）。合成の候補だけを使う。"""

import pytest

from rag_engine.generation.answer_models import AnswerContext, AnswerRecord, CragRetrievalAttempt
from rag_engine.generation.zero_evidence import ZERO_EVIDENCE_STAGE_LABELS, zero_evidence_stage


def _record(record_id: str) -> AnswerRecord:
    return AnswerRecord(id=record_id, engine="docling", engine_label="docling", page=1, seq_no=1, category="Text", text="本文")


def _attempt(*, rejected: tuple[str, ...] = (), stop_reason: str = "") -> CragRetrievalAttempt:
    return CragRetrievalAttempt(
        attempt=1, query="質問", retrieval_queries=("質問",), sufficient=False, rejected_chunk_ids=rejected, stop_reason=stop_reason
    )


@pytest.mark.parametrize(
    ("context", "attempts", "stage", "candidates", "rejected"),
    [
        (AnswerContext(records=[], text=""), (), "retrieval", 0, 0),
        # CRAG の 3 回とも検索の候補が無い（評価器は空の候補を見ただけ）。
        (AnswerContext(records=[], text=""), (_attempt(), _attempt(), _attempt()), "retrieval", 0, 0),
        (
            AnswerContext(records=[], text="", ranked_candidates=(_record("c1"), _record("c2"))),
            (_attempt(rejected=("c1", "c2"), stop_reason="all_candidates_rejected"),),
            "evidence_check",
            2,
            2,
        ),
        (
            AnswerContext(records=[], text="", insufficient_reason="all_candidates_rejected", expansion_records=(_record("c1"),)),
            (_attempt(rejected=("c1",)),),
            "evidence_check",
            1,
            1,
        ),
        (AnswerContext(records=[], text="", ranked_candidates=(_record("c1"),)), (), "context", 1, 0),
    ],
    ids=["no-candidates", "no-candidates-crag", "all-rejected", "all-rejected-refined", "context-empty"],
)
def test_zero_evidence_stage(
    context: AnswerContext, attempts: tuple[CragRetrievalAttempt, ...], stage: str, candidates: int, rejected: int
) -> None:
    result = zero_evidence_stage(context, attempts)

    assert result["stage"] == stage
    assert result["label"] == ZERO_EVIDENCE_STAGE_LABELS[stage]
    assert (result["candidate_count"], result["rejected_count"]) == (candidates, rejected)
    assert result["crag_attempt_count"] == len(attempts)
