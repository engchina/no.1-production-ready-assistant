"""生成専用の整数行範囲を、履歴互換の引用契約へ変換する。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from functools import lru_cache
from types import GenericAlias
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, StrictInt, create_model
from pydantic.json_schema import SkipJsonSchema

LINE_START_DESCRIPTION = (
    "根拠内でコードが付けた「1|」から始まる引用単位の開始番号。PDFの頁・物理行ではない。"
    "番号のない改行も同じ単位に含む。番号が表示されない根拠は null。"
)
LINE_END_DESCRIPTION = (
    "同じ根拠ブロックの引用単位の終了番号。開始番号以上かつ表示された最大番号以下。"
    "単一単位は開始と同じ値。番号なし根拠は開始・終了とも null。"
)

if TYPE_CHECKING:
    from rag_engine.models.llm import GroundedDraft


@dataclass(frozen=True)
class WireRangeBinding:
    """指定範囲の構造上の結果。原文の意味支持を表さない。"""

    lines: str
    reason_code: (
        Literal["citation_range_missing", "citation_range_invalid", "citation_range_out_of_bounds", "citation_extraction_failed"] | None
    ) = None
    whole_text: bool = False


def bind_wire_range(line_start: int | None, line_end: int | None, *, unit_count: int, numbered: bool) -> WireRangeBinding:
    """明示した整数範囲だけを読む。本文から行を推測したり、範囲を切り詰めたりしない。"""
    if unit_count < 1:
        return WireRangeBinding("", "citation_extraction_failed")
    if line_start is None and line_end is None:
        return WireRangeBinding("", "citation_range_missing") if numbered else WireRangeBinding("", whole_text=True)
    if type(line_start) is not int or type(line_end) is not int:
        return WireRangeBinding("", "citation_range_invalid")
    lines = str(line_start) if line_start == line_end else f"{line_start}-{line_end}"
    if line_start < 1 or line_end < 1 or line_start > unit_count or line_end > unit_count:
        return WireRangeBinding(lines, "citation_range_out_of_bounds")
    if line_start > line_end:
        return WireRangeBinding(lines, "citation_range_invalid")
    if not numbered and unit_count == 1:
        # 番号なしで単位が 1 つなら (1,1) は null と同じ本文を指す。経路で扱いを分けないよう、
        # ここで全文の指定へそろえる（#3778）。0・逆順・範囲外は上で拒み、切り詰めない。
        return WireRangeBinding(lines, whole_text=True)
    return WireRangeBinding(lines)


@lru_cache(maxsize=1)
def generation_draft_wire_model() -> type[BaseModel]:
    """既存の項目・草稿のFieldInfoを保ち、生成時だけ引用を整数の契約へ置き換える。"""
    from rag_engine.models.llm import GroundedCitation, GroundedDraft, GroundedItem, _StrictModel

    citation = create_model(
        "GroundedCitationWire",
        __base__=_StrictModel,
        evidence_id=(str, deepcopy(GroundedCitation.model_fields["evidence_id"])),
        line_start=(StrictInt | None, Field(description=LINE_START_DESCRIPTION)),
        line_end=(StrictInt | None, Field(description=LINE_END_DESCRIPTION)),
    )
    item_fields: dict[str, Any] = {"citations": (GenericAlias(list, citation), deepcopy(GroundedItem.model_fields["citations"]))}
    item = create_model("GroundedItemWire", __base__=GroundedItem, **item_fields)
    draft_fields: dict[str, Any] = {"items": (GenericAlias(list, item), deepcopy(GroundedDraft.model_fields["items"]))}
    return create_model("GroundedDraftWire", __base__=GroundedDraft, **draft_fields)


@lru_cache(maxsize=1)
def planned_generation_wire_model() -> type[BaseModel]:
    """根拠計画を入力で受け取る生成では、その計画を出力 schema から除く。"""
    from rag_engine.models.llm import RequestCoverage

    return create_model(
        "PlannedGroundedDraftWire",
        __base__=generation_draft_wire_model(),
        request_coverage=(SkipJsonSchema[list[RequestCoverage]], Field(default_factory=list, exclude=True)),
    )


def canonical_generation_draft(value: BaseModel) -> GroundedDraft:
    """新wireは整数の指定を私有属性へ残す。既存の草稿・履歴のlinesは変更しない。"""
    from rag_engine.models.llm import GroundedCitation, GroundedDraft

    line_limit = next((entry.max_length for entry in GroundedCitation.model_fields["lines"].metadata if hasattr(entry, "max_length")), None)
    wire = generation_draft_wire_model()
    if isinstance(value, GroundedDraft) and not isinstance(value, wire):
        return value
    if not isinstance(value, wire):
        value = wire.model_validate(value.model_dump())
    # 未指定の計画を「空の計画を明示した応答」へ変え、不要な計画の再生成を起こさない。
    raw = value.model_dump(exclude_unset=True)
    ranges: list[list[tuple[int | None, int | None]]] = []
    for item in raw.get("items", []):
        own: list[tuple[int | None, int | None]] = []
        converted = []
        for citation in item.get("citations", []):
            start, end = citation["line_start"], citation["line_end"]
            own.append((start, end))
            # 不正範囲も消さず保持する。確定・失敗は実際の根拠の行数と照合して決める。
            lines = "" if start is None or end is None else str(start) if start == end else f"{start}-{end}"
            # 巨大な不正整数で旧文字列の長さ制限に当たり、正常な本文まで失わせない。
            # 元の値は _wire_range に無損失で残し、範囲の検査はその値を使う。
            if line_limit is not None and len(lines) > line_limit:
                lines = ""
            converted.append({"evidence_id": citation["evidence_id"], "lines": lines})
        ranges.append(own)
        if "citations" in item:
            item["citations"] = converted
    draft = GroundedDraft.model_validate(raw)
    if isinstance(value, GroundedDraft):
        draft._provider_output_text = value._provider_output_text
        # 応答の検証で寄せた重複の数（#3849）。寄せた後の値から作り直すので、ここで引き継ぐ。
        draft._collapsed_duplicates = dict(value._collapsed_duplicates)
    for item, own in zip(draft.items, ranges, strict=True):
        for citation, interval in zip(item.citations, own, strict=True):
            citation._wire_range = interval
    return draft
