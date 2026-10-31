"""pytest 共通設定。

ローカル `.env` は実 Oracle smoke 用に `NL2SQL_RUNTIME_MODE=oracle` へ切り替えられる。
単体テストは CI と同じ deterministic/memory 実行に固定し、開発者環境の `.env` に依存させない。
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import functools
import os
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from pr_backend_core.config import isolate_env_files_for_tests

# 手元の `.env`（共通・製品）を読ませない。`app` の import の前に呼ぶ（3 製品で同じ。#1972）。
isolate_env_files_for_tests("NL2SQL_")

os.environ["PLATFORM_ENVIRONMENT"] = "local"
os.environ["PLATFORM_AUTH_MODE"] = "local"


os.environ["NL2SQL_ENABLE_METRICS"] = "false"
os.environ["NL2SQL_RUNTIME_MODE"] = "deterministic"
os.environ["NL2SQL_PERSISTENCE_MODE"] = "memory"
os.environ["PLATFORM_ORACLE_USER"] = "APP"
# 業務データの SELECT の間だけ session の時間帯を切り替える経路（#3265）を通すため、
# 既定（UTC。#3274）ではない時間帯にする。UTC のときに切り替えないことは個別のテストで確かめる。
os.environ["PLATFORM_TIMEZONE"] = "Asia/Tokyo"
# 本番の Argon2 のコスト（time_cost=3・64 MiB・並列 4）は 1 回のハッシュに約 70ms かかる。
# テストは RAG / Agent のテスト（security_support.py）と同じ軽い値にする（#344）。
os.environ["PLATFORM_AUTH_ARGON2_TIME_COST"] = "1"
os.environ["PLATFORM_AUTH_ARGON2_MEMORY_KIB"] = "8192"
os.environ["PLATFORM_AUTH_ARGON2_PARALLELISM"] = "1"


async def _run_sync_in_test_thread[T](
    func: Callable[..., T],
    /,
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run sync FastAPI callables off-loop without AnyIO's test-time worker hang.

    第 1 引数は Starlette の ``run_in_threadpool`` と同じく位置専用にする。fastapi 0.142 は
    ``run_in_threadpool(_run_sync_endpoint, function=..., arguments=...)`` と呼ぶため、
    名前で受けると ``function`` が衝突する(#1673)。
    """

    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()
    context = contextvars.copy_context()
    call = functools.partial(func, *args, **kwargs)

    def complete(ok: bool, value: T | BaseException) -> None:
        if future.cancelled():
            return
        if ok:
            future.set_result(value)  # type: ignore[arg-type]
            return
        future.set_exception(value)  # type: ignore[arg-type]

    def run() -> None:
        try:
            result = context.run(call)
        except BaseException as exc:
            loop.call_soon_threadsafe(complete, False, exc)
            return
        loop.call_soon_threadsafe(complete, True, result)

    threading.Thread(target=run, name="pytest-fastapi-sync-call", daemon=True).start()
    while not future.done():
        await asyncio.sleep(0.001)
    return future.result()


def _install_test_threadpool_for_asgi_tests() -> None:
    import fastapi.dependencies.utils as fastapi_dependency_utils
    import fastapi.routing as fastapi_routing
    import starlette.concurrency as starlette_concurrency

    fastapi_dependency_utils.run_in_threadpool = _run_sync_in_test_thread  # type: ignore[attr-defined, assignment]
    fastapi_routing.run_in_threadpool = _run_sync_in_test_thread  # type: ignore[attr-defined, assignment]
    starlette_concurrency.run_in_threadpool = _run_sync_in_test_thread  # type: ignore[assignment]


_install_test_threadpool_for_asgi_tests()


@pytest.fixture(autouse=True)
def _restore_settings_singleton() -> Iterator[None]:
    """Settings の singleton の値を、テストの後にテストの前の値へ戻す。

    システム設定の API（データベース設定の PATCH など）は singleton の属性をその場で書き換える。
    monkeypatch で先に退避していない属性（`oracle_user` など）は次のテストに残り、
    実行の順序や xdist の振り分けで結果が変わっていた（例: 現在の schema owner が `ADMIN`）。
    ほかの autouse の fixture（monkeypatch を使うもの）より先に定義し、最後に戻す。
    """
    from app.settings import _settings_singleton

    settings = _settings_singleton()
    saved_fields = copy.deepcopy(settings.__dict__)
    saved_private = copy.deepcopy(settings.__pydantic_private__)
    saved_fields_set = set(settings.__pydantic_fields_set__)
    yield
    settings.__dict__.clear()
    settings.__dict__.update(saved_fields)
    object.__setattr__(settings, "__pydantic_private__", saved_private)
    object.__setattr__(settings, "__pydantic_fields_set__", saved_fields_set)


@pytest.fixture(autouse=True)
def _reset_cli_connect_mode() -> Iterator[None]:
    """CLI の main が有効にする CLI の接続（#1779）を、テストごとに戻す（順序に依らない）。"""
    from pr_backend_core.oracle_dsn import disable_cli_connect_mode

    disable_cli_connect_mode()
    yield
    disable_cli_connect_mode()


