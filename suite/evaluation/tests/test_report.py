"""Report: suite の結果の比較の要約（#3240・#3417）。

比較は suite の結果どうし（RAG の Target と業務 Agent の Target を同じ Evaluator で採点したもの）
だけを並べる。違う評価セットの結果と、製品の評価の結果（RAG の評価の job の結果）は比べない。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from baseline_support import CASE_FIXTURES, SUMMARIES, replay

from pr_suite_evaluation.cli import main
from pr_suite_evaluation.report import (
    FORBIDDEN_ACTION_REASON,
    summarize_result,
    summary_markdown,
)


def test_summary_of_a_suite_report_shows_reasoning_and_split_rows() -> None:
    suite = next(item for item in SUMMARIES if item["id"] == "completed")
    fixtures = [CASE_FIXTURES[fixture_id] for fixture_id in suite["case_fixtures"]]
    report = replay(fixtures, suite.get("thresholds")).record.report
    summary = summarize_result(report.model_dump(mode="json"))
    assert summary["reasoning_types"]
    assert summary["hops"]
    assert summary["unevaluable"]["response_decision_accuracy"] == 5
    table = summary_markdown({"D": summary})
    assert "| 評価不能のケース: 回答・確認・保留の判断の正しさ | 5 |" in table
    assert any(line.startswith("| dev / ") for line in table.splitlines())


def test_summary_compares_results_in_a_markdown_table() -> None:
    data: dict[str, Any] = {
        "case_count": 2,
        "error_count": 0,
        "response_decision_accuracy": 0.5,
        "claim_support_rate": 0.5,
        "case_results": [
            {"case_id": "a", "status": "success", "elapsed_ms": 1000.0, "failure_reasons": []},
            {
                "case_id": "b",
                "status": "success",
                "elapsed_ms": 3000.0,
                "failure_reasons": [FORBIDDEN_ACTION_REASON],
            },
            # 失敗したケースは危険な回答に数えない。
            {
                "case_id": "c",
                "status": "error",
                "elapsed_ms": 500.0,
                "failure_reasons": ["case_error", FORBIDDEN_ACTION_REASON],
            },
        ],
        "split_breakdown": {
            "holdout": {
                "case_count": 1,
                "error_count": 0,
                "metrics": {"response_decision_accuracy": 0.0},
            }
        },
        "category_breakdown": {
            "clarification_required": {
                "case_count": 2,
                "error_count": 0,
                "metrics": {"response_decision_accuracy": 0.5},
            }
        },
        "denominators": {"metrics": {"response_decision_accuracy": {"unevaluable": 3}}},
    }
    summary = summarize_result(data)
    assert summary["median_elapsed_seconds"] == 2.0
    assert summary["dangerous_case_count"] == 1
    assert summary["dangerous_case_ids"] == ["b"]
    assert summary["failed_case_ids"] == ["c"]
    assert summary["categories"]["clarification_required"]["handling_correct_rate"] == 0.5
    table = summary_markdown({"A": summary, "D": summary})
    assert "| 指標 | A | D |" in table
    assert "| 回答・確認・保留の判断の正しさ | 0.50 | 0.50 |" in table
    assert "| 評価不能のケース: 回答・確認・保留の判断の正しさ | 3 | 3 |" in table
    assert "| 危険な回答のケース | 1 | 1 |" in table
    assert "| holdout: 回答・確認・保留の判断の正しさ | 0.00 | 0.00 |" in table
    assert "| clarification_required: 回答・確認・保留の判断の正しさ | 0.50 | 0.50 |" in table
    assert "| 1 問の時間の中央値（秒） | 2.00 | 2.00 |" in table


def test_dangerous_cases_are_unmeasured_without_forbidden_action() -> None:
    data = {
        "case_count": 1,
        "error_count": 0,
        "case_results": [
            {"case_id": "a", "status": "success", "elapsed_ms": 1000.0, "failure_reasons": []}
        ],
    }
    summary = summarize_result(data)
    assert summary["dangerous_case_count"] is None
    assert "| 危険な回答のケース | - |" in summary_markdown({"D": summary})


def _write_record(tmp_path: Path, name: str, fixture_ids: list[str]) -> Path:
    record = replay([CASE_FIXTURES[fixture_id] for fixture_id in fixture_ids]).record
    path = tmp_path / name
    path.write_text(record.model_dump_json(), encoding="utf-8")
    return path


def test_compare_reads_suite_records_and_shows_the_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ids = ["answered-agent-outcome", "clarification-skips-retrieval"]
    first = _write_record(tmp_path, "d1.json", ids)
    second = _write_record(tmp_path, "d2.json", ids)
    assert main(["compare", f"D={first}", f"E={second}"]) == 0
    out = capsys.readouterr().out
    assert "| 対象 | agent_mcp | agent_mcp |" in out
    assert "| 採点の版 | six_metrics_v1+agent_v3 | six_metrics_v1+agent_v3 |" in out


def test_compare_rejects_other_datasets_and_product_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _write_record(tmp_path, "d1.json", ["answered-agent-outcome"])
    other = _write_record(tmp_path, "d2.json", ["no-expectations"])
    assert main(["compare", f"D={first}", f"E={other}"]) == 2
    assert "評価セットの内容" in capsys.readouterr().err
    # 製品の評価の結果（RAG の評価の job の data）は suite の結果ではないので読まない。
    product = tmp_path / "product.json"
    product.write_text('{"data": {"case_count": 1}}', encoding="utf-8")
    assert main(["compare", f"A={product}"]) == 2
    assert "suite の結果のファイルの形ではありません" in capsys.readouterr().err
