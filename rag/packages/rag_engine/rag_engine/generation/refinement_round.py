"""既存の回数・期限内で、選別した原文による任意の再回答を試す。"""

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, cast

from rag_engine.generation.evidence_refinement import RefinementStrategy, evidence_source_keys, select_refinement_evidence


@dataclass(frozen=True)
class RefinementResult:
    candidate: Any | None
    spans: tuple[dict[str, Any], ...]
    adopted: bool
    diagnostics: dict[str, Any]


def citation_observations(current: Any) -> list[dict[str, Any]]:
    """実際の引用位置の検査だけを選別へ渡し、意味支持と混同しない。"""
    from rag_engine.generation import grounded

    result = []
    for entry in current.checked:
        for own in grounded.item_citations(entry):
            proof = own.citation_resolution
            result.append(
                {
                    "evidence_id": own.span["evidence_id"] if own.span else proof.get("resolved_evidence_id", ""),
                    "citation_validity": "valid"
                    if proof.get("authenticity") == "verified" and proof.get("status") == "extracted"
                    else "unknown",
                    "content_usability": "unusable"
                    if proof.get("reason_code") in {"citation_heading_only", "citation_content_unusable"}
                    else proof.get("content_usability", "unknown"),
                    "claim_support": "unknown",
                    "applicability": "unknown",
                }
            )
    return result


def protected_sources(
    current: Any, knowledge: Any, known_gaps: Sequence[str]
) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]], list[str]]:
    """明示された必須・不明の対応を保持する。未引用だから不要とは推定しない。"""
    required: list[tuple[str, ...]] = []
    uncertain: list[tuple[str, ...]] = []
    unresolved: list[str] = []
    optional = set(knowledge.guide_optional_steps) if knowledge else set()
    for step_id, _title, sources in knowledge.guide_steps if knowledge else ():
        (uncertain if step_id in optional else required).append(tuple(sources))
        if not sources:
            unresolved.append(step_id)
    coverage = {row.request_id: row for row in current.draft.request_coverage}
    for request in current.requests:
        if request.get("kind") == "context":
            continue
        row = coverage.get(request["id"])
        sources = tuple(
            dict.fromkeys(
                [
                    *(row.evidence_ids if row else ()),
                    *((entry.evidence_id for entry in row.evidence_scope if entry.relation in {"direct", "linked"}) if row else ()),
                ]
            )
        )
        if sources:
            required.append(sources)
        else:
            unresolved.append(request["id"])
    if known_gaps:
        unresolved.extend(str(gap) for gap in known_gaps)
    return required, uncertain, unresolved


def review_feedback(current: Any) -> dict[str, Any]:
    """前回答は検査対象としてのみ渡す。削除した根拠のID・原文を混ぜない。"""
    return {
        "unverified_previous_draft": {
            "source_kind": "generated_draft_not_evidence",
            "items": [{"request_id": entry.item.request_id, "text": entry.item.text} for entry in current.checked if entry.published],
        }
    }


# 採否の理由コード（#3781）。``adoption`` は最初に当たった理由、``adoption_reasons`` は当たったすべて。
ADOPTED = "deterministic_checks_improved"
_RANGE_CODES = frozenset({"citation_range_missing", "citation_range_invalid", "citation_range_out_of_bounds"})
UNAUDITED_REFINEMENT_DETAIL = "根拠を絞った再回答で加えた・言い換えた説明で、監査なしのため意味の支持を確かめていない"


@dataclass(frozen=True)
class AdoptionDecision:
    adopted: bool
    reason: str
    reasons: tuple[str, ...]
    checks: dict[str, Any]
    candidate: Any


def _normalized(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))


def _anchors(entry: Any) -> set[tuple[str, str]]:
    """主張の根拠の位置（出典の永続 ID と抽出した原文）。表示ラベル・本文の言い回しに依らない。"""
    from rag_engine.generation import grounded

    result = set()
    for own in grounded.item_citations(entry):
        if own.span is None:
            continue
        proof = own.citation_resolution
        source = str(proof.get("source_id") or own.span.get("source_id") or own.span.get("evidence_id") or "")
        quote = _normalized(proof.get("resolved_quote") or own.item.quote or "")
        if source and quote:
            result.add((source, quote))
    return result


def _covers(anchors: set[tuple[str, str]], anchor: tuple[str, str]) -> bool:
    source, quote = anchor
    return any(other_source == source and quote in other_quote for other_source, other_quote in anchors)


def _claims(round_: Any) -> list[Any]:
    return [entry for entry in round_.checked if entry.confirmed and entry.item.kind != "gap"]


