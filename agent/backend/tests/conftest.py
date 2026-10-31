"""テスト全体の共通設定（#750）。

共通認証の service は local でも Oracle（`PLATFORM_*`）を使うため、テストは既定で InMemory の
store に差し替える（開発機の共通 `.env` の DB に触れない）。store の選択を確かめるテストは
`set_security_service(None)` で既定に戻してから確かめる。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

from pr_backend_core.config import isolate_env_files_for_tests

# 手元の `.env`（共通・製品）を読ませない。`app` の import の前に呼ぶ（3 製品で同じ。#1972）。
isolate_env_files_for_tests("AGENT_")

os.environ["PLATFORM_ENVIRONMENT"] = "local"
os.environ["PLATFORM_AUTH_MODE"] = "local"


# 保存先の既定は `auto`（#839）。開発機の共通 `.env` に DB の設定があると、Run の repository が
# Oracle に接続するため、テストは memory に固定する（`auto` を確かめるテストは settings を差し替えて
# `storage_backend.reset()` する）。`app` を import する前に設定する。
os.environ["AGENT_RUNTIME_REPOSITORY_BACKEND"] = "memory"

import pytest  # noqa: E402
from pr_backend_core.llm_request_params import reset_dropped_parameters  # noqa: E402
from pr_system_settings.database_status import clear_database_status_cache  # noqa: E402

from app.features.agent import (  # noqa: E402
    run_facts_store,
    storage_backend,
    storage_bootstrap,
    tools,
)
from app.features.agent import runtime as agent_runtime  # noqa: E402
from app.security.service import SecurityService, set_security_service  # noqa: E402
from app.security.store import InMemorySecurityStore  # noqa: E402
from app.settings import get_settings  # noqa: E402

# Run の repository の保存（#1939）: テストは event loop の上の変更も書き終えてから戻し（保存先を
# すぐに確かめられる）、変えた Run・業務 Agent の印の付け忘れを保存のたびに検出する。
agent_runtime.BACKGROUND_WRITES = False
agent_runtime.VERIFY_CHANGE_TRACKING = True


@pytest.fixture(autouse=True)
def _in_memory_security_service() -> Iterator[None]:
    store = InMemorySecurityStore()
    store._ensure_system_admin_role()
    set_security_service(SecurityService(store, get_settings()))
    yield
    set_security_service(None)


@pytest.fixture(autouse=True)
def _reset_cli_connect_mode() -> Iterator[None]:
    """CLI の main が有効にする CLI の接続（#1779）を、テストごとに戻す（順序に依らない）。"""
    from pr_backend_core.oracle_dsn import disable_cli_connect_mode

    disable_cli_connect_mode()
    yield
    disable_cli_connect_mode()


@pytest.fixture(autouse=True)
def _fresh_database_status_cache() -> Iterator[None]:
    """DB の状態 API の `ok` の cache（#793）をテストごとに捨てる（テストの順序に依らない）。"""
    clear_database_status_cache()
    yield
    clear_database_status_cache()


@pytest.fixture(autouse=True)
def _fresh_dropped_llm_parameters() -> Iterator[None]:
    """モデルが受け付けない引数の記憶（#3539。プロセスの間の状態）をテストごとに捨てる。"""
    reset_dropped_parameters()
    yield
    reset_dropped_parameters()


@pytest.fixture(autouse=True)
def _no_run_facts_store() -> Iterator[None]:
    """Run の事実（#794）は既定で保存しない（構成を変えるテストが Oracle を選ばないように）。

    保存先を確かめるテストは `run_facts_store.configure` / `reset` で差し替える。
    """
    run_facts_store.configure(None)
    yield
    run_facts_store.configure(None)


@pytest.fixture(autouse=True)
def _fresh_storage_backend_decision() -> Iterator[None]:
    """保存先の `auto` の判定（#839）をテストごとにやり直す。"""
    storage_backend.reset()
    yield
    storage_backend.reset()


@pytest.fixture(autouse=True)
def _storage_loaded() -> Iterator[None]:
    """Run・定義の保存先の読み込み（#1212）は済んだものとする（API の要求で定義を復元しない）。

    読み込みを確かめるテストは `storage_bootstrap.reset()` で未読み込みに戻す。
    """
    storage_bootstrap.reset(ready=True)
    yield
    storage_bootstrap.reset(ready=True)


@pytest.fixture(autouse=True)
def _no_mcp_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP の再試行の待ち（#854）はテストでは待たない。"""
    monkeypatch.setattr(tools, "_retry_sleep", lambda _seconds: None)


@pytest.fixture(autouse=True)
def _stub_upload_storage_object_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """アップロード保存先の保存の前の確認（#2101）で、実の OCI を呼ばない（既定は成功）。

    失敗を確かめるテストは `default_object_storage_client` をさらに差し替える。
    """
    from pr_system_settings import upload_storage_check

    class _ObjectStorageStub:
        def head_bucket(self, *_args: object, **_kwargs: object) -> None:
            return None

        def put_object(self, *_args: object, **_kwargs: object) -> None:
            return None

        def get_object(self, *_args: object, **_kwargs: object) -> None:
            return None

        def delete_object(self, *_args: object, **_kwargs: object) -> None:
            return None

    monkeypatch.setattr(
        upload_storage_check,
        "default_object_storage_client",
        lambda _settings: _ObjectStorageStub(),
    )
