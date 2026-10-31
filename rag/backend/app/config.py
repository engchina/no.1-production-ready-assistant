"""アプリケーション設定。

環境変数 → 3製品共通の `platform/.env`（`PLATFORM_*`）→ RAG の `backend/.env`（`RAG_*`）から
読み込む（#211）。シークレットはコードにハードコードしない。
"""

import contextlib
import logging
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Self

from pr_backend_core.chat_history import (
    CHAT_HISTORY_DEFAULT_MAX_CHARS,
    CHAT_HISTORY_DEFAULT_MAX_CHARS_PER_TURN,
    CHAT_HISTORY_DEFAULT_MAX_TURNS,
    CHAT_HISTORY_MAX_CHARS_LIMIT,
    CHAT_HISTORY_MAX_CHARS_PER_TURN_LIMIT,
    CHAT_HISTORY_MAX_TURNS_LIMIT,
    CHAT_HISTORY_MIN_CHARS,
    CHAT_HISTORY_MIN_CHARS_PER_TURN,
)
from pr_backend_core.chat_history_window import (
    CHAT_HISTORY_DAYS_LIMIT,
    CHAT_HISTORY_DEFAULT_DAYS,
)
from pr_backend_core.config import (
    PlatformEnvSourcesMixin,
    RuntimeSettings,
    TimezoneSettings,
    TracingSettings,
    platform_env_file,
    product_env_file,
    product_settings_config,
    settings_env_name,
)
from pr_backend_core.timezones import local_today
from pr_system_settings.auth.domain import DEFAULT_CSRF_COOKIE_NAME, DEFAULT_SESSION_COOKIE_NAME
from pr_system_settings.chat_attachments.limits import (
    CHAT_ATTACHMENT_DEFAULT_MAX_BYTES,
    CHAT_ATTACHMENT_DEFAULT_MAX_FILES,
    CHAT_ATTACHMENT_DEFAULT_TTL_HOURS,
    CHAT_ATTACHMENT_MAX_BYTES_MAX,
    CHAT_ATTACHMENT_MAX_BYTES_MIN,
    CHAT_ATTACHMENT_MAX_FILES_MAX,
    CHAT_ATTACHMENT_MAX_FILES_MIN,
    CHAT_ATTACHMENT_TTL_HOURS_MAX,
    CHAT_ATTACHMENT_TTL_HOURS_MIN,
)
from pr_system_settings.model import EnterpriseAiConfiguredModel as EnterpriseAiConfiguredModel
from pr_system_settings.model import EnterpriseAiConnection as EnterpriseAiConnection
from pr_system_settings.model import ModelSecretStateMixin, ModelSettingsStore
from pr_system_settings.model import (
    enterprise_ai_connection_for_model as enterprise_ai_connection_for_model,
)
from pr_system_settings.model import (
    enterprise_ai_default_model_id as enterprise_ai_default_model_id,
)
from pr_system_settings.model import (
    enterprise_ai_judge_model_id as enterprise_ai_judge_model_id,
)
from pr_system_settings.model import (
    enterprise_ai_max_input_tokens as enterprise_ai_max_input_tokens,
)
from pr_system_settings.model import enterprise_ai_model_catalog as enterprise_ai_model_catalog
from pr_system_settings.model import (
    enterprise_ai_output_token_budget as enterprise_ai_output_token_budget,
)
from pr_system_settings.model import (
    enterprise_ai_reasoning_effort_to_send as enterprise_ai_reasoning_effort_to_send,
)
from pr_system_settings.model import enterprise_ai_vision_model_id as enterprise_ai_vision_model_id
from pydantic import (
    BaseModel,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
from rag_parser_core.sheet_records import ExcelOptions
from rag_parser_core.structured_records import JSONL_RECORD_BOUNDARY
from rag_pipeline_core.chunking import (
    CHUNK_OVERLAP_MAX_CHARS as CHUNK_OVERLAP_MAX_CHARS,
)
from rag_pipeline_core.chunking import (
    CHUNK_SIZE_MAX_CHARS as CHUNK_SIZE_MAX_CHARS,
)
from rag_pipeline_core.chunking import (
    CHUNK_SIZE_MIN_CHARS as CHUNK_SIZE_MIN_CHARS,
)
from rag_pipeline_core.evaluation import LEGACY_EVALUATION_SUITES

from app.parser_settings_store import ParserSettingsStore
from app.rag.language_pack_store import LANGUAGE_PACK_STORE
from app.search_defaults import SEARCH_TOP_K_DEFAULT, SEARCH_TOP_K_MAX

UploadStorageBackend = Literal["local", "oci"]
AuditPersistence = Literal["log", "oracle", "both"]
ParserAdapterBackend = Literal[
    # 廃止済みの in-process baseline。validator は local を正規化せず保持する(advanced
    # diagnostics の scorecard/staging golden gate が「常時利用可能な baseline」概念として
    # 参照するため)。runtime では ingestion._partition_source が local を既定エンジン
    # (DEFAULT_PARSER_ADAPTER_BACKEND = docling)のサービスへマップし、in-process 解析は実行しない。
    "local",
    "docling",
    "unstructured",
]
# 削除した文書解析エンジン(#270: Marker / Unlimited-OCR / GLM-OCR、#2042: OCI Generative AI
# (Vision)、#22: MinerU / Dots.OCR / OCI Document Understanding、#21: 構造化レコード JSONL /
# XML ツリー JSON)。保存済みの設定
# (parser-settings.json・文書レシピ・KB 構築設定・取込ジョブ)に残っていても取込・画面を
# 壊さないよう、読み込み時に既定エンジンへ寄せる(旧 ``auto`` と同じ扱い)。
# 旧値は再保存時に消えるため、既存データを書き換える migration は持たない。
REMOVED_PARSER_ADAPTER_BACKENDS = frozenset(
    {
        "marker",
        "unlimited_ocr",
        "glm_ocr",
        "oci_genai_vision",
        "enterprise_ai_vlm",
        "mineru",
        "dots_ocr",
        "oci_document_understanding",
        # 前処理の構造化レコードの JSONL は Docling・Unstructured のサービスが読む(#21)。
        "structured_records_jsonl",
        "xml_tree_json",
    }
)
# 既定の文書解析エンジン(#286: Unstructured から Docling へ変更)。Unstructured は明示選択で使える。
DEFAULT_PARSER_ADAPTER_BACKEND: Literal["docling"] = "docling"


def normalize_parser_adapter_backend_value(value: object) -> object:
    """旧値・削除済みエンジンを既定エンジンへ正規化する(未知値はそのまま検証へ回す)。

    - ``auto``(旧既定)と削除済みエンジン → 既定 ``docling``(DEFAULT_PARSER_ADAPTER_BACKEND)。
    - ``local_partition``(結果タグ別名)→ baseline 値 ``local``。
    - ``local`` は正規化しない(advanced diagnostics が常時利用可能な baseline として扱う)。
    """
    normalized = str(value).strip().casefold()
    if normalized == "auto" or normalized in REMOVED_PARSER_ADAPTER_BACKENDS:
        return DEFAULT_PARSER_ADAPTER_BACKEND
    if normalized == "local_partition":
        return "local"
    return value


# 固定分割の方式と、#2457 の前の固定分割符の既定(escape 表現)。既定は記録の境界のマーカーに変えた。
# 既存の chunk_set_id と分割の結果を保つ比較・移行だけに使う。
FIXED_DELIMITER_STRATEGY: Literal["fixed_delimiter"] = "fixed_delimiter"
PREVIOUS_DEFAULT_CHUNK_DELIMITER = "\\n\\n"


PreprocessProfile = Literal[
    "passthrough",
    "office_to_pdf",
    "csv_to_jsonl",
    "excel_to_jsonl",
    "word_to_jsonl",
]
# 削除したファイル準備の変換方式(text_normalize、#2042: スキャン画像の補正・URL→Markdown)。
# 保存済みの設定(backend/.env・文書レシピ・KB 構築設定・取込ジョブ)に残っていても起動・取込を
# 壊さないよう、読み込み時に「変換なし」(passthrough)へ寄せる。旧値は再保存時に消える。
REMOVED_PREPROCESS_PROFILES = frozenset({"text_normalize", "url_to_markdown", "image_enhance"})


def normalize_removed_preprocess_profile(value: object) -> object:
    """削除した変換方式を「変換なし」へ寄せる(未知値はそのまま検証へ回す)。"""
    if isinstance(value, str) and value.strip().casefold() in REMOVED_PREPROCESS_PROFILES:
        logging.getLogger(__name__).warning(
            "removed_preprocess_profile_normalized",
            extra={"preprocess_profile": value.strip().casefold()},
        )
        return "passthrough"
    return value


ChunkingStrategy = Literal[
    "structure_aware",
    "recursive_character",
    "small_to_big",
    "markdown_heading",
    "page_level",
    "fixed_size",
    "fixed_delimiter",
]
CHUNKING_STRATEGIES_WITH_MIN_CHARS: set[ChunkingStrategy] = {
    "structure_aware",
    "recursive_character",
    "markdown_heading",
    "page_level",
}
# 削除した分割方式の保存値を後継の方式へ読み替える(.env / 文書レシピ / KB の保存値)。
# 親子階層(hierarchical_parent_child)は親子階層（small-to-big）へ置き換えた(#271)。
LEGACY_CHUNKING_STRATEGY_ALIASES: dict[str, ChunkingStrategy] = {
    "sentence_window": "recursive_character",
    "hierarchical_parent_child": "small_to_big",
}


def normalize_legacy_chunking_strategy_value(value: object) -> object:
    """削除した分割方式の保存値を後継へ読み替える(pydantic の before validator 用)。"""
    if isinstance(value, str):
        return LEGACY_CHUNKING_STRATEGY_ALIASES.get(value.strip().casefold(), value)
    return value


# 親子階層（small-to-big。`small_to_big`）の分割パラメータ。既定値と範囲は rag_poc の
# rag_engine.chunking.constants(DEFAULT_* / *_RANGE)と同じ(テストで一致を確認する)。
# rag_engine.chunking は import すると分割実装一式を読み込むため、ここでは値を複製して持つ。
# OCI Enterprise AI の LLM 1 回の timeout（`oci_enterprise_ai_timeout_seconds`）の上限（秒）。
# 保存済みの回答の評価の時間の上限もこの値から決める（#304）。
OCI_ENTERPRISE_AI_TIMEOUT_MAX_SECONDS = 600.0
# 品質評価の job の全体の時間の上限の最大（秒。1 日）。job の上限は、回答の回数 × 回答の上限を
# この値で抑える（#1430。`app.rag.evaluation_jobs.evaluation_job_time_limit_seconds`）。
RAG_EVALUATION_JOB_TIMEOUT_MAX_SECONDS = 86400
# 取込 job の全体の上限（秒。#2003）。既定は 1 文書 1,000 図を 1 件 60 秒で順に読んでも足りる
# 24 時間、設定できる上限は 7 日。ふだんは停滞の時間（進捗が変わらない秒数）で止める。
INGESTION_JOB_SUBPROCESS_TIMEOUT_DEFAULT_SECONDS = 86400.0
INGESTION_JOB_SUBPROCESS_TIMEOUT_MAX_SECONDS = 604800.0
# 取込 job の停滞の時間の既定（秒。#2003）。進捗を出さない 1 回の呼び出し（再試行を含む）の最長
# より長くする: 解析サービスは配備の unit の 1230 秒 × 5 回 + 待ち = 約 6170 秒（timeout も
# 再試行する）、Vision の LLM は共通の設定（PLATFORM_OCI_ENTERPRISE_AI_*）で選べる最長が
# 600 秒 × 6 回 = 3600 秒。これらより長い 2 時間にする。
INGESTION_JOB_STALL_TIMEOUT_DEFAULT_SECONDS = 7200.0
CHUNK_CHILD_TARGET_CHARS_DEFAULT = 1000
CHUNK_CHILD_TARGET_CHARS_MIN = 300
CHUNK_CHILD_TARGET_CHARS_MAX = 1600
CHUNK_TABLE_CHILD_TARGET_CHARS_DEFAULT = 3000
CHUNK_TABLE_CHILD_TARGET_CHARS_MIN = 300
CHUNK_TABLE_CHILD_TARGET_CHARS_MAX = 8000
CHUNK_PARENT_TARGET_CHARS_DEFAULT = 6000
CHUNK_PARENT_TARGET_CHARS_MIN = 1200
CHUNK_PARENT_TARGET_CHARS_MAX = 10000
CHUNK_PARENT_MAX_PAGES_DEFAULT = 3
CHUNK_PARENT_MAX_PAGES_MIN = 1
CHUNK_PARENT_MAX_PAGES_MAX = 5
CHUNK_PARENT_MAX_CHILDREN_DEFAULT = 12
CHUNK_PARENT_MAX_CHILDREN_MIN = 3
CHUNK_PARENT_MAX_CHILDREN_MAX = 20
# 親子階層（small-to-big）の分割パラメータの Settings 属性名
# (保存・受け渡し・chunk_set の hash で使う)。
SMALL_TO_BIG_SETTING_FIELDS: tuple[str, ...] = (
    "rag_chunk_child_target_chars",
    "rag_chunk_table_child_target_chars",
    "rag_chunk_parent_target_chars",
    "rag_chunk_parent_max_pages",
    "rag_chunk_parent_max_children",
)
# 回答フローの選択肢(rag_engine.generation.answer_models の ID と一致させる)。
QueryStrategy = Literal[
    "auto_routing",
    "simple_retrieval",
    "rag_fusion",
    "query_decomposition",
    "step_back_prompting",
    "hyde",
]
AnswerFlow = Literal["crag", "standard_rag"]
# 回答の LLM の用途ごとの推論(思考)の量(#1499)。model はモデルの登録値(共通のモデル設定)のまま、
# none は送らない、low / medium / high はその値を送る(platform の推論の量の選択肢と同じ)。
ReasoningEffortOverride = Literal["model", "none", "low", "medium", "high"]
VectorIndexProfile = Literal[
    "balanced",
    "accurate",
    "fast",
]
# 評価の基準(閾値のプリセット。#591)。
EvaluationSuite = Literal[
    "standard",
    "strict",
]
# 文書構造索引の構築(#621)。off = 構築しない、entities = 文書と章・節の見出しのつながりを構築する。
GraphProfile = Literal[
    "off",
    "entities",
]
# #621 で削除した文書構造索引の構築の値。backend/.env に残っていると起動を止めて書き換えを促す。
REMOVED_GRAPH_PROFILES = frozenset({"full"})
EnterpriseAiVlmInputMode = Literal["files_api", "inline_image"]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
# RAG 固有の設定（`RAG_*`）を置く `backend/.env`。
BACKEND_ENV_FILE = product_env_file("RAG_", BACKEND_ROOT)
# 3製品共通の設定（`PLATFORM_*`）を置く `platform/.env`。`PLATFORM_ENV_FILE` で上書きできる。
PLATFORM_ENV_FILE = platform_env_file(BACKEND_ROOT)
DEFAULT_MODEL_SETTINGS_FILE = "model-settings.json"
# 文書解析の設定（RAG 専用。#1701）。相対 path は backend/ 基準。
DEFAULT_PARSER_SETTINGS_FILE = "parser-settings.json"
# 言語資源の全体の上書き(#2181)。
DEFAULT_LANGUAGE_PACK_FILE = "language-pack.json"
DEFAULT_LOCAL_STORAGE_DIR = "/u01/data/production-ready-rag"


# 検索・回答プロファイル（と品質評価の rag_overrides）の値だけで決める Settings の項目（#1388）。
# プロファイルの値は Settings に重ねて回答の検索へ渡す（``compose_query_settings``）ため Settings の
# 項目として持つが、環境変数 / .env からは読まない（既定はコードの Settings の既定）。値の範囲は
# Settings の検証で持つ。プロファイルで上書きできない項目は ``PROFILE_GLOBAL_QUERY_FIELDS``
# （``app/rag/search_answer_profile_config.py``）が正本。
PROFILE_ONLY_SETTING_FIELDS = frozenset(
    {
        # rerank の後、起点に採る同じ文書の chunk の上限（#2167）。既定 5。
        "rag_max_anchors_per_document",
    }
)


def _without_profile_only_fields(
    source: PydanticBaseSettingsSource,
) -> Callable[[], dict[str, Any]]:
    """環境変数 / .env の source から、プロファイルだけで決める項目を除く。"""
    excluded = {
        name.casefold()
        for field in PROFILE_ONLY_SETTING_FIELDS
        for name in (field, settings_env_name("RAG_", field))
    }

    def read() -> dict[str, Any]:
        return {key: value for key, value in source().items() if key.casefold() not in excluded}

    # pydantic-settings は source の名前ごとに読んだ値を持つ（debug の表示）。元の名前を保つ。
    read.__name__ = type(source).__name__
    return read


class _PersistedParserAdapterSettings(BaseModel):
    """UI から保存された文書解析 backend と外部接続設定（`parser-settings.json`。#1701）。

    API key は持たない（RAG の backend/.env の `RAG_PARSER_*_API_KEY`）。
    """

    # 既定エンジンは Docling(#286)。選択中の engine の flag だけを既定で有効にする。
    adapter_backend: ParserAdapterBackend = DEFAULT_PARSER_ADAPTER_BACKEND
    docling_enabled: bool = True
    unstructured_enabled: bool = False

    # 削除済みエンジンの項目(marker_enabled・mineru_enabled 等)と、画面から外した
    # docling_vision_enabled(#497。Vision の既定は backend/.env の RAG_VISION_ENABLED で
    # 決める。#528)は既定の extra=ignore で読み捨てる。
    # adapter_backend に削除済みエンジンが残っていれば既定エンジンへ寄せる(#270・#22)。
    @field_validator("adapter_backend", mode="before")
    @classmethod
    def normalize_adapter_backend(cls, value: object) -> object:
        return normalize_parser_adapter_backend_value(value)


# 取込の worker の同時実行数に書ける上限(全体・重い段とも)。
INGESTION_QUEUE_MAX_CONCURRENCY = 16


def detect_vcpu_count() -> int:
    """この process が使える vCPU の数(OCI の Flex の Compute では OCPU × 2)。

    cgroup / taskset で絞られた CPU を数えるため ``os.sched_getaffinity`` を先に使う(#2027)。
    """
    sched_getaffinity = getattr(os, "sched_getaffinity", None)
    if sched_getaffinity is not None:
        with contextlib.suppress(OSError):
            return max(1, len(sched_getaffinity(0)))
    return max(1, os.cpu_count() or 1)


@dataclass(frozen=True)
class IngestionQueueConcurrency:
    """取込の worker の同時実行数の決まった値と、その元(起動時のログに出す。#2027)。"""

    total: int
    heavy_phase: int
    vcpu: int
    total_auto: bool
    heavy_phase_auto: bool

    def log_fields(self) -> dict[str, object]:
        return {
            "worker_concurrency": self.total,
            "heavy_phase_concurrency": self.heavy_phase,
            "vcpu": self.vcpu,
            "worker_concurrency_auto": self.total_auto,
            "heavy_phase_concurrency_auto": self.heavy_phase_auto,
        }


def resolve_ingestion_queue_concurrency(
    total: int | Literal["auto"],
    heavy_phase: int | Literal["auto"],
    *,
    vcpu: int,
) -> IngestionQueueConcurrency:
    """同時実行数の設定(数か auto)から、worker の全体の枠と重い段の上限を決める(#2027)。

    auto の全体は max(2, vCPU − 2)、重い段は max(1, (vCPU − 2) // 3)。3 製品の backend・Nginx・
    解析サービスに CPU を残す。解析(重い段)は 1 回で複数のスレッドの CPU を使うので CPU の数で
    絞り、LLM の応答待ちが多い段は全体の枠で並べる。重い段は全体を超えない。
    """
    reserved = max(vcpu - 2, 0)
    resolved_total = (
        min(INGESTION_QUEUE_MAX_CONCURRENCY, max(2, reserved)) if total == "auto" else total
    )
    resolved_heavy = max(1, reserved // 3) if heavy_phase == "auto" else heavy_phase
    return IngestionQueueConcurrency(
        total=resolved_total,
        heavy_phase=min(resolved_heavy, resolved_total),
        vcpu=vcpu,
        total_auto=total == "auto",
        heavy_phase_auto=heavy_phase == "auto",
    )


class Settings(
    PlatformEnvSourcesMixin,
    RuntimeSettings,
    ModelSecretStateMixin,
    TracingSettings,
    TimezoneSettings,
    BaseSettings,
):
    """環境変数ベースの設定。旧名（属性名と同じ環境変数名）は読まない（#211）。"""

    # 環境変数名は属性名から決める（共通の属性は `PLATFORM_*`、それ以外は `RAG_*`。#211）。
    model_config = product_settings_config(prefix="RAG_", backend_dir=BACKEND_ROOT)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[Any, ...]:
        """共通の source（旧名を読まない）から、プロファイルだけで決める項目を除く（#1388）。"""
        init, env, dotenv, secrets = super().settings_customise_sources(
            settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
        )
        return (
            init,
            _without_profile_only_fields(env),
            _without_profile_only_fields(dotenv),
            secrets,
        )

    # --- アプリ ---
    app_name: str = "production-ready-rag"
    log_level: str = Field(default="INFO")
    app_version: str = Field(default="0.1.0")
    # --- 共通認証（platform の pr_system_settings.auth。#214）---
    # 構成管理者・認証ポリシー・ログインの Cookie 名は共通 `.env` の PLATFORM_ADMIN_* /
    # PLATFORM_AUTH_*（#211）。Cookie は 3 製品で共有し、1 回のログインで全製品を使える（#1565）。
    service_name: str = Field(default="production-ready-rag")
    app_admin_login_user_id: str = Field(default="")
    app_admin_login_user_password: str = Field(default="")
    app_auth_session_cookie_name: str = Field(default=DEFAULT_SESSION_COOKIE_NAME)
    app_auth_csrf_cookie_name: str = Field(default=DEFAULT_CSRF_COOKIE_NAME)
    app_auth_idle_timeout_minutes: int = Field(default=60)
    app_auth_absolute_timeout_hours: int = Field(default=12)
    app_auth_failed_login_limit: int = Field(default=5)
    app_auth_lockout_minutes: int = Field(default=15)
    # ログインの試行の回数の制限（ログイン ID と送信元 IP の組・送信元 IP ごと。0 で無効。#1087）。
    app_auth_login_attempt_limit: int = Field(default=5)
    app_auth_login_ip_attempt_limit: int = Field(default=20)
    app_auth_login_attempt_window_minutes: int = Field(default=15)
    app_auth_password_min_length: int = Field(default=12)
    app_auth_password_max_length: int = Field(default=128)
    app_auth_argon2_time_cost: int = Field(default=3)
    app_auth_argon2_memory_kib: int = Field(default=65536)
    app_auth_argon2_parallelism: int = Field(default=4)
    # Agent が MCP（`POST /api/mcp`）を利用者として呼ぶサービストークンの署名鍵（#230 / #232）。
    # 共通 `.env` の PLATFORM_SERVICE_TOKEN_SECRET。空なら MCP は 503 で拒否する。
    app_service_token_secret: str = Field(default="", repr=False)
    # 公開の基底の URL（共通 `.env` の PLATFORM_PUBLIC_BASE_URL。例 `https://<公開 IP>`）。配備の
    # suite-init.sh が書き、API キーの画面の接続先に出す（#1870）。
    app_public_base_url: str = ""
    # 外部の MCP クライアントの API キーの 1 分あたりの呼び出しの回数（#1874。0 は上限なし）。
    app_api_key_calls_per_minute: int = Field(default=60, ge=0, le=100_000)
    model_settings_file: str = Field(
        default=DEFAULT_MODEL_SETTINGS_FILE,
        description="UI から保存した共有ランタイム設定 JSON。存在する場合は .env より優先する。",
    )
    rag_parser_settings_file: str = Field(
        default=DEFAULT_PARSER_SETTINGS_FILE,
        description=(
            "「文書解析」画面から保存した解析エンジンと外部接続の JSON（RAG 専用。#1701）。"
            "存在する場合は .env より優先する。相対 path は backend/ 基準。"
        ),
    )

    rag_language_pack_file: str = Field(
        default=DEFAULT_LANGUAGE_PACK_FILE,
        description=(
            "「言語資源」画面から保存した、組み込みの言語資源との差の JSON(#2181)。"
            "相対 path は backend/ 基準。"
        ),
    )

    # --- HuggingFace モデルダウンロード ---
    # RAG_HUGGINGFACE_TOKEN / RAG_HUGGINGFACE_ENDPOINT。huggingface_hub が読む HF_TOKEN /
    # HF_ENDPOINT は、サービス管理が起動/再起動の前にサービス実行用の env ファイルへ書き、
    # parser の systemd の unit が EnvironmentFile で読む(#286)。
    huggingface_token: str = Field(default="")
    huggingface_endpoint: str = Field(default="")

    # CORS 許可オリジン（フロントエンド）
    cors_origins: list[str] = Field(default=["http://localhost:3000"])

    # --- OCI 共通 ---
    oci_config_file: str = Field(default="~/.oci/config")
    oci_config_profile: str = Field(default="DEFAULT")
    oci_region: str = Field(default="")
    oci_compartment_id: str = Field(default="")

    # --- OCI Enterprise AI（LLM / Vision-capable LLM）---
    # 注意: OCI Generative AI の chat 推論 API ではなく Enterprise AI を使う
    oci_enterprise_ai_endpoint: str = Field(default="")
    oci_enterprise_ai_project_ocid: str = Field(default="")
    oci_enterprise_ai_api_key: str = Field(default="")
    # OCI Enterprise AI のセカンダリ接続（#533）。モデルを呼ぶ接続は
    # enterprise_ai_connection_for_model で引く。
    oci_enterprise_ai_secondary_endpoint: str = Field(default="")
    oci_enterprise_ai_secondary_project_ocid: str = Field(default="")
    oci_enterprise_ai_secondary_api_key: str = Field(default="")
    # ターシャリ接続（#786。OpenAI / OpenAI 互換 API 向け。Project OCID は任意）。
    oci_enterprise_ai_tertiary_endpoint: str = Field(default="")
    oci_enterprise_ai_tertiary_project_ocid: str = Field(default="")
    oci_enterprise_ai_tertiary_api_key: str = Field(default="")
    oci_enterprise_ai_models: list[EnterpriseAiConfiguredModel] = Field(default_factory=list)
    # 既定のテキストモデルと既定の Vision モデル（#499。画面・API では 2 つとも必須。#566）。
    # 呼び出しに使う ID は enterprise_ai_default_model_id / enterprise_ai_vision_model_id
    # で解決する。
    oci_enterprise_ai_default_text_model: str = Field(default="")
    oci_enterprise_ai_default_vision_model: str = Field(default="")
    # 品質評価の判定のモデル（#3574。空 = 既定のテキストモデル、
    # same_as_answer = 回答と同じモデル）。
    # 判定に使う ID は enterprise_ai_judge_model_id で解決する。
    oci_enterprise_ai_default_judge_model: str = Field(default="")
    oci_enterprise_ai_llm_path: str = Field(default="/responses")
    oci_enterprise_ai_vlm_path: str = Field(default="/responses")
    oci_enterprise_ai_vlm_input_mode: EnterpriseAiVlmInputMode = Field(
        default="files_api",
        description=(
            "Enterprise AI VLM への入力搬送方式。files_api は VLM 入力を明示的に /files "
            "経由へ送る。inline_image は画像のみ inline。"
        ),
    )
    oci_enterprise_ai_llm_payload_template: str = Field(
        default="",
        description=(
            "Enterprise AI LLM endpoint の request JSON template。空なら標準 RAG payload。"
        ),
    )
    oci_enterprise_ai_vlm_payload_template: str = Field(
        default="",
        description=(
            "Enterprise AI VLM endpoint の request JSON template。空なら標準 OCR payload。"
        ),
    )
    oci_enterprise_ai_llm_response_path: str = Field(
        default="",
        description=(
            "Enterprise AI LLM response から回答候補を取り出す JSON Pointer。"
            "空なら既知 envelope を順番に照合する。"
        ),
    )
    oci_enterprise_ai_vlm_response_path: str = Field(
        default="",
        description=(
            "Enterprise AI VLM response から StructuredExtraction 候補を取り出す JSON Pointer。"
            "空なら既知 envelope を順番に照合する。"
        ),
    )
    oci_enterprise_ai_timeout_seconds: float = Field(
        default=OCI_ENTERPRISE_AI_TIMEOUT_MAX_SECONDS,
        gt=0.0,
        le=OCI_ENTERPRISE_AI_TIMEOUT_MAX_SECONDS,
    )
    oci_enterprise_ai_max_retries: int = Field(default=3, ge=0, le=5)
    # 出力の上限は登録モデルごとの `max_output_tokens`(#1719。platform の
    # `enterprise_ai_output_token_budget`)。共通の LLM / VLM の出力の上限は持たない。
    # チャットで同じ会話の前のターンを LLM に渡す予算（共通の設定。#1818）。
    # 解釈は pr_backend_core.chat_history（新しいターンから詰め、古いターンを落とす）。
    chat_history_max_turns: int = Field(
        default=CHAT_HISTORY_DEFAULT_MAX_TURNS, ge=0, le=CHAT_HISTORY_MAX_TURNS_LIMIT
    )
    chat_history_max_chars_per_turn: int = Field(
        default=CHAT_HISTORY_DEFAULT_MAX_CHARS_PER_TURN,
        ge=CHAT_HISTORY_MIN_CHARS_PER_TURN,
        le=CHAT_HISTORY_MAX_CHARS_PER_TURN_LIMIT,
    )
    chat_history_max_chars: int = Field(
        default=CHAT_HISTORY_DEFAULT_MAX_CHARS,
        ge=CHAT_HISTORY_MIN_CHARS,
        le=CHAT_HISTORY_MAX_CHARS_LIMIT,
    )
    # チャットの「会話の履歴」に出す期間（日。共通の設定。#2340）。0 は期間で絞らない。
    # 解釈は pr_backend_core.chat_history_window（最後の更新が直近 N 日の会話だけを一覧に出す）。
    chat_history_days: int = Field(
        default=CHAT_HISTORY_DEFAULT_DAYS, ge=0, le=CHAT_HISTORY_DAYS_LIMIT
    )
    # 会話の添付（共通の設定。#2517）。解釈は pr_system_settings.chat_attachments.limits。
    chat_attachment_ttl_hours: int = Field(
        default=CHAT_ATTACHMENT_DEFAULT_TTL_HOURS,
        ge=CHAT_ATTACHMENT_TTL_HOURS_MIN,
        le=CHAT_ATTACHMENT_TTL_HOURS_MAX,
    )
    chat_attachment_max_bytes: int = Field(
        default=CHAT_ATTACHMENT_DEFAULT_MAX_BYTES,
        ge=CHAT_ATTACHMENT_MAX_BYTES_MIN,
        le=CHAT_ATTACHMENT_MAX_BYTES_MAX,
    )
    chat_attachment_max_files: int = Field(
        default=CHAT_ATTACHMENT_DEFAULT_MAX_FILES,
        ge=CHAT_ATTACHMENT_MAX_FILES_MIN,
        le=CHAT_ATTACHMENT_MAX_FILES_MAX,
    )

    # --- OCI Generative AI（埋め込み / リランク）---
    oci_genai_embedding_model: str = Field(default="cohere.embed-v4.0")
    oci_genai_embedding_dim: int = Field(
        default=1536,
        ge=1536,
        le=1536,
        description="Cohere Embed v4 と Oracle VECTOR(1536, FLOAT32) に合わせる。",
    )
    oci_genai_rerank_model: str = Field(default="cohere.rerank-v4.0-fast")

    # --- Oracle AI Database ---
    oracle_user: str = Field(default="")
    oracle_password: str = Field(default="")
    oracle_dsn: str = Field(default="")
    oracle_client_lib_dir: str = Field(
        default="",
        description=(
            "指定したときだけ Thick mode（Instant Client）で接続する。既定は空で Thin mode。"
        ),
    )
    oracle_wallet_dir: str = Field(
        default="/u01/aipoc/wallet",
        description=(
            "Thin mode の Wallet 配置先。"
            "Thick mode では PLATFORM_ORACLE_CLIENT_LIB_DIR/network/admin を使う。"
        ),
    )
    oracle_wallet_password: str = Field(default="")
    oracle_adb_ocid: str = Field(
        default="",
        description=(
            "Autonomous Database 操作対象の OCID。起動 / 停止 / 情報取得に使う。"
            "ベクトル検索とは別経路の OCI Database 制御プレーン操作用。"
        ),
    )
    oracle_adb_region: str = Field(
        default="",
        description=(
            "Autonomous Database 管理専用の OCI region。未設定なら PLATFORM_OCI_REGION を使う。"
        ),
    )
    oracle_tcp_connect_timeout_seconds: float = Field(
        default=10.0,
        gt=0.0,
        le=120.0,
        description="Oracle TCP 接続の待機秒数。ADB/Wallet 疎通確認を長時間ブロックしない。",
    )
    oracle_db_test_timeout_seconds: float = Field(
        default=15.0,
        gt=0.0,
        le=180.0,
        description="Oracle 接続テスト API 全体の待機秒数。",
    )
    oracle_pool_max: int = Field(
        default=16,
        ge=1,
        le=128,
        description=(
            "backend の Oracle の接続 pool の上限(接続の数)。回答の検索は 1 問の中で"
            "検索文をまとめた vector / keyword の SQL を並行に読み(#1498)、1 問で 2 本程度の"
            "接続を使うため、同時に答える問の数に合わせる。"
        ),
    )
    oracle_pool_min: int = Field(
        default=4,
        ge=1,
        le=128,
        description=(
            "backend(API)の起動時に開いておく Oracle の接続の数(接続 pool の min。#1498)。"
            "起動直後の問で接続の作成を待たない。取込の job のプロセスは 1 本から始める。"
            "RAG_ORACLE_POOL_MAX より大きければ RAG_ORACLE_POOL_MAX にする。"
        ),
    )
    oracle_vector_target_accuracy: int = Field(
        default=95,
        ge=1,
        le=100,
        description="Oracle AI Vector Search の FETCH APPROX target accuracy。",
    )

    # --- OCI Object Storage ---
    object_storage_region: str = Field(default="")
    object_storage_namespace: str = Field(default="")
    object_storage_bucket: str = Field(default="production-ready")
    upload_storage_backend: UploadStorageBackend = Field(
        default="local",
        description=(
            "アップロード原本の保存先。local は PLATFORM_LOCAL_STORAGE_DIR、"
            "oci は OCI Object Storage。"
        ),
    )

    # --- ローカルアップロード保存先 ---
    local_storage_dir: str = Field(default=DEFAULT_LOCAL_STORAGE_DIR)
    max_upload_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    allowed_upload_content_types: list[str] = Field(
        default=[
            "application/pdf",
            "image/gif",
            "image/jpeg",
            "image/jpg",
            "image/png",
            "image/tif",
            "image/webp",
            "image/tiff",
            "text/plain",
            "text/markdown",
            "text/csv",
            "text/tab-separated-values",
            # reStructuredText・Org(プレーンテキストとして読む。#3351)。
            "text/x-rst",
            "text/prs.fallenstein.rst",
            "text/org",
            "text/x-org",
            "text/html",
            "application/xhtml+xml",
            "application/json",
            "application/jsonl",
            "application/jsonlines",
            "application/ndjson",
            "application/xml",
            # .xml は text/xml でも送られる(#3360)。
            "text/xml",
            "application/csv",
            "application/x-ndjson",
            "message/rfc822",
            "application/eml",
            "application/vnd.ms-outlook",
            "application/x-msg",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-powerpoint",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.ms-excel",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            # マクロ付きの Office(ファイル準備で変換してから読む。マクロは実行しない。#2470)。
            "application/vnd.ms-word.document.macroEnabled.12",
            "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
            "application/vnd.ms-excel.sheet.macroEnabled.12",
            # ODT・RTF(ファイル準備の Office→PDF で PDF にしてから読む。#3351)。
            "application/vnd.oasis.opendocument.text",
            "application/rtf",
            "application/x-rtf",
            "text/rtf",
            "application/octet-stream",
        ]
    )

    # --- 取込 queue ---
    ingestion_queue_startup_recovery_enabled: bool = Field(
        default=True,
        description="起動時に永続化済み QUEUED job と stale RUNNING job を自動回復する。",
    )
    ingestion_queue_startup_drain_limit: int = Field(default=50, ge=1, le=500)
    ingestion_queue_stale_running_seconds: float = Field(
        default=300.0,
        gt=0.0,
        le=86400.0,
        description=(
            "heartbeat の無い RUNNING job(lease 導入前の行・lease を持たない実行)を"
            "stale とみなすまでの秒数。heartbeat のある job は"
            "RAG_INGESTION_QUEUE_LEASE_TTL_SECONDS で判定する(#357)。"
        ),
    )
    ingestion_queue_heartbeat_interval_seconds: float = Field(
        default=15.0,
        gt=0.0,
        le=600.0,
        description="取込 worker が自分の lease の RUNNING job に heartbeat を打つ間隔(秒)。#357",
    )
    ingestion_queue_lease_ttl_seconds: float = Field(
        default=90.0,
        gt=0.0,
        le=3600.0,
        description=(
            "heartbeat がこの秒数途絶えた RUNNING job を、worker が止まったとみなして回復する。"
            "heartbeat 間隔の 3 倍以上にする。job の長さの上限は job の timeout だけが持つ(#357)。"
        ),
    )
    ingestion_queue_shutdown_grace_seconds: float = Field(
        default=60.0,
        ge=0.0,
        le=3600.0,
        description=(
            "取込 worker が停止(SIGTERM)を受けてから、実行中の job の完了を待つ上限(秒)。"
            "超えたら子プロセスを止め、自分の lease の job を QUEUED に戻す(attempt は増やさない)。"
            "systemd の TimeoutStopSec はこれより長くする(#357)。"
        ),
    )
    ingestion_queue_recovery_interval_seconds: float = Field(
        default=60.0,
        gt=0.0,
        le=3600.0,
        description=(
            "専用ワーカーが起動後も stale/固着文書の復旧を再実行する最短間隔（秒）。"
            "クラッシュで INGESTING のまま取り残された文書を再起動なしで回復させる。"
        ),
    )
    ingestion_queue_worker_concurrency: int | Literal["auto"] = Field(
        default="auto",
        description=(
            "取込の worker が同時に処理する job(文書)の数(1〜16)。既定 auto は vCPU の数から"
            "max(2, vCPU − 2)(#2027)。3 製品の backend・Nginx・解析サービスに CPU を残す。"
            "軽い段(Chunk 作成・索引)は LLM / Embedding の応答待ちが多いので、全体の枠で並べる。"
            "Enterprise AI の 429 が増えるときは数を書いて下げる。"
        ),
    )
    ingestion_queue_heavy_phase_concurrency: int | Literal["auto"] = Field(
        default="auto",
        description=(
            "全体の枠のうち、重い段(ファイル準備・抽出。解析サービスの CPU を使う)を同時に処理する"
            "上限(1〜16。全体を超えない)。既定 auto は max(1, (vCPU − 2) // 3)(#2027)。解析は"
            "1 回で複数のスレッドの CPU を使うため、重い段だけを CPU の数で絞る。"
        ),
    )
    ingestion_queue_pipeline_priority_window_seconds: int = Field(
        default=3600,
        ge=0,
        le=86400,
        description=(
            "続きの段(Chunk 作成・索引)の job を、その文書の取込の最初の段の投入の時刻まで"
            "繰り上げて取る上限(秒。#2027)。始めた文書を先に終わらせ(パイプライン)、"
            "この秒数より前から待っている job は追い越さない(飢餓を防ぐ)。0 なら投入の順(FIFO)。"
        ),
    )
    ingestion_job_max_attempts: int = Field(default=3, ge=1, le=20)
    ingestion_queue_dedicated_worker_enabled: bool = Field(
        default=True,
        description=(
            "True にすると取込はキュー投入のみとし、専用ワーカー（in-process または別プロセス）"
            "がジョブを消費する。HTTP リクエスト内では取込を実行しない。"
        ),
    )
    ingestion_queue_poll_interval_seconds: float = Field(
        default=2.0,
        gt=0.0,
        le=60.0,
        description="専用ワーカーが QUEUED ジョブをポーリングする間隔（秒）。",
    )
    ingestion_queue_inprocess_worker_enabled: bool = Field(
        default=True,
        description=(
            "専用ワーカーモード時に API プロセス内（lifespan）でもワーカーを起動するか。"
            "別プロセスのワーカーへ完全に切り出す場合は False にする。"
        ),
    )
    ingestion_queue_process_isolation_enabled: bool = Field(
        default=True,
        description=(
            "in-process ワーカーが job 本体を subprocess で実行し、Docling/OCR/CUDA 初期化を "
            "API プロセスから隔離する。専用の取込 worker(別プロセス)では False にして"
            "直接実行できる。"
        ),
    )
    ingestion_job_subprocess_timeout_seconds: float = Field(
        default=INGESTION_JOB_SUBPROCESS_TIMEOUT_DEFAULT_SECONDS,
        gt=0.0,
        le=INGESTION_JOB_SUBPROCESS_TIMEOUT_MAX_SECONDS,
        description=(
            "process isolation 時の 1 取込 job の全体の上限(秒。保険)。進んでいる job も"
            "これを超えたら止める。1 文書 1,000 図を 1 件 60 秒で順に読んでも足りる 24 時間を"
            "既定にする(#2003)。ふだんの打ち切りは RAG_INGESTION_JOB_STALL_TIMEOUT_SECONDS。"
        ),
    )
    ingestion_job_stall_timeout_seconds: float = Field(
        default=INGESTION_JOB_STALL_TIMEOUT_DEFAULT_SECONDS,
        gt=0.0,
        le=INGESTION_JOB_SUBPROCESS_TIMEOUT_MAX_SECONDS,
        description=(
            "process isolation 時、進捗を出す段(解析のページ・図・表の説明)の途中で job の"
            "進捗(rag_ingestion_jobs.progress_json)がこの秒数変わらなければ止める(#2003)。"
            "進捗を出さない 1 回の呼び出し(解析サービス・"
            "Vision の LLM。再試行を含む)の最長より長くする(起動時に検証する)。"
        ),
    )

    # --- RAG ---
    rag_chunk_size: int = Field(
        default=800,
        ge=CHUNK_SIZE_MIN_CHARS,
        le=CHUNK_SIZE_MAX_CHARS,
    )
    rag_chunk_overlap: int = Field(default=120, ge=0, le=CHUNK_OVERLAP_MAX_CHARS)
    rag_chunking_strategy: ChunkingStrategy = Field(
        default="small_to_big",
        description=(
            "chunks 段階の分割戦略(Chunking アダプター)。"
            "small_to_big(既定)は親子階層(small-to-big。どの解析の結果でも、"
            "見出し・節・頁の構造から検索用の子と回答の文脈の親を作る)、"
            "structure_aware は element/section/table 認識、recursive_character は固定長、"
            "markdown_heading は章節単位、page_level はページ単位、"
            "fixed_size は章節・文境界を無視した純粋な固定長分割、"
            "fixed_delimiter は指定文字列での固定分割。"
        ),
    )
    rag_chunk_delimiter: str = Field(
        # 既定は構造化レコードの JSONL を読んだ抽出の記録の境界のマーカー(#2457)。
        default=JSONL_RECORD_BOUNDARY,
        min_length=1,
        max_length=256,
        description="fixed_delimiter 戦略で使う分割符。\\n / \\t / \\\\ の escape 表現を許可する。",
    )
    # 親子階層（small-to-big。`small_to_big`）の分割パラメータ。
    # rag_poc の「チャンキング」tab と同じ 5 項目。
    rag_chunk_child_target_chars: int = Field(
        default=CHUNK_CHILD_TARGET_CHARS_DEFAULT,
        ge=CHUNK_CHILD_TARGET_CHARS_MIN,
        le=CHUNK_CHILD_TARGET_CHARS_MAX,
        description=(
            "親子階層(small-to-big)で、検索に使う子 chunk の目標文字数。"
            "超える Text / List-item は文末で複数の子へ分ける。"
        ),
    )
    rag_chunk_table_child_target_chars: int = Field(
        default=CHUNK_TABLE_CHILD_TARGET_CHARS_DEFAULT,
        ge=CHUNK_TABLE_CHILD_TARGET_CHARS_MIN,
        le=CHUNK_TABLE_CHILD_TARGET_CHARS_MAX,
        description=(
            "親子階層(small-to-big)で、表を行グループへ分ける閾値と各グループの目標文字数。"
            "各グループには列見出しと関連見出しを繰り返し付ける。"
        ),
    )
    rag_chunk_parent_target_chars: int = Field(
        default=CHUNK_PARENT_TARGET_CHARS_DEFAULT,
        ge=CHUNK_PARENT_TARGET_CHARS_MIN,
        le=CHUNK_PARENT_TARGET_CHARS_MAX,
        description="親子階層(small-to-big)で、回答文脈に使う親 chunk の目標文字数。",
    )
    rag_chunk_parent_max_pages: int = Field(
        default=CHUNK_PARENT_MAX_PAGES_DEFAULT,
        ge=CHUNK_PARENT_MAX_PAGES_MIN,
        le=CHUNK_PARENT_MAX_PAGES_MAX,
        description="親子階層(small-to-big)で、1 つの親 chunk がまたげる最大ページ数。",
    )
    rag_chunk_parent_max_children: int = Field(
        default=CHUNK_PARENT_MAX_CHILDREN_DEFAULT,
        ge=CHUNK_PARENT_MAX_CHILDREN_MIN,
        le=CHUNK_PARENT_MAX_CHILDREN_MAX,
        description="親子階層(small-to-big)で、1 つの親 chunk に入れる子 chunk の最大数。",
    )
    rag_chunk_min_chars: int = Field(
        default=120,
        ge=0,
        le=2000,
        description=(
            "この文字数未満の微小 chunk を隣接 chunk へ吸収する下限。0 で無効。"
            "rag_chunk_size より小さくする。"
        ),
    )
    rag_chunk_context_header_enabled: bool = Field(
        default=True,
        description=(
            "embedding 入力の先頭へ「文書名 > section_path」の文脈ヘッダを前置する"
            "(Anthropic Contextual Retrieval の決定論版)。保存 chunk 本文・引用表示は変えない。"
        ),
    )

    rag_context_group_max_chunks: int = Field(
        default=4,
        ge=1,
        le=20,
        description="同一 chunk group から anchor ごとに追加する sibling chunk 数の上限。",
    )
    rag_reference_expansion_enabled: bool = Field(
        default=True,
        description=(
            "回答の検索で、上位の候補の chunk が本文で参照する節(「第3章を参照」など。取込時に"
            "解決して chunk の metadata に保存)の chunk を rerank の候補に足す(#1280)。"
            "LLM の呼び出しは増えない。参照の無い候補だけなら SQL も増えない。"
        ),
    )
    rag_reference_expansion_max_chunks: int = Field(
        default=4,
        ge=1,
        le=20,
        description="交差参照で 1 回の検索に足す参照先の chunk 数の上限(#1280)。",
    )
    rag_entity_index_enabled: bool = Field(
        default=False,
        description=(
            "取込の索引の保存の後に、chunk から実体（表の行の名前・属性の値、本文の定義の形・"
            "言及）と「実体と chunk の関連」を決定的に抜き出し、Oracle の rag_entities / "
            "rag_entity_aliases / rag_entity_chunks に保存する。既定 OFF。全体の設定と文書"
            "レシピの「実体・別名索引」で構築を選ぶ。LLM は使わない。回答の検索は、"
            "この表との SQL の join で関連する chunk を 1 段だけ足す（検索方法の全体設定の"
            " entity_expansion_enabled。#1388）。"
        ),
    )
    rag_entity_name_columns: list[str] = Field(
        default_factory=list,
        description=(
            "実体の抽出で、表の行の名前（同じ実体の別名）にする列名（#1362）。空なら列名で"
            "決める（ID・番号・コード・正式名・名称・〜名・略称・別表記・通称）。"
        ),
    )
    rag_entity_attribute_columns: list[str] = Field(
        default_factory=list,
        description=(
            "実体の抽出で、表の行の属性の値を実体にする列名（#1362。担当部署・重要度など）。"
            "空なら名前の列・長い本文の列（説明・備考など）・日付や数値の列を除いた列。"
        ),
    )
    # 名称・別名と章節の拡張は検索方法の全体設定で選ぶ。
    rag_entity_expansion_enabled: bool = Field(
        default=False,
        description=(
            "回答の検索で、質問と上位の候補の chunk の実体から、実体の表との SQL の join で"
            "関連する chunk を 1 段だけ足す（#1362）。検索方法の全体設定で選ぶ（既定 OFF）。"
            "実体は文書レシピで実体・別名索引の構築"
            "（rag_entity_index_enabled）を選んだ文書にだけある。LLM の呼び出しは増えない。"
        ),
    )
    rag_entity_expansion_max_chunks: int = Field(
        default=6,
        ge=1,
        le=20,
        description=(
            "実体の 1 段の拡張で 1 回の検索に足す chunk 数の上限（#1362）。検索方法の"
            "全体設定で選ぶ（#1388）。"
        ),
    )
    rag_graph_expansion_enabled: bool = Field(
        default=False,
        description=(
            "回答の検索で、文書構造索引の構築（rag_graph_profile=entities）で作った章節・表 / 図の"
            "見出しの entity と chunk の対応から候補を足す（#2055。既定 OFF。LLM の呼び出しは"
            "増えない）。検索の上位の chunk と同じ狭い章節・表の chunk と、質問に見出しの語が"
            "含まれる章節の chunk を足す。検索方法の画面で選ぶ（全体設定だけで決め、"
            "検索・回答プロファイルでは上書きしない）。文書構造索引の無い文書では何もしない。"
        ),
    )
    rag_graph_expansion_max_chunks: int = Field(
        default=6,
        ge=1,
        le=20,
        description=(
            "文書構造による検索拡張で 1 回の検索に足す chunk 数の上限（#2055）。"
            "全体設定だけで決め、検索・回答プロファイルでは上書きしない。"
        ),
    )
    rag_section_expansion_enabled: bool = Field(
        default=True,
        description=(
            "回答で、当たった chunk の章節（章節ナビゲーション）から親の章節へ段階的に広げて"
            "文脈に入れる（#1436。既定 ON）。根拠が十分でも最低 1 段（所有の章節）を読み、"
            "新たな根拠を加えた後に CRAG の十分性を確かめ、足りれば止める"
            "（判定ごとに LLM の呼び出しが 1 回増える）。標準 RAG では必要な範囲の原文を補完し、"
            "十分性の評価器は呼ばない。一部取得は読了にしない。"
            "検索方法の全体設定で選ぶ。"
        ),
    )
    rag_section_expansion_max_levels: int = Field(
        default=4,
        ge=1,
        le=8,
        description="章節の拡張で上がる段の上限（#1436。既定 4。所有の章節を 1 段目に数える）。",
    )
    rag_section_expansion_input_token_percent: int = Field(
        default=50,
        ge=10,
        le=90,
        description=(
            "章節の拡張で、1 回の回答の根拠の入力 token の上限にする、回答のモデルの入力の上限"
            "（システム設定 › モデル の登録モデルの「入力の上限（tokens）」。#1437）に対する"
            "割合（%。#1436。既定 50）。"
        ),
    )
    rag_section_extraction_enabled: bool = Field(
        default=False,
        description=(
            "(#2800)章節の拡張で、根拠の入力の上限に入らない章節から質問に関係する原文を LLM で"
            "抜き出す(#1437)。既定 OFF は、入らない章節を rerank の順で上限まで入れ、"
            "読めなかった範囲を「回答に未反映の関連章節」に示す(生成の前の LLM の呼び出しを"
            "増やさない)。"
        ),
    )
    rag_max_anchors_per_document: int = Field(
        default=5,
        ge=0,
        le=20,
        description=(
            "rerank の後、回答の根拠の起点(上位 top_k 件)に採る同じ文書の chunk の上限(#2167。"
            "既定 5、0 は上限なし)。超えた chunk は落とさず後ろへ回し、別の文書の関連する chunk を"
            "起点に入れる。検索・回答プロファイルの max_anchors_per_document で選ぶ。"
        ),
    )
    rag_navigation_summary_enabled: bool = Field(
        default=False,
        description=(
            "取込時に navigation tree の各章節 node を OCI Enterprise AI LLM で要約し、"
            "progressive disclosure / Navigate retrieval に使う（既定 OFF）。"
        ),
    )
    rag_navigation_summary_max_nodes: int = Field(
        default=24,
        ge=1,
        le=200,
        description="navigation node 要約を生成する node 数の上限（LLM 呼び出し回数の bound）。",
    )
    # 見出しの階層の推定（#2277）。しおりの深さを当てられず、番号の体系でも多くの見出しの深さを
    # 決められない文書（Docling の結果）だけ、見出しの文字列を OCI Enterprise AI に渡して階層を
    # 受け取る。
    rag_heading_level_inference_enabled: bool = Field(
        default=False,
        description=(
            "しおりも番号も無い文書の見出しの階層を OCI Enterprise AI で推定する"
            "（#2277。既定 OFF）。"
            "見出しの文字列だけを渡し、応答の形が合わない・失敗のときは今の階層のまま。"
        ),
    )
    rag_heading_level_inference_min_headings: int = Field(
        default=3,
        ge=1,
        le=1000,
        description="見出しの階層を推定する文書の見出しの数の下限（これ未満は推定しない）。",
    )
    rag_heading_level_inference_max_headings: int = Field(
        default=300,
        ge=1,
        le=2000,
        description="見出しの階層を推定する文書の見出しの数の上限（これを超える文書は推定しない）。",
    )
    rag_heading_level_inference_unresolved_ratio: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "番号の体系で深さを決められない見出しの割合がこれ以上の文書だけ推定する（0〜1）。"
        ),
    )
    rag_heading_level_inference_max_title_chars: int = Field(
        default=120,
        ge=10,
        le=1000,
        description="見出しの階層の推定で LLM に渡す 1 つの見出しの文字数の上限。",
    )
    rag_field_extraction_enabled: bool = Field(
        default=False,
        description=(
            "取込時に field schema 定義に従い OCI Enterprise AI structured output で named "
            "field/entity を抽出する（PoweRAG/LangExtract 由来。既定 OFF。#2839。"
            "項目の定義が 0 件のときは何もしない。#537）。"
        ),
    )
    rag_field_extraction_input_token_percent: int = Field(
        default=40,
        ge=10,
        le=90,
        description=(
            "項目抽出の 1 回の要求に入れる入力の予算にする、既定のテキストモデルの入力の上限"
            "（システム設定 › モデル の「入力の上限（tokens）」）に対する割合"
            "（%。#2720。既定 40）。"
            "指示・項目の定義を含めてこの予算に収まるように本文を分けて抽出する。残りは出力の枠と、"
            "token の見積もりの誤差（まれな漢字が続く本文は見積もりより多い）の余裕に使う。"
        ),
    )
    rag_min_similarity: float = Field(default=0.05, ge=0.0, le=1.0)
    rag_rrf_k: int = Field(
        default=60,
        ge=1,
        le=1000,
        description="Hybrid retrieval の Reciprocal Rank Fusion 定数。",
    )
    rag_retrieval_query_limit: int = Field(
        default=600,
        ge=1,
        le=2000,
        description="検索文ごとに各検索経路から取得する上限。起点数・多様化の有無とは独立。",
    )
    rag_retrieval_candidate_limit: int = Field(
        default=150,
        ge=1,
        le=2000,
        description="融合順位から選ぶ基本候補の上限。回答起点数 top_k とは独立。",
    )
    rag_rerank_candidate_limit: int = Field(
        default=0,
        ge=0,
        le=2000,
        description="明示参照・多様化・各種拡張を含む精排入力の合計上限。0は有効な各段の既存上限の合計から互換上限を導出。",
    )
    rag_candidate_diversity_depth: int = Field(
        default=600,
        ge=0,
        le=2000,
        description=(
            "多様化を探す融合順位の深さ(0 は無効)。"
            "DB の取得上限は rag_retrieval_query_limit で独立。"
            "融合の上位(候補の上限)"
            "は変えず、その外から文書ごとの上位を rerank の候補に足す。1 つの文書が融合の上位を"
            "占めても、別の文書の根拠が rerank の判断を受ける(#2228)。"
        ),
    )
    rag_candidate_diversity_new_document: int = Field(
        default=10,
        ge=0,
        le=50,
        description="候補の多様化で、融合の上位に 1 件も無い文書から足す件数(#2228)。",
    )
    rag_candidate_diversity_present_document: int = Field(
        default=5,
        ge=0,
        le=50,
        description="候補の多様化で、融合の上位にある文書から続きを足す件数(#2228)。",
    )
    rag_candidate_diversity_max_extra: int = Field(
        default=100,
        ge=0,
        le=1000,
        description=(
            "候補の多様化で足す件数の合計の上限(#2228)。融合の順に足すので、上限で切るのは融合の"
            "点の低い候補。rerank の候補の数(時間)を抑える。"
        ),
    )
    rag_approved_faq_semantic_enabled: bool = Field(
        default=True,
        description=(
            "Approved FAQ(類似問)の照合に embedding の意味類似度を加える(rag_poc と同じ既定 ON)。"
        ),
    )
    rag_approved_faq_suggestion_min_score: float = Field(
        default=0.75,
        ge=0.0,
        le=1.0,
        description=(
            "チャットと RAG 検索で類似問を提示する一致度の下限(0〜1。#684 / #709)。"
            "低いと弱い候補が毎回出て、"
            "利用者に毎回「どれでもない」を選ばせることになる。"
        ),
    )
    rag_approved_faq_suggestion_max_gap: float = Field(
        default=0.1,
        ge=0.0,
        le=1.0,
        description=(
            "チャットと RAG 検索で類似問を提示するとき、1 位の一致度からこの差より離れた候補を"
            "出さない(#709)。"
            "1 位とほぼ同じくらい近い候補だけを並べ、遠い 2・3 位で迷わせない。"
        ),
    )
    # 回答は rag_poc の根拠付き回答(質問ルーティング / CRAG / 生成 + 監査ラウンド)だけ
    # にした(#594)。回答エンジンの選択(旧 RAG_ANSWER_ENGINE)は読まない。
    rag_history_rewrite_enabled: bool = Field(
        default=True,
        description=(
            "チャットで回答するとき、会話履歴から最新の質問を"
            "単独の質問へ書き換える(履歴がある場合だけ LLM 呼び出しが 1 回増える)。"
        ),
    )
    rag_search_top_k_default: int = Field(
        default=SEARCH_TOP_K_DEFAULT,
        ge=1,
        le=SEARCH_TOP_K_MAX,
        description="検索・チャット・MCP で top_k を省略したときの基準件数。",
    )
    rag_query_strategy: QueryStrategy = Field(
        default="simple_retrieval",
        description="質問の拡張(query rewriting / expa"
        "nsion)の方式。全体設定だけで決め、検索・回答プロファイルでは上書きしない。",
    )
    rag_answer_flow: AnswerFlow = Field(
        default="standard_rag",
        description=(
            "回答の生成方式。standard_rag(標準 RAG。既定)は補正しない。"
            "crag(CRAG)は検索結果を評価して必要なら補正検索する"
            "(AI の呼び出しと時間が増えるので明示して選ぶ。#3509)。"
        ),
    )
    rag_neighbor_child_count: int = Field(
        default=3,
        ge=0,
        le=20,
        description=(
            "回答で、根拠の child の前後から context へ足す近傍 child 数。"
            "全体設定だけで決め、検索・回答プロファイルでは上書きしない。"
        ),
    )
    rag_rerank_enabled: bool = Field(
        default=True,
        description=(
            "回答で、検索候補を OCI Generative AI の rerank で並べ替える。"
            "全体設定だけで決め、検索・回答プロファイルでは上書きしない。"
        ),
    )
    rag_screen_linking_enabled: bool = Field(
        default=False,
        description=(
            "回答で、検索範囲の画面目録(文書ごとの番号付きの見出し)から質問を解決する"
            "操作画面を LLM で選び、その画面の根拠を検索候補に加える"
            "(LLM の呼び出しが 1 回増える。#554)。"
            "全体設定だけで決め、検索・回答プロファイルでは上書きしない。"
        ),
    )
    rag_article_catalog_enabled: bool = Field(
        default=True,
        description=(
            "回答で、検索の候補に出た条で構成された文書(XML ツリー JSON で取り込んだ文書など)の"
            "条の目録から、質問の根拠になる条を LLM で選び、その条の根拠を候補に加えて context の"
            "枠を確保する(#2254)。条で構成された文書が候補に無ければ LLM を呼ばない"
            "(候補にあれば文書ごとに 1 回、並行に呼ぶ)。"
        ),
    )
    rag_article_catalog_max_documents: int = Field(
        default=16,
        ge=0,
        le=32,
        description=(
            "条の目録を読む文書の上限(検索の候補の文書の、候補の順位の逆数の合計の大きい順。"
            "#2254・#2261)。0 は選ばない。既定は評価で必要だった文書の順位(最大 8)に"
            "余裕を持たせた値(#3518)。文書ごとに LLM を 2 回呼ぶ。"
        ),
    )
    rag_article_catalog_max_articles: int = Field(
        default=8,
        ge=0,
        le=20,
        description=(
            "条の目録から 1 つの文書で選ぶ条の上限(#2254)。既定は評価で根拠の条が選ばれた"
            "位置(最大 6)に余裕を持たせた値(#3518)。"
        ),
    )
    rag_article_catalog_context_records: int = Field(
        default=6,
        ge=0,
        le=10,
        description=(
            "条の目録で選んだ条のうち、context に足す数(rerank の順。#2254)。検索の根拠の"
            "起点・親の枠の外に足し、検索の根拠を減らさない。0 は足さない(候補に加えるだけ)。"
        ),
    )
    rag_article_catalog_scope_documents: int = Field(
        default=2,
        ge=0,
        le=5,
        description=(
            "検索の範囲の条で構成された文書の一覧から、LLM が選んで目録を読む文書の数"
            "(検索の候補に出ない文書。手続の一般的な規則の文書など。#2254)。0 は候補の文書だけ。"
        ),
    )
    rag_article_catalog_original_question_search: bool = Field(
        default=True,
        description=(
            "元の質問(業務 Agent が注入する original_question)でも 1 回ベクトル検索し、"
            "その候補を条の目録を読む文書の順位に足す(候補・context には足さない)。"
            "短い検索文で事例の論点の文書が候補に出ないことへの対策。"
        ),
    )
    rag_mcp_catalog_article_evidence_limit: int = Field(
        default=120,
        ge=0,
        le=200,
        description=(
            "MCP の rag_retrieve_evidence が evidence_limit の外で末尾に足す、"
            "条の目録で選んだ条の根拠の数の上限(選んだ順。各文書の条を順に回した順)。"
            "業務 Agent のモデルの入力を抑える。0 は足さない。"
        ),
    )
    rag_article_catalog_refine_articles: int = Field(
        default=6,
        ge=0,
        le=20,
        description=(
            "条の選択の 2 段目で、1 段目で選んだ条と同じ章の条と、選んだ条を書き出しで"
            "名指しする別の章の条を本文の書き出しつきで見せ、文書ごとに足させる条の数の上限。"
            "0 は 2 段目を行わない。"
        ),
    )
    rag_article_catalog_review_enabled: bool = Field(
        default=False,
        description=(
            "条の目録で選んだ条を回答の生成が引かなかったとき、条ごとに原文が支える説明を"
            "別の LLM の呼び出しで検討し、その説明の出典に足す(#3526)。"
            "足した出典も原文照合と条の出典の監査に掛ける。評価で確かめた説明に根拠の無い条の"
            "出典が付いたので既定は無効。無効なら検討の呼び出しをしない。"
        ),
    )
    rag_article_catalog_refine_candidates: int = Field(
        default=60,
        ge=0,
        le=200,
        description=(
            "条の選択の 2 段目で見せる条の数の上限(書き出しつき。同じ章の条と、選んだ条を名指しする"
            "別の章の条の合計。同じ章の条に 3 分の 2、名指しの条に 3 分の 1 を割り当てる。"
            "名指しの条は 3 分の 1 を超えず、名指しの条の余りは同じ章の条が使う)。"
            "0 は 2 段目を行わない。"
        ),
    )
    rag_article_catalog_output_token_cap: int = Field(
        default=8192,
        ge=0,
        le=32768,
        description=(
            "条の選択の LLM の呼び出し 1 回の出力の上限(token)。"
            "選ぶ出力は目録の番号の列で通常は数百 token。"
            "同じ出力を繰り返して書き続ける呼び出しを抑え、"
            "超えた文書は選ばずに続ける。0 は抑えない。"
        ),
    )
    rag_article_catalog_call_timeout_seconds: float = Field(
        default=180.0,
        ge=0.0,
        le=600.0,
        description=(
            "条の選択の LLM の呼び出し 1 回の時間の上限(秒)。並行の呼び出しをすべて待つので、"
            "1 つの遅い呼び出しで段全体が止まらないようにする。"
            "超えた文書は選ばずに続ける。0 は抑えない。"
        ),
    )
    rag_article_catalog_cache_dir: str = Field(
        default="",
        max_length=1024,
        description=(
            "条の選択と範囲の文書の選択の結果を、process をまたいで使う記録の置き場所(#3507)。"
            "空は PLATFORM_LOCAL_STORAGE_DIR の下の cache/rag-article-catalog。"
            "同じ Compute の backend の worker が同じ場所を読み書きする。"
        ),
    )
    rag_article_catalog_cache_ttl_seconds: int = Field(
        default=86400,
        ge=0,
        le=30 * 86400,
        description=(
            "条の選択の記録の有効期間(秒)。key は索引の版(chunk_set)・目録の内容・質問・"
            "モデル・選択の設定で、同じ入力の LLM の結果だけを使い回す。"
            "0 は process をまたぐ記録を使わない(process の中の記録だけ)。"
        ),
    )
    rag_article_catalog_cache_max_entries: int = Field(
        default=4096,
        ge=0,
        le=100_000,
        description=(
            "条の選択の記録の件数の上限(1 件は文書 1 つの選択か範囲の文書の選択 1 回)。"
            "超えたら古い記録から消す。0 は process をまたぐ記録を使わない。"
        ),
    )
    rag_answer_validation_max_input_bytes: int = Field(
        default=96_000,
        ge=16_000,
        le=1_000_000,
        description=(
            "回答の最終の検証(rag_validate_answer)でモデルに渡す入力の上限"
            "(UTF-8 の bytes。指示・質問・回答・根拠を含む)。根拠は渡された順に収まるだけ使い、"
            "収まらない根拠は検証に使わない(evidence_truncated)。"
            "検証のモデルの入力の上限より小さくする。"
        ),
    )
    rag_answer_validation_timeout_seconds: float = Field(
        default=540.0,
        gt=0.0,
        le=OCI_ENTERPRISE_AI_TIMEOUT_MAX_SECONDS,
        description=(
            "回答の最終の検証(rag_validate_answer)の通しの上限(秒。#2700)。"
            "根拠の読み直しから監査の LLM の呼び出しまでを含み、LLM 1 回の timeout と"
            "再試行をこの残り時間に縮める。上限までに監査が返らなかった段落は、"
            "確かめ終わらなかった段落(unassessed)として返す(status=partial、"
            "どの段落も返らなければ timed_out)。呼び出し側(Agent の接続 rag の待ち時間 "
            "AGENT_EXTERNAL_RAG_TIMEOUT_SECONDS、既定 630 秒)より短くする。"
        ),
    )
    rag_answer_validation_batch_passages: int = Field(
        default=5,
        ge=1,
        le=128,
        description=(
            "回答の最終の検証で、監査の LLM の 1 回の呼び出しに渡す回答の段落の数"
            "(#2700)。段落がこれより多い回答は組に分けて監査する(指示・質問・回答の"
            "全文・根拠はどの組も同じ)。1 回の出力が減り、組ごとに並行に呼べる。"
        ),
    )
    rag_answer_validation_concurrency: int = Field(
        default=4,
        ge=1,
        le=16,
        description=(
            "回答の最終の検証の 1 回で、同時に監査する段落の組の数(#2700)。"
            "組がこれより多ければ、空いた分から順に監査する。"
        ),
    )
    rag_request_coverage_retrieval_enabled: bool = Field(
        default=False,
        description=(
            "回答の生成の前に、質問の要求ごとに根拠があるかを語の一致で確かめ(LLM は呼ばない)、"
            "根拠の無い要求だけを要求の文で 1 回ずつ検索し直して回答の材料に足す(#1279)。"
            "全体設定だけで決め、検索・回答プロファイルでは上書きしない。"
        ),
    )
    rag_request_coverage_max_queries: int = Field(
        default=2,
        ge=0,
        le=5,
        description="根拠の無い要求の再検索の回数の上限(1 要求 1 回。#1279)。",
    )
    rag_request_coverage_max_chunks: int = Field(
        default=4,
        ge=0,
        le=12,
        description="根拠の無い要求の再検索で回答の材料に足す根拠(親 chunk)の数の上限(#1279)。",
    )
    rag_request_coverage_deadline_seconds: float = Field(
        default=10.0,
        ge=0,
        le=60,
        description=("根拠の無い要求の再検索の期限(秒)。過ぎたら次の要求の検索を始めない(#1279)。"),
    )
    rag_request_coverage_max_subquestions: int = Field(
        default=5,
        ge=0,
        le=10,
        description=(
            "丸数字・(1) などで並べた小問のうち、根拠が無いか一部だけの小問ごとに"
            "検索する小問の数の上限(1 小問 1 回。0 で小問ごとの検索をしない。#2399)。"
            "期限は再検索と共通。"
        ),
    )
    rag_request_coverage_chunks_per_subquestion: int = Field(
        default=2,
        ge=0,
        le=6,
        description="小問ごとの検索で、小問ごとに回答の材料に確保する根拠(親 chunk)の数(#2399)。",
    )
    rag_extra_retrieval_max_queries: int = Field(
        default=8,
        ge=0,
        le=20,
        description=(
            "1 回の回答で、主の検索の後に足す検索(業務ガイドの手順ごとの検索・小問ごとの検索・"
            "根拠の無い要求の再検索)の回数の合計の上限(#2395)。種類ごとの上限はこの枠の中で数え、"
            "必須のガイド参照の取得分を先に確保し、その他の追加検索は残りから取る。"
        ),
    )
    rag_guide_step_retrieval_enabled: bool = Field(
        default=True,
        description=(
            "照合した業務ガイドの手順(分岐で残った手順)ごとに「質問 + 手順の検索の手がかり」で"
            "検索し、手順が参照する資料の頁の根拠を回答の材料に入れる(#2395)。LLM は呼ばない"
            "(検索が手順ごとに増える)。全体設定だけで決め、検索・回答プロファイルでは上書きしない。"
        ),
    )
    rag_guide_step_max_queries: int = Field(
        default=4,
        ge=0,
        le=10,
        description=(
            "必須のガイド参照を取得した後に行う補助検索・任意手順の検索の回数の上限。"
            "0 でも必須参照の取得は行う(#3795)。"
            "追加の検索の枠(RAG_EXTRA_RETRIEVAL_MAX_QUERIES)の残りも超えない。"
            "全体設定だけで決め、検索・回答プロファイルでは上書きしない。"
        ),
    )
    rag_guide_step_chunks_per_step: int = Field(
        default=2,
        ge=1,
        le=5,
        description="業務ガイドの手順ごとの検索で、1 手順に確保する根拠(親 chunk)の数(#2395)。",
    )
    rag_guide_reference_max_chunks: int = Field(
        default=4,
        ge=0,
        le=12,
        description=(
            "業務ガイドの手順が参照する資料の頁から、検索の順位に関係なく回答の材料に入れる根拠"
            "(親 chunk)の追加数の上限。必須の取得対象ごとの最低1件は別に確保し、"
            "全体の本文予算は増やさない(#3795)。"
        ),
    )
    rag_guide_step_deadline_seconds: float = Field(
        default=60.0,
        ge=0,
        le=60,
        description=(
            "業務ガイドの手順ごとの検索の期限(秒。既定 60。#2826)。"
            "過ぎたら次の検索を始めない(#2395)。"
            "全体設定だけで決め、検索・回答プロファイルでは上書きしない。"
        ),
    )
    rag_strategy_reasoning_effort: ReasoningEffortOverride = Field(
        default="model",
        description=(
            "質問拡張戦略の判定と検索文の生成の LLM の呼び出しの推論の量(#1499。既定 model = "
            "モデルの登録値。low では戦略の選択がぶれ、検索文 0 本の単純検索に倒れることがあった)。"
        ),
    )
    rag_judge_reasoning_effort: ReasoningEffortOverride = Field(
        default="medium",
        description=(
            "根拠の評価(CRAG の評価と章節の拡張の十分性の判定)の LLM の呼び出しの推論の量"
            "(#1499。既定 medium。#1550)。model はモデルの登録値のまま。モデルの登録値が none の"
            "モデルには送らない。"
        ),
    )
    rag_generation_reasoning_effort: ReasoningEffortOverride = Field(
        default="model",
        description=(
            "回答文の生成の LLM の呼び出しの推論の量(#1499。既定 model = モデルの登録値)。"
            "監査は RAG_AUDIT_REASONING_EFFORT(#1528)。"
        ),
    )
    rag_audit_reasoning_effort: ReasoningEffortOverride = Field(
        default="model",
        description=(
            "生成後の監査(主張と引用の適用性の確認)の LLM の呼び出しの推論の量(#1528。"
            "既定 model = モデルの登録値)。"
        ),
    )
    rag_crag_skip_on_strong_signals: bool = Field(
        default=False,
        description=(
            "規則の信号が強いとき CRAG の根拠の評価(1 回目の LLM の呼び出し)を飛ばし、"
            "曖昧な帯だけで呼ぶ(#1499・#1528。既定 OFF)。rerank の最上位の score が閾値以上で、"
            "質問の要求に語の一致で根拠の無いもの(missing)が無く、定義を問う語の定義本文が"
            "あるときだけ飛ばす。"
            "エラー文・列挙・網羅・件数を問う質問(#1503)と、候補の除外が要る根拠(旧版の文書・"
            "別の文書と同じ本文)があるときは必ず呼ぶ。生成後の監査は飛ばさない。"
        ),
    )
    rag_crag_compact_input: bool = Field(
        default=True,
        description=(
            "CRAG の根拠の評価の入力を詰める(#1528。既定 ON。A/B で入力 tokens が"
            " 1/4〜1/9)。候補を生成に渡す親の数にそろえ、JSON を"
            "空白なしで書き、候補の ID を chunk_uid だけにする。章節の拡張の判定は候補の数を"
            "そろえない(広げた章節を見せる)。"
        ),
    )
    rag_crag_short_candidate_ids: bool = Field(
        default=True,
        description=(
            "(既定 ON。#1864)CRAG の根拠の評価の詰めた入力(rag_crag_compact_input)で、候補の"
            " chunk_uid を短い ID(C1・C1-1)にする。評価器は候補ごとの判定・観点の根拠・回復の起点に"
            "ID を書き写すので、長い chunk_uid(文書 ID の 32 桁と chunk の名前)が出力の token と"
            "時間を増やしていた。返った ID は chunk_uid に戻すので、判定の働きは変わらない。"
        ),
    )
    rag_answer_audit_compact_reasons: bool = Field(
        default=False,
        description=(
            "(既定 OFF。#1912)生成後の監査の reason を、判定が通った説明"
            "(supported かつ matched)と答えている要求(addressed)では空文字にさせる。"
            "監査の見える出力は約 5 割減るが、同じ入力の対の比較で監査が甘くなった"
            "(通らなかった判定が 5 件 → 1 件)ため既定では使わない。品質評価の比較用。"
        ),
    )
    rag_answer_max_correction_rounds: int = Field(
        default=2,
        ge=0,
        le=2,
        description=(
            "回答文の生成と根拠確認の是正の組(作り直し)の回数の上限(#1912。0〜2。"
            "既定 2 は従来と同じ)。是正 1 組は生成と監査の LLM の呼び出しを 1 回ずつ足す"
            "(#1864 の記録で 1 組 5〜63 秒)。"
            "0 は作り直さず、1 組目の検査と監査の結果で確定する。"
        ),
    )
    rag_answer_evidence_refinement_mode: Literal["legacy", "cited_only", "cited_required"] = Field(
        default="legacy",
        description=(
            "生成後の根拠の選別。legacy は既存方式を維持し、cited_only は引用済みの原文だけ、"
            "cited_required は引用済みの原文と必須の補完根拠へ絞る。明示的に選んだ場合だけ、"
            "既存の是正回数・時間予算内で最大 1 回再生成する。監査・補検索は自動で有効にしない。"
        ),
    )
    rag_answer_audit_mode: Literal["none", "standard", "detailed"] = Field(
        default="none",
        description=(
            "(#2795・#2800)回答の監査の型。none(既定)は生成の後の LLM の監査と作り直しをせず、"
            "原文との照合と規則の検査(決定的)だけで印を付ける(WeKnora と同じく生成の後に確認の"
            " LLM を呼ばない)。要求の充足は説明の有無、業務ガイドの手順の充足は決定的な対応付けで"
            "決める。standard は 1 回の監査(主張・要求・要件)と事実の問題の作り直し 1 組まで。"
            "detailed は未使用の根拠も列挙させ、完全性の不足でも最大 3 組まで作り直す"
            "(業務ガイドの下書きの試答は常に detailed)。"
        ),
    )
    rag_llm_stall_detection_enabled: bool = Field(
        default=True,
        description=(
            "(#1926)回答(検索の回答・チャット)の LLM の呼び出しを streaming で受け、"
            "停滞した呼び出しを打ち切って同じ要求を送り直す。OCI Enterprise AI の呼び出しは"
            "まれ(記録の約 1%)に通常の 1/10 以下の速さ(13〜23 token/秒。中央値 224)に落ち、"
            "同じ token 数で 36 秒 → 183 秒かかった。"
            "判定の基準は同じ接続・モデルの直近の完了した呼び出し(3 回以上)から学ぶ。"
            "回答の期限の無い呼び出し(取込など)は従来どおり streaming しない。"
        ),
    )
    rag_llm_stall_idle_seconds: float = Field(
        default=20.0,
        ge=5.0,
        le=120.0,
        description=(
            "(#1926)応答の event(最初の event を含む)がこの秒だけ来なければ停滞として打ち切る。"
            "直近の呼び出しの最大の間隔の 3 倍が長ければそちら(推論を stream しないモデル)。"
            "記録では最初の event まで 0.1〜0.2 秒、event の間隔は 1 秒未満。"
        ),
    )
    rag_llm_stall_min_rate_ratio: float = Field(
        default=0.25,
        gt=0.0,
        le=0.9,
        description=(
            "(#1926)直近 10 秒の出力の速さ(delta の event/秒)が、直近の完了した呼び出しの"
            "速さの中央値のこの割合を下回れば停滞として打ち切る。"
            "記録の 10 パーセンタイルは中央値の約 0.65、遅い呼び出しは約 0.06〜0.1。"
        ),
    )
    rag_llm_stall_resends: int = Field(
        default=1,
        ge=0,
        le=2,
        description=(
            "(#1926)停滞で打ち切って送り直す回数の上限(LLM の呼び出し 1 回あたり)。"
            "最後の送信は停滞を検出せずに回答の期限まで待つ。"
            "0 は打ち切らない(streaming の時刻の記録だけ)。"
        ),
    )
    rag_llm_runaway_detection_enabled: bool = Field(
        default=True,
        description=(
            "(#2966)回答の LLM の呼び出しを streaming で受けるとき(停滞の検出が ON)、"
            "出力が同じ並びの繰り返し・空白だけで続いたら打ち切り、"
            "推論の量を下げて 1 回だけ送り直す。出力が速く続くまま出力の上限"
            "(32,768 tokens)まで書き続け、200 秒かかって未完了になった記録がある。"
            "streaming しない呼び出しも、出力の上限で未完了の応答に同じ暴走があれば、"
            "上限を広げずに同じ送り直しにする(#3835)。"
            "正当な長い出力(表・一覧)は行ごとに値が変わるので打ち切らない。"
        ),
    )
    rag_section_expansion_followup_reserve_percent: int = Field(
        default=20,
        ge=0,
        le=50,
        description=(
            "業務ガイドの手順ごとの検索が後に来る回答で、最初の章節の拡張が使わずに残す入力の割合"
            "(#2816。章節の拡張の根拠の入力の上限に対する %。既定 20、0 は残さない)。"
            "手順の根拠から広げる章節の分を残す。"
        ),
    )
    rag_section_skip_regrade_at_scope: bool = Field(
        default=True,
        description=(
            "(既定 ON。#1528 の A/B で列挙の問が 484 → 293 秒、品質は同じ)"
            "読む範囲が文書全体の列挙の問(#1503)で、章節の拡張が文書全体まで読んだら、"
            "CRAG の評価し直しを省いて生成へ進む(#1528)。"
        ),
    )
    rag_crag_skip_min_rerank_score: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="CRAG の評価を飛ばす rerank の最上位の score の閾値(#1499。0〜1)。",
    )
    rag_query_expansion_max_queries: int = Field(
        default=5,
        ge=1,
        le=5,
        description=(
            "質問拡張で LLM が作る検索文の本数の上限(#1499。既定は従来と同じ 5)。"
            "原質問・用語の別名・"
            "語彙の補助の検索文は数えない。"
        ),
    )
    rag_auto_field_filter_enabled: bool = Field(
        default=False,
        description=(
            "質問に書かれた条件(「2025 年以降」「10 万円以"
            "上」など)を、検索・回答プロファイルの KB で定義した"
            "抽出項目の条件として LLM で読み取り、検索を絞り込む"
            "(Metadata Filter Extraction。LLM の呼び出しが 1 回増える。"
            "読み取った条件で 0 件なら条件を外して検索し直す。#652)。"
            "全体設定だけで決め、検索・回答プロファイルでは上書きしない。"
        ),
    )
    rag_filter_relaxation_max_steps: int = Field(
        default=3,
        ge=0,
        le=3,
        description=(
            "回答の検索で、自動で足した検索条件(質問から読み取った抽出項目の条件・質問が名指しした"
            "文書のページ・文書名)の候補が 0 件のとき、その条件を外して検索し直す段の数の上限"
            "(#652・#2401)。外す順は 抽出項目の条件 → ページ → 文書名 で、段ごとに 1 回だけ"
            "検索し直す。利用者が画面・検索・回答プロファイルで指定した条件は外さない。"
            "0 で検索し直さない。"
        ),
    )
    rag_answer_record_retention_days: int = Field(
        default=0,
        ge=0,
        le=3650,
        description=(
            "回答の記録と実行履歴の保持日数。既定の 0 は無期限で、自動では削除しない(#3235)。"
            "正の値にすると、回答保存時と設定変更時に期限切れを削除する。"
        ),
    )
    rag_support_guide_max_published_revisions: int = Field(
        default=5,
        ge=1,
        le=100,
        description=(
            "業務ガイドの公開の履歴に残す版の数（#2356）。公開・前の版に戻すときに、公開日時の"
            "新しい順に超えた版を消す（公開中の版は残す。版の番号は振り直さない）。"
        ),
    )
    rag_support_guide_enabled: bool = Field(
        default=False,
        description="業務ガイドを質問への回答に使う。既定は OFF。検索・回答プロファイルで選ぶ。",
    )
    rag_support_guide_semantic_match_enabled: bool = Field(
        default=False,
        description=(
            "業務ガイドの照合で、質問と質問の例（intent_examples）の埋め込み（OCI Generative AI）"
            "の近さを点に足す（#2398）。質問ごとに埋め込みの呼び出しが 1 回増える。質問の例の"
            "埋め込みは公開のときに作って持つ。検索・回答プロファイルで上書きできる。"
        ),
    )
    rag_support_guide_semantic_threshold: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description=(
            "意味の照合で、質問と質問の例の cosine の近さがこの値以上なら点を足す（#2398）。"
        ),
    )
    rag_support_guide_close_match_clarification_enabled: bool = Field(
        default=False,
        description=(
            "業務ガイドの照合で上位の 2 つの点が近いとき、どのガイドに沿うかを確認の問い（選択肢は"
            "ガイドの題名）で確かめる（#2398）。検索・回答プロファイルで上書きできる。"
        ),
    )
    rag_support_guide_close_match_margin: int = Field(
        default=1,
        ge=0,
        le=10,
        description="上位の 2 つの業務ガイドの点の差がこの値以下なら「近い」とみなす（#2398）。",
    )

    rag_runtime_knowledge: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "リクエスト単位で検索・回答プロファイルから解決する用語・同義語と回答ルール"
            "(rag_poc runtime knowledge payload)。"
            "回答フローだけが使う。"
        ),
    )
    rag_support_guide: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "リクエスト単位で検索・回答プロファイルの公開した業務ガイドから選んだ 1 つの要約"
            "（guide_id・版・判断・既知 / 不明の条件・確認の質問。#1238）。回答フローだけが使う。"
        ),
    )
    rag_guide_preview: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "リクエスト単位で、業務ガイドの下書きで試す回答の印（guide_id・下書きの版。#1288）。"
            "回答の記録に残し、利用者の回答の履歴・質問の履歴に入れない。"
        ),
    )
    rag_answer_contact_override: str = Field(
        default="",
        description=(
            "リクエスト単位で検索・回答プロファイルから解決する回答の注意書きの窓口の上書き（#2400）。"
            "空ならシステム設定の窓口。"
        ),
    )
    rag_language_pack_overrides: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        description=(
            "リクエスト単位で検索・回答プロファイルから解決する言語資源の上書き（検索・回答の項目だけ。"
            "#2181）。回答の要求の間だけ全体の言語資源に重ねて束ねる。"
        ),
    )
    rag_search_answer_profile_revision: dict[str, object] = Field(
        default_factory=dict,
        description=(
            "リクエスト単位で解決した検索・回答プロファイルの版（id・更新時刻・設定の sha256。"
            "#1276）。回答の記録の provenance に残すだけで、検索・回答の挙動は変えない。"
        ),
    )
    rag_domain_keywords: list[str] = Field(
        default_factory=list,
        description=(
            "リクエスト単位で検索・回答プロファイルから解決するドメインキーワード。"
            "全文検索で分割せず 1 語として優先する。"
        ),
    )
    rag_embedding_cache_enabled: bool = Field(
        default=True,
        description=(
            "同一 process 内で OCI Generative AI embedding 結果を LRU cache する。"
            "cache key は本文 hash と model/input_type/dimension だけで構成する。"
        ),
    )
    rag_embedding_cache_max_entries: int = Field(default=4096, ge=0, le=200000)
    rag_embedding_batch_size: int = Field(
        default=96,
        ge=1,
        le=96,
        description=(
            "OCI Generative AI embedding へ 1 回に送る text 数(最大 96)。"
            "大きな文書取込や query expansion で API payload を過大化しない。"
        ),
    )
    rag_rerank_cache_enabled: bool = Field(
        default=True,
        description=(
            "同一 process 内で OCI Generative AI rerank 結果を LRU cache する。"
            "cache key は query/document hash と model/top_n だけで構成する。"
        ),
    )
    rag_rerank_cache_max_entries: int = Field(default=1024, ge=0, le=100000)
    rag_evidence_budget_chars: int = Field(
        default=80000,
        ge=1000,
        le=500000,
        description=(
            "回答の生成に渡す根拠の原文の文字予算（文字。#3776）。"
            "既定 80000 は rag_engine の既定と同じ。"
            "根拠の本文・前置き・出典情報・描画の書式をこの予算内に収める。"
            "全体設定だけで決め、検索・回答プロファイル・KB・文書レシピでは上書きしない。"
            "章節の拡張（rag_section_expansion_input_token_percent）は読む章節を"
            "入力 token の上限で別に数え、生成時はこの文字上限とモデルの入力予算の両方に収める。"
            "使った予算と送った根拠の文字数は回答の診断の evidence_budget に残る。"
        ),
    )
    rag_answer_timeout_seconds: float = Field(
        default=600.0,
        gt=0.0,
        le=OCI_ENTERPRISE_AI_TIMEOUT_MAX_SECONDS,
        description=(
            "LLM を呼ぶ回答生成（チャット・RAG 検索の回答。ストリーム・非ストリーム・MCP。"
            "品質評価の 1 ケース）の通しの上限（秒。#375 / #383）。検索の計画・追加の検索の再分解・"
            "回答の生成で LLM を複数回呼ぶ。上限は LLM 1 回の timeout の設定の上限と同じ。"
            "既定は上限と同じ 600 秒（推論の量の既定 medium で、推論型のモデルの 1 回の呼び出しが"
            "数十秒になるため。#1425）。画面（630 秒）と Nginx（660 秒）の待ち時間は"
            "これより長くする。"
        ),
    )
    db_read_timeout_seconds: float = Field(
        default=8.0,
        gt=0.0,
        le=60.0,
        description=(
            "閲覧系一覧/集計 API（ドキュメント・取込ジョブ・ナレッジベース）の DB 待機秒数。"
            "DB 停止時に 500 ではなく空データ + warning で縮退応答するための上限。"
        ),
    )
    rag_pdf_segmentation_enabled: bool = Field(
        default=True,
        description=(
            "PDF をページの段（rag_pdf_max_pages_per_segment ページ）に分け、段ごとに"
            "解析サービス（Docling など）を呼ぶ。段ごとに状態・進捗・抽出結果を残し、"
            "失敗・中断の後は成功した段を解析し直さない（#2001）。"
        ),
    )
    rag_pdf_max_pages_per_segment: int = Field(default=10, ge=1, le=50)
    rag_pdf_max_segments: int = Field(default=300, ge=1, le=2000)
    rag_pdf_segment_parse_concurrency: int = Field(
        default=1,
        ge=1,
        le=4,
        description=(
            "PDF の段を同時に解析する数（#2001）。既定は 1（順番）。解析サービスは段ごとにページを"
            "描画して解析するため、上げるときは解析サービスの CPU・メモリと worker の数を確かめる。"
        ),
    )
    # ページの境目で切れた段落の判定の幾何の条件（#2273）。PDF の文字の層の行の位置で見る（文字の
    # 層の無いページ・縦書き・回転したページは文の終わりの規則だけで判定する）。値は行の高さの倍数。
    rag_page_continuation_geometry_enabled: bool = Field(
        default=True,
        description=(
            "ページの境目の段落の続きの判定に、行の位置（前の段落の最後の行が右端まで届く・"
            "次の段落の最初の行に字下げが無い・段落の幅が近い）を使う（#2273）。"
        ),
    )
    rag_page_continuation_right_edge_tolerance: float = Field(
        default=1.0,
        ge=0.0,
        le=10.0,
        description=(
            "前の段落の最後の行の右端と段落の右端の差がこの値（行の高さの倍数）未満なら、"
            "最後の行が右端まで届いているとみなす。"
        ),
    )
    rag_page_continuation_indent_tolerance: float = Field(
        default=0.5,
        ge=0.0,
        le=10.0,
        description=(
            "次の段落の最初の行の左端と段落の左端の差がこの値（行の高さの倍数）未満なら、"
            "字下げが無いとみなす。"
        ),
    )
    rag_page_continuation_width_difference_ratio: float = Field(
        default=1.0,
        ge=0.0,
        le=10.0,
        description=("2 つの段落の幅の差が狭いほうの幅のこの倍数未満なら、幅が近いとみなす。"),
    )
    # 取込の AI の呼び出しの並行と再試行（#2002。共通の部品 pr_backend_core.bounded_calls）。
    rag_vision_concurrency: int = Field(
        default=4,
        ge=1,
        le=16,
        description=(
            "図・表の説明（Vision）の VLM を同時に呼ぶ数の上限（#2002）。429 を受けると自動で"
            "減らし（最小 1）、成功が続くとこの上限まで戻す。同じページの図は順番に読む。"
        ),
    )
    rag_navigation_summary_concurrency: int = Field(
        default=4,
        ge=1,
        le=16,
        description="章節の要約の LLM を同時に呼ぶ数の上限（#2002）。",
    )
    rag_embedding_concurrency: int = Field(
        default=2,
        ge=1,
        le=8,
        description=(
            "Embedding の要求（最大 96 件の batch）を同時に送る数の上限（#2002）。1 つの要求が"
            "大きいため、LLM・VLM より小さい既定にする。"
        ),
    )
    rag_evaluation_concurrency: int = Field(
        default=2,
        ge=1,
        le=8,
        description=(
            "品質評価で同時に実行するケースの数の上限（#2004）。1 ケースの往復と判定は順に行い、"
            "結果はケースの順のまま。回答・判定が 429 で失敗したら評価の全体で待って並行の数を"
            "下げ（最小 1）、送り直す。1 は 1 件ずつ（#2004 より前と同じ）。"
        ),
    )
    rag_support_guide_import_concurrency: int = Field(
        default=4,
        ge=1,
        le=8,
        description=(
            "回答の過程の表から業務ガイドの下書きを作るとき、同時に送る LLM の呼び出しの数の上限"
            "（#2397。1 行で 1 回）。"
        ),
    )
    rag_support_guide_import_max_rows: int = Field(
        default=30,
        ge=1,
        le=100,
        description=(
            "回答の過程の表から 1 回で下書きにする行の数の上限（#2397。1 行で LLM を 1 回呼ぶ）。"
        ),
    )
    rag_ai_call_backoff_max_seconds: float = Field(
        default=60.0,
        ge=1.0,
        le=600.0,
        description=(
            "AI の呼び出しの再試行の待ち（指数の backoff と full jitter）の上限（秒。#2002）。"
        ),
    )
    rag_ai_call_final_retry_delay_seconds: float = Field(
        default=30.0,
        ge=0.0,
        le=600.0,
        description=(
            "再試行を使い切った件を、段の最後にまとめてもう一度呼ぶ前の待ちの秒数（#2002）。"
        ),
    )
    rag_ai_call_fail_job_on_item_error: bool = Field(
        default=False,
        description=(
            "最後の再試行でも失敗した件があれば取込を失敗にする（#2002）。既定は偽で、"
            "その件を「説明できなかった」などとして記録し、取込を続ける。"
        ),
    )
    rag_guardrails_input_enabled: bool = Field(
        default=False, description="OCI Guardrails で質問を検査する。無効時は質問を送信しない。"
    )
    rag_guardrails_output_enabled: bool = Field(
        default=False, description="OCI Guardrails で回答を検査する。無効時は回答を送信しない。"
    )
    oci_guardrails_compartment_id: str = Field(
        default="",
        description="OCI Guardrails の compartment OCID。空欄時は oci_compartment_id を使う。",
    )
    oci_guardrails_endpoint: str = Field(
        default="",
        description="OCI Generative AI Inference のサービスエンドポイント上書き(空欄は SDK 既定)。",
    )
    oci_guardrails_prompt_injection_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="prompt injection の risk score をブロック扱いにする閾値(0.0–1.0)。",
    )
    oci_guardrails_timeout_seconds: float = Field(
        default=5.0,
        gt=0.0,
        le=30.0,
        description="OCI Guardrails 検査の接続・読取 timeout 秒。",
    )
    rag_evaluation_job_timeout_seconds: int = Field(
        default=3600,
        ge=60,
        le=RAG_EVALUATION_JOB_TIMEOUT_MAX_SECONDS,
        description=(
            "品質評価の job（golden set の評価。#390）の全体の時間の上限の最小値（秒）。"
            "job の上限は、投入時の回答の回数（ケース × 往復）× "
            "rag_answer_timeout_seconds とこの値の大きいほうで、86400 秒を超えない（#1430）。"
            "上限に達したら実行中のケースを打ち切り、残りのケースは実行せずに失敗として記録して結果を返す。"
            "1 回の回答の上限は rag_answer_timeout_seconds。"
        ),
    )
    rag_evaluation_suite: EvaluationSuite = Field(
        default="standard",
        description=(
            "品質評価の基準(閾値のプリセット。#591)。standard(標準。既定)/ strict(厳格)。"
            "request の thresholds が最優先。閾値は、そのケースの集合で測れた指標だけに適用する。"
        ),
    )
    rag_graph_profile: GraphProfile = Field(
        default="off",
        description=(
            "文書構造索引の構築。off(既定)は構築しない、entities は文書と章・節の見出しのつながりを"
            "構築する(LLM は使わない)。ナレッジベースの文書構造グラフで見られ、"
            "rag_graph_expansion_enabled を ON にすると回答の検索の候補を広げるのにも使う(#2055)。"
        ),
    )
    rag_vector_index_profile: VectorIndexProfile = Field(
        default="accurate",
        description=(
            "索引/検索精度の Vector Index アダプター。balanced は"
            "RAG_ORACLE_VECTOR_TARGET_ACCURACY をそのまま使い、accurate(既定。#272)は高再現(98)、"
            "fast は低レイテンシ(85)へ検索時 target accuracy を上書きする。"
            "推奨 HNSW ビルドパラメータは設定画面に表示し、適用には索引再作成が必要。"
        ),
    )
    # --- 前処理(Preprocess)ステージ(parse の前の原本変換)---
    rag_preprocess_profile: PreprocessProfile = Field(
        default="passthrough",
        description=(
            "parse の前に原本を一度だけ canonical な中間物へ変換する前処理プリセット。"
            "passthrough(既定)は変換せず現行挙動と一致。office_to_pdf は Word・PowerPoint→PDF"
            "(.doc/.docm/.ppt/.pptm を含む。Excel は対象外)、"
            "csv_to_jsonl は CSV→構造化 JSON、"
            "excel_to_jsonl は Excel(.xls/.xlsx/.xlsm)→構造化 JSON、"
            "word_to_jsonl は Word→構造化 JSON"
            "(いずれも各々独立した前処理マイクロサービス)。"
        ),
    )
    rag_preprocess_enabled: bool = Field(
        default=False,
        description=(
            "前処理マイクロサービスへの HTTP 委譲を有効化する。OFF(既定)は in-process で "
            "扱える profile(passthrough)のみ実行し、サービス必須の変換は "
            "passthrough へ安全に縮退する。"
        ),
    )
    rag_preprocess_office_to_pdf_service_url: str = Field(
        default="http://127.0.0.1:18010",
        description="Office→PDF(Word・PowerPoint)前処理マイクロサービスの base URL。",
    )
    rag_preprocess_csv_to_jsonl_service_url: str = Field(
        default="http://127.0.0.1:18012",
        description="CSV→構造化 JSON 前処理マイクロサービスの base URL。",
    )
    rag_preprocess_excel_options: ExcelOptions = Field(
        default_factory=ExcelOptions,
        description=(
            "前処理 excel_to_jsonl の選択肢(#1221。読み方 auto/table/procedure・表頭の行と行数・"
            "読むシート・読まないシート・非表示のシート・読まない列・列の役割の判定と指定(#1281))。"
            "文書レシピの excel_options で"
            '上書きできる。環境変数は JSON(例: {"mode": "table", "header_row": 2})。'
        ),
    )
    rag_preprocess_excel_to_jsonl_service_url: str = Field(
        default="http://127.0.0.1:18013",
        description="Excel(.xls/.xlsx)→行の記録 JSON 前処理マイクロサービスの base URL。",
    )
    rag_preprocess_word_to_jsonl_service_url: str = Field(
        default="http://127.0.0.1:18017",
        description=(
            "Word(.doc/.docx/.docm)→構造化レコードの JSONL の前処理サービスの base URL(#2457)。"
        ),
    )
    rag_preprocess_service_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        description=(
            "前処理マイクロサービス呼び出しの HTTP timeout(秒)。"
            "超過・接続失敗時は warning を付けて passthrough(原本そのまま parse)へ縮退する。"
        ),
    )
    rag_canonical_artifact_prefix: str = Field(
        default="artifacts/canonical",
        max_length=256,
        description="前処理で生成した正規化原本(canonical source)の Object Storage key prefix。",
    )
    rag_parser_adapter_backend: ParserAdapterBackend = Field(
        default=DEFAULT_PARSER_ADAPTER_BACKEND,
        description=(
            "文書解析 backend の明示選択。既定は Docling(#286)。Unstructured は明示選択で使う。"
            "Docling/Unstructured は対応 parser マイクロサービスへ HTTP 委譲する"
            "(前処理の構造化レコードの JSONL も同じサービスが読む。#21)。"
            "in-process 解析・local fallback は持たない。"
        ),
    )
    rag_parser_docling_enabled: bool = Field(
        default=True,
        description=(
            "Docling adapter を有効化する。既定 backend のため既定で True。"
            "取込時は parser-docling マイクロサービスの常時起動が前提。"
        ),
    )
    rag_vision_enabled: bool = Field(
        default=True,
        description=(
            "文書解析の後に図・画像を OCI Enterprise AI の VLM で読み取り、図の要素の本文を"
            "説明文にする(全ての解析エンジン。Docling は解析サービスの中で読み取る)。"
            "全体の既定は backend/.env で決め(文書解析の画面の「解析後の処理」から"
            "保存できる。#528)、文書のレシピで上書きする。既定 ON(#537)。"
            "画像 1 枚ごとに VLM を呼ぶ。"
        ),
    )
    rag_parser_unstructured_enabled: bool = Field(
        default=False,
        description=(
            "Unstructured adapter を有効化する。既定 backend ではないため既定で False"
            "(画面で Unstructured を選ぶと有効になる)。選択時は parser-unstructured の起動が前提。"
        ),
    )
    rag_parser_docling_service_url: str = Field(
        default="http://127.0.0.1:18020",
        description="Docling parser マイクロサービスの base URL。",
    )
    rag_parser_unstructured_service_url: str = Field(
        default="http://127.0.0.1:18022",
        description="Unstructured parser マイクロサービスの base URL。",
    )
    rag_parser_service_timeout_seconds: float = Field(
        default=300.0,
        gt=0,
        description=(
            "parser マイクロサービス呼び出しの HTTP timeout(秒)。"
            "超過・接続失敗時は warning を付けて local/Enterprise AI fallback へ縮退する。"
        ),
    )
    rag_http_service_retry_attempts: int = Field(
        default=5,
        ge=1,
        le=10,
        description="HTTP マイクロサービス呼び出しの最大試行回数。",
    )
    rag_http_service_retry_initial_delay_seconds: float = Field(
        default=0.5,
        ge=0.0,
        le=60.0,
        description="HTTP マイクロサービス retry の初期待機秒数。",
    )
    rag_http_service_retry_max_delay_seconds: float = Field(
        default=4.0,
        ge=0.0,
        le=300.0,
        description="HTTP マイクロサービス retry の最大待機秒数。",
    )
    rag_raptor_enabled: bool = Field(
        default=False,
        description=(
            "RAPTOR 再帰要約索引: chunking 後に leaf chunk を再帰 cluster + OCI Enterprise AI で"
            "要約し、多層级 summary node を leaf と一緒に索引する。OFF(既定)は leaf のみ。"
            "追加 LLM 呼び出しを伴う opt-in。再試行のあとも要約できない cluster は飛ばす(leaf は"
            "残る)。権限・モデルの設定の誤り・割り当ての使い切りは取込を止める(#2067)。"
        ),
    )
    rag_raptor_cluster_size: int = Field(
        default=5,
        ge=2,
        le=50,
        description="RAPTOR の 1 cluster あたり chunk 数(要約単位)。",
    )
    rag_raptor_max_levels: int = Field(
        default=2,
        ge=1,
        le=5,
        description="RAPTOR の階層の要約 tree の最大階層数。",
    )
    rag_raptor_concurrency: int = Field(
        default=4,
        ge=1,
        le=16,
        description=(
            "RAPTOR の階層の要約の LLM を同時に呼ぶ数の上限（#2067。1 つの階層の cluster を並べ、"
            "階層は順に作る）。429 を受けると自動で減らし（最小 1）、成功が続くとこの上限まで戻す。"
        ),
    )
    rag_parser_readiness_probe_enabled: bool = Field(
        default=False,
        description=(
            "readiness 画面の adapter version/可用性を parser サービスの /health 問い合わせで "
            "解決する。OFF(既定)は backend プロセス内の import 検出にフォールバック(開発/テスト "
            "用)。本番(systemd の unit)では true にしてサービスの導入状況を表示する。"
        ),
    )
    rag_parser_readiness_probe_timeout_seconds: float = Field(
        default=2.0,
        gt=0,
        description="readiness の /health 問い合わせ timeout(秒)。",
    )
    # --- サービス管理（前処理 / Parser マイクロサービスの稼働可視化・起動/停止。#286）---
    rag_service_control_enabled: bool = Field(
        default=False,
        description=(
            "サービス管理画面からの起動/停止(systemd の unit の操作)を有効化する。"
            "OFF(既定)は稼働状態の可視化のみで、制御 API は 409(control_disabled)で拒否する。"
            "local(PLATFORM_ENVIRONMENT=local)は自動で有効。操作は sudoers で許可した "
            "allowlist の unit の `systemctl enable --now / disable --now / restart` と "
            "`journalctl -u <unit>` だけを `sudo -n` で実行する(rag/docs/deployment.md)。"
        ),
    )
    rag_service_control_timeout_seconds: float = Field(
        default=60.0,
        gt=0,
        description=(
            "systemctl / journalctl の subprocess の timeout(秒)。超過は失敗として構造化返却する。"
        ),
    )
    rag_service_runtime_env_file: str = Field(
        default=str(BACKEND_ROOT / "service-runtime.env"),
        description=(
            "マイクロサービスの unit が EnvironmentFile で読む実行用の env ファイル。"
            "backend が起動/再起動の前に HuggingFace 設定と実効 OCI Enterprise AI 設定"
            "(model-settings.json 由来を含む)を書く(0600)。unit 側の path と一致させる。"
        ),
    )
    rag_service_status_probe_timeout_seconds: float = Field(
        default=5.0,
        gt=0,
        description=(
            "サービス管理画面が各マイクロサービスの /health を問い合わせる timeout(秒)。"
            "接続拒否/timeout は stopped、到達したが status!=ok は degraded として表示する。"
        ),
    )
    rag_segment_checkpoint_enabled: bool = Field(
        default=True,
        description="取込 segment checkpoint を Oracle に永続化し、失敗 segment の再試行に使う。",
    )
    rag_extraction_artifact_cache_enabled: bool = Field(
        default=True,
        description="構造化抽出 JSON artifact を chunk/embedding 前に Object Storage へ保存する。",
    )
    rag_extraction_artifact_prefix: str = Field(
        default="artifacts/extractions",
        max_length=256,
        description="構造化抽出 artifact の Object Storage key prefix。",
    )
    rag_review_gate_enabled: bool = Field(
        default=True,
        description=(
            "True のときファイル処理を段階レビュー式(EXTRACT→CHUNK→INDEX)にする。"
            "False は従来互換で EXTRACT job 内から最後まで進める。"
        ),
    )
    rag_auto_parse_after_preprocess_enabled: bool = Field(
        default=True,
        description="PREPROCESS 完了後に PREPROCESSED で止めず、EXTRACT を自動で続行する。",
    )
    rag_auto_chunk_after_extract_enabled: bool = Field(
        default=True,
        description="EXTRACT 完了後に REVIEW で止めず、CHUNK job を自動投入する。",
    )
    rag_auto_index_after_chunk_enabled: bool = Field(
        default=True,
        description="CHUNK 完了後に CHUNKED で止めず、INDEX job を自動投入する。",
    )

    # --- チャット（会話 / マルチモデル比較）---
    rag_chat_enabled: bool = Field(
        default=True,
        description=(
            "チャット(会話)機能の有効化。True で会話 API を提供し UI に項目を出す。"
            "False のとき会話 API は 404 を返す(運用キルスイッチ)。"
        ),
    )
    rag_chat_max_compare_models: int = Field(
        default=3,
        ge=1,
        le=5,
        description="マルチモデル比較で同時に回答生成する OCI モデルの最大数。",
    )
    rag_chat_max_active_answers_per_user: int = Field(
        default=3,
        ge=1,
        le=20,
        description=(
            "同じ利用者が同時に作成できるチャットの回答の数（backend のプロセスごと。#1175）。"
            "回答の作成は SSE の接続が切れても続くので、送信を重ねて LLM を使い過ぎないよう抑える。"
        ),
    )

    # --- レート制限（高コスト API の保護）---
    rate_limit_enabled: bool = Field(default=True)
    rate_limit_window_seconds: float = Field(default=60.0, gt=0.0, le=3600.0)
    rate_limit_search_requests: int = Field(default=60, ge=1, le=10000)
    rate_limit_evaluation_runs: int = Field(default=10, ge=1, le=1000)
    rate_limit_uploads: int = Field(default=30, ge=1, le=1000)
    rate_limit_ingest_requests: int = Field(default=20, ge=1, le=1000)

    # --- 監査 ---
    audit_context_hash_salt: str = Field(
        default="",
        description="tenant/user id を監査ログへ hash 化するときの任意 salt。.env から注入する。",
    )
    audit_persistence: AuditPersistence = Field(
        default="log",
        description="RAG 監査イベントの保存先。log / oracle / both。",
    )

    # --- Trace export（OpenTelemetry / Langfuse gateway 連携用）---
    trace_export_http_endpoint: str = Field(default="")
    trace_export_http_bearer_token: str = Field(default="")
    trace_export_http_timeout_seconds: float = Field(default=2.0, gt=0.0, le=30.0)
    trace_export_queue_size: int = Field(default=1024, ge=1, le=100000)

    @field_validator("model_settings_file")
    @classmethod
    def normalize_model_settings_file(cls, value: str) -> str:
        """空指定は共通 `.env` と同じ階層の既定ファイルへ戻す。"""
        return value.strip() or DEFAULT_MODEL_SETTINGS_FILE

    @field_validator("huggingface_endpoint")
    @classmethod
    def normalize_huggingface_endpoint(cls, value: str) -> str:
        """ミラー URL に scheme を補う。

        ``hf-mirror.com`` のように scheme なしで設定されると、HF hub の DL が
        ``httpx.UnsupportedProtocol``(URL missing 'http(s)://')で失敗する。
        scheme 省略時は ``https://`` を補う。空は空のまま(公式 hub 既定)。
        """
        endpoint = value.strip()
        if endpoint and "://" not in endpoint:
            return f"https://{endpoint}"
        return endpoint

    @field_validator("rag_parser_adapter_backend", mode="before")
    @classmethod
    def normalize_legacy_parser_adapter_backend(cls, value: object) -> object:
        """旧値・削除済みエンジン(#270・#22)の正規化。

        ``local`` は **正規化しない**。advanced diagnostics(scorecard/staging golden gate)が
        「常時利用可能な baseline」概念として扱う。runtime では ingestion._partition_source が
        ``local`` を既定エンジン(``docling``)のサービスへマップし、in-process 解析は実行しない。
        """
        return normalize_parser_adapter_backend_value(value)

    @field_validator("rag_preprocess_profile", mode="before")
    @classmethod
    def normalize_legacy_preprocess_profile(cls, value: object) -> object:
        """削除した変換方式(text_normalize・#2042 の 2 つ)は起動互換のため passthrough へ寄せる。"""
        return normalize_removed_preprocess_profile(value)

    @field_validator("rag_chunking_strategy", mode="before")
    @classmethod
    def normalize_legacy_chunking_strategy(cls, value: object) -> object:
        """削除した分割方式(sentence_window / hierarchical_parent_child)を後継へ寄せる。"""
        return normalize_legacy_chunking_strategy_value(value)

    @field_validator("rag_evaluation_suite", mode="before")
    @classmethod
    def normalize_legacy_evaluation_suite(cls, value: object) -> object:
        """削除した評価の基準(#591)を後継へ寄せる(起動互換。未知の値は拒否する)。"""
        normalized = str(value).strip().casefold()
        return LEGACY_EVALUATION_SUITES.get(normalized, value)

    @field_validator("oci_enterprise_ai_vlm_input_mode", mode="before")
    @classmethod
    def normalize_legacy_settings_vlm_input_mode(cls, value: object) -> object:
        """廃止済み旧値は起動互換のため files_api へ寄せる。"""
        if str(value).strip().casefold() == "auto":
            return "files_api"
        return value

    @field_validator("rag_graph_profile", mode="before")
    @classmethod
    def reject_removed_graph_profile(cls, value: object) -> object:
        """削除した full(#621)は読み替えず、書き換え先を示して起動を止める。"""
        if str(value).strip().casefold() in REMOVED_GRAPH_PROFILES:
            raise ValueError(
                "RAG_GRAPH_PROFILE=full は廃止しました(#621)。backend/.env の値を entities"
                "(構築する)か off(構築しない)に書き換えてください。"
            )
        return value

    @field_validator(
        "ingestion_queue_worker_concurrency",
        "ingestion_queue_heavy_phase_concurrency",
        mode="before",
    )
    @classmethod
    def normalize_ingestion_queue_concurrency(cls, value: object) -> object:
        """未設定(空)と大文字小文字の違いを auto に寄せる(#2027)。"""
        if value is None:
            return "auto"
        if isinstance(value, str):
            text = value.strip().lower()
            return "auto" if text in {"", "auto"} else text
        return value

    @field_validator(
        "ingestion_queue_worker_concurrency",
        "ingestion_queue_heavy_phase_concurrency",
    )
    @classmethod
    def validate_ingestion_queue_concurrency_range(
        cls, value: int | Literal["auto"]
    ) -> int | Literal["auto"]:
        if value != "auto" and not 1 <= value <= INGESTION_QUEUE_MAX_CONCURRENCY:
            raise ValueError(
                f"1〜{INGESTION_QUEUE_MAX_CONCURRENCY} の数か auto を指定してください。"
            )
        return value

    @model_validator(mode="after")
    def validate_ingestion_queue_heavy_phase_concurrency(self) -> Self:
        """数を書いた重い段の上限は、数を書いた全体の枠を超えない(#2027)。

        どちらかが auto なら起動時に全体の枠で頭打ちにする(vCPU で決まるため、ここでは決めない)。
        """
        total = self.ingestion_queue_worker_concurrency
        heavy = self.ingestion_queue_heavy_phase_concurrency
        if total != "auto" and heavy != "auto" and heavy > total:
            raise ValueError(
                "RAG_INGESTION_QUEUE_HEAVY_PHASE_CONCURRENCY は "
                "RAG_INGESTION_QUEUE_WORKER_CONCURRENCY 以下にしてください。"
            )
        return self

    def ingestion_queue_concurrency(self, *, vcpu: int | None = None) -> IngestionQueueConcurrency:
        """取込の worker の全体の枠と重い段の上限(auto は vCPU から決める。#2027)。"""
        return resolve_ingestion_queue_concurrency(
            self.ingestion_queue_worker_concurrency,
            self.ingestion_queue_heavy_phase_concurrency,
            vcpu=detect_vcpu_count() if vcpu is None else vcpu,
        )

    @model_validator(mode="after")
    def validate_ingestion_queue_lease(self) -> Self:
        """lease の TTL は heartbeat 間隔の 3 倍以上にする。

        heartbeat が 1〜2 回失敗しただけで、他の worker に実行中の job を回復させないため(#357)。
        """
        if (
            self.ingestion_queue_lease_ttl_seconds
            < self.ingestion_queue_heartbeat_interval_seconds * 3
        ):
            raise ValueError(
                "RAG_INGESTION_QUEUE_LEASE_TTL_SECONDS は "
                "RAG_INGESTION_QUEUE_HEARTBEAT_INTERVAL_SECONDS の 3 倍以上にしてください。"
            )
        return self

    def ingestion_silent_call_timeouts(self) -> dict[str, float]:
        """進捗を出す段の中で、進捗を変えずに続きうる 1 回の呼び出しの最長(秒。再試行を含む)。

        停滞の時間(``ingestion_job_stall_timeout_seconds``)はこれより長くする(#2003)。
        値の key は直す設定(環境変数)の名前。
        """
        timeouts = {
            "rag_parser_service_timeout_seconds": (
                self.rag_parser_service_timeout_seconds * self.rag_http_service_retry_attempts
                + self.rag_http_service_retry_max_delay_seconds
                * max(self.rag_http_service_retry_attempts - 1, 0)
            ),
            "oci_enterprise_ai_timeout_seconds": (
                self.oci_enterprise_ai_timeout_seconds * (self.oci_enterprise_ai_max_retries + 1)
            ),
        }
        return {settings_env_name("RAG_", field): value for field, value in timeouts.items()}

    @model_validator(mode="after")
    def validate_ingestion_job_stall_timeout(self) -> Self:
        """停滞の時間は、進捗を出さない 1 回の呼び出しの最長より長くする(#2003)。

        短いと、解析サービスの 1 回の呼び出しや Vision の再試行の間に、進んでいる job を止める。
        """
        name, longest = max(self.ingestion_silent_call_timeouts().items(), key=lambda item: item[1])
        if self.ingestion_job_stall_timeout_seconds <= longest:
            raise ValueError(
                "RAG_INGESTION_JOB_STALL_TIMEOUT_SECONDS は、進捗を出さない 1 回の呼び出しの最長"
                f"（{name} から {longest:g} 秒。再試行を含む）より長くしてください。"
            )
        return self

    @model_validator(mode="after")
    def validate_rag_chunk_settings(self) -> Self:
        """chunk size と各 chunking 戦略パラメータの整合性を起動時に検証する。"""
        self.rag_chunk_delimiter = self.rag_chunk_delimiter.strip()
        if not self.rag_chunk_delimiter:
            raise ValueError("RAG_CHUNK_DELIMITER を入力してください。")
        if self.rag_chunking_strategy == "fixed_delimiter":
            return self
        if self.rag_chunk_overlap >= self.rag_chunk_size:
            raise ValueError("RAG_CHUNK_OVERLAP は RAG_CHUNK_SIZE より小さくしてください。")
        if (
            self.rag_chunking_strategy in CHUNKING_STRATEGIES_WITH_MIN_CHARS
            and self.rag_chunk_min_chars >= self.rag_chunk_size
        ):
            raise ValueError("RAG_CHUNK_MIN_CHARS は RAG_CHUNK_SIZE より小さくしてください。")
        return self

    @property
    def oracle_driver_mode(self) -> str:
        """既定は Thin mode。PLATFORM_ORACLE_CLIENT_LIB_DIR を指定したときだけ Thick mode。

        Wallet の判定にも使う。
        """
        return "thick" if self.oracle_client_lib_dir.strip() else "thin"

    @property
    def oracle_connection_security(self) -> str:
        """RAG は Wallet mTLS だけに対応する（Walletless TLS は接続処理が未対応）。"""
        return "wallet_mtls"

    @property
    def resolved_oracle_wallet_dir(self) -> str:
        """Wallet 配置先。

        Thin は PLATFORM_ORACLE_WALLET_DIR、Thick は <CLIENT_LIB_DIR>/network/admin。
        """
        client_lib_dir = self.oracle_client_lib_dir.strip()
        if client_lib_dir:
            return str(Path(client_lib_dir).expanduser() / "network" / "admin")
        return self.oracle_wallet_dir.strip()

    @property
    def resolved_oracle_adb_region(self) -> str:
        """ADB 管理専用 region。旧設定互換のため PLATFORM_OCI_REGION へ fallback する。"""
        return self.oracle_adb_region.strip() or self.oci_region.strip()


