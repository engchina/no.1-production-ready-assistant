"""Agent が MCP 接続（RAG / NL2SQL / 外部 MCP）のツールを呼ぶ（#233 / #757）。

契約どおりの fake MCP サーバー（`mcp_support`）に対して、MCP の手順（initialize と session id）、
認証（サービストークンの `sub` / `aud` / claims・API キー）、エラーの変換、再試行、ツールの一覧
（readOnlyHint と承認）、画面からのツールの取得の利用者を確かめる。
"""

from __future__ import annotations

import inspect
import json
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import httpx
import pytest
from mcp_support import (
    DEFAULT_OUTPUTS,
    SERVICE_TOKEN_SECRET,
    FakeProductMcp,
    McpToolError,
    fake_product_mcp,
)
from pr_backend_core.observability.tracing import SpanUpdate, operation_trace
from pr_system_settings.auth.service_token import verify_service_token
from pytest import MonkeyPatch
from security_support import (
    ProductionAuth,
    client,
    enable_production_auth,
    login,
)

import app.features.agent.tools as tools_module
from app.features.agent.config import (
    PRODUCT_MCP_CONNECTION_LABELS,
    McpConnectionConfig,
    _product_connections,
    runtime_config_store,
)
from app.features.agent.skills import skill_registry
from app.features.agent.tools import (
    ExternalMcpToolInfo,
    ExternalToolError,
    JsonRpcResponse,
    ToolCall,
    ToolInvocationContext,
    ToolPolicy,
    ToolResult,
    list_mcp_connection_tools,
    mcp_function_name,
    mcp_tool_definition,
    mcp_tool_handler,
    tool_registry,
)
from app.features.agent.tools import _mcp_tools_from_result as _mcp_tools_from_result
from app.security.permissions import MENU_SETTINGS_EXTERNAL_MCP
from app.security.service import set_security_service
from app.settings import get_settings

USER_UUID = "11111111-2222-3333-4444-555555555555"
CONTRACTS = Path(__file__).resolve().parents[3] / "platform/contracts/mcp"
# fake の RAG / NL2SQL のツールの readOnlyHint（`mcp_support.FakeProductMcp`）。
READ_ONLY = {
    "rag_search": True,
    "rag_list_search_answer_profiles": True,
    "rag_recommend_search_answer_profile": True,
    "rag_read_source": True,
    "rag_lookup_guides": True,
    "rag_retrieve_evidence": True,
    "rag_outline": True,
    "rag_read_document": True,
    "rag_read_section": True,
    "rag_validate_answer": True,
    "nl2sql_query": False,
    "nl2sql_get_job": True,
}
ALLOW_ALL = ToolPolicy(
    allow={
        mcp_function_name("rag" if name.startswith("rag") else "nl2sql", name) for name in READ_ONLY
    }
)


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def _invoke(
    server_id: str,
    name: str,
    arguments: dict[str, Any],
    *,
    context: ToolInvocationContext | None = None,
    policy: ToolPolicy | None = ALLOW_ALL,
) -> ToolResult:
    config = runtime_config_store.get_mcp(server_id)
    info = ExternalMcpToolInfo(
        name=name,
        server_id=server_id,
        read_only=READ_ONLY.get(name, False),
        function_name=mcp_function_name(server_id, name),
    )
    definition = mcp_tool_definition(config, info)
    return tool_registry.invoke(
        ToolCall(name=definition.name, arguments=arguments),
        policy=policy,
        context=context or ToolInvocationContext(user_uuid=USER_UUID),
        definition=definition,
        handler=mcp_tool_handler(config, info),
    )


# ---------------------------------------------------------------------------
# 接続の一覧（RAG / NL2SQL は既定の接続）と Skill
# ---------------------------------------------------------------------------


def test_rag_and_nl2sql_are_builtin_service_token_connections() -> None:
    connections = {config.server_id: config for config in runtime_config_store.list_mcp_servers()}

    for server_id in ("rag", "nl2sql"):
        config = connections[server_id]
        assert config.source == "builtin"
        assert config.effective_auth_mode() == "service_token"
        assert config.audience() == server_id
    # 表示名は用途の名前に製品名を添える（#1325）。接続 ID（ツール名の接頭辞）は変えない。
    assert connections["rag"].label == "ナレッジ検索（RAG）"
    assert connections["nl2sql"].label == "データ問い合わせ（NL2SQL）"
    with pytest.raises(ValueError):
        runtime_config_store.remove_mcp_server("rag")


def test_builtin_skills_use_tools_in_product_contracts() -> None:
    """組み込み Skill が使う RAG / NL2SQL のツールが、製品の MCP の契約にある（#248）。"""
    contract_tools = {
        product: {
            tool["name"]
            for tool in json.loads((CONTRACTS / f"{product}-tools.json").read_text())["tools"]
        }
        for product in ("rag", "nl2sql")
    }
    used = 0
    for skill in skill_registry.export("builtin"):
        for requirement in skill.mcp_requirements:
            if requirement.server_id not in contract_tools:
                continue
            assert set(requirement.tool_names) <= contract_tools[requirement.server_id], skill.id
            used += 1
    assert used >= 3


def test_fake_product_mcp_follows_the_input_contracts() -> None:
    """fake の RAG / NL2SQL の引数が製品の MCP の契約と合う（必須の項目・項目の名前。#1379）。

    契約で必須の引数（例: nl2sql_query の profile_id）を Agent が送らない誤りを、fake が拒否して
    検出できるようにする。
    """
    fake = FakeProductMcp()
    checked = 0
    for product, server in fake.servers.items():
        contract = {
            tool["name"]: tool["inputSchema"]
            for tool in json.loads((CONTRACTS / f"{product}-tools.json").read_text())["tools"]
        }
        for name, tool in server.tools.items():
            schema = tool.input_model.model_json_schema()
            assert set(schema.get("required", [])) == set(contract[name].get("required", [])), name
            assert set(schema["properties"]) <= set(contract[name]["properties"]), name
            # 既定（Agent が契約の既定から件数を決める。#3240）も契約と同じ。
            for key, spec in schema["properties"].items():
                if "default" in spec and "default" in contract[name]["properties"][key]:
                    assert spec["default"] == contract[name]["properties"][key]["default"], (
                        name,
                        key,
                    )
            checked += 1
    # + nl2sql_list_profiles / nl2sql_recommend_profile / nl2sql_cancel_requirement（#1441）・
    # nl2sql_cancel_job（#3318）と、画面が保存した根拠を確かめ直す内部のツール
    # rag_readable_documents（#3252）。
    assert checked == len(READ_ONLY) + 5


