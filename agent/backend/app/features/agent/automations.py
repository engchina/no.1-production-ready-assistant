"""業務 Agent の自動実行（スケジュール。#784・#1799）。

- 自動実行は業務 Agent・指示（Run の目標）・スケジュールを持ち、作った利用者として Run を作る
  （RAG / NL2SQL の MCP はこの利用者として呼ぶ）。`metadata.automation_id` で自動実行の Run と
  分かる。
- スケジュール: 毎日 / 平日 / 毎週（曜日）/ 毎時（分）。時刻はタイムゾーン
  （IANA 名または固定時差）で決める。
  予定の時刻を過ぎたら Run を作り、次回を計算し直す。停止中に過ぎた回は実行しない
  （起動時に次回へ送る）。
- 画面の「今すぐ実行」でも Run を作る（Run の `metadata.automation_trigger` は `manual`）。
- 無人の実行なので、承認が要るツールは呼ばない（組み込み Runtime が承認待ちにせずに却下して
  モデルへ理由を返す。#1813）。編集画面は使えないツールを `approval_required_tools` で警告する。
- 「前回」は、保存した `last_result`（Run を作った時点の結果）ではなく、前回の Run の今の状態を
  表示のときに Run の保存先から引く（`with_last_run_status`。#1812）。
- 前回の Run が終わっていなければ、その回は飛ばす（Run を積み上げない）。
- スケジューラは API のプロセス（gunicorn は 1 worker）で回す。dispatcher のモードでは
  queued の Run を作るだけで、実行は dispatcher が行う（重複して起動しない）。
- 定義は Control Plane の定義と同じ保存先（`AGENT_CONTROL_PLANE_ITEMS`。kind `automation`。#764）。
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from app.features.agent import builtin_runtime
from app.features.agent.runtime import (
    BUILTIN_RUNTIME_ID,
    RunCreateRequest,
    RunState,
    RunStatus,
    agent_unavailable_reason,
)
from app.features.agent.timezones import default_timezone_name, resolve_timezone, timezone_name
from app.settings import get_settings

logger = logging.getLogger(__name__)

AUTOMATION_GOAL_MAX_CHARS = 4000
# スケジューラの間隔（秒）。テストは tick を直接呼ぶ。
SCHEDULER_INTERVAL_SECONDS = 30.0
_TIME_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_ACTIVE_RUN_STATUSES = {
    RunStatus.QUEUED,
    RunStatus.RUNNING,
    RunStatus.WAITING_APPROVAL,
    RunStatus.WAITING_CONFIRMATION,
}


def _now() -> datetime:
    return datetime.now(UTC)


class ScheduleFrequency(StrEnum):
    DAILY = "daily"
    WEEKDAYS = "weekdays"
    WEEKLY = "weekly"
    HOURLY = "hourly"


class AutomationSchedule(BaseModel):
    frequency: ScheduleFrequency = ScheduleFrequency.DAILY
    # 毎日 / 平日 / 毎週の時刻（HH:MM）。
    time: str = "09:00"
    # 毎週の曜日（0 = 月曜 … 6 = 日曜）。
    weekdays: list[int] = Field(default_factory=lambda: [0])
    # 毎時の分。
    minute: int = Field(default=0, ge=0, le=59)
    # 省いたときは共通の設定の時間帯（PLATFORM_TIMEZONE。#3255）。
    timezone: str = Field(default_factory=default_timezone_name)

    @field_validator("time")
    @classmethod
    def _time(cls, value: str) -> str:
        if not _TIME_PATTERN.match(value.strip()):
            raise ValueError("時刻は HH:MM（00:00〜23:59）で入力してください。")
        return value.strip()

    @field_validator("weekdays")
    @classmethod
    def _weekdays(cls, value: list[int]) -> list[int]:
        days = sorted(set(value))
        if any(day < 0 or day > 6 for day in days):
            raise ValueError("曜日は 0（月）〜 6（日）です。")
        return days

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        return timezone_name(resolve_timezone(value))

    @model_validator(mode="after")
    def _weekly_needs_days(self) -> AutomationSchedule:
        if self.frequency == ScheduleFrequency.WEEKLY and not self.weekdays:
            raise ValueError("毎週のときは曜日を 1 つ以上選んでください。")
        return self


def next_run_after(schedule: AutomationSchedule, after: datetime) -> datetime:
    """`after` より後の最初の予定の時刻（UTC）。"""
    tz = resolve_timezone(schedule.timezone)
    local = after.astimezone(tz)
    if schedule.frequency == ScheduleFrequency.HOURLY:
        candidate = local.replace(minute=schedule.minute, second=0, microsecond=0)
        if candidate <= local:
            # 夏時間の切り替えでも 1 時間後になるよう、UTC で足してから戻す。
            candidate = (candidate.astimezone(UTC) + timedelta(hours=1)).astimezone(tz)
            candidate = candidate.replace(minute=schedule.minute, second=0, microsecond=0)
        return candidate.astimezone(UTC)
    hour, minute = (int(part) for part in schedule.time.split(":"))
    for offset in range(0, 15):
        day = local.date() + timedelta(days=offset)
        if schedule.frequency == ScheduleFrequency.WEEKDAYS and day.weekday() >= 5:
            continue
        if (
            schedule.frequency == ScheduleFrequency.WEEKLY
            and day.weekday() not in schedule.weekdays
        ):
            continue
        candidate = datetime.combine(day, time(hour, minute), tzinfo=tz)
        if candidate > local:
            return candidate.astimezone(UTC)
    raise ValueError("次回の実行日時を決められません。")  # pragma: no cover - 曜日は検証済み


class AutomationInput(BaseModel):
    agent_id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    goal: str = Field(min_length=1, max_length=AUTOMATION_GOAL_MAX_CHARS)
    enabled: bool = True
    schedule: AutomationSchedule = Field(default_factory=AutomationSchedule)

    @field_validator("name", "goal")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("空白だけにはできません。")
        return value.strip()


class Automation(AutomationInput):
    id: str = Field(default_factory=lambda: f"auto_{uuid4().hex[:16]}")
    # 実行する利用者（作った利用者）。
    run_as_user_uuid: str
    created_by_user_uuid: str
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_run_id: str | None = None
    last_trigger: str | None = None
    # 前回の結果（Run の状態か `skipped` / `failed_to_start`）と、その理由。
    last_result: str | None = None
    last_message: str | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class AutomationView(Automation):
    """画面・API に返す自動実行（#1812）。

    保存した定義に、表示のときに引く値を足す（足した値は保存しない）。
    """

    # 前回の Run（`last_run_id`）の今の状態（完了・失敗・承認待ちなど）。Run の記録が無ければ None。
    last_run_status: RunStatus | None = None


def with_last_run_status(items: list[Automation]) -> list[AutomationView]:
    """前回の Run の状態を足す（#1812）。

    Run の状態は Run の保存先（完了・失敗を書くのは組み込み Runtime と dispatcher）が正本なので、
    自動実行の定義に写して保存し直さず、表示のときに引く。一覧の件数の Run を 1 件ずつ取りに
    行かず、`run_statuses` で 1 回にまとめて引く。
    """
    from app.features.agent.runtime import runtime_repository

    run_ids = [item.last_run_id for item in items if item.last_run_id]
    statuses = runtime_repository.run_statuses(run_ids) if run_ids else {}
    return [
        AutomationView.model_validate(
            {
                **item.model_dump(),
                "last_run_status": statuses.get(item.last_run_id) if item.last_run_id else None,
            }
        )
        for item in items
    ]


def automation_view(item: Automation) -> AutomationView:
    return with_last_run_status([item])[0]


class AutomationsData(BaseModel):
    automations: list[AutomationView] = Field(default_factory=list)
    # False は定義の保存先（Oracle）が無い（再起動で消える）。
    persistent: bool = True


class AutomationRun(BaseModel):
    run_id: str
    status: RunStatus
    trigger: str
    created_at: datetime
    updated_at: datetime


class AutomationDetail(BaseModel):
    automation: AutomationView
    runs: list[AutomationRun] = Field(default_factory=list)


class AutomationToolCheck(BaseModel):
    """自動実行で使えない（承認が要る）ツールの確認（編集画面の警告。#1813）。"""

    agent_id: str
    # 業務 Agent のスキルが使うツールのうち、ツールのポリシーで承認が要るもの（モデルに渡す名前）。
    approval_required_tools: list[str] = Field(default_factory=list)
    # ツールを確かめられなかった MCP 接続の理由（接続できないなど）。
    warnings: list[str] = Field(default_factory=list)


