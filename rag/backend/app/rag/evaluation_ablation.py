"""検索の工程ごとの除去実験（ablation）の構成と集計（#61）。

同じ評価セット・基準日・範囲・索引で、全部の工程を使う構成（full）と、工程を 1 つずつ無効にした
構成を流し（評価の上書き ``rag_overrides``。``evaluation_run_cli ablate``）、full との差を出す。

- 品質: 同じケースの対応のある差と区間・悪化したケース（#60 の ``compare_paired``）。
- 位置ごとの文書の再現率: 検索直後（``candidate_fusion``）・rerank の後（``rerank_output``）・生成に
  渡した根拠（``answer_context``）の選ばれた chunk に、期待する文書が入ったか（回答の診断の
  ``retrieval_stages``）。最終の引用だけの MRR とは別に、どの位置で根拠が落ちたかを見る。
- 時間: 評価全体の wall-clock とケースの時間の中央値、工程ごとの時間（回答の診断の
  ``execution_steps``。並列の工程の時間の合計を全体の時間として扱わない）。
- 呼び出し: 実の client が送った LLM・埋め込み・再ランクの request の数（再試行・失敗を含む）と、
  応答の ``usage`` の token（取れない値は None。0 にしない）。#59 の記録の session で数える。
- 工程が実際に動いたか: 工程の記録・呼び出しの数・品質のどれにも差が無ければ
  ``observed_effect: false``（非適用・対象データ無しの可能性。「効かない」と断定しない）。
  full で既に無効の工程は ``already_off`` で流さない。
- 取込の前置き・分割の比較は同じ chunk の集合の検索の切替ではないため、ここでは扱わない
  （別の文書レシピの成果物を比べる）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from statistics import median
from typing import Any, cast

from app.config import Settings
from app.rag.evaluation import evaluation_settings
from app.rag.evaluation_paired import compare_paired
from app.schemas.evaluation import (
    EVALUATION_METRIC_NAMES,
    EvaluationCaseResult,
    EvaluationMetrics,
    EvaluationPairedComparison,
    EvaluationRagOverrides,
)

# 工程の名前 → (full の設定の項目, 無効にする上書き)。
ABLATIONS: dict[str, tuple[str, dict[str, object]]] = {
    "rerank": ("rag_rerank_enabled", {"rerank_enabled": False}),
    "neighbor_children": ("rag_neighbor_child_count", {"neighbor_child_count": 0}),
    "reference_expansion": (
        "rag_reference_expansion_enabled",
        {"reference_expansion_enabled": False},
    ),
    "entity_expansion": ("rag_entity_expansion_enabled", {"entity_expansion_enabled": False}),
    "graph_expansion": ("rag_graph_expansion_enabled", {"graph_expansion_enabled": False}),
    "section_expansion": ("rag_section_expansion_enabled", {"section_expansion_enabled": False}),
}
# 評価の上書きで無効にできず、ここでは測らない工程（成功を装わずに表示する）。
UNSUPPORTED_STAGES: dict[str, str] = {
    "query_expansion": "質問の拡張は検索の方式（query_strategy）の選択で、単独の on / off が無い",
    "ingestion_preamble_and_chunking": "取込の前置き・分割は別の文書レシピの成果物を比べる",
    "image_channel": "画像の検索・回答時の原画像の添付は評価の上書きで切り替えられない",
}
# 根拠の位置（回答の診断の retrieval_stages の stage）。
EVIDENCE_POSITIONS: dict[str, str] = {
    "candidate_fusion": "検索直後",
    "rerank_output": "rerank の後",
    "answer_context": "生成に渡した根拠",
}
FULL = "full"
FULL_AGAIN = "full-again"
# 品質の差の比較から外す、比べたい違い（上書きと設定の fingerprint）以外の条件の差は比較不能にする。
_EXPECTED_DIFFERENCES = ("request.rag_overrides", "settings.rag_config_fingerprint")


@dataclass(frozen=True)
class AblationConfig:
    """1 つの構成（``name`` は full / no-<工程> / full-again）。"""

    name: str
    stage: str | None
    overrides: EvaluationRagOverrides | None
    skipped_reason: str | None = None


@dataclass
class AblationRun:
    """1 つの構成を流した結果（外部の呼び出しの記録と wall-clock の秒）。"""

    config: AblationConfig
    metrics: EvaluationMetrics
    calls: list[dict[str, Any]] = field(default_factory=list)
    wall_seconds: float = 0.0


def ablation_configs(
    settings: Settings,
    overrides: EvaluationRagOverrides | None,
    stages: Sequence[str],
    *,
    full_again: bool = False,
) -> list[AblationConfig]:
    """full と、工程を 1 つずつ無効にした構成（full で既に無効なら流さない）。"""
    unknown = [stage for stage in stages if stage not in ABLATIONS]
    if unknown:
        raise ValueError(
            f"除去できない工程です: {', '.join(unknown)}（選べる: {', '.join(ABLATIONS)}）"
        )
    effective = evaluation_settings(settings, overrides)
    base = overrides.model_dump(exclude_none=True) if overrides is not None else {}
    configs = [AblationConfig(name=FULL, stage=None, overrides=overrides)]
    for stage in stages:
        attribute, off = ABLATIONS[stage]
        name = f"no-{stage}"
        if not getattr(effective, attribute):
            configs.append(AblationConfig(name, stage, None, skipped_reason="already_off"))
            continue
        configs.append(
            AblationConfig(name, stage, EvaluationRagOverrides.model_validate({**base, **off}))
        )
    if full_again:
        configs.append(AblationConfig(name=FULL_AGAIN, stage=None, overrides=overrides))
    return configs


def ablation_report(
    runs: Sequence[AblationRun],
    skipped: Sequence[AblationConfig],
    *,
    mode: str,
    seed: int,
    resamples: int,
    min_cases: int,
) -> dict[str, Any]:
    """full と各構成の、品質・根拠の位置・時間・呼び出しの差の表。"""
    by_name = {run.config.name: run for run in runs}
    full = by_name[FULL]
    summaries = {run.config.name: run_summary(run) for run in runs}
    stages: list[dict[str, Any]] = []
    for run in runs:
        if run.config.stage is None:
            continue
        stages.append(
            _stage_report(
                full, run, summaries, mode=mode, seed=seed, resamples=resamples, min_cases=min_cases
            )
        )
    for config in skipped:
        stages.append(
            {"stage": config.stage, "config": config.name, "status": config.skipped_reason}
        )
    report: dict[str, Any] = {
        "mode": mode,
        "as_of": (full.metrics.run_manifest or {}).get("as_of"),
        "order": [run.config.name for run in runs],
        "cache": (
            "埋め込み・再ランクの cache は構成ごとに空から始まり、構成の中のケースの間では効く。"
            "DB・OS の cache は最初の構成の後は温まる（順は order。full-again で順の偏りを見る）。"
        ),
        "timing": "replay（記録の応答。実の速さではない）" if mode == "replay" else "live",
        "seed": seed,
        "resamples": resamples,
        "min_cases": min_cases,
        "runs": summaries,
        "stages": stages,
        "unsupported": UNSUPPORTED_STAGES,
    }
    if FULL_AGAIN in by_name:
        # 同じ構成を流し直した差（生成の揺れ・順の偏りの目安）。工程の差はこれと比べて読む。
        again = compare_paired(
            full.metrics,
            by_name[FULL_AGAIN].metrics,
            seed=seed,
            resamples=resamples,
            min_cases=min_cases,
        )
        report["full_again"] = {
            "quality": _quality(again),
            "wall_seconds": _delta(
                summaries[FULL]["wall_seconds"], summaries[FULL_AGAIN]["wall_seconds"]
            ),
        }
    return report


def run_summary(run: AblationRun) -> dict[str, Any]:
    """1 構成の時間・呼び出し・工程・根拠の位置の集計。"""
    cases = run.metrics.case_results
    elapsed = [case.elapsed_ms for case in cases]
    return {
        "overrides": run.config.overrides.model_dump(mode="json", exclude_none=True)
        if run.config.overrides
        else {},
        "wall_seconds": run.wall_seconds,
        "case_median_ms": median(elapsed) if elapsed else None,
        "error_count": run.metrics.error_count,
        "metrics": {name: getattr(run.metrics, name) for name in EVALUATION_METRIC_NAMES},
        "calls": _call_counts(run.calls),
        "tokens": _tokens(run.calls),
        "steps": _steps(run.metrics),
        "evidence_recall": _evidence_recall(run.metrics),
    }


def _stage_report(
    full: AblationRun,
    run: AblationRun,
    summaries: Mapping[str, Mapping[str, Any]],
    *,
    mode: str,
    seed: int,
    resamples: int,
    min_cases: int,
) -> dict[str, Any]:
    paired = compare_paired(
        full.metrics, run.metrics, seed=seed, resamples=resamples, min_cases=min_cases
    )
    unexpected = [
        path for path in paired.condition_differences if not path.startswith(_EXPECTED_DIFFERENCES)
    ]
    reasons = [*paired.reasons, *(f"condition_differs:{path}" for path in unexpected)]
    left, right = summaries[full.config.name], summaries[run.config.name]
    steps = _changed_steps(left["steps"], right["steps"])
    calls = {
        kind: _delta(left["calls"].get(kind, 0), right["calls"].get(kind, 0))
        for kind in sorted(left["calls"].keys() | right["calls"].keys())
    }
    quality = _quality(paired)
    observed = (
        bool(steps)
        or any(item["difference"] for item in calls.values())
        or any(
            item["verdict"] != "unchanged"
            for item in quality.values()
            if item["verdict"] != "not_measured"
        )
    )
    return {
        "stage": run.config.stage,
        "config": run.config.name,
        "status": "not_comparable" if reasons else "compared",
        "reasons": reasons,
        "observed_effect": observed,
        "changed_steps": steps,
        "quality": quality,
        "evidence_recall": {
            position: _delta(
                left["evidence_recall"].get(position), right["evidence_recall"].get(position)
            )
            for position in EVIDENCE_POSITIONS
        },
        "wall_seconds": _delta(left["wall_seconds"], right["wall_seconds"]),
        "case_median_ms": _delta(left["case_median_ms"], right["case_median_ms"]),
        "calls": calls,
        "tokens": {
            name: _delta((left["tokens"] or {}).get(name), (right["tokens"] or {}).get(name))
            for name in ("input", "output")
        },
        "timing": "replay" if mode == "replay" else "live",
    }


def _quality(paired: EvaluationPairedComparison) -> dict[str, dict[str, Any]]:
    """指標ごとの差・区間・判定と、悪化したケースの id。"""
    return {
        item.metric: {
            "mean_difference": item.mean_difference,
            "interval": item.interval,
            "verdict": item.verdict,
            "paired_cases": item.paired_cases,
            "error_cases": item.error_cases,
            "regressed_case_ids": [
                case.case_id
                for case in paired.cases
                if (case.differences.get(item.metric) or 0.0) < 0
            ],
        }
        for item in paired.metrics
    }


def _delta(left: float | None, right: float | None) -> dict[str, float | None]:
    difference = None if left is None or right is None else right - left
    return {"full": left, "ablated": right, "difference": difference}


def _call_counts(calls: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """実の client が送った request の数（種類ごと。``llm`` は生成・判定。失敗は ``failed``）。"""
    counts: dict[str, int] = {}
    for call in calls:
        kind = "llm" if call.get("kind") == "http" else str(call.get("kind"))
        counts[kind] = counts.get(kind, 0) + 1
        status = (call.get("result") or {}).get("status")
        if "error" in call or (isinstance(status, int) and status >= 400):
            counts["failed"] = counts.get("failed", 0) + 1
    return counts


def _tokens(calls: Sequence[Mapping[str, Any]]) -> dict[str, int] | None:
    """LLM の応答の ``usage`` の token の合計（どの応答にも無ければ None。0 にしない）。"""
    usages = [usage for call in calls if call.get("kind") == "http" and (usage := _usage(call))]
    if not usages:
        return None
    return {
        "input": sum(int(usage.get("input_tokens") or 0) for usage in usages),
        "output": sum(int(usage.get("output_tokens") or 0) for usage in usages),
        "responses_with_usage": len(usages),
    }


def _usage(call: Mapping[str, Any]) -> Mapping[str, Any] | None:
    body = (call.get("result") or {}).get("body")
    if not isinstance(body, str):
        return None
    try:
        usage = json.loads(body).get("usage")
        return usage if isinstance(usage, Mapping) else None
    except (json.JSONDecodeError, AttributeError):
        pass
    # stream（SSE）の応答は、完了の event の response に usage がある。
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[5:])
        except json.JSONDecodeError:
            continue
        usage = (event.get("response") or {}).get("usage") if isinstance(event, dict) else None
        if isinstance(usage, Mapping):
            return usage
    return None


def _steps(metrics: EvaluationMetrics) -> dict[str, dict[str, float]]:
    """回答の工程ごとの、動いたケースの数・時間の合計・LLM の呼び出し（``execution_steps``）。"""
    steps: dict[str, dict[str, float]] = {}
    for case in metrics.case_results:
        for step in _answer(case).get("execution_steps") or []:
            if not isinstance(step, Mapping) or step.get("status") != "complete":
                continue
            item = steps.setdefault(
                str(step.get("name")), {"cases": 0, "seconds": 0.0, "llm_calls": 0}
            )
            item["cases"] += 1
            item["seconds"] += float(step.get("elapsed_seconds") or 0.0)
            item["llm_calls"] += int(step.get("llm_calls") or 0)
    return steps


def _changed_steps(
    left: Mapping[str, Mapping[str, float]], right: Mapping[str, Mapping[str, float]]
) -> dict[str, dict[str, float]]:
    """動いたケースの数が変わった工程（full → ablated）。"""
    return {
        name: {
            "full": left.get(name, {}).get("cases", 0),
            "ablated": right.get(name, {}).get("cases", 0),
        }
        for name in sorted(left.keys() | right.keys())
        if left.get(name, {}).get("cases", 0) != right.get(name, {}).get("cases", 0)
    }


def _evidence_recall(metrics: EvaluationMetrics) -> dict[str, float | None]:
    """位置ごとの、期待する文書が選ばれた chunk に入った割合の平均（記録の無い位置は None）。"""
    values: dict[str, list[float]] = {position: [] for position in EVIDENCE_POSITIONS}
    for case in metrics.case_results:
        relevant = set(case.relevant_document_ids)
        if not relevant or case.status == "error":
            continue
        selected: dict[str, set[str]] = {}
        for stage in _answer(case).get("retrieval_stages") or []:
            if isinstance(stage, Mapping) and stage.get("stage") in EVIDENCE_POSITIONS:
                documents = {str(item).split(":", 1)[0] for item in stage.get("selected_ids") or []}
                selected.setdefault(str(stage["stage"]), set()).update(documents)
        for position, documents in selected.items():
            values[position].append(len(relevant & documents) / len(relevant))
    return {
        position: (sum(items) / len(items) if items else None) for position, items in values.items()
    }


def _answer(case: EvaluationCaseResult) -> dict[str, Any]:
    return cast(dict[str, Any], case.diagnostics.answer or {})


__all__ = [
    "ABLATIONS",
    "EVIDENCE_POSITIONS",
    "FULL",
    "FULL_AGAIN",
    "UNSUPPORTED_STAGES",
    "AblationConfig",
    "AblationRun",
    "ablation_configs",
    "ablation_report",
    "run_summary",
]
