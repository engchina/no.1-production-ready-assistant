"""業務 Agent を外部の MCP クライアントに公開する（`POST /api/mcp`。#1874）。

外部の Agent（Claude Code・Codex など）が、業務 Agent を「専門家」として呼ぶためのチャットの
ツールを出す。RAG / NL2SQL と同じ共通の `pr_backend_core.mcp`（Streamable HTTP の JSON 応答）を
使い、認証は共通の API キー（`Authorization: Bearer prak_…`。使える製品に `agent`）。

- 利用者はキーを発行した利用者で、画面と同じ権限（capability）と対象範囲（使える業務 Agent）で
  判定する。キーに業務 Agent の範囲（`resource_scopes["agent"]`）があれば、さらにその範囲だけ
  （範囲の外は 403）。Run・会話の見え方も画面と同じで、キーの持ち主が system_admin なら他の
  利用者の Run を読める。会話を続ける・回答前の確認に答えるのは会話の持ち主だけで、system_admin の
  キーでも他の利用者の会話・Run には 403（`OTHERS_CONTENT_FORBIDDEN`。#3244）。
- ツール: `agent_list`（使える業務 Agent）・`agent_chat`（質問。`thread_id` で会話を続ける）・
  `agent_get_run`（状態・回答・根拠・回答前の確認）・`agent_answer_confirmation`（回答前の確認に
  答える / 確認せずに回答）・`agent_cancel_run`（実行の取消。冪等。#3240）・
  `agent_get_run_observation`（評価・監査の観測。根拠の全件・ツールの呼び出しの経過・利用量。
  #3240）。回答を待つのは `wait_seconds` まで。終わらなければ途中の状態を返し、`agent_get_run` で
  続きを読む。
- MCP から作った Run は無人の実行と同じく、承認の要るツールを呼ばない（#1813 と同じ扱い。
  `runtime.is_mcp_run`）。承認する人が画面にいるとは限らないため。
- キーごとに直近 24 時間に作れる Run の数（`PLATFORM_API_KEY_RUNS_PER_DAY`）。超えたら 429 相当の
  ツールのエラー（`details.retry_after_seconds`）。1 分あたりの呼び出しの回数は共通の認証が数える。
- Run の metadata にキーの ID と名前を残し、実行履歴・監査に「MCP（キーの名前）」として出す。

Agent は MCP を呼ぶ側でもある（RAG / NL2SQL / 外部 MCP）。ここは呼ばれる側で、RAG は Agent を
呼ばない（向きは外部のクライアント → Agent → RAG / NL2SQL）。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from pr_backend_core.mcp import McpServer, McpTool, McpToolError, mcp_http_response
from pr_backend_core.source_text import (
    SOURCE_TEXT_ITEM_MAX_TOKENS as PLATFORM_SOURCE_TEXT_ITEM_MAX_TOKENS,
)
from pr_backend_core.source_text import (
    SOURCE_TEXT_PAGE_BUDGET_TOKENS,
    SOURCE_TEXT_PAGE_LIMIT_DEFAULT,
    SOURCE_TEXT_PAGE_LIMIT_MAX,
    SourceText,
    SourceTextItem,
    fit_source_texts,
    page_source_texts,
)
from pr_backend_core.source_text import (
    SOURCE_TEXT_RESPONSE_BUDGET_TOKENS as PLATFORM_SOURCE_TEXT_RESPONSE_BUDGET_TOKENS,
)
from pr_backend_core.token_estimate import estimate_input_tokens
from pr_system_settings.auth.visibility import (
    OTHERS_CONTENT_FORBIDDEN_CODE,
    OTHERS_CONTENT_FORBIDDEN_MESSAGE,
    OTHERS_CONTENT_FORBIDDEN_STATUS,
)
from pydantic import BaseModel, Field

from app.features.agent import router as agent_api
from app.features.agent.answer_evidence import (
    ANSWER_EVIDENCE_KIND,
    ATTACHMENT_DOCUMENT_PREFIX,
    answer_evidence,
)
from app.features.agent.answer_outcome import ANSWER_OUTCOMES
from app.features.agent.answer_validation import ANSWER_VALIDATION_KIND
from app.features.agent.confirmation import (
    MAX_ITEMS,
    SOURCE_AGENT,
    ConfirmationAnswer,
    ConfirmationAnswerError,
    ConfirmationRequest,
)
from app.features.agent.evidence_access import unreadable_documents
from app.features.agent.run_facts import run_answer_outcome, run_finished_at
from app.features.agent.run_observation import (
    ObservedEvidenceItem,
    ObservedToolStatus,
    ValidationStatus,
    answer_citations,
    observe_run,
    run_agent_version,
    validation_status,
)
from app.features.agent.runtime import (
    AGENT_UNPUBLISHED_MESSAGE,
    CANCEL_VIA_KEY,
    MCP_API_KEY_ID_KEY,
    MCP_API_KEY_NAME_KEY,
    MCP_APPROVAL_SKIPPED_CODE,
    MCP_RUN_SOURCE,
    RUN_SOURCE_KEY,
    AgentNotPublishedError,
    AgentProfile,
    ConfirmationClosedError,
    RunCreateRequest,
    RunEventType,
    RunState,
    RunStatus,
    ThreadNotFoundError,
    agent_unavailable_reason,
    run_answer_notice,
    run_answer_text,
    runtime_repository,
)
from app.features.agent.skill_export import export_skill
from app.features.agent.skills import skill_registry
from app.security.domain import Principal
from app.security.permissions import ADMIN, RUNS_OPERATE, RUNS_VIEW
from app.settings import get_settings

MCP_SERVER_NAME = "production-ready-agent"
# 既定の待ち時間は、MCP のクライアントのツールの待ち時間の既定（Claude Code は 60 秒）より短く
# する。終わらなければ途中の状態を返すので、クライアントの時間切れで結果を失わない。
CHAT_DEFAULT_WAIT_SECONDS = 50
GET_RUN_DEFAULT_WAIT_SECONDS = 30
MAX_WAIT_SECONDS = 300
QUESTION_MAX_CHARS = 8000
# 回答を待つ間隔（テストは短くする）。
wait_poll_seconds = 0.5

# 根拠の抜き出しの長さと件数（呼び出し側のモデルの文脈を膨らませない）。
# RunOutput の形の版（#2738 で段落の印・外した段落・回答の警告を足して 2、#3240 で回答の対応・
# 根拠の ID・引用の印・見られない根拠の数を足して 3、#3321 で根拠の本文を 400 文字の先頭から全文と
# 応答の予算・境界での切り詰めにし、truncated・text_omitted・text_length・excerpt_range・
# header_range・next_offset・sources_text_omitted を足して 4）。
RUN_OUTPUT_SCHEMA_VERSION = 4
# agent_list・agent_skill_export の出力の形の版（3 製品の MCP の出力に版を持たせる。#3240）。
LIST_OUTPUT_SCHEMA_VERSION = 1
SKILL_EXPORT_SCHEMA_VERSION = 1
CANCEL_OUTPUT_SCHEMA_VERSION = 1
# agent_get_run_observation の出力の形の版（#3240。#3262 で根拠の場所に RAG の表のセルの座標の
# table_row・table_cells を足して 2、行の番号の基準 row_basis・表の番号 table_index・CSV の物理行の
# 範囲 source_line_start / source_line_end を足して 3、#3429 で行の番号の基準に解析の表の格子の
# parsed_table を足して 4、#3546 でツールの呼び出しに RAG の縮退（degraded_steps・
# guardrail_degraded・zero_evidence_stage）を、根拠に表の行・列の対応の検査が読めるか
# （table_binding）を足して 5）。
OBSERVATION_SCHEMA_VERSION = 5
# 根拠の件数の上限。本文は件ごとの全文を応答の予算に入れる（3 製品で共通の規則。値と決め方は
# pr_backend_core.source_text。#3321）。
_MAX_SOURCES = 10
SOURCE_TEXT_ITEM_MAX_TOKENS = PLATFORM_SOURCE_TEXT_ITEM_MAX_TOKENS
SOURCE_TEXT_BUDGET_TOKENS = PLATFORM_SOURCE_TEXT_RESPONSE_BUDGET_TOKENS

_READ = frozenset({RUNS_VIEW, RUNS_OPERATE, ADMIN})
_OPERATE = frozenset({RUNS_OPERATE, ADMIN})
# 待つのをやめる状態（終わった・人の答えを待つ）。
_SETTLED = frozenset(
    {
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.WAITING_APPROVAL,
        RunStatus.WAITING_CONFIRMATION,
    }
)
_RUN_LIMIT_WINDOW = timedelta(hours=24)

AGENT_NOT_FOUND_CODE = "AGENT_NOT_FOUND"
AGENT_OUT_OF_KEY_SCOPE_CODE = "AGENT_OUT_OF_KEY_SCOPE"
AGENT_NOT_AVAILABLE_CODE = "AGENT_NOT_AVAILABLE"
RUN_NOT_FOUND_CODE = "RUN_NOT_FOUND"
THREAD_NOT_FOUND_CODE = "THREAD_NOT_FOUND"
# system_admin のキーで、他の利用者の会話・Run に続きの質問・確認の答えを足そうとしたときの code は
# platform の `OTHERS_CONTENT_FORBIDDEN_CODE`（3 製品で同じ。#3244）。
RUN_LIMIT_CODE = "API_KEY_RUN_LIMIT"
CONFIRMATION_NOT_FOUND_CODE = "CONFIRMATION_NOT_FOUND"
CONFIRMATION_CLOSED_CODE = "CONFIRMATION_CLOSED"
CONFIRMATION_ANSWER_INVALID_CODE = "CONFIRMATION_ANSWER_INVALID"
SKILL_NOT_FOUND_CODE = "SKILL_NOT_FOUND"
INVALID_CURSOR_CODE = "INVALID_CURSOR"

_OUT_OF_SCOPE_MESSAGE = (
    "この API キーではこの業務 Agent を使えません。キーの「使える業務 Agent」を確認してください。"
)

router = APIRouter(tags=["mcp"])


# ---- 入出力 -------------------------------------------------------------------------


class AgentListInput(BaseModel):
    pass


class SkillSummary(BaseModel):
    id: str
    name: str
    description: str = ""


class AgentSummary(BaseModel):
    id: str
    name: str
    description: str = ""
    # 業務 Agent が使うスキル（公開中の版）。何を調べられるかの目安。
    skills: list[SkillSummary] = Field(default_factory=list)


class AgentListOutput(BaseModel):
    schema_version: int = Field(
        default=LIST_OUTPUT_SCHEMA_VERSION, description="出力の形の版（変わったら上がる）。"
    )
    agents: list[AgentSummary]


class AgentChatInput(BaseModel):
    agent_id: str = Field(
        min_length=1, max_length=200, description="業務 Agent の ID（agent_list の id）"
    )
    question: str = Field(min_length=1, max_length=QUESTION_MAX_CHARS, description="質問")
    thread_id: str | None = Field(
        default=None,
        pattern=r"^thread_[0-9a-f]{32}$",
        description=(
            "続ける会話の ID（前の agent_chat / agent_get_run の thread_id）。"
            "省略すると新しい会話を始める。前の質問と回答を踏まえて答える。"
        ),
    )
    wait_seconds: int = Field(
        default=CHAT_DEFAULT_WAIT_SECONDS,
        ge=0,
        le=MAX_WAIT_SECONDS,
        description=(
            "回答を待つ秒数。終わらなければ途中の状態と run_id を返すので、agent_get_run で読む。"
        ),
    )


class AgentGetRunInput(BaseModel):
    run_id: str = Field(min_length=1, max_length=200, description="agent_chat の run_id")
    wait_seconds: int = Field(
        default=GET_RUN_DEFAULT_WAIT_SECONDS,
        ge=0,
        le=MAX_WAIT_SECONDS,
        description="まだ終わっていなければ、終わるまで待つ秒数（0 は今の状態をすぐ返す）。",
    )


class AgentCancelRunInput(BaseModel):
    run_id: str = Field(min_length=1, max_length=200, description="agent_chat の run_id")


CancelResult = Literal["cancelled", "already_cancelled", "already_finished"]


class CancelRunOutput(BaseModel):
    schema_version: int = Field(
        default=CANCEL_OUTPUT_SCHEMA_VERSION, description="出力の形の版（変わったら上がる）。"
    )
    run_id: str
    status: str = Field(description="取消の後の Run の状態。")
    result: CancelResult = Field(
        description=(
            "cancelled=取り消した / already_cancelled=すでに取り消されていた / "
            "already_finished=終わっていたため変えていない（status が completed / failed）。"
        )
    )
    message: str


class SkillExportInput(BaseModel):
    skill_id: str = Field(
        min_length=1, max_length=200, description="スキルの ID（agent_list の skills の id）"
    )


class SkillFileOutput(BaseModel):
    # スキルのフォルダからの相対パス（SKILL.md・references/…）。
    path: str
    content: str


class SkillExportOutput(BaseModel):
    """Agent Skills の形のスキル。`<name>/` のフォルダに files を置くと外部の Agent で使える。"""

    schema_version: int = Field(
        default=SKILL_EXPORT_SCHEMA_VERSION, description="出力の形の版（変わったら上がる）。"
    )
    name: str
    files: list[SkillFileOutput]


class ConfirmationAnswerInput(BaseModel):
    item_id: str = Field(min_length=1, max_length=128, description="確認事項の ID")
    option_ids: list[str] = Field(
        default_factory=list, max_length=8, description="選んだ選択肢の ID"
    )
    other_text: str = Field(
        default="", max_length=2000, description="選択肢に無いときの自由入力の答え"
    )


class AgentAnswerConfirmationInput(BaseModel):
    run_id: str = Field(min_length=1, max_length=200, description="確認を出した Run の ID")
    confirmation_id: str = Field(
        min_length=1, max_length=200, description="確認の ID（confirmation.id）"
    )
    action: Literal["answer", "skip"] = Field(
        default="answer",
        description="answer は確認事項に答える。skip は確認せずに（前提を示して）回答させる。",
    )
    answers: list[ConfirmationAnswerInput] = Field(
        default_factory=list,
        max_length=MAX_ITEMS,
        description="answer のとき、すべての確認事項への答え（選択肢か自由入力）。",
    )
    wait_seconds: int = Field(
        default=CHAT_DEFAULT_WAIT_SECONDS,
        ge=0,
        le=MAX_WAIT_SECONDS,
        description="答えた後の回答を待つ秒数。",
    )


class ConfirmationOptionOutput(BaseModel):
    id: str
    label: str
    description: str = ""


class ConfirmationItemOutput(BaseModel):
    id: str
    prompt: str
    reason: str = ""
    # single は 1 つ選ぶ、multiple は複数選べる。
    selection: str = "single"
    options: list[ConfirmationOptionOutput] = Field(default_factory=list)


class ConfirmationOutput(BaseModel):
    """回答前の確認（#1839）。agent_answer_confirmation で答える。"""

    id: str
    # agent（業務 Agent 自身）/ nl2sql / rag（調べた先が確認を求めた）。
    source: str
    items: list[ConfirmationItemOutput]
    round: int
    max_rounds: int
    # 確認せずに回答（action=skip）を選べるか。
    can_skip: bool


