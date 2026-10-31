"""文書の一覧の「状態」をレシピ1の状態から出すテスト(#2012)。

取込の job はレシピの job で、工程の状態はレシピの行だけに書く(#504)。一覧の `status`・状態の
絞り込み・件数は、文書の詳細が既定で出す状態(`slot_no` の最も小さいレシピの状態。レシピの行が
無ければ文書の状態)と同じ正本から出す。レシピ1の job が順番待ちの文書は、行に
「〈工程〉の順番待ち」と出るため、状態の絞り込みでは `queued_phase` で選び、`status` の結果に
入れない(#2677)。SQL の形は fake の pool で、実際の値は実 Oracle で確かめる(未到達なら oracle_db
fixture が skip し、作成行は cleanup_to_baseline で後始末する)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.api.routes import documents as documents_route
from app.clients.oracle import OracleClient
from app.main import app
from app.schemas.document import (
    DocumentSummary,
    FileStatus,
    IngestionJob,
    IngestionJobPhase,
    IngestionJobStatus,
)
from tests.support import AsgiTestClient
from tests.test_oracle_adapter import FakeOraclePool, _oci_settings, _run_inline
from tests.test_oracle_db_paths import _document_state_row

_RECIPE_STATUS_SQL = "MIN(r.status) KEEP (DENSE_RANK FIRST ORDER BY r.slot_no)"
_QUEUED_PHASE_SQL = "MIN(qj.phase) KEEP (DENSE_RANK FIRST ORDER BY qj.queued_at, qj.job_id)"

api_client = AsgiTestClient(app)


def _list_selects(pool: FakeOraclePool) -> list[str]:
    return [
        call.statement
        for call in pool.connection.calls
        if call.statement.lstrip().upper().startswith("SELECT")
        and "FROM rag_documents" in call.statement
    ]


async def test_list_documents_selects_status_from_first_recipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一覧の列の状態は、レシピ1の状態(無ければ文書の状態)を `status` として読む。"""
    pool = FakeOraclePool(
        execute_results=[
            [
                _document_state_row(document_id="doc-1", status="REVIEW", ingestion_active=0),
                _document_state_row(document_id="doc-2", status="UPLOADED", ingestion_active=1),
            ],
            [],
        ]
    )
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)
    # 公開版の取得は独立に検証し、このテストでは元の文書 SQL の応答だけを供給する。
    monkeypatch.setattr(client, "document_language_pack_uses", AsyncMock(return_value={}))

    documents = await client.list_documents(limit=10)

    assert [document.status for document in documents] == [FileStatus.REVIEW, FileStatus.UPLOADED]
    # 待機中・実行中の job がある文書は、状態が変わる前でも一覧の取り直しの対象にする。
    assert [document.ingestion_active for document in documents] == [False, True]
    [statement, *_] = _list_selects(pool)
    # SQL は空白を詰めて送る。外側の FROM より前が列の一覧。
    select_list = statement.rsplit(" FROM rag_documents WHERE ", 1)[0]
    assert _RECIPE_STATUS_SQL in select_list
    assert "rag_documents.status ) AS status," in select_list
    assert "FROM rag_ingestion_jobs j" in select_list
    assert "j.status IN ('QUEUED', 'RUNNING')" in select_list


async def test_list_and_count_filter_status_by_first_recipe() -> None:
    """状態の絞り込みと件数は、一覧に出す状態と同じ式で判定する(文書の列だけで判定しない)。"""
    pool = FakeOraclePool(execute_results=[[], [{"count_value": 0}]])
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)

    await client.list_documents(status=FileStatus.REVIEW, limit=10)
    await client.count_documents(status=FileStatus.REVIEW)

    statements = _list_selects(pool)
    assert len(statements) == 2
    for statement in statements:
        where_clause = statement.rsplit(" FROM rag_documents WHERE ", 1)[1]
        assert _RECIPE_STATUS_SQL in where_clause
        assert ") = :status" in where_clause
    binds = [call.parameters for call in pool.connection.calls if "= :status" in call.statement]
    assert all(bind["status"] == "REVIEW" for bind in binds)


async def test_status_filter_excludes_queued_documents() -> None:
    """`status` の絞り込みと件数は、レシピ1の job が順番待ちの文書を除く(#2677)。

    順番待ちの行は「〈工程〉の順番待ち」と出るため、行の表示とそろえる。
    """
    pool = FakeOraclePool(execute_results=[[], [{"count_value": 0}]])
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)

    await client.list_documents(status=FileStatus.UPLOADED, limit=10)
    await client.count_documents(status=FileStatus.UPLOADED)

    statements = _list_selects(pool)
    assert len(statements) == 2
    for statement in statements:
        where_clause = statement.rsplit(" FROM rag_documents WHERE ", 1)[1]
        assert _QUEUED_PHASE_SQL in where_clause
        assert ") IS NULL" in where_clause
        assert ":queued_phase" not in where_clause


async def test_list_and_count_filter_by_queued_phase() -> None:
    """`queued_phase` は一覧と件数を、レシピ1の最初の QUEUED の job の段で同じ式で絞る(#2677)。"""
    pool = FakeOraclePool(execute_results=[[], [{"count_value": 0}]])
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)

    await client.list_documents(queued_phase=IngestionJobPhase.PREPROCESS, limit=10, offset=25)
    await client.count_documents(queued_phase=IngestionJobPhase.PREPROCESS)

    statements = _list_selects(pool)
    assert len(statements) == 2
    for statement in statements:
        where_clause = statement.rsplit(" FROM rag_documents WHERE ", 1)[1]
        assert _QUEUED_PHASE_SQL in where_clause
        assert ") = :queued_phase" in where_clause
        # 一覧の順番待ちの表示(`_attach_queue_waits`)と同じく、レシピ1の job だけを見る。
        assert "qj.status = 'QUEUED'" in where_clause
        assert "MIN(fr.slot_no)" in where_clause
        # 状態は絞らない。
        assert ":status" not in where_clause
    calls = [call for call in pool.connection.calls if ":queued_phase" in call.statement]
    assert len(calls) == 2
    assert all(call.parameters["queued_phase"] == "PREPROCESS" for call in calls)
    # ページングは SQL の OFFSET / FETCH で行う(順番待ちの絞り込みを手元でしない)。
    [list_call, _count_call] = calls
    assert list_call.parameters["offset"] == 25
    assert "OFFSET :offset ROWS" in list_call.statement


