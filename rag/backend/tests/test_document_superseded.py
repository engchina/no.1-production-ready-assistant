"""文書の版(新しい版に置き換えた文書=旧版)の記録と、回答の検索からの除外のテスト(#1248)。"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.api.routes import documents as documents_route
from app.clients import oracle as oracle_module
from app.clients.oracle import (
    DocumentSupersessionError,
    OracleClient,
    _oracle_retrieval_where,
    _retrieved_chunk_from_row,
)
from app.main import app
from app.mcp.tools import _evidence
from app.rag import oracle_schema
from app.rag.answer_engine import _source_file_name
from app.schemas.document import (
    DocumentDetail,
    DocumentSummary,
    DocumentSupersededByRequest,
    FileStatus,
)
from app.schemas.search import RetrievedChunk, normalize_search_filters
from tests.support import AsgiTestClient

client = AsgiTestClient(app)

_NOT_SUPERSEDED = "d.superseded_by_document_id IS NULL"


# ---- schema / migration ----


def test_document_ddl_and_migration_add_superseded_columns() -> None:
    """列は DDL と migration の両方に持ち、自己参照の FK は付けない。"""
    ddl = oracle_module.oracle_document_schema_sql()
    assert "superseded_by_document_id VARCHAR2(64)" in ddl
    assert "superseded_at            TIMESTAMP WITH TIME ZONE" in ddl
    assert "REFERENCES rag_documents (document_id)" in ddl  # 既存の重複の FK だけ
    assert ddl.count("REFERENCES") == 1

    migration = next(
        section
        for section in oracle_schema.oracle_schema_migration_sections()
        if section.name == "20261007_001_document_superseded"
    )
    assert migration.table_name == "rag_documents"
    assert not migration.destructive
    for column in ("SUPERSEDED_BY_DOCUMENT_ID", "SUPERSEDED_AT"):
        assert f"column_name = '{column}'" in migration.sql
    assert "ADD (superseded_by_document_id VARCHAR2(64))" in migration.sql
    assert "ADD (superseded_at TIMESTAMP WITH TIME ZONE)" in migration.sql
    assert "FOREIGN KEY" not in migration.sql
    # 後の migration（#1362 など）で上がる。この migration 以降の版であること。
    assert oracle_schema.MIGRATION_ARTIFACT_VERSION >= "20261007_001"


def test_document_summary_exposes_superseded_state() -> None:
    current = DocumentSummary(
        id="doc-2",
        file_name="v2.pdf",
        status=FileStatus.INDEXED,
        uploaded_at=datetime(2026, 10, 1, tzinfo=UTC),
    )
    superseded = current.model_copy(
        update={
            "id": "doc-1",
            "superseded_by_document_id": "doc-2",
            "superseded_by_file_name": "v2.pdf",
            "superseded_at": datetime(2026, 10, 7, tzinfo=UTC),
        }
    )

    assert current.model_dump(mode="json")["is_superseded"] is False
    dumped = superseded.model_dump(mode="json")
    assert dumped["is_superseded"] is True
    assert dumped["superseded_by_document_id"] == "doc-2"
    assert dumped["superseded_by_file_name"] == "v2.pdf"


def test_superseded_by_request_normalizes_blank_and_rejects_unknown_keys() -> None:
    blank = DocumentSupersededByRequest(superseded_by_document_id=" ")
    assert blank.superseded_by_document_id is None
    assert DocumentSupersededByRequest().superseded_by_document_id is None
    with pytest.raises(ValidationError):
        DocumentSupersededByRequest.model_validate({"superseded_by": "doc-2"})


# ---- 検索の除外 ----


def test_normalize_search_filters_accepts_include_superseded() -> None:
    assert normalize_search_filters({"include_superseded": " TRUE "}) == {
        "include_superseded": "true"
    }
    # false は既定と同じなので落とす。
    assert normalize_search_filters({"include_superseded": "false"}) == {}
    with pytest.raises(ValueError, match="include_superseded"):
        normalize_search_filters({"include_superseded": "yes"})


def test_retrieval_where_excludes_superseded_documents_by_default() -> None:
    sql, _ = _oracle_retrieval_where({})
    assert _NOT_SUPERSEDED in sql

    sql, _ = _oracle_retrieval_where({"knowledge_base_id": "kb-1", "category_name": "手順"})
    assert _NOT_SUPERSEDED in sql


def test_retrieval_where_includes_superseded_when_requested_or_document_named() -> None:
    included, _ = _oracle_retrieval_where({"include_superseded": "true"})
    assert _NOT_SUPERSEDED not in included

    # 特定の文書を読む経路(文書の chunk 数・根拠の前後の補完)は旧版でも読む。
    named, binds = _oracle_retrieval_where({"document_id": "doc-1"})
    assert _NOT_SUPERSEDED not in named
    assert binds["filter_document_id"] == "doc-1"

    excluded, _ = _oracle_retrieval_where({"include_superseded": "false"})
    assert _NOT_SUPERSEDED in excluded


def test_retrievable_chunk_reads_superseded_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP の rag_read_source は旧版の根拠も読める(文書としては有効なため)。"""
    captured: list[str] = []

    class _Client(OracleClient):
        async def _fetch_one(
            self, statement: str, binds: Mapping[str, object] | None = None
        ) -> dict[str, object] | None:
            captured.append(statement)
            return None

    fake = _Client.__new__(_Client)
    assert asyncio.run(fake.retrievable_chunk("doc-1", "chunk-1")) is None
    assert _NOT_SUPERSEDED not in captured[0]
    assert "d.superseded_by_document_id" in captured[0]


