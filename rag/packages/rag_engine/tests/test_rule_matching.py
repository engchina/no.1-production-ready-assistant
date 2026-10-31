"""用途別の旧方針を保った移行と、理由の追加を検証する。"""

import pytest

from benchmarks.text_matching import cases, legacy_rules
from rag_engine.knowledge.rule_matching import rank_rules
from rag_engine.knowledge.runtime_knowledge import RuntimeRule, _tokens


@pytest.mark.parametrize("case", cases(), ids=lambda case: case.name)
def test_same_input_preserves_legacy_selection_and_order(case):
    before = legacy_rules(case.question, case.rules, case.limit)
    after = rank_rules(case.question, case.rules, case.limit, tokens=_tokens)
    assert tuple(rule.rule_id for rule in after.selected) == tuple(rule.rule_id for rule in before) == case.expected


def test_question_offsets_and_top_k_reason_are_retained():
    question = "前 ｶﾞ 後"
    rules = tuple(RuntimeRule(str(index), "合成ルール", ("ガ",)) for index in range(9))
    result = rank_rules(question, rules, 6, tokens=_tokens)
    assert result.decisions[0]["contributions"][0]["spans"] == [{"start": 2, "end": 4}]
    assert result.decisions[0]["offset_source"] == "question_input"
    assert result.decisions[6]["reason"] == "limit"
    assert result.decisions[6]["rank"] == 7
    assert not result.decisions[6]["accepted"]


def test_shared_body_word_does_not_make_a_rule_a_candidate():
    result = rank_rules("alpha", (RuntimeRule("a", "beta", content="alpha"),), 6, tokens=_tokens)
    assert not result.selected
    assert result.decisions[0]["reason"] == "shared_words_only"
    assert result.decisions[0]["unmet"] == ["minimum_should_match"]


def test_zero_or_negative_legacy_limit_still_means_one():
    rules = (RuntimeRule("a", "alpha"), RuntimeRule("b", "alpha"))
    for limit in (0, -1):
        assert tuple(rule.rule_id for rule in rank_rules("alpha", rules, limit, tokens=_tokens).selected) == ("a",)
