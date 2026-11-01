"""必須子本文の到達を生成の parse_text 境界で検証する。実サービス呼出0。"""

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import OpenAI
from test_bound_audit_transport import response_bytes

from rag_engine.adapters import model_api
from rag_engine.config import get_settings
from rag_engine.generation import answering, grounded
from rag_engine.generation.answer_models import AnswerContext, AnswerRecord
from rag_engine.generation.answering import _grounded_prompt, _grounded_spans
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.knowledge.runtime_knowledge import RuntimeKnowledgeContext
from rag_engine.models.llm import GroundedDraft
from rag_engine.retrieval.context_builder import ContextChildEvidence, ContextParentEvidence
from rag_engine.retrieval.evidence_selection import evidence_spans
from rag_engine.retrieval.source_context import source_context_text

QUESTION = "設定内容を変更して確定する方法を教えてください。"
OPERATIONS = ("一覧の対象行を選び、詳細画面で項目Aを変更して確定します。", "別の一覧で対象行を選び、項目Bを変更して保存します。")


def pair(index: int, *, image: bool = False) -> tuple[AnswerRecord, AnswerRecord]:
    operation = OPERATIONS[index]
    prefix = "概要の見出し\n" + "表示例と補足説明。\n" * 400
    parent = AnswerRecord(
        id=f"parent-{index}",
        engine="docling",
        engine_label="Docling",
        page=1,
        page_end=2,
        seq_no=1,
        category="Chunk",
        text=prefix + operation,
        source=f"document-{index}.pdf",
        source_run_id=f"run-{index}",
        chunk_id=f"parent-{index}",
        chunk_uid=f"run-{index}:parent-{index}",
        chunk_level="parent",
        metadata={
            "section_path": ["合成設定"],
            "document_id": f"doc-{index}",
            "layout": {"native_text_ranges": [{"record_id": f"native-{index}", "page": 1, "start": 0, "end": len(prefix)}]},
        },
    )
    child = replace(
        parent,
        id=f"child-{index}",
        chunk_id=f"child-{index}",
        chunk_uid=f"run-{index}:child-{index}",
        chunk_level="child",
        parent_chunk_id=parent.chunk_id,
        parent_chunk_uid=parent.chunk_uid,
        page=2,
        page_end=2,
        seq_no=2,
        text=operation,
        metadata={"section_path": ["合成設定"]},
        source_record_refs=({"record_id": f"image-{index}", "page": 2, "category": "Picture"},) if image else (),
    )
    return parent, child


def fixture(
    count: int = 1, *, image: bool = False, alias: bool = False, parent_source: bool = False
) -> tuple[AnswerContext, RuntimeKnowledgeContext, list[dict[str, Any]]]:
    parents, trees, steps, images = [], [], [], []
    for index in range(count):
        parent, child = pair(index, image=image)
        children = [ContextChildEvidence(child, "retrieved_anchor", "synthetic_hit")]
        if parent_source:
            neighbor_text = "隣接欄の参考表示です。" * 60
            parent = replace(parent, text=parent.text.replace(OPERATIONS[index], neighbor_text + "\n" + OPERATIONS[index]))
            neighbor = replace(
                child, id=f"neighbor-{index}", chunk_id=f"neighbor-{index}", chunk_uid=f"run-{index}:neighbor-{index}", text=neighbor_text
            )
            children.insert(0, ContextChildEvidence(neighbor, "neighbor_context", "synthetic_neighbor"))
        if alias:
            child = replace(child, id=f"alias-child-{index}", chunk_id=f"alias-child-{index}", chunk_uid=f"run-{index}:alias-child-{index}")
            children.append(ContextChildEvidence(child, "neighbor_context", "synthetic_alias"))
        parents.append(parent)
        trees.append(ContextParentEvidence(parent, "synthesis_parent", "parent_of_retrieved_child", tuple(children)))
        steps.append((f"step-{index}", "必要な操作", (parent.chunk_uid if parent_source else child.chunk_uid,)))
        if image:
            images.append(
                {"source_run_id": parent.source_run_id, "source": parent.source, "chunk_id": parent.chunk_id, "image_id": f"image-{index}"}
            )
    context = AnswerContext(records=parents, text="", evidence_tree=tuple(trees))
    knowledge = RuntimeKnowledgeContext(
        original_question=QUESTION, expanded_question=QUESTION, path=Path("unused.json"), guide_steps=tuple(steps)
    )
    return context, knowledge, images


