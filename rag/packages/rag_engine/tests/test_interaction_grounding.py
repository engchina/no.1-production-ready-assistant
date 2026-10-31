"""複数要求、項目対応、キーと例示値の誤適用を検証する。"""

import pytest

from rag_engine.generation.interaction_grounding import interaction_binding_error
from rag_engine.generation.value_grounding import unsupported_document_value_claim
from rag_engine.retrieval.task_contract import request_units


def spans(text):
    return [dict(evidence_id="E1", text=text, origin="document_text")]


@pytest.mark.parametrize(
    "question",
    [
        "在庫設定の名称を変更する方法と、過去の記録への影響を教えてください。",
        "配送先の変更手順と確定済み伝票への影響は。",
        "支払変更の方法と再発行の要否を教えて。",
    ],
)
def test_each_explicit_requirement_gets_a_separate_audit_unit(question):
    units = request_units(question)
    facets = [u for u in units if u.get("kind") == "explicit_facet"]
    assert len(facets) == 2
    assert {u["id"] for u in facets} == {"Q1.F1", "Q1.F2"}
    assert all(question.rstrip("。") in u["text"] for u in facets)


def test_single_or_withdrawn_requirement_does_not_get_extra_facets():
    assert len(request_units("変更方法を教えてください。")) == 1
    assert len(request_units("変更方法を教えて。影響の説明は不要です。")) == 2


def test_field_alias_requires_an_explicit_native_relationship():
    source = spans("「配送名」を変更します。「請求名」は請求書に使用します。")
    assert interaction_binding_error("「配送名」（「請求名」）を変更します。", source)
    assert not interaction_binding_error("「配送名」を変更します。", source)
    assert not interaction_binding_error("「配送名」（「請求名」）を変更します。", spans("配送名と請求名は同じ項目の別名です。"))
    assert interaction_binding_error("追加(実行)ボタンを押します。", spans("追加して実行すると登録できます。"))


def test_key_label_pair_in_the_cited_screen_description_is_accepted():
    """画面が 1 枚の文書では、引用元の画面説明そのものが対応の唯一の根拠になる (#776)。"""
    screen = "■ 要点\n回答用本文: 基本設定画面で値を変更する。\n■ 可視情報\nボタン: (F1) メニュー / (F2) 戻る / (F3) / (F4) / (F5) 実行\n"
    source = [dict(evidence_id="E1", text=screen, origin="image_extraction")]

    assert not interaction_binding_error("(F5) 実行ボタンを押します。", source, screen_texts=(screen,))
    # ラベルのない無効ボタンを引き写しただけの列挙は拒まない。
    assert not interaction_binding_error("ボタンは(F1)メニュー/(F3)/(F5)実行です。", source, screen_texts=(screen,))
    # 根拠に無いキーと、根拠と食い違う対応は従来どおり拒む。
    assert interaction_binding_error("(F9) 保存ボタンを押します。", source, screen_texts=(screen,))
    assert interaction_binding_error("(F2) 保存ボタンを押します。", source, screen_texts=(screen,))


def test_button_named_in_the_same_function_body_is_accepted():
    # 手順が隣の span に分かれても、同じ機能の本文にあるボタン名は根拠あり (#676)。別機能にしかなければ従来どおり検出する。
    quoted = spans("営業所の取引先名を変更します。\n〔マスタ管理⇒マスタ管理 2 タブ⇒取引先登録〕")
    answer = "取引先登録画面で取引先名を変更し、「実行」ボタンを押す。"
    assert interaction_binding_error(answer, quoted) == "案内したボタン名が引用本文にない"
    assert not interaction_binding_error(
        answer, quoted, body_texts=["①取引先名、略称、カナ名など変わる箇所を変更します。\n②実行ボタンを押します。"]
    )
    assert interaction_binding_error(answer, quoted, body_texts=["①追加するグループのコードを入力します。"])
    # 画面の生成説明（screen_texts）はボタン名の存在証明には使わない (#637)。
    assert interaction_binding_error(answer, quoted, screen_texts=["画面下部に実行ボタンがある。"])


def test_non_numeric_sample_group_is_not_the_actual_group():
    source = spans("画面の例ではグループは「通知書」です。登録帳票一覧で所属を確認します。")
    assert unsupported_document_value_claim("今回のグループは「通知書」です。", source, "証明書の所属は？")
    assert not unsupported_document_value_claim("図の例は「通知書」です。", source, "図の例は？")
    assert not unsupported_document_value_claim("今回のグループは「通知書」です。", source, "私のグループは「通知書」です。")
    assert not unsupported_document_value_claim("登録帳票一覧で確認し、そのグループを選択します。", source, "所属は？")


def test_user_date_is_not_mistaken_for_a_document_example():
    source = spans("対象月はR年.月形式で入力します。図の例はR8.4です。")
    assert not unsupported_document_value_claim("今回の対象月はR12.8です。", source, "令和12年8月を指定したい。")
    assert unsupported_document_value_claim("今回の対象月はR8.4です。", source, "令和12年8月を指定したい。")


def test_dates_in_the_paraphrase_are_not_required_as_rule_values():
    # 「平成27年度」の 27 は日付で、上限の規則値ではない。規則文での裏付けを日付にまで要求しない (#569)。
    span = {
        "origin": "document_text",
        "text": (
            "<table><tr><td>① 月間上限額 の引き上げ</td><td>月間上限額は 100 万円です。</td>"
            "<td>月間上限額は、 120 万円まで 引き上げられます。</td></tr></table>改正後の制度は平成 28 年 1 月 1 日から施行されます。"
        ),
    }
    for text in (
        "平成27年度改正で積立プランの月間上限額は改正前100万円から改正後120万円に変わります。",
        "平成28年1月1日から月間上限額は120万円です。",
        "2016年4月から上限は120万円です。",
    ):
        assert not unsupported_document_value_claim(text, [span], "上限額は？"), text
    # 規則値そのものが原文の規則文になければ、日付が付いていても降格する。
    assert unsupported_document_value_claim("平成27年度改正で月間上限額は150万円です。", [span], "上限額は？")
    # 画像の説明にしかない値は、従来どおり規則へ昇格させない。
    image = {"origin": "image_extraction", "text": "入力欄の上限は 99 です。"}
    assert unsupported_document_value_claim("上限は99です。", [image], "上限は？")


def test_document_rule_sentence_backs_the_values_without_rule_words():
    # 本文の規則は「上限」「最大」の語が無くても規則を述べる。言い換えの「最大」で降格しない (#2209)。
    source = spans("貸出期間は14日以内とする。延長は1回に限り、7日まで認める。")
    assert not unsupported_document_value_claim("貸出は14日以内で、延長は1回に限り最大7日まで認められます。", source, "延長できますか。")
    # 本文にない値は従来どおり降格する（「日」の数は日付として外すので、回数で確かめる）。
    assert unsupported_document_value_claim("延長は最大3回まで認められます。", source, "延長できますか。")
    # 表示例の文の値は、本文にあっても規則の裏付けにしない。
    example = spans("画面の例では延長の回数は5回と表示されます。")
    assert unsupported_document_value_claim("延長は最大5回までです。", example, "延長の上限は？")
