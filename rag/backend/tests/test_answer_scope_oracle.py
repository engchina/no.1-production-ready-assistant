"""回答が検索範囲から読む語の一覧を、実 Oracle AI Database で確かめる。

未到達なら oracle_db fixture が skip し、作成行は cleanup_to_baseline で後始末する。
AI は決定論スタブ(oracle_db fixture)で、SQL だけを実 DB で評価する。
"""

from uuid import uuid4

import pytest

from app.clients.oracle import OracleClient, _oracle_retrieval_where
from app.rag.chunking import Chunk
from app.schemas.document import DocumentClassification, FileStatus
from app.schemas.extraction import StructuredExtraction

_EMBEDDING = [0.1] * 1024


async def _indexed_document(
    client: OracleClient,
    *,
    file_name: str,
    chunks: list[Chunk],
    large_category: str | None = None,
    duplicate_of_document_id: str | None = None,
    knowledge_base_ids: list[str] | None = None,
) -> str:
    """検索対象(INDEXED・active な chunk_set)の文書を 1 件作る。"""
    detail = await client.create_document(
        file_name=file_name,
        object_storage_path=f"local://{file_name}",
        content_type="application/pdf",
        duplicate_of_document_id=duplicate_of_document_id,
        knowledge_base_ids=knowledge_base_ids,
    )
    document_id = detail.id
    recipes = await client.list_document_recipes(document_id)
    recipe_id = str(recipes[0]["recipe_id"])
    chunk_set_id = f"cs_scope_{uuid4().hex[:16]}"
    await client.upsert_chunk_set(
        chunk_set_id=chunk_set_id, document_id=document_id, recipe_id=recipe_id
    )
    await client.save_index(
        document_id,
        StructuredExtraction(raw_text="本文", confidence=0.9),
        chunks,
        [_EMBEDDING] * len(chunks),
        chunk_set_id=chunk_set_id,
    )
    await client.mark_chunk_set_indexed(
        chunk_set_id=chunk_set_id, chunk_count=len(chunks), vector_count=len(chunks)
    )
    # materialized_revision は取込と同じくレシピの版を渡す(None の bind は型が決まらない)。
    await client.activate_recipe_chunk_set(
        recipe_id=recipe_id,
        chunk_set_id=chunk_set_id,
        materialized_revision=int(str(recipes[0].get("config_revision") or 1)),
    )
    await client.update_document_status(document_id, FileStatus.INDEXED)
    if large_category is not None:
        await client.save_document_classification(
            document_id, DocumentClassification(large_category=large_category)
        )
    return document_id


def _chunk(index: int, text: str, section_path: str | None = None) -> Chunk:
    return Chunk(
        index=index,
        text=text,
        start_offset=0,
        end_offset=len(text),
        metadata={"section_path": section_path} if section_path else {},
    )


@pytest.mark.usefixtures("oracle_db")
async def test_retrieval_large_categories_on_real_oracle() -> None:
    """検索範囲の文書の大分類を DISTINCT で返す。範囲外・分類の無い文書は含めない(#553)。"""
    client = OracleClient()
    token = uuid4().hex[:12]
    await _indexed_document(
        client,
        file_name=f"scope-{token}-a1.pdf",
        chunks=[_chunk(0, "A")],
        large_category="10_業務A",
    )
    await _indexed_document(
        client,
        file_name=f"scope-{token}-a2.pdf",
        chunks=[_chunk(0, "A")],
        large_category="10_業務A",
    )
    await _indexed_document(
        client, file_name=f"scope-{token}-b.pdf", chunks=[_chunk(0, "B")], large_category="業務B"
    )
    await _indexed_document(client, file_name=f"scope-{token}-none.pdf", chunks=[_chunk(0, "N")])
    # 範囲外(ファイル名の条件に合わない)の文書の大分類は含めない。
    await _indexed_document(
        client, file_name=f"other-{token}.pdf", chunks=[_chunk(0, "C")], large_category="業務C"
    )

    assert await client.retrieval_large_categories({"file_name": f"scope-{token}"}) == [
        "10_業務A",
        "業務B",
    ]


