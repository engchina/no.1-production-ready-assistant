"""Oracle Deep Data Security V001 plan、適用、検証。"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from pr_backend_core.api import InvalidCursorError, decode_cursor, encode_cursor

from app.clients.oracle_runtime import (
    OraclePoolManager,
    close_oracle_pools,
    get_oracle_pool_manager,
)
from app.clients.oracle_statement_executor import oracle_statement_executor
from app.env_file import locked_env_file, replace_env_file
from app.features.nl2sql.object_identity import (
    canonical_object_part,
    format_object_part,
    normalize_object_part,
    parse_object_identity,
    qualified_object_name,
)
from app.features.nl2sql.object_visibility import (
    is_user_visible_schema_object,
    user_visible_object_sql_filters,
)
from app.settings import BACKEND_ENV_FILE, Settings, get_settings

from .domain import (
    LEGACY_APP_USER_ID_SCOPE_VALUE_SOURCE,
    LOGIN_USER_ID_SCOPE_VALUE_SOURCE,
    DataEntitlementRecord,
    DataEntitlementScopeFilter,
    Principal,
    RoleRecord,
    scope_filter_payload,
    scope_filters_scope_code,
)
from .scope_relations import DependencyPlan
from .service import (
    SecurityApiError,
    SecurityService,
    get_security_service,
)

logger = logging.getLogger(__name__)

PLAN_VERSION = "V001"
PASSWORD_PLACEHOLDER = "<secret:NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD>"  # nosec B105
DEEPSEC_DATA_USER = "DEEPSEC_DATA_USER"
DEEPSEC_APPLY_CONFIRMATION = "ADMIN_EXECUTE"
DEEPSEC_RESET_CONFIRMATION = "ADMIN_RESET"
DEEPSEC_DB_ROLE = "NL2SQL_APP_DB_ROLE"
DEEPSEC_DATA_ROLE = "NL2SQL_APP_DATA_ROLE"
MANAGED_DATA_GRANT_PREFIX = "NL2SQL_DG_"
DATA_GRANT_PREDICATE_MAX_LENGTH = 4000
_DEEPSEC_MANAGED_TARGET_TYPES = frozenset({"TABLE", "VIEW", "MATERIALIZED VIEW"})
_DEEPSEC_TEXT_TYPES = frozenset({"CHAR", "NCHAR", "VARCHAR2", "NVARCHAR2"})
_DEEPSEC_NUMBER_TYPES = frozenset({"NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE"})
_DEEPSEC_TEMPORAL_TYPES = frozenset({"DATE", "TIMESTAMP"})
_DEEPSEC_SCOPE_VALUE_TYPES = frozenset({"TEXT", "NUMBER", "TEMPORAL"})
_DEEPSEC_SCOPE_VALUE_SOURCES = frozenset({"LITERAL", LOGIN_USER_ID_SCOPE_VALUE_SOURCE})
_DEEPSEC_SCOPE_MODES = frozenset({"ALL", "COLUMN_EQUALS", "FILTERS", "EXPRESSION"})
_DEEPSEC_NULL_OPERATORS = frozenset({"IS_NULL", "IS_NOT_NULL"})
_DEEPSEC_TEXT_OPERATORS = frozenset(
    {"EQ", "NE", "CONTAINS", "STARTS_WITH", "IN", *_DEEPSEC_NULL_OPERATORS}
)
_DEEPSEC_NUMBER_OPERATORS = frozenset(
    {"EQ", "NE", "GT", "GTE", "LT", "LTE", "BETWEEN", "IN", *_DEEPSEC_NULL_OPERATORS}
)
_DEEPSEC_TEMPORAL_OPERATORS = frozenset(
    {
        "EQ",
        "BEFORE",
        "ON_OR_BEFORE",
        "AFTER",
        "ON_OR_AFTER",
        "BETWEEN",
        *_DEEPSEC_NULL_OPERATORS,
    }
)
_DEEPSEC_SCOPE_OPERATORS_BY_TYPE = {
    "TEXT": _DEEPSEC_TEXT_OPERATORS,
    "NUMBER": _DEEPSEC_NUMBER_OPERATORS,
    "TEMPORAL": _DEEPSEC_TEMPORAL_OPERATORS,
}
_DEEPSEC_MAX_SCOPE_FILTERS = 8
_DEEPSEC_MAX_SCOPE_FILTER_VALUES = 25
_DEEPSEC_PREDICATE_TABLE_GRANTS = (
    # ユーザーとロールの割り当ては 3 製品で共有する PLATFORM_* テーブル（#212）。
    ("predicate_user_roles_grant", "PLATFORM_USER_ROLES"),
    ("predicate_roles_grant", "PLATFORM_ROLES"),
    ("predicate_data_entitlements_grant", "NL2SQL_APP_DATA_ENTITLEMENTS"),
)
_DEEPSEC_APP_USER_CONTEXT_EXPR = "ORA_END_USER_CONTEXT.CLIENT_IDENTIFIER"
_DEEPSEC_LOGIN_USER_ID_CONTEXT_EXPR = "SYS_CONTEXT('NL2SQL_APP_USER_CTX', 'LOGIN_USER_ID')"
_DEEPSEC_LEGACY_APP_USER_CONTEXT_EXPR = "SYS_CONTEXT('NL2SQL_APP_USER_CTX', 'APP_USER_ID')"
_POSITIVE_INTEGER_LITERAL_RE = re.compile(r"[1-9]\d*")
_NUMBER_LITERAL_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")
_DATE_LITERAL_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATETIME_LITERAL_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?")
_DEEPSEC_INTERNAL_OWNER_NAMES = frozenset({"SYS", "SYSTEM"})
_DEEPSEC_INTERNAL_OBJECT_PREFIXES = (
    "NL2SQL_APP_",
    "NL2SQL_AUTH_",
    "NL2SQL_DEEPSEC_",
    # 3 製品共通の基盤（ユーザー・ロール・セッションなど。#212）は Data Grant の対象にしない。
    "PLATFORM_",
)
_DEEPSEC_CONFLICTING_POLICY_DETAIL_LIMIT = 5
# DeepSec の設定は NL2SQL 固有なので製品の backend/.env に置く（#211）。
# テストが差し替えられるよう module の値として持つ。
_BACKEND_ENV_FILE = BACKEND_ENV_FILE
_DEEPSEC_ENABLED_KEY = "NL2SQL_ORACLE_DEEPSEC_ENABLED"
_DEEPSEC_DATA_USER_KEY = "NL2SQL_ORACLE_DEEPSEC_DATA_USER"
_DEEPSEC_DATA_USER_PASSWORD_KEY = "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD"  # nosec B105
_ORACLE_PASSWORD_REUSE_RE = re.compile(r"\bORA-28007\b", re.IGNORECASE)
# ORA-52516: END USER or DATA ROLE <name> does not exist.（名前は英語の文のときだけ取れる。#1676）
_ORACLE_MISSING_PRINCIPAL_RE = re.compile(
    r"\bORA-52516\b(?::\s*END USER or DATA ROLE\s+(?P<name>\"?[^\s\"]+\"?))?",
    re.IGNORECASE,
)
_DEEPSEC_CONFIG_KEYS = frozenset(
    {
        _DEEPSEC_ENABLED_KEY,
        _DEEPSEC_DATA_USER_KEY,
        _DEEPSEC_DATA_USER_PASSWORD_KEY,
    }
)
_ENV_ASSIGNMENT_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _env_assignment_key(line: str) -> str | None:
    match = _ENV_ASSIGNMENT_RE.match(line)
    return match.group(1) if match else None


def _format_env_value(value: str) -> str:
    if not value:
        return '""'
    if re.search(r"\s|#|=|'|\\", value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _strict_identifier(value: str) -> str:
    normalized = value.strip().strip('"').upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}", normalized):
        raise SecurityApiError(400, f"安全でない Oracle identifier です: {value}")
    return normalized


def _identifier_token(value: str) -> str:
    """対象 owner / object / column を canonical token にする（SQL へそのまま埋め込める）。

    引用が不要な名前は大文字の単純な識別子、引用が必要な名前は `"..."`。token は二重引用符・
    制御文字を内部に含まないことを canonical_object_part で保証している。
    """

    try:
        return canonical_object_part(value)
    except ValueError as exc:
        raise SecurityApiError(400, f"安全でない Oracle identifier です: {value}") from exc


def _identifier_name(value: str) -> str:
    """canonical token から、辞書ビューの bind に使うカタログ上の名前を取り出す。"""

    return normalize_object_part(_identifier_token(value))


def _catalog_identifier_token(value: object) -> str:
    """辞書ビューが返したカタログ上の名前（引用符なし）を canonical token にする。"""

    name = str(value or "")
    if not name or '"' in name or name != name.strip():
        raise SecurityApiError(400, f"安全でない Oracle identifier です: {name}")
    return _identifier_token(format_object_part(name))


def _has_forbidden_password_char(value: str) -> bool:
    return '"' in value or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)


def _quoted_password(value: str) -> str:
    if not value or len(value) > 256 or _has_forbidden_password_char(value):
        raise SecurityApiError(
            503, "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD を安全な値で設定してください。"
        )
    return '"' + value.replace('"', '""') + '"'


def _validate_data_user_password(value: str) -> str:
    # 「${」は backend/.env を読むとき（python-dotenv）に環境変数として展開され、
    # Oracle に設定した値と再起動後に読む値が変わるので受け付けない（#1000）。
    if len(value) < 12 or len(value) > 256 or _has_forbidden_password_char(value) or "${" in value:
        raise SecurityApiError(
            400,
            "NL2SQL_ORACLE_DEEPSEC_DATA_USER_PASSWORD は12〜256文字で、"
            "二重引用符・「${」・制御文字を含めずに指定してください。",
        )
    return value


def _is_oracle_password_reuse_error(exc: Exception) -> bool:
    return bool(_ORACLE_PASSWORD_REUSE_RE.search(str(exc)))


def _looks_like_missing_scope_filters_column(exc: Exception) -> bool:
    message = str(exc).upper()
    return "ORA-00904" in message and any(
        name in message for name in ("SCOPE_FILTERS", "SCOPE_EXPRESSION")
    )


def _write_deepsec_config_env_locked(settings: Settings, env_path: Path) -> None:
    """DeepSec の 3 key を書く。既存の key があればその位置に、なければ末尾の節に置く。"""
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    insert_at: int | None = None
    next_lines: list[str] = []
    for line in lines:
        if _env_assignment_key(line) in _DEEPSEC_CONFIG_KEYS:
            if insert_at is None:
                insert_at = len(next_lines)
            continue
        next_lines.append(line)
    deepsec_lines = [
        f"{_DEEPSEC_ENABLED_KEY}=true",
        f"{_DEEPSEC_DATA_USER_KEY}={_format_env_value(settings.oracle_deepsec_data_user)}",
        (
            f"{_DEEPSEC_DATA_USER_PASSWORD_KEY}="
            f"{_format_env_value(settings.oracle_deepsec_data_user_password)}"
        ),
    ]
    if insert_at is None:
        if next_lines and next_lines[-1].strip():
            next_lines.append("")
        next_lines.append("# Deep Data Security")
        next_lines.extend(deepsec_lines)
    else:
        next_lines[insert_at:insert_at] = deepsec_lines
    replace_env_file(env_path, "\n".join(next_lines).rstrip() + "\n")


def _write_deepsec_config_env(settings: Settings) -> None:
    try:
        with locked_env_file(_BACKEND_ENV_FILE) as env_path:
            _write_deepsec_config_env_locked(settings, env_path)
    except OSError as exc:
        raise SecurityApiError(
            500,
            "DeepSec DATA USER 認証情報を backend/.env へ保存できませんでした。",
        ) from exc


def _single_quoted_identifier_sql(template: str, **values: str) -> str:
    """固定 PL/SQL template の単一引用符の中へ、検証済みの識別子を escape して埋め込む。

    値は呼び出し側で `_identifier_token` / `_identifier_name` を通したものに限る。
    置換は 1 回の走査で行い、値に含まれる `{...}` を再置換しない。
    """

    def replace_placeholder(match: re.Match[str]) -> str:
        value = values[match.group(1)]
        if any(ord(char) < 32 for char in value):
            raise SecurityApiError(400, f"安全でない Oracle identifier です: {value}")
        return value.replace("'", "''")

    return re.sub(r"\{([a-z_]+)\}", replace_placeholder, template)


def _trusted_identifier_sql(template: str, **identifiers: str) -> str:
    """固定 SQL template へ検証済み Oracle identifier だけを埋め込む。"""

    rendered = template
    for key, value in identifiers.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_$#]{0,127}(?:\.[A-Z][A-Z0-9_$#]{0,127})*", value):
            raise SecurityApiError(400, f"安全でない Oracle identifier です: {value}")
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


@dataclass(frozen=True, slots=True)
class DeepSecStep:
    step_no: int
    key: str
    title: str
    description: str
    statements: tuple[str, ...]
    ignored_error_codes: frozenset[str] = frozenset()

    @property
    def checksum(self) -> str:
        payload = "\n-- statement --\n".join(statement.strip() for statement in self.statements)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class OracleManagedDataGrant:
    grant_name: str
    target_owner: str
    target_object: str
    grantee: str = ""
    grantee_type: str = ""


@dataclass(frozen=True, slots=True)
class DataEntitlementSyncEntry:
    entitlement: DataEntitlementRecord
    statements: tuple[str, ...]
    checksum: str


@dataclass(frozen=True, slots=True)
class DataEntitlementSyncPlan:
    role: RoleRecord
    entries: tuple[DataEntitlementSyncEntry, ...]
    cleanup_statements: tuple[str, ...]
    checksum: str

    @property
    def statements(self) -> tuple[str, ...]:
        return (
            *self.cleanup_statements,
            *(statement for entry in self.entries for statement in entry.statements),
        )


def _statement_failure_message(lead: str, error: Mapping[str, object]) -> str:
    """Oracle の文の失敗を、利用者向けの日本語の 1 文目と Oracle の応答に分けて返す（#1022）。

    以前は Oracle の文（ORA-...）だけ、空なら英語の "SQL execution failed" を返していた。
    次に何をすればよいかが分かる Oracle の error は、案内の文を lead の後ろに足す（#1676）。
    """

    detail = str(error.get("error_message") or "").strip()
    index = error.get("index")
    position = f"{index} 番目の文で失敗しました。" if isinstance(index, int) else ""
    oracle_detail = f"Oracle の応答: {detail}" if detail else "Oracle の応答はありません。"
    hint = _oracle_error_hint(detail)
    return f"{lead}{hint}（{position}{oracle_detail}）"


def _oracle_error_hint(detail: str) -> str:
    """Oracle の error に対応する、利用者向けの次の操作の案内（無ければ空）。"""

    match = _ORACLE_MISSING_PRINCIPAL_RE.search(detail)
    if match is None:
        return ""
    name = (match.group("name") or "").strip('"').upper()
    if name == DEEPSEC_DATA_ROLE:
        return (
            f"DATA ROLE {DEEPSEC_DATA_ROLE} がまだ Oracle にありません。"
            "「基盤構成」で V001.1 を適用してから、もう一度実行してください。"
        )
    return (
        "DATA USER（END USER）がまだ Oracle にありません。「DATA USER 認証」で "
        "DATA USER パスワードを保存して「Oracle へ同期」を実行するか、「基盤構成」で "
        "V001.1 をもう一度適用してください"
        "（V001.1 は保存済みのパスワードで DATA USER を作ります）。"
    )


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _managed_data_grant_grantee(settings: Settings) -> str:
    _ = settings
    return DEEPSEC_DATA_ROLE


def _legacy_direct_end_user_data_grant_grantee(settings: Settings) -> str:
    return _strict_identifier(settings.oracle_deepsec_data_user)


def _oracle_base_type(data_type: str) -> str:
    normalized = data_type.strip().upper()
    if normalized.startswith("TIMESTAMP"):
        return "TIMESTAMP"
    return normalized.split("(", 1)[0].split(" ", 1)[0]


def _scope_value_type(data_type: str) -> str:
    base_type = _oracle_base_type(data_type)
    if base_type in _DEEPSEC_TEXT_TYPES:
        return "TEXT"
    if base_type in _DEEPSEC_NUMBER_TYPES:
        return "NUMBER"
    if base_type in _DEEPSEC_TEMPORAL_TYPES:
        return "TEMPORAL"
    return ""


def _like_pattern(value: str, *, prefix: str, suffix: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"{prefix}{escaped}{suffix}"


# NL2SQL・共通基盤・他製品のシステムテーブルと Oracle の内部 object（名前に $ / #）は SQL で除く。
# Python だけで落とすと、同じ schema の `AGENT_` / `PLATFORM_` / `RAG_` の表が 1 ページの大半を
# 占めて view が次のページに回り、件数もずれる（#1708）。条件は他の一覧と共通（#1710）。
_SYSTEM_OBJECT_SQL_FILTERS = user_visible_object_sql_filters(
    object_column="o.object_name", owner_column="o.owner"
)


def _encode_target_object_cursor(owner: str, object_name: str) -> str:
    """対象 object の keyset カーソル（共通の codec。#1266）。"""
    return encode_cursor({"owner": owner, "object_name": object_name})


