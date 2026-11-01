"""最終の検証の、業務ガイドの手順ごとの充足（#2708）。

呼び出し側（Agent）の回答の段落と、業務ガイドの手順の主の操作の文を、RAG の回答と同じ規則
（rag_engine の `guide_step_validation`）で対応付ける。手順の題名の文字の一致には頼らない。

- 確かめる手順: 公開の版の手順のうち、分かっている条件で外れた分岐の手順を除いたもの。条件で
  決まらない分岐の手順は必須に数えない（`answer_checks.conditional_steps`）。
- 本文の充足検査が完了して不足を確認した必須の手順だけを、warning の finding
  （`guide_step_unexplained`）にする。候補の対応・未実行・未確認は検査状態として返す。
- 順序・分岐・依存の検査（`answer_checks.check_guide_steps`）には、手順の説明にした段落の位置を
  渡す。

意味の近さの判定の embedding は回答の検証 1 回で多くて 1 回（OCI Generative AI の Cohere Embed。
検索と同じ）。使えない・失敗なら語の判定（安全側）に戻る。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from rag_engine.evaluation.guide_step_validation import validate_guide_steps
from rag_engine.generation.grounded import GUIDE_STEP_EXPLAINED, guide_step_gap_line
from rag_engine.retrieval.guide_steps import GuidePlan

from app.config import Settings
from app.rag.answer_checks import (
    AnswerFinding,
    check_guide_steps,
    conditional_steps,
    excluded_steps,
)
from app.schemas.support_guide import SupportGuideContent

Embed = Callable[[Sequence[str]], Sequence[Sequence[float]]]

GUIDE_STEP_UNEXPLAINED = "guide_step_unexplained"


def guide_step_plan(
    content: SupportGuideContent, conditions: Mapping[str, str]
) -> GuidePlan | None:
    """確かめる手順（外れた分岐の手順を除き、決まらない分岐の手順は required=False）。"""
    excluded = excluded_steps(content, conditions)
    conditional = conditional_steps(content, conditions)
    return GuidePlan.from_payload(
        "",
        1,
        [
            {
                "id": step.id,
                "title": step.title,
                "required": step.id not in conditional,
                "retrieval_hints": list(step.retrieval_hints),
                "references": [
                    {
                        "document_id": reference.document_id,
                        "title": reference.title,
                        "page_ranges": [
                            {"page_start": item.page_start, "page_end": item.page_end}
                            for item in reference.page_ranges
                        ],
                    }
                    for reference in step.references
                ],
            }
            for step in content.steps
            if step.id not in excluded
        ],
    )


def guide_step_embed(settings: Settings, loop: asyncio.AbstractEventLoop) -> Embed:
    """回答の検証の thread から event loop の embedding を呼ぶ関数（回答と同じ Cohere Embed）。"""
    from app.clients.genai import GenAiClient
    from app.rag.answer_engine import _LoopEmbedder

    return _LoopEmbedder(GenAiClient(settings=settings), loop).embed


def guide_step_findings(
    answer: str,
    content: SupportGuideContent,
    conditions: Mapping[str, str],
    claim_checks: Sequence[Mapping[str, Any]] | None,
    evidence: Sequence[Mapping[str, Any]],
    *,
    embed: Embed | None,
) -> tuple[list[AnswerFinding], list[dict[str, Any]], dict[str, Any] | None]:
    """手順の順序・分岐・依存と手順ごとの充足の finding、手順ごとの状態、embedding の計測。

    ``claim_checks`` が None（主張の監査の判定が無い）なら手順ごとの充足は確かめず、手順の名前の
    一致で順序だけを確かめる。``evidence`` は渡した根拠
    （``{id, document_id, page_start, page_end, text}``）。
    """
    plan = guide_step_plan(content, conditions)
    if claim_checks is None or plan is None:
        return check_guide_steps(answer, content, conditions), [], None
    result = validate_guide_steps(plan, claim_checks, evidence, embed=embed)
    steps: list[dict[str, Any]] = result["steps"]
    located = {step["step_id"]: step["answer_quote"] for step in steps if step["answer_quote"]}
    findings = check_guide_steps(answer, content, conditions, located)
    findings += [
        AnswerFinding(
            "guide_steps",
            GUIDE_STEP_UNEXPLAINED,
            "warning",
            guide_step_gap_line(step),
            step_id=step["step_id"],
        )
        for step in steps
        if step["required"]
        and step["status"] != GUIDE_STEP_EXPLAINED
        and step.get("validation", {}).get("body_coverage", {}).get("execution") == "completed"
        and step.get("validation", {}).get("body_coverage", {}).get("status")
        in {"missing", "partial"}
    ]
    return (
        findings,
        [{k: v for k, v in step.items() if k != "answer_quote"} for step in steps],
        result["embedding"],
    )


__all__ = [
    "GUIDE_STEP_UNEXPLAINED",
    "guide_step_embed",
    "guide_step_findings",
    "guide_step_plan",
]