@pytest.mark.usefixtures("oracle_db")
async def test_retrieval_screen_catalog_on_real_oracle() -> None:
    """検索範囲の chunk の(文書名、見出しの列)を DISTINCT で集計し、画面の chunk を読む(#554)。

    範囲の状態は文書の追加で変わる(画面目録の cache を作り直す)。
    """
    client = OracleClient()
    token = uuid4().hex[:12]
    screen = "（２）帳票印字設定"
    filters = {"file_name": f"screen-{token}"}
    setting = f"screen-{token}-setting.pdf"
    await _indexed_document(
        client,
        file_name=setting,
        chunks=[
            _chunk(0, "設定の概要", "設定"),
            _chunk(1, "印字の有無を切り替えます。", f"設定 > {screen}"),
            _chunk(2, "印字の位置を変えます。", f"設定 > {screen}"),
            _chunk(3, "履歴を表示します。", f"設定 > {screen}の履歴"),
            _chunk(4, "見出しの無い本文"),
        ],
    )
    state_before = await client.retrieval_scope_state(filters)

    sections = await client.retrieval_screen_sections(filters)

    assert sections == [
        (setting, "設定", 1),
        (setting, f"設定 > {screen}", 2),
        (setting, f"設定 > {screen}の履歴", 1),
    ]
    chunks = await client.retrieval_screen_chunks(
        filters, file_name=setting, heading=screen, limit=10
    )
    # SQL は見出しの部分一致(「…の履歴」も含む)。要素としての一致は呼び出し側で確かめる。
    assert [chunk.text for chunk in chunks] == [
        "印字の有無を切り替えます。",
        "印字の位置を変えます。",
        "履歴を表示します。",
    ]
    assert all(chunk.file_name == setting for chunk in chunks)
    assert (
        len(
            await client.retrieval_screen_chunks(
                filters, file_name=setting, heading=screen, limit=1
            )
        )
        == 1
    )

    await _indexed_document(
        client, file_name=f"screen-{token}-other.pdf", chunks=[_chunk(0, "別文書", "（１）一覧")]
    )
    assert await client.retrieval_scope_state(filters) != state_before


@pytest.mark.usefixtures("oracle_db")
async def test_retrievable_chunk_follows_search_visibility_on_real_oracle() -> None:
    """MCP の rag_read_source が読む chunk は検索と同じ見え方の条件に従う（#1219）。

    有効な chunk_set の chunk だけを返し、古い版・利用できる範囲の外は返さない。古い版は
    ``accessible_chunk_exists`` で見分ける。
    """
    from dataclasses import replace

    from app.rag.request_context import (
        current_audit_request_context,
        reset_audit_request_context,
        set_audit_request_context,
    )

    client = OracleClient()
    token = uuid4().hex[:12]
    document_id = await _indexed_document(
        client,
        file_name=f"read-{token}.pdf",
        chunks=[_chunk(0, "立替経費は翌月10日までに申請する。", "規程 > 第2条")],
    )
    [old] = await client.list_document_chunks(document_id)

    chunk = await client.retrievable_chunk(document_id, old.chunk_id)
    assert chunk is not None
    assert chunk.text == "立替経費は翌月10日までに申請する。"
    assert chunk.metadata["section_path"] == "規程 > 第2条"
    assert chunk.metadata["chunk_set_id"]
    assert await client.retrievable_chunk(document_id, "missing-chunk") is None
    assert await client.accessible_chunk_exists(document_id, "missing-chunk") is False

    # 利用できるナレッジベースの外の文書は、検索と同じく見えない。
    scoped = set_audit_request_context(
        replace(current_audit_request_context(), allowed_knowledge_base_ids=frozenset({"kb-none"}))
    )
    try:
        assert await client.retrievable_chunk(document_id, old.chunk_id) is None
        assert await client.accessible_chunk_exists(document_id, old.chunk_id) is False
    finally:
        reset_audit_request_context(scoped)

    # 新しい版を有効にすると、古い版の chunk は検索では見えない（行が残っていれば stale）。
    recipes = await client.list_document_recipes(document_id)
    recipe_id = str(recipes[0]["recipe_id"])
    new_chunk_set_id = f"cs_read_{uuid4().hex[:16]}"
    await client.upsert_chunk_set(
        chunk_set_id=new_chunk_set_id, document_id=document_id, recipe_id=recipe_id
    )
    await client.save_index(
        document_id,
        StructuredExtraction(raw_text="本文", confidence=0.9),
        [_chunk(0, "改訂後の本文", "規程 > 第2条")],
        [_EMBEDDING],
        chunk_set_id=new_chunk_set_id,
    )
    await client.mark_chunk_set_indexed(
        chunk_set_id=new_chunk_set_id, chunk_count=1, vector_count=1
    )
    await client.activate_recipe_chunk_set(
        recipe_id=recipe_id,
        chunk_set_id=new_chunk_set_id,
        materialized_revision=int(str(recipes[0].get("config_revision") or 1)),
    )
    assert await client.retrievable_chunk(document_id, old.chunk_id) is None
    remaining = {view.chunk_id for view in await client.list_document_chunks(document_id)}
    assert await client.accessible_chunk_exists(document_id, old.chunk_id) is (
        old.chunk_id in remaining
    )


