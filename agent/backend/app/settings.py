"""サービス設定。共通基底 BaseServiceSettings を継承し、ドメイン設定を足す。"""

import logging
from functools import lru_cache
from pathlib import Path
from typing import Annotated

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
from pr_system_settings.model import (
    EnterpriseAiConfiguredModel,
    ModelSecretStateMixin,
    ModelSettingsStore,
)
from pydantic import BeforeValidator, Field

logger = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[1]
# 製品固有の設定（`AGENT_*`）は `backend/.env`（画面からは書かない）。
# 3製品共通の設定（`PLATFORM_*`）を置く共通 `.env`（既定は `<repo>/platform/.env`。#211）。
PLATFORM_ENV_FILE = platform_env_file(BACKEND_DIR)


class Settings(ModelSecretStateMixin, BaseServiceSettings):
    """サービス固有設定。

    環境変数名は属性名から決まる（#211）。3製品共通の属性（OCI 認証・アップロード保存先・
    モデル・データベース）は共通 `.env` の `PLATFORM_*`、それ以外は `backend/.env` の `AGENT_*`。
    """

    model_config = product_settings_config(prefix="AGENT_", backend_dir=BACKEND_DIR)

    service_name: str = "production-ready-agent"
    # CORS 許可オリジンの既定は製品の dev server（suite と衝突しない port。#25）。
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3102"])
    # 実行環境と認証方式は platform の RuntimeSettings を継承する。
    # 構成管理者・認証ポリシー・ログインの Cookie 名は共通 `.env` の PLATFORM_ADMIN_* /
    # PLATFORM_AUTH_*（#211）。Cookie は 3 製品で共有し、1 回のログインで全製品を使える（#1565）。
    app_admin_login_user_id: str = ""
    app_admin_login_user_password: str = ""
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
    # サービス間の token の署名鍵（共通 `.env` の PLATFORM_SERVICE_TOKEN_SECRET。#230 / #233）。
    app_service_token_secret: str = ""
    # 外部の MCP クライアントの API キーの上限（#1874。0 は上限なし）。1 分あたりの呼び出しの
    # 回数（MCP の入口）と、直近 24 時間に MCP で作れる Run の数（キーごと）。
    app_api_key_calls_per_minute: int = Field(default=60, ge=0, le=100_000)
    app_api_key_runs_per_day: int = Field(default=200, ge=0, le=1_000_000)
    # 公開の基底の URL（共通 `.env` の PLATFORM_PUBLIC_BASE_URL。例 `https://<公開 IP>`）。前段の
    # reverse proxy の URL を利用者が書き、API キーの画面の接続先に出す（#1870・#19）。
    app_public_base_url: str = ""
    # システム設定（3製品共通）。
    oci_config_file: str = "~/.oci/config"
    oci_config_profile: str = "DEFAULT"
    oci_compartment_id: str = ""
    oci_region: str = ""
    object_storage_region: str = ""
    object_storage_namespace: str = ""
    object_storage_bucket: str = "production-ready"
    upload_storage_backend: str = "local"
    local_storage_dir: str = "/u01/data/production-ready-agent"
    max_upload_bytes: int = 100 * 1024 * 1024
    model_settings_file: str = "model-settings.json"
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
    oci_enterprise_ai_vlm_path: str = "/responses"
    oci_enterprise_ai_vlm_input_mode: str = "auto"
    oci_enterprise_ai_llm_payload_template: str = ""
    oci_enterprise_ai_vlm_payload_template: str = ""
    oci_enterprise_ai_llm_response_path: str = ""
    oci_enterprise_ai_vlm_response_path: str = ""
    oci_enterprise_ai_timeout_seconds: float = 600.0
    oci_enterprise_ai_max_retries: int = 3
    # 出力の上限は登録モデルごとの `max_output_tokens`（#1719。platform の
    # `enterprise_ai_output_token_budget`）。共通の LLM / VLM の出力の上限は持たない。
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
    oci_genai_embedding_model: str = "cohere.embed-v4.0"
    oci_genai_embedding_dim: int = 1536
    oci_genai_rerank_model: str = "cohere.rerank-v4.0-fast"
    oracle_dsn: str | None = None
    oracle_user: str | None = None
    oracle_password: str | None = None
    oracle_client_lib_dir: str = "/u01/aipoc/instantclient_23_26"
    oracle_wallet_dir: str | None = None
    oracle_wallet_password: str | None = None
    oracle_adb_ocid: str | None = None
    oracle_adb_region: str | None = None
    oracle_tcp_connect_timeout_seconds: float = 10.0
    oracle_db_test_timeout_seconds: float = 15.0
    # PLATFORM_ORACLE_* の接続 pool の大きさ（Agent 固有。`app.oracle_connection`。#793）。
    oracle_pool_min_connections: int = 1
    oracle_pool_max_connections: int = 8
    # MCP 接続（#757）。RAG / NL2SQL は各製品の MCP（`POST /api/mcp`）で、接続 `rag` / `nl2sql`
    # の URL の初期値になる（例: http://rag-host/api/mcp）。Run の利用者のサービストークン
    # （共通 `.env` の PLATFORM_SERVICE_TOKEN_SECRET で署名）で呼ぶ（#233）。タイムアウト（秒）は
    # 1 回の MCP の呼び出しの待ち時間で、画面の「MCP 接続」の上限（900 秒）以下にする。
    agent_external_rag_mcp_url: str | None = None
    # RAG の `rag_search` は RAG の回答生成（`RAG_ANSWER_TIMEOUT_SECONDS`。既定 600 秒）を通しで待つ
    # ため、RAG の回答の上限より長くする（RAG の画面の timeout と同じ 630 秒。#1425）。
    agent_external_rag_timeout_seconds: float = 630.0
    # 画面（ブラウザ）から RAG を開く起点（例: http://rag-host）。RAG の図の根拠を開く短命の URL
    # （#1311）の path をこの起点に付ける。空なら RAG が MCP の呼び出しを受けた起点（MCP の URL の
    # host。private IP のことがある）のまま使う。1 台の Compute（#1316）では同じ origin の `/rag`。
    agent_external_rag_public_url: str | None = None
    agent_external_nl2sql_mcp_url: str | None = None
    # NL2SQL の `nl2sql_query` / `nl2sql_get_job` は SQL の生成・実行をジョブにし、1 回の
    # 呼び出しでは結果を最大 45 秒（`wait_seconds`）だけ待って、終わらなければ job_id を返す。
    # `nl2sql_query` は SQL の生成前の確認（OCI Enterprise AI の LLM。下書きと回答の後の
    # 再検査で最大 2 回。#1441）を呼び、その分だけジョブを待つ秒数を減らす。待ちの上限 45 秒と
    # SQL の生成前の確認の 480 秒（NL2SQL の LLM 1 回 240 秒 × 試行 2 回。#2240）に、HTTP の往復・
    # ジョブの登録・DB の負荷の余裕 75 秒を足した 600 秒にする（#1432・#2240）。続きの取得
    # （`nl2sql_get_job`）は 1 回 20 秒（`tools._JOB_POLL_WAIT_SECONDS`）で、ジョブの完了は
    # `agent_nl2sql_job_wait_seconds` まで続きを取って待つ（#848 / #1430）。
    agent_external_nl2sql_timeout_seconds: float = 600.0
    # NL2SQL のジョブが pending / running のまま返ったとき、ツールの中で完了を待つ合計の上限
    # （秒。#848）。既定の SQL の生成（Select AI の 1 回）と実行は、それぞれ NL2SQL の DB の
    # 呼び出しの上限（`NL2SQL_ORACLE_CALL_TIMEOUT_SECONDS`、既定 120 秒）で打ち切られるため、
    # 300 秒で足りる。
    # nl2sql_get_job を wait_seconds 付きで繰り返し呼ぶ。0 なら待たずにそのまま返す。
    agent_nl2sql_job_wait_seconds: float = 300.0
    # MCP の 1 メッセージの再試行の回数（#854）。送信前の失敗（接続できない・接続の timeout）と
    # 429 / 503 は全メッセージ、502 / 504 は手順（initialize・tools/list）と読み取り専用の
    # ツールだけ。待ちは指数 backoff + jitter（Retry-After に従う）で、呼び出し全体は接続の
    # timeout に収める。
    agent_external_mcp_max_retries: int = 3
    # MCP 接続の宣言。JSON list か {"servers": [...]}。各項目は server_id/id 必須、
    # base_url・auth_mode・資格情報・timeout_seconds を任意で持つ。
    agent_external_mcp_servers_json: str | None = None
    # Skill 外部定義: 中立ディレクトリ(skills/<id>/SKILL.md)と JSON 宣言。
    agent_skills_dir: str | None = None
    agent_skills_definitions_json: str | None = None
    # 組み込み Runtime の実行（#754）。in_process は API のプロセスで実行し、
    # それ以外は runtime_dispatcher（別プロセス）が claim して実行する。
    agent_runtime_dispatch_mode: str = "in_process"
    agent_runtime_dispatch_poll_seconds: float = 1.0
    agent_runtime_dispatch_lease_seconds: int = 120
    agent_permission_default_mode: str = "approval"
    # Run・業務 Agent と画面で変えた定義の保存先（auto / memory / file / oracle_checkpoint /
    # oracle_normalized）。Oracle は共通の PLATFORM_ORACLE_* で接続し、テーブルはシステムテーブルが
    # 作る（#764）。oracle_normalized は監査用の projection（AGENT_RUNTIME_RUNS など）も書く。
    # auto（既定。#839）は DB の設定がそろっていれば oracle_checkpoint、無ければ memory
    # （起動時に判定）。DB に接続できないあいだも起動し、接続できた時点で読み込む（#1212）。
    agent_runtime_repository_backend: str = "auto"
    agent_runtime_snapshot_path: str | None = None
    agent_runtime_projection_retention_days: int = 0
    agent_runtime_projection_write_mode: str = "replace"
    agent_max_tool_calls_per_run: int = 20
    # モデルの応答が出力の上限で未完了（max_output_tokens）でも、使った出力が上限のこの割合未満なら
    # 上限に達したはずがない呼び先の一時的な不具合として、同じ要求を 1 回だけ送り直す（#2355）。
    # 0 以下は送り直さない。
    agent_model_incomplete_retry_output_ratio: float = Field(default=0.1, ge=0.0, le=1.0)
    # モデルに渡す入力を、モデルの入力の上限に収める（#2383。`context_window`）。予算は登録モデルの
    # 入力の上限（max_input_tokens）から出力の上限と余白（入力の上限 × 割合）を引いた token の数で、
    # 超えるときは古いツールの結果から縮める。token の見積もりは platform の
    # `estimate_input_tokens`（ASCII は 4 字で 1、ほかは 1 字で 1）に係数を掛ける。
    agent_context_compaction_enabled: bool = True
    agent_context_token_estimate_scale: float = Field(default=1.0, gt=0.0, le=10.0)
    agent_context_safety_margin_ratio: float = Field(default=0.05, ge=0.0, le=0.5)
    # 支援タスクの予算（#1243）。超える呼び出しは実行せず、ツールの結果（budget_exceeded）でモデルに
    # 知らせる（Run は失敗にしない）。0 以下は上限なし。
    # Run ごとの RAG の呼び出し（rag_search・rag_retrieve_evidence。1 回が重い検索と回答の生成）。
    # 既定 6 は 5 段の多段の質問（段ごとに 1 回）と 1 回の言い換え（#1345）。
    # 本文を読むツール（rag_read_source・rag_outline・rag_read_document・rag_read_section）は
    # 数えない（ツールの呼び出しの上限には数える）。
    agent_max_rag_calls_per_run: int = 6
    # 同じ会話（thread）の通しのツールの呼び出し。前の Run の支援タスクの状態に積む。
    agent_max_tool_calls_per_task: int = 60
    # 品質評価の 1 ケースの上限時間（秒。Run の実行。判定は別。#1432）。0 以下（既定）は自動で、
    # 接続 `rag` の待ち時間 ×（Run の RAG の予算〔agent_max_rag_calls_per_run。0 以下なら
    # agent_max_tool_calls_per_run〕+ 1）にする。+1 は回答の最終の検証（rag_validate_answer）と
    # モデルの推論の分。既定では 630 ×（6 + 1）= 4410 秒。300 秒〜86400 秒に収める。
    agent_evaluation_case_timeout_seconds: float = 0.0
    # 品質評価で同時に実行するケースの数の上限（#2004）。判定（LLM-as-judge）は job の中で 1 つの
    # 上限つきの並行の実行器（pr_backend_core.bounded_calls）を共有し、429 を受けたら全体で待って
    # 並行の数を下げる（最小 1）。結果はケースの順のまま。1 は 1 件ずつ（#2004 より前と同じ）。
    agent_evaluation_concurrency: int = Field(default=2, ge=1, le=8)
    # 公開に求める品質評価の合格率（#2519。0〜1、0 は求めない）。0 より大きいと、業務 Agent の
    # 公開には、下書きを最後に変えた後に始めた下書きの評価（完了したもの）の最新の合格率がこの値
    # 以上であることを求める。前の版に戻す・スキルの更新だけの公開は対象にしない。
    agent_publish_min_pass_rate: float = Field(default=0.0, ge=0.0, le=1.0)
    # 公開前の回帰の門（#2569。0〜1、未設定は使わない）。公開中の版がある業務 Agent の公開には、
    # 下書きの評価と同じ評価セット・同じ評価ケースで公開中の版を評価した最新の評価を基準にし、
    # 下書きの合格率が「基準 − この値」以上であることを求める（基準の評価が無ければ断る）。
    # 空の値（`AGENT_PUBLISH_MAX_PASS_RATE_DROP=`）は未設定と同じにする。
    agent_publish_max_pass_rate_drop: Annotated[
        float | None, BeforeValidator(lambda value: None if value == "" else value)
    ] = Field(default=None, ge=0.0, le=1.0)
    # 公開前の評価の門で求める、下書きの評価のケースの数の下限（#2569。0 は求めない）。合格率の
    # 分母になったケース（判定できなかったケース・取り消しを除く）を数える。門を使うときだけ見る。
    agent_publish_min_cases: int = Field(default=0, ge=0, le=1000)
    # 業務 Agent の版の履歴に残す版の数（#2356）。新しい版を公開したとき、公開日時の新しい順に
    # 超えた版を消す（公開中の版は古くても残す。版の番号は振り直さない）。
    agent_max_published_versions: int = Field(default=5, ge=1, le=100)
    # 回答の最終の検証（#1246。#1277 で既定 on）。on のとき、RAG の根拠を使った Run の回答を、
    # 保存する前に Control Plane が根拠を返した RAG の接続ごとに MCP rag_validate_answer で検証する
    # （RAG がモデルを 1 回呼ぶため、接続ごとに 10〜20 秒増える）。根拠と矛盾する段落だけを回答から
    # 外して「根拠と矛盾し除いた内容」に示し、確かめられない段落は本文に残して印を付ける（#2738）。
    # 予算（上の 2 つ）には数えない。
    agent_final_validation_enabled: bool = True
    # 記録済みの出力を本文で繰り返す報告の見出し。語を変える場合はこの設定で選ぶ。
    agent_output_record_section_titles: list[str] = Field(
        default_factory=lambda: [
            "記録",
            "記録内容",
            "記録された項目",
            "記録した項目",
            "出力の記録",
            "出力（記録済み）",
            "出力項目の記録",
        ]
    )
    # 根拠・データのツールを呼ばずに作った回答の拒答（#1835。既定 on）。on のとき、根拠・
    # データのツール（RAG・NL2SQL・登録した MCP 接続）を持つ業務 Agent が、どれも呼ばずに最終の
    # 回答を作ったら 1 回だけモデルへ差し戻し、それでも呼ばなければ決まった拒答の文に置き換える
    # （確認の質問だけの回答は除く）。回答前の確認（`ask_user_confirmation`）も、ツールを呼ぶ前なら
    # 先に調べさせる（#1917）。
    agent_tool_grounding_required: bool = True
    agent_metrics_enabled: bool = True
    agent_trace_events_enabled: bool = True
    agent_trace_events_buffer_size: int = 500
    agent_trace_events_retention_seconds: int = 86_400
    agent_trace_exporter_url: str | None = None
    agent_trace_exporter_api_key: str | None = None
    agent_trace_exporter_timeout_seconds: float = 2.0
    agent_trace_exporter_retry_queue_size: int = 100
    agent_trace_exporter_retry_max_attempts: int = 3
    agent_trace_exporter_retry_base_delay_seconds: float = 1.0
    agent_trace_exporter_retry_max_delay_seconds: float = 60.0
    agent_trace_exporter_retry_worker_enabled: bool = True
    agent_trace_exporter_retry_worker_interval_seconds: float = 5.0
    agent_trace_exporter_retry_worker_batch_size: int = 100
    agent_trace_sample_rate: float = 1.0
    agent_langfuse_host: str | None = None
    agent_langfuse_public_key: str | None = None
    agent_langfuse_secret_key: str | None = None
    agent_opentelemetry_endpoint: str | None = None
    agent_artifact_storage_backend: str = "inline"
    agent_artifact_storage_path: str = ".agent-artifacts"

    @property
    def oracle_driver_mode(self) -> str:
        """Agent は python-oracledb の Thin mode だけで接続する（Wallet の判定用）。"""
        return "thin"

    @property
    def oracle_connection_security(self) -> str:
        """Agent は Wallet mTLS だけに対応する。"""
        return "wallet_mtls"

    @property
    def resolved_oracle_adb_region(self) -> str:
        """ADB 管理用の region。未設定なら PLATFORM_OCI_REGION を使う。"""
        return (self.oracle_adb_region or self.oci_region or "").strip()

    @property
    def resolved_oracle_wallet_dir(self) -> str:
        """参照実装と同じく PLATFORM_ORACLE_CLIENT_LIB_DIR/network/admin を Wallet 配置先にする。"""
        client_lib_dir = self.oracle_client_lib_dir.strip()
        if client_lib_dir:
            return str(Path(client_lib_dir).expanduser() / "network" / "admin")
        return (self.oracle_wallet_dir or "").strip()


