"""回答の最終の検証の決定的な検査（#1276、handoff §12 の 1・4・5）。モデルは呼ばない。

主張の監査（モデル 1 回。`answer_validation`）と並べて、呼び出し側が任意で渡した要求を確かめる。
AnswerEnvelope の要求ごとの充足（addressed / partial / missing / unknown）のうち、partial・missing
の要求を報告する。回答の本文に不足として示していれば warning、黙って落ちていれば error。
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from rag_engine.generation.answer_labels import answer_labels

FindingCheck = Literal["requests"]
FindingSeverity = Literal["error", "warning"]


def gap_section_titles() -> tuple[str, ...]:
    """回答の本文で不足を示す節の見出し（rag_engine の言語資源の answer_labels。#2040）。"""
    labels = answer_labels()
    return (labels.gaps_section, labels.unverified_section, labels.deadline_section)


@dataclass(frozen=True)
class AnswerFinding:
    check: FindingCheck
    code: str
    severity: FindingSeverity
    message: str
    request_id: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


def _normalized(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def check_requests(
    answer: str, requests: Sequence[Mapping[str, str]], gaps: Sequence[str] = ()
) -> list[AnswerFinding]:
    """要求ごとの充足。partial / missing を報告し、本文で不足を示していなければ error にする。"""
    normalized = _normalized(answer)
    section = any(_normalized(title) in normalized for title in gap_section_titles())
    gap_shown = any(_normalized(gap) and _normalized(gap) in normalized for gap in gaps)
    findings: list[AnswerFinding] = []
    for request in requests:
        request_id = str(request.get("id") or "")
        text = " ".join(str(request.get("text") or "").split())
        status = request.get("status")
        label = f"要求「{text}」" if text else f"要求 {request_id}"
        if status == "unknown":
            findings.append(
                AnswerFinding(
                    "requests",
                    "request_unverified",
                    "warning",
                    f"{label}に答えたかは確かめられていません。",
                    request_id=request_id,
                )
            )
            continue
        if status not in {"partial", "missing"}:
            continue
        disclosed = section or gap_shown or bool(text and _normalized(text) in normalized)
        state = "一部しか答えていません" if status == "partial" else "答えていません"
        findings.append(
            AnswerFinding(
                "requests",
                f"request_{status}",
                "warning" if disclosed else "error",
                f"{label}に{state}"
                + (
                    "（回答に不足として示しています）。"
                    if disclosed
                    else "。不足も示していません。"
                ),
                request_id=request_id,
            )
        )
    return findings


__all__ = [
    "AnswerFinding",
    "check_requests",
]
