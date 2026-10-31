"""構造化レコード JSONL の parser マイクロサービス（#2457）。

前処理（csv_to_jsonl / excel_to_jsonl / word_to_jsonl）が作った構造化レコードの JSON Lines を
契約どおりに読み、
記録を 1 要素にした ``StructuredExtraction`` を返す。検索用テキスト（``raw_text``）は記録の本文を
境界のマーカー（``<<<JSONL_RECORD_BOUNDARY>>>``）で区切り、固定分割（``fixed_delimiter``）が
1 記録 1 chunk にする（解析の実体は ``rag_parser_core.structured_records``）。
共有 contract（``rag_parser_core``）の app factory で、ほかの parser と同じ HTTP 契約
（``POST /parse`` / ``GET /health``）を公開する。外部の parser の package は使わない。
"""

from rag_parser_core.service import create_parse_app

app = create_parse_app(
    backend="structured_records_jsonl",
    import_name="rag_parser_core.structured_records",
    distribution_names=("rag-parser-core",),
    title="parser-structured-records-jsonl",
)
