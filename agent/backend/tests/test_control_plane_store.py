"""画面・API で変えた定義の保存と復元（#764）。

Skill・MCP 接続（秘密は暗号化）・ツール権限を API で変え、保存先
（JSON ファイル / Oracle の `AGENT_CONTROL_PLANE_ITEMS`）に残ること、メモリから消しても
`restore_control_plane()` で戻ることを確かめる。
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pytest import MonkeyPatch

from app.features.agent import control_plane_store
from app.features.agent.config import runtime_config_store
from app.features.agent.control_plane_store import (
    ControlPlaneStoreError,
    FileItemStore,
    OracleItemStore,
    restore_control_plane,
    set_control_plane_store,
)
from app.features.agent.skills import skill_registry
from app.main import app
from app.secret_box import SecretBoxError, open_secret, seal_secret
from app.settings import get_settings

SECRET = "control-plane-test-secret-0123456789abcdef"  # nosec B105 - テスト用


def _request(method: str, path: str, payload: dict[str, Any] | None = None) -> httpx.Response:
    import anyio

    async def call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.request(method, path, json=payload)

    return anyio.run(call)


@pytest.fixture
def file_store(tmp_path: Path, monkeypatch: MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(get_settings(), "app_service_token_secret", SECRET)
    monkeypatch.setattr(
        runtime_config_store,
        "_mcp_servers",
        {config.server_id: config for config in runtime_config_store.list_mcp_servers()},
    )
    monkeypatch.setattr(
        runtime_config_store, "_tool_policy", runtime_config_store.get_tool_policy()
    )
    path = tmp_path / "agent-runtime.control-plane.json"
    set_control_plane_store(FileItemStore(path))
    try:
        yield path
    finally:
        set_control_plane_store(None)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove("cp764_skill")


# ---------------------------------------------------------------------------
# 秘密の暗号化
# ---------------------------------------------------------------------------


def test_secret_box_round_trip_and_key_change(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "app_service_token_secret", SECRET)
    sealed = seal_secret("erp-api-key")
    assert sealed.startswith("enc:v1:")
    assert "erp-api-key" not in sealed
    assert open_secret(sealed) == "erp-api-key"

    # 署名鍵を変えると復号できない（画面で入れ直す）。
    monkeypatch.setattr(get_settings(), "app_service_token_secret", SECRET[::-1])
    assert open_secret(sealed) is None
    monkeypatch.setattr(get_settings(), "app_service_token_secret", "short")
    with pytest.raises(SecretBoxError, match="PLATFORM_SERVICE_TOKEN_SECRET"):
        seal_secret("x")


# ---------------------------------------------------------------------------
# API の変更を保存し、再起動の後に戻す
# ---------------------------------------------------------------------------


def test_api_changes_are_saved_and_restored(file_store: Path) -> None:
    assert (
        _request(
            "POST",
            "/api/skills",
            {"id": "cp764_skill", "name": "保存する Skill", "instructions": "手順"},
        ).status_code
        == 200
    )
    assert (
        _request(
            "POST",
            "/api/settings/mcp-connections",
            {
                "server_id": "cp764_erp",
                "base_url": "https://erp.example.test/mcp",
                "auth_mode": "api_key",
                "api_key": "erp-api-key",
            },
        ).status_code
        == 200
    )
    assert (
        _request(
            "PATCH",
            "/api/settings/mcp-connections/rag",
            {"base_url": "http://rag-host/api/mcp", "timeout_seconds": 30},
        ).status_code
        == 200
    )
    assert _request("PATCH", "/api/settings/tool-policy", {"ask": ["echo"]}).status_code == 200

    saved = json.loads(file_store.read_text(encoding="utf-8"))
    assert set(saved) == {"skill", "mcp_connection", "tool_policy"}
    # API キーは暗号化して保存する（平文を残さない）。
    erp = saved["mcp_connection"]["cp764_erp"]
    assert erp["api_key"].startswith("enc:v1:")
    assert "erp-api-key" not in file_store.read_text(encoding="utf-8")

    # 再起動の代わりにメモリから消し、RAG の URL も初期値に戻してから復元する。
    skill_registry.remove("cp764_skill")
    runtime_config_store.remove_mcp_server("cp764_erp")
    runtime_config_store.upsert_mcp_server("rag", base_url="", timeout_seconds=60)
    runtime_config_store.patch_tool_policy(ask=[])

    restored = restore_control_plane()

    assert restored == {
        "mcp_connection": 2,
        "tool_policy": 1,
        "skill": 1,
        # 自動実行（#784）は tests/test_automations.py で確かめる。
        "automation": 0,
        # 品質評価（#776）は tests/test_evaluation.py で確かめる。
        "evaluation_set": 0,
        "evaluation_job": 0,
    }
    skill = skill_registry.get("cp764_skill")
    assert skill is not None and skill.instructions == "手順"
    erp_config = runtime_config_store.get_mcp("cp764_erp")
    assert erp_config.api_key == "erp-api-key"
    assert erp_config.source == "runtime"
    rag = runtime_config_store.get_mcp("rag")
    # RAG は組み込みのまま（認証方式は変えない）で、画面で変えた URL・タイムアウトだけ戻る。
    assert (rag.base_url, rag.timeout_seconds, rag.source) == (
        "http://rag-host/api/mcp",
        30,
        "builtin",
    )
    assert rag.effective_auth_mode() == "service_token"
    assert runtime_config_store.get_tool_policy().ask == {"echo"}
    # 復元は保存し直さない（ファイルは変わらない）。
    assert json.loads(file_store.read_text(encoding="utf-8")) == saved

    # 削除も保存する。
    assert _request("DELETE", "/api/settings/mcp-connections/cp764_erp").status_code == 200
    assert _request("DELETE", "/api/skills/cp764_skill").status_code == 200
    after = json.loads(file_store.read_text(encoding="utf-8"))
    assert "cp764_erp" not in after["mcp_connection"]
    assert "cp764_skill" not in after.get("skill", {})


def test_secret_without_signing_key_is_not_saved(
    file_store: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "app_service_token_secret", "")

    response = _request(
        "POST",
        "/api/settings/mcp-connections",
        {"server_id": "cp764_nokey", "auth_mode": "api_key", "api_key": "plain-key"},
    )

    assert response.status_code == 503
    assert "PLATFORM_SERVICE_TOKEN_SECRET" in response.json()["error_messages"][0]
    assert not file_store.exists() or "plain-key" not in file_store.read_text(encoding="utf-8")
    with contextlib.suppress(KeyError, ValueError):
        runtime_config_store.remove_mcp_server("cp764_nokey")


# ---------------------------------------------------------------------------
# Oracle の保存先
# ---------------------------------------------------------------------------


class _FakeItemsCursor:
    def __init__(self, database: _FakeItemsDatabase) -> None:
        self._db = database
        self._rows: list[tuple[str, str, str]] = []

    def __enter__(self) -> _FakeItemsCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def setinputsizes(self, **sizes: Any) -> None:
        self._db.input_sizes.append(dict(sizes))

    def execute(self, statement: str, **params: Any) -> None:
        if not self._db.table_exists:
            raise RuntimeError("ORA-00942: table or view does not exist")
        sql = " ".join(statement.split()).upper()
        if sql.startswith("SELECT ITEM_KIND"):
            self._rows = [(kind, item_id, doc) for (kind, item_id), doc in self._db.rows.items()]
        elif sql.startswith("MERGE INTO AGENT_CONTROL_PLANE_ITEMS"):
            self._db.rows[(params["item_kind"], params["item_id"])] = params["item_json"]
        elif sql.startswith("DELETE FROM AGENT_CONTROL_PLANE_ITEMS"):
            self._db.rows.pop((params["item_kind"], params["item_id"]), None)
        else:  # pragma: no cover - 想定外の SQL
            raise AssertionError(sql)

    def fetchall(self) -> list[tuple[str, str, str]]:
        return self._rows


class _FakeItemsDatabase:
    def __init__(self) -> None:
        self.table_exists = True
        self.rows: dict[tuple[str, str], str] = {}
        self.commits = 0
        self.input_sizes: list[dict[str, Any]] = []

    def connect(self) -> _FakeItemsConnection:
        return _FakeItemsConnection(self)


class _FakeItemsConnection:
    def __init__(self, database: _FakeItemsDatabase) -> None:
        self._db = database

    def __enter__(self) -> _FakeItemsConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> _FakeItemsCursor:
        return _FakeItemsCursor(self._db)

    def commit(self) -> None:
        self._db.commits += 1


def test_oracle_item_store_merges_loads_and_deletes() -> None:
    database = _FakeItemsDatabase()
    store = OracleItemStore(connect_factory=database.connect)

    store.put("skill", "s1", {"id": "s1", "name": "一つ目"})
    store.put("skill", "s1", {"id": "s1", "name": "更新"})
    store.put("tool_policy", "default", {"default_mode": "approval"})

    assert store.load() == {
        "skill": {"s1": {"id": "s1", "name": "更新"}},
        "tool_policy": {"default": {"default_mode": "approval"}},
    }
    store.delete("skill", "s1")
    assert "skill" not in store.load()
    assert database.commits == 4
    # 定義の JSON は 4,000 byte を超えうるため CLOB で bind する（#841）。削除は bind しない。
    import oracledb

    assert database.input_sizes == [{"item_json": oracledb.DB_TYPE_CLOB}] * 3


def test_oracle_item_store_without_table_starts_empty_and_refuses_writes() -> None:
    database = _FakeItemsDatabase()
    database.table_exists = False
    store = OracleItemStore(connect_factory=database.connect)

    # システムテーブルを作る前でも起動できる（読み込みは空）。保存は作成を案内して断る。
    assert store.load() == {}
    with pytest.raises(ControlPlaneStoreError, match="システムテーブル"):
        store.put("skill", "s1", {"id": "s1"})


def test_store_follows_runtime_repository_backend(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_runtime_repository_backend", "oracle_checkpoint")
    assert isinstance(control_plane_store.build_control_plane_store(), OracleItemStore)
    monkeypatch.setattr(settings, "agent_runtime_repository_backend", "file")
    monkeypatch.setattr(settings, "agent_runtime_snapshot_path", str(tmp_path / "runtime.json"))
    store = control_plane_store.build_control_plane_store()
    assert isinstance(store, FileItemStore)
    monkeypatch.setattr(settings, "agent_runtime_repository_backend", "memory")
    monkeypatch.setattr(settings, "agent_runtime_snapshot_path", None)
    assert control_plane_store.build_control_plane_store().persistent is False
