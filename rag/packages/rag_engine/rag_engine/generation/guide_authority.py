"""業務ガイドの要求範囲を、資料適用・主張支持の監査から独立して記録する。"""

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from rag_engine.models.llm import GroundedAudit


def bind_guide_authority(
    audit: GroundedAudit,
    task_scope: Sequence[dict[str, Any]],
    item_candidates: Mapping[int, Sequence[dict[str, Any]]],
) -> GroundedAudit:
    """コードで選んだ範囲を付ける。候補の共通出典は意味支持・充足の証明にしない。"""
    required = list(dict.fromkeys(str(row["step_id"]) for row in task_scope if row.get("step_id") and row.get("required") is True))
    unresolved = list(dict.fromkeys(str(row["step_id"]) for row in task_scope if row.get("step_id") and row.get("required") is False))
    valid = set(required) | set(unresolved)
    return audit.model_copy(
        update={
            "reviews": [
                row.model_copy(
                    update={
                        "business_scope": {
                            "source": "business_guide",
                            "required_step_ids": deepcopy(required),
                            "unresolved_step_ids": deepcopy(unresolved),
                            "candidate_step_ids": list(
                                dict.fromkeys(
                                    str(candidate["step_id"])
                                    for candidate in item_candidates.get(row.index, ())
                                    if candidate.get("step_id") in valid
                                )
                            ),
                            "association": "source_only",
                        }
                    }
                )
                for row in audit.reviews
            ]
        }
    )


def authoritative_guide_inputs(
    task_scope: Sequence[dict[str, Any]],
    steps: Sequence[tuple[str, str, Sequence[str]]],
    contracts: Sequence[dict[str, Any]],
) -> tuple[tuple[tuple[str, str, Sequence[str]], ...], tuple[str, ...], tuple[dict[str, Any], ...]]:
    """要求を検索成否から独立させる。根拠IDは既存の検索結果だけから引き継ぐ。"""
    sources = {identity: evidence for identity, _title, evidence in steps}
    previous = {row["step_id"]: row for row in contracts}
    selected_steps = []
    optional = []
    selected_contracts = []
    for row in task_scope:
        identity = str(row.get("step_id") or "")
        if not identity:
            continue
        selected_steps.append((identity, str(row.get("title") or ""), sources.get(identity, ())))
        if row.get("required") is not True:
            optional.append(identity)
        contract = deepcopy(previous.get(identity, {}))
        contract.update(
            step_id=identity,
            purpose=row.get("purpose", contract.get("purpose", "")),
            requirements=deepcopy(row.get("requirements", contract.get("requirements", []))),
            depends_on=deepcopy(row.get("depends_on", contract.get("depends_on", []))),
        )
        selected_contracts.append(contract)
    return tuple(selected_steps), tuple(optional), tuple(selected_contracts)


def compile_guide_task_contract(original_contract: Mapping[str, Any], task_scope: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Guideの決定済み要求を主タスクへ編成する。根拠の支持や小問の所属は推定しない。"""
    contract = deepcopy(dict(original_contract))
    if not task_scope:
        return contract
    fields = (
        "guide_id",
        "revision",
        "step_id",
        "title",
        "required",
        "purpose",
        "depends_on",
        "requirements",
        "evidence_requirements",
        "done_when",
    )
    scope = [{key: deepcopy(row[key]) for key in fields if key in row} for row in task_scope]
    required = [row for row in scope if row.get("required") is True]
    unresolved = [row for row in scope if row.get("required") is not True]
    original_request = str(contract.get("current_request") or "")
    contract["original_question"] = contract.get("original_question", original_request)
    contract["original_current_request"] = original_request
    contract["scope_sources"] = ["original_question", "business_guide"]
    contract["required_guide_scope"] = required
    contract["unresolved_guide_scope"] = unresolved
    if required:
        contract["current_request"] = "\n".join(
            value for value in (original_request, json.dumps({"required_guide_scope": required}, ensure_ascii=False)) if value
        )
        units = contract.get("request_units", [])
        targets = [row for row in units if row.get("kind") != "context"]
        if len(targets) == 1:
            unit = targets[0]
            unit["original_text"] = unit.get("text", "")
            unit["text"] = contract["current_request"]
            unit["scope_sources"] = ["original_question", "business_guide"]
    return contract
