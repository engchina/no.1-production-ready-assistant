"""データを書き換える migration の後の、動いている backend のメモリの読み直し（#1844）。

システムテーブルの「作成・更新」で保存済みのデータ（Run・業務 Agent の checkpoint、画面・API で
変えた定義）を書き換えたら、同じプロセスの Run の repository と定義の registry を保存先から
読み直し、書き換えの間の保存が migration の結果を上書きしないことを確かめる。Oracle は fake で
再現する（checkpoint の行と、system schema の辞書・台帳は `test_system_schema` の fake）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from checkpoint_support import CheckpointTable
from pytest import MonkeyPatch
from security_support import client
from test_system_schema import (
    _FakeConnection,
    _FakeCursor,
    _FakeDatabase,
    _legacy_database,
    _manager,
)

import app.features.agent.router as agent_router
import app.features.settings.system_tables as system_tables_api
from app.cli import agent_system_schema
from app.features.agent import control_plane_store, runtime, storage_bootstrap
from app.features.agent.automations import automation_store
from app.features.agent.config import runtime_config_store
from app.features.agent.control_plane_store import (
    WRITES_PAUSED_MESSAGE,
    ControlPlaneStoreError,
    FileItemStore,
    set_control_plane_store,
)
from app.features.agent.evaluation import evaluation_set_store, evaluation_store
from app.features.agent.runtime import (
    AgentProfile,
    AgentProfilePatch,
    AgentRuntimeOracleCheckpointRepository,
)
from app.features.agent.skills import AgentSkillDefinition, skill_registry
from app.settings import get_settings
from app.system_schema import _RENAMED_SKILL_IDS_013, MIGRATIONS, SystemSchemaManager

MIGRATION_013 = "20261014_013_rename_builtin_skill_ids"
AGENT_ID = "agent-1844"
NEW_SKILL_IDS = ["business-rag-research", "structured-data-query"]
OLD_SKILL_IDS = ["business_rag_research", "structured_data_query"]
# 書き換えの途中で止める待ちの上限（秒）。止まらないことを確かめる待ちは短くする。
WAIT_SECONDS = 10.0
BLOCKED_SECONDS = 0.2


class _Checkpoints(CheckpointTable):
    """Run・業務 Agent の保存先（head と 1 件 = 1 行。#1939）。"""

    def __init__(self) -> None:
        super().__init__()
        # migration 013 の UPDATE の直前に呼ぶ（書き換えの途中で止めるテスト用）。
        self.before_rewrite: Callable[[], None] = lambda: None

    def rename_skill_ids(self) -> None:
        """保存済みの業務 Agent の JSON の引用符つきの ID を置き換える（013 と同じ置き換え）。

        書き換えの仕組み（#1844）を確かめるため、業務 Agent の行に当てる（実の 013 は #1939 より
        前の全体の snapshot に当てた）。
        """
        self.before_rewrite()
        for row in self.entities.values():
            text = row.text
            if text is None:
                continue
            for old_id, new_id in _RENAMED_SKILL_IDS_013:
                text = text.replace(f'"{old_id}"', f'"{new_id}"')
            row.text = text


class _MigrationCursor(_FakeCursor):
    """system schema の fake に、checkpoint を書き換える migration 013 の UPDATE を足す。"""

    def __init__(self, database: _FakeDatabase, checkpoints: _Checkpoints) -> None:
        super().__init__(database)
        self._checkpoints = checkpoints

    def execute(self, statement: str, parameters: dict[str, Any] | None = None) -> None:
        if statement.upper().startswith("UPDATE AGENT_RUNTIME_CHECKPOINTS"):
            self.database.executed.append(statement.upper())
            self._checkpoints.rename_skill_ids()
            return
        super().execute(statement, parameters)


class _MigrationConnection(_FakeConnection):
    def __init__(self, database: _FakeDatabase, checkpoints: _Checkpoints) -> None:
        super().__init__(database)
        self._checkpoints = checkpoints

    def cursor(self) -> _MigrationCursor:
        return _MigrationCursor(self.database, self._checkpoints)