class TextRangeOutput(BaseModel):
    """本文の中の範囲（文字の位置。[start, end)。RAG の根拠の excerpt_range と同じ）。"""

    start: int
    end: int


class SourceOutput(BaseModel):
    """回答の根拠（RAG の資料。回答に使ったもの）。"""

    title: str
    location: str = ""
    excerpt: str = Field(
        default="",
        description=(
            "根拠の本文（既定は全文）。応答の合計の予算（token の見積もり "
            f"{SOURCE_TEXT_BUDGET_TOKENS}）に根拠の順で入れ、入らない根拠は空（text_omitted）。"
            f"1 件で {SOURCE_TEXT_ITEM_MAX_TOKENS} を超える根拠は、質問に当たる部分を中心に"
            "文・段落（表は行）の境界で切った一部（truncated）。"
        ),
    )
    truncated: bool = Field(
        default=False, description="excerpt が本文の一部だけか（範囲は excerpt_range）。"
    )
    text_omitted: bool = Field(
        default=False,
        description=(
            "応答の予算に入らず excerpt を空にしたか（本文は RAG の rag_read_source に "
            "document_id と chunk_id を渡して読む）。"
        ),
    )
    text_length: int | None = Field(
        default=None, description="根拠の本文の文字数（分からなければ null）。"
    )
    excerpt_range: TextRangeOutput | None = Field(
        default=None,
        description="excerpt の本文の位置（文字の [start, end)）。text_omitted・不明なら null。",
    )
    header_range: TextRangeOutput | None = Field(
        default=None,
        description=(
            "切った表で excerpt の先頭に付けた見出しの行の位置（excerpt_range と続かないとき"
            "だけ）。無ければ null。"
        ),
    )
    next_offset: int | None = Field(
        default=None,
        description=(
            "excerpt の後ろの続きを RAG の rag_read_source で読むときの offset（続きが無ければ "
            "null）。"
        ),
    )
    # ID の名前は RAG の根拠（`rag_search` の evidence）と同じ（契約の一致はテストで確かめる）。
    document_id: str | None = Field(
        default=None, description="RAG の文書の ID（会話の添付の根拠は null）。"
    )
    chunk_id: str | None = Field(
        default=None, description="RAG の chunk の ID（rag_read_source に渡せる。添付は null）。"
    )
    cited: bool = Field(
        default=False,
        description=(
            "回答で引用した根拠か（引用の無い回答は、AI に渡した根拠をすべて false で出す）。"
        ),
    )


