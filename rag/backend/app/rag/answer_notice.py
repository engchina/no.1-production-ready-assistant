"""回答の注意書き（設定は製品ごと。#2405・#2449）と、検索・回答プロファイルの窓口の上書き（#2400）。

文はこの製品の運用設定の「回答の注意書き」
（platform の `pr_system_settings.answer_notice`）の本文と窓口で
決まる。RAG は回答の確定（回答側の安全チェック）の後に、回答とは別の項目として付ける。モデルへの
指示・会話の履歴・回答の検証には入れない。確認の質問（`needs_clarification`）には付けない。

窓口の案内の 1 行（本文の後）は次の順で決める。同じ案内を 2 度出さない。

1. 業務ガイドの人への引き継ぎ（handoff。回答は `needs_human`）で、ガイドに窓口
   （`handoff.contact`）があるとき: 回答の本文がガイドの窓口を案内するので、注意書きには窓口の
   案内を出さない（ガイドを優先する）。
2. 検索・回答プロファイルの窓口の上書き（`contact_override`）があれば、その窓口。無ければ
   システム設定の窓口。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from pr_system_settings.answer_notice import (
    DEFAULT_ANSWER_NOTICE_PARTS,
    AnswerNoticeParts,
    answer_notice_contact_line,
)

logger = logging.getLogger(__name__)

AnswerNoticeSource = Callable[[], AnswerNoticeParts | None]
# 回答ではなく確認の質問の outcome（回答の封筒の `outcome`）。注意書きを付けない。
NOTICE_EXCLUDED_OUTCOMES = frozenset({"needs_clarification"})
# 業務ガイドの人への引き継ぎの判断（`rag_support_guide.decision`）。
GUIDE_HANDOFF_DECISION = "handoff"
# 引き継ぎの判断のときに、ガイドの窓口（handoff.contact）を持つ要求ごとの印。
GUIDE_HANDOFF_CONTACT_KEY = "handoff_contact"


def current_answer_notice() -> AnswerNoticeParts | None:
    """いまの注意書きの部品（無効なら None）。設定を読めないときは既定（回答は止めない）。"""
    try:
        from app.security.service import get_security_service

        return get_security_service().answer_notice.current_parts()
    except Exception as exc:  # 共通認証の service を作れない構成でも回答は返す
        logger.warning("answer_notice_unavailable", extra={"error_type": type(exc).__name__})
        return DEFAULT_ANSWER_NOTICE_PARTS


def inherited_answer_notice_contact() -> tuple[bool, str]:
    """プロファイルとガイドが継承する、システム設定の窓口（有効か・窓口。#2683）。

    回答に使う値（`current_answer_notice`）と同じ。無効なら窓口の案内を出さないので (False, "")。
    """
    parts = current_answer_notice()
    return (True, parts.contact) if parts is not None else (False, "")


def answer_outcome_takes_notice(diagnostics: Mapping[str, object]) -> bool:
    """回答の診断の outcome が注意書きを付ける回答か（確認の質問は付けない）。"""
    return diagnostics.get("outcome") not in NOTICE_EXCLUDED_OUTCOMES


def compose_rag_answer_notice(
    parts: AnswerNoticeParts | None,
    diagnostics: Mapping[str, object],
    *,
    contact_override: str = "",
    guide: Mapping[str, object] | None = None,
) -> str | None:
    """回答に付ける注意書き（モジュールの説明の順で窓口の案内を決める）。付けないなら None。"""
    if parts is None or not answer_outcome_takes_notice(diagnostics):
        return None
    if (
        diagnostics.get("outcome") == "needs_human"
        and guide
        and guide.get("decision") == GUIDE_HANDOFF_DECISION
        and str(guide.get(GUIDE_HANDOFF_CONTACT_KEY) or "").strip()
    ):
        return parts.text("")
    contact = contact_override.strip() or parts.contact
    return parts.text(answer_notice_contact_line(contact))
