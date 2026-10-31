"""NL2SQL が MCP で公開するツール（#231）。

Agent は Run の利用者のサービストークンで `POST /api/mcp` を呼ぶ。認証と利用者の特定は
`authorize_api_request`（`service_token_paths={"/mcp"}`）が行い、actor も同じ経路で
入る。ここでは画面の route と同じ service 層・業務プロファイルの範囲・ジョブの所有者の判定を使う。

- `nl2sql_list_profiles`: `GET /nl2sql/profiles/search` と同じ範囲の業務プロファイル一覧
- `nl2sql_recommend_profile`: `POST /nl2sql/recommend-profile`（しきい値未満は推薦なし。
  `profile_ids` を渡すとその候補の中からだけ推薦する）
- `nl2sql_query`: `POST /nl2sql/jobs` でジョブを作り、`wait_seconds` まで結果を待つ。
  `profile_id` を省略したとき・使えない ID のときは、既定の業務プロファイル `"default"` に黙って
  切り替えず（#1379）、使える業務プロファイルが 1 つならそれを、そうでなければ質問の推薦が決めた
  ものを使い、結果の `auto_selected_profile` に示す（決まらなければ候補つきのエラー。#2355）
- `nl2sql_get_job`: `GET /nl2sql/jobs/{job_id}`（本人のジョブだけ。利用者が system_admin なら
  全員のジョブを見られる。画面の「AI 活用」と同じ規則。#1889）。`nl2sql_query` の
  `requirement_session_id` で確認を続けられるのは、確認を始めた本人だけ（#3244）
- `nl2sql_cancel_job`: 実行中のジョブを止める（`POST /nl2sql/jobs/{id}/cancel` と同じ協調の取消。
  冪等で、終わったジョブはエラーにせず `result` で返す。止められるのは本人と system_admin〔#3244 の
  運用の操作〕。Agent は Run の取消で、その Run が始めたジョブを止める。#3318）
- `nl2sql_cancel_requirement`: SQL の生成前の確認の session を閉じる
  （`POST /nl2sql/query-sessions/{id}/cancel`。利用者が Agent のチャットで
  「データベースの検索は不要」としたとき、Agent の Control Plane が呼ぶ）

SQL の生成前の確認（#1439・#1441）: `nl2sql_query` は SQL を作る前に、画面の回答前の確認と同じ流れ
（`POST /nl2sql/query-sessions` の guided・回答・`apply`・`POST /nl2sql/jobs` の
`requirement_session_id`）で、Schema・業務プロファイル・オントロジーから SQL の要件を確かめる。
足りない情報・明確でない情報があれば SQL を作らずに `status=needs_clarification` と確認の項目
（`requirement.question`。質問・候補・理由。表・列は物理名）を 1 つだけ返す。呼び出し元が利用者に
聞き、`requirement_session_id` と `answer` を付けて呼び直すと要件を更新し、確認の要るものが無くなり
SELECT と FROM が明確になってから SQL を生成・実行する。確認が要らなければ聞かずに進む。

サービストークンにプロファイルの範囲の claim（`profile_ids`。業務 Agent のデータの範囲。#1379）が
あれば、業務プロファイルは「利用者の権限 ∩ claim」で判定する: 範囲外の `profile_id` は 403
（`PROFILE_SCOPE_FORBIDDEN`）、一覧・推薦は範囲で絞る。claim の無い呼び出しは今までどおり。
ジョブ（`nl2sql_get_job`・`nl2sql_cancel_job`）と確認の session も、読むたび・操作するたびに
その時点の claim と利用者の業務プロファイルの権限で判定する（OWASP API1。#3318）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from functools import wraps
from typing import Any, Literal

from fastapi import HTTPException, Request
from pr_backend_core.mcp import McpServer, McpTool, McpToolError
from pr_backend_core.observability.tracing import ObservedSpan, SpanDisplay, observed_span
from pr_system_settings.auth.dependencies import service_token_profile_scope
from pr_system_settings.auth.errors import SecurityApiError
from pr_system_settings.auth.service_token import (
    PROFILE_SCOPE_FORBIDDEN_CODE,
    PROFILE_SCOPE_MAX_IDS,
    narrow_to_profile_scope,
)
from pr_system_settings.auth.visibility import (
    OTHERS_CONTENT_FORBIDDEN_CODE,
    OTHERS_CONTENT_FORBIDDEN_MESSAGE,
    OTHERS_CONTENT_FORBIDDEN_STATUS,
    can_access_user_data,
    can_add_user_content,
    can_view_all_users_data,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.api.concurrency import run_sync_io
from app.features.nl2sql import ontology_router
from app.features.nl2sql.models import (
    JobCreateRequest,
    JobData,
    JobOrigin,
    JobStatus,
    Nl2SqlProfile,
    ProfileRecommendationRequest,
)
from app.features.nl2sql.ontology_models import (
    ClarificationMode,
    QuerySessionStatus,
    RequirementPhase,
)
from app.features.nl2sql.ontology_router import (
    ClarificationAnswerRequest,
    QuerySessionApiCreate,
    QuerySessionData,
    RequirementApplyRequest,
)
from app.features.nl2sql.ontology_service import OntologyNotFoundError, OntologyServiceError
from app.features.nl2sql.oracle_adapter import SelectAiCredentialMissingError
from app.features.nl2sql.profile_access import (
    allowed_profile_ids_for_request,
    assert_profile_access,
    principal_from_request,
    profile_access_denied,
)
from app.features.nl2sql.router import _with_requirement
from app.features.nl2sql.select_ai_readiness import select_ai_credential_missing_message
from app.features.nl2sql.service import (
    JOB_CANCELLED_ERROR_CODE,
    JOB_FAILED_ERROR_CODE,
    SCHEMA_CATALOG_EMPTY_ERROR_CODE,
    Nl2SqlPersistenceUnavailable,
    Nl2SqlRepositoryOperationFailed,
    OthersContentForbiddenError,
    SchemaCatalogEmptyError,
    nl2sql_service,
)
from app.features.nl2sql.sql_requirement import RequirementAiUnavailableError
from app.features.nl2sql.trace_spans import REQUIREMENT_SPAN, REQUIREMENT_STAGE
from app.security.permissions import (
    PROFILE_READ_PERMISSION,
    QUERY_GENERATE_PERMISSION,
    SQL_EXECUTE_PERMISSION,
)
from app.settings import get_settings

SERVER_NAME = "production-ready-nl2sql"
# ツールの出力の版（出力の形を変えたら上げる。3 製品の MCP の出力の外枠をそろえる）。
# 1: すべてのツールの出力に schema_version を足した（#3240）。
# 2: ジョブを取り消すツール（nl2sql_cancel_job）を足した（#3318）。
MCP_OUTPUT_SCHEMA_VERSION = 2
# 業務プロファイルを自動で 1 つに決められなかった nl2sql_query のエラー（#2355）。
PROFILE_NOT_SELECTED_CODE = "NL2SQL_PROFILE_NOT_SELECTED"
# そのエラーの message に示す推薦の候補の数。
SELECTION_CANDIDATES_SHOWN = 5
DEFAULT_ROW_LIMIT = 100
MAX_ROW_LIMIT = 1000
# 1 回の MCP 呼び出しで worker を長く塞がないよう、待ちの上限は 45 秒にする
# （gunicorn の worker の timeout より十分短くする。unit は init_script.sh の `--timeout 300`）。
MAX_WAIT_SECONDS = 45
JOB_POLL_INTERVAL_SECONDS = 0.5
_TERMINAL_STATUSES = frozenset({JobStatus.DONE, JobStatus.ERROR})
_JOB_NOT_FOUND_MESSAGE = "指定されたジョブが見つかりません。"
_REQUIREMENT_NOT_FOUND_MESSAGE = (
    "指定された回答前の確認が見つかりません。requirement_session_id を付けずに "
    "nl2sql_query を呼び直してください。"
)
_REQUIREMENT_CLOSED_MESSAGE = (
    "この回答前の確認は終了しています（取り消し・期限切れ）。requirement_session_id を付けずに "
    "nl2sql_query を呼び直してください。"
)
_REQUIREMENT_SKIPPED_MESSAGE = (
    "OCI Enterprise AI が設定されていないため、回答前の確認をせずに SQL を生成しました。"
)
# 業務プロファイルの「曖昧な質問の扱い」が「前提を示して SQL を生成する」（#1839）で、
# 確認せずに作った。
_REQUIREMENT_ASSUMED_MESSAGE = (
    "業務プロファイルの方針（前提を示して SQL を生成する）により、確認せずに SQL を生成しました。"
    "回答では、確かめていない前提（AI の推測）を利用者に示してください。"
)
# 利用者が「確認せずに SQL を生成」を選んだ（`skip_clarification`。#1839）。
_REQUIREMENT_USER_SKIPPED_MESSAGE = (
    "利用者が確認せずに SQL を生成するよう選んだため、確認事項を聞かずに SQL を生成しました。"
    "回答では、確かめていない前提（AI の推測）を利用者に示してください。"
)
REQUIREMENT_AI_NOT_CONFIGURED_CODE = "REQUIREMENT_AI_NOT_CONFIGURED"
# この業務プロファイルの範囲では SQL を作れない（SQL の生成前の確認。#1441）。
REQUIREMENT_UNANSWERABLE_CODE = "REQUIREMENT_UNANSWERABLE"
# 変更できない（終了した・実行中の） SQL の生成前の確認の session。
_CLOSED_SESSION_STATUSES = frozenset(
    {
        QuerySessionStatus.EXECUTING,
        QuerySessionStatus.DONE,
        QuerySessionStatus.ERROR,
        QuerySessionStatus.CANCELLED,
    }
)

# 処理順の Step 形式（docs/agents/prompts.md。#3830）。
_INSTRUCTIONS = (
    "Production Ready NL2SQL のツールです。利用者が使える業務プロファイルの範囲で、"
    "自然言語の質問から SQL を生成・実行します。\n\n"
    "## Step 1: 業務プロファイルを選ぶ\n"
    "業務プロファイルが分からないときは "
    "nl2sql_list_profiles / nl2sql_recommend_profile で選び、nl2sql_query に渡してください。\n\n"
    # 省略・使えない ID のときの自動の選択は profile_id の説明にある（#2355）。
    # 待つ秒数の上限は wait_seconds の schema（MAX_WAIT_SECONDS）にある。
    "## Step 2: 実行中の結果を取得する\n"
    "nl2sql_query が pending / running を返したら、job_id を nl2sql_get_job に渡し、"
    "done / error になるまで続きを取得します。\n\n"
    # 呼び直しの引数の組み立ては requirement_session_id・answer・skip_clarification の説明にある。
    "## Step 3: SQL の生成前の確認に答える\n"
    "nl2sql_query が needs_clarification を返したら SQL はまだ作っていません。"
    "requirement.question の問いを利用者に 1 つだけ確かめてから、答えを付けて nl2sql_query を"
    "呼び直します（利用者が確認せずに進めると選んだら skip_clarification）。"
)


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ListProfilesInput(_Input):
    query: str | None = Field(
        default=None, max_length=200, description="業務プロファイル名・説明の絞り込み語"
    )
    limit: int = Field(default=20, ge=1, le=100, description="返す件数の上限")


def _require_question(value: str) -> str:
    # 空白だけの質問は画面・API（JobCreateRequest）と同じく拒否する。ジョブの作成で検証に落ちて
    # ツールの内部エラーにならないよう、引数の誤り（MCP_TOOL_ARGUMENTS_INVALID）にする（#1054）。
    if not value.strip():
        raise ValueError("質問を入力してください。")
    return value


def _optional_profile_id(value: str | None) -> str | None:
    # 空白だけの ID は省略と同じ（既定の業務プロファイルにはしない。#1379・#2355）。
    if value is None:
        return None
    return value.strip() or None


def _require_profile_id(value: str) -> str:
    # 空白だけの ID を既定の業務プロファイルとして扱わない（#1379）。
    stripped = value.strip()
    if not stripped:
        raise ValueError("業務プロファイルの ID を指定してください。")
    return stripped


class RecommendProfileInput(_Input):
    question: str = Field(
        min_length=1,
        max_length=4000,
        description=(
            "利用者の質問（日本語。指示語・省略は前の質問で補い、それだけで意味の通る文にする）"
        ),
    )
    profile_ids: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=PROFILE_SCOPE_MAX_IDS,
        description="推薦の候補にする業務プロファイルの ID（省略時は使えるすべて）",
    )

    _validate_question = field_validator("question")(_require_question)

    @field_validator("profile_ids")
    @classmethod
    def _validate_profile_ids(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return list(dict.fromkeys(_require_profile_id(item) for item in value))


class RequirementAnswerInput(_Input):
    """SQL の生成前の確認の項目への利用者の答え。"""

    # SQL の生成前の確認（#1441）。

    item_id: str = Field(
        min_length=1,
        max_length=128,
        description="答える確認の項目の ID（前の結果の requirement.question.item_id）",
    )
    selected_candidate_ids: list[str] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "利用者が選んだ候補の ID（requirement.question.candidates の id）。"
            "候補に無い答えは free_text に入れる"
            "（selected_candidate_ids と free_text はどちらか一方）"
        ),
    )
    free_text: str = Field(
        default="", max_length=2000, description="候補に無い利用者の答え（利用者の言葉のまま）"
    )

    @model_validator(mode="after")
    def _require_answer(self) -> RequirementAnswerInput:
        # 画面の回答前の確認と同じく、候補を選ぶか自由入力のどちらか一方で答える。
        if bool(self.selected_candidate_ids) == bool(self.free_text.strip()):
            raise ValueError(
                "selected_candidate_ids か free_text のどちらか一方だけを指定してください。"
            )
        return self


class QueryInput(_Input):
    question: str = Field(min_length=1, max_length=4000, description="利用者の質問（日本語）")

    _validate_question = field_validator("question")(_require_question)
    profile_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "使う業務プロファイルの ID。ID を推測で作らない。省略したとき・使えない ID のときは、"
            "使える業務プロファイルが 1 つならそれを、そうでなければ質問から推薦したものを使い、"
            "auto_selected_profile に示す（決まらなければ候補つきのエラー）。"
            "requirement_session_id を渡すときは省略してよい（その確認の業務プロファイル）"
        ),
    )
    _validate_profile_id = field_validator("profile_id")(_optional_profile_id)
    row_limit: int = Field(
        default=DEFAULT_ROW_LIMIT, ge=1, le=MAX_ROW_LIMIT, description="取得する行数の上限"
    )
    wait_seconds: int = Field(
        default=40,
        ge=0,
        le=MAX_WAIT_SECONDS,
        description="結果を待つ秒数。過ぎたら pending / running のまま job_id を返す",
    )
    requirement_session_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description=(
            "前の nl2sql_query が needs_clarification で返した requirement.session_id。"
            "確認の続き（answer を付ける）と SQL の生成に使う。省略すると新しく確かめ始める"
        ),
    )
    answer: RequirementAnswerInput | None = Field(
        default=None,
        description=(
            "requirement.question への利用者の答え（requirement_session_id と一緒に渡す）。"
            "利用者が答えていない問いを推測で埋めない"
        ),
    )
    skip_clarification: bool = Field(
        default=False,
        description=(
            # 確認せずに SQL を生成する（#1839）。
            "利用者が確認せずに SQL を生成するよう選んだとき true。確認事項を聞かず、"
            "要件を反映できれば反映し、できなければ確認を使わずに作る（requirement.status は"
            " skipped）。利用者が選んでいないのに true にしない。answer と一緒には指定しない"
        ),
    )

    @model_validator(mode="after")
    def _answer_needs_session(self) -> QueryInput:
        if self.answer is not None and self.requirement_session_id is None:
            raise ValueError("answer は requirement_session_id と一緒に指定してください。")
        if self.answer is not None and self.skip_clarification:
            raise ValueError("answer と skip_clarification は一緒に指定できません。")
        return self


class CancelRequirementInput(_Input):
    requirement_session_id: str = Field(
        min_length=1,
        max_length=128,
        description="閉じる SQL の生成前の確認の session（nl2sql_query の requirement.session_id）",
    )


class GetJobInput(_Input):
    job_id: str = Field(min_length=1, max_length=128)
    wait_seconds: int = Field(
        default=0,
        ge=0,
        le=MAX_WAIT_SECONDS,
        description=(
            "完了を待つ秒数（0 はすぐ返す）。pending / running の間は 40 程度を渡し、"
            "終わるまでの呼び出しの回数を減らす"
        ),
    )


class CancelJobInput(_Input):
    job_id: str = Field(min_length=1, max_length=128, description="nl2sql_query の job_id")


class VersionedOutput(BaseModel):
    """ツールの出力の共通部分（出力の形の版）。"""

    schema_version: int = Field(
        default=MCP_OUTPUT_SCHEMA_VERSION, description="出力の形の版（変わったら上がる）。"
    )


class ProfileItem(BaseModel):
    id: str
    name: str
    category: str
    description: str


class ListProfilesOutput(VersionedOutput):
    profiles: list[ProfileItem]


class RecommendationCandidate(BaseModel):
    id: str
    name: str
    reason: str | None = None
    score: float | None = None


class RecommendProfileOutput(VersionedOutput):
    recommended_profile_id: str | None
    rewritten_question: str | None
    candidates: list[RecommendationCandidate]


class QueryStatus(StrEnum):
    """`nl2sql_query` / `nl2sql_get_job` の状態（ジョブの状態か、生成前の確認で止めた状態）。"""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"
    # SQL を作る前に、利用者に確かめる項目がある（#1441）。
    NEEDS_CLARIFICATION = "needs_clarification"
    # この業務プロファイルの範囲では SQL を作れない（表をつなげない・SELECT / FROM が決まらない）。
    UNANSWERABLE = "unanswerable"


class RequirementCandidateOutput(BaseModel):
    id: str
    label: str
    description: str = ""
    # 表・列の物理名（論理名を括弧で添える）。
    refs: str = ""


class RequirementQuestionOutput(BaseModel):
    """利用者に確かめる 1 つの項目（表・列は物理名）。"""

    item_id: str
    section: str = Field(
        description=(
            "SQL のどの部分か（select / from / join / where / period / group_by / order_by / "
            "limit / subquery / other）"
        )
    )
    label: str
    question: str
    reason: str = ""
    answer_kind: str = Field(description="single_select / multi_select / free_text")
    candidates: list[RequirementCandidateOutput] = Field(default_factory=list)
    # AI の今の理解（あれば）。
    value: str = ""
    refs: str = ""


class RequirementOutput(BaseModel):
    """SQL の生成前の確認の状態（#1441。画面の回答前の確認と同じ session）。"""

    session_id: str
    status: str = Field(
        description=(
            "needs_answer / ready / unanswerable / applied / cancelled / skipped"
            "（OCI Enterprise AI が無い構成・業務プロファイルの方針が"
            "「前提を示して SQL を生成する」・skip_clarification で確かめなかった）"
        )
    )
    question: RequirementQuestionOutput | None = None
    remaining_count: int = 0
    answered_count: int = 0
    select_from_clear: bool = False
    minimum_missing: list[str] = Field(default_factory=list)
    # 確かめた要件のまとめと、それを書き直した質問の文（表・列は物理名）。
    summary: str = ""
    clarified_question: str = ""
    sql_skeleton: str = ""
    message: str = ""


