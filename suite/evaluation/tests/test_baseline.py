"""製品横断の評価の基線（#3240 の段 1・段 2 → suite）。

`fixtures/agent_baseline/` の合成のケースと、共有の fixture の公開の Agent MCP の出力を、suite の
Target・Evaluator・Report に流して、次を確かめる（fixture の README.md「保証すること」）。

- 旧形式の推定（`--infer-missing-outcome`）を選ぶと、段 2 の rag の CLI の採点・集計・分母・比較の
  要約と同じ値になる（`expected.scores` / `expected.metrics` / `expected.denominators` /
  `expected.cli`。差は `STAGE2_DIFFS` と採点の版だけ）。
- 既定（推定しない）の採点・集計・分母は、suite の基線（`expected.v3`）と同じ。
- 保存した観測から採点し直すと、流したときと同じ値になる（Target を呼ばない）。

意図して値を変えたら `UPDATE_SUITE_EVALUATION_BASELINE=1 uv run pytest tests/test_baseline.py` で
`expected.v3` を書き直し、変わった値を README.md と PR に書く。
"""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from baseline_support import (
    AGENT_TOOLS,
    CASE_FIXTURES,
    FIXTURES,
    OBSERVATIONS,
    PUBLIC_RUNS,
    SUMMARIES,
    Replay,
    final_observation,
    load_json,
    replay,
    run_ids,
)

from pr_suite_evaluation.dataset import METRIC_NAMES
from pr_suite_evaluation.evaluators import CaseScore
from pr_suite_evaluation.experiment import ExperimentRecord, ScorerConfig, rescore
from pr_suite_evaluation.report import Report, summarize_result, summary_markdown
from pr_suite_evaluation.targets.agent_mcp import (
    CANCEL_SCHEMA_VERSION,
    OBSERVATION_SCHEMA_VERSION,
    RUN_OUTPUT_SCHEMA_VERSION,
)

UPDATE = os.environ.get("UPDATE_SUITE_EVALUATION_BASELINE") == "1"
DEPENDENCIES: dict[str, Any] = load_json("agent_internal_dependencies.json")
# 段 2 と比べる採点の項目（製品の評価のケースの結果のうち、suite も持つもの）。
SCORE_FIELDS: tuple[str, ...] = (
    "status",
    "category",
    "split",
    "reasoning_type",
    "hops",
    "retrieved_document_ids",
    "relevant_document_ids",
    "hit_document_ids",
    "required_evidence_recall",
    "mrr",
    "answer_correctness",
    "requirement_coverage",
    "claim_support_rate",
    "response_decision_accuracy",
    "expected_decisions",
    "observed_outcome",
    "outcome_source",
    "missing_evidence",
    "turn_results",
    "failure_reasons",
    "elapsed_ms",
    "error_type",
    "error_stage",
)
# 段 2 の結果にあり、suite に無い項目（RAG の評価の項目。Agent の経路ではいつも空）。
RAG_ONLY_SCORE_FIELDS: dict[str, Any] = {"answer_evaluation": None}
RAG_ONLY_METRIC_FIELDS: dict[str, Any] = {"evaluation_suite": "standard", "comparison": None}
# 旧形式の推定を選んだときに、段 2 から意図して変わる値（README.md「意図して作り直した値」）。
STAGE2_DIFFS: dict[str, dict[str, tuple[Any, Any]]] = {
    # Run を作れなかった往復にも、失敗した往復の段を付ける。
    "run-create-error": {"error_stage": (None, "turn_0")},
}
STAGE2_VERSION = "six_metrics_v1+agent_v2"
LEGACY_VERSION = "six_metrics_v1+agent_v3+legacy_outcome"
V3_VERSION = "six_metrics_v1+agent_v3"


