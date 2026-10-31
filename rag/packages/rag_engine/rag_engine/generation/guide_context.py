"""実際の引用と業務ガイドの、出典だけに基づく監査入力の対応。"""

from collections.abc import Iterable, Sequence
from copy import deepcopy
from typing import Any


def audit_guide_candidates(
    guide_steps: Sequence[dict[str, Any]],
    contracts: Sequence[dict[str, Any]],
    actual_citation_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """原文定位済みの引用IDと手順の根拠IDが重なる候補を入力順に展開する。

    呼出側が当該説明の有効な引用IDだけを渡す。候補は出典の関連であり、意味支持・
    条件適合・要件の充足を証明しない。候補が無いことも材料不足の証明ではない。
    共有する根拠は複数手順に対応でき、一つの手順へ排他的に割り当てない。
    """
    cited = {identity for identity in actual_citation_ids if identity}
    result: list[dict[str, Any]] = []
    for step in guide_steps:
        step_id = step.get("step_id")
        if not step_id:
            continue
        matched = list(dict.fromkeys(identity for identity in step.get("evidence_ids", ()) if identity in cited))
        if not matched:
            continue
        requirements = [
            deepcopy(requirement)
            for contract in contracts
            if contract.get("step_id") == step_id
            for requirement in contract.get("requirements", ())
        ]
        result.append(
            {
                "step_id": step_id,
                "association": "source_only",
                "matched_evidence_ids": matched,
                "candidate_requirements": requirements,
            }
        )
    return result
