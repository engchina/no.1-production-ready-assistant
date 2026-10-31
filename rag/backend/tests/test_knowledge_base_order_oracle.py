"""KB の自然順とページ境界を実 Oracle で確かめる。"""

from uuid import uuid4

import pytest

from app.clients.oracle import OracleClient
from app.schemas.knowledge_base import KnowledgeBaseStatus


@pytest.mark.usefixtures("oracle_db")
async def test_knowledge_base_list_uses_natural_name_order_before_paging() -> None:
    client = OracleClient()
    await client.ensure_default_search_answer_profile()
    default = next(item for item in await client.list_knowledge_bases() if item.name == "DEFAULT")
    suffix = uuid4().hex[:12]
    names = ["0", "2", "10", "20", "21", "１００", "a2b2", "a2b10", "a10b2", "あ", "漢字"]
    # 名前と逆の順で作成し、更新日時より名前が優先されることを確かめる。
    created = {
        name: await client.create_knowledge_base(name=f"{name}_{suffix}")
        for name in reversed(names)
    }
    await client.update_knowledge_base(created["21"].id, description="並べ替えのテスト")
    ids = [item.id for item in created.values()] + [default.id]
    expected = [default.id] + [created[name].id for name in names]
    actual: list[str] = []
    for offset in range(0, len(expected), 3):
        page = await client.list_knowledge_bases(knowledge_base_ids=ids, limit=3, offset=offset)
        actual.extend(item.id for item in page)
    assert actual == expected
    assert len(set(actual)) == len(expected)
    assert (
        await client.list_knowledge_bases(knowledge_base_ids=ids, limit=3, offset=len(expected))
        == []
    )
    archived = await client.create_knowledge_base(name=f"19_{suffix}")
    await client.archive_knowledge_base(archived.id)
    filtered = await client.list_knowledge_bases(
        status=KnowledgeBaseStatus.ACTIVE, query=suffix, limit=3, offset=2
    )
    assert [item.id for item in filtered] == [created[name].id for name in names[2:5]]
