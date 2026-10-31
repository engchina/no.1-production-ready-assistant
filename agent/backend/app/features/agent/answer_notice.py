"""回答の注意書き（設定は製品ごと。#2405・#2449）。

文はこの製品の運用設定の「回答の注意書き」
（platform の `pr_system_settings.answer_notice`）で決まる。
Agent は最終の検証の後に、回答の成果物の `notice` として付ける（本文の `text` とは別）。モデルへの
指示・会話の履歴（前の回答の `text`）・最終の検証の入力には入れない。RAG / NL2SQL をサービス
トークンで呼んだ結果には注意書きが無い（呼び先が API キーの呼び出しにだけ返す）ので、Agent の回答の
末尾には Agent が付ける 1 つだけが出る。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from pr_system_settings.answer_notice import DEFAULT_ANSWER_NOTICE_TEXT

from app.features.agent.answer_outcome import NEEDS_CLARIFICATION

logger = logging.getLogger(__name__)


def current_answer_notice() -> str | None:
    """いまの注意書きの文（無効なら None）。設定を読めないときは既定の文（回答は止めない）。"""
    try:
        from app.security.service import get_security_service

        return get_security_service().answer_notice.current_text()
    except Exception as exc:  # 共通認証の service を作れない構成でも回答は返す
        logger.warning("answer_notice_unavailable", extra={"error_type": type(exc).__name__})
        return DEFAULT_ANSWER_NOTICE_TEXT


def answer_takes_notice(outcome: Mapping[str, Any] | None, *, confirmations: bool) -> bool:
    """回答に注意書きを付けるか。回答前の確認（カード）と確認の質問だけの回答には付けない。"""
    if confirmations:
        return False
    return not (outcome is not None and outcome.get("value") == NEEDS_CLARIFICATION)
