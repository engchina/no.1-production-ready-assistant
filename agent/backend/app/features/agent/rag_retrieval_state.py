"""RAG の根拠のツールの結果の、縮退・根拠 0 件・表の検査の可否の読み方（#3546。#3262 の残り）。

RAG の MCP（`rag_search` / `rag_retrieve_evidence`）は、検索・回答の任意の補助の処理が失敗して
縮退したこと（`degraded_steps`。処理の種類と回数）、安全チェックを完了できず止めたこと
（`guardrail_degraded`）、根拠 0 件のときに 0 件になった段（`zero_evidence.stage`）、根拠ごとに
表の行・列の対応の検査が本文を読めるか（`table_binding`）を返す。組み込み Runtime はこれを次に
使う（どれも RAG の結果を書き換えない。記録する step の結果は RAG のまま）。

- モデルへのツールの結果の案内（`retrieval_notice`）: 根拠が 0 件で、安全チェックが完了して
  いない・補助の処理が失敗したときは、0 件が「資料に記載が無い」ことを意味しないと伝える
  （状態で変わる案内はツールの結果に置く。docs/agents/prompts.md）。
- 回答の対応（`answer_outcome`）: 縮退・安全チェックの未完了で 0 件になった `rag_search` を、
  資料に答えが無いと結論した回答（`absence_conclusion`）の根拠に数えない。縮退した検索を使った
  こと・表の対応の検査が読めない根拠で裏付けたことを、記録だけのシグナルに残す。
- 観測（`agent_get_run_observation`）: ツールの呼び出しごとの縮退・0 件の段と、根拠ごとの
  `table_binding` を写す（`run_observation`）。

ここは読み方と案内の文だけを持つ（呼び出しは `builtin_runtime`・`answer_outcome`・
`run_observation`）。知らない値・形の合わない値は無いものとして扱う（精密に見せない）。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

JsonObject = dict[str, Any]

# RAG の `ZeroEvidence.stage` の語彙（契約 rag-tools.json）。
ZERO_EVIDENCE_STAGES = frozenset({"retrieval", "filter", "evidence_check", "context"})
# RAG の `RagEvidence.table_binding` の語彙。null は表の無い根拠。
TABLE_BINDING_CHECKABLE = "checkable"
TABLE_BINDING_NOT_CHECKABLE = "not_checkable"
TABLE_BINDINGS = frozenset({TABLE_BINDING_CHECKABLE, TABLE_BINDING_NOT_CHECKABLE})

# モデルへの案内の理由。
NOTICE_GUARDRAIL_DEGRADED = "guardrail_degraded"
NOTICE_DEGRADED_STEPS = "degraded_steps"
GUARDRAIL_DEGRADED_HINT = (
    "安全チェックを完了できなかったため、この呼び出しは資料を調べていません。資料に記載が無い"
    "とは答えず、資料を確かめられなかったことを利用者に伝えてください。"
)
DEGRADED_ZERO_EVIDENCE_HINT = (
    "根拠は 0 件ですが、補助の処理（degraded_steps）が失敗したため、当たる資料を読めなかった"
    "可能性があります。資料に記載が無いとは断定せず、確かめられなかった点として示してください。"
)


def degraded_steps(output: Mapping[str, Any]) -> list[JsonObject]:
    """縮退した任意の補助の処理（`kind`・`count`。形の合わない項目は除く）。"""
    raw = output.get("degraded_steps")
    steps: list[JsonObject] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        kind, count = item.get("kind"), item.get("count")
        if (
            isinstance(kind, str)
            and kind
            and isinstance(count, int)
            and not isinstance(count, bool)
            and count > 0
        ):
            steps.append({"kind": kind, "count": count})
    return steps


def guardrail_degraded(output: Mapping[str, Any]) -> bool:
    """安全チェックを完了できず止めたか。"""
    return output.get("guardrail_degraded") is True


def zero_evidence_stage(output: Mapping[str, Any]) -> str | None:
    """根拠 0 件のときに 0 件になった段（無い・知らない段は None）。"""
    raw = output.get("zero_evidence")
    stage = raw.get("stage") if isinstance(raw, Mapping) else None
    return stage if isinstance(stage, str) and stage in ZERO_EVIDENCE_STAGES else None


def table_binding(item: Mapping[str, Any]) -> str | None:
    """根拠の表の行・列の対応の検査が本文を読めるか（無い・知らない値は None）。"""
    value = item.get("table_binding")
    return value if isinstance(value, str) and value in TABLE_BINDINGS else None


def _no_evidence(output: Mapping[str, Any]) -> bool:
    evidence = output.get("evidence")
    return not (isinstance(evidence, list) and evidence)


def degraded_absence(output: Mapping[str, Any]) -> bool:
    """根拠が 0 件で、それが資料に記載が無いことを示さない（安全チェックの未完了・縮退）か。"""
    return _no_evidence(output) and (guardrail_degraded(output) or bool(degraded_steps(output)))


def degraded_retrieval(output: Mapping[str, Any]) -> bool:
    """縮退した検索の結果か（根拠の有無に依らない。安全チェックの未完了も含む）。"""
    return guardrail_degraded(output) or bool(degraded_steps(output))


def retrieval_notice(output: Mapping[str, Any]) -> JsonObject | None:
    """根拠 0 件が資料の不在を意味しないときの、モデルへの案内（無ければ None）。"""
    if not degraded_absence(output):
        return None
    if guardrail_degraded(output):
        return {"reason": NOTICE_GUARDRAIL_DEGRADED, "next_step": GUARDRAIL_DEGRADED_HINT}
    return {"reason": NOTICE_DEGRADED_STEPS, "next_step": DEGRADED_ZERO_EVIDENCE_HINT}


__all__ = [
    "DEGRADED_ZERO_EVIDENCE_HINT",
    "GUARDRAIL_DEGRADED_HINT",
    "NOTICE_DEGRADED_STEPS",
    "NOTICE_GUARDRAIL_DEGRADED",
    "TABLE_BINDINGS",
    "TABLE_BINDING_CHECKABLE",
    "TABLE_BINDING_NOT_CHECKABLE",
    "ZERO_EVIDENCE_STAGES",
    "degraded_absence",
    "degraded_retrieval",
    "degraded_steps",
    "guardrail_degraded",
    "retrieval_notice",
    "table_binding",
    "zero_evidence_stage",
]
