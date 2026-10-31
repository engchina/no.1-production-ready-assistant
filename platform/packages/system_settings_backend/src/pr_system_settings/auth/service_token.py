"""サービス間の呼び出しに使う短命の署名付き token（#230）。

Agent が RAG / NL2SQL の MCP を「Run の利用者として」呼ぶために使う。形式は HS256 の JWT
（`sub` = `user_uuid`、`aud` = 呼び先の製品、`exp` = 短命）。鍵は 3 製品共通の
`PLATFORM_SERVICE_TOKEN_SECRET`。ユーザーは `PLATFORM_USERS` で共有しているので、呼び先は
`sub` から自分の製品の権限・対象範囲をそのまま組み立てられる。

呼び出し元の用途による範囲（#1379）: 任意の claim `profile_ids` に、その呼び出しで使ってよい
プロファイル（RAG: 検索・回答プロファイル、NL2SQL: 業務プロファイル）の ID を入れる。Agent の
業務 Agent の定義で範囲を設定したときだけ付け、未設定なら付けない（範囲なし = 利用者の権限だけ）。
呼び先は「利用者の権限 ∩ claim」で判定する（`profile_scope_from_claims` /
`narrow_to_profile_scope`）。

ponytail: 依存を増やさないため標準ライブラリで HS256 だけを実装する。鍵を持つ製品は任意の
利用者の token を作れる（3 製品は DB の資格情報も共有する同じ信頼境界）。製品ごとに鍵を分ける
必要が出たら、`aud` ごとの鍵に変える。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from .errors import SecurityApiError

SERVICE_TOKEN_DEFAULT_TTL_SECONDS = 60
# 呼び出し元と呼び先の時計のずれを許す秒数。
SERVICE_TOKEN_LEEWAY_SECONDS = 30
# token の有効期限の上限。これより長い token は受け付けない（漏れたときの影響を抑える）。
SERVICE_TOKEN_MAX_TTL_SECONDS = 600
SERVICE_TOKEN_MIN_SECRET_LENGTH = 32
_HEADER = {"alg": "HS256", "typ": "JWT"}
# 呼び出しで使ってよいプロファイルの ID の claim（#1379）。無ければ範囲なし。
PROFILE_SCOPE_CLAIM = "profile_ids"
# claim に入れられるプロファイルの数と ID の長さの上限（Agent の範囲の上限にそろえる）。
PROFILE_SCOPE_MAX_IDS = 50
PROFILE_SCOPE_MAX_ID_CHARS = 128
# claim の範囲の外のプロファイルを指定した（または指定が要るのに無い）呼び出しの拒否（403）。
PROFILE_SCOPE_FORBIDDEN_CODE = "PROFILE_SCOPE_FORBIDDEN"


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _segment(value: Mapping[str, Any]) -> str:
    return _b64url_encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())


def _signature(secret: str, signing_input: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256)
    return _b64url_encode(digest.digest())


def _require_secret(secret: str) -> str:
    secret = secret.strip()
    if len(secret) < SERVICE_TOKEN_MIN_SECRET_LENGTH:
        raise SecurityApiError(
            503,
            "サービス間の認証が設定されていません。"
            f"PLATFORM_SERVICE_TOKEN_SECRET に {SERVICE_TOKEN_MIN_SECRET_LENGTH} 文字以上の"
            "ランダムな値を設定してください。",
        )
    return secret


def _valid_profile_ids(value: object) -> list[str] | None:
    """claim の `profile_ids` の形（空でない文字列の 1〜上限件の一覧）。形が違えば None。"""
    if not isinstance(value, list) or not 0 < len(value) <= PROFILE_SCOPE_MAX_IDS:
        return None
    ids: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        if len(item) > PROFILE_SCOPE_MAX_ID_CHARS:
            return None
        ids.append(item.strip())
    return ids


def issue_service_token(
    secret: str,
    *,
    subject: str,
    audience: str,
    issuer: str,
    ttl_seconds: int = SERVICE_TOKEN_DEFAULT_TTL_SECONDS,
    claims: Mapping[str, Any] | None = None,
    profile_ids: Sequence[str] | None = None,
    now: float | None = None,
) -> str:
    """`sub` の利用者として `aud` の製品を呼ぶ token を作る。`claims` は監査用の追加項目。

    `profile_ids` は呼び出しで使ってよいプロファイルの ID（#1379）。空・None なら claim を付けない。
    """
    secret = _require_secret(secret)
    if not subject or not audience:
        raise ValueError("subject と audience は必須です。")
    if not 0 < ttl_seconds <= SERVICE_TOKEN_MAX_TTL_SECONDS:
        raise ValueError(f"ttl_seconds は 1〜{SERVICE_TOKEN_MAX_TTL_SECONDS} 秒にしてください。")
    extra = dict(claims or {})
    extra.pop(PROFILE_SCOPE_CLAIM, None)
    if profile_ids:
        scoped = _valid_profile_ids(list(dict.fromkeys(profile_ids)))
        if scoped is None:
            raise ValueError(
                f"profile_ids は {PROFILE_SCOPE_MAX_IDS} 件以内の、"
                f"{PROFILE_SCOPE_MAX_ID_CHARS} 文字以内の ID にしてください。"
            )
        extra[PROFILE_SCOPE_CLAIM] = scoped
    issued_at = int(now if now is not None else time.time())
    payload = {
        **extra,
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "iat": issued_at,
        "exp": issued_at + ttl_seconds,
    }
    signing_input = f"{_segment(_HEADER)}.{_segment(payload)}"
    return f"{signing_input}.{_signature(secret, signing_input)}"


def verify_service_token(
    secret: str, token: str, *, audience: str, now: float | None = None
) -> dict[str, Any]:
    """署名・`aud`・有効期限を確かめ、claims を返す。失敗は 401。"""
    secret = _require_secret(secret)
    invalid = SecurityApiError(401, "サービストークンが無効です。")
    parts = token.split(".")
    if len(parts) != 3:
        raise invalid
    header_segment, payload_segment, signature = parts
    expected = _signature(secret, f"{header_segment}.{payload_segment}")
    if not hmac.compare_digest(signature.encode("ascii", "ignore"), expected.encode("ascii")):
        raise invalid
    try:
        header = json.loads(_b64url_decode(header_segment))
        payload = json.loads(_b64url_decode(payload_segment))
    except (ValueError, UnicodeDecodeError):
        raise invalid from None
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise invalid
    if not isinstance(payload, dict):
        raise invalid
    current = now if now is not None else time.time()
    exp, iat = payload.get("exp"), payload.get("iat")
    if not isinstance(exp, int) or not isinstance(iat, int):
        raise invalid
    if exp - iat > SERVICE_TOKEN_MAX_TTL_SECONDS or iat > current + SERVICE_TOKEN_LEEWAY_SECONDS:
        raise invalid
    if exp + SERVICE_TOKEN_LEEWAY_SECONDS < current:
        raise SecurityApiError(401, "サービストークンの有効期限が切れています。")
    if payload.get("aud") != audience or not isinstance(payload.get("sub"), str):
        raise invalid
    if not payload["sub"]:
        raise invalid
    # 範囲の claim は、あれば形を確かめる（壊れた範囲を「範囲なし」として通さない。#1379）。
    if PROFILE_SCOPE_CLAIM in payload and _valid_profile_ids(payload[PROFILE_SCOPE_CLAIM]) is None:
        raise invalid
    return payload


def profile_scope_from_claims(claims: Mapping[str, Any] | None) -> frozenset[str] | None:
    """検証済みの claims の、使ってよいプロファイルの ID（#1379）。

    claim が無ければ None（範囲なし）。`verify_service_token` が形を確かめた claims を渡す。
    形が違うときは空集合（何も使えない）にする。
    """
    if claims is None or PROFILE_SCOPE_CLAIM not in claims:
        return None
    ids = _valid_profile_ids(claims[PROFILE_SCOPE_CLAIM])
    return frozenset(ids) if ids is not None else frozenset()


def narrow_to_profile_scope(
    allowed: Collection[str] | None, scope: frozenset[str] | None
) -> frozenset[str] | None:
    """利用者が使えるプロファイル（None は制限なし）を、claim の範囲（None は範囲なし）との
    積にする。"""
    if scope is None:
        return frozenset(allowed) if allowed is not None else None
    if allowed is None:
        return scope
    return frozenset(item for item in allowed if item in scope)
