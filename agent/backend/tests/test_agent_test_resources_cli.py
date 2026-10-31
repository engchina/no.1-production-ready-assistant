"""合成資料の業務 Agent の作成・公開の命令（#3545）を、Agent の管理 API に対して確かめる。

命令は Agent 自身の管理 API（Cookie / CSRF）を呼ぶ。テストは Agent の app を ASGI で動かし
（共通認証は production・InMemory、保存先は memory）、RAG の MCP を呼ぶデータの範囲の確認だけを
差し替える。RAG の API は呼ばない。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import anyio
import httpx
import pytest
from security_support import USER_PASSWORD, ProductionAuth, enable_production_auth

from app.cli import agent_test_resources as cli
from app.features.agent import router as agent_router
from app.features.agent.runtime import AgentProfile, AgentProfilePatch, runtime_repository
from app.main import app
from app.security.permissions import MENU_AGENTS
from app.security.service import set_security_service
from app.settings import get_settings

API = "http://testserver/api"
ADMIN = "agent-test-resources-admin"


class AppTransport(httpx.BaseTransport):
    """同期の httpx.Client の request を、Agent の app（ASGI）へ 1 件ずつ渡す。"""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path.removeprefix("/api")))
        body = request.read()

        async def send() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
                return await client.request(
                    request.method, str(request.url), headers=request.headers, content=body
                )

        response = anyio.run(send)
        return httpx.Response(
            response.status_code, headers=response.headers, content=response.content
        )

    def writes(self) -> list[tuple[str, str]]:
        return [item for item in self.requests if item[0] != "GET" and item[1] != "/auth/login"]


@pytest.fixture
def auth(monkeypatch: pytest.MonkeyPatch) -> Iterator[ProductionAuth]:
    production = enable_production_auth(monkeypatch)
    production.create_user(ADMIN, system_admin=True)
    monkeypatch.setenv("SUITE_TEST_LOGIN_USER", ADMIN)
    monkeypatch.setenv("SUITE_TEST_PASSWORD", USER_PASSWORD)
    yield production
    set_security_service(None)


@pytest.fixture
def verified(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, list[str]]]:
    """データの範囲の確認（RAG の MCP を編集者のサービストークンで呼ぶ）を差し替えて記録する。"""
    calls: list[dict[str, list[str]]] = []

    def verify(added: dict[str, list[str]], *, user_uuid: str | None) -> None:
        assert user_uuid
        calls.append(added)

    monkeypatch.setattr(agent_router, "verify_editor_can_use", verify)
    return calls


@pytest.fixture
def namespace() -> Iterator[str]:
    """テストごとの名前の区分（同じプロセスの memory の保存先で、ほかのテストと重ねない）。"""
    value = f"t{uuid4().hex[:12]}"
    yield value
    for agent in runtime_repository.list_agents():
        if agent.name.startswith(f"テスト_{value}_"):
            runtime_repository.delete_agent(agent.id)


def _names(namespace: str) -> dict[str, str]:
    definitions = cli.read_definitions(cli.DEFAULT_DEFINITIONS)
    return {
        key: f"テスト_{namespace}_{definition['name'].removeprefix('テスト_')}"
        for key, definition in definitions.items()
    }


def _agents(name: str) -> list[AgentProfile]:
    return [agent for agent in runtime_repository.list_agents() if agent.name == name]


def _run(
    capsys: pytest.CaptureFixture[str], *args: str, transport: httpx.BaseTransport | None = None
) -> tuple[int, dict[str, Any], AppTransport | None]:
    used = transport or AppTransport()
    code = cli.main(["--agent-api", API, *args], transport=used)
    output = json.loads(capsys.readouterr().out)
    return code, output, used if isinstance(used, AppTransport) else None


def _profiles(namespace: str, suffix: str = "") -> list[str]:
    return [
        "--profile",
        f"A=profile-A{suffix}",
        "--profile",
        f"B=profile-B{suffix}",
        "--namespace",
        namespace,
    ]


@pytest.mark.usefixtures("auth")
def test_creates_and_publishes_then_rerun_only_reads(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    namespace: str,
    verified: list[dict[str, list[str]]],
) -> None:
    state = tmp_path / "out" / "agent-state.json"
    code, output, transport = _run(capsys, *_profiles(namespace), "--state", str(state))
    assert code == 0, output
    assert output["data"] == json.loads(state.read_text(encoding="utf-8"))
    assert state.stat().st_mode & 0o777 == 0o600
    assert transport is not None
    assert {path for _method, path in transport.writes()} >= {"/agents"}
    # 保存の前に、画面と同じくデータの範囲を確かめた（RAG の MCP の部分だけ差し替え）。
    assert sorted(call["rag"][0] for call in verified) == ["profile-A", "profile-B"]
    for key, name in _names(namespace).items():
        (agent,) = _agents(name)
        group = output["data"]["groups"][key]
        assert group == {"agent_id": agent.id, "published_version": 1}
        assert agent.id.startswith("agent_") and agent.owner_user_uuid is None
        published = agent.published()
        assert published is not None and published.skills
        assert published.data_scopes["rag"].model_dump() == {
            "profile_ids": [f"profile-{key}"],
            "default_profile_id": f"profile-{key}",
        }
    code, again, transport = _run(capsys, *_profiles(namespace))
    assert code == 0 and again["data"]["groups"] == output["data"]["groups"]
    assert transport is not None and transport.writes() == []


@pytest.mark.usefixtures("auth", "verified")
def test_changed_profile_ids_patch_the_draft_and_publish_a_new_version(
    capsys: pytest.CaptureFixture[str], namespace: str
) -> None:
    _, first, _ = _run(capsys, *_profiles(namespace))
    code, second, transport = _run(capsys, *_profiles(namespace, "-rebuilt"))
    assert code == 0, second
    assert transport is not None
    methods = {method for method, _path in transport.writes()}
    assert methods == {"PATCH", "POST"}  # 作成はしない（下書きの修正と公開だけ）
    for key, name in _names(namespace).items():
        group = second["data"]["groups"][key]
        assert group["agent_id"] == first["data"]["groups"][key]["agent_id"]
        assert group["published_version"] == 2
        (agent,) = _agents(name)
        assert agent.data_scopes["rag"].profile_ids == [f"profile-{key}-rebuilt"]


@pytest.mark.usefixtures("auth", "verified")
def test_disabled_agent_is_enabled_without_a_new_version(
    capsys: pytest.CaptureFixture[str], namespace: str
) -> None:
    """有効 / 無効は版の項目ではないので、戻しても公開し直さない。"""
    _, first, _ = _run(capsys, *_profiles(namespace))
    agent_id = first["data"]["groups"]["A"]["agent_id"]
    runtime_repository.patch_agent(agent_id, AgentProfilePatch(enabled=False))
    code, second, transport = _run(capsys, *_profiles(namespace))
    assert code == 0, second
    assert transport is not None and transport.writes() == [("PATCH", f"/agents/{agent_id}")]
    assert second["data"]["groups"]["A"]["published_version"] == 1


@pytest.mark.usefixtures("auth")
def test_data_scope_refusal_is_reported_and_nothing_is_created(
    capsys: pytest.CaptureFixture[str], namespace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_added: dict[str, list[str]], *, user_uuid: str | None) -> None:
        raise ValueError("使えないプロファイルがあります。")

    monkeypatch.setattr(agent_router, "verify_editor_can_use", refuse)
    code, output, _ = _run(capsys, *_profiles(namespace))
    assert code == 1
    assert "HTTP 400" in output["error"]["message"]
    assert "使えないプロファイル" in output["error"]["message"]
    assert all(not _agents(name) for name in _names(namespace).values())


@pytest.mark.usefixtures("auth", "verified")
def test_publish_gate_refusal_keeps_the_draft_and_fails(
    capsys: pytest.CaptureFixture[str], namespace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """公開は画面と同じ評価の門を通る（下書きの評価が無ければ 409）。"""
    monkeypatch.setattr(get_settings(), "agent_publish_min_pass_rate", 0.8)
    code, output, _ = _run(capsys, *_profiles(namespace))
    assert code == 1 and "HTTP 409" in output["error"]["message"]
    (agent,) = _agents(_names(namespace)["A"])
    assert agent.published_version is None


def test_requires_agent_admin(
    auth: ProductionAuth,
    capsys: pytest.CaptureFixture[str],
    namespace: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth.user_with_permissions("agent-test-resources-viewer", [MENU_AGENTS])
    monkeypatch.setenv("SUITE_TEST_LOGIN_USER", "agent-test-resources-viewer")
    code, output, transport = _run(capsys, *_profiles(namespace))
    assert code == 1 and "Agent 管理" in output["error"]["message"]
    assert transport is not None and transport.writes() == []


@pytest.mark.usefixtures("auth", "verified")
@pytest.mark.parametrize("kind", ["duplicate", "personal"])
def test_conflicting_agents_stop_without_writes(
    capsys: pytest.CaptureFixture[str], namespace: str, kind: str
) -> None:
    name = _names(namespace)["A"]
    owners = [None, None] if kind == "duplicate" else ["user-1"]
    for owner in owners:
        runtime_repository.create_agent(
            AgentProfile(name=name, versioned=True, owner_user_uuid=owner)
        )
    code, output, transport = _run(capsys, *_profiles(namespace))
    assert code == 1
    assert ("複数" if kind == "duplicate" else "個人") in output["error"]["message"]
    assert transport is not None and transport.writes() == []


@pytest.mark.usefixtures("auth", "verified")
def test_rag_state_supplies_profiles_and_namespace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], namespace: str
) -> None:
    state = tmp_path / "rag-state.json"
    state.write_text(
        json.dumps(
            {
                "complete": True,
                "namespace": namespace,
                "groups": {
                    key: {
                        "kb_id": f"kb-{key}",
                        "profile_ids": [f"profile-{key}"],
                        "default_profile_id": f"profile-{key}",
                    }
                    for key in ("A", "B")
                },
                "documents": {},
            }
        ),
        encoding="utf-8",
    )
    code, output, _ = _run(capsys, "--rag-state", str(state))
    assert code == 0, output
    assert output["data"]["namespace"] == namespace
    assert all(len(_agents(name)) == 1 for name in _names(namespace).values())
    code, output, _ = _run(capsys, "--rag-state", str(state), "--namespace", "other")
    assert code == 1 and "state の名前の区分" in output["error"]["message"]


@pytest.mark.parametrize(
    ("response", "visible"),
    [
        (httpx.Response(500, json={"error_messages": ["private"]}), False),
        (httpx.Response(200, text="private"), False),
        (httpx.Response(409, json={"error_messages": ["公開の理由"]}), True),
    ],
    ids=["server-error", "invalid-body", "client-error-reason"],
)
def test_errors_show_only_client_error_reasons(response: httpx.Response, visible: bool) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda _: response))
    with pytest.raises(cli.ProvisionError) as caught:
        cli.AgentApi(client, API).call("GET", "/agents")
    assert ("private" in str(caught.value)) is False
    assert ("公開の理由" in str(caught.value)) is visible


@pytest.mark.parametrize(
    "overrides",
    [{"complete": False}, {"groups": {}}, {"groups": {"A": {"profile_ids": []}}}],
    ids=["incomplete", "no-groups", "no-profile"],
)
def test_rag_state_must_be_complete_and_have_profiles(
    tmp_path: Path, overrides: dict[str, Any]
) -> None:
    state = {
        "complete": True,
        "groups": {"A": {"profile_ids": ["p"], "default_profile_id": "p"}},
        **overrides,
    }
    path = tmp_path / "rag-state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(cli.ProvisionError):
        cli.read_rag_state(path)


@pytest.mark.parametrize(
    "values",
    [["A"], ["A=x", "A=y"], ["A="], ["A B=x"]],
    ids=["no-separator", "duplicate", "empty-id", "bad-group"],
)
def test_profile_arguments_are_validated(values: list[str]) -> None:
    with pytest.raises(cli.ProvisionError):
        cli.parse_profiles(values)


def test_definitions_reject_data_scopes_and_unknown_fields(tmp_path: Path) -> None:
    base = cli.read_definitions(cli.DEFAULT_DEFINITIONS)["A"]
    path = tmp_path / "agents.json"
    extras: list[dict[str, Any]] = [{"data_scopes": {}}, {"owner_user_uuid": "x"}]
    for extra in extras:
        path.write_text(
            json.dumps({"version": 1, "agents": {"A": {**base, **extra}}}), encoding="utf-8"
        )
        with pytest.raises(cli.ProvisionError, match="使えない項目"):
            cli.read_definitions(path)


def test_missing_group_in_profiles_is_rejected_before_calling_the_api(
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = AppTransport()
    assert cli.main(["--agent-api", API, "--profile", "A=p"], transport=transport) == 1
    assert "B" in json.loads(capsys.readouterr().out)["error"]["message"]
    assert transport.requests == []


def test_repository_definitions_are_valid() -> None:
    definitions = cli.read_definitions(cli.DEFAULT_DEFINITIONS)
    assert set(definitions) == {"A", "B"}
    for definition in definitions.values():
        assert definition["skill_ids"] == ["business-rag-research"]
        assert definition["enabled"] is True
