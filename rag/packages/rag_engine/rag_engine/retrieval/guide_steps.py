"""業務ガイドの手順ごとの検索と、手順が参照する資料の頁の固定（#2395。静的な多段）。

照合した業務ガイドの手順（分かっている条件で外れた手順は呼び出し側が除く）ごとに検索し、手順の根拠を
回答の材料に確保する。手順 A の後に要る手順 B のように、質問の文だけでは上位に出ない手順の根拠を取るため。

- 検索文は「質問 + 手順の検索の手がかり」（手がかりが無ければ手順の題名）。
- 手順が資料と頁（`references`。表計算の資料はシートの行。#2663）を持てば、その範囲に絞った検索も行い、当たった根拠は検索の順位に関係なく
  回答の材料に入れる（必須対象ごとの最低1件と、`reference_max_chunks` 件の追加候補）。
- 回数は追加の検索の合計枠から必須対象分を先に確保する。各必須対象に最大1回の取得機会を割り当て、
  残りで補助検索・任意手順の検索を種類別の上限まで行う。候補の取得は範囲の読了や意味支持を保証しない。

検索と並べ替えは既存の hybrid 検索を使い、モデル（LLM）は呼ばない。足す根拠は既存の根拠を追い出さない
（呼び出し側が併合する）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal

# 2: 手順ごとの検索をしなかった手順（unsearched_step_ids）と、手順の参照の頁に当たる根拠の数（referenced_count）を
#    足した（#2706）。
GUIDE_STEP_TRACE_SCHEMA_VERSION = 3
# 1 手順の検索文に足す手がかりの上限。
MAX_STEP_HINTS = 5

SearchKind = Literal["step", "reference"]


@dataclass(frozen=True)
class GuidePageRange:
    page_start: int
    page_end: int

    def to_payload(self) -> dict[str, int]:
        return {"page_start": self.page_start, "page_end": self.page_end}


@dataclass(frozen=True)
class GuideSheetRange:
    """表計算の資料のシートと行の範囲（#2663）。行が無ければシートの全体。頁とは別の位置。"""

    sheet_name: str
    row_start: int | None = None
    row_end: int | None = None

    def to_payload(self) -> dict[str, Any]:
        rows = {"row_start": self.row_start, "row_end": self.row_end or self.row_start} if self.row_start else {}
        return {"sheet_name": self.sheet_name, **rows}


@dataclass(frozen=True)
class GuideStepReference:
    document_id: str
    title: str = ""
    # 空なら文書の全体。
    page_ranges: tuple[GuidePageRange, ...] = ()
    # 表計算の資料のシートの行の範囲（頁の範囲とは同時に持たない）。
    sheet_ranges: tuple[GuideSheetRange, ...] = ()

    def filters(self) -> list[dict[str, Any]]:
        """検索の範囲の絞り込み（`page_ranges` の形。頁かシートの行。どちらも無ければ文書の全体）。"""
        ranges = [item.to_payload() for item in self.page_ranges] or [item.to_payload() for item in self.sheet_ranges]
        if not ranges:
            return [{"document_id": self.document_id}]
        return [{"document_id": self.document_id, **item} for item in ranges]

    def to_payload(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "title": self.title,
            "page_ranges": [item.to_payload() for item in self.page_ranges],
            "sheet_ranges": [item.to_payload() for item in self.sheet_ranges],
        }


@dataclass(frozen=True)
class GuideStep:
    step_id: str
    title: str
    retrieval_hints: tuple[str, ...] = ()
    references: tuple[GuideStepReference, ...] = ()
    # 回答の充足で必須の手順か（条件が決まらない分岐の行き先とその後続は False。#2491）。
    required: bool = True
    purpose: str = ""
    evidence_requirements: tuple[str, ...] = ()
    done_when: str = ""
    depends_on: tuple[str, ...] = ()

    def hints(self) -> tuple[str, ...]:
        return self.retrieval_hints[:MAX_STEP_HINTS] or ((self.title,) if self.title else ())

    def page_filters(self) -> list[dict[str, Any]]:
        return [item for reference in self.references for item in reference.filters()]


