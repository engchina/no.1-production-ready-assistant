"""実体の層（#1362）: 抽出・名寄せ・検索の 1 段の拡張・表の定義・取込と検索のつなぎ。

評価セット（``rag/evaluation/multi-hop``）の資料で、橋渡しの段（台帳の行・組織規程の略号の表）が 1
段の拡張で足されることを、実体の表を Python で持つ store
（``entity_layer_support.InMemoryEntityStore``。SQL と同じ join の規則）で確かめる。実 Oracle の
SQL は ``test_entity_layer_oracle.py``。
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from app.config import Settings
from app.rag.chunking import Chunk
from app.rag.entity_expansion import (
    ENTITY_EXPANSION_KEY,
    ENTITY_EXPANSION_ROLE,
    plan_entity_expansion,
)
from app.rag.entity_index import (
    EntityIndexOptions,
    build_entity_index,
    document_scope_label,
    entity_key,
    record_fields,
)
from app.rag.evaluation_handling import contains_normalized
from app.schemas.search import RetrievedChunk
from tests.entity_layer_support import (
    CHUNK_SET_ID,
    MULTI_HOP_DIR,
    CorpusDocument,
    InMemoryEntityStore,
    corpus_store,
    multi_hop_corpus,
    retrieved,
)

KB = {"knowledge_base_id": "kb-1"}
LEDGER = "file:system-ledger.xlsx"
ORG = "file:organization-rules.pdf"
LOGISTICS_LEDGER = "file:logistics-system-ledger.xlsx"
LOGISTICS_ORG = "file:logistics-organization-rules.pdf"


def _document(name: str) -> CorpusDocument:
    return next(doc for doc in multi_hop_corpus() if doc.document_id == f"file:{name}")


def _entities(name: str, **kwargs: Any) -> dict[str, Any]:
    document = _document(name)
    index = build_entity_index(
        document_id=document.document_id,
        chunks=document.chunks,
        chunk_set_id=CHUNK_SET_ID,
        document_title=document.chunks[0].text,
        **kwargs,
    )
    links: dict[str, list[tuple[int, str, str | None]]] = {}
    for link in index.links:
        links.setdefault(link.entity_id, []).append(
            (int(link.chunk_id.rsplit(":", 1)[1]), link.chunk_role, link.attribute_name)
        )
    return {
        entity.display_name: {
            "type": entity.entity_type,
            "scope": entity.scope_label,
            "aliases": [alias.alias_key for alias in entity.aliases],
            "links": sorted(links.get(entity.entity_id, [])),
        }
        for entity in index.entities
    }


async def _plan(
    store: InMemoryEntityStore,
    question: str,
    *,
    seeds: list[RetrievedChunk] | None = None,
    max_chunks: int = 6,
    filters: dict[str, str] | None = None,
) -> list[Any]:
    seed_chunks = seeds or []
    return await plan_entity_expansion(
        store,
        dict(KB if filters is None else filters),
        question=question,
        seed_chunks=seed_chunks,
        exclude_chunk_ids={chunk.chunk_id for chunk in seed_chunks},
        max_chunks=max_chunks,
    )


def _ids(plan: list[Any]) -> list[str]:
    return [
        f"{item.chunk.document_id.removeprefix('file:')}#{item.chunk.chunk_id.rsplit(':', 1)[1]}"
        for item in plan
    ]


# ---- 名寄せ ----


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("ＨＲＭ", "HRM"),
        ("ＳＹＳ－１０４", "SYS-104"),
        ("経費 Portal", "経費portal"),
        ("Document Portal", "documentportal"),
        ("ｼｽﾃﾑ", "システム"),
    ],
)
def test_entity_key_folds_width_case_and_spaces(left: str, right: str) -> None:
    assert entity_key(left) == entity_key(right)


def test_full_and_half_width_names_become_the_same_alias() -> None:
    """台帳の「HRM」「SYS-104」と、保守計画・障害連絡規程の「ＨＲＭ」「ＳＹＳ－１０４」は同じ別名。"""
    ledger = _entities("system-ledger.xlsx")
    plan = _entities("maintenance-plan.pdf")
    incident = _entities("incident-contact-rules.pdf")

    assert "hrm" in ledger["人事評価システム"]["aliases"]
    assert "sys-104" in ledger["受発注管理システム"]["aliases"]
    assert plan["HRM"]["aliases"] == ["hrm"]
    assert plan["HRM"]["links"] == [(1, "definition", None)]
    assert incident["SYS-104"]["aliases"] == ["sys-104"]


def test_alias_column_values_are_aliases_of_the_row_entity() -> None:
    """略称・別表記の列（「経費 Portal」「OMS、受発注」）は、その行の実体の別名になる。"""
    ledger = _entities("system-ledger.xlsx")

    assert ledger["経費精算ポータル"]["aliases"] == ["経費精算ポータル", "sys-101", "経費portal"]
    assert ledger["経費精算ポータル"]["links"] == [(1, "definition", None)]
    assert ledger["受発注管理システム"]["aliases"] == [
        "受発注管理システム",
        "sys-104",
        "oms",
        "受発注",
    ]
    # カナ / 英字の別表記（「ドキュメントポータル」と「Document Portal」）。
    assert "documentportal" in ledger["ドキュメントポータル"]["aliases"]
    assert ledger["経費精算ポータル"]["type"] == "record"
    assert ledger["経費精算ポータル"]["scope"] == "サンプル社"


def test_single_character_codes_are_kept_from_table_values_and_definitions() -> None:
    """1 文字の部署の略号（「経」「総」）は、台帳の列の値と組織規程の定義から実体になる。"""
    ledger = _entities("system-ledger.xlsx")
    org = _entities("organization-rules.pdf")

    assert ledger["経"]["aliases"] == ["経", "担当部署経"]
    assert ledger["経"]["type"] == "value"
    assert all(role == "attribute" for _, role, _ in ledger["経"]["links"])
    assert {name for _, _, name in ledger["経"]["links"]} == {"担当部署"}
    assert "総" in ledger
    # 重要度の「A」のような 1 文字の英数字は、列名つきの別名だけで実体にする。
    assert ledger["重要度A"]["aliases"] == ["重要度a"]
    assert org["経理部"]["aliases"] == ["経理部", "経"]
    assert org["総務部"]["aliases"] == ["総務部", "総"]
    # 定義の行（第 2 章）と、同じ文書で定義した名前で始まる行（第 3 章の承認者）。
    assert org["経理部"]["links"] == [(2, "definition", None), (3, "definition", None)]
    assert org["情報システム部"]["aliases"] == ["情報システム部", "情", "情シス"]


def test_single_character_is_not_matched_inside_other_words() -> None:
    """本文の「経費」「総務」の中の 1 文字は、言及の実体にしない（言及は 2 文字以上）。"""
    chunk = Chunk(
        text="経費の申請と総務の手続きは、経理部に確認します。",
        index=0,
        start_offset=0,
        end_offset=0,
        metadata={"content_kind": "text"},
    )
    index = build_entity_index(document_id="doc", chunks=[chunk], chunk_set_id="cs")

    keys = {alias.alias_key for entity in index.entities for alias in entity.aliases}
    assert keys == {"経理部"}
    assert all(len(key) >= 2 for key in keys)


def test_record_fields_keeps_slashes_inside_values() -> None:
    assert record_fields("システムID: SYS-1 / 正式名: A / B 連携 / 担当部署: 経") == [
        ("システムID", "SYS-1"),
        ("正式名", "A / B 連携"),
        ("担当部署", "経"),
    ]


def test_recipe_columns_choose_names_and_attributes() -> None:
    """レシピの列名の指定（entity_name_columns / entity_attribute_columns）で列の役割を決める。"""
    entities = _entities(
        "system-ledger.xlsx",
        options=EntityIndexOptions(name_columns=("正式名",), attribute_columns=("担当部署",)),
    )

    assert entities["経費精算ポータル"]["aliases"] == ["経費精算ポータル"]
    assert "経" in entities
    assert "重要度A" not in entities
    assert "社外秘" not in entities


def test_scope_label_comes_from_title_and_preamble() -> None:
    assert document_scope_label(["サンプル社のシステム台帳（架空の会社。評価用の合成資料）"]) == (
        "サンプル社"
    )
    assert document_scope_label(["サンプル物流社 組織規程", "対象: サンプル社の子会社"]) == (
        "サンプル物流社"
    )
    assert document_scope_label(["架空の会社の資料", "子会社"]) is None


# ---- 検索の 1 段の拡張 ----


async def test_question_entity_resolves_ledger_row_then_one_hop_to_department() -> None:
    """質問のシステム名 → 台帳の行（名寄せ）→ 担当部署の略号「経」→ 組織規程の第 2 章・第 3 章（1
    段）。
    """
    store = corpus_store()

    plan = await _plan(store, "経費精算ポータルの変更の申請は、誰が承認しますか？")

    assert _ids(plan)[:3] == [
        "system-ledger.xlsx#1",
        "organization-rules.pdf#2",
        "organization-rules.pdf#3",
    ]
    row, code, approver = plan[:3]
    assert row.info["hop"] == 0
    assert row.info["seed"] == "question"
    assert row.info["entity"] == "経費精算ポータル"
    assert code.info["hop"] == 1
    assert code.info["match"] == "経"
    assert code.info["entity"] == "経理部"
    assert code.info["from_chunk_id"] == row.chunk.chunk_id
    assert "略号「経」: 経理部" in code.chunk.text
    assert "経理部の承認者は管理本部長です" in approver.chunk.text
    # 別の会社（サンプル物流社）の「経」（経理課）は足さない。
    assert all(item.chunk.document_id != LOGISTICS_ORG for item in plan)
    assert all(item.info["ambiguous"] is False for item in plan)


async def test_expansion_is_one_hop_only() -> None:
    """1 段で足した chunk の実体はたどらない（2 段以上の再帰はしない）。"""
    store = InMemoryEntityStore()

    def document(document_id: str, rows: list[str]) -> CorpusDocument:
        return CorpusDocument(
            document_id,
            f"{document_id}.xlsx",
            [
                Chunk(
                    text=text,
                    index=index,
                    start_offset=0,
                    end_offset=0,
                    metadata={"content_kind": "record"},
                )
                for index, text in enumerate(rows)
            ],
        )

    store.add_document(document("systems", ["システム名: 受注システム / 担当部署: 営"]))
    # 部署の表: 略号「営」の行の属性「本部: 東」→ さらに「東」を定義する表（2 段目）。
    store.add_document(document("departments", ["部署コード: 営 / 部署名: 営業部 / 本部: 東"]))
    store.add_document(document("regions", ["本部コード: 東 / 本部名: 東日本本部"]))

    plan = await _plan(store, "受注システムの担当部署は？")

    assert [item.chunk.document_id for item in plan] == ["systems", "departments"]
    assert [item.info["hop"] for item in plan] == [0, 1]


async def test_expansion_respects_the_chunk_budget() -> None:
    store = corpus_store()

    plan = await _plan(store, "経費精算ポータルと勤怠管理システムの承認者は？", max_chunks=2)

    assert _ids(plan) == ["system-ledger.xlsx#1", "system-ledger.xlsx#2"]


async def test_expansion_stays_inside_the_knowledge_base_scope() -> None:
    """検索範囲の外（別のナレッジベース）の組織規程は、名寄せの途中にも足す chunk にも使わない。"""
    store = InMemoryEntityStore()
    for document in multi_hop_corpus():
        kb = "kb-other" if document.document_id == ORG else "kb-1"
        store.add_document(document, knowledge_base_ids=[kb])

    plan = await _plan(store, "経費精算ポータルの承認者は？")
    outside = await _plan(
        store, "経費精算ポータルの承認者は？", filters={"knowledge_base_id": "kb-x"}
    )

    assert _ids(plan)[0] == "system-ledger.xlsx#1"
    assert all(item.chunk.document_id != ORG for item in plan)
    assert outside == []


async def test_documents_without_the_recipe_have_no_expansion() -> None:
    """実体の抽出を選ばない文書（既定のレシピ）だけの検索範囲では、何も足さない。"""
    store = InMemoryEntityStore()
    for document in multi_hop_corpus():
        store.add_document(document, index_entities=False)

    assert await _plan(store, "経費精算ポータルの承認者は？") == []


async def test_seed_chunks_bridge_to_rows_and_codes() -> None:
    """上位の chunk（保守計画の「ＨＲＭ: 毎月第 2 土曜日」）の実体から台帳の行、その行の属性へ。"""
    store = corpus_store()
    plan_chunk = store.chunks[f"file:maintenance-plan.pdf:{CHUNK_SET_ID}:1"]

    plan = await _plan(store, "毎月第 2 土曜日に保守するシステムの機密区分は？", seeds=[plan_chunk])

    ids = _ids(plan)
    # 「ＨＲＭ」を定義する chunk（台帳の行と、旧版の保守計画の同じ行）を先に足す。
    assert set(ids[:2]) == {"system-ledger.xlsx#3", "maintenance-plan-2025.pdf#1"}
    row = plan[ids.index("system-ledger.xlsx#3")]
    assert row.info["seed"] == "chunk"
    assert "SYS-103" in row.chunk.text
    # 起点の chunk は足さない（すでに候補の上位にある）。
    assert "maintenance-plan.pdf#1" not in ids


async def test_colliding_aliases_across_companies_are_kept_and_marked() -> None:
    """「OMS」はサンプル社と物流社の両方にある。質問で決められなければ両方を足して印を付ける。"""
    store = corpus_store()

    both = await _plan(store, "ＯＭＳ の担当部署の承認者は？")
    logistics = await _plan(store, "サンプル物流社の OMS の担当部署は？")

    rows = {item.chunk.document_id: item for item in both if item.info["hop"] == 0}
    assert {LEDGER, LOGISTICS_LEDGER} <= set(rows)
    assert rows[LEDGER].info["ambiguous"] is True
    assert rows[LOGISTICS_LEDGER].info["ambiguous"] is True
    assert {item.chunk.document_id for item in logistics} <= {
        LOGISTICS_LEDGER,
        LOGISTICS_ORG,
        "file:incident-contact-rules.pdf",
    }
    assert _ids(logistics)[:3] == [
        "logistics-system-ledger.xlsx#1",
        "logistics-organization-rules.pdf#2",
        "logistics-organization-rules.pdf#3",
    ]
    assert all(item.info["ambiguous"] is False for item in logistics)


async def test_short_aliases_in_other_words_come_after_named_entities() -> None:
    """質問の「受付から」の「受付」（受付管理システムの略称）は、名指しした実体の後に回す。"""
    store = corpus_store()

    plan = await _plan(store, "経費精算ポータルの変更は受付から何営業日以内に承認されますか？")

    ids = _ids(plan)
    assert ids.index("system-ledger.xlsx#67") > ids.index("organization-rules.pdf#3")


def _bridge_cases() -> list[dict[str, Any]]:
    cases = json.loads((MULTI_HOP_DIR / "multi-hop.json").read_text(encoding="utf-8"))["cases"]
    return [case for case in cases if case["reasoning_type"] == "bridge"]


_BRIDGE_DOCUMENTS = {LEDGER, ORG, LOGISTICS_LEDGER, LOGISTICS_ORG}
# 実体の層では足さない根拠（組織規程の第 4 章の代理の規則は、実体ではなく同じ文書の中の参照でつな
# がる。#1280 の交差参照の範囲）。
_NOT_ENTITY_BRIDGES = {"org-proxy", "org-proxy-exec", "org-proxy-direct"}


async def test_multi_hop_bridge_steps_enter_with_one_hop() -> None:
    """評価セットの bridge の全問で、台帳の行・組織規程の略号と承認者の根拠が 1 段の拡張で足される。

    上位の候補（起点）には、台帳・組織規程以外の必要な根拠（承認規程・保守計画など、質問の語で検索
    に当たる資料）の chunk を置く（#1335 の再評価で検索に出た根拠）。足す件数は既定の 6 件。
    """
    store = corpus_store()
    total = covered = 0
    missing: list[str] = []
    for case in _bridge_cases():
        seeds: list[RetrievedChunk] = []
        for evidence in case["required_evidence"]:
            if evidence["document_id"] in _BRIDGE_DOCUMENTS:
                continue
            seeds.extend(
                chunk
                for chunk in store.chunks.values()
                if chunk.document_id == evidence["document_id"]
                and contains_normalized(chunk.text, evidence["text"])
                and chunk not in seeds
            )
        plan = await _plan(store, case["query"], seeds=seeds)
        for evidence in case["required_evidence"]:
            if evidence["document_id"] not in _BRIDGE_DOCUMENTS:
                continue
            if evidence["id"] in _NOT_ENTITY_BRIDGES:
                continue
            total += 1
            if any(
                item.chunk.document_id == evidence["document_id"]
                and contains_normalized(item.chunk.text, evidence["text"])
                for item in plan
            ):
                covered += 1
            else:
                missing.append(f"{case['id']}:{evidence['id']}")

    # 台帳の行（ledger-1xx）と組織規程の略号・承認者（org-code-* / org-appr-*）。
    assert total >= 60
    # 取れないのは、運用要領の連携元を 2 段たどる 1 件だけ（br-wms-link-department の受発注管理）。
    assert missing == ["br-wms-link-department:ledger-104"]
    assert covered == total - 1


async def test_single_hop_cases_add_nothing_harmful_when_no_entity_named() -> None:
    """実体を名指ししない 1 段の対照の質問（承認の記録の保管）では、台帳・組織規程を足さない。"""
    store = corpus_store()

    plan = await _plan(store, "承認の記録は何年間保管しますか？")

    assert plan == []


# ---- metadata ----


def test_expansion_metadata_marks_role_reason() -> None:
    from app.rag.entity_expansion import expansion_metadata

    chunk = retrieved(_document("system-ledger.xlsx"), _document("system-ledger.xlsx").chunks[1])
    marked = expansion_metadata(chunk, {"entity": "経費精算ポータル", "hop": 0, "x": [1]})

    assert marked.metadata[ENTITY_EXPANSION_KEY] == {"entity": "経費精算ポータル", "hop": 0}
    assert ENTITY_EXPANSION_KEY not in chunk.metadata
    assert ENTITY_EXPANSION_ROLE == "entity_expansion"


def test_mcp_orders_entity_expansion_with_ranked_evidence() -> None:
    """MCP の根拠の並びで、実体からたどった根拠は検索で当たった根拠と同じく順位の順（#1348）。"""
    from app.mcp.tools import mcp_evidence_order

    def chunk(chunk_id: str, role: str, rank: int | None) -> RetrievedChunk:
        metadata: dict[str, Any] = {"evidence_role": role}
        if rank is not None:
            metadata["evidence_retrieval_rank"] = rank
        return RetrievedChunk(
            document_id="d", chunk_id=chunk_id, text=chunk_id, score=0.0, metadata=metadata
        )

    ordered = mcp_evidence_order(
        [
            chunk("context", "neighbor_context", None),
            chunk("anchor-2", "retrieved_anchor", 2),
            chunk("entity-4", ENTITY_EXPANSION_ROLE, 4),
            chunk("anchor-1", "retrieved_anchor", 1),
        ]
    )

    assert [item.chunk_id for item in ordered] == ["anchor-1", "anchor-2", "entity-4", "context"]


