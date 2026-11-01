"""表・一覧の行と列の対応に反して、別の行・列の値を述べた文を見つける（#2659）。

根拠の表（HTML の表・Markdown の表・「名前：値」の行の並び・1 文の中で並べた「A は x、B は y」）は、値をその行の
区分・名前と、その列の見出しに対応させる。文がある行の区分・名前と、別の行にしか無い値を同じ所で結び付けていれば、
語と値が根拠のどこかにあっても、根拠の対応とは違う。語の一致・話題の一致では見分けられないので、表の構造（行・列）で
確かめる（表の事実の検証で、セルを行と列の見出しに結び付けて照らす考え方）。

値は、英数字だけのセル（コード・番号）と、先頭の列（行の区分・名前の列）以外のセル（日本語の値・単位付きの値）と、
そのセルの中のコード（「A-05-13-1／140 分」の「A-05-13-1」）。他の語の一部に現れやすい短い語（数字の無い 2 文字以下。
「あり」「なし」）は値にしない。

決定的な照合で、意味は見ない。誤って正しい文を外さないように、次のときだけ「対応しない」とする。

- 値のある行のどれにも、文が同じ所で述べた行の手がかり（その表の 1 行にだけあるセル。同じ行のコードなどの値も含む）
  が無く、
- 別の行の区分・名前（値でないセル）を、文が同じ所で述べている。別の行の値を同じ所で述べただけ
  （複数の行の値の列挙・範囲「254・253・300」「A101〜A103」）では疑わない。
- 見出しの行のセルと、表の複数の行にあるセルは、行を見分ける手がかりにしない。
- 列: 行は合っていても、表に見出しの行があり、文がその値の列の見出しを述べず、同じ行に値のある別の列の見出し
  （「改定前」の値に対する「改定後」）を同じ所で述べている。

「同じ所」は文の読点・句点で区切った区間で、値の無い区間（「区分Ⅱの場合、」）は続く区間と合わせて見る。
最終の検証（`evaluation.answer_validation`）と回答の生成の検査（`generation.grounded`）が同じ判定を使う。
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from rag_engine.parsing.content_labels import content_labels
from rag_engine.retrieval.character_forms import SYMBOL_FOLD, fold_width

# 値とみなすセル（英数字と区切りの「-」「_」「.」だけ。NFKC でローマ数字・全角も英数字にそろえる）。
_VALUE_CELL = re.compile(r"[0-9a-z]+(?:[-_.][0-9a-z]+)*")
_MAX_VALUE_CHARS = 16
# 区分・名前とみなすセルの長さ（短すぎると別の語の一部に当たり、長い説明の文は文が丸ごと写すことが少ない）。
_MIN_LABEL_CHARS = 2
_MAX_LABEL_CHARS = 40
# 先頭の列以外の英数字だけでないセルを値とみなす最小の長さ（数字を含まない語。短い語は別の語の一部に現れやすい）。
_MIN_WORD_VALUE_CHARS = 3
# セルの中のコード（区切りを含む英数字の並びか、英字と数字の両方を含む並び。数字だけの並びは数量・番号と
# 見分けられないので除く）。
_CODE_IN_CELL = re.compile(r"(?<![0-9a-z])(?:[0-9a-z]+(?:[-_.][0-9a-z]+)+|(?=[0-9a-z]*[a-z])(?=[0-9a-z]*[0-9])[0-9a-z]{2,})(?![0-9a-z])")
# 説明の文のセル（読点・句点を含む）。
_SENTENCE_PUNCTUATION = re.compile(r"[、。，．]")
# ローマ数字だけのセル（「Ⅱ」）。NFKC で「ii」になるが、値ではなく区分の名前として扱う。
_ROMAN_CELL = re.compile(r"[\u2160-\u217f]{1,4}")
# セルの照合に使う頭の部分（「01（通常）」「区分Ⅱ (旧)」の括弧・空白の前）。
# 数の桁区切りのカンマ（「12,000円」）は区切りにしない（#123）。
_DIGIT_GROUP_COMMA = r"(?<![0-9])[,，]|[,，](?![0-9]{3}(?![0-9]))"
_CELL_KEY_END = re.compile(rf"\s*[（(［\[〔【「『<＜]|\s{{2,}}|\s*(?:[、。;；]|{_DIGIT_GROUP_COMMA})")
_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr\s*>", re.IGNORECASE | re.DOTALL)
_CELL = re.compile(r"<t([dh])\b([^>]*)>(.*?)</t[dh]\s*>", re.IGNORECASE | re.DOTALL)
_SPAN = re.compile(r"""(row|col)span\s*=\s*["']?(\d+)""", re.IGNORECASE)
_TAG = re.compile(r"<[^<>]*>")
_TABLE_BLOCK = re.compile(r"<table\b.*?</table\s*>", re.IGNORECASE | re.DOTALL)
# Markdown の表の区切りの行（| --- | :---: |）。
_PIPE_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
# 「名前：値」の行の区切り（全角のコロン・縦棒・タブ・等号。半角のコロンは時刻「9:00」と区別する）。
_PAIR_SEPARATOR = re.compile(r"[：｜|\t＝]|(?<![0-9]):(?![0-9])|\s=\s")
# 「名前：値」の行の先頭の箇条書きの印（「- 」「・」「1. 」）。
_PAIR_MARKER = re.compile(r"^\s*(?:[-*]\s+|[・•]\s*|[0-9]{1,3}[.)．]\s+)")
# 1 文の中で並べた「A は x、B は y」の区間（名前と値の区切りは「は」・コロン・等号。値は短い語・数・コード）。
_PROSE_PAIR = re.compile(
    r"^\s*(?P<label>[^、。，,；;：:＝=]{2,30}?)\s*(?:は|：|:|＝|=)\s*(?P<value>[^、。，,；;]{1,20}?)\s*"
    r"(?:です|である|であり|とする|とします|とし|となる|となります|となり|で)?\s*$"
)
# 文の区間の区切り（読点・句点・改行・セミコロン）。
_SENTENCE_END = re.compile(r"[。！？!?\n]+")
_CLAUSE_END = re.compile(rf"[、；;]|{_DIGIT_GROUP_COMMA}")
# 値の照合の区間は、括弧の始まりでも区切る（「…です（参考：課長以上は…）」の括弧の中は別の事実。#123）。
_WINDOW_END = re.compile(rf"{_CLAUSE_END.pattern}|[（(]")
# 文の中で値の照合から除く所（根拠の ID・出典の括弧・頁の参照・強調の印）。
_CITATION = re.compile(r"【[^【】]*】|\[[^\[\]]*(?:ID|id)[^\[\]]*\]")
_CHUNK_ID = re.compile(r"(?<![0-9A-Za-z])[0-9A-Fa-f]{6,}(?:[0-9A-Fa-f:…‥\-]|\.{2,})*:[0-9]+(?![0-9A-Za-z])")
_PAGE_REF = re.compile(r"(?<![0-9A-Za-z])[pP]{1,2}\.\s*[0-9]+(?:\s*[-–〜~]\s*[0-9]+)?")
_EMPHASIS = re.compile(r"\*\*|__|`")
# 行の先頭の箇条書きの番号（「1. 」「(2)」「③」）。手順の番号を値として照合しない。
_LIST_MARKER = re.compile(r"(?m)^\s*(?:[0-9]{1,3}[.)．、]|[（(][0-9]{1,3}[）)]|[①-⑳])\s*")
# 漢字（「手順1」「第1」「1件」のように 1 文字の数字に続く・先立つ語）。
_IDEOGRAPH = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
# 数字・コードの内部の区切り（節番号・小数・英字付きの番号を一部の値として拾わない）。
_VALUE_JOINERS = frozenset("-_.‐‑‒–−")
# 読点などで短くしたセルの語を、別の複合語の一部分として照合しない。
_LABEL_LETTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u30a0-\u30ff]")


