"""モデル設定の API（3製品共通。NL2SQL の実装を基準に移設。#103）。

- 登録モデル（LLM・埋め込み・再ランク。種類 `kind`。#45）と、接続・既定のモデルの設定。
  埋め込み / 再ランクも LLM と同じく、選んだ接続の Endpoint URL と API key で呼ぶ
  （`embedding_model_connection` / `embedding_api_base_url`）
- OCI Enterprise AI の接続（Endpoint URL・Project OCID・API key）は、プライマリ接続・
  セカンダリ接続・ターシャリ接続の 3 件まで（#533 / #542 / #786）。ターシャリ接続は OpenAI /
  OpenAI 互換 API 向けで、Project OCID は任意。登録モデルごとに `connection_id` で使う接続を選ぶ
  （未指定ならプライマリ接続）。実行時は `enterprise_ai_connection_for_model` でモデルから接続を引く
- API key は共通 `.env`（`platform/.env`）の `PLATFORM_OCI_ENTERPRISE_AI_API_KEY`
  （プライマリ接続）・`PLATFORM_OCI_ENTERPRISE_AI_SECONDARY_API_KEY`（セカンダリ接続）・
  `PLATFORM_OCI_ENTERPRISE_AI_TERTIARY_API_KEY`（ターシャリ接続）だけに保存し、JSON には書かない
- それ以外は `model-settings.json`（`version: 4`）へ保存し、起動時と mtime の変化時に読み込む
- 登録に無いモデルは、別の接続へ切り替えずに失敗させる（#3240）
- 登録モデルごとに推論（思考）の量 `reasoning_effort` を持つ（#1425。既定は low。#3004）。
  3 製品の呼び出しは `enterprise_ai_reasoning`（送る値の解釈はここだけ。none は送らない）で
  モデルから引き、Responses API の `reasoning: {"effort": ...}` を送る
- 登録モデルごとに入力の上限 `max_input_tokens`（tokens）を持つ（#1437。既定 128000）。
  回答の根拠などの入力の予算は `input_token_budget` で token の数として決める（値の解釈は
  ここだけ。token の見積もり `estimate_input_tokens` の実体は `pr_backend_core.token_estimate`。
  3 製品はこれを使う）
- 登録モデルごとに出力の上限 `max_output_tokens`（tokens）を持つ（#1719。既定 32768。
  入力の上限以下）。
  入力の上限は prompt と応答の合計（context）で、出力の上限はその中の応答の分（推論のあるモデルは
  推論の token も含む）。呼び出しの `max_output_tokens` は `enterprise_ai_output_token_budget`
  （モデルの出力の上限と、入力の上限から prompt の見積もりを引いた残りの小さい方）で決め、
  未完了（`incomplete` / `max_output_tokens`）の送り直しは `enterprise_ai_retry_output_token_budget`
  でモデルの上限まで広げる。共通の LLM / VLM の出力の上限（#1719 より前の
  `llm_max_output_tokens` / `vlm_max_output_tokens`）は持たない
- model-settings.json はモデルの設定だけを持ち、製品固有の設定は置かない（他製品の保存で欠けると
  その製品が読めなくなるため。RAG の文書解析の設定は RAG の parser-settings.json。#1701）
- モデル単位の接続テスト。実際の呼び出しは製品が `run_model_test` で渡す
- 既定のモデルは 2 つ（#499）。画像を読む呼び出しは「既定の Vision モデル」、
  それ以外は「既定のテキストモデル」を使う。画面・API では 2 つとも必須（#566）
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal
from uuid import uuid4

from dotenv import dotenv_values
from fastapi import APIRouter, HTTPException, params
from pr_backend_core.llm_request_params import collect_dropped_parameters
from pr_backend_core.schemas import ApiResponse

# 入力 token の見積もりは backend_core の 1 つ（検索の結果の本文の予算と共有する。#3321）。
from pr_backend_core.token_estimate import estimate_input_tokens as estimate_input_tokens
from pr_enterprise_ai.errors import (
    classify_enterprise_ai_http_failure,
    enterprise_ai_failure_actions,
    enterprise_ai_http_status_from_message,
)
from pr_enterprise_ai.urls import (
    DEFAULT_ENTERPRISE_AI_API_PATH as DEFAULT_ENTERPRISE_AI_API_PATH,
)
from pr_enterprise_ai.urls import enterprise_ai_base_url as enterprise_ai_base_url
from pr_enterprise_ai.urls import enterprise_ai_request_url as enterprise_ai_request_url
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator

from .env_file import write_env_values

logger = logging.getLogger(__name__)

ENTERPRISE_AI_API_KEY_ENV = "PLATFORM_OCI_ENTERPRISE_AI_API_KEY"
ENTERPRISE_AI_SECONDARY_API_KEY_ENV = "PLATFORM_OCI_ENTERPRISE_AI_SECONDARY_API_KEY"
ENTERPRISE_AI_TERTIARY_API_KEY_ENV = "PLATFORM_OCI_ENTERPRISE_AI_TERTIARY_API_KEY"
# OCI Enterprise AI の接続（#533）。プライマリ接続が既定。接続ごとに Settings の属性（env 名）が
# 決まっているので、ID は枠の名前にする。画面の名前は「プライマリ接続」「セカンダリ接続」
# 「ターシャリ接続」（#542 / #786）。プライマリ接続は #533 より前からの属性・env 名をそのまま使う。
ENTERPRISE_AI_PRIMARY_CONNECTION_ID: Final = "primary"
ENTERPRISE_AI_SECONDARY_CONNECTION_ID: Final = "secondary"
ENTERPRISE_AI_TERTIARY_CONNECTION_ID: Final = "tertiary"
ENTERPRISE_AI_CONNECTION_IDS = (
    ENTERPRISE_AI_PRIMARY_CONNECTION_ID,
    ENTERPRISE_AI_SECONDARY_CONNECTION_ID,
    ENTERPRISE_AI_TERTIARY_CONNECTION_ID,
)
MAX_ENTERPRISE_AI_CONNECTIONS = len(ENTERPRISE_AI_CONNECTION_IDS)
# 既定の判定モデル（`default_judge_model_id`。#3574）の「回答と同じモデル」を表す明示の値。
# 空（既定）は既定のテキストモデル、それ以外は登録モデルの ID。
# 解決は `enterprise_ai_judge_model_id` だけで行う。
ENTERPRISE_AI_JUDGE_SAME_AS_ANSWER: Final = "same_as_answer"
# 設定してもしなくてもよい接続（プライマリ接続の後ろ）。Endpoint URL があるときだけ有効になる。
_OPTIONAL_CONNECTION_IDS = ENTERPRISE_AI_CONNECTION_IDS[1:]
# Project OCID を任意にする接続（#786）。ターシャリ接続は OpenAI / OpenAI 互換 API 向けで、
# Project OCID（`OpenAI-Project` ヘッダー）は OCI Enterprise AI に使うときだけ要る。

# 4: 登録モデルの `reasoning_effort`（#1425）。3 の JSON はそのまま読める
# （項目が無い = 既定の low）。
# 5: 登録モデルの `max_input_tokens`（#1437）。4 の JSON はそのまま読める
# （項目が無い = 既定の 128000）。
# 6: 登録モデルの `max_output_tokens`（#1719）。共通の `llm_max_output_tokens` /
# `vlm_max_output_tokens` は持たず、5 の JSON のそれらは読み捨てる（項目の無いモデル =
# 既定の 32768。入力の上限がそれより小さいモデルは入力の上限）。
# 7: 登録モデルの種類 `kind`（text / embedding / rerank）と、既定の埋め込み / 再ランクのモデル
# （#45）。6 の JSON の `generative_ai` の節は、読み込むときに種類 embedding / rerank の登録モデル
# （プライマリ接続）と既定のモデルへ移し、Base URL・次元は読み捨てる。
MODEL_SETTINGS_DOCUMENT_VERSION = 7
# この版より前の JSON だけが API key（`enterprise_ai.api_key`）を持てた
# （v3 から secret は `.env`）。
MODEL_SETTINGS_SECRET_FREE_DOCUMENT_VERSION = 3
MODEL_SETTINGS_FILE_MODE = 0o600
MODEL_SETTINGS_DIRECTORY_MODE = 0o700

EnterpriseAiVlmInputMode = Literal["auto", "files_api", "inline_image"]
EnterpriseAiConnectionId = Literal["primary", "secondary", "tertiary"]
# 登録モデルの種類（#45）。埋め込み / 再ランクも LLM と同じ一覧に、接続を選んで登録する。
EnterpriseAiModelKind = Literal["text", "embedding", "rerank"]
ENTERPRISE_AI_MODEL_KIND_TEXT: Final = "text"
ENTERPRISE_AI_MODEL_KIND_EMBEDDING: Final = "embedding"
ENTERPRISE_AI_MODEL_KIND_RERANK: Final = "rerank"
ModelSettingsSecretSource = Literal["environment", "legacy_json", "missing"]
# 推論（思考）の量（#1425。Responses API の `reasoning.effort`）。選択肢は 4 つで、既定は low
# （推論が出力の上限を使い切って未完了のときは、送り直しで 1 段下げる。#2023）。
# none は「送らない」（`reasoning` を送らない。endpoint の既定のまま）で、それ以外はその値を送る。
# 解釈は `reasoning_effort_to_send` の 1 か所だけにし、3 製品はこれ
# （か `enterprise_ai_reasoning`）を使う。
# 受け付ける値はモデル（endpoint）で違うので、確かめるのはモデルの接続テストに任せる。
# 2026-10-11 の実測（短い同じ入力）:
# - claude-opus-5（ターシャリ接続）: none / minimal / low / medium / high
#   （と xhigh / max）を受け付ける
# - openai.gpt-oss-120b: low / medium / high（none・xhigh・max は 400、
#   minimal は 400「Harmony は未対応」）
# - xai.grok-4.3: minimal / low / medium / high（none・xhigh・max は 400）
# 選択肢は 3 モデルとも使える none / low / medium / high だけにする（none は送らないので、
# どのモデルでも使える。minimal は gpt-oss が受け付けないので持たない）。
EnterpriseAiReasoningEffort = Literal["none", "low", "medium", "high"]
ENTERPRISE_AI_REASONING_EFFORTS: Final[tuple[str, ...]] = (
    "none",
    "low",
    "medium",
    "high",
)
# 初期の既定（指定しないモデル・新しく登録するモデル）。#2966 の比較で、回答の生成は
# low が medium の約 2 倍速く、多くの問の品質は同じだった（#3004）。
# 役目ごとに上書きできる（RAG の根拠の評価は medium）。
DEFAULT_ENTERPRISE_AI_REASONING_EFFORT: Final = "low"
# 「送らない」の値。
ENTERPRISE_AI_REASONING_EFFORT_OMIT: Final = "none"
# モデルの入力の上限（tokens。#1437）。登録モデルごとに持ち、値が無いとき（#1437 より前の設定・
# env だけで設定したモデル）は既定の 128000（多くのモデルが受け付ける大きさで、上限の大きい
# モデルでも安全な側）。範囲は 4096〜2000000。入力の予算（回答の根拠・抽出の 1 回分）は
# `input_token_budget` で上限の割合として決め、本文の token は `estimate_input_tokens` で見積もる。
DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS: Final = 128_000
ENTERPRISE_AI_MAX_INPUT_TOKENS_MIN: Final = 4_096
ENTERPRISE_AI_MAX_INPUT_TOKENS_MAX: Final = 2_000_000
# モデルの出力の上限（tokens。#1719）。登録モデルごとに持ち、値が無いとき（#1719 より前の設定・
# env だけで設定したモデル）は既定の 32768（入力の上限がそれより小さいモデルは入力の上限）。
# 推論のあるモデルは推論の token も出力の上限に数えるため、表・列の多い Structured Outputs でも
# 足りる大きさにする（2026-10-13 の実測: openai.gpt-oss-120b・推論 medium は 5 表・32 列のコメントで
# 出力 2756 token（うち推論 1506）、上限 1200 では推論だけで上限に達して incomplete）。範囲は
# 1〜2000000（OCI の公式文書の context は xai.grok-4.3 が 1M、openai.gpt-oss-120b が 128k。
# 128k 以上を入れられるようにする）で、入力の上限（context）以下。
DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS: Final = 32_768
ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MIN: Final = 1
ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MAX: Final = 2_000_000
ModelSettingsTestStatus = Literal["success", "failed"]
ModelSettingsTestTargetType = Literal["enterprise_text", "enterprise_vision", "embedding", "rerank"]
ModelTestDetails = dict[str, str | int | float | bool | None]


# --------------------------------------------------------------------------- schemas


class EnterpriseAiConfiguredModel(BaseModel):
    """OCI Enterprise AI provider に登録する LLM。"""

    model_id: str = Field(default="", max_length=256)
    display_name: str = Field(default="", max_length=256)
    vision_enabled: bool = False
    # モデルの種類（#45）。text は LLM（画像対応は `vision_enabled`）、embedding は
    # `POST {endpoint}/embeddings`、rerank は `POST {endpoint}/reranks` で呼ぶ。未指定（#45 より前の
    # 設定）は text。
    kind: EnterpriseAiModelKind = ENTERPRISE_AI_MODEL_KIND_TEXT
    # 呼び出しに使う接続（#533）。未指定（#533 より前の設定）はプライマリ接続。
    # 存在しない接続を指す値は、保存時に欄のエラーにする（`validate_enterprise_ai_connections`）。
    connection_id: str = Field(default=ENTERPRISE_AI_PRIMARY_CONNECTION_ID, max_length=64)
    # 推論（思考）の量（#1425）。未指定（#1425 より前の設定も）は既定の low。
    # none は `reasoning` を送らない（`reasoning_effort_to_send`）。
    reasoning_effort: EnterpriseAiReasoningEffort = DEFAULT_ENTERPRISE_AI_REASONING_EFFORT
    # 入力の上限（tokens。#1437）。未指定（#1437 より前の設定も）は既定の 128000。
    # 範囲外は読み込み・保存とも検証エラー（`max_input_tokens_to_use` は env の直書きの値だけを
    # 既定に寄せる）。
    max_input_tokens: int = Field(
        default=DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS,
        ge=ENTERPRISE_AI_MAX_INPUT_TOKENS_MIN,
        le=ENTERPRISE_AI_MAX_INPUT_TOKENS_MAX,
    )
    # 出力の上限（tokens。#1719）。入力の上限（context）の中の応答の分で、推論のあるモデルは推論の
    # token も含む。未指定（#1719 より前の設定も）は既定の 32768（入力の上限がそれより小さければ
    # 入力の上限）。範囲外と、入力の上限を超える値は読み込み・保存とも検証エラー。
    max_output_tokens: int = Field(
        default=DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS,
        ge=ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MIN,
        le=ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MAX,
    )

    @model_validator(mode="before")
    @classmethod
    def default_max_output_tokens(cls, value: object) -> object:
        """出力の上限の値が無いモデルは、既定（32768）と入力の上限の小さい方にする（#1719）。"""
        if not isinstance(value, Mapping):
            return value
        current = value.get("max_output_tokens")
        if current is not None and not (isinstance(current, str) and not current.strip()):
            return value
        result = dict(value)
        result["max_output_tokens"] = min(
            DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS,
            max_input_tokens_to_use(result.get("max_input_tokens")),
        )
        return result

    @field_validator("kind", mode="before")
    @classmethod
    def default_kind(cls, value: object) -> object:
        """空・未指定は text にする。"""
        if value is None:
            return ENTERPRISE_AI_MODEL_KIND_TEXT
        if isinstance(value, str):
            return value.strip().casefold() or ENTERPRISE_AI_MODEL_KIND_TEXT
        return value

    @model_validator(mode="after")
    def vision_only_for_text(self) -> EnterpriseAiConfiguredModel:
        """画像対応はテキストのモデル（LLM）だけの属性（#45）。"""
        if self.kind != ENTERPRISE_AI_MODEL_KIND_TEXT:
            self.vision_enabled = False
        return self

    @model_validator(mode="after")
    def output_within_input(self) -> EnterpriseAiConfiguredModel:
        """出力の上限は入力の上限（prompt と応答の合計）以下にする（#1719）。"""
        if self.max_output_tokens > self.max_input_tokens:
            raise ValueError(
                "出力の上限は入力の上限（prompt と応答の合計）以下にしてください"
                f"（出力の上限 {self.max_output_tokens}・入力の上限 {self.max_input_tokens}）。"
            )
        return self

    @field_validator("model_id", "display_name")
    @classmethod
    def strip_text(cls, value: str) -> str:
        """前後空白を設定値へ混入させない。"""
        return value.strip()

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def normalize_reasoning_effort(cls, value: object) -> object:
        """前後空白・大文字を正規化する。None は未指定と同じ既定（low）にする。

        選択肢（none / low / medium / high）以外は、読み込み・保存とも検証エラーにする。
        """
        if value is None:
            return DEFAULT_ENTERPRISE_AI_REASONING_EFFORT
        if isinstance(value, str):
            return value.strip().casefold()
        return value

    @field_validator("max_input_tokens", mode="before")
    @classmethod
    def normalize_max_input_tokens(cls, value: object) -> object:
        """None・空は既定（128000）にし、真偽値は検証エラーにする（bool は int の部分型のため）。"""
        if value is None or (isinstance(value, str) and not value.strip()):
            return DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS
        if isinstance(value, bool):
            raise ValueError("入力の上限は整数で入力してください。")
        return value

    @field_validator("max_output_tokens", mode="before")
    @classmethod
    def reject_bool_max_output_tokens(cls, value: object) -> object:
        """真偽値は検証エラーにする（bool は int の部分型のため）。"""
        if isinstance(value, bool):
            raise ValueError("出力の上限は整数で入力してください。")
        return value

    @field_validator("connection_id", mode="before")
    @classmethod
    def default_connection(cls, value: object) -> object:
        """空・未指定はプライマリ接続にする。"""
        if value is None:
            return ENTERPRISE_AI_PRIMARY_CONNECTION_ID
        if isinstance(value, str):
            return value.strip() or ENTERPRISE_AI_PRIMARY_CONNECTION_ID
        return value


