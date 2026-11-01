"""品質評価の判定の校正を画面で行う（記入の集合・記入・一致・裁定・校正。#3578）。

suite の CLI（`label-template` / `label-import` / `calibrate`）と RAG の
`python -m app.rag.answer_judge_calibration` の手順
（suite/evaluation/docs/judge-calibration.md）を、ブックの代わりに DB の記入で行う。
ロジックは作り直さない:

- 区分（dev / holdout）・誤りの型と目標・合成の対照・記入者の間の κ・判定の揺れ・観点の基準は
  platform の `pr_evaluation_scoring`（suite と共有）。
- 校正（評価器の実行・一致の集計・閾値の合否）は `app.rag.answer_judge_calibration`
  （`backend_evaluator`・`calibrate_evaluations`・`HumanLabels`）。
- 書き出し・取り込みの JSON は suite と同じ LabelSet の形
  （`LABEL_SET_FIELDS`・`LABEL_SET_ITEM_FIELDS`）。

記入者は自分の判定だけを見る（他の記入者と評価器の判定を返さない）。全員の判定・一致・裁定・校正・
書き出し・取り込みは、判定の校正の管理の権限（route が決める）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol

from pr_backend_core.bounded_calls import Attempt
from pr_evaluation_scoring import CalibrationThresholds
from pr_evaluation_scoring.labeling import (
    ANNOTATOR_ID,
    ASPECT_HOLDOUT_TARGET,
    ASPECT_LABEL_TARGET,
    DEFAULT_PERTURB_MAX,
    DEFAULT_SPLIT_SALT,
    ERROR_TYPE_IDS,
    ERROR_TYPES,
    HUMAN_LABELS,
    LABEL_CHOICES,
    NO_CATEGORY,
    agreed_label,
    assign_splits,
    item_order_key,
    label_variance,
    pairwise_agreement,
)
from pr_evaluation_scoring.perturbation import PERTURBATIONS, PerturbationSource, perturb_items
from pr_evaluation_scoring.rubric import applicable_aspects, aspect_criteria, load_rubric_data
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.clients.evaluation_calibration_store import (
    CalibrationRecord,
    EvaluationCalibrationStore,
    LabelItemRecord,
    LabelRecord,
    LabelRole,
    LabelSetRecord,
    MemoryEvaluationCalibrationStore,
    OracleEvaluationCalibrationStore,
)
from app.clients.oracle import (
    OracleClient,
    _json_loads,
    _oracle_tenant_predicate,
    _with_tenant_bind,
)
from app.rag.answer_judge_calibration import (
    ASPECT_METRICS,
    DEFAULT_PASS_AT,
    Evaluate,
    HumanLabels,
    LabeledAnswer,
    aspect_label,
    backend_evaluator,
    calibrate_evaluations,
    judge_prompt_sha256,
    judge_provenance,
    judge_rubric_version,
)
from app.rag.request_context import current_audit_request_context

logger = logging.getLogger(__name__)

RUBRIC = load_rubric_data()
ASPECTS: tuple[str, ...] = tuple(ASPECT_METRICS)
IMPORT_CODE = "import"
SOURCE_JOB_LIMIT = 50
ANNOTATION_ROWS_MAX_LIMIT = 100


class CalibrationError(ValueError):
    """利用者に返す、校正の操作の誤り（409 / 422）。"""

    safe_for_user = True


class LabelSetNotFoundError(LookupError):
    safe_for_user = True


class UnresolvedLabelsError(CalibrationError):
    """最終の判定の決まらない行が残っている（書き出し・校正の前に記入・裁定が要る）。"""


def _sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _user_hash() -> str | None:
    return current_audit_request_context().user_id_hash


@dataclass(frozen=True)
class JudgeIdentity:
    """今の評価器（判定のモデルと指示の sha256）。校正の結果と照らして「校正済み」を決める。"""

    model_id: str | None
    prompt_sha256: str


def current_judge_identity(settings: Any) -> JudgeIdentity:
    provenance = judge_provenance(settings)
    model = provenance.get("model")
    return JudgeIdentity(
        model_id=str(model) if model else None, prompt_sha256=judge_prompt_sha256()
    )


# ---- 記入の項目の元（終わった品質評価の job と回答の記録） ----------------------------------


@dataclass(frozen=True)
class SourceCase:
    """job の 1 ケースの回答（回答の記録の trace_id）。"""

    source: str
    case_id: str
    category: str | None
    trace_id: str


class CalibrationSourceReader(Protocol):
    """記入の項目の元を読む（利用者が見られる job と回答の記録だけ）。"""

    async def list_finished_jobs(self) -> list[dict[str, Any]]: ...

    async def job_result(self, job_id: str) -> Mapping[str, Any] | None: ...

    async def answer_record(self, trace_id: str) -> Mapping[str, Any] | None: ...


def _job_owner_clause(binds: dict[str, object]) -> str:
    """job の持ち主の条件（system_admin は全員の job。ほかは自分の job だけ）。"""
    context = current_audit_request_context()
    if context.all_users_visible:
        return "1 = 1"
    if context.user_id_hash is None:
        return "1 = 0"
    binds["source_user_id_hash"] = context.user_id_hash
    return "user_id_hash = :source_user_id_hash"


class OracleCalibrationSourceReader:
    """`rag_evaluation_jobs`（kind = run・成功）と `rag_answer_records` から読む。"""

    def __init__(self, oracle: OracleClient | None = None) -> None:
        self._oracle = oracle or OracleClient()

    async def list_finished_jobs(self) -> list[dict[str, Any]]:
        if not self._oracle.is_connection_configured():
            return []
        binds: dict[str, object] = dict(limit=SOURCE_JOB_LIMIT)
        owner = _job_owner_clause(binds)
        rows = await self._oracle._fetch_all(
            f"""
            SELECT job_id, created_at, finished_at, total_cases,
                   JSON_VALUE(result_json, '$.case_count' RETURNING NUMBER) AS case_count,
                   CASE WHEN JSON_EXISTS(result_json, '$.comparison.search_answer_profile_id')
                        THEN 1 ELSE 0 END AS compared
            FROM rag_evaluation_jobs
            WHERE kind = 'run' AND status = 'SUCCEEDED'
              AND {_oracle_tenant_predicate()} AND {owner}
            ORDER BY finished_at DESC NULLS LAST, job_id
            FETCH FIRST :limit ROWS ONLY
            """,
            _with_tenant_bind(binds),
        )
        return [dict(row) for row in rows]

    async def job_result(self, job_id: str) -> Mapping[str, Any] | None:
        binds: dict[str, object] = dict(job_id=job_id)
        owner = _job_owner_clause(binds)
        row = await self._oracle._fetch_one(
            f"""
            SELECT result_json FROM rag_evaluation_jobs
            WHERE job_id = :job_id AND kind = 'run' AND status = 'SUCCEEDED'
              AND {_oracle_tenant_predicate()} AND {owner}
            """,
            _with_tenant_bind(binds),
        )
        return _json_loads(row.get("result_json")) if row is not None else None

    async def answer_record(self, trace_id: str) -> Mapping[str, Any] | None:
        return await self._oracle.get_answer_record(trace_id)


def job_cases(job_id: str, result: Mapping[str, Any]) -> list[SourceCase]:
    """job の結果の成功したケースの回答（比べた検索・回答プロファイルの回答は別の元にする）。"""
    base = f"job-{job_id[:8]}"
    passes: list[tuple[str, Any]] = [(base, result.get("case_results"))]
    comparison = result.get("comparison")
    if isinstance(comparison, Mapping):
        metrics = comparison.get("metrics")
        if isinstance(metrics, Mapping):
            passes.append((f"{base}-cmp", metrics.get("case_results")))
    cases: list[SourceCase] = []
    for source, case_results in passes:
        for case in case_results if isinstance(case_results, list) else []:
            if not isinstance(case, Mapping) or case.get("status", "success") != "success":
                continue
            trace_id = case.get("trace_id")
            case_id = case.get("case_id")
            if not trace_id or not case_id:
                continue
            category = case.get("category")
            cases.append(
                SourceCase(
                    source=source,
                    case_id=str(case_id),
                    category=str(category) if category else None,
                    trace_id=str(trace_id),
                )
            )
    return cases


# ---- LabelSet（suite と同じ人手の判定の JSON） --------------------------------------------


class LabelSetEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=200)
    text: str


class LabelSetPerturbation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(..., min_length=1, max_length=80)
    base_item_id: str = Field(..., min_length=1, max_length=200)
    description: str = ""


class LabelSetItem(BaseModel):
    """人手の判定の 1 件（suite の `LabeledItem` と同じ項目。名前は `LABEL_SET_ITEM_FIELDS`）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., min_length=1, max_length=200)
    split: Literal["dev", "holdout"]
    question: str = Field(..., min_length=1)
    answer: str
    standard_answer: str | None = None
    evidence: list[LabelSetEvidence] = Field(default_factory=list)
    labels: dict[str, Literal["pass", "fail"]] = Field(..., min_length=1)
    labeler: str | None = None
    note: str | None = None
    origin: Literal["human", "synthetic"] = "human"
    perturbation: LabelSetPerturbation | None = None
    error_types: list[str] = Field(default_factory=list)
    annotator_labels: dict[str, dict[str, Literal["pass", "fail", "cannot_judge"]]] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def _origin(self) -> LabelSetItem:
        if (self.origin == "synthetic") != (self.perturbation is not None):
            raise ValueError("合成の項目には perturbation を、人手の項目には付けない: " + self.id)
        return self


