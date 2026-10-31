"""指定された索引実装へチャンクを保存する。"""

from rag_engine.models.contracts import IndexRequest, IndexResult
from rag_engine.ports import Indexer


class IndexingService:
    """保存先を明示した索引サービス。"""

    def __init__(self, indexer: Indexer):
        self.indexer = indexer

    def index(self, request: IndexRequest) -> IndexResult:
        """空の保存先・空チャンクを外部呼び出し前に拒否する。"""
        if not request.collection.strip() or not request.batch.chunks:
            raise ValueError("collection and chunks must not be empty")
        return self.indexer.index(request)
