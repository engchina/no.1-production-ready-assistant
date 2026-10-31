"""scalar / 日付 / カテゴリ pre-filter（PoweRAG 由来）の単体テスト。

`normalize_search_filters`（schema 検証）と `_oracle_retrieval_where`（Oracle の述語生成）は
いずれも純粋関数なので、実 Oracle なしで pre-filter のロジックを検証できる。
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.clients.oracle import _oracle_retrieval_where
from app.schemas.search import normalize_search_filters


def test_normalize_numeric_range_filters_parse_and_canonicalize() -> None:
    normalized = normalize_search_filters({"page_number_min": " 2 ", "page_number_max": "10"})
    assert normalized == {"page_number_min": "2", "page_number_max": "10"}


def test_normalize_rejects_non_integer_numeric_filter() -> None:
    with pytest.raises(ValueError, match="数値フィルター"):
        normalize_search_filters({"page_number_min": "abc"})


def test_normalize_rejects_negative_numeric_filter() -> None:
    with pytest.raises(ValueError, match="0 以上"):
        normalize_search_filters({"page_number_min": "-1"})


def test_normalize_rejects_inverted_numeric_range() -> None:
    with pytest.raises(ValueError, match="page_number_min"):
        normalize_search_filters({"page_number_min": "9", "page_number_max": "3"})


def test_normalize_accepts_date_only_and_datetime_filters() -> None:
    normalized = normalize_search_filters(
        {"uploaded_from": "2026-01-01", "uploaded_to": "2026-01-31T12:00:00Z"}
    )
    assert normalized == {
        "uploaded_from": "2026-01-01",
        "uploaded_to": "2026-01-31T12:00:00Z",
    }


def test_normalize_rejects_bad_date_filter() -> None:
    with pytest.raises(ValueError, match="日付フィルター"):
        normalize_search_filters({"indexed_from": "2026/01/01"})


def test_normalize_rejects_inverted_date_range() -> None:
    with pytest.raises(ValueError, match="以前"):
        normalize_search_filters({"uploaded_from": "2026-02-01", "uploaded_to": "2026-01-01"})


def test_normalize_content_kinds_dedupes_and_validates() -> None:
    normalized = normalize_search_filters({"content_kinds": "Table, figure ,table"})
    assert normalized == {"content_kinds": "table,figure"}


def test_normalize_accepts_chunk_set_id_filter() -> None:
    """実験検索用 chunk_set_id は素通し(strip のみ)で受け付ける。"""
    assert normalize_search_filters({"chunk_set_id": " cs_x "}) == {"chunk_set_id": "cs_x"}


def test_normalize_rejects_unknown_content_kind_in_list() -> None:
    with pytest.raises(ValueError, match="内容種別"):
        normalize_search_filters({"content_kinds": "table,bogus"})


def test_retrieval_where_builds_numeric_range_predicates() -> None:
    sql, binds = _oracle_retrieval_where({"page_number_min": "2", "page_number_max": "5"})
    # ページの重なり(chunk の page_start〜page_end、無ければ page_number)で判定する(#717)。
    assert "$.page_number' RETURNING NUMBER)) >= :filter_page_number_min" in sql
    assert "$.page_number' RETURNING NUMBER)) <= :filter_page_number_max" in sql
    assert binds["filter_page_number_min"] == 2
    assert binds["filter_page_number_max"] == 5


def test_retrieval_where_builds_date_range_with_end_of_day_boundary() -> None:
    sql, binds = _oracle_retrieval_where(
        {"uploaded_from": "2026-01-01", "uploaded_to": "2026-01-31"}
    )
    assert "d.uploaded_at >= :filter_uploaded_from" in sql
    assert "d.uploaded_at <= :filter_uploaded_to" in sql
    assert binds["filter_uploaded_from"] == datetime(2026, 1, 1, tzinfo=UTC)
    # date-only の `_to` は当日全体を含むよう終端へ寄せる。
    assert binds["filter_uploaded_to"] == datetime(2026, 1, 31, 23, 59, 59, 999999, tzinfo=UTC)


def test_retrieval_where_parses_zulu_datetime_as_utc() -> None:
    _, binds = _oracle_retrieval_where({"indexed_from": "2026-03-04T05:06:07Z"})
    assert binds["filter_indexed_from"] == datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)


def test_retrieval_where_builds_content_kind_in_predicate() -> None:
    sql, binds = _oracle_retrieval_where({"content_kinds": "table,figure"})
    assert "LOWER(JSON_VALUE(c.metadata_json, '$.content_kind')) IN (" in sql
    assert binds["filter_content_kind_in_0"] == "table"
    assert binds["filter_content_kind_in_1"] == "figure"


def test_retrieval_where_rejects_unknown_filter_key() -> None:
    with pytest.raises(ValueError, match="未対応の検索フィルター"):
        _oracle_retrieval_where({"totally_unknown": "x"})


def test_retrieval_where_adds_active_recipe_filter_for_kb_scope() -> None:
    """KB スコープ検索でも各レシピの active + INDEXED 出力だけを選ぶ。"""
    sql, binds = _oracle_retrieval_where({"knowledge_base_id": "kb-1"})
    assert "rag_kb_chunk_set_bindings" not in sql
    assert "active_cs.is_active = 1" in sql
    assert "active_cs.status = 'INDEXED'" in sql
    assert "active_cs.chunk_set_id = c.chunk_set_id" in sql
    assert "JOIN rag_document_recipes active_r" in sql
    assert any(name.startswith("filter_knowledge_base_id") for name in binds)


def test_retrieval_where_allows_duplicate_kb_membership_to_reuse_canonical_chunks() -> None:
    """KB 所属が duplicate 側だけでも canonical chunk を検索対象にできる。"""
    sql, _ = _oracle_retrieval_where({"knowledge_base_id": "kb-1"})
    membership = sql.split("d.document_id IN (", 1)[1]
    members, duplicates = membership.split("UNION ALL", 1)
    assert "SELECT dkb.document_id" in members
    assert "SELECT duplicate_d.duplicate_of_document_id" in duplicates
    assert "JOIN rag_documents duplicate_d" in duplicates
    assert "duplicate_d.document_id = dkb.document_id" in duplicates


def test_retrieval_where_kb_membership_is_not_correlated_per_row() -> None:
    """KB の条件は文書の集合を 1 回だけ作る形（相関の副問い合わせを OR で結ばない。#2124）。

    命中の行ごとに副問い合わせを実行すると、DB 全体の命中数 × KB の文書数に比例して遅くなる。
    所属の文書と重複の正本のどちらにも、KB の状態・権限・指定の条件を付ける。
    """
    sql, binds = _oracle_retrieval_where({"knowledge_base_id": "kb-1"})
    membership = sql.split("d.document_id IN (", 1)[1]
    assert "OR EXISTS" not in membership
    assert "= d.document_id" not in membership
    members, duplicates = membership.split("UNION ALL", 1)
    for part in (members, duplicates):
        assert "kb.status = 'ACTIVE'" in part
        assert "dkb.knowledge_base_id IN (:filter_knowledge_base_id_0)" in part
    assert binds["filter_knowledge_base_id_0"] == "kb-1"


def test_retrieval_where_reuses_canonical_only_for_duplicates_without_own_index() -> None:
    """自前の索引を持つ重複は正本を範囲に入れない。旧版の重複からは正本へ届かない(#1381)。"""
    sql, _ = _oracle_retrieval_where({"knowledge_base_id": "kb-1"})
    reuse = sql.split("JOIN rag_documents duplicate_d", 1)[1]
    assert "NOT EXISTS (" in reuse
    assert "FROM rag_chunk_sets own_cs" in reuse
    assert "own_cs.document_id = duplicate_d.document_id" in reuse
    assert "own_cs.is_active = 1" in reuse
    assert "own_cs.status = 'INDEXED'" in reuse
    assert "duplicate_d.superseded_by_document_id IS NULL" in reuse

    included, _ = _oracle_retrieval_where(
        {"knowledge_base_id": "kb-1", "include_superseded": "true"}
    )
    assert "FROM rag_chunk_sets own_cs" in included
    assert "duplicate_d.superseded_by_document_id IS NULL" not in included


def test_retrieval_where_keeps_active_recipe_filter_without_kb_scope() -> None:
    """KB 未指定でも stale chunk_set を混ぜず active レシピだけを検索する。"""
    sql, _ = _oracle_retrieval_where({})
    assert "rag_kb_chunk_set_bindings" not in sql
    assert "active_cs.is_active = 1" in sql


def test_retrieval_where_reads_all_active_recipes() -> None:
    """全 chunk_set ではなく、各レシピの active 出力だけを横断する。"""
    sql, _ = _oracle_retrieval_where({"knowledge_base_id": "kb-1"})
    assert "rag_kb_chunk_set_bindings b" not in sql
    assert "active_cs.is_active = 1" in sql
    # KB スコープ自体(所属 KB の EXISTS)は維持する。
    assert "rag_document_knowledge_bases dkb" in sql


def test_retrieval_where_rejects_removed_serving_mode_filter() -> None:
    """配信モード(serving_mode)は削除した(#1331)。検索の条件として受け付けない。"""
    with pytest.raises(ValueError, match="serving_mode"):
        _oracle_retrieval_where({"knowledge_base_id": "kb-1", "serving_mode": "single"})


def test_retrieval_where_explicit_chunk_set_filters_and_bypasses_serving() -> None:
    """実験: chunk_set_id 明示時はその chunk_set だけに絞り、is_serving 制限を外す。"""
    sql, binds = _oracle_retrieval_where({"knowledge_base_id": "kb-1", "chunk_set_id": "cs_x"})
    assert "c.chunk_set_id = :filter_chunk_set_id" in sql
    assert binds["filter_chunk_set_id"] == "cs_x"
    # 配信中以外の候補も対象にするため serving 制限(is_serving=1)を足さない。
    assert "cs.is_serving = 1" not in sql
    # KB スコープ(所属 KB の EXISTS)自体は維持する。
    assert "rag_document_knowledge_bases dkb" in sql


def _field_filter(*conditions: dict[str, str]) -> dict[str, str]:
    return {"extraction_fields": json.dumps(list(conditions), ensure_ascii=False)}


def test_normalize_extraction_field_conditions_canonicalizes_typed_values() -> None:
    """項目の条件(#549)は型ごとに値をそろえた JSON にする。"""
    normalized = normalize_search_filters(
        _field_filter(
            {"name": " 金額 ", "value_type": "number", "op": "gte", "value": "1000000.0"},
            {"name": "契約日", "value_type": "date", "op": "lte", "value": "2025-12-31"},
            {"name": "更新あり", "value_type": "bool", "value": "TRUE"},
            {"name": "契約番号", "value_type": "string", "value": " C-1 "},
        )
    )
    assert json.loads(normalized["extraction_fields"]) == [
        {"name": "金額", "value_type": "number", "op": "gte", "value": "1000000.0"},
        {"name": "契約日", "value_type": "date", "op": "lte", "value": "2025-12-31"},
        {"name": "更新あり", "value_type": "bool", "op": "eq", "value": "true"},
        {"name": "契約番号", "value_type": "string", "op": "eq", "value": "C-1"},
    ]
    assert normalize_search_filters({"extraction_fields": "[]"}) == {}


@pytest.mark.parametrize(
    ("condition", "message"),
    [
        ({"name": "契約番号", "value_type": "string", "op": "gte", "value": "C"}, "演算子"),
        ({"name": "更新あり", "value_type": "bool", "op": "lte", "value": "true"}, "演算子"),
        ({"name": "金額", "value_type": "number", "op": "gte", "value": "百万"}, "数値"),
        ({"name": "金額", "value_type": "number", "op": "gte", "value": "NaN"}, "数値"),
        ({"name": "契約日", "value_type": "date", "op": "gte", "value": "2025/01/01"}, "YYYY"),
        ({"name": "更新あり", "value_type": "bool", "value": "はい"}, "true か false"),
        ({"name": "契約日", "value_type": "date", "op": "like", "value": "2025-01-01"}, "形式"),
        ({"name": "金額", "value_type": "money", "value": "1"}, "形式"),
        ({"name": "  ", "value_type": "string", "value": "x"}, "空"),
        ({"name": "金額", "value_type": "number", "value": "1", "column": "x"}, "形式"),
    ],
    ids=[
        "string-range",
        "bool-range",
        "number-text",
        "number-nan",
        "date-format",
        "bool-text",
        "unknown-op",
        "unknown-type",
        "blank-name",
        "extra-key",
    ],
)
def test_normalize_rejects_invalid_extraction_field_conditions(
    condition: dict[str, str], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_search_filters(_field_filter(condition))


def test_normalize_rejects_malformed_or_too_many_extraction_field_conditions() -> None:
    with pytest.raises(ValueError, match="形式"):
        normalize_search_filters({"extraction_fields": "{not json"})
    too_many = [{"name": f"項目{i}", "value_type": "string", "value": "x"} for i in range(11)]
    with pytest.raises(ValueError, match="10 件"):
        normalize_search_filters(_field_filter(*too_many))


def test_retrieval_where_builds_document_exists_per_extraction_field_condition() -> None:
    """型ごとの条件は、採用中の抽出の項目を見る文書単位の EXISTS と型付きの bind になる。"""
    filters = normalize_search_filters(
        _field_filter(
            {"name": "金額", "value_type": "number", "op": "gte", "value": "1000000"},
            {"name": "契約日", "value_type": "date", "op": "lte", "value": "2025-12-31"},
            {"name": "更新あり", "value_type": "bool", "value": "true"},
            {"name": "契約番号", "value_type": "string", "value": "C-1"},
        )
    )
    sql, binds = _oracle_retrieval_where(filters)

    assert sql.count("FROM rag_document_extractions fx") == 4
    assert "fx0.document_id = d.document_id" in sql
    assert "JSON_TABLE(" in sql and "'$.fields[*]'" in sql
    # 採用中(active な chunk_set が参照する)の抽出だけを見る。
    assert "fx_cs0.extraction_recipe_id = fx0.extraction_recipe_id" in sql
    assert "fx_cs0.is_active = 1" in sql
    assert "fx_f0.field_number >= :filter_field_0_value" in sql
    assert "fx_f1.field_date <= :filter_field_1_value" in sql
    assert "LOWER(fx_f2.field_text) = :filter_field_2_value" in sql
    assert "fx_f3.field_text = :filter_field_3_value" in sql
    assert binds["filter_field_0_name"] == "金額"
    assert binds["filter_field_0_value"] == Decimal("1000000")
    assert binds["filter_field_1_value"] == date(2025, 12, 31)
    assert binds["filter_field_2_value"] == "true"
    assert binds["filter_field_3_value"] == "C-1"


def test_retrieval_where_binds_extraction_field_name_and_value_without_interpolation() -> None:
    """項目名と値は bind で渡し、SQL に埋めない(SQL injection を作らない)。"""
    injected = "x' OR 1=1 --"
    filters = normalize_search_filters(
        _field_filter({"name": injected, "value_type": "string", "value": injected})
    )
    sql, binds = _oracle_retrieval_where(filters)
    assert injected not in sql
    assert binds["filter_field_0_name"] == injected
    assert binds["filter_field_0_value"] == injected


def test_retrieval_where_rejects_unvalidated_extraction_field_operator() -> None:
    """検証を通らない演算子は SQL の組み立てでも拒否する(許可リストの外を SQL にしない)。"""
    raw = json.dumps([{"name": "a", "value_type": "number", "op": "; DROP", "value": "1"}])
    with pytest.raises(ValueError, match="形式"):
        _oracle_retrieval_where({"extraction_fields": raw})


def test_retrieval_where_chunk_set_filter_for_document_scope_experiment() -> None:
    """KB 未指定の文書スコープ実験検索でも chunk_set_id で 1 つに絞れる。"""
    sql, binds = _oracle_retrieval_where({"document_id": "doc-1", "chunk_set_id": "cs_y"})
    assert "c.chunk_set_id = :filter_chunk_set_id" in sql
    assert binds["filter_chunk_set_id"] == "cs_y"
    assert binds["filter_document_id"] == "doc-1"


def test_page_ranges_filter_is_normalized_and_becomes_an_or_of_page_overlaps() -> None:
    """章節のページ範囲(#717)。どれかの範囲に重なる chunk に絞る(OR)。"""
    from app.clients.oracle import _oracle_retrieval_where

    normalized = normalize_search_filters(
        {
            "page_ranges": '[{"document_id":"doc-a","page_start":3,"page_end":5},'
            '{"document_id":"doc-b"}]'
        }
    )
    assert normalized["page_ranges"] == (
        '[{"document_id":"doc-a","page_start":3,"page_end":5},'
        '{"document_id":"doc-b","page_start":null,"page_end":null}]'
    )
    sql, binds = _oracle_retrieval_where(normalized)
    assert "d.document_id = :filter_pr_doc_0" in sql
    assert " OR (d.document_id = :filter_pr_doc_1)" in sql
    assert binds["filter_pr_doc_0"] == "doc-a"
    assert binds["filter_pr_end_0"] == 5
    assert binds["filter_pr_start_0"] == 3
    # ページの重なりは page_start〜page_end(無ければ page_number)で見る。
    assert "'$.page_start' RETURNING NUMBER" in sql
    assert "'$.page_end' RETURNING NUMBER" in sql


def test_page_ranges_filter_carries_sheet_row_ranges_apart_from_pages() -> None:
    """表計算のシートの行の範囲(#2663)。行の番号を頁として絞らない。"""
    from app.clients.oracle import _oracle_retrieval_where

    normalized = normalize_search_filters(
        {
            "page_ranges": '[{"document_id":"doc-s","sheet_name":"シートA","row_start":42,'
            '"row_end":45},{"document_id":"doc-s","sheet_name":"シートB"},'
            '{"document_id":"doc-a","page_start":3,"page_end":5}]'
        }
    )
    sql, binds = _oracle_retrieval_where(normalized)
    assert binds["filter_pr_sheet_0"] == "シートA"
    assert (binds["filter_pr_row_start_0"], binds["filter_pr_row_end_0"]) == (42, 45)
    assert "filter_pr_start_0" not in binds and "filter_pr_end_0" not in binds
    # 行の無いシートの範囲はシートの全体。
    assert binds["filter_pr_sheet_1"] == "シートB"
    assert "filter_pr_row_start_1" not in binds
    # 頁の範囲は今までどおり頁で絞る。
    assert (binds["filter_pr_start_2"], binds["filter_pr_end_2"]) == (3, 5)
    assert "JSON_VALUE(c.metadata_json, '$.sheet_name') = :filter_pr_sheet_0" in sql
    assert "'$.row_start' RETURNING NUMBER" in sql
    # 頁の範囲だけの値は、従来の形で書く。
    assert normalized["page_ranges"].endswith(
        '{"document_id":"doc-a","page_start":3,"page_end":5}]'
    )


@pytest.mark.parametrize(
    "item",
    [
        {"document_id": "a", "row_start": 3},
        {"document_id": "a", "sheet_name": "シートA", "page_start": 3},
        {"document_id": "a", "sheet_name": "シートA", "row_start": 5, "row_end": 2},
    ],
    ids=["rows-without-sheet", "sheet-and-page", "reversed-rows"],
)
def test_sheet_row_ranges_are_validated(item: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        normalize_search_filters({"page_ranges": json.dumps([item])})


def test_page_number_filters_use_chunk_page_ranges() -> None:
    """page_number を持たない分割の chunk も、page_start / page_end で絞れる(#717)。"""
    from app.clients.oracle import _oracle_retrieval_where

    sql, binds = _oracle_retrieval_where({"page_number_min": "2", "page_number_max": "4"})
    assert "'$.page_end' RETURNING NUMBER" in sql
    assert "COALESCE(JSON_VALUE(c.metadata_json, '$.page_start' RETURNING NUMBER)" in sql
    assert binds["filter_page_number_min"] == 2


@pytest.mark.parametrize(
    "value",
    ["{}", "[1]", '[{"document_id":"a","page_start":5,"page_end":2}]', "not json"],
    ids=["object", "not-range", "reversed", "invalid-json"],
)
def test_invalid_page_ranges_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_search_filters({"page_ranges": value})
