"""評価を process の中で流し、外部の AI の応答を記録・再生する CLI（#59）。

評価の処理（``_execute_run``）は偽物に差し替え、その中から外部の呼び出し（``external_call``）を
する。モデルの API・Oracle には接続しない。
"""

from __future__ import annotations

import json
import stat
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch
from rag_engine.resources.external_calls import external_call, external_call_mode

from app import config as config_module
from app.config import get_settings, settings_today
from app.rag import evaluation_run_cli
from app.schemas.evaluation import EvaluationMetrics, EvaluationRunRequest


@pytest.fixture(autouse=True)
def _restore_settings(monkeypatch: MonkeyPatch) -> None:
    """CLI が書き換える設定（接続・並行の数）を、テストの後に元へ戻す。"""
    settings = get_settings()
    names = ["rag_evaluation_concurrency"] + [
        f"enterprise_ai_{slot}{field}"
        for slot in ("", "secondary_", "tertiary_")
        for field in ("api_key", "endpoint")
    ]
    for name in names:
        monkeypatch.setattr(settings, name, getattr(settings, name))


def _golden(tmp_path: Path) -> Path:
    path = tmp_path / "golden.json"
    path.write_text(json.dumps({"cases": [{"id": "c1", "query": "質問"}]}), encoding="utf-8")
    return path


def _fake_run(monkeypatch: MonkeyPatch, *, live: Any, extra_call: bool = False) -> list[date]:
    """偽の評価: 埋め込みを 1 回（``extra_call`` なら 2 回）呼び、manifest を返す。"""
    from app.api.routes import evaluation as evaluation_route

    seen_today: list[date] = []

    async def execute_run(
        request: EvaluationRunRequest, *, time_budget_seconds: float
    ) -> tuple[EvaluationMetrics, str | None]:
        del time_budget_seconds
        seen_today.append(settings_today())
        for _ in range(2 if extra_call else 1):
            external_call("embeddings", {"input": [request.cases[0].query]}, live)
        manifest = {
            "as_of": settings_today().isoformat(),
            "evaluation_set": {"case_count": len(request.cases)},
            "code_version": "abc",
            "external_calls": {"mode": external_call_mode()},
        }
        return EvaluationMetrics(case_count=1, passed=True, run_manifest=manifest), None

    monkeypatch.setattr(evaluation_route, "_execute_run", execute_run)
    return seen_today


def _offline() -> object:
    raise AssertionError("再生で実の API を呼びました")


def test_record_then_replay_without_api(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    golden = _golden(tmp_path)
    recording = tmp_path / "private" / "recording.json"
    monkeypatch.setattr(config_module, "local_today", lambda settings: date(2026, 10, 31))
    _fake_run(monkeypatch, live=lambda: {"data": [[0.1, 0.2]]})

    code = evaluation_run_cli.main(
        ["run", str(golden), "--record", str(recording), "--output", str(tmp_path / "live.json")]
    )

    assert code == 0
    assert stat.S_IMODE(recording.stat().st_mode) == 0o600
    recorded = json.loads(recording.read_text(encoding="utf-8"))
    assert recorded["manifest"]["as_of"] == "2026-10-31"
    assert len(recorded["entries"]) == 1
    assert "質問" not in recording.read_text(encoding="utf-8")
    live_result = json.loads((tmp_path / "live.json").read_text(encoding="utf-8"))
    assert live_result["data"]["run_manifest"]["external_calls"]["mode"] == "record"

    # 別の日・API キーなしでも、記録の日で再生する。
    monkeypatch.setattr(config_module, "local_today", lambda settings: date(2026, 11, 5))
    monkeypatch.setattr(get_settings(), "enterprise_ai_api_key", "")
    seen_today = _fake_run(monkeypatch, live=_offline)
    output = tmp_path / "replay.json"

    code = evaluation_run_cli.main(
        ["run", str(golden), "--replay", str(recording), "--output", str(output)]
    )

    assert code == 0
    assert seen_today == [date(2026, 10, 31)]
    assert get_settings().enterprise_ai_api_key == evaluation_run_cli.REPLAY_PLACEHOLDER_KEY
    external = json.loads(output.read_text(encoding="utf-8"))["data"]["run_manifest"][
        "external_calls"
    ]
    assert external["mode"] == "replay"
    assert external["mismatches"] == 0
    assert external["unused"] == 0
    assert external["condition_differences"] == []

    # 記録と再生の結果は同じ実行条件（外部の呼び出しの扱い・コードの版は比べない）。
    assert evaluation_run_cli.main(["diff", str(tmp_path / "live.json"), str(output)]) == 0


@pytest.mark.parametrize("extra_call", [True, False])
def test_replay_fails_on_extra_or_missing_calls(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str], extra_call: bool
) -> None:
    golden = _golden(tmp_path)
    recording = tmp_path / "recording.json"
    _fake_run(monkeypatch, live=lambda: {"data": []}, extra_call=not extra_call)
    assert evaluation_run_cli.main(["run", str(golden), "--record", str(recording)]) == 0
    capsys.readouterr()

    _fake_run(monkeypatch, live=_offline, extra_call=extra_call)
    code = evaluation_run_cli.main(["run", str(golden), "--replay", str(recording)])

    assert code == evaluation_run_cli.REPLAY_FAILED_EXIT_CODE
    message = "同じ入力の余分な呼び出し" if extra_call else "記録の呼び出しが使われませんでした"
    assert message in capsys.readouterr().err


