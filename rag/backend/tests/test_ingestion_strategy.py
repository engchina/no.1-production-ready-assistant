"""source profile と取込 pipeline の抽出 strategy 接続テスト。"""

import json
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pr_system_settings.embedding import EMBEDDING_DIMENSIONS
from pypdf import PdfReader
from rag_parser_core.preprocess import ConvertOutcome
from rag_parser_core.result import ParserRegistryResult

from app.clients.oci_enterprise_ai import (
    EnterpriseAiIncompleteResponseError,
    OciEnterpriseAiClient,
)
from app.clients.oci_genai import OciGenAiClient
from app.config import Settings
from app.rag import ingestion as ingestion_module
from app.rag.audit import record_rag_ingestion_audit
from app.rag.chunking import Chunk
from app.rag.graph_index import GraphIndex
from app.rag.ingestion import (
    IngestionCancelledError,
    IngestionPipeline,
    IngestionUserError,
)
from app.rag.ingestion_quality import build_ingestion_quality_report
from app.schemas.document import (
    DocumentChunkView,
    DocumentDetail,
    FileStatus,
    IngestionSegment,
    SourceModality,
    SourceProfile,
)
from app.schemas.extraction import (
    DocumentElement,
    ExtractionAsset,
    ExtractionPage,
    ExtractionTable,
    ExtractionTableCell,
    StructuredExtraction,
)
from app.schemas.knowledge_base import KnowledgeBaseRef


class FakeOracle:
    """IngestionPipeline に必要な OracleClient subset。"""

    def __init__(self) -> None:
        self.saved_extraction: StructuredExtraction | None = None
        self.saved_chunk_count = 0
        self.atomic_index_save_count = 0
        self.saved_graph_index: GraphIndex | None = None
        self.graph_document_id: str | None = None
        self.statuses: list[FileStatus] = []
        self.error_messages: list[str | None] = []
        self.segments: dict[str, IngestionSegment] = {}
        self.extraction_artifacts: dict[str, dict[str, object]] = {}
        self.recipe_rows: dict[str, dict[str, object]] = {}
        # 実体の索引（既定 ON。#1388）の保存の transaction の数。SQL は実行しない。
        self.entity_transactions = 0

    async def _run_transaction(self, operation: object) -> None:
        _ = operation
        self.entity_transactions += 1

    async def update_document_status(
        self,
        document_id: str,
        status: FileStatus,
        error_message: str | None = None,
    ) -> DocumentDetail:
        self.statuses.append(status)
        self.error_messages.append(error_message)
        return DocumentDetail(
            id=document_id,
            file_name="layout.pdf",
            status=status,
            content_type="application/pdf",
            file_size_bytes=7,
            content_sha256="a" * 64,
            uploaded_at=datetime.now(UTC),
            indexed_at=datetime.now(UTC) if status == FileStatus.INDEXED else None,
            error_message=error_message,
        )

    async def save_extraction(
        self,
        document_id: str,
        extraction: StructuredExtraction,
    ) -> None:
        _ = document_id
        self.saved_extraction = extraction

    async def save_index(
        self,
        document_id: str,
        extraction: StructuredExtraction,
        chunks: list[Any],
        vectors: list[list[float]],
        chunk_set_id: str | None = None,
    ) -> None:
        _ = document_id, vectors, chunk_set_id
        self.atomic_index_save_count += 1
        self.saved_extraction = extraction
        self.saved_chunk_count = len(chunks)

    async def upsert_document_extraction_artifact(self, **kwargs: object) -> None:
        extraction_recipe_id = str(kwargs["extraction_recipe_id"])
        self.extraction_artifacts[extraction_recipe_id] = dict(kwargs)

    async def list_document_extraction_field_sets(self, document_id: str) -> list[object]:
        _ = document_id
        return []

    async def get_document_extraction_artifact(
        self,
        *,
        document_id: str,
        extraction_recipe_id: str,
    ) -> dict[str, object] | None:
        _ = document_id
        artifact = self.extraction_artifacts.get(extraction_recipe_id)
        if artifact is None:
            return None
        return {**artifact, "extraction_json": artifact.get("extraction")}

    async def get_document_recipe(
        self, document_id: str, recipe_id: str
    ) -> dict[str, object] | None:
        _ = document_id
        return self.recipe_rows.get(recipe_id)

    async def update_document_recipe_status(
        self,
        *,
        recipe_id: str,
        status: FileStatus,
        active_extraction_recipe_id: str | None = None,
        **_kwargs: object,
    ) -> None:
        row = self.recipe_rows.setdefault(recipe_id, {"recipe_id": recipe_id})
        row["status"] = status.value
        if active_extraction_recipe_id is not None:
            row["active_extraction_recipe_id"] = active_extraction_recipe_id

    async def list_document_knowledge_bases(
        self,
        document_id: str,
    ) -> list[KnowledgeBaseRef]:
        _ = document_id
        return [KnowledgeBaseRef(id="kb-1", name="社内規程")]

    async def get_document(self, document_id: str) -> Any:
        # 関係情報の文書の節点の名前に使うファイル名(#2051)。
        _ = document_id
        return SimpleNamespace(file_name="policy.pdf")

    async def replace_document_graph_index(
        self,
        document_id: str,
        graph_index: GraphIndex,
        *,
        chunk_set_id: str | None = None,
    ) -> None:
        _ = chunk_set_id
        self.graph_document_id = document_id
        self.saved_graph_index = graph_index

    async def replace_ingestion_segments(
        self,
        document_id: str,
        segments: list[IngestionSegment],
    ) -> list[IngestionSegment]:
        self.segments = {
            segment.segment_id: segment.model_copy(update={"document_id": document_id})
            for segment in segments
        }
        return list(self.segments.values())

    async def list_ingestion_segments(self, document_id: str) -> list[IngestionSegment]:
        return [segment for segment in self.segments.values() if segment.document_id == document_id]

    async def update_ingestion_segment(
        self,
        segment_id: str,
        **updates: object,
    ) -> IngestionSegment | None:
        segment = self.segments.get(segment_id)
        if segment is None:
            return None
        updated = segment.model_copy(
            update={key: value for key, value in updates.items() if value is not None}
        )
        self.segments[segment_id] = updated
        return updated


class CapturingVlm(OciEnterpriseAiClient):
    """VLM 呼び出し時の parser profile と prompt を記録する。"""

    parser_profile: str | None = None
    prompt = ""

    async def extract_with_vlm(
        self,
        image_bytes: bytes,
        prompt: str,
        *,
        mime_type: str = "application/octet-stream",
        parser_profile: str = "enterprise_ai_generic",
    ) -> dict[str, object]:
        _ = image_bytes, mime_type
        self.parser_profile = parser_profile
        self.prompt = prompt
        return {
            "raw_text": "PDF 規程本文です。\n| 項目 | 値 |\n| 承認 | 部門長 |",
            "document_type": "社内規程",
            "confidence": 0.92,
            "warnings": [],
            "elements": [
                {
                    "kind": "text",
                    "text": "PDF 規程本文です。",
                    "order": 1,
                    "page_number": 1,
                },
                {
                    "kind": "table",
                    "text": "| 項目 | 値 |\n| 承認 | 部門長 |",
                    "order": 2,
                    "page_number": 1,
                },
            ],
        }


class SegmentCapturingVlm(OciEnterpriseAiClient):
    """PDF segment ごとの VLM 呼び出しを記録する。"""

    def __init__(self, *, fail_multi_page_once: bool = False) -> None:
        self.fail_multi_page_once = fail_multi_page_once
        self.failed_once = False
        self.page_counts: list[int] = []
        self.prompts: list[str] = []

    async def extract_with_vlm(
        self,
        image_bytes: bytes,
        prompt: str,
        *,
        mime_type: str = "application/octet-stream",
        parser_profile: str = "enterprise_ai_generic",
    ) -> dict[str, object]:
        _ = mime_type, parser_profile
        reader = PdfReader(BytesIO(image_bytes))
        page_count = len(reader.pages)
        self.page_counts.append(page_count)
        self.prompts.append(prompt)
        if self.fail_multi_page_once and page_count > 1 and not self.failed_once:
            self.failed_once = True
            raise EnterpriseAiIncompleteResponseError("max_output_tokens")
        call_index = len(self.page_counts)
        return {
            "raw_text": f"segment {call_index} の本文",
            "document_type": "社内規程",
            "confidence": 0.9,
            "warnings": [],
            "elements": [
                {
                    "kind": "text",
                    "text": f"segment {call_index} の本文",
                    "order": 1,
                    "page_number": 1,
                }
            ],
        }


