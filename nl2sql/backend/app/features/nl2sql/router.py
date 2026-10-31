"""NL2SQL feature router."""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Literal, NamedTuple

import anyio
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from pr_backend_core import ApiResponse
from pr_backend_core.api import (
    DEFAULT_PAGE_LIMIT,
    CursorParams,
    InvalidCursorError,
    OffsetParams,
    cursor_params,
    empty_page,
    offset_params,
)
from pr_backend_core.chat_history_window import chat_history_days
from pr_backend_core.observability.model_usage import run_usage_summary_fields
from pr_system_settings.auth.errors import ROUTE_FORBIDDEN_CODE, SecurityApiError
from pr_system_settings.auth.visibility import (
    DEFAULT_RUN_EXECUTOR_FILTER,
    OTHERS_CONTENT_FORBIDDEN_MESSAGE,
    OTHERS_CONTENT_FORBIDDEN_STATUS,
    OWNER_NOT_FOUND_STATUS,
    RunExecutorFilter,
    can_access_run,
    can_view_all_users_data,
)
from pydantic import ValidationError

from app.api.concurrency import run_sync_io
from app.api.problems import invalid_cursor_exception as _invalid_cursor
from app.security.domain import Principal
from app.security.permissions import (
    FEEDBACK_MANAGE_PERMISSION,
    PROFILE_MANAGE_PERMISSION,
    SQL_EXECUTE_PERMISSION,
)
from app.security.service import get_security_service
from app.settings import get_settings

from .enterprise_ai_client import (
    EnterpriseAiDirectError,
    enterprise_ai_configuration_failure_message,
)
from .incremental_store import (
    HISTORY_SORT_DIRECTIONS,
    HISTORY_SORT_KEYS,
    IncrementalVersionConflict,
)
from .models import (
    HISTORY_RUN_STATUS_CANCELLED,
    AdminFeedbackReviewData,
    AdminFeedbackReviewRequest,
    AgentConversationCreateData,
    AgentConversationCreateRequest,
    AgentConversationsData,
    AgentPrivilegeCheckData,
    AgentTeamRunData,
    AgentTeamRunRequest,
    AgentToolRunRequest,
    AllowedObjects,
    AnalyzeData,
    AnalyzeRequest,
    AnnotationApplyData,
    AnnotationApplyRequest,
    AnnotationSuggestionData,
    AssetCleanupData,
    AssetCleanupRequest,
    AssetRefreshData,
    ClassifierFeedbackImportData,
    ClassifierFeedbackImportRequest,
    ClassifierImportData,
    ClassifierModelImportData,
    ClassifierPredictionData,
    ClassifierPredictRequest,
    ClassifierStatusData,
    ClassifierTrainingCandidatesData,
    ClassifierTrainingDataData,
    ClassifierTrainingExample,
    ClassifierTrainingExampleUpdateRequest,
    ClassifierTrainRequest,
    CommentApplyData,
    CommentApplyRequest,
    CommentSuggestionData,
    CommentSuggestionRequest,
    DbAdminAiAnalysisData,
    DbAdminAiAnalysisRequest,
    DbAdminChatAnalyzeData,
    DbAdminChatAnalyzeRequest,
    DbAdminChatData,
    DbAdminChatExecuteData,
    DbAdminChatGenerateRequest,
    DbAdminChatPage,
    DbAdminChatPlanUpdateRequest,
    DbAdminChatSendData,
    DbAdminChatSummary,
    DbAdminChatTurn,
    DbAdminChatTurnExecuteRequest,
    DbAdminCsvUploadData,
    DbAdminCsvUploadRequest,
    DbAdminDataPreviewData,
    DbAdminDataPreviewRequest,
    DbAdminDropTableRequest,
    DbAdminDropViewRequest,
    DbAdminExecuteData,
    DbAdminExecuteRequest,
    DbAdminImportTabularData,
    DbAdminImportTabularRequest,
    DbAdminJoinWhereData,
    DbAdminJoinWhereRequest,
    DbAdminObjectDetail,
    DbAdminObjectPage,
    DbAdminObjectsData,
    DbAdminStatementsRequest,
    DbAdminTruncateTableRequest,
    DemoLearningData,
    DiagnosticsData,
    DomainInventoryData,
    DomainInventoryRequest,
    ExecuteRequest,
    FeedbackClearData,
    FeedbackData,
    FeedbackEntriesData,
    FeedbackEntriesDeleteRequest,
    FeedbackIndexData,
    FeedbackIndexRequest,
    FeedbackListData,
    FeedbackRequest,
    FeedbackSearchConfigData,
    FeedbackSearchConfigRequest,
    HistoryCancellation,
    HistoryData,
    HistoryItem,
    JobCreateData,
    JobCreateRequest,
    JobData,
    LegacyLearningMaterialData,
    MetadataSqlGenerateData,
    MetadataSqlGenerateRequest,
    MetadataSqlSampleData,
    MetadataSqlSampleRequest,
    Nl2SqlEngine,
    Nl2SqlEnginesData,
    Nl2SqlProfile,
    PersistenceStatusData,
    PreviewData,
    PreviewRequest,
    ProfileDeleteData,
    ProfileLearningMaterialImportData,
    ProfilePatchRequest,
    ProfileRecommendationData,
    ProfileRecommendationRequest,
    ProfileSelectAiConfig,
    ProfileSelectAiProfileRequest,
    ProfileSummaryPage,
    ProfileSyncJobData,
    ProfileSyncJobRequest,
    ProfileUpsertRequest,
    ProfileUsageContext,
    QueryResults,
    QuestionToSqlRequest,
    ReverseSqlData,
    ReverseSqlRequest,
    RewriteData,
    RewriteRequest,
    SampleDataInfo,
    SampleDataMutationData,
    SampleDataMutationRequest,
    SampleDataset,
    SelectAiAgentAssetsData,
    SelectAiCredentialReadinessData,
    SelectAiDbProfileDetailData,
    SelectAiDbProfileDropRequest,
    SelectAiDbProfileMutationData,
    SelectAiDbProfileRefreshJobData,
    SelectAiDbProfilesData,
    SelectAiDbProfileUpsertRequest,
    SelectAiFeedbackAddData,
    SelectAiFeedbackAddRequest,
    SelectAiFeedbackDeleteRequest,
    SelectAiFeedbackEntriesData,
    SelectAiFeedbackMutationData,
    SelectAiFeedbackVectorIndexRequest,
    SelectAiProfilesExportData,
    SelectAiProfilesImportRequest,
    SimilarHistoryData,
    SimilarHistoryRequest,
    SqlChatData,
    SqlChatExecuteData,
    SqlChatPage,
    SqlChatSummary,
    SqlChatUpdateRequest,
    StructureToSqlData,
    StructureToSqlRequest,
    SyntheticDataGenerateRequest,
    SyntheticDataOperationData,
    SyntheticDataResultsData,
    TraceProgressData,
    normalize_profile_identifier,
    validate_profile_identifier,
)
from .oracle_adapter import (
    OracleAdapterError,
    SelectAiCredentialMissingError,
    TabularImportValidationError,
)
from .profile_access import (
    allowed_profile_ids_for_request,
    assert_profile_access,
    profile_access_denied,
)
from .quality_evaluation_models import (
    QualityEvaluationCapabilities,
    QualityEvaluationJobPage,
    QualityEvaluationJobSummary,
    QualityEvaluationResultPage,
)
from .quality_evaluation_service import (
    QualityEvaluationJobNotFoundError,
    QualityEvaluationJobStateError,
    QualityEvaluationValidationError,
    quality_evaluation_service,
)
from .select_ai_readiness import (
    assert_select_ai_credential_ready,
    select_ai_credential_missing_detail,
    select_ai_credential_readiness,
    uses_select_ai_credential,
)
from .service import (
    _SCHEMA_EMPTY_MESSAGE,
    ChatConversationBusyError,
    DbAdminChatConversationFullError,
    DbAdminChatGenerationError,
    HistoryNotRatableError,
    JobIdConflictError,
    JobTraceData,
    OthersContentForbiddenError,
    ProfileNameConflict,
    ProfileOracleCleanupFailed,
    ProfileScopePermissionError,
    SchemaCatalogEmptyError,
    nl2sql_service,
)
from .service import (
    is_select_only as _is_select_only,
)
from .tabular_files import TabularFileReadError
from .trace_spans import TraceSpanReadError

logger = logging.getLogger(__name__)
LEARNING_MATERIAL_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
_LEARNING_MATERIAL_UPLOAD_TOO_LARGE_DETAIL = (
    "学習資材 .xlsx ファイルのサイズが上限 5 MB を超えています。"
)
CLASSIFIER_UPLOAD_MAX_BYTES = 5 * 1024 * 1024
_CLASSIFIER_TRAINING_UPLOAD_TOO_LARGE_DETAIL = (
    "classifier training data .xlsx ファイルのサイズが上限 5 MB を超えています。"
)
_CLASSIFIER_MODEL_UPLOAD_TOO_LARGE_DETAIL = (
    "classifier model artifact .json ファイルのサイズが上限 5 MB を超えています。"
)
_CLASSIFIER_TRAINING_UPLOAD_TYPE_DETAIL = (
    "classifier training data は .xlsx テンプレートを指定してください。"
)
_CLASSIFIER_MODEL_UPLOAD_TYPE_DETAIL = (
    "pickle/joblib artifact は安全上の理由で import できません。"
    "coef / intercept / classes を含む JSON artifact を指定してください。"
)


def _enterprise_ai_sql_generation_exception(exc: EnterpriseAiDirectError) -> HTTPException:
    """Enterprise AI で SQL を作れなかったときの応答。

    設定の誤りは「再試行」と案内しない（#1684）。
    """
    if exc.configuration_error:
        return HTTPException(
            status_code=502,
            detail=enterprise_ai_configuration_failure_message(exc, "SQL の生成で "),
        )
    return HTTPException(
        status_code=502,
        detail="OCI Enterprise AI で SQL を生成できませんでした。再試行してください。",
    )


def _profile_name_conflict_exception(exc: ProfileNameConflict) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={
            "code": exc.code,
            "message_ja": str(exc),
            "field_errors": [
                {
                    "pointer": exc.field_pointer,
                    "code": "profile_name_conflict",
                    "message": str(exc),
                }
            ],
        },
    )


async def _read_bounded_upload(
    file: UploadFile,
    *,
    maximum_bytes: int,
    too_large_detail: str,
) -> bytes:
    file_size = getattr(file, "size", None)
    if isinstance(file_size, int) and file_size > maximum_bytes:
        raise HTTPException(status_code=413, detail=too_large_detail)
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = await file.read(1024 * 1024)
        except TypeError:
            content = await file.read()
            if len(content) > maximum_bytes:
                raise HTTPException(status_code=413, detail=too_large_detail) from None
            return content
        if not chunk:
            break
        total += len(chunk)
        if total > maximum_bytes:
            raise HTTPException(status_code=413, detail=too_large_detail)
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_learning_material_upload(file: UploadFile) -> bytes:
    return await _read_bounded_upload(
        file,
        maximum_bytes=LEARNING_MATERIAL_UPLOAD_MAX_BYTES,
        too_large_detail=_LEARNING_MATERIAL_UPLOAD_TOO_LARGE_DETAIL,
    )


def _require_upload_suffix(
    filename: str,
    allowed_suffixes: set[str],
    detail: str,
    *,
    status_code: int = 400,
) -> None:
    if Path(filename).suffix.lower() not in allowed_suffixes:
        raise HTTPException(status_code=status_code, detail=detail)


async def _read_classifier_training_upload(file: UploadFile) -> bytes:
    _require_upload_suffix(
        file.filename or "",
        {".xlsx"},
        _CLASSIFIER_TRAINING_UPLOAD_TYPE_DETAIL,
    )
    return await _read_bounded_upload(
        file,
        maximum_bytes=CLASSIFIER_UPLOAD_MAX_BYTES,
        too_large_detail=_CLASSIFIER_TRAINING_UPLOAD_TOO_LARGE_DETAIL,
    )


async def _read_classifier_model_upload(file: UploadFile) -> bytes:
    _require_upload_suffix(
        file.filename or "",
        {".json"},
        _CLASSIFIER_MODEL_UPLOAD_TYPE_DETAIL,
        status_code=422,
    )
    return await _read_bounded_upload(
        file,
        maximum_bytes=CLASSIFIER_UPLOAD_MAX_BYTES,
        too_large_detail=_CLASSIFIER_MODEL_UPLOAD_TOO_LARGE_DETAIL,
    )


def _require_persistence() -> None:
    nl2sql_service.ensure_persistence_available()


def _principal_from_request(request: Request) -> Principal | None:
    principal = getattr(request.state, "principal", None)
    return principal if isinstance(principal, Principal) else None


class ActorAccess(NamedTuple):
    """job/feedback の行レベルアクセス判定に使う actor 情報。"""

    actor_user_uuid: str
    actor_can_manage: bool


def _actor_access_args(request: Request, *, manage_permission: str) -> ActorAccess:
    principal = _principal_from_request(request)
    if principal is None:
        # 意図的な fail-open: PLATFORM_AUTH_MODE=local のとき、
        # テナント概念が無いため全 actor 制約を外す(管理者相当)。認証有効時は
        # authorize_api_request が principal を必ず設定するので、この分岐には入らない。
        return ActorAccess(actor_user_uuid="", actor_can_manage=True)
    return ActorAccess(
        actor_user_uuid=principal.user_uuid,
        actor_can_manage=principal.is_system_admin or principal.has_permission(manage_permission),
    )


def _ai_use_access(request: Request) -> ActorAccess:
    """「AI 活用」の画面の job・履歴の判定（#1889）: system_admin は全員、それ以外は本人だけ。

    製品の管理権限（`nl2sql.feedback.manage` など）は全員を見る理由にしない（改善・運用の
    フィードバック管理は `_actor_access_args` のまま）。
    """
    principal = _principal_from_request(request)
    if principal is None:
        # 認証無効（`_actor_access_args` と同じ fail-open）。
        return ActorAccess(actor_user_uuid="", actor_can_manage=True)
    return ActorAccess(
        actor_user_uuid=principal.user_uuid,
        actor_can_manage=can_view_all_users_data(principal),
    )


_CHAT_NOT_FOUND_DETAIL = "会話が見つかりません。"
_CHAT_TURN_NOT_FOUND_DETAIL = "指定された会話のターンが見つかりません。"


def _chat_not_found() -> HTTPException:
    return HTTPException(status_code=OWNER_NOT_FOUND_STATUS, detail=_CHAT_NOT_FOUND_DETAIL)


def _others_content_forbidden() -> HTTPException:
    """他の利用者の会話・実行に新しい実行・内容を足そうとした system_admin への 403（#3244）。"""
    return HTTPException(
        status_code=OTHERS_CONTENT_FORBIDDEN_STATUS, detail=OTHERS_CONTENT_FORBIDDEN_MESSAGE
    )


def _chat_all_users(request: Request) -> bool:
    principal = _principal_from_request(request)
    return principal is not None and can_view_all_users_data(principal)


def _chat_owner_names(owners: list[str]) -> dict[str, str]:
    identities = get_security_service().display_identities(owners)
    return {uuid: identity.display_name for uuid, identity in identities.items()}


def _with_chat_owners[PageT: (SqlChatPage, DbAdminChatPage)](
    page: PageT, *, actor: str, all_users: bool
) -> PageT:
    """会話の一覧に、本人の会話かと、system_admin には利用者の表示名を足す（#1889・#1696）。

    チャットと DB 管理チャット（#2319）の会話の一覧で同じ見え方にする。
    """
    owners = sorted({item.actor_user_uuid for item in page.items if item.actor_user_uuid})
    names = _chat_owner_names(owners) if all_users and owners else {}
    return page.model_copy(
        update={
            "items": [
                _with_chat_owner(item, actor=actor, all_users=all_users, names=names)
                for item in page.items
            ]
        }
    )