class LabelSetDocument(BaseModel):
    """人手の判定の JSON（suite の `LabelSet` と同じ形。名前は `LABEL_SET_FIELDS`）。"""

    model_config = ConfigDict(extra="forbid")

    rubric_id: str
    description: str | None = None
    provenance: dict[str, Any] = Field(default_factory=dict)
    items: list[LabelSetItem] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> LabelSetDocument:
        ids = [item.id for item in self.items]
        duplicates = sorted(item for item in set(ids) if ids.count(item) > 1)
        if duplicates:
            raise ValueError("項目の id が重複しています: " + ", ".join(duplicates))
        return self


def rubric_provenance() -> dict[str, Any]:
    """記入の基準の rubric（suite の `Rubric.provenance` と同じ sha256）。"""
    return dict(id=RUBRIC["id"], version=RUBRIC["version"], sha256=_sha256(RUBRIC))


def aspect_criteria_list() -> list[dict[str, Any]]:
    instructions = str(RUBRIC["instructions"])
    result: list[dict[str, Any]] = []
    for aspect in RUBRIC["aspects"]:
        aspect_id = str(aspect["id"])
        if aspect_id not in ASPECT_METRICS:
            continue
        result.append(
            dict(
                aspect=aspect_id,
                metric=ASPECT_METRICS[aspect_id],
                criteria=aspect_criteria(instructions, aspect_id),
                requires_standard_answer="standard_answer" in (aspect.get("requires") or []),
            )
        )
    return result


