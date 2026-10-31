"""設定値の安全な制約テスト。"""

import re
import tomllib
from pathlib import Path

import pytest
from pr_backend_core.config import settings_env_names
from pydantic import ValidationError
from rag_pipeline_core.chunking import CHUNK_OVERLAP_MAX_CHARS, CHUNK_SIZE_MAX_CHARS

from app import config as config_module
from app.config import (
    DEFAULT_LOCAL_STORAGE_DIR,
    DEFAULT_MODEL_SETTINGS_FILE,
    DEFAULT_PARSER_ADAPTER_BACKEND,
    EnterpriseAiConfiguredModel,
    Settings,
    enterprise_ai_output_token_budget,
    resolve_model_settings_file,
)


def test_chunking_strategy_defaults_to_small_to_big() -> None:
    """Chunking アダプターの既定戦略は親子階層（small-to-big）。

    #594。回答が親子の文脈を前提にする。
    """
    settings = Settings()
    assert settings.rag_chunking_strategy == "small_to_big"
    assert settings.rag_chunk_min_chars == 120
    assert settings.rag_chunk_delimiter == "<<<JSONL_RECORD_BOUNDARY>>>"


def test_chunk_min_chars_must_be_smaller_than_chunk_size() -> None:
    """適用 strategy では min_chars の誤設定を起動時に拒否する。"""
    with pytest.raises(ValidationError):
        Settings(
            rag_chunking_strategy="structure_aware",
            rag_chunk_size=300,
            rag_chunk_min_chars=300,
        )
    assert Settings(
        rag_chunking_strategy="fixed_size",
        rag_chunk_size=800,
        rag_chunk_min_chars=800,
    )


