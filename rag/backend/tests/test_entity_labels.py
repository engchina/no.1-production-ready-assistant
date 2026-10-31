"""実体の抽出の見出し・列挙のラベル（「原因 A」「手順 1」）の扱い（#1393）。

本文のラベルの形は、それだけでは拡張の起点・経路にしない（別名の種類 ``label``）。別の文書が同じ別
名をラベル以外の種類（台帳の「重要度: A」など）で持つときだけ使う。1 文字の部署の略号・ID・正式名
は従来どおり実体にする。評価セット（``rag/evaluation/multi-hop`` / ``business-support``）の原稿で
確かめる（LLM・Oracle は使わない）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.clients.entity_store import EntityStore
from app.clients.oracle import OracleClient
from app.config import Settings
from app.rag.chunking import Chunk
from app.rag.entity_expansion import plan_entity_expansion
from app.rag.entity_index import (
    ALIAS_KIND_LABEL,
    build_entity_index,
    is_label_shaped,
)
from app.schemas.search import RetrievedChunk
from tests.entity_layer_support import (
    CHUNK_SET_ID,
    CorpusDocument,
    InMemoryEntityStore,
    business_support_corpus,
    multi_hop_corpus,
)

KB = {"knowledge_base_id": "kb-1"}
NOTES = "file:error-e1023-notes.pdf"
INCIDENT = "file:incident-contact-rules.pdf"
LEDGERS = {"file:system-ledger.xlsx", "file:logistics-system-ledger.xlsx"}


def _all_documents() -> list[CorpusDocument]:
    return [*multi_hop_corpus(), *business_support_corpus()]


def _aliases(document_id: str) -> dict[str, dict[str, str]]:
    """文書の実体の表示名 → {別名: 別名の種類}。"""
    document = next(doc for doc in _all_documents() if doc.document_id == document_id)
    index = build_entity_index(
        document_id=document.document_id,
        chunks=document.chunks,
        chunk_set_id=CHUNK_SET_ID,
        document_title=document.chunks[0].text,
    )
    return {
        entity.display_name: {alias.alias_key: alias.alias_kind for alias in entity.aliases}
        for entity in index.entities
    }


def _store(*, outside_kb: set[str] | None = None) -> InMemoryEntityStore:
    store = InMemoryEntityStore()
    for document in _all_documents():
        kb = "kb-other" if document.document_id in (outside_kb or set()) else "kb-1"
        store.add_document(document, knowledge_base_ids=[kb])
    return store


async def _plan(
    store: InMemoryEntityStore, question: str, seeds: list[RetrievedChunk] | None = None
) -> list[Any]:
    seed_chunks = seeds or []
    return await plan_entity_expansion(
        store,
        dict(KB),
        question=question,
        seed_chunks=seed_chunks,
        exclude_chunk_ids={chunk.chunk_id for chunk in seed_chunks},
        max_chunks=6,
    )


def _ids(plan: list[Any]) -> list[str]:
    return [
        f"{item.chunk.document_id.removeprefix('file:')}#{item.chunk.chunk_id.rsplit(':', 1)[1]}"
        for item in plan
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("原因 A", True),
        ("原因A", True),
        ("原因　Ａ", True),
        ("手順 1", True),
        ("手順１２", True),
        ("案 B", True),
        ("重要度 C", True),
        ("Step 2", True),
        ("Plan B", True),
        ("HRM", False),
        ("SYS-104", False),
        ("経", False),
        ("経理部", False),
        ("経費 Portal", False),
        ("Document Portal", False),
        ("手順 123", False),
        ("重要度 AB", False),
        ("OMS 2", False),
    ],
    ids=lambda value: str(value),
)
def test_label_shape(text: str, expected: bool) -> None:
    assert is_label_shaped(text) is expected


def test_heading_labels_are_saved_as_labels_and_codes_stay_entities() -> None:
    """本文の「原因 A」「重要度 A」はラベルの別名にし、表の値・略号・ID・正式名は従来どおり。"""
    notes = _aliases(NOTES)
    incident = _aliases(INCIDENT)
    ledger = _aliases("file:system-ledger.xlsx")
    org = _aliases("file:organization-rules.pdf")

    assert notes["原因 A"] == {"原因a": ALIAS_KIND_LABEL}
    assert notes["原因 B"] == {"原因b": ALIAS_KIND_LABEL}
    assert incident["重要度 A"] == {"重要度a": ALIAS_KIND_LABEL}
    # 台帳の「重要度: A」は表の値（ラベルではない）。
    assert ledger["重要度A"] == {"重要度a": "name"}
    # 1 文字の部署の略号（表の値と定義の形）・システム ID・正式名は実体の別名のまま。
    assert ledger["経"] == {"経": "name", "担当部署経": "qualified"}
    assert org["経理部"] == {"経理部": "name", "経": "code"}
    portal = ledger["経費精算ポータル"]
    assert portal["sys-101"] == "id"
    assert portal["経費精算ポータル"] == "name"
    labels = {
        key
        for document in _all_documents()
        for aliases in _aliases(document.document_id).values()
        for key, kind in aliases.items()
        if kind == ALIAS_KIND_LABEL
    }
    # 評価セットのラベルは「重要度 A〜C」と「原因 A / B」だけ（システム・部署の名前は含まない）。
    assert labels == {"重要度a", "重要度b", "重要度c", "原因a", "原因b"}


def test_table_value_in_the_same_document_keeps_the_label_as_an_entity() -> None:
    """同じ文書の表の値が先に作った別名は、本文のラベルの形でも表の値の種類のまま。"""
    chunks = [
        Chunk(
            text="システム名: 受注システム / 重要度: A",
            index=0,
            start_offset=0,
            end_offset=0,
            metadata={"content_kind": "record"},
        ),
        Chunk(
            text="重要度 A: 30 分以内に連絡します。\n手順 1: 画面を開きます。",
            index=1,
            start_offset=0,
            end_offset=0,
            metadata={"content_kind": "text"},
        ),
    ]
    index = build_entity_index(document_id="doc", chunks=chunks, chunk_set_id="cs")
    kinds = {
        alias.alias_key: alias.alias_kind for entity in index.entities for alias in entity.aliases
    }

    assert kinds["重要度a"] == "name"
    assert kinds["手順1"] == ALIAS_KIND_LABEL


async def test_document_local_labels_do_not_expand_unrelated_questions() -> None:
    """障害メモの「原因 A」は、起点の chunk に出ても、質問で名指ししても拡張に使わない（#1393）。

    #1362 の評価（業務支援）では、関係しない質問の上位に障害メモの「対処」の章が入ると、「原因 A」の
    名寄せで「考えられる原因」の章を足していた。
    """
    store = _store()
    remedy = store.chunks[f"{NOTES}:{CHUNK_SET_ID}:3"]
    assert "原因 A の場合" in remedy.text

    unrelated = await _plan(store, "アカウントを削除するにはどうしますか？", seeds=[remedy])
    named = await _plan(store, "原因 A の対処は？", seeds=[remedy])

    assert unrelated == []
    assert named == []


async def test_labels_shared_with_a_ledger_still_bridge() -> None:
    """台帳の「重要度: A〜C」と同じ別名の、障害連絡規程の「重要度 A: …」は拡張に使う。"""
    store = _store()

    named = await _plan(
        store, "重要度 C のシステムで障害が起きたら、いつまでにどこへ報告しますか？"
    )
    hop = await _plan(store, "資産管理システムで障害が起きたときの復旧の目標はどれくらいですか？")

    assert _ids(named) == ["incident-contact-rules.pdf#1"]
    assert named[0].info["match"] == "重要度c"
    # 台帳の行（名寄せ）→ 属性の「重要度: B」→ 障害連絡規程の第 1 章（1 段）。
    assert _ids(hop)[0] == "system-ledger.xlsx#7"
    bridge = hop[_ids(hop).index("incident-contact-rules.pdf#1")]
    assert bridge.info["hop"] == 1
    assert bridge.info["match"] == "重要度b"
    assert all(item.chunk.document_id != NOTES for item in [*named, *hop])


async def test_labels_need_the_other_document_inside_the_search_scope() -> None:
    """同じ別名を持つ台帳が検索範囲の外なら、本文のラベルだけでは拡張に使わない。"""
    store = _store(outside_kb=LEDGERS)

    plan = await _plan(store, "重要度 C のシステムで障害が起きたら、いつまでにどこへ報告しますか？")

    assert plan == []


async def test_entity_store_sql_limits_labels_to_aliases_shared_across_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """3 つの SQL は、ラベルの別名を別の文書のラベル以外の別名がある範囲だけで使う。"""
    client = OracleClient(settings=Settings())
    calls: list[str] = []

    async def fake_fetch_all(
        statement: str, binds: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        calls.append(" ".join(statement.split()))
        return []

    monkeypatch.setattr(client, "_fetch_all", fake_fetch_all)
    store = EntityStore(client)

    await store.entity_seed_aliases(dict(KB), seed_chunk_ids=["c1"], question_key="原因a", limit=9)
    await store.entity_definition_chunks(dict(KB), alias_keys=["原因a"], limit=5)
    await store.entity_attribute_definition_chunks(dict(KB), source_chunk_ids=["c1"], limit=5)

    seed_sql, definition_sql, hop_sql = calls
    assert "alias.alias_kind <> 'label' OR EXISTS" in seed_sql
    assert "label_a.alias_key = alias.alias_key" in seed_sql
    for sql in (definition_sql, hop_sql):
        assert "a.alias_kind <> 'label' OR EXISTS" in sql
        assert "label_a.alias_key = a.alias_key" in sql
    for sql in calls:
        assert "label_a.alias_kind <> 'label'" in sql
        assert "label_e.document_id <> e.document_id" in sql
        # 別の文書の chunk も検索と同じ見え方の条件（有効な chunk_set・KB の範囲）で選ぶ。
        assert "JOIN rag_chunks c ON c.chunk_id = label_ec.chunk_id" in sql
        assert sql.count("rag_chunk_sets active_cs") >= 2
