"""チャット(会話 / マルチモデル比較)API。

会話は検索・回答プロファイル(Search Answer Pro
file)配下に置く。メッセージ送信は既存 RAG パイプラインを

再利用し、会話履歴で省略を補って確認・検索・回答へ渡す。
``model_ids`` を複数指定すると設定済み OCI モデルへ fan-out し横並び比較できる。
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from time import perf_counter
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pr_backend_core.api import OffsetParams, empty_page, offset_params, paginate
from pr_backend_core.chat_history_window import (
    ChatHistoryPage,
    chat_history_cutoff,
    chat_history_days,
)
from pr_backend_core.logging import safe_exception_fields
from pr_system_settings.auth.visibility import (
    OTHERS_CONTENT_FORBIDDEN_MESSAGE,
    OTHERS_CONTENT_FORBIDDEN_STATUS,
)
from pr_system_settings.chat_attachments import ChatAttachmentReference
from starlette.concurrency import run_in_threadpool

from app.api.routes.search import (
    STREAM_ERROR_MESSAGE,
    _answer_chunks,
    _published_guides,
    _resolve_query_context,
    _sse_event,
    answer_model_choices,
    ensure_search_answer_profile_has_knowledge_bases,
    ensure_search_answer_profile_knowledge_bases_permitted,
    registered_answer_model_choices,
)
from app.clients.oci_enterprise_ai import OciEnterpriseAiClient
from app.clients.oracle import OracleClient, StoredConversation, StoredMessage
from app.clients.oracle_run_history import RunHistoryStore
from app.config import (
    Settings,
    enterprise_ai_default_model_id,
    get_settings,
)
from app.db_degradation import load_or_degrade
from app.rag.answer_engine import AnswerScope
from app.rag.answer_failure import describe_answer_failure
from app.rag.answer_notice import current_answer_notice
from app.rag.answer_timeout import AnswerTimeoutError, run_answer_with_timeout
from app.rag.chat_answer_runs import (
    CHAT_ANSWER_CANCELLED_MESSAGE,
    CHAT_ANSWER_INTERRUPTED_MESSAGE,
    CHAT_ANSWER_TIMEOUT_MARGIN_SECONDS,
    ChatAnswerLimitError,
    ChatAnswerRun,
    get_chat_answer_run_service,
)
from app.rag.chat_attachments import attach_to_conversation, delete_conversation_attachments
from app.rag.chat_question_token import (
    context_digest,
    issue_question_token,
    read_question_context,
    read_rule_selection_answers,
    read_rule_selection_elapsed,
)
from app.rag.document_sections_service import current_section_pages
from app.rag.guardrails import GuardrailPolicy, GuardrailResult
from app.rag.history_question import HistoryConfirmation
from app.rag.observability import elapsed_ms, new_trace_id
from app.rag.pipeline import (
    ChatTurn,
    RagPipeline,
    history_confirmation_facts,
    scope_with_history_confirmations,
    storage_failure,
)
from app.rag.question_understanding import profile_language_scope
from app.rag.rate_limit import enforce_rate_limit
from app.rag.request_context import (
    caller_owns,
    current_audit_request_context,
)
from app.rag.run_history import RunRecorder
from app.rag.saved_evidence import readable_document_ids, visible_citations
from app.rag.search_answer_profile_config import resolve_search_answer_profile_settings
from app.rag.search_answer_profile_knowledge import (
    clarification_document_ids,
    clarification_record,
    find_approved_faq,
    load_runtime_knowledge_payload,
    resolve_clarification,
)
from app.rag.signed_url import SignedUrlUnavailableError
from app.rag.stored_answer import passage_mark_record, stored_citation, stored_citations
from app.rag.support_guide_runtime import (
    GUIDE_CLARIFICATION_PREFIX,
    guide_clarification_record,
    resolve_guide_clarification,
)
from app.rag.trace_store import AnswerTrace, TraceSnapshot, TraceTerminal, trace_snapshot
from app.schemas.chat import (
    ChatAnswerCancelResult,
    ChatClarificationRecord,
    ChatMessage,
    ChatMessageRequest,
    ConversationCreateRequest,
    ConversationDetail,
    ConversationStatus,
    ConversationSummary,
    ConversationUpdateRequest,
    MessageRole,
    MessageStatus,
)
from app.schemas.common import ApiResponse, Page
from app.schemas.search import RetrievedChunk, SearchRequest, SearchResponse
from app.schemas.search_answer_profile_knowledge import ClarificationAnswer
from app.security.dependencies import current_principal
from app.security.user_names import display_names_by_user_hash

router = APIRouter()
logger = logging.getLogger(__name__)
# 業務ガイドを選ぶときに含める、前の利用者の発話の数（#1238）。
GUIDE_CONTEXT_TURNS = 3

CHAT_DISABLED_MESSAGE = "チャット機能は現在無効です。"
CONVERSATION_NOT_FOUND_MESSAGE = "会話が見つかりません。"
SEARCH_ANSWER_PROFILE_NOT_FOUND_MESSAGE = "検索・回答プロファイルが見つかりません。"
HISTORY_PROMPT_LIMIT = 40
BLOCKED_MESSAGE_PLACEHOLDER = "安全ポリシーにより内容を保存しませんでした。"
ANSWER_STREAM_NOT_FOUND_MESSAGE = (
    "回答の配信を再開できません。保存済みの会話から回答を取り直してください。"
)
DUPLICATE_MESSAGE_ID_MESSAGE = "同じ質問を既に送信しています。会話を読み込み直してください。"
# 回答は作ったが会話に保存できなかった回答（読み込み直したときに出す文。#2342）。
CHAT_ANSWER_NOT_SAVED_MESSAGE = (
    "回答は作成しましたが、会話に保存できなかったため表示できません。"
    "もう一度質問してください。続くときは、トレース ID を添えて管理者に連絡してください。"
)
ANSWER_NOT_FOUND_MESSAGE = "回答が見つかりません。会話を読み込み直してください。"


def _require_chat_enabled(settings: Settings) -> None:
    """チャット無効時は 404 にする(運用キルスイッチ)。"""
    if not settings.rag_chat_enabled:
        raise HTTPException(status_code=404, detail=CHAT_DISABLED_MESSAGE)


def _search_answer_profile_is_archived(view: object) -> bool:
    """テスト fake を含む検索・回答プロファイルの status を寛容に判定する。"""
    status = getattr(view, "status", None)
    return getattr(status, "value", status) == "ARCHIVED"


def _to_conversation_summary(
    conversation: StoredConversation, owner_names: dict[str, str] | None = None
) -> ConversationSummary:
    """会話の要約。

    system_admin が全員の会話を見るときは、本人の会話かと利用者の表示名を付ける（#1889）。
    `owned` が False の会話は閲覧と停止・削除だけで、続きの質問・名前の変更・評価は 403（#3244）。
    """
    context = current_audit_request_context()
    return ConversationSummary(
        id=conversation.id,
        search_answer_profile_id=conversation.search_answer_profile_id,
        title=conversation.title,
        status=ConversationStatus(conversation.status),
        message_count=conversation.message_count,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        owned=_owned_by_caller(conversation),
        actor_display_name=(
            (owner_names or {}).get(conversation.user_id_hash or "", "")
            if context.all_users_visible
            else ""
        ),
    )


def _owned_by_caller(conversation: StoredConversation) -> bool:
    """見ている利用者が作った会話か（持ち主の分からない会話は誰の会話でもない。`caller_owns`）。

    一般の利用者には持ち主の分からない会話は見えず（一覧・詳細の条件で絞る）、system_admin には
    閲覧と停止・削除だけの会話として出す（#3244）。
    """
    return caller_owns(conversation.user_id_hash)


def _ensure_caller_owns(conversation: StoredConversation) -> None:
    """会話に新しい内容を足すのは持ち主だけ（#3244。`can_add_user_content` と同じ規則）。

    続きの質問（確認への回答・添付を含む）・名前の変更が対象。system_admin でも他の利用者の
    会話には 403（見える会話なので 404 にしない。見えない会話は呼ぶ前に 404）。会話は利用者を
    hash でしか持たないので、応答の `owned` と同じ判定で比べる。
    """
    if not _owned_by_caller(conversation):
        raise HTTPException(
            status_code=OTHERS_CONTENT_FORBIDDEN_STATUS, detail=OTHERS_CONTENT_FORBIDDEN_MESSAGE
        )


async def _owner_names(conversations: list[StoredConversation]) -> dict[str, str]:
    """system_admin の一覧の利用者の表示名（`user_id_hash` → 表示名。#1889・#1696）。"""
    if not current_audit_request_context().all_users_visible:
        return {}
    hashes = {item.user_id_hash for item in conversations if item.user_id_hash}
    if not hashes:
        return {}
    return await run_in_threadpool(display_names_by_user_hash, hashes)