def _element_chunk(index: int, text: str, element_ids: str) -> Chunk:
    return Chunk(
        index=index,
        text=text,
        start_offset=0,
        end_offset=len(text),
        metadata={"element_ids": element_ids, "page_start": 1},
    )


@pytest.mark.usefixtures("oracle_db")
async def test_element_locator_follows_rechunking_on_real_oracle() -> None:
    """要素の定位子は、同じ解析の結果で chunk を作り直しても同じ要素を含む chunk を返す（#1330）。

    要素の ID は部分一致ではなく完全一致で選ぶ（``p1-1`` で ``p1-10`` を返さない）。解析の結果が
    変わった（古い定位子）ことは、文書が見えるか（``accessible_document_exists``）で見分ける。
    """
    client = OracleClient()
    token = uuid4().hex[:12]
    extraction_id = f"er_loc_{token}"
    document_id = await _indexed_document(
        client,
        file_name=f"locator-{token}.pdf",
        chunks=[_element_chunk(0, "第1条の本文", "docling-p1-10")],
    )
    recipes = await client.list_document_recipes(document_id)
    recipe_id = str(recipes[0]["recipe_id"])
    revision = int(str(recipes[0].get("config_revision") or 1))

    async def rechunk(chunks: list[Chunk], extraction_recipe_id: str) -> str:
        chunk_set_id = f"cs_loc_{uuid4().hex[:16]}"
        await client.upsert_chunk_set(
            chunk_set_id=chunk_set_id,
            document_id=document_id,
            recipe_id=recipe_id,
            extraction_recipe_id=extraction_recipe_id,
        )
        await client.save_index(
            document_id,
            StructuredExtraction(raw_text="本文", confidence=0.9),
            chunks,
            [_EMBEDDING] * len(chunks),
            chunk_set_id=chunk_set_id,
        )
        await client.mark_chunk_set_indexed(
            chunk_set_id=chunk_set_id, chunk_count=len(chunks), vector_count=len(chunks)
        )
        await client.activate_recipe_chunk_set(
            recipe_id=recipe_id, chunk_set_id=chunk_set_id, materialized_revision=revision
        )
        return chunk_set_id

    first_set = await rechunk(
        [
            _element_chunk(0, "第1条と第2条", "docling-p1-1,docling-p1-2"),
            _element_chunk(1, "第10条", "docling-p1-10"),
        ],
        extraction_id,
    )
    found = await client.retrievable_element_chunk(document_id, extraction_id, "docling-p1-2")
    assert found is not None
    assert (found.text, found.metadata["chunk_set_id"]) == ("第1条と第2条", first_set)
    assert await client.chunk_set_extraction_recipe_ids([first_set]) == {first_set: extraction_id}
    # 部分一致（p1-1 は p1-10 の先頭と同じ）で別の要素の chunk を返さない。
    only_ten = await client.retrievable_element_chunk(document_id, extraction_id, "docling-p1-10")
    assert only_ten is not None and only_ten.text == "第10条"

    # 文書分割だけを変えて作り直す（同じ解析の結果）。chunk の ID は変わるが、同じ要素を返す。
    second_set = await rechunk(
        [_element_chunk(0, "第1条から第10条まで", "docling-p1-1,docling-p1-2,docling-p1-10")],
        extraction_id,
    )
    again = await client.retrievable_element_chunk(document_id, extraction_id, "docling-p1-2")
    assert again is not None
    assert again.metadata["chunk_set_id"] == second_set
    assert again.chunk_id != found.chunk_id

    # 解析をやり直した（別の解析の結果）後は、古い定位子では読めず、古い版と分かる。
    await rechunk([_element_chunk(0, "再解析後", "docling-p1-1")], f"er_new_{token}")
    assert (
        await client.retrievable_element_chunk(document_id, extraction_id, "docling-p1-2") is None
    )
    # 文書は見えるので、今の解析の結果に無い定位子は古い版（source_stale）と分かる。
    assert await client.accessible_document_exists(document_id) is True
    assert await client.accessible_document_exists(f"missing-{token}") is False


