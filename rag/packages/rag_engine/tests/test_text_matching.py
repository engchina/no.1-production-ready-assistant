"""照合の四層と、自然文の意味判断へ昇格させない境界。"""

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from rag_engine.matching.analysis import AnalysisPolicy, Span, analyze, normalize_text
from rag_engine.matching.conditions import Clause, Condition, evaluate, match_clause
from rag_engine.matching.ranking import CandidateMatch, rank_candidates


def clause(name="subject", alternatives=("alpha",), **kwargs):
    return Clause(name, "title", name, alternatives, **kwargs)


@pytest.mark.parametrize(
    "original,normalized",
    [
        (" ＡＢＣ\tStraße  ", "abc strasse"),
        ("ｶﾞとカ\u3099", "ガとガ"),
        ("删除　项目", "删除 项目"),
        ("\u1100\u1161", "가"),
        ("e\u0301", "é"),
    ],
)
def test_analysis_and_key_use_same_normalization(original, normalized):
    value = analyze(original)
    assert value.text == normalize_text(original) == normalized
    assert len(value.text) == len(value.origins)
    assert all(0 <= span.start < span.end <= len(original) for span in value.origins)


@pytest.mark.parametrize(
    "original,label,expected",
    [
        ("前 ｶﾞ 後", "ガ", Span(2, 4)),
        ("前カ\u3099後", "ガ", Span(1, 3)),
        ("前㍿後", "株式会社", Span(1, 2)),
        (" A\t  B ", "a b", Span(1, 6)),
        ("Straße", "ss", Span(4, 5)),
    ],
)
def test_original_offsets_survive_expansion_composition_and_whitespace(original, label, expected):
    result = match_clause(analyze(original), clause(alternatives=(label,), mode="substring"))
    assert result.spans == (expected,)


def test_identifiers_are_not_forced_through_nfkc_or_casefold():
    policy = AnalysisPolicy(normalization="literal", casefold=False, whitespace="preserve")
    exact = clause(alternatives=("Ａ_1",), mode="exact")
    assert match_clause(analyze("Ａ_1", policy), exact).matched
    assert not match_clause(analyze("A_1", policy), exact).matched
    assert not match_clause(analyze(" Ａ_1 ", policy), exact).matched


def test_word_phrase_and_substring_are_separate_contracts():
    text = analyze("alphabet alpha beta")
    assert match_clause(text, clause(mode="term")).spans == (Span(9, 14),)
    assert match_clause(text, clause(alternatives=("alpha beta",), mode="phrase")).spans == (Span(9, 19),)
    assert not match_clause(analyze("alphabet beta"), clause(alternatives=("alpha",), mode="phrase")).matched
    assert match_clause(analyze("alphabet"), clause(mode="substring")).matched
    assert not match_clause(analyze("alphabet"), clause(mode="term")).matched
    assert not match_clause(analyze("alpha beta"), clause(mode="exact")).matched


@pytest.mark.parametrize("text,label", [("設定を削除します", "削除"), ("请删除项目", "删除")])
def test_unspaced_cjk_requires_an_explicit_phrase_or_character_policy(text, label):
    assert match_clause(analyze(text), clause(alternatives=(label,), mode="phrase")).matched
    assert not match_clause(analyze(text), clause(alternatives=(label,), mode="term")).matched
    characters = analyze(text, AnalysisPolicy(segmentation="cjk_characters"))
    assert match_clause(characters, clause(alternatives=(label[0],), mode="term")).matched


def test_all_any_exclude_and_minimum_count_distinct_groups():
    subject = clause()
    action = Clause("action", "triggers", "action", ("remove", "delete"), weight=2)
    duplicate = replace(action, name="action-alias", alternatives=("delete",))
    other = clause("other", ("beta",))
    excluded = clause("excluded", ("gamma",))
    condition = Condition(must=(subject,), should=(action, duplicate, other), exclude=(excluded,), minimum_should_match=2)
    assert evaluate(analyze("alpha delete beta"), condition).eligible
    assert evaluate(analyze("alpha delete"), condition).unmet == ("minimum_should_match",)
    assert "subject" in evaluate(analyze("delete beta"), condition).unmet
    assert "excluded" in evaluate(analyze("alpha delete beta gamma"), condition).unmet


def test_alias_votes_are_capped_and_scores_are_not_confidence():
    first = Clause("one", "title", "operation", ("delete", "remove"), weight=3)
    second = Clause("two", "aliases", "operation", ("delete",), weight=2)
    condition = evaluate(analyze("delete remove delete"), Condition(should=(first, second), minimum_should_match=1))
    candidate = CandidateMatch("a", 0, condition)
    assert candidate.score == 3
    result = rank_candidates((candidate,), limit=1)[0].explanation()
    assert result["score_kind"] == "ranking_only"
    assert "confidence" not in result
    assert len(result["contributions"]) == 1


def test_top_k_ties_and_failed_conditions_are_explained_without_raw_text():
    condition = Condition(should=(clause(),), minimum_should_match=1)
    positive = evaluate(analyze("alpha"), condition)
    negative = evaluate(analyze("beta"), condition)
    candidates = tuple(CandidateMatch(str(index), index, positive) for index in range(1000))
    results = rank_candidates((*candidates, CandidateMatch("missing", 1000, negative)), limit=6)
    assert [result.candidate.key for result in results if result.selected] == list(map(str, range(6)))
    assert results[6].reason == "limit"
    assert results[-1].reason == "unmet_condition"
    assert results[-1].explanation()["unmet"] == ["minimum_should_match"]
    assert "original" not in results[0].explanation()


def test_spans_are_bounded_and_deduplicated_across_aliases():
    result = match_clause(analyze("alpha " * 100), clause(alternatives=("alpha", "ＡＬＰＨＡ")), max_spans=3)
    assert result.spans == (Span(0, 5), Span(6, 11), Span(12, 17))


def test_negative_comparative_and_topic_change_are_not_intent_or_consent():
    condition = Condition(should=(clause(),), minimum_should_match=1)
    for question in ("not alpha", "alpha or beta?", "stop alpha; now beta"):
        result = evaluate(analyze(question), condition)
        assert result.eligible  # 語はある。否定の理解・対象の確定・同意はこの層で行わない。
        assert not hasattr(result, "confirmed")
        assert not hasattr(result, "authorized")


@pytest.mark.parametrize(
    "build",
    [
        lambda: Condition(),
        lambda: Condition(should=(clause(),)),
        lambda: Condition(should=(clause(),), minimum_should_match=2),
        lambda: Condition(must=(clause(), clause())),
        lambda: clause(alternatives=(" ",)),
        lambda: clause(weight=float("nan")),
        lambda: AnalysisPolicy(normalization="unknown"),
    ],
)
def test_invalid_configuration_is_rejected(build):
    with pytest.raises(ValueError):
        build()


def test_foundation_imports_only_pure_standard_library_modules():
    root = Path(__file__).parents[1] / "rag_engine" / "matching"
    allowed = {"__future__", "re", "math", "unicodedata", "dataclasses", "typing"}
    for file in root.glob("*.py"):
        for node in ast.walk(ast.parse(file.read_text())):
            if isinstance(node, ast.Import):
                assert all(alias.name.split(".")[0] in allowed for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                assert (node.module or "").split(".")[0] in allowed