def capture_generation(
    context: AnswerContext, knowledge: RuntimeKnowledgeContext, images: list[dict[str, Any]], budget: int
) -> tuple[str, list[dict[str, Any]]]:
    spans = _grounded_spans(QUESTION, context, images, knowledge, budget=budget)
    prompt = _grounded_prompt(
        QUESTION, spans, context, images, None, None, knowledge, None, None, template="{{question}}\n{{answer_context}}"
    )
    captured: list[str] = []

    def parse(system: str, actual_prompt: str, settings: Any, schema: Any, **kwargs: Any) -> GroundedDraft:
        assert schema is generation_draft_wire_model()
        captured.append(actual_prompt)
        return GroundedDraft(items=[], summary="", confidence="low")

    grounded.run_round(
        QUESTION,
        spans,
        get_settings(environ={}, dotenv_path=None),
        prompt=prompt,
        image_paths=(),
        provider_id=None,
        parse_text=parse,
        parse_images=parse,
        audit_mode="none",
    )
    assert len(captured) == 1
    return captured[0], spans


def quotable_body(prompt: str) -> str:
    # 補助情報の出典IDや children.text だけの一致を成功に数えない。
    without_notes = re.sub(r"<補助情報[^\n]*>.*?</補助情報>", "", prompt, flags=re.S)
    if "BEGIN_UNTRUSTED_RETRIEVED_CONTEXT\n" in without_notes:
        without_notes = without_notes.split("BEGIN_UNTRUSTED_RETRIEVED_CONTEXT\n", 1)[1]
    without_notes = without_notes.split("END_UNTRUSTED_RETRIEVED_CONTEXT", 1)[0]
    blocks = re.split(r"^\[E\d+\][^\n]*\n", without_notes, flags=re.M)[1:]
    return "\n".join(
        re.sub(r"^\d+\| ", "", re.split(r"^=== 機能", block, maxsplit=1, flags=re.M)[0], flags=re.M).strip() for block in blocks
    )


@pytest.mark.parametrize("image,alias", [(False, False), (False, True), (True, False)])
def test_required_tail_child_operation_reaches_actual_generation_input(image: bool, alias: bool) -> None:
    context, knowledge, images = fixture(image=image, alias=alias)
    prompt, spans = capture_generation(context, knowledge, images, budget=700)
    assert OPERATIONS[0] in quotable_body(prompt)
    assert (
        sum(len(span["text"]) + len(span.get("answer_context", "")) + len(source_context_text(span["source_context"])) for span in spans)
        <= 700
    )
    for span in spans:
        if OPERATIONS[0] in span["text"]:
            assert grounded.numbered_text(span["text"]) in prompt
            assert span["page"] == 2 and span["page_end"] == 2


def test_multiple_required_child_bodies_share_budget_before_optional_parent_prefixes() -> None:
    context, knowledge, images = fixture(count=2)
    prompt, spans = capture_generation(context, knowledge, images, budget=700)
    assert all(operation in quotable_body(prompt) for operation in OPERATIONS)
    assert (
        sum(len(span["text"]) + len(span.get("answer_context", "")) + len(source_context_text(span["source_context"])) for span in spans)
        <= 700
    )


