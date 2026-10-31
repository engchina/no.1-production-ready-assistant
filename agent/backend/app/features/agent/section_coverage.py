"""回答に使った章節の未読の続き（#2492）。

`rag_read_section` は章節を `max_chars` までしか返さず、続きがあれば `section_complete=false` と
`next_cursor` を返す。手順・一覧・条件のまとまりを求める質問で、回答に使った章節の続きを読まずに
答えると、後半の手順・前提を欠いた回答になる。最終の検証（`rag_validate_answer`）は回答の主張が
根拠に合うかを確かめるだけなので、書かれていない手順の不足は見つからない。

ここは Run の step から章節ごとの読み取りの状態を決定的に追跡し（モデルは呼ばない）、回答に使った
章節のうち終わりまで読めていないものを返す。

- 同じ章節: 同じ MCP 接続・文書で、`section_id` が同じもの。文書が取り込み直されて続きの位置が
  古くなった（`source_stale`）後に読み直した章節は、題名が同じなら同じ章節とみなす。
- 読み切った: その章節か、その上の章節（読み取りの結果の `parent_section_id` をたどる）を
  `section_complete=true` まで読んだ。
- 続きの読み取り: `cursor` を渡した呼び出しを、その `next_cursor` を返した章節に対応付ける。
  読めなかった理由は、文書の版が変わった（`stale`）・ツールの呼び出しの上限（`budget`）・
  そのほかの失敗（`failed`）。続きを呼んでいなければ `unread`。
- 読み始めていない章節（#2503）: 根拠を集めるツール（`rag_search` / `rag_retrieve_evidence`）の
  根拠の章節（`section`）が抜粋より長く、その章節の chunk を根拠ですべて得ていない
  （`chunk_count`）ものは、`rag_read_section` で読み始めていなくても章節として追跡する（理由
  `not_read`）。文書を先頭から `rag_read_document` で終わりまで読んだ文書の章節は読み切ったと
  みなす。
- 回答に使った章節: 回答が章節の chunk の id を書いたか、回答の本文が章節の本文・題名と当たる
  （`evidence_match`）。探索で読んだだけで回答に使っていない章節は数えない。
- 質問が手順・一覧・条件のまとまりを求めるとき（`asks_for_grouped_content`）だけ判定する。

呼ぶ側（組み込み Runtime）は、未読の続きがあり予算が残っていれば答える前に 1 回だけ差し戻し、
それでも残れば回答に「読み切れていない章節」を示し、最終の検証の成果物に主張の照合とは別の
`coverage` として残す。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app.features.agent.answer_validation import cited_chunk_ids
from app.features.agent.evidence_match import EvidenceText, answer_features, matched_features
from app.features.agent.support_task import (
    BUDGET_EXCEEDED_CODE,
    RAG_BUDGET_TOOLS,
    RAG_READ_DOCUMENT,
    RAG_READ_SECTION,
    asks_for_grouped_content,
)
from app.features.agent.tools import MCP_TOOL_SEPARATOR, mcp_base_tool_name

if TYPE_CHECKING:
    from app.features.agent.runtime import RunStep

JsonObject = dict[str, Any]

# 続きを読めていない理由（`not_read` は章節を読み始めていない。#2503）。
REASON_NOT_READ = "not_read"
REASON_UNREAD = "unread"
REASON_STALE = "stale"
REASON_BUDGET = "budget"
REASON_FAILED = "failed"
# 差し戻せば読める理由（予算の上限は差し戻しても読めない）。
RETRYABLE_REASONS = frozenset({REASON_NOT_READ, REASON_UNREAD, REASON_STALE, REASON_FAILED})
# 文書を先頭から読むときに渡さない、読み始めの位置の引数（`rag_read_document`）。
_DOCUMENT_START_ARGUMENTS = ("cursor", "locator", "page", "section_id")
# RAG が続きの位置を古い版のもの・形の違うものとして拒んだときの code（契約 rag-tools.json の
# `errors`）。
_STALE_CODES = frozenset({"source_stale", "cursor_invalid"})

COVERAGE_COMPLETE = "complete"
COVERAGE_INCOMPLETE = "incomplete"
# 差し戻し・回答の注記に挙げる章節の上限。
MAX_UNREAD_SECTIONS = 5

UNREAD_SECTIONS_RETRY_HINT = (
    "直前の回答は、根拠にした次の章節を終わりまで読まずに作られています。質問は手順・一覧・条件の"
    "まとまりを求めているので、章節ごとに終わりまで読んでから改めて回答してください。読み切れない"
    "章節は、確かめていない範囲として回答に示してください。"
)
_START_LINE = (
    "章節を読んでいない。document_id と section_id を rag_read_section に渡して終わりまで読む"
)
_CONTINUE_LINE = "next_cursor を rag_read_section の cursor（document_id も渡す）に渡して続きを読む"
_STALE_LINE = (
    "文書が更新され続きの位置が古い。rag_outline で章節を確かめ、section_id を "
    "rag_read_section に渡して最初から読み直す"
)
UNREAD_SECTIONS_HEADING = "読み切れていない章節"
UNREAD_SECTIONS_INTRO = (
    "次の章節は終わりまで読めていないため、手順・条件の全体は確かめられていません。"
)
_REASON_LABELS = {
    REASON_NOT_READ: "章節を読んでいません（検索の抜粋だけを使いました）",
    REASON_UNREAD: "続きを読んでいません",
    REASON_STALE: "文書が更新され、続きの位置が古くなりました",
    REASON_BUDGET: "この実行のツールの呼び出しの上限に達しました",
    REASON_FAILED: "続きの読み取りに失敗しました",
}


@dataclass
class _Section:
    """1 つの章節の読み取りの状態。"""

    connection: str
    document_id: str
    section_id: str
    title: str = ""
    file_name: str | None = None
    parent_section_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    complete: bool = False
    next_cursor: str | None = None
    reason: str = REASON_NOT_READ
    # 根拠を集めるツールの根拠で得た chunk（章節の chunk をすべて得たら読み切ったとみなす）。
    evidence_chunk_ids: set[str] = field(default_factory=set)
    chunk_ids: list[str] = field(default_factory=list)
    evidence: EvidenceText = field(default_factory=EvidenceText)


def _connection(function_name: str) -> str:
    """function tool の名前の接続の部分（`rag__rag_read_section` → `rag`。無ければ空）。"""
    if MCP_TOOL_SEPARATOR not in function_name:
        return ""
    return function_name.split(MCP_TOOL_SEPARATOR, 1)[0]


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _failure_reason(step: RunStep) -> str:
    result = step.tool_result
    if result is None:
        return REASON_FAILED
    if result.error_code == BUDGET_EXCEEDED_CODE:
        return REASON_BUDGET
    details = result.error_details if isinstance(result.error_details, dict) else {}
    if result.error_code in _STALE_CODES or details.get("error_code") in _STALE_CODES:
        return REASON_STALE
    return REASON_FAILED


def _succeeded_output(step: RunStep) -> JsonObject | None:
    result = step.tool_result
    if result is None or not result.success or step.status != "completed":
        return None
    return result.output if isinstance(result.output, dict) else None


def _track_evidence(
    sections: dict[tuple[str, str, str], _Section],
    parents: dict[tuple[str, str, str], str],
    connection: str,
    output: JsonObject,
) -> None:
    """根拠を集めるツールの根拠の章節のうち、抜粋より長い章節を追跡に加える（#2503）。"""
    for item in output.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        section = item.get("section")
        document_id = _text(item.get("document_id"))
        section_id = _text(section.get("section_id")) if isinstance(section, dict) else ""
        if not isinstance(section, dict) or not document_id or not section_id:
            continue
        excerpt = _text(item.get("excerpt"))
        chars = _int(section.get("chars"))
        key = (connection, document_id, section_id)
        entry = sections.get(key)
        if entry is None:
            if chars is None or chars <= len(excerpt):
                # 抜粋で章節の全体を得ている。
                continue
            entry = sections[key] = _Section(connection, document_id, section_id)
            entry.title = _text(section.get("title"))
            entry.page_start = _int(section.get("page_start"))
            entry.page_end = _int(section.get("page_end"))
        entry.file_name = entry.file_name or _text(item.get("file_name")) or None
        parent = _text(section.get("parent_section_id"))
        if parent:
            parents.setdefault(key, parent)
        chunk_id = _text(item.get("chunk_id"))
        if chunk_id:
            entry.evidence_chunk_ids.add(chunk_id)
            if chunk_id not in entry.chunk_ids:
                entry.chunk_ids.append(chunk_id)
        entry.evidence.add(excerpt, [entry.title])
        chunk_count = _int(section.get("chunk_count"))
        if chunk_count is not None and len(entry.evidence_chunk_ids) >= chunk_count > 0:
            # 根拠で章節の chunk をすべて得た。
            entry.complete = True


