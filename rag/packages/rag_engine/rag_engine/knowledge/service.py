"""FAQ・用語・フィードバックをアプリ固有の保存先で扱う。"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from rag_engine.knowledge import approved_faq as faq
from rag_engine.knowledge.domain_keywords import load_domain_keywords, save_domain_keywords
from rag_engine.resources.runtime import Runtime


class KnowledgeService:
    """既存アプリの知識管理を UI なしで再利用する。"""

    def __init__(self, runtime: Runtime):
        self.runtime = runtime

    def _faq_path(self) -> Path:
        if self.runtime.paths.faq is None:
            raise ValueError("Configure an explicit FAQ path before using FAQ management")
        return self.runtime.paths.faq

    def list_faq(self) -> list[faq.ApprovedFaqRecord]:
        """明示された FAQ を読む。未設定時は既存業務 FAQ を読まず空一覧を返す。"""
        return faq.load_approved_faq_records(self.runtime.paths.faq) if self.runtime.paths.faq is not None else []

    def add_faq(self, question: str, approved_answer: str) -> faq.ApprovedFaqMutationResult:
        """FAQ を保存し、既存の backup・重複判定・改訂規約を維持する。"""
        with self.runtime.activate():
            return faq.add_approved_faq_record(self._faq_path(), question=question, approved_answer=approved_answer)

    def delete_faq(self, selectors: str | Sequence[str]) -> faq.ApprovedFaqMutationResult:
        """明示された ID の FAQ を削除する。元データは既存の backup 規約で保存する。"""
        with self.runtime.activate():
            return faq.delete_approved_faq_records(self._faq_path(), selectors)

    def suggest(self, question: str, **options) -> list[faq.ApprovedFaqSuggestion]:
        """このアプリの FAQ のみを候補にし、任意の分類・semantic index を適用する。"""
        with self.runtime.activate():
            return faq.suggest_approved_faq_questions(question, self.list_faq(), **options)

    def promote_feedback(self, record: dict[str, Any]) -> faq.ApprovedFaqMutationResult | None:
        """レビュー済みフィードバックをこのアプリの FAQ に昇格する。"""
        with self.runtime.activate():
            return faq.promote_answer_feedback_to_approved_faq(self._faq_path(), record)

    def keywords(self) -> list[str]:
        """このアプリの作業領域にある用語を読む。"""
        return load_domain_keywords(self.runtime.paths.output)

    def save_keywords(self, values):
        """このアプリの用語を保存する。"""
        return save_domain_keywords(self.runtime.paths.output, values)