def _with_chat_owner[SummaryT: (SqlChatSummary, DbAdminChatSummary)](
    item: SummaryT,
    *,
    actor: str,
    all_users: bool,
    names: dict[str, str] | None = None,
) -> SummaryT:
    owner = item.actor_user_uuid
    if names is None:
        names = _chat_owner_names([owner]) if all_users and owner else {}
    return item.model_copy(
        update={
            "owned": owner == actor,
            "actor_display_name": names.get(owner, "") if all_users else "",
        }
    )


def _with_turn_senders(data: SqlChatData) -> SqlChatData:
    """持ち主以外（system_admin）が送ったターンに、送り手の表示名を足す（#1928・#1696）。

    引けない利用者（削除済みなど）は空にし、画面が「削除済みのユーザー」と出す。
    """
    senders = sorted({turn.sender_user_uuid for turn in data.turns if turn.sender_user_uuid})
    names = _chat_owner_names(senders) if senders else {}
    return data.model_copy(
        update={
            "turns": [
                turn
                if turn.sender_user_uuid is None
                else turn.model_copy(
                    update={"sender_display_name": names.get(turn.sender_user_uuid, "")}
                )
                for turn in data.turns
            ]
        }
    )


def _profile_access_denied() -> HTTPException:
    return profile_access_denied()


def _allowed_profile_ids_for_request(request: Request) -> set[str] | None:
    return allowed_profile_ids_for_request(request, nl2sql_service)


def _profile_access_digest(request: Request) -> str:
    allowed_profile_ids = _allowed_profile_ids_for_request(request)
    if allowed_profile_ids is None:
        return "all"
    payload = "\n".join(sorted(allowed_profile_ids))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _assert_profile_access(
    request: Request,
    profile_id: str | None,
    *,
    default_profile: bool = False,
) -> None:
    assert_profile_access(request, profile_id, default_profile=default_profile)


def _quality_evaluation_job_for_access(
    job_id: str,
    request: Request,
    *,
    wake: bool = False,
    require_actor_owner: bool = False,
) -> QualityEvaluationJobSummary:
    try:
        job_for_access = quality_evaluation_service.peek_job_record(job_id)
    except QualityEvaluationJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    _assert_profile_access(request, job_for_access.profile_id)
    if require_actor_owner:
        access = _actor_access_args(request, manage_permission=PROFILE_MANAGE_PERMISSION)
        # 持ち主の無い job も、管理の権限が無ければ本人の物とみなさない（#1126）。
        if not access.actor_can_manage and job_for_access.actor_user_uuid != access.actor_user_uuid:
            raise HTTPException(
                status_code=403,
                detail="他のユーザーのSQL生成評価 job を操作する権限がありません。",
            )
    if not wake:
        try:
            return quality_evaluation_service.peek_job(job_id)
        except QualityEvaluationJobNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    try:
        return quality_evaluation_service.get_job(job_id)
    except QualityEvaluationJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


persistence_router = APIRouter(prefix="/nl2sql", tags=["nl2sql"])
router = APIRouter(
    prefix="/nl2sql",
    tags=["nl2sql"],
    dependencies=[Depends(_require_persistence)],
)


@persistence_router.get(
    "/persistence",
    response_model=ApiResponse[PersistenceStatusData],
)
def persistence_status() -> ApiResponse[PersistenceStatusData]:
    """NL2SQL incremental store の可用性を返す。"""
    return ApiResponse(data=nl2sql_service.persistence_status())


@persistence_router.post(
    "/persistence/recover",
    response_model=ApiResponse[PersistenceStatusData],
)
def recover_persistence() -> ApiResponse[PersistenceStatusData]:
    """DB 復旧後に接続/migration を再確認する（業務 state は再読込しない）。"""
    return ApiResponse(data=nl2sql_service.recover_persistence())


def is_select_only(sql: str) -> bool:
    """Backward-compatible safety guard export for tests and callers."""
    return _is_select_only(sql)


@router.post("/preview", response_model=ApiResponse[PreviewData])
def preview(req: PreviewRequest, request: Request) -> ApiResponse[PreviewData]:
    """自然言語から SQL を生成して実行せずに safety / engine meta を返す。"""
    _assert_profile_access(request, req.profile_id, default_profile=True)
    try:
        return ApiResponse(data=nl2sql_service.preview(req))
    except SchemaCatalogEmptyError:
        # main.py の専用 handler が error_code=SCHEMA_CATALOG_EMPTY を付与する。
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/execute", response_model=ApiResponse[QueryResults])
def execute(req: ExecuteRequest, request: Request) -> ApiResponse[QueryResults]:
    """SELECT/WITH のみを安全に実行する。

    実行スコープは principal に許可された業務プロファイル群の許可オブジェクトへ強制する。
    profile manager は全有効プロファイルの和集合、system admin / 認証無効時は
    request の allowed_objects のみを使う。
    local skeleton は deterministic mock result を返す。
    実運用では Oracle 実行 adapter へ差し替える。
    """
    try:
        allowed = nl2sql_service.resolve_direct_sql_allowed_objects(
            req.allowed_objects,
            profile_ids=_allowed_profile_ids_for_request(request),
        )
        safety, _executable, results = nl2sql_service.execute_sql(
            sql=req.sql,
            allowed=allowed,
            row_limit=req.row_limit,
        )
    except SchemaCatalogEmptyError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except OracleAdapterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not safety.is_safe:
        if safety.blocked_reason == _SCHEMA_EMPTY_MESSAGE:
            raise SchemaCatalogEmptyError(safety.blocked_reason)
        raise HTTPException(status_code=400, detail=safety.blocked_reason)
    return ApiResponse(data=results)


def _with_requirement(req: JobCreateRequest, principal: object) -> JobCreateRequest:
    """回答前の確認で反映した要件を job に入れる（#1439。画面が送った要件の本文は使わない）。

    session を検査し（本人・同じ業務プロファイル・反映済み・同じ質問の文）、構造化した要件を
    `sql_requirement` に、要件で選んだ表を `allowed_objects` の範囲にする。
    チャットのターン（#1442）は、チャットで確かめた session・同じ会話の続き・利用者が送った
    質問の文と同じものだけを受け付ける。
    """

    req = req.model_copy(update={"sql_requirement": None})
    session_id = (req.requirement_session_id or "").strip()
    if not session_id:
        return req
    from .ontology_router import ontology_runtime
    from .ontology_service import OntologyNotFoundError, OntologyServiceError

    try:
        spec = ontology_runtime.applied_requirement_spec(
            session_id,
            actor_user_uuid=str(getattr(principal, "user_uuid", "")),
            actor_is_system_admin=principal is None
            or bool(getattr(principal, "is_system_admin", False)),
            profile_id=req.profile_id or "",
            question=req.question,
            chat=req.is_chat_turn,
            previous_job_id=req.previous_job_id,
        )
    except OntologyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=exc.message_ja) from exc
    except OntologyServiceError as exc:
        raise HTTPException(status_code=409, detail=exc.message_ja) from exc
    requested = {name for name in req.allowed_objects.table_names if name.strip()}
    tables = [table for table in spec.tables if not requested or table in requested]
    if not tables:
        raise HTTPException(
            status_code=409,
            detail="回答前の確認で選んだ表が、今回の対象の表に含まれていません。",
        )
    allowed = AllowedObjects(
        table_names=tables,
        columns={
            table: columns
            for table, columns in req.allowed_objects.columns.items()
            if table in tables
        },
        enforce_table_scope=True,
    )
    return req.model_copy(update={"sql_requirement": spec, "allowed_objects": allowed})


@router.post("/jobs", response_model=ApiResponse[JobCreateData])
def create_job(req: JobCreateRequest, request: Request) -> ApiResponse[JobCreateData]:
    """NL2SQL の SQL 生成・実行の job を開始する。

    チャットの続き（`previous_job_id`）は会話の持ち主だけ（#3244）。他の利用者の会話は、見えない
    一般の利用者には 404、見える system_admin には 403。ジョブの actor は送った利用者で、権限・
    業務プロファイル・SQL の実行の接続（#904）もその人で決める。
    """
    principal = getattr(request.state, "principal", None)
    # job は生成に加えて SQL を実行する。manifest（生成）に加えて実行の権限も要求する（#242）。
    if (
        not req.generation_only
        and isinstance(principal, Principal)
        and not principal.has_permission(SQL_EXECUTE_PERMISSION)
    ):
        if not req.chat:
            raise SecurityApiError(
                403, "この機能を利用する権限がありません。", code=ROUTE_FORBIDDEN_CODE
            )
        # チャットは実行の権限が無ければ生成だけにする（画面は実行しない理由を出す。#1176）。
        req = req.model_copy(update={"generation_only": True})
    _assert_profile_access(request, req.profile_id, default_profile=True)
    req = _with_requirement(req, principal)
    try:
        actor_user_uuid = str(getattr(principal, "user_uuid", ""))
        return ApiResponse(
            data=nl2sql_service.start_job(
                req,
                actor_user_uuid=actor_user_uuid,
                actor_is_system_admin=bool(getattr(principal, "is_system_admin", False)),
            )
        )
    except OthersContentForbiddenError as exc:
        raise _others_content_forbidden() from exc
    except PermissionError as exc:
        # 見えない他の利用者の会話は存在を漏らさない 404。
        raise _chat_not_found() from exc
    except SelectAiCredentialMissingError as exc:
        # Select AI / Select AI Agent の生成は Credential が前提（#1688）。
        raise _select_ai_credential_missing_exception(exc) from exc
    except JobIdConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValidationError:
        # 保存した値（添付など）を読めないのは入力の誤りではない。pydantic の原文を 400 の本文に
        # 出さず、想定外のエラーとして返す（#2548）。
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/execute", response_model=ApiResponse[SqlChatExecuteData])
def execute_chat_turn(job_id: str, request: Request) -> ApiResponse[SqlChatExecuteData]:
    """チャットのターンで生成した SQL を実行する（#1154）。

    SQL は画面から受け取らず、ターンのジョブに保存した生成 SQL を、SQL 生成のジョブの実行の段階と
    同じ経路（業務プロファイルの範囲・安全検査・接続の分離・実行履歴）で実行する。権限は SQL の実行
    （route の権限）、実行できる会話（会話の持ち主だけ。#3244。見える他の利用者の会話は 403、
    見えない会話は 404）、ターンの業務プロファイルの利用権限を確かめる。
    """
    principal = getattr(request.state, "principal", None)
    actor_user_uuid = str(getattr(principal, "user_uuid", ""))
    all_users = _chat_all_users(request)
    try:
        # 見える会話のターンか（管理の権限は理由にしない）と、業務プロファイルの利用権限を、
        # 実行の前に確かめる。実行できるのは会話の持ち主だけ（#3244。service が確かめる）で、
        # 要求した利用者の権限と接続（#904）で行う。
        job = nl2sql_service.get_job(
            job_id, actor_user_uuid=actor_user_uuid, actor_can_manage=all_users
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=OWNER_NOT_FOUND_STATUS, detail=_CHAT_TURN_NOT_FOUND_DETAIL
        ) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="指定された会話のターンが見つかりません。")
    _assert_profile_access(request, job.profile_id)
    try:
        data = nl2sql_service.execute_chat_turn(
            job_id,
            actor_user_uuid=actor_user_uuid,
            actor_is_system_admin=bool(getattr(principal, "is_system_admin", False)),
        )
    except OthersContentForbiddenError as exc:
        raise _others_content_forbidden() from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=OWNER_NOT_FOUND_STATUS, detail=_CHAT_TURN_NOT_FOUND_DETAIL
        ) from exc
    except LookupError as exc:
        raise HTTPException(
            status_code=404, detail="指定された会話のターンが見つかりません。"
        ) from exc
    except SchemaCatalogEmptyError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse(data=data)


@router.post("/jobs/{job_id}/execution-result", response_model=ApiResponse[SqlChatExecuteData])
def take_chat_execution_result(job_id: str, request: Request) -> ApiResponse[SqlChatExecuteData]:
    """チャットのジョブの中で実行した結果（行を含む）を 1 回だけ受け取る（#1176）。

    行は会話に残さず、受け取ったら消す。期限を過ぎた・受け取り済みの結果は 404（画面は要約と
    「もう一度実行」を出す）。権限は SQL の実行（route の権限）、見える会話（会話の持ち主か
    system_admin）、ターンの業務プロファイルの利用権限（`/execute` と同じ）。行を受け取れる
    のは実行した利用者だけ（その人の権限と接続で取った行。ほかの人は 404 で、「もう一度実行」で
    自分の権限で実行する）。
    """
    principal = getattr(request.state, "principal", None)
    actor_user_uuid = str(getattr(principal, "user_uuid", ""))
    try:
        job = nl2sql_service.get_job(
            job_id, actor_user_uuid=actor_user_uuid, actor_can_manage=_chat_all_users(request)
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=OWNER_NOT_FOUND_STATUS, detail=_CHAT_TURN_NOT_FOUND_DETAIL
        ) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="指定された会話のターンが見つかりません。")
    _assert_profile_access(request, job.profile_id)
    try:
        data = nl2sql_service.take_chat_execution_result(
            job_id,
            actor_user_uuid=actor_user_uuid,
            actor_is_system_admin=_chat_all_users(request),
        )
    except (PermissionError, LookupError) as exc:
        raise HTTPException(
            status_code=404,
            detail="実行の結果はもう受け取れません。もう一度実行してください。",
        ) from exc
    return ApiResponse(data=data)


@router.get("/chats", response_model=ApiResponse[SqlChatPage])
def list_sql_chats(
    request: Request,
    paging: Annotated[
        OffsetParams, Depends(offset_params(default=DEFAULT_PAGE_LIMIT, max_limit=100))
    ],
) -> ApiResponse[SqlChatPage]:
    """会話の一覧を、現在利用できる業務プロファイルの範囲で返す。

    system_admin は全員の会話（利用者の表示名つき）、それ以外は本人の会話だけ（#1889）。
    会話の履歴は 1 ページずつ返す（新しい順。任意のページへ移る offset 型。#1265・#1891）。
    最後の更新が会話の履歴の期間（共通の設定 `PLATFORM_CHAT_HISTORY_DAYS`）の中の会話だけを返す
    （期間の外の会話も `GET /chats/{conversation_id}` では開ける。#2340）。
    """
    principal = getattr(request.state, "principal", None)
    actor = str(getattr(principal, "user_uuid", ""))
    all_users = _chat_all_users(request)
    try:
        page = nl2sql_service.list_sql_chats(
            actor=actor,
            profile_ids=_allowed_profile_ids_for_request(request),
            offset=paging.offset,
            limit=paging.limit,
            all_users=all_users,
            history_days=chat_history_days(get_settings()),
        )
        return ApiResponse(data=_with_chat_owners(page, actor=actor, all_users=all_users))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.get("/chats/{conversation_id}", response_model=ApiResponse[SqlChatData])
def get_sql_chat(conversation_id: str, request: Request) -> ApiResponse[SqlChatData]:
    """会話の内容。system_admin は全員の会話、それ以外は本人の会話だけ（他人は 404。#1889）。"""
    principal = getattr(request.state, "principal", None)
    actor = str(getattr(principal, "user_uuid", ""))
    all_users = _chat_all_users(request)
    try:
        data = nl2sql_service.get_sql_chat(conversation_id, actor=actor, all_users=all_users)
    except PermissionError as exc:
        raise _chat_not_found() from exc
    if data is None:
        raise _chat_not_found()
    _assert_profile_access(request, data.conversation.profile_id)
    return ApiResponse(
        data=_with_turn_senders(data).model_copy(
            update={
                "conversation": _with_chat_owner(
                    data.conversation, actor=actor, all_users=all_users
                )
            }
        )
    )