def error_type_list() -> list[dict[str, Any]]:
    return [
        dict(
            id=item.id,
            group=item.group,
            title=item.title,
            aspects=item.aspects,
            holdout_target=item.holdout_target,
        )
        for item in ERROR_TYPES
    ]


# ---- 記入の項目を作る ----------------------------------------------------------------------


def _record_evidence(record: Mapping[str, Any]) -> list[dict[str, str]]:
    evaluation_input = record.get("evaluation_input_json")
    raw = evaluation_input.get("evidence_items") if isinstance(evaluation_input, Mapping) else None
    evidence: list[dict[str, str]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, Mapping):
            continue
        evidence_id = str(item.get("id") or item.get("chunk_uid") or "")
        if evidence_id:
            evidence.append(dict(id=evidence_id[:200], text=str(item.get("text") or "")))
    return evidence


def _record_standard_answer(record: Mapping[str, Any]) -> str | None:
    evaluation = record.get("evaluation_json")
    value = evaluation.get("standard_answer") if isinstance(evaluation, Mapping) else None
    return str(value) if isinstance(value, str) and value.strip() else None


def _ordered_items(
    payloads: Sequence[dict[str, Any]], *, salt: str, excluded: frozenset[str] = frozenset()
) -> list[LabelItemRecord]:
    """人の項目は sha256 の順（同じケースの回答が続かない）、合成の項目はその後ろ。"""
    human = sorted(
        (item for item in payloads if item["origin"] == "human"),
        key=lambda item: item_order_key(salt, item["id"]),
    )
    synthetic = [item for item in payloads if item["origin"] == "synthetic"]
    return [
        LabelItemRecord(
            item_id=item["id"],
            item_order=index,
            split=item["split"],
            origin=item["origin"],
            payload=item,
            excluded=item["id"] in excluded,
        )
        for index, item in enumerate([*human, *synthetic])
    ]


def items_sha256(items: Sequence[LabelItemRecord]) -> str:
    return _sha256(dict(rubric=rubric_provenance(), items=[item.payload for item in items]))


@dataclass
class BuiltItems:
    items: list[LabelItemRecord]
    sources: dict[str, Any]
    split_policy: dict[str, Any]


def _count(counter: dict[str, int], key: str) -> None:
    counter[key] = counter.get(key, 0) + 1


async def build_items_from_jobs(
    reader: CalibrationSourceReader,
    job_ids: Sequence[str],
    *,
    holdout_ratio: float,
    include_synthetic: bool,
    salt: str = DEFAULT_SPLIT_SALT,
) -> BuiltItems:
    """終わった品質評価の job の回答を、記入の項目にする（suite の `label-template` と同じ区分）。

    1 項目 = 1 つのケースの回答（回答の記録の質問・回答・評価器に渡した根拠・標準回答）。回答の
    記録が無い（保持期間で消えた・見られない）・回答が空の・観点の無いケースは項目にしない（件数を
    残す）。
    """
    drafts: list[dict[str, Any]] = []
    skipped: dict[str, int] = dict()
    seen: set[str] = set()
    jobs: list[dict[str, Any]] = []
    for job_id in dict.fromkeys(job_ids):
        result = await reader.job_result(job_id)
        if result is None:
            raise CalibrationError(
                "選んだ品質評価の結果が見つかりません。終わった評価を選んでください。"
            )
        cases = job_cases(job_id, result)
        jobs.append(dict(job_id=job_id, cases=len(cases)))
        for case in cases:
            item_id = case.source + ":" + case.case_id
            if item_id in seen:
                continue
            seen.add(item_id)
            record = await reader.answer_record(case.trace_id)
            if record is None:
                _count(skipped, "record_missing")
                continue
            answer = str(record.get("answer") or "")
            question = str(record.get("question") or "")
            if not answer.strip() or not question.strip():
                _count(skipped, "no_answer")
                continue
            standard_answer = _record_standard_answer(record)
            aspects = [
                aspect
                for aspect in applicable_aspects(RUBRIC, has_standard_answer=bool(standard_answer))
                if aspect in ASPECT_METRICS
            ]
            if not aspects:
                _count(skipped, "no_aspect")
                continue
            drafts.append(
                dict(
                    id=item_id,
                    origin="human",
                    source=case.source,
                    case_id=case.case_id,
                    category=case.category,
                    question=question,
                    answer=answer,
                    standard_answer=standard_answer,
                    evidence=_record_evidence(record),
                    aspects=aspects,
                    perturbation=None,
                    synthetic_labels=dict(),
                )
            )
    if not drafts:
        raise CalibrationError(
            "記入できる回答がありません。回答の記録が残っている、成功したケースのある評価を"
            "選んでください。"
        )
    strata: dict[str, str] = dict()
    for draft in drafts:
        strata.setdefault(draft["case_id"], draft["category"] or NO_CATEGORY)
    splits = assign_splits(strata, holdout_ratio=holdout_ratio, salt=salt)
    for draft in drafts:
        draft["split"] = splits[draft["case_id"]]
    kinds = list(PERTURBATIONS) if include_synthetic else []
    by_id = dict((draft["id"], draft) for draft in drafts)
    sources = [
        PerturbationSource(
            id=draft["id"],
            answer=draft["answer"],
            standard_answer=draft["standard_answer"],
            aspects=tuple(draft["aspects"]),
        )
        for draft in drafts
    ]
    for perturbed in perturb_items(sources, kinds, perturb_max=DEFAULT_PERTURB_MAX, salt=salt):
        base = by_id[perturbed.base_item_id]
        drafts.append(
            dict(
                base,
                id=perturbed.id,
                origin="synthetic",
                answer=perturbed.answer,
                aspects=list(perturbed.labels),
                perturbation=dict(
                    kind=perturbed.kind,
                    base_item_id=perturbed.base_item_id,
                    description=perturbed.description,
                ),
                synthetic_labels=dict(perturbed.labels),
            )
        )
    return BuiltItems(
        items=_ordered_items(drafts, salt=salt),
        sources=dict(jobs=jobs, skipped=skipped),
        split_policy=dict(
            unit="case_id",
            strata="category",
            holdout_ratio=holdout_ratio,
            salt=salt,
            perturb=kinds,
            perturb_max=DEFAULT_PERTURB_MAX,
        ),
    )