def _case_fixture_path(fixture_id: str) -> Path:
    return FIXTURES / "cases" / f"{fixture_id}.json"


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def case_scores(result: CaseScore) -> dict[str, Any]:
    """比べる採点の項目（往復の trace_id は Agent の Run の ID なので除く）。"""
    dumped = result.model_dump(mode="json")
    scores = {field: dumped[field] for field in SCORE_FIELDS}
    scores["turn_results"] = [
        {key: value for key, value in turn.items() if key != "trace_id"}
        for turn in dumped["turn_results"]
    ]
    return scores


def v3_scores(result: CaseScore) -> dict[str, Any]:
    """suite の基線の採点の項目（段 2 の項目と、評価不能の記録）。"""
    dumped = result.model_dump(mode="json")
    return {
        **case_scores(result),
        "undetermined_evidence": dumped["undetermined_evidence"],
        "metric_status": dumped["metric_status"],
    }


def stage2_denominators(fixtures: Sequence[Mapping[str, Any]], report: Report) -> dict[str, Any]:
    """段 2 の分母の形（観測の種類ごとの数・指標ごとの測れた / 測れない数）。"""
    observations: dict[str, int] = {}
    for fixture in fixtures:
        name = final_observation(fixture)
        observations[name] = observations.get(name, 0) + 1
    succeeded = report.case_count - report.error_count
    return {
        "case_count": report.case_count,
        "error_count": report.error_count,
        "observations": dict(sorted(observations.items())),
        "measured": {name: report.metric_case_counts[name] for name in METRIC_NAMES},
        "not_evaluable": {
            name: succeeded - report.metric_case_counts[name] for name in METRIC_NAMES
        },
    }


def report_metrics(report: Report) -> dict[str, Any]:
    return report.model_dump(mode="json", exclude={"case_results", "denominators"})


