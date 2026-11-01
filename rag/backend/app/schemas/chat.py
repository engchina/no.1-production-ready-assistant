"""チャット(会話 / マルチモデル比較)関連スキーマ。

会話は検索・回答プロファイル(Search Answer Profile)配下に置く。検索・回答は既存 RAG パイプラインを
再利用し、ASSISTANT メッセージは生成モデル・引用・trace を保持する。
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pr_system_settings.chat_attachments import ChatAttachmentReference
from pydantic import BaseModel, Field, field_validator

from app.schemas.search import SEARCH_TOP_K_MAX, RetrievedChunk
from app.schemas.search_answer_profile_knowledge import ClarificationAnswer
from app.search_defaults import search_top_k_default, search_top_k_schema


class ConversationStatus(StrEnum):
    """会話の状態。"""

    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class MessageRole(StrEnum):
    """メッセージの役割。"""

    USER = "USER"
    ASSISTANT = "ASSISTANT"
    SYSTEM = "SYSTEM"


class MessageStatus(StrEnum):
    """メッセージの確定状態。

    回答は作成を始めた時点で ``STREAMING`` で保存し、終わったら ``COMPLETE``・``ERROR``（失敗・
    中断）・``CANCELLED``（利用者の停止）にする（#1175）。
    """

    STREAMING = "STREAMING"
    COMPLETE = "COMPLETE"
    ERROR = "ERROR"
    CANCELLED = "CANCELLED"


class ChatClarificationRecordEntry(BaseModel):
    """回答前の確認の 1 件の確認事項と、利用者の答え（#1839）。"""

    prompt: str
    # 選んだ選択肢の名前（答えた時点の名前。ルール・業務ガイドが後で変わっても
    # 会話の記録は変えない）。
    selected: list[str] = Field(default_factory=list)
    other_text: str = ""
    # 選択肢の無い（自由入力だけで答える）確認事項。画面は自由入力の答えに「その他: 」を
    # 付けない（答えた時点の写し。#3410）。
    free_text_only: bool = False


class ChatClarificationRecord(BaseModel):
    """回答前に確認した内容（#1839）。USER のメッセージに付け、会話の利用者の側に出す。"""

    # answered = 答えた / skipped = 「確認せずに回答」で飛ばした（前提を示して回答した）。
    outcome: Literal["answered", "skipped"]
    entries: list[ChatClarificationRecordEntry] = Field(default_factory=list)


class ChatMessage(BaseModel):
    """会話中の 1 メッセージ。ASSISTANT は引用・モデル・trace を含む。"""

    message_id: str
    conversation_id: str
    role: MessageRole
    content: str
    model: str | None = None
    citations: list[RetrievedChunk] = Field(default_factory=list)
    # 保存した引用のうち、見る人が今の権限で見られない根拠の数（抜粋・文書は返さない。#3252）。
    withheld_evidence_count: int = 0
    trace_id: str | None = None
    status: MessageStatus = MessageStatus.COMPLETE
    reply_to_message_id: str | None = None
    created_at: datetime
    # 回答に付けた注意書き（ASSISTANT の回答だけ。#2405）。確認の質問・失敗・停止・この機能より前の
    # 回答は None。会話の履歴としてモデルに渡す本文（content）には含めない。
    notice: str | None = None
    # 回答前に確認した内容（USER のメッセージだけ。確認しなかった質問は None。#1839）。
    clarification: ChatClarificationRecord | None = None
    # 会話の持ち主が送った質問か。#3244 より前に system_admin が他の利用者の会話で送った質問は
    # false（今は持ち主しか送れない）。
    sent_by_owner: bool = True
    # 持ち主以外が送った質問の、送った人の表示名（#1928・#1696。引けない利用者は空）。
    sender_display_name: str = ""
    # 質問に添付したファイル（#2517。名前・種類・大きさ）。添付の無い質問・回答は空。
    attachments: list[ChatAttachmentReference] = Field(default_factory=list)
    # 回答の段落の印・根拠と矛盾して外した段落・回答全体の警告（ASSISTANT の回答だけ。#2738。形は
    # platform/contracts/answer-passages/passage-marks.json）。記録の無い回答は空。
    passage_marks: list[dict[str, Any]] = Field(default_factory=list)
    removed_passages: list[dict[str, Any]] = Field(default_factory=list)
    answer_warnings: list[dict[str, Any]] = Field(default_factory=list)
    # 回答のモデル（``model``）へ添付した根拠の画像の件数（ASSISTANT の終わった回答だけ。回答の
    # 記録の診断の ``models.attached_images``。#3444）。1 件以上なら回答の見出しに「画像も参照」を
    # 出す。
    # 画像を添付しなかった回答・記録の無い回答は 0。
    answer_attached_images: int = 0


