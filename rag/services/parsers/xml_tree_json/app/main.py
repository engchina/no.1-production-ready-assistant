"""XML ツリー JSON の parser マイクロサービス（#2024）。

XML の要素の木を tag / attr / children の JSON にした文書を、Markdown などに変換せず
要素の階層のまま読み、段落を 1 要素にした ``StructuredExtraction`` を返す
（解析の実体は ``rag_parser_core.xml_tree_json``）。
共有 contract（``rag_parser_core``）の app factory で、ほかの parser と同じ HTTP 契約
（``POST /parse`` / ``GET /health``）を公開する。外部の parser の package は使わない。
"""

from rag_parser_core.service import create_parse_app

app = create_parse_app(
    backend="xml_tree_json",
    import_name="rag_parser_core.xml_tree_json",
    distribution_names=("rag-parser-core",),
    title="parser-xml-tree-json",
)
