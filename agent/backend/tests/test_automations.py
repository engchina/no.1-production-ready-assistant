"""業務 Agent の自動実行（スケジュール。#784・#1799）。"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch
from security_support import ProductionAuth, client, enable_production_auth, login

from app.features.agent import automations
from app.features.agent.automations import (
    AutomationSchedule,
    ScheduleFrequency,
    automation_store,
    next_run_after,
    scheduler_tick,
)
from app.features.agent.control_plane_store import (
    FileItemStore,
    restore_control_plane,
    set_control_plane_store,
)
from app.features.agent.runtime import RunStatus, runtime_repository
from app.security.permissions import ADMIN, MENU_AUTOMATIONS
from app.security.service import set_security_service

# 2026-10-02（金）09:30 JST。
NOW = datetime(2026, 10, 2, 0, 30, tzinfo=UTC)
# 時間帯を明示した予定（既定の時間帯は UTC。#3274）。
TOKYO = "Asia/Tokyo"


def _jst(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC) - timedelta(hours=9)


@pytest.mark.parametrize(
    ("schedule", "expected"),
    [
        (
            AutomationSchedule(timezone=TOKYO, frequency=ScheduleFrequency.DAILY, time="10:00"),
            _jst(2, 10),
        ),
        (
            AutomationSchedule(timezone=TOKYO, frequency=ScheduleFrequency.DAILY, time="09:00"),
            _jst(3, 9),
        ),
        # 金曜の 09:30 の後の平日の 09:00 は月曜。
        (
            AutomationSchedule(timezone=TOKYO, frequency=ScheduleFrequency.WEEKDAYS, time="09:00"),
            _jst(5, 9),
        ),
        # 毎週の水曜（2）と土曜（5）。
        (
            AutomationSchedule(
                timezone=TOKYO, frequency=ScheduleFrequency.WEEKLY, time="08:00", weekdays=[2, 5]
            ),
            _jst(3, 8),
        ),
        (
            AutomationSchedule(timezone=TOKYO, frequency=ScheduleFrequency.HOURLY, minute=15),
            _jst(2, 10, 15),
        ),
        (
            AutomationSchedule(timezone=TOKYO, frequency=ScheduleFrequency.HOURLY, minute=45),
            _jst(2, 9, 45),
        ),
    ],
    ids=[
        "daily-today",
        "daily-tomorrow",
        "weekdays-monday",
        "weekly",
        "hourly-next",
        "hourly-same",
    ],
)
def test_next_run_is_computed_in_the_timezone(
    schedule: AutomationSchedule, expected: datetime
) -> None:
    assert next_run_after(schedule, NOW) == expected


def test_invalid_schedules_are_rejected() -> None:
    with pytest.raises(ValueError, match="HH:MM"):
        AutomationSchedule(time="25:00")
    with pytest.raises(ValueError, match="曜日"):
        AutomationSchedule(frequency=ScheduleFrequency.WEEKLY, weekdays=[])
    with pytest.raises(ValueError, match="タイムゾーン"):
        AutomationSchedule(timezone="Mars/Olympus")


@pytest.fixture
def store(monkeypatch: MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "items.json"
    set_control_plane_store(FileItemStore(path))
    automation_store.clear()
    # API の Run は作るだけで、このテストでは実行しない（実行は組み込み Runtime のテスト）。
    monkeypatch.setattr("app.features.agent.router._schedule_builtin_run", lambda _run: None)
    try:
        yield path
    finally:
        automation_store.clear()
        set_control_plane_store(None)
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id
                for run_id, run in repository._runs.items()
                if run.metadata.get("automation_id")
            ]:
                repository._runs.pop(run_id)


BODY: dict[str, Any] = {
    "agent_id": "default",
    "name": "毎朝の売上の要約",
    "goal": "昨日の売上を要約してください。",
    "schedule": {"frequency": "daily", "time": "09:00", "timezone": "Asia/Tokyo"},
}


def _due(automation_id: str) -> None:
    """次回を過去にして、すぐに実行される状態にする。"""

    def mutate(item: Any) -> None:
        item.next_run_at = datetime.now(UTC) - timedelta(minutes=1)

    automation_store.change(automation_id, mutate)


def test_scheduled_automation_creates_runs_and_skips_while_the_previous_runs(store: Path) -> None:
    del store
    created = client.post("/api/automations", json=BODY)
    assert created.status_code == 200, created.text
    automation = created.json()["data"]
    assert automation["next_run_at"] is not None
    _due(automation["id"])

    [fired] = scheduler_tick()

    assert fired.result == "created"
    run = runtime_repository.get_run(str(fired.run_id))
    assert run.goal == "昨日の売上を要約してください。"
    assert run.metadata["automation_id"] == automation["id"]
    assert run.metadata["automation_trigger"] == "schedule"
    current = automation_store.get(automation["id"])
    assert current.last_result == "created" and current.last_run_id == run.id
    assert current.next_run_at is not None and current.next_run_at > datetime.now(UTC)

    # 前回の Run（queued のまま）が終わっていなければ、次の回は飛ばす。
    _due(automation["id"])
    [skipped] = scheduler_tick()
    assert (skipped.result, skipped.run_id) == ("skipped", None)
    # 終われば次の回は実行する。
    runtime_repository.cancel_run(run.id)
    _due(automation["id"])
    [again] = scheduler_tick()
    assert again.result == "created"

    detail = client.get(f"/api/automations/{automation['id']}").json()["data"]
    assert [item["run_id"] for item in detail["runs"]] == [again.run_id, run.id]
    assert detail["runs"][1]["status"] == RunStatus.CANCELLED


def _set_run_status(run_id: str, status: RunStatus) -> None:
    """Run の状態を変える（実行は組み込み Runtime のテスト。ここでは「前回」の元だけを見る）。"""
    repository: Any = runtime_repository
    with repository._lock:  # noqa: SLF001 - テストで Run の状態を進める
        repository._runs[run_id].status = status


@pytest.mark.parametrize(
    "status",
    [
        RunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    ],
    ids=["running", "waiting-approval", "completed", "failed", "cancelled"],
)
def test_last_run_status_follows_the_run(store: Path, status: RunStatus) -> None:
    """「前回」は Run を作ったことではなく、前回の Run の今の状態を返す（#1812）。"""
    del store
    automation = client.post("/api/automations", json=BODY).json()["data"]
    assert automation["last_run_status"] is None
    fired = client.post(f"/api/automations/{automation['id']}/run").json()["data"]
    run_id = fired["run_id"]

    _set_run_status(run_id, status)

    [listed] = client.get("/api/automations").json()["data"]["automations"]
    detail = client.get(f"/api/automations/{automation['id']}").json()["data"]["automation"]
    for item in (listed, detail):
        assert (item["last_result"], item["last_run_id"]) == ("created", run_id)
        assert item["last_run_status"] == status
    # 表示のときに引く値で、自動実行の定義には保存しない。
    assert "last_run_status" not in automation_store.get(automation["id"]).model_dump()


