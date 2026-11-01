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

from app.rag.answer_judge_calibration import (
    HumanLabels,
    LabeledAnswer,
    aspect_label,
    calibrate_evaluations,
    evaluation_input,
    main,
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

    assert main([str(labels_path), "--pass-at", "x=1", "--output", str(again)]) == 2
    assert "--pass-at" in capsys.readouterr().err
