"""Oracle Text に索引する search_text の正規化（#1336）。

質問の側は NFKC で互換文字（①・Ⅴ・㈱・㌔・℃）を 1・V・(株)・キロ・°C にする。
WORLD_LEXER は全角 / 半角の違いは同一視するが互換文字は同一視しないため、索引の側も同じ形にする。
表示の本文は原文のまま保つ。
実 Oracle で当たることは ``test_text_search_tokenizer_oracle.py`` が確かめる。
"""

from app.clients.oracle import _chunk_search_text, _feedback_detail_binds
from app.rag.chunking import Chunk
from app.rag.chunking_small_to_big import ENGINE_SEARCH_TEXT_KEY


def test_chunk_search_text_is_normalized_but_chunk_text_is_kept() -> None:
    chunk = Chunk(
        text="手順①で申請します。第Ⅴ章を参照。",
        index=0,
        start_offset=0,
        end_offset=17,
        metadata={"context_header": "規程.pdf > ㈱サンプル"},
    )

    assert _chunk_search_text(chunk) == "規程.pdf > (株)サンプル\n手順1で申請します。第V章を参照。"
    assert chunk.text == "手順①で申請します。第Ⅴ章を参照。"


def test_parent_child_search_text_is_normalized() -> None:
    chunk = Chunk(
        text="上限は５㌔",
        index=0,
        start_offset=0,
        end_offset=5,
        metadata={ENGINE_SEARCH_TEXT_KEY: "文書: 規程\n上限は５㌔、温度は２５℃以下"},
    )

    assert _chunk_search_text(chunk) == "文書: 規程\n上限は5キロ、温度は25°C以下"


def test_feedback_search_text_is_normalized_but_stored_texts_are_kept() -> None:
    binds = _feedback_detail_binds(
        {"question_text": "手順①はどこ", "answer_text": "第Ⅴ章です", "comment_text": "㈱の表記"},
        feedback_id="fb-1",
        tenant_id_hash=None,
    )

    assert binds["search_text"] == "手順1はどこ\n第V章です\n(株)の表記"
    assert binds["question_text"] == "手順①はどこ"
    assert binds["answer_text"] == "第Ⅴ章です"


def test_feedback_without_texts_has_no_search_text() -> None:
    binds = _feedback_detail_binds({}, feedback_id="fb-2", tenant_id_hash=None)

    assert binds["search_text"] is None