class SegmentFailingVlm(OciEnterpriseAiClient):
    """指定回数目の PDF segment 抽出で失敗する VLM fake。"""

    def __init__(self, *, fail_on_call: int) -> None:
        self.fail_on_call = fail_on_call
        self.page_counts: list[int] = []

    async def extract_with_vlm(
        self,
        image_bytes: bytes,
        prompt: str,
        *,
        mime_type: str = "application/octet-stream",
        parser_profile: str = "enterprise_ai_generic",
    ) -> dict[str, object]:
        _ = prompt, mime_type, parser_profile
        reader = PdfReader(BytesIO(image_bytes))
        page_count = len(reader.pages)
        self.page_counts.append(page_count)
        call_index = len(self.page_counts)
        if call_index == self.fail_on_call:
            raise RuntimeError("segment failure")
        return {
            "raw_text": f"segment {call_index} の本文",
            "document_type": "社内規程",
            "confidence": 0.9,
            "warnings": [],
            "elements": [
                {
                    "kind": "text",
                    "text": f"segment {call_index} の本文",
                    "order": 1,
                    "page_number": 1,
                }
            ],
        }


class InvalidStructuredExtractionVlm(OciEnterpriseAiClient):
    """schema 不整合の VLM 応答を返す fake。"""

    async def extract_with_vlm(
        self,
        image_bytes: bytes,
        prompt: str,
        *,
        mime_type: str = "application/octet-stream",
        parser_profile: str = "enterprise_ai_generic",
    ) -> dict[str, object]:
        _ = image_bytes, prompt, mime_type, parser_profile
        return {
            "raw_text": "schema 不整合の本文",
            "document_type": "社内規程",
            "confidence": 1.4,
            "warnings": [],
        }


class FailingIfCalledVlm(OciEnterpriseAiClient):
    """再実行時に cached extraction が使われることを検証する VLM fake。"""

    async def extract_with_vlm(
        self,
        image_bytes: bytes,
        prompt: str,
        *,
        mime_type: str = "application/octet-stream",
        parser_profile: str = "enterprise_ai_generic",
    ) -> dict[str, object]:
        _ = image_bytes, prompt, mime_type, parser_profile
        raise AssertionError("cached extraction artifact があれば VLM を再実行しない")


class FakeEmbeddingClient(OciGenAiClient):
    """チャンク数分の 1024 次元ベクトルを返す。"""

    async def embed(
        self,
        texts: list[str],
        *,
        input_type: str = "SEARCH_DOCUMENT",
    ) -> list[list[float]]:
        _ = input_type
        return [[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1) for _ in texts]


class FailingEmbeddingClient(OciGenAiClient):
    """抽出 artifact cache 後の後段失敗を作る embedding fake。"""

    async def embed(
        self,
        texts: list[str],
        *,
        input_type: str = "SEARCH_DOCUMENT",
    ) -> list[list[float]]:
        _ = texts, input_type
        raise RuntimeError("embedding failure")


class FakeObjectStorage:
    """抽出 artifact cache 用の Object Storage fake。"""

    def __init__(self) -> None:
        self.puts: list[tuple[str, bytes, str]] = []
        self.gets: list[str] = []
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        self.puts.append((key, data, content_type))
        path = f"oci://namespace/bucket/{key}"
        self.objects[path] = data
        return path

    async def get(self, path: str) -> bytes:
        self.gets.append(path)
        return self.objects[path]

    def seed_extraction(self, path: str, extraction: StructuredExtraction) -> None:
        self.objects[path] = extraction.model_dump_json().encode("utf-8")


class FailingObjectStorage(FakeObjectStorage):
    """full extraction artifact cache 失敗を作る Object Storage fake。"""

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        self.puts.append((key, data, content_type))
        raise RuntimeError("object storage unavailable")


def _pdf_source_profile(*, file_size_bytes: int) -> SourceProfile:
    """PDF source profile のテスト fixture。"""
    return SourceProfile(
        original_file_name="layout.pdf",
        sanitized_file_name="layout.pdf",
        extension=".pdf",
        content_type="application/pdf",
        inferred_content_type="application/pdf",
        file_size_bytes=file_size_bytes,
        content_sha256="a" * 64,
        modality=SourceModality.PDF,
        parser_profile="enterprise_ai_pdf_layout",
    )


def _canned_extraction() -> StructuredExtraction:
    """parser マイクロサービスが返す抽出の代替(pipeline テスト用)。"""
    return StructuredExtraction.model_validate(
        {
            "raw_text": "PDF 規程本文です。\n| 項目 | 値 |\n| 承認 | 部門長 |",
            "document_type": "社内規程",
            "confidence": 0.92,
            "warnings": [],
            "elements": [
                {
                    "kind": "text",
                    "text": "PDF 規程本文です。",
                    "order": 1,
                    "page_number": 1,
                },
                {
                    "kind": "table",
                    "text": "| 項目 | 値 |\n| 承認 | 部門長 |",
                    "order": 2,
                    "page_number": 1,
                },
            ],
        }
    )


@pytest.fixture(autouse=True)
def _stub_parser_service(monkeypatch: pytest.MonkeyPatch) -> None:
    """in-process 解析は撤去済み。pipeline テストは parser 委譲を canned 抽出で代替する。

    `IngestionPipeline._partition_source` は選択 backend を `ParserServiceClient.runner` へ
    HTTP 委譲する。本 fixture はその runner を決定論スタブへ差し替え、実サービス未起動でも
    pipeline(chunk/embedding/index/graph)を検証できるようにする。
    """

    def _runner(
        self: object,
        backend: str,
        source_bytes: bytes,
        source_profile: SourceProfile | None,
        content_type: str,
        *,
        fail_fast: bool = False,
    ) -> ParserRegistryResult:
        _ = self, source_bytes, source_profile, content_type, fail_fast
        return ParserRegistryResult(
            extraction=_canned_extraction(),
            parser_backend=backend,
            parser_version=f"{backend}_test",
            template="text_blocks",
        )

    monkeypatch.setattr(
        "app.clients.parser_service.ParserServiceClient.runner",
        _runner,
        raising=True,
    )


def test_ingestion_trace_structure_attributes_follow_quality_report_counts() -> None:
    """chunking trace は quality_report と同じ構造件数を記録する。"""
    extraction = StructuredExtraction(
        raw_text="本文\n表\n図",
        confidence=0.92,
        elements=[
            DocumentElement(kind="text", text="本文", page_number=1, confidence=0.6),
        ],
        pages=[
            ExtractionPage(page_number=1, element_ids=["el-0000"]),
            ExtractionPage(page_number=2),
            ExtractionPage(page_number=3, element_ids=["tbl-main"]),
            ExtractionPage(page_number=4, element_ids=["fig-1"]),
        ],
        tables=[
            ExtractionTable(
                table_id="tbl-main",
                element_id="tbl-main",
                page_number=3,
                cells=[ExtractionTableCell(row=0, col=0, text="金額", confidence=0.4)],
            )
        ],
        assets=[ExtractionAsset(asset_id="fig-1", kind="image", page_number=4, alt_text="構成図")],
        parser_artifacts={
            "page_count": 5,
            "table_count": 2,
            "equation_count": 2,
            "asset_count": 3,
            "low_confidence_count": 4,
            "failed_segment_count": 1,
        },
    )
    quality = build_ingestion_quality_report(extraction)
    extraction = extraction.model_copy(update={"quality_report": quality})

    attributes = ingestion_module._extraction_structure_attributes(extraction)

    assert attributes == {
        "element_count": 1,
        "table_count": 2,
        "figure_count": 3,
        "formula_count": 2,
        "page_count": 5,
        "asset_count": 3,
        "low_confidence_count": 4,
        "failed_segment_count": 1,
    }


