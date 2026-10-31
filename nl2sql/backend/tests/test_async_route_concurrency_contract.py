"""ASGI event loop 上で同期 I/O を直接動かさない契約テスト。"""

from __future__ import annotations

import ast
import asyncio
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from pr_system_settings import oci as shared_oci

from app.api import health as health_routes
from app.features.nl2sql import router as nl2sql_router
from app.features.nl2sql.models import DbAdminObjectsData, SchemaObjectPage
from app.features.schema import router as schema_router
from app.main import app
from app.settings import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"
ROUTE_METHODS = {"get", "post", "put", "patch", "delete"}
THREADPOOL_HELPERS = {"run_sync_io"}

SYNC_SERVICE_ROOTS = {
    "nl2sql_service",
    "ontology_runtime",
    "ontology_build_service",
    "ontology_publish_service",
    "system_schema_manager",
}
SYNC_FACTORY_ROOTS = {
    "get_security_service",
    "get_deepsec_service",
}
SYNC_MODULE_ROOTS = {
    "oracledb",
    "requests",
    "shutil",
    "subprocess",
}
SYNC_LOCAL_HELPERS = {
    "_install_database_wallet",
    "_install_downloaded_database_wallet",
    "_install_oci_private_key",
    "_install_uploaded_database_wallet",
    "_prepare_database_wallet_download",
    "_persist_adb_settings",
    "_read_object_storage_namespace",
    "_test_oci_config",
}
FILE_METHODS = {
    "glob",
    "iterdir",
    "open",
    "read_bytes",
    "read_text",
    "write_bytes",
    "write_text",
}


@dataclass(frozen=True)
class Violation:
    file: Path
    function: str
    line: int
    call: str
    reason: str

    def format(self) -> str:
        path = self.file.relative_to(BACKEND_DIR)
        return f"{path}:{self.line} {self.function} -> {self.call} ({self.reason})"


def _router_files() -> Iterable[Path]:
    # ontology_router.py 等の別名 router も契約対象にする(router.py 完全一致だと漏れる)
    yield from sorted(APP_DIR.rglob("*router*.py"))


def _is_route_handler(function: ast.AsyncFunctionDef) -> bool:
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if (
            isinstance(target, ast.Attribute)
            and target.attr in ROUTE_METHODS
            and isinstance(target.value, ast.Name)
            and target.value.id == "router"
        ):
            return True
    return False


def _root_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _root_name(node.value)
    if isinstance(node, ast.Call):
        return _root_name(node.func)
    if isinstance(node, ast.Subscript):
        return _root_name(node.value)
    return None


def _call_display(node: ast.Call) -> str:
    try:
        return ast.unparse(node.func)
    except Exception:  # pragma: no cover - ast.unparse fallback
        return type(node.func).__name__


def _attribute_name(node: ast.AST) -> str | None:
    return node.attr if isinstance(node, ast.Attribute) else None


class _ParentAnnotator(ast.NodeVisitor):
    def __init__(self) -> None:
        self.parents: dict[ast.AST, ast.AST] = {}

    def generic_visit(self, node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            self.parents[child] = node
        super().generic_visit(node)


def _is_within_threadpool_helper(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, ast.Call) and _root_name(current.func) in THREADPOOL_HELPERS:
            return True
        current = parents.get(current)
    return False


def _sync_call_reason(node: ast.Call) -> str | None:
    root = _root_name(node.func)
    attr = _attribute_name(node.func)
    if root in SYNC_SERVICE_ROOTS:
        return "sync domain service/runtime"
    if root in SYNC_FACTORY_ROOTS:
        return "sync service factory/store"
    if root in SYNC_MODULE_ROOTS:
        return "sync module I/O"
    if root == "time" and attr == "sleep":
        return "sync sleep"
    if root in SYNC_LOCAL_HELPERS:
        return "sync local helper"
    if attr in FILE_METHODS:
        return "sync file/path I/O"
    if root in {"open"}:
        return "sync file I/O"
    return None


def test_async_routes_do_not_call_sync_io_directly() -> None:
    violations: list[Violation] = []
    for file_path in _router_files():
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        annotator = _ParentAnnotator()
        annotator.visit(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.AsyncFunctionDef) or not _is_route_handler(node):
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                if _root_name(child.func) in THREADPOOL_HELPERS:
                    continue
                reason = _sync_call_reason(child)
                if reason is None or _is_within_threadpool_helper(child, annotator.parents):
                    continue
                violations.append(
                    Violation(
                        file=file_path,
                        function=node.name,
                        line=child.lineno,
                        call=_call_display(child),
                        reason=reason,
                    )
                )

    assert not violations, (
        "async route 内の同期 I/O は event loop を塞ぎます。"
        " route を def にするか await run_sync_io(...) で包んでください:\n"
        + "\n".join(item.format() for item in violations)
    )


def _block_until_released(
    entered: threading.Event,
    release: threading.Event,
) -> None:
    entered.set()
    if not release.wait(timeout=2):
        raise AssertionError("blocking fake was not released")


async def _wait_for_entered(entered: threading.Event) -> None:
    for _ in range(100):
        if entered.is_set():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("blocking route was not entered")


def _enable_local_debug_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "local")
    monkeypatch.setattr(settings, "environment", "local")
    monkeypatch.setattr(settings, "auth_mode", "local")


