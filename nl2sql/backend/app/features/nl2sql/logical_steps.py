"""SQL 構造から「業務者向け説明 + 技術詳細」を決定論で組み立てる。

処理手順(logical steps)と SQL 論理構造の両方で共有する。追加の LLM 呼び出しは行わず、
語彙マップと単純なパターン一致だけで変換する。読み取れない断片は無理に言い換えず汎用文へ
落とし、生の SQL 断片は technical 側に必ず残す(業務者は business、技術者は technical を読む)。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .models import Nl2SqlLogicalStep, Nl2SqlLogicalStructureItem

LabelResolver = Callable[[str], str]

# 集計関数 -> 業務語。SQL 断片は technical 側に残すのでここは平易な日本語だけにする。
_AGGREGATION_BUSINESS: dict[str, str] = {
    "COUNT": "件数を数えます",
    "SUM": "合計を計算します",
    "AVG": "平均を求めます",
    "MIN": "最小値を求めます",
    "MAX": "最大値を求めます",
}

# SQL 操作 -> 業務語(summary 用の体言止め)。
_OPERATION_BUSINESS: dict[str, str] = {
    "SELECT": "一覧の取得",
    "GROUP BY": "集計",
    "ORDER BY": "並べ替え",
    "WITH": "中間集計の利用",
}

# 比較演算子 -> 業務語(値の後ろに付ける)。長い演算子から順に判定する。
_COMPARISON_BUSINESS: list[tuple[str, str]] = [
    (">=", "以上の"),
    ("<=", "以下の"),
    ("<>", "以外の"),
    ("!=", "以外の"),
    (">", "より大きい"),
    ("<", "未満の"),
    ("=", "と一致する"),
]

_FILTER_FALLBACK = "指定された条件で絞り込みます"
_FILTER_PHRASE_FALLBACK = "指定条件に合う"
_MAX_INLINE_ITEMS = 3


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _identifier_tail(value: str) -> str:
    """`"D"."DEPARTMENT_ID"` のような参照から末尾の識別子だけを取り出す。"""

    text = _clean(value).replace('"', "")
    text = text.split()[0] if text.split() else text
    return text.rsplit(".", 1)[-1]


def _default_label(value: str) -> str:
    return _identifier_tail(value) or _clean(value)


def _normalize_label(value: str) -> str:
    """列/表コメント由来のラベルは文中へ埋め込むので句読点・改行を落とす。"""

    text = " ".join(_clean(value).split())
    return text.strip("。．.、,；;:　 ")


def _resolve(resolver: LabelResolver | None, value: str) -> str:
    """ラベル解決は表示のためだけなので、失敗しても物理名へ静かに縮退する。"""

    fallback = _default_label(value)
    if resolver is None:
        return fallback
    try:
        resolved = _normalize_label(resolver(value))
    except Exception:
        return fallback
    return resolved or fallback


def _join_labels(labels: Sequence[str], *, separator: str = "、") -> str:
    unique: list[str] = []
    for label in labels:
        cleaned = _normalize_label(label)
        if cleaned and cleaned not in unique:
            unique.append(cleaned)
    if len(unique) > _MAX_INLINE_ITEMS:
        return separator.join(unique[:_MAX_INLINE_ITEMS]) + "ほか"
    return separator.join(unique)


def _split_columns(clause: str) -> list[str]:
    return [part.strip() for part in _clean(clause).split(",") if part.strip()]


_BETWEEN_WORD = re.compile(r"between\b", re.IGNORECASE)
_CONNECTOR_WORD = re.compile(r"\s+(and|or)\s+", re.IGNORECASE)


def _split_conditions(clause: str) -> list[tuple[str, str]]:
    """WHERE 句を AND / OR で素朴に分割する(括弧内と BETWEEN の AND は分割しない)。

    照合は位置を指定して行い(`text[index:]` を毎文字複製しない)、AND / OR は空白の連続の
    先頭だけで照合する。空白の連続の途中から読み直すと O(n²) になるため(#1646)。
    """

    text = _clean(clause)
    conditions: list[tuple[str, str]] = []
    buffer: list[str] = []
    connector = ""
    depth = 0
    index = 0
    pending_between = False
    while index < len(text):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if depth == 0:
            previous = text[index - 1] if index > 0 else ""
            if not (previous.isalnum() or previous == "_") and _BETWEEN_WORD.match(text, index):
                pending_between = True
            match = None
            if char.isspace() and not previous.isspace():
                match = _CONNECTOR_WORD.match(text, index)
            if match:
                if pending_between and match.group(1).lower() == "and":
                    # BETWEEN a AND b の AND は条件の区切りではない。
                    pending_between = False
                    buffer.append(text[index : match.end()])
                    index = match.end()
                    continue
                conditions.append((connector, "".join(buffer).strip()))
                connector = "かつ" if match.group(1).lower() == "and" else "または"
                buffer = []
                index = match.end()
                continue
        buffer.append(char)
        index += 1
    tail = "".join(buffer).strip()
    if tail:
        conditions.append((connector, tail))
    return [(connector, value) for connector, value in conditions if value]


def _format_value(value: str) -> str:
    text = _clean(value)
    if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
        return text[1:-1].replace("%", "")
    return text.replace('"', "")


# 前置きだけを正規表現で照合し、値は文字列の操作で取り出す。`(.+?)\s+AND\s+(.+)$` のように
# 空白と任意の文字が重なる式は、空白の長い連続で O(n²) に後戻りするため(#1646)。
_BETWEEN_PREFIX = re.compile(r"(NOT\s+)?BETWEEN\s", re.IGNORECASE)
_BETWEEN_AND = re.compile(r"\sAND\s", re.IGNORECASE)
_LIKE_PREFIX = re.compile(r"(NOT\s+)?LIKE\s", re.IGNORECASE)


def _between_bounds(rest: str) -> tuple[bool, str, str] | None:
    """`[NOT] BETWEEN 下限 AND 上限` を (否定か, 下限, 上限) に分ける。形が違えば None。"""

    prefix = _BETWEEN_PREFIX.match(rest)
    if not prefix:
        return None
    body = rest[prefix.end() :]
    separator = _BETWEEN_AND.search(body)
    if not separator:
        return None
    low = body[: separator.start()].strip()
    high = body[separator.end() :].strip()
    # 以前の `(.+?)` / `(.+)` と同じく、改行をまたぐ値と空の値は読み下さない。
    if not low or not high or "\n" in low or "\n" in high:
        return None
    return bool(prefix.group(1)), low, high


def _strip_outer_parens(text: str) -> str:
    """条件全体を包む括弧だけを外す(内側の括弧は保持する)。"""

    value = _clean(text)
    if not value.startswith("(") or not value.endswith(")"):
        return value
    depth = 0
    for index, char in enumerate(value):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and index != len(value) - 1:
                return value
    return value[1:-1].strip()


def _condition_business(
    condition: str,
    column_label: LabelResolver | None,
    *,
    depth: int = 0,
) -> str:
    """単純な `列 演算子 値` だけを業務語へ言い換える(推測で意味を作らない)。"""

    text = _clean(condition)
    if not text:
        return ""
    grouped = _strip_outer_parens(text)
    if grouped != text and depth < 2:
        # (A OR B) のような括弧グループは 1 段だけ展開して読み下す。
        return _conditions_phrase(grouped, column_label, depth=depth + 1) or _FILTER_PHRASE_FALLBACK
    column_pattern = r'^("[^"]+"|[A-Za-z_][\w$#]*)((?:\.(?:"[^"]+"|[A-Za-z_][\w$#]*))*)\s*'
    match = re.match(column_pattern, text)
    if not match:
        return _FILTER_PHRASE_FALLBACK
    column_ref = f"{match.group(1)}{match.group(2)}"
    label = _resolve(column_label, column_ref)
    rest = text[match.end() :].strip()
    upper = rest.upper()

    if upper.startswith("IS NOT NULL"):
        return f"{label}が設定されている"
    if upper.startswith("IS NULL"):
        return f"{label}が未設定の"
    between = _between_bounds(rest)
    if between:
        negated, low_text, high_text = between
        low = _format_value(low_text)
        high = _format_value(high_text)
        suffix = "の範囲外の" if negated else "の範囲の"
        return f"{label}が{low}〜{high}{suffix}"
    in_match = re.match(r"^(NOT\s+)?IN\s*\((.+)\)$", rest, re.IGNORECASE | re.DOTALL)
    if in_match:
        values = _join_labels(
            [_format_value(part) for part in in_match.group(2).split(",")], separator="・"
        )
        suffix = "のいずれでもない" if in_match.group(1) else "のいずれかの"
        return f"{label}が{values}{suffix}"
    like_match = _LIKE_PREFIX.match(rest)
    like_value = rest[like_match.end() :].strip() if like_match else ""
    if like_match and like_value and "\n" not in like_value:
        value = _format_value(like_value)
        suffix = "を含まない" if like_match.group(1) else "を含む"
        return f"{label}が{value}{suffix}"
    for operator, word in _COMPARISON_BUSINESS:
        if rest.startswith(operator):
            value = _format_value(rest[len(operator) :])
            if not value:
                return _FILTER_PHRASE_FALLBACK
            return f"{label}が{value}{word}"
    return _FILTER_PHRASE_FALLBACK


def _conditions_phrase(
    clause: str,
    column_label: LabelResolver | None,
    *,
    depth: int = 0,
) -> str:
    conditions = _split_conditions(clause)
    if not conditions:
        return ""
    phrases: list[str] = []
    for index, (connector, condition) in enumerate(conditions[:_MAX_INLINE_ITEMS]):
        phrase = (
            _condition_business(condition, column_label, depth=depth) or _FILTER_PHRASE_FALLBACK
        )
        phrases.append(phrase if index == 0 else f"、{connector}{phrase}")
    suffix = "ほか" if len(conditions) > _MAX_INLINE_ITEMS else ""
    return "".join(phrases) + suffix


def _filters_business(clause: str, column_label: LabelResolver | None) -> str:
    phrase = _conditions_phrase(clause, column_label)
    if not phrase:
        return _FILTER_FALLBACK
    return f"{phrase}行に絞り込みます"


def _join_business(
    clause: str,
    table_label: LabelResolver | None,
    column_label: LabelResolver | None,
) -> str:
    text = _clean(clause)
    table_match = re.search(
        r'\bjoin\s+("[^"]+"|[\w$#]+)((?:\.(?:"[^"]+"|[\w$#]+))*)', text, re.IGNORECASE
    )
    table = (
        _resolve(table_label, f"{table_match.group(1)}{table_match.group(2)}")
        if table_match
        else "関連データ"
    )
    on_match = re.search(
        r'\bon\s+("[^"]+"|[\w$#]+)((?:\.(?:"[^"]+"|[\w$#]+))*)\s*=\s*("[^"]+"|[\w$#]+)((?:\.(?:"[^"]+"|[\w$#]+))*)',
        text,
        re.IGNORECASE,
    )
    if on_match:
        left = _resolve(column_label, f"{on_match.group(1)}{on_match.group(2)}")
        right = _resolve(column_label, f"{on_match.group(3)}{on_match.group(4)}")
        key = left if left == right else f"{left}と{right}"
        return f"{table}を{key}で突き合わせます"
    return f"{table}を突き合わせます"


def _aggregation_business(item: str) -> str:
    name = _clean(item).upper()
    for function, business in _AGGREGATION_BUSINESS.items():
        if name.startswith(function):
            return business
    return f"{_clean(item)} で集計します"


def _group_by_business(clause: str, column_label: LabelResolver | None) -> str:
    labels = [_resolve(column_label, column) for column in _split_columns(clause)]
    subject = _join_labels(labels, separator="・")
    return f"{subject}ごとに集計します" if subject else "指定された単位で集計します"


def _order_by_business(clause: str, column_label: LabelResolver | None) -> str:
    phrases: list[str] = []
    for column in _split_columns(clause)[:_MAX_INLINE_ITEMS]:
        descending = re.search(r"\bdesc\b", column, re.IGNORECASE) is not None
        reference = re.sub(r"\b(asc|desc|nulls\s+(first|last))\b", "", column, flags=re.IGNORECASE)
        stripped = reference.strip()
        # ORDER BY 2 のような列番号指定は業務名に化けさせず「N 番目の列」と表現する。
        label = f"{stripped}番目の列" if stripped.isdigit() else _resolve(column_label, reference)
        phrases.append(f"{label}の{'降順' if descending else '昇順'}")
    subject = _join_labels(phrases, separator="・")
    return f"{subject}で並べ替えます" if subject else "指定された順序で並べ替えます"


def _operation_words(structure: Mapping[str, Any]) -> list[str]:
    operations = [_clean(item).upper() for item in structure.get("operations") or []]
    if not operations:
        # 生成側 analysis のように operations を持たない構造では SELECT を基点にする。
        operations = ["SELECT"]
    # 集計・並べ替えは句の有無からも補う(operations に GROUP BY が無い SQL でも業務語は出す)。
    if (
        structure.get("group_by") or structure.get("aggregations")
    ) and "GROUP BY" not in operations:
        operations.append("GROUP BY")
    if structure.get("order_by") and "ORDER BY" not in operations:
        operations.append("ORDER BY")
    words: list[str] = []
    for operation in operations:
        word = _OPERATION_BUSINESS.get(operation)
        if word and word not in words:
            words.append(word)
    return words


def _summary_business(structure: Mapping[str, Any], table_labels: Sequence[str] | None) -> str:
    subject = _join_labels(table_labels or [])
    words = _operation_words(structure)
    action = "・".join(words) if words else "データの参照"
    return f"{subject or '対象データ'}を対象に、{action}を行います。"


def build_business_explanation(
    structure: Mapping[str, Any],
    table_labels: Sequence[str] | None = None,
) -> str:
    """reverse 応答の説明文を業務者向け + 技術要約の併記にする。"""

    business = _summary_business(structure, table_labels)
    operations = [_clean(item) for item in structure.get("operations") or [] if _clean(item)]
    if not operations:
        return business
    return f"{business}(SQL 構造: {', '.join(operations)})"


def build_logical_steps(
    structure: Mapping[str, Any],
    *,
    limit: int | None = None,
    table_labels: Sequence[str] | None = None,
    table_label: LabelResolver | None = None,
    column_label: LabelResolver | None = None,
) -> list[Nl2SqlLogicalStep]:
    """処理手順を business(業務者向け)/ technical(技術者向け)併記で組み立てる。

    technical は既存の `logical_steps` 文字列と同じ表記にし、UI では 2 行併記で表示する。
    """

    steps: list[Nl2SqlLogicalStep] = []
    summary = _clean(structure.get("summary"))
    if summary:
        steps.append(
            Nl2SqlLogicalStep(
                kind="summary",
                business=_summary_business(structure, table_labels),
                technical=summary,
            )
        )
    for item in list(structure.get("filters") or [])[:_MAX_INLINE_ITEMS]:
        steps.append(
            Nl2SqlLogicalStep(
                kind="filter",
                business=_filters_business(str(item), column_label),
                technical=f"条件: {item}",
            )
        )
    for item in list(structure.get("joins") or [])[:_MAX_INLINE_ITEMS]:
        steps.append(
            Nl2SqlLogicalStep(
                kind="join",
                business=_join_business(str(item), table_label, column_label),
                technical=f"結合: {item}",
            )
        )
    for item in list(structure.get("aggregations") or [])[:_MAX_INLINE_ITEMS]:
        steps.append(
            Nl2SqlLogicalStep(
                kind="aggregation",
                business=_aggregation_business(str(item)),
                technical=f"集計: {item}",
            )
        )
    for item in list(structure.get("group_by") or [])[:_MAX_INLINE_ITEMS]:
        steps.append(
            Nl2SqlLogicalStep(
                kind="group_by",
                business=_group_by_business(str(item), column_label),
                technical=f"グループ化: {item}",
            )
        )
    for item in list(structure.get("order_by") or [])[:_MAX_INLINE_ITEMS]:
        steps.append(
            Nl2SqlLogicalStep(
                kind="order_by",
                business=_order_by_business(str(item), column_label),
                technical=f"並び替え: {item}",
            )
        )
    if limit is not None and limit > 0:
        steps.append(
            Nl2SqlLogicalStep(
                kind="limit",
                business=f"先頭 {limit} 件だけ取り出します",
                technical=f"件数制限: 上位{limit}件",
            )
        )
    return [step for step in steps if step.business or step.technical]


def build_logical_structure_items(
    structure: Mapping[str, Any],
    *,
    table_labels: Sequence[str] | None = None,
    table_label: LabelResolver | None = None,
    column_label: LabelResolver | None = None,
) -> list[Nl2SqlLogicalStructureItem]:
    """SQL 論理構造を business / technical 併記の項目一覧にする。"""

    items: list[Nl2SqlLogicalStructureItem] = []
    summary = _clean(structure.get("summary"))
    if summary:
        items.append(
            Nl2SqlLogicalStructureItem(
                kind="summary",
                business=_summary_business(structure, table_labels),
                technical=summary,
            )
        )
    statement_type = _clean(structure.get("statement_type"))
    if statement_type:
        items.append(
            Nl2SqlLogicalStructureItem(
                kind="statement",
                business=(
                    "データを取り出すだけの参照 SQL です"
                    if statement_type.upper() in {"SELECT", "WITH"}
                    else "データを変更する可能性のある SQL です"
                ),
                technical=statement_type,
            )
        )
    operations = [str(item) for item in structure.get("operations") or []]
    if operations:
        items.append(
            Nl2SqlLogicalStructureItem(
                kind="operations",
                business="・".join(_operation_words(structure)) or "データの参照",
                technical="; ".join(operations),
            )
        )
    sections: list[tuple[str, list[str], Callable[[str], str]]] = [
        (
            "filters",
            [str(item) for item in structure.get("filters") or []],
            lambda value: _filters_business(value, column_label),
        ),
        (
            "joins",
            [str(item) for item in structure.get("joins") or []],
            lambda value: _join_business(value, table_label, column_label),
        ),
        (
            "group_by",
            [str(item) for item in structure.get("group_by") or []],
            lambda value: _group_by_business(value, column_label),
        ),
        (
            "order_by",
            [str(item) for item in structure.get("order_by") or []],
            lambda value: _order_by_business(value, column_label),
        ),
        (
            "aggregations",
            [str(item) for item in structure.get("aggregations") or []],
            _aggregation_business,
        ),
    ]
    for kind, values, to_business in sections:
        if not values:
            continue
        items.append(
            Nl2SqlLogicalStructureItem(
                kind=kind,
                business="、".join(to_business(value) for value in values[:_MAX_INLINE_ITEMS]),
                technical="; ".join(values),
            )
        )
    return items