def _saved_citations(message: StoredMessage) -> list[RetrievedChunk]:
    """保存した引用（以前に保存した会話の回答も、保存するときと同じ項目だけにする。#1371）。"""
    citations: list[RetrievedChunk] = []
    for raw in message.citations:
        with suppress(Exception):
            citations.append(stored_citation(RetrievedChunk.model_validate(raw)))
    return citations


def _to_chat_message(
    message: StoredMessage,
    conversation: StoredConversation | None = None,
    sender_names: dict[str, str] | None = None,
    readable_documents: set[str] | None = None,
) -> ChatMessage:
    """保存したメッセージの応答の形。

    会話の持ち主以外（#3244 より前の system_admin）が送った質問には、送った人の表示名を添える。
    ``readable_documents`` を渡すと、引用をその文書（見る人が今見られる文書）だけにし、見られない
    根拠は数だけを返す（保存した根拠の再表示。#3252）。
    """
    sent_by_owner = _sent_by_owner(message, conversation)
    citations = _saved_citations(message)
    withheld = 0
    if readable_documents is not None:
        citations, withheld = visible_citations(citations, readable_documents)
    return ChatMessage(
        message_id=message.id,
        conversation_id=message.conversation_id,
        role=MessageRole(message.role),
        content=message.content,
        model=message.model,
        citations=citations,
        withheld_evidence_count=withheld,
        guardrail_warnings=message.guardrail_warnings,
        trace_id=message.trace_id,
        status=MessageStatus(message.status),
        reply_to_message_id=message.reply_to_message_id,
        created_at=message.created_at,
        clarification=_saved_clarification(message),
        # 回答に付けた注意書き（終わった回答だけ。#2405）。
        notice=message.answer_notice if message.status == "COMPLETE" else None,
        sent_by_owner=sent_by_owner,
        sender_display_name=(
            "" if sent_by_owner else (sender_names or {}).get(message.user_id_hash or "", "")
        ),
        attachments=_saved_attachments(message),
        # 回答のモデルへ添付した根拠の画像の件数（終わった回答だけ。#3444）。
        answer_attached_images=(
            message.answer_attached_images if message.status == "COMPLETE" else 0
        ),
        # 段落の印（終わった回答だけ。#2738）。契約の項目だけを返す。
        **(
            passage_mark_record(message.passage_record)
            if message.status == "COMPLETE" and message.passage_record
            else {}
        ),
    )


def _saved_attachments(message: StoredMessage) -> list[ChatAttachmentReference]:
    """質問に添付したファイル（#2517）。形の違う要素は捨てる（壊れた記録で会話を止めない）。"""
    references: list[ChatAttachmentReference] = []
    for raw in message.attachments or []:
        with suppress(Exception):
            references.append(ChatAttachmentReference.model_validate(raw))
    return references


def _sent_by_owner(message: StoredMessage, conversation: StoredConversation | None) -> bool:
    """会話の持ち主が送った質問か（回答・利用者の分からない古い行は持ち主の物とみなす。#1928）。"""
    if conversation is None or message.role != "USER":
        return True
    owner = conversation.user_id_hash
    sender = message.user_id_hash
    return owner is None or sender is None or owner == sender


async def _sender_names(
    conversation: StoredConversation, messages: list[StoredMessage]
) -> dict[str, str]:
    """持ち主以外が送った質問の、送った人の表示名（`user_id_hash` → 表示名。#1928）。"""
    hashes = {
        message.user_id_hash
        for message in messages
        if message.user_id_hash and not _sent_by_owner(message, conversation)
    }
    if not hashes:
        return {}
    return await run_in_threadpool(display_names_by_user_hash, hashes)


def _saved_clarification(message: StoredMessage) -> ChatClarificationRecord | None:
    """保存した回答前に確認した内容（#1839）。形の違う値は捨てる（会話の表示を止めない）。"""
    if message.clarification is None:
        return None
    with suppress(ValueError):
        return ChatClarificationRecord.model_validate(message.clarification)
    return None


def _guide_user_context(messages: list[StoredMessage]) -> list[str]:
    """利用者の発話と確認に答えた値を、ガイドの条件認識へ引き継ぐ。"""
    contexts: list[str] = []
    for message in [item for item in messages if item.role == "USER"][-GUIDE_CONTEXT_TURNS:]:
        lines = [message.content]
        record = _saved_clarification(message)
        if record is not None and record.outcome == "answered":
            for entry in record.entries:
                values = [*entry.selected, *([entry.other_text] if entry.other_text else [])]
                if values:
                    lines.append(f"確認の質問: {entry.prompt}\n利用者の答え: {'、'.join(values)}")
        contexts.append("\n".join(lines))
    return contexts


@router.get("/models", response_model=ApiResponse[list[dict[str, str]]])
async def list_compare_models() -> ApiResponse[list[dict[str, str]]]:
    """比較で選べるモデル(既定のテキストモデルと既定の Vision モデル。先頭が既定。#675)。"""
    settings = get_settings()
    _require_chat_enabled(settings)
    models = answer_model_choices(settings)
    return ApiResponse(
        data=models,
        warning_messages=(
            [] if models else ["生成モデルが未設定です。システム設定 > モデルで登録してください。"]
        ),
    )


@router.get("/conversations", response_model=ApiResponse[ChatHistoryPage[ConversationSummary]])
async def list_conversations(
    paging: Annotated[OffsetParams, Depends(offset_params(default=50, max_limit=200))],
    search_answer_profile_id: str | None = Query(default=None, max_length=128),
) -> ApiResponse[ChatHistoryPage[ConversationSummary]]:
    """会話一覧を返す。DB 停止時は空一覧 + warning で縮退する。

    system_admin は全員の会話（利用者の表示名つき）、それ以外は自分の会話だけ（#1889）。
    最後の更新が会話の履歴の期間（共通の設定 `PLATFORM_CHAT_HISTORY_DAYS`）の中の会話だけを、
    件数と 1 ページの両方で返す（期間の外の会話も `GET /conversations/{id}` では開ける。#2340）。
    """
    settings = get_settings()
    _require_chat_enabled(settings)
    oracle = OracleClient()
    history_days = chat_history_days(settings)
    updated_since = chat_history_cutoff(history_days)

    async def _load() -> Page[ConversationSummary]:
        items = await oracle.list_conversations(
            search_answer_profile_id=search_answer_profile_id,
            updated_since=updated_since,
            limit=paging.limit,
            offset=paging.offset,
        )
        total = await oracle.count_conversations(
            search_answer_profile_id=search_answer_profile_id, updated_since=updated_since
        )
        names = await _owner_names(items)
        return paginate(
            [_to_conversation_summary(item, names) for item in items],
            total=total,
            limit=paging.limit,
            offset=paging.offset,
        )

    fallback: Page[ConversationSummary] = empty_page(paging)
    page, degraded = await load_or_degrade(
        _load,
        timeout_seconds=settings.db_read_timeout_seconds,
        fallback=fallback,
        log_label="conversations_list",
    )
    return ApiResponse(
        data=ChatHistoryPage[ConversationSummary].from_page(page, history_days=history_days),
        warning_messages=[degraded.message] if degraded else [],
    )


