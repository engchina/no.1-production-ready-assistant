"""原質問の要求 ID と、明示された未回答の追加検索を追跡する。候補取得は回答可能性と区別する。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from time import monotonic
from typing import Any

from rag_engine.dependencies import report_degraded
from rag_engine.retrieval.task_contract import task_contract

COVERAGE_SCHEMA_VERSION = 2
# 1 要求の再検索に足す手がかり（用語の別名・業務ガイドの検索の手がかり）の上限。
MAX_HINTS = 3
# 要求単位の文の中の「原文: …」以降は判定に使わない（親の要求の原文の写し）。
_FACET_TARGET = re.compile(r"対象「(.+?)」の(.+?)を個別に回答する")
_DEFINITION_TARGET = re.compile(r"項目「(.+?)」")
# 別名・手がかりの照合では、一般的な問い方の語を除く。
_QUESTION_FORM_TERMS = frozenset(
    {
        "方法",
        "手順",
        "やり方",
        "仕方",
        "可否",
        "必要",
        "可能",
        "場合",
        "とき",
        "教え",
        "知り",
        "意味",
        "どう",
        "どこ",
        "なぜ",
        "いつ",
        "何",
        "について",
        "ください",
    }
)


@dataclass(frozen=True)
class CoverageBudget:
    """欠けた要求の再検索の予算（handoff §11.3 の「検索の回数・根拠の数・期限」）。"""

    enabled: bool = True
    # 再検索する要求の数（1 要求 1 回）。
    max_queries: int = 2
    # 再検索で回答の材料に足す根拠（親）の数の合計。
    max_chunks: int = 4
    # 再検索の全体の期限（秒）。期限を過ぎたら次の要求の検索を始めない（実行中の 1 回は中断しない）。
    deadline_seconds: float = 10.0
    # 小問ごとの検索をする小問の数（1 小問 1 回）と、小問ごとに回答の材料に足す根拠（親）の数 (#2399)。
    max_subquestions: int = 5
    chunks_per_subquestion: int = 2


@dataclass(frozen=True)
class RequestTarget:
    """元の質問から追跡する要求 1 件。"""

    id: str
    kind: str
    text: str
    terms: tuple[str, ...]
    # 小問の印（小問でなければ空）と、検索に使う文（空なら text。小問は前置き＋小問の文）。
    subquestion: str = ""
    query: str = ""
    # 要求に先行する、質問中で明示された対象・状況。要求の語だけの一致で覆域を確定しない。
    context_terms: tuple[str, ...] = ()


@dataclass
class CoverageUsage:
    """生成前と生成後の再検索で共有する使用量。設定の上限を段ごとにリセットしない。"""

    queries: int = 0
    subquestions: int = 0
    chunks: int = 0
    elapsed_seconds: float = 0.0
    searched_queries: list[str] = field(default_factory=list)

    def remaining(self, budget: CoverageBudget) -> CoverageBudget:
        return replace(
            budget,
            max_queries=max(0, budget.max_queries - self.queries),
            max_subquestions=max(0, budget.max_subquestions - self.subquestions),
            max_chunks=max(0, budget.max_chunks - self.chunks),
            deadline_seconds=max(0.0, budget.deadline_seconds - self.elapsed_seconds),
        )


def _normalize(text: Any) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).casefold()


def _focus_text(unit: Mapping[str, Any]) -> str:
    """要求単位から、判定と再検索に使う文を返す。対象にしない単位は空。"""
    kind = str(unit.get("kind") or "")
    text = " ".join(str(unit.get("text") or "").split())
    unit_id = str(unit.get("id") or "")
    if kind == "context":
        return ""
    if kind in {"request", "desired_outcome"}:
        return text
    if kind == "explicit_facet" and ".R" in unit_id:
        # 「AとBを追加」の対象ごとの子要求。親の文の判定では一方だけで足りてしまうので、対象ごとに追跡する。
        match = _FACET_TARGET.search(text)
        return f"{match.group(1)}の{match.group(2)}" if match else ""
    if not kind and ".D" in unit_id:
        match = _DEFINITION_TARGET.search(text)
        return f"{match.group(1)}の意味" if match else ""
    # 「操作方法」「影響」などの論点の子要求は親の文と同じ語で判定が変わらないので、親の判定に任せる。
    return ""


def _content_terms(raw_terms: Iterable[str]) -> tuple[str, ...]:
    """検索の手がかりに使う語。2 文字以上で、問い方の語を除き、部分の語が揃う複合語（「登録方法」）は部分に任せる。

    分かち書きは複合語とその部分の両方を返す。複合語のまま数えると、資料が「登録」「方法」を別々に書く
    ときに一致しないので、部分の語がある複合語は数えない。
    """
    terms = [t for t in dict.fromkeys(_normalize(term).strip() for term in raw_terms) if len(t) >= 2 and t not in _QUESTION_FORM_TERMS]
    candidates = set(terms) | {_normalize(t).strip() for t in raw_terms}
    return tuple(t for t in terms if not any(other != t and len(other) >= 2 and other in t for other in candidates))


def coverage_targets(question: str, tokenize: Callable[[str], Sequence[str]]) -> list[RequestTarget]:
    """質問契約の要求を ID ごとに保持する。語の集合では要求を統合しない。"""
    targets: list[RequestTarget] = []
    seen: set[str] = set()
    contract = task_contract(question)
    contexts = [str(unit["text"]) for unit in task_contract(contract["current_request"])["request_units"] if unit.get("kind") == "context"]
    context_text = " ".join(contexts)
    try:
        context_terms = _content_terms(tokenize(context_text)) if context_text else ()
    except Exception:  # noqa: BLE001 - 分かち書きの失敗で質問を失敗にしない。
        context_terms = ()
    for unit in contract["request_units"]:
        focus = _focus_text(unit)
        if not focus:
            continue
        # 小問は印を除いた文で判定し、前置き（対象・状況）を付けた文で検索する（#2399）。
        label = str(unit.get("subquestion") or "")
        body = focus.removeprefix(label).strip() if label else focus
        try:
            raw_terms = tokenize(body)
        except Exception:  # noqa: BLE001 - 分かち書きの失敗は判定しない（unassessed）にする。
            raw_terms = ()
        terms = _content_terms(raw_terms)
        key = str(unit.get("id") or "")
        if key in seen:
            continue
        seen.add(key)
        targets.append(
            RequestTarget(
                id=str(unit.get("id") or ""),
                kind=str(unit.get("kind") or "definition"),
                text=focus,
                terms=terms,
                subquestion=label,
                query=" ".join(part for part in (str(unit.get("preamble") or context_text), body) if part) if label or context_text else "",
                context_terms=context_terms,
            )
        )
    return targets


def _record_key(record: Any) -> str:
    return str(getattr(record, "chunk_uid", "") or getattr(record, "id", "") or "")


def request_rows(targets: Sequence[RequestTarget]) -> list[dict[str, Any]]:
    """要求の追跡行を作る。取得済み本文から充足性を推測しない。"""
    return [
        {
            "id": target.id,
            "kind": target.kind,
            "text": target.text,
            "status": "unassessed",
            "assessment": "not_performed",
            "answerability": "unassessed",
            "retrieval_status": "not_planned",
            "evidence_ids": [],
            **({"subquestion": target.subquestion} if target.subquestion else {}),
        }
        for target in targets
    ]


def coverage_counts(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {"unassessed": len(rows)}


def _needs_search(row: Mapping[str, Any]) -> bool:
    """生成の明示した gap または実行済み監査の不足だけを検索対象にする。"""
    return row.get("answer_status") in {"missing", "partial"}


def _query_key(text: str) -> str:
    return re.sub(r"[\s。、．，,.?？!！]+", "", _normalize(text))


def within_extra_budget(rows: Sequence[Mapping[str, Any]], budget: CoverageBudget, remaining: int) -> CoverageBudget:
    """小問ごとの検索と再検索の回数を、追加の検索の枠の残り `remaining`（#2395）の中に収めた予算（小問が先）。"""
    wanted = sum(1 for row in rows if row.get("subquestion") and _needs_search(row))
    subquestions = max(0, min(budget.max_subquestions, wanted, remaining))
    return replace(budget, max_subquestions=subquestions, max_queries=max(0, min(budget.max_queries, remaining - subquestions)))