class CancelRequirementOutput(VersionedOutput):
    requirement_session_id: str
    status: str = Field(description="cancelled（閉じた）/ closed（もう閉じていた）")


CancelJobResult = Literal["cancelled", "already_cancelled", "already_finished"]
# 取消の結果の文（#3318。Agent の agent_cancel_run と同じ語彙）。
_CANCEL_JOB_MESSAGES: dict[str, str] = {
    "cancelled": "ジョブの取消を受け付けました。実行中の段階の区切りで止まります。",
    "already_cancelled": "ジョブはすでに取り消されています。",
    "already_finished": (
        "ジョブはすでに終わっているため、取り消していません。nl2sql_get_job で結果を読めます。"
    ),
}


class CancelJobOutput(VersionedOutput):
    job_id: str
    status: QueryStatus = Field(description="取消の後のジョブの状態。")
    result: CancelJobResult = Field(
        description=(
            "cancelled=取消を受け付けた（止まると status が error・error_code が JOB_CANCELLED）/ "
            "already_cancelled=すでに取り消されていた / "
            "already_finished=終わっていたため変えていない（status が done / error）。"
        )
    )
    message: str


class AutoSelectedProfile(BaseModel):
    """profile_id を省略・誤ったときに自動で選んだ業務プロファイル（#2355）。"""

    id: str
    name: str
    requested_id: str | None = Field(
        default=None, description="渡した profile_id（省略したときは null）"
    )
    method: Literal["only_usable", "recommended"] = Field(
        description="only_usable=使えるのが 1 つだけ / recommended=質問から推薦した"
    )