@router.post("/conversations", response_model=ApiResponse[ConversationDetail])
async def create_conversation(
    request: ConversationCreateRequest,
) -> ApiResponse[ConversationDetail]:
    """検索・回答プロファイル配下に会話を作成する。"""
    settings = get_settings()
    _require_chat_enabled(settings)
    oracle = OracleClient()
    view = await oracle.get_search_answer_profile(request.search_answer_profile_id)
    if view is None:
        raise HTTPException(status_code=404, detail=SEARCH_ANSWER_PROFILE_NOT_FOUND_MESSAGE)
    if _search_answer_profile_is_archived(view):
        raise HTTPException(
            status_code=409,
            detail="アーカイブ済みの検索・回答プロファイルでは会話を作成できません。",
        )
    conversation = await oracle.create_conversation(
        search_answer_profile_id=request.search_answer_profile_id, title=request.title
    )
    detail = ConversationDetail(**_to_conversation_summary(conversation).model_dump(), messages=[])
    return ApiResponse(data=detail)


@router.get("/conversations/{conversation_id}", response_model=ApiResponse[ConversationDetail])
async def get_conversation(conversation_id: str) -> ApiResponse[ConversationDetail]:
    """会話詳細とメッセージ列を返す。

    system_admin は全員の会話、それ以外は自分の会話だけ（ほかの利用者の会話は 404。#1889）。
    """
    settings = get_settings()
    _require_chat_enabled(settings)
    oracle = OracleClient()
    conversation = await oracle.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail=CONVERSATION_NOT_FOUND_MESSAGE)
    messages = await oracle.list_messages(conversation_id)
    # 作成していたプロセスが止まった作成中の回答は、中断の失敗にしてから返す（#1175）。
    if await _close_interrupted_answers(oracle, messages):
        messages = await oracle.list_messages(conversation_id)
    sender_names = await _sender_names(conversation, messages)
    # 保存した引用は、見る人の今の権限で確かめ直す（会話のすべての回答を 1 回で。#3252）。
    readable = await readable_document_ids(
        oracle, [_saved_citations(message) for message in messages]
    )
    detail = ConversationDetail(
        **_to_conversation_summary(conversation, await _owner_names([conversation])).model_dump(),
        messages=[
            _to_chat_message(message, conversation, sender_names, readable) for message in messages
        ],
    )
    return ApiResponse(data=detail)


@router.patch("/conversations/{conversation_id}", response_model=ApiResponse[ConversationSummary])
async def rename_conversation(
    conversation_id: str,
    request: ConversationUpdateRequest,
) -> ApiResponse[ConversationSummary]:
    """会話タイトルを変更する（持ち主だけ。system_admin でも他の利用者の会話は 403。#3244）。"""
    settings = get_settings()
    _require_chat_enabled(settings)
    oracle = OracleClient()
    existing = await oracle.get_conversation(conversation_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=CONVERSATION_NOT_FOUND_MESSAGE)
    _ensure_caller_owns(existing)
    try:
        conversation = await oracle.rename_conversation(conversation_id, request.title)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=CONVERSATION_NOT_FOUND_MESSAGE) from exc
    return ApiResponse(
        data=_to_conversation_summary(conversation, await _owner_names([conversation]))
    )


@router.delete("/conversations/{conversation_id}", response_model=ApiResponse[None])
async def delete_conversation(conversation_id: str) -> ApiResponse[None]:
    """会話とそのメッセージを削除する（運用の操作。system_admin は他の利用者の会話も。#3244）。"""
    settings = get_settings()
    _require_chat_enabled(settings)
    try:
        await OracleClient().delete_conversation(conversation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=CONVERSATION_NOT_FOUND_MESSAGE) from exc
    # 会話の添付も消す（#2517。消せなくても期限で消える）。
    await run_in_threadpool(delete_conversation_attachments, conversation_id)
    return ApiResponse(data=None)


@router.post("/conversations/{conversation_id}/messages/stream")
async def stream_message(
    conversation_id: str,
    request: ChatMessageRequest,
    http_request: Request,
) -> StreamingResponse:
    """メッセージを送信し、回答(マルチモデルは N 系統)を SSE でストリーミングする。

    回答の作成は接続から切り離した task で進める（#1175）。接続が切れても作成は続き、最終の
    回答は会話のメッセージに保存される。画面は `GET .../messages/{質問の id}/stream` で続きを
    購読し直すか、保存済みの会話を取り直す。停止は `POST .../messages/{質問の id}/cancel`。
    """
    settings = get_settings()
    _require_chat_enabled(settings)
    enforce_rate_limit("search", http_request)
    oracle = OracleClient()
    conversation = await _load_sendable_conversation(oracle, conversation_id)
    context = current_audit_request_context()
    try:
        get_chat_answer_run_service().ensure_capacity(
            tenant_id_hash=context.tenant_id_hash,
            user_id_hash=context.user_id_hash,
            limit=settings.rag_chat_max_active_answers_per_user,
        )
    except ChatAnswerLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    # 質問の添付（#2517）を会話に結び付ける（見えない添付は 404・別の会話の添付と上限は 409）。
    attachments: list[ChatAttachmentReference] = []
    if request.attachment_ids:
        attachments = await run_in_threadpool(
            attach_to_conversation,
            current_principal(http_request),
            request.attachment_ids,
            conversation,
        )
    # 検索・回答プロファイルの解決・発話の検査・USER と作成中の ASSISTANT の保存は、応答を始める
    # 前に行う。応答を始めた後の HTTPException（409 / 404）や DB の例外は、利用者に理由を返せず
    # stream が途切れるため（#463）。
    turn = await _prepare_chat_turn(
        oracle,
        conversation_id,
        conversation.search_answer_profile_id,
        request,
        settings,
        attachments=attachments,
    )
    columns = _resolve_compare_models(request, turn.settings)
    run = await _start_answer_run(oracle, turn, columns)
    return StreamingResponse(
        _subscribe_events(run, after=0),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _load_sendable_conversation(
    oracle: OracleClient, conversation_id: str
) -> StoredConversation:
    """送信できる会話を返す(会話・検索・回答プロファイルの存在と状態、参照 KB の範囲を確認する)。

    送れるのは会話の持ち主だけ（system_admin でも他の利用者の会話は 403。#3244）。
    """
    conversation = await oracle.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail=CONVERSATION_NOT_FOUND_MESSAGE)
    _ensure_caller_owns(conversation)
    if conversation.status != "ACTIVE":
        raise HTTPException(status_code=409, detail="アーカイブ済みの会話には送信できません。")
    view = await oracle.get_search_answer_profile(conversation.search_answer_profile_id)
    if view is None:
        raise HTTPException(status_code=404, detail=SEARCH_ANSWER_PROFILE_NOT_FOUND_MESSAGE)
    if _search_answer_profile_is_archived(view):
        raise HTTPException(
            status_code=409,
            detail="アーカイブ済みの検索・回答プロファイルではチャットできません。",
        )
    # 検索・回答プロファイルの参照 KB が 0 件なら 409（
    # 全 KB を検索しない。#304）、1 つも利用できないなら
    #
    # 403（#214）。どちらも生成を始める前に HTTP の status で返す。
    profile_config = getattr(view, "config", None)
    if profile_config is not None:
        knowledge_base_ids = profile_config.normalized_knowledge_base_ids()
        ensure_search_answer_profile_has_knowledge_bases(knowledge_base_ids)
        ensure_search_answer_profile_knowledge_bases_permitted(knowledge_base_ids)
    return conversation


