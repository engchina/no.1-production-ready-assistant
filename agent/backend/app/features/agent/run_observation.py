"""Run の観測（評価・監査の機械が公開の MCP の `agent_get_run_observation` で読む。#3240）。

Run が保存した記録（step のツールの呼び出しと結果・成果物）だけから作る（RAG / NL2SQL へ新しく
読みに行かない）。見る人の今の権限での確かめ直し・ページング・認可は呼ぶ側（`mcp_server`）が行う。

- **根拠**（`ObservedEvidenceItem`）: RAG の根拠のツール（`rag_search` / `rag_retrieve_evidence`）の
  `evidence` と、読み取りのツール（`rag_read_source` / `rag_read_document` / `rag_read_section`）で
  読んだ chunk。同じ chunk（`document_id`・`chunk_id`）は初めて現れた位置に 1 件だけにし、その順を
  `rank` にする。本文は生成に渡した本文そのもので、後の呼び出しが同じ chunk の別の本文を
  渡したらまだ無い本文だけを改行でつなぐ（rag の評価の CLI の `evidence_citations` と同じ
  規則）。会話の添付の根拠（`attachment:`）・RAG の接続でないツールの結果は含めない。
- **ツールの呼び出し**（`ObservedToolCallItem`）: Run の順。状態・失敗の分類（`error_code`）・
  根拠の数・旧版を含めた検索か・NL2SQL の状態と行の数・RAG の根拠のツールの縮退（縮退した任意の
  補助の処理の種類と回数・安全チェックの未完了・根拠 0 件の段。#3546）だけ。引数の本体・エラーの
  文・SQL・行・列の名前・`profile_id` は持たない（OWASP API3。設計の Q5）。
- 根拠の `table_binding`（表の行・列の対応の検査が本文を読めるか。#3546）は、根拠のツールが返した
  値を写す（読み取りのツールの chunk は null）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from app.features.agent.answer_evidence import ATTACHMENT_DOCUMENT_PREFIX
from app.features.agent.answer_validation import (
    STATUS_COMPLETED,
    STATUS_SKIPPED,
    STATUS_UNVALIDATED,
)
from app.features.agent.data_scope import NL2SQL_CONNECTION_ID
from app.features.agent.rag_figures import RAG_CONNECTION_ID
from app.features.agent.rag_retrieval_state import (
    degraded_steps,
    guardrail_degraded,
    table_binding,
    zero_evidence_stage,
)
from app.features.agent.runtime import (
    AUTOMATION_APPROVAL_SKIPPED_CODE,
    MCP_APPROVAL_SKIPPED_CODE,
    RunState,
    RunStep,
    StepStatus,
)
from app.features.agent.support_task import (
    NL2SQL_QUERY,
    RAG_READ_DOCUMENT,
    RAG_READ_SECTION,
    RAG_READ_SOURCE,
    RAG_RETRIEVE_EVIDENCE,
    RAG_SEARCH,
)
from app.features.agent.tools import MCP_TOOL_SEPARATOR, mcp_base_tool_name

EVIDENCE_TOOLS = frozenset({RAG_SEARCH, RAG_RETRIEVE_EVIDENCE})
READ_TOOLS = frozenset({RAG_READ_SOURCE, RAG_READ_DOCUMENT, RAG_READ_SECTION})
NL2SQL_GET_JOB = "nl2sql_get_job"
NL2SQL_JOB_TOOLS = frozenset({NL2SQL_QUERY, NL2SQL_GET_JOB})
# NL2SQL の `status` の語彙（契約 nl2sql-tools.json の nl2sql_query / nl2sql_get_job）。
NL2SQL_JOB_STATUSES = frozenset(
    {"pending", "running", "done", "error", "needs_clarification", "unanswerable"}
)
# 1 つの観測に出すツールの呼び出しの上限（数は `tool_call_total`）。
MAX_OBSERVED_TOOL_CALLS = 200
# 根拠の場所の項目（RAG の `EvidenceLocator` と同じ名前。`bbox` は出さない）。
LOCATOR_FIELDS = (
    "section_path",
    "page_start",
    "page_end",
    "page_label_start",
    "page_label_end",
    "sheet_name",
    "row_basis",
    "table_index",
    "row_start",
    "row_end",
    "source_line_start",
    "source_line_end",
    "cell_range",
    "element_locator",
    "table_row",
    "table_cells",
)
# 行の番号の基準（RAG の `EvidenceLocator.row_basis`）。知らない値は写さない。parsed_table は解析が
# 組み立てた表の格子の行（原本の座標ではない。#3429）。
ROW_BASES = frozenset({"sheet", "csv", "word_table", "parsed_table"})
# 失敗の分類として出してよい形（外部の MCP の結果の任意の文を分類として写さない）。
_ERROR_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_OTHER_ERROR_CODE = "tool_error"
_RAG_PREFIX = f"{RAG_CONNECTION_ID}{MCP_TOOL_SEPARATOR}"
_NL2SQL_PREFIX = f"{NL2SQL_CONNECTION_ID}{MCP_TOOL_SEPARATOR}"
_APPROVAL_SKIPPED_CODES = frozenset({MCP_APPROVAL_SKIPPED_CODE, AUTOMATION_APPROVAL_SKIPPED_CODE})

ObservedToolStatus = Literal[
    "pending",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
    "approval_skipped",
]
EvidenceSource = Literal["search", "read"]
ValidationStatus = Literal["completed", "skipped", "unvalidated"]


@dataclass(slots=True)
class ObservedToolCallItem:
    index: int
    call_id: str
    name: str
    status: ObservedToolStatus
    error_code: str | None
    started_at: datetime | None
    completed_at: datetime | None
    include_superseded: bool | None
    job_status: str | None
    row_count: int | None
    rows_truncated: bool | None
    # 根拠のツール・読み取りのツールが返した根拠の文書（呼び出しの中の重複を含む。見られない根拠を
    # 除いた数を `evidence_count` にする）。ほかのツールは None。
    evidence_documents: list[str] | None = None
    # RAG の根拠のツールの縮退（#3546）。ほかのツール・失敗した呼び出しは None。
    degraded_steps: list[dict[str, Any]] | None = None
    guardrail_degraded: bool | None = None
    zero_evidence_stage: str | None = None


@dataclass(slots=True)
class ObservedEvidenceItem:
    rank: int
    tool_call_index: int
    source: EvidenceSource
    document_id: str
    chunk_id: str
    text: str
    # 生成に渡した本文が chunk の一部だけか（どの呼び出しも全文を渡していない）。
    truncated: bool
    file_name: str | None = None
    chunk_set_id: str | None = None
    evidence_type: str | None = None
    origin: str | None = None
    superseded: bool = False
    retrieval_rank: int | None = None
    locator: dict[str, Any] = field(default_factory=dict)
    table_binding: str | None = None


@dataclass(slots=True)
class RunObservationData:
    tool_calls: list[ObservedToolCallItem]
    tool_call_total: int
    evidence: list[ObservedEvidenceItem]


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _records(value: object) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _rag_document(value: object) -> str | None:
    """RAG の文書の ID（会話の添付の根拠は RAG の文書ではないので None）。"""
    document_id = _str(value)
    if document_id is None or document_id.startswith(ATTACHMENT_DOCUMENT_PREFIX):
        return None
    return document_id


def _locator(value: Mapping[str, Any]) -> dict[str, Any]:
    """RAG の `EvidenceLocator` の名前の項目だけ（値の型が合うものだけ写す）。"""
    locator: dict[str, Any] = {}
    sections = value.get("section_path")
    if isinstance(sections, list):
        locator["section_path"] = [item for item in sections if isinstance(item, str)]
    for key in (
        "page_start",
        "page_end",
        "row_start",
        "row_end",
        "source_line_start",
        "source_line_end",
        "table_row",
        "table_index",
    ):
        number = _int(value.get(key))
        if number is not None:
            locator[key] = number
    if (basis := _str(value.get("row_basis"))) in ROW_BASES:
        locator["row_basis"] = basis
    for key in (
        "page_label_start",
        "page_label_end",
        "sheet_name",
        "cell_range",
        "element_locator",
    ):
        text = _str(value.get(key))
        if text is not None:
            locator[key] = text
    cells = _table_cells(value.get("table_cells"))
    if cells:
        locator["table_cells"] = cells
    return locator


def _table_cells(value: object) -> list[dict[str, Any]]:
    """表のセル（RAG の `EvidenceTableCell`）。形の合わないセルがあれば何も写さない。"""
    items = value if isinstance(value, list) else []
    cells: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            return []
        column, path = _str(item.get("table_col")), item.get("header_path")
        if column is None or not isinstance(path, list):
            return []
        if not all(isinstance(part, str) for part in path):
            return []
        cells.append({"table_col": column, "header_path": list(path)})
    return cells


@dataclass(slots=True)
class _Piece:
    document_id: str
    chunk_id: str
    text: str
    complete: bool
    file_name: str | None = None
    chunk_set_id: str | None = None
    evidence_type: str | None = None
    origin: str | None = None
    superseded: bool = False
    retrieval_rank: int | None = None
    locator: dict[str, Any] = field(default_factory=dict)
    table_binding: str | None = None


def _search_pieces(output: Mapping[str, Any]) -> list[_Piece]:
    pieces: list[_Piece] = []
    for item in _records(output.get("evidence")):
        document_id, chunk_id = _rag_document(item.get("document_id")), _str(item.get("chunk_id"))
        if document_id is None or chunk_id is None:
            continue
        excerpt = item.get("excerpt")
        locator = item.get("locator")
        pieces.append(
            _Piece(
                document_id=document_id,
                chunk_id=chunk_id,
                text=excerpt if isinstance(excerpt, str) else "",
                # RAG が 1 件の上限で切った・応答の予算で省いた根拠は、生成に一部だけを渡した。
                complete=item.get("truncated") is not True and item.get("text_omitted") is not True,
                file_name=_str(item.get("file_name")),
                chunk_set_id=_str(item.get("chunk_set_id")),
                evidence_type=_str(item.get("evidence_type")),
                origin=_str(item.get("origin")),
                superseded=item.get("superseded") is True,
                retrieval_rank=_int(item.get("retrieval_rank")),
                locator=_locator(locator) if isinstance(locator, Mapping) else {},
                table_binding=table_binding(item),
            )
        )
    return pieces


def _read_pieces(tool: str, output: Mapping[str, Any]) -> list[_Piece]:
    """読み取りのツールの出力の chunk（rag の評価の CLI の `_read_items` と同じ規則）。"""
    document_id = _rag_document(output.get("document_id"))
    if document_id is None:
        return []
    file_name = _str(output.get("file_name"))
    chunk_set_id = _str(output.get("chunk_set_id"))
    superseded = output.get("superseded") is True
    if tool == RAG_READ_SOURCE:
        chunk_id = _str(output.get("chunk_id"))
        if chunk_id is None:
            return []
        text = output.get("text")
        locator = output.get("locator")
        return [
            _Piece(
                document_id=document_id,
                chunk_id=chunk_id,
                text=text if isinstance(text, str) else "",
                # 先頭から終わりまで読んだときだけ全文。
                complete=_int(output.get("offset")) in {0, None}
                and output.get("truncated") is not True,
                file_name=file_name,
                chunk_set_id=chunk_set_id,
                evidence_type=_str(output.get("evidence_type")),
                superseded=superseded,
                locator=_locator(locator) if isinstance(locator, Mapping) else {},
            )
        ]
    whole = output.get("text")
    whole = whole if isinstance(whole, str) else ""
    pieces: list[_Piece] = []
    for chunk in _records(output.get("chunks")):
        chunk_id = _str(chunk.get("chunk_id"))
        if chunk_id is None:
            continue
        start, end = _int(chunk.get("start")), _int(chunk.get("end"))
        pieces.append(
            _Piece(
                document_id=document_id,
                chunk_id=chunk_id,
                text=whole[start:end] if start is not None and end is not None else "",
                complete=start is not None and end is not None,
                file_name=file_name,
                chunk_set_id=chunk_set_id,
                superseded=superseded,
                locator=_locator(chunk),
            )
        )
    return pieces


def _status(step: RunStep) -> ObservedToolStatus:
    result = step.tool_result
    if result is not None and result.error_code in _APPROVAL_SKIPPED_CODES:
        return "approval_skipped"
    if step.status == StepStatus.COMPLETED and result is not None and not result.success:
        return "failed"
    return step.status.value


def _error_code(step: RunStep) -> str | None:
    result = step.tool_result
    if result is None or result.success:
        return None
    code = result.error_code
    if not code:
        return _OTHER_ERROR_CODE
    return code if _ERROR_CODE.match(code) else _OTHER_ERROR_CODE


def _succeeded(step: RunStep) -> Mapping[str, Any] | None:
    result = step.tool_result
    if step.status != StepStatus.COMPLETED or result is None or not result.success:
        return None
    return result.output if isinstance(result.output, Mapping) else None


def observe_run(run: RunState) -> RunObservationData:
    """Run の保存した記録から、ツールの呼び出しと根拠の観測を作る（権限の確かめ直しの前）。"""
    calls: list[ObservedToolCallItem] = []
    evidence: list[ObservedEvidenceItem] = []
    seen: dict[tuple[str, str], int] = {}
    tool_steps = [(step, step.tool_call) for step in run.steps if step.tool_call is not None]
    for index, (step, call) in enumerate(tool_steps):
        tool = mcp_base_tool_name(call.name)
        rag = call.name.startswith(_RAG_PREFIX)
        nl2sql = call.name.startswith(_NL2SQL_PREFIX)
        output = _succeeded(step)
        include_superseded: bool | None = None
        job_status: str | None = None
        row_count: int | None = None
        rows_truncated: bool | None = None
        documents: list[str] | None = None
        degraded: list[dict[str, Any]] | None = None
        guardrail: bool | None = None
        zero_stage: str | None = None
        pieces: list[_Piece] = []
        source: EvidenceSource = "search"
        if rag and tool in EVIDENCE_TOOLS:
            include_superseded = call.arguments.get("include_superseded") is True
            pieces = _search_pieces(output) if output is not None else []
            documents = [piece.document_id for piece in pieces]
            if output is not None:
                degraded = degraded_steps(output)
                guardrail = guardrail_degraded(output)
                zero_stage = zero_evidence_stage(output)
        elif rag and tool in READ_TOOLS:
            pieces = _read_pieces(tool, output) if output is not None else []
            documents = [piece.document_id for piece in pieces]
            source = "read"
        elif nl2sql and tool in NL2SQL_JOB_TOOLS and output is not None:
            status = output.get("status")
            job_status = status if status in NL2SQL_JOB_STATUSES else None
            row_count = _int(output.get("returned_count"))
            truncated = output.get("truncated")
            rows_truncated = truncated if isinstance(truncated, bool) else None
        if index < MAX_OBSERVED_TOOL_CALLS:
            calls.append(
                ObservedToolCallItem(
                    index=index,
                    call_id=step.id,
                    name=call.name,
                    status=_status(step),
                    error_code=_error_code(step),
                    started_at=step.started_at,
                    completed_at=step.completed_at,
                    include_superseded=include_superseded,
                    job_status=job_status,
                    row_count=row_count,
                    rows_truncated=rows_truncated,
                    evidence_documents=documents,
                    degraded_steps=degraded,
                    guardrail_degraded=guardrail,
                    zero_evidence_stage=zero_stage,
                )
            )
        for piece in pieces:
            key = (piece.document_id, piece.chunk_id)
            known_index = seen.get(key)
            if known_index is not None:
                known = evidence[known_index]
                # 同じ chunk を後で渡した本文は、まだ無い本文だけ足す（順は先に出た位置のまま）。
                if piece.text and piece.text not in known.text:
                    known.text = f"{known.text}\n{piece.text}" if known.text else piece.text
                known.truncated = known.truncated and not piece.complete
                if known.table_binding is None:
                    known.table_binding = piece.table_binding
                continue
            seen[key] = len(evidence)
            evidence.append(
                ObservedEvidenceItem(
                    rank=len(evidence) + 1,
                    tool_call_index=index,
                    source=source,
                    document_id=piece.document_id,
                    chunk_id=piece.chunk_id,
                    text=piece.text,
                    truncated=not piece.complete,
                    file_name=piece.file_name,
                    chunk_set_id=piece.chunk_set_id,
                    evidence_type=piece.evidence_type,
                    origin=piece.origin,
                    superseded=piece.superseded,
                    retrieval_rank=piece.retrieval_rank,
                    locator=piece.locator,
                    table_binding=piece.table_binding,
                )
            )
    return RunObservationData(tool_calls=calls, tool_call_total=len(tool_steps), evidence=evidence)


def answer_citations(
    items: Sequence[Mapping[str, Any]],
) -> dict[tuple[str, str], str | None]:
    """回答で引用した根拠（成果物「回答の根拠」の `cited`）→ 回答での使われ方（`answer_use`）。"""
    cited: dict[tuple[str, str], str | None] = {}
    for item in items:
        if item.get("cited") is not True:
            continue
        document_id, chunk_id = _rag_document(item.get("document_id")), _str(item.get("chunk_id"))
        if document_id is None or chunk_id is None:
            continue
        use = item.get("answer_use")
        cited[(document_id, chunk_id)] = use if use in {"confirmed", "reference"} else None
    return cited


def run_agent_version(run: RunState) -> int | None:
    """Run に使った業務 Agent の公開の版（下書きの実行・記録の無い Run は None）。"""
    return _int(run.metadata.get("agent_version"))


def validation_status(content: Mapping[str, Any]) -> ValidationStatus | None:
    """最終の検証の状態（成果物「資料との照合」の `status`。無い・知らない値は None）。"""
    status = content.get("status")
    if status == STATUS_COMPLETED:
        return "completed"
    if status == STATUS_SKIPPED:
        return "skipped"
    if status == STATUS_UNVALIDATED:
        return "unvalidated"
    return None
