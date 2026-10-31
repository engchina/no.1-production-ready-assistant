"""質問が示すエラー文の抽出（task_contract.error_messages）を検証する (#686)。"""

from fictional_examples import ERROR_MESSAGE_7, ERROR_QUESTION_15

from rag_engine.retrieval.task_contract import error_messages, task_contract


def test_quoted_errors_in_a_sentence_about_errors_are_extracted_in_order():
    q = f"一括処理で①「{ERROR_QUESTION_15}」、②「{ERROR_MESSAGE_7}」というエラーがでている。何か操作が必要か。"
    assert error_messages(q) == [ERROR_QUESTION_15, ERROR_MESSAGE_7]
    assert task_contract(q)["error_messages"] == error_messages(q)


def test_unquoted_phrase_before_no_error_is_the_last_clause():
    # 対象名や文脈（「入荷検品チェックリストで」）を落とし、区切りの最後の句だけをエラー文にする (#694)。
    assert error_messages("入荷検品チェックリストで定期仕入契約の仕入先コードが登録済みの仕入先ではないのエラーが出ている。") == [
        "定期仕入契約の仕入先コードが登録済みの仕入先ではない"
    ]
    assert error_messages("送信したところ、仕入先コードが違うというエラーで戻ってきた。") == ["仕入先コードが違う"]
    assert error_messages("一括処理ができないのエラーが出た。") == ["一括処理ができない"]  # 「できない」の「で」は区切りにしない


def test_questions_without_errors_or_without_a_message_yield_nothing():
    assert error_messages("取引先名が変わった。どこから操作すればよいか。") == []
    assert error_messages("エラーが出た。どうすればよいか。") == []
    assert error_messages("「はい」を選択したところエラーになった。") == []  # 2 文字の引用はエラー文とみなさない


def test_asks_cause_detects_why_questions():
    """原因・理由を尋ねる標識。経緯だけの文や操作方法の質問には付かない (#713)。"""
    from rag_engine.retrieval.task_contract import asks_cause

    assert asks_cause("返品送料が3月分の月次売上表に反映されていない。どうしてか。")
    assert asks_cause("棚卸件数が一致しないのはなぜか。")
    assert asks_cause("表示されない原因を教えてほしい。")
    assert not asks_cause("拠点名を変更する方法を教えてほしい。")
    assert task_contract("データを削除するにはどうすればよいか。")["asks_cause"] is False


def test_asks_cause_needs_the_cause_noun_to_be_the_target_of_the_question():
    """「原因」「理由」の名詞は、問う形の的のときだけ原因の質問にする。項目の名前の「理由」では付かない (#3491)。"""
    from rag_engine.retrieval.inquiry_conditions import parse_inquiry_conditions
    from rag_engine.retrieval.task_contract import asks_cause

    asked = [
        "申請が承認されなかった理由は何ですか。",
        "送信できない理由は？",
        "エラーの原因と対処方法を教えてください。",
        "何が原因で処理が失敗するのか。",
        "どういう理由で差し戻されたのか。",
        "原因として考えられるものは何ですか。",
    ]
    named = [
        "申請画面Aの差戻しの理由は何文字以上で、期限は何営業日以内ですか。",
        "差戻し理由を入力する手順を教えてください。",
        "理由の欄はどこにありますか。",
        "どんな理由を入力すればよいですか。",
    ]
    for question in asked:
        assert asks_cause(question), question
        assert "reason" in parse_inquiry_conditions(question).decision_types, question
    for question in named:
        assert not asks_cause(question), question
        assert task_contract(question)["asks_cause"] is False, question
        assert "reason" not in parse_inquiry_conditions(question).decision_types, question