def _resolve_compare_models(
    request: ChatMessageRequest, settings: Settings
) -> list[dict[str, str]]:
    """比較対象の OCI モデル(model_id + label)を解決する。上限は設定値で抑える。"""
    catalog = {
        choice["model_id"]: choice["display_name"]
        for choice in registered_answer_model_choices(settings)
    }
    default_model = enterprise_ai_default_model_id(settings)
    if request.model_ids:
        selected = [
            {"model_id": model_id, "label": catalog.get(model_id, model_id)}
            for model_id in request.model_ids
            if model_id in catalog
        ]
    else:
        selected = []
    if not selected:
        label = catalog.get(default_model, default_model) or "既定モデル"
        selected = [{"model_id": default_model, "label": label}]
    return selected[: settings.rag_chat_max_compare_models]


async def _build_safe_history(
    messages: list[StoredMessage], guardrails: GuardrailPolicy
) -> list[ChatTurn]:
    """ブロック済みターンを除外し、旧メッセージも再検査して生成履歴を作る。"""
    turns: list[ChatTurn] = []
    seen_replies: set[str] = set()
    allowed_user_ids: set[str] = set()
    blocked_user_ids: set[str] = set()
    for message in messages[-HISTORY_PROMPT_LIMIT * 3 :]:
        if message.role == "USER":
            if message.status != "COMPLETE":
                blocked_user_ids.add(message.id)
                continue
            result = await asyncio.to_thread(guardrails.validate_query, message.content)
            if not result.allowed:
                blocked_user_ids.add(message.id)
                continue
            allowed_user_ids.add(message.id)
            # 確認への明示的な回答だけを利用者の履歴へ足す。スキップ・前提は足さない。
            content = _guide_user_context([message])[0]
            checked = (
                await asyncio.to_thread(guardrails.validate_query, content)
                if content != message.content
                else result
            )
            if checked.allowed:
                facts: list[HistoryConfirmation] = []
                resets: list[str] = []
                record = _saved_clarification(message)
                owner = current_audit_request_context().user_id_hash
                if (
                    record is not None
                    and record.outcome == "answered"
                    and message.user_id_hash in (None, owner)
                ):
                    for index, entry in enumerate(record.entries):
                        values = [
                            *entry.selected,
                            *([entry.other_text] if entry.other_text else []),
                        ]
                        if not values:
                            continue
                        prompt_check = await asyncio.to_thread(
                            guardrails.validate_query, entry.prompt
                        )
                        value_check = await asyncio.to_thread(
                            guardrails.validate_query, "、".join(values)
                        )
                        if prompt_check.allowed and value_check.allowed:
                            facts.append(
                                HistoryConfirmation(
                                    id=f"{message.id}:{index}",
                                    key=context_digest(entry.prompt),
                                    question=prompt_check.sanitized_text,
                                    answer=value_check.sanitized_text,
                                )
                            )
                if (
                    record is not None
                    and record.outcome == "skipped"
                    and message.user_id_hash in (None, owner)
                ):
                    resets = [context_digest(entry.prompt) for entry in record.entries]
                turns.append(
                    ChatTurn(
                        role="USER",
                        content=checked.sanitized_text,
                        confirmed_facts=tuple(facts),
                        confirmation_resets=tuple(resets),
                    )
                )
            else:
                allowed_user_ids.discard(message.id)
                blocked_user_ids.add(message.id)
        elif message.role == "ASSISTANT":
            if message.status != "COMPLETE":
                continue
            if message.reply_to_message_id in blocked_user_ids:
                continue
            if message.reply_to_message_id and message.reply_to_message_id not in allowed_user_ids:
                continue
            # 同一ユーザーターンに複数モデルの回答がある場合は先頭だけを履歴に使う。
            key = message.reply_to_message_id or message.id
            if key in seen_replies:
                continue
            result = await asyncio.to_thread(guardrails.validate_answer, message.content)
            if not result.allowed:
                continue
            seen_replies.add(key)
            turns.append(ChatTurn(role="ASSISTANT", content=result.sanitized_text))
    return turns[-HISTORY_PROMPT_LIMIT:]


async def prepare_chat_question(
    oracle: OracleClient,
    conversation_id: str | None,
    search_answer_profile_id: str,
    query: str,
    settings: Settings,
    *,
    token: str | None = None,
    confirmed_facts: list[HistoryConfirmation] | None = None,
    force_token: bool = False,
) -> tuple[str, str | None]:
    """現在の権限で文脈を読み、確認と検索が使う質問を一度だけ補完する。"""
    query = query.strip()
    messages: list[StoredMessage] = []
    owner: str | None = None
    if conversation_id:
        conversation = await oracle.get_conversation(conversation_id)
        if (
            conversation is None
            or conversation.search_answer_profile_id != search_answer_profile_id
        ):
            raise HTTPException(status_code=404, detail=CONVERSATION_NOT_FOUND_MESSAGE)
        _ensure_caller_owns(conversation)
        owner = conversation.user_id_hash
        messages = await oracle.list_messages(conversation_id)
    if not messages and not token and not force_token:
        return query, None
    view = await oracle.get_search_answer_profile(search_answer_profile_id)
    if view is not None:
        settings, _ = resolve_search_answer_profile_settings(settings, view.config)
    with profile_language_scope(settings) as language_pack:
        binding = {
            "conversation": conversation_id,
            "owner": owner,
            "profile": search_answer_profile_id,
            "query": context_digest(query),
            "revision": context_digest([asdict(message) for message in messages]),
            "policy": context_digest(
                [
                    settings.model_dump(mode="json"),
                    language_pack.hash,
                    view.model_dump(mode="json") if view else None,
                    await load_runtime_knowledge_payload(oracle, search_answer_profile_id),
                    await _published_guides(oracle, search_answer_profile_id),
                ]
            ),
        }
        guardrails = GuardrailPolicy(settings)
        checked = await asyncio.to_thread(guardrails.validate_query, query)
        if not checked.allowed:
            return query, None
        history = await _build_safe_history(messages, guardrails)
        facts = history_confirmation_facts(history)
        if token:
            try:
                question, selected = read_question_context(
                    settings.app_service_token_secret, token, binding
                )
                if any(identity not in facts for identity in selected):
                    raise ValueError("確認回答の参照が現在の履歴にありません。")
            except (ValueError, SignedUrlUnavailableError) as exc:
                raise HTTPException(
                    status_code=409,
                    detail="会話または設定が変わりました。質問を送信し直してください。",
                ) from exc
            if confirmed_facts is not None:
                confirmed_facts.extend(facts[identity] for identity in selected)
            return question, token
        question = checked.sanitized_text
        selected_facts: tuple[HistoryConfirmation, ...] = ()
        if history and settings.rag_history_rewrite_enabled:
            pipeline = RagPipeline(settings=settings, guardrails=guardrails)
            question = await pipeline._safe_rewritten_query(question, history) or question
            selected_facts = pipeline._selected_history_facts
        if confirmed_facts is not None:
            confirmed_facts.extend(selected_facts)
        # 署名鍵が未設定なら補完を共有せず、正式送信側で現在の履歴を読む。
        if not settings.app_service_token_secret:
            return question, None
        try:
            token = issue_question_token(
                settings.app_service_token_secret,
                binding,
                question,
                confirmed_fact_ids=tuple(fact.id for fact in selected_facts),
            )
        except SignedUrlUnavailableError:
            return question, None
        return question, token


@dataclass(frozen=True, slots=True)
class PreparedChatTurn:
    """保存済みのユーザー発話と、回答生成に使う有効 request / Settings / 履歴。"""

    conversation_id: str
    request: SearchRequest
    settings: Settings
    guardrails: GuardrailPolicy
    query_guardrail: GuardrailResult
    history: list[ChatTurn]
    user_message: StoredMessage
    # 利用者が選んだ類似の承認済み FAQ(質問・承認済みの回答。#684)。
    approved_faq: tuple[str, str] | None = None
    # 利用者が確認の質問で選んだ条件と対象範囲(#717)。
    scope: AnswerScope | None = None
    history_question_prepared: bool = False


