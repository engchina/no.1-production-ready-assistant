"""RAG の回答の評価器を人手の判定で校正する（#3418・#2577）。

対象は標準回答による回答の評価（`rag_engine.evaluation.answer_eval.evaluate_answer_payload` と、指標
を足す `app.rag.answer_metrics.score_answer_evaluation`。画面の品質評価・評価の job と同じ経路）。
検索の評価器の校正（`rag_engine.evaluation.crag_grader_eval`）とは対象が違うので分ける。

人手の判定は、製品横断の評価の校正と同じ形の JSON（LabelSet: `rubric_id`・`items[]` の `id`・
`split`・`question`・`answer`・`standard_answer`・`evidence`・観点ごとの `labels`・`origin`）を、
suite の package を import せずにこの module の型で読む。一致の集計と校正の合否の閾値は platform の
`pr_evaluation_scoring`（`agreement`・`CalibrationThresholds`・`calibration_failures`）を共有し、
同じ人手の判定・同じ基準で suite の判定器と並べられる。

評価器の出力 → 観点の pass / fail:

- `requirement_coverage` ← `requirement_coverage`（必要な項目への対応率）
- `answer_correctness` ← `answer_correctness`（標準回答に照らした正しさ。1 / 0）
- `claim_support` ← `claim_support_rate`（監査した主張のうち裏付けられた割合）

値が `pass_at`（既定 1.0。rubric の「1 つでも欠ければ fail」と同じ）以上なら pass、未満なら fail。
評価が完了しない・値が無い（測れない）項目は判定できなかった件（`unjudged`）に数える。

    cd rag/backend
    # 評価器を動かして校正する（OCI Enterprise AI を呼ぶ。接続とモデルは RAG の設定）
    uv run python -m app.rag.answer_judge_calibration ../../tmp/eval/labels.json \\
        --output ../../tmp/eval/rag-answer-calibration.json
    # 判定のモデルを明示する（省略時は共通のモデル設定の既定の判定モデル。#3574）
    uv run python -m app.rag.answer_judge_calibration ../../tmp/eval/labels.json \\
        --judge-model <登録モデルの ID> --output ../../tmp/eval/rag-answer-calibration.json
    # 人手の判定を直したら、保存した評価を使って集計だけやり直す（モデルを呼ばない）
    uv run python -m app.rag.answer_judge_calibration ../../tmp/eval/labels.json \\
        --evaluations ../../tmp/eval/rag-answer-calibration.json \\
        --output ../../tmp/eval/rag-answer-calibration.v2.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pr_evaluation_scoring import (
    CalibrationThresholds,
    LabelPair,
    agreement,
    calibration_failures,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError

REPORT_SCHEMA_VERSION = 1
PASS = "pass"  # nosec B105 - 判定のラベル（資格情報ではない）
FAIL = "fail"
HUMAN_LABELS = (PASS, FAIL)
SPLITS = ("dev", "holdout", "synthetic")
# 観点 → 評価器の指標の名前。
ASPECT_METRICS: dict[str, str] = {
    "requirement_coverage": "requirement_coverage",
    "answer_correctness": "answer_correctness",
    "claim_support": "claim_support_rate",
}
DEFAULT_PASS_AT = 1.0


class LabeledEvidence(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1)
    text: str


class LabeledAnswer(BaseModel):
    """人手の判定の 1 件（評価器に渡す入力と、観点ごとの人手のラベル）。"""

    model_config = ConfigDict(extra="ignore")

    id: str = Field(..., min_length=1)
    split: Literal["dev", "holdout"]
    question: str = Field(..., min_length=1)
    answer: str
    standard_answer: str | None = None
    evidence: list[LabeledEvidence] = Field(default_factory=list)
    labels: dict[str, Literal["pass", "fail"]] = Field(..., min_length=1)
    origin: Literal["human", "synthetic"] = "human"


class HumanLabels(BaseModel):
    model_config = ConfigDict(extra="ignore")

    rubric_id: str
    items: list[LabeledAnswer] = Field(..., min_length=1)


def load_labels(path: Path) -> HumanLabels:
    return HumanLabels.model_validate_json(path.read_text(encoding="utf-8"))


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluation_input(item: LabeledAnswer) -> dict[str, Any]:
    """人手の判定の項目を、評価器の入力（回答の記録の `evaluation_input` と同じキー）にする。"""
    return {
        "question": item.question,
        "answer_text": item.answer,
        "reasoning_summary": "",
        "insufficient_reason": "",
        "cited_evidence": [evidence.id for evidence in item.evidence],
        "evidence_items": [
            {"id": evidence.id, "text": evidence.text, "source": evidence.id}
            for evidence in item.evidence
        ],
    }


def aspect_label(
    evaluation: Mapping[str, Any] | None, aspect_id: str, *, pass_at: float = DEFAULT_PASS_AT
) -> str | None:
    """評価器の出力から観点の pass / fail（完了しない・値の無い評価は None）。"""
    if not evaluation or evaluation.get("status") != "completed":
        return None
    metric = ASPECT_METRICS.get(aspect_id)
    value = evaluation.get(metric) if metric else None
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    return PASS if value >= pass_at else FAIL


def _bucket(item: LabeledAnswer) -> str:
    return "synthetic" if item.origin == "synthetic" else item.split


def calibrate_evaluations(
    labels: HumanLabels,
    evaluations: Mapping[str, Mapping[str, Any]],
    *,
    thresholds: CalibrationThresholds,
    pass_at: Mapping[str, float] | None = None,
    evaluator: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """評価器の出力と人手の判定の一致を観点ごと・区分ごとに集計し、holdout で合否を決める。

    合成の項目（`origin=synthetic`。故意に壊した回答）は synthetic の区分に別に集計し、合否に
    入れない。評価器の対象でない観点（`ASPECT_METRICS` に無い観点）のラベルは集計しない。
    """
    cutoffs = {aspect: (pass_at or {}).get(aspect, DEFAULT_PASS_AT) for aspect in ASPECT_METRICS}
    aspects: dict[str, Any] = {}
    for aspect_id in ASPECT_METRICS:
        splits: dict[str, Any] = {}
        holdout = None
        for split in SPLITS:
            pairs = [
                LabelPair(
                    item_id=item.id,
                    human=human,
                    judge=aspect_label(
                        evaluations.get(item.id), aspect_id, pass_at=cutoffs[aspect_id]
                    ),
                )
                for item in labels.items
                if _bucket(item) == split and (human := item.labels.get(aspect_id)) is not None
            ]
            if not pairs:
                continue
            result = agreement(pairs, HUMAN_LABELS, positive=PASS, negative=FAIL)
            splits[split] = {
                **asdict(result),
                "disagreements": [pair.item_id for pair in pairs if pair.judge != pair.human],
                "unjudged_items": [pair.item_id for pair in pairs if pair.judge is None],
            }
            if split == "holdout":
                holdout = result
        failures = calibration_failures(holdout, thresholds)
        aspects[aspect_id] = {"splits": splits, "passed": not failures, "failures": failures}
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "provenance": {
            "labels": {
                "rubric_id": labels.rubric_id,
                "sha256": _sha256(labels.model_dump(mode="json")),
                "item_count": len(labels.items),
            },
            "evaluator": dict(evaluator or {}),
            "aspect_metrics": dict(ASPECT_METRICS),
            "pass_at": cutoffs,
            "thresholds": thresholds.provenance(),
        },
        "aspects": aspects,
        "passed": all(item["passed"] for item in aspects.values()),
        "evaluations": {key: dict(value) for key, value in evaluations.items()},
    }


Evaluate = Callable[[LabeledAnswer], Mapping[str, Any]]


def evaluate_labels(labels: HumanLabels, evaluate: Evaluate) -> dict[str, dict[str, Any]]:
    """人手の判定の項目ごとに評価器を動かす（`evaluate` は 1 件の評価。テストでは台本）。"""
    return {item.id: dict(evaluate(item)) for item in labels.items}


def resolve_judge_model_id(settings: Any, judge_model_id: str | None = None) -> str:
    """判定のモデル（指定、無ければ共通のモデル設定の既定の判定モデル。#3574）。"""
    from app.config import enterprise_ai_judge_model_id

    return (judge_model_id or "").strip() or enterprise_ai_judge_model_id(settings)


