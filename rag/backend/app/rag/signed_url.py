"""ブラウザが開く短命・対象に縛った署名つきの URL のトークン（図 #1311・原文のプレビュー #2389）。

Agent の画面（ブラウザ）は RAG の Cookie を持たないため、Agent の backend が MCP の
``rag_read_source`` で URL を受け取り、画面はその URL で図・原文のページを開く。用途ごとの
中身は ``figure_url`` と ``source_preview`` が決め、署名・検証はこの module が共通に行う。

- トークンは HMAC-SHA256 の署名（stateless）。プロセスのメモリに置かないので、複数の
  worker・再起動でも検証できる。中身は用途ごとの対象（利用者・文書・chunk・版など）と
  ``iat``・``exp``（発行から 5 分以内）。
- 署名の鍵は共通 ``.env`` の ``PLATFORM_SERVICE_TOKEN_SECRET`` から HKDF-SHA256 で用途を
  区別して導く（用途の印 ``info`` は用途ごとに別）。サービストークンの鍵そのものでは署名
  しないので、URL のトークンをサービストークンとして使うこと（またはその逆）も、別の用途の
  トークンとして使うこともできない。鍵が無い・短いときは URL を作らず、読み取りも拒否する
  （fail closed）。
- トークンは URL の path に置く（query に載せない）。読み取りのたびに、署名に加えて今の
  権限・版を確かめ直す（``app.api.routes.figures``・``app.api.routes.source_previews``）。
  トークンは「読んでよい対象」を縛るだけで、権限は持たない。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any

# URL の有効期限（秒）。画面が開く直前に作るので短くてよい。上限も同じ 5 分。
SIGNED_URL_TTL_SECONDS = 300
SIGNED_URL_MAX_TTL_SECONDS = 300
# 発行時刻が未来に寄っているときに許す秒数（同じ RAG の worker 間の時計のずれ）。
SIGNED_URL_LEEWAY_SECONDS = 30
SIGNED_URL_MIN_SECRET_LENGTH = 32
_HKDF_SALT = b"production-ready-rag"
# 用途ごとの中身に使えない（共通の）項目。
_RESERVED_CLAIMS = frozenset({"iat", "exp"})


class SignedUrlUnavailableError(RuntimeError):
    """署名の鍵が設定されていない（URL を作らない・読ませない）。"""


class SignedTokenError(ValueError):
    """トークンが無効（改ざん・形式の誤り・別の用途）か、期限切れ。"""

    def __init__(self, *, expired: bool = False) -> None:
        super().__init__("signed token expired" if expired else "signed token invalid")
        self.expired = expired


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _hkdf_sha256(secret: bytes, *, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """RFC 5869 の HKDF-SHA256（標準ライブラリだけで書く。長さは 1 ブロック分まで）。"""
    if not 0 < length <= hashlib.sha256().digest_size:
        raise ValueError("length は 1〜32 にしてください。")
    pseudo_random_key = hmac.new(salt, secret, hashlib.sha256).digest()
    return hmac.new(pseudo_random_key, info + b"\x01", hashlib.sha256).digest()[:length]


def signing_key(secret: str, *, info: bytes, unavailable_message: str) -> bytes:
    """用途 ``info`` の署名の鍵（サービストークンの鍵から用途を区別して導く）。"""
    secret = secret.strip()
    if len(secret) < SIGNED_URL_MIN_SECRET_LENGTH:
        raise SignedUrlUnavailableError(
            f"{unavailable_message}共通 .env の PLATFORM_SERVICE_TOKEN_SECRET に"
            f" {SIGNED_URL_MIN_SECRET_LENGTH} 文字以上のランダムな値を設定してください。"
        )
    return _hkdf_sha256(secret.encode("utf-8"), salt=_HKDF_SALT, info=info)


def _signature(key: bytes, signing_input: str) -> str:
    return _b64url_encode(hmac.new(key, signing_input.encode("ascii"), hashlib.sha256).digest())


def issue_signed_token(
    key: bytes,
    *,
    version: str,
    claims: Mapping[str, Any],
    ttl_seconds: int = SIGNED_URL_TTL_SECONDS,
    now: float | None = None,
) -> tuple[str, int]:
    """用途の対象 ``claims`` に縛ったトークンと、期限（epoch 秒）を返す。"""
    if not 0 < ttl_seconds <= SIGNED_URL_MAX_TTL_SECONDS:
        raise ValueError(f"ttl_seconds は 1〜{SIGNED_URL_MAX_TTL_SECONDS} 秒にしてください。")
    if _RESERVED_CLAIMS & set(claims):
        raise ValueError("iat・exp は claims に入れられません。")
    issued_at = int(now if now is not None else time.time())
    expires_at = issued_at + ttl_seconds
    payload = {**claims, "iat": issued_at, "exp": expires_at}
    body = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signing_input = f"{version}.{body}"
    return f"{signing_input}.{_signature(key, signing_input)}", expires_at


def verify_signed_token(
    key: bytes, token: str, *, version: str, now: float | None = None
) -> dict[str, Any]:
    """署名・形式・期限を確かめて中身（``iat``・``exp`` を含む）を返す。"""
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != version:
        raise SignedTokenError()
    _, body, signature = parts
    expected = _signature(key, f"{version}.{body}")
    if not hmac.compare_digest(signature.encode("ascii", "ignore"), expected.encode("ascii")):
        raise SignedTokenError()
    try:
        payload = json.loads(_b64url_decode(body))
    except (ValueError, UnicodeDecodeError, binascii.Error):
        raise SignedTokenError() from None
    if not isinstance(payload, dict):
        raise SignedTokenError()
    issued_at, expires_at = payload.get("iat"), payload.get("exp")
    if not isinstance(issued_at, int) or not isinstance(expires_at, int):
        raise SignedTokenError()
    if isinstance(issued_at, bool) or isinstance(expires_at, bool):
        raise SignedTokenError()
    current = now if now is not None else time.time()
    if expires_at - issued_at > SIGNED_URL_MAX_TTL_SECONDS or expires_at <= issued_at:
        raise SignedTokenError()
    if issued_at > current + SIGNED_URL_LEEWAY_SECONDS:
        raise SignedTokenError()
    if expires_at <= current:
        raise SignedTokenError(expired=True)
    return payload


def required_text(payload: Mapping[str, Any], key: str) -> str:
    """中身の必須の文字列（無い・空・文字列でなければ無効なトークン）。"""
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise SignedTokenError()
    return value


def optional_text(payload: Mapping[str, Any], key: str) -> str | None:
    """中身の任意の文字列（無ければ None。文字列でなければ無効なトークン）。"""
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise SignedTokenError()
    return value
