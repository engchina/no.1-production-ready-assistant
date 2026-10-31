"""Guide識別子は要求のIDであり、文書の事実根拠IDにしない。"""

import pytest

from rag_engine.generation import grounded
from rag_engine.models.llm import GroundedCitation, GroundedDraft, GroundedItem


def check(identity: str, *, guide_ids: tuple[str, ...] = ("guide-step-a",)):
    span = {
        "evidence_id": "stable-a",
        "label": "E1",
        "source_id": "p-a",
        "source": "synthetic.pdf",
        "function": ("synthetic.pdf", "合成の操作"),
        "text": "空条件でも上限件数まで処理します。",
    }
    grounded.format_functions([span])
    citation = GroundedCitation(evidence_id=identity, lines="1")
    citation._wire_range = (1, 1)
    draft = GroundedDraft(
        items=[GroundedItem(kind="operation", text="合成の操作を行います。", citations=[citation])], summary="", confidence="low"
    )
    checked, notes = grounded.verify("操作方法は？", draft, [span], guide_ids=guide_ids)
    return checked[0], notes


def test_exact_current_guide_id_is_a_namespace_error_not_a_fact_source():
    entry, notes = check("guide-step-a")
    assert entry.citation_resolution["reason_code"] == "citation_id_namespace"
    assert entry.citation_resolution["requested_id_namespace"] == "business_guide"
    assert entry.citation_resolution["expected_id_namespace"] == "document_evidence"
    assert entry.citation_resolution["authenticity"] == "unverified"
    assert entry.published and not entry.confirmed
    assert notes[0]["kept"]
    assert entry.marks[0]["code"] == "citation_id_namespace"


@pytest.mark.parametrize("identity", ["unknown", "guide-step-similar"])
def test_similar_prefix_does_not_infer_a_guide_namespace(identity):
    entry, _ = check(identity)
    assert entry.citation_resolution["reason_code"] == "citation_id_invalid"
    assert "requested_id_namespace" not in entry.citation_resolution


def test_real_document_id_takes_precedence_over_guide_name_collision():
    entry, notes = check("E1", guide_ids=("E1",))
    assert entry.citation_resolution["authenticity"] == "verified" and not notes
    assert entry.review_assessment == {}  # 定位は意味支持ではない。


def test_run_round_passes_current_guide_identity_to_basic_citation_check():
    from rag_engine.config import get_settings
    from rag_engine.generation.citation_wire import generation_draft_wire_model

    spans = [
        {
            "evidence_id": "stable-a",
            "label": "E1",
            "source_id": "p-a",
            "source": "synthetic.pdf",
            "function": ("synthetic.pdf", "合成の操作"),
            "text": "対象を選択して処理します。",
        }
    ]
    calls = []

    def generate(system, prompt, settings, schema, **kwargs):
        calls.append(schema)
        assert schema is generation_draft_wire_model()
        return schema.model_validate(
            {
                "items": [
                    {
                        "kind": "operation",
                        "text": "合成の操作を行います。",
                        "citations": [{"evidence_id": "guide-step-a", "line_start": None, "line_end": None}],
                    }
                ],
                "summary": "",
                "confidence": "low",
            }
        )

    result = grounded.run_round(
        "操作方法は？",
        spans,
        get_settings(environ={}, dotenv_path=None),
        prompt=grounded.format_functions(spans),
        image_paths=(),
        provider_id=None,
        parse_text=generate,
        parse_images=lambda *args, **kwargs: pytest.fail("画像呼出は不要"),
        audit_mode="none",
        guide_steps=(("guide-step-a", "合成手順", ("p-a",)),),
    )
    entry = next(row for row in result.checked if row.item.text == "合成の操作を行います。")
    assert len(calls) == 1 and result.review_execution == "not_run"
    assert entry.citation_resolution["reason_code"] == "citation_id_namespace"
    assert not entry.confirmed and entry.published


def test_legacy_free_quote_does_not_turn_guide_identity_into_a_fact_source():
    item = GroundedItem(
        kind="operation", text="合成の操作", citations=[GroundedCitation(evidence_id="guide-step-a", quote="対象を選択します。")]
    )
    span = {"evidence_id": "stable-a", "label": "E1", "text": "対象を選択します。"}
    extracted, error = grounded.cited_quote(item, [span], guide_ids=("guide-step-a",))
    assert error and not extracted.quote
    assert extracted.citations[0]._extraction["reason_code"] == "citation_id_namespace"
