"""サービス設定。共通基底 BaseServiceSettings を継承し、ドメイン設定を足す。"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    PLATFORM_SETTING_FIELDS,
    BaseServiceSettings,
    EnvFieldReloader,
    platform_env_file,
    product_env_file,
    product_settings_config,
)
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
from pr_system_settings.model import connection_label as connection_label
from pr_system_settings.model import (
    enterprise_ai_connection_for_model as enterprise_ai_connection_for_model,
)
from pr_system_settings.model import (
    enterprise_ai_default_model_id as enterprise_ai_default_model_id,
)
from pr_system_settings.model import enterprise_ai_model_catalog as enterprise_ai_model_catalog
from pr_system_settings.model import (
    enterprise_ai_reasoning as enterprise_ai_reasoning,
)
from pr_system_settings.model import enterprise_ai_vision_model_id as enterprise_ai_vision_model_id
from pydantic import Field, field_validator

BACKEND_DIR = Path(__file__).resolve().parents[1]
# 製品固有の設定（NL2SQL_*）。Select AI Credential など製品固有の writer が書く。
BACKEND_ENV_FILE = product_env_file("NL2SQL_", BACKEND_DIR)
# 3製品共通の設定（PLATFORM_*）。システム設定画面と構成管理者のパスワード変更が書く（#211）。
PLATFORM_ENV_FILE = platform_env_file(BACKEND_DIR)
DEFAULT_MODEL_SETTINGS_FILE = "model-settings.json"
logger = logging.getLogger(__name__)


class Settings(ModelSecretStateMixin, BaseServiceSettings):
    """サービス固有設定。

    共通の属性（OCI / Oracle / モデル / アップロード保存先 / 構成管理者・認証ポリシー）は
    共通 `.env` の `PLATFORM_*`、それ以外は製品の `backend/.env` の `NL2SQL_*` から読む（#211）。
    属性名は変えず、環境変数名は `product_settings_config` の規則で決まる。
    """

    model_config = product_settings_config(prefix="NL2SQL_", backend_dir=BACKEND_DIR)

    service_name: str = "production-ready-nl2sql"
    enable_metrics: bool = True
    oracle_user: str = ""
    oracle_password: str = ""
    oracle_dsn: str = ""
    oracle_driver_mode: str = "thin"
    oracle_connection_security: str = "wallet_mtls"
    oracle_client_lib_dir: str = ""
    oracle_wallet_dir: str = "/u01/aipoc/wallet"
    oracle_wallet_password: str = ""
    oracle_adb_ocid: str = ""
    oracle_adb_region: str = ""
    oci_region: str = ""
    oci_compartment_id: str = ""
    oci_config_file: str = "~/.oci/config"
    oci_config_profile: str = "DEFAULT"
    oci_auth_mode: str = "config_file"
    oci_user_ocid: str = ""
    oci_fingerprint: str = ""
    oci_tenancy_ocid: str = ""
    oci_key_file: str = ""
    oci_genai_endpoint: str = ""
    oci_genai_embedding_model: str = "cohere.embed-v4.0"
    oci_genai_embedding_dim: int = 1536
    oci_genai_rerank_model: str = "cohere.rerank-v4.0-fast"
    oci_genai_embed_model_id: str = "cohere.embed-v4.0"
    oci_genai_rerank_model_id: str = "cohere.rerank-v4.0-fast"
    oci_enterprise_ai_endpoint: str = ""
    oci_enterprise_ai_project_ocid: str = ""
    oci_enterprise_ai_api_key: str = ""
    # OCI Enterprise AI のセカンダリ接続（#533）。モデルを呼ぶ接続は
    # enterprise_ai_connection_for_model で引く。
    oci_enterprise_ai_secondary_endpoint: str = ""
    oci_enterprise_ai_secondary_project_ocid: str = ""
    oci_enterprise_ai_secondary_api_key: str = ""
    # ターシャリ接続（#786。OpenAI / OpenAI 互換 API 向け。Project OCID は任意）。
    oci_enterprise_ai_tertiary_endpoint: str = ""
    oci_enterprise_ai_tertiary_project_ocid: str = ""
    oci_enterprise_ai_tertiary_api_key: str = ""
    oci_enterprise_ai_models: list[EnterpriseAiConfiguredModel] = Field(default_factory=list)
    oci_enterprise_ai_default_text_model: str = ""
    oci_enterprise_ai_default_vision_model: str = ""
    # 品質評価の判定のモデル（#3574。共通のモデル設定の保存で持つ。RAG の品質評価だけが読む）。
    oci_enterprise_ai_default_judge_model: str = ""
    oci_enterprise_ai_llm_path: str = "/responses"
    oci_enterprise_ai_llm_payload_template: str = ""
    oci_enterprise_ai_llm_response_path: str = ""
    oci_enterprise_ai_vlm_path: str = "/responses"
    oci_enterprise_ai_vlm_payload_template: str = ""
    oci_enterprise_ai_vlm_response_path: str = ""
    oci_enterprise_ai_vlm_input_mode: str = "auto"
    oci_enterprise_ai_timeout_seconds: float = 600.0
    oci_enterprise_ai_max_retries: int = 3
    # 出力の上限は登録モデルごとの `max_output_tokens`（#1719。既定 32768）。共通の LLM / VLM の
    # 上限は持たない（呼び出しは pr_system_settings の enterprise_ai_output_token_budget）。
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
    model_settings_file: str = DEFAULT_MODEL_SETTINGS_FILE
    upload_storage_backend: str = "local"
    local_storage_dir: str = "/u01/data/production-ready-nl2sql"
    object_storage_region: str = ""
    object_storage_namespace: str = ""
    object_storage_bucket: str = "production-ready"
    max_upload_bytes: int = 200 * 1024 * 1024
    # NL2SQL 安全境界（既定: SELECT のみ許可）。DDL/DML/PLSQL は禁止する方針。
    nl2sql_allow_select_only: bool = True
    nl2sql_default_row_limit: int = 100
    # 同時に実行する NL2SQL job worker 数の上限(超過分は pending のまま待機)。
    # in-process queue worker 数をこの値で抑え、Oracle セッションの枯渇を防ぐ。
    nl2sql_job_max_concurrency: int = Field(default=4, ge=1, le=64)
    # inprocess: local/CI 用 bounded worker、external: API は永続 queue への投入だけを行う。
    nl2sql_synthetic_worker_mode: Literal["inprocess", "external"] = "inprocess"
    nl2sql_job_worker_mode: Literal["inprocess", "external"] = "inprocess"
    nl2sql_job_worker_poll_seconds: float = 1.0
    nl2sql_job_lease_seconds: float = 900.0
    # legacy snapshot の running job が lease を持たずこの秒数更新されない場合のみ、
    # 再起動前の中断として扱う。新しい queue job は lease 期限後に worker が再 claim する。
    nl2sql_job_stale_after_seconds: float = Field(default=1800.0, ge=60.0)
    # oracle（既定）: python-oracledb 経由で Oracle / Select AI を呼ぶ。
    # deterministic: CI / テスト用の決定論スタブで、tests/conftest.py が明示する（#897）。
    nl2sql_runtime_mode: str = "oracle"
    # oracle が既定。memory は local/CI で明示指定する非永続モード。
    nl2sql_persistence_mode: str = "oracle"
    # oracle の永続化は常に entity 単位の repository（incremental）。旧 snapshot の表と移行の
    # CLI は持たない（`legacy_snapshot` は #1758、`NL2SQL_ORACLE_STATE_TABLE` は #1765 で廃止）。
    nl2sql_cache_ttl_seconds: float = 5.0
    nl2sql_profile_cache_max_entries: int = 1000
    nl2sql_schema_object_cache_max_entries: int = 500
    nl2sql_ontology_graph_cache_max_revisions: int = 3
    nl2sql_ontology_graph_cache_max_megabytes: int = 256
    nl2sql_schema_refresh_worker_enabled: bool = True
    # inprocess: local 開発用 thread、external: API は job 永続化だけを行う。
    nl2sql_schema_refresh_worker_mode: str = "inprocess"
    nl2sql_profile_list_refresh_timeout_seconds: float = Field(default=600.0, ge=30)
    nl2sql_schema_refresh_lease_seconds: float = 900.0
    # 明示 system schema DDL が既存 DML lock の解放を待つ上限。
    nl2sql_system_schema_ddl_lock_timeout_seconds: int = Field(
        default=30,
        ge=0,
        le=120,
    )
    # SQL生成評価は local で inprocess、Oracle 運用で external worker を使う。
    nl2sql_quality_evaluation_worker_mode: str = "inprocess"
    nl2sql_quality_evaluation_worker_poll_seconds: float = 1.0
    nl2sql_quality_evaluation_lease_seconds: float = 900.0
    nl2sql_quality_evaluation_attempt_timeout_seconds: float = 300.0
    nl2sql_quality_evaluation_max_file_bytes: int = 10 * 1024 * 1024
    nl2sql_quality_evaluation_max_cases: int = 100
    nl2sql_quality_evaluation_max_attempts: int = 1000
    # SQL生成評価の試行（ケース × engine × 繰り返し）を同時に実行する数の上限（#2059。共通の
    # 上限つきの並行の実行器）。SQL の生成と判定の呼び出しは 1 つの実行器を共有し、429 を受けたら
    # 全体で待って並行の数を下げる（最小 1）。結果はケース・engine・繰り返しの位置に書く。
    # 1 は 1 件ずつ。
    # Select AI / Select AI Agent の試行は DB の接続を使うため、接続の pool の上限より小さくする。
    nl2sql_quality_evaluation_concurrency: int = Field(default=2, ge=1, le=8)
    # Ontology worker / reasoning。typo(例: "in-process")だと job が誰にも処理されず
    # queued のまま沈黙するため、起動時に Literal で検証する。
    nl2sql_ontology_worker_mode: Literal["inprocess", "external"] = "inprocess"
    # AI 構築の抽出/命名呼び出しが要る出力トークンの量（#1719 で 8000 から 32768）。送る上限は
    # モデルの出力の上限と入力の上限の残りで抑える（enterprise_ai_output_token_budget）。推論のある
    # モデルは推論の token も数えるため、小さい値では SCHEMA_NAMING の JSON が途中で切れる。
    nl2sql_ontology_extraction_max_output_tokens: int = 32768
    # GraphRAG 流 gleaning(取りこぼし回収の追加パス)。0 で無効。
    nl2sql_ontology_extraction_gleaning_passes: int = 1
    # 1 回の抽出呼び出しに載せる資料本文の上限(チャンクが大きいほど抽出漏れが増えるため)。
    nl2sql_ontology_extraction_batch_max_chars: int = 12000
    # 一括の LLM・Embedding の呼び出しの並行の上限（#2004。共通の上限つきの並行の実行器
    # pr_backend_core.bounded_calls）。429 を受けたら全体で待って並行の数を下げ（最小 1）、成功が
    # 続けばこの上限まで戻す。結果は入力の順のまま。1 は 1 件ずつ。
    # 業務プロファイルの AI 構築の抽出（Q/A・資料の組）と、資料の PDF の文字の無いページの OCR。
    nl2sql_ontology_build_concurrency: int = Field(default=4, ge=1, le=16)
    # コメント・アノテーションの SQL の生成の組（ドメインは前の組の結果を使うため順に呼ぶ）。
    nl2sql_metadata_sql_concurrency: int = Field(default=4, ge=1, le=16)
    # Embedding の要求（96 件ずつ。Feedback の索引・質問分類の学習・オントロジーの埋め込み）。
    nl2sql_embedding_concurrency: int = Field(default=2, ge=1, le=8)
    # 公開前の Markdown 解析は自動再送せず、待機時間を含む期限を永続化する。
    nl2sql_ontology_preparation_timeout_seconds: float = Field(default=600.0, gt=0)
    nl2sql_ontology_worker_poll_seconds: float = 1.0
    nl2sql_ontology_validation_timeout_seconds: float = Field(default=600.0, gt=0)
    nl2sql_ontology_publish_timeout_seconds: float = Field(default=600.0, gt=0)
    nl2sql_ontology_worker_claim_timeout_seconds: float = 3900.0
    nl2sql_ontology_build_timeout_seconds: float = Field(default=21600.0, gt=0)
    nl2sql_ontology_build_lease_seconds: float = Field(default=120.0, ge=30)
    # Oracle Profile 同期は永続 job で実行し、DB round-trip と job 全体を別々に制限する。
    nl2sql_oracle_call_timeout_seconds: float = 120.0
    nl2sql_profile_sync_job_timeout_seconds: float = 300.0
    nl2sql_ontology_reasoning_profile: str = "owl2rl"
    nl2sql_ontology_shacl_enabled: bool = True
    nl2sql_ontology_profile_confirmation_required: bool = True
    nl2sql_ontology_confirmation_ttl_seconds: int = 900
    # ユーザ要望により Oracle Select AI / Select AI Agent を NL2SQL エンジンとして同時サポートする。
    # local / CI では deterministic adapter、実運用では Oracle DB adapter に差し替える。
    nl2sql_select_ai_enabled: bool = True
    nl2sql_select_ai_agent_enabled: bool = True
    nl2sql_enterprise_ai_direct_enabled: bool = True
    nl2sql_select_ai_profile_prefix: str = "NL2SQL"
    nl2sql_select_ai_provider: str = "oci"
    nl2sql_select_ai_credential_name: str = "NL2SQL_OCI_CRED"
    nl2sql_select_ai_region: str = "us-chicago-1"
    nl2sql_select_ai_model: str = ""
    nl2sql_schema_owner_allowlist: list[str] = Field(default_factory=list)
    nl2sql_schema_sample_rows: int = 3
    nl2sql_schema_sample_columns_per_table: int = 6
    oracle_db_test_timeout_seconds: float = 15.0
    oracle_tcp_connect_timeout_seconds: float = 10.0
    nl2sql_oracle_connect_timeout_seconds: int = 5
    # 状態の保存先（業務プロファイル・ジョブ・履歴・オントロジー・評価）の接続 pool の上限（#830）。
    # ジョブの worker・heartbeat・API の要求が短く借りて返す。
    # 業務データ・Select AI の接続は含まない。
    nl2sql_oracle_state_pool_max: int = Field(default=8, ge=1, le=64)
    # アプリの接続で業務データを読む（SELECT の実行）・Select AI で生成するときの接続 pool の上限
    # （#904）。system_admin 用と非 system_admin 用の 2 つの pool に、それぞれこの上限を使う。
    # 状態の保存先の接続は含まない。
    nl2sql_oracle_runtime_pool_max: int = Field(default=8, ge=1, le=64)
    nl2sql_csv_import_max_rows: int = 5000
    # 新規取込の文字列列は、追記の余地を持たせる。
    nl2sql_csv_import_text_min_chars: int = Field(default=255, ge=1, le=4000)
    nl2sql_csv_import_text_growth_factor: float = Field(default=2.0, ge=1.0, le=10.0)
    # 件数未指定の SELECT 実行で読み込む行数の安全上限(超えた分は読まず truncated にする)。
    nl2sql_max_result_rows: int = Field(default=100000, ge=1)
    # チャットで生成した SQL の実行（#1154）の上限。行は 1 回の実行で取得する行数（超えた分は読まず
    # 「さらに行があります」にする）、セルの文字数（超えた文字は切る）、応答の行のバイト数（超えた
    # 行は返さず打ち切りにする）。時間は SQL 生成のジョブと同じ call timeout
    # （nl2sql_oracle_call_timeout_seconds）。全件が要るときは SQL 生成・SELECT SQL の実行の
    # 画面で実行する。
    nl2sql_chat_result_max_rows: int = Field(default=1000, ge=1, le=5000)
    nl2sql_chat_result_max_cell_chars: int = Field(default=2000, ge=100, le=100000)
    nl2sql_chat_result_max_bytes: int = Field(default=2_000_000, ge=10_000, le=20_000_000)
    # チャットのジョブの中で実行した結果の行を、画面が受け取るまで置いておく上限（秒。#1176）。
    # 行は会話に残さない（受け取ったら消す。期限を過ぎたら要約と「もう一度実行」を出す）。
    nl2sql_chat_result_retention_seconds: int = Field(default=600, ge=30, le=86_400)
    # 操作が途絶えた未完了の query session（回答前の確認など）を中止するまでの時間（秒）。
    # 画面の中止・再読込の後始末が届かなかった session（ブラウザの異常終了・通信断）を残さない。
    nl2sql_query_session_idle_ttl_seconds: int = Field(default=86_400, ge=600, le=2_592_000)
    # 回答前の確認で、全部の質問に答えた後に要件のまとめを LLM で検査し直す回数の上限（#1439）。
    # 上限に達したら、残った確認の項目を画面に明示し、利用者が SQL の生成に進むかを決める。
    # 確認のカードの往復の上限はこの値 + 1（最初の確認を 1 回目に数える）で、既定は 3 回
    # （3 製品の「回答前の確認」の既定。platform/docs/ux-contracts/confirm-before-answer.md §5）。
    nl2sql_requirement_max_review_rounds: int = Field(default=2, ge=1, le=10)
    # 回答前の確認の LLM の呼び出し（#1715）。利用者が画面で待つので、共通の LLM の設定
    # （待ち時間 600 秒・再試行 3 回）より短くする。出力の上限は、推論（reasoning）の token も
    # 上限に数えるモデル（openai.gpt-oss-120b など）で推論と JSON が収まる量（2026-10-13 の実測。
    # 推論は 800〜2000 token、要件の JSON は 1000〜2500 token）。
    # 上限は 16384（#2310。ユーザーの判断）。ほかの Structured Outputs の「32768 を下回らない」
    # （#1719）の例外で、渡した値をそのまま上限にする（モデルの出力の上限と入力の上限の残りでは
    # 抑える）。推論以外の出力が止まらずに上限まで書き続ける回（2026-10-18 の実測。32768 で
    # 224〜237 秒）を約半分の時間で切り、上限を広げずに推論を下げた送り直しに任せる。下限の 8192 は
    # 実測の要る量の約 2 倍。
    # 待ち時間は 240 秒（#2240）。同じ context（約 3800 文字）でも応答まで 9.6〜208 秒とぶれ
    # （2026-10-17 の実測。出力の長さに依らない）、90 秒では遅い回が 2 回とも切れた。再試行 1 回で
    # 最悪 480 秒（Agent の AGENT_EXTERNAL_NL2SQL_TIMEOUT_SECONDS はこれを含めて決める）。
    nl2sql_requirement_timeout_seconds: float = Field(default=240.0, gt=0.0, le=600.0)
    nl2sql_requirement_max_retries: int = Field(default=1, ge=0, le=5)
    nl2sql_requirement_max_output_tokens: int = Field(default=16384, ge=8192, le=2_000_000)
    # SELECT 実行で使ってよい利用者定義・package 関数(カンマ区切り)。
    # 例: APP.FMT_CODE,UTIL_PKG.LABEL
    nl2sql_allowed_user_functions: str = ""
    nl2sql_csv_import_max_columns: int = 200
    nl2sql_feedback_embedding_enabled: bool = False
    nl2sql_feedback_vector_table: str = "NL2SQL_FEEDBACK_VECTORS"
    nl2sql_feedback_vector_index: str = "NL2SQL_FEEDBACK_VEC_IDX"

    # アプリケーション認証/RBAC。local/CI は PLATFORM_AUTH_MODE=local を明示する。
    app_admin_login_user_id: str = ""
    app_admin_login_user_password: str = ""
    # ログインの Cookie は 3 製品で共有する（共通 `.env` の PLATFORM_AUTH_*_COOKIE_NAME。#1565）。
    app_auth_session_cookie_name: str = DEFAULT_SESSION_COOKIE_NAME
    app_auth_csrf_cookie_name: str = DEFAULT_CSRF_COOKIE_NAME
    app_auth_idle_timeout_minutes: int = 60
    app_auth_absolute_timeout_hours: int = 12
    app_auth_failed_login_limit: int = 5
    app_auth_lockout_minutes: int = 15
    # ログインの試行の回数の制限（ログイン ID と送信元 IP の組・送信元 IP ごと。0 で無効。#1087）。
    app_auth_login_attempt_limit: int = 5
    app_auth_login_ip_attempt_limit: int = 20
    app_auth_login_attempt_window_minutes: int = 15
    app_auth_password_min_length: int = 12
    app_auth_password_max_length: int = 128
    app_auth_argon2_time_cost: int = 3
    app_auth_argon2_memory_kib: int = 65536
    app_auth_argon2_parallelism: int = 4
    # Agent が MCP（/api/mcp）を Run の利用者として呼ぶサービストークンの署名鍵（#230 / #231）。
    # 共通 .env の PLATFORM_SERVICE_TOKEN_SECRET。空なら MCP は 503 になる。
    app_service_token_secret: str = ""
    # 公開の基底の URL（共通 .env の PLATFORM_PUBLIC_BASE_URL。例 https://<公開 IP>）。配備の
    # suite-init.sh が書き、API キーの画面の接続先に出す（#1870）。
    app_public_base_url: str = ""
    # 外部の MCP クライアントの API キーの 1 分あたりの呼び出しの回数（#1874。0 は上限なし）。
    app_api_key_calls_per_minute: int = Field(default=60, ge=0, le=100_000)

    @property
    def resolved_oci_config_profile(self) -> str:
        """PLATFORM_OCI_CONFIG_PROFILE。空なら DEFAULT。"""
        return self.oci_config_profile.strip() or "DEFAULT"

    @property
    def resolved_oracle_wallet_dir(self) -> str:
        """明示 Wallet 配置先を優先し、未指定時だけ Thick の既定位置を返す。"""
        wallet_dir = self.oracle_wallet_dir.strip()
        if wallet_dir:
            return wallet_dir
        if self.oracle_driver_mode.strip().lower() == "thin":
            return ""
        client_lib_dir = self.oracle_client_lib_dir.strip()
        if client_lib_dir:
            return str(Path(client_lib_dir).expanduser() / "network" / "admin")
        return ""

    @property
    def resolved_oracle_adb_region(self) -> str:
        """ADB 管理専用 region。未設定なら PLATFORM_OCI_REGION へ fallback する。"""
        return self.oracle_adb_region.strip() or self.oci_region.strip()

    @field_validator("model_settings_file")
    @classmethod
    def normalize_model_settings_file(cls, value: str) -> str:
        """空指定は共通 .env と同じ階層の既定ファイルへ戻す。"""
        return value.strip() or DEFAULT_MODEL_SETTINGS_FILE

    @field_validator("oracle_driver_mode")
    @classmethod
    def validate_oracle_driver_mode(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"thin", "thick"}:
            raise ValueError(
                "PLATFORM_ORACLE_DRIVER_MODE は thin または thick を指定してください。"
            )
        return normalized

    @field_validator("oracle_connection_security")
    @classmethod
    def validate_oracle_connection_security(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"wallet_mtls", "walletless_tls"}:
            raise ValueError(
                "PLATFORM_ORACLE_CONNECTION_SECURITY は wallet_mtls または "
                "walletless_tls を指定してください。"
            )
        return normalized


@lru_cache
def _settings_singleton() -> Settings:
    """設定シングルトン。"""
    # `.env` は module の値から読む（読み直しの部品と同じ場所。テストが差し替えられる）。
    settings = Settings(_env_file=(PLATFORM_ENV_FILE, BACKEND_ENV_FILE))
    load_persisted_model_settings(settings)
    _PLATFORM_ENV_RELOADER.mark_loaded(settings)
    return settings


def get_settings() -> Settings:
    """設定のシングルトンを返す。共通 `.env`・永続化ファイルの更新があれば再読込する。"""
    settings = _settings_singleton()
    reload_platform_settings_if_changed(settings)
    reload_persisted_model_settings_if_changed(settings)
    return settings


def reload_platform_settings_if_changed(settings: Settings) -> list[str]:
    """別の製品・別の worker が共通 `.env` に保存した共通の設定を取り込む（#2092・#2102）。

    `.env` を読み直した項目があれば、model-settings.json のモデル設定をかけ直す（JSON の値を
    `.env` の値より優先する。RAG の `reload_env_settings_if_changed` と同じ）。接続 pool・OCI の
    client は、作ったときの設定と今の設定を比べて作り直す（`SharedOraclePool` など）。
    """
    changed = _PLATFORM_ENV_RELOADER.reload_if_changed(settings)
    if changed:
        MODEL_SETTINGS_STORE.load(settings, refresh_secret=True)
        logger.info("platform_settings_reloaded", extra={"fields": ",".join(sorted(changed))})
    return changed


def reset_settings_cache() -> None:
    """テストや明示的な再初期化のため Settings singleton を破棄する。"""
    _settings_singleton.cache_clear()
    MODEL_SETTINGS_STORE.reset()
    _PLATFORM_ENV_RELOADER.reset()


# 3 製品共通の設定（OCI 認証・アップロード保存先・モデル・データベース・認証ポリシーなど）は
# `platform/.env`。別の製品・別の worker の画面で保存した値を、再起動なしで次のリクエストから
# 使う（#2092・#2102）。テストが差し替えられるよう、呼出時に値を引く。
_PLATFORM_ENV_RELOADER = EnvFieldReloader(
    Settings,
    env_files=lambda: (PLATFORM_ENV_FILE, BACKEND_ENV_FILE),
    fields=PLATFORM_SETTING_FIELDS,
)


def resolve_model_settings_file(path_value: str) -> Path:
    """PLATFORM_MODEL_SETTINGS_FILE を共通 .env と同じディレクトリ基準で解決する。

    model-settings.json は3製品で共有する（#211）。テストが差し替えられるよう、
    共通 .env の場所は呼出時に module の値を参照する。
    """
    raw_path = path_value.strip() or DEFAULT_MODEL_SETTINGS_FILE
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (PLATFORM_ENV_FILE.parent / path).resolve()


# モデル設定の読み書きは3製品共通（platform の pr_system_settings。#103）。
# API key（PLATFORM_OCI_ENTERPRISE_AI_API_KEY）は共通 .env へ保存する（#211）。
MODEL_SETTINGS_STORE = ModelSettingsStore(
    resolve_path=lambda settings: resolve_model_settings_file(settings.model_settings_file),
    env_file=lambda _settings: PLATFORM_ENV_FILE,
)


def load_persisted_model_settings(settings: Settings) -> None:
    """UI 保存済みのモデル設定 JSON があれば Settings へ上書き適用する。"""
    MODEL_SETTINGS_STORE.load(settings)


def reload_persisted_model_settings_if_changed(settings: Settings) -> None:
    """別 worker が保存したモデル設定を次回リクエストで取り込む。"""
    MODEL_SETTINGS_STORE.reload_if_changed(settings)