@lru_cache
def _settings_singleton() -> Settings:
    """環境変数/.env と永続化ファイルから初期 Settings を作る。

    `.env` は module の `PLATFORM_ENV_FILE` / `BACKEND_ENV_FILE` から読む
    （本番は model_config と同じパス）。テストはこの 2 つを差し替えて、
    開発者の手元の `.env` を読まないようにする（#483）。
    """
    settings = Settings(_env_file=(PLATFORM_ENV_FILE, BACKEND_ENV_FILE))
    load_persisted_model_settings(settings)
    load_persisted_parser_settings(settings)
    # 以後の .env の変更を差分で取り込むため、今の .env の内容を基準として覚える。
    reload_env_settings_if_changed(settings)
    return settings


def get_settings() -> Settings:
    """設定のシングルトンを返す。.env・永続化ファイルの更新があれば再読込する。"""
    settings = _settings_singleton()
    reload_env_settings_if_changed(settings)
    reload_persisted_model_settings_if_changed(settings)
    PARSER_SETTINGS_STORE.reload_if_changed(settings)
    return settings


def settings_today() -> date:
    """共通の設定の時間帯（`PLATFORM_TIMEZONE`。既定 UTC）の今日（#3255）。

    OS の時間帯に左右されない。検索の基準日（`as_of`）・業務ガイドの期間・LLM に渡す
    「今日の日付」の既定に使う。
    """
    return local_today(get_settings())