# API schema 上の名前（NL2SQL / RAG の既存名）。中身は同じ。
EnterpriseAiModelEntrySettings = EnterpriseAiConfiguredModel


class EnterpriseAiConnectionSettings(BaseModel):
    """OCI Enterprise AI の接続 1 件（#533）。`api_key` は書き込み専用で、応答では空にする。

    接続の表示名は持たない（#542。画面は ID から「プライマリ接続」などと表示する）。
    #533 の表示名（`display_name`）を含む payload も、知らない key として無視して受け付ける。
    """

    connection_id: EnterpriseAiConnectionId = ENTERPRISE_AI_PRIMARY_CONNECTION_ID
    endpoint: str = Field(default="", max_length=2048)
    project_ocid: str = Field(default="", max_length=512)
    api_key: str = Field(default="", max_length=4096)
    has_api_key: bool = False
    clear_api_key: bool = False

    @field_validator("endpoint", "project_ocid", "api_key")
    @classmethod
    def strip_text(cls, value: str) -> str:
        """前後空白を設定値へ混入させない。"""
        return value.strip()


# #533 より前の payload の接続の key（プライマリ接続の 1 組を top-level に持つ形）。
_LEGACY_CONNECTION_KEYS = ("endpoint", "project_ocid", "api_key", "has_api_key", "clear_api_key")


