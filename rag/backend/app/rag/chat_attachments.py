"""チャットの添付（会話の期間だけ使う作業ファイル。#2517）。

保存・解析・見え方・期限は platform の `pr_system_settings.chat_attachments`。RAG が持つのは:

- 画像の読み取り（既定の Vision モデル。`EnterpriseAiClient.generate_from_image`）。
- 回答の根拠: 会話に送信した添付を、根拠を選ぶときに共通のサービスで解析し（アップロードでは
  解析しない。結果は保存して再利用する。#2652）、節・行を、質問との関連（rerank。使えなければ
  文字の一致）で選び、
  検索の根拠と同じ chunk（`RetrievedChunk`）にする。回答フローは検索の結果と並べて rerank・
  文脈・引用に使い、画面の根拠に添付のファイル名と節・頁・行が出る。ファイル全体はプロンプトに
  入れない。

添付の chunk の `document_id` は `attachment:<添付の ID>`（文書の表には無い）。根拠の原文の
プレビュー・文書のレシピ・フィードバックの文書は、metadata の `chat_attachment_id` で見分けて
開かない。
"""

from __future__ import annotations

import logging
import re
import threading
import unicodedata
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from anyio import to_thread
from pr_backend_core.observability.model_purpose import async_call_with_model_purpose
from pr_system_settings.chat_attachments import (
    IMAGE_READING_PROMPT,
    ChatAttachmentReference,
    ChatAttachmentService,
    ChatAttachmentToolError,
    ChatAttachmentToolset,
)
from pr_system_settings.chat_attachments.models import ChatAttachmentRecord, ParsedAttachment

from app.clients.oracle import StoredConversation
from app.config import enterprise_ai_vision_model_id, get_settings
from app.rag.degraded_steps import ReportDegraded, not_recorded
from app.schemas.search import RetrievedChunk

logger = logging.getLogger(__name__)

ATTACHMENT_DOCUMENT_PREFIX: Final = "attachment:"
# 引用の metadata の印（添付の ID）。保存した引用にも残す（`stored_answer`）。
ATTACHMENT_METADATA_KEY: Final = "chat_attachment_id"
# 1 つの根拠にする節の文字数と、表の行の数。
UNIT_CHARACTERS: Final = 1_500
ROWS_PER_UNIT: Final = 20
# 1 回の質問で根拠に入れる添付の箇所の数。添付が小さい（合計がこの文字数以下）ならすべて入れる。
MAX_ATTACHMENT_EVIDENCE: Final = 6
ALL_IN_CHARACTERS: Final = 6_000

# rerank（質問, 候補の文, 上位の数）→ [(候補の位置, 値)]。
RerankFunction = Callable[[str, list[str], int], Awaitable[list[tuple[int, float]]]]

_SERVICE: ChatAttachmentService | None = None
_SERVICE_LOCK = threading.Lock()


def _image_model_id() -> str | None:
    """画像を読み取る既定の Vision モデル（モデル・接続が無ければ None）。"""
    from pr_system_settings.model import enterprise_ai_connection_for_model

    settings = get_settings()
    model_id = enterprise_ai_vision_model_id(settings)
    if not model_id or not enterprise_ai_connection_for_model(settings, model_id).is_configured():
        return None
    return str(model_id)


def _image_reader() -> Callable[[bytes, str], Awaitable[str]] | None:
    """既定の Vision モデルで画像を文にする関数（Vision モデル・接続が無ければ None）。"""
    if not _image_model_id():
        return None

    async def read(data: bytes, media_type: str) -> str:
        from app.clients.enterprise_ai import EnterpriseAiClient

        return await async_call_with_model_purpose(
            "document.read",
            EnterpriseAiClient(settings=get_settings()).generate_from_image,
            data,
            IMAGE_READING_PROMPT,
            mime_type=media_type,
        )

    return read


def chat_attachment_service() -> ChatAttachmentService:
    """process で 1 つの添付のサービス（保存先は共通認証の store と同じ DB）。"""
    global _SERVICE
    if _SERVICE is None:
        with _SERVICE_LOCK:
            if _SERVICE is None:
                from app.security.service import get_security_service

                security = get_security_service()
                _SERVICE = ChatAttachmentService(
                    security.chat_attachment_store,
                    product="rag",
                    settings=get_settings,
                    image_reader=_image_reader,
                    image_model_id=_image_model_id,
                    # 運用設定 › チャットの添付（製品ごと。既定は無効）。
                    enabled=security.chat_attachment_settings.enabled,
                    settings_failure=security.chat_attachment_settings.failure,
                )
    return _SERVICE


def set_chat_attachment_service(service: ChatAttachmentService | None) -> None:
    """テスト用: service を差し替える（None で既定に戻す）。"""
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = service