class Nl2SqlJobResult(VersionedOutput):
    """契約の `Nl2SqlJobResult`（Agent の `external_nl2sql_*` がそのまま通す）。"""

    # SQL の生成前の確認で止めた（needs_clarification / unanswerable）ときはジョブを作らない
    # ので null。
    job_id: str | None = None
    status: QueryStatus
    profile_id: str | None = None
    generated_sql: str | None = None
    executable_sql: str | None = None
    explanation: str | None = None
    is_safe: bool | None = None
    safety_issues: list[str] = Field(default_factory=list)
    safety_check: Literal["checked", "skipped"] | None = Field(
        default=None,
        description=(
            "SQL の安全確認。checked（確かめた）/ skipped（Select AI・Select AI Agent が生成した "
            "SELECT 文のため省略。業務プロファイルの表・列の範囲の照合も行わない）"
        ),
    )
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    returned_count: int = 0
    total: int | None = None
    has_more: bool = False
    truncated: bool = False
    history_id: str | None = None
    error_code: str | None = Field(
        default=None,
        description=(
            "失敗の分類。status が error のときは必ず入る。Oracle のエラーなら ORA-xxxxx / "
            "DPY-xxxx、それ以外は SQL_BLOCKED（安全性の判定で止めた）・SQL_EXECUTION_FAILED・"
            "SCHEMA_CATALOG_EMPTY・JOB_CANCELLED・JOB_INTERRUPTED・NL2SQL_JOB_FAILED・"
            "NL2SQL_PROFILE_REVALIDATION_REQUIRED（公開の後に業務プロファイルか Schema が変わり、"
            "管理者が業務定義を再検証して公開するまで再試行しても直らない）、"
            "OCI Enterprise AI の HTTP の失敗なら ENTERPRISE_AI_AUTHORIZATION_FAILED・"
            "ENTERPRISE_AI_AUTHENTICATION_FAILED・ENTERPRISE_AI_MODEL_NOT_FOUND"
            "（設定の誤りで、再試行しても直らない）・ENTERPRISE_AI_RATE_LIMITED・"
            "ENTERPRISE_AI_UNAVAILABLE・ENTERPRISE_AI_REQUEST_REJECTED など"
        ),
    )
    error_message: str | None = None
    # SQL の生成前の確認の状態（nl2sql_query だけ。#1441）。
    requirement: RequirementOutput | None = None
    auto_selected_profile: AutoSelectedProfile | None = Field(
        default=None,
        description=(
            "nl2sql_query の profile_id を省略したか、使えない ID を渡したときに自動で選んだ"
            "業務プロファイル（それ以外は null）"
        ),
    )
    # 回答の注意書き（#2405）。API キーで呼んだ外部のクライアントにだけ返す（Agent がサービス
    # トークンで呼んだときは返さず、Agent の回答の末尾には Agent が付ける 1 つだけを出す）。
    notice: str | None = Field(
        default=None, description="利用者に回答を示すときは、末尾にこの文をそのまま添える。"
    )


