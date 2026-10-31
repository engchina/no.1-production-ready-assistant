"""既存 Docling / Oracle / OCI 実装を SDK の機能契約へ接続する。"""

import json
from collections.abc import Sequence
from dataclasses import asdict, replace

from rag_engine.config import Settings
from rag_engine.models.contracts import (
    AnswerResult,
    Evidence,
    GenerationRequest,
    IndexRequest,
    IndexResult,
    ParsedDocument,
    ParseRequest,
    SearchRequest,
    SearchResult,
    TraceStep,
)
from rag_engine.ports import Embedder
from rag_engine.resources.runtime import Runtime


class DocumentParser:
    """既存解析器を明示的な保存先・profile・モデルプールで実行する。"""

    def __init__(self, settings: Settings, runtime: Runtime, *, engines: tuple[str, ...] = ("docling",)):
        self.settings, self.runtime, self.engines = settings, runtime, engines

    def parse(self, request: ParseRequest) -> ParsedDocument:
        """解析成果物を保存し、HTTP URL を除いた構造化文書を返す。"""
        try:
            from rag_engine.parsing.analysis import analyze_pdf
        except ImportError as exc:
            raise RuntimeError("Install rag-engine[pdf,docling]") from exc
        with self.runtime.activate():
            result = analyze_pdf(
                request.source,
                request.page_range,
                list(self.engines),
                self.settings,
                use_docling_vision=request.vision,
                parse_entire_file=request.entire_file,
                document_metadata=dict(request.metadata),
            )
        if not result.records:
            raise RuntimeError("Parser produced no layout records; inspect the analysis status")
        return ParsedDocument(
            result.run_id,
            result.pdf_name,
            tuple(result.records),
            tuple(replace(page, image_url="") for page in result.pages),
            result.run_id,
            result.source_page_count,
            result.document_metadata,
            result.classification,
        )


