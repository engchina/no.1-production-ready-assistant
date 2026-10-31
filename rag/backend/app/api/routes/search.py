"""RAG 検索 API。"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import suppress
from time import perf_counter
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pr_backend_core.api import DEFAULT_PAGE_LIMIT, OffsetParams, offset_params, paginate
from pr_backend_core.mcp import report_tool_progress
from pr_system_settings.auth.errors import SecurityApiError

from app.clients.oracle import OracleClient
from app.clients.oracle_run_history import RunHistoryStore
from app.clients.support_guide_store import SupportGuideStore
from app.config import (
    Settings,
    enterprise_ai_default_model_id,
    enterprise_ai_model_catalog,
    enterprise_ai_vision_model_id,
    get_settings,
)
from app.rag.answer_assumptions import with_answer_assumptions
from app.rag.answer_engine import AnswerScope
from app.rag.answer_failure import (
    ANSWER_API_FAILED_ERROR_CODE,
    ANSWER_INCOMPLETE_ERROR_CODE,
    AnswerApiFailure,
    describe_answer_api_failure,
    describe_answer_failure,
    find_incomplete_response,
)
from app.rag.answer_notice import GUIDE_HANDOFF_CONTACT_KEY, current_answer_notice
from app.rag.answer_provenance import search_answer_profile_revision
from app.rag.answer_timeout import (
    AnswerTimeoutError,
    StageTracker,
    answer_stage_label,
    run_answer_with_timeout,
)
from app.rag.audit import record_rag_search_audit
from app.rag.chat_question_token import context_digest
from app.rag.diagnostics import build_search_diagnostics
from app.rag.document_sections_service import current_section_pages
from app.rag.extraction_field_adapter import load_field_schema, resolve_field_definitions
from app.rag.observability import (
    SEARCH_METRIC_MODE,
    elapsed_ms,
    new_trace_id,
    record_rag_request,
)
from app.rag.pipeline import RagPipeline, SearchStageProgress
from app.rag.question_understanding import profile_language_scope
from app.rag.rate_limit import enforce_rate_limit
from app.rag.request_context import current_audit_request_context
from app.rag.rule_selection import apply_rule_selection_scope, recognize_rule_selections
from app.rag.run_history import RunRecorder, search_run_conditions
from app.rag.saved_evidence import withhold_saved_answer
from app.rag.search_answer_profile_config import (
    ProfileAnswerModelUnavailableError,
    SearchAnswerProfileConfig,
    resolve_search_answer_profile_settings,
    validate_profile_answer_model,
)
from app.rag.search_answer_profile_knowledge import (
    RUNTIME_KNOWLEDGE_KIND,
    clarification_document_ids,
    load_domain_keywords,
)
from app.rag.stored_answer import stored_citation_metadata
from app.rag.support_guide_conditions import (
    resolve_answered_conditions,
    resolve_text_conditions,
)
from app.rag.support_guide_evidence import (
    EVIDENCE_CONDITIONS_KEY,
    EVIDENCE_PLANS_KEY,
    evidence_condition_candidates,
    evidence_plans,
)
from app.rag.support_guide_query import build_guide_query
from app.rag.support_guide_runtime import (
    GUIDE_LOAD_FAILED_KEY,
    GUIDE_PREVIEW_TRACE_PREFIX,
    GuideChoice,
    GuideContext,
    GuidePreview,
    build_guide_context,
    clarification_questions,
    guide_notices,
    guide_step_plan,
    query_summary,
    select_guide,
    short_circuit_answer,
    with_draft,
    with_guide_rule,
)
from app.rag.trace_store import AnswerTrace, TraceSnapshot, run_trace_snapshot, trace_snapshot
from app.schemas.common import ApiResponse, Page
from app.schemas.search import (
    AnswerRecordDetail,
    AnswerRecordSummary,
    SearchRequest,
    SearchResponse,
    format_search_id_filter,
)
from app.schemas.search_answer_profile import SearchAnswerProfileDetail
from app.schemas.search_answer_profile_knowledge import ClarificationAnswer
from app.schemas.settings import FieldDefinitionData, SearchExtractionFieldsData
from app.schemas.support_guide import SupportGuideContent
from app.security.permissions import SCOPE_FORBIDDEN_CODE

router = APIRouter()
logger = logging.getLogger(__name__)
STREAM_ERROR_MESSAGE = "検索処理中にエラーが発生しました。"


class AnswerIncompleteHTTPError(HTTPException):
    """LLM の応答が送り直しても未完了だった回答の失敗（502。#2023）。

    REST は利用者向けの文（理由と直し方）を返し、MCP は ``code`` を error code にする
    （`pr_backend_core.mcp.mcp_error_from_exception`）。
    """

    code = ANSWER_INCOMPLETE_ERROR_CODE

    def __init__(self, message: str) -> None:
        super().__init__(status_code=502, detail=message)


class AnswerApiFailedHTTPError(HTTPException):
    """回答の pipeline の中で AI の API の呼び出しが失敗した回答（種類ごとの 5xx。#2667）。

    割り当ての使い切り・流量の制限などを定型の 500 にせず、種類・再試行の目安・中断した工程の文と
    種類ごとの status（呼び先が示せば ``Retry-After``）で返す。MCP の ``rag_search`` は
    ``failure`` を種類つきのツールのエラーにする（#2475）。呼び先の応答の本文・質問は入れない。
    """

    code = ANSWER_API_FAILED_ERROR_CODE

    def __init__(self, failure: AnswerApiFailure) -> None:
        super().__init__(
            status_code=failure.status, detail=failure.message, headers=failure.headers()
        )
        self.failure = failure


def _log_answer_failure(event: str, trace_id: str, exc: Exception, fallback: str) -> None:
    """回答の失敗を traceback つきで残す。

    未完了の応答は理由・上限・推論の量（「詳細」の文）も残す（#2023）。
    """
    failure = describe_answer_failure(exc, fallback)
    logger.exception(
        event,
        extra={
            "trace_id": trace_id,
            "exception_type": type(exc).__name__,
            **({"incomplete_reason": failure.reason} if failure.reason else {}),
            **({"incomplete_detail": failure.detail} if failure.detail else {}),
            **({"error_stage": failure.stage} if failure.stage else {}),
        },
    )


def answer_model_choices(settings: Settings) -> list[dict[str, str]]:
    """回答に選べるモデル(既定のテキストモデル、既定の Vision モデルの順。#675)。

    チャットの比較と RAG 検索の選択で共通に使う。``kind`` は ``text`` / ``vision`` /
    ``text_vision``。Vision が未設定ならテキストの 1 件だけ。テキストと同じモデルなら 1 件に
    まとめ、``kind`` を ``text_vision`` にして画像対応モデルでもあることを画面に伝える
    (同じモデルを 2 件並べても比較にならない。#888)。
    """
    labels = {
        model.model_id: model.display_name or model.model_id
        for model in enterprise_ai_model_catalog(settings)
        if model.model_id
    }
    text_model = enterprise_ai_default_model_id(settings)
    vision_model = enterprise_ai_vision_model_id(settings)
    choices: list[dict[str, str]] = []
    if text_model:
        kind = "text_vision" if vision_model == text_model else "text"
        choices.append(
            {
                "model_id": text_model,
                "display_name": labels.get(text_model, text_model),
                "kind": kind,
            }
        )
    if vision_model and vision_model != text_model:
        choices.append(
            {
                "model_id": vision_model,
                "display_name": labels.get(vision_model, vision_model),
                "kind": "vision",
            }
        )
    return choices


def registered_answer_model_choices(settings: Settings) -> list[dict[str, str]]:
    """回答設定に保存できる登録モデルの、接続情報を含まない候補。"""
    choices = answer_model_choices(settings)
    selected_ids = {choice["model_id"] for choice in choices}
    choices.extend(
        {
            "model_id": model.model_id,
            "display_name": model.display_name or model.model_id,
            "kind": "text_vision" if model.vision_enabled else "text",
        }
        for model in enterprise_ai_model_catalog(settings)
        if model.model_id and model.model_id not in selected_ids
    )
    return choices


def _answer_model_id(request: SearchRequest, settings: Settings) -> str | None:
    """RAG 検索で選んだ回答のモデル。候補外・未指定は None(既定のテキストモデル)。"""
    if request.model_id and any(
        choice["model_id"] == request.model_id
        for choice in registered_answer_model_choices(settings)
    ):
        return request.model_id
    return None


def _answer_model_registered(settings: Settings, config: SearchAnswerProfileConfig) -> bool:
    """検索・回答プロファイルの回答のモデルが登録済み（または未指定）か。"""
    try:
        validate_profile_answer_model(settings, config)
    except ProfileAnswerModelUnavailableError:
        return False
    return True


def _run_model_id(answer_model_id: str | None, settings: Settings) -> str | None:
    """実行履歴に残す回答のモデル（未指定は実行のときの既定のテキストモデルの具体的な ID。#3419）。

    回答エンジンと同じ規則（`answer_model_id or enterprise_ai_default_model_id`）で、実行の時点に
    決める。後で既定のモデルを変えても、過去の実行のモデルは変わらない。
    """
    return answer_model_id or enterprise_ai_default_model_id(settings) or None


@router.get("/models", response_model=ApiResponse[list[dict[str, str]]])
async def list_answer_models() -> ApiResponse[list[dict[str, str]]]:
    """RAG 検索の回答に選べるモデル(先頭が未選択のときの既定。#675)。"""
    models = answer_model_choices(get_settings())
    return ApiResponse(
        data=models,
        warning_messages=(
            [] if models else ["生成モデルが未設定です。システム設定 > モデルで登録してください。"]
        ),
    )


@router.post("", response_model=ApiResponse[SearchResponse])
async def search(
    http_request: Request,
    request: SearchRequest,
) -> ApiResponse[SearchResponse]:
    """自然言語クエリで RAG 検索を実行する。

    回答は回答フロー(質問の理解 -> Oracle AI Vector Search の hybrid 検索 ->
    Cohere Rerank -> 根拠の評価・補正検索 -> 回答の生成と監査)で作る。
    """
    enforce_rate_limit("search", http_request)
    result = await _run_search_with_timeout(request, run_recorder=_search_run_recorder(request))
    return ApiResponse(data=result)


def _search_run_recorder(request: SearchRequest) -> RunRecorder | None:
    """RAG 検索の画面の実行を実行履歴に残す（#1890）。根拠の検索だけの実行は残さない。"""
    if request.retrieval_only:
        return None
    return RunRecorder(
        surface="search",
        settings=get_settings(),
        store=RunHistoryStore(OracleClient()),
        conditions=search_run_conditions(request),
    )


@router.post("/stream")
async def stream_search(
    http_request: Request,
    request: SearchRequest,
) -> StreamingResponse:
    """RAG 検索結果を SSE 形式でストリーミングする。"""
    enforce_rate_limit("search", http_request)
    # 検索・回答プロファイル・KB の解決（404 / 403）は s
    # tream を始める前に行い、HTTP の status で返す。
    #
    resolved = await _resolve_query_context(request, get_settings())
    return StreamingResponse(
        _stream_search_events_with_timeout(resolved, run_recorder=_search_run_recorder(request)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _resolve_query_context(
    request: SearchRequest,
    global_settings: Settings,
    *,
    guide_context: Sequence[str] = (),
    interactive: bool = False,
    guide_preview: GuidePreview | None = None,
    answered_rule_ids: Sequence[str] = (),
    confirmed_rule_answers: Sequence[ClarificationAnswer] = (),
) -> tuple[SearchRequest, Settings, str | None, str | None]:
    """検索の有効 request / Settings と適用済みの Search Answer Profile id を返す。

    解決順は Search Answer Profile > グローバル既定。
    検索・回答プロファイル指定時は参照 KB 群を検索対象へ展開し、その回答の設定を適用する。
    KB はナレッジ構築設定だけを持つため、KB query legacy 値は検索 runtime へ反映しない。
    戻り値は (有効 request, 有効 Settings, 適用 KB id, 適用 Search Answer Profile id)。

    業務ガイド（#1238）は ``guide_context``（チャットの前の発話）と質問で選ぶ。``interactive``
    （チャット）は送信の前に確認の質問を出すので、残った不明の条件は分岐で答える。

    ``guide_preview``（下書きで試す。#1288）は、そのガイドの公開の版の代わりに下書きを、この
    1 回の回答の照合にだけ使う（保存・公開の版は変えない）。回答の記録に印を残す。
    """
    oracle = OracleClient()
    settings = global_settings
    if request.search_answer_profile_id:
        search_answer_profile_id = request.search_answer_profile_id
        view = await oracle.get_search_answer_profile(search_answer_profile_id)
        if view is None:
            raise HTTPException(
                status_code=404,
                detail=f"{SEARCH_ANSWER_PROFILE_NOT_FOUND_PREFIX}{search_answer_profile_id}",
            )
        ensure_search_answer_profile_not_archived(view, search_answer_profile_id)
        effective_request = request
        kb_ids = view.config.normalized_knowledge_base_ids()
        # 参照 KB が 0 件の検索・回答プロファイルで利用者の全 KB を検索しない（#304）。
        if not request.knowledge_base_ids:
            ensure_search_answer_profile_has_knowledge_bases(kb_ids)
            # request 明示の KB があればそちらを優先し、無ければ参照 KB 群を展開する。
            effective_request = _with_knowledge_base_ids(request, kb_ids)
        # 利用者の KB 範囲との積集合にする（request で範囲外の KB を読めないようにする）。
        effective_request = _scope_request_knowledge_bases(
            effective_request, from_search_answer_profile=not request.knowledge_base_ids
        )
        profile_config = view.config
        if explicit_model := _answer_model_id(request, global_settings):
            profile_config = profile_config.model_copy(update={"answer_model_id": explicit_model})
        # 回答を作らない検索も、根拠を集める LLM（質問拡張・CRAG など）はチャットと同じ
        # 回答のモデルで呼ぶ（#3506。回答の有無でモデルを変えない）。ただし回答を作らない検索は、
        # 登録を外したモデルを選んだままのプロファイルでも止めず、既定のモデルで根拠を集める。
        settings, applied = resolve_search_answer_profile_settings(
            settings,
            profile_config,
            apply_answer_model=request.generate_answer
            or _answer_model_registered(global_settings, profile_config),
        )
        with profile_language_scope(settings):
            # 回答の記録に残す検索・回答プロファイルの版（#1276）。
            settings = settings.model_copy(
                update={"rag_search_answer_profile_revision": search_answer_profile_revision(view)}
            )
            # 検索・回答プロファイルのドメインキーワードは全文検索で 1 語として優先する。
            domain_keywords = await load_domain_keywords(oracle, view.id)
            if domain_keywords:
                settings = settings.model_copy(update={"rag_domain_keywords": domain_keywords})
            # 用語・同義語と回答ルールは回答エンジンだけが使う。
            runtime_knowledge = await oracle.get_search_answer_profile_knowledge(
                view.id, RUNTIME_KNOWLEDGE_KIND
            )
            selection_state = dict(effective_request._rule_selection_state)
            selection_binding = context_digest(
                [runtime_knowledge, request.query, request.original_question]
            )
            recognized_answers = tuple(
                answer
                for answer in confirmed_rule_answers
                if answer.rule_id not in answered_rule_ids
            )
            if selection_state.get("binding") == selection_binding:
                recognized_answers = tuple(
                    ClarificationAnswer.model_validate(value)
                    for value in selection_state.get("answers", [])
                )
            elif not selection_state.get("prepared"):
                selection_usage: dict[str, float] = {}
                selection_rules = (runtime_knowledge or {}).get("rules", [])
                if not isinstance(selection_rules, list):
                    selection_rules = []
                recognized_answers = await recognize_rule_selections(
                    {
                        **(runtime_knowledge or {}),
                        "rules": [
                            rule
                            for rule in selection_rules
                            if isinstance(rule, dict)
                            and str(rule.get("id", "")) not in answered_rule_ids
                        ],
                    },
                    request.query,
                    settings,
                    source_text="\n".join(
                        [*guide_context, request.original_question or request.query]
                    ),
                    answers=recognized_answers,
                    usage=selection_usage,
                )
                selection_state["elapsed_seconds"] = float(
                    selection_state.get("elapsed_seconds", 0.0)
                ) + selection_usage.get("elapsed_seconds", 0.0)
            selection_state.update(
                binding=selection_binding,
                answers=[answer.model_dump() for answer in recognized_answers],
            )
            effective_request = apply_rule_selection_scope(
                effective_request,
                runtime_knowledge or {},
                recognized_answers,
                current_pages=await current_section_pages(
                    oracle,
                    {
                        doc
                        for answer in recognized_answers
                        for doc in clarification_document_ids(runtime_knowledge or {}, answer)
                    },
                )
                if recognized_answers
                else None,
            )
            effective_request._rule_selection_state = selection_state
            initial_budget = float(
                selection_state.setdefault(
                    "initial_budget_seconds", settings.rag_answer_timeout_seconds
                )
            )
            remaining_budget = max(
                0.0, initial_budget - float(selection_state.get("elapsed_seconds", 0.0))
            )
            settings = settings.model_copy(
                update={
                    "rag_answer_timeout_seconds": min(
                        settings.rag_answer_timeout_seconds, remaining_budget
                    )
                }
            )
            if guide_preview is not None:
                # 管理画面から明示した下書きの試答だけ、この回答に限って有効にする。
                settings = settings.model_copy(update={"rag_support_guide_enabled": True})
            if not settings.rag_support_guide_enabled:
                runtime_knowledge = await asyncio.to_thread(
                    with_answer_assumptions,
                    runtime_knowledge,
                    policy=profile_config.clarification_policy,
                    question=request.query,
                    conditions=request.conditions,
                    guide={},
                    answered_rule_ids=answered_rule_ids,
                    confirmed_answers=recognized_answers,
                )
                settings = settings.model_copy(
                    update={
                        "rag_support_guide": {},
                        "rag_runtime_knowledge": runtime_knowledge or {},
                    }
                )
                return effective_request, settings, None, view.id if (applied or kb_ids) else None
            # 公開した業務ガイドのうち、質問に合う 1 つを選ぶ（#1238）。適用範囲は質問と検索の
            # 絞り込みの手がかりで確かめる（#1278）。
            guides, guides_failed = await load_published_guides(oracle, view.id)
            if guide_preview is not None:
                guides = with_draft(guides, guide_preview)
                # 下書きの試答は業務ガイドの要件ごとの充足まで詳しく監査する(#2795)。
                settings = settings.model_copy(
                    update={
                        "rag_guide_preview": guide_preview.marker(),
                        "rag_answer_audit_mode": "detailed",
                    }
                )
            guide_text = "\n".join([*guide_context, request.query])
            # 照合の前に用語の別名で展開し、ON なら意味の照合の埋め込みを作る（#2398）。
            query = await build_guide_query(
                oracle,
                settings,
                view.id,
                guides,
                guide_text,
                runtime_knowledge,
                unsaved=frozenset({guide_preview.guide_id})
                if guide_preview is not None
                else frozenset(),
            )
            # 上位のガイドが近いときに確かめるのは、回答を作る検索・MCP だけ（チャットは送信の前の
            # 確認の判定で確かめる）。
            close_margin = (
                settings.rag_support_guide_close_match_margin
                if settings.rag_support_guide_close_match_clarification_enabled
                and request.generate_answer
                and not interactive
                else None
            )
            selected = select_guide(
                guides,
                query,
                request.conditions,
                interactive=interactive,
                context=await support_guide_context(
                    oracle, guides, guide_text, effective_request.filters
                ),
                close_margin=close_margin,
            )
            # 確認に自由入力で答えた選択式の条件は、答えの文が選んだ選択肢に決める（#2726）。
            selected = await resolve_answered_conditions(
                selected, request.conditions, settings, interactive=interactive
            )
            if request.generate_answer:
                selected = await resolve_text_conditions(
                    selected, guide_text, settings, interactive=interactive
                )
            if guides_failed:
                # 読めなかったことを回答の診断に残す（ガイドを使わずに答える。#1278）。
                settings = settings.model_copy(
                    update={"rag_support_guide": {GUIDE_LOAD_FAILED_KEY: True}}
                )
            if isinstance(selected, GuideChoice):
                settings = settings.model_copy(
                    update={
                        "rag_support_guide": {
                            **selected.summary(),
                            **query_summary(query),
                            "clarifications": selected.clarifications(),
                            "short_answer": selected.short_answer(),
                        }
                    }
                )
            elif selected is not None:
                match = selected
                runtime_knowledge = with_guide_rule(runtime_knowledge, match)
                settings = settings.model_copy(
                    update={
                        "rag_support_guide": {
                            **match.summary(),
                            **query_summary(query),
                            **(
                                {"draft": True}
                                if guide_preview is not None
                                and match.guide_id == guide_preview.guide_id
                                else {}
                            ),
                            "clarifications": clarification_questions(match),
                            # 回答の根拠で決められる条件の候補（#2404。画面の図の項目の種類）。
                            EVIDENCE_CONDITIONS_KEY: evidence_condition_candidates(match),
                            # 根拠で条件が決まったときの手順の計画（#2689）。
                            EVIDENCE_PLANS_KEY: evidence_plans(match),
                            # 手順ごとの検索（#2395）。回答フローだけが使う。
                            "step_plan": guide_step_plan(match),
                            # 業務ガイドの窓口・引き継ぐ条件・承認（#2792。資料と分けて出す）。
                            "notices": guide_notices(match),
                            "short_answer": (
                                short_circuit_answer(match)
                                if match.decision in {"clarify", "handoff"}
                                else ""
                            ),
                            # 引き継ぎの窓口（#2400。回答の注意書きで同じ窓口の案内を重ねない）。
                            GUIDE_HANDOFF_CONTACT_KEY: (
                                match.content.handoff.contact if match.decision == "handoff" else ""
                            ),
                        }
                    }
                )
            runtime_knowledge = await asyncio.to_thread(
                with_answer_assumptions,
                runtime_knowledge,
                policy=profile_config.clarification_policy,
                question=request.query,
                conditions=request.conditions,
                guide=settings.rag_support_guide or {},
                answered_rule_ids=answered_rule_ids,
                confirmed_answers=recognized_answers,
            )
            if (
                profile_config.clarification_policy == "assume"
                and settings.rag_support_guide.get("decision") == "clarify"
            ):
                # 確認カードの代わりに、未確定の条件を示して根拠に基づく回答を生成する。
                settings = settings.model_copy(
                    update={
                        "rag_support_guide": {
                            **settings.rag_support_guide,
                            "decision": "branch",
                            "short_answer": "",
                        }
                    }
                )
            if runtime_knowledge:
                settings = settings.model_copy(update={"rag_runtime_knowledge": runtime_knowledge})
            applied_view = view.id if (applied or kb_ids) else None
            return effective_request, settings, None, applied_view

    request = _scope_request_knowledge_bases(request)
    return request, settings, None, None


async def load_published_guides(
    oracle: OracleClient, search_answer_profile_id: str
) -> tuple[list[tuple[str, int, SupportGuideContent]], bool]:
    """公開した業務ガイドと、読めなかったか。

    読めなければ（表をまだ作っていない環境など）使わずに答え、呼び出し元が失敗を回答の診断・MCP の
    エラーに出す（#1278）。
    """
    try:
        guides = await SupportGuideStore(oracle).published_contents(search_answer_profile_id)
    except Exception:  # noqa: BLE001 - 業務ガイドは補助。読めなくても検索・回答は続ける。
        logger.warning("support guides load failed", exc_info=True)
        return [], True
    return guides, False


async def _published_guides(
    oracle: OracleClient, search_answer_profile_id: str
) -> list[tuple[str, int, SupportGuideContent]]:
    """公開した業務ガイド（読めなければ空。確認の質問の候補・チャットの答えの引き直しに使う）。"""
    guides, _ = await load_published_guides(oracle, search_answer_profile_id)
    return guides


def profile_scope_filters(view: SearchAnswerProfileDetail) -> dict[str, str] | None:
    """検索・回答プロファイルの参照 KB のうち利用者が使える KB の絞り込み（無ければ None）。"""
    knowledge_base_ids = view.config.normalized_knowledge_base_ids()
    permitted = permitted_knowledge_base_ids(knowledge_base_ids)
    scoped = knowledge_base_ids if permitted is None else permitted
    return {"knowledge_base_id": format_search_id_filter(scoped)} if scoped else None


async def support_guide_context(
    oracle: OracleClient,
    guides: Sequence[tuple[str, int, SupportGuideContent]],
    text: str,
    filters: Mapping[str, str] | None,
) -> GuideContext:
    """業務ガイドの適用範囲の手がかり（#1278）。

    業務の名指しは、検索範囲（``filters``。KB の範囲を含む）の文書の大分類の語でも探す（#553 と同じ
    一覧）。ガイドに業務の指定が無い・範囲が無いときは読まない。読めなければガイドの語だけで探す。
    """
    vocabulary: list[str] = []
    if filters is not None and any(
        content.applicability.business_domains for _, _, content in guides
    ):
        try:
            vocabulary = await oracle.retrieval_large_categories(dict(filters))
        except Exception:  # noqa: BLE001 - 手がかりは補助。読めなければガイドの語だけで探す。
            logger.warning("business names load failed for support guides", exc_info=True)
    return build_guide_context(text, guides, filters=filters, business_vocabulary=vocabulary)


def ensure_search_answer_profile_not_archived(view: object, search_answer_profile_id: str) -> None:
    """アーカイブ済みの検索・回答プロファイルは検索にも検索の絞り込みの項目にも使わない（409）。"""
    status = getattr(view, "status", None)
    if getattr(status, "value", status) == "ARCHIVED":
        raise HTTPException(
            status_code=409,
            detail=f"アーカイブ済みの検索・回答プロファイルは検索に使用できません: {search_answer_profile_id}",  # noqa: E501
        )


SEARCH_ANSWER_PROFILE_NO_KNOWLEDGE_BASES_MESSAGE = (
    "この検索・回答プロファイルには参照するナレッジベースがありません。"
    "検索・回答プロファイルの設定でナレッジベースを追加してください。"
)
SEARCH_ANSWER_PROFILE_KNOWLEDGE_BASES_FORBIDDEN_MESSAGE = (
    "この検索・回答プロファイルのナレッジベースを利用す"
    "る権限がありません。管理者に権限を依頼してください。"
)
# 指定した検索・回答プロファイルが無い（利用者が使えないものを含む）404 の文の頭。MCP はこの文で
# 見分けて、次の手の付いたツールのエラーにする（app.mcp.tools.profile_not_found_error）。
SEARCH_ANSWER_PROFILE_NOT_FOUND_PREFIX = "指定した検索・回答プロファイルが見つかりません: "
REQUEST_KNOWLEDGE_BASES_FORBIDDEN_MESSAGE = (
    "指定したナレッジベースを利用する権限がありません。管理者に権限を依頼してください。"
)


def permitted_knowledge_base_ids(knowledge_base_ids: Iterable[str]) -> list[str] | None:
    """KB を、利用者が利用できる KB との積集合にする（順序は保つ）。None は制限なし。"""
    allowed = current_audit_request_context().allowed_knowledge_base_ids
    if allowed is None:
        return None
    return [item for item in knowledge_base_ids if item in allowed]


def ensure_search_answer_profile_has_knowledge_bases(knowledge_base_ids: Sequence[str]) -> None:
    """検索・回答プロファイルの参照 KB が 0 件なら検索・チャットをしない（409。#304）。

    KB を絞らずに検索すると利用者が使える全 KB を検索してしまい、画面の説明（選択した業務
    ビューに紐づく KB を検索する）と合わない。理由を返し、画面はその場で表示する。
    """
    if not knowledge_base_ids:
        raise HTTPException(
            status_code=409, detail=SEARCH_ANSWER_PROFILE_NO_KNOWLEDGE_BASES_MESSAGE
        )


def ensure_search_answer_profile_knowledge_bases_permitted(
    knowledge_base_ids: Sequence[str],
) -> None:
    """検索・回答プロファイルの参照 KB が 1 つも利用で
    きないなら 403（黙って 0 件にしない。#214）。"""
    permitted = permitted_knowledge_base_ids(knowledge_base_ids)
    if knowledge_base_ids and permitted is not None and not permitted:
        raise SecurityApiError(
            403, SEARCH_ANSWER_PROFILE_KNOWLEDGE_BASES_FORBIDDEN_MESSAGE, code=SCOPE_FORBIDDEN_CODE
        )


def _scope_request_knowledge_bases(
    request: SearchRequest, *, from_search_answer_profile: bool = False
) -> SearchRequest:
    """検索対象の KB を、利用者が利用できる KB との積集合にする（#214）。

    検索・回答プロファイルの参照 KB を展開した後、または request が明示した KB に適用する。一部だけ
    許可されていれば積集合で検索を続け、積集合が空なら検
    索しない（403。検索・回答プロファイルの KB か

    request が指定した KB かで文言を分ける）。KB を指定しない検索は、Oracle の検索条件が
    利用できる KB へ絞る。
    """
    if not request.knowledge_base_ids:
        return request
    permitted = permitted_knowledge_base_ids(request.knowledge_base_ids)
    if permitted is None or permitted == list(request.knowledge_base_ids):
        return request
    if not permitted:
        raise SecurityApiError(
            403,
            (
                SEARCH_ANSWER_PROFILE_KNOWLEDGE_BASES_FORBIDDEN_MESSAGE
                if from_search_answer_profile
                else REQUEST_KNOWLEDGE_BASES_FORBIDDEN_MESSAGE
            ),
            code=SCOPE_FORBIDDEN_CODE,
        )
    return _with_knowledge_base_ids(request, permitted)


def _with_knowledge_base_ids(
    request: SearchRequest,
    knowledge_base_ids: list[str],
) -> SearchRequest:
    """検索・回答プロファイルの参照 KB 群を検索対象へ展開した request を作る。"""
    payload = request.model_dump()
    payload["knowledge_base_ids"] = knowledge_base_ids
    filters = dict(payload.get("filters") or {})
    # validator が knowledge_base_ids から knowledge_base_id フィルターを再構成する。
    filters.pop("knowledge_base_id", None)
    payload["filters"] = filters
    return SearchRequest.model_validate(payload)


# 取り消した検索・回答のメトリクスの outcome と、監査の error_stage（#2691）。
SEARCH_CANCELLED_OUTCOME = "cancelled"


class SearchCancelledError(Exception):
    """取り消した検索・回答（監査の error_type。#2691）。"""

    def __init__(self) -> None:
        super().__init__("検索・回答は呼び出し元の取消で止めました。")


async def _report_tool_progress(progress: SearchStageProgress) -> None:
    """工程の始まりを MCP の進み具合（`notifications/progress`）で知らせる（#1864）。

    MCP の rag_search で client が progressToken を渡したときだけ届く（それ以外は何もしない）。
    """
    if progress.outcome == "started":
        report_tool_progress(answer_stage_label(progress.stage))


async def _run_search_with_timeout(
    request: SearchRequest,
    *,
    guide_preview: GuidePreview | None = None,
    run_recorder: RunRecorder | None = None,
    scope: AnswerScope | None = None,
    caller_queries: Sequence[str] | None = None,
    complete_evidence: bool = True,
) -> SearchResponse:
    """検索・回答の pipeline を回答生成の上限（`rag_answer_timeout_seconds`。#375）付きで実行する。

    pipeline は回答を LLM で生成するため、LLM を何度か呼んでも収まる回答生成の上限を使う。
    ``guide_preview`` は業務ガイドの下書きで試す回答（#1288。trace_id に接頭辞を付ける）。
    ``run_recorder`` は RAG 検索の画面の実行を実行履歴に残す（#1890。MCP・下書きは渡さない）。
    ``scope`` は回答前の確認の答え（MCP の rag_search のルールの確認。#2117）。
    ``caller_queries`` は呼び出し元（業務 Agent）が作った検索文（MCP の rag_retrieve_evidence の
    planning=caller。#2651）。渡すと質問に応じた推論の LLM を呼ばずに検索する（`RagPipeline`）。
    ``complete_evidence=False`` は回答を作らない検索を主の文書の検索の後で止める（MCP の
    rag_retrieve_evidence。根拠の補いは呼び出し元がする。#3506）。RAG 検索の画面は既定の True で、
    チャットと同じ流れで根拠を集めて回答の生成の手前で止める。
    """
    request, settings, applied_kb, applied_view = await _resolve_query_context(
        request, get_settings(), guide_preview=guide_preview
    )
    started_at = perf_counter()
    trace_id = new_trace_id()
    if guide_preview is not None:
        trace_id = GUIDE_PREVIEW_TRACE_PREFIX + trace_id
    answer_model_id = _answer_model_id(request, settings)
    # 処理の経過（共通の span の表に書く。MCP の要求の中では Agent の trace の下に入る。#3226）。
    trace = AnswerTrace(trace_id, owner_user_uuid=current_audit_request_context().user_uuid)
    if run_recorder is not None:
        run_recorder.model_id = (
            _run_model_id(answer_model_id, settings) if request.generate_answer else None
        )
        # 実行履歴の詳細の処理の経過（表を引く trace id と工程の詳細）とモデルの利用量（#3419）。
        run_recorder.trace_source = trace.record
        run_recorder.usage_source = trace.model_usage
    # 取り消したときに止まった工程を残す（#2691）。
    stages = StageTracker(_report_tool_progress)
    try:
        result = await run_answer_with_timeout(
            lambda tracker: RagPipeline(
                settings=settings,
                answer_model_id=answer_model_id,
                run_recorder=run_recorder,
                scope=scope,
                answer_notice=current_answer_notice,
                caller_queries=caller_queries,
                complete_evidence=complete_evidence,
            ).run(
                request,
                trace_id=trace_id,
                progress_callback=tracker,
                trace=trace,
            ),
            settings,
            stages,
        )
        if applied_kb is not None:
            result.diagnostics.kb_adapter_config_applied = applied_kb
        if applied_view is not None:
            result.diagnostics.search_answer_profile_applied = applied_view
        return result
    except AnswerTimeoutError as exc:
        elapsed = elapsed_ms(started_at)
        diagnostics = build_search_diagnostics(
            request, settings=settings, retrieval_strategy_adapter="grounded"
        )
        if applied_kb is not None:
            diagnostics.kb_adapter_config_applied = applied_kb
        if applied_view is not None:
            diagnostics.search_answer_profile_applied = applied_view
        record_rag_request(SEARCH_METRIC_MODE, "error", elapsed / 1000, 0)
        record_rag_search_audit(
            trace_id=trace_id,
            outcome="error",
            sanitized_query=request.query,
            filters=request.filters,
            findings=[],
            retrieved_count=0,
            citations=[],
            elapsed_ms=elapsed,
            diagnostics=diagnostics,
            # 監査の error_type は従来どおり元の TimeoutError にする。
            error=exc.original_error,
            error_stage="timeout",
        )
        await _record_failed_run(run_recorder, request, trace_id, started_at, exc)
        raise HTTPException(status_code=504, detail=exc.user_message) from exc
    except asyncio.CancelledError:
        # 呼び出し元が取り消した（MCP の client の切断・画面の停止。#2691）。回答の期限を取り消した
        # ので、回答の thread は次の工程・LLM の呼び出しを始めない。成功としては残さず、取消を監査に
        # 残す（監査の outcome は error、error_stage は cancelled。回答の記録は保存しない）。
        _record_cancelled_search(
            request,
            settings,
            trace_id=trace_id,
            started_at=started_at,
            stage=stages.current_stage,
            applied_kb=applied_kb,
            applied_view=applied_view,
        )
        raise
    except HTTPException:
        raise
    except Exception as exc:
        await _record_failed_run(run_recorder, request, trace_id, started_at, exc)
        incomplete = find_incomplete_response(exc)
        if incomplete is not None and incomplete.user_message:
            # 送り直しても未完了の応答（#2023）。定型の 500 ではなく、理由と直し方を返す。
            _log_answer_failure("rag_search_answer_incomplete", trace_id, exc, STREAM_ERROR_MESSAGE)
            raise AnswerIncompleteHTTPError(incomplete.user_message) from exc
        api_failure = describe_answer_api_failure(exc)
        if api_failure is not None:
            # AI の API の失敗（割り当ての使い切り・流量の制限・認証など。#2667）。定型の 500 に
            # せず、種類・再試行の目安・中断した工程を返す。ログも分類の項目だけ（本文を残さない）。
            logger.warning(
                "rag_search_answer_api_failed",
                extra={
                    "trace_id": trace_id,
                    "error_code": ANSWER_API_FAILED_ERROR_CODE,
                    **api_failure.details(),
                },
            )
            raise AnswerApiFailedHTTPError(api_failure) from exc
        raise


def _record_cancelled_search(
    request: SearchRequest,
    settings: Settings,
    *,
    trace_id: str,
    started_at: float,
    stage: str | None,
    applied_kb: str | None,
    applied_view: str | None,
) -> None:
    """取り消した検索・回答を監査とメトリクスに残す（#2691。止まった工程と秒。業務データは残さない）。"""
    elapsed = elapsed_ms(started_at)
    diagnostics = build_search_diagnostics(
        request, settings=settings, retrieval_strategy_adapter="grounded"
    )
    if applied_kb is not None:
        diagnostics.kb_adapter_config_applied = applied_kb
    if applied_view is not None:
        diagnostics.search_answer_profile_applied = applied_view
    record_rag_request(SEARCH_METRIC_MODE, SEARCH_CANCELLED_OUTCOME, elapsed / 1000, 0)
    record_rag_search_audit(
        trace_id=trace_id,
        outcome="error",
        sanitized_query=request.query,
        filters=request.filters,
        findings=[],
        retrieved_count=0,
        citations=[],
        elapsed_ms=elapsed,
        diagnostics=diagnostics,
        error=SearchCancelledError(),
        error_stage=SEARCH_CANCELLED_OUTCOME,
    )
    logger.info(
        "rag_search_cancelled",
        extra={"trace_id": trace_id, "stage": stage, "elapsed_ms": round(elapsed)},
    )


async def _record_failed_run(
    run_recorder: RunRecorder | None,
    request: SearchRequest,
    trace_id: str,
    started_at: float,
    error: Exception,
) -> None:
    """失敗した実行（時間切れ・例外）を実行履歴に残す（#1890。止まった段は集めた段で分かる）。"""
    if run_recorder is None:
        return
    await run_recorder.record(
        run_id=trace_id,
        # 安全チェックの後の質問（pipeline が受け取った時点で recorder に残す）。チェックの前に
        # 失敗したときは保存しない（原文には機微な値が残っている）。
        question=run_recorder.question,
        search_answer_profile_id=request.search_answer_profile_id,
        answer_generated=request.generate_answer,
        status="error",
        elapsed_ms=elapsed_ms(started_at),
        error=error,
        error_stage=((error.stage or "timeout") if isinstance(error, AnswerTimeoutError) else None),
    )


async def run_guide_preview(request: SearchRequest, preview: GuidePreview) -> SearchResponse:
    """業務ガイドの下書きで試す回答（#1288）。

    通常の検索・回答と同じ pipeline で 1 回だけ答える（モデルの呼び出しは通常の回答と同じ）。
    公開の版・保存した下書きは変えず、回答の記録には ``guide_preview`` の印を残す。
    """
    return await _run_search_with_timeout(request, guide_preview=preview)


async def _stream_search_events_with_timeout(
    resolved: tuple[SearchRequest, Settings, str | None, str | None],
    *,
    run_recorder: RunRecorder | None = None,
) -> AsyncIterator[str]:
    """処理の経過を即時 SSE で返しながら検索 pipeline を実行する。

    `resolved` は `_resolve_query_context` の結
    果（有効 request・Settings・適用 KB・検索・回答プロファイル）。

    最初に実行の id（`run`。trace_id）を送る。処理の経過は配信では送らず、画面が実行の trace の
    API（`GET /search/runs/{trace_id}/trace`。共通の span の表の snapshot。#3226）で読む。実行履歴
    には表を引く trace id と工程の詳細を残す。工程の開始・終了の `stage` は状態の 1 行（ナレッジベ
    ースの検索テスト）だけに使う。
    """
    request, settings, applied_kb, applied_view = resolved
    started_at = perf_counter()
    trace_id = new_trace_id()
    queue: asyncio.Queue[tuple[str, object] | None] = asyncio.Queue()
    queue.put_nowait(("run", {"trace_id": trace_id}))
    trace = AnswerTrace(trace_id, owner_user_uuid=current_audit_request_context().user_uuid)
    if run_recorder is not None:
        run_recorder.trace_source = trace.record
        run_recorder.usage_source = trace.model_usage

    async def emit_progress(stage: SearchStageProgress) -> None:
        await queue.put(
            (
                "stage",
                {
                    "trace_id": stage.trace_id,
                    "stage": stage.stage,
                    "outcome": stage.outcome,
                    "elapsed_ms": stage.elapsed_ms,
                    "occurred_at_ms": stage.occurred_at_ms,
                },
            )
        )

    async def produce() -> None:
        try:
            # 回答を LLM で生成するため、回答生成の上限で打ち切る（#375）。
            answer_model_id = _answer_model_id(request, settings)
            if run_recorder is not None:
                run_recorder.model_id = (
                    _run_model_id(answer_model_id, settings) if request.generate_answer else None
                )
            result = await run_answer_with_timeout(
                lambda tracker: RagPipeline(
                    settings=settings,
                    answer_model_id=answer_model_id,
                    run_recorder=run_recorder,
                    answer_notice=current_answer_notice,
                ).run(
                    request,
                    trace_id=trace_id,
                    progress_callback=tracker,
                    trace=trace,
                ),
                settings,
                emit_progress,
            )
            if applied_kb is not None:
                result.diagnostics.kb_adapter_config_applied = applied_kb
            if applied_view is not None:
                result.diagnostics.search_answer_profile_applied = applied_view
            await queue.put(("result", result))
        except AnswerTimeoutError as exc:
            elapsed = elapsed_ms(started_at)
            diagnostics = build_search_diagnostics(
                request, settings=settings, retrieval_strategy_adapter="grounded"
            )
            if applied_kb is not None:
                diagnostics.kb_adapter_config_applied = applied_kb
            if applied_view is not None:
                diagnostics.search_answer_profile_applied = applied_view
            record_rag_request(SEARCH_METRIC_MODE, "error", elapsed / 1000, 0)
            record_rag_search_audit(
                trace_id=trace_id,
                outcome="error",
                sanitized_query=request.query,
                filters=request.filters,
                findings=[],
                retrieved_count=0,
                citations=[],
                elapsed_ms=elapsed,
                diagnostics=diagnostics,
                error=exc.original_error,
                error_stage="timeout",
            )
            await _record_failed_run(run_recorder, request, trace_id, started_at, exc)
            await queue.put(
                (
                    "error",
                    {
                        "trace_id": trace_id,
                        "message": exc.user_message,
                        # 画面は error_type で 504 相当に揃える
                        # （`AnswerTimeoutError` は TimeoutError の派生）。
                        "error_type": "TimeoutError",
                        "stage": exc.stage,
                    },
                )
            )
        except Exception as exc:
            # stream は応答を始めた後なので共通の例外ハンドラーを通らない。原因を追えるよう
            # traceback をここで記録する。利用者へは、LLM の応答が未完了なら理由と直し方を、
            # それ以外は秘匿した定型文を返し、画面の「詳細」に出す項目（種類・工程・理由）を
            # 添える（#2023）。
            _log_answer_failure("rag_search_stream_failed", trace_id, exc, STREAM_ERROR_MESSAGE)
            await _record_failed_run(run_recorder, request, trace_id, started_at, exc)
            failure = describe_answer_failure(exc, STREAM_ERROR_MESSAGE)
            await queue.put(("error", {"trace_id": trace_id, **failure.event_fields()}))
        finally:
            await queue.put(None)

    producer = asyncio.create_task(produce())
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            event_name, payload = event
            if event_name == "result" and isinstance(payload, SearchResponse):
                async for item in _search_events(payload):
                    yield item
                continue
            yield _sse_event(event_name, payload)
    finally:
        if not producer.done():
            producer.cancel()
            with suppress(asyncio.CancelledError):
                await producer


async def _search_events(
    result: SearchResponse,
) -> AsyncIterator[str]:
    """検証済み SearchResponse を SSE イベント列へ変換する。"""
    diagnostics = result.diagnostics.model_dump(mode="json")
    yield _sse_event(
        "metadata",
        {
            "trace_id": result.trace_id,
            "elapsed_ms": result.elapsed_ms,
            "guardrail_warnings": result.guardrail_warnings,
            "diagnostics": diagnostics,
            # 回答の注意書き（#2405）。画面は回答の本文と根拠の後に出す。
            "notice": result.notice,
        },
    )
    for chunk in _answer_chunks(result.answer):
        yield _sse_event("delta", {"text": chunk})
    yield _sse_event(
        "citations",
        [citation.model_dump(mode="json") for citation in result.citations],
    )
    yield _sse_event("done", {"trace_id": result.trace_id})


def _answer_chunks(answer: str, chunk_size: int = 48) -> list[str]:
    """回答を UI が扱いやすい短い delta に分割する。"""
    if not answer:
        return [""]
    return [answer[index : index + chunk_size] for index in range(0, len(answer), chunk_size)]


def _sse_event(event: str, data: object) -> str:
    """SSE イベント文字列を生成する。"""
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


@router.get("/extraction-fields", response_model=ApiResponse[SearchExtractionFieldsData])
async def list_search_extraction_fields(
    search_answer_profile_id: Annotated[str, Query(min_length=1, max_length=128)],
) -> ApiResponse[SearchExtractionFieldsData]:
    """検索の絞り込みに使える項目を返す(#549)。

    選んだ検索・回答プロファイルの参照 KB のうち利用者が
    使える有効な KB の定義(KB に無ければ全体の既定)の

    和集合。同じ項目名は先の KB(作成の古い順)の定義を使う。存在しない検索・回答プロファイルは 404、
    アーカイブ済みは検索と同じく 409。
    """
    oracle = OracleClient()
    view = await oracle.get_search_answer_profile(search_answer_profile_id)
    if view is None:
        raise HTTPException(
            status_code=404,
            detail=f"{SEARCH_ANSWER_PROFILE_NOT_FOUND_PREFIX}{search_answer_profile_id}",
        )
    # 検索と同じく、アーカイブ済みの検索・回答プロファイルは項目を返さない（#961）。
    ensure_search_answer_profile_not_archived(view, search_answer_profile_id)
    # 利用者が使えない KB とアーカイブ済みの KB は Oracle の条件で除く。
    field_sets = await oracle.list_knowledge_base_extraction_field_sets(
        view.config.normalized_knowledge_base_ids()
    )
    fields = resolve_field_definitions(field_sets, load_field_schema().fields) if field_sets else []
    return ApiResponse(
        data=SearchExtractionFieldsData(
            fields=[FieldDefinitionData.model_validate(field.model_dump()) for field in fields]
        )
    )


# チャットが会話の回答を引き当てるときに一度に渡せる trace_id の上限。
ANSWER_TRACE_ID_FILTER_MAX = 100


@router.get("/answers", response_model=ApiResponse[Page[AnswerRecordSummary]])
async def list_saved_answers(
    paging: Annotated[
        OffsetParams, Depends(offset_params(default=DEFAULT_PAGE_LIMIT, max_limit=100))
    ],
    search_answer_profile_id: str | None = Query(default=None, max_length=128),
    trace_id: Annotated[list[str] | None, Query(max_length=ANSWER_TRACE_ID_FILTER_MAX)] = None,
) -> ApiResponse[Page[AnswerRecordSummary]]:
    """保存された回答(回答の記録)を新しい順に返す(検索・回答プロファイルで絞り込み可。総件数つき。#304)。

    持ち主の回答だけを返す（SYSTEM_ADMIN と `rag.feedback.manage` は全件）。`trace_id` を
    繰り返して渡すと、その回答だけにする（チャットが会話の回答の保存有無を引き当てる）。
    """
    trace_ids = _normalize_trace_id_filter(trace_id)
    oracle = OracleClient()
    rows = await oracle.list_answer_records(
        search_answer_profile_id=search_answer_profile_id,
        limit=paging.limit,
        offset=paging.offset,
        trace_ids=trace_ids,
    )
    total = await oracle.count_answer_records(
        search_answer_profile_id=search_answer_profile_id, trace_ids=trace_ids
    )
    return ApiResponse(
        data=paginate(
            [AnswerRecordSummary.model_validate(row) for row in rows],
            total=total,
            limit=paging.limit,
            offset=paging.offset,
        )
    )


def _normalize_trace_id_filter(values: list[str] | None) -> list[str] | None:
    """trace_id の絞り込みを空白除去・重複排除する。長すぎる ID は 422。"""
    if values is None:
        return None
    normalized: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 64:
            raise HTTPException(status_code=422, detail="trace_id の指定が正しくありません。")
        if cleaned not in normalized:
            normalized.append(cleaned)
    return normalized


@router.get("/answers/{trace_id}", response_model=ApiResponse[AnswerRecordDetail])
async def get_saved_answer(trace_id: str) -> ApiResponse[AnswerRecordDetail]:
    """保存された回答 1 件(回答・引用・根拠と実行記録)を返す。"""
    oracle = OracleClient()
    row = await oracle.get_answer_record(trace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="回答が見つかりません。")
    detail = _answer_record_detail(row)
    # 保存した引用と診断は、見る人の今の権限で確かめ直す（見られない根拠は数だけ。#3252）。
    citations, withheld, diagnostics = await withhold_saved_answer(
        oracle, detail.citations, detail.answer_diagnostics
    )
    return ApiResponse(
        data=detail.model_copy(
            update={
                "citations": citations,
                "withheld_evidence_count": withheld,
                "answer_diagnostics": diagnostics,
            }
        )
    )


@router.get("/answers/{trace_id}/trace", response_model=ApiResponse[TraceSnapshot])
async def get_saved_answer_trace(trace_id: str) -> ApiResponse[TraceSnapshot]:
    """保存された回答の処理の経過（共通の span の表の snapshot。#3226）。

    回答の記録と同じ判定で開き（範囲の外は 404）、記録の trace id で表を読む。保存した回答は完了の
    実行。
    """
    row = await OracleClient().get_answer_record(trace_id)
    if row is None:
        raise HTTPException(status_code=404, detail="回答が見つかりません。")
    diagnostics = row.get("diagnostics_json")
    saved = diagnostics.get("trace") if isinstance(diagnostics, Mapping) else None
    return ApiResponse(data=await trace_snapshot(saved, target_id=trace_id, terminal="done"))


@router.get("/runs/{run_id}/trace", response_model=ApiResponse[TraceSnapshot])
async def get_search_run_trace(run_id: str) -> ApiResponse[TraceSnapshot]:
    """RAG 検索の実行（`POST /search/stream` の `run` の trace_id）の処理の経過（#3226）。

    実行の記録（実行履歴）は実行の終わりに保存するので、記録があればその判定（実行履歴と同じ見え方）
    で開いて読む。まだ無い（実行中・実行履歴に残さない根拠の検索だけの実行）ときは、表を実行の id
    で引く（system_admin 以外は自分が実行した span だけ。他人の実行は空）。
    """
    oracle = OracleClient()
    row = await RunHistoryStore(oracle).get_run(run_id)
    if row is not None:
        return ApiResponse(data=await run_trace_snapshot(oracle, row))
    return ApiResponse(
        data=await trace_snapshot(None, target_id=run_id, terminal=None, lookup_operation_id=run_id)
    )


def _answer_record_detail(row: dict[str, object]) -> AnswerRecordDetail:
    return AnswerRecordDetail.model_validate(
        {
            **row,
            "citations": _stored_citation_rows(row.get("citations_json")),
            "answer_diagnostics": row.get("diagnostics_json") or {},
            "notice": row.get("answer_notice") or None,
        }
    )


def _stored_citation_rows(value: object) -> list[dict[str, object]]:
    """保存した引用を、保存するときと同じ項目だけにして返す（以前の記録も同じ形にする。#1371）。"""
    if not isinstance(value, list):
        return []
    return [
        {**item, "metadata": stored_citation_metadata(item["metadata"])}
        if isinstance(item, dict) and isinstance(item.get("metadata"), dict)
        else item
        for item in value
    ]
