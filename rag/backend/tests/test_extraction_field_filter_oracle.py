"""項目抽出の値による検索の絞り込み(#549)の実 Oracle 統合テスト。

`_extraction_field_where` が作る文書単位の EXISTS(JSON_TABLE と型変換・bind)が実 Oracle で
期待どおりに文書を絞ることを確かめる。未到達なら oracle_db fixture が skip し、作成行は
cleanup_to_baseline で後始末する。
"""

import json
from uuid import uuid4

import pytest

from app.clients.oracle import (
    OracleClient,
    OracleConnectionProtocol,
    _execute,
    _extraction_field_where,
    _oracle_in_predicate,
)
from app.schemas.extraction import ExtractionField, StructuredExtraction
from app.schemas.search import normalize_search_filters


async def _document_with_fields(
    client: OracleClient, fields: list[ExtractionField], *, active: bool = True
) -> str:
    """抽出の項目を持つ文書を作り、その抽出を参照する chunk_set を(既定で)採用中にする。"""
    detail = await client.create_document(
        file_name="contract.txt",
        object_storage_path="oci://bucket/contract.txt",
        content_type="text/plain",
    )
    extraction_recipe_id = f"er_{uuid4().hex[:16]}"
    chunk_set_id = f"cs_{uuid4().hex[:16]}"
    await client.upsert_document_extraction_artifact(
        document_id=detail.id,
        extraction_recipe_id=extraction_recipe_id,
        source_sha256="a" * 64,
        recipe_subset={"parser": "docling"},
        extraction=StructuredExtraction(raw_text="本文", fields=fields).to_document_payload(),
        status="materialized",
    )
    await client.upsert_chunk_set(
        chunk_set_id=chunk_set_id,
        document_id=detail.id,
        extraction_recipe_id=extraction_recipe_id,
        status="INDEXED",
    )

    def activate(connection: OracleConnectionProtocol) -> None:
        _execute(
            connection,
            "UPDATE rag_chunk_sets SET is_active = :is_active WHERE chunk_set_id = :chunk_set_id",
            {"is_active": 1 if active else 0, "chunk_set_id": chunk_set_id},
        )

    await client._run_transaction(activate)
    return detail.id


async def _matching(client: OracleClient, document_ids: list[str], *conditions: object) -> set[str]:
    filters = normalize_search_filters({"extraction_fields": json.dumps(list(conditions))})
    clauses, binds = _extraction_field_where(filters["extraction_fields"])
    in_sql, in_binds = _oracle_in_predicate("d.document_id", "doc", document_ids)
    rows = await client._fetch_all(
        f"SELECT d.document_id FROM rag_documents d WHERE {in_sql} AND {' AND '.join(clauses)}",
        {**binds, **in_binds},
    )
    return {str(row["document_id"]) for row in rows}


@pytest.mark.usefixtures("oracle_db")
async def test_extraction_field_conditions_filter_documents_on_real_oracle() -> None:
    client = OracleClient()
    large = await _document_with_fields(
        client,
        [
            ExtractionField(name="金額", value="1500000", value_type="number"),
            ExtractionField(name="契約日", value="2025-04-01", value_type="date"),
            ExtractionField(name="自動更新", value="true", value_type="bool"),
            ExtractionField(name="契約番号", value="C-1", value_type="string"),
        ],
    )
    small = await _document_with_fields(
        client,
        [
            ExtractionField(name="金額", value="500000", value_type="number"),
            ExtractionField(name="契約日", value="2024-01-15", value_type="date"),
            ExtractionField(name="自動更新", value="false", value_type="bool"),
        ],
    )
    # 寄せられなかった値(数値にならない)と、項目の無い文書は一致しない。
    unparsed = await _document_with_fields(
        client, [ExtractionField(name="金額", value="約100万円", value_type="number")]
    )
    no_fields = await _document_with_fields(client, [])
    # 採用中でない(active でない chunk_set の)抽出は見ない。
    inactive = await _document_with_fields(
        client, [ExtractionField(name="金額", value="9000000", value_type="number")], active=False
    )
    documents = [large, small, unparsed, no_fields, inactive]

    assert await _matching(
        client, documents, {"name": "金額", "value_type": "number", "op": "gte", "value": "1000000"}
    ) == {large}
    assert await _matching(
        client, documents, {"name": "金額", "value_type": "number", "op": "lte", "value": "500000"}
    ) == {small}
    assert await _matching(
        client,
        documents,
        {"name": "契約日", "value_type": "date", "op": "gte", "value": "2025-01-01"},
    ) == {large}
    assert await _matching(
        client, documents, {"name": "自動更新", "value_type": "bool", "value": "false"}
    ) == {small}
    assert await _matching(
        client, documents, {"name": "契約番号", "value_type": "string", "value": "C-1"}
    ) == {large}
    # 条件どうしは AND(範囲の上下限を 2 つの条件で指定する)。
    assert await _matching(
        client,
        documents,
        {"name": "金額", "value_type": "number", "op": "gte", "value": "100000"},
        {"name": "契約日", "value_type": "date", "op": "lte", "value": "2024-12-31"},
    ) == {small}
