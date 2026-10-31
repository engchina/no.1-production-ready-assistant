"""項目抽出の入力の予算・分割・統合・失敗の分類・再開(#2720)。

本文はすべて合成の内容(業務の内容を持たない)。モデルは決定論のスタブで、実時間で待たない。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pr_backend_core.bounded_calls import BoundedCallAborted, BoundedCallFailed, ErrorKind
from pr_system_settings.model import estimate_input_tokens

from app.clients import ai_calls
from app.config import Settings
from app.rag import extraction_field_adapter as fields_mod
from app.rag import ingestion as ingestion_module
from app.rag.extraction_field_adapter import (
    FIELD_EXTRACTION_OUTCOME_ARTIFACT_KEY,
    FIELD_EXTRACTION_RESPONSE_SCHEMA,
    FieldDefinition,
    FieldExtractionFailed,
    FieldExtractionResponseError,
    UnitExtraction,
    extract_fields_from_extraction,
    parse_extraction_candidates,
)
from app.rag.field_extraction_units import (
    MIN_UNIT_TEXT_TOKENS,
    FieldExtractionUnit,
    FieldValueCandidate,
    UnitCandidates,
    field_extraction_text_budget,
    merge_field_candidates,
    plan_field_extraction_units,
    split_field_extraction_unit,
)
from app.rag.ingestion import IngestionPipeline, IngestionUserError
from app.schemas.extraction import DocumentElement, ExtractionField, StructuredExtraction

_DEFS = [
    FieldDefinition(name="試験番号", description="合成の試験の番号", value_type="string"),
    FieldDefinition(name="試験日", description="合成の試験の日付", value_type="date"),
]


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(fields_mod.FIELD_SCHEMA_FILE_ENV, str(tmp_path / "extraction-fields.json"))

    async def fake_sleep(seconds: float) -> None:
        _ = seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(ai_calls, "_sleep", fake_sleep)
    monkeypatch.setattr(ai_calls, "_random", lambda: 0.5)


def _paragraphs(count: int, *, chars: int = 120, page_every: int = 5) -> StructuredExtraction:
    """合成の段落の文書(見出し 1 つと段落 count 個)。"""
    elements = [
        DocumentElement(
            kind="title", text="# 第1章 合成の章", order=0, element_id="h0", page_number=1
        )
    ]
    for index in range(count):
        body = f"段落{index:04d}。" + "あ" * chars
        elements.append(
            DocumentElement(
                kind="text",
                text=body,
                order=index + 1,
                element_id=f"p{index:04d}",
                page_number=index // page_every + 1,
            )
        )
    return StructuredExtraction(elements=elements)


def _body_elements(extraction: StructuredExtraction) -> list[DocumentElement]:
    return [element for element in extraction.elements if element.text.strip()]


def _outcome(extraction: StructuredExtraction) -> dict[str, Any]:
    """刻んだ実行結果(dict)。"""
    outcome = extraction.parser_artifacts[FIELD_EXTRACTION_OUTCOME_ARTIFACT_KEY]
    assert isinstance(outcome, dict)
    return dict(outcome)


# --- 予算 -------------------------------------------------------------------------------------


def test_text_budget_subtracts_instructions_from_share_of_model_limit() -> None:
    budget = field_extraction_text_budget(
        max_input_tokens=100_000, percent=40, fixed_texts=("指示" * 100, "x" * 400)
    )
    # 40% の 40000 から、指示(200)・ASCII(100)・枠の余地(512)を引く。
    assert budget == 40_000 - 200 - 100 - 512


# --- 分割 -------------------------------------------------------------------------------------


def test_small_document_is_one_unit_without_header() -> None:
    extraction = _paragraphs(3)
    units = plan_field_extraction_units(extraction, text_budget_tokens=10_000)
    assert len(units) == 1
    assert units[0].text == "\n".join(element.text for element in _body_elements(extraction))
    assert units[0].overlap == ()


def test_long_document_units_stay_within_budget_and_cover_every_element_once() -> None:
    extraction = _paragraphs(200)
    budget = 2_000
    units = plan_field_extraction_units(extraction, text_budget_tokens=budget)

    assert len(units) > 5
    assert all(unit.tokens <= budget for unit in units)
    assert all(unit.total == len(units) for unit in units)
    covered = [element_id for unit in units for element_id in unit.element_ids]
    assert covered == [element.element_id for element in _body_elements(extraction)]
    # 2 つ目からの単位は頭書き(位置と見出し)と、前の単位の末尾の重なりを持つ。
    second = units[1]
    assert second.text.startswith("[文書の一部 2/")
    assert "見出し: 第1章 合成の章" in second.text.splitlines()[0]
    assert second.overlap and second.overlap[-1].element_id == units[0].atoms[-1].element_id


def test_large_table_is_split_with_header_repeated() -> None:
    header = "| 列A | 列B |\n| --- | --- |"
    rows = "\n".join(f"| 行{index:04d} | {'い' * 40} |" for index in range(300))
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(
                kind="table", text=f"{header}\n{rows}", order=0, element_id="t0", page_number=2
            )
        ]
    )
    budget = 1_500
    units = plan_field_extraction_units(extraction, text_budget_tokens=budget)

    assert len(units) > 1
    for unit in units:
        assert unit.tokens <= budget
        for atom in unit.atoms:
            assert atom.text.startswith(header)
            assert atom.element_id == "t0" and atom.page_number == 2
    body_rows = [
        line for unit in units for atom in unit.atoms for line in atom.text.splitlines()[2:]
    ]
    assert body_rows == rows.splitlines()


def test_long_line_without_breaks_is_split_without_losing_text() -> None:
    text = "".join(f"文{index:05d}" for index in range(3_000))  # 改行も句点も無い長い 1 行
    extraction = StructuredExtraction(
        elements=[DocumentElement(kind="text", text=text, order=0, element_id="e0")]
    )
    budget = 1_000
    units = plan_field_extraction_units(extraction, text_budget_tokens=budget)

    assert len(units) > 1
    assert all(unit.tokens <= budget for unit in units)
    assert "".join(atom.text for unit in units for atom in unit.atoms) == text


def test_value_across_unit_boundary_is_readable_in_next_unit() -> None:
    """項目名と値が別の要素で単位の境界をまたいでも、次の単位で両方を読める(重なり)。"""
    elements = [
        DocumentElement(
            kind="text", text="前置き" + "う" * 300, order=index, element_id=f"a{index}"
        )
        for index in range(5)
    ]
    elements.append(DocumentElement(kind="text", text="試験番号:", order=5, element_id="label"))
    elements.append(DocumentElement(kind="text", text="T-0042", order=6, element_id="value"))
    extraction = StructuredExtraction(elements=elements)
    crossings = 0
    # 予算を少しずつ変え、境界が項目名と値の間に来る場合をすべて確かめる。
    for budget in range(1_400, 1_700):
        units = plan_field_extraction_units(extraction, text_budget_tokens=budget)
        label_unit = next(unit for unit in units if "label" in unit.element_ids)
        value_unit = next(unit for unit in units if "value" in unit.element_ids)
        if label_unit is value_unit:
            continue
        crossings += 1
        assert any(atom.element_id == "label" for atom in value_unit.overlap)
        assert "試験番号:" in value_unit.text and "T-0042" in value_unit.text
        assert "label" not in value_unit.element_ids  # 重ねた範囲は新しく読む範囲に数えない
    assert crossings > 0


def test_resplit_halves_unit_and_keeps_position() -> None:
    extraction = _paragraphs(40)
    units = plan_field_extraction_units(extraction, text_budget_tokens=4_000)
    unit = units[0]
    parts = split_field_extraction_unit(unit, text_budget_tokens=2_000)

    assert len(parts) >= 2
    assert all(part.tokens <= 2_000 for part in parts)
    assert [eid for part in parts for eid in part.element_ids] == list(unit.element_ids)
    assert all(part.index == unit.index and part.total == unit.total for part in parts)
    assert [part.order for part in parts] == sorted(part.order for part in parts)
    assert split_field_extraction_unit(unit, text_budget_tokens=MIN_UNIT_TEXT_TOKENS - 1) == []


def test_derived_elements_are_not_sent() -> None:
    extraction = StructuredExtraction(
        elements=[
            DocumentElement(kind="text", text="原文の段落", order=0, element_id="e0"),
            DocumentElement(
                kind="text",
                text="試験番号: T-1",
                order=1,
                element_id="field-1",
                content_kind="field",
                metadata={"extracted_field": True},
            ),
        ]
    )
    units = plan_field_extraction_units(extraction, text_budget_tokens=1_000)
    assert units[0].text == "原文の段落"


# --- 応答の検証 --------------------------------------------------------------------------------


def test_parse_accepts_schema_object_and_keeps_raw_value() -> None:
    raw = json.dumps(
        {
            "fields": [
                {
                    "name": "試験日",
                    "value": "2026年1月2日",
                    "value_type": "date",
                    "confidence": 0.8,
                },
                {"name": "定義外", "value": "x", "value_type": "string", "confidence": 1},
            ]
        },
        ensure_ascii=False,
    )
    [candidate] = parse_extraction_candidates(raw, _DEFS)
    assert (candidate.field.value, candidate.raw_value) == ("2026-01-02", "2026年1月2日")
    assert FIELD_EXTRACTION_RESPONSE_SCHEMA["required"] == ["fields"]


@pytest.mark.parametrize(
    "raw", ['{"items": []}', '{"fields": "x"}', "抽出できません", "[broken"], ids=str
)
def test_parse_rejects_responses_that_are_not_field_lists(raw: str) -> None:
    with pytest.raises(FieldExtractionResponseError):
        parse_extraction_candidates(raw, _DEFS)


# --- 統合 -------------------------------------------------------------------------------------


def _unit(index: int, *elements: tuple[str, str, int]) -> FieldExtractionUnit:
    units = plan_field_extraction_units(
        StructuredExtraction(
            elements=[
                DocumentElement(
                    kind="text", text=text, order=order, element_id=eid, page_number=page
                )
                for order, (eid, text, page) in enumerate(elements)
            ]
        ),
        text_budget_tokens=10_000,
    )
    return FieldExtractionUnit(
        index=index, total=3, atoms=units[0].atoms, text=units[0].text, tokens=units[0].tokens
    )


def _candidate(
    name: str, value: str, raw: str | None = None, confidence: float | None = None
) -> FieldValueCandidate:
    definition = next(item for item in _DEFS if item.name == name)
    field = ExtractionField(
        name=name,
        value=fields_mod.normalize_field_value(definition.value_type, value),
        value_type=definition.value_type,
        confidence=confidence,
    )
    return FieldValueCandidate(field=field, raw_value=raw or value)


def test_merge_dedupes_typed_values_and_records_source() -> None:
    first = _unit(0, ("e1", "試験日 2026年1月2日", 1))
    second = _unit(1, ("e7", "再掲 2026/1/2", 3))
    merged = merge_field_candidates(
        [
            UnitCandidates(second, [_candidate("試験日", "2026/1/2", confidence=0.9)]),
            UnitCandidates(first, [_candidate("試験日", "2026年1月2日", confidence=0.6)]),
        ],
        [definition.name for definition in _DEFS],
    )
    [field] = merged.fields
    assert (field.value, field.source_element_id, field.page_number) == ("2026-01-02", "e1", 1)
    assert field.confidence == 0.9
    assert merged.conflict_fields == []
    assert merged.candidate_count == 2


def test_merge_conflict_prefers_more_evidence_then_document_order() -> None:
    a = _unit(0, ("e1", "試験番号 T-0001", 1))
    b = _unit(1, ("e2", "試験番号 T-0002", 2))
    c = _unit(2, ("e3", "試験番号 T-0002 (再掲)", 3))
    names = [definition.name for definition in _DEFS]
    # 根拠の多い値(2 つの要素)を採る。後の単位の値・confidence の高い値に置き換えない。
    merged = merge_field_candidates(
        [
            UnitCandidates(a, [_candidate("試験番号", "T-0001", confidence=0.99)]),
            UnitCandidates(b, [_candidate("試験番号", "T-0002", confidence=0.1)]),
            UnitCandidates(c, [_candidate("試験番号", "T-0002", confidence=0.1)]),
        ],
        names,
    )
    assert [field.value for field in merged.fields] == ["T-0002"]
    assert merged.conflict_fields == ["試験番号"]
    # 根拠の数が同じなら文書の前にある値。
    tie = merge_field_candidates(
        [
            UnitCandidates(b, [_candidate("試験番号", "T-0002")]),
            UnitCandidates(a, [_candidate("試験番号", "T-0001")]),
        ],
        names,
    )
    assert [field.value for field in tie.fields] == ["T-0001"]


def test_merge_counts_overlap_duplicate_once() -> None:
    """重なりの範囲で同じ要素から 2 回読んだ値は、根拠 1 つに数える。"""
    shared = _unit(0, ("e1", "試験番号 T-0009", 1))
    later = FieldExtractionUnit(index=1, total=3, atoms=(), overlap=shared.atoms, text="")
    other = _unit(2, ("e5", "試験番号 T-0010", 4))
    merged = merge_field_candidates(
        [
            UnitCandidates(shared, [_candidate("試験番号", "T-0009")]),
            UnitCandidates(later, [_candidate("試験番号", "T-0009")]),
            UnitCandidates(other, [_candidate("試験番号", "T-0010")]),
            UnitCandidates(_unit(2, ("e6", "T-0010", 5)), [_candidate("試験番号", "T-0010")]),
        ],
        [definition.name for definition in _DEFS],
    )
    assert [field.value for field in merged.fields] == ["T-0010"]


# --- 単位ごとの実行と失敗の分類 -----------------------------------------------------------------


def _failed(kind: ErrorKind, status: int | None = None) -> BoundedCallFailed:
    return BoundedCallFailed("synthetic", kind=kind, status=status)


async def test_too_large_unit_is_resplit_and_all_text_is_processed() -> None:
    extraction = _paragraphs(60)
    seen: list[FieldExtractionUnit] = []

    async def extract(unit: FieldExtractionUnit) -> UnitExtraction:
        if unit.tokens > 1_500:
            raise _failed(ErrorKind.TOO_LARGE, 400)
        seen.append(unit)
        return UnitExtraction([])

    result = await extract_fields_from_extraction(
        extraction, _DEFS, extract, text_budget_tokens=3_000
    )
    outcome = _outcome(result)
    assert outcome["status"] == "completed" and outcome["field_count"] == 0
    assert outcome["resplit_count"] >= 1
    assert [eid for unit in seen for eid in unit.element_ids] == [
        element.element_id for element in _body_elements(extraction)
    ]


@pytest.mark.parametrize(
    ("kind", "category"),
    [
        (ErrorKind.TRUNCATED, "truncated"),
        (ErrorKind.INVALID_OUTPUT, "invalid_response"),
        (ErrorKind.BAD_REQUEST, "request_rejected"),
        (ErrorKind.SAFETY, "safety"),
        (ErrorKind.RATE_LIMITED, "transient"),
        (ErrorKind.UNKNOWN, "unknown"),
    ],
    ids=lambda value: value.value if isinstance(value, ErrorKind) else str(value),
)
async def test_partial_failure_is_classified_and_not_published(
    kind: ErrorKind, category: str
) -> None:
    extraction = _paragraphs(60)

    async def extract(unit: FieldExtractionUnit) -> UnitExtraction:
        if unit.index == 1:
            raise _failed(kind, 400)
        return UnitExtraction([_candidate("試験番号", "T-1")])

    with pytest.raises(FieldExtractionFailed) as raised:
        await extract_fields_from_extraction(extraction, _DEFS, extract, text_budget_tokens=2_000)
    failure = raised.value
    assert failure.category == category and failure.kind is kind and failure.status == 400
    assert failure.run.failed == 1 and failure.run.skipped == 0
    assert failure.run.succeeded == failure.run.unit_count - 1 > 0


async def test_failure_before_any_success_stops_remaining_units() -> None:
    extraction = _paragraphs(60)
    calls: list[int] = []

    async def extract(unit: FieldExtractionUnit) -> UnitExtraction:
        calls.append(unit.index)
        raise _failed(ErrorKind.BAD_REQUEST, 400)

    with pytest.raises(FieldExtractionFailed) as raised:
        await extract_fields_from_extraction(extraction, _DEFS, extract, text_budget_tokens=2_000)
    assert calls == [0]
    assert raised.value.run.skipped == raised.value.run.unit_count - 1 > 0


async def test_aborted_call_propagates() -> None:
    async def extract(unit: FieldExtractionUnit) -> UnitExtraction:
        raise BoundedCallAborted("synthetic", kind=ErrorKind.PERMISSION)

    with pytest.raises(BoundedCallAborted):
        await extract_fields_from_extraction(
            _paragraphs(3), _DEFS, extract, text_budget_tokens=2_000
        )


# --- 取込の工程(予算・再開・文言・ログ) ------------------------------------------------------


class _MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put(self, key: str, data: bytes, content_type: str) -> str:
        _ = content_type
        self.objects[key] = data
        return key

    async def get(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise FileNotFoundError(key) from exc


class _NoKbOracle:
    async def list_document_extraction_field_sets(self, document_id: str) -> list[object]:
        _ = document_id
        return []


class _HttpError(Exception):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"returned {status}: {body}")
        self.response = SimpleNamespace(status_code=status, headers={})


class _ScriptedLlm:
    """本文に印のある単位だけ失敗させ、ほかは本文に書かれた試験番号を返す。"""

    def __init__(self, fail_marker: str | None = None, error: Exception | None = None) -> None:
        self.fail_marker = fail_marker
        self.error = error
        self.texts: list[str] = []
        self.prompts: list[str] = []
        self.options: list[dict[str, object]] = []

    async def generate(self, prompt: str, text: str, **options: object) -> str:
        self.prompts.append(prompt)
        self.texts.append(text)
        self.options.append(options)
        if self.fail_marker and self.fail_marker in text and self.error is not None:
            raise self.error
        numbers = re.findall(r"T-\d{4}", text)
        fields = [
            {"name": "試験番号", "value": number, "value_type": "string", "confidence": 0.5}
            for number in numbers[:1]
        ]
        return json.dumps({"fields": fields}, ensure_ascii=False)


def _pipeline(llm: _ScriptedLlm, storage: _MemoryStorage, **settings: Any) -> IngestionPipeline:
    return IngestionPipeline(
        vlm=llm,  # type: ignore[arg-type]
        oracle=_NoKbOracle(),  # type: ignore[arg-type]
        object_storage=storage,  # type: ignore[arg-type]
        settings=Settings(
            rag_field_extraction_enabled=True,
            rag_ai_call_final_retry_delay_seconds=0.0,
            **settings,
        ),
    )


def _long_document(marker_at: int = 30) -> StructuredExtraction:
    extraction = _paragraphs(60, chars=400)
    elements = list(extraction.elements)
    elements[marker_at] = elements[marker_at].model_copy(
        update={"text": elements[marker_at].text + " 合成の印 T-0007"}
    )
    elements[1] = elements[1].model_copy(update={"text": elements[1].text + " T-0001"})
    return StructuredExtraction(elements=elements)


@pytest.fixture
def small_model(monkeypatch: pytest.MonkeyPatch) -> int:
    """既定のテキストモデルの入力の上限を小さくする(長い合成の文書を分けさせる)。"""
    limit = 20_000
    monkeypatch.setattr(ingestion_module, "enterprise_ai_max_input_tokens", lambda *_: limit)
    return limit


async def test_ingestion_splits_by_budget_and_sends_schema(small_model: int) -> None:
    fields_mod.save_field_schema(_DEFS)
    llm = _ScriptedLlm()
    result = await _pipeline(llm, _MemoryStorage())._attach_extraction_fields(
        "trace", _long_document(), document_id="doc-a"
    )

    budget = small_model * 40 // 100
    assert len(llm.texts) > 1
    assert all(estimate_input_tokens(text) <= budget for text in llm.texts)
    assert all(
        options["response_schema"] == FIELD_EXTRACTION_RESPONSE_SCHEMA for options in llm.options
    )
    # 2 つの値の根拠は 1 つずつで、文書の前にある値を採り、競合として数える。
    assert [(field.name, field.value) for field in result.fields] == [("試験番号", "T-0001")]
    outcome = _outcome(result)
    assert outcome["unit_count"] == len(llm.texts)
    assert outcome["conflict_fields"] == ["試験番号"]


async def test_ingestion_resumes_only_failed_units(
    small_model: int, caplog: pytest.LogCaptureFixture
) -> None:
    _ = small_model
    fields_mod.save_field_schema(_DEFS)
    storage = _MemoryStorage()
    body = "synthetic provider body with secret-like text"
    failing = _ScriptedLlm("合成の印", _HttpError(400, body))
    caplog.set_level(logging.INFO)

    with pytest.raises(IngestionUserError) as raised:
        await _pipeline(failing, storage)._attach_extraction_fields(
            "trace", _long_document(), document_id="doc-a"
        )
    message = str(raised.value)
    assert "受け付けませんでした" in message and "HTTP 400" in message
    assert "部分のうち 1 部分が失敗" in message and "使い直します" in message
    # ログと文言に応答の本文・値・本文を出さない。
    logged = " ".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    for secret in (body, "T-0007", "T-0001", "合成の印", "段落"):
        assert secret not in logged and secret not in message
    failed_calls = len(failing.texts)

    resumed = _ScriptedLlm()
    result = await _pipeline(resumed, storage)._attach_extraction_fields(
        "trace", _long_document(), document_id="doc-a"
    )
    # 成功した単位は保存した結果を使い、失敗した単位だけを呼ぶ。
    assert len(resumed.texts) == 1 and "合成の印" in resumed.texts[0]
    outcome = _outcome(result)
    assert outcome["reused_unit_count"] == outcome["unit_count"] - 1 == failed_calls - 1


async def test_ingestion_resplits_on_engine_prompt_length_error(small_model: int) -> None:
    """vLLM 系の 400(prompt length exceeds max_model_len)は入力の上限の超過として分け直す。"""
    _ = small_model
    fields_mod.save_field_schema(_DEFS)
    over = _HttpError(
        400,
        '{"error":{"message":"The engine prompt length 9 exceeds the max_model_len 8.",'
        '"type":"invalid_request_error","code":400}}',
    )

    class _LimitedLlm(_ScriptedLlm):
        async def generate(self, prompt: str, text: str, **options: object) -> str:
            if estimate_input_tokens(text) > 3_000:
                self.texts.append("")
                raise over
            return await super().generate(prompt, text, **options)

    llm = _LimitedLlm()
    result = await _pipeline(llm, _MemoryStorage())._attach_extraction_fields(
        "trace", _long_document(), document_id="doc-a"
    )
    outcome = _outcome(result)
    assert outcome["resplit_count"] >= 1
    assert [field.value for field in result.fields] == ["T-0001"]


async def test_ingestion_unclassified_400_is_not_treated_as_too_large(small_model: int) -> None:
    _ = small_model
    fields_mod.save_field_schema(_DEFS)
    llm = _ScriptedLlm("段落0000", _HttpError(400, '{"error":{"message":"invalid parameter"}}'))
    with pytest.raises(IngestionUserError, match="入力の上限の超過とは判定できない"):
        await _pipeline(llm, _MemoryStorage())._attach_extraction_fields(
            "trace", _long_document(), document_id="doc-a"
        )
    # 分け直さず、最初の単位の失敗で残りを呼ばない。
    assert len(llm.texts) == 1


async def test_ingestion_stops_when_definitions_leave_no_room_for_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingestion_module, "enterprise_ai_max_input_tokens", lambda *_: 4_096)
    long_defs = [
        FieldDefinition(name=f"項目{index}", description="説" * 400, value_type="string")
        for index in range(10)
    ]
    fields_mod.save_field_schema(long_defs)
    llm = _ScriptedLlm()
    with pytest.raises(IngestionUserError, match="本文を入れる余地がありません"):
        await _pipeline(llm, _MemoryStorage())._attach_extraction_fields(
            "trace", _long_document(), document_id="doc-a"
        )
    assert llm.texts == []


async def test_field_extraction_uses_saved_prompt_and_keeps_definitions_and_schema(
    monkeypatch: pytest.MonkeyPatch, small_model: int
) -> None:
    async def overrides(self: object) -> dict[str, str]:
        return {"field_extraction": "合成の独自項目指示\n"}

    monkeypatch.setattr(_NoKbOracle, "answer_prompt_overrides", overrides, raising=False)
    fields_mod.save_field_schema(_DEFS)
    llm = _ScriptedLlm()
    await _pipeline(llm, _MemoryStorage())._attach_extraction_fields(
        "trace", _long_document(), document_id="doc-a"
    )
    assert llm.prompts
    assert all(
        prompt.startswith("合成の独自項目指示\n抽出する field 定義:") for prompt in llm.prompts
    )
    assert all(
        options["response_schema"] == FIELD_EXTRACTION_RESPONSE_SCHEMA for options in llm.options
    )