def test_last_run_status_is_empty_when_the_run_is_gone_or_not_started(
    store: Path, monkeypatch: MonkeyPatch
) -> None:
    """Run を作れなかった・Run の記録が無いときは、状態を返さない（理由は last_message。#1812）。"""
    del store
    automation = client.post("/api/automations", json=BODY).json()["data"]
    fired = client.post(f"/api/automations/{automation['id']}/run").json()["data"]
    repository: Any = runtime_repository
    with repository._lock:  # noqa: SLF001 - 保存期間を過ぎて Run が消えた状態にする
        repository._runs.pop(fired["run_id"])
    [gone] = client.get("/api/automations").json()["data"]["automations"]
    assert (gone["last_result"], gone["last_run_status"]) == ("created", None)

    monkeypatch.setattr(automations, "owner_check", lambda _item: "利用者が無効です。")
    client.post(f"/api/automations/{automation['id']}/run")
    [refused] = client.get("/api/automations").json()["data"]["automations"]
    assert refused["last_result"] == "failed_to_start"
    assert refused["last_message"] == "利用者が無効です。"


def test_list_looks_up_run_statuses_at_once(store: Path, monkeypatch: MonkeyPatch) -> None:
    """一覧は前回の Run の状態を 1 回にまとめて引く（Run を 1 件ずつ取りに行かない。#1812）。"""
    del store
    for index in range(3):
        created = client.post("/api/automations", json={**BODY, "name": f"自動実行 {index}"})
        client.post(f"/api/automations/{created.json()['data']['id']}/run")
    repository: Any = runtime_repository
    lookups: list[list[str]] = []
    original = repository.run_statuses

    def counted(run_ids: Any) -> Any:
        lookups.append(list(run_ids))
        return original(run_ids)

    monkeypatch.setattr(repository, "run_statuses", counted)
    monkeypatch.setattr(
        repository, "get_run", lambda _run_id: pytest.fail("get_run を 1 件ずつ呼んだ")
    )

    items = client.get("/api/automations").json()["data"]["automations"]

    assert len(lookups) == 1 and len(lookups[0]) == 3
    assert {item["last_run_status"] for item in items} == {"queued"}


def test_disabled_automations_are_not_scheduled(store: Path) -> None:
    del store
    disabled = client.post("/api/automations", json={**BODY, "enabled": False}).json()["data"]
    assert disabled["next_run_at"] is None
    assert scheduler_tick() == []