def _fold(text: str) -> str:
    """照合の形（NFKC・小文字・空白をまとめる）。ローマ数字「Ⅱ」は「ii」、全角の英数字は半角になる。"""
    return " ".join(unicodedata.normalize("NFKC", fold_width(text)).lower().split())


def _cell_key(cell: str) -> str:
    """セルの照合に使う頭の部分（括弧の補足・続く説明を除く）。"""
    folded = _fold(cell)
    match = _CELL_KEY_END.search(folded)
    return (folded[: match.start()] if match else folded).strip(" ・:：")


def _is_value(key: str) -> bool:
    # 種類を決めるときだけ横線をそろえる。セルの値と照合のキーは元の表記のまま残す。
    return 0 < len(key) <= _MAX_VALUE_CHARS and _VALUE_CELL.fullmatch(key.translate(SYMBOL_FOLD)) is not None


def _is_label(key: str) -> bool:
    return _MIN_LABEL_CHARS <= len(key) <= _MAX_LABEL_CHARS and not _is_value(key)


@dataclass(frozen=True)
class Table:
    """表の本文の行（見出しの行を除く）。各行はセルの照合の形の集合。``cells`` は照合の形 → 原文のセル。

    ``values`` は先頭の列以外の値のセルとセルの中のコード（英数字だけの値は ``_is_value`` で見る）、``labels`` は
    値の形でも区分の名前として扱うセル（ローマ数字だけの先頭の列）。``header`` は列の見出し（無ければ空）、``grid``
    は本文の行の列ごとのセル（照合の形。空欄は空文字）。
    """

    rows: tuple[frozenset[str], ...]
    cells: dict[str, str]
    values: frozenset[str] = frozenset()
    labels: frozenset[str] = frozenset()
    header: tuple[str, ...] = ()
    grid: tuple[tuple[str, ...], ...] = ()
    # セル（とセルの中のコード）の照合の形 → そのセルがある列。
    columns: dict[str, frozenset[int]] = field(default_factory=dict)

    def is_value(self, key: str) -> bool:
        return key not in self.labels and (_is_value(key) or key in self.values)

    def is_label(self, key: str) -> bool:
        return key in self.labels or _is_label(key)


