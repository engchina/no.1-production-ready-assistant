"""Unstructured parser マイクロサービス。

共有 contract(rag_parser_core)の app factory を使い、この image に導入された
unstructured の partition で parse して `StructuredExtraction` を返す。
"""

from rag_parser_core.service import create_parse_app

app = create_parse_app(
    backend="unstructured",
    import_name="unstructured",
    distribution_names=("unstructured",),
    title="parser-unstructured",
)