@dataclass(frozen=True)
class GuidePlan:
    """回答に使う業務ガイドの手順（分岐で残った手順だけ）。"""

    guide_id: str
    revision: int
    steps: tuple[GuideStep, ...] = ()
    # 業務ガイドの題名と、設定の窓口・引き継ぐ条件・承認（#2792。回答の「業務ガイドの案内」の節の材料）。
    title: str = ""
    contact: str = ""
    handoff_conditions: tuple[str, ...] = ()
    approval_required: bool = False
    approval_note: str = ""
    branch_notes: tuple[str, ...] = ()

    @property
    def reference_ranges(self) -> tuple[tuple[str, int | None, int | None], ...]:
        """手順が参照する文書と頁の範囲（文書 id, 始まりの頁, 終わりの頁）。頁の無い参照は頁を None にする（#2828）。"""
        ranges: list[tuple[str, int | None, int | None]] = []
        for step in self.steps:
            for reference in step.references:
                if not reference.document_id:
                    continue
                if not reference.page_ranges:
                    ranges.append((reference.document_id, None, None))
                ranges.extend((reference.document_id, item.page_start, item.page_end) for item in reference.page_ranges)
        return tuple(dict.fromkeys(ranges))

    @property
    def optional_step_ids(self) -> tuple[str, ...]:
        """回答の充足で必須に数えない手順の id（#2491）。"""
        return tuple(step.step_id for step in self.steps if not step.required)

    @property
    def step_terms(self) -> tuple[tuple[str, tuple[str, ...]], ...]:
        """手順ごとの検索の手がかり（手順 id, 手がかりの列）。回答の item を手順に対応付ける語に使う（#2649）。"""
        return tuple((step.step_id, step.retrieval_hints[:MAX_STEP_HINTS]) for step in self.steps if step.retrieval_hints)

    @property
    def task_scope(self) -> tuple[dict[str, Any], ...]:
        """分岐確定後の回答範囲。根拠の取得・支持状態とは独立に全手順を保持する。"""
        contracts = {contract["step_id"]: contract for contract in self.step_contracts}
        return tuple(
            {
                "guide_id": self.guide_id,
                "revision": self.revision,
                "step_id": step.step_id,
                "title": step.title,
                "required": step.required,
                "depends_on": list(step.depends_on),
                "purpose": step.purpose,
                "evidence_requirements": list(step.evidence_requirements),
                "done_when": step.done_when,
                "requirements": contracts.get(step.step_id, {}).get(
                    "requirements",
                    [{"id": f"{step.step_id}:0", "text": step.done_when or step.title, "kind": "completion"}]
                    if step.done_when or step.title
                    else [],
                ),
            }
            for step in self.steps
        )

    @property
    def step_contracts(self) -> tuple[dict[str, Any], ...]:
        """手順の目的・根拠の要件・完了条件と前提手順を監査へ渡す。"""
        return tuple(
            {
                "step_id": step.step_id,
                "purpose": step.purpose,
                "requirements": [
                    {"id": f"{step.step_id}:{index}", "text": text, "kind": kind}
                    for index, (kind, text) in enumerate(
                        [*(("evidence", text) for text in step.evidence_requirements), ("completion", step.done_when or step.title)]
                    )
                    if text
                ],
                "depends_on": list(step.depends_on),
            }
            for step in self.steps
            if step.purpose or step.evidence_requirements or step.done_when
        )

    @classmethod
    def from_payload(
        cls, guide_id: str, revision: int, steps: Iterable[Mapping[str, Any]], *, title: str = "", notices: Mapping[str, Any] | None = None
    ) -> GuidePlan | None:
        """呼び出し側の手順の一覧（id・title・required・retrieval_hints・references）から作る。手順が無ければ None。

        ``notices`` は業務ガイドの窓口（contact）・引き継ぐ条件（handoff_conditions）・承認（approval_required・approval_note）。
        """
        parsed: list[GuideStep] = []
        for raw in steps:
            if not isinstance(raw, Mapping) or not str(raw.get("id") or "").strip():
                continue
            references = tuple(
                GuideStepReference(
                    document_id=str(item.get("document_id")),
                    title=str(item.get("title") or ""),
                    page_ranges=tuple(
                        GuidePageRange(int(r["page_start"]), int(r.get("page_end") or r["page_start"]))
                        for r in item.get("page_ranges") or ()
                        if isinstance(r, Mapping) and r.get("page_start")
                    ),
                    sheet_ranges=tuple(
                        GuideSheetRange(
                            str(r["sheet_name"]),
                            int(r["row_start"]) if r.get("row_start") else None,
                            int(r.get("row_end") or r["row_start"]) if r.get("row_start") else None,
                        )
                        for r in item.get("sheet_ranges") or ()
                        if isinstance(r, Mapping) and str(r.get("sheet_name") or "").strip()
                    ),
                )
                for item in raw.get("references") or ()
                if isinstance(item, Mapping) and str(item.get("document_id") or "").strip()
            )
            parsed.append(
                GuideStep(
                    step_id=str(raw["id"]).strip(),
                    title=str(raw.get("title") or "").strip(),
                    retrieval_hints=tuple(str(h).strip() for h in raw.get("retrieval_hints") or () if str(h).strip()),
                    references=references,
                    required=raw.get("required") is not False,
                    purpose=str(raw.get("purpose") or ""),
                    evidence_requirements=tuple(str(x) for x in raw.get("evidence_requirements") or () if x),
                    done_when=str(raw.get("done_when") or ""),
                    depends_on=tuple(str(x) for x in raw.get("depends_on") or () if x),
                )
            )
        notices = notices or {}
        if not parsed:
            return None
        return cls(
            guide_id=guide_id,
            revision=int(revision),
            steps=tuple(parsed),
            title=str(title or ""),
            contact=str(notices.get("contact") or ""),
            handoff_conditions=tuple(str(item) for item in notices.get("handoff_conditions") or () if str(item).strip()),
            approval_required=notices.get("approval_required") is True,
            approval_note=str(notices.get("approval_note") or ""),
            branch_notes=tuple(str(item).strip() for item in notices.get("branch_notes") or () if str(item).strip()),
        )


