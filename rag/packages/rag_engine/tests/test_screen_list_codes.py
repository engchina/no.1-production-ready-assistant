"""画面の一覧に写った行のコードを、本文に対応の記載が無いまま対象として断定した説明の警告（#2797）。合成の fixture。"""

from __future__ import annotations

from rag_engine.generation import grounded
from rag_engine.models.llm import GroundedCitation, GroundedItem

# 合成のコードの一覧の画面の生成説明（コード K0001〜K0008 が並ぶ。画面の ID は SC100000、パラメータは Prm02）。
SCREEN = (
    "■ 可視情報\n"
    "画面名: コード登録(SC100000)\n"
    "コード・エラー: SC100000 / K0001 / K0002 / K0003 / K0004 / K0005 / K0006 / K0007 / K0008 / Prm02\n"
    "■ 構造\n"
    "表の行:\n"
    "- 1行目: 区分甲 | K0001 | 名称A\n"
    "- 2行目: 区分甲 | K0002 | 名称B\n"
)


def _span(**extra):
    return {"text": SCREEN, "tags": ("image_generated",), "document_texts": [], "function_texts": [], "section_path": [], **extra}


def _item(text):
    return GroundedItem(kind="rule", text=text, citations=[GroundedCitation(quote="K0002 名称B", evidence_id="E1")])


def test_a_listed_row_code_claimed_as_the_target_is_warned():
    reason = grounded._check_screen_list_codes("名称を変えたい", _item("区分甲の K0002 の名称を変更します。"), _span())
    assert "K0002" in reason


def test_codes_backed_by_the_document_text_or_the_question_are_not_warned():
    item = _item("区分甲の K0002 の名称を変更します。")
    assert grounded._check_screen_list_codes("名称を変えたい", item, _span(document_texts=["K0002 は名称B の種別です。"])) == ""
    assert grounded._check_screen_list_codes("K0002 の名称を変えたい", item, _span()) == ""


def test_single_screen_ids_and_parameter_names_are_not_list_rows():
    # 1 つだけの画面の ID・パラメータの名前は一覧の行ではない（同じ形の語が並ばない）。
    item = _item("コード登録画面（SC100000）で Prm02 を確認します。")
    assert grounded._check_screen_list_codes("確認したい", item, _span()) == ""


def test_only_image_generated_spans_are_checked():
    span = {**_span(), "tags": ()}
    assert grounded._check_screen_list_codes("名称を変えたい", _item("K0002 の名称を変更します。"), span) == ""


def test_the_check_is_an_item_warning():
    assert grounded._check_screen_list_codes in grounded.ITEM_CHECKS
    assert grounded.ITEM_CHECK_NAMES[grounded._check_screen_list_codes]
