"""source parser registry のルーティングテスト。"""

import json
import sys
from email.message import EmailMessage
from io import BytesIO
from types import ModuleType, SimpleNamespace
from zipfile import ZipFile

import pytest
from rag_parser_core.registry import (
    parse_openxml_office_segment_extractions,
    parse_with_registry,
)

from app.rag.chunking import chunk_extraction
from app.rag.ingestion_quality import build_ingestion_quality_report
from app.rag.source_profile import build_source_profile
from app.schemas.document import SourceModality
from app.schemas.extraction import ExtractionAsset, ExtractionTableCell


def test_parser_registry_partitions_text_html_email_sources() -> None:
    """標準テキスト系は Enterprise AI に送らず local partition する。"""
    cases = [
        ("plain.txt", "# 見出し\n本文".encode(), "text/plain", "text"),
        ("memo.md", b"# Title\nBody", "text/markdown", "text"),
        ("table.csv", b"name,value\nalpha,1", "text/csv", "text"),
        ("payload.json", b'{"name":"alpha"}', "application/json", "text"),
        (
            "events.jsonl",
            b'{"event":"created"}\n{"event":"done"}',
            "application/octet-stream",
            "text",
        ),
        (
            "events.ndjson",
            b'{"event":"created"}\n{"event":"done"}',
            "application/x-ndjson",
            "text",
        ),
        (
            "page.html",
            b"<html><body><h1>Title</h1><p>Body</p></body></html>",
            "text/html",
            "html",
        ),
        (
            "mail.eml",
            b"Subject: Hello\nFrom: a@example.com\nTo: b@example.com\n\nBody",
            "message/rfc822",
            "email",
        ),
    ]

    for file_name, data, content_type, modality in cases:
        profile = build_source_profile(
            original_file_name=file_name,
            sanitized_file_name=file_name,
            content_type=content_type,
            file_size_bytes=len(data),
            content_sha256="a" * 64,
            data=data,
        )

        result = parse_with_registry(data, source_profile=profile, content_type=content_type)

        assert profile.modality == SourceModality(modality)
        assert result.parser_backend == "local_partition"
        assert result.extraction is not None
        assert result.extraction.raw_text


def test_parser_registry_preserves_markdown_code_and_equation_blocks() -> None:
    """Markdown code/formula block は local parser の element lineage を保持する。"""
    data = b"""# Runbook
```sql
select 1 from dual;
```

\\[
a^2 + b^2 = c^2
\\]
"""
    content_type = "text/markdown"
    profile = build_source_profile(
        original_file_name="runbook.md",
        sanitized_file_name="runbook.md",
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256="b" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type=content_type)

    assert result.parser_backend == "local_partition"
    assert result.extraction is not None
    code_element = next(element for element in result.extraction.elements if element.kind == "code")
    equation_element = next(
        element for element in result.extraction.elements if element.kind == "equation"
    )
    assert code_element.content_kind == "code"
    assert code_element.source_parser == "local_text_structure"
    assert code_element.metadata["code_language"] == "sql"
    assert code_element.metadata["chunk_template"] == "markdown_by_heading"
    assert equation_element.content_kind == "equation"
    assert equation_element.source_parser == "local_text_structure"
    assert equation_element.metadata["equation_delimiter"] == "\\[\\]"


def test_parser_registry_preserves_markdown_link_lineage_in_chunks() -> None:
    """Markdown inline/reference links は安全 URL だけを chunk metadata へ残す。"""
    data = """# Links

詳細は [公式](https://example.com/docs?q=rag,links) と [仕様][spec] を確認します。
[危険](javascript:alert(1)) は URL lineage に入れません。

[spec]: https://example.com/spec
""".encode()
    content_type = "text/markdown"
    profile = build_source_profile(
        original_file_name="links.md",
        sanitized_file_name="links.md",
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256="e" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type=content_type)

    assert result.extraction is not None
    text_element = next(element for element in result.extraction.elements if "公式" in element.text)
    assert text_element.metadata["link_count"] == 2
    assert text_element.metadata["link_urls"] == (
        "https://example.com/docs?q=rag,links\nhttps://example.com/spec"
    )
    assert text_element.metadata["link_texts"] == "公式\n仕様"
    chunks = chunk_extraction(result.extraction, chunk_size=240, overlap=0)
    link_chunk = next(chunk for chunk in chunks if "仕様" in chunk.text)
    assert link_chunk.metadata["link_count"] == 2
    assert link_chunk.metadata["link_urls"] == (
        "https://example.com/docs?q=rag,links\nhttps://example.com/spec"
    )
    assert link_chunk.metadata["link_texts"] == "公式\n仕様"


def test_parser_registry_promotes_markdown_images_to_assets_and_chunks() -> None:
    """Markdown image は figure element / assets[] / chunk metadata へ remap する。"""
    data = """# 図表

![検索フロー](assets/flow.png "RAG flow")

![危険](data:text/html,alert)
""".encode()
    content_type = "text/markdown"
    profile = build_source_profile(
        original_file_name="images.md",
        sanitized_file_name="images.md",
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256="f" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type=content_type)

    assert result.extraction is not None
    assert len(result.extraction.assets) == 1
    figure = next(
        element for element in result.extraction.elements if element.content_kind == "figure"
    )
    assert figure.kind == "figure"
    assert figure.text == "検索フロー"
    assert figure.metadata["asset_id"] == "markdown-image-0000"
    assert figure.metadata["link_urls"] == "assets/flow.png"
    assert figure.metadata["link_texts"] == "検索フロー"
    asset = result.extraction.assets[0]
    assert asset.asset_id == "markdown-image-0000"
    assert asset.kind == "image"
    assert asset.alt_text == "検索フロー"
    assert asset.metadata["source_url"] == "assets/flow.png"
    assert asset.metadata["title"] == "RAG flow"
    chunk = next(
        item
        for item in chunk_extraction(result.extraction, chunk_size=120, overlap=0)
        if item.metadata["content_kind"] == "figure"
    )
    assert chunk.metadata["asset_id"] == "markdown-image-0000"
    assert chunk.metadata["link_urls"] == "assets/flow.png"


def test_parser_registry_promotes_markdown_tables_to_structured_cells() -> None:
    """Markdown table は table element だけでなく tables[]/cells[] にも保持する。"""
    data = """# 料金

|項目|金額|
|---|---|
|交通費|1000円|
|宿泊費|2000円|
""".encode()
    content_type = "text/markdown"
    profile = build_source_profile(
        original_file_name="prices.md",
        sanitized_file_name="prices.md",
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256="9" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type=content_type)

    assert result.extraction is not None
    assert result.extraction.parser_artifacts["table_count"] == 1
    table_element = next(
        element for element in result.extraction.elements if element.content_kind == "table"
    )
    assert table_element.metadata["table_id"] == "markdown-table-0000"
    assert table_element.metadata["row_count"] == 3
    assert table_element.metadata["column_count"] == 2
    table = result.extraction.tables[0]
    assert table.table_id == "markdown-table-0000"
    assert table.element_id == table_element.element_id
    assert [(cell.row, cell.col, cell.text) for cell in table.cells] == [
        (0, 0, "項目"),
        (0, 1, "金額"),
        (1, 0, "交通費"),
        (1, 1, "1000円"),
        (2, 0, "宿泊費"),
        (2, 1, "2000円"),
    ]
    table_chunk = next(
        chunk
        for chunk in chunk_extraction(result.extraction, chunk_size=120, overlap=0)
        if chunk.metadata["content_kind"] == "table"
    )
    assert table_chunk.metadata["table_id"] == "markdown-table-0000"
    assert table_chunk.metadata["table_row_count"] == 3
    assert table_chunk.metadata["table_column_count"] == 2
    assert table_chunk.metadata["table_row_tree_version"] == "row_tree_v1"


def test_parser_registry_preserves_markdown_table_caption_and_escaped_pipe() -> None:
    """Markdown table caption と escaped pipe は cell 構造を壊さず保持する。"""
    data = """# 製品

表1: 製品別説明

|項目|説明|
|---|---|
|OCI\\|Oracle|クラウド基盤|
|RAG|検索\\|生成|
""".encode()
    content_type = "text/markdown"
    profile = build_source_profile(
        original_file_name="escaped-table.md",
        sanitized_file_name="escaped-table.md",
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256="6" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type=content_type)

    assert result.extraction is not None
    table_element = next(
        element for element in result.extraction.elements if element.content_kind == "table"
    )
    assert table_element.text.startswith("表1: 製品別説明\n|項目|説明|")
    assert table_element.metadata["table_caption"] == "表1: 製品別説明"
    assert table_element.metadata["column_count"] == 2
    table = result.extraction.tables[0]
    assert table.caption == "表1: 製品別説明"
    assert table.metadata["table_caption"] == "表1: 製品別説明"
    assert [(cell.row, cell.col, cell.text) for cell in table.cells] == [
        (0, 0, "項目"),
        (0, 1, "説明"),
        (1, 0, "OCI|Oracle"),
        (1, 1, "クラウド基盤"),
        (2, 0, "RAG"),
        (2, 1, "検索|生成"),
    ]
    chunk = next(
        item
        for item in chunk_extraction(result.extraction, chunk_size=160, overlap=0)
        if item.metadata["content_kind"] == "table"
    )
    assert chunk.metadata["table_caption"] == "表1: 製品別説明"
    assert chunk.metadata["table_column_count"] == 2
    assert json.loads(str(chunk.metadata["table_row_tree_column_keys"])) == [
        "項目",
        "説明",
    ]


def test_parser_registry_infers_cross_page_markdown_table_lineage() -> None:
    """page marker をまたぐ同一表頭の Markdown table は同じ table_id に寄せる。"""
    data = """# 複数ページ表

Page 1

|項目|金額|
|---|---|
|交通費|1000円|

Page 2

|項目|金額|
|---|---|
|宿泊費|2000円|
""".encode()
    content_type = "text/markdown"
    profile = build_source_profile(
        original_file_name="cross-page-table.md",
        sanitized_file_name="cross-page-table.md",
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256="c" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type=content_type)

    assert result.extraction is not None
    table_elements = [
        element for element in result.extraction.elements if element.content_kind == "table"
    ]
    assert len(table_elements) == 2
    assert {element.metadata["table_id"] for element in table_elements} == {"inferred-table-0001"}
    assert [element.page_number for element in table_elements] == [1, 2]
    assert all(element.metadata["table_cross_page"] is True for element in table_elements)
    assert len(result.extraction.tables) == 2
    assert {table.table_id for table in result.extraction.tables} == {"inferred-table-0001"}
    assert all(len(table.cells) == 4 for table in result.extraction.tables)
    table_chunks = [
        chunk
        for chunk in chunk_extraction(result.extraction, chunk_size=80, overlap=0)
        if chunk.metadata["content_kind"] == "table"
    ]
    assert {chunk.metadata["table_id"] for chunk in table_chunks} == {"inferred-table-0001"}
    assert all(chunk.metadata["table_cross_page"] is True for chunk in table_chunks)


def test_parser_registry_preserves_csv_and_tsv_table_cells() -> None:
    """CSV/TSV は quoting を尊重し、tables/cells と table chunk lineage を持つ。"""
    cases = [
        (
            "table.csv",
            b'name,amount\n"alpha, beta",1200\n',
            "text/csv",
            [
                (0, 0, "name"),
                (0, 1, "amount"),
                (1, 0, "alpha, beta"),
                (1, 1, "1200"),
            ],
            "csv",
        ),
        (
            "table.tsv",
            b"name\tamount\nalpha\t1200\n",
            "text/tab-separated-values",
            [
                (0, 0, "name"),
                (0, 1, "amount"),
                (1, 0, "alpha"),
                (1, 1, "1200"),
            ],
            "tsv",
        ),
    ]

    for file_name, data, content_type, expected_cells, table_format in cases:
        profile = build_source_profile(
            original_file_name=file_name,
            sanitized_file_name=file_name,
            content_type=content_type,
            file_size_bytes=len(data),
            content_sha256="0" * 64,
            data=data,
        )

        result = parse_with_registry(data, source_profile=profile, content_type=content_type)

        assert result.template == "table_preserve_rows"
        assert result.extraction is not None
        assert result.extraction.parser_artifacts["table_format"] == table_format
        assert result.extraction.elements[0].kind == "table"
        assert result.extraction.elements[0].content_kind == "table"
        assert result.extraction.elements[0].metadata["chunk_template"] == "table_preserve_rows"
        table = result.extraction.tables[0]
        assert table.metadata["row_count"] == 2
        assert table.metadata["column_count"] == 2
        assert [(cell.row, cell.col, cell.text) for cell in table.cells] == expected_cells
        assert [cell.metadata["cell_ref"] for cell in table.cells] == [
            "A1",
            "B1",
            "A2",
            "B2",
        ]


