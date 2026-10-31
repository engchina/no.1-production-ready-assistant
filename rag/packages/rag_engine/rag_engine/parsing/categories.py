"""解析エンジン固有カテゴリを比較ビューア用カテゴリへ正規化する。"""

from __future__ import annotations

from dataclasses import dataclass

CANONICAL_CATEGORIES = [
    "Caption",
    "Footnote",
    "Formula",
    "List-item",
    "Page-footer",
    "Page-header",
    "Picture",
    "Section-header",
    "Table",
    "Text",
    "Title",
]


_CATEGORY_ALIASES = {
    "caption": "Caption",
    "figure_caption": "Caption",
    "figcaption": "Caption",
    "footnote": "Footnote",
    "formula": "Formula",
    "equation": "Formula",
    "algorithm": "Formula",
    "list": "List-item",
    "list_item": "List-item",
    "list-item": "List-item",
    "footer": "Page-footer",
    "page_footer": "Page-footer",
    "page-footer": "Page-footer",
    "header": "Page-header",
    "page_header": "Page-header",
    "page-header": "Page-header",
    "image": "Picture",
    "picture": "Picture",
    "figure": "Picture",
    "chart": "Picture",
    "paragraph_title": "Section-header",
    "section_header": "Section-header",
    "section-header": "Section-header",
    "headline": "Section-header",
    "subheadline": "Section-header",
    "table": "Table",
    "text": "Text",
    "narrativetext": "Text",
    "uncategorizedtext": "Text",
    "paragraph": "Text",
    "number": "Text",
    "title": "Title",
    "doc_title": "Title",
    "document_title": "Title",
}


@dataclass(frozen=True)
class CategoryStyle:
    """比較ビューアで使うカテゴリ名、色、表示優先度を保持します。"""

    label: str
    color: str


CATEGORY_STYLES = [
    CategoryStyle("Title", "#B42318"),
    CategoryStyle("Section-header", "#0E7490"),
    CategoryStyle("Text", "#2563EB"),
    CategoryStyle("Table", "#64748B"),
    CategoryStyle("Picture", "#E11D48"),
    CategoryStyle("Formula", "#7C2D12"),
    CategoryStyle("List-item", "#65A30D"),
    CategoryStyle("Caption", "#A16207"),
    CategoryStyle("Footnote", "#475569"),
    CategoryStyle("Page-header", "#0F766E"),
    CategoryStyle("Page-footer", "#155E75"),
]


def normalize_category(value: object, default: str = "Text") -> str:
    """各ライブラリ固有のラベルを比較用の 11 カテゴリへ寄せる。"""
    if value is None:
        return default
    raw = str(value).strip()
    if raw in CANONICAL_CATEGORIES:
        return raw
    key = raw.replace(" ", "_").replace("-", "_").lower()
    key = key.replace("__", "_")
    return _CATEGORY_ALIASES.get(key, _CATEGORY_ALIASES.get(raw.lower(), default))
