"""任意の根拠縮小を実際の生成入力境界で確認する。外部モデルは呼ばない。"""

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from audit_fixtures import legacy_audit_result
from openai import OpenAI
from test_bound_audit_transport import response_bytes

from rag_engine.adapters import oci
from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import answering, grounded
from rag_engine.generation.answer_models import AnswerContext, AnswerRecord
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.knowledge.runtime_knowledge import RuntimeKnowledgeContext, RuntimeRule
from rag_engine.models.audit_wire import is_audit_wire

QUESTION = "対象Aを開く方法を教えてください。"
OPEN = "対象Aの一覧から対象を選んで詳細画面を開きます。"
SAVE = "詳細画面の内容を確かめ、保存して変更を確定します。"
NOISE = "無関係な対象Bの装飾設定を説明する除外用本文。"
RULE = "未確認の条件は断定せず、操作順序を保って説明します。"


def record(key: str, text: str, index: int) -> AnswerRecord:
    return AnswerRecord(
        id=key,
        chunk_id=key,
        chunk_uid=f"run:{key}",
        source_run_id="run",
        engine="docling",
        engine_label="Docling",
        category="Chunk",
        chunk_level="parent",
        page=index + 1,
        seq_no=index,
        text=text,
        source=f"document-{key}.pdf",
        metadata={"section_path": [f"合成章{index}"]},
    )


def context() -> AnswerContext:
    records = [record("open", OPEN, 0), record("save", SAVE, 1), record("noise", NOISE, 2)]
    return AnswerContext(records=records, text="\n".join(row.text for row in records))


class Parser:
    """実wire型を使う生成stub。意味の判定能力は模擬しない。"""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.responses = responses or []
        self.calls: list[dict[str, Any]] = []

    def __call__(self, system: str, prompt: str, settings: Any, schema: Any, **options: Any) -> Any:
        assert schema is generation_draft_wire_model(), "none で追加の監査を呼ばない"
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        if self.responses:
            value = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
            if isinstance(value, Exception):
                raise value
            if callable(value):
                return schema.model_validate(value(prompt))
            return schema.model_validate(value)
        return schema.model_validate(payload())


def payload(*, gap: bool = False) -> dict[str, Any]:
    return {
        "summary": "対象の画面を開く方法です。",
        "confidence": "medium",
        "request_coverage": [
            {"request_id": "Q1", "evidence_ids": ["E1"], "evidence_scope": [{"evidence_id": "E1", "target": "対象A", "relation": "direct"}]}
        ],
        "items": [
            {"kind": "gap", "text": "保存方法はまだ確かめられません。", "request_id": "Q1", "citations": []}
            if gap
            else {
                "kind": "operation",
                "text": OPEN,
                "request_id": "Q1",
                "citations": [{"evidence_id": "E1", "line_start": 1, "line_end": 1}],
            }
        ],
    }


def run(parser: Parser, *, mode: str = "cited_only", knowledge: RuntimeKnowledgeContext | None = None, **changes: Any) -> Any:
    config = replace(
        get_settings(environ={}, dotenv_path=None),
        **{
            "answer_audit_mode": "none",
            "answer_evidence_refinement_mode": mode,
            "answer_max_correction_rounds": 1,
            "confirmed_premises": (("操作権限", "権限あり"),),
            "confirmed_premise_sources": (("操作権限", "権限あり", "user"),),
            **changes,
        },
    )

    def unused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("外部I/Oは不要")

    with bind_dependencies(AnswerDependencies(unused, unused, parser, unused, unused)):
        return answering.synthesize_grounded_answer(QUESTION, context(), config, image_prompt_mode="text_only", runtime_knowledge=knowledge)


def test_legacy_keeps_initial_generation_input_and_does_not_add_calls() -> None:
    parser = Parser()
    result = run(parser, mode="legacy")
    assert len(parser.calls) == 1
    assert all(text in parser.calls[0]["prompt"] for text in (OPEN, SAVE, NOISE))
    assert result.response.generation_trace["evidence_refinement"]["stop_reason"] == "disabled"
    assert OPEN in result.response.answer_text


