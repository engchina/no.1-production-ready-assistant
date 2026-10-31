"""評価のケースの「回答の過程」を、評価の期待（手順・条件・根拠・対応）の下書きにする（#2402）。

構造化は業務ガイドの取り込み（#2397。`support_guide_process_import`）と同じ部品を使う: 見出しの語
（言語資源の `guide_import_cues.process_headings`）で節に分け、同じ LLM の呼び出しで業務ガイドの
内容（`SupportGuideContent`）の下書きにし、資料の名前を同じ規則でナレッジベースの文書に解決する。
ここでは、その下書きを評価のケースの期待に直す（LLM を呼ばない決定的な対応）。

- 手順 → `required_answer_points`: 手順の題名と説明を使い、意味上の網羅性を測る。
- 確認事項 → `required_conditions`: 条件の名前。
- 資料と頁 → 解決できた資料は正解の文書（`relevant_document_ids`）にし、頁・章節があれば必要な根拠
  （`required_evidence`）にする。解決できない・複数に当たる資料は下書きの警告にする。
- 対応 → `expected_outcomes`: 手順があれば回答・条件付きの回答、利用者に確かめる条件があれば回答前の
  確認、窓口への引き継ぎがあれば人への引き継ぎを受け入れる。

下書きは保存しない。画面で確かめてから評価のケースに足す。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from pydantic import ValidationError

from app.schemas.evaluation import (
    EvaluationEvidence,
    EvaluationOutcome,
    EvaluationProcessDraft,
    EvaluationProcessRow,
)
from app.schemas.support_guide import (
    SupportGuideContent,
    SupportGuideProcessItem,
    SupportGuideProcessReference,
    SupportGuideProcessRow,
)

# 頁の書き方（「12」「12-14」「p.12」「12〜14 頁」）。数字の範囲だけを読む。
_PAGE_RANGE = re.compile(r"(\d{1,6})\s*(?:[-~〜～–—]\s*(\d{1,6}))?")
_REFERENCE_CODES = frozenset(
    {
        "reference_unresolved",
        "reference_ambiguous",
        "reference_location_mismatch",
        "reference_location_unreadable",
    }
)


def guide_process_row(row: EvaluationProcessRow) -> SupportGuideProcessRow:
    """評価のケースの行を、業務ガイドの取り込みの行の形にする（同じ構造化に渡す）。"""
    return SupportGuideProcessRow(
        row=row.row,
        question=row.question,
        process=row.answer_process,
        standard_answer=row.standard_answer,
    )


def page_range(text: str) -> tuple[int, int | None] | None:
    """頁の書き方から最初の範囲（始まり, 終わり）を読む。読めなければ None。"""
    match = _PAGE_RANGE.search(unicodedata.normalize("NFKC", text or ""))
    if match is None:
        return None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else None
    if start < 1:
        return None
    if end is not None and end < start:
        end = None
    return start, end if end != start else None


def _step_terms(content: SupportGuideContent) -> list[str]:
    terms: list[str] = []
    for step in content.steps:
        term = " — ".join(part for part in (step.title.strip(), step.purpose.strip()) if part)
        if term and term not in terms:
            terms.append(term)
    return terms[:30]


def _outcomes(content: SupportGuideContent) -> list[EvaluationOutcome]:
    outcomes: list[EvaluationOutcome] = []
    if content.steps:
        outcomes += ["answered", "conditional"]
    if any(condition.unknown_handling == "ask" for condition in content.conditions):
        outcomes.append("needs_clarification")
    handoff = bool(content.handoff.contact.strip() or content.handoff.conditions) or any(
        condition.unknown_handling == "handoff" for condition in content.conditions
    )
    if handoff:
        outcomes.append("needs_human")
        if "conditional" not in outcomes:
            outcomes.append("conditional")
    return outcomes


def _evidence(
    references: Sequence[SupportGuideProcessReference],
) -> tuple[list[str], list[EvaluationEvidence]]:
    relevant: list[str] = []
    evidence: list[EvaluationEvidence] = []
    for reference in references:
        if reference.status != "resolved" or not reference.document_id:
            continue
        if reference.document_id not in relevant:
            relevant.append(reference.document_id)
        # 頁として読めた資料だけ（表計算の行・頁として読めない書き方は頁にしない。#2663）。
        pages = page_range(reference.pages) if reference.page_ranges else None
        section = reference.section.strip() or None
        if pages is None and section is None:
            continue
        label = " | ".join(
            part
            for part in (
                reference.file_name or reference.name,
                f"p.{reference.pages.strip()}" if pages else "",
                f"章節: {section}" if section else "",
            )
            if part
        )
        try:
            evidence.append(
                EvaluationEvidence(
                    id=f"p{len(evidence) + 1}",
                    document_id=reference.document_id,
                    page_start=pages[0] if pages else None,
                    page_end=pages[1] if pages else None,
                    section=section[:300] if section else None,
                    label=label[:1000],
                )
            )
        except ValidationError:
            continue
    return relevant, evidence[:30]


def process_draft(
    row: EvaluationProcessRow, item: SupportGuideProcessItem
) -> EvaluationProcessDraft:
    """業務ガイドの下書き（1 行）を、評価のケースの期待の下書きにする。"""
    warnings = [
        issue.message
        for issue in item.issues
        if issue.code in _REFERENCE_CODES or issue.severity == "warning"
    ]
    if item.content is None:
        errors = [issue.message for issue in item.issues if issue.code not in _REFERENCE_CODES]
        return EvaluationProcessDraft(
            case_id=row.case_id,
            row=row.row,
            references=item.references,
            warnings=warnings,
            error="".join(errors[:3]) or "回答の過程から期待の下書きを作れませんでした。",
        )
    content = item.content
    relevant, evidence = _evidence(item.references)
    return EvaluationProcessDraft(
        case_id=row.case_id,
        row=row.row,
        required_answer_points=_step_terms(content),
        required_conditions=list(
            dict.fromkeys(condition.label.strip() for condition in content.conditions)
        )[:30],
        expected_outcomes=_outcomes(content),
        relevant_document_ids=relevant,
        required_evidence=evidence,
        references=item.references,
        warnings=warnings,
    )


__all__ = ["guide_process_row", "page_range", "process_draft"]