def _track(steps: Sequence[RunStep]) -> list[_Section]:
    """`rag_read_section` と根拠の step から、章節ごとの読み取りの状態を作る（読んだ順）。"""
    sections: dict[tuple[str, str, str], _Section] = {}
    by_cursor: dict[tuple[str, str], _Section] = {}
    parents: dict[tuple[str, str, str], str] = {}
    # 先頭から終わりまで読んだ文書と、先頭から読んでいる途中の文書の続きの位置
    # （rag_read_document）。
    documents_read: set[tuple[str, str]] = set()
    document_cursors: dict[tuple[str, str], str] = {}
    for step in steps:
        call = step.tool_call
        if call is None:
            continue
        tool = mcp_base_tool_name(call.name)
        connection = _connection(call.name)
        arguments = call.arguments if isinstance(call.arguments, dict) else {}
        if tool in RAG_BUDGET_TOOLS:
            output = _succeeded_output(step)
            if output is not None:
                _track_evidence(sections, parents, connection, output)
            continue
        if tool == RAG_READ_DOCUMENT:
            output = _succeeded_output(step)
            if output is None:
                continue
            document_id = _text(output.get("document_id"))
            cursor = _text(arguments.get("cursor"))
            from_start = not any(arguments.get(name) for name in _DOCUMENT_START_ARGUMENTS)
            if not document_id or not (
                from_start or document_cursors.get((connection, cursor)) == document_id
            ):
                continue
            next_cursor = _text(output.get("next_cursor"))
            if next_cursor:
                document_cursors[(connection, next_cursor)] = document_id
            else:
                documents_read.add((connection, document_id))
            continue
        if tool != RAG_READ_SECTION:
            continue
        cursor = _text(arguments.get("cursor"))
        result = step.tool_result
        output = result.output if result is not None else None
        if (
            result is None
            or not result.success
            or step.status != "completed"
            or not isinstance(output, dict)
        ):
            entry = by_cursor.get((connection, cursor)) if cursor else None
            if entry is not None and not entry.complete:
                entry.reason = _failure_reason(step)
            continue
        section = output.get("section")
        document_id = _text(output.get("document_id"))
        section_id = _text(section.get("section_id")) if isinstance(section, dict) else ""
        if not isinstance(section, dict) or not document_id or not section_id:
            continue
        key = (connection, document_id, section_id)
        entry = sections.get(key)
        if entry is None:
            entry = sections[key] = _Section(connection, document_id, section_id)
        entry.title = _text(section.get("title")) or entry.title
        # 古い版の位置で続きを読めなかった同じ題名の章節は、取り込み直した後のこの章節にまとめる。
        for other_key, other in list(sections.items()):
            if (
                other is not entry
                and other.reason == REASON_STALE
                and not other.complete
                and other.connection == connection
                and other.document_id == document_id
                and entry.title
                and other.title == entry.title
            ):
                entry.chunk_ids += [item for item in other.chunk_ids if item not in entry.chunk_ids]
                entry.evidence.add("\n".join(other.evidence.texts), [])
                del sections[other_key]
        entry.file_name = _text(output.get("file_name")) or entry.file_name
        entry.parent_section_id = _text(section.get("parent_section_id")) or None
        entry.page_start = _int(section.get("page_start"))
        entry.page_end = _int(section.get("page_end"))
        if entry.parent_section_id:
            parents[key] = entry.parent_section_id
        for child in output.get("child_sections") or []:
            child_id = _text(child.get("section_id")) if isinstance(child, dict) else ""
            if child_id:
                parents[(connection, document_id, child_id)] = section_id
        for chunk in output.get("chunks") or []:
            chunk_id = _text(chunk.get("chunk_id")) if isinstance(chunk, dict) else ""
            if chunk_id and chunk_id not in entry.chunk_ids:
                entry.chunk_ids.append(chunk_id)
        entry.evidence.add(output.get("text"), [entry.title])
        if output.get("section_complete") is True:
            entry.complete = True
            entry.next_cursor = None
        elif not entry.complete:
            entry.next_cursor = _text(output.get("next_cursor")) or None
            entry.reason = REASON_UNREAD
            if entry.next_cursor:
                by_cursor[(connection, entry.next_cursor)] = entry
    complete = {key for key, entry in sections.items() if entry.complete}
    unread: list[_Section] = []
    for key, entry in sections.items():
        if entry.complete or (entry.connection, entry.document_id) in documents_read:
            continue
        # 上の章節を読み切っていれば、その中の章節も読み切っている。
        connection, document_id, section_id = key
        seen = {section_id}
        parent = parents.get(key)
        covered = False
        while parent and parent not in seen:
            if (connection, document_id, parent) in complete:
                covered = True
                break
            seen.add(parent)
            parent = parents.get((connection, document_id, parent))
        if not covered:
            unread.append(entry)
    return unread


