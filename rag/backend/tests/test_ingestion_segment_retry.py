"""segment checkpoint / artifact cache の pipeline 契約テスト。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from app.clients.oci_enterprise_ai import OciEnterpriseAiClient
from app.clients.oci_genai import OciGenAiClient
from app.config import Settings
from app.rag import ingestion as ingestion_module
from app.rag.ingestion import IngestionPipeline
from app.schemas.document import (
    DocumentDetail,
    FileStatus,
    IngestionSegment,
    SourceModality,
    SourcePreviewKind,
    SourceProfile,
)
from app.schemas.extraction import DocumentElement, StructuredExtraction


async def test_cached_segment_artifact_identity_mismatch_is_cache_miss() -> None:
    """別 segment の artifact は checkpoint reuse せず、該当 range を再処理へ戻す。"""
    document_id = "doc-segment-identity"
    cached_path = "local://artifacts/extractions/doc-segment-identity/p1-2.json"
    storage = _FakeObjectStorage(
        {
            cached_path: _extraction_payload(
                "wrong segment cached text",
                page_number=1,
                element_id="wrong-e1",
                source_parser="enterprise_ai_pdf_layout",
                parser_artifacts={
                    "extraction_artifact_schema_version": (
                        ingestion_module.EXTRACTION_ARTIFACT_SCHEMA_VERSION
                    ),
                    "extraction_artifact_kind": "segment",
                    "extraction_artifact_document_id": document_id,
                    "extraction_artifact_trace_id": "trace-previous",
                    "extraction_artifact_segment_id": f"{document_id}:p9-10",
                    "extraction_artifact_page_start": 9,
                    "extraction_artifact_page_end": 10,
                },
            )
        }
    )
    pipeline = IngestionPipeline(
        object_storage=storage,  # type: ignore[arg-type]
        settings=Settings.model_construct(),
    )
    segment = IngestionSegment(
        segment_id=f"{document_id}:p1-2",
        document_id=document_id,
        status="SUCCEEDED",
        parser_backend="enterprise_ai",
        parser_profile="enterprise_ai_pdf_layout",
        page_start=1,
        page_end=2,
        artifact_path=cached_path,
    )

    result = await pipeline._load_cached_segment_extractions(
        [segment],
        target_segments=[],
        available_ranges={(1, 2)},
    )

    assert result.cached == []
    assert result.missing_ranges == {(1, 2)}


async def test_cached_full_artifact_document_mismatch_is_not_reused() -> None:
    """別 document の full artifact は全 segment 成功済みでも再利用しない。"""
    document_id = "doc-full-identity"
    cached_path = "local://artifacts/extractions/doc-full-identity/full.json"
    storage = _FakeObjectStorage(
        {
            cached_path: _extraction_payload(
                "wrong document full text",
                page_number=1,
                element_id="wrong-doc-e1",
                source_parser="enterprise_ai_pdf_layout",
                parser_artifacts={
                    "extraction_artifact_schema_version": (
                        ingestion_module.EXTRACTION_ARTIFACT_SCHEMA_VERSION
                    ),
                    "extraction_artifact_kind": "full",
                    "extraction_artifact_document_id": "another-document",
                    "extraction_artifact_trace_id": "trace-previous",
                },
            )
        }
    )
    pipeline = IngestionPipeline(
        object_storage=storage,  # type: ignore[arg-type]
        settings=Settings.model_construct(),
    )
    segment = IngestionSegment(
        segment_id=f"{document_id}:source",
        document_id=document_id,
        status="SUCCEEDED",
        parser_backend="enterprise_ai",
        parser_profile="enterprise_ai_pdf_layout",
        artifact_path=cached_path,
    )

    assert await pipeline._load_cached_full_extraction([segment], parse_key="key") is None


def _full_artifact_case(
    parse_key: str | None,
) -> tuple[IngestionPipeline, IngestionSegment]:
    document_id = "doc-full-parse-key"
    cached_path = "local://artifacts/extractions/doc-full-parse-key/full.json"
    artifacts: dict[str, Any] = {
        "extraction_artifact_schema_version": ingestion_module.EXTRACTION_ARTIFACT_SCHEMA_VERSION,
        "extraction_artifact_kind": "full",
        "extraction_artifact_document_id": document_id,
        "extraction_artifact_trace_id": "trace-previous",
    }
    if parse_key is not None:
        artifacts["extraction_artifact_parse_key"] = parse_key
    storage = _FakeObjectStorage(
        {
            cached_path: _extraction_payload(
                "previous parse text",
                page_number=1,
                element_id="previous-e1",
                source_parser="unstructured",
                parser_artifacts=artifacts,
            )
        }
    )
    pipeline = IngestionPipeline(
        object_storage=storage,  # type: ignore[arg-type]
        settings=Settings.model_construct(),
    )
    segment = IngestionSegment(
        segment_id=f"{document_id}:source",
        document_id=document_id,
        status="SUCCEEDED",
        parser_backend="unstructured",
        parser_profile="text_blocks",
        artifact_path=cached_path,
    )
    return pipeline, segment


async def test_cached_full_artifact_with_same_parse_key_is_reused() -> None:
    """同じ入力・同じ設定の再試行では、保存した解析の結果を再利用する。"""
    pipeline, segment = _full_artifact_case("key-a")

    cached = await pipeline._load_cached_full_extraction([segment], parse_key="key-a")

    assert cached is not None
    assert cached.parser_artifacts["extraction_artifact_reused"] is True


async def test_cached_full_artifact_with_other_parse_key_is_not_reused() -> None:
    """ファイル準備の出力や解析の設定が変わったら、前回の解析の結果を使わない(#2076)。"""
    pipeline, segment = _full_artifact_case("key-a")

    assert await pipeline._load_cached_full_extraction([segment], parse_key="key-b") is None


async def test_cached_full_artifact_without_parse_key_is_not_reused() -> None:
    """鍵を持たない(#2076 の前の)artifact は、入力が同じか分からないため再利用しない。"""
    pipeline, segment = _full_artifact_case(None)

    assert await pipeline._load_cached_full_extraction([segment], parse_key="key-a") is None


class _RetrySegmentVlm(OciEnterpriseAiClient):
    """失敗 segment だけが呼ばれたことを記録する VLM fake。"""

    def __init__(self) -> None:
        self.calls: list[bytes] = []

    async def extract_with_vlm(
        self,
        image_bytes: bytes,
        prompt: str,
        *,
        mime_type: str = "application/octet-stream",
        parser_profile: str = "enterprise_ai_generic",
    ) -> dict[str, object]:
        _ = prompt, mime_type, parser_profile
        self.calls.append(image_bytes)
        assert image_bytes == b"segment-3-4"
        return _extraction_payload(
            "page 3-4 retried text",
            page_number=1,
            element_id="retried-e1",
            source_parser="enterprise_ai_pdf_layout",
        )


class _EmbeddingClient(OciGenAiClient):
    """入力件数分の 1536 次元 embedding を返す fake。"""

    async def embed(
        self,
        texts: list[str],
        *,
        input_type: str = "SEARCH_DOCUMENT",
    ) -> list[list[float]]:
        _ = input_type
        return [[1.0] + [0.0] * 1535 for _ in texts]


class _FakeObjectStorage:
    """Object Storage artifact cache をメモリ上で再現する。"""

    def __init__(self, initial: dict[str, dict[str, object]]) -> None:
        self.objects = {
            key: json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
            for key, value in initial.items()
        }
        self.put_keys: list[str] = []

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        assert content_type == "application/json"
        reference = f"local://{key}"
        self.objects[reference] = data
        self.put_keys.append(key)
        return reference

    async def get(self, key: str) -> bytes:
        return self.objects[key]


class _FakeSegmentRetryOracle:
    """IngestionPipeline が使う Oracle 操作だけを実装する fake。"""

    def __init__(
        self,
        *,
        document_id: str,
        segments: list[IngestionSegment],
    ) -> None:
        self.document = DocumentDetail(
            id=document_id,
            file_name="segmented.pdf",
            status=FileStatus.UPLOADED,
            content_type="application/pdf",
            file_size_bytes=20,
            content_sha256=hashlib.sha256(b"%PDF segmented retry").hexdigest(),
            uploaded_at=datetime.now(UTC),
            object_storage_path="local://uploaded/segmented.pdf",
            source_profile=_pdf_source_profile(),
        )
        self.segments = segments
        self.saved_extraction: StructuredExtraction | None = None
        self.saved_chunk_count = 0
        self.saved_vector_count = 0

    async def update_document_status(
        self,
        document_id: str,
        status: FileStatus,
        error_message: str | None = None,
    ) -> DocumentDetail:
        assert document_id == self.document.id
        self.document = self.document.model_copy(
            update={"status": status, "error_message": error_message}
        )
        return self.document

    async def replace_ingestion_segments(
        self,
        document_id: str,
        segments: list[IngestionSegment],
    ) -> list[IngestionSegment]:
        assert document_id == self.document.id
        self.segments = list(segments)
        return list(self.segments)

    async def list_ingestion_segments(self, document_id: str) -> list[IngestionSegment]:
        assert document_id == self.document.id
        return [segment.model_copy() for segment in self.segments]

    async def update_ingestion_segment(
        self,
        segment_id: str,
        *,
        status: str | None = None,
        attempt_count: int | None = None,
        artifact_path: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> IngestionSegment | None:
        for index, segment in enumerate(self.segments):
            if segment.segment_id != segment_id:
                continue
            updates: dict[str, Any] = {}
            if status is not None:
                updates["status"] = status
            if attempt_count is not None:
                updates["attempt_count"] = attempt_count
            if artifact_path is not None:
                updates["artifact_path"] = artifact_path
            if error_code is not None:
                updates["error_code"] = error_code
            if error_message is not None:
                updates["error_message"] = error_message
            self.segments[index] = segment.model_copy(update=updates)
            return self.segments[index]
        return None

    async def save_index(
        self,
        document_id: str,
        extraction: StructuredExtraction,
        chunks: list[Any],
        vectors: list[list[float]],
        chunk_set_id: str | None = None,
    ) -> None:
        _ = chunk_set_id
        assert document_id == self.document.id
        self.saved_extraction = extraction
        self.saved_chunk_count = len(chunks)
        self.saved_vector_count = len(vectors)


def _pdf_source_profile() -> SourceProfile:
    digest = hashlib.sha256(b"%PDF segmented retry").hexdigest()
    return SourceProfile(
        original_file_name="segmented.pdf",
        sanitized_file_name="segmented.pdf",
        extension=".pdf",
        content_type="application/pdf",
        inferred_content_type="application/pdf",
        file_size_bytes=20,
        content_sha256=digest,
        modality=SourceModality.PDF,
        parser_profile="enterprise_ai_pdf_layout",
        parser_backend="enterprise_ai",
        parser_version="enterprise_ai_v1",
        preview_kind=SourcePreviewKind.PDF,
        quality_warnings=[],
    )


async def _inline_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    """unit test では thread を残さず、to_thread 対象を同期実行する。"""
    return func(*args, **kwargs)


def _extraction_payload(
    text: str,
    *,
    page_number: int,
    element_id: str,
    source_parser: str,
    parser_artifacts: dict[str, object] | None = None,
) -> dict[str, object]:
    resolved_parser_artifacts = {
        "parser_backend": "enterprise_ai",
        "source_parser": source_parser,
        **(parser_artifacts or {}),
    }
    extraction = StructuredExtraction(
        raw_text=text,
        document_type="社内規程",
        confidence=0.92,
        elements=[
            DocumentElement(
                kind="text",
                text=text,
                order=1,
                element_id=element_id,
                page_number=page_number,
                source_parser=source_parser,
            )
        ],
        parser_artifacts=resolved_parser_artifacts,
    )
    return extraction.to_document_payload()
