"""Excel(.xls/.xlsx)→構造化 JSON 前処理マイクロサービス。

共有 contract(rag_parser_core)の app factory を使い、Excel をシート単位のレコード配列
JSON へ再マップして `ConvertResponse` を返す。openpyxl(.xlsx)+ xlrd(.xls)依存は本
サービス image にのみ含め、他 parser / backend に非干渉。
"""

from rag_parser_core.preprocess import ConvertHealth, supported_profiles_from
from rag_parser_core.preprocess_service import create_preprocess_app

from app.converters import convert, openpyxl_available, xlrd_available


def _health() -> ConvertHealth:
    """openpyxl / xlrd の導入状況を返す(readiness 表示の値ソース)。"""
    xlsx_ready = openpyxl_available()
    xls_ready = xlrd_available()
    return ConvertHealth(
        status="ok" if (xlsx_ready or xls_ready) else "degraded",
        backend="preprocess-excel-to-jsonl",
        package_name="openpyxl+xlrd",
        package_version=None,
        supported_profiles=supported_profiles_from(
            ["excel_to_jsonl"] if (xlsx_ready or xls_ready) else ["passthrough"]
        ),
    )


app = create_preprocess_app(
    converter=convert, health_probe=_health, title="preprocess-excel-to-jsonl"
)