@dataclass(frozen=True)
class StepSearch:
    """手順 1 件の検索 1 回の結果。"""

    step_id: str
    kind: SearchKind
    text: str
    queries: tuple[str, ...]
    parents: tuple[Any, ...]  # ContextParentEvidence（順位順）
    expansion_records: tuple[Any, ...] = ()
    elapsed_ms: int = 0
    error: str = ""
    lane: Literal["required", "optional"] = "optional"
    target_id: str = ""
    owner_ids: tuple[str, ...] = ()
    reused: bool = False


def step_queries(question: str, step: GuideStep) -> tuple[str, tuple[str, ...]]:
    """手順の検索の (主の検索文, 検索文の列)。主は「質問 + 手がかり」、手がかりだけの文も足す。"""
    hints = " ".join(step.hints())
    text = " ".join(part for part in (question.strip(), hints) if part)
    return text, tuple(dict.fromkeys(q for q in (text, hints) if q))


def planned_searches(plan: GuidePlan, limit: int) -> list[tuple[GuideStep, SearchKind]]:
    """検索する (手順, 種類) を順に ``limit`` 件まで。

    各手順に 1 回（参照があれば頁に絞った検索、無ければ絞らない検索）を先に割り当て、残りで参照のある手順の
    絞らない検索をする（後ろの手順の検索が、前の手順の 2 回目に押し出されないようにする）。
    """
    first: list[tuple[GuideStep, SearchKind]] = [(step, "reference" if step.references else "step") for step in plan.steps]
    second: list[tuple[GuideStep, SearchKind]] = [(step, "step") for step in plan.steps if step.references]
    return [*first, *second][: max(0, limit)]