def _used(entry: _Section, answer: str) -> bool:
    """回答がその章節を使ったか（chunk の id を書いた・本文・題名と当たる）。"""
    if cited_chunk_ids(answer, entry.chunk_ids):
        return True
    return bool(matched_features(answer_features(answer), entry.evidence))


def _record(entry: _Section, *, budget_exhausted: bool) -> JsonObject:
    reason = entry.reason
    if budget_exhausted and reason in {REASON_NOT_READ, REASON_UNREAD}:
        reason = REASON_BUDGET
    return {
        "connection": entry.connection,
        "document_id": entry.document_id,
        "file_name": entry.file_name,
        "section_id": entry.section_id,
        "title": entry.title,
        "page_start": entry.page_start,
        "page_end": entry.page_end,
        "next_cursor": entry.next_cursor,
        "reason": reason,
    }


def section_coverage(
    steps: Sequence[RunStep],
    answer: str,
    question_texts: Sequence[object],
    *,
    budget_exhausted: bool = False,
) -> JsonObject | None:
    """回答に使った章節を終わりまで読んだか（判定しなければ None）。

    質問が手順・一覧・条件のまとまりを求め、`rag_read_section` で読んだ章節か、根拠を集めるツールの
    根拠の章節がある Run だけ判定する。
    返すのは `status`（`complete` / `incomplete`）と、回答に使ったのに終わりまで読めていない章節
    （`sections`。理由 `reason` つき、最大 `MAX_UNREAD_SECTIONS` 件）。`budget_exhausted` は、
    この実行のツールの呼び出しの上限に達したか（続きを読んでいない章節の理由を `budget` にする）。
    """
    if not answer.strip() or not asks_for_grouped_content(question_texts):
        return None
    if not any(
        step.tool_call is not None
        and mcp_base_tool_name(step.tool_call.name) in {RAG_READ_SECTION, *RAG_BUDGET_TOOLS}
        for step in steps
    ):
        return None
    unread = [
        _record(entry, budget_exhausted=budget_exhausted)
        for entry in _track(steps)
        if _used(entry, answer)
    ][:MAX_UNREAD_SECTIONS]
    return {
        "status": COVERAGE_INCOMPLETE if unread else COVERAGE_COMPLETE,
        "sections": unread,
    }