class AutomationFired(BaseModel):
    run_id: str | None
    # created / skipped
    result: str
    message: str


def _save(item: Automation) -> None:
    from app.features.agent import control_plane_store

    control_plane_store.save_automation(item.id, item.model_dump(mode="json"))


def _delete(automation_id: str) -> None:
    from app.features.agent import control_plane_store

    control_plane_store.delete_automation(automation_id)


class AutomationStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._items: dict[str, Automation] = {}

    def list(self) -> list[Automation]:
        with self._lock:
            items = [item.model_copy(deep=True) for item in self._items.values()]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    def get(self, automation_id: str) -> Automation:
        with self._lock:
            item = self._items.get(automation_id)
            if item is None:
                raise KeyError(automation_id)
            return item.model_copy(deep=True)

    def create(self, data: AutomationInput, *, owner: str, now: datetime) -> Automation:
        item = Automation(**data.model_dump(), run_as_user_uuid=owner, created_by_user_uuid=owner)
        item.next_run_at = _next(item, now)
        _save(item)
        with self._lock:
            self._items[item.id] = item
        return item.model_copy(deep=True)

    def update(self, automation_id: str, data: AutomationInput, *, now: datetime) -> Automation:
        with self._lock:
            current = self._items.get(automation_id)
            if current is None:
                raise KeyError(automation_id)
            updated = current.model_copy(update={**data.model_dump(), "updated_at": now})
            # model_copy は検証しないため、schedule を model に戻す。
            updated.schedule = data.schedule
            updated.next_run_at = _next(updated, now)
        _save(updated)
        with self._lock:
            self._items[automation_id] = updated
        return updated.model_copy(deep=True)

    def change(self, automation_id: str, mutate: Callable[[Automation], None]) -> Automation:
        with self._lock:
            item = self._items.get(automation_id)
            if item is None:
                raise KeyError(automation_id)
            changed = item.model_copy(deep=True)
            mutate(changed)
        _save(changed)
        with self._lock:
            self._items[automation_id] = changed
        return changed.model_copy(deep=True)

    def change_quietly(self, automation_id: str, mutate: Callable[[Automation], None]) -> None:
        """実行の記録（保存に失敗しても実行は止めない）。"""
        try:
            self.change(automation_id, mutate)
        except KeyError:
            return
        except Exception:  # noqa: BLE001 - 保存先の一時的な失敗で自動実行を止めない
            logger.warning("agent_automation_not_saved", extra={"automation_id": automation_id})

    def delete(self, automation_id: str) -> None:
        with self._lock:
            if automation_id not in self._items:
                raise KeyError(automation_id)
        _delete(automation_id)
        with self._lock:
            self._items.pop(automation_id, None)

    def restore(self, item: Automation, *, now: datetime | None = None) -> None:
        """保存した定義を戻す。停止中に過ぎた回は実行せず、次回へ送る。"""
        current = now or _now()
        if item.next_run_at is None or item.next_run_at <= current:
            item.next_run_at = _next(item, current)
        with self._lock:
            self._items[item.id] = item

    def replace_all(self, items: Sequence[Automation], *, now: datetime | None = None) -> None:
        """保存先の定義で置き換える（データを書き換える migration の後の読み直し。#1844）。

        起動時の復元と違い、過ぎた回は次回へ送らない（停止していないので、スケジューラがそのまま
        実行する）。次回が無い有効な定義だけ、次回を決める。
        """
        current = now or _now()
        for item in items:
            if item.next_run_at is None:
                item.next_run_at = _next(item, current)
        with self._lock:
            self._items = {item.id: item for item in items}

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


