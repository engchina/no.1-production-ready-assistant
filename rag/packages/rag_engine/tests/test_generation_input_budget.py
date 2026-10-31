"""根拠ブロックと固定入力の予算を、本文の途中切断や必須根拠の除外なしに守る。"""

from dataclasses import replace

import pytest

from rag_engine.config import get_settings
from rag_engine.generation.input_budget import GenerationInputBudgetExceeded, ensure_model_input, fit_generation_evidence


def settings(**overrides):
    base = get_settings(environ={}, dotenv_path=None)
    providers = {key: replace(value, max_output_tokens=100) for key, value in base.llm_providers.items()}
    return replace(base, llm_providers=providers, **overrides)


def fit(spans, config, **options):
    return fit_generation_evidence(
        spans,
        config,
        render=lambda selected: "質問\n" + block(selected),
        evidence_text=block,
        system=options.pop("system", "指示"),
        schema=options.pop("schema", {"type": "object"}),
        **options,
    )


def block(spans):
    return "\n".join("出典情報|" + s["text"] for s in spans)


def test_rendered_metadata_counts_in_character_cap_and_keeps_complete_body():
    spans = [{"evidence_id": str(i), "text": "本文" * 20} for i in range(4)]
    selected, prompt, diagnostic = fit(spans, settings(evidence_budget_chars=100))
    assert selected == spans[:2]
    assert len(block(selected)) <= 100
    assert selected[0]["text"] == spans[0]["text"]
    assert prompt.endswith(block(selected))
    assert diagnostic["excluded_span_count"] == 2


def test_pinned_and_previous_citations_are_kept_before_optional_evidence():
    spans = [{"evidence_id": str(i), "text": "本文" * 20, "pinned": i == 3} for i in range(4)]
    selected, _, _ = fit(spans, settings(evidence_budget_chars=100), protected_ids={"2"})
    assert selected == spans[2:]


def test_fixed_system_schema_and_output_reservation_reduce_available_input():
    spans = [{"evidence_id": str(i), "text": "本文" * 20} for i in range(4)]
    config = settings(evidence_budget_chars=80000, answer_max_input_tokens=500)
    _, _, base = fit(spans, config)
    selected, _, measured = fit(spans, config, system="指示" * 120)
    assert len(selected) < len(spans)
    assert measured["output_reserved_tokens"] == 100
    assert measured["available_input_tokens"] == 400
    assert measured["estimated_input_tokens"] <= 400
    assert base["excluded_span_count"] == 0
    with pytest.raises(GenerationInputBudgetExceeded):
        fit(spans, config, schema={"description": "形式" * 300})


def test_required_text_that_cannot_fit_is_not_sent_or_truncated():
    spans = [{"evidence_id": "required", "text": "本文" * 100, "pinned": True}]
    with pytest.raises(GenerationInputBudgetExceeded) as raised:
        fit(spans, settings(evidence_budget_chars=50))
    assert raised.value.diagnostic["reason"] == "required_evidence_or_fixed_input_exceeds_limit"
    assert len(spans[0]["text"]) == 200


def test_final_repair_input_checks_immutable_draft_and_feedback_too():
    config = settings(answer_max_input_tokens=400)
    ensure_model_input(config, "指示", "本文", {"type": "object"})
    with pytest.raises(GenerationInputBudgetExceeded) as raised:
        ensure_model_input(config, "指示", "草稿と指摘" * 200, {"type": "object"})
    assert raised.value.diagnostic["reason"] == "final_model_input_exceeds_limit"


def test_synthesis_does_not_send_fixed_input_that_cannot_fit():
    from test_grounded_answer import context, record

    from rag_engine.dependencies import AnswerDependencies, bind_dependencies
    from rag_engine.generation.answering import synthesize_grounded_answer

    def unused(*args, **kwargs):
        pytest.fail("入力の予算外ではモデルを呼ばない")

    config = settings(answer_max_input_tokens=100)
    with bind_dependencies(AnswerDependencies(unused, unused, unused, unused, unused)):
        response = synthesize_grounded_answer(
            "資料の内容は？", context(record(0, "合成の本文です。", "合成の説明")), config, image_prompt_mode="text_only"
        ).response
    trace = response.generation_trace
    assert trace["generation_input_budget"]["fits"] is False
    assert trace["llm_calls"] == 0
    assert trace["generation_failure"]["error"] == "GenerationInputBudgetExceeded"


def test_budget_notice_is_compatible_with_existing_published_language_pack():
    from rag_parser_core.language import build_pack, builtin_pack

    current = builtin_pack()
    saved = current.to_dict()
    saved["answer_labels"].pop("answer_evidence_budget_limited")
    loaded = build_pack(saved)
    assert loaded.ingest_hash == current.ingest_hash
    assert loaded.text("answer_labels", "answer_evidence_budget_limited").format(count=2)