def test_fake_nl2sql_outputs_carry_the_contract_schema_version() -> None:
    """fake の NL2SQL の出力は契約と同じ形の版を持つ（#3240）。"""
    contract = json.loads((CONTRACTS / "nl2sql-tools.json").read_text(encoding="utf-8"))
    for tool in contract["tools"]:
        output = DEFAULT_OUTPUTS[tool["name"]]
        if callable(output):
            output = output(SimpleNamespace(requirement_session_id="qs-1", job_id="job-1"))
        version = tool["outputSchema"]["properties"]["schema_version"]["default"]
        assert output["schema_version"] == version, tool["name"]


# ---------------------------------------------------------------------------
# MCP の手順と token
# ---------------------------------------------------------------------------


def test_initialize_session_id_and_headers(monkeypatch: MonkeyPatch) -> None:
    mcp = fake_product_mcp(monkeypatch)

    result = _invoke(
        "rag",
        "rag_search",
        {"query": "契約の更新条件"},
        context=ToolInvocationContext(user_uuid=USER_UUID, trace_id="t-1"),
    )

    assert result.success is True, result.error
    assert [request["method"] for request in mcp.requests] == [
        "initialize",
        "notifications/initialized",
        "tools/call",
    ]
    initialize, initialized, call = mcp.requests
    assert initialize["body"]["params"]["protocolVersion"] == "2025-06-18"
    assert initialize["body"]["params"]["clientInfo"]["name"] == "production-ready-agent"
    assert "mcp-session-id" not in initialize["headers"]
    # サーバーが返した session id を以降の request に付ける。
    assert initialized["headers"]["mcp-session-id"] == "session-rag-1"
    assert call["headers"]["mcp-session-id"] == "session-rag-1"
    for request in mcp.requests:
        assert request["headers"]["accept"] == "application/json, text/event-stream"
        assert request["headers"]["mcp-protocol-version"] == "2025-06-18"
    # 標準の tools/call（旧 gateway の server_id は送らない）。trace_id は JSON-RPC の id。
    assert call["body"]["id"] == "t-1"
    params = dict(call["body"]["params"])
    meta = params.pop("_meta")
    assert params == {
        "name": "rag_search",
        "arguments": {"query": "契約の更新条件"},
    }
    # traceparent は HTTP header と JSON-RPC の `params._meta` の両方に入れる（#3226。呼び先は
    # サービストークンの要求の `_meta` を優先して親にする）。値は `mcp.request` の span。
    assert meta == {"traceparent": call["headers"]["traceparent"]}
    assert "_meta" in initialize["body"]["params"]
    # 通知（id の無いメッセージ）は変えない。
    assert "params" not in initialized["body"]
    assert result.output is not None
    assert result.output["answer"] == "根拠付き回答"
    assert result.output["evidence"][0]["file_name"] == "契約書.pdf"
    assert result.output["evidence"][0]["locator"]["page_start"] == 3
    assert result.audit_metadata["tool_name"] == "rag__rag_search"
    assert result.audit_metadata["audit_tags"] == ["mcp", "rag"]


def test_mcp_request_spans_carry_the_mcp_method_and_tool(monkeypatch: MonkeyPatch) -> None:
    """`mcp.request` の span は JSON-RPC の method（`mcp.method.name`）と、`tools/call` なら
    呼んだツール（`gen_ai.tool.name`）を持つ（OpenTelemetry の MCP の semantic conventions。
    画面は `tools/call` だけを行にする。#3272）。引数は span に入れない。"""
    fake_product_mcp(monkeypatch)
    updates: list[SpanUpdate] = []

    with operation_trace("agent.execute", operation_id="run-3272", listener=updates.append):
        result = _invoke("rag", "rag_search", {"query": "合成の語 A"})

    assert result.success is True, result.error
    finished = [
        update.attributes
        for update in updates
        if update.operation_name == "mcp.request" and update.status == "done"
    ]
    assert [
        (attributes.get("mcp.method.name"), attributes.get("gen_ai.tool.name"))
        for attributes in finished
    ] == [
        ("initialize", None),
        ("notifications/initialized", None),
        ("tools/call", "rag_search"),
    ]
    assert all("合成の語 A" not in str(attributes) for attributes in finished)


def test_token_subject_audience_and_claims_follow_run_user(monkeypatch: MonkeyPatch) -> None:
    mcp = fake_product_mcp(monkeypatch)
    context = ToolInvocationContext(user_uuid=USER_UUID, run_id="run-233", agent_id="agent-233")

    rag = _invoke("rag", "rag_list_search_answer_profiles", {"limit": 10}, context=context)
    nl2sql = _invoke("nl2sql", "nl2sql_get_job", {"job_id": "job-1"}, context=context)

    assert rag.success is True, rag.error
    assert nl2sql.success is True, nl2sql.error
    rag_call, nl2sql_call = mcp.tool_calls
    # 接続ごとの audience の token（fake が verify_service_token で検証済み）。
    assert rag_call["product"] == "rag"
    assert nl2sql_call["product"] == "nl2sql"
    for call, audience in ((rag_call, "rag"), (nl2sql_call, "nl2sql")):
        claims = call["claims"]
        assert claims["sub"] == USER_UUID
        assert claims["aud"] == audience
        assert claims["iss"] == "agent"
        assert claims["run_id"] == "run-233"
        assert claims["agent_id"] == "agent-233"
        assert claims["exp"] - claims["iat"] == 60
    # 別の製品の audience では通らない。
    token = mcp.requests[-1]["headers"]["authorization"].removeprefix("Bearer ")
    with pytest.raises(Exception, match="無効"):
        verify_service_token(SERVICE_TOKEN_SECRET, token, audience="rag")


