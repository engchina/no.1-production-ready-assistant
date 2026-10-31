"""生成後の根拠選別。原文の書換えや意味支持の判定は行わない。"""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal

RefinementStrategy = Literal["legacy", "cited_only", "cited_required"]


@dataclass(frozen=True)
class EvidenceSelection:
    spans: tuple[dict[str, Any], ...]
    diagnostics: dict[str, Any]


def _identity(span: Mapping[str, Any]) -> set[str]:
    return {str(value) for key in ("evidence_id", "source_id", "label") if (value := span.get(key))}


def evidence_source_keys(span: Mapping[str, Any]) -> set[str]:
    """当該spanに明示された出典・子・別名だけを照合用に返す。"""
    return (
        _identity(span)
        | {str(value) for key in ("source_aliases", "scope_source_ids") for value in span.get(key, ()) or () if value}
        | {str(row["id"]) for row in span.get("child_ranges", ()) or () if isinstance(row, Mapping) and row.get("id")}
    )


def _clean_context(span: Mapping[str, Any], excluded_ids: set[str]) -> dict[str, Any]:
    selected = deepcopy(dict(span))
    for key in ("answer_context", "function_texts", "adjacent_texts", "screen_texts", "document_texts", "limits", "metadata"):
        selected.pop(key, None)
    if "source_context" in selected:
        selected["source_context"] = {
            "document_name": str(span.get("source") or ""),
            "section_path": deepcopy(span.get("section_path") or []),
        }
    for key in ("source_aliases", "scope_source_ids"):
        if key in selected:
            selected[key] = [value for value in selected[key] if str(value) not in excluded_ids]
    if "child_ranges" in selected:
        selected["child_ranges"] = [
            row for row in selected["child_ranges"] if not isinstance(row, Mapping) or str(row.get("id", "")) not in excluded_ids
        ]
    return selected


def select_refinement_evidence(
    spans: Sequence[dict[str, Any]],
    citation_observations: Sequence[Mapping[str, Any]],
    *,
    strategy: RefinementStrategy = "legacy",
    required_source_groups: Sequence[Sequence[str]] = (),
    uncertain_source_groups: Sequence[Sequence[str]] = (),
    unresolved_requirement_ids: Sequence[str] = (),
) -> EvidenceSelection:
    """初回の基本引用検査と、呼出元が明示した保全関係だけで候補を選ぶ。

    citation_validity=valid は引用位置の確認だけを表す。claim_support・applicabilityは観察記録であり、
    採否には使わない。cited_only は保全漏れも記録する比較用候補で、最終採用は呼出元が判断する。
    cited_required は未定位の必須・不明関係があれば元集合を保つ。
    """
    original = tuple(spans)
    diagnostics: dict[str, Any] = {
        "strategy": strategy,
        "input_stage": "prepared_for_generation",
        "original_evidence_ids": [span.get("evidence_id", "") for span in spans],
        "citation_observations": [],
        "candidate_missing_required_groups": [],
        "candidate_missing_uncertain_groups": [],
        "unresolved_requirement_ids": list(unresolved_requirement_ids),
        "semantic_support_adopted": False,
    }

    def unchanged(reason: str) -> EvidenceSelection:
        return EvidenceSelection(original, {**diagnostics, "status": "unchanged", "reason": reason})

    if strategy == "legacy":
        return unchanged("legacy")
    if strategy not in {"cited_only", "cited_required"}:
        return unchanged("invalid_strategy")
    try:
        ids = [str(span["evidence_id"]) for span in spans]
        labels = [str(span["label"]) for span in spans]
        if len(set(ids)) != len(ids) or len(set(labels)) != len(labels) or not all(ids) or not all(labels):
            return unchanged("ambiguous_evidence_identity")
        cited: set[str] = set()
        for observation in citation_observations:
            identity = str(observation.get("evidence_id") or "")
            validity = observation.get("citation_validity", "unknown")
            usable = observation.get("content_usability", "unknown")
            diagnostics["citation_observations"].append(
                {
                    "evidence_id": identity,
                    "citation_validity": validity,
                    "content_usability": usable,
                    "claim_support": observation.get("claim_support", "unknown"),
                    "applicability": observation.get("applicability", "unknown"),
                    "identity_found": identity in ids,
                }
            )
            if identity in ids and validity == "valid" and usable != "unusable":
                cited.add(identity)
        if not cited:
            return unchanged("no_valid_citations")
        selected_ids = set(cited)
        groups = [("required", group) for group in required_source_groups] + [("uncertain", group) for group in uncertain_source_groups]
        group_matches = [
            (kind, list(group), {str(span["evidence_id"]) for span in spans if evidence_source_keys(span) & set(group)})
            for kind, group in groups
        ]
        if strategy == "cited_required":
            if unresolved_requirement_ids or any(not matches for _kind, _group, matches in group_matches):
                return unchanged("unresolved_preservation_scope")
            for _kind, _group, matches in group_matches:
                selected_ids.update(matches)
        selected = [span for span in spans if span["evidence_id"] in selected_ids]
        excluded = [span for span in spans if span["evidence_id"] not in selected_ids]
        excluded_ids = set().union(*(evidence_source_keys(span) for span in excluded)) - set().union(
            *(_identity(span) for span in selected)
        )
        candidate = tuple(_clean_context(span, excluded_ids) for span in selected)
        for kind, group, _matches in group_matches:
            if not any(evidence_source_keys(span) & set(group) for span in candidate):
                diagnostics[f"candidate_missing_{kind}_groups"].append(group)
        if strategy == "cited_required" and (
            diagnostics["candidate_missing_required_groups"] or diagnostics["candidate_missing_uncertain_groups"]
        ):
            return unchanged("preservation_reference_removed")
        diagnostics.update(
            status="selected",
            reason="candidate_only",
            selected_evidence_ids=[span["evidence_id"] for span in candidate],
            excluded_evidence_ids=[span["evidence_id"] for span in excluded],
            label_policy="preserved",
            context_policy="own_source_identity_only",
        )
        return EvidenceSelection(candidate, diagnostics)
    except Exception as exc:  # 選別だけの失敗で、検査済みの元集合を失わない。
        diagnostics["selection_error_type"] = type(exc).__name__
        return unchanged("selection_failed")
