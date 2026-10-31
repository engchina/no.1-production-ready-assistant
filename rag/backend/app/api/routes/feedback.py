"""回答・引用フィードバック API。"""

import asyncio
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pr_backend_core.api import OffsetParams, offset_params, paginate
from pr_system_settings.auth.visibility import (
    OTHERS_CONTENT_FORBIDDEN_MESSAGE,
    OTHERS_CONTENT_FORBIDDEN_STATUS,
)

from app.clients.oracle import OracleClient
from app.rag.evaluation_excel import XLSX_MEDIA_TYPE, evaluation_cases_xlsx, evidence_line
from app.rag.rate_limit import enforce_rate_limit
from app.rag.request_context import caller_owns, current_audit_request_context
from app.rag.search_answer_profile_knowledge import import_approved_faq
from app.rag.support_guide_runtime import is_guide_preview_trace
from app.schemas.common import ApiResponse
from app.schemas.evaluation import STANDARD_ANSWER_MAX_CHARS
from app.schemas.feedback import (
    CurrentFeedbackItem,
    FeedbackApprovedFaqPromotion,
    FeedbackCitationSnapshot,
    FeedbackContentSource,
    FeedbackDashboard,
    FeedbackDetail,
    FeedbackItem,
    FeedbackRating,
    FeedbackReason,
    FeedbackReasonCount,
    FeedbackRequest,
    FeedbackSortOrder,
    FeedbackSourceSurface,
    FeedbackSubmissionResponse,
    FeedbackSummary,
    FeedbackTargetType,
)

router = APIRouter()


@router.post("", response_model=ApiResponse[FeedbackSubmissionResponse])
async def submit_feedback(
    http_request: Request,
    request: FeedbackRequest,
) -> ApiResponse[FeedbackSubmissionResponse]:
    """回答または引用 feedback を追記する。"""
    enforce_rate_limit("search", http_request)
    if is_guide_preview_trace(request.trace_id):
        # 業務ガイドの下書きで試した回答は、利用者のフィードバックに混ぜない（#1288）。
        raise HTTPException(
            status_code=409,
            detail="業務ガイドの下書きで試した回答にはフィードバックを送れません。",
        )
    oracle = OracleClient()
    if await oracle.get_search_answer_profile(request.search_answer_profile_id) is None:
        raise HTTPException(status_code=404, detail="検索・回答プロファイルが見つかりません。")
    details = await _resolve_feedback_details(oracle, request)
    payload = request.model_dump(
        mode="json",
        exclude={"message_id", "content_snapshot", "comment", "corrected_answer"},
    )
    payload["comment_hash"] = request.comment_hash
    payload["comment_chars"] = request.comment_chars
    feedback_id = await oracle.save_feedback(payload, details=details)
    return ApiResponse(
        data=FeedbackSubmissionResponse(
            feedback_id=feedback_id,
            trace_id=request.trace_id,
            search_answer_profile_id=request.search_answer_profile_id,
            target_type=request.target_type,
            source_surface=request.source_surface,
            document_id=request.document_id,
            chunk_id=request.chunk_id,
            message_id=request.message_id,
            rating=request.rating,
            reason=request.reason,
            comment=request.comment,
            corrected_answer=request.corrected_answer,
        )
    )


@router.get("/current", response_model=ApiResponse[list[CurrentFeedbackItem]])
async def current_feedback(
    trace_id: str = Query(..., min_length=1, max_length=64),
) -> ApiResponse[list[CurrentFeedbackItem]]:
    """現在の利用者・trace の最新評価を返す。"""
    rows = await OracleClient().list_current_feedback(trace_id.strip())
    return ApiResponse(data=[CurrentFeedbackItem.model_validate(row) for row in rows])