def test_missing_user_fails_before_calling_without_a_substitute(
    monkeypatch: MonkeyPatch, auth: ProductionAuth
) -> None:
    """利用者の無い呼び出しは、別の主体に置き換えず `mcp.user_required` で失敗する（#3251）。

    共通認証に利用者がいても、文脈に利用者が無ければ呼び先へ送らない（confused deputy を防ぐ）。
    """
    auth.create_user("svc-agent")
    mcp = fake_product_mcp(monkeypatch)

    for server_id, name, arguments in (
        ("rag", "rag_search", {"query": "a"}),
        ("nl2sql", "nl2sql_get_job", {"job_id": "j"}),
    ):
        result = _invoke(
            server_id, name, arguments, context=ToolInvocationContext(run_id="run-3251")
        )
        assert result.success is False
        assert result.error_code == "mcp.user_required"
    assert mcp.requests == []
    # `tools/list`（画面の「ツールを取得」・ツール権限の画面の経路）も同じ。
    with pytest.raises(ExternalToolError) as raised:
        list_mcp_connection_tools("rag", context=ToolInvocationContext())
    assert raised.value.code == "mcp.user_required"
    assert mcp.requests == []


def test_invoke_has_no_default_context() -> None:
    """`tool_registry.invoke` は利用者の無い既定の文脈を作らない（入口が主体を決める。#3251）。"""
    parameter = inspect.signature(tool_registry.invoke).parameters["context"]
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        tool_registry.invoke(ToolCall(name="rag__rag_search", arguments={}))  # type: ignore[call-arg]


def test_concurrent_calls_of_different_users_do_not_cross(monkeypatch: MonkeyPatch) -> None:
    """別の利用者の呼び出しを同時に動かしても、token の `sub` / `profile_ids` / `run_id` は
    それぞれの文脈のまま（#3251。thread をまたいで共有の主体を持たない）。"""
    barrier = threading.Barrier(2, timeout=10)

    def search(_argument: Any) -> dict[str, Any]:
        # 2 つの呼び出しが同時に呼び先の中にいる状態を作る（片方だけなら timeout で失敗する）。
        barrier.wait()
        return deepcopy(DEFAULT_OUTPUTS["rag_search"])

    mcp = fake_product_mcp(monkeypatch, outputs={"rag_search": search})
    users = {
        "a": ("aaaaaaaa-0000-0000-0000-000000000001", ("profile-a",)),
        "b": ("bbbbbbbb-0000-0000-0000-000000000002", ("profile-b1", "profile-b2")),
    }

    def call(key: str) -> ToolResult:
        user_uuid, profiles = users[key]
        context = ToolInvocationContext(
            run_id=f"run-{key}", user_uuid=user_uuid, profile_scopes={"rag": profiles}
        )
        return _invoke("rag", "rag_search", {"query": key}, context=context)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(call, ["a", "b"]))

    assert all(result.success for result in results), [result.error for result in results]
    assert len(mcp.tool_calls) == 2
    for tool_call in mcp.tool_calls:
        key = tool_call["arguments"]["query"]
        user_uuid, profiles = users[key]
        assert tool_call["claims"]["sub"] == user_uuid
        assert tool_call["claims"]["profile_ids"] == list(profiles)
        assert tool_call["claims"]["run_id"] == f"run-{key}"


def test_missing_service_token_secret_fails(monkeypatch: MonkeyPatch) -> None:
    mcp = fake_product_mcp(monkeypatch, secret="too-short")

    result = _invoke("rag", "rag_search", {"query": "a"})

    assert result.error_code == "mcp.service_token_not_configured"
    assert "PLATFORM_SERVICE_TOKEN_SECRET" in (result.error or "")
    assert mcp.requests == []


def test_not_configured_mcp_url_fails(monkeypatch: MonkeyPatch) -> None:
    fake_product_mcp(monkeypatch)
    runtime_config_store.upsert_mcp_server("rag", base_url="")

    result = _invoke("rag", "rag_search", {"query": "a"})

    assert result.error_code == "mcp.not_configured"
    assert result.error_details["server_id"] == "rag"


# ---------------------------------------------------------------------------
# エラーの変換と再試行
# ---------------------------------------------------------------------------


def test_is_error_is_converted_to_tool_error(monkeypatch: MonkeyPatch) -> None:
    fake_product_mcp(
        monkeypatch,
        outputs={
            "rag_search": McpToolError(
                "RAG_SEARCH_ANSWER_PROFILE_FORBIDDEN",
                "この検索・回答プロファイルは利用できません。",
                details={"search_answer_profile_id": "bv-x"},
            )
        },
    )

    result = _invoke("rag", "rag_search", {"query": "a", "search_answer_profile_id": "bv-x"})

    assert result.success is False
    assert result.error_code == "mcp.tool_error"
    assert result.error == "この検索・回答プロファイルは利用できません。"
    assert result.error_details["error_code"] == "RAG_SEARCH_ANSWER_PROFILE_FORBIDDEN"
    assert result.error_details["details"] == {"search_answer_profile_id": "bv-x"}
    assert result.error_details["tool_name"] == "rag_search"
    assert result.error_details["server_id"] == "rag"


def test_invalid_arguments_are_rejected_by_the_connection(monkeypatch: MonkeyPatch) -> None:
    fake_product_mcp(monkeypatch)

    result = _invoke("rag", "rag_search", {"top_k": 0})

    # 引数の検証は呼び先（MCP サーバー）の契約で行い、結果をそのままモデルへ返す。
    assert result.error_code == "mcp.tool_error"
    assert result.error_details["error_code"] == "MCP_TOOL_ARGUMENTS_INVALID"


