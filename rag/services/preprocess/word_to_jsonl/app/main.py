"""Word(.doc/.docx)→構造化レコード JSONL 前処理マイクロサービス（#2457）。

共有 contract(rag_parser_core)の app factory を使い、Word の本文を見出しの階層で読んで、段落・
リスト・表のまとまりを記録にした構造化レコード JSONL の `ConvertResponse` を返す。.doc は
LibreOffice で .docx にしてから読む。python-docx・LibreOffice は本サービスだけが持ち、他の
parser / backend に非干渉。
"""

from rag_parser_core.preprocess import ConvertHealth, supported_profiles_from
from rag_parser_core.preprocess_service import create_preprocess_app

from app.converters import convert, soffice_path


def _health() -> ConvertHealth:
    """python-docx と LibreOffice の導入状況を返す(readiness 表示の値ソース)。

    .docx は python-docx だけで読める。LibreOffice が無いときは .doc を変換できないため degraded。
    """
    try:
        import docx  # noqa: F401

        docx_ready = True
    except ImportError:
        docx_ready = False
    office_ready = soffice_path() is not None
    return ConvertHealth(
        status="ok" if docx_ready and office_ready else "degraded",
        backend="preprocess-word-to-jsonl",
        package_name="python-docx+libreoffice",
        package_version=None,
        supported_profiles=supported_profiles_from(
            ["word_to_jsonl"] if docx_ready else ["passthrough"]
        ),
    )


app = create_preprocess_app(
    converter=convert, health_probe=_health, title="preprocess-word-to-jsonl"
)