# 回答の対応（RAG の `rag_search` の outcome と同じ語彙。#1305。一致はテストで確かめる）。
AnswerOutcomeValue = Literal[
    "answered",
    "conditional",
    "needs_clarification",
    "needs_environment_data",
    "needs_human",
    "insufficient_evidence",
]


class ReasonOutput(BaseModel):
    """印・外した段落・回答の警告の理由。"""

    code: str
    # 利用者に見せる短い文。
    label: str
    detail: str = ""


class PassageMarkOutput(BaseModel):
    """回答の本文に残した段落の印（unverified=根拠で確かめられていない / warning=注意が必要）。"""

    text: str
    state: Literal["unverified", "warning"]
    reasons: list[ReasonOutput] = Field(default_factory=list)


class RemovedPassageOutput(BaseModel):
    """根拠と矛盾するため回答の本文から除いた段落。"""

    text: str
    reasons: list[ReasonOutput] = Field(default_factory=list)


class RunOutput(BaseModel):
    schema_version: int = Field(
        default=RUN_OUTPUT_SCHEMA_VERSION, description="出力の形の版（変わったら上がる）。"
    )
    run_id: str
    agent_id: str
    # 会話の ID。続きの質問は agent_chat の thread_id に渡す。
    thread_id: str | None = None
    # queued / running / waiting_confirmation / waiting_approval / completed / failed / cancelled
    status: str
    outcome: AnswerOutcomeValue | None = Field(
        default=None,
        description=(
            "回答の対応。answered=資料で答えた / conditional=条件・不足を示して答えた / "
            "needs_clarification=利用者への確認 / "
            "needs_environment_data=現場の値・記録の確認が要る / "
            "needs_human=人への引き継ぎ / insufficient_evidence=資料から答えられない。"
            "completed でなければ null。"
        ),
    )
    # 待ちをやめた理由に関わらず、Run がまだ進むか（agent_get_run で読み直す）。
    in_progress: bool
    answer: str | None = None
    # 回答前の確認（答えていないもの）。あれば agent_answer_confirmation で答える。
    confirmation: ConfirmationOutput | None = None
    sources: list[SourceOutput] = Field(default_factory=list)
    sources_text_omitted: int = Field(
        default=0, ge=0, description="応答の予算に入らず本文を省いた根拠（text_omitted）の数。"
    )
    withheld_evidence_count: int = Field(
        default=0, ge=0, description="今の権限で見られないため sources に出さない根拠の数。"
    )
    passage_marks: list[PassageMarkOutput] = Field(
        default_factory=list,
        description="answer の本文の段落（text）のうち、確かめが要る段落と理由。",
    )
    removed_passages: list[RemovedPassageOutput] = Field(
        default_factory=list, description="根拠と矛盾するため answer の本文から除いた段落と理由。"
    )
    answer_warnings: list[ReasonOutput] = Field(
        default_factory=list, description="回答全体についての警告。"
    )
    # 使ったツールの名前（Agent の中の名前。例: rag__rag_search）。
    tools_used: list[str] = Field(default_factory=list)
    # 承認が要るため使わなかったツール（MCP から作った Run は承認の要るツールを呼ばない）。
    skipped_approval_tools: list[str] = Field(default_factory=list)
    pending_approvals: int = 0
    # 次にすること（日本語）。
    message: str
    # 回答の注意書き（#2405）。API キーで呼んだ外部のクライアントにだけ返す。
    notice: str | None = Field(
        default=None, description="利用者に回答を示すときは、末尾にこの文をそのまま添える。"
    )


class AgentGetRunObservationInput(BaseModel):
    run_id: str = Field(min_length=1, max_length=200, description="agent_chat の run_id")
    cursor: str | None = Field(
        default=None,
        max_length=500,
        description="前の応答の next_cursor（続きの根拠を読む）。省略すると先頭から。",
    )
    limit: int = Field(
        default=SOURCE_TEXT_PAGE_LIMIT_DEFAULT,
        ge=1,
        le=SOURCE_TEXT_PAGE_LIMIT_MAX,
        description="1 回に返す根拠の件数の上限。",
    )


class ObservedTableCell(BaseModel):
    """表の 1 行の根拠の値のあるセル（RAG の EvidenceTableCell と同じ名前）。"""

    table_col: str
    header_path: list[str]


class ObservedLocator(BaseModel):
    """根拠の場所（RAG の根拠の locator と同じ名前。一致はテストで確かめる）。"""

    section_path: list[str] = Field(default_factory=list)
    page_start: int | None = None
    page_end: int | None = None
    page_label_start: str | None = None
    page_label_end: str | None = None
    sheet_name: str | None = None
    # 行の番号の基準（sheet=表計算のシート / csv=CSV の記録の番号 / word_table=Word の表の行 /
    # parsed_table=解析が組み立てた表の格子の行。原本の座標ではない）と表の番号。
    row_basis: Literal["sheet", "csv", "word_table", "parsed_table"] | None = None
    table_index: int | None = None
    row_start: int | None = None
    row_end: int | None = None
    # CSV の原本の物理行の範囲（row_start・row_end は記録の番号）。
    source_line_start: int | None = None
    source_line_end: int | None = None
    cell_range: str | None = None
    element_locator: str | None = None
    table_row: int | None = None
    table_cells: list[ObservedTableCell] = Field(default_factory=list)


class ObservedUsage(BaseModel):
    """モデルの利用量（承認の後の再開を含む累計。名前は OpenTelemetry の gen_ai.usage.*）。"""

    model: str | None = Field(default=None, description="モデルの ID（gen_ai.request.model）。")
    requests: int = Field(default=0, ge=0, description="モデルの呼び出しの回数。")
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


# RAG の ZeroEvidence.stage と RagEvidence.table_binding の語彙（契約の一致はテストで確かめる）。
ObservedZeroEvidenceStage = Literal["retrieval", "filter", "evidence_check", "context"]
ObservedTableBinding = Literal["checkable", "not_checkable"]


class ObservedDegradedStep(BaseModel):
    """縮退した任意の補助の処理（RAG の DegradedStep と同じ名前）。"""

    kind: str
    count: int = Field(ge=1)


class ObservedToolCall(BaseModel):
    """ツールの呼び出し（Run の順）。"""

    index: int = Field(ge=0, description="Run の中の呼び出しの順（0 始まり）。")
    call_id: str = Field(description="呼び出しの ID（gen_ai.tool.call.id）。")
    name: str = Field(description="ツールの名前（<接続>__<ツール>。tools_used と同じ）。")
    status: ObservedToolStatus = Field(
        description="approval_skipped=承認が要るため呼ばなかった。ほかは呼び出しの状態。"
    )
    error_code: str | None = Field(default=None, description="失敗の分類（error.type）。")
    started_at: datetime | None = None
    completed_at: datetime | None = None
    evidence_count: int | None = Field(
        default=None,
        description=(
            "RAG の根拠・読み取りのツールが返した根拠の数（今の権限で見られない根拠を除く）。"
            "ほかのツールは null。"
        ),
    )
    include_superseded: bool | None = Field(
        default=None,
        description="RAG の根拠のツールで旧版も検索したか。ほかのツールは null。",
    )
    job_status: str | None = Field(
        default=None, description="NL2SQL の nl2sql_query / nl2sql_get_job の status。"
    )
    row_count: int | None = Field(default=None, description="NL2SQL の returned_count。")
    rows_truncated: bool | None = Field(default=None, description="NL2SQL の truncated。")
    degraded_steps: list[ObservedDegradedStep] | None = Field(
        default=None,
        description=(
            "RAG の根拠のツールの degraded_steps（失敗して縮退した任意の補助の処理の種類と回数。"
            "空なら縮退していない）。ほかのツール・失敗した呼び出しは null。"
        ),
    )
    guardrail_degraded: bool | None = Field(
        default=None,
        description=(
            "RAG の根拠のツールの guardrail_degraded（安全チェックを完了できず止めた）。"
            "ほかのツール・失敗した呼び出しは null。"
        ),
    )
    zero_evidence_stage: ObservedZeroEvidenceStage | None = Field(
        default=None,
        description="RAG の根拠のツールが根拠 0 件のとき、0 件になった段（zero_evidence.stage）。",
    )


