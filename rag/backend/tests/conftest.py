"""pytest 共通 fixture。

アプリ（`app`）と、アプリを import するテスト補助（`tests._ai_stubs` など）は、fixture の中で
import する。conftest は xdist（`-n`）の controller も読み込むが、controller はテストを収集・
実行しない。module の先頭で import すると、worker の起動の前に約 2 秒の import を直列に待つ
（#401）。
"""

from __future__ import annotations

import atexit
import fcntl
import gc
import os
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pr_backend_core.config import isolate_env_files_for_tests

# 手元の `.env`（共通・製品）を読ませない。`app` の import の前に呼ぶ（3 製品で同じ。#1972）。
isolate_env_files_for_tests("RAG_")

os.environ["PLATFORM_ENVIRONMENT"] = "local"
os.environ["PLATFORM_AUTH_MODE"] = "local"


if TYPE_CHECKING:
    from app.config import Settings
    from app.rag.request_context import AuditRequestContext
    from tests.trace_store_support import FakeTraceDb

# プロセスの終了時（pytest の後始末と、ほかの atexit の処理の後）に、残っているオブジェクトを GC の
# 対象から外す。Python の終了処理の GC が、アプリとテストで作った大量のオブジェクトをたどって
# 1 プロセス 1〜2 秒かかり、xdist の controller は全 worker の終了をこれだけ待っていた（#401）。
# 循環参照のごみの `__del__` が終了時に呼ばれなくなるだけで、テストの結果には影響しない。
atexit.register(gc.freeze)


@pytest.fixture(scope="session", autouse=True)
def _shared_httpx_default_tls_context() -> Iterator[None]:
    """httpx の既定の TLS 設定（CA 証明書の読み込み）を worker ごとに 1 回だけ作り、使い回す。

    `httpx.Client()` は作るたびに certifi の CA 証明書を読み込む（約 20ms）。検索・取込のステージ
    （chunking・retrieval 等）の remote 委譲は既定で ON（`http://127.0.0.1:1803x`）で、
    `Settings.model_construct(...)` などで作ったテストの設定では、パイプラインを 1 回流すごとに
    数回 `httpx.Client()` を作り、接続を拒否されて in-process へ縮退する。この読み込みだけで
    全体の約 6 秒を占めていた（#401）。既定（`verify=True`・`cert=None`・`trust_env=True`、
    `SSL_CERT_FILE` / `SSL_CERT_DIR` なし）のときだけ同じ設定を返し、それ以外は httpx に任せる。
    """
    import os
    import ssl

    import httpx
    from httpx._transports import default as httpx_transports

    original = httpx_transports.create_ssl_context  # type: ignore[attr-defined]
    shared: list[ssl.SSLContext] = []

    def create_ssl_context(
        verify: ssl.SSLContext | str | bool = True,
        cert: object = None,
        trust_env: bool = True,
    ) -> ssl.SSLContext:
        uses_default = verify is True and cert is None and trust_env
        if not uses_default or os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
            return original(verify=verify, cert=cert, trust_env=trust_env)  # type: ignore[arg-type]
        if not shared:
            shared.append(httpx.create_ssl_context())
        return shared[0]

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(httpx_transports, "create_ssl_context", create_ssl_context)
        yield


# xdist の worker が nodeid に group 名を付ける hook より先に marker を付ける。
@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """実 Oracle を使うテスト（`oracle_db`）は、xdist でも 1 つの worker で直列に実行する（#344）。

    `cleanup_to_baseline` は baseline にない行をすべて消すため、別の worker の実 Oracle の
    テストと同時に動くと互いの行を消す。`--dist loadgroup`（pyproject の addopts）で効く。
    """
    for item in items:
        if "oracle_db" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.xdist_group("oracle"))