@router.get("", response_model=ApiResponse[FeedbackDashboard])
async def list_feedback(
    paging: Annotated[OffsetParams, Depends(offset_params(default=50, max_limit=100))],
    search_answer_profile_id: str | None = Query(default=None, min_length=1, max_length=64),
    target_type: FeedbackTargetType | None = None,
    rating: FeedbackRating | None = None,
    reason: FeedbackReason | None = None,
    # 省略したときは期間で絞らない（全期間）。画面は「すべて」のとき送らない（#978）。
    period_days: int | None = Query(default=None, ge=1, le=3650),
    q: str | None = Query(default=None, max_length=200),
    sort_order: FeedbackSortOrder = FeedbackSortOrder.NEWEST,
) -> ApiResponse[FeedbackDashboard]:
    """有効な最新票を集計・一覧表示する。

    SYSTEM_ADMIN はすべての利用者の分、ほかのロールは自分が送った分だけ（#408）。一覧・件数・
    集計は Oracle の SQL の条件で同じ範囲に絞る（`OracleClient.list_feedback_dashboard_rows`）。
    """
    rows, total, groups, previous_groups = await OracleClient().list_feedback_dashboard_rows(
        search_answer_profile_id=search_answer_profile_id,
        target_type=target_type.value if target_type else None,
        rating=rating.value if rating else None,
        reason=reason.value if reason else None,
        period_days=period_days,
        search_query=q.strip() if q and q.strip() else None,
        sort_order=sort_order.value,
        limit=paging.limit,
        offset=paging.offset,
    )
    items = [FeedbackItem.model_validate(row) for row in rows]
    return ApiResponse(
        data=FeedbackDashboard(
            summary=_feedback_summary(groups),
            previous_summary=(
                _feedback_summary(previous_groups) if period_days is not None else None
            ),
            items=paginate(items, total=total, limit=paging.limit, offset=paging.offset),
        )
    )


@router.get("/{feedback_id}", response_model=ApiResponse[FeedbackDetail])
async def get_feedback_detail(
    feedback_id: str,
) -> ApiResponse[FeedbackDetail]:
    """feedback の本文・根拠・実行診断を返す（SYSTEM_ADMIN 以外は自分が送った分だけ。#408）。"""
    return ApiResponse(data=await _scoped_feedback_detail(OracleClient(), feedback_id))


@router.post(
    "/{feedback_id}/approved-faq", response_model=ApiResponse[FeedbackApprovedFaqPromotion]
)
async def promote_feedback_to_approved_faq(
    feedback_id: str,
) -> ApiResponse[FeedbackApprovedFaqPromotion]:
    """回答 feedback を検索・回答プロファイルの Approved FAQ へ登録する(rag_poc の FAQ 昇格)。

    「役に立った」は保存した回答、それ以外は修正した回答を登録する。同じ質問の FAQ は置き換える。
    """
    from rag_engine.knowledge.approved_faq import (
        APPROVED_FAQ_IMPORT_MODE_DELETE_THEN_INSERT,
        approved_faq_feedback_skip_reason,
        approved_faq_import_row_from_answer_feedback,
    )

    oracle = OracleClient()
    detail = await _promotable_feedback(oracle, feedback_id)
    record = await _answer_feedback_record(oracle, detail)
    row = approved_faq_import_row_from_answer_feedback(record)
    if row is None:
        raise HTTPException(status_code=409, detail=approved_faq_feedback_skip_reason(record))
    search_answer_profile_id = str(detail.search_answer_profile_id)
    if await oracle.get_search_answer_profile(search_answer_profile_id) is None:
        raise HTTPException(status_code=404, detail="検索・回答プロファイルが見つかりません。")
    result = await import_approved_faq(
        oracle, search_answer_profile_id, [row], mode=APPROVED_FAQ_IMPORT_MODE_DELETE_THEN_INSERT
    )
    return ApiResponse(
        data=FeedbackApprovedFaqPromotion(
            search_answer_profile_id=search_answer_profile_id,
            question=row.question,
            inserted_count=result.inserted_count,
            deleted_count=result.deleted_count,
        )
    )


@router.get("/{feedback_id}/evaluation-case")
async def feedback_evaluation_case(feedback_id: str) -> Response:
    """回答 feedback から品質評価のケースを作り、評価リクエストの Excel の 1 行で返す（#2065）。

    品質評価の画面のテンプレートと同じ形（質問・期待する根拠・標準回答）で、利用者は評価
    リクエストの Excel に行を写して使う。標準回答は修正した回答（「役に立った」は保存した回答）で、
    「役に立った」は引用の文書（ファイル名と、分かればページ）を期待する根拠にする。
    """
    detail = await _promotable_feedback(OracleClient(), feedback_id)
    helpful = detail.rating == FeedbackRating.HELPFUL
    expected_answer = ((detail.answer if helpful else detail.corrected_answer) or "").strip()
    if not detail.question or not expected_answer:
        raise HTTPException(
            status_code=409,
            detail="評価ケースにする回答がありません。修正した回答があるフィードバックを選んでください。",
        )
    evidence = (
        list(
            dict.fromkeys(
                evidence_line(citation.file_name, citation.page_number)
                for citation in detail.citations
                if citation.file_name
            )
        )
        if helpful
        else []
    )
    content = await asyncio.to_thread(
        evaluation_cases_xlsx,
        [(detail.question, evidence, expected_answer[:STANDARD_ANSWER_MAX_CHARS])],
    )
    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": (
                f'attachment; filename="evaluation_case_{_safe_file_part(detail.feedback_id)}.xlsx"'
            )
        },
    )


