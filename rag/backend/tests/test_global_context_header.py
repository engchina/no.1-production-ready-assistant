"""文脈ヘッダの全体設定を旧レシピとプレビューから無効化させない。"""

from app.api.routes.documents import _merge_document_processing_config
from app.config import Settings
from app.schemas.document import DocumentChunkPreviewRequest, DocumentProcessingConfig


def test_old_recipe_cannot_override_global_context_header() -> None:
    settings = Settings(_env_file=None, rag_chunk_context_header_enabled=True)
    config = DocumentProcessingConfig.model_validate({"chunk_context_header_enabled": False})
    effective, snapshot = _merge_document_processing_config(config, settings)
    assert effective.rag_chunk_context_header_enabled is True
    assert "chunk_context_header_enabled" not in config.model_dump()
    assert "chunk_context_header_enabled" not in snapshot.model_dump()
    request = DocumentChunkPreviewRequest.model_validate({"chunk_context_header_enabled": False})
    assert "rag_chunk_context_header_enabled" not in request.settings_overrides()