def conversation_toolset(
    conversation_id: str, *, report_degraded: ReportDegraded = not_recorded
) -> ChatAttachmentToolset | None:
    """会話の添付（添付が無い・読めないときは None。回答は止めない）。

    読めなかったときは ``report_degraded`` に残す（添付が無いときは残さない。#3286）。
    """
    try:
        toolset = chat_attachment_service().toolset(conversation_id, observe=False)
    except Exception as exc:  # noqa: BLE001 - 添付を読めなくても回答は続ける
        logger.warning(
            "rag_chat_attachments_unavailable",
            extra={"conversation_id": conversation_id, "error_type": type(exc).__name__},
        )
        report_degraded("chat_attachments", exc)
        return None
    return toolset if toolset else None


def is_attachment_chunk(metadata: object) -> bool:
    """添付の根拠か（引用の metadata の印）。"""
    return isinstance(metadata, dict) and bool(metadata.get(ATTACHMENT_METADATA_KEY))


@dataclass(frozen=True)
class AttachmentUnit:
    """根拠の候補にする添付の 1 箇所（節の一部・表の行のまとまり）。"""

    record: ChatAttachmentRecord
    unit_id: str
    text: str
    section_title: str | None = None
    page: int | None = None
    sheet_name: str | None = None
    row_start: int | None = None
    row_end: int | None = None


def _split(text: str, size: int) -> list[str]:
    """段落の区切りで `size` 文字ほどに分ける（長い段落は途中で切る）。"""
    pieces: list[str] = []
    current = ""
    for paragraph in re.split(r"\n{2,}", text):
        while len(paragraph) > size:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(paragraph[:size])
            paragraph = paragraph[size:]
        if current and len(current) + len(paragraph) + 2 > size:
            pieces.append(current)
            current = ""
        current = f"{current}\n\n{paragraph}" if current else paragraph
    if current.strip():
        pieces.append(current)
    return pieces


def attachment_units(
    records: Sequence[ChatAttachmentRecord],
    parsed: Callable[[str], ParsedAttachment],
    *,
    report_degraded: ReportDegraded = not_recorded,
) -> list[AttachmentUnit]:
    """会話の添付を根拠の候補に分ける（節は `UNIT_CHARACTERS`、表は `ROWS_PER_UNIT` 行ずつ）。

    `parsed` は初めて読む添付を解析する（同期。event loop からは thread で呼ぶ）。読めない添付
    （解析中・失敗・期限切れ）は根拠に入れずに続け、``report_degraded`` に残す（#3286）。
    """
    units: list[AttachmentUnit] = []
    for record in records:
        try:
            content = parsed(record.attachment_id)
        except ChatAttachmentToolError as exc:
            logger.warning(
                "rag_chat_attachment_unreadable",
                extra={"attachment_id": record.attachment_id, "reason": str(exc)},
            )
            report_degraded("chat_attachments", exc)
            continue
        for section in content.sections:
            for index, piece in enumerate(_split(section.text, UNIT_CHARACTERS), start=1):
                units.append(
                    AttachmentUnit(
                        record=record,
                        unit_id=f"{section.section_id}.{index}",
                        text=piece,
                        section_title=section.title,
                        page=section.page,
                    )
                )
        for sheet_index, sheet in enumerate(content.sheets, start=1):
            for start in range(0, len(sheet.rows), ROWS_PER_UNIT):
                rows = sheet.rows[start : start + ROWS_PER_UNIT]
                lines = [
                    f"行 {start + offset + 1}: "
                    + " / ".join(
                        f"{column}: {value}"
                        for column, value in zip(sheet.columns, row, strict=False)
                        if value is not None
                    )
                    for offset, row in enumerate(rows)
                ]
                units.append(
                    AttachmentUnit(
                        record=record,
                        unit_id=f"t{sheet_index}.{start // ROWS_PER_UNIT + 1}",
                        text=f"シート「{sheet.sheet}」\n" + "\n".join(lines),
                        sheet_name=sheet.sheet,
                        row_start=start + 1,
                        row_end=start + len(rows),
                    )
                )
    return units


def _bigrams(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold())
    return {normalized[index : index + 2] for index in range(max(len(normalized) - 1, 0))}


def _lexical_scores(question: str, units: Sequence[AttachmentUnit]) -> list[float]:
    """文字の 2-gram の重なり（rerank が使えないときの並べ方）。"""
    wanted = _bigrams(question)
    if not wanted:
        return [0.0 for _ in units]
    return [len(wanted & _bigrams(unit.text)) / len(wanted) for unit in units]


