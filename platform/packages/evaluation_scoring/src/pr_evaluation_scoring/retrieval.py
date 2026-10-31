"""検索の指標（正解の文書の順位・取れた文書）。"""

from __future__ import annotations

from collections.abc import Collection, Iterable


def unique_in_order(values: Iterable[str]) -> list[str]:
    """重複を除き、初めて現れた順を保つ。"""
    return list(dict.fromkeys(values))


def reciprocal_rank(retrieved_ids: Iterable[str], relevant_ids: Collection[str]) -> float:
    """最初に現れた正解の文書の順位の逆数（無ければ 0）。"""
    for index, document_id in enumerate(retrieved_ids, start=1):
        if document_id in relevant_ids:
            return 1.0 / index
    return 0.0


def document_hits(retrieved_ids: Iterable[str], relevant_ids: Collection[str]) -> list[str]:
    """取れた正解の文書（取れた順・重複なし）。"""
    return unique_in_order(item for item in retrieved_ids if item in relevant_ids)