class ObservedEvidence(BaseModel):
    """Run が調べた RAG の根拠（根拠のツールの evidence と、読み取りのツールで読んだ chunk）。"""

    rank: int = Field(
        ge=1, description="Run の中で初めて現れた順（document_id・chunk_id で重複を除く）。"
    )
    tool_call_index: int = Field(ge=0, description="初めて返した呼び出しの index。")
    source: Literal["search", "read"] = Field(
        description="search=根拠のツール / read=読み取りのツール。"
    )
    document_id: str
    chunk_id: str
    file_name: str | None = None
    chunk_set_id: str | None = None
    evidence_type: str | None = Field(
        default=None, description="RAG の evidence_type（読み取りの chunk は分からなければ null）。"
    )
    origin: str | None = Field(
        default=None, description="RAG の origin（読み取りの chunk は null）。"
    )
    superseded: bool = False
    retrieval_rank: int | None = Field(
        default=None, description="RAG の検索の順位（読み取りの chunk・前後の文脈は null）。"
    )
    cited: bool = Field(default=False, description="回答で引用した根拠か。")
    answer_use: Literal["confirmed", "reference"] | None = Field(
        default=None,
        description="回答での使われ方（RAG の answer_use と同じ。引用でなければ null）。",
    )
    locator: ObservedLocator = Field(default_factory=ObservedLocator)
    table_binding: ObservedTableBinding | None = Field(
        default=None,
        description=(
            "RAG の table_binding（表の行・列の対応の検査がこの根拠の本文を読めるか。null は表の"
            "無い根拠・読み取りのツールの chunk）。"
        ),
    )
    text: str = Field(
        default="",
        description=(
            "生成に渡した本文そのもの（切らない。同じ chunk を別の呼び出しが渡した本文は改行で"
            "つなぐ）。text_omitted なら空。"
        ),
    )
    text_length: int = Field(ge=0, description="生成に渡した本文の文字数。")
    text_omitted: bool = Field(
        default=False,
        description=(
            f"1 件でページの予算（token の見積もり {SOURCE_TEXT_PAGE_BUDGET_TOKENS}）を超えるため"
            "本文を省いたか（RAG の rag_read_source で読む）。"
        ),
    )
    truncated: bool = Field(
        default=False,
        description="生成に渡した本文が chunk の一部だけだったか（RAG が切った・途中まで読んだ）。",
    )


class RunObservation(BaseModel):
    schema_version: int = Field(
        default=OBSERVATION_SCHEMA_VERSION, description="出力の形の版（変わったら上がる）。"
    )
    run_id: str
    agent_id: str = Field(description="業務 Agent の ID（gen_ai.agent.id）。")
    thread_id: str | None = Field(default=None, description="会話の ID（gen_ai.conversation.id）。")
    agent_version: int | None = Field(
        default=None, description="Run に使った業務 Agent の公開の版（下書きの実行は null）。"
    )
    trace_id: str | None = Field(
        default=None, description="Run の trace id（W3C Trace Context。始まっていなければ null）。"
    )
    status: str
    outcome: AnswerOutcomeValue | None = Field(
        default=None, description="agent_get_run の outcome と同じ。completed でなければ null。"
    )
    validation_status: ValidationStatus | None = Field(
        default=None, description="回答の最終の検証の状態（無ければ null）。"
    )
    created_at: datetime
    finished_at: datetime | None = Field(default=None, description="終わっていなければ null。")
    usage: ObservedUsage | None = None
    tool_calls: list[ObservedToolCall] = Field(
        default_factory=list, description="ツールの呼び出し（Run の順。tool_call_total 件まで）。"
    )
    tool_call_total: int = Field(default=0, ge=0, description="ツールの呼び出しの全件の数。")
    evidence: list[ObservedEvidence] = Field(
        default_factory=list,
        description=(
            "cursor からの根拠（rank の順。limit 件と、本文の合計の予算 "
            f"{SOURCE_TEXT_PAGE_BUDGET_TOKENS} まで）。"
        ),
    )
    evidence_total: int = Field(default=0, ge=0, description="今の権限で見られる根拠の全件の数。")
    withheld_evidence_count: int = Field(
        default=0, ge=0, description="今の権限で見られないため除いた根拠の数。"
    )
    next_cursor: str | None = Field(
        default=None, description="続きの根拠があれば、次の呼び出しの cursor。"
    )


# ---- 判定---------------------------------------------------------------------------


def _principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise McpToolError("MCP_UNAUTHENTICATED", "認証できません。", status=401)
    return principal


def _key_scope(request: Request) -> frozenset[str] | None:
    """API キーの業務 Agent の範囲（キーで来ていない・範囲なしは None）。"""
    record = getattr(request.state, "api_key", None)
    scope = getattr(record, "scope", None)
    return scope("agent") if callable(scope) else None


def _key_metadata(request: Request) -> dict[str, str]:
    """Run に残す出所（MCP とキーの ID・名前。監査の「MCP（キーの名前）」）。"""
    record = getattr(request.state, "api_key", None)
    metadata = {RUN_SOURCE_KEY: MCP_RUN_SOURCE}
    if record is not None:
        metadata[MCP_API_KEY_ID_KEY] = str(record.key_id)
        metadata[MCP_API_KEY_NAME_KEY] = str(record.name)
    return metadata


def _visible(request: Request, agent_id: str) -> bool:
    """利用者の対象範囲（ロール）とキーの範囲の両方に入る業務 Agent か。"""
    scope = _key_scope(request)
    if scope is not None and agent_id not in scope:
        return False
    return agent_api._agent_allowed(request, agent_id)


def _require_agent_in_scope(request: Request, agent_id: str) -> None:
    scope = _key_scope(request)
    if scope is not None and agent_id not in scope:
        raise McpToolError(AGENT_OUT_OF_KEY_SCOPE_CODE, _OUT_OF_SCOPE_MESSAGE, status=403)
    if not agent_api._agent_allowed(request, agent_id):
        raise McpToolError(
            AGENT_NOT_FOUND_CODE, "業務 Agent が見つからないか、使う権限がありません。", status=403
        )


def _find_agent(agent_id: str) -> AgentProfile | None:
    try:
        return agent_api._control_plane_agent(agent_id)
    except KeyError:
        return None


def _require_available_agent(request: Request, agent_id: str) -> AgentProfile:
    _require_agent_in_scope(request, agent_id)
    agent = _find_agent(agent_id)
    if agent is None:
        raise McpToolError(AGENT_NOT_FOUND_CODE, "業務 Agent が見つかりません。", status=404)
    reason = agent_unavailable_reason(agent)
    if reason is not None:
        raise McpToolError(AGENT_NOT_AVAILABLE_CODE, reason, status=409)
    return agent


def _readable_run(request: Request, run_id: str) -> RunState:
    """呼び出し元が読める Run（画面と同じ規則。#1889・#1954）。

    呼び出し元が実行した Run・呼び出し元の会話の Run、キーの持ち主が system_admin なら全員の Run。
    ほかの利用者の Run は「無い」として扱い、存在を漏らさない。キーの業務 Agent の範囲はその上に
    重ねてかかる。
    """
    _principal(request)
    try:
        run = runtime_repository.get_run(run_id)
    except KeyError:
        run = None
    if run is None or not agent_api._run_visible_to(request, run):
        raise McpToolError(RUN_NOT_FOUND_CODE, "Run が見つかりません。", status=404)
    _require_agent_in_scope(request, run.agent_id)
    return run


def _require_run_budget(request: Request) -> None:
    """キーごとの直近 24 時間の Run の数（`PLATFORM_API_KEY_RUNS_PER_DAY`。#1874）。"""
    record = getattr(request.state, "api_key", None)
    limit = int(get_settings().app_api_key_runs_per_day)
    if record is None or limit <= 0:
        return
    now = datetime.now(UTC)
    since = now - _RUN_LIMIT_WINDOW
    created = sorted(
        run.created_at
        for run in runtime_repository.list_runs()
        if run.metadata.get(MCP_API_KEY_ID_KEY) == record.key_id and run.created_at >= since
    )
    if len(created) < limit:
        return
    retry_after = max(1, int((created[-limit] + _RUN_LIMIT_WINDOW - now).total_seconds()) + 1)
    raise McpToolError(
        RUN_LIMIT_CODE,
        f"この API キーで作れる実行（Run）は直近 24 時間に {limit} 件までです。"
        f"{retry_after} 秒ほど後に呼び直してください。",
        status=429,
        details={"retry_after_seconds": retry_after, "limit": limit},
    )


# ---- 出力 ---------------------------------------------------------------------------


def _pending_confirmation(run: RunState) -> ConfirmationRequest | None:
    return next((item for item in run.confirmations if item.status == "pending"), None)