def _safe_file_part(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in value)[:64]


async def _scoped_feedback_detail(oracle: OracleClient, feedback_id: str) -> FeedbackDetail:
    """見える範囲の feedback を返す。ない ID は 404、他人の分は 403（#408）。

    詳細の SQL は送信者で絞る（SYSTEM_ADMIN 以外は自分の分だけ）。見つからなかったときだけ、
    送信者で絞らない存在の確認をして、他人の分（送信者のない古い行を含む）を 403 にする
    （NL2SQL のジョブ・フィードバックと同じ応答）。
    """
    cleaned_id = feedback_id.strip()
    if not cleaned_id or len(cleaned_id) > 64:
        raise HTTPException(status_code=404, detail="フィードバックが見つかりません。")
    row = await oracle.get_feedback_detail(cleaned_id)
    if row is not None:
        return FeedbackDetail.model_validate(row)
    if not current_audit_request_context().feedback_all_users and await oracle.feedback_exists(
        cleaned_id
    ):
        raise HTTPException(
            status_code=403, detail="他の利用者のフィードバックを参照する権限がありません。"
        )
    raise HTTPException(status_code=404, detail="フィードバックが見つかりません。")


async def _promotable_feedback(oracle: OracleClient, feedback_id: str) -> FeedbackDetail:
    detail = await _scoped_feedback_detail(oracle, feedback_id)
    if detail.target_type != FeedbackTargetType.ANSWER:
        raise HTTPException(status_code=409, detail="回答のフィードバックだけを昇格できます。")
    return detail


async def _answer_feedback_record(
    oracle: OracleClient, detail: FeedbackDetail
) -> dict[str, object]:
    """rag_poc の回答 feedback record の形へ写す(FAQ 昇格の変換・除外規則をそのまま使うため)。"""
    answer_trace: dict[str, object] = {
        "question": detail.question or "",
        "answer_text": detail.answer or "",
        "retrieval_scope": "knowledge_base",
    }
    # 回答フローの回答なら、除外規則(根拠不足かつ信頼度 low)に使う値を回答記録から補う。
    answer_record = await oracle.get_answer_record(detail.trace_id)
    diagnostics = answer_record.get("diagnostics_json") if answer_record else None
    if isinstance(diagnostics, Mapping):
        answer_trace["confidence"] = diagnostics.get("confidence") or ""
        answer_trace["insufficient_reason"] = diagnostics.get("insufficient_reason") or ""
    helpful = detail.rating == FeedbackRating.HELPFUL
    return {
        "feedback_id": detail.feedback_id,
        "feedback_type": "correct" if helpful else str(detail.reason or ""),
        "question": detail.question or "",
        "corrected_answer": "" if helpful else (detail.corrected_answer or ""),
        "comment": detail.comment or "",
        "answer_trace": answer_trace,
    }