def _decode_target_object_cursor(cursor: str) -> tuple[str, str]:
    try:
        payload = decode_cursor(cursor, required=("owner", "object_name"))
    except InvalidCursorError as exc:
        raise SecurityApiError(422, "対象 object の cursor が不正です。") from exc
    value = (payload or {}).get("owner"), (payload or {}).get("object_name")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise SecurityApiError(422, "対象 object の cursor が不正です。")
    # cursor はカタログ上の名前（引用符なし・大文字小文字を保持）を持つ。
    return (
        normalize_object_part(_catalog_identifier_token(str(value[0]))),
        normalize_object_part(_catalog_identifier_token(str(value[1]))),
    )


def _deepsec_target_visible(owner: str, object_name: str) -> bool:
    """owner / object はカタログ上の名前（引用符なし・大文字小文字を保持）で受け取る。"""

    owner_name = str(owner or "")
    if owner_name.strip().upper() in _DEEPSEC_INTERNAL_OWNER_NAMES:
        return False
    try:
        return is_user_visible_schema_object(
            format_object_part(owner_name),
            format_object_part(str(object_name or "")),
        )
    except ValueError:
        return False


def _non_empty_value(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise SecurityApiError(400, f"{label} を指定してください。")
    if any(ord(char) < 32 for char in normalized):
        raise SecurityApiError(400, f"{label} に制御文字は使用できません。")
    return normalized


def _number_literal(value: str) -> str:
    normalized = _non_empty_value(value, "数値 scope 値")
    if not _NUMBER_LITERAL_RE.fullmatch(normalized):
        raise SecurityApiError(400, "数値 scope 値は数値 literal で指定してください。")
    try:
        Decimal(normalized)
    except InvalidOperation as exc:
        raise SecurityApiError(400, "数値 scope 値は数値 literal で指定してください。") from exc
    return normalized.removeprefix("+").upper()


def _positive_integer_literal(value: str) -> str:
    normalized = _non_empty_value(value, "正整数 scope 値")
    if not _POSITIVE_INTEGER_LITERAL_RE.fullmatch(normalized):
        raise SecurityApiError(400, "NUMBER の EQ scope 値は正整数で指定してください。")
    return normalized


def _normalize_temporal_value(value: str) -> tuple[str, bool]:
    normalized = _non_empty_value(value, "日付/時刻 scope 値").replace("T", " ")
    if _DATE_LITERAL_RE.fullmatch(normalized):
        try:
            datetime.strptime(normalized, "%Y-%m-%d")
        except ValueError as exc:
            raise SecurityApiError(400, "日付 scope 値は YYYY-MM-DD で指定してください。") from exc
        return normalized, False
    if _DATETIME_LITERAL_RE.fullmatch(normalized):
        if len(normalized) == 16:
            normalized = f"{normalized}:00"
        try:
            datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise SecurityApiError(
                400, "日付/時刻 scope 値は YYYY-MM-DD HH:MM[:SS] で指定してください。"
            ) from exc
        return normalized, True
    raise SecurityApiError(
        400, "日付/時刻 scope 値は YYYY-MM-DD または YYYY-MM-DD HH:MM[:SS] で指定してください。"
    )


def _temporal_literal(value: str, data_type: str) -> str:
    normalized, has_time = _normalize_temporal_value(value)
    if _oracle_base_type(data_type) == "DATE":
        if not has_time:
            return f"DATE {_sql_literal(normalized)}"
        return f"TO_DATE({_sql_literal(normalized)}, 'YYYY-MM-DD HH24:MI:SS')"
    if not has_time:
        normalized = f"{normalized} 00:00:00"
    return f"TIMESTAMP {_sql_literal(normalized)}"


def _scope_filter_literal(
    filter_item: DataEntitlementScopeFilter,
    *,
    data_type: str,
    value: str,
) -> str:
    if filter_item.value_type == "TEXT":
        return _sql_literal(_non_empty_value(value, "文字列 scope 値"))
    if filter_item.value_type == "NUMBER":
        return _number_literal(value)
    if filter_item.value_type == "TEMPORAL":
        return _temporal_literal(value, data_type)
    raise SecurityApiError(400, "scope filter value_type が不正です。")


def _scope_filter_values(
    filter_item: DataEntitlementScopeFilter,
    *,
    data_type: str,
) -> list[str]:
    values = [
        _scope_filter_literal(filter_item, data_type=data_type, value=value)
        for value in filter_item.values
        if str(value).strip()
    ]
    if not values:
        raise SecurityApiError(400, "IN scope filter は 1 件以上の値を指定してください。")
    if len(values) > _DEEPSEC_MAX_SCOPE_FILTER_VALUES:
        raise SecurityApiError(
            400,
            f"IN scope filter の値は {_DEEPSEC_MAX_SCOPE_FILTER_VALUES} 件以内で指定してください。",
        )
    return values


def _scope_filter_predicate(
    target: str,
    filter_item: DataEntitlementScopeFilter,
    *,
    data_type: str,
) -> str:
    column = f"{target}.{_identifier_token(filter_item.column_name)}"
    operator = filter_item.operator.strip().upper()
    value_type = filter_item.value_type.strip().upper()
    value_source = filter_item.value_source.strip().upper() or "LITERAL"
    if value_source == LEGACY_APP_USER_ID_SCOPE_VALUE_SOURCE:
        value_source = LOGIN_USER_ID_SCOPE_VALUE_SOURCE
    normalized_filter = replace(
        filter_item,
        operator=operator,
        value_type=value_type,
        value_source=value_source,
    )
    if value_source not in _DEEPSEC_SCOPE_VALUE_SOURCES:
        raise SecurityApiError(400, "scope filter value_source が不正です。")
    if operator not in _DEEPSEC_SCOPE_OPERATORS_BY_TYPE.get(value_type, frozenset()):
        raise SecurityApiError(400, "scope filter operator が列型に対応していません。")
    if value_source == LOGIN_USER_ID_SCOPE_VALUE_SOURCE:
        if value_type not in {"TEXT", "NUMBER"} or operator != "EQ":
            raise SecurityApiError(
                400,
                "ログインユーザーID scope 値は文字列列または NUMBER 列の "
                "EQ 条件でのみ指定できます。",
            )
        context_value = _DEEPSEC_LOGIN_USER_ID_CONTEXT_EXPR
        if value_type == "NUMBER":
            numeric_context_value = (
                f"CASE WHEN REGEXP_LIKE({context_value}, '^[0-9]+$') "
                f"THEN TO_NUMBER({context_value}) END"
            )
            return f"{column} = {numeric_context_value}"
        return f"{column} = {context_value}"
    if operator == "IS_NULL":
        return f"{column} IS NULL"
    if operator == "IS_NOT_NULL":
        return f"{column} IS NOT NULL"
    if operator == "IN":
        values = ", ".join(_scope_filter_values(normalized_filter, data_type=data_type))
        return f"{column} IN ({values})"
    if operator == "BETWEEN":
        start = _scope_filter_literal(
            normalized_filter,
            data_type=data_type,
            value=normalized_filter.value,
        )
        end = _scope_filter_literal(
            normalized_filter,
            data_type=data_type,
            value=normalized_filter.value_to,
        )
        return f"{column} BETWEEN {start} AND {end}"
    if value_type == "TEXT" and operator in {"CONTAINS", "STARTS_WITH"}:
        prefix = "%" if operator == "CONTAINS" else ""
        pattern = _like_pattern(
            _non_empty_value(normalized_filter.value, "文字列 scope 値"),
            prefix=prefix,
            suffix="%",
        )
        escape_literal = _sql_literal("\\")
        return f"{column} LIKE {_sql_literal(pattern)} ESCAPE {escape_literal}"
    if value_type == "NUMBER" and operator == "EQ":
        value = _positive_integer_literal(normalized_filter.value)
    else:
        value = _scope_filter_literal(
            normalized_filter,
            data_type=data_type,
            value=normalized_filter.value,
        )
    operator_sql = {
        "EQ": "=",
        "NE": "<>",
        "GT": ">",
        "GTE": ">=",
        "LT": "<",
        "LTE": "<=",
        "BEFORE": "<",
        "ON_OR_BEFORE": "<=",
        "AFTER": ">",
        "ON_OR_AFTER": ">=",
    }.get(operator)
    if operator_sql is None:
        raise SecurityApiError(400, "scope filter operator が不正です。")
    return f"{column} {operator_sql} {value}"


def _qualified(owner: str, name: str) -> str:
    """canonical な `OWNER.OBJECT`。保存キー・比較キー・SQL 識別子を兼ねる。"""

    return f"{_identifier_token(owner)}.{_identifier_token(name)}"


def _disable_data_grants_only_statement(target_owner: str, target_object: str) -> str:
    target = _qualified(target_owner, target_object)
    return _single_quoted_identifier_sql(
        """
        DECLARE
          v_count NUMBER;
        BEGIN
          SELECT COUNT(*) INTO v_count FROM ALL_OBJECTS
           WHERE OWNER = '{target_owner}'
             AND OBJECT_NAME = '{target_object}'
             AND OBJECT_TYPE IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW');
          IF v_count > 0 THEN
            EXECUTE IMMEDIATE 'SET USE DATA GRANTS ONLY ON {target} DISABLED';
          END IF;
        END;
        """,
        target_owner=_identifier_name(target_owner),
        target_object=_identifier_name(target_object),
        target=target,
    )


def _revoke_target_select_statement(target_owner: str, target_object: str) -> str:
    """対象の SELECT を DATA USER の DB role から外し、外れたことを確かめる（#1022）。

    `USE DATA GRANTS ONLY` を無効にした表では、DB role の SELECT（object privilege）だけで
    DATA USER が全行を読める。無効にする前にこの文を実行し、SELECT が残っていたら
    （別の grantor の grant など）エラーで止めて、無効化へ進ませない（fail-closed）。
    """

    target = _qualified(target_owner, target_object)
    return _single_quoted_identifier_sql(
        """
        DECLARE
          v_count NUMBER;
        BEGIN
          SELECT COUNT(*) INTO v_count FROM DBA_TAB_PRIVS
           WHERE OWNER = '{target_owner}'
             AND TABLE_NAME = '{target_object}'
             AND GRANTEE = '{db_role}'
             AND PRIVILEGE = 'SELECT';
          IF v_count > 0 THEN
            EXECUTE IMMEDIATE 'REVOKE SELECT ON {target} FROM {db_role}';
            SELECT COUNT(*) INTO v_count FROM DBA_TAB_PRIVS
             WHERE OWNER = '{target_owner}'
               AND TABLE_NAME = '{target_object}'
               AND GRANTEE = '{db_role}'
               AND PRIVILEGE = 'SELECT';
            IF v_count > 0 THEN
              RAISE_APPLICATION_ERROR(
                -20003,
                'SELECT on {target} is still granted to {db_role}'
              );
            END IF;
          END IF;
        END;
        """,
        target_owner=_identifier_name(target_owner),
        target_object=_identifier_name(target_object),
        target=target,
        db_role=DEEPSEC_DB_ROLE,
    )


def _data_grant_checksum(statements: tuple[str, ...]) -> str:
    payload = "\n-- statement --\n".join(statement.strip() for statement in statements)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _data_grant_name(entitlement: DataEntitlementRecord) -> str:
    source = "|".join(
        (
            entitlement.entitlement_id,
            entitlement.role_id,
            entitlement.target_owner,
            entitlement.target_object,
            entitlement.scope_code,
        )
    )
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:24].upper()
    return f"{MANAGED_DATA_GRANT_PREFIX}{digest}"


