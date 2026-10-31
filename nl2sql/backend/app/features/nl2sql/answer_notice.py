"""回答の注意書き（設定は製品ごと。#2405・#2449）。

文はこの製品の運用設定の「回答の注意書き」
（platform の `pr_system_settings.answer_notice`）で決まる。
NL2SQL は回答の確定（SQL の実行の成功・DB 管理チャットの SQL の生成）の後に、回答とは別の項目
（`notice`）として付ける。モデルへの指示・会話の履歴（前の質問と SQL）には入れない。
"""

from __future__ import annotations

import logging

from pr_system_settings.answer_notice import DEFAULT_ANSWER_NOTICE_TEXT

logger = logging.getLogger(__name__)


def current_answer_notice() -> str | None:
    """いまの注意書きの文（無効なら None）。設定を読めないときは既定の文（回答は止めない）。"""
    try:
        from app.security.service import get_security_service

        return get_security_service().answer_notice.current_text()
    except Exception as exc:  # 共通認証の service を作れない構成でも回答は返す
        logger.warning("answer_notice_unavailable", extra={"error_type": type(exc).__name__})
        return DEFAULT_ANSWER_NOTICE_TEXT
