"""DB 管理チャットの生成した文の、参照する object と列の検査（#2352）。

モデルはデータ辞書・動的性能ビューの列を記憶から書き、無い列で `ORA-00904` になる。列の一覧は
コードにもプロンプトにも書かず、生成した文が参照する object を実行と同じ管理の接続で調べる
（`SqlObjectDescriber`。データは読まない）。

- 参照する object と列は sqlglot（Oracle の方言）で取り出す。別名・`*`・副問い合わせ / CTE・
  引用識別子・`SERIAL#` のような名前を扱い、確かめられない参照（表関数・DB link・PIVOT など）は
  確かめない（存在しないと決めつけない）。
- 止めるかどうかは Oracle の解析（`parse_error`。実行しない）が `ORA-00904` を返したときだけ
  決める。sqlglot の静的な検査は、存在しない列の一覧と、どの object の列かの説明に使う
  （疑似列・PL/SQL の関数など、静的な検査が誤る文を止めないため）。Oracle の解析が使えない
  構成（決定論の実行環境）は静的な検査で決める。
- 調べられない object（存在しない・読む権限が無い）は警告だけにする。

ここは DB に触れない純粋な処理だけを持つ。調べる実装は `OracleNl2SqlAdapter.admin_sql_describer`。
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, build_scope

from .oracle_dialect import OracleConversion

# Oracle の疑似列と、括弧の無い関数（sqlglot は列として読む）。Oracle の SQL の言語の語で、
# 業務やビューの列の一覧ではない。
ORACLE_PSEUDO_COLUMNS = frozenset(
    {
        "ROWNUM",
        "ROWID",
        "LEVEL",
        "ORA_ROWSCN",
        "CONNECT_BY_ISLEAF",
        "CONNECT_BY_ISCYCLE",
        "COLUMN_VALUE",
        "OBJECT_VALUE",
        "OBJECT_ID",
        "NEXTVAL",
        "CURRVAL",
        "SYSDATE",
        "SYSTIMESTAMP",
        "CURRENT_DATE",
        "CURRENT_TIMESTAMP",
        "LOCALTIMESTAMP",
        "DBTIMEZONE",
        "SESSIONTIMEZONE",
        "USER",
        "UID",
        "VERSIONS_STARTSCN",
        "VERSIONS_STARTTIME",
        "VERSIONS_ENDSCN",
        "VERSIONS_ENDTIME",
        "VERSIONS_XID",
        "VERSIONS_OPERATION",
    }
)

# 理由の文に並べる列の数（それより多い列は「ほか N 列」）。直す指示・分析の文脈には全部を渡す。
REASON_COLUMN_LIMIT = 30
CONTEXT_COLUMN_LIMIT = 400

_SIMPLE_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_$#]{0,127}")
_ORA_INVALID_IDENTIFIER = re.compile(r"ORA-00904:\s*((?:\"[^\"]*\"\.)*\"([^\"]+)\")")
_MISSING_OBJECT_CODES = ("ORA-00942", "ORA-04043", "ORA-01031")
_IDENTIFIER_TOKEN = re.compile(r'"([^"]+)"|([A-Za-z][A-Za-z0-9_$#]*)')


class SqlObjectDescriber(Protocol):
    """参照する object の列を、データを読まずに調べる（実行と同じ管理の接続）。"""

    @property
    def available(self) -> bool:
        """接続できて調べられる（False なら describe・parse_error の結果は「分からない」）。"""
        ...

    def describe(self, reference_sql: str) -> tuple[str, ...] | None:
        """`SELECT * FROM <reference_sql>` の列の名前。調べられなければ None。"""
        ...

    def parse_error(self, sql: str) -> str | None:
        """Oracle の解析（実行しない）のエラー。通れば空、解析できない構成なら None。"""
        ...


@dataclass(frozen=True, slots=True)
class ObjectRef:
    """文が参照する object（`OWNER.NAME` など。引用の無い部分は大文字にそろえる）。"""

    parts: tuple[tuple[str, bool], ...]

    @property
    def key(self) -> str:
        return ".".join(_normalize(name, quoted) for name, quoted in self.parts)

    @property
    def name(self) -> str:
        last, quoted = self.parts[-1]
        return _normalize(last, quoted)

    def reference_sql(self) -> str | None:
        """調べる SQL に埋める参照。名前として安全に書けなければ None（調べない）。"""

        rendered: list[str] = []
        for name, quoted in self.parts:
            if quoted:
                if not name or '"' in name or "\x00" in name or len(name) > 128:
                    return None
                rendered.append(f'"{name}"')
            elif _SIMPLE_IDENTIFIER.fullmatch(name):
                rendered.append(name.upper())
            else:
                return None
        return ".".join(rendered)


@dataclass(frozen=True, slots=True)
class UnknownColumn:
    """存在しない列と、参照先の候補（名前と実際の列）。"""

    column: str
    sources: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass(slots=True)
class StatementColumnCheck:
    """1 文の検査の結果。"""

    # 調べた object の実際の列（`ObjectRef.key` → 列の名前）。
    objects: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # 調べられなかった object（存在しない・読む権限が無い）。
    undescribed: list[str] = field(default_factory=list)
    unknown: list[UnknownColumn] = field(default_factory=list)
    # Oracle の解析のエラー（空は通った、None は解析していない）。
    oracle_error: str | None = None

    @property
    def invalid(self) -> bool:
        """存在しない列を参照する（実行を止める）。"""

        return bool(self.unknown)

    @property
    def missing_object(self) -> bool:
        """Oracle の解析で、参照する object が見つからない・読めないと分かった。"""

        return bool(self.oracle_error) and any(
            code in str(self.oracle_error) for code in _MISSING_OBJECT_CODES
        )

    @property
    def needs_repair(self) -> bool:
        return self.invalid or (self.missing_object and bool(self.undescribed))


class ObjectColumnsCache:
    """調べた object の列の process ごとの cache（TTL つき。調べられない object は短く持つ）。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = 600.0,
        negative_ttl_seconds: float = 60.0,
        max_entries: int = 2000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._negative_ttl = negative_ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[str, tuple[float, tuple[str, ...] | None]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> tuple[bool, tuple[str, ...] | None]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return False, None
            expires_at, value = entry
            if expires_at <= self._clock():
                del self._entries[key]
                return False, None
            return True, value

    def put(self, key: str, value: tuple[str, ...] | None) -> None:
        ttl = self._ttl if value is not None else self._negative_ttl
        with self._lock:
            if len(self._entries) >= self._max_entries and key not in self._entries:
                self._entries.pop(next(iter(self._entries)))
            self._entries[key] = (self._clock() + ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class CachedDescriber:
    """`ObjectColumnsCache` を通して調べる（接続できないときの「分からない」は cache しない）。"""

    def __init__(self, inner: SqlObjectDescriber, cache: ObjectColumnsCache) -> None:
        self._inner = inner
        self._cache = cache

    @property
    def available(self) -> bool:
        return self._inner.available

    def describe(self, reference_sql: str) -> tuple[str, ...] | None:
        hit, value = self._cache.get(reference_sql)
        if hit:
            return value
        value = self._inner.describe(reference_sql)
        if value is not None or self._inner.available:
            self._cache.put(reference_sql, value)
        return value

    def parse_error(self, sql: str) -> str | None:
        return self._inner.parse_error(sql)


def _normalize(name: str, quoted: bool) -> str:
    return name if quoted else name.upper()


def _identifier(node: exp.Expression | None) -> tuple[str, bool] | None:
    if isinstance(node, exp.Identifier):
        return str(node.this), bool(node.quoted)
    return None


def _parse(statement: str) -> exp.Expression | None:
    try:
        parsed = sqlglot.parse_one(statement.strip().rstrip(";"), read=OracleConversion)
    except (SqlglotError, ValueError, RecursionError):
        return None
    if not isinstance(parsed, exp.Expression) or isinstance(parsed, exp.Command):
        return None
    return parsed


def _cte_names(parsed: exp.Expression) -> set[str]:
    names: set[str] = set()
    for cte in parsed.find_all(exp.CTE):
        alias = cte.args.get("alias")
        ident = _identifier(alias.this if isinstance(alias, exp.TableAlias) else None)
        if ident is not None:
            names.add(_normalize(*ident))
    return names


def _table_ref(table: exp.Table) -> ObjectRef | None:
    """名前の表（表関数・JSON_TABLE などは None）。DB link（`@`）は調べないので None。"""

    parts: list[tuple[str, bool]] = []
    for key in ("catalog", "db", "this"):
        value = table.args.get(key)
        if value is None:
            continue
        ident = _identifier(value)
        if ident is None:
            return None
        parts.append(ident)
    if not parts or any("@" in name for name, _quoted in parts):
        return None
    return ObjectRef(tuple(parts))


def referenced_objects(statement: str) -> list[ObjectRef] | None:
    """文が参照する名前の object（CTE の名前を除く。重複なし）。解析できなければ None。"""

    parsed = _parse(statement)
    if parsed is None:
        return None
    ctes = _cte_names(parsed)
    found: dict[str, ObjectRef] = {}
    for table in parsed.find_all(exp.Table):
        ref = _table_ref(table)
        if ref is None:
            continue
        if len(ref.parts) == 1 and ref.name in ctes:
            continue
        found.setdefault(ref.key, ref)
    return list(found.values())


def statement_object_names(statement: str) -> set[str]:
    """文に出る名前（引用の無い名前は大文字）。同じターンの前の DDL が触る object の判定に使う。"""

    names: set[str] = set()
    for match in _IDENTIFIER_TOKEN.finditer(statement):
        quoted, plain = match.group(1), match.group(2)
        names.add(quoted if quoted is not None else plain.upper())
    return names


# ---- 静的な検査（SELECT） ----


@dataclass(slots=True)
class _Source:
    """scope の 1 つの参照先。`columns` が None なら列を確かめられない。"""

    label: str
    columns: frozenset[str] | None
    display_columns: tuple[str, ...] = ()


def _scope_output_columns(scope: Scope) -> tuple[str, ...] | None:
    """副問い合わせ・CTE の出力の列。`*` を含むなど確かめられなければ None。"""

    expression = scope.expression
    selects = getattr(expression, "selects", None)
    if not isinstance(selects, list):
        return None
    names: list[str] = []
    for item in selects:
        if isinstance(item, exp.Star) or (
            isinstance(item, exp.Column) and isinstance(item.this, exp.Star)
        ):
            return None
        if isinstance(item, exp.Alias):
            ident = _identifier(item.args.get("alias"))
        elif isinstance(item, exp.Column):
            ident = _identifier(item.this)
        else:
            # 別名の無い式の列は外から名前で参照できない（`*` で読むだけ）。
            continue
        if ident is not None:
            names.append(_normalize(*ident))
    return tuple(names)


def _source_alias(name: str, source: exp.Table | Scope) -> str:
    if isinstance(source, exp.Table):
        alias = source.args.get("alias")
        ident = _identifier(alias.this if isinstance(alias, exp.TableAlias) else None)
        return (
            _normalize(*ident)
            if ident
            else _normalize(*(_identifier(source.this) or (name, False)))
        )
    parent = source.expression.parent
    alias = parent.args.get("alias") if parent is not None else None
    ident = _identifier(alias.this if isinstance(alias, exp.TableAlias) else None)
    return _normalize(*ident) if ident else name.upper()


def _scope_sources(
    scope: Scope, columns_of: Callable[[ObjectRef], tuple[str, ...] | None]
) -> dict[str, _Source]:
    sources: dict[str, _Source] = {}
    for name, source in scope.sources.items():
        alias = _source_alias(name, source)
        if isinstance(source, exp.Table):
            ref = _table_ref(source)
            described = columns_of(ref) if ref is not None else None
            sources[alias] = _Source(
                label=ref.key if ref is not None else alias,
                columns=frozenset(described) if described is not None else None,
                display_columns=described or (),
            )
        elif isinstance(source, Scope):
            outputs = _scope_output_columns(source)
            sources[alias] = _Source(
                label=alias,
                columns=frozenset(outputs) if outputs is not None else None,
                display_columns=outputs or (),
            )
        else:
            sources[alias] = _Source(label=alias, columns=None)
    return sources


def _select_aliases(scope: Scope) -> set[str]:
    aliases: set[str] = set()
    for item in getattr(scope.expression, "selects", None) or []:
        if isinstance(item, exp.Alias):
            ident = _identifier(item.args.get("alias"))
            if ident is not None:
                aliases.add(_normalize(*ident))
    return aliases


def static_unknown_columns(
    statement: str, columns_of: Callable[[ObjectRef], tuple[str, ...] | None]
) -> list[UnknownColumn]:
    """SELECT の文で、調べた object・副問い合わせの出力に無い列（確かめられる参照だけ）。"""

    parsed = _parse(statement)
    if parsed is None or not isinstance(parsed, exp.Query):
        return []
    # 列を作る構文（PIVOT・MATCH_RECOGNIZE）は出力の列を確かめられないので、静的には検査しない。
    if any(parsed.find_all(exp.Pivot, exp.MatchRecognize)):
        return []
    try:
        root = build_scope(parsed)
    except (SqlglotError, ValueError, RecursionError):
        return []
    if root is None:
        return []
    source_cache: dict[int, dict[str, _Source]] = {}

    def sources_of(scope: Scope) -> dict[str, _Source]:
        key = id(scope)
        if key not in source_cache:
            source_cache[key] = _scope_sources(scope, columns_of)
        return source_cache[key]

    unknown: dict[str, UnknownColumn] = {}
    for scope in root.traverse():
        aliases = _select_aliases(scope)
        for column in scope.columns:
            ident = _identifier(column.this)
            if ident is None:
                continue
            name = _normalize(*ident)
            if not ident[1] and name in ORACLE_PSEUDO_COLUMNS:
                continue
            if column.args.get("db") is not None or column.args.get("catalog") is not None:
                continue
            qualifier = _identifier(column.args.get("table"))
            chain: list[Scope] = []
            current: Scope | None = scope
            while current is not None:
                chain.append(current)
                current = current.parent
            if qualifier is not None:
                target = _normalize(*qualifier)
                resolved = next(
                    (sources_of(item)[target] for item in chain if target in sources_of(item)),
                    None,
                )
                # 別名でも表でもない修飾（順序・パッケージ・型の属性など）は確かめない。
                if resolved is None or resolved.columns is None or name in resolved.columns:
                    continue
                label = f"{qualifier[0]}.{ident[0]}"
                unknown.setdefault(
                    label.upper(),
                    UnknownColumn(name, ((resolved.label, resolved.display_columns),)),
                )
                continue
            if name in aliases:
                continue
            candidates: list[_Source] = []
            for item in chain:
                candidates.extend(sources_of(item).values())
            if not candidates or any(source.columns is None for source in candidates):
                continue
            if any(name in (source.columns or ()) for source in candidates):
                continue
            unknown.setdefault(
                name,
                UnknownColumn(
                    name,
                    tuple(
                        (source.label, source.display_columns)
                        for source in sources_of(scope).values()
                    ),
                ),
            )
    return list(unknown.values())


def _oracle_invalid_identifier(message: str) -> str:
    match = _ORA_INVALID_IDENTIFIER.search(message)
    return match.group(2) if match else ""


def check_statement(
    statement: str,
    describer: SqlObjectDescriber,
    *,
    select_only: bool,
    changed_names: Iterable[str] = (),
) -> StatementColumnCheck | None:
    """1 文の参照する object と列を確かめる。確かめない文は None。

    `changed_names` は同じターンの前の文（DDL など）に出る名前で、それを参照する文は実行の前に
    object が無い・列が違うので確かめない。
    """

    refs = referenced_objects(statement)
    if refs is None:
        refs = []
    changed = set(changed_names)
    if any(ref.name in changed for ref in refs):
        return None
    check = StatementColumnCheck()
    described: dict[str, tuple[str, ...] | None] = {}
    for ref in refs:
        reference = ref.reference_sql()
        columns = describer.describe(reference) if reference is not None else None
        described[ref.key] = columns
        if columns is not None:
            check.objects[ref.key] = columns
        elif reference is not None and describer.available:
            check.undescribed.append(ref.key)

    def columns_of(ref: ObjectRef) -> tuple[str, ...] | None:
        return described.get(ref.key)

    static = static_unknown_columns(statement, columns_of) if select_only else []
    oracle_error = describer.parse_error(statement.strip().rstrip(";"))
    check.oracle_error = oracle_error
    if oracle_error is None:
        check.unknown = static
        return check
    identifier = _oracle_invalid_identifier(oracle_error)
    if not identifier:
        # Oracle の解析が通った（静的な検査の誤り）か、列の誤り以外のエラー。止めない。
        return check
    unknown = list(static)
    if all(item.column != _normalize(identifier, True) for item in unknown):
        unknown.append(UnknownColumn(identifier, tuple(check.objects.items())))
    check.unknown = unknown
    return check


# ---- 文言・文脈 ----


def _column_list(columns: Sequence[str], limit: int) -> str:
    shown = ", ".join(columns[:limit])
    if len(columns) > limit:
        shown += f" ほか {len(columns) - limit} 列"
    return shown or "(列なし)"


def unknown_column_reason(check: StatementColumnCheck) -> str:
    """実行を止める理由（「列が存在しません: X（<object> の列: …）」）。"""

    names = list(dict.fromkeys(item.column for item in check.unknown))
    sources: dict[str, tuple[str, ...]] = {}
    for item in check.unknown:
        for label, columns in item.sources:
            sources.setdefault(label, columns)
    detail = "／".join(
        f"{label} の列: {_column_list(columns, REASON_COLUMN_LIMIT)}"
        for label, columns in sources.items()
    )
    return f"列が存在しません: {', '.join(names)}" + (f"（{detail}）" if detail else "")


def undescribed_warning(key: str) -> str:
    return f"{key} の列を確かめられませんでした（存在しないか、読む権限がありません）。"


def object_columns_context(
    objects: Mapping[str, Sequence[str]], undescribed: Iterable[str] = ()
) -> list[str]:
    """直す指示・エラーの分析の文脈に渡す、参照する object の実際の列の行。"""

    lines = [
        f"- {key}: {_column_list(list(columns), CONTEXT_COLUMN_LIMIT)}"
        for key, columns in objects.items()
    ]
    lines.extend(f"- {key}: (存在しないか、読む権限がありません)" for key in undescribed)
    return lines