def _others_content_forbidden() -> McpToolError:
    """system_admin が他の利用者の確認・会話に内容を足そうとしたときのツールのエラー。

    code は 3 製品の MCP で同じ（#3244）。
    """
    return McpToolError(
        OTHERS_CONTENT_FORBIDDEN_CODE,
        OTHERS_CONTENT_FORBIDDEN_MESSAGE,
        status=OTHERS_CONTENT_FORBIDDEN_STATUS,
    )


def _translate_domain_errors[A, R](
    handler: Callable[[A], Awaitable[R]],
) -> Callable[[A], Awaitable[R]]:
    """画面では例外 handler が返す業務エラーを、ツールの `isError` に変換する。"""

    @wraps(handler)
    async def wrapped(arguments: A) -> R:
        try:
            await run_sync_io(nl2sql_service.ensure_persistence_available)
            return await handler(arguments)
        except (Nl2SqlPersistenceUnavailable, Nl2SqlRepositoryOperationFailed) as exc:
            raise McpToolError(exc.reason_code, exc.public_message) from exc
        except SchemaCatalogEmptyError as exc:
            raise McpToolError(SCHEMA_CATALOG_EMPTY_ERROR_CODE, str(exc)) from exc
        except OthersContentForbiddenError as exc:
            # 他の利用者の会話・実行に内容を足す system_admin（画面の 403 と同じ。code は 3 製品の
            # MCP で同じ。#3244）。
            raise _others_content_forbidden() from exc
        except SelectAiCredentialMissingError as exc:
            # Select AI / Select AI Agent の生成は Credential が前提（画面の 409 と同じ。#1688）。
            raise McpToolError(exc.code, select_ai_credential_missing_message(exc)) from exc
        except OntologyServiceError as exc:
            # SQL の生成前の確認の業務エラー（Schema が読めない・LLM が使えない・session の
            # 終了など）。
            message = exc.message_ja
            finding_codes = getattr(exc, "finding_codes", None)
            if finding_codes:
                message = f"{message} 対象: {', '.join(str(item) for item in finding_codes)}"
            # SQL の生成前の確認の LLM の失敗は、種類・HTTP ステータス・モデル ID・所要時間など
            # を添える（#1715。secret・prompt は入れない）。
            details = getattr(exc, "details", None)
            raise McpToolError(exc.code, message, details=details or None) from exc

    return wrapped


def _actor(request: Request) -> tuple[str, bool]:
    principal = principal_from_request(request)
    if principal is None:
        # ローカル実行（PLATFORM_AUTH_MODE=local）は画面の route と同じく actor 制約を外す。
        return "", False
    return principal.user_uuid, principal.is_system_admin


def _job_origin(request: Request) -> JobOrigin:
    """MCP の呼び出しのきっかけ（実行履歴の種類。#1986）。

    API キー（`request.state.api_key`。外部の MCP クライアント）はキーの名前、サービストークン
    （`request.state.service_token_claims`。Agent の Run）は claim の `run_source`（品質評価の Run
    なら `evaluation`）と `run_id`。署名した claim だけを読み、引数（利用者が書ける値）からは
    決めない。local の認証（token を検証しない）はどちらも無いので `mcp` になる。
    """
    api_key = getattr(request.state, "api_key", None)
    claims = getattr(request.state, "service_token_claims", None)
    return JobOrigin.for_mcp(
        api_key_name=getattr(api_key, "name", None),
        service_token_claims=claims if api_key is None and isinstance(claims, dict) else None,
    )


def _cancel_via(request: Request) -> dict[str, str]:
    """取消の要求に残す入口（MCP と、API キーの ID か Agent の Run の ID。#3318）。

    署名した claim と認証の結果だけを読む（引数からは決めない）。
    """
    via = {"via": "mcp"}
    api_key = getattr(request.state, "api_key", None)
    if api_key is not None:
        key_id = getattr(api_key, "key_id", None)
        if key_id:
            via["api_key_id"] = str(key_id)
        return via
    claims = getattr(request.state, "service_token_claims", None)
    run_id = claims.get("run_id") if isinstance(claims, dict) else None
    if isinstance(run_id, str) and run_id:
        via["agent_run_id"] = run_id
    return via


def _cancel_job_result(job: JobData, *, already_requested: bool) -> CancelJobResult:
    """取消の結果（終わったジョブはエラーにせず値で返す。Agent の agent_cancel_run と同じ）。"""
    if job.status not in _TERMINAL_STATUSES:
        return "already_cancelled" if already_requested else "cancelled"
    if job.error_code == JOB_CANCELLED_ERROR_CODE:
        return "already_cancelled"
    return "already_finished"


def _job_error_message(job: JobData) -> str | None:
    """画面は失敗の文と詳細（例外・ORA-… の元の文）を分けて出す（#1072）。MCP の呼び出し側
    （Agent）は文を 1 つで受け取るため、原因を判断できるよう詳細を文の後ろに付けて返す。"""
    if job.error_message and job.error_detail:
        return f"{job.error_message}\n詳細: {job.error_detail}"
    return job.error_message