def _next(item: Automation, now: datetime) -> datetime | None:
    if not item.enabled:
        return None
    return next_run_after(item.schedule, now)


automation_store = AutomationStore()


# ---- 実行 -------------------------------------------------------------------------------

# 実行する利用者を確かめる関数（production は共通認証の利用者の現在の権限。テストで差し替える）。
OwnerCheck = Callable[[Automation], str | None]


def check_owner(item: Automation) -> str | None:
    """実行する利用者がこの業務 Agent を実行できるか。できなければ理由（日本語）。"""
    from app.security.dependencies import local_debug_principal
    from app.security.domain import LOCAL_DEBUG_USER_UUID, as_principal
    from app.security.permissions import ADMIN, RUNS_OPERATE
    from app.security.service import get_security_service

    settings = get_settings()
    if item.run_as_user_uuid == LOCAL_DEBUG_USER_UUID:
        if settings.app_auth_enabled:
            return "local で作った自動実行は、本番では実行できません。"
        principal = local_debug_principal()
    else:
        try:
            principal = as_principal(
                get_security_service().principal_for_worker(item.run_as_user_uuid)
            )
        except Exception:  # noqa: BLE001 - 利用者を確かめられなければ実行しない
            return (
                "実行する利用者の権限を確認できません（無効・削除・初回のパスワード変更が未了）。"
            )
    if not principal.has_any_permission({RUNS_OPERATE, ADMIN}):
        return "実行する利用者に実行の権限がありません。"
    if not principal.can_use_agent(item.agent_id):
        return "実行する利用者はこの業務 Agent を使えません。"
    return None