def test_removed_parent_child_strategy_reads_as_small_to_big(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """削除した親子階層の保存値は親子階層（small-to-big）として読む。子サイズの旧変数は読まない。"""
    monkeypatch.setenv("RAG_CHUNKING_STRATEGY", "hierarchical_parent_child")
    monkeypatch.setenv("RAG_CHUNK_CHILD_SIZE", "900")
    settings = Settings()
    assert settings.rag_chunking_strategy == "small_to_big"
    assert not hasattr(settings, "rag_chunk_child_size")


def test_chunking_small_to_big_params_match_rag_poc_defaults_and_ranges() -> None:
    """親子階層（small-to-big）の既定値と範囲は rag_engine.chunking.constants と一致する。"""
    from rag_engine.chunking import constants as engine_constants

    settings = Settings()
    pairs = {
        "rag_chunk_child_target_chars": (
            engine_constants.DEFAULT_CHILD_TARGET_CHARS,
            engine_constants.CHILD_TARGET_CHARS_RANGE,
        ),
        "rag_chunk_table_child_target_chars": (
            engine_constants.DEFAULT_TABLE_CHILD_TARGET_CHARS,
            engine_constants.TABLE_CHILD_TARGET_CHARS_RANGE,
        ),
        "rag_chunk_parent_target_chars": (
            engine_constants.DEFAULT_PARENT_TARGET_CHARS,
            engine_constants.PARENT_TARGET_CHARS_RANGE,
        ),
        "rag_chunk_parent_max_pages": (
            engine_constants.DEFAULT_PARENT_MAX_PAGES,
            engine_constants.PARENT_MAX_PAGES_RANGE,
        ),
        "rag_chunk_parent_max_children": (
            engine_constants.DEFAULT_PARENT_MAX_CHILDREN,
            engine_constants.PARENT_MAX_CHILDREN_RANGE,
        ),
    }
    for field, (default, (minimum, maximum, _step)) in pairs.items():
        assert getattr(settings, field) == default, field
        Settings.model_validate({field: minimum})
        Settings.model_validate({field: maximum})
        with pytest.raises(ValidationError):
            Settings.model_validate({field: minimum - 1})
        with pytest.raises(ValidationError):
            Settings.model_validate({field: maximum + 1})


def test_unknown_chunking_strategy_is_rejected() -> None:
    """未知の chunking 戦略名は Literal で拒否する。"""
    with pytest.raises(ValidationError):
        Settings(rag_chunking_strategy="semantic_double_pass")


def test_vector_index_profile_defaults_to_accurate() -> None:
    """Vector Index アダプターの既定は高精度（accurate。検索時 target accuracy 98。#272）。"""
    assert Settings().rag_vector_index_profile == "accurate"


def test_unknown_vector_index_profile_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(rag_vector_index_profile="ivf_flat")


def test_evaluation_suite_defaults_to_standard() -> None:
    """評価の基準の既定は標準(#591)。"""
    assert Settings().rag_evaluation_suite == "standard"


def test_answer_flow_defaults_to_standard_rag_in_every_layer() -> None:
    """回答の生成方式の既定は標準 RAG。CRAG は明示して選んだときだけ使う(#3509)。

    backend の全体設定・.env.example・rag_engine の既定(answer_flow を省いた呼び出し)をそろえる。
    """
    from rag_engine.generation.answer_models import DEFAULT_ANSWER_FLOW, STANDARD_ANSWER_FLOW

    assert Settings.model_fields["rag_answer_flow"].default == "standard_rag"
    assert DEFAULT_ANSWER_FLOW == STANDARD_ANSWER_FLOW == "standard_rag"
    example = (config_module.BACKEND_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "RAG_ANSWER_FLOW=standard_rag" in example.splitlines()


def test_unknown_evaluation_suite_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(rag_evaluation_suite="autorag_tuner")


def test_graph_profile_defaults_to_off() -> None:
    """文書構造索引の構築の既定は off(構築しない)。"""
    assert Settings().rag_graph_profile == "off"


def test_automatic_document_stage_progression_is_enabled_by_default() -> None:
    settings = Settings()
    assert settings.rag_auto_parse_after_preprocess_enabled is True
    assert settings.rag_auto_chunk_after_extract_enabled is True
    assert settings.rag_auto_index_after_chunk_enabled is True


def test_unknown_graph_profile_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(rag_graph_profile="neo4j")


def test_removed_full_graph_profile_stops_startup_with_guidance() -> None:
    """削除した full(#621)は読み替えず、書き換え先を示して止める。"""
    with pytest.raises(ValidationError) as error:
        Settings(rag_graph_profile="full")
    assert "RAG_GRAPH_PROFILE=full は廃止しました" in str(error.value)
    assert "entities" in str(error.value)


def test_legacy_graph_enabled_flag_is_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """legacy の RAG_GRAPH_ENABLED は #621 で廃止した(読まず、profile を変えない)。"""
    monkeypatch.setenv("RAG_GRAPH_ENABLED", "true")
    settings = Settings()
    assert "rag_graph_enabled" not in Settings.model_fields
    assert settings.rag_graph_profile == "off"


def test_oracle_defaults_to_thin_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定は Thin mode で、Wallet は PLATFORM_ORACLE_WALLET_DIR（NL2SQL / terraform と同じ）。"""
    monkeypatch.delenv("PLATFORM_ORACLE_CLIENT_LIB_DIR", raising=False)
    monkeypatch.delenv("PLATFORM_ORACLE_WALLET_DIR", raising=False)
    settings = Settings(_env_file=None)
    assert settings.oracle_driver_mode == "thin"
    assert settings.resolved_oracle_wallet_dir == "/u01/aipoc/wallet"


def test_oracle_client_lib_dir_switches_to_thick_mode() -> None:
    """PLATFORM_ORACLE_CLIENT_LIB_DIR を指定したときだけ Thick mode と <lib_dir>/network/admin。"""
    settings = Settings(_env_file=None, oracle_client_lib_dir="/opt/oracle/instantclient_23_26")
    assert settings.oracle_driver_mode == "thick"
    assert settings.resolved_oracle_wallet_dir == "/opt/oracle/instantclient_23_26/network/admin"


def test_embedding_dimension_is_fixed_to_oracle_vector_width() -> None:
    """embedding 次元は Oracle VECTOR(1536, FLOAT32) と一致させる。"""
    assert Settings().oci_genai_embedding_dim == 1536

    with pytest.raises(ValidationError):
        Settings(oci_genai_embedding_dim=1024)


def test_model_settings_file_defaults_to_relative_env_sibling_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PLATFORM_MODEL_SETTINGS_FILE は .env と同じ階層の相対 JSON を既定にする。"""
    monkeypatch.delenv("PLATFORM_MODEL_SETTINGS_FILE", raising=False)

    settings = Settings(model_settings_file="")

    assert settings.model_settings_file == DEFAULT_MODEL_SETTINGS_FILE


def test_relative_model_settings_file_resolves_from_platform_env_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """相対 PLATFORM_MODEL_SETTINGS_FILE は共通 .env と同じ階層を基準にする（#211）。"""
    platform_dir = tmp_path / "platform"
    monkeypatch.setattr(config_module, "PLATFORM_ENV_FILE", platform_dir / ".env")

    assert (
        resolve_model_settings_file("model-settings.json")
        == (platform_dir / "model-settings.json").resolve()
    )


def test_settings_read_platform_and_rag_env_names_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """共通の設定は PLATFORM_*、RAG 固有の設定は RAG_* で読み、旧名は読まない（#211）。"""
    monkeypatch.setenv("PLATFORM_ORACLE_DSN", "platform-dsn")
    monkeypatch.setenv("PLATFORM_AUTH_MODE", "authenticated")
    monkeypatch.setenv("RAG_HUGGINGFACE_ENDPOINT", "hf-mirror.com")
    monkeypatch.setenv("RAG_OCI_DOCUMENT_UNDERSTANDING_LANGUAGE", "en")
    monkeypatch.setenv("HF_TOKEN", "legacy-token")

    settings = Settings(_env_file=None)

    assert settings.oracle_dsn == "platform-dsn"
    assert settings.auth_mode == "authenticated"
    assert settings.huggingface_endpoint == "https://hf-mirror.com"
    assert settings.oci_document_understanding_language == "en"
    assert settings.huggingface_token == ""


def test_settings_do_not_read_legacy_env_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新名がなければ、旧名（属性名と同じ名前）の環境変数と .env の行は反映しない（#211）。"""
    # 新名の環境変数（CI の実 Oracle の step は PLATFORM_ORACLE_* を渡す。#1972）を外して確かめる。
    monkeypatch.delenv("PLATFORM_ORACLE_DSN", raising=False)
    monkeypatch.delenv("PLATFORM_ORACLE_USER", raising=False)
    monkeypatch.delenv("RAG_LOG_LEVEL", raising=False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.setenv("ORACLE_DSN", "legacy-dsn")
    monkeypatch.setenv("OCI_DOCUMENT_UNDERSTANDING_NAMESPACE", "legacy-namespace")
    backend_env = tmp_path / "backend.env"
    backend_env.write_text("LOG_LEVEL=WARN\nORACLE_USER=legacy-user\n", encoding="utf-8")

    settings = Settings(_env_file=backend_env)

    assert settings.oracle_dsn == ""
    assert settings.oci_document_understanding_namespace == ""
    assert settings.log_level == "INFO"
    assert settings.oracle_user == ""
    # 属性名での生成（テストや設定画面の候補作成）は引き続き使える。
    assert Settings(_env_file=None, oracle_dsn="attr-dsn").oracle_dsn == "attr-dsn"


def test_settings_read_platform_env_then_backend_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共通 .env と backend/.env の両方を読む（環境変数が最優先）。"""
    platform_env = tmp_path / "platform.env"
    platform_env.write_text("PLATFORM_ORACLE_DSN=platform_high\n", encoding="utf-8")
    backend_env = tmp_path / "backend.env"
    backend_env.write_text("RAG_CHUNK_SIZE=900\n", encoding="utf-8")
    monkeypatch.delenv("PLATFORM_ORACLE_DSN", raising=False)
    monkeypatch.delenv("RAG_CHUNK_SIZE", raising=False)

    settings = Settings(_env_file=(platform_env, backend_env))

    assert settings.oracle_dsn == "platform_high"
    assert settings.rag_chunk_size == 900


def test_backend_env_example_lists_only_rag_settings() -> None:
    """backend/.env.example は RAG 固有の設定（RAG_*）だけを持つ（#211）。"""
    names = set(settings_env_names(Settings).values())
    example = config_module.BACKEND_ROOT / ".env.example"
    keys = [
        line.split("=", 1)[0]
        for line in example.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    ]

    assert keys
    assert [key for key in keys if not key.startswith("RAG_")] == []
    assert [key for key in keys if key not in names] == []


def test_auth_settings_use_platform_and_rag_env_names() -> None:
    """共通認証: 構成管理者・認証ポリシー・Cookie 名は PLATFORM_*（#214）。

    Cookie は 3 製品で共有する（#1565）。
    """
    names = settings_env_names(Settings)
    assert names["app_admin_login_user_id"] == "PLATFORM_ADMIN_LOGIN_USER_ID"
    assert names["app_admin_login_user_password"] == "PLATFORM_ADMIN_LOGIN_USER_PASSWORD"
    assert names["app_auth_cookie_secure"] == "PLATFORM_AUTH_COOKIE_SECURE"
    assert names["app_auth_idle_timeout_minutes"] == "PLATFORM_AUTH_IDLE_TIMEOUT_MINUTES"
    assert names["app_auth_session_cookie_name"] == "PLATFORM_AUTH_SESSION_COOKIE_NAME"
    assert names["app_auth_csrf_cookie_name"] == "PLATFORM_AUTH_CSRF_COOKIE_NAME"
    assert names["auth_mode"] == "PLATFORM_AUTH_MODE"
    # 単一アカウント方式の設定は廃止した。
    for removed in (
        "auth_username",
        "auth_password",
        "auth_session_secret",
        "auth_session_timeout_seconds",
        "auth_cookie_name",
        "auth_cookie_secure",
    ):
        assert removed not in names
    settings = Settings(_env_file=None)
    assert settings.service_name == "production-ready-rag"
    assert settings.app_auth_session_cookie_name == "pr_session"
    assert settings.app_auth_csrf_cookie_name == "pr_csrf"
    assert settings.local_debug_enabled is (settings.auth_mode == "local")
    production = Settings(_env_file=None, auth_mode="authenticated")
    assert production.app_auth_enabled is True
    assert production.local_debug_enabled is False


def test_enterprise_ai_max_retries_defaults_to_three_and_is_bounded() -> None:
    """Enterprise AI の最大リトライ回数は既定 3、0-5 に制限する。"""
    assert Settings().oci_enterprise_ai_max_retries == 3
    assert Settings(oci_enterprise_ai_max_retries=0).oci_enterprise_ai_max_retries == 0
    assert Settings(oci_enterprise_ai_max_retries=5).oci_enterprise_ai_max_retries == 5

    with pytest.raises(ValidationError):
        Settings(oci_enterprise_ai_max_retries=-1)
    with pytest.raises(ValidationError):
        Settings(oci_enterprise_ai_max_retries=6)


def test_http_service_retry_defaults_to_five_attempts_and_is_bounded() -> None:
    """HTTP マイクロサービス共通 retry は既定 5 試行、1-10 に制限する。"""
    settings = Settings()

    assert settings.rag_http_service_retry_attempts == 5
    assert Settings(rag_http_service_retry_attempts=1).rag_http_service_retry_attempts == 1
    assert Settings(rag_http_service_retry_attempts=10).rag_http_service_retry_attempts == 10

    with pytest.raises(ValidationError):
        Settings(rag_http_service_retry_attempts=0)
    with pytest.raises(ValidationError):
        Settings(rag_http_service_retry_attempts=11)


def test_external_gpu_parser_connection_defaults() -> None:
    """外部 GPU parser は未接続を既定とし、旧 service URL を読まない。"""
    settings = Settings()

    assert settings.rag_parser_mineru_api_host == ""
    assert settings.rag_parser_dots_ocr_api_host == ""
    assert settings.rag_parser_mineru_tier == "basic"
    assert settings.rag_parser_mineru_job_timeout_seconds == 1800.0
    assert settings.rag_parser_dots_ocr_model == "rednote-hilab/dots.mocr"

    assert Settings(rag_parser_mineru_tier="advanced").rag_parser_mineru_tier == "advanced"
    # 3.x の解析の方式（pipeline 等）は 4.0 の tier ではない（#1329）。
    with pytest.raises(ValidationError):
        Settings(rag_parser_mineru_tier="pipeline")


def test_legacy_gpu_service_url_is_not_migrated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAG_PARSER_MINERU_SERVICE_URL", "http://legacy-mineru:8000/parse")

    settings = Settings(_env_file=None)

    assert settings.rag_parser_mineru_api_host == ""


def test_enterprise_ai_timeout_defaults_to_pdf_friendly_value() -> None:
    """PDF/VLM 取込は 60 秒を超えることがあるため既定 timeout を長めにする。"""
    assert Settings().oci_enterprise_ai_timeout_seconds == 600.0

    with pytest.raises(ValidationError):
        Settings(oci_enterprise_ai_timeout_seconds=0)


def test_enterprise_ai_output_token_limits_follow_each_model() -> None:
    """出力の上限は登録モデルごとの値(#1719)。共通の LLM / VLM の出力の上限は持たない。"""
    assert not [name for name in Settings.model_fields if name.endswith("_max_output_tokens")]
    settings = Settings(
        oci_enterprise_ai_models=[
            EnterpriseAiConfiguredModel(model_id="llm-a", max_output_tokens=16000),
            EnterpriseAiConfiguredModel(model_id="llm-small", max_input_tokens=8192),
        ]
    )
    assert enterprise_ai_output_token_budget(settings, "llm-a") == 16000
    # 入力の上限の小さいモデルは入力の上限まで。登録の無いモデルは既定の 32768。
    assert enterprise_ai_output_token_budget(settings, "llm-small") == 8192
    assert enterprise_ai_output_token_budget(settings, "unknown") == 32768


def test_pdf_segmentation_defaults_are_bounded() -> None:
    """大 PDF は VLM 前に小さな page segment へ分割する。"""
    settings = Settings()

    assert settings.rag_pdf_segmentation_enabled is True
    assert settings.rag_pdf_max_pages_per_segment == 10
    assert settings.rag_pdf_max_segments == 300

    with pytest.raises(ValidationError):
        Settings(rag_pdf_max_pages_per_segment=0)
    with pytest.raises(ValidationError):
        Settings(rag_pdf_max_segments=0)


def test_ingestion_queue_defaults_keep_api_process_non_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ローカル既定でも HTTP リクエスト内で取込を実行しない。"""
    for key in (
        "RAG_INGESTION_QUEUE_DEDICATED_WORKER_ENABLED",
        "RAG_INGESTION_QUEUE_INPROCESS_WORKER_ENABLED",
        "RAG_INGESTION_QUEUE_PROCESS_ISOLATION_ENABLED",
        "RAG_INGESTION_QUEUE_STALE_RUNNING_SECONDS",
        "RAG_INGESTION_JOB_SUBPROCESS_TIMEOUT_SECONDS",
        "RAG_INGESTION_JOB_STALL_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None)

    assert settings.ingestion_queue_dedicated_worker_enabled is True
    assert settings.ingestion_queue_inprocess_worker_enabled is True
    assert settings.ingestion_queue_process_isolation_enabled is True
    assert settings.ingestion_queue_stale_running_seconds == 300.0
    # 全体の上限は 1 文書 1,000 図を見込んだ 24 時間、ふだんは停滞の 2 時間で止める(#2003)。
    assert settings.ingestion_job_subprocess_timeout_seconds == 86400.0
    assert settings.ingestion_job_stall_timeout_seconds == 7200.0


def test_ingestion_job_stall_timeout_exceeds_longest_silent_call() -> None:
    """停滞の時間は、進捗を出さない 1 回の呼び出しの最長より長い(既定でも。#2003)。"""
    settings = Settings(_env_file=None)
    longest = max(settings.ingestion_silent_call_timeouts().values())

    assert settings.ingestion_job_stall_timeout_seconds > longest
    # Vision の LLM は 1 回 600 秒を再試行込みで 4 回まで呼ぶ。
    assert settings.ingestion_silent_call_timeouts()[
        "PLATFORM_OCI_ENTERPRISE_AI_TIMEOUT_SECONDS"
    ] == pytest.approx(2400.0)
    # 共通の設定(3 製品で共有)で選べる最長(600 秒 × 6 回)でも、既定の停滞の時間で起動できる。
    widest = Settings(
        _env_file=None, oci_enterprise_ai_timeout_seconds=600.0, oci_enterprise_ai_max_retries=5
    )
    assert widest.ingestion_job_stall_timeout_seconds > max(
        widest.ingestion_silent_call_timeouts().values()
    )


def test_ingestion_job_stall_timeout_default_starts_with_deployed_parser_timeout() -> None:
    """配備の unit の解析サービスの timeout(init_script.sh)でも既定で起動できる(#2003)。"""
    init_script = (Path(__file__).resolve().parents[2] / "init_script.sh").read_text(
        encoding="utf-8"
    )
    match = re.search(r"Environment=RAG_PARSER_SERVICE_TIMEOUT_SECONDS=(\d+)", init_script)
    assert match is not None
    deployed = Settings(_env_file=None, rag_parser_service_timeout_seconds=float(match.group(1)))

    assert deployed.ingestion_job_stall_timeout_seconds > max(
        deployed.ingestion_silent_call_timeouts().values()
    )


@pytest.mark.parametrize(
    ("overrides", "setting_name"),
    [
        ({"ingestion_job_stall_timeout_seconds": 1800.0}, "PLATFORM_OCI_ENTERPRISE_AI"),
        (
            {"rag_parser_mineru_job_timeout_seconds": 7200.0},
            "RAG_PARSER_MINERU_JOB_TIMEOUT_SECONDS",
        ),
        (
            {"rag_parser_service_timeout_seconds": 3600.0},
            "RAG_PARSER_SERVICE_TIMEOUT_SECONDS",
        ),
    ],
    ids=["stall-too-short", "mineru-longer", "parser-service-longer"],
)
def test_ingestion_job_stall_timeout_rejects_shorter_than_silent_call(
    overrides: dict[str, float], setting_name: str
) -> None:
    """停滞の時間が呼び出しの最長以下なら、直す設定の名前を出して起動を止める(#2003)。"""
    with pytest.raises(ValidationError, match="RAG_INGESTION_JOB_STALL_TIMEOUT_SECONDS") as raised:
        Settings.model_validate(overrides)

    assert setting_name in str(raised.value)


def test_ingestion_job_overall_timeout_allows_up_to_seven_days() -> None:
    """全体の上限は 7 日まで設定できる(#2003)。"""
    settings = Settings(
        _env_file=None,
        ingestion_job_subprocess_timeout_seconds=604800.0,
        ingestion_job_stall_timeout_seconds=86400.0,
    )
    assert settings.ingestion_job_subprocess_timeout_seconds == 604800.0
    with pytest.raises(ValidationError):
        Settings(_env_file=None, ingestion_job_subprocess_timeout_seconds=604801.0)


def test_parser_adapter_default_is_docling(monkeypatch: pytest.MonkeyPatch) -> None:
    """既定 backend は Docling(#286)。既定エンジンの flag だけ有効で、他の adapter は無効。"""
    for key in (
        "RAG_PARSER_ADAPTER_BACKEND",
        "RAG_PARSER_DOCLING_ENABLED",
        "RAG_PARSER_UNSTRUCTURED_ENABLED",
        "RAG_PARSER_MINERU_ENABLED",
        "RAG_PARSER_DOTS_OCR_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None)

    assert DEFAULT_PARSER_ADAPTER_BACKEND == "docling"
    assert settings.rag_parser_adapter_backend == "docling"
    assert settings.rag_parser_docling_enabled is True
    assert settings.rag_parser_unstructured_enabled is False
    assert settings.rag_parser_mineru_enabled is False
    assert settings.rag_parser_dots_ocr_enabled is False
    # auto(旧既定)は既定 docling へ。local_partition は baseline 値 local へ。
    # local は正規化せず baseline 値として残す(runtime は ingestion で既定エンジンへマップ)。
    assert Settings(rag_parser_adapter_backend="auto").rag_parser_adapter_backend == "docling"
    assert Settings(rag_parser_adapter_backend="local").rag_parser_adapter_backend == "local"
    assert (
        Settings(rag_parser_adapter_backend="local_partition").rag_parser_adapter_backend == "local"
    )
    # Unstructured は引き続き明示選択できる。
    assert (
        Settings(rag_parser_adapter_backend="unstructured").rag_parser_adapter_backend
        == "unstructured"
    )
    assert Settings(rag_parser_docling_enabled=False).rag_parser_docling_enabled is False

    with pytest.raises(ValidationError):
        Settings(rag_parser_adapter_backend="llama_parse")


@pytest.mark.parametrize(
    "removed",
    ["marker", "unlimited_ocr", "glm_ocr", " Marker ", "oci_genai_vision", "enterprise_ai_vlm"],
)
def test_removed_parser_backends_fall_back_to_default(
    monkeypatch: pytest.MonkeyPatch, removed: str
) -> None:
    """削除したエンジン(#270)が env に残っていても既定の Docling(#286)として扱う。"""
    assert Settings(rag_parser_adapter_backend=removed).rag_parser_adapter_backend == "docling"
    monkeypatch.setenv("RAG_PARSER_ADAPTER_BACKEND", removed)
    assert Settings(_env_file=None).rag_parser_adapter_backend == "docling"


@pytest.mark.parametrize(
    "removed", ["marker", "unlimited_ocr", "glm_ocr", "oci_genai_vision", "enterprise_ai_vlm"]
)
def test_saved_parser_settings_with_removed_engine_load_as_default(removed: str) -> None:
    """parser-settings.json に削除したエンジン(#270)が残っていても読み込みで既定へ寄せる。"""
    # 既定(docling)以外から始め、読み込みで既定エンジンへ寄ったことを区別できるようにする。
    settings = Settings(_env_file=None, rag_parser_adapter_backend="unstructured")
    config_module._load_parser_adapters(
        settings,
        {
            "adapter_backend": removed,
            f"{removed}_enabled": True,
            f"{removed}_api_host": "https://removed.example.com",
            f"{removed}_model": "removed-model",
            "unstructured_enabled": True,
        },
    )

    assert settings.rag_parser_adapter_backend == "docling"
    dumped = config_module._dump_parser_adapters(settings)
    assert dumped["adapter_backend"] == "docling"
    assert not [key for key in dumped if removed in key]


def test_saved_parser_settings_ignore_stale_docling_vision_flag() -> None:
    """parser-settings.json に残った docling_vision_enabled(#497 で画面から削除)は読まない。

    Vision の全体の既定は backend/.env の RAG_VISION_ENABLED で決める（#528）。読み込みは失敗せず、
    次に保存すると消える。
    """
    settings = Settings(_env_file=None, rag_vision_enabled=False)
    config_module._load_parser_adapters(
        settings,
        {"adapter_backend": "docling", "docling_enabled": True, "docling_vision_enabled": True},
    )

    assert settings.rag_parser_adapter_backend == "docling"
    assert settings.rag_vision_enabled is False
    assert not hasattr(settings, "rag_parser_docling_vision_enabled")
    assert "docling_vision_enabled" not in config_module._dump_parser_adapters(settings)


def test_vision_setting_reads_only_the_new_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vision は RAG_VISION_ENABLED で切り替える(既定は有効。#537)。

    旧 env(#497 で削除)は読まない。
    """
    monkeypatch.setenv("RAG_PARSER_DOCLING_VISION_ENABLED", "false")
    monkeypatch.setenv("RAG_ASSET_SUMMARY_ENABLED", "false")
    monkeypatch.delenv("RAG_VISION_ENABLED", raising=False)
    assert Settings(_env_file=None).rag_vision_enabled is True

    monkeypatch.setenv("RAG_VISION_ENABLED", "false")
    settings = Settings(_env_file=None)
    assert settings.rag_vision_enabled is False
    assert "rag_asset_summary_enabled" not in Settings.model_fields
    assert "rag_asset_summary_max_assets" not in Settings.model_fields


def test_vision_is_enabled_and_field_extraction_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """env に設定がなければ、Vision は有効(#537)、項目抽出は無効(#2839)。"""
    monkeypatch.delenv("RAG_VISION_ENABLED", raising=False)
    monkeypatch.delenv("RAG_FIELD_EXTRACTION_ENABLED", raising=False)
    settings = Settings(_env_file=None)
    assert settings.rag_vision_enabled is True
    assert settings.rag_field_extraction_enabled is False


def test_saved_parser_settings_without_backend_load_as_docling() -> None:
    """保存済み parser 節に backend が無い / 旧 auto なら既定 Docling で読む(#286)。"""
    settings = Settings(
        _env_file=None,
        rag_parser_adapter_backend="unstructured",
        rag_parser_docling_enabled=False,
        rag_parser_unstructured_enabled=True,
    )
    config_module._load_parser_adapters(settings, {})

    assert settings.rag_parser_adapter_backend == "docling"
    assert settings.rag_parser_docling_enabled is True
    assert settings.rag_parser_unstructured_enabled is False

    settings = Settings(_env_file=None, rag_parser_adapter_backend="unstructured")
    config_module._load_parser_adapters(settings, {"adapter_backend": "auto"})
    assert settings.rag_parser_adapter_backend == "docling"


def test_missing_parser_settings_file_keeps_docling_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """parser-settings.json が無ければ Settings の既定(Docling)がそのまま残る(#1701)。"""
    monkeypatch.delenv("RAG_PARSER_ADAPTER_BACKEND", raising=False)
    monkeypatch.delenv("RAG_PARSER_DOCLING_ENABLED", raising=False)
    settings = Settings(
        _env_file=None, rag_parser_settings_file=str(tmp_path / "parser-settings.json")
    )
    config_module.load_persisted_parser_settings(settings)

    assert settings.rag_parser_adapter_backend == "docling"
    assert settings.rag_parser_docling_enabled is True


def test_relative_parser_settings_file_resolves_from_backend_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """RAG_PARSER_SETTINGS_FILE の相対 path は RAG の backend/ 基準(#1701)。"""
    monkeypatch.setattr(config_module, "BACKEND_ENV_FILE", tmp_path / "backend" / ".env")

    assert (
        config_module.resolve_parser_settings_file("")
        == (tmp_path / "backend" / "parser-settings.json").resolve()
    )
    assert config_module.resolve_parser_settings_file("/srv/parser.json") == Path(
        "/srv/parser.json"
    )


def test_saved_unstructured_selection_is_kept() -> None:
    """既定が Docling になっても、保存済みの Unstructured 選択はそのまま読み込む(#286)。"""
    settings = Settings(_env_file=None)
    config_module._load_parser_adapters(
        settings,
        {"adapter_backend": "unstructured", "unstructured_enabled": True},
    )

    assert settings.rag_parser_adapter_backend == "unstructured"
    assert settings.rag_parser_unstructured_enabled is True


def test_backend_env_example_selects_docling_parser() -> None:
    """backend/.env.example の既定の解析エンジンは Docling(#286)。"""
    example = config_module.BACKEND_ROOT / ".env.example"
    values = dict(
        line.split("=", 1)
        for line in example.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )

    assert values["RAG_PARSER_ADAPTER_BACKEND"] == "docling"
    assert values["RAG_PARSER_DOCLING_ENABLED"] == "true"
    assert values["RAG_PARSER_ADAPTER_BACKEND"] == DEFAULT_PARSER_ADAPTER_BACKEND


def test_removed_parser_engine_settings_are_gone() -> None:
    """削除したエンジン(#270)の Settings 項目・環境変数は持たない(旧名は読まない)。"""
    names = settings_env_names(Settings)
    for engine in ("marker", "unlimited_ocr", "glm_ocr", "genai_vision", "parser_asr_service"):
        assert not [name for name in names if engine in name], engine


def test_removed_audio_transcription_settings_are_gone() -> None:
    """音声・動画の取込(OCI AI Speech の文字起こし)の設定は持たない(#3369)。"""
    names = set(settings_env_names(Settings).values())
    assert not [name for name in names if "SPEECH" in name or "_ASR" in name]


def test_parser_adapter_per_adapter_extras_are_declared_and_conflict_free() -> None:
    """外部 parser はサービス化。backend は per-adapter extra のみ宣言する(combined extra なし)。"""
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    optional = pyproject["project"]["optional-dependencies"]

    # torch / torchvision は CPU 版の index から入れるため extra に直接書く(#1650)。
    cpu_torch = ["torch>=2.13,<3", "torchvision>=0.28,<1"]
    assert optional["docling"] == ["docling==2.132.0", *cpu_torch]
    # pi-heif・pandoc を入れる all-docs にしない(#3351)。
    assert optional["unstructured"] == [
        "unstructured[pdf,image,docx,pptx,xlsx,csv,tsv,md]==0.27.8",
        *cpu_torch,
    ]
    assert pyproject["tool"]["uv"]["sources"]["torch"] == {"index": "pytorch-cpu"}
    # Marker は削除した(#270)。combined extra も提供しない(サービス分離の理由)。
    assert set(optional) == {"docling", "unstructured"}
    # backend は共有 contract package を path 依存で取り込む。
    assert "rag-parser-core" in pyproject["project"]["dependencies"]


def test_tsv_upload_content_type_is_allowed_by_default() -> None:
    """TSV は table-preserving local parser と整合するため既定 upload whitelist に含める。"""
    assert "text/tab-separated-values" in Settings().allowed_upload_content_types


def test_json_lines_upload_content_types_are_allowed_by_default() -> None:
    """JSONL / NDJSON は local text parser と整合するため既定 upload whitelist に含める。"""
    allowed = set(Settings().allowed_upload_content_types)

    assert {
        "application/jsonl",
        "application/jsonlines",
        "application/ndjson",
        "application/x-ndjson",
    } <= allowed


def test_browser_previewable_image_content_types_are_allowed_by_default() -> None:
    """Enterprise AI image payload と整合する画像 MIME は既定 upload whitelist に含める。"""
    allowed = set(Settings().allowed_upload_content_types)

    assert {"image/gif", "image/jpeg", "image/jpg", "image/png", "image/webp"} <= allowed
    assert {"image/tif", "image/tiff"} <= allowed


def test_semantic_html_and_outlook_msg_content_types_are_allowed_by_default() -> None:
    """SourceProfile が判定できる XHTML / Outlook MSG MIME は upload whitelist に含める。"""
    allowed = set(Settings().allowed_upload_content_types)

    assert "application/xhtml+xml" in allowed
    assert {"application/vnd.ms-outlook", "application/x-msg"} <= allowed


def test_audio_and_video_content_types_are_not_allowed_by_default() -> None:
    """音声・動画は取り込まない形式で、upload の既定の許可に含めない(#3369)。"""
    allowed = Settings().allowed_upload_content_types

    assert not [item for item in allowed if item.startswith(("audio/", "video/"))]
    assert "application/ogg" not in allowed


def test_enterprise_ai_response_paths_default_to_known_envelope_detection() -> None:
    """Enterprise AI response path は既定で空にし、既知 envelope を順番に照合する。"""
    settings = Settings()

    assert settings.oci_enterprise_ai_llm_response_path == ""
    assert settings.oci_enterprise_ai_vlm_response_path == ""
    assert (
        Settings(
            oci_enterprise_ai_llm_response_path="/data/text"
        ).oci_enterprise_ai_llm_response_path
        == "/data/text"
    )


def test_chunk_overlap_must_be_smaller_than_chunk_size() -> None:
    """chunk overlap が chunk size 以上の誤設定は起動時に拒否する。"""
    with pytest.raises(ValidationError, match="RAG_CHUNK_OVERLAP"):
        Settings(rag_chunk_size=400, rag_chunk_overlap=400)


def test_chunking_accepts_product_maximums_and_rejects_larger_values() -> None:
    settings = Settings(
        rag_chunk_size=CHUNK_SIZE_MAX_CHARS,
        rag_chunk_overlap=CHUNK_OVERLAP_MAX_CHARS,
    )
    assert settings.rag_chunk_size == 32_000
    assert settings.rag_chunk_overlap == 8_000

    with pytest.raises(ValidationError):
        Settings(rag_chunk_size=CHUNK_SIZE_MAX_CHARS + 1)
    with pytest.raises(ValidationError):
        Settings(rag_chunk_overlap=CHUNK_OVERLAP_MAX_CHARS + 1)


def test_removed_oci_platform_settings_are_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """共通 .env に残った OCI 認証・Object Storage・ADB の値は読まない（#11）。"""
    platform_env = tmp_path / "platform.env"
    platform_env.write_text(
        "PLATFORM_UPLOAD_STORAGE_BACKEND=oci\n"
        "PLATFORM_OBJECT_STORAGE_NAMESPACE=legacy-namespace\n"
        "PLATFORM_OCI_REGION=legacy-region\n"
        "PLATFORM_ORACLE_ADB_OCID=ocid1.autonomousdatabase.oc1..legacy\n",
        encoding="utf-8",
    )
    for name in (
        "PLATFORM_UPLOAD_STORAGE_BACKEND",
        "PLATFORM_OBJECT_STORAGE_NAMESPACE",
        "PLATFORM_OCI_REGION",
        "PLATFORM_ORACLE_ADB_OCID",
        "RAG_OBJECT_STORAGE_NAMESPACE",
        "RAG_OCI_REGION",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=(platform_env,))

    assert not hasattr(settings, "upload_storage_backend")
    assert not hasattr(settings, "oracle_adb_ocid")
    # OCI Document Understanding が読む値は RAG の .env（RAG_*）に置く。
    assert settings.object_storage_namespace == ""
    assert settings.oci_region == ""


def test_local_storage_dir_defaults_to_u01_persistent_path() -> None:
    """local 保存の既定ディレクトリは /u01 配下の永続化想定パスにする。"""
    assert Settings().local_storage_dir == DEFAULT_LOCAL_STORAGE_DIR
    assert DEFAULT_LOCAL_STORAGE_DIR == "/u01/data/production-ready-rag"


def test_max_upload_bytes_defaults_to_200_mib_and_is_positive() -> None:
    """アップロードサイズ上限は既定 200 MiB、正の値に制限する。"""
    assert Settings().max_upload_bytes == 200 * 1024 * 1024

    with pytest.raises(ValidationError):
        Settings(max_upload_bytes=0)


def test_db_read_timeout_is_positive() -> None:
    """DB 読み取りの timeout は正の秒数に制限する。"""
    assert Settings().db_read_timeout_seconds == 8.0

    with pytest.raises(ValidationError):
        Settings(db_read_timeout_seconds=0)


def test_oracle_connection_timeouts_are_bounded() -> None:
    """Oracle 接続テストは UI を長時間待たせない短い timeout を持つ。"""
    settings = Settings()

    assert settings.oracle_tcp_connect_timeout_seconds == 10.0
    assert settings.oracle_db_test_timeout_seconds == 15.0
    assert (
        Settings(
            oracle_tcp_connect_timeout_seconds=5, oracle_db_test_timeout_seconds=20
        ).oracle_db_test_timeout_seconds
        == 20
    )

    with pytest.raises(ValidationError):
        Settings(oracle_tcp_connect_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(oracle_db_test_timeout_seconds=0)


def test_oracle_vector_target_accuracy_is_bounded() -> None:
    """Oracle 近似 vector search の target accuracy は 1-100 に制限する。"""
    assert Settings().oracle_vector_target_accuracy == 95
    assert Settings(oracle_vector_target_accuracy=90).oracle_vector_target_accuracy == 90

    with pytest.raises(ValidationError):
        Settings(oracle_vector_target_accuracy=0)
    with pytest.raises(ValidationError):
        Settings(oracle_vector_target_accuracy=101)


def test_rag_rrf_k_is_bounded() -> None:
    """Hybrid retrieval の RRF 定数は正の安全な範囲に制限する。"""
    assert Settings().rag_rrf_k == 60
    assert Settings(rag_rrf_k=10).rag_rrf_k == 10

    with pytest.raises(ValidationError):
        Settings(rag_rrf_k=0)
    with pytest.raises(ValidationError):
        Settings(rag_rrf_k=1001)


def test_genai_cache_defaults_and_bounds() -> None:
    """embedding / rerank cache は既定有効で、容量を安全範囲に制限する。"""
    settings = Settings()

    assert settings.rag_embedding_cache_enabled is True
    assert settings.rag_embedding_cache_max_entries == 4096
    assert settings.rag_embedding_batch_size == 96
    assert settings.rag_rerank_cache_enabled is True
    assert settings.rag_rerank_cache_max_entries == 1024
    assert Settings(rag_embedding_cache_max_entries=0).rag_embedding_cache_max_entries == 0
    assert Settings(rag_embedding_batch_size=1).rag_embedding_batch_size == 1
    assert Settings(rag_rerank_cache_max_entries=0).rag_rerank_cache_max_entries == 0

    with pytest.raises(ValidationError):
        Settings(rag_embedding_cache_max_entries=-1)
    with pytest.raises(ValidationError):
        Settings(rag_embedding_batch_size=0)
    with pytest.raises(ValidationError):
        Settings(rag_embedding_batch_size=97)
    with pytest.raises(ValidationError):
        Settings(rag_rerank_cache_max_entries=-1)


def test_context_group_max_chunks_is_bounded() -> None:
    """回答の根拠の group から足す sibling 数(親子階層の small-to-big)を制限する。"""
    settings = Settings()

    assert settings.rag_context_group_max_chunks == 4
    assert Settings(rag_context_group_max_chunks=2).rag_context_group_max_chunks == 2

    with pytest.raises(ValidationError):
        Settings(rag_context_group_max_chunks=0)
    with pytest.raises(ValidationError):
        Settings(rag_context_group_max_chunks=21)


def test_rate_limit_defaults_protect_expensive_endpoints() -> None:
    """高コスト API の limiter は既定で有効、正の上限を持つ。"""
    settings = Settings()

    assert settings.rate_limit_enabled is True
    assert settings.rate_limit_window_seconds == 60.0
    assert settings.rate_limit_search_requests > 0
    assert settings.rate_limit_evaluation_runs > 0
    assert settings.rate_limit_uploads > 0
    assert settings.rate_limit_ingest_requests > 0

    with pytest.raises(ValidationError):
        Settings(rate_limit_search_requests=0)


def test_sensitive_identifier_masking_is_enabled_by_default() -> None:
    """機微な識別子のマスクは既定で有効にする。"""
    assert "guardrail_mask_sensitive_identifiers" not in Settings.model_fields


def test_audit_context_hash_salt_is_optional() -> None:
    """監査 context hash salt は任意で、既定では空にする。"""
    assert Settings().audit_context_hash_salt == ""
    assert Settings(audit_context_hash_salt="env-provided-salt").audit_context_hash_salt == (
        "env-provided-salt"
    )


def test_trace_exporter_is_disabled_by_default_and_bounded() -> None:
    """trace exporter は既定では無効で、timeout/queue は過大設定を拒否する。"""
    settings = Settings()

    assert settings.trace_export_http_endpoint == ""
    assert settings.trace_export_http_timeout_seconds == 2.0
    assert settings.trace_export_queue_size == 1024

    with pytest.raises(ValidationError):
        Settings(trace_export_http_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(trace_export_queue_size=0)


def test_get_settings_picks_up_env_changes_saved_by_another_process() -> None:
    """別プロセスが画面から .env に保存した値を、変わった項目だけ取り込む（#465）。"""
    import os

    from app import config as app_config

    env_file = app_config.BACKEND_ENV_FILE
    settings = app_config.get_settings()
    # このプロセスの中で変えた値（.env にない値）は、再読込で既定値へ戻さない。
    settings.rag_rerank_enabled = False

    env_file.write_text("RAG_ANSWER_RECORD_RETENTION_DAYS=30\n", encoding="utf-8")
    assert app_config.get_settings().rag_answer_record_retention_days == 30

    env_file.write_text("RAG_ANSWER_RECORD_RETENTION_DAYS=45\n", encoding="utf-8")
    stat = env_file.stat()
    # 同じ更新時刻に丸められても変更として扱われるよう、時刻を進める。
    os.utime(env_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    reloaded = app_config.get_settings()

    assert reloaded is settings
    assert reloaded.rag_answer_record_retention_days == 45
    assert reloaded.rag_rerank_enabled is False


def test_get_settings_picks_up_shared_settings_saved_by_another_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NL2SQL・Agent の画面で保存した共通の設定を、再起動なしで使う（#2102）。

    共有 pool は接続の設定の指紋で作り直す（test_oracle_adapter.py）。
    """
    import os

    from pr_system_settings.env_file import write_env_values

    from app import config as app_config

    for name in (
        "PLATFORM_LOCAL_STORAGE_DIR",
        "PLATFORM_ORACLE_DSN",
        "PLATFORM_ORACLE_USER",
        "PLATFORM_AUTH_IDLE_TIMEOUT_MINUTES",
    ):
        monkeypatch.delenv(name, raising=False)
    platform_env = app_config.PLATFORM_ENV_FILE
    write_env_values(
        platform_env,
        {"PLATFORM_LOCAL_STORAGE_DIR": "/data/a", "PLATFORM_ORACLE_DSN": "db-a_high"},
        section_comment="# test",
    )
    app_config.reset_settings_cache()
    settings = app_config.get_settings()
    assert (settings.local_storage_dir, settings.oracle_dsn) == ("/data/a", "db-a_high")

    # 別の製品の保存と同じ書き方（`write_env_values`）で共通 `.env` を書き換える。
    write_env_values(
        platform_env,
        {
            "PLATFORM_LOCAL_STORAGE_DIR": "/data/b",
            "PLATFORM_ORACLE_DSN": "db-b_high",
            "PLATFORM_ORACLE_USER": "APP_B",
            "PLATFORM_AUTH_IDLE_TIMEOUT_MINUTES": "17",
        },
        section_comment="# test",
    )
    stat = platform_env.stat()
    # 同じ更新時刻に丸められても変更として扱われるよう、時刻を進める。
    os.utime(platform_env, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    reloaded = app_config.get_settings()

    assert reloaded is settings
    assert reloaded.local_storage_dir == "/data/b"
    assert (reloaded.oracle_dsn, reloaded.oracle_user) == ("db-b_high", "APP_B")
    assert reloaded.app_auth_idle_timeout_minutes == 17
    app_config.reset_settings_cache()


def test_get_settings_keeps_running_when_the_reloaded_env_is_invalid() -> None:
    """読み直した .env が不正でも例外にせず、今の値のまま動く（#465）。"""
    import os

    from app import config as app_config

    env_file = app_config.BACKEND_ENV_FILE
    env_file.write_text("RAG_ANSWER_RECORD_RETENTION_DAYS=30\n", encoding="utf-8")
    assert app_config.get_settings().rag_answer_record_retention_days == 30

    env_file.write_text("RAG_ANSWER_RECORD_RETENTION_DAYS=not-a-number\n", encoding="utf-8")
    stat = env_file.stat()
    os.utime(env_file, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    assert app_config.get_settings().rag_answer_record_retention_days == 30


def test_platform_and_product_env_examples_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """実際の雛形を読み込み、初回起動時の設定解析エラーを検出する。"""
    import os

    from app.config import Settings

    for key in tuple(os.environ):
        if key.startswith(("PLATFORM_", "RAG_")):
            monkeypatch.delenv(key)
    backend = Path(__file__).resolve().parents[1]
    settings = Settings(
        _env_file=(backend.parents[1] / "platform" / ".env.example", backend / ".env.example")
    )

    assert settings.cors_origins
    assert all(origin.startswith("http://") for origin in settings.cors_origins)
    assert settings.oci_enterprise_ai_models == []
    assert settings.oracle_password in ("", None)


def test_history_rewrite_env_example_matches_default_and_precedes_query_strategy() -> None:
    """履歴の書き換えを雛形でも有効にし、独立した質問拡張より前に案内する。"""
    from dotenv import dotenv_values

    example = config_module.BACKEND_ROOT / ".env.example"
    values = dotenv_values(example, interpolate=False)
    assert values["RAG_HISTORY_REWRITE_ENABLED"] == "true"
    assert Settings.model_fields["rag_history_rewrite_enabled"].default is True
    lines = example.read_text(encoding="utf-8").splitlines()
    assert lines.index("RAG_HISTORY_REWRITE_ENABLED=true") < lines.index(
        "RAG_QUERY_STRATEGY=simple_retrieval"
    )


def test_settings_today_uses_common_timezone_not_os_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """基準日の既定は共通の設定の時間帯（PLATFORM_TIMEZONE。既定 UTC）の今日（#3255）。"""
    from datetime import UTC, date, datetime

    import pr_backend_core.timezones as timezones_module

    class _FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            # 2026-10-25 01:30 JST（UTC では前の日）。
            return datetime(2026, 10, 24, 16, 30, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(timezones_module, "datetime", _FixedDatetime)
    settings = config_module.get_settings()
    assert settings.timezone == "UTC"  # 既定（#3274）。
    assert config_module.settings_today() == date(2026, 10, 24)
    monkeypatch.setattr(settings, "timezone", "Asia/Tokyo")
    assert config_module.settings_today() == date(2026, 10, 25)


def test_platform_timezone_rejects_fixed_offsets() -> None:
    with pytest.raises(ValidationError):
        config_module.Settings(timezone="+09:00")
