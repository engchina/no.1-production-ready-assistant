"""採点の入力（Target の観測を、Target に依らない形にしたもの。#3240）。

Target の adapter が保存した観測（`CaseObservation`）から作り、Evaluator はこれだけを読む。
値の分からない項目は None のまま渡し、Evaluator が「評価不能」とその理由を記録する（推測の値で
埋めない。推定するときは `outcome_source` に推定と残す）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Citation:
    """回答の根拠の chunk（場所は RAG の `EvidenceLocator` と同じ名前の Mapping）。"""

    document_id: str
    chunk_id: str
    text: str
    # 本文を省かれた（応答の予算に入らなかった）根拠。本文の条件を照合できない。
    text_omitted: bool = False
    locator: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnView:
    """1 往復の回答。"""

    turn: int
    trace_id: str
    elapsed_ms: float
    # 回答の対応（OUTCOMES の値）。分からなければ None。
    outcome: str | None
    # 対応の出所（Target が付けた値・確認・推定）。分からなければ None。
    outcome_source: str | None
    citations: tuple[Citation, ...] = ()
    # 見る人の今の権限で見られないため、観測から除かれた根拠の数。
    withheld_evidence_count: int = 0
    # 回答の本文（Target がシステムとして足した節を除いた本文。LLM による判定の入力。#3418）。
    answer: str = ""


@dataclass(frozen=True)
class CaseFailure:
    """回答まで進めなかったケース（採点せず、ケースの失敗にする）。"""

    error_type: str
    error_message: str
    error_stage: str | None
    trace_id: str


@dataclass(frozen=True)
class CaseView:
    """1 ケースの採点の入力。"""

    case_id: str
    turns: tuple[TurnView, ...]
    failure: CaseFailure | None = None
    elapsed_ms: float = 0.0
