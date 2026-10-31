"""確認の取り消し（#1441・#1445）。

Agent のチャットで、確かめ中の確認を利用者がいつでも取り消せる。確認には 2 種類ある。

- NL2SQL の SQL の生成前の確認（`nl2sql_query` の `needs_clarification`）: 取り消しの操作は
  「データベースの検索は不要」。
- RAG の確認（`rag_search` の `needs_clarification` の `clarifications`・業務ガイドの clarify。
  どの画面・機能・業務か など）: 取り消しの操作は「資料の検索は不要」。

取り消しの出所（`trigger`）:

- `button`: チャットの確認の質問のそばの操作（`POST /api/runs` の `clarification_cancel`）。
- `message`: 確かめ中の問いがあるときの会話の文（「データベースは関係ない」「資料は関係ない」
  「確認はやめて」など。決定的な文の判定で、モデルに判定させない）。

取り消した Run の扱い（`builtin_runtime`）:

- その種類のツール（NL2SQL は `nl2sql_*`、RAG は `rag_*`）をモデルに渡さない（その質問で使わない）。
- 指示に「ほかの手段で続けるか、利用者に意図を聞き直す」を足す。
- NL2SQL は確かめ中の SQL の生成前の確認の session を閉じる（Control Plane が
  `nl2sql_cancel_requirement` を呼ぶ。step に残し、予算には数えない）。

記録（同じ形）: Run の metadata `clarification_cancel`（取り消しの一覧）とイベント、支援タスクの
状態の `clarification_cancellations`（会話の通し）。1 件は
`{kind, trigger, run_id, at, questions, requirement_sessions}`。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any, Literal

from app.features.agent.tools import MCP_TOOL_SEPARATOR, mcp_base_tool_name

JsonObject = dict[str, Any]

ClarificationKind = Literal["nl2sql", "rag"]
KIND_NL2SQL: ClarificationKind = "nl2sql"
KIND_RAG: ClarificationKind = "rag"
CLARIFICATION_KINDS: tuple[ClarificationKind, ...] = (KIND_NL2SQL, KIND_RAG)
TRIGGER_BUTTON = "button"
TRIGGER_MESSAGE = "message"
# Run の metadata の key（Control Plane の予約。利用者の metadata では付けられない）。
CANCEL_METADATA_KEY = "clarification_cancel"
# Control Plane が呼ぶ SQL の生成前の確認の取り消し（`nl2sql_cancel_requirement`）の
# trace_id の接頭辞。モデルの予算・消費には数えない。
CANCEL_TRACE_PREFIX = "clarification_cancel_"
NL2SQL_CANCEL_TOOL = "nl2sql_cancel_requirement"
# 取り消したときにモデルへ渡さないツール（ツールの部分の接頭辞）。
_DECLINED_TOOL_PREFIXES: dict[str, str] = {KIND_NL2SQL: "nl2sql_", KIND_RAG: "rag_"}
_KIND_LABELS: dict[str, str] = {KIND_NL2SQL: "データベース（NL2SQL）", KIND_RAG: "資料（RAG）"}
MAX_CANCELLATIONS = 10
_MAX_QUESTIONS = 10
_MAX_TEXT_CHARS = 300

# 会話の文の判定（NFKC・小文字にしてから照合する）。対象の語の後に、要らない・関係ないことを示す語。
_SUBJECTS: dict[str, str] = {
    KIND_NL2SQL: r"(?:データベース|db|sql|nl2sql)",
    KIND_RAG: r"(?:資料|文書|ドキュメント|マニュアル|ナレッジ|rag)",
}
_DECLINE = (
    r"(?:関係(?:ない|なく|ありません|無い|のない|しない)|不要|要らない|いらない|"
    r"必要(?:ない|ありません|無い|はない|はありません)|使わ(?:ない|ず|なくて)|"
    r"調べ(?:なくて|る必要は)|見なくて|検索(?:しなくて|は不要|はいらない)|"
    r"(?:の|に関する)?質問(?:では|じゃ)(?:ない|ありません)|"
    r"(?:では|じゃ)(?:ない|ありません))"
)
_KIND_PATTERNS = {
    kind: re.compile(rf"{subject}[^。！？!?\n]{{0,16}}?{_DECLINE}")
    for kind, subject in _SUBJECTS.items()
}
# 種類を言わずに確認そのものをやめる（確かめ中の確認をすべて取り消す）。
_ANY_PATTERN = re.compile(
    r"(?:確認|質問)(?:は|を)?(?:もう)?(?:やめ|取り消|取消|キャンセル|中止|不要|いらない|要らない)"
)


def _text(value: object, limit: int = _MAX_TEXT_CHARS) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return ""
    return " ".join(str(value).split())[:limit]


def _records(value: object) -> list[JsonObject]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def clarification_kind(entry: Mapping[str, Any]) -> ClarificationKind:
    """確かめ中の問いの種類（`kind` の無い問いは RAG の確認）。"""
    return KIND_NL2SQL if entry.get("kind") == KIND_NL2SQL else KIND_RAG


def pending_of(state: Mapping[str, Any] | None, kind: str) -> list[JsonObject]:
    """前の Run の状態の、その種類の確かめ中の問い。"""
    if not isinstance(state, Mapping):
        return []
    return [
        item
        for item in _records(state.get("pending_clarifications"))
        if clarification_kind(item) == kind
    ]


def pending_kinds(state: Mapping[str, Any] | None) -> list[ClarificationKind]:
    return [kind for kind in CLARIFICATION_KINDS if pending_of(state, kind)]


def message_cancel_kinds(goal: str, state: Mapping[str, Any] | None) -> list[ClarificationKind]:
    """利用者の発言が、確かめ中の確認の取り消しか（取り消す種類。確かめ中の種類だけ）。"""
    pending = pending_kinds(state)
    if not pending or not goal.strip():
        return []
    text = unicodedata.normalize("NFKC", goal).lower()
    named = [kind for kind in pending if _KIND_PATTERNS[kind].search(text)]
    if named:
        return named
    return list(pending) if _ANY_PATTERN.search(text) else []


def button_cancel_kinds(metadata: Mapping[str, Any]) -> list[ClarificationKind]:
    """取り消しの操作（`POST /api/runs` の `clarification_cancel`）で作った Run の種類。"""
    return [
        kind
        for record in _records(metadata.get(CANCEL_METADATA_KEY))
        if record.get("trigger") == TRIGGER_BUTTON
        and (kind := clarification_kind(record)) == record.get("kind")
    ]


def declined_kinds(metadata: Mapping[str, Any]) -> set[str]:
    """この Run で取り消した（使わない）確認の種類（Run の記録から）。"""
    return {
        str(record.get("kind"))
        for record in _records(metadata.get(CANCEL_METADATA_KEY))
        if record.get("kind") in CLARIFICATION_KINDS
    }


def is_declined_tool(name: str, kinds: Iterable[str]) -> bool:
    """取り消した種類のツールか（MCP 接続のツールの、ツールの部分の接頭辞で判定する）。"""
    if MCP_TOOL_SEPARATOR not in name:
        return False
    base = mcp_base_tool_name(name)
    return any(base.startswith(_DECLINED_TOOL_PREFIXES[kind]) for kind in kinds)


def button_record(kind: ClarificationKind) -> JsonObject:
    """取り消しの操作で作る Run の、最初の記録（実行の始めに問い・session を足す）。"""
    return {"kind": kind, "trigger": TRIGGER_BUTTON}


def cancel_record(
    kind: ClarificationKind,
    *,
    trigger: str,
    run_id: str,
    at: str,
    pending: list[JsonObject],
) -> JsonObject:
    """取り消しの記録（Run の metadata と支援タスクの状態で同じ形）。"""
    sessions = [
        {"requirement_session_id": session_id, "status": "pending"}
        for session_id in dict.fromkeys(
            _text(item.get("requirement_session_id"), 128) for item in pending
        )
        if session_id
    ]
    return {
        "kind": kind,
        "trigger": trigger,
        "run_id": run_id,
        "at": at,
        "questions": [
            question
            for item in pending[:_MAX_QUESTIONS]
            if (question := _text(item.get("question")))
        ],
        "requirement_sessions": sessions,
    }


def cancel_instructions(records: list[JsonObject]) -> str:
    """取り消した Run の指示（取り消した確認が無ければ空）。"""
    kinds = [
        kind
        for kind in CLARIFICATION_KINDS
        if kind in declined_kinds({CANCEL_METADATA_KEY: records})
    ]
    if not kinds:
        return ""
    lines = ["# 確認の取り消し（利用者の操作）"]
    for kind in kinds:
        if kind == KIND_NL2SQL:
            lines.append(
                "- 利用者は「データベースの検索は不要」と伝え、データベース（NL2SQL）の確認を"
                "取り消しました。この質問ではデータベースを調べず、NL2SQL のツールを使いません"
                "（確認の session は閉じました）。"
            )
        else:
            lines.append(
                "- 利用者は「資料の検索は不要」と伝え、資料（RAG）の確認を取り消しました。"
                "この質問では資料を調べず、RAG のツールを使いません。"
            )
    lines.append(
        "- 前の質問（会話の前の発言）に、ほかの手段（使える別のツール・一般的な知識）で答えられる"
        "なら、そのまま続けてください。答えられないときや、何を知りたいかが分からないときは、"
        "取り消した確認を聞き直さずに、利用者に知りたいことを短く聞き直してください。"
    )
    return "\n".join(lines)


def kind_label(kind: str) -> str:
    return _KIND_LABELS.get(kind, kind)


__all__ = [
    "CANCEL_METADATA_KEY",
    "CANCEL_TRACE_PREFIX",
    "CLARIFICATION_KINDS",
    "KIND_NL2SQL",
    "KIND_RAG",
    "MAX_CANCELLATIONS",
    "NL2SQL_CANCEL_TOOL",
    "TRIGGER_BUTTON",
    "TRIGGER_MESSAGE",
    "ClarificationKind",
    "button_cancel_kinds",
    "button_record",
    "cancel_instructions",
    "cancel_record",
    "clarification_kind",
    "declined_kinds",
    "is_declined_tool",
    "kind_label",
    "message_cancel_kinds",
    "pending_kinds",
    "pending_of",
]
