"""実 Oracle AI Database で、実行履歴の並べ替えがページをまたいで同じ順になるか（#1764・#1777）。

通常 CI では実行しない。`NL2SQL_RUN_ORACLE_INTEGRATION=1` の明示指定時だけ、
一意な業務プロファイルの ID を付けた一時の履歴の文書を作り、質問・実行日時の昇順・降順で
2 件ずつのページ（keyset と offset。#1891）を最後までたどって確かめ、finally で必ず消す。
LLM は呼ばない。資格情報は出力しない。
質問は日本語の辞書順（JAPANESE_M）で並ぶ。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from dotenv import dotenv_values

from app.features.nl2sql.incremental_store import (
    HISTORY_QUESTION_SORT_PREFIX_CHARS,
    OracleIncrementalNl2SqlRepository,
)
from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.nl2sql.service import history_visibility_filters
from app.settings import get_settings

pytestmark = pytest.mark.skipif(
    os.getenv("NL2SQL_RUN_ORACLE_INTEGRATION") != "1",
    reason="NL2SQL_RUN_ORACLE_INTEGRATION=1 の明示指定が必要です。",
)


@pytest.fixture
def live_settings() -> Iterator[Any]:
    """conftest が PLATFORM_ORACLE_USER=APP を強制するため、共通 .env の接続ユーザーへ戻す。"""
    settings = get_settings()
    env_user = str(
        dotenv_values(Path(__file__).resolve().parents[3] / "platform" / ".env").get(
            "PLATFORM_ORACLE_USER"
        )
        or os.environ.get("PLATFORM_ORACLE_USER", "")
    )
    original_user = settings.oracle_user
    if env_user:
        settings.oracle_user = env_user
    try:
        yield settings
    finally:
        settings.oracle_user = original_user


def _pages(
    repository: OracleIncrementalNl2SqlRepository,
    *,
    profile_id: str,
    sort_key: str,
    direction: str,
) -> list[str]:
    ids: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        documents, cursor, total = repository.list_documents_page(
            "history",
            cursor=cursor,
            limit=2,
            profile_id=profile_id,
            sort_key=sort_key,
            direction=direction,
        )
        assert total == len(_ROWS)
        ids.extend(str(document["id"]) for document in documents)
        if not cursor:
            return ids
    raise AssertionError("ページが終わらない")


def _offset_pages(
    repository: OracleIncrementalNl2SqlRepository,
    *,
    profile_id: str,
    sort_key: str,
    direction: str,
) -> list[str]:
    """画面の一覧と同じ offset のページ（OFFSET … FETCH NEXT。#1891）を順に読む。"""
    ids: list[str] = []
    for offset in range(0, len(_ROWS) + 2, 2):
        documents, next_cursor, total = repository.list_documents_page(
            "history",
            cursor=None,
            offset=offset,
            limit=2,
            profile_id=profile_id,
            sort_key=sort_key,
            direction=direction,
        )
        assert next_cursor is None
        assert total == len(_ROWS)
        ids.extend(str(document["id"]) for document in documents)
    return ids


_LONG_PREFIX = "長" * (HISTORY_QUESTION_SORT_PREFIX_CHARS + 50)
# 質問の文と実行日時の順を変えておく。binary（文字コード）の順では、カタカナ（アイス）が
# ひらがなの後、「顧客」（U+9867）が「注文」（U+6CE8）の後になる。日本語の辞書順では、
# 英字 → かな（ひらがなとカタカナは同じ音で並び、濁音は清音の後）→ 漢字（JIS X 0208 の順。
# 顧 → 注）。同じ質問（顧客の確認）と、先頭の比べる文字数が同じ長い質問（注文 長…）は
# ENTITY_ID の順で決まる。
_ROWS = [
    ("h1", "顧客の確認", "2026-10-01T00:00:01+00:00"),
    ("h2", "abc の売上", "2026-10-01T00:00:02+00:00"),
    ("h3", f"注文 {_LONG_PREFIX} 1", "2026-10-01T00:00:03+00:00"),
    ("h4", "顧客の確認", "2026-10-01T00:00:04+00:00"),
    ("h5", "がっこうの数", "2026-10-01T00:00:05+00:00"),
    ("h6", f"注文 {_LONG_PREFIX} 2", "2026-10-01T00:00:06+00:00"),
    ("h7", "かきの在庫", "2026-10-01T00:00:07+00:00"),
    ("h8", "アイスの売上", "2026-10-01T00:00:08+00:00"),
]
# memory の store の同じ確認は tests/test_nl2sql_history_paging.py の
# test_memory_history_sort_matches_the_oracle_japanese_order（同じ質問・同じ期待の順）。
_QUESTION_ASC = ["h2", "h8", "h7", "h5", "h1", "h4", "h3", "h6"]


