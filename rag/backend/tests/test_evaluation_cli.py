"""golden set 評価 gate CLI のテスト。"""

import json
from pathlib import Path
from typing import Any

from pytest import CaptureFixture, MonkeyPatch

from app.rag import evaluation_cli


def test_evaluation_gate_cli_passes_and_writes_output_file(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """評価 API が passed を返す場合は exit 0 にし、レスポンスを artifact 化する。"""
    golden_set = _write_golden_set(tmp_path)
    output = tmp_path / "reports" / "evaluation-result.json"
    observed: dict[str, Any] = {}

    def fake_post(
        *,
        api_url: str,
        payload: dict[str, Any],
        timeout: float,
        headers: dict[str, str],
        poll_interval: float,
    ) -> dict[str, Any]:
        observed.update(
            {
                "api_url": api_url,
                "payload": payload,
                "timeout": timeout,
                "headers": headers,
            }
        )
        return {"data": _metrics_payload(passed=True)}

    monkeypatch.setattr(evaluation_cli, "_post_evaluation_request", fake_post)

    exit_code = evaluation_cli.main(
        [
            str(golden_set),
            "--api-url",
            "http://rag.example.test/api/evaluation/run",
            "--timeout",
            "12.5",
            "--output",
            str(output),
            "--tenant-id",
            "tenant-secret",
            "--user-id",
            "user-secret",
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.out == ""
    assert "評価 gate passed" in captured.err
    assert "response_decision_accuracy=1.0" in captured.err
    assert "tenant-secret" not in captured.err
    assert "user-secret" not in captured.err
    assert observed["api_url"] == "http://rag.example.test/api/evaluation/run"
    assert observed["timeout"] == 12.5
    assert observed["headers"] == {
        "X-Tenant-ID": "tenant-secret",
        "X-User-ID": "user-secret",
    }
    assert observed["payload"]["filters"] == {"status": "INDEXED"}
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["data"]["passed"] is True


def test_evaluation_gate_cli_returns_one_when_gate_fails(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """passed=false / error_count / threshold failure は CI 用 exit 1 にする。"""
    golden_set = _write_golden_set(tmp_path)

    def fake_post(
        *,
        api_url: str,
        payload: dict[str, Any],
        timeout: float,
        headers: dict[str, str],
        poll_interval: float,
    ) -> dict[str, Any]:
        return {"data": _metrics_payload(passed=False, error_count=1)}

    monkeypatch.setattr(evaluation_cli, "_post_evaluation_request", fake_post)

    exit_code = evaluation_cli.main([str(golden_set), "--api-url", "http://api.test/run"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "評価 gate failed" in captured.err
    response = json.loads(captured.out)
    assert response["data"]["passed"] is False
    assert response["data"]["error_count"] == 1
    assert response["data"]["threshold_failures"]


def test_evaluation_gate_cli_writes_redacted_trend_output(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """--trend-output は nightly 用 aggregate だけを書き、golden query 原文を含めない。"""
    golden_set = _write_golden_set(tmp_path)
    trend_output = tmp_path / "reports" / "evaluation-trend.json"

    def fake_post(
        *,
        api_url: str,
        payload: dict[str, Any],
        timeout: float,
        headers: dict[str, str],
        poll_interval: float,
    ) -> dict[str, Any]:
        del api_url, payload, timeout, headers
        return {"data": _metrics_payload(passed=True)}

    monkeypatch.setattr(evaluation_cli, "_post_evaluation_request", fake_post)

    exit_code = evaluation_cli.main([str(golden_set), "--trend-output", str(trend_output)])

    assert exit_code == 0
    trend = json.loads(trend_output.read_text(encoding="utf-8"))
    assert trend["kind"] == "run"
    assert trend["passed"] is True
    assert trend["metrics"]["case_count"] == 1
    assert trend["metrics"]["answer_correctness"] == 1.0
    assert trend["metrics"]["requirement_coverage"] is None
    assert trend["metrics"]["metric_case_counts"]["required_evidence_recall"] == 1
    assert "bbox_citation_coverage" not in trend["metrics"]
    assert "case_results" not in trend["metrics"]
    trend_text = json.dumps(trend, ensure_ascii=False)
    assert "承認条件はいくらですか" not in trend_text
    assert "120000" not in trend_text


def test_evaluation_gate_cli_uses_base_url(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """api-base-url 指定時は評価 job の投入 endpoint を自動で付ける。"""
    monkeypatch.delenv("RAG_EVALUATION_API_URL", raising=False)
    monkeypatch.delenv("RAG_EVALUATION_RUN_API_URL", raising=False)
    golden_set = _write_golden_set(tmp_path)
    observed: dict[str, Any] = {}

    def fake_post(
        *,
        api_url: str,
        payload: dict[str, Any],
        timeout: float,
        headers: dict[str, str],
        poll_interval: float,
    ) -> dict[str, Any]:
        observed["api_url"] = api_url
        return {"data": _metrics_payload(passed=True)}

    monkeypatch.setattr(evaluation_cli, "_post_evaluation_request", fake_post)

    exit_code = evaluation_cli.main(
        [str(golden_set), "--api-base-url", "https://staging.example.test/"]
    )

    assert exit_code == 0
    assert observed["api_url"] == "https://staging.example.test/api/evaluation/jobs/run"


def test_evaluation_gate_cli_prefers_explicit_api_url_over_base_url(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """明示 api-url は api-base-url より優先する。"""
    golden_set = _write_golden_set(tmp_path)
    observed: dict[str, Any] = {}

    def fake_post(
        *,
        api_url: str,
        payload: dict[str, Any],
        timeout: float,
        headers: dict[str, str],
        poll_interval: float,
    ) -> dict[str, Any]:
        observed["api_url"] = api_url
        return {"data": _metrics_payload(passed=True)}

    monkeypatch.setattr(evaluation_cli, "_post_evaluation_request", fake_post)

    exit_code = evaluation_cli.main(
        [
            str(golden_set),
            "--api-url",
            "https://explicit.example.test/custom/run",
            "--api-base-url",
            "https://staging.example.test",
        ]
    )

    assert exit_code == 0
    assert observed["api_url"] == "https://explicit.example.test/custom/run"


def test_evaluation_gate_cli_uses_run_specific_env_url(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """評価専用の env URL は汎用 RAG_EVALUATION_API_URL より優先する。"""
    golden_set = _write_golden_set(tmp_path)
    observed: dict[str, Any] = {}
    monkeypatch.setenv("RAG_EVALUATION_API_URL", "https://generic.example.test/api/evaluation/run")
    monkeypatch.setenv(
        "RAG_EVALUATION_RUN_API_URL",
        "https://run.example.test/api/evaluation/jobs/run",
    )

    def fake_post(
        *,
        api_url: str,
        payload: dict[str, Any],
        timeout: float,
        headers: dict[str, str],
        poll_interval: float,
    ) -> dict[str, Any]:
        observed["api_url"] = api_url
        return {"data": _metrics_payload(passed=True)}

    monkeypatch.setattr(evaluation_cli, "_post_evaluation_request", fake_post)

    exit_code = evaluation_cli.main([str(golden_set)])

    assert exit_code == 0
    assert observed["api_url"] == "https://run.example.test/api/evaluation/jobs/run"


def test_evaluation_gate_cli_rejects_invalid_golden_set_without_query_leakage(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """評価ファイルの validation error は query 本文を出さず exit 2 にする。"""
    golden_set = tmp_path / "invalid-golden-set.json"
    golden_set.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "invalid-case",
                        "query": "INV-SECRET の承認条件",
                        "relevant_document_ids": ["doc-1"],
                    }
                ],
                "thresholds": {"required_evidence_recall": 1.1},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fail_if_called(
        *,
        api_url: str,
        payload: dict[str, Any],
        timeout: float,
        headers: dict[str, str],
        poll_interval: float,
    ) -> dict[str, Any]:
        raise AssertionError("invalid golden set は API へ送らない")

    monkeypatch.setattr(evaluation_cli, "_post_evaluation_request", fail_if_called)

    exit_code = evaluation_cli.main([str(golden_set)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "評価ファイルの形式が不正です" in captured.err
    assert "thresholds.required_evidence_recall" in captured.err
    assert "INV-SECRET" not in captured.err


def test_evaluation_gate_cli_returns_three_on_api_error(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    capsys: CaptureFixture[str],
) -> None:
    """API 接続・HTTP エラーは gate failure と区別して exit 3 にする。"""
    golden_set = _write_golden_set(tmp_path)

    def fake_post(
        *,
        api_url: str,
        payload: dict[str, Any],
        timeout: float,
        headers: dict[str, str],
        poll_interval: float,
    ) -> dict[str, Any]:
        raise evaluation_cli.EvaluationGateError(
            "評価 API に接続できませんでした: gaierror",
            exit_code=3,
        )

    monkeypatch.setattr(evaluation_cli, "_post_evaluation_request", fake_post)

    exit_code = evaluation_cli.main([str(golden_set)])

    captured = capsys.readouterr()
    assert exit_code == 3
    assert "評価 API に接続できませんでした" in captured.err
    assert captured.out == ""


def _write_golden_set(tmp_path: Path) -> Path:
    path = tmp_path / "golden-set.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "id": "policy-amount",
                        "query": "承認条件はいくらですか。",
                        "relevant_document_ids": ["doc-1"],
                        "required_answer_points": ["120000"],
                    }
                ],
                "top_k": 5,
                "filters": {"status": "indexed"},
                "thresholds": {
                    "required_evidence_recall": 0.8,
                    "mrr": 0.7,
                    "answer_correctness": 0.8,
                    "response_decision_accuracy": 0.9,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _metrics_payload(*, passed: bool, error_count: int = 0) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    if not passed:
        failures = [
            {
                "metric": "required_evidence_recall",
                "actual": 0.5,
                "threshold": 0.8,
            }
        ]
    return {
        "case_count": 1,
        "error_count": error_count,
        "required_evidence_recall": 1.0 if passed else 0.5,
        "mrr": 1.0,
        "faithfulness": 0.8,
        "citation_traceability_coverage": 1.0 if passed else 0.5,
        "answer_correctness": 1.0,
        "response_decision_accuracy": 1.0 if passed else 0.0,
        "metric_case_counts": {"required_evidence_recall": 1, "requirement_coverage": 0},
        "passed": passed,
        "threshold_failures": failures,
        "case_results": [],
    }


def test_evaluation_gate_cli_waits_longer_than_evaluation_job_time_limit() -> None:
    """--timeout 未指定なら job の全体の上限より長く待つ（結果を受け取る。#390 / #1430）。"""
    from app.config import Settings

    assert evaluation_cli._job_wait_seconds({"time_limit_seconds": 12000}) == (
        12000 + evaluation_cli.JOB_WAIT_MARGIN_SECONDS
    )
    # 古い backend で job の上限が分からないときは、以前の既定の上限より長く待つ。
    job_limit = Settings.model_fields["rag_evaluation_job_timeout_seconds"].default
    assert evaluation_cli._job_wait_seconds({}) == evaluation_cli.DEFAULT_TIMEOUT_SECONDS
    assert job_limit < evaluation_cli.DEFAULT_TIMEOUT_SECONDS


def test_evaluation_gate_cli_timeout_defaults_to_job_limit(
    monkeypatch: MonkeyPatch,
) -> None:
    """--timeout と RAG_EVALUATION_TIMEOUT_SECONDS が無ければ、待ち時間は job の上限から決める。"""
    monkeypatch.delenv("RAG_EVALUATION_TIMEOUT_SECONDS", raising=False)
    assert evaluation_cli._build_parser().parse_args(["golden.json"]).timeout is None
    monkeypatch.setenv("RAG_EVALUATION_TIMEOUT_SECONDS", "900")
    assert evaluation_cli._build_parser().parse_args(["golden.json"]).timeout == 900.0


class _FakeJobServer:
    """評価 job の API（投入・状態・取り消し）の fake。"""

    def __init__(self, statuses: list[dict[str, Any]]) -> None:
        self.statuses = statuses
        self.requests: list[tuple[str, str]] = []

    def handler(self, request: Any) -> Any:
        import httpx

        self.requests.append((request.method, request.url.path))
        if request.method == "POST" and request.url.path.endswith("/cancel"):
            return httpx.Response(200, json={"data": {**self.statuses[0], "status": "CANCELLED"}})
        if request.method == "POST":
            submitted = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return httpx.Response(202, json={"data": submitted})
        current = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return httpx.Response(200, json={"data": current})


def _job(status: str, completed: int, **extra: Any) -> dict[str, Any]:
    return {
        "job_id": "job-1",
        "kind": "run",
        "status": status,
        "total_cases": 2,
        "completed_cases": completed,
        **extra,
    }


def test_post_evaluation_request_polls_job_until_result(
    capsys: CaptureFixture[str],
) -> None:
    """投入 → 状態の取得を繰り返し、成功したら結果と job の状態を返す（#390）。"""
    import httpx

    server = _FakeJobServer(
        [
            _job("RUNNING", 0),
            _job("RUNNING", 1, current_case_id="c2"),
            _job("SUCCEEDED", 2, run_result=_metrics_payload(passed=True)),
        ]
    )
    sleeps: list[float] = []

    result = evaluation_cli._post_evaluation_request(
        api_url="http://rag.example.test/api/evaluation/run",
        payload={"cases": []},
        timeout=60,
        headers={"X-User-ID": "user"},
        poll_interval=0.5,
        transport=httpx.MockTransport(server.handler),
        sleep=sleeps.append,
    )

    # 以前の同期 API の URL は job の API に読み替える。
    assert server.requests[0] == ("POST", "/api/evaluation/jobs/run")
    assert server.requests[1:] == [("GET", "/api/evaluation/jobs/job-1")] * 2
    assert sleeps == [0.5, 0.5]
    assert result["data"]["passed"] is True
    assert result["job"]["status"] == "SUCCEEDED"
    assert "run_result" not in result["job"]
    err = capsys.readouterr().err
    assert "評価 job job-1: 0 / 2 件" in err
    assert "評価 job job-1: 1 / 2 件" in err


def test_post_evaluation_request_cancels_job_after_timeout() -> None:
    """待つ時間を超えたら job を取り消し、exit 3 の失敗にする。"""
    import httpx
    import pytest

    server = _FakeJobServer([_job("RUNNING", 0)])
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(evaluation_cli.EvaluationGateError) as raised:
        evaluation_cli._post_evaluation_request(
            api_url="http://rag.example.test/api/evaluation/jobs/run",
            payload={"cases": []},
            timeout=10,
            headers={},
            poll_interval=4,
            transport=httpx.MockTransport(server.handler),
            sleep=sleep,
            clock=lambda: now[0],
        )

    assert raised.value.exit_code == 3
    assert "取り消しました" in str(raised.value)
    assert server.requests[-1] == ("POST", "/api/evaluation/jobs/job-1/cancel")


def test_post_evaluation_request_waits_job_time_limit_without_timeout() -> None:
    """timeout が None なら、job の上限 + 余裕まで待ってから取り消す（#1430）。"""
    import httpx
    import pytest

    server = _FakeJobServer([_job("RUNNING", 0, time_limit_seconds=12000)])
    now = [0.0]

    def sleep(seconds: float) -> None:
        now[0] += seconds

    with pytest.raises(evaluation_cli.EvaluationGateError) as raised:
        evaluation_cli._post_evaluation_request(
            api_url="http://rag.example.test/api/evaluation/jobs/run",
            payload={"cases": []},
            timeout=None,
            headers={},
            poll_interval=1000,
            transport=httpx.MockTransport(server.handler),
            sleep=sleep,
            clock=lambda: now[0],
        )

    assert raised.value.exit_code == 3
    # 以前の固定の 3900 秒では打ち切らず、12000 + 300 秒を超えてから取り消す。
    assert now[0] >= 12000 + evaluation_cli.JOB_WAIT_MARGIN_SECONDS
    assert now[0] < 12000 + evaluation_cli.JOB_WAIT_MARGIN_SECONDS + 1000
    assert server.requests[-1] == ("POST", "/api/evaluation/jobs/job-1/cancel")


def test_post_evaluation_request_fails_when_job_is_not_succeeded() -> None:
    import httpx
    import pytest

    server = _FakeJobServer(
        [_job("RUNNING", 0), _job("FAILED", 1, error_message="品質評価の実行に失敗しました。")]
    )
    with pytest.raises(evaluation_cli.EvaluationGateError) as raised:
        evaluation_cli._post_evaluation_request(
            api_url="http://rag.example.test/api/evaluation/jobs/run",
            payload={"cases": []},
            timeout=60,
            headers={},
            poll_interval=1,
            transport=httpx.MockTransport(server.handler),
            sleep=lambda _: None,
        )
    assert raised.value.exit_code == 3
    assert "FAILED" in str(raised.value)
    assert "品質評価の実行に失敗しました。" in str(raised.value)


def test_job_urls_are_derived_from_submit_url() -> None:
    assert (
        evaluation_cli._job_submit_url("https://h.example/prefix/api/evaluation/run/")
        == "https://h.example/prefix/api/evaluation/jobs/run"
    )
    assert (
        evaluation_cli._job_submit_url("https://h.example/api/evaluation/jobs/run")
        == "https://h.example/api/evaluation/jobs/run"
    )
    assert (
        evaluation_cli._job_status_url("https://h.example/api/evaluation/jobs/run", "abc")
        == "https://h.example/api/evaluation/jobs/abc"
    )