class OciEmbedder:
    """既存 OCI embedding のモデル・次元・retry 設定を利用する。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    def embed(self, texts: Sequence[str], *, query: bool = False) -> list[list[float]]:
        """入力順の embedding を返す。認証・通信エラーを伝播する。"""
        from rag_engine.adapters.oci import embed_texts

        return embed_texts(texts, self.settings, input_type="SEARCH_QUERY" if query else "SEARCH_DOCUMENT")


def _require_text_only(settings: Settings, embedder: Embedder | None) -> None:
    """image vector は常に OCI で作るため、別空間になる custom embedder との併用を拒否する。"""
    if embedder is not None and settings.image_embedding_enabled:
        raise ValueError("A custom Embedder cannot be combined with IMAGE_EMBEDDING_ENABLED")


class OracleIndexer:
    """既存 ADB knowledge_base へ保存する。新しい collection schema は導入しない。

    custom embedder は OracleRetriever にも同じものを渡すこと。保存行は
    settings.embedding_model で識別するため、モデルを替えるときはこの値も変える。
    """

    def __init__(self, settings: Settings, runtime: Runtime, *, embedder: Embedder | None = None):
        _require_text_only(settings, embedder)
        self.settings, self.runtime = settings, runtime
        self.embedder = embedder or OciEmbedder(settings)

    def index(self, request: IndexRequest) -> IndexResult:
        """親子チャンク manifest と ADB 索引を保存する。失敗結果は例外に変換する。"""
        if request.collection != "knowledge_base":
            raise ValueError("OracleIndexer supports collection='knowledge_base'; use index_id to search a chunk run")
        from rag_engine.adapters.oracle.store import save_chunk_run_embeddings
        from rag_engine.chunking.storage import persist_batch

        with self.runtime.activate():
            result = persist_batch(request.batch, output_dir=self.settings.output_dir)
            saved = save_chunk_run_embeddings(
                result,
                self.settings,
                embedder=lambda texts, settings, **kwargs: self.embedder.embed(texts),
            )
        if saved.error_count:
            raise RuntimeError("; ".join(saved.errors))
        return IndexResult(request.collection, saved.created_count, result.chunk_run_id)


class OracleRetriever:
    """ADB hybrid search と既存の父子・近傍展開を再利用する。

    embedder 指定時は query も同じ embedder（query=True）で vector 化する。索引と別の
    embedder を使うと vector 空間が一致せず、エラーなしに無意味な順位になる。
    """

    def __init__(self, settings: Settings, runtime: Runtime, *, embedder: Embedder | None = None):
        _require_text_only(settings, embedder)
        self.settings, self.runtime, self.embedder = settings, runtime, embedder

    def retrieve(self, request: SearchRequest) -> SearchResult:
        """knowledge_base または IndexResult.index_id を検索する。fallback は行わない。"""
        from rag_engine.adapters.oracle.store import check_adb_hybrid_search_ready
        from rag_engine.generation.answering import build_adb_hybrid_answer_context
        from rag_engine.knowledge.classification import REQUEST_FILTER_KEYS, classification_filter_from_values

        request.validate()
        unknown = set(request.filters) - REQUEST_FILTER_KEYS
        if unknown:
            raise ValueError(f"Unsupported Oracle filters: {sorted(unknown)}")
        scope = "knowledge_base" if request.collection == "knowledge_base" else "current_chunk_run"
        chunk_run_id = "" if scope == "knowledge_base" else request.collection
        with self.runtime.activate():
            filters = classification_filter_from_values(**dict(request.filters))
            embedder = self.embedder
            query_embedder = None if embedder is None else (lambda query, settings: embedder.embed([query], query=True)[0])
            check_adb_hybrid_search_ready(
                chunk_run_id=chunk_run_id, settings=self.settings, retrieval_scope=scope, classification_filter=filters
            )
            context = build_adb_hybrid_answer_context(
                request.question,
                "",
                (),
                self.settings,
                top_k=request.top_k,
                neighbor_child_count=request.neighbor_count,
                retrieval_queries=request.queries or None,
                retrieval_scope=scope,
                pinned_chunk_run_id=chunk_run_id,
                classification_filter=filters,
                rerank_enabled=self.settings.default_rerank_enabled,
                query_embedder=query_embedder,
            )
        return SearchResult(
            tuple(
                Evidence(
                    record.id,
                    record.text,
                    record.source,
                    record.page,
                    {"record": asdict(record)},
                )
                for record in context.records
            )
        )


class OciGenerator:
    """既存の文書 grounding・画像根拠・回答出力契約を使用する。"""

    def __init__(self, settings: Settings, runtime: Runtime):
        self.settings, self.runtime = settings, runtime

    def generate(self, request: GenerationRequest) -> AnswerResult:
        """根拠の全文をバッチごとにモデルへ渡し、所属証拠付きの回答を返す。

        空根拠または単一根拠の予算超過時はモデルを呼ばず不足を返す。
        初回OCI呼出の失敗は呼出元へ送出する。不足後の画像補完だけが失敗した場合は
        元の部分回答と失敗traceを返す。画像非対応モデルからはChicagoへ切り替える。
        """
        from rag_engine.generation import answering as answer
        from rag_engine.generation.service import GenerationService
        from rag_engine.retrieval.context_builder import ContextParentEvidence, context_bundle_from_parent_evidence

        if not request.evidence:
            return GenerationService(self).generate(request)
        records = []
        for item in request.evidence:
            raw = item.metadata.get("record")
            if raw:
                records.append(answer.AnswerRecord(**raw))
            else:
                records.append(
                    answer.AnswerRecord(
                        id=item.id,
                        engine="external",
                        engine_label="external",
                        page=item.page,
                        seq_no=len(records) + 1,
                        category="Text",
                        text=item.text,
                        source=item.source,
                        metadata=dict(item.metadata),
                    )
                )
        with self.runtime.activate():
            # 親本文が回答予算を超える根拠は受け付けない。prompt の抜粋は synthesize_grounded_answer 側で行う。
            bundle = context_bundle_from_parent_evidence(
                tuple(ContextParentEvidence(record, "synthesis_parent", "sdk_evidence") for record in records),
                max_chars=answer.MAX_CONTEXT_CHARS,
            )
            if bundle.status == "insufficient" or len(bundle.records) != len(records):
                reason = bundle.insufficient_reason or "Evidence exceeds synthesis budget; re-run chunking with a smaller parent size."
                return AnswerResult("", confidence="low", insufficient_reason=reason, needs_human_review=True)
            context = answer.AnswerContext(
                records=bundle.records,
                text=bundle.text,
                evidence=bundle.evidence,
                evidence_tree=bundle.evidence_tree,
            )
            images = answer.answer_image_evidence(records, self.settings.output_dir)
            mode = answer._image_prompt_mode(images, self.settings)
            response = answer.synthesize_grounded_answer(
                request.question,
                context,
                self.settings,
                image_evidence=images,
                image_prompt_mode=mode,
                answer_llm_provider=self.settings.default_answer_llm,
            ).response
        from rag_engine.generation.citations import resolve_citations

        citations = resolve_citations(request.evidence, records, response.cited_evidence)
        decision = response.generation_trace.get("image_fallback", {})
        # SDKでも画像添付による是正の有無を確認可能にし、元回答本文は公開traceへ重複保存しない。
        trace = (
            (
                TraceStep(
                    "image_fallback",
                    detail=json.dumps(
                        {key: decision.get(key) for key in ("reason", "source_provider", "regeneration_calls")}, ensure_ascii=False
                    ),
                ),
            )
            if decision
            else ()
        )
        return AnswerResult(
            response.answer_text, citations, response.confidence, response.insufficient_reason, bool(response.needs_human_review), trace
        )