def _take_carried(carried: dict[int, tuple[int, str]], column: int, cells: list[str]) -> int:
    """上の行の rowspan のセルを、その列から続く分だけ行に写し、次の列の位置を返す。"""
    while column in carried:
        remaining, text = carried[column]
        cells.append(text)
        if remaining <= 1:
            del carried[column]
        else:
            carried[column] = (remaining - 1, text)
        column += 1
    return column


def _html_rows(block: str) -> list[tuple[bool, list[str]]]:
    """HTML の表の行（見出しの行か、セルの本文）。rowspan / colspan は覆う行・列にセルを写す。"""
    carried: dict[int, tuple[int, str]] = {}  # 列 → (残りの行の数, 本文)
    rows: list[tuple[bool, list[str]]] = []
    for row_match in _ROW.finditer(block):
        cells: list[str] = []
        kinds: list[str] = []
        column = 0
        for kind, attributes, body in _CELL.findall(row_match.group(1)):
            column = _take_carried(carried, column, cells)
            text = html.unescape(_TAG.sub(" ", body)).strip()
            spans = {name.lower(): int(value) for name, value in _SPAN.findall(attributes)}
            for _ in range(max(1, min(spans.get("col", 1), 50))):
                cells.append(text)
                kinds.append(kind.lower())
                if spans.get("row", 1) > 1:
                    carried[column] = (min(spans["row"], 500) - 1, text)
                column += 1
        _take_carried(carried, column, cells)
        if cells:
            rows.append((bool(kinds) and all(kind == "th" for kind in kinds), cells))
    return rows


def _pipe_rows(lines: Sequence[str]) -> list[tuple[bool, list[str]]]:
    """Markdown の表の行（区切りの行の直前の行は見出し）。"""
    rows: list[tuple[bool, list[str]]] = []
    for index, line in enumerate(lines):
        if _PIPE_RULE.match(line) or line.count("|") < 2:
            continue
        header = index + 1 < len(lines) and _PIPE_RULE.match(lines[index + 1]) is not None
        rows.append((header, [cell.strip() for cell in line.strip().strip("|").split("|")]))
    return rows