def judge_provenance(settings: Any, judge_model_id: str | None = None) -> dict[str, Any]:
    """評価器の判定のモデル（校正の結果に記録し、画面の「校正済み」の照合に使う。#3578）。"""
    return {
        "provider": "enterprise_ai",
        "model": resolve_judge_model_id(settings, judge_model_id),
    }


def judge_prompt_sha256() -> str:
    """評価器の指示（``answer_eval`` の ``*_PROMPT``）の sha256（校正の結果と照合する。#3578）。

    指示の名前・数が変わっても、module の大文字の ``*_PROMPT`` の文字列を名前の順に hash する。
    """
    from rag_engine.evaluation import answer_eval
    from rag_engine.resources.runtime import resolve_prompt

    keys = {
        "SCOPE_PROMPT": "scope",
        "COVERAGE_PROMPT": "coverage",
        "CLAIM_PROMPT": "claim",
        "CORRECTNESS_PROMPT": "correctness",
    }
    prompts = sorted(
        (name, resolve_prompt(f"evaluation.{keys[name]}", value) if name in keys else value)
        for name, value in vars(answer_eval).items()
        if name.isupper() and name.endswith("PROMPT") and isinstance(value, str)
    )
    return _sha256(prompts)


def backend_evaluator(
    settings: Any | None = None, judge_model_id: str | None = None
) -> tuple[Evaluate, dict[str, Any]]:
    """RAG の設定で、画面・評価の job と同じ評価器（`evaluate_answer_record`）を使う。

    判定のモデルは ``judge_model_id``（`--judge-model`）、無ければ共通のモデル設定の既定の判定モデル
    （人手の判定の回答は作ったモデルが分からないので、「回答と同じモデル」は既定のテキストモデル。#3574）。
    """
    from app.config import get_settings
    from app.rag.answer_engine import evaluate_answer_record
    from app.schemas.search import RetrievedChunk

    resolved = settings or get_settings()
    judge_model = resolve_judge_model_id(resolved, judge_model_id)

    def evaluate(item: LabeledAnswer) -> Mapping[str, Any]:
        citations = [
            RetrievedChunk(
                document_id=evidence.id, chunk_id=evidence.id, text=evidence.text, score=0.0
            )
            for evidence in item.evidence
        ]
        return evaluate_answer_record(
            evaluation_input(item),
            item.standard_answer or "",
            resolved,
            citations=citations,
            judge_model_id=judge_model,
        )

    return evaluate, {"provider": "enterprise_ai", "model": judge_model}


