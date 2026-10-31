"""Office→PDF 前処理マイクロサービス。

共有 contract(rag_parser_core)の app factory を使い、この image に導入された
LibreOffice で Office→PDF 変換して `ConvertResponse` を返す。変換依存は本サービス単独で
upgrade でき、他 parser / backend に非干渉。
"""

from rag_parser_core.preprocess import ConvertHealth, supported_profiles_from
from rag_parser_core.preprocess_service import create_preprocess_app

from app.converters import convert, soffice_path


def _health() -> ConvertHealth:
    """LibreOffice の導入状況を返す(readiness 表示の値ソース)。"""
    office_ready = soffice_path() is not None
    return ConvertHealth(
        status="ok" if office_ready else "degraded",
        backend="preprocess-office-to-pdf",
        package_name="libreoffice",
        package_version=None,
        supported_profiles=supported_profiles_from(
            ["office_to_pdf"] if office_ready else ["passthrough"]
        ),
    )


app = create_preprocess_app(
    converter=convert, health_probe=_health, title="preprocess-office-to-pdf"
)