def _pair_runs(lines: Sequence[str]) -> list[list[tuple[bool, list[str]]]]:
    """「名前：値」の行（区切りで 2 つ以上のセルに分かれる行）の、続いて並ぶ行のまとまり。見出しの行は持たない。"""
    runs: list[list[tuple[bool, list[str]]]] = [[]]
    for line in lines:
        body = _PAIR_MARKER.sub("", line, count=1)
        cells = [cell.strip() for cell in _PAIR_SEPARATOR.split(body) if cell.strip()] if line.count("|") < 2 else []
        if len(cells) >= 2:
            runs[-1].append((False, cells))
        elif runs[-1]:
            runs.append([])
    return [run for run in runs if run]


def _prose_runs(lines: Sequence[str]) -> list[list[tuple[bool, list[str]]]]:
    """1 文の中で読点で並べた「A は x、B は y」（名前と値の組が 2 つ以上・名前がすべて違う）を表の行にする。"""
    runs: list[list[tuple[bool, list[str]]]] = []
    for line in lines:
        for sentence in _SENTENCE_END.split(line):
            pairs = [match for clause in _CLAUSE_END.split(sentence) if (match := _PROSE_PAIR.match(clause))]
            labels = [_cell_key(match["label"]) for match in pairs]
            if len(pairs) >= 2 and all(labels) and len(set(labels)) == len(labels):
                runs.append([(False, [match["label"].strip(), match["value"].strip()]) for match in pairs])
    return runs


def _value_cell(key: str, cell: str) -> bool:
    """先頭の列以外のセルを値とみなすか（英数字だけ・数字を含む・数字の無い 3 文字以上の語）。

    読点・句点で区切った文のセルは説明で、値ではない（頭の部分だけが文に現れても対応の手がかりにならない）。
    """
    if _is_value(key):
        return True
    if not key or len(key) > _MAX_LABEL_CHARS or _SENTENCE_PUNCTUATION.search(cell):
        return False
    return any(char.isdigit() for char in key) or len(key) >= _MIN_WORD_VALUE_CHARS


_INDEX_CELL = re.compile(r"[0-9]{1,6}")


def index_column(body: Sequence[Sequence[str]]) -> bool:
    """先頭の列が行の番号（整数が上の行から増える）で、ほかに 2 列以上ある表か。

    行の番号の列を区分・名前の列とみなすと、次の列の区分・名前（「部品 1」）を値として扱い、正しい文を別の列の値の
    取り違えと疑ってしまう。行の番号は飛んでもよい（根拠に一部の行だけがあるとき）。
    """
    if len(body) < 2 or any(len(cells) < 3 for cells in body):
        return False
    numbers: list[int] = []
    for cells in body:
        key = _fold(cells[0])
        if not _INDEX_CELL.fullmatch(key):
            return False
        numbers.append(int(key))
    return all(later > earlier for earlier, later in zip(numbers, numbers[1:], strict=False))


def _table(rows: Sequence[tuple[bool, list[str]]]) -> Table | None:
    body = [cells for header, cells in rows if not header]
    headers = [cells for header, cells in rows if header]
    # 見出しの印（<th>・区切りの行）が無い HTML の表は、値のセルが無い最初の行を見出しとみなす。
    if body and len(body) == len(rows) and len(body) >= 3 and not any(_is_value(_cell_key(cell)) for cell in body[0]):
        headers = [body[0]]
        body = body[1:]
    if index_column(body):
        # 先頭の行の番号の列は、行の区分・名前の列でも値でもない（次の列を区分・名前の列にする。#3564）。
        body = [cells[1:] for cells in body]
        headers = [cells[1:] for cells in headers]
    grid = tuple(tuple(_cell_key(cell) for cell in cells) for cells in body)
    values: set[str] = set()
    labels: set[str] = set()
    keyed: list[frozenset[str]] = []
    originals: dict[str, str] = {}
    columns: dict[str, set[int]] = {}
    for cells, keys in zip(body, grid, strict=True):
        row = {key for key in keys if key}
        for column, (cell, key) in enumerate(zip(cells, keys, strict=True)):
            if not key:
                continue
            originals.setdefault(key, " ".join(cell.split())[:_MAX_LABEL_CHARS])
            columns.setdefault(key, set()).add(column)
            if column == 0:
                if _ROMAN_CELL.fullmatch(cell.strip()):
                    labels.add(key)
                continue
            if _value_cell(key, cell):
                values.add(key)
            # セルの中のコード（文がコードだけを述べたときも、その行の値として照合する）。
            for code in _CODE_IN_CELL.findall(_fold(cell)):
                if code != key and _is_value(code):
                    row.add(code)
                    values.add(code)
                    originals.setdefault(code, code)
                    columns.setdefault(code, set()).add(column)
        keyed.append(frozenset(row))
    header: tuple[str, ...] = ()
    if headers:
        header = tuple(_cell_key(cell) for cell in headers[-1])
        for cell, key in zip(headers[-1], header, strict=True):
            if key:
                originals.setdefault(key, " ".join(cell.split())[:_MAX_LABEL_CHARS])
    if len(keyed) < 2:
        return None
    return Table(
        tuple(keyed),
        originals,
        frozenset(values),
        frozenset(labels),
        header,
        grid,
        {key: frozenset(found) for key, found in columns.items()},
    )