def _page_chunk(index: int, text: str, section_path: str, page: int) -> Chunk:
    return Chunk(
        index=index,
        text=text,
        start_offset=0,
        end_offset=len(text),
        metadata={"section_path": section_path, "page_start": page, "page_end": page},
    )


@pytest.mark.usefixtures("oracle_db")
async def test_document_reading_index_and_chunks_on_real_oracle() -> None:
    """文書を順に読む目次と本文（MCP の rag_outline / rag_read_document。#1332）。

    目次は本文を読まずに節・頁・文字数を chunk の順に返し、本文は指定した位置から順に返す。
    利用できるナレッジベースの外の文書は、検索と同じく読めない。
    """
    from dataclasses import replace

    from app.rag.request_context import (
        current_audit_request_context,
        reset_audit_request_context,
        set_audit_request_context,
    )

    client = OracleClient()
    token = uuid4().hex[:12]
    document_id = await _indexed_document(
        client,
        file_name=f"reading-{token}.pdf",
        chunks=[
            _page_chunk(0, "総則の本文。", "規程 > 第1章", 1),
            _page_chunk(1, "申請の本文です。", "規程 > 第2章", 2),
            _page_chunk(2, "承認の本文。", "規程 > 第3章", 3),
        ],
    )

    chunk_set_id, rows = await client.document_reading_index(document_id)
    assert chunk_set_id is not None
    assert [row["section_path"] for row in rows] == ["規程 > 第1章", "規程 > 第2章", "規程 > 第3章"]
    assert [(row["page_start"], row["page_end"]) for row in rows] == [(1, 1), (2, 2), (3, 3)]
    assert [row["chars"] for row in rows] == [6, 8, 6]
    indexes = [int(str(row["chunk_index"])) for row in rows]

    tail = await client.readable_document_chunks(
        document_id, chunk_set_id=chunk_set_id, from_index=indexes[1], limit=10
    )
    assert [chunk.text for chunk in tail] == ["申請の本文です。", "承認の本文。"]
    assert tail[0].file_name == f"reading-{token}.pdf"
    # 有効でない chunk_set の位置からは読めない（古い cursor）。
    assert (
        await client.readable_document_chunks(
            document_id, chunk_set_id="cs_missing", from_index=0, limit=10
        )
        == []
    )

    scoped = set_audit_request_context(
        replace(current_audit_request_context(), allowed_knowledge_base_ids=frozenset({"kb-none"}))
    )
    try:
        assert await client.document_reading_index(document_id) == (None, [])
        assert (
            await client.readable_document_chunks(
                document_id, chunk_set_id=chunk_set_id, from_index=0, limit=10
            )
            == []
        )
    finally:
        reset_audit_request_context(scoped)


@pytest.mark.usefixtures("oracle_db")
async def test_evaluation_document_locations_on_real_oracle() -> None:
    """品質評価のテンプレートの「文書一覧」のページ数・シート名（#2086）。

    chunk の metadata から求める（本文は読まない）。
    """
    client = OracleClient()
    token = uuid4().hex[:12]
    paged = await _indexed_document(
        client,
        file_name=f"locations-{token}.pdf",
        chunks=[
            _page_chunk(0, "1 ページの本文。", "資料 > 第1章", 1),
            _page_chunk(1, "3 ページの本文。", "資料 > 第2章", 3),
        ],
    )
    sheet_text = "シートの行の本文。"
    sheet = await _indexed_document(
        client,
        file_name=f"locations-{token}.xlsx",
        chunks=[
            Chunk(
                index=index,
                text=sheet_text,
                start_offset=0,
                end_offset=len(sheet_text),
                metadata={"sheet_name": name, "row_start": 2, "row_end": 3},
            )
            for index, name in enumerate(["料金", "一覧", "料金"])
        ],
    )

    locations = await client.evaluation_document_locations([paged, sheet, "doc-missing"])

    assert locations[paged] == (3, ())
    assert locations[sheet] == (None, ("一覧", "料金"))
    assert "doc-missing" not in locations
    assert await client.evaluation_document_locations([]) == {}


