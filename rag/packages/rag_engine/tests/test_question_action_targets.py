from rag_engine.retrieval.task_contract import action_targets, task_contract


def test_action_targets_take_the_object_of_the_requested_operation():
    """要求文の「X を<操作語>する／したい」の X を原文の語で返す (#700)。"""
    assert action_targets(
        "月末にまとめて印刷する請求書の備考欄について、3行のうち1行を変更したい。ロゴ画像を変更するにはどうしたらよいか。"
    ) == ["ロゴ画像"]
    assert action_targets("倉庫を移転したので、拠点名を変更する方法を教えてほしい。") == ["拠点名"]
    assert action_targets("得意先ごとに登録している掛率を一括で変更することは可能か。") == ["掛率"]
    assert action_targets("権限を設定するにはどのように操作するのか。店長と経理担当者に権限を付与したい。") == ["権限"]


def test_action_targets_skip_history_generic_words_and_numbers():
    """経緯の過去形、総称語、数字始まりの語は対象にしない (#700)。"""
    assert action_targets("見積書を印刷したところ、伝票タイトルが旧社名のままになっているが、どこから変更すればよいか。") == []
    assert action_targets("締め日を更新する前に売上計上ボタンを押してしまった。データを削除するにはどうすればよいか。") == []
    assert action_targets("承認欄のうち1枠を変更したい。") == []
    assert task_contract("単価データの取込はデータ連携＞単価データ取込からでよいか。")["action_targets"] == []