async def _prepare_chat_turn(
    oracle: OracleClient,
    conversation_id: str,
    search_answer_profile_id: str,
    request: ChatMessageRequest,
    settings: Settings,
    *,
    attachments: list[ChatAttachmentReference] | None = None,
) -> PreparedChatTurn:
    """検索・回答プロファイルの設定を解決し、発話を検査して USER メッセージを保存する。"""
    approved_faq: tuple[str, str] | None = None
    if request.approved_faq_id:
        record = await find_approved_faq(oracle, search_answer_profile_id, request.approved_faq_id)
        if record is None:
            raise HTTPException(
                status_code=422,
                detail="選んだ類似問が見つかりません。もう一度送信して選び直してください。",
            )
        approved_faq = (record.question, record.approved_answer)
    guide_reply = request.clarification is not None and request.clarification.rule_id.startswith(
        GUIDE_CLARIFICATION_PREFIX
    )
    guide_skip = bool(request.clarification_skipped_rule_id) and str(
        request.clarification_skipped_rule_id
    ).startswith(GUIDE_CLARIFICATION_PREFIX)
    if guide_reply or guide_skip:
        view = await oracle.get_search_answer_profile(search_answer_profile_id)
        if view is None or view.config.query.support_guide_enabled is not True:
            # スイッチを切る前の確認への回答は、ガイドの条件や検索範囲へ反映しない。
            request = request.model_copy(
                update={
                    "clarification": None if guide_reply else request.clarification,
                    "clarification_skipped_rule_id": None
                    if guide_skip
                    else request.clarification_skipped_rule_id,
                }
            )
    scope: AnswerScope | None = None
    filters: dict[str, str] = {}
    conditions: dict[str, str] = {}
    # 会話に残す「回答前に確認した内容」（#1839）。答え・飛ばしたことを、確認事項と一緒に USER の
    # メッセージに保存する（選択肢の名前は答えた時点のもの）。
    clarification_log: dict[str, object] | None = None
    skipped_rule_id = request.clarification_skipped_rule_id
    if request.clarification is None and skipped_rule_id:
        if skipped_rule_id.startswith(GUIDE_CLARIFICATION_PREFIX):
            clarification_log = guide_clarification_record(
                await _published_guides(oracle, search_answer_profile_id), skipped_rule_id, None
            )
        else:
            clarification_log = clarification_record(
                await load_runtime_knowledge_payload(oracle, search_answer_profile_id),
                skipped_rule_id,
                None,
            )
    if request.clarification is not None and request.clarification.rule_id.startswith(
        GUIDE_CLARIFICATION_PREFIX
    ):
        guides = await _published_guides(oracle, search_answer_profile_id)
        guide_answer = resolve_guide_clarification(
            guides,
            request.clarification.rule_id,
            request.clarification.option_ids,
            request.clarification.other_text,
        )
        # 業務ガイドの条件の確認（#1238）。答えは既知の条件として回答に渡す。
        if guide_answer is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "選んだ確認の答えが見つかりません。業務ガイドが変わった可能性があります。"
                    "もう一度送信して選び直してください。"
                ),
            )
        conditions, context = guide_answer
        scope = AnswerScope(context=context, search_terms=(), label="")
        clarification_log = guide_clarification_record(
            guides,
            request.clarification.rule_id,
            request.clarification.option_ids,
            request.clarification.other_text,
        )
    elif request.clarification is not None:
        payload = await load_runtime_knowledge_payload(oracle, search_answer_profile_id)
        resolved = resolve_clarification(
            payload,
            request.clarification,
            await current_section_pages(
                oracle, clarification_document_ids(payload, request.clarification)
            ),
        )
        if resolved is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    "選んだ確認の答えが見つかりません。検索・回答プロファイルのルールが変わった可能性が"
                    "あります。もう一度送信して選び直してください。"
                ),
            )
        scope, page_ranges = resolved
        if page_ranges:
            filters["page_ranges"] = page_ranges
        clarification_log = clarification_record(
            payload, request.clarification.rule_id, request.clarification
        )
    inherited_facts: list[HistoryConfirmation] = []
    prepared_question, _ = (
        await prepare_chat_question(
            oracle,
            conversation_id,
            search_answer_profile_id,
            request.content,
            settings,
            token=request.question_context_token,
            confirmed_facts=inherited_facts,
        )
        if request.question_context_token
        else (request.content, None)
    )
    if request.question_context_token and request.clarification is not None:
        # token の準備後に新しく答えた条件を、以前の補完句と古い事実で上書きしない。
        prepared_question = request.content
        inherited_facts = []
    # 同輪の明示回答は履歴より優先する。設定文や展示用 context は読み取らない。
    if scope is not None and guide_reply and clarification_log is not None:
        current_confirmation = ChatClarificationRecord.model_validate(clarification_log)
        scope = replace(
            scope,
            confirmed_premises=tuple(
                (
                    entry.prompt,
                    "、".join([*entry.selected, *([entry.other_text] if entry.other_text else [])]),
                )
                for entry in current_confirmation.entries
                if entry.selected or entry.other_text
            ),
        )
    reset_questions: set[str] = set()
    if clarification_log is not None and clarification_log.get("outcome") == "skipped":
        reset_questions = {
            context_digest(entry.prompt)
            for entry in ChatClarificationRecord.model_validate(clarification_log).entries
        }
        inherited_facts = [
            fact for fact in inherited_facts if (fact.key or fact.question) not in reset_questions
        ]
    scope = scope_with_history_confirmations(scope, inherited_facts)
    base_request = SearchRequest(
        query=prepared_question,
        original_question=request.content if prepared_question != request.content else None,
        model_id=request.model_ids[0] if request.model_ids else None,
        top_k=request.top_k,
        search_answer_profile_id=search_answer_profile_id,
        filters=filters,
        conditions=conditions,
    )
    if request.question_context_token:
        base_request._rule_selection_state["prepared"] = True
        base_request._rule_selection_state["elapsed_seconds"] = read_rule_selection_elapsed(
            settings.app_service_token_secret, request.question_context_token
        )
    # 履歴は今回のユーザー発話を保存する前に読む(自分自身を含めない)。
    prior_messages = await oracle.list_messages(conversation_id)
    (
        effective_request,
        effective_settings,
        _applied_kb,
        _applied_view,
    ) = await _resolve_query_context(
        base_request,
        settings,
        # 業務ガイドは前の発話も含めて選ぶ（短い返答だけで照合が外れないように。#1238）。
        guide_context=()
        if request.question_context_token and settings.rag_history_rewrite_enabled
        else _guide_user_context(prior_messages),
        interactive=True,
        confirmed_rule_answers=tuple(
            ClarificationAnswer.model_validate(value)
            for value in read_rule_selection_answers(
                settings.app_service_token_secret, request.question_context_token
            )
        )
        if request.question_context_token
        else (),
        answered_rule_ids=(request.clarification.rule_id,)
        if request.clarification is not None
        and not request.clarification.rule_id.startswith(GUIDE_CLARIFICATION_PREFIX)
        else (),
    )
    guardrails = GuardrailPolicy(effective_settings)
    original_guardrail = await asyncio.to_thread(guardrails.validate_query, request.content)
    query_guardrail = (
        await asyncio.to_thread(guardrails.validate_query, prepared_question)
        if original_guardrail.allowed
        else original_guardrail
    )
    if request.client_message_id and any(
        message.id == request.client_message_id for message in prior_messages
    ):
        raise HTTPException(status_code=409, detail=DUPLICATE_MESSAGE_ID_MESSAGE)
    history = await _build_safe_history(prior_messages, guardrails)
    if reset_questions:
        history = [
            replace(
                turn,
                confirmed_facts=tuple(
                    fact
                    for fact in turn.confirmed_facts
                    if (fact.key or fact.question) not in reset_questions
                ),
            )
            for turn in history
        ]
    now = datetime.now(UTC)
    user_message = await oracle.append_message(
        StoredMessage(
            # 画面が決めた id（#1175）。送信の直後の停止をこの id で取り消せる。
            id=request.client_message_id or uuid4().hex,
            conversation_id=conversation_id,
            role="USER",
            content=(
                original_guardrail.sanitized_text
                if query_guardrail.allowed and prepared_question != request.content
                else query_guardrail.sanitized_text
                if query_guardrail.allowed
                else BLOCKED_MESSAGE_PLACEHOLDER
            ),
            guardrail_warnings=query_guardrail.warnings,
            status="COMPLETE" if query_guardrail.allowed else "ERROR",
            created_at=now,
            clarification=clarification_log,
            attachments=(
                [reference.model_dump(mode="json") for reference in attachments]
                if attachments
                else None
            ),
        )
    )
    return PreparedChatTurn(
        conversation_id=conversation_id,
        request=effective_request,
        settings=effective_settings,
        guardrails=guardrails,
        query_guardrail=query_guardrail,
        history=history,
        user_message=user_message,
        history_question_prepared=request.question_context_token is not None,
        approved_faq=approved_faq,
        scope=scope,
    )


