"""見出しの階層の推定(OCI Enterprise AI。任意。#2277)。

LLM は決定論のスタブ。record と見出しは合成の内容。
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from app.config import Settings
from app.rag.heading_levels import (
    HEADING_LEVEL_ARTIFACT,
    LLM_HEADING_SOURCE,
    HeadingLevelOptions,
    apply_heading_levels,
    parse_heading_levels,
    plan_heading_level_inference,
)
from app.rag.ingestion import IngestionPipeline
from app.rag.ingestion_prompts import HEADING_LEVEL_PROMPT
from app.rag.variant_keys import compute_extraction_recipe_id
from app.schemas.extraction import DocumentElement, StructuredExtraction

# 番号の無い見出し(番号の体系では深さを決められない)。
HEADINGS = ["はじめに", "背景", "目的", "まとめ"]
EXPECTED_LEVELS = [1, 2, 2, 1]


def _records(
    headings: list[str], *, engine: str = "docling", raw: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, heading in enumerate(headings):
        page = index + 1
        records.append(
            {
                "id": f"{engine}-p{page}-1",
                "engine": engine,
                "page": page,
                "seq_no": 1,
                "bbox": [100.0, 300.0, 600.0, 340.0],
                "coord_system": "image_top_left",
                "page_width": 1000.0,
                "page_height": 1400.0,
                "category": "Section-header",
                "text": heading,
                "raw": dict(raw or {}),
            }
        )
        records.append(
            {
                "id": f"{engine}-p{page}-2",
                "engine": engine,
                "page": page,
                "seq_no": 2,
                "bbox": [100.0, 400.0, 900.0, 600.0],
                "coord_system": "image_top_left",
                "page_width": 1000.0,
                "page_height": 1400.0,
                "category": "Text",
                "text": f"本文 {index + 1}。",
                "raw": {},
            }
        )
    return records


def _extraction(records: list[dict[str, Any]]) -> StructuredExtraction:
    return StructuredExtraction(
        elements=[
            DocumentElement(
                kind="title" if record["category"] == "Section-header" else "text",
                text=record["text"],
                element_id=record["id"],
                page_number=record["page"],
            )
            for record in records
        ],
        parser_artifacts={"layout_records": {"version": 1, "pages": [], "records": records}},
    )


def _response(levels: list[int]) -> str:
    return json.dumps({"levels": [{"i": i, "level": level} for i, level in enumerate(levels)]})


def test_plan_sends_only_heading_texts_in_reading_order() -> None:
    plan = plan_heading_level_inference(_extraction(_records(HEADINGS)), HeadingLevelOptions())

    assert plan.planned
    assert plan.record_ids == tuple(f"docling-p{page}-1" for page in range(1, 5))
    assert json.loads(plan.payload) == [{"i": i, "text": text} for i, text in enumerate(HEADINGS)]
    assert "本文" not in plan.payload


@pytest.mark.parametrize(
    ("extraction", "reason"),
    [
        (
            _extraction(_records(["1. 概要", "1.1 範囲", "2. 手順", "2.1 準備"])),
            "numbering_resolves",
        ),
        (_extraction(_records(HEADINGS[:2])), "too_few_headings"),
        (
            _extraction(
                _records(HEADINGS, raw={"heading_source": "pdf_outline", "heading_level": 1})
            ),
            "outline_levels",
        ),
        (_extraction(_records(HEADINGS, engine="other_engine")), "engine_not_supported"),
        (StructuredExtraction(elements=[DocumentElement(text="本文")]), "no_layout_records"),
    ],
    ids=["numbered", "too-few", "outline", "other-engine", "no-layout"],
)
def test_plan_skips_documents_that_do_not_need_inference(
    extraction: StructuredExtraction, reason: str
) -> None:
    plan = plan_heading_level_inference(extraction, HeadingLevelOptions())

    assert not plan.planned
    assert plan.reason == reason


def test_plan_thresholds_come_from_settings() -> None:
    options = HeadingLevelOptions.from_settings(
        Settings(rag_heading_level_inference_min_headings=5)
    )

    plan = plan_heading_level_inference(_extraction(_records(HEADINGS)), options)

    assert plan.reason == "too_few_headings"


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps({"levels": [{"i": 0, "level": 1}, {"i": 1, "level": 2}, {"i": 2, "level": 2}]}),
        json.dumps({"levels": [{"i": i, "level": 1, "text": "x"} for i in range(4)]}),
        _response([1, 2, 7, 1]),
        json.dumps({"levels": [{"i": 0, "level": 1}] * 4}),
        json.dumps({"levels": [{"i": str(i), "level": 1} for i in range(4)]}),
        json.dumps({"levels": [{"i": i, "level": 1} for i in range(4)], "note": "x"}),
    ],
    ids=[
        "not-json",
        "missing",
        "extra-key",
        "out-of-range",
        "duplicate",
        "string-index",
        "extra-top",
    ],
)
def test_parse_rejects_any_mismatch(raw: str) -> None:
    assert parse_heading_levels(raw, 4) is None


def test_parse_accepts_wrapped_json_in_any_order() -> None:
    raw = (
        "```json\n"
        + json.dumps(
            {
                "levels": [
                    {"i": 3, "level": 1},
                    {"i": 0, "level": 1},
                    {"i": 2, "level": 2},
                    {"i": 1, "level": 2},
                ]
            }
        )
        + "\n```"
    )

    assert parse_heading_levels(raw, 4) == EXPECTED_LEVELS


def test_apply_sets_levels_and_rebuilds_section_paths() -> None:
    extraction = _extraction(_records(HEADINGS))
    plan = plan_heading_level_inference(extraction, HeadingLevelOptions())

    result = apply_heading_levels(extraction, plan, EXPECTED_LEVELS)

    paths = {element.text: element.section_path for element in result.elements}
    assert paths["本文 2。"] == ["はじめに", "背景"]
    assert paths["目的"] == ["はじめに", "目的"]
    assert paths["まとめ"] == ["まとめ"]
    layout = cast(dict[str, Any], result.parser_artifacts["layout_records"])
    heading = next(r for r in layout["records"] if r["id"] == "docling-p2-1")
    assert heading["raw"] == {"heading_level": 2, "heading_source": LLM_HEADING_SOURCE}
    report = cast(dict[str, Any], result.parser_artifacts[HEADING_LEVEL_ARTIFACT])
    assert report["status"] == "applied"


class OciGenAiConfigError(Exception):
    """再試行しない種類(認証の設定の誤り)の失敗。待たずに失敗にする。"""


class _FakeVlm:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def generate(self, prompt: str, context: str, **kwargs: Any) -> str:
        self.calls.append((prompt, context, kwargs))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


async def _attach(settings: Settings, vlm: _FakeVlm) -> StructuredExtraction:
    pipeline = IngestionPipeline(settings=settings, vlm=cast(Any, vlm), oracle=cast(Any, object()))
    attach = IngestionPipeline._attach_heading_levels
    return await attach(pipeline, "trace", _extraction(_records(HEADINGS)))


def _enabled() -> Settings:
    return Settings(rag_heading_level_inference_enabled=True)


async def test_ingestion_applies_inferred_levels_with_structured_output() -> None:
    vlm = _FakeVlm(_response(EXPECTED_LEVELS))

    result = await _attach(_enabled(), vlm)

    assert len(vlm.calls) == 1
    prompt, context, kwargs = vlm.calls[0]
    assert prompt == HEADING_LEVEL_PROMPT
    assert json.loads(context)[1] == {"i": 1, "text": "背景"}
    assert kwargs["response_schema"]["required"] == ["levels"]
    paths = {element.text: element.section_path for element in result.elements}
    assert paths["本文 2。"] == ["はじめに", "背景"]


@pytest.mark.parametrize(
    ("response", "status"),
    [
        (_response([1, 2, 2]), "invalid_response"),
        ("見出しの階層は次のとおりです。", "invalid_response"),
        (OciGenAiConfigError("認証の設定がありません"), "failed"),
    ],
    ids=["missing-heading", "not-json", "call-failed"],
)
async def test_ingestion_keeps_levels_when_response_is_unusable(
    response: str | Exception, status: str
) -> None:
    result = await _attach(_enabled(), _FakeVlm(response))

    layout = cast(dict[str, Any], result.parser_artifacts["layout_records"])
    assert all("heading_level" not in record["raw"] for record in layout["records"])
    original = _extraction(_records(HEADINGS))
    assert [e.section_path for e in result.elements] == [e.section_path for e in original.elements]
    report = cast(dict[str, Any], result.parser_artifacts[HEADING_LEVEL_ARTIFACT])
    assert report["status"] == status


async def test_ingestion_does_not_call_llm_when_disabled() -> None:
    vlm = _FakeVlm(_response(EXPECTED_LEVELS))

    result = await _attach(Settings(), vlm)

    assert vlm.calls == []
    assert HEADING_LEVEL_ARTIFACT not in result.parser_artifacts


def test_extraction_recipe_id_changes_only_when_enabled() -> None:
    default = compute_extraction_recipe_id("sha", Settings())
    disabled = compute_extraction_recipe_id(
        "sha", Settings(rag_heading_level_inference_enabled=False)
    )
    enabled = compute_extraction_recipe_id(
        "sha", Settings(rag_heading_level_inference_enabled=True)
    )

    assert default == disabled
    assert enabled != default


async def test_heading_levels_use_the_saved_prompt() -> None:
    class Oracle:
        async def answer_prompt_overrides(self) -> dict[str, str]:
            return {"heading_levels": "合成の独自階層指示"}

    vlm = _FakeVlm(_response(EXPECTED_LEVELS))
    pipeline = IngestionPipeline(
        settings=_enabled(), vlm=cast(Any, vlm), oracle=cast(Any, Oracle())
    )
    await pipeline._attach_heading_levels("trace", _extraction(_records(HEADINGS)))
    assert vlm.calls[0][0] == "合成の独自階層指示"
    assert vlm.calls[0][2]["response_schema"]["required"] == ["levels"]