class _Environment:
    def __init__(self, checkpoints: _Checkpoints, manager: SystemSchemaManager) -> None:
        self.checkpoints = checkpoints
        self.manager = manager

    def saved_agent(self) -> dict[str, Any]:
        text = self.checkpoints.entities[("default", "agent", AGENT_ID)].text
        assert text is not None
        value: dict[str, Any] = json.loads(text)
        return value


def _seed_saved_agent(checkpoints: _Checkpoints) -> None:
    """#1816 より前の ID（旧 ID）で業務 Agent を保存した保存先。"""
    agent = AgentProfile(
        id=AGENT_ID,
        name="保存済みの業務 Agent",
        skill_ids=NEW_SKILL_IDS,
        versioned=True,
    )
    checkpoints.seed(agents=[agent])
    row = checkpoints.entities[("default", "agent", AGENT_ID)]
    text = row.text or ""
    for new_id, old_id in zip(NEW_SKILL_IDS, OLD_SKILL_IDS, strict=True):
        text = text.replace(f'"{new_id}"', f'"{old_id}"')
    row.text = text


@pytest.fixture
def environment(monkeypatch: MonkeyPatch) -> Iterator[_Environment]:
    """migration 013 だけが未適用の DB と、その checkpoint を読み込んだ Oracle の repository。"""
    checkpoints = _Checkpoints()
    _seed_saved_agent(checkpoints)
    database = _FakeDatabase()

    @contextmanager
    def schema_connection() -> Iterator[_MigrationConnection]:
        yield _MigrationConnection(database, checkpoints)

    manager = SystemSchemaManager(schema_connection)
    manager.initialize()
    database.migrations.pop(MIGRATION_013)
    assert manager.status()["pending_versions"] == [MIGRATION_013]
    monkeypatch.setattr(system_tables_api, "system_schema_manager", manager)
    monkeypatch.setattr(agent_router, "system_schema_manager", manager)

    repository = AgentRuntimeOracleCheckpointRepository(connect_factory=checkpoints.connect)
    runtime.set_runtime_repository(repository)
    try:
        yield _Environment(checkpoints, manager)
    finally:
        runtime.set_runtime_repository(None)


def _listed_skill_ids() -> list[str]:
    response = client.get("/api/agents")
    assert response.status_code == 200, response.text
    agent = next(item for item in response.json()["data"]["agents"] if item["id"] == AGENT_ID)
    return list(agent["skill_ids"])


def test_rewriting_migration_reloads_agents_in_this_process(environment: _Environment) -> None:
    """適用の直後に `GET /api/agents` が新しい ID を返す（再起動を待たない）。"""
    assert _listed_skill_ids() == OLD_SKILL_IDS

    response = client.post("/api/settings/database/system-tables/initialize", json={})

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["operation"] == "migrated"
    assert data["rewritten_saved_data"] == ["runtime"]
    # 同じプロセスは読み直したので、再起動は求めない（dispatcher の無い構成）。
    assert data["restart_required"] is None
    assert _listed_skill_ids() == NEW_SKILL_IDS
    assert environment.saved_agent()["skill_ids"] == NEW_SKILL_IDS


def test_save_during_rewrite_waits_and_keeps_migration_result(
    environment: _Environment,
) -> None:
    """書き換えの間の保存は読み直しまで待ち、migration の結果を古いメモリの値で上書きしない。"""
    rewriting = threading.Event()
    release = threading.Event()

    def pause() -> None:
        rewriting.set()
        assert release.wait(WAIT_SECONDS)

    environment.checkpoints.before_rewrite = pause
    results: dict[str, Any] = {}

    def initialize() -> None:
        results["initialize"] = environment.manager.initialize()

    saved = threading.Event()

    def save() -> None:
        runtime.runtime_repository.patch_agent(
            AGENT_ID, AgentProfilePatch(description="保存した説明")
        )
        saved.set()

    initializer = threading.Thread(target=initialize)
    initializer.start()
    assert rewriting.wait(WAIT_SECONDS)
    saver = threading.Thread(target=save)
    saver.start()
    # 書き換えの途中は保存しない（repository の変更は lock を待つ）。
    assert not saved.wait(BLOCKED_SECONDS)
    release.set()
    initializer.join(WAIT_SECONDS)
    saver.join(WAIT_SECONDS)
    assert not initializer.is_alive() and not saver.is_alive()

    assert results["initialize"]["operation"] == "migrated"
    saved_agent = environment.saved_agent()
    # 保存は読み直した状態の上で行った（新しい ID のまま、説明も残る）。
    assert saved_agent["skill_ids"] == NEW_SKILL_IDS
    assert saved_agent["description"] == "保存した説明"
    assert _listed_skill_ids() == NEW_SKILL_IDS


