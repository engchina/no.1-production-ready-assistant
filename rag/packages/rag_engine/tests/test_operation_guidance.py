"""操作案内の十分性と、未確認データ・資料を残す契約の回帰テスト。"""

from unittest.mock import patch

import pytest
from test_task_contract_audit import record

from rag_engine.generation.answer_policy import OPERATION_GUIDANCE_POLICY
from rag_engine.generation.answering import (
    AnswerContext,
    _grade_crag_retrieval,
)
from rag_engine.knowledge.prompt_files import DEFAULT_VLM_ANSWER_PROMPT
from rag_engine.models.llm import CragRetrievalGradeOutput
from rag_engine.retrieval.task_contract import task_contract


# 直接説明できる確認方法があれば、別観点の不足・未確認を保持した部分回答へ進める。
@pytest.mark.parametrize(
    "aspect,status,source,reason,duplicate,expected_unknown,expected_missing",
    [
        ("requested_result", "data_confirmation", "a", "現在の出力を閲覧し位置を照合", False, (), ()),
        ("applicability", "data_confirmation", "a", "設定値を閲覧し適用条件と照合", False, (), ()),
        ("requested_result", "data_confirmation", "unknown", "実値を照合", False, ("requested_result",), ()),
        ("requested_result", "data_confirmation", "a", " ", False, ("requested_result",), ()),
        ("requested_result", "data_confirmation", "a", "実値を照合", True, ("requested_result",), ()),
        ("procedure", "data_confirmation", "a", "操作自体は未確認", False, ("procedure",), ()),
        ("business_object", "data_confirmation", "a", "対象不明", False, ("business_object",), ()),
        ("applicability", "missing", "a", "適用版の文書が不足", False, (), ("applicability",)),
    ],
)
def test_crag_verified_confirmation_method_preserves_other_aspect_gaps(
    aspect, status, source, reason, duplicate, expected_unknown, expected_missing
):
    question = "印字位置の確認方法"
    r = record("a", "帳票設定画面で位置設定を確認し、現在の出力と照合する。")
    checks = [
        dict(aspect=k, status="supported", source_ids=["a"], evidence=[dict(source_id="a", quote=r.text)], reason="操作の根拠")
        for k in task_contract(question)["required_aspects"]
    ]
    target = next(c for c in checks if c["aspect"] == aspect)
    target.update(status=status, source_ids=[source], evidence=[dict(source_id=source, quote=r.text)], reason=reason)
    if duplicate:
        checks.append(dict(target))
    output = CragRetrievalGradeOutput(
        sufficient=True,
        confidence=0.9,
        candidate_verdicts=[dict(chunk_uid="a", relevant=True, reason="関連")],
        reason="操作案内",
        rewritten_query="",
        aspect_checks=checks,
        answerable_parts=[dict(aspect="requested_result", kind="verification", claim=r.text, evidence=[dict(source_id="a", quote=r.text)])],
    )
    with patch("rag_engine.generation.answering.parse_text_response", return_value=output) as parse:
        result = _grade_crag_retrieval(1, question, question, [question], AnswerContext(records=[r], text=r.text), object())
    assert result.generation_usable
    assert result.unknown_aspects == expected_unknown
    assert result.missing_aspects == expected_missing
    # 判定 prompt には共有方針の全文ではなく、十分性に関わる要約だけを載せる (#957)。
    assert OPERATION_GUIDANCE_POLICY not in parse.call_args.args[1]
    assert "実値が未提供でも、根拠にある手順・規則・確認方法を説明できれば十分とする" in parse.call_args.args[1]


def test_default_and_optional_evaluation_use_guidance_goal():
    from rag_engine.evaluation.answer_eval import CLAIM_PROMPT, COVERAGE_PROMPT
    from rag_engine.generation.grounded import GENERATE_SYSTEM_PROMPT

    # 生成 prompt は共有方針の全文を結合せず、items 向けに書いた方針を持つ (#960)。編集可能テンプレートには重複させない。
    assert OPERATION_GUIDANCE_POLICY not in GENERATE_SYSTEM_PROMPT
    assert "### 例示値と実データ" in GENERATE_SYSTEM_PROMPT
    # 表の列の役割の表示（#1281）は、前処理が本文に付ける表示（rag_parser_core の
    # COLUMN_ROLE_TEXT_LABELS。backend の test_sheet_records が両方の一致を確かめる）と同じ文字で指示する。
    for label in ("資料の既定値", "例示の値", "記載時点の設定値", "資料の推奨値", "設定できる範囲"):
        assert f"［{label}］" in GENERATE_SYSTEM_PROMPT
    assert "資料の値を今の値として断定しない" in GENERATE_SYSTEM_PROMPT
    assert "今の設定値として断定した説明は誤り" in OPERATION_GUIDANCE_POLICY
    assert OPERATION_GUIDANCE_POLICY not in DEFAULT_VLM_ANSWER_PROMPT
    # 品質評価の判定の指示は判定の基準だけを持ち、回答の生成の方針は入れない（#3576）。
    assert OPERATION_GUIDANCE_POLICY not in CLAIM_PROMPT + COVERAGE_PROMPT
    assert "未提供なら `confidence=low`" not in GENERATE_SYSTEM_PROMPT + DEFAULT_VLM_ANSWER_PROMPT
    assert "「関連資料を追加する」" not in GENERATE_SYSTEM_PROMPT + DEFAULT_VLM_ANSWER_PROMPT
