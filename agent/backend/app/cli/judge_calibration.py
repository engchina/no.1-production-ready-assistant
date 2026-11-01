"""品質評価の評価者（LLM による判定）を人手の判定で校正する（#66）。

品質評価の 2 つの LLM の判定を、人が判定した校正セット（合成）と照らして、評価者自身の誤りを測る。

- `answer_points`: 回答が期待する要点を満たすか（`evaluation.judge_answer`。品質評価の job と同じ
  指示・モデル・再試行）。correct → pass、incorrect → fail、uncertain → 判定できなかった件。
- `output_meaning`: 比べ方が「意味が同じ」の文字の項目の判定（`evaluation.judge_output_meaning`）。
  same → pass、別 → fail。判定に失敗した項目は判定できなかった件。

一致の集計・校正の合否の閾値は、RAG の回答の評価器の校正
（`rag/backend/app/rag/answer_judge_calibration.py`）と同じ platform の `pr_evaluation_scoring`
（`agreement`・`CalibrationThresholds`・`calibration_failures`）で、結果の形（`aspects.<観点>.splits.<dev|holdout>`・`passed`・`failures`・`provenance`）もそろえる。
合否は holdout の人手の判定だけで決め、dev は指示の調整に使う。観点（`perspective`。否定・数値・
日付・条件・長さや丁寧さの偏り・回答の中の評価者への指示など）ごとの内訳も出す。

安全・状態の遷移（未実行の完了報告・禁止ツールの実行・承認の迂回）は品質評価のコードの判定
（`evaluation.safety_check`）が正本で、ここで測る LLM の判定はそれを上書きしない（`unexecuted_completion` の観点は、評価者が
未実行の完了報告を誤って合格にする率を見るためのもの）。

    cd agent/backend
    SET=../evaluation/judge-calibration/agent-judge-calibration.v1.json
    W=../../tmp/judge-calibration
    # 判定のモデルを呼んで校正する（品質評価と同じ既定のテキストモデル。システム設定 > モデル）
    uv run python -m app.cli.judge_calibration $SET --output $W/agent.json
    # 指示・モデルを変えた後、前の結果を基線にして違いを出す
    uv run python -m app.cli.judge_calibration $SET --baseline $W/agent.json --output $W/v2.json
    # 人手の判定だけを直したら、前の判定を使って集計だけやり直す（モデルを呼ばない）
    uv run python -m app.cli.judge_calibration $SET --judgements $W/agent.json --output $W/v3.json

手順と結果の読み方は agent/docs/agent-judge-calibration.md。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pr_backend_core.bounded_calls import BoundedCallAborted
from pr_evaluation_scoring import (
    CalibrationThresholds,
    LabelPair,
    agreement,
    calibration_failures,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

REPORT_SCHEMA_VERSION = 1
PASS = "pass"  # nosec B105 - 判定のラベル（資格情報ではない）
FAIL = "fail"
HUMAN_LABELS = (PASS, FAIL)
SPLITS = ("dev", "holdout", "synthetic")
ANSWER_POINTS = "answer_points"
OUTPUT_MEANING = "output_meaning"
ASPECTS = (ANSWER_POINTS, OUTPUT_MEANING)
# 観点 → 説明（校正セットの `perspective`。ここに無い値は読み込みで断る）。
PERSPECTIVES: dict[str, str] = {
    "fact_consistency": "事実の整合（起点・対象・役割の取り違え）",
    "unexecuted_completion": "未実行の操作の完了報告",
    "expected_points": "期待する要点の欠落",
    "meaning_match": "文字の項目の意味の一致・言い換え",
    "negation": "否定・有無の反転",
    "number": "数値の違い・表記の違い",
    "date": "日付の違い・表記の違い",
    "condition": "条件の欠落・反転",
    "length_bias": "長さによる偏り",
    "politeness_bias": "丁寧さによる偏り",
    "order_bias": "並び順による偏り（順序に意味がある / 無い）",
    "judge_injection": "回答の中の評価者への指示",
}

Label = Literal["pass", "fail"]
Status = Literal["judged", "uncertain", "incomplete", "failed"]


class CalibrationItem(BaseModel):
    """校正セットの 1 件（RAG の LabelSet と同じ名前の項目 + 観点・人の根拠）。

    `answer_points` は `question`（質問）・`standard_answer`（期待する回答の要点）・`answer`（実際の
    回答）・`clarifications`（問い返しへの答え）を、`output_meaning` は `question`（項目の名前）・
    `standard_answer`（期待する値）・`answer`（記録した値）を使う。`note` は人の判定の根拠。
    """

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1)
    split: Literal["dev", "holdout"]
    aspect: Literal["answer_points", "output_meaning"]
    perspective: str
    question: str = Field(..., min_length=1)
    standard_answer: str = Field(..., min_length=1)
    answer: str
    clarifications: list[str] = Field(default_factory=list)
    label: Label
    note: str = Field(..., min_length=1)
    origin: Literal["human", "synthetic"] = "human"

    @field_validator("perspective")
    @classmethod
    def _known_perspective(cls, value: str) -> str:
        if value not in PERSPECTIVES:
            raise ValueError(f"知らない観点です: {value}（{', '.join(PERSPECTIVES)}）")
        return value


class CalibrationSet(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rubric_id: str = Field(..., min_length=1)
    description: str = ""
    items: list[CalibrationItem] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> CalibrationSet:
        counts = Counter(item.id for item in self.items)
        duplicated = sorted(item_id for item_id, count in counts.items() if count > 1)
        if duplicated:
            raise ValueError(f"項目の ID が重なっています: {', '.join(duplicated)}")
        return self


class Judgement(BaseModel):
    """評価者の 1 件の判定（`label` が None なら判定できなかった件。理由は `status`・`reason`）。"""

    aspect: str
    label: Label | None
    status: Status
    reason: str = ""
    verdict: str | None = None
    score: float | None = None
    missing_points: list[str] = Field(default_factory=list)
    error_type: str | None = None
    attempts: int = 0


def load_set(path: Path) -> CalibrationSet:
    return CalibrationSet.model_validate_json(path.read_text(encoding="utf-8"))


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _bucket(item: CalibrationItem) -> str:
    return "synthetic" if item.origin == "synthetic" else item.split


def _summary(pairs: Sequence[LabelPair]) -> dict[str, Any]:
    result = agreement(pairs, HUMAN_LABELS, positive=PASS, negative=FAIL)
    return {
        **asdict(result),
        "false_pass_items": [p.item_id for p in pairs if p.human == FAIL and p.judge == PASS],
        "false_fail_items": [p.item_id for p in pairs if p.human == PASS and p.judge == FAIL],
        "unjudged_items": [p.item_id for p in pairs if p.judge is None],
    }


def calibrate(
    calibration_set: CalibrationSet,
    judgements: Mapping[str, Judgement],
    *,
    thresholds: CalibrationThresholds,
    evaluator: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """評価者の判定と人手の判定の一致を観点（aspect）・区分・観点の内訳ごとに集計する。

    合否は aspect ごとに holdout の人手の項目だけで決める（`calibration_failures`）。判定の無い
    項目は ValueError（黙って判定できなかった件にしない）。合成の項目（`origin=synthetic`）は
    合否に入れない。
    """
    missing = [item.id for item in calibration_set.items if item.id not in judgements]
    if missing:
        raise ValueError(f"判定の無い項目があります: {', '.join(missing[:10])}")
    aspects: dict[str, Any] = {}
    for aspect in ASPECTS:
        items = [item for item in calibration_set.items if item.aspect == aspect]
        if not items:
            continue

        def pair(item: CalibrationItem) -> LabelPair:
            return LabelPair(item_id=item.id, human=item.label, judge=judgements[item.id].label)

        splits = {
            split: _summary([pair(item) for item in items if _bucket(item) == split])
            for split in SPLITS
            if any(_bucket(item) == split for item in items)
        }
        perspectives = {
            perspective: {
                split: _summary(
                    [pair(i) for i in items if i.perspective == perspective and _bucket(i) == split]
                )
                for split in SPLITS
                if any(i.perspective == perspective and _bucket(i) == split for i in items)
            }
            for perspective in PERSPECTIVES
            if any(item.perspective == perspective for item in items)
        }
        holdout = [pair(item) for item in items if _bucket(item) == "holdout"]
        failures = calibration_failures(
            agreement(holdout, HUMAN_LABELS, positive=PASS, negative=FAIL) if holdout else None,
            thresholds,
        )
        aspects[aspect] = {
            "splits": splits,
            "perspectives": perspectives,
            "passed": not failures,
            "failures": failures,
        }
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provenance": {
            "labels": {
                "rubric_id": calibration_set.rubric_id,
                "sha256": _sha256(calibration_set.model_dump(mode="json")),
                "item_count": len(calibration_set.items),
            },
            "evaluator": dict(evaluator or {}),
            "thresholds": thresholds.provenance(),
        },
        "aspects": aspects,
        "passed": bool(aspects) and all(item["passed"] for item in aspects.values()),
        "judgements": {
            item.id: {
                **judgements[item.id].model_dump(mode="json"),
                "human": item.label,
                "perspective": item.perspective,
                "split": _bucket(item),
            }
            for item in calibration_set.items
        },
    }


# ---- 基線との比較 ------------------------------------------------------------------------

_COMPARED_METRICS = ("accuracy", "kappa", "false_pass_rate", "false_fail_rate", "coverage")


def _delta(base: Any, target: Any) -> float | None:
    if isinstance(base, int | float) and isinstance(target, int | float):
        return round(float(target) - float(base), 4)
    return None


def compare_reports(base: Mapping[str, Any], target: Mapping[str, Any]) -> dict[str, Any]:
    """前の校正の結果（基線）と比べる。

    同じ校正セット（sha256）・同じ結果の形でなければ比較できない（`comparable=false` と理由）。
    評価者（モデル・指示の hash）・閾値の違いは `changes` に出す。評価者が同じでも、判定のモデルは
    seed を使わないので、差は判定の揺れを含む。
    """
    reasons: list[str] = []
    if base.get("schema_version") != target.get("schema_version"):
        reasons.append(
            f"結果の形の版が違う（{base.get('schema_version')} → {target.get('schema_version')}）"
        )
    base_prov = base.get("provenance") or {}
    target_prov = target.get("provenance") or {}
    base_labels = (base_prov.get("labels") or {}).get("sha256")
    target_labels = (target_prov.get("labels") or {}).get("sha256")
    if base_labels != target_labels:
        reasons.append("校正セットが違う（sha256 が一致しない。項目・人手の判定を変えた）")
    changes: list[str] = []
    base_eval = base_prov.get("evaluator") or {}
    target_eval = target_prov.get("evaluator") or {}
    if base_eval.get("model") != target_eval.get("model"):
        changes.append(f"判定のモデル: {base_eval.get('model')} → {target_eval.get('model')}")
    base_sha = base_eval.get("instructions_sha256") or {}
    target_sha = target_eval.get("instructions_sha256") or {}
    for aspect in ASPECTS:
        if base_sha.get(aspect) != target_sha.get(aspect):
            changes.append(f"判定の指示（{aspect}）の sha256 が違う")
    if base_eval.get("model_settings") != target_eval.get("model_settings"):
        changes.append("判定のモデルの設定（推論の量・出力の上限）が違う")
    if base_prov.get("thresholds") != target_prov.get("thresholds"):
        changes.append("校正の合否の閾値が違う（合否の差は閾値の差を含む）")
    result: dict[str, Any] = {
        "comparable": not reasons,
        "reasons": reasons,
        "changes": changes,
        "base_created_at": base.get("created_at"),
    }
    if reasons:
        return result
    aspects: dict[str, Any] = {}
    for aspect in ASPECTS:
        base_aspect = (base.get("aspects") or {}).get(aspect)
        target_aspect = (target.get("aspects") or {}).get(aspect)
        if not base_aspect or not target_aspect:
            continue
        base_holdout = base_aspect["splits"].get("holdout") or {}
        target_holdout = target_aspect["splits"].get("holdout") or {}
        aspects[aspect] = {
            "passed": {"base": base_aspect["passed"], "target": target_aspect["passed"]},
            "holdout": {
                metric: {
                    "base": base_holdout.get(metric),
                    "target": target_holdout.get(metric),
                    "delta": _delta(base_holdout.get(metric), target_holdout.get(metric)),
                }
                for metric in _COMPARED_METRICS
            },
        }
    base_judgements = base.get("judgements") or {}
    flipped = [
        {
            "id": item_id,
            "aspect": judgement.get("aspect"),
            "split": judgement.get("split"),
            "human": judgement.get("human"),
            "base": base_judgements[item_id].get("label"),
            "target": judgement.get("label"),
        }
        for item_id, judgement in (target.get("judgements") or {}).items()
        if item_id in base_judgements
        and base_judgements[item_id].get("label") != judgement.get("label")
    ]
    result["aspects"] = aspects
    result["flipped"] = flipped
    return result


# ---- 評価者を動かす ------------------------------------------------------------------------

JudgeOne = Callable[[CalibrationItem], Awaitable[Judgement]]


async def judge_items(
    calibration_set: CalibrationSet, judge: JudgeOne, *, concurrency: int = 1
) -> dict[str, Judgement]:
    """項目ごとに評価者を動かす（並行の上限つき。`judge` は 1 件の判定。テストでは台本）。"""
    gate = asyncio.Semaphore(max(1, concurrency))

    async def one(item: CalibrationItem) -> tuple[str, Judgement]:
        async with gate:
            return item.id, await judge(item)

    results = await asyncio.gather(*(one(item) for item in calibration_set.items))
    return dict(results)


def backend_judge() -> tuple[JudgeOne, dict[str, Any]]:
    """品質評価の job と同じ評価者（既定のテキストモデル・同じ指示・同じ実行器）。

    モデルを解決できなければ `BuiltinRuntimeError`（設定の誤り。校正を始めない）。
    """
    from app.features.agent import builtin_runtime, evaluation
    from app.features.agent.ai_calls import evaluation_concurrency, make_ai_caller
    from app.features.agent.output_fields import OutputFieldResult

    target = builtin_runtime.resolve_model_target("")
    caller = make_ai_caller("evaluation_judge", max_concurrency=evaluation_concurrency())

    async def judge(item: CalibrationItem) -> Judgement:
        if item.aspect == OUTPUT_MEANING:
            field = OutputFieldResult(
                row=0,
                field="value",
                label=item.question,
                expected=item.standard_answer,
                actual=item.answer,
                correct=False,
                method="meaning",
            )
            [result] = await evaluation.judge_output_meaning([field], caller=caller)
            if result.note:
                return Judgement(
                    aspect=item.aspect, label=None, status="failed", reason=result.note
                )
            if not result.reason:
                return Judgement(
                    aspect=item.aspect,
                    label=None,
                    status="failed",
                    reason="評価者が項目の判定を返しませんでした。",
                )
            return Judgement(
                aspect=item.aspect,
                label=PASS if result.correct else FAIL,
                status="judged",
                reason=result.reason,
            )
        case = evaluation.EvaluationCase(
            id=item.id, question=item.question, expected=item.standard_answer
        )
        try:
            judgement, attempts = await evaluation.judge_answer(
                case, item.answer, item.clarifications, caller=caller
            )
        except evaluation.JudgeIncompleteError as exc:
            return Judgement(
                aspect=item.aspect,
                label=None,
                status="incomplete",
                reason=exc.message,
                attempts=exc.attempts,
            )
        except evaluation.JudgeCallError as exc:
            return Judgement(
                aspect=item.aspect,
                label=None,
                status="failed",
                reason="判定のモデルの呼び出しか、出力の形の検証に失敗しました。",
                error_type=type(exc.__cause__).__name__ if exc.__cause__ else None,
                attempts=exc.attempts,
            )
        verdict = judgement.verdict.value
        label: Label | None = {"correct": PASS, "incorrect": FAIL}.get(verdict)  # type: ignore[assignment]
        return Judgement(
            aspect=item.aspect,
            label=label,
            status="judged" if label else "uncertain",
            reason=judgement.summary,
            verdict=verdict,
            score=judgement.score,
            missing_points=judgement.missing_points,
            attempts=attempts,
        )

    provenance = {
        "provider": "enterprise_ai",
        "model": target.model_id,
        # provider がモデルの snapshot を公開しない限り、固定できるのはモデルの ID まで。
        "model_settings": {
            "reasoning_effort": target.reasoning_effort,
            "max_output_tokens": target.max_output_tokens,
        },
        "judge_fingerprint": evaluation.judge_fingerprint(),
        "instructions_sha256": {
            ANSWER_POINTS: hashlib.sha256(evaluation.JUDGE_INSTRUCTIONS.encode()).hexdigest(),
            OUTPUT_MEANING: hashlib.sha256(evaluation.MEANING_INSTRUCTIONS.encode()).hexdigest(),
        },
        "seed": None,
    }
    return judge, provenance


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.judge_calibration",
        description="品質評価の評価者（LLM の判定）を人手の判定の校正セットで校正する",
    )
    parser.add_argument("set", type=Path, help="校正セット（JSON）")
    parser.add_argument(
        "--judgements", type=Path, help="前の校正の結果（評価者を呼ばずに集計だけやり直す）"
    )
    parser.add_argument("--baseline", type=Path, help="基線にする前の校正の結果（違いを出す）")
    defaults = CalibrationThresholds()
    parser.add_argument("--min-items", type=int, default=defaults.min_items)
    parser.add_argument("--min-kappa", type=float, default=defaults.min_kappa)
    parser.add_argument("--max-false-pass-rate", type=float, default=defaults.max_false_pass_rate)
    parser.add_argument("--min-coverage", type=float, default=defaults.min_coverage)
    parser.add_argument(
        "--concurrency", type=int, default=2, help="同時に判定する項目の数（既定 2）"
    )
    parser.add_argument("--output", type=Path, required=True, help="校正の結果の出力先")
    return parser


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"JSON の object ではありません: {path}")
    return data


def main(
    argv: Sequence[str] | None = None, *, judge: tuple[JudgeOne, dict[str, Any]] | None = None
) -> int:
    args = _build_parser().parse_args(argv)
    try:
        calibration_set = load_set(args.set)
        thresholds = CalibrationThresholds(
            min_items=args.min_items,
            min_kappa=args.min_kappa,
            max_false_pass_rate=args.max_false_pass_rate,
            min_coverage=args.min_coverage,
        )
        if args.judgements:
            previous = _read_json(args.judgements)
            judgements = {
                key: Judgement.model_validate(value)
                for key, value in (previous.get("judgements") or {}).items()
            }
            provenance = (previous.get("provenance") or {}).get("evaluator") or {}
        else:
            if judge is None:
                from app.features.agent import builtin_runtime

                try:
                    judge = backend_judge()
                except builtin_runtime.BuiltinRuntimeError as exc:
                    raise ValueError(exc.message) from exc
            judge_one, provenance = judge
            judgements = asyncio.run(
                judge_items(calibration_set, judge_one, concurrency=args.concurrency)
            )
        report = calibrate(calibration_set, judgements, thresholds=thresholds, evaluator=provenance)
        if args.baseline:
            report["baseline"] = compare_reports(_read_json(args.baseline), report)
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except BoundedCallAborted as error:
        # 権限・設定の誤り・割り当ての使い切り（校正を続けても判定できない）。
        print(f"error: 判定のモデルを呼べません: {error}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for aspect, item in report["aspects"].items():
        holdout = item["splits"].get("holdout") or {}
        print(
            f"{aspect}: passed={item['passed']} holdout={holdout.get('total', 0)} "
            f"kappa={holdout.get('kappa')} false_pass={holdout.get('false_pass_rate')} "
            f"false_fail={holdout.get('false_fail_rate')} unjudged={holdout.get('unjudged')} "
            f"{'; '.join(item['failures'])}",
            file=sys.stderr,
        )
    baseline = report.get("baseline")
    if baseline:
        if baseline["comparable"]:
            print(
                f"baseline: changes={baseline['changes'] or 'なし'} "
                f"flipped={len(baseline['flipped'])}",
                file=sys.stderr,
            )
        else:
            print(f"baseline: 比較できません: {'; '.join(baseline['reasons'])}", file=sys.stderr)
    print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI boundary
    raise SystemExit(main())
