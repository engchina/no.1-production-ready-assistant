"""確認を省略する方針を、未確定の条件とともに回答生成へ渡す。"""

from collections.abc import Mapping, Sequence
from typing import Any

from app.rag.search_answer_profile_knowledge import (
    rule_answer,
    unanswered_rule_clarifications,
)
from app.schemas.search_answer_profile_knowledge import ClarificationAnswer


def with_answer_assumptions(
    payload: Mapping[str, Any] | None,
    *,
    policy: str,
    question: str,
    conditions: Mapping[str, str],
    guide: Mapping[str, Any],
    answered_rule_ids: Sequence[str] = (),
    confirmed_answers: Sequence[ClarificationAnswer] = (),
) -> dict[str, Any]:
    """保存値を変えず、今回の回答用に方針と未回答事項を重ねる。"""
    result = dict(payload or {})
    # 保存された補助情報から、別の回答の方針を引き継がない。
    result.pop("assume_unconfirmed", None)
    result.pop("unconfirmed_conditions", None)
    if policy != "assume":
        return result
    pending: list[dict[str, Any]] = []
    for rule, clarification in unanswered_rule_clarifications(
        result, question, answers=confirmed_answers
    ):
        rule_id = str(rule.get("id", ""))
        answer = conditions.get(f"rule:{rule_id}", "")
        if rule_id in answered_rule_ids or (answer and rule_answer(result, rule_id, answer)):
            continue
        pending.append(
            {
                "condition_id": f"rule:{rule_id}",
                "question": clarification.question,
                "options": [option.label for option in clarification.options],
            }
        )
    pending.extend(item for item in guide.get("clarifications", []) if isinstance(item, dict))
    result.update(assume_unconfirmed=True, unconfirmed_conditions=pending)
    return result