def called_with_api_key(request: Request) -> bool:
    """利用者の API キー（`prak_…`）で呼んだ外部の MCP クライアントか（#2405）。

    Agent のサービストークン・local（認証なし）の呼び出しは False。
    """
    return getattr(request.state, "api_key", None) is not None


def _job_result(job: JobData, *, include_notice: bool = False) -> Nl2SqlJobResult:
    """job の応答。`include_notice` なら、答えた job（done）の回答の注意書きを付ける。"""
    result = job.result
    output = Nl2SqlJobResult(
        job_id=job.job_id,
        status=QueryStatus(job.status.value),
        profile_id=job.profile_id or None,
        # 旧い snapshot など、error_code を持たない失敗も分類できるようにする（#847）。
        error_code=job.error_code
        or (JOB_FAILED_ERROR_CODE if job.status == JobStatus.ERROR else None),
        error_message=_job_error_message(job),
    )
    if result is None:
        return output
    safety = result.safety
    issues = [safety.blocked_reason] if safety.blocked_reason else []
    output.generated_sql = result.generated_sql or None
    output.executable_sql = result.executable_sql or None
    output.explanation = result.explanation or None
    output.is_safe = safety.is_safe
    output.safety_issues = [*issues, *safety.warnings]
    output.safety_check = "checked" if safety.safety_checked else "skipped"
    output.columns = list(result.results.columns)
    output.rows = list(result.results.rows)
    output.returned_count = result.results.returned_count or len(result.results.rows)
    output.total = result.results.total
    output.has_more = result.results.has_more
    output.truncated = result.results.truncated
    output.history_id = result.history_id or None
    if include_notice and job.status == JobStatus.DONE:
        output.notice = result.notice
    return output


def _requirement_output(data: QuerySessionData) -> RequirementOutput:
    """SQL の生成前の確認の session を、呼び出し元（Agent）に返す形にする（表・列は物理名）。"""

    session = data.session
    state = data.requirement_state
    draft = session.requirement
    if session.status == QuerySessionStatus.CANCELLED:
        status = "cancelled"
    elif session.requirement_applied_at is not None:
        status = "applied"
    elif state is not None:
        status = state.status.value
    else:
        status = RequirementPhase.UNANSWERABLE.value
    item = state.current_item if state is not None else None
    question = None
    if item is not None and status == RequirementPhase.NEEDS_ANSWER.value:
        question = RequirementQuestionOutput(
            item_id=item.id,
            section=item.section.value,
            label=item.label_ja,
            question=item.question_ja or item.label_ja,
            reason=item.reason_ja,
            answer_kind=item.answer_kind.value,
            candidates=[
                RequirementCandidateOutput(
                    id=candidate.id,
                    label=candidate.label_ja,
                    description=candidate.description_ja,
                    refs=candidate.refs_ja,
                )
                for candidate in item.candidates
            ],
            value=item.value_ja,
            refs=item.refs_ja,
        )
    return RequirementOutput(
        session_id=session.id,
        status=status,
        question=question,
        remaining_count=len(state.unresolved_items) if state is not None else 0,
        answered_count=state.answered_count if state is not None else 0,
        select_from_clear=state.select_from_clear if state is not None else False,
        minimum_missing=list(state.minimum_missing_ja) if state is not None else [],
        summary=state.summary_ja if state is not None else "",
        clarified_question=draft.clarified_question_ja if draft is not None else "",
        sql_skeleton=(state.sql_skeleton if state is not None else "")
        or (draft.sql_skeleton if draft is not None else ""),
        message=state.message_ja if state is not None else "",
    )


def _profile_assumes(profile_id: str) -> bool:
    """業務プロファイルの「曖昧な質問の扱い」が「前提を示して SQL を生成する」か（#1839）。"""

    try:
        profile = Nl2SqlProfile.model_validate(nl2sql_service.get_profile(profile_id))
    except (KeyError, ValueError):
        return False
    return profile.clarification_policy == "assume"


def _requirement_ready(data: QuerySessionData) -> bool:
    """SQL の生成に進める（確認の要る項目が無く、SELECT と FROM が明確）か。

    画面は再検査の回数の上限に達したら残った項目を AI に任せて進められるが、MCP（Agent）は
    確認の要るものが無くなるまで 1 問ずつ聞く（#1441）。
    """

    state = data.requirement_state
    return (
        state is not None
        and state.status == RequirementPhase.READY
        and state.select_from_clear
        and not state.unresolved_items
        and state.can_apply
    )


def _profile_scope_forbidden(profile_id: str) -> SecurityApiError:
    return SecurityApiError(
        403,
        f"業務プロファイル「{profile_id}」は、この呼び出し元（業務 Agent）の"
        "データの範囲の外です。nl2sql_list_profiles で使える業務プロファイルを確かめてください。",
        code=PROFILE_SCOPE_FORBIDDEN_CODE,
    )


