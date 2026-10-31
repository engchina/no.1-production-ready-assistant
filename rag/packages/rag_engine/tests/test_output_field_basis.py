"""出力データの項目の裏付けを、同じ成果物の定義と原文引用で確かめる。"""

import pytest

from rag_engine.evaluation.answer_validation import ValidationClaimCheck, _enforce_support
from rag_engine.generation import grounded
from rag_engine.models.llm import GroundedAudit, GroundedCitation, GroundedItem

QUOTE = "ファイル A の列は区分と年度です。"


@pytest.mark.parametrize(
    "quote,matches,supported",
    [
        ("", True, False),
        ("", None, False),
        (QUOTE, False, False),
        (QUOTE, None, False),
        (QUOTE, True, True),
    ],
)
def test_both_audits_require_the_definition_and_output_identity(quote, matches, supported):
    fields = {"asserts_output_fields": True, "definition_quote": quote, "output_artifact_matches": matches}
    text = "ファイル A に区分列があります。"
    entry = grounded.CheckedItem(
        GroundedItem(kind="operation", text=text, citations=[GroundedCitation(quote=QUOTE, evidence_id="E1")]),
        {"text": QUOTE, "evidence_id": "E1"},
    )
    review = {"index": 0, "support": "supported", "applicability": "matched", "reason": "合成の判定", "output_fields": fields}
    grounded.apply_audit([entry], GroundedAudit(reviews=[review], summary_supported=True, goal_alignment="aligned"))
    assert (not entry.marks) is supported
    claim = ValidationClaimCheck(
        answer_quote=text,
        answer_passage_id="A1",
        status="supported",
        source_id="c1",
        evidence_id="E1",
        evidence_quote=QUOTE,
        reason="合成の判定",
        output_fields=fields,
    )
    result = _enforce_support(
        [claim], [{"id": "A1", "text": claim.answer_quote}], {"E1": {"id": "c1", "text": QUOTE}}, {"c1": QUOTE}, [QUOTE]
    )
    assert (result[0].status == "supported") is supported
    if not supported:
        assert result[0].audit_status == "supported"
        assert result[0].support_check == "output_field_basis_unconfirmed"


@pytest.mark.parametrize("carried", [False, True])
def test_screen_filter_is_not_a_definition_even_for_a_carried_item(carried):
    quote = "画面 A の抽出条件: 区分"
    entry = grounded.CheckedItem(
        GroundedItem(
            kind="operation",
            text="出力データ A の区分列で絞り込みます。",
            citations=[GroundedCitation(quote=quote, evidence_id="E1")],
        ),
        {"text": quote, "evidence_id": "E1"},
        carried=carried,
    )
    audit = GroundedAudit(
        reviews=[
            {
                "index": 0,
                "support": "supported",
                "applicability": "matched",
                "reason": "画面項目",
                "output_fields": {"asserts_output_fields": True, "definition_quote": "", "output_artifact_matches": True},
            }
        ],
        summary_supported=True,
        goal_alignment="aligned",
    )
    grounded.apply_audit([entry], audit)
    assert any(mark["code"] == "unsupported" for mark in entry.marks)
    assert not entry.removed


def test_invented_definition_is_not_supported_by_either_audit():
    fields = {"asserts_output_fields": True, "definition_quote": "ファイル A に年度列があります。", "output_artifact_matches": True}
    quote = "抽出条件は年度です。"
    item = GroundedItem(
        kind="operation",
        text="ファイル A の年度列で絞ります。",
        citations=[GroundedCitation(quote=quote, evidence_id="E1")],
    )
    entry = grounded.CheckedItem(item, {"text": quote, "evidence_id": "E1"})
    audit = GroundedAudit(
        reviews=[{"index": 0, "support": "supported", "applicability": "matched", "reason": "合成", "output_fields": fields}],
        summary_supported=True,
        goal_alignment="aligned",
    )
    grounded.apply_audit([entry], audit)
    assert entry.marks
    claim = ValidationClaimCheck(
        answer_quote=item.text, answer_passage_id="A1", status="supported", source_id="c1", reason="合成", output_fields=fields
    )
    result = _enforce_support([claim], [{"id": "A1", "text": item.text}], {}, {"c1": quote}, [quote])
    assert result[0].status == "unsupported"


