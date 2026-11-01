"""検索の工程ごとの除去実験（#61）。合成の結果・合成の外部の応答だけの決定論テスト。

- 構成（full と工程を 1 つずつ無効にした構成）が、評価の上書きとして本番の pipeline の設定に届く。
- 品質の差・悪化したケース・根拠の位置ごとの再現率・工程の記録・呼び出しの数・token を表に出す。
- 工程の記録・呼び出し・品質に差が無い構成は「非適用の可能性」、full で無効の工程は流さない。
- CLI で構成ごとに記録し、鍵なしで同じ除去実験を再生できる。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch
from rag_engine.resources.external_calls import external_call

from app import config as config_module
from app.config import Settings, get_settings
from app.rag import evaluation_run_cli
from app.rag.evaluation import EvaluationRunner
from app.rag.evaluation_ablation import (
    FULL,
    AblationConfig,
    AblationRun,
    ablation_configs,
    ablation_report,
)
from app.rag.pipeline import SearchStageProgressCallback
from app.schemas.evaluation import (
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationMetrics,
    EvaluationRagOverrides,
    EvaluationRunRequest,
)
from app.schemas.search import SearchRequest, SearchResponse


def test_configs_disable_one_stage_each_and_skip_already_off() -> None:
    settings = Settings(rag_rerank_enabled=True, rag_section_expansion_enabled=False)
    base = EvaluationRagOverrides(rrf_k=10)

    configs = ablation_configs(settings, base, ["rerank", "section_expansion"], full_again=True)

    assert [config.name for config in configs] == [
        "full",
        "no-rerank",
        "no-section_expansion",
        "full-again",
    ]
    no_rerank = configs[1].overrides
    assert no_rerank is not None
    # full の上書き（rrf_k）は保ったまま、その工程だけを無効にする。
    assert (no_rerank.rerank_enabled, no_rerank.rrf_k) == (False, 10)
    assert configs[2].skipped_reason == "already_off"
    with pytest.raises(ValueError, match="除去できない工程"):
        ablation_configs(settings, None, ["query_expansion"])


async def test_stage_switch_reaches_production_pipeline_settings(monkeypatch: MonkeyPatch) -> None:
    """構成の上書きは、評価の runner が作る本番の RagPipeline の設定に届く。"""
    observed: list[Settings] = []

    class CapturingRagPipeline:
        def __init__(self, settings: Settings) -> None:
            observed.append(settings)

        async def run(
            self,
            request: SearchRequest,
            trace_id: str | None = None,
            progress_callback: SearchStageProgressCallback | None = None,
        ) -> SearchResponse:
            return SearchResponse(
                query=request.query, answer="", results=[], trace_id=trace_id or "t", elapsed_ms=1.0
            )

    monkeypatch.setattr("app.rag.evaluation.RagPipeline", CapturingRagPipeline)
    settings = Settings(
        rag_answer_timeout_seconds=30.0,
        rag_rerank_enabled=True,
        rag_neighbor_child_count=2,
        rag_reference_expansion_enabled=True,
        rag_entity_expansion_enabled=True,
        rag_graph_expansion_enabled=True,
        rag_section_expansion_enabled=True,
    )
    configs = ablation_configs(
        settings,
        None,
        [
            "rerank",
            "neighbor_children",
            "reference_expansion",
            "entity_expansion",
            "graph_expansion",
            "section_expansion",
        ],
    )
    cases = [EvaluationCase(id="c1", query="質問")]
    for config in configs:
        await EvaluationRunner(settings=settings).run(
            cases=cases, top_k=3, rag_overrides=config.overrides
        )

    full, *ablated = observed
    assert full.rag_rerank_enabled and full.rag_section_expansion_enabled
    assert [
        ablated[0].rag_rerank_enabled,
        ablated[1].rag_neighbor_child_count,
        ablated[2].rag_reference_expansion_enabled,
        ablated[3].rag_entity_expansion_enabled,
        ablated[4].rag_graph_expansion_enabled,
        ablated[5].rag_section_expansion_enabled,
    ] == [False, 0, False, False, False, False]
    # 無効にした工程のほかは full と同じ。
    assert ablated[0].rag_section_expansion_enabled is True


def _case(case_id: str, *, mrr: float, rerank: bool) -> EvaluationCaseResult:
    steps = [{"name": "文書検索", "status": "complete", "elapsed_seconds": 1.0, "llm_calls": 0}]
    stages = [{"stage": "candidate_fusion", "selected_ids": ["doc-1:cs:0", "doc-9:cs:1"]}]
    if rerank:
        steps.append(
            {"name": "Rerank", "status": "complete", "elapsed_seconds": 0.5, "llm_calls": 0}
        )
        stages.append({"stage": "rerank_output", "selected_ids": ["doc-1:cs:0"]})
    stages.append({"stage": "answer_context", "selected_ids": ["doc-1:cs:0"] if mrr else []})
    return EvaluationCaseResult.model_validate(
        {
            "case_id": case_id,
            "trace_id": f"t-{case_id}",
            "elapsed_ms": 1500.0 if rerank else 1000.0,
            "relevant_document_ids": ["doc-1"],
            "mrr": mrr,
            "diagnostics": {"answer": {"execution_steps": steps, "retrieval_stages": stages}},
        }
    )


def _run(
    name: str, stage: str | None, cases: list[EvaluationCaseResult], calls: list[dict[str, Any]]
) -> AblationRun:
    manifest = {"evaluation_set": {"sha256": "set-1"}, "comparable": True, "as_of": "2026-10-31"}
    if stage is not None:
        manifest["request"] = {"rag_overrides": {"rerank_enabled": False}}
    metrics = EvaluationMetrics(
        case_count=len(cases), passed=True, case_results=cases, run_manifest=manifest
    )
    return AblationRun(
        AblationConfig(name, stage, None),
        metrics,
        calls,
        wall_seconds=10.0 if stage is None else 7.0,
    )


def _llm_call(input_tokens: int, *, stream: bool = False) -> dict[str, Any]:
    usage = {"input_tokens": input_tokens, "output_tokens": 10}
    body = (
        f"event: response.completed\ndata: {json.dumps({'response': {'usage': usage}})}\n\n"
        if stream
        else json.dumps({"usage": usage})
    )
    return {"kind": "http", "key": "k", "result": {"status": 200, "body": body}}


def test_report_shows_quality_evidence_time_and_calls() -> None:
    full_cases = [_case(f"c{i}", mrr=1.0, rerank=True) for i in range(12)]
    ablated_cases = [_case(f"c{i}", mrr=0.0 if i < 3 else 1.0, rerank=False) for i in range(12)]
    rerank_call = {"kind": "rerank", "key": "r", "result": {"output": {}}}
    failed_call = {"kind": "rerank", "key": "r", "error": {"type": "TimeoutError"}}
    full = _run(
        FULL,
        None,
        full_cases,
        [_llm_call(100), _llm_call(50, stream=True), rerank_call, failed_call],
    )
    ablated = _run("no-rerank", "rerank", ablated_cases, [_llm_call(80)])

    report = ablation_report([full, ablated], [], mode="live", seed=1, resamples=200, min_cases=10)

    stage = report["stages"][0]
    assert stage["status"] == "compared"
    assert stage["observed_effect"] is True
    assert stage["quality"]["mrr"]["regressed_case_ids"] == ["c0", "c1", "c2"]
    assert stage["quality"]["mrr"]["mean_difference"] == pytest.approx(-0.25)
    assert stage["changed_steps"] == {"Rerank": {"full": 12, "ablated": 0}}
    # 根拠の位置: rerank の後の記録は無効にした構成には無い（None。0 にしない）。
    assert stage["evidence_recall"]["candidate_fusion"]["difference"] == 0.0
    assert stage["evidence_recall"]["rerank_output"] == {
        "full": 1.0,
        "ablated": None,
        "difference": None,
    }
    assert stage["evidence_recall"]["answer_context"]["difference"] == pytest.approx(-0.25)
    assert stage["calls"]["rerank"] == {"full": 2, "ablated": 0, "difference": -2}
    assert stage["calls"]["failed"]["full"] == 1
    assert stage["tokens"]["input"] == {"full": 150, "ablated": 80, "difference": -70}
    assert stage["wall_seconds"]["difference"] == pytest.approx(-3.0)
    assert stage["case_median_ms"]["difference"] == pytest.approx(-500.0)
    assert report["order"] == ["full", "no-rerank"]
    assert "query_expansion" in report["unsupported"]


def test_report_marks_no_effect_skip_and_unexpected_conditions() -> None:
    cases = [_case(f"c{i}", mrr=1.0, rerank=True) for i in range(12)]
    full = _run(FULL, None, cases, [])
    same = _run("no-graph_expansion", "graph_expansion", cases, [])
    drifted = _run("no-entity_expansion", "entity_expansion", cases, [])
    assert drifted.metrics.run_manifest is not None
    drifted.metrics.run_manifest["corpus"] = {"sha256": "other"}
    skipped = AblationConfig(
        "no-section_expansion", "section_expansion", None, skipped_reason="already_off"
    )

    report = ablation_report(
        [full, same, drifted], [skipped], mode="replay", seed=1, resamples=100, min_cases=10
    )

    by_stage = {item["stage"]: item for item in report["stages"]}
    assert by_stage["graph_expansion"]["observed_effect"] is False
    assert by_stage["entity_expansion"]["status"] == "not_comparable"
    assert by_stage["entity_expansion"]["reasons"] == ["condition_differs:corpus.sha256"]
    assert by_stage["section_expansion"]["status"] == "already_off"
    assert report["timing"].startswith("replay")
    assert full.calls == [] and report["runs"]["full"]["tokens"] is None


@pytest.fixture(autouse=True)
def _restore_settings(monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    names = [
        "rag_evaluation_concurrency",
        "rag_rerank_enabled",
        "rag_section_expansion_enabled",
    ] + [
        f"enterprise_ai_{slot}{field}"
        for slot in ("", "secondary_", "tertiary_")
        for field in ("api_key", "endpoint")
    ]
    for name in names:
        monkeypatch.setattr(settings, name, getattr(settings, name))


def test_cli_records_and_replays_ablation(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from app.api.routes import evaluation as evaluation_route

    golden = tmp_path / "golden.json"
    golden.write_text(
        json.dumps({"cases": [{"id": f"c{i}", "query": f"q{i}"} for i in range(12)]}),
        encoding="utf-8",
    )
    settings = get_settings()
    monkeypatch.setattr(settings, "rag_rerank_enabled", True)
    monkeypatch.setattr(settings, "rag_section_expansion_enabled", False)
    monkeypatch.setattr(config_module, "local_today", lambda settings: date(2026, 10, 31))
    live = {"on": True}

    async def execute_run(
        request: EvaluationRunRequest, *, time_budget_seconds: float
    ) -> tuple[EvaluationMetrics, None]:
        del time_budget_seconds
        rerank = not (request.rag_overrides and request.rag_overrides.rerank_enabled is False)

        def call(kind: str, case: str) -> Any:
            def respond() -> Any:
                assert live["on"], "再生で実の API を呼びました"
                return {"ok": True}

            return external_call(kind, {"case": case}, respond)

        results = []
        for case in request.cases:
            call("embeddings", case.id)
            if rerank:
                call("rerank", case.id)
            results.append(
                _case(case.id, mrr=1.0 if rerank or case.id != "c0" else 0.0, rerank=rerank)
            )
        manifest = {
            "as_of": config_module.settings_today().isoformat(),
            "evaluation_set": {"sha256": "set-1"},
            "comparable": True,
            "request": {
                "rag_overrides": request.rag_overrides.model_dump(exclude_none=True)
                if request.rag_overrides
                else {}
            },
        }
        return EvaluationMetrics(
            case_count=len(results), passed=True, case_results=results, run_manifest=manifest
        ), None

    monkeypatch.setattr(evaluation_route, "_execute_run", execute_run)
    out = tmp_path / "ablation"
    args = ["ablate", str(golden), "--stages", "rerank,section_expansion", "--output-dir", str(out)]

    assert evaluation_run_cli.main([*args, "--record"]) == 0
    report = json.loads((out / "ablation.json").read_text(encoding="utf-8"))
    stage = {item["stage"]: item for item in report["stages"]}
    assert stage["rerank"]["calls"]["rerank"] == {"full": 12, "ablated": 0, "difference": -12}
    assert stage["rerank"]["quality"]["mrr"]["regressed_case_ids"] == ["c0"]
    assert stage["section_expansion"]["status"] == "already_off"
    assert (out / "full.recording.json").exists() and (out / "no-rerank.recording.json").exists()
    printed = capsys.readouterr().out
    assert "rerank を除く" in printed
    assert "未対応" in printed

    # 別の日・鍵なしで、記録から同じ除去実験を再生する。
    live["on"] = False
    monkeypatch.setattr(config_module, "local_today", lambda settings: date(2026, 11, 5))
    assert evaluation_run_cli.main([*args, "--replay"]) == 0
    replayed = json.loads((out / "ablation.replay.json").read_text(encoding="utf-8"))
    assert json.loads((out / "ablation.json").read_text(encoding="utf-8"))["mode"] == "live"
    assert replayed["mode"] == "replay"
    assert replayed["as_of"] == "2026-10-31"
    assert replayed["stages"][0]["quality"] == report["stages"][0]["quality"]
