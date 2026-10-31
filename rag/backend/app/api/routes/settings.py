"""設定 API。"""

import asyncio
import logging
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pr_backend_core.logging import safe_exception_fields
from pr_backend_core.observability.model_purpose import async_call_with_model_purpose
from pr_system_settings.answer_notice import build_answer_notice_router
from pr_system_settings.auth.errors import ROUTE_FORBIDDEN_CODE, SecurityApiError
from pr_system_settings.chat_attachments import build_chat_attachment_settings_router
from pr_system_settings.database import build_database_router
from pr_system_settings.model import build_model_router
from pr_system_settings.model_test_input import (
    MODEL_TEST_EMBEDDING_INPUT,
    MODEL_TEST_IMAGE_BYTES,
    MODEL_TEST_IMAGE_MIME_TYPE,
    MODEL_TEST_RERANK_DOCUMENTS,
    MODEL_TEST_RERANK_QUERY,
    MODEL_TEST_TEXT_PROMPT,
    MODEL_TEST_VISION_PROMPT,
    model_test_text_context,
)
from pr_system_settings.oci import build_oci_router
from pr_system_settings.oci import test_oci_config as _test_oci_config
from pr_system_settings.system_schema import system_tables_status_error
from pr_system_settings.trace_recording import build_trace_recording_router
from pr_system_settings.upload_storage import (
    build_upload_storage_router,
)
from rag_parser_core.capabilities import ADAPTER_CAPABILITIES, supported_modalities
from rag_parser_core.language import FIELD_SPECS

from app import config as app_config
from app.api.routes.documents import global_document_processing_config
from app.clients.oci_enterprise_ai import OciEnterpriseAiClient
from app.clients.oci_genai import OciGenAiClient
from app.clients.oracle import (
    OracleClient,
    close_oracle_pool,
    test_oracle_connection,
)
from app.clients.oracle_run_history import RunHistoryStore
from app.config import (
    MODEL_SETTINGS_STORE,
    PARSER_SETTINGS_STORE,
    SMALL_TO_BIG_SETTING_FIELDS,
    Settings,
    get_settings,
)
from app.rag.answer_prompts import (
    EDITABLE_PROMPT_KEYS,
    readonly_prompt_stages,
    required_tokens,
    stage_prompt_permission,
)
from app.rag.answer_prompts import default_prompt as default_answer_prompt
from app.rag.answer_prompts import required_placeholders as answer_prompt_required_placeholders
from app.rag.answer_prompts import validate_prompt as validate_answer_prompt
from app.rag.chunking_strategy import (
    chunking_runtime_settings,
    normalize_chunking_strategy,
)
from app.rag.evaluation_adapter import (
    evaluation_adapter_runtime_settings,
    normalize_evaluation_suite,
)
from app.rag.extraction_field_adapter import (
    FieldDefinition,
    load_saved_field_schema,
    reset_field_schema,
    save_field_schema,
    standard_field_definitions,
)
from app.rag.graph_adapter import (
    graph_adapter_runtime_settings,
    normalize_graph_profile,
)
from app.rag.oracle_schema import vector_index_reindex_sql
from app.rag.parser_adapter_readiness import parser_adapter_runtime_settings
from app.rag.parser_adapter_scorecard import (
    ParserAdapterSourceRoute,
    build_parser_adapter_scorecard,
    build_parser_adapter_source_routes,
)
from app.rag.preprocess_strategy import (
    normalize_preprocess_profile,
    preprocess_runtime_settings,
)
from app.rag.section_map_service import schedule_refresh_all_section_maps
from app.rag.section_rules import (
    SectionRulesSettingsData,
    SectionRulesStore,
    reset_section_rules,
    save_section_rules,
    section_rules_settings,
)
from app.rag.system_schema import SystemSchemaError, system_schema_manager
from app.rag.system_schema_runtime import system_schema_runtime
from app.rag.vector_index_adapter import (
    normalize_vector_index_profile,
    vector_index_adapter_runtime_settings,
)
from app.schemas.common import ApiResponse
from app.schemas.evaluation import EvaluationThresholds
from app.schemas.settings import (
    POST_PARSE_SETTING_FIELDS,
    AnsweringSettingsData,
    AnsweringSettingsUpdate,
    AnswerPromptsData,
    AnswerPromptUpdate,
    AnswerPromptView,
    AnswerRecordSettingsData,
    AnswerRecordSettingsUpdate,
    ChunkingSettingsData,
    ChunkingSettingsUpdate,
    ChunkingStrategyStatusData,
    EntityIndexSettingsData,
    EntityIndexSettingsUpdate,
    EvaluationSettingsData,
    EvaluationSettingsUpdate,
    EvaluationSuiteStatusData,
    ExtractionFieldsSettingsData,
    ExtractionFieldsSettingsUpdate,
    FieldDefinitionData,
    GraphProfileStatusData,
    GraphSettingsData,
    GraphSettingsUpdate,
    GuardrailSettingsData,
    GuardrailSettingsUpdate,
    HuggingFaceSettingsData,
    HuggingFaceSettingsUpdate,
    ModelSettingsTestRequest,
    OciConfigField,
    ParserAdapterBackendSourceMatrixData,
    ParserAdapterScorecardData,
    ParserAdapterScorecardEntryData,
    ParserAdapterSettingsData,
    ParserAdapterSettingsUpdate,
    ParserAdapterSourceRouteData,
    ParserAdapterStatusData,
    ParserBackendCapabilityData,
    PipelineSettingsData,
    PipelineSettingsUpdate,
    PreprocessProfileStatusData,
    PreprocessSettingsData,
    PreprocessSettingsUpdate,
    SystemTablesDeleteOrphansRequest,
    SystemTablesInitializeRequest,
    SystemTablesOperationData,
    SystemTablesOrphanDeletionData,
    SystemTablesStatusData,
    VectorIndexProfileStatusData,
    VectorIndexSettingsData,
    VectorIndexSettingsUpdate,
)
from app.security.permissions import ANSWER_PROMPT_EDIT_PERMISSIONS
from app.security.service import get_security_service

router = APIRouter()
logger = logging.getLogger(__name__)
OCI_DIRECTORY_MODE = 0o700
# RAG 固有の設定（`RAG_*`）の保存先。3製品共通の設定（`PLATFORM_*`）は共通 `.env`
# （app.config.PLATFORM_ENV_FILE）へ保存する（#211）。
BACKEND_ENV_FILE = Path(__file__).resolve().parents[3] / ".env"

# アップロード保存先は3製品共通の実装（platform の pr_system_settings。#97）。
# テストで get_settings / PLATFORM_ENV_FILE を差し替えられるよう、呼出時に module の値を参照する。
router.include_router(
    build_upload_storage_router(
        get_settings=lambda: get_settings(),
        env_file=lambda: app_config.PLATFORM_ENV_FILE,
    )
)
# OCI 認証も3製品共通の実装（pr_system_settings.oci。#100）。
router.include_router(
    build_oci_router(
        get_settings=lambda: get_settings(),
        env_file=lambda: app_config.PLATFORM_ENV_FILE,
    )
)
# モデル設定も3製品共通の実装（pr_system_settings.model。#103）。
router.include_router(
    build_model_router(
        get_settings=lambda: get_settings(),
        store=MODEL_SETTINGS_STORE,
        run_model_test=lambda settings, request: _run_model_settings_test(settings, request),
    )
)
# データベース設定も3製品共通の実装（pr_system_settings.database。#108）。
# 接続そのものと接続 pool の後始末は RAG のものを渡す。Walletless TLS と
# パスワードの表示は、RAG の接続処理と権限が対応していないため有効にしない。
router.include_router(
    build_database_router(
        get_settings=lambda: get_settings(),
        env_file=lambda: app_config.PLATFORM_ENV_FILE,
        test_connection=lambda candidate: test_oracle_connection(candidate),
        on_saved=lambda _settings: close_oracle_pool(),
    )
)
# 回答の注意書きも3製品共通の実装（pr_system_settings.answer_notice。#2405）。
# 保存先は共通の表のこの製品の行（#2449）。
router.include_router(
    build_answer_notice_router(
        load=lambda: get_security_service().load_answer_notice(),
        save=lambda record: get_security_service().save_answer_notice(record),
    )
)
router.include_router(
    build_trace_recording_router(provider=lambda: get_security_service().trace_recording),
)
# チャットの添付を使うか（運用設定 › チャットの添付。設定は製品ごと。既定は無効。#2517）。
router.include_router(
    build_chat_attachment_settings_router(
        load=lambda: get_security_service().chat_attachment_settings.load(),
        save=lambda record: get_security_service().chat_attachment_settings.save(record),
        get_settings=lambda: get_settings(),
    )
)
ENV_FILE_MODE = 0o600
ENV_ASSIGNMENT_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
OCI_CONFIG_KEYS: tuple[OciConfigField, ...] = (
    "user",
    "fingerprint",
    "tenancy",
    "region",
    "key_file",
)


