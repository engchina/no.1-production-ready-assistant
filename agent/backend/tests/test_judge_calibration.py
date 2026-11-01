"""品質評価の評価者の校正（#66）: 集計・欠測・不正出力・基線との比較・同梱の校正セット。

判定のモデルは SDK の `ScriptedModel`（台本どおりに応答する）か、既知の判定の列を返す台本の
評価者に差し替え、モデルの API へは接続しない。台本の判定が正しいことは評価者の能力の証明では
ないので、ここで確かめるのは集計と、判定できなかった件の扱いだけ。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message
from pr_evaluation_scoring import CalibrationThresholds
from pytest import MonkeyPatch

from app.cli import judge_calibration as calibration
from app.cli.judge_calibration import (
    PERSPECTIVES,
    CalibrationItem,
    CalibrationSet,
    Judgement,
    backend_judge,
    calibrate,
    compare_reports,
    judge_items,
    load_set,
    main,
)
from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget

BUNDLED_SET = (
    Path(__file__).resolve().parents[2]
    / "evaluation/judge-calibration/agent-judge-calibration.v1.json"
)


def _item(item_id: str, split: str, label: str, **values: Any) -> CalibrationItem:
    return CalibrationItem.model_validate(
        {
            "id": item_id,
            "split": split,
            "aspect": values.pop("aspect", "answer_points"),
            "perspective": values.pop("perspective", "number"),
            "question": "上限額は？",
            "standard_answer": "5 万円",
            "answer": "5 万円です。",
            "label": label,
            "note": "人の根拠",
            **values,
        }
    )


def _judged(label: str | None, status: str = "judged", aspect: str = "answer_points") -> Judgement:
    return Judgement.model_validate(
        {"aspect": aspect, "label": label, "status": status, "reason": "台本"}
    )


def _set(*items: CalibrationItem) -> CalibrationSet:
    return CalibrationSet(rubric_id="agent-answer-judge", items=list(items))


def test_bundled_set_covers_every_perspective_with_human_rationale() -> None:
    """同梱の校正セット: 全観点・両方の区分・pass と fail・人の根拠を持ち、合成の業務に依らない。"""
    loaded = load_set(BUNDLED_SET)

    assert {item.perspective for item in loaded.items} == set(PERSPECTIVES)
    for aspect in calibration.ASPECTS:
        items = [item for item in loaded.items if item.aspect == aspect]
        for split in ("dev", "holdout"):
            labels = {item.label for item in items if item.split == split}
            assert labels == {"pass", "fail"}, (aspect, split)
    assert all(item.note.strip() and item.origin == "human" for item in loaded.items)


def test_known_judgements_give_confusion_false_pass_fail_and_unjudged() -> None:
    """既知の判定の列から、混同行列・誤合格 / 誤不合格・未判定・観点の内訳を集計する。"""
    items = _set(
        _item("h1", "holdout", "pass"),
        _item("h2", "holdout", "pass"),
        _item("h3", "holdout", "fail", perspective="negation"),
        _item("h4", "holdout", "fail", perspective="negation"),
        _item("h5", "holdout", "fail"),
        _item("d1", "dev", "fail"),
        _item("s1", "holdout", "fail", origin="synthetic"),
    )
    judgements = {
        "h1": _judged("pass"),
        "h2": _judged("fail"),  # 誤不合格
        "h3": _judged("pass"),  # 誤合格
        "h4": _judged(None, "uncertain"),  # 未判定（不確か）
        "h5": _judged(None, "failed"),  # 未判定（不正な出力）
        "d1": _judged("fail"),
        "s1": _judged("pass"),
    }

    report = calibrate(items, judgements, thresholds=CalibrationThresholds(min_items=3))

    aspect = report["aspects"]["answer_points"]
    holdout = aspect["splits"]["holdout"]
    assert (holdout["total"], holdout["judged"], holdout["unjudged"]) == (5, 3, 2)
    assert holdout["confusion"] == {"pass": {"pass": 1, "fail": 1}, "fail": {"pass": 1, "fail": 0}}
    assert (holdout["false_pass_items"], holdout["false_fail_items"]) == (["h3"], ["h2"])
    assert holdout["unjudged_items"] == ["h4", "h5"]
    assert (holdout["false_pass_rate"], holdout["false_fail_rate"]) == (1.0, 0.5)
    assert holdout["coverage"] == 0.6
    # 合成の項目は synthetic に分け、合否（holdout）に入れない。
    assert aspect["splits"]["synthetic"]["false_pass_items"] == ["s1"]
    assert aspect["splits"]["dev"]["accuracy"] == 1.0
    # 観点の内訳: 否定の 2 件は誤合格 1・未判定 1。
    negation = aspect["perspectives"]["negation"]["holdout"]
    assert (negation["total"], negation["false_pass_items"], negation["unjudged"]) == (2, ["h3"], 1)
    # 正解率だけで合格にしない: 判定率・誤合格の率で不合格。
    assert not aspect["passed"] and not report["passed"]
    assert any("判定率" in failure for failure in aspect["failures"])
    assert any("誤合格の率" in failure for failure in aspect["failures"])
    # 判定の理由と人の判定を項目ごとに残す。
    assert (report["judgements"]["h5"]["status"], report["judgements"]["h5"]["label"]) == (
        "failed",
        None,
    )
    assert report["judgements"]["h3"]["human"] == "fail"
    assert report["judgements"]["s1"]["split"] == "synthetic"


def test_missing_judgement_is_an_error_not_unjudged() -> None:
    with pytest.raises(ValueError, match="判定の無い項目"):
        calibrate(_set(_item("h1", "holdout", "pass")), {}, thresholds=CalibrationThresholds())


def test_holdout_less_aspect_is_not_passed() -> None:
    """holdout の無い観点は「未実行」として不合格（dev だけの結果を校正済みにしない）。"""
    report = calibrate(
        _set(_item("d1", "dev", "pass")),
        {"d1": _judged("pass")},
        thresholds=CalibrationThresholds(min_items=1),
    )

    assert report["aspects"]["answer_points"]["failures"] == ["holdout の項目がありません"]
    assert report["passed"] is False


def test_invalid_set_is_rejected() -> None:
    with pytest.raises(ValueError, match="知らない観点"):
        _item("x", "holdout", "pass", perspective="unknown")
    with pytest.raises(ValueError, match="重なっています"):
        _set(_item("x", "holdout", "pass"), _item("x", "dev", "fail"))
    with pytest.raises(ValueError):
        _item("x", "holdout", "cannot_judge")


def _script(monkeypatch: MonkeyPatch, *steps: Any) -> ScriptedModel:
    model = ScriptedModel(list(steps))
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id=model_id or "judge-model",
            endpoint="https://llm.example",
            project_id="",
            api_key="secret-key-for-test",
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _answer(verdict: str) -> Any:
    payload = {
        "verdict": verdict,
        "score": 0.5,
        "summary": f"{verdict} の理由",
        "missing_points": [],
    }
    return [assistant_message(json.dumps(payload, ensure_ascii=False))]


def _meaning(same: bool) -> Any:
    payload = {"items": [{"number": 1, "same": same, "reason": "意味の理由"}]}
    return [assistant_message(json.dumps(payload, ensure_ascii=False))]


def test_backend_judge_maps_verdicts_and_invalid_output(monkeypatch: MonkeyPatch) -> None:
    """品質評価と同じ評価者: correct / incorrect / uncertain・不正な出力・意味の判定の対応。"""
    model = _script(
        monkeypatch,
        _answer("correct"),
        _answer("incorrect"),
        _answer("uncertain"),
        # schema に合わない出力（不正な出力）。判定できなかった件にし、合否に数えない。
        [assistant_message("判定できません")],
        _meaning(True),
        [assistant_message("壊れた出力")],
    )
    items = _set(
        _item("a1", "holdout", "pass"),
        _item("a2", "holdout", "fail"),
        _item("a3", "holdout", "fail"),
        _item("a4", "holdout", "fail", perspective="judge_injection"),
        _item("m1", "holdout", "pass", aspect="output_meaning", perspective="meaning_match"),
        _item("m2", "holdout", "fail", aspect="output_meaning", perspective="negation"),
    )
    judge, provenance = backend_judge()

    judgements = anyio.run(judge_items, items, judge)

    assert [judgements[key].label for key in ("a1", "a2", "a3", "a4", "m1", "m2")] == [
        "pass",
        "fail",
        None,
        None,
        "pass",
        None,
    ]
    assert judgements["a1"].reason == "correct の理由"
    assert judgements["a3"].status == "uncertain"
    assert (judgements["a4"].status, judgements["a4"].error_type) == (
        "failed",
        "ModelBehaviorError",
    )
    assert judgements["m2"].status == "failed"
    # 回答の判定は品質評価と同じ指示・入力（質問・期待する要点・実際の回答）を渡す。
    assert "期待する回答の要点" in json.dumps(model.calls[0].input, ensure_ascii=False)
    # 評価者の記録: モデル・指示の hash・品質評価の job と同じ指紋。資格情報は含めない。
    assert provenance["model"] == "judge-model"
    assert set(provenance["instructions_sha256"]) == {"answer_points", "output_meaning"}
    assert provenance["judge_fingerprint"]
    assert "secret-key-for-test" not in json.dumps(provenance)


def _report(judge_label: str, *, instructions: str = "a", labels_note: str = "人の根拠") -> Any:
    items = _set(_item("h1", "holdout", "pass", note=labels_note), _item("h2", "holdout", "fail"))
    report = calibrate(
        items,
        {"h1": _judged("pass"), "h2": _judged(judge_label)},
        thresholds=CalibrationThresholds(min_items=2),
        evaluator={"model": "judge-model", "instructions_sha256": {"answer_points": instructions}},
    )
    return report


def test_baseline_comparison_shows_changes_and_flips() -> None:
    """評価者の指示を変えた後: 変わった構成・holdout の指標の差・判定の変わった項目を出す。"""
    base = _report("pass", instructions="a")
    target = _report("fail", instructions="b")

    comparison = compare_reports(base, target)

    assert comparison["comparable"] is True
    assert comparison["changes"] == ["判定の指示（answer_points）の sha256 が違う"]
    holdout = comparison["aspects"]["answer_points"]["holdout"]
    assert holdout["false_pass_rate"] == {"base": 1.0, "target": 0.0, "delta": -1.0}
    assert comparison["flipped"] == [
        {
            "id": "h2",
            "aspect": "answer_points",
            "split": "holdout",
            "human": "fail",
            "base": "pass",
            "target": "fail",
        }
    ]


def test_baseline_with_other_labels_is_not_comparable() -> None:
    comparison = compare_reports(_report("pass"), _report("pass", labels_note="直した根拠"))

    assert comparison["comparable"] is False
    assert comparison["reasons"] == [
        "校正セットが違う（sha256 が一致しない。項目・人手の判定を変えた）"
    ]
    assert "aspects" not in comparison


def test_cli_runs_reaggregates_and_compares(tmp_path: Path) -> None:
    """CLI: 台本の評価者で校正 → 前の判定で集計だけやり直す → 基線と比べる。"""
    set_path = tmp_path / "set.json"
    set_path.write_text(
        _set(_item("h1", "holdout", "pass"), _item("h2", "holdout", "fail")).model_dump_json(),
        encoding="utf-8",
    )
    calls: list[str] = []

    async def judge(item: CalibrationItem) -> Judgement:
        calls.append(item.id)
        return _judged("pass")

    first = tmp_path / "first.json"
    assert main([str(set_path), "--output", str(first)], judge=(judge, {"model": "m"})) == 0
    assert sorted(calls) == ["h1", "h2"]

    second = tmp_path / "second.json"
    code = main(
        [
            str(set_path),
            "--judgements",
            str(first),
            "--baseline",
            str(first),
            "--output",
            str(second),
        ]
    )
    assert code == 0 and sorted(calls) == ["h1", "h2"]  # 評価者を呼ばない
    report = json.loads(second.read_text(encoding="utf-8"))
    assert report["provenance"]["evaluator"] == {"model": "m"}
    assert report["aspects"]["answer_points"]["splits"]["holdout"]["false_pass_items"] == ["h2"]
    assert report["baseline"]["comparable"] is True and report["baseline"]["flipped"] == []


def test_cli_rejects_broken_inputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps({"rubric_id": "x", "items": [{"id": "a"}]}), encoding="utf-8")
    assert main([str(broken), "--output", str(tmp_path / "out.json")]) == 2

    set_path = tmp_path / "set.json"
    set_path.write_text(_set(_item("h1", "holdout", "pass")).model_dump_json(), encoding="utf-8")
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"judgements": {}}), encoding="utf-8")
    code = main([str(set_path), "--judgements", str(empty), "--output", str(tmp_path / "o.json")])
    assert code == 2
    assert "判定の無い項目" in capsys.readouterr().err
    assert not (tmp_path / "o.json").exists()
