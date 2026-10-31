"""質問文から、後続処理が実際に使う情報だけを読み取る。

以前は回答方針（executor）・タスク・質問タイプも判定して表示していたが、検索戦略にも
回答フローにも分岐がなく、表示のためだけのラベルになっていた。後続が使うのは、
質問理解（検索の絞り込み・候補の加点・回答に渡す条件）だけである。根拠の画像を回答に添付するかは、質問ではなく
回答のモデルが画像を読めるかで決める（#3444）。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any

from rag_engine.retrieval.inquiry_conditions import InquiryConditionParse, parse_inquiry_conditions


@dataclass(frozen=True)
class QuestionPlan:
    """質問理解を保持します。"""

    original_question: str
    inquiry_conditions: InquiryConditionParse | None = None

    def to_payload(self) -> dict[str, Any]:
        """UI と trace 保存に使う JSON 互換 payload へ変換します。"""
        return {"original_question": self.original_question}


def plan_question(question: str) -> QuestionPlan:
    """質問文を解析します。"""
    text = unicodedata.normalize("NFKC", str(question or "")).strip()
    return QuestionPlan(text, inquiry_conditions=parse_inquiry_conditions(text))