def test_raw_vlm_trace_structure_attributes_read_first_class_payload_fields() -> None:
    """VLM trace は raw payload の pages/tables/assets/artifacts も件数化する。"""
    attributes = ingestion_module._raw_extraction_structure_attributes(
        {
            "elements": [
                {
                    "kind": "text",
                    "text": "本文",
                    "page_number": 1,
                }
            ],
            "pages": [
                {"page_number": 1, "element_ids": ["el-1"]},
                {"page_number": 2, "element_ids": []},
                {"page_number": 3, "element_ids": ["tbl-main"]},
                {"page_number": 4, "element_ids": ["fig-1"]},
            ],
            "tables": [{"table_id": "tbl-main", "page_number": 3}],
            "assets": [{"asset_id": "fig-1", "kind": "image", "page_number": 4}],
            "parser_artifacts": {
                "page_count": "5",
                "adapter_table_count": 2,
                "equation_count": 2,
                "asset_count": 3,
            },
        }
    )

    assert attributes == {
        "element_count": 1,
        "table_count": 2,
        "figure_count": 3,
        "formula_count": 2,
        "page_count": 5,
        "asset_count": 3,
    }


async def test_recipe_extraction_artifacts_are_isolated_and_pointer_switches_after_write() -> None:
    """同じ設定でも recipe ごとに別 artifact を保存してから pointer を切り替える。"""
    oracle = FakeOracle()
    source_profile = _pdf_source_profile(file_size_bytes=7)
    extraction = _canned_extraction()
    settings = Settings(rag_parser_adapter_backend="local")

    first = IngestionPipeline(
        vlm=cast(Any, object()),
        genai=cast(Any, object()),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, FakeObjectStorage()),
        document_understanding=cast(Any, object()),
        settings=settings,
        recipe_id="recipe-1",
        recipe_revision=2,
    )
    second = IngestionPipeline(
        vlm=cast(Any, object()),
        genai=cast(Any, object()),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, FakeObjectStorage()),
        document_understanding=cast(Any, object()),
        settings=settings,
        recipe_id="recipe-2",
        recipe_revision=2,
    )

    first_id = await first._persist_extraction_layer("doc-1", source_profile, extraction)
    second_id = await second._persist_extraction_layer("doc-1", source_profile, extraction)

    assert first_id is not None and second_id is not None
    assert first_id != second_id
    assert set(oracle.extraction_artifacts) == {first_id, second_id}
    assert oracle.recipe_rows["recipe-1"]["active_extraction_recipe_id"] == first_id
    assert oracle.recipe_rows["recipe-2"]["active_extraction_recipe_id"] == second_id


async def test_recipe_extraction_persistence_failure_is_fatal() -> None:
    """recipe artifact を保存できなければ legacy の best-effort と違って失敗させる。"""

    class FailingRecipeOracle(FakeOracle):
        async def upsert_document_extraction_artifact(self, **kwargs: object) -> None:
            _ = kwargs
            raise RuntimeError("write failed")

    pipeline = IngestionPipeline(
        vlm=cast(Any, object()),
        genai=cast(Any, object()),
        oracle=cast(Any, FailingRecipeOracle()),
        object_storage=cast(Any, FakeObjectStorage()),
        document_understanding=cast(Any, object()),
        settings=Settings(rag_parser_adapter_backend="local"),
        recipe_id="recipe-1",
        recipe_revision=1,
    )

    with pytest.raises(RuntimeError, match="write failed"):
        await pipeline._persist_extraction_layer(
            "doc-1", _pdf_source_profile(file_size_bytes=7), _canned_extraction()
        )


async def test_recipe_review_load_does_not_fall_back_to_document_extraction() -> None:
    """recipe pointer が無い場合は他 recipe 相当の legacy 本文を読まない。"""
    oracle = FakeOracle()
    oracle.recipe_rows["recipe-empty"] = {
        "recipe_id": "recipe-empty",
        "active_extraction_recipe_id": None,
    }
    pipeline = IngestionPipeline(
        vlm=cast(Any, object()),
        genai=cast(Any, object()),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, FakeObjectStorage()),
        document_understanding=cast(Any, object()),
        settings=Settings(rag_parser_adapter_backend="local"),
        recipe_id="recipe-empty",
        recipe_revision=1,
    )
    detail = DocumentDetail(
        id="doc-1",
        file_name="layout.pdf",
        status=FileStatus.REVIEW,
        uploaded_at=datetime.now(UTC),
        content_sha256="a" * 64,
        extraction={"raw_text": "別レシピの本文"},
    )

    with pytest.raises(IngestionUserError, match="抽出結果が見つかりません"):
        await pipeline._load_reviewed_extraction(detail)


async def test_ingestion_pipeline_caches_extraction_artifact_and_segment_checkpoint() -> None:
    """抽出 artifact を保存し、segment checkpoint に artifact path を残す。"""
    oracle = FakeOracle()
    storage = FakeObjectStorage()
    pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, storage),
        settings=Settings(
            # 項目抽出は対象外(stub の VLM は応答しないため、有効なら取込が止まる。#2481)。
            rag_field_extraction_enabled=False,
            rag_parser_adapter_backend="local",
            rag_review_gate_enabled=False,
            rag_auto_parse_after_preprocess_enabled=True,
        ),
    )

    detail = await pipeline.ingest(
        "doc-artifact",
        b"pdfdata",
        "本文を抽出してください。",
        content_type="application/pdf",
        source_profile=_pdf_source_profile(file_size_bytes=7),
    )

    assert detail.status == FileStatus.INDEXED
    assert storage.puts
    key, data, content_type = storage.puts[0]
    assert key.startswith("artifacts/extractions/doc-artifact/")
    assert content_type == "application/json"
    assert b"raw_text" in data
    payload = json.loads(data)
    parser_artifacts = payload["parser_artifacts"]
    trace_key = key.rsplit("/", 1)[-1].removesuffix(".json")
    assert parser_artifacts["extraction_artifact_schema_version"] == 1
    assert parser_artifacts["extraction_artifact_kind"] == "full"
    assert parser_artifacts["extraction_artifact_document_id"] == "doc-artifact"
    assert parser_artifacts["extraction_artifact_trace_id"] == trace_key
    assert oracle.saved_extraction is not None
    artifact_path = oracle.saved_extraction.parser_artifacts["extraction_artifact_path"]
    assert isinstance(artifact_path, str)
    assert artifact_path.startswith("oci://namespace/bucket/artifacts/extractions/doc-artifact/")
    assert oracle.saved_extraction.parser_artifacts["extraction_artifact_schema_version"] == 1
    assert oracle.saved_extraction.parser_artifacts["extraction_artifact_kind"] == "full"
    assert oracle.segments
    assert all(segment.status == "SUCCEEDED" for segment in oracle.segments.values())
    assert all(segment.artifact_path == artifact_path for segment in oracle.segments.values())


async def test_low_confidence_excel_header_stops_at_review_even_when_gate_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """表頭の推定の信頼度が低いシートがあれば、全体のレビューの設定が off でも確認で止める。

    #1229。
    """
    from app.rag import ingestion as ingestion_module
    from app.rag.ingestion_quality import build_ingestion_quality_report, requires_review

    def with_low_confidence_header(extraction: Any, **kwargs: Any) -> Any:
        # parser のサービスが返す抽出の warning（sheet_records_extraction）の代わり。
        extraction = extraction.model_copy(
            update={"warnings": [*extraction.warnings, "excel_header_low_confidence:費目"]}
        )
        return build_ingestion_quality_report(extraction, **kwargs)

    monkeypatch.setattr(
        ingestion_module, "build_ingestion_quality_report", with_low_confidence_header
    )
    oracle = FakeOracle()
    pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, FakeObjectStorage()),
        settings=Settings(
            # 項目抽出は対象外(stub の VLM は応答しないため、有効なら取込が止まる。#2481)。
            rag_field_extraction_enabled=False,
            rag_parser_adapter_backend="local",
            rag_review_gate_enabled=False,
            rag_auto_parse_after_preprocess_enabled=True,
        ),
    )

    detail = await pipeline.ingest(
        "doc-low-header",
        b"pdfdata",
        "本文を抽出してください。",
        content_type="application/pdf",
        source_profile=_pdf_source_profile(file_size_bytes=7),
    )

    assert detail.status == FileStatus.REVIEW
    assert oracle.saved_extraction is not None
    report = oracle.saved_extraction.quality_report
    assert report is not None
    assert "excel_header_low_confidence:費目" in report.quality_warnings
    assert report.risk_level == "medium"
    assert requires_review(report) is True
    other = report.model_copy(update={"quality_warnings": ["long_document"]})
    assert requires_review(other) is False
    assert requires_review(None) is False


