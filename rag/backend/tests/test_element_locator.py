"""根拠の要素の定位子（#1330）。"""

import json

import pytest

from app.rag.element_locator import (
    ElementLocator,
    chunk_element_ids,
    chunk_element_locator,
    parse_element_locator,
)


def test_locator_round_trips() -> None:
    locator = ElementLocator("doc-1", "er_abc", "docling-p3-7", page=3)

    assert str(locator) == "doc:doc-1/ext:er_abc/page:3/el:docling-p3-7"
    assert parse_element_locator(str(locator)) == locator
    # 頁は分かるときだけ。
    assert str(ElementLocator("doc-1", "er_abc", "el-0001")) == "doc:doc-1/ext:er_abc/el:el-0001"
    assert parse_element_locator("doc:doc-1/ext:er_abc/el:el-0001") == ElementLocator(
        "doc-1", "er_abc", "el-0001"
    )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "doc-1:c1",
        "doc:doc-1/el:p1",
        "doc:doc-1/ext:er/page:0/el:p1",
        "doc:doc-1/ext:er/page:x/el:p1",
        "doc:doc 1/ext:er/el:p1",
        "doc:d/ext:e/el:" + "x" * 1100,
    ],
    ids=["empty", "chunk-id", "no-ext", "page-zero", "page-text", "space", "too-long"],
)
def test_invalid_locator_is_rejected(value: str) -> None:
    assert parse_element_locator(value) is None


def test_chunk_element_ids_reads_comma_string_and_list() -> None:
    assert chunk_element_ids({"element_ids": "a, b,,c"}) == ["a", "b", "c"]
    assert chunk_element_ids({"element_ids": ["a", "b"]}) == ["a", "b"]
    assert chunk_element_ids({}) == []


def test_chunk_locator_uses_first_element_and_its_page() -> None:
    # 親子階層の chunk は要素ごとの頁（source_record_refs）を使う。
    metadata = {
        "document_id": "doc-1",
        "extraction_recipe_id": "er_1",
        "element_ids": "docling-p4-2,docling-p5-1",
        "page_start": 3,
        "source_record_refs_json": json.dumps(
            [{"record_id": "docling-p4-2", "page": 4}, {"record_id": "docling-p5-1", "page": 5}]
        ),
    }
    assert str(chunk_element_locator(metadata)) == "doc:doc-1/ext:er_1/page:4/el:docling-p4-2"
    # 要素ごとの頁が無ければ chunk の開始の頁。
    metadata.pop("source_record_refs_json")
    assert str(chunk_element_locator(metadata)) == "doc:doc-1/ext:er_1/page:3/el:docling-p4-2"


@pytest.mark.parametrize(
    "missing", ["document_id", "extraction_recipe_id", "element_ids"], ids=lambda key: key
)
def test_chunk_without_parse_reference_has_no_locator(missing: str) -> None:
    metadata = {"document_id": "doc-1", "extraction_recipe_id": "er_1", "element_ids": "el-1"}
    metadata.pop(missing)

    assert chunk_element_locator(metadata) is None
