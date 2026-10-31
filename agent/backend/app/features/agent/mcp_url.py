"""MCP 接続の URL に書かれた資格情報（userinfo・secret らしい query）の検出と伏せ字（#1056）。

資格情報は認証の欄（API キー・OAuth）で secret として保存し、URL には書かない。
- 保存の API は、userinfo（`https://user:pass@host/…`）と secret らしい query（`?api_key=…`）を
  拒否する
- 既に保存した URL（保存の検証の前のデータ・`.env` の宣言）は壊さず、一覧・詳細・
  エラーの詳細に出すときだけ伏せる。実際の接続には保存した URL をそのまま使う

規則は画面の先回りの検証（frontend `src/lib/mcp-url.ts`）と e2e の mock と同じにする。
"""

from __future__ import annotations

import re
from urllib.parse import unquote_plus, urlsplit, urlunsplit

URL_CREDENTIAL_MASK = "***"

# query の名前を語に分けたとき、どれかの語がこれなら secret とみなす
# （`api_key`・`apiKey`・`X-Api-Key`・`access_token`・`X-Amz-Signature`・`client_secret` など）。
_SECRET_QUERY_WORDS = frozenset(
    {
        "key",
        "apikey",
        "token",
        "secret",
        "password",
        "passwd",
        "pwd",
        "auth",
        "authorization",
        "credential",
        "credentials",
        "signature",
        "sig",
        "jwt",
        "bearer",
    }
)
_CAMEL_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_WORD_SEPARATOR = re.compile(r"[^a-z0-9]+")
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>]+")

USERINFO_MESSAGE = (
    "MCP の URL にユーザー名・パスワード（user:pass@）を含めないでください。"
    "資格情報は「認証」の欄（API キー・OAuth）で設定してください。"
)


def _secret_query_message(names: list[str]) -> str:
    joined = "・".join(names)
    return (
        f"MCP の URL に資格情報のパラメータ（{joined}）を含めないでください。"
        "資格情報は「認証」の欄（API キー・OAuth）で設定してください。"
    )


def is_secret_query_name(name: str) -> bool:
    """query の名前が資格情報らしいか（語のどれかが `_SECRET_QUERY_WORDS` に入る）。"""
    words = _WORD_SEPARATOR.split(_CAMEL_BOUNDARY.sub(r"\1_\2", name).lower())
    return any(word in _SECRET_QUERY_WORDS for word in words if word)


def _query_name(part: str) -> str:
    return unquote_plus(part.split("=", 1)[0])


def secret_query_names(url: str) -> list[str]:
    """URL の query のうち、資格情報らしい名前（重複なし・出現順）。"""
    query = urlsplit(url).query
    names: list[str] = []
    for part in query.split("&"):
        name = _query_name(part)
        if part and is_secret_query_name(name) and name not in names:
            names.append(name)
    return names


def url_credential_error(url: str) -> str | None:
    """保存を断る理由（利用者向けの 1 文）。資格情報が無ければ None。値は文に含めない。"""
    try:
        parts = urlsplit(url)
        if "@" in parts.netloc:
            return USERINFO_MESSAGE
        names = secret_query_names(url)
    except ValueError:
        return None
    return _secret_query_message(names) if names else None


def url_has_credentials(url: str | None) -> bool:
    return bool(url) and mask_url_credentials(url) != url


def mask_url_credentials(url: str | None) -> str | None:
    """userinfo と secret らしい query の値を `***` にした URL（それ以外はそのまま）。"""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        # 解析できない URL は中身を出さない。
        return URL_CREDENTIAL_MASK
    netloc = parts.netloc
    if "@" in netloc:
        netloc = f"{URL_CREDENTIAL_MASK}@{netloc.rsplit('@', 1)[1]}"
    query = "&".join(
        f"{part.split('=', 1)[0]}={URL_CREDENTIAL_MASK}"
        if part and is_secret_query_name(_query_name(part))
        else part
        for part in parts.query.split("&")
    )
    if netloc == parts.netloc and query == parts.query:
        return url
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))


def mask_urls_in_text(text: str) -> str:
    """文の中の URL の資格情報を伏せる（例外の文など、URL を含みうる文を応答に出すとき）。"""
    return _URL_IN_TEXT.sub(lambda match: mask_url_credentials(match[0]) or "", text)
