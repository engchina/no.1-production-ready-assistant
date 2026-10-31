"""Optional Oracle runtime adapter for NL2SQL.

この module は `oracledb` を import-time dependency にしない。local / CI は deterministic
adapter のまま動き、`NL2SQL_RUNTIME_MODE=oracle` のときだけ runtime import する。
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from pr_backend_core.api import offset_fetch_binds, offset_fetch_clause
from pr_backend_core.observability.model_purpose import model_purpose_attributes
from pr_backend_core.observability.model_usage import UNMEASURED_SOURCE_ATTRIBUTE
from pr_backend_core.observability.tracing import observed_span
from pr_backend_core.oracle_dsn import dsn_without_tns_retry
from pr_backend_core.oracle_pool import OraclePoolSize, SharedOraclePool
from pr_backend_core.oracle_session import init_oracle_session, local_session_time_zone
from pr_backend_core.timezones import session_time_zone_offset
from pr_system_settings.embedding import EMBEDDING_VECTOR_COLUMN_TYPE

from app.clients.oracle_diagnostics import oracle_connection_diagnostics
from app.settings import Settings

from .models import (
    CsvImportColumn,
    ExplainPlanData,
    ExplainPlanOperation,
    QueryResults,
    SchemaCatalog,
    SchemaColumn,
    SchemaConstraintDetail,
    SchemaOwnersData,
    SchemaOwnerSummary,
    SchemaTable,
    SchemaViewDependency,
)
from .object_identity import (
    OracleObjectIdentity,
    canonical_object_part,
    format_object_part,
    normalize_object_part,
    object_match_key,
    parse_object_identity,
    qualified_object_name,
)
from .object_visibility import (
    SYSTEM_OBJECT_BLOCKED_MESSAGE,
    filter_user_visible_catalog,
    is_user_visible_object_name,
    is_user_visible_owner_name,
    is_user_visible_schema_object,
    user_visible_object_sql_predicate,
)
from .oracle_lob import configure_clob_fetch_as_text, configure_connection_clob_fetch_as_text
from .sql_lexing import _tokens as sql_tokens

logger = logging.getLogger(__name__)


class OracleAdapterError(RuntimeError):
    """Oracle adapter の実行時エラー。"""


class SelectAiFeedbackNotFoundError(OracleAdapterError):
    """削除する DBMS_CLOUD_AI の feedback が無い（ORA-20000: No matching feedback record）。"""


def _looks_like_select_ai_feedback_not_found(message: str) -> bool:
    # DBMS_CLOUD_AI.FEEDBACK(operation => 'DELETE') の、対象が無いときの ORA-20000（#1681）。
    return "NO MATCHING FEEDBACK RECORD" in message.upper()


def _looks_like_select_ai_feedback_sql_unmatched(message: str) -> bool:
    # positive・response の無い ADD で、select ai の実行の記録（V$MAPPED_SQL）が無いとき。
    return "NO MATCHING SQL STATEMENT" in message.upper()


class SelectAiCredentialMissingError(OracleAdapterError):
    """DBMS_CLOUD_AI Profile が参照する Credential が現 schema に存在しない。"""

    code = "SELECT_AI_CREDENTIAL_MISSING"

    def __init__(self, credential_name: str, schema_name: str) -> None:
        self.credential_name = credential_name
        self.schema_name = schema_name
        qualified = (
            f'"{schema_name}"."{credential_name}"' if schema_name else f'"{credential_name}"'
        )
        super().__init__(f"Select AI Credential {qualified} が存在しません。")


class SelectAiCredentialExistsError(OracleAdapterError):
    """明示的な再作成指定なしに既存 Credential を上書きしようとした。"""

    code = "SELECT_AI_CREDENTIAL_EXISTS"


class TabularImportValidationError(OracleAdapterError):
    """Excel/CSV 取込を DB mutation 前に拒否する利用者修正可能なエラー。"""


def select_ai_object_list_entry(identity: OracleObjectIdentity) -> dict[str, str]:
    """Owner 付き object を Select AI の `object_list` 要素にする。

    `DBMS_CLOUD_AI` は `object_list` の owner / name を SQL 識別子として解釈する（#564 で実 Oracle
    23.26.3.3.0 を確認）。引用なしは大文字化され（`Mixed_Case` / `mixed_case` → `MIXED_CASE`）、
    `"Mixed_Case"` は大文字小文字を保持する。カタログ上の名前は引用が必要な部分だけ `"..."` で囲んだ
    token で渡す。引用が不要な名前は従来と同じ値（`{"owner": "SH", "name": "ORDERS"}`）になる。
    """

    return {
        "owner": format_object_part(identity.owner),
        "name": format_object_part(identity.object_name),
    }


# 辞書ビューから業務の object を取る SQL の条件。システムテーブル（NL2SQL_ / PLATFORM_ / RAG_ /
# AGENT_）と Oracle の内部 object（名前に $ / #）を最初から除き、取得量・件数に入れない（#1710）。
_CATALOG_VISIBLE_SQL = user_visible_object_sql_predicate(
    object_column="c.table_name", owner_column="c.owner"
)
_OBJECT_VISIBLE_SQL = user_visible_object_sql_predicate(
    object_column="o.object_name", owner_column="o.owner"
)
_DEPENDENCY_VISIBLE_SQL = user_visible_object_sql_predicate(
    object_column="d.name", owner_column="d.owner"
)
_DEPENDENCY_REFERENCED_VISIBLE_SQL = user_visible_object_sql_predicate(
    object_column="d.referenced_name", owner_column="d.referenced_owner"
)
_TABLE_VISIBLE_SQL = user_visible_object_sql_predicate(
    object_column="t.table_name", owner_column="t.owner"
)
_VIEW_VISIBLE_SQL = user_visible_object_sql_predicate(
    object_column="v.view_name", owner_column="v.owner"
)

WALLET_PASSWORD_REQUIRED_ERROR = (
    "Oracle Wallet がパスワードを必要としています。"  # nosec B105
    "PLATFORM_ORACLE_WALLET_PASSWORD または PLATFORM_ORACLE_PASSWORD を設定してください。"
)
DEEPSEC_THIN_ONLY_ERROR = (
    "Oracle Deep Data Security は python-oracledb Thin mode のみ対応です。"
    "NL2SQL_ORACLE_DEEPSEC_ENABLED=true の場合は PLATFORM_ORACLE_DRIVER_MODE=thin にしてください。"
)
THIN_WALLET_MTLS_REQUIRED_FILES = frozenset({"tnsnames.ora", "ewallet.pem"})
THICK_WALLET_MTLS_REQUIRED_FILES = frozenset({"tnsnames.ora", "sqlnet.ora", "cwallet.sso"})
WALLET_MTLS_REQUIRED_FILES = THIN_WALLET_MTLS_REQUIRED_FILES
FEEDBACK_VECTOR_DB_LOCK_ID = (
    int(hashlib.sha256(b"nl2sql_feedback_vector_index").hexdigest()[:12], 16) % 1_073_741_823
) + 1
FEEDBACK_VECTOR_DB_LOCK_TIMEOUT_SECONDS = 60
SELECT_FETCH_BATCH_SIZE = 1000


def ensure_deepsec_thin_mode(settings: Settings) -> None:
    """DeepSec 利用時は python-oracledb Thin mode だけを許可する。"""
    if (
        getattr(settings, "oracle_deepsec_enabled", False)
        and getattr(settings, "oracle_driver_mode", "thin").strip().lower() != "thin"
    ):
        raise OracleAdapterError(DEEPSEC_THIN_ONLY_ERROR)


def wallet_mtls_required_files(settings: Settings) -> frozenset[str]:
    """driver mode に応じた mTLS Wallet の必須ファイルを返す。"""
    if getattr(settings, "oracle_driver_mode", "thin").strip().lower() == "thin":
        return THIN_WALLET_MTLS_REQUIRED_FILES
    return THICK_WALLET_MTLS_REQUIRED_FILES


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    read = getattr(value, "read", None)
    if callable(read):
        return str(read())
    return str(value)


# ドメインの NOT NULL は ALL_DOMAIN_COLS.NULLABLE で復元するため、同義の CHECK は除外する。
_DOMAIN_NOT_NULL_CONDITION = re.compile(r'^"?[\w$#]+"?\s+IS\s+NOT\s+NULL$', re.IGNORECASE)


def _domain_display_name(owner: str, name: str) -> str:
    try:
        return qualified_object_name(owner, name)
    except ValueError:
        return f"{owner}.{name}" if owner else name


def _domain_column_type(row: Sequence[Any]) -> str:
    """ALL_DOMAIN_COLS の 1 行を型文字列にする。

    row = (data_type, data_length, char_length, data_precision, data_scale, ...)。
    """
    data_type = str(row[0] or "").upper()
    data_length, char_length, precision, scale = row[1], row[2], row[3], row[4]
    if data_type in {"VARCHAR2", "VARCHAR", "CHAR"} and data_length:
        # ALL_DOMAIN_COLS に CHAR_USED が無いため、文字長とバイト長の違いで CHAR 長の宣言を見分ける
        # (VARCHAR2(20 CHAR) は AL32UTF8 で DATA_LENGTH=80 / CHAR_LENGTH=20)。
        if char_length and int(char_length) != int(data_length):
            return f"{data_type}({int(char_length)} CHAR)"
        return f"{data_type}({int(data_length)})"
    if data_type in {"NVARCHAR2", "NCHAR"} and (char_length or data_length):
        return f"{data_type}({int(char_length or data_length)})"
    if data_type == "NUMBER" and precision is not None:
        suffix = f",{int(scale)}" if scale else ""
        return f"NUMBER({int(precision)}{suffix})"
    if data_type == "FLOAT" and precision is not None:
        return f"FLOAT({int(precision)})"
    if data_type == "RAW" and data_length:
        return f"RAW({int(data_length)})"
    return data_type


def _catalog_column_type(
    data_type: Any,
    data_length: Any,
    char_length: Any,
    precision: Any,
    scale: Any,
    char_used: Any,
) -> str:
    """ALL_TAB_COLUMNS の 1 列を、精度・桁・長さを含む型の文字列にする（DDL の書き方）。"""

    name = str(data_type or "").upper()
    if name in {"VARCHAR2", "CHAR"} and (char_length or data_length):
        if str(char_used or "").upper() == "C":
            return f"{name}({int(char_length or data_length)} CHAR)"
        return f"{name}({int(data_length or char_length)})"
    if name in {"NVARCHAR2", "NCHAR"} and (char_length or data_length):
        return f"{name}({int(char_length or data_length)})"
    if name == "NUMBER" and precision is not None:
        suffix = f",{int(scale)}" if scale else ""
        return f"NUMBER({int(precision)}{suffix})"
    if name == "FLOAT" and precision is not None:
        return f"FLOAT({int(precision)})"
    if name == "RAW" and data_length:
        return f"RAW({int(data_length)})"
    return name


def _coerce_result_value(value: Any) -> Any:
    read = getattr(value, "read", None)
    if callable(read):
        return _coerce_text(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex().upper()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict | list | tuple):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def _select_ai_object_name(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    for key in (
        "name",
        "NAME",
        "object_name",
        "OBJECT_NAME",
        "table_name",
        "TABLE_NAME",
        "objectName",
        "tableName",
    ):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _select_ai_object_list_items(
    value: Any, *, candidate_scope: bool = False, depth: int = 0
) -> list[Any]:
    if value is None or depth > 6:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        with suppress(json.JSONDecodeError):
            return _select_ai_object_list_items(
                json.loads(text), candidate_scope=candidate_scope, depth=depth + 1
            )
        return [text] if candidate_scope else []
    if isinstance(value, list):
        items: list[Any] = []
        for item in value:
            items.extend(_select_ai_object_list_items(item, candidate_scope=True, depth=depth + 1))
        return items
    if not isinstance(value, dict):
        return []
    if candidate_scope and _select_ai_object_name(value):
        return [value]

    collected: list[Any] = []
    for key in (
        "object_list",
        "OBJECT_LIST",
        "objectList",
        "objects",
        "OBJECTS",
        "tables",
        "TABLES",
    ):
        if key in value:
            collected.extend(
                _select_ai_object_list_items(value[key], candidate_scope=True, depth=depth + 1)
            )
    for key in (
        "attributes",
        "ATTRIBUTES",
        "profile_attributes",
        "PROFILE_ATTRIBUTES",
        "profileAttributes",
        "params",
        "PARAMS",
    ):
        if key in value:
            collected.extend(
                _select_ai_object_list_items(value[key], candidate_scope=False, depth=depth + 1)
            )
    return collected


def _normalize_select_ai_object_list(value: Any) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in _select_ai_object_list_items(value, candidate_scope=False):
        name = _select_ai_object_name(item)
        if not is_user_visible_object_name(name):
            continue
        record = dict(item) if isinstance(item, dict) else {"name": name}
        record.setdefault("name", name)
        for owner_key in ("owner", "OWNER", "schema", "SCHEMA"):
            owner = record.get(owner_key)
            if isinstance(owner, str) and owner.strip():
                record.setdefault("owner", owner.strip())
                break
        owner = str(record.get("owner") or "").strip()
        if not is_user_visible_schema_object(owner, name):
            continue
        # owner / name は SQL 識別子（引用なしは大文字、`"Mixed_Case"` は保持）。単純に
        # 大文字化すると `"Mixed_Case"` と `MIXED_CASE` を同じ key にして片方を落とす（#564）。
        key = f"{object_match_key(owner)}.{object_match_key(name)}"
        if key in seen:
            continue
        seen.add(key)
        normalized.append(record)
    return normalized


_FLEXIBLE_DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y%m%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%Y年%m月%d日",
)


def _flexible_date_value(value: str) -> datetime | None:
    """CSV セル値を柔軟に datetime へ変換する(SQL Assist の _convert_to_date 再マップ)。"""
    text = str(value or "").strip()
    if not text:
        return None
    # 'YYYY-MM-DD HH:MM:SS.ffffff' のマイクロ秒は落として解釈する
    text = re.sub(r"\.\d+$", "", text)
    for fmt in _FLEXIBLE_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)  # noqa: DTZ007
        except ValueError:
            continue
    # Excel シリアル日付(1899-12-30 起点、9999-12-31 まで)
    try:
        serial = float(text)
    except ValueError:
        return None
    if 1 <= serial <= 2958465:
        return datetime(1899, 12, 30) + timedelta(days=serial)  # noqa: DTZ001
    return None


def _normalize_statement_candidate(candidate: str) -> str:
    """候補末尾の runtime error 文言と 2 文目以降を落とす。"""
    error_match = re.search(r"\bException encountered\s*:", candidate, flags=re.IGNORECASE)
    if error_match:
        candidate = candidate[: error_match.start()]
    # 文字列リテラル・引用識別子・コメント内の `;` では切らない（`WHERE NAME = 'a;b'` 等）。
    offset = 0
    for kind, text in sql_tokens(candidate):
        if kind == "code" and text == ";":
            return candidate[:offset].strip()
        offset += len(text)
    return candidate.strip()


def _mask_sql_quoted_regions(statement: str) -> str:
    masked = re.sub(r"'[^']*'", "LIT", statement)
    return re.sub(r'"[^"]*"', "QID", masked)


def _plausible_select_statement(statement: str) -> bool:
    """候補が SQL 本体か(前置き prose や途中からの suffix でないか)を判定する。"""
    masked = _mask_sql_quoted_regions(statement)
    depth = 0
    for char in masked:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            # 閉じ括弧過多 = サブクエリ途中から始まる suffix なので除外する。
            if depth < 0:
                return False
    if re.match(r"^\s*with\s+\S+\s+as\s*\(", masked, flags=re.IGNORECASE):
        return True
    depth = 0
    from_index: int | None = None
    for match in re.finditer(r"\(|\)|\bfrom\b", masked, flags=re.IGNORECASE):
        token = match.group(0)
        if token == "(":  # nosec B105
            depth += 1
        elif token == ")":  # nosec B105
            depth -= 1
        elif depth == 0:
            from_index = match.start()
            break
    if from_index is None:
        return False
    # select list に文末句読点が現れる候補は「... generated. SELECT ...」型の前置き prose。
    select_list = masked[:from_index]
    return re.search(r"[.。!?！?](\s|$)", select_list) is None


def _extract_select_statement(text: str) -> str:
    """LLM/Select AI response から最初の妥当な SELECT/WITH statement を抽出する。

    前置き prose(「Sorry, ... SELECT statement could not be generated. SELECT ...」等)を
    飛ばしつつ、サブクエリ/CTE を含む statement を先頭から丸ごと保全する。
    service.py の `_extract_select_from_text`(Enterprise AI Direct 用)と対の関係。
    """
    cleaned = text.strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        for key in ("sql", "generated_sql", "query", "result"):
            candidate = str(payload.get(key) or "").strip()
            if candidate:
                extracted = _extract_select_statement(candidate)
                if extracted:
                    return extracted
    candidates: list[str] = []
    for match in re.finditer(r"\b(with|select)\b", cleaned, flags=re.IGNORECASE):
        candidate = cleaned[match.start() :].strip()
        if match.group(1).lower() == "with" or re.search(
            r"\bfrom\b", candidate, flags=re.IGNORECASE
        ):
            candidates.append(candidate)
    if not candidates:
        return ""
    for candidate in candidates:
        normalized = _normalize_statement_candidate(candidate)
        if normalized and _plausible_select_statement(normalized):
            return normalized
    # 全候補が妥当性検査に落ちた場合は従来挙動(最後の候補)へフォールバックする。
    return _normalize_statement_candidate(candidates[-1])


def _quote_identifier(identifier: str) -> str:
    escaped = identifier.replace('"', '""')
    return f'"{escaped}"'


def _quote_object_identity(identity: OracleObjectIdentity) -> str:
    return f"{_quote_identifier(identity.owner)}.{_quote_identifier(identity.object_name)}"


def _sample_catalog_name(value: str) -> str:
    """代表値取得の辞書ビュー照合と `"..."` 引用に使うカタログ上の名前を検証する。

    `canonical_object_part` と同じく `"`・NUL・制御文字を含む名前を拒否する。名前は
    `_quote_identifier` で二重引用符に囲んで SQL に埋め込む。
    """

    try:
        canonical_object_part(format_object_part(value))
    except ValueError as exc:
        raise OracleAdapterError(f"安全でない Oracle object name です: {value}") from exc
    return value


def _strict_sql_name(value: str) -> str:
    normalized = value.strip().strip('"').upper()
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", normalized):
        raise OracleAdapterError(f"安全でない Oracle object name です: {value}")
    return normalized


def _staging_sql_name(base_name: str) -> str:
    suffix = f"_STG_{uuid4().hex[:16].upper()}"
    return _strict_sql_name(f"{base_name[: 128 - len(suffix)]}{suffix}")


def _select_ai_feedback_index_names(profile_name: str) -> tuple[str, str, str]:
    safe_profile = _strict_sql_name(profile_name)
    index_name = f"{safe_profile}_FEEDBACK_VECINDEX"
    table_name = f"{index_name}$VECTAB"
    if len(index_name) > 128 or len(table_name) > 128:
        raise OracleAdapterError(f"feedback index 名が長すぎます: {profile_name}")
    return safe_profile, index_name, table_name


def oracle_connect_kwargs(
    settings: Settings,
    *,
    user: str | None = None,
    password: str | None = None,
) -> dict[str, object]:
    """python-oracledb connect に渡す共通 kwargs を作る。"""
    ensure_deepsec_thin_mode(settings)
    kwargs: dict[str, object] = {
        "user": user if user is not None else settings.oracle_user,
        "dsn": _oracle_connection_test_dsn(settings),
        "tcp_connect_timeout": settings.nl2sql_oracle_connect_timeout_seconds,
    }
    resolved_password = password if password is not None else settings.oracle_password
    if resolved_password.strip():
        kwargs["password"] = resolved_password
    _add_wallet_kwargs(settings, kwargs)
    return kwargs


def _oracle_connect_kwargs(settings: Settings) -> dict[str, object]:
    """後方互換 wrapper。"""
    return oracle_connect_kwargs(settings)


def _oracle_connection_test_dsn(settings: Settings) -> str:
    """Wallet alias の descriptor が取れれば、長い retry 設定を外して接続テストする（#1214）。"""
    if _oracle_connection_security(settings) == "walletless_tls":
        return settings.oracle_dsn
    return dsn_without_tns_retry(
        settings.oracle_dsn, settings.resolved_oracle_wallet_dir.strip() or None
    )


def _add_wallet_kwargs(settings: Settings, kwargs: dict[str, object]) -> None:
    """Wallet 設定を kwargs に追加する。"""
    if _oracle_connection_security(settings) == "walletless_tls":
        return

    wallet_dir = settings.resolved_oracle_wallet_dir.strip()
    if not wallet_dir:
        raise OracleAdapterError(
            "PLATFORM_ORACLE_CONNECTION_SECURITY=wallet_mtls では "
            "PLATFORM_ORACLE_WALLET_DIR が必要です。"
        )

    wallet_path = Path(wallet_dir).expanduser()
    if not wallet_path.is_dir():
        raise OracleAdapterError(f"Oracle Wallet ディレクトリが見つかりません: {wallet_dir}")
    required_files = wallet_mtls_required_files(settings)
    if not _wallet_mtls_files_exist(wallet_path, required_files=required_files):
        required = ", ".join(sorted(required_files))
        raise OracleAdapterError(
            f"Oracle Wallet に mTLS 接続ファイルが揃っていません。必要ファイル: {required}"
        )

    wallet_password = settings.oracle_wallet_password.strip() or settings.oracle_password.strip()
    if not wallet_password and _wallet_requires_password(wallet_path):
        raise OracleAdapterError(WALLET_PASSWORD_REQUIRED_ERROR)

    resolved_wallet_path = str(wallet_path)
    kwargs["config_dir"] = resolved_wallet_path
    kwargs["wallet_location"] = resolved_wallet_path
    if wallet_password:
        kwargs["wallet_password"] = wallet_password


def _oracle_connection_security(settings: Settings) -> str:
    mode = getattr(settings, "oracle_connection_security", "wallet_mtls").strip().lower()
    return mode if mode in {"wallet_mtls", "walletless_tls"} else "wallet_mtls"


