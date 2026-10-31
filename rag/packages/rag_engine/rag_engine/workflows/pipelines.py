"""個別サービスを同期で組み合わせる ingestion / query pipeline。"""

from dataclasses import replace
from time import monotonic

from rag_engine.chunking import ChunkingConfig
from rag_engine.generation.service import GenerationService
from rag_engine.indexing import IndexingService
from rag_engine.models.contracts import (
    AnswerResult,
    ChunkRequest,
    GenerationRequest,
    IndexRequest,
    IndexResult,
    ParseRequest,
    SearchRequest,
    StageError,
    TraceStep,
)
from rag_engine.parsing.service import ParsingService
from rag_engine.ports import Chunker, Generator, Indexer, Parser, QueryPlanner, Reranker, Retriever
from rag_engine.retrieval.service import RetrievalService


def _stage(name, trace, operation):
    start = monotonic()
    try:
        result = operation()
    except Exception as exc:
        trace.append(TraceStep(name, "failed", monotonic() - start))
        raise StageError(name, tuple(trace)) from exc
    trace.append(TraceStep(name, elapsed_seconds=monotonic() - start))
    return result


class IngestionPipeline:
    """解析・分割・索引の順に実行する。失敗した後の工程は呼び出さない。"""

    def __init__(self, parser: Parser, chunker: Chunker, indexer: Indexer):
        self.parser, self.chunker, self.indexer = ParsingService(parser), chunker, IndexingService(indexer)

    def run(self, request: ParseRequest, *, collection: str, config: ChunkingConfig | None = None) -> IndexResult:
        """索引結果を返す。部分成果物の保存は個別 adapter の契約に従う。"""
        if not collection.strip():
            raise ValueError("collection must not be empty")
        checked_config = (config or ChunkingConfig()).validate()
        trace = []
        document = _stage("parse", trace, lambda: self.parser.parse(request))
        batch = _stage("chunk", trace, lambda: self.chunker.chunk(ChunkRequest(document, checked_config)))
        return _stage("index", trace, lambda: self.indexer.index(IndexRequest(batch, collection)))


class QueryPipeline:
    """質問準備・検索・回答を組み合わせる。外部実装を構築時に注入する。"""

    def __init__(
        self, retriever: Retriever, generator: Generator, *, planner: QueryPlanner | None = None, reranker: Reranker | None = None
    ):
        self.retriever = RetrievalService(retriever, reranker)
        self.generator = GenerationService(generator)
        self.planner = planner

    def run(self, request: SearchRequest) -> AnswerResult:
        """工程付きの回答を返す。空検索時は生成を skipped として記録する。"""
        request.validate()
        trace = []
        planned = _stage("plan", trace, lambda: self.planner.plan(request)) if self.planner else request
        found = _stage("retrieve", trace, lambda: self.retriever.retrieve(planned))
        trace.extend(found.trace)
        answer = _stage("generate", trace, lambda: self.generator.generate(GenerationRequest(request.question, found.evidence)))
        if not found.evidence:
            trace[-1] = replace(trace[-1], status="skipped", detail="no evidence")
        return replace(answer, trace=tuple(trace) + answer.trace)