@pytest.fixture
def sleeps(monkeypatch: MonkeyPatch) -> list[float]:
    """再試行の待ち（#854）を記録する（待たない）。"""
    recorded: list[float] = []
    monkeypatch.setattr(tools_module, "_retry_sleep", recorded.append)
    return recorded


@pytest.mark.parametrize("status", [500, 502, 504, "timeout"])
def test_write_tool_calls_are_not_retried_after_reaching_the_server(
    monkeypatch: MonkeyPatch, status: int | Literal["timeout"]
) -> None:
    """書き込みのツールは、送った後の失敗で再送しない（SQL の実行・LLM の呼び出しの重複を防ぐ）。"""
    mcp = fake_product_mcp(monkeypatch)
    mcp.tool_call_statuses.extend([status, status])

    result = _invoke("nl2sql", "nl2sql_query", {"question": "売上", "profile_id": "profile-sales"})

    expected = "timeout" if status == "timeout" else "http_error"
    assert result.error_code == f"mcp.{expected}"
    assert result.error_details["attempts"] == 1
    assert result.error_details["retryable"] is False
    assert [call["name"] for call in mcp.tool_calls] == ["nl2sql_query"]


@pytest.mark.parametrize("status", [500, "timeout"])
def test_read_only_tool_calls_are_not_retried_on_timeout_or_500(
    monkeypatch: MonkeyPatch, status: int | Literal["timeout"]
) -> None:
    """読み取り専用でも、読み取りの timeout（呼び先で LLM が動いている）と 500 は再送しない。"""
    mcp = fake_product_mcp(monkeypatch)
    mcp.tool_call_statuses.extend([status, status])

    result = _invoke("rag", "rag_search", {"query": "a"})

    assert result.success is False
    assert [call["name"] for call in mcp.tool_calls] == ["rag_search"]


def test_read_only_tool_calls_retry_gateway_errors(
    monkeypatch: MonkeyPatch, sleeps: list[float]
) -> None:
    """読み取り専用（readOnlyHint）のツールは、MCP の意味どおり 502 / 504 を再試行してよい。"""
    mcp = fake_product_mcp(monkeypatch)
    mcp.tool_call_statuses.extend([502, 504])

    result = _invoke("rag", "rag_search", {"query": "a"})

    assert result.success is True, result.error
    assert [call["name"] for call in mcp.tool_calls] == ["rag_search"] * 3
    # 0.5 秒から 2 倍ずつ（後半を jitter）。
    assert len(sleeps) == 2
    assert 0.25 <= sleeps[0] <= 0.5
    assert 0.5 <= sleeps[1] <= 1.0


def test_tool_calls_retry_503(monkeypatch: MonkeyPatch) -> None:
    mcp = fake_product_mcp(monkeypatch)
    mcp.tool_call_statuses.append(503)

    result = _invoke("nl2sql", "nl2sql_query", {"question": "売上", "profile_id": "profile-sales"})

    assert result.success is True, result.error
    assert [call["name"] for call in mcp.tool_calls] == ["nl2sql_query", "nl2sql_query"]


def test_retry_after_is_honored(monkeypatch: MonkeyPatch, sleeps: list[float]) -> None:
    mcp = fake_product_mcp(monkeypatch)
    mcp.tool_call_statuses.append((429, {"Retry-After": "3"}))

    result = _invoke("nl2sql", "nl2sql_query", {"question": "売上", "profile_id": "profile-sales"})

    assert result.success is True, result.error
    assert sleeps == [3.0]


def test_retry_that_does_not_fit_the_connection_timeout_is_not_attempted(
    monkeypatch: MonkeyPatch, sleeps: list[float]
) -> None:
    """待つと接続の timeout（呼び出し全体の期限）を超えるときは、再試行せずに失敗を返す。"""
    mcp = fake_product_mcp(monkeypatch, rag_timeout_seconds=10)
    mcp.tool_call_statuses.append((503, {"Retry-After": "30"}))

    result = _invoke("rag", "rag_search", {"query": "a"})

    assert result.error_code == "mcp.http_error"
    assert result.error_details["status_code"] == 503
    assert result.error_details["retryable"] is False
    assert "時間をおいて再実行してください" in (result.error or "")
    assert sleeps == []
    assert len(mcp.tool_calls) == 1


@pytest.mark.parametrize("failure", ["connect", "connect_timeout"])
def test_write_tool_calls_retry_when_the_request_did_not_reach_the_server(
    monkeypatch: MonkeyPatch, failure: Literal["connect", "connect_timeout"]
) -> None:
    """送信前の失敗（接続できない・接続の timeout）は、書き込みのツールでも再試行してよい。"""
    mcp = fake_product_mcp(monkeypatch)
    mcp.method_failures["tools/call"] = [failure, failure]

    result = _invoke("nl2sql", "nl2sql_query", {"question": "売上", "profile_id": "profile-sales"})

    assert result.success is True, result.error
    assert [call["name"] for call in mcp.tool_calls] == ["nl2sql_query"]


def test_unreachable_service_returns_a_clear_tool_error(
    monkeypatch: MonkeyPatch, sleeps: list[float]
) -> None:
    """呼び先が起動していないと、上限まで待って再試行し、直し方の分かる失敗をモデルへ返す。"""
    mcp = fake_product_mcp(monkeypatch)
    mcp.method_failures["initialize"] = ["connect"] * 10

    result = _invoke("rag", "rag_search", {"query": "a"})

    assert result.success is False
    assert result.error_code == "mcp.unreachable"
    assert result.error == (
        "MCP 接続「ナレッジ検索（RAG）」に接続できません（RAG のサービスが起動しているか、"
        "接続の URL が正しいかを確認してください）。"
    )
    max_retries = get_settings().agent_external_mcp_max_retries
    assert result.error_details["attempts"] == max_retries + 1
    assert len(sleeps) == max_retries
    assert mcp.tool_calls == []