async def test_ingestion_pipeline_stops_at_preprocessed_when_parse_gate_off() -> None:
    """ファイル準備ゲート off では PREPROCESSED で停止し、抽出/索引へ進めない。"""
    oracle = FakeOracle()
    storage = FakeObjectStorage()
    pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, storage),
        settings=Settings(
            rag_parser_adapter_backend="local",
            rag_auto_parse_after_preprocess_enabled=False,
        ),
    )

    detail = await pipeline.ingest(
        "doc-gate",
        b"pdfdata",
        "本文を抽出してください。",
        content_type="application/pdf",
        source_profile=_pdf_source_profile(file_size_bytes=7),
    )

    assert detail.status == FileStatus.PREPROCESSED
    assert oracle.statuses[-1] == FileStatus.PREPROCESSED
    # parse 以降は走らない: 抽出も chunk も保存されない。
    assert oracle.saved_extraction is None
    assert oracle.saved_chunk_count == 0


async def test_ingestion_pipeline_sanitizes_extraction_artifact_keys() -> None:
    """artifact cache key は prefix/document id を安全な Object Storage path に正規化する。"""
    oracle = FakeOracle()
    storage = FakeObjectStorage()
    pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, storage),
        settings=Settings(
            # 項目抽出は対象外(stub の VLM は応答しないため、有効なら取込が止まる。#2481)。
            rag_field_extraction_enabled=False,
            rag_parser_adapter_backend="local",
            rag_extraction_artifact_prefix=("../unsafe//prefix with space\\nested/./../final"),
            rag_auto_parse_after_preprocess_enabled=True,
        ),
    )

    await pipeline.ingest(
        "doc/with/slash:artifact?",
        b"pdfdata",
        "本文を抽出してください。",
        content_type="application/pdf",
        source_profile=_pdf_source_profile(file_size_bytes=7),
    )

    assert storage.puts
    key, _data, _content_type = storage.puts[0]
    assert key.startswith("unsafe/prefix_with_space/nested/final/doc_with_slash_artifact_/")
    assert "\\" not in key
    assert " " not in key
    assert "//" not in key
    assert ".." not in key.split("/")
    assert oracle.saved_extraction is not None
    artifact_path = oracle.saved_extraction.parser_artifacts["extraction_artifact_path"]
    assert isinstance(artifact_path, str)
    assert "/doc_with_slash_artifact_/" in artifact_path


async def test_ingestion_pipeline_reports_extraction_artifact_cache_failure() -> None:
    """artifact cache 失敗は保存 payload と品質レポートへ警告として残す。"""
    oracle = FakeOracle()
    storage = FailingObjectStorage()
    pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, storage),
        settings=Settings(
            # 項目抽出は対象外(stub の VLM は応答しないため、有効なら取込が止まる。#2481)。
            rag_field_extraction_enabled=False,
            rag_parser_adapter_backend="local",
            rag_review_gate_enabled=False,
            rag_auto_parse_after_preprocess_enabled=True,
        ),
    )

    detail = await pipeline.ingest(
        "doc-artifact-cache-failure",
        b"pdfdata",
        "本文を抽出してください。",
        content_type="application/pdf",
        source_profile=_pdf_source_profile(file_size_bytes=7),
    )

    assert detail.status == FileStatus.INDEXED
    assert storage.puts
    assert oracle.saved_extraction is not None
    assert oracle.saved_extraction.parser_artifacts["extraction_artifact_cache_failed"] is True
    assert "extraction_artifact_cache_failed" in oracle.saved_extraction.warnings
    assert oracle.saved_extraction.quality_report is not None
    assert (
        "extraction_artifact_cache_failed"
        in oracle.saved_extraction.quality_report.quality_warnings
    )
    assert oracle.saved_extraction.quality_report.risk_level == "medium"
    assert all(segment.artifact_path is None for segment in oracle.segments.values())


class _StubConvertingPreprocess:
    """converting プロファイルで必ず converted=True を返す前処理スタブ(サービス不要)。"""

    def convert(
        self,
        source_bytes: bytes,
        *,
        content_type: str,
        source_profile: Any = None,
        profile: Any = None,
        options: Any = None,
    ) -> ConvertOutcome:
        _ = source_bytes, content_type, source_profile, profile, options
        return ConvertOutcome(
            converted=True,
            converter_name="stub",
            converter_version="v1",
            derived_bytes=b"%PDF-1.7 derived",
            derived_content_type="application/pdf",
            page_map={"1": 1},
        )


async def test_preprocess_canonical_persist_failure_surfaces_as_error() -> None:
    """変換成功でも処理後ファイル保存に失敗したら silent な PREPROCESSED を作らず ERROR にする。"""
    oracle = FakeOracle()
    storage = FailingObjectStorage()
    pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, storage),
        settings=Settings(
            rag_parser_adapter_backend="local",
            rag_preprocess_enabled=True,
            rag_preprocess_profile="office_to_pdf",
        ),
    )
    pipeline._preprocess = cast(Any, _StubConvertingPreprocess())

    with pytest.raises(IngestionUserError, match="処理後ファイルを保存できませんでした"):
        await pipeline.ingest(
            "doc-canonical-fail",
            b"pdfdata",
            "本文を抽出してください。",
            content_type="application/pdf",
            source_profile=_pdf_source_profile(file_size_bytes=7),
        )

    assert oracle.statuses[-1] == FileStatus.ERROR
    assert oracle.error_messages[-1] is not None
    assert "処理後ファイルを保存できませんでした" in oracle.error_messages[-1]
    # 処理後ファイルが無い壊れた PREPROCESSED 成功状態を作らない。
    assert FileStatus.PREPROCESSED not in oracle.statuses


async def test_canonical_artifact_persists_even_when_extraction_cache_disabled() -> None:
    """canonical(処理後ファイル)保存は抽出 JSON キャッシュフラグでは無効化されない(必須)。"""
    oracle = FakeOracle()
    storage = FakeObjectStorage()
    pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, storage),
        settings=Settings(
            rag_parser_adapter_backend="local",
            rag_extraction_artifact_cache_enabled=False,
        ),
    )

    path = await pipeline._cache_canonical_artifact(
        document_id="doc-canonical",
        trace_id="trace-1",
        derived_bytes=b"%PDF-1.7 derived",
        content_type="application/pdf",
    )

    assert path is not None
    assert storage.puts
    assert "/canonical.pdf" in storage.puts[0][0]


class _NonPersistingOracle:
    """save_preprocess_artifact を受けるが永続化しない(読み戻すと artifact なし)スタブ。"""

    async def save_preprocess_artifact(self, document_id: str, artifact: Any) -> None:
        _ = document_id, artifact

    async def get_document(self, document_id: str) -> DocumentDetail:
        return DocumentDetail(
            id=document_id,
            file_name="x.pdf",
            status=FileStatus.PREPROCESSING,
            content_type="application/pdf",
            file_size_bytes=1,
            content_sha256="a" * 64,
            uploaded_at=datetime.now(UTC),
            indexed_at=None,
            preprocess_artifact=None,
        )


async def test_preprocess_artifact_save_not_persisted_surfaces_as_error() -> None:
    """ファイル準備 artifact の保存が永続化しなければ silent にせず ERROR 用例外を出す。"""
    from rag_parser_core.preprocess import SourceDerivation

    pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, _NonPersistingOracle()),
        settings=Settings(rag_parser_adapter_backend="local"),
    )
    derivation = SourceDerivation(
        derivation_id="d1",
        preprocess_profile="office_to_pdf",
        converted=True,
        derived_object_path="local://artifacts/canonical/doc-x/t/canonical.pdf",
        derived_content_type="application/pdf",
        source_content_type="application/pdf",
        source_sha256="a" * 64,
        derived_sha256="b" * 64,
    )
    with pytest.raises(IngestionUserError, match="処理後ファイル情報を保存できませんでした"):
        await pipeline._save_preprocess_artifact(
            document_id="doc-x",
            source_derivation=derivation,
            original_file_name="x.pdf",
            original_object_storage_path="local://uploaded/x.pdf",
            fallback_content_type="application/pdf",
        )