async def _retrieval_document_ids(client: OracleClient, filters: dict[str, str]) -> set[str]:
    """検索と同じ条件(`_oracle_retrieval_where`)で chunk の取れる文書の ID。"""
    where_sql, binds = _oracle_retrieval_where(filters)
    rows = await client._fetch_all(  # noqa: SLF001 - 検索の範囲の述語を実 DB で評価する
        "SELECT DISTINCT d.document_id FROM rag_chunks c "
        "JOIN rag_documents d ON d.document_id = c.document_id WHERE " + where_sql,
        binds,
    )
    return {str(row["document_id"]) for row in rows}


@pytest.mark.usefixtures("oracle_db")
async def test_duplicate_with_own_index_stays_in_its_kb_on_real_oracle() -> None:
    """別の KB に同じ内容の文書があっても、自前の索引を持つ重複は自分の chunk で検索する(#1381)。

    取込を省いた重複(自前の索引が無い)は正本の chunk を使い、旧版として登録した重複からは
    正本へ届かない(include_superseded=true のときは届く)。
    """
    client = OracleClient()
    token = uuid4().hex[:12]
    earlier = await client.create_knowledge_base(name=f"評価 前回 {token}")
    current = await client.create_knowledge_base(name=f"評価 今回 {token}")
    skipped = await client.create_knowledge_base(name=f"重複の取込を省いた {token}")
    canonical = await _indexed_document(
        client,
        file_name=f"rules-{token}.pdf",
        chunks=[_chunk(0, "前回の本文")],
        knowledge_base_ids=[earlier.id],
    )
    canonical_old = await _indexed_document(
        client,
        file_name=f"rules-{token}-2023.pdf",
        chunks=[_chunk(0, "前回の旧版の本文")],
        knowledge_base_ids=[earlier.id],
    )
    duplicate = await _indexed_document(
        client,
        file_name=f"rules-{token}.pdf",
        chunks=[_chunk(0, "今回の本文")],
        duplicate_of_document_id=canonical,
        knowledge_base_ids=[current.id],
    )
    duplicate_old = await _indexed_document(
        client,
        file_name=f"rules-{token}-2023.pdf",
        chunks=[_chunk(0, "今回の旧版の本文")],
        duplicate_of_document_id=canonical_old,
        knowledge_base_ids=[current.id],
    )
    await client.set_document_superseded_by(duplicate_old, duplicate)

    assert await _retrieval_document_ids(client, {"knowledge_base_id": current.id}) == {duplicate}
    assert await _retrieval_document_ids(
        client, {"knowledge_base_id": current.id, "include_superseded": "true"}
    ) == {duplicate, duplicate_old}

    # 取込を省いた重複(自前の索引が無い)は正本の chunk を使う。旧版の重複からは届かない。
    reused = await client.create_document(
        file_name=f"rules-{token}.pdf",
        object_storage_path=f"local://rules-{token}-reused.pdf",
        content_type="application/pdf",
        duplicate_of_document_id=canonical,
        knowledge_base_ids=[skipped.id],
    )
    reused_old = await client.create_document(
        file_name=f"rules-{token}-2023.pdf",
        object_storage_path=f"local://rules-{token}-2023-reused.pdf",
        content_type="application/pdf",
        duplicate_of_document_id=canonical_old,
        knowledge_base_ids=[skipped.id],
    )
    await client.set_document_superseded_by(reused_old.id, reused.id)
    assert await _retrieval_document_ids(client, {"knowledge_base_id": skipped.id}) == {canonical}
    assert await _retrieval_document_ids(
        client, {"knowledge_base_id": skipped.id, "include_superseded": "true"}
    ) == {canonical, canonical_old}