# ---- 行（項目 × 観点）の状態・進み具合・一致 -----------------------------------------------


@dataclass
class RowState:
    """人の項目 × 観点の、記入者の判定と裁定。"""

    item: LabelItemRecord
    aspect: str
    annotations: dict[str, LabelRecord] = field(default_factory=dict)
    adjudication: LabelRecord | None = None

    @property
    def agreed(self) -> str | None:
        labels: list[str | None] = [label.label for label in self.annotations.values()]
        return agreed_label(labels) if len(labels) >= 2 else None

    @property
    def status(self) -> Literal["pending", "agreed", "disagreed", "adjudicated"]:
        if self.adjudication is not None:
            return "adjudicated"
        if len(self.annotations) < 2:
            return "pending"
        return "agreed" if self.agreed is not None else "disagreed"

    @property
    def final(self) -> str | None:
        """最終の判定（裁定、無ければ 2 人以上の全員一致）。"""
        return self.adjudication.label if self.adjudication is not None else self.agreed

    def final_error_type(self) -> str | None:
        if self.adjudication is not None and self.adjudication.error_type:
            return self.adjudication.error_type
        types = {label.error_type for label in self.annotations.values() if label.error_type}
        return types.pop() if len(types) == 1 else None


def human_items(items: Sequence[LabelItemRecord]) -> list[LabelItemRecord]:
    return [item for item in items if item.origin == "human"]


def item_aspects(item: LabelItemRecord) -> list[str]:
    return [aspect for aspect in item.payload.get("aspects") or [] if aspect in ASPECT_METRICS]


def row_states(items: Sequence[LabelItemRecord], labels: Sequence[LabelRecord]) -> list[RowState]:
    """人の項目 × 観点の行（項目の並び × 観点の並び）。"""
    states: dict[tuple[str, str], RowState] = dict()
    ordered: list[RowState] = []
    for item in human_items(items):
        for aspect in ASPECTS:
            if aspect in item_aspects(item):
                state = RowState(item=item, aspect=aspect)
                states[(item.item_id, aspect)] = state
                ordered.append(state)
    for label in labels:
        target = states.get((label.item_id, label.aspect))
        if target is None:
            continue
        if label.role == "adjudication":
            target.adjudication = label
        else:
            target.annotations[label.annotator_code] = label
    return ordered


def progress(
    states: Sequence[RowState], picks: Mapping[tuple[str, str], LabelRecord | str]
) -> dict[str, Any]:
    """観点ごとの記入の進み具合（``picks`` は行 → 判定。holdout の pass / fail を目標と比べる）。"""
    aspects: list[dict[str, Any]] = []
    error_counts = dict((error_id, 0) for error_id in ERROR_TYPE_IDS)
    for aspect in ASPECTS:
        rows = [state for state in states if state.aspect == aspect]
        if not rows:
            continue
        counts = dict(labeled=0, cannot_judge=0, holdout_rows=0, holdout_pass=0, holdout_fail=0)
        for state in rows:
            holdout = state.item.split == "holdout"
            counts["holdout_rows"] += holdout
            pick = picks.get((state.item.item_id, aspect))
            if pick is None:
                continue
            label = pick.label if isinstance(pick, LabelRecord) else pick
            error_type = (
                pick.error_type if isinstance(pick, LabelRecord) else state.final_error_type()
            )
            counts["labeled"] += 1
            counts["cannot_judge"] += label == "cannot_judge"
            if holdout:
                counts["holdout_pass"] += label == "pass"
                counts["holdout_fail"] += label == "fail"
                if error_type in error_counts:
                    error_counts[error_type] += 1
        aspects.append(dict(aspect=aspect, rows=len(rows), **counts))
    return dict(
        aspects=aspects,
        error_types=[dict(id=key, holdout_count=value) for key, value in error_counts.items()],
        holdout_target=ASPECT_HOLDOUT_TARGET,
        label_target=ASPECT_LABEL_TARGET,
    )