def re_retrieval_skip_reason(rows: Sequence[Mapping[str, Any]], budget: CoverageBudget) -> str:
    """再検索しない理由。空なら再検索する（根拠の無い要求と、根拠が無いか一部だけの小問）。"""
    if not budget.enabled:
        return "disabled"
    if budget.deadline_seconds <= 0:
        return "deadline"
    wanted = [row for row in rows if _needs_search(row)]
    if not wanted:
        return "no_explicit_gap"
    subquestion_budget = budget.max_subquestions > 0 and budget.chunks_per_subquestion > 0
    request_budget = budget.max_queries > 0 and budget.max_chunks > 0
    if not any(subquestion_budget if row.get("subquestion") else request_budget for row in wanted):
        return "no_budget"
    return ""


def request_hints(target: RequestTarget, runtime_knowledge: Any, tokenize: Callable[[str], Sequence[str]]) -> list[str]:
    """要求の再検索に足す手がかり。要求の語を含む用語の別名と、要求の語を共有する規則（業務ガイド）の手がかり。"""
    if runtime_knowledge is None:
        return []
    focus = _normalize(target.text)
    terms = set(target.terms)
    hints: list[str] = []
    for term in getattr(runtime_knowledge, "matched_terms", ()) or ():
        labels = [str(label) for label in term.labels() if str(label).strip()]
        if any(_normalize(label) in focus for label in labels):
            hints.extend(label for label in labels if _normalize(label) not in focus)
    stage_rules = getattr(runtime_knowledge, "rules_for_stage", None)
    rules = stage_rules("generation") if callable(stage_rules) else getattr(runtime_knowledge, "matched_rules", ()) or ()
    for rule in rules:
        for trigger in getattr(rule, "triggers", ()) or ():
            trigger = str(trigger).strip()
            if not trigger or _normalize(trigger) in focus:
                continue
            try:
                trigger_terms = {_normalize(t).strip() for t in tokenize(trigger)}
            except Exception:  # noqa: BLE001 - 手がかりは補助。
                continue
            if trigger_terms & terms:
                hints.append(trigger)
    return list(dict.fromkeys(hints))[:MAX_HINTS]