def test_dispatcher_process_needs_restart(
    environment: _Environment, monkeypatch: MonkeyPatch
) -> None:
    """別のプロセス（dispatcher）のメモリは読み直せないので、再起動を結果で求める。"""
    monkeypatch.setattr(get_settings(), "agent_runtime_dispatch_mode", "dispatcher")

    response = client.post("/api/settings/database/system-tables/initialize", json={})

    assert response.status_code == 200, response.text
    restart = response.json()["data"]["restart_required"]
    assert restart["processes"] == [storage_bootstrap.DISPATCHER_PROCESS]
    assert "再起動してください" in restart["message"]
    assert _listed_skill_ids() == NEW_SKILL_IDS


def test_failed_reload_asks_to_restart_backend(
    environment: _Environment, monkeypatch: MonkeyPatch
) -> None:
    """読み直せなければ（checkpoint を読めない）、この backend の再起動を求める。"""

    def broken() -> None:
        raise RuntimeError("DPY-4011: the database or network closed the connection")

    repository = runtime.loaded_runtime_repository()
    assert isinstance(repository, AgentRuntimeOracleCheckpointRepository)
    monkeypatch.setattr(repository, "_read_storage", broken)

    data = environment.manager.initialize()

    assert data["restart_required"]["processes"] == [storage_bootstrap.BACKEND_PROCESS]


def test_migrations_declare_rewritten_saved_data() -> None:
    """保存済みのデータを書き換える migration は、読み直す保存先を宣言する。"""
    rewrites = {migration.name: migration.rewrites for migration in MIGRATIONS}
    assert rewrites[MIGRATION_013] == frozenset({"runtime"})
    for name in (
        "20261014_008_remove_plugin_distribution_sources",
        "20261014_009_remove_automation_webhook",
        "20261014_010_remove_plugins",
        "20261014_011_remove_agent_mcp_api_keys",
    ):
        assert rewrites[name] == frozenset({"control_plane"})
    # DDL だけ・権限の表だけの migration は、メモリを読み直さない。
    assert rewrites["20261014_014_run_fact_data_tool_calls"] == frozenset()
    assert rewrites["20261002_002_remove_retired_permission_codes"] == frozenset()


# ---- 画面・API で変えた定義（AGENT_CONTROL_PLANE_ITEMS） ------------------------------


@pytest.fixture
def control_plane_file(tmp_path: Path) -> Iterator[Path]:
    """保存する構成の定義の保存先（JSON ファイル）と、registry の状態の退避・復元。"""
    skills = skill_registry.export("runtime")
    servers = [item for item in runtime_config_store.list_mcp_servers()]
    policy = runtime_config_store.get_tool_policy()
    automations = automation_store.list()
    sets = evaluation_set_store.list()
    jobs = evaluation_store.list()
    path = tmp_path / "agent-runtime.control-plane.json"
    set_control_plane_store(FileItemStore(path))
    try:
        yield path
    finally:
        set_control_plane_store(None)
        skill_registry.replace_runtime(skills)
        runtime_config_store.replace_restored(servers, policy)
        automation_store.replace_all(automations)
        evaluation_set_store.replace_all(sets)
        evaluation_store.replace_all(jobs)