def test_diff_reports_condition_changes_and_old_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def write(name: str, manifest: dict[str, Any] | None) -> Path:
        path = tmp_path / name
        metrics = EvaluationMetrics(case_count=1, run_manifest=manifest)
        path.write_text(json.dumps({"data": metrics.model_dump(mode="json")}), encoding="utf-8")
        return path

    base = {"as_of": "2026-10-31", "corpus": {"sha256": "a"}, "comparable": True}
    left = write("left.json", base)
    right = write("right.json", {**base, "corpus": {"sha256": "b"}})
    unstable = write("unstable.json", {**base, "comparable": False, "changed_during_run": ["x"]})

    assert evaluation_run_cli.main(["diff", str(left), str(right)]) == 1
    assert 'corpus.sha256: "a" -> "b"' in capsys.readouterr().out
    assert evaluation_run_cli.main(["diff", str(left), str(unstable)]) == 1
    assert "実行中に条件が変わりました（x）" in capsys.readouterr().out
    assert evaluation_run_cli.main(["diff", str(left), str(write("old.json", None))]) == 1
    assert "#59 より前の結果" in capsys.readouterr().err


def test_genai_http_failures_replay_with_same_status(monkeypatch: MonkeyPatch) -> None:
    """埋め込み・再ランクの HTTP の失敗は、再生でも同じ status の HTTPError にする。"""
    import urllib.error
    from email.message import Message

    from rag_engine.resources.external_calls import ExternalCallSession, use_external_calls

    from app.clients.genai import GenAiClient

    url = "https://embedding.example/v1/embeddings"
    body = {"model": "embedding-a", "input": ["x"]}

    def rate_limited(self: GenAiClient, url: str, body: dict[str, Any], *, rerank: bool) -> Any:
        raise urllib.error.HTTPError(url, 429, "Too Many Requests", Message(), None)

    monkeypatch.setattr(GenAiClient, "_send", rate_limited)
    client = GenAiClient(settings=get_settings())
    session = ExternalCallSession("record")
    with use_external_calls(session), pytest.raises(urllib.error.HTTPError):
        client._post(url, body, rerank=False)

    monkeypatch.setattr(GenAiClient, "_send", lambda *args, **kwargs: _offline())
    replay = ExternalCallSession.from_recording(session.to_recording({}))
    with use_external_calls(replay), pytest.raises(urllib.error.HTTPError) as raised:
        client._post(url, body, rerank=False)
    assert raised.value.code == 429
