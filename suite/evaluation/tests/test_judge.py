"""LLM による回答の判定と、その人手の判定による校正（#3418・#2577）。

判定のモデルは台本（`fixtures/judge_calibration/judge_outputs.json` の録画の代わりの応答）で、
実サービスは呼ばない。OCI Enterprise AI の呼び方（URL・ヘッダー・構造化出力・失敗の分類）は
MockTransport で確かめる。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from test_rag_mcp_target import FakeRagMcp, _evidence, _mcp, _output

from pr_suite_evaluation.cli import main
from pr_suite_evaluation.dataset import Dataset, LoadedDataset
from pr_suite_evaluation.experiment import ExperimentRecord, ScorerConfig, run_experiment
from pr_suite_evaluation.judge.calibration import (
    CalibrationReport,
    CalibrationThresholds,
    calibrate,
    judge_labels,
    load_labels,
)
from pr_suite_evaluation.judge.client import (
    EnterpriseAiJudgeClient,
    JudgeCallError,
    JudgeConfigError,
    JudgeModelConfig,
    JudgeReply,
)
from pr_suite_evaluation.judge.judging import (
    JudgeInput,
    JudgeSettings,
    calibration_gate,
    judge_item,
    prompt_sha256,
)
from pr_suite_evaluation.judge.rubric import Rubric, load_rubric
from pr_suite_evaluation.judge.run import judge_record
from pr_suite_evaluation.targets.rag_mcp import RagMcpTarget, RagTargetConfig

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "judge_calibration"
LABELS = FIXTURES / "labels.json"
OUTPUTS: dict[str, Any] = json.loads((FIXTURES / "judge_outputs.json").read_text("utf-8"))
RUBRIC = load_rubric()
MODEL = "synthetic-judge"


class ScriptedJudge:
    """台本の判定のモデル。項目の質問と回答から台本の応答を選ぶ（回数と入力を記録する）。"""

    def __init__(self, replies: Mapping[str, Any] | None = None, *, model: str = MODEL) -> None:
        labels = load_labels(LABELS)
        self._by_answer = {item.answer: item.id for item in labels.items}
        self._replies = dict(replies or OUTPUTS["outputs"])
        self._model = model
        self.prompts: list[dict[str, Any]] = []
        self.schemas: list[Mapping[str, Any]] = []

    @property
    def model(self) -> str:
        return self._model

    def complete(
        self, *, instructions: str, prompt: str, schema: Mapping[str, Any], schema_name: str
    ) -> JudgeReply:
        payload = json.loads(prompt)
        self.prompts.append(payload)
        self.schemas.append(schema)
        key = self._by_answer.get(payload["answer"], payload["answer"])
        reply = self._replies[key]
        if isinstance(reply, Exception):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return JudgeReply(text=text, model=self._model, usage={"input_tokens": 10})


def _report(thresholds: CalibrationThresholds | None = None) -> CalibrationReport:
    labels = load_labels(LABELS)
    settings = JudgeSettings()
    judgements = judge_labels(labels, RUBRIC, ScriptedJudge(), settings)
    return calibrate(
        labels,
        RUBRIC,
        judgements=judgements,
        judge_provenance={"provider": "oci_enterprise_ai", "model": MODEL},
        settings=settings,
        thresholds=thresholds or CalibrationThresholds(),
    )


# ---- rubric ----------------------------------------------------------------------------------


def test_default_rubric_has_versioned_aspects_and_a_strict_output_schema() -> None:
    assert (RUBRIC.id, RUBRIC.version) == ("answer_quality", "1")
    assert RUBRIC.aspect_ids == ["requirement_coverage", "answer_correctness", "claim_support"]
    assert len(RUBRIC.sha256) == 64
    schema = RUBRIC.output_schema()
    item = schema["properties"]["aspects"]["items"]
    assert item["properties"]["label"]["enum"] == ["pass", "fail", "cannot_judge"]
    assert item["additionalProperties"] is False
    # 指示か出力の形を変えると hash が変わる（校正をやり直す）。
    changed = Rubric.model_validate({**RUBRIC.model_dump(), "instructions": "別の指示"})
    assert changed.sha256 != RUBRIC.sha256
    assert prompt_sha256(changed) != prompt_sha256(RUBRIC)
    with pytest.raises(ValueError, match="知らない指標"):
        Rubric.model_validate(
            {**RUBRIC.model_dump(), "aspects": [{"id": "x", "metric": "unknown", "requires": []}]}
        )


# ---- 1 件の判定 -------------------------------------------------------------------------------


ITEM = JudgeInput(
    item_id="x",
    question="合成の質問",
    answer="合成の回答",
    standard_answer="合成の標準回答",
    evidence=(("e1", "合成の根拠"),),
)
GOOD = {
    "aspects": [
        {"aspect_id": aspect_id, "label": "pass", "reason": "r"} for aspect_id in RUBRIC.aspect_ids
    ]
}


def test_invalid_outputs_are_retried_then_unevaluable() -> None:
    missing = {"aspects": GOOD["aspects"][:2]}
    judge = ScriptedJudge({"合成の回答": "not json"})
    result = judge_item(ITEM, RUBRIC, judge, JudgeSettings(max_attempts=2))
    assert (result.status, result.error_code, result.attempts) == (
        "failed",
        "judge_invalid_output",
        2,
    )
    assert len(judge.prompts) == 2
    assert {item.unevaluable_reason for item in result.aspects.values()} == {"judge_invalid_output"}
    for bad in (missing, {"aspects": [*GOOD["aspects"], GOOD["aspects"][0]]}):
        again = judge_item(ITEM, RUBRIC, ScriptedJudge({"合成の回答": bad}), JudgeSettings())
        assert again.status == "failed"


def test_call_failures_and_cannot_judge_are_unevaluable() -> None:
    error = JudgeCallError("x", error_code="judge_rate_limit")
    failed = judge_item(ITEM, RUBRIC, ScriptedJudge({"合成の回答": error}), JudgeSettings())
    assert (failed.status, failed.error_code, failed.attempts) == ("failed", "judge_rate_limit", 1)
    unsure = {
        "aspects": [
            {**GOOD["aspects"][0], "label": "cannot_judge"},
            *GOOD["aspects"][1:],
        ]
    }
    result = judge_item(ITEM, RUBRIC, ScriptedJudge({"合成の回答": unsure}), JudgeSettings())
    assert result.status == "completed"
    assert result.aspects["requirement_coverage"].unevaluable_reason == "judge_cannot_judge"
    assert result.aspects["claim_support"].label == "pass"


def test_aspects_needing_a_standard_answer_and_empty_answers_are_not_applicable() -> None:
    no_standard = JudgeInput(**{**ITEM.__dict__, "standard_answer": None})
    only_support = {"aspects": [GOOD["aspects"][2]]}
    judge = ScriptedJudge({"合成の回答": only_support})
    result = judge_item(no_standard, RUBRIC, judge, JudgeSettings())
    assert judge.prompts[0]["aspects"] == ["claim_support"]
    assert result.aspects["requirement_coverage"].status == "not_applicable"
    assert result.aspects["claim_support"].status == "scored"
    empty = judge_item(
        JudgeInput(**{**ITEM.__dict__, "answer": " "}), RUBRIC, judge, JudgeSettings()
    )
    assert (empty.status, empty.error_code) == ("skipped", "no_answer")


def test_long_evidence_is_cut_and_recorded() -> None:
    item = JudgeInput(**{**ITEM.__dict__, "evidence": (("e1", "a" * 30), ("e2", "b" * 30))})
    judge = ScriptedJudge({"合成の回答": GOOD})
    result = judge_item(item, RUBRIC, judge, JudgeSettings(max_evidence_chars=40))
    assert [evidence["id"] for evidence in judge.prompts[0]["evidence"]] == ["e1"]
    assert result.evidence_truncated is True


# ---- 校正 -------------------------------------------------------------------------------------


def test_calibration_reports_agreement_per_aspect_and_split() -> None:
    report = _report()
    support = report.aspects["claim_support"].splits["holdout"]
    # holdout の 8 件: 人手の fail 3（h02・h03・h07）のうち h02 を判定は pass（誤合格）、
    # 人手の pass 5 のうち h04 を判定は fail（誤不合格）。
    assert support.confusion == {"pass": {"pass": 4, "fail": 1}, "fail": {"pass": 1, "fail": 2}}
    assert (support.total, support.judged, support.accuracy) == (8, 8, round(6 / 8, 4))
    assert support.false_pass_rate == round(1 / 3, 4)
    assert support.false_fail_rate == round(1 / 5, 4)
    assert sorted(support.disagreements) == [
        "h02-unsupported-claim",
        "h04-general-rule-over-applied",
    ]
    coverage = report.aspects["requirement_coverage"].splits["holdout"]
    assert (coverage.judged, coverage.unjudged) == (7, 1)
    assert report.aspects["requirement_coverage"].splits["dev"].accuracy == 1.0
    # 既定の閾値（holdout 50 件）では、合成の 8 件で合格にしない。
    assert report.passed is False
    assert any("件数 8 < 50" in failure for failure in report.aspects["claim_support"].failures)
    assert report.provenance["rubric"]["sha256"] == RUBRIC.sha256
    assert report.provenance["labels"]["item_count"] == 10
    assert report.provenance["thresholds"]["min_kappa"] == 0.6


def test_calibration_gate_passes_only_aspects_meeting_every_threshold() -> None:
    loose = CalibrationThresholds(min_items=5, min_kappa=0.5, max_false_pass_rate=0.4)
    report = _report(loose)
    passed = {aspect_id: item.passed for aspect_id, item in report.aspects.items()}
    # 判定率が 7/8 の requirement_coverage は min_coverage 0.95 を満たさない。
    assert passed == {
        "requirement_coverage": False,
        "answer_correctness": passed["answer_correctness"],
        "claim_support": passed["claim_support"],
    }
    assert passed["requirement_coverage"] is False
    data = report.model_dump(mode="json")
    gate, status = calibration_gate(data, rubric=RUBRIC, model=MODEL)
    assert gate["requirement_coverage"] is False
    assert status in {"partially_calibrated", "uncalibrated"}
    # rubric かモデルが違う校正は使わない。
    assert calibration_gate(data, rubric=RUBRIC, model="other")[1] == "mismatch"
    assert calibration_gate(None, rubric=RUBRIC, model=MODEL) == ({}, "uncalibrated")


def test_labels_must_match_the_rubric(tmp_path: Path) -> None:
    labels = load_labels(LABELS)
    other = labels.model_copy(update={"rubric_id": "other"})
    with pytest.raises(ValueError, match="rubric"):
        judge_labels(other, RUBRIC, ScriptedJudge(), JudgeSettings())
    raw = json.loads(LABELS.read_text("utf-8"))
    raw["items"][0]["labels"]["maybe"] = "pass"
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(raw, ensure_ascii=False), "utf-8")
    with pytest.raises(ValueError, match="rubric に無い観点"):
        judge_labels(load_labels(path), RUBRIC, ScriptedJudge(), JudgeSettings())


# ---- OCI Enterprise AI の呼び方 ----------------------------------------------------------------


def _config(**fields: Any) -> JudgeModelConfig:
    values = {
        "endpoint": "https://inference.example.invalid/openai/v1/",
        "model": "judge-model",
        "api_key": "synthetic-key",
        "project_ocid": "ocid1.synthetic",
    }
    return JudgeModelConfig(**{**values, **fields})


def test_enterprise_ai_client_posts_the_structured_output_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {
            "model": "judge-model",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": '{"aspects": []}'}]}
            ],
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        return httpx.Response(200, json=body)

    client = EnterpriseAiJudgeClient(
        httpx.Client(transport=httpx.MockTransport(handler)), _config()
    )
    reply = client.complete(
        instructions="指示",
        prompt="{}",
        schema=RUBRIC.output_schema(),
        schema_name="answer_judgement",
    )
    assert reply.text == '{"aspects": []}'
    assert reply.usage == {"input_tokens": 5, "output_tokens": 3}
    request = seen[0]
    assert str(request.url) == "https://inference.example.invalid/openai/v1/responses"
    assert request.headers["authorization"] == "Bearer synthetic-key"
    assert request.headers["openai-project"] == "ocid1.synthetic"
    body = json.loads(request.content)
    assert body["model"] == "judge-model"
    assert "seed" not in body
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True
    assert body["text"]["format"]["schema"] == RUBRIC.output_schema()


def test_enterprise_ai_client_classifies_http_failures() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    client = EnterpriseAiJudgeClient(
        httpx.Client(transport=httpx.MockTransport(handler)), _config()
    )
    with pytest.raises(JudgeCallError) as error:
        client.complete(instructions="i", prompt="{}", schema={}, schema_name="s")
    assert error.value.error_code == "judge_rate_limit"


def test_model_config_comes_from_suite_env_without_a_default_model() -> None:
    with pytest.raises(JudgeConfigError, match="SUITE_EVALUATION_JUDGE_MODEL"):
        JudgeModelConfig.from_env(
            {
                "SUITE_EVALUATION_JUDGE_ENDPOINT": "https://x/v1",
                "SUITE_EVALUATION_JUDGE_API_KEY": "synthetic-secret",
            }
        )
    config = JudgeModelConfig.from_env(
        {
            "SUITE_EVALUATION_JUDGE_ENDPOINT": "https://host.example.invalid/openai/v1",
            "SUITE_EVALUATION_JUDGE_API_KEY": "synthetic-secret",
            "SUITE_EVALUATION_JUDGE_MODEL": "m",
        }
    )
    assert config.provenance() == {
        "provider": "oci_enterprise_ai",
        "endpoint_host": "host.example.invalid",
        "model": "m",
        "max_output_tokens": None,
    }
    assert "synthetic-secret" not in repr(config)


# ---- 保存した観測の判定 ------------------------------------------------------------------------


def _rag_record(standard_answer: str | None) -> tuple[ExperimentRecord, LoadedDataset]:
    case: dict[str, Any] = {"id": "a", "query": "合成の記録 R の保管の期間は？"}
    if standard_answer:
        case["standard_answer"] = standard_answer
    dataset = LoadedDataset(
        dataset=Dataset.model_validate(
            {"cases": [case], "thresholds": {"answer_correctness": 0.8}}
        ),
        name="synthetic",
        sha256="1" * 64,
    )
    output = _output(
        "trace-a", evidence=[_evidence("doc-r", "c1", "記録 R は作成の日から 5 年間保管する。")]
    )
    output["answer"] = "記録 R は 5 年間保管します。"
    config = RagTargetConfig(mcp_url="http://rag/api/mcp", knowledge_base_ids=("kb",))
    record = run_experiment(
        dataset,
        RagMcpTarget(_mcp(FakeRagMcp([output])), config),
        target_config=config.provenance(),
        scorer=ScorerConfig(target_kind="rag_mcp"),
        log=lambda _: None,
    )
    return record, dataset


def test_judge_record_keeps_judgements_separate_and_gates_by_calibration() -> None:
    record, dataset = _rag_record("5 年間。")
    judge = ScriptedJudge()
    judged = judge_record(
        record,
        dataset,
        scorer=ScorerConfig(target_kind="rag_mcp"),
        rubric=RUBRIC,
        client=judge,
        judge_provenance={"model": MODEL},
        settings=JudgeSettings(),
    )
    # 決定的な採点は変えない（回答の指標は評価不能のまま）。
    assert judged.report == record.report
    assert judged.report.case_results[0].metric_status["answer_correctness"].reason == (
        "answer_judge_unavailable"
    )
    assert judge.prompts[0]["evidence"] == [
        {"id": "doc-r#c1", "text": "記録 R は作成の日から 5 年間保管する。"}
    ]
    assert judged.judge is not None
    summary = judged.judge.summary
    assert summary.calibration_status == "uncalibrated"
    correctness = summary.aspects["answer_correctness"]
    assert (correctness.pass_rate, correctness.threshold, correctness.passed) == (1.0, 0.8, None)
    assert judged.judge.provenance["rubric"]["sha256"] == RUBRIC.sha256
    # 同じ rubric・モデルで校正に通った観点だけ合否を出す（合成の校正の結果を作って確かめる）。
    calibration = {
        "provenance": {"rubric": {"sha256": RUBRIC.sha256}, "judge": {"model": MODEL}},
        "aspects": {"answer_correctness": {"passed": True}},
        "passed": False,
    }
    gated = judge_record(
        record,
        dataset,
        scorer=ScorerConfig(target_kind="rag_mcp"),
        rubric=RUBRIC,
        client=ScriptedJudge(),
        judge_provenance={"model": MODEL},
        settings=JudgeSettings(),
        calibration=calibration,
    )
    assert gated.judge is not None
    assert gated.judge.summary.calibration_status == "partially_calibrated"
    assert gated.judge.summary.aspects["answer_correctness"].passed is True
    assert gated.judge.summary.aspects["claim_support"].passed is None


# ---- CLI ------------------------------------------------------------------------------------


def test_calibrate_cli_recomputes_from_saved_judgements_without_the_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    saved = tmp_path / "calibration.json"
    saved.write_text(_report().model_dump_json(), encoding="utf-8")
    output = tmp_path / "again.json"
    args = ["calibrate", str(LABELS), "--judgements", str(saved), "--min-items", "5"]
    assert main([*args, "--output", str(output)]) == 0
    report = CalibrationReport.model_validate_json(output.read_text("utf-8"))
    assert report.provenance["judge"]["model"] == MODEL
    assert report.provenance["thresholds"]["min_items"] == 5
    assert "claim_support: passed=" in capsys.readouterr().err


def test_judge_cli_requires_the_judge_model_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record, _dataset = _rag_record("5 年間。")
    path = tmp_path / "a.json"
    path.write_text(record.model_dump_json(), encoding="utf-8")
    dataset = tmp_path / "set.json"
    dataset.write_text(
        json.dumps({"cases": [{"id": "a", "query": "q", "standard_answer": "5 年間。"}]}),
        encoding="utf-8",
    )
    for name in ("ENDPOINT", "API_KEY", "MODEL"):
        monkeypatch.delenv(f"SUITE_EVALUATION_JUDGE_{name}", raising=False)
    args = ["judge", str(path), "--dataset", str(dataset), "--output", str(tmp_path / "j.json")]
    assert main(args) == 2
    assert "SUITE_EVALUATION_JUDGE_MODEL" in capsys.readouterr().err
