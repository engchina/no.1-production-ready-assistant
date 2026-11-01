"""RAG の process の入口が、範囲の条件で DB を読む前に context を決めていること（#3334）。

文脈の無い処理は何も読めない（`NO_ACCESS_AUDIT_REQUEST_CONTEXT`。#3250）ので、要求の外の入口
（systemd の unit・`init_script.sh` が呼ぶ CLI・`pyproject.toml` の scripts・`__main__` の
module）が `system_access_context` / `with_system_access` を付け忘れると、エラーにならず黙って
空振りする。

- 入口は機械で見つける（`__main__` の module・`pyproject.toml` の scripts・配備のスクリプトの
  `python -m` / `<module>:app`）。import の閉包（関数の中の import も含む）が範囲の条件の context
  （`app.rag.request_context`）に届く入口は、下の一覧のどちらかに入れるか、`main` に
  `with_system_access` を付ける（どちらも無ければこのテストが落ちる）。
- 一覧の system の入口は、実際に `main` を呼んで `system_access_context` に入ることを確かめる。
- API（`app.main`）は要求ごとに middleware と認証の dependency が context を作る。起動時の処理は
  下の `API_LIFESPAN_TASKS` に分類を書き、要求から始まって全件を扱う後台の処理は個別のテストで
  確かめる。
"""

from __future__ import annotations

import ast
import asyncio
import contextvars
import importlib
import importlib.util
import inspect
import logging
import re
import tomllib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import cache
from pathlib import Path
from types import ModuleType

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
RAG_DIR = BACKEND_DIR.parent
REPO_DIR = RAG_DIR.parent
# 入口を探す Python のディレクトリ（backend の venv で動くもの）。
SOURCE_DIRS = ("app", "scripts")
# 範囲の条件の context。ここに import が届く入口は、context を決めてから DB を読む。
SCOPED_CONTEXT_MODULE = "app.rag.request_context"
# unit・起動のコマンドを書く配備のスクリプト（RAG の unit は rag/ の 2 つが書く。platform は
# 配備の後の確認で RAG の CLI を呼ぶ）。
RAG_COMMAND_FILES = ("init_script.sh", "scripts/*.sh")
PLATFORM_COMMAND_FILES = ("platform/deploy/*.sh",)

# 範囲の制限の無い context（system の context）で動く入口と、`main` に渡す引数。
SYSTEM_ACCESS_ENTRY_POINTS: dict[str, tuple[str, ...]] = {
    # 取込の worker の unit（`IngestionQueueWorker.run_forever`。API の中の worker も同じ）。
    "app.rag.ingestion_worker": (),
    # worker が job ごとに起動する子の process。
    "app.rag.ingestion_job_runner": ("job-synthetic",),
    # `init_script.sh`・`suite-health.sh` が呼ぶ system schema の適用・確認。
    "app.rag.system_schema_cli": (),
    "app.rag.oracle_schema": (),
    # 運用の CLI（全件を読む・書き直す）。
    "app.rag.classification_normalization": (),
    "app.rag.text_search_index_cli": (),
    "app.rag.staging_smoke": (),
    "app.rag.file_processing_staging_cli": (),
    # 一括取込の計測。
    "scripts.measure_bulk_ingestion": (),
}

# 範囲の条件の context に import が届くが、system の context を作らない入口と理由。
OTHER_ENTRY_POINTS: dict[str, str] = {
    "app.main": "API。要求ごとに middleware と認証の dependency が利用者の context を作る",
    "app.rag.legacy_export": "旧テーブルを生の接続で書き出すだけ（範囲の条件を通らない）",
    "app.rag.answer_verify_cli": "backend の API を HTTP で呼ぶ。DB は読まない（判定の設定だけ）",
    "app.rag.answer_judge_calibration": (
        "人手の判定の項目を回答の評価器（LLM）にかけて集計するだけ。DB は読まない（設定だけ）"
    ),
    "app.rag.representation_experiment": (
        "同梱の合成の資料を手元で分割・検索して比べるだけ。DB は読まない（設定だけ。#75）"
    ),
}

# API の process の起動時に要求の外で動く処理（`app.main.lifespan`）と分類。
API_LIFESPAN_TASKS: dict[str, str] = {
    "_warm_search": "接続の pool と vector index の設定を用意するだけ（範囲の条件を通らない）",
    "start_trace_span_writer": "処理の経過の span を書くだけ（読まない）",
    "run_forever": "in-process の取込の worker（IngestionQueueWorker）。system の context に入る",
    "get_evaluation_job_service": "停止で自分の lease の評価の job を失敗にする（更新だけ）",
    "get_chat_answer_run_service": "停止で作成中の回答を取り消す（各 run は要求の context のまま）",
}


