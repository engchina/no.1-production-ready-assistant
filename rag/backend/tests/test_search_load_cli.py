"""search load / p95 gate CLI のテスト。"""

import asyncio
import json
from pathlib import Path
from typing import Any

from pytest import CaptureFixture, MonkeyPatch

from app.rag import search_load_cli


def test_search_load_cli_passes_and_writes_redacted_artifacts(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """search load が成功すると p95 集計を保存し、query 原文は出力しない。"""
    scenario = _write_scenario(tmp_path)
    output = tmp_path / "reports" / "search-load.json"
    trend_output = tmp_path / "reports" / "search-load-trend.json"
    observed_payloads: list[dict[str, Any]] = []

    async def fake_post(
        client: object,
        *,
        api_url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        _ = client
        observed_payloads.append({"api_url": api_url, "payload": payload, "headers": headers})
        elapsed_ms = 100.0 + (len(observed_payloads) * 10)
        return {
            "data": {
                "answer": "回答本文",
                "trace_id": f"trace-{len(observed_payloads)}",
                "elapsed_ms": elapsed_ms,
                "citations": [],
                "guardrail_warnings": [],
                "diagnostics": {
                    "answer": {
                        "execution_steps": [
                            {"name": "質問の理解", "elapsed_seconds": 0.012, "llm_calls": 0},
                            {"name": "文書検索", "elapsed_seconds": 0.024},
                            # 同じ工程が 2 回あれば合計する(補正検索の 2 回目など)。
                            {"name": "文書検索", "elapsed_seconds": 0.006},
                            {"name": "回答生成", "elapsed_seconds": 0.046, "llm_calls": 2},
                            {"name": "根拠確認", "elapsed_seconds": 0.01, "llm_calls": 1},
                            {"name": "根拠確認", "elapsed_seconds": 0.01, "llm_calls": 1},
                        ]
                    }
                },
            }
        }

    monkeypatch.setattr(search_load_cli, "_post_search_request", fake_post)

    exit_code = search_load_cli.main(
        [
            str(scenario),
            "--api-url",
            "http://rag.example.test/api/search",
            "--output",
            str(output),
            "--trend-output",
            str(trend_output),
            "--tenant-id",
            "tenant-secret",
            "--user-id",
            "user-secret",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert "検索 load gate passed" in captured.err
    assert "tenant-secret" not in captured.err
    assert "user-secret" not in captured.err
    assert len(observed_payloads) == 4
    assert observed_payloads[0]["api_url"] == "http://rag.example.test/api/search"
    assert observed_payloads[0]["headers"] == {
        "X-Tenant-ID": "tenant-secret",
        "X-User-ID": "user-secret",
    }
    assert observed_payloads[0]["payload"]["query"] == "秘密の承認条件を教えてください。"

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["data"]["passed"] is True
    assert result["data"]["request_count"] == 4
    assert result["data"]["client_latency_ms"]["count"] == 4
    assert result["data"]["server_latency_ms"]["p95"] == 140.0
    assert result["data"]["stage_latency_ms"]["質問の理解"]["p95"] == 12.0
    assert result["data"]["stage_latency_ms"]["文書検索"]["p95"] == 30.0
    # 工程ごとのモデルの呼び出しの回数(#1226)。同じ工程は合計し、回数の無い工程は出さない。
    assert result["data"]["stage_llm_calls"] == {
        "回答生成": {"mean": 2.0, "max": 2.0},
        "根拠確認": {"mean": 2.0, "max": 2.0},
        "質問の理解": {"mean": 0.0, "max": 0.0},
    }
    output_text = output.read_text(encoding="utf-8")
    trend_text = trend_output.read_text(encoding="utf-8")
    assert "秘密の承認条件" not in output_text
    assert "秘密の承認条件" not in trend_text
    assert "回答本文" not in output_text
    assert "result_sha256" in trend_text


def test_search_load_cli_returns_one_when_threshold_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """p95 / error rate 閾値を超える場合は CI 用 exit 1 にする。"""
    scenario = _write_scenario(
        tmp_path,
        thresholds={
            "client_p95_ms": 1.0,
            "server_p95_ms": 1.0,
            "error_rate": 0.0,
            "stage_p95_ms": {"文書検索": 1.0},
        },
    )

    async def fake_post(
        client: object,
        *,
        api_url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        _ = client, api_url, payload, headers
        await asyncio.sleep(0.002)
        return {
            "data": {
                "trace_id": "trace-slow",
                "elapsed_ms": 200.0,
                "diagnostics": {
                    "answer": {"execution_steps": [{"name": "文書検索", "elapsed_seconds": 0.08}]}
                },
            }
        }

    monkeypatch.setattr(search_load_cli, "_post_search_request", fake_post)

    exit_code = search_load_cli.main([str(scenario)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "検索 load gate failed" in captured.err
    result = json.loads(captured.out)
    failure_metrics = {failure["metric"] for failure in result["data"]["threshold_failures"]}
    assert "client_p95_ms" in failure_metrics
    assert "server_p95_ms" in failure_metrics
    assert "stage_p95_ms.文書検索" in failure_metrics


def test_search_load_cli_counts_http_errors_without_query_leakage(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """HTTP error は低機密 error_type として集計し、query 原文を出さない。"""
    scenario = _write_scenario(tmp_path)
    output = tmp_path / "reports" / "search-load-error.json"

    async def fake_post(
        client: object,
        *,
        api_url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        _ = client, api_url, payload, headers
        raise search_load_cli.SearchLoadRunError("http_status", status_code=503)

    monkeypatch.setattr(search_load_cli, "_post_search_request", fake_post)

    exit_code = search_load_cli.main([str(scenario), "--output", str(output)])

    assert exit_code == 1
    result_text = output.read_text(encoding="utf-8")
    result = json.loads(result_text)
    assert result["data"]["error_count"] == 4
    assert result["data"]["error_type_counts"] == {"http_status:503": 4}
    assert "秘密の承認条件" not in result_text


def test_search_load_cli_rejects_invalid_scenario_without_query_leakage(
    tmp_path: Path,
    capsys: CaptureFixture[str],
) -> None:
    """scenario validation error は path/type だけを表示する。"""
    scenario = tmp_path / "invalid-search-load.json"
    scenario.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "bad",
                        "query": "秘密の承認条件を教えてください。",
                        "top_k": 0,
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    exit_code = search_load_cli.main([str(scenario)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "load scenario の形式が不正です" in captured.err
    assert "秘密の承認条件" not in captured.err


def test_search_load_cli_uses_api_base_url(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """api-base-url 指定時は /api/search を自動で付ける。"""
    scenario = _write_scenario(tmp_path)
    observed: dict[str, str] = {}

    async def fake_post(
        client: object,
        *,
        api_url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
    ) -> dict[str, Any]:
        _ = client, payload, headers
        observed["api_url"] = api_url
        return {"data": {"trace_id": "trace", "elapsed_ms": 1.0, "diagnostics": {}}}

    monkeypatch.setattr(search_load_cli, "_post_search_request", fake_post)

    exit_code = search_load_cli.main(
        [str(scenario), "--api-base-url", "https://staging.example.test/"]
    )

    assert exit_code == 0
    assert observed["api_url"] == "https://staging.example.test/api/search"


def _write_scenario(
    tmp_path: Path,
    *,
    thresholds: dict[str, Any] | None = None,
) -> Path:
    scenario = tmp_path / "search-load.json"
    scenario.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "approval",
                        "query": "秘密の承認条件を教えてください。",
                        "top_k": 5,
                        "filters": {"status": "INDEXED"},
                    },
                    {
                        "id": "troubleshooting",
                        "query": "検索できない場合の確認項目は何ですか。",
                        "top_k": 5,
                    },
                ],
                "repeat": 2,
                "concurrency": 2,
                "thresholds": thresholds or {"server_p95_ms": 5000.0},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return scenario


def test_case_can_target_a_search_answer_profile() -> None:
    """負荷・基線の計測は検索・回答プロファイルを指定して実際の設定で測れる（#1226）。"""
    case = search_load_cli.SearchLoadCase(
        id="c", query="承認者は？", search_answer_profile_id="bv-1"
    )
    payload = case.to_search_request().model_dump(mode="json")
    assert payload["search_answer_profile_id"] == "bv-1"
    plain = search_load_cli.SearchLoadCase(id="c", query="承認者は？").to_search_request()
    assert plain.search_answer_profile_id is None