def build_mcp_server(request: Request) -> McpServer:
    """リクエストの利用者で判定するツールを持つ MCP サーバーを作る。"""

    def scope() -> frozenset[str] | None:
        """呼び出し元の範囲（サービストークンの claim。#1379）。None は範囲なし。"""
        return service_token_profile_scope(request)

    def allowed_profile_ids() -> set[str] | None:
        """使える業務プロファイル（利用者の権限 ∩ claim）。None は制限なし。"""
        allowed = narrow_to_profile_scope(
            allowed_profile_ids_for_request(request, nl2sql_service), scope()
        )
        return set(allowed) if allowed is not None else None

    def own_job(job_id: str) -> JobData:
        actor_user_uuid, _ = _actor(request)
        try:
            # 画面の「AI 活用」と同じ規則（#1889）: 本人のジョブだけ、利用者（キー・
            # サービストークンの持ち主）が system_admin なら全員のジョブを見られる。製品の管理権限
            # （フィードバック管理など）は理由にしない。
            job = nl2sql_service.get_job(
                job_id,
                actor_user_uuid=actor_user_uuid,
                actor_can_manage=can_view_all_users_data(principal_from_request(request)),
            )
        except PermissionError:
            job = None  # 他人のジョブは存在も明かさない
        if job is None:
            raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND_MESSAGE)
        # 読むたびにその時点の範囲で判定する（OWASP API1。#3318）: 呼び出し元（業務 Agent）の
        # claim の外と、利用者が今は使えない業務プロファイルのジョブは、本人のジョブでも返さない
        # （確認の session の `own_session` と同じ 403）。
        scoped = scope()
        if scoped is not None and job.profile_id not in scoped:
            raise _profile_scope_forbidden(job.profile_id)
        assert_profile_access(request, job.profile_id)
        return job

    def own_session(session_id: str, *, adding: bool = False) -> QuerySessionData:
        """SQL の生成前の確認の session（ジョブと同じく本人だけ、system_admin は全員見られる）。

        `adding`（確認に答える・反映して SQL を作る）は持ち主だけで、見える他の利用者の session
        （system_admin）は 403（#3244）。取り消しは `adding` なし。
        """
        try:
            data = ontology_router.ontology_runtime.get_session(session_id)
        except OntologyNotFoundError:
            data = None
        principal = principal_from_request(request)
        # 持ち主の無い session（認証無効の期間に作ったもの）も本人の物とみなさない（system_admin は
        # 扱える）。
        if (
            data is not None
            and principal is not None
            and not can_access_user_data(principal, data.session.actor_user_uuid.strip())
        ):
            data = None  # 他人の session は存在も明かさない
        if data is None or data.session.clarification_mode != ClarificationMode.GUIDED:
            raise HTTPException(status_code=404, detail=_REQUIREMENT_NOT_FOUND_MESSAGE)
        if (
            adding
            and principal is not None
            and not can_add_user_content(principal, data.session.actor_user_uuid.strip())
        ):
            raise _others_content_forbidden()
        scoped = scope()
        if scoped is not None and data.session.profile_id not in scoped:
            raise _profile_scope_forbidden(data.session.profile_id)
        assert_profile_access(request, data.session.profile_id)
        return data

    async def wait_for_job(job_id: str, wait_seconds: int) -> Nl2SqlJobResult:
        deadline = time.monotonic() + wait_seconds
        while True:
            job = await run_sync_io(own_job, job_id)
            remaining = deadline - time.monotonic()
            if job.status in _TERMINAL_STATUSES or remaining <= 0:
                return _job_result(job, include_notice=called_with_api_key(request))
            await asyncio.sleep(min(JOB_POLL_INTERVAL_SECONDS, remaining))

    @_translate_domain_errors
    async def list_profiles(arguments: ListProfilesInput) -> ListProfilesOutput:
        def run() -> ListProfilesOutput:
            page = nl2sql_service.search_profiles(
                cursor=None,
                limit=arguments.limit,
                query=arguments.query or "",
                include_archived=False,
                allowed_profile_ids=allowed_profile_ids(),
            )
            return ListProfilesOutput(
                profiles=[
                    ProfileItem(
                        id=item.id,
                        name=item.name,
                        category=item.category,
                        description=item.description,
                    )
                    for item in page.items
                ]
            )

        return await run_sync_io(run)

    def recommend_for(question: str, profile_ids: list[str] | None) -> RecommendProfileOutput:
        """質問から業務プロファイルを推薦する（画面と同じしきい値。同期）。"""
        allowed = allowed_profile_ids()
        if profile_ids is not None:
            # 候補の一覧（#1379）。権限・claim の外の ID は候補にしない（積）。
            requested = set(profile_ids)
            allowed = requested if allowed is None else allowed & requested
        if allowed is not None and not allowed:
            return RecommendProfileOutput(
                recommended_profile_id=None, rewritten_question=None, candidates=[]
            )
        try:
            data = nl2sql_service.recommend_profile(
                ProfileRecommendationRequest(question=question),
                allowed_profile_ids=allowed,
            )
        except ValueError as exc:
            if "権限" in str(exc):
                raise profile_access_denied() from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        candidates = [
            RecommendationCandidate(
                id=candidate.profile_id,
                name=candidate.profile_name,
                reason="、".join(candidate.matched_terms) or None,
                score=candidate.score,
            )
            for candidate in data.candidates
            if allowed is None or candidate.profile_id in allowed
        ]
        recommended = data.recommended_profile_id or None
        # 画面と同じく、しきい値未満の推薦は採用しない。
        if data.confidence < data.confidence_threshold:
            recommended = None
        if recommended is not None and allowed is not None and recommended not in allowed:
            recommended = None
        return RecommendProfileOutput(
            recommended_profile_id=recommended,
            rewritten_question=(data.rewritten_question or None) if recommended else None,
            candidates=candidates,
        )

    @_translate_domain_errors
    async def recommend_profile(arguments: RecommendProfileInput) -> RecommendProfileOutput:
        return await run_sync_io(recommend_for, arguments.question, arguments.profile_ids)

    def profile_usable(profile_id: str) -> bool:
        """利用者（権限 ∩ claim）が使える、アーカイブしていない業務プロファイルか（同期）。"""
        allowed = allowed_profile_ids()
        if allowed is not None and profile_id not in allowed:
            return False
        try:
            nl2sql_service.get_profile(profile_id)
        except (KeyError, ValueError):
            return False
        return True

    def select_profile(question: str, requested: str | None) -> AutoSelectedProfile:
        """profile_id を省略・誤ったときの業務プロファイル（#2355。同期）。

        使えるものが 1 つならそれ、そうでなければ質問の推薦が決めたもの。決まらなければ候補と
        次の手の付いたエラー。数えるのは利用者の権限 ∩ claim の中だけ。
        """
        usable = nl2sql_service.search_profiles(
            cursor=None,
            limit=2,
            query="",
            include_archived=False,
            allowed_profile_ids=allowed_profile_ids(),
        ).items
        if len(usable) == 1:
            only = usable[0]
            return AutoSelectedProfile(
                id=only.id, name=only.name, requested_id=requested, method="only_usable"
            )
        message = (
            f"業務プロファイル「{requested}」は見つからないか、使えません。"
            if requested is not None
            else ""
        )
        details: dict[str, Any] = {"profile_id": requested, "next_tools": []}
        if not usable:
            raise McpToolError(
                PROFILE_NOT_SELECTED_CODE,
                message
                + "使える業務プロファイルがありません（管理者に利用の範囲を確かめてください）。",
                status=404,
                details=details,
            )
        recommendation = recommend_for(question, None)
        chosen = next(
            (
                item
                for item in recommendation.candidates
                if item.id == recommendation.recommended_profile_id
            ),
            None,
        )
        if chosen is not None:
            return AutoSelectedProfile(
                id=chosen.id, name=chosen.name, requested_id=requested, method="recommended"
            )
        shown = recommendation.candidates[:SELECTION_CANDIDATES_SHOWN]
        details["next_tools"] = ["nl2sql_list_profiles"]
        if shown:
            details["candidates"] = [{"id": item.id, "name": item.name} for item in shown]
            listed = "、".join(f"「{item.name}」（id: {item.id}）" for item in shown)
            message += (
                f"質問からは業務プロファイルを 1 つに決められませんでした。候補: {listed}。"
                "利用者に確かめるか、合うものの ID を profile_id に渡して呼び直してください。"
            )
        else:
            message += (
                "質問からは業務プロファイルを決められませんでした。nl2sql_list_profiles の一覧から"
                "選び、その ID を profile_id に渡して呼び直してください。"
            )
        raise McpToolError(PROFILE_NOT_SELECTED_CODE, message, status=404, details=details)

    async def resolve_query_profile(
        arguments: QueryInput,
    ) -> tuple[str, AutoSelectedProfile | None]:
        """nl2sql_query で使う業務プロファイルの ID と、自動で選んだならその内容（#2355）。"""
        requested = arguments.profile_id
        if arguments.requirement_session_id is not None:
            # 確認の続きは、その確認の業務プロファイル（違う ID は呼び出しの先で 400）。
            if requested is not None:
                return requested, None
            data = await run_sync_io(own_session, arguments.requirement_session_id, adding=True)
            return data.session.profile_id, None
        scoped = scope()
        if requested is not None and scoped is not None and requested not in scoped:
            # 呼び出し元（業務 Agent）の範囲の外は、選び直さずに範囲の外と返す（#1379）。
            raise _profile_scope_forbidden(requested)
        if requested is not None and await run_sync_io(profile_usable, requested):
            return requested, None
        selected = await run_sync_io(select_profile, arguments.question, requested)
        return selected.id, selected

    async def start_and_wait(job_request: JobCreateRequest, wait_seconds: int) -> Nl2SqlJobResult:
        actor_user_uuid, actor_is_system_admin = _actor(request)
        try:
            created = await run_sync_io(
                nl2sql_service.start_job,
                job_request,
                actor_user_uuid=actor_user_uuid,
                actor_is_system_admin=actor_is_system_admin,
                # 実行履歴の種類（MCP・品質評価）。呼び出し元の認証の結果から決める（#1986）。
                origin=_job_origin(request),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await wait_for_job(created.job_id, wait_seconds)

    @_translate_domain_errors
    async def query(arguments: QueryInput) -> Nl2SqlJobResult:
        profile_id, selected = await resolve_query_profile(arguments)
        result = await run_query(arguments, profile_id)
        result.auto_selected_profile = selected
        return result

    async def run_query(arguments: QueryInput, profile_id: str) -> Nl2SqlJobResult:
        scoped = scope()
        if scoped is not None and profile_id not in scoped:
            raise _profile_scope_forbidden(profile_id)
        assert_profile_access(request, profile_id)
        actor_user_uuid, actor_is_system_admin = _actor(request)
        runtime = ontology_router.ontology_runtime

        def requirement_step() -> QuerySessionData:
            """SQL の生成前の確認を始める・続ける（#1441。SQL はまだ作らない）。"""
            if arguments.requirement_session_id is None:
                # 業務プロファイルは呼び出し元が profile_id で明示して選んだ（推薦の確認を省く）。
                return runtime.create_session(
                    QuerySessionApiCreate(
                        question=arguments.question,
                        profile_id=profile_id,
                        row_limit=arguments.row_limit,
                        clarification_mode=ClarificationMode.GUIDED,
                    ),
                    actor_user_uuid=actor_user_uuid,
                    actor_is_system_admin=actor_is_system_admin,
                    profile_confirmed=True,
                )
            data = own_session(arguments.requirement_session_id, adding=True)
            if data.session.profile_id != profile_id:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "requirement_session_id の業務プロファイルと profile_id が違います。"
                        "同じ profile_id で呼び直してください。"
                    ),
                )
            if data.session.requirement_applied_at is not None:
                return data
            if data.session.status in _CLOSED_SESSION_STATUSES:
                raise HTTPException(status_code=409, detail=_REQUIREMENT_CLOSED_MESSAGE)
            if arguments.answer is None:
                return data
            answer = arguments.answer
            return runtime.answer_clarification(
                data.session.id,
                ClarificationAnswerRequest(
                    # 会話は 1 問ずつ進むので、今の版に答える（古い版の上書きは session が防ぐ）。
                    base_version=data.session.current_intent_version,
                    item_id=answer.item_id,
                    selected_candidate_ids=list(dict.fromkeys(answer.selected_candidate_ids)),
                    free_text=answer.free_text.strip(),
                ),
            )

        async def confirm_requirement(
            requirement_trace: ObservedSpan,
        ) -> Nl2SqlJobResult | tuple[JobCreateRequest, RequirementOutput]:
            """回答前の確認の工程（#1441）。SQL を作らずに返す結果か、作るジョブと確認の結果を返す。

            ジョブは呼び出し元が確認の span の外で作る（確認の工程とジョブの工程を兄弟にする。
            #3477）。
            """
            try:
                data = await run_sync_io(requirement_step)
            except RequirementAiUnavailableError as exc:
                if (
                    exc.code != REQUIREMENT_AI_NOT_CONFIGURED_CODE
                    or arguments.requirement_session_id
                ):
                    raise
                # OCI Enterprise AI を設定していない構成（Select AI だけ・ローカルの決定論）は、
                # SQL の生成前の確認をせずに SQL を作る。確かめていないことを requirement で返す。
                requirement_trace.set_status("skipped")
                return (
                    JobCreateRequest(
                        question=arguments.question,
                        profile_id=profile_id,
                        row_limit=arguments.row_limit,
                    ),
                    RequirementOutput(
                        session_id="", status="skipped", message=_REQUIREMENT_SKIPPED_MESSAGE
                    ),
                )
            if data.session.requirement_applied_at is None:
                # 業務プロファイルの方針が「前提を示して SQL を生成する」か、利用者が「確認せずに
                # SQL を生成」を選んだ（skip_clarification）なら聞かない（#1839）。要件を反映できる
                # （確認事項が無い・再検査の上限で残りを AI に任せられる）なら反映し、できないなら
                # 確認を使わずに作る（画面の「確認せずに SQL を生成」と同じ）。
                assume = arguments.skip_clarification or await run_sync_io(
                    _profile_assumes, profile_id
                )
                state = data.requirement_state
                if (
                    assume
                    and not _requirement_ready(data)
                    and not (state is not None and state.can_apply)
                ):
                    if data.session.status not in _CLOSED_SESSION_STATUSES:
                        await run_sync_io(runtime.cancel_session, data.session.id)
                    return (
                        JobCreateRequest(
                            question=arguments.question,
                            profile_id=profile_id,
                            row_limit=arguments.row_limit,
                        ),
                        RequirementOutput(
                            session_id="",
                            status="skipped",
                            message=(
                                _REQUIREMENT_USER_SKIPPED_MESSAGE
                                if arguments.skip_clarification
                                else _REQUIREMENT_ASSUMED_MESSAGE
                            ),
                        ),
                    )
                if not _requirement_ready(data) and not assume:
                    requirement = _requirement_output(data)
                    if requirement.question is not None:
                        # 確認の要る項目がある: SQL を作らずに 1 問だけ返す。
                        return Nl2SqlJobResult(
                            status=QueryStatus.NEEDS_CLARIFICATION,
                            profile_id=profile_id,
                            requirement=requirement,
                        )
                    # 聞く項目が無いのに SELECT / FROM が決まらない・範囲で答えられない: 閉じて
                    # 返す。
                    if data.session.status not in _CLOSED_SESSION_STATUSES:
                        await run_sync_io(runtime.cancel_session, data.session.id)
                    return Nl2SqlJobResult(
                        status=QueryStatus.UNANSWERABLE,
                        profile_id=profile_id,
                        error_code=REQUIREMENT_UNANSWERABLE_CODE,
                        error_message=requirement.message or None,
                        requirement=requirement.model_copy(update={"status": "unanswerable"}),
                    )
                data = await run_sync_io(
                    runtime.apply_requirement,
                    data.session.id,
                    RequirementApplyRequest(base_version=data.session.current_intent_version),
                )
            draft = data.session.requirement
            if draft is None:  # pragma: no cover - guided の session は必ず下書きを持つ
                raise HTTPException(status_code=409, detail="回答前の確認の要件がありません。")
            # 画面と同じく、確かめた要件（構造化した要件と書き直した質問の文）で job を作る。質問の
            # 文は利用者の質問のままにし、書き直した文は生成にだけ使う（`generation_question`。
            # #1727）。生成の方式は指定せず、ジョブの作成が業務プロファイルの既定の生成方式にする
            # （#1524）。use_ontology_context 等は画面と同じく JobCreateRequest の既定で作る。
            # row_limit は必ず渡す（None だと全件を取得する）。
            job_request = _with_requirement(
                JobCreateRequest(
                    question=data.session.original_question,
                    profile_id=profile_id,
                    row_limit=arguments.row_limit,
                    requirement_session_id=data.session.id,
                ),
                getattr(request.state, "principal", None),
            )
            return job_request, _requirement_output(data)

        started = time.monotonic()
        # 回答前の確認（LLM の要件の下書き・見直し）は工程の span の中で行う。呼び出し元（Agent）の
        # 処理の経過で、確認の LLM の呼び出しがこの工程の子になる（#3477）。
        with observed_span(
            REQUIREMENT_SPAN,
            attributes={"pr.stage": REQUIREMENT_STAGE},
            display=SpanDisplay(kind=REQUIREMENT_STAGE),
        ) as requirement_trace:
            decided = await confirm_requirement(requirement_trace)
        if isinstance(decided, Nl2SqlJobResult):
            return decided
        job_request, requirement = decided
        # SQL の生成前の確認（LLM）にかかった分だけ、ジョブを待つ秒数を減らす（1 回の呼び出しの
        # 長さを「SQL の生成前の確認 + wait_seconds」にしない。終わらなければ job_id を返し、
        # 続きは nl2sql_get_job）。
        remaining = max(0, arguments.wait_seconds - int(time.monotonic() - started))
        result = await start_and_wait(job_request, remaining)
        result.requirement = requirement
        return result

    @_translate_domain_errors
    async def get_job(arguments: GetJobInput) -> Nl2SqlJobResult:
        return await wait_for_job(arguments.job_id, arguments.wait_seconds)

    @_translate_domain_errors
    async def cancel_job(arguments: CancelJobInput) -> CancelJobOutput:
        def run() -> CancelJobOutput:
            # 本人か system_admin（#3244 の運用の操作）・範囲の判定は nl2sql_get_job と同じ。
            own_job(arguments.job_id)
            actor_user_uuid, _ = _actor(request)
            try:
                outcome = nl2sql_service.cancel_job(
                    arguments.job_id,
                    actor_user_uuid=actor_user_uuid,
                    actor_can_manage=can_view_all_users_data(principal_from_request(request)),
                    via=_cancel_via(request),
                )
            except PermissionError:
                outcome = None
            if outcome is None:
                raise HTTPException(status_code=404, detail=_JOB_NOT_FOUND_MESSAGE)
            result = _cancel_job_result(outcome.job, already_requested=outcome.already_requested)
            return CancelJobOutput(
                job_id=outcome.job.job_id,
                status=QueryStatus(outcome.job.status.value),
                result=result,
                message=_CANCEL_JOB_MESSAGES[result],
            )

        return await run_sync_io(run)

    @_translate_domain_errors
    async def cancel_requirement(arguments: CancelRequirementInput) -> CancelRequirementOutput:
        def run() -> CancelRequirementOutput:
            runtime = ontology_router.ontology_runtime
            data = own_session(arguments.requirement_session_id)
            if data.session.status in _CLOSED_SESSION_STATUSES:
                return CancelRequirementOutput(
                    requirement_session_id=data.session.id, status="closed"
                )
            runtime.cancel_session(data.session.id)
            return CancelRequirementOutput(
                requirement_session_id=data.session.id, status="cancelled"
            )

        return await run_sync_io(run)

    tools = [
        McpTool(
            name="nl2sql_list_profiles",
            title="業務プロファイルの一覧",
            description="利用者が使える業務プロファイル（SQL を生成する対象の範囲）を一覧します。",
            input_model=ListProfilesInput,
            handler=list_profiles,
            output_model=ListProfilesOutput,
            permissions=(frozenset({PROFILE_READ_PERMISSION}),),
        ),
        McpTool(
            name="nl2sql_recommend_profile",
            title="業務プロファイルの推薦",
            description=(
                "質問に合う業務プロファイルを推薦します。推薦できないときは null で、candidates に "
                "score の高い順の候補（説明・表の名前と質問の語が重なるもの）を返します。"
                "profile_ids を渡すと、その中からだけ推薦します。"
                # 会話の続きは前のターンの業務プロファイルを使う（#1880。question の書き方は
                # question の説明にある）。
                "同じ会話の続きの質問では前のターンと同じ業務プロファイルを使い、推薦し直すのは"
                "話題が変わったときだけです。"
            ),
            input_model=RecommendProfileInput,
            handler=recommend_profile,
            output_model=RecommendProfileOutput,
            permissions=(frozenset({QUERY_GENERATE_PERMISSION}),),
        ),
        McpTool(
            name="nl2sql_query",
            title="業務データの照会",
            description=(
                "質問から SELECT SQL を生成して実行し、結果の行を返します。"
                "SQL を作る前に、足りない情報・明確でない情報を確かめます（SQL の生成前の確認）。"
                "確かめる項目があれば SQL を作らずに status=needs_clarification と "
                "requirement.question（質問・候補・理由。表・列は物理名）を 1 つだけ返すので、"
                "利用者に聞いてから requirement_session_id と answer を付けて呼び直します。"
                "wait_seconds 内に終わらなければ job_id を返すので、"
                "nl2sql_get_job で続きを取ります。"
            ),
            input_model=QueryInput,
            handler=query,
            output_model=Nl2SqlJobResult,
            permissions=(
                frozenset({QUERY_GENERATE_PERMISSION}),
                frozenset({SQL_EXECUTE_PERMISSION}),
            ),
            read_only=False,
        ),
        McpTool(
            name="nl2sql_get_job",
            title="照会の結果の取得",
            description=(
                "nl2sql_query で作ったジョブの状態と結果を返します"
                "（本人のジョブだけ。利用者が system_admin なら全員のジョブ）。"
                "wait_seconds の間、完了を待ってから返します。"
            ),
            input_model=GetJobInput,
            handler=get_job,
            output_model=Nl2SqlJobResult,
            permissions=(frozenset({QUERY_GENERATE_PERMISSION}),),
        ),
        McpTool(
            name="nl2sql_cancel_job",
            title="照会のジョブの取り消し",
            description=(
                "nl2sql_query の job_id のジョブ（pending / running）を止めます"
                "（本人のジョブだけ。利用者が system_admin なら全員のジョブ）。"
                "利用者が止めると言ったとき、結果を待たないと決めたときに使います。"
            ),
            input_model=CancelJobInput,
            handler=cancel_job,
            output_model=CancelJobOutput,
            permissions=(frozenset({QUERY_GENERATE_PERMISSION}),),
            read_only=False,
            destructive=True,
            idempotent=True,
            open_world=False,
        ),
        McpTool(
            name="nl2sql_cancel_requirement",
            title="照会の確認の取り消し",
            description=(
                "nl2sql_query の SQL の生成前の確認（needs_clarification）を取り消して閉じます"
                "（本人の確認だけ。利用者が system_admin なら全員の確認）。"
                "利用者がデータベースの検索は不要と伝えたときに使います。"
            ),
            input_model=CancelRequirementInput,
            handler=cancel_requirement,
            output_model=CancelRequirementOutput,
            permissions=(frozenset({QUERY_GENERATE_PERMISSION}),),
            read_only=False,
        ),
    ]
    return McpServer(
        name=SERVER_NAME,
        version=get_settings().app_version,
        tools=tools,
        instructions=_INSTRUCTIONS,
    )


def has_any_permission_for(request: Request) -> Callable[[frozenset[str]], bool]:
    principal = principal_from_request(request)
    if principal is None:
        # ローカル実行（PLATFORM_AUTH_MODE=local）は他の API と同じく権限を判定しない。
        return lambda _permissions: True
    return principal.has_any_permission
