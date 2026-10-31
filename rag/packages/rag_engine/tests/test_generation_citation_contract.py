"""実際の生成入口から整数範囲の変換と、補写材料の現在ラベルへの接線を確かめる。"""

import pytest
from test_citation_extraction_provenance import spans

from rag_engine.config import get_settings
from rag_engine.generation import grounded
from rag_engine.generation.citation_wire import generation_draft_wire_model


@pytest.mark.parametrize(
    "start,end,expected",
    [
        (2, 3, "extracted"),
        (None, None, "citation_range_missing"),
        (2, None, "citation_range_invalid"),
        (2, 999, "citation_range_out_of_bounds"),
    ],
)
def test_run_round_uses_integer_wire_and_preserves_unverified_body(start, end, expected):
    evidence = spans()
    text = "対象を選択して内容を保存します。"
    calls = []

    def generate(system, prompt, settings, schema, **kwargs):
        calls.append(schema)
        assert schema is generation_draft_wire_model()
        return schema.model_validate(
            {
                "items": [
                    {
                        "kind": "operation",
                        "text": text,
                        "citations": [{"evidence_id": evidence[0]["label"], "line_start": start, "line_end": end}],
                    }
                ],
                "summary": "",
                "confidence": "low",
            }
        )

    result = grounded.run_round(
        "対象の操作手順は？",
        evidence,
        get_settings(environ={}, dotenv_path=None),
        prompt=grounded.format_functions(evidence),
        image_paths=(),
        provider_id=None,
        parse_text=generate,
        parse_images=lambda *args, **kwargs: pytest.fail("画像の呼出しは不要"),
        audit_mode="none",
    )
    assert len(calls) == 1
    entry = next(entry for entry in result.checked if entry.item.text == text)
    assert entry.item.citations[0]._wire_range == (start, end)
    assert entry.published and not entry.removed
    assert entry.citation_resolution.get("reason_code", entry.citation_resolution["status"]) == expected
    if expected != "extracted":
        assert entry.citation_resolution["authenticity"] == "unverified"
        assert not entry.confirmed


def test_context_rebinds_omitted_operation_support_to_current_display_id():
    evidence = spans("設定内容を保存します。")
    actual = evidence[0]
    original = {
        "unanswered_requests": [
            {
                "request_id": "Q1",
                "status": "partial",
                "evidence_status": "available",
                "missing_content": "設定内容の保存",
                "evidence_ids": [actual["evidence_id"]],
                "evidence_support": [{"evidence_id": actual["evidence_id"], "source": actual["source"], "quote": actual["text"]}],
            }
        ]
    }
    rendered = grounded._relabeled_feedback(original, evidence)["unanswered_requests"][0]
    assert rendered["evidence_ids"] == [actual["label"]]
    assert rendered["evidence_support"][0]["evidence_id"] == actual["label"]
    assert rendered["evidence_support"][0]["quote"] == actual["text"]
    assert original["unanswered_requests"][0]["evidence_ids"] == [actual["evidence_id"]]
