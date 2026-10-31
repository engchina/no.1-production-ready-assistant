"""検索と任意の再順位付けを組み合わせるサービス。"""

from dataclasses import replace

from rag_engine.models.contracts import SearchRequest, SearchResult
from rag_engine.ports import Reranker, Retriever


class RetrievalService:
    """検索不能の例外は維持し、別バックエンドへ暗黙に切り替えない。"""

    def __init__(self, retriever: Retriever, reranker: Reranker | None = None):
        self.retriever, self.reranker = retriever, reranker

    def retrieve(self, request: SearchRequest) -> SearchResult:
        """根拠を取得する。空結果では reranker を呼ばない。"""
        request.validate()
        result = self.retriever.retrieve(request)
        if self.reranker is not None and result.evidence:
            result = replace(result, evidence=self.reranker.rerank(request.question, result.evidence))
        return result