def _chat_error_message(exc: Exception) -> str:
    """回答生成の失敗を利用者向けの文言にする(ERROR メッセージと SSE の error event)。

    LLM の応答が送り直しても未完了なら、理由と直し方の文にする（#2023）。それ以外は定型文。
    """
    if isinstance(exc, AnswerTimeoutError):
        return exc.user_message
    return describe_answer_failure(exc, STREAM_ERROR_MESSAGE).message


async def _generate_chat_answer(
    oracle: OracleClient,
    turn: PreparedChatTurn,
    model_id: str,
    message_id: str,
    *,
    lease_owner: str,
    trace: AnswerTrace | None = None,
) -> tuple[StoredMessage, SearchResponse]:
    """1 モデル分の回答を生成し、作成中の ASSISTANT メッセージに最終の状態を書く(#1175)。

    生成は回答生成の上限（`rag_answer_timeout_seconds`。#375）で打ち切る。時間切れは最後の工程を持
    つ `AnswerTimeoutError` になる。失敗したら ERROR を保存してから例外をそのまま送出する。保存は
    作成中（STREAMING）の行だけに当たる（利用者の停止・中断の後は書かない）。処理の経過（`trace`）
    は回答の span を共通の span の表に書き（#3226）、表を引く trace id と工程の詳細をメッセージに
    一緒に保存する。
    """
    trace_id = new_trace_id()
    if trace is not None:
        trace.run_id = trace_id
    # 実行履歴（#1890）。会話と回答のメッセージに結び付け、比較の列ごとに 1 件残す。
    run_recorder = RunRecorder(
        surface="chat",
        settings=turn.settings,
        store=RunHistoryStore(oracle),
        conversation_id=turn.conversation_id,
        message_id=message_id,
        # 列のモデル（未指定は実行のときの既定のテキストモデルの具体的な ID。#3419）。
        model_id=model_id or enterprise_ai_default_model_id(turn.settings) or None,
        # 実行履歴の詳細の処理の経過（会話の回答と同じ trace。#3226）とモデルの利用量（#3419）。
        trace_source=trace.record if trace is not None else None,
        usage_source=trace.model_usage if trace is not None else None,
    )
    started_at = perf_counter()
    try:
        llm = OciEnterpriseAiClient(settings=turn.settings, model_id=model_id or None)
        # llm は会話履歴による質問の書き換えに使う。回答は回答エンジンが自分でモデルを呼ぶので、
        # 列のモデルを別に渡す(#593)。
        pipeline = RagPipeline(
            settings=turn.settings,
            llm=llm,
            guardrails=turn.guardrails,
            answer_model_id=model_id or None,
            approved_faq=turn.approved_faq,
            scope=turn.scope,
            run_recorder=run_recorder,
            answer_notice=current_answer_notice,
            # 会話の添付（#2517）の節・行を根拠の候補に足す。
            attachment_conversation_id=turn.conversation_id,
            history_question_prepared=turn.history_question_prepared,
        )
        result = await run_answer_with_timeout(
            lambda progress_tracker: pipeline.run(
                turn.request,
                trace_id=trace_id,
                progress_callback=progress_tracker,
                history=turn.history,
                query_guardrail_result=turn.query_guardrail,
                # 処理の経過は回答の span（共通の span の表。#3226）。
                trace=trace,
            ),
            turn.settings,
        )
        assistant = StoredMessage(
            id=message_id,
            conversation_id=turn.conversation_id,
            reply_to_message_id=turn.user_message.id,
            role="ASSISTANT",
            model=model_id or None,
            content=result.answer,
            # 画面が使う項目だけを保存する（索引の内部の項目・親の本文を持たない。#1371）。
            citations=stored_citations(result.citations),
            guardrail_warnings=result.guardrail_warnings,
            trace_id=result.trace_id,
            status="COMPLETE",
            elapsed_ms=result.elapsed_ms,
            created_at=datetime.now(UTC),
            progress=trace.record() if trace is not None else None,
            # 回答の注意書き（#2405）。本文（会話の履歴に使う）とは別の列に保存する。
            answer_notice=result.notice,
            # 段落の印（#2738）。列には保存せず、回答の記録（diagnostics_json）から読み直す。
            passage_record=passage_mark_record(result.diagnostics.answer),
        )
    except Exception as exc:
        await run_recorder.record(
            run_id=trace_id,
            question=run_recorder.question,
            search_answer_profile_id=turn.request.search_answer_profile_id,
            answer_generated=turn.request.generate_answer,
            status="error",
            elapsed_ms=elapsed_ms(started_at),
            error=exc,
            error_stage=((exc.stage or "timeout") if isinstance(exc, AnswerTimeoutError) else None),
        )
        with suppress(Exception):
            await oracle.finish_chat_message(
                StoredMessage(
                    id=message_id,
                    conversation_id=turn.conversation_id,
                    reply_to_message_id=turn.user_message.id,
                    role="ASSISTANT",
                    model=model_id or None,
                    content=_chat_error_message(exc),
                    trace_id=trace_id,
                    status="ERROR",
                    created_at=datetime.now(UTC),
                    progress=trace.record() if trace is not None else None,
                ),
                lease_owner=lease_owner,
            )
        raise
    try:
        await oracle.finish_chat_message(assistant, lease_owner=lease_owner)
    except Exception as exc:
        # 回答は作った。保存できなくても利用者には回答を届け、保存できなかったことを示す（#2342）。
        # 以前は回答の生成の失敗として扱い、作った回答を捨てて定型の失敗の文にしていた。
        failure = storage_failure("chat_message_save", exc)
        logger.warning(
            "chat_answer_save_failed",
            extra={"trace_id": result.trace_id, **safe_exception_fields(exc)},
        )
        await run_recorder.add_storage_failure(failure)
        # 読み込み直したときに「作成中」のまま・定型の失敗の文にならないよう、理由の文で閉じる。
        with suppress(Exception):
            await oracle.finish_chat_message(
                StoredMessage(
                    id=message_id,
                    conversation_id=turn.conversation_id,
                    reply_to_message_id=turn.user_message.id,
                    role="ASSISTANT",
                    model=model_id or None,
                    content=CHAT_ANSWER_NOT_SAVED_MESSAGE,
                    trace_id=result.trace_id,
                    status="ERROR",
                    elapsed_ms=result.elapsed_ms,
                    created_at=datetime.now(UTC),
                    progress=trace.record() if trace is not None else None,
                ),
                lease_owner=lease_owner,
            )
        result = result.model_copy(
            update={"storage_failures": [*result.storage_failures, failure]}
        ).with_evaluation_input(result.evaluation_input)
    return assistant, result