def test_confirmed_export_operation_is_retained_and_unsupported_summary_is_detected():
    quote = "一覧 A を選びファイル出力を実行します。"
    entry = grounded.CheckedItem(
        GroundedItem(kind="operation", text=quote, citations=[GroundedCitation(quote=quote, evidence_id="E1")]),
        {"text": quote, "evidence_id": "E1"},
    )
    audit = GroundedAudit(
        reviews=[{"index": 0, "support": "supported", "applicability": "matched", "reason": "出力操作", "output_fields": None}],
        summary_supported=True,
        goal_alignment="aligned",
    )
    grounded.apply_audit([entry], audit)
    assert not entry.marks and not entry.removed
    bad = "出力ファイルの年度列で絞ります。"
    assert grounded.summary_asserts_denied(bad, [bad], [quote])
    assert not grounded.summary_asserts_denied(quote, [bad], [quote])


def test_paragraph_without_column_claim_keeps_supported_with_an_empty_object():
    """列を主張しない段落に空の照合が付いても、出力ファイルの項目定義を求めない（#2821）。"""
    fields = {"asserts_output_fields": False, "definition_quote": "", "output_artifact_matches": None}
    quote = "抽出ボタンを押すと一覧に対象が表示されます。"
    claim = ValidationClaimCheck(
        answer_quote="抽出ボタンを押すと一覧に対象が表示されます。",
        answer_passage_id="A1",
        status="supported",
        source_id="c1",
        evidence_id="E1",
        evidence_quote=quote,
        reason="合成の判定",
        output_fields=fields,
    )
    result = _enforce_support(
        [claim], [{"id": "A1", "text": claim.answer_quote}], {"E1": {"id": "c1", "text": quote}}, {"c1": quote}, [quote]
    )
    assert result[0].status == "supported"
    entry = grounded.CheckedItem(
        GroundedItem(kind="operation", text=quote, citations=[GroundedCitation(quote=quote, evidence_id="E1")]),
        {"text": quote, "evidence_id": "E1"},
        carried=True,
    )
    audit = GroundedAudit(
        reviews=[{"index": 0, "support": "unsupported", "applicability": "matched", "reason": "揺れ", "output_fields": fields}],
        summary_supported=True,
        goal_alignment="aligned",
    )
    grounded.apply_audit([entry], audit)
    # 持ち越した支持済みの item は、列を主張しない照合だけで印を増やさない。
    assert not entry.marks
    assert (
        grounded.summary_output_fields_error(
            GroundedAudit.model_validate({**audit.model_dump(), "summary_output_fields": fields}), [], quote
        )
        == ""
    )


@pytest.mark.parametrize(
    "text",
    [
        "送信解除ボタンを押した後、実行ボタンを押して送信中を解除します。",
        "必要項目を入力して実行ボタンを押します。",
        "CSV出力ボタンで出力できます。",
    ],
)
def test_operation_paragraph_is_not_checked_even_when_the_model_claims_columns(text):
    """モデルが列を主張したと申告しても、文が出力物の列・項目を述べていなければ照合しない（#2821 の実モデルの出力）。"""
    fields = {"asserts_output_fields": True, "definition_quote": "", "output_artifact_matches": True}
    assert not grounded.mentions_output_fields(text)
    claim = ValidationClaimCheck(
        answer_quote=text,
        answer_passage_id="A1",
        status="supported",
        source_id="c1",
        evidence_id="E1",
        evidence_quote=text,
        reason="合成の判定",
        output_fields=fields,
    )
    result = _enforce_support([claim], [{"id": "A1", "text": text}], {"E1": {"id": "c1", "text": text}}, {"c1": text}, [text])
    assert result[0].status == "supported"
    entry = grounded.CheckedItem(
        GroundedItem(kind="operation", text=text, citations=[GroundedCitation(quote=text, evidence_id="E1")]),
        {"text": text, "evidence_id": "E1"},
    )
    review = {"index": 0, "support": "supported", "applicability": "matched", "reason": "合成", "output_fields": fields}
    audit = GroundedAudit(reviews=[review], summary_supported=True, goal_alignment="aligned")
    grounded.apply_audit([entry], audit)
    assert not entry.marks
    summary_audit = GroundedAudit.model_validate({**audit.model_dump(), "summary_output_fields": fields})
    assert grounded.summary_output_fields_error(summary_audit, [], text) == ""