def run_step_searches(
    question: str,
    plan: GuidePlan,
    *,
    limit: int,
    deadline_seconds: float,
    retrieve: Callable[[str, tuple[str, ...], list[dict[str, Any]]], Any],
    clock: Callable[[], float] = monotonic,
) -> tuple[list[StepSearch], str]:
    """手順ごとに検索する。戻り値は (結果, 打ち切りの理由)。

    retrieve(text, queries, page_ranges) は AnswerContext（evidence_tree と expansion_records を持つ）を返す。
    page_ranges が空なら絞らない検索。検索の失敗は手順ごとに記録し、回答は既存の根拠で続ける。
    """
    searches = planned_searches(plan, limit)
    wanted = len(plan.steps) + sum(1 for step in plan.steps if step.references)
    stop = "max_queries" if len(searches) < wanted else ""
    results: list[StepSearch] = []
    start = clock()
    for step, kind in searches:
        if clock() - start >= deadline_seconds:
            stop = "deadline"
            break
        text, queries = step_queries(question, step)
        page_ranges = step.page_filters() if kind == "reference" else []
        began = clock()
        try:
            context = retrieve(text, queries, page_ranges)
        except Exception as exc:  # noqa: BLE001 - 手順の検索は補助。失敗しても既存の根拠で回答する。
            # error は例外の型の名前だけ（本文は残さない。#3259）。
            results.append(StepSearch(step.step_id, kind, text, queries, (), (), int((clock() - began) * 1000), type(exc).__name__))
            continue
        results.append(
            StepSearch(
                step.step_id,
                kind,
                text,
                queries,
                tuple(getattr(context, "evidence_tree", ()) or ()),
                tuple(getattr(context, "expansion_records", ()) or ()),
                int((clock() - began) * 1000),
            )
        )
    return results, stop


def unsearched_steps(plan: GuidePlan, results: Sequence[StepSearch]) -> tuple[str, ...]:
    """検索できなかった手順の id（期限・回数の上限で検索しなかった・検索がすべて失敗した。#2706）。"""
    searched = {result.step_id for result in results if not result.error}
    return tuple(step.step_id for step in plan.steps if step.step_id not in searched)


def _record_pages(record: Any) -> tuple[str, int, int] | None:
    """根拠の (文書の id, 頁の始め, 頁の終わり)。分からなければ None。"""
    metadata = getattr(record, "metadata", None)
    document_id = str(metadata.get("document_id") or "") if isinstance(metadata, Mapping) else ""
    start = getattr(record, "page", None)
    if not document_id or not isinstance(start, int) or isinstance(start, bool):
        return None
    end = getattr(record, "page_end", None)
    return document_id, start, end if isinstance(end, int) and end >= start else start


def reference_step_sources(plan: GuidePlan, records: Iterable[Any]) -> dict[str, tuple[str, ...]]:
    """手順の参照の頁の範囲に当たる根拠（手順 id → 根拠の key。#2706）。

    業務ガイドの定義で手順の根拠になる根拠（参照の文書と頁の範囲が重なる）を、検索の結果に関係なく手順に紐づける。
    期限で検索しなかった手順の根拠も、複数の手順が同じ根拠を参照すること（共有）も、これで分かる。頁の範囲の無い
    参照（文書の全体）は手順に固有な範囲を決めないので紐づけない。
    """
    pages = [(key, located) for record in records if (key := _key_of(record)) and (located := _record_pages(record))]
    sources: dict[str, tuple[str, ...]] = {}
    for step in plan.steps:
        ranges = [
            (reference.document_id, item.page_start, item.page_end) for reference in step.references for item in reference.page_ranges
        ]
        keys = [
            key
            for key, (document_id, start, end) in pages
            if any(document_id == ref_doc and start <= ref_end and ref_start <= end for ref_doc, ref_start, ref_end in ranges)
        ]
        if keys:
            sources[step.step_id] = tuple(dict.fromkeys(keys))
    return sources