def _generated_block_start(line: str) -> bool:
    """画像の生成説明の欄の見出し（「■ グループ」）か、図の中の文字（OCR）の欄の行か（言語資源のラベル）。"""
    labels = content_labels()
    stripped = line.strip()
    prefix = labels.group_prefix.strip()
    if prefix and stripped.startswith(prefix) and stripped[len(prefix) :].strip() in set(labels.groups.values()):
        return True
    ocr = labels.text("picture_ocr")
    return bool(ocr) and re.match(rf"{re.escape(ocr)}\s*[:：]", stripped) is not None


def _document_lines(lines: Sequence[str]) -> list[str]:
    """本文の行から、画像の生成説明の欄と図の中の文字（OCR）の欄（見出しの行から次の空行まで）を除いた行。

    生成説明は 1 つの図の属性を「ラベル: 値」で並べた形で、行は同じ図の別の属性（区分・名前ごとに値を対応させた
    表の行ではない）。欄の値と別の欄の名前を同じ所で述べても、表の対応の取り違えではない（#3482）。
    """
    kept: list[str] = []
    generated = False
    for line in lines:
        if not line.strip():
            generated = False
            kept.append(line)
        elif generated or _generated_block_start(line):
            generated = True
        else:
            kept.append(line)
    return kept


def evidence_tables(text: str, *, line_pairs: bool = True) -> list[Table]:
    """根拠の本文の表（HTML の表・Markdown の表・「名前：値」の行の並び）。行が 2 つ以上のものだけ。

    「名前：値」の行と 1 文の並びは資料の本文からだけ取る（画像の生成説明・図の中の文字の欄は除く）。
    ``line_pairs`` が偽なら、本当の表の構造（HTML・Markdown の表）だけを返す（画像の生成説明の抜粋の根拠）。
    """
    source = str(text or "")
    tables: list[Table] = []
    for block in _TABLE_BLOCK.findall(source):
        table = _table(_html_rows(block))
        if table is not None:
            tables.append(table)
    rest = _TABLE_BLOCK.sub("\n", source)
    lines = [line for line in rest.split("\n") if line.strip()]
    document = [line for line in _document_lines(rest.split("\n")) if line.strip()] if line_pairs else []
    for rows in (_pipe_rows(lines), *_pair_runs(document), *_prose_runs(document)):
        table = _table(rows)
        if table is not None:
            tables.append(table)
    return tables


def _clean_claim(text: str) -> str:
    """文から、根拠の ID・出典の括弧・頁の参照・強調の印を除く（値の照合に当てない）。"""
    text = _LIST_MARKER.sub(" ", str(text or ""))
    text = _CITATION.sub(" ", text)
    text = _CHUNK_ID.sub(" ", text)
    text = _PAGE_REF.sub(" ", text)
    return _fold(_EMPHASIS.sub("", text))


