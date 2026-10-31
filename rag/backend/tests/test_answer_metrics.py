"""標準回答による評価の指標と合否(評価の基準の指標と閾値。#680)。"""

import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.rag.answer_metrics import score_answer_evaluation
from app.schemas.search import RetrievedChunk

ANSWER = "特典は QUO カード 5 千円です。"
CITATIONS = [
    RetrievedChunk(
        document_id="d1",
        chunk_id="c1",
        text="特典 住宅ローン契約実行者に QUO カード 5 千円を贈呈",
        score=0.9,
        metadata={"evidence_model_used": True},
    )
]


def _judged(coverage: float = 1.0, claim: str = "supported") -> dict[str, object]:
    return {
        "status": "completed",
        "requirement_coverage": coverage,
        "answer_correctness": 1.0,
        "claim_checks": [{"status": claim}],
    }


def _score(
    evaluation: Mapping[str, object],
    suite: str = "standard",
    *,
    answer: str = ANSWER,
    citations: Sequence[RetrievedChunk] = CITATIONS,
) -> dict[str, Any]:
    return score_answer_evaluation(
        evaluation,
        answer=answer,
        citations=citations,
        insufficient_reason="",
        settings=Settings(rag_evaluation_suite=suite),
    )


def test_only_metrics_measurable_for_one_answer_are_judged_with_suite_thresholds() -> None:
    result = _score(_judged(coverage=0.95))
    metrics = {metric["name"]: metric for metric in result["metrics"]}
    assert result["suite"] == "standard"
    assert set(metrics) == {"answer_correctness", "claim_support_rate", "requirement_coverage"}
    assert metrics["requirement_coverage"] == {
        "name": "requirement_coverage",
        "value": 0.95,
        "threshold": 0.9,
        "passed": True,
        "reference": False,
    }
    strict = {m["name"]: m for m in _score(_judged(coverage=0.95), "strict")["metrics"]}
    assert strict["requirement_coverage"]["threshold"] == 1.0
    assert strict["requirement_coverage"]["passed"] is False


def test_unsupported_claim_or_refusal_fails_the_answer() -> None:
    unsupported = _score(_judged(claim="unsupported"))
    assert unsupported["passed"] is False
    assert {m["name"]: m["value"] for m in unsupported["metrics"]}["claim_support_rate"] == 0
    unassessed = _score(_judged(claim="unassessed"))
    assert unassessed["claim_support_rate"] is None
    assert (
        next(m for m in unassessed["metrics"] if m["name"] == "claim_support_rate")["value"] is None
    )
    refused = _score(_judged(), answer="", citations=[])
    assert refused["claim_support_rate"] is None
    assert all(m["name"] != "refusal_accuracy" for m in refused["metrics"])


def test_unfinished_evaluation_is_returned_as_is() -> None:
    evaluation = {"status": "error", "message": "評価を完了できませんでした。"}
    assert _score(evaluation) == evaluation


def test_faithfulness_is_reference_only_and_ignores_generated_boilerplate() -> None:
    from rag_engine.generation.answer_labels import answer_labels

    from app.rag.answer_metrics import grounding_text

    labels = answer_labels()
    answer = f"{labels.neutral_summary}\n\n{labels.rules_section}\n\n・{ANSWER}\n根拠：info.pdf p.1"
    # 見出し・節の直下の説明・前置きは文言に依らず主張から外す（#2040 で文言を変えても同じ）。
    assert (
        grounding_text(
            f"{answer}\n\n{answer_labels().quote_only_section}\n\n{answer_labels().quote_only_note}"
        )
        == f"・{ANSWER}"
    )
    assert grounding_text(answer) == f"・{ANSWER}"
    # 読めなかった章節の行はシステムの案内なので数えない（#2040）。
    unread = (
        f"{answer_labels().unverified_section}\n\n"
        "・「a.pdf」の「第2章」は、読み込み量の上限に達したため未反映です。"
    )
    assert grounding_text(f"{answer}\n\n{unread}") == f"・{ANSWER}"
    # 語句一致ではなく、主張の支持の判定を採用する。
    result = _score(_judged(), answer="合成の言い換えです。")
    metrics = {metric["name"]: metric for metric in result["metrics"]}
    assert "faithfulness" not in metrics
    assert metrics["claim_support_rate"]["value"] == 1.0
    assert metrics["claim_support_rate"]["reference"] is False
    assert result["passed"] is True


def test_grounding_text_keeps_marked_passages_and_drops_the_system_sections() -> None:
    """faithfulness は印の段落を本文として数え、外した段落・確認が必要な点の節は数えない。

    #2738。RAG 自身の回答の節（Agent の回答の節は suite の Agent の adapter。#3240）。
    """
    from rag_engine.generation.answer_labels import answer_labels

    from app.rag.answer_metrics import grounding_text

    labels = answer_labels()
    rag_answer = "\n".join(
        [
            "設定は 2 段階で変更します。",
            "",
            labels.rules_section,
            "",
            "・区分Ⅱの選択肢の値は 01 です。",
            "根拠：設定.pdf p.1",
            "",
            labels.removed_section,
            "",
            labels.removed_section_note,
            "・「変更は翌月から反映されます。」：根拠と矛盾（当月から反映）",
            "",
            labels.marked_section,
            "",
            labels.marked_section_note,
            "・「区分Ⅱの選択肢の値は 01 です。」：根拠の表の対応と違う可能性",
        ]
    )
    assert grounding_text(rag_answer).splitlines() == [
        "設定は 2 段階で変更します。",
        "・区分Ⅱの選択肢の値は 01 です。",
    ]


# RAG 自身の回答の節の基線（#3240 の段 1）。Agent の回答の節（旧形式を含む）は suite の
# Agent の adapter のテスト（suite/evaluation/tests/test_agent_legacy.py）。
ANSWER_SECTIONS: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parent / "fixtures/answer_sections.json").read_text("utf-8")
)


def _with_labels(template: str) -> str:
    """fixture の {name} を言語資源の answer_labels() の項目に置き換える。"""
    from rag_engine.generation.answer_labels import answer_labels

    labels = answer_labels()
    return re.sub(r"\{(\w+)\}", lambda match: str(getattr(labels, match.group(1))), template)


@pytest.mark.parametrize("case", ANSWER_SECTIONS["rag_sections"], ids=lambda case: str(case["id"]))
def test_grounding_text_of_rag_answer_sections_matches_the_baseline(case: dict[str, str]) -> None:
    """RAG 自身の回答の節（見出し・前置き・出典行・参考情報・未反映の章節・外した段落・印）。"""
    from app.rag.answer_metrics import grounding_text

    assert grounding_text(_with_labels(case["answer"])) == _with_labels(case["grounding"])


@pytest.mark.parametrize("status", ["unassessed", "citation_error"])
def test_unassessed_claims_cannot_pass_with_other_perfect_metrics(status: str) -> None:
    from app.rag.answer_metrics import claims_supported

    evaluated = _judged(coverage=1.0, claim=status)
    assert _score(evaluated)["passed"] is None
    assert claims_supported([{"status": status}]) is None
    assert claims_supported([{"status": "unsupported"}, {"status": status}]) is False