@router.get(
    "/database/system-tables",
    response_model=ApiResponse[SystemTablesStatusData],
)
async def get_system_tables_status() -> ApiResponse[SystemTablesStatusData]:
    """RAG system table の状態を DDL なしで取得する。"""

    try:
        data = await asyncio.to_thread(system_schema_manager.status)
    except Exception as exc:
        raise system_tables_status_error(exc) from exc
    return ApiResponse(data=SystemTablesStatusData.model_validate(data))


@router.post(
    "/database/system-tables/initialize",
    response_model=ApiResponse[SystemTablesOperationData],
)
async def initialize_system_tables(
    payload: SystemTablesInitializeRequest,
) -> ApiResponse[SystemTablesOperationData] | JSONResponse:
    """管理者の明示操作として作成・更新または全再作成する。"""

    # 権限（rag.system_tables.manage）は API の権限 manifest で確認する（#214）。
    try:
        data = await asyncio.to_thread(
            system_schema_manager.initialize,
            recreate=payload.recreate,
            confirmation=payload.confirmation,
            # データを消す未適用の migration の承認（画面の確認ダイアログ。無ければ 409。#619）。
            allow_destructive=payload.allow_destructive,
        )
    except SystemSchemaError as exc:
        return _system_schema_error_response(exc)
    system_schema_runtime.invalidate()
    return ApiResponse(data=SystemTablesOperationData.model_validate(data))


@router.post(
    "/database/system-tables/orphaned-rows/delete",
    response_model=ApiResponse[SystemTablesOrphanDeletionData],
)
async def delete_system_table_orphaned_rows(
    payload: SystemTablesDeleteOrphansRequest,
) -> ApiResponse[SystemTablesOrphanDeletionData] | JSONResponse:
    """管理者の明示操作として、外部キーの参照先のない行を削除して検査済みにする（#511）。

    利用者が確認した件数（`expected_orphan_rows`）より増えていたら削除しない。
    """

    # 権限は作成・更新と同じ rag.system_tables.manage（API の権限 manifest で確認する）。
    try:
        data = await asyncio.to_thread(
            system_schema_manager.delete_orphaned_rows,
            constraint_name=payload.constraint_name,
            expected_orphan_rows=payload.expected_orphan_rows,
        )
    except SystemSchemaError as exc:
        return _system_schema_error_response(exc)
    system_schema_runtime.invalidate()
    return ApiResponse(data=SystemTablesOrphanDeletionData.model_validate(data))


def _system_schema_error_response(exc: SystemSchemaError) -> JSONResponse:
    """システムテーブル操作の業務エラー（公開してよい文言と ORA コードだけ）。"""

    return JSONResponse(
        status_code=exc.status_code,
        headers=exc.retry_headers,
        content={
            "data": None,
            "error_messages": [exc.public_message],
            "warning_messages": [],
            "error_code": exc.code,
        },
    )


@router.get("/huggingface", response_model=ApiResponse[HuggingFaceSettingsData])
async def get_huggingface_settings() -> ApiResponse[HuggingFaceSettingsData]:
    """HuggingFace モデルダウンロード設定を返す。token 実値は返さない。"""
    return ApiResponse(data=_huggingface_settings_data(get_settings()))


@router.patch("/huggingface", response_model=ApiResponse[HuggingFaceSettingsData])
async def update_huggingface_settings(
    payload: HuggingFaceSettingsUpdate,
) -> ApiResponse[HuggingFaceSettingsData]:
    """HuggingFace 設定を backend/.env と現在プロセスへ反映する。"""
    settings = get_settings()
    candidate = _huggingface_settings_candidate(settings, payload)
    _persist_huggingface_settings(candidate)
    _apply_huggingface_settings(settings, candidate)
    return ApiResponse(data=_huggingface_settings_data(settings))


@router.get("/parser-adapters", response_model=ApiResponse[ParserAdapterSettingsData])
async def get_parser_adapter_settings() -> ApiResponse[ParserAdapterSettingsData]:
    """任意 parser adapter の feature flag と package readiness を返す。"""
    return ApiResponse(data=_parser_adapter_settings_data(get_settings()))


@router.patch("/parser-adapters", response_model=ApiResponse[ParserAdapterSettingsData])
async def update_parser_adapter_settings(
    payload: ParserAdapterSettingsUpdate,
) -> ApiResponse[ParserAdapterSettingsData]:
    """任意 parser adapter の backend/feature flag と「解析後の処理」を保存し runtime へ反映する。

    - 解析エンジン（`adapter_backend` と feature flag）は parser-settings.json（#1701）。
    - 「解析後の処理」（Vision・項目抽出・章節木。#528）は backend/.env。ほかのプロセスは
      `.env` の更新時刻の変化で読み直す（#466）。
    """
    settings = get_settings()
    post_parse_updates = _post_parse_settings_updates(payload)
    parser_fields = payload.model_fields_set - set(POST_PARSE_SETTING_FIELDS)
    if post_parse_updates:
        # 先に .env を書く。失敗したら解析エンジンの設定も変えない（500）。
        _persist_post_parse_settings(settings.model_copy(update=post_parse_updates))
        for name, value in post_parse_updates.items():
            setattr(settings, name, value)
    if parser_fields:
        # 解析エンジンは RAG 専用の parser-settings.json に保存する（共有の model-settings.json には
        # 書かない。#1701）。
        try:
            with PARSER_SETTINGS_STORE.lock(settings):
                PARSER_SETTINGS_STORE.reload_if_changed(settings)
                candidate = _parser_adapter_settings_candidate(settings, payload)
                PARSER_SETTINGS_STORE.save(candidate)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail="文書解析の設定を保存できませんでした。",
            ) from exc
        PARSER_SETTINGS_STORE.load(settings)
    return ApiResponse(data=_parser_adapter_settings_data(settings))


@router.get("/pipeline", response_model=ApiResponse[PipelineSettingsData])
async def get_pipeline_settings() -> ApiResponse[PipelineSettingsData]:
    """設定の概要: 工程の自動進行と、レシピ 11 項目の全体の既定を返す（#528）。"""
    return ApiResponse(data=_pipeline_settings_data(get_settings()))


@router.patch("/pipeline", response_model=ApiResponse[PipelineSettingsData])
async def update_pipeline_settings(
    payload: PipelineSettingsUpdate,
) -> ApiResponse[PipelineSettingsData]:
    """工程の自動進行を backend/.env と現在プロセスへ反映する（#528）。"""
    settings = get_settings()
    updates = {
        f"rag_{name}": value
        for name, value in payload.model_dump(exclude_unset=True).items()
        if value is not None
    }
    if updates:
        _persist_pipeline_settings(settings.model_copy(update=updates))
        for name, value in updates.items():
            setattr(settings, name, value)
    return ApiResponse(data=_pipeline_settings_data(settings))


@router.get("/preprocess", response_model=ApiResponse[PreprocessSettingsData])
async def get_preprocess_settings() -> ApiResponse[PreprocessSettingsData]:
    """前処理(Preprocess)アダプター(parse 前の原本変換)の選択と利用可否を返す。"""
    return ApiResponse(data=_preprocess_settings_data(get_settings()))


