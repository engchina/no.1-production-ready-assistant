"""docling の PDF の backend がテキスト層の字を落とさないことの回帰テスト(#1505)。

LibreOffice が書き出す PDF は、日本語のフォントを 1 バイトの文字コードの部分フォントに分けて
埋め込み、文字コード 0x20 にも空白でない字を割り当てる(ToUnicode の対応表は正しい)。
docling-parse 7.21 以前はこの 0x20 を ToUnicode によらず空白にしたため、「第 5 章 点検の記録」が
「点検の 録」に、「架空の会社」が「 空の会社」になった。7.22.2 で直っている(pyproject の下限)。

評価のデータセットの PDF(LibreOffice の書き出し)を、Docling の変換と同じ backend で読んで確かめる。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from docling.backend.docling_parse_backend import ThreadedDoclingParseDocumentBackend
from docling.datamodel.base_models import InputFormat
from docling.datamodel.document import InputDocument

_MULTI_HOP = Path(__file__).resolve().parents[4] / "evaluation" / "multi-hop"


def _page_texts(path: Path) -> dict[int, str]:
    """ページ番号(1 始まり)ごとの text cell の文字列。並列の backend はページの順に返さない。"""
    in_doc = InputDocument(
        path_or_stream=path, format=InputFormat.PDF, backend=ThreadedDoclingParseDocumentBackend
    )
    backend = in_doc._backend
    try:
        return {
            page.page_no: "\n".join(cell.text for cell in page.get_text_cells())
            for page in backend.iter_pages()
        }
    finally:
        backend.unload()


@pytest.mark.parametrize(
    ("file_name", "page_no", "expected"),
    [
        # 見出し(太字)の「記」が、部分フォントの文字コード 0x20 にある。
        ("server-room-inspection.pdf", 10, "第 5 章 点検の記録"),
        # 本文の「架」が、別の部分フォントの文字コード 0x20 にある。
        ("server-room-inspection.pdf", 1, "（架空の会社。"),
        # 同じ書き出しの別の文書では「空」が 0x20 にある(Issue の「架 の会社」)。
        ("access-request-procedure.pdf", 1, "（架空の会社。"),
    ],
    ids=["heading-ki", "body-ka", "body-kuu"],
)
def test_glyph_on_code_0x20_is_not_dropped(file_name: str, page_no: int, expected: str) -> None:
    pages = _page_texts(_MULTI_HOP / file_name)

    assert expected in pages[page_no]
