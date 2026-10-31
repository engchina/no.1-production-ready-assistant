"""利用者フィードバックの低機密スキーマ。"""

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.common import Page


class FeedbackRating(StrEnum):
    """フィードバック評価。"""

    HELPFUL = "helpful"
    NOT_HELPFUL = "not_helpful"


class FeedbackReason(StrEnum):
    """改善箇所を特定する低機密理由カテゴリ。"""

    INCORRECT = "incorrect"
    INCOMPLETE = "incomplete"
    MISSING_EVIDENCE = "missing_evidence"
    NOT_RELEVANT = "not_relevant"
    ANSWER_UNTRUSTED = "answer_untrusted"
    # rag_poc の回答 feedback 分類(ナレッジ不足 / 情報が古い / 質問が曖昧)。
    MISSING_KNOWLEDGE = "missing_knowledge"
    OUTDATED_SOURCE = "outdated_source"
    AMBIGUOUS_QUESTION = "ambiguous_question"


class FeedbackTargetType(StrEnum):
    """評価対象。"""

    ANSWER = "answer"
    CITATION = "citation"


class FeedbackSourceSurface(StrEnum):
    """フィードバックを送信した画面。"""

    SEARCH = "search"
    CHAT = "chat"


class FeedbackContentSource(StrEnum):
    """管理画面へ保存する本文の取得元。"""

    CHAT_MESSAGE = "chat_message"
    SEARCH_SNAPSHOT = "search_snapshot"


class FeedbackSortOrder(StrEnum):
    """フィードバック一覧の時刻順。"""

    NEWEST = "newest"
    OLDEST = "oldest"


ANSWER_REASONS = {
    FeedbackReason.INCORRECT,
    FeedbackReason.INCOMPLETE,
    FeedbackReason.NOT_RELEVANT,
    FeedbackReason.ANSWER_UNTRUSTED,
    FeedbackReason.MISSING_KNOWLEDGE,
    FeedbackReason.OUTDATED_SOURCE,
    FeedbackReason.AMBIGUOUS_QUESTION,
}
# 修正した回答(rag_poc の corrected_answer)の上限。Approved FAQ の回答の上限に合わせる。
CORRECTED_ANSWER_MAX_CHARS = 20_000
CITATION_REASONS = {
    FeedbackReason.MISSING_EVIDENCE,
    FeedbackReason.NOT_RELEVANT,
    FeedbackReason.ANSWER_UNTRUSTED,
}


