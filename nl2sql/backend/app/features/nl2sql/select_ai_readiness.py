"""Select AI の Credential の事前条件（#1683 / #1688）。

DBMS_CLOUD_AI Profile は Select AI の Credential（`NL2SQL_SELECT_AI_CREDENTIAL_NAME`。既定
`NL2SQL_OCI_CRED`）を参照する。Credential が無いと、業務プロファイルの Oracle 反映・Select AI /
Select AI Agent での SQL の生成・合成データ・SQL 生成評価・Select AI Agent アセットの再構築が
Oracle の処理の途中で失敗する。これらの操作は、Oracle に触る処理を始める前にここで確かめ、
無ければ 409 `SELECT_AI_CREDENTIAL_MISSING` で止める。画面は
`GET /api/nl2sql/select-ai/credential-readiness` で同じ状態を読み、操作の直前に事前条件を示す。

- 確かめないとき: deterministic runtime（Oracle を使わない）・Credential 名が未設定
  （Profile が Credential を参照しない）。
- 確かめられないとき（DB に接続できない等）は止めない（処理が同じ失敗を返す）。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.settings import get_settings

from .models import Nl2SqlEngine, SelectAiCredentialReadinessData
from .oracle_adapter import SelectAiCredentialMissingError

logger = logging.getLogger(__name__)

# Select AI の Credential が前提の生成の方式（Enterprise AI の直接の生成は使わない）。
SELECT_AI_CREDENTIAL_ENGINES = frozenset({Nl2SqlEngine.SELECT_AI, Nl2SqlEngine.SELECT_AI_AGENT})

# DBMS_CLOUD_AI が参照する Credential が無いときの ORA（実 ADB で確認。#1683）:
#   ORA-20004: Credential "ADMIN"."NL2SQL_OCI_CRED" does not exist
_CREDENTIAL_MISSING_ORA_RE = re.compile(
    r"\bORA-20004\b[^\n]*\bcredential\b[^\n]*\bdoes not exist\b", re.IGNORECASE
)
# 文言の形は画面の事前条件の案内と DB ゲート（messaging.md §3.4.1）にそろえる。
_CREDENTIAL_MISSING_MESSAGE = (
    "Select AI・Select AI Agent の処理には Select AI Credential が必要です。"
    "運用設定の「Select AI Credential」で作成してから、再試行してください。"
)


@dataclass(frozen=True)
class SelectAiCredentialCheck:
    """Credential の確認の結果。`exists` は確かめられなかったとき None。"""

    required: bool
    credential_name: str
    exists: bool | None
    schema_name: str = ""

    @property
    def missing(self) -> bool:
        return self.required and self.exists is False


def check_select_ai_credential(adapter: Any, settings: Any = None) -> SelectAiCredentialCheck:
    """`USER_CREDENTIALS` で Credential の有無を副作用なしで確かめる。"""
    settings = settings if settings is not None else get_settings()
    runtime_mode = str(getattr(settings, "nl2sql_runtime_mode", "") or "").strip().lower()
    credential_name = (
        str(getattr(settings, "nl2sql_select_ai_credential_name", "") or "").strip().upper()
    )
    if runtime_mode != "oracle" or not credential_name:
        return SelectAiCredentialCheck(False, credential_name, None)
    status = getattr(adapter, "get_select_ai_credential_status", None)
    if status is None:
        return SelectAiCredentialCheck(True, credential_name, None)
    try:
        schema_name, exists = status(credential_name)
    except Exception as exc:
        logger.warning(
            "Select AI Credential の状態を確認できませんでした。",
            extra={"credential_name": credential_name, "error_type": type(exc).__name__},
        )
        return SelectAiCredentialCheck(True, credential_name, None)
    return SelectAiCredentialCheck(True, credential_name, bool(exists), str(schema_name or ""))


def select_ai_credential_readiness(
    adapter: Any, settings: Any = None
) -> SelectAiCredentialReadinessData:
    check = check_select_ai_credential(adapter, settings)
    return SelectAiCredentialReadinessData(
        credential_required=check.required,
        credential_name=check.credential_name,
        credential_exists=check.exists,
    )


def assert_select_ai_credential_ready(adapter: Any, settings: Any = None) -> None:
    """Credential が無いと分かったら `SelectAiCredentialMissingError` で止める。"""
    check = check_select_ai_credential(adapter, settings)
    if check.missing:
        raise SelectAiCredentialMissingError(check.credential_name, check.schema_name)


def uses_select_ai_credential(engines: Iterable[Nl2SqlEngine | str]) -> bool:
    """生成の方式のどれかが Select AI の Credential を使うか。"""
    return any(Nl2SqlEngine(engine) in SELECT_AI_CREDENTIAL_ENGINES for engine in engines)


def is_credential_missing_oracle_error(exc: BaseException) -> bool:
    """例外とその原因の連鎖のどこかに、Credential が無いときの ORA があるか。"""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _CREDENTIAL_MISSING_ORA_RE.search(str(current)):
            return True
        current = current.__cause__ or current.__context__
    return False


def select_ai_credential_missing_message(exc: BaseException | None = None) -> str:
    """Credential が無いときの利用者向けの文。ORA と PL/SQL の stack は出さない。"""
    if isinstance(exc, SelectAiCredentialMissingError) and exc.credential_name:
        name = (
            f'"{exc.schema_name}"."{exc.credential_name}"'
            if exc.schema_name
            else f'"{exc.credential_name}"'
        )
        return f"Select AI Credential {name} が存在しません。{_CREDENTIAL_MISSING_MESSAGE}"
    return f"Select AI Credential が存在しません。{_CREDENTIAL_MISSING_MESSAGE}"


def select_ai_credential_missing_detail(exc: BaseException | None = None) -> dict[str, str]:
    """HTTPException の detail（problem 応答の code と本文）。"""
    return {
        "code": SelectAiCredentialMissingError.code,
        "message_ja": select_ai_credential_missing_message(exc),
    }
