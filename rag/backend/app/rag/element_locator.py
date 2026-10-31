"""根拠の元の文書の要素を指す、分割に依存しない定位子（#1330）。

形は ``doc:{document_id}/ext:{extraction_recipe_id}/page:{頁}/el:{element_id}``
（頁は分かるときだけ）。

- ``extraction_recipe_id`` は解析の結果（原本・前処理・解析の設定と、レシピの版）の識別子。
  文書分割の設定だけを変えて chunk を作り直しても同じ解析の結果を使うので、定位子は変わらない。
  解析をやり直すと変わり、古い定位子は ``source_stale`` になる。
- ``element_id`` は解析の要素の ID（Docling の ``docling-p{頁}-{番号}``・MinerU の
  ``mineru-p{頁}-b{番号}`` など）。chunk の metadata の ``element_ids``（カンマ区切り）に入る。
- 頁は人が読むための情報で、読み直しは ``ext`` と ``el`` だけで決める。

``element_ids`` を持たない分割（文字数・区切り文字・Markdown の見出しでの分割）の chunk は
定位子を持たない。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

_LOCATOR_PATTERN = re.compile(
    r"^doc:(?P<document_id>[^/\s]+)/ext:(?P<extraction_recipe_id>[^/\s]+)"
    r"(?:/page:(?P<page>[1-9][0-9]{0,5}))?/el:(?P<element_id>[^\s]+)$"
)
LOCATOR_MAX_LENGTH = 1024


@dataclass(frozen=True, slots=True)
class ElementLocator:
    document_id: str
    extraction_recipe_id: str
    element_id: str
    page: int | None = None

    def __str__(self) -> str:
        page = f"/page:{self.page}" if self.page else ""
        return f"doc:{self.document_id}/ext:{self.extraction_recipe_id}{page}/el:{self.element_id}"


def parse_element_locator(value: str) -> ElementLocator | None:
    """定位子の文字列を読む。形が違えば None。"""
    match = _LOCATOR_PATTERN.fullmatch(value.strip()) if len(value) <= LOCATOR_MAX_LENGTH else None
    if match is None:
        return None
    page = match.group("page")
    return ElementLocator(
        document_id=match.group("document_id"),
        extraction_recipe_id=match.group("extraction_recipe_id"),
        element_id=match.group("element_id"),
        page=int(page) if page else None,
    )


def chunk_element_ids(metadata: Mapping[str, object]) -> list[str]:
    """chunk の metadata の ``element_ids``（カンマ区切りか list）を読み順の list にする。"""
    value = metadata.get("element_ids")
    if isinstance(value, str):
        items: list[object] = list(value.split(","))
    elif isinstance(value, list | tuple):
        items = list(value)
    else:
        return []
    return [text for item in items if (text := str(item).strip())]


def chunk_element_locator(metadata: Mapping[str, object]) -> ElementLocator | None:
    """chunk の先頭の要素の定位子。解析の結果の ID か要素の ID が無ければ None。

    ``extraction_recipe_id`` は保存した chunk の metadata には無く、chunk_set の列から読んで
    metadata に入れる（``OracleClient.chunk_set_extraction_recipe_ids``）。
    """
    document_id = _text(metadata.get("document_id"))
    extraction_recipe_id = _text(metadata.get("extraction_recipe_id"))
    element_ids = chunk_element_ids(metadata)
    if not document_id or not extraction_recipe_id or not element_ids:
        return None
    element_id = element_ids[0]
    return ElementLocator(
        document_id=document_id,
        extraction_recipe_id=extraction_recipe_id,
        element_id=element_id,
        page=_element_page(metadata, element_id),
    )


def _element_page(metadata: Mapping[str, object], element_id: str) -> int | None:
    """要素の頁（親子階層は要素ごとの source_record_refs、無ければ chunk の開始の頁）。"""
    refs = metadata.get("source_record_refs_json")
    if isinstance(refs, str):
        try:
            refs = json.loads(refs)
        except ValueError:
            refs = None
    if isinstance(refs, list):
        for ref in refs:
            if isinstance(ref, Mapping) and str(ref.get("record_id") or "") == element_id:
                page = _positive_int(ref.get("page"))
                if page is not None:
                    return page
    return _positive_int(metadata.get("page_start")) or _positive_int(metadata.get("page_number"))


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


def _text(value: object) -> str:
    return str(value).strip() if isinstance(value, str | int) else ""
