"""利用状況（Run のモデル利用量の集計。#772）。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pytest import MonkeyPatch
from security_support import ProductionAuth, client, enable_production_auth, login

from app.features.agent.run_facts import fact_from_run
from app.features.agent.runtime import RunState, RunStatus, RunUsage, runtime_repository
from app.features.agent.usage import build_usage_report, resolve_timezone
from app.security.domain import LOCAL_DEBUG_USER_UUID
from app.security.permissions import MENU_RUNS, MENU_USAGE
from app.security.service import set_security_service

TOKYO = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)  # 東京の 10/2 12:00
ALICE = "aaaaaaaa-0000-0000-0000-000000000001"
BOB = "bbbbbbbb-0000-0000-0000-000000000002"


def _run(
    run_id: str,
    created_at: datetime,
    *,
    agent_id: str = "default",
    user: str | None = ALICE,
    tokens: tuple[int, int] | None = (100, 20),
    model: str = "model-a",
    requests: int = 1,
) -> RunState:
    usage = (
        None
        if tokens is None
        else RunUsage(
            model=model,
            requests=requests,
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            total_tokens=sum(tokens),
        )
    )
    return RunState(
        id=run_id,
        goal="質問",
        agent_id=agent_id,
        runtime_id="builtin",
        status=RunStatus.COMPLETED,
        created_by_user_uuid=user,
        usage=usage,
        created_at=created_at,
        updated_at=created_at,
    )


def _report(runs: list[RunState], days: int = 7) -> Any:
    return build_usage_report(
        [fact_from_run(run) for run in runs],
        days=days,
        now=NOW,
        tz=TOKYO,
        agent_names={"default": "資料・データ横断アシスタント", "sales": "営業の Agent"},
        user_names=lambda uuids: {
            uuid: {ALICE: "Alice"}.get(uuid, "") for uuid in uuids if uuid == ALICE
        },
    )


def test_report_sums_the_period_and_compares_with_the_previous_period() -> None:
    runs = [
        _run("r1", NOW - timedelta(hours=1)),
        _run("r2", NOW - timedelta(days=2), agent_id="sales", user=BOB, tokens=(300, 100)),
        # #772 より前の Run（利用量なし）は Run 数だけ数える。
        _run("r3", NOW - timedelta(days=3), tokens=None),
        # 直前の 7 日（比較用）。
        _run("r4", NOW - timedelta(days=10), tokens=(50, 10)),
        # 2 期間より前と、未来の Run は数えない。
        _run("r5", NOW - timedelta(days=30)),
        _run("r6", NOW + timedelta(hours=1)),
    ]

    report = _report(runs)

    assert report.totals.model_dump() == {
        "runs": 3,
        "runs_with_usage": 2,
        "requests": 2,
        "input_tokens": 400,
        "output_tokens": 120,
        "total_tokens": 520,
    }
    assert (report.previous.runs, report.previous.total_tokens) == (1, 60)
    # 業務 Agent・利用者・モデルは token の多い順。
    assert [(item.agent_id, item.agent_name, item.total_tokens) for item in report.by_agent] == [
        ("sales", "営業の Agent", 400),
        ("default", "資料・データ横断アシスタント", 120),
    ]
    assert [(item.user_uuid, item.display_name, item.runs) for item in report.by_user] == [
        (BOB, "", 1),
        (ALICE, "Alice", 2),
    ]
    assert [(item.model, item.runs) for item in report.by_model] == [("model-a", 2)]


@pytest.mark.parametrize("model", ["model-a", ""])
@pytest.mark.parametrize("tokens,requests", [((0, 0), 0), ((100, 20), 1)])
def test_model_breakdown_keeps_recorded_zero_usage_and_unknown_model_names(
    model: str, tokens: tuple[int, int], requests: int
) -> None:
    report = _report(
        [
            _run("unrecorded", NOW, tokens=None),
            _run("recorded", NOW, tokens=tokens, model=model, requests=requests),
        ]
    )

    assert (report.totals.runs, report.totals.runs_with_usage) == (2, 1)
    assert [
        (item.model, item.runs, item.requests, item.total_tokens) for item in report.by_model
    ] == [(model, 1, requests, sum(tokens))]
    assert report.by_agent[0].runs == report.by_user[0].runs == 2
    assert sum(item.runs for item in report.by_day) == 2


def test_model_breakdown_is_empty_when_all_runs_have_no_usage_record() -> None:
    report = _report([_run("unrecorded", NOW, tokens=None)])

    assert report.by_model == []
    assert (report.totals.runs, report.totals.runs_with_usage) == (1, 0)
    assert report.by_agent[0].runs == report.by_user[0].runs == 1
    assert sum(item.runs for item in report.by_day) == 1


def test_days_are_split_in_the_viewer_timezone_and_every_day_is_listed() -> None:
    # UTC の 10/1 16:00 は東京の 10/2 01:00（今日）。UTC の 10/1 14:00 は東京の 10/1 23:00。
    runs = [
        _run("today", datetime(2026, 10, 1, 16, 0, tzinfo=UTC)),
        _run("yesterday", datetime(2026, 10, 1, 14, 0, tzinfo=UTC)),
    ]

    report = _report(runs)

    assert [item.day for item in report.by_day] == [
        date(2026, 9, 26) + timedelta(days=offset) for offset in range(7)
    ]
    by_day = {item.day: item.runs for item in report.by_day}
    assert by_day[date(2026, 10, 2)] == 1
    assert by_day[date(2026, 10, 1)] == 1
    assert sum(by_day.values()) == 2
    # 期間の始まりは、利用者のタイムゾーンの 6 日前の 0 時。
    assert report.since == datetime(2026, 9, 26, tzinfo=TOKYO)
    assert report.timezone == "Asia/Tokyo"


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="タイムゾーン"):
        resolve_timezone("Mars/Olympus")


@pytest.fixture
def seeded_runs() -> Iterator[None]:
    repository: Any = runtime_repository
    now = datetime.now(UTC)
    runs = [
        _run("usage-772-a", now - timedelta(minutes=5), user=LOCAL_DEBUG_USER_UUID),
        _run("usage-772-b", now - timedelta(minutes=6), agent_id="sales-772", user=BOB),
    ]
    with repository._lock:  # noqa: SLF001 - テスト用に Run を直接置く
        for run in runs:
            repository._runs[run.id] = run
    try:
        yield
    finally:
        with repository._lock:  # noqa: SLF001
            for run in runs:
                repository._runs.pop(run.id, None)


def test_usage_api_returns_the_report(seeded_runs: None) -> None:
    del seeded_runs
    response = client.get("/api/usage", params={"days": 7, "timezone": "Asia/Tokyo"})

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["days"] == 7
    assert len(data["by_day"]) == 7
    users = {item["user_uuid"]: item["display_name"] for item in data["by_user"]}
    # local のローカル利用者は名前で出す。
    assert users[LOCAL_DEBUG_USER_UUID] == "ローカル利用者"
    assert {"default", "sales-772"} <= {item["agent_id"] for item in data["by_agent"]}


def test_usage_api_rejects_unknown_period_and_timezone() -> None:
    assert client.get("/api/usage", params={"days": 14}).status_code == 422
    # 90 日を超える期間（#794）。
    for days in (180, 365):
        response = client.get("/api/usage", params={"days": days})
        assert response.status_code == 200, response.text
        assert len(response.json()["data"]["by_day"]) == days
        # memory の構成はメモリの Run を集計する。
        assert response.json()["data"]["source"] == "memory"
    bad_tz = client.get("/api/usage", params={"timezone": "Mars/Olympus"})
    assert bad_tz.status_code == 422
    assert "タイムゾーン" in bad_tz.text


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def test_usage_needs_the_menu_permission_and_is_limited_to_allowed_agents(
    auth: ProductionAuth, seeded_runs: None
) -> None:
    del seeded_runs
    auth.user_with_permissions("runs-only-772", [MENU_RUNS], agent_ids=["default"])
    denied = client.get("/api/usage", headers=login("runs-only-772"))
    assert denied.status_code == 403

    bob = auth.user_with_permissions("usage-772", [MENU_USAGE], agent_ids=["sales-772"])
    repository: Any = runtime_repository
    with repository._lock:  # noqa: SLF001 - 作成者を作った利用者に合わせる
        repository._runs["usage-772-b"].created_by_user_uuid = bob.user_uuid  # noqa: SLF001
    allowed = client.get("/api/usage", headers=login("usage-772"))

    assert allowed.status_code == 200, allowed.text
    data = allowed.json()["data"]
    # 利用できる業務 Agent（sales-772）の Run だけを集計する。
    assert [item["agent_id"] for item in data["by_agent"]] == ["sales-772"]
    assert [(item["user_uuid"], item["display_name"]) for item in data["by_user"]] == [
        (bob.user_uuid, "usage-772")
    ]


@pytest.mark.parametrize(
    "name, minutes",
    [("+00:00", 0), ("+09:00", 540), ("+05:30", 330), ("-03:30", -210), ("+23:59", 1439)],
)
def test_browser_offset_timezone(name: str, minutes: int) -> None:
    tz = resolve_timezone(name)
    assert NOW.astimezone(tz).utcoffset() == timedelta(minutes=minutes)
    report = build_usage_report(
        [fact_from_run(_run("boundary", datetime(2026, 10, 1, 23, 30, tzinfo=UTC)))],
        days=7,
        now=NOW,
        tz=tz,
        agent_names={},
        user_names=lambda _: {},
    )
    assert report.timezone == name
    assert (
        next(day.day for day in report.by_day if day.runs)
        == datetime(2026, 10, 1, 23, 30, tzinfo=UTC).astimezone(tz).date()
    )


@pytest.mark.parametrize("name", ["+24:00", "+09:60", "09:00", "+9:00", "Invalid/Zone"])
def test_invalid_browser_timezone(name: str) -> None:
    with pytest.raises(ValueError):
        resolve_timezone(name)


@pytest.mark.parametrize("name", ["+00:00", "+09:00", "+05:30", "-03:30"])
def test_usage_api_accepts_browser_offset(name: str) -> None:
    response = client.get("/api/usage", params={"timezone": name, "days": 7})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["timezone"] == name


def test_omitted_timezone_uses_the_common_setting(monkeypatch: MonkeyPatch) -> None:
    """時間帯を省いたら（API / MCP）、共通の設定の時間帯（#3255）で日を区切る。"""
    from app.features.agent.automations import AutomationSchedule
    from app.settings import get_settings

    assert resolve_timezone("") == ZoneInfo("UTC")  # 既定は UTC（#3274）。
    monkeypatch.setattr(get_settings(), "timezone", "Asia/Tokyo")
    assert resolve_timezone(None) == ZoneInfo("Asia/Tokyo")
    assert resolve_timezone("  ") == ZoneInfo("Asia/Tokyo")
    # 渡した時間帯は設定より優先する。
    assert resolve_timezone("UTC") == ZoneInfo("UTC")
    assert AutomationSchedule().timezone == "Asia/Tokyo"
