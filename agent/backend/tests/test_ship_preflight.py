"""出荷前点検（`app.cli.ship_preflight`。#64）の判定のテスト。

稼働中のサービスの代わりに、HTTP の応答を返す関数を渡す。外部への通信はしない。
証拠の無い項目を pass にしないこと、拒否されるべき呼び出しが通ったら blocker にすること、
秘密の値を出力に出さないことを確かめる。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pr_system_settings.auth.errors import SecurityApiError
from pr_system_settings.auth.service_token import verify_service_token
from pytest import MonkeyPatch

from app.cli import ship_preflight
from app.cli.ship_preflight import (
    BLOCKER,
    ERROR,
    MANUAL,
    NOT_RUN,
    PASS,
    WARNING,
    Check,
    Report,
    run_preflight,
)
from app.settings import Settings, get_settings

SECRET = "preflight-test-secret-0123456789abcdef"  # nosec B105 - テスト用
RAG = "http://rag.test/api/mcp"
NL2SQL = "http://nl2sql.test/api/mcp"
AGENT = "http://agent.test"


def _settings(**update: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "production",
        "auth_mode": "authenticated",
        "app_service_token_secret": SECRET,
        "agent_external_rag_mcp_url": RAG,
        "agent_external_nl2sql_mcp_url": NL2SQL,
        "agent_runtime_repository_backend": "oracle_checkpoint",
        "agent_tool_grounding_required": True,
        "agent_final_validation_enabled": True,
    }
    values.update(update)
    return get_settings().model_copy(update=values)


class _Services:
    """RAG / NL2SQL / Agent の health・ready・MCP の認証の振る舞いを真似る。"""

    def __init__(self, *, secret: str = SECRET) -> None:
        self.secret = secret
        self.ready_status = 200
        self.down: set[str] = set()
        self.accept_without_token = False
        self.requests: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        self.requests.append((method, url))
        host = httpx.URL(url).host
        if host in self.down:
            raise httpx.ConnectError("refused")
        if url.endswith("/health"):
            return httpx.Response(200, json={"data": {"status": "ok"}})
        if url.endswith("/ready"):
            checks = {
                "oracle": "ok",
                "enterprise_ai": "ok" if self.ready_status == 200 else "missing",
            }
            return httpx.Response(self.ready_status, json={"data": {"checks": checks}})
        assert method == "POST" and url.endswith("/mcp")
        assert kwargs["json"]["method"] == "tools/list"
        authorization = kwargs["headers"].get("authorization", "")
        if not authorization:
            return httpx.Response(200 if self.accept_without_token else 401)
        try:
            verify_service_token(
                self.secret, authorization.removeprefix("Bearer "), audience=host.split(".")[0]
            )
        except SecurityApiError:
            return httpx.Response(401)
        # 署名は正しいが、利用者が存在しない。
        return httpx.Response(403)


def _by_name(report: Report) -> dict[str, Check]:
    return {f"{check.area}/{check.name}": check for check in report.checks}


def test_healthy_configuration_is_incomplete_without_evidence() -> None:
    """構成と稼働がすべて正しくても、契約・スタブ検証を実行しなければ READY にしない。"""
    services = _Services()

    report = run_preflight(_settings(), agent_url=AGENT, http=services)

    checks = _by_name(report)
    assert report.verdict == "INCOMPLETE"
    assert checks["contract/tools_list"].status == NOT_RUN
    assert checks["fault_injection/stub_tests"].status == NOT_RUN
    assert checks["operations/stop_and_recovery_drill"].status == MANUAL
    for name in ("rag", "nl2sql"):
        for probe in ("no_token", "wrong_audience", "unknown_subject"):
            assert checks[f"auth/{name}.{probe}"].status == PASS
    assert {check.status for check in report.checks} <= {PASS, NOT_RUN, MANUAL}
    # tools/call は送らない（読み取りだけ）。
    assert all(method in {"GET", "POST"} for method, _ in services.requests)


def test_ready_only_when_every_area_has_evidence(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        ship_preflight,
        "check_contract",
        lambda _user: [Check("contract", "rag", PASS, "ok")],
    )
    monkeypatch.setattr(
        ship_preflight,
        "check_stub_tests",
        lambda _run: [Check("fault_injection", "stub_tests", PASS, "22 passed")],
    )

    report = run_preflight(_settings(), agent_url=AGENT, http=_Services())

    assert report.verdict == "READY"


def test_secret_mismatch_is_a_blocker_with_the_remedy() -> None:
    services = _Services(secret="another-secret-0123456789abcdefghij")

    report = run_preflight(_settings(), agent_url=AGENT, http=services)

    check = _by_name(report)["auth/rag.unknown_subject"]
    assert report.verdict == "NOT_READY"
    assert check.status == BLOCKER
    assert "PLATFORM_SERVICE_TOKEN_SECRET" in check.remedy


def test_call_without_token_that_is_accepted_is_a_blocker() -> None:
    services = _Services()
    services.accept_without_token = True

    report = run_preflight(_settings(), agent_url=AGENT, http=services)

    check = _by_name(report)["auth/rag.no_token"]
    assert check.status == BLOCKER
    assert "期待は 401、実際は 200" in check.detail


@pytest.mark.parametrize(
    ("update", "name"),
    [
        ({"environment": "local", "auth_mode": "local"}, "config/auth_mode"),
        ({"app_service_token_secret": "short"}, "config/service_token_secret"),
        ({"agent_external_rag_mcp_url": ""}, "config/rag_mcp_url"),
        ({"agent_runtime_repository_backend": "memory"}, "config/run_store"),
    ],
)
def test_unsafe_configuration_is_a_blocker(update: dict[str, Any], name: str) -> None:
    report = run_preflight(_settings(**update), agent_url=AGENT, http=_Services())

    check = _by_name(report)[name]
    assert check.status == BLOCKER
    assert check.remedy
    assert report.verdict == "NOT_READY"


def test_disabled_grounding_is_a_warning() -> None:
    report = run_preflight(
        _settings(agent_tool_grounding_required=False), agent_url=AGENT, http=_Services()
    )

    assert _by_name(report)["config/tool_grounding_required"].status == WARNING


def test_service_down_and_not_ready_are_blockers() -> None:
    services = _Services()
    services.down.add("nl2sql.test")
    services.ready_status = 503

    report = run_preflight(_settings(), agent_url=AGENT, http=services)

    checks = _by_name(report)
    assert checks["health/nl2sql"].status == BLOCKER
    assert "ConnectError" in checks["health/nl2sql"].detail
    assert checks["ready/rag"].status == BLOCKER
    assert "enterprise_ai=missing" in checks["ready/rag"].detail
    # 届かない呼び先の認証は確かめられない（pass にしない）。
    assert checks["auth/nl2sql.no_token"].status == ERROR


def test_failure_of_a_check_itself_is_an_error(monkeypatch: MonkeyPatch) -> None:
    def broken(_settings: Settings) -> list[Check]:
        raise RuntimeError("broken")

    monkeypatch.setattr(ship_preflight, "check_config", broken)

    report = run_preflight(_settings(), agent_url=AGENT, http=_Services())

    assert any(check.status == ERROR for check in report.checks)
    assert report.verdict == "NOT_READY"


def test_output_never_contains_the_secret(
    monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    services = _Services(secret="another-secret-0123456789abcdefghij")
    monkeypatch.setattr(ship_preflight, "get_settings", _settings)
    monkeypatch.setattr(
        ship_preflight,
        "run_preflight",
        lambda settings, **kwargs: run_preflight(settings, **{**kwargs, "http": services}),
    )

    text_exit = ship_preflight.main(["--agent-url", AGENT])
    text = capsys.readouterr().out
    json_exit = ship_preflight.main(["--agent-url", AGENT, "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert text_exit == json_exit == 1
    assert SECRET not in text and SECRET not in json.dumps(payload)
    assert "verdict: NOT_READY" in text
    assert payload["verdict"] == "NOT_READY"
    assert payload["config_fingerprint"] and payload["commit"] and payload["checked_at"]
