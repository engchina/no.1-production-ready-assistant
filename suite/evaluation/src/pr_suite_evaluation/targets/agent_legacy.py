"""Agent の旧形式の回答の扱い（#1305・#1456・#2738。#3240 で RAG から移した）。

- 回答の節: Agent の最終の検証が回答の末尾に足す節（今の見出しは公開の契約
  `platform/contracts/answer-passages/unverified-section.json` の headings。#2738 より前の Run は
  「資料で確認できず除いた内容」）は、外した段落・印の段落の引用と理由で、回答の本文ではない。
- 対応の推定: `outcome` の無い完了の Run（#1305 より前の Run）は、利用者が
  `--infer-missing-outcome` を選んだときだけ回答の文から推定する（推定は
  `outcome_source=inferred` と残す）。既定では推定せず、対応を使う指標を「評価不能」にする。
  新しく流す Run は必ず `outcome` を持つ（Agent の契約）。
"""

from __future__ import annotations

import re

# 今の Agent の最終の検証が足す節の見出し（公開の契約の headings。tests/test_agent_legacy.py が
# 契約の fixture と比べる）。
AGENT_SECTION_HEADINGS: tuple[str, ...] = ("根拠と矛盾し除いた内容", "確認が必要な点")
# #2738 より前の Agent の最終の検証が足していた節の見出し（保存済みの Run のため）。
LEGACY_UNVERIFIED_HEADING = "資料で確認できず除いた内容"
SYSTEM_SECTION_HEADINGS: frozenset[str] = frozenset(
    {*AGENT_SECTION_HEADINGS, LEGACY_UNVERIFIED_HEADING}
)
# Agent の最終の検証が回答の末尾に足す定型の注記（推定で最後の行を見るときに除く。#1305）。
AGENT_NOTICES: frozenset[str] = frozenset(
    {
        "この回答は資料との照合を終えられず、本質問の条件への該当は確認できていません。",
        "この回答は資料の根拠を使っておらず、資料と照らし合わせて確かめていません。",
    }
)
_QUESTION_ENDINGS = ("？", "?")
# 見出しの行（Markdown の見出し・太字だけの行。後ろの「：」は許す）。
_HEADING_LINE = re.compile(r"(?:#{1,6}\s+(?P<md>.+?)|\*\*(?P<bold>[^*\n]+)\*\*)\s*[:：]?")


def _heading_title(line: str) -> str | None:
    match = _HEADING_LINE.fullmatch(line.strip())
    if match is None:
        return None
    return (match.group("md") or match.group("bold") or "").strip().strip("*").strip()


def answer_body(answer: str) -> str:
    """Agent の最終の検証が足した節（見出しから次の見出しの前まで）を除いた回答の本文。"""
    kept: list[str] = []
    inside = False
    for line in answer.splitlines():
        title = _heading_title(line)
        if title is not None:
            inside = title in SYSTEM_SECTION_HEADINGS
        if not inside:
            kept.append(line)
    return "\n".join(kept)


def asks_question(answer: str) -> bool:
    """回答の本文の最後の行（定型の注記を除く）が問いか。"""
    lines = [
        line.strip()
        for line in answer_body(answer).splitlines()
        if line.strip() and line.strip() not in AGENT_NOTICES
    ]
    return bool(lines) and lines[-1].endswith(_QUESTION_ENDINGS)


def infer_outcome(answer: str, *, has_citations: bool) -> str:
    """`outcome` の無い完了の Run の対応の推定（旧形式の互換。#1305）。

    最後の行が問いなら確認の質問、本文か根拠の無い回答は拒答、ほかは回答とする（段 2 までの
    比較評価の CLI と同じ規則）。
    """
    if asks_question(answer):
        return "needs_clarification"
    if not answer.strip() or not has_citations:
        return "insufficient_evidence"
    return "answered"