@router.patch("/preprocess", response_model=ApiResponse[PreprocessSettingsData])
async def update_preprocess_settings(
    payload: PreprocessSettingsUpdate,
) -> ApiResponse[PreprocessSettingsData]:
    """ファイル準備設定を backend/.env と現在プロセスへ反映する。"""
    settings = get_settings()
    candidate = _preprocess_settings_candidate(settings, payload)
    _persist_preprocess_settings(candidate)
    _apply_preprocess_settings(settings, candidate)
    return ApiResponse(data=_preprocess_settings_data(settings))


@router.get("/chunking", response_model=ApiResponse[ChunkingSettingsData])
async def get_chunking_settings() -> ApiResponse[ChunkingSettingsData]:
    """Chunking アダプター(分割戦略)の選択と多様化パラメータを返す。"""
    return ApiResponse(data=_chunking_settings_data(get_settings()))


@router.patch("/chunking", response_model=ApiResponse[ChunkingSettingsData])
async def update_chunking_settings(
    payload: ChunkingSettingsUpdate,
) -> ApiResponse[ChunkingSettingsData]:
    """文書分割設定を backend/.env と現在プロセスへ反映する。"""
    settings = get_settings()
    candidate = _chunking_settings_candidate(settings, payload)
    _persist_chunking_settings(candidate)
    _apply_chunking_settings(settings, candidate)
    return ApiResponse(data=_chunking_settings_data(settings))


@router.get("/answering", response_model=ApiResponse[AnsweringSettingsData])
async def get_answering_settings() -> ApiResponse[AnsweringSettingsData]:
    """回答の検索と生成の全体既定(質問の拡張・回答の生成方式など。#593)を返す。"""
    return ApiResponse(data=_answering_settings_data(get_settings()))


@router.patch("/answering", response_model=ApiResponse[AnsweringSettingsData])
async def update_answering_settings(
    payload: AnsweringSettingsUpdate,
) -> ApiResponse[AnsweringSettingsData]:
    """回答の検索と生成の全体既定を backend/.env と現在プロセスへ反映する(#593)。

    送った項目だけを変える。この画面の項目は全体設定だけで決め、検索・回答プロファイルでは上書きしない。
    """
    settings = get_settings()
    updates = {
        field: value
        for field, value in (
            ("rag_query_strategy", payload.query_strategy),
            ("rag_answer_flow", payload.answer_flow),
            ("rag_answer_audit_mode", payload.answer_audit_mode),
            ("rag_answer_evidence_refinement_mode", payload.answer_evidence_refinement_mode),
            ("rag_neighbor_child_count", payload.neighbor_child_count),
            ("rag_rerank_enabled", payload.rerank_enabled),
            ("rag_screen_linking_enabled", payload.screen_linking_enabled),
            (
                "rag_request_coverage_retrieval_enabled",
                payload.request_coverage_retrieval_enabled,
            ),
            ("rag_auto_field_filter_enabled", payload.auto_field_filter_enabled),
            ("rag_graph_expansion_enabled", payload.graph_expansion_enabled),
            ("rag_entity_expansion_enabled", payload.entity_expansion_enabled),
            ("rag_entity_expansion_max_chunks", payload.entity_expansion_max_chunks),
            ("rag_section_expansion_enabled", payload.section_expansion_enabled),
            ("rag_section_expansion_max_levels", payload.section_expansion_max_levels),
            (
                "rag_section_expansion_input_token_percent",
                payload.section_expansion_input_token_percent,
            ),
        )
        if value is not None
    }
    if updates:
        candidate = settings.model_copy(update=updates)
        _write_env_values(
            BACKEND_ENV_FILE,
            {
                "RAG_ENTITY_EXPANSION_ENABLED": _format_env_bool(
                    candidate.rag_entity_expansion_enabled
                ),
                "RAG_ENTITY_EXPANSION_MAX_CHUNKS": str(candidate.rag_entity_expansion_max_chunks),
                "RAG_SECTION_EXPANSION_ENABLED": _format_env_bool(
                    candidate.rag_section_expansion_enabled
                ),
                "RAG_SECTION_EXPANSION_MAX_LEVELS": str(candidate.rag_section_expansion_max_levels),
                "RAG_SECTION_EXPANSION_INPUT_TOKEN_PERCENT": str(
                    candidate.rag_section_expansion_input_token_percent
                ),
                "RAG_QUERY_STRATEGY": candidate.rag_query_strategy,
                "RAG_ANSWER_FLOW": candidate.rag_answer_flow,
                "RAG_ANSWER_AUDIT_MODE": candidate.rag_answer_audit_mode,
                "RAG_ANSWER_EVIDENCE_REFINEMENT_MODE": (
                    candidate.rag_answer_evidence_refinement_mode
                ),
                "RAG_NEIGHBOR_CHILD_COUNT": str(candidate.rag_neighbor_child_count),
                "RAG_RERANK_ENABLED": _format_env_bool(candidate.rag_rerank_enabled),
                "RAG_SCREEN_LINKING_ENABLED": _format_env_bool(
                    candidate.rag_screen_linking_enabled
                ),
                "RAG_REQUEST_COVERAGE_RETRIEVAL_ENABLED": _format_env_bool(
                    candidate.rag_request_coverage_retrieval_enabled
                ),
                "RAG_AUTO_FIELD_FILTER_ENABLED": _format_env_bool(
                    candidate.rag_auto_field_filter_enabled
                ),
                "RAG_GRAPH_EXPANSION_ENABLED": _format_env_bool(
                    candidate.rag_graph_expansion_enabled
                ),
            },
            section_comment="# 回答の検索と生成",
            error_detail="回答の検索と生成の設定を backend/.env へ保存できませんでした。",
        )
        for field, value in updates.items():
            setattr(settings, field, value)
    return ApiResponse(data=_answering_settings_data(settings))


def _answering_settings_data(settings: Settings) -> AnsweringSettingsData:
    return AnsweringSettingsData(
        query_strategy=settings.rag_query_strategy,
        answer_flow=settings.rag_answer_flow,
        answer_audit_mode=settings.rag_answer_audit_mode,
        answer_evidence_refinement_mode=settings.rag_answer_evidence_refinement_mode,
        neighbor_child_count=settings.rag_neighbor_child_count,
        rerank_enabled=settings.rag_rerank_enabled,
        screen_linking_enabled=settings.rag_screen_linking_enabled,
        request_coverage_retrieval_enabled=settings.rag_request_coverage_retrieval_enabled,
        auto_field_filter_enabled=settings.rag_auto_field_filter_enabled,
        graph_expansion_enabled=settings.rag_graph_expansion_enabled,
        entity_expansion_enabled=settings.rag_entity_expansion_enabled,
        entity_expansion_max_chunks=settings.rag_entity_expansion_max_chunks,
        section_expansion_enabled=settings.rag_section_expansion_enabled,
        section_expansion_max_levels=settings.rag_section_expansion_max_levels,
        section_expansion_input_token_percent=settings.rag_section_expansion_input_token_percent,
    )


@router.get("/answer-records", response_model=ApiResponse[AnswerRecordSettingsData])
async def get_answer_record_settings() -> ApiResponse[AnswerRecordSettingsData]:
    """回答の記録の保持日数を返す。"""
    return ApiResponse(
        data=AnswerRecordSettingsData(
            retention_days=get_settings().rag_answer_record_retention_days
        )
    )


@router.patch("/answer-records", response_model=ApiResponse[AnswerRecordSettingsData])
async def update_answer_record_settings(
    payload: AnswerRecordSettingsUpdate,
) -> ApiResponse[AnswerRecordSettingsData]:
    """保持日数を backend/.env と現在プロセスへ反映し、期限切れの記録を削除する。"""
    settings = get_settings()
    _write_env_values(
        BACKEND_ENV_FILE,
        {"RAG_ANSWER_RECORD_RETENTION_DAYS": str(payload.retention_days)},
        section_comment="# 回答の記録",
        error_detail="回答記録の保持設定を backend/.env へ保存できませんでした。",
    )
    settings.rag_answer_record_retention_days = payload.retention_days
    if payload.retention_days > 0:
        try:
            oracle = OracleClient()
            await oracle.purge_answer_records(payload.retention_days)
            # 実行履歴（#1890）も回答の記録と同じ保持日数で消す。
            await RunHistoryStore(oracle).purge_runs(payload.retention_days)
        except Exception as exc:  # 次の回答保存時にも削除するため、設定保存は止めない。
            logger.warning("answer record purge failed", extra={**safe_exception_fields(exc)})
    return ApiResponse(data=AnswerRecordSettingsData(retention_days=payload.retention_days))