@pytest.mark.parametrize(
    "streamed,parent_source,whole_path",
    [
        pytest.param(False, False, False, id="child-nonstream"),
        pytest.param(True, False, False, id="child-stream"),
        pytest.param(False, True, False, id="parent-nonstream"),
        pytest.param(False, False, True, id="whole-budget-nonstream"),
    ],
)
def test_sdk_sends_required_child_bodies_in_quotable_context(
    streamed: bool, parent_source: bool, whole_path: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, knowledge, images = fixture(count=2, parent_source=parent_source)
    expected_operations: tuple[str, ...] = OPERATIONS
    budget = 700
    if whole_path:
        parent, child = pair(0)
        parent = replace(
            parent,
            id="p",
            chunk_id="p",
            chunk_uid="v:p",
            source="a.pdf",
            source_run_id="r",
            text="見出しの補足。" * 15 + OPERATIONS[0],
            metadata={},
        )
        child = replace(
            child,
            id="c",
            chunk_id="c",
            chunk_uid="v:c",
            source=parent.source,
            source_run_id=parent.source_run_id,
            parent_chunk_id="p",
            parent_chunk_uid="v:p",
        )
        tree = ContextParentEvidence(
            parent, "synthesis_parent", "parent_of_retrieved_child", (ContextChildEvidence(child, "retrieved_anchor", "synthetic_hit"),)
        )
        context = AnswerContext(records=[parent], text="", evidence_tree=(tree,))
        knowledge = replace(knowledge, guide_steps=(("step-0", "必要な操作", (child.chunk_uid,)),))
        expected_operations = (OPERATIONS[0],)
        budget = 300
    observed_anchors: list[list[str]] = []
    original_selection = evidence_spans

    def observe_selection(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        observed_anchors.append([child.chunk_uid for child in kwargs.get("anchors", ())])
        return original_selection(*args, **kwargs)

    monkeypatch.setattr(answering, "evidence_spans", observe_selection)
    spans = _grounded_spans(QUESTION, context, images, knowledge, budget=budget, whole_records=whole_path)
    prompt = _grounded_prompt(
        QUESTION, spans, context, images, None, None, knowledge, None, None, template="{{question}}\n{{answer_context}}"
    )
    sent: list[dict[str, Any]] = []
    schema = generation_draft_wire_model()
    raw = json.dumps(schema.model_validate({"items": [], "summary": "", "confidence": "low"}).model_dump())

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
    monkeypatch.setattr(model_api, "_openai_client_for_provider", lambda *_args: client)
    monkeypatch.setattr(model_api, "_stall_detection_enabled", lambda _settings: streamed)
    settings = get_settings(
        environ={
            "ENTERPRISE_AI_DEFAULT_MODEL": "synthetic",
            "ENTERPRISE_AI_ENDPOINT": "https://mock.invalid/v1",
            "ENTERPRISE_AI_API_KEY": "mock-only",
        },
        dotenv_path=None,
        llm_stall_resends=0,
    )
    try:
        result = model_api._parse_text_once(
            "## Step 1: 資料の手順を回答する", prompt, settings, schema, settings.llm_providers[settings.default_answer_llm]
        )
    finally:
        client.close()
    assert isinstance(result, schema)
    assert len(sent) == 1
    assert sent[0].get("stream", False) is streamed
    user_input = next(row["content"] for row in sent[0]["input"] if row["role"] == "user")
    assert user_input == prompt
    assert all(operation in quotable_body(user_input) for operation in expected_operations)
    assert sent[0]["text"]["format"]["strict"] is True
    assert (
        sum(len(row["text"]) + len(row.get("answer_context", "")) + len(source_context_text(row["source_context"])) for row in spans)
        <= budget
    )
    if parent_source:
        assert observed_anchors == [["run-0:child-0", "run-1:child-1"]]
        assert not any("隣接欄の参考表示" in row["text"] for row in spans if row.get("pinned"))


def test_request_body_reader_handles_short_and_multiline_units_without_metadata_false_positive() -> None:
    sample = (
        "guide mentions method outside source\nBEGIN_UNTRUSTED_RETRIEVED_CONTEXT\n"
        "=== 機能 F1: 合成文書 ===\n[E1] p.1\n<補助情報（quote 不可）>\nmetadata-only-operation\n</補助情報>\n"
        "短い操作本文。\n\n[E2] p.2\n1| 長い操作の前半。\n同じ引用単位の後半。\n2| 次の操作。\n"
        "END_UNTRUSTED_RETRIEVED_CONTEXT\n"
    )
    body = quotable_body(sample)
    assert "短い操作本文。" in body
    assert "長い操作の前半。\n同じ引用単位の後半。\n次の操作。" in body
    assert "metadata-only-operation" not in body and "guide mentions" not in body
    assert "=== 機能" not in body


def test_unaffordable_required_bodies_report_incomplete_preparation_and_are_not_claimed_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    context, knowledge, images = fixture(count=2)
    stages: list[dict[str, Any]] = []
    monkeypatch.setattr(answering, "record_retrieval_stage", stages.append)
    prompt, spans = capture_generation(context, knowledge, images, budget=20)
    assert spans == []
    assert not any(operation in quotable_body(prompt) for operation in OPERATIONS)
    stage = next(row for row in stages if row["stage"] == "generation_evidence_preparation")
    assert stage["input_stage"] == "prepared_for_generation"
    assert len(stage["groups"]) == 2
    for row in stage["groups"]:
        assert row["body_status"] in {"missing", "partial"}
        assert row["located_child_ids"]
        assert row["selected_chars"] < row["required_chars"]
        assert row["required_chars"] > 0
        assert "sent" not in row
