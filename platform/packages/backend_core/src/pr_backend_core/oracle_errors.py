"""Oracle のエラーの分類（3 製品共通。#820）。

python-oracledb に依存せず、例外の文字列と型名・`args[0]` の `full_code` / `message` を読む。

- `oracle_error_codes(exc)`：例外（`__cause__` / `__context__` の連鎖を含む）が持つ
  ORA / DRG / DPY / DPI のコード
- `is_oracle_pool_wait_timeout(exc)`：pool の待ちの timeout（`DPY-4005`）
- `is_oracle_connection_error(exc)`：DB に接続できない・接続が切れた（停止中・起動中・ネットワーク・
  資格情報）。DB ゲートはこれを「接続できない」（`unreachable`）に分類し、「初期化が必要」と区別する
"""

from __future__ import annotations

import re

ORACLE_ERROR_CODE_RE = re.compile(r"\b(?:ORA|DRG|DPY|DPI)-\d{4,5}\b", re.IGNORECASE)

# pool が接続を返さないまま待ちの上限に達した（python-oracledb の Thin / Thick 共通）。
POOL_WAIT_TIMEOUT_CODE = "DPY-4005"

# DB に接続できない・接続が切れたことを表すコード（範囲は下の `_CONNECTION_CODE_RANGES`）。
_CONNECTION_CODES = frozenset(
    {
        POOL_WAIT_TIMEOUT_CODE,
        "DPY-1001",  # not connected to database
        "DPY-4011",  # the database or network closed the connection
        "DPY-4024",  # call timeout exceeded
        "DPI-1010",  # not connected
        "DPI-1080",  # connection was closed by ORA-3113
        "ORA-01017",  # invalid username/password
        "ORA-01033",  # initialization or shutdown in progress
        "ORA-01034",  # Oracle not available
        "ORA-01089",  # immediate shutdown in progress
        "ORA-01090",  # shutdown in progress
        "ORA-03113",  # end-of-file on communication channel
        "ORA-03114",  # not connected to Oracle
        "ORA-03135",  # connection lost contact
        "ORA-28000",  # account is locked
        "ORA-28001",  # password has expired
        "ORA-28547",  # connection to server failed
        "ORA-29002",  # SSL transport detected invalid or obsolete server certificate
        "ORA-29003",  # SSL transport detected mismatched server certificate
        "ORA-29024",  # certificate validation failure
    }
)
# DPY-6000〜6999（Thin の接続・ネットワーク）と ORA-12150〜12999（Oracle Net。
# ORA-12170 / 125xx を含む）。
_CONNECTION_CODE_RANGES: tuple[tuple[str, int, int], ...] = (
    ("DPY", 6000, 6999),
    ("ORA", 12150, 12999),
)


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """`exc` と、その原因（`__cause__` / `__context__`）の連鎖（循環は 1 回だけ）。"""
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 10:
        seen.add(id(current))
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def oracle_error_codes(exc: BaseException) -> list[str]:
    """原因の連鎖も含む ORA / DRG / DPY / DPI のコード（大文字・重複なし・出現順）。"""
    codes: list[str] = []
    for item in _exception_chain(exc):
        error = item.args[0] if item.args else None
        full_code = getattr(error, "full_code", None)
        if isinstance(full_code, str) and ORACLE_ERROR_CODE_RE.fullmatch(full_code):
            codes.append(full_code.upper())
        # driver のエラーオブジェクトは、文字列表現とは別に message に下位コードを持つ。
        message = getattr(error, "message", None)
        if isinstance(message, str):
            codes.extend(code.upper() for code in ORACLE_ERROR_CODE_RE.findall(message))
        codes.extend(code.upper() for code in ORACLE_ERROR_CODE_RE.findall(str(item)))
    return list(dict.fromkeys(codes))


def _is_connection_code(code: str) -> bool:
    if code in _CONNECTION_CODES:
        return True
    prefix, _, number = code.partition("-")
    if not number.isdigit():
        return False
    value = int(number)
    return any(
        prefix == range_prefix and low <= value <= high
        for range_prefix, low, high in _CONNECTION_CODE_RANGES
    )


def is_oracle_pool_wait_timeout(exc: BaseException) -> bool:
    """pool の待ちの timeout（`DPY-4005`）か。"""
    return POOL_WAIT_TIMEOUT_CODE in oracle_error_codes(exc)


def is_oracle_connection_error(exc: BaseException) -> bool:
    """DB に接続できない・接続が切れたことによる失敗か（pool の待ちの timeout を含む）。

    コードが無い例外は、接続確認の timeout（`TimeoutError` / 型名に timeout を含む製品の例外）と
    `ConnectionError` だけを接続の失敗とみなす。SQL・辞書・権限のエラー（`ORA-00942` など）は
    含めない。
    """
    codes = oracle_error_codes(exc)
    if codes:
        return any(_is_connection_code(code) for code in codes)
    return any(
        isinstance(item, TimeoutError | ConnectionError) or "timeout" in type(item).__name__.lower()
        for item in _exception_chain(exc)
    )


__all__ = [
    "ORACLE_ERROR_CODE_RE",
    "POOL_WAIT_TIMEOUT_CODE",
    "is_oracle_connection_error",
    "is_oracle_pool_wait_timeout",
    "oracle_error_codes",
]
