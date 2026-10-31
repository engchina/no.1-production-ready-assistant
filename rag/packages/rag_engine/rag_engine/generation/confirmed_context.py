"""生成・監査へ同じ確認済み前提と出所を渡す。"""

from collections.abc import Sequence


def confirmed_premise_records(premises: Sequence[tuple[str, str]], sources: Sequence[tuple[str, str, str]]) -> list[dict[str, str]]:
    """既存の構造化確認を投影する。自由文や不明・仮定から事実を作らない。"""
    origins = {(question, answer): source for question, answer, source in sources}
    return [
        {"question": question, "answer": answer, **({"source": origins[question, answer]} if (question, answer) in origins else {})}
        for question, answer in premises
        if (question, answer) not in origins or origins[question, answer] in {"user", "question"}
    ]