def test_source_profile_classifies_tsv_extension_as_text_when_mime_is_generic() -> None:
    """TSV は application/octet-stream でも拡張子から local table parser へ進める。"""
    data = b"name\tamount\nalpha\t1200\n"

    profile = build_source_profile(
        original_file_name="metrics.tsv",
        sanitized_file_name="metrics.tsv",
        content_type="application/octet-stream",
        file_size_bytes=len(data),
        content_sha256="d" * 64,
        data=data,
    )

    assert profile.modality == SourceModality.TEXT
    assert profile.parser_backend == "local_partition"
    assert profile.parser_profile == "local_text_structure"
    assert profile.preview_kind == "text"
    assert profile.unsupported_reason is None


def test_parser_registry_preserves_html_table_cells_in_reading_order() -> None:
    """HTML table は普通 text block に分解せず、table/cell 構造として保持する。"""
    data = (
        "<html><body><h1>料金</h1>"
        "<table><tr><th>項目</th><th>金額</th></tr>"
        "<tr><td>交通費</td><td>1200</td></tr></table>"
        "<p>注記</p></body></html>"
    ).encode()
    profile = build_source_profile(
        original_file_name="page.html",
        sanitized_file_name="page.html",
        content_type="text/html",
        file_size_bytes=len(data),
        content_sha256="3" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type="text/html")

    assert result.extraction is not None
    assert [element.kind for element in result.extraction.elements] == [
        "title",
        "table",
        "text",
    ]
    table_element = result.extraction.elements[1]
    assert table_element.content_kind == "table"
    assert table_element.section_path == ["料金"]
    assert table_element.metadata["chunk_template"] == "html_semantic"
    table = result.extraction.tables[0]
    assert table.table_id == "html-table-0000"
    assert table.metadata["row_count"] == 2
    assert table.metadata["column_count"] == 2
    assert [(cell.row, cell.col, cell.text) for cell in table.cells] == [
        (0, 0, "項目"),
        (0, 1, "金額"),
        (1, 0, "交通費"),
        (1, 1, "1200"),
    ]
    assert [cell.metadata["cell_ref"] for cell in table.cells] == [
        "A1",
        "B1",
        "A2",
        "B2",
    ]


def test_parser_registry_preserves_html_table_caption_lineage() -> None:
    """HTML table caption は table / element / chunk の lineage に保持する。"""
    data = (
        "<html><body><h1>料金</h1>"
        "<table><caption>表1: 経費明細</caption>"
        "<tr><th>項目</th><th>金額</th></tr>"
        "<tr><td>交通費</td><td>1200</td></tr></table>"
        "</body></html>"
    ).encode()
    profile = build_source_profile(
        original_file_name="caption-table.html",
        sanitized_file_name="caption-table.html",
        content_type="text/html",
        file_size_bytes=len(data),
        content_sha256="7" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type="text/html")

    assert result.extraction is not None
    table_element = result.extraction.elements[1]
    table = result.extraction.tables[0]
    assert table.caption == "表1: 経費明細"
    assert table.metadata["table_caption"] == "表1: 経費明細"
    assert table_element.text.startswith("表1: 経費明細\n| 項目 | 金額 |")
    assert table_element.metadata["table_caption"] == "表1: 経費明細"
    chunk = next(
        item
        for item in chunk_extraction(result.extraction, chunk_size=160, overlap=0)
        if item.metadata["content_kind"] == "table"
    )
    assert chunk.text.startswith("表1: 経費明細\n| 項目 | 金額 |")
    assert chunk.metadata["table_caption"] == "表1: 経費明細"
    assert chunk.metadata["table_id"] == "html-table-0000"


def test_parser_registry_preserves_html_table_rowspan_colspan() -> None:
    """HTML table の rowspan/colspan は table cell metadata と chunk 形状へ残す。"""
    data = (
        "<html><body><h1>売上</h1>"
        "<table>"
        "<tr><th rowspan='2'>地域</th><th colspan='2'>売上</th></tr>"
        "<tr><th>2025</th><th>2026</th></tr>"
        "<tr><td>関西</td><td>1200</td><td>1500</td></tr>"
        "</table></body></html>"
    ).encode()
    profile = build_source_profile(
        original_file_name="span-table.html",
        sanitized_file_name="span-table.html",
        content_type="text/html",
        file_size_bytes=len(data),
        content_sha256="4" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type="text/html")

    assert result.extraction is not None
    table = result.extraction.tables[0]
    assert table.metadata["row_count"] == 3
    assert table.metadata["column_count"] == 3
    cells = table.cells
    assert [(cell.row, cell.col, cell.text, cell.row_span, cell.col_span) for cell in cells] == [
        (0, 0, "地域", 2, 1),
        (0, 1, "売上", 1, 2),
        (1, 1, "2025", 1, 1),
        (1, 2, "2026", 1, 1),
        (2, 0, "関西", 1, 1),
        (2, 1, "1200", 1, 1),
        (2, 2, "1500", 1, 1),
    ]
    table_element = result.extraction.elements[1]
    assert "| 地域 | 売上 |  |" in table_element.text
    table_chunk = next(
        chunk
        for chunk in chunk_extraction(result.extraction, chunk_size=120, overlap=0)
        if chunk.metadata["content_kind"] == "table"
    )
    assert table_chunk.metadata["table_row_count"] == 3
    assert table_chunk.metadata["table_column_count"] == 3


def test_parser_registry_preserves_html_code_block_language_in_chunks() -> None:
    """HTML pre/code は text ではなく code element と language metadata として保持する。"""
    data = (
        b"<html><body><h1>Runbook</h1>"
        b"<pre><code class='language-sql'>select 1\nfrom dual;</code></pre>"
        b"</body></html>"
    )
    profile = build_source_profile(
        original_file_name="runbook.html",
        sanitized_file_name="runbook.html",
        content_type="text/html",
        file_size_bytes=len(data),
        content_sha256="a" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type="text/html")

    assert result.extraction is not None
    code_element = next(element for element in result.extraction.elements if element.kind == "code")
    assert code_element.content_kind == "code"
    assert code_element.source_parser == "local_html_semantic"
    assert code_element.metadata["code_language"] == "sql"
    chunks = chunk_extraction(result.extraction, chunk_size=120, overlap=0)
    code_chunk = next(chunk for chunk in chunks if chunk.metadata["content_kind"] == "code")
    assert code_chunk.metadata["code_language"] == "sql"
    assert "select 1" in code_chunk.text


def test_parser_registry_preserves_html_figure_caption_dependency() -> None:
    """HTML figure/figcaption は parent-child lineage として chunk metadata まで残す。"""
    data = (
        "<html><body><h1>構成</h1>"
        "<figure>RAG フロー図<figcaption>図1: citation の対応</figcaption></figure>"
        "</body></html>"
    ).encode()
    profile = build_source_profile(
        original_file_name="figure.html",
        sanitized_file_name="figure.html",
        content_type="text/html",
        file_size_bytes=len(data),
        content_sha256="4" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type="text/html")

    assert result.extraction is not None
    kinds = [element.kind for element in result.extraction.elements]
    assert kinds == ["title", "figure", "figure_caption"]
    figure = result.extraction.elements[1]
    caption = result.extraction.elements[2]
    assert caption.parent_id == figure.element_id
    chunks = chunk_extraction(result.extraction, chunk_size=120, overlap=0)
    figure_chunk = next(chunk for chunk in chunks if chunk.metadata["content_kind"] == "figure")
    assert figure_chunk.metadata["parent_element_ids"] == figure.element_id
    assert figure_chunk.metadata["dependency_edge_count"] == 1


def test_parser_registry_promotes_html_images_to_assets_and_citation_metadata() -> None:
    """HTML img は image asset と figure citation metadata として保持する。"""
    data = (
        "<html><body><h1>構成</h1>"
        "<figure><img src='/assets/flow.png' alt='検索フロー図'>"
        "<figcaption>図1: citation の対応</figcaption></figure>"
        "<h2>別図</h2>"
        "<img src='javascript:alert(1)' alt='危険画像'>"
        "</body></html>"
    ).encode()
    profile = build_source_profile(
        original_file_name="image-figure.html",
        sanitized_file_name="image-figure.html",
        content_type="text/html",
        file_size_bytes=len(data),
        content_sha256="9" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type="text/html")

    assert result.extraction is not None
    assert result.extraction.parser_artifacts["asset_count"] == 2
    figure = result.extraction.elements[1]
    caption = result.extraction.elements[2]
    unsafe_figure = result.extraction.elements[4]
    assert figure.kind == "figure"
    assert figure.text == "検索フロー図"
    assert figure.metadata["asset_id"] == "html-image-0000"
    assert figure.metadata["link_urls"] == "/assets/flow.png"
    assert caption.parent_id == figure.element_id
    assert unsafe_figure.metadata["asset_id"] == "html-image-0001"
    assert "link_urls" not in unsafe_figure.metadata
    safe_asset, unsafe_asset = result.extraction.assets
    assert safe_asset.asset_id == "html-image-0000"
    assert safe_asset.kind == "image"
    assert safe_asset.alt_text == "検索フロー図"
    assert safe_asset.metadata["source_url"] == "/assets/flow.png"
    assert unsafe_asset.asset_id == "html-image-0001"
    assert "source_url" not in unsafe_asset.metadata
    chunks = chunk_extraction(result.extraction, chunk_size=120, overlap=0)
    figure_chunk = next(
        chunk for chunk in chunks if chunk.metadata.get("asset_id") == "html-image-0000"
    )
    assert figure_chunk.metadata["content_kind"] == "figure"
    assert figure_chunk.metadata["link_urls"] == "/assets/flow.png"
    assert figure_chunk.metadata["dependency_edge_count"] == 1


def test_parser_registry_preserves_html_link_lineage_in_chunks() -> None:
    """HTML anchor は安全な URL と表示 text を chunk/citation metadata へ残す。"""
    data = (
        "<html><body><h1>参考資料</h1>"
        "<p>詳細は<a href='https://example.com/spec?q=rag,table'>仕様</a>を確認します。"
        "<a href='javascript:alert(1)'>危険リンク</a></p>"
        "</body></html>"
    ).encode()
    profile = build_source_profile(
        original_file_name="links.html",
        sanitized_file_name="links.html",
        content_type="text/html",
        file_size_bytes=len(data),
        content_sha256="5" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type="text/html")

    assert result.extraction is not None
    text_element = result.extraction.elements[1]
    assert text_element.metadata["link_count"] == 1
    assert text_element.metadata["link_urls"] == "https://example.com/spec?q=rag,table"
    assert text_element.metadata["link_texts"] == "仕様"
    assert "危険リンク" in text_element.text
    chunks = chunk_extraction(result.extraction, chunk_size=160, overlap=0)
    link_chunk = next(chunk for chunk in chunks if "仕様" in chunk.text)
    assert link_chunk.metadata["link_count"] == 1
    assert link_chunk.metadata["link_urls"] == "https://example.com/spec?q=rag,table"
    assert link_chunk.metadata["link_texts"] == "仕様"


def test_parser_registry_preserves_email_parts_and_attachment_metadata() -> None:
    """email は headers/body を要素化し、添付は原文なし metadata asset として保持する。"""
    message = EmailMessage()
    message["Subject"] = "契約更新"
    message["From"] = "alice@example.com"
    message["To"] = "bob@example.com"
    message["Date"] = "Wed, 17 Jun 2026 10:00:00 +0900"
    message.set_content("本文です。添付を確認してください。")
    message.add_attachment(
        b"secret attachment bytes",
        maintype="application",
        subtype="pdf",
        filename="contract.pdf",
    )
    data = message.as_bytes()
    profile = build_source_profile(
        original_file_name="mail.eml",
        sanitized_file_name="mail.eml",
        content_type="message/rfc822",
        file_size_bytes=len(data),
        content_sha256="4" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type="message/rfc822")

    assert result.extraction is not None
    assert result.extraction.parser_artifacts["attachment_count"] == 1
    assert [element.element_id for element in result.extraction.elements] == [
        "email-headers",
        "email-body",
    ]
    headers, body = result.extraction.elements
    assert headers.content_kind == "email"
    assert headers.metadata["email_part"] == "headers"
    assert headers.metadata["subject_chars"] == 4
    assert "Subject: 契約更新" in headers.text
    assert body.metadata["email_part"] == "body"
    assert "本文です" in body.text
    asset = result.extraction.assets[0]
    assert asset.asset_id == "email-attachment-0000"
    assert asset.kind == "email_attachment"
    assert asset.alt_text == "contract.pdf"
    assert asset.metadata["file_name"] == "contract.pdf"
    assert asset.metadata["content_type"] == "application/pdf"
    assert asset.metadata["size_bytes"] == len(b"secret attachment bytes")
    assert "secret attachment bytes" not in str(result.extraction.to_document_payload())


