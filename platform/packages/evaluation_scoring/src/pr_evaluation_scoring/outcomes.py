"""回答の対応（outcome）の語彙と、回答・確認・保留の判断（response decision）。

対応の語彙は RAG の `rag_search.outcome` と Agent の `RunOutput.outcome` の公開の契約
（`platform/contracts/mcp/rag-tools.json` / `agent-tools.json`）と同じ 6 つ。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal

# answered: 資料にもとづいて答えた / conditional: 条件・不足を示して答えた /
# needs_clarification: 利用者に条件を確かめた / needs_environment_data: 現場の値・記録が要る /
# needs_human: 人への引き継ぎ / insufficient_evidence: 資料から答えられない（拒答）。
OUTCOMES: tuple[str, ...] = (
    "answered",
    "conditional",
    "needs_clarification",
    "needs_environment_data",
    "needs_human",
    "insufficient_evidence",
)
# 検索をしない回答の対応（引用が無ければ、検索の指標の対象外）。
NO_RETRIEVAL_OUTCOMES: frozenset[str] = frozenset({"needs_clarification", "needs_human"})

Decision = Literal["answer", "clarify", "hold"]
DECISIONS: tuple[Decision, ...] = ("answer", "clarify", "hold")


def response_decision(outcome: str) -> Decision:
    """対応を、回答（answer）・確認（clarify）・保留（hold）の判断にする。"""
    if outcome in {"answered", "conditional"}:
        return "answer"
    return "clarify" if outcome == "needs_clarification" else "hold"


def expected_decisions(
    expected_response_decision: str | None, expected_outcomes: Sequence[str]
) -> set[str]:
    """期待の判断（明示があればそれ、無ければ期待する対応の判断）。期待が無ければ空。"""
    if expected_response_decision:
        return {expected_response_decision}
    return {response_decision(item) for item in expected_outcomes}


def skips_retrieval(outcome: str | None, *, has_citations: bool) -> bool:
    """確認の質問・人への引き継ぎで引用の無い回答か（検索の指標の対象外）。"""
    return outcome in NO_RETRIEVAL_OUTCOMES and not has_citations


def all_correct(values: Iterable[bool | None]) -> bool | None:
    """往復ごとの判断の正しさをまとめる（3 値の論理積）。

    1 つでも誤り（False）があれば False、誤りが無く測れない往復（None）があれば None、
    すべて正しければ True。測る往復が無ければ None。
    """
    items = list(values)
    if not items:
        return None
    if any(item is False for item in items):
        return False
    if any(item is None for item in items):
        return None
    return True