def _mentions(key: str, window: str, *, shortened_label: bool = False, roman_label: bool = False) -> bool:
    """セルの照合の形が区間に現れるか。

    英数字で終わる・始まるセルは、前後が英数字の続きなら別の語。1 文字の値（「1」）は、前後が漢字なら手順の番号・
    数量（「手順1」「1件」）とみなして数えない。区切り付きの番号・コードの内部も独立した値として扱わない。
    セルを短くした区分・名前は、漢字・カタカナが続く複合語の内部だけでは行の手がかりにしない。
    ローマ数字だけの区分の名前（``roman_label``。「区分Ⅰ」の「Ⅰ」）は、漢字に続いても数える。
    """
    return bool(_mention_starts(key, window, shortened_label=shortened_label, roman_label=roman_label))


def _mention_starts(key: str, window: str, *, shortened_label: bool = False, roman_label: bool = False) -> list[int]:
    """セルの照合の形が区間に現れる位置（`_mentions` が数える所だけ）。"""
    found: list[int] = []
    start = window.find(key)
    while start >= 0:
        end = start + len(key)
        before = window[start - 1] if start > 0 else ""
        after = window[end] if end < len(window) else ""
        joined = (key[0].isalnum() and key[0].isascii() and before.isascii() and before.isalnum()) or (
            key[-1].isalnum() and key[-1].isascii() and after.isascii() and after.isalnum()
        )
        if _is_value(key):
            left = window[start - 2] if start >= 2 else ""
            right = window[end + 1] if end + 1 < len(window) else ""
            joined = (
                joined
                or (before in _VALUE_JOINERS and left.isascii() and left.isalnum())
                or (after in _VALUE_JOINERS and right.isascii() and right.isalnum())
            )
            # 符号付きの数を、正の値と誤って同一視しない。
            joined = joined or (key[0].isdigit() and before in {"+", "-", "−"})
        if shortened_label:
            joined = joined or bool(_LABEL_LETTER.fullmatch(before) or _LABEL_LETTER.fullmatch(after))
        counted = not roman_label and (len(key) == 1 and (_IDEOGRAPH.fullmatch(before or " ") or _IDEOGRAPH.fullmatch(after or " ")))
        if not joined and not counted:
            found.append(start)
        start = window.find(key, start + 1)
    return found


def _windows(claim: str, values: set[str]) -> Iterable[str]:
    """文の区間（読点・句点で区切る）。表の値（``values``）の無い区間は続く区間と合わせる。"""
    for sentence in _SENTENCE_END.split(claim):
        pending = ""
        for clause in _WINDOW_END.split(sentence):
            pending = f"{pending} {clause}" if pending else clause
            if any(_mentions(value, clause) for value in values):
                yield pending
                pending = ""
        if pending.strip():
            yield pending