def reset_settings_cache() -> None:
    """テストや明示的な再初期化のため Settings singleton を破棄する。"""
    _settings_singleton.cache_clear()
    MODEL_SETTINGS_STORE.reset()
    PARSER_SETTINGS_STORE.reset()
    LANGUAGE_PACK_STORE.reset()
    _ENV_RELOAD_STATE.key = None
    _ENV_RELOAD_STATE.snapshot = None


@dataclass
class _EnvReloadState:
    """前回読んだ .env の (パス, 更新時刻) と、その内容から作った Settings。"""

    key: tuple[tuple[str, int | None], ...] | None = None
    snapshot: Settings | None = None


_ENV_RELOAD_STATE = _EnvReloadState()
_ENV_RELOAD_LOCK = threading.Lock()


def _env_file_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def reload_env_settings_if_changed(settings: Settings) -> None:
    """別プロセス（もう一方の worker・取込 worker）が画面から保存した `.env` を取り込む（#465）。

    設定画面の保存は `.env` に書き、保存を受けたプロセスの Settings だけを書き換える。
    ほかのプロセスは、`.env` の更新時刻が変わったら読み直し、前回読んだ内容から
    **変わった項目だけ** を反映する（プロセスの中で変えた値や、`.env` にない値を既定値へ戻さない）。
    反映後は model-settings.json と parser-settings.json の値をかけ直す。`.env` のパスが
    変わったとき（テストの tmp など）は基準を取り直すだけにする。
    """
    files = (PLATFORM_ENV_FILE, BACKEND_ENV_FILE)
    key = tuple((str(path), _env_file_mtime_ns(path)) for path in files)
    state = _ENV_RELOAD_STATE
    if key == state.key:
        return
    with _ENV_RELOAD_LOCK:
        if key == state.key:
            return
        previous, previous_key = state.snapshot, state.key
        try:
            snapshot = Settings(_env_file=files)
        except (ValidationError, OSError) as exc:
            # 読み直しに失敗しても（手で書いた不正な値など）リクエストは止めず、今の値のまま動く。
            # 同じ内容で何度も読み直さないよう時刻は覚え、基準は最後に読めた内容のままにする。
            state.key = key
            logging.getLogger(__name__).warning(
                "rag_env_settings_reload_failed", extra={"error_type": type(exc).__name__}
            )
            return
        state.key, state.snapshot = key, snapshot
        same_files = previous_key is not None and [path for path, _ in previous_key] == [
            path for path, _ in key
        ]
        if previous is None or not same_files:
            return
        changed = [
            name
            for name in Settings.model_fields
            if getattr(snapshot, name) != getattr(previous, name)
        ]
        for name in changed:
            setattr(settings, name, getattr(snapshot, name))
        if changed:
            load_persisted_model_settings(settings)
            load_persisted_parser_settings(settings)


