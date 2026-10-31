"""失敗した管理 SQL の履歴と、同じ修正へ戻ることの検出。"""

from __future__ import annotations

from collections.abc import Sequence

from pr_backend_core.oracle_errors import oracle_error_codes
from sqlglot.dialects import Dialect
from sqlglot.errors import TokenError

from .models import DbAdminChatCorrectionAttempt, DbAdminChatStatementLastExecution, DbAdminChatTurn

ATTEMPT_LIMIT = 3
SQL_CONTEXT_CHARS = 12000
ERROR_SUMMARY_CHARS = 500


def sql_identity(sql: str) -> tuple[tuple[str, str], ...]:
    """コメント・空白・非引用の大小文字・末尾の区切りだけを無視する。リテラルは保つ。"""
    try:
        tokens = Dialect.get_or_raise("oracle").tokenize(sql)
    except TokenError:
        return (("RAW", sql.strip().rstrip(";").strip()),)
    if tokens and tokens[-1].token_type.name == "SLASH":
        last = tokens[-1]
        # SQL*Plus の単独行だけを区切りとする。式の除算と引用の内容は残す。
        if sql.splitlines()[last.line - 1].strip() == "/":
            tokens.pop()
    while tokens and tokens[-1].token_type.name == "SEMICOLON":
        tokens.pop()
    return tuple(
        (
            token.token_type.name,
            token.text
            if "STRING" in token.token_type.name or token.token_type.name == "IDENTIFIER"
            else token.text.upper(),
        )
        for token in tokens
    )


def record_failures(
    turn: DbAdminChatTurn, executions: Sequence[DbAdminChatStatementLastExecution]
) -> list[DbAdminChatCorrectionAttempt]:
    attempts = list(turn.correction_attempts)
    for execution in executions:
        if execution.status != "failed":
            continue
        statement = next((s for s in turn.plan.statements if s.index == execution.index), None)
        if statement is None:
            continue
        codes = oracle_error_codes(RuntimeError(execution.outcome))
        attempt = DbAdminChatCorrectionAttempt(
            statement_index=statement.index,
            revision=turn.revision,
            sql=statement.sql,
            error_code=next(iter(codes), ""),
            error_summary=next(iter(execution.outcome.splitlines()), "")[:ERROR_SUMMARY_CHARS],
            executed_at=execution.executed_at,
        )
        attempts = [
            a
            for a in attempts
            if (a.statement_index, a.revision) != (attempt.statement_index, attempt.revision)
        ]
        attempts.append(attempt)
    kept: list[DbAdminChatCorrectionAttempt] = []
    for index in sorted({a.statement_index for a in attempts}):
        kept.extend([a for a in attempts if a.statement_index == index][-(ATTEMPT_LIMIT + 1) :])
    return kept