def _skill(skill_id: str, name: str) -> AgentSkillDefinition:
    return AgentSkillDefinition(id=skill_id, name=name, instructions="手順", source="runtime")


def test_control_plane_rewrite_reloads_definitions_and_refuses_saves(
    control_plane_file: Path,
) -> None:
    """定義を書き換える migration の後は保存先の内容になり、書き換えの間の保存は 503 で断る。"""
    for skill in (_skill("cp1844_keep", "残すスキル"), _skill("cp1844_drop", "消すスキル")):
        control_plane_store.save_skill(skill_registry.upsert_custom(skill))
    store = control_plane_store.get_control_plane_store()

    def rewrite() -> str:
        # migration の代わりに保存先を書き換える（1 件を消し、1 件の名前を変える）。
        store.delete("skill", "cp1844_drop")
        kept = _skill("cp1844_keep", "migration で変えた名前").model_dump(mode="json")
        store.put("skill", "cp1844_keep", kept)
        # 書き換えの間の保存は待たずに断る（古いメモリの値から作った定義を後で書かない）。
        with pytest.raises(ControlPlaneStoreError, match=WRITES_PAUSED_MESSAGE):
            control_plane_store.save_skill(_skill("cp1844_new", "途中で足すスキル"))
        refused = client.post(
            "/api/skills", json={"id": "cp1844_api", "name": "画面から足すスキル"}
        )
        assert refused.status_code == 503
        return "done"

    result, outcome = storage_bootstrap.rewrite_saved_data({"control_plane"}, rewrite)

    assert result == "done"
    assert outcome.reloaded == ("control_plane",)
    assert outcome.restart_required() is None
    kept_skill = skill_registry.get("cp1844_keep")
    assert kept_skill is not None and kept_skill.name == "migration で変えた名前"
    assert skill_registry.get("cp1844_drop") is None
    # 読み直しの後は保存できる。
    control_plane_store.save_skill(skill_registry.upsert_custom(_skill("cp1844_after", "後")))
    assert "cp1844_after" in json.loads(control_plane_file.read_text(encoding="utf-8"))["skill"]


def test_rewrite_failure_still_reloads(control_plane_file: Path) -> None:
    """書き換えが途中で失敗しても、書いた分を読み直す（メモリの古い値を残さない）。"""
    control_plane_store.save_skill(skill_registry.upsert_custom(_skill("cp1844_gone", "消える")))
    store = control_plane_store.get_control_plane_store()

    def rewrite() -> None:
        store.delete("skill", "cp1844_gone")
        raise RuntimeError("ORA-03113: end-of-file on communication channel")

    with pytest.raises(RuntimeError, match="ORA-03113"):
        storage_bootstrap.rewrite_saved_data({"control_plane"}, rewrite)

    assert skill_registry.get("cp1844_gone") is None


def test_cli_asks_to_restart_running_backend(
    monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI は別のプロセスなので、書き換えたら動いている backend の再起動を出力で求める。"""
    monkeypatch.setattr(agent_system_schema, "system_schema_manager", _manager(_legacy_database()))

    assert agent_system_schema.main(["--initialize", "--allow-destructive"]) == 0

    data = json.loads(capsys.readouterr().out)["data"]
    assert data["rewritten_saved_data"] == ["control_plane", "runtime"]
    assert data["restart_required"]["processes"] == [storage_bootstrap.BACKEND_PROCESS]


def test_ddl_only_migration_does_not_reload(monkeypatch: MonkeyPatch) -> None:
    """列を足すだけの migration（014）は保存済みのデータを書き換えないので、読み直さない。"""
    database = _FakeDatabase()
    manager = _manager(database)
    manager.initialize()
    database.migrations.pop("20261014_014_run_fact_data_tool_calls")
    calls: list[str] = []

    def reload() -> dict[str, int]:
        calls.append("reload")
        return {}

    monkeypatch.setattr(control_plane_store, "reload_control_plane", reload)

    data = manager.initialize()

    assert data["operation"] == "migrated"
    assert data["rewritten_saved_data"] == []
    assert data["restart_required"] is None
    assert calls == []