def _deterministic_counts(round_: Any) -> dict[str, int]:
    """決定的な検査の結果だけを数える（意味の支持は含めない）。"""
    from rag_engine.generation import grounded

    marks = [mark.get("code", "") for entry in round_.checked for own in grounded.item_citations(entry) for mark in own.marks]
    return {
        # 監査の判定を診断だけにする設定では ``Round.verified`` が 0 になるため、印の無い説明を直接数える。
        "confirmed": len(_claims(round_)),
        "marked": round_.marked,
        "citation_errors": sum(code in grounded.UNSOURCED_MARK_CODES and code not in _RANGE_CODES for code in marks)
        + sum(1 for entry in round_.dropped if not entry.get("ignored") and not entry.get("kept")),
        "citation_range_errors": len(grounded.citation_range_issues(round_.checked, round_.dropped)),
        "plan_errors": len(round_.timing.get("request_plan_errors") or ()),
        "required_guide_steps_explained": sum(
            bool(step.get("required")) and step.get("status") == grounded.GUIDE_STEP_EXPLAINED for step in round_.guide_steps
        ),
    }


def adoption_check(candidate: Any, current: Any, selection: Mapping[str, Any]) -> AdoptionDecision:
    """決定的な検査が良くなり、確認済みの主張と必須の根拠を失わない候補だけを採る（#3781）。

    主張の保持は本文の逐語ではなく、根拠の位置（出典と抽出した原文）で比べる。監査なし（none）では、加えた・
    言い換えた説明に ``unassessed`` の印を付けて採り、要約は監査なしの警告のまま出す。件数や短さだけでは採らない。
    """
    from rag_engine.generation import grounded

    reasons: list[str] = []
    if (
        selection.get("candidate_missing_required_groups")
        or selection.get("candidate_missing_uncertain_groups")
        or selection.get("unresolved_requirement_ids")
    ):
        reasons.append("required_evidence_not_preserved")
    if candidate.audit_cut:
        reasons.append("review_incomplete")
    if not any(entry.published and entry.item.kind != "gap" for entry in candidate.checked):
        reasons.append("no_answer_content")
    before, after = _deterministic_counts(current), _deterministic_counts(candidate)
    candidate_claims = _claims(candidate)
    candidate_anchors = set().union(*(_anchors(entry) for entry in candidate_claims))
    previous = _claims(current)
    # 根拠の位置を持たない確認済みの説明は、保持を確かめられないので失ったと扱う。
    lost = [
        entry for entry in previous if not (anchors := _anchors(entry)) or not all(_covers(candidate_anchors, anchor) for anchor in anchors)
    ]
    if lost:
        reasons.append("previous_claim_lost")
    for key, code in (
        ("citation_errors", "citation_errors_increased"),
        ("citation_range_errors", "citation_range_errors_increased"),
        ("plan_errors", "plan_errors_increased"),
    ):
        if after[key] > before[key]:
            reasons.append(code)
    if after["required_guide_steps_explained"] < before["required_guide_steps_explained"]:
        reasons.append("required_guide_step_lost")
    if current.addressed - candidate.addressed - candidate.withdrawn_requests:
        reasons.append("addressed_request_lost")
    unchanged = {
        (_normalized(entry.item.condition) + "\n" + _normalized(entry.item.text), anchor)
        for entry in previous
        for anchor in _anchors(entry)
    }

    def verbatim(entry: Any) -> bool:
        anchors = _anchors(entry)
        text = _normalized(entry.item.condition) + "\n" + _normalized(entry.item.text)
        return bool(anchors) and all((text, anchor) in unchanged for anchor in anchors)

    rewritten = [entry for entry in candidate_claims if not verbatim(entry)]
    rewritten_requests = {entry.item.request_id for entry in rewritten}
    gaps = {_normalized(entry.item.text) for entry in candidate.checked if entry.item.kind == "gap"}
    gap_requests = {entry.item.request_id for entry in candidate.checked if entry.item.kind == "gap"}
    if any(
        _normalized(entry.item.text) not in gaps
        and not (entry.item.request_id and entry.item.request_id in gap_requests | rewritten_requests)
        for entry in current.checked
        if entry.item.kind == "gap"
    ):
        reasons.append("previous_gap_resolution_unverified")
    improved = (
        after["confirmed"] >= before["confirmed"]
        and after["marked"] <= before["marked"]
        and (after["confirmed"] > before["confirmed"] or after["marked"] < before["marked"])
    )
    if not improved:
        reasons.append("no_deterministic_improvement")
    unaudited = candidate.timing.get("audit") == grounded.AUDIT_MODE_NONE
    checks: dict[str, Any] = {
        "audit_mode": "none" if unaudited else "audited",
        "before": before,
        "after": after,
        "previous_confirmed_claims": len(previous),
        "previous_confirmed_claims_lost": len(lost),
        "new_or_reworded_claims": len(rewritten),
        "summary_changed": _normalized(candidate.summary) != _normalized(current.summary),
        "unassessed_marked": 0,
    }
    if reasons:
        return AdoptionDecision(False, reasons[0], tuple(reasons), checks, candidate)
    if unaudited:
        # 監査なしでは、加えた・言い換えた説明の意味の支持を確かめていない。確かめた説明として出さない。
        for entry in rewritten:
            entry.mark("unassessed", UNAUDITED_REFINEMENT_DETAIL)
        checks["unassessed_marked"] = len(rewritten)
        candidate = replace(
            candidate,
            audit=grounded.deterministic_request_audit(candidate.requests, candidate.checked),
            summary_warning=candidate.summary_warning or ("監査なし" if str(candidate.summary or "").strip() else ""),
        )
    return AdoptionDecision(True, ADOPTED, (ADOPTED,), checks, candidate)