@dataclass(slots=True)
class _BlockingCall:
    entered: threading.Event
    release: threading.Event


async def _assert_permissions_return_while_blocked(
    path: str,
    block: _BlockingCall,
    *,
    method: str = "GET",
    json: dict[str, object] | None = None,
) -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        slow_task = asyncio.create_task(client.request(method, path, json=json))
        try:
            await _wait_for_entered(block.entered)
            response = await asyncio.wait_for(
                client.get("/api/security/permissions"),
                timeout=0.35,
            )
            assert response.status_code == 200
            assert response.json()["data"]
        finally:
            block.release.set()
        slow_response = await asyncio.wait_for(slow_task, timeout=2)
        assert slow_response.status_code == 200


def _new_block() -> _BlockingCall:
    return _BlockingCall(threading.Event(), threading.Event())


def test_blocked_nl2sql_db_admin_route_does_not_block_security_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block = _new_block()
    _enable_local_debug_auth(monkeypatch)

    class BlockingNl2SqlService:
        def ensure_persistence_available(self) -> None:
            return None

        def list_db_admin_tables(self) -> DbAdminObjectsData:
            _block_until_released(block.entered, block.release)
            return DbAdminObjectsData(runtime="deterministic")

    monkeypatch.setattr(nl2sql_router, "nl2sql_service", BlockingNl2SqlService())

    asyncio.run(
        _assert_permissions_return_while_blocked(
            "/api/nl2sql/db-admin/tables",
            block,
        )
    )


def test_blocked_schema_objects_route_does_not_block_security_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block = _new_block()
    _enable_local_debug_auth(monkeypatch)

    class BlockingNl2SqlService:
        def search_schema_objects(self, **_kwargs: object) -> SchemaObjectPage:
            _block_until_released(block.entered, block.release)
            return SchemaObjectPage(catalog_version=1)

    monkeypatch.setattr(schema_router, "nl2sql_service", BlockingNl2SqlService())

    asyncio.run(
        _assert_permissions_return_while_blocked(
            "/api/schema/objects?limit=10",
            block,
        )
    )


def test_blocked_database_ready_route_does_not_block_security_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block = _new_block()
    _enable_local_debug_auth(monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_runtime_mode", "oracle")
    monkeypatch.setattr(settings, "nl2sql_persistence_mode", "oracle")
    # platform の database_readiness（#325）が ok になる Walletless TLS の設定。
    monkeypatch.setattr(settings, "oracle_user", "APP")
    monkeypatch.setattr(settings, "oracle_password", "secret")
    monkeypatch.setattr(settings, "oracle_dsn", "adb.example.oraclecloud.com:1522/service_high")
    monkeypatch.setattr(settings, "oracle_connection_security", "walletless_tls")
    monkeypatch.setattr(settings, "oracle_deepsec_enabled", False)

    async def async_oracle_probe(_settings: object) -> None:
        return None

    class BlockingReadyService:
        uses_incremental_store = True

        def check_incremental_store(self) -> tuple[bool, str]:
            _block_until_released(block.entered, block.release)
            return True, ""

    monkeypatch.setattr(health_routes, "test_oracle_connection", async_oracle_probe)
    monkeypatch.setattr(health_routes, "nl2sql_service", BlockingReadyService())
    monkeypatch.setattr(health_routes, "observe_system_schema_epoch", lambda: None)

    asyncio.run(
        _assert_permissions_return_while_blocked(
            "/api/ready/database",
            block,
        )
    )


def test_blocked_settings_namespace_route_does_not_block_security_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    block = _new_block()
    _enable_local_debug_auth(monkeypatch)

    def blocking_namespace(_payload: object, **_config: object) -> str:
        _block_until_released(block.entered, block.release)
        return "testnamespace"

    monkeypatch.setattr(shared_oci, "_read_object_storage_namespace", blocking_namespace)

    asyncio.run(
        _assert_permissions_return_while_blocked(
            "/api/settings/oci/object-storage/namespace",
            block,
            method="POST",
            json={
                "config_file": "~/.oci/config",
                "profile": "DEFAULT",
                "region": "ap-tokyo-1",
            },
        )
    )