async def test_ingestion_pipeline_reuses_full_extraction_artifact_after_embedding_failure() -> None:
    """VLM 成功後の後段失敗は full extraction artifact から復旧し VLM を再実行しない。"""
    oracle = FakeOracle()
    storage = FakeObjectStorage()
    first_pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FailingEmbeddingClient(),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, storage),
        settings=Settings(
            # 項目抽出は対象外(stub の VLM は応答しないため、有効なら取込が止まる。#2481)。
            rag_field_extraction_enabled=False,
            rag_parser_adapter_backend="local",
            rag_review_gate_enabled=False,
            rag_auto_parse_after_preprocess_enabled=True,
        ),
    )

    with pytest.raises(RuntimeError, match="embedding failure"):
        await first_pipeline.ingest(
            "doc-full-artifact-retry",
            b"pdfdata",
            "本文を抽出してください。",
            content_type="application/pdf",
            source_profile=_pdf_source_profile(file_size_bytes=7),
        )

    assert oracle.statuses == [
        FileStatus.PREPROCESSING,
        FileStatus.INGESTING,
        FileStatus.ERROR,
    ]
    assert oracle.saved_extraction is None
    assert oracle.saved_chunk_count == 0
    assert len(storage.puts) == 1
    [source_segment] = list(oracle.segments.values())
    assert source_segment.status == "SUCCEEDED"
    assert source_segment.artifact_path is not None

    second_pipeline = IngestionPipeline(
        vlm=FailingIfCalledVlm(),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, storage),
        settings=Settings(
            # 項目抽出は対象外(stub の VLM は応答しないため、有効なら取込が止まる。#2481)。
            rag_field_extraction_enabled=False,
            rag_parser_adapter_backend="local",
            rag_review_gate_enabled=False,
            rag_auto_parse_after_preprocess_enabled=True,
        ),
    )

    detail = await second_pipeline.ingest(
        "doc-full-artifact-retry",
        b"pdfdata",
        "本文を抽出してください。",
        content_type="application/pdf",
        source_profile=_pdf_source_profile(file_size_bytes=7),
    )

    assert detail.status == FileStatus.INDEXED
    assert storage.gets == [source_segment.artifact_path]
    assert len(storage.puts) == 2
    assert oracle.saved_extraction is not None
    assert oracle.saved_extraction.parser_artifacts["extraction_artifact_reused"] is True
    assert "PDF 規程本文です。" in oracle.saved_extraction.raw_text
    assert oracle.saved_chunk_count > 0


async def test_ingestion_pipeline_does_not_reuse_full_extraction_for_other_parse_input() -> None:
    """前回の取込と解析に渡す内容が違えば(ファイル準備の出力の変化など)、保存した解析の結果を
    使わずに解析し直す(#2076)。"""
    oracle = FakeOracle()
    storage = FakeObjectStorage()
    settings = Settings(
        # 項目抽出は対象外(stub の VLM は応答しないため、有効なら取込が止まる。#2481)。
        rag_field_extraction_enabled=False,
        rag_parser_adapter_backend="local",
        rag_review_gate_enabled=False,
        rag_auto_parse_after_preprocess_enabled=True,
    )
    first_pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FailingEmbeddingClient(),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, storage),
        settings=settings,
    )
    with pytest.raises(RuntimeError, match="embedding failure"):
        await first_pipeline.ingest(
            "doc-full-artifact-changed",
            b"pdfdata",
            "本文を抽出してください。",
            content_type="application/pdf",
            source_profile=_pdf_source_profile(file_size_bytes=7),
        )
    [source_segment] = list(oracle.segments.values())
    assert source_segment.status == "SUCCEEDED"
    assert source_segment.artifact_path is not None

    second_pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, storage),
        settings=settings,
    )
    detail = await second_pipeline.ingest(
        "doc-full-artifact-changed",
        b"pdfdata-changed",
        "本文を抽出してください。",
        content_type="application/pdf",
        source_profile=_pdf_source_profile(file_size_bytes=15),
    )

    assert detail.status == FileStatus.INDEXED
    # 前回の artifact の結果を使わず、解析し直した(新しい artifact は今回の入力の鍵を持つ)。
    assert oracle.saved_extraction is not None
    artifacts = oracle.saved_extraction.parser_artifacts
    assert "extraction_artifact_reused" not in artifacts
    assert artifacts["extraction_artifact_path"] != source_segment.artifact_path
    first_key = json.loads(storage.objects[source_segment.artifact_path])["parser_artifacts"][
        "extraction_artifact_parse_key"
    ]
    assert artifacts["extraction_artifact_parse_key"] != first_key


@pytest.mark.usefixtures("mineru_selectable")
def test_ingestion_selected_external_parser_fallback_stops_ingestion() -> None:
    """明示選択 parser が fallback 結果なら Enterprise AI へ進めず止める。"""
    pipeline = object.__new__(IngestionPipeline)
    pipeline._settings = Settings(
        # 項目抽出は対象外(stub の VLM は応答しないため、有効なら取込が止まる。#2481)。
        rag_field_extraction_enabled=False,
        rag_parser_adapter_backend="mineru",
        rag_parser_mineru_enabled=True,
    )
    result = ParserRegistryResult(
        extraction=None,
        parser_backend="mineru",
        parser_version="service_unavailable",
        fallback_used=True,
        warnings=("mineru_adapter_failed",),
    )

    with pytest.raises(IngestionUserError, match="MinerU"):
        pipeline._raise_if_selected_parser_was_not_used(result)


async def test_ingestion_pipeline_cancel_after_extraction_does_not_save_index() -> None:
    """cancel 済み job は extraction/chunk/index 保存と INDEXED 遷移を止める。"""
    oracle = FakeOracle()
    pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, oracle),
        settings=Settings(
            rag_parser_adapter_backend="local",
            rag_review_gate_enabled=False,
            rag_auto_parse_after_preprocess_enabled=True,
        ),
    )
    checks = 0

    async def cancel_after_extraction() -> bool:
        # 開始・ファイル準備後・INGESTING の前後・segment 準備後の順に確かめる(#305)。
        # 5 回目 = 解析の直前で取り消す。
        nonlocal checks
        checks += 1
        return checks >= 5

    with pytest.raises(IngestionCancelledError):
        await pipeline.ingest(
            "doc-cancel",
            b"pdfdata",
            "本文を抽出してください。",
            content_type="application/pdf",
            source_profile=_pdf_source_profile(file_size_bytes=7),
            cancel_checker=cancel_after_extraction,
        )

    assert oracle.statuses == [FileStatus.PREPROCESSING, FileStatus.INGESTING]
    assert oracle.atomic_index_save_count == 0
    assert oracle.saved_extraction is None
    assert oracle.saved_chunk_count == 0


async def test_ingestion_pipeline_cancel_during_preprocess_does_not_write_later_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ファイル準備中に cancel されたら、PREPROCESSED を書かずに取り消しで止める(#305)。"""
    oracle = FakeOracle()
    pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, oracle),
        settings=Settings(
            rag_parser_adapter_backend="local",
            rag_review_gate_enabled=False,
            rag_auto_parse_after_preprocess_enabled=False,
        ),
    )
    cancelled = False
    original_preprocess = pipeline._preprocess_source

    async def preprocess_then_cancel(**kwargs: Any) -> Any:
        nonlocal cancelled
        result = await original_preprocess(**kwargs)
        cancelled = True
        return result

    async def cancel_checker() -> bool:
        return cancelled

    monkeypatch.setattr(pipeline, "_preprocess_source", preprocess_then_cancel)

    with pytest.raises(IngestionCancelledError):
        await pipeline.ingest(
            "doc-cancel-preprocess",
            b"pdfdata",
            "本文を抽出してください。",
            content_type="application/pdf",
            source_profile=_pdf_source_profile(file_size_bytes=7),
            cancel_checker=cancel_checker,
        )

    assert oracle.statuses == [FileStatus.PREPROCESSING]