def own_picks(
    states: Sequence[RowState], code: str | None
) -> dict[tuple[str, str], LabelRecord | str]:
    picks: dict[tuple[str, str], LabelRecord | str] = dict()
    if code is None:
        return picks
    for state in states:
        own = state.annotations.get(code)
        if own is not None:
            picks[(state.item.item_id, state.aspect)] = own
    return picks


def final_picks(states: Sequence[RowState]) -> dict[tuple[str, str], LabelRecord | str]:
    picks: dict[tuple[str, str], LabelRecord | str] = dict()
    for state in states:
        final = state.final
        if final is not None:
            picks[(state.item.item_id, state.aspect)] = final
    return picks


def annotator_codes(labels: Sequence[LabelRecord]) -> list[str]:
    codes = dict.fromkeys(label.annotator_code for label in labels if label.role == "annotation")
    return sorted(codes, key=_code_order)


def _code_order(code: str) -> tuple[int, str]:
    digits = code[1:] if code.startswith("a") else ""
    return (int(digits), code) if digits.isdigit() else (10**6, code)


def aspect_agreements(states: Sequence[RowState], codes: Sequence[str]) -> list[dict[str, Any]]:
    """観点ごとの記入者の間の一致（組ごとの κ）と、行の状態の数。"""
    result: list[dict[str, Any]] = []
    for aspect in ASPECTS:
        rows = [state for state in states if state.aspect == aspect]
        if not rows:
            continue
        by_code = dict(
            (
                code,
                dict(
                    (state.item.item_id, state.annotations[code].label)
                    for state in rows
                    if code in state.annotations
                ),
            )
            for code in codes
        )
        pairs = pairwise_agreement([state.item.item_id for state in rows], by_code)
        statuses = [state.status for state in rows]
        result.append(
            dict(
                aspect=aspect,
                rows=len(rows),
                agreed=statuses.count("agreed"),
                disagreed=statuses.count("disagreed"),
                adjudicated=statuses.count("adjudicated"),
                pending=statuses.count("pending"),
                pairs=[
                    dict(
                        annotators=list(pair.annotators),
                        compared=pair.compared,
                        agreement=pair.agreement,
                        kappa=pair.kappa,
                    )
                    for pair in pairs
                ],
            )
        )
    return result


def next_code(labels: Sequence[LabelRecord], role: LabelRole, user_hash: str) -> str:
    """利用者の集合の中の匿名の ID（記入者 ``a1``…・裁定者 ``adj1``…。実名を持たない）。"""
    prefix = "a" if role == "annotation" else "adj"
    for label in labels:
        if label.role == role and label.annotator_hash == user_hash:
            return label.annotator_code
    used = {label.annotator_code for label in labels if label.role == role}
    number = 1
    while prefix + str(number) in used:
        number += 1
    return prefix + str(number)


# ---- 書き出し・取り込み（LabelSet） --------------------------------------------------------


def _unresolved_message(states: Sequence[RowState]) -> str | None:
    open_rows = [state for state in states if state.final is None]
    if not open_rows:
        return None
    pending = sum(state.status == "pending" for state in open_rows)
    disagreed = len(open_rows) - pending
    return (
        f"最終の判定の決まらない行が {len(open_rows)} 行あります（記入の待ち {pending} 行・"
        f"要裁定 {disagreed} 行）。記入を終えるか、裁定してください。"
    )


def _all_labels(state: RowState) -> list[LabelRecord]:
    return [*state.annotations.values(), *([state.adjudication] if state.adjudication else [])]