@router.get("/answer-prompts", response_model=ApiResponse[AnswerPromptsData])
async def get_answer_prompts() -> ApiResponse[AnswerPromptsData]:
    """編集できるプロンプト(回答生成・図の読み取り)と、回答フローの各段の読み取り専用プロンプトを返す。"""
    saved = await OracleClient().list_answer_prompts()
    return ApiResponse(data=_answer_prompts_data(saved))


@router.put("/answer-prompts/{key}", response_model=ApiResponse[AnswerPromptsData])
async def put_answer_prompt(
    key: str, payload: AnswerPromptUpdate, request: Request
) -> ApiResponse[AnswerPromptsData]:
    """編集できるプロンプトを保存する(次の回答・次の解析から使う)。"""
    _require_answer_prompt_permission(request, key)
    try:
        validate_answer_prompt(key, payload.content)
        if key == "image_retrieval":
            from rag_engine.parsing.vision_prompt_rules import split_image_retrieval_prompt

            validate_answer_prompt(key, split_image_retrieval_prompt(payload.content)[0])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="プロンプトが見つかりません。") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    oracle = OracleClient()
    if key == "image_retrieval":
        from rag_engine.parsing.vision_prompt_rules import split_image_retrieval_prompt

        previous = await oracle.list_answer_prompts()
        body, supplied_rules = split_image_retrieval_prompt(payload.content)
        legacy = split_image_retrieval_prompt(str(previous.get(key, {}).get("content", "")))[1]
        if supplied_rules is not None:
            await oracle.save_answer_prompt("picture_description.rules", supplied_rules)
        elif legacy is not None and "picture_description.rules" not in previous:
            await oracle.save_answer_prompt("picture_description.rules", legacy)
        await oracle.save_answer_prompt(key, body)
    else:
        await oracle.save_answer_prompt(key, payload.content)
    return ApiResponse(data=_answer_prompts_data(await oracle.list_answer_prompts()))


@router.delete("/answer-prompts/{key}", response_model=ApiResponse[AnswerPromptsData])
async def reset_answer_prompt(key: str, request: Request) -> ApiResponse[AnswerPromptsData]:
    """保存したプロンプトを消して既定値へ戻す。"""
    _require_answer_prompt_permission(request, key)
    if key not in EDITABLE_PROMPT_KEYS:
        raise HTTPException(status_code=404, detail="プロンプトが見つかりません。")
    oracle = OracleClient()
    if key in {"image_retrieval", "picture_description.rules"}:
        from rag_engine.parsing.vision_prompt_rules import split_image_retrieval_prompt

        previous = await oracle.list_answer_prompts()
        image = previous.get("image_retrieval")
        if image:
            body, legacy = split_image_retrieval_prompt(str(image["content"]))
            if legacy is not None:
                if key == "image_retrieval" and "picture_description.rules" not in previous:
                    await oracle.save_answer_prompt("picture_description.rules", legacy)
                if key == "picture_description.rules":
                    await oracle.save_answer_prompt("image_retrieval", body)
    await oracle.delete_answer_prompt(key)
    return ApiResponse(data=_answer_prompts_data(await oracle.list_answer_prompts()))


def _require_answer_prompt_permission(request: Request, key: str) -> None:
    """プロンプトを編集する画面の権限を確かめる(#1010)。

    route の manifest は回答プロンプト・文書解析のどちらかの権限で通すので、ここで key ごとに
    その画面の権限を求める。知らない key はこの後の 404 に任せる。
    """
    permission = ANSWER_PROMPT_EDIT_PERMISSIONS.get(key) or stage_prompt_permission(key)
    if permission is None:
        return
    principal = getattr(request.state, "principal", None)
    if principal is None or not principal.has_permission(permission):
        raise SecurityApiError(
            403, "この機能を利用する権限がありません。", code=ROUTE_FORBIDDEN_CODE
        )


def _answer_prompts_data(saved: dict[str, dict[str, object]]) -> AnswerPromptsData:
    from app.rag.answer_prompts import separate_vision_prompt_rows

    saved = separate_vision_prompt_rows(saved)
    prompts = []
    for key in EDITABLE_PROMPT_KEYS:
        row = saved.get(key)
        prompts.append(
            AnswerPromptView.model_validate(
                {
                    "key": key,
                    "content": row["content"] if row else default_answer_prompt(key),
                    "default_content": default_answer_prompt(key),
                    "customized": row is not None,
                    "required_placeholders": list(answer_prompt_required_placeholders(key)),
                    "required_tokens": list(required_tokens(key)),
                    "updated_at": row["updated_at"] if row else None,
                }
            )
        )
    return AnswerPromptsData.model_validate(
        {
            "prompts": prompts,
            "stages": readonly_prompt_stages(
                overrides={prompt.key: prompt.content for prompt in prompts},
                image_prompt=next(
                    prompt.content for prompt in prompts if prompt.key == "image_retrieval"
                ),
            ),
        }
    )


def _extraction_fields_data() -> ExtractionFieldsSettingsData:
    """field schema 定義を非機密 API 形へ変換する。未保存なら標準の項目(#556)。"""
    saved = load_saved_field_schema()
    fields = standard_field_definitions() if saved is None else saved.fields
    return ExtractionFieldsSettingsData(
        fields=[
            FieldDefinitionData(
                name=field.name,
                description=field.description,
                value_type=field.value_type,
            )
            for field in fields
        ],
        uses_standard=saved is None,
    )


@router.get("/extraction-fields", response_model=ApiResponse[ExtractionFieldsSettingsData])
async def get_extraction_fields_settings() -> ApiResponse[ExtractionFieldsSettingsData]:
    """field 抽出 schema 定義(抽出対象 field の一覧)を返す。"""
    return ApiResponse(data=_extraction_fields_data())


@router.patch("/extraction-fields", response_model=ApiResponse[ExtractionFieldsSettingsData])
async def update_extraction_fields_settings(
    payload: ExtractionFieldsSettingsUpdate,
) -> ApiResponse[ExtractionFieldsSettingsData]:
    """field 抽出 schema 定義を保存する（文書解析の「解析後の処理」で編集する。#528）。

    name の重複（大文字小文字を区別しない）は 422。保存先は extraction-fields.json で、
    取込は毎回ファイルから読むため、ほかのプロセスにも次の取込から効く。
    """
    definitions = [
        FieldDefinition(name=field.name, description=field.description, value_type=field.value_type)
        for field in payload.fields
    ]
    try:
        save_field_schema(definitions)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail="抽出項目の定義を保存できませんでした。"
        ) from exc
    return ApiResponse(data=_extraction_fields_data())


@router.delete("/extraction-fields", response_model=ApiResponse[ExtractionFieldsSettingsData])
async def reset_extraction_fields_settings() -> ApiResponse[ExtractionFieldsSettingsData]:
    """保存した全体の既定を消し、標準の項目に戻す(「標準の項目に戻す」。#556)。

    extraction-fields.json を消して「未設定」に戻すため、標準の項目を後の版で見直したときも
    その版の標準の項目を使う。保存していないときも成功する(何度呼んでも同じ結果)。
    """
    try:
        reset_field_schema()
    except OSError as exc:
        raise HTTPException(status_code=500, detail="標準の項目に戻せませんでした。") from exc
    return ApiResponse(data=_extraction_fields_data())


@router.get("/section-rules", response_model=ApiResponse[SectionRulesSettingsData])
async def get_section_rules_settings() -> ApiResponse[SectionRulesSettingsData]:
    """章節の抽出規則の全体の既定(方式・独自の規則)とプリセットの中身(#715)。"""
    return ApiResponse(data=section_rules_settings())


