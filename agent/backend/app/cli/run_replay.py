"""業務 Agent の Run の記録再生（オフラインの検証。#67）。

記録（合成のモデルの出力とツールの応答）を使って、組み込み Runtime の制御（ツール権限・承認・
予算・検証・成果物）を、モデルの API と MCP の呼び先に接続せずに再生し、記録した結果と比べる。
利用者向けの「再実行」（`POST /api/runs/{run_id}/replay`。同じ質問で新しい Run を実際に実行する）
とは別の、開発・CI 用の入口で、その API の意味は変えない。

    cd agent/backend
    # 再生して、記録の期待（構成の指紋と結果）と比べる
    uv run python -m app.cli.run_replay ../evaluation/replay/approval-wall.v1.json
    # 承認の壁を変えて差を見る（ツール権限の上書き。`allow` / `ask` / `deny`）
    uv run python -m app.cli.run_replay <記録> --policy ops__register_item=allow
    # 記録を作り直す（入力を書いた記録に、今の構成の指紋と結果を期待として書き込む）
    uv run python -m app.cli.run_replay <記録> --update

再生の境界:

- モデルは記録の出力を順に返す（`agents.testing.ScriptedModel`）。記録より多く呼べば失敗、
  使わなかった出力が残っても失敗。MCP は記録の接続のツール一覧と `tools/call` の応答だけを返す
  （`httpx.MockTransport`）。記録に無い呼び出しは呼び先へ送らずに失敗とし、実の通信は
  `httpx` の transport で遮断して数える（どれも 0 でなければ失敗）。書き込みのツールも記録の
  応答を返すだけで、副作用は起きない。
- 記録したモデルの出力は、記録した構成（指示・ツールの定義・スキル・MCP の schema・モデルの
  設定）に対する出力である。今の構成の指紋が記録と違えば、再生を比べられない（指示・スキル・
  schema・モデルの変更は新しい実走の評価が要る）。ツール権限の「承認」/「自動」はモデルに渡す
  ツールを変えないので指紋は同じで、再生で差を検出できる（「拒否」はツールを渡さないので指紋が
  変わる）。
- 記録に資格情報・個人情報（Bearer・API キー・メール・カード番号など）があれば読まない。
  記録は合成のデータで作り、業務の Run の原文を記録に使わない。
- 再生は process の中のメモリだけを使う（`main` は `.env` を読まず、保存先を memory にする）。

終了コード: 0 = 構成も結果も記録と同じ、1 = 構成は同じで結果が違う（制御の回帰・承認の壁の
違い）、2 = 記録の誤り・構成の違い・記録との不一致・機微情報・実の通信の試み（比べられない）。
手順は agent/docs/agent-run-replay.md。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from unittest import mock

from pydantic import BaseModel, ConfigDict, Field, ValidationError

RECORD_SCHEMA_VERSION = 1
REPLAY_USER_UUID = "00000000-0000-4000-8000-000000000067"
REPLAY_AGENT_ID = "replay-agent"
REPLAY_HOST_SUFFIX = ".replay.invalid"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReplayTool(_Strict):
    name: str = Field(..., min_length=1)
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    read_only: bool = False


class ReplayConnection(_Strict):
    server_id: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,30}$")
    label: str = ""
    tools: list[ReplayTool] = Field(..., min_length=1)


class ReplayRequirement(_Strict):
    server_id: str
    tool_names: list[str]


class ReplayReference(_Strict):
    path: str
    content: str


class ReplaySkill(_Strict):
    id: str = Field(..., pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    name: str
    instructions: str = ""
    mcp_requirements: list[ReplayRequirement] = Field(default_factory=list)
    references: list[ReplayReference] = Field(default_factory=list)


class ReplayAgent(_Strict):
    name: str
    instructions: str = ""
    skills: list[ReplaySkill] = Field(..., min_length=1)


class ReplayModel(_Strict):
    """記録したときのモデルの設定（指紋に入る。接続先・資格情報は持たない）。"""

    model_id: str = "replay-model"
    reasoning_effort: str = ""
    max_output_tokens: int = 32768
    max_input_tokens: int = 128000


class ReplayToolPolicy(_Strict):
    default_mode: Literal["approval", "deny"] = "approval"
    allow: list[str] = Field(default_factory=list)
    ask: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)


class ReplayModelItem(_Strict):
    """モデルの出力の 1 項目（ツールの呼び出しか、利用者への文）。"""

    type: Literal["function_call", "message"]
    name: str = ""
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str = ""
    text: str = ""


class ReplayToolResult(_Strict):
    """MCP の `tools/call` の応答（`arguments` が None なら引数を問わない）。"""

    server_id: str
    tool: str
    arguments: dict[str, Any] | None = None
    result: dict[str, Any]


class ReplayApproval(_Strict):
    tool: str
    approved: bool


class ReplayExpected(_Strict):
    run_config: dict[str, Any]
    outcome: dict[str, Any]


class ReplayRecord(_Strict):
    schema_version: Literal[1]
    description: str = ""
    agent: ReplayAgent
    connections: list[ReplayConnection] = Field(default_factory=list)
    model: ReplayModel = Field(default_factory=ReplayModel)
    tool_policy: ReplayToolPolicy = Field(default_factory=ReplayToolPolicy)
    goal: str = Field(..., min_length=1)
    model_turns: list[list[ReplayModelItem]] = Field(..., min_length=1)
    tool_results: list[ReplayToolResult] = Field(default_factory=list)
    approvals: list[ReplayApproval] = Field(default_factory=list)
    expected: ReplayExpected | None = None


class RecordRejected(ValueError):
    """記録を再生に使えない（理由は利用者向けの日本語）。"""


def load_record(path: Path) -> ReplayRecord:
    """記録を読む。資格情報・個人情報を含む記録は `RecordRejected`。"""
    from app.features.agent.tools import sensitive_findings

    data = json.loads(path.read_text(encoding="utf-8"))
    findings = sensitive_findings(data)
    if findings:
        raise RecordRejected(
            "記録に資格情報か個人情報らしい値があります（合成のデータで作り直してください）: "
            + ", ".join(findings)
        )
    try:
        return ReplayRecord.model_validate(data)
    except ValidationError as exc:
        raise RecordRejected(f"記録の形が違います: {exc}") from exc


@dataclass
class _Traffic:
    """再生の間の外部の呼び出し（記録から返した数・記録との不一致・遮断した実の通信）。"""

    served: list[dict[str, Any]] = field(default_factory=list)
    # まだ使っていない記録のツールの応答（記録の順）。
    pending: list[ReplayToolResult] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mcp_handler(record: ReplayRecord, traffic: _Traffic) -> Any:
    import httpx

    from app.features.agent.tools import MCP_PROTOCOL_VERSION

    connections = {item.server_id: item for item in record.connections}
    traffic.pending = list(record.tool_results)
    pending = traffic.pending

    def reply(body: Mapping[str, Any], result: object) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": result})

    def error(body: Mapping[str, Any], message: str) -> httpx.Response:
        payload = {"code": -32000, "message": message}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body.get("id"), "error": payload})

    def handle(request: httpx.Request) -> httpx.Response:
        server_id = request.url.host.removesuffix(REPLAY_HOST_SUFFIX)
        connection = connections.get(server_id)
        if connection is None or not request.url.host.endswith(REPLAY_HOST_SUFFIX):
            traffic.blocked.append(f"{request.method} {request.url.host}")
            raise httpx.ConnectError("replay: 記録の接続のほかへは送りません", request=request)
        body = json.loads(request.content or b"{}")
        method = body.get("method")
        if "id" not in body:
            return httpx.Response(202)
        if method == "initialize":
            return reply(body, {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}})
        if method == "tools/list":
            return reply(
                body,
                {
                    "tools": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "inputSchema": tool.input_schema,
                            "annotations": {"readOnlyHint": tool.read_only},
                        }
                        for tool in connection.tools
                    ]
                },
            )
        if method == "tools/call":
            params = body.get("params") or {}
            name, arguments = params.get("name"), params.get("arguments") or {}
            for index, item in enumerate(pending):
                if (
                    item.server_id == server_id
                    and item.tool == name
                    and (
                        item.arguments is None
                        or _canonical(item.arguments) == _canonical(arguments)
                    )
                ):
                    pending.pop(index)
                    traffic.served.append({"server_id": server_id, "tool": name})
                    return reply(body, item.result)
            traffic.mismatches.append(f"記録に無いツールの呼び出し: {server_id}/{name}")
            return error(body, "replay: 記録に無い呼び出しです（呼び先へは送っていません）。")
        traffic.mismatches.append(f"記録に無い MCP のメソッド: {server_id}/{method}")
        return error(body, "replay: 記録に無いメソッドです。")

    return handle


def _model_steps(record: ReplayRecord) -> list[list[Any]]:
    from agents.testing import assistant_message, function_call

    steps: list[list[Any]] = []
    for turn in record.model_turns:
        items: list[Any] = []
        for number, item in enumerate(turn):
            if item.type == "function_call":
                items.append(
                    function_call(
                        item.name, item.arguments, call_id=item.call_id or f"call-{number}"
                    )
                )
            else:
                items.append(assistant_message(item.text))
        steps.append(items)
    return steps


@contextmanager
def _sandbox(
    record: ReplayRecord, policy: ReplayToolPolicy, traffic: _Traffic, model: Any
) -> Iterator[None]:
    """記録の業務 Agent・スキル・MCP 接続・ツール権限を登録し、外部の呼び出しを差し替える。"""
    import httpx

    from app.features.agent import builtin_runtime
    from app.features.agent.builtin_runtime import ModelTarget
    from app.features.agent.config import (
        McpConnectionConfig,
        ToolPolicyRuntimeConfig,
        runtime_config_store,
    )
    from app.features.agent.runtime import AgentProfile, runtime_repository
    from app.features.agent.skills import (
        AgentSkillDefinition,
        SkillMcpRequirement,
        SkillReference,
        skill_registry,
    )

    handler = _mcp_handler(record, traffic)
    real_client = httpx.Client

    def client_factory(*_args: Any, **kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

    def blocked(name: str) -> Any:
        def deny(_self: Any, request: httpx.Request) -> Any:
            traffic.blocked.append(f"{request.method} {request.url.host}")
            raise httpx.ConnectError("replay: 実の通信は遮断しています", request=request)

        return deny

    connections = {
        **{item.server_id: item for item in runtime_config_store.list_mcp_servers()},
        **{
            item.server_id: McpConnectionConfig(
                server_id=item.server_id,
                label=item.label or item.server_id,
                base_url=f"http://{item.server_id}{REPLAY_HOST_SUFFIX}/api/mcp",
                auth_mode="none",
                trust_tool_annotations=True,
            )
            for item in record.connections
        },
    }
    target = ModelTarget(
        model_id=record.model.model_id,
        endpoint="",
        project_id="",
        api_key="",
        reasoning_effort=record.model.reasoning_effort,
        max_output_tokens=record.model.max_output_tokens,
        max_input_tokens=record.model.max_input_tokens,
    )
    skill_ids = [skill.id for skill in record.agent.skills]
    with ExitStack() as stack:
        # `httpx.Client` は module の属性なので、Agent のどの module の同期の client も記録の
        # transport を通る（記録の接続のほかへの送信は遮断して数える）。
        stack.enter_context(mock.patch.object(httpx, "Client", client_factory))
        stack.enter_context(
            mock.patch.object(httpx.HTTPTransport, "handle_request", blocked("sync"))
        )
        stack.enter_context(
            mock.patch.object(httpx.AsyncHTTPTransport, "handle_async_request", blocked("async"))
        )
        stack.enter_context(mock.patch.object(runtime_config_store, "_mcp_servers", connections))
        stack.enter_context(
            mock.patch.object(
                runtime_config_store,
                "get_tool_policy",
                lambda: ToolPolicyRuntimeConfig(
                    default_mode=policy.default_mode,
                    allow=set(policy.allow),
                    ask=set(policy.ask),
                    deny=set(policy.deny),
                ),
            )
        )
        stack.enter_context(
            mock.patch.object(builtin_runtime, "resolve_model_target", lambda _model_id="": target)
        )
        stack.enter_context(mock.patch.object(builtin_runtime, "model_factory", lambda _t: model))
        for skill in record.agent.skills:
            skill_registry.upsert_custom(
                AgentSkillDefinition(
                    id=skill.id,
                    name=skill.name,
                    instructions=skill.instructions,
                    mcp_requirements=[
                        SkillMcpRequirement(server_id=item.server_id, tool_names=item.tool_names)
                        for item in skill.mcp_requirements
                    ],
                    references=[
                        SkillReference(
                            path=item.path, content=item.content, characters=len(item.content)
                        )
                        for item in skill.references
                    ],
                )
            )
            stack.callback(skill_registry.remove, skill.id)
        runtime_repository.create_agent(
            AgentProfile(
                id=REPLAY_AGENT_ID,
                name=record.agent.name,
                instructions=record.agent.instructions,
                skill_ids=skill_ids,
            )
        )
        stack.callback(_quietly, runtime_repository.delete_agent, REPLAY_AGENT_ID)
        yield


def _quietly(function: Any, *args: Any) -> None:
    with suppress(KeyError, ValueError):
        function(*args)


def run_outcome(run: Any, traffic: _Traffic) -> dict[str, Any]:
    """比べる結果（状態・ツールの判定と実行・承認・呼び先へ届いた呼び出し・成果物・回答）。"""
    steps = []
    for step in run.steps:
        if step.tool_call is None:
            continue
        result = step.tool_result
        steps.append(
            {
                "tool": step.tool_call.name,
                "status": step.status.value,
                "decision": result.policy_decision.value
                if result is not None and result.policy_decision is not None
                else None,
                "success": result.success if result is not None else None,
                "error_code": result.error_code if result is not None else None,
            }
        )
    answer = next(
        (str(item.content.get("text", "")) for item in run.artifacts if item.kind == "answer"),
        None,
    )
    return {
        "status": run.status.value,
        "steps": steps,
        "approvals": [
            {"tool": item.tool_call.name, "status": item.status.value} for item in run.approvals
        ],
        # 呼び先へ届いた tools/call（実走なら副作用が起きる呼び出し）。
        "delivered": [f"{item['server_id']}/{item['tool']}" for item in traffic.served],
        "artifacts": sorted(item.kind for item in run.artifacts),
        "answer": answer,
    }


def outcome_differences(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    return [
        f"{key}: {_canonical(expected.get(key))} → {_canonical(actual.get(key))}"
        for key in sorted(set(expected) | set(actual))
        if expected.get(key) != actual.get(key)
    ]


@dataclass
class ReplayReport:
    status: Literal["same", "different", "not_comparable"]
    reasons: list[str]
    differences: list[str]
    run_config: dict[str, Any]
    outcome: dict[str, Any]
    external_calls: dict[str, int]
    observed: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reasons": self.reasons,
            "differences": self.differences,
            "external_calls": self.external_calls,
            "run_config": self.run_config,
            "outcome": self.outcome,
            "observed": self.observed,
        }


def _decided_policy(base: ReplayToolPolicy, overrides: Mapping[str, str]) -> ReplayToolPolicy:
    policy = base.model_copy(deep=True)
    for tool, mode in overrides.items():
        if mode not in {"allow", "ask", "deny"}:
            raise RecordRejected(f"--policy の値は allow / ask / deny です: {tool}={mode}")
        for name in ("allow", "ask", "deny"):
            values = [item for item in getattr(policy, name) if item != tool]
            setattr(policy, name, [*values, tool] if name == mode else values)
    return policy


async def replay_record(
    record: ReplayRecord, *, policy_overrides: Mapping[str, str] | None = None
) -> ReplayReport:
    """記録を再生し、記録の期待（構成の指紋と結果）と比べる。"""
    from agents.testing import ScriptedModel

    from app.features.agent import builtin_runtime
    from app.features.agent.run_config import (
        RUN_CONFIG_METADATA_KEY,
        config_differences,
        observed_rag_targets,
        observed_references,
    )
    from app.features.agent.runtime import (
        ApprovalDecisionRequest,
        ApprovalStatus,
        RunCreateRequest,
        RunStatus,
        runtime_repository,
    )
    from app.features.agent.tools import sensitive_findings

    policy = _decided_policy(record.tool_policy, policy_overrides or {})
    traffic = _Traffic()
    model = ScriptedModel(_model_steps(record))
    decisions = list(record.approvals)
    with _sandbox(record, policy, traffic, model):
        run = runtime_repository.create_builtin_run(
            RunCreateRequest(goal=record.goal, agent_id=REPLAY_AGENT_ID),
            created_by_user_uuid=REPLAY_USER_UUID,
        )
        await builtin_runtime.execute_run(run.id)
        # 承認待ちになったら、記録の判断（ツールの名前ごと、記録の順）で決めて再開する。
        while True:
            state = runtime_repository.get_run(run.id)
            waiting = [a for a in state.approvals if a.status == ApprovalStatus.PENDING]
            if state.status != RunStatus.WAITING_APPROVAL or not waiting:
                break
            for approval in waiting:
                index = next(
                    (i for i, d in enumerate(decisions) if d.tool == approval.tool_call.name),
                    None,
                )
                if index is None:
                    break
                runtime_repository.decide_approval(
                    approval.id,
                    ApprovalDecisionRequest(
                        approved=decisions.pop(index).approved, decided_by="replay"
                    ),
                )
            else:
                await builtin_runtime.resume_run(run.id)
                continue
            break
        state = runtime_repository.get_run(run.id)
        config = dict(state.metadata.get(RUN_CONFIG_METADATA_KEY) or {})
        outcome = run_outcome(state, traffic)
        observed = {
            "references": observed_references(state.steps, config),
            "rag_targets": observed_rag_targets(state.steps),
        }
        # 失敗の経路を含め、保存した Run に機微情報が残っていないか（記録は検査済みなので、
        # 残っていれば Runtime が作った値の漏れ）。
        leaked = sensitive_findings(state.model_dump(mode="json"), field_names=False)
    mismatches = list(traffic.mismatches)
    if len(model.calls) > len(record.model_turns):
        mismatches.append(
            f"記録より多くモデルを呼びました（記録 {len(record.model_turns)} 回・"
            f"呼び出し {len(model.calls)} 回）"
        )
    # 使われなかった記録（制御の違いで Run が先に止まった・別の道を通った）は、結果の違いに数える。
    unused = (
        [f"使われなかったモデルの出力: {model.remaining_steps} 回分"]
        if model.remaining_steps
        else []
    )
    unused.extend(
        f"使われなかったツールの応答: {item.server_id}/{item.tool}" for item in traffic.pending
    )
    if decisions:
        unused.append("使われなかった承認の判断: " + ", ".join(item.tool for item in decisions))
    reasons = list(mismatches)
    if leaked:
        reasons.append("保存した Run に機微情報らしい値があります: " + ", ".join(leaked))
    if traffic.blocked:
        reasons.append("実の通信を試みました（遮断しました）: " + ", ".join(traffic.blocked))
    differences: list[str] = []
    if record.expected is None:
        reasons.append("記録に期待（expected）がありません（--update で作ってください）")
        reasons.extend(unused)
    else:
        changed = config_differences(record.expected.run_config, config)
        if changed:
            reasons.append(
                "実行構成が記録と違います（記録したモデルの出力は今の構成に当てはまりません。"
                "新しい実走の評価が要ります）: " + ", ".join(changed)
            )
        differences = outcome_differences(record.expected.outcome, outcome) + unused
    status: Literal["same", "different", "not_comparable"] = (
        "not_comparable" if reasons else "different" if differences else "same"
    )
    return ReplayReport(
        status=status,
        reasons=reasons,
        differences=differences,
        run_config=config,
        outcome=outcome,
        observed=observed,
        external_calls={
            "model_replayed": len(record.model_turns) - model.remaining_steps,
            "mcp_replayed": len(traffic.served),
            "live": len(traffic.blocked),
        },
    )


def _policy_overrides(values: Sequence[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for value in values:
        tool, separator, mode = value.partition("=")
        if not separator or not tool:
            raise RecordRejected(f"--policy は <ツール>=<allow|ask|deny> です: {value}")
        overrides[tool] = mode
    return overrides


def _isolate_process() -> None:
    """CLI の process は手元の `.env`・保存先を読まず、メモリだけを使う（再生で DB に書かない）。"""
    from pr_backend_core.config import isolate_env_files_for_tests

    isolate_env_files_for_tests("AGENT_")
    os.environ["AGENT_RUNTIME_REPOSITORY_BACKEND"] = "memory"
    os.environ["PLATFORM_ENVIRONMENT"] = "local"
    os.environ["PLATFORM_AUTH_MODE"] = "local"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli.run_replay",
        description="業務 Agent の Run を記録から再生する（モデル・MCP に接続しない検証）",
    )
    parser.add_argument("record", type=Path, help="記録（JSON）")
    parser.add_argument(
        "--policy",
        action="append",
        default=[],
        help="ツール権限の上書き（<ツール>=<allow|ask|deny>。承認の壁を変えて差を見る）",
    )
    parser.add_argument(
        "--update", action="store_true", help="今の構成の指紋と結果を、記録の期待として書き込む"
    )
    parser.add_argument("--json", action="store_true", help="結果を JSON で出す")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    import asyncio

    args = _build_parser().parse_args(argv)
    try:
        record = load_record(args.record)
        overrides = _policy_overrides(args.policy)
        if args.update:
            record = record.model_copy(update={"expected": None})
        report = asyncio.run(replay_record(record, policy_overrides=overrides))
    except (OSError, json.JSONDecodeError, RecordRejected) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    if args.update:
        blocking = [reason for reason in report.reasons if "期待（expected）" not in reason]
        if blocking:
            print("error: 記録を作れません: " + "; ".join(blocking), file=sys.stderr)
            return 2
        data = record.model_dump(mode="json", exclude_none=True)
        data["expected"] = {"run_config": report.run_config, "outcome": report.outcome}
        args.record.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
        print(f"wrote {args.record}", file=sys.stderr)
        return 0
    if args.json:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"status: {report.status}")
        print(f"external_calls: {_canonical(report.external_calls)}")
        for reason in report.reasons:
            print(f"  比べられない: {reason}")
        for difference in report.differences:
            print(f"  違い: {difference}")
    return {"same": 0, "different": 1}.get(report.status, 2)


if __name__ == "__main__":  # pragma: no cover - CLI boundary
    import logging

    # 保存先・DB の無い process の起動の警告（設定を読めない旨）を出さない。
    logging.disable(logging.WARNING)
    _isolate_process()
    raise SystemExit(main())