@pytest.mark.parametrize("recipe_id", [None, "recipe-2"])
async def test_ingestion_pipeline_cancel_before_review_does_not_write_review(
    monkeypatch: pytest.MonkeyPatch,
    recipe_id: str | None,
) -> None:
    """REVIEW を書く直前に cancel されたら、REVIEW と抽出の切り替えを書かない(#305)。"""
    oracle = FakeOracle()
    pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, FakeObjectStorage()),
        settings=Settings(
            # 項目抽出は対象外(stub の VLM は応答しないため、有効なら取込が止まる。#2481)。
            rag_field_extraction_enabled=False,
            rag_parser_adapter_backend="local",
            rag_review_gate_enabled=True,
            rag_auto_parse_after_preprocess_enabled=True,
        ),
        recipe_id=recipe_id,
        recipe_revision=1 if recipe_id is not None else None,
    )
    cancelled = False
    original_attach = pipeline._attach_navigation_tree

    async def attach_then_cancel(*args: Any, **kwargs: Any) -> Any:
        nonlocal cancelled
        result = await original_attach(*args, **kwargs)
        cancelled = True
        return result

    async def cancel_checker() -> bool:
        return cancelled

    monkeypatch.setattr(pipeline, "_attach_navigation_tree", attach_then_cancel)

    with pytest.raises(IngestionCancelledError):
        await pipeline.ingest(
            "doc-cancel-review",
            b"pdfdata",
            "本文を抽出してください。",
            content_type="application/pdf",
            source_profile=_pdf_source_profile(file_size_bytes=7),
            cancel_checker=cancel_checker,
        )

    assert FileStatus.REVIEW not in oracle.statuses
    assert oracle.saved_extraction is None
    if recipe_id is not None:
        row = oracle.recipe_rows.get(recipe_id, {})
        assert row.get("status") != FileStatus.REVIEW.value
        assert "active_extraction_recipe_id" not in row


async def test_ingestion_pipeline_writes_graph_index_when_enabled() -> None:
    """関係情報を構築する設定(entities)のときは、取込結果から関係情報を保存する。"""
    oracle = FakeOracle()
    settings = Settings.model_construct(
        # 項目抽出は対象外(stub の VLM は応答しないため、有効なら取込が止まる。#2481)。
        rag_field_extraction_enabled=False,
        rag_parser_adapter_backend="local",
        rag_review_gate_enabled=False,
        rag_auto_parse_after_preprocess_enabled=True,
        rag_graph_profile="entities",
        rag_chunk_size=800,
        rag_chunk_overlap=120,
        oci_genai_embedding_model="cohere.embed-v4.0",
        oci_enterprise_ai_models=[],
        oci_enterprise_ai_default_text_model="",
        oci_enterprise_ai_default_vision_model="enterprise-vlm",
    )
    pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, FakeObjectStorage()),
        settings=settings,
    )

    detail = await pipeline.ingest(
        "doc-graph",
        b"pdfdata",
        "本文を抽出してください。",
        content_type="application/pdf",
        source_profile=_pdf_source_profile(file_size_bytes=7),
    )

    assert detail.status == FileStatus.INDEXED
    assert oracle.graph_document_id == "doc-graph"
    assert oracle.saved_graph_index is not None
    assert {entity.knowledge_base_id for entity in oracle.saved_graph_index.entities} == {"kb-1"}
    assert oracle.saved_graph_index.relationships
    assert oracle.saved_graph_index.entity_chunk_links
    # 文書の節点はファイル名で区別できる(#2051)。
    assert "文書全体: policy.pdf" in {
        entity.canonical_name for entity in oracle.saved_graph_index.entities
    }


def test_checkpoint_parser_profile_prefers_external_adapter_lineage() -> None:
    """外部 parser 成功時の checkpoint 表示は Enterprise AI profile へ戻さない。"""
    extraction = StructuredExtraction.model_validate(
        {
            "raw_text": "Unstructured が抽出した本文",
            "document_type": "reference",
            "confidence": 0.9,
            "parser_artifacts": {
                "source_parser": "unstructured_adapter",
                "external_adapter": "unstructured",
            },
        }
    )
    result = ParserRegistryResult(
        extraction=extraction,
        parser_backend="unstructured",
        parser_version="unstructured:1.0.0",
    )

    assert (
        ingestion_module._checkpoint_parser_profile(
            result,
            fallback="enterprise_ai_pdf_layout",
        )
        == "unstructured_adapter"
    )


def _office_source_profile() -> SourceProfile:
    """Office(docx)source profile のテスト fixture。"""
    return SourceProfile(
        original_file_name="report.docx",
        sanitized_file_name="report.docx",
        extension=".docx",
        content_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        file_size_bytes=10,
        content_sha256="d" * 64,
        modality=SourceModality.OFFICE,
        parser_profile="local_office_structure",
    )


def _preflight_pipeline(backend: str, monkeypatch: pytest.MonkeyPatch) -> IngestionPipeline:
    """pre-flight テスト用の pipeline。runner 呼び出しは失敗扱いにする。"""

    def _forbidden_runner(*args: object, **kwargs: object) -> ParserRegistryResult:
        raise AssertionError("pre-flight で停止すべきなので runner は呼ばれない")

    monkeypatch.setattr(
        "app.clients.parser_service.ParserServiceClient.runner",
        _forbidden_runner,
        raising=True,
    )
    return IngestionPipeline(
        vlm=cast(Any, object()),
        genai=cast(Any, object()),
        oracle=cast(Any, FakeOracle()),
        object_storage=cast(Any, FakeObjectStorage()),
        document_understanding=cast(Any, object()),
        settings=Settings().model_copy(update={"rag_parser_adapter_backend": backend}),
    )