def test_none_opt_in_sends_only_selected_evidence_and_never_audits(monkeypatch: pytest.MonkeyPatch) -> None:
    original = answering._grounded_spans
    prepared: list[list[dict[str, Any]]] = []

    def contaminated(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        spans = original(*args, **kwargs)
        for span in spans:
            if OPEN in span["text"]:
                span.update(answer_context=NOISE, function_texts=[NOISE], metadata={"extra": NOISE})
        prepared.append(spans)
        return spans

    monkeypatch.setattr(answering, "_grounded_spans", contaminated)
    parser = Parser()
    result = run(parser)
    assert len(parser.calls) == 2
    first, second = (row["prompt"] for row in parser.calls)
    assert NOISE in first
    assert OPEN in second and NOISE not in second and SAVE not in second
    assert "[E1]" in second and "[E2]" not in second and "[E3]" not in second
    assert "document-open.pdf" in second and "document-noise.pdf" not in second
    trace = result.response.generation_trace
    assert trace["evidence_refinement"]["additional_generation_calls"] == 1
    assert trace["evidence_refinement"]["semantic_verification"] == "not_performed_by_selection"
    assert all(row["review_execution"] == "not_run" for row in trace["rounds"])
    assert prepared


def test_required_uncited_original_guide_rules_and_confirmed_condition_reach_second_input() -> None:
    knowledge = RuntimeKnowledgeContext(
        original_question=QUESTION,
        expanded_question=QUESTION,
        path=Path("unused.json"),
        matched_rules=(RuntimeRule(rule_id="rule-a", title="合成回答ルール", content=RULE, purpose="generation"),),
        guide_steps=(("open", "画面を開く", ("run:open",)), ("save", "変更を保存する", ("run:save",))),
        guide_task_scope=(
            {"step_id": "open", "required": True},
            {"step_id": "save", "required": True, "depends_on": ["open"]},
        ),
        guide_step_contracts=(
            {"step_id": "save", "purpose": "変更を保存する", "requirements": [{"id": "save:0", "text": "保存して確定する"}]},
        ),
    )
    parser = Parser()
    result = run(parser, mode="cited_required", knowledge=knowledge)
    assert len(parser.calls) == 2
    second = parser.calls[1]["prompt"]
    assert OPEN in second and SAVE in second and NOISE not in second
    assert RULE in second and "保存して確定する" in second
    assert '"answer": "権限あり"' in second and '"source": "user"' in second
    assert "[E1]" in second and "[E2]" in second and "[E3]" not in second
    selection = result.response.generation_trace["evidence_refinement"]["selection"]
    assert selection["label_policy"] == "preserved"
    assert selection["candidate_missing_required_groups"] == []


def test_zero_correction_budget_cannot_add_refinement_generation() -> None:
    parser = Parser()
    result = run(parser, answer_max_correction_rounds=0)
    assert len(parser.calls) == 1
    assert result.response.generation_trace["evidence_refinement"]["stop_reason"] == "correction_limit"
    assert OPEN in result.response.answer_text


def test_deadline_before_refinement_keeps_initial_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(grounded.RoundBudget, "allows_round", lambda self, number: number == 0)
    parser = Parser()
    result = run(parser)
    assert len(parser.calls) == 1
    assert result.response.generation_trace["evidence_refinement"]["stop_reason"] == "deadline"
    assert OPEN in result.response.answer_text


@pytest.mark.parametrize("failure", [RuntimeError("synthetic failure"), TimeoutError("synthetic timeout")])
def test_refinement_exception_keeps_initial_answer_without_another_retry(failure: Exception) -> None:
    parser = Parser([payload(), failure])
    result = run(parser)
    assert len(parser.calls) == 2
    assert OPEN in result.response.answer_text
    trace = result.response.generation_trace["evidence_refinement"]
    assert trace["execution"] == "error" and trace["initial_answer_retained"]
    assert trace["error_type"] == type(failure).__name__


def test_shorter_gap_does_not_replace_useful_initial_operation() -> None:
    parser = Parser([payload(), payload(gap=True)])
    result = run(parser)
    assert len(parser.calls) == 2
    assert OPEN in result.response.answer_text
    assert result.response.generation_trace["evidence_refinement"]["adopted"] is False


@pytest.mark.parametrize("streamed", [False, True])
def test_sdk_serializes_only_selected_second_round_sources(monkeypatch: pytest.MonkeyPatch, streamed: bool) -> None:
    sent: list[dict[str, Any]] = []
    schema = generation_draft_wire_model()
    raw = json.dumps(schema.model_validate(payload()).model_dump())

    def transport(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        data, headers = response_bytes(raw, streamed)
        return httpx.Response(200, content=data, headers=headers)

    client = OpenAI(
        api_key="mock-only",
        base_url="https://mock.invalid/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(transport)),
    )
    monkeypatch.setattr(oci, "_openai_client_for_provider", lambda *_args: client)
    monkeypatch.setattr(oci, "_stall_detection_enabled", lambda _settings: streamed)
    sdk_settings = get_settings(
        environ={
            "OCI_ENTERPRISE_AI_DEFAULT_MODEL": "synthetic",
            "OCI_ENTERPRISE_AI_ENDPOINT": "https://mock.invalid/v1",
            "OCI_ENTERPRISE_AI_API_KEY": "mock-only",
        },
        dotenv_path=None,
        llm_stall_resends=0,
    )

    class SdkParser(Parser):
        def __call__(self, system: str, prompt: str, settings: Any, schema: Any, **options: Any) -> Any:
            assert schema is generation_draft_wire_model()
            self.calls.append({"prompt": prompt, "system": system})
            return oci._parse_text_once(system, prompt, sdk_settings, schema, sdk_settings.llm_providers[sdk_settings.default_answer_llm])

    parser = SdkParser()
    try:
        result = run(parser)
    finally:
        client.close()
    assert len(sent) == len(parser.calls) == 2
    original, refined = [next(row["content"] for row in body["input"] if row["role"] == "user") for body in sent]
    assert NOISE in original
    assert NOISE not in json.dumps(sent[1], ensure_ascii=False)
    assert SAVE not in refined and OPEN in refined
    assert "[E1]" in refined and "[E2]" not in refined and "[E3]" not in refined
    assert all(body["text"]["format"]["strict"] is True for body in sent)
    assert all(body.get("stream", False) is streamed for body in sent)
    assert sent[0]["text"]["format"] == sent[1]["text"]["format"]
    assert result.response.generation_trace["evidence_refinement"]["additional_generation_calls"] == 1


def test_kept_nonconsecutive_label_id_and_numeric_range_are_not_rebound(monkeypatch: pytest.MonkeyPatch) -> None:
    original_context = context()
    expanded = OPEN + "\n" + "補足の表示項目を確認します。" * 20
    changed = replace(original_context.records[0], text=expanded)
    reordered = AnswerContext(records=[original_context.records[2], changed, original_context.records[1]], text="")
    monkeypatch.setattr(__import__(__name__), "context", lambda: reordered)
    value = payload()
    value["request_coverage"][0]["evidence_ids"] = ["E2"]
    value["request_coverage"][0]["evidence_scope"][0]["evidence_id"] = "E2"
    value["items"][0]["citations"] = [{"evidence_id": "E2", "line_start": 1, "line_end": 1}]
    checked: list[Any] = []
    original = grounded.run_round

    def observe(*args: Any, **kwargs: Any) -> Any:
        current = original(*args, **kwargs)
        checked.append(current)
        return current

    monkeypatch.setattr(grounded, "run_round", observe)
    parser = Parser([value])
    run(parser)
    assert len(parser.calls) == len(checked) == 2
    assert "[E2]" in parser.calls[1]["prompt"] and "[E1]" not in parser.calls[1]["prompt"]
    proofs = [current.checked[0].citation_resolution for current in checked]
    for key in ("requested_evidence_id", "requested_lines", "source_id", "resolved_quote", "content_sha256"):
        assert proofs[0][key] == proofs[1][key]
    assert proofs[1]["requested_lines"] == "1"
    assert proofs[1]["requested_evidence_id"] == "E2"
    assert proofs[1]["authenticity"] == "verified"


def test_actual_joint_citations_keep_both_original_sources_and_their_order() -> None:
    value = payload()
    value["items"][0]["text"] = OPEN + SAVE
    value["items"][0]["citations"].append({"evidence_id": "E2", "line_start": 1, "line_end": 1})
    value["request_coverage"][0]["evidence_ids"].append("E2")
    value["request_coverage"][0]["evidence_scope"].append({"evidence_id": "E2", "target": "対象A", "relation": "direct"})
    parser = Parser([value])
    result = run(parser)
    assert len(parser.calls) == 2
    second = parser.calls[1]["prompt"]
    assert OPEN in second and SAVE in second and NOISE not in second
    assert second.index("[E1]") < second.index("[E2]")
    selection = result.response.generation_trace["evidence_refinement"]["selection"]
    assert len(selection["selected_evidence_ids"]) == 2
    assert selection["semantic_support_adopted"] is False


def test_removing_unresolved_gap_alone_is_not_a_verified_improvement() -> None:
    initial = payload()
    initial["items"].append(payload(gap=True)["items"][0])
    parser = Parser([initial, payload()])
    result = run(parser)
    assert len(parser.calls) == 2
    assert OPEN in result.response.answer_text
    assert "保存方法はまだ確かめられません。" in result.response.answer_text
    trace = result.response.generation_trace["evidence_refinement"]
    assert trace["adopted"] is False
    assert trace["adoption"] == "previous_gap_resolution_unverified"
    assert "no_deterministic_improvement" in trace["adoption_reasons"]


def test_legacy_and_opt_in_send_identical_initial_system_schema_and_context() -> None:
    legacy, selected = Parser(), Parser()
    run(legacy, mode="legacy")
    run(selected, mode="cited_only")
    assert len(legacy.calls) == 1 and len(selected.calls) == 2
    assert legacy.calls[0] == selected.calls[0]


def test_one_resolved_request_does_not_hide_another_unresolved_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(__import__(__name__), "QUESTION", "対象Aを開く方法は？対象Cを閉じる方法は？")
    parser = Parser()
    result = run(parser, mode="cited_required")
    assert len(parser.calls) == 1
    trace = result.response.generation_trace["evidence_refinement"]
    assert trace["stop_reason"] == "unresolved_preservation_scope"
    assert trace["selection"]["unresolved_requirement_ids"] == ["Q2"]
    assert all(text in parser.calls[0]["prompt"] for text in (OPEN, SAVE, NOISE))
    assert OPEN in result.response.answer_text


def test_refinement_permission_error_propagates_instead_of_publishing_fallback() -> None:
    parser = Parser([payload(), PermissionError("synthetic permission rejection")])
    with pytest.raises(PermissionError):
        run(parser)
    assert len(parser.calls) == 2


@pytest.mark.parametrize("mode", ["standard", "detailed"])
@pytest.mark.parametrize("corrections", [0, 1])
def test_audited_modes_share_generation_budget_without_selection_audit(mode: str, corrections: int) -> None:
    class AuditedParser(Parser):
        def __init__(self) -> None:
            super().__init__()
            self.audit_calls: list[str] = []

        def __call__(self, system: str, prompt: str, settings: Any, schema: Any, **options: Any) -> Any:
            if schema is generation_draft_wire_model():
                return super().__call__(system, prompt, settings, schema, **options)
            assert is_audit_wire(schema)
            self.audit_calls.append(prompt)
            # 実モデルの意味判定ではなく、既存監査の呼出範囲だけを制御する。
            return legacy_audit_result(
                {
                    "goal_alignment": "aligned",
                    "summary_supported": True,
                    "reviews": [
                        {
                            "index": 0,
                            "support": "supported",
                            "applicability": "matched",
                            "source_applicability": "matched",
                            "reason": "",
                        }
                    ],
                    "request_reviews": [{"request_id": name, "status": "addressed", "reason": ""} for name in schema.expected_request_ids],
                },
                schema,
            )

    parser = AuditedParser()
    result = run(parser, answer_audit_mode=mode, answer_max_correction_rounds=corrections)
    expected = 1 + corrections
    assert len(parser.calls) == len(parser.audit_calls) == expected
    trace = result.response.generation_trace
    assert len(trace["rounds"]) == expected
    assert trace["evidence_refinement"]["additional_generation_calls"] == corrections
    assert all(row["review_execution"] == "completed" for row in trace["rounds"])
    assert OPEN in result.response.answer_text


def _round(entries: list[Any], summary: str = "画面を開く方法です。", **changes: Any) -> Any:
    from rag_engine.models.llm import GroundedDraft

    return grounded.Round(
        summary=summary,
        draft=GroundedDraft(summary=summary, items=[entry.item for entry in entries], confidence="medium"),
        checked=entries,
        **{"dropped": [], "audit": None, "requests": [{"id": "Q1", "text": QUESTION}], **changes},
    )


def _claim(text: str, source: str, quote: str, *, condition: str = "", marks: tuple[str, ...] = ()) -> Any:
    from rag_engine.models.llm import GroundedItem

    item = GroundedItem(
        kind="operation", text=text, request_id="Q1", condition=condition, applies="conditional" if condition else "matched"
    )
    entry = grounded.CheckedItem(
        item.model_copy(update={"quote": quote}),
        span={"evidence_id": f"e-{source}", "source_id": source, "text": quote},
        citation_resolution={"source_id": source, "resolved_quote": quote, "status": "extracted", "authenticity": "verified"},
    )
    for code in marks:
        entry.mark(code, "合成の印")
    return entry


NONE_TIMING = {"audit": grounded.AUDIT_MODE_NONE}


def test_changed_summary_alone_is_not_a_deterministic_improvement() -> None:
    from rag_engine.generation.refinement_round import adoption_check

    current = _round([_claim(OPEN, "run:open", OPEN)])
    candidate = _round([_claim(OPEN, "run:open", OPEN)], summary="変更の確定まで完了できます。", timing=NONE_TIMING)
    decision = adoption_check(candidate, current, {})
    assert (decision.adopted, decision.reason) == (False, "no_deterministic_improvement")
    assert decision.checks["summary_changed"] is True
    assert current.summary == "画面を開く方法です。"


@pytest.mark.parametrize("change", ["reworded", "condition"], ids=["reworded", "condition"])
def test_none_adopts_reworded_claim_kept_by_anchor_and_marks_it_unassessed(change: str) -> None:
    from rag_engine.generation.refinement_round import adoption_check

    current = _round([_claim(OPEN, "run:open", OPEN), _claim(SAVE, "run:save", SAVE, marks=("quote_unmatched",))])
    rewritten = (
        _claim("一覧で対象を選ぶと詳細画面が開きます。", "run:open", OPEN)
        if change == "reworded"
        else _claim(OPEN, "run:open", OPEN, condition="承認済みの場合")
    )
    candidate = _round([rewritten], summary="一覧から詳細画面を開けます。", timing=NONE_TIMING)
    decision = adoption_check(candidate, current, {})
    assert (decision.adopted, decision.reasons) == (True, ("deterministic_checks_improved",))
    assert decision.checks["before"]["marked"] == 1 and decision.checks["after"]["marked"] == 0
    assert decision.checks["previous_confirmed_claims_lost"] == 0
    assert decision.checks["new_or_reworded_claims"] == decision.checks["unassessed_marked"] == 1
    adopted = decision.candidate
    assert [mark["code"] for mark in adopted.checked[0].marks] == ["unassessed"]
    assert not adopted.checked[0].confirmed and adopted.checked[0].published
    assert adopted.summary == "一覧から詳細画面を開けます。" and adopted.summary_warning == "監査なし"
    assert {row.request_id: row.status for row in adopted.audit.request_reviews} == {"Q1": "partial"}


def test_verbatim_kept_claim_stays_confirmed_after_adoption() -> None:
    from rag_engine.generation.refinement_round import adoption_check

    current = _round([_claim(OPEN, "run:open", OPEN)])
    candidate = _round([_claim(OPEN, "run:open", OPEN), _claim(SAVE, "run:save", SAVE)], timing=NONE_TIMING)
    decision = adoption_check(candidate, current, {})
    assert decision.adopted
    kept, added = decision.candidate.checked
    assert kept.confirmed and not kept.marks
    assert [mark["code"] for mark in added.marks] == ["unassessed"]


def test_more_confirmed_claims_do_not_replace_a_lost_previous_anchor() -> None:
    from rag_engine.generation.refinement_round import adoption_check

    current = _round([_claim(OPEN, "run:open", OPEN)])
    candidate = _round([_claim(SAVE, "run:save", SAVE), _claim(NOISE, "run:noise", NOISE)], timing=NONE_TIMING)
    decision = adoption_check(candidate, current, {})
    assert (decision.adopted, decision.reason) == (False, "previous_claim_lost")
    assert decision.checks["previous_confirmed_claims_lost"] == 1
    assert not candidate.checked[0].marks, "不採用の候補には印を足さない"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("dropped", "citation_errors_increased"),
        ("plan", "plan_errors_increased"),
        ("range", "citation_range_errors_increased"),
    ],
    ids=["dropped", "plan", "range"],
)
def test_new_deterministic_errors_block_adoption(change: str, reason: str) -> None:
    from rag_engine.generation.refinement_round import adoption_check

    current = _round([_claim(OPEN, "run:open", OPEN)])
    entries = [_claim(OPEN, "run:open", OPEN), _claim(SAVE, "run:save", SAVE)]
    changes: dict[str, Any] = {"timing": dict(NONE_TIMING)}
    if change == "dropped":
        changes["dropped"] = [{"index": 2, "reason": "合成の不一致"}]
    elif change == "plan":
        changes["timing"]["request_plan_errors"] = ["合成の計画の不整合"]
    else:
        quote_only = _claim(NOISE, "run:noise", NOISE)
        quote_only.quote_only = True
        quote_only.citation_resolution.update(reason_code="citation_range_out_of_bounds", requested_evidence_id="E3")
        entries.append(quote_only)
    candidate = _round(entries, **changes)
    decision = adoption_check(candidate, current, {})
    assert not decision.adopted
    assert reason in decision.reasons


