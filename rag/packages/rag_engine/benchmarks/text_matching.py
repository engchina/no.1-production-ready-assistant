"""同一入力で移行前の選択・順位と比較する（python -m benchmarks.text_matching）。"""

from __future__ import annotations

import json
import math
import re
import statistics
import time
import tracemalloc
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial

from rag_engine.knowledge.rule_matching import rank_rules
from rag_engine.knowledge.runtime_knowledge import RuntimeRule, _tokens


def _legacy_key(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]+", " ", text)).strip()


def _legacy_hit(question: str, label: str) -> bool:
    text, key = _legacy_key(question), _legacy_key(label)
    if not text or not key:
        return False
    if re.fullmatch(r"[0-9a-z_]+", key):
        return re.search(rf"(?<![0-9a-z_]){re.escape(key)}(?![0-9a-z_])", text) is not None
    return key in text


def legacy_rules(question: str, rules: Sequence[RuntimeRule], limit: int) -> tuple[RuntimeRule, ...]:
    """移行前のルール選択の基線。重み・上限・同点順を固定して比較する。"""
    scored = []
    for index, rule in enumerate(rules):
        if not rule.active:
            continue
        if "pinned" in rule.tags:
            scored.append((1000, index, rule))
            continue
        score = sum(3 for label in rule.labels() if _legacy_hit(question, label))
        if score and set(_tokens(question)) & set(_tokens(rule.content)):
            score += 1
        if score:
            scored.append((score, index, rule))
    return tuple(rule for _, _, rule in sorted(scored, key=lambda item: (-item[0], item[1]))[: max(1, limit)])


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    rules: tuple[RuntimeRule, ...]
    expected: tuple[str, ...]
    limit: int = 6


def cases() -> tuple[Case, ...]:
    simple = (
        RuntimeRule("a", "alpha", ("remove", "delete"), "合成の指示"),
        RuntimeRule("b", "beta", ("beta",), "合成の指示"),
    )
    return (
        Case("latin-word", "alphabet", simple, ()),
        Case("aliases", "alpha remove delete", simple, ("a",)),
        Case("width-case-space", " ＡＬＰＨＡ\tremove ", simple, ("a",)),
        Case("negative-limit", "not alpha", simple, ("a",)),
        Case("comparison-limit", "alpha or beta?", simple, ("a", "b")),
        Case("topic-change-limit", "stop alpha; now beta", simple, ("a", "b")),
        Case("japanese", "項目を削除します", (RuntimeRule("ja", "合成 A", ("削除",)),), ("ja",)),
        Case("chinese", "请删除项目", (RuntimeRule("zh", "合成 B", ("删除",)),), ("zh",)),
        Case("normalization-dedup", "alpha", (RuntimeRule("x", "alpha", ("ＡＬＰＨＡ", "alpha")),), ("x",)),
        Case("inactive", "alpha", (RuntimeRule("x", "alpha", status="deprecated"),), ()),
        Case("pinned", "alpha", (*simple, RuntimeRule("p", "合成 P", tags=("pinned",))), ("p", "a")),
        Case("large-ties", "alpha", tuple(RuntimeRule(str(i), "alpha", (f"term_{i}",)) for i in range(1000)), tuple(map(str, range(6)))),
        Case(
            "large-label-set",
            "alpha",
            tuple(RuntimeRule(str(i), "alpha", tuple(f"term_{i}_{j}" for j in range(32))) for i in range(1000)),
            tuple(map(str, range(6))),
        ),
    )


def _measure(run: Callable[[], object], repeats: int = 9) -> dict[str, float]:
    run()
    durations = []
    for _ in range(repeats):
        start = time.perf_counter()
        run()
        durations.append((time.perf_counter() - start) * 1000)
    tracemalloc.start()
    run()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "p50_ms": round(statistics.median(durations), 3),
        "p95_ms": round(sorted(durations)[math.ceil(0.95 * repeats) - 1], 3),
        "peak_kib": round(peak / 1024, 1),
    }


def main() -> None:
    output = []
    for case in cases():
        baseline = partial(legacy_rules, case.question, case.rules, case.limit)
        current = partial(rank_rules, case.question, case.rules, case.limit, tokens=_tokens)
        old = tuple(rule.rule_id for rule in baseline())
        new = tuple(rule.rule_id for rule in current().selected)
        output.append(
            {
                "case": case.name,
                "baseline": _measure(baseline),
                "with_explanations": _measure(current),
                "same_ranking": old == new == case.expected,
                "false_positive": len(set(new) - set(case.expected)),
                "missed": len(set(case.expected) - set(new)),
            }
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