def _key_of(record: Any) -> str:
    return str(getattr(record, "chunk_uid", "") or getattr(record, "id", "") or "")


def _key(parent: Any) -> str:
    return _key_of(getattr(parent, "record", None))


@dataclass(frozen=True)
class StepSelection:
    """手順の検索の結果から選んだ根拠。"""

    # (step_id, 種類, parent)。回答の材料に無く、足す根拠。
    added: tuple[tuple[str, SearchKind, Any], ...]
    # 手順 id → その手順の根拠の key（既存の根拠のうち手順の検索で当たったものと、足した根拠）。
    step_sources: Mapping[str, tuple[str, ...]]


def select_step_parents(
    results: Sequence[StepSearch],
    existing_keys: Iterable[str],
    *,
    chunks_per_step: int,
    reference_max_chunks: int,
) -> StepSelection:
    """手順ごとの検索の結果から、手順の根拠を選ぶ。

    - 絞らない検索: 各検索の上位 ``chunks_per_step`` 件を手順の根拠にする（既にある根拠はそのまま手順に紐づけ、
      無い根拠を足す）。
    - 頁に絞った検索: 参照の頁の根拠は順位に関係なく入れる。各検索の上位から手順の間で交互に、合計
      ``reference_max_chunks`` 件まで（既にある根拠は数えずに手順に紐づける）。
    """
    existing = set(existing_keys)
    seen = set(existing)
    added: list[tuple[str, SearchKind, Any]] = []
    sources: dict[str, list[str]] = {}

    def link(step_id: str, key: str) -> None:
        keys = sources.setdefault(step_id, [])
        if key not in keys:
            keys.append(key)

    # 必須の最少一候補は先に確保する。任意の追加候補だけを種類別上限で制限する。
    for result in results:
        if result.lane != "required":
            continue
        for parent in result.parents[:1]:
            key = _key(parent)
            if not key:
                continue
            for owner in result.owner_ids or (result.step_id,):
                link(owner, key)
            if key not in seen:
                seen.add(key)
                added.append((result.step_id, result.kind, parent))

    per_step = max(0, int(chunks_per_step))
    for result in results:
        if result.kind != "step":
            continue
        taken = 0
        for parent in result.parents:
            if taken >= per_step:
                break
            key = _key(parent)
            if not key:
                continue
            taken += 1
            for owner in result.owner_ids or (result.step_id,):
                link(owner, key)
            if key not in seen:
                seen.add(key)
                added.append((result.step_id, "step", parent))

    queues = [list(result.parents) for result in results if result.kind == "reference"]
    owners = [result.owner_ids or (result.step_id,) for result in results if result.kind == "reference"]
    pinned = 0
    limit = max(0, int(reference_max_chunks))
    while pinned < limit and any(queues):
        for index, queue in enumerate(queues):
            while queue:
                parent = queue.pop(0)
                key = _key(parent)
                if not key:
                    continue
                for owner in owners[index]:
                    link(owner, key)
                if key in seen:
                    continue
                seen.add(key)
                added.append((owners[index][0], "reference", parent))
                pinned += 1
                break
            if pinned >= limit:
                break
    return StepSelection(added=tuple(added), step_sources={step_id: tuple(keys) for step_id, keys in sources.items()})


__all__ = [
    "GUIDE_STEP_TRACE_SCHEMA_VERSION",
    "GuidePageRange",
    "GuidePlan",
    "RequiredGuideTarget",
    "required_guide_targets",
    "run_required_guide_searches",
    "target_records",
    "GuideSheetRange",
    "GuideStep",
    "GuideStepReference",
    "StepSearch",
    "StepSelection",
    "planned_searches",
    "reference_step_sources",
    "run_step_searches",
    "select_step_parents",
    "step_queries",
    "unsearched_steps",
]