@cache
def _module_paths() -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for directory in SOURCE_DIRS:
        for path in (BACKEND_DIR / directory).rglob("*.py"):
            parts = path.relative_to(BACKEND_DIR).with_suffix("").parts
            if parts[-1] == "__init__":
                parts = parts[:-1]
            paths[".".join(parts)] = path
    return paths


@cache
def _tree(module: str) -> ast.Module:
    return ast.parse(_module_paths()[module].read_text(encoding="utf-8"))


def _imported_names(module: str) -> set[str]:
    """`module` が import する名前（関数の中の遅延 import も含む。相対 import は絶対にする）。"""
    path = _module_paths()[module]
    package = module.split(".") if path.name == "__init__.py" else module.split(".")[:-1]
    names: set[str] = set()
    for node in ast.walk(_tree(module)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                prefix = package[: len(package) - node.level + 1]
                base = ".".join([*prefix, base] if base else prefix)
            names.add(base)
            names.update(f"{base}.{alias.name}" for alias in node.names)
    expanded = set(names)
    for name in names:
        parts = name.split(".")
        expanded.update(".".join(parts[:index]) for index in range(1, len(parts)))
    return expanded


@cache
def _import_closure(module: str) -> frozenset[str]:
    known = _module_paths()
    seen: set[str] = set()
    stack = [module]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(name for name in _imported_names(current) if name in known)
    return frozenset(seen)


def _has_main_guard(module: str) -> bool:
    for node in _tree(module).body:
        if (
            isinstance(node, ast.If)
            and isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            and any(
                isinstance(item, ast.Constant) and item.value == "__main__"
                for item in node.test.comparators
            )
        ):
            return True
    return False


def _main_is_declared(module: str) -> bool:
    """`main` に `with_system_access` を付けているか（静的に見る。実行は別のテストで確かめる）。"""
    for node in _tree(module).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "main":
            return any(
                (isinstance(item, ast.Name) and item.id == "with_system_access")
                or (isinstance(item, ast.Attribute) and item.attr == "with_system_access")
                for item in node.decorator_list
            )
    return False


def _pyproject_script_modules() -> set[str]:
    project = tomllib.loads((BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8"))
    project = project.get("project", {})
    targets = [
        *project.get("scripts", {}).values(),
        *project.get("gui-scripts", {}).values(),
    ]
    return {str(target).split(":", 1)[0].strip() for target in targets}


_PYTHON_MODULE_PATTERN = re.compile(r"(?:^|\s)-m\s+(app(?:\.\w+)+)")
_ASGI_TARGET_PATTERN = re.compile(r"(?:^|\s)(app(?:\.\w+)+):\w+")


def _command_modules(patterns: tuple[str, ...], root: Path) -> set[str]:
    modules: set[str] = set()
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            text = path.read_text(encoding="utf-8")
            modules.update(_PYTHON_MODULE_PATTERN.findall(text))
            modules.update(_ASGI_TARGET_PATTERN.findall(text))
    return modules


@cache
def _discovered_entry_points() -> frozenset[str]:
    known = _module_paths()
    rag_commands = _command_modules(RAG_COMMAND_FILES, RAG_DIR)
    stale = sorted(module for module in rag_commands if module not in known)
    assert not stale, f"rag の配備のスクリプトが、backend に無い module を起動している: {stale}"
    # platform の配備のスクリプトは 3 製品の CLI を呼ぶので、RAG の backend にある module だけ。
    platform_commands = {
        module for module in _command_modules(PLATFORM_COMMAND_FILES, REPO_DIR) if module in known
    }
    scripts = _pyproject_script_modules()
    stale_scripts = sorted(module for module in scripts if module not in known)
    assert not stale_scripts, (
        f"pyproject.toml の scripts が、無い module を指している: {stale_scripts}"
    )
    mains = {module for module in known if _has_main_guard(module)}
    return frozenset(mains | rag_commands | platform_commands | scripts)


def _scoped_entry_points() -> set[str]:
    return {
        module
        for module in _discovered_entry_points()
        if SCOPED_CONTEXT_MODULE in _import_closure(module)
    }


def test_discovery_finds_the_known_entry_points() -> None:
    discovered = _discovered_entry_points()
    # 配備のスクリプト（API・取込の worker の unit・schema の CLI）と `__main__` から見つける。
    assert {"app.main", "app.rag.ingestion_worker", "app.rag.system_schema_cli"} <= discovered
    assert "scripts.measure_bulk_ingestion" in discovered
    # DB を使わない CLI（HTTP だけ）は範囲の条件に届かない（閉包の判定が広すぎないこと）。
    assert "app.rag.evaluation_cli" in discovered
    assert "app.rag.evaluation_cli" not in _scoped_entry_points()


def test_every_entry_point_reaching_scoped_reads_is_classified() -> None:
    """範囲の条件に届く入口は、system の context を明示するか、理由を一覧に書く。"""
    declared = {module for module in _scoped_entry_points() if _main_is_declared(module)}
    unclassified = sorted(
        _scoped_entry_points()
        - set(SYSTEM_ACCESS_ENTRY_POINTS)
        - set(OTHER_ENTRY_POINTS)
        - declared
    )
    assert not unclassified, (
        "範囲の条件で DB を読みうる入口が、context を決めていない（文脈の無い処理は何も読めない。"
        "#3250）。全件を扱う入口なら main に with_system_access を付けて "
        "SYSTEM_ACCESS_ENTRY_POINTS に、範囲の条件を通らないなら理由を OTHER_ENTRY_POINTS に書く: "
        f"{unclassified}"
    )


def test_classified_entry_points_still_exist() -> None:
    listed = set(SYSTEM_ACCESS_ENTRY_POINTS) | set(OTHER_ENTRY_POINTS)
    assert not set(SYSTEM_ACCESS_ENTRY_POINTS) & set(OTHER_ENTRY_POINTS)
    stale = sorted(listed - _discovered_entry_points())
    assert not stale, f"一覧の入口が見つからない（改名・削除したら一覧も直す）: {stale}"


def test_lifespan_tasks_are_classified() -> None:
    """`app.main.lifespan` が呼ぶ処理を足したら、要求の外で動く処理として分類する。"""
    lifespan = next(
        node
        for node in ast.walk(_tree("app.main"))
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan"
    )
    called = {
        node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
    }
    for task in API_LIFESPAN_TASKS:
        assert task in called, f"lifespan が呼ばなくなった処理が一覧に残っている: {task}"
    # 背景の task として動かす coroutine は、一覧に分類があること。
    spawned = {
        _spawned_name(node.args[0])
        for node in ast.walk(lifespan)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_task"
        and node.args
    }
    unclassified = sorted(spawned - set(API_LIFESPAN_TASKS))
    assert not unclassified, (
        f"lifespan の背景の task を API_LIFESPAN_TASKS に分類する: {unclassified}"
    )


def _spawned_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    return ast.dump(node)


class _EnteredSystemAccess(Exception):
    """`system_access_context` に入った印（入口の本体は実行しない）。"""


@contextmanager
def _record_system_access() -> Iterator[None]:
    raise _EnteredSystemAccess
    yield  # pragma: no cover - 入った時点で止める


def _load_entry_module(module: str) -> ModuleType:
    if module.startswith("scripts."):
        path = _module_paths()[module]
        spec = importlib.util.spec_from_file_location(f"_entry_{path.stem}", path)
        assert spec is not None and spec.loader is not None
        loaded = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(loaded)
        return loaded
    return importlib.import_module(module)


def _call_main(main: Callable[..., object], argv: tuple[str, ...]) -> None:
    target = inspect.unwrap(main)
    if inspect.signature(target).parameters:
        main(list(argv))
    else:
        main()


def _declared_unlisted_entry_points() -> list[str]:
    return sorted(
        module
        for module in _scoped_entry_points()
        if _main_is_declared(module) and module not in SYSTEM_ACCESS_ENTRY_POINTS
    )


@pytest.mark.parametrize("module", sorted(SYSTEM_ACCESS_ENTRY_POINTS), ids=str)
def test_system_entry_point_enters_system_access(
    module: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _assert_enters_system_access(module, SYSTEM_ACCESS_ENTRY_POINTS[module], monkeypatch)


def test_declared_unlisted_entry_points_enter_system_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for module in _declared_unlisted_entry_points():
        _assert_enters_system_access(module, (), monkeypatch)


def _assert_enters_system_access(
    module: str, argv: tuple[str, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.rag.request_context as request_context

    loaded = _load_entry_module(module)
    # `with_system_access` は request_context の名前を、context manager を直接使う入口は自分の
    # module の名前を引く。どちらも入った時点で止め、入口の本体（DB・AI）は動かさない。
    monkeypatch.setattr(request_context, "system_access_context", _record_system_access)
    if hasattr(loaded, "system_access_context"):
        monkeypatch.setattr(loaded, "system_access_context", _record_system_access)
    # 入口が process の logging を設定し直さないようにする（ほかのテストの caplog を壊さない）。
    for name in ("configure_logging", "configure_cli_logging"):
        if hasattr(loaded, name):
            monkeypatch.setattr(loaded, name, lambda *_args, **_kwargs: None)
    with pytest.raises(_EnteredSystemAccess):
        _call_main(loaded.main, argv)


# ---- 文脈の無い範囲の読み取りの警告（#3334） ----


def _read_document_scope_without_context() -> list[str]:
    from app.clients.oracle import _oracle_access_predicates

    return _oracle_access_predicates()


def test_scoped_read_without_context_warns_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.rag.request_context as request_context

    monkeypatch.setattr(request_context, "_NO_ACCESS_READ_WARNED", set())
    with caplog.at_level(logging.WARNING, logger="app.rag.request_context"):
        # 文脈を何も設定していない context（要求の外の入口が context を作らなかったとき）。
        first = contextvars.Context().run(_read_document_scope_without_context)
        second = contextvars.Context().run(_read_document_scope_without_context)
    assert "1 = 0" in first and "1 = 0" in second
    warnings = [record for record in caplog.records if record.message == "no_access_context_read"]
    # 文書の条件は文書と KB の範囲の 2 つを読む。2 回読んでも、読み手ごとに 1 回だけ出す。
    assert sorted(str(getattr(record, "reader", "")) for record in warnings) == [
        "document_scope",
        "knowledge_base_scope",
    ]


@pytest.mark.parametrize("kind", ["system", "request"], ids=str)
def test_scoped_read_with_context_does_not_warn(
    kind: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.rag.request_context as request_context

    monkeypatch.setattr(request_context, "_NO_ACCESS_READ_WARNED", set())

    def read() -> list[str]:
        if kind == "system":
            with request_context.system_access_context():
                return _read_document_scope_without_context()
        # production の要求の初期値（範囲は空の集合）も、context を作った要求なので警告しない。
        token = request_context.set_audit_request_context(
            request_context.without_access_scope(request_context.AuditRequestContext())
        )
        try:
            return _read_document_scope_without_context()
        finally:
            request_context.reset_audit_request_context(token)

    with caplog.at_level(logging.WARNING, logger="app.rag.request_context"):
        predicates = contextvars.Context().run(read)
    assert ("1 = 0" in predicates) is (kind == "request")
    assert not [record for record in caplog.records if record.message == "no_access_context_read"]


# ---- 要求から始まって全件を扱う後台の処理 ----


async def test_section_rule_refresh_runs_under_system_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """章節の規則の保存の後の全文書の作り直しは、保存した利用者の範囲で絞らない（#3334）。"""
    import app.rag.section_map_service as section_map_service
    from app.rag.request_context import (
        AuditRequestContext,
        current_audit_request_context,
        reset_audit_request_context,
        set_audit_request_context,
    )

    seen: list[AuditRequestContext] = []

    async def fake_refresh_all(*, reason: str, oracle: object = None) -> int:
        seen.append(current_audit_request_context())
        return 0

    monkeypatch.setattr(section_map_service, "refresh_all_section_maps", fake_refresh_all)
    monkeypatch.setattr(section_map_service, "_refresh_all_task", None)
    # 対象範囲を 1 つのナレッジベースに絞られた利用者の要求の中で保存した。
    token = set_audit_request_context(
        AuditRequestContext(
            user_id_hash="user-a",
            allowed_document_ids=frozenset(),
            allowed_category_names=frozenset(),
            allowed_knowledge_base_ids=frozenset({"kb-a"}),
            allowed_search_answer_profile_ids=frozenset({"profile-a"}),
        )
    )
    try:
        section_map_service.schedule_refresh_all_section_maps(reason="section_rules_saved")
        task = section_map_service._refresh_all_task
        assert task is not None
        await asyncio.wait_for(task, timeout=5)
    finally:
        reset_audit_request_context(token)
    assert len(seen) == 1
    context = seen[0]
    assert context.allowed_document_ids is None
    assert context.allowed_knowledge_base_ids is None
    assert context.allowed_search_answer_profile_ids is None
    assert context.user_id_hash is None