def test_session_initialization_and_tools_list_retry(
    monkeypatch: MonkeyPatch, sleeps: list[float]
) -> None:
    """initialize → tools/list（状態を変えない手順）は 500 / 502 / 読み取りの timeout も再試行。"""
    mcp = fake_product_mcp(monkeypatch)
    mcp.method_failures["initialize"] = [500, "timeout"]
    mcp.method_failures["tools/list"] = [502]

    listed = list_mcp_connection_tools("rag", context=ToolInvocationContext(user_uuid=USER_UUID))

    assert {tool.name for tool in listed.tools} >= {"rag_search"}
    assert len(sleeps) == 3


# ---------------------------------------------------------------------------
# ツールの一覧・承認・ガードレール
# ---------------------------------------------------------------------------


def test_tools_list_reports_read_only_and_function_names(monkeypatch: MonkeyPatch) -> None:
    fake_product_mcp(monkeypatch)
    context = ToolInvocationContext(user_uuid=USER_UUID)

    rag = {tool.name: tool for tool in list_mcp_connection_tools("rag", context=context).tools}
    nl2sql = {
        tool.name: tool for tool in list_mcp_connection_tools("nl2sql", context=context).tools
    }

    assert rag["rag_search"].read_only is True
    assert rag["rag_list_search_answer_profiles"].read_only is True
    assert rag["rag_search"].function_name == "rag__rag_search"
    assert "query" in rag["rag_search"].input_schema["properties"]
    assert nl2sql["nl2sql_query"].read_only is False
    assert nl2sql["nl2sql_get_job"].read_only is True


def test_tools_list_reads_the_tool_title() -> None:
    """ツールの表示名は `title`、無ければ古い版の `annotations.title`。ツール定義に載せる。"""
    response = JsonRpcResponse(
        result={
            "tools": [
                {"name": "a", "title": " 表示名 A ", "annotations": {"title": "古い"}},
                {"name": "b", "annotations": {"title": "表示名 B", "readOnlyHint": True}},
                {"name": "c", "title": ""},
            ]
        }
    )
    listed = {tool.name: tool for tool in _mcp_tools_from_result("rag", response).tools}

    assert listed["a"].title == "表示名 A"
    assert listed["b"].title == "表示名 B"
    assert listed["c"].title is None
    config = runtime_config_store.get_mcp("rag")
    assert mcp_tool_definition(config, listed["b"]).title == "表示名 B"
    assert mcp_tool_definition(config, listed["c"]).title is None


def test_function_names_fit_the_model_tool_name_rules() -> None:
    assert mcp_function_name("rag", "rag_search") == "rag__rag_search"
    assert mcp_function_name("erp.v2", "stock/lookup") == "erp_v2__stock_lookup"
    long_name = mcp_function_name("connection", "x" * 100)
    assert len(long_name) == 64
    assert long_name != mcp_function_name("connection", "x" * 101)


def test_write_tools_require_approval_by_default(monkeypatch: MonkeyPatch) -> None:
    """書き込みのツールは既定で承認。nl2sql_query は既定で自動実行（#1832。明示の ask は優先）。"""
    mcp = fake_product_mcp(monkeypatch)
    default_policy = ToolPolicy()

    cancel = _invoke(
        "nl2sql",
        "nl2sql_cancel_requirement",
        {"requirement_session_id": "session-1"},
        policy=default_policy,
    )
    explicit_ask = _invoke(
        "nl2sql",
        "nl2sql_query",
        {"question": "売上", "profile_id": "profile-sales"},
        policy=ToolPolicy(ask={"nl2sql__nl2sql_query"}),
    )
    search = _invoke("rag", "rag_search", {"query": "契約"}, policy=default_policy)

    assert cancel.approval_required is True
    assert explicit_ask.approval_required is True
    assert search.success is True, search.error
    assert [call["name"] for call in mcp.tool_calls] == ["rag_search"]
    query = mcp_tool_definition(
        runtime_config_store.get_mcp("nl2sql"),
        ExternalMcpToolInfo(name="nl2sql_query", server_id="nl2sql", read_only=False),
    )
    # readOnlyHint は false のまま（送信後の失敗では再送しない）で、既定の判定だけ自動実行。
    assert query.permission_level == "write"
    assert default_policy.explain(query) == ("allow", "standard")
    assert ToolPolicy(default_mode="deny").decide(query) == "allow"


def test_nl2sql_sql_is_flagged_by_the_guardrail(monkeypatch: MonkeyPatch) -> None:
    job = {"job_id": "job-x", "status": "done", "generated_sql": "drop table customers"}
    fake_product_mcp(monkeypatch, outputs={"nl2sql_get_job": job})

    result = _invoke("nl2sql", "nl2sql_get_job", {"job_id": "job-x"})

    assert result.success is True, result.error
    assert "nl2sql.non_readonly_sql_returned_as_audit_only" in result.guardrail_warnings


# ---------------------------------------------------------------------------
# NL2SQL のジョブの完了を待つ（#848）
# ---------------------------------------------------------------------------


def _job_output(status: str, job_id: str = "job-1") -> dict[str, Any]:
    output: dict[str, Any] = {"job_id": job_id, "status": status}
    if status == "done":
        output.update(columns=["AMOUNT"], rows=[{"AMOUNT": 1200}], returned_count=1)
    return output


def _get_job_after(polls_until_done: int) -> Any:
    """`polls_until_done` 回目の nl2sql_get_job で done を返す fake（それまでは running）。"""
    calls = {"count": 0}

    def get_job(argument: Any) -> dict[str, Any]:
        calls["count"] += 1
        status = "done" if calls["count"] >= polls_until_done else "running"
        return _job_output(status, argument.job_id)

    return get_job


