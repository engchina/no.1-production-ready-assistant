"""品質評価の判定の校正の API（記入の集合・記入・一致・裁定・校正・LabelSet の入出力。#3578）。

記入（自分の判定だけ）は品質評価の権限、全員の判定・一致・裁定・校正・書き出し・取り込みは判定の
校正の管理の権限（`rag.evaluation_calibration.manage`。manifest が route ごとに決める）。記入の行は
自分の判定だけを返し、他の記入者・評価器の判定を返さない（blind）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Sequence
from pathlib import PurePath
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, Response, UploadFile
from pr_evaluation_scoring.labeling import ERROR_TYPE_IDS

from app.clients.evaluation_calibration_store import (
    CalibrationFinish,
    CalibrationRecord,
    EvaluationCalibrationStore,
    LabelRecord,
    LabelRole,
    LabelSetRecord,
)
from app.config import get_settings
from app.rag.answer_judge_calibration import HumanLabels
from app.rag.evaluation import EvaluationProgressCallback
from app.rag.evaluation_calibration import (
    ANNOTATION_ROWS_MAX_LIMIT,
    CalibrationError,
    RowState,
    UnresolvedLabelsError,
    annotator_codes,
    aspect_agreements,
    aspect_criteria_list,
    build_items_from_jobs,
    build_label_set_document,
    calibration_summary,
    current_judge_identity,
    error_type_list,
    final_picks,
    get_calibration_store,
    get_source_reader,
    import_label_set,
    item_aspects,
    items_sha256,
    judge_status,
    new_label_set_id,
    next_code,
    own_picks,
    parse_label_set,
    progress,
    row_states,
    rubric_provenance,
    run_calibration,
)
from app.rag.evaluation_jobs import (
    EvaluationJobLimitError,
    EvaluationJobOutcome,
    evaluation_job_failed_message,
    evaluation_job_time_limit_seconds,
    get_evaluation_job_service,
)
from app.rag.request_context import current_audit_request_context
from app.schemas.common import ApiResponse
from app.schemas.evaluation_calibration import (
    AgreementData,
    AnnotationRow,
    AnnotationRowsData,
    AspectCriterion,
    CalibrationListData,
    CalibrationRequest,
    CalibrationSourceJob,
    CalibrationSourcesData,
    CalibrationStartData,
    CalibrationSummary,
    ErrorTypeInfo,
    JudgeCalibrationStatus,
    LabelEvidence,
    LabelInput,
    LabelProgress,
    LabelSetCreateRequest,
    LabelSetDetail,
    LabelSetImportData,
    LabelSetListData,
    LabelSetSummary,
    SyntheticExcludeRequest,
    SyntheticItem,
    SyntheticItemsData,
)
from app.security.dependencies import current_principal
from app.security.permissions import EVALUATION_CALIBRATION_MANAGE

router = APIRouter()
logger = logging.getLogger(__name__)

LABEL_SET_IMPORT_MAX_BYTES = 20 * 1024 * 1024
LABEL_SET_NOT_FOUND = "記入の集合が見つかりません。"
CALIBRATION_CANCELLED_MESSAGE = "利用者の操作か、サービスの停止で校正を取り消しました。"
RowFilter = Literal["all", "unlabeled", "labeled"]
AgreementFilter = Literal["all", "disagreed", "pending", "agreed", "adjudicated"]


def _can_manage(request: Request) -> bool:
    try:
        return current_principal(request).has_permission(EVALUATION_CALIBRATION_MANAGE)
    except HTTPException:
        return False


def _me() -> str:
    user_hash = current_audit_request_context().user_id_hash
    if user_hash is None:
        raise HTTPException(status_code=403, detail="利用者が分からないため記入できません。")
    return user_hash


async def _label_set(store: EvaluationCalibrationStore, label_set_id: str) -> LabelSetRecord:
    record = await store.get_label_set(label_set_id)
    if record is None:
        raise HTTPException(status_code=404, detail=LABEL_SET_NOT_FOUND)
    return record


def _resolved_rows(labels: Sequence[LabelRecord]) -> int:
    """最終の判定の決まった行（裁定か、2 人以上の全員一致）の数（項目を読まずに数える）。"""
    rows: dict[tuple[str, str], list[LabelRecord]] = dict()
    for label in labels:
        rows.setdefault((label.item_id, label.aspect), []).append(label)
    resolved = 0
    for values in rows.values():
        if any(value.role == "adjudication" for value in values):
            resolved += 1
            continue
        annotated = [value.label for value in values]
        resolved += len(annotated) >= 2 and len(set(annotated)) == 1
    return resolved


def _summary(
    record: LabelSetRecord,
    labels: Sequence[LabelRecord],
    *,
    me: str | None,
    latest: CalibrationRecord | None,
    can_manage: bool,
) -> LabelSetSummary:
    counts = record.sources.get("counts") or dict()
    mine = [label for label in labels if label.role == "annotation" and label.annotator_hash == me]
    identity = current_judge_identity(get_settings())
    return LabelSetSummary(
        label_set_id=record.label_set_id,
        name=record.name,
        origin=record.origin,
        created_at=record.created_at,
        human_item_count=int(counts.get("human") or 0),
        synthetic_item_count=int(counts.get("synthetic") or 0),
        row_count=int(counts.get("rows") or 0),
        annotator_count=len(annotator_codes(labels)),
        my_labeled=len(mine),
        resolved_rows=_resolved_rows(labels),
        created_by_me=me is not None and record.created_by_hash == me,
        latest_calibration=CalibrationSummary.model_validate(calibration_summary(latest, identity))
        if latest is not None and can_manage
        else None,
    )


@router.get("/status", response_model=ApiResponse[JudgeCalibrationStatus])
async def get_judge_calibration_status() -> ApiResponse[JudgeCalibrationStatus]:
    """今の評価器（判定のモデル・指示）の観点ごとの校正の状態（品質評価の結果の表示）。"""
    identity = current_judge_identity(get_settings())
    records = await get_calibration_store().list_calibrations(status="SUCCEEDED")
    return ApiResponse(
        data=JudgeCalibrationStatus.model_validate(
            dict(
                judge_model_id=identity.model_id,
                prompt_sha256=identity.prompt_sha256,
                aspects=judge_status(records, identity),
            )
        )
    )


@router.get("/sources", response_model=ApiResponse[CalibrationSourcesData])
async def list_calibration_sources() -> ApiResponse[CalibrationSourcesData]:
    """記入の集合を作れる、終わった品質評価（自分の評価。system_admin は全員の評価）。"""
    rows = await get_source_reader().list_finished_jobs()
    jobs = [
        CalibrationSourceJob(
            job_id=str(row["job_id"]),
            created_at=row["created_at"],
            finished_at=row.get("finished_at"),
            case_count=int(row.get("case_count") or row.get("total_cases") or 0),
            compared=str(row.get("compared") or 0) == "1",
        )
        for row in rows
    ]
    return ApiResponse(data=CalibrationSourcesData(jobs=jobs))


@router.get("/label-sets", response_model=ApiResponse[LabelSetListData])
async def list_label_sets(request: Request) -> ApiResponse[LabelSetListData]:
    store = get_calibration_store()
    can_manage = _can_manage(request)
    me = current_audit_request_context().user_id_hash
    records = await store.list_label_sets()
    latest: dict[str, CalibrationRecord] = dict()
    if can_manage:
        for calibration in await store.list_calibrations():
            latest.setdefault(calibration.label_set_id, calibration)
    items = [
        _summary(
            record,
            await store.list_labels(record.label_set_id),
            me=me,
            latest=latest.get(record.label_set_id),
            can_manage=can_manage,
        )
        for record in records
    ]
    return ApiResponse(data=LabelSetListData(items=items, can_manage=can_manage))


async def _created(
    store: EvaluationCalibrationStore, record: LabelSetRecord, request: Request
) -> LabelSetSummary:
    labels = await store.list_labels(record.label_set_id)
    return _summary(
        record,
        labels,
        me=current_audit_request_context().user_id_hash,
        latest=None,
        can_manage=_can_manage(request),
    )


def _new_record(
    *,
    name: str,
    origin: Literal["jobs", "import"],
    items: Sequence[Any],
    split_policy: dict[str, Any],
    sources: dict[str, Any],
) -> LabelSetRecord:
    human = [item for item in items if item.origin == "human"]
    rubric = rubric_provenance()
    return LabelSetRecord(
        label_set_id=new_label_set_id(),
        name=name,
        origin=origin,
        rubric_id=rubric["id"],
        rubric_version=rubric["version"],
        rubric_sha256=rubric["sha256"],
        items_sha256=items_sha256(items),
        item_count=len(items),
        split_policy=split_policy,
        sources=dict(
            sources,
            counts=dict(
                human=len(human),
                synthetic=len(items) - len(human),
                rows=sum(len(item_aspects(item)) for item in human),
            ),
        ),
        created_by_hash=current_audit_request_context().user_id_hash,
    )


@router.post("/label-sets", response_model=ApiResponse[LabelSetSummary], status_code=201)
async def create_label_set(
    request: Request, body: LabelSetCreateRequest
) -> ApiResponse[LabelSetSummary]:
    """終わった品質評価の回答から記入の集合を作る（区分は suite の `label-template` と同じ）。"""
    store = get_calibration_store()
    try:
        built = await build_items_from_jobs(
            get_source_reader(),
            body.job_ids,
            holdout_ratio=body.holdout_ratio,
            include_synthetic=body.include_synthetic,
        )
    except CalibrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    record = _new_record(
        name=body.name.strip(),
        origin="jobs",
        items=built.items,
        split_policy=built.split_policy,
        sources=built.sources,
    )
    await store.create_label_set(record, built.items, [])
    return ApiResponse(data=await _created(store, record, request))


@router.post("/label-sets/import", response_model=ApiResponse[LabelSetImportData], status_code=201)
async def import_label_set_file(
    request: Request,
    file: Annotated[UploadFile, File()],
    name: Annotated[str | None, Form(max_length=200)] = None,
) -> ApiResponse[LabelSetImportData]:
    """人手の判定の JSON（LabelSet。suite の `label-import` の出力と同じ形）を取り込む。"""
    content = await file.read(LABEL_SET_IMPORT_MAX_BYTES + 1)
    if len(content) > LABEL_SET_IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="JSON が大きすぎます（20 MB まで）。")
    try:
        imported = import_label_set(parse_label_set(content))
    except CalibrationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    store = get_calibration_store()
    file_name = PurePath(file.filename or "").stem
    record = _new_record(
        name=(name or "").strip() or file_name or "取り込んだ人手の判定",
        origin="import",
        items=imported.items,
        split_policy=imported.split_policy,
        sources=imported.sources,
    )
    await store.create_label_set(record, imported.items, imported.labels)
    return ApiResponse(
        data=LabelSetImportData(
            label_set=await _created(store, record, request),
            imported_items=len(imported.items),
            imported_labels=len(imported.labels),
        )
    )


def _my_code(labels: Sequence[LabelRecord], me: str | None) -> str | None:
    if me is None:
        return None
    return next(
        (
            label.annotator_code
            for label in labels
            if label.role == "annotation" and label.annotator_hash == me
        ),
        None,
    )


@router.get("/label-sets/{label_set_id}", response_model=ApiResponse[LabelSetDetail])
async def get_label_set(label_set_id: str, request: Request) -> ApiResponse[LabelSetDetail]:
    """記入の集合と、観点の基準・誤りの型・自分の進み具合（管理の権限なら最終の判定の進み具合）。"""
    store = get_calibration_store()
    record = await _label_set(store, label_set_id)
    can_manage = _can_manage(request)
    me = current_audit_request_context().user_id_hash
    items = await store.list_items(label_set_id)
    labels = await store.list_labels(label_set_id)
    states = row_states(items, labels)
    my_code = _my_code(labels, me)
    latest = None
    if can_manage:
        calibrations = await store.list_calibrations(label_set_id=label_set_id)
        latest = calibrations[0] if calibrations else None
    return ApiResponse(
        data=LabelSetDetail(
            summary=_summary(record, labels, me=me, latest=latest, can_manage=can_manage),
            rubric_id=record.rubric_id,
            rubric_version=record.rubric_version,
            aspects=[AspectCriterion.model_validate(item) for item in aspect_criteria_list()],
            error_types=[ErrorTypeInfo.model_validate(item) for item in error_type_list()],
            my_code=my_code,
            my_progress=LabelProgress.model_validate(progress(states, own_picks(states, my_code))),
            final_progress=LabelProgress.model_validate(progress(states, final_picks(states)))
            if can_manage
            else None,
            can_manage=can_manage,
            skipped=dict(record.sources.get("skipped") or dict()),
            holdout_ratio=record.split_policy.get("holdout_ratio"),
        )
    )


def _row_evidence(payload: dict[str, Any]) -> list[LabelEvidence]:
    return [
        LabelEvidence(id=str(value.get("id") or ""), text=str(value.get("text") or ""))
        for value in payload.get("evidence") or []
        if isinstance(value, dict)
    ]


def _annotation_row(state: RowState, own: LabelRecord | None) -> AnnotationRow:
    payload = state.item.payload
    return AnnotationRow.model_validate(
        dict(
            item_id=state.item.item_id,
            aspect=state.aspect,
            split=state.item.split,
            question=str(payload.get("question") or ""),
            answer=str(payload.get("answer") or ""),
            standard_answer=payload.get("standard_answer"),
            evidence=_row_evidence(payload),
            label=own.label if own else None,
            error_type=own.error_type if own else None,
            note=own.note if own else None,
        )
    )


@router.get("/label-sets/{label_set_id}/rows", response_model=ApiResponse[AnnotationRowsData])
async def list_annotation_rows(
    label_set_id: str,
    aspect: Annotated[str | None, Query(max_length=80)] = None,
    status: Annotated[RowFilter, Query()] = "all",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=ANNOTATION_ROWS_MAX_LIMIT)] = 1,
) -> ApiResponse[AnnotationRowsData]:
    """記入の行（項目 × 観点）と自分の判定だけ（他の記入者・評価器の判定は返さない）。"""
    store = get_calibration_store()
    await _label_set(store, label_set_id)
    me = _me()
    items = await store.list_items(label_set_id)
    labels = await store.list_labels(label_set_id, role="annotation", annotator_hash=me)
    states = [
        state for state in row_states(items, labels) if aspect is None or state.aspect == aspect
    ]
    code = labels[0].annotator_code if labels else None

    def own(state: RowState) -> LabelRecord | None:
        return state.annotations.get(code) if code else None

    unlabeled = sum(own(state) is None for state in states)
    if status == "unlabeled":
        states = [state for state in states if own(state) is None]
    elif status == "labeled":
        states = [state for state in states if own(state) is not None]
    page = states[offset : offset + limit]
    return ApiResponse(
        data=AnnotationRowsData(
            items=[_annotation_row(state, own(state)) for state in page],
            total=len(states),
            offset=offset,
            limit=limit,
            unlabeled=unlabeled,
        )
    )


async def _save(
    store: EvaluationCalibrationStore, label_set_id: str, body: LabelInput, role: LabelRole
) -> list[RowState]:
    me = _me()
    if body.error_type is not None and body.error_type not in ERROR_TYPE_IDS:
        raise HTTPException(status_code=422, detail="知らない誤りの型・対照です。")
    items = await store.list_items(label_set_id)
    labels = await store.list_labels(label_set_id)
    item = next((value for value in items if value.item_id == body.item_id), None)
    if item is None or item.origin != "human" or body.aspect not in item_aspects(item):
        raise HTTPException(status_code=404, detail="記入の行が見つかりません。")
    code = next_code(labels, role, me)
    if body.label is None:
        await store.delete_label(
            label_set_id, item_id=body.item_id, aspect=body.aspect, role=role, annotator_code=code
        )
    else:
        await store.save_label(
            label_set_id,
            LabelRecord(
                item_id=body.item_id,
                aspect=body.aspect,
                role=role,
                annotator_code=code,
                annotator_hash=me,
                label=body.label,
                error_type=body.error_type or None,
                note=(body.note or "").strip() or None,
            ),
        )
    return row_states(items, await store.list_labels(label_set_id))


@router.put("/label-sets/{label_set_id}/labels", response_model=ApiResponse[LabelProgress])
async def save_label(label_set_id: str, body: LabelInput) -> ApiResponse[LabelProgress]:
    """自分の判定を保存する（``label`` が null なら消す）。返すのは自分の進み具合だけ。"""
    store = get_calibration_store()
    await _label_set(store, label_set_id)
    states = await _save(store, label_set_id, body, "annotation")
    labels = await store.list_labels(label_set_id)
    code = _my_code(labels, _me())
    return ApiResponse(data=LabelProgress.model_validate(progress(states, own_picks(states, code))))


def _agreement_row(state: RowState) -> dict[str, Any]:
    payload = state.item.payload
    adjudication = state.adjudication
    return dict(
        item_id=state.item.item_id,
        aspect=state.aspect,
        split=state.item.split,
        status=state.status,
        question=str(payload.get("question") or ""),
        answer=str(payload.get("answer") or ""),
        standard_answer=payload.get("standard_answer"),
        evidence=_row_evidence(payload),
        annotations=[
            dict(code=code, label=label.label, error_type=label.error_type, note=label.note)
            for code, label in sorted(state.annotations.items())
        ],
        final_label=state.final,
        adjudication=dict(
            code=adjudication.annotator_code,
            label=adjudication.label,
            error_type=adjudication.error_type,
            note=adjudication.note,
        )
        if adjudication
        else None,
    )


async def _agreement(
    store: EvaluationCalibrationStore,
    label_set_id: str,
    *,
    aspect: str | None,
    status: AgreementFilter,
    offset: int,
    limit: int,
) -> AgreementData:
    items = await store.list_items(label_set_id)
    labels = await store.list_labels(label_set_id)
    states = row_states(items, labels)
    codes = annotator_codes(labels)
    rows = [
        state
        for state in states
        if (aspect is None or state.aspect == aspect)
        and (status == "all" or state.status == status)
    ]
    return AgreementData.model_validate(
        dict(
            annotators=codes,
            aspects=aspect_agreements(states, codes),
            rows=[_agreement_row(state) for state in rows[offset : offset + limit]],
            total=len(rows),
            offset=offset,
            limit=limit,
        )
    )


@router.get("/label-sets/{label_set_id}/agreement", response_model=ApiResponse[AgreementData])
async def get_agreement(
    label_set_id: str,
    aspect: Annotated[str | None, Query(max_length=80)] = None,
    status: Annotated[AgreementFilter, Query()] = "disagreed",
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=ANNOTATION_ROWS_MAX_LIMIT)] = 25,
) -> ApiResponse[AgreementData]:
    """観点ごとの記入者の間の一致（κ）と、裁定の行（既定は食い違いの行）。"""
    store = get_calibration_store()
    await _label_set(store, label_set_id)
    return ApiResponse(
        data=await _agreement(
            store, label_set_id, aspect=aspect, status=status, offset=offset, limit=limit
        )
    )


@router.put("/label-sets/{label_set_id}/adjudications", response_model=ApiResponse[AgreementData])
async def save_adjudication(label_set_id: str, body: LabelInput) -> ApiResponse[AgreementData]:
    """最終の判定を裁定する（``label`` が null なら裁定を消す）。返すのは観点ごとの一致。"""
    store = get_calibration_store()
    await _label_set(store, label_set_id)
    await _save(store, label_set_id, body, "adjudication")
    return ApiResponse(
        data=await _agreement(
            store, label_set_id, aspect=None, status="disagreed", offset=0, limit=1
        )
    )


def _synthetic_item(item: Any) -> SyntheticItem:
    perturbation = item.payload.get("perturbation") or dict()
    return SyntheticItem(
        item_id=item.item_id,
        base_item_id=str(perturbation.get("base_item_id") or ""),
        kind=str(perturbation.get("kind") or ""),
        description=str(perturbation.get("description") or ""),
        fail_aspects=list(item.payload.get("synthetic_labels") or []),
        answer=str(item.payload.get("answer") or ""),
        excluded=item.excluded,
    )


@router.get("/label-sets/{label_set_id}/synthetic", response_model=ApiResponse[SyntheticItemsData])
async def list_synthetic_items(label_set_id: str) -> ApiResponse[SyntheticItemsData]:
    """合成の対照（故意に壊した回答）。壊しても誤りにならない回答は除外できる。"""
    store = get_calibration_store()
    await _label_set(store, label_set_id)
    items = await store.list_items(label_set_id)
    return ApiResponse(
        data=SyntheticItemsData(
            items=[_synthetic_item(item) for item in items if item.origin == "synthetic"]
        )
    )


@router.put(
    "/label-sets/{label_set_id}/synthetic/{item_id}", response_model=ApiResponse[SyntheticItem]
)
async def set_synthetic_excluded(
    label_set_id: str, item_id: str, body: SyntheticExcludeRequest
) -> ApiResponse[SyntheticItem]:
    store = get_calibration_store()
    await _label_set(store, label_set_id)
    if not await store.set_item_excluded(label_set_id, item_id, body.excluded):
        raise HTTPException(status_code=404, detail="合成の対照が見つかりません。")
    items = await store.list_items(label_set_id)
    return ApiResponse(
        data=_synthetic_item(next(item for item in items if item.item_id == item_id))
    )


@router.get(
    "/label-sets/{label_set_id}/calibrations", response_model=ApiResponse[CalibrationListData]
)
async def list_calibrations(label_set_id: str) -> ApiResponse[CalibrationListData]:
    store = get_calibration_store()
    await _label_set(store, label_set_id)
    identity = current_judge_identity(get_settings())
    records = await store.list_calibrations(label_set_id=label_set_id)
    return ApiResponse(
        data=CalibrationListData(
            items=[
                CalibrationSummary.model_validate(calibration_summary(record, identity))
                for record in records
            ]
        )
    )


@router.post(
    "/label-sets/{label_set_id}/calibrations",
    response_model=ApiResponse[CalibrationStartData],
    status_code=202,
)
async def start_calibration(
    label_set_id: str, body: CalibrationRequest
) -> ApiResponse[CalibrationStartData]:
    """最終の判定で、RAG の回答の評価器を校正する job を投入する（進捗は品質評価の job の API）。"""
    store = get_calibration_store()
    record = await _label_set(store, label_set_id)
    items = await store.list_items(label_set_id)
    labels = await store.list_labels(label_set_id)
    try:
        document = build_label_set_document(record, items, labels)
    except UnresolvedLabelsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    human_labels = HumanLabels.model_validate(document.model_dump(mode="json"))
    settings = get_settings()
    identity = current_judge_identity(settings)
    calibration_id = uuid.uuid4().hex
    total = len(human_labels.items) * body.repeats
    calibration = CalibrationRecord(
        calibration_id=calibration_id,
        label_set_id=label_set_id,
        status="RUNNING",
        repeats=body.repeats,
        user_id_hash=current_audit_request_context().user_id_hash,
        judge_model_id=identity.model_id,
        prompt_sha256=identity.prompt_sha256,
        labels_sha256=items_sha256(items),
    )

    async def finish(status: Literal["FAILED", "CANCELLED"], message: str) -> None:
        with contextlib.suppress(Exception):
            await store.finish_calibration(
                calibration_id, CalibrationFinish(status=status, error_message=message)
            )

    async def execute(report_progress: EvaluationProgressCallback) -> EvaluationJobOutcome:
        try:
            outcome = await run_calibration(
                human_labels, repeats=body.repeats, settings=settings, progress=report_progress
            )
        except asyncio.CancelledError:
            await finish("CANCELLED", CALIBRATION_CANCELLED_MESSAGE)
            raise
        except Exception as exc:
            await finish("FAILED", evaluation_job_failed_message(exc))
            raise
        await store.finish_calibration(
            calibration_id,
            CalibrationFinish(
                status="SUCCEEDED",
                judge_rubric_version=outcome.judge_rubric_version,
                passed=bool(outcome.summary.get("passed")),
                summary=outcome.summary,
                report=outcome.report,
            ),
        )
        return EvaluationJobOutcome(
            result=dict(calibration_id=calibration_id, passed=outcome.summary.get("passed"))
        )

    await store.create_calibration(calibration)
    try:
        job = await get_evaluation_job_service().submit(
            kind="calibration",
            total_cases=total,
            time_limit_seconds=evaluation_job_time_limit_seconds(settings, answer_runs=total),
            executor=execute,
            job_id=calibration_id,
        )
    except EvaluationJobLimitError as exc:
        await finish("FAILED", str(exc))
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    created = await store.get_calibration(calibration_id)
    return ApiResponse(
        data=CalibrationStartData(
            job=job,
            calibration=CalibrationSummary.model_validate(
                calibration_summary(created or calibration, identity)
            ),
        )
    )


@router.get("/label-sets/{label_set_id}/export")
async def export_label_set(label_set_id: str) -> Response:
    """最終の判定を LabelSet の JSON で書き出す（suite の `calibrate` がそのまま読む）。"""
    store = get_calibration_store()
    record = await _label_set(store, label_set_id)
    items = await store.list_items(label_set_id)
    labels = await store.list_labels(label_set_id)
    try:
        document = build_label_set_document(record, items, labels)
    except UnresolvedLabelsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    disposition = 'attachment; filename="labels-' + label_set_id[:8] + '.json"'
    headers = dict([("Content-Disposition", disposition)])
    return Response(
        content=document.model_dump_json(indent=2) + "\n",
        media_type="application/json",
        headers=headers,
    )


@router.delete("/label-sets/{label_set_id}", response_model=ApiResponse[dict[str, bool]])
async def delete_label_set(label_set_id: str) -> ApiResponse[dict[str, bool]]:
    """記入の集合を消す（項目・判定・校正の結果も消える）。"""
    store = get_calibration_store()
    if not await store.delete_label_set(label_set_id):
        raise HTTPException(status_code=404, detail=LABEL_SET_NOT_FOUND)
    return ApiResponse(data=dict(deleted=True))