class EnterpriseAiModelSettings(BaseModel):
    """OCI Enterprise AI モデル provider 設定。"""

    # 1 件目（プライマリ接続）が既定。セカンダリ接続・ターシャリ接続は任意（#533 / #786）。
    connections: list[EnterpriseAiConnectionSettings] = Field(
        default_factory=lambda: [EnterpriseAiConnectionSettings()],
        min_length=1,
        max_length=MAX_ENTERPRISE_AI_CONNECTIONS,
    )
    models: list[EnterpriseAiConfiguredModel] = Field(default_factory=list, max_length=20)
    # 画像を扱わない処理の既定。モデルを 1 つ以上登録したら必須（#499 / #566）。
    default_text_model_id: str = Field(default="", max_length=256)
    # 画像を読む処理の既定。モデルを 1 つ以上登録したら必須で、Vision 対応のモデルに限る。
    default_vision_model_id: str = Field(default="", max_length=256)
    # 品質評価で回答を判定するモデル（#3574）。空 = 既定のテキストモデル、
    # `ENTERPRISE_AI_JUDGE_SAME_AS_ANSWER` = 回答を作ったモデル、それ以外は登録モデルの ID。
    default_judge_model_id: str = Field(default="", max_length=256)
    # 埋め込み / 再ランクに使うモデル（#45）。種類 embedding / rerank の登録モデルの ID。
    # その種類のモデルを登録したら必須。
    default_embedding_model_id: str = Field(default="", max_length=256)
    default_rerank_model_id: str = Field(default="", max_length=256)
    api_path: str = Field(default="/responses", max_length=512)
    vlm_input_mode: EnterpriseAiVlmInputMode = "auto"
    text_payload_template: str = Field(default="", max_length=20000)
    vision_payload_template: str = Field(default="", max_length=20000)
    text_response_path: str = Field(default="", max_length=1024)
    vision_response_path: str = Field(default="", max_length=1024)
    timeout_seconds: float = Field(default=600.0, gt=0.0, le=600.0)
    max_retries: int = Field(default=3, ge=0, le=5)

    @model_validator(mode="before")
    @classmethod
    def legacy_single_connection(cls, value: object) -> object:
        """#533 より前の payload（接続情報を top-level に 1 組）は、プライマリ接続とする。"""
        if not isinstance(value, Mapping) or "connections" in value:
            return value
        legacy = {key: value[key] for key in _LEGACY_CONNECTION_KEYS if key in value}
        if not legacy:
            return value
        converted = {key: item for key, item in value.items() if key not in legacy}
        converted["connections"] = [
            {"connection_id": ENTERPRISE_AI_PRIMARY_CONNECTION_ID, **legacy}
        ]
        return converted

    @field_validator("connections")
    @classmethod
    def validate_connection_order(
        cls, value: list[EnterpriseAiConnectionSettings]
    ) -> list[EnterpriseAiConnectionSettings]:
        """接続はプライマリ接続を先頭に「セカンダリ → ターシャリ」の順で、重複なく並べる。

        セカンダリ接続を置かずにターシャリ接続だけを置いてもよい（#786）。
        """
        positions = [
            ENTERPRISE_AI_CONNECTION_IDS.index(connection.connection_id) for connection in value
        ]
        if not positions or positions[0] != 0 or positions != sorted(set(positions)):
            raise ValueError(
                "接続はプライマリ接続（primary）を先頭に、セカンダリ接続（secondary）、"
                "ターシャリ接続（tertiary）の順で、重複なく指定してください。"
            )
        return value

    @field_validator(
        "default_text_model_id",
        "default_vision_model_id",
        "default_judge_model_id",
        "default_embedding_model_id",
        "default_rerank_model_id",
        "api_path",
        "text_payload_template",
        "vision_payload_template",
        "text_response_path",
        "vision_response_path",
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        """前後空白を設定値へ混入させない。"""
        return value.strip()

    def connection(self, connection_id: str) -> EnterpriseAiConnectionSettings | None:
        """指定した接続。なければ None。"""
        return next(
            (item for item in self.connections if item.connection_id == connection_id), None
        )

    @property
    def primary_connection(self) -> EnterpriseAiConnectionSettings:
        """プライマリ接続（既定）。"""
        return self.connections[0]

    @field_validator("text_payload_template", "vision_payload_template")
    @classmethod
    def validate_payload_template(cls, value: str) -> str:
        """payload template は空または JSON object 文字列だけを許可する。"""
        if not value:
            return value
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise ValueError("payload template は JSON object で入力してください。") from exc
        if not isinstance(parsed, dict):
            raise ValueError("payload template は JSON object で入力してください。")
        return value

    @field_validator("text_response_path", "vision_response_path")
    @classmethod
    def validate_response_path(cls, value: str) -> str:
        """response path は空または JSON Pointer 形式だけを許可する。"""
        if value and not value.startswith("/"):
            raise ValueError("response path は / で始まる JSON Pointer で入力してください。")
        return value


class _LegacyGenerativeAiSettings(BaseModel):
    """#45 より前の JSON の `generative_ai`（埋め込み / 再ランクの独立した節）。読み込み互換専用。

    読み込むときに、モデル ID を種類 `embedding` / `rerank` の登録モデル（プライマリ接続）と
    既定の埋め込み / 再ランクのモデルへ移す（`ModelSettingsStore._apply`）。Base URL・次元は
    読み捨てる（Base URL は接続の Endpoint URL に、次元は製品の設定 `oci_genai_embedding_dim`
    にした。#46 からは `EMBEDDING_DIMENSIONS` の 1024 に固定）。
    """

    embedding_model: str = ""
    rerank_model: str = ""

    @field_validator("embedding_model", "rerank_model", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ModelSettingsPayload(BaseModel):
    """モデル設定の読み書き payload。

    #45 で埋め込み / 再ランクの独立した節（`generative_ai`）を外した。埋め込み / 再ランクの
    モデルは登録モデル（種類 `embedding` / `rerank`）と既定のモデルで表す。`generative_ai` を
    含む古い request は、知らない key として読み捨てる。
    """

    enterprise_ai: EnterpriseAiModelSettings


class ModelSettingsUpdateRequest(ModelSettingsPayload):
    """モデル設定の保存（PATCH）の request（#1037）。

    `base_revision` は画面が読み込んだ時点の `ModelSettingsData.revision`。送られたときは、
    保存の直前の `revision` と違えば 409 にして保存しない（別の製品・タブの保存を古い値で
    上書きしない）。
    送らない呼び出しは従来どおり保存する。
    """

    base_revision: str | None = Field(default=None, max_length=128)


class ModelSettingsData(BaseModel):
    """モデル設定 API のレスポンス data。"""

    settings: ModelSettingsPayload
    model_settings_file: str
    source: Literal["runtime"]
    secret_source: ModelSettingsSecretSource
    legacy_secret_detected: bool = False
    # 保存済みのモデル設定の版（#1037）。保存の `base_revision` に返す。
    revision: str = ""
    # 埋め込みの次元（#45。読み取り専用の表示用）。製品の設定 `oci_genai_embedding_dim`（Oracle の
    # VECTOR 列に合わせた値）で、埋め込みの呼び出しに `dimensions` として送る。
    embedding_dim: int | None = None


class ModelSettingsTestRequest(BaseModel):
    """保存前のモデル設定で特定モデルを実 API に対してテストする request。"""

    settings: ModelSettingsPayload
    target_type: ModelSettingsTestTargetType
    model_id: str = Field(default="", max_length=256)
    vision_enabled: bool = False

    @field_validator("model_id")
    @classmethod
    def strip_model_id(cls, value: str) -> str:
        """前後空白を設定値へ混入させない。"""
        return value.strip()


class ModelSettingsTestResult(BaseModel):
    """モデル単位の実接続テスト結果。"""

    status: ModelSettingsTestStatus
    target_type: ModelSettingsTestTargetType
    model_id: str
    message: str
    troubleshooting: list[str] = Field(default_factory=list)
    raw_error: str | None = None
    error_type: str | None = None
    elapsed_ms: int
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    details: ModelTestDetails = Field(default_factory=dict)


# --------------------------------------------------------------------------- secret state


class ModelSecretStateMixin(BaseModel):
    """製品の Settings に混ぜる、Enterprise AI API key の取得元の状態。

    `class Settings(ModelSecretStateMixin, BaseSettings)` のように先頭に置く。
    セカンダリ接続・ターシャリ接続の API key（#533 / #786）は `.env` / 環境変数だけから読む
    （旧 JSON の互換はない）。
    """

    _environment_enterprise_ai_api_key: str = PrivateAttr(default="")
    # セカンダリ接続・ターシャリ接続の API key の基準値（接続 ID → key）。
    _environment_enterprise_ai_connection_api_keys: dict[str, str] = PrivateAttr(
        default_factory=dict
    )
    _model_secret_source: ModelSettingsSecretSource = PrivateAttr(default="missing")
    _legacy_model_secret_detected: bool = PrivateAttr(default=False)
    _model_secret_state_initialized: bool = PrivateAttr(default=False)

    @property
    def model_secret_source(self) -> ModelSettingsSecretSource:
        """Enterprise AI API key（プライマリ接続）の実効的な取得元。"""
        return self._model_secret_source

    @property
    def legacy_model_secret_detected(self) -> bool:
        """model-settings.json に旧 secret field が残っているか。"""
        return self._legacy_model_secret_detected

    def prepare_model_secret_state(
        self,
        environment_api_key: str | None = None,
        environment_secondary_api_key: str | None = None,
        environment_tertiary_api_key: str | None = None,
    ) -> None:
        """JSON 再読込前に環境由来 secret を基準値へ戻す。

        値を渡すと新しい基準値にする（別 worker が `.env` を更新した場合）。
        """
        environment_keys = {
            ENTERPRISE_AI_SECONDARY_CONNECTION_ID: environment_secondary_api_key,
            ENTERPRISE_AI_TERTIARY_CONNECTION_ID: environment_tertiary_api_key,
        }
        if not self._model_secret_state_initialized:
            self._environment_enterprise_ai_api_key = _api_key_of(self).strip()
            self._environment_enterprise_ai_connection_api_keys = {
                connection_id: _setting_text(self, _CONNECTION_FIELDS[connection_id].api_key)
                for connection_id in _OPTIONAL_CONNECTION_IDS
            }
            self._model_secret_state_initialized = True
        if environment_api_key is not None:
            self._environment_enterprise_ai_api_key = environment_api_key.strip()
        for connection_id, environment_key in environment_keys.items():
            if environment_key is not None:
                self._environment_enterprise_ai_connection_api_keys[connection_id] = (
                    environment_key.strip()
                )
        self._set_api_key(self._environment_enterprise_ai_api_key)
        for connection_id in _OPTIONAL_CONNECTION_IDS:
            _set_if_field(
                self,
                _CONNECTION_FIELDS[connection_id].api_key,
                self._environment_enterprise_ai_connection_api_keys.get(connection_id, ""),
            )
        self._model_secret_source = (
            "environment" if self._environment_enterprise_ai_api_key else "missing"
        )
        self._legacy_model_secret_detected = False

    def set_runtime_enterprise_ai_api_key(self, api_key: str) -> None:
        """`.env` 更新後の secret 状態（プライマリ接続）を現在プロセスへ反映する。"""
        normalized = api_key.strip()
        self._environment_enterprise_ai_api_key = normalized
        self._model_secret_state_initialized = True
        self._set_api_key(normalized)
        self._model_secret_source = "environment" if normalized else "missing"
        self._legacy_model_secret_detected = False

    def apply_legacy_enterprise_ai_api_key(self, api_key: str, *, detected: bool) -> None:
        """旧 JSON の secret を、環境に key がないときだけ一時的に runtime へ適用する。"""
        normalized = api_key.strip()
        self._legacy_model_secret_detected = detected
        if not self._environment_enterprise_ai_api_key and normalized:
            self._set_api_key(normalized)
            self._model_secret_source = "legacy_json"  # nosec B105

    def mark_legacy_model_secret_detected(self) -> None:
        """製品固有の節（parser など）の secret が旧 JSON に残っていることを記録する。"""
        self._legacy_model_secret_detected = True

    def _set_api_key(self, value: str) -> None:
        # Settings 側のフィールド（mixin 自体は持たない）。
        setattr(self, "oci_enterprise_ai_api_key", value)  # noqa: B010


def _api_key_of(settings: Any) -> str:
    return str(getattr(settings, "oci_enterprise_ai_api_key", "") or "")


# --------------------------------------------------------------------------- connections


@dataclass(frozen=True)
class _ConnectionFields:
    """接続ごとの Settings の属性名と、API key の env 名。"""

    endpoint: str
    project_ocid: str
    api_key: str
    api_key_env: str


# プライマリ接続は #533 より前からの属性・env 名（既存環境の更新は要らない）。セカンダリ接続は
# `PLATFORM_OCI_ENTERPRISE_AI_SECONDARY_*`、ターシャリ接続は `PLATFORM_OCI_ENTERPRISE_AI_TERTIARY_*`
# （どちらも `PLATFORM_SETTING_FIELDS` に登録）。
_PRIMARY_FIELDS = _ConnectionFields(
    endpoint="oci_enterprise_ai_endpoint",
    project_ocid="oci_enterprise_ai_project_ocid",
    api_key="oci_enterprise_ai_api_key",
    api_key_env=ENTERPRISE_AI_API_KEY_ENV,
)
_SECONDARY_FIELDS = _ConnectionFields(
    endpoint="oci_enterprise_ai_secondary_endpoint",
    project_ocid="oci_enterprise_ai_secondary_project_ocid",
    api_key="oci_enterprise_ai_secondary_api_key",
    api_key_env=ENTERPRISE_AI_SECONDARY_API_KEY_ENV,
)
_TERTIARY_FIELDS = _ConnectionFields(
    endpoint="oci_enterprise_ai_tertiary_endpoint",
    project_ocid="oci_enterprise_ai_tertiary_project_ocid",
    api_key="oci_enterprise_ai_tertiary_api_key",
    api_key_env=ENTERPRISE_AI_TERTIARY_API_KEY_ENV,
)
_CONNECTION_FIELDS: dict[str, _ConnectionFields] = {
    ENTERPRISE_AI_PRIMARY_CONNECTION_ID: _PRIMARY_FIELDS,
    ENTERPRISE_AI_SECONDARY_CONNECTION_ID: _SECONDARY_FIELDS,
    ENTERPRISE_AI_TERTIARY_CONNECTION_ID: _TERTIARY_FIELDS,
}


@dataclass(frozen=True)
class EnterpriseAiConnection:
    """モデルの呼び出しに使う OCI Enterprise AI の接続（#533）。

    API key を含むので、API の応答やログに出さない。
    """

    connection_id: str
    endpoint: str
    project_ocid: str
    api_key: str = field(repr=False)

    def is_configured(self) -> bool:
        """Endpoint と API key がそろっているか（Project OCID は任意）。"""
        return bool(self.endpoint and self.api_key)

    @property
    def base_url(self) -> str:
        """OpenAI 互換 API の base URL（`enterprise_ai_base_url`。#1440）。"""
        return enterprise_ai_base_url(self.endpoint)

    def request_url(self, api_path: str = DEFAULT_ENTERPRISE_AI_API_PATH) -> str:
        """API を呼ぶ URL（`enterprise_ai_request_url`。#1440）。"""
        return enterprise_ai_request_url(self.endpoint, api_path)


def _connection_from_settings(settings: Any, connection_id: str) -> EnterpriseAiConnection:
    fields = _CONNECTION_FIELDS[connection_id]
    return EnterpriseAiConnection(
        connection_id=connection_id,
        endpoint=_setting_text(settings, fields.endpoint),
        project_ocid=_setting_text(settings, fields.project_ocid),
        api_key=_setting_text(settings, fields.api_key),
    )


def enterprise_ai_connections(settings: Any) -> list[EnterpriseAiConnection]:
    """設定済みの接続の一覧。

    プライマリ接続は常にあり、セカンダリ接続・ターシャリ接続は Endpoint URL があるときだけある。
    """
    connections = [_connection_from_settings(settings, ENTERPRISE_AI_PRIMARY_CONNECTION_ID)]
    for connection_id in _OPTIONAL_CONNECTION_IDS:
        connection = _connection_from_settings(settings, connection_id)
        if connection.endpoint:
            connections.append(connection)
    return connections


def enterprise_ai_connection(settings: Any, connection_id: str) -> EnterpriseAiConnection:
    """指定した接続。ない接続（保存後に env から消した場合など）はプライマリ接続を使う。"""
    connections = enterprise_ai_connections(settings)
    for connection in connections:
        if connection.connection_id == connection_id:
            return connection
    if connection_id != ENTERPRISE_AI_PRIMARY_CONNECTION_ID:
        logger.warning(
            "enterprise_ai_connection_missing",
            extra={"warning_code": "ENTERPRISE_AI_CONNECTION_MISSING", "connection": connection_id},
        )
    return connections[0]


def embedding_model_connection(settings: Any, *, rerank: bool = False) -> EnterpriseAiConnection:
    """埋め込み（`rerank=True` は再ランク）のモデルを呼ぶ接続（#45）。

    既定の埋め込み / 再ランクのモデル（Settings の `oci_genai_embedding_model` /
    `oci_genai_rerank_model`）を登録モデルの同じ種類から引き、その接続を使う。登録に無い
    モデル（`.env` だけで設定した場合）はプライマリ接続。3 製品と rag_engine の埋め込み /
    再ランクの endpoint・API key はすべてこれで引く。
    """
    kind = ENTERPRISE_AI_MODEL_KIND_RERANK if rerank else ENTERPRISE_AI_MODEL_KIND_EMBEDDING
    model_id = _setting_text(
        settings, "oci_genai_rerank_model" if rerank else "oci_genai_embedding_model"
    )
    connection_id = next(
        (
            model.connection_id
            for model in enterprise_ai_registered_models(settings)
            if model.kind == kind and model.model_id == model_id
        ),
        ENTERPRISE_AI_PRIMARY_CONNECTION_ID,
    )
    return enterprise_ai_connection(settings, connection_id)


def rerank_base_url(endpoint: str) -> str:
    """再ランクの base URL（#8）。Endpoint URL の `/compatible-mode/` を `/compatible-api/` に。

    百煉（DashScope）は chat・埋め込みが `/compatible-mode/v1`、**再ランクだけ**
    `/compatible-api/v1` 配下で、`/compatible-mode/v1/reranks` は 404 になる。`/compatible-mode/` を
    含まない endpoint はそのまま使う。
    """
    return enterprise_ai_base_url(endpoint).replace("/compatible-mode/", "/compatible-api/")


def embedding_api_base_url(settings: Any, *, rerank: bool = False) -> str:
    """埋め込み / 再ランクの OpenAI 互換 base URL（#8 / #45）。決まらなければ空文字。

    `embedding_model_connection` の接続の Endpoint URL。再ランクは `rerank_base_url` で読み替える。
    """
    endpoint = embedding_model_connection(settings, rerank=rerank).endpoint
    return rerank_base_url(endpoint) if rerank else enterprise_ai_base_url(endpoint)


class EnterpriseAiModelNotRegisteredError(ValueError):
    """明示したモデルがモデル設定に登録されていない（#3240）。

    別のモデル・接続へ暗黙に切り替えずに、呼び出しの前に失敗させる。文はそのまま利用者に見せてよい
    （モデル ID だけを含み、secret を含まない）。
    """

    code: Final = "enterprise_ai_model_not_registered"
    safe_for_user: Final = True

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        super().__init__(
            f"モデル「{model_id}」はシステム設定 > モデル に登録されていません。"
            "モデルを登録するか、登録済みのモデルを選んでください。"
        )


def enterprise_ai_connection_for_model(settings: Any, model_id: str) -> EnterpriseAiConnection:
    """モデルを呼ぶときに使う接続（#533）。3 製品の呼び出し・接続テストはすべてこれで引く。

    - 空の ID は既定のテキストモデル（`enterprise_ai_default_model_id`）の接続。
    - 登録モデルの ID はそのモデルの接続。
    - 既定のテキスト / Vision モデルの ID は、登録モデルに無くてもプライマリ接続（`.env` だけで
      既定のモデルを設定した環境）。
    - それ以外（登録していない・登録を消したモデル）は `EnterpriseAiModelNotRegisteredError`。
      プライマリ接続へ暗黙に切り替えない（#3240 の不変条件 2）。
    """
    normalized = model_id.strip() or enterprise_ai_default_model_id(settings)
    catalog = enterprise_ai_model_catalog(settings)
    connection_id = next(
        (model.connection_id for model in catalog if model.model_id == normalized), None
    )
    if connection_id is None:
        defaults = {
            _setting_text(settings, "oci_enterprise_ai_default_text_model"),
            _setting_text(settings, "oci_enterprise_ai_default_vision_model"),
        }
        if normalized and normalized not in defaults:
            raise EnterpriseAiModelNotRegisteredError(normalized)
        connection_id = ENTERPRISE_AI_PRIMARY_CONNECTION_ID
    return enterprise_ai_connection(settings, connection_id)


def enterprise_ai_reasoning_effort(settings: Any, model_id: str) -> str:
    """モデルに設定した推論（思考）の量（#1425。none / low / medium / high）。

    登録モデルにない ID（env だけで設定した場合など）は、登録モデルの既定と同じ medium。
    送る値にするときは `reasoning_effort_to_send` を通す（none は送らない）。
    """
    normalized = model_id.strip()
    return next(
        (
            model.reasoning_effort
            for model in enterprise_ai_model_catalog(settings)
            if model.model_id == normalized
        ),
        DEFAULT_ENTERPRISE_AI_REASONING_EFFORT,
    )


def reasoning_effort_to_send(effort: str) -> str:
    """設定の値を、Responses API の `reasoning.effort` に送る値にする（#1425。解釈はここだけ）。

    none（送らない）と空は ""（`reasoning` を送らない）、それ以外はその値。
    """
    normalized = (effort or "").strip().casefold()
    if not normalized or normalized == ENTERPRISE_AI_REASONING_EFFORT_OMIT:
        return ""
    return normalized


# 出力の上限で未完了になった呼び出しを送り直すときに 1 段下げる推論の量（#2023）。
# 業界の型（まず出力の上限を広げ、それでも終わらなければ推論を減らす）に合わせ、
# high → medium → low。low は下げない: 下の none は「送らない」（endpoint の既定。
# gpt-oss は medium）で、推論が増えることがある。
# minimal は gpt-oss が 400 にするので使わない。
REASONING_EFFORT_STEP_DOWN: Final[Mapping[str, str]] = {"high": "medium", "medium": "low"}


def lower_reasoning_effort(effort: str) -> str:
    """推論の量を 1 段下げた、送る値（#2023）。下げられない（low・none・空）ときは ""。"""
    return REASONING_EFFORT_STEP_DOWN.get(reasoning_effort_to_send(effort), "")


def enterprise_ai_reasoning_effort_to_send(settings: Any, model_id: str) -> str:
    """モデルの推論の量を送る値で返す（""は送らない）。env・config で値を渡す部品に使う。"""
    return reasoning_effort_to_send(enterprise_ai_reasoning_effort(settings, model_id))


def reasoning_payload(effort: str) -> dict[str, str] | None:
    """設定の値から Responses API の `reasoning` を作る（送らないなら None）。"""
    to_send = reasoning_effort_to_send(effort)
    return {"effort": to_send} if to_send else None


def enterprise_ai_reasoning(settings: Any, model_id: str) -> dict[str, str] | None:
    """モデルの Responses API の `reasoning`（`{"effort": ...}`）。送らないときは None（#1425）。"""
    return reasoning_payload(enterprise_ai_reasoning_effort(settings, model_id))


def max_input_tokens_to_use(value: object) -> int:
    """設定の値を、使う入力の上限（tokens）にする（#1437。解釈はここだけ）。

    整数で範囲（4096〜2000000）の中ならその値、値なし・範囲外・整数でない値は既定の 128000。
    保存・読み込みは `EnterpriseAiConfiguredModel` が検証するので、ここで既定に戻すのは
    env の直書きなどの値だけ。
    """
    if isinstance(value, bool) or value is None:
        return DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS
    if not ENTERPRISE_AI_MAX_INPUT_TOKENS_MIN <= number <= ENTERPRISE_AI_MAX_INPUT_TOKENS_MAX:
        return DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS
    return number


def enterprise_ai_max_input_tokens(settings: Any, model_id: str) -> int:
    """モデルに設定した入力の上限（tokens。#1437）。

    登録モデルにない ID（env だけで設定した場合など）は、登録モデルの既定と同じ 128000。
    """
    normalized = model_id.strip()
    return next(
        (
            max_input_tokens_to_use(model.max_input_tokens)
            for model in enterprise_ai_model_catalog(settings)
            if model.model_id == normalized
        ),
        DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS,
    )


def input_token_budget(max_input_tokens: object, percent: object) -> int:
    """入力の上限に対する割合（%）を、入力の予算（tokens）にする（#1437）。

    割合は 1〜100 に収め（値が無い・数でなければ 50）、上限は `max_input_tokens_to_use` で解釈する。
    指示・出力の余地を残すため、呼び出し側は 100% 未満を渡す（回答の根拠は 50%、
    抽出の 1 回分は 60%）。
    """
    limit = max_input_tokens_to_use(max_input_tokens)
    try:
        share = int(float(str(percent).strip())) if not isinstance(percent, bool) else 50
    except (TypeError, ValueError):
        share = 50
    share = max(1, min(100, share))
    return max(1, limit * share // 100)


def max_output_tokens_to_use(value: object) -> int:
    """設定の値を、使う出力の上限（tokens）にする（#1719。解釈はここだけ）。

    整数で範囲（1〜2000000）の中ならその値、値なし・範囲外・整数でない値は既定の 32768。
    保存・読み込みは `EnterpriseAiConfiguredModel` が検証するので、ここで既定に戻すのは
    env の直書きなどの値だけ。
    """
    if isinstance(value, bool) or value is None:
        return DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS
    if not ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MIN <= number <= ENTERPRISE_AI_MAX_OUTPUT_TOKENS_MAX:
        return DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS
    return number


def enterprise_ai_max_output_tokens(settings: Any, model_id: str) -> int:
    """モデルに設定した出力の上限（tokens。#1719）。入力の上限を超えない。

    登録モデルにない ID（env だけで設定した場合など）は、登録モデルの既定と同じ 32768。
    """
    normalized = model_id.strip()
    limit = next(
        (
            max_output_tokens_to_use(model.max_output_tokens)
            for model in enterprise_ai_model_catalog(settings)
            if model.model_id == normalized
        ),
        DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS,
    )
    return min(limit, enterprise_ai_max_input_tokens(settings, model_id))


def output_token_budget(
    *,
    max_input_tokens: object,
    max_output_tokens: object,
    prompt_tokens: int = 0,
    requested: int | None = None,
) -> int:
    """1 回の呼び出しの `max_output_tokens`（#1719。値の解釈はここだけ）。

    - 機能が要る量（`requested`）を渡したときは、既定（32768）を下回らない（推論のあるモデルは
      推論の token も数えるため、小さい上限では Structured Outputs が未完了になる）
    - モデルの出力の上限を超えない
    - 入力の上限（prompt と応答の合計）から prompt の見積もりを引いた残りを超えない
      （出力の上限の大きいモデルで prompt が入らずに失敗するのを防ぐ）
    """
    model_limit = min(
        max_output_tokens_to_use(max_output_tokens), max_input_tokens_to_use(max_input_tokens)
    )
    room = max_input_tokens_to_use(max_input_tokens) - max(0, int(prompt_tokens))
    wanted = (
        model_limit
        if requested is None
        else max(int(requested), min(DEFAULT_ENTERPRISE_AI_MAX_OUTPUT_TOKENS, model_limit))
    )
    return max(1, min(wanted, model_limit, room))


def enterprise_ai_output_token_budget(
    settings: Any, model_id: str, *, prompt_tokens: int = 0, requested: int | None = None
) -> int:
    """モデルの 1 回の呼び出しの `max_output_tokens`（#1719。`output_token_budget` を参照）。"""
    return output_token_budget(
        max_input_tokens=enterprise_ai_max_input_tokens(settings, model_id),
        max_output_tokens=enterprise_ai_max_output_tokens(settings, model_id),
        prompt_tokens=prompt_tokens,
        requested=requested,
    )


def enterprise_ai_retry_output_token_budget(
    settings: Any, model_id: str, *, current: int, prompt_tokens: int = 0
) -> int | None:
    """出力の上限で未完了になった呼び出しを送り直すときの上限（#1719）。

    モデルの出力の上限（と入力の上限の残り）まで広げる。今の上限より広げられなければ None
    （同じ上限で送り直しても終わらないので、送り直さない）。
    """
    widened = enterprise_ai_output_token_budget(settings, model_id, prompt_tokens=prompt_tokens)
    return widened if widened > int(current) else None


# --------------------------------------------------------------------------- catalog


def _coerce_model(value: object) -> EnterpriseAiConfiguredModel:
    if isinstance(value, EnterpriseAiConfiguredModel):
        return value
    if isinstance(value, BaseModel):
        value = value.model_dump()
    return EnterpriseAiConfiguredModel.model_validate(value)


def _configured_models(settings: Any) -> list[EnterpriseAiConfiguredModel]:
    return [
        model
        for model in (
            _coerce_model(item) for item in getattr(settings, "oci_enterprise_ai_models", [])
        )
        if model.model_id
    ]


def enterprise_ai_registered_models(settings: Any) -> list[EnterpriseAiConfiguredModel]:
    """登録モデルの一覧（#45。種類を問わない）。一覧がなければ既定のモデルの設定から補う。

    画面の登録モデルの一覧と、埋め込み / 再ランクの接続の解決に使う。LLM の選択肢は
    `enterprise_ai_model_catalog`（種類 text だけ）。
    """
    return _configured_models(settings) or enterprise_ai_model_catalog(settings)


def enterprise_ai_model_catalog(settings: Any) -> list[EnterpriseAiConfiguredModel]:
    """LLM（種類 text）の登録モデル一覧を返す。一覧がなければ既定のモデルの設定から補う。

    3 製品の LLM の選択肢・接続の解決はこれを使う。埋め込み / 再ランクのモデル（#45）は含めない。
    """
    configured = [
        model
        for model in _configured_models(settings)
        if model.kind == ENTERPRISE_AI_MODEL_KIND_TEXT
    ]
    if configured:
        return configured
    text_model = _setting_text(settings, "oci_enterprise_ai_default_text_model")
    vision_model = _setting_text(settings, "oci_enterprise_ai_default_vision_model")
    models: list[EnterpriseAiConfiguredModel] = []
    if text_model:
        models.append(
            EnterpriseAiConfiguredModel(
                model_id=text_model,
                display_name=text_model,
                vision_enabled=bool(vision_model and vision_model == text_model),
            )
        )
    if vision_model and vision_model != text_model:
        models.append(
            EnterpriseAiConfiguredModel(
                model_id=vision_model, display_name=vision_model, vision_enabled=True
            )
        )
    return models


def _setting_text(settings: Any, name: str) -> str:
    return str(getattr(settings, name, "") or "").strip()


def enterprise_ai_vision_model_id(settings: Any) -> str:
    """画像を読む呼び出し（Vision/OCR）で使うモデル ID（既定の Vision モデル）を返す。

    明示の設定がない環境（`.env` で登録モデルだけを書いた場合など）では、登録モデルのうち
    Vision 対応のもの（既定のテキストモデルが Vision 対応ならそれ）を使う。
    """
    explicit = _setting_text(settings, "oci_enterprise_ai_default_vision_model")
    if explicit:
        return explicit
    return _vision_model_id(
        enterprise_ai_model_catalog(settings),
        _setting_text(settings, "oci_enterprise_ai_default_text_model"),
    )


def enterprise_ai_judge_model_id(settings: Any, answer_model_id: str | None = None) -> str:
    """品質評価で回答を判定するモデル ID（#3574。既定の判定モデルの解決はここだけ）。

    - 既定の判定モデルが空（既定）: 既定のテキストモデル（`enterprise_ai_default_model_id`）
    - 「回答と同じモデル」（`ENTERPRISE_AI_JUDGE_SAME_AS_ANSWER`）: ``answer_model_id``
      （回答を作ったモデル）。分からなければ既定のテキストモデル
    - 登録モデルの ID: そのモデル（すべての回答を同じモデルで判定する）
    """
    judge = _setting_text(settings, "oci_enterprise_ai_default_judge_model")
    if judge == ENTERPRISE_AI_JUDGE_SAME_AS_ANSWER:
        return (answer_model_id or "").strip() or enterprise_ai_default_model_id(settings)
    return judge or enterprise_ai_default_model_id(settings)


def enterprise_ai_default_model_id(settings: Any) -> str:
    """画像を扱わない呼び出しで使うモデル ID を返す。

    既定のテキストモデル → 既定の Vision モデル → 登録モデルの先頭 の順に決める（#499）。
    画面・API では既定のテキストモデルは必須（#566）。Vision・先頭への代替は、`.env` だけで
    構成した既存環境が設定を保存し直すまで動き続けるための安全策として残す。
    """
    text_model = _setting_text(settings, "oci_enterprise_ai_default_text_model")
    if text_model:
        return text_model
    vision_model = enterprise_ai_vision_model_id(settings)
    if vision_model:
        return vision_model
    catalog = enterprise_ai_model_catalog(settings)
    return catalog[0].model_id if catalog else ""


def _vision_model_id(models: Sequence[EnterpriseAiConfiguredModel], default_model: str) -> str:
    """Vision/OCR 用 model を default 優先で選ぶ（明示の指定がない環境・旧 JSON の補完）。"""
    for model in models:
        if model.model_id == default_model and model.vision_enabled:
            return model.model_id
    for model in models:
        if model.model_id and model.vision_enabled:
            return model.model_id
    return ""


@dataclass(frozen=True)
class ModelFieldError:
    """保存前の検証で見つかった入力欄ごとのエラー。

    `field` は payload の key。一覧の中の欄は `connections.1.endpoint` /
    `models.0.connection_id` のように添字を付ける。
    """

    field: str
    message: str


# 利用者に見せる接続の名前（#542）。画面のタブ・登録モデルの「接続」の選択肢・メッセージで
# 同じ語を使う。
ENTERPRISE_AI_CONNECTION_LABELS: Final[Mapping[str, str]] = {
    ENTERPRISE_AI_PRIMARY_CONNECTION_ID: "プライマリ接続",
    ENTERPRISE_AI_SECONDARY_CONNECTION_ID: "セカンダリ接続",
    ENTERPRISE_AI_TERTIARY_CONNECTION_ID: "ターシャリ接続",
}


def connection_label(connection_id: str) -> str:
    """利用者に見せる接続の名前（「プライマリ接続」など。知らない ID はプライマリ接続）。"""
    return ENTERPRISE_AI_CONNECTION_LABELS.get(
        connection_id, ENTERPRISE_AI_CONNECTION_LABELS[ENTERPRISE_AI_PRIMARY_CONNECTION_ID]
    )


def validate_enterprise_ai_connections(
    enterprise: EnterpriseAiModelSettings,
    api_keys: Mapping[str, str] | None = None,
) -> list[ModelFieldError]:
    """接続と、登録モデルが指す接続を確かめる（#533 / #542）。

    - セカンダリ接続・ターシャリ接続を設定したら、Endpoint URL・API key は必須。
      Project OCID はどの接続でも任意（#786 / #9。OCI Enterprise AI に使うときだけ入れる。
      入力したときだけ `OpenAI-Project` header で送るので、OCI 以外の
      OpenAI 互換 API では空で足りる）。
      API key は保存後の値（`resolve_api_keys` の結果。空欄は保存済みの値を保持）で確かめるので、
      `api_keys` を渡したときだけ確かめる
    - プライマリ接続は OCI で運用するときだけ必須（画面は「OCI 運用時必須」）なので、
      ここでは確かめない
    - 登録モデルは、存在する接続を指す
    """
    errors: list[ModelFieldError] = []
    for index, connection in enumerate(enterprise.connections):
        if connection.connection_id == ENTERPRISE_AI_PRIMARY_CONNECTION_ID:
            continue
        label = connection_label(connection.connection_id)
        required = [("endpoint", "Endpoint URL", bool(connection.endpoint))]
        if api_keys is not None:
            required.append(
                ("api_key", "API key", bool(api_keys.get(connection.connection_id, "")))
            )
        errors.extend(
            ModelFieldError(
                f"connections.{index}.{field_name}",
                f"{label}の {field_label} を入力してください。",
            )
            for field_name, field_label, present in required
            if not present
        )
    available = {connection.connection_id for connection in enterprise.connections}
    for index, model in enumerate(enterprise.models):
        if model.model_id and model.connection_id not in available:
            errors.append(
                ModelFieldError(
                    f"models.{index}.connection_id",
                    f"「{model.display_name or model.model_id}」の接続がありません。"
                    "登録されている接続から選び直してください。",
                )
            )
    return errors


def validate_model_ids(enterprise: EnterpriseAiModelSettings) -> list[ModelFieldError]:
    """登録モデルのモデル ID が重複しないかを確かめる（#1035）。

    既定のモデル・接続の解決（`enterprise_ai_connection_for_model`）・接続テストはモデル ID で
    先頭の行を引くので、同じ ID の 2 行目以降は使われず、その行に選んだ接続も効かない。
    エラーは 2 回目以降に現れた行に付ける。
    """
    seen: set[str] = set()
    errors: list[ModelFieldError] = []
    for index, model in enumerate(enterprise.models):
        if not model.model_id:
            continue
        if model.model_id in seen:
            errors.append(
                ModelFieldError(
                    f"models.{index}.model_id",
                    f"モデル ID「{model.model_id}」はすでに登録されています。"
                    "同じモデルは 1 行にまとめてください。",
                )
            )
        seen.add(model.model_id)
    return errors


def validate_default_models(enterprise: EnterpriseAiModelSettings) -> list[ModelFieldError]:
    """既定のモデルが登録モデルと矛盾しないかを確かめる（#499）。

    - モデルを 1 つ以上登録したら、既定の Vision モデルは必須で、Vision 対応のモデルに限る
    - 既定のテキストモデルも、モデルを 1 つ以上登録したら必須（#566）。登録したモデルのどれか
      （Vision 対応でもよい）
    - 既定の判定モデル（#3574）は任意。空・「回答と同じモデル」以外は登録したモデルのどれか
    - テキスト・Vision・判定は種類 text のモデル、既定の埋め込み / 再ランクのモデル（#45）は
      それぞれ種類 embedding / rerank のモデル。埋め込み / 再ランクは、その種類のモデルを
      登録したら必須（登録しないときは `.env` の値のまま）
    エラーは画面の並び順（テキスト → Vision → 判定 → 埋め込み → 再ランク）で返す。
    """
    errors = _validate_text_default_models(enterprise)
    for kind, field_name, label in (
        (ENTERPRISE_AI_MODEL_KIND_EMBEDDING, "default_embedding_model_id", "埋め込みモデル"),
        (ENTERPRISE_AI_MODEL_KIND_RERANK, "default_rerank_model_id", "再ランクモデル"),
    ):
        of_kind = {
            model.model_id for model in enterprise.models if model.model_id and model.kind == kind
        }
        value = str(getattr(enterprise, field_name))
        if of_kind and not value:
            errors.append(ModelFieldError(field_name, f"既定の{label}を選択してください。"))
        elif of_kind and value not in of_kind:
            errors.append(
                ModelFieldError(
                    field_name,
                    f"既定の{label}「{value}」は登録モデルにありません。"
                    "種類が合う登録モデルから選び直してください。",
                )
            )
    return errors


def _validate_text_default_models(enterprise: EnterpriseAiModelSettings) -> list[ModelFieldError]:
    """既定のテキスト / Vision / 判定のモデル（種類 text の登録モデル）を確かめる。"""
    models = [
        model
        for model in enterprise.models
        if model.model_id and model.kind == ENTERPRISE_AI_MODEL_KIND_TEXT
    ]
    registered = {model.model_id for model in models}
    vision_capable = {model.model_id for model in models if model.vision_enabled}
    errors: list[ModelFieldError] = []
    text = enterprise.default_text_model_id
    if models and not text:
        errors.append(
            ModelFieldError("default_text_model_id", "既定のテキストモデルを選択してください。")
        )
    elif text and text not in registered:
        errors.append(
            ModelFieldError(
                "default_text_model_id",
                f"既定のテキストモデル「{text}」は登録モデルにありません。"
                "登録モデルから選び直してください。",
            )
        )
    vision = enterprise.default_vision_model_id
    if models and not vision_capable:
        errors.append(
            ModelFieldError(
                "default_vision_model_id",
                "画像入力に対応したモデルがありません。"
                "登録モデルの 1 つ以上で「画像入力に対応」をオンにしてください。",
            )
        )
    elif models and not vision:
        errors.append(
            ModelFieldError("default_vision_model_id", "既定の画像対応モデルを選択してください。")
        )
    elif vision and vision not in registered:
        errors.append(
            ModelFieldError(
                "default_vision_model_id",
                f"既定の画像対応モデル「{vision}」は登録モデルにありません。"
                "登録モデルから選び直してください。",
            )
        )
    elif vision and vision not in vision_capable:
        errors.append(
            ModelFieldError(
                "default_vision_model_id",
                f"「{vision}」は画像入力に対応していません。"
                "対応をオンにするか、別のモデルを選んでください。",
            )
        )
    judge = enterprise.default_judge_model_id
    if judge and judge != ENTERPRISE_AI_JUDGE_SAME_AS_ANSWER and judge not in registered:
        errors.append(
            ModelFieldError(
                "default_judge_model_id",
                f"既定の判定モデル「{judge}」は登録モデルにありません。"
                "登録モデルから選び直してください。",
            )
        )
    return errors


# --------------------------------------------------------------------------- payload <-> Settings


def model_payload(settings: Any) -> ModelSettingsPayload:
    """Settings から UI 用 payload を組み立てる（secret は含めない）。"""
    models = [model.model_copy() for model in enterprise_ai_registered_models(settings)]
    if not models:
        models.append(EnterpriseAiConfiguredModel())
    api_path = settings.oci_enterprise_ai_llm_path or settings.oci_enterprise_ai_vlm_path
    return ModelSettingsPayload(
        enterprise_ai=EnterpriseAiModelSettings(
            connections=[
                EnterpriseAiConnectionSettings(
                    connection_id=connection.connection_id,
                    endpoint=connection.endpoint,
                    project_ocid=connection.project_ocid,
                    api_key="",
                    has_api_key=bool(connection.api_key),
                )
                for connection in enterprise_ai_connections(settings)
            ],
            models=models,
            default_text_model_id=_setting_text(settings, "oci_enterprise_ai_default_text_model"),
            default_vision_model_id=enterprise_ai_vision_model_id(settings),
            default_judge_model_id=_setting_text(settings, "oci_enterprise_ai_default_judge_model"),
            default_embedding_model_id=_usable_embedding_model_id(
                _setting_text(settings, "oci_genai_embedding_model")
            ),
            default_rerank_model_id=_usable_embedding_model_id(
                _setting_text(settings, "oci_genai_rerank_model")
            ),
            api_path=api_path or "/responses",
            vlm_input_mode=settings.oci_enterprise_ai_vlm_input_mode,
            text_payload_template=settings.oci_enterprise_ai_llm_payload_template,
            vision_payload_template=settings.oci_enterprise_ai_vlm_payload_template,
            text_response_path=settings.oci_enterprise_ai_llm_response_path,
            vision_response_path=settings.oci_enterprise_ai_vlm_response_path,
            timeout_seconds=settings.oci_enterprise_ai_timeout_seconds,
            max_retries=settings.oci_enterprise_ai_max_retries,
        ),
    )


def apply_model_settings(settings: Any, payload: ModelSettingsPayload) -> None:
    """payload（secret 以外）を Settings へ反映する。API key は呼出側で扱う。"""
    enterprise = payload.enterprise_ai
    models = [model.model_copy() for model in enterprise.models if model.model_id]
    for connection_id, fields in _CONNECTION_FIELDS.items():
        # payload にない接続（セカンダリ接続・ターシャリ接続を削除した場合）は空にする。
        connection = enterprise.connection(connection_id) or EnterpriseAiConnectionSettings()
        _set_if_field(settings, fields.endpoint, connection.endpoint)
        _set_if_field(settings, fields.project_ocid, connection.project_ocid)
    settings.oci_enterprise_ai_models = models
    settings.oci_enterprise_ai_default_text_model = enterprise.default_text_model_id
    settings.oci_enterprise_ai_default_vision_model = enterprise.default_vision_model_id
    settings.oci_enterprise_ai_default_judge_model = enterprise.default_judge_model_id
    settings.oci_enterprise_ai_llm_path = enterprise.api_path
    settings.oci_enterprise_ai_vlm_path = enterprise.api_path
    settings.oci_enterprise_ai_vlm_input_mode = enterprise.vlm_input_mode
    settings.oci_enterprise_ai_llm_payload_template = enterprise.text_payload_template
    settings.oci_enterprise_ai_vlm_payload_template = enterprise.vision_payload_template
    settings.oci_enterprise_ai_llm_response_path = enterprise.text_response_path
    settings.oci_enterprise_ai_vlm_response_path = enterprise.vision_response_path
    settings.oci_enterprise_ai_timeout_seconds = enterprise.timeout_seconds
    settings.oci_enterprise_ai_max_retries = enterprise.max_retries
    # 既定の埋め込み / 再ランクのモデル（#45）。endpoint・API key はそのモデルの接続から
    # `embedding_model_connection` で引く。次元は製品の設定（`oci_genai_embedding_dim`。Oracle の
    # VECTOR 列に合わせた値）のまま変えない。
    _apply_embedding_models(
        settings, enterprise.default_embedding_model_id, enterprise.default_rerank_model_id
    )


def _apply_embedding_models(settings: Any, embedding_model: str, rerank_model: str) -> None:
    """既定の埋め込み / 再ランクのモデルを Settings へ。空は今の値（`.env`・製品の既定）のまま。"""
    embedding_model = embedding_model or _setting_text(settings, "oci_genai_embedding_model")
    rerank_model = rerank_model or _setting_text(settings, "oci_genai_rerank_model")
    settings.oci_genai_embedding_model = embedding_model
    settings.oci_genai_rerank_model = rerank_model
    # NL2SQL は旧名の model ID も読んでいる。
    _set_if_field(settings, "oci_genai_embed_model_id", embedding_model)
    _set_if_field(settings, "oci_genai_rerank_model_id", rerank_model)


def _set_if_field(settings: Any, name: str, value: object) -> None:
    if name in getattr(type(settings), "model_fields", {}):
        setattr(settings, name, value)


def _secret_value(*, current: str, update: str | None, clear: bool) -> str:
    """secret の保持・更新・削除を判定する。"""
    if clear:
        return ""
    if update:
        return update
    return current


def resolve_api_keys(settings: Any, payload: ModelSettingsPayload) -> dict[str, str]:
    """保存後の接続ごとの API key（空欄は現在値を保持、`clear_api_key` は削除）。

    payload にない接続（削除したセカンダリ接続・ターシャリ接続）の key は空にする。
    """
    keys: dict[str, str] = {}
    for connection_id, fields in _CONNECTION_FIELDS.items():
        connection = payload.enterprise_ai.connection(connection_id)
        keys[connection_id] = (
            _secret_value(
                current=_setting_text(settings, fields.api_key),
                update=connection.api_key,
                clear=connection.clear_api_key,
            )
            if connection is not None
            else ""
        )
    return keys


def resolve_api_key(settings: Any, payload: ModelSettingsPayload) -> str:
    """保存後のプライマリ接続の API key。"""
    return resolve_api_keys(settings, payload)[ENTERPRISE_AI_PRIMARY_CONNECTION_ID]


def model_settings_revision(settings: Any) -> str:
    """保存済みのモデル設定の版（#1037）。

    画面に返す値（secret は含めず、API key は有無だけ）の hash。3 製品が同じ
    `model-settings.json` と共通 `.env` を読むので、どの製品・worker でも同じ状態なら同じ値になる。
    """
    return _payload_revision(model_payload(settings))


def _payload_revision(payload: ModelSettingsPayload) -> str:
    document = payload.model_dump_json()
    return hashlib.sha256(document.encode("utf-8")).hexdigest()[:32]


def model_settings_data(settings: Any) -> ModelSettingsData:
    """現在の Settings を API data へ変換する（secret を含めない）。"""
    payload = model_payload(settings)
    return ModelSettingsData(
        settings=payload,
        model_settings_file=settings.model_settings_file,
        source="runtime",
        secret_source=settings.model_secret_source,
        legacy_secret_detected=settings.legacy_model_secret_detected,
        revision=_payload_revision(payload),
        embedding_dim=_embedding_dim_of(settings),
    )


def _embedding_dim_of(settings: Any) -> int | None:
    value = getattr(settings, "oci_genai_embedding_dim", None)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# --------------------------------------------------------------------------- persistence


class _PersistedConnection(BaseModel):
    # #533 の JSON にある表示名（`display_name`）は、知らない key として読み捨てる（#542）。
    connection_id: str = ""
    endpoint: str = ""
    project_ocid: str = ""

    @field_validator("connection_id", "endpoint", "project_ocid", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class _PersistedEnterpriseAiSettings(BaseModel):
    # #533 からの接続の一覧（secret は書かない）。None は #533 より前の JSON（接続 1 組を
    # top-level の `endpoint` / `project_ocid` に持つ形）で、プライマリ接続として読む。
    connections: list[_PersistedConnection] | None = None
    endpoint: str = ""
    project_ocid: str = ""
    # v1 / v2（RAG・Agent）の読込互換専用。v3 の writer は secret を JSON へ出力しない。
    api_key: str | None = None
    models: list[EnterpriseAiConfiguredModel] = Field(default_factory=list)
    default_text_model_id: str | None = None
    default_vision_model_id: str | None = None
    # 既定の判定モデル（#3574。空 = 既定のテキストモデル）。前の JSON に無ければ空。
    default_judge_model_id: str = ""
    # 既定の埋め込み / 再ランクのモデル（#45）。None は #45 より前の JSON（`generative_ai` の
    # 節から移す）。
    default_embedding_model_id: str | None = None
    default_rerank_model_id: str | None = None
    # #499 より前の JSON の key（既定モデルが 1 つ）。読み込むときに上の 2 つへ移す。
    default_model_id: str | None = None
    api_path: str = "/responses"
    vlm_input_mode: EnterpriseAiVlmInputMode = "auto"
    text_payload_template: str = ""
    vision_payload_template: str = ""
    text_response_path: str = ""
    vision_response_path: str = ""
    timeout_seconds: float = 600.0
    max_retries: int = 3
    # #1719 より前の `llm_max_output_tokens` / `vlm_max_output_tokens` と、廃止した `seed`
    # （#3529）は、知らない key として読み捨てる（出力の上限は登録モデルごとの
    # `max_output_tokens`）。

    @field_validator("vlm_input_mode", mode="before")
    @classmethod
    def normalize_vlm_input_mode(cls, value: object) -> object:
        return str(value).strip().casefold() or "auto"

    @field_validator(
        "default_text_model_id",
        "default_vision_model_id",
        "default_judge_model_id",
        "default_embedding_model_id",
        "default_rerank_model_id",
        "default_model_id",
        mode="before",
    )
    @classmethod
    def strip_model_id(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    def default_models(self, models: Sequence[EnterpriseAiConfiguredModel]) -> tuple[str, str]:
        """(既定のテキストモデル, 既定の Vision モデル) を返す。読み込みは失敗させない。

        - #499 より前の JSON（`default_model_id` だけ）は、従来と同じモデルが使われるように、
          `default_model_id`（空なら先頭の登録モデル）をテキストに、従来の導出の結果を Vision にする
        - Vision に Vision 対応の登録モデル以外が書かれていたら（手で編集した場合など）、
          従来の導出の結果に置き換える
        """
        if self.default_vision_model_id is None:
            legacy = self.default_text_model_id or self.default_model_id or ""
            if not legacy and models:
                legacy = models[0].model_id
            return legacy, _vision_model_id(models, legacy)
        text = self.default_text_model_id or ""
        vision = self.default_vision_model_id
        if not any(model.model_id == vision and model.vision_enabled for model in models):
            vision = _vision_model_id(models, text)
        return text, vision

    def connection_settings(self, settings: Any) -> list[EnterpriseAiConnectionSettings]:
        """保存済みの接続。読み込みは失敗させない（知らない ID・重複は捨てる）。

        #533 より前の JSON は top-level の接続をプライマリ接続にし、セカンダリ接続・
        ターシャリ接続は今の Settings（env）のまま。
        """
        if self.connections is None:
            primary = EnterpriseAiConnectionSettings(
                connection_id=ENTERPRISE_AI_PRIMARY_CONNECTION_ID,
                endpoint=self.endpoint,
                project_ocid=self.project_ocid,
            )
            return [primary] + [
                EnterpriseAiConnectionSettings(
                    connection_id=item.connection_id,
                    endpoint=item.endpoint,
                    project_ocid=item.project_ocid,
                )
                for item in enterprise_ai_connections(settings)[1:]
            ]
        saved = {item.connection_id: item for item in reversed(self.connections)}
        connections: list[EnterpriseAiConnectionSettings] = []
        for connection_id in ENTERPRISE_AI_CONNECTION_IDS:
            item = saved.get(connection_id)
            if item is None:
                if connection_id == ENTERPRISE_AI_PRIMARY_CONNECTION_ID:
                    connections.append(EnterpriseAiConnectionSettings())
                continue
            connections.append(
                EnterpriseAiConnectionSettings(
                    connection_id=connection_id,
                    endpoint=item.endpoint,
                    project_ocid=item.project_ocid,
                )
            )
        return connections


class _PersistedModelSettings(BaseModel):
    version: int = 1
    enterprise_ai: _PersistedEnterpriseAiSettings = Field(
        default_factory=_PersistedEnterpriseAiSettings
    )
    # #45 より前の JSON の埋め込み / 再ランクの節（読み込み互換専用）。
    generative_ai: _LegacyGenerativeAiSettings | None = None


class ModelSettingsStore:
    """model-settings.json と `.env` の API key の読み書き。製品ごとに 1 つ作る。

    model-settings.json と API key は3製品で共有する（共通 `.env`。#211）。
    """

    def __init__(
        self,
        *,
        resolve_path: Callable[[Any], Path],
        env_file: Callable[[Any], Path],
    ) -> None:
        self._resolve_path = resolve_path
        self._env_file = env_file
        self._loaded: tuple[str, int | None] | None = None

    def path(self, settings: Any) -> Path:
        return self._resolve_path(settings)

    def env_file(self, settings: Any) -> Path:
        """API key を保存する `.env`。"""
        return self._env_file(settings)

    def reset(self) -> None:
        """テストや明示的な再初期化のため、最後に読んだファイルの記録を消す。"""
        self._loaded = None

    def load(self, settings: Any, *, refresh_secret: bool = False) -> None:
        """保存済みの JSON があれば Settings へ上書き適用する。

        `.env` に API key があればそれを使う。`refresh_secret`（別 worker が保存した後の再読込）の
        ときは、`.env` から key が消えていれば未設定にする。
        """
        settings.prepare_model_secret_state(
            self._environment_api_key(settings, refresh=refresh_secret),
            self._environment_api_key(
                settings, refresh=refresh_secret, name=ENTERPRISE_AI_SECONDARY_API_KEY_ENV
            ),
            self._environment_api_key(
                settings, refresh=refresh_secret, name=ENTERPRISE_AI_TERTIARY_API_KEY_ENV
            ),
        )
        path = self.path(settings)
        if not path.is_file():
            self._loaded = (str(path), None)
            return
        try:
            mtime_ns = path.stat().st_mtime_ns
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("JSON object ではありません。")
            persisted = _PersistedModelSettings.model_validate(raw)
            self._apply(settings, persisted)
        except (OSError, ValueError) as exc:
            raise ValueError(f"モデル設定ファイルを読み込めません: {path}") from exc
        if settings.legacy_model_secret_detected:
            logger.warning(
                "legacy_model_secret_detected",
                extra={"warning_code": "MODEL_SETTINGS_LEGACY_SECRET"},
            )
        self._loaded = (str(path), mtime_ns)

    def reload_if_changed(self, settings: Any) -> None:
        """別 worker が保存したモデル設定を次回リクエストで取り込む。"""
        path = self.path(settings)
        try:
            mtime_ns = path.stat().st_mtime_ns if path.is_file() else None
        except OSError:
            mtime_ns = None
        if self._loaded == (str(path), mtime_ns):
            return
        if mtime_ns is None:
            self._loaded = (str(path), None)
            return
        self.load(settings, refresh_secret=True)

    @contextmanager
    def lock(self, settings: Any) -> Iterator[None]:
        """model-settings.json の read-modify-write を worker 間で直列化する。"""
        path = self.path(settings)
        lock_path = path.with_name(f"{path.name}.lock")
        _ensure_directory(lock_path.parent)
        with lock_path.open("a", encoding="utf-8") as lock_file:
            lock_path.chmod(MODEL_SETTINGS_FILE_MODE)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def save(
        self,
        settings: Any,
        payload: ModelSettingsPayload,
        *,
        api_key: str,
        secondary_api_key: str | None = None,
        tertiary_api_key: str | None = None,
    ) -> None:
        """`.env` の API key → JSON の順に保存する。JSON に失敗したら `.env` を元に戻す。

        `.env` を先に書くのは、JSON の mtime を見て再読込する別 worker が新しい key を読めるように
        するため。`lock()` の中で呼ぶ。失敗時は OSError を送出する。
        `secondary_api_key`（セカンダリ接続）・`tertiary_api_key`（ターシャリ接続）を省くと今の値の
        まま。payload にその接続がなければ消す。
        """
        api_key_values: dict[str, str | None] = {ENTERPRISE_AI_API_KEY_ENV: api_key.strip() or None}
        optional_keys = {
            ENTERPRISE_AI_SECONDARY_CONNECTION_ID: secondary_api_key,
            ENTERPRISE_AI_TERTIARY_CONNECTION_ID: tertiary_api_key,
        }
        for connection_id, connection_key in optional_keys.items():
            fields = _CONNECTION_FIELDS[connection_id]
            if payload.enterprise_ai.connection(connection_id) is None:
                connection_key = ""
            elif connection_key is None:
                connection_key = _setting_text(settings, fields.api_key)
            api_key_values[fields.api_key_env] = connection_key.strip() or None
        targets = [(self.env_file(settings), api_key_values)]
        written: list[tuple[Path, dict[str, str | None]]] = []
        try:
            for env_file, values in targets:
                current = _dotenv_values(env_file)
                for name, value in values.items():
                    # 環境変数から来ただけの値は `.env` に書かない（環境変数を変えたら追従させる）。
                    if name not in current and value == (os.environ.get(name) or "").strip():
                        values[name] = None
                previous = {name: current.get(name) or None for name in values}
                _write_env_secrets(env_file, values)
                written.append((env_file, previous))
            self.write_document(settings, payload)
        except OSError:
            for env_file, previous in reversed(written):
                _write_env_secrets(env_file, previous)
            raise

    def write_document(self, settings: Any, payload: ModelSettingsPayload) -> None:
        """JSON を atomic に保存する（secret は書かない）。`lock()` の中で呼ぶ。"""
        path = self.path(settings)
        # 共有ファイルなので、他の製品の節（この製品が知らない key）は残す。
        document = {
            key: value
            for key, value in _read_document(path).items()
            if key not in _COMMON_DOCUMENT_KEYS
        }
        document.update(_model_settings_document(payload))
        _ensure_directory(path.parent)
        tmp_path = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
        try:
            tmp_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tmp_path.chmod(MODEL_SETTINGS_FILE_MODE)
            tmp_path.replace(path)
            path.chmod(MODEL_SETTINGS_FILE_MODE)
        finally:
            tmp_path.unlink(missing_ok=True)

    def _environment_api_key(
        self, settings: Any, *, refresh: bool, name: str = ENTERPRISE_AI_API_KEY_ENV
    ) -> str | None:
        """実効の API key。None は「分からないので現在の基準値を使う」。

        画面で保存した値（`.env` にある key）を、プロセスの環境変数より優先する。
        `.env` に key がなければ環境変数を使う（画面で削除した場合も環境変数へ戻る）。
        """
        env_file = self.env_file(settings)
        value = _dotenv_values(env_file).get(name)
        if value is not None:
            return value
        if name in os.environ:
            return os.environ[name]
        return "" if refresh and env_file.is_file() else None

    @staticmethod
    def _write_api_key(env_file: Path, value: str | None) -> None:
        _write_env_secrets(env_file, {ENTERPRISE_AI_API_KEY_ENV: value})

    @staticmethod
    def _apply(settings: Any, persisted: _PersistedModelSettings) -> None:
        enterprise = persisted.enterprise_ai
        models = [model for model in enterprise.models if model.model_id]
        text_model, vision_model = enterprise.default_models(models)
        embedding_model, rerank_model = _embedding_defaults(enterprise, persisted, models, settings)
        apply_model_settings(
            settings,
            ModelSettingsPayload.model_construct(
                enterprise_ai=EnterpriseAiModelSettings.model_construct(
                    **enterprise.model_dump(
                        exclude={
                            "connections",
                            "endpoint",
                            "project_ocid",
                            "api_key",
                            "models",
                            "default_model_id",
                            "default_text_model_id",
                            "default_vision_model_id",
                            "default_embedding_model_id",
                            "default_rerank_model_id",
                        }
                    ),
                    connections=enterprise.connection_settings(settings),
                    models=models,
                    default_text_model_id=text_model,
                    default_vision_model_id=vision_model,
                    default_embedding_model_id=embedding_model,
                    default_rerank_model_id=rerank_model,
                ),
            ),
        )
        legacy_secret = (enterprise.api_key or "").strip()
        settings.apply_legacy_enterprise_ai_api_key(
            legacy_secret
            if persisted.version < MODEL_SETTINGS_SECRET_FREE_DOCUMENT_VERSION
            else "",
            detected=bool(legacy_secret),
        )


# #8 より前の OCI Generative AI（Cohere）の埋め込み / 再ランクのモデル ID の接頭辞。OpenAI 互換の
# 経路では呼べないので、画面・移行では未設定として扱い、利用者に選び直させる（#45）。
_LEGACY_OCI_GENAI_MODEL_PREFIX: Final = "cohere."


def _usable_embedding_model_id(value: str) -> str:
    """既定の埋め込み / 再ランクのモデル ID。旧 Cohere の ID は空にする（#45）。"""
    return "" if value.startswith(_LEGACY_OCI_GENAI_MODEL_PREFIX) else value


def _embedding_defaults(
    enterprise: _PersistedEnterpriseAiSettings,
    persisted: _PersistedModelSettings,
    models: list[EnterpriseAiConfiguredModel],
    settings: Any,
) -> tuple[str, str]:
    """保存済みの既定の埋め込み / 再ランクのモデル（#45）。`models` を書き換えることがある。

    #45 より前の JSON（`default_embedding_model_id` が無い）は、`generative_ai` の節のモデル ID を
    既定のモデルにし、登録に無ければ種類 embedding / rerank・プライマリ接続の登録モデルとして
    足す（その JSON の埋め込み / 再ランクはプライマリ接続の key で呼んでいたので、挙動を保つ）。
    旧 OCI Generative AI（Cohere。`cohere.` で始まる ID）は OpenAI 互換の経路で呼べないので
    移さず、未設定（画面で選び直す）にする。`generative_ai` も無い JSON は今の Settings
    （`.env`）の値のまま。
    """
    legacy = persisted.generative_ai
    values: list[str] = []
    for kind, saved, legacy_value, current in (
        (
            ENTERPRISE_AI_MODEL_KIND_EMBEDDING,
            enterprise.default_embedding_model_id,
            legacy.embedding_model if legacy else "",
            _setting_text(settings, "oci_genai_embedding_model"),
        ),
        (
            ENTERPRISE_AI_MODEL_KIND_RERANK,
            enterprise.default_rerank_model_id,
            legacy.rerank_model if legacy else "",
            _setting_text(settings, "oci_genai_rerank_model"),
        ),
    ):
        if saved is not None:
            values.append(saved)
            continue
        legacy_value = _usable_embedding_model_id(legacy_value)
        if legacy and not legacy_value:
            values.append("")
            continue
        if legacy_value and not any(model.model_id == legacy_value for model in models):
            models.append(
                EnterpriseAiConfiguredModel(
                    model_id=legacy_value, display_name=legacy_value, kind=kind
                )
            )
        values.append(legacy_value or current)
    return values[0], values[1]


def _model_settings_document(payload: ModelSettingsPayload) -> dict[str, Any]:
    enterprise = payload.enterprise_ai
    return {
        "version": MODEL_SETTINGS_DOCUMENT_VERSION,
        "enterprise_ai": {
            "connections": [
                connection.model_dump(include={"connection_id", "endpoint", "project_ocid"})
                for connection in enterprise.connections
            ],
            "models": [
                model.model_dump(
                    include={
                        "model_id",
                        "display_name",
                        "vision_enabled",
                        "kind",
                        "connection_id",
                        "reasoning_effort",
                        "max_input_tokens",
                        "max_output_tokens",
                    }
                )
                for model in enterprise.models
                if model.model_id
            ],
            "default_text_model_id": enterprise.default_text_model_id,
            "default_vision_model_id": enterprise.default_vision_model_id,
            "default_judge_model_id": enterprise.default_judge_model_id,
            "default_embedding_model_id": enterprise.default_embedding_model_id,
            "default_rerank_model_id": enterprise.default_rerank_model_id,
            "api_path": enterprise.api_path,
            "vlm_input_mode": enterprise.vlm_input_mode,
            "text_payload_template": enterprise.text_payload_template,
            "vision_payload_template": enterprise.vision_payload_template,
            "text_response_path": enterprise.text_response_path,
            "vision_response_path": enterprise.vision_response_path,
            "timeout_seconds": enterprise.timeout_seconds,
            "max_retries": enterprise.max_retries,
        },
    }


_COMMON_DOCUMENT_KEYS = frozenset({"version", "enterprise_ai", "generative_ai"})


def _read_document(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _dotenv_values(env_file: Path) -> dict[str, str | None]:
    return dict(dotenv_values(env_file)) if env_file.is_file() else {}


def _write_env_secrets(env_file: Path, values: Mapping[str, str | None]) -> None:
    write_env_values(env_file, values, section_comment="# モデル設定の secret（画面から保存）")


def _ensure_directory(path: Path) -> None:
    existed = path.exists()
    path.mkdir(mode=MODEL_SETTINGS_DIRECTORY_MODE, parents=True, exist_ok=True)
    if not existed:
        path.chmod(MODEL_SETTINGS_DIRECTORY_MODE)


# --------------------------------------------------------------------------- model test


def model_test_candidate(settings: Any, request: ModelSettingsTestRequest) -> Any:
    """保存前 payload を、対象モデルだけを呼ぶ一時 Settings へ変換する。"""
    candidate = settings.model_copy(deep=True)
    apply_model_settings(candidate, request.settings)
    # 接続ごとの key（#533）。製品は `enterprise_ai_connection_for_model` で対象モデルの接続を引く。
    keys = resolve_api_keys(settings, request.settings)
    for connection_id, fields in _CONNECTION_FIELDS.items():
        _set_if_field(candidate, fields.api_key, keys[connection_id])
    model_id = request.model_id
    if request.target_type in {"enterprise_text", "enterprise_vision"}:
        candidate.oci_enterprise_ai_default_text_model = model_id
    if request.target_type == "enterprise_vision":
        candidate.oci_enterprise_ai_default_vision_model = model_id
        candidate.oci_enterprise_ai_models = [
            model.model_copy(
                update={"vision_enabled": model.model_id == model_id or model.vision_enabled}
            )
            for model in enterprise_ai_model_catalog(candidate)
        ]
    elif request.target_type == "embedding":
        candidate.oci_genai_embedding_model = model_id
        _set_if_field(candidate, "oci_genai_embed_model_id", model_id)
    elif request.target_type == "rerank":
        candidate.oci_genai_rerank_model = model_id
        _set_if_field(candidate, "oci_genai_rerank_model_id", model_id)
    return candidate


def _model_test_success_message(
    target_type: ModelSettingsTestTargetType, model_id: str, details: ModelTestDetails
) -> str:
    if target_type == "enterprise_text":
        return f"Enterprise AI の回答生成モデル「{model_id}」から応答を取得しました。"
    if target_type == "enterprise_vision":
        return (
            f"Enterprise AI の画像対応モデル「{model_id}」から構造化抽出レスポンスを取得しました。"
        )
    if target_type == "embedding":
        dim = details.get("vector_dim")
        if dim:
            return f"埋め込みモデル「{model_id}」で {dim} 次元のベクトルを取得しました。"
        return f"埋め込みモデル「{model_id}」でベクトルを取得しました。"
    order = details.get("ranked_order")
    score = details.get("top_score")
    if order and score is not None:
        return (
            f"再ランクモデル「{model_id}」で文書を並べ替えました"
            f"（順位: {order}。最上位のスコア {score}）。"
        )
    return f"再ランクモデル「{model_id}」から順位スコアを取得しました。"


def _model_test_failure_message(target_type: ModelSettingsTestTargetType, model_id: str) -> str:
    if target_type in {"enterprise_text", "enterprise_vision"}:
        return f"Enterprise AI モデル「{model_id or '未入力'}」のテストに失敗しました。"
    if target_type == "embedding":
        return f"埋め込みモデル「{model_id or '未入力'}」のテストに失敗しました。"
    return f"再ランクモデル「{model_id or '未入力'}」のテストに失敗しました。"


def _model_test_troubleshooting(
    target_type: ModelSettingsTestTargetType,
    raw_error: str,
    error_type: str,
) -> list[str]:
    """実エラーからユーザーが次に確認しやすい項目を返す。"""
    lowered = f"{raw_error} {error_type}".lower()
    tips: list[str] = []
    if target_type in {"enterprise_text", "enterprise_vision"}:
        tips.extend(
            [
                "Endpoint URL、API パス、Project OCID、API key が Enterprise AI の"
                " OpenAI-compatible gateway と一致しているか確認してください。",
                "モデル ID が Enterprise AI 側の model deployment / gateway で"
                "利用可能か確認してください。",
            ]
        )
        if "response path" in lowered or "回答 text" in raw_error or "構造化抽出" in raw_error:
            tips.append(
                "独自 gateway の場合は payload template と response path が"
                "実レスポンスの JSON 構造に合っているか確認してください。"
            )
    else:
        tips.extend(
            [
                "OCI config file、profile、region、compartment OCID が"
                "バックエンド実行環境から参照できるか確認してください。",
                "モデル ID と IAM policy が OCI Generative AI Inference の"
                " embedding/rerank 呼び出しを許可しているか確認してください。",
            ]
        )
    http_status = (
        enterprise_ai_http_status_from_message(raw_error)
        if target_type in {"enterprise_text", "enterprise_vision"}
        else None
    )
    if http_status is not None:
        # Enterprise AI の HTTP の失敗は、3 製品で共通の対応表で案内する（#1684。OCI の 404 は
        # 「モデルが無い」ではなく API key の認可の失敗で、モデルが無いときは 400 になる）。
        tips.extend(
            enterprise_ai_failure_actions(
                classify_enterprise_ai_http_failure(http_status, raw_error)
            )
        )
        if any(token in lowered for token in ("timeout", "timed out")):
            tips.append(
                "タイムアウトです。ネットワーク経路を確認し、"
                "必要ならタイムアウト秒数を一時的に長くしてください。"
            )
        return list(dict.fromkeys(tips))
    if any(token in lowered for token in ("401", "unauthorized", "authentication")):
        tips.append(
            "認証エラーです。API key / OCI config の資格情報を再発行または再保存してください。"
        )
    if any(token in lowered for token in ("403", "notauthorized", "not authorized", "forbidden")):
        tips.append(
            "権限エラーです。Project / compartment / IAM policy の対象が"
            "このモデル呼び出しを許可しているか確認してください。"
        )
    if any(token in lowered for token in ("404", "not found")):
        tips.append(
            "Endpoint、API パス、model ID のいずれかが見つからないか、IAM policy が"
            "この呼び出しを許可していません（OCI は権限の不足も 404 で返します）。"
            "リージョンと IAM policy も確認してください。"
        )
    if any(token in lowered for token in ("timeout", "timed out")):
        tips.append(
            "タイムアウトです。ネットワーク経路を確認し、"
            "必要ならタイムアウト秒数を一時的に長くしてください。"
        )
    if any(token in lowered for token in ("429", "quota", "rate")):
        tips.append(
            "レート制限または quota の可能性があります。"
            "しばらく待つか service limit を確認してください。"
        )
    if any(token in lowered for token in ("500", "502", "503", "504")):
        tips.append(
            "サービス側または gateway 側の一時障害の可能性があります。"
            "少し待って再試行し、OCI 側の稼働状況を確認してください。"
        )
    return list(dict.fromkeys(tips))


def sanitize_model_test_error(raw_error: str, secrets: Sequence[str]) -> str:
    """実エラーは残しつつ、既知の secret だけを伏せる。"""
    sanitized = raw_error.strip() or "詳細メッセージは返されませんでした。"
    for secret in secrets:
        cleaned = secret.strip()
        if cleaned:
            sanitized = sanitized.replace(cleaned, "<secret>")
    return sanitized[:2000]


async def run_model_settings_test(
    settings: Any,
    request: ModelSettingsTestRequest,
    run_model_test: RunModelTest,
) -> ModelSettingsTestResult:
    """対象モデルを実 API で呼び、表示用の結果へ変換する（例外は結果に含める）。"""
    started = time.perf_counter()
    candidate = model_test_candidate(settings, request)
    try:
        if not request.model_id:
            raise ValueError("テストするモデル ID を入力してください。")
        # モデルが受け付けない引数（#3539）を外して送ったら、結果の詳細と文に出す。
        with collect_dropped_parameters() as dropped:
            details = await run_model_test(candidate, request)
    except Exception as exc:  # noqa: BLE001 - 外部 SDK/API の多様な例外を表示用に握る
        raw_error = sanitize_model_test_error(
            str(exc),
            [_setting_text(candidate, fields.api_key) for fields in _CONNECTION_FIELDS.values()],
        )
        return ModelSettingsTestResult(
            status="failed",
            target_type=request.target_type,
            model_id=request.model_id,
            message=_model_test_failure_message(request.target_type, request.model_id),
            troubleshooting=_model_test_troubleshooting(
                request.target_type, raw_error, type(exc).__name__
            ),
            raw_error=raw_error,
            error_type=type(exc).__name__,
            elapsed_ms=_elapsed_ms(started),
        )
    message = _model_test_success_message(request.target_type, request.model_id, details)
    if dropped:
        names = ", ".join(sorted(dropped))
        details = {**details, "dropped_parameters": names}
        message += _dropped_parameters_message(names)
    return ModelSettingsTestResult(
        status="success",
        target_type=request.target_type,
        model_id=request.model_id,
        message=message,
        elapsed_ms=_elapsed_ms(started),
        details=details,
    )


def _dropped_parameters_message(names: str) -> str:
    """受け付けない引数を外して送ったときに、成功の文に足す文（#3539）。"""
    return (
        f"このモデルが受け付けない引数（{names}）は外して送りました"
        "（backend を再起動するまで、このモデルには送りません）。"
    )


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


# --------------------------------------------------------------------------- router

RunModelTest = Callable[[Any, ModelSettingsTestRequest], Awaitable[ModelTestDetails]]


MODEL_SETTINGS_CONFLICT_MESSAGE: Final = (
    "モデル設定は、この画面を開いた後にほかの画面（別の製品を含む）で更新されました。"
    "最新の設定を読み込んでから、保存し直してください。"
)


def save_model_settings(
    settings: Any,
    store: ModelSettingsStore,
    payload: ModelSettingsPayload,
    *,
    base_revision: str | None = None,
) -> None:
    """保存が成功してから runtime へ反映する。

    登録モデルか既定のモデルを変える保存では既定のモデルを検証する（不正なら HTTPException(422)）。
    変えない保存（接続情報・Generative AI の節だけの保存）は、保存済みの状態が不正でも止めない。
    接続（セカンダリ接続・ターシャリ接続の必須の欄。#542 / #786）と、登録モデルが指す接続は
    毎回検証する（#533）。
    `base_revision`（画面が読み込んだ時点の版。#1037）を渡したときは、lock の中で読み直した後の版と
    違えば HTTPException(409) にして保存しない。
    永続化の失敗は HTTPException(500)。
    """
    # 接続（#533）は毎回確かめる。直す欄は同じ画面にあるので、保存済みの状態が不正でも止める。
    # セカンダリ接続・ターシャリ接続の API key は、保存後の値（空欄は保存済みの値を保持）で
    # 確かめる（#542）。
    errors = validate_enterprise_ai_connections(
        payload.enterprise_ai, api_keys=resolve_api_keys(settings, payload)
    )
    if _default_models_changed(settings, payload.enterprise_ai):
        # 登録モデルを変える保存だけで確かめる（#1035）。既存の不正な状態で、
        # 接続・Generative AI の節の保存は止めない。
        errors += validate_model_ids(payload.enterprise_ai)
        errors += validate_default_models(payload.enterprise_ai)
    if errors:
        raise HTTPException(status_code=422, detail=" ".join(error.message for error in errors))
    try:
        with store.lock(settings):
            store.reload_if_changed(settings)
            if base_revision is not None and base_revision != model_settings_revision(settings):
                raise HTTPException(status_code=409, detail=MODEL_SETTINGS_CONFLICT_MESSAGE)
            keys = resolve_api_keys(settings, payload)
            store.save(
                settings,
                payload,
                api_key=keys[ENTERPRISE_AI_PRIMARY_CONNECTION_ID],
                secondary_api_key=keys[ENTERPRISE_AI_SECONDARY_CONNECTION_ID],
                tertiary_api_key=keys[ENTERPRISE_AI_TERTIARY_CONNECTION_ID],
            )
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail="モデル設定を永続化ファイルへ保存できませんでした。"
        ) from exc
    # 保存したファイルから読み直し、runtime を保存した状態にそろえる。
    # API key を削除した場合は、プロセスの環境変数の key に戻る（再読込時と同じ規則）。
    store.load(settings, refresh_secret=True)


def _default_models_changed(settings: Any, enterprise: EnterpriseAiModelSettings) -> bool:
    """登録モデル（ID と Vision 対応）か既定のモデルが、現在の設定から変わるか。"""

    def key(value: EnterpriseAiModelSettings) -> tuple[object, ...]:
        return (
            [
                (model.model_id, model.vision_enabled, model.kind)
                for model in value.models
                if model.model_id
            ],
            value.default_text_model_id,
            value.default_vision_model_id,
            value.default_judge_model_id,
            value.default_embedding_model_id,
            value.default_rerank_model_id,
        )

    return key(model_payload(settings).enterprise_ai) != key(enterprise)


def build_model_router(
    *,
    get_settings: Callable[[], Any],
    store: ModelSettingsStore,
    run_model_test: RunModelTest,
    write_dependencies: Sequence[params.Depends] = (),
    action_dependencies: Sequence[params.Depends] = (),
) -> APIRouter:
    """`/model*` の router を作る。製品側で `/settings` 配下に include する。

    - `run_model_test(candidate_settings, request)`：対象モデルを実際に呼び、表示用 details を返す
    - `write_dependencies`：PATCH に付ける依存関係
    - `action_dependencies`：外部へ通信する接続テストに付ける依存関係
    """
    router = APIRouter()

    @router.get("/model", response_model=ApiResponse[ModelSettingsData])
    def get_model_settings() -> ApiResponse[ModelSettingsData]:
        return ApiResponse(data=model_settings_data(get_settings()))

    @router.patch(
        "/model",
        response_model=ApiResponse[ModelSettingsData],
        dependencies=list(write_dependencies),
    )
    def update_model_settings(
        request: ModelSettingsUpdateRequest,
    ) -> ApiResponse[ModelSettingsData]:
        settings = get_settings()
        payload = ModelSettingsPayload.model_validate(request.model_dump(exclude={"base_revision"}))
        save_model_settings(settings, store, payload, base_revision=request.base_revision)
        return ApiResponse(data=model_settings_data(settings))

    @router.post(
        "/model/test",
        response_model=ApiResponse[ModelSettingsTestResult],
        dependencies=list(action_dependencies),
    )
    async def test_model_settings(
        request: ModelSettingsTestRequest,
    ) -> ApiResponse[ModelSettingsTestResult]:
        result = await run_model_settings_test(get_settings(), request, run_model_test)
        return ApiResponse(data=result)

    return router