def test_audited_candidate_is_not_marked_again() -> None:
    from rag_engine.generation.refinement_round import adoption_check

    current = _round([_claim(OPEN, "run:open", OPEN)])
    candidate = _round([_claim(OPEN, "run:open", OPEN), _claim(SAVE, "run:save", SAVE)], timing={"audit": "standard"})
    decision = adoption_check(candidate, current, {})
    assert decision.adopted and decision.checks["audit_mode"] == "audited"
    assert decision.checks["unassessed_marked"] == 0
    assert all(entry.confirmed for entry in decision.candidate.checked)


def test_none_adopts_added_guide_step_with_unassessed_mark_end_to_end() -> None:
    knowledge = RuntimeKnowledgeContext(
        original_question=QUESTION,
        expanded_question=QUESTION,
        path=Path("unused.json"),
        guide_steps=(("open", "画面を開く", ("run:open",)), ("save", "変更を保存する", ("run:save",))),
        guide_task_scope=({"step_id": "open", "required": True}, {"step_id": "save", "required": True, "depends_on": ["open"]}),
    )
    refined = payload()
    refined["summary"] = "画面を開いて保存する方法です。"
    refined["request_coverage"][0]["evidence_ids"].append("E2")
    refined["request_coverage"][0]["evidence_scope"].append({"evidence_id": "E2", "target": "対象A", "relation": "direct"})
    refined["items"].append(
        {"kind": "operation", "text": SAVE, "request_id": "Q1", "citations": [{"evidence_id": "E2", "line_start": 1, "line_end": 1}]}
    )
    parser = Parser([payload(), refined])
    result = run(parser, mode="cited_required", knowledge=knowledge)
    assert len(parser.calls) == 2
    trace = result.response.generation_trace["evidence_refinement"]
    assert trace["adopted"] is True and trace["adoption"] == "deterministic_checks_improved"
    assert trace["adoption_checks"]["audit_mode"] == "none"
    assert trace["adoption_checks"]["previous_confirmed_claims_lost"] == 0
    assert trace["adoption_checks"]["new_or_reworded_claims"] == trace["adoption_checks"]["unassessed_marked"] == 1
    assert trace["adoption_checks"]["after"]["confirmed"] > trace["adoption_checks"]["before"]["confirmed"]
    assert OPEN in result.response.answer_text and SAVE in result.response.answer_text
    assert "確かめが終わっていない" in result.response.answer_text
    adopted = next(
        row for row in result.response.generation_trace["rounds"] if row.get("remedy", {}).get("remedy") == "evidence_refinement"
    )
    assert adopted["accepted"] is True
    marks = {row["text"]: [mark["code"] for mark in row.get("marks", ())] for row in adopted["items"]}
    assert marks[OPEN] == [] and marks[SAVE] == ["unassessed"]


