"""RAG の回答の評価器の、人手の判定（LabelSet の JSON）による校正（#3418・#2577）。

評価器は台本（`evaluate_answer_payload` と指標の計算の出力の形の合成の値）で、モデルの API は
呼ばない。人手の判定は合成の内容（業務の内容を含まない）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pr_evaluation_scoring import CalibrationThresholds
from pydantic import ValidationError

from app.rag.answer_judge_calibration import (
    ASPECT_METRICS,
    HumanLabels,
    LabeledAnswer,
    aspect_label,
    aspect_reasons,
    calibrate_evaluations,
    compare_reports,
    evaluate_labels,
    evaluation_input,
    load_labels,
    main,
)

BUNDLED_SET = (
    Path(__file__).resolve().parents[2]
    / "evaluation/judge-calibration/rag-answer-judge-calibration.v1.json"
)


def _item(index: int, split: str, labels: dict[str, str], origin: str = "human") -> dict[str, Any]:
    return {
        "id": f"i{index:02d}",
        "split": split,
        "question": f"合成の質問 {index}",
        "answer": f"合成の回答 {index}",
        "standard_answer": "合成の標準回答",
        "evidence": [{"id": f"e{index}", "text": "合成の根拠"}],
        "labels": labels,
        "origin": origin,
        # suite の LabelSet の項目（記入者ごとの判定・合成の壊し方）は読まずに無視する。
        "annotator_labels": {"a1": labels},
        **(
            {"perturbation": {"kind": "swap_values", "base_item_id": "i01"}}
            if origin == "synthetic"
            else {}
        ),
    }


def _labels() -> dict[str, Any]:
    good = {"requirement_coverage": "pass", "answer_correctness": "pass", "claim_support": "pass"}
    items = [
        _item(1, "dev", good),
        _item(2, "holdout", good),
        _item(3, "holdout", {**good, "claim_support": "fail"}),
        _item(4, "holdout", {**good, "answer_correctness": "fail"}),
        _item(5, "holdout", good),
        _item(6, "holdout", {"claim_support": "fail"}, origin="synthetic"),
    ]
    return {"rubric_id": "answer_quality", "provenance": {"x": 1}, "items": items}


def _evaluation(
    coverage: float | None, correctness: float | None, support: float | None
) -> dict[str, Any]:
    return {
        "status": "completed",
        "rubric_version": 11,
        "requirement_coverage": coverage,
        "answer_correctness": correctness,
        "claim_support_rate": support,
    }


EVALUATIONS: dict[str, dict[str, Any]] = {
    "i01": _evaluation(1.0, 1.0, 1.0),
    "i02": _evaluation(1.0, 1.0, 1.0),
    # 人手は claim_support を fail、評価器は 0.5 → fail（一致）。
    "i03": _evaluation(1.0, 1.0, 0.5),
    # 人手は answer_correctness を fail、評価器は 1.0 → pass（誤合格）。
    "i04": _evaluation(1.0, 1.0, 1.0),
    # 評価が完了しない → 判定できなかった件。
    "i05": {"status": "error", "message": "合成の失敗"},
    # 合成の対照（壊した回答）を評価器は見逃した → synthetic の誤合格。
    "i06": _evaluation(1.0, 1.0, 1.0),
}


def test_aspect_label_maps_metrics_to_pass_and_fail() -> None:
    evaluation = _evaluation(0.75, 0.0, None)
    assert aspect_label(evaluation, "requirement_coverage") == "fail"
    assert aspect_label(evaluation, "requirement_coverage", pass_at=0.7) == "pass"
    assert aspect_label(evaluation, "answer_correctness") == "fail"
    # 測れない指標・完了しない評価・知らない観点は判定できなかった件。
    assert aspect_label(evaluation, "claim_support") is None
    assert aspect_label({"status": "error"}, "answer_correctness") is None
    assert aspect_label(None, "answer_correctness") is None
    assert aspect_label(evaluation, "unknown") is None


def test_evaluation_input_uses_the_answer_record_keys() -> None:
    item = LabeledAnswer.model_validate(_item(1, "dev", {"claim_support": "pass"}))
    payload = evaluation_input(item)
    assert payload["answer_text"] == "合成の回答 1"
    assert payload["evidence_items"] == [{"id": "e1", "text": "合成の根拠", "source": "e1"}]
    assert payload["cited_evidence"] == ["e1"]


def test_calibration_uses_the_shared_agreement_and_gate() -> None:
    labels = HumanLabels.model_validate(_labels())
    report = calibrate_evaluations(
        labels,
        EVALUATIONS,
        thresholds=CalibrationThresholds(min_items=3),
        evaluator={"model": "synthetic"},
    )
    correctness = report["aspects"]["answer_correctness"]["splits"]["holdout"]
    assert (correctness["total"], correctness["judged"], correctness["unjudged"]) == (4, 3, 1)
    assert correctness["confusion"]["fail"]["pass"] == 1
    assert correctness["false_pass_rate"] == 1.0
    assert correctness["unjudged_items"] == ["i05"]
    support = report["aspects"]["claim_support"]["splits"]
    assert support["holdout"]["confusion"]["fail"]["fail"] == 1
    # 合成の対照は合否に入れず、synthetic の区分に別に集計する。
    assert support["synthetic"]["total"] == 1
    assert support["synthetic"]["false_pass_rate"] == 1.0
    assert support["holdout"]["total"] == 4
    assert report["aspects"]["answer_correctness"]["passed"] is False
    assert any("誤合格の率" in item for item in report["aspects"]["answer_correctness"]["failures"])
    assert report["passed"] is False
    assert report["provenance"]["thresholds"]["min_items"] == 3
    assert report["provenance"]["evaluator"] == {"model": "synthetic"}
    assert len(report["provenance"]["labels"]["sha256"]) == 64


def test_cli_runs_the_evaluator_then_recomputes_from_saved_evaluations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(json.dumps(_labels(), ensure_ascii=False), "utf-8")
    calls: list[str] = []

    def evaluate(item: LabeledAnswer) -> Mapping[str, Any]:
        calls.append(item.id)
        return EVALUATIONS[item.id]

    output = tmp_path / "report.json"
    args = [str(labels_path), "--min-items", "3", "--output", str(output)]
    assert main(args, evaluator=(evaluate, {"model": "synthetic"})) == 0
    assert calls == [f"i{index:02d}" for index in range(1, 7)]
    assert "claim_support: passed=" in capsys.readouterr().err

    again = tmp_path / "again.json"
    rerun = [str(labels_path), "--evaluations", str(output), "--pass-at", "claim_support=0.5"]

    def fail(item: LabeledAnswer) -> Mapping[str, Any]:
        raise AssertionError("評価器を呼ばない")

    assert main([*rerun, "--output", str(again)], evaluator=(fail, {})) == 0
    report = json.loads(again.read_text("utf-8"))
    assert report["provenance"]["pass_at"]["claim_support"] == 0.5
    assert report["provenance"]["evaluator"] == {"model": "synthetic"}
    # 下限を 0.5 にすると i03（0.5）は pass になり、人手の fail と食い違う（i05 は判定できない）。
    assert report["aspects"]["claim_support"]["splits"]["holdout"]["disagreements"] == [
        "i03",
        "i05",
    ]

    compared = tmp_path / "compared.json"
    baseline_args = [
        *rerun,
        "--min-items",
        "3",
        "--baseline",
        str(output),
        "--output",
        str(compared),
    ]
    assert main(baseline_args, evaluator=(fail, {})) == 0
    baseline = json.loads(compared.read_text("utf-8"))["baseline"]
    assert baseline["comparable"] is True
    assert baseline["changes"] == ["pass にする指標の下限（pass_at）が違う"]
    assert [flip["id"] for flip in baseline["flipped"]] == ["i03"]
    assert "baseline: comparable=True flipped=1" in capsys.readouterr().err

    assert main([str(labels_path), "--pass-at", "x=1", "--output", str(again)]) == 2
    assert "--pass-at" in capsys.readouterr().err


def _typed_labels() -> HumanLabels:
    data = _labels()
    data["items"][2]["error_types"] = ["unsupported_claim"]
    data["items"][3]["error_types"] = ["attribute_swap"]
    data["items"][1]["error_types"] = ["correct_paraphrase"]
    return HumanLabels.model_validate(data)


def test_report_breaks_down_error_types_and_keeps_reasons() -> None:
    evaluations = {
        **EVALUATIONS,
        "i03": {
            **_evaluation(1.0, 1.0, 0.5),
            "claim_checks": [
                {"status": "supported", "answer_quote": "正しい文", "reason": "根拠にある"},
                {"status": "unsupported", "answer_quote": "根拠に無い文", "reason": "根拠に無い"},
            ],
        },
        "i04": {**_evaluation(1.0, 1.0, 1.0), "correctness_reason": "標準回答と合う"},
    }
    report = calibrate_evaluations(
        _typed_labels(), evaluations, thresholds=CalibrationThresholds(min_items=3)
    )
    correctness = report["aspects"]["answer_correctness"]
    assert correctness["splits"]["holdout"]["false_pass_items"] == ["i04"]
    assert correctness["splits"]["holdout"]["false_fail_items"] == []
    # 誤りの型ごとの内訳: 取り違え（i04）を評価器は見逃した。
    swap = correctness["error_types"]["attribute_swap"]["holdout"]
    assert (swap["total"], swap["false_pass_rate"]) == (1, 1.0)
    assert set(correctness["error_types"]) == {
        "unsupported_claim",
        "attribute_swap",
        "correct_paraphrase",
    }
    judgement = report["judgements"]["i04"]
    assert judgement["human"]["answer_correctness"] == "fail"
    assert judgement["judge"]["answer_correctness"] == "pass"
    assert judgement["reasons"]["answer_correctness"] == ["標準回答と合う"]
    assert report["judgements"]["i03"]["reasons"]["claim_support"] == [
        "unsupported: 根拠に無い文 — 根拠に無い"
    ]
    # 完了しない評価は status と例外の型だけを理由にする。
    assert report["judgements"]["i05"]["reasons"]["claim_support"] == [
        "評価が完了しませんでした（error）"
    ]
    assert report["provenance"]["judge_rubric_version"] == "11"


def test_aspect_reasons_lists_unaddressed_requirements() -> None:
    evaluation = {
        "status": "completed",
        "standard_answer_scope": {
            "requirements": [{"requirement": "期限"}, {"requirement": "承認"}]
        },
        "coverage_checks": [
            {"requirement_index": 1, "status": "addressed"},
            {"requirement_index": 2, "status": "missing"},
        ],
    }
    assert aspect_reasons(evaluation, "requirement_coverage") == ["missing: 承認"]
    assert aspect_reasons(
        {"status": "error", "error_type": "TimeoutError"}, "answer_correctness"
    ) == ["評価が完了しませんでした（error・TimeoutError）"]
    assert aspect_reasons(None, "claim_support") == ["評価がありません"]


def test_failed_evaluator_calls_become_unjudged_without_the_message() -> None:
    labels = HumanLabels.model_validate(_labels())

    def evaluate(item: LabeledAnswer) -> Mapping[str, Any]:
        if item.id == "i02":
            raise ConnectionError("https://secret.example/ key=xxx")
        return EVALUATIONS[item.id]

    evaluations = evaluate_labels(labels, evaluate)
    assert evaluations["i02"]["status"] == "error"
    assert evaluations["i02"]["error_type"] == "ConnectionError"
    assert "secret" not in json.dumps(evaluations, ensure_ascii=False)
    report = calibrate_evaluations(labels, evaluations, thresholds=CalibrationThresholds())
    assert "i02" in report["aspects"]["claim_support"]["splits"]["holdout"]["unjudged_items"]


def test_labels_reject_unknown_error_types_and_duplicate_ids() -> None:
    data = _labels()
    data["items"][0]["error_types"] = ["unknown_type"]
    with pytest.raises(ValidationError, match="知らない誤りの型"):
        HumanLabels.model_validate(data)
    data = _labels()
    data["items"][1]["id"] = data["items"][0]["id"]
    with pytest.raises(ValidationError, match="重なっています"):
        HumanLabels.model_validate(data)


def test_baseline_reports_metric_deltas_changes_and_flipped_items() -> None:
    labels = _typed_labels()
    thresholds = CalibrationThresholds(min_items=3)
    base = calibrate_evaluations(
        labels, EVALUATIONS, thresholds=thresholds, evaluator={"model": "m1", "prompt_sha256": "a"}
    )
    # 指示を直した評価器は i04 の誤りを見つける。
    fixed = {**EVALUATIONS, "i04": _evaluation(1.0, 0.0, 1.0)}
    target = calibrate_evaluations(
        labels, fixed, thresholds=thresholds, evaluator={"model": "m1", "prompt_sha256": "b"}
    )
    result = compare_reports(base, target)
    assert result["comparable"] is True
    assert result["changes"] == ["判定の指示の sha256: a → b"]
    holdout = result["aspects"]["answer_correctness"]["holdout"]
    assert holdout["false_pass_rate"] == {"base": 1.0, "target": 0.0, "delta": -1.0}
    assert result["flipped"] == [
        {
            "id": "i04",
            "aspect": "answer_correctness",
            "split": "holdout",
            "human": "fail",
            "base": "pass",
            "target": "fail",
        }
    ]
    # 人手の判定が違えば比べない。
    other = HumanLabels.model_validate({**labels.model_dump(), "rubric_id": "other"})
    changed = calibrate_evaluations(other, fixed, thresholds=thresholds)
    refused = compare_reports(base, changed)
    assert refused["comparable"] is False
    assert "aspects" not in refused


def test_bundled_set_covers_the_issue_cases_and_does_not_pass_by_size() -> None:
    labels = load_labels(BUNDLED_SET)
    assert all(item.note and item.origin == "human" for item in labels.items)
    covered = {error_type for item in labels.items for error_type in item.error_types}
    assert {
        "correct_paraphrase",
        "partial_omission",
        "unsupported_claim",
        "correct_refusal",
        "partial_answer",
    } <= covered
    for aspect in ASPECT_METRICS:
        values = {item.labels[aspect] for item in labels.items if aspect in item.labels}
        assert values == {"pass", "fail"}
    # 人手と同じ判定を返す台本でも、holdout の件数が閾値（50）に届かないので不合格のまま。
    perfect = {
        item.id: {
            "status": "completed",
            **{
                metric: (1.0 if item.labels.get(aspect, "pass") == "pass" else 0.0)
                for aspect, metric in ASPECT_METRICS.items()
            },
        }
        for item in labels.items
    }
    report = calibrate_evaluations(labels, perfect, thresholds=CalibrationThresholds())
    holdout = report["aspects"]["answer_correctness"]["splits"]["holdout"]
    assert holdout["kappa"] == 1.0
    assert report["passed"] is False
    assert any("件数" in reason for reason in report["aspects"]["answer_correctness"]["failures"])
