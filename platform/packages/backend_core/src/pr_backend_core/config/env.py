"""3製品共通の `.env` と、環境変数名の規則（#211）。

- 3製品共通の設定（システム設定画面のアップロード保存先・モデル・データベースと、
  共通認証の構成管理者・認証ポリシー）は platform の共通 `.env` に置き、`PLATFORM_` で始める。
- 製品固有の設定は製品の `backend/.env` に置き、製品の接頭辞（`RAG_` / `NL2SQL_` / `AGENT_`）で
  始める。
- Settings の属性名は変えず、環境変数名だけを validation alias で決める。旧名は読まない。
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterable, Sequence
from functools import partial
from pathlib import Path

from pydantic import AliasGenerator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

logger = logging.getLogger(__name__)

# 共通 `.env` の場所を上書きする環境変数（コンテナなど、既定の場所にないとき）。
PLATFORM_ENV_FILE_ENV = "PLATFORM_ENV_FILE"
PLATFORM_ENV_PREFIX = "PLATFORM_"

# 共通 `.env` に置く Settings の属性。製品が持たない属性は無視される。
PLATFORM_SETTING_FIELDS = frozenset(
    {
        "environment",
        "auth_mode",
        # trace（3 製品共通）
        "trace_enabled",
        "trace_sample_rate",
        "trace_otlp_endpoint",
        "trace_otlp_api_key",
        "trace_queue_size",
        "trace_batch_size",
        "trace_export_timeout_seconds",
        "trace_shutdown_timeout_seconds",
        "trace_ui_max_segments",
        "trace_ui_max_spans",
        "trace_retention_days",
        "trace_store_flush_seconds",
        "trace_store_batch_size",
        "trace_store_queue_size",
        # アップロード保存先
        "local_storage_dir",
        # モデル
        "model_settings_file",
        "enterprise_ai_endpoint",
        "enterprise_ai_project_id",
        "enterprise_ai_api_key",
        # OCI Enterprise AI のセカンダリ接続（#533）。API key は共通 `.env` だけに置く。
        "enterprise_ai_secondary_endpoint",
        "enterprise_ai_secondary_project_id",
        "enterprise_ai_secondary_api_key",
        # OCI Enterprise AI のターシャリ接続（#786。OpenAI / OpenAI 互換 API 向け）。
        "enterprise_ai_tertiary_endpoint",
        "enterprise_ai_tertiary_project_id",
        "enterprise_ai_tertiary_api_key",
        "enterprise_ai_models",
        "enterprise_ai_default_text_model",
        "enterprise_ai_default_vision_model",
        "enterprise_ai_default_judge_model",
        "enterprise_ai_llm_path",
        "enterprise_ai_vlm_path",
        "enterprise_ai_vlm_input_mode",
        "enterprise_ai_llm_payload_template",
        "enterprise_ai_vlm_payload_template",
        "enterprise_ai_llm_response_path",
        "enterprise_ai_vlm_response_path",
        "enterprise_ai_timeout_seconds",
        "enterprise_ai_max_retries",
        "genai_endpoint",
        "genai_embedding_model",
        "genai_embedding_dim",
        "genai_rerank_model",
        "genai_embed_model_id",
        "genai_rerank_model_id",
        # データベース
        "oracle_user",
        "oracle_password",
        "oracle_dsn",
        "oracle_driver_mode",
        "oracle_connection_security",
        "oracle_client_lib_dir",
        "oracle_wallet_dir",
        "oracle_wallet_password",
        "oracle_tcp_connect_timeout_seconds",
        "oracle_db_test_timeout_seconds",
        # 共通認証（構成管理者・認証ポリシー・ログインの Cookie 名。環境と認証方式も共通）。
        # Cookie 名を 3 製品で共通にし、1 回のログインで全製品を使えるようにする（#1565）。
        "app_admin_login_user_id",
        "app_admin_login_user_password",
        "app_auth_cookie_secure",
        "app_auth_session_cookie_name",
        "app_auth_csrf_cookie_name",
        "app_auth_idle_timeout_minutes",
        "app_auth_absolute_timeout_hours",
        "app_auth_failed_login_limit",
        "app_auth_lockout_minutes",
        "app_auth_login_attempt_limit",
        "app_auth_login_ip_attempt_limit",
        "app_auth_login_attempt_window_minutes",
        "app_auth_password_min_length",
        "app_auth_password_max_length",
        "app_auth_argon2_time_cost",
        "app_auth_argon2_memory_kib",
        "app_auth_argon2_parallelism",
        # サービス間の token の署名鍵（Agent → RAG / NL2SQL の MCP。#230）
        "app_service_token_secret",
        # 公開の基底の URL（配備の suite-init.sh が公開 IP と port から書く。API キーの画面。#1870）
        "app_public_base_url",
        # 外部の MCP クライアントの API キーの呼び出しの上限（#1874）。1 分あたりの呼び出しの回数
        # （3 製品の MCP）と、1 日（直近 24 時間）あたりの Agent の Run の数。
        "app_api_key_calls_per_minute",
        "app_api_key_runs_per_day",
        # チャットの同じ会話の前のターンを LLM に渡す予算（3 製品で同じ値。#1818）
        "chat_history_max_turns",
        "chat_history_max_chars_per_turn",
        "chat_history_max_chars",
        # チャットの「会話の履歴」に出す期間（日。3 製品で同じ値。#2340）
        "chat_history_days",
        # 会話の添付の期限（時間）・1 つの大きさ・会話あたりの件数（3 製品で同じ値。#2517）
        "chat_attachment_ttl_hours",
        "chat_attachment_max_bytes",
        "chat_attachment_max_files",
        # 利用者の暦の時間帯（IANA の地域名。既定 UTC。#3255・#3274）
        "timezone",
    }
)


def platform_env_name(field: str) -> str:
    """共通の属性の環境変数名。`app_` は付けない（`app_auth_*` → `PLATFORM_AUTH_*`）。"""
    return PLATFORM_ENV_PREFIX + field.upper().removeprefix("APP_")


def product_env_name(prefix: str, field: str) -> str:
    """製品の属性の環境変数名。属性名が接頭辞で始まっていれば重ねない。"""
    name = field.upper()
    return name if name.startswith(prefix) else prefix + name


def settings_env_name(prefix: str, field: str) -> str:
    if field in PLATFORM_SETTING_FIELDS:
        return platform_env_name(field)
    return product_env_name(prefix, field)


def platform_env_file(backend_dir: Path) -> Path:
    """共通 `.env`。`PLATFORM_ENV_FILE` がなければ `<repo>/platform/.env`。"""
    configured = os.environ.get(PLATFORM_ENV_FILE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return backend_dir.resolve().parents[1] / "platform" / ".env"


def product_env_file(prefix: str, backend_dir: Path) -> Path:
    """製品の `backend/.env`。`<接頭辞>ENV_FILE`（例 `RAG_ENV_FILE`）があればその場所（#1972）。"""
    configured = os.environ.get(f"{prefix}ENV_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return backend_dir / ".env"


def isolate_env_files_for_tests(prefix: str) -> None:
    """テストに手元の `.env`（共通の `platform/.env` と製品の `backend/.env`）を読ませない。

    3 製品の `tests/conftest.py` の先頭で、`app` を import する前に呼ぶ（Settings・認証などが
    `.env` の場所を決める前。#1972）。環境変数で渡すので、xdist の worker・テストが起動する
    子プロセスにも効く。
    実 Oracle の結合テストの接続は環境変数（`PLATFORM_ORACLE_*`）で渡す（CI は `platform/.env` を
    環境変数に読み込む）。`.env` の読み書きを確かめるテストは、製品の module の
    `PLATFORM_ENV_FILE` / `BACKEND_ENV_FILE` を `tmp_path` に差し替える。
    """
    # 作られない場所（親のディレクトリも無い）。誤って書き込もうとすると失敗して気づける。
    missing = Path(__file__).resolve().with_name("__no_env_file_in_tests__")
    os.environ[PLATFORM_ENV_FILE_ENV] = str(missing / "platform.env")
    os.environ[f"{prefix}ENV_FILE"] = str(missing / "backend.env")


def product_settings_config(*, prefix: str, backend_dir: Path) -> SettingsConfigDict:
    """製品の Settings の model_config。共通 `.env` → 製品の `backend/.env` の順に読む。

    環境変数は `.env` より優先する。属性名での生成（テストの `Settings(oracle_dsn=...)`）も許す。
    旧名を読まないよう、Settings は `PlatformEnvSourcesMixin` を使う
    （`BaseServiceSettings` は継承済み）。
    """
    return SettingsConfigDict(
        env_file=(platform_env_file(backend_dir), product_env_file(prefix, backend_dir)),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
        alias_generator=AliasGenerator(validation_alias=partial(settings_env_name, prefix)),
    )


def _alias_only(source: PydanticBaseSettingsSource) -> PydanticBaseSettingsSource:
    """環境変数 / `.env` では validation alias（新名）だけで探す。属性名（旧名）では探さない。"""
    source.config = {**source.config, "populate_by_name": False, "validate_by_name": False}
    if isinstance(source, DotEnvSettingsSource):
        # `.env` の未使用の行は extra として渡され、属性名と同じ旧名（`LOG_LEVEL` など）が
        # populate_by_name で属性に入ってしまう。一致した項目だけを返す。
        source.dotenv_filtering = "only_existing"
    return source


class PlatformEnvSourcesMixin:
    """`product_settings_config` と組み合わせる。旧名（属性名と同じ環境変数名）を読まない。

    `populate_by_name=True` は `Settings(oracle_dsn=...)` のような属性名での生成のためだけに使う。
    """

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            _alias_only(env_settings),
            _alias_only(dotenv_settings),
            file_secret_settings,
        )


