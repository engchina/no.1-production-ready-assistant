"""単件回答の意味上の正確性・網羅性・根拠への忠実性と、測定できた指標の閾値判定。

標準回答がない場合は正確性・網羅性を未評価とし、主張の支持は回答と引用だけで測る。"""

import re
from collections.abc import Mapping, Sequence
from typing import Any

from rag_engine.generation.answer_labels import answer_labels
from rag_engine.generation.grounded import (
    generated_section_lines,
    is_structural_line,
)

from app.config import Settings
from app.rag.evaluation_adapter import resolve_evaluation_adapter
from app.schemas.search import RetrievedChunk

UNSUPPORTED_CLAIM_STATUSES = frozenset({"unsupported", "contradicted"})
# 1 件の回答では表示だけにして、合否に使わない指標。
REFERENCE_ONLY_METRICS: frozenset[str] = frozenset()


def system_section_headings() -> frozenset[str]:
    """回答の本文の末尾の、主張ではない節の見出し（言語資源の answer_labels。#2738）。

    根拠と矛盾して外した内容・確認が必要な点の節で、外した段落・印の段落の引用と理由を持つ。
    """
    labels = answer_labels()
    return frozenset({labels.removed_section, labels.marked_section})


# 見出しの行（Markdown の見出し・太字だけの行。後ろの「：」は許す）。
_HEADING_LINE = re.compile(r"(?:#{1,6}\s+(?P<md>.+?)|\*\*(?P<bold>[^*\n]+)\*\*)\s*[:：]?")


def _heading_title(line: str) -> str | None:
    match = _HEADING_LINE.fullmatch(line.strip())
    if match is None:
        return None
    return (match.group("md") or match.group("bold") or "").strip().strip("*").strip()


def without_system_sections(answer: str) -> str:
    """主張ではない節（``system_section_headings``。次の見出しの前まで）を除く。

    節は外した段落・印の段落の原文と理由なので、忠実さ（語句の一致）に数えない。印の段落そのものは
    本文に残るので数える（#2738）。
    """
    headings = system_section_headings()
    kept: list[str] = []
    inside = False
    for line in answer.splitlines():
        title = _heading_title(line)
        if title is not None:
            inside = title in headings
        if not inside:
            kept.append(line)
    return "\n".join(kept)


def grounding_text(answer: str) -> str:
    """忠実さの照合に使う回答の本文。

    回答生成が付けた見出し・出典行・定型文（「参考情報」の節の直下の説明を含む。#2040）と、
    「根拠と矛盾し除いた内容」「確認が必要な点」の節（#2738）は主張ではないので外す。印の付いた
    段落は本文の主張として数える（#2738）。Agent の回答の節（旧形式を含む）の扱いは suite の
    Agent の adapter（`suite/evaluation`。#3240）にあり、RAG は Agent の回答の形式を持たない。
    """
    body = without_system_sections(answer)
    # 読めなかった章節・時間の上限で省いた処理の行もシステムの案内なので外す（#2040）。
    generated = generated_section_lines(body)
    return "\n".join(
        line
        for line in body.splitlines()
        if line.strip()
        and not is_structural_line(line)
        and line.strip() != answer_labels().neutral_summary
        and line.strip() not in generated
    )


def is_abstained_answer(
    answer: str, citations: Sequence[RetrievedChunk], insufficient_reason: str
) -> bool:
    """回答が「資料から答えられない」旨だけか(拒答)。

    本文か引用が無い回答は拒答。不足の理由があり、モデルが使った根拠が 1 つも無い回答も拒答と
    する(必要な根拠を原文のまま示すだけの回答を含む)。
    """
    if not answer.strip() or not citations:
        return True
    if not insufficient_reason.strip():
        return False
    # 親（まとまり）だけを参照した根拠（#2710）も、回答が資料を使った印にする。
    return not any(
        chunk.metadata.get("evidence_model_used") or chunk.metadata.get("evidence_parent_used")
        for chunk in citations
    )


def claims_supported(claims: Sequence[Mapping[str, Any]]) -> bool | None:
    """根拠のない主張・根拠と矛盾する主張が無いか。"""
    if any(str(claim.get("status")) in UNSUPPORTED_CLAIM_STATUSES for claim in claims):
        return False
    if any(claim.get("status") in {"unassessed", "citation_error"} for claim in claims):
        return None
    return True


def score_answer_evaluation(
    evaluation: Mapping[str, Any],
    *,
    answer: str,
    citations: Sequence[RetrievedChunk],
    insufficient_reason: str,
    settings: Settings,
) -> dict[str, Any]:
    """LLM の判定(照合・監査)に、評価の基準の指標・閾値・合否を足す。未完了の評価はそのまま。"""
    if evaluation.get("status") != "completed":
        return dict(evaluation)
    params = resolve_evaluation_adapter(settings)
    claims = [claim for claim in evaluation.get("claim_checks") or [] if isinstance(claim, Mapping)]
    assessed = [
        claim for claim in claims if claim.get("status") not in {"not_a_claim", "data_confirmation"}
    ]
    support = (
        sum(claim.get("status") == "supported" for claim in assessed) / len(assessed)
        if assessed
        and citations
        and all(
            claim.get("status") in {"supported", "unsupported", "contradicted"}
            for claim in assessed
        )
        else None
    )
    coverage = evaluation.get("requirement_coverage")
    correctness = evaluation.get("answer_correctness")
    values: dict[str, float | None] = {
        "answer_correctness": float(correctness) if isinstance(correctness, int | float) else None,
        "requirement_coverage": float(coverage) if isinstance(coverage, int | float) else None,
        "claim_support_rate": support,
    }
    metrics = []
    for name, value in values.items():
        threshold = getattr(params.thresholds, name)
        metrics.append(
            {
                "name": name,
                "value": round(value, 4) if value is not None else None,
                "threshold": threshold,
                "passed": value >= threshold
                if value is not None and threshold is not None
                else None,
                "reference": name in REFERENCE_ONLY_METRICS,
            }
        )
    judged = [metric for metric in metrics if metric["passed"] is not None]
    incomplete = any(claim.get("status") in {"unassessed", "citation_error"} for claim in assessed)
    passed = (
        False
        if any(metric["passed"] is False for metric in judged)
        else None
        if incomplete or not judged
        else True
    )
    return {
        **evaluation,
        "metrics_version": "six_metrics_v1",
        "claim_support_rate": support,
        "suite": params.suite,
        "metrics": metrics,
        "passed": passed,
    }