def resolve_model_settings_file(path_value: str) -> Path:
    """PLATFORM_MODEL_SETTINGS_FILE を共通 `.env` と同じディレクトリ基準で解決する。"""
    path = Path(path_value.strip() or "model-settings.json").expanduser()
    return path if path.is_absolute() else (PLATFORM_ENV_FILE.parent / path).resolve()


# モデル設定の読み書きは3製品共通（platform の pr_system_settings。#103）。
# model-settings.json と API key は3製品で共有する（共通 `.env`。#211）。
# テストで PLATFORM_ENV_FILE を差し替えられるよう、呼出時に module の値を参照する。
MODEL_SETTINGS_STORE = ModelSettingsStore(
    resolve_path=lambda settings: resolve_model_settings_file(settings.model_settings_file),
    env_file=lambda _settings: PLATFORM_ENV_FILE,
)


# 3 製品共通の設定（OCI 認証・アップロード保存先・モデル・データベース・認証ポリシーなど）は
# `platform/.env`。別の製品・別の worker の画面で保存した値を、再起動なしで次のリクエストから
# 使う（#2092・#2102）。テストが差し替えられるよう、呼出時に値を引く。
def _env_files() -> tuple[Path, Path]:
    return (PLATFORM_ENV_FILE, product_env_file("AGENT_", BACKEND_DIR))