@router.patch("/section-rules", response_model=ApiResponse[SectionRulesSettingsData])
async def update_section_rules_settings(
    payload: SectionRulesStore,
) -> ApiResponse[SectionRulesSettingsData]:
    """章節の抽出規則の全体の既定を保存する(文書解析の「解析後の処理」で編集する。#715)。

    正規表現として読めない規則は 422。保存先は section-rules.json で、章節を取得するたびに読む
    ため、取込をやり直さずに次の表示から効く。
    """
    try:
        store = save_section_rules(payload)
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail="章節の抽出規則を保存できませんでした。"
        ) from exc
    # 章節が変わる文書の章節と chunk の対応を作り直す（#1438。chunk・embedding は変えない）。
    schedule_refresh_all_section_maps(reason="section_rules_saved")
    return ApiResponse(data=section_rules_settings(store))


@router.delete("/section-rules", response_model=ApiResponse[SectionRulesSettingsData])
async def reset_section_rules_settings() -> ApiResponse[SectionRulesSettingsData]:
    """保存した全体の既定を消し、「解析エンジンの見出し」に戻す(#715)。"""
    try:
        reset_section_rules()
    except OSError as exc:
        raise HTTPException(status_code=500, detail="章節の抽出規則を戻せませんでした。") from exc
    schedule_refresh_all_section_maps(reason="section_rules_reset")
    return ApiResponse(data=section_rules_settings())


@router.get("/guardrail", response_model=ApiResponse[GuardrailSettingsData])
async def get_guardrail_settings() -> ApiResponse[GuardrailSettingsData]:
    """OCI Guardrails の有効状態と準備状況を返す。"""
    return ApiResponse(data=_guardrail_settings_data(get_settings()))


@router.patch("/guardrail", response_model=ApiResponse[GuardrailSettingsData])
async def update_guardrail_settings(
    payload: GuardrailSettingsUpdate,
) -> ApiResponse[GuardrailSettingsData]:
    """安全チェック設定を backend/.env と現在プロセスへ反映する。"""
    settings = get_settings()
    candidate = settings.model_copy(
        update={
            "rag_guardrails_input_enabled": payload.input_enabled,
            "rag_guardrails_output_enabled": payload.output_enabled,
        }
    )
    if (
        candidate.rag_guardrails_input_enabled or candidate.rag_guardrails_output_enabled
    ) and _oci_guardrails_readiness_code(candidate):
        raise HTTPException(
            status_code=422,
            detail="OCI Guardrails を有効にする前に、OCI 認証と compartment を設定してください。",
        )
    _persist_guardrail_settings(candidate)
    settings.rag_guardrails_input_enabled = candidate.rag_guardrails_input_enabled
    settings.rag_guardrails_output_enabled = candidate.rag_guardrails_output_enabled
    return ApiResponse(data=_guardrail_settings_data(settings))


@router.get("/vector-index", response_model=ApiResponse[VectorIndexSettingsData])
async def get_vector_index_settings() -> ApiResponse[VectorIndexSettingsData]:
    """Vector Index アダプター(索引/検索精度)の選択と解決内容を返す。"""
    return ApiResponse(data=await _vector_index_settings_data(get_settings()))


@router.patch("/vector-index", response_model=ApiResponse[VectorIndexSettingsData])
async def update_vector_index_settings(
    payload: VectorIndexSettingsUpdate,
) -> ApiResponse[VectorIndexSettingsData]:
    """検索インデックス設定を backend/.env と現在プロセスへ反映する。"""
    settings = get_settings()
    candidate = settings.model_copy(
        update={"rag_vector_index_profile": normalize_vector_index_profile(payload.profile)}
    )
    _persist_vector_index_settings(candidate)
    settings.rag_vector_index_profile = candidate.rag_vector_index_profile
    return ApiResponse(data=await _vector_index_settings_data(settings))


@router.get("/evaluation-suite", response_model=ApiResponse[EvaluationSettingsData])
async def get_evaluation_settings() -> ApiResponse[EvaluationSettingsData]:
    """Evaluation アダプター(評価スイート/閾値)の選択と解決内容を返す。"""
    return ApiResponse(data=_evaluation_settings_data(get_settings()))


@router.patch("/evaluation-suite", response_model=ApiResponse[EvaluationSettingsData])
async def update_evaluation_settings(
    payload: EvaluationSettingsUpdate,
) -> ApiResponse[EvaluationSettingsData]:
    """品質評価設定を backend/.env と現在プロセスへ反映する。"""
    settings = get_settings()
    candidate = settings.model_copy(
        update={"rag_evaluation_suite": normalize_evaluation_suite(payload.suite)}
    )
    _persist_evaluation_settings(candidate)
    settings.rag_evaluation_suite = candidate.rag_evaluation_suite
    return ApiResponse(data=_evaluation_settings_data(settings))


@router.get("/entity-index", response_model=ApiResponse[EntityIndexSettingsData])
async def get_entity_index_settings() -> ApiResponse[EntityIndexSettingsData]:
    """実体・別名索引を構築する全体の既定を返す。"""
    return ApiResponse(
        data=EntityIndexSettingsData(enabled=get_settings().rag_entity_index_enabled)
    )


@router.patch("/entity-index", response_model=ApiResponse[EntityIndexSettingsData])
async def update_entity_index_settings(
    payload: EntityIndexSettingsUpdate,
) -> ApiResponse[EntityIndexSettingsData]:
    """保存に成功した場合だけ現在プロセスの設定も更新する。"""
    settings = get_settings()
    _write_env_values(
        BACKEND_ENV_FILE,
        {"RAG_ENTITY_INDEX_ENABLED": str(payload.enabled).lower()},
        section_comment="# 実体・別名索引",
        error_detail="実体・別名索引の設定を保存できませんでした。",
    )
    settings.rag_entity_index_enabled = payload.enabled
    return ApiResponse(data=EntityIndexSettingsData(enabled=payload.enabled))


@router.get("/graph", response_model=ApiResponse[GraphSettingsData])
async def get_graph_settings() -> ApiResponse[GraphSettingsData]:
    """文書構造索引の構築の選択(構築する / しない)を返す。"""
    return ApiResponse(data=_graph_settings_data(get_settings()))


@router.patch("/graph", response_model=ApiResponse[GraphSettingsData])
async def update_graph_settings(
    payload: GraphSettingsUpdate,
) -> ApiResponse[GraphSettingsData]:
    """文書構造索引の設定を backend/.env と現在プロセスへ反映する。"""
    settings = get_settings()
    candidate = settings.model_copy(
        update={"rag_graph_profile": normalize_graph_profile(payload.profile)}
    )
    _persist_graph_settings(candidate)
    settings.rag_graph_profile = candidate.rag_graph_profile
    return ApiResponse(data=_graph_settings_data(settings))


async def _run_model_settings_test(
    settings: Settings,
    request: ModelSettingsTestRequest,
) -> dict[str, str | int | float | bool | None]:
    """対象モデルの実 API 呼び出しを行い、表示用 details を返す（共有 router の hook）。"""
    if request.target_type == "enterprise_text":
        text = await async_call_with_model_purpose(
            "connection.check",
            OciEnterpriseAiClient(settings=settings).generate,
            MODEL_TEST_TEXT_PROMPT,
            model_test_text_context("Production Ready RAG"),
        )
        return {"response_chars": len(text), "surface": "llm"}
    if request.target_type == "enterprise_vision":
        text = await async_call_with_model_purpose(
            "connection.check",
            OciEnterpriseAiClient(settings=settings).generate_from_image,
            MODEL_TEST_IMAGE_BYTES,
            MODEL_TEST_VISION_PROMPT,
            mime_type=MODEL_TEST_IMAGE_MIME_TYPE,
        )
        return {
            "surface": "vision",
            "response_chars": len(text),
        }
    if request.target_type == "embedding":
        vectors = await async_call_with_model_purpose(
            "connection.check",
            OciGenAiClient(settings=settings).embed,
            [MODEL_TEST_EMBEDDING_INPUT],
            input_type="SEARCH_QUERY",
        )
        vector = vectors[0] if vectors else []
        return {"vector_dim": len(vector), "input_count": len(vectors)}
    ranks = await async_call_with_model_purpose(
        "connection.check",
        OciGenAiClient(settings=settings).rerank,
        MODEL_TEST_RERANK_QUERY,
        list(MODEL_TEST_RERANK_DOCUMENTS),
        top_n=1,
    )
    top_score = ranks[0][1] if ranks else None
    return {"ranked_count": len(ranks), "top_score": top_score}


