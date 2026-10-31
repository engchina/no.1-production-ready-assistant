"""UI・プロバイダー・保存形式に依存しない同期 SDK の契約。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rag_engine.chunking import ChunkingConfig, DocumentChunk
from rag_engine.models.layout import LayoutRecord, PageImage


@dataclass(frozen=True)
class ParseRequest:
    """解析元とページ指定。全文解析が既定で、ファイルは変更しない。"""

    source: Path
    page_range: str = ""
    entire_file: bool = True
    vision: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedDocument:
    """解析データ。ページ番号・bbox は元の LayoutRecord 規約を維持する。

    document_id は独立索引で同一文書を更新するための安定した識別子とし、
    別文書には別 ID を渡す。PDF がない場合も保存後の索引キーに使用する。
    """

    document_id: str
    source_name: str
    records: tuple[LayoutRecord, ...]
    pages: tuple[PageImage, ...] = ()
    source_run_id: str = ""
    page_count: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)
    classification: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChunkRequest:
    """構造化解析結果と分割設定。保存処理を要求しない。"""

    document: ParsedDocument
    config: ChunkingConfig = field(default_factory=ChunkingConfig)


@dataclass(frozen=True)
class ChunkBatch:
    """文書単位の親子チャンク集合。"""

    document: ParsedDocument
    chunks: tuple[DocumentChunk, ...]
    config: ChunkingConfig


@dataclass(frozen=True)
class IndexRequest:
    """指定した論理 collection にチャンクを書き込む要求。"""

    batch: ChunkBatch
    collection: str


@dataclass(frozen=True)
class IndexResult:
    """索引書き込み件数とバックエンドの識別子。"""

    collection: str
    indexed_count: int
    index_id: str = ""


@dataclass(frozen=True)
class TraceStep:
    """処理工程の結果。elapsed_seconds の単位は秒。"""

    name: str
    status: str = "complete"
    elapsed_seconds: float = 0.0
    detail: str = ""


@dataclass(frozen=True)
class Evidence:
    """テキストと元の出典情報。metadata に score・bbox・画像参照を保持できる。"""

    id: str
    text: str
    source: str = ""
    page: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SearchRequest:
    """検索条件。collection の解釈は adapter の公開契約に従う。"""

    question: str
    collection: str
    top_k: int = 20
    neighbor_count: int = 3
    filters: Mapping[str, str] = field(default_factory=dict)
    queries: tuple[str, ...] = ()

    def validate(self) -> None:
        """空質問・空 collection・不正な件数は外部呼び出し前に拒否する。"""
        if not self.question.strip() or not self.collection.strip():
            raise ValueError("question and collection must not be empty")
        if not 1 <= self.top_k <= 50 or not 0 <= self.neighbor_count <= 20:
            raise ValueError("top_k must be 1..50; neighbor_count must be 0..20")


@dataclass(frozen=True)
class SearchResult:
    """検索で得た根拠と実行工程。"""

    evidence: tuple[Evidence, ...]
    trace: tuple[TraceStep, ...] = ()


@dataclass(frozen=True)
class GenerationRequest:
    """根拠を明示した回答要求。検索を暗黙には行わない。"""

    question: str
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True)
class AnswerResult:
    """回答・引用・不足理由。表示用 HTML や HTTP URL は含めない。"""

    text: str
    citations: tuple[Evidence, ...] = ()
    confidence: str = ""
    insufficient_reason: str = ""
    needs_human_review: bool = False
    trace: tuple[TraceStep, ...] = ()


class StageError(RuntimeError):
    """外部処理が失敗した工程と中断までの trace を保持する。"""

    def __init__(self, stage: str, trace: tuple[TraceStep, ...]):
        super().__init__(f"RAG engine stage failed: {stage}")
        self.stage = stage
        self.trace = trace