async def select_attachment_units(
    question: str,
    units: Sequence[AttachmentUnit],
    *,
    rerank: RerankFunction | None,
    limit: int = MAX_ATTACHMENT_EVIDENCE,
    report_degraded: ReportDegraded = not_recorded,
) -> list[tuple[AttachmentUnit, float]]:
    """質問に関係する添付の箇所を選ぶ（小さな添付はすべて。rerank が失敗したら文字の一致）。

    rerank の失敗は ``report_degraded`` に残す（#3286）。
    """
    if not units:
        return []
    if sum(len(unit.text) for unit in units) <= ALL_IN_CHARACTERS and len(units) <= limit:
        return [(unit, 1.0) for unit in units]
    if rerank is not None:
        try:
            ranked = await rerank(question, [unit.text for unit in units], limit)
            return [(units[index], float(score)) for index, score in ranked[:limit]]
        except Exception as exc:  # noqa: BLE001 - rerank の失敗は文字の一致で続ける
            logger.warning(
                "rag_chat_attachments_rerank_failed", extra={"error_type": type(exc).__name__}
            )
            report_degraded("chat_attachment_rerank", exc)
    scores = _lexical_scores(question, units)
    order = sorted(range(len(units)), key=lambda index: (-scores[index], index))
    return [(units[index], scores[index]) for index in order[:limit]]


def attachment_chunk(unit: AttachmentUnit, score: float) -> RetrievedChunk:
    """添付の箇所を回答の根拠（検索の chunk と同じ形）にする。"""
    record = unit.record
    metadata: dict[str, str | int | None] = {
        ATTACHMENT_METADATA_KEY: record.attachment_id,
        "file_name": record.file_name,
    }
    if unit.section_title:
        metadata["section_title"] = unit.section_title
    if unit.page is not None:
        metadata["page_start"] = unit.page
        metadata["page_end"] = unit.page
    if unit.sheet_name is not None:
        metadata["sheet_name"] = unit.sheet_name
        metadata["row_start"] = unit.row_start
        metadata["row_end"] = unit.row_end
    return RetrievedChunk(
        document_id=f"{ATTACHMENT_DOCUMENT_PREFIX}{record.attachment_id}",
        chunk_id=f"{ATTACHMENT_DOCUMENT_PREFIX}{record.attachment_id}:{unit.unit_id}",
        text=unit.text,
        score=score,
        file_name=record.file_name,
        metadata=dict(metadata),
    )


async def attachment_evidence(
    question: str,
    conversation_id: str,
    *,
    rerank: RerankFunction | None,
    report_degraded: ReportDegraded = not_recorded,
) -> list[RetrievedChunk]:
    """会話の添付から、質問の根拠にする箇所（添付が無ければ空）。

    読めない添付と rerank の失敗は ``report_degraded`` に残す（回答の診断の degraded_steps。
    #3286）。
    """
    toolset = conversation_toolset(conversation_id, report_degraded=report_degraded)
    if toolset is None:
        return []
    try:
        units = await to_thread.run_sync(
            lambda: attachment_units(
                toolset.records, toolset.parsed, report_degraded=report_degraded
            )
        )
    except Exception as exc:  # noqa: BLE001 - 期限で消えた添付などは根拠に入れずに続ける
        logger.warning("rag_chat_attachments_unreadable", extra={"error_type": type(exc).__name__})
        report_degraded("chat_attachments", exc)
        return []
    selected = await select_attachment_units(
        question, units, rerank=rerank, report_degraded=report_degraded
    )
    return [attachment_chunk(unit, score) for unit, score in selected]


def attach_to_conversation(
    principal: Any, attachment_ids: Sequence[str], conversation: StoredConversation
) -> list[ChatAttachmentReference]:
    """質問の添付を会話に結び付け、質問に残す参照を返す。

    会話の権限（見えるか・持ち主だけが送れるか。#3244）は呼ぶ側が確かめるので、添付の持ち主は
    送った人（= 会話の持ち主）。
    """
    return chat_attachment_service().attach_to_conversation(
        principal,
        attachment_ids,
        conversation_id=conversation.id,
        owner_user_uuid=str(principal.user_uuid),
    )


def conversation_owner_for_attachments(principal: Any, conversation_id: str) -> str:
    """添付の API の会話の持ち主の UUID（見えない会話は 404。threadpool から呼ぶ）。

    会話は利用者を hash でしか持たないので、持ち主の UUID は呼んだ利用者の会話のときだけ分かる。
    他の利用者の会話・持ち主の分からない会話は空文字を返し、platform の添付の API が添付（内容を
    足す操作）を 403 にする（system_admin でも。#3244）。添付の一覧は閲覧なので見える会話なら返す。
    """
    from anyio import from_thread
    from fastapi import HTTPException

    from app.clients.oracle import OracleClient
    from app.rag.request_context import caller_owns

    conversation = from_thread.run(OracleClient().get_conversation, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="会話が見つかりません。")
    return str(principal.user_uuid) if caller_owns(conversation.user_id_hash) else ""


def delete_conversation_attachments(conversation_id: str) -> None:
    """会話の削除に合わせて添付も消す（消せなくても期限で消える）。"""
    try:
        chat_attachment_service().delete_conversation(conversation_id)
    except Exception as exc:  # noqa: BLE001 - 会話の削除は止めない
        logger.warning(
            "rag_conversation_attachments_delete_failed",
            extra={"conversation_id": conversation_id, "error_type": type(exc).__name__},
        )
