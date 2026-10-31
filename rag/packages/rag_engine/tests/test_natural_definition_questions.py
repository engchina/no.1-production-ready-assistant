"""自然な定義質問の原文・条件・利用者申告を保持する。"""

from rag_engine.retrieval.task_contract import task_contract


def test_hypothetical_validity_is_not_observed_state():
    contract = task_contract("有効な場合はどんな状態ですか。検品は有効かもしれない。")
    assert not contract["intent_parts"]["observed_states"]