@pytest.mark.usefixtures("oracle_db")
async def test_including_superseded_stays_within_allowed_knowledge_bases_on_real_oracle() -> None:
    """旧版を含めても、利用者の範囲(許可した KB)の外の文書は入らない(#1392)。

    MCP の include_superseded=true は検索の要求の filters の include_superseded になる。業務 Agent
    のデータの範囲(#1379)と利用者の権限は KB の範囲(allowed_knowledge_base_ids と
    knowledge_base_id)に落ちるので、範囲の外の KB の旧版・重複の正本(#1381)は検索に入らない。
    """
    from dataclasses import replace

    from app.rag.request_context import (
        current_audit_request_context,
        reset_audit_request_context,
        set_audit_request_context,
    )

    client = OracleClient()
    token = uuid4().hex[:12]
    inside = await client.create_knowledge_base(name=f"範囲の中 {token}")
    outside = await client.create_knowledge_base(name=f"範囲の外 {token}")
    # 範囲の外の KB: 今の版と旧版(どちらも範囲の中の重複の正本)。
    outside_current = await _indexed_document(
        client,
        file_name=f"policy-{token}.pdf",
        chunks=[_chunk(0, "範囲の外の今の版")],
        knowledge_base_ids=[outside.id],
    )
    outside_old = await _indexed_document(
        client,
        file_name=f"policy-{token}-2023.pdf",
        chunks=[_chunk(0, "範囲の外の旧版")],
        knowledge_base_ids=[outside.id],
    )
    await client.set_document_superseded_by(outside_old, outside_current)
    # 範囲の中の KB: 自前の索引を持つ重複(今の版と旧版)。
    inside_current = await _indexed_document(
        client,
        file_name=f"policy-{token}.pdf",
        chunks=[_chunk(0, "範囲の中の今の版")],
        duplicate_of_document_id=outside_current,
        knowledge_base_ids=[inside.id],
    )
    inside_old = await _indexed_document(
        client,
        file_name=f"policy-{token}-2023.pdf",
        chunks=[_chunk(0, "範囲の中の旧版")],
        duplicate_of_document_id=outside_old,
        knowledge_base_ids=[inside.id],
    )
    await client.set_document_superseded_by(inside_old, inside_current)

    scoped = set_audit_request_context(
        replace(current_audit_request_context(), allowed_knowledge_base_ids=frozenset({inside.id}))
    )
    try:
        included = {"include_superseded": "true"}
        assert await _retrieval_document_ids(client, included) == {inside_current, inside_old}
        assert await _retrieval_document_ids(
            client, {**included, "knowledge_base_id": inside.id}
        ) == {inside_current, inside_old}
        # 範囲の外の KB を名指ししても、許可の外なので何も取れない。
        assert (
            await _retrieval_document_ids(client, {**included, "knowledge_base_id": outside.id})
            == set()
        )
        assert await _retrieval_document_ids(client, {}) == {inside_current}
    finally:
        reset_audit_request_context(scoped)


