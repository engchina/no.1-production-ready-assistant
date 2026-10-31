"""質問の対象語から作る決定的な全文検索クエリ（#730）。

質問の複合語（「出荷基準年月」）は分かち書きで単字（年・月）に割れ、Oracle Text の OR 検索では
どの chunk にも一致して重みが薄まる。資料が対象を別の形（「基準月修正」）で書いていると、向量検索でも
上位に来ない。対象語の部分語（分かち書きの連続窓と、単字を 1 つ抜いた窓）を 1 語 1 クエリの
フレーズ検索にし、各クエリの上位 3 件が候補に確保される（#669）ことで、資料側の言い方に届かせる。
LLM は使わない。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from rag_engine.retrieval.task_contract import generic_target_words, task_contract
from rag_engine.retrieval.text_search_tokenizer import escape_oracle_text_term

# 1 質問あたりのクエリ数の上限と、部分語として使う最短の文字数。
MAX_TARGET_PHRASES = 6
MIN_PHRASE_CHARS = 2


def target_phrases(question: str, tokenize: Callable[[str], Sequence[str]]) -> list[str]:
    """質問の対象語（`action_targets` / `business_objects`）とその部分語を返す。

    部分語は分かち書きの連続窓（2 語以上）と、単字の語を 1 つ抜いた窓。単字だけの窓（「年月」）と
    総称語は除く。順序は対象語そのもの → 語数の少ない窓（略称に近い）→ 長い窓。上限 `MAX_TARGET_PHRASES`。
    """
    contract = task_contract(question)
    targets = [
        t
        for t in dict.fromkeys([*contract.get("action_targets", ()), *contract.get("business_objects", ())])
        if len(t) >= MIN_PHRASE_CHARS and t not in generic_target_words()
    ]
    phrases: list[str] = []
    for target in targets:
        tokens = [t for t in tokenize(target) if t] or [target]
        candidates: list[tuple[int, str]] = [(0, target)]
        compact_target = re.sub(r"\s+", "", target)
        for size in range(2, len(tokens) + 1):
            for start in range(0, len(tokens) - size + 1):
                window = tokens[start : start + size]
                joined = "".join(window)
                # tokenizer の正規化形（「見積り」）でつないだ窓は対象語に現れない。表層に一致する窓だけ使う。
                if all(len(t) == 1 for t in window) or joined not in compact_target:
                    continue
                candidates.append((size, joined))
                # 単字の語（「年」）を 1 つ抜いた窓。「基準年月」→「基準月」のような資料側の略し方に届く。
                for drop in range(1, size - 1):
                    if len(window[drop]) == 1:
                        candidates.append((size, "".join(window[:drop] + window[drop + 1 :])))
        for _, phrase in sorted(candidates, key=lambda item: item[0]):
            phrase = re.sub(r"\s+", "", phrase)
            if len(phrase) >= MIN_PHRASE_CHARS and phrase not in generic_target_words() and phrase not in phrases:
                phrases.append(phrase)
    return phrases[:MAX_TARGET_PHRASES]


def target_text_queries(question: str, tokenize: Callable[[str], Sequence[str]]) -> list[str]:
    """`target_phrases` を 1 語 1 クエリの Oracle Text フレーズ検索式（`{基準月}`）にする。"""
    queries = [escape_oracle_text_term(phrase) for phrase in target_phrases(question, tokenize)]
    return [q for q in dict.fromkeys(queries) if q]
