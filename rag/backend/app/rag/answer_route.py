"""回答の経路と理由の記録（#1283、handoff §5 / §13）。

- 回答の経路は、利用者が使う画面・呼び出し元で決まる（このサービスの回答は固定の RAG）。
  質問ごとにモデルで経路を選ぶ振り分けは置かない。
- 回答の対応（AnswerEnvelope の outcome）を経路の理由として、決定的に記録する
  （モデルは呼ばない）。
- 資料だけでは回答を確定できず、現場の値・記録の確認が要る対応（``needs_environment_data``）は
  ``requires_environment_data`` を立てる。続け方（現場のデータを読む道具を使うかどうか）は
  呼び出し元が決める。このサービスは呼び出し元・ほかのサービスを呼ばず、案内もしない。

記録は回答の診断（``diagnostics.answer.route``）に入り、回答の記録（trace）に残る。
Prometheus には理由と現場のデータの要否だけを数える（業務の原文は送らない）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ROUTE_SCHEMA_VERSION = 1
# 経路の理由（回答の対応）。古い回答・回答を作らない検索など、対応が無いときは unknown。
ROUTE_REASONS = frozenset(
    {
        "answered",
        "conditional",
        "needs_clarification",
        "needs_environment_data",
        "needs_human",
        "insufficient_evidence",
        "unknown",
    }
)
# 資料だけでは回答を確定できず、現場の値・記録の確認が要る対応。
ENVIRONMENT_DATA_OUTCOMES = frozenset({"needs_environment_data"})


def answer_route(answer_diagnostics: Mapping[str, Any]) -> dict[str, Any]:
    """回答の診断から経路の記録を作る（決定的。モデルは呼ばない）。"""
    envelope = answer_diagnostics.get("envelope")
    envelope = envelope if isinstance(envelope, Mapping) else {}
    outcome = str(envelope.get("outcome") or answer_diagnostics.get("outcome") or "")
    reason = outcome if outcome in ROUTE_REASONS else "unknown"
    signals = [
        str(value) for value in envelope.get("handoff_reasons") or () if isinstance(value, str)
    ]
    return {
        "schema_version": ROUTE_SCHEMA_VERSION,
        "path": "rag",
        "reason": reason,
        # outcome を決めた手がかり（AnswerEnvelope の handoff_reasons）。経路の評価に使う。
        "signals": signals,
        "requires_environment_data": reason in ENVIRONMENT_DATA_OUTCOMES,
    }


__all__ = ["ENVIRONMENT_DATA_OUTCOMES", "ROUTE_REASONS", "ROUTE_SCHEMA_VERSION", "answer_route"]