class _RecordingListOracle:
    """一覧の API が oracle に渡した絞り込みを記録する fake。"""

    def __init__(self) -> None:
        self.list_kwargs: list[dict[str, object]] = []
        self.count_kwargs: list[dict[str, object]] = []

    async def list_documents(self, **kwargs: object) -> list[DocumentSummary]:
        self.list_kwargs.append(kwargs)
        return []

    async def count_documents(self, **kwargs: object) -> int:
        self.count_kwargs.append(kwargs)
        return 7


def test_list_api_passes_queued_phase_to_list_and_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """`GET /api/documents?queued_phase=` は一覧と件数の両方に同じ段を渡す(#2677)。"""
    fake = _RecordingListOracle()
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)

    resp = api_client.get("/api/documents", params={"queued_phase": "EXTRACT", "limit": 25})

    assert resp.status_code == 200
    assert resp.json()["data"]["total"] == 7
    [list_kwargs] = fake.list_kwargs
    [count_kwargs] = fake.count_kwargs
    assert list_kwargs["queued_phase"] == IngestionJobPhase.EXTRACT
    assert count_kwargs["queued_phase"] == IngestionJobPhase.EXTRACT
    assert list_kwargs["status"] is None
    assert count_kwargs["status"] is None


@pytest.mark.parametrize(
    ("params", "expected_status"),
    [
        ({"status": "UPLOADED", "queued_phase": "PREPROCESS"}, 422),
        ({"queued_phase": "UNKNOWN"}, 422),
    ],
    ids=["status-and-queued-phase", "unknown-phase"],
)
def test_list_api_rejects_invalid_queued_phase(
    monkeypatch: pytest.MonkeyPatch, params: dict[str, str], expected_status: int
) -> None:
    """状態と順番待ちの段は同時に選べず、知らない段は受け付けない(#2677)。"""
    fake = _RecordingListOracle()
    monkeypatch.setattr(documents_route, "OracleClient", lambda: fake)

    resp = api_client.get("/api/documents", params=params)

    assert resp.status_code == expected_status
    assert fake.list_kwargs == []


@pytest.mark.usefixtures("oracle_db")
async def test_list_documents_status_follows_recipe_status_in_oracle() -> None:
    """レシピの job が進めたレシピ1の状態が、一覧の状態・絞り込み・件数に出る(#2012)。"""
    client = OracleClient()
    file_name = f"list-status-{uuid4().hex[:12]}.txt"
    detail = await client.create_document(
        file_name=file_name,
        object_storage_path=f"oci://bucket/{file_name}",
        content_type="text/plain",
    )
    recipe = await client.ensure_default_document_recipe(detail.id)

    # 登録直後はレシピ1も文書も UPLOADED。
    [listed] = await client.list_documents(query=file_name, limit=10)
    assert listed.status == FileStatus.UPLOADED

    # レシピの job は文書の status を変えず、レシピの行だけを進める(#504)。
    await client.update_document_recipe_status(
        recipe_id=str(recipe["recipe_id"]), status=FileStatus.REVIEW
    )
    stored = await client.get_document(detail.id)
    assert stored is not None
    assert stored.status == FileStatus.UPLOADED

    [listed] = await client.list_documents(query=file_name, limit=10)
    assert listed.status == FileStatus.REVIEW
    assert [
        document.id
        for document in await client.list_documents(
            status=FileStatus.REVIEW, query=file_name, limit=10
        )
    ] == [detail.id]
    assert await client.list_documents(status=FileStatus.UPLOADED, query=file_name, limit=10) == []
    assert await client.count_documents(status=FileStatus.REVIEW, query=file_name) == 1
    assert await client.count_documents(status=FileStatus.UPLOADED, query=file_name) == 0
    assert listed.ingestion_active is False

    # 待機中の job がある間は、レシピの状態が変わる前でも ingestion_active が立つ。
    await client.create_ingestion_job(
        IngestionJob(
            id=f"job_list_status_{uuid4().hex[:16]}",
            document_id=detail.id,
            recipe_id=str(recipe["recipe_id"]),
            recipe_revision=int(str(recipe["config_revision"])),
            status=IngestionJobStatus.QUEUED,
            parser_profile="local_text_structure",
            queued_at=datetime.now(UTC),
        )
    )
    [listed] = await client.list_documents(query=file_name, limit=10)
    assert listed.ingestion_active is True

    # 順番待ちの文書は `queued_phase` で選び、`status` の結果に入れない(#2677)。
    assert [
        document.id
        for document in await client.list_documents(
            queued_phase=IngestionJobPhase.PREPROCESS, query=file_name, limit=10
        )
    ] == [detail.id]
    assert (
        await client.count_documents(queued_phase=IngestionJobPhase.PREPROCESS, query=file_name)
        == 1
    )
    assert (
        await client.count_documents(queued_phase=IngestionJobPhase.EXTRACT, query=file_name) == 0
    )
    assert await client.list_documents(status=FileStatus.REVIEW, query=file_name, limit=10) == []
    assert await client.count_documents(status=FileStatus.REVIEW, query=file_name) == 0