def test_partition_source_preflight_blocks_unsupported_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dots_ocr × Office は外部 API を呼ぶ前に対応形式入りエラーで停止する。"""
    from app.clients.parser_service import ParserServiceUnavailableError

    pipeline = _preflight_pipeline("dots_ocr", monkeypatch)

    with pytest.raises(ParserServiceUnavailableError) as exc_info:
        pipeline._partition_source(
            parse_bytes=b"docx-bytes",
            source_profile=_office_source_profile(),
            content_type=_office_source_profile().content_type,
        )

    assert exc_info.value.reason == "adapter_source_unsupported"
    assert exc_info.value.warning_code == "dots_ocr_adapter_source_unsupported"
    assert "対応形式: PDF・画像" in str(exc_info.value)


@pytest.mark.parametrize("content_kind", ["pdf", "office"], ids=["pdf", "office"])
@pytest.mark.parametrize("backend", ["oci_document_understanding", "mineru"])
def test_partition_source_blocks_roadmap_engine_before_any_call(
    monkeypatch: pytest.MonkeyPatch, content_kind: str, backend: str
) -> None:
    """準備中(ロードマップ)のエンジンは、対応形式でも呼ばずに止める。

    OCI Document Understanding(#3374)と MinerU(#3359)。
    """
    from app.clients.parser_service import ParserServiceUnavailableError

    pipeline = _preflight_pipeline(backend, monkeypatch)
    profile = (
        _pdf_source_profile(file_size_bytes=10)
        if content_kind == "pdf"
        else _office_source_profile()
    )

    with pytest.raises(ParserServiceUnavailableError) as exc_info:
        pipeline._partition_source(
            parse_bytes=b"source-bytes",
            source_profile=profile,
            content_type=profile.content_type,
        )

    assert exc_info.value.reason == "engine_roadmap"
    assert exc_info.value.warning_code == "parser_engine_roadmap"
    assert "準備中（ロードマップ）" in str(exc_info.value)


def test_partition_source_blocks_historical_removed_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """履歴レシピの削除済み engine は旧結果を使うだけにし、新規処理を開始しない。"""
    from app.clients.parser_service import ParserServiceUnavailableError

    pipeline = _preflight_pipeline("removed_engine", monkeypatch)

    with pytest.raises(ParserServiceUnavailableError) as exc_info:
        pipeline._partition_source(
            parse_bytes=b"docx-bytes",
            source_profile=_office_source_profile(),
            content_type=_office_source_profile().content_type,
        )

    assert exc_info.value.reason == "engine_removed"
    assert "解析エンジン（removed_engine）は削除されました" in str(exc_info.value)
    assert "旧結果は保持されています" in str(exc_info.value)


def test_partition_source_preflight_passes_converted_pdf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """office_to_pdf 変換後(content_type=application/pdf)は dots_ocr でも通過する。"""
    pipeline = _preflight_pipeline("dots_ocr", monkeypatch)

    def _runner(
        self: object,
        backend: str,
        source_bytes: bytes,
        source_profile: SourceProfile | None,
        content_type: str,
        *,
        fail_fast: bool = False,
    ) -> ParserRegistryResult:
        return ParserRegistryResult(
            extraction=_canned_extraction(),
            parser_backend=backend,
            parser_version="dots_ocr_test",
        )

    monkeypatch.setattr(
        "app.clients.parser_service.ParserServiceClient.runner",
        _runner,
        raising=True,
    )

    result = pipeline._partition_source(
        parse_bytes=b"%PDF",
        source_profile=_office_source_profile(),
        content_type="application/pdf",
    )

    assert result.parser_backend == "dots_ocr"
    assert result.extraction is not None


@pytest.mark.parametrize("configured", ["local", "local_partition", ""])
def test_partition_source_maps_legacy_or_empty_backend_to_default_docling(
    configured: str,
) -> None:
    """廃止済み local・空の backend は既定エンジン Docling(#286)のサービスへ委譲する。"""
    pipeline = IngestionPipeline(
        vlm=cast(Any, object()),
        genai=cast(Any, object()),
        oracle=cast(Any, FakeOracle()),
        object_storage=cast(Any, FakeObjectStorage()),
        document_understanding=cast(Any, object()),
        # validator を通さず旧値・空値を直接入れる(保存済み snapshot 相当)。
        settings=Settings().model_copy(update={"rag_parser_adapter_backend": configured}),
    )

    result = pipeline._partition_source(
        parse_bytes=b"%PDF",
        source_profile=_pdf_source_profile(file_size_bytes=4),
        content_type="application/pdf",
    )

    # autouse の _stub_parser_service は受け取った backend をそのまま parser_backend に返す。
    assert result.parser_backend == "docling"
    assert result.extraction is not None


def test_embedding_input_with_context_header_prepends_title_and_section() -> None:
    """Contextual chunk header は「文書名 > section_path」を embedding 入力へ前置する。"""
    chunk = Chunk(
        text="本文です。",
        index=0,
        start_offset=0,
        end_offset=5,
        metadata={"section_path": "第1章 > 第2節"},
    )
    assert (
        ingestion_module._embedding_input_with_context_header(chunk, "製品マニュアル.pdf")
        == "製品マニュアル.pdf > 第1章 > 第2節\n本文です。"
    )


def test_embedding_input_with_context_header_falls_back_to_raw_text() -> None:
    """文書名も section_path もない chunk はヘッダなしで本文のみを返す。"""
    chunk = Chunk(text="本文です。", index=0, start_offset=0, end_offset=5, metadata={})
    assert ingestion_module._embedding_input_with_context_header(chunk, "") == "本文です。"


def test_embedding_input_with_context_header_uses_title_only_without_section() -> None:
    """section_path がない場合は文書名だけをヘッダにする。"""
    chunk = Chunk(text="本文です。", index=0, start_offset=0, end_offset=5, metadata={})
    assert (
        ingestion_module._embedding_input_with_context_header(chunk, "manual.pdf")
        == "manual.pdf\n本文です。"
    )


def test_chunk_embedding_inputs_skip_context_header_when_disabled() -> None:
    """無効化時は保存済み metadata にヘッダがあっても本文だけを embedding する。"""
    pipeline = IngestionPipeline(
        vlm=cast(Any, object()),
        genai=cast(Any, object()),
        oracle=cast(Any, object()),
        object_storage=cast(Any, object()),
        settings=Settings(rag_chunk_context_header_enabled=False),
    )
    chunk = Chunk(
        text="本文です。",
        index=0,
        start_offset=0,
        end_offset=5,
        metadata={"context_header": "manual.pdf > 第1章"},
    )

    assert pipeline._chunk_embedding_inputs([chunk]) == ["本文です。"]


class _ResumeOracle(FakeOracle):
    """保存済み Chunk(CHUNKED)から索引だけを行うレシピの工程用の fake。"""

    def __init__(self) -> None:
        super().__init__()
        self.updated_chunks: list[Chunk] = []
        self.recipe_rows["recipe-1"] = {"recipe_id": "recipe-1"}

    async def get_document(self, document_id: str) -> DocumentDetail | None:
        return DocumentDetail(
            id=document_id,
            file_name="製品マニュアル.pdf",
            status=FileStatus.CHUNKED,
            uploaded_at=datetime.now(UTC),
            content_sha256="a" * 64,
            file_size_bytes=2048,
        )

    async def list_chunk_set_chunks(self, chunk_set_id: str) -> list[DocumentChunkView]:
        _ = chunk_set_id
        return [
            DocumentChunkView(
                document_id="doc-1",
                chunk_id="chunk-1",
                chunk_index=0,
                text="本文です。",
                section_path="第1章 > 概要",
                element_ids=["e1"],
                metadata={"section_path": "第1章 > 概要"},
            )
        ]

    async def update_chunk_set_embeddings(
        self,
        *,
        chunk_set_id: str,
        chunks: list[Chunk],
        embeddings: list[list[float]],
    ) -> None:
        _ = chunk_set_id, embeddings
        self.updated_chunks = chunks


class _ResumePipeline(IngestionPipeline):
    async def _load_reviewed_extraction(self, detail: DocumentDetail) -> StructuredExtraction:
        _ = detail
        return StructuredExtraction(raw_text="本文です。")


async def test_index_chunked_rebuilds_context_header_without_document_lookup() -> None:
    """CHUNKED 再開時も既取得の detail と chunk metadata だけで検索入力を再構築する。"""

    class CapturingEmbeddingClient(FakeEmbeddingClient):
        def __init__(self) -> None:
            self.texts: list[str] = []

        async def embed(
            self,
            texts: list[str],
            *,
            input_type: str = "SEARCH_DOCUMENT",
        ) -> list[list[float]]:
            self.texts = texts
            return await super().embed(texts, input_type=input_type)

    oracle = _ResumeOracle()
    embedding = CapturingEmbeddingClient()
    pipeline = _ResumePipeline(
        vlm=cast(Any, object()),
        genai=embedding,
        oracle=cast(Any, oracle),
        object_storage=cast(Any, FakeObjectStorage()),
        settings=Settings(
            rag_chunk_context_header_enabled=True,
            rag_entity_index_enabled=True,
            rag_graph_profile="off",
        ),
        recipe_id="recipe-1",
        recipe_revision=1,
    )

    await pipeline.index_chunked("doc-1", chunk_set_id="chunk-set-1", record_outcome=False)

    expected = "製品マニュアル.pdf > 第1章 > 概要"
    assert embedding.texts == [f"{expected}\n本文です。"]
    assert oracle.updated_chunks[0].metadata["context_header"] == expected
    # 索引の構築を明示した場合、保存済み Chunk の索引の後にも 1 回保存する。
    assert oracle.entity_transactions == 1


async def test_recipe_index_chunked_records_single_success_audit_with_document_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """レシピの索引工程は成功の監査を 1 回だけ出し、原本は文書の hash / サイズで残す(#493)。

    保存済み Chunk の索引は原本 bytes を読まないため、空 bytes の hash ではなく、文書に
    記録済みの原本の hash とサイズを使う。本文(抽出テキスト)は監査に含めない。
    """
    events: list[dict[str, Any]] = []
    metrics: list[tuple[str, int]] = []

    def spy_audit(**kwargs: Any) -> object:
        event = record_rag_ingestion_audit(**kwargs)
        events.append(event.model_dump(mode="json"))
        return event

    monkeypatch.setattr(ingestion_module, "record_rag_ingestion_audit", spy_audit)
    monkeypatch.setattr(
        ingestion_module,
        "record_ingestion",
        lambda outcome, chunk_count: metrics.append((outcome, chunk_count)),
    )
    pipeline = _ResumePipeline(
        vlm=cast(Any, object()),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, _ResumeOracle()),
        object_storage=cast(Any, FakeObjectStorage()),
        settings=Settings(rag_graph_profile="off"),
        recipe_id="recipe-1",
        recipe_revision=1,
    )

    await pipeline.index_chunked("doc-1", chunk_set_id="chunk-set-1", record_outcome=True)

    assert [event["outcome"] for event in events] == ["success"]
    assert events[0]["source_sha256"] == "a" * 64
    assert events[0]["source_bytes"] == 2048
    assert events[0]["chunk_count"] == 1
    assert events[0]["vector_count"] == 1
    assert "本文です。" not in json.dumps(events[0], ensure_ascii=False)
    assert metrics == [("success", 1)]


def _spy_ingestion_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, Any]], list[tuple[str, int]]]:
    """取込の監査イベントと metric を記録する spy を差し込む。"""
    events: list[dict[str, Any]] = []
    metrics: list[tuple[str, int]] = []

    def spy_audit(**kwargs: Any) -> object:
        event = record_rag_ingestion_audit(**kwargs)
        events.append(event.model_dump(mode="json"))
        return event

    monkeypatch.setattr(ingestion_module, "record_rag_ingestion_audit", spy_audit)
    monkeypatch.setattr(
        ingestion_module,
        "record_ingestion",
        lambda outcome, chunk_count: metrics.append((outcome, chunk_count)),
    )
    return events, metrics