def refine_answer(
    current: Any,
    spans: Sequence[dict[str, Any]],
    *,
    strategy: str,
    knowledge: Any,
    known_gaps: Sequence[str],
    next_round: int,
    max_rounds: int,
    budget: Any,
    generate: Callable[[Sequence[dict[str, Any]], dict[str, Any]], Any],
    images_present: bool = False,
) -> RefinementResult:
    """追加の選別用モデルを呼ばず、任意の再生成だけを既存予算内で試す。"""
    original = tuple(spans)
    trace: dict[str, Any] = {
        "strategy": strategy,
        "execution": "not_run",
        "additional_generation_calls": 0,
        "semantic_verification": "not_performed_by_selection",
        "context_scope": "original_retrieval_history",
        "final_generation_evidence_scope": "initial_spans",
    }

    def skipped(reason: str) -> RefinementResult:
        return RefinementResult(None, original, False, {**trace, "stop_reason": reason})

    if strategy == "legacy":
        return skipped("disabled")
    if next_round >= max_rounds:
        return skipped("correction_limit")
    if images_present:
        return skipped("image_selection_unverified")
    required, uncertain, unresolved = protected_sources(current, knowledge, known_gaps)
    selection = select_refinement_evidence(
        spans,
        citation_observations(current),
        strategy=cast(RefinementStrategy, strategy),
        required_source_groups=required,
        uncertain_source_groups=uncertain,
        unresolved_requirement_ids=unresolved,
    )
    trace["selection"] = selection.diagnostics
    if selection.diagnostics.get("status") != "selected":
        return skipped(str(selection.diagnostics.get("reason") or "no_reduction"))
    if not budget.allows_round(next_round):
        return skipped("deadline")
    trace["additional_generation_calls"] = None
    try:
        with budget.round():
            candidate = generate(selection.spans, review_feedback(current))
    except PermissionError:
        raise
    except Exception as exc:
        return RefinementResult(
            None, original, False, {**trace, "execution": "error", "error_type": type(exc).__name__, "initial_answer_retained": True}
        )
    decision = adoption_check(candidate, current, selection.diagnostics)
    adopted, candidate = decision.adopted, decision.candidate
    trace.update(
        execution="completed",
        additional_generation_calls=candidate.timing.get("generation_calls", 1),
        adoption=decision.reason,
        adoption_reasons=list(decision.reasons),
        adoption_checks=decision.checks,
        adopted=adopted,
        initial_answer_retained=not adopted,
        final_generation_evidence_scope="selected_spans" if adopted else "initial_spans",
    )
    budget.note(candidate, {}, accepted=adopted)
    return RefinementResult(candidate, selection.spans, adopted, trace)


def prompt_context_for(context: Any, spans: Sequence[dict[str, Any]]) -> Any:
    """原文をコピーせず、選択範囲だけの安全確認用レコードを作る。"""
    from rag_engine.generation.answer_models import AnswerRecord

    records = [
        AnswerRecord(
            id=str(span["source_id"]),
            engine="",
            engine_label="",
            page=int(span.get("page") or 1),
            seq_no=index,
            category="Text",
            text=span["text"],
            source=span.get("source", ""),
        )
        for index, span in enumerate(spans)
    ]
    return replace(
        context, records=records, text="", evidence=(), evidence_tree=(), expansion_records=(), ranked_candidates=(), deferred_records=()
    )


def project_knowledge(knowledge: Any, spans: Sequence[dict[str, Any]]) -> Any:
    """業務の指示は保持し、根拠対応だけを今回提示する集合へ束縛する。"""
    if knowledge is None:
        return None
    keys = set().union(*(evidence_source_keys(span) for span in spans))
    return replace(
        knowledge,
        guide_steps=tuple(
            (step_id, title, tuple(source for source in sources if source in keys)) for step_id, title, sources in knowledge.guide_steps
        ),
    )


def project_crag_view(view: dict[str, Any] | None) -> dict[str, Any] | None:
    """再束縛できない評価原文を再回答へ混ぜず、未確認の観点は残す。"""
    if view is None:
        return None
    result = deepcopy(view)
    for key in ("aspect_checks", "answerable_parts"):
        for row in result.get(key, ()):
            row["evidence"] = [value for value in row.get("evidence", ()) if value.get("evidence_ids")]
            if row.get("status") in {"supported", "data_confirmation"} and not row["evidence"]:
                row["status"] = "unknown"
    result["generation_usable"] = any(row.get("status") == "supported" for row in result.get("answerable_parts", ()))
    return result