def _confirmation_output(item: ConfirmationRequest) -> ConfirmationOutput:
    return ConfirmationOutput(
        id=item.id,
        source=item.source,
        items=[
            ConfirmationItemOutput(
                id=entry.id,
                prompt=entry.prompt,
                reason=entry.reason,
                selection=entry.selection,
                options=[
                    ConfirmationOptionOutput(
                        id=option.id, label=option.label, description=option.description
                    )
                    for option in entry.options
                ],
            )
            for entry in item.items
        ],
        round=item.round,
        max_rounds=item.max_rounds,
        can_skip=item.can_skip,
    )


def _location(locator: object) -> str:
    if not isinstance(locator, Mapping):
        return ""
    parts: list[str] = []
    sheet = locator.get("sheet_name")
    if isinstance(sheet, str) and sheet.strip():
        parts.append(sheet.strip())
    start, end = locator.get("page_start"), locator.get("page_end")
    if isinstance(start, int) and start > 0:
        parts.append(
            f"p.{start}" if not isinstance(end, int) or end == start else f"p.{start}-{end}"
        )
    sections = locator.get("section_path")
    if isinstance(sections, list):
        names = [item for item in sections if isinstance(item, str) and item.strip()]
        if names:
            parts.append(" > ".join(names))
    return " · ".join(parts)


def _answer_evidence(run: RunState) -> Mapping[str, Any]:
    """Run の回答の根拠（成果物 `answer_evidence`。#2328）。無ければ同じ判定で作る。"""
    for artifact in reversed(run.artifacts):
        if artifact.kind == ANSWER_EVIDENCE_KIND and isinstance(artifact.content, dict):
            return artifact.content
    return answer_evidence(run.steps, run_answer_text(run) or "", _answer_validation(run) or None)


def _answer_validation(run: RunState) -> Mapping[str, Any]:
    """Run の最終の検証の成果物の内容（無ければ空）。"""
    for artifact in reversed(run.artifacts):
        if artifact.kind == ANSWER_VALIDATION_KIND and isinstance(artifact.content, dict):
            return artifact.content
    return {}


def _reasons(value: object) -> list[ReasonOutput]:
    return [
        ReasonOutput(
            code=str(item.get("code") or ""),
            label=str(item.get("label") or ""),
            detail=str(item.get("detail") or ""),
        )
        for item in (value if isinstance(value, list) else [])
        if isinstance(item, Mapping)
    ]


def _review(
    run: RunState,
) -> tuple[list[PassageMarkOutput], list[RemovedPassageOutput], list[ReasonOutput]]:
    """回答の段落の印・外した段落・回答の警告（成果物「資料との照合」。#2738）。"""
    if run.status != RunStatus.COMPLETED:
        return [], [], []
    content = _answer_validation(run)
    marks = content.get("passage_marks")
    removed = content.get("removed_passages")
    return (
        [
            PassageMarkOutput(
                text=str(item.get("text") or ""),
                state=item["state"],
                reasons=_reasons(item.get("reasons")),
            )
            for item in (marks if isinstance(marks, list) else [])
            if isinstance(item, Mapping) and item.get("state") in {"unverified", "warning"}
        ],
        [
            RemovedPassageOutput(
                text=str(item.get("text") or ""), reasons=_reasons(item.get("reasons"))
            )
            for item in (removed if isinstance(removed, list) else [])
            if isinstance(item, Mapping)
        ],
        _reasons(content.get("answer_warnings")),
    )