_PLATFORM_ENV_RELOADER = EnvFieldReloader(
    Settings, env_files=_env_files, fields=PLATFORM_SETTING_FIELDS
)


@lru_cache
def _settings_singleton() -> Settings:
    # `.env` は module の値から読む（読み直しの部品と同じ場所。テストが差し替えられる）。
    settings = Settings(_env_file=_env_files())
    MODEL_SETTINGS_STORE.load(settings)
    _PLATFORM_ENV_RELOADER.mark_loaded(settings)
    return settings


def get_settings() -> Settings:
    """設定シングルトン。共通 `.env`・保存済みのモデル設定が更新されていれば再読込する。"""
    settings = _settings_singleton()
    reload_platform_settings_if_changed(settings)
    MODEL_SETTINGS_STORE.reload_if_changed(settings)
    return settings


def reload_platform_settings_if_changed(settings: Settings) -> list[str]:
    """別の製品・別の worker が共通 `.env` に保存した共通の設定を取り込む（#2092・#2102）。

    `.env` を読み直した項目があれば、model-settings.json のモデル設定をかけ直す（JSON の値を
    `.env` の値より優先する。RAG の `reload_env_settings_if_changed` と同じ）。接続 pool は、
    作ったときの接続引数と今の接続引数を比べて作り直す（`SharedOraclePool`）。
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
