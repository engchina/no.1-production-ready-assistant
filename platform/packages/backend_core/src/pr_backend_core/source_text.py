"""検索の結果と観測が返す本文（根拠の chunk など）の大きさの規則（3 製品で共通の 1 つ。#3321）。

- **検索の結果**（RAG の `rag_search` / `rag_retrieve_evidence`、Agent の公開 MCP の `sources`）:
  1 件ごとに全文を返し、応答の合計に予算（token の見積もり）を置いて順位の高いものから入れる
  （`fit_source_texts`）。予算に入らない件は本文を省き（`text_omitted`）、ID・場所・長さだけを返す。
  1 件だけが 1 件の上限を超えるときだけ切る: 質問に当たった部分を中心にした範囲を選び、本文は文・
  段落の境界、表は見出しの行を残して行の境界で切る（`excerpt_source_text`）。切った範囲
  （`excerpt_range`）と続きの位置（`next_offset`）を返し、読み取りのツールで続きを読めるようにする。
- **続きの読み取り**（RAG の `rag_read_source` / `rag_read_section` / `rag_read_document`）:
  位置（`offset`）から順に、文字数の上限に入る最後の文・段落（表は行）の境界まで返す
  （`read_source_window`）。続きの位置は返した範囲の終わりちょうどで、繰り返すと隙間も重なりも無く
  全文になる。表の続きは先頭に見出しの行を付け、その範囲（`header_range`）を返す（#3344）。
- **観測**（評価・監査）: 生成に渡した本文そのもの（切らない）。件数と 1 ページの合計の予算で
  ページに分け、1 件だけで予算を超える件は本文を省く（`page_source_texts`）。

業界の慣行に合わせる:

- ツールの出力は 1 件の長さではなく応答の全体の token で抑え、省いたものと続きを読む手を返す
  （Anthropic のツールの設計の指針。Claude Code は MCP のツールの出力を既定 25,000 token で切る）。
- 抜粋は質問の語が当たる部分を中心にし、文の境界で切る（Lucene / Elasticsearch の highlighter の
  passage）。質問の語は、CJK の文字は 2 文字の組（Lucene の CJKBigramFilter と同じ考え。ひらがなだけ
  の組は助詞などで当たりやすいので数えない）、それ以外は英数字の語で数える。
- 評価・観測は生成に渡した本文をそのまま記録する（Ragas・TruLens の context、Langfuse・
  OpenTelemetry GenAI の content）。

見積もりは `estimate_input_tokens`（3 製品で共通）。切り方は内容の種類（本文 / 表）だけで決め、
特定の文書・業務の語には依らない。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from .token_estimate import estimate_input_tokens

# 1 件の上限（token の見積もり）。今の chunk の大きさから決めた（合成の文書を RAG の既定の分割に
# かけて測った。#3321）: 本文の子 chunk は既定の目標 1000 字で最大 1001 字・設定の上限 1600 字で
# 最大 1597 字、表の子 chunk は既定の目標 3000 字で最大 3148 字（列見出しを含む）。見積もりは
# CJK の 1 字を 1 token にするので、既定の分割の chunk はすべての字が CJK でも上限に入り、切らずに
# 全文を返す。切るのは、目標を既定より大きくした表・構造の無い大きな表・長い図の説明など、既定より
# 大きな chunk。
SOURCE_TEXT_ITEM_MAX_TOKENS = 4000
# 検索の結果の応答の合計の予算（token の見積もり）。本文の外の項目（ID・場所など）も含めた応答の
# 全体をこの見積もりに収める（呼ぶ側が本文の外の分を引いて fit_source_texts に渡す）。外部の MCP
# クライアントの出力の上限（Claude Code の既定 25,000 token）より下に置き、見積もりは CJK の 1 字を
# 1 token にする多めの見積もりなので、上限との差（1,000）を余白にする。rag_retrieve_evidence の既定
# 50 件（#3512）で返せる本文を増やすため 20000 から広げた（#3521）。rag_search の既定の 12 件の
# 既定の大きさの chunk（合成の測定で平均 約 450〜650）はすべて入り、表の chunk が多い応答・件数の
# 多い応答だけ、順位の低い件の本文を省く。
SOURCE_TEXT_RESPONSE_BUDGET_TOKENS = 24000
# 観測のページの件数（既定・上限）と、1 ページの本文の合計の予算（token の見積もり）。予算は検索の
# 結果と同じにし、LLM のクライアントが観測を読んでも出力の上限に収まるようにする。
SOURCE_TEXT_PAGE_LIMIT_DEFAULT = 20
SOURCE_TEXT_PAGE_LIMIT_MAX = 100
SOURCE_TEXT_PAGE_BUDGET_TOKENS = SOURCE_TEXT_RESPONSE_BUDGET_TOKENS

SourceTextKind = Literal["text", "table"]

# 本文の単位の終わり（段落・行・文）。句点・感嘆符・疑問符と、その後ろの閉じ括弧・引用符・空白を
# 含める。英文のピリオドは後ろが空白か終わりのときだけ（小数・略号の途中で切らない）。
_SENTENCE_END = re.compile(
    r"\n+|[。．！？!?]+[」』）)\]】〕\"'’”]*[ \t　]*|\.(?=\s|$)[\"'’”)\]]*[ \t　]*"
)
# 文より細かい単位（読点・区切りの記号・空白）。1 つの文だけで上限を超えるときに使う。
_CLAUSE_END = re.compile(r"[、，,;；:：]+[ \t　]*|[ \t　]+")
_HTML_ROW = re.compile(r"<tr\b[^>]*>.*?</tr\s*>", re.IGNORECASE | re.DOTALL)
_HTML_DATA_CELL = re.compile(r"<td\b", re.IGNORECASE)
_MARKDOWN_RULE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_CELL_SEPARATORS = ("|", "\t")


@dataclass(frozen=True, slots=True)
class TextRange:
    """本文の中の範囲（文字の位置。``[start, end)``）。"""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class SourceText:
    """返す本文と、元の本文との関係。"""

    # 返す本文（省いたときは空）。切った表は ``header_range`` の行を先頭に付けた本文。
    text: str
    # 元の本文の文字数。
    text_length: int
    # 本文の一部だけを返したか（範囲は ``excerpt_range``）。
    truncated: bool = False
    # 予算に入らず本文を返さなかったか。
    text_omitted: bool = False
    # 返した本文の元の範囲（全文は ``[0, text_length)``。省いたときは None）。
    excerpt_range: TextRange | None = None
    # 切った表で本文の先頭に付けた見出しの行の範囲（``excerpt_range`` と続かないときだけ）。
    header_range: TextRange | None = None
    # 続きを読むときの位置（``excerpt_range`` の後ろに本文が残るときだけ）。
    next_offset: int | None = None
    # 返した本文の token の見積もり。
    tokens: int = 0


@dataclass(frozen=True, slots=True)
class SourceTextItem:
    """予算に入れる 1 件（順位の順に渡す）。"""

    text: str
    kind: SourceTextKind = "text"
    # 本文の外の項目（ID・場所など）の token の見積もり。省いた件もこの分は応答に残る。
    fixed_tokens: int = 0


@dataclass(frozen=True, slots=True)
class SourceTextPage:
    """観測の 1 ページ（``items`` は元の位置と本文の組）。"""

    items: list[tuple[int, SourceText]]
    next_offset: int | None


def full_source_text(text: str) -> SourceText:
    """切らない本文。"""
    return SourceText(
        text=text,
        text_length=len(text),
        excerpt_range=TextRange(0, len(text)),
        tokens=estimate_input_tokens(text),
    )


def omitted_source_text(text: str) -> SourceText:
    """本文を省いた 1 件（長さだけを返す）。"""
    return SourceText(text="", text_length=len(text), text_omitted=True)


def query_terms(queries: Iterable[str | None]) -> frozenset[str]:
    """質問の語（CJK は 2 文字の組、それ以外は英数字の語）。"""
    terms: set[str] = set()
    for query in queries:
        if not query:
            continue
        for run, cjk in _runs(_normalize(query)):
            if not cjk:
                if len(run) >= 2 or run.isdigit():
                    terms.add(run)
                continue
            if len(run) == 1:
                if not _is_hiragana(run):
                    terms.add(run)
                continue
            for index in range(len(run) - 1):
                pair = run[index : index + 2]
                if not all(_is_hiragana(char) for char in pair):
                    terms.add(pair)
    return frozenset(terms)


def excerpt_source_text(
    text: str,
    *,
    max_tokens: int = SOURCE_TEXT_ITEM_MAX_TOKENS,
    kind: SourceTextKind = "text",
    queries: Iterable[str | None] = (),
    terms: frozenset[str] | None = None,
) -> SourceText:
    """1 件の本文を上限に収める（上限以下なら全文。超えるときだけ境界で切る）。"""
    if max_tokens < 1:
        raise ValueError("max_tokens は 1 以上にしてください。")
    if estimate_input_tokens(text) <= max_tokens:
        return full_source_text(text)
    words = query_terms(queries) if terms is None else terms
    if kind == "table":
        layout = _table_layout(text)
        if layout is not None:
            header, rows = layout
            header_tokens = estimate_input_tokens(text[header.start : header.end])
            # 見出しが上限の半分を超える表は、見出しを残すと行がほとんど入らないので本文として切る。
            if header_tokens <= max_tokens // 2:
                window = _window(text, rows, max_tokens - header_tokens, words, level=0)
                return _table_excerpt(text, header, window)
    window = _window(text, _units(text, 0, len(text), _SENTENCE_END), max_tokens, words, level=1)
    return _excerpt(text, window)


def read_source_window(
    text: str, *, offset: int = 0, max_chars: int, kind: SourceTextKind = "text"
) -> SourceText:
    """``offset`` から順に読む窓（続きの読み取り。見出しの行を含めて ``max_chars`` 文字まで）。

    上限に入る最後の文・段落の境界（表は行の境界）で切る。1 つの単位だけで上限を超えるときだけ、
    細かい単位（読点・空白）、最後に字で切る。``excerpt_range`` は ``[offset, 終わり)``、
    ``next_offset`` はその終わり（続きが無ければ None）なので、``next_offset`` を渡して読み続けると
    隙間も重なりも無く全文になる。表の続き（``offset`` が見出しの後ろ）は本文の先頭に見出しの行を
    付けて ``header_range`` に示す（見出しが上限の半分を超える表は付けない。抜粋と同じ）。
    """
    if offset < 0:
        raise ValueError("offset は 0 以上にしてください。")
    if max_chars < 1:
        raise ValueError("max_chars は 1 以上にしてください。")
    length = len(text)
    start = min(offset, length)
    header: TextRange | None = None
    budget = max_chars
    if start >= length:
        end = start
    elif kind == "table" and (layout := _table_layout(text)) is not None:
        head, rows = layout
        if start < head.end:
            units = [*_units(text, start, head.end, _SENTENCE_END), *rows]
        else:
            units = [TextRange(max(row.start, start), row.end) for row in rows if row.end > start]
            if start > 0 and 0 < head.end - head.start <= max_chars // 2:
                header = head
                budget = max_chars - (head.end - head.start)
        end = _greedy_end(units, budget, text=text, level=-1)
    else:
        end = _forward_end(text, start, length, budget, level=0)
    prefix = text[header.start : header.end] if header is not None else ""
    body = prefix + text[start:end]
    return SourceText(
        text=body,
        text_length=length,
        truncated=start > 0 or end < length,
        excerpt_range=TextRange(start, end),
        header_range=header,
        next_offset=end if end < length else None,
        tokens=estimate_input_tokens(body),
    )


def fit_source_texts(
    items: Sequence[SourceTextItem],
    *,
    budget_tokens: int = SOURCE_TEXT_RESPONSE_BUDGET_TOKENS,
    item_max_tokens: int = SOURCE_TEXT_ITEM_MAX_TOKENS,
    queries: Iterable[str | None] = (),
) -> list[SourceText]:
    """検索の結果の本文を応答の予算に入れる（順位の順に、入るものから。入らない件は省く）。

    順位の高い件の本文が、順位の低い件のために省かれることはない（入らない件を飛ばして、後ろの
    小さな件は入れる）。
    """
    terms = query_terms(queries)
    remaining = budget_tokens - sum(max(0, item.fixed_tokens) for item in items)
    fitted: list[SourceText] = []
    for item in items:
        piece = excerpt_source_text(
            item.text, max_tokens=item_max_tokens, kind=item.kind, terms=terms
        )
        if piece.tokens <= remaining:
            remaining -= piece.tokens
            fitted.append(piece)
        else:
            fitted.append(omitted_source_text(item.text))
    return fitted


def page_source_texts(
    items: Sequence[SourceTextItem],
    *,
    offset: int = 0,
    limit: int = SOURCE_TEXT_PAGE_LIMIT_DEFAULT,
    budget_tokens: int = SOURCE_TEXT_PAGE_BUDGET_TOKENS,
) -> SourceTextPage:
    """観測の本文を全文でページに分ける（件数 ``limit`` と合計の予算まで）。

    1 件だけで予算を超える件は本文を省いて数え、ページを止めない（読み取りのツールで読む）。
    予算の残りに入らない件は次のページの先頭にする。どのページも 1 件以上を進める。
    """
    if offset < 0:
        raise ValueError("offset は 0 以上にしてください。")
    if limit < 1:
        raise ValueError("limit は 1 以上にしてください。")
    page: list[tuple[int, SourceText]] = []
    remaining = budget_tokens
    index = offset
    while index < len(items) and len(page) < limit:
        item = items[index]
        fixed = max(0, item.fixed_tokens)
        piece = full_source_text(item.text)
        if fixed + piece.tokens > budget_tokens:
            if page and fixed > remaining:
                break
            page.append((index, omitted_source_text(item.text)))
            remaining -= fixed
        elif fixed + piece.tokens > remaining:
            break
        else:
            page.append((index, piece))
            remaining -= fixed + piece.tokens
        index += 1
    return SourceTextPage(items=page, next_offset=index if index < len(items) else None)


def _excerpt(text: str, window: TextRange) -> SourceText:
    body = text[window.start : window.end]
    return SourceText(
        text=body,
        text_length=len(text),
        truncated=True,
        excerpt_range=window,
        next_offset=window.end if window.end < len(text) else None,
        tokens=estimate_input_tokens(body),
    )


def _table_excerpt(text: str, header: TextRange, window: TextRange) -> SourceText:
    if window.start <= header.end:
        return _excerpt(text, TextRange(0, window.end))
    body = text[header.start : header.end] + text[window.start : window.end]
    return SourceText(
        text=body,
        text_length=len(text),
        truncated=True,
        excerpt_range=window,
        header_range=header,
        next_offset=window.end if window.end < len(text) else None,
        tokens=estimate_input_tokens(body),
    )


def _table_layout(text: str) -> tuple[TextRange, list[TextRange]] | None:
    """表の見出し（表の前の文・表の題・列見出しの行）と行の範囲。行に分けられなければ None。"""
    rows = list(_HTML_ROW.finditer(text))
    if len(rows) >= 2:
        header_rows = 0
        while header_rows < len(rows) and not _HTML_DATA_CELL.search(rows[header_rows].group()):
            header_rows += 1
        # 見出しのセル（th）が無い表は、最初の行を列見出しとみなす。
        header_rows = max(1, header_rows)
        if header_rows >= len(rows):
            return None
        starts = [match.start() for match in rows[header_rows:]]
        ends = [*starts[1:], len(text)]
        return TextRange(0, starts[0]), [
            TextRange(start, end) for start, end in zip(starts, ends, strict=True)
        ]
    lines = _units(text, 0, len(text), re.compile(r"\n"))
    if len(lines) < 2:
        return None
    contents = [text[line.start : line.end] for line in lines]
    counts = {sep: sum(1 for line in contents if sep in line) for sep in _CELL_SEPARATORS}
    separator = max(_CELL_SEPARATORS, key=lambda sep: counts[sep])
    header_lines = 1
    if counts[separator]:
        # 区切りを持つ最初の行（列見出しの行）までを見出しにし、Markdown の罫線の行も含める。
        first = next(index for index, line in enumerate(contents) if separator in line)
        header_lines = first + 1
        if header_lines < len(lines) and _MARKDOWN_RULE.match(contents[header_lines]):
            header_lines += 1
    if header_lines >= len(lines):
        return None
    return TextRange(0, lines[header_lines].start), lines[header_lines:]


def _units(text: str, start: int, end: int, pattern: re.Pattern[str]) -> list[TextRange]:
    """``[start, end)`` を、区切りの後ろで切った隙間の無い単位に分ける。"""
    units: list[TextRange] = []
    cursor = start
    for match in pattern.finditer(text, start, end):
        if match.end() > cursor:
            units.append(TextRange(cursor, match.end()))
            cursor = match.end()
    if cursor < end:
        units.append(TextRange(cursor, end))
    return units


def _forward_end(text: str, start: int, end: int, budget: int, *, level: int) -> int:
    """``[start, end)`` を先頭から読み、``budget`` 字に入る最後の単位の境界（続きの読み取り）。

    ``level`` は分ける単位（0 は文・段落、1 は読点・空白、2 以上は字）。
    """
    if end - start <= budget:
        return end
    if level >= 2:
        return start + budget
    units = _units(text, start, end, (_SENTENCE_END, _CLAUSE_END)[level])
    return _greedy_end(units, budget, text=text, level=level)


def _greedy_end(units: list[TextRange], budget: int, *, text: str, level: int) -> int:
    """隙間の無い単位を先頭から ``budget`` 字に入るだけ足した終わり。

    先頭の単位だけで超えるときは、その単位を 1 段細かく分けて読む（``level`` は単位の段。
    表の行は -1）。
    """
    origin = units[0].start
    cut = origin
    for unit in units:
        if unit.end - origin > budget:
            break
        cut = unit.end
    if cut > origin:
        return cut
    first = units[0]
    return _forward_end(text, first.start, first.end, budget, level=level + 1)


def _window(
    text: str, units: list[TextRange], budget: int, terms: frozenset[str], *, level: int
) -> TextRange:
    """質問の語が最も当たる単位から前後へ、予算に入るだけ単位を足した範囲。"""
    costs = [estimate_input_tokens(text[unit.start : unit.end]) for unit in units]
    center = _best_unit(text, units, terms)
    if costs[center] > budget:
        unit = units[center]
        pattern = (_SENTENCE_END, _CLAUSE_END)[level] if level < 2 else None
        finer = _units(text, unit.start, unit.end, pattern) if pattern is not None else []
        if len(finer) > 1:
            return _window(text, finer, budget, terms, level=level + 1)
        return _char_window(text, unit, budget, terms)
    low, high, used = center, center + 1, costs[center]
    grow_after = grow_before = True
    # 読む向き（後ろ）を先に、前後へ交互に広げる。入らない単位で止める（飛ばして繋げない）。
    while grow_after or grow_before:
        if grow_after:
            if high < len(units) and used + costs[high] <= budget:
                used += costs[high]
                high += 1
            else:
                grow_after = False
        if grow_before:
            if low > 0 and used + costs[low - 1] <= budget:
                low -= 1
                used += costs[low]
            else:
                grow_before = False
    return TextRange(units[low].start, units[high - 1].end)


def _char_window(text: str, unit: TextRange, budget: int, terms: frozenset[str]) -> TextRange:
    """区切りの無い単位を、質問の語の位置から字の単位で予算まで広げる（最後の手段）。"""
    segment = _normalize(text[unit.start : unit.end])
    anchor = unit.start
    if len(segment) == unit.end - unit.start:
        positions = [segment.find(term) for term in terms if term in segment]
        if positions:
            anchor = unit.start + min(positions)
    low = high = anchor
    ascii_count = other = 0

    def cost(add_ascii: int, add_other: int) -> int:
        return other + add_other + (ascii_count + add_ascii + 3) // 4

    while True:
        moved = False
        for direction in (1, -1):
            index = high if direction == 1 else low - 1
            if not unit.start <= index < unit.end:
                continue
            is_ascii = ord(text[index]) < 0x80
            if cost(int(is_ascii), int(not is_ascii)) > budget:
                continue
            ascii_count += int(is_ascii)
            other += int(not is_ascii)
            if direction == 1:
                high += 1
            else:
                low -= 1
            moved = True
        if not moved:
            break
    if high == low:
        high = min(unit.end, low + 1)
    return TextRange(low, high)


def _best_unit(text: str, units: list[TextRange], terms: frozenset[str]) -> int:
    """質問の語が最も多く当たる単位の位置（当たらなければ先頭）。"""
    if not terms:
        return 0
    best, best_score = 0, 0
    for index, unit in enumerate(units):
        segment = _normalize(text[unit.start : unit.end])
        score = sum(1 for term in terms if term in segment)
        if score > best_score:
            best, best_score = index, score
    return best


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _runs(value: str) -> list[tuple[str, bool]]:
    """CJK の文字の続きと、英数字の続きに分ける（それ以外の文字は区切り）。"""
    runs: list[tuple[str, bool]] = []
    current: list[str] = []
    current_cjk = False
    for char in value:
        cjk = _is_cjk(char)
        word = cjk or char.isalnum()
        if not word or (current and cjk != current_cjk):
            if current:
                runs.append(("".join(current), current_cjk))
            current = []
        if word:
            current.append(char)
            current_cjk = cjk
    if current:
        runs.append(("".join(current), current_cjk))
    return runs


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return (
        0x3040 <= code <= 0x30FF  # ひらがな・カタカナ
        or 0x3400 <= code <= 0x4DBF  # CJK 統合漢字拡張 A
        or 0x4E00 <= code <= 0x9FFF  # CJK 統合漢字
        or 0xF900 <= code <= 0xFAFF  # CJK 互換漢字
        or 0xAC00 <= code <= 0xD7AF  # ハングル
        or 0x20000 <= code <= 0x3134F  # CJK 統合漢字拡張 B〜
    )


def _is_hiragana(char: str) -> bool:
    return 0x3040 <= ord(char) <= 0x309F