def _suite_fixtures(suite: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [CASE_FIXTURES[fixture_id] for fixture_id in suite["case_fixtures"]]


# ---- fixture の整合 --------------------------------------------------------------------------


def test_fixtures_are_consistent() -> None:
    """fixture の id がケースの id と同じで、失敗した往復の後に往復を書いていない。"""
    assert CASE_FIXTURES
    for fixture_id, fixture in CASE_FIXTURES.items():
        assert fixture["case"]["id"] == fixture_id
        observations = [turn["observation"] for turn in fixture["turns"]]
        assert set(observations) <= OBSERVATIONS, fixture_id
        assert all(item == "completed" for item in observations[:-1]), fixture_id
        assert len(observations) <= 1 + len(fixture["case"].get("turns", [])), fixture_id
        for turn in fixture["turns"]:
            assert (turn["observation"] == "api_error") == ("run_id" not in turn), fixture_id
            if "run_id" in turn:
                assert turn["run_id"] in PUBLIC_RUNS, fixture_id
        for field, value in RAG_ONLY_SCORE_FIELDS.items():
            assert fixture["expected"]["scores"][field] == value, (fixture_id, field)
    everything = next(suite for suite in SUMMARIES if suite["id"] == "all")
    assert sorted(everything["case_fixtures"]) == sorted(CASE_FIXTURES)
    assert {final_observation(fixture) for fixture in CASE_FIXTURES.values()} == OBSERVATIONS
    used = {run_id for fixture in CASE_FIXTURES.values() for run_id in run_ids(fixture)}
    assert used == set(PUBLIC_RUNS)
    for suite in SUMMARIES:
        for field, value in RAG_ONLY_METRIC_FIELDS.items():
            assert suite["expected"]["metrics"][field] == value, (suite["id"], field)


def test_target_reads_the_output_versions_of_the_public_contract() -> None:
    def version(tool: str) -> int:
        return int(AGENT_TOOLS[tool]["outputSchema"]["properties"]["schema_version"]["default"])

    assert version("agent_chat") == version("agent_get_run") == RUN_OUTPUT_SCHEMA_VERSION
    assert version("agent_answer_confirmation") == RUN_OUTPUT_SCHEMA_VERSION
    assert version("agent_get_run_observation") == OBSERVATION_SCHEMA_VERSION
    assert version("agent_cancel_run") == CANCEL_SCHEMA_VERSION


# ---- 段 2 との一致（旧形式の推定を選んだとき） ----------------------------------------------


@pytest.mark.parametrize("fixture_id", sorted(CASE_FIXTURES))
def test_legacy_mode_case_scores_match_the_stage2_baseline(fixture_id: str) -> None:
    fixture = CASE_FIXTURES[fixture_id]
    result = replay([fixture], infer_missing_outcome=True)
    actual = case_scores(result.record.report.case_results[0])
    expected = dict(fixture["expected"]["scores"])
    for field in RAG_ONLY_SCORE_FIELDS:
        expected.pop(field)
    for field, (before, after) in STAGE2_DIFFS.get(fixture_id, {}).items():
        assert expected[field] == before, field
        expected[field] = after
    assert actual == expected


@pytest.mark.parametrize("fixture_id", sorted(CASE_FIXTURES))
def test_requests_and_cancellations_match_the_stage2_baseline(fixture_id: str) -> None:
    """Agent に送る依頼（agent_chat の引数）と取り消す Run は段 2 と同じ。"""
    fixture = CASE_FIXTURES[fixture_id]
    result = replay([fixture])
    assert result.agent.chat_requests == fixture["expected"]["cli"]["chat_requests"]
    assert result.agent.cancelled == fixture["expected"]["cli"]["cancelled_run_ids"]
    # 業務 Agent の作成・公開はしない（MCP のツールは持たない。#3240 の Q4）。
    assert set(result.agent.tools) <= {
        "agent_chat",
        "agent_get_run",
        "agent_get_run_observation",
        "agent_cancel_run",
    }


@pytest.mark.parametrize("fixture_id", sorted(CASE_FIXTURES))
def test_run_summaries_match_the_stage2_run_records(fixture_id: str) -> None:
    """Run の要約（Target の診断）は段 2 の Run の記録と同じ（Run を作れなかった往復は足した）。"""
    fixture = CASE_FIXTURES[fixture_id]
    result = replay([fixture], infer_missing_outcome=True)
    runs = [run for run in result.record.target["runs"] if run["run_id"] is not None]
    expected = fixture["expected"]["cli"]["runs"]
    assert len(runs) == len(expected)
    for actual, before in zip(runs, expected, strict=True):
        status = "timeout" if actual["status"] == "timeout" else actual["target_status"]
        assert status == before["status"]
        for key, value in before.items():
            if key == "status":
                continue
            if key == "outcome" and value is None and before["outcome_source"] == "inferred":
                # 段 2 の Run の記録は、推定した対応のうち確認の質問だけを outcome に残していた。
                # suite の要約は推定した対応をそのまま出す（README.md「意図して作り直した値」）。
                assert actual[key] is not None
                continue
            assert actual[key] == value, key


@pytest.mark.parametrize("suite", SUMMARIES, ids=lambda suite: str(suite["id"]))
def test_legacy_mode_summary_matches_the_stage2_baseline(suite: dict[str, Any]) -> None:
    fixtures = _suite_fixtures(suite)
    result = replay(fixtures, suite.get("thresholds"), infer_missing_outcome=True)
    report = result.record.report
    expected = dict(suite["expected"]["metrics"])
    for field in RAG_ONLY_METRIC_FIELDS:
        expected.pop(field)
    assert expected.pop("metrics_version") == STAGE2_VERSION
    actual = report_metrics(report)
    assert actual.pop("metrics_version") == LEGACY_VERSION
    assert actual == expected
    assert stage2_denominators(fixtures, report) == suite["expected"]["denominators"]
    summary = summarize_result(report.model_dump(mode="json"))
    summary.pop("unevaluable")
    cli = suite["expected"]["cli"]
    assert summary == cli["summary"]
    assert summary_markdown({"D": summary}).splitlines() == cli["summary_markdown"]
    assert result.record.target["outcome_sources"] == cli["outcome_sources"]
    assert result.record.target["superseded_cases"] == cli["superseded_cases"]


# ---- suite の基線（既定。推定しない） ----------------------------------------------------------


def _update_case(fixture_id: str, result: Replay) -> None:
    path = _case_fixture_path(fixture_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["expected"]["v3"] = {"scores": v3_scores(result.record.report.case_results[0])}
    _write_json(path, data)


@pytest.mark.parametrize("fixture_id", sorted(CASE_FIXTURES))
def test_case_scores_match_the_suite_baseline(fixture_id: str) -> None:
    fixture = CASE_FIXTURES[fixture_id]
    result = replay([fixture])
    if UPDATE:
        _update_case(fixture_id, result)
        return
    assert v3_scores(result.record.report.case_results[0]) == fixture["expected"]["v3"]["scores"]


@pytest.mark.parametrize("suite", SUMMARIES, ids=lambda suite: str(suite["id"]))
def test_summary_matches_the_suite_baseline(suite: dict[str, Any]) -> None:
    result = replay(_suite_fixtures(suite), suite.get("thresholds"))
    report = result.record.report
    actual = {
        "metrics": report_metrics(report),
        "denominators": report.denominators.model_dump(mode="json"),
    }
    assert actual["metrics"]["metrics_version"] == V3_VERSION
    if UPDATE:
        path = FIXTURES / "summaries.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        for item in data["suites"]:
            if item["id"] == suite["id"]:
                item["expected"]["v3"] = actual
        _write_json(path, data)
        return
    assert actual == suite["expected"]["v3"]


@pytest.mark.parametrize("suite", SUMMARIES, ids=lambda suite: str(suite["id"]))
@pytest.mark.parametrize("legacy", [False, True], ids=["default", "legacy"])
def test_failed_cases_are_outside_the_metric_denominators(
    suite: dict[str, Any], legacy: bool
) -> None:
    """失敗したケースは error_count に入り、指標の分母（測れた・対象外・評価不能）に入らない。"""
    fixtures = _suite_fixtures(suite)
    report = replay(fixtures, suite.get("thresholds"), infer_missing_outcome=legacy).record.report
    failed = [fixture for fixture in fixtures if fixture["expected"]["scores"]["status"] == "error"]
    assert report.error_count == len(failed)
    succeeded = report.case_count - report.error_count
    for name, item in report.denominators.metrics.items():
        assert item.scored + item.not_applicable + item.unevaluable == succeeded, name
        assert item.scored == report.metric_case_counts[name], name
        assert sum(item.unevaluable_reasons.values()) == item.unevaluable, name
    assert sum(report.denominators.final_statuses.values()) == report.case_count
    assert sum(report.denominators.error_types.values()) == report.error_count
    if failed:
        assert report.passed is False


def test_completed_run_that_skipped_an_approval_tool_is_a_case_failure() -> None:
    """承認の要るツールを呼ばずに答えた公開の MCP の Run は、ケースの失敗（#3240 の Q7）。"""
    fixture = CASE_FIXTURES["approval-skipped-run"]
    run = PUBLIC_RUNS[run_ids(fixture)[0]]
    assert run["agent_get_run"]["status"] == "completed"
    assert run["agent_get_run"]["skipped_approval_tools"]
    result = replay([fixture])
    scores = case_scores(result.record.report.case_results[0])
    assert (scores["status"], scores["error_type"], scores["error_stage"]) == (
        "error",
        "AgentRunApprovalSkipped",
        "turn_0",
    )
    assert result.record.report.denominators.final_statuses == {"approval_skipped": 1}
    # 観測の approval_skipped だけでも失敗にする。
    observed_only = copy.deepcopy(dict(PUBLIC_RUNS))
    observed_only[run_ids(fixture)[0]]["agent_get_run"]["skipped_approval_tools"] = []
    again = replay([fixture], runs=observed_only).record.report.case_results[0]
    assert again.error_type == "AgentRunApprovalSkipped"


# ---- 保存した観測からの採点 ------------------------------------------------------------------


@pytest.mark.parametrize("suite", SUMMARIES, ids=lambda suite: str(suite["id"]))
def test_rescoring_saved_observations_reproduces_the_report(
    suite: dict[str, Any], tmp_path: Path
) -> None:
    """保存した結果のファイルを読み直して採点し直すと、同じ集計になる（Target を呼ばない）。

    推定の有無を変えた採点し直しは、その設定で流したときと同じ集計になる。
    """
    fixtures = _suite_fixtures(suite)
    result = replay(fixtures, suite.get("thresholds"))
    path = tmp_path / "record.json"
    path.write_text(result.record.model_dump_json(), encoding="utf-8")
    saved = ExperimentRecord.model_validate_json(path.read_text(encoding="utf-8"))
    scorer = ScorerConfig(run_timeout_seconds=2.0)
    same = rescore(saved, result.dataset, scorer)
    assert same.report == result.record.report
    assert same.provenance["observed_dataset"] == result.record.provenance["dataset"]
    legacy = rescore(
        saved, result.dataset, ScorerConfig(infer_missing_outcome=True, run_timeout_seconds=2.0)
    )
    expected = replay(fixtures, suite.get("thresholds"), infer_missing_outcome=True).record.report
    assert legacy.report == expected
    assert legacy.provenance["scorer"]["metrics_version"] == LEGACY_VERSION
    assert (
        legacy.provenance["scorer"]["config_sha256"] != same.provenance["scorer"]["config_sha256"]
    )


def test_provenance_records_versions_and_hashes() -> None:
    result = replay([CASE_FIXTURES["answered-agent-outcome"]])
    provenance = result.record.provenance
    assert provenance["dataset"]["sha256"] == result.dataset.sha256
    assert provenance["dataset"]["case_ids"] == ["answered-agent-outcome"]
    assert provenance["target"]["kind"] == "agent_mcp"
    assert provenance["target"]["agent_id"] == "agent-baseline"
    assert len(provenance["target"]["config_sha256"]) == 64
    assert provenance["scorer"]["metrics_version"] == V3_VERSION
    assert provenance["scorer"]["config"]["infer_missing_outcome"] is False
    # 資格情報は結果に残さない。
    assert "prak_" not in result.record.model_dump_json()


# ---- 公開の Agent MCP の項目への依存 ---------------------------------------------------------


def _observation(run: dict[str, Any]) -> dict[str, Any]:
    observation: dict[str, Any] = run["agent_get_run_observation"]
    return observation


def _strip_citation_ids(run: dict[str, Any]) -> None:
    for item in [*_observation(run)["evidence"], *run["agent_get_run"]["sources"]]:
        item["document_id"] = None
        item["chunk_id"] = None


def _strip_locators(run: dict[str, Any]) -> None:
    for item in _observation(run)["evidence"]:
        item["locator"] = {}


def _strip_answer_outcome(run: dict[str, Any]) -> None:
    run["agent_get_run"]["outcome"] = None
    _observation(run)["outcome"] = None


def _strip_validation_status(run: dict[str, Any]) -> None:
    _observation(run)["validation_status"] = None


def _strip_usage(run: dict[str, Any]) -> None:
    _observation(run)["usage"] = None


def _strip_include_superseded(run: dict[str, Any]) -> None:
    for call in _observation(run)["tool_calls"]:
        call["include_superseded"] = None


ABLATIONS: dict[str, Callable[[dict[str, Any]], None]] = {
    "strip_citation_ids": _strip_citation_ids,
    "strip_locators": _strip_locators,
    "strip_answer_outcome": _strip_answer_outcome,
    "strip_validation_status": _strip_validation_status,
    "strip_usage": _strip_usage,
    "strip_include_superseded": _strip_include_superseded,
}


def _ablated_runs(fixture: Mapping[str, Any], ablation: str) -> dict[str, dict[str, Any]]:
    runs = copy.deepcopy(dict(PUBLIC_RUNS))
    for run_id in run_ids(fixture):
        ABLATIONS[ablation](runs[run_id])
    return runs


def _has_field(tool: str, path: str) -> bool:
    schema = AGENT_TOOLS[tool]["outputSchema"]
    node: Mapping[str, Any] = schema
    for part in path.split("."):
        name = part.removesuffix("[]")
        properties = node.get("properties", {})
        if name not in properties:
            return False
        node = properties[name]
        if part.endswith("[]"):
            node = node["items"]
        reference = node.get("$ref") or next(
            (option["$ref"] for option in node.get("anyOf", []) if "$ref" in option), None
        )
        if reference:
            node = schema["$defs"][reference.rsplit("/", 1)[-1]]
    return True


def test_every_dependency_names_its_public_fields_and_an_ablation() -> None:
    for dependency in DEPENDENCIES["dependencies"]:
        assert dependency["public_agent_mcp"] == "public", dependency["id"]
        assert dependency["public_fields"], dependency["id"]
        for field in dependency["public_fields"]:
            tool, _, path = field.partition(".")
            assert _has_field(tool, path), (dependency["id"], field)
        assert dependency["ablation"] in ABLATIONS, dependency["id"]
        assert dependency["witness"]["fixture"] in CASE_FIXTURES, dependency["id"]
    for item in DEPENDENCIES["not_ablatable"]:
        assert item["public_agent_mcp"] in {"public", "partial"}, item["id"]
        for field in item.get("public_fields", []):
            tool, _, path = field.partition(".")
            assert _has_field(tool, path), (item["id"], field)
    for item in DEPENDENCIES["not_added"]:
        assert item["public_agent_mcp"] == "not_added", item["id"]


@pytest.mark.parametrize(
    "dependency", DEPENDENCIES["dependencies"], ids=lambda dependency: str(dependency["id"])
)
def test_scores_depend_only_on_public_agent_mcp_fields(dependency: dict[str, Any]) -> None:
    """公開の Agent MCP の項目を消すと、一覧に書いた項目（だけ）が変わる（旧形式の推定を選んだとき。

    段 2 と同じ規則で比べる。既定〔推定しない〕では outcome を消すと判断の正しさが評価不能になる）。
    """
    witness = dependency["witness"]
    fixture = CASE_FIXTURES[witness["fixture"]]
    baseline = replay([fixture], infer_missing_outcome=True).record
    ablated = replay(
        [fixture], runs=_ablated_runs(fixture, dependency["ablation"]), infer_missing_outcome=True
    ).record
    before = case_scores(baseline.report.case_results[0])
    after = case_scores(ablated.report.case_results[0])
    changed = {field for field in SCORE_FIELDS if before[field] != after[field]}
    assert changed == set(witness["score_changes"])
    for field in witness["run_changes"]:
        assert [run[field] for run in baseline.target["runs"]] != [
            run[field] for run in ablated.target["runs"]
        ], field


def test_without_outcome_the_decision_is_unevaluable_by_default() -> None:
    """outcome を消した Run は、既定では判断の正しさを推定せず評価不能（理由つき）にする。"""
    fixture = CASE_FIXTURES["answered-agent-outcome"]
    ablated = replay([fixture], runs=_ablated_runs(fixture, "strip_answer_outcome")).record
    result = ablated.report.case_results[0]
    assert result.status == "success"
    assert result.response_decision_accuracy is None
    assert result.metric_status["response_decision_accuracy"].model_dump() == {
        "status": "unevaluable",
        "reason": "outcome_missing",
    }
    assert ablated.report.denominators.metrics["response_decision_accuracy"].unevaluable == 1
