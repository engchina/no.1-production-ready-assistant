"""CSV→構造化レコード JSONL 前処理マイクロサービス(#2457)。

共有 contract(rag_parser_core)の app factory を使い、CSV を決定論で構造化レコードの
JSONL へ再マップして `ConvertResponse` を返す。純 Python のみで完結する軽量サービスで、
他 parser / backend に非干渉。
"""

from rag_parser_core.preprocess import ConvertHealth, supported_profiles_from
from rag_parser_core.preprocess_service import create_preprocess_app

from app.converters import convert


def _health() -> ConvertHealth:
    """CSV→JSONL は外部依存なしで常時 ready。"""
    return ConvertHealth(
        status="ok",
        backend="preprocess-csv-to-jsonl",
        package_name="csv2json",
        package_version="v2",
        supported_profiles=supported_profiles_from(["csv_to_jsonl"]),
    )


app = create_preprocess_app(
    converter=convert, health_probe=_health, title="preprocess-csv-to-jsonl"
)