@router.patch("/chats/{conversation_id}", response_model=ApiResponse[SqlChatSummary])
def rename_sql_chat(
    conversation_id: str, update: SqlChatUpdateRequest, request: Request
) -> ApiResponse[SqlChatSummary]:
    """会話の名前を変える（#1817）。持ち主だけ（#3244。system_admin が他の利用者の会話を変えるのは
    403、見えない会話は 404）。"""
    principal = getattr(request.state, "principal", None)
    actor = str(getattr(principal, "user_uuid", ""))
    all_users = _chat_all_users(request)
    try:
        current = nl2sql_service.get_sql_chat(conversation_id, actor=actor, all_users=all_users)
        if current is None:
            raise LookupError(conversation_id)
        _assert_profile_access(request, current.conversation.profile_id)
        data = nl2sql_service.rename_sql_chat(
            conversation_id, actor=actor, title=update.title, all_users=all_users
        )
    except OthersContentForbiddenError as exc:
        raise _others_content_forbidden() from exc
    except PermissionError as exc:
        raise _chat_not_found() from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=_CHAT_NOT_FOUND_DETAIL) from exc
    except ChatConversationBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail="SQL の生成中は会話名を変更できません。生成が終わってから変更してください。",
        ) from exc
    return ApiResponse(data=_with_chat_owner(data, actor=actor, all_users=all_users))


def _delete_chat_attachments(conversation_id: str) -> None:
    from app.features.nl2sql.chat_attachments import chat_attachment_service

    try:
        chat_attachment_service().delete_conversation(conversation_id)
    except Exception as exc:  # noqa: BLE001 - 会話の削除は止めない
        logger.warning(
            "nl2sql_chat_attachments_delete_failed",
            extra={"conversation_id": conversation_id, "error_type": type(exc).__name__},
        )


@router.delete("/chats/{conversation_id}", response_model=ApiResponse[None])
def delete_sql_chat(conversation_id: str, request: Request) -> ApiResponse[None]:
    """会話を消す（#1817）。system_admin は全員の会話、それ以外は本人の会話だけ（#1889）。

    ジョブは監査・フィードバック・実行履歴のために残し、会話の一覧・詳細・続きから外す。
    SQL の生成中のターンがある会話は 409（停止してから消す）。
    """
    principal = getattr(request.state, "principal", None)
    actor = str(getattr(principal, "user_uuid", ""))
    all_users = _chat_all_users(request)
    try:
        current = nl2sql_service.get_sql_chat(conversation_id, actor=actor, all_users=all_users)
        if current is None:
            raise LookupError(conversation_id)
        _assert_profile_access(request, current.conversation.profile_id)
        nl2sql_service.delete_sql_chat(conversation_id, actor=actor, all_users=all_users)
        # 会話の添付も消す（#2517。消せなくても期限で消える）。
        _delete_chat_attachments(conversation_id)
    except PermissionError as exc:
        raise _chat_not_found() from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=_CHAT_NOT_FOUND_DETAIL) from exc
    except ChatConversationBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                "SQL の生成中の会話は削除できません。"
                "停止するか、生成が終わってから削除してください。"
            ),
        ) from exc
    return ApiResponse(data=None)


@router.get("/jobs/{job_id}", response_model=ApiResponse[JobData])
def get_job(job_id: str, request: Request) -> ApiResponse[JobData]:
    """NL2SQL の SQL 生成・実行の job の状態・結果を返す。

    system_admin は全員の job、それ以外は本人の job だけ（他人の job は 404。#1889）。
    """
    try:
        access = _ai_use_access(request)
        job = nl2sql_service.get_job(
            job_id,
            actor_user_uuid=access.actor_user_uuid,
            actor_can_manage=access.actor_can_manage,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=OWNER_NOT_FOUND_STATUS, detail=_JOB_PROGRESS_NOT_FOUND_DETAIL
        ) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="指定されたジョブが見つかりません。")
    if job.chat:
        _assert_profile_access(request, job.profile_id)
    return ApiResponse(data=job)


_JOB_PROGRESS_NOT_FOUND_DETAIL = "指定されたジョブが見つかりません。"


_TRACE_READ_FAILED_DETAIL = "処理の経過を読み込めませんでした。時間をおいて再度お試しください。"


def _trace_read_unavailable(exc: TraceSpanReadError) -> HTTPException:
    """span の表を読めなかった（画面は前の内容を残して読み直す）。"""

    return HTTPException(status_code=503, detail=_TRACE_READ_FAILED_DETAIL)


def _job_trace_for_request(job_id: str, request: Request) -> JobTraceData:
    """ジョブの処理の経過（span の表の snapshot）。

    アクセスの判定は `GET /jobs/{job_id}` と同じ（本人のジョブか system_admin・業務プロファイル）。
    """
    try:
        access = _ai_use_access(request)
        progress = nl2sql_service.get_job_trace(
            job_id,
            actor_user_uuid=access.actor_user_uuid,
            actor_can_manage=access.actor_can_manage,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=OWNER_NOT_FOUND_STATUS, detail=_JOB_PROGRESS_NOT_FOUND_DETAIL
        ) from exc
    except TraceSpanReadError as exc:
        raise _trace_read_unavailable(exc) from exc
    if progress is None:
        raise HTTPException(status_code=404, detail=_JOB_PROGRESS_NOT_FOUND_DETAIL)
    if progress.chat:
        _assert_profile_access(request, progress.profile_id)
    return progress


@router.get("/jobs/{job_id}/trace", response_model=ApiResponse[TraceProgressData])
def get_job_trace(job_id: str, request: Request) -> ApiResponse[TraceProgressData]:
    """ジョブの処理の経過（共通の span の表の snapshot。#3226 の案 A）。

    span は worker（別の process）が書くので、実行中も段の中の span（モデルの呼び出し・Select AI）
    まで読める。画面は実行中に 1 秒ごとに読み直す。最終の状態はジョブの状態（正本）で決める。
    """
    return ApiResponse(data=_job_trace_for_request(job_id, request).trace)


@router.post("/jobs/{job_id}/cancel", response_model=ApiResponse[JobData])
def cancel_job(job_id: str, request: Request) -> ApiResponse[JobData]:
    """実行中の NL2SQL の SQL 生成・実行の job の協調キャンセルを要求する。

    worker は stage 境界で検出して停止する(実行中 stage の途中では止まらない)。
    terminal な job には no-op で現在の状態を返す。system_admin は全員の job、それ以外は本人の
    job だけ（他人の job は 404。#1889）。
    """
    try:
        access = _ai_use_access(request)
        job = nl2sql_service.request_job_cancel(
            job_id,
            actor_user_uuid=access.actor_user_uuid,
            actor_can_manage=access.actor_can_manage,
        )
    except PermissionError as exc:
        raise HTTPException(
            status_code=OWNER_NOT_FOUND_STATUS, detail=_JOB_PROGRESS_NOT_FOUND_DETAIL
        ) from exc
    if job is None:
        raise HTTPException(status_code=404, detail="指定されたジョブが見つかりません。")
    return ApiResponse(data=job)


@router.get("/profiles", response_model=ApiResponse[list[Nl2SqlProfile]])
def list_profiles(
    request: Request, response: Response, include_archived: bool = False
) -> ApiResponse[list[Nl2SqlProfile]]:
    """NL2SQL profile 一覧。"""
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "Wed, 30 Sep 2026 00:00:00 GMT"
    response.headers["Link"] = '</api/nl2sql/profiles/search>; rel="successor-version"'
    allowed_profile_ids = _allowed_profile_ids_for_request(request)
    profiles = nl2sql_service.list_profiles(include_archived=include_archived)
    if allowed_profile_ids is not None:
        profiles = [profile for profile in profiles if profile.id in allowed_profile_ids]
    return ApiResponse(data=profiles)


