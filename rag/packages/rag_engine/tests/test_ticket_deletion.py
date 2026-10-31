"""#367: 引用実在を誤承認しても別対象の削除を公開しない。"""

import pytest

from rag_engine.generation.operation_audit import deletion_targets, is_operation_instruction

QUESTION = "店舗端末の期限付クーポンを削除する方法を教えてください。"


@pytest.mark.parametrize("text", ["履歴を削除します。", "履歴を消去してください。", "一括削除できます。", "Delete the record."])
def test_deletion_is_an_operation(text):
    assert is_operation_instruction(text)


def test_delete_target_is_explicit():
    assert deletion_targets(QUESTION) == ["期限付クーポン"]
    assert deletion_targets("「一時データ」の削除方法は？") == ["一時データ"]
    assert deletion_targets("削除方法は？") == []
    # 総称語だけの対象は検査しない。修飾語付きは対象 (#696)。
    assert deletion_targets("売上計上ボタンを押してしまった。データを削除するにはどうすればよいか。") == []
    assert deletion_targets("情報を削除したい。") == []
    assert deletion_targets("出荷情報を削除するには。") == ["出荷情報"]
    assert deletion_targets("掛率データを削除したい。") == ["掛率データ"]


def test_same_target_original_can_support_deletion():
    from rag_engine.generation.operation_audit import deletion_target_supported

    assert deletion_target_supported(QUESTION, [dict(text="期限付クーポンを選択して削除します。")])
    assert not deletion_target_supported(QUESTION, [dict(text="出力履歴を削除します。", source="期限付クーポン.pdf")])