def resolve_model_settings_file(path_value: str) -> Path:
    """PLATFORM_MODEL_SETTINGS_FILE を共通 `.env` と同じディレクトリ基準で解決する。

    model-settings.json は3製品で共有する（#211）。
    """
    raw_path = path_value.strip() or DEFAULT_MODEL_SETTINGS_FILE
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (PLATFORM_ENV_FILE.parent / path).resolve()


_PARSER_ADAPTER_FIELDS = tuple(_PersistedParserAdapterSettings.model_fields)
# parser の API key（JSON に書かず、RAG の backend/.env に保存する。#106 / #211）。
# 外部 API を呼ぶ解析エンジンは無いため、現在は空(#22)。
PARSER_SECRET_ATTRS: tuple[str, ...] = ()


def resolve_language_pack_file(settings: Settings) -> Path:
    """RAG_LANGUAGE_PACK_FILE を RAG の backend/.env と同じディレクトリ基準で解決する(#2181)。"""
    path = Path(settings.rag_language_pack_file.strip() or DEFAULT_LANGUAGE_PACK_FILE).expanduser()
    return path if path.is_absolute() else (BACKEND_ENV_FILE.parent / path).resolve()


def resolve_parser_settings_file(path_value: str) -> Path:
    """RAG_PARSER_SETTINGS_FILE を RAG の backend/.env と同じディレクトリ基準で解決する。"""
    path = Path(path_value.strip() or DEFAULT_PARSER_SETTINGS_FILE).expanduser()
    if path.is_absolute():
        return path
    return (BACKEND_ENV_FILE.parent / path).resolve()