class ChatAnswerCancelResult(BaseModel):
    """回答の作成の取消の結果(#1175)。作成中の回答を止めた・止める予定なら True。"""

    cancelled: bool


class ConversationSummary(BaseModel):
    """会話一覧用の要約。"""

    id: str
    search_answer_profile_id: str
    title: str | None = None
    status: ConversationStatus = ConversationStatus.ACTIVE
    message_count: int = 0
    created_at: datetime
    updated_at: datetime
    # 見ている利用者の会話か。system_admin が他の利用者の会話を見るときは false（画面は会話の
    # 履歴に利用者の表示名を添え、入力欄・名前の変更・評価を出さない。閲覧・停止・削除だけ。
    # #1889・#3244）。
    owned: bool = True
    # system_admin の一覧・詳細だけに付く利用者の表示名（#1889・#1696。引けない利用者は空）。
    actor_display_name: str = ""


class ConversationDetail(ConversationSummary):
    """会話詳細。メッセージ列を同梱する。"""

    messages: list[ChatMessage] = Field(default_factory=list)


class ConversationCreateRequest(BaseModel):
    """会話作成リクエスト。"""

    search_answer_profile_id: str = Field(..., min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=400)

    @field_validator("search_answer_profile_id")
    @classmethod
    def strip_search_answer_profile_id(cls, value: str) -> str:
        """検索・回答プロファイル ID の前後空白を除去する。"""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("検索・回答プロファイルを指定してください。")
        return cleaned

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str | None) -> str | None:
        """空文字のタイトルは未指定として扱う。"""
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class ConversationUpdateRequest(BaseModel):
    """会話タイトル更新リクエスト。"""

    title: str = Field(..., min_length=1, max_length=80)

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, value: object) -> object:
        """空白だけのタイトルを拒否し、前後空白を除去する。"""
        if not isinstance(value, str):
            return value
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("会話名を入力してください。")
        return cleaned


class ChatMessageRequest(BaseModel):
    """会話へのメッセージ送信リクエスト。"""

    content: str = Field(..., min_length=1, max_length=8000)
    question_context_token: str | None = Field(default=None, min_length=1, max_length=300000)
    # 複数指定でマルチモデル比較(設定済み OCI モデル間)。空なら既定モデル 1 系統。
    model_ids: list[str] = Field(default_factory=list, max_length=5)
    # 旧 standard の検索モード(``mode``)は #595 で削除した。送られても読み捨てる。
    # 検索する件数。チャットの画面は送らず、RAG 検索の既定と同じ値を使う(#3512)。
    top_k: int = Field(
        default_factory=search_top_k_default,
        ge=1,
        le=SEARCH_TOP_K_MAX,
        json_schema_extra=search_top_k_schema,
    )
    # 利用者が選んだ類似の承認済み FAQ の id(#684)。b
    # ackend が検索・回答プロファイルの FAQ から引き直し、
    #
    # 質問と一緒に回答の LLM へ渡す。利用者の送った文を FAQ として扱わない。
    approved_faq_id: str | None = Field(default=None, min_length=1, max_length=200)
    # 利用者が確認の質問に答えた内容(#717)。backen
    # d が検索・回答プロファイルのルールから引き直し、選んだ
    #
    # 章節のページに絞って検索し、選んだ条件を回答の前提にする。
    clarification: ClarificationAnswer | None = None
    # 回答前の確認を「確認せずに回答」で飛ばしたときの確認の id（#1839）。回答は変えず、会話に
    # 「確認せずに回答しました」と確認事項を残すためだけに使う（ルール・業務ガイドから引き直す）。
    clarification_skipped_rule_id: str | None = Field(default=None, min_length=1, max_length=200)
    # 画面が決めた質問（USER のメッセージ）の id（#1175）。送信の直後（`start` の前）の停止でも、
    # この id で回答の作成を取り消せる（NL2SQL の `client_job_id`（#900）と同じ考え方）。
    client_message_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    # 質問に添付するファイル（#2517。`POST /api/chat-attachments` の `attachment_id`）。送信で会話に
    # 結び付け、会話の続きの質問も根拠に使う。件数の上限は共通の設定（API が確かめる）。
    attachment_ids: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("content")
    @classmethod
    def strip_content(cls, value: str) -> str:
        """本文の前後空白を除去し、空入力を拒否する。"""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("メッセージを入力してください。")
        return cleaned

    @field_validator("model_ids")
    @classmethod
    def dedupe_model_ids(cls, values: list[str]) -> list[str]:
        """モデル ID の前後空白と重複を取り除く。"""
        seen: set[str] = set()
        normalized: list[str] = []
        for value in values:
            cleaned = value.strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            normalized.append(cleaned)
        return normalized