def test_history_sort_pages_through_all_rows_in_order_on_oracle(live_settings: Any) -> None:
    adapter = OracleNl2SqlAdapter(live_settings)
    repository = OracleIncrementalNl2SqlRepository(connection_factory=adapter.state_connection)
    tag = uuid.uuid4().hex[:10]
    profile_id = f"t1777_{tag}"
    entity_ids = {key: f"t1777_{tag}_{key}" for key, _question, _created in _ROWS}
    try:
        for key, question, created_at in _ROWS:
            repository.put_document(
                "history",
                entity_ids[key],
                {"id": key, "question": question, "created_at": created_at},
                profile_id=profile_id,
                status="unrated",
            )

        assert (
            _pages(repository, profile_id=profile_id, sort_key="question", direction="asc")
            == _QUESTION_ASC
        )
        assert _pages(
            repository, profile_id=profile_id, sort_key="question", direction="desc"
        ) == list(reversed(_QUESTION_ASC))
        created_asc = [key for key, _question, _created in _ROWS]
        assert (
            _pages(repository, profile_id=profile_id, sort_key="created_at", direction="asc")
            == created_asc
        )
        assert _pages(
            repository, profile_id=profile_id, sort_key="created_at", direction="desc"
        ) == list(reversed(created_asc))
        # offset のページも keyset と同じ順で、抜け・重なりが無い（末尾を越えたページは空）。
        for sort_key, expected in (("question", _QUESTION_ASC), ("created_at", created_asc)):
            assert (
                _offset_pages(repository, profile_id=profile_id, sort_key=sort_key, direction="asc")
                == expected
            )
            assert _offset_pages(
                repository, profile_id=profile_id, sort_key=sort_key, direction="desc"
            ) == list(reversed(expected))
        assert repository.count_documents("history", profile_id=profile_id) == len(_ROWS)
        assert repository.count_documents("history", profile_id=profile_id, query="顧客") == 2
    finally:
        for entity_id in entity_ids.values():
            repository.delete_document("history", entity_id)


# 実行した人と会話の持ち主（#1954）。v3 は会話の無い実行（持ち主の key が無い）、v5 は実行した
# 人の記録が無い行。
_RUN_ROWS = [
    ("v1", "alice", "alice"),
    ("v2", "admin", "alice"),
    ("v3", "admin", None),
    ("v4", "bob", "bob"),
    ("v5", None, None),
]


def test_history_visibility_and_executor_filter_on_oracle(live_settings: Any) -> None:
    """実行履歴の見え方と「実行者」の絞り込みの SQL（OR・除外）を実 Oracle で確かめる（#1954）。"""
    adapter = OracleNl2SqlAdapter(live_settings)
    repository = OracleIncrementalNl2SqlRepository(connection_factory=adapter.state_connection)
    tag = uuid.uuid4().hex[:10]
    profile_id = f"t1954_{tag}"
    entity_ids = {key: f"t1954_{tag}_{key}" for key, _actor, _owner in _RUN_ROWS}

    def visible(viewer: str, *, all_users: bool, executor: str = "all") -> tuple[list[str], int]:
        filters, any_filters, exclude_filters = history_visibility_filters(
            viewer_user_uuid=viewer,
            all_users=all_users,
            executor=executor,  # type: ignore[arg-type]
        )
        documents, _cursor, total = repository.list_documents_page(
            "history",
            cursor=None,
            offset=0,
            limit=10,
            profile_id=profile_id,
            payload_filters=filters or None,
            payload_any_filters=any_filters or None,
            payload_exclude_filters=exclude_filters or None,
            sort_key="created_at",
            direction="asc",
        )
        count = repository.count_documents(
            "history",
            profile_id=profile_id,
            payload_filters=filters or None,
            payload_any_filters=any_filters or None,
            payload_exclude_filters=exclude_filters or None,
        )
        assert count == total
        return [str(document["id"]) for document in documents], total

    try:
        for index, (key, actor, owner) in enumerate(_RUN_ROWS, start=1):
            payload: dict[str, Any] = {
                "id": key,
                "question": f"質問 {key}",
                "created_at": f"2026-10-15T00:00:0{index}+00:00",
            }
            if actor is not None:
                payload["actor_user_uuid"] = actor
            if owner is not None:
                payload["conversation_owner_user_uuid"] = owner
            repository.put_document(
                "history", entity_ids[key], payload, profile_id=profile_id, status="unrated"
            )

        assert visible("alice", all_users=False) == (["v1", "v2"], 2)
        assert visible("alice", all_users=False, executor="mine") == (["v1"], 1)
        assert visible("alice", all_users=False, executor="others") == (["v2"], 1)
        assert visible("bob", all_users=False) == (["v4"], 1)
        assert visible("carol", all_users=False) == ([], 0)
        assert visible("admin", all_users=True) == (["v1", "v2", "v3", "v4", "v5"], 5)
        assert visible("admin", all_users=True, executor="mine") == (["v2", "v3"], 2)
        assert visible("admin", all_users=True, executor="others") == (["v1", "v4", "v5"], 3)

        # 種類（会話の持ち主の有無）と期間（実行日時の下限）の SQL（#1955）。
        def ranged(**conditions: Any) -> tuple[list[str], int]:
            documents, _cursor, total = repository.list_documents_page(
                "history",
                cursor=None,
                offset=0,
                limit=10,
                profile_id=profile_id,
                sort_key="created_at",
                direction="asc",
                **conditions,
            )
            assert repository.count_documents("history", profile_id=profile_id, **conditions) == (
                total
            )
            return [str(document["id"]) for document in documents], total

        chat = {"conversation_owner_user_uuid": True}
        query = {"conversation_owner_user_uuid": False}
        assert ranged(payload_present_filters=chat) == (["v1", "v2", "v4"], 3)
        assert ranged(payload_present_filters=query) == (["v3", "v5"], 2)
        assert ranged(created_since="2026-10-15T00:00:03+00:00") == (["v3", "v4", "v5"], 3)
        assert ranged(payload_present_filters=chat, created_since="2026-10-15T00:00:03+00:00") == (
            ["v4"],
            1,
        )
    finally:
        for entity_id in entity_ids.values():
            repository.delete_document("history", entity_id)
