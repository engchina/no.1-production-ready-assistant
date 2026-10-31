"""解析からチャンク作成、embedding 保存までの Knowledge Base 登録処理。"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rag_engine.adapters.oracle.store import EmbeddingFunction, EmbeddingSaveResult, save_chunk_run_embeddings
from rag_engine.chunking import ChunkingConfig, ChunkingResult, create_chunk_run
from rag_engine.config import Settings
from rag_engine.knowledge.classification import DocumentClassification, classification_from_metadata
from rag_engine.parsing.analysis import analyze_pdf


@dataclass(frozen=True)
class KnowledgeRegistrationResult:
    """Knowledge Base 登録処理の各成果物と保存件数を保持します。"""

    source_path: str
    source_file_name: str
    run_id: str
    chunk_run_id: str
    classification: dict[str, Any]
    chunk_result: ChunkingResult
    embedding_result: EmbeddingSaveResult
    # Vision 説明に失敗した Picture/Table の件数。登録は継続するため ok には含めない。
    vision_failed_count: int = 0

    @property
    def ok(self) -> bool:
        """処理結果が成功扱いかどうかを返します。"""
        return self.embedding_result.ok


def register_source_file_for_rag(
    *,
    source_path: str | Path,
    engine_ids: Iterable[str],
    settings: Settings,
    classification: DocumentClassification | dict[str, Any] | None = None,
    document_metadata: dict[str, Any] | None = None,
    chunking_config: ChunkingConfig | None = None,
    min_confidence: float = 0.0,
    dpi: int | None = None,
    use_docling_vision: bool = True,
    embedder: EmbeddingFunction | None = None,
) -> KnowledgeRegistrationResult:
    """文書情報を引き継いで解析・チャンク作成・ADB embedding 保存を実行する。

    document_metadata は任意の文書ID・版・日付・URL。検証は解析開始前に行い、
    不正値は ValueError になる。解析/embedding の外部 API と ADB への書込みを伴う。
    解析に成功した engine が無い場合は、各 engine の失敗理由を含む RuntimeError。
    Vision 説明の個別失敗では中断せず、件数を結果の vision_failed_count に記録する。
    """
    source = Path(source_path)
    selected_engines = [str(engine) for engine in engine_ids if str(engine or "").strip()]
    document_classification = classification_from_metadata(classification)
    run = analyze_pdf(
        pdf_path=source,
        page_range="all",
        engine_ids=selected_engines,
        settings=settings,
        min_confidence=min_confidence,
        dpi=dpi,
        use_docling_vision=use_docling_vision,
        parse_entire_file=True,
        classification=document_classification,
        document_metadata=document_metadata,
    )
    # analyze_pdf は engine の例外を status に格納して正常終了する。ここで止めないと、
    # 後続のチャンク作成が「解析テキストがありません」で失敗し、元の原因が見えなくなる。
    if not any(status.available for status in run.statuses):
        details = "\n".join(f"{status.label}: {status.message}" for status in run.statuses)
        raise RuntimeError(f"すべての解析エンジンが失敗、または利用できません。\n{details}")
    vision_failed_count = sum(record.raw.get("vision_status") == "failed" for record in run.records)
    chunk_result = create_chunk_run(
        output_dir=settings.output_dir,
        run_id=run.run_id,
        preferred_engine_ids=selected_engines,
        config=chunking_config or ChunkingConfig(),
        classification=document_classification,
    )
    embedding_kwargs: dict[str, Any] = {}
    if embedder is not None:
        embedding_kwargs["embedder"] = embedder
    embedding_result = save_chunk_run_embeddings(
        chunk_result,
        settings,
        preferred_engine_ids=selected_engines,
        **embedding_kwargs,
    )
    return KnowledgeRegistrationResult(
        source_path=str(source),
        source_file_name=source.name,
        run_id=run.run_id,
        chunk_run_id=chunk_result.chunk_run_id,
        classification=document_classification.to_metadata(),
        chunk_result=chunk_result,
        embedding_result=embedding_result,
        vision_failed_count=vision_failed_count,
    )


def format_knowledge_registration_result(result: KnowledgeRegistrationResult) -> str:
    """Knowledge Base 登録結果を Markdown 表示へ整形します。"""
    status = "検索可能（ready）" if result.ok else "エラー（error）"
    classification = result.classification
    return "\n".join(
        [
            "### Knowledge Base 登録結果",
            f"- 状態（status）: {status}",
            f"- ファイル（file）: `{result.source_file_name}`",
            f"- 解析実行 ID（Run ID）: `{result.run_id}`",
            f"- チャンク実行 ID（Chunk Run ID）: `{result.chunk_run_id}`",
            (
                "- 分類: "
                f"大={classification.get('large_category', '')} / "
                f"中={classification.get('middle_category', '')} / "
                f"小={classification.get('small_category', '')} / "
                f"source={classification.get('source', '')}"
            ),
            f"- 子チャンク数（child chunks）: {result.chunk_result.child_count}",
            f"- 親チャンク数（parent chunks）: {result.chunk_result.parent_count}",
            f"- Vision 説明の失敗: {result.vision_failed_count} 件",
            f"- 作成・更新した embedding（created/updated）: {result.embedding_result.created_count}",
            f"- 再利用した embedding（skipped）: {result.embedding_result.skipped_count}",
            f"- エラー（errors）: {result.embedding_result.error_count}",
            *[f"- エラー詳細（error detail）: {error}" for error in result.embedding_result.errors],
        ]
    )
