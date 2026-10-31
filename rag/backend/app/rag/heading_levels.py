"""見出しの階層を OCI Enterprise AI で推定する（任意。既定 OFF。#2277）。

PDF のしおり（outline）の深さを当てられず、番号の体系（``rag_engine`` の headings）でも多くの
見出しの深さを決められない文書だけを対象にする。見出しの文字列だけを読み順に compact な JSON で
LLM に渡し、各見出しの階層（1〜6）を受け取る。応答は Pydantic で厳密に確かめ（見出しの番号が
すべて 1 回ずつそろい、値が 1〜6）、1 つでも合わなければ今の階層のまま（何も変えない）。

段ごとの解析をまとめ、文書全体の整理（``pdf_segment_merge``）を当てた後の原本全体の record に
当てる。受け取った階層は record の ``raw.heading_level`` に入れ、要素の ``section_path`` を作り直す
（chunk の ``section_path`` と章節の木の両方が同じ階層を使う）。対象は要素の ``section_path`` を
record から作る解析エンジン（Docling）の結果だけ。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.rag.pdf_segment_merge import (
    DOCLING_LAYOUT_ENGINE,
    LAYOUT_ARTIFACT,
    OUTLINE_HEADING_SOURCE,
    elements_with_layout_section_paths,
)
from app.schemas.extraction import (
    DocumentElement,
    ExtractionArtifactValue,
    StructuredExtraction,
)

HEADING_LEVEL_ARTIFACT = "heading_level_inference"
LLM_HEADING_SOURCE = "llm_heading_level"
HEADING_LEVEL_MIN = 1
HEADING_LEVEL_MAX = 6
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


@dataclass(frozen=True)
class HeadingLevelOptions:
    """推定の対象にする条件（設定 ``RAG_HEADING_LEVEL_INFERENCE_*`` から作る）。"""

    min_headings: int = 3
    unresolved_ratio: float = 0.5
    max_headings: int = 300
    max_title_chars: int = 120

    @classmethod
    def from_settings(cls, settings: object) -> HeadingLevelOptions:
        default = cls()
        return cls(
            min_headings=int(
                getattr(settings, "rag_heading_level_inference_min_headings", default.min_headings)
            ),
            unresolved_ratio=float(
                getattr(
                    settings,
                    "rag_heading_level_inference_unresolved_ratio",
                    default.unresolved_ratio,
                )
            ),
            max_headings=int(
                getattr(settings, "rag_heading_level_inference_max_headings", default.max_headings)
            ),
            max_title_chars=int(
                getattr(
                    settings,
                    "rag_heading_level_inference_max_title_chars",
                    default.max_title_chars,
                )
            ),
        )


@dataclass(frozen=True)
class HeadingLevelPlan:
    """推定の計画。``record_ids`` が空なら推定しない（理由は ``reason``）。"""

    reason: str
    record_ids: tuple[str, ...] = ()
    payload: str = ""
    heading_count: int = 0
    unresolved_count: int = 0

    @property
    def planned(self) -> bool:
        return bool(self.record_ids)


class _HeadingLevel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    i: int = Field(ge=0)
    level: int = Field(ge=HEADING_LEVEL_MIN, le=HEADING_LEVEL_MAX)


class _HeadingLevels(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    levels: list[_HeadingLevel]


# LLM の応答の形(構造化出力の JSON schema)。
HEADING_LEVELS_RESPONSE_SCHEMA = _HeadingLevels.model_json_schema()


def plan_heading_level_inference(
    extraction: StructuredExtraction, options: HeadingLevelOptions
) -> HeadingLevelPlan:
    """推定するか（と LLM に渡す見出しの JSON）を決める。"""
    records = _layout_records(extraction)
    if not records:
        return HeadingLevelPlan(reason="no_layout_records")
    engines = {str(record.get("engine") or "").strip().casefold() for record in records}
    if engines != {DOCLING_LAYOUT_ENGINE}:
        return HeadingLevelPlan(reason="engine_not_supported")
    if any(_raw(record).get("heading_source") == OUTLINE_HEADING_SOURCE for record in records):
        return HeadingLevelPlan(reason="outline_levels")
    from rag_engine.chunking.headings import heading_level_candidates

    candidates = heading_level_candidates([dict(record) for record in records])
    count = len(candidates)
    unresolved = sum(1 for _, _, level in candidates if level is None)
    if count < max(1, options.min_headings):
        return HeadingLevelPlan(
            reason="too_few_headings", heading_count=count, unresolved_count=unresolved
        )
    if count > options.max_headings:
        return HeadingLevelPlan(
            reason="too_many_headings", heading_count=count, unresolved_count=unresolved
        )
    if unresolved < count * options.unresolved_ratio:
        return HeadingLevelPlan(
            reason="numbering_resolves", heading_count=count, unresolved_count=unresolved
        )
    limit = max(1, options.max_title_chars)
    payload = json.dumps(
        [{"i": index, "text": text[:limit]} for index, (_, text, _) in enumerate(candidates)],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return HeadingLevelPlan(
        reason="planned",
        record_ids=tuple(record_id for record_id, _, _ in candidates),
        payload=payload,
        heading_count=count,
        unresolved_count=unresolved,
    )


def parse_heading_levels(raw: str, count: int) -> list[int] | None:
    """LLM の応答の階層（見出しの番号の順）。形が合わなければ None。

    ``{"levels": [{"i": 番号, "level": 1〜6}, ...]}`` だけを受け付け、番号が 0〜count-1 の
    すべてを 1 回ずつ含むときだけ返す（余分な key・欠け・重複・範囲外の値は None）。
    """
    match = _JSON_OBJECT.search(str(raw or ""))
    if match is None:
        return None
    try:
        parsed = _HeadingLevels.model_validate(json.loads(match.group(0)))
    except (json.JSONDecodeError, ValidationError):
        return None
    indices = [item.i for item in parsed.levels]
    if sorted(indices) != list(range(count)):
        return None
    by_index = {item.i: item.level for item in parsed.levels}
    return [by_index[index] for index in range(count)]


def apply_heading_levels(
    extraction: StructuredExtraction, plan: HeadingLevelPlan, levels: Sequence[int]
) -> StructuredExtraction:
    """推定した階層を record に入れ、要素の ``section_path`` を作り直す。"""
    layout = extraction.parser_artifacts.get(LAYOUT_ARTIFACT)
    if not plan.planned or len(levels) != len(plan.record_ids) or not isinstance(layout, Mapping):
        return extraction
    by_id = dict(zip(plan.record_ids, levels, strict=True))
    records: list[dict[str, Any]] = []
    for record in _layout_records(extraction):
        copied = dict(record)
        level = by_id.get(str(record.get("id") or ""))
        if level is not None:
            copied["raw"] = {
                **_raw(record),
                "heading_level": level,
                "heading_source": LLM_HEADING_SOURCE,
            }
        records.append(copied)
    elements: list[DocumentElement] = []
    for element in extraction.elements:
        level = by_id.get(element.element_id or "")
        if level is None:
            elements.append(element)
            continue
        metadata = {
            **element.metadata,
            "heading_level": level,
            "heading_source": LLM_HEADING_SOURCE,
        }
        elements.append(element.model_copy(update={"metadata": metadata}))
    elements = elements_with_layout_section_paths(elements, records)
    return extraction.model_copy(
        update={
            "elements": elements,
            "parser_artifacts": {
                **extraction.parser_artifacts,
                LAYOUT_ARTIFACT: cast(ExtractionArtifactValue, {**layout, "records": records}),
                HEADING_LEVEL_ARTIFACT: _report(plan, "applied"),
            },
        }
    )


def with_heading_level_report(
    extraction: StructuredExtraction, plan: HeadingLevelPlan, status: str
) -> StructuredExtraction:
    """推定しなかった・失敗した理由を抽出結果に残す（階層は変えない）。"""
    return extraction.model_copy(
        update={
            "parser_artifacts": {
                **extraction.parser_artifacts,
                HEADING_LEVEL_ARTIFACT: _report(plan, status),
            }
        }
    )


def _report(plan: HeadingLevelPlan, status: str) -> ExtractionArtifactValue:
    return {
        "status": status,
        "reason": plan.reason,
        "headings": plan.heading_count,
        "unresolved_headings": plan.unresolved_count,
    }


def _layout_records(extraction: StructuredExtraction) -> list[Mapping[str, Any]]:
    layout = extraction.parser_artifacts.get(LAYOUT_ARTIFACT)
    if not isinstance(layout, Mapping):
        return []
    records = layout.get("records")
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, Mapping)]


def _raw(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("raw")
    return dict(raw) if isinstance(raw, Mapping) else {}