async def _resolve_feedback_details(
    oracle: OracleClient,
    request: FeedbackRequest,
) -> dict[str, object] | None:
    """chat は server record、検索は画面の snapshot を保存する。

    検索の snapshot は画面が送る内容なので、trace の存在は確かめない
    （監査の保存先に依存させない。#457）。検索・回答プロファイルの利用範囲は呼び出し元が確かめ、
    一覧・詳細はフィードバックを送った本人の会話・監査だけを結び付ける。
    """
    details: dict[str, object] | None = None
    if request.message_id:
        details = await oracle.get_feedback_message_context(request.message_id, request.trace_id)
        if details is None:
            raise HTTPException(status_code=404, detail="評価対象のメッセージが見つかりません。")
        _ensure_conversation_owner(details.pop("conversation_user_id_hash", None))
        details["citations"] = _feedback_citations(details.get("citations"))
    elif request.source_surface == FeedbackSourceSurface.CHAT:
        # メッセージ ID の無いチャットの評価も、trace の回答の会話の持ち主だけ（#3244）。会話に無い
        # trace（None）は持ち主で絞らない。
        owner = await oracle.get_chat_answer_owner_hash(request.trace_id)
        if owner is not None:
            _ensure_conversation_owner(owner)
    elif request.content_snapshot is not None:
        details = {
            "message_id": None,
            "content_source": FeedbackContentSource.SEARCH_SNAPSHOT.value,
            "question_text": request.content_snapshot.question,
            "answer_text": request.content_snapshot.answer,
            "citations": [
                citation.model_dump(mode="json") for citation in request.content_snapshot.citations
            ],
        }
    elif request.comment is not None or request.corrected_answer is not None:
        details = {
            "message_id": None,
            "content_source": (
                FeedbackContentSource.CHAT_MESSAGE.value
                if request.source_surface.value == "chat"
                else FeedbackContentSource.SEARCH_SNAPSHOT.value
            ),
            "question_text": None,
            "answer_text": None,
            "citations": [],
        }

    if details is not None:
        details["comment_text"] = request.comment
        details["corrected_answer_text"] = request.corrected_answer
    return details


def _ensure_conversation_owner(owner_user_id_hash: object) -> None:
    """チャットの回答を評価するのは会話の持ち主だけ（system_admin でも他の利用者は 403。#3244）。

    持ち主の分からない会話の回答には誰も評価を足せない（チャットの `owned` と同じ `caller_owns`）。
    """
    owner = owner_user_id_hash if isinstance(owner_user_id_hash, str) else None
    if not caller_owns(owner):
        raise HTTPException(
            status_code=OTHERS_CONTENT_FORBIDDEN_STATUS, detail=OTHERS_CONTENT_FORBIDDEN_MESSAGE
        )


def _feedback_citations(value: object) -> list[dict[str, object]]:
    """StoredMessage の RetrievedChunk JSON を小さな feedback snapshot へ落とす。"""
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    snapshots: list[dict[str, object]] = []
    for item in value[:50]:
        if not isinstance(item, Mapping):
            continue
        metadata = item.get("metadata")
        metadata_map = metadata if isinstance(metadata, Mapping) else {}
        raw = {
            "document_id": item.get("document_id"),
            "chunk_id": item.get("chunk_id"),
            "file_name": item.get("file_name") or metadata_map.get("file_name"),
            "section_title": metadata_map.get("section_title"),
            "page_number": metadata_map.get("page_number") or metadata_map.get("page"),
            "content_preview": str(item.get("text") or item.get("content") or "")[:2000] or None,
            "rerank_score": item.get("rerank_score"),
        }
        try:
            snapshots.append(FeedbackCitationSnapshot.model_validate(raw).model_dump(mode="json"))
        except ValueError:
            continue
    return snapshots


def _feedback_summary(groups: list[dict[str, object]]) -> FeedbackSummary:
    total = helpful = answer_total = answer_helpful = citation_total = citation_helpful = 0
    reasons: dict[FeedbackReason, int] = {}
    for group in groups:
        raw_count = group.get("item_count")
        count = int(raw_count) if isinstance(raw_count, int | float | str | Decimal) else 0
        target = str(group.get("target_type") or "")
        rating = str(group.get("rating") or "")
        total += count
        if rating == FeedbackRating.HELPFUL:
            helpful += count
        if target == FeedbackTargetType.ANSWER:
            answer_total += count
            if rating == FeedbackRating.HELPFUL:
                answer_helpful += count
        elif target == FeedbackTargetType.CITATION:
            citation_total += count
            if rating == FeedbackRating.HELPFUL:
                citation_helpful += count
        raw_reason = group.get("reason")
        if raw_reason:
            reason_key = FeedbackReason(str(raw_reason))
            reasons[reason_key] = reasons.get(reason_key, 0) + count

    return FeedbackSummary(
        total=total,
        helpful_count=helpful,
        not_helpful_count=total - helpful,
        helpful_rate=_rate(helpful, total),
        answer_total=answer_total,
        answer_helpful_rate=_rate(answer_helpful, answer_total),
        citation_total=citation_total,
        citation_helpful_rate=_rate(citation_helpful, citation_total),
        reason_counts=[
            FeedbackReasonCount(reason=reason, count=count)
            for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0].value))
        ],
    )


def _rate(helpful: int, total: int) -> float:
    return round(helpful / total, 4) if total else 0.0
