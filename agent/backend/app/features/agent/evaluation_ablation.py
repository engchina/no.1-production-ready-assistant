"""実行補助の寄与の比較（除去実験。#69）。

全構成の品質評価（基準）と、実行補助を 1 つずつ外した品質評価
（`EvaluationRequest.disabled_aids`）を、同じ評価セット・同じケース・同じ評価者・同じ版で並べ、
品質・完了の仕方・手間（モデル / ツール / MCP の呼び出し・token・差し戻し）・遅延（p50 / p95）の
差と、補助のコード経路が実際に動いたかを出す。結果は保存した評価の job と Run の記録だけから
作る（モデル・MCP を新しく呼ばない）。

- 品質の差は #62 の対応のある比較（`paired_comparison`。単位はケース、95% の MOVER 区間）と、
  悪化 / 改善したケース（`compare_jobs` の行）。
- 補助が動いたか（`aid_activity`）は Run の記録で数える: 文脈の縮小のイベント
  （`context_compaction`）・回答の検証の成果物（`answer_validation`。検証した）・差し戻し / 拒答の
  イベント（`tool_grounding`）。基準で動いていない補助は、外しても同じ（`not_applicable`）。
- 判定（`decision`）: `adopt`（外すと品質が確かに下がる。補助を続ける）、`reconsider`（外すと
  品質が確かに上がる）、`undetermined`（区間が 0 をまたぐ。小標本で差が見えないことは不要の
  証明ではない。手間・遅延の差だけを添える）、`not_applicable`、`not_comparable`（条件が違う）。
- RAG の検索の工程の寄与（MRR・再現率）は RAG の除去実験
  （`rag/backend/app/rag/evaluation_ablation.py`）が別に出す。ここでは Agent の完了品質と RAG の
  呼び出しの手間（`rag_calls`）を出し、検索が良くなっただけで最終の判断も良くなったとみなさない。
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

from app.features.agent.evaluation import (
    EvaluationJob,
    cases_fingerprint,
    compare_jobs,
    paired_comparison,
)
from app.features.agent.evaluation_aids import EVALUATION_AIDS
from app.features.agent.runtime import RunEventType, RunState
from app.features.agent.tools import MCP_TOOL_SEPARATOR

RAG_TOOL_NAMES = frozenset({"rag_search", "rag_retrieve_evidence"})


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    """最近接順位の分位（値が無ければ None）。"""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(quantile * len(ordered)) - 1)]


def _distribution(values: Sequence[float]) -> dict[str, float | None]:
    return {
        "total": sum(values) if values else None,
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
    }


def _aid_activity(run: RunState) -> dict[str, int]:
    """Run の中で補助のコード経路が動いた回数。"""
    events = [event for event in run.events if event.type == RunEventType.RUNTIME_EVENT]
    validated = [
        item
        for item in run.artifacts
        if item.kind == "answer_validation" and item.content.get("status") == "completed"
    ]
    return {
        "context_compaction": sum(1 for event in events if "context_compaction" in event.payload),
        "final_validation": len(validated),
        "tool_grounding": sum(1 for event in events if "tool_grounding" in event.payload),
    }


def _run_ids(job: EvaluationJob) -> list[str]:
    ids: list[str] = []
    for result in job.results:
        ids.extend(item.run_id for item in result.clarification_rounds)
        if result.run_id:
            ids.append(result.run_id)
    return ids


def _metrics(job: EvaluationJob, get_run: Callable[[str], RunState | None]) -> dict[str, Any]:
    runs = [run for run_id in _run_ids(job) if (run := get_run(run_id)) is not None]
    model_calls = [float(run.usage.requests) for run in runs if run.usage is not None]
    tokens = [float(run.usage.total_tokens) for run in runs if run.usage is not None]
    tools = [[step.tool_call.name for step in run.steps if step.tool_call] for run in runs]
    activity: Counter[str] = Counter()
    active_runs: Counter[str] = Counter()
    for run in runs:
        for aid, count in _aid_activity(run).items():
            activity[aid] += count
            active_runs[aid] += 1 if count else 0
    durations = [float(item.duration_ms) for item in job.results if item.duration_ms is not None]
    summary = job.summary
    return {
        "quality": {
            "pass_rate": summary.pass_rate,
            "pass_rate_interval": summary.pass_rate_interval,
            "judged_cases": summary.judged_cases,
            "correct": summary.correct,
        },
        # 完了の仕方（判定・実行の失敗・時間切れ・承認の停止・取消など）と、確認で終わったケース・
        # 調べずに断った回答（#2608）。
        "completion": {
            "statuses": dict(Counter(item.status.value for item in job.results)),
            "ended_with_clarification": sum(1 for r in job.results if r.ended_with_clarification),
            "refused_before_tools": sum(
                1 for r in job.results if r.refusal_stage == "before_tools"
            ),
        },
        "safety": {
            "violations": summary.safety_violations,
            "violation_cases": summary.safety_violation_cases,
        },
        "runs": len(runs),
        "model_calls": _distribution(model_calls),
        "tokens": _distribution(tokens),
        "tool_calls": _distribution([float(len(names)) for names in tools]),
        "mcp_calls": _distribution(
            [float(sum(1 for name in names if MCP_TOOL_SEPARATOR in name)) for names in tools]
        ),
        "rag_calls": _distribution(
            [
                float(
                    sum(
                        1
                        for name in names
                        if name.rsplit(MCP_TOOL_SEPARATOR, 1)[-1] in RAG_TOOL_NAMES
                    )
                )
                for names in tools
            ]
        ),
        "latency_ms": {"p50": _percentile(durations, 0.5), "p95": _percentile(durations, 0.95)},
        "aid_activity": {
            aid: {"events": activity[aid], "runs": active_runs[aid]} for aid in EVALUATION_AIDS
        },
    }


def _comparable(base: EvaluationJob, target: EvaluationJob) -> list[str]:
    reasons: list[str] = []
    if cases_fingerprint(base) != cases_fingerprint(target):
        reasons.append("評価ケースが違う")
    if base.agent_id != target.agent_id or base.agent_version != target.agent_version:
        reasons.append("業務 Agent か版が違う")
    if base.agent_version == "draft" and base.definition_hash != target.definition_hash:
        reasons.append("下書きの中身が違う")
    if (base.judge_fingerprint or target.judge_fingerprint) and (
        base.judge_fingerprint != target.judge_fingerprint
    ):
        reasons.append("評価者（判定のモデル・指示）が違う")
    if base.trials != target.trials:
        reasons.append("試行の回数が違う")
    return reasons


def _decision(
    base: dict[str, Any], target: dict[str, Any], removed: Sequence[str], paired: Any
) -> tuple[str, str]:
    active = [aid for aid in removed if base["aid_activity"][aid]["events"]]
    if not active:
        return (
            "not_applicable",
            "基準の評価で外した補助のコード経路が動いていない（対象のケースが無い）。外しても同じ。",
        )
    if paired is None:
        return "undetermined", "対応のある比較に使えるケースがありません。"
    low, high = paired.interval
    span = f"差 {paired.difference:+.2f}（95% 区間 {low:+.2f}〜{high:+.2f}、ケース {paired.cases}）"
    if paired.conclusive and paired.difference < 0:
        return "adopt", f"外すと品質が下がる: {span}。補助を続ける。"
    if paired.conclusive and paired.difference > 0:
        return "reconsider", f"外すと品質が上がる: {span}。補助の見直しを検討する。"
    costs = []
    for key, label in (("tokens", "token"), ("model_calls", "モデルの呼び出し")):
        before, after = base[key]["total"], target[key]["total"]
        if before and after is not None:
            costs.append(f"{label} {round((after - before) / before * 100):+d}%")
    before_p50, after_p50 = base["latency_ms"]["p50"], target["latency_ms"]["p50"]
    if before_p50 is not None and after_p50 is not None:
        costs.append(f"p50 {round(after_p50 - before_p50):+d} ms")
    return (
        "undetermined",
        f"品質の差は確かでない: {span}。差が見えないことは不要の証明ではない"
        + (f"（外したときの手間: {'・'.join(costs)}）。" if costs else "。"),
    )


def ablation_report(
    base: EvaluationJob,
    targets: Sequence[EvaluationJob],
    get_run: Callable[[str], RunState | None],
) -> dict[str, Any]:
    """基準（全構成）と、補助を外した評価の比較の表。"""
    base_metrics = _metrics(base, get_run)
    rows: list[dict[str, Any]] = []
    for target in targets:
        metrics = _metrics(target, get_run)
        reasons = _comparable(base, target)
        removed = [aid for aid in target.disabled_aids if aid not in base.disabled_aids]
        if sorted(base.disabled_aids) == sorted(target.disabled_aids):
            # 同じ構成をもう一度流した評価（順序・cache の温まりの目安）。
            name = "full-again" if not base.disabled_aids else "same-again"
        else:
            name = "no-" + "+".join(removed) if removed else "+".join(target.disabled_aids)
        paired = None if reasons else paired_comparison(base, target)
        comparison = compare_jobs(base, target)
        changed = {
            change: [
                row.question
                for row in comparison.rows
                if row.change == change and row.same_definition
            ]
            for change in ("regressed", "improved")
        }
        decision, reason = (
            ("not_comparable", "条件が違う: " + "・".join(reasons))
            if reasons
            else (
                ("baseline_repeat", "同じ構成の繰り返し（揺れ・順序の偏りの目安）")
                if not removed
                else _decision(base_metrics, metrics, removed, paired)
            )
        )
        observed = {
            aid: {
                "base": base_metrics["aid_activity"][aid]["events"],
                "target": metrics["aid_activity"][aid]["events"],
            }
            for aid in removed
        }
        rows.append(
            {
                "job_id": target.id,
                "name": name,
                "disabled_aids": list(target.disabled_aids),
                "created_at": target.created_at.isoformat(),
                "decision": decision,
                "reason": reason,
                "paired": paired.model_dump(mode="json") if paired else None,
                "regressed_questions": changed["regressed"],
                "improved_questions": changed["improved"],
                # 外した補助のコード経路が、基準では動き、外した評価では動かなかったか。
                "observed": observed,
                "metrics": metrics,
            }
        )
    return {
        "base": {
            "job_id": base.id,
            "disabled_aids": list(base.disabled_aids),
            "created_at": base.created_at.isoformat(),
            "agent_id": base.agent_id,
            "agent_version": base.agent_version,
            "set_id": base.set_id,
            "trials": base.trials,
            "judge_fingerprint": base.judge_fingerprint,
            "metrics": base_metrics,
        },
        # 流した順（最初の評価は DB・OS・モデル側の cache が冷えている。全構成をもう一度流すと
        # 順序の偏りの目安になる）。
        "order": [item.id for item in sorted([base, *targets], key=lambda job: job.created_at)],
        "rows": rows,
    }