def test_running_nl2sql_job_is_awaited_inside_the_tool(monkeypatch: MonkeyPatch) -> None:
    mcp = fake_product_mcp(
        monkeypatch,
        outputs={"nl2sql_query": _job_output("running"), "nl2sql_get_job": _get_job_after(3)},
    )

    result = _invoke(
        "nl2sql",
        "nl2sql_query",
        {"question": "部門別の売上", "profile_id": "profile-sales", "wait_seconds": 45},
        context=ToolInvocationContext(user_uuid=USER_UUID, run_id=None, trace_id="call-1"),
    )

    assert result.success is True, result.error
    assert result.output is not None
    assert result.output["status"] == "done"
    assert result.output["rows"] == [{"AMOUNT": 1200}]
    # 続きは nl2sql_get_job を wait_seconds 付き（MCP の timeout 60 秒に収まる値）で呼ぶ。
    waits = mcp.calls_of("nl2sql_get_job")
    assert [call["arguments"] for call in waits] == [{"job_id": "job-1", "wait_seconds": 20}] * 3
    # 待つ分を含めて、SDK の function tool の timeout を決める。
    config = runtime_config_store.get_mcp("nl2sql")
    info = ExternalMcpToolInfo(name="nl2sql_query", server_id="nl2sql")
    assert mcp_tool_definition(config, info).timeout_seconds == 60.0 + 300.0
    rag = runtime_config_store.get_mcp("rag")
    assert (
        mcp_tool_definition(
            rag, ExternalMcpToolInfo(name="rag_search", server_id="rag")
        ).timeout_seconds
        == 60.0
    )


def test_nl2sql_get_job_also_waits_until_done(monkeypatch: MonkeyPatch) -> None:
    mcp = fake_product_mcp(monkeypatch, outputs={"nl2sql_get_job": _get_job_after(2)})

    result = _invoke("nl2sql", "nl2sql_get_job", {"job_id": "job-7"})

    assert result.output is not None
    assert result.output["status"] == "done"
    assert [call["arguments"]["job_id"] for call in mcp.calls_of("nl2sql_get_job")] == [
        "job-7",
        "job-7",
    ]


def test_job_wait_stops_at_the_total_budget(monkeypatch: MonkeyPatch) -> None:
    mcp = fake_product_mcp(
        monkeypatch,
        outputs={"nl2sql_query": _job_output("running"), "nl2sql_get_job": _job_output("running")},
    )
    monkeypatch.setattr(get_settings(), "agent_nl2sql_job_wait_seconds", 50.0)
    clock = {"now": 1000.0}

    def fake_monotonic() -> float:
        clock["now"] += 10.0  # 呼ぶたびに 10 秒進む（待ちの代わり）
        return clock["now"]

    monkeypatch.setattr(tools_module, "monotonic", fake_monotonic)

    result = _invoke("nl2sql", "nl2sql_query", {"question": "売上", "profile_id": "profile-sales"})

    # 上限を超えたら running のまま返す（モデルが job_id で続きを取れる）。
    assert result.output is not None
    assert (result.output["status"], result.output["job_id"]) == ("running", "job-1")
    polls = mcp.calls_of("nl2sql_get_job")
    assert 1 <= len(polls) <= 5
    assert all(1 <= call["arguments"]["wait_seconds"] <= 20 for call in polls)


def test_job_wait_is_disabled_with_zero_budget(monkeypatch: MonkeyPatch) -> None:
    mcp = fake_product_mcp(monkeypatch, outputs={"nl2sql_query": _job_output("pending")})
    monkeypatch.setattr(get_settings(), "agent_nl2sql_job_wait_seconds", 0.0)

    result = _invoke("nl2sql", "nl2sql_query", {"question": "売上", "profile_id": "profile-sales"})

    assert result.output is not None
    assert result.output["status"] == "pending"
    assert mcp.calls_of("nl2sql_get_job") == []


def test_job_wait_stops_when_the_run_is_cancelled(monkeypatch: MonkeyPatch) -> None:
    from app.features.agent.runtime import RunStatus, runtime_repository

    mcp = fake_product_mcp(
        monkeypatch,
        outputs={"nl2sql_query": _job_output("running"), "nl2sql_get_job": _job_output("running")},
    )
    statuses = iter([RunStatus.RUNNING, RunStatus.CANCELLED])

    class _Run:
        def __init__(self, status: RunStatus) -> None:
            self.status = status

    def get_run(run_id: str) -> _Run:
        assert run_id == "run-1"
        return _Run(next(statuses))

    monkeypatch.setattr(runtime_repository, "get_run", get_run)

    result = _invoke(
        "nl2sql",
        "nl2sql_query",
        {"question": "売上", "profile_id": "profile-sales"},
        context=ToolInvocationContext(user_uuid=USER_UUID, run_id="run-1"),
    )

    # 1 回目の待ちの後、Run がキャンセルされていたら待つのをやめる。
    assert result.output is not None
    assert result.output["status"] == "running"
    assert len(mcp.calls_of("nl2sql_get_job")) == 1


def test_job_wait_failure_returns_the_last_result(monkeypatch: MonkeyPatch) -> None:
    mcp = fake_product_mcp(monkeypatch, outputs={"nl2sql_query": _job_output("running")})
    mcp.outputs["nl2sql_get_job"] = McpToolError("NL2SQL_UNAVAILABLE", "一時的に利用できません。")

    result = _invoke("nl2sql", "nl2sql_query", {"question": "売上", "profile_id": "profile-sales"})

    assert result.success is True, result.error
    assert result.output is not None
    assert result.output["status"] == "running"
    assert len(mcp.calls_of("nl2sql_get_job")) == 1


# ---------------------------------------------------------------------------
# 外部 MCP の接続（API キー・structuredContent の無い応答）
# ---------------------------------------------------------------------------


def test_api_key_connection_sends_bearer_and_returns_text_content(
    monkeypatch: MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append({"headers": dict(request.headers), "body": body})
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "initialize":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "result": {"capabilities": {}}}
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"content": [{"type": "text", "text": "在庫は 12 個です。"}]},
            },
        )

    real_client = httpx.Client
    monkeypatch.setattr(
        "app.features.agent.tools.httpx.Client",
        lambda timeout: real_client(transport=httpx.MockTransport(handle), timeout=timeout),
    )
    runtime_config_store.upsert_mcp_server(
        "erp757", base_url="https://erp.example.test/mcp", auth_mode="api_key", api_key="k-757"
    )
    try:
        result = _invoke(
            "erp757",
            "stock_lookup",
            {"sku": "A-1"},
            policy=ToolPolicy(allow={"erp757__stock_lookup"}),
        )
    finally:
        runtime_config_store.remove_mcp_server("erp757")

    assert result.success is True, result.error
    assert result.output == {"content": "在庫は 12 個です。"}
    assert all(item["headers"]["authorization"] == "Bearer k-757" for item in requests)
    params = dict(requests[-1]["body"]["params"])
    # 外部の MCP にも trace の `_meta` を送る（呼び先は親にするかを自分で決める。#3226）。
    assert set(params.pop("_meta")) == {"traceparent"}
    assert params == {"name": "stock_lookup", "arguments": {"sku": "A-1"}}


