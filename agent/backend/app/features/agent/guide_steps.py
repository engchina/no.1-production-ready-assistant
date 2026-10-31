"""業務ガイドの手順を段として引く（#2396。動的な多段）。

RAG の `rag_lookup_guides` の最上位の業務ガイドが答える判断（answer / branch）のとき、
その手順（分かっている条件で外れた分岐の手順は RAG が返さない）を段として数え、この Run の
根拠（根拠を集める・読むツールの結果）がどの手順に紐づくかを決定的に判定する（モデルは
呼ばない）。

紐づけの規則（どれか 1 つで紐づく）:
- 参照: 根拠の文書が手順の参照の文書で、参照に頁の範囲があれば根拠の頁が重なる。
- 検索文: 根拠を集めた呼び出しの query に、手順の検索の手がかり（無ければ題名）が出る。
- 本文: 根拠の本文に、手順の検索の手がかりが出る。

判定と次の手（手順ごとの検索の語・参照の頁の読み方）を作るだけで、引くかはモデルが決める
（planner は作らない。#756）。根拠の無い手順で答えたときの差し戻しは、呼ぶ側（組み込み
Runtime）が 1 回だけ行う。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from app.features.agent.tools import mcp_base_tool_name

if TYPE_CHECKING:
    from app.features.agent.runtime import RunStep

JsonObject = dict[str, Any]

RAG_LOOKUP_GUIDES = "rag_lookup_guides"
# 手順の根拠を数えるツール（根拠を集める・読む）。
GUIDE_EVIDENCE_TOOLS = frozenset(
    {
        "rag_search",
        "rag_retrieve_evidence",
        "rag_read_source",
        "rag_read_document",
        "rag_read_section",
    }
)
# 手順を段として数える業務ガイドの判断（確認・引き継ぎは答えないので数えない）。
_ANSWER_DECISIONS = frozenset({"answer", "branch"})
# 手がかりの照合に使う最短の長さ（1 文字の語は本文のどこにでも出る）。
_MIN_HINT_CHARS = 2
# 次の手・差し戻しに挙げる手順と参照の上限。
MAX_GUIDE_STEPS = 20
MAX_STEP_REFERENCES = 5
_MAX_TEXT = 200

GUIDE_STEPS_HINT = (
    "業務ガイドの手順を段として順に引いてください。steps の手順ごとに、質問の対象の語に "
    "query の語を足して rag_retrieve_evidence を呼んで根拠を集め、references の頁は "
    "rag_read_document（document_id と page）で読みます。"
    "根拠の無い手順は、確かめられない手順として示してください。"
)
GUIDE_STEPS_COMBINED_HINT = (
    "この実行で残る検索の回数が手順の数より少ないため、手順の query をまとめて "
    "rag_retrieve_evidence を 1 回呼び、references の頁は rag_read_document"
    "（document_id と page）で読んでください。"
)
UNRESOLVED_GUIDE_STEPS_HINT = (
    "直前の回答は、業務ガイドの次の手順の根拠を集めずに作られています。手順ごとに、検索の手がかりで"
    "rag_retrieve_evidence を呼ぶか、参照の頁を rag_read_document（document_id と page）で読んで"
    "根拠を確かめてから、改めて回答してください。根拠が見つからない手順は、確かめられない手順として"
    "示してください。"
)


def _text(value: object, limit: int = _MAX_TEXT) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, str) else ""


def _records(value: object) -> list[JsonObject]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _norm(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _page_ranges(reference: JsonObject) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for item in _records(reference.get("page_ranges")):
        start, end = item.get("page_start"), item.get("page_end")
        if isinstance(start, int) and not isinstance(start, bool):
            ranges.append((start, end if isinstance(end, int) and end >= start else start))
    return ranges


def _reference(item: JsonObject) -> JsonObject | None:
    document_id = _text(item.get("document_id"), 128)
    if not document_id:
        return None
    return {
        "document_id": document_id,
        "title": _text(item.get("title")),
        "page_ranges": [
            {"page_start": start, "page_end": end} for start, end in _page_ranges(item)
        ],
    }


def _step(item: JsonObject) -> JsonObject | None:
    step_id = _text(item.get("id"), 64)
    if not step_id:
        return None
    hints = [hint for value in item.get("retrieval_hints") or [] if (hint := _text(value))]
    return {
        "id": step_id,
        "title": _text(item.get("title")),
        "retrieval_hints": hints[:10],
        "references": [
            reference
            for value in _records(item.get("references"))
            if (reference := _reference(value)) is not None
        ][:MAX_STEP_REFERENCES],
    }


def plan_from_lookup(output: object) -> JsonObject | None:
    """`rag_lookup_guides` の最上位の業務ガイドの手順（答える判断のときだけ。無ければ None）。"""
    guides = _records(output.get("guides")) if isinstance(output, dict) else []
    if not guides:
        return None
    guide = guides[0]
    if guide.get("decision") not in _ANSWER_DECISIONS:
        return None
    steps = [step for item in _records(guide.get("steps")) if (step := _step(item)) is not None]
    if not steps:
        return None
    return {
        "guide_id": _text(guide.get("guide_id"), 64),
        "revision": guide.get("revision"),
        "title": _text(guide.get("title")),
        "steps": steps[:MAX_GUIDE_STEPS],
    }


def guide_plan(steps: Sequence[RunStep]) -> JsonObject | None:
    """この Run で最後に成功した業務ガイドの照合（モデル・Control Plane）の手順。"""
    for step in reversed(list(steps)):
        call = step.tool_call
        result = step.tool_result
        if call is None or mcp_base_tool_name(call.name) != RAG_LOOKUP_GUIDES:
            continue
        if result is None or not result.success:
            continue
        return plan_from_lookup(result.output)
    return None


def _mapping(value: object) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _pages(start: object, end: object) -> tuple[int, int] | None:
    if not isinstance(start, int) or isinstance(start, bool):
        return None
    return start, end if isinstance(end, int) and end >= start else start


def _evidence_items(tool: str, output: JsonObject) -> list[JsonObject]:
    """根拠の (document_id, chunk_id, 頁, 本文)。根拠を集める・読むツールの結果だけ。"""
    items: list[JsonObject] = []
    if tool in {"rag_search", "rag_retrieve_evidence"}:
        for item in _records(output.get("evidence")):
            locator = _mapping(item.get("locator"))
            items.append(
                {
                    "document_id": item.get("document_id"),
                    "chunk_id": item.get("chunk_id"),
                    "pages": _pages(locator.get("page_start"), locator.get("page_end")),
                    "text": item.get("excerpt") if isinstance(item.get("excerpt"), str) else "",
                }
            )
    elif tool == "rag_read_source":
        locator = _mapping(output.get("locator"))
        items.append(
            {
                "document_id": output.get("document_id"),
                "chunk_id": output.get("chunk_id"),
                "pages": _pages(locator.get("page_start"), locator.get("page_end")),
                "text": output.get("text") if isinstance(output.get("text"), str) else "",
            }
        )
    elif tool in {"rag_read_document", "rag_read_section"}:
        text = output.get("text") if isinstance(output.get("text"), str) else ""
        for chunk in _records(output.get("chunks")):
            items.append(
                {
                    "document_id": output.get("document_id"),
                    "chunk_id": chunk.get("chunk_id"),
                    "pages": _pages(chunk.get("page_start"), chunk.get("page_end")),
                    "text": text,
                }
            )
    return [item for item in items if isinstance(item.get("document_id"), str)]


def _hints(step: JsonObject) -> list[str]:
    hints = step.get("retrieval_hints") or ([step["title"]] if step.get("title") else [])
    return [hint for value in hints if len(hint := _norm(str(value))) >= _MIN_HINT_CHARS]


def _referenced(step: JsonObject, item: JsonObject) -> bool:
    for reference in step.get("references") or []:
        if reference.get("document_id") != item.get("document_id"):
            continue
        ranges = _page_ranges(reference)
        pages = item.get("pages")
        if not ranges:
            return True
        if pages is not None and any(
            start <= pages[1] and pages[0] <= end for start, end in ranges
        ):
            return True
    return False


def step_evidence(plan: JsonObject, steps: Sequence[RunStep]) -> dict[str, list[str]]:
    """手順 id → この Run でその手順に紐づいた根拠の chunk_id（順に、重複なし）。"""
    linked: dict[str, list[str]] = {step["id"]: [] for step in plan.get("steps") or []}
    for run_step in steps:
        call = run_step.tool_call
        result = run_step.tool_result
        if call is None or result is None or not result.success:
            continue
        tool = mcp_base_tool_name(call.name)
        if tool not in GUIDE_EVIDENCE_TOOLS or not isinstance(result.output, dict):
            continue
        query = call.arguments.get("query") if isinstance(call.arguments, dict) else None
        normalized_query = _norm(query) if isinstance(query, str) else ""
        items = _evidence_items(tool, result.output)
        for step in plan.get("steps") or []:
            hints = _hints(step)
            by_query = bool(normalized_query) and any(hint in normalized_query for hint in hints)
            # 本文の照合は検索の手がかりだけ（題名は手順の言い方で、本文に出るとは限らない）。
            body_hints = [
                hint
                for value in step.get("retrieval_hints") or []
                if len(hint := _norm(str(value))) >= _MIN_HINT_CHARS
            ]
            for item in items:
                text = _norm(str(item.get("text") or ""))
                if by_query or _referenced(step, item) or any(hint in text for hint in body_hints):
                    chunk_id = str(item.get("chunk_id") or item.get("document_id"))
                    if chunk_id not in linked[step["id"]]:
                        linked[step["id"]].append(chunk_id)
    return linked


def guide_step_states(steps: Sequence[RunStep]) -> list[JsonObject]:
    """支援タスクの状態に残す手順ごとの根拠の有無（業務ガイドを照合していなければ空）。"""
    plan = guide_plan(steps)
    if plan is None:
        return []
    linked = step_evidence(plan, steps)
    owners: dict[str, int] = {}
    for chunk_ids in linked.values():
        for chunk_id in set(chunk_ids):
            owners[chunk_id] = owners.get(chunk_id, 0) + 1
    return [
        {
            "id": step["id"],
            "title": step["title"],
            # 根拠で確かめられない（題名だけの）手順は None。
            "evidence": bool(linked.get(step["id"])) if _checkable(step) else None,
            # 根拠がほかの手順と共有の chunk だけか（#2708）。共有の chunk のどの文がこの手順の
            # 説明かは根拠の有無では分からないので、説明を確かめたかは最終の検証の手順ごとの
            # 充足（`rag_validate_answer` の `guide_steps`）で決める。
            "shared": bool(linked.get(step["id"]))
            and all(owners[chunk_id] > 1 for chunk_id in linked[step["id"]]),
            "chunk_ids": linked.get(step["id"], [])[:10],
        }
        for step in plan["steps"]
    ]


def unresolved_guide_steps(steps: Sequence[RunStep]) -> list[JsonObject]:
    """答える前に根拠を集めていない業務ガイドの手順（#2396。無ければ空）。

    この Run で業務ガイドを照合し（答える判断）、根拠を集める・読むツールを 1 回以上呼んだ
    ときだけ判定する（根拠のツールを呼ばない回答は別の差し戻しが扱う）。対象は根拠で確かめられる
    手順（検索の手がかりか参照の資料を持つ手順）だけ。題名だけの手順は根拠との紐づけを決められない
    ので数えない。
    """
    plan = guide_plan(steps)
    if plan is None:
        return []
    called = any(
        step.tool_call is not None
        and mcp_base_tool_name(step.tool_call.name) in GUIDE_EVIDENCE_TOOLS
        and step.tool_result is not None
        and step.tool_result.success
        for step in steps
    )
    if not called:
        return []
    linked = step_evidence(plan, steps)
    return [step for step in plan["steps"] if _checkable(step) and not linked.get(step["id"])]


def _checkable(step: JsonObject) -> bool:
    """根拠との紐づけで確かめられる手順か（検索の手がかりか参照の資料を持つ）。"""
    return bool(step.get("retrieval_hints") or step.get("references"))


def _reference_label(reference: JsonObject) -> str:
    pages = "、".join(
        f"p.{item['page_start']}"
        + (f"-{item['page_end']}" if item["page_end"] != item["page_start"] else "")
        for item in reference.get("page_ranges") or []
    )
    name = reference.get("title") or reference["document_id"]
    return f"{name}（document_id: {reference['document_id']}{'、' + pages if pages else ''}）"


def unresolved_guide_steps_message(pending: list[JsonObject]) -> str:
    """差し戻しの文（手順ごとに 1 行。検索の手がかりと参照の資料・頁）。"""
    lines = [UNRESOLVED_GUIDE_STEPS_HINT, ""]
    for step in pending:
        parts = [f"- 手順 {step['id']}「{step['title']}」の根拠が未取得。"]
        if step.get("retrieval_hints"):
            parts.append(f"検索の手がかり: {'、'.join(step['retrieval_hints'])}。")
        if step.get("references"):
            parts.append(
                f"参照: {'、'.join(_reference_label(item) for item in step['references'])}。"
            )
        lines.append("".join(parts))
    return "\n".join(lines)


def guide_steps_next_step(output: object, rag_calls_remaining: int | None) -> JsonObject | None:
    """業務ガイドの手順を段として引く次の手（答える判断で手順があるときだけ。無ければ None）。

    手順ごとの検索の語（質問の語に足す手がかり。無ければ題名）と参照の資料・頁を挙げる。この実行で残る
    検索の回数が手順の数より少なければ、手順の語をまとめて 1 回で引く案内にする（読むツールは
    予算の外）。
    """
    plan = plan_from_lookup(output)
    if plan is None:
        return None
    steps = [
        {
            "id": step["id"],
            "title": step["title"],
            "query": " ".join(step["retrieval_hints"]) or step["title"],
            **({"references": step["references"]} if step["references"] else {}),
        }
        for step in plan["steps"]
    ]
    combined = rag_calls_remaining is not None and rag_calls_remaining < len(steps)
    return {
        "steps": steps,
        "instruction": GUIDE_STEPS_COMBINED_HINT if combined else GUIDE_STEPS_HINT,
    }


__all__ = [
    "GUIDE_EVIDENCE_TOOLS",
    "UNRESOLVED_GUIDE_STEPS_HINT",
    "guide_plan",
    "guide_step_states",
    "guide_steps_next_step",
    "plan_from_lookup",
    "step_evidence",
    "unresolved_guide_steps",
    "unresolved_guide_steps_message",
]
