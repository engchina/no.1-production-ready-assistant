"""未校正の意味観察だけで要約を削らず、引用の基本検査は別に保つ。"""

from dataclasses import replace
from unittest.mock import patch

import pytest
from test_grounded_answer import FakeModel, audit, context, draft, ids, item, marks, record, run

from rag_engine.config import get_settings

QUESTION = "設定を変更する手順を教えてください。"
TEXT = "追加ボタンがあります。実行ボタンを押します。"
GOOD = "実行ボタンを押します。"
CLAIM = "設定ボタンで一覧を初期化できます。"
PARAPHRASE = "設定機能を使えば一覧の初期化が可能です。"


@pytest.mark.parametrize(
    "quote,support,summary",
    [
        ("存在しない引用", "supported", CLAIM),
        ("存在しない引用", "supported", PARAPHRASE),
        ("追加ボタンがあります。", "unsupported", PARAPHRASE),
        ("追加ボタンがあります。", "contradicted", PARAPHRASE),
        ("追加ボタンがあります。", "unknown", PARAPHRASE),
    ],
)
def test_mixed_body_keeps_summary_with_semantic_support_unverified(quote, support, summary):
    ctx = context(record(0, TEXT, "合成設定", source="synthetic.pdf"))
    eid = ids(ctx, QUESTION)[TEXT]
    reviews = ((0, support, "matched", ""), (1, "supported", "matched", ""))
    if quote == "存在しない引用":
        reviews = ((0, "supported", "matched", ""),)
    model = FakeModel(
        [draft(item(eid, CLAIM, quote, request_id="Q1"), item(eid, GOOD, GOOD, request_id="Q1"), summary=summary)],
        [audit(*reviews, requests=[("Q1", "addressed")], summary_supported=True)],
    )
    with patch("test_grounded_answer.get_settings", return_value=replace(get_settings(), answer_max_correction_rounds=0)):
        result = run(model, ctx, QUESTION).response
    assert result.answer_text.startswith(summary)
    assert result.generation_trace["semantic_audit_policy"]["mode"] == "diagnostic_only"
    assert result.generation_trace["semantic_audit_observation"]["adopted"] is False
    assert GOOD in result.answer_text
    assert CLAIM in result.answer_text
    if quote == "存在しない引用":
        assert "quote_unmatched" in marks(result)[CLAIM]
    else:
        assert CLAIM not in marks(result)
    assert len(model.prompts) == 2


def test_summary_basis_is_recorded_without_adopting_the_untrusted_review():
    ctx = context(record(0, TEXT, "合成設定", source="synthetic.pdf"))
    eid = ids(ctx, QUESTION)[TEXT]
    model = FakeModel(
        [draft(item(eid, CLAIM, "追加ボタンがあります。", request_id="Q1"), item(eid, GOOD, GOOD, request_id="Q1"), summary=GOOD)],
        [
            {
                **audit(
                    (0, "unsupported", "matched", ""),
                    (1, "supported", "matched", ""),
                    requests=[("Q1", "addressed")],
                    summary_supported=True,
                ),
                "summary_basis": [GOOD],
            }
        ],
    )
    with patch("test_grounded_answer.get_settings", return_value=replace(get_settings(), answer_max_correction_rounds=0)):
        result = run(model, ctx, QUESTION).response
    assert result.answer_text.startswith(GOOD)
    assert CLAIM in result.answer_text
    assert CLAIM not in marks(result)
    observed = result.generation_trace["semantic_audit_observation"]
    assert observed["audit"]["summary_basis"] == [GOOD]
    assert observed["audit"]["reviews"][0]["support"] == "unsupported"
    assert observed["adopted"] is False
    assert len(model.prompts) == 2
