"""質問の対象語の部分語から作る全文検索クエリ (#730)。"""

from rag_engine.retrieval.target_text_queries import target_phrases, target_text_queries


def _tokenize(text):
    table = {"出荷予定年月": ["出荷", "予定", "年", "月"], "拠点名": ["拠点", "名"], "見積登録画面": ["見積り", "登録", "画面"]}
    return table.get(text, [text])


def test_phrases_include_the_target_windows_and_windows_without_a_single_char_token():
    phrases = target_phrases("出荷予定年月を変更するには、どうしたらよいか。", _tokenize)
    assert phrases[0] == "出荷予定年月"
    assert "予定月" in phrases  # 「年」を抜いた窓。資料の「予定月変更」に届く
    assert "年月" not in phrases  # 単字だけの窓は使わない
    assert target_text_queries("出荷予定年月を変更するには、どうしたらよいか。", _tokenize)[0] == "{出荷予定年月}"


def test_normalized_token_forms_and_generic_targets_are_skipped():
    # tokenizer の正規化形「見積り」でつないだ窓は対象語「見積登録画面」に現れないので使わない
    phrases = target_phrases("見積登録画面はどこにあるのか。", _tokenize)
    assert "見積り登録" not in phrases and "見積登録画面" in phrases
    assert target_text_queries("データを削除するにはどうすればよいか。", _tokenize) == []
    assert target_text_queries("倉庫を移転したので、拠点名を変更する方法を教えてほしい。", _tokenize) == ["{拠点名}"]


def test_existential_object_comes_first_so_it_reaches_the_query_limit():
    """「A に B がある」の B が対象の先頭に入り、画面名の部分語に押し出されない (#1068)。"""
    from rag_engine.retrieval.task_contract import task_contract

    question = "架空在庫照会画面に備考欄があるが、どこで入力できるか。"
    assert task_contract(question)["business_objects"] == ["備考欄", "架空在庫照会画面"]
    assert target_text_queries(question, _tokenize)[0] == "{備考欄}"
    # 存在構文が無い質問の対象抽出は変えない
    assert task_contract("架空会員証の設定方法を教えてください。")["business_objects"] == ["架空会員証"]
    # 「ない」で終わる文は存在構文として採らない
    assert task_contract("架空在庫照会画面に備考欄がない。")["business_objects"] == ["架空在庫照会画面"]