def _by_document(items: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """同じ資料の根拠を続けて並べる（資料は初めて現れた順、資料の中は元の順）。"""
    order: dict[str, int] = {}
    for item in items:
        order.setdefault(str(item.get("document_id") or item.get("file_name") or ""), len(order))
    return sorted(
        items, key=lambda item: order[str(item.get("document_id") or item.get("file_name") or "")]
    )


def _rag_ids(item: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """根拠の RAG の文書と chunk の ID（会話の添付の根拠は RAG の文書ではないので両方 None）。"""
    document_id, chunk_id = item.get("document_id"), item.get("chunk_id")
    if not isinstance(document_id, str) or not document_id:
        return None, None
    if document_id.startswith(ATTACHMENT_DOCUMENT_PREFIX):
        return None, None
    return document_id, chunk_id if isinstance(chunk_id, str) and chunk_id else None


def _outcome(run: RunState) -> AnswerOutcomeValue | None:
    """完了した Run の回答の対応（成果物 `answer` の `outcome.value`。無い・知らない値は None）。"""
    if run.status != RunStatus.COMPLETED:
        return None
    value = run_answer_outcome(run)
    return cast(AnswerOutcomeValue, value) if value in ANSWER_OUTCOMES else None


def _source_items(run: RunState) -> list[Mapping[str, Any]]:
    """回答の根拠（RAG の資料）。回答で引用した根拠（画面のチャットの「回答で引用」と同じ判定。
    #2328）を選び、1 件も無ければ AI に渡した根拠の全件（資料ごとにまとめ、最大 _MAX_SOURCES 件）。
    """
    items = [item for item in _answer_evidence(run).get("items") or [] if isinstance(item, Mapping)]
    cited = [item for item in items if item.get("cited") is True]
    return _by_document(cited or items)[:_MAX_SOURCES]


def _source(item: Mapping[str, Any]) -> SourceOutput:
    """根拠 1 件（本文は空。`_fit_sources` が予算に入れて埋める）。"""
    document_id, chunk_id = _rag_ids(item)
    return SourceOutput(
        title=str(item.get("file_name") or item.get("document_id") or "名前の無い資料"),
        location=_location(item.get("locator")),
        document_id=document_id,
        chunk_id=chunk_id,
        cited=item.get("cited") is True,
    )


def _text_range(value: object) -> TextRangeOutput | None:
    if isinstance(value, Mapping):
        start, end = value.get("start"), value.get("end")
    else:
        start, end = getattr(value, "start", None), getattr(value, "end", None)
    if isinstance(start, int) and isinstance(end, int) and 0 <= start <= end:
        return TextRangeOutput(start=start, end=end)
    return None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _with_source_text(
    source: SourceOutput, item: Mapping[str, Any], text: str, piece: SourceText
) -> SourceOutput:
    """根拠に本文を入れる。範囲と続きの位置は RAG の chunk の本文の位置で返す。

    保存した本文は RAG の根拠の excerpt（既定は全文。RAG が 1 件の上限で切った・応答の予算で省いた
    ときはその一部・空）。RAG が切った本文をここでさらに切ったときは、chunk の中の位置を示せない
    ので範囲を返さない。
    """
    rag_length = _int_or_none(item.get("text_length"))
    text_length = rag_length if rag_length is not None else len(text)
    if item.get("text_omitted") is True or piece.text_omitted:
        return source.model_copy(
            update={"excerpt": "", "text_omitted": True, "text_length": text_length}
        )
    rag_truncated = item.get("truncated") is True
    if not piece.truncated:
        if rag_truncated:
            update: dict[str, Any] = {
                "truncated": True,
                "excerpt_range": _text_range(item.get("excerpt_range")),
                "header_range": _text_range(item.get("header_range")),
                "next_offset": _int_or_none(item.get("next_offset")),
            }
        else:
            update = {"excerpt_range": TextRangeOutput(start=0, end=len(text))}
        return source.model_copy(
            update={"excerpt": piece.text, "text_length": text_length, **update}
        )
    update = {"excerpt": piece.text, "truncated": True, "text_length": text_length}
    if not rag_truncated:
        update |= {
            "excerpt_range": _text_range(piece.excerpt_range),
            "header_range": _text_range(piece.header_range),
            "next_offset": piece.next_offset,
        }
    return source.model_copy(update=update)


def _fit_sources(
    items: Sequence[Mapping[str, Any]], *, queries: Sequence[str | None], reserved_tokens: int
) -> tuple[list[SourceOutput], int]:
    """根拠の本文を応答の予算に入れる（3 製品で共通の規則。#3321）。根拠と、本文を省いた数。

    予算は応答の全体（`reserved_tokens` は根拠の外の項目の分）。根拠の本文の外の項目は省いた根拠にも
    残るので先に引き、本文を根拠の順に入れる。
    """
    sources = [_source(item) for item in items]
    texts = [item.get("excerpt") if isinstance(item.get("excerpt"), str) else "" for item in items]
    fitted = fit_source_texts(
        [
            SourceTextItem(
                text=str(text),
                kind="table" if _is_table(item) else "text",
                fixed_tokens=estimate_input_tokens(source.model_dump_json())
                + _json_overhead_tokens(str(text)),
            )
            for item, source, text in zip(items, sources, texts, strict=True)
        ],
        budget_tokens=SOURCE_TEXT_BUDGET_TOKENS - reserved_tokens,
        item_max_tokens=SOURCE_TEXT_ITEM_MAX_TOKENS,
        queries=queries,
    )
    filled = [
        _with_source_text(source, item, str(text), piece)
        for source, item, text, piece in zip(sources, items, texts, fitted, strict=True)
    ]
    return filled, sum(1 for source in filled if source.text_omitted)


def _is_table(item: Mapping[str, Any]) -> bool:
    return "table" in {item.get("evidence_type"), item.get("content_kind")}


def _json_overhead_tokens(text: str) -> int:
    """本文を JSON の文字列にしたときに増える分（エスケープ）と、一覧の区切りの token。"""
    escaped = json.dumps(text, ensure_ascii=False)[1:-1]
    return estimate_input_tokens(escaped) - estimate_input_tokens(text) + 1


def _failure_message(run: RunState) -> str:
    for event in reversed(run.events):
        if event.type == RunEventType.RUNTIME_FAILED and event.message:
            return event.message
    return "業務 Agent の実行に失敗しました。"


def run_output(
    run: RunState, *, include_notice: bool = False, withheld_evidence_count: int = 0
) -> RunOutput:
    """Run の応答。`include_notice` なら、完了した回答の注意書きを付ける（#2405）。

    `withheld_evidence_count` は、見る人の今の権限で見られないため除いた根拠の数（#3252）。
    """
    confirmation = _pending_confirmation(run)
    tools_used: list[str] = []
    skipped: list[str] = []
    for step in run.steps:
        if step.tool_call is None:
            continue
        name = step.tool_call.name
        result = step.tool_result
        if result is not None and result.error_code == MCP_APPROVAL_SKIPPED_CODE:
            if name not in skipped:
                skipped.append(name)
        elif name not in tools_used:
            tools_used.append(name)
    pending_approvals = sum(1 for approval in run.approvals if approval.status == "pending")
    status = run.status
    in_progress = status in {RunStatus.QUEUED, RunStatus.RUNNING}
    if confirmation is not None:
        message = (
            "回答の前に確認が必要です。confirmation の確認事項に agent_answer_confirmation で"
            "答えてください（利用者に聞くか、分かっていれば答える。can_skip なら action=skip で"
            "確認せずに回答させられます）。"
        )
    elif status == RunStatus.COMPLETED:
        message = "回答しました。続きの質問は thread_id を agent_chat に渡してください。"
    elif status == RunStatus.FAILED:
        message = _failure_message(run)
    elif status == RunStatus.CANCELLED:
        message = "実行は取り消されました。"
    elif status == RunStatus.WAITING_APPROVAL:
        message = (
            "承認が必要な操作があります。Agent の画面の「承認」で判断されると続きを実行します。"
            "agent_get_run で後から読めます。"
        )
    else:
        message = (
            "実行しています。agent_get_run（wait_seconds）で続きを読んでください。"
            "止めるときは agent_cancel_run を使います。"
        )
    passage_marks, removed_passages, warnings = _review(run)
    output = RunOutput(
        run_id=run.id,
        agent_id=run.agent_id,
        thread_id=run.thread_id,
        status=str(status.value),
        outcome=_outcome(run),
        in_progress=in_progress,
        answer=run_answer_text(run),
        confirmation=_confirmation_output(confirmation) if confirmation is not None else None,
        withheld_evidence_count=withheld_evidence_count,
        passage_marks=passage_marks,
        removed_passages=removed_passages,
        answer_warnings=warnings,
        tools_used=tools_used,
        skipped_approval_tools=skipped,
        pending_approvals=pending_approvals,
        message=message,
        notice=(
            run_answer_notice(run)
            if include_notice and status == RunStatus.COMPLETED and confirmation is None
            else None
        ),
    )
    sources, text_omitted = _fit_sources(
        _source_items(run), queries=[run.goal], reserved_tokens=_output_tokens(output)
    )
    return output.model_copy(update={"sources": sources, "sources_text_omitted": text_omitted})


def _output_tokens(output: BaseModel) -> int:
    """出力の token の見積もり（MCP が返す JSON の形）。"""
    return estimate_input_tokens(output.model_dump_json())


_INVALID_CURSOR_MESSAGE = (
    "cursor が正しくありません。同じ run_id の前の応答の next_cursor をそのまま渡すか、"
    "省略して先頭から読んでください。"
)


def _encode_cursor(run_id: str, offset: int) -> str:
    """観測の続きの位置（Run の ID と、根拠の全件の中のずれ）。中身は不透明として扱わせる。"""
    raw = json.dumps({"run_id": run_id, "offset": offset}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _cursor_offset(cursor: str | None, run_id: str, total: int) -> int:
    """cursor のずれ（別の Run の cursor・壊れた cursor は INVALID_CURSOR）。"""
    if cursor is None:
        return 0
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise McpToolError(INVALID_CURSOR_CODE, _INVALID_CURSOR_MESSAGE, status=400) from exc
    offset = decoded.get("offset") if isinstance(decoded, dict) else None
    if (
        not isinstance(decoded, dict)
        or decoded.get("run_id") != run_id
        or not isinstance(offset, int)
        or isinstance(offset, bool)
        or not 0 <= offset <= total
    ):
        raise McpToolError(INVALID_CURSOR_CODE, _INVALID_CURSOR_MESSAGE, status=400)
    return offset


async def _unreadable_for_viewer(
    request: Request, run: RunState, document_ids: set[str]
) -> frozenset[str]:
    """見る人（キーの持ち主）が今見られない RAG の文書（#3252 と同じ確かめ。fail closed）。"""
    if not document_ids:
        return frozenset()
    viewer = agent_api._run_creator_user_uuid(request)
    if not viewer:
        return frozenset(document_ids)
    return await run_in_threadpool(unreadable_documents, run, viewer, document_ids)


def _observed_evidence(
    item: ObservedEvidenceItem, cited: Mapping[tuple[str, str], str | None]
) -> ObservedEvidence:
    """根拠 1 件（本文は空。ページに入れるときに埋める）。"""
    key = (item.document_id, item.chunk_id)
    use = cited.get(key)
    return ObservedEvidence(
        rank=item.rank,
        tool_call_index=item.tool_call_index,
        source=item.source,
        document_id=item.document_id,
        chunk_id=item.chunk_id,
        file_name=item.file_name,
        chunk_set_id=item.chunk_set_id,
        evidence_type=item.evidence_type,
        origin=item.origin,
        superseded=item.superseded,
        retrieval_rank=item.retrieval_rank,
        cited=key in cited,
        answer_use=cast(Literal["confirmed", "reference"], use) if use is not None else None,
        locator=ObservedLocator.model_validate(item.locator),
        table_binding=cast(ObservedTableBinding | None, item.table_binding),
        text_length=len(item.text),
        truncated=item.truncated,
    )


async def run_observation(
    request: Request, run: RunState, *, cursor: str | None, limit: int
) -> RunObservation:
    """Run の観測（評価・監査）。根拠は見る人の今の権限で毎回確かめ直す（#3252。OWASP API1）。

    観測は Run が保存した記録だけから作る（`run_observation.observe_run`）。根拠の本文は生成に
    渡した本文そのもので、件数（`limit`）と 1 ページの本文の合計の予算でページに分ける（3 製品で
    共通の規則 `page_source_texts`。#3321）。cursor のずれは見られない根拠も含めた全件の中の位置に
    するので、ページの間で権限が変わっても根拠を飛ばさない。
    """
    data = observe_run(run)
    offset = _cursor_offset(cursor, run.id, len(data.evidence))
    unreadable = await _unreadable_for_viewer(
        request, run, {item.document_id for item in data.evidence}
    )
    loaded = {item.id: item for item in await agent_api._evidence_artifacts(run)}
    hydrated = run.model_copy(
        update={"artifacts": [loaded.get(item.id, item) for item in run.artifacts]}
    )
    cited = answer_citations(
        [
            item
            for item in _answer_evidence(hydrated).get("items") or []
            if isinstance(item, Mapping)
        ]
    )
    visible = [
        (position, item)
        for position, item in enumerate(data.evidence)
        if item.document_id not in unreadable
    ]
    usage = run.usage
    output = RunObservation(
        run_id=run.id,
        agent_id=run.agent_id,
        thread_id=run.thread_id,
        agent_version=run_agent_version(run),
        trace_id=run.trace_id,
        status=str(run.status.value),
        outcome=_outcome(run),
        validation_status=validation_status(_answer_validation(hydrated)),
        created_at=run.created_at,
        finished_at=run_finished_at(run),
        usage=(
            ObservedUsage(
                model=usage.model or None,
                requests=usage.requests,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )
            if usage is not None
            else None
        ),
        tool_calls=[
            ObservedToolCall(
                index=call.index,
                call_id=call.call_id,
                name=call.name,
                status=call.status,
                error_code=call.error_code,
                started_at=call.started_at,
                completed_at=call.completed_at,
                evidence_count=(
                    sum(1 for document in call.evidence_documents if document not in unreadable)
                    if call.evidence_documents is not None
                    else None
                ),
                include_superseded=call.include_superseded,
                job_status=call.job_status,
                row_count=call.row_count,
                rows_truncated=call.rows_truncated,
                degraded_steps=(
                    [ObservedDegradedStep.model_validate(step) for step in call.degraded_steps]
                    if call.degraded_steps is not None
                    else None
                ),
                guardrail_degraded=call.guardrail_degraded,
                zero_evidence_stage=cast(
                    ObservedZeroEvidenceStage | None,
                    call.zero_evidence_stage,
                ),
            )
            for call in data.tool_calls
        ],
        tool_call_total=data.tool_call_total,
        evidence_total=len(visible),
        withheld_evidence_count=len(data.evidence) - len(visible),
    )
    # 本文の外の項目（応答の外枠・次の cursor・根拠の本文以外）は予算から先に引く。
    reserved = _output_tokens(
        output.model_copy(update={"next_cursor": _encode_cursor(run.id, len(data.evidence))})
    )
    shells = [_observed_evidence(item, cited) for _position, item in visible]
    start = next(
        (index for index, (position, _item) in enumerate(visible) if position >= offset),
        len(visible),
    )
    page = page_source_texts(
        [
            SourceTextItem(
                text=item.text,
                fixed_tokens=_output_tokens(shell) + _json_overhead_tokens(item.text),
            )
            for (_position, item), shell in zip(visible, shells, strict=True)
        ],
        offset=start,
        limit=limit,
        budget_tokens=max(1, SOURCE_TEXT_PAGE_BUDGET_TOKENS - reserved),
    )
    evidence = [
        shells[index].model_copy(update={"text": piece.text, "text_omitted": piece.text_omitted})
        for index, piece in page.items
    ]
    next_cursor = (
        _encode_cursor(run.id, visible[page.next_offset][0])
        if page.next_offset is not None
        else None
    )
    return output.model_copy(update={"evidence": evidence, "next_cursor": next_cursor})


_CANCEL_MESSAGES: dict[str, str] = {
    "cancelled": "実行を取り消しました。",
    "already_cancelled": "実行はすでに取り消されています。",
    "already_finished": (
        "実行はすでに終わっているため、取り消していません。agent_get_run で結果を読めます。"
    ),
}


def _cancel_via(request: Request) -> dict[str, str]:
    """取消のイベントに残す入口（MCP とキーの ID。#3240）。"""
    record = getattr(request.state, "api_key", None)
    via = {CANCEL_VIA_KEY: MCP_RUN_SOURCE}
    if record is not None:
        via[MCP_API_KEY_ID_KEY] = str(record.key_id)
    return via


def _cancel_result(before: RunStatus, after: RunState) -> CancelResult:
    if before == RunStatus.CANCELLED:
        return "already_cancelled"
    # 呼ぶ前に終わっていた、または取消と完了が重なり完了が先に決まった（#911）。
    return "cancelled" if after.status == RunStatus.CANCELLED else "already_finished"


def called_with_api_key(request: Request) -> bool:
    """利用者の API キー（`prak_…`）で呼んだ外部の MCP クライアントか（#2405）。"""
    return getattr(request.state, "api_key", None) is not None


async def _wait(run_id: str, wait_seconds: int) -> RunState:
    deadline = time.monotonic() + wait_seconds
    while True:
        run = runtime_repository.get_run(run_id)
        if run.status in _SETTLED or time.monotonic() >= deadline:
            return run
        await asyncio.sleep(wait_poll_seconds)


# ---- サーバー -------------------------------------------------------------------------


def build_agent_mcp_server(request: Request) -> McpServer:
    def list_agents(_arguments: AgentListInput) -> AgentListOutput:
        _principal(request)
        agents: list[AgentSummary] = []
        for agent in runtime_repository.list_agents():
            # 公開した版の無い・無効な業務 Agent は利用者の Run で使えないので出さない。
            if agent_unavailable_reason(agent) is not None or not _visible(request, agent.id):
                continue
            published = agent.published()
            skill_ids = published.skill_ids if published is not None else agent.skill_ids
            skills = [
                SkillSummary(id=skill.id, name=skill.name, description=skill.description)
                for skill in (skill_registry.get(skill_id) for skill_id in skill_ids)
                if skill is not None
            ]
            agents.append(
                AgentSummary(
                    id=agent.id,
                    name=published.name if published is not None else agent.name,
                    description=(
                        published.description if published is not None else agent.description
                    ),
                    skills=skills,
                )
            )
        return AgentListOutput(agents=agents)

    async def chat(arguments: AgentChatInput) -> RunOutput:
        caller = _principal(request)
        agent = _require_available_agent(request, arguments.agent_id)
        # 続ける会話はキーの持ち主の会話だけ（system_admin のキーでも他の利用者の会話は 403。
        # 画面と同じ。#3244）。
        if arguments.thread_id is not None and not agent_api._may_continue_thread(
            request, arguments.thread_id
        ):
            raise McpToolError(
                OTHERS_CONTENT_FORBIDDEN_CODE,
                OTHERS_CONTENT_FORBIDDEN_MESSAGE,
                status=OTHERS_CONTENT_FORBIDDEN_STATUS,
            )
        _require_run_budget(request)
        try:
            run = runtime_repository.create_builtin_run(
                RunCreateRequest(
                    goal=arguments.question,
                    agent_id=agent.id,
                    thread_id=arguments.thread_id,
                ),
                created_by_user_uuid=caller.user_uuid,
                extra_metadata=_key_metadata(request),
            )
        except ThreadNotFoundError as exc:
            # 他人の会話（system_admin でないキー）・別の業務 Agent の会話も「無い」として
            # 扱う（存在を漏らさない）。
            raise McpToolError(
                THREAD_NOT_FOUND_CODE, "続ける会話が見つかりません。", status=404
            ) from exc
        except AgentNotPublishedError as exc:
            raise McpToolError(
                AGENT_NOT_AVAILABLE_CODE, AGENT_UNPUBLISHED_MESSAGE, status=409
            ) from exc
        agent_api._schedule_builtin_run(run)
        return run_output(
            await _wait(run.id, arguments.wait_seconds),
            include_notice=called_with_api_key(request),
        )

    async def get_run(arguments: AgentGetRunInput) -> RunOutput:
        run = _readable_run(request, arguments.run_id)
        waited = await _wait(run.id, arguments.wait_seconds)
        # 後から読む Run の根拠は、呼んだ利用者の今の権限で確かめ直す（見られない根拠は sources に
        # 出さず、数だけを返す。#3252）。
        checked, withheld = await agent_api._evidence_checked_run(request, waited)
        return run_output(
            checked,
            include_notice=called_with_api_key(request),
            withheld_evidence_count=withheld,
        )

    async def get_run_observation(arguments: AgentGetRunObservationInput) -> RunObservation:
        # 読める Run は agent_get_run と同じ（実行した利用者・会話の持ち主・system_admin。キーの
        # 業務 Agent の範囲。ほかの利用者の Run は「無い」。#3244）。ID を知っていても読めない。
        run = _readable_run(request, arguments.run_id)
        return await run_observation(request, run, cursor=arguments.cursor, limit=arguments.limit)

    async def answer_confirmation(arguments: AgentAnswerConfirmationInput) -> RunOutput:
        caller = _principal(request)
        run = _readable_run(request, arguments.run_id)
        _require_available_agent(request, run.agent_id)
        confirmation = next(
            (item for item in run.confirmations if item.id == arguments.confirmation_id), None
        )
        if (
            confirmation is not None
            and confirmation.source == SOURCE_AGENT
            and not agent_api._run_approvable_by(request, run)
        ):
            # Agent 自身の確認への答えは同じ Run を Run の利用者の権限で再開するので、実行した
            # 利用者だけ（画面と同じ）。
            raise McpToolError(RUN_NOT_FOUND_CODE, "Run が見つかりません。", status=404)
        if not agent_api._run_content_addable_by(request, run):
            # 答えるのは会話の持ち主だけ（system_admin のキーでも他の利用者の Run は 403。#3244）。
            raise McpToolError(
                OTHERS_CONTENT_FORBIDDEN_CODE,
                OTHERS_CONTENT_FORBIDDEN_MESSAGE,
                status=OTHERS_CONTENT_FORBIDDEN_STATUS,
            )
        _require_run_budget(request)
        try:
            outcome = runtime_repository.decide_confirmation(
                run.id,
                arguments.confirmation_id,
                action=arguments.action,
                answers=[
                    ConfirmationAnswer.model_validate(item.model_dump())
                    for item in arguments.answers
                ],
                decided_by=caller.login_user_id,
                actor_user_uuid=caller.user_uuid,
            )
        except KeyError as exc:
            raise McpToolError(
                CONFIRMATION_NOT_FOUND_CODE, "回答前の確認が見つかりません。", status=404
            ) from exc
        except ConfirmationClosedError as exc:
            raise McpToolError(
                CONFIRMATION_CLOSED_CODE,
                "この確認は答え済みか、会話が先へ進んだため答えられません。"
                "agent_get_run で今の状態を読んでください。",
                status=409,
            ) from exc
        except ConfirmationAnswerError as exc:
            raise McpToolError(CONFIRMATION_ANSWER_INVALID_CODE, str(exc), status=422) from exc
        except AgentNotPublishedError as exc:
            raise McpToolError(
                AGENT_NOT_AVAILABLE_CODE, AGENT_UNPUBLISHED_MESSAGE, status=409
            ) from exc
        if outcome.next_run is not None:
            agent_api._schedule_builtin_run(outcome.next_run)
        agent_api._schedule_builtin_run(outcome.run)
        target = outcome.next_run or outcome.run
        return run_output(
            await _wait(target.id, arguments.wait_seconds),
            include_notice=called_with_api_key(request),
        )

    async def cancel_run(arguments: AgentCancelRunInput) -> CancelRunOutput:
        """Run を取り消す（画面の `POST /runs/{id}/cancel` と同じ人。冪等。#3240）。

        止められるのは Run を見て運用の操作をしてよい人（実行した利用者・会話の持ち主・
        system_admin。#3244）と、キーの業務 Agent の範囲。終わった Run はエラーにせず `result` で
        返す（画面の REST の 409 は変えない）。取消は Run を作らないので、キーの Run の数の上限
        （`PLATFORM_API_KEY_RUNS_PER_DAY`）に数えない。
        """
        caller = _principal(request)
        run = await run_in_threadpool(_readable_run, request, arguments.run_id)
        before = run.status
        if before not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
            try:
                run = await run_in_threadpool(
                    runtime_repository.cancel_run,
                    run.id,
                    cancelled_by=caller.login_user_id,
                    via=_cancel_via(request),
                )
            except KeyError as exc:
                raise McpToolError(
                    RUN_NOT_FOUND_CODE, "Run が見つかりません。", status=404
                ) from exc
            # 取り消した Run が始めた NL2SQL のジョブも止める（画面の取消と同じ。#3318）。
            agent_api._schedule_nl2sql_job_cancel(before, run)
        result = _cancel_result(before, run)
        return CancelRunOutput(
            run_id=run.id,
            status=str(run.status.value),
            result=result,
            message=_CANCEL_MESSAGES[result],
        )

    def skill_export(arguments: SkillExportInput) -> SkillExportOutput:
        _principal(request)
        # 使える業務 Agent（キーの範囲を含む）の公開中の版が使うスキルだけを出す。
        usable: set[str] = set()
        for agent in runtime_repository.list_agents():
            published = agent.published()
            if published is None or agent_unavailable_reason(agent) is not None:
                continue
            if _visible(request, agent.id):
                usable.update(published.skill_ids)
        skill = skill_registry.get(arguments.skill_id) if arguments.skill_id in usable else None
        if skill is None:
            raise McpToolError(SKILL_NOT_FOUND_CODE, "スキルが見つかりません。", status=404)
        exported = export_skill(skill)
        return SkillExportOutput(
            name=exported.name,
            files=[
                SkillFileOutput(path=item.path, content=item.content) for item in exported.files
            ],
        )

    return McpServer(
        name=MCP_SERVER_NAME,
        version="1",
        # 処理順の Step 形式（docs/agents/prompts.md。#3830）。
        instructions=(
            "業務 Agent（社内の資料・業務データを調べて答える専門家）に質問する。\n\n"
            "## Step 1: 業務 Agent を選んで質問する\n"
            "agent_list で使える業務 Agent を確かめ、agent_chat で質問する。\n\n"
            "## Step 2: 結果に応じて続ける\n"
            "in_progress なら agent_get_run で続きを読む。confirmation が返ったら、"
            "確認事項に agent_answer_confirmation で答える。続きの質問は返った thread_id を"
            "agent_chat に渡す。\n\n"
            "## Step 3: 回答の根拠を読む\n"
            "回答の根拠は sources にある。"
        ),
        tools=[
            McpTool(
                name="agent_list",
                title="業務 Agent の一覧",
                description=(
                    "使える業務 Agent（ID・名前・説明・使うスキル）の一覧を返す。"
                    "質問の内容に合う業務 Agent を選ぶのに使う。"
                ),
                input_model=AgentListInput,
                handler=list_agents,
                permissions=(_READ,),
                output_model=AgentListOutput,
            ),
            McpTool(
                name="agent_chat",
                title="業務 Agent への質問",
                description=(
                    "業務 Agent に質問し、回答（根拠つき）を返す。業務 Agent は社内の資料（RAG）・"
                    "業務データ（NL2SQL）などを調べて答える。thread_id を渡すと同じ会話の続きとして"
                    "答える。回答の前に確認が要るときは confirmation を返す。wait_seconds までに"
                    "終わらなければ in_progress=true と run_id を返す（agent_get_run で読む）。"
                    "承認の要る操作は行わない。"
                ),
                input_model=AgentChatInput,
                handler=chat,
                permissions=(_OPERATE,),
                read_only=False,
                output_model=RunOutput,
            ),
            McpTool(
                name="agent_get_run",
                title="実行の結果の取得",
                description=(
                    "agent_chat の run_id で、状態・回答・根拠・回答前の確認を返す。"
                    "まだ終わっていなければ wait_seconds まで待つ。"
                ),
                input_model=AgentGetRunInput,
                handler=get_run,
                permissions=(_READ,),
                output_model=RunOutput,
            ),
            McpTool(
                name="agent_get_run_observation",
                title="実行の観測の取得",
                description=(
                    "実行（run_id）の評価・監査に使う観測（調べた根拠の全件と場所・ツールの呼び出しの"
                    "経過・利用量）を返す。利用者への回答には agent_get_run を使う。"
                ),
                input_model=AgentGetRunObservationInput,
                handler=get_run_observation,
                permissions=(_READ,),
                output_model=RunObservation,
            ),
            McpTool(
                name="agent_answer_confirmation",
                title="回答前の確認への回答",
                description=(
                    "回答前の確認（agent_chat / agent_get_run の confirmation）に答える。"
                    "action=answer はすべての確認事項に、選択肢の ID（option_ids）か自由入力"
                    "（other_text）で答える。action=skip は確認せずに前提を示して回答させる"
                    "（can_skip のときだけ）。答えた後の回答を返す。"
                ),
                input_model=AgentAnswerConfirmationInput,
                handler=answer_confirmation,
                permissions=(_OPERATE,),
                read_only=False,
                output_model=RunOutput,
            ),
            McpTool(
                name="agent_cancel_run",
                title="実行の取り消し",
                description=(
                    "agent_chat の run_id の実行を止める（回答前の確認の待ちも閉じる）。"
                    "利用者が止めると言ったとき、回答を待たないと決めたときに使う。"
                ),
                input_model=AgentCancelRunInput,
                handler=cancel_run,
                permissions=(_OPERATE,),
                read_only=False,
                destructive=True,
                idempotent=True,
                open_world=False,
                output_model=CancelRunOutput,
            ),
            McpTool(
                name="agent_skill_export",
                title="スキルの書き出し",
                description=(
                    "業務 Agent が使うスキルを Agent Skills の形（SKILL.md と references/ の"
                    "ファイル）で返す。name のフォルダにファイルを置くと、外部の Agent でも"
                    "同じ手順で RAG・NL2SQL の MCP を使える。"
                ),
                input_model=SkillExportInput,
                handler=skill_export,
                permissions=(_READ,),
                output_model=SkillExportOutput,
            ),
        ],
    )


def has_any_permission_for(request: Request) -> Callable[[frozenset[str]], bool]:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        return lambda _permissions: False
    return principal.has_any_permission


@router.post("/mcp", response_class=Response)
async def mcp_endpoint(request: Request) -> Response:
    """MCP の JSON-RPC（initialize / ping / tools/list / tools/call）を処理する。

    認証は共通の API キー（`authorize_api_request` の `api_key_paths`）。ツールごとの権限は
    ツールが利用者の capability で判定する。
    """
    if not isinstance(getattr(request.state, "principal", None), Principal):
        raise HTTPException(
            status_code=401, detail="API キーを Authorization header で指定してください。"
        )
    return await mcp_http_response(
        request,
        build_agent_mcp_server(request),
        has_any_permission=has_any_permission_for(request),
    )
