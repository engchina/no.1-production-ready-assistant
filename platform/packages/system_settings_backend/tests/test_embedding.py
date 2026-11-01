"""埋め込みの次元の正本（#46）。"""

from __future__ import annotations

from pr_system_settings.database import database_settings_data
from pr_system_settings.embedding import EMBEDDING_DIMENSIONS, EMBEDDING_VECTOR_COLUMN_TYPE


def test_default_is_text_embedding_v4_default_dimensions() -> None:
    assert EMBEDDING_DIMENSIONS == 1024
    assert EMBEDDING_VECTOR_COLUMN_TYPE == "VECTOR(1024, FLOAT32)"


def test_database_settings_show_the_code_vector_column() -> None:
    """データベースの設定の表示は、設定の値ではなく DDL と同じ正本を出す。"""

    class Settings:
        genai_embedding_dim = 1536

    data = database_settings_data(Settings())

    assert data.embedding_dimension == EMBEDDING_DIMENSIONS
    assert data.vector_column == EMBEDDING_VECTOR_COLUMN_TYPE