def test_parser_registry_marks_outlook_msg_as_unsupported_until_parser_exists() -> None:
    """Outlook MSG は RFC822 parser へ誤投入せず、明示的な未対応として返す。"""
    data = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1outlook msg"
    profile = build_source_profile(
        original_file_name="approval.msg",
        sanitized_file_name="approval.msg",
        content_type="application/octet-stream",
        file_size_bytes=len(data),
        content_sha256="9" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
    )

    assert profile.modality == SourceModality.EMAIL
    assert profile.parser_backend == "unsupported"
    assert profile.parser_profile == "unsupported_outlook_msg"
    assert profile.preview_kind == "unsupported"
    assert profile.unsupported_reason == "outlook_msg_not_supported"
    assert "unsupported_outlook_msg" in profile.quality_warnings
    assert result.extraction is None
    assert result.parser_backend == "unsupported"
    assert result.unsupported_reason == "outlook_msg_not_supported"
    assert "unsupported_outlook_msg" in result.warnings


def test_source_profile_accepts_webp_as_enterprise_ai_image() -> None:
    """WEBP は upload whitelist / SourceProfile / Enterprise AI image payload を揃える。"""
    data = b"RIFF....WEBP"
    profile = build_source_profile(
        original_file_name="receipt.webp",
        sanitized_file_name="receipt.webp",
        content_type="image/webp",
        file_size_bytes=len(data),
        content_sha256="8" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type=profile.content_type)

    assert profile.modality == SourceModality.IMAGE
    assert profile.parser_profile == "enterprise_ai_image_ocr"
    assert profile.parser_backend == "enterprise_ai"
    assert profile.preview_kind == "image"
    assert profile.unsupported_reason is None
    assert result.extraction is None
    assert result.parser_backend == "enterprise_ai"


def test_parser_registry_marks_tiff_image_as_unsupported_until_conversion_exists() -> None:
    """TIFF はブラウザ preview / Enterprise AI image payload 未整合のため明示的に止める。"""
    data = b"II*\x00tiff"
    profile = build_source_profile(
        original_file_name="scan.tiff",
        sanitized_file_name="scan.tiff",
        content_type="application/octet-stream",
        file_size_bytes=len(data),
        content_sha256="7" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type=profile.content_type)

    assert profile.modality == SourceModality.IMAGE
    assert profile.parser_profile == "unsupported_tiff_image"
    assert profile.parser_backend == "unsupported"
    assert profile.preview_kind == "unsupported"
    assert profile.unsupported_reason == "tiff_image_not_supported"
    assert "unsupported_tiff_image" in profile.quality_warnings
    assert result.extraction is None
    assert result.parser_backend == "unsupported"
    assert result.unsupported_reason == "tiff_image_not_supported"
    assert "unsupported_tiff_image" in result.warnings


def test_parser_registry_partitions_openxml_office_sources() -> None:
    """docx/pptx/xlsx は標準ライブラリ adapter で最低限の text block にする。"""
    cases = [
        (
            "doc.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            _docx_bytes("Word 本文"),
            "office_document",
        ),
        (
            "slides.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            _pptx_bytes("Slide 本文"),
            "office_slide",
        ),
        (
            "book.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            _xlsx_bytes("Sheet 本文"),
            "office_sheet",
        ),
    ]

    for file_name, content_type, data, template in cases:
        profile = build_source_profile(
            original_file_name=file_name,
            sanitized_file_name=file_name,
            content_type=content_type,
            file_size_bytes=len(data),
            content_sha256="b" * 64,
            data=data,
        )

        result = parse_with_registry(data, source_profile=profile, content_type=content_type)

        assert profile.modality == SourceModality.OFFICE
        assert profile.parser_profile == "local_office_structure"
        assert profile.parser_backend == "local_partition"
        assert result.parser_backend == "local_partition"
        assert result.template == template
        assert result.extraction is not None
        assert "本文" in result.extraction.raw_text


def test_source_profile_marks_legacy_office_as_unsupported() -> None:
    """旧バイナリ Office は local parser 対応済みのように表示しない。"""
    data = b"legacy office"
    profile = build_source_profile(
        original_file_name="legacy.doc",
        sanitized_file_name="legacy.doc",
        content_type="application/msword",
        file_size_bytes=len(data),
        content_sha256="9" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type=profile.content_type)

    assert profile.modality == SourceModality.OFFICE
    assert profile.parser_profile == "unsupported_legacy_office_binary"
    assert profile.parser_backend == "unsupported"
    assert profile.preview_kind == "unsupported"
    assert profile.unsupported_reason == "legacy_office_binary_not_supported"
    assert "unsupported_legacy_office_binary" in profile.quality_warnings
    assert result.extraction is None
    assert result.parser_backend == "unsupported"
    assert result.unsupported_reason == "legacy_office_binary_not_supported"
    assert "unsupported_legacy_office_binary" in result.warnings


def test_parser_registry_uses_openxml_content_type_when_extension_is_missing() -> None:
    """拡張子がなくても OpenXML MIME が正しければ local Office parser に合わせる。"""
    data = _docx_bytes("MIME 判定本文")
    content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    profile = build_source_profile(
        original_file_name="upload",
        sanitized_file_name="upload",
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256="8" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type=content_type)

    assert profile.extension is None
    assert profile.parser_profile == "local_office_structure"
    assert profile.parser_backend == "local_partition"
    assert result.parser_backend == "local_partition"
    assert result.extraction is not None
    assert "MIME 判定本文" in result.extraction.raw_text


def test_parser_registry_preserves_xlsx_table_cells() -> None:
    """XLSX は markdown text だけでなく tables/cells も保持する。"""
    data = _xlsx_table_bytes([["項目", "金額"], ["交通費", "1200"]])
    content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    profile = build_source_profile(
        original_file_name="book.xlsx",
        sanitized_file_name="book.xlsx",
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256="6" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type=content_type)
    segments = parse_openxml_office_segment_extractions(data, source_profile=profile)

    assert result.extraction is not None
    assert result.extraction.tables
    table = result.extraction.tables[0]
    assert table.table_id == "xlsx-sheet-1"
    assert table.page_number == 1
    assert table.metadata["row_count"] == 2
    assert table.metadata["column_count"] == 2
    assert [(cell.row, cell.col, cell.text) for cell in table.cells] == [
        (0, 0, "項目"),
        (0, 1, "金額"),
        (1, 0, "交通費"),
        (1, 1, "1200"),
    ]
    assert len(segments.segments) == 1
    assert segments.segments[0].extraction.tables[0].cells[3].text == "1200"


def test_parser_registry_preserves_xlsx_formula_lineage() -> None:
    """XLSX formula cell は table metadata と equation chunk に残す。"""
    data = _xlsx_formula_table_bytes()
    content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    profile = build_source_profile(
        original_file_name="formula.xlsx",
        sanitized_file_name="formula.xlsx",
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256="7" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type=content_type)
    segments = parse_openxml_office_segment_extractions(data, source_profile=profile)

    assert result.extraction is not None
    table = result.extraction.tables[0]
    assert table.metadata["formula_count"] == 1
    assert table.metadata["formula_cell_refs"] == "B2"
    assert "B2=SUM(A2:A2)" in str(table.metadata["formula_cells"])
    assert [(cell.row, cell.col, cell.text) for cell in table.cells] == [
        (0, 0, "項目"),
        (0, 1, "合計"),
        (1, 0, "1200"),
        (1, 1, "1200"),
    ]
    formula_cell = table.cells[3]
    assert formula_cell.metadata["formula_cell_ref"] == "B2"
    assert formula_cell.metadata["equation_format"] == "excel_formula"
    assert formula_cell.metadata["formula"] == "SUM(A2:A2)"
    assert formula_cell.metadata["formula_value"] == "1200"
    formula = next(
        element for element in result.extraction.elements if element.content_kind == "equation"
    )
    assert formula.kind == "equation"
    assert formula.text == "B2 = SUM(A2:A2) (値: 1200)"
    assert formula.parent_id == "xlsx-sheet-1"
    assert formula.metadata["equation_format"] == "excel_formula"
    assert formula.metadata["formula_cell_ref"] == "B2"
    chunks = chunk_extraction(result.extraction, chunk_size=120, overlap=0)
    table_chunk = next(chunk for chunk in chunks if chunk.metadata["content_kind"] == "table")
    assert table_chunk.metadata["formula_count"] == 1
    assert table_chunk.metadata["formula_cell_count"] == 1
    assert table_chunk.metadata["formula_cell_refs"] == "B2"
    assert "B2=SUM(A2:A2)" in str(table_chunk.metadata["formula_cells"])
    formula_chunk = next(chunk for chunk in chunks if chunk.metadata["content_kind"] == "equation")
    assert formula_chunk.metadata["equation_format"] == "excel_formula"
    assert formula_chunk.metadata["formula_count"] == 1
    assert formula_chunk.metadata["formula_cell_count"] == 1
    assert formula_chunk.metadata["formula_cell_refs"] == "B2"
    assert formula_chunk.metadata["formula_cell_row"] == 1
    assert formula_chunk.metadata["formula_cell_col"] == 1
    assert formula_chunk.metadata["formula_value"] == "1200"
    assert formula_chunk.metadata["parent_element_ids"] == "xlsx-sheet-1"
    quality = build_ingestion_quality_report(result.extraction, source_profile=profile)
    assert quality.formula_count == 1
    assert "formula_review" in quality.quality_warnings
    assert len(segments.segments) == 1
    segment_formula = next(
        element
        for element in segments.segments[0].extraction.elements
        if element.content_kind == "equation"
    )
    assert segment_formula.metadata["formula_cell_ref"] == "B2"
    assert segments.segments[0].extraction.parser_artifacts["formula_count"] == 1


def test_parser_registry_preserves_docx_table_cells() -> None:
    """DOCX table は paragraph 順を保ち、tables/cells を保持する。"""
    data = _docx_table_bytes(
        before="契約条件",
        rows=[["項目", "値"], ["期間", "1年"]],
        after="以上",
    )
    content_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    profile = build_source_profile(
        original_file_name="contract.docx",
        sanitized_file_name="contract.docx",
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256="5" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type=content_type)

    assert result.extraction is not None
    assert result.extraction.raw_text.splitlines() == [
        "契約条件",
        "| 項目 | 値 |",
        "| 期間 | 1年 |",
        "以上",
    ]
    table = result.extraction.tables[0]
    assert table.table_id == "docx-table-0000"
    assert table.metadata["row_count"] == 2
    assert table.metadata["column_count"] == 2
    assert [(cell.row, cell.col, cell.text) for cell in table.cells] == [
        (0, 0, "項目"),
        (0, 1, "値"),
        (1, 0, "期間"),
        (1, 1, "1年"),
    ]


def test_parser_registry_preserves_pptx_table_cells() -> None:
    """PPTX slide table は slide/page lineage 付きで cells を保持する。"""
    data = _pptx_table_bytes([["項目", "値"], ["進捗", "90%"]])
    content_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    profile = build_source_profile(
        original_file_name="deck.pptx",
        sanitized_file_name="deck.pptx",
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256="7" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type=content_type)
    segments = parse_openxml_office_segment_extractions(data, source_profile=profile)

    assert result.extraction is not None
    table = result.extraction.tables[0]
    assert table.table_id == "pptx-slide-1-table-0000"
    assert table.page_number == 1
    assert table.metadata["office_segment_number"] == 1
    assert table.metadata["row_count"] == 2
    assert table.metadata["column_count"] == 2
    assert [(cell.row, cell.col, cell.text) for cell in table.cells] == [
        (0, 0, "項目"),
        (0, 1, "値"),
        (1, 0, "進捗"),
        (1, 1, "90%"),
    ]
    assert len(segments.segments) == 1
    assert segments.segments[0].extraction.tables[0].cells[3].text == "90%"