@contextmanager
def _oracle_schema_lock(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """xdist の worker の間で共有するスキーマの準備のロック（basetemp の親は worker で共通）。"""
    lock_path = tmp_path_factory.getbasetemp().parent / "rag-oracle-schema.lock"
    with lock_path.open("w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        yield


@pytest.fixture
def oracle_schema_locked(
    oracle_db: None, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """テストの間、別の worker の開始時のスキーマの準備（`ensure_schema`）を待たせる（#2450）。

    表の形や台帳を一時的に戻すテストが使う。実 Oracle のテストどうしは 1 つの worker で直列に
    動くが、別の worker が遅れて開始すると、途中の形を見て「作成・更新」を当ててしまう。
    """
    with _oracle_schema_lock(tmp_path_factory):
        yield


@pytest.fixture(scope="session", autouse=True)
def _oracle_db_session(tmp_path_factory: pytest.TempPathFactory) -> None:
    """実 Oracle が使えるならスキーマを保証し baseline を記録する。"""
    from app.config import get_settings
    from tests import _oracle_test_db

    if not _oracle_test_db.db_available():
        return
    _oracle_test_db.apply_real_oracle_settings(get_settings())
    # xdist の worker は同時に起動するため、スキーマの作成（DDL）は 1 つずつにする（#344）。
    with _oracle_schema_lock(tmp_path_factory):
        skip_reason = _oracle_test_db.ensure_schema()
    if skip_reason is not None:
        # データを消す未適用の migration は当てない。実 Oracle のテストは `oracle_db` で
        # skip する（#619）。
        warnings.warn(skip_reason, stacklevel=1)
        return
    _oracle_test_db.capture_baseline()


@pytest.fixture(autouse=True)
def _reset_cli_connect_mode() -> Iterator[None]:
    """CLI の main が有効にする CLI の接続（#1779）を、テストごとに戻す（順序に依らない）。"""
    from pr_backend_core.oracle_dsn import disable_cli_connect_mode

    disable_cli_connect_mode()
    yield
    disable_cli_connect_mode()


@pytest.fixture(autouse=True)
def isolated_local_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """各テストで保存先と runtime の状態を分離する。"""
    from app import config as app_config
    from app.api.routes import settings as settings_routes
    from app.config import get_settings
    from app.rag.guardrail_adapter import reset_guardrail_static_cache
    from app.rag.rate_limit import reset_rate_limiter
    from app.rag.vector_index_adapter import reset_vector_index_static_cache
    from app.security.service import set_security_service
    from app.services import control as service_control

    monkeypatch.setenv("PLATFORM_MODEL_SETTINGS_FILE", str(tmp_path / "model-settings.json"))
    monkeypatch.setenv("RAG_PARSER_SETTINGS_FILE", str(tmp_path / "parser-settings.json"))
    # 言語資源の全体の上書き(#2181)は、テストごとに組み込みのパックへ戻す。
    from app.rag.language_pack_store import LANGUAGE_PACK_STORE

    LANGUAGE_PACK_STORE.reset()
    # 共通 .env（モデルの API key・DB / 保存先の画面保存先）と RAG の backend/.env
    # （parser の API key・RAG 固有の画面保存先）は tmp へ分離する（#211）。
    monkeypatch.setattr(app_config, "PLATFORM_ENV_FILE", tmp_path / "platform.env")
    monkeypatch.setattr(app_config, "BACKEND_ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(settings_routes, "BACKEND_ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("PLATFORM_ENTERPRISE_AI_API_KEY", raising=False)
    monkeypatch.delenv("PLATFORM_ENTERPRISE_AI_SECONDARY_API_KEY", raising=False)
    set_security_service(None)
    reset_rate_limiter()
    reset_guardrail_static_cache()
    reset_vector_index_static_cache()
    _reset_runtime_settings(get_settings(), tmp_path)
    # サービス管理の systemctl / journalctl / sudo は実行しない(手元の systemd を触らない。#286)。
    # 既定は「systemd を使えない」扱いにし、必要なテストだけ fake を差し込む。
    monkeypatch.setattr(service_control, "run_command", _systemd_unavailable)
    monkeypatch.setattr(
        get_settings(), "rag_service_runtime_env_file", str(tmp_path / "service-runtime.env")
    )


async def _systemd_unavailable(argv: list[str], timeout: float) -> object:
    from app.services.systemd import CommandUnavailableError

    raise CommandUnavailableError(f"{argv[0]} はテストでは実行しません。")


def _skip_or_fail_oracle(reason: str) -> None:
    """実 Oracle を使えないときは skip する。`RAG_TEST_REQUIRE_ORACLE=1` なら失敗にする。

    実 Oracle の結合テストの CI（oracle-integration.yml）は DB を用意して実行するので、
    届かない・準備が足りないのは CI の壊れ。skip のまま成功にすると、テストが壊れたまま
    気づかれない（#1595）。
    """
    if os.environ.get("RAG_TEST_REQUIRE_ORACLE") == "1":
        pytest.fail(f"RAG_TEST_REQUIRE_ORACLE=1 のため失敗にします: {reason}", pytrace=False)
    pytest.skip(reason)


@pytest.fixture
def trace_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeTraceDb]:
    """共通の span の表の fake（書き手と読み出しの接続。#3226。実 DB は使わない）。"""
    from tests.trace_store_support import install_trace_db

    yield from install_trace_db(monkeypatch)


@pytest.fixture
def system_access() -> Iterator[None]:
    """要求の外から route の内部の関数を呼ぶテストを、範囲の制限の無い context で動かす（#3250）。

    文脈の無い呼び出しは何も使えない（`NO_ACCESS_AUDIT_REQUEST_CONTEXT`）ので、範囲と関係の無い
    処理を確かめるテストだけが明示して使う。
    """
    from app.rag.request_context import system_access_context

    with system_access_context():
        yield


@pytest.fixture
def oracle_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """実 Oracle AI Database を使う統合テスト用。未到達なら skip し、作成行を後始末する。

    Oracle は実 DB を使うが、VLM/embedding/rerank/LLM は決定論スタブへ差し替える。
    `isolated_local_state` が Oracle 接続設定を初期化した後に実値を再適用するため、
    autouse より後に動く本 fixture で上書きしている。
    """
    from dataclasses import replace

    from app.config import get_settings
    from app.rag.request_context import (
        NO_ACCESS_AUDIT_REQUEST_CONTEXT,
        audit_request_context_from_headers,
        current_audit_request_context,
        reset_audit_request_context,
        set_audit_request_context,
    )
    from tests import _ai_stubs, _oracle_test_db
    from tests.support import TEST_REQUEST_HEADERS

    if not _oracle_test_db.db_available():
        _skip_or_fail_oracle("実 Oracle AI Database に未到達のため統合テストをスキップします。")
    skip_reason = _oracle_test_db.schema_skip_reason()
    if skip_reason is not None:
        _skip_or_fail_oracle(skip_reason)
    settings = get_settings()
    # 実 Oracle の接続の項目だけを入れる。手元の model-settings.json（文書解析の既定など）は読まない
    # （テストの既定値を上書きして CI と違う経路を通るため。AI は決定論スタブに差し替える。#483）。
    _oracle_test_db.apply_real_oracle_settings(settings)
    _ai_stubs.patch_ai_clients(monkeypatch)
    # local の全権限の利用者と同じく、全員の会話・保存済みの回答を扱う（利用者の分からない
    # context は会話・回答の記録が 0 件になるため、明示する。#3250）。header の dict は大文字の
    # 名前なので、利用者・tenant の hash は付かない（今までどおり）。
    context = replace(
        audit_request_context_from_headers(
            TEST_REQUEST_HEADERS,
            request_id="pytest-oracle-integration",
            settings=settings,
        ),
        answer_records_unrestricted=True,
        all_users_visible=True,
    )

    def current_or_default_context() -> AuditRequestContext:
        current = current_audit_request_context()
        # 文脈の無い処理（文脈を引き継がない thread など）は、何も使えない既定の context になる
        # （#3250）。統合テストでは、その代わりにテストの利用者の context を使う。
        if current is NO_ACCESS_AUDIT_REQUEST_CONTEXT or (
            current.request_id is None
            and current.tenant_id_hash is None
            and current.user_id_hash is None
            and current.allowed_document_ids is None
            and current.allowed_category_names is None
            and current.allowed_knowledge_base_ids is None
        ):
            return context
        return current

    monkeypatch.setattr(
        "app.clients.oracle.current_audit_request_context",
        current_or_default_context,
    )
    # 範囲の条件の context（#3334）も同じ既定にする。
    monkeypatch.setattr(
        "app.clients.oracle.access_scope_context",
        lambda _reader: current_or_default_context(),
    )
    monkeypatch.setattr(
        "app.rag.audit.current_audit_request_context",
        current_or_default_context,
    )
    token = set_audit_request_context(context)
    _oracle_test_db.cleanup_to_baseline()
    try:
        yield
    finally:
        _oracle_test_db.cleanup_to_baseline()
        reset_audit_request_context(token)


def _reset_runtime_settings(settings: Settings, tmp_path: Path) -> None:
    """mutable runtime settings をテスト既定値へ戻す。"""
    settings.oracle_client_lib_dir = str(tmp_path / "instantclient_23_26")
    settings.oracle_wallet_dir = ""
    settings.local_storage_dir = str(tmp_path / "storage")
    settings.max_upload_bytes = 200 * 1024 * 1024
    settings.rate_limit_enabled = True
    settings.auth_mode = "local"
    settings.app_admin_login_user_id = ""
    settings.app_admin_login_user_password = ""
    settings.app_auth_cookie_secure = False
    settings.model_settings_file = str(tmp_path / "model-settings.json")
    settings.rag_parser_settings_file = str(tmp_path / "parser-settings.json")
    settings.enterprise_ai_endpoint = ""
    settings.enterprise_ai_project_id = ""
    settings.enterprise_ai_secondary_endpoint = ""
    settings.enterprise_ai_secondary_project_id = ""
    settings.enterprise_ai_tertiary_endpoint = ""
    settings.enterprise_ai_tertiary_project_id = ""
    # プライマリ接続・セカンダリ接続・ターシャリ接続（#533 / #786）の API key の基準値を空にする。
    settings.prepare_model_secret_state("", "", "")
    settings.enterprise_ai_models = []
    settings.enterprise_ai_default_text_model = ""
    settings.enterprise_ai_default_vision_model = ""
    settings.enterprise_ai_llm_path = "/responses"
    settings.enterprise_ai_vlm_path = "/responses"
    settings.enterprise_ai_llm_payload_template = ""
    settings.enterprise_ai_vlm_payload_template = ""
    settings.enterprise_ai_llm_response_path = ""
    settings.enterprise_ai_vlm_response_path = ""
    settings.enterprise_ai_timeout_seconds = 600.0
    settings.enterprise_ai_max_retries = 3
    settings.enterprise_ai_vlm_input_mode = "files_api"
    settings.rag_parser_adapter_backend = "unstructured"
    settings.rag_parser_docling_enabled = False
    settings.rag_parser_unstructured_enabled = True
    settings.genai_embedding_model = "cohere.embed-v4.0"
    settings.genai_embedding_dim = 1024
    settings.genai_rerank_model = "cohere.rerank-v4.0-fast"
    settings.rag_pdf_segmentation_enabled = True
    settings.rag_pdf_max_pages_per_segment = 10
    settings.rag_pdf_max_segments = 300
    settings.rag_pdf_segment_parse_concurrency = 1
    # 本番既定は False(PREPROCESSED で停止)。既存テストは preprocess を越えて
    # REVIEW まで進む前提なのでテスト既定は自動進行とし、停止挙動は専用テストで検証する。
    settings.rag_auto_parse_after_preprocess_enabled = True


@pytest.fixture(autouse=True)
def _sequential_evaluation_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    """品質評価のケースを 1 件ずつ実行する（#2004）。

    既存のテストは、評価全体の時間の上限・ケースの時間切れ・スタブの呼ばれた順を、1 件ずつの
    実行で確かめる。並行は `tests/test_evaluation_concurrency.py` で `evaluation_concurrency` を
    差し替え直して確かめる。
    """
    import app.rag.evaluation as evaluation

    monkeypatch.setattr(evaluation, "evaluation_concurrency", lambda _settings: 1)


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


@pytest.fixture(autouse=True)
def published_language_unit_scope(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP 単体テストだけに公開版の依存を明示供給する。実 DB テストは除外する。"""
    if "oracle_db" in request.fixturenames:
        return
    from app.rag import published_language_runtime
    from tests.language_release_api_support import FakeReleaseStore

    store = FakeReleaseStore()

    async def initialized() -> FakeReleaseStore:
        return store

    monkeypatch.setattr(published_language_runtime, "initialized_release_store", initialized)


@pytest.fixture
def trace_body_enabled() -> Iterator[None]:
    """本文の表示を検証するテストだけ明示的に記録を有効にする。"""
    from pr_backend_core.observability.call_policy import call_content_policy

    with call_content_policy(lambda: True):
        yield