def build_label_set_document(
    record: LabelSetRecord, items: Sequence[LabelItemRecord], labels: Sequence[LabelRecord]
) -> LabelSetDocument:
    """最終の判定（裁定・全員一致）から LabelSet を作る（suite の `label-import` の出力と同じ形）。

    決まらない行が残れば `UnresolvedLabelsError`。cannot_judge の観点は書かず、全観点が
    cannot_judge の項目は落とす。合成の対照は除外したもの以外を `origin=synthetic` で足す。
    """
    states = row_states(items, labels)
    message = _unresolved_message(states)
    if message:
        raise UnresolvedLabelsError(message)
    by_item: dict[str, list[RowState]] = dict()
    for state in states:
        by_item.setdefault(state.item.item_id, []).append(state)
    labeled: list[LabelSetItem] = []
    dropped: list[str] = []
    for item in human_items(items):
        rows = by_item.get(item.item_id, [])
        finals = dict((state.aspect, state.final) for state in rows if state.final in HUMAN_LABELS)
        if not finals:
            dropped.append(item.item_id)
            continue
        codes = sorted(set(code for state in rows for code in state.annotations), key=_code_order)
        adjudicators = sorted(
            set(state.adjudication.annotator_code for state in rows if state.adjudication)
        )
        sources = [label for state in rows for label in _all_labels(state)]
        notes = [label.annotator_code + ": " + label.note for label in sources if label.note]
        if item.payload.get("note"):
            notes.insert(0, str(item.payload["note"]))
        error_types = set(label.error_type for label in sources if label.error_type)
        error_types.update(item.payload.get("error_types") or [])
        payload = item.payload
        labeled.append(
            LabelSetItem(
                id=item.item_id,
                split=item.split,
                question=str(payload.get("question") or ""),
                answer=str(payload.get("answer") or ""),
                standard_answer=payload.get("standard_answer"),
                evidence=[LabelSetEvidence(**value) for value in payload.get("evidence") or []],
                labels=finals,
                labeler="+".join(adjudicators or codes) or None,
                note="\n".join(notes) or None,
                error_types=sorted(error_types),
                annotator_labels=dict(
                    (
                        code,
                        dict(
                            (state.aspect, state.annotations[code].label)
                            for state in rows
                            if code in state.annotations
                        ),
                    )
                    for code in codes
                ),
            )
        )
    synthetic = [item for item in items if item.origin == "synthetic"]
    excluded = sorted(item.item_id for item in synthetic if item.excluded)
    for item in synthetic:
        if item.excluded:
            continue
        payload = item.payload
        labeled.append(
            LabelSetItem(
                id=item.item_id,
                split=item.split,
                question=str(payload.get("question") or ""),
                answer=str(payload.get("answer") or ""),
                standard_answer=payload.get("standard_answer"),
                evidence=[LabelSetEvidence(**value) for value in payload.get("evidence") or []],
                labels=dict(payload.get("synthetic_labels") or dict()),
                labeler="synthetic",
                origin="synthetic",
                perturbation=LabelSetPerturbation(**(payload.get("perturbation") or dict())),
            )
        )
    if not labeled:
        raise UnresolvedLabelsError("pass / fail の判定のある項目がありません。")
    codes = annotator_codes(labels)
    adjudicator_codes = sorted(
        set(label.annotator_code for label in labels if label.role == "adjudication")
    )
    provenance: dict[str, Any] = dict(
        label_set_id=record.label_set_id,
        items_sha256=record.items_sha256,
        rubric=dict(
            id=record.rubric_id, version=record.rubric_version, sha256=record.rubric_sha256
        ),
        split_policy=record.split_policy,
        sources=record.sources,
        annotators=codes,
        adjudicator="+".join(adjudicator_codes) or None,
        dropped_cannot_judge_items=len(dropped),
        synthetic=dict(items=len(synthetic), excluded=excluded),
        inter_annotator=dict(
            (
                entry["aspect"],
                dict(
                    rows=entry["rows"],
                    disagreements=entry["disagreed"] + entry["adjudicated"],
                    kappa=[pair["kappa"] for pair in entry["pairs"]],
                ),
            )
            for entry in aspect_agreements(states, codes)
        ),
    )
    return LabelSetDocument(
        rubric_id=record.rubric_id,
        description="RAG の判定の校正の画面で作った人手の判定（#3578）。",
        provenance=provenance,
        items=labeled,
    )


@dataclass
class ImportedLabelSet:
    items: list[LabelItemRecord]
    labels: list[LabelRecord]
    split_policy: dict[str, Any]
    sources: dict[str, Any]


def _check_code(code: str) -> str:
    if not ANNOTATOR_ID.match(code):
        raise CalibrationError(
            "記入者の ID は英数字と _ . - の 40 文字までにしてください"
            "（実名・メールは書かない）: " + code[:40]
        )
    return code


def _import_final(item: LabelSetItem, aspect: str) -> tuple[str, str | None]:
    """取り込む行の最終の判定と、記入者の全員一致（2 人以上のとき）。"""
    annotated: list[str | None] = [
        values[aspect] for values in item.annotator_labels.values() if aspect in values
    ]
    agreed = agreed_label(annotated) if len(annotated) >= 2 else None
    # 最終の判定に無い観点は cannot_judge だった（suite の LabelSet は書かない）。
    final: str = item.labels.get(aspect) or "cannot_judge"
    return final, agreed


def import_label_set(document: LabelSetDocument) -> ImportedLabelSet:
    """LabelSet（suite の `label-import` の出力か、画面の書き出し）を記入の集合にする。

    記入者ごとの元の判定（`annotator_labels`）は記入者の判定に、最終の判定が記入者の全員一致と
    違う（か記入者が 2 人に満たない）行は裁定にする。合成の項目は除外しない対照として入れる。
    """
    if document.rubric_id != RUBRIC["id"]:
        raise CalibrationError(
            "rubric が違います（"
            + document.rubric_id
            + "）。"
            + str(RUBRIC["id"])
            + " の判定の JSON を選んでください。"
        )
    payloads: list[dict[str, Any]] = []
    labels: list[LabelRecord] = []
    for item in document.items:
        named = set(item.labels)
        for values in item.annotator_labels.values():
            named.update(values)
        unknown = sorted(named.difference(ASPECT_METRICS))
        if unknown:
            raise CalibrationError("知らない観点の判定です: " + ", ".join(unknown))
        bad_types = sorted(set(item.error_types).difference(ERROR_TYPE_IDS))
        if bad_types:
            raise CalibrationError("知らない誤りの型・対照です: " + ", ".join(bad_types))
        base = dict(
            id=item.id,
            split=item.split,
            source="import",
            case_id=item.id,
            category=None,
            question=item.question,
            answer=item.answer,
            standard_answer=item.standard_answer,
            evidence=[value.model_dump() for value in item.evidence],
        )
        if item.origin == "synthetic":
            payloads.append(
                dict(
                    base,
                    origin="synthetic",
                    aspects=list(item.labels),
                    perturbation=item.perturbation.model_dump() if item.perturbation else None,
                    synthetic_labels=dict(item.labels),
                )
            )
            continue
        aspects = [aspect for aspect in ASPECTS if aspect in named]
        payloads.append(
            dict(
                base,
                origin="human",
                aspects=aspects,
                perturbation=None,
                synthetic_labels=dict(),
                error_types=list(item.error_types),
                note=item.note,
            )
        )
        for code, values in item.annotator_labels.items():
            for aspect, label in values.items():
                labels.append(
                    LabelRecord(
                        item_id=item.id,
                        aspect=aspect,
                        role="annotation",
                        annotator_code=_check_code(code),
                        label=label,
                    )
                )
        labeler = item.labeler or ""
        adjudicator = labeler if ANNOTATOR_ID.match(labeler) else IMPORT_CODE
        for aspect in aspects:
            final, agreed = _import_final(item, aspect)
            if final != agreed:
                labels.append(
                    LabelRecord(
                        item_id=item.id,
                        aspect=aspect,
                        role="adjudication",
                        annotator_code=adjudicator,
                        label=final,
                    )
                )
    policy = document.provenance.get("split_policy")
    split_policy = dict(policy) if isinstance(policy, Mapping) else dict()
    salt = str(split_policy.get("salt") or DEFAULT_SPLIT_SALT)
    split_policy.setdefault("salt", salt)
    return ImportedLabelSet(
        items=_ordered_items(payloads, salt=salt),
        labels=labels,
        split_policy=split_policy,
        sources=dict(imported=dict(provenance_keys=sorted(document.provenance))),
    )