def _unbound_in_table(window: str, table: Table) -> tuple[list[tuple[str, str]], set[str]]:
    """区間の値のうち、この表で別の行の区分・名前と結び付けたもの（値, 別の行のセル）と、この表で行に結び付いた値。

    行に結び付いたかは、値の行にあって区間に現れる手がかり（1 行にだけあるセル。区分・名前とコードなどの値）で見る。
    疑うのは、別の行の区分・名前（値でないセル）を述べたときだけ。別の行の値どうしの共起は、複数の行の値の列挙・
    範囲で、区分と値の結び付けではない。別の行は、その行にも値のセルがある行（同じ並びの別の項目）だけを数える。
    注記・見出しのような値の無い行の語を述べただけでは疑わない。
    """
    counts: dict[str, int] = {}
    for row in table.rows:
        for key in row:
            counts[key] = counts.get(key, 0) + 1

    def starts(key: str) -> list[int]:
        return _mention_starts(
            key,
            window,
            shortened_label=table.is_label(key) and not table.is_value(key) and _fold(table.cells.get(key, key)) != key,
            roman_label=key in table.labels,
        )

    def said(key: str) -> bool:
        return bool(starts(key))

    found = {key: places for key in counts if (table.is_value(key) or table.is_label(key)) and (places := starts(key))}
    # 述べた長いセルの中にだけ現れる短いセル（「部品 1」の「1」）は、別に述べたことにしない（#3564）。
    spans = [(place, place + len(key)) for key, places in found.items() for place in places]
    mentioned = {
        key
        for key, places in found.items()
        if any(
            not any(left <= place and place + len(key) <= right and right - left > len(key) for left, right in spans) for place in places
        )
    }
    # 行を見分ける手がかり: その表の 1 行にだけあり、区間に現れるセル。
    distinct = {key for key in mentioned if counts[key] == 1}
    unbound: list[tuple[str, str]] = []
    bound: set[str] = set()
    for value in sorted(key for key in mentioned if table.is_value(key)):
        rows = [row for row in table.rows if value in row]
        value_columns = table.columns.get(value, frozenset())
        # 行の手がかりは別の列のセル（同じセルの中のコードは、そのセル自身なので数えない）。
        others = {key for key in distinct - {value} if not (table.columns.get(key, frozenset()) & value_columns)}
        matched = [index for index, row in enumerate(table.rows) if value in row and row & others]
        if matched:
            other_column = _other_column(table, value, matched, said)
            if other_column:
                unbound.append((table.cells.get(value, value), table.cells.get(other_column, other_column)))
            else:
                bound.add(table.cells.get(value, value))
            continue
        elsewhere = sorted(
            key
            for key in others
            if table.is_label(key)
            and not (table.columns.get(key, frozenset()) & value_columns)
            and any(key in row and _parallel(table, index, value, value_columns) for index, row in enumerate(table.rows) if row not in rows)
        )
        if elsewhere:
            unbound.append((table.cells.get(value, value), table.cells.get(elsewhere[0], elsewhere[0])))
    return unbound, bound


def _kind(key: str) -> str:
    """値の種類（コード・番号 / 数字を含む語 / 語）。別の行が同じ並びの項目かを見分けるのに使う。"""
    if _is_value(key):
        return "code"
    return "number" if any(char.isdigit() for char in key) else "word"


def _parallel(table: Table, index: int, value: str, value_columns: frozenset[int]) -> bool:
    """別の行（``index``）が、値と同じ列に同じ種類の値を持つ（同じ並びの別の項目の行）か。

    注記・見出しのような、値の列に別の種類の文（「入力は必須」）しか無い行の語を述べただけでは疑わない。
    """
    cells = table.grid[index] if index < len(table.grid) else ()
    kind = _kind(value)
    for column in value_columns:
        cell = cells[column] if column < len(cells) else ""
        if not cell or not table.is_value(cell) and not _CODE_IN_CELL.findall(cell):
            continue
        if _kind(cell) == kind or (kind == "code" and _CODE_IN_CELL.findall(cell)):
            return True
    return False


def _other_column(table: Table, value: str, rows: Sequence[int], said: Callable[[str], bool]) -> str:
    """行は合っている値について、文がその値の列の見出しを述べず、同じ行の別の列の見出しを述べたらその見出し。

    見出しの行が無い表・先頭の列（行の区分・名前の列）・同じ見出しが 2 列以上ある表は見ない。その行の別の列が
    空欄・同じ値なら疑わない。
    """
    names = [key for key in table.header if key]
    if not names or len(set(names)) != len(names):
        return ""
    header = table.header
    for index in rows:
        cells = table.grid[index] if index < len(table.grid) else ()
        for column, key in enumerate(cells):
            if column == 0 or column >= len(header) or not (key == value or value in _CODE_IN_CELL.findall(key)):
                continue
            if not _is_label(header[column]) or said(header[column]):
                continue
            for other, name in enumerate(header):
                if (
                    other not in {0, column}
                    and other < len(cells)
                    and cells[other]
                    and cells[other] != key
                    and _is_label(name)
                    and said(name)
                ):
                    return name
    return ""


