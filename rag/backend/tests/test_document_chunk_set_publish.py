"""chunk_set 公開境界の単体テスト。"""

from datetime import UTC, datetime
from typing import Any

import pytest

from app.api.routes.documents import _reconcile_document_chunk_sets
from app.config import get_settings
from app.rag.ingestion import IngestionUserError
from app.schemas.document import DocumentDetail, DocumentProcessingConfig, FileStatus


class FakePublishOracle:
    """_reconcile_document_chunk_sets に必要な最小 Oracle subset。"""

    def __init__(self) -> None:
        self.statuses: list[tuple[FileStatus, str | None]] = []

    async def count_document_chunks(self, document_id: str) -> int:
        _ = document_id
        return 2

    async def upsert_chunk_set(self, **_kwargs: Any) -> None:
        return None

    async def mark_chunk_set_indexed(self, **_kwargs: Any) -> None:
        return None

    async def upsert_document_extraction_artifact(self, **_kwargs: Any) -> None:
        return None

    async def delete_stale_document_chunk_sets(self, **_kwargs: Any) -> None:
        return None

    async def set_document_serving_chunk_set(self, **_kwargs: Any) -> None:
        raise RuntimeError("serving failed")

    async def update_document_status(
        self,
        document_id: str,
        status: FileStatus,
        error_message: str | None = None,
    ) -> DocumentDetail:
        self.statuses.append((status, error_message))
        return _detail(document_id, status=status, error_message=error_message)


def _detail(
    document_id: str = "doc-1",
    *,
    status: FileStatus = FileStatus.INDEXED,
    error_message: str | None = None,
) -> DocumentDetail:
    return DocumentDetail(
        id=document_id,
        file_name="policy.txt",
        status=status,
        content_type="text/plain",
        file_size_bytes=24,
        content_sha256="a" * 64,
        uploaded_at=datetime.now(UTC),
        indexed_at=datetime.now(UTC) if status == FileStatus.INDEXED else None,
        extraction={
            "raw_text": "社内規程",
            "document_type": "社内規程",
            "confidence": 1.0,
            "elements": [{"kind": "text", "text": "社内規程", "order": 1}],
        },
        error_message=error_message,
    )


@pytest.mark.asyncio
async def test_chunk_set_serving_failure_marks_document_error() -> None:
    """chunk/vector 保存後でも serving 確定に失敗したら INDEXED 成功扱いにしない。"""
    oracle = FakePublishOracle()

    with pytest.raises(IngestionUserError, match="公開設定"):
        await _reconcile_document_chunk_sets(
            oracle,  # type: ignore[arg-type]
            "doc-1",
            _detail(),
            "chunk-set-1",
            get_settings(),
            DocumentProcessingConfig(),
        )

    assert oracle.statuses == [
        (FileStatus.ERROR, "索引の公開設定に失敗しました。時間をおいて再実行してください。")
    ]