@dataclass(frozen=True)
class RequiredGuideTarget:
    """分岐後の必須の取得対象。参照の和集合ではなく一つの範囲を持つ。"""

    target_id: str
    steps: tuple[GuideStep, ...]
    scope_filter: Mapping[str, Any] | None = None

    @property
    def owner_ids(self) -> tuple[str, ...]:
        return tuple(step.step_id for step in self.steps)

    @property
    def kind(self) -> SearchKind:
        return "reference" if self.scope_filter is not None else "step"

    def to_payload(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "step_ids": list(self.owner_ids),
            "kind": self.kind,
            "filter": dict(self.scope_filter) if self.scope_filter is not None else None,
            "scope_coverage": "unknown",
            "semantic_support": "not_run",
        }


def required_guide_targets(plan: GuidePlan) -> tuple[RequiredGuideTarget, ...]:
    """同じ明示範囲だけ共有し、参照なしの検索目的は手順ごとに保持する。"""
    groups: dict[str, tuple[dict[str, Any] | None, list[GuideStep]]] = {}
    for step in plan.steps:
        if not step.required:
            continue
        filters: list[dict[str, Any] | None] = list(step.page_filters()) or [None]
        for scope in filters:
            identity = json.dumps(scope, sort_keys=True, ensure_ascii=False) if scope is not None else "step:" + step.step_id
            if identity not in groups:
                groups[identity] = (scope, [])
            if step not in groups[identity][1]:
                groups[identity][1].append(step)
    return tuple(
        RequiredGuideTarget("required:" + hashlib.sha256(key.encode()).hexdigest()[:20], tuple(steps), scope)
        for key, (scope, steps) in groups.items()
    )


def _original_body(record: Any) -> str:
    from rag_engine.parsing.structural_text import structural_only

    body = str(getattr(record, "body_text", "") or getattr(record, "text", "") or "").strip()
    return body if body and not structural_only(body) else ""


def target_records(target: RequiredGuideTarget, records: Iterable[Any]) -> tuple[Any, ...]:
    """対象の文書と位置に当たる原文候補。本文の支持や範囲全体の読了は判定しない。"""
    found = []
    for record in records:
        if not _original_body(record):
            continue
        scope = target.scope_filter
        if scope is None:
            found.append(record)
            continue
        metadata = getattr(record, "metadata", {}) or {}
        if not isinstance(metadata, Mapping):
            continue
        if str(metadata.get("document_id") or "") != str(scope.get("document_id") or ""):
            continue
        if scope.get("sheet_name"):
            location = metadata.get("sheet_location") or {}
            if not isinstance(location, Mapping):
                continue
            if location.get("sheet_name") != scope["sheet_name"]:
                continue
            if scope.get("row_start") is not None:
                start, end = location.get("row_start"), location.get("row_end") or location.get("row_start")
                if (
                    type(start) is not int
                    or type(end) is not int
                    or start > int(scope.get("row_end") or scope["row_start"])
                    or end < int(scope["row_start"])
                ):
                    continue
        elif scope.get("page_start") is not None:
            located = _record_pages(record)
            if located is None or located[1] > int(scope.get("page_end") or scope["page_start"]) or located[2] < int(scope["page_start"]):
                continue
        found.append(record)
    return tuple(found)


