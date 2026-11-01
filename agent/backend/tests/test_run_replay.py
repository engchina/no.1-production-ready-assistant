"""Run の実行構成の指紋と記録再生（#67）。

再生はモデル（記録の出力を返す `ScriptedModel`）と MCP（記録の応答を返す `httpx.MockTransport`）を
差し替え、実の通信を遮断して数える。同梱の記録（`agent/evaluation/replay/approval-wall.v1.json`）は
合成で、構成を意図して変えたら `python -m app.cli.run_replay <記録> --update` で作り直す。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from app.cli import run_replay
from app.cli.run_replay import (
    RecordRejected,
    ReplayRecord,
    load_record,
    main,
    replay_record,
)
from app.features.agent import run_config
from app.features.agent.runtime import runtime_repository
from app.features.agent.tools import ToolCall, ToolResult

RECORD = Path(__file__).resolve().parents[2] / "evaluation/replay/approval-wall.v1.json"
WRITE = "ops__register_item"


def _record(**updates: Any) -> ReplayRecord:
    record = load_record(RECORD)
    return record.model_copy(update=updates) if updates else record


def _replay(record: ReplayRecord, **policy: str) -> run_replay.ReplayReport:
    async def go() -> run_replay.ReplayReport:
        return await replay_record(record, policy_overrides=policy)

    return anyio.run(go)


def test_bundled_record_replays_the_same_without_external_calls() -> None:
    """同じ記録で同じ許可・承認・成果物。モデル・MCP は記録から返し、実の通信は 0。"""
    report = _replay(_record())

    assert report.status == "same", (report.reasons, report.differences, "--update が要るか確認")
    assert report.external_calls == {"model_replayed": 3, "mcp_replayed": 2, "live": 0}
    assert report.outcome["approvals"] == [{"tool": WRITE, "status": "approved"}]
    assert report.outcome["delivered"] == ["ops/lookup_item", "ops/register_item"]
    # 実行構成は Run の metadata に残り、資格情報・本文を含まない。
    assert report.run_config["fingerprint"] == _record().expected.run_config["fingerprint"]  # type: ignore[union-attr]
    assert report.run_config["model"]["snapshot"] == "unverified"
    assert "品目は照会してから" not in json.dumps(report.run_config, ensure_ascii=False)


def test_changed_approval_wall_is_detected_without_resending_writes() -> None:
    """承認の壁を変えると差が出る（指紋は同じ）。拒否はツールを渡さないので比べられない。"""
    allowed = _replay(_record(), **{WRITE: "allow"})
    assert allowed.status == "different"
    assert any(item.startswith("approvals:") for item in allowed.differences)
    assert "使われなかった承認の判断: ops__register_item" in allowed.differences

    # 照会も承認にすると、承認待ちで止まり、書き込みは呼び先へ届かない。
    stricter = _replay(_record(), ops__lookup_item="ask")
    assert stricter.status == "different"
    assert stricter.outcome["status"] == "waiting_approval"
    assert stricter.outcome["delivered"] == []
    assert stricter.external_calls["live"] == 0

    denied = _replay(_record(), **{WRITE: "deny"})
    assert denied.status == "not_comparable"
    assert any("tools（消えた: ops__register_item）" in reason for reason in denied.reasons)
    assert denied.outcome["delivered"] == ["ops/lookup_item"]


def test_rejected_approval_does_not_deliver_the_write() -> None:
    """却下を記録した再生: 書き込みは呼び先へ届かない（記録の応答も使わない）。"""
    base = _record()
    rejected = base.model_copy(
        update={"approvals": [base.approvals[0].model_copy(update={"approved": False})]}
    )

    report = _replay(rejected)

    assert report.outcome["approvals"] == [{"tool": WRITE, "status": "rejected"}]
    assert report.outcome["delivered"] == ["ops/lookup_item"]
    assert "使われなかったツールの応答: ops/register_item" in report.differences
    assert report.external_calls["live"] == 0


def test_insufficient_or_mismatched_records_are_not_comparable() -> None:
    base = _record()
    # モデルの出力が足りない（記録より多く呼ぶ）。
    short = _replay(base.model_copy(update={"model_turns": base.model_turns[:-1]}))
    assert short.status == "not_comparable"
    assert any("記録より多くモデルを呼びました" in reason for reason in short.reasons)

    # 記録に無い引数の呼び出しは呼び先へ送らずに失敗とする。
    results = [item.model_copy(deep=True) for item in base.tool_results]
    results[0].arguments = {"item_id": "B-2"}
    unmatched = _replay(base.model_copy(update={"tool_results": results}))
    assert unmatched.status == "not_comparable"
    assert "記録に無いツールの呼び出し: ops/lookup_item" in unmatched.reasons
    assert unmatched.external_calls["live"] == 0

    # 版の不一致: スキルの指示を変えると記録したモデルの出力は当てはまらない。
    agent = base.agent.model_copy(deep=True)
    agent.skills[0].instructions = "別の指示"
    changed = _replay(base.model_copy(update={"agent": agent}))
    assert changed.status == "not_comparable"
    assert any("instructions" in reason and "skills" in reason for reason in changed.reasons)

    # 期待の無い記録は比べられない。
    assert _replay(base.model_copy(update={"expected": None})).status == "not_comparable"


def test_live_network_is_blocked_in_the_sandbox() -> None:
    record = _record()
    traffic = run_replay._Traffic()  # noqa: SLF001 - 遮断の境界を直接確かめる
    with (
        run_replay._sandbox(record, record.tool_policy, traffic, model=None),  # noqa: SLF001
        pytest.raises(httpx.ConnectError),
    ):
        httpx.Client().get("https://llm.example/v1/responses")
    with (
        run_replay._sandbox(record, record.tool_policy, traffic, model=None),  # noqa: SLF001
        pytest.raises(httpx.ConnectError),
    ):
        anyio.run(httpx.AsyncClient().get, "https://llm.example/v1/responses")
    assert traffic.blocked == ["GET llm.example", "GET llm.example"]


def test_records_with_secrets_are_rejected(tmp_path: Path) -> None:
    data = json.loads(RECORD.read_text(encoding="utf-8"))
    data["tool_results"][0]["result"]["structuredContent"]["note"] = (
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789"
    )
    path = tmp_path / "leaky.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(RecordRejected, match="資格情報"):
        load_record(path)
    assert main([str(path)]) == 2


def test_cli_update_then_compare(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = json.loads(RECORD.read_text(encoding="utf-8"))
    data.pop("expected")
    path = tmp_path / "record.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert main([str(path)]) == 2  # 期待の無い記録は比べられない
    assert main([str(path), "--update"]) == 0
    assert "expected" in json.loads(path.read_text(encoding="utf-8"))
    assert main([str(path)]) == 0
    assert main([str(path), "--policy", f"{WRITE}=allow"]) == 1
    assert main([str(path), "--policy", f"{WRITE}=never"]) == 2
    assert "status: different" in capsys.readouterr().out


def test_run_config_keeps_previous_fingerprint_when_rebuilt_differently() -> None:
    """承認の後の再開で構成が変わったら、前の指紋を残す（同じ Run の中の変化を分かるように）。"""
    from app.features.agent.runtime import AgentProfile, RunCreateRequest

    runtime_repository.create_agent(AgentProfile(id="test67-agent", name="指紋"))
    try:
        run = runtime_repository.create_builtin_run(
            RunCreateRequest(goal="x", agent_id="test67-agent"),
            created_by_user_uuid=run_replay.REPLAY_USER_UUID,
        )
        runtime_repository.note_run_config(run.id, {"fingerprint": "a"})
        runtime_repository.note_run_config(run.id, {"fingerprint": "a"})
        runtime_repository.note_run_config(run.id, {"fingerprint": "b"})
        saved = runtime_repository.get_run(run.id).metadata[run_config.RUN_CONFIG_METADATA_KEY]
        assert saved == {"fingerprint": "b", "previous_fingerprints": ["a"]}
    finally:
        runtime_repository.delete_agent("test67-agent")


class _Step:
    def __init__(self, name: str, output: dict[str, Any], success: bool = True) -> None:
        self.tool_call = ToolCall(name=name)
        self.tool_result = ToolResult(name=name, success=success, output=output)


def test_observed_references_and_rag_targets() -> None:
    config = {"skills": [{"id": "s", "references": [{"path": "a.md", "sha256": "h1"}]}]}
    steps = [
        _Step("skill_reference_read", {"skill_id": "s", "path": "a.md", "sha256": "h1"}),
        _Step("skill_reference_read", {"skill_id": "s", "path": "a.md", "sha256": "h2"}),
        _Step(
            "rag__rag_search",
            {
                "provenance": {"search_answer_profile_id": "p1", "prompt_version": "v1"},
                "evidence": [{"chunk_set_id": "cs-2"}, {"chunk_set_id": "cs-1"}],
            },
        ),
        _Step("rag__rag_search", {"provenance": {"search_answer_profile_id": "p9"}}, False),
    ]

    [reference] = run_config.observed_references(steps, config)
    # Run の間に本文が変わった（同じ本文を読み続けた保証が無い）。
    assert reference["sha256"] == ["h1", "h2"] and reference["changed"] is True
    targets = run_config.observed_rag_targets(steps)
    assert targets["chunk_set_ids"] == ["cs-1", "cs-2"]
    assert [item["search_answer_profile_id"] for item in targets["provenance"]] == ["p1"]


def test_config_differences_name_parts() -> None:
    base = {"tools": {"names": ["a", "b"]}, "model": {"model_id": "m1"}}
    target = {"tools": {"names": ["a", "c"]}, "model": {"model_id": "m2"}}

    assert run_config.config_differences(base, target) == [
        "tools（足された: c; 消えた: b）",
        "model",
    ]