@pytest.mark.usefixtures("oracle_db")
async def test_older_versions_of_hit_documents_on_real_oracle() -> None:
    """当たった文書の旧版を、旧版を含めた検索と同じ範囲の中だけから返す(#1405)。

    タイトルは見出しのある最初の chunk の先頭の見出し(版・年度を含む)。範囲の外の KB の旧版・
    旧版でない文書は返さない。
    """
    from dataclasses import replace

    from app.rag.request_context import (
        current_audit_request_context,
        reset_audit_request_context,
        set_audit_request_context,
    )

    client = OracleClient()
    token = uuid4().hex[:12]
    inside = await client.create_knowledge_base(name=f"版の範囲の中 {token}")
    outside = await client.create_knowledge_base(name=f"版の範囲の外 {token}")
    current = await _indexed_document(
        client,
        file_name=f"maintenance-plan-{token}.pdf",
        chunks=[_chunk(0, "ＨＲＭ: 毎月第 2 土曜日", "定期保守計画 2026年度 > 第 1 章")],
        knowledge_base_ids=[inside.id, outside.id],
    )
    old = await _indexed_document(
        client,
        file_name=f"maintenance-plan-{token}-2025.pdf",
        chunks=[
            _chunk(0, "表紙"),
            _chunk(1, "ＨＲＭ: 毎月第 1 土曜日", "定期保守計画 2025年度 > 第 1 章"),
        ],
        knowledge_base_ids=[inside.id],
    )
    await client.set_document_superseded_by(old, current)
    # 範囲の外の KB にだけある旧版。
    outside_old = await _indexed_document(
        client,
        file_name=f"maintenance-plan-{token}-2024.pdf",
        chunks=[_chunk(0, "ＨＲＭ: 毎月第 3 土曜日", "定期保守計画 2024年度")],
        knowledge_base_ids=[outside.id],
    )
    await client.set_document_superseded_by(outside_old, current)
    # 旧版でない別の文書は返さない。
    await _indexed_document(
        client,
        file_name=f"other-{token}.pdf",
        chunks=[_chunk(0, "別の文書", "別の文書")],
        knowledge_base_ids=[inside.id],
    )

    versions = await client.retrieval_older_versions(
        {"knowledge_base_id": inside.id}, [current], limit=10
    )
    assert [(v.document_id, v.title, v.superseded_by_document_id) for v in versions] == [
        (old, "定期保守計画 2025年度", current)
    ]
    assert versions[0].file_name == f"maintenance-plan-{token}-2025.pdf"
    both = {"knowledge_base_id": f"{inside.id},{outside.id}"}
    found = await client.retrieval_older_versions(both, [current], limit=10)
    assert {version.document_id for version in found} == {old, outside_old}
    assert len(await client.retrieval_older_versions(both, [current], limit=1)) == 1
    assert await client.retrieval_older_versions(both, [old], limit=10) == []

    # 利用者の範囲(許可した KB)の外の旧版は、KB を名指ししても返さない。
    scoped = set_audit_request_context(
        replace(current_audit_request_context(), allowed_knowledge_base_ids=frozenset({inside.id}))
    )
    try:
        found = await client.retrieval_older_versions(both, [current], limit=10)
        assert [version.document_id for version in found] == [old]
    finally:
        reset_audit_request_context(scoped)


@pytest.mark.usefixtures("oracle_db")
async def test_accessible_document_ids_follow_the_viewer_scope_on_real_oracle() -> None:
    """保存した根拠の確かめ直し（#3252）: 見る人の今の KB の範囲の文書だけを返す。

    重複アップロードの正本は複製の KB で見え、削除した文書・範囲の外の文書は含まない。
    """
    from dataclasses import replace

    from app.rag.request_context import (
        current_audit_request_context,
        reset_audit_request_context,
        set_audit_request_context,
    )

    client = OracleClient()
    token = uuid4().hex[:12]
    kb_a = await client.create_knowledge_base(name=f"根拠 A {token}")
    kb_b = await client.create_knowledge_base(name=f"根拠 B {token}")
    in_a = await _indexed_document(
        client, file_name=f"a-{token}.pdf", chunks=[_chunk(0, "A")], knowledge_base_ids=[kb_a.id]
    )
    in_b = await _indexed_document(
        client, file_name=f"b-{token}.pdf", chunks=[_chunk(0, "B")], knowledge_base_ids=[kb_b.id]
    )
    # B の文書を A に重複アップロードした複製（正本の chunk を使う）。
    await client.create_document(
        file_name=f"b-{token}.pdf",
        object_storage_path=f"local://b-{token}-dup.pdf",
        content_type="application/pdf",
        duplicate_of_document_id=in_b,
        knowledge_base_ids=[kb_a.id],
    )
    removed = await _indexed_document(
        client, file_name=f"c-{token}.pdf", chunks=[_chunk(0, "C")], knowledge_base_ids=[kb_a.id]
    )
    assert await client.delete_document(removed) is True
    ids = [in_a, in_b, removed, f"missing-{token}"]

    assert await client.accessible_document_ids(ids) == {in_a, in_b}
    for allowed, expected in (
        (frozenset({kb_b.id}), {in_b}),
        # 正本（B）は A の複製から見える（検索と同じ見え方）。
        (frozenset({kb_a.id}), {in_a, in_b}),
        (frozenset(), set()),
    ):
        scoped = set_audit_request_context(
            replace(current_audit_request_context(), allowed_knowledge_base_ids=allowed)
        )
        try:
            assert await client.accessible_document_ids(ids) == expected
        finally:
            reset_audit_request_context(scoped)
