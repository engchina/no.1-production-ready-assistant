"""回答本文を変更せず、明示された引用の整数範囲だけを修復する。"""

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field, PrivateAttr, StrictInt

from rag_engine.generation.citation_wire import LINE_END_DESCRIPTION, LINE_START_DESCRIPTION, bind_wire_range
from rag_engine.models.llm import GroundedDraft, _StrictModel

CITATION_RANGE_REPAIR_SYSTEM_PROMPT = (
    "## Step 1: 引用の範囲を修復する\n元の草稿と修復対象を読み、対象の引用範囲を応答schemaで返してください。"
)


class CitationRangeUpdate(_StrictModel):
    item_index: StrictInt
    citation_index: StrictInt
    evidence_id: str
    line_start: StrictInt | None = Field(description=LINE_START_DESCRIPTION)
    line_end: StrictInt | None = Field(description=LINE_END_DESCRIPTION)


class CitationRangeRepair(_StrictModel):
    _provider_output_text: str | None = PrivateAttr(default=None)
    ranges: list[CitationRangeUpdate]


def apply_range_repair(
    draft: GroundedDraft, response: CitationRangeRepair, targets: Sequence[Mapping[str, Any]]
) -> tuple[GroundedDraft, dict[str, Any]]:
    """実際に指定した根拠の範囲だけ採用し、失敗した引用・本文・計画は元のまま残す。"""
    repaired = draft.model_copy(deep=True)
    expected = {(row["item_index"], row["citation_index"]): row for row in targets}
    counts = Counter((row.item_index, row.citation_index) for row in response.ranges)
    adopted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in response.ranges:
        key = row.item_index, row.citation_index
        target = expected.get(key)
        reason = ""
        if target is None:
            reason = "unknown_target"
        elif counts[key] != 1:
            reason = "duplicate_target"
        elif row.evidence_id != target["evidence_id"]:
            reason = "evidence_id_changed"
        else:
            binding = bind_wire_range(row.line_start, row.line_end, unit_count=target["unit_count"], numbered=target["numbered"])
            reason = binding.reason_code or ""
            # 番号なしの根拠は全文の指定（null と、単位 1 つへの (1,1)）だけを受ける。判定は bind_wire_range にそろえる。
            if not target["numbered"] and not binding.whole_text and (row.line_start is not None or row.line_end is not None):
                reason = "citation_range_invalid"
        detail = {"item_index": row.item_index, "citation_index": row.citation_index}
        if reason:
            rejected.append({**detail, "reason_code": reason})
            continue
        citation = repaired.items[row.item_index].citations[row.citation_index]
        # 私有の抽出証明は再利用せず、通常の原文・提示版の検証へ戻す。
        citation.lines = binding.lines
        citation._wire_range = row.line_start, row.line_end
        citation._extraction = {}
        adopted.append(detail)
    missing = [{"item_index": key[0], "citation_index": key[1], "reason_code": "missing_target"} for key in expected if key not in counts]
    return repaired, {
        "status": "completed" if len(adopted) == len(expected) and not rejected else "partial",
        "scope": "citation_range_only",
        "adopted": adopted,
        "rejected": [*rejected, *missing],
    }