# ---- 設定・レシピ ----


def test_settings_default_off_for_recipe_and_expansion(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "RAG_ENTITY_EXPANSION_ENABLED",
        "RAG_SCREEN_LINKING_ENABLED",
        "RAG_REQUEST_COVERAGE_RETRIEVAL_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None)
    # 索引の構築は既定 OFF。検索時の拡張とは別に設定する。
    assert settings.rag_entity_index_enabled is False
    assert settings.rag_chunk_context_header_enabled is True
    assert settings.rag_entity_name_columns == []
    assert settings.rag_entity_attribute_columns == []
    # 任意の検索拡張は明示的に有効化した場合だけ使う。
    assert settings.rag_entity_expansion_enabled is False
    assert settings.rag_screen_linking_enabled is False
    assert settings.rag_request_coverage_retrieval_enabled is False
    assert settings.rag_entity_expansion_max_chunks == 6
    with pytest.raises(ValueError):
        Settings(rag_entity_expansion_max_chunks=0)


@pytest.mark.parametrize(
    "field", ["entity_expansion", "screen_linking", "request_coverage_retrieval"]
)
def test_optional_search_expansion_accepts_explicit_environment_enable(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.setenv(f"RAG_{field.upper()}_ENABLED", "true")
    settings = Settings(_env_file=None)
    assert getattr(settings, f"rag_{field}_enabled") is True


def test_entity_expansion_uses_global_environment_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """拡張の開閉と上限は検索方法の全体設定を保存した環境変数から読む。"""
    from app.config import PROFILE_ONLY_SETTING_FIELDS

    assert {
        "rag_entity_expansion_enabled",
        "rag_entity_expansion_max_chunks",
    }.isdisjoint(PROFILE_ONLY_SETTING_FIELDS)
    monkeypatch.setenv("RAG_ENTITY_EXPANSION_ENABLED", "false")
    monkeypatch.setenv("RAG_ENTITY_EXPANSION_MAX_CHUNKS", "3")
    # 同じ source のほかの項目は今までどおり読む。
    monkeypatch.setenv("RAG_ENTITY_INDEX_ENABLED", "false")
    env_file = tmp_path / "backend.env"
    env_file.write_text(
        "RAG_ENTITY_EXPANSION_ENABLED=false\nRAG_ENTITY_EXPANSION_MAX_CHUNKS=2\n"
        "RAG_REFERENCE_EXPANSION_MAX_CHUNKS=7\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.rag_entity_expansion_enabled is False
    assert settings.rag_entity_expansion_max_chunks == 3
    assert settings.rag_entity_index_enabled is False
    assert settings.rag_reference_expansion_max_chunks == 7
    # 属性名での生成（テスト・評価の上書き）は受け付ける。
    assert Settings(rag_entity_expansion_enabled=False).rag_entity_expansion_enabled is False


def test_search_answer_profile_ignores_legacy_entity_expansion() -> None:
    """保存済みの個別設定は適用せず、全体の名称・別名の拡張を使う。"""
    from app.rag.search_answer_profile_config import (
        parse_search_answer_profile_config,
        resolve_search_answer_profile_settings,
    )

    base = Settings(rag_entity_expansion_enabled=True)
    selected, applied = resolve_search_answer_profile_settings(
        base,
        parse_search_answer_profile_config(
            {
                "knowledge_base_ids": ["kb-1"],
                "query": {"entity_expansion_enabled": False, "entity_expansion_max_chunks": 3},
            }
        ),
    )
    assert applied is False
    assert selected.rag_entity_expansion_enabled is True
    assert selected.rag_entity_expansion_max_chunks == 6
    # 全体の設定は変えない（ほかのプロファイルの検索には効かない）。
    assert base.rag_entity_expansion_enabled is True
    # プロファイルの値に関係なく、明示的に有効化した全体の設定を使う。
    for query in ({}, {"entity_expansion_enabled": None}, {"entity_expansion_enabled": True}):
        other, _ = resolve_search_answer_profile_settings(
            base,
            parse_search_answer_profile_config({"knowledge_base_ids": ["kb-2"], "query": query}),
        )
        assert other.rag_entity_expansion_enabled is True
        assert other.rag_entity_expansion_max_chunks == 6
    # 上限の範囲はコードの検証で持つ（1〜20）。範囲の外の保存値は読まない（空の設定へ縮退）。
    broken = parse_search_answer_profile_config(
        {"knowledge_base_ids": ["kb-1"], "query": {"entity_expansion_max_chunks": 21}}
    )
    assert broken.query.entity_expansion_max_chunks is None


def test_recipe_option_maps_to_ingestion_settings() -> None:
    from app.api.routes import documents as documents_route
    from app.schemas.document import DocumentProcessingConfig

    config = DocumentProcessingConfig.model_validate(
        {"entity_index_enabled": True, "entity_attribute_columns": [" 担当部署 ", "重要度"]}
    )
    effective, _ = documents_route._merge_document_processing_config(config)

    assert effective.rag_entity_index_enabled is True
    assert effective.rag_entity_attribute_columns == ["担当部署", "重要度"]
    assert effective.rag_entity_name_columns == []
    groups = documents_route.DOCUMENT_PROCESSING_OUTPUT_GROUPS
    assert groups["entity_index_enabled"] == (
        "entity_index_enabled",
        "entity_name_columns",
        "entity_attribute_columns",
    )
    with pytest.raises(ValueError):
        DocumentProcessingConfig.model_validate({"entity_name_columns": [""]})


# ---- 表の定義 ----


def test_schema_defines_cascading_foreign_keys_and_indexes() -> None:
    """文書・chunk の削除と再索引で、実体・別名・関連が残らない（ON DELETE CASCADE）。

    chunk_set には FK を張らない（取込は chunk を保存した後で chunk_set の行を作る経路がある）。
    """
    from app.rag.oracle_schema import oracle_schema_migration_sections, oracle_schema_sections
    from app.rag.system_schema import (
        MANAGED_FOREIGN_KEYS,
        MANAGED_INDEXES,
        MANAGED_TABLES,
    )

    assert {"RAG_ENTITIES", "RAG_ENTITY_ALIASES", "RAG_ENTITY_CHUNKS"} <= set(MANAGED_TABLES)
    assert {
        "RAG_ENTITIES_CHUNK_SET_IDX",
        "RAG_ENTITIES_DOCUMENT_IDX",
        "RAG_ENTITY_ALIASES_KEY_IDX",
        "RAG_ENTITY_CHUNKS_CHUNK_IDX",
    } <= set(MANAGED_INDEXES)
    cascades = {
        (spec.table_name, spec.referenced_table_name)
        for spec in MANAGED_FOREIGN_KEYS
        if spec.table_name.startswith("RAG_ENTIT") and spec.delete_rule == "CASCADE"
    }
    assert cascades == {
        ("RAG_ENTITIES", "RAG_DOCUMENTS"),
        ("RAG_ENTITY_ALIASES", "RAG_ENTITIES"),
        ("RAG_ENTITY_CHUNKS", "RAG_ENTITIES"),
        ("RAG_ENTITY_CHUNKS", "RAG_CHUNKS"),
    }
    names = [section.name for section in oracle_schema_sections()]
    assert names.index("entities") > names.index("chunk_sets")
    migration = next(
        section
        for section in oracle_schema_migration_sections()
        if section.name == "20261009_001_entity_layer"
    )
    assert migration.destructive is False
    for name in ("RAG_ENTITIES", "RAG_ENTITY_ALIASES", "RAG_ENTITY_CHUNKS"):
        assert f"table_name = '{name}'" in migration.sql
    assert "DELETE FROM" not in migration.sql and "DROP " not in migration.sql
    for table in ("rag_entities", "rag_entity_aliases", "rag_entity_chunks"):
        assert not table.upper().startswith(("PLATFORM_", "NL2SQL_", "AGENT_"))


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, object]] = []

    def cursor(self) -> _RecordingConnection:
        return self

    def execute(self, statement: str, parameters: object = None) -> None:
        self.statements.append((" ".join(statement.split()), parameters))

    def executemany(self, statement: str, parameters: object) -> None:
        self.statements.append((" ".join(statement.split()), parameters))

    def setinputsizes(self, **kwargs: object) -> None:
        return None

    def close(self) -> None:
        return None


class _TransactionOracle:
    def __init__(self) -> None:
        self.connection = _RecordingConnection()

    async def _run_transaction(self, operation: Any) -> Any:
        return operation(self.connection)


async def test_replace_entity_index_deletes_the_chunk_set_rows_first() -> None:
    """再索引は chunk_set の実体を消してから入れる（別名と関連は FK の CASCADE で一緒に消える）。"""
    from app.clients.entity_store import EntityStore

    document = _document("organization-rules.pdf")
    index = build_entity_index(
        document_id=document.document_id, chunks=document.chunks, chunk_set_id="cs-9"
    )
    oracle = _TransactionOracle()

    await EntityStore(cast(Any, oracle)).replace_chunk_set_entity_index(
        document.document_id, "cs-9", index
    )

    statements = [statement for statement, _ in oracle.connection.statements]
    assert statements[0] == "DELETE FROM rag_entities WHERE chunk_set_id = :chunk_set_id"
    # 古い chunk_set の、関連の無い実体（同じ文書）も消す。
    assert statements[1].startswith("DELETE FROM rag_entities e WHERE e.document_id = :document_id")
    assert "NOT EXISTS" in statements[1]
    assert statements[2].startswith("INSERT INTO rag_entities")
    assert statements[3].startswith("INSERT INTO rag_entity_aliases")
    assert statements[4].startswith("INSERT INTO rag_entity_chunks")
    entity_rows = cast(list[dict[str, object]], oracle.connection.statements[2][1])
    assert {row["chunk_set_id"] for row in entity_rows} == {"cs-9"}
    link_rows = cast(list[dict[str, object]], oracle.connection.statements[4][1])
    assert all(
        str(row["chunk_id"]).startswith(f"{document.document_id}:cs-9:") for row in link_rows
    )


async def test_entity_store_queries_use_the_search_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """3 つの SQL は検索と同じ見え方の条件（KB の範囲・権限・有効な chunk_set）で chunk を join す
    る。
    """
    from app.clients.entity_store import EntityStore
    from app.clients.oracle import OracleClient

    client = OracleClient(settings=Settings())
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_fetch_all(
        statement: str, binds: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        calls.append((" ".join(statement.split()), dict(binds or {})))
        return []

    monkeypatch.setattr(client, "_fetch_all", fake_fetch_all)
    store = EntityStore(client)

    assert (
        await store.entity_seed_aliases(
            dict(KB), seed_chunk_ids=["c1", "c2"], question_key="経費精算ポータル", limit=9
        )
        == []
    )
    assert await store.entity_definition_chunks(dict(KB), alias_keys=["経", "hrm"], limit=5) == []
    assert (
        await store.entity_attribute_definition_chunks(dict(KB), source_chunk_ids=["c1"], limit=5)
        == []
    )
    assert await store.entity_definition_chunks(dict(KB), alias_keys=[], limit=5) == []

    seed_sql, seed_binds = calls[0]
    assert "kb-1" in seed_binds.values()
    assert "ec.chunk_role IN ('definition', 'mention')" in seed_sql
    assert "LENGTH(qa.alias_key) >= :entity_min_alias_chars" in seed_sql
    assert seed_binds["entity_question_key"] == "経費精算ポータル"
    assert seed_binds["entity_seed_0"] == "c1"
    assert "rag_chunk_sets active_cs" in seed_sql
    definition_sql, definition_binds = calls[1]
    assert "ec.chunk_role = 'definition'" in definition_sql
    assert "JOIN rag_chunks c ON c.chunk_id = ec.chunk_id" in definition_sql
    assert "superseded_by_document_id IS NULL" in definition_sql
    assert definition_binds["entity_alias_0"] == "経"
    hop_sql, hop_binds = calls[2]
    assert "src.chunk_role = 'attribute'" in hop_sql
    assert "a.alias_key = src_a.alias_key AND a.entity_id <> src.entity_id" in hop_sql
    assert hop_binds["entity_source_0"] == "c1"
    assert "kb-1" in hop_binds.values()
    assert len(calls) == 3


# ---- 取込 ----


class _IngestOracle:
    def __init__(self) -> None:
        self.saved: list[str] = []
        self.transactions = 0

    async def save_index(self, document_id: str, *args: Any, **kwargs: Any) -> None:
        self.saved.append(document_id)

    async def _run_transaction(self, operation: Any) -> Any:
        self.transactions += 1
        return operation(_RecordingConnection())


async def _save_index(settings: Settings) -> _IngestOracle:
    from app.rag.ingestion import IngestionPipeline
    from app.schemas.extraction import StructuredExtraction

    document = _document("organization-rules.pdf")
    oracle = _IngestOracle()
    pipeline = IngestionPipeline(
        vlm=cast(Any, object()),
        genai=cast(Any, object()),
        oracle=cast(Any, oracle),
        object_storage=cast(Any, object()),
        settings=settings,
    )
    await pipeline._save_index(
        "trace-1362",
        document.document_id,
        StructuredExtraction(raw_text="x"),
        document.chunks,
        [[0.0]] * len(document.chunks),
        chunk_set_id="cs-1",
    )
    return oracle


@pytest.mark.parametrize("explicit", [False, True])
async def test_recipe_without_entity_index_does_not_touch_the_entity_tables(
    explicit: bool,
) -> None:
    """未指定・明示 OFF はいずれも実体の表に触らない（SQL も実行しない）。"""
    settings = (
        Settings(rag_graph_profile="off", rag_entity_index_enabled=False)
        if explicit
        else Settings(rag_graph_profile="off")
    )
    oracle = await _save_index(settings)

    assert oracle.saved == [ORG]
    assert oracle.transactions == 0


async def test_enabled_recipe_saves_the_entity_index_after_the_index() -> None:
    """構築を選んだときだけ、列の指定が無くても索引を保存する。"""
    oracle = await _save_index(Settings(rag_graph_profile="off", rag_entity_index_enabled=True))

    assert oracle.saved == [ORG]
    assert oracle.transactions == 1


# ---- 回答の検索（AnswerEngine）----


def _engine(store: InMemoryEntityStore, hits: list[RetrievedChunk], **settings: Any) -> Any:
    from app.rag.answer_engine import AnswerEngine
    from tests.test_cross_references import FakeGenAi, ReferenceOracle

    return AnswerEngine(
        Settings(**{"rag_entity_expansion_enabled": True, **settings}),
        oracle=cast(Any, ReferenceOracle(hits, [])),
        genai=cast(Any, FakeGenAi()),
        entity_store=store,
    )


QUESTION = "経費精算ポータルの変更の申請は、誰が承認し、受付から何営業日以内に承認されますか？"


def _approval_hits(store: InMemoryEntityStore) -> list[RetrievedChunk]:
    approval = store.chunk_ids("file:approval-rules.pdf")
    procedure = store.chunk_ids("file:change-procedure.pdf")
    return [store.chunks[chunk_id] for chunk_id in [approval[2], procedure[2], approval[4]]]


async def test_search_inserts_entity_expansion_after_top_candidates() -> None:
    from app.rag.answer_engine import _SearchState
    from app.schemas.search import SearchRequest

    store = corpus_store()
    hits = _approval_hits(store)
    state = _SearchState()

    result = await _engine(store, hits)._search(
        SearchRequest(query=QUESTION, filters={"knowledge_base_id": "kb-1", "file_name": "承認"}),
        state,
        retrieval_queries=[QUESTION],
    )

    order = [child.chunk_uid for child in result.child_chunks]
    expanded = list(state.entity_expansions)
    # 上位の 3 件の後ろに、台帳の行と組織規程（略号の表・承認者）を足す。
    assert order[:3] == [hit.chunk_id for hit in hits]
    assert order[3 : 3 + len(expanded)] == expanded
    assert expanded[:3] == [
        f"{LEDGER}:{CHUNK_SET_ID}:1",
        f"{ORG}:{CHUNK_SET_ID}:2",
        f"{ORG}:{CHUNK_SET_ID}:3",
    ]
    child = result.child_chunks[3]
    assert child.metadata["entity_expansion"]["entity"] == "経費精算ポータル"
    marker = state.chunks[expanded[1]].metadata[ENTITY_EXPANSION_KEY]
    assert isinstance(marker, dict) and marker["match"] == "経"
    assert store.calls[:3] == ["seed", "definition", "hop"]


async def test_search_caches_the_plan_and_skips_when_disabled_or_search_only() -> None:
    from app.rag.answer_engine import _SearchState
    from app.schemas.search import SearchRequest

    store = corpus_store()
    hits = _approval_hits(store)
    request = SearchRequest(query=QUESTION, filters={"knowledge_base_id": "kb-1"})
    engine = _engine(store, hits)
    state = _SearchState()

    await engine._search(request, state, retrieval_queries=[QUESTION])
    await engine._search(request, state, retrieval_queries=[QUESTION, "言い換え"])
    # 同じ起点（質問と上位の候補）なら、CRAG の各回で実体の表を読み直さない。
    assert store.calls == ["seed", "definition", "hop"]

    disabled = await _engine(store, hits, rag_entity_expansion_enabled=False)._search(
        request, _SearchState(), retrieval_queries=[QUESTION]
    )
    assert [child.chunk_uid for child in disabled.child_chunks] == [hit.chunk_id for hit in hits]
    # 検索だけ（KB の検索テスト・レシピの検索比較）は上位 top_k 件の候補なので足さない。
    retrieved_only = await engine.retrieve(request.model_copy(update={"top_k": 3}))
    assert [chunk.chunk_id for chunk in retrieved_only] == [hit.chunk_id for hit in hits]


async def test_search_continues_when_the_entity_tables_cannot_be_read() -> None:
    from app.rag.answer_engine import _SearchState
    from app.schemas.search import SearchRequest

    class Broken(InMemoryEntityStore):
        async def entity_seed_aliases(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("ORA-00942")

    store = corpus_store()
    hits = _approval_hits(store)
    state = _SearchState()

    result = await _engine(Broken(chunks=store.chunks), hits)._search(
        SearchRequest(query=QUESTION), state, retrieval_queries=[QUESTION]
    )

    assert [child.chunk_uid for child in result.child_chunks] == [hit.chunk_id for hit in hits]
    assert state.entity_expansions == {}


class _EngineResult:
    """rag_engine の回答の結果（``_outcome_from_result`` が読む属性だけ）。"""

    def __init__(self, children: list[dict[str, Any]]) -> None:
        self.evidence_items = [{"chunk_id": "p", "children": children}]
        self.generation_trace: dict[str, Any] = {}
        self.execution_steps: list[Any] = []
        self.generated_queries: list[str] = []
        self.text_search_tokens: list[str] = []
        self.crag_attempts: list[Any] = []
        self.envelope: dict[str, Any] = {}
        self.external_data_items: list[Any] = []
        self.question_type: list[str] = []
        self.answer_text = "回答"

    def __getattr__(self, name: str) -> Any:
        return None


def test_outcome_marks_entity_expansion_role_for_mcp_and_answer() -> None:
    from app.rag.answer_engine import _outcome_from_result, _SearchState
    from app.rag.entity_expansion import expansion_metadata

    store = corpus_store()
    hit = _approval_hits(store)[0]
    row = store.chunks[f"{LEDGER}:{CHUNK_SET_ID}:1"]
    state = _SearchState()
    state.chunks = {hit.chunk_id: hit, row.chunk_id: expansion_metadata(row, {"entity": "x"})}

    outcome = _outcome_from_result(
        _EngineResult(
            [
                {
                    "chunk_id": hit.chunk_id,
                    "retrieval_role": "retrieved_anchor",
                    "retrieval_rank": 1,
                },
                {
                    "chunk_id": row.chunk_id,
                    "retrieval_role": "retrieved_anchor",
                    "retrieval_rank": 4,
                },
            ]
        ),
        state,
    )

    roles = {chunk.chunk_id: chunk.metadata["evidence_role"] for chunk in outcome.citations}
    assert roles == {hit.chunk_id: "retrieved_anchor", row.chunk_id: ENTITY_EXPANSION_ROLE}


# ---- MCP の根拠の並びで、実体の拡張の根拠を evidence_limit の内に入れる（#1362。D の holdout）


def _mcp_chunk(
    chunk_id: str, role: str, rank: int | None = None, *, expansion: bool = False
) -> RetrievedChunk:
    metadata: dict[str, Any] = {"evidence_role": role}
    if rank is not None:
        metadata["evidence_retrieval_rank"] = rank
    if expansion:
        metadata[ENTITY_EXPANSION_KEY] = {"entity": chunk_id, "hop": 1}
    return RetrievedChunk(
        document_id="d", chunk_id=chunk_id, text=chunk_id, score=0.0, metadata=metadata
    )


def _ids_of(chunks: list[RetrievedChunk]) -> list[str]:
    return [chunk.chunk_id for chunk in chunks]


def test_mcp_order_reserves_limit_for_entity_expansion_after_many_hits() -> None:
    """評価の D の holdout の形: 当たった chunk が 20 件を超え、拡張の根拠（略号の表など）が親の
    文脈の役割で 21〜38 位にある。evidence_limit 20 の内の末尾（上限の 3 割まで）に入れる。
    """
    from app.mcp.tools import mcp_evidence_order

    hits = [_mcp_chunk(f"hit-{n}", "retrieved_anchor", n) for n in range(1, 23)]
    context = [_mcp_chunk(f"ctx-{n}", "neighbor_context") for n in range(10)]
    expanded = [
        _mcp_chunk("ledger-row", ENTITY_EXPANSION_ROLE, 30, expansion=True),
        _mcp_chunk("org-code", "neighbor_context", expansion=True),
        _mcp_chunk("org-approver", "parent_context", expansion=True),
    ]
    citations = [*context[:4], *hits, *expanded[1:], *context[4:], expanded[0]]

    ordered = mcp_evidence_order(citations, 20)

    assert _ids_of(ordered[:20]) == [
        *(f"hit-{n}" for n in range(1, 18)),
        "ledger-row",
        "org-code",
        "org-approver",
    ]
    # 押し出した当たりは上限の後ろに残り、前後の文脈はその後。
    assert _ids_of(ordered[20:25]) == ["hit-18", "hit-19", "hit-20", "hit-21", "hit-22"]
    assert len(ordered) == len(citations)
    # 上限を渡さない（従来の）並びでは、拡張の文脈の根拠は前後の文脈の中に残る。
    plain = _ids_of(mcp_evidence_order(citations))
    assert plain.index("org-code") > 20


def test_mcp_order_puts_entity_expansion_before_context_when_hits_are_few() -> None:
    from app.mcp.tools import mcp_evidence_order

    hits = [_mcp_chunk(f"hit-{n}", "retrieved_anchor", n) for n in range(1, 6)]
    context = [_mcp_chunk(f"ctx-{n}", "neighbor_context") for n in range(30)]
    expanded = _mcp_chunk("org-code", "neighbor_context", expansion=True)

    ordered = mcp_evidence_order([*hits, *context, expanded], 20)

    assert _ids_of(ordered[:7]) == [
        "hit-1",
        "hit-2",
        "hit-3",
        "hit-4",
        "hit-5",
        "org-code",
        "ctx-0",
    ]


def test_mcp_order_caps_the_reserved_share_and_keeps_expansion_already_inside() -> None:
    from app.mcp.tools import ENTITY_EXPANSION_LIMIT_SHARE, mcp_evidence_order

    hits = [_mcp_chunk(f"hit-{n}", "retrieved_anchor", n) for n in range(1, 25)]
    inside = _mcp_chunk("inside", ENTITY_EXPANSION_ROLE, 2, expansion=True)
    outside = [_mcp_chunk(f"exp-{n}", "neighbor_context", expansion=True) for n in range(10)]

    ordered = mcp_evidence_order([*hits, inside, *outside], 20)

    head = _ids_of(ordered[:20])
    # 上限 20 の 3 割 = 6 件まで（上限の内に元からある 1 件を含む）。
    assert ENTITY_EXPANSION_LIMIT_SHARE == 0.3
    assert [cid for cid in head if cid.startswith(("exp-", "inside"))] == [
        "inside",
        "exp-0",
        "exp-1",
        "exp-2",
        "exp-3",
        "exp-4",
    ]
    assert head.index("inside") == 2
    # 拡張の根拠が無い・上限に収まるときは従来どおり。
    assert _ids_of(mcp_evidence_order(hits, 20)) == _ids_of(hits)
    # 同じ順位の当たりがあれば、citations の順（当たりが先）。
    assert _ids_of(mcp_evidence_order([*hits[:3], inside], 20)) == [
        "hit-1",
        "hit-2",
        "inside",
        "hit-3",
    ]


def test_mcp_order_also_reserves_context_in_the_same_parent_as_entity_expansion() -> None:
    """拡張の根拠（第 3 章の承認者）と同じ親のかたまりの前後の文脈（第 2 章の略号の表）も、上限の
    内に入れる。別の親の前後の文脈は動かさない。
    """
    from app.mcp.tools import mcp_evidence_order

    def grouped(chunk: RetrievedChunk, group: str) -> RetrievedChunk:
        return chunk.model_copy(update={"metadata": {**chunk.metadata, "chunk_group_id": group}})

    hits = [_mcp_chunk(f"hit-{n}", "retrieved_anchor", n) for n in range(1, 23)]
    approver = grouped(_mcp_chunk("org-approver", ENTITY_EXPANSION_ROLE, 25, expansion=True), "org")
    code_table = grouped(_mcp_chunk("org-code", "neighbor_context"), "org")
    other = grouped(_mcp_chunk("other-ctx", "neighbor_context"), "other")

    ordered = mcp_evidence_order([*hits, other, code_table, approver], 20)

    assert _ids_of(ordered[:20])[-2:] == ["org-approver", "org-code"]
    assert "other-ctx" not in _ids_of(ordered[:20])


# ---- 検索・回答プロファイルの案内（実体の索引を持つ文書の数。#1388）----


async def test_count_entity_documents_uses_the_retrieval_scope() -> None:
    from app.clients.entity_store import EntityStore

    class CountingOracle:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        async def _fetch_all(self, sql: str, binds: dict[str, object]) -> list[dict[str, object]]:
            self.calls.append((sql, binds))
            return [{"document_count": 2}]

    oracle = CountingOracle()
    store = EntityStore(cast(Any, oracle))

    assert await store.count_entity_documents([]) == 0
    assert await store.count_entity_documents([" ", ""]) == 0
    assert oracle.calls == []
    assert await store.count_entity_documents(["kb-1", "kb-2", "kb-1"]) == 2
    sql, binds = oracle.calls[0]
    assert "COUNT(DISTINCT c.document_id)" in sql
    assert "rag_entity_chunks" in sql
    # 回答の検索と同じ見え方（有効な chunk_set・索引済みの文書・ナレッジベース）で数える。
    assert "d.status = 'INDEXED'" in sql
    assert "active_cs.is_active = 1" in sql
    assert sorted(str(v) for k, v in binds.items() if k.startswith("filter_knowledge")) == [
        "kb-1",
        "kb-2",
    ]


def test_entity_index_coverage_api(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.clients import entity_store
    from app.main import app
    from app.security.permissions import ROUTE_PERMISSIONS
    from tests.support import AsgiTestClient

    seen: list[list[str]] = []

    async def count(self: Any, knowledge_base_ids: list[str]) -> int:
        seen.append(list(knowledge_base_ids))
        return 0

    monkeypatch.setattr(entity_store.EntityStore, "count_entity_documents", count)
    client = AsgiTestClient(app)

    response = client.post(
        "/api/search-answer-profiles/entity-index-coverage",
        json={"knowledge_base_ids": ["kb-1", "kb-2"]},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"document_count": 0}
    assert seen == [["kb-1", "kb-2"]]
    assert ("POST", "/search-answer-profiles/entity-index-coverage") in ROUTE_PERMISSIONS
    too_many = client.post(
        "/api/search-answer-profiles/entity-index-coverage",
        json={"knowledge_base_ids": [f"kb-{index}" for index in range(201)]},
    )
    assert too_many.status_code == 422
