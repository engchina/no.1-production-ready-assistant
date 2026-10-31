"""Target: 公開の RAG MCP（`rag_search`）の adapter（#3417）。

RAG の MCP（`POST /api/mcp`）は MockTransport の台本で、出力は契約
（`platform/contracts/mcp/rag-tools.json`）の形の合成の値。送る引数は契約の inputSchema で確かめる。
業務 Agent の Target と同じ Evaluator・Report で採点し、同じ評価セットの A / C / D を `compare` で
並べられることを確かめる。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from baseline_support import CASE_FIXTURES, RAG_TOOLS, baseline_dataset, check_arguments, replay

from pr_suite_evaluation.cli import DEFAULT_API_KEY_ENV, main
from pr_suite_evaluation.dataset import Dataset, LoadedDataset
from pr_suite_evaluation.experiment import ExperimentRecord, ScorerConfig, run_experiment
from pr_suite_evaluation.mcp_client import MCP_PROTOCOL_VERSION, McpClient
from pr_suite_evaluation.targets.rag_mcp import (
    DEFAULT_EVIDENCE_LIMIT,
    RAG_SEARCH_SCHEMA_VERSION,
    RagEvaluationError,
    RagMcpTarget,
    RagTargetConfig,
    case_requests,
)

API_KEY = "prak_synthetic"
URL = "http://rag/api/mcp"


def _evidence(document_id: str, chunk_id: str, excerpt: str, **fields: Any) -> dict[str, Any]:
    """`rag_search` の `evidence[]` の 1 件（契約の RagEvidence の形の合成の値）。"""
    return {
        "evidence_id": chunk_id,
        "document_id": document_id,
        "chunk_id": chunk_id,
        "file_name": "manual.pdf",
        "chunk_set_id": None,
        "recipe_id": None,
        "content_kind": "text",
        "evidence_type": "text",
        "image_ref": None,
        "locator": {
            "section_path": ["2. 手順"],
            "page_start": 3,
            "page_end": 3,
            "sheet_name": None,
            "row_start": None,
            "row_end": None,
            "cell_range": None,
            "page_label_start": None,
            "page_label_end": None,
            "bbox": None,
            "bbox_unit": None,
            "element_locator": None,
            "table_row": None,
            "table_cells": [],
        },
        "excerpt": excerpt,
        "truncated": False,
        "text_omitted": False,
        "text_length": len(excerpt),
        "excerpt_range": None,
        "header_range": None,
        "next_offset": None,
        "used_in_answer": True,
        "answer_use": "confirmed",
        "role": "retrieved_anchor",
        "score": None,
        "rerank_score": None,
        "retrieval_rank": 1,
        "origin": "source",
        "superseded": False,
        "references": [],
        "reference_from": None,
        "reference_label": None,
        "section": None,
        **fields,
    }


def _output(
    trace_id: str,
    *,
    outcome: str = "answered",
    evidence: list[dict[str, Any]] | None = None,
    clarifications: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """`rag_search` の出力（契約の required と、採点が読む項目）。"""
    items = evidence or []
    return {
        "schema_version": RAG_SEARCH_SCHEMA_VERSION,
        "answer": "合成の回答です。",
        "trace_id": trace_id,
        "guardrail_warnings": [],
        "outcome": outcome,
        "requests": [],
        "conditions": [],
        "gaps": [],
        "confirmations": [],
        "clarifications": clarifications or [],
        "evidence": items,
        "evidence_omitted": 0,
        "evidence_text_omitted": sum(1 for item in items if item["text_omitted"]),
        "older_versions": [],
    }


class FakeRagMcp:
    """台本の RAG の MCP。`rag_search` の呼び出しごとに台本の次の出力を返す。"""

    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self.outputs = list(outputs)
        self.calls: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []
        self.overrides: dict[str, Any] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.headers.append(request.headers)
        message = json.loads(request.content)
        if message["method"] == "initialize":
            return self._reply(message, {"protocolVersion": MCP_PROTOCOL_VERSION})
        assert message["params"]["name"] == "rag_search"
        arguments = message["params"]["arguments"]
        check_arguments("rag_search", arguments, RAG_TOOLS)
        self.calls.append(arguments)
        if self.overrides is not None:
            return self._reply(message, self.overrides)
        content = self.outputs.pop(0)
        return self._reply(
            message,
            {
                "content": [{"type": "text", "text": json.dumps(content, ensure_ascii=False)}],
                "structuredContent": content,
                "isError": False,
            },
        )

    @staticmethod
    def _reply(message: dict[str, Any], result: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})


def _mcp(fake: FakeRagMcp) -> McpClient:
    return McpClient(
        httpx.Client(transport=httpx.MockTransport(fake)),
        URL,
        api_key=API_KEY,
        label="RAG",
        error_class=RagEvaluationError,
    )


def _dataset(
    cases: list[dict[str, Any]], thresholds: dict[str, float] | None = None
) -> LoadedDataset:
    return LoadedDataset(
        dataset=Dataset.model_validate({"cases": cases, "thresholds": thresholds}),
        name="synthetic",
        sha256="0" * 64,
    )


def _evaluate(
    fake: FakeRagMcp, cases: list[dict[str, Any]], config: RagTargetConfig | None = None
) -> ExperimentRecord:
    config = config or RagTargetConfig(mcp_url=URL, knowledge_base_ids=("kb-1",))
    ticks = iter(float(value) for value in range(10_000))
    target = RagMcpTarget(_mcp(fake), config, clock=lambda: next(ticks))
    return run_experiment(
        _dataset(cases),
        target,
        target_config=config.provenance(),
        scorer=ScorerConfig(target_kind="rag_mcp"),
        log=lambda _: None,
    )


CASE: dict[str, Any] = {
    "id": "grant",
    "query": "権限の付け方は？",
    "category": "clarification_required",
    "split": "dev",
    "conditions": {"mode": "標準"},
    "relevant_document_ids": ["doc-manual"],
    "expected_outcomes": ["needs_clarification"],
    "turns": [
        {
            "reply": "個別の利用者です。",
            "conditions": {"target": "個別"},
            "expected_outcomes": ["answered"],
        }
    ],
    "required_evidence": [
        {"id": "e1", "document_id": "doc-manual", "page_start": 3, "text": "個別の利用者に付与"}
    ],
}


def test_output_version_is_the_public_contract_version() -> None:
    schema = RAG_TOOLS["rag_search"]["outputSchema"]["properties"]["schema_version"]
    assert schema["default"] == RAG_SEARCH_SCHEMA_VERSION
    assert RAG_TOOLS["rag_search"]["inputSchema"]["properties"]["evidence_limit"]["maximum"] == (
        DEFAULT_EVIDENCE_LIMIT
    )


def test_multi_turn_case_sends_conditions_and_scores_with_the_same_evaluator() -> None:
    clarify = _output(
        "trace-1",
        outcome="needs_clarification",
        clarifications=[
            {"condition_id": "target", "label": "付与先", "question": "付与先は？", "options": []}
        ],
    )
    answered = _output(
        "trace-2",
        evidence=[
            _evidence("doc-other", "c9", "別の本文"),
            _evidence("doc-manual", "c1", "個別の利用者に付与する手順"),
        ],
    )
    fake = FakeRagMcp([clarify, answered])
    record = _evaluate(fake, [CASE])
    assert fake.calls == [
        {
            "query": "権限の付け方は？",
            "evidence_limit": 50,
            "knowledge_base_ids": ["kb-1"],
            "conditions": {"mode": "標準"},
        },
        {
            "query": "権限の付け方は？\n個別の利用者です。",
            "original_question": "権限の付け方は？\n個別の利用者です。",
            "evidence_limit": 50,
            "knowledge_base_ids": ["kb-1"],
            "conditions": {"mode": "標準", "target": "個別"},
        },
    ]
    assert {headers["authorization"] for headers in fake.headers} == {f"Bearer {API_KEY}"}
    result = record.report.case_results[0]
    assert result.status == "success"
    assert [(turn.observed_outcome, turn.outcome_source) for turn in result.turn_results] == [
        ("needs_clarification", "rag_answer"),
        ("answered", "rag_answer"),
    ]
    assert result.response_decision_accuracy == 1.0
    assert result.retrieved_document_ids == ["doc-other", "doc-manual"]
    assert result.mrr == 0.5
    assert result.required_evidence_recall == 1.0
    assert record.report.metrics_version == "six_metrics_v1+rag_v1"
    assert record.provenance["target"]["kind"] == "rag_mcp"
    assert record.target["runs"][0]["clarification_conditions"] == ["target"]
    assert record.observations[0].turns[1].payload["rag_search"]["trace_id"] == "trace-2"


def test_profile_and_superseded_cases_are_passed_to_rag_search() -> None:
    fake = FakeRagMcp([_output("trace-1")])
    config = RagTargetConfig(mcp_url=URL, search_answer_profile_id="profile-1", top_k=20)
    case = {"id": "v", "query": "旧版との違いは？", "include_superseded": True}
    _evaluate(fake, [case], config)
    assert fake.calls == [
        {
            "query": "旧版との違いは？",
            "evidence_limit": 50,
            "search_answer_profile_id": "profile-1",
            "top_k": 20,
            "include_superseded": True,
        }
    ]


def test_target_needs_a_knowledge_base_or_a_profile() -> None:
    with pytest.raises(ValueError, match="knowledge-base-id"):
        RagTargetConfig(mcp_url=URL)
    with pytest.raises(ValueError, match="evidence_limit"):
        RagTargetConfig(mcp_url=URL, knowledge_base_ids=("kb",), evidence_limit=99)


def test_omitted_evidence_text_is_unevaluable() -> None:
    omitted = _evidence("doc-manual", "c1", "", text_omitted=True, text_length=90_000)
    fake = FakeRagMcp([_output("trace-1", evidence=[omitted])])
    case = {
        "id": "a",
        "query": "q",
        "relevant_document_ids": ["doc-manual"],
        "required_evidence": [{"id": "e1", "document_id": "doc-manual", "text": "本文"}],
    }
    result = _evaluate(fake, [case]).report.case_results[0]
    assert result.required_evidence_recall is None
    assert result.metric_status["required_evidence_recall"].reason == "evidence_text_omitted"
    assert result.mrr == 1.0


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (
            {
                "content": [],
                "structuredContent": {"error_code": "PROFILE_NOT_FOUND", "message": "無い"},
                "isError": True,
            },
            "PROFILE_NOT_FOUND",
        ),
        (
            {"content": [], "structuredContent": {"schema_version": 99}, "isError": False},
            "schema_version=99",
        ),
    ],
    ids=["tool-error", "unknown-schema-version"],
)
def test_tool_errors_are_case_errors(override: dict[str, Any], expected: str) -> None:
    fake = FakeRagMcp([])
    fake.overrides = override
    record = _evaluate(fake, [{"id": "a", "query": "q", "expected_outcomes": ["answered"]}])
    result = record.report.case_results[0]
    assert (result.status, result.error_type, result.error_stage) == (
        "error",
        "RagEvaluationError",
        "turn_0",
    )
    assert result.error_message is not None and expected in result.error_message
    assert record.report.denominators.final_statuses == {"target_error": 1}


def test_case_requests_join_replies_and_accumulate_conditions() -> None:
    case = Dataset.model_validate({"cases": [CASE]}).cases[0]
    assert case_requests(case) == [
        ("権限の付け方は？", {"mode": "標準"}),
        ("権限の付け方は？\n個別の利用者です。", {"mode": "標準", "target": "個別"}),
    ]


def _golden(tmp_path: Path) -> Path:
    path = tmp_path / "set.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "a",
                        "query": "q",
                        "relevant_document_ids": ["file:m.pdf"],
                        "expected_outcomes": ["answered"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return path


def test_main_runs_a_and_c_and_compares_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A（ナレッジベース）と C（プロファイル）を同じ評価セットで流し、同じ採点器の結果を並べる。"""
    outputs = [
        _output("trace-a", evidence=[_evidence("doc-m", "c1", "本文")]),
        _output("trace-c", outcome="conditional", evidence=[_evidence("doc-m", "c1", "本文")]),
    ]
    fake = FakeRagMcp(outputs)
    real_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        kwargs.pop("trust_env", None)
        return real_client(transport=httpx.MockTransport(fake), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, API_KEY)
    document_map = tmp_path / "map.json"
    document_map.write_text(json.dumps({"documents": {"m.pdf": "doc-m"}}), encoding="utf-8")
    common = [str(_golden(tmp_path)), "--document-map", str(document_map), "--target", "rag-mcp"]
    a = tmp_path / "a.json"
    c = tmp_path / "c.json"
    assert (
        main(["run", *common, "--mcp-url", URL, "--knowledge-base-id", "kb-1", "--output", str(a)])
        == 0
    )
    assert (
        main(
            [
                "run",
                *common,
                "--mcp-url",
                URL,
                "--search-answer-profile-id",
                "p-1",
                "--output",
                str(c),
            ]
        )
        == 0
    )
    assert fake.calls[0]["knowledge_base_ids"] == ["kb-1"]
    assert fake.calls[1]["search_answer_profile_id"] == "p-1"
    assert API_KEY not in a.read_text(encoding="utf-8")
    capsys.readouterr()
    assert main(["compare", f"A={a}", f"C={c}"]) == 0
    out = capsys.readouterr().out
    assert "| 対象 | rag_mcp | rag_mcp |" in out
    assert "| 正解文書の順位（MRR） | 1.00 | 1.00 |" in out
    # 保存した観測を採点し直す（RAG を呼ばない）。
    calls = len(fake.calls)
    rescored = tmp_path / "a.rescored.json"
    args = [
        "score",
        str(a),
        "--dataset",
        str(_golden(tmp_path)),
        "--document-map",
        str(document_map),
    ]
    assert main([*args, "--output", str(rescored)]) == 0
    assert len(fake.calls) == calls
    record = ExperimentRecord.model_validate_json(rescored.read_text(encoding="utf-8"))
    assert record.report.metrics_version == "six_metrics_v1+rag_v1"