@router.get("/profiles/search", response_model=ApiResponse[ProfileSummaryPage])
def search_profiles(
    request: Request,
    response: Response,
    paging: Annotated[CursorParams, Depends(cursor_params(default=50, max_limit=100))],
    q: str = "",
    include_archived: bool = False,
    sort: str = "name",
    direction: str = "asc",
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> ApiResponse[ProfileSummaryPage] | Response:
    """Full payload を返さない業務 profile keyset page。"""
    try:
        page = nl2sql_service.search_profiles(
            cursor=paging.cursor,
            limit=paging.limit,
            query=q,
            include_archived=include_archived,
            allowed_profile_ids=_allowed_profile_ids_for_request(request),
            sort=sort,
            direction=direction,
        )
    except InvalidCursorError as exc:
        raise _invalid_cursor(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # sort 指定ごとに並びが変わるため ETag にも含める。
    access_digest = _profile_access_digest(request)
    quoted_etag = f'"profiles-{page.change_token}-{sort}-{direction}-{access_digest}"'
    if if_none_match == quoted_etag:
        return Response(status_code=304, headers={"ETag": quoted_etag})
    response.headers["ETag"] = quoted_etag
    return ApiResponse(data=page)


@router.get(
    "/profiles/{profile_id}/usage-context",
    response_model=ApiResponse[ProfileUsageContext],
)
def get_profile_usage_context(
    profile_id: str,
    request: Request,
    response: Response,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> ApiResponse[ProfileUsageContext] | Response:
    """AI 活用画面向けに profile の最小利用コンテキストを返す。"""
    _assert_profile_access(request, profile_id)
    try:
        profile = nl2sql_service.get_profile(profile_id, include_archived=False)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    quoted_etag = f'"{profile.etag}"'
    if profile.etag and if_none_match == quoted_etag:
        return Response(status_code=304, headers={"ETag": quoted_etag})
    if profile.etag:
        response.headers["ETag"] = quoted_etag
    return ApiResponse(
        data=ProfileUsageContext.model_validate(
            profile.model_dump(
                include={
                    "id",
                    "name",
                    "category",
                    "description",
                    "allowed_tables",
                    "allowed_views",
                    "default_engine",
                    "clarification_policy",
                    "archived",
                    "object_scope_version",
                    "version",
                    "etag",
                    "updated_at",
                }
            )
        )
    )


@router.get("/profiles/{profile_id}", response_model=ApiResponse[Nl2SqlProfile])
def get_profile_detail(
    profile_id: str,
    request: Request,
    response: Response,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> ApiResponse[Nl2SqlProfile] | Response:
    """選択された profile だけを遅延取得する。"""
    _assert_profile_access(request, profile_id)
    try:
        profile = nl2sql_service.get_profile(profile_id, include_archived=True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    quoted_etag = f'"{profile.etag}"'
    if profile.etag and if_none_match == quoted_etag:
        return Response(status_code=304, headers={"ETag": quoted_etag})
    if profile.etag:
        response.headers["ETag"] = quoted_etag
    return ApiResponse(data=profile)


@router.get("/engines", response_model=ApiResponse[Nl2SqlEnginesData])
def list_engines() -> ApiResponse[Nl2SqlEnginesData]:
    """この環境で使える生成の方式（業務プロファイルの既定の生成方式の選択肢。#1524）。"""
    return ApiResponse(data=Nl2SqlEnginesData(engines=nl2sql_service.engine_availability()))


@router.post("/profiles", response_model=ApiResponse[Nl2SqlProfile])
def create_profile(req: ProfileUpsertRequest, response: Response) -> ApiResponse[Nl2SqlProfile]:
    """NL2SQL profile を作成する。"""
    profile = Nl2SqlProfile(id=str(uuid.uuid4()), **req.model_dump())
    try:
        nl2sql_service.assert_engine_available(profile.default_engine)
        stored = nl2sql_service.create_profile(profile)
    except ProfileNameConflict as exc:
        raise _profile_name_conflict_exception(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if stored.etag:
        response.headers["ETag"] = f'"{stored.etag}"'
    return ApiResponse(data=stored)


@router.patch("/profiles/{profile_id}", response_model=ApiResponse[Nl2SqlProfile])
def update_profile(
    profile_id: str,
    req: ProfilePatchRequest,
    request: Request,
    response: Response,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ApiResponse[Nl2SqlProfile]:
    """NL2SQL profile を更新する。"""
    _assert_profile_access(request, profile_id)
    try:
        if nl2sql_service.uses_incremental_store and not if_match:
            raise HTTPException(status_code=428, detail="If-Match header が必要です。")
        patch_data = req.model_dump(exclude_unset=True, exclude_none=True)

        def apply_profile_patch(current: Nl2SqlProfile) -> Nl2SqlProfile:
            update = dict(patch_data)
            # 既定の生成方式を変えるときだけ、この環境で使えるかを確かめる（#1524）。
            # 設定を外した後も、ほかの項目の保存は止めない。
            requested_engine = update.get("default_engine")
            if requested_engine is not None and requested_engine != current.default_engine:
                nl2sql_service.assert_engine_available(Nl2SqlEngine(requested_engine))
            profile_name = normalize_profile_identifier(update.get("name", current.name))
            validate_profile_identifier(profile_name)
            update["name"] = profile_name
            select_ai_patch = update.pop("select_ai_config", None)
            if select_ai_patch is not None:
                select_ai_config_data = {
                    **current.select_ai_config.model_dump(),
                    **select_ai_patch,
                    "profile_name": profile_name,
                }
                update["select_ai_config"] = ProfileSelectAiConfig.model_validate(
                    select_ai_config_data
                )
            else:
                update["select_ai_config"] = current.select_ai_config.model_copy(
                    update={"profile_name": profile_name}
                )
            return current.model_copy(update=update)

        updated = nl2sql_service.update_profile(
            profile_id,
            apply_profile_patch,
            expected_etag=if_match.strip('"') if if_match else None,
        )
    except IncrementalVersionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="業務プロファイルが更新されています。再読込してください。",
            headers={"ETag": f'"{exc.current_etag}"'},
        ) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="指定された業務プロファイルが見つかりません。"
        ) from exc
    except ProfileNameConflict as exc:
        raise _profile_name_conflict_exception(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if updated.etag:
        response.headers["ETag"] = f'"{updated.etag}"'
    return ApiResponse(data=updated)


@router.delete("/profiles/{profile_id}", response_model=ApiResponse[ProfileDeleteData])
def delete_profile(
    profile_id: str,
    request: Request,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> ApiResponse[ProfileDeleteData]:
    """NL2SQL profile を物理削除する。"""
    _assert_profile_access(request, profile_id)
    try:
        if nl2sql_service.uses_incremental_store and not if_match:
            raise HTTPException(status_code=428, detail="If-Match header が必要です。")
        deleted = nl2sql_service.delete_profile_with_oracle_cleanup(
            profile_id,
            expected_etag=if_match.strip('"') if if_match else None,
        )
        # Profile/view の DB transaction が確定してから job と runtime cache を片付ける。
        # build worker は proposal 書込直前にも Profile の存在を確認する。
        from .ontology_router import ontology_build_service, ontology_runtime
        from .profile_sync import profile_sync_service

        for cleanup in (
            ontology_build_service.cancel_profile_jobs,
            ontology_build_service.purge_profile_source_documents,
            profile_sync_service.cancel_for_profile,
            ontology_runtime.delete_profile_state,
        ):
            try:
                cleanup(profile_id)
            except Exception:
                # Profile/view transaction は既に commit 済み。worker の存在再確認と
                # FK cascade が再生成を防ぐため、cleanup 障害で成功済み削除を 500 にしない。
                logger.warning(
                    "profile_delete_post_commit_cleanup_failed",
                    exc_info=True,
                    extra={"profile_id": profile_id, "cleanup": cleanup.__name__},
                )
        return ApiResponse(data=deleted)
    except ProfileOracleCleanupFailed as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except IncrementalVersionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="業務プロファイルが更新されています。再読込してください。",
            headers={"ETag": f'"{exc.current_etag}"'},
        ) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="指定された業務プロファイルが見つかりません。"
        ) from exc
    except ProfileNameConflict as exc:
        raise _profile_name_conflict_exception(exc) from exc


@router.post(
    "/profiles/{profile_id}/learning-material/import",
    response_model=ApiResponse[ProfileLearningMaterialImportData],
)
async def import_profile_learning_material(
    profile_id: str,
    request: Request,
    file: Annotated[UploadFile, File()],
    mode: Annotated[str, Form()] = "merge",
) -> ApiResponse[ProfileLearningMaterialImportData]:
    """旧版 terms/rules/few-shot .xlsx テンプレートを取り込む。rules は追加指示へ吸収する。"""
    _assert_profile_access(request, profile_id)
    content = await _read_learning_material_upload(file)
    try:
        return ApiResponse(
            data=await run_sync_io(
                nl2sql_service.import_profile_learning_material,
                profile_id=profile_id,
                filename=file.filename or "learning_material.xlsx",
                content=content,
                mode=mode,
            )
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="指定された業務プロファイルが見つかりません。"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/profiles/{profile_id}/learning-material/export.xlsx")
def export_profile_learning_material(profile_id: str, request: Request) -> Response:
    """Profile learning material を Excel workbook として出力する。"""
    _assert_profile_access(request, profile_id)
    try:
        filename, content = nl2sql_service.export_profile_learning_material_xlsx(profile_id)
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="指定された業務プロファイルが見つかりません。"
        ) from exc
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/legacy-learning-material",
    response_model=ApiResponse[LegacyLearningMaterialData],
)
def get_legacy_learning_material() -> ApiResponse[LegacyLearningMaterialData]:
    """旧版 terms.xlsx / rules.xlsx 互換データ（グローバル用語集・グローバルルール）を返す。"""
    return ApiResponse(data=nl2sql_service.get_legacy_learning_material())


@router.post(
    "/legacy-learning-material/terms/import",
    response_model=ApiResponse[LegacyLearningMaterialData],
)
async def import_legacy_terms(
    file: Annotated[UploadFile, File()],
) -> ApiResponse[LegacyLearningMaterialData]:
    """旧版 terms.xlsx 互換の用語集を取り込む。"""
    content = await _read_learning_material_upload(file)
    try:
        return ApiResponse(
            data=await run_sync_io(
                nl2sql_service.import_legacy_terms,
                filename=file.filename or "terms.xlsx",
                content=content,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/legacy-learning-material/rules/import",
    response_model=ApiResponse[LegacyLearningMaterialData],
)
async def import_legacy_rules(
    file: Annotated[UploadFile, File()],
) -> ApiResponse[LegacyLearningMaterialData]:
    """旧版 rules.xlsx 互換のグローバルルールを取り込む。"""
    content = await _read_learning_material_upload(file)
    try:
        return ApiResponse(
            data=await run_sync_io(
                nl2sql_service.import_legacy_rules,
                filename=file.filename or "rules.xlsx",
                content=content,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/legacy-learning-material/terms/export.xlsx")
def export_legacy_terms() -> Response:
    """旧版 terms.xlsx 互換の用語集を Excel workbook として出力する。"""
    filename, content = nl2sql_service.export_legacy_terms_xlsx()
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/legacy-learning-material/rules/export.xlsx")
def export_legacy_rules() -> Response:
    """旧版 rules.xlsx 互換のグローバルルールを Excel workbook として出力する。"""
    filename, content = nl2sql_service.export_legacy_rules_xlsx()
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/profiles/{profile_id}/archive", response_model=ApiResponse[Nl2SqlProfile])
def archive_profile(profile_id: str, request: Request) -> ApiResponse[Nl2SqlProfile]:
    """NL2SQL profile を archive する。"""
    _assert_profile_access(request, profile_id)
    try:
        return ApiResponse(data=nl2sql_service.archive_profile(profile_id))
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="指定された業務プロファイルが見つかりません。"
        ) from exc
    except ProfileNameConflict as exc:
        raise _profile_name_conflict_exception(exc) from exc


@router.post("/profiles/{profile_id}/restore", response_model=ApiResponse[Nl2SqlProfile])
def restore_profile(profile_id: str, request: Request) -> ApiResponse[Nl2SqlProfile]:
    """archive 済みの NL2SQL profile を復元する。"""
    _assert_profile_access(request, profile_id)
    try:
        return ApiResponse(data=nl2sql_service.restore_profile(profile_id))
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="指定された業務プロファイルが見つかりません。"
        ) from exc
    except ProfileNameConflict as exc:
        raise _profile_name_conflict_exception(exc) from exc


@router.post(
    "/profiles/{profile_id}/select-ai-profile",
    response_model=ApiResponse[SelectAiDbProfileMutationData],
)
def upsert_profile_select_ai_profile(
    profile_id: str,
    req: ProfileSelectAiProfileRequest,
    request: Request,
) -> ApiResponse[SelectAiDbProfileMutationData]:
    """業務 profile から Oracle DBMS_CLOUD_AI profile を作成する。"""
    _assert_profile_access(request, profile_id)
    try:
        return ApiResponse(data=nl2sql_service.upsert_profile_select_ai_profile(profile_id, req))
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="指定された業務プロファイルが見つかりません。"
        ) from exc


def _select_ai_credential_missing_exception(exc: SelectAiCredentialMissingError) -> HTTPException:
    """Select AI の Credential が足りないときの 409（#1683 / #1688）。"""
    return HTTPException(status_code=409, detail=select_ai_credential_missing_detail(exc))


def _assert_select_ai_credential_ready() -> None:
    """Oracle に触る前に Select AI の Credential を確かめ、無ければ 409 で止める（#1688）。"""
    try:
        assert_select_ai_credential_ready(nl2sql_service._oracle_adapter)  # noqa: SLF001
    except SelectAiCredentialMissingError as exc:
        raise _select_ai_credential_missing_exception(exc) from exc


@router.get(
    "/select-ai/credential-readiness",
    response_model=ApiResponse[SelectAiCredentialReadinessData],
)
def get_select_ai_credential_readiness() -> ApiResponse[SelectAiCredentialReadinessData]:
    """Select AI の Credential が作成済みかを返す（#1683 / #1688）。

    業務プロファイルの保存（Oracle 反映）・SQL 生成とチャットの Select AI / Select AI Agent・
    合成データ・SQL 生成評価の画面が、操作の前に事前条件を示すのに使う。運用設定の
    Select AI Credential 管理の権限が無い利用者でも読める。秘密は返さない。
    """
    return ApiResponse(
        data=select_ai_credential_readiness(nl2sql_service._oracle_adapter)  # noqa: SLF001
    )


@router.post(
    "/profiles/{profile_id}/oracle-sync-jobs",
    response_model=ApiResponse[ProfileSyncJobData],
    status_code=202,
)
def create_profile_oracle_sync_job(
    profile_id: str,
    req: ProfileSyncJobRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    request: Request,
) -> ApiResponse[ProfileSyncJobData]:
    """保存済み業務 Profile の Oracle 反映を永続 queue へ投入する。"""
    _assert_profile_access(request, profile_id)

    from .profile_sync import profile_sync_service

    try:
        return ApiResponse(
            data=profile_sync_service.start(
                profile_id,
                req,
                idempotency_key=idempotency_key,
            )
        )
    except SelectAiCredentialMissingError as exc:
        raise _select_ai_credential_missing_exception(exc) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="指定された業務プロファイルが見つかりません。"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/oracle-sync-jobs/{job_id}",
    response_model=ApiResponse[ProfileSyncJobData],
)
def get_profile_oracle_sync_job(
    job_id: str,
    request: Request,
) -> ApiResponse[ProfileSyncJobData]:
    """Oracle Profile 同期 job の進捗を返す。"""

    from .profile_sync import profile_sync_service

    job = profile_sync_service.peek(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="指定された同期 job が見つかりません。")
    _assert_profile_access(request, job.profile_id)
    return ApiResponse(data=profile_sync_service.get(job_id) or job)


@router.post(
    "/oracle-sync-jobs/{job_id}/retry",
    response_model=ApiResponse[ProfileSyncJobData],
    status_code=202,
)
def retry_profile_oracle_sync_job(
    job_id: str,
    request: Request,
) -> ApiResponse[ProfileSyncJobData]:
    """失敗した Oracle Profile 同期 job を最新版 Profile で再試行する。"""

    from .profile_sync import profile_sync_service

    try:
        job = profile_sync_service.peek(job_id)
        if job is None:
            raise KeyError(job_id)
        _assert_profile_access(request, job.profile_id)
        return ApiResponse(data=profile_sync_service.retry(job_id))
    except SelectAiCredentialMissingError as exc:
        raise _select_ai_credential_missing_exception(exc) from exc
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail="指定された同期 job が見つかりません。",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/select-ai/profiles/refresh", response_model=ApiResponse[AssetRefreshData])
def refresh_select_ai_profile(
    request: Request,
    profile_id: str | None = None,
) -> ApiResponse[AssetRefreshData]:
    """Oracle Select AI profile を作成/更新する adapter boundary。"""
    _assert_profile_access(request, profile_id, default_profile=True)
    _assert_select_ai_credential_ready()
    try:
        return ApiResponse(data=nl2sql_service.refresh_select_ai_profile(profile_id))
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="指定された業務プロファイルが見つかりません。"
        ) from exc


@router.post("/select-ai-agent/assets/refresh", response_model=ApiResponse[AssetRefreshData])
def refresh_select_ai_agent_assets(
    request: Request,
    profile_id: str | None = None,
) -> ApiResponse[AssetRefreshData]:
    """Oracle Select AI Agent assets を作成/更新する adapter boundary。"""
    _assert_profile_access(request, profile_id, default_profile=True)
    _assert_select_ai_credential_ready()
    try:
        return ApiResponse(data=nl2sql_service.refresh_select_ai_agent_assets(profile_id))
    except KeyError as exc:
        raise HTTPException(
            status_code=404, detail="指定された業務プロファイルが見つかりません。"
        ) from exc


@router.post("/select-ai/assets/cleanup", response_model=ApiResponse[list[AssetCleanupData]])
def cleanup_select_ai_assets(
    req: AssetCleanupRequest,
    request: Request,
) -> ApiResponse[list[AssetCleanupData]]:
    """Oracle Select AI / Agent assets の cleanup を実行する。"""
    _assert_profile_access(request, req.profile_id, default_profile=True)
    return ApiResponse(
        data=nl2sql_service.cleanup_select_ai_assets(
            profile_id=req.profile_id,
            engines=req.engines,
            confirmation=req.confirmation,
            reason=req.reason,
        )
    )


@router.get("/select-ai/db-profiles", response_model=ApiResponse[SelectAiDbProfilesData])
def select_ai_db_profiles(
    include_detail: bool = False,
    business_profiles_only: bool = False,
    include_archived_business_profiles: bool = True,
) -> ApiResponse[SelectAiDbProfilesData]:
    """Oracle DBMS_CLOUD_AI profile 一覧を返す。"""
    return ApiResponse(
        data=nl2sql_service.list_select_ai_db_profiles(
            include_detail=include_detail,
            business_profiles_only=business_profiles_only,
            include_archived_business_profiles=include_archived_business_profiles,
        )
    )


@router.post(
    "/select-ai/db-profiles/refresh-jobs",
    response_model=ApiResponse[SelectAiDbProfileRefreshJobData],
    status_code=202,
)
def create_select_ai_db_profile_refresh_job() -> ApiResponse[SelectAiDbProfileRefreshJobData]:
    """Oracle DBMS_CLOUD_AI profile 一覧を手動で全量再取得する。"""
    return ApiResponse(data=nl2sql_service.start_select_ai_db_profile_refresh_job())


@router.get(
    "/select-ai/db-profile-refresh-jobs/{job_id}",
    response_model=ApiResponse[SelectAiDbProfileRefreshJobData],
)
def get_select_ai_db_profile_refresh_job(
    job_id: str,
) -> ApiResponse[SelectAiDbProfileRefreshJobData]:
    """Oracle DBMS_CLOUD_AI profile 一覧 refresh job の進捗を返す。"""
    job = nl2sql_service.get_select_ai_db_profile_refresh_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail="指定された DB Profile 一覧 refresh job が見つかりません。",
        )
    return ApiResponse(data=job)


@router.get(
    "/select-ai/db-profiles/{profile_name}",
    response_model=ApiResponse[SelectAiDbProfileDetailData],
)
def select_ai_db_profile_detail(
    profile_name: str,
) -> ApiResponse[SelectAiDbProfileDetailData]:
    """Oracle DBMS_CLOUD_AI profile 詳細を返す。"""
    return ApiResponse(data=nl2sql_service.get_select_ai_db_profile(profile_name))


@router.get("/select-ai/feedback", response_model=ApiResponse[SelectAiFeedbackEntriesData])
def select_ai_feedback(
    profile_name: str,
    paging: Annotated[
        OffsetParams, Depends(offset_params(default=DEFAULT_PAGE_LIMIT, max_limit=100))
    ],
) -> ApiResponse[SelectAiFeedbackEntriesData]:
    """Oracle DBMS_CLOUD_AI profile feedback vector entries の 1 ページ（offset 型。#1891）。"""
    return ApiResponse(
        data=nl2sql_service.list_select_ai_feedback_entries(
            profile_name, limit=paging.limit, offset=paging.offset
        )
    )


@router.post(
    "/select-ai/feedback/add",
    response_model=ApiResponse[SelectAiFeedbackAddData],
)
def add_select_ai_feedback(
    req: SelectAiFeedbackAddRequest,
) -> ApiResponse[SelectAiFeedbackAddData]:
    """Oracle DBMS_CLOUD_AI profile feedback entry を追加する。"""
    return ApiResponse(data=nl2sql_service.add_select_ai_feedback(req))


@router.post(
    "/select-ai/feedback/delete",
    response_model=ApiResponse[SelectAiFeedbackMutationData],
)
def delete_select_ai_feedback(
    req: SelectAiFeedbackDeleteRequest,
) -> ApiResponse[SelectAiFeedbackMutationData]:
    """Oracle DBMS_CLOUD_AI profile feedback entry を削除する。"""
    return ApiResponse(data=nl2sql_service.delete_select_ai_feedback(req))


@router.post(
    "/select-ai/feedback/vector-index",
    response_model=ApiResponse[SelectAiFeedbackMutationData],
)
def update_select_ai_feedback_vector_index(
    req: SelectAiFeedbackVectorIndexRequest,
) -> ApiResponse[SelectAiFeedbackMutationData]:
    """Oracle DBMS_CLOUD_AI feedback vector index attributes を更新する。"""
    return ApiResponse(data=nl2sql_service.update_select_ai_feedback_vector_index(req))


@router.post(
    "/select-ai/db-profiles",
    response_model=ApiResponse[SelectAiDbProfileMutationData],
)
def upsert_select_ai_db_profile(
    req: SelectAiDbProfileUpsertRequest,
) -> ApiResponse[SelectAiDbProfileMutationData]:
    """Oracle DBMS_CLOUD_AI profile を low-level JSON から作成/更新する。"""
    return ApiResponse(data=nl2sql_service.upsert_select_ai_db_profile(req))


@router.patch(
    "/select-ai/db-profiles/{profile_name}",
    response_model=ApiResponse[SelectAiDbProfileMutationData],
)
def patch_select_ai_db_profile(
    profile_name: str,
    req: SelectAiDbProfileUpsertRequest,
) -> ApiResponse[SelectAiDbProfileMutationData]:
    """Oracle DBMS_CLOUD_AI profile を名前指定で更新する。"""
    return ApiResponse(
        data=nl2sql_service.upsert_select_ai_db_profile(
            req.model_copy(
                update={
                    "profile_name": req.profile_name or profile_name,
                    "original_name": req.original_name or profile_name,
                }
            )
        )
    )


@router.get(
    "/select-ai/profiles/export.json",
    response_model=ApiResponse[SelectAiProfilesExportData],
)
def export_select_ai_profiles_json(
    business_profiles_only: bool = False,
    include_archived_business_profiles: bool = True,
) -> ApiResponse[SelectAiProfilesExportData]:
    """Oracle DBMS_CLOUD_AI profile definitions を JSON として返す。"""
    return ApiResponse(
        data=nl2sql_service.export_select_ai_profiles_json(
            business_profiles_only=business_profiles_only,
            include_archived_business_profiles=include_archived_business_profiles,
        )
    )


@router.post(
    "/select-ai/profiles/import-json",
    response_model=ApiResponse[list[SelectAiDbProfileMutationData]],
)
def import_select_ai_profiles_json(
    req: SelectAiProfilesImportRequest,
) -> ApiResponse[list[SelectAiDbProfileMutationData]]:
    """Oracle DBMS_CLOUD_AI profile definitions JSON を import する。"""
    return ApiResponse(data=nl2sql_service.import_select_ai_profiles_json(req))


@router.post(
    "/select-ai/db-profiles/{profile_name}/drop",
    response_model=ApiResponse[AssetCleanupData],
)
def drop_select_ai_db_profile(
    profile_name: str,
    req: SelectAiDbProfileDropRequest,
) -> ApiResponse[AssetCleanupData]:
    """Oracle DBMS_CLOUD_AI profile を名前指定で drop する。"""
    return ApiResponse(
        data=nl2sql_service.drop_select_ai_db_profile(
            profile_name,
            confirmation=req.confirmation,
            reason=req.reason,
        )
    )


@router.get("/select-ai-agent/assets", response_model=ApiResponse[SelectAiAgentAssetsData])
def select_ai_agent_assets() -> ApiResponse[SelectAiAgentAssetsData]:
    """Oracle Select AI Agent low-level asset names を返す。"""
    return ApiResponse(data=nl2sql_service.list_select_ai_agent_assets())


@router.post("/select-ai-agent/run-team", response_model=ApiResponse[AgentTeamRunData])
def run_select_ai_agent_team(
    req: AgentTeamRunRequest,
) -> ApiResponse[AgentTeamRunData]:
    """Oracle Select AI Agent team を実行する。"""
    _assert_select_ai_credential_ready()
    try:
        return ApiResponse(data=nl2sql_service.run_select_ai_agent_team(req))
    except OracleAdapterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/select-ai-agent/run-tool", response_model=ApiResponse[AgentTeamRunData])
def run_select_ai_agent_tool(
    req: AgentToolRunRequest,
) -> ApiResponse[AgentTeamRunData]:
    """Oracle Select AI Agent tool を明示名で実行する。"""
    _assert_select_ai_credential_ready()
    try:
        return ApiResponse(data=nl2sql_service.run_select_ai_agent_tool(req))
    except OracleAdapterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post(
    "/select-ai-agent/conversations/create",
    response_model=ApiResponse[AgentConversationCreateData],
)
def create_select_ai_agent_conversation(
    req: AgentConversationCreateRequest,
) -> ApiResponse[AgentConversationCreateData]:
    """Oracle Select AI Agent conversation を作成する（DBMS_CLOUD_AI.CREATE_CONVERSATION）。"""
    try:
        return ApiResponse(data=nl2sql_service.create_select_ai_agent_conversation(req))
    except OracleAdapterError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/select-ai-agent/assets/cleanup", response_model=ApiResponse[list[AssetCleanupData]])
def cleanup_select_ai_agent_assets(
    req: AssetCleanupRequest,
    request: Request,
) -> ApiResponse[list[AssetCleanupData]]:
    """Oracle Select AI Agent low-level assets の cleanup を実行する。"""
    _assert_profile_access(request, req.profile_id, default_profile=True)
    return ApiResponse(data=nl2sql_service.cleanup_select_ai_agent_assets_low_level(req))


@router.get(
    "/select-ai-agent/conversations",
    response_model=ApiResponse[AgentConversationsData],
)
def select_ai_agent_conversations(
    team_name: str | None = None,
    limit: int = 20,
) -> ApiResponse[AgentConversationsData]:
    """Oracle Select AI Agent conversation 履歴を返す。"""
    return ApiResponse(
        data=nl2sql_service.list_select_ai_agent_conversations(
            team_name=team_name,
            limit=max(1, min(limit, 100)),
        )
    )


@router.get(
    "/select-ai-agent/privileges/check",
    response_model=ApiResponse[AgentPrivilegeCheckData],
)
def check_select_ai_agent_privileges() -> ApiResponse[AgentPrivilegeCheckData]:
    """Oracle Select AI Agent 実行に必要な package/view 可視性を確認する。"""
    return ApiResponse(data=nl2sql_service.check_select_ai_agent_privileges())


@router.get("/history", response_model=ApiResponse[HistoryData])
def history(
    request: Request,
    paging: Annotated[
        OffsetParams, Depends(offset_params(default=DEFAULT_PAGE_LIMIT, max_limit=100))
    ],
    q: str = "",
    rating: str = "all",
    safety: str = "all",
    status: str = "all",
    kind: str = "all",
    since: str = "",
    executor: RunExecutorFilter = DEFAULT_RUN_EXECUTOR_FILTER,
    sort: str = "created_at",
    direction: str = "desc",
) -> ApiResponse[HistoryData]:
    """NL2SQL の実行履歴（任意のページへ移る offset 型。既定は新しい順。#1891）。

    system_admin は全員の履歴、それ以外は自分が実行した行と、自分の会話で他の人（system_admin）が
    実行した行（#1889・#1954）。`executor`（`all` / `mine` / `others`）は「実行者」の絞り込み。
    各行に `executed_by_me`・`executor_display_name`（実行した人の表示名）と、チャットの往復なら
    `conversation_owner_display_name`・`owned_by_me` を添える（会話の無い実行は None）。
    `status`（`all` / `succeeded` / `blocked`）・`kind`（`all` / `not_evaluation` / `chat` /
    `query` / `mcp` / `evaluation`）・`since`（実行日時の下限。ISO 8601）は 3 製品の実行履歴の
    共通の絞り込み（#1955・#1986。run-history.md）。画面の既定は `kind=not_evaluation`（Agent の
    品質評価の Run が MCP で呼んだ実行を隠す）。
    `unfiltered_total` は絞り込みを掛けない、見られる範囲の件数（品質評価を含まない種類なら
    品質評価の行を除く）。`hidden_evaluation_total` は既定で見える行が無いときに隠している品質評価の
    行の数。`others_visible` は
    他の人が実行した行が見えるか（画面の「実行者」の列と絞り込みを出す。#1955）。
    `sort`（`created_at` / `question`）と `direction`（`asc` / `desc`）はサーバーで並べ、
    ページをまたいで同じ順にする（#1764）。
    """
    if rating not in {"all", "good", "bad", "unrated"}:
        raise HTTPException(status_code=422, detail="rating が不正です。")
    if safety not in {"all", "safe", "blocked"}:
        raise HTTPException(status_code=422, detail="safety が不正です。")
    if sort not in HISTORY_SORT_KEYS:
        raise HTTPException(status_code=422, detail="sort が不正です。")
    if direction not in HISTORY_SORT_DIRECTIONS:
        raise HTTPException(status_code=422, detail="direction が不正です。")
    principal = getattr(request.state, "principal", None)
    viewer_user_uuid = str(getattr(principal, "user_uuid", "") or "") if principal else ""
    all_users = principal is None or can_view_all_users_data(principal)
    if not all_users and not viewer_user_uuid:
        return ApiResponse(
            data=HistoryData.model_validate({**dict(empty_page(paging)), "unfiltered_total": 0})
        )
    try:
        data = nl2sql_service.list_history(
            viewer_user_uuid=viewer_user_uuid,
            all_users=all_users,
            executor=executor,
            offset=paging.offset,
            limit=paging.limit,
            rating=rating,
            safety=safety,
            status=status,
            kind=kind,
            since=since,
            query=q.strip(),
            sort_key=sort,
            direction=direction,
        )
        is_system_admin = principal is not None and principal.is_system_admin
        others_visible = is_system_admin or nl2sql_service.history_has_other_executors(
            viewer_user_uuid=viewer_user_uuid
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse(
        data=data.model_copy(
            update={
                "others_visible": others_visible,
                "items": _with_history_people(
                    data.items,
                    viewer_user_uuid=viewer_user_uuid,
                    is_system_admin=principal is not None and principal.is_system_admin,
                    resolve_names=principal is not None,
                    principal=principal,
                ),
            }
        )
    )


_HISTORY_NOT_FOUND_DETAIL = "実行履歴が見つかりません。"


@router.get("/history/{history_id}", response_model=ApiResponse[HistoryItem])
def history_item(request: Request, history_id: str) -> ApiResponse[HistoryItem]:
    """実行履歴の 1 行（一覧の `?id=<run_id>` の全幅の詳細。#1955）。

    見え方は一覧と同じ（実行した人・会話の持ち主・system_admin。#1954）。見えない行と無い行は
    どちらも 404（存在を漏らさない）。認証の無い要求（認証無効）はすべての行を見られる。
    """
    principal = getattr(request.state, "principal", None)
    item = nl2sql_service.get_history_item(history_id)
    if item is None or (
        principal is not None
        and not can_access_run(
            principal,
            executor_user_uuid=item.actor_user_uuid,
            owner_user_uuid=item.conversation_owner_user_uuid or None,
        )
    ):
        raise HTTPException(status_code=OWNER_NOT_FOUND_STATUS, detail=_HISTORY_NOT_FOUND_DETAIL)
    viewer_user_uuid = str(getattr(principal, "user_uuid", "") or "") if principal else ""
    return ApiResponse(
        data=_with_history_people(
            [item],
            viewer_user_uuid=viewer_user_uuid,
            is_system_admin=principal is not None and principal.is_system_admin,
            resolve_names=principal is not None,
            principal=principal,
        )[0]
    )


@router.get("/history/{history_id}/trace", response_model=ApiResponse[TraceProgressData])
def history_item_trace(request: Request, history_id: str) -> ApiResponse[TraceProgressData]:
    """実行履歴の行の処理の経過（実行したジョブの span。#3226 の案 A）。

    見え方は `GET /history/{history_id}` と同じ（見えない行と無い行はどちらも 404）。
    """
    principal = getattr(request.state, "principal", None)
    item = nl2sql_service.get_history_item(history_id)
    if item is None or (
        principal is not None
        and not can_access_run(
            principal,
            executor_user_uuid=item.actor_user_uuid,
            owner_user_uuid=item.conversation_owner_user_uuid or None,
        )
    ):
        raise HTTPException(status_code=OWNER_NOT_FOUND_STATUS, detail=_HISTORY_NOT_FOUND_DETAIL)
    try:
        progress = nl2sql_service.history_trace(
            item,
            viewer_is_admin=principal is None or can_view_all_users_data(principal),
        )
    except TraceSpanReadError as exc:
        raise _trace_read_unavailable(exc) from exc
    return ApiResponse(data=progress)


def _with_history_people(
    items: list[HistoryItem],
    *,
    viewer_user_uuid: str,
    is_system_admin: bool,
    resolve_names: bool = True,
    principal: Any = None,
) -> list[HistoryItem]:
    """実行履歴の行に、実行した人と会話の持ち主の表示名と、見ている利用者との関係を添える（#1954）。

    保存した行は変えず、応答だけに補完する。実行した人の表示名は見る人を問わず返す（持ち主の
    会話で system_admin が実行した行に「〈名前〉が実行」を出すため）。ログイン ID
    （`actor_login_user_id`）は今までどおり system_admin にだけ返す（#1696）。
    利用者の無い要求（認証無効）は、すべての行を自分の実行とみなし、表示名を引かない。
    """
    if not items:
        return items
    people = sorted(
        {
            uuid
            for item in items
            for uuid in (
                item.actor_user_uuid,
                item.conversation_owner_user_uuid,
                item.cancelled_by_user_uuid,
            )
            if uuid
        }
    )
    identities = (
        get_security_service().display_identities(people) if people and resolve_names else {}
    )
    key_names = _history_cancel_key_names(items, principal) if resolve_names else {}

    def name_of(user_uuid: str) -> str:
        identity = identities.get(user_uuid)
        if identity is None:
            return ""
        return (identity.display_name or "").strip() or identity.login_user_id

    result: list[HistoryItem] = []
    for item in items:
        executor = item.actor_user_uuid
        owner = item.conversation_owner_user_uuid
        identity = identities.get(executor) if is_system_admin else None
        total_tokens, usage_measurement = run_usage_summary_fields(item.usage)
        result.append(
            item.model_copy(
                update={
                    "executed_by_me": not viewer_user_uuid or executor == viewer_user_uuid,
                    "executor_display_name": name_of(executor) if executor else "",
                    "conversation_owner_display_name": name_of(owner) if owner else None,
                    "owned_by_me": (
                        (not viewer_user_uuid or owner == viewer_user_uuid) if owner else None
                    ),
                    # 種類はきっかけ（MCP・品質評価。#1986）、無ければチャットの往復（会話の
                    # 持ち主あり）か SQL 生成か（#1955）。
                    "kind": item.run_source or ("chat" if owner else "query"),
                    "actor_login_user_id": identity.login_user_id if identity else "",
                    "actor_display_name": identity.display_name if identity else "",
                    # 工程の詳細は処理の経過（`GET /history/{history_id}/trace`）だけが返す。
                    "span_details": {},
                    # 一覧の「token」の列（合計の token と計測の状態。詳細の概要と同じ値。#3543）。
                    "total_tokens": total_tokens,
                    "usage_measurement": usage_measurement,
                    # 取り消した人と入口（停止の行だけ。#3348）。記録の値（利用者・キー・Agent の
                    # Run の ID）は返さず、見ている利用者に合わせた表示に置き換える。
                    "cancellation": _history_cancellation(
                        item,
                        name_of=name_of,
                        viewer_user_uuid=viewer_user_uuid,
                        key_names=key_names,
                    ),
                    "cancelled_by_user_uuid": "",
                    "cancel_via": "",
                    "cancel_api_key_id": "",
                    "cancel_agent_run_id": "",
                }
            )
        )
    return result


def _history_cancel_key_names(items: list[HistoryItem], principal: Any) -> dict[str, str]:
    """停止の行の API キーの名前（見ている利用者が既存のキーの一覧で見られるキーだけ。#3348）。

    system_admin は全員のキー、それ以外は自分のキーだけ（`AuthService.api_key_names`）。見られない
    キー・削除したキーは名前を返さない（画面は「MCP」だけ）。キーの ID・秘密は返さない。
    """
    key_ids = sorted({item.cancel_api_key_id for item in items if item.cancel_api_key_id})
    if not key_ids or principal is None:
        return {}
    try:
        names: dict[str, str] = get_security_service().api_key_names(principal, key_ids)
    except Exception:  # noqa: BLE001 - キーの名前は表示の補助（引けなければ「MCP」だけ）
        logger.warning("nl2sql_history_cancel_key_names_unavailable", exc_info=True)
        return {}
    return names


def _history_cancellation(
    item: HistoryItem,
    *,
    name_of: Callable[[str], str],
    viewer_user_uuid: str,
    key_names: Mapping[str, str],
) -> HistoryCancellation | None:
    """停止の行の「取消」（取り消した人と入口。#3348）。停止でない行は None。"""
    if item.run_status != HISTORY_RUN_STATUS_CANCELLED:
        return None
    actor = item.cancelled_by_user_uuid
    via: Literal["screen", "mcp", "agent_run"] = (
        "screen" if item.cancel_via != "mcp" else "agent_run" if item.cancel_agent_run_id else "mcp"
    )
    return HistoryCancellation(
        via=via,
        actor_recorded=bool(actor),
        actor_display_name=name_of(actor) if actor else "",
        by_me=bool(actor) and actor == viewer_user_uuid,
        api_key_name=key_names.get(item.cancel_api_key_id, "") if via == "mcp" else "",
    )


@router.post("/feedback", response_model=ApiResponse[FeedbackData])
def feedback(req: FeedbackRequest, request: Request) -> ApiResponse[FeedbackData]:
    """実行結果への feedback を保存する（SQL 生成の画面）。

    評価できるのは実行した本人だけ（#3244）。見える他の利用者の履歴（system_admin）は 403、
    見えない履歴は 404。評価した人を履歴の `feedback_actor_user_uuid` に残す。
    """
    try:
        access = _ai_use_access(request)
        data = nl2sql_service.save_feedback(
            req.history_id,
            req.rating,
            req.comment,
            actor_user_uuid=access.actor_user_uuid,
            actor_can_manage=access.actor_can_manage,
            allowed_profile_ids=_allowed_profile_ids_for_request(request),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="対象の SQL 履歴が見つかりません。") from exc
    except ProfileScopePermissionError as exc:
        raise _profile_access_denied() from exc
    except OthersContentForbiddenError as exc:
        raise _others_content_forbidden() from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=OWNER_NOT_FOUND_STATUS, detail="対象の SQL 履歴が見つかりません。"
        ) from exc
    except HistoryNotRatableError as exc:
        # 停止の行は評価できない（#3348）。
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ApiResponse(data=data)


@router.post("/feedback/admin-review", response_model=ApiResponse[AdminFeedbackReviewData])
def admin_review_feedback(
    req: AdminFeedbackReviewRequest,
    request: Request,
) -> ApiResponse[AdminFeedbackReviewData]:
    """管理者 review を保存し、必要な場合だけ Select AI feedback へ登録する。"""
    try:
        data = nl2sql_service.save_admin_feedback_review(
            req,
            allowed_profile_ids=_allowed_profile_ids_for_request(request),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="対象の SQL 履歴が見つかりません。") from exc
    except PermissionError as exc:
        raise _profile_access_denied() from exc
    except HistoryNotRatableError as exc:
        # 停止の行は評価できない（#3348）。
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse(data=data)


@router.get("/feedback", response_model=ApiResponse[FeedbackListData])
def list_feedback(
    request: Request,
    paging: Annotated[
        OffsetParams, Depends(offset_params(default=DEFAULT_PAGE_LIMIT, max_limit=100))
    ],
    rating: str = "all",
    profile_id: str = "",
    q: str = "",
) -> ApiResponse[FeedbackListData]:
    """アプリ内 SQL feedback を Profile/評価/キーワードで一覧する（offset 型。#1891）。"""
    if rating not in {"all", "good", "bad", "unrated"}:
        raise HTTPException(status_code=422, detail="rating が不正です。")
    if profile_id.strip():
        _assert_profile_access(request, profile_id.strip())
    try:
        data = nl2sql_service.list_feedback(
            offset=paging.offset,
            limit=paging.limit,
            rating=rating,
            profile_id=profile_id.strip(),
            query=q.strip(),
            allowed_profile_ids=_allowed_profile_ids_for_request(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse(data=data)


@router.delete("/feedback/{history_id}", response_model=ApiResponse[FeedbackClearData])
def clear_feedback(history_id: str, request: Request) -> ApiResponse[FeedbackClearData]:
    """SQL 履歴を残したままアプリ内 feedback だけを解除する。"""
    try:
        access = _actor_access_args(request, manage_permission=FEEDBACK_MANAGE_PERMISSION)
        data = nl2sql_service.clear_feedback(
            history_id,
            actor_user_uuid=access.actor_user_uuid,
            actor_can_manage=access.actor_can_manage,
            allowed_profile_ids=_allowed_profile_ids_for_request(request),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="対象の SQL 履歴が見つかりません。") from exc
    except ProfileScopePermissionError as exc:
        raise _profile_access_denied() from exc
    except PermissionError as exc:
        raise HTTPException(
            status_code=403,
            detail="他のユーザーのフィードバックを解除する権限がありません。",
        ) from exc
    return ApiResponse(data=data)


@router.post("/demo/learning", response_model=ApiResponse[DemoLearningData])
def seed_demo_learning() -> ApiResponse[DemoLearningData]:
    """Learning / feedback 画面の検証用 demo データを投入する。"""
    return ApiResponse(data=nl2sql_service.seed_demo_learning_data())


@router.get("/sample-data", response_model=ApiResponse[SampleDataInfo])
def sample_data_info(dataset: SampleDataset = SampleDataset.HR) -> ApiResponse[SampleDataInfo]:
    """Optional SQL Assist sample package status / SQL preview."""
    return ApiResponse(data=nl2sql_service.sample_data_info(dataset))


@router.post("/sample-data/import", response_model=ApiResponse[SampleDataMutationData])
def import_sample_data(
    req: SampleDataMutationRequest,
) -> ApiResponse[SampleDataMutationData]:
    """Optional SQL Assist sample data import execution."""
    return ApiResponse(data=nl2sql_service.import_sample_data(req))


@router.post("/sample-data/delete", response_model=ApiResponse[SampleDataMutationData])
def delete_sample_data(
    req: SampleDataMutationRequest,
) -> ApiResponse[SampleDataMutationData]:
    """Optional SQL Assist sample data delete execution."""
    return ApiResponse(data=nl2sql_service.delete_sample_data(req))


@router.get("/feedback-index", response_model=ApiResponse[FeedbackIndexData])
def feedback_index_status(request: Request) -> ApiResponse[FeedbackIndexData]:
    """Feedback learning vector index の状態を返す。"""
    return ApiResponse(
        data=nl2sql_service.feedback_index_status(
            allowed_profile_ids=_allowed_profile_ids_for_request(request),
        )
    )


@router.post("/feedback-index/rebuild", response_model=ApiResponse[FeedbackIndexData])
def rebuild_feedback_index(
    req: FeedbackIndexRequest,
    request: Request,
) -> ApiResponse[FeedbackIndexData]:
    """Feedback learning vector index の再構築 plan / 実行。"""
    return ApiResponse(
        data=nl2sql_service.rebuild_feedback_index(
            req,
            allowed_profile_ids=_allowed_profile_ids_for_request(request),
        )
    )


@router.post("/feedback-index/clear", response_model=ApiResponse[FeedbackIndexData])
def clear_feedback_index(
    req: FeedbackIndexRequest,
    request: Request,
) -> ApiResponse[FeedbackIndexData]:
    """Feedback learning vector index の clear plan / 実行。"""
    return ApiResponse(
        data=nl2sql_service.clear_feedback_index(
            req,
            allowed_profile_ids=_allowed_profile_ids_for_request(request),
        )
    )


@router.get("/feedback-entries", response_model=ApiResponse[FeedbackEntriesData])
def feedback_entries(request: Request) -> ApiResponse[FeedbackEntriesData]:
    """Feedback learning entries を一覧する。"""
    return ApiResponse(
        data=nl2sql_service.list_feedback_entries(
            allowed_profile_ids=_allowed_profile_ids_for_request(request),
        )
    )


@router.post("/feedback-entries/delete", response_model=ApiResponse[FeedbackEntriesData])
def delete_feedback_entries(
    req: FeedbackEntriesDeleteRequest,
    request: Request,
) -> ApiResponse[FeedbackEntriesData]:
    """Feedback learning entries を削除する。"""
    try:
        return ApiResponse(
            data=nl2sql_service.delete_feedback_entries(
                req.history_ids,
                allowed_profile_ids=_allowed_profile_ids_for_request(request),
            )
        )
    except PermissionError as exc:
        raise _profile_access_denied() from exc


@router.get("/feedback-config", response_model=ApiResponse[FeedbackSearchConfigData])
def feedback_config() -> ApiResponse[FeedbackSearchConfigData]:
    """Feedback similar-history default config を返す。"""
    return ApiResponse(data=nl2sql_service.feedback_search_config())


@router.patch("/feedback-config", response_model=ApiResponse[FeedbackSearchConfigData])
def update_feedback_config(
    req: FeedbackSearchConfigRequest,
) -> ApiResponse[FeedbackSearchConfigData]:
    """Feedback similar-history default config を更新する。"""
    return ApiResponse(data=nl2sql_service.update_feedback_search_config(req))


@router.get("/classifier", response_model=ApiResponse[ClassifierStatusData])
def classifier_status(request: Request) -> ApiResponse[ClassifierStatusData]:
    """Embedding + LogisticRegression classifier の状態を返す。"""
    return ApiResponse(
        data=nl2sql_service.classifier_status(
            allowed_profile_ids=_allowed_profile_ids_for_request(request),
        )
    )


@router.get("/classifier/training-data", response_model=ApiResponse[ClassifierTrainingDataData])
def classifier_training_data(request: Request) -> ApiResponse[ClassifierTrainingDataData]:
    """Classifier training data 一覧を返す。"""
    return ApiResponse(
        data=nl2sql_service.classifier_training_data(
            allowed_profile_ids=_allowed_profile_ids_for_request(request),
        )
    )


@router.get(
    "/classifier/training-candidates",
    response_model=ApiResponse[ClassifierTrainingCandidatesData],
)
def classifier_training_candidates(
    request: Request,
    paging: Annotated[
        OffsetParams, Depends(offset_params(default=DEFAULT_PAGE_LIMIT, max_limit=100))
    ],
    status: str = "all",
    profile_id: str = "",
    q: str = "",
    history_id: str = "",
) -> ApiResponse[ClassifierTrainingCandidatesData]:
    """good feedback から質問/Profile training 候補を導出する（offset 型。#1891）。"""
    allowed_statuses = {
        "all",
        "pending",
        "added",
        "already_covered",
        "conflict",
        "profile_missing",
        "source_changed",
    }
    if status not in allowed_statuses:
        raise HTTPException(status_code=422, detail="status が不正です。")
    if profile_id.strip():
        _assert_profile_access(request, profile_id.strip())
    try:
        data = nl2sql_service.classifier_training_candidates(
            offset=paging.offset,
            limit=paging.limit,
            status=status,
            profile_id=profile_id.strip(),
            query=q.strip(),
            history_id=history_id.strip(),
            allowed_profile_ids=_allowed_profile_ids_for_request(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse(data=data)


@router.post(
    "/classifier/training-data/from-feedback",
    response_model=ApiResponse[ClassifierFeedbackImportData],
)
def import_classifier_training_data_from_feedback(
    req: ClassifierFeedbackImportRequest,
    request: Request,
) -> ApiResponse[ClassifierFeedbackImportData]:
    """確認済み feedback の質問/Profile 対応を training data に追加する。"""
    for item in req.items:
        if item.profile_id:
            _assert_profile_access(request, item.profile_id)
    return ApiResponse(data=nl2sql_service.import_classifier_feedback_examples(req))


@router.patch(
    "/classifier/training-data/{example_id}",
    response_model=ApiResponse[ClassifierTrainingExample],
)
def update_classifier_training_example(
    example_id: str,
    req: ClassifierTrainingExampleUpdateRequest,
    request: Request,
) -> ApiResponse[ClassifierTrainingExample]:
    try:
        current = next(
            (
                item
                for item in nl2sql_service.classifier_training_data().examples
                if item.id == example_id
            ),
            None,
        )
        if current is None:
            raise KeyError(example_id)
        _assert_profile_access(request, current.profile_id)
        _assert_profile_access(request, req.profile_id)
        data = nl2sql_service.update_classifier_training_example(example_id, req)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Training data が見つかりません。") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ApiResponse(data=data)


@router.delete(
    "/classifier/training-data/{example_id}",
    response_model=ApiResponse[ClassifierTrainingDataData],
)
def delete_classifier_training_example(
    example_id: str,
    request: Request,
) -> ApiResponse[ClassifierTrainingDataData]:
    try:
        current = next(
            (
                item
                for item in nl2sql_service.classifier_training_data().examples
                if item.id == example_id
            ),
            None,
        )
        if current is None:
            raise KeyError(example_id)
        _assert_profile_access(request, current.profile_id)
        data = nl2sql_service.delete_classifier_training_example(example_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Training data が見つかりません。") from exc
    return ApiResponse(data=data)


@router.post("/classifier/training-data/import", response_model=ApiResponse[ClassifierImportData])
async def import_classifier_training_data(
    request: Request,
    file: Annotated[UploadFile, File()],
    replace: Annotated[bool, Form()] = False,
    profile_id: Annotated[str | None, Form()] = None,
) -> ApiResponse[ClassifierImportData]:
    """CATEGORY/TEXT の .xlsx training data テンプレートを取り込む。"""
    if profile_id:
        _assert_profile_access(request, profile_id)
    content = await _read_classifier_training_upload(file)
    try:
        return ApiResponse(
            data=await run_sync_io(
                nl2sql_service.import_classifier_training_data,
                filename=file.filename or "training_data.xlsx",
                content=content,
                replace=replace,
                profile_id=profile_id,
                allowed_profile_ids=_allowed_profile_ids_for_request(request),
            )
        )
    except TabularFileReadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/classifier/training-data/export.xlsx")
def export_classifier_training_data_xlsx(request: Request) -> Response:
    """Classifier training data を Excel workbook として出力する。"""
    filename, content = nl2sql_service.export_classifier_training_data_xlsx(
        allowed_profile_ids=_allowed_profile_ids_for_request(request)
    )
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/classifier/train", response_model=ApiResponse[ClassifierStatusData])
def train_classifier(
    req: ClassifierTrainRequest,
    request: Request,
) -> ApiResponse[ClassifierStatusData]:
    """Imported training data から LogisticRegression classifier を学習する。"""
    try:
        return ApiResponse(
            data=nl2sql_service.train_classifier(
                req,
                allowed_profile_ids=_allowed_profile_ids_for_request(request),
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/classifier/model/import", response_model=ApiResponse[ClassifierModelImportData])
async def import_classifier_model(
    file: Annotated[UploadFile, File()],
) -> ApiResponse[ClassifierModelImportData]:
    """唯一の classifier model を安全な JSON artifact で置き換える。"""
    content = await _read_classifier_model_upload(file)
    try:
        return ApiResponse(
            data=await run_sync_io(
                nl2sql_service.import_classifier_model_artifact,
                filename=file.filename or "classifier.json",
                content=content,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/classifier/models/import", response_model=ApiResponse[ClassifierModelImportData])
async def import_classifier_model_legacy(
    file: Annotated[UploadFile, File()],
    # 送られない bool の form は False（FastAPI 0.147 から）。`activate=true` を送ったときだけ
    # 受理する。
    activate: Annotated[bool, Form()] = False,
) -> ApiResponse[ClassifierModelImportData]:
    """旧複数形 URL。単一モデルとして置き換える場合（`activate=true`）のみ受理する。"""
    if not activate:
        raise HTTPException(
            status_code=422,
            detail="単一モデル管理では activate=false を指定できません。",
        )
    return await import_classifier_model(file)


@router.post("/classifier/predict", response_model=ApiResponse[ClassifierPredictionData])
def predict_classifier(
    req: ClassifierPredictRequest,
) -> ApiResponse[ClassifierPredictionData]:
    """質問を classifier category/profile 候補へ分類する。"""
    return ApiResponse(data=nl2sql_service.predict_classifier(req))


@router.post("/similar-history", response_model=ApiResponse[SimilarHistoryData])
def similar_history(
    req: SimilarHistoryRequest, request: Request
) -> ApiResponse[SimilarHistoryData]:
    """質問に近い履歴を few-shot / feedback 学習候補として返す。"""
    if req.profile_id:
        _assert_profile_access(request, req.profile_id)
    return ApiResponse(
        data=nl2sql_service.similar_history(
            req,
            allowed_profile_ids=_allowed_profile_ids_for_request(request),
        )
    )


@router.post("/recommend-profile", response_model=ApiResponse[ProfileRecommendationData])
def recommend_profile(
    req: ProfileRecommendationRequest,
    request: Request,
) -> ApiResponse[ProfileRecommendationData]:
    """質問から profile / schema 範囲と query rewrite を推薦する。"""
    if req.current_profile_id:
        _assert_profile_access(request, req.current_profile_id)
    try:
        return ApiResponse(
            data=nl2sql_service.recommend_profile(
                req,
                allowed_profile_ids=_allowed_profile_ids_for_request(request),
            )
        )
    except ValueError as exc:
        if "権限" in str(exc):
            raise _profile_access_denied() from exc
        raise


@router.post("/rewrite", response_model=ApiResponse[RewriteData])
def rewrite(req: RewriteRequest, request: Request) -> ApiResponse[RewriteData]:
    """用語・schema・追加指示を使って質問を書き換える。"""
    _assert_profile_access(request, req.profile_id, default_profile=True)
    return ApiResponse(data=nl2sql_service.rewrite(req))


@router.post("/analyze", response_model=ApiResponse[AnalyzeData])
def analyze(req: AnalyzeRequest, request: Request) -> ApiResponse[AnalyzeData]:
    """SQL の安全性・参照表・推奨修正を返す。"""
    try:
        allowed = nl2sql_service.resolve_direct_sql_allowed_objects(
            req.allowed_objects,
            profile_ids=_allowed_profile_ids_for_request(request),
        )
        data = nl2sql_service.analyze_sql(
            req.sql,
            allowed,
            req.row_limit,
            use_llm=req.use_llm,
        )
    except SchemaCatalogEmptyError:
        raise
    except ValueError as exc:
        # 入力 SQL に起因する解析失敗（不正な識別子など）は 500 ではなく 400 で返す。
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse(data=data)


@router.get(
    "/quality-evaluations/capabilities",
    response_model=ApiResponse[QualityEvaluationCapabilities],
)
def quality_evaluation_capabilities(
    request: Request,
    profile_id: Annotated[str | None, Query()] = None,
) -> ApiResponse[QualityEvaluationCapabilities]:
    """実行 engine / Judge の readiness と Excel 制限を返す。"""
    if profile_id:
        _assert_profile_access(request, profile_id)
    return ApiResponse(data=quality_evaluation_service.capabilities(profile_id=profile_id))


@router.get("/quality-evaluations/template.xlsx")
def quality_evaluation_template() -> Response:
    """日本語ヘッダーの入力テンプレートを返す。"""
    return Response(
        content=quality_evaluation_service.template_workbook(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                'attachment; filename="nl2sql_quality_evaluation_template.xlsx"'
            )
        },
    )


@router.post(
    "/quality-evaluations",
    response_model=ApiResponse[QualityEvaluationJobSummary],
    status_code=202,
)
async def create_quality_evaluation(
    request: Request,
    profile_id: Annotated[str, Form()],
    engines: Annotated[list[Nl2SqlEngine], Form()],
    repeat_count: Annotated[int, Form()],
    file: Annotated[UploadFile, File()],
) -> ApiResponse[QualityEvaluationJobSummary]:
    """Excel 入力を検証し、永続 SQL生成評価 job を投入する。"""
    _assert_profile_access(request, profile_id)
    maximum = get_settings().nl2sql_quality_evaluation_max_file_bytes
    content = await file.read(maximum + 1)
    principal = getattr(request.state, "principal", None)
    actor_user_uuid = str(getattr(principal, "user_uuid", ""))
    if uses_select_ai_credential(engines):
        # Select AI / Select AI Agent の評価は Credential が前提。
        # 評価の job を作る前に止める（#1688）。
        await run_sync_io(_assert_select_ai_credential_ready)
    try:
        data = await run_sync_io(
            quality_evaluation_service.submit,
            profile_id=profile_id,
            engines=engines,
            repeat_count=repeat_count,
            content=content,
            filename=file.filename or "evaluation.xlsx",
            actor_user_uuid=actor_user_uuid,
        )
        return ApiResponse(data=data)
    except QualityEvaluationValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "QUALITY_EVALUATION_VALIDATION_ERROR", "errors": exc.errors},
        ) from exc


@router.get(
    "/quality-evaluations",
    response_model=ApiResponse[QualityEvaluationJobPage],
)
def list_quality_evaluations(
    request: Request,
    paging: Annotated[
        OffsetParams, Depends(offset_params(default=DEFAULT_PAGE_LIMIT, max_limit=100))
    ],
) -> ApiResponse[QualityEvaluationJobPage]:
    """最近の SQL生成評価 job をページ取得する（offset 型。#1891）。"""
    page = quality_evaluation_service.list_jobs(
        offset=paging.offset,
        limit=paging.limit,
        allowed_profile_ids=_allowed_profile_ids_for_request(request),
    )
    return ApiResponse(data=page)


@router.get(
    "/quality-evaluations/{job_id}/results",
    response_model=ApiResponse[QualityEvaluationResultPage],
)
def quality_evaluation_results(
    job_id: str,
    request: Request,
    paging: Annotated[
        OffsetParams, Depends(offset_params(default=DEFAULT_PAGE_LIMIT, max_limit=100))
    ],
) -> ApiResponse[QualityEvaluationResultPage]:
    """SQL生成評価結果の明細をページ取得する（offset 型。#1891）。"""
    _quality_evaluation_job_for_access(job_id, request)
    try:
        return ApiResponse(
            data=quality_evaluation_service.list_results(
                job_id=job_id, offset=paging.offset, limit=paging.limit
            )
        )
    except QualityEvaluationJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/quality-evaluations/{job_id}/results.xlsx")
def quality_evaluation_results_xlsx(job_id: str, request: Request) -> Response:
    """完了した SQL生成評価の全結果を Excel で返す。"""
    _quality_evaluation_job_for_access(job_id, request)
    try:
        filename, content = quality_evaluation_service.results_workbook(job_id)
    except QualityEvaluationJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except QualityEvaluationJobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/quality-evaluations/{job_id}/cancel",
    response_model=ApiResponse[QualityEvaluationJobSummary],
)
def cancel_quality_evaluation(
    job_id: str,
    request: Request,
) -> ApiResponse[QualityEvaluationJobSummary]:
    """待機中または実行中の SQL生成評価 job を中止する。"""
    try:
        _quality_evaluation_job_for_access(job_id, request, require_actor_owner=True)
        return ApiResponse(data=quality_evaluation_service.cancel_job(job_id))
    except QualityEvaluationJobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete(
    "/quality-evaluations/{job_id}",
    response_model=ApiResponse[QualityEvaluationJobSummary],
)
def delete_quality_evaluation(
    job_id: str,
    request: Request,
) -> ApiResponse[QualityEvaluationJobSummary]:
    """完了済みの SQL生成評価 job と結果明細を削除する。"""
    try:
        _quality_evaluation_job_for_access(job_id, request, require_actor_owner=True)
        return ApiResponse(data=quality_evaluation_service.delete_job(job_id))
    except QualityEvaluationJobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/quality-evaluations/{job_id}",
    response_model=ApiResponse[QualityEvaluationJobSummary],
)
def get_quality_evaluation(
    job_id: str,
    request: Request,
) -> ApiResponse[QualityEvaluationJobSummary]:
    """SQL生成評価 job の進捗と集計を返す。"""
    try:
        return ApiResponse(data=_quality_evaluation_job_for_access(job_id, request, wake=True))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/reverse", response_model=ApiResponse[ReverseSqlData])
def reverse(req: ReverseSqlRequest, request: Request) -> ApiResponse[ReverseSqlData]:
    """SQL から自然言語説明を生成する。

    profile の glossary / カタログ(論理名・コメント)を使うため、他ルートと同じく
    principal に許可された profile だけを受け付ける。
    """
    _assert_profile_access(request, req.profile_id, default_profile=True)
    try:
        return ApiResponse(data=nl2sql_service.reverse_sql(req))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reverse/deep", response_model=ApiResponse[ReverseSqlData])
def reverse_deep(req: ReverseSqlRequest, request: Request) -> ApiResponse[ReverseSqlData]:
    """SQL から Enterprise AI backed の自然言語説明を生成する。

    schema context を Enterprise AI へ送るため、許可 profile の検証は必須。
    """
    _assert_profile_access(request, req.profile_id, default_profile=True)
    try:
        return ApiResponse(data=nl2sql_service.reverse_sql_deep(req))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reverse/sql", response_model=ApiResponse[StructureToSqlData])
def structure_to_sql(
    req: StructureToSqlRequest, request: Request
) -> ApiResponse[StructureToSqlData]:
    """論理構造から SQL を生成する。DB 実行・履歴保存は行わない。"""
    _assert_profile_access(request, req.profile_id, default_profile=True)
    try:
        return ApiResponse(data=nl2sql_service.structure_to_sql(req))
    except EnterpriseAiDirectError as exc:
        raise _enterprise_ai_sql_generation_exception(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/reverse/question-sql", response_model=ApiResponse[StructureToSqlData])
def question_to_sql(req: QuestionToSqlRequest, request: Request) -> ApiResponse[StructureToSqlData]:
    """表示中の自然言語質問から SQL を生成する。実行・履歴保存は行わない。"""
    _assert_profile_access(request, req.profile_id, default_profile=True)
    try:
        return ApiResponse(data=nl2sql_service.question_to_sql(req))
    except EnterpriseAiDirectError as exc:
        raise _enterprise_ai_sql_generation_exception(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/comments/suggest", response_model=ApiResponse[CommentSuggestionData])
def suggest_comments(
    req: CommentSuggestionRequest | None = None,
) -> ApiResponse[CommentSuggestionData]:
    """表/列コメント候補を deterministic / Enterprise AI で生成する。"""
    return ApiResponse(data=nl2sql_service.suggest_comments(req))


@router.post("/comments/generate-sql", response_model=ApiResponse[MetadataSqlGenerateData])
def generate_comment_sql(
    req: MetadataSqlGenerateRequest,
) -> ApiResponse[MetadataSqlGenerateData]:
    """SQL Assist コメント管理互換の COMMENT ON SQL を生成する。"""
    try:
        return ApiResponse(data=nl2sql_service.generate_comment_sql(req))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/metadata-samples", response_model=ApiResponse[MetadataSqlSampleData])
def metadata_samples(
    req: MetadataSqlSampleRequest,
) -> ApiResponse[MetadataSqlSampleData]:
    """コメント/アノテーション SQL 生成向けの列代表値を再取得する。"""
    try:
        return ApiResponse(data=nl2sql_service.get_metadata_samples(req))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/comments/apply", response_model=ApiResponse[CommentApplyData])
def apply_comments(req: CommentApplyRequest) -> ApiResponse[CommentApplyData]:
    """COMMENT ON TABLE/COLUMN の restricted execution。"""
    return ApiResponse(data=nl2sql_service.apply_comments(req))


@router.post("/annotations/generate", response_model=ApiResponse[AnnotationSuggestionData])
def generate_annotations() -> ApiResponse[AnnotationSuggestionData]:
    """Oracle annotation 候補を生成する。"""
    return ApiResponse(data=nl2sql_service.suggest_annotations())


@router.post("/annotations/generate-sql", response_model=ApiResponse[MetadataSqlGenerateData])
def generate_annotation_sql(
    req: MetadataSqlGenerateRequest,
) -> ApiResponse[MetadataSqlGenerateData]:
    """SQL Assist アノテーション管理互換の ALTER ... ANNOTATIONS SQL を生成する。"""
    try:
        return ApiResponse(data=nl2sql_service.generate_annotation_sql(req))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/annotations/apply", response_model=ApiResponse[AnnotationApplyData])
def apply_annotations(req: AnnotationApplyRequest) -> ApiResponse[AnnotationApplyData]:
    """Oracle annotation の restricted execution。"""
    return ApiResponse(data=nl2sql_service.apply_annotations(req))


@router.post("/domains/inventory", response_model=ApiResponse[DomainInventoryData])
def domain_inventory(req: DomainInventoryRequest) -> ApiResponse[DomainInventoryData]:
    """対象表の列に付いた既存ドメインの定義と関連付け先を返す。"""
    try:
        return ApiResponse(data=nl2sql_service.get_domain_inventory(req))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/domains/generate-sql", response_model=ApiResponse[MetadataSqlGenerateData])
def generate_domain_sql(
    req: MetadataSqlGenerateRequest,
) -> ApiResponse[MetadataSqlGenerateData]:
    """ドメイン管理の CREATE DOMAIN / ALTER TABLE ... MODIFY (... DOMAIN ...) SQL を生成する。"""
    try:
        return ApiResponse(data=nl2sql_service.generate_domain_sql(req))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/db-admin/tables", response_model=ApiResponse[DbAdminObjectsData])
def db_admin_tables() -> ApiResponse[DbAdminObjectsData]:
    """DB admin table 一覧を返す。"""
    return ApiResponse(data=nl2sql_service.list_db_admin_tables())


@router.get("/db-admin/objects", response_model=ApiResponse[DbAdminObjectPage])
def db_admin_objects(
    response: Response,
    paging: Annotated[CursorParams, Depends(cursor_params(default=50, max_limit=100))],
    q: str = "",
    owner: str = "",
    owner_prefix: str = "",
    query_scope: Literal["all", "name_comment"] = "all",
    type: str = "all",  # noqa: A002 - public query parameter name
    row_state: str = "all",
    include_counts: bool = True,
    if_none_match: Annotated[str | None, Header(alias="If-None-Match")] = None,
) -> ApiResponse[DbAdminObjectPage] | Response:
    """データ管理向け軽量 object page。全量 Catalog/CLOB は読み込まない。"""
    if type not in {"all", "table", "view"}:
        raise HTTPException(status_code=422, detail="type が不正です。")
    if row_state not in {"all", "with_rows", "empty_rows", "unknown_rows"}:
        raise HTTPException(status_code=422, detail="row_state が不正です。")
    if query_scope not in {"all", "name_comment"}:
        raise HTTPException(status_code=422, detail="query_scope が不正です。")
    try:
        page = nl2sql_service.list_db_admin_objects_page(
            cursor=paging.cursor,
            limit=paging.limit,
            query=q,
            owner=owner,
            owner_prefix=owner_prefix,
            query_scope=query_scope,
            object_type=type,
            row_state=row_state,
            include_counts=include_counts,
        )
    except InvalidCursorError as exc:
        raise _invalid_cursor(exc) from exc
    quoted_etag = f'"schema-{page.catalog_version}"'
    if if_none_match == quoted_etag:
        return Response(status_code=304, headers={"ETag": quoted_etag})
    response.headers["ETag"] = quoted_etag
    return ApiResponse(data=page)


@router.get("/db-admin/tables/{table_name}", response_model=ApiResponse[DbAdminObjectDetail])
def db_admin_table_detail(
    table_name: str, include_ddl: bool = True, exact_count: bool = False, owner: str = ""
) -> ApiResponse[DbAdminObjectDetail]:
    """DB admin table 詳細/DDL を返す。

    include_ddl=false で重い GET_DDL を省略(列一覧の初期表示を高速化)。
    exact_count=false は num_rows 統計、true のみ COUNT(*) で正確件数を取得。
    """
    try:
        return ApiResponse(
            data=nl2sql_service.get_db_admin_object(
                table_name, "table", owner=owner, include_ddl=include_ddl, exact_count=exact_count
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/db-admin/views", response_model=ApiResponse[DbAdminObjectsData])
def db_admin_views() -> ApiResponse[DbAdminObjectsData]:
    """DB admin view 一覧を返す。"""
    return ApiResponse(data=nl2sql_service.list_db_admin_views())


@router.get("/db-admin/views/{view_name}", response_model=ApiResponse[DbAdminObjectDetail])
def db_admin_view_detail(
    view_name: str, include_ddl: bool = True, exact_count: bool = False, owner: str = ""
) -> ApiResponse[DbAdminObjectDetail]:
    """DB admin view 詳細/DDL を返す。

    include_ddl=false で重い GET_DDL を省略(列一覧の初期表示を高速化)。
    exact_count=false は num_rows 統計、true のみ COUNT(*) で正確件数を取得(view は通常 None)。
    """
    try:
        return ApiResponse(
            data=nl2sql_service.get_db_admin_object(
                view_name, "view", owner=owner, include_ddl=include_ddl, exact_count=exact_count
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/db-admin/drop-table", response_model=ApiResponse[DbAdminExecuteData])
def db_admin_drop_table(req: DbAdminDropTableRequest) -> ApiResponse[DbAdminExecuteData]:
    """DB admin DROP TABLE execution。"""
    try:
        return ApiResponse(data=nl2sql_service.drop_db_admin_table(req))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/db-admin/truncate-table", response_model=ApiResponse[DbAdminExecuteData])
def db_admin_truncate_table(req: DbAdminTruncateTableRequest) -> ApiResponse[DbAdminExecuteData]:
    """DB admin TRUNCATE TABLE execution。"""
    try:
        return ApiResponse(data=nl2sql_service.truncate_db_admin_table(req))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/db-admin/execute", response_model=ApiResponse[DbAdminExecuteData])
def db_admin_execute(req: DbAdminExecuteRequest) -> ApiResponse[DbAdminExecuteData]:
    """DB admin SQL executor。通常 NL2SQL 実行 path とは分離する。"""
    return ApiResponse(data=nl2sql_service.execute_db_admin_sql(req))


@router.post("/db-admin/statements", response_model=ApiResponse[DbAdminExecuteData])
def db_admin_statements(req: DbAdminStatementsRequest) -> ApiResponse[DbAdminExecuteData]:
    """文種 whitelist 付き複数 statement 実行(テーブル/ビュー作成・データ SQL)。"""
    return ApiResponse(data=nl2sql_service.execute_db_admin_statements(req))


@router.post("/db-admin/drop-view", response_model=ApiResponse[DbAdminExecuteData])
def db_admin_drop_view(req: DbAdminDropViewRequest) -> ApiResponse[DbAdminExecuteData]:
    """DB admin DROP VIEW execution。"""
    try:
        return ApiResponse(data=nl2sql_service.drop_db_admin_view(req))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/db-admin/preview-data", response_model=ApiResponse[DbAdminDataPreviewData])
def db_admin_preview_data(
    req: DbAdminDataPreviewRequest,
) -> ApiResponse[DbAdminDataPreviewData]:
    """テーブル/ビューのデータ表示(件数上限+任意 WHERE)。"""
    try:
        return ApiResponse(data=nl2sql_service.preview_db_admin_data(req))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/db-admin/preview-data/export.xlsx")
def db_admin_export_preview_xlsx(req: DbAdminDataPreviewRequest) -> Response:
    """テーブル/ビューの表示結果を Excel workbook として出力する。"""
    try:
        filename, content = nl2sql_service.export_db_admin_preview_xlsx(req)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/db-admin/upload-csv", response_model=ApiResponse[DbAdminCsvUploadData])
def db_admin_upload_csv(
    req: DbAdminCsvUploadRequest,
) -> ApiResponse[DbAdminCsvUploadData]:
    """既存テーブルへの CSV アップロード(INSERT / TRUNCATE&INSERT)。"""
    try:
        return ApiResponse(data=nl2sql_service.upload_db_admin_csv(req))
    except TabularImportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/db-admin/analyze-error", response_model=ApiResponse[DbAdminAiAnalysisData])
def db_admin_analyze_error(
    req: DbAdminAiAnalysisRequest,
) -> ApiResponse[DbAdminAiAnalysisData]:
    """Admin SQL 実行結果の AI 分析(OCI Enterprise AI、未設定時は deterministic)。"""
    return ApiResponse(data=nl2sql_service.analyze_db_admin_failure(req))


# ---- DB 管理チャット（#2264）。画面だけの API（Cookie / CSRF）。API キー・MCP・Agent は使えない


# 会話の履歴（#2319）の見え方は「AI 活用」と同じ（system_admin は全員、それ以外は本人だけ。他人と
# 無い会話は 404）。


def _db_admin_chat_actor(request: Request) -> tuple[str, bool]:
    principal = getattr(request.state, "principal", None)
    return str(getattr(principal, "user_uuid", "") or ""), _chat_all_users(request)


def _with_db_admin_chat_senders(turns: list[DbAdminChatTurn]) -> list[DbAdminChatTurn]:
    """持ち主以外（system_admin）が送ったターンに、送り手の表示名を足す（チャットと同じ。#1928）。"""
    senders = sorted({turn.sender_user_uuid for turn in turns if turn.sender_user_uuid})
    names = _chat_owner_names(senders) if senders else {}
    # 工程の詳細は処理の経過（`GET .../turns/{turn_id}/trace`）だけが返す。
    return [
        turn.model_copy(update={"span_details": {}})
        if turn.sender_user_uuid is None
        else turn.model_copy(
            update={
                "sender_display_name": names.get(turn.sender_user_uuid, ""),
                "span_details": {},
            }
        )
        for turn in turns
    ]


def _client_stopped_waiting(request: Request) -> bool:
    """画面が生成を待つのをやめた（停止した・接続が切れた）か。分からなければ待っているとみなす。"""
    try:
        return bool(anyio.from_thread.run(request.is_disconnected))
    except RuntimeError:
        return False


@router.get("/db-admin-chat/conversations", response_model=ApiResponse[DbAdminChatPage])
def list_db_admin_chats(
    request: Request,
    paging: Annotated[
        OffsetParams, Depends(offset_params(default=DEFAULT_PAGE_LIMIT, max_limit=100))
    ],
) -> ApiResponse[DbAdminChatPage]:
    """DB 管理チャットの会話の履歴（新しい順。offset 型）。

    最後の更新が会話の履歴の期間（共通の設定 `PLATFORM_CHAT_HISTORY_DAYS`）の中の会話だけ（#2340）。
    """
    actor, all_users = _db_admin_chat_actor(request)
    try:
        page = nl2sql_service.list_db_admin_chats(
            actor=actor,
            all_users=all_users,
            offset=paging.offset,
            limit=paging.limit,
            history_days=chat_history_days(get_settings()),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return ApiResponse(data=_with_chat_owners(page, actor=actor, all_users=all_users))


@router.get(
    "/db-admin-chat/conversations/{conversation_id}/turns/{turn_id}/trace",
    response_model=ApiResponse[TraceProgressData],
)
def get_db_admin_chat_turn_trace(
    conversation_id: str, turn_id: str, request: Request
) -> ApiResponse[TraceProgressData]:
    """DB 管理チャットのターンの処理の経過（生成のときの span。#3226 の案 A）。

    見え方は会話と同じ（system_admin は全員、それ以外は本人の会話だけ。他人と無いターンは 404）。
    """
    actor, all_users = _db_admin_chat_actor(request)
    try:
        progress = nl2sql_service.db_admin_chat_turn_trace(
            conversation_id, turn_id, actor=actor, all_users=all_users
        )
    except (PermissionError, LookupError) as exc:
        raise _chat_not_found() from exc
    except TraceSpanReadError as exc:
        raise _trace_read_unavailable(exc) from exc
    return ApiResponse(data=progress)


@router.get(
    "/db-admin-chat/conversations/{conversation_id}", response_model=ApiResponse[DbAdminChatData]
)
def get_db_admin_chat(conversation_id: str, request: Request) -> ApiResponse[DbAdminChatData]:
    """DB 管理チャットの会話の内容（ターン・生成した SQL・最後の実行の要約）。"""
    actor, all_users = _db_admin_chat_actor(request)
    try:
        data = nl2sql_service.get_db_admin_chat(conversation_id, actor=actor, all_users=all_users)
    except (PermissionError, LookupError) as exc:
        raise _chat_not_found() from exc
    return ApiResponse(
        data=data.model_copy(
            update={
                "conversation": _with_chat_owner(
                    data.conversation, actor=actor, all_users=all_users
                ),
                "turns": _with_db_admin_chat_senders(data.turns),
            }
        )
    )


@router.patch(
    "/db-admin-chat/conversations/{conversation_id}",
    response_model=ApiResponse[DbAdminChatSummary],
)
def rename_db_admin_chat(
    conversation_id: str, update: SqlChatUpdateRequest, request: Request
) -> ApiResponse[DbAdminChatSummary]:
    """DB 管理チャットの会話の名前を変える（チャットと同じ規則: 1〜80 文字）。"""
    actor, all_users = _db_admin_chat_actor(request)
    try:
        data = nl2sql_service.rename_db_admin_chat(
            conversation_id, actor=actor, all_users=all_users, title=update.title
        )
    except OthersContentForbiddenError as exc:
        raise _others_content_forbidden() from exc
    except (PermissionError, LookupError) as exc:
        raise _chat_not_found() from exc
    return ApiResponse(data=_with_chat_owner(data, actor=actor, all_users=all_users))


@router.delete("/db-admin-chat/conversations/{conversation_id}", response_model=ApiResponse[None])
def delete_db_admin_chat(conversation_id: str, request: Request) -> ApiResponse[None]:
    """DB 管理チャットの会話を消す（実行の監査の表は残す）。"""
    actor, all_users = _db_admin_chat_actor(request)
    try:
        nl2sql_service.delete_db_admin_chat(conversation_id, actor=actor, all_users=all_users)
    except (PermissionError, LookupError) as exc:
        raise _chat_not_found() from exc
    return ApiResponse(data=None)


@router.put(
    "/db-admin-chat/conversations/{conversation_id}/turns/{turn_id}/plan",
    response_model=ApiResponse[DbAdminChatTurn],
)
def replace_db_admin_chat_turn_sql(
    conversation_id: str, turn_id: str, req: DbAdminChatPlanUpdateRequest, request: Request
) -> ApiResponse[DbAdminChatTurn]:
    """ターンの SQL を、AI のエラー分析で直した SQL に置き換える（サーバーが分類し直す）。"""
    actor, all_users = _db_admin_chat_actor(request)
    try:
        turn = nl2sql_service.replace_db_admin_chat_turn_sql(
            conversation_id,
            turn_id,
            req.sql,
            actor=actor,
            all_users=all_users,
            statement_index=req.statement_index,
        )
    except OthersContentForbiddenError as exc:
        raise _others_content_forbidden() from exc
    except (PermissionError, LookupError) as exc:
        raise HTTPException(
            status_code=OWNER_NOT_FOUND_STATUS, detail=_CHAT_TURN_NOT_FOUND_DETAIL
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    [named] = _with_db_admin_chat_senders([turn])
    return ApiResponse(data=named)


@router.post("/db-admin-chat/generate", response_model=ApiResponse[DbAdminChatSendData])
def db_admin_chat_generate(
    req: DbAdminChatGenerateRequest, request: Request
) -> ApiResponse[DbAdminChatSendData]:
    """質問から Oracle の SQL を作り、会話のターンとして保存する（Enterprise AI Direct）。

    前の往復はサーバーが保存した会話から復元する。画面が停止した（待つのをやめた）生成は保存しない。
    """
    actor, all_users = _db_admin_chat_actor(request)
    try:
        data = nl2sql_service.send_db_admin_chat_question(
            req,
            actor=actor,
            all_users=all_users,
            keep_result=lambda: not _client_stopped_waiting(request),
        )
    except DbAdminChatGenerationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except DbAdminChatConversationFullError as exc:
        raise HTTPException(
            status_code=409,
            detail="この会話は往復の上限に達しました。新しい会話を始めてください。",
        ) from exc
    except OthersContentForbiddenError as exc:
        raise _others_content_forbidden() from exc
    except (PermissionError, LookupError) as exc:
        raise _chat_not_found() from exc
    if data is None:
        # 画面は応答を待っていない（停止した）。保存していないことだけを返す。
        raise HTTPException(status_code=409, detail="生成を停止したため、保存しませんでした。")
    return ApiResponse(
        data=data.model_copy(
            update={
                "conversation": _with_chat_owner(
                    data.conversation, actor=actor, all_users=all_users
                ),
                "turn": _with_db_admin_chat_senders([data.turn])[0],
            }
        )
    )


@router.post("/db-admin-chat/execute", response_model=ApiResponse[DbAdminChatExecuteData])
def db_admin_chat_execute(
    req: DbAdminChatTurnExecuteRequest, request: Request
) -> ApiResponse[DbAdminChatExecuteData]:
    """ターンに保存した SQL の 1 文（`mode="statement"`）か、すべての文（`mode="all"`。
    最初のエラーで
    止める）を実行する。SELECT は押せば実行、
    変える文は確認語があるときだけ（実行した文ごとに監査の表に
    残し、ターンに文ごとの最後の実行の要約を残す）。"""
    actor, all_users = _db_admin_chat_actor(request)
    try:
        data = nl2sql_service.execute_db_admin_chat_turn(req, actor=actor, all_users=all_users)
    except OthersContentForbiddenError as exc:
        raise _others_content_forbidden() from exc
    except (PermissionError, LookupError) as exc:
        raise HTTPException(
            status_code=OWNER_NOT_FOUND_STATUS, detail=_CHAT_TURN_NOT_FOUND_DETAIL
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiResponse(data=data)


@router.post(
    "/db-admin-chat/conversations/{conversation_id}/turns/{turn_id}/analyze-error",
    response_model=ApiResponse[DbAdminChatAnalyzeData],
)
def db_admin_chat_analyze_error(
    conversation_id: str,
    turn_id: str,
    req: DbAdminChatAnalyzeRequest,
    request: Request,
) -> ApiResponse[DbAdminChatAnalyzeData]:
    """保存された文の失敗と履歴を分析する。"""
    actor, all_users = _db_admin_chat_actor(request)
    try:
        return ApiResponse(
            data=nl2sql_service.analyze_db_admin_chat_failure(
                conversation_id,
                turn_id,
                req,
                actor=actor,
                all_users=all_users,
            )
        )
    except OthersContentForbiddenError as exc:
        raise _others_content_forbidden() from exc
    except (PermissionError, LookupError) as exc:
        raise HTTPException(
            status_code=OWNER_NOT_FOUND_STATUS, detail=_CHAT_TURN_NOT_FOUND_DETAIL
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/db-admin/extract-join-where", response_model=ApiResponse[DbAdminJoinWhereData])
def db_admin_extract_join_where(
    req: DbAdminJoinWhereRequest,
) -> ApiResponse[DbAdminJoinWhereData]:
    """ビュー DDL から JOIN/WHERE 条件を抽出する(OCI Enterprise AI、未設定時は deterministic)。"""
    return ApiResponse(data=nl2sql_service.extract_db_admin_join_where(req))


@router.post("/db-admin/import-tabular", response_model=ApiResponse[DbAdminImportTabularData])
def db_admin_import_tabular(
    req: DbAdminImportTabularRequest,
) -> ApiResponse[DbAdminImportTabularData]:
    """CSV/XLSX/XLS tabular data を DB admin tool から import する。"""
    try:
        return ApiResponse(data=nl2sql_service.import_db_admin_tabular(req))
    except TabularImportValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/db-admin/tables/{table_name}/export.xlsx")
def db_admin_export_table_xlsx(table_name: str, limit: int = 1000, owner: str = "") -> Response:
    """DB admin table の列情報を Excel workbook として出力する。"""
    try:
        filename, content = nl2sql_service.export_db_admin_table_xlsx(
            table_name,
            limit=max(1, min(limit, 50000)),
            owner=owner,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/db-admin/views/{view_name}/export.xlsx")
def db_admin_export_view_xlsx(view_name: str, limit: int = 1000, owner: str = "") -> Response:
    """DB admin view の列情報を Excel workbook として出力する。"""
    try:
        filename, content = nl2sql_service.export_db_admin_view_xlsx(
            view_name,
            limit=max(1, min(limit, 50000)),
            owner=owner,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/synthetic-data/generate",
    status_code=202,
    deprecated=True,
    response_model=ApiResponse[SyntheticDataOperationData],
)
def generate_synthetic_data(
    req: SyntheticDataGenerateRequest,
    request: Request,
) -> ApiResponse[SyntheticDataOperationData]:
    """旧クライアントも永続受理へ統合し、手続き応答を生成成功と扱わない。"""
    from uuid import uuid4

    from .models import TimingEnvelope
    from .synthetic_models import SyntheticRunRequest
    from .synthetic_service import get_synthetic_service

    run = get_synthetic_service().create(
        SyntheticRunRequest(**req.model_dump(), idempotency_key=str(uuid4())),
        getattr(request.state, "principal", None),
    )
    return ApiResponse(
        data=SyntheticDataOperationData(
            table_name=run.targets[0].table_name,
            object_list=[target.table_name for target in run.targets],
            row_count=run.targets[0].requested_rows,
            executed=False,
            runtime="oracle",
            status="accepted",
            message="生成を受け付けました。生成状況で完了と追加件数を確認してください。",
            engine_meta={
                "run_id": run.run_id,
                "status_url": f"/api/nl2sql/synthetic-data/runs/{run.run_id}",
            },
            timing=TimingEnvelope(created_at=run.created_at, stage_timings=[]),
        )
    )


@router.get("/synthetic-data/results", response_model=ApiResponse[SyntheticDataResultsData])
def synthetic_data_results(
    table_name: str,
    limit: int = Query(default=100, ge=1, le=100000),
) -> ApiResponse[SyntheticDataResultsData]:
    """Synthetic DB data generation 後の table preview を返す。"""
    try:
        return ApiResponse(
            data=nl2sql_service.synthetic_data_results(
                table_name=table_name,
                limit=limit,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/diagnostics", response_model=ApiResponse[DiagnosticsData])
def diagnostics() -> ApiResponse[DiagnosticsData]:
    """OCI / Oracle / NL2SQL エンジン設定の非 secret 診断を返す。"""
    return ApiResponse(data=nl2sql_service.diagnostics())


from .synthetic_router import router as synthetic_router  # noqa: E402

router.include_router(synthetic_router)
