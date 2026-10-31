"""根拠 1 件の原文のプレビューをブラウザで開く、短命・対象に縛った署名つきの URL（#2389）。

Agent の画面（ブラウザ）は RAG の Cookie を持たないため、Agent の backend が MCP の
``rag_read_source``（``include_preview``）でプレビューの情報（種類・ページの一覧・ページの
画像と原本の URL・強調に要る根拠の位置）を受け取り、画面は共通のプレビューの部品でその URL を
開く。図の URL（#1311）と同じ署名の仕組み（``app.rag.signed_url``）で、用途の印は
``rag-source-preview-v1``。

- トークンの中身は ``sub``（利用者）・``doc``・``chunk``・``cs``（版）・``rcp``（処理
  レシピ）・``var``（原本か処理後のファイルか）。chunk に縛るのは、読み取りのたびに「その
  利用者に、その根拠が検索と同じ見え方の条件で見えるか」を確かめ直すため
  （``app.api.routes.source_previews``）。
- プレビューの種類は RAG の根拠のパネル（frontend の ``DocumentPreview``）と同じ決め方:
  拡張子で画像・PDF・テキストを分け、Office は処理後の PDF があればその PDF のページを出す。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.rag.signed_url import (
    SIGNED_URL_TTL_SECONDS,
    SignedTokenError,
    issue_signed_token,
    optional_text,
    required_text,
    signing_key,
    verify_signed_token,
)
from app.schemas.document import DocumentPreprocessArtifact

SOURCE_PREVIEW_TTL_SECONDS = SIGNED_URL_TTL_SECONDS
_TOKEN_VERSION = "p1"  # nosec B105 - トークンの形式の版で秘密ではない
_HKDF_INFO = b"rag-source-preview-v1"

PreviewKind = Literal["pdf", "image", "text", "unsupported"]
PreviewVariant = Literal["original", "prepared"]

# 拡張子 → プレビューの種類（frontend の DocumentPreview の kindOf と同じ）。
_IMAGE_EXTENSIONS = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp"})
_TEXT_EXTENSIONS = frozenset({"txt", "csv", "md", "json", "log", "html", "htm", "eml"})
_OFFICE_EXTENSIONS = frozenset({"doc", "docx", "ppt", "pptx", "xls", "xlsx"})


@dataclass(frozen=True, slots=True)
class SourcePreviewClaims:
    """署名を確かめたトークンの中身。"""

    subject: str
    document_id: str
    chunk_id: str
    chunk_set_id: str | None
    recipe_id: str | None
    variant: PreviewVariant
    issued_at: int
    expires_at: int


def source_preview_signing_key(secret: str) -> bytes:
    """プレビューの URL の署名の鍵（サービストークンの鍵から用途を区別して導く）。"""
    return signing_key(
        secret, info=_HKDF_INFO, unavailable_message="原文のプレビューの URL を作れません。"
    )


def issue_source_preview_token(
    secret: str,
    *,
    subject: str,
    document_id: str,
    chunk_id: str,
    chunk_set_id: str | None,
    recipe_id: str | None,
    variant: PreviewVariant,
    ttl_seconds: int = SOURCE_PREVIEW_TTL_SECONDS,
    now: float | None = None,
) -> tuple[str, int]:
    """利用者・根拠・版・ファイルに縛ったトークンと、期限（epoch 秒）を返す。"""
    key = source_preview_signing_key(secret)
    if not subject or not document_id or not chunk_id:
        raise ValueError("subject・document_id・chunk_id は必須です。")
    return issue_signed_token(
        key,
        version=_TOKEN_VERSION,
        claims={
            "sub": subject,
            "doc": document_id,
            "chunk": chunk_id,
            "cs": chunk_set_id,
            "rcp": recipe_id,
            "var": variant,
        },
        ttl_seconds=ttl_seconds,
        now=now,
    )


def verify_source_preview_token(
    secret: str, token: str, *, now: float | None = None
) -> SourcePreviewClaims:
    """署名・形式・期限を確かめて中身を返す。鍵が無ければ ``SignedUrlUnavailableError``。"""
    payload = verify_signed_token(
        source_preview_signing_key(secret), token, version=_TOKEN_VERSION, now=now
    )
    variant = payload.get("var")
    if variant not in ("original", "prepared"):
        raise SignedTokenError()
    return SourcePreviewClaims(
        subject=required_text(payload, "sub"),
        document_id=required_text(payload, "doc"),
        chunk_id=required_text(payload, "chunk"),
        chunk_set_id=optional_text(payload, "cs"),
        recipe_id=optional_text(payload, "rcp"),
        variant=variant,
        issued_at=payload["iat"],
        expires_at=payload["exp"],
    )


def is_prepared_pdf(artifact: DocumentPreprocessArtifact | None) -> bool:
    """処理後のファイルが PDF か（Office の原本は変換済みの PDF のページを出す）。"""
    if artifact is None or not artifact.object_storage_path:
        return False
    content_type = (artifact.content_type or "").split(";", 1)[0].strip().lower()
    if content_type:
        return content_type == "application/pdf"
    return artifact.file_name.lower().endswith(".pdf")


def preview_kind(
    file_name: str, prepared: DocumentPreprocessArtifact | None
) -> tuple[PreviewKind, PreviewVariant]:
    """プレビューの種類と、出すファイル（原本か処理後のファイルか）。"""
    extension = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    if extension in _IMAGE_EXTENSIONS:
        return "image", "original"
    if extension == "pdf":
        return "pdf", "original"
    if extension in _TEXT_EXTENSIONS:
        return "text", "original"
    if extension in _OFFICE_EXTENSIONS and is_prepared_pdf(prepared):
        return "pdf", "prepared"
    return "unsupported", "original"