def test_retrieved_chunk_marks_superseded_document() -> None:
    row = {
        "document_id": "doc-1",
        "chunk_id": "chunk-1",
        "chunk_text": "第 1 版の手順",
        "metadata_json": "{}",
        "chunk_index": 0,
        "file_name": "manual-v1.pdf",
        "superseded_by_document_id": "doc-2",
        "score": 0.5,
    }
    chunk = _retrieved_chunk_from_row(row)
    assert chunk.metadata["document_superseded"] is True
    assert chunk.metadata["superseded_by_document_id"] == "doc-2"
    # 文書名そのものは変えない(文書名の絞り込み・画面目録の照合に使うため)。
    assert chunk.file_name == "manual-v1.pdf"

    current = _retrieved_chunk_from_row({**row, "superseded_by_document_id": None})
    assert "document_superseded" not in current.metadata


def _chunk(*, superseded: bool) -> RetrievedChunk:
    return RetrievedChunk(
        document_id="doc-1",
        chunk_id="chunk-1",
        text="手順",
        score=0.5,
        file_name="manual-v1.pdf",
        metadata={"document_superseded": True} if superseded else {},
    )


def test_answer_source_file_name_marks_superseded_document() -> None:
    assert _source_file_name(_chunk(superseded=True)) == "manual-v1.pdf（旧版）"
    assert _source_file_name(_chunk(superseded=False)) == "manual-v1.pdf"


def test_mcp_evidence_reports_superseded() -> None:
    assert _evidence(_chunk(superseded=True)).superseded is True
    assert _evidence(_chunk(superseded=False)).superseded is False


# ---- 保存(OracleClient)----


class _Table:
    """``rag_documents`` の id → superseded_by_document_id(見える文書だけ)。"""

    def __init__(self, rows: dict[str, str | None], *, hidden: set[str] | None = None) -> None:
        self.rows = rows
        self.hidden = hidden or set()
        self.updates: list[Mapping[str, object]] = []
        self.locked: list[str] = []


