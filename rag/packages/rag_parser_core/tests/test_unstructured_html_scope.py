"""main の外の文書情報も、同じ Unstructured の解析に順序を保って渡す。"""

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from rag_parser_core.registry import _html_full_document_input, _unstructured_adapter_result
from rag_parser_core.result import UNSTRUCTURED_TEXT_CONTRACT


@pytest.mark.parametrize("tag", ["main", "MAIN", "main\nclass='x'"])
def test_only_real_main_tag_names_are_replaced(tag):
    html = (
        "<!doctype html><header>題名</header><!-- <main>comment</main> -->"
        f'<script>"<main>x</main>"</script><{tag}>本文 &amp; 字</main><footer>版</footer>'
    )
    result, changed = _html_full_document_input(html.encode())
    assert changed
    expected = html.replace(f"<{tag}>本文", f"<div{tag[4:]}>本文").replace("字</main>", "字</div>")
    assert result.decode() == expected


def test_no_main_keeps_original_bytes():
    source = "<body><header>題名</header><p>本文</p></body>".encode("cp932")
    assert _html_full_document_input(source) == (source, False)


def test_adapter_passes_complete_document_to_same_backend(monkeypatch):
    captured = {}

    def partition(**kwargs):
        captured.update(kwargs)
        text = Path(kwargs["filename"]).read_text()
        assert "<main" not in text
        assert text.index("題名") < text.index("本文") < text.index("末尾")
        return [
            SimpleNamespace(category="Title", text="題名", metadata=None),
            SimpleNamespace(category="NarrativeText", text="本文", metadata=None),
            SimpleNamespace(category="NarrativeText", text="末尾", metadata=None),
        ]

    module = ModuleType("unstructured.partition.auto")
    module.partition = partition
    monkeypatch.setitem(sys.modules, "unstructured.partition.auto", module)
    result = _unstructured_adapter_result(
        "<header>題名</header><main>本文</main><footer>末尾</footer>".encode(),
        source_profile=None,
        content_type="text/html",
    )
    assert result.parser_backend == "unstructured"
    assert captured["encoding"] == "utf-8"
    assert captured["skip_headers_and_footers"] is False
    assert result.extraction.raw_text == "題名\n\n本文\n\n末尾"
    assert (
        result.extraction.parser_artifacts["unstructured_text_contract"]
        == UNSTRUCTURED_TEXT_CONTRACT
    )
    assert result.extraction.parser_artifacts["html_main_scope"] == "full_document"


def test_region_roles_are_original_and_ambiguous_repeated_text_is_not_assigned():
    from rag_parser_core.extraction import DocumentElement, StructuredExtraction
    from rag_parser_core.registry import _restore_html_region_roles

    html = (
        "<header><h1>資料の題名</h1></header><main><p>共通の注意</p></main>"
        "<footer>共通の注意<br>末尾の版情報</footer>"
    )
    extraction = StructuredExtraction(
        raw_text="",
        elements=[
            DocumentElement(kind="title", text="資料の題名"),
            DocumentElement(kind="text", text="共通の注意"),
            DocumentElement(kind="text", text="末尾の版情報"),
        ],
    )
    result = _restore_html_region_roles(extraction, html.encode())
    assert [e.metadata.get("html_region_role") for e in result.elements] == [
        "header",
        None,
        "footer",
    ]
    assert [e.text for e in result.elements] == [e.text for e in extraction.elements]