def settings_env_names(settings_cls: type[BaseSettings]) -> dict[str, str]:
    """Settings の属性名 → 環境変数名（`.env.example` や設定監査の照合に使う）。"""
    names: dict[str, str] = {}
    for field, info in settings_cls.model_fields.items():
        alias = info.validation_alias
        names[field] = alias if isinstance(alias, str) else field.upper()
    return names


type _EnvFileSignature = tuple[tuple[str, int | None, int | None], ...]


class EnvFieldReloader:
    """`.env` の更新（mtime・size）を見て、指定した Settings の項目だけを読み直す（#2092・#2102）。

    NL2SQL・Agent は 3 製品共通の項目（`PLATFORM_SETTING_FIELDS`）すべてに使う。RAG は `.env` の
    全体を同じ考え方で読み直す（`app.config.reload_env_settings_if_changed`。#465）。

    設定画面の保存は共通の `.env` に書き、保存を受けたプロセスの Settings だけを書き換える。
    ほかの製品の backend・同じ製品の別の worker は、`get_settings()` からこの部品を呼んで、
    `.env` が変わった次のリクエストで取り込む。

    - 前回読んだ `.env` の内容から **変わった項目だけ** を反映する（プロセスの中で変えた値を、
      `.env` の別の行の変更で戻さない。RAG の `reload_env_settings_if_changed` と同じ。#465）。
    - `.env` のパスが変わったとき（テストの tmp など）は、基準を取り直すだけにする。
    - 環境変数で与えた項目は読み直さない（環境変数は `.env` より優先する）。
    - 読み直しに失敗しても（手で書いた不正な値など）止めず、今の値のまま動く。同じ内容で何度も
      読み直さないよう、`.env` の状態は覚える。
    - `env_files` はテストが `.env` の場所を差し替えられるよう、呼び出しのたびに解決する。
    """

    def __init__(
        self,
        settings_cls: type[BaseSettings],
        *,
        env_files: Callable[[], Sequence[Path]],
        fields: Iterable[str],
    ) -> None:
        self._settings_cls = settings_cls
        self._env_files = env_files
        self._fields = tuple(field for field in fields if field in settings_cls.model_fields)
        self._env_names = settings_env_names(settings_cls)
        self._lock = threading.Lock()
        self._loaded: _EnvFileSignature | None = None
        self._snapshot: dict[str, object] | None = None

    def _signature(self) -> _EnvFileSignature:
        signature: list[tuple[str, int | None, int | None]] = []
        for path in self._env_files():
            try:
                stat = path.stat()
            except OSError:
                signature.append((str(path), None, None))
                continue
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(signature)

    def mark_loaded(self, settings: BaseSettings) -> None:
        """Settings を `.env` から作った直後に呼ぶ（今の `.env` の内容を基準として覚える）。

        基準は `.env` を読み直した値にする（#2102）。`settings` には `.env` の後にかけた値
        （model-settings.json のモデル設定など）が入っているので、それを基準にすると、`.env` の
        別の行の変更で、`.env` の古い値へ戻してしまう。読めなければ `settings` の値を基準にする。
        """
        with self._lock:
            self._loaded = self._signature()
            source: BaseSettings = settings
            try:
                source = self._settings_cls(_env_file=tuple(self._env_files()))
            except Exception as exc:  # noqa: BLE001 - 読めなければ今の値を基準にする
                logger.warning(
                    "env_settings_reload_failed", extra={"error_type": type(exc).__name__}
                )
            self._snapshot = {field: getattr(source, field) for field in self._fields}

    def reset(self) -> None:
        """テストや Settings の作り直しのために、基準を忘れる。"""
        with self._lock:
            self._loaded = None
            self._snapshot = None

    def reload_if_changed(self, settings: BaseSettings) -> list[str]:
        """`.env` が変わっていれば読み直し、反映した項目の名前を返す。"""
        signature = self._signature()
        if signature == self._loaded:
            return []
        with self._lock:
            if signature == self._loaded:
                return []
            previous_signature, previous_snapshot = self._loaded, self._snapshot
            self._loaded = signature
            try:
                fresh = self._settings_cls(_env_file=tuple(self._env_files()))
            except Exception as exc:  # noqa: BLE001 - 読み直しの失敗でリクエストを止めない
                logger.warning(
                    "env_settings_reload_failed", extra={"error_type": type(exc).__name__}
                )
                return []
            snapshot = {field: getattr(fresh, field) for field in self._fields}
            self._snapshot = snapshot
            same_files = previous_signature is not None and [
                path for path, _, _ in previous_signature
            ] == [path for path, _, _ in signature]
            if previous_snapshot is None or not same_files:
                return []
            environ = {name.upper() for name in os.environ}
            changed: list[str] = []
            for field in self._fields:
                if self._env_names[field].upper() in environ:
                    continue
                if snapshot[field] != previous_snapshot.get(field):
                    setattr(settings, field, snapshot[field])
                    changed.append(field)
            return changed