def parse_label_set(content: bytes) -> LabelSetDocument:
    """取り込む JSON を読む（形が違えば `CalibrationError`。本文は返さない）。"""
    try:
        return LabelSetDocument.model_validate_json(content)
    except ValidationError as exc:
        errors = exc.errors()
        location = ".".join(str(part) for part in errors[0]["loc"]) if errors else ""

        raise CalibrationError(
            "人手の判定の JSON（LabelSet）の形が違います（" + location + "）。"
        ) from exc


# ---- 校正（RAG の回答の評価器を最終の判定と照らす） -----------------------------------------


SPLIT_ORDER = ("holdout", "dev", "synthetic")
_SPLIT_KEYS = (
    "total",
    "judged",
    "coverage",
    "accuracy",
    "kappa",
    "false_pass_rate",
    "false_fail_rate",
)


def summarize_report(
    report: Mapping[str, Any], variance: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """校正の結果の観点 × 区分の一致と合否（画面と「校正済み」の判定に使う小さな要約）。"""
    aspects: list[dict[str, Any]] = []
    for aspect, entry in (report.get("aspects") or dict()).items():
        splits = entry.get("splits") or dict()
        aspects.append(
            dict(
                aspect=aspect,
                passed=bool(entry.get("passed")),
                failures=list(entry.get("failures") or []),
                splits=[
                    dict(dict((key, splits[split].get(key)) for key in _SPLIT_KEYS), split=split)
                    for split in SPLIT_ORDER
                    if split in splits
                ],
                variance=variance.get(aspect),
            )
        )
    return dict(aspects=aspects, passed=bool(report.get("passed")))


def _variance(
    labels: HumanLabels, runs: Sequence[Mapping[str, Mapping[str, Any]]], pass_at: float
) -> dict[str, dict[str, Any]]:
    """観点ごとに、回の間で評価器のラベル（判定できなかったことを含む）が変わった項目の率。"""
    if len(runs) < 2:
        return dict()
    result: dict[str, dict[str, Any]] = dict()
    for aspect in ASPECTS:
        targets = [item.id for item in labels.items if aspect in item.labels]
        outcomes = [
            dict(
                (item_id, aspect_label(run.get(item_id), aspect, pass_at=pass_at) or "-")
                for item_id in targets
            )
            for run in runs
        ]
        measured = label_variance(outcomes)
        result[aspect] = dict(
            runs=measured.runs,
            items=measured.items,
            unstable=measured.unstable,
            unstable_rate=measured.unstable_rate,
        )
    return result


class CalibrationProgress(Protocol):
    async def __call__(self, *, completed_cases: int, current_case_id: str | None) -> None: ...


@dataclass
class CalibrationOutcome:
    report: dict[str, Any]
    summary: dict[str, Any]
    judge_rubric_version: str | None


async def run_calibration(
    labels: HumanLabels,
    *,
    repeats: int,
    settings: Any,
    progress: CalibrationProgress,
    evaluator: tuple[Evaluate, Mapping[str, Any]] | None = None,
) -> CalibrationOutcome:
    """最終の判定の項目ごとに評価器を `repeats` 回動かし、`calibrate_evaluations` で集計する。

    評価器・一致の集計・閾値は `python -m app.rag.answer_judge_calibration` と同じ。並行の数と
    流量の制限の待ちは品質評価と同じ実行器（`make_ai_caller(settings, "evaluation")`）。合否は
    最初の回で決め、2 回目以降は揺れ（`variance`）だけに使う。
    """
    from rag_engine.evaluation.answer_eval import standard_scope_cache

    from app.clients.ai_calls import make_ai_caller

    evaluate, provenance = evaluator or backend_evaluator(settings)
    caller = make_ai_caller(settings, "evaluation")
    completed = 0
    runs: list[dict[str, dict[str, Any]]] = []
    await progress(completed_cases=0, current_case_id=labels.items[0].id)
    for _repeat in range(repeats):

        async def judge(item: LabeledAnswer, _attempt: Attempt) -> dict[str, Any]:
            nonlocal completed
            value = dict(await asyncio.to_thread(evaluate, item))
            completed += 1
            await progress(completed_cases=completed, current_case_id=item.id)
            return value

        # 回ごとに標準回答の比較範囲を固定し直す（回の間の揺れに範囲の固定も含める）。
        with standard_scope_cache():
            outcomes = await caller.map(
                labels.items, judge, labels=[item.id for item in labels.items]
            )
        runs.append(
            dict(
                (
                    item.id,
                    outcome.value
                    if outcome.ok and outcome.value is not None
                    else dict(status="error", message="評価を完了できませんでした。"),
                )
                for item, outcome in zip(labels.items, outcomes, strict=True)
            )
        )
    report = calibrate_evaluations(
        labels, runs[0], thresholds=CalibrationThresholds(), evaluator=dict(provenance)
    )
    variance = _variance(labels, runs, DEFAULT_PASS_AT)
    version = judge_rubric_version(runs[0])
    report["provenance"]["judge_prompt_sha256"] = judge_prompt_sha256()
    report["provenance"]["judge_rubric_version"] = version
    report["provenance"]["repeats"] = repeats
    report["variance"] = variance
    report["repeat_evaluations"] = runs[1:]
    await progress(completed_cases=completed, current_case_id=None)
    return CalibrationOutcome(
        report=report, summary=summarize_report(report, variance), judge_rubric_version=version
    )


def calibration_summary(record: CalibrationRecord, identity: JudgeIdentity) -> dict[str, Any]:
    """校正の結果の画面の形（`CalibrationSummary`）。"""
    return dict(
        calibration_id=record.calibration_id,
        label_set_id=record.label_set_id,
        status=record.status,
        created_at=record.created_at,
        finished_at=record.finished_at,
        repeats=record.repeats,
        judge_model_id=record.judge_model_id,
        judge_rubric_version=record.judge_rubric_version,
        prompt_sha256=record.prompt_sha256,
        labels_sha256=record.labels_sha256,
        passed=record.passed,
        error_message=record.error_message,
        aspects=list(record.summary.get("aspects") or []),
        current=record.judge_model_id == identity.model_id
        and record.prompt_sha256 == identity.prompt_sha256,
    )


def _holdout_kappa(entry: Mapping[str, Any]) -> float | None:
    for split in entry.get("splits") or []:
        if split.get("split") == "holdout":
            value = split.get("kappa")
            return float(value) if isinstance(value, int | float) else None
    return None


def judge_status(
    records: Sequence[CalibrationRecord], identity: JudgeIdentity
) -> list[dict[str, Any]]:
    """観点ごとに、今の評価器（モデル・指示）で合格した最新の校正（無ければ未校正）。"""
    matching = sorted(
        (
            record
            for record in records
            if record.status == "SUCCEEDED"
            and record.judge_model_id == identity.model_id
            and record.prompt_sha256 == identity.prompt_sha256
        ),
        key=lambda record: record.finished_at or record.created_at,
        reverse=True,
    )
    result: list[dict[str, Any]] = []
    for aspect, metric in ASPECT_METRICS.items():
        status: dict[str, Any] = dict(aspect=aspect, metric=metric, calibrated=False)
        for record in matching:
            entry = next(
                (
                    value
                    for value in record.summary.get("aspects") or []
                    if value.get("aspect") == aspect
                ),
                None,
            )
            if entry is not None and entry.get("passed"):
                status.update(
                    calibrated=True,
                    kappa=_holdout_kappa(entry),
                    calibration_id=record.calibration_id,
                    calibrated_at=record.finished_at or record.created_at,
                    judge_rubric_version=record.judge_rubric_version,
                )
                break
        result.append(status)
    return result


# ---- 保存先 --------------------------------------------------------------------------------

_STORE: EvaluationCalibrationStore | None = None
_MEMORY_STORE: MemoryEvaluationCalibrationStore | None = None
_READER: CalibrationSourceReader | None = None


def get_calibration_store() -> EvaluationCalibrationStore:
    """保存先（Oracle の接続設定が無い開発環境はプロセスの中の memory）。"""
    global _MEMORY_STORE
    if _STORE is not None:
        return _STORE
    client = OracleClient()
    if client.is_connection_configured():
        return OracleEvaluationCalibrationStore(client)
    if _MEMORY_STORE is None:
        _MEMORY_STORE = MemoryEvaluationCalibrationStore()
    return _MEMORY_STORE


def set_calibration_store(store: EvaluationCalibrationStore | None) -> None:
    """テスト用に保存先を差し替える。"""
    global _STORE
    _STORE = store


def get_source_reader() -> CalibrationSourceReader:
    return _READER or OracleCalibrationSourceReader()


def set_source_reader(reader: CalibrationSourceReader | None) -> None:
    """テスト用に記入の項目の元を差し替える。"""
    global _READER
    _READER = reader


def new_label_set_id() -> str:
    return uuid.uuid4().hex


def label_choices() -> tuple[str, ...]:
    return LABEL_CHOICES


def created_at_key(record: LabelSetRecord) -> datetime:
    return record.created_at