def _huggingface_settings_data(settings: Settings) -> HuggingFaceSettingsData:
    """Settings から HuggingFace 設定の表示用データを作る(token 実値は返さない)。"""
    return HuggingFaceSettingsData(
        endpoint=settings.huggingface_endpoint,
        token_configured=bool(settings.huggingface_token.strip()),
        config_source="runtime",
    )


def _huggingface_settings_candidate(
    base: Settings,
    payload: HuggingFaceSettingsUpdate,
) -> Settings:
    """更新 payload を適用した一時 Settings を作る。

    `model_copy` は validator を実行しないため、起動時の読み込みと同じ正規化（scheme の補完）を
    ここで行う。正規化しないと、backend を再起動するまで scheme なしの endpoint が実行中の設定と
    サービスの `HF_ENDPOINT` に渡り、モデルの DL が失敗する（#1018）。token は前後の空白を除き、
    空白だけなら未入力（保存済みの値を保持）とする。
    """
    updates = {
        "huggingface_endpoint": Settings.normalize_huggingface_endpoint(payload.endpoint),
        "huggingface_token": _secret_value(
            current=base.huggingface_token,
            update=None if payload.token is None else payload.token.strip(),
            clear=payload.clear_token,
        ),
    }
    return base.model_copy(update=updates)


def _apply_huggingface_settings(target: Settings, source: Settings) -> None:
    """HuggingFace 関連設定だけ現在プロセスへ反映する。"""
    target.huggingface_endpoint = source.huggingface_endpoint
    target.huggingface_token = source.huggingface_token


def _persist_huggingface_settings(settings: Settings) -> None:
    """HuggingFace 設定を backend/.env へ永続化する。

    parser の huggingface_hub へは、サービス管理が起動/再起動の前に HF_TOKEN / HF_ENDPOINT として
    サービス実行用の env ファイルへ書いて渡す(#286)。
    """
    _write_env_values(
        BACKEND_ENV_FILE,
        {
            "RAG_HUGGINGFACE_TOKEN": settings.huggingface_token,
            "RAG_HUGGINGFACE_ENDPOINT": settings.huggingface_endpoint,
        },
        section_comment="# HuggingFace モデルダウンロード",
        error_detail="HuggingFace 設定を backend/.env へ保存できませんでした。",
    )


def _parser_adapter_settings_data(settings: Settings) -> ParserAdapterSettingsData:
    """Settings から parser adapter readiness の表示用データを作る。"""
    runtime = parser_adapter_runtime_settings(settings)
    scorecard = build_parser_adapter_scorecard(runtime)
    source_routes = build_parser_adapter_source_routes(runtime)
    route_data = [_parser_adapter_source_route_data(route) for route in source_routes]
    return ParserAdapterSettingsData(
        adapter_backend=runtime.adapter_backend,
        effective_order=list(runtime.effective_order),
        adapters=[
            ParserAdapterStatusData(
                backend=adapter.backend,
                package_name=adapter.package_name,
                import_name=adapter.import_name,
                distribution_name=adapter.distribution_name,
                install_package=adapter.install_package,
                enabled=adapter.enabled,
                selected=adapter.selected,
                installed=adapter.installed,
                status=adapter.status,
                version=adapter.version,
                warning_code=adapter.warning_code,
            )
            for adapter in runtime.adapters
        ],
        scorecard=ParserAdapterScorecardData(
            selected_backend=scorecard.selected_backend,
            recommended_backend=scorecard.recommended_backend,
            metrics_source=scorecard.metrics_source,
            metrics_applied_to=scorecard.metrics_applied_to,
            entries=[
                ParserAdapterScorecardEntryData(
                    backend=entry.backend,
                    rank=entry.rank,
                    score=entry.score,
                    status=entry.status,
                    recommended=entry.recommended,
                    executable=entry.executable,
                    selected=entry.selected,
                    enabled=entry.enabled,
                    installed=entry.installed,
                    metric_source=entry.metric_source,
                    metric_count=entry.metric_count,
                    signals=dict(entry.signals),
                    reason_codes=list(entry.reason_codes),
                    warning_codes=list(entry.warning_codes),
                )
                for entry in scorecard.entries
            ],
        ),
        source_routes=route_data,
        backend_source_kind_matrix=_parser_adapter_backend_source_matrix(route_data),
        capabilities=_parser_backend_capabilities_data(),
        vision_enabled=settings.rag_vision_enabled,
        heading_level_inference_enabled=settings.rag_heading_level_inference_enabled,
        field_extraction_enabled=settings.rag_field_extraction_enabled,
        navigation_summary_enabled=settings.rag_navigation_summary_enabled,
        navigation_summary_max_nodes=settings.rag_navigation_summary_max_nodes,
        raptor_enabled=settings.rag_raptor_enabled,
        vision_concurrency=settings.rag_vision_concurrency,
        config_source="runtime",
    )


def _post_parse_settings_updates(
    payload: ParserAdapterSettingsUpdate,
) -> dict[str, bool | int]:
    """「解析後の処理」の更新（Settings の属性名 → 値）。省略・null の項目は変えない。"""
    updates: dict[str, bool | int] = {}
    for name in POST_PARSE_SETTING_FIELDS:
        value = getattr(payload, name)
        if value is not None:
            updates[f"rag_{name}"] = value
    return updates


def _persist_post_parse_settings(settings: Settings) -> None:
    """「解析後の処理」の全体の既定を backend/.env へ永続化する（#528）。"""
    _write_env_values(
        BACKEND_ENV_FILE,
        {
            "RAG_VISION_ENABLED": _format_env_bool(settings.rag_vision_enabled),
            "RAG_HEADING_LEVEL_INFERENCE_ENABLED": _format_env_bool(
                settings.rag_heading_level_inference_enabled
            ),
            "RAG_FIELD_EXTRACTION_ENABLED": _format_env_bool(settings.rag_field_extraction_enabled),
            "RAG_NAVIGATION_SUMMARY_ENABLED": _format_env_bool(
                settings.rag_navigation_summary_enabled
            ),
            "RAG_RAPTOR_ENABLED": _format_env_bool(settings.rag_raptor_enabled),
            "RAG_VISION_CONCURRENCY": str(settings.rag_vision_concurrency),
        },
        section_comment="# 文書解析: 解析後の処理",
        error_detail="解析後の処理の設定を backend/.env へ保存できませんでした。",
    )


def _pipeline_settings_data(settings: Settings) -> PipelineSettingsData:
    """Settings から設定の概要の表示用データを作る。"""
    return PipelineSettingsData(
        auto_parse_after_preprocess_enabled=settings.rag_auto_parse_after_preprocess_enabled,
        auto_chunk_after_extract_enabled=settings.rag_auto_chunk_after_extract_enabled,
        auto_index_after_chunk_enabled=settings.rag_auto_index_after_chunk_enabled,
        recipe_defaults=global_document_processing_config(settings),
        vector_index_profile=normalize_vector_index_profile(settings.rag_vector_index_profile),
    )


def _persist_pipeline_settings(settings: Settings) -> None:
    """工程の自動進行を backend/.env へ永続化する（#528）。"""
    _write_env_values(
        BACKEND_ENV_FILE,
        {
            "RAG_AUTO_PARSE_AFTER_PREPROCESS_ENABLED": _format_env_bool(
                settings.rag_auto_parse_after_preprocess_enabled
            ),
            "RAG_AUTO_CHUNK_AFTER_EXTRACT_ENABLED": _format_env_bool(
                settings.rag_auto_chunk_after_extract_enabled
            ),
            "RAG_AUTO_INDEX_AFTER_CHUNK_ENABLED": _format_env_bool(
                settings.rag_auto_index_after_chunk_enabled
            ),
        },
        section_comment="# 工程の自動進行",
        error_detail="工程の自動進行の設定を backend/.env へ保存できませんでした。",
    )


def _parser_backend_capabilities_data() -> list[ParserBackendCapabilityData]:
    """capabilities 正本から backend ごとの対応形式宣言を作る。"""
    return [
        ParserBackendCapabilityData(
            backend=backend,
            modalities=[m.value for m in supported_modalities(backend)],
            extensions=sorted(capability.extensions),
        )
        for backend, capability in ADAPTER_CAPABILITIES.items()
    ]