def targeted_queries(
    target: RequestTarget, hints: Sequence[str], *, unmatched: Sequence[str] = (), searched: Iterable[str] = ()
) -> tuple[str, ...]:
    """欠けた要求 1 件の検索文。要求の文（小問は前置き＋小問の文）を主軸にし、手がかりがあれば手がかり付きの文を、
    根拠に現れなかった語（``unmatched``）があればその語だけの文を足す。最初の検索の文（``searched``）と同じ文は除く。"""
    text = target.query or target.text
    candidates = (text, " ".join([text, *hints]) if hints else "", " ".join(unmatched))
    done = {_query_key(query) for query in searched}
    queries: dict[str, str] = {}
    for query in candidates:
        key = _query_key(query)
        if key and key not in done:
            queries.setdefault(key, query)
    return tuple(queries.values())


@dataclass(frozen=True)
class PlannedRetrieval:
    """追加の検索 1 回の計画。chunk_cap はこの検索で確保する根拠の数（None は欠けた要求の共有の枠）。"""

    target: RequestTarget
    chunk_cap: int | None = None


def plan_targeted_retrievals(
    rows: Sequence[Mapping[str, Any]], targets: Sequence[RequestTarget], budget: CoverageBudget
) -> tuple[list[PlannedRetrieval], str]:
    """追加の検索の枠を 1 か所で数える。戻り値は (検索する順の計画, 枠で打ち切った理由)。

    小問（根拠が無いか一部）を質問の順に `max_subquestions` 件まで、それぞれ `chunks_per_subquestion` 件を確保して
    先に検索し、次に根拠の無い要求を `max_queries` 件まで検索する（足す根拠は合計 `max_chunks` 件を交互に）。
    要求単位ごとの別の検索を足すときも、この計画に足して同じ期限と根拠の選び方（`select_new_parents`）で数える。
    """
    by_id = {target.id: target for target in targets}
    wanted = [by_id[row["id"]] for row in rows if row["id"] in by_id and _needs_search(row)]
    status = {row["id"]: row.get("answer_status") or row["status"] for row in rows}
    subquestions = [target for target in wanted if target.subquestion]
    # 根拠の無い要求を、一部だけの要求より先に検索する（枠が足りないときは missing を優先）。
    requests = sorted((target for target in wanted if not target.subquestion), key=lambda target: status[target.id] != "missing")
    plan: list[PlannedRetrieval] = []
    stop = ""
    if budget.chunks_per_subquestion > 0:
        plan.extend(PlannedRetrieval(target, budget.chunks_per_subquestion) for target in subquestions[: budget.max_subquestions])
        if len(subquestions) > budget.max_subquestions:
            stop = "max_subquestions"
    if budget.max_chunks > 0:
        plan.extend(PlannedRetrieval(target) for target in requests[: budget.max_queries])
        if len(requests) > budget.max_queries:
            stop = stop or "max_queries"
    return plan, stop


@dataclass(frozen=True)
class TargetedRetrieval:
    """欠けた要求 1 件の再検索の結果。"""

    request_id: str
    queries: tuple[str, ...]
    parents: tuple[Any, ...]  # ContextParentEvidence（順位順）
    expansion_records: tuple[Any, ...] = ()
    elapsed_ms: int = 0
    # 失敗したときの例外の型の名前（例外の文は入れない。#3284）。
    error: str = ""
    # この要求に確保する根拠の数（小問）。None は欠けた要求の共有の枠（max_chunks）から取る。
    chunk_cap: int | None = None
    reused: bool = False