# 段階の間に event が無い間（回答の作成など）も、この間隔で SSE のコメントを送る（#1160）。
# 画面は配信が途絶えた（接続が切れた・止まった）かを、届いたバイトで判定する。中継（Nginx など）の
# 無通信の打ち切りも防ぐ。
SSE_HEARTBEAT_SECONDS = 10.0
SSE_HEARTBEAT = ": keepalive\n\n"
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def _start_answer_run(
    oracle: OracleClient,
    turn: PreparedChatTurn,
    columns: list[dict[str, str]],
) -> ChatAnswerRun:
    """作成中の ASSISTANT メッセージを保存し、回答の作成を接続から切り離した task で始める(#1175)。

    `start` の event（保存済みの質問・比較の列・列ごとの作成中のメッセージ）を記録する。処理の経過
    は回答の配信では送らず、画面が回答ごとの trace の API（共通の span の表の snapshot。#3226）で
    読む。
    """
    service = get_chat_answer_run_service()
    context = current_audit_request_context()
    run = service.register(
        run_id=turn.user_message.id,
        conversation_id=turn.conversation_id,
        tenant_id_hash=context.tenant_id_hash,
        user_id_hash=context.user_id_hash,
        store=oracle,
    )
    # 処理の経過（共通の span の表に書く回答の span。対象は作成中のメッセージ。#3226）。
    traces: dict[str, AnswerTrace] = {}
    try:
        for column in columns:
            model_id = column["model_id"]
            trace = AnswerTrace(uuid4().hex, owner_user_uuid=context.user_uuid, model_id=model_id)
            placeholder = await oracle.append_message(
                StoredMessage(
                    id=trace.message_id,
                    conversation_id=turn.conversation_id,
                    reply_to_message_id=turn.user_message.id,
                    role="ASSISTANT",
                    model=model_id or None,
                    content="",
                    status="STREAMING",
                    created_at=datetime.now(UTC),
                    progress=trace.record(),
                    lease_owner=service.worker_id,
                )
            )
            run.message_ids[model_id] = placeholder.id
            run.progress[placeholder.id] = trace
            traces[model_id] = trace
    except Exception:
        service.discard(run)
        raise
    run.publish(
        "start",
        {
            "conversation_id": turn.conversation_id,
            "user_message": _to_chat_message(turn.user_message).model_dump(mode="json"),
            "columns": [
                {
                    "model_id": column["model_id"],
                    "label": column["label"],
                    "message_id": run.message_ids[column["model_id"]],
                }
                for column in columns
            ],
        },
    )

    async def execute(current: ChatAnswerRun) -> None:
        await _answer_turn(current, oracle, turn, columns, traces, service.worker_id)

    service.start(
        run,
        execute,
        timeout_seconds=turn.settings.rag_answer_timeout_seconds
        + CHAT_ANSWER_TIMEOUT_MARGIN_SECONDS,
    )
    return run


async def _answer_turn(
    run: ChatAnswerRun,
    oracle: OracleClient,
    turn: PreparedChatTurn,
    columns: list[dict[str, str]],
    traces: dict[str, AnswerTrace],
    lease_owner: str,
) -> None:
    """各モデルへ fan-out して回答を作り、event を記録する（購読の有無に関係なく最後まで進む）。"""

    async def run_model(column: dict[str, str]) -> None:
        model_id = column["model_id"]
        message_id = run.message_ids[model_id]
        trace = traces[model_id]
        persisted_version = trace.version
        persist_task: asyncio.Task[None] | None = None

        async def persist_progress() -> None:
            # 処理の経過の記録（trace id・工程の詳細）を保存する（画面は記録の trace id で span の
            # 表を読み、開いた段の詳細を記録から足す。再読込・別の worker からも作成中の段階を見せ
            # る）。保存は補助。知らせが続いても書き込みは 1 つずつにし、書いている間に増えた分は
            # 書き終わってからまとめて書く（古い記録で新しい記録を上書きしない）。
            nonlocal persisted_version
            while message_id not in run.closed_message_ids and trace.version > persisted_version:
                version = trace.version
                try:
                    await oracle.update_chat_message_progress(
                        message_id, lease_owner=lease_owner, progress=trace.record()
                    )
                except Exception as exc:  # noqa: BLE001 - 記録の保存に失敗しても作成は続ける
                    logger.warning(
                        "chat_answer_progress_save_failed",
                        extra={"run_id": run.run_id, "error_type": type(exc).__name__},
                    )
                    return
                persisted_version = version

        def schedule_persist() -> None:
            nonlocal persist_task
            if persist_task is None or persist_task.done():
                persist_task = asyncio.get_running_loop().create_task(persist_progress())

        trace.attach(on_change=schedule_persist)
        try:
            assistant, result = await _generate_chat_answer(
                oracle,
                turn,
                model_id,
                message_id,
                lease_owner=lease_owner,
                trace=trace,
            )
        except Exception as exc:
            run.mark_closed(message_id)
            # SSE では例外を error event へ落とす（処理の経過の終端はメッセージの状態で出す）。
            failure = describe_answer_failure(exc, STREAM_ERROR_MESSAGE)
            run.publish(
                "error",
                {
                    "model_id": model_id,
                    "message_id": message_id,
                    # 画面の「詳細」に出す（保存した ERROR の回答も trace_id を持つ。#2023）。
                    "trace_id": trace.run_id,
                    **({"reason": failure.reason} if failure.reason else {}),
                    "message": _chat_error_message(exc),
                    "error_type": failure.error_type if failure.incomplete else type(exc).__name__,
                    # 画面の「詳細」のエラーコード（ORA-03146 など。例外の本文は送らない。#2342）。
                    **({"error_code": failure.error_code} if failure.error_code else {}),
                    "occurred_at": datetime.now(UTC).isoformat(),
                    # 時間切れになった工程（#375）。画面は文言をそのまま出し、工程は記録に使う。
                    "stage": exc.stage if isinstance(exc, AnswerTimeoutError) else failure.stage,
                },
            )
            return
        run.mark_closed(message_id)
        run.publish(
            "metadata",
            {
                "model_id": model_id,
                "message_id": assistant.id,
                "trace_id": result.trace_id,
                "elapsed_ms": result.elapsed_ms,
                "guardrail_warnings": result.guardrail_warnings,
                # 回答エンジンの根拠・実行記録(無いときは None)。
                "answer_diagnostics": result.diagnostics.answer,
                # 作ったが保存できなかった記録（回答の記録・会話の回答。#2342）。
                "storage_failures": [item.model_dump() for item in result.storage_failures],
                # 回答の注意書き（#2405）。画面は回答の本文と根拠の後に出す。
                "notice": result.notice,
            },
        )
        for chunk in _answer_chunks(result.answer):
            run.publish("delta", {"model_id": model_id, "text": chunk})
        run.publish(
            "citations",
            {
                "model_id": model_id,
                "citations": [citation.model_dump(mode="json") for citation in result.citations],
            },
        )
        run.publish("done", {"model_id": model_id, "message_id": assistant.id})

    await asyncio.gather(*(run_model(column) for column in columns))


async def _subscribe_events(run: ChatAnswerRun, *, after: int) -> AsyncIterator[str]:
    """回答の作成の event を SSE で流す（`id:` は再購読で続きを指す連番）。

    接続が切れて generator が止まっても、作成の task は止めない(#1175)。
    """
    async for event in run.subscribe(after=after, heartbeat_seconds=SSE_HEARTBEAT_SECONDS):
        if event is None:
            yield SSE_HEARTBEAT
            continue
        yield f"id: {event.seq}\n" + _sse_event(event.name, event.payload)


def _last_event_id(header: str | None, after: int | None) -> int:
    """再購読の続きの位置（`after` を優先し、無ければ `Last-Event-ID`。読めなければ最初から）。"""
    if after is not None:
        return after
    if header is None:
        return 0
    try:
        return max(0, int(header.strip()))
    except ValueError:
        return 0