def unbound_table_values(claim: str, evidence_texts: Sequence[str], *, line_pairs: bool = True) -> list[tuple[str, str]]:
    """文が、根拠の表の別の行の区分・名前と結び付けた値の組（値, 別の行のセル。根拠の原文の表記）。

    同じ値をどれかの表で同じ行の区分・名前と結び付けていれば、その値は外さない（表をまたいで疑わない）。
    ``line_pairs`` は ``evidence_tables`` と同じ。
    """
    tables = [table for text in evidence_texts for table in evidence_tables(text, line_pairs=line_pairs)]
    if not tables:
        return []
    values = {key for table in tables for row in table.rows for key in row if table.is_value(key)}
    found: dict[str, str] = {}
    for window in _windows(_clean_claim(claim), values):
        bound: set[str] = set()
        candidates: dict[str, str] = {}
        for table in tables:
            unbound, bound_here = _unbound_in_table(window, table)
            bound |= bound_here
            for value, other in unbound:
                candidates.setdefault(value, other)
        for value, other in candidates.items():
            if value not in bound:
                found.setdefault(value, other)
    # セルの中のコードと、そのセル全体の両方が当たったら、セル全体だけを返す（同じ誤りを 2 回数えない）。
    return sorted(
        (value, other)
        for value, other in found.items()
        if not any(value != whole and _fold(value) in _fold(whole) and found[whole] == other for whole in found)
    )


def _correct_cell(pair: tuple[str, str], tables: Sequence[Table]) -> tuple[str, str, str] | None:
    """取り違えた組（値, 別の行の名前か別の列の見出し）について、表の正しい対応（行の名前, 見出し, 正しい値）。

    別の行の名前なら、その行の値の列のセル。別の列の見出しなら、値の行のその列のセル。決まらなければ None。
    """
    value, other = (_cell_key(part) for part in pair)
    for table in tables:
        if other in table.header:
            column = table.header.index(other)
            for cells in table.grid:
                has_value = value in cells or any(value in _CODE_IN_CELL.findall(cell) for cell in cells)
                if has_value and column < len(cells) and cells[column] and cells[column] != value:
                    label = table.cells.get(cells[0], cells[0]) if cells and cells[0] else ""
                    return label, table.cells.get(other, other), table.cells.get(cells[column], cells[column])
            continue
        for column in sorted(table.columns.get(value, frozenset())):
            for cells in table.grid:
                if other in cells and column < len(cells) and cells[column] and cells[column] != value:
                    return table.cells.get(other, other), "", table.cells.get(cells[column], cells[column])
    return None


def table_mismatch_notes(pairs: Sequence[tuple[str, str]], evidence_texts: Sequence[str], *, line_pairs: bool = True) -> list[str]:
    """取り違えた組（`unbound_table_values`）ごとの、根拠の表の正しい対応の文（外した段落の理由に出す）。"""
    tables = [table for text in evidence_texts for table in evidence_tables(text, line_pairs=line_pairs)]
    notes: list[str] = []
    for value, other in pairs:
        correct = _correct_cell((value, other), tables)
        if correct is None:
            notes.append(f"値『{value}』は『{other}』と別の行の値")
        elif correct[1]:
            row = f"『{correct[0]}』の" if correct[0] else ""
            notes.append(f"{row}『{correct[1]}』は『{correct[2]}』（『{value}』は別の列の値）")
        else:
            notes.append(f"『{correct[0]}』の値は『{correct[2]}』（『{value}』は別の行の値）")
    return notes


def table_corrections(
    pairs: Sequence[tuple[str, str]], evidence_texts: Sequence[str], *, line_pairs: bool = True
) -> list[tuple[str, str | None]]:
    """取り違えた組ごとの（誤った値, 表の正しい値）。正しい値が決まらなければ None。"""
    tables = [table for text in evidence_texts for table in evidence_tables(text, line_pairs=line_pairs)]
    return [(value, correct[2] if (correct := _correct_cell((value, other), tables)) else None) for value, other in pairs]


def states_value(value: str, text: str) -> bool:
    """文が値（セル全体か、セルの中のコード）を述べているか（照合の形で比べる）。"""
    folded = _fold(text)
    key = _cell_key(value)
    return bool(key) and (key in folded or any(code in folded for code in _CODE_IN_CELL.findall(_fold(value))))


__all__ = ["Table", "evidence_tables", "index_column", "states_value", "table_corrections", "table_mismatch_notes", "unbound_table_values"]
