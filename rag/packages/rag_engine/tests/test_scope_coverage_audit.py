"""別画面の誤適用、複合要求の欠落、適切な確認案内の反例を保護する。"""

from rag_engine.retrieval.task_contract import request_units


def test_request_units_preserve_numbered_questions_and_context():
    question = "既に登録済み。①取込方法は？②取消方法は？"
    assert [u["text"] for u in request_units(question)] == ["既に登録済み。", "①取込方法は？", "②取消方法は？"]
