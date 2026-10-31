"""NL2SQL / 管理画面で共有する Oracle owner-qualified object identity。"""

from __future__ import annotations

import re
from dataclasses import dataclass

_SIMPLE_IDENTIFIER = re.compile(r"^[A-Z][A-Z0-9_$#]{0,127}$")
_MAX_CANONICAL_PART_BYTES = 128


def _split_identifier_parts(value: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    in_double = False
    index = 0
    raw = str(value or "").strip()
    while index < len(raw):
        char = raw[index]
        if char == '"':
            buffer.append(char)
            if in_double and index + 1 < len(raw) and raw[index + 1] == '"':
                buffer.append(raw[index + 1])
                index += 2
                continue
            in_double = not in_double
            index += 1
            continue
        if char == "." and not in_double:
            part = "".join(buffer).strip()
            if part:
                parts.append(part)
            buffer = []
            index += 1
            continue
        buffer.append(char)
        index += 1
    if in_double:
        raise ValueError(f"{value}: Oracle 識別子が不正です。")
    tail = "".join(buffer).strip()
    if tail:
        parts.append(tail)
    return parts


def _unquote_identifier_part(value: str, original: str) -> str:
    if not (len(value) >= 2 and value[0] == value[-1] == '"'):
        raise ValueError(f"{original}: Oracle 識別子が不正です。")
    inner = value[1:-1]
    chars: list[str] = []
    index = 0
    while index < len(inner):
        char = inner[index]
        if char == '"':
            if index + 1 < len(inner) and inner[index + 1] == '"':
                chars.append('"')
                index += 2
                continue
            raise ValueError(f"{original}: Oracle 識別子が不正です。")
        chars.append(char)
        index += 1
    normalized = "".join(chars)
    if not normalized or "\x00" in normalized:
        raise ValueError(f"{original}: Oracle 識別子が不正です。")
    return normalized


def normalize_object_part(value: str) -> str:
    """规范化一个 Oracle owner/object 标识符。"""

    raw = str(value or "").strip()
    if '"' in raw:
        return _unquote_identifier_part(raw, str(value))
    if not raw or "\x00" in raw:
        raise ValueError(f"{value}: Oracle 識別子が不正です。")
    normalized = raw.upper()
    if _SIMPLE_IDENTIFIER.fullmatch(normalized):
        return normalized
    return raw


def format_object_part(value: str) -> str:
    """Catalog/API 表示用に、必要な部分だけ二重引用符で囲む。"""

    raw = str(value or "").strip()
    normalized = normalize_object_part(raw) if raw.startswith('"') else raw
    if not normalized or "\x00" in normalized:
        raise ValueError(f"{value}: Oracle 識別子が不正です。")
    if _SIMPLE_IDENTIFIER.fullmatch(normalized):
        return normalized
    return '"' + normalized.replace('"', '""') + '"'


def canonical_object_part(value: str) -> str:
    """API 入力の owner / object / column 1 部分を、保存・比較・SQL 用の canonical token にする。

    - `"Mixed_Case"` のように引用された値は、引用符を外した名前を大文字小文字を保って使う。
    - 引用されていない値は Oracle と同じく大文字として解釈する（`orders` → `ORDERS`）。
      Oracle の非引用識別子にならない値（`DEPT@REMOTE`、`my table` 等）は、推測で引用せず拒否する。
    - 結果は `format_object_part` と同じ規則で、引用が必要な名前だけ `"..."` で囲む。
      引用が不要な名前は従来の保存キー（大文字）と同じ値になる。
    - Oracle の識別子に使えない `"`・NUL・制御文字を含む名前と、token が 128 byte を超える
      名前は拒否する。token は二重引用符を含まないため、そのまま SQL 識別子として埋め込める。
    """

    raw = str(value or "").strip()
    if not raw.startswith('"') and not _SIMPLE_IDENTIFIER.fullmatch(raw.upper()):
        raise ValueError(f"{value}: Oracle 識別子が不正です。")
    name = normalize_object_part(raw)
    if (
        not name
        or '"' in name
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name)
        or name != name.strip()
    ):
        raise ValueError(f"{value}: Oracle 識別子が不正です。")
    token = format_object_part(name)
    if len(token.encode("utf-8")) > _MAX_CANONICAL_PART_BYTES:
        raise ValueError(f"{value}: Oracle 識別子が長すぎます（引用符を含めて 128 バイト以内）。")
    return token


def canonical_qualified_name(value: str) -> str:
    """`OWNER.OBJECT`（各部は引用可）を canonical token の単純連結にする。"""

    parts = _split_identifier_parts(value)
    if len(parts) != 2:
        raise ValueError(f"{value}: OWNER.OBJECT 形式で指定してください。")
    return f"{canonical_object_part(parts[0])}.{canonical_object_part(parts[1])}"


