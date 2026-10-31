"""原質問に語を足した検索文の出どころの表示名（#3490）。

段「検索文と検索語の確定」の検索文の出どころ・回答の記録の ``query_source`` は、実際に語を足したもの
（用語・同義語 / 回答ルール / 業務ガイド / 確認で選んだ条件）の名前を並べる。旧名「用語・ルール」は出さない。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rag_engine.knowledge.runtime_knowledge import (
    PINNED_RULE_TAG,
    RUNTIME_KNOWLEDGE_FILE_NAME,
    SUPPORT_GUIDE_RULE_TAG,
    RuntimeKnowledgeContext,
    build_runtime_knowledge_context,
)

_TERM = {"term": "正式名A", "aliases": ["略称A"]}
_RULE = {"id": "rule-a", "title": "条件Bの規則", "triggers": ["条件B"], "content": "条件Bは確認してから答える"}


def _context(tmp_path: Path, question: str, *, terms: list[Any], rules: list[Any]) -> RuntimeKnowledgeContext:
    path = tmp_path / RUNTIME_KNOWLEDGE_FILE_NAME
    path.write_text(json.dumps({"schema_version": 1, "terms": terms, "rules": rules}, ensure_ascii=False), encoding="utf-8")
    return build_runtime_knowledge_context(question, tmp_path, path)


@pytest.mark.parametrize(
    ("question", "terms", "rules", "expected"),
    [
        ("略称A の期限は？", [_TERM], [], ("用語・同義語",)),
        ("条件B の期限は？", [], [_RULE], ("確認・回答ルール",)),
        ("略称A で条件B の期限は？", [_TERM], [_RULE], ("用語・同義語", "確認・回答ルール")),
        (
            "期限は？",
            [],
            [{**_RULE, "id": "guide-1", "triggers": ["手がかりC"], "tags": [PINNED_RULE_TAG, SUPPORT_GUIDE_RULE_TAG]}],
            ("業務ガイド",),
        ),
        ("期限は？", [], [{**_RULE, "triggers": ["手がかりC"], "tags": [PINNED_RULE_TAG]}], ("確認・回答ルール",)),
    ],
    ids=["terms", "rules", "terms-and-rules", "support-guide", "pinned-rule"],
)
def test_query_source_names_what_added_words(
    tmp_path: Path, question: str, terms: list[Any], rules: list[Any], expected: tuple[str, ...]
) -> None:
    context = _context(tmp_path, question, terms=terms, rules=rules)

    assert context.expanded_question != context.original_question
    assert context.expansion_sources == expected
    assert context.query_source == " + ".join(("原質問", *expected))
    assert context.to_payload()["query_source"] == context.query_source


def test_rule_words_already_added_by_terms_are_not_counted(tmp_path: Path) -> None:
    """回答ルールの照合キーワードが用語の別名と同じで、新しい語を足さなければ、回答ルールを出どころに数えない。"""
    rule = {"id": "rule-a", "title": "R-1", "triggers": ["略称A"], "content": "確認してから答える"}
    context = _context(tmp_path, "略称A の期限は？", terms=[_TERM], rules=[rule])

    assert context.matched_rules
    assert context.expansion_sources == ("用語・同義語",)


def test_scope_search_terms_are_named_as_the_confirmed_condition(tmp_path: Path) -> None:
    context = _context(tmp_path, "略称A の期限は？", terms=[_TERM], rules=[]).with_scope("選択: 区分D", ["区分D"])

    assert context.expansion_sources == ("用語・同義語", "確認で選んだ条件")
    assert context.query_source == "原質問 + 用語・同義語 + 確認で選んだ条件"


def test_question_without_added_words_is_the_original_question_only(tmp_path: Path) -> None:
    context = _context(tmp_path, "期限は？", terms=[_TERM], rules=[_RULE])

    assert context.expanded_question == context.original_question
    assert context.expansion_sources == ()
    assert context.query_source == "原質問のみ"