def run_targeted_retrievals(
    rows: Sequence[Mapping[str, Any]],
    targets: Sequence[RequestTarget],
    *,
    budget: CoverageBudget,
    retrieve: Callable[[str, tuple[str, ...]], Any],
    runtime_knowledge: Any = None,
    tokenize: Callable[[str], Sequence[str]],
    searched_queries: Sequence[str] = (),
    clock: Callable[[], float] = monotonic,
) -> tuple[list[TargetedRetrieval], str]:
    """計画（`plan_targeted_retrievals`）の要求ごとに 1 回だけ検索する。戻り値は (結果, 打ち切りの理由)。

    最初の検索の文（``searched_queries``）と違う検索文を作れない要求は検索しない。どの要求も作れなければ
    打ち切りの理由を ``same_query`` にする（#2810）。

    retrieve(request_text, queries) は AnswerContext（evidence_tree と expansion_records を持つ）を返す。
    検索の失敗は要求ごとに記録し、回答は既存の根拠で続ける（失敗と「根拠が無い」を区別する。handoff §9 の 5）。
    """
    plan, stop = plan_targeted_retrievals(rows, targets, budget)
    results: list[TargetedRetrieval] = []
    start = clock()
    same_query = 0
    searched = list(searched_queries)
    for planned in plan:
        target = planned.target
        if clock() - start >= budget.deadline_seconds:
            stop = "deadline"
            break
        queries = targeted_queries(
            target,
            request_hints(target, runtime_knowledge, tokenize),
            searched=searched,
        )
        if not queries:
            previous = next(
                (
                    result
                    for result in results
                    if any(_query_key(query) == _query_key(target.query or target.text) for query in result.queries)
                ),
                None,
            )
            if previous is not None:
                results.append(replace(previous, request_id=target.id, reused=True, chunk_cap=planned.chunk_cap))
                continue
            same_query += 1
            continue
        searched.extend(queries)
        began = clock()
        try:
            context = retrieve(target.query or target.text, queries)
        except Exception as exc:  # noqa: BLE001 - 再検索は補助。失敗しても既存の根拠で回答する。
            # 記録には例外の型の名前だけを残す（例外の文は DB のエラーの文を含みうる。#3284）。
            report_degraded("request_coverage_retrieval", exc)
            results.append(
                TargetedRetrieval(
                    target.id,
                    queries,
                    (),
                    (),
                    int((clock() - began) * 1000),
                    type(exc).__name__,
                    planned.chunk_cap,
                )
            )
            continue
        results.append(
            TargetedRetrieval(
                target.id,
                queries,
                tuple(getattr(context, "evidence_tree", ()) or ()),
                tuple(getattr(context, "expansion_records", ()) or ()),
                int((clock() - began) * 1000),
                chunk_cap=planned.chunk_cap,
            )
        )
    if plan and same_query == len(plan):
        stop = stop or "same_query"
    return results, stop


def select_new_parents(results: Sequence[TargetedRetrieval], existing_keys: Iterable[str], max_chunks: int) -> list[tuple[str, Any]]:
    """再検索の結果から、回答の材料に無い根拠（親）を要求ごとに順番に 1 件ずつ選ぶ。戻り値は (request_id, parent)。

    1 つの要求の結果が予算を使い切らないよう、各要求の最上位から交互に取る。`chunk_cap` のある要求（小問）は
    その件数まで確保し、無い要求（欠けた要求）は合わせて max_chunks 件までにする。
    """
    seen = set(existing_keys)
    queues = [[(result.request_id, parent) for parent in result.parents] for result in results]
    caps = [result.chunk_cap for result in results]
    taken = [0] * len(results)
    shared = 0
    selected: list[tuple[str, Any]] = []

    def open_(index: int) -> bool:
        cap = caps[index]
        return taken[index] < cap if cap is not None else shared < max_chunks

    progress = True
    while progress:
        progress = False
        for index, queue in enumerate(queues):
            if not open_(index):
                continue
            while queue:
                request_id, parent = queue.pop(0)
                key = _record_key(parent.record)
                if not key or key in seen:
                    continue
                seen.add(key)
                selected.append((request_id, parent))
                taken[index] += 1
                if caps[index] is None:
                    shared += 1
                progress = True
                break
    return selected


__all__ = [
    "COVERAGE_SCHEMA_VERSION",
    "within_extra_budget",
    "CoverageBudget",
    "CoverageUsage",
    "PlannedRetrieval",
    "RequestTarget",
    "TargetedRetrieval",
    "request_rows",
    "coverage_counts",
    "coverage_targets",
    "plan_targeted_retrievals",
    "re_retrieval_skip_reason",
    "request_hints",
    "run_targeted_retrievals",
    "select_new_parents",
    "targeted_queries",
]
