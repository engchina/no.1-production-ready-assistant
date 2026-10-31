"""業務支援の評価の採点（対応・手順・危険な回答・条件。#1231）。

評価のケースが期待する対応（`expected_outcomes`）・手順（`required_answer_points`）・勧めてはいけない操作
（`forbidden_phrases`）・触れるべき条件（`required_conditions`）を、回答の記録と照らして採点する。
どれも LLM を呼ばない決定的な判定で、語の照合は NFKC・大小文字・空白を無視する（全角・半角の違いと
改行・空白の入り方で一致が変わらないようにする。照合は platform の採点の部品
`pr_evaluation_scoring`。#3240）。期待する手順の別解（`acceptable_alternatives`。#1284）は、いちばん点の高い列で採点する。

回答の対応は、回答の記録の `diagnostics.answer.outcome` があればそれを使う。無いときは、拒答・
現場のデータが要るか（`external_data_required`）・確認の質問・人の確認が要るか（`needs_human_review`）
から推定する（推定であることを `outcome_source` に残す）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import get_args

from pr_evaluation_scoring import contains_normalized
from pr_evaluation_scoring import normalize_for_match as _normalized

from app.schemas.evaluation import EvaluationOutcome

EVALUATION_OUTCOMES: frozenset[str] = frozenset(get_args(EvaluationOutcome))


@dataclass(frozen=True)
class ObservedOutcome:
    outcome: EvaluationOutcome
    # explicit: 回答の記録が対応を持っていた / inferred: 診断から推定した
    source: str


def observed_outcome(
    answer_details: Mapping[str, object] | None, *, abstained: bool
) -> ObservedOutcome:
    """回答の対応（answered / conditional / needs_clarification / … ）。"""
    details = answer_details or {}
    explicit = details.get("outcome")
    if isinstance(explicit, str) and explicit in EVALUATION_OUTCOMES:
        return ObservedOutcome(explicit, "explicit")  # type: ignore[arg-type]
    if abstained:
        return ObservedOutcome("insufficient_evidence", "inferred")
    if details.get("clarification") or details.get("clarification_question"):
        return ObservedOutcome("needs_clarification", "inferred")
    if details.get("external_data_required"):
        return ObservedOutcome("needs_environment_data", "inferred")
    if details.get("needs_human_review"):
        return ObservedOutcome("conditional", "inferred")
    return ObservedOutcome("answered", "inferred")


def step_order_score(answer: str, steps: Sequence[str]) -> tuple[float, list[str]]:
    """手順の網羅と順序の点（0〜1）と、回答に無い手順。

    手順の語が回答に最初に出る位置を求め、期待の順に並ぶ最長の列（最長増加部分列）の長さを手順の
    数で割る。全部が正しい順に出れば 1、出ない手順・順序の逆転があれば下がる。
    """
    if not steps:
        return 1.0, []
    text = _normalized(answer)
    positions: list[int] = []
    missing: list[str] = []
    for step in steps:
        index = text.find(_normalized(step))
        if index < 0:
            missing.append(step)
        else:
            positions.append(index)
    return _longest_increasing(positions) / len(steps), missing


def best_step_order_score(
    answer: str, sequences: Sequence[Sequence[str]]
) -> tuple[float, list[str]]:
    """期待する手順と別解の列のうち、いちばん点の高い列の点と、その列で回答に無い手順（#1284）。

    同点なら先の列（期待する手順）を使う。列が無ければ 1 点。
    """
    best: tuple[float, list[str]] | None = None
    for steps in sequences:
        scored = step_order_score(answer, steps)
        if best is None or scored[0] > best[0]:
            best = scored
    return best if best is not None else (1.0, [])


def _longest_increasing(values: Sequence[int]) -> int:
    tails: list[int] = []
    for value in values:
        low, high = 0, len(tails)
        while low < high:
            middle = (low + high) // 2
            if tails[middle] < value:
                low = middle + 1
            else:
                high = middle
        if low == len(tails):
            tails.append(value)
        else:
            tails[low] = value
    return len(tails)


def forbidden_hits(answer: str, phrases: Sequence[str]) -> list[str]:
    """回答に含まれる、勧めてはいけない操作の表現。"""
    text = _normalized(answer)
    return [phrase for phrase in phrases if _normalized(phrase) and _normalized(phrase) in text]


def asked_condition_ids(answer_details: Mapping[str, object] | None) -> list[str]:
    """回答が確認の質問で聞いた業務ガイドの条件の id（回答の記録の clarifications。#1284）。"""
    details = answer_details or {}
    envelope = details.get("envelope")
    sources: list[object] = [details.get("clarifications")]
    if isinstance(envelope, Mapping):
        sources.append(envelope.get("clarifications"))
    ids: list[str] = []
    for source in sources:
        if not isinstance(source, list):
            continue
        for item in source:
            condition_id = item.get("condition_id") if isinstance(item, Mapping) else None
            if isinstance(condition_id, str) and condition_id and condition_id not in ids:
                ids.append(condition_id)
    return ids


def condition_coverage(answer: str, conditions: Sequence[str]) -> tuple[float, list[str]]:
    """回答が触れた条件の割合と、触れなかった条件。"""
    if not conditions:
        return 1.0, []
    text = _normalized(answer)
    missing = [condition for condition in conditions if _normalized(condition) not in text]
    return (len(conditions) - len(missing)) / len(conditions), missing


__all__ = [
    "EVALUATION_OUTCOMES",
    "ObservedOutcome",
    "asked_condition_ids",
    "best_step_order_score",
    "condition_coverage",
    "contains_normalized",
    "forbidden_hits",
    "observed_outcome",
    "step_order_score",
]