def object_name_tokens(value: str) -> list[str]:
    """`OBJECT` / `OWNER.OBJECT`（各部は引用可）を、部分ごとの比較用 token に分ける。

    `normalize_object_part` → `format_object_part` と同じ規則で、引用名は大文字小文字を保ち、
    引用なしは大文字として扱う。検証（無効名の拒否）はせず、照合キーを作るためだけに使う。
    引用符の対応が壊れている値は `ValueError`。
    """

    return [
        format_object_part(normalize_object_part(part)) for part in _split_identifier_parts(value)
    ]


def sql_identifier_token(name: str, *, quoted: bool) -> str:
    """SQL parser が返した識別子 1 部分（引用符を外した値と引用の有無）を canonical token にする。

    Oracle と同じく、引用されていない識別子は大文字として、引用された識別子は書かれたとおりに
    解釈する。`SALES."Mixed_Case"` の `Mixed_Case` を大文字化すると、大文字の同名表
    `SALES.MIXED_CASE` と区別できない。
    """

    raw = str(name or "")
    return format_object_part(raw if quoted else raw.upper())


def is_unquoted_object_part(name: str) -> bool:
    """カタログ上の名前が、引用なしの Oracle 識別子（`[A-Z][A-Z0-9_$#]*`）で書けるか。"""

    return bool(_SIMPLE_IDENTIFIER.fullmatch(str(name or "")))


def object_match_key(value: str) -> str:
    """入力の `OBJECT` / `OWNER.OBJECT` / `OWNER.OBJECT.COLUMN`（各部は引用可）の照合キー。

    `object_name_tokens` の単純連結。引用符が壊れた値は例外にせず、前後の空白を除いて返す
    （照合に失敗させるだけで、別の object に一致させない）。
    """

    try:
        return ".".join(object_name_tokens(value))
    except ValueError:
        return str(value or "").strip()


def catalog_match_key(*parts: str) -> str:
    """カタログ上の owner / object / column 名（引用符なし・大文字小文字を保持）の照合キー。

    引用が必要な部分だけ `"..."` で囲む（`format_object_part`）。`object_match_key` と同じ
    規則のため、入力とカタログを突き合わせられる。不正な値は例外にせず、そのまま連結する。
    """

    try:
        return ".".join(format_object_part(part) for part in parts)
    except ValueError:
        return ".".join(str(part or "") for part in parts)


def split_match_key(key: str) -> list[str]:
    """`object_match_key` / `catalog_match_key` の結果を部分 token に分ける。

    引用名の中の `.`（`SALES."A.B"`）で分けない。引用符が壊れた値は `.` で単純に分ける。
    """

    try:
        return _split_identifier_parts(key)
    except ValueError:
        return [part for part in str(key or "").split(".") if part]


def object_part_name(value: str) -> str:
    """入力 1 部分（`"Mixed_Case"` / `orders`）または照合 token を、カタログ上の名前にする。

    `normalize_object_part` と同じ規則（引用名は引用符を外して大文字小文字を保ち、引用なしは
    大文字）。SQL に `"..."` で埋め込む名前や、カタログの名前との比較に使う。不正な値は例外に
    せず前後の空白を除いて返す（どの object にも一致しない）。
    """

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return normalize_object_part(raw)
    except ValueError:
        return raw


@dataclass(frozen=True, slots=True)
class OracleObjectIdentity:
    """Owner-aware 的只读对象身份。"""

    owner: str
    object_name: str

    @property
    def qualified_name(self) -> str:
        return qualified_object_name(self.owner, self.object_name)

    @property
    def quoted_name(self) -> str:
        return f'"{self.owner}"."{self.object_name}"'


def parse_object_identity(
    value: str,
    *,
    default_owner: str = "",
) -> OracleObjectIdentity:
    """`OBJECT` 或 `OWNER.OBJECT` を解析し、必ず owner 付きで返す。"""

    parts = _split_identifier_parts(value)
    if len(parts) == 1 and default_owner:
        return OracleObjectIdentity(
            owner=normalize_object_part(default_owner),
            object_name=normalize_object_part(parts[0]),
        )
    if len(parts) == 2:
        return OracleObjectIdentity(
            owner=normalize_object_part(parts[0]),
            object_name=normalize_object_part(parts[1]),
        )
    if len(parts) == 1 and '"' in parts[0]:
        normalize_object_part(parts[0])
    raise ValueError(f"{value}: OWNER.OBJECT 形式で指定してください。")


def qualified_object_name(owner: str, object_name: str) -> str:
    """Catalog metadata から canonical `OWNER.OBJECT` key を作る。"""

    return f"{format_object_part(owner)}.{format_object_part(object_name)}"


__all__ = [
    "OracleObjectIdentity",
    "canonical_object_part",
    "canonical_qualified_name",
    "catalog_match_key",
    "format_object_part",
    "is_unquoted_object_part",
    "normalize_object_part",
    "object_match_key",
    "object_name_tokens",
    "object_part_name",
    "parse_object_identity",
    "qualified_object_name",
    "split_match_key",
    "sql_identifier_token",
]