def _clean_identifier(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if any(ord(character) < 32 or ord(character) == 127 for character in cleaned):
        raise ValueError("ID に制御文字は使用できません。")
    return cleaned


class FeedbackCitationSnapshot(BaseModel):
    """feedback 時点の根拠参照。本文全体ではなく調査に必要な抜粋だけを保存する。"""

    document_id: str = Field(..., min_length=1, max_length=64)
    chunk_id: str = Field(..., min_length=1, max_length=128)
    file_name: str | None = Field(default=None, max_length=512)
    section_title: str | None = Field(default=None, max_length=1000)
    page_number: int | None = Field(default=None, ge=1)
    content_preview: str | None = Field(default=None, max_length=2000)
    rerank_score: float | None = None

    @field_validator("document_id", "chunk_id")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        return _clean_identifier(value) or ""


class FeedbackContentSnapshot(BaseModel):
    """検索画面で利用者が実際に見た質問・回答・根拠の snapshot。

    回答を生成しない検索（検索結果だけ）の引用の評価では回答が無いため、回答は任意にする
    （質問は必須。どの質問への評価かを一覧・詳細に出すため。#978）。回答の評価は
    `FeedbackRequest` で回答を必須にする。
    """

    question: str = Field(..., min_length=1, max_length=20_000)
    answer: str | None = Field(default=None, max_length=100_000)
    citations: list[FeedbackCitationSnapshot] = Field(default_factory=list, max_length=50)

    @field_validator("question")
    @classmethod
    def normalize_question(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("質問の本文を入力してください。")
        return cleaned

    @field_validator("answer")
    @classmethod
    def normalize_answer(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None


class FeedbackRequest(BaseModel):
    """回答または引用に対するフィードバック登録。"""

    trace_id: str = Field(..., min_length=1, max_length=64)
    search_answer_profile_id: str = Field(..., min_length=1, max_length=64)
    target_type: FeedbackTargetType
    source_surface: FeedbackSourceSurface
    document_id: str | None = Field(default=None, max_length=64)
    chunk_id: str | None = Field(default=None, max_length=128)
    message_id: str | None = Field(default=None, max_length=64)
    content_snapshot: FeedbackContentSnapshot | None = None
    rating: FeedbackRating
    reason: FeedbackReason | None = None
    comment: str | None = Field(default=None, max_length=1000)
    corrected_answer: str | None = Field(default=None, max_length=CORRECTED_ANSWER_MAX_CHARS)

    @field_validator(
        "trace_id", "search_answer_profile_id", "document_id", "chunk_id", "message_id"
    )
    @classmethod
    def validate_identifier(cls, value: str | None) -> str | None:
        return _clean_identifier(value)

    @field_validator("comment", "corrected_answer")
    @classmethod
    def normalize_comment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = "\n".join(line.rstrip() for line in value.strip().splitlines()).strip()
        return cleaned or None

    @model_validator(mode="after")
    def validate_target_and_reason(self) -> Self:
        if self.message_id is not None and self.content_snapshot is not None:
            raise ValueError("メッセージ ID と画面 snapshot は同時に指定できません。")
        if self.message_id is not None and self.source_surface != FeedbackSourceSurface.CHAT:
            raise ValueError("メッセージ ID はチャットの評価だけに指定できます。")
        if (
            self.content_snapshot is not None
            and self.source_surface != FeedbackSourceSurface.SEARCH
        ):
            raise ValueError("画面 snapshot は RAG 検索の評価だけに指定できます。")
        if (
            self.content_snapshot is not None
            and self.content_snapshot.answer is None
            and self.target_type == FeedbackTargetType.ANSWER
        ):
            raise ValueError("回答の評価には回答の本文を入力してください。")
        if self.target_type == FeedbackTargetType.CITATION:
            if not self.document_id or not self.chunk_id:
                raise ValueError("引用フィードバックには文書 ID とチャンク ID が必要です。")
            allowed_reasons = CITATION_REASONS
        else:
            if self.document_id is not None or self.chunk_id is not None:
                raise ValueError("回答フィードバックには文書 ID とチャンク ID を指定できません。")
            allowed_reasons = ANSWER_REASONS

        if self.corrected_answer is not None and self.target_type != FeedbackTargetType.ANSWER:
            raise ValueError("修正した回答は回答のフィードバックだけに指定できます。")
        if self.rating == FeedbackRating.HELPFUL:
            self.reason = None
            self.comment = None
            self.corrected_answer = None
        elif self.reason is None:
            raise ValueError("役に立たなかった理由を選択してください。")
        elif self.reason not in allowed_reasons:
            raise ValueError("評価対象に対応していない理由です。")
        return self

    @property
    def comment_hash(self) -> str | None:
        if self.comment is None:
            return None
        return hashlib.sha256(self.comment.encode("utf-8")).hexdigest()

    @property
    def comment_chars(self) -> int:
        return len(self.comment or "")


class FeedbackSubmissionResponse(BaseModel):
    """フィードバック登録結果。"""

    feedback_id: str
    trace_id: str
    search_answer_profile_id: str | None = None
    target_type: FeedbackTargetType
    source_surface: FeedbackSourceSurface | None = None
    document_id: str | None = None
    chunk_id: str | None = None
    message_id: str | None = None
    rating: FeedbackRating
    reason: FeedbackReason | None = None
    comment: str | None = None
    corrected_answer: str | None = None


class CurrentFeedbackItem(FeedbackSubmissionResponse):
    """現在の利用者について有効な最新フィードバック。"""

    created_at: datetime


class FeedbackReasonCount(BaseModel):
    """低評価理由別の件数。"""

    reason: FeedbackReason
    count: int = Field(ge=0)


class FeedbackSummary(BaseModel):
    """有効票の集計。"""

    total: int = 0
    helpful_count: int = 0
    not_helpful_count: int = 0
    helpful_rate: float = 0.0
    answer_total: int = 0
    answer_helpful_rate: float = 0.0
    citation_total: int = 0
    citation_helpful_rate: float = 0.0
    reason_counts: list[FeedbackReasonCount] = Field(default_factory=list)


class FeedbackItem(CurrentFeedbackItem):
    """管理画面に表示するフィードバック明細。"""

    search_answer_profile_name: str | None = None
    conversation_id: str | None = None
    conversation_title: str | None = None
    model: str | None = None
    file_name: str | None = None
    question_preview: str | None = None
    comment_preview: str | None = None
    has_comment: bool = False


class FeedbackExecutionInfo(BaseModel):
    """trace から取得できる検索・生成の低機密診断。"""

    outcome: str | None = None
    search_mode: str | None = None
    elapsed_ms: float | None = None
    retrieved_count: int | None = None
    reranked_count: int | None = None
    citation_count: int | None = None
    guardrail_codes: list[str] = Field(default_factory=list)
    config_fingerprint: str | None = None


class FeedbackDetail(FeedbackItem):
    """管理者用 drawer で遅延取得する feedback 全文。"""

    content_source: FeedbackContentSource | None = None
    question: str | None = None
    answer: str | None = None
    comment: str | None = None
    citations: list[FeedbackCitationSnapshot] = Field(default_factory=list)
    execution: FeedbackExecutionInfo = Field(default_factory=FeedbackExecutionInfo)


class FeedbackApprovedFaqPromotion(BaseModel):
    """フィードバックを検索・回答プロファイルの Approved FAQ へ登録した結果。"""

    search_answer_profile_id: str
    question: str
    inserted_count: int = Field(ge=0)
    deleted_count: int = Field(ge=0)


class FeedbackDashboard(BaseModel):
    """専用画面向けの集計とページング済み明細。"""

    summary: FeedbackSummary
    previous_summary: FeedbackSummary | None = None
    items: Page[FeedbackItem]
