"""文書インデックスと全件選択がファイル名の自然順で一致することを確かめる。"""

from uuid import uuid4

import pytest

from app.clients.oracle import OracleClient
from app.schemas.document import FileStatus
from tests.test_oracle_adapter import FakeOraclePool, _oci_settings, _run_inline


async def test_document_list_and_bulk_selection_use_the_same_order() -> None:
    pool = FakeOraclePool(execute_results=[[], [], [{"count_value": 0}]])
    client = OracleClient(settings=_oci_settings(), pool=pool, db_call_runner=_run_inline)
    assert await client.list_documents(limit=3, offset=3) == []
    assert await client.list_document_ids(limit=3) == ([], 0)
    list_sql, ids_sql, _ = [call.statement for call in pool.connection.calls]
    # 列・絞り込みの中にも ORDER BY があるため、外側の並びを比べる。
    list_order = list_sql.split("ORDER BY NLSSORT((", 1)[1].split(" OFFSET", 1)[0]
    ids_order = ids_sql.split("ORDER BY NLSSORT((", 1)[1].split(" FETCH FIRST", 1)[0]
    assert list_order == ids_order
    assert list_order.endswith("file_name ASC, document_id ASC")


@pytest.mark.usefixtures("oracle_db")
async def test_document_name_order_across_pages_and_bulk_selection() -> None:
    client = OracleClient()
    suffix = uuid4().hex[:12]
    kb = await client.create_knowledge_base(name=f"sort-{suffix}", description="並べ替えのテスト")
    names = ["0", "2", "10", "20", "20", "21", "１００", "a2b2", "a2b10", "a10b2", "あ", "漢字"]
    groups: dict[str, list[str]] = {}
    for name in names:
        document = await client.create_document(
            file_name=f"{name}_{suffix}.txt",
            object_storage_path=f"local://sort/{uuid4().hex}.txt",
            knowledge_base_ids=[kb.id],
            content_type="text/plain",
        )
        groups.setdefault(name, []).append(document.id)
    # 同名の文書は ID で順を固定する。新しいアップロードを先頭にしない。
    expected = [identifier for group in groups.values() for identifier in sorted(group)]
    # 同じ検索語に一致する別 KB の文書は混ぜない。
    await client.create_document(
        file_name=f"15_{suffix}.txt",
        object_storage_path=f"local://sort/{suffix}/other.txt",
        content_type="text/plain",
    )
    actual: list[str] = []
    for offset in range(0, len(expected), 3):
        page = await client.list_documents(
            query=suffix,
            status=FileStatus.UPLOADED,
            knowledge_base_id=kb.id,
            limit=3,
            offset=offset,
        )
        actual.extend(document.id for document in page)
    assert actual == expected
    assert len(set(actual)) == len(expected)
    assert (
        await client.list_documents(
            query=suffix, knowledge_base_id=kb.id, limit=3, offset=len(expected)
        )
        == []
    )
    ids, total = await client.list_document_ids(
        query=suffix, status=FileStatus.UPLOADED, knowledge_base_id=kb.id, limit=5
    )
    assert total == len(expected)
    assert ids == expected[:5]