@pytest.fixture(autouse=True)
def _isolate_model_secret_env_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """設定の読み書きを、開発者の backend/.env と共通 .env から切り離す（#103 / #211）。

    共通 .env（API key・システム設定画面の保存先）と model-settings.json は tmp_path に置く。
    """
    import app.settings as app_settings

    monkeypatch.setattr(app_settings, "BACKEND_ENV_FILE", tmp_path / "backend.env")
    monkeypatch.setattr(app_settings, "PLATFORM_ENV_FILE", tmp_path / "platform.env")


@pytest.fixture(autouse=True)
def _fresh_nl2sql_service_state() -> None:
    """module の singleton `nl2sql_service` の状態をテストごとに初期状態へ戻す。

    テストの singleton は memory store で動き、catalog・プロファイル・ジョブ・履歴を process の
    中で持ち続ける。API を通すテスト（サンプルの取り込み・catalog の作り直しなど）の書き込みが
    同じ process の後のテストに残り、実行の順序や xdist の振り分けで結果が変わっていた。
    router・MCP・quality evaluation などが同じ object を import しているため、module の属性は
    差し替えず、新しく作った service の状態で中身だけを置き換える（`__dict__` の代入 1 回）。
    """
    from app.features.nl2sql.service import Nl2SqlService, nl2sql_service

    nl2sql_service.__dict__ = Nl2SqlService().__dict__


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """retry の間の待ちを実時間で待たない（#344）。

    逆生成の段階 retry は 1 秒 → 2 秒待つため、失敗の経路を通るテストが 1 件 3 秒かかっていた。
    待ち時間そのものを確かめるテストは、テストの中で `_retry_sleep` を差し替え直してよい。
    """
    import app.features.nl2sql.reverse_generation as reverse_generation

    monkeypatch.setattr(reverse_generation, "_retry_sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def _bounded_ai_calls_for_scripted_clients(monkeypatch: pytest.MonkeyPatch) -> None:
    """一括の LLM の呼び出しの実行器（#2004）を、台本どおりに応答するスタブに合わせる。

    スタブの client は呼ばれた順に応答・記録するため、業務プロファイルの AI 構築の抽出の組は
    1 件ずつ呼ぶ（並行は、テストの中で `nl2sql_ontology_build_concurrency` を上げて確かめる）。
    再試行の待ちは実時間で待たない。
    """
    import app.features.nl2sql.ai_calls as ai_calls
    from app.settings import get_settings

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ai_calls, "_sleep", no_wait)
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_build_concurrency", 1)


@pytest.fixture(autouse=True)
def _fresh_database_status_cache() -> Iterator[None]:
    """DB の状態 API の `ok` の cache（#793）をテストごとに捨てる（テストの順序に依らない）。"""
    from pr_system_settings.database_status import clear_database_status_cache

    clear_database_status_cache()
    yield
    clear_database_status_cache()


@pytest.fixture(autouse=True)
def _fresh_dropped_llm_parameters() -> Iterator[None]:
    """モデルが受け付けない引数の記憶（#3539。プロセスの間の状態）をテストごとに捨てる。"""
    from pr_backend_core.llm_request_params import reset_dropped_parameters

    reset_dropped_parameters()
    yield
    reset_dropped_parameters()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "rule_parser: 公開前の確認で、決まった形の Markdown を決定論で読む経路を使う（#1729）",
    )
    config.addinivalue_line(
        "markers",
        "parse_cache: 公開前の確認で、自由な行の LLM の解析の結果を使い回す（#1735）",
    )


@pytest.fixture(autouse=True)
def _markdown_parse_cache(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """既定のテストは、スタブの LLM の応答を変えて解析し直すため、解析の結果を使い回さない。

    使い回し（同じ Markdown と schema なら同じ結果。#1735）は `@pytest.mark.parse_cache` を
    付けたテストで確かめる。
    """
    if request.node.get_closest_marker("parse_cache"):
        return
    from app.features.nl2sql.ontology_markdown_workspace import MarkdownOntologyWorkspace

    monkeypatch.setattr(MarkdownOntologyWorkspace, "_cached_free_lines_parse", lambda *_args: None)


@pytest.fixture(autouse=True)
def _markdown_rule_parser(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """公開前の確認の Markdown の解析は、既定のテストでは LLM の経路（スタブ）を通す。

    決まった形の Markdown を決定論で読む経路（`ontology_markdown_parser`）は、
    `@pytest.mark.rule_parser` を付けたテストで確かめる（#1729）。
    """
    if request.node.get_closest_marker("rule_parser"):
        return
    import app.features.nl2sql.ontology_markdown_parser as parser

    def all_free(markdown: str) -> parser.RuleParse:
        # すべての行を「決まった形で読めない行」にして、LLM の経路（スタブ）に渡す。
        return parser.RuleParse(
            free_lines=[i for i, line in enumerate(markdown.splitlines(), 1) if line.strip()]
        )

    monkeypatch.setattr(parser, "parse_rendered_markdown", lambda _markdown: None)
    monkeypatch.setattr(parser, "parse_markdown_rules", all_free)