def _wallet_dir_exists(settings: Settings) -> bool:
    wallet_dir = settings.resolved_oracle_wallet_dir.strip()
    return bool(wallet_dir and Path(wallet_dir).expanduser().is_dir())


def _wallet_mtls_files_exist(
    wallet_path: Path,
    *,
    required_files: frozenset[str] = WALLET_MTLS_REQUIRED_FILES,
) -> bool:
    return wallet_path.is_dir() and all(
        (wallet_path / file_name).is_file() for file_name in required_files
    )


def _wallet_requires_password(wallet_path: Path) -> bool:
    """自動ログイン Wallet がなく、秘密鍵が暗号化されていればパスワード必須。"""
    try:
        files = [path for path in wallet_path.iterdir() if path.is_file()]
    except OSError:
        return False
    names = {path.name.lower() for path in files}
    if "ewallet.p12" in names:
        return True
    encrypted_pem_exists = any(
        path.suffix.lower() == ".pem" and _pem_file_is_encrypted(path) for path in files
    )
    return bool(encrypted_pem_exists)


def _pem_file_is_encrypted(path: Path) -> bool:
    """暗号化 PEM の代表的な marker だけを少量読み取って判定する。"""
    try:
        head = path.read_bytes()[:4096]
    except OSError:
        return False
    text = head.decode("utf-8", errors="ignore").upper()
    return "BEGIN ENCRYPTED PRIVATE KEY" in text or "PROC-TYPE: 4,ENCRYPTED" in text


# 共通認証（PLATFORM_*）の store が使う接続 pool（#793）。認証は要求のたびに通るため、毎回
# Wallet / mTLS の handshake と認証をやり直さない。接続引数が変わると作り直し、DB 設定の保存時・
# 終了時に `close_auth_connection_pool`（`close_oracle_pools` から）で閉じる。
_AUTH_CONNECTION_POOL = SharedOraclePool(name="nl2sql-auth")


def close_auth_connection_pool() -> None:
    """共通認証の接続 pool を閉じる（次に借りるときに作り直す）。"""
    _AUTH_CONNECTION_POOL.close()


# NL2SQL の状態の保存先（業務プロファイル・ジョブ・履歴・オントロジー・評価。`NL2SQL_*` の表）が使う
# 接続 pool（#830）。1 回の問い合わせのジョブは状態を数十回読み書きするため、そのたびに
# Wallet / mTLS の handshake と認証をやり直さない。業務データの SQL・Select AI（`DBMS_CLOUD_AI`）・
# DeepSec の接続はこの pool に混ぜない（業務データの読み取りと Select AI の生成は下の
# `nl2sql-runtime-admin` / `nl2sql-runtime-user` の pool、それ以外は単発の接続 `connection()`）。
_STATE_CONNECTION_POOL = SharedOraclePool(name="nl2sql-state")
# アプリの接続（DB の利用者は `PLATFORM_ORACLE_USER`）で業務データを読む（SELECT の実行）・
# Select AI で生成する（`DBMS_CLOUD_AI.GENERATE`）ときに借りる接続 pool（#904）。1 回のジョブで
# 1〜2 回使い、そのたびに Wallet / mTLS の handshake と認証（遅延の大きいネットワークで約 3 秒）を
# やり直さない。
# 使うのは session の状態を変えない処理だけ（`OracleNl2SqlAdapter.runtime_connection`）。
# - system_admin 用と非 system_admin 用で pool を分け、同じ pool・同じ接続を共有しない
#   （資格情報は同じアプリの利用者でも、actor の種類の境界を接続で保つ。利用者の要件）
# - DeepSec の DATA USER の接続（利用者の context を設定する。`OraclePoolManager`）とは別の pool
#   （DeepSec 有効の非 system_admin はそちらを使い、ここの user 用の pool は使わない）
# - 状態の保存先の pool（`nl2sql-state`）とも分け、業務データの SQL と状態の読み書きを混ぜない
# - ALTER SESSION・`DBMS_CLOUD_AI.SET_PROFILE`・DDL / DML・Select AI Agent・オントロジーの操作の
#   トランザクションは今までどおり単発の接続（`connection()`）
_RUNTIME_ADMIN_CONNECTION_POOL = SharedOraclePool(name="nl2sql-runtime-admin")
_RUNTIME_USER_CONNECTION_POOL = SharedOraclePool(name="nl2sql-runtime-user")
# 同じスレッドが状態の接続を借りたまま、もう 1 本借りようとしたか
# （pool の上限で自分を待たないため）。
_STATE_CONNECTION_DEPTH = threading.local()


def close_state_connection_pool() -> None:
    """状態の保存先の接続 pool を閉じる（次に借りるときに作り直す）。"""
    _STATE_CONNECTION_POOL.close()


def close_runtime_connection_pool() -> None:
    """業務データの読み取り・Select AI の生成の接続 pool（admin 用・user 用）を閉じる。

    次に借りるときに作り直す。
    """
    _RUNTIME_ADMIN_CONNECTION_POOL.close()
    _RUNTIME_USER_CONNECTION_POOL.close()


class OracleAdminSqlDescriber:
    """DB 管理チャットの列の検査（#2352）。管理の接続で、実行しない解析（`cursor.parse`）だけ。

    object の列は `SELECT * FROM <object> WHERE 1 = 0` を解析した `cursor.description` から読む
    （データは読まない）。文の検査も解析だけで、実行しない。接続は最初に要るときに開き、
    つなげなければ `available` を False にして「分からない」を返す（生成は止めない）。
    """

    def __init__(self, adapter: OracleNl2SqlAdapter) -> None:
        self._adapter = adapter
        self._stack: ExitStack | None = None
        self._cursor: Any = None
        self._available = True

    @property
    def available(self) -> bool:
        return self._available

    def _open(self) -> Any:
        if not self._available:
            return None
        if self._cursor is None:
            stack = ExitStack()
            try:
                conn = stack.enter_context(self._adapter.connection())
                self._cursor = stack.enter_context(conn.cursor())
            except Exception as exc:
                stack.close()
                self._available = False
                logger.warning(
                    "nl2sql_db_admin_column_check_unavailable",
                    extra={"error": str(exc)[:300]},
                )
                return None
            self._stack = stack
        return self._cursor

    def describe(self, reference_sql: str) -> tuple[str, ...] | None:
        cursor = self._open()
        if cursor is None:
            return None
        try:
            # reference_sql は検査済みの識別子だけ（ObjectRef.reference_sql）。
            cursor.parse(f"SELECT * FROM {reference_sql} WHERE 1 = 0")  # nosec B608
        except Exception:
            return None
        return tuple(str(item[0]) for item in cursor.description or ())

    def parse_error(self, sql: str) -> str | None:
        cursor = self._open()
        if cursor is None:
            return None
        try:
            cursor.parse(sql)
        except Exception as exc:
            message = str(exc).strip()
            if "ORA-" not in message:
                # 接続の切断など、文の誤りではないもの。
                return None
            return message.splitlines()[0]
        return ""

    def close(self) -> None:
        if self._stack is not None:
            self._stack.close()
            self._stack = None
            self._cursor = None


