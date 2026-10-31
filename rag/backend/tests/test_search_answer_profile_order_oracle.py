"""検索・回答プロファイルの自然順とページ境界を実 Oracle で確かめる。"""

from uuid import uuid4

import pytest

from app.clients.oracle import OracleClient
from app.schemas.search_answer_profile import SearchAnswerProfileStatus


@pytest.mark.usefixtures("oracle_db")
async def test_profile_list_uses_natural_name_order_before_paging() -> None:
    client = OracleClient()
    default = await client.ensure_default_search_answer_profile()
    suffix = uuid4().hex[:12]
    # 作成順と名前順を違わせ、直近に更新した名前が先頭に来ないことも確かめる。
    names = [
        "0",
        "2",
        "10",
        "20",
        "21",
        "１００",
        "a2b2",
        "a2b10",
        "a10b2",
        "あ",
        "漢字",
    ]
    created = [
        await client.create_search_answer_profile(
            name=f"{name}_{suffix}", description="並べ替えのテスト"
        )
        for name in names
    ]
    ids = [item.id for item in reversed(created)] + [default.id]
    expected = [default.id] + [item.id for item in created]
    actual: list[str] = []
    for offset in range(0, len(expected), 3):
        page = await client.list_search_answer_profiles(
            search_answer_profile_ids=ids, limit=3, offset=offset
        )
        actual.extend(item.id for item in page)
    assert actual == expected
    assert len(set(actual)) == len(expected)
    assert (
        await client.list_search_answer_profiles(
            search_answer_profile_ids=ids, limit=3, offset=len(expected)
        )
        == []
    )
    # 検索・状態の絞り込みでも全体の名前順を保つ。
    archived = await client.create_search_answer_profile(
        name=f"19_{suffix}", description="並べ替えのテスト"
    )
    await client.archive_search_answer_profile(archived.id)
    filtered = await client.list_search_answer_profiles(
        status=SearchAnswerProfileStatus.ACTIVE, query=suffix, limit=3, offset=2
    )
    assert [item.id for item in filtered] == [item.id for item in created[2:5]]
