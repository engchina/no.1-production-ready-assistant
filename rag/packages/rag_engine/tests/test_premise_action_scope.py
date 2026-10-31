"""実行の禁止と提案の禁止を別に扱う（匿名の合成応答）。"""

from unittest.mock import patch

import pytest

from rag_engine.evaluation.answer_validation import (
    PremiseConsistencyOutput,
    ValidationClaimCheck,
    _enforce_support,
    premise_conflicts,
)


@pytest.mark.parametrize(
    ("scope", "mode", "question", "expected"),
    [
        ("execution_only", "proposal", "変更は実行しないでください。対応案を示してください。", False),
        ("execution_only", "explanation", "変更は実行しないでください。影響を説明してください。", False),
        ("execution_only", "execution", "変更は実行しないでください。", True),
        ("proposal", "proposal", "変更案を提案しないでください。", True),
        ("content", "proposal", "対象 A だけを変更してください。", True),
        ("unknown", "proposal", "対応を検討してください。", False),
        ("execution_only", "unknown", "変更は実行しないでください。", False),
    ],
)
def test_conflict_adoption_respects_action_scope(scope, mode, question, expected):
    passage = "設定 A の変更を候補として示します。"
    check = {
        "answer_passage_id": "A1",
        "constraints": "条件",
        "recommendation": "案",
        "fit": "conflicts",
        "constraint_scope": scope,
        "passage_mode": mode,
        "constraint_quote": question,
        "passage_quote": passage,
    }
    diagnostics = []
    with patch("rag_engine.evaluation.answer_validation._parse_checked", return_value=PremiseConsistencyOutput(checks=[check])) as parse:
        conflicts = premise_conflicts(
            question,
            [{"id": "A1", "text": passage}],
            [{"question": "対象は？", "answer": "対象 A"}],
            None,
            diagnostics=diagnostics,
        )
    assert parse.call_count == 1
    assert ("A1" in conflicts) is expected
    assert diagnostics[0]["fit"] == "conflicts"
    assert diagnostics[0]["adopted"] is expected
    claim = ValidationClaimCheck(
        answer_quote=passage,
        answer_passage_id="A1",
        status="supported",
        source_id="c1",
        evidence_quote=passage,
        applicability="matched",
        reason="資料による裏付け",
    )
    [result] = _enforce_support([claim], [{"id": "A1", "text": passage}], {}, {"c1": passage}, [], premise_conflicts=conflicts)
    assert result.status == ("contradicted" if expected else "supported")


@pytest.mark.parametrize("change", ["missing_scope", "unknown_id", "duplicate_id", "missing_id", "bad_constraint", "bad_passage"])
def test_unbound_or_incomplete_conflict_never_overrides_support(change):
    check = {
        "answer_passage_id": "A1",
        "constraints": "実行禁止",
        "recommendation": "実行",
        "fit": "conflicts",
        "constraint_scope": "execution_only",
        "passage_mode": "execution",
        "constraint_quote": "実行しない",
        "passage_quote": "実行しました。",
    }
    if change == "missing_scope":
        del check["constraint_scope"]
    elif change == "unknown_id":
        check["answer_passage_id"] = "A99"
    elif change == "bad_constraint":
        check["constraint_quote"] = "提案も禁止する"
    elif change == "bad_passage":
        check["passage_quote"] = "資料に無い文章"
    checks = [] if change == "missing_id" else [check, check] if change == "duplicate_id" else [check]
    diagnostics = []
    with patch("rag_engine.evaluation.answer_validation._parse_checked", return_value=PremiseConsistencyOutput(checks=checks)):
        assert not premise_conflicts(
            "実行しない",
            [{"id": "A1", "text": "実行しました。"}],
            [{"answer": "対象 A"}],
            None,
            diagnostics=diagnostics,
        )
    assert diagnostics and all(not item["adopted"] for item in diagnostics)


def test_ignored_execution_conflict_does_not_bypass_quote_verification():
    claim = ValidationClaimCheck(
        answer_quote="変更案を示します。",
        answer_passage_id="A1",
        status="supported",
        source_id="c1",
        evidence_quote="原文にはない引用。",
        support_quote="原文にはない引用。",
        applicability="matched",
        reason="合成の監査応答",
    )
    [result] = _enforce_support([claim], [{"id": "A1", "text": claim.answer_quote}], {}, {"c1": "確認してから変更します。"}, [])
    assert result.status == "citation_error"
    assert result.support_check == "quote_unmatched"


@pytest.mark.parametrize("error", [RuntimeError("synthetic failure"), TimeoutError("synthetic timeout")])
def test_failed_comparison_keeps_prior_support_and_failure_status(error):
    diagnostics = []
    with patch("rag_engine.evaluation.answer_validation._parse_checked", side_effect=error):
        assert not premise_conflicts(
            "案を示してください。",
            [{"id": "A1", "text": "変更案です。"}],
            [{"answer": "実行しない"}],
            None,
            diagnostics=diagnostics,
        )
    assert diagnostics == [
        {"status": "timed_out" if isinstance(error, TimeoutError) else "failed", "decision": "check_failed", "adopted": False}
    ]
