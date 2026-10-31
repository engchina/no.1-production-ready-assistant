"""DB 管理チャット（#2264）の方針で止める文・生成の文脈・LLM への指示。

ここは DB に触れない純粋な処理だけを持つ。SQL の分割・文の分類・製品の object の検査・実行・監査は
「管理 SQL を実行」と同じ実装（service.py の `_split_sql_statements` / `_admin_statement_type` /
`_execute_db_admin_sql`）を使う。

方針で止める文は技術の安全の規則としてコードに書く（利用者の判断。#2264）。文字列リテラル・引用識別子・
コメントの中身を空白にした文（masked）に当てるので、値や注釈の中の語では止めない。
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import DbAdminChatHistoryTurn, SchemaTable


@dataclass(frozen=True, slots=True)
class ForbiddenStatementRule:
    code: str
    pattern: re.Pattern[str]
    message: str


def _rule(code: str, pattern: str, message: str) -> ForbiddenStatementRule:
    return ForbiddenStatementRule(code, re.compile(pattern, re.IGNORECASE | re.DOTALL), message)


# 文の先頭で判定する規則（masked で先頭のコメントを除いた文）。
_LEADING_RULES: tuple[ForbiddenStatementRule, ...] = (
    _rule(
        "ALTER_SYSTEM",
        r"^ALTER\s+SYSTEM\b",
        "ALTER SYSTEM（インスタンスの設定・セッションの強制終了など）は実行できません。",
    ),
    _rule(
        "ALTER_DATABASE",
        r"^ALTER\s+(?:PLUGGABLE\s+)?DATABASE\b(?!\s+LINK\b)",
        "ALTER DATABASE / ALTER PLUGGABLE DATABASE は実行できません。",
    ),
    _rule(
        "CREATE_DROP_DATABASE",
        r"^(?:CREATE|DROP)\s+(?:PLUGGABLE\s+)?DATABASE\b(?!\s+LINK\b)",
        "データベース・プラガブル・データベースの作成と削除は実行できません。",
    ),
    _rule(
        "SHUTDOWN_STARTUP",
        r"^(?:SHUTDOWN|STARTUP)\b",
        "データベースの停止・起動は実行できません。",
    ),
    _rule(
        "USER_ACCOUNT",
        r"^(?:CREATE|ALTER|DROP)\s+USER\b",
        "ユーザーの作成・変更・削除は実行できません。",
    ),
    _rule(
        "DROP_TABLESPACE",
        r"^DROP\s+TABLESPACE\b",
        "表領域の削除は実行できません。",
    ),
    _rule(
        "AUDIT_POLICY",
        r"^(?:NOAUDIT\b|(?:CREATE|ALTER|DROP)\s+AUDIT\s+POLICY\b)",
        "監査の設定の変更は実行できません。",
    ),
    _rule(
        "SESSION_STATE",
        r"^ALTER\s+SESSION\b",
        "ALTER SESSION は実行できません（接続の状態を変えるため）。",
    ),
    _rule(
        "TRANSACTION_CONTROL",
        r"^(?:COMMIT|ROLLBACK|SAVEPOINT|SET\s+TRANSACTION)\b",
        "COMMIT / ROLLBACK などのトランザクションの制御は書けません"
        "（全部成功したときだけ commit します）。",
    ),
    _rule(
        "ADMIN_PRIVILEGE_GRANT",
        r"^(?:GRANT|REVOKE)\b.*\b(?:DBA|PDB_DBA|SYSDBA|SYSOPER|SYSBACKUP|SYSDG|SYSKM|SYSRAC"
        r"|ALL\s+PRIVILEGES|ANY\s+PRIVILEGE)\b",
        "管理者の権限・ロール（DBA・SYSDBA・ALL PRIVILEGES など）の"
        "付与と取り消しは実行できません。",
    ),
)

# 文のどこにあっても止める規則（SELECT を含む）。
_ANYWHERE_RULES: tuple[ForbiddenStatementRule, ...] = (
    _rule(
        "DYNAMIC_SQL",
        r"\bEXECUTE\s+IMMEDIATE\b|\bDBMS_SQL\b|\bOPEN\s+\w+\s+FOR\b",
        "動的 SQL（EXECUTE IMMEDIATE・DBMS_SQL など）は実行できません。",
    ),
    _rule(
        "EXTERNAL_ACCESS_PACKAGE",
        r"\b(?:UTL_HTTP|UTL_TCP|UTL_SMTP|UTL_MAIL|UTL_FILE|UTL_INADDR|DBMS_NETWORK_ACL_ADMIN"
        r"|DBMS_CLOUD|DBMS_CREDENTIAL|DBMS_JAVA|DBMS_SCHEDULER|DBMS_JOB|DBMS_PIPE|DBMS_LDAP"
        r"|HTTPURITYPE)\b",
        "外部への通信・ファイル・ジョブ・資格情報を扱うパッケージは実行できません。",
    ),
)

FORBIDDEN_STATEMENT_RULES: tuple[ForbiddenStatementRule, ...] = (*_LEADING_RULES, *_ANYWHERE_RULES)


def forbidden_statement_error(masked_statement: str) -> str:
    """方針で止める文なら理由を返す（止めないなら空）。

    `masked_statement` は先頭のコメントを除き、リテラル・引用識別子・コメントの中身を空白にした文。
    """

    text = masked_statement.strip()
    for rule in _LEADING_RULES:
        if rule.pattern.search(text):
            return rule.message
    for rule in _ANYWHERE_RULES:
        if rule.pattern.search(text):
            return rule.message
    return ""


_SINGLE_OBJECT_DESTRUCTIVE = re.compile(
    r"^(?:DROP|TRUNCATE)\s+(?:TABLE|VIEW|MATERIALIZED\s+VIEW|INDEX|SEQUENCE|SYNONYM"
    r"|PROCEDURE|FUNCTION|PACKAGE(?:\s+BODY)?|TRIGGER|TYPE(?:\s+BODY)?|CLUSTER|DOMAIN)(?=\s)",
    re.IGNORECASE,
)
_IDENTIFIER_PART = re.compile(r'"([^"]+)"|([A-Za-z_][\w$#]*)')


def single_object_confirmation_target(statement: str, masked_statement: str) -> str:
    """1 つの対象を消す文（DROP / TRUNCATE）の確認語（対象の名前）。当てはまらなければ空。

    `statement` は先頭のコメントを除いた元の文、`masked_statement` は同じ位置の masked の文。
    対象の名前は引用の無い部分を大文字にし、`OWNER.OBJECT` のように点でつなぐ。
    """

    match = _SINGLE_OBJECT_DESTRUCTIVE.match(masked_statement)
    if match is None:
        return ""
    # masked では引用識別子も空白なので、名前は元の文の、種類の語の後の空白の先から読む。
    rest = statement[match.end() :].lstrip()
    parts: list[str] = []
    index = 0
    while True:
        part = _IDENTIFIER_PART.match(rest, index)
        if part is None:
            break
        parts.append(part.group(1) if part.group(1) is not None else part.group(2).upper())
        index = part.end()
        dot = re.match(r"\s*\.\s*", rest[index:])
        if dot is None or len(parts) >= 2:
            break
        index += dot.end()
    return ".".join(parts)


# ---- 生成の文脈 ----

CONTEXT_TOKEN_MIN_LENGTH = 2
CONTEXT_FREQUENCY_MIN_TABLES = 3
CONTEXT_COMMON_TOKEN_RATIO = 0.5
CONTEXT_COLUMN_MIN_MATCHES = 2
CONTEXT_COLUMN_MIN_SCORE = 1.0
CONTEXT_TABLE_LIMIT = 12
CONTEXT_COLUMN_LIMIT = 40
CONTEXT_OTHER_TABLE_LIMIT = 80
HISTORY_TURN_LIMIT = 6
_HISTORY_SQL_CHARS = 2000


def select_context_tables(
    tables: Sequence[SchemaTable],
    query_tokens: set[str],
    tokenize: Callable[[str], set[str]],
    *,
    limit: int = CONTEXT_TABLE_LIMIT,
) -> list[SchemaTable]:
    """DB 構造の cache から、質問（と前の会話）に関係する表を選ぶ。

    表の名前・論理名・コメントの一致を列の一致より重く数え、点の高い順に `limit` 件。どれも
    一致しなければ空（表の名前の一覧だけを渡す。管理の質問はデータ辞書を使うので表は要らない）。
    """

    def meaningful(tokens: set[str]) -> set[str]:
        # 日本語の単文字と、ひらがなだけの短い並びは表を識別する根拠にしない。
        return {
            token
            for token in tokens
            if len(token) >= CONTEXT_TOKEN_MIN_LENGTH and not re.fullmatch(r"[ぁ-ゖー]+", token)
        }

    query_tokens = meaningful(query_tokens)
    if not query_tokens:
        return []
    documents = []
    frequency: Counter[str] = Counter()
    for table in tables:
        name_tokens = meaningful(
            tokenize(
                " ".join(
                    [
                        table.table_name,
                        table.table_name.replace("_", " "),
                        table.logical_name,
                        table.comment,
                    ]
                )
            )
        )
        column_tokens: set[str] = set()
        for column in table.columns:
            column_tokens |= meaningful(
                tokenize(
                    " ".join(
                        [
                            column.column_name,
                            column.column_name.replace("_", " "),
                            column.logical_name,
                            column.comment,
                        ]
                    )
                )
            )
        documents.append((table, name_tokens, column_tokens))
        frequency.update(name_tokens | column_tokens)

    def weight(token: str) -> float:
        ratio = frequency[token] / len(tables)
        if len(tables) >= CONTEXT_FREQUENCY_MIN_TABLES and ratio > CONTEXT_COMMON_TOKEN_RATIO:
            return 0.0
        return 1.0 / frequency[token]

    scored: list[tuple[float, str, SchemaTable]] = []
    for table, name_tokens, column_tokens in documents:
        identifier_tokens = meaningful(tokenize(table.table_name.replace("_", " "))) | meaningful(
            tokenize(table.table_name)
        )
        name_score = sum(
            1.0 / frequency[token] if token in identifier_tokens else weight(token)
            for token in query_tokens & name_tokens
        )
        column_matches = {token for token in query_tokens & column_tokens if weight(token) > 0}
        column_score = sum(weight(token) for token in column_matches)
        if name_score > 0 or (
            len(column_matches) >= CONTEXT_COLUMN_MIN_MATCHES
            and column_score >= CONTEXT_COLUMN_MIN_SCORE
        ):
            scored.append((3 * name_score + column_score, table.qualified_name, table))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [table for _score, _name, table in scored[:limit]]


def build_generation_context(
    *,
    current_owner: str,
    context_tables: Sequence[SchemaTable],
    other_table_names: Iterable[str],
    history: Sequence[DbAdminChatHistoryTurn],
) -> str:
    lines = [f"current_schema: {current_owner or '(不明)'}"]
    lines.append("relevant_tables（DB 構造の cache から質問に関係する表を選んだもの）:")
    if not context_tables:
        lines.append("- (質問に一致する表はありません)")
    for table in context_tables:
        lines.append(
            f"- {table.table_type} {table.qualified_name}"
            + (f" logical={table.logical_name}" if table.logical_name else "")
            + (f" comment={table.comment}" if table.comment else "")
        )
        for column in table.columns[:CONTEXT_COLUMN_LIMIT]:
            lines.append(
                f"  - column {column.column_name} type={column.data_type}"
                + (f" comment={column.comment}" if column.comment else "")
            )
        if len(table.columns) > CONTEXT_COLUMN_LIMIT:
            lines.append(f"  - (ほか {len(table.columns) - CONTEXT_COLUMN_LIMIT} 列)")
    others = list(other_table_names)
    if others:
        shown = others[:CONTEXT_OTHER_TABLE_LIMIT]
        lines.append("other_tables（名前だけ）: " + ", ".join(shown))
        if len(others) > len(shown):
            lines.append(f"(ほか {len(others) - len(shown)} 件)")
    recent = list(history)[-HISTORY_TURN_LIMIT:]
    if recent:
        lines.append("conversation（同じ画面の前の質問と SQL。続きの質問の文脈）:")
        for turn in recent:
            lines.append(f"- 質問: {turn.question}")
            if turn.sql:
                lines.append(f"  SQL: {turn.sql[:_HISTORY_SQL_CHARS]}")
            if turn.outcome:
                lines.append(f"  結果: {turn.outcome[:500]}")
    return "\n".join(lines)


def _load_prompt(name: str) -> str:
    return (Path(__file__).parent / "prompts" / "db_admin_chat" / f"{name}.txt").read_text(
        encoding="utf-8"
    )


# LLM への指示（日本語）。DB の管理の質問に使うデータ辞書・動的性能ビューと、作らない文を書く。
SYSTEM_PROMPT = _load_prompt("system")
ANALYSIS_SYSTEM_PROMPT = _load_prompt("analysis")
# 存在しない列・object を参照する文を、参照する object の実際の列で 1 回だけ直させる（#2352）。
REPAIR_SYSTEM_PROMPT = _load_prompt("repair")