def _resume_pipeline(
    oracle: _ResumeOracle, pipeline_class: type[_ResumePipeline] = _ResumePipeline
) -> _ResumePipeline:
    return pipeline_class(
        vlm=cast(Any, object()),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, FakeObjectStorage()),
        settings=Settings(rag_graph_profile="off"),
        recipe_id="recipe-1",
        recipe_revision=1,
    )


async def test_index_chunked_defers_success_outcome_until_caller_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``record_outcome=False`` の索引は成功を記録せず、内容を呼び出し側へ残す(#504)。

    レシピの job は active への切り替えの後にこれを 1 回だけ記録する。
    """
    events, metrics = _spy_ingestion_outcomes(monkeypatch)
    pipeline = _resume_pipeline(_ResumeOracle())

    await pipeline.index_chunked("doc-1", chunk_set_id="chunk-set-1", record_outcome=False)

    assert events == []
    assert metrics == []
    outcome = pipeline.deferred_success_outcome
    assert outcome is not None

    outcome.record()

    assert [event["outcome"] for event in events] == ["success"]
    assert events[0]["source_sha256"] == "a" * 64
    assert events[0]["source_bytes"] == 2048
    assert events[0]["chunk_count"] == 1
    assert events[0]["vector_count"] == 1
    assert "本文です。" not in json.dumps(events[0], ensure_ascii=False)
    assert metrics == [("success", 1)]


async def test_index_chunked_failure_audit_uses_document_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保存済み Chunk の索引の失敗の監査も、文書に記録済みの原本の hash とサイズを使う(#504)。"""

    class EmptyChunkOracle(_ResumeOracle):
        async def list_chunk_set_chunks(self, chunk_set_id: str) -> list[DocumentChunkView]:
            _ = chunk_set_id
            return []

    events, metrics = _spy_ingestion_outcomes(monkeypatch)
    pipeline = _resume_pipeline(EmptyChunkOracle())

    with pytest.raises(IngestionUserError, match="索引対象のチャンク"):
        await pipeline.index_chunked("doc-1", chunk_set_id="chunk-set-1", record_outcome=False)

    assert [event["outcome"] for event in events] == ["error"]
    assert events[0]["source_sha256"] == "a" * 64
    assert events[0]["source_bytes"] == 2048
    assert metrics == [("error", 0)]
    assert pipeline.deferred_success_outcome is None


async def test_chunk_reviewed_failure_audit_uses_document_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chunk 作成の失敗の監査も、空 bytes ではなく文書の原本の hash とサイズを使う(#504)。"""

    class FailingChunkPipeline(_ResumePipeline):
        async def _build_chunks_for_extraction(self, **kwargs: Any) -> list[Chunk]:
            _ = kwargs
            raise IngestionUserError("分割に失敗しました。")

    events, metrics = _spy_ingestion_outcomes(monkeypatch)
    pipeline = _resume_pipeline(_ResumeOracle(), FailingChunkPipeline)

    with pytest.raises(IngestionUserError, match="分割に失敗"):
        await pipeline.chunk_reviewed("doc-1", chunk_set_id="chunk-set-1", record_outcome=False)

    assert [event["outcome"] for event in events] == ["error"]
    assert events[0]["source_sha256"] == "a" * 64
    assert events[0]["source_bytes"] == 2048
    assert metrics == [("error", 0)]


async def test_ingest_audit_uses_given_document_source_instead_of_input_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """レシピの job が渡した原本の hash とサイズを、ingest の監査が使う(#504)。

    処理後ファイル(原本と別の bytes)を読む工程でも、監査は文書の原本を指す。
    """
    events, _metrics = _spy_ingestion_outcomes(monkeypatch)
    pipeline = _resume_pipeline(_ResumeOracle())

    async def cancelled() -> bool:
        return True

    with pytest.raises(IngestionCancelledError):
        await pipeline.ingest(
            "doc-1",
            b"prepared bytes",
            "prompt",
            record_outcome=False,
            manage_document_state=False,
            cancel_checker=cancelled,
            source_sha256="b" * 64,
            source_size=4096,
        )

    assert [event["outcome"] for event in events] == ["error"]
    assert events[0]["source_sha256"] == "b" * 64
    assert events[0]["source_bytes"] == 4096


async def test_pipeline_releases_heavy_slot_after_parse_before_llm_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重い段の枠を返すのは、解析・図表の読み取りの後、見出しの推定・項目抽出の前に 1 回(#2702)。"""
    from app.rag.ingestion_slots import heavy_slot_releaser

    pipeline = IngestionPipeline(
        vlm=CapturingVlm(),
        genai=FakeEmbeddingClient(),
        oracle=cast(Any, FakeOracle()),
        object_storage=cast(Any, FakeObjectStorage()),
        settings=Settings(
            rag_field_extraction_enabled=False,
            rag_parser_adapter_backend="local",
            rag_review_gate_enabled=False,
            rag_auto_parse_after_preprocess_enabled=True,
        ),
    )
    events: list[str] = []

    def record(name: str, method: Any) -> Any:
        async def wrapped(*args: Any, **kwargs: Any) -> Any:
            events.append(name)
            return await method(*args, **kwargs)

        return wrapped

    for name in ("_attach_vision", "_attach_heading_levels", "_attach_extraction_fields"):
        monkeypatch.setattr(pipeline, name, record(name, getattr(pipeline, name)))

    with heavy_slot_releaser(lambda: events.append("release")):
        detail = await pipeline.ingest(
            "doc-heavy-slot",
            b"pdfdata",
            "本文を抽出してください。",
            content_type="application/pdf",
            source_profile=_pdf_source_profile(file_size_bytes=7),
        )

    assert detail.status == FileStatus.INDEXED
    assert events == [
        "_attach_vision",
        "release",
        "_attach_heading_levels",
        "_attach_extraction_fields",
    ]


@pytest.mark.parametrize("enabled", [True, False])
def test_small_to_big_search_input_respects_context_header(enabled: bool) -> None:
    """専用の検索本文を保ち、文脈ヘッダの追加だけを切り替える。"""
    from app.rag.chunking_small_to_big import engine_search_text

    pipeline = IngestionPipeline(
        vlm=cast(Any, object()),
        genai=cast(Any, object()),
        oracle=cast(Any, object()),
        object_storage=cast(Any, object()),
        settings=Settings(rag_chunk_context_header_enabled=enabled),
    )
    item = Chunk(
        text="本文",
        index=0,
        start_offset=0,
        end_offset=2,
        metadata={"engine_search_text": "検索用の文脈と本文", "section_path": "節 A"},
    )
    contextualized = ingestion_module._chunks_with_context_headers(
        [item],
        document_title="文書 A",
        enabled=enabled,
    )
    expected = "文書 A > 節 A\n検索用の文脈と本文" if enabled else "検索用の文脈と本文"
    assert pipeline._chunk_embedding_inputs(contextualized) == [expected]
    assert engine_search_text(contextualized[0].metadata) == expected
    assert contextualized[0].text == "本文"
    # OFF は保存済みのヘッダも embedding に追加しない。
    if not enabled:
        item.metadata["context_header"] = "古いヘッダ"
        assert pipeline._chunk_embedding_inputs([item]) == ["検索用の文脈と本文"]
