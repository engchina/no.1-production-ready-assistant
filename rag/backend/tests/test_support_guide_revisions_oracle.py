"""業務ガイドの公開の履歴の上限（#2356）を、実 Oracle AI Database で確かめる。

公開・前の版に戻すと同じ transaction で、直近の 5 版
（``RAG_SUPPORT_GUIDE_MAX_PUBLISHED_REVISIONS``）を超えた古い版を消し、版の番号は振り直さない。
未到達なら oracle_db fixture が skip する。作ったガイドはテストの中で削除する（版は FK の
ON DELETE CASCADE で消える）。
"""

from __future__ import annotations

import pytest

from app.clients.oracle import OracleClient
from app.clients.support_guide_store import SupportGuideNotFoundError, SupportGuideStore
from app.schemas.support_guide import SupportGuideContent
from tests import _oracle_test_db


def _content(title: str) -> SupportGuideContent:
    return SupportGuideContent.model_validate(
        {
            "title": title,
            "goal": {"expected_result": "結果 A になる", "intent_examples": ["手順 A を知りたい"]},
            "steps": [{"id": "s1", "title": "手順 1"}],
        }
    )


def _delete_guide(guide_id: str) -> None:
    with _oracle_test_db._schema_connection() as connection:
        cursor = connection.cursor()
        cursor.execute("DELETE FROM rag_support_guides WHERE guide_id = :g", {"g": guide_id})
        connection.commit()


@pytest.mark.usefixtures("oracle_db")
async def test_publish_keeps_only_latest_revisions_on_real_oracle() -> None:
    oracle = OracleClient()
    profile = await oracle.ensure_default_search_answer_profile()
    store = SupportGuideStore(oracle)
    guide_id = await store.create_guide(profile.id, _content("ガイド A"), user=None)
    try:
        for index in range(1, 7):
            revision = await store.publish(
                profile.id, guide_id, _content(f"ガイド A の版 {index}"), base_revision=1, user=None
            )
            assert revision == index
        revisions = [item.revision for item in await store.list_revisions(guide_id)]
        assert revisions == [6, 5, 4, 3, 2]
        with pytest.raises(SupportGuideNotFoundError):
            await store.get_revision(guide_id, 1)

        # 前の版に戻すのも新しい版の公開。戻した元の版の番号は記録に残り、古い版から消す。
        target = await store.get_revision(guide_id, 2)
        rolled = await store.publish(
            profile.id, guide_id, target.content, base_revision=1, user=None, rollback_from=2
        )
        assert rolled == 7
        summary, _ = await store.get_guide(profile.id, guide_id)
        assert summary.published_revision == 7
        listed = await store.list_revisions(guide_id)
        assert [item.revision for item in listed] == [7, 6, 5, 4, 3]
        assert listed[0].rollback_from == 2
    finally:
        _delete_guide(guide_id)


def _embedding_rows(guide_id: str) -> int:
    with _oracle_test_db._schema_connection() as connection:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM rag_support_guide_embeddings WHERE guide_id = :g",
            {"g": guide_id},
        )
        row = cursor.fetchone()
    return int(row[0])


@pytest.mark.usefixtures("oracle_db")
async def test_intent_embeddings_follow_the_published_revision_on_real_oracle() -> None:
    """質問の例の埋め込み（#2398）は公開中の版のものだけを読み、版を消すと一緒に消える。"""
    oracle = OracleClient()
    profile = await oracle.ensure_default_search_answer_profile()
    store = SupportGuideStore(oracle)
    guide_id = await store.create_guide(profile.id, _content("ガイド B"), user=None)
    try:
        first = await store.publish(
            profile.id, guide_id, _content("ガイド B の版 1"), base_revision=1, user=None
        )
        vectors = [[0.25] * 1024]
        await store.save_intent_embeddings(
            guide_id,
            first,
            model_id="model-a",
            dimension=1024,
            examples_sha256="a" * 64,
            vectors=vectors,
        )
        stored = (await store.published_intent_embeddings(profile.id))[guide_id]
        assert (stored.revision, stored.model_id, stored.dimension) == (first, "model-a", 1024)
        assert stored.vectors == ((0.25,) * 1024,)

        # 同じ版に保存し直すと置き換える。
        await store.save_intent_embeddings(
            guide_id,
            first,
            model_id="model-b",
            dimension=1024,
            examples_sha256="b" * 64,
            vectors=vectors,
        )
        replaced = await store.published_intent_embeddings(profile.id)
        assert replaced[guide_id].model_id == "model-b"

        # 新しい版を公開すると、前の版の埋め込みは読まない（作り直す対象になる）。
        for index in range(2, 7):
            await store.publish(
                profile.id, guide_id, _content(f"ガイド B の版 {index}"), base_revision=1, user=None
            )
        assert guide_id not in await store.published_intent_embeddings(profile.id)
        # 履歴の上限で版 1 が消えると、その版の埋め込みも FK の ON DELETE CASCADE で消える。
        assert _embedding_rows(guide_id) == 0
    finally:
        _delete_guide(guide_id)