def _run_belongs_to_conversation(run: ChatAnswerRun, conversation_id: str) -> bool:
    """run が、この会話の（同じ tenant の）ものか。

    会話を開けるか（本人の会話か system_admin。#1889）は呼び出し側が会話を読んで確かめる。会話を
    開ける利用者は、誰が送った質問の回答でも購読し直し・停止できる（持ち主と system_admin。
    購読し直しは閲覧、停止は運用の操作。#3244）。
    """
    context = current_audit_request_context()
    return run.conversation_id == conversation_id and run.tenant_id_hash == context.tenant_id_hash


@router.get("/conversations/{conversation_id}/messages/{message_id}/stream")
async def resume_message_stream(
    conversation_id: str,
    message_id: str,
    after: int | None = Query(default=None, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """作成中の回答の配信を、続き（`Last-Event-ID` / `after` の次の event）から購読し直す(#1175)。

    `message_id` は質問（USER のメッセージ）の id。このプロセスで作成していない（別の worker・
    再起動の後・作成の記録を残す時間を過ぎた）ときは 404。画面は保存済みの会話を取り直す。
    """
    settings = get_settings()
    _require_chat_enabled(settings)
    oracle = OracleClient()
    if await oracle.get_conversation(conversation_id) is None:
        raise HTTPException(status_code=404, detail=CONVERSATION_NOT_FOUND_MESSAGE)
    run = get_chat_answer_run_service().get(message_id)
    if run is None or not _run_belongs_to_conversation(run, conversation_id):
        raise HTTPException(status_code=404, detail=ANSWER_STREAM_NOT_FOUND_MESSAGE)
    return StreamingResponse(
        _subscribe_events(run, after=_last_event_id(last_event_id, after)),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


@router.post(
    "/conversations/{conversation_id}/messages/{message_id}/cancel",
    response_model=ApiResponse[ChatAnswerCancelResult],
)
async def cancel_message_answer(
    conversation_id: str, message_id: str
) -> ApiResponse[ChatAnswerCancelResult]:
    """質問（`message_id`）への回答の作成を止め、作成中の回答を停止（CANCELLED）にする(#1175)。

    接続の切断では作成を止めないので、利用者の「停止」はこの API で取り消す。
    - このプロセスで作成中: task を止め、停止を保存するまで待つ。
    - 別のプロセスで作成中: 作成中の回答を停止にする（そのプロセスは次の heartbeat で止まる）。
    - まだ作成を始めていない（送信の直後）: 取消を覚えておき、始まったら停止として保存する。
    """
    settings = get_settings()
    _require_chat_enabled(settings)
    oracle = OracleClient()
    if await oracle.get_conversation(conversation_id) is None:
        raise HTTPException(status_code=404, detail=CONVERSATION_NOT_FOUND_MESSAGE)
    service = get_chat_answer_run_service()
    run = service.get(message_id)
    if run is not None and _run_belongs_to_conversation(run, conversation_id):
        if run.done:
            return ApiResponse(data=ChatAnswerCancelResult(cancelled=False))
        await service.cancel(run)
        return ApiResponse(data=ChatAnswerCancelResult(cancelled=True))
    messages = await oracle.list_messages(conversation_id)
    if not any(message.id == message_id for message in messages):
        service.remember_early_cancel(message_id, conversation_id)
        return ApiResponse(data=ChatAnswerCancelResult(cancelled=True))
    cancelled = False
    for message in messages:
        if (
            message.role == "ASSISTANT"
            and message.reply_to_message_id == message_id
            and message.status == "STREAMING"
        ):
            cancelled = (
                await oracle.close_streaming_chat_message(
                    message.id,
                    status="CANCELLED",
                    content=CHAT_ANSWER_CANCELLED_MESSAGE,
                )
                or cancelled
            )
    return ApiResponse(data=ChatAnswerCancelResult(cancelled=cancelled))


async def _close_interrupted_answers(oracle: OracleClient, messages: list[StoredMessage]) -> bool:
    """作成していたプロセスが止まった作成中の回答を、中断の失敗にする(#1175)。

    このプロセスで作成中のものは除く。このプロセスが作っていたのに動いていないもの（task が後始末
    の前に終わった）と、heartbeat が途絶えたもの（別のプロセスの停止・再起動）だけを、DB の時刻で
    判定して閉じる（永遠に作成中にしない）。処理の経過の終端はメッセージの状態で出す。閉じたら
    True。
    """
    service = get_chat_answer_run_service()
    closed = False
    for message in messages:
        if message.role != "ASSISTANT" or message.status != "STREAMING":
            continue
        if service.is_running(message.reply_to_message_id):
            continue
        try:
            closed = (
                await oracle.close_streaming_chat_message(
                    message.id,
                    status="ERROR",
                    content=CHAT_ANSWER_INTERRUPTED_MESSAGE,
                    lease_owner=service.worker_id,
                    stale_seconds=service.stale_seconds,
                )
                or closed
            )
        except Exception as exc:  # noqa: BLE001 - 判定できなければ作成中のまま返す
            logger.warning(
                "chat_answer_interrupt_check_failed", extra={"error_type": type(exc).__name__}
            )
    return closed


# --------------------------------------------------------------------------- # 回答の処理の経過（
# 共通の span の表の snapshot。#3226）
# --------------------------------------------------------------------------- #


async def _load_answer_message(
    oracle: OracleClient, conversation_id: str, message_id: str
) -> StoredMessage:
    """会話の回答（ASSISTANT のメッセージ）。

    会話・回答が無い・範囲の外なら 404（会話の取得と同じ判定）。
    """
    if await oracle.get_conversation(conversation_id) is None:
        raise HTTPException(status_code=404, detail=CONVERSATION_NOT_FOUND_MESSAGE)
    message = await oracle.get_chat_message(conversation_id, message_id)
    if message is None or message.role != "ASSISTANT":
        raise HTTPException(status_code=404, detail=ANSWER_NOT_FOUND_MESSAGE)
    return message


async def _close_if_interrupted(oracle: OracleClient, message: StoredMessage) -> StoredMessage:
    """作成していたプロセスが止まった作成中の回答なら中断の失敗にして、読み直した回答を返す。"""
    if message.status != "STREAMING":
        return message
    if not await _close_interrupted_answers(oracle, [message]):
        return message
    reloaded = await oracle.get_chat_message(message.conversation_id, message.id)
    return reloaded if reloaded is not None else message


_MESSAGE_TERMINAL: dict[str, TraceTerminal] = {
    "COMPLETE": "done",
    "ERROR": "failed",
    "CANCELLED": "cancelled",
}


@router.get(
    "/conversations/{conversation_id}/messages/{assistant_message_id}/trace",
    response_model=ApiResponse[TraceSnapshot],
)
async def get_answer_trace(
    conversation_id: str, assistant_message_id: str
) -> ApiResponse[TraceSnapshot]:
    """回答（`assistant_message_id`）の処理の経過（共通の span の表の snapshot）を返す。

    会話を開ける利用者だけ（会話の取得と同じ判定。他人の会話は 404）。回答のメッセージに保存した
    trace id で表を読み、開いた段の詳細はメッセージの記録から足す。終端はメッセージの状態（正本）。
    画面は作成中の間、読み直す。作成していたプロセスが止まった回答は、中断の失敗にしてから返す。
    """
    settings = get_settings()
    _require_chat_enabled(settings)
    oracle = OracleClient()
    message = await _load_answer_message(oracle, conversation_id, assistant_message_id)
    message = await _close_if_interrupted(oracle, message)
    snapshot = await trace_snapshot(
        message.progress,
        target_id=message.id,
        terminal=_MESSAGE_TERMINAL.get(message.status),
    )
    return ApiResponse(data=snapshot)
