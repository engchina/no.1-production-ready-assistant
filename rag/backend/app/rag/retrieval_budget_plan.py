"""既存の品質評価へ渡す比較要求と環境差分を、通信せずに準備する。"""

import argparse
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TypedDict

from pydantic import TypeAdapter

from app.config import Settings
from app.schemas.evaluation import EvaluationRagOverrides, EvaluationRunRequest

# 実行時の設定経路を持つ項目だけ。文書選択の無効化はbackendから指定できない。
_REQUEST_STAGES = {
    "reference": "reference_expansion_enabled",
    "entity": "entity_expansion_enabled",
    "structure": "graph_expansion_enabled",
    "section": "section_expansion_enabled",
}
_ENV_STAGES = {
    "diversity": {"RAG_CANDIDATE_DIVERSITY_MAX_EXTRA": "0"},
    "screen": {"RAG_SCREEN_LINKING_ENABLED": "false"},
    "article": {"RAG_ARTICLE_CATALOG_ENABLED": "false"},
}


class PlanVariant(TypedDict):
    id: str
    request: dict[str, Any]
    environment_overlay: dict[str, str]


class ComparisonPlan(TypedDict):
    schema_version: int
    request_sha256: str
    executed: bool
    requires_effective_settings_snapshot: bool
    unavailable_controls: list[str]
    variants: list[PlanVariant]


def prepare_plan(
    request: EvaluationRunRequest, *, total_caps: Sequence[int] = (), recovery_matrix: bool = False
) -> ComparisonPlan:
    """ケース・範囲・モデル以外の既存入力を保ち、一度に一項目だけ変える。"""
    if request.compare_search_answer_profile_id:
        raise ValueError("別プロファイルとの同時比較を外してから準備してください。")
    if any(value <= 0 for value in total_caps):
        raise ValueError("比較する総上限は正の整数で指定してください。")
    cap_adapter = TypeAdapter[int](
        Settings.model_fields["rag_rerank_candidate_limit"].rebuild_annotation()
    )
    for value in total_caps:
        cap_adapter.validate_python(value)
    baseline = request.model_dump(mode="json", exclude_none=True)
    variants: list[PlanVariant] = [
        {"id": "baseline", "request": baseline, "environment_overlay": {}}
    ]
    for name, key in _REQUEST_STAGES.items():
        overrides = (
            request.rag_overrides.model_dump(exclude_none=True) if request.rag_overrides else {}
        )
        overrides[key] = False
        changed = request.model_copy(update={"rag_overrides": EvaluationRagOverrides(**overrides)})
        variants.append(
            {
                "id": f"without-{name}",
                "request": changed.model_dump(mode="json", exclude_none=True),
                "environment_overlay": {},
            }
        )
    for name, overlay in _ENV_STAGES.items():
        variants.append(
            {"id": f"without-{name}", "request": baseline, "environment_overlay": overlay}
        )
    variants.append(
        {
            "id": "without-anchor-document-cap",
            "request": request.model_copy(
                update={
                    "rag_overrides": EvaluationRagOverrides(
                        **{
                            **(
                                request.rag_overrides.model_dump(exclude_none=True)
                                if request.rag_overrides
                                else {}
                            ),
                            "max_anchors_per_document": 0,
                        }
                    )
                }
            ).model_dump(mode="json", exclude_none=True),
            "environment_overlay": {},
        }
    )
    for cap in dict.fromkeys(total_caps):
        variants.append(
            {
                "id": f"total-cap-{cap}",
                "request": baseline,
                "environment_overlay": {"RAG_RERANK_CANDIDATE_LIMIT": str(cap)},
            }
        )
    if recovery_matrix:
        if total_caps:
            raise ValueError("補救の比較と候補上限の比較は別の実験にしてください。")
        variants = []
        for crag, coverage in ((False, False), (True, False), (False, True), (True, True)):
            overrides = (
                request.rag_overrides.model_dump(exclude_none=True) if request.rag_overrides else {}
            )
            changed = request.model_copy(
                update={
                    "rag_overrides": EvaluationRagOverrides(
                        **{**overrides, "answer_flow": "crag" if crag else "standard_rag"}
                    )
                }
            )
            variants.append(
                {
                    "id": f"recovery-crag-{int(crag)}-coverage-{int(coverage)}",
                    "request": changed.model_dump(mode="json", exclude_none=True),
                    "environment_overlay": {
                        "RAG_REQUEST_COVERAGE_RETRIEVAL_ENABLED": str(coverage).lower()
                    },
                }
            )
    digest = hashlib.sha256(
        json.dumps(baseline, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "request_sha256": digest,
        "executed": False,
        "requires_effective_settings_snapshot": True,
        "unavailable_controls": ["document_selection_disabled"],
        "variants": variants,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path, help="既存 EvaluationRunRequest JSON")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--total-cap", type=int, action="append", default=[])
    parser.add_argument(
        "--recovery-matrix",
        action="store_true",
        help="生成前 CRAG と生成後不足補查の四条件を準備する",
    )
    args = parser.parse_args(argv)
    request = EvaluationRunRequest.model_validate_json(args.request.read_text(encoding="utf-8"))
    plan = prepare_plan(request, total_caps=args.total_cap, recovery_matrix=args.recovery_matrix)
    # 既存結果や入力を上書きせず、新しい比較のディレクトリだけを作る。
    args.output_dir.mkdir(parents=True, exist_ok=False)
    for variant in plan["variants"]:
        name = variant["id"]
        (args.output_dir / f"{name}.json").write_text(
            json.dumps(variant["request"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (args.output_dir / f"{name}.env").write_text(
            "".join(f"{key}={value}\n" for key, value in variant["environment_overlay"].items()),
            encoding="utf-8",
        )
    (args.output_dir / "manifest.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
