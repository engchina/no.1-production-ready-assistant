"""要約だけに付加された出力項目の照合。資料・項目は合成。"""

import pytest
from test_grounded_answer import FakeModel, audit, context, draft, item, record, removed, run


@pytest.mark.parametrize(
    ("definition", "matches", "in_source", "warning"),
    [
        ("", None, False, True),
        ("別のファイルの列: 状態", False, True, True),
        ("CSVの列: 状態", True, False, True),
        ("CSVの列: 状態", True, True, False),
    ],
)
def test_uncalibrated_output_field_observation_preserves_summary_and_raw_evidence(definition, matches, in_source, warning):
    operation = "一覧表をCSVに出力できます。"
    summary = "CSVには状態列が含まれます。"
    body = operation + ("\n" + definition if in_source else "")
    ctx = context(record(1, body, "出力画面"))
    from rag_engine.generation.answering import _grounded_spans

    spans = _grounded_spans("CSVの項目を確認したい", ctx, (), None)
    label = next(span["label"] for span in spans if operation in span["text"])
    model = FakeModel(
        [draft(item(label, operation, operation), summary=summary)],
        [
            {
                **audit((0, "supported", "matched", ""), requests=[("Q1", "addressed")]),
                "summary_output_fields": {
                    "asserts_output_fields": True,
                    "definition_quote": definition,
                    "output_artifact_matches": matches,
                },
            }
        ],
    )
    result = run(model, ctx, "CSVの項目を確認したい").response
    assert result.answer_text.startswith(summary)
    assert operation in result.answer_text
    assert not [mark for mark in result.envelope["passage_marks"] if mark["text"] == summary]
    assert result.generation_trace["finalization"]["summary"]["status"] == "kept"
    observed = result.generation_trace["semantic_audit_observation"]["audit"]
    assert observed["summary_output_fields"] == {
        "asserts_output_fields": True,
        "definition_quote": definition,
        "output_artifact_matches": matches,
    }
    effective = result.generation_trace["rounds"][0]["audit"]
    assert effective["summary_output_fields"] is None
    assert result.generation_trace["semantic_audit_observation"]["adopted"] is False
    assert removed(result) == []


def test_summary_without_output_field_claim_keeps_normal_audit_behavior():
    from rag_engine.generation.grounded import summary_output_fields_error
    from rag_engine.models.llm import GroundedAudit

    normal = GroundedAudit(reviews=[], summary_supported=True, goal_alignment="aligned")
    assert summary_output_fields_error(normal, [], "CSVには状態列が含まれます。") == ""
    assert normal.summary_supported is True