def _parser_adapter_source_route_data(
    route: ParserAdapterSourceRoute,
) -> ParserAdapterSourceRouteData:
    return ParserAdapterSourceRouteData(
        source_kind=route.source_kind,
        candidate_order=list(route.candidate_order),
        attempted_order=list(route.attempted_order),
        active_order=list(route.active_order),
        selected_backend=route.selected_backend,
        reason_codes=list(route.reason_codes),
        warning_codes=list(route.warning_codes),
    )


def _parser_adapter_backend_source_matrix(
    routes: list[ParserAdapterSourceRouteData],
) -> ParserAdapterBackendSourceMatrixData:
    backend_source_kinds: dict[str, list[str]] = {}
    covered_source_kinds: list[str] = []
    for route in routes:
        backend_source_kinds.setdefault(route.selected_backend, []).append(route.source_kind)
        covered_source_kinds.append(route.source_kind)
    normalized_backend_source_kinds = {
        backend: sorted(set(source_kinds))
        for backend, source_kinds in sorted(backend_source_kinds.items())
    }
    required_source_kinds = sorted({route.source_kind for route in routes})
    covered = sorted(set(covered_source_kinds))
    return ParserAdapterBackendSourceMatrixData(
        evidence_source="runtime_routes",
        required_source_kinds=required_source_kinds,
        covered_source_kinds=covered,
        missing_source_kinds=sorted(set(required_source_kinds) - set(covered)),
        backend_source_kinds=normalized_backend_source_kinds,
        route_evidence=routes,
    )


def _parser_adapter_settings_candidate(
    base: Settings,
    payload: ParserAdapterSettingsUpdate,
) -> Settings:
    """parser adapter 更新 payload から保存候補 settings を作る。"""
    updates: dict[str, object] = {
        # 省略したときは今の解析エンジンのまま（「解析後の処理」だけの保存。#528）。
        "rag_parser_adapter_backend": payload.adapter_backend or base.rag_parser_adapter_backend,
        "rag_parser_docling_enabled": _optional_bool(
            payload.docling_enabled,
            base.rag_parser_docling_enabled,
        ),
        "rag_parser_unstructured_enabled": _optional_bool(
            payload.unstructured_enabled,
            base.rag_parser_unstructured_enabled,
        ),
    }
    return base.model_copy(update=updates)


def _optional_bool(value: bool | None, fallback: bool) -> bool:
    return fallback if value is None else value


def _graph_settings_data(settings: Settings) -> GraphSettingsData:
    """Settings から文書構造索引の設定の表示用データを作る。"""
    runtime = graph_adapter_runtime_settings(settings)
    return GraphSettingsData(
        profile=runtime.profile,
        enabled=runtime.enabled,
        profiles=[
            GraphProfileStatusData(name=status.name, selected=status.selected)
            for status in runtime.profiles
        ],
        config_source="runtime",
    )


def _persist_graph_settings(settings: Settings) -> None:
    """文書構造索引の設定を backend/.env へ永続化する。"""
    _write_env_values(
        BACKEND_ENV_FILE,
        {"RAG_GRAPH_PROFILE": settings.rag_graph_profile},
        section_comment="# 文書構造索引の構築",
        error_detail="文書構造索引の設定を backend/.env へ保存できませんでした。",
    )


def _evaluation_settings_data(settings: Settings) -> EvaluationSettingsData:
    """Settings から品質評価設定の表示用データを作る。"""
    runtime = evaluation_adapter_runtime_settings(settings)

    def _thresholds_dict(thresholds: EvaluationThresholds | None) -> dict[str, float]:
        if thresholds is None:
            return {}
        return {
            key: float(value) for key, value in thresholds.model_dump(exclude_none=True).items()
        }

    return EvaluationSettingsData(
        suite=runtime.suite,
        thresholds=_thresholds_dict(runtime.thresholds),
        suites=[
            EvaluationSuiteStatusData(
                name=status.name,
                origin=status.origin,
                recommended_for=list(status.recommended_for),
                selected=status.selected,
                thresholds=_thresholds_dict(status.thresholds),
            )
            for status in runtime.suites
        ],
        config_source="runtime",
    )


def _persist_evaluation_settings(settings: Settings) -> None:
    """品質評価設定を backend/.env へ永続化する。"""
    _write_env_values(
        BACKEND_ENV_FILE,
        {"RAG_EVALUATION_SUITE": settings.rag_evaluation_suite},
        section_comment="# Evaluation アダプター",
        error_detail="品質評価設定を backend/.env へ保存できませんでした。",
    )


async def _vector_index_settings_data(settings: Settings) -> VectorIndexSettingsData:
    """Settings と実際の索引の値から検索インデックス設定の表示用データを作る。"""
    actual_build = await OracleClient(settings).get_vector_index_build_params()
    runtime = vector_index_adapter_runtime_settings(settings, actual_build)
    return VectorIndexSettingsData(
        profile=runtime.profile,
        target_accuracy=runtime.target_accuracy,
        neighbors=runtime.neighbors,
        efconstruction=runtime.efconstruction,
        distance=runtime.distance,
        requires_reprovision=runtime.requires_reprovision,
        index_status=runtime.index_status,
        actual_neighbors=runtime.actual_neighbors,
        actual_efconstruction=runtime.actual_efconstruction,
        profiles=[
            VectorIndexProfileStatusData(
                name=status.name,
                origin=status.origin,
                recommended_for=list(status.recommended_for),
                selected=status.selected,
                target_accuracy=status.target_accuracy,
                neighbors=status.neighbors,
                efconstruction=status.efconstruction,
                distance=status.distance,
                index_status=status.index_status,
            )
            for status in runtime.profiles
        ],
        reindex_sql=vector_index_reindex_sql(
            target_accuracy=runtime.target_accuracy,
            neighbors=runtime.neighbors,
            efconstruction=runtime.efconstruction,
            distance=runtime.distance,
        ),
        config_source="runtime",
    )


def _persist_vector_index_settings(settings: Settings) -> None:
    """検索インデックス設定を backend/.env へ永続化する。"""
    _write_env_values(
        BACKEND_ENV_FILE,
        {"RAG_VECTOR_INDEX_PROFILE": settings.rag_vector_index_profile},
        section_comment="# Vector Index アダプター",
        error_detail="検索インデックス設定を backend/.env へ保存できませんでした。",
    )


def _guardrail_settings_data(settings: Settings) -> GuardrailSettingsData:
    """Settings から安全チェック設定の表示用データを作る。"""
    return GuardrailSettingsData(
        input_enabled=settings.rag_guardrails_input_enabled,
        output_enabled=settings.rag_guardrails_output_enabled,
        oci_configured=_oci_guardrails_configured(settings),
        oci_warning_code=_oci_guardrails_readiness_code(settings),
    )


def _oci_guardrails_configured(settings: Settings) -> bool:
    """OCI Guardrails の compartment と API キー認証を静的に確認する。"""
    compartment_configured = bool(
        str(getattr(settings, "oci_guardrails_compartment_id", "") or "").strip()
        or str(getattr(settings, "oci_compartment_id", "") or "").strip()
    )
    return (
        compartment_configured
        and _test_oci_config(settings, verify_with_oci=False).status == "success"
    )


def _oci_guardrails_readiness_code(settings: Settings) -> str | None:
    """OCI Guardrails を使う場合の静的 readiness の問題(なければ None)を返す。"""
    compartment_configured = bool(
        str(getattr(settings, "oci_guardrails_compartment_id", "") or "").strip()
        or str(getattr(settings, "oci_compartment_id", "") or "").strip()
    )
    if not compartment_configured:
        return "oci_guardrails_compartment_missing"
    if _test_oci_config(settings, verify_with_oci=False).status != "success":
        return "oci_guardrails_credentials_invalid"
    return None


