"""レシピの chunk_set を active にする処理のテスト(#568)。

`activate_recipe_chunk_set` は `materialized_revision` を省略(None)でも渡しても成功し、
省略時はレシピの `config_revision` を実体化済みの版として記録する。SQL と bind は fake の
接続で、実際の型の解決は実 Oracle で確かめる(未到達なら oracle_db fixture が skip し、
作成行は cleanup_to_baseline で後始末する)。
"""

from collections.abc import Callable, Mapping
from typing import Any, cast
from uuid import uuid4

import pytest

from app.clients.oracle import OracleClient
from app.config import Settings


class _RecordingCursor:
    """実行した SQL と bind を記録し、SELECT には行を 1 件返す fake cursor。"""

    description = None

    def __init__(self, calls: list[tuple[str, dict[str, object]]]) -> None:
        self._calls = calls
        self._rows: list[dict[str, object]] = []

    def execute(self, statement: str, parameters: Mapping[str, object] | None = None) -> None:
        self._calls.append((statement, dict(parameters or {})))
        lock_or_select = statement.startswith("SELECT recipe_id") or statement.startswith(
            "SELECT chunk_set_id FROM rag_chunk_sets WHERE chunk_set_id"
        )
        self._rows = [{"ok": 1}] if lock_or_select else []

    def fetchall(self) -> list[dict[str, object]]:
        return self._rows

    def close(self) -> None:
        return None


class _RecordingConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self.calls)


@pytest.mark.parametrize("materialized_revision", [None, 4], ids=["omitted", "given"])
async def test_activate_recipe_chunk_set_casts_materialized_revision_bind(
    monkeypatch: pytest.MonkeyPatch, materialized_revision: int | None
) -> None:
    """None の bind でも型が決まるよう、版の bind を NUMBER へ CAST して渡す(#568)。"""
    client = OracleClient(settings=Settings.model_construct())
    connection = _RecordingConnection()

    async def run_transaction(operation: Callable[[object], object]) -> object:
        return operation(connection)

    monkeypatch.setattr(client, "_run_transaction", run_transaction)

    await client.activate_recipe_chunk_set(
        recipe_id="recipe-1",
        chunk_set_id="cs-1",
        materialized_revision=materialized_revision,
    )

    [(statement, binds)] = [
        call for call in connection.calls if call[0].startswith("UPDATE rag_document_recipes")
    ]
    assert (
        "materialized_revision = COALESCE( CAST(:materialized_revision AS NUMBER), "
        "config_revision )" in statement
    )
    assert binds["materialized_revision"] == materialized_revision


@pytest.mark.usefixtures("oracle_db")
@pytest.mark.parametrize(
    ("materialized_revision", "expected_revision"),
    [(None, 1), (3, 3)],
    ids=["omitted", "given"],
)
async def test_activate_recipe_chunk_set_records_materialized_revision(
    materialized_revision: int | None, expected_revision: int
) -> None:
    """版の省略時は config_revision(新しいレシピは 1)、指定時はその版を記録する。"""
    client = OracleClient()
    detail = await client.create_document(
        file_name="activate-recipe.txt",
        object_storage_path="oci://bucket/activate-recipe.txt",
        content_type="text/plain",
    )
    recipe = await client.ensure_default_document_recipe(detail.id)
    recipe_id = str(recipe["recipe_id"])
    chunk_set_id = f"cs_activate_{uuid4().hex[:16]}"
    await client.upsert_chunk_set(
        chunk_set_id=chunk_set_id, document_id=detail.id, recipe_id=recipe_id
    )
    await client.mark_chunk_set_indexed(chunk_set_id=chunk_set_id, chunk_count=0, vector_count=0)

    await client.activate_recipe_chunk_set(
        recipe_id=recipe_id,
        chunk_set_id=chunk_set_id,
        materialized_revision=materialized_revision,
    )

    activated = await client.get_document_recipe(detail.id, recipe_id)
    assert activated is not None
    assert activated["status"] == "INDEXED"
    assert activated["active_chunk_set_id"] == chunk_set_id
    assert activated["materialized_revision"] == expected_revision


@pytest.mark.usefixtures("oracle_db")
async def test_clone_active_recipe_chunks_preserves_metadata_and_keeps_old_active() -> None:
    from app.rag.chunking import Chunk
    from app.schemas.extraction import StructuredExtraction

    client = OracleClient()
    document = await client.create_document(
        file_name="synthetic-reindex.txt",
        object_storage_path="synthetic-reindex.txt",
        content_type="text/plain",
    )
    recipe = await client.ensure_default_document_recipe(document.id)
    recipe_id = str(recipe["recipe_id"])
    source = f"source_{uuid4().hex[:16]}"
    target = f"target_{uuid4().hex[:16]}"
    await client.upsert_chunk_set(chunk_set_id=source, document_id=document.id, recipe_id=recipe_id)
    await client.save_chunk_preview(
        document.id,
        StructuredExtraction(raw_text="合成の本文"),
        [
            Chunk(
                index=0,
                text="合成の本文",
                start_offset=0,
                end_offset=5,
                metadata={
                    "source_element_ids": cast(Any, ["element-1", "element-2"]),
                    "page_start": 2,
                },
            )
        ],
        chunk_set_id=source,
    )
    await client.mark_chunk_set_indexed(chunk_set_id=source, chunk_count=1, vector_count=0)
    await client.activate_recipe_chunk_set(recipe_id=recipe_id, chunk_set_id=source)
    await client.upsert_chunk_set(chunk_set_id=target, document_id=document.id, recipe_id=recipe_id)
    await client.clone_recipe_chunk_set_chunks(
        recipe_id=recipe_id,
        source_chunk_set_id=source,
        target_chunk_set_id=target,
    )
    original = await client.list_chunk_set_chunks(source)
    copied = await client.list_chunk_set_chunks(target)
    assert len(original) == len(copied) == 1
    assert copied[0].text == original[0].text
    assert copied[0].chunk_id != original[0].chunk_id
    assert copied[0].metadata["chunk_id"] == copied[0].chunk_id
    assert copied[0].metadata["chunk_set_id"] == target
    assert copied[0].metadata["source_element_ids"] == ["element-1", "element-2"]
    active = await client.get_document_recipe(document.id, recipe_id)
    assert active is not None and active["active_chunk_set_id"] == source
