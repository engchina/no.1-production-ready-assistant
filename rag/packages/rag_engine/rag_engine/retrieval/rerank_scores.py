"""候補の rerank の点数を読む（選択・並べ替えの主キー。#3504）。

rerank の点数がある候補は、それを選択・並べ替えの主キーにし、件数・RRF・語の一致・文書の位置は同点の順か、
rerank が動かなかったときの代わりにだけ使う。点数は `metadata["rerank"]` に rerank の工程が書く:

- ``relevance_score``: 並べ替えの点数（原質問と検索文ごとの rerank の最高値。#2161）
- ``question_relevance_score``: 原質問だけとの点数（#3504。無い記録は ``relevance_score`` と同じ）

例外（利用者の判断 2026-10-26）: 条の目録で選んだ条（#2254）は、rerank 最優先にせず元の規則のまま扱う
（目録の文書の順・条の選択・関連度に関わらず context の枠の外に足すこと）。rerank 最優先の工程は
``keeps_original_order`` の候補を並べ替え・切りの対象にしない。例外を切り替えるときはこの関数だけを変える。
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from rag_engine.retrieval.article_catalog import is_catalog_article


def _score(record: Any, key: str) -> float | None:
    metadata = getattr(record, "metadata", None)
    rerank = metadata.get("rerank") if isinstance(metadata, dict) else None
    value = rerank.get(key) if isinstance(rerank, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return float(value)


def rerank_score(record: Any) -> float | None:
    """並べ替えの点数。rerank していない候補は None。"""
    return _score(record, "relevance_score")


def question_rerank_score(record: Any) -> float | None:
    """原質問だけとの rerank の点数。記録が無ければ並べ替えの点数（検索文が無いときは同じ値）。"""
    score = _score(record, "question_relevance_score")
    return score if score is not None else rerank_score(record)


def best_rerank_score(records: Iterable[Any]) -> float | None:
    """候補の rerank の点数の最高値（MaxP）。どれも rerank していなければ None。"""
    scores = [score for score in (rerank_score(record) for record in records) if score is not None]
    return max(scores) if scores else None


def keeps_original_order(record: Any) -> bool:
    """rerank 最優先の例外の候補か（条の目録で選んだ条。#2254。利用者の判断 2026-10-26）。"""
    return is_catalog_article(record)


def rerank_first_key(score: float | None) -> tuple[int, float]:
    """昇順の sort key: rerank の点数を持つものを点数の高い順に先に、持たないものを後に置く。"""
    return (0, -score) if score is not None else (1, 0.0)
