"""図の根拠の元の画像をブラウザで開く、短命・対象に縛った署名つきの URL（#1311）。

Agent の画面（ブラウザ）は RAG の Cookie を持たないため、Agent の backend が MCP の
``rag_read_source``（``include_image_url``）で URL を受け取り、画面はその URL で図を開く。

署名・検証・期限・鍵の導き方は根拠のプレビュー（#2389）と共通の ``app.rag.signed_url``。
図のトークンの中身は ``sub``（利用者）・``document_id``・``chunk_id``・``chunk_set_id``（版）で、
用途の印は ``rag-figure-url-v1``（別の用途のトークンとしては使えない）。読み取りのたびに、署名に
加えて今の権限・版を確かめ直す（``app.api.routes.figures``）。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.rag.signed_url import (
    SIGNED_URL_LEEWAY_SECONDS,
    SIGNED_URL_MAX_TTL_SECONDS,
    SIGNED_URL_MIN_SECRET_LENGTH,
    SIGNED_URL_TTL_SECONDS,
    SignedTokenError,
    SignedUrlUnavailableError,
    issue_signed_token,
    optional_text,
    required_text,
    signing_key,
    verify_signed_token,
)

FIGURE_URL_TTL_SECONDS = SIGNED_URL_TTL_SECONDS
FIGURE_URL_MAX_TTL_SECONDS = SIGNED_URL_MAX_TTL_SECONDS
FIGURE_URL_LEEWAY_SECONDS = SIGNED_URL_LEEWAY_SECONDS
FIGURE_URL_MIN_SECRET_LENGTH = SIGNED_URL_MIN_SECRET_LENGTH
_TOKEN_VERSION = "f1"  # nosec B105 - トークンの形式の版で秘密ではない
_HKDF_INFO = b"rag-figure-url-v1"

# 図の URL の失敗は、共通の署名つきの URL の失敗と同じ型（呼び出し側は名前で見分ける）。
FigureUrlUnavailableError = SignedUrlUnavailableError
FigureTokenError = SignedTokenError


@dataclass(frozen=True, slots=True)
class FigureClaims:
    """署名を確かめたトークンの中身。"""

    subject: str
    document_id: str
    chunk_id: str
    chunk_set_id: str | None
    issued_at: int
    expires_at: int


def figure_signing_key(secret: str) -> bytes:
    """図の URL の署名の鍵（サービストークンの鍵から用途を区別して導く）。"""
    return signing_key(secret, info=_HKDF_INFO, unavailable_message="図を開く URL を作れません。")


def issue_figure_token(
    secret: str,
    *,
    subject: str,
    document_id: str,
    chunk_id: str,
    chunk_set_id: str | None,
    ttl_seconds: int = FIGURE_URL_TTL_SECONDS,
    now: float | None = None,
) -> tuple[str, int]:
    """利用者・根拠・版に縛ったトークンと、期限（epoch 秒）を返す。"""
    key = figure_signing_key(secret)
    if not subject or not document_id or not chunk_id:
        raise ValueError("subject・document_id・chunk_id は必須です。")
    return issue_signed_token(
        key,
        version=_TOKEN_VERSION,
        claims={"sub": subject, "doc": document_id, "chunk": chunk_id, "cs": chunk_set_id},
        ttl_seconds=ttl_seconds,
        now=now,
    )


def verify_figure_token(secret: str, token: str, *, now: float | None = None) -> FigureClaims:
    """署名・形式・期限を確かめて中身を返す。鍵が無ければ ``FigureUrlUnavailableError``。"""
    payload = verify_signed_token(
        figure_signing_key(secret), token, version=_TOKEN_VERSION, now=now
    )
    return FigureClaims(
        subject=required_text(payload, "sub"),
        document_id=required_text(payload, "doc"),
        chunk_id=required_text(payload, "chunk"),
        chunk_set_id=optional_text(payload, "cs"),
        issued_at=payload["iat"],
        expires_at=payload["exp"],
    )