def test_parser_registry_records_safe_fallback_for_corrupted_office() -> None:
    """壊れた Office は fallback_used と warning を残して Enterprise AI 側へ渡す。"""
    data = b"not a zip"
    profile = build_source_profile(
        original_file_name="broken.docx",
        sanitized_file_name="broken.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size_bytes=len(data),
        content_sha256="c" * 64,
        data=data,
    )

    result = parse_with_registry(data, source_profile=profile, content_type=profile.content_type)

    assert result.extraction is None
    assert result.parser_backend == "enterprise_ai"
    assert result.fallback_used is True
    assert "office_local_parse_failed" in result.warnings


def test_parser_registry_records_external_adapter_unavailable_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docling/Unstructured flags は未導入環境でも安全に local fallback する。

    Docling は PDF と画像だけを受けるため(#286)、Markdown は Unstructured で確かめる。
    """

    def unavailable(name: str) -> bool:
        return False

    monkeypatch.setattr("rag_parser_core.registry._module_available", unavailable)
    data = b"# Title\nBody"
    profile = build_source_profile(
        original_file_name="memo.md",
        sanitized_file_name="memo.md",
        content_type="text/markdown",
        file_size_bytes=len(data),
        content_sha256="e" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.parser_backend == "local_partition"
    assert result.extraction is not None
    assert result.fallback_used is True
    assert "unstructured_adapter_package_missing" in result.warnings


def test_parser_registry_requires_adapter_feature_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """adapter backend を明示しても feature flag が false なら外部 parser は呼ばない。"""
    docling_module = ModuleType("docling")
    converter_module = ModuleType("docling.document_converter")

    class FakeDocumentConverter:
        def convert(self, path: str) -> object:
            _ = path
            raise AssertionError("feature flag が false の adapter は呼び出さない")

    converter_module.__dict__["DocumentConverter"] = FakeDocumentConverter
    monkeypatch.setitem(sys.modules, "docling", docling_module)
    monkeypatch.setitem(sys.modules, "docling.document_converter", converter_module)
    data = b"# Title\nBody"
    profile = build_source_profile(
        original_file_name="memo.md",
        sanitized_file_name="memo.md",
        content_type="text/markdown",
        file_size_bytes=len(data),
        content_sha256="8" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="docling",
    )

    assert result.parser_backend == "local_partition"
    assert result.extraction is not None
    assert result.fallback_used is True
    assert "docling_adapter_feature_flag_disabled" in result.warnings


def test_parser_registry_adapter_fallback_warning_affects_quality_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """adapter fallback は取込品質にも残し、parser fallback rate で追跡できる。"""

    monkeypatch.setattr("rag_parser_core.registry._module_available", lambda _name: False)
    data = b"<h1>Title</h1><p>Body</p>"
    profile = build_source_profile(
        original_file_name="page.html",
        sanitized_file_name="page.html",
        content_type="text/html",
        file_size_bytes=len(data),
        content_sha256="7" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.extraction is not None
    quality = build_ingestion_quality_report(
        result.extraction.model_copy(
            update={"warnings": [*result.extraction.warnings, *result.warnings]}
        ),
        source_profile=profile,
        parser_backend=result.parser_backend,
        parser_version=result.parser_version,
        fallback_used=result.fallback_used,
    )
    assert result.fallback_used is True
    assert quality.fallback_used is True
    assert quality.risk_level == "medium"
    assert "parser_fallback_used" in quality.quality_warnings
    assert "unstructured_adapter_package_missing" in quality.quality_warnings


def test_parser_registry_unsupported_explicit_adapter_skips_simple_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未対応の明示 adapter は単純 text/markdown では呼ばず local parser を使う。"""

    def fail_if_checked(name: str) -> bool:
        raise AssertionError(f"text source should not probe external adapter: {name}")

    monkeypatch.setattr("rag_parser_core.registry._module_available", fail_if_checked)
    data = b"# Runbook\nBody"
    profile = build_source_profile(
        original_file_name="runbook.md",
        sanitized_file_name="runbook.md",
        content_type="text/markdown",
        file_size_bytes=len(data),
        content_sha256="1" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="mineru",
        docling_enabled=True,
        unstructured_enabled=True,
        mineru_enabled=True,
    )

    assert result.parser_backend == "local_partition"
    assert result.fallback_used is True
    assert result.warnings == ("mineru_adapter_source_unsupported",)
    assert result.extraction is not None
    assert result.extraction.parser_artifacts["chunk_template"] == "markdown_by_heading"


@pytest.mark.parametrize(
    ("file_name", "content_type"),
    [("guide.rst", "text/x-rst"), ("notes.org", "text/org")],
    ids=["rst", "org"],
)
def test_parser_registry_unstructured_reads_rst_org_as_plain_text(
    monkeypatch: pytest.MonkeyPatch, file_name: str, content_type: str
) -> None:
    """.rst / .org は pandoc(入れない)へ回さず、プレーンテキストとして渡す(#3351)。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "9.9.0"
    partition_module = ModuleType("unstructured.partition.auto")
    calls: list[dict[str, object]] = []

    def partition(**kwargs: object) -> list[object]:
        calls.append(kwargs)
        return [SimpleNamespace(text="Body", category="NarrativeText", metadata=None)]

    partition_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", ModuleType("unstructured.partition"))
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", partition_module)
    data = b"Title\n=====\n\nBody\n"
    profile = build_source_profile(
        original_file_name=file_name,
        sanitized_file_name=file_name,
        content_type=content_type,
        file_size_bytes=len(data),
        content_sha256="3" * 64,
        data=data,
    )
    assert profile.modality == SourceModality.TEXT

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        docling_enabled=False,
        unstructured_enabled=True,
    )

    assert result.parser_backend == "unstructured"
    assert result.extraction is not None
    assert [str(call["filename"]).rsplit(".", 1)[-1] for call in calls] == ["txt"]
    assert calls[0]["content_type"] == "text/plain"
    # unstructured の PDF・画像の解析が import する pi_heif は、入れなくても import できる。
    assert hasattr(sys.modules["pi_heif"], "register_heif_opener")


@pytest.mark.parametrize(
    ("file_name", "content_type"),
    [("memo.odt", "application/vnd.oasis.opendocument.text"), ("memo.rtf", "text/rtf")],
    ids=["odt", "rtf"],
)
def test_parser_registry_does_not_parse_odt_rtf_original(file_name: str, content_type: str) -> None:
    """ODT・RTF は原本のままでは解析しない(Office→PDF で PDF にしてから読む。#3351)。"""
    profile = build_source_profile(
        original_file_name=file_name,
        sanitized_file_name=file_name,
        content_type=content_type,
        file_size_bytes=4,
        content_sha256="4" * 64,
    )

    result = parse_with_registry(
        b"{\\rtf1}",
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.parser_backend == "unsupported"
    assert result.extraction is None
    assert result.unsupported_reason == "odt_rtf_not_supported"
    assert result.warnings == ("unsupported_odt_rtf",)


def test_parser_registry_explicit_unstructured_routes_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """email は明示選択された Unstructured partition で解析する。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "9.9.0"
    partition_package = ModuleType("unstructured.partition")
    partition_module = ModuleType("unstructured.partition.auto")

    def partition(**kwargs: object) -> list[object]:
        assert str(kwargs["filename"]).endswith(".eml")
        return [
            SimpleNamespace(
                text="Subject: Hello\nBody",
                category="EmailMessage",
                metadata=SimpleNamespace(page_number=None),
            )
        ]

    partition_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", partition_module)
    monkeypatch.setattr(
        "rag_parser_core.registry._module_available",
        lambda name: name == "unstructured",
    )
    data = b"Subject: Hello\nFrom: a@example.com\nTo: b@example.com\n\nBody"
    profile = build_source_profile(
        original_file_name="mail.eml",
        sanitized_file_name="mail.eml",
        content_type="message/rfc822",
        file_size_bytes=len(data),
        content_sha256="2" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        docling_enabled=False,
        unstructured_enabled=True,
    )

    assert result.parser_backend == "unstructured"
    assert result.parser_version == "9.9.0"
    assert result.fallback_used is False
    assert result.warnings == ()
    assert result.extraction is not None
    assert result.extraction.parser_artifacts["external_adapter"] == "unstructured"
    element = result.extraction.elements[0]
    assert element.content_kind == "email"
    assert element.page_number == 1
    assert element.metadata["email_part"] == "headers"
    assert result.extraction.parser_artifacts["email_lineage_normalized"] is True


def test_parser_registry_explicit_docling_does_not_take_office(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docling は PDF と画像だけを受ける(#286)。Office は Docling へ渡さない。

    Office は ファイル準備の office_to_pdf で PDF にするか、Unstructured を選ぶ。
    """
    docling_module = ModuleType("docling")
    docling_module.__dict__["__version__"] = "3.0.0"
    converter_module = ModuleType("docling.document_converter")

    class FakeDocument:
        def export_to_markdown(self) -> str:
            return "# Office Docling\n本文"

    class FakeDocumentConverter:
        def convert(self, path: str) -> object:
            raise AssertionError(f"Docling に Office を渡した: {path}")

    converter_module.__dict__["DocumentConverter"] = FakeDocumentConverter
    monkeypatch.setitem(sys.modules, "docling", docling_module)
    monkeypatch.setitem(sys.modules, "docling.document_converter", converter_module)
    monkeypatch.setattr(
        "rag_parser_core.registry._module_available", lambda name: name == "docling"
    )
    data = _docx_bytes("本文")
    profile = build_source_profile(
        original_file_name="report.docx",
        sanitized_file_name="report.docx",
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        file_size_bytes=len(data),
        content_sha256="3" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="docling",
        docling_enabled=True,
        unstructured_enabled=False,
    )

    assert result.parser_backend != "docling"
    assert "docling_adapter_source_unsupported" in result.warnings


def test_image_adapter_without_bbox_gets_full_frame_source_asset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bbox を返さない image adapter でも source image asset で preview lineage を残す。"""
    docling_module = ModuleType("docling")
    docling_module.__dict__["__version__"] = "3.0.0"
    converter_module = ModuleType("docling.document_converter")

    class FakeDocument:
        def export_to_markdown(self) -> str:
            return "TOTAL 1000 JPY"

    class FakeDocumentConverter:
        def convert(self, path: str) -> object:
            assert path.endswith(".png")
            return SimpleNamespace(document=FakeDocument())

    converter_module.__dict__["DocumentConverter"] = FakeDocumentConverter
    monkeypatch.setitem(sys.modules, "docling", docling_module)
    monkeypatch.setitem(sys.modules, "docling.document_converter", converter_module)

    data = b"\x89PNG\r\n\x1a\n"
    profile = build_source_profile(
        original_file_name="receipt.png",
        sanitized_file_name="receipt.png",
        content_type="image/png",
        file_size_bytes=len(data),
        content_sha256="5" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="docling",
        docling_enabled=True,
    )

    assert result.parser_backend == "docling"
    assert result.extraction is not None
    assert result.extraction.parser_artifacts["source_image_full_frame_asset_count"] == 1
    asset = result.extraction.assets[0]
    assert asset.asset_id == "source-image-0000"
    assert asset.kind == "source_image"
    assert asset.page_number == 1
    assert asset.bbox == [0.0, 0.0, 1.0, 1.0]
    assert asset.metadata["bbox_scope"] == "source_image_full_frame"
    assert asset.metadata["bbox_unit"] == "ratio"


def test_parser_registry_explicit_mineru_rejects_html_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """実装済み MinerU adapter が扱えない source は warning を残して local fallback する。"""

    def fail_if_checked(name: str) -> bool:
        raise AssertionError(f"unsupported mineru source should not import adapter: {name}")

    monkeypatch.setattr("rag_parser_core.registry._module_available", fail_if_checked)
    data = b"<h1>Title</h1><p>Body</p>"
    profile = build_source_profile(
        original_file_name="page.html",
        sanitized_file_name="page.html",
        content_type="text/html",
        file_size_bytes=len(data),
        content_sha256="4" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="mineru",
        mineru_enabled=True,
    )

    assert result.parser_backend == "local_partition"
    assert result.fallback_used is True
    assert "mineru_adapter_source_unsupported" in result.warnings
    assert result.extraction is not None
    assert result.extraction.parser_artifacts["chunk_template"] == "html_semantic"


def test_parser_registry_uses_docling_adapter_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docling adapter が使える場合は共通 schema に remap して返す。"""
    docling_module = ModuleType("docling")
    docling_module.__dict__["__version__"] = "1.2.3"
    converter_module = ModuleType("docling.document_converter")

    class FakeDocument:
        pages = {"1": object(), "2": object()}
        tables = [object()]

        def export_to_markdown(self) -> str:
            return "# Docling\n本文"

    class FakeDocumentConverter:
        def convert(self, path: str) -> object:
            assert path.endswith(".pdf")
            return SimpleNamespace(document=FakeDocument())

    converter_module.__dict__["DocumentConverter"] = FakeDocumentConverter
    monkeypatch.setitem(sys.modules, "docling", docling_module)
    monkeypatch.setitem(sys.modules, "docling.document_converter", converter_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="sample.pdf",
        sanitized_file_name="sample.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="f" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="docling",
        docling_enabled=True,
    )

    assert result.parser_backend == "docling"
    assert result.parser_version == "1.2.3"
    assert result.template == "pdf_layout"
    assert result.extraction is not None
    assert "Docling" in result.extraction.raw_text
    assert result.extraction.parser_artifacts["parser_backend"] == "docling"
    assert result.extraction.parser_artifacts["page_count"] == 2
    assert result.extraction.parser_artifacts["table_count"] == 1


def test_docling_adapter_remaps_structured_text_and_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docling の block/table 出力は flat markdown に潰さず共通 schema へ remap する。"""
    docling_module = ModuleType("docling")
    docling_module.__dict__["__version__"] = "2.0.0"
    converter_module = ModuleType("docling.document_converter")

    class FakeDocument:
        pages = {"1": object(), "2": object()}
        texts = [
            SimpleNamespace(
                id="title-1",
                category="Title",
                text="請求書",
                metadata=SimpleNamespace(page_number=1, heading_level=1),
            ),
            SimpleNamespace(
                category="NarrativeText",
                text="支払条件です。",
                metadata=SimpleNamespace(parent_id="title-1"),
            ),
        ]
        tables = [
            SimpleNamespace(
                kind="table",
                rows=[["項目", "金額"], ["交通費", "1000円"]],
                metadata=SimpleNamespace(
                    page_number=2,
                    bbox=[0, 0, 50, 20],
                    parent_id="title-1",
                    section_path=["請求書", "明細"],
                ),
            )
        ]

        def export_to_markdown(self) -> str:
            return "# 請求書\n支払条件です。"

    class FakeDocumentConverter:
        def convert(self, path: str) -> object:
            assert path.endswith(".pdf")
            return SimpleNamespace(document=FakeDocument())

    converter_module.__dict__["DocumentConverter"] = FakeDocumentConverter
    monkeypatch.setitem(sys.modules, "docling", docling_module)
    monkeypatch.setitem(sys.modules, "docling.document_converter", converter_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="invoice.pdf",
        sanitized_file_name="invoice.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="3" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="docling",
        docling_enabled=True,
    )

    assert result.parser_backend == "docling"
    assert result.extraction is not None
    assert [element.kind for element in result.extraction.elements] == [
        "title",
        "text",
        "table",
    ]
    title_element = result.extraction.elements[0]
    text_element = result.extraction.elements[1]
    assert title_element.element_id == "title-1"
    assert title_element.section_path == ["請求書"]
    assert text_element.parent_id == "title-1"
    assert text_element.section_path == ["請求書"]
    table_element = result.extraction.elements[2]
    assert table_element.content_kind == "table"
    assert table_element.parent_id == "title-1"
    assert table_element.section_path == ["請求書", "明細"]
    assert table_element.page_number == 2
    assert table_element.bbox == [0.0, 0.0, 50.0, 20.0]
    assert table_element.metadata["bbox_coordinate_mode"] == "xyxy"
    assert table_element.metadata["bbox_unit"] == "percent"
    assert result.extraction.parser_artifacts["adapter_export"] == "structured_elements"
    assert result.extraction.parser_artifacts["adapter_element_count"] == 3
    table = result.extraction.tables[0]
    assert table.page_number == 2
    assert [(cell.row, cell.col, cell.text) for cell in table.cells] == [
        (0, 0, "項目"),
        (0, 1, "金額"),
        (1, 0, "交通費"),
        (1, 1, "1000円"),
    ]


def test_docling_adapter_recursively_flattens_nested_page_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Docling の page/group 配下 block は page lineage を継承して schema へ remap する。"""
    docling_module = ModuleType("docling")
    docling_module.__dict__["__version__"] = "2.2.0"
    converter_module = ModuleType("docling.document_converter")

    class FakeTitle:
        id = "nested-title"
        category = "Title"
        text = "運用手順"

    class FakeParagraph:
        id = "nested-body"
        category = "NarrativeText"
        text = "検索結果を確認します。"

    class FakeTable:
        id = "nested-table"
        category = "Table"
        cells = [
            {"row": 0, "col": 0, "text": "状態"},
            {"row": 0, "col": 1, "text": "件数"},
            {"row": 1, "col": 0, "text": "INDEXED"},
            {"row": 1, "col": 1, "text": "3"},
        ]

    class FakeGroup:
        children = [FakeParagraph(), FakeTable()]

    class FakePage:
        page_no = 2
        metadata = SimpleNamespace(section_path=["管理ガイド", "検索"])
        children = [FakeTitle(), FakeGroup()]

    class FakeDocument:
        pages = {"2": FakePage()}

    class FakeDocumentConverter:
        def convert(self, path: str) -> object:
            assert path.endswith(".pdf")
            return SimpleNamespace(document=FakeDocument())

    converter_module.__dict__["DocumentConverter"] = FakeDocumentConverter
    monkeypatch.setitem(sys.modules, "docling", docling_module)
    monkeypatch.setitem(sys.modules, "docling.document_converter", converter_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="nested-docling.pdf",
        sanitized_file_name="nested-docling.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="4" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="docling",
        docling_enabled=True,
    )

    assert result.parser_backend == "docling"
    assert result.extraction is not None
    assert [element.element_id for element in result.extraction.elements] == [
        "nested-title",
        "nested-body",
        "nested-table",
    ]
    assert {element.page_number for element in result.extraction.elements} == {2}
    assert all(
        element.section_path == ["管理ガイド", "検索"] for element in result.extraction.elements
    )
    table = result.extraction.tables[0]
    assert table.page_number == 2
    assert [(cell.row, cell.col, cell.text) for cell in table.cells] == [
        (0, 0, "状態"),
        (0, 1, "件数"),
        (1, 0, "INDEXED"),
        (1, 1, "3"),
    ]
    chunks = chunk_extraction(result.extraction, chunk_size=80, overlap=0)
    table_chunk = next(chunk for chunk in chunks if chunk.metadata["content_kind"] == "table")
    assert table_chunk.metadata["page_start"] == 2
    assert table_chunk.metadata["section_path"] == "管理ガイド > 検索"


def test_docling_adapter_keeps_section_container_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Section 自体が children を持つ場合も親 element と dependency lineage を保持する。"""
    docling_module = ModuleType("docling")
    docling_module.__dict__["__version__"] = "2.3.0"
    converter_module = ModuleType("docling.document_converter")

    class FakeParagraph:
        id = "sec-body"
        category = "NarrativeText"
        text = "検索結果の根拠を確認します。"

    class FakeSection:
        id = "sec-ops"
        category = "Section"
        text = "運用確認"
        metadata = SimpleNamespace(heading_level=1)
        children = [FakeParagraph()]

    class FakePage:
        page_number = 1
        children = [FakeSection()]

    class FakeDocument:
        pages = {"1": FakePage()}

    class FakeDocumentConverter:
        def convert(self, path: str) -> object:
            assert path.endswith(".pdf")
            return SimpleNamespace(document=FakeDocument())

    converter_module.__dict__["DocumentConverter"] = FakeDocumentConverter
    monkeypatch.setitem(sys.modules, "docling", docling_module)
    monkeypatch.setitem(sys.modules, "docling.document_converter", converter_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="section-tree.pdf",
        sanitized_file_name="section-tree.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="5" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="docling",
        docling_enabled=True,
    )

    assert result.parser_backend == "docling"
    assert result.extraction is not None
    section, paragraph = result.extraction.elements
    assert section.element_id == "sec-ops"
    assert section.kind == "title"
    assert section.section_path == ["運用確認"]
    assert paragraph.element_id == "sec-body"
    assert paragraph.parent_id == "sec-ops"
    assert paragraph.page_number == 1
    assert paragraph.section_path == ["運用確認"]
    chunks = chunk_extraction(result.extraction, chunk_size=160, overlap=0)
    section_chunk = next(chunk for chunk in chunks if "検索結果の根拠" in chunk.text)
    assert section_chunk.metadata["element_ids"] == "sec-ops,sec-body"
    assert section_chunk.metadata["parent_element_ids"] == "sec-ops"
    assert section_chunk.metadata["dependency_edges"] == (
        '[{"child_id":"sec-body","parent_id":"sec-ops"}]'
    )


def test_docling_adapter_preserves_page_size_for_absolute_bbox_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """adapter page size は absolute bbox の preview/citation metadata へ伝播する。"""
    docling_module = ModuleType("docling")
    docling_module.__dict__["__version__"] = "2.4.0"
    converter_module = ModuleType("docling.document_converter")

    class FakeBlock:
        id = "abs-bbox-text"
        category = "NarrativeText"
        text = "絶対座標で定位する本文です。"
        bbox = [153, 198, 306, 396]

    class FakePage:
        page_no = 2
        label = "ii"
        size = SimpleNamespace(width=612, height=792)
        rotation = 0
        children = [FakeBlock()]

    class FakeDocument:
        pages = {"2": FakePage()}

    class FakeDocumentConverter:
        def convert(self, path: str) -> object:
            assert path.endswith(".pdf")
            return SimpleNamespace(document=FakeDocument())

    converter_module.__dict__["DocumentConverter"] = FakeDocumentConverter
    monkeypatch.setitem(sys.modules, "docling", docling_module)
    monkeypatch.setitem(sys.modules, "docling.document_converter", converter_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="absolute-bbox.pdf",
        sanitized_file_name="absolute-bbox.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="6" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="docling",
        docling_enabled=True,
    )

    assert result.extraction is not None
    page = result.extraction.pages[0]
    assert page.page_number == 2
    assert page.label == "ii"
    assert page.width == 612
    assert page.height == 792
    assert page.element_ids == ["abs-bbox-text"]
    element = result.extraction.elements[0]
    assert element.page_number == 2
    assert element.bbox == [153.0, 198.0, 306.0, 396.0]
    assert element.metadata["bbox_coordinate_mode"] == "xyxy"
    assert element.metadata["bbox_unit"] == "absolute"
    assert element.metadata["page_width"] == 612
    assert element.metadata["page_height"] == 792
    chunk = chunk_extraction(result.extraction, chunk_size=120, overlap=0)[0]
    assert chunk.metadata["bbox"] == "[153.0,198.0,306.0,396.0]"
    assert chunk.metadata["bbox_unit"] == "absolute"
    assert chunk.metadata["page_width"] == 612
    assert chunk.metadata["page_height"] == 792


def test_docling_adapter_keeps_cell_only_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """text/rows/category がない adapter table でも cells から schema へ remap する。"""
    docling_module = ModuleType("docling")
    docling_module.__dict__["__version__"] = "2.1.0"
    converter_module = ModuleType("docling.document_converter")

    class FakeGridBlock:
        id = "cell-grid-1"
        metadata = SimpleNamespace(
            page_number=3,
            bbox={"left": 1, "top": 2, "right": 90, "bottom": 40},
        )
        cells = [
            SimpleNamespace(
                row=0,
                col=0,
                value="項目",
                bbox={"left": 1, "top": 2, "right": 20, "bottom": 10},
                confidence=0.99,
            ),
            SimpleNamespace(
                row=0,
                col=1,
                value="金額",
                bbox={"left": 21, "top": 2, "right": 40, "bottom": 10},
                confidence=0.98,
            ),
            SimpleNamespace(
                row=1,
                col=0,
                value="交通費",
                bbox={"left": 1, "top": 11, "right": 20, "bottom": 20},
                row_span=1,
                col_span=1,
            ),
            SimpleNamespace(
                row=1,
                col=1,
                value="1000円",
                bbox={"left": 21, "top": 11, "right": 40, "bottom": 20},
            ),
        ]

    class FakePage:
        page_no = 3
        size = SimpleNamespace(width=612, height=792)
        rotation = 0

    class FakeDocument:
        pages = {"1": object(), "2": object(), "3": FakePage()}
        tables = [FakeGridBlock()]

    class FakeDocumentConverter:
        def convert(self, path: str) -> object:
            assert path.endswith(".pdf")
            return SimpleNamespace(document=FakeDocument())

    converter_module.__dict__["DocumentConverter"] = FakeDocumentConverter
    monkeypatch.setitem(sys.modules, "docling", docling_module)
    monkeypatch.setitem(sys.modules, "docling.document_converter", converter_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="cell-only.pdf",
        sanitized_file_name="cell-only.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="9" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="docling",
        docling_enabled=True,
    )

    assert result.parser_backend == "docling"
    assert result.extraction is not None
    assert result.extraction.parser_artifacts["adapter_export"] == "structured_elements"
    assert result.extraction.parser_artifacts["adapter_table_count"] == 1
    assert result.extraction.raw_text == "| 項目 | 金額 |\n| 交通費 | 1000円 |"
    element = result.extraction.elements[0]
    assert element.kind == "table"
    assert element.content_kind == "table"
    assert element.text == "| 項目 | 金額 |\n| 交通費 | 1000円 |"
    assert element.page_number == 3
    assert element.bbox == [1.0, 2.0, 90.0, 40.0]
    assert element.metadata["table_source"] == "adapter_cells"
    assert element.metadata["page_width"] == 612
    assert element.metadata["page_height"] == 792
    table = result.extraction.tables[0]
    assert table.table_id == "cell-grid-1"
    assert table.element_id == "cell-grid-1"
    assert table.page_number == 3
    assert table.metadata["table_source"] == "adapter_cells"
    assert table.metadata["page_width"] == 612
    assert table.metadata["page_height"] == 792
    assert [(cell.row, cell.col, cell.text) for cell in table.cells] == [
        (0, 0, "項目"),
        (0, 1, "金額"),
        (1, 0, "交通費"),
        (1, 1, "1000円"),
    ]
    assert table.cells[0].bbox == [1.0, 2.0, 20.0, 10.0]
    assert table.cells[0].confidence == 0.99
    assert table.cells[0].metadata["bbox_coordinate_mode"] == "xyxy"
    assert table.cells[0].metadata["bbox_unit"] == "percent"
    assert table.cells[0].metadata["page_width"] == 612
    assert table.cells[0].metadata["page_height"] == 792
    chunk = chunk_extraction(result.extraction, chunk_size=80, overlap=0)[0]
    assert chunk.metadata["bbox"] == "[1.0,2.0,90.0,40.0]"
    assert chunk.metadata["bbox_unit"] == "percent"
    assert chunk.metadata["page_width"] == 612
    assert chunk.metadata["page_height"] == 792


def test_parser_registry_uses_unstructured_adapter_elements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unstructured elements は page/bbox/content_kind を保持して remap する。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")

    class FakeMetadata:
        def __init__(self) -> None:
            self.page_number = 3
            self.coordinates = {"points": [(0.1, 0.2), (0.5, 0.6)]}
            self.detection_class_prob = 0.87

    class FakeNarrativeText:
        id = "narrative-1"
        category = "NarrativeText"
        text = "本文ブロック"
        metadata = FakeMetadata()

    class FakeTable:
        id = "table-1"
        category = "Table"
        text = "| A | B |\n| --- | --- |\n| alpha | 1 |"
        metadata = SimpleNamespace(page_number=4, coordinates=[0, 0, 10, 20])

    def partition(*, filename: str, content_type: str) -> list[object]:
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        return [FakeNarrativeText(), FakeTable()]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="sample.pdf",
        sanitized_file_name="sample.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="2" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.parser_backend == "unstructured"
    assert result.parser_version == "7.8.9"
    assert result.extraction is not None
    first, second = result.extraction.elements
    assert first.page_number == 3
    assert first.bbox == [0.1, 0.2, 0.5, 0.6]
    assert first.confidence == 0.87
    assert first.content_kind == "text"
    assert first.metadata["bbox_coordinate_mode"] == "xyxy"
    assert first.metadata["bbox_unit"] == "ratio"
    assert second.page_number == 4
    assert second.bbox == [0.0, 0.0, 10.0, 20.0]
    assert second.content_kind == "table"
    assert second.metadata["bbox_coordinate_mode"] == "xyxy"
    assert second.metadata["bbox_unit"] == "percent"
    assert second.metadata["table_id"] == "table-1"
    assert second.metadata["row_count"] == 2
    assert second.metadata["column_count"] == 2
    assert result.extraction.tables
    table = result.extraction.tables[0]
    assert table.table_id == "table-1"
    assert table.element_id == "table-1"
    assert table.page_number == 4
    assert table.metadata["row_count"] == 2
    assert table.metadata["column_count"] == 2
    assert [(cell.row, cell.col, cell.text) for cell in table.cells] == [
        (0, 0, "A"),
        (0, 1, "B"),
        (1, 0, "alpha"),
        (1, 1, "1"),
    ]
    table_chunk = next(
        chunk
        for chunk in chunk_extraction(result.extraction, chunk_size=80, overlap=0)
        if chunk.metadata["content_kind"] == "table"
    )
    assert table_chunk.metadata["table_id"] == "table-1"
    assert table_chunk.metadata["table_row_count"] == 2
    assert table_chunk.metadata["table_column_count"] == 2
    assert table_chunk.metadata["source_parser"] == "unstructured_adapter"


def test_parser_registry_preserves_adapter_link_metadata_in_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """adapter element の link/reference metadata は共通 link lineage へ remap する。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")

    class FakeLinkedText:
        id = "linked-text-1"
        category = "NarrativeText"
        text = "仕様を確認します。"
        metadata = SimpleNamespace(
            page_number=2,
            links=[
                {"url": "https://example.com/adapter-spec", "text": "仕様"},
                {"href": "javascript:alert(1)", "text": "危険"},
            ],
        )

    def partition(*, filename: str, content_type: str) -> list[object]:
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        return [FakeLinkedText()]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="linked.pdf",
        sanitized_file_name="linked.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="8" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.extraction is not None
    element = result.extraction.elements[0]
    assert element.metadata["link_count"] == 1
    assert element.metadata["link_urls"] == "https://example.com/adapter-spec"
    assert element.metadata["link_texts"] == "仕様"
    chunk = chunk_extraction(result.extraction, chunk_size=120, overlap=0)[0]
    assert chunk.metadata["link_count"] == 1
    assert chunk.metadata["link_urls"] == "https://example.com/adapter-spec"
    assert chunk.metadata["link_texts"] == "仕様"


def test_parser_registry_promotes_adapter_figures_to_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部 adapter の image/figure block は elements だけでなく assets[] にも残す。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")

    class FakeImage:
        id = "fig-asset-1"
        category = "Image"
        metadata = SimpleNamespace(
            page_number=5,
            coordinates={"left": 10, "top": 20, "right": 80, "bottom": 60},
            alt_text="検索フロー図",
            detection_class_prob=0.92,
        )

    def partition(*, filename: str, content_type: str) -> list[object]:
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        return [FakeImage()]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="figures.pdf",
        sanitized_file_name="figures.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="c" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.extraction is not None
    assert result.extraction.parser_artifacts["adapter_asset_count"] == 1
    element = result.extraction.elements[0]
    assert element.kind == "figure"
    assert element.content_kind == "figure"
    assert element.text == "検索フロー図"
    assert element.metadata["asset_id"] == "fig-asset-1"
    assert element.metadata["asset_kind"] == "image"
    asset = result.extraction.assets[0]
    assert asset.asset_id == "fig-asset-1"
    assert asset.kind == "image"
    assert asset.page_number == 5
    assert asset.bbox == [10.0, 20.0, 80.0, 60.0]
    assert asset.alt_text == "検索フロー図"
    assert asset.metadata["element_id"] == "fig-asset-1"
    assert asset.metadata["confidence"] == 0.92
    figure_chunk = chunk_extraction(result.extraction, chunk_size=80, overlap=0)[0]
    assert figure_chunk.metadata["content_kind"] == "figure"
    assert figure_chunk.metadata["element_ids"] == "fig-asset-1"
    assert figure_chunk.metadata["asset_id"] == "fig-asset-1"
    assert figure_chunk.metadata["asset_kind"] == "image"


def test_parser_registry_promotes_adapter_charts_to_figure_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Chart/diagram 系 block は other/text へ落とさず figure asset として保持する。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")

    class FakeChart:
        id = "chart-1"
        category = "Chart"
        text = "四半期売上: Q1 1200万円, Q2 1500万円"
        metadata = SimpleNamespace(
            page_number=6,
            coordinates={"x": 10, "y": 20, "width": 100, "height": 80},
            detection_class_prob=0.89,
        )

    def partition(*, filename: str, content_type: str) -> list[object]:
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        return [FakeChart()]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="chart.pdf",
        sanitized_file_name="chart.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="2" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.extraction is not None
    assert result.extraction.parser_artifacts["adapter_asset_count"] == 1
    element = result.extraction.elements[0]
    assert element.kind == "figure"
    assert element.content_kind == "figure"
    assert element.metadata["asset_id"] == "chart-1"
    assert element.metadata["asset_kind"] == "chart"
    assert element.metadata["adapter_raw_element_type"] == "Chart"
    asset = result.extraction.assets[0]
    assert asset.asset_id == "chart-1"
    assert asset.kind == "chart"
    assert asset.page_number == 6
    assert asset.bbox == [10.0, 20.0, 110.0, 100.0]
    assert asset.metadata["adapter_raw_element_type"] == "Chart"
    chunk = chunk_extraction(result.extraction, chunk_size=120, overlap=0)[0]
    assert chunk.metadata["content_kind"] == "figure"
    assert chunk.metadata["asset_id"] == "chart-1"
    assert chunk.metadata["asset_kind"] == "chart"
    assert chunk.metadata["bbox_coordinate_mode"] == "xyxy"


def test_parser_registry_infers_adapter_figure_caption_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """隣接 FigureCaption は parent_id がなくても直前 figure へ結びつける。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")

    class FakeImage:
        id = "fig-1"
        category = "Image"
        metadata = SimpleNamespace(page_number=4, alt_text="RAG フロー図")

    class FakeCaption:
        id = "fig-1-caption"
        category = "FigureCaption"
        text = "図1: RAG フロー"
        metadata = SimpleNamespace(page_number=4)

    def partition(*, filename: str, content_type: str) -> list[object]:
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        return [FakeImage(), FakeCaption()]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="caption.pdf",
        sanitized_file_name="caption.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="f" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.extraction is not None
    figure, caption = result.extraction.elements
    assert figure.kind == "figure"
    assert caption.kind == "figure_caption"
    assert caption.parent_id == figure.element_id
    figure_chunk = chunk_extraction(result.extraction, chunk_size=120, overlap=0)[0]
    assert figure_chunk.metadata["content_kind"] == "figure"
    assert figure_chunk.metadata["parent_element_ids"] == "fig-1"
    assert figure_chunk.metadata["dependency_edges"] == (
        '[{"child_id":"fig-1-caption","parent_id":"fig-1"}]'
    )


def test_parser_registry_infers_adapter_table_caption_kind_and_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TableCaption は table ではなく caption として直前 table に結びつける。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")

    class FakeTable:
        id = "tbl-1"
        category = "Table"
        rows = [["項目", "値"], ["状態", "INDEXED"]]
        metadata = SimpleNamespace(page_number=2)

    class FakeTableCaption:
        id = "tbl-1-caption"
        category = "TableCaption"
        text = "表1: 検索状態"
        metadata = SimpleNamespace(page_number=2)

    def partition(*, filename: str, content_type: str) -> list[object]:
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        return [FakeTable(), FakeTableCaption()]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="table-caption.pdf",
        sanitized_file_name="table-caption.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="0" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.extraction is not None
    table, caption = result.extraction.elements
    assert table.kind == "table"
    assert table.text.startswith("表1: 検索状態\n| 項目 | 値 |")
    assert table.metadata["table_caption"] == "表1: 検索状態"
    assert table.metadata["caption_element_id"] == "tbl-1-caption"
    assert caption.kind == "table_caption"
    assert caption.content_kind == "table"
    assert caption.parent_id == table.element_id
    assert caption.metadata["table_id"] == "tbl-1"
    extraction_table = result.extraction.tables[0]
    assert extraction_table.caption == "表1: 検索状態"
    assert extraction_table.metadata["table_caption"] == "表1: 検索状態"
    table_chunk = chunk_extraction(result.extraction, chunk_size=120, overlap=0)[0]
    assert table_chunk.metadata["content_kind"] == "table"
    assert table_chunk.metadata["table_id"] == "tbl-1"
    assert table_chunk.metadata["table_caption"] == "表1: 検索状態"
    assert table_chunk.text.startswith("表1: 検索状態\n| 項目 | 値 |")


def test_parser_registry_preserves_adapter_formula_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """text 欠落の Formula block でも latex metadata から equation を復元する。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")

    class FakeFormula:
        id = "eq-1"
        category = "Formula"
        metadata = SimpleNamespace(page_number=3, latex="E = mc^2")

    def partition(*, filename: str, content_type: str) -> list[object]:
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        return [FakeFormula()]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="formula.pdf",
        sanitized_file_name="formula.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="e" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.extraction is not None
    element = result.extraction.elements[0]
    assert element.kind == "equation"
    assert element.content_kind == "equation"
    assert element.text == "E = mc^2"
    assert element.metadata["equation_format"] == "latex"
    assert element.metadata["equation_source_field"] == "latex"
    equation_chunk = chunk_extraction(result.extraction, chunk_size=80, overlap=0)[0]
    assert equation_chunk.metadata["content_kind"] == "equation"
    assert equation_chunk.metadata["element_ids"] == "eq-1"
    assert equation_chunk.metadata["equation_format"] == "latex"


def test_parser_registry_preserves_adapter_code_language_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部 adapter の Code block language は chunk metadata の code_language に寄せる。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")

    class FakeCode:
        id = "code-1"
        category = "CodeSnippet"
        text = "print('ready')"
        metadata = SimpleNamespace(page_number=2, language="Python")

    def partition(*, filename: str, content_type: str) -> list[object]:
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        return [FakeCode()]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="code.pdf",
        sanitized_file_name="code.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="1" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.extraction is not None
    element = result.extraction.elements[0]
    assert element.kind == "code"
    assert element.content_kind == "code"
    assert element.metadata["language"] == "Python"
    assert element.metadata["code_language"] == "python"
    chunk = chunk_extraction(result.extraction, chunk_size=80, overlap=0)[0]
    assert chunk.metadata["content_kind"] == "code"
    assert chunk.metadata["code_language"] == "python"


def test_unstructured_adapter_uses_page_breaks_for_missing_page_numbers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PageBreak は index せず、後続 block の page lineage 推定に使う。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")

    class FakeText:
        id = "p1-text"
        category = "NarrativeText"
        text = "1ページ目の本文"

    class FakePageBreak:
        category = "PageBreak"
        metadata = SimpleNamespace(page_number=1)

    class FakeTable:
        id = "p2-table"
        category = "Table"
        rows = [["項目", "値"], ["状態", "INDEXED"]]

    def partition(*, filename: str, content_type: str) -> list[object]:
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        return [FakeText(), FakePageBreak(), FakeTable()]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="page-break.pdf",
        sanitized_file_name="page-break.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="7" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.extraction is not None
    assert result.extraction.parser_artifacts["adapter_page_break_count"] == 1
    assert [element.element_id for element in result.extraction.elements] == [
        "p1-text",
        "p2-table",
    ]
    assert [element.page_number for element in result.extraction.elements] == [1, 2]
    assert [page.page_number for page in result.extraction.pages] == [1, 2]
    assert [page.element_ids for page in result.extraction.pages] == [
        ["p1-text"],
        ["p2-table"],
    ]
    chunks = chunk_extraction(result.extraction, chunk_size=80, overlap=0)
    table_chunk = next(chunk for chunk in chunks if chunk.metadata["content_kind"] == "table")
    assert table_chunk.metadata["page_start"] == 2
    assert table_chunk.metadata["page_end"] == 2


def test_unstructured_adapter_requests_page_breaks_and_table_inference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unstructured adapter は対応版なら日英 hi_res と表解析を要求する。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")
    seen_kwargs: dict[str, object] = {}

    class FakeTable:
        id = "table-hires-1"
        category = "Table"
        text = "| A | B |\n| --- | --- |\n| alpha | 1 |"
        metadata = SimpleNamespace(page_number=2)

    def partition(**kwargs: object) -> list[object]:
        seen_kwargs.update(kwargs)
        return [FakeTable()]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="table.pdf",
        sanitized_file_name="table.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="a" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert isinstance(seen_kwargs["filename"], str)
    assert seen_kwargs["filename"].endswith(".pdf")
    assert seen_kwargs["content_type"] == "application/pdf"
    assert seen_kwargs["include_page_breaks"] is True
    assert seen_kwargs["strategy"] == "hi_res"
    assert seen_kwargs["languages"] == ["jpn", "eng"]
    assert seen_kwargs["infer_table_structure"] is True
    assert result.extraction is not None
    assert result.extraction.parser_artifacts["partition_include_page_breaks"] is True
    assert result.extraction.parser_artifacts["partition_strategy"] == "hi_res"
    assert result.extraction.parser_artifacts["partition_infer_table_structure"] is True


def test_unstructured_adapter_filters_unsupported_partition_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """古い adapter API には対応していない kwargs を渡さず fallback を防ぐ。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")

    class FakeText:
        id = "text-1"
        category = "NarrativeText"
        text = "本文"
        metadata = SimpleNamespace(page_number=1)

    def partition(*, filename: str, content_type: str) -> list[object]:
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        return [FakeText()]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="legacy.pdf",
        sanitized_file_name="legacy.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="b" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.parser_backend == "unstructured"
    assert result.extraction is not None
    assert result.extraction.elements[0].text == "本文"


def test_parser_registry_prefers_adapter_table_rows_over_caption_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外部 adapter の table rows/data は caption text より優先して chunk/citation に使う。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")

    class FakeRowsTable:
        id = "table-rows-1"
        category = "Table"
        text = "予算表"
        rows = [["項目", "金額"], ["交通費", "1000円"]]
        metadata = SimpleNamespace(page_number=5)

    def partition(*, filename: str, content_type: str) -> list[object]:
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        return [FakeRowsTable()]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="budget.pdf",
        sanitized_file_name="budget.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="8" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.extraction is not None
    element = result.extraction.elements[0]
    assert element.text == "| 項目 | 金額 |\n| 交通費 | 1000円 |"
    assert "予算表" not in result.extraction.raw_text
    assert element.metadata["table_id"] == "table-rows-1"
    assert element.metadata["row_count"] == 2
    assert element.metadata["column_count"] == 2
    assert element.metadata["table_source"] == "adapter_rows"
    table = result.extraction.tables[0]
    assert table.table_id == "table-rows-1"
    assert table.page_number == 5
    assert table.metadata["table_source"] == "adapter_rows"
    assert [(cell.row, cell.col, cell.text) for cell in table.cells] == [
        (0, 0, "項目"),
        (0, 1, "金額"),
        (1, 0, "交通費"),
        (1, 1, "1000円"),
    ]
    assert [cell.metadata["cell_ref"] for cell in table.cells] == [
        "A1",
        "B1",
        "A2",
        "B2",
    ]
    table_chunk = chunk_extraction(result.extraction, chunk_size=80, overlap=0)[0]
    assert "交通費" in table_chunk.text
    assert table_chunk.metadata["table_id"] == "table-rows-1"
    assert table_chunk.metadata["table_row_count"] == 2
    assert table_chunk.metadata["table_column_count"] == 2


def test_parser_registry_preserves_adapter_structured_table_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """adapter の cell-level bbox/span/confidence を tables[].cells へ保持する。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")

    class FakeStructuredCellTable:
        id = "table-cells-1"
        category = "Table"
        text = "明細表"
        cells = [
            {
                "row": 0,
                "col": 0,
                "text": "項目",
                "bbox": {"x": 0, "y": 0, "w": 20, "h": 10},
            },
            {
                "row": 0,
                "col": 1,
                "text": "金額",
                "bbox": {"left": 20, "top": 0, "right": 40, "bottom": 10},
                "col_span": 2,
                "confidence": 0.95,
                "metadata": {"header": True, "scope": "col"},
            },
            {
                "row": 1,
                "col": 0,
                "text": "交通費",
                "bbox": {"xmin": 0, "ymin": 10, "xmax": 20, "ymax": 20},
            },
            {
                "row": 1,
                "col": 1,
                "text": "1000円",
                "cell_id": "cell-b2",
                "metadata": {
                    "cell_ref": "B2",
                    "formula": "SUM(B1:B1)",
                    "formula_value": "1000円",
                    "equation_format": "excel_formula",
                },
            },
        ]
        metadata = SimpleNamespace(page_number=6)

    def partition(*, filename: str, content_type: str) -> list[object]:
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        return [FakeStructuredCellTable()]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="cells.pdf",
        sanitized_file_name="cells.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="d" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.extraction is not None
    element = result.extraction.elements[0]
    assert element.text == "| 項目 | 金額 |  |\n| 交通費 | 1000円 |  |"
    assert "明細表" not in result.extraction.raw_text
    assert element.metadata["table_source"] == "adapter_cells"
    table = result.extraction.tables[0]
    assert table.metadata["table_source"] == "adapter_cells"
    assert table.metadata["row_count"] == 2
    assert table.metadata["column_count"] == 3
    first, second, third, fourth = table.cells
    assert (first.row, first.col, first.text, first.bbox) == (
        0,
        0,
        "項目",
        [0.0, 0.0, 20.0, 10.0],
    )
    assert second.col_span == 2
    assert second.bbox == [20.0, 0.0, 40.0, 10.0]
    assert second.confidence == 0.95
    assert second.metadata["is_header"] is True
    assert second.metadata["header_scope"] == "col"
    assert third.bbox == [0.0, 10.0, 20.0, 20.0]
    assert third.metadata["cell_ref"] == "A2"
    assert "formula_cell_ref" not in third.metadata
    assert (fourth.row, fourth.col, fourth.text) == (1, 1, "1000円")
    assert fourth.metadata["cell_id"] == "cell-b2"
    assert [cell.metadata["cell_ref"] for cell in table.cells] == [
        "A1",
        "B1",
        "A2",
        "B2",
    ]
    assert fourth.metadata["cell_ref"] == "B2"
    assert fourth.metadata["formula_cell_ref"] == "B2"
    assert fourth.metadata["formula"] == "SUM(B1:B1)"
    assert fourth.metadata["formula_value"] == "1000円"
    assert fourth.metadata["equation_format"] == "excel_formula"
    table_chunk = chunk_extraction(result.extraction, chunk_size=80, overlap=0)[0]
    assert table_chunk.metadata["table_id"] == "table-cells-1"
    assert table_chunk.metadata["table_row_count"] == 2
    assert table_chunk.metadata["table_column_count"] == 3


def test_parser_registry_uses_adapter_metadata_text_as_html_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unstructured などの metadata.text_as_html も table cells として remap する。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")

    class FakeHtmlTable:
        id = "table-html-1"
        category = "Table"
        text = "売上一覧"
        metadata = SimpleNamespace(
            page_number=6,
            text_as_html=(
                "<table><tr><th>地域</th><th colspan='2'>売上</th></tr>"
                "<tr><td>関西</td><td>1200</td><td>1500</td></tr></table>"
            ),
        )

    def partition(*, filename: str, content_type: str) -> list[object]:
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        return [FakeHtmlTable()]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="sales.pdf",
        sanitized_file_name="sales.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="9" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.extraction is not None
    element = result.extraction.elements[0]
    assert element.text == "| 地域 | 売上 |  |\n| 関西 | 1200 | 1500 |"
    assert "売上一覧" not in result.extraction.raw_text
    assert element.metadata["table_id"] == "table-html-1"
    assert element.metadata["row_count"] == 2
    assert element.metadata["column_count"] == 3
    assert element.metadata["table_source"] == "adapter_cells"
    table = result.extraction.tables[0]
    cell_shapes = [
        (cell.row, cell.col, cell.text, cell.row_span, cell.col_span) for cell in table.cells
    ]
    assert cell_shapes == [
        (0, 0, "地域", 1, 1),
        (0, 1, "売上", 1, 2),
        (1, 0, "関西", 1, 1),
        (1, 1, "1200", 1, 1),
        (1, 2, "1500", 1, 1),
    ]
    table_chunk = chunk_extraction(result.extraction, chunk_size=80, overlap=0)[0]
    assert "関西" in table_chunk.text
    assert table_chunk.metadata["table_id"] == "table-html-1"
    assert table_chunk.metadata["table_row_count"] == 2
    assert table_chunk.metadata["table_column_count"] == 3


def test_parser_registry_remaps_mapping_adapter_table_elements(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON 形式の adapter element でも kind/id/page/bbox/table rows を保持する。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")

    def partition(*, filename: str, content_type: str) -> list[object]:
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        return [
            {
                "id": "table-dict-1",
                "type": "Table",
                "text": "在庫表",
                "data": [["SKU", "数量"], ["A-001", "42"]],
                "metadata": {
                    "page_number": 7,
                    "coordinates": {"x1": 10, "y1": 20, "x2": 30, "y2": 40},
                    "confidence": 0.91,
                },
            }
        ]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="inventory.pdf",
        sanitized_file_name="inventory.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="a" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.extraction is not None
    element = result.extraction.elements[0]
    assert element.element_id == "table-dict-1"
    assert element.content_kind == "table"
    assert element.page_number == 7
    assert element.bbox == [10.0, 20.0, 30.0, 40.0]
    assert element.confidence == 0.91
    assert element.text == "| SKU | 数量 |\n| A-001 | 42 |"
    assert element.metadata["table_id"] == "table-dict-1"
    assert element.metadata["row_count"] == 2
    assert element.metadata["column_count"] == 2
    assert element.metadata["bbox_coordinate_mode"] == "xyxy"
    assert element.metadata["bbox_unit"] == "percent"
    table_chunk = chunk_extraction(result.extraction, chunk_size=80, overlap=0)[0]
    assert table_chunk.metadata["element_ids"] == "table-dict-1"
    assert table_chunk.metadata["page_start"] == 7
    assert table_chunk.metadata["table_id"] == "table-dict-1"


def test_parser_registry_remaps_adapter_bbox_key_variants_for_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """left/top/right/bottom や x/y/w/h 形式も preview 用 bbox lineage に寄せる。"""
    unstructured_module = ModuleType("unstructured")
    unstructured_module.__dict__["__version__"] = "7.8.9"
    partition_package = ModuleType("unstructured.partition")
    auto_module = ModuleType("unstructured.partition.auto")

    def partition(*, filename: str, content_type: str) -> list[object]:
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        return [
            {
                "id": "ltrb-1",
                "type": "NarrativeText",
                "text": "left top right bottom bbox",
                "metadata": {
                    "page_number": 8,
                    "coordinates": {"left": 10, "top": 20, "right": 90, "bottom": 80},
                },
            },
            {
                "id": "xywh-1",
                "type": "NarrativeText",
                "text": "x y width height bbox",
                "metadata": {
                    "page_number": 9,
                    "coordinates": {"x": 5, "y": 7, "w": 20, "h": 30},
                },
            },
        ]

    auto_module.__dict__["partition"] = partition
    monkeypatch.setitem(sys.modules, "unstructured", unstructured_module)
    monkeypatch.setitem(sys.modules, "unstructured.partition", partition_package)
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", auto_module)

    data = b"%PDF"
    profile = build_source_profile(
        original_file_name="bbox.pdf",
        sanitized_file_name="bbox.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="b" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="unstructured",
        unstructured_enabled=True,
    )

    assert result.extraction is not None
    first, second = result.extraction.elements
    assert first.bbox == [10.0, 20.0, 90.0, 80.0]
    assert first.metadata["bbox_coordinate_mode"] == "xyxy"
    assert first.metadata["bbox_unit"] == "percent"
    assert second.bbox == [5.0, 7.0, 25.0, 37.0]
    assert second.metadata["bbox_coordinate_mode"] == "xyxy"
    assert second.metadata["bbox_unit"] == "percent"
    chunk = chunk_extraction(
        result.extraction.model_copy(update={"elements": [first]}),
        chunk_size=80,
        overlap=0,
    )[0]
    assert chunk.metadata["bbox"] == "[10.0,20.0,90.0,80.0]"
    assert chunk.metadata["bbox_coordinate_mode"] == "xyxy"
    assert chunk.metadata["bbox_unit"] == "percent"


def test_extraction_table_cell_and_asset_bbox_accept_adapter_key_variants() -> None:
    """table cell / asset bbox も element と同じ正規化規則で保存する。"""
    cell = ExtractionTableCell(
        row=1,
        col=2,
        text="状態",
        bbox={"left": 10, "top": 20, "right": 30, "bottom": 40},
    )
    asset = ExtractionAsset(
        asset_id="figure-1",
        kind="figure",
        bbox={"x": 5, "y": 7, "w": 20, "h": 30},
    )

    assert cell.bbox == [10.0, 20.0, 30.0, 40.0]
    assert asset.bbox == [5.0, 7.0, 25.0, 37.0]


def _docx_bytes(text: str) -> bytes:
    return _zip_bytes({"word/document.xml": f"<document><t>{text}</t></document>"})


def _docx_table_bytes(*, before: str, rows: list[list[str]], after: str) -> bytes:
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    table_rows = []
    for row in rows:
        cells = "".join(f"<w:tc><w:p><w:r><w:t>{value}</w:t></w:r></w:p></w:tc>" for value in row)
        table_rows.append(f"<w:tr>{cells}</w:tr>")
    document = (
        f'<w:document xmlns:w="{namespace}"><w:body>'
        f"<w:p><w:r><w:t>{before}</w:t></w:r></w:p>"
        f"<w:tbl>{''.join(table_rows)}</w:tbl>"
        f"<w:p><w:r><w:t>{after}</w:t></w:r></w:p>"
        "</w:body></w:document>"
    )
    return _zip_bytes({"word/document.xml": document})


def _pptx_bytes(text: str) -> bytes:
    return _zip_bytes({"ppt/slides/slide1.xml": f"<sld><t>{text}</t></sld>"})


def _pptx_table_bytes(rows: list[list[str]]) -> bytes:
    namespace = "http://schemas.openxmlformats.org/drawingml/2006/main"
    table_rows = []
    for row in rows:
        cells = "".join(
            (f"<a:tc><a:txBody><a:p><a:r><a:t>{value}</a:t></a:r></a:p></a:txBody></a:tc>")
            for value in row
        )
        table_rows.append(f"<a:tr>{cells}</a:tr>")
    slide = (
        f'<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        f'xmlns:a="{namespace}"><p:cSld><p:spTree><p:graphicFrame>'
        f"<a:tbl>{''.join(table_rows)}</a:tbl>"
        "</p:graphicFrame></p:spTree></p:cSld></p:sld>"
    )
    return _zip_bytes({"ppt/slides/slide1.xml": slide})


def _xlsx_bytes(text: str) -> bytes:
    return _xlsx_table_bytes([[text]])


def _xlsx_table_bytes(rows: list[list[str]]) -> bytes:
    values = [value for row in rows for value in row]
    shared = "<sst>" + "".join(f"<si><t>{value}</t></si>" for value in values) + "</sst>"
    worksheet_rows: list[str] = []
    cursor = 0
    for row in rows:
        cells: list[str] = []
        for _value in row:
            cells.append(f'<c t="s"><v>{cursor}</v></c>')
            cursor += 1
        worksheet_rows.append("<row>" + "".join(cells) + "</row>")
    return _zip_bytes(
        {
            "xl/sharedStrings.xml": shared,
            "xl/worksheets/sheet1.xml": (
                "<worksheet><sheetData>" + "".join(worksheet_rows) + "</sheetData></worksheet>"
            ),
        }
    )


def _xlsx_formula_table_bytes() -> bytes:
    shared = "<sst><si><t>項目</t></si><si><t>合計</t></si></sst>"
    worksheet = (
        "<worksheet><sheetData>"
        '<row r="1">'
        '<c r="A1" t="s"><v>0</v></c>'
        '<c r="B1" t="s"><v>1</v></c>'
        "</row>"
        '<row r="2">'
        '<c r="A2"><v>1200</v></c>'
        '<c r="B2"><f>SUM(A2:A2)</f><v>1200</v></c>'
        "</row>"
        "</sheetData></worksheet>"
    )
    return _zip_bytes(
        {
            "xl/sharedStrings.xml": shared,
            "xl/worksheets/sheet1.xml": worksheet,
        }
    )


def _zip_bytes(files: dict[str, str]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return output.getvalue()


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.mark.parametrize(
    ("backend", "file_name", "content_type", "expected"),
    [
        # docling: PDF・画像だけ(parser サービスは Docling のレイアウト解析。#286)
        ("docling", "doc.pdf", "application/pdf", True),
        ("docling", "scan.png", "image/png", True),
        ("docling", "photo.jpg", "image/jpeg", True),
        ("docling", "memo.md", "text/markdown", False),
        ("docling", "memo.txt", "text/plain", False),
        ("docling", "data.json", "application/json", False),
        ("docling", "data.xml", "application/xml", False),
        ("docling", "page.html", "text/html", False),
        ("docling", "sheet.xlsx", _XLSX_MIME, False),
        ("docling", "mail.eml", "message/rfc822", False),
        # unstructured: 汎用(メール含む)
        ("unstructured", "mail.eml", "message/rfc822", True),
        ("unstructured", "doc.pdf", "application/pdf", True),
        # mineru: PDF・画像・Office(HTML 不可)
        ("mineru", "sheet.xlsx", _XLSX_MIME, True),
        ("mineru", "page.html", "text/html", False),
        # 外部 GPU OCR は backend 側で PDF をページ画像化する。
        ("dots_ocr", "scan.png", "image/png", True),
        ("dots_ocr", "doc.pdf", "application/pdf", True),
        # XML ツリー JSON: JSON だけ(#2042。対応する形かは解析で判定する)
        ("xml_tree_json", "tree.json", "application/json", True),
        ("xml_tree_json", "doc.pdf", "application/pdf", False),
        # OCI service backend: PDF・画像
        ("oci_document_understanding", "scan.png", "image/png", True),
        ("oci_document_understanding", "mail.eml", "message/rfc822", False),
        # 未宣言 backend と削除したエンジン(#270)は非対応
        ("no_such_backend", "doc.pdf", "application/pdf", False),
        ("marker", "doc.pdf", "application/pdf", False),
        ("unlimited_ocr", "doc.pdf", "application/pdf", False),
        ("glm_ocr", "scan.png", "image/png", False),
        # 削除した OCI Generative AI (Vision)(#2042)
        ("oci_genai_vision", "doc.pdf", "application/pdf", False),
    ],
)
def test_adapter_capabilities_matrix(
    backend: str, file_name: str, content_type: str, expected: bool
) -> None:
    """capabilities.py の宣言 matrix が従来 if-chain と同じ判定を返す。"""
    from rag_parser_core.capabilities import adapter_supports_source

    profile = build_source_profile(
        original_file_name=file_name,
        sanitized_file_name=file_name,
        content_type=content_type,
        file_size_bytes=16,
        content_sha256="c" * 64,
        data=b"payload-16-bytes",
    )

    assert (
        adapter_supports_source(backend, source_profile=profile, content_type=content_type)
        is expected
    )


def test_adapter_capabilities_unknown_modality_only_unstructured() -> None:
    """UNKNOWN modality は unstructured だけが受ける(従来挙動)。"""
    from rag_parser_core.capabilities import adapter_supports_source

    assert adapter_supports_source("unstructured", source_profile=None, content_type="")
    assert not adapter_supports_source("docling", source_profile=None, content_type="")


def test_supported_modalities_for_display() -> None:
    """表示用一覧は定義順で UNKNOWN を含まない。"""
    from rag_parser_core.capabilities import supported_modalities

    assert [m.value for m in supported_modalities("dots_ocr")] == ["pdf", "image"]
    assert [m.value for m in supported_modalities("unstructured")] == [
        "pdf",
        "image",
        "text",
        "html",
        "email",
        "office",
    ]
    assert supported_modalities("no_such_backend") == ()


def test_parser_registry_classifies_broken_pdf_as_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PdfiumError 系の失敗は adapter_failed でなく adapter_invalid_input に分類する。"""
    docling_module = ModuleType("docling")
    docling_module.__dict__["__version__"] = "3.0.0"
    converter_module = ModuleType("docling.document_converter")

    class PdfiumError(RuntimeError):
        pass

    class FakeDocumentConverter:
        def convert(self, path: str) -> object:
            raise PdfiumError("Failed to load document (PDFium: Data format error).")

    converter_module.__dict__["DocumentConverter"] = FakeDocumentConverter
    monkeypatch.setitem(sys.modules, "docling", docling_module)
    monkeypatch.setitem(sys.modules, "docling.document_converter", converter_module)
    monkeypatch.setattr(
        "rag_parser_core.registry._module_available", lambda name: name == "docling"
    )

    data = b"broken-not-a-real-pdf"
    profile = build_source_profile(
        original_file_name="broken.pdf",
        sanitized_file_name="broken.pdf",
        content_type="application/pdf",
        file_size_bytes=len(data),
        content_sha256="b" * 64,
        data=data,
    )

    result = parse_with_registry(
        data,
        source_profile=profile,
        content_type=profile.content_type,
        adapter_backend="docling",
        docling_enabled=True,
    )

    assert result.extraction is None
    assert result.fallback_used is True
    assert "docling_adapter_invalid_input" in result.warnings
    assert "docling_adapter_failed" not in result.warnings
