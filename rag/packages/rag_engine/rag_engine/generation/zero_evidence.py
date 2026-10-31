"""根拠 0 件で回答を作らずに終えたとき、どの段で 0 件になったかを決める（#2401）。

回答の記録の「根拠 0 件・生成 0 回」だけでは、検索の候補が無かったのか、根拠確認（CRAG の評価）が候補を
すべて外したのかが分からない。段と件数を工程の詳細と回答の記録（``generation_trace.zero_evidence``）に
残し、検索をやり直さずに原因の段を追えるようにする。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from rag_engine.generation.answer_models import AnswerContext, CragRetrievalAttempt

# 段の名前と表示の文。``filter`` は呼び出し側（検索の条件を持つ backend）が、自動で足した検索条件が
# 効いたまま候補が 0 件だったときに ``retrieval`` を置き換える。
ZERO_EVIDENCE_STAGE_LABELS = {
    "retrieval": "文書検索（検索の候補が 0 件）",
    "filter": "文書検索（自動で足した検索条件で候補が 0 件）",
    "evidence_check": "根拠確認（候補をすべて質問と別の話題と判定）",
    "context": "根拠の組み立て（候補はあるが回答に渡す根拠が 0 件）",
}

# 評価器が全候補を不適合とした印（``_refine_crag_context`` の insufficient_reason と CRAG の stop_reason）。
_ALL_REJECTED = "all_candidates_rejected"


def zero_evidence_stage(context: AnswerContext, crag_attempts: Sequence[CragRetrievalAttempt] = ()) -> dict[str, Any]:
    """根拠 0 件になった段と、その判断に使った件数を返す。

    - 検索の候補が 1 件も無い（評価器が外した候補も無い）: ``retrieval``
    - 評価器が候補をすべて不適合とした: ``evidence_check``
    - 候補はあるが、根拠の組み立て（文書の選択・文字数の上限など）で 0 件になった: ``context``
    """
    candidate_count = max(len(context.ranked_candidates), len(context.expansion_records))
    rejected = {uid for attempt in crag_attempts for uid in attempt.rejected_chunk_ids}
    stop_reason = crag_attempts[-1].stop_reason if crag_attempts else ""
    if stop_reason == _ALL_REJECTED or context.insufficient_reason == _ALL_REJECTED or (rejected and not candidate_count):
        stage = "evidence_check"
    elif not candidate_count:
        stage = "retrieval"
    else:
        stage = "context"
    return {
        "stage": stage,
        "label": ZERO_EVIDENCE_STAGE_LABELS[stage],
        "candidate_count": candidate_count,
        "rejected_count": len(rejected),
        "crag_attempt_count": len(crag_attempts),
        "stop_reason": stop_reason,
    }