def _persist_guardrail_settings(settings: Settings) -> None:
    """安全チェック設定を backend/.env へ永続化する。"""
    _write_env_values(
        BACKEND_ENV_FILE,
        {
            "RAG_GUARDRAILS_INPUT_ENABLED": str(settings.rag_guardrails_input_enabled).lower(),
            "RAG_GUARDRAILS_OUTPUT_ENABLED": str(settings.rag_guardrails_output_enabled).lower(),
        },
        section_comment="# OCI Guardrails",
        error_detail="安全チェック設定を backend/.env へ保存できませんでした。",
    )


def _preprocess_settings_data(settings: Settings) -> PreprocessSettingsData:
    """Settings からファイル準備設定の表示用データを作る。"""
    runtime = preprocess_runtime_settings(settings)
    return PreprocessSettingsData(
        profile=runtime.profile,
        service_enabled=runtime.service_enabled,
        service_url=runtime.service_url,
        canonical_artifact_prefix=runtime.canonical_artifact_prefix,
        profiles=[
            PreprocessProfileStatusData(
                name=status.name,
                origin=status.origin,
                recommended_for=list(status.recommended_for),
                selected=status.selected,
                in_process=status.in_process,
                requires_service=status.requires_service,
                available=status.available,
            )
            for status in runtime.profiles
        ],
        config_source="runtime",
    )


def _preprocess_settings_candidate(
    base: Settings,
    payload: PreprocessSettingsUpdate,
) -> Settings:
    """前処理アダプター更新 payload から保存候補 settings を作る。"""
    return base.model_copy(
        update={"rag_preprocess_profile": normalize_preprocess_profile(payload.profile)}
    )


def _apply_preprocess_settings(target: Settings, source: Settings) -> None:
    """保存済みファイル準備設定を現在プロセスへ反映する。"""
    target.rag_preprocess_profile = source.rag_preprocess_profile


def _persist_preprocess_settings(settings: Settings) -> None:
    """ファイル準備設定を backend/.env へ永続化する。"""
    _write_env_values(
        BACKEND_ENV_FILE,
        {"RAG_PREPROCESS_PROFILE": settings.rag_preprocess_profile},
        section_comment="# 前処理(Preprocess)アダプター",
        error_detail="ファイル準備設定を backend/.env へ保存できませんでした。",
    )


def _chunking_settings_data(settings: Settings) -> ChunkingSettingsData:
    """Settings から文書分割設定の表示用データを作る。"""
    runtime = chunking_runtime_settings(settings)
    return ChunkingSettingsData(
        strategy=runtime.strategy,
        chunk_size=runtime.chunk_size,
        overlap=runtime.overlap,
        min_chars=runtime.min_chars,
        delimiter=runtime.delimiter,
        context_header_enabled=settings.rag_chunk_context_header_enabled,
        chunk_child_target_chars=runtime.small_to_big.child_target_chars,
        chunk_table_child_target_chars=runtime.small_to_big.table_child_target_chars,
        chunk_parent_target_chars=runtime.small_to_big.parent_target_chars,
        chunk_parent_max_pages=runtime.small_to_big.parent_max_pages,
        chunk_parent_max_children=runtime.small_to_big.parent_max_children,
        strategies=[
            ChunkingStrategyStatusData(
                name=status.name,
                origin=status.origin,
                recommended_for=list(status.recommended_for),
                selected=status.selected,
            )
            for status in runtime.strategies
        ],
        config_source="runtime",
    )


def _chunking_settings_candidate(
    base: Settings,
    payload: ChunkingSettingsUpdate,
) -> Settings:
    """Chunking アダプター更新 payload から保存候補 settings を作る。"""
    return base.model_copy(
        update={
            "rag_chunking_strategy": normalize_chunking_strategy(payload.strategy),
            "rag_chunk_size": payload.chunk_size,
            "rag_chunk_overlap": payload.overlap,
            "rag_chunk_min_chars": payload.min_chars,
            "rag_chunk_delimiter": payload.delimiter,
            "rag_chunk_child_target_chars": payload.chunk_child_target_chars,
            "rag_chunk_table_child_target_chars": payload.chunk_table_child_target_chars,
            "rag_chunk_parent_target_chars": payload.chunk_parent_target_chars,
            "rag_chunk_parent_max_pages": payload.chunk_parent_max_pages,
            "rag_chunk_parent_max_children": payload.chunk_parent_max_children,
        }
    )


def _apply_chunking_settings(target: Settings, source: Settings) -> None:
    """保存済み文書分割設定を現在プロセスへ反映する。"""
    target.rag_chunking_strategy = source.rag_chunking_strategy
    target.rag_chunk_size = source.rag_chunk_size
    target.rag_chunk_overlap = source.rag_chunk_overlap
    target.rag_chunk_min_chars = source.rag_chunk_min_chars
    target.rag_chunk_delimiter = source.rag_chunk_delimiter
    for name in SMALL_TO_BIG_SETTING_FIELDS:
        setattr(target, name, getattr(source, name))


def _persist_chunking_settings(settings: Settings) -> None:
    """文書分割設定を backend/.env へ永続化する。"""
    _write_env_values(
        BACKEND_ENV_FILE,
        {
            "RAG_CHUNKING_STRATEGY": settings.rag_chunking_strategy,
            "RAG_CHUNK_SIZE": str(settings.rag_chunk_size),
            "RAG_CHUNK_OVERLAP": str(settings.rag_chunk_overlap),
            "RAG_CHUNK_MIN_CHARS": str(settings.rag_chunk_min_chars),
            "RAG_CHUNK_DELIMITER": settings.rag_chunk_delimiter,
            **{name.upper(): str(getattr(settings, name)) for name in SMALL_TO_BIG_SETTING_FIELDS},
            # 交差参照の解析の語は言語資源へ移した(#2181)。以前に保存した値は消す。
            **{f"RAG_REFERENCE_{spec.key.upper()}": None for spec in FIELD_SPECS},
            # 削除した親子階層(#271)の子 chunk サイズ。読まない値なので保存時に消す。
            "RAG_CHUNK_CHILD_SIZE": None,
        },
        section_comment="# Chunking アダプター",
        error_detail="文書分割設定を backend/.env へ保存できませんでした。",
    )


def _format_env_bool(value: bool) -> str:
    return "true" if value else "false"


def _write_env_values(
    path: Path,
    values: Mapping[str, str | None],
    *,
    section_comment: str,
    error_detail: str,
) -> None:
    """既存 .env のコメントや無関係な値を保ったまま指定 key だけ更新する。"""
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        next_lines: list[str] = []
        written: set[str] = set()
        for line in lines:
            key = _env_assignment_key(line)
            if key is None or key not in values:
                next_lines.append(line)
                continue
            if key in written:
                continue
            value = values[key]
            if value is None:
                written.add(key)
                continue
            next_lines.append(f"{key}={_format_env_value(value)}")
            written.add(key)

        missing = [key for key, value in values.items() if key not in written and value is not None]
        if missing:
            if next_lines and next_lines[-1].strip():
                next_lines.append("")
            next_lines.append(section_comment)
            for key in missing:
                value = values[key]
                if value is not None:
                    next_lines.append(f"{key}={_format_env_value(value)}")

        content = "\n".join(next_lines).rstrip() + "\n"
        _replace_env_file(path, content)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=error_detail) from exc


def _env_assignment_key(line: str) -> str | None:
    """通常の .env 代入行から key を取り出す。コメント行は対象外。"""
    if line.lstrip().startswith("#"):
        return None
    match = ENV_ASSIGNMENT_RE.match(line)
    return match.group(1) if match else None


def _format_env_value(value: str) -> str:
    """python-dotenv と shell の両方で読みやすい .env value へ整形する。"""
    normalized = value.strip()
    if not normalized:
        return ""
    if re.search(r"[\s#\"']", normalized):
        return '"' + normalized.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return normalized


def _replace_env_file(path: Path, content: str) -> None:
    """同一ディレクトリ内の一時ファイルから atomic replace する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else ENV_FILE_MODE
    tmp_path = path.with_name(f".{path.name}.tmp-{uuid4().hex}")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.chmod(mode)
        tmp_path.replace(path)
        path.chmod(mode)
    finally:
        tmp_path.unlink(missing_ok=True)


def _secret_value(*, current: str, update: str | None, clear: bool) -> str:
    """secret の保持・更新・削除を判定する。"""
    if clear:
        return ""
    if update is not None and update != "":
        return update
    return current