def _is_real_data_entitlement(entitlement: DataEntitlementRecord) -> bool:
    return bool(entitlement.target_owner and entitlement.target_object)


def _is_complete_data_entitlement(entitlement: DataEntitlementRecord) -> bool:
    if not _is_real_data_entitlement(entitlement):
        return False
    if not entitlement.column_names:
        return False
    if entitlement.scope_mode == "EXPRESSION":
        return bool(entitlement.scope_expression)
    if entitlement.scope_mode == "FILTERS":
        return bool(entitlement.scope_filters)
    return not (
        entitlement.scope_mode == "COLUMN_EQUALS"
        and (not entitlement.scope_column or not entitlement.scope_code)
    )


def _uses_login_user_id_scope(entitlement: DataEntitlementRecord) -> bool:
    if entitlement.scope_mode == "EXPRESSION":
        from .scope_expression import expression_uses_login

        return expression_uses_login(entitlement.scope_expression)
    return any(
        (filter_item.value_source.strip().upper() or "LITERAL")
        in {LOGIN_USER_ID_SCOPE_VALUE_SOURCE, LEGACY_APP_USER_ID_SCOPE_VALUE_SOURCE}
        for filter_item in entitlement.scope_filters
    )


def build_data_entitlement_statements(
    settings: Settings,
    entitlement: DataEntitlementRecord,
    *,
    column_types: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    owner = _strict_identifier(settings.oracle_user)
    data_grant_grantee = _managed_data_grant_grantee(settings)
    target = _qualified(entitlement.target_owner, entitlement.target_object)
    grant_name = entitlement.data_grant_name or _data_grant_name(entitlement)
    grant = f"{owner}.{_strict_identifier(grant_name)}"
    if not entitlement.column_names:
        raise SecurityApiError(400, "Data Grant に含める列を選択してください。")
    columns = ", ".join(_identifier_token(column) for column in entitlement.column_names)
    entitlement_id_literal = _sql_literal(entitlement.entitlement_id)
    role_id_literal = _sql_literal(entitlement.role_id)
    # owner は Oracle identifier whitelist、entitlement/role は SQL literal escape 済み。
    predicate = (
        "EXISTS (\n"
        "  SELECT 1\n"  # nosec B608
        f"    FROM {owner}.PLATFORM_USER_ROLES ur\n"
        f"    JOIN {owner}.PLATFORM_ROLES r ON r.ROLE_ID = ur.ROLE_ID\n"
        f"    JOIN {owner}.NL2SQL_APP_DATA_ENTITLEMENTS e ON e.ROLE_ID = r.ROLE_ID\n"
        f"   WHERE ur.USER_UUID = {_DEEPSEC_APP_USER_CONTEXT_EXPR}\n"
        "     AND r.ARCHIVED = 0\n"
        f"     AND e.ENTITLEMENT_ID = {entitlement_id_literal}\n"
        f"     AND e.ROLE_ID = {role_id_literal}\n"
        "     AND e.CAPABILITY = 'SELECT'\n"
        "     AND e.APPLY_STATUS = 'APPLIED'"
    )
    scope_mode = entitlement.scope_mode.strip().upper() or "ALL"
    if scope_mode == "COLUMN_EQUALS":
        scope_column = _identifier_token(entitlement.scope_column)
        predicate += f"\n             AND {target}.{scope_column} = e.SCOPE_CODE"
    elif scope_mode == "FILTERS":
        type_by_column = {
            _identifier_token(str(column)): str(data_type)
            for column, data_type in (column_types or {}).items()
        }
        for filter_item in entitlement.scope_filters:
            column_name = _identifier_token(filter_item.column_name)
            data_type = type_by_column.get(column_name, filter_item.value_type)
            filter_predicate = _scope_filter_predicate(
                target,
                filter_item,
                data_type=data_type,
            )
            predicate += f"\n             AND {filter_predicate}"
    elif scope_mode == "EXPRESSION":
        from .scope_expression import compile_expression

        predicate += "\n             AND " + compile_expression(entitlement, column_types or {})
    elif scope_mode != "ALL":
        raise SecurityApiError(
            400, "scope_mode は ALL、COLUMN_EQUALS、FILTERS、EXPRESSION のいずれかです。"
        )
    predicate += "\n        )"
    if len(predicate.strip()) > DATA_GRANT_PREDICATE_MAX_LENGTH:
        raise SecurityApiError(
            400,
            "Data Grant predicate は Oracle の上限 4000 文字以内にしてください。",
        )
    # 順序は fail-closed にする（#1022）。先に `USE DATA GRANTS ONLY` を有効にしてから
    # Data Grant を作り、DB role の SELECT は最後に付ける。途中で止まっても、DATA USER が
    # 行の制限なしに読める状態（SELECT があり、DATA GRANTS ONLY が無効）を作らない。
    return (
        f"SET USE DATA GRANTS ONLY ON {target} ENABLED",
        f"DROP DATA GRANT IF EXISTS {grant}",
        f"""
        CREATE OR REPLACE DATA GRANT {grant}
          AS SELECT ({columns})
          ON {target}
          WHERE {predicate.strip()}
          TO {data_grant_grantee}
        """,
        f"GRANT SELECT ON {target} TO {DEEPSEC_DB_ROLE}",
    )


def build_data_entitlement_preview(
    settings: Settings,
    entitlement: DataEntitlementRecord,
    *,
    column_types: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    if not _is_complete_data_entitlement(entitlement):
        return ()
    return tuple(
        _preview_statement(item)
        for item in build_data_entitlement_statements(
            settings,
            entitlement,
            column_types=column_types,
        )
    )


def _preview_statement(statement: str) -> str:
    return re.sub(r"\s+$", "", statement.strip())


def _data_user_password_sync_statement(settings: Settings) -> str:
    data_user = _strict_identifier(settings.oracle_deepsec_data_user)
    owner = _strict_identifier(settings.oracle_user)
    return (
        f"ALTER END USER IF EXISTS {data_user} IDENTIFIED BY {PASSWORD_PLACEHOLDER} "
        f"ACCOUNT UNLOCK SCHEMA {owner}"
    )


def _data_user_create_statement(settings: Settings) -> str:
    data_user = _strict_identifier(settings.oracle_deepsec_data_user)
    owner = _strict_identifier(settings.oracle_user)
    return (
        f"CREATE END USER IF NOT EXISTS {data_user} IDENTIFIED BY {PASSWORD_PLACEHOLDER} "
        f"SCHEMA {owner}"
    )


def _deepsec_env_snapshot_locked(env_path: Path) -> tuple[bool, str]:
    if not env_path.exists():
        return False, ""
    return True, env_path.read_text(encoding="utf-8")


def _deepsec_env_snapshot() -> tuple[bool, str]:
    try:
        with locked_env_file(_BACKEND_ENV_FILE) as env_path:
            return _deepsec_env_snapshot_locked(env_path)
    except OSError as exc:
        raise SecurityApiError(
            500,
            "DeepSec DATA USER 認証情報を backend/.env から読み取れませんでした。",
        ) from exc


def _restore_deepsec_env_snapshot_locked(env_path: Path, snapshot: tuple[bool, str]) -> None:
    existed, content = snapshot
    if existed:
        replace_env_file(env_path, content)
        return
    with suppress(FileNotFoundError):
        env_path.unlink()


def _restore_deepsec_env_snapshot(snapshot: tuple[bool, str]) -> None:
    try:
        with locked_env_file(_BACKEND_ENV_FILE) as env_path:
            _restore_deepsec_env_snapshot_locked(env_path, snapshot)
    except OSError as exc:
        raise SecurityApiError(
            500,
            "DeepSec DATA USER 認証情報を backend/.env へ復元できませんでした。",
        ) from exc


def build_v001_plan(settings: Settings) -> tuple[DeepSecStep, ...]:
    owner = _strict_identifier(settings.oracle_user)
    data_user = _strict_identifier(settings.oracle_deepsec_data_user)
    role = "NL2SQL_APP_DB_ROLE"
    data_role = "NL2SQL_APP_DATA_ROLE"

    role_step = DeepSecStep(
        step_no=1,
        key="principals_and_roles",
        title="共有 DATA USER とロール",
        description=(
            "保存済みの DATA USER パスワードで DATA USER（END USER）を作り"
            "（既にあれば作り直さない）、関連付ける最小 DB role と local DATA ROLE を準備します。"
        ),
        statements=(
            f"CREATE ROLE {role}",
            f"GRANT CREATE SESSION TO {role}",
            *(
                f"GRANT SELECT ON {owner}.{table_name} TO {role}"
                for _key, table_name in _DEEPSEC_PREDICATE_TABLE_GRANTS
            ),
            f"CREATE DATA ROLE IF NOT EXISTS {data_role}",
            f"GRANT {role} TO {data_role}",
            # 新しい配備では END USER がまだ無く、DATA ROLE の付与が ORA-52516 になる（#1676）。
            # 保存済みのパスワードで先に作る。既にあれば何もしない（パスワードの同期は
            # 「DATA USER 認証」の「Oracle へ同期」のまま）。
            # 計画とチェックサムは placeholder のまま。
            _data_user_create_statement(settings),
            f"GRANT DATA ROLE {data_role} TO {data_user}",
        ),
        ignored_error_codes=frozenset({"ORA-01921"}),
    )
    context_package_spec = _trusted_identifier_sql(
        """
        CREATE OR REPLACE PACKAGE {owner}.NL2SQL_DEEPSEC_CTX_PKG AUTHID DEFINER AS
          PROCEDURE SET_APP_USER_UUID(p_user_uuid IN VARCHAR2);
          PROCEDURE CLEAR_APP_USER;
        END NL2SQL_DEEPSEC_CTX_PKG;
        """,
        owner=owner,
    )
    context_package_body = _trusted_identifier_sql(
        """
        CREATE OR REPLACE PACKAGE BODY {owner}.NL2SQL_DEEPSEC_CTX_PKG AS
          PROCEDURE SET_APP_USER_UUID(p_user_uuid IN VARCHAR2) IS
            v_login_user_id VARCHAR2(64);
          BEGIN
            SELECT LOGIN_USER_ID INTO v_login_user_id
              FROM {owner}.PLATFORM_USERS
             WHERE USER_UUID = p_user_uuid AND STATUS = 'ACTIVE';
            DBMS_SESSION.SET_CONTEXT('NL2SQL_APP_USER_CTX', 'LOGIN_USER_ID', v_login_user_id);
            DBMS_SESSION.SET_CONTEXT('NL2SQL_APP_USER_CTX', 'APP_USER_ID', NULL);
            DBMS_SESSION.SET_IDENTIFIER(p_user_uuid);
          EXCEPTION
            WHEN NO_DATA_FOUND THEN
              RAISE_APPLICATION_ERROR(-20001, 'invalid application user');
          END SET_APP_USER_UUID;

          PROCEDURE CLEAR_APP_USER IS
          BEGIN
            DBMS_SESSION.SET_CONTEXT('NL2SQL_APP_USER_CTX', 'LOGIN_USER_ID', NULL);
            DBMS_SESSION.SET_CONTEXT('NL2SQL_APP_USER_CTX', 'APP_USER_ID', NULL);
            DBMS_SESSION.CLEAR_IDENTIFIER;
          END CLEAR_APP_USER;
        END NL2SQL_DEEPSEC_CTX_PKG;
        """,
        owner=owner,
    )
    context_package_compile_check = _trusted_identifier_sql(
        """
        DECLARE
          v_error_count NUMBER;
          v_first_error VARCHAR2(1000);
        BEGIN
          SELECT COUNT(*),
                 MIN('line ' || LINE || ':' || POSITION || ' ' || TEXT)
                   KEEP (DENSE_RANK FIRST ORDER BY SEQUENCE)
            INTO v_error_count, v_first_error
            FROM ALL_ERRORS
           WHERE OWNER = '{owner}'
             AND NAME = 'NL2SQL_DEEPSEC_CTX_PKG'
             AND TYPE IN ('PACKAGE', 'PACKAGE BODY');
          IF v_error_count > 0 THEN
            RAISE_APPLICATION_ERROR(
              -20002,
              'NL2SQL_DEEPSEC_CTX_PKG compile error: ' || SUBSTR(v_first_error, 1, 900)
            );
          END IF;
        END;
        """,
        owner=owner,
    )
    context_step = DeepSecStep(
        step_no=2,
        key="application_context",
        title="アプリケーションコンテキスト",
        description="認証済み application user UUID を検証して session context へ設定します。",
        statements=(
            context_package_spec,
            context_package_body,
            context_package_compile_check,
            f"CREATE OR REPLACE CONTEXT NL2SQL_APP_USER_CTX USING {owner}.NL2SQL_DEEPSEC_CTX_PKG",
            f"GRANT EXECUTE ON {owner}.NL2SQL_DEEPSEC_CTX_PKG TO {role}",
            _trusted_identifier_sql(
                """
            DECLARE v_package NUMBER;
            BEGIN
              SELECT COUNT(*) INTO v_package FROM ALL_OBJECTS
               WHERE OWNER = '{owner}' AND OBJECT_NAME = 'NL2SQL_ONT_ACTION_TX'
                 AND OBJECT_TYPE = 'PACKAGE';
              IF v_package > 0 THEN
                EXECUTE IMMEDIATE 'GRANT EXECUTE ON {owner}.NL2SQL_ONT_ACTION_TX TO {role}';
              END IF;
            END;
            """,
                owner=owner,
                role=role,
            ),
        ),
    )
    return (role_step, context_step)


def build_v001_reset_statements(
    settings: Settings,
    entitlements: list[DataEntitlementRecord] | tuple[DataEntitlementRecord, ...] = (),
) -> tuple[str, ...]:
    owner = _strict_identifier(settings.oracle_user)
    data_user = _strict_identifier(settings.oracle_deepsec_data_user)
    role = DEEPSEC_DB_ROLE
    data_role = DEEPSEC_DATA_ROLE
    probe = f"{owner}.NL2SQL_DEEPSEC_PROBE"
    managed_targets: list[tuple[str, str, str]] = []
    managed_target_tokens: list[tuple[str, str]] = []
    managed_grants: list[str] = []
    seen_targets: set[tuple[str, str]] = set()
    seen_grants: set[str] = set()
    for entitlement in entitlements:
        if not _is_real_data_entitlement(entitlement):
            continue
        target_owner = _identifier_token(entitlement.target_owner)
        target_object = _identifier_token(entitlement.target_object)
        target_key = (target_owner, target_object)
        if target_key not in seen_targets:
            seen_targets.add(target_key)
            managed_target_tokens.append(target_key)
            managed_targets.append(
                (
                    _identifier_name(target_owner),
                    _identifier_name(target_object),
                    f"{target_owner}.{target_object}",
                )
            )
        data_grant_name = entitlement.data_grant_name or _data_grant_name(entitlement)
        data_grant_name = _strict_identifier(data_grant_name)
        if data_grant_name not in seen_grants:
            seen_grants.add(data_grant_name)
            managed_grants.append(data_grant_name)
    managed_disable_statements = tuple(
        _single_quoted_identifier_sql(
            """
            DECLARE
              v_count NUMBER;
            BEGIN
              SELECT COUNT(*) INTO v_count FROM ALL_OBJECTS
               WHERE OWNER = '{target_owner}'
                 AND OBJECT_NAME = '{target_object}'
                 AND OBJECT_TYPE IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW');
              IF v_count > 0 THEN
                EXECUTE IMMEDIATE 'SET USE DATA GRANTS ONLY ON {target} DISABLED';
              END IF;
            END;
            """,
            target_owner=target_owner,
            target_object=target_object,
            target=target,
        )
        for target_owner, target_object, target in managed_targets
    )
    # 解除の途中で止まっても DATA USER が全行を読めないよう、DATA GRANTS ONLY を無効にする前に
    # 対象の SELECT を DB role から外す（#1022）。DB role 自体は最後に DROP する。
    managed_revoke_statements = tuple(
        _revoke_target_select_statement(target_owner, target_object)
        for target_owner, target_object in managed_target_tokens
    )
    managed_drop_statements = tuple(
        f"DROP DATA GRANT IF EXISTS {owner}.{grant_name}" for grant_name in managed_grants
    )
    disable_probe_data_grants_only = _trusted_identifier_sql(
        """
        DECLARE
          v_count NUMBER;
        BEGIN
          SELECT COUNT(*) INTO v_count FROM ALL_TABLES
           WHERE OWNER = '{owner}' AND TABLE_NAME = 'NL2SQL_DEEPSEC_PROBE';
          IF v_count > 0 THEN
            EXECUTE IMMEDIATE 'SET USE DATA GRANTS ONLY ON {probe} DISABLED';
          END IF;
        END;
        """,
        owner=owner,
        probe=probe,
    )
    drop_probe_table = _trusted_identifier_sql(
        """
        DECLARE
          v_count NUMBER;
        BEGIN
          SELECT COUNT(*) INTO v_count FROM ALL_TABLES
           WHERE OWNER = '{owner}' AND TABLE_NAME = 'NL2SQL_DEEPSEC_PROBE';
          IF v_count > 0 THEN
            EXECUTE IMMEDIATE 'DROP TABLE {probe} CASCADE CONSTRAINTS PURGE';
          END IF;
        END;
        """,
        owner=owner,
        probe=probe,
    )
    drop_context = """
        DECLARE
          v_count NUMBER;
        BEGIN
          SELECT COUNT(*) INTO v_count FROM DBA_CONTEXT
           WHERE NAMESPACE = 'NL2SQL_APP_USER_CTX';
          IF v_count > 0 THEN
            EXECUTE IMMEDIATE 'DROP CONTEXT NL2SQL_APP_USER_CTX';
          END IF;
        END;
        """
    drop_context_package = _trusted_identifier_sql(
        """
        DECLARE
          v_count NUMBER;
        BEGIN
          SELECT COUNT(*) INTO v_count FROM ALL_OBJECTS
           WHERE OWNER = '{owner}'
             AND OBJECT_NAME = 'NL2SQL_DEEPSEC_CTX_PKG'
             AND OBJECT_TYPE = 'PACKAGE';
          IF v_count > 0 THEN
            EXECUTE IMMEDIATE 'DROP PACKAGE {owner}.NL2SQL_DEEPSEC_CTX_PKG';
          END IF;
        END;
        """,
        owner=owner,
    )
    drop_role = _trusted_identifier_sql(
        """
        DECLARE
          v_count NUMBER;
        BEGIN
          SELECT COUNT(*) INTO v_count FROM DBA_ROLES
           WHERE ROLE = '{role}';
          IF v_count > 0 THEN
            EXECUTE IMMEDIATE 'DROP ROLE {role}';
          END IF;
        END;
        """,
        role=role,
    )
    return (
        *managed_revoke_statements,
        *managed_disable_statements,
        *managed_drop_statements,
        disable_probe_data_grants_only,
        f"DROP DATA GRANT IF EXISTS {owner}.NL2SQL_DEEPSEC_PROBE_SENSITIVE",
        f"DROP DATA GRANT IF EXISTS {owner}.NL2SQL_DEEPSEC_PROBE_ROWS",
        drop_probe_table,
        drop_context,
        drop_context_package,
        f"DROP END USER IF EXISTS {data_user}",
        f"DROP DATA ROLE IF EXISTS {data_role}",
        drop_role,
    )


class DeepSecService:
    def __init__(
        self,
        settings: Settings,
        security: SecurityService,
        pools: OraclePoolManager,
    ) -> None:
        self.settings = settings
        self.security = security
        self.pools = pools

    def plan(self) -> dict[str, object]:
        try:
            states = self.security.store.get_deepsec_states()
        except Exception as exc:
            self.security._raise_security_migration_if_needed(exc)  # noqa: SLF001
            raise
        steps = []
        foundation_verified = self.status().get("configured") is True
        for step in build_v001_plan(self.settings):
            state = self._state_for_step(states, step)
            recorded = states.get((PLAN_VERSION, step.step_no), {})
            if (
                foundation_verified
                and step.step_no in {1, 2}
                and (not recorded or recorded.get("checksum") == step.checksum)
            ):
                state = {**state, "status": "APPLIED", "error_message": ""}
            steps.append(
                {
                    "step_no": step.step_no,
                    "key": step.key,
                    "title": step.title,
                    "description": step.description,
                    "checksum": step.checksum,
                    "status": state.get("status", "PENDING"),
                    "error_message": state.get("error_message", ""),
                    "executed_at": state.get("executed_at"),
                    "sql": [self._preview_sql(statement) for statement in step.statements],
                }
            )
        return {
            "version": PLAN_VERSION,
            "driver_mode": self.settings.oracle_driver_mode,
            "connection_security": self.settings.oracle_connection_security,
            "deepsec_enabled": self.settings.oracle_deepsec_enabled,
            "data_user": self.settings.oracle_deepsec_data_user,
            "has_data_user_password": bool(self.settings.oracle_deepsec_data_user_password),
            "steps": steps,
        }

    def data_entitlements(self) -> list[dict[str, object]]:
        try:
            roles = self.security.list_roles(include_archived=True)
        except Exception as exc:
            if _looks_like_missing_scope_filters_column(exc):
                raise SecurityApiError(
                    409,
                    "DeepSec Data Grant の schema migration が未適用です。"
                    "app_security_migrate を実行して "
                    "SCOPE_FILTERS / SCOPE_EXPRESSION 列を作成してください。",
                ) from exc
            raise
        return [self._role_entitlements_payload(role) for role in roles]

    def role_entitlements(self, role: RoleRecord) -> dict[str, object]:
        return self._role_entitlements_payload(role)

    def target_objects(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        q: str = "",
        owner_prefix: str = "",
        include_counts: bool = False,
        profile_id: str | None = None,
    ) -> dict[str, object]:
        normalized_limit = min(max(limit, 1), 100)
        owner_filter, owner_binds = self._target_object_owner_filter("o.owner")
        filters = [
            owner_filter,
            "o.object_type IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW')",
            "o.status = 'VALID'",
            "NVL(u.oracle_maintained, 'N') = 'N'",
            "o.owner NOT IN ('SYS', 'SYSTEM')",
            *_SYSTEM_OBJECT_SQL_FILTERS,
        ]
        binds: dict[str, object] = dict(owner_binds)
        if profile_id:
            from .scope_relations import scope_profiles

            profiles = [p for p in scope_profiles() if p["id"] == profile_id]
            if not profiles:
                raise SecurityApiError(404, "有効な業務プロファイルが見つかりません。")
            names = profiles[0]["objects"]
            terms = []
            for i, name in enumerate(names):
                try:
                    identity = parse_object_identity(str(name))
                except ValueError:
                    continue
                # 連結文字列で比較すると引用名・dot を含む名前を取り違えるため、部分ごとに比べる。
                binds[f"profile_owner_{i}"] = identity.owner
                binds[f"profile_object_{i}"] = identity.object_name
                terms.append(
                    f"(o.owner = :profile_owner_{i} AND o.object_name = :profile_object_{i})"
                )
            filters.append("(" + " OR ".join(terms) + ")" if terms else "1 = 0")
        normalized_owner_prefix = owner_prefix.strip().upper()
        if normalized_owner_prefix:
            filters.append("UPPER(o.owner) LIKE :owner_prefix ESCAPE '\\'")
            binds["owner_prefix"] = _like_pattern(
                normalized_owner_prefix,
                prefix="",
                suffix="%",
            )
        normalized_query = q.strip().upper()
        if normalized_query:
            # 対象の選択は検索できる選択欄 1 つで探す（#608）。
            # スキーマ名・`OWNER.OBJECT` でも見つかるよう、
            # 所有者を付けた名前とコメントを照合する（オブジェクト名だけの部分一致も含む）。
            filters.append(
                "(UPPER(o.owner || '.' || o.object_name) LIKE :object_query ESCAPE '\\' "
                "OR UPPER(NVL(tc.comments, '')) LIKE :object_query ESCAPE '\\')"
            )
            binds["object_query"] = _like_pattern(normalized_query, prefix="%", suffix="%")
        base_filter_sql = " AND ".join(filters)
        page_filters = list(filters)
        if cursor:
            cursor_owner, cursor_name = _decode_target_object_cursor(cursor)
            page_filters.append(
                "(o.owner > :cursor_owner OR "
                "(o.owner = :cursor_owner AND o.object_name > :cursor_name))"
            )
            binds["cursor_owner"] = cursor_owner
            binds["cursor_name"] = cursor_name
        binds["fetch_limit"] = normalized_limit + 1
        # filter SQL は固定 fragment と bind だけで組み立てる。
        sql = (
            "SELECT owner_name, object_name, object_type, row_count, comments\n"  # nosec B608
            "  FROM (\n"
            "    SELECT o.owner AS owner_name,\n"
            "           o.object_name,\n"
            "           o.object_type,\n"
            "           t.num_rows AS row_count,\n"
            "           NVL(tc.comments, '') AS comments\n"
            "      FROM ALL_OBJECTS o\n"
            "      JOIN ALL_USERS u\n"
            "        ON u.username = o.owner\n"
            "      LEFT JOIN ALL_TABLES t\n"
            "        ON t.owner = o.owner\n"
            "       AND t.table_name = o.object_name\n"
            "      LEFT JOIN ALL_TAB_COMMENTS tc\n"
            "        ON tc.owner = o.owner\n"
            "       AND tc.table_name = o.object_name\n"
            f"     WHERE {' AND '.join(page_filters)}\n"
            "     ORDER BY o.owner, o.object_name\n"
            "  )\n"
            " WHERE ROWNUM <= :fetch_limit"
        )
        count_sql = (
            "SELECT COUNT(*)\n"  # nosec B608
            "  FROM ALL_OBJECTS o\n"
            "  JOIN ALL_USERS u\n"
            "    ON u.username = o.owner\n"
            "  LEFT JOIN ALL_TAB_COMMENTS tc\n"
            "    ON tc.owner = o.owner\n"
            "   AND tc.table_name = o.object_name\n"
            f" WHERE {base_filter_sql}"
        )
        with self.pools.control_connection() as conn, conn.cursor() as db_cursor:
            db_cursor.execute(sql, binds)
            rows = list(db_cursor.fetchall())
            total: int | None = None
            if include_counts:
                count_binds = {
                    key: value
                    for key, value in binds.items()
                    if key not in {"cursor_owner", "cursor_name", "fetch_limit"}
                }
                db_cursor.execute(count_sql, count_binds)
                total = int((db_cursor.fetchone() or (0,))[0] or 0)

        page_rows = rows[:normalized_limit]
        # owner / object_name はカタログ上の値のまま返す。以前は大文字化していたため、
        # "Mixed_Case" が大文字の同名表 MIXED_CASE として選択されていた。
        items = [
            {
                "name": object_name,
                "owner": owner,
                "qualified_name": qualified_object_name(owner, object_name),
                "object_type": object_type,
                "row_count": int(row_count) if row_count is not None else None,
                "comment": comment,
            }
            for row in page_rows
            for owner, object_name, object_type, row_count, comment in [
                (
                    str(row[0] or ""),
                    str(row[1] or ""),
                    str(row[2] or "").upper(),
                    row[3],
                    str(row[4] or ""),
                )
            ]
            if owner and object_name and _deepsec_target_visible(owner, object_name)
        ]
        next_cursor = None
        if len(rows) > normalized_limit and page_rows:
            # Python の可視性の判定で落とした行がページの末尾に続いても次へ進めるよう、
            # 候補ではなく SQL で取った最後の行から作る（#1708）。
            last_row = page_rows[-1]
            next_cursor = _encode_target_object_cursor(
                str(last_row[0] or ""), str(last_row[1] or "")
            )
        return {
            "runtime": "oracle",
            "owner": normalized_owner_prefix,
            "items": items,
            "total": total,
            "counts_included": include_counts,
            "next_cursor": next_cursor,
            "warnings": [],
        }

    def target_object_detail(
        self,
        *,
        owner: str,
        object_name: str,
        object_type: str = "",
    ) -> dict[str, object]:
        target_owner = _identifier_name(owner)
        target_object = _identifier_name(object_name)
        if not _deepsec_target_visible(target_owner, target_object):
            raise SecurityApiError(404, "対象 object が見つかりません。")
        requested_type = object_type.strip().replace("_", " ").upper()
        if requested_type and requested_type not in _DEEPSEC_MANAGED_TARGET_TYPES:
            raise SecurityApiError(400, "対象は TABLE / VIEW / MATERIALIZED VIEW のみです。")

        owner_filter, owner_binds = self._target_object_owner_filter("o.owner")
        filters = [
            owner_filter,
            "o.owner = :owner",
            "o.object_name = :object_name",
            "o.object_type IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW')",
            "o.status = 'VALID'",
            "NVL(u.oracle_maintained, 'N') = 'N'",
            "o.owner NOT IN ('SYS', 'SYSTEM')",
            *_SYSTEM_OBJECT_SQL_FILTERS,
        ]
        binds: dict[str, object] = {
            **owner_binds,
            "owner": target_owner,
            "object_name": target_object,
        }
        if requested_type:
            filters.append("o.object_type = :object_type")
            binds["object_type"] = requested_type
        with self.pools.control_connection() as conn, conn.cursor() as db_cursor:
            # filter SQL は固定 fragment と bind だけで組み立てる。
            detail_sql = (
                "SELECT o.owner,\n"  # nosec B608
                "       o.object_name,\n"
                "       o.object_type,\n"
                "       t.num_rows,\n"
                "       NVL(tc.comments, '')\n"
                "  FROM ALL_OBJECTS o\n"
                "  JOIN ALL_USERS u\n"
                "    ON u.username = o.owner\n"
                "  LEFT JOIN ALL_TABLES t\n"
                "    ON t.owner = o.owner\n"
                "   AND t.table_name = o.object_name\n"
                "  LEFT JOIN ALL_TAB_COMMENTS tc\n"
                "    ON tc.owner = o.owner\n"
                "   AND tc.table_name = o.object_name\n"
                f" WHERE {' AND '.join(filters)}\n"
                " ORDER BY CASE o.object_type\n"
                "            WHEN 'TABLE' THEN 1\n"
                "            WHEN 'VIEW' THEN 2\n"
                "            ELSE 3\n"
                "          END"
            )
            db_cursor.execute(detail_sql, binds)
            object_row = db_cursor.fetchone()
            if object_row is None:
                raise SecurityApiError(404, "対象 object が見つかりません。")
            db_cursor.execute(
                """
                SELECT c.column_name,
                       c.data_type ||
                       CASE
                         WHEN c.data_type IN ('VARCHAR2','CHAR','NVARCHAR2','NCHAR')
                         THEN '(' || c.data_length || ')'
                         WHEN c.data_type = 'NUMBER' AND c.data_precision IS NOT NULL
                         THEN '(' || c.data_precision ||
                              CASE WHEN c.data_scale > 0 THEN ',' || c.data_scale ELSE '' END || ')'
                         ELSE ''
                       END AS data_type,
                       c.nullable,
                       NVL(cc.comments, '')
                  FROM ALL_TAB_COLUMNS c
                  LEFT JOIN ALL_COL_COMMENTS cc
                    ON cc.owner = c.owner
                   AND cc.table_name = c.table_name
                   AND cc.column_name = c.column_name
                 WHERE c.owner = :owner
                   AND c.table_name = :object_name
                 ORDER BY c.column_id
                """,
                {"owner": target_owner, "object_name": target_object},
            )
            column_rows = list(db_cursor.fetchall())

        actual_owner = str(object_row[0] or "")
        actual_name = str(object_row[1] or "")
        actual_type = str(object_row[2] or "").upper()
        row_count = object_row[3]
        comment = str(object_row[4] or "")
        return {
            "name": actual_name,
            "owner": actual_owner,
            "qualified_name": qualified_object_name(actual_owner, actual_name),
            "object_type": actual_type,
            "row_count": int(row_count) if row_count is not None else None,
            "comment": comment,
            "columns": [
                {
                    # Data Grant の column_names と同じ canonical token で返す。
                    "column_name": _catalog_identifier_token(column_name),
                    "logical_name": str(column_comment or column_name or ""),
                    "data_type": str(data_type or ""),
                    "nullable": str(nullable or "Y").upper() == "Y",
                    "comment": str(column_comment or ""),
                    "sample_values": [],
                }
                for column_name, data_type, nullable, column_comment in column_rows
            ],
            "warnings": [],
        }

    def _target_owner_allowlist(self) -> set[str]:
        return {
            owner.strip().upper()
            for owner in getattr(self.settings, "nl2sql_schema_owner_allowlist", [])
            if owner.strip()
        }

    def _target_owner_allowed(self, owner: str) -> bool:
        """`_target_object_owner_filter` と同じ判定（allowlist が空なら全 schema）。"""
        owners = self._target_owner_allowlist()
        return not owners or owner in owners

    def _target_object_owner_filter(self, column_sql: str) -> tuple[str, dict[str, str]]:
        owners = self._target_owner_allowlist()
        if not owners:
            return "1 = 1", {}
        binds = {f"owner_{index}": owner for index, owner in enumerate(sorted(owners))}
        placeholders = ", ".join(f":{key}" for key in binds)
        return f"{column_sql} IN ({placeholders})", binds

    def preview_data_entitlements(
        self,
        role_id: str,
        *,
        expected_version: int,
        entitlements: list[DataEntitlementRecord],
        actor: Principal,
    ) -> dict[str, object]:
        _ = actor
        if not self.settings.oracle_deepsec_enabled:
            raise SecurityApiError(409, "NL2SQL_ORACLE_DEEPSEC_ENABLED=true を設定してください。")
        role = self._editable_data_entitlement_role(role_id, expected_version=expected_version)
        self.pools.validate_deepsec_control_configuration()
        with self.pools.control_connection() as conn, conn.cursor() as cursor:
            plan = self._build_data_entitlement_sync_plan(role, entitlements, cursor=cursor)
        return {
            "role_id": role_id,
            "version": role.version,
            "data_entitlements": [
                self._data_entitlement_sync_payload(entry) for entry in plan.entries
            ],
            "cleanup_sql": list(plan.cleanup_statements),
            "checksum": plan.checksum,
        }

    def apply_data_entitlements(
        self,
        role_id: str,
        *,
        expected_version: int,
        confirmation: str,
        entitlements: list[DataEntitlementRecord],
        actor: Principal,
    ) -> dict[str, object]:
        if confirmation.strip() != DEEPSEC_APPLY_CONFIRMATION:
            raise SecurityApiError(
                409,
                f"Data Grant の適用には confirmation={DEEPSEC_APPLY_CONFIRMATION} が必要です。",
            )
        if not self.settings.oracle_deepsec_enabled:
            raise SecurityApiError(409, "NL2SQL_ORACLE_DEEPSEC_ENABLED=true を設定してください。")
        role = self._editable_data_entitlement_role(role_id, expected_version=expected_version)
        self.pools.validate_deepsec_control_configuration()
        oracle_started = False
        try:
            with self.pools.control_connection() as conn, conn.cursor() as cursor:
                plan = self._build_data_entitlement_sync_plan(role, entitlements, cursor=cursor)
            results: list[dict[str, object]] = []
            if plan.statements:
                oracle_started = True
                with self.pools.control_connection() as conn:
                    # 失敗した文の後ろは実行しない（#1022）。DDL は rollback できないため、
                    # 途中で止まった状態が fail-closed になる順序（REVOKE → DROP → 無効化、
                    # 有効化 → Data Grant → GRANT）で文を並べている。
                    results = oracle_statement_executor.execute(
                        conn,
                        plan.statements,
                        atomic=False,
                        include_sql=False,
                        stop_on_error=True,
                    )
            errors = [item for item in results if item["status"] == "error"]
            if errors:
                raise SecurityApiError(
                    409,
                    _statement_failure_message(
                        "Data Grant の適用が途中で止まりました。"
                        "DATA USER が行の制限なしに読める状態にはしていません。"
                        "このロールの Data Grant は失敗として記録したため、"
                        "原因を解消してから、もう一度適用してください。",
                        errors[0],
                    ),
                )
            applied_at = datetime.now(UTC)
            applied_entitlements = [
                replace(
                    entry.entitlement,
                    apply_status="APPLIED",
                    apply_error_message="",
                    sql_checksum=entry.checksum,
                    applied_at=applied_at,
                )
                for entry in plan.entries
            ]
            updated = self.security.commit_role_data_entitlement_sync(
                role_id,
                expected_version=expected_version,
                entitlements=applied_entitlements,
                actor=actor,
            )
            close_oracle_pools()
            return {
                "role": self.role_entitlements(updated),
                "status": "APPLIED",
                "checksum": plan.checksum,
                "cleanup_count": len(plan.cleanup_statements),
                "applied_count": len(plan.entries),
            }
        except Exception as exc:
            safe_error = self._safe_error(exc)
            if oracle_started:
                self._mark_role_data_entitlements_failed(
                    role,
                    str(exc.public_message if isinstance(exc, SecurityApiError) else safe_error),
                )
            if isinstance(exc, SecurityApiError):
                raise
            raise SecurityApiError(500, f"Data Grant の適用に失敗しました: {safe_error}") from exc

    def _mark_role_data_entitlements_failed(self, role: RoleRecord, message: str) -> None:
        """Oracle への同期が途中で止まったロールの保存済み Data Grant を FAILED にする（#1022）。

        Oracle 側は一部の文だけが実行された状態のため、保存済みの APPLIED は事実と合わない。
        Data Grant の predicate は `APPLY_STATUS = 'APPLIED'` を条件にしているので、FAILED に
        すると、このロールの Data Grant は再適用が成功するまで行を許可しない（fail-closed）。
        記録できなくても、元の失敗の応答を優先する。
        """
        for entitlement in role.entitlements:
            if not _is_real_data_entitlement(entitlement):
                continue
            try:
                self.security.store.set_deepsec_entitlement_apply_state(
                    entitlement.entitlement_id,
                    status="FAILED",
                    error_message=self._safe_error(Exception(message)),
                )
            except Exception:
                logger.warning(
                    "deepsec_data_entitlement_failed_state_not_recorded",
                    extra={"entitlement_id": entitlement.entitlement_id},
                    exc_info=True,
                )

    def _editable_data_entitlement_role(
        self,
        role_id: str,
        *,
        expected_version: int,
    ) -> RoleRecord:
        role = self.security.get_role(role_id)
        if role is None:
            raise SecurityApiError(404, "ロールが見つかりません。")
        if role.is_built_in:
            raise SecurityApiError(409, "組み込み SYSTEM_ADMIN ロールには設定できません。")
        if role.archived:
            raise SecurityApiError(409, "アーカイブ済みロールには設定できません。")
        if role.version != expected_version:
            raise SecurityApiError(
                409,
                "ロールが別の操作で更新されています。表示を更新して再試行してください。",
            )
        return role

    def _build_data_entitlement_sync_plan(
        self,
        role: RoleRecord,
        entitlements: list[DataEntitlementRecord],
        *,
        cursor: Any,
    ) -> DataEntitlementSyncPlan:
        records = self.security._data_entitlement_records(  # noqa: SLF001
            role.role_id,
            entitlements,
            current_entitlements=role.entitlements,
        )
        from .scope_expression import parse_expression, related_nodes

        graph: dict[str, set[str]] = {}
        for policy in records:
            if policy.scope_mode == "EXPRESSION":
                graph.setdefault(
                    _qualified(policy.target_owner, policy.target_object), set()
                ).update(
                    _qualified(node.target_owner, node.target_object)
                    for node in related_nodes(parse_expression(policy.scope_expression))
                )
        # 他ロールの草稿ではなく、DB に残る grant と今回の予定参照を検証する。
        # 除外は owner/name 単位とし、同じ対象の他ロール・外部 grant は必ず検査する。
        dependency_plan = DependencyPlan(
            replaced_grants=frozenset(
                (
                    _strict_identifier(self.settings.oracle_user),
                    _strict_identifier(item.data_grant_name or _data_grant_name(item)),
                )
                for item in [*role.entitlements, *records]
                if _is_real_data_entitlement(item)
            ),
            edges=graph,
        )
        entries: list[DataEntitlementSyncEntry] = []
        for entitlement in records:
            normalized = replace(
                entitlement,
                role_id=role.role_id,
                capability="SELECT",
                target_owner=_identifier_token(entitlement.target_owner),
                target_object=_identifier_token(entitlement.target_object),
                resource_code=_qualified(entitlement.target_owner, entitlement.target_object),
                scope_code=(
                    "*"
                    if entitlement.scope_mode == "ALL"
                    else (
                        scope_filters_scope_code(entitlement.scope_filters)
                        if entitlement.scope_mode == "FILTERS"
                        else entitlement.scope_code
                    )
                ),
                scope_filters=list(entitlement.scope_filters),
            )
            normalized = replace(
                normalized,
                data_grant_name=normalized.data_grant_name or _data_grant_name(normalized),
            )
            column_types = (
                self._validate_data_entitlement(cursor, normalized, dependency_plan=dependency_plan)
                if normalized.scope_mode == "EXPRESSION"
                else self._validate_data_entitlement(cursor, normalized)
            )
            statements = tuple(
                _preview_statement(statement)
                for statement in build_data_entitlement_statements(
                    self.settings,
                    normalized,
                    column_types=column_types,
                )
            )
            entries.append(
                DataEntitlementSyncEntry(
                    entitlement=normalized,
                    statements=statements,
                    checksum=_data_grant_checksum(statements),
                )
            )
        cleanup_statements = self._role_data_grant_cleanup_statements(
            role,
            desired_entitlements=[entry.entitlement for entry in entries],
        )
        all_statements = (
            *cleanup_statements,
            *(statement for entry in entries for statement in entry.statements),
        )
        return DataEntitlementSyncPlan(
            role=role,
            entries=tuple(entries),
            cleanup_statements=cleanup_statements,
            checksum=_data_grant_checksum(all_statements),
        )

    def _role_data_grant_cleanup_statements(
        self,
        role: RoleRecord,
        *,
        desired_entitlements: list[DataEntitlementRecord],
    ) -> tuple[str, ...]:
        desired_grant_names = {
            _strict_identifier(entitlement.data_grant_name or _data_grant_name(entitlement))
            for entitlement in desired_entitlements
            if _is_real_data_entitlement(entitlement)
        }
        desired_targets = {
            (
                _identifier_token(entitlement.target_owner),
                _identifier_token(entitlement.target_object),
            )
            for other_role in self.security.list_roles(include_archived=True)
            if other_role.role_id != role.role_id
            for entitlement in other_role.entitlements
            if _is_real_data_entitlement(entitlement)
        }
        desired_targets.update(
            (
                _identifier_token(entitlement.target_owner),
                _identifier_token(entitlement.target_object),
            )
            for entitlement in desired_entitlements
            if _is_real_data_entitlement(entitlement)
        )
        owner = _strict_identifier(self.settings.oracle_user)
        # 対象を使う最後の Data Grant を消すときは、SELECT の REVOKE → Data Grant の DROP →
        # `USE DATA GRANTS ONLY` の無効化の順にする（#1022）。以前は無効化を先に行い、
        # SELECT を外さなかったため、DATA USER が DB role の SELECT で全行を読めた（fail-open）。
        revoke_statements: list[str] = []
        drop_statements: list[str] = []
        disable_statements: list[str] = []
        disabled_targets: set[tuple[str, str]] = set()
        dropped_grants: set[str] = set()
        current_entitlements = sorted(
            (
                entitlement
                for entitlement in role.entitlements
                if _is_real_data_entitlement(entitlement)
            ),
            key=lambda item: (
                item.target_owner.upper(),
                item.target_object.upper(),
                item.data_grant_name or _data_grant_name(item),
            ),
        )
        for entitlement in current_entitlements:
            grant_name = _strict_identifier(
                entitlement.data_grant_name or _data_grant_name(entitlement)
            )
            if grant_name in desired_grant_names:
                continue
            target = (
                _identifier_token(entitlement.target_owner),
                _identifier_token(entitlement.target_object),
            )
            if target not in desired_targets and target not in disabled_targets:
                revoke_statements.append(_revoke_target_select_statement(*target))
                disable_statements.append(_disable_data_grants_only_statement(*target))
                disabled_targets.add(target)
            if grant_name not in dropped_grants:
                drop_statements.append(f"DROP DATA GRANT IF EXISTS {owner}.{grant_name}")
                dropped_grants.add(grant_name)
        return (*revoke_statements, *drop_statements, *disable_statements)

    @staticmethod
    def _data_entitlement_sync_payload(
        entry: DataEntitlementSyncEntry,
    ) -> dict[str, object]:
        entitlement = entry.entitlement
        return {
            "entitlement_id": entitlement.entitlement_id,
            "resource_code": entitlement.resource_code,
            "scope_code": entitlement.scope_code,
            "capability": entitlement.capability,
            "target_owner": entitlement.target_owner,
            "target_object": entitlement.target_object,
            "target_type": entitlement.target_type,
            "column_names": list(entitlement.column_names),
            "scope_mode": entitlement.scope_mode,
            "scope_column": entitlement.scope_column,
            "scope_filters": [scope_filter_payload(item) for item in entitlement.scope_filters],
            "scope_expression": entitlement.scope_expression,
            "data_grant_name": entitlement.data_grant_name,
            "sql_checksum": entry.checksum,
            "apply_status": entitlement.apply_status,
            "apply_error_message": entitlement.apply_error_message,
            "applied_at": entitlement.applied_at,
            "sql": list(entry.statements),
            "checksum": entry.checksum,
        }

    def _managed_oracle_data_grants(self, cursor: Any) -> list[OracleManagedDataGrant]:
        expected_grantee = _managed_data_grant_grantee(self.settings)
        legacy_direct_grantee = _legacy_direct_end_user_data_grant_grantee(self.settings)
        cursor.execute(
            """
            SELECT DISTINCT GRANT_NAME, OBJECT_OWNER, OBJECT_NAME, GRANTEE, GRANTEE_TYPE
              FROM DBA_DATA_GRANTS
             WHERE OWNER = :owner
               AND GRANT_NAME LIKE :grant_prefix
               AND GRANTEE IN (:expected_grantee, :legacy_grantee)
             ORDER BY OBJECT_OWNER, OBJECT_NAME, GRANT_NAME
            """,
            {
                "owner": _strict_identifier(self.settings.oracle_user),
                "grant_prefix": f"{MANAGED_DATA_GRANT_PREFIX}%",
                "expected_grantee": expected_grantee,
                "legacy_grantee": legacy_direct_grantee,
            },
        )
        return [
            OracleManagedDataGrant(
                grant_name=_strict_identifier(str(row[0])),
                target_owner=_catalog_identifier_token(row[1]),
                target_object=_catalog_identifier_token(row[2]),
                grantee=_strict_identifier(str(row[3])),
                grantee_type=str(row[4]).upper(),
            )
            for row in cursor.fetchall()
        ]

    def status(self) -> dict[str, object]:
        result: dict[str, object] = {
            "configured": False,
            "driver_mode": self.settings.oracle_driver_mode,
            "connection_security": self.settings.oracle_connection_security,
            "deepsec_enabled": self.settings.oracle_deepsec_enabled,
            "data_user": self.settings.oracle_deepsec_data_user,
            "has_data_user_password": bool(self.settings.oracle_deepsec_data_user_password),
            "objects": {},
            "message": "Deep Data Security は未設定です。",
        }
        if self.settings.oracle_deepsec_enabled:
            try:
                self.pools.validate_deepsec_control_configuration()
            except Exception as exc:
                result["message"] = f"DeepSec 状態を確認できませんでした: {self._safe_error(exc)}"
                return result
        if not self.settings.oracle_user or not self.settings.oracle_dsn:
            return result
        owner = _strict_identifier(self.settings.oracle_user)
        legacy_direct_grantee = _legacy_direct_end_user_data_grant_grantee(self.settings)
        try:
            with self.pools.control_connection() as conn, conn.cursor() as cursor:
                checks: dict[str, tuple[str, dict[str, object]]] = {
                    "data_user": (
                        "SELECT COUNT(*) FROM DBA_END_USERS WHERE USERNAME = :name",
                        {"name": _strict_identifier(self.settings.oracle_deepsec_data_user)},
                    ),
                    "data_role": (
                        "SELECT COUNT(*) FROM DBA_DATA_ROLES WHERE DATA_ROLE = :name",
                        {"name": DEEPSEC_DATA_ROLE},
                    ),
                    "db_role": (
                        "SELECT COUNT(*) FROM DBA_ROLES WHERE ROLE = :name",
                        {"name": DEEPSEC_DB_ROLE},
                    ),
                    "context": (
                        "SELECT COUNT(*) FROM DBA_CONTEXT "
                        "WHERE NAMESPACE = :name AND SCHEMA = :owner "
                        "AND PACKAGE = 'NL2SQL_DEEPSEC_CTX_PKG'",
                        {"name": "NL2SQL_APP_USER_CTX", "owner": owner},
                    ),
                    "context_package": (
                        "SELECT COUNT(*) FROM ALL_OBJECTS WHERE OWNER = :owner "
                        "AND OBJECT_NAME = 'NL2SQL_DEEPSEC_CTX_PKG' "
                        "AND OBJECT_TYPE = 'PACKAGE' AND STATUS = 'VALID'",
                        {"owner": owner},
                    ),
                    "managed_data_grants": (
                        "SELECT COUNT(DISTINCT GRANT_NAME) FROM DBA_DATA_GRANTS "
                        "WHERE OWNER = :owner AND GRANT_NAME LIKE 'NL2SQL_DG_%'",
                        {"owner": owner},
                    ),
                    "managed_data_grants_data_role": (
                        "SELECT COUNT(DISTINCT GRANT_NAME) FROM DBA_DATA_GRANTS "
                        "WHERE OWNER = :owner AND GRANT_NAME LIKE 'NL2SQL_DG_%' "
                        "AND GRANTEE = :grantee AND GRANTEE_TYPE = 'DATA ROLE'",
                        {"owner": owner, "grantee": DEEPSEC_DATA_ROLE},
                    ),
                    "managed_data_grants_direct_end_user": (
                        "SELECT COUNT(DISTINCT GRANT_NAME) FROM DBA_DATA_GRANTS "
                        "WHERE OWNER = :owner AND GRANT_NAME LIKE 'NL2SQL_DG_%' "
                        "AND GRANTEE = :grantee AND GRANTEE_TYPE = 'END USER'",
                        {"owner": owner, "grantee": legacy_direct_grantee},
                    ),
                }
                for key, table_name in _DEEPSEC_PREDICATE_TABLE_GRANTS:
                    checks[key] = (
                        "SELECT COUNT(*) FROM DBA_TAB_PRIVS "
                        "WHERE OWNER = :owner AND TABLE_NAME = :table_name "
                        "AND GRANTEE = :grantee AND PRIVILEGE = 'SELECT'",
                        {
                            "owner": owner,
                            "table_name": table_name,
                            "grantee": DEEPSEC_DB_ROLE,
                        },
                    )
                objects: dict[str, int] = {}
                for key, (sql, params) in checks.items():
                    cursor.execute(sql, params)
                    objects[key] = int(cursor.fetchone()[0])
                managed_target_vpd_policies = 0
                for target_owner, target_object in sorted(self._managed_target_keys()):
                    cursor.execute(
                        """
                        SELECT COUNT(*)
                          FROM DBA_POLICIES
                         WHERE OBJECT_OWNER = :target_owner
                           AND OBJECT_NAME = :target_object
                           AND ENABLE = 'YES'
                        """,
                        {
                            "target_owner": target_owner,
                            "target_object": target_object,
                        },
                    )
                    managed_target_vpd_policies += int(cursor.fetchone()[0])
                objects["managed_target_vpd_policies"] = managed_target_vpd_policies
                result["objects"] = objects
                foundation_keys = (
                    "data_user",
                    "data_role",
                    "db_role",
                    "context",
                    "context_package",
                    *[key for key, _table_name in _DEEPSEC_PREDICATE_TABLE_GRANTS],
                )
                result["configured"] = all(objects[key] > 0 for key in foundation_keys)
                result["message"] = (
                    "Deep Data Security の基盤構成は適用済みです。"
                    if result["configured"]
                    else "DeepSec V001 に未適用のオブジェクトがあります。"
                )
        except Exception as exc:
            result["message"] = f"DeepSec 状態を確認できませんでした: {self._safe_error(exc)}"
        return result

    def update_config(self, data_user_password: str) -> dict[str, object]:
        password = _validate_data_user_password(data_user_password)
        previous_enabled = self.settings.oracle_deepsec_enabled
        previous_data_user = self.settings.oracle_deepsec_data_user
        previous_password = self.settings.oracle_deepsec_data_user_password
        env_snapshot: tuple[bool, str] | None = None
        self.settings.oracle_deepsec_enabled = True
        self.settings.oracle_deepsec_data_user = DEEPSEC_DATA_USER
        self.settings.oracle_deepsec_data_user_password = password
        try:
            self.pools.validate_deepsec_configuration()
            with locked_env_file(_BACKEND_ENV_FILE) as env_path:
                env_snapshot = _deepsec_env_snapshot_locked(env_path)
                _write_deepsec_config_env_locked(self.settings, env_path)
                try:
                    self._sync_existing_data_user_password()
                except Exception:
                    _restore_deepsec_env_snapshot_locked(env_path, env_snapshot)
                    env_snapshot = None
                    raise
        except Exception as exc:
            safe_error = self._safe_error(exc)
            self.settings.oracle_deepsec_enabled = previous_enabled
            self.settings.oracle_deepsec_data_user = previous_data_user
            self.settings.oracle_deepsec_data_user_password = previous_password
            if env_snapshot is not None:
                _restore_deepsec_env_snapshot(env_snapshot)
            close_oracle_pools()
            if isinstance(exc, SecurityApiError):
                raise SecurityApiError(
                    exc.status_code,
                    safe_error,
                    code=exc.code,
                    title=exc.title,
                    retryable=exc.retryable,
                    field_errors=exc.field_errors,
                ) from exc
            raise SecurityApiError(409, safe_error) from exc
        close_oracle_pools()
        return self.status()

    def sync_saved_data_user_password(self) -> dict[str, object]:
        if not self.settings.oracle_deepsec_data_user_password:
            raise SecurityApiError(
                409,
                "保存済み DATA USER パスワードがありません。Deep Data Security 画面で "
                "DATA USER パスワードを保存してから Oracle END USER へ同期してください。",
            )
        if not self.settings.oracle_user.strip() or not self.settings.oracle_dsn.strip():
            raise SecurityApiError(
                409,
                "Oracle END USER への同期には PLATFORM_ORACLE_USER と "
                "PLATFORM_ORACLE_DSN の設定が必要です。",
            )
        try:
            synced = self._sync_existing_data_user_password()
        except SecurityApiError:
            close_oracle_pools()
            raise
        except Exception as exc:
            safe_error = self._safe_error(exc)
            close_oracle_pools()
            raise SecurityApiError(409, safe_error) from exc
        close_oracle_pools()
        if not synced:
            raise SecurityApiError(
                409,
                "Oracle END USER への同期が完了していません。DATA USER 認証の"
                "「Oracle へ同期」を再実行してください。",
            )
        return self.status()

    def _confirm_data_user_login_after_password_reuse_error(self, exc: Exception) -> None:
        try:
            self.pools.validate_data_user_login()
        except Exception as login_exc:
            login_error = self._safe_error(login_exc)
            raise SecurityApiError(
                409,
                "Oracle の password reuse policy により、保存済み DATA USER パスワードを"
                "再設定できませんでした。さらに保存済みパスワードで DATA USER ログインを"
                "確認できなかったため、同期済みとは判断できません。Deep Data Security 画面で"
                f"新しい DATA USER パスワードを保存してから同期してください: {login_error}",
            ) from exc

    def _sync_existing_data_user_password(self) -> bool:
        if not self.settings.oracle_user.strip() or not self.settings.oracle_dsn.strip():
            return False
        self.pools.validate_deepsec_control_configuration()
        data_user = _strict_identifier(self.settings.oracle_deepsec_data_user)
        try:
            with self.pools.control_connection() as conn, conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM DBA_END_USERS WHERE USERNAME = :name",
                    {"name": data_user},
                )
                row = cursor.fetchone()
                exists = bool(row and int(row[0]) > 0)
                statement = (
                    _data_user_password_sync_statement(self.settings)
                    if exists
                    else _data_user_create_statement(self.settings)
                )
                cursor.execute(self._execution_sql(statement))
        except Exception as exc:
            safe_error = self._safe_error(exc)
            if _is_oracle_password_reuse_error(exc):
                self._confirm_data_user_login_after_password_reuse_error(exc)
                return True
            raise SecurityApiError(
                409,
                "DeepSec DATA USER パスワードを Oracle END USER へ同期できませんでした。"
                "PLATFORM_ORACLE_USER に CREATE END USER / ALTER END USER 権限があるか"
                "確認してください: "
                f"{safe_error}",
            ) from exc
        return True

    def apply_step(
        self,
        step_no: int,
        checksum: str,
        confirmation: str,
        actor: Principal,
    ) -> dict[str, object]:
        if confirmation.strip() != DEEPSEC_APPLY_CONFIRMATION:
            raise SecurityApiError(
                409,
                f"DeepSec step の適用には confirmation={DEEPSEC_APPLY_CONFIRMATION} が必要です。",
            )
        if not self.settings.oracle_deepsec_enabled:
            raise SecurityApiError(409, "NL2SQL_ORACLE_DEEPSEC_ENABLED=true を設定してください。")
        if not self.settings.oracle_deepsec_data_user_password:
            # V001.1 は保存済みのパスワードで DATA USER（END USER）を作る（#1676）。未保存のまま
            # 進めると validate_deepsec_configuration の OracleAdapterError が 500 になるので、
            # 先に止める。
            raise SecurityApiError(
                409,
                "DATA USER パスワードが保存されていません。Deep Data Security 画面の"
                "「DATA USER 認証」で DATA USER パスワードを保存してから、"
                "基盤構成を適用してください。",
            )
        self.pools.validate_deepsec_configuration()
        plan = {step.step_no: step for step in build_v001_plan(self.settings)}
        step = plan.get(step_no)
        if step is None:
            raise SecurityApiError(404, "DeepSec plan step が見つかりません。")
        if not checksum or checksum != step.checksum:
            raise SecurityApiError(
                409, "SQL plan のチェックサムが一致しません。画面を再読込してください。"
            )
        states = self.security.store.get_deepsec_states()
        for previous in range(1, step_no):
            previous_step = plan[previous]
            previous_state = self._state_for_step(states, previous_step)
            if previous_state.get("status") != "APPLIED":
                raise SecurityApiError(409, "前の DeepSec step を先に適用してください。")
        self.security.store.set_deepsec_state(
            version=PLAN_VERSION,
            step_no=step.step_no,
            step_key=step.key,
            checksum=step.checksum,
            status="RUNNING",
            error_message="",
            executed_by_user_uuid=actor.user_uuid,
        )
        statements = [self._execution_sql(statement) for statement in step.statements]
        try:
            with self.pools.control_connection() as conn:
                results = oracle_statement_executor.execute(
                    conn,
                    statements,
                    atomic=False,
                    include_sql=False,
                    ignored_error_codes=step.ignored_error_codes,
                )
            errors = [item for item in results if item["status"] == "error"]
            if errors:
                # V001.1 は DATA USER のパスワードを含む文を実行するので、
                # Oracle の応答から念のため除く。
                raise SecurityApiError(
                    409,
                    self._redact_secret(
                        _statement_failure_message(
                            f"DeepSec の手順 {step.step_no}「{step.title}」の適用に失敗しました。"
                            "原因を解消してから、この手順をもう一度適用してください。",
                            errors[0],
                        )
                    ),
                )
            self.security.store.set_deepsec_state(
                version=PLAN_VERSION,
                step_no=step.step_no,
                step_key=step.key,
                checksum=step.checksum,
                status="APPLIED",
                error_message="",
                executed_by_user_uuid=actor.user_uuid,
            )
            close_oracle_pools()
            return {
                "version": PLAN_VERSION,
                "step_no": step.step_no,
                "status": "APPLIED",
                "results": results,
            }
        except Exception as exc:
            safe_error = self._safe_error(exc)
            self.security.store.set_deepsec_state(
                version=PLAN_VERSION,
                step_no=step.step_no,
                step_key=step.key,
                checksum=step.checksum,
                status="FAILED",
                error_message=safe_error,
                executed_by_user_uuid=actor.user_uuid,
            )
            if isinstance(exc, SecurityApiError):
                raise
            raise SecurityApiError(500, f"DeepSec step の実行に失敗しました: {safe_error}") from exc

    def reset(
        self,
        version: str,
        confirmation: str,
        actor: Principal,
    ) -> dict[str, object]:
        if version != PLAN_VERSION:
            raise SecurityApiError(404, "DeepSec plan version が見つかりません。")
        if confirmation.strip() != DEEPSEC_RESET_CONFIRMATION:
            raise SecurityApiError(
                409,
                f"DeepSec 構成の解除には confirmation={DEEPSEC_RESET_CONFIRMATION} が必要です。",
            )
        self.pools.validate_deepsec_control_configuration()
        self.pools.close()
        statements = [
            self._preview_sql(statement)
            for statement in build_v001_reset_statements(
                self.settings,
                self._managed_real_entitlements(),
            )
        ]
        try:
            with self.pools.control_connection() as conn:
                # 失敗した文の後ろは実行しない（#1022）。SELECT の REVOKE より後ろの
                # DATA GRANTS ONLY の無効化へ、REVOKE の失敗を越えて進ませない。
                results = oracle_statement_executor.execute(
                    conn,
                    statements,
                    atomic=False,
                    include_sql=False,
                    stop_on_error=True,
                )
            errors = [item for item in results if item["status"] == "error"]
            if errors:
                raise SecurityApiError(
                    409,
                    _statement_failure_message(
                        "DeepSec 構成の解除が途中で止まりました。"
                        "原因を解消してから、もう一度解除してください。",
                        errors[0],
                    ),
                )
            step_numbers = [1, 2, 3, 4]
            self.security.store.clear_deepsec_states(
                version=PLAN_VERSION,
                step_numbers=step_numbers,
            )
            self.security.store.clear_deepsec_entitlement_apply_states()
            close_oracle_pools()
            return {
                "version": PLAN_VERSION,
                "status": "RESET",
                "step_numbers": step_numbers,
                "executed_by_user_uuid": actor.user_uuid,
                "results": results,
            }
        except Exception as exc:
            if isinstance(exc, SecurityApiError):
                raise
            safe_error = self._safe_error(exc)
            raise SecurityApiError(
                500,
                f"DeepSec 構成の解除に失敗しました: {safe_error}",
            ) from exc

    def _role_entitlements_payload(self, role: RoleRecord) -> dict[str, object]:
        entitlements: list[dict[str, object]] = []
        for entitlement in role.entitlements:
            data_grant_name = entitlement.data_grant_name or (
                _data_grant_name(entitlement) if _is_real_data_entitlement(entitlement) else ""
            )
            normalized = replace(entitlement, data_grant_name=data_grant_name)
            try:
                statements = build_data_entitlement_preview(self.settings, normalized)
            except SecurityApiError:
                statements = ()
            checksum = _data_grant_checksum(statements) if statements else entitlement.sql_checksum
            entitlements.append(
                {
                    "entitlement_id": entitlement.entitlement_id,
                    "resource_code": entitlement.resource_code,
                    "scope_code": entitlement.scope_code,
                    "capability": entitlement.capability,
                    "target_owner": entitlement.target_owner,
                    "target_object": entitlement.target_object,
                    "target_type": entitlement.target_type,
                    "column_names": list(entitlement.column_names),
                    "scope_mode": entitlement.scope_mode,
                    "scope_column": entitlement.scope_column,
                    "scope_filters": [
                        scope_filter_payload(item) for item in entitlement.scope_filters
                    ],
                    "data_grant_name": data_grant_name,
                    "scope_expression": entitlement.scope_expression,
                    "sql_checksum": entitlement.sql_checksum,
                    "apply_status": entitlement.apply_status,
                    "apply_error_message": entitlement.apply_error_message,
                    "applied_at": entitlement.applied_at,
                    "sql": list(statements),
                    "checksum": checksum,
                }
            )
        return {
            "role_id": role.role_id,
            "role_code": role.role_code,
            "display_name": role.display_name,
            "description": role.description,
            "is_built_in": role.is_built_in,
            "archived": role.archived,
            "version": role.version,
            "data_entitlements": entitlements,
        }

    def _managed_real_entitlements(self) -> list[DataEntitlementRecord]:
        entitlements: list[DataEntitlementRecord] = []
        for role in self.security.list_roles(include_archived=True):
            entitlements.extend(
                entitlement
                for entitlement in role.entitlements
                if _is_real_data_entitlement(entitlement)
            )
        return entitlements

    def _managed_target_keys(self) -> set[tuple[str, str]]:
        """管理対象の (owner, object) をカタログ上の名前（辞書ビューの bind 用）で返す。"""
        return {
            (
                _identifier_name(entitlement.target_owner),
                _identifier_name(entitlement.target_object),
            )
            for entitlement in self._managed_real_entitlements()
            if _is_real_data_entitlement(entitlement)
        }

    def _enabled_vpd_policy_rows(
        self,
        cursor: Any,
        *,
        target_owner: str,
        target_object: str,
    ) -> list[tuple[str, str, str, str]]:
        cursor.execute(
            """
            SELECT POLICY_NAME, PF_OWNER, FUNCTION, POLICY_TYPE
              FROM DBA_POLICIES
             WHERE OBJECT_OWNER = :target_owner
               AND OBJECT_NAME = :target_object
               AND ENABLE = 'YES'
             ORDER BY POLICY_NAME
            """,
            {
                "target_owner": _identifier_name(target_owner),
                "target_object": _identifier_name(target_object),
            },
        )
        return [
            (
                str(row[0]),
                str(row[1] or ""),
                str(row[2] or ""),
                str(row[3] or ""),
            )
            for row in cursor.fetchall()
        ]

    def _validate_data_entitlement(
        self,
        cursor: Any,
        entitlement: DataEntitlementRecord,
        *,
        dependency_plan: DependencyPlan | None = None,
    ) -> dict[str, str]:
        # bind にはカタログ上の名前を使う。引用名を大文字化せず、大文字の同名表と取り違えない。
        target_owner = _identifier_name(entitlement.target_owner)
        target_object = _identifier_name(entitlement.target_object)
        target_type = entitlement.target_type.strip().upper() or "TABLE"
        if target_type not in _DEEPSEC_MANAGED_TARGET_TYPES:
            raise SecurityApiError(400, "対象は TABLE / VIEW / MATERIALIZED VIEW のみです。")
        if target_owner.upper() in _DEEPSEC_INTERNAL_OWNER_NAMES:
            raise SecurityApiError(400, "SYS/SYSTEM schema の object は管理対象にできません。")
        app_owner = _strict_identifier(self.settings.oracle_user)
        if target_owner.upper() == app_owner and target_object.upper().startswith(
            _DEEPSEC_INTERNAL_OBJECT_PREFIXES
        ):
            raise SecurityApiError(400, "NL2SQL の内部/security object は管理対象にできません。")
        # 対象の選択欄（target_objects / target_object_detail）と同じ範囲だけを受け付ける。
        # API を直接呼んでも、他製品・共通基盤の表（RAG_ / AGENT_ / PLATFORM_ / NL2SQL_）や、
        # 許可していない schema の表に Data Grant を作らない（#999）。
        if not _deepsec_target_visible(
            target_owner, target_object
        ) or not self._target_owner_allowed(target_owner):
            raise SecurityApiError(
                400, "この table/view は Data Grant の対象にできません。対象を選び直してください。"
            )
        if entitlement.capability != "SELECT":
            raise SecurityApiError(400, "V1 の Data Grant は SELECT のみ対応しています。")
        scope_mode = entitlement.scope_mode.strip().upper() or "ALL"
        if scope_mode not in _DEEPSEC_SCOPE_MODES:
            raise SecurityApiError(
                400, "scope_mode は ALL、COLUMN_EQUALS、FILTERS、EXPRESSION のいずれかです。"
            )
        if not entitlement.column_names:
            raise SecurityApiError(400, "Data Grant に含める列を選択してください。")

        cursor.execute(
            """
            SELECT OBJECT_TYPE
              FROM ALL_OBJECTS
             WHERE OWNER = :owner
               AND OBJECT_NAME = :object_name
               AND OBJECT_TYPE IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW')
               AND STATUS = 'VALID'
            """,
            {"owner": target_owner, "object_name": target_object},
        )
        object_types = {str(row[0]).upper() for row in cursor.fetchall()}
        if not object_types:
            raise SecurityApiError(400, "対象 table/view が見つかりません。")
        if target_type not in object_types:
            raise SecurityApiError(400, "対象 object type が metadata と一致しません。")

        cursor.execute(
            """
            SELECT COLUMN_NAME, DATA_TYPE
              FROM ALL_TAB_COLUMNS
             WHERE OWNER = :owner AND TABLE_NAME = :object_name
            """,
            {"owner": target_owner, "object_name": target_object},
        )
        # 列も canonical token で照合する。大文字化すると "Amount" と AMOUNT を取り違える。
        columns = {
            _catalog_identifier_token(row[0]): str(row[1]).upper() for row in cursor.fetchall()
        }
        missing_columns = [
            column
            for column in entitlement.column_names
            if _identifier_token(column) not in columns
        ]
        if missing_columns:
            raise SecurityApiError(
                400,
                "対象 object に存在しない列です: " + ", ".join(sorted(missing_columns)),
            )
        if scope_mode == "EXPRESSION":
            from .scope_expression import validate_expression_metadata

            if entitlement.scope_column or entitlement.scope_filters:
                raise SecurityApiError(400, "EXPRESSION は条件ツリーのみ指定してください。")
            validate_expression_metadata(self, cursor, entitlement, columns, dependency_plan)
            return columns
        if entitlement.scope_expression is not None:
            raise SecurityApiError(400, "条件ツリーには EXPRESSION モードを指定してください。")
        if scope_mode == "ALL":
            if entitlement.scope_code != "*":
                raise SecurityApiError(400, "ALL scope の scope_code は * にしてください。")
            if entitlement.scope_filters:
                raise SecurityApiError(400, "ALL scope では scope filters を指定できません。")
            return columns
        if scope_mode == "FILTERS":
            if entitlement.scope_column:
                raise SecurityApiError(400, "FILTERS scope では scope column を指定できません。")
            self._validate_scope_filters(columns, entitlement)
            return columns
        scope_column = _identifier_token(entitlement.scope_column)
        if scope_column not in columns:
            raise SecurityApiError(400, "scope column が対象 object に存在しません。")
        if _scope_value_type(columns[scope_column]) != "TEXT":
            raise SecurityApiError(400, "scope column は文字列型の列を指定してください。")
        if entitlement.scope_code == "*":
            raise SecurityApiError(400, "COLUMN_EQUALS scope は具体的な値を指定してください。")
        if entitlement.scope_filters:
            raise SecurityApiError(400, "COLUMN_EQUALS scope では scope filters を指定できません。")
        return columns

    def _validate_scope_filters(
        self,
        columns: dict[str, str],
        entitlement: DataEntitlementRecord,
    ) -> None:
        if not entitlement.scope_filters:
            raise SecurityApiError(400, "FILTERS scope は条件を 1 件以上指定してください。")
        if len(entitlement.scope_filters) > _DEEPSEC_MAX_SCOPE_FILTERS:
            raise SecurityApiError(
                400,
                f"scope filter は {_DEEPSEC_MAX_SCOPE_FILTERS} 件以内で指定してください。",
            )
        if entitlement.scope_code != scope_filters_scope_code(entitlement.scope_filters):
            raise SecurityApiError(
                400,
                "FILTERS scope の scope_code が条件 checksum と一致しません。",
            )
        for filter_item in entitlement.scope_filters:
            column_name = _identifier_token(filter_item.column_name)
            if column_name not in columns:
                raise SecurityApiError(400, "scope filter column が対象 object に存在しません。")
            value_type = filter_item.value_type.strip().upper()
            if value_type not in _DEEPSEC_SCOPE_VALUE_TYPES:
                raise SecurityApiError(400, "scope filter value_type が不正です。")
            actual_type = _scope_value_type(columns[column_name])
            if not actual_type:
                raise SecurityApiError(400, "scope filter column は対応型の列を指定してください。")
            if actual_type != value_type:
                raise SecurityApiError(400, "scope filter value_type が対象列の型と一致しません。")
            operator = filter_item.operator.strip().upper()
            if operator not in _DEEPSEC_SCOPE_OPERATORS_BY_TYPE[value_type]:
                raise SecurityApiError(400, "scope filter operator が列型に対応していません。")
            _scope_filter_predicate(
                _qualified(entitlement.target_owner, entitlement.target_object),
                replace(
                    filter_item,
                    column_name=column_name,
                    operator=operator,
                    value_type=value_type,
                ),
                data_type=columns[column_name],
            )

    def verify(self, actor: Principal) -> dict[str, object]:
        _ = actor
        if not self.settings.oracle_deepsec_enabled:
            raise SecurityApiError(409, "NL2SQL_ORACLE_DEEPSEC_ENABLED=true を設定してください。")
        self.pools.validate_deepsec_control_configuration()
        entitlements = [
            entitlement
            for entitlement in self._managed_real_entitlements()
            if entitlement.apply_status == "APPLIED"
        ]
        checks: list[dict[str, object]] = []
        try:
            status = self.status()
            status_objects = status.get("objects")
            status_object_counts = status_objects if isinstance(status_objects, dict) else {}
            checks.append(
                {
                    "key": "foundation",
                    "passed": bool(status.get("configured")),
                    "detail": str(status.get("message") or ""),
                }
            )
            missing_predicate_grants = [
                table_name
                for key, table_name in _DEEPSEC_PREDICATE_TABLE_GRANTS
                if int(status_object_counts.get(key, 0) or 0) <= 0
            ]
            checks.append(
                {
                    "key": "predicate_table_grants",
                    "passed": not missing_predicate_grants,
                    "detail": (
                        "Data Grant predicate 用 app table SELECT grants are applied."
                        if not missing_predicate_grants
                        else (
                            "Missing SELECT grants for Data Grant predicate tables: "
                            + ", ".join(missing_predicate_grants)
                        )
                    ),
                }
            )
            with self.pools.control_connection() as conn, conn.cursor() as cursor:
                if not entitlements:
                    checks.append(
                        {
                            "key": "managed_data_grants",
                            "passed": True,
                            "detail": "適用済みの実データ Data Grant はありません。",
                        }
                    )
                expected_grantee = _managed_data_grant_grantee(self.settings)
                legacy_direct_grantee = _legacy_direct_end_user_data_grant_grantee(self.settings)
                for entitlement in entitlements:
                    grant_name = entitlement.data_grant_name or _data_grant_name(entitlement)
                    cursor.execute(
                        """
                        SELECT GRANT_NAME, OBJECT_OWNER, OBJECT_NAME, GRANTEE,
                               GRANTEE_TYPE, USE_DATA_GRANTS_ONLY, PREDICATE
                          FROM DBA_DATA_GRANTS
                         WHERE OWNER = :owner
                           AND GRANT_NAME = :grant_name
                           AND OBJECT_OWNER = :target_owner
                           AND OBJECT_NAME = :target_object
                           AND GRANTEE IN (:expected_grantee, :legacy_grantee)
                        """,
                        {
                            "owner": _strict_identifier(self.settings.oracle_user),
                            "grant_name": _strict_identifier(grant_name),
                            "target_owner": _identifier_name(entitlement.target_owner),
                            "target_object": _identifier_name(entitlement.target_object),
                            "expected_grantee": expected_grantee,
                            "legacy_grantee": legacy_direct_grantee,
                        },
                    )
                    rows = cursor.fetchall()
                    expected_rows = [
                        row
                        for row in rows
                        if str(row[3]).upper() == expected_grantee
                        and str(row[4]).upper() == "DATA ROLE"
                    ]
                    legacy_direct_rows = [
                        row
                        for row in rows
                        if str(row[3]).upper() == legacy_direct_grantee
                        and str(row[4]).upper() == "END USER"
                    ]
                    use_only = any(bool(row[5]) for row in expected_rows)
                    predicates = [str(row[6] or "") for row in expected_rows]
                    uses_runtime_context = any(
                        _DEEPSEC_APP_USER_CONTEXT_EXPR in predicate for predicate in predicates
                    )
                    needs_login_user_id_context = _uses_login_user_id_scope(entitlement)
                    uses_login_user_id_context = any(
                        _DEEPSEC_LOGIN_USER_ID_CONTEXT_EXPR in predicate for predicate in predicates
                    )
                    uses_legacy_context = any(
                        _DEEPSEC_LEGACY_APP_USER_CONTEXT_EXPR in predicate
                        for predicate in predicates
                    )
                    target = _qualified(entitlement.target_owner, entitlement.target_object)
                    vpd_policy_rows = self._enabled_vpd_policy_rows(
                        cursor,
                        target_owner=entitlement.target_owner,
                        target_object=entitlement.target_object,
                    )
                    vpd_policy_details = [
                        f"{name}({owner}.{function}, {policy_type})"
                        for name, owner, function, policy_type in vpd_policy_rows[
                            :_DEEPSEC_CONFLICTING_POLICY_DETAIL_LIMIT
                        ]
                    ]
                    if len(vpd_policy_rows) > _DEEPSEC_CONFLICTING_POLICY_DETAIL_LIMIT:
                        vpd_policy_details.append(
                            f"...+{len(vpd_policy_rows) - _DEEPSEC_CONFLICTING_POLICY_DETAIL_LIMIT}"
                        )
                    checks.append(
                        {
                            "key": f"vpd_policy:{entitlement.entitlement_id}",
                            "passed": not vpd_policy_rows,
                            "detail": (
                                f"{target}: enabled VPD/RLS policies are not present."
                                if not vpd_policy_rows
                                else (
                                    f"{target}: enabled VPD/RLS policies can further filter "
                                    "DeepSec rows: " + ", ".join(vpd_policy_details)
                                )
                            ),
                        }
                    )
                    checks.append(
                        {
                            "key": f"data_grant:{entitlement.entitlement_id}",
                            "passed": (
                                bool(expected_rows)
                                and use_only
                                and not legacy_direct_rows
                                and uses_runtime_context
                                and (not needs_login_user_id_context or uses_login_user_id_context)
                                and not uses_legacy_context
                            ),
                            "detail": (
                                f"{grant_name} -> {target}: rows={len(rows)}, "
                                f"data_role_rows={len(expected_rows)}, "
                                f"direct_end_user_rows={len(legacy_direct_rows)}, "
                                f"use_data_grants_only={use_only}, "
                                f"uses_runtime_context={uses_runtime_context}, "
                                f"needs_login_user_id_context={needs_login_user_id_context}, "
                                f"uses_login_user_id_context={uses_login_user_id_context}, "
                                f"uses_legacy_context={uses_legacy_context}"
                            ),
                        }
                    )
        except Exception as exc:
            safe_error = self._safe_error(exc)
            raise SecurityApiError(500, f"DeepSec 検証に失敗しました: {safe_error}") from exc
        passed = all(bool(item["passed"]) for item in checks)
        return {
            "version": PLAN_VERSION,
            "passed": passed,
            "checked_at": datetime.now(UTC).isoformat(),
            "checks": checks,
        }

    @staticmethod
    def _state_for_step(
        states: dict[tuple[str, int], dict[str, object]],
        step: DeepSecStep,
    ) -> dict[str, object]:
        state = states.get((PLAN_VERSION, step.step_no), {})
        if state.get("checksum") != step.checksum:
            return {}
        return state

    def _execution_sql(self, statement: str) -> str:
        if PASSWORD_PLACEHOLDER not in statement:
            return statement.strip()
        return statement.replace(
            PASSWORD_PLACEHOLDER,
            _quoted_password(self.settings.oracle_deepsec_data_user_password),
        ).strip()

    @staticmethod
    def _preview_sql(statement: str) -> str:
        return re.sub(r"\s+$", "", statement.strip())

    def _redact_secret(self, text: str) -> str:
        secret = self.settings.oracle_deepsec_data_user_password
        if secret:
            text = text.replace(secret, "[REDACTED]")
        return text

    def _safe_error(self, exc: Exception) -> str:
        text = self._redact_secret(str(exc).replace("\n", " "))
        # DATA USER password は exception に出ない前提だが、長い driver detail は切り捨てる。
        return text[:1000]


def get_deepsec_service() -> DeepSecService:
    return DeepSecService(get_settings(), get_security_service(), get_oracle_pool_manager())