def _represented_scope(target: RequiredGuideTarget, records: Sequence[Any]) -> bool:
    """各位置に原文候補がある場合だけ再取得を省く。文書・シート全体は推定しない。"""
    scope = target.scope_filter
    if scope is None or not records:
        return False
    row = "sheet_name" in scope
    lower = scope.get("row_start" if row else "page_start")
    if type(lower) is not int:
        return False
    upper = int(scope.get("row_end" if row else "page_end") or lower)
    intervals = []
    for record in records:
        if row:
            metadata = getattr(record, "metadata", {}) or {}
            if not isinstance(metadata, Mapping):
                continue
            value = metadata.get("sheet_location") or {}
            if not isinstance(value, Mapping):
                continue
            a, b = value.get("row_start"), value.get("row_end") or value.get("row_start")
        else:
            located = _record_pages(record)
            if located is None:
                continue
            _doc, a, b = located
        if type(a) is int and type(b) is int and a <= b:
            intervals.append((a, b))
    cursor = lower
    for a, b in sorted(intervals):
        if a > cursor:
            break
        cursor = max(cursor, b + 1)
    return cursor > upper


def run_required_guide_searches(
    question: str,
    targets: Sequence[RequiredGuideTarget],
    *,
    budget: Any,
    existing_records: Sequence[Any],
    deadline_seconds: float,
    retrieve: Callable[[str, tuple[str, ...], list[dict[str, Any]]], Any],
    clock: Callable[[], float] = monotonic,
) -> tuple[list[StepSearch], list[dict[str, Any]], str]:
    """有限の対象へ一回だけ取得機会を割り当てる。失敗も実際の枠を消費する。"""
    from rag_engine.retrieval.context_builder import ContextParentEvidence

    reusable = {target.target_id: target_records(target, existing_records) for target in targets}
    reused = {target.target_id for target in targets if _represented_scope(target, reusable[target.target_id])}
    # 先頭の対象に枠を固定しない。再利用後に残る全対象へ同じ物理枠を回す。
    budget.reserve_required(len(targets) - len(reused))
    results: list[StepSearch] = []
    states: list[dict[str, Any]] = []
    start = clock()
    stop = ""
    for target in targets:
        state = {
            **target.to_payload(),
            "execution": "not_run",
            "acquisition": "unknown",
            "candidate_source_ids": [],
            "retained_source_ids": [],
            "retained_body_chars": 0,
        }
        text, queries = step_queries(question, target.steps[0])
        parents: tuple[Any, ...] = ()
        expansion: tuple[Any, ...] = ()
        error = ""
        elapsed = 0
        is_reused = target.target_id in reused
        if is_reused:
            parents = tuple(
                ContextParentEvidence(record=record, role="synthesis_parent", reason="guide_required_reuse")
                for record in reusable[target.target_id]
            )
            state.update(execution="reused", acquisition="candidate_available")
        elif clock() - start >= deadline_seconds:
            stop = "deadline"
            state["reason"] = "deadline"
        elif not budget.take_required():
            stop = stop or "required_budget_exhausted"
            state["reason"] = "required_budget_exhausted"
        else:
            began = clock()
            try:
                context = retrieve(text, queries, [dict(target.scope_filter)] if target.scope_filter is not None else [])
                candidates = tuple(getattr(context, "evidence_tree", ()) or ())
                parents = tuple(parent for parent in candidates if target_records(target, [parent.record]))
                expansion = tuple(getattr(context, "expansion_records", ()) or ())
                state.update(execution="completed", acquisition="candidate_available" if parents else "no_candidates")
            except Exception as exc:  # noqa: BLE001 - 一対象の失敗は他の対象を消さない。
                error = type(exc).__name__
                state.update(execution="error", reason=error)
            elapsed = int((clock() - began) * 1000)
        if state["execution"] != "not_run":
            results.append(
                StepSearch(
                    target.owner_ids[0],
                    target.kind,
                    text,
                    queries,
                    parents,
                    expansion,
                    elapsed,
                    error,
                    "required",
                    target.target_id,
                    target.owner_ids,
                    is_reused,
                )
            )
        state["candidate_source_ids"] = list(dict.fromkeys(_key(parent) for parent in parents if _key(parent)))
        state["elapsed_ms"] = elapsed
        states.append(state)
    budget.reserve_required(0)
    return results, states, stop