def _backend_evaluator(judge_model_id: str | None = None) -> tuple[Evaluate, dict[str, Any]]:
    """CLI の評価器（`--judge-model`）。"""
    return backend_evaluator(None, judge_model_id)


def _pass_at(values: Sequence[str]) -> dict[str, float]:
    cutoffs: dict[str, float] = {}
    for value in values:
        aspect, separator, number = value.partition("=")
        if not separator or aspect not in ASPECT_METRICS:
            raise ValueError(
                f"--pass-at は <観点>=<値>（観点は {', '.join(ASPECT_METRICS)}）: {value}"
            )
        cutoffs[aspect] = float(number)
    return cutoffs


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.rag.answer_judge_calibration",
        description="RAG の回答の評価器を人手の判定（LabelSet の JSON）で校正する",
    )
    parser.add_argument("labels", type=Path, help="人手の判定（LabelSet の JSON）")
    parser.add_argument(
        "--evaluations", type=Path, help="前の校正の結果（評価器を呼ばずに集計だけやり直す）"
    )
    parser.add_argument(
        "--pass-at",
        action="append",
        default=[],
        help=f"観点を pass にする指標の下限（<観点>=<値>。既定 {DEFAULT_PASS_AT}）",
    )
    defaults = CalibrationThresholds()
    parser.add_argument("--min-items", type=int, default=defaults.min_items)
    parser.add_argument("--min-kappa", type=float, default=defaults.min_kappa)
    parser.add_argument("--max-false-pass-rate", type=float, default=defaults.max_false_pass_rate)
    parser.add_argument("--min-coverage", type=float, default=defaults.min_coverage)
    parser.add_argument(
        "--judge-model",
        help="判定のモデル（登録モデルの ID。省略時は共通のモデル設定の既定の判定モデル）",
    )
    parser.add_argument("--output", type=Path, required=True, help="校正の結果の出力先")
    return parser


def main(
    argv: Sequence[str] | None = None, *, evaluator: tuple[Evaluate, dict[str, Any]] | None = None
) -> int:
    args = _build_parser().parse_args(argv)
    try:
        labels = load_labels(args.labels)
        thresholds = CalibrationThresholds(
            min_items=args.min_items,
            min_kappa=args.min_kappa,
            max_false_pass_rate=args.max_false_pass_rate,
            min_coverage=args.min_coverage,
        )
        cutoffs = _pass_at(args.pass_at)
        if args.evaluations:
            previous = json.loads(args.evaluations.read_text(encoding="utf-8"))
            evaluations = previous.get("evaluations") or {}
            provenance = (previous.get("provenance") or {}).get("evaluator") or {}
            missing = [item.id for item in labels.items if item.id not in evaluations]
            if missing:
                raise ValueError(f"評価の無い項目があります: {', '.join(missing[:10])}")
        else:
            evaluate, provenance = evaluator or _backend_evaluator(args.judge_model)
            evaluations = evaluate_labels(labels, evaluate)
        report = calibrate_evaluations(
            labels, evaluations, thresholds=thresholds, pass_at=cutoffs, evaluator=provenance
        )
    except (OSError, ValueError, ValidationError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    for aspect_id, item in report["aspects"].items():
        holdout = item["splits"].get("holdout") or {}
        synthetic = item["splits"].get("synthetic") or {}
        print(
            f"{aspect_id}: passed={item['passed']} kappa={holdout.get('kappa')} "
            f"items={holdout.get('total', 0)} "
            f"synthetic_false_pass={synthetic.get('false_pass_rate')} "
            f"{'; '.join(item['failures'])}",
            file=sys.stderr,
        )
    print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
