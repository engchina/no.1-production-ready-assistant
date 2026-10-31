"""拡張語の関連性と明示別名の維持を、業務固有の例外なしで検証する。"""

import json

import pytest

from rag_engine.knowledge.runtime_knowledge import build_runtime_knowledge_context
from rag_engine.retrieval.task_contract import filter_queries


@pytest.mark.parametrize(
    "question,title,identifier",
    [
        ("到来者 リスト", "所得届一覧", "YIPrint-02"),
        ("在庫 一覧", "給与明細", "PAY900"),
    ],
)
def test_shared_word_does_not_import_other_business(tmp_path, question, title, identifier):
    (tmp_path / "runtime_knowledge.json").write_text(
        json.dumps(
            {
                "rules": [{"id": "r", "title": title, "triggers": [identifier], "content": question.split()[-1]}],
                "terms": [{"term": title, "aliases": [identifier], "description": question.split()[-1]}],
            }
        ),
        encoding="utf-8",
    )
    result = build_runtime_knowledge_context(question, tmp_path)
    assert result.expanded_question == question
    assert not result.prompt_context()
    assert len(result.expansion_decisions) == 2
    assert all(d["reason"] == "shared_words_only" and not d["accepted"] for d in result.expansion_decisions)


def test_matching_rule_trigger_does_not_imply_other_trigger_alias(tmp_path):
    (tmp_path / "runtime_knowledge.json").write_text(
        json.dumps(
            {
                "rules": [{"id": "r", "title": "入庫条件", "triggers": ["入庫", "PAY900"], "content": "条件を確認"}],
            }
        ),
        encoding="utf-8",
    )
    result = build_runtime_knowledge_context("入庫したい", tmp_path)
    assert "PAY900" not in result.expanded_question
    assert result.expansion_decisions[0]["matched_labels"] == ["入庫"]


def test_explicit_glossary_alias_remains_supported(tmp_path):
    (tmp_path / "runtime_knowledge.json").write_text(
        json.dumps(
            {
                "terms": [{"term": "在庫照会", "aliases": ["STK100", "在庫検索"]}],
            }
        ),
        encoding="utf-8",
    )
    result = build_runtime_knowledge_context("在庫検索の方法", tmp_path)
    assert "STK100" in result.expanded_question
    accepted, rejected = filter_queries(
        result.original_question, [result.expanded_question], grounded_text=" ".join(result.matched_terms[0].labels())
    )
    assert accepted and not rejected


def test_rewrite_guard_keeps_original_and_synonym_but_rejects_new_identifier():
    accepted, rejected = filter_queries("在庫照会 STK100", ["在庫照会 STK100", "在庫検索", "給与 PAY900"])
    assert accepted == ("在庫照会 STK100", "在庫検索")
    assert rejected == ({"query": "給与 PAY900", "reason": "原質問・明示別名にない業務識別子"},)