def _label(section: JsonObject) -> str:
    start, end = section.get("page_start"), section.get("page_end")
    pages = ""
    if isinstance(start, int):
        pages = f"p.{start}" + (f"-{end}" if isinstance(end, int) and end != start else "")
    name = section.get("file_name") or section.get("document_id")
    place = "、".join(str(item) for item in (name, pages) if item)
    return f"「{section.get('title') or section.get('section_id')}」（{place}）"


def unread_sections_message(sections: Sequence[JsonObject]) -> str:
    """差し戻しの文（章節ごとに 1 行。続きの cursor か読み直し方）。"""
    lines = [UNREAD_SECTIONS_RETRY_HINT, ""]
    for section in sections:
        head = (
            f"- {_label(section)}（document_id: {section['document_id']}、"
            f"section_id: {section['section_id']}）: "
        )
        if section.get("reason") == REASON_NOT_READ:
            lines.append(head + _START_LINE + "。")
        elif section.get("reason") == REASON_STALE or not section.get("next_cursor"):
            lines.append(head + _STALE_LINE + "。")
        else:
            lines.append(head + f"{_CONTINUE_LINE}（next_cursor: {section['next_cursor']}）。")
    return "\n".join(lines)


def with_unread_sections_notice(answer: str, sections: Sequence[JsonObject]) -> str:
    """回答の末尾に、終わりまで読めていない章節と理由を足す（利用者に見せる回答）。"""
    lines = [UNREAD_SECTIONS_INTRO]
    for section in sections:
        reason = _REASON_LABELS.get(str(section.get("reason")), _REASON_LABELS[REASON_FAILED])
        lines.append(f"- {_label(section)}: {reason}")
    return f"{answer.rstrip()}\n\n**{UNREAD_SECTIONS_HEADING}**\n\n" + "\n".join(lines)


__all__ = [
    "COVERAGE_COMPLETE",
    "COVERAGE_INCOMPLETE",
    "RETRYABLE_REASONS",
    "UNREAD_SECTIONS_HEADING",
    "UNREAD_SECTIONS_RETRY_HINT",
    "section_coverage",
    "unread_sections_message",
    "with_unread_sections_notice",
]