owner_check: OwnerCheck = check_owner
# 実行中の Run の task（GC で消えないよう参照を持つ）。
_tasks: set[asyncio.Task[None]] = set()


def _schedule_execution(run: RunState) -> None:
    if run.runtime_id != BUILTIN_RUNTIME_ID or run.status != RunStatus.QUEUED:
        return
    if get_settings().agent_runtime_dispatch_mode.strip().lower() != "in_process":
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(builtin_runtime.execute_run(run.id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def fire(
    automation_id: str,
    *,
    trigger: str,
    now: datetime | None = None,
) -> AutomationFired:
    """自動実行の Run を作る。前回の Run が終わっていない・利用者が実行できないときは作らない。"""
    from app.features.agent.runtime import runtime_repository

    current = now or _now()
    item = automation_store.get(automation_id)

    def record(result: str, message: str, run_id: str | None = None) -> AutomationFired:
        def mutate(target: Automation) -> None:
            target.last_run_at = current
            target.last_trigger = trigger
            target.last_result = result
            target.last_message = message
            if run_id:
                target.last_run_id = run_id

        automation_store.change_quietly(automation_id, mutate)
        return AutomationFired(run_id=run_id, result=result, message=message)

    if item.last_run_id:
        try:
            previous = runtime_repository.get_run(item.last_run_id)
        except KeyError:
            previous = None
        if previous is not None and previous.status in _ACTIVE_RUN_STATUSES:
            return record("skipped", "前回の実行が終わっていないため、この回は実行しませんでした。")
    problem = owner_check(item)
    if problem:
        logger.warning("agent_automation_owner_rejected", extra={"automation_id": item.id})
        return record("failed_to_start", problem)
    agents = {agent.id: agent for agent in runtime_repository.list_agents()}
    reason = agent_unavailable_reason(agents.get(item.agent_id))
    if reason is not None:
        return record("failed_to_start", reason)
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(
            goal=item.goal,
            agent_id=item.agent_id,
            metadata={"automation_id": item.id, "automation_trigger": trigger},
        ),
        created_by_user_uuid=item.run_as_user_uuid,
    )
    _schedule_execution(run)
    return record("created", "実行を作りました。", run.id)


def scheduler_tick(now: datetime | None = None) -> list[AutomationFired]:
    """予定の時刻を過ぎた自動実行の Run を作り、次回を計算し直す。"""
    current = now or _now()
    fired: list[AutomationFired] = []
    for item in automation_store.list():
        if not item.enabled or item.next_run_at is None or item.next_run_at > current:
            continue
        next_at = next_run_after(item.schedule, current)

        def advance(target: Automation, next_at: datetime = next_at) -> None:
            target.next_run_at = next_at

        automation_store.change_quietly(item.id, advance)
        try:
            fired.append(fire(item.id, trigger="schedule", now=current))
        except Exception:  # noqa: BLE001 - 1 件の失敗で他の自動実行を止めない
            logger.exception("agent_automation_fire_failed", extra={"automation_id": item.id})
    return fired


async def run_scheduler() -> None:
    """スケジューラ（API のプロセスの lifespan で回す）。

    期限の判定と Run の作成（保存の I/O を含む）はスレッドで行い、作った Run の実行（in-process の
    モード）はこのイベントループで始める。
    """
    from app.features.agent import storage_bootstrap
    from app.features.agent.runtime import runtime_repository

    while True:
        # 自動実行の定義と Run の保存先を読み込むまで（DB の停止中など）は判定しない（#1212）。
        if not storage_bootstrap.is_ready():
            await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)
            continue
        try:
            fired = await asyncio.to_thread(scheduler_tick)
            for item in fired:
                if item.run_id:
                    _schedule_execution(runtime_repository.get_run(item.run_id))
        except Exception:  # noqa: BLE001 - スケジューラは止めない
            logger.exception("agent_automation_scheduler_failed")
        await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)


def recent_runs(automation_id: str, limit: int = 10) -> list[AutomationRun]:
    from app.features.agent.runtime import runtime_repository

    runs = [
        run
        for run in runtime_repository.list_runs()
        if run.metadata.get("automation_id") == automation_id
    ]
    runs.sort(key=lambda run: run.created_at, reverse=True)
    return [
        AutomationRun(
            run_id=run.id,
            status=run.status,
            trigger=str(run.metadata.get("automation_trigger") or ""),
            created_at=run.created_at,
            updated_at=run.updated_at,
        )
        for run in runs[:limit]
    ]
