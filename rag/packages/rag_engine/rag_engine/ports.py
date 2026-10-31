"""外部実装が満たす最小契約。import 時に SDK や接続を初期化しない。"""

from collections.abc import Sequence
from typing import Protocol

from rag_engine.models.contracts import (
    AnswerResult,
    ChunkBatch,
    ChunkRequest,
    Evidence,
    GenerationRequest,
    IndexRequest,
    IndexResult,
    ParsedDocument,
    ParseRequest,
    SearchRequest,
    SearchResult,
)


class Parser(Protocol):
    """元ファイルを解析する。外部 API・モデル利用は実装側が管理する。"""

    def parse(self, request: ParseRequest) -> ParsedDocument:
        """解析済み文書を返す。失敗時は例外を送出する。"""
        ...


class Chunker(Protocol):
    """解析結果を親子チャンクに変換する。"""

    def chunk(self, request: ChunkRequest) -> ChunkBatch:
        """ファイルを保存せずチャンク集合を返す。"""
        ...


class Embedder(Protocol):
    """検索文・文書の embedding を生成する。"""

    def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
        """入力順の vector を返す。次元は adapter の設定で固定する。"""
        ...


class Indexer(Protocol):
    """チャンクと embedding の索引を保存する。"""

    def index(self, request: IndexRequest) -> IndexResult:
        """書き込み結果を返す。失敗時の rollback は実装が保証する。"""
        ...


class Retriever(Protocol):
    """検索条件から根拠を取得する。"""

    def retrieve(self, request: SearchRequest) -> SearchResult:
        """順位付き根拠を返す。利用不能と空結果を区別する。"""
        ...


class Reranker(Protocol):
    """取得済み根拠を並べ替える。"""

    def rerank(self, question: str, evidence: tuple[Evidence, ...]) -> tuple[Evidence, ...]:
        """関連度順の根拠を返す。"""
        ...


class Generator(Protocol):
    """渡された根拠から回答を生成する。"""

    def generate(self, request: GenerationRequest) -> AnswerResult:
        """引用と不足理由を含む構造化回答を返す。"""
        ...


class QueryPlanner(Protocol):
    """検索の前に質問や query を展開する。"""

    def plan(self, request: SearchRequest) -> SearchRequest:
        """元要求を変更せず新しい検索要求を返す。"""
        ...


class ArtifactStore(Protocol):
    """アプリが所有する成果物の byte 単位 I/O。"""

    def read(self, key: str) -> bytes:
        """相対 key のデータを返す。不存在は FileNotFoundError。"""
        ...

    def write(self, key: str, data: bytes) -> None:
        """相対 key を原子的に保存する。"""
        ...