def _load_parser_adapters(settings: Settings, raw: Mapping[str, Any]) -> None:
    """parser-settings.json の内容を Settings へ反映する（無い項目は既定値）。"""
    parser = _PersistedParserAdapterSettings.model_validate(raw)
    for name in _PARSER_ADAPTER_FIELDS:
        setattr(settings, f"rag_parser_{name}", getattr(parser, name))


def _dump_parser_adapters(settings: Settings) -> dict[str, Any]:
    document: dict[str, Any] = {
        name: getattr(settings, f"rag_parser_{name}") for name in _PARSER_ADAPTER_FIELDS
    }
    return document


# モデル設定の読み書きは3製品共通（platform の pr_system_settings。#103）。
MODEL_SETTINGS_STORE = ModelSettingsStore(
    resolve_path=lambda settings: resolve_model_settings_file(settings.model_settings_file),
    # モデルの API key（PLATFORM_OCI_ENTERPRISE_AI_API_KEY）は共通 `.env` に保存する（#211）。
    # テストで差し替えられるよう、呼出時に module の値を参照する。
    env_file=lambda _settings: PLATFORM_ENV_FILE,
)
# 文書解析の設定は RAG 専用のファイルに置き、共有の model-settings.json には書かない（#1701）。
PARSER_SETTINGS_STORE = ParserSettingsStore(
    resolve_path=lambda settings: resolve_parser_settings_file(settings.rag_parser_settings_file),
    env_file=lambda _settings: BACKEND_ENV_FILE,
    load=_load_parser_adapters,
    dump=_dump_parser_adapters,
    secrets=tuple((attr, settings_env_name("RAG_", attr)) for attr in PARSER_SECRET_ATTRS),
)


def load_persisted_parser_settings(settings: Settings) -> None:
    """「文書解析」画面で保存した parser-settings.json があれば Settings へ上書き適用する。"""
    PARSER_SETTINGS_STORE.load(settings)


def load_persisted_model_settings(settings: Settings) -> None:
    """UI 保存済みの共有ランタイム設定 JSON があれば Settings へ上書き適用する。"""
    MODEL_SETTINGS_STORE.load(settings)


def reload_persisted_model_settings_if_changed(settings: Settings) -> None:
    """別 worker が保存した共有ランタイム設定を次回リクエストで取り込む。"""
    MODEL_SETTINGS_STORE.reload_if_changed(settings)