@pytest.mark.parametrize("adopted", [False, True])
def test_context_history_and_final_generation_evidence_have_distinct_scopes(monkeypatch: pytest.MonkeyPatch, adopted: bool) -> None:
    from rag_engine.generation import refinement_round

    # 採否の判定のstubで分岐だけを通す。実際の品質改善・意味支持の成功は表さない。
    monkeypatch.setattr(
        refinement_round,
        "adoption_check",
        lambda candidate, current, selection: refinement_round.AdoptionDecision(adopted, "synthetic", ("synthetic",), {}, candidate),
    )
    parser = Parser()
    result = run(parser)
    assert len(parser.calls) == 2
    trace = result.response.generation_trace
    refinement = trace["evidence_refinement"]
    assert refinement["adopted"] is adopted
    assert refinement["context_scope"] == "original_retrieval_history"
    assert refinement["final_generation_evidence_scope"] == ("selected_spans" if adopted else "initial_spans")
    assert {row.text for row in result.context.records} == {OPEN, SAVE, NOISE}
    selected = trace["selected_evidence"]
    assert len(selected) == (1 if adopted else 3)
    assert all(NOISE not in fact["quote"] for fact in result.response.evidence_facts)
    assert refinement["semantic_verification"] == "not_performed_by_selection"
    assert NOISE not in parser.calls[1]["prompt"]