# ---------------------------------------------------------------------------
# 画面からのツールの取得（production）
# ---------------------------------------------------------------------------


def test_connection_tools_endpoint_uses_logged_in_user(
    monkeypatch: MonkeyPatch, auth: ProductionAuth
) -> None:
    mcp = fake_product_mcp(monkeypatch)
    viewer = auth.user_with_permissions("mcp-viewer", [MENU_SETTINGS_EXTERNAL_MCP])

    response = client.get("/api/settings/mcp-connections/rag/tools", headers=login("mcp-viewer"))

    assert response.status_code == 200, response.text
    names = [tool["name"] for tool in response.json()["data"]["tools"]]
    assert "rag_search" in names
    assert mcp.requests[-1]["method"] == "tools/list"
    assert mcp.requests[-1]["claims"]["sub"] == viewer.user_uuid


# ---------------------------------------------------------------------------
# 利用者向けの失敗の文（#1014）と、組み込みの接続の認証方式
# ---------------------------------------------------------------------------


def _oauth_transport(
    monkeypatch: MonkeyPatch, handle: Callable[[httpx.Request], httpx.Response]
) -> None:
    real_client = httpx.Client
    monkeypatch.setattr(
        "app.features.agent.tools.httpx.Client",
        lambda timeout, **_kwargs: real_client(
            transport=httpx.MockTransport(handle), timeout=timeout
        ),
    )


def _raise_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ReadTimeout("timed out", request=request)