def _client(monkeypatch: pytest.MonkeyPatch, table: _Table) -> OracleClient:
    def select_for_update(_connection: object, document_id: str) -> dict[str, object] | None:
        table.locked.append(document_id)
        visible = document_id in table.rows and document_id not in table.hidden
        return {"document_id": document_id} if visible else None

    def select_state(_connection: object, document_id: str) -> object | None:
        visible = document_id in table.rows and document_id not in table.hidden
        return object() if visible else None

    def fetch_one(
        _connection: object, _statement: str, binds: Mapping[str, object]
    ) -> dict[str, object] | None:
        document_id = str(binds["chain_document_id"])
        if document_id not in table.rows:
            return None
        return {"superseded_by_document_id": table.rows[document_id]}

    def execute(_connection: object, statement: str, binds: Mapping[str, object]) -> None:
        assert "UPDATE rag_documents" in statement
        table.updates.append(binds)
        table.rows[str(binds["document_id"])] = binds["superseded_by_document_id"]  # type: ignore[assignment]

    def detail(_connection: object, document_id: str) -> DocumentDetail:
        return DocumentDetail(
            id=document_id,
            file_name=f"{document_id}.pdf",
            status=FileStatus.INDEXED,
            uploaded_at=datetime(2026, 10, 1, tzinfo=UTC),
            superseded_by_document_id=table.rows[document_id],
        )

    monkeypatch.setattr(oracle_module, "_select_document_for_update", select_for_update)
    monkeypatch.setattr(oracle_module, "_select_document_state", select_state)
    monkeypatch.setattr(oracle_module, "_fetch_one", fetch_one)
    monkeypatch.setattr(oracle_module, "_execute", execute)
    monkeypatch.setattr(oracle_module, "_select_document_detail_with_refs", detail)

    class _Client(OracleClient):
        async def _run_transaction(self, operation, *, retry_deadlocks=False):  # type: ignore[no-untyped-def]
            return operation(object())

    return _Client.__new__(_Client)


def test_set_superseded_by_saves_and_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    table = _Table({"doc-1": None, "doc-2": None})
    oracle = _client(monkeypatch, table)

    saved = asyncio.run(oracle.set_document_superseded_by("doc-1", "doc-2"))
    assert saved.is_superseded and saved.superseded_by_document_id == "doc-2"
    # 2 つの文書を ID の順にロックする(逆向きの同時の設定を直列にする)。
    assert table.locked == ["doc-1", "doc-2"]

    cleared = asyncio.run(oracle.set_document_superseded_by("doc-1", None))
    assert not cleared.is_superseded
    assert table.updates[-1]["superseded_by_document_id"] is None


def test_set_superseded_by_rejects_self_cycle_and_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # doc-3 → doc-2 → doc-1 の連鎖。doc-1 を doc-3 で置き換えると循環する。
    table = _Table(
        {"doc-1": None, "doc-2": "doc-1", "doc-3": "doc-2", "doc-x": None},
        hidden={"doc-x"},
    )
    oracle = _client(monkeypatch, table)

    with pytest.raises(DocumentSupersessionError, match="文書自身"):
        asyncio.run(oracle.set_document_superseded_by("doc-1", "doc-1"))
    with pytest.raises(DocumentSupersessionError, match="循環"):
        asyncio.run(oracle.set_document_superseded_by("doc-1", "doc-2"))
    with pytest.raises(DocumentSupersessionError, match="循環"):
        asyncio.run(oracle.set_document_superseded_by("doc-1", "doc-3"))
    with pytest.raises(DocumentSupersessionError, match="見つかりません"):
        asyncio.run(oracle.set_document_superseded_by("doc-1", "doc-missing"))
    # 利用者から見えない文書は新しい版にできない。
    with pytest.raises(DocumentSupersessionError, match="見つかりません"):
        asyncio.run(oracle.set_document_superseded_by("doc-1", "doc-x"))
    with pytest.raises(KeyError):
        asyncio.run(oracle.set_document_superseded_by("doc-missing", "doc-1"))
    assert table.updates == []

    # 連鎖の外の新しい文書で置き換えるのは通る。
    table.rows["doc-4"] = None
    saved = asyncio.run(oracle.set_document_superseded_by("doc-1", "doc-4"))
    assert saved.superseded_by_document_id == "doc-4"


# ---- API ----