class OracleNl2SqlAdapter:
    """Thin python-oracledb wrapper.

    実 SQL 生成・実行はここへ閉じ込める。呼び出し側 service は同じ API shape のまま
    deterministic / oracle runtime を切り替える。
    """

    def __init__(self, settings: Settings, *, connect_attempts: int = 1) -> None:
        self.settings = settings
        self.connect_attempts = max(1, min(connect_attempts, 3))
        self._oracledb: Any | None = None
        self._client_initialized = False

    def is_configured(self) -> bool:
        if not self.settings.oracle_user.strip() or not self.settings.oracle_dsn.strip():
            return False
        if _oracle_connection_security(self.settings) == "walletless_tls":
            return bool(self.settings.oracle_password.strip())
        wallet_dir = self.settings.resolved_oracle_wallet_dir.strip()
        wallet_path = Path(wallet_dir).expanduser() if wallet_dir else None
        return bool(
            wallet_path
            and _wallet_mtls_files_exist(
                wallet_path,
                required_files=wallet_mtls_required_files(self.settings),
            )
        )

    def module_available(self) -> bool:
        try:
            self._load_oracledb()
        except OracleAdapterError:
            return False
        return True

    def test_connection(self) -> tuple[bool, str]:
        if not self.is_configured():
            return False, "Oracle 接続情報が不足しています。"
        try:
            with self.connection() as conn, conn.cursor() as cursor:
                cursor.execute("SELECT 1 FROM DUAL")
                cursor.fetchone()
            return True, "Oracle 接続に成功しました。"
        except Exception as exc:
            return False, f"Oracle 接続に失敗しました: {exc}"

    @contextmanager
    def connection(self, *, call_timeout_seconds: float | None = None) -> Iterator[Any]:
        oracledb = self._load_oracledb()
        self._init_client(oracledb)
        if not self.is_configured():
            raise OracleAdapterError("Oracle 接続情報が不足しています。")
        for attempt in range(self.connect_attempts):
            try:
                conn = oracledb.connect(**_oracle_connect_kwargs(self.settings))
                break
            except OracleAdapterError:
                raise
            except Exception as exc:  # 接続確立前だけ再試行し、yield 後の SQL は再送しない。
                message = str(exc)
                if (
                    "DPY-6005" in message
                    and "timed out" in message.lower()
                    and attempt + 1 < self.connect_attempts
                ):
                    time.sleep(0.25 * (attempt + 1))
                    continue
                diagnostics = oracle_connection_diagnostics(exc)
                logger.error(
                    "%s %s",
                    diagnostics["summary"],
                    diagnostics["suggested_action"],
                    extra={
                        **diagnostics,
                        "event": "oracle_connection_failed",
                        "operation": "connect",
                        "attempts": attempt + 1,
                    },
                )
                raise OracleAdapterError(f"Oracle 接続に失敗しました: {exc}") from exc
        try:
            # python-oracledb call_timeout は 1 round-trip 単位の millisecond。
            # 0 (無期限) を避け、DBMS_CLOUD_AI/Agent PL/SQL が worker を占有し続けないようにする。
            timeout_seconds = (
                call_timeout_seconds
                if call_timeout_seconds is not None
                else self.settings.nl2sql_oracle_call_timeout_seconds
            )
            call_timeout_ms = int(max(1.0, float(timeout_seconds)) * 1000)
            if hasattr(conn, "call_timeout"):
                conn.call_timeout = call_timeout_ms
            # result cache を使わない（ADB の内部エラーを避ける。#333）。失敗は finally で閉じる。
            init_oracle_session(conn)
            try:
                yield conn
            except Exception:
                rollback = getattr(conn, "rollback", None)
                if callable(rollback):
                    rollback()
                raise
        finally:
            conn.close()

    @contextmanager
    def pooled_connection(self) -> Iterator[Any]:
        """共通認証の store 用に、pool から接続を借りる（#793）。

        `connection()` と同じく、未コミットの変更を持たない（例外時は rollback して返す）。
        呼び出しの timeout は借りるたびに `connection()` と同じ値を設定する。業務データ・DeepSec の
        接続には使わない。
        """
        oracledb = self._load_oracledb()
        self._init_client(oracledb)
        if not self.is_configured():
            raise OracleAdapterError("Oracle 接続情報が不足しています。")
        try:
            conn = _AUTH_CONNECTION_POOL.acquire(_oracle_connect_kwargs(self.settings))
        except OracleAdapterError:
            raise
        except Exception as exc:
            diagnostics = oracle_connection_diagnostics(exc)
            logger.error(
                "%s %s",
                diagnostics["summary"],
                diagnostics["suggested_action"],
                extra={
                    **diagnostics,
                    "event": "oracle_connection_failed",
                    "operation": "acquire",
                    "pool": _AUTH_CONNECTION_POOL.name,
                },
            )
            raise OracleAdapterError(f"Oracle 接続に失敗しました: {exc}") from exc
        try:
            if hasattr(conn, "call_timeout"):
                conn.call_timeout = int(
                    max(1.0, float(self.settings.nl2sql_oracle_call_timeout_seconds)) * 1000
                )
            yield conn
        except BaseException:
            with suppress(Exception):
                conn.rollback()
            raise
        finally:
            # pool に返す（未コミットの変更は rollback される。切れた接続は pool が捨てる）。
            with suppress(Exception):
                conn.close()

    @contextmanager
    def state_connection(self) -> Iterator[Any]:
        """状態の保存先（`NL2SQL_*` の表）の読み書きに、pool から接続を借りる（#830）。

        `connection()` と同じく、返すときに未コミットの変更を持たない（例外のときは rollback し、
        正常に抜けても pool に返すときに python-oracledb が rollback する。commit は呼び出し側）。
        呼び出しの timeout は借りるたびに `connection()` と同じ値を設定し、result cache の無効化
        （`init_oracle_session`）は pool の session callback が新しい接続ごとに当てる。
        同じスレッドが借りたまま入れ子で借りるときは、pool の上限で自分を待たないよう単発の接続に
        する。業務データ・Select AI・DeepSec の接続には使わない（session の状態を持ち得る）。
        CLOB 列は文字列で fetch する（`configure_connection_clob_fetch_as_text`。#904）。
        """
        depth = int(getattr(_STATE_CONNECTION_DEPTH, "value", 0))
        if depth > 0:
            logger.debug("nl2sql_state_connection_nested", extra={"depth": depth})
            with self.connection() as conn:
                configure_connection_clob_fetch_as_text(conn)
                yield conn
            return
        oracledb = self._load_oracledb()
        self._init_client(oracledb)
        if not self.is_configured():
            raise OracleAdapterError("Oracle 接続情報が不足しています。")
        _STATE_CONNECTION_POOL.resize(
            OraclePoolSize.of(1, self.settings.nl2sql_oracle_state_pool_max)
        )
        try:
            conn = _STATE_CONNECTION_POOL.acquire(_oracle_connect_kwargs(self.settings))
        except OracleAdapterError:
            raise
        except Exception as exc:
            diagnostics = oracle_connection_diagnostics(exc)
            logger.error(
                "%s %s",
                diagnostics["summary"],
                diagnostics["suggested_action"],
                extra={
                    **diagnostics,
                    "event": "oracle_connection_failed",
                    "operation": "acquire",
                    "pool": _STATE_CONNECTION_POOL.name,
                },
            )
            raise OracleAdapterError(f"Oracle 接続に失敗しました: {exc}") from exc
        _STATE_CONNECTION_DEPTH.value = depth + 1
        try:
            if hasattr(conn, "call_timeout"):
                conn.call_timeout = int(
                    max(1.0, float(self.settings.nl2sql_oracle_call_timeout_seconds)) * 1000
                )
            # JSON CLOB は行と一緒に文字列で受け取る（行ごとの LOB の read の往復を無くす。#904）。
            configure_connection_clob_fetch_as_text(conn)
            yield conn
        except BaseException:
            with suppress(Exception):
                conn.rollback()
            raise
        finally:
            _STATE_CONNECTION_DEPTH.value = depth
            # pool に返す（未コミットの変更は rollback される。切れた接続は pool が捨てる）。
            with suppress(Exception):
                conn.close()

    @contextmanager
    def runtime_connection(self, *, call_timeout_seconds: float | None = None) -> Iterator[Any]:
        """アプリの接続で業務データを読む・Select AI で生成するときに、pool から借りる（#904）。

        DB の利用者は `connection()` と同じアプリの利用者（`PLATFORM_ORACLE_USER`）。使うのは
        session の状態を変えない処理（SELECT の実行・`DBMS_CLOUD_AI.GENERATE`・類似履歴の
        ベクトル検索）だけで、DeepSec の DATA USER の接続（利用者の context を設定する）・
        状態の保存先の pool とは混ぜない。
        `connection()` と同じく、呼び出しの timeout は借りるたびに設定し、result cache の無効化は
        pool の session callback が新しい接続ごとに当てる。例外のときと、未確定のトランザクションが
        残っているときは rollback してから返す。

        pool は今の actor（`current_actor_context()`。worker ではジョブの actor の `actor_scope`）で
        選ぶ。system_admin は `nl2sql-runtime-admin`、それ以外の認証済みの利用者は
        `nl2sql-runtime-user` で、同じ pool・同じ接続を共有しない。actor が無いとき（システムの
        処理・認証が無効のとき）は、どちらの actor の接続とも混ぜないよう pool を使わず、
        今までどおり単発の接続（`connection()`）にする。
        """
        from app.security.request_actor import current_actor_context

        actor = current_actor_context()
        if not actor.is_system_admin and not actor.user_uuid:
            with (
                self.connection(call_timeout_seconds=call_timeout_seconds)
                if call_timeout_seconds is not None
                else self.connection()
            ) as single:
                yield single
            return
        pool = (
            _RUNTIME_ADMIN_CONNECTION_POOL
            if actor.is_system_admin
            else _RUNTIME_USER_CONNECTION_POOL
        )
        oracledb = self._load_oracledb()
        self._init_client(oracledb)
        if not self.is_configured():
            raise OracleAdapterError("Oracle 接続情報が不足しています。")
        pool.resize(OraclePoolSize.of(1, self.settings.nl2sql_oracle_runtime_pool_max))
        try:
            conn = pool.acquire(_oracle_connect_kwargs(self.settings))
        except OracleAdapterError:
            raise
        except Exception as exc:
            diagnostics = oracle_connection_diagnostics(exc)
            logger.error(
                "%s %s",
                diagnostics["summary"],
                diagnostics["suggested_action"],
                extra={
                    **diagnostics,
                    "event": "oracle_connection_failed",
                    "operation": "acquire",
                    "pool": pool.name,
                },
            )
            raise OracleAdapterError(f"Oracle 接続に失敗しました: {exc}") from exc
        try:
            timeout_seconds = (
                call_timeout_seconds
                if call_timeout_seconds is not None
                else self.settings.nl2sql_oracle_call_timeout_seconds
            )
            if hasattr(conn, "call_timeout"):
                conn.call_timeout = int(max(1.0, float(timeout_seconds)) * 1000)
            yield conn
            if getattr(conn, "transaction_in_progress", False):
                conn.rollback()
        except BaseException:
            with suppress(Exception):
                conn.rollback()
            raise
        finally:
            # pool に返す（切れた接続は pool が捨てる）。
            with suppress(Exception):
                conn.close()

    @contextmanager
    def user_data_connection(self, *, read_only: bool = False) -> Iterator[Any]:
        """認証済み actor のデータ処理にだけ共有 DeepSec DATA USER を使う。

        - DeepSec 無効: 全員がアプリの接続
        - DeepSec 有効で system_admin: アプリの接続（DeepSec の制限を受けない）
        - DeepSec 有効で非 system_admin: DeepSec の DATA USER の pool（借りるたびに利用者の
          context を設定し、返すときに消す。消せなければ捨てる。
          `OraclePoolManager.data_connection`）

        `read_only=True`（SELECT だけを実行し、トランザクションを残さない）なら、アプリの接続は
        `runtime_connection()`（system_admin 用・非 system_admin 用で別の pool）から借りる（#904）。
        DeepSec の
        DATA USER の接続は `read_only` にかかわらず今までどおり（アプリの接続の pool と
        共有しない）。
        """
        app_connection = self.runtime_connection if read_only else self.connection
        if not self.settings.oracle_deepsec_enabled:
            with app_connection() as connection:
                yield connection
            return
        from app.clients.oracle_runtime import get_oracle_pool_manager
        from app.security.request_actor import current_actor_context

        actor = current_actor_context()
        if actor.is_system_admin:
            with app_connection() as connection:
                yield connection
            return
        if not actor.user_uuid:
            raise OracleAdapterError("DeepSec データ接続には認証済み application user が必要です。")
        with get_oracle_pool_manager().data_connection(actor.user_uuid) as connection:
            yield connection

    def fetch_catalog(
        self,
        *,
        include_samples: bool = True,
        object_keys: set[tuple[str, str]] | None = None,
    ) -> SchemaCatalog:
        owner_filter, owner_binds = self._schema_owner_filter("c.owner")
        target_filter = ""
        target_binds: dict[str, str] = {}
        if object_keys:
            target_parts: list[str] = []
            for index, (owner, object_name) in enumerate(sorted(object_keys)):
                owner_key = f"target_owner_{index}"
                name_key = f"target_name_{index}"
                target_parts.append(f"(c.owner = :{owner_key} AND c.table_name = :{name_key})")
                # object key は辞書ビュー上の名前（大文字小文字を保持）。大文字化すると
                # `Mixed_Case` の代わりに大文字の同名表 `MIXED_CASE` を取得する（#563）。
                target_binds[owner_key] = owner
                target_binds[name_key] = object_name
            target_filter = " AND (" + " OR ".join(target_parts) + ")"
        # owner_filter / target_filter は固定 fragment と bind だけで組み立てる。
        sql = (
            "SELECT\n"  # nosec B608
            "    c.owner,\n"
            "    c.table_name,\n"
            "    NVL(tc.comments, c.table_name) AS table_comment,\n"
            "    c.column_name,\n"
            "    NVL(cc.comments, c.column_name) AS column_comment,\n"
            "    c.data_type,\n"
            "    c.nullable,\n"
            "    c.column_id,\n"
            "    t.num_rows,\n"
            "    NVL(o.object_type, 'TABLE') AS object_type,\n"
            "    c.data_length,\n"
            "    c.char_length,\n"
            "    c.data_precision,\n"
            "    c.data_scale,\n"
            "    c.char_used\n"
            "FROM all_tab_columns c\n"
            "LEFT JOIN all_tables t ON t.owner = c.owner AND t.table_name = c.table_name\n"
            "LEFT JOIN all_objects o\n"
            "  ON o.owner = c.owner\n"
            " AND o.object_name = c.table_name\n"
            " AND o.object_type IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW')\n"
            "LEFT JOIN all_tab_comments tc ON tc.owner = c.owner AND tc.table_name = c.table_name\n"
            "LEFT JOIN all_col_comments cc\n"
            "  ON cc.owner = c.owner\n"
            " AND cc.table_name = c.table_name\n"
            " AND cc.column_name = c.column_name\n"
            f"WHERE {owner_filter}\n"
            f"  AND {_CATALOG_VISIBLE_SQL}\n"
            f"  {target_filter}\n"
            "ORDER BY c.owner, c.table_name, c.column_id"
        )
        tables: dict[str, SchemaTable] = {}
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(sql, {**owner_binds, **target_binds})
            for row in cursor:
                owner = str(row[0] or "APP")
                table_name = str(row[1])
                table_comment = str(row[2] or table_name)
                column_name = str(row[3])
                column_comment = str(row[4] or column_name)
                # 精度・桁・長さを含む物理の型（`NUMBER(12,2)`・`VARCHAR2(50)`）にする。
                # オントロジーの物理の型（Physical Type）が DB の列と一致する（#1733）。
                data_type = (
                    _catalog_column_type(row[5], *row[10:15]) if len(row) >= 15 else str(row[5])
                )
                nullable = str(row[6]).upper() == "Y"
                row_count = int(row[8]) if row[8] is not None else None
                table_type = str(row[9]).lower() if len(row) > 9 and row[9] else "table"
                table_key = f"{owner}.{table_name}"
                table = tables.setdefault(
                    table_key,
                    SchemaTable(
                        table_name=table_name,
                        logical_name=table_comment,
                        owner=owner,
                        table_type=table_type,
                        comment=table_comment,
                        row_count=row_count,
                    ),
                )
                table.columns.append(
                    SchemaColumn(
                        column_name=column_name,
                        logical_name=column_comment,
                        data_type=data_type,
                        nullable=nullable,
                    )
                )
            self._load_constraints(cursor, tables, object_keys=object_keys)
            view_dependencies = self._load_view_dependencies(cursor, object_keys=object_keys)
            # 全 catalog refresh で全表を走査しない。sample は profile 選択時または
            # object detail 展開時に fetch_metadata_sample_values から遅延取得する。
            if include_samples:
                self._load_sample_values(cursor, tables)
        catalog = filter_user_visible_catalog(
            SchemaCatalog(
                refreshed_at=datetime.now(UTC).isoformat(),
                tables=list(tables.values()),
                view_dependencies=view_dependencies,
            )
        )
        catalog.schema_fingerprint = self._schema_fingerprint(catalog)
        return catalog

    def fetch_schema_owners(self) -> SchemaOwnersData:
        """ALL_* から現在ユーザーが実際に参照できる業務 schema を列挙する。"""

        allowlist = self._configured_schema_owner_allowlist()
        # _OBJECT_VISIBLE_SQL は固定の条件（利用者の入力を含まない）。
        sql = (
            "SELECT\n"  # nosec B608
            "    o.owner,\n"
            "    NVL(u.oracle_maintained, 'N') AS oracle_maintained,\n"
            "    MAX(CASE WHEN o.owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')\n"
            "             THEN 1 ELSE 0 END) AS is_current,\n"
            "    COUNT(DISTINCT CASE WHEN o.object_type = 'TABLE'\n"
            "                        THEN o.object_name END) AS table_count,\n"
            "    COUNT(DISTINCT CASE WHEN o.object_type IN ('VIEW', 'MATERIALIZED VIEW')\n"
            "                        THEN o.object_name END) AS view_count\n"
            "FROM all_objects o\n"
            "JOIN all_users u ON u.username = o.owner\n"
            "WHERE o.object_type IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW')\n"
            "  AND o.status = 'VALID'\n"
            f"  AND {_OBJECT_VISIBLE_SQL}\n"
            "GROUP BY o.owner, NVL(u.oracle_maintained, 'N')\n"
            "ORDER BY o.owner"
        )
        owners: list[SchemaOwnerSummary] = []
        excluded_maintained: set[str] = set()
        current_owner = self.settings.oracle_user.strip().upper()
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(sql)
            for row in cursor:
                # 小文字を含む user（引用付きで作成）を大文字の同名 user と取り違えない（#563）。
                owner = str(row[0] or "")
                oracle_maintained = str(row[1] or "N").upper() == "Y"
                is_current = bool(int(row[2] or 0))
                if is_current:
                    current_owner = owner
                if oracle_maintained:
                    excluded_maintained.add(owner)
                    continue
                if not is_user_visible_owner_name(owner):
                    continue
                if allowlist and owner not in allowlist:
                    continue
                owners.append(
                    SchemaOwnerSummary(
                        owner=owner,
                        is_current=is_current,
                        table_count=int(row[3] or 0),
                        view_count=int(row[4] or 0),
                    )
                )
        return SchemaOwnersData(
            current_owner=current_owner,
            owners=owners,
            excluded_oracle_maintained_count=len(excluded_maintained),
        )

    def fetch_catalog_objects(self, object_keys: set[tuple[str, str]]) -> SchemaCatalog:
        """変更された object だけを詳細取得する（Oracle bind 数を bounded に保つ）。"""

        if not object_keys:
            return SchemaCatalog(refreshed_at=datetime.now(UTC).isoformat(), tables=[])
        tables: list[SchemaTable] = []
        dependencies: dict[tuple[str, str, str, str], SchemaViewDependency] = {}
        ordered = sorted(object_keys)
        for offset in range(0, len(ordered), 250):
            partial = self.fetch_catalog(
                include_samples=False,
                object_keys=set(ordered[offset : offset + 250]),
            )
            tables.extend(partial.tables)
            for dependency in partial.view_dependencies:
                key = (
                    dependency.owner,
                    dependency.view_name,
                    dependency.referenced_owner,
                    dependency.referenced_name,
                )
                dependencies[key] = dependency
        catalog = SchemaCatalog(
            refreshed_at=datetime.now(UTC).isoformat(),
            tables=tables,
            view_dependencies=list(dependencies.values()),
        )
        catalog.schema_fingerprint = self._schema_fingerprint(catalog)
        return catalog

    def fetch_schema_manifest(
        self,
        object_keys: set[tuple[str, str]] | None = None,
    ) -> dict[tuple[str, str], str]:
        """ALL_OBJECTS から軽量 change manifest だけを取得する。"""

        owner_filter, owner_binds = self._schema_owner_filter("o.owner")
        target_filter, target_binds = self._object_key_filter(
            "o.owner", "o.object_name", object_keys, prefix="manifest_target"
        )
        # owner_filter / target_filter は固定 fragment と bind だけで組み立てる。
        sql = (
            "SELECT o.owner, o.object_name, o.last_ddl_time\n"  # nosec B608
            "FROM all_objects o\n"
            f"WHERE {owner_filter}\n"
            "  AND o.object_type IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW')\n"
            "  AND o.status = 'VALID'\n"
            f"  AND {_OBJECT_VISIBLE_SQL}\n"
            f"  {target_filter}\n"
            "ORDER BY o.owner, o.object_name"
        )
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(sql, {**owner_binds, **target_binds})
            return {
                (str(owner), str(object_name)): (
                    last_ddl_time.isoformat()
                    if hasattr(last_ddl_time, "isoformat")
                    else str(last_ddl_time or "")
                )
                for owner, object_name, last_ddl_time in cursor
                if is_user_visible_schema_object(str(owner or ""), str(object_name or ""))
            }

    def catalog_fingerprint(self, catalog: SchemaCatalog) -> str:
        """Refresh worker 用の public deterministic fingerprint。"""

        return self._schema_fingerprint(catalog)

    def _configured_schema_owner_allowlist(self) -> set[str]:
        return {
            owner.strip().upper()
            for owner in self.settings.nl2sql_schema_owner_allowlist
            if owner.strip()
        }

    def _schema_owner_filter(self, column_sql: str) -> tuple[str, dict[str, str]]:
        owners = self._configured_schema_owner_allowlist()
        business_owner_filter = (
            "EXISTS (SELECT 1 FROM all_users nl2sql_owner "  # nosec B608
            f"WHERE nl2sql_owner.username = {column_sql} "
            "AND NVL(nl2sql_owner.oracle_maintained, 'N') = 'N')"
        )
        if not owners:
            return business_owner_filter, {}
        binds = {f"owner_{index}": owner for index, owner in enumerate(sorted(owners))}
        placeholders = ", ".join(f":{name}" for name in binds)
        return f"{business_owner_filter} AND {column_sql} IN ({placeholders})", binds

    def _db_admin_identity(self, object_name: str, owner: str = "") -> OracleObjectIdentity:
        requested_owner = normalize_object_part(owner) if owner.strip() else ""
        try:
            identity = parse_object_identity(
                object_name,
                default_owner=requested_owner or self.settings.oracle_user,
            )
        except ValueError as exc:
            raise OracleAdapterError(str(exc)) from exc
        if requested_owner and identity.owner != requested_owner:
            raise OracleAdapterError("owner と object_name の owner 指定が一致しません。")
        if not is_user_visible_schema_object(identity.owner, identity.object_name):
            raise OracleAdapterError(SYSTEM_OBJECT_BLOCKED_MESSAGE)
        return identity

    def _load_constraints(
        self,
        cursor: Any,
        tables: dict[str, SchemaTable],
        *,
        object_keys: set[tuple[str, str]] | None = None,
    ) -> None:
        owner_filter, owner_binds = self._schema_owner_filter("uc.owner")
        target_filter, target_binds = self._object_key_filter(
            "uc.owner", "uc.table_name", object_keys, prefix="constraint_target"
        )
        # owner_filter / target_filter は固定 fragment と bind だけで組み立てる。
        constraint_sql = (
            "SELECT\n"  # nosec B608
            "    uc.table_name,\n"
            "    uc.constraint_name,\n"
            "    uc.constraint_type,\n"
            "    LISTAGG(ucc.column_name, ', ') WITHIN GROUP (ORDER BY ucc.position) AS columns,\n"
            "    uc.owner AS owner_name,\n"
            "    ruc.owner AS referenced_owner,\n"
            "    ruc.table_name AS referenced_table,\n"
            "    LISTAGG(rucc.column_name, ', ') WITHIN GROUP (ORDER BY rucc.position)\n"
            "        AS referenced_columns,\n"
            "    uc.delete_rule,\n"
            "    uc.status,\n"
            "    uc.deferrable\n"
            "FROM all_constraints uc\n"
            "LEFT JOIN all_cons_columns ucc\n"
            "  ON ucc.owner = uc.owner\n"
            " AND ucc.constraint_name = uc.constraint_name\n"
            " AND ucc.table_name = uc.table_name\n"
            "LEFT JOIN all_constraints ruc\n"
            "  ON ruc.owner = uc.r_owner\n"
            " AND ruc.constraint_name = uc.r_constraint_name\n"
            "LEFT JOIN all_cons_columns rucc\n"
            "  ON rucc.owner = ruc.owner\n"
            " AND rucc.constraint_name = ruc.constraint_name\n"
            " AND rucc.table_name = ruc.table_name\n"
            " AND rucc.position = ucc.position\n"
            f"WHERE {owner_filter}\n"
            f"  {target_filter}\n"
            "  AND uc.constraint_type IN ('P', 'R', 'U', 'C')\n"
            "GROUP BY uc.table_name, uc.constraint_name, uc.constraint_type,\n"
            "         uc.owner, ruc.owner, ruc.table_name,\n"
            "         uc.delete_rule, uc.status, uc.deferrable\n"
            "ORDER BY uc.table_name, uc.constraint_name"
        )
        cursor.execute(constraint_sql, {**owner_binds, **target_binds})
        for row in cursor:
            table_name, constraint_name, constraint_type, columns = row[:4]
            owner = str(row[4]) if len(row) > 4 and row[4] else "APP"
            table = tables.get(f"{owner}.{table_name}")
            if not table:
                continue
            column_text = str(columns or "").strip()
            suffix = f"({column_text})" if column_text else ""
            table.constraints.append(f"{constraint_name} {constraint_type}{suffix}")
            referenced_owner = str(row[5]) if len(row) > 5 and row[5] else None
            referenced_table = str(row[6]) if len(row) > 6 and row[6] else None
            referenced_columns_text = str(row[7] or "") if len(row) > 7 else ""
            table.constraint_details.append(
                SchemaConstraintDetail(
                    constraint_name=str(constraint_name),
                    constraint_type=str(constraint_type),
                    owner=owner,
                    table_name=str(table_name),
                    columns=[value.strip() for value in column_text.split(",") if value.strip()],
                    referenced_owner=referenced_owner,
                    referenced_table=referenced_table,
                    referenced_columns=[
                        value.strip()
                        for value in referenced_columns_text.split(",")
                        if value.strip()
                    ],
                    delete_rule=str(row[8]) if len(row) > 8 and row[8] else "NO ACTION",
                    status=str(row[9]) if len(row) > 9 and row[9] else "ENABLED",
                    deferrable=(str(row[10]) if len(row) > 10 and row[10] else "NOT DEFERRABLE"),
                )
            )

    def _load_view_dependencies(
        self,
        cursor: Any,
        *,
        object_keys: set[tuple[str, str]] | None = None,
    ) -> list[SchemaViewDependency]:
        owner_filter, owner_binds = self._schema_owner_filter("d.owner")
        target_filter, target_binds = self._object_key_filter(
            "d.owner", "d.name", object_keys, prefix="dependency_target"
        )
        # owner_filter / target_filter は固定 fragment と bind だけで組み立てる。
        dependency_sql = (
            "SELECT\n"  # nosec B608
            "    d.owner AS owner_name,\n"
            "    d.name AS view_name,\n"
            "    d.referenced_owner,\n"
            "    d.referenced_name,\n"
            "    d.referenced_type\n"
            "FROM all_dependencies d\n"
            "WHERE d.type IN ('VIEW', 'MATERIALIZED VIEW')\n"
            "  AND d.referenced_type IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW')\n"
            f"  AND {owner_filter}\n"
            f"  AND {_DEPENDENCY_VISIBLE_SQL}\n"
            f"  AND {_DEPENDENCY_REFERENCED_VISIBLE_SQL}\n"
            f"  {target_filter}\n"
            "ORDER BY d.name, d.referenced_owner, d.referenced_name"
        )
        cursor.execute(dependency_sql, {**owner_binds, **target_binds})
        return [
            SchemaViewDependency(
                owner=str(owner),
                view_name=str(view_name),
                referenced_owner=str(referenced_owner),
                referenced_name=str(referenced_name),
                referenced_type=str(referenced_type),
            )
            for owner, view_name, referenced_owner, referenced_name, referenced_type in cursor
        ]

    @staticmethod
    def _object_key_filter(
        owner_column: str,
        name_column: str,
        object_keys: set[tuple[str, str]] | None,
        *,
        prefix: str,
    ) -> tuple[str, dict[str, str]]:
        """変更 object の metadata 関連 query を同じ bounded batch に限定する。"""

        if not object_keys:
            return "", {}
        binds: dict[str, str] = {}
        clauses: list[str] = []
        for index, (owner, object_name) in enumerate(sorted(object_keys)):
            owner_key = f"{prefix}_owner_{index}"
            name_key = f"{prefix}_name_{index}"
            binds[owner_key] = owner
            binds[name_key] = object_name
            clauses.append(f"({owner_column} = :{owner_key} AND {name_column} = :{name_key})")
        return "AND (" + " OR ".join(clauses) + ")", binds

    def _schema_fingerprint(self, catalog: SchemaCatalog) -> str:
        payload = {
            "tables": [
                {
                    "owner": table.owner,
                    "name": table.table_name,
                    "type": table.table_type.upper(),
                    "columns": [
                        (column.column_name, column.data_type.upper(), column.nullable)
                        for column in table.columns
                    ],
                    "constraints": [
                        detail.model_dump(mode="json") for detail in table.constraint_details
                    ],
                }
                for table in catalog.tables
            ],
            "view_dependencies": [
                dependency.model_dump(mode="json") for dependency in catalog.view_dependencies
            ],
        }
        normalized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _load_sample_values(self, cursor: Any, tables: dict[str, SchemaTable]) -> None:
        sample_rows = max(self.settings.nl2sql_schema_sample_rows, 0)
        sample_columns = max(self.settings.nl2sql_schema_sample_columns_per_table, 0)
        if sample_rows == 0 or sample_columns == 0:
            return
        for table in tables.values():
            quoted_table = (
                f"{_quote_identifier(table.owner)}.{_quote_identifier(table.table_name)}"
                if table.owner
                else _quote_identifier(table.table_name)
            )
            for column in table.columns[:sample_columns]:
                quoted_column = _quote_identifier(column.column_name)
                try:
                    # Safe: identifiers come from Oracle catalog metadata and are quoted.
                    sample_sql = (
                        f"SELECT DISTINCT {quoted_column} "  # nosec B608
                        f"FROM {quoted_table} "
                        f"WHERE {quoted_column} IS NOT NULL "
                        "FETCH FIRST :sample_rows ROWS ONLY"
                    )
                    cursor.execute(
                        sample_sql,
                        {"sample_rows": sample_rows},
                    )
                    column.sample_values = [
                        _coerce_text(row[0]) for row in cursor if _coerce_text(row[0])
                    ]
                except Exception:
                    column.sample_values = []

    def fetch_domain_inventory(
        self, targets: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """対象表の列に付いた SQL ドメインの定義と schema 内の関連付け列を集める。

        SQL ドメインに対応したバージョンの ALL_TAB_COLS.DOMAIN_NAME / ALL_DOMAINS /
        ALL_DOMAIN_COLS / ALL_DOMAIN_CONSTRAINTS / ALL_ANNOTATIONS_USAGE を使う。
        dictionary 列が無い版(ORA-00904)では warning を付けて空を返し、作成フローを壊さない。
        """
        warnings: list[str] = []
        domain_keys: list[tuple[str, str]] = []
        domains: list[dict[str, Any]] = []
        try:
            with self.connection() as conn, conn.cursor() as cursor:
                for target in targets:
                    identity = parse_object_identity(
                        str(target.get("object_name") or ""),
                        default_owner=str(target.get("owner") or "") or self.settings.oracle_user,
                    )
                    try:
                        cursor.execute(
                            """
                            SELECT DISTINCT domain_owner, domain_name
                            FROM all_tab_cols
                            WHERE owner = :owner AND table_name = :table_name
                              AND domain_name IS NOT NULL
                            """,
                            {"owner": identity.owner, "table_name": identity.object_name},
                        )
                    except Exception as exc:
                        warnings.append(
                            "この Oracle ではドメインの dictionary 列を参照できないため"
                            f"既存ドメインを取得しませんでした: {exc}"
                        )
                        return [], warnings
                    for domain_owner, domain_name in cursor.fetchall():
                        key = (str(domain_owner or ""), str(domain_name or ""))
                        if all(key) and key not in domain_keys:
                            domain_keys.append(key)
                for owner, name in domain_keys:
                    try:
                        definition = self._load_domain_definition(cursor, owner, name)
                    except Exception as exc:
                        warnings.append(f"{owner}.{name}: ドメイン定義の取得に失敗しました: {exc}")
                        continue
                    if definition is not None:
                        domains.append(definition)
        except OracleAdapterError:
            raise
        except Exception as exc:
            raise OracleAdapterError(f"ドメイン情報の取得に失敗しました: {exc}") from exc
        return domains, warnings

    def _load_domain_definition(self, cursor: Any, owner: str, name: str) -> dict[str, Any] | None:
        cursor.execute(
            """
            SELECT cols, type, data_display, data_order
            FROM all_domains
            WHERE owner = :owner AND name = :name
            """,
            {"owner": owner, "name": name},
        )
        head = cursor.fetchone()
        if head is None:
            return None
        cols = int(head[0] or 0)
        kind = str(head[1] or "").upper()
        if kind == "FLEXIBLE":
            domain_type = "flexible"
        elif kind == "ENUMERATED":
            domain_type = "enumerated"
        elif cols > 1:
            domain_type = "multi_column"
        else:
            domain_type = "single"
        display = _coerce_text(head[2]).strip()
        order = _coerce_text(head[3]).strip()

        cursor.execute(
            """
            SELECT data_type, data_length, char_length, data_precision, data_scale,
                   nullable, exact
            FROM all_domain_cols
            WHERE owner = :owner AND domain_name = :name AND data_type_id = 1
            ORDER BY column_id
            """,
            {"owner": owner, "name": name},
        )
        column_rows = cursor.fetchall()
        data_type = ""
        strict = False
        nullable = True
        if column_rows and domain_type == "single":
            first = column_rows[0]
            data_type = _domain_column_type(first)
            nullable = str(first[5] or "Y").upper() == "Y"
            strict = bool(first[6])

        cursor.execute(
            """
            SELECT search_condition
            FROM all_domain_constraints
            WHERE domain_owner = :owner AND domain_name = :name AND constraint_type = 'C'
            ORDER BY name
            """,
            {"owner": owner, "name": name},
        )
        constraints = []
        for (condition,) in cursor.fetchall():
            text = _coerce_text(condition).strip()
            if text and not _DOMAIN_NOT_NULL_CONDITION.match(text):
                constraints.append(text)

        cursor.execute(
            """
            SELECT c.owner, c.table_name, c.column_name
            FROM all_tab_cols c
            WHERE c.domain_owner = :owner AND c.domain_name = :name
              AND c.hidden_column = 'NO'
              AND EXISTS (
                SELECT 1 FROM all_tables t
                WHERE t.owner = c.owner AND t.table_name = c.table_name
              )
            ORDER BY c.owner, c.table_name, c.column_id
            """,
            {"owner": owner, "name": name},
        )
        columns = [
            {
                "owner": str(row[0] or ""),
                "table_name": str(row[1] or ""),
                "column_name": str(row[2] or ""),
            }
            for row in cursor.fetchall()
        ]
        return {
            "owner": owner,
            "name": name,
            "domain_type": domain_type,
            "data_type": data_type,
            "strict": strict,
            "nullable": nullable,
            "constraints": constraints,
            "display": display,
            "order": order,
            "annotations": self._load_domain_annotations(cursor, owner, name),
            "columns": columns,
        }

    def _load_domain_annotations(self, cursor: Any, owner: str, name: str) -> list[dict[str, str]]:
        """ドメインの annotation を列レベル → オブジェクトレベルの順で重複なく集める。

        ALL_ANNOTATIONS_USAGE の owner 列名は版で異なりうるため SELECT * で列名を見て絞る。
        """
        cursor.execute(
            """
            SELECT * FROM all_annotations_usage
            WHERE object_name = :name AND object_type = 'DOMAIN'
            """,
            {"name": name},
        )
        names = [str(item[0]).upper() for item in cursor.description or []]
        rows = cursor.fetchall()
        owner_index = next((names.index(c) for c in ("OWNER", "OBJECT_OWNER") if c in names), -1)
        column_index = names.index("COLUMN_NAME") if "COLUMN_NAME" in names else -1
        name_index = names.index("ANNOTATION_NAME")
        value_index = names.index("ANNOTATION_VALUE")
        ordered = sorted(rows, key=lambda row: 0 if column_index >= 0 and row[column_index] else 1)
        annotations: list[dict[str, str]] = []
        seen: set[str] = set()
        for row in ordered:
            if owner_index >= 0 and str(row[owner_index] or "") != owner:
                continue
            annotation_name = str(row[name_index] or "")
            if not annotation_name or annotation_name.upper() in seen:
                continue
            seen.add(annotation_name.upper())
            annotations.append({"name": annotation_name, "value": _coerce_text(row[value_index])})
        return annotations

    def fetch_metadata_sample_values(
        self, targets: list[dict[str, Any]], sample_limit: int
    ) -> tuple[dict[str, dict[str, list[str]]], list[str]]:
        """選択済み table/view の列代表値を、生成時の指定件数で取得する。"""
        if sample_limit <= 0:
            return {}, []

        samples: dict[str, dict[str, list[str]]] = {}
        warnings: list[str] = []
        try:
            with self.connection() as conn, conn.cursor() as cursor:
                for target in targets:
                    raw_object_name = str(target.get("object_name") or "")
                    raw_owner = str(target.get("owner") or "")
                    # owner / object / column は Oracle の引用規則で解釈する（引用なしは大文字、
                    # `"Mixed_Case"` は大文字小文字を保持）。引用符を外して大文字化すると、
                    # 大文字の同名表・同名列の代表値を取得する（#563）。
                    identity = parse_object_identity(
                        raw_object_name,
                        default_owner=raw_owner or self.settings.oracle_user,
                    )
                    owner = _sample_catalog_name(identity.owner)
                    object_name = _sample_catalog_name(identity.object_name)
                    requested_columns = [
                        _sample_catalog_name(normalize_object_part(str(column)))
                        for column in target.get("columns", [])
                        if str(column).strip()
                    ]
                    cursor.execute(
                        """
                        SELECT column_name FROM all_tab_columns
                        WHERE owner = :owner AND table_name = :object_name
                        ORDER BY column_id
                        """,
                        {"owner": owner, "object_name": object_name},
                    )
                    available_columns = {str(row[0]) for row in cursor}
                    columns = requested_columns or list(available_columns)
                    columns = [column for column in columns if column in available_columns]
                    skipped_columns = set(requested_columns) - available_columns
                    if skipped_columns:
                        warnings.append(
                            f"{object_name}: 存在しない列を除外しました: "
                            + ", ".join(sorted(skipped_columns))
                        )
                    if not columns:
                        warnings.append(f"{object_name}: サンプル取得可能な列がありません。")
                        continue
                    object_samples: dict[str, list[str]] = {}
                    qualified_name = qualified_object_name(owner, object_name)
                    quoted_object = f"{_quote_identifier(owner)}.{_quote_identifier(object_name)}"
                    for column in columns:
                        try:
                            quoted_column = _quote_identifier(column)
                            cursor.execute(
                                (
                                    f"SELECT DISTINCT {quoted_column} "  # nosec B608
                                    f"FROM {quoted_object} "
                                    f"WHERE {quoted_column} IS NOT NULL "
                                    "FETCH FIRST :sample_rows ROWS ONLY"
                                ),
                                {"sample_rows": sample_limit},
                            )
                            values = [_coerce_text(row[0]) for row in cursor]
                            values = [value for value in values if value]
                            if values:
                                object_samples[column] = values
                        except Exception as exc:
                            warnings.append(
                                f"{qualified_name}.{column}: サンプル取得に失敗しました: {exc}"
                            )
                    if object_samples:
                        samples[qualified_name] = object_samples
        except Exception as exc:
            if isinstance(exc, OracleAdapterError):
                raise
            raise OracleAdapterError(f"生成用サンプルの取得に失敗しました: {exc}") from exc
        return samples, warnings

    def execute_select(
        self,
        sql: str,
        max_rows: int | None,
        *,
        admin_control_plane: bool = False,
        binds: Mapping[str, str] | None = None,
    ) -> QueryResults:
        """SELECT を実行する。`admin_control_plane=True` は DB 管理チャット（#2264）の SELECT で、
        利用者に関係なくアプリのユーザーの管理の接続（`connection()`。管理 SQL の更新系と同じ、
        pool を使わない単発の接続）で読む。DeepSec の DATA USER・業務の実行の pool は使わない。
        `binds` はチャットの添付の表（#2517）の行の JSON（CLOB で渡す。SQL は呼ぶ側が WITH 句を
        付けたもの）。
        """
        connection_cm = (
            self.connection() if admin_control_plane else self.user_data_connection(read_only=True)
        )
        try:
            with (
                connection_cm as conn,
                self._business_session_time_zone(conn, admin_control_plane=admin_control_plane),
                conn.cursor() as cursor,
            ):
                if binds:
                    clob = self._load_oracledb().DB_TYPE_CLOB
                    cursor.setinputsizes(**{name: clob for name in binds})
                    cursor.execute(sql, dict(binds))
                else:
                    cursor.execute(sql)
                columns, rows, has_more = self._fetch_query_rows(cursor, max_rows)
            from app.security.request_actor import current_actor_context

            actor = current_actor_context()
            vpd_context_enforced = (
                not admin_control_plane
                and self.settings.oracle_deepsec_enabled
                and bool(actor.user_uuid)
                and not actor.is_system_admin
            )
            return QueryResults(
                columns=columns,
                rows=rows,
                total=len(rows),
                returned_count=len(rows),
                has_more=has_more,
                truncated=has_more,
                execution_context=(
                    "admin_control_plane"
                    if admin_control_plane
                    else "deepsec_data_plane"
                    if vpd_context_enforced
                    else "oracle_data_plane"
                ),
                vpd_context_enforced=vpd_context_enforced,
            )
        except OracleAdapterError:
            raise
        except Exception as exc:
            raise OracleAdapterError(f"SELECT の実行に失敗しました: {exc}") from exc

    def _business_session_time_zone(
        self, connection: Any, *, admin_control_plane: bool
    ) -> AbstractContextManager[None]:
        """業務データの SELECT の間だけ、session の時間帯を共通の設定の時間帯にする（#3265）。

        `CURRENT_DATE` / `CURRENT_TIMESTAMP` が設定の時間帯の今を返す（生成した SQL の
        `SYSDATE` / `SYSTIMESTAMP` は `use_session_current_datetime` で置き換えてある）。抜けると
        `'+00:00'` に戻すため、pool（system_admin 用・非 system_admin 用・DeepSec の DATA USER）
        に返した接続とアプリの表の接続は UTC のまま。DB 管理チャット（`admin_control_plane`）は
        更新系の文と同じ session（UTC）のまま読む（読む文と書く文で「今」をそろえる）。
        """
        if admin_control_plane:
            return nullcontext()
        return local_session_time_zone(connection, session_time_zone_offset(self.settings))

    def _fetch_query_rows(
        self, cursor: Any, max_rows: int | None
    ) -> tuple[list[str], list[dict[str, Any]], bool]:
        """実行した SELECT の cursor から列の名前（重名は `_2` を付ける）と行を読む。

        `max_rows` があればその行数まで（超えたら has_more）、無ければ安全上限で打ち切る。
        """
        column_names = [description[0] for description in cursor.description or []]
        # 既存の一意な名前を予約してから重名列へキーを割り当てる。
        reserved = set(column_names)
        used: set[str] = set()
        columns: list[str] = []
        for name in column_names:
            key = name
            suffix = 2
            if key in used:
                key = f"{name}_{suffix}"
                while key in used or key in reserved:
                    suffix += 1
                    key = f"{name}_{suffix}"
            used.add(key)
            columns.append(key)
        rows: list[dict[str, Any]] = []
        has_more = False
        if max_rows is not None and max_rows > 0:
            fetched_rows = cursor.fetchmany(max_rows + 1)
            has_more = len(fetched_rows) > max_rows
            fetched_rows = fetched_rows[:max_rows]
        else:
            # 件数未指定でも全件をメモリ・job 結果・応答へ載せないよう、安全上限で打ち切る。
            cap = self.settings.nl2sql_max_result_rows
            fetched_rows = []
            while len(fetched_rows) <= cap:
                batch = cursor.fetchmany(SELECT_FETCH_BATCH_SIZE)
                if not batch:
                    break
                fetched_rows.extend(batch)
            has_more = len(fetched_rows) > cap
            fetched_rows = fetched_rows[:cap]
        for row in fetched_rows:
            rows.append(
                {columns[index]: _coerce_result_value(value) for index, value in enumerate(row)}
            )
        return columns, rows, has_more

    def explain_select(self, sql: str) -> ExplainPlanData:
        """Oracle PLAN_TABLE から cost/cardinality と full scan を要約する。"""

        statement_id = f"NL2SQL_{hashlib.sha256(sql.encode()).hexdigest()[:20].upper()}"
        normalized = sql.strip().rstrip(";")
        try:
            with self.connection() as conn, conn.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM PLAN_TABLE WHERE statement_id = :statement_id",
                    {"statement_id": statement_id},
                )
                # EXPLAIN PLAN は DDL のため bind 変数を受け付けない。statement_id は
                # SHA-256 由来の英数字だけに限定し、SQL 本体は sqlglot の read-only
                # gate 通過後だけ渡される。
                cursor.execute(  # nosec B608
                    f"EXPLAIN PLAN SET STATEMENT_ID = '{statement_id}' FOR {normalized}",
                )
                cursor.execute(
                    """
                    SELECT operation, options, object_owner, object_name,
                           cost, cardinality, bytes
                    FROM plan_table
                    WHERE statement_id = :statement_id
                    ORDER BY id
                    """,
                    {"statement_id": statement_id},
                )
                operations = [
                    ExplainPlanOperation(
                        operation=str(row[0] or ""),
                        options=str(row[1] or ""),
                        owner=str(row[2] or ""),
                        object_name=str(row[3] or ""),
                        cost=int(row[4]) if row[4] is not None else None,
                        cardinality=int(row[5]) if row[5] is not None else None,
                        bytes=int(row[6]) if row[6] is not None else None,
                    )
                    for row in cursor
                ]
                cursor.execute(
                    "DELETE FROM PLAN_TABLE WHERE statement_id = :statement_id",
                    {"statement_id": statement_id},
                )
                conn.commit()
        except Exception as exc:
            return ExplainPlanData(
                available=False,
                warning=f"Oracle EXPLAIN PLAN を利用できません: {exc}",
            )
        root = operations[0] if operations else None
        full_scans = sorted(
            {
                operation.object_name
                for operation in operations
                if operation.operation.upper() == "TABLE ACCESS"
                and "FULL" in operation.options.upper()
                and operation.object_name
            }
        )
        return ExplainPlanData(
            available=bool(operations),
            total_cost=root.cost if root else None,
            estimated_cardinality=root.cardinality if root else None,
            full_table_scans=full_scans,
            operations=operations,
            warning="" if operations else "PLAN_TABLE に実行計画が生成されませんでした。",
        )

    def list_db_admin_objects(self, object_type: str) -> list[dict[str, Any]]:
        """List visible tables or views for the DB admin console."""
        normalized_type = "view" if object_type.lower() == "view" else "table"
        if normalized_type == "view":
            owner_filter, owner_binds = self._schema_owner_filter("v.owner")
            sql = """
                SELECT v.view_name, v.owner AS owner_name, NULL, NVL(c.comments, ' ')
                FROM all_views v
                LEFT JOIN all_tab_comments c
                  ON c.owner = v.owner AND c.table_name = v.view_name
                WHERE {visible_filter}
                  AND {owner_filter}
                ORDER BY v.owner, v.view_name
            """
        else:
            owner_filter, owner_binds = self._schema_owner_filter("t.owner")
            sql = """
                SELECT t.table_name, t.owner AS owner_name, t.num_rows, NVL(c.comments, ' ')
                FROM all_tables t
                LEFT JOIN all_tab_comments c
                  ON c.owner = t.owner AND c.table_name = t.table_name
                WHERE {visible_filter}
                  AND {owner_filter}
                ORDER BY t.owner, t.table_name
            """
        with self.connection() as conn, conn.cursor() as cursor:
            visible_filter = _VIEW_VISIBLE_SQL if normalized_type == "view" else _TABLE_VISIBLE_SQL
            cursor.execute(
                sql.format(owner_filter=owner_filter, visible_filter=visible_filter),
                owner_binds,
            )
            rows = cursor.fetchall() if hasattr(cursor, "fetchall") else list(cursor)
        return [
            {
                "name": str(row[0] or ""),
                "owner": str(row[1] or ""),
                "qualified_name": qualified_object_name(str(row[1] or ""), str(row[0] or "")),
                "object_type": normalized_type,
                "row_count": int(row[2]) if row[2] is not None else None,
                "comment": _coerce_text(row[3]) if len(row) > 3 else "",
            }
            for row in rows
            if is_user_visible_schema_object(str(row[1] or ""), str(row[0] or ""))
        ]

    def find_db_admin_object_type(self, object_name: str, owner: str = "") -> str | None:
        """同名オブジェクトの実種別を、エラー表示の補足用に取得する。"""
        identity = self._db_admin_identity(object_name, owner)
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT object_type FROM all_objects
                WHERE owner = :owner AND object_name = :object_name
                ORDER BY CASE object_type WHEN 'TABLE' THEN 0 WHEN 'VIEW' THEN 1 ELSE 2 END
                """,
                {"owner": identity.owner, "object_name": identity.object_name},
            )
            row = cursor.fetchone()
        return str(row[0]).upper() if row and row[0] else None

    def get_db_admin_object_detail(
        self,
        *,
        object_name: str,
        owner: str = "",
        object_type: str,
        include_ddl: bool = True,
        exact_count: bool = False,
    ) -> dict[str, Any]:
        """Return columns and DBMS_METADATA DDL for a table/view.

        include_ddl=False のときは重い DBMS_METADATA.GET_DDL を実行せず ddl="" を返す
        (列一覧の初期表示を高速化。DDL は DDL タブ表示時に別途取得する)。
        exact_count=False のときは全表スキャンの COUNT(*) を実行せず row_count=None を返す
        (件数は呼び出し側が num_rows 統計で補完。正確件数は exact_count=True 時のみ)。
        """
        identity = self._db_admin_identity(object_name, owner)
        safe_name = identity.object_name
        quoted_object = _quote_object_identity(identity)
        normalized_type = "VIEW" if object_type.lower() == "view" else "TABLE"
        columns: list[SchemaColumn] = []
        warnings: list[str] = []
        comment = ""
        row_count: int | None = None
        ddl = ""
        with self.connection() as conn, conn.cursor() as cursor:
            domain_names: dict[str, str] = {}
            try:
                cursor.execute(
                    """
                    SELECT column_name, domain_owner, domain_name
                    FROM all_tab_cols
                    WHERE owner = :owner AND table_name = :object_name
                      AND domain_name IS NOT NULL
                    """,
                    {"owner": identity.owner, "object_name": safe_name},
                )
                for column_name, domain_owner, domain_name in cursor.fetchall():
                    domain_names[str(column_name or "")] = _domain_display_name(
                        str(domain_owner or ""), str(domain_name or "")
                    )
            except Exception:  # nosec B110 - SQL ドメイン非対応のバージョンは DOMAIN_NAME 列が無い(ORA-00904)
                domain_names = {}
            cursor.execute(
                """
                SELECT c.column_name,
                       c.data_type ||
                       CASE
                         WHEN c.data_type IN ('VARCHAR2','CHAR') AND c.char_used = 'C'
                         THEN '(' || c.char_length || ' CHAR)'
                         WHEN c.data_type IN ('NVARCHAR2','NCHAR')
                         THEN '(' || c.char_length || ')'
                         WHEN c.data_type IN ('VARCHAR2','CHAR')
                         THEN '(' || c.data_length || ')'
                         WHEN c.data_type = 'NUMBER' AND c.data_precision IS NOT NULL
                         THEN '(' || c.data_precision ||
                              CASE WHEN c.data_scale > 0 THEN ',' || c.data_scale ELSE '' END || ')'
                         ELSE ''
                       END AS data_type,
                       c.nullable,
                       NVL(cc.comments, ' ')
                FROM all_tab_columns c
                LEFT JOIN all_col_comments cc
                  ON cc.owner = c.owner
                 AND cc.table_name = c.table_name
                 AND cc.column_name = c.column_name
                WHERE c.owner = :owner AND c.table_name = :object_name
                ORDER BY c.column_id
                """,
                {"owner": identity.owner, "object_name": safe_name},
            )
            for column_name, data_type, nullable, column_comment in cursor:
                columns.append(
                    SchemaColumn(
                        column_name=str(column_name or ""),
                        logical_name=_coerce_text(column_comment) or str(column_name or ""),
                        data_type=str(data_type or ""),
                        nullable=str(nullable or "Y").upper() == "Y",
                        comment=_coerce_text(column_comment),
                        domain_name=domain_names.get(str(column_name or ""), ""),
                    )
                )
            cursor.execute(
                """
                SELECT comments FROM all_tab_comments
                WHERE owner = :owner AND table_name = :object_name
                """,
                {"owner": identity.owner, "object_name": safe_name},
            )
            row = cursor.fetchone()
            comment = _coerce_text(row[0]) if row and row[0] else ""
            if normalized_type == "TABLE" and exact_count:
                try:
                    cursor.execute(f"SELECT COUNT(*) FROM {quoted_object}")  # nosec B608
                    row = cursor.fetchone()
                    row_count = int(row[0] or 0) if row else None
                except Exception as exc:
                    warnings.append(f"row count の取得に失敗しました: {exc}")
            comment_object_type = "TABLE"
            if include_ddl:
                ddl_type = normalized_type
                if normalized_type == "VIEW":
                    # MView/実体 TABLE を VIEW として GET_DDL すると ORA-31603 になるため
                    # user_objects で実種別を判定する
                    with suppress(Exception):
                        cursor.execute(
                            """
                            SELECT object_type FROM all_objects
                            WHERE owner = :owner AND object_name = :object_name
                            ORDER BY CASE object_type
                              WHEN 'MATERIALIZED VIEW' THEN 0 WHEN 'VIEW' THEN 1 ELSE 2 END
                            """,
                            {"owner": identity.owner, "object_name": safe_name},
                        )
                        row = cursor.fetchone()
                        actual = str(row[0] or "").upper() if row else ""
                        if actual == "MATERIALIZED VIEW":
                            ddl_type = "MATERIALIZED_VIEW"
                            comment_object_type = "MATERIALIZED VIEW"
                        elif actual == "TABLE":
                            ddl_type = "TABLE"
                try:
                    cursor.execute(
                        (
                            "SELECT DBMS_METADATA.GET_DDL"
                            "(:object_type, :object_name, :owner) FROM DUAL"
                        ),
                        {
                            "object_type": ddl_type,
                            "object_name": safe_name,
                            "owner": identity.owner,
                        },
                    )
                    row = cursor.fetchone()
                    ddl = _coerce_text(row[0]) if row and row[0] else ""
                except Exception as exc:
                    warnings.append(f"DBMS_METADATA.GET_DDL に失敗しました: {exc}")
        if ddl:
            ddl = ddl.rstrip()
            if not ddl.endswith(";"):
                ddl += ";"
            if comment:
                escaped_comment = comment.replace("'", "''")
                ddl += f"\nCOMMENT ON {comment_object_type} {quoted_object} IS '{escaped_comment}';"
            for column in columns:
                if column.comment:
                    escaped_column_comment = column.comment.replace("'", "''")
                    ddl += (
                        f"\nCOMMENT ON COLUMN {quoted_object}."
                        f"{_quote_identifier(column.column_name)} "
                        f"IS '{escaped_column_comment}';"
                    )
        return {
            "name": safe_name,
            "owner": identity.owner,
            "qualified_name": identity.qualified_name,
            "object_type": normalized_type.lower(),
            "row_count": row_count,
            "comment": comment,
            "columns": [column.model_dump(mode="json") for column in columns],
            "ddl": ddl,
            "warnings": warnings,
        }

    def execute_admin_statements(
        self,
        statements: list[str],
        *,
        atomic: bool = True,
        ignored_error_codes: frozenset[str] = frozenset(),
    ) -> list[dict[str, Any]]:
        """Execute non-SELECT admin SQL statements.

        atomic=True は all-or-nothing、atomic=False は SQL Assist 互換の部分成功
        (成功が 1 件でもあれば commit、全滅なら rollback)。
        """
        from app.clients.oracle_statement_executor import oracle_statement_executor

        with self.connection() as conn:
            return oracle_statement_executor.execute(
                conn,
                statements,
                atomic=atomic,
                normalize=self._normalize_admin_statement,
                statement_type=self._admin_statement_type,
                output_reader=self._fetch_dbms_output,
                success_message=self._admin_success_message,
                ignored_error_codes=ignored_error_codes,
            )

    @contextmanager
    def admin_sql_describer(self) -> Iterator[OracleAdminSqlDescriber]:
        """DB 管理チャットの列の検査に使う、管理の接続の解析だけの describer（#2352）。"""
        describer = OracleAdminSqlDescriber(self)
        try:
            yield describer
        finally:
            describer.close()

    def execute_admin_script(
        self, statements: list[str], *, query_indexes: frozenset[int], max_rows: int | None
    ) -> list[dict[str, Any]]:
        """SELECT を含む文を、1 つの管理の接続で上から順に実行する（「すべて実行」。#2333）。

        DB 管理チャットの「すべて実行」が使う。最初のエラーで止め（後ろの文は実行しない）、
        全部成功したときだけ commit、失敗したら rollback する（DDL は Oracle の暗黙の commit で
        戻らない）。`query_indexes`（1 始まり）の文は SELECT として同じ cursor から `max_rows` 行
        まで読み、結果の dict の `select_result`（`QueryResults`）に入れる。
        """
        from app.clients.oracle_statement_executor import oracle_statement_executor

        def read_query(cursor: Any) -> tuple[int, QueryResults]:
            columns, rows, has_more = self._fetch_query_rows(cursor, max_rows)
            return len(rows), QueryResults(
                columns=columns,
                rows=rows,
                total=len(rows),
                returned_count=len(rows),
                has_more=has_more,
                truncated=has_more,
                execution_context="admin_control_plane",
            )

        with self.connection() as conn:
            return oracle_statement_executor.execute(
                conn,
                statements,
                atomic=True,
                normalize=self._normalize_admin_statement,
                statement_type=self._admin_statement_type,
                output_reader=self._fetch_dbms_output,
                success_message=self._admin_success_message,
                stop_on_error=True,
                query_indexes=query_indexes,
                query_reader=read_query,
            )

    def import_tabular_table(
        self,
        *,
        table_name: str,
        columns: list[CsvImportColumn],
        rows: list[dict[str, str | None]],
        mode: str,
    ) -> dict[str, Any]:
        """Import parsed tabular rows into Oracle using create/replace/append/truncate mode."""
        safe_table = _strict_sql_name(table_name)
        quoted_table = _quote_identifier(safe_table)
        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"create", "replace", "append", "truncate"}:
            raise OracleAdapterError(f"未対応 import mode です: {mode}")
        column_defs = ", ".join(
            f"{_quote_identifier(column.column_name)} {column.data_type}" for column in columns
        )
        ddl = f"CREATE TABLE {quoted_table} ({column_defs})"
        bind_names = [f"c{index}" for index, _column in enumerate(columns)]
        insert_sql = (
            f"INSERT INTO {quoted_table} "  # nosec B608
            f"({', '.join(_quote_identifier(column.column_name) for column in columns)}) "
            f"VALUES ({', '.join(':' + name for name in bind_names)})"
        )
        with self.connection() as conn, conn.cursor() as cursor:
            target_types: dict[str, str] = {}
            if normalized_mode in {"append", "truncate"}:
                target_types = self._validate_existing_tabular_import(
                    cursor,
                    table_name=safe_table,
                    columns=columns,
                    rows=rows,
                )
            bind_rows = [
                {
                    bind_names[index]: (
                        self._csv_upload_value(
                            row.get(column.column_name),
                            target_types[column.column_name],
                        )
                        if target_types
                        else self._coerce_csv_value(row.get(column.column_name), column)
                    )
                    for index, column in enumerate(columns)
                }
                for row in rows
            ]
            file_row_numbers = [
                int(getattr(row, "file_row_number", index + 2)) for index, row in enumerate(rows)
            ]
            created_table = False

            def cleanup_created_table() -> None:
                if not created_table:
                    return
                rollback = getattr(conn, "rollback", None)
                if callable(rollback):
                    rollback()
                self._drop_best_effort(cursor, f"DROP TABLE {quoted_table} PURGE", {})

            if normalized_mode in {"replace", "create"}:
                if normalized_mode == "replace":
                    self._drop_best_effort(cursor, f"DROP TABLE {quoted_table} PURGE", {})
                self._execute_plsql_like(cursor, ddl, {})
                created_table = True
            elif normalized_mode == "truncate":
                # TRUNCATE は暗黙 commit されるため、後続 INSERT 失敗時に既存行を戻せない。
                # DELETE と INSERT を同一 transaction に置き、全件成功時だけ commit する。
                self._execute_plsql_like(cursor, f"DELETE FROM {quoted_table}", {})  # nosec B608
            if bind_rows:
                try:
                    cursor.executemany(insert_sql, bind_rows, batcherrors=True)
                    batch_errors = list(cursor.getbatcherrors())
                except TabularImportValidationError:
                    cleanup_created_table()
                    raise
                except Exception as exc:
                    if "ORA-12899" in str(exc):
                        cleanup_created_table()
                        raise self._tabular_import_batch_error(
                            exc,
                            table_name=safe_table,
                            row_offset=None,
                        ) from exc
                    cleanup_created_table()
                    raise OracleAdapterError(
                        "Excel/CSV 取込の Oracle INSERT に失敗しました。"
                    ) from exc
                if batch_errors:
                    first_error = batch_errors[0]
                    row_offset = getattr(first_error, "offset", None)
                    file_row_number = (
                        file_row_numbers[row_offset]
                        if isinstance(row_offset, int) and 0 <= row_offset < len(file_row_numbers)
                        else None
                    )
                    cleanup_created_table()
                    raise self._tabular_import_batch_error(
                        first_error,
                        table_name=safe_table,
                        row_offset=row_offset,
                        row_number=file_row_number,
                    )
            conn.commit()
        return {
            "runtime": "oracle",
            "table_name": safe_table,
            "row_count": len(bind_rows),
            "mode": normalized_mode,
            "ddl": ddl,
            "insert_sql": insert_sql,
        }

    def _validate_existing_tabular_import(
        self,
        cursor: Any,
        *,
        table_name: str,
        columns: list[CsvImportColumn],
        rows: list[dict[str, str | None]],
    ) -> dict[str, str]:
        """既存表の列存在と CHAR/BYTE 上限を mutation 前に Oracle semantics で検証する。"""
        cursor.execute(
            """
            SELECT column_name,
                   data_type,
                   data_length,
                   char_length,
                   char_used
            FROM user_tab_columns
            WHERE table_name = :table_name
            ORDER BY column_id
            """,
            {"table_name": table_name},
        )
        table_columns = {
            str(row[0] or "").upper(): {
                "data_type": str(row[1] or "").upper(),
                "data_length": int(row[2] or 0),
                "char_length": int(row[3] or 0),
                "char_used": str(row[4] or "B").upper(),
            }
            for row in cursor.fetchall()
        }
        if not table_columns:
            raise TabularImportValidationError(
                f"{table_name}: 取込先テーブルが見つからないか、列情報を取得できません。"
                "表名を確認して再試行してください。"
            )
        missing = [
            column.column_name
            for column in columns
            if column.column_name.upper() not in table_columns
        ]
        if missing:
            joined = ", ".join(missing)
            raise TabularImportValidationError(
                f"{table_name}: 取込先に存在しない列があります: {joined}。"
                "CSV/XLSX/XLS のヘッダーまたは表定義を修正して再試行してください。"
            )

        text_columns: list[tuple[CsvImportColumn, dict[str, Any]]] = []
        for column in columns:
            metadata = table_columns[column.column_name.upper()]
            if metadata["data_type"] in {"CHAR", "VARCHAR2", "NCHAR", "NVARCHAR2"}:
                text_columns.append((column, metadata))
        self._validate_tabular_text_lengths(
            cursor,
            table_name=table_name,
            text_columns=text_columns,
            rows=rows,
        )
        return {
            column.column_name: str(table_columns[column.column_name.upper()]["data_type"])
            for column in columns
        }

    def _validate_tabular_text_lengths(
        self,
        cursor: Any,
        *,
        table_name: str,
        text_columns: list[tuple[CsvImportColumn, dict[str, Any]]],
        rows: list[dict[str, str | None]],
    ) -> None:
        """JSON_TABLE で入力を小分けし、DB charset における LENGTH/LENGTHB を測定する。"""
        if not text_columns or not rows:
            return
        json_column_defs: list[str] = ["file_row NUMBER PATH '$.__file_row' ERROR ON ERROR"]
        violation_queries: list[str] = []
        for index, (column, metadata) in enumerate(text_columns):
            alias = f"c{index}"
            char_semantics = metadata["char_used"] == "C" or metadata["data_type"] in {
                "NCHAR",
                "NVARCHAR2",
            }
            # CHAR semantics は CLOB へ投影し、4000 byte を超える多バイト値も
            # 文字数の検証前に失敗させない。BYTE semantics は LENGTHB が CLOB を
            # 扱えない多バイト DB に備え、SQL VARCHAR2 の上限まで安全に切り詰める。
            projection_type = "CLOB" if char_semantics else "VARCHAR2(4000 BYTE) TRUNCATE"
            # CSV identifier は英数字/underscore に正規化済みなので JSON path へ安全に埋め込める。
            json_column_defs.append(
                f"{alias} {projection_type} "
                f"PATH '$.\"{column.column_name}\"' NULL ON EMPTY ERROR ON ERROR"
            )
            length_function = "LENGTH" if char_semantics else "LENGTHB"
            maximum = (
                int(metadata["char_length"]) if char_semantics else int(metadata["data_length"])
            )
            unit = "文字" if char_semantics else "バイト"
            violation_queries.append(
                "SELECT file_row, "  # nosec B608
                f"'{column.column_name}' AS column_name, "
                f"{length_function}({alias}) AS actual_length, "
                f"{maximum} AS maximum_length, "
                f"'{unit}' AS length_unit "
                "FROM input_rows "
                f"WHERE {alias} IS NOT NULL AND {length_function}({alias}) > {maximum}"
            )

        sql = (
            "WITH input_rows AS ("  # nosec B608
            "SELECT * FROM JSON_TABLE("
            ":payload, '$[*]' COLUMNS (" + ", ".join(json_column_defs) + "))) "  # nosec B608
            "SELECT file_row, column_name, actual_length, maximum_length, length_unit "
            "FROM ("
            + " UNION ALL ".join(violation_queries)
            + ") ORDER BY file_row FETCH FIRST 1 ROW ONLY"
        )
        cursor.setinputsizes(payload=self._load_oracledb().DB_TYPE_CLOB)
        chunk_size = 200
        for start in range(0, len(rows), chunk_size):
            payload_rows = [
                {
                    "__file_row": int(getattr(row, "file_row_number", start + offset + 2)),
                    **{
                        column.column_name: row.get(column.column_name)
                        for column, _metadata in text_columns
                    },
                }
                for offset, row in enumerate(rows[start : start + chunk_size])
            ]
            cursor.execute(sql, {"payload": json.dumps(payload_rows, ensure_ascii=False)})
            violation = cursor.fetchone()
            if violation is None:
                continue
            file_row, column_name, actual, maximum, unit = violation
            raise TabularImportValidationError(
                f"{table_name}.{column_name}: ファイル{int(file_row)}行目は"
                f"{int(actual)}{unit}で、取込先列の上限{int(maximum)}{unit}を超えています。"
                "値を短くするか列定義を拡張して再試行してください。"
            )

    def _tabular_import_batch_error(
        self,
        exc: Exception,
        *,
        table_name: str,
        row_offset: int | None,
        row_number: int | None = None,
    ) -> OracleAdapterError:
        """batch DML error を安全な import エラーへ正規化する。"""
        message = str(exc)
        file_row = (
            row_number
            if row_number is not None
            else (row_offset + 2 if isinstance(row_offset, int) else None)
        )
        if "ORA-12899" not in message:
            row_detail = f"ファイル{file_row}行目の" if file_row is not None else ""
            return OracleAdapterError(
                f"{row_detail}Excel/CSV データを Oracle に投入できませんでした。"
                "値の形式と取込先の制約を確認して再試行してください。"
            )
        match = re.search(
            r'column\s+"[^"]+"\."[^"]+"\."([^"]+)".*?' r"actual:\s*(\d+),\s*maximum:\s*(\d+)",
            message,
            re.IGNORECASE | re.DOTALL,
        )
        column_name = match.group(1) if match else "文字列"
        actual = match.group(2) if match else "許容値超過"
        maximum = match.group(3) if match else "列定義"
        row_detail = f"ファイル{file_row}行目の" if file_row is not None else ""
        return TabularImportValidationError(
            f"{table_name}.{column_name}: {row_detail}値の長さ({actual})が"
            f"取込先列の上限({maximum})を超えています。"
            "値を短くするか列定義を拡張して再試行してください。"
        )

    def upload_csv_to_existing_table(
        self,
        *,
        table_name: str,
        owner: str = "",
        columns: list[CsvImportColumn],
        rows: list[dict[str, str | None]],
        truncate: bool,
    ) -> dict[str, Any]:
        """既存テーブルへ CSV 行を投入する(SQL Assist upload_csv_data の再マップ)。

        CSV 列名とテーブル列名を大文字比較でマッチングし、行ごとに INSERT して
        エラー先頭 5 件を収集する。追記は成功 1 件以上で commit、全置換は全件成功時だけ
        DELETE と INSERT を commit する。
        """
        identity = self._db_admin_identity(table_name, owner)
        safe_table = identity.object_name
        quoted_table = _quote_object_identity(identity)
        with self.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name, data_type
                FROM all_tab_columns
                WHERE owner = :owner AND table_name = :table_name
                ORDER BY column_id
                """,
                {"owner": identity.owner, "table_name": safe_table},
            )
            table_columns = [(str(row[0] or ""), str(row[1] or "")) for row in cursor.fetchall()]
            if not table_columns:
                raise OracleAdapterError(
                    f"{identity.qualified_name}: テーブルが見つからないか列がありません。"
                )
            csv_by_upper: dict[str, CsvImportColumn] = {}
            for column in columns:
                for key in (column.source_name.strip().upper(), column.column_name.upper()):
                    if key and key not in csv_by_upper:
                        csv_by_upper[key] = column
            matched: list[tuple[str, str, CsvImportColumn]] = []
            for name, data_type in table_columns:
                csv_column = csv_by_upper.get(name.upper())
                if csv_column is not None:
                    matched.append((name, data_type, csv_column))
            if not matched:
                raise OracleAdapterError(
                    "CSV の列名がテーブルの列名と一致しません。ヘッダ行を確認してください。"
                )
            matched_csv_names = {column.column_name for _name, _type, column in matched}
            unmatched_csv = [
                column.source_name
                for column in columns
                if column.column_name not in matched_csv_names
            ]
            if truncate:
                self._execute_plsql_like(cursor, f"DELETE FROM {quoted_table}", {})  # nosec B608
            bind_names = [f"c{index}" for index in range(len(matched))]
            insert_sql = (
                f"INSERT INTO {quoted_table} "  # nosec B608
                f"({', '.join(_quote_identifier(name) for name, _type, _column in matched)}) "
                f"VALUES ({', '.join(':' + bind for bind in bind_names)})"
            )
            success_count = 0
            row_errors: list[str] = []
            date_error = False
            for row_index, row in enumerate(rows, start=1):
                binds = {
                    bind_names[bind_index]: self._csv_upload_value(
                        row.get(csv_column.column_name), data_type
                    )
                    for bind_index, (_name, data_type, csv_column) in enumerate(matched)
                }
                try:
                    cursor.execute(insert_sql, binds)
                    success_count += 1
                except Exception as exc:
                    message = str(exc)
                    if "ORA-01861" in message or "ORA-01843" in message:
                        date_error = True
                    if len(row_errors) < 5:
                        file_row_number = int(getattr(row, "file_row_number", row_index))
                        row_errors.append(f"行{file_row_number}: {message}")
            if truncate:
                if success_count == len(rows) and rows:
                    conn.commit()
                else:
                    conn.rollback()
                    if row_errors:
                        row_errors.insert(
                            0,
                            "全置換モードは INSERT エラーのため rollback しました。"
                            "既存データは保持されています。",
                        )
                    success_count = 0
            elif success_count > 0:
                conn.commit()
            else:
                conn.rollback()
        hint = (
            "日付列のフォーマットを解釈できませんでした。"
            "YYYY-MM-DD(例: 2026-01-31)形式を推奨します。"
            if date_error
            else ""
        )
        return {
            "runtime": "oracle",
            "table_name": identity.qualified_name,
            "matched_columns": [name for name, _type, _column in matched],
            "unmatched_csv_columns": unmatched_csv,
            "row_count": len(rows),
            "success_count": success_count,
            "error_count": len(rows) - success_count,
            "row_errors": row_errors,
            "hint": hint,
            "insert_sql": insert_sql,
        }

    def _csv_upload_value(self, value: str | None, data_type: str) -> Any:
        if value is None or str(value).strip() == "":
            return None
        upper_type = data_type.upper()
        if upper_type == "DATE" or upper_type.startswith("TIMESTAMP"):
            converted = _flexible_date_value(str(value))
            # 変換不能な値はそのまま渡し、Oracle 側の行エラー(ORA-01861 等)として報告する
            return converted if converted is not None else str(value)
        if upper_type == "NUMBER":
            text = str(value).strip()
            try:
                return int(text)
            except ValueError:
                try:
                    return float(text)
                except ValueError:
                    return text
        return value

    def apply_comment_statements(self, statements: list[str]) -> dict[str, Any]:
        """Execute generated COMMENT ON statements.

        Callers must generate the statements from validated catalog metadata; this method
        intentionally does not accept arbitrary DDL from API clients.
        """
        with self.connection() as conn, conn.cursor() as cursor:
            for statement in statements:
                self._execute_plsql_like(cursor, statement.strip().rstrip(";"), {})
            conn.commit()
        return {
            "runtime": "oracle",
            "statement_count": len(statements),
        }

    def apply_safe_statements(self, statements: list[str]) -> dict[str, Any]:
        """Execute generated catalog-validated metadata statements."""
        with self.connection() as conn, conn.cursor() as cursor:
            for statement in statements:
                self._execute_plsql_like(cursor, statement.strip().rstrip(";"), {})
            conn.commit()
        return {
            "runtime": "oracle",
            "statement_count": len(statements),
        }

    def list_select_ai_profiles(self) -> list[dict[str, Any]]:
        """List DBMS_CLOUD_AI profiles from the Oracle data dictionary when available."""
        candidates = [
            """
            SELECT PROFILE_NAME, NVL(STATUS, 'UNKNOWN'), OWNER, CREATED
            FROM USER_CLOUD_AI_PROFILES
            ORDER BY PROFILE_NAME
            """,
            """
            SELECT PROFILE_NAME, 'UNKNOWN', USER, NULL
            FROM USER_CLOUD_AI_PROFILES
            ORDER BY PROFILE_NAME
            """,
        ]
        errors: list[str] = []
        with self.connection() as conn, conn.cursor() as cursor:
            for sql in candidates:
                try:
                    cursor.execute(sql)
                    rows = cursor.fetchall() if hasattr(cursor, "fetchall") else list(cursor)
                    return [
                        {
                            "name": str(row[0] or ""),
                            "status": str(row[1] or "unknown").lower(),
                            "owner": str(row[2] or ""),
                            "created_at": _coerce_text(row[3]) if len(row) > 3 else "",
                        }
                        for row in rows
                    ]
                except Exception as exc:
                    errors.append(str(exc))
                    continue
        raise OracleAdapterError(
            "Oracle Select AI profile 一覧を取得できませんでした: " + "; ".join(errors)
        )

    def fetch_select_ai_profile_names(
        self,
        profile_names: set[str] | None = None,
    ) -> set[str]:
        """USER_CLOUD_AI_PROFILES から profile 名だけを軽量取得する。"""

        names = {name.strip().upper() for name in (profile_names or set()) if name.strip()}
        binds: dict[str, Any] = {}
        target_filter = ""
        if names:
            placeholders: list[str] = []
            for index, name in enumerate(sorted(names)):
                bind_name = f"profile_name_{index}"
                placeholders.append(f":{bind_name}")
                binds[bind_name] = name
            target_filter = f"WHERE UPPER(PROFILE_NAME) IN ({', '.join(placeholders)})"
        sql = f"SELECT PROFILE_NAME FROM USER_CLOUD_AI_PROFILES {target_filter}"  # nosec B608
        with self.connection() as conn, conn.cursor() as cursor:
            try:
                cursor.execute(sql, binds)
                rows = cursor.fetchall() if hasattr(cursor, "fetchall") else list(cursor)
            except Exception as exc:
                raise OracleAdapterError(
                    f"Oracle Select AI profile 名一覧を取得できませんでした: {exc}"
                ) from exc
        return {str(row[0] or "").strip().upper() for row in rows if str(row[0] or "").strip()}

    def get_select_ai_profile_detail(self, *, profile_name: str) -> dict[str, Any]:
        """Fetch one DBMS_CLOUD_AI profile detail with best-effort attribute decoding.

        Autonomous DB の ``USER_CLOUD_AI_PROFILES`` は ``ATTRIBUTES``/``OWNER``/``CREATED``
        列を持たない環境がある(それらを select すると ORA-00904)。存在が保証される
        ``PROFILE_NAME``/``STATUS`` のみを主ビューから読み、属性・object_list は 1 属性 = 1 行の
        ``USER_CLOUD_AI_PROFILE_ATTRIBUTES`` から best-effort で組み立てる。
        """
        safe_name = profile_name.strip()
        if not safe_name:
            raise OracleAdapterError("profile_name が空です。")
        candidates = [
            """
            SELECT PROFILE_NAME, NVL(STATUS, 'UNKNOWN')
            FROM USER_CLOUD_AI_PROFILES
            WHERE UPPER(PROFILE_NAME) = UPPER(:profile_name)
            """,
            """
            SELECT PROFILE_NAME, 'UNKNOWN'
            FROM USER_CLOUD_AI_PROFILES
            WHERE UPPER(PROFILE_NAME) = UPPER(:profile_name)
            """,
        ]
        errors: list[str] = []
        with self.connection() as conn, conn.cursor() as cursor:
            row: Any = None
            for sql in candidates:
                try:
                    cursor.execute(sql, {"profile_name": safe_name})
                    row = cursor.fetchone()
                    break
                except Exception as exc:  # noqa: BLE001 - 環境差の列欠落を吸収
                    errors.append(str(exc))
                    continue
            else:
                raise OracleAdapterError(
                    "Oracle Select AI profile 詳細を取得できませんでした: " + "; ".join(errors)
                )
            if not row:
                raise OracleAdapterError(f"{profile_name}: profile が見つかりません。")

            attributes = self._fetch_cloud_ai_profile_attributes(cursor, safe_name)
            object_list = _normalize_select_ai_object_list(attributes)
            owner = ""
            if object_list:
                owner = str(object_list[0].get("owner") or "")
            if not owner:
                owner = self.settings.oracle_user.strip().upper()
            return {
                "name": str(row[0] or safe_name),
                "status": str(row[1] or "unknown").lower(),
                "owner": owner,
                "created_at": str(attributes.get("created") or ""),
                "attributes": attributes,
                "description": str(attributes.get("description") or ""),
                "object_list": object_list,
            }

    @staticmethod
    def _fetch_cloud_ai_profile_attributes(cursor: Any, profile_name: str) -> dict[str, Any]:
        """``USER_CLOUD_AI_PROFILE_ATTRIBUTES`` を 1 属性 = 1 行で読み dict へ復元する。

        値は LOB のことがあるため ``_coerce_text`` で文字列化し、JSON として解釈できれば
        構造化して格納する(``object_list`` など)。参照失敗時は空 dict で縮退する。
        """
        try:
            cursor.execute(
                """
                SELECT ATTRIBUTE_NAME, ATTRIBUTE_VALUE
                FROM USER_CLOUD_AI_PROFILE_ATTRIBUTES
                WHERE UPPER(PROFILE_NAME) = UPPER(:profile_name)
                """,
                {"profile_name": profile_name},
            )
            rows = cursor.fetchall() if hasattr(cursor, "fetchall") else list(cursor)
        except Exception:  # noqa: BLE001 - ビュー未提供環境では attributes 無しで縮退
            return {}
        attributes: dict[str, Any] = {}
        for attr_row in rows:
            name = str(attr_row[0] or "").strip()
            if not name:
                continue
            text = _coerce_text(attr_row[1]) if len(attr_row) > 1 else ""
            try:
                attributes[name] = json.loads(text) if text else text
            except json.JSONDecodeError:
                attributes[name] = text
        return attributes

    def list_select_ai_feedback_entries(
        self, *, profile_name: str, limit: int = 25, offset: int = 0
    ) -> dict[str, Any]:
        """List one offset page of a DBMS_CLOUD_AI profile feedback vector table.

        表（`<PROFILE>_FEEDBACK_VECINDEX$VECTAB`）は DBMS_CLOUD_AI が作る普通の表で、列は
        CONTENT（CLOB）・ATTRIBUTES（JSON）・EMBEDDING（VECTOR）だけ（主キー・登録日時が無い）。
        件数は COUNT(*)、ページは ROWID の順の OFFSET / FETCH で取る（ページをまたいで
        同じ行を 2 度出さない安定した順。#1891）。
        """
        safe_profile, index_name, table_name = _select_ai_feedback_index_names(profile_name)
        quoted_table = _quote_identifier(table_name)
        query = (
            "SELECT CONTENT, "
            "JSON_VALUE(ATTRIBUTES, '$.sql_id' RETURNING VARCHAR2(128)) AS SQL_ID, "
            "JSON_VALUE(ATTRIBUTES, '$.sql_text' RETURNING CLOB) AS SQL_TEXT, "
            f"ATTRIBUTES FROM {quoted_table} "  # nosec B608
            "ORDER BY ROWID " + offset_fetch_clause()
        )
        count_query = f"SELECT COUNT(*) FROM {quoted_table}"  # nosec B608
        page_limit = max(1, int(limit))
        page_offset = max(0, int(offset))
        with self.connection() as conn, conn.cursor() as cursor:
            try:
                cursor.execute(count_query)
                count_row = cursor.fetchone()
                total = int(count_row[0] or 0) if count_row else 0
                cursor.execute(query, offset_fetch_binds(offset=page_offset, limit=page_limit))
                rows = cursor.fetchall() if hasattr(cursor, "fetchall") else list(cursor)
                # python-oracledb の CLOB は接続に紐づく locator なので、接続を閉じる前に
                # 文字列へ materialize する。接続 context 外で read() すると
                # DPY-1001 / DPI-1010 になる。
                materialized_rows = [
                    tuple(_coerce_result_value(value) for value in row) for row in rows
                ]
            except Exception as exc:
                message = str(exc)
                if "ORA-00942" in message or "ORA-04043" in message:
                    raise OracleAdapterError(
                        "Select AI feedback vector table が未作成です。"
                        "feedback vector index を再構築してください。"
                    ) from exc
                raise OracleAdapterError(
                    f"Select AI feedback entries の取得に失敗しました: {message}"
                ) from exc
        items: list[dict[str, Any]] = []
        for row in materialized_rows:
            attributes_text = _coerce_text(row[3] if len(row) > 3 else "")
            try:
                attributes = json.loads(attributes_text) if attributes_text.strip() else {}
            except json.JSONDecodeError:
                attributes = {"raw": attributes_text}
            items.append(
                {
                    "content": _coerce_text(row[0] if len(row) > 0 else ""),
                    "sql_id": _coerce_text(row[1] if len(row) > 1 else ""),
                    "sql_text": _coerce_text(row[2] if len(row) > 2 else ""),
                    "attributes": (
                        attributes if isinstance(attributes, dict) else {"raw": attributes}
                    ),
                    "raw_attributes": attributes_text,
                }
            )
        return {
            "runtime": "oracle",
            "profile_name": safe_profile,
            "index_name": index_name,
            "table_name": table_name,
            "items": items,
            # 件数とページの取得の間に行が増えても、返したページの終わりより少なくしない。
            "total": max(total, page_offset + len(items)) if items else total,
            "limit": page_limit,
            "offset": page_offset,
        }

    def delete_select_ai_feedback(self, *, profile_name: str, sql_text: str) -> dict[str, Any]:
        """Delete one DBMS_CLOUD_AI feedback entry by SQL text."""
        safe_profile, index_name, table_name = _select_ai_feedback_index_names(profile_name)
        with self.connection() as conn, conn.cursor() as cursor:
            try:
                cursor.execute(
                    """
                    BEGIN
                        DBMS_CLOUD_AI.FEEDBACK(
                            profile_name => :profile_name,
                            sql_text => :sql_text,
                            operation => 'DELETE'
                        );
                    END;
                    """,
                    {"profile_name": safe_profile, "sql_text": sql_text},
                )
                conn.commit()
            except Exception as exc:
                if _looks_like_select_ai_feedback_not_found(str(exc)):
                    raise SelectAiFeedbackNotFoundError(
                        "削除する Select AI feedback が見つかりません（すでに削除されています）。"
                        f"一覧を再読み込みしてください: {exc}"
                    ) from exc
                raise OracleAdapterError(f"Select AI feedback の削除に失敗しました: {exc}") from exc
        return {
            "runtime": "oracle",
            "package": "DBMS_CLOUD_AI",
            "profile_name": safe_profile,
            "index_name": index_name,
            "table_name": table_name,
        }

    def add_select_ai_feedback(
        self,
        *,
        profile_name: str,
        sql_text: str,
        feedback_type: str,
        response: str,
        feedback_content: str,
    ) -> dict[str, Any]:
        """Add one DBMS_CLOUD_AI feedback entry.

        ``sql_text``（``select ai showsql <質問>``）と ``response``（正しい SQL）を渡す。この形は、
        その文を ``select ai`` で実行していなくても登録できる（公式の文書の「Provide Feedback
        Without Prior Usage」。#1681 で実 ADB でも確かめた）。``positive`` や ``response`` の無い
        feedback は、実行の記録（V$MAPPED_SQL）が無いと ORA-20000 になるため、service は
        ``NEGATIVE`` と ``response`` で登録する。
        """
        safe_profile, index_name, table_name = _select_ai_feedback_index_names(profile_name)
        with self.connection() as conn, conn.cursor() as cursor:
            try:
                cursor.execute(
                    """
                    BEGIN
                        DBMS_CLOUD_AI.FEEDBACK(
                            profile_name => :profile_name,
                            sql_text => :sql_text,
                            feedback_type => :feedback_type,
                            response => :response,
                            feedback_content => :feedback_content,
                            operation => 'ADD'
                        );
                    END;
                    """,
                    {
                        "profile_name": safe_profile,
                        "sql_text": sql_text,
                        "feedback_type": feedback_type,
                        "response": response,
                        "feedback_content": feedback_content,
                    },
                )
                conn.commit()
            except Exception as exc:
                if _looks_like_select_ai_feedback_sql_unmatched(str(exc)):
                    raise OracleAdapterError(
                        "Select AI feedback を追加できませんでした。"
                        "response（正しい SQL）の無い feedback は、同じ `select ai` の文を実行した"
                        "記録（V$MAPPED_SQL）が必要です。正しい SQL を入れてください: "
                        f"{exc}"
                    ) from exc
                raise OracleAdapterError(f"Select AI feedback の追加に失敗しました: {exc}") from exc
        return {
            "runtime": "oracle",
            "package": "DBMS_CLOUD_AI",
            "profile_name": safe_profile,
            "index_name": index_name,
            "table_name": table_name,
            "sql_text": sql_text,
            "feedback_type": feedback_type,
        }

    def update_select_ai_feedback_vector_index(
        self, *, profile_name: str, similarity_threshold: float, match_limit: int
    ) -> dict[str, Any]:
        """Update DBMS_CLOUD_AI feedback vector index attributes."""
        safe_profile, index_name, table_name = _select_ai_feedback_index_names(profile_name)
        attributes = json.dumps(
            {
                "similarity_threshold": float(similarity_threshold),
                "match_limit": int(match_limit),
            },
            ensure_ascii=False,
        )
        with self.connection() as conn, conn.cursor() as cursor:
            try:
                cursor.execute(
                    """
                    BEGIN
                        DBMS_CLOUD_AI.UPDATE_VECTOR_INDEX(
                            index_name => :index_name,
                            attributes => :attributes
                        );
                    END;
                    """,
                    {"index_name": index_name, "attributes": attributes},
                )
                conn.commit()
            except Exception as exc:
                raise OracleAdapterError(
                    f"Select AI feedback vector index の更新に失敗しました: {exc}"
                ) from exc
        return {
            "runtime": "oracle",
            "package": "DBMS_CLOUD_AI",
            "profile_name": safe_profile,
            "index_name": index_name,
            "table_name": table_name,
            "attributes": attributes,
        }

    def upsert_select_ai_profile_low_level(
        self,
        *,
        profile_name: str,
        attributes: dict[str, Any],
        description: str = "",
        original_name: str = "",
    ) -> dict[str, Any]:
        """Create or replace DBMS_CLOUD_AI profile from raw attributes JSON."""
        safe_name = profile_name.strip()
        if not safe_name:
            raise OracleAdapterError("profile_name が空です。")
        attrs = json.dumps(attributes or {}, ensure_ascii=False)
        desc = description or ""
        with self.connection() as conn, conn.cursor() as cursor:
            credential_name = str((attributes or {}).get("credential_name") or "").strip()
            if credential_name:
                schema_name = self._current_schema(cursor)
                if not self._select_ai_credential_exists_with_cursor(cursor, credential_name):
                    raise SelectAiCredentialMissingError(credential_name, schema_name)
            if original_name.strip() and original_name.strip().upper() != safe_name.upper():
                self._drop_cloud_ai_profile_best_effort(cursor, original_name.strip())
            self._drop_cloud_ai_profile_best_effort(cursor, safe_name)
            self._execute_first_supported_plsql(
                cursor,
                [
                    (
                        """
                        BEGIN
                            DBMS_CLOUD_AI.CREATE_PROFILE(
                                profile_name => :name,
                                attributes => :attrs,
                                description => :description
                            );
                        END;
                        """,
                        {"name": safe_name, "attrs": attrs, "description": desc},
                    ),
                    (
                        """
                        BEGIN
                            DBMS_CLOUD_AI.CREATE_PROFILE(
                                profile_name => :name,
                                attributes => :attrs
                            );
                        END;
                        """,
                        {"name": safe_name, "attrs": attrs},
                    ),
                ],
            )
            conn.commit()
        return {
            "runtime": "oracle",
            "package": "DBMS_CLOUD_AI",
            "profile_name": safe_name,
            "attributes": attributes,
            "description": desc,
        }

    def get_select_ai_credential_status(self, credential_name: str) -> tuple[str, bool]:
        """現接続 schema の Credential 所有状態を副作用なしで返す。"""
        safe_name = credential_name.strip().upper()
        if not safe_name:
            raise OracleAdapterError("credential_name が空です。")
        with self.connection() as conn, conn.cursor() as cursor:
            schema_name = self._current_schema(cursor)
            exists = self._select_ai_credential_exists_with_cursor(cursor, safe_name)
        return schema_name, exists

    def create_select_ai_credential(
        self,
        *,
        credential_name: str,
        user_ocid: str,
        tenancy_ocid: str,
        fingerprint: str,
        private_key: str,
        recreate: bool,
    ) -> str:
        """OCI signing key Credential を bind variable のみで作成または再作成する。"""
        safe_name = credential_name.strip().upper()
        if not safe_name:
            raise OracleAdapterError("credential_name が空です。")
        with self.connection() as conn, conn.cursor() as cursor:
            exists = self._select_ai_credential_exists_with_cursor(cursor, safe_name)
            if exists and not recreate:
                raise SelectAiCredentialExistsError(
                    f'Select AI Credential "{safe_name}" は既に存在します。'
                )
            if exists:
                self._execute_plsql(
                    cursor,
                    """
                    BEGIN
                        DBMS_CLOUD.DROP_CREDENTIAL(credential_name => :credential_name);
                    END;
                    """,
                    {"credential_name": safe_name},
                )
            self._execute_plsql(
                cursor,
                """
                BEGIN
                    DBMS_CLOUD.CREATE_CREDENTIAL(
                        credential_name => :credential_name,
                        user_ocid => :user_ocid,
                        tenancy_ocid => :tenancy_ocid,
                        private_key => :private_key,
                        fingerprint => :fingerprint
                    );
                END;
                """,
                {
                    "credential_name": safe_name,
                    "user_ocid": user_ocid,
                    "tenancy_ocid": tenancy_ocid,
                    "private_key": private_key,
                    "fingerprint": fingerprint,
                },
            )
            conn.commit()
        return "recreated" if exists else "created"

    @staticmethod
    def _current_schema(cursor: Any) -> str:
        cursor.execute("SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM DUAL")
        row = cursor.fetchone()
        return str(row[0] or "").strip().upper() if row else ""

    @staticmethod
    def _select_ai_credential_exists_with_cursor(cursor: Any, credential_name: str) -> bool:
        cursor.execute(
            "SELECT COUNT(*) FROM USER_CREDENTIALS WHERE CREDENTIAL_NAME = UPPER(:name)",
            {"name": credential_name},
        )
        row = cursor.fetchone()
        return bool(row and int(row[0] or 0) > 0)

    def list_agent_conversations(
        self, *, team_name: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Fetch recent Select AI Agent conversation prompts when dictionary view exists.

        USER_CLOUD_AI_CONVERSATION_PROMPTS の応答の列は PROMPT_RESPONSE で、team の名前は無い。
        Select AI Agent の prompt は PROMPT_ACTION = 'AGENT' で、team は
        USER_AI_AGENT_TEAM_HISTORY の CONVERSATION_ID から引く（#1681 で実 ADB で確かめた）。
        """
        filters = []
        binds: dict[str, Any] = {"limit": max(limit, 1)}
        if team_name:
            filters.append(
                "EXISTS (SELECT 1 FROM USER_AI_AGENT_TEAM_HISTORY h "
                "WHERE h.CONVERSATION_ID = p.CONVERSATION_ID "
                "AND UPPER(h.TEAM_NAME) = UPPER(:team_name))"
            )
            binds["team_name"] = team_name
        agent_where = " AND ".join(["p.PROMPT_ACTION = 'AGENT'", *filters])
        conversation_sql = (
            "SELECT p.CONVERSATION_ID, p.PROMPT, p.PROMPT_RESPONSE, p.CREATED, "  # nosec B608
            "(SELECT MAX(h.TEAM_NAME) FROM USER_AI_AGENT_TEAM_HISTORY h "
            "WHERE h.CONVERSATION_ID = p.CONVERSATION_ID) AS TEAM_NAME "
            "FROM USER_CLOUD_AI_CONVERSATION_PROMPTS p "
            f"WHERE {agent_where} "
            "ORDER BY p.CREATED DESC "
            "FETCH FIRST :limit ROWS ONLY"
        )
        # team の履歴の view を読めない ADB 向けに、prompt だけを返す（team で絞るときは使わない）。
        prompt_sql = (
            "SELECT p.CONVERSATION_ID, p.PROMPT, p.PROMPT_RESPONSE, p.CREATED, NULL "  # nosec B608
            "FROM USER_CLOUD_AI_CONVERSATION_PROMPTS p "
            "WHERE p.PROMPT_ACTION = 'AGENT' "
            "ORDER BY p.CREATED DESC "
            "FETCH FIRST :limit ROWS ONLY"
        )
        candidates = [(conversation_sql, binds)]
        if not team_name:
            candidates.append((prompt_sql, binds))
        errors: list[str] = []
        with self.connection() as conn, conn.cursor() as cursor:
            for sql, params in candidates:
                try:
                    cursor.execute(sql, params)
                    rows = cursor.fetchall() if hasattr(cursor, "fetchall") else list(cursor)
                    return [
                        {
                            "conversation_id": str(row[0] or ""),
                            "prompt": _coerce_text(row[1]),
                            "response": _coerce_text(row[2]) if len(row) > 2 else "",
                            "created_at": _coerce_text(row[3]) if len(row) > 3 else "",
                            "team_name": str(row[4] or "") if len(row) > 4 else "",
                        }
                        for row in rows
                    ]
                except Exception as exc:
                    errors.append(str(exc))
                    continue
        raise OracleAdapterError(
            "Select AI Agent conversation 履歴を取得できませんでした: " + "; ".join(errors)
        )

    def check_select_ai_agent_privileges(self) -> list[dict[str, str]]:
        """Run side-effect-free Select AI Agent privilege checks."""

        def check_count(
            cursor: Any,
            *,
            name: str,
            sql: str,
            params: dict[str, Any] | None = None,
            ok_message: str,
            warning_message: str,
        ) -> dict[str, str]:
            try:
                cursor.execute(sql, params or {})
                row = cursor.fetchone()
                count = int(row[0] or 0) if row else 0
                return {
                    "name": name,
                    "status": "ok" if count > 0 else "warning",
                    "message": ok_message if count > 0 else warning_message,
                }
            except Exception as exc:
                return {
                    "name": name,
                    "status": "warning",
                    "message": f"{warning_message}: {exc}",
                }

        def check_access(
            cursor: Any,
            *,
            name: str,
            sql: str,
            ok_message: str,
            warning_message: str,
        ) -> dict[str, str]:
            try:
                cursor.execute(sql)
                cursor.fetchone()
                return {"name": name, "status": "ok", "message": ok_message}
            except Exception as exc:
                return {
                    "name": name,
                    "status": "warning",
                    "message": f"{warning_message}: {exc}",
                }

        with self.connection() as conn, conn.cursor() as cursor:
            checks: list[dict[str, str]] = []
            try:
                cursor.execute("SELECT 1 FROM DUAL")
                checks.append(
                    {
                        "name": "oracle_connection",
                        "status": "ok",
                        "message": "Oracle へ接続できます。",
                    }
                )
            except Exception as exc:
                raise OracleAdapterError(f"Oracle 接続確認に失敗しました: {exc}") from exc
            checks.append(
                check_count(
                    cursor,
                    name="dbms_cloud_ai_package",
                    sql="""
                    SELECT COUNT(*)
                    FROM ALL_PROCEDURES
                    WHERE OBJECT_NAME = 'DBMS_CLOUD_AI'
                    """,
                    ok_message="DBMS_CLOUD_AI package が参照可能です。",
                    warning_message="DBMS_CLOUD_AI package を参照できません。",
                )
            )
            checks.append(
                check_count(
                    cursor,
                    name="dbms_cloud_ai_agent_package",
                    sql="""
                    SELECT COUNT(*)
                    FROM ALL_PROCEDURES
                    WHERE OBJECT_NAME = 'DBMS_CLOUD_AI_AGENT'
                    """,
                    ok_message="DBMS_CLOUD_AI_AGENT package が参照可能です。",
                    warning_message="DBMS_CLOUD_AI_AGENT package を参照できません。",
                )
            )
            checks.append(
                check_count(
                    cursor,
                    name="dbms_cloud_ai_create_conversation",
                    sql="""
                    SELECT COUNT(*)
                    FROM ALL_PROCEDURES
                    WHERE OBJECT_NAME = 'DBMS_CLOUD_AI'
                      AND PROCEDURE_NAME = 'CREATE_CONVERSATION'
                    """,
                    ok_message=(
                        "DBMS_CLOUD_AI.CREATE_CONVERSATION（Select AI Agent の会話）を使えます。"
                    ),
                    warning_message=(
                        "DBMS_CLOUD_AI.CREATE_CONVERSATION を参照できません。"
                        "Select AI Agent の会話を作れません"
                    ),
                )
            )
            checks.append(
                check_access(
                    cursor,
                    name="user_cloud_ai_profiles",
                    sql="SELECT PROFILE_NAME FROM USER_CLOUD_AI_PROFILES WHERE 1 = 0",
                    ok_message="USER_CLOUD_AI_PROFILES を参照できます。",
                    warning_message="USER_CLOUD_AI_PROFILES を参照できません。",
                )
            )
            checks.append(
                check_access(
                    cursor,
                    name="user_cloud_ai_conversation_prompts",
                    sql=(
                        "SELECT CONVERSATION_ID FROM USER_CLOUD_AI_CONVERSATION_PROMPTS WHERE 1 = 0"
                    ),
                    ok_message="USER_CLOUD_AI_CONVERSATION_PROMPTS を参照できます。",
                    warning_message="USER_CLOUD_AI_CONVERSATION_PROMPTS を参照できません。",
                )
            )
            return checks

    def generate_synthetic_data(
        self,
        *,
        table_name: str,
        row_count: int,
        profile_name: str = "",
        object_list: list[str] | None = None,
        user_prompt: str = "",
        sample_rows: int = 0,
        use_comments: bool = True,
        on_connection: Callable[[Any], None] | None = None,
        staging: bool = False,
    ) -> dict[str, Any]:
        """Call DBMS_CLOUD_AI.GENERATE_SYNTHETIC_DATA for a validated table."""
        normalized_profile_name = profile_name.strip()
        if not normalized_profile_name:
            raise OracleAdapterError(
                "DBMS_CLOUD_AI.GENERATE_SYNTHETIC_DATA の実行には profile_name が必要です。"
            )

        def identity(name: str) -> OracleObjectIdentity:
            if not staging:
                return self._db_admin_identity(name)
            value = parse_object_identity(name, default_owner=self.settings.oracle_user)
            if value.owner != self.settings.oracle_user.upper() or not re.fullmatch(
                r"NL2SQL_SP_[A-F0-9]{32}_[0-9]{1,3}", value.object_name
            ):
                raise OracleAdapterError("合成データの一時表名が不正です。")
            return value

        table_identity = identity(table_name) if table_name.strip() else None
        object_identities = [identity(item) for item in object_list or [] if item.strip()]
        if table_identity is None and not object_identities:
            raise OracleAdapterError("synthetic data 対象 table/object_list が空です。")
        target_identity = table_identity or object_identities[0]
        safe_objects = [identity.qualified_name for identity in object_identities]
        params_json = json.dumps(
            {
                "comments": bool(use_comments),
                "sample_rows": max(int(sample_rows), 0),
            },
            ensure_ascii=False,
        )
        procedure_candidates: list[tuple[str, dict[str, Any]]] = []
        if object_identities and table_identity is None:
            procedure_candidates.append(
                (
                    """
                    BEGIN
                        DBMS_CLOUD_AI.GENERATE_SYNTHETIC_DATA(
                            profile_name => :profile_name,
                            object_list => :object_list,
                            params => :params
                        );
                    END;
                    """,
                    {
                        "profile_name": normalized_profile_name,
                        "object_list": json.dumps(
                            [
                                {
                                    **select_ai_object_list_entry(identity),
                                    "record_count": int(row_count),
                                    **({"user_prompt": user_prompt} if user_prompt.strip() else {}),
                                }
                                for identity in object_identities
                            ],
                            ensure_ascii=False,
                        ),
                        "params": params_json,
                    },
                )
            )
        else:
            procedure_candidates.append(
                (
                    """
                    BEGIN
                        DBMS_CLOUD_AI.GENERATE_SYNTHETIC_DATA(
                            profile_name => :profile_name,
                            object_name => :object_name,
                            owner_name => :owner_name,
                            record_count => :row_count,
                            user_prompt => :user_prompt,
                            params => :params
                        );
                    END;
                    """,
                    {
                        "profile_name": normalized_profile_name,
                        # DBMS_CLOUD_AI は SQL 識別子として解釈する。引用しない `Mixed_Case` は
                        # 大文字の同名表 `MIXED_CASE` に生成する（#564 で実 Oracle を確認）。
                        "object_name": format_object_part(target_identity.object_name),
                        "owner_name": format_object_part(target_identity.owner),
                        "row_count": int(row_count),
                        "user_prompt": user_prompt or None,
                        "params": params_json,
                    },
                )
            )
        errors: list[str] = []
        with self.connection() as conn, conn.cursor() as cursor:
            if on_connection is not None:
                on_connection(conn)
            for sql, params in procedure_candidates:
                try:
                    cursor.execute(sql, params)
                    conn.commit()
                    return {
                        "runtime": "oracle",
                        "package": "DBMS_CLOUD_AI",
                        "mode": "procedure",
                        "table_name": target_identity.qualified_name,
                        "object_list": safe_objects,
                        "row_count": int(row_count),
                    }
                except Exception as exc:
                    message = str(exc)
                    if self._looks_like_signature_error(message):
                        errors.append(message)
                        continue
                    raise OracleAdapterError(
                        f"DBMS_CLOUD_AI.GENERATE_SYNTHETIC_DATA に失敗しました: {message}"
                    ) from exc
        raise OracleAdapterError(
            "DBMS_CLOUD_AI.GENERATE_SYNTHETIC_DATA の対応 signature が見つかりません: "
            + "; ".join(errors)
        )

    def _feedback_vector_create_table_sql(self, quoted_table: str) -> str:
        return (
            f"CREATE TABLE {quoted_table} ("  # nosec B608
            "HISTORY_ID VARCHAR2(64) PRIMARY KEY, "
            "PROFILE_ID VARCHAR2(128), "
            "QUESTION CLOB, "
            "GENERATED_SQL CLOB, "
            "FEEDBACK_RATING VARCHAR2(32), "
            f"EMBEDDING {EMBEDDING_VECTOR_COLUMN_TYPE}, "
            "CREATED_AT TIMESTAMP WITH TIME ZONE)"
        )

    def _feedback_vector_create_index_sql(self, *, quoted_table: str, quoted_index: str) -> str:
        return (
            f"CREATE VECTOR INDEX {quoted_index} "  # nosec B608
            f"ON {quoted_table} (EMBEDDING) "
            "ORGANIZATION INMEMORY NEIGHBOR GRAPH DISTANCE COSINE"
        )

    @contextmanager
    def _feedback_vector_operation_lock(self, cursor: Any) -> Iterator[None]:
        """feedback VECTOR table の rebuild / publish / clear を DB session 間で直列化する。"""
        binds = {
            "lock_id": FEEDBACK_VECTOR_DB_LOCK_ID,
            "lock_timeout": FEEDBACK_VECTOR_DB_LOCK_TIMEOUT_SECONDS,
        }
        try:
            cursor.execute(
                """
                DECLARE
                    lock_result PLS_INTEGER;
                BEGIN
                    lock_result := DBMS_LOCK.REQUEST(
                        id => :lock_id,
                        lockmode => DBMS_LOCK.X_MODE,
                        timeout => :lock_timeout,
                        release_on_commit => FALSE
                    );
                    IF lock_result NOT IN (0, 4) THEN
                        RAISE_APPLICATION_ERROR(
                            -20051,
                            'Feedback vector index 操作 lock を取得できません。'
                        );
                    END IF;
                END;
                """,
                binds,
            )
        except Exception as exc:
            raise OracleAdapterError(
                f"Feedback vector index 操作 lock を取得できません: {exc}"
            ) from exc
        try:
            yield
        finally:
            try:
                cursor.execute(
                    """
                    DECLARE
                        lock_result PLS_INTEGER;
                    BEGIN
                        lock_result := DBMS_LOCK.RELEASE(id => :lock_id);
                    END;
                    """,
                    {"lock_id": FEEDBACK_VECTOR_DB_LOCK_ID},
                )
            except Exception as exc:
                logger.warning("feedback vector operation lock release failed: %s", exc)

    def rebuild_feedback_vector_index(
        self,
        *,
        table_name: str,
        index_name: str,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Recreate the NL2SQL feedback VECTOR table and vector index."""
        safe_table = _strict_sql_name(table_name)
        safe_index = _strict_sql_name(index_name)
        quoted_table = _quote_identifier(safe_table)
        quoted_index = _quote_identifier(safe_index)
        stage_table = _staging_sql_name(safe_table)
        stage_index = _staging_sql_name(safe_index)
        quoted_stage_table = _quote_identifier(stage_table)
        quoted_stage_index = _quote_identifier(stage_index)
        create_table = self._feedback_vector_create_table_sql(quoted_stage_table)
        insert_sql = (
            f"INSERT INTO {quoted_stage_table} "  # nosec B608
            "(HISTORY_ID, PROFILE_ID, QUESTION, GENERATED_SQL, "
            "FEEDBACK_RATING, EMBEDDING, CREATED_AT) "
            "VALUES (:history_id, :profile_id, :question, :generated_sql, :feedback_rating, "
            "TO_VECTOR(:embedding_json), SYSTIMESTAMP)"
        )
        create_index = self._feedback_vector_create_index_sql(
            quoted_table=quoted_stage_table,
            quoted_index=quoted_stage_index,
        )
        bind_rows = [
            {
                "history_id": str(row["history_id"]),
                "profile_id": str(row.get("profile_id") or ""),
                "question": str(row.get("question") or ""),
                "generated_sql": str(row.get("generated_sql") or ""),
                "feedback_rating": str(row.get("feedback_rating") or ""),
                "embedding_json": json.dumps(row.get("embedding") or []),
            }
            for row in rows
        ]
        old_replaced = False
        try:
            with self.connection() as conn, conn.cursor() as cursor:
                with self._feedback_vector_operation_lock(cursor):
                    self._drop_best_effort(cursor, f"DROP INDEX {quoted_stage_index}", {})
                    self._drop_best_effort(cursor, f"DROP TABLE {quoted_stage_table} PURGE", {})
                    self._execute_plsql_like(cursor, create_table, {})
                    if bind_rows:
                        cursor.executemany(insert_sql, bind_rows)
                    self._execute_plsql_like(cursor, create_index, {})
                    old_replaced = True
                    self._drop_best_effort(cursor, f"DROP INDEX {quoted_index}", {})
                    self._drop_best_effort(cursor, f"DROP TABLE {quoted_table} PURGE", {})
                    self._execute_plsql_like(
                        cursor,
                        f"ALTER TABLE {quoted_stage_table} RENAME TO {quoted_table}",  # nosec B608
                        {},
                    )
                    self._execute_plsql_like(
                        cursor,
                        f"ALTER INDEX {quoted_stage_index} RENAME TO {quoted_index}",  # nosec B608
                        {},
                    )
                conn.commit()
        except OracleAdapterError:
            if not old_replaced:
                with suppress(Exception), self.connection() as conn, conn.cursor() as cursor:
                    self._drop_best_effort(cursor, f"DROP INDEX {quoted_stage_index}", {})
                    self._drop_best_effort(cursor, f"DROP TABLE {quoted_stage_table} PURGE", {})
                    conn.commit()
            raise
        except Exception as exc:
            if not old_replaced:
                with suppress(Exception), self.connection() as conn, conn.cursor() as cursor:
                    self._drop_best_effort(cursor, f"DROP INDEX {quoted_stage_index}", {})
                    self._drop_best_effort(cursor, f"DROP TABLE {quoted_stage_table} PURGE", {})
                    conn.commit()
            raise OracleAdapterError(
                f"Feedback vector index の rebuild に失敗しました: {exc}"
            ) from exc
        return {
            "runtime": "oracle",
            "table_name": safe_table,
            "index_name": safe_index,
            "row_count": len(bind_rows),
        }

    def upsert_feedback_vector_entry(
        self,
        *,
        table_name: str,
        index_name: str,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        """Upsert one approved NL2SQL feedback item into the VECTOR table."""
        safe_table = _strict_sql_name(table_name)
        safe_index = _strict_sql_name(index_name)
        quoted_table = _quote_identifier(safe_table)
        quoted_index = _quote_identifier(safe_index)
        merge_sql = (
            f"MERGE INTO {quoted_table} target "  # nosec B608
            "USING (SELECT :history_id AS HISTORY_ID FROM DUAL) src "
            "ON (target.HISTORY_ID = src.HISTORY_ID) "
            "WHEN MATCHED THEN UPDATE SET "
            "PROFILE_ID = :profile_id, "
            "QUESTION = :question, "
            "GENERATED_SQL = :generated_sql, "
            "FEEDBACK_RATING = :feedback_rating, "
            "EMBEDDING = TO_VECTOR(:embedding_json), "
            "CREATED_AT = SYSTIMESTAMP "
            "WHEN NOT MATCHED THEN INSERT "
            "(HISTORY_ID, PROFILE_ID, QUESTION, GENERATED_SQL, FEEDBACK_RATING, "
            "EMBEDDING, CREATED_AT) "
            "VALUES (:history_id, :profile_id, :question, :generated_sql, :feedback_rating, "
            "TO_VECTOR(:embedding_json), SYSTIMESTAMP)"
        )
        params = {
            "history_id": str(row["history_id"]),
            "profile_id": str(row.get("profile_id") or ""),
            "question": str(row.get("question") or ""),
            "generated_sql": str(row.get("generated_sql") or ""),
            "feedback_rating": str(row.get("feedback_rating") or ""),
            "embedding_json": json.dumps(row.get("embedding") or []),
        }
        with self.connection() as conn, conn.cursor() as cursor:
            with self._feedback_vector_operation_lock(cursor):
                self._execute_ddl_allow_existing(
                    cursor,
                    self._feedback_vector_create_table_sql(quoted_table),
                    {},
                )
                self._execute_plsql_like(cursor, merge_sql, params)
                self._execute_ddl_allow_existing(
                    cursor,
                    self._feedback_vector_create_index_sql(
                        quoted_table=quoted_table,
                        quoted_index=quoted_index,
                    ),
                    {},
                )
            conn.commit()
        return {
            "runtime": "oracle",
            "table_name": safe_table,
            "index_name": safe_index,
            "history_id": params["history_id"],
            "executed": True,
        }

    def delete_feedback_vector_entry(self, *, table_name: str, history_id: str) -> dict[str, Any]:
        """Remove one NL2SQL feedback item from the VECTOR table if it exists."""
        safe_table = _strict_sql_name(table_name)
        quoted_table = _quote_identifier(safe_table)
        delete_sql = f"DELETE FROM {quoted_table} WHERE HISTORY_ID = :history_id"  # nosec B608
        with self.connection() as conn, conn.cursor() as cursor:
            try:
                with self._feedback_vector_operation_lock(cursor):
                    cursor.execute(delete_sql, {"history_id": history_id})
                conn.commit()
            except Exception as exc:
                message = str(exc)
                if "ORA-00942" in message or "ORA-04043" in message:
                    return {
                        "runtime": "oracle",
                        "table_name": safe_table,
                        "history_id": history_id,
                        "executed": False,
                    }
                raise OracleAdapterError(
                    f"Feedback vector entry の削除に失敗しました: {exc}"
                ) from exc
        return {
            "runtime": "oracle",
            "table_name": safe_table,
            "history_id": history_id,
            "executed": True,
        }

    def clear_feedback_vector_index(self, *, table_name: str, index_name: str) -> dict[str, Any]:
        """Drop the NL2SQL feedback VECTOR index/table if present."""
        safe_table = _strict_sql_name(table_name)
        safe_index = _strict_sql_name(index_name)
        quoted_table = _quote_identifier(safe_table)
        quoted_index = _quote_identifier(safe_index)
        with self.connection() as conn, conn.cursor() as cursor:
            with self._feedback_vector_operation_lock(cursor):
                self._drop_best_effort(cursor, f"DROP INDEX {quoted_index}", {})
                self._drop_best_effort(cursor, f"DROP TABLE {quoted_table} PURGE", {})
            conn.commit()
        return {
            "runtime": "oracle",
            "table_name": safe_table,
            "index_name": safe_index,
        }

    def search_feedback_vector_index(
        self,
        *,
        table_name: str,
        embedding: list[float],
        profile_id: str | None,
        profile_ids: Iterable[str] | None = None,
        include_bad: bool,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Search feedback history with Oracle AI Vector Search similarity."""
        safe_table = _strict_sql_name(table_name)
        quoted_table = _quote_identifier(safe_table)
        filters = ["1 = 1"]
        binds: dict[str, Any] = {
            "embedding_json": json.dumps(embedding),
            "limit": max(limit, 1),
        }
        scoped_profile_ids = sorted(
            {str(profile_id or "").strip()}
            | {str(item or "").strip() for item in profile_ids or []}
        )
        scoped_profile_ids = [item for item in scoped_profile_ids if item]
        if len(scoped_profile_ids) == 1:
            filters.append("PROFILE_ID = :profile_id")
            binds["profile_id"] = scoped_profile_ids[0]
        elif scoped_profile_ids:
            names: list[str] = []
            for index, scoped_profile_id in enumerate(scoped_profile_ids):
                name = f"profile_id_{index}"
                names.append(f":{name}")
                binds[name] = scoped_profile_id
            filters.append(f"PROFILE_ID IN ({', '.join(names)})")
        if not include_bad:
            filters.append("FEEDBACK_RATING = :feedback_rating")
            binds["feedback_rating"] = "good"
        where_clause = " AND ".join(filters)
        query = (
            "SELECT HISTORY_ID, PROFILE_ID, QUESTION, GENERATED_SQL, FEEDBACK_RATING, "
            "VECTOR_DISTANCE(EMBEDDING, TO_VECTOR(:embedding_json), COSINE) AS DISTANCE "
            f"FROM {quoted_table} "  # nosec B608
            f"WHERE {where_clause} "
            "ORDER BY DISTANCE FETCH FIRST :limit ROWS ONLY"
        )
        with self.runtime_connection() as conn, conn.cursor() as cursor:
            # QUESTION / GENERATED_SQL の CLOB は行と一緒に文字列で受け取る（#904）。
            configure_clob_fetch_as_text(cursor)
            cursor.execute(query, binds)
            rows = cursor.fetchall() if hasattr(cursor, "fetchall") else list(cursor)
        results: list[dict[str, Any]] = []
        for row in rows:
            distance = float(row[5] or 0)
            results.append(
                {
                    "history_id": str(row[0] or ""),
                    "profile_id": str(row[1] or ""),
                    "question": _coerce_text(row[2]),
                    "generated_sql": _coerce_text(row[3]),
                    "feedback_rating": str(row[4] or ""),
                    "distance": distance,
                    "score": round(max(0.0, 1.0 - distance), 3),
                }
            )
        return results

    def generate_select_ai_text(
        self,
        *,
        profile_name: str,
        question: str,
        action: str = "showsql",
        attributes: dict[str, str] | None = None,
        call_timeout_seconds: float | None = None,
    ) -> str:
        """Oracle Select AI profile で DBMS_CLOUD_AI.GENERATE の生テキストを返す。

        DBMS_CLOUD_AI.GENERATE の属性は環境差があるため、呼び出しは adapter 内に限定する。
        profile は引数で渡し、session の profile（`DBMS_CLOUD_AI.SET_PROFILE`）は変えないため、
        アプリの接続の pool（`runtime_connection`。actor の種類ごとに別の pool）から借りる（#904）。
        """
        with observed_span(
            "oracle.select_ai",
            kind="client",
            attributes={
                "db.system.name": "oracle.db",
                "db.operation.name": "SELECT_AI",
                **model_purpose_attributes(default="sql.generate"),
                # DB の中でモデルを呼ぶので、アプリは利用量を受け取れない（実行履歴は数を作らず
                # 未計測にする。#3543）。
                UNMEASURED_SOURCE_ATTRIBUTE: "select_ai",
            },
        ) as call_trace:
            call_trace.set_call_input(
                {
                    "prompt": question,
                    "profile_name": profile_name,
                    "action": action,
                    **({"attributes": attributes} if attributes else {}),
                }
            )
            connection = (
                self.runtime_connection(call_timeout_seconds=call_timeout_seconds)
                if call_timeout_seconds is not None
                else self.runtime_connection()
            )
            with connection as conn, conn.cursor() as cursor:
                # 生成結果の CLOB は行と一緒に文字列で受け取る（LOB の read の往復を無くす。#904）。
                configure_clob_fetch_as_text(cursor)
                binds: dict[str, str] = {
                    "prompt": question,
                    "profile_name": profile_name,
                    "action": action,
                }
                if attributes:
                    binds["attributes"] = json.dumps(attributes, ensure_ascii=False)
                    cursor.execute(
                        """
                        SELECT DBMS_CLOUD_AI.GENERATE(
                            prompt => :prompt,
                            profile_name => :profile_name,
                            action => :action,
                            attributes => :attributes
                        )
                        FROM DUAL
                        """,
                        binds,
                    )
                else:
                    cursor.execute(
                        """
                        SELECT DBMS_CLOUD_AI.GENERATE(
                            prompt => :prompt,
                            profile_name => :profile_name,
                            action => :action
                        )
                        FROM DUAL
                        """,
                        binds,
                    )
                row = cursor.fetchone()
                text = _coerce_text(row[0] if row else "")
            call_trace.set_call_output(text)
            return text

    def generate_select_ai_sql(
        self,
        *,
        profile_name: str,
        question: str,
        action: str = "showsql",
        attributes: dict[str, str] | None = None,
        call_timeout_seconds: float | None = None,
    ) -> str:
        """Oracle Select AI profile で SQL を生成する。"""
        text = self.generate_select_ai_text(
            profile_name=profile_name,
            question=question,
            action=action,
            attributes=attributes,
            call_timeout_seconds=call_timeout_seconds,
        )
        return _extract_select_statement(text)

    def generate_select_ai_prompt(
        self,
        *,
        profile_name: str,
        question: str,
        attributes: dict[str, str] | None = None,
        call_timeout_seconds: float | None = None,
    ) -> str:
        """Oracle Select AI profile の showprompt 結果を返す。"""
        return self.generate_select_ai_text(
            profile_name=profile_name,
            question=question,
            action="showprompt",
            attributes=attributes,
            call_timeout_seconds=call_timeout_seconds,
        )

    def refresh_select_ai_profile(
        self,
        *,
        profile_name: str,
        allowed_tables: list[str],
        row_limit: int | None,
        description: str = "",
    ) -> dict[str, Any]:
        """Create or replace an Oracle Select AI profile."""
        attributes = self._select_ai_profile_attributes(
            allowed_tables=allowed_tables,
            row_limit=row_limit,
            description=description,
        )
        with self.connection() as conn, conn.cursor() as cursor:
            self._drop_cloud_ai_profile_best_effort(cursor, profile_name)
            self._execute_plsql(
                cursor,
                """
                BEGIN
                    DBMS_CLOUD_AI.CREATE_PROFILE(
                        profile_name => :profile_name,
                        attributes => :attributes
                    );
                END;
                """,
                {"profile_name": profile_name, "attributes": json.dumps(attributes)},
            )
            conn.commit()
        return {
            "runtime": "oracle",
            "package": "DBMS_CLOUD_AI",
            "profile_name": profile_name,
            "profile_attributes": attributes,
        }

    def drop_select_ai_profile(self, *, profile_name: str) -> dict[str, Any]:
        """Drop an Oracle Select AI profile if it exists."""
        with self.connection() as conn, conn.cursor() as cursor:
            self._drop_cloud_ai_profile_best_effort(cursor, profile_name)
            conn.commit()
        return {
            "runtime": "oracle",
            "package": "DBMS_CLOUD_AI",
            "profile_name": profile_name,
        }

    def refresh_select_ai_agent_assets(
        self,
        *,
        profile_name: str,
        tool_name: str,
        agent_name: str,
        task_name: str,
        team_name: str,
        allowed_tables: list[str],
        row_limit: int | None,
        description: str = "",
        refresh_profile: bool = True,
    ) -> dict[str, Any]:
        """Create or replace Oracle Select AI Agent profile/tool/agent/task/team assets."""
        profile_meta = (
            self.refresh_select_ai_profile(
                profile_name=profile_name,
                allowed_tables=allowed_tables,
                row_limit=row_limit,
                description=description,
            )
            if refresh_profile
            else {
                "runtime": "oracle",
                "package": "DBMS_CLOUD_AI",
                "profile_name": profile_name,
                "reused": True,
            }
        )
        profile_attributes = self._select_ai_profile_attributes(
            allowed_tables=allowed_tables,
            row_limit=row_limit,
            description=description,
        )
        tool_attributes = {
            "tool_type": "SQL",
            "tool_params": {"profile_name": profile_name},
            "instruction": (
                "Use this tool to generate Oracle SELECT/WITH SQL from natural language. "
                "Use SHOWSQL behavior and do not execute DML, DDL, PL/SQL, or multi-statement SQL."
            ),
        }
        agent_attributes = {
            "profile_name": profile_name,
            "role": description.strip() or "Oracle SQL による業務データ分析を支援します。",
            "tools": [tool_name],
        }
        task_attributes = {
            "instruction": (
                "Use the SQL tool to create exactly one Oracle SELECT statement for the "
                f"user's request. Invoke tool {tool_name} with JSON keys TOOL_NAME, QUERY, "
                "and ACTION. TOOL_NAME must be the SQL tool name, QUERY must be the user's "
                "natural language request, and ACTION must be SHOWSQL. "
                "Return strict JSON only with keys sql and explanation. "
                "sql must be a single SELECT/WITH statement without markdown, comments, "
                "or trailing narration. explanation must be concise and written in Japanese."
            ),
            "tools": [tool_name],
            "enable_human_tool": False,
        }
        team_attributes = {
            "agents": [{"name": agent_name, "task": task_name}],
            "process": "sequential",
        }
        with self.connection() as conn, conn.cursor() as cursor:
            for procedure, name_param, name in [
                ("DROP_TEAM", "team_name", team_name),
                ("DROP_TASK", "task_name", task_name),
                ("DROP_AGENT", "agent_name", agent_name),
                ("DROP_TOOL", "tool_name", tool_name),
            ]:
                self._drop_best_effort(
                    cursor,
                    f"""
                    BEGIN
                        DBMS_CLOUD_AI_AGENT.{procedure}({name_param} => :name, force => TRUE);
                    END;
                    """,
                    {"name": name},
                )
            self._drop_cloud_ai_profile_best_effort(cursor, f"AGENT${team_name}")
            self._drop_sql_translator_profile_best_effort(cursor, f"AGENT${team_name}")
            conn.commit()
            self._execute_agent_create(
                cursor,
                procedure="CREATE_TOOL",
                name_param="tool_name",
                name=tool_name,
                attributes=tool_attributes,
            )
            self._execute_agent_create(
                cursor,
                procedure="CREATE_AGENT",
                name_param="agent_name",
                name=agent_name,
                attributes=agent_attributes,
            )
            self._execute_agent_create(
                cursor,
                procedure="CREATE_TASK",
                name_param="task_name",
                name=task_name,
                attributes=task_attributes,
            )
            try:
                self._execute_agent_create(
                    cursor,
                    procedure="CREATE_TEAM",
                    name_param="team_name",
                    name=team_name,
                    attributes=team_attributes,
                )
            except OracleAdapterError as exc:
                if not self._looks_like_profile_already_exists(str(exc)):
                    raise
                self._drop_cloud_ai_profile_best_effort(cursor, f"AGENT${team_name}")
                self._drop_sql_translator_profile_best_effort(cursor, f"AGENT${team_name}")
                conn.commit()
                self._execute_agent_create(
                    cursor,
                    procedure="CREATE_TEAM",
                    name_param="team_name",
                    name=team_name,
                    attributes=team_attributes,
                )
            conn.commit()
        return {
            "runtime": "oracle",
            "package": "DBMS_CLOUD_AI_AGENT",
            "select_ai_profile_meta": profile_meta,
            "profile_attributes": profile_attributes,
            "tool_attributes": tool_attributes,
            "agent_attributes": agent_attributes,
            "task_attributes": task_attributes,
            "team_attributes": team_attributes,
        }

    def drop_select_ai_agent_assets(
        self,
        *,
        profile_name: str,
        tool_name: str,
        agent_name: str,
        task_name: str,
        team_name: str,
    ) -> dict[str, Any]:
        """Drop Oracle Select AI Agent assets if they exist."""
        with self.connection() as conn, conn.cursor() as cursor:
            for procedure, name_param, name in [
                ("DROP_TEAM", "team_name", team_name),
                ("DROP_TASK", "task_name", task_name),
                ("DROP_AGENT", "agent_name", agent_name),
                ("DROP_TOOL", "tool_name", tool_name),
            ]:
                self._drop_best_effort(
                    cursor,
                    f"""
                    BEGIN
                        DBMS_CLOUD_AI_AGENT.{procedure}({name_param} => :name, force => TRUE);
                    END;
                    """,
                    {"name": name},
                )
            for name in [f"AGENT${team_name}", profile_name]:
                self._drop_cloud_ai_profile_best_effort(cursor, name)
            self._drop_sql_translator_profile_best_effort(cursor, f"AGENT${team_name}")
            conn.commit()
        return {
            "runtime": "oracle",
            "package": "DBMS_CLOUD_AI_AGENT",
            "profile_name": profile_name,
            "tool_name": tool_name,
            "agent_name": agent_name,
            "task_name": task_name,
            "team_name": team_name,
        }

    def run_select_ai_agent_team(
        self,
        *,
        team_name: str,
        question: str,
        tool_name: str | None = None,
        conversation_id: str | None = None,
        call_timeout_seconds: float | None = None,
    ) -> tuple[str, str]:
        """Select AI Agent の team を、会話（conversation_id）を付けて RUN_TEAM で実行する。

        conversation_id は ``DBMS_CLOUD_AI.CREATE_CONVERSATION`` で作る（渡されたらそれを続ける）。
        作れなければ、RUN_TOOL に切り替えずに失敗を返す（RUN_TOOL は team の agent・task の
        指示を通らず、利用者が選んだ方式と違う結果になるため）。RUN_TEAM の引数は公式の文書の
        ``(team_name, user_prompt, params)`` で、conversation_id は ``params`` の JSON に入れる。
        team の profile が消えて RUN_TOOL に切り替えるときは、RUN_TEAM の span を失敗で閉じてから
        RUN_TOOL を呼ぶ（2 つの呼び出しを生成の工程の下の兄弟にする。#3477）。
        """
        with observed_span(
            "oracle.select_ai",
            kind="client",
            attributes={
                "db.system.name": "oracle.db",
                "db.operation.name": "SELECT_AI",
                **model_purpose_attributes(default="sql.generate"),
                # DB の中でモデルを呼ぶので、アプリは利用量を受け取れない（実行履歴は数を作らず
                # 未計測にする。#3543）。
                UNMEASURED_SOURCE_ATTRIBUTE: "select_ai_agent",
            },
        ) as team_trace:
            active_conversation_id = (conversation_id or "").strip()
            if not active_conversation_id:
                active_conversation_id = self.create_agent_conversation(
                    call_timeout_seconds=call_timeout_seconds
                )
            params = json.dumps(
                {"conversation_id": active_conversation_id},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            team_trace.set_call_input(
                {"team_name": team_name, "user_prompt": question, "params": params}
            )
            connection = (
                self.connection(call_timeout_seconds=call_timeout_seconds)
                if call_timeout_seconds is not None
                else self.connection()
            )
            with connection as conn, conn.cursor() as cursor:
                try:
                    cursor.execute(
                        """
                        SELECT DBMS_CLOUD_AI_AGENT.RUN_TEAM(
                            team_name => :team_name,
                            user_prompt => :user_prompt,
                            params => :params
                        )
                        FROM DUAL
                        """,
                        {"team_name": team_name, "user_prompt": question, "params": params},
                    )
                    row = cursor.fetchone()
                    text = _coerce_text(row[0] if row else "")
                    team_trace.set_call_output(text)
                    return _extract_select_statement(text), active_conversation_id
                except Exception as exc:
                    message = str(exc)
                    if not self._looks_like_agent_profile_loss(message):
                        raise self._agent_runtime_error(exc) from exc
                    team_trace.set_call_output({"error_type": type(exc).__name__})
                    team_trace.set_attributes({"error.type": type(exc).__name__})
                    team_trace.set_status("failed")
        # team が参照する profile（AGENT$<team>）が消えたときだけ、tool を直接呼んで復旧する。
        # conversation_id は直接実行の名前になり、service が復旧を記録する。
        fallback_tool = tool_name or self._tool_name_from_team_name(team_name)
        logger.warning(
            "Select AI Agent team の profile が見つからないため RUN_TOOL に切り替えます。",
            extra={
                "event": "select_ai_agent_run_tool_fallback",
                "team_name": team_name,
                "tool_name": fallback_tool,
            },
        )
        return self.run_select_ai_agent_tool(
            tool_name=fallback_tool,
            question=question,
            call_timeout_seconds=call_timeout_seconds,
        )

    def run_select_ai_agent_tool(
        self,
        *,
        tool_name: str,
        question: str,
        call_timeout_seconds: float | None = None,
    ) -> tuple[str, str]:
        with observed_span(
            "oracle.select_ai",
            kind="client",
            attributes={
                "db.system.name": "oracle.db",
                "db.operation.name": "SELECT_AI",
                **model_purpose_attributes(default="sql.generate"),
                # DB の中でモデルを呼ぶので、アプリは利用量を受け取れない（実行履歴は数を作らず
                # 未計測にする。#3543）。
                UNMEASURED_SOURCE_ATTRIBUTE: "select_ai_agent",
            },
        ) as call_trace:
            payload = json.dumps(
                {"TOOL_NAME": tool_name, "QUERY": question, "ACTION": "SHOWSQL"},
                ensure_ascii=False,
            )
            call_trace.set_call_input({"tool_name": tool_name, "input": payload})
            connection = (
                self.connection(call_timeout_seconds=call_timeout_seconds)
                if call_timeout_seconds is not None
                else self.connection()
            )
            with connection as conn, conn.cursor() as cursor:
                try:
                    cursor.execute(
                        """
                        SELECT DBMS_CLOUD_AI_AGENT.RUN_TOOL(
                            tool_name => :tool_name,
                            input => :input
                        )
                        FROM DUAL
                        """,
                        {"tool_name": tool_name, "input": payload},
                    )
                except Exception as exc:
                    raise self._agent_runtime_error(exc) from exc
                row = cursor.fetchone()
                text = _coerce_text(row[0] if row else "")
            call_trace.set_call_output(text)
            return _extract_select_statement(text), f"run_tool:{tool_name}"

    def create_agent_conversation(self, *, call_timeout_seconds: float | None = None) -> str:
        """Select AI Agent の会話の id を ``DBMS_CLOUD_AI.CREATE_CONVERSATION`` で作る（#1681）。

        会話の API は ``DBMS_CLOUD_AI`` にあり、``DBMS_CLOUD_AI_AGENT`` には無い（公式の文書と
        実 ADB で確かめた）。Select AI Agent（2025-10）は Select AI Conversations（2025-09）
        より後に入ったため、Agent を使える ADB では必ず使える。
        """
        connection = (
            self.connection(call_timeout_seconds=call_timeout_seconds)
            if call_timeout_seconds is not None
            else self.connection()
        )
        with connection as conn, conn.cursor() as cursor:
            try:
                cursor.execute("SELECT DBMS_CLOUD_AI.CREATE_CONVERSATION() FROM DUAL")
            except Exception as exc:
                raise self._agent_conversation_error(exc) from exc
            row = cursor.fetchone()
        conversation_id = _coerce_text(row[0] if row else "").strip()
        if not conversation_id:
            raise OracleAdapterError(
                "Select AI Agent の会話（conversation_id）を作成できませんでした。"
                "DBMS_CLOUD_AI.CREATE_CONVERSATION が空の値を返しました。"
            )
        return conversation_id

    def _select_ai_profile_attributes(
        self, *, allowed_tables: list[str], row_limit: int | None, description: str
    ) -> dict[str, Any]:
        del row_limit, description
        attributes: dict[str, Any] = {
            "provider": self.settings.nl2sql_select_ai_provider,
            "enforce_object_list": True,
            "annotations": True,
            "comments": True,
            "constraints": True,
            "object_list": self._object_list(allowed_tables),
        }
        if self.settings.nl2sql_select_ai_credential_name:
            attributes["credential_name"] = self.settings.nl2sql_select_ai_credential_name
        if self.settings.nl2sql_select_ai_model:
            attributes["model"] = self.settings.nl2sql_select_ai_model
        select_ai_region = (
            self.settings.nl2sql_select_ai_region.strip() or self.settings.oci_region.strip()
        )
        if select_ai_region:
            attributes["region"] = select_ai_region
        if self.settings.oci_compartment_id:
            attributes["oci_compartment_id"] = self.settings.oci_compartment_id
        return attributes

    def _object_list(self, allowed_tables: list[str]) -> list[dict[str, str]]:
        owner = self.settings.oracle_user.upper() if self.settings.oracle_user else ""
        objects: list[dict[str, str]] = []
        for table_name in allowed_tables:
            if not table_name.strip():
                continue
            # 大文字化・dot 分割すると `SALES."Mixed_Case"` が `SALES."MIXED_CASE"` に化けるため、
            # 引用規則どおりに分解し、Oracle が解釈する token で渡す（#561 / #564）。
            try:
                identity = parse_object_identity(table_name, default_owner=owner)
            except ValueError:
                if owner:
                    raise OracleAdapterError(
                        f"{table_name}: Select AI の対象 object 名が不正です。"
                    ) from None
                objects.append({"name": format_object_part(normalize_object_part(table_name))})
                continue
            objects.append(select_ai_object_list_entry(identity))
        return objects

    def _execute_agent_create(
        self,
        cursor: Any,
        *,
        procedure: str,
        name_param: str,
        name: str,
        attributes: dict[str, Any],
    ) -> None:
        attributes_json = json.dumps(attributes, ensure_ascii=False)
        self._execute_first_supported_plsql(
            cursor,
            [
                (
                    f"""
                    BEGIN
                        DBMS_CLOUD_AI_AGENT.{procedure}(
                            {name_param} => :name,
                            attributes => :attributes
                        );
                    END;
                    """,
                    {"name": name, "attributes": attributes_json},
                ),
                (
                    f"""
                    BEGIN
                        DBMS_CLOUD_AI_AGENT.{procedure}(
                            name => :name,
                            attributes => :attributes
                        );
                    END;
                    """,
                    {"name": name, "attributes": attributes_json},
                ),
            ],
        )

    def _execute_first_supported_plsql(
        self, cursor: Any, candidates: list[tuple[str, dict[str, Any]]]
    ) -> None:
        errors: list[str] = []
        for sql, params in candidates:
            try:
                self._execute_plsql(cursor, sql, params)
                return
            except OracleAdapterError as exc:
                if not self._looks_like_signature_error(str(exc)):
                    raise
                errors.append(str(exc))
        raise OracleAdapterError("; ".join(errors) or "Oracle PL/SQL 呼び出しに失敗しました。")

    def _execute_plsql(self, cursor: Any, sql: str, params: dict[str, Any]) -> None:
        try:
            cursor.execute(sql, params)
        except Exception as exc:
            raise OracleAdapterError(f"Oracle PL/SQL 実行に失敗しました: {exc}") from exc

    def _execute_plsql_like(self, cursor: Any, sql: str, params: dict[str, Any]) -> None:
        try:
            cursor.execute(sql, params)
        except Exception as exc:
            raise OracleAdapterError(f"Oracle SQL 実行に失敗しました: {exc}") from exc

    def _execute_ddl_allow_existing(self, cursor: Any, sql: str, params: dict[str, Any]) -> bool:
        try:
            cursor.execute(sql, params)
        except Exception as exc:
            message = str(exc)
            if "ORA-00955" in message or "ORA-01408" in message:
                return False
            raise OracleAdapterError(f"Oracle SQL 実行に失敗しました: {exc}") from exc
        return True

    def _drop_best_effort(self, cursor: Any, sql: str, params: dict[str, Any]) -> bool:
        try:
            cursor.execute(sql, params)
        except Exception:
            return False
        return True

    def _drop_cloud_ai_profile_best_effort(self, cursor: Any, profile_name: str) -> None:
        for sql, params in [
            (
                """
                BEGIN
                    DBMS_CLOUD_AI.DROP_PROFILE(
                        profile_name => :name,
                        force => TRUE
                    );
                END;
                """,
                {"name": profile_name},
            ),
            (
                """
                BEGIN
                    DBMS_CLOUD_AI.DROP_PROFILE(profile_name => :name);
                END;
                """,
                {"name": profile_name},
            ),
            (
                """
                BEGIN
                    DBMS_CLOUD_AI.DROP_PROFILE(:name, TRUE);
                END;
                """,
                {"name": profile_name},
            ),
            (
                """
                BEGIN
                    DBMS_CLOUD_AI.DROP_PROFILE(:name);
                END;
                """,
                {"name": profile_name},
            ),
        ]:
            if self._drop_best_effort(cursor, sql, params):
                break

    def _drop_sql_translator_profile_best_effort(self, cursor: Any, profile_name: str) -> None:
        for sql, params in [
            (
                """
                BEGIN
                    DBMS_SQL_TRANSLATOR.DROP_PROFILE(profile_name => :name);
                END;
                """,
                {"name": profile_name},
            ),
            (
                """
                BEGIN
                    DBMS_SQL_TRANSLATOR.DROP_PROFILE(:name);
                END;
                """,
                {"name": profile_name},
            ),
        ]:
            self._drop_best_effort(cursor, sql, params)

    def _looks_like_signature_error(self, message: str) -> bool:
        normalized = message.upper()
        return (
            "PLS-00306" in normalized
            or "PLS-306" in normalized
            or "PLS-00302" in normalized
            or "ORA-00904" in normalized
        )

    def _looks_like_profile_already_exists(self, message: str) -> bool:
        normalized = message.upper()
        return "PROFILE" in normalized and "ALREADY EXISTS" in normalized

    def _agent_runtime_error(self, exc: Exception) -> OracleAdapterError:
        message = str(exc)
        if self._looks_like_signature_error(message):
            return OracleAdapterError(
                "Oracle Select AI Agent runtime API がこの database では利用できません。"
                f"DBMS_CLOUD_AI_AGENT の version / 権限を確認してください: {message}"
            )
        return OracleAdapterError(f"Oracle Select AI Agent 実行に失敗しました: {message}")

    def _agent_conversation_error(self, exc: Exception) -> OracleAdapterError:
        message = str(exc)
        if self._looks_like_signature_error(message):
            return OracleAdapterError(
                "Select AI Agent の会話（conversation_id）を作成できませんでした。"
                "この database では DBMS_CLOUD_AI.CREATE_CONVERSATION を使えません。"
                "Select AI の会話に対応した Oracle Autonomous AI Database と、"
                f"DBMS_CLOUD_AI の EXECUTE 権限を確認してください: {message}"
            )
        return OracleAdapterError(
            "Select AI Agent の会話（conversation_id）を作成できませんでした。"
            f"DBMS_CLOUD_AI の権限と接続状態を確認してください: {message}"
        )

    def _looks_like_agent_profile_loss(self, message: str) -> bool:
        normalized = message.upper()
        return "INVALID PROFILE" in normalized or "ORA-20046" in normalized

    def _tool_name_from_team_name(self, team_name: str) -> str:
        if team_name.endswith("_TEAM"):
            return f"{team_name[: -len('_TEAM')]}_TOOL"
        return f"{team_name}_TOOL"

    def _admin_statement_type(self, statement: str) -> str:
        stripped = str(statement or "").strip()
        while stripped.startswith("--") or stripped.startswith("/*"):
            if stripped.startswith("--"):
                newline = stripped.find("\n")
                stripped = "" if newline < 0 else stripped[newline + 1 :].lstrip()
            else:
                end = stripped.find("*/")
                stripped = "" if end < 0 else stripped[end + 2 :].lstrip()
        if re.match(r"^comment\s+on\b", stripped, flags=re.IGNORECASE):
            return "COMMENT"
        if re.match(r"^(select|with)\b", stripped, flags=re.IGNORECASE):
            return "SELECT"
        if re.match(r"^(begin|declare|exec|execute)\b", stripped, flags=re.IGNORECASE):
            return "PLSQL"
        for keyword in (
            "insert",
            "update",
            "delete",
            "merge",
            "create",
            "drop",
            "alter",
            "truncate",
            "rename",
            "flashback",
            "purge",
            "grant",
            "revoke",
        ):
            if re.match(rf"^{keyword}\b", stripped, flags=re.IGNORECASE):
                return keyword.upper()
        return "UNKNOWN"

    def _normalize_admin_statement(self, statement: str) -> str:
        stripped = str(statement or "").strip()
        if re.match(r"^(exec|execute)\b", stripped, flags=re.IGNORECASE):
            body = re.sub(r"^(exec|execute)\s+", "", stripped, flags=re.IGNORECASE).strip()
            return f"BEGIN {body.rstrip(';')}; END;"
        return stripped

    def _fetch_dbms_output(self, cursor: Any, batch: int = 1000) -> str:
        lines: list[str] = []
        try:
            line_var = cursor.var(str)
            status_var = cursor.var(int)
            for _ in range(batch):
                cursor.callproc("dbms_output.get_line", (line_var, status_var))
                if int(status_var.getvalue() or 0) != 0:
                    break
                lines.append(str(line_var.getvalue() or ""))
        except Exception:
            return ""
        return "\n".join(line for line in lines if line)

    def _admin_success_message(self, statement_type: str, row_count: int | None) -> str:
        if statement_type in {"INSERT", "UPDATE", "DELETE", "MERGE"}:
            return f"RowsAffected={row_count if row_count is not None else 0}"
        if statement_type == "PLSQL":
            return "PL/SQL executed"
        if statement_type == "COMMENT":
            return "Comment applied"
        return "OK"

    def _load_oracledb(self) -> Any:
        if self._oracledb is not None:
            return self._oracledb
        try:
            self._oracledb = importlib.import_module("oracledb")
        except ModuleNotFoundError as exc:
            raise OracleAdapterError("python-oracledb がインストールされていません。") from exc
        return self._oracledb

    def _init_client(self, oracledb: Any) -> None:
        ensure_deepsec_thin_mode(self.settings)
        if self._client_initialized or self.settings.oracle_driver_mode == "thin":
            return
        if not self.settings.oracle_client_lib_dir:
            return
        init_oracle_client = getattr(oracledb, "init_oracle_client", None)
        if callable(init_oracle_client):
            init_oracle_client(lib_dir=self.settings.oracle_client_lib_dir)
        self._client_initialized = True

    def _coerce_csv_value(self, value: str | None, column: CsvImportColumn) -> Any:
        if value is None or value == "":
            return None
        if column.data_type == "NUMBER":
            try:
                return int(value)
            except ValueError:
                return float(value)
        if column.data_type == "DATE":
            return datetime.fromisoformat(value)
        if column.data_type.startswith("TIMESTAMP"):
            return datetime.fromisoformat(value)
        return value