def _raise_connect_error(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


def _unauthorized(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(401, json={"error": "invalid_client"})


def _html(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text="<html>")


def _no_access_token(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"token_type": "bearer"})


@pytest.mark.parametrize(
    ("handle", "expected"),
    [
        (_unauthorized, "HTTP 401"),
        (_html, "JSON ではありません"),
        (_no_access_token, "access_token"),
        (_raise_timeout, "秒以内に終わりませんでした"),
        (_raise_connect_error, "接続できません"),
    ],
    ids=["http-401", "not-json", "no-access-token", "timeout", "unreachable"],
)
def test_oauth_token_failures_are_japanese(
    monkeypatch: MonkeyPatch,
    handle: Callable[[httpx.Request], httpx.Response],
    expected: str,
) -> None:
    _oauth_transport(monkeypatch, handle)
    monkeypatch.setattr(tools_module, "_mcp_oauth_token_cache", {})

    with pytest.raises(ExternalToolError) as caught:
        tools_module._mcp_oauth_bearer_token(
            token_url="https://idp.example.test/token",
            client_id="client-1014",
            client_secret="secret-1014",  # nosec B106 - テスト用
            scope=None,
            timeout_seconds=3,
        )

    message = caught.value.message
    assert expected in message
    assert "OAuth" in message
    assert "external MCP" not in message
    # 秘密は文にも details にも出さない。
    assert "secret-1014" not in message
    assert "secret-1014" not in json.dumps(caught.value.details, ensure_ascii=False)


def test_incomplete_oauth_credentials_are_japanese() -> None:
    with pytest.raises(ExternalToolError) as caught:
        tools_module._mcp_oauth_bearer_token(
            token_url="https://idp.example.test/token",
            client_id="client-1014",
            client_secret=None,
            scope=None,
            timeout_seconds=3,
        )
    assert "そろっていません" in caught.value.message
    assert "external MCP" not in caught.value.message


def test_invalid_mcp_responses_are_japanese() -> None:
    with pytest.raises(ExternalToolError) as schema_error:
        tools_module._mcp_jsonrpc_response({"jsonrpc": "2.0", "id": 1, "result": "ok"})
    assert "JSON-RPC の形式ではありません" in schema_error.value.message

    response = httpx.Response(200, json=["not", "an", "object"])
    with pytest.raises(ExternalToolError) as body_error:
        tools_module._response_json_object(
            response, service_code="mcp", service_label="MCP 接続「erp」", attempt=1
        )
    assert body_error.value.message == "MCP 接続「erp」の応答が JSON の object ではありません。"


DEPLOYED_RAG_URL = "http://127.0.0.1:8000/api/mcp"
DEPLOYED_NL2SQL_URL = "http://127.0.0.1:8100/api/mcp"


@pytest.fixture
def deployed_connections(monkeypatch: MonkeyPatch) -> dict[str, McpConnectionConfig]:
    """配備（環境変数）が RAG / NL2SQL の MCP の URL を与えた構成の接続（#1325）。"""
    monkeypatch.setattr(get_settings(), "agent_external_rag_mcp_url", DEPLOYED_RAG_URL)
    monkeypatch.setattr(get_settings(), "agent_external_nl2sql_mcp_url", f" {DEPLOYED_NL2SQL_URL} ")
    connections = {config.server_id: config for config in runtime_config_store.list_mcp_servers()}
    connections.update({config.server_id: config for config in _product_connections()})
    monkeypatch.setattr(runtime_config_store, "_mcp_servers", connections)
    return connections


def test_deployed_builtin_connection_url_is_locked(
    deployed_connections: dict[str, McpConnectionConfig],
) -> None:
    listed = client.get("/api/settings/mcp-connections")
    assert listed.status_code == 200, listed.text
    by_id = {item["server_id"]: item for item in listed.json()["data"]["connections"]}
    assert by_id["rag"]["base_url"] == DEPLOYED_RAG_URL
    assert by_id["rag"]["base_url_locked"] is True
    assert by_id["rag"]["label"] == PRODUCT_MCP_CONNECTION_LABELS["rag"]
    assert by_id["rag"]["removable"] is False
    assert by_id["nl2sql"]["base_url"] == DEPLOYED_NL2SQL_URL
    assert by_id["nl2sql"]["base_url_locked"] is True

    changed = client.patch(
        "/api/settings/mcp-connections/rag", json={"base_url": "http://other.example.test/api/mcp"}
    )
    assert changed.status_code == 400
    message = changed.json()["error_messages"][0]
    assert "AGENT_EXTERNAL_RAG_MCP_URL" in message
    assert "変えられません" in message
    cleared = client.patch("/api/settings/mcp-connections/nl2sql", json={"base_url": ""})
    assert cleared.status_code == 400
    assert "AGENT_EXTERNAL_NL2SQL_MCP_URL" in cleared.json()["error_messages"][0]

    # 今と同じ URL と、timeout など運用の値は変えられる。
    same = client.patch(
        "/api/settings/mcp-connections/rag",
        json={"base_url": DEPLOYED_RAG_URL, "timeout_seconds": 42},
    )
    assert same.status_code == 200, same.text
    assert same.json()["data"]["timeout_seconds"] == 42
    assert runtime_config_store.get_mcp("rag").base_url == DEPLOYED_RAG_URL
    assert client.request("DELETE", "/api/settings/mcp-connections/rag").status_code == 400
    assert runtime_config_store.get_mcp("rag").source == "builtin"


def test_builtin_connection_label_cannot_be_changed(
    deployed_connections: dict[str, McpConnectionConfig],
) -> None:
    renamed = client.patch("/api/settings/mcp-connections/nl2sql", json={"label": "売上 DB"})
    assert renamed.status_code == 400
    assert "名前" in renamed.json()["error_messages"][0]
    same = client.patch(
        "/api/settings/mcp-connections/nl2sql",
        json={"label": PRODUCT_MCP_CONNECTION_LABELS["nl2sql"]},
    )
    assert same.status_code == 200, same.text
    assert runtime_config_store.get_mcp("nl2sql").label == PRODUCT_MCP_CONNECTION_LABELS["nl2sql"]


def test_restore_keeps_deployed_url_and_standard_label(
    deployed_connections: dict[str, McpConnectionConfig],
) -> None:
    # 前の版・前の配備で保存した URL と名前は、配備の値と標準の名前を上書きしない。
    runtime_config_store.restore_mcp_server(
        McpConnectionConfig(
            server_id="rag",
            label="RAG",
            base_url="http://old-host/api/mcp",
            timeout_seconds=15,
            source="builtin",
            base_url_locked=False,
        )
    )
    restored = runtime_config_store.get_mcp("rag")
    assert restored.base_url == DEPLOYED_RAG_URL
    assert restored.base_url_locked is True
    assert restored.label == PRODUCT_MCP_CONNECTION_LABELS["rag"]
    # 運用の値（timeout）は保存した値を戻す。
    assert restored.timeout_seconds == 15


def test_builtin_connection_url_is_editable_without_deployment_url(
    monkeypatch: MonkeyPatch,
) -> None:
    # 配備の環境変数が無い構成（ローカルの開発）は、画面で URL を設定でき、保存した URL を戻す。
    monkeypatch.setattr(get_settings(), "agent_external_rag_mcp_url", None)
    monkeypatch.setattr(get_settings(), "agent_external_nl2sql_mcp_url", "  ")
    connections = {config.server_id: config for config in _product_connections()}
    assert connections["rag"].base_url_locked is False
    assert connections["nl2sql"].base_url is None
    assert connections["nl2sql"].base_url_locked is False
    monkeypatch.setattr(runtime_config_store, "_mcp_servers", connections)

    patched = client.patch(
        "/api/settings/mcp-connections/rag", json={"base_url": "http://localhost:8000/api/mcp"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["data"]["base_url_locked"] is False
    runtime_config_store.restore_mcp_server(
        McpConnectionConfig(
            server_id="nl2sql",
            label="NL2SQL",
            base_url="http://localhost:8100/api/mcp",
            source="builtin",
        )
    )
    restored = runtime_config_store.get_mcp("nl2sql")
    assert restored.base_url == "http://localhost:8100/api/mcp"
    assert restored.label == PRODUCT_MCP_CONNECTION_LABELS["nl2sql"]


def test_saved_connections_never_lock_the_url() -> None:
    # 標準の接続でない接続は、保存の値に base_url_locked があってもロックしない。
    runtime_config_store.restore_mcp_server(
        McpConnectionConfig(
            server_id="cp1325_saved", base_url="http://saved.example.test/mcp", base_url_locked=True
        )
    )
    try:
        assert runtime_config_store.get_mcp("cp1325_saved").base_url_locked is False
    finally:
        runtime_config_store.remove_mcp_server("cp1325_saved")


def test_builtin_connection_auth_mode_cannot_be_changed() -> None:
    before = runtime_config_store.get_mcp("rag")

    changed = client.patch("/api/settings/mcp-connections/rag", json={"auth_mode": "none"})
    audience = client.patch(
        "/api/settings/mcp-connections/rag", json={"service_audience": "other-product"}
    )
    # 今と同じ値は受け付ける（画面は組み込みの接続に認証方式を送らない）。
    same = client.patch(
        "/api/settings/mcp-connections/rag",
        json={"auth_mode": "service_token", "service_audience": "rag", "timeout_seconds": 9},
    )

    try:
        assert changed.status_code == 400
        assert "認証方式" in changed.json()["error_messages"][0]
        assert audience.status_code == 400
        assert same.status_code == 200, same.text
        after = runtime_config_store.get_mcp("rag")
        assert after.effective_auth_mode() == "service_token"
        assert after.audience() == "rag"
    finally:
        runtime_config_store.upsert_mcp_server("rag", timeout_seconds=before.timeout_seconds)