def test_main_requires_a_knowledge_base_or_a_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(DEFAULT_API_KEY_ENV, API_KEY)
    document_map = tmp_path / "map.json"
    document_map.write_text(json.dumps({"documents": {"m.pdf": "doc-m"}}), encoding="utf-8")
    args = [
        "run",
        str(_golden(tmp_path)),
        "--document-map",
        str(document_map),
        "--target",
        "rag-mcp",
    ]
    assert main([*args, "--output", str(tmp_path / "o.json")]) == 2
    assert "--knowledge-base-id" in capsys.readouterr().err


def test_rag_and_agent_results_of_the_same_dataset_are_compared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """同じ評価セットの RAG（A）と業務 Agent（D）を、同じ Evaluator の結果として並べる。"""
    fixture = CASE_FIXTURES["answered-agent-outcome"]
    dataset = baseline_dataset([fixture])
    relevant = fixture["case"]["relevant_document_ids"][0]
    fake = FakeRagMcp([_output("trace-a", evidence=[_evidence(relevant, "c1", "本文")])])
    config = RagTargetConfig(mcp_url=URL, knowledge_base_ids=("kb-1",))
    rag = run_experiment(
        dataset,
        RagMcpTarget(_mcp(fake), config),
        target_config=config.provenance(),
        scorer=ScorerConfig(target_kind="rag_mcp"),
        log=lambda _: None,
    )
    agent = replay([fixture]).record
    assert rag.provenance["dataset"]["sha256"] == agent.provenance["dataset"]["sha256"]
    a = tmp_path / "a.json"
    d = tmp_path / "d.json"
    a.write_text(rag.model_dump_json(), encoding="utf-8")
    d.write_text(agent.model_dump_json(), encoding="utf-8")
    assert main(["compare", f"A={a}", f"D={d}"]) == 0
    out = capsys.readouterr().out
    assert "| 対象 | rag_mcp | agent_mcp |" in out
    assert "| 採点の版 | six_metrics_v1+rag_v1 | six_metrics_v1+agent_v3 |" in out
    with pytest.raises(ValueError, match="採点の設定"):
        run_experiment(
            dataset,
            RagMcpTarget(_mcp(fake), config),
            target_config=config.provenance(),
            scorer=ScorerConfig(),
            log=lambda _: None,
        )