def test_schedule_is_the_only_trigger(store: Path) -> None:
    """トリガーはスケジュールだけ（#1799）。省略したスケジュールは既定（毎日 09:00）にする。"""
    created = client.post("/api/automations", json={**BODY, "schedule": None})
    assert created.status_code == 422
    defaulted = client.post(
        "/api/automations", json={key: value for key, value in BODY.items() if key != "schedule"}
    )
    assert defaulted.status_code == 200, defaulted.text
    automation = defaulted.json()["data"]
    assert automation["schedule"]["frequency"] == "daily"
    assert automation["schedule"]["time"] == "09:00"
    assert automation["next_run_at"] is not None
    # トリガーの種類・Webhook の秘密は応答にも保存先にも無い。
    stored = json.loads(store.read_text())["automation"][automation["id"]]
    for document in (automation, stored):
        assert not {"trigger", "webhook_token_hash", "webhook_token_prefix"} & set(document)
    # Webhook の受け口と秘密の発行の API は無い。
    assert client.post(f"/api/hooks/{automation['id']}", json={}).status_code == 404
    assert client.post(f"/api/automations/{automation['id']}/webhook-token").status_code == 404


def test_owner_that_cannot_run_the_agent_does_not_start(
    store: Path, monkeypatch: MonkeyPatch
) -> None:
    del store
    automation = client.post("/api/automations", json=BODY).json()["data"]
    monkeypatch.setattr(automations, "owner_check", lambda _item: "利用者が無効です。")
    fired = client.post(f"/api/automations/{automation['id']}/run").json()["data"]
    assert (fired["result"], fired["run_id"], fired["message"]) == (
        "failed_to_start",
        None,
        "利用者が無効です。",
    )
    assert automation_store.get(automation["id"]).last_result == "failed_to_start"


def test_manual_run(store: Path) -> None:
    del store
    automation = client.post("/api/automations", json=BODY).json()["data"]
    fired = client.post(f"/api/automations/{automation['id']}/run")
    assert fired.status_code == 200, fired.text
    run = runtime_repository.get_run(fired.json()["data"]["run_id"])
    assert run.metadata["automation_trigger"] == "manual"


def test_automations_are_restored_without_catching_up_missed_runs(store: Path) -> None:
    del store
    automation = client.post("/api/automations", json=BODY).json()["data"]
    _due(automation["id"])

    automation_store.clear()
    restored = restore_control_plane()

    assert restored["automation"] == 1
    current = automation_store.get(automation["id"])
    # 停止中に過ぎた回は実行せず、次回へ送る。
    assert current.next_run_at is not None and current.next_run_at > datetime.now(UTC)
    assert scheduler_tick() == []


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def test_automation_permissions(auth: ProductionAuth, store: Path) -> None:
    del store
    auth.user_with_permissions("viewer-784", [MENU_AUTOMATIONS], agent_ids=["default"])
    viewer = login("viewer-784")
    assert client.post("/api/automations", json=BODY, headers=viewer).status_code == 403
    admin = auth.user_with_permissions("admin-784", [ADMIN])
    created = client.post("/api/automations", json=BODY, headers=login("admin-784"))
    assert created.status_code == 200, created.text
    automation = created.json()["data"]
    assert automation["run_as_user_uuid"] == admin.user_uuid
    assert [
        item["id"]
        for item in client.get("/api/automations", headers=viewer).json()["data"]["automations"]
    ] == [automation["id"]]
    assert (
        client.post(f"/api/automations/{automation['id']}/run", headers=viewer).status_code == 403
    )
    # 利用できない業務 Agent の自動実行は見えない。
    auth.user_with_permissions("other-784", [MENU_AUTOMATIONS], agent_ids=["sales"])
    other = login("other-784")
    assert client.get("/api/automations", headers=other).json()["data"]["automations"] == []
    assert client.get(f"/api/automations/{automation['id']}", headers=other).status_code == 404


def test_unpublished_agents_cannot_be_automated(store: Path) -> None:
    """自動実行は利用者の Run なので、公開した版の無い業務 Agent は選べない（#792）。"""
    del store
    draft_id = "automation-792-draft"
    created = client.post(
        "/api/agents", json={"id": draft_id, "name": "下書きの Agent", "instructions": "答える。"}
    )
    assert created.status_code == 200, created.text
    try:
        refused = client.post("/api/automations", json={**BODY, "agent_id": draft_id})
        assert refused.status_code == 422
        assert "公開していない業務 Agent" in refused.text
        automation = client.post("/api/automations", json=BODY).json()["data"]
        moved = client.put(
            f"/api/automations/{automation['id']}", json={**BODY, "agent_id": draft_id}
        )
        assert moved.status_code == 422
    finally:
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(draft_id)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("+00:00", "2026-10-02T09:00:00+00:00"),
        ("+05:30", "2026-10-02T03:30:00+00:00"),
        ("-03:30", "2026-10-02T12:30:00+00:00"),
    ],
)
def test_browser_offset_schedule(name: str, expected: str) -> None:
    from app.features.agent.automations import AutomationSchedule, next_run_after

    schedule = AutomationSchedule(time="09:00", timezone=name)
    assert next_run_after(schedule, datetime(2026, 10, 2, tzinfo=UTC)).isoformat() == expected
