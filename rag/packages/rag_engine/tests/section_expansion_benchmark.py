"""合成の5問で読取方針を比較する。実モデルの回答品質・実サービスの待ち時間は測らない。

実行: python -m tests.section_expansion_benchmark
"""

from __future__ import annotations

import json
from statistics import mean
from time import perf_counter

from rag_engine.generation import section_expansion as sections
from tests.test_section_expansion import (
    SectionStub,
    _attempt,
    _child_ids,
    _expand,
    _seed_context,
    _settings,
)

CASES = (
    ("owner", ("c31a",), ({"c31a"}, {"c31b"}), 128000),
    ("parent", ("c31a",), ({"c31a", "c31b"}, {"c32a", "c33b"}), 128000),
    ("grandparent", ("c31a",), ({"c31a"}, {"c4a", "c4b"}), 128000),
    ("three_owners", ("c31a", "c32a", "c4a"), ({"c31b", "c32b"}, {"c4a", "c4b"}), 128000),
    ("budget", ("c31a",), ({"c31a"}, {"c32a", "c33b"}), 1200),
)
POLICIES = ("none", "owner", "parent", "grandparent", "adaptive")


def benchmark() -> dict:
    rows = []
    for name, seeds, steps, tokens in CASES:
        required = set.union(*steps)
        for policy in POLICIES:
            context = _seed_context(*seeds)
            store = SectionStub()
            calls = []

            def grade(candidate, level, calls=calls, required=required, policy=policy):
                calls.append(level)
                present = set(_child_ids(candidate))
                enough = required <= present if policy == "adaptive" else level >= {"parent": 2, "grandparent": 3}[policy]
                return _attempt(sufficient=enough, missing=() if enough else ("requested_result",))

            start = perf_counter()
            if policy == "none":
                result = None
                accepted = True  # 章節補完前に足りると自己申告する評価器を意図的に再現する。
            else:
                result = _expand(
                    context,
                    _settings(answer_max_input_tokens=tokens),
                    None if policy == "owner" else grade,
                    store,
                    last=_attempt(sufficient=True),
                )
                context = result.context
                accepted = result.stop_reason == sections.STOP_SUFFICIENT
            elapsed = (perf_counter() - start) * 1000
            present = set(_child_ids(context))
            rows.append(
                {
                    "case": name,
                    "policy": policy,
                    "evidence_recall": len(present & required) / len(required),
                    "step_fulfillment": sum(step <= present for step in steps) / len(steps),
                    "false_sufficient": bool(accepted and not required <= present),
                    "extra_chunks": len(present - required),
                    "input_tokens_estimate": sections.context_tokens(context),
                    "elapsed_ms": round(elapsed, 3),
                    "read_calls": len(store.read_calls),
                    "judge_calls": len(calls),
                    "minimum_scope_complete": bool(result and result.trace["minimum_scope_complete"]),
                    "unread": len(result.unread) if result else 0,
                    "source_ids": sorted(present),
                }
            )
    summary = {}
    for policy in POLICIES:
        group = [row for row in rows if row["policy"] == policy]
        summary[policy] = {
            key: round(mean(row[key] for row in group), 4)
            for key in (
                "evidence_recall",
                "step_fulfillment",
                "false_sufficient",
                "extra_chunks",
                "input_tokens_estimate",
                "elapsed_ms",
                "read_calls",
                "judge_calls",
            )
        }
    return {"kind": "deterministic_synthetic", "cases": len(CASES), "summary": summary, "rows": rows}


if __name__ == "__main__":
    print(json.dumps(benchmark(), ensure_ascii=False, indent=2))