class FakeSupersessionOracle:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def set_document_superseded_by(
        self, document_id: str, superseded_by_document_id: str | None
    ) -> DocumentDetail:
        self.calls.append((document_id, superseded_by_document_id))
        if document_id == "doc-missing":
            raise KeyError(document_id)
        if superseded_by_document_id == document_id:
            raise DocumentSupersessionError("文書自身を新しい版にはできません。")
        if superseded_by_document_id == "doc-cycle":
            raise DocumentSupersessionError("版の置き換えが循環します。")
        return DocumentDetail(
            id=document_id,
            file_name="manual-v1.pdf",
            status=FileStatus.INDEXED,
            uploaded_at=datetime(2026, 10, 1, tzinfo=UTC),
            superseded_by_document_id=superseded_by_document_id,
            superseded_by_file_name="manual-v2.pdf" if superseded_by_document_id else None,
            superseded_at=datetime(2026, 10, 7, tzinfo=UTC) if superseded_by_document_id else None,
        )


def test_put_superseded_by_sets_and_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeSupersessionOracle()
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)

    response = client.put(
        "/api/documents/doc-1/superseded-by", json={"superseded_by_document_id": "doc-2"}
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["is_superseded"] is True
    assert data["superseded_by_document_id"] == "doc-2"
    assert data["superseded_by_file_name"] == "manual-v2.pdf"

    cleared = client.put(
        "/api/documents/doc-1/superseded-by", json={"superseded_by_document_id": None}
    )
    assert cleared.status_code == 200
    assert cleared.json()["data"]["is_superseded"] is False
    assert fake.calls == [("doc-1", "doc-2"), ("doc-1", None)]


def test_put_superseded_by_rejects_self_cycle_and_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(documents_route, "OracleClient", FakeSupersessionOracle)

    own = client.put(
        "/api/documents/doc-1/superseded-by", json={"superseded_by_document_id": "doc-1"}
    )
    cycle = client.put(
        "/api/documents/doc-1/superseded-by", json={"superseded_by_document_id": "doc-cycle"}
    )
    missing = client.put(
        "/api/documents/doc-missing/superseded-by", json={"superseded_by_document_id": "doc-1"}
    )
    unknown_key = client.put("/api/documents/doc-1/superseded-by", json={"document": "doc-2"})

    assert own.status_code == 422
    assert "文書自身" in own.text
    assert cycle.status_code == 422
    assert missing.status_code == 404
    assert unknown_key.status_code == 422


# ---- 実 Oracle ----


@pytest.mark.usefixtures("oracle_db")
async def test_superseded_document_is_excluded_from_retrieval_on_real_oracle() -> None:
    """実 Oracle AI Database で、置き換え・循環の拒否・除外の述語・新しい版の削除を確かめる。"""
    oracle = OracleClient()
    old = await oracle.create_document(
        file_name="manual-v1.pdf",
        object_storage_path="local://manual-v1.pdf",
        content_type="application/pdf",
    )
    new = await oracle.create_document(
        file_name="manual-v2.pdf",
        object_storage_path="local://manual-v2.pdf",
        content_type="application/pdf",
    )

    saved = await oracle.set_document_superseded_by(old.id, new.id)
    assert saved.is_superseded
    assert saved.superseded_by_file_name == "manual-v2.pdf"
    assert saved.superseded_at is not None
    with pytest.raises(DocumentSupersessionError):
        await oracle.set_document_superseded_by(new.id, old.id)

    async def matches(document_id: str, filters: dict[str, str]) -> bool:
        clause = oracle_module._NOT_SUPERSEDED_SQL  # noqa: SLF001 - 述語を実 DB で評価する
        if oracle_module._includes_superseded_documents(filters):  # noqa: SLF001
            clause = "1 = 1"
        row = await oracle._fetch_one(  # noqa: SLF001
            "SELECT COUNT(*) AS count_value FROM rag_documents d "
            "WHERE d.document_id = :document_id AND " + clause,
            {"document_id": document_id},
        )
        return bool(row and int(str(row["count_value"])))

    assert not await matches(old.id, {})
    assert await matches(old.id, {"include_superseded": "true"})
    assert await matches(new.id, {})

    listed = {document.id: document for document in await oracle.list_documents()}
    assert listed[old.id].superseded_by_file_name == "manual-v2.pdf"

    # 新しい版を削除すると、旧版は今有効な版に戻る。
    assert await oracle.delete_document(new.id)
    restored = await oracle.get_document(old.id)
    assert restored is not None and not restored.is_superseded
    assert await matches(old.id, {})
    await oracle.delete_document(old.id)
