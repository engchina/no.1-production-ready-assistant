"""回答の段落の印・外した段落・回答全体の警告（#2738）。

回答の検証で、本文から外すのは根拠と矛盾する段落（``contradicted``）だけにする。根拠で確かめられない段落は
本文に残して印（``unverified``）を、決定的な規則の検査は警告（``warning``）を付ける。回答ごとに次の 3 つを持つ
（形の正本は ``platform/contracts/answer-passages/passage-marks.json``。RAG・Agent・UI のテストが読む）。

- ``passage_marks``: 本文に残した段落（``text`` は本文の行の原文）ごとの状態と理由。
- ``removed_passages``: 根拠と矛盾して本文から外した段落と理由。
- ``answer_warnings``: 回答全体の警告（質問からずれている可能性など）。

正本は構造化した記録で、本文の文字列に印の記号を埋め込まない。理由の ``label`` は言語資源
（``answer_labels.passage_reason_labels``）の文で、画面はそのまま出す。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from rag_parser_core.language import builtin_pack

from rag_engine.generation.answer_labels import answer_labels

PassageState = Literal["unverified", "warning"]
UNVERIFIED: PassageState = "unverified"
WARNING: PassageState = "warning"

# 本文に残す段落の理由の code → 印の状態（契約の reason_codes の kind=passage）。
PASSAGE_REASON_STATES: dict[str, PassageState] = {
    "unsupported": UNVERIFIED,
    "citation_error": UNVERIFIED,
    "quote_unmatched": UNVERIFIED,
    "citation_id_invalid": UNVERIFIED,
    "citation_id_namespace": UNVERIFIED,
    "citation_id_ambiguous": UNVERIFIED,
    "citation_range_missing": UNVERIFIED,
    "citation_range_invalid": UNVERIFIED,
    "citation_range_out_of_bounds": UNVERIFIED,
    "citation_extraction_failed": UNVERIFIED,
    "citation_heading_only": UNVERIFIED,
    "citation_content_unusable": UNVERIFIED,
    "citation_version_mismatch": UNVERIFIED,
    "unassessed": UNVERIFIED,
    "figure_not_read": UNVERIFIED,
    "target_mismatch": WARNING,
    "value_unbound": WARNING,
    # 根拠の表の対応と取り違えた段落を、表の正しい値に直して残した（#3005）。
    "table_corrected": WARNING,
    "item_check": WARNING,
    "superseded": WARNING,
    "after_contradicted_step": WARNING,
    "after_unverified_step": WARNING,
    "summary_unaudited": WARNING,
    "application_unaudited": WARNING,
    # 説明が引いた根拠に無く、業務ガイドの文から来た（資料の記載ではない。#2792）。
    "guide_text": UNVERIFIED,
}
# 本文から外した段落の理由（kind=removed）と、回答全体の警告の理由（kind=answer）。
REMOVED_REASONS = frozenset({"contradicted"})
ANSWER_WARNING_REASONS = frozenset({"guide_check", "no_evidence", "off_goal", "requests", "correction_failed"})
REASON_CODES = frozenset({*PASSAGE_REASON_STATES, *REMOVED_REASONS, *ANSWER_WARNING_REASONS})

# 本文の末尾の節（「根拠と矛盾し除いた内容」「確認が必要な点」）に出す段落の引用と、回答全体の警告の理由の長さの
# 上限（Agent の最終の検証が足す同じ節と同じ。#2738）。
NOTE_TEXT_MAX_CHARS = 80
NOTE_DETAIL_MAX_CHARS = 160


def reason(code: str, detail: str = "") -> dict[str, str]:
    """理由 1 件（code・label・detail）。code は契約の語彙だけ。"""
    if code not in REASON_CODES:
        raise ValueError(f"段落の印の理由の code ではありません: {code}")
    label = answer_labels().passage_reason_labels.get(code)
    if not label:
        label = next(
            (label for label, value in builtin_pack().mapping("answer_labels", "passage_reason_labels").items() if value == code), code
        )
    return {"code": code, "label": label, "detail": " ".join(str(detail or "").split())}


def passage_state(reasons: Iterable[Mapping[str, Any]]) -> PassageState | None:
    """理由から段落の状態。``unverified`` を ``warning`` より優先する。本文に残す理由が無ければ None。"""
    states = {PASSAGE_REASON_STATES.get(str(item.get("code") or "")) for item in reasons}
    if UNVERIFIED in states:
        return UNVERIFIED
    return WARNING if WARNING in states else None


def unique_reasons(reasons: Iterable[Mapping[str, Any]]) -> list[dict[str, str]]:
    """同じ code と detail の理由を 1 つにする（並びは保つ）。"""
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, str]] = []
    for item in reasons:
        key = (str(item.get("code") or ""), str(item.get("detail") or ""))
        if key in seen or not key[0]:
            continue
        seen.add(key)
        result.append({"code": key[0], "label": str(item.get("label") or ""), "detail": key[1]})
    return result


def passage_mark(text: str, reasons: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """本文に残した段落の印（理由が本文に残す理由を含まなければ None）。"""
    kept = [item for item in unique_reasons(reasons) if item["code"] in PASSAGE_REASON_STATES]
    state = passage_state(kept)
    if not text.strip() or state is None:
        return None
    return {"text": text, "state": state, "reasons": kept}


def removed_passage(text: str, reasons: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """本文から外した段落（矛盾の理由が無ければ None）。"""
    kept = [item for item in unique_reasons(reasons) if item["code"] in REMOVED_REASONS]
    if not text.strip() or not kept:
        return None
    return {"text": text, "reasons": kept}


def answer_warning(code: str, detail: str = "") -> dict[str, str]:
    """回答全体の警告 1 件。"""
    if code not in ANSWER_WARNING_REASONS:
        raise ValueError(f"回答全体の警告の code ではありません: {code}")
    return reason(code, detail)


def merge_marks(marks: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """同じ段落（text）の印を 1 つにまとめる（理由を並べ、状態は ``unverified`` を優先）。"""
    by_text: dict[str, list[Mapping[str, Any]]] = {}
    for mark in marks:
        by_text.setdefault(str(mark.get("text") or ""), []).extend(mark.get("reasons") or ())
    return [merged for text, reasons in by_text.items() if (merged := passage_mark(text, reasons)) is not None]


def _short(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _passage_reasons(reasons: Sequence[Mapping[str, Any]]) -> str:
    """段落の理由の label（「・」でつなぐ）。理由の詳細（detail）は本文の節に出さず、記録にだけ残す。"""
    return "・".join(dict.fromkeys(str(item.get("label") or item.get("code") or "") for item in reasons if item))


def _warning_text(item: Mapping[str, Any]) -> str:
    """回答全体の警告の行の文（label と、決定的な検査の文の detail）。"""
    label = str(item.get("label") or item.get("code") or "")
    detail = _short(str(item.get("detail") or ""), NOTE_DETAIL_MAX_CHARS)
    return f"{label}: {detail}" if detail else label


def _passage_quote(text: str) -> str:
    """節に出す段落の短い引用（先頭の箇条書き・番号の印は除く）。"""
    labels = answer_labels()
    body = labels.section_item_line.sub("", text.strip(), count=1)
    return _short(body, NOTE_TEXT_MAX_CHARS)


def removed_section_lines(removed: Sequence[Mapping[str, Any]]) -> list[str]:
    """本文の末尾の「根拠と矛盾し除いた内容」の節の行（外した段落の短い引用と理由の label）。"""
    labels = answer_labels()
    lines = [
        labels.item_bullet + labels.passage_note(_passage_quote(str(item.get("text") or "")), _passage_reasons(item.get("reasons") or ()))
        for item in removed
    ]
    return [labels.removed_section_note, *dict.fromkeys(lines)] if lines else []


def marked_section_lines(marks: Sequence[Mapping[str, Any]], warnings: Sequence[Mapping[str, Any]] = ()) -> list[str]:
    """本文の末尾の「確認が必要な点」の節の行（回答全体の警告と、印の段落の短い引用と理由の label）。"""
    labels = answer_labels()
    lines = [labels.item_bullet + labels.answer_warning(_warning_text(item)) for item in warnings]
    lines += [
        labels.item_bullet + labels.passage_note(_passage_quote(str(item.get("text") or "")), _passage_reasons(item.get("reasons") or ()))
        for item in marks
    ]
    return [labels.marked_section_note, *dict.fromkeys(lines)] if lines else []


def mark_counts(record: Mapping[str, Any]) -> dict[str, int]:
    """回答ごとの数: 矛盾で外した段落・未確認の段落・要注意の段落・回答全体の警告（評価と比較に使う）。"""
    marks = [item for item in record.get("passage_marks") or () if isinstance(item, Mapping)]
    return {
        "removed": sum(1 for item in record.get("removed_passages") or () if isinstance(item, Mapping)),
        "unverified": sum(1 for item in marks if item.get("state") == UNVERIFIED),
        "warning": sum(1 for item in marks if item.get("state") == WARNING),
        "answer_warnings": sum(1 for item in record.get("answer_warnings") or () if isinstance(item, Mapping)),
    }


__all__ = [
    "ANSWER_WARNING_REASONS",
    "PASSAGE_REASON_STATES",
    "REASON_CODES",
    "REMOVED_REASONS",
    "UNVERIFIED",
    "WARNING",
    "PassageState",
    "answer_warning",
    "mark_counts",
    "marked_section_lines",
    "merge_marks",
    "passage_mark",
    "passage_state",
    "reason",
    "removed_passage",
    "removed_section_lines",
    "unique_reasons",
]
