"""DB に保存する秘密（MCP 接続の API キー・OAuth の client secret）の暗号化（#764）。

鍵は 3 製品の共通 `.env` の `PLATFORM_SERVICE_TOKEN_SECRET`（32 文字以上）から HKDF-SHA256 で
導く（新しい環境変数を増やさない）。暗号は Fernet（AES-128-CBC + HMAC-SHA256）。保存する値は
`enc:v1:<token>`。署名鍵を変えると復号できなくなるため、その接続の秘密は画面で入れ直す。
"""

from __future__ import annotations

import base64
import logging

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.settings import get_settings

logger = logging.getLogger(__name__)

SEALED_PREFIX = "enc:v1:"
_SALT = b"production-ready-agent"
_INFO = b"agent-control-plane-secrets"
_MIN_SECRET_LENGTH = 32


class SecretBoxError(RuntimeError):
    """暗号化の鍵が無い（画面に出す日本語の文言）。"""


def _fernet() -> Fernet:
    secret = get_settings().app_service_token_secret.strip()
    if len(secret) < _MIN_SECRET_LENGTH:
        raise SecretBoxError(
            "秘密を保存できません。共通 .env の PLATFORM_SERVICE_TOKEN_SECRET（32 文字以上）を"
            "設定してください。"
        )
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT, info=_INFO).derive(
        secret.encode("utf-8")
    )
    return Fernet(base64.urlsafe_b64encode(key))


def seal_secret(value: str) -> str:
    """平文の秘密を保存用の値（`enc:v1:...`）にする。"""
    return SEALED_PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def open_secret(value: str) -> str | None:
    """保存した値を平文に戻す。復号できない（鍵が変わった等）ときは None（入れ直しが必要）。"""
    if not value.startswith(SEALED_PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(SEALED_PREFIX) :].encode("ascii")).decode("utf-8")
    except (InvalidToken, SecretBoxError, ValueError):
        logger.warning("agent_secret_unreadable")
        return None
