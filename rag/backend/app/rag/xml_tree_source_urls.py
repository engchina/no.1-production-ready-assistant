"""XML ツリー JSON の chunk に原典の URL を付ける(#2024)。

解析そのものは parser のサービス(parser-xml-tree-json)が行う。XML ツリー JSON は使用エンジンで
選んだときだけ使う(#2042。原本の形式で自動には振り分けない)。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.config import Settings


class _ChunkLike(Protocol):
    metadata: dict[str, str | int | float | bool | None]


def apply_xml_tree_source_urls(chunks: Sequence[_ChunkLike], settings: Settings) -> None:
    """XML ツリー JSON の chunk に原典の URL(``xml_tree_source_url``)を付ける(#2024)。

    ``rag_xml_tree_json_source_url_template`` の ``{document_id}`` を文書 ID に置き換える。ひな形が
    空、または https で始まらないときは付けない(引用カードの「原典を開く」は https だけを開く)。
    """
    template = str(getattr(settings, "rag_xml_tree_json_source_url_template", "") or "").strip()
    if not template.startswith("https://") or "{document_id}" not in template:
        return
    for chunk in chunks:
        document_id = chunk.metadata.get("xml_tree_document_id")
        if isinstance(document_id, str) and document_id.strip():
            chunk.metadata["xml_tree_source_url"] = template.replace(
                "{document_id}", document_id.strip()
            )
