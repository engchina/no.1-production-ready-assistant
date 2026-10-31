"""実行履歴（チャット・RAG 検索の実行。#1890）の API スキーマ。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pr_backend_core.observability.model_usage import RunModelUsage, UsageMeasurement
from pydantic import BaseModel, Field

from app.schemas.common import JsonValue
from app.schemas.search import RetrievedChunk, StorageFailure

RunSurface = Literal["search", "chat"]
RunStatus = Literal["success", "blocked", "error"]
RunVerification = Literal["verified", "partial", "unverified"]
ValidationExecution = Literal[
    "disabled", "not_run", "error", "timed_out", "passed", "failed", "partial", "unknown"
]


class RunSummary(BaseModel):
    """実行履歴の一覧の行（質問は先頭の一部だけ）。"""

    run_id: str
    surface: RunSurface
    search_answer_profile_id: str | None = None
    # 検索・回答プロファイルの名前（削除・読めないときは None。画面は ID を出さない）。
    search_answer_profile_name: str | None = None
    # 安全チェックの後の質問。安全チェックで止めた質問は保存しないので None。
    question: str | None = None
    # 回答を作った実行か（RAG 検索の既定は回答を作らない。#649）。
    answer_generated: bool
    status: RunStatus
    # 回答の対応（answered / conditional / insufficient_evidence など。#1235）。
    answer_outcome: str | None = None
    # 回答の検証（回答の監査で要求ごとに根拠で答えられたか）。回答を作らない実行は None。
    verification: RunVerification | None = None
    validation_execution: ValidationExecution | None = None
    confidence: str | None = None
    needs_human_review: bool | None = None
    citation_count: int = 0
    elapsed_ms: float | None = None
    error_stage: str | None = None
    model_id: str | None = None
    conversation_id: str | None = None
    message_id: str | None = None
    created_at: datetime
    # 見ている利用者自身が実行したか（#1954。持ち主の会話で system_admin が実行した行は、持ち主には
    # false）。
    executed_by_me: bool = False
    # 実行した利用者（実行者）の表示名。見る人を問わず返す（#1954）。引けない利用者（削除済み
    # など）は空文字（画面は「削除済みのユーザー」と出す）。
    executor_display_name: str = ""
    # モデルの利用量の合計（入力 + 出力の token。#3419・#3466）と計測の状態（complete / partial）。
    # 利用量の記録の無い実行（この機能より前・trace の始まる前に失敗）はどちらも None（画面は
    # 「未計測」。0 にしない）。LLM を呼ばなかった実行は 0 と complete。
    total_tokens: int | None = None
    usage_measurement: UsageMeasurement | None = None


class RunFeedback(BaseModel):
    """実行への利用者フィードバック（送った利用者・対象ごとの最新の票）。"""

    feedback_id: str
    target_type: Literal["answer", "citation"]
    rating: Literal["helpful", "not_helpful"]
    reason: str | None = None
    document_id: str | None = None
    created_at: datetime


class RunSearchConditions(BaseModel):
    """RAG 検索の実行の条件（件数・画面の絞り込み。#3309）。

    実行の詳細の「RAG 検索で開く」が画面に戻す。
    """

    top_k: int | None = None
    # 文書の分類・基準日・抽出項目の値（`filters` のキー → 値。画面が送れる条件だけ）。
    filters: dict[str, str] = Field(default_factory=dict)


class RunDetail(RunSummary):
    """実行の詳細（質問の全文・回答・根拠・回答の診断・処理の段・フィードバック）。"""

    error_type: str | None = None
    # 失敗のエラーコード（ORA-03146 など。例外の本文は持たない。#2342）。
    error_code: str | None = None
    # 回答を作ったが保存できなかった記録（回答の記録・会話の回答。#2342）。
    storage_failures: list[StorageFailure] = Field(default_factory=list)
    # 会話の持ち主の表示名（#1954）。会話の無い実行（RAG 検索）・会話が削除された実行は None、
    # 引けない利用者は空文字。
    conversation_owner_display_name: str | None = None
    # 見ている利用者が会話の持ち主か（会話を開く導線は持ち主と system_admin だけ）。会話の無い
    # 実行は None。
    owned_by_me: bool | None = None
    answer: str | None = None
    # 回答の記録が保持期間で消えた・読めないとき False（回答を作った実行だけ意味がある）。
    answer_available: bool = False
    # 回答に付けた注意書き（回答の記録から読む。#2405）。
    answer_notice: str | None = None
    citations: list[RetrievedChunk] = Field(default_factory=list)
    # 保存した根拠のうち、見る人が今の権限で見られない根拠の数（抜粋・文書は返さない。#3252）。
    withheld_evidence_count: int = 0
    answer_diagnostics: dict[str, JsonValue] | None = None
    feedback: list[RunFeedback] = Field(default_factory=list)
    # RAG 検索の実行の条件（#3309）。チャットの実行と、この機能より前の記録は None。
    search_conditions: RunSearchConditions | None = None
    # モデルの呼び出しの回数と利用量（#3419）。この機能より前の記録・trace の始まる前に失敗した
    # 実行は None（画面は「未計測」）。LLM を呼ばなかった実行は requests=0 の実測。
    usage: RunModelUsage | None = None
