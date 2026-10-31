"""見出しの判定・昇格・節パス（section_path）・目次・ランニングヘッダの扱い。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from rag_parser_core.language import current_pack
from rag_parser_core.language.markers import marker_patterns
from rag_parser_core.language.text import numbered, text_patterns

from rag_engine.chunking.constants import (
    PROMOTED_TEXT_HEADING_SOURCE,
    ROUTE_CAPTION_HEADING_SOURCE,
    RUNNING_HEAD_HEADING_SOURCE,
    SECTION_CATEGORIES,
    SECTION_HEADER_HEADING_SOURCE,
    TITLE_HEADING_SOURCE,
    DocumentChunk,
    _boilerplate_text_key,
    _int_value,
)
from rag_engine.chunking.records import _record_text


# 番号の前置き・数・単位・文末の記号・文として終わる語は言語資源（#2181）。番号の前後の空白は
# あってもなくてもよい（LibreOffice などの PDF は「第 4 章」と空白を入れる。#1517）。全角の数字と空白は
# NFKC で半角になる。
@dataclass(frozen=True)
class _HeadingPatterns:
    # 文末の直後（閉じ括弧の前は除く）と、空白が続く英文のピリオドの直後を分割候補にします。
    sentence_boundary: re.Pattern[str]
    # 「第 N 編」「第 N 部」。章より上の区分で、配下の番号の見出しは 1 段深くなる
    # （`_updated_section_path`）。「第一部門」のような語と紛れないよう、後は空白・区切り・行末に限る。
    part: re.Pattern[str]
    levels: tuple[tuple[re.Pattern[str], int], ...]
    # 文として終わる「見出し」（Docling が Section-header に分類した手順行「２ . 顧客情報を入力します。」）。
    sentence_heading: re.Pattern[str]
    numbered_section: re.Pattern[str]


_heading_patterns_by_hash: dict[str, _HeadingPatterns] = {}


def _heading_patterns() -> _HeadingPatterns:
    pack = current_pack()
    cached = _heading_patterns_by_hash.get(pack.hash)
    if cached is not None:
        return cached
    text = text_patterns(pack)
    part_or_chapter = f"(?:{text.part_units}|{text.chapter_units})"
    # 文末の記号か改行（後読みは 1 文字の集合にする）。
    boundary = text.sentence_end[:-1] + "\\n]" if text.sentence_end.startswith("[") else "[\\n]"
    patterns = _HeadingPatterns(
        sentence_boundary=re.compile(rf"(?<={boundary})(?!{text.sentence_closer})|(?<=\.)(?=\s)"),
        part=re.compile(rf"{numbered(text, text.part_units)}(?=\s|$|[:.、])"),
        numbered_section=re.compile(
            numbered(text, f"(?:{text.part_units}|{text.chapter_units}|{text.section_units}|{text.article_units}|{text.paragraph_units})")
        ),
        levels=(
            (re.compile(numbered(text, part_or_chapter)), 1),
            # 「Ⅰ 月次定例処理」「Ⅴ．定例処理」のローマ数字の章。NFKC で Ⅴ は V になる。英字 1 文字の章記号
            # （「A 【画面説明】」）と紛れないよう、ローマ数字として成立する並びに限る (#730)。I・V・X は単独でも
            # ローマ数字として成立するため、1 文字だけで空白の直後に囲み記号が続く「I 【画面説明】」
            # 「V 【操作説明】」は英字系列（下の level 6）へ倒す。2 文字以上（「Ⅲ 【補足】」）や句点区切り
            # （「Ⅴ．定例処理」）はローマ数字のまま (#747)。
            (
                re.compile(r"(?:X{0,3}(?:IX|IV|V?I{0,3}))(?<=[IVX])(?:\s*[.．、]|(?<=[IVX]{2})\s|\s(?![【\[〔]))"),
                1,
            ),
            (re.compile(numbered(text, text.section_units)), 2),
            (re.compile(numbered(text, f"(?:{text.paragraph_units}|{text.article_units})")), 3),
            # 「B-(1) 新規登録」形式の機能見出し。章を表す英字だけの見出しは推定しません。
            (re.compile(r"[A-Z]?[-—―ー]\([0-9]+\)"), 2),
            # 「（Ａ）システム設定」「（ B ）利用状況」のような英字 1 文字の括弧番号は番号付き機能見出しの上の章。
            (re.compile(r"\(\s*[A-Z]\s*\)"), 4),
            # 「（１）」「（ 12 ）」（Docling は括弧内に空白を入れることがある）と「（一）」。
            (re.compile(rf"\(?\s*[0-9]{{1,2}}\s*\)|\(\s*{text.kanji}{{1,3}}\s*\)"), 5),
            # 操作説明書の「A 【画面説明】」「B 【操作説明】」「B-1.」。番号付き機能見出しの直下に並ぶ系列。
            (re.compile(r"[A-Z]\s*(?:[【\[〔]|[-—―ー.．]\s*[0-9])"), 6),
        ),
        sentence_heading=re.compile(rf"{text.sentence_like_ending}{text.sentence_ending_follower}?$"),
    )
    if len(_heading_patterns_by_hash) >= 16:
        _heading_patterns_by_hash.clear()
    _heading_patterns_by_hash[pack.hash] = patterns
    return patterns


def _sentence_boundary_pattern() -> re.Pattern[str]:
    return _heading_patterns().sentence_boundary


# 柱の初出（章名）。番号見出しより上位で、path の根になる。
_CHAPTER_LEVEL = 0
_LETTERED_LEVEL = 6
_CIRCLED_LEVEL = 8
# 番号付き機能見出し（(n) 以上）までを「機能ユニット」とみなし、親チャンクはこの単位をまたがない。
_UNIT_MAX_LEVEL = 5


# 系列の先頭（「A 【画面説明】」）。直近の非英字見出しの直下に新しい系列を始める。
def _series_start_pattern() -> re.Pattern[str]:
    """系列の先頭（「A 【画面説明】」）。系列の囲み記号は言語資源(#2181)。"""
    return _marker_regex(current_pack().hash, "series_start")


_SECTION_PATH_LIMIT = 8
# 「1.」「1.2」「1.2.3 概要」「3.3消火設備」。区切りのない「3つの方法」や「2024年度」は番号として扱いません。
# 2 段以上の番号（「3.3」）は、空白の無い日本語の見出し（「3.3消火設備」）も番号として読む。1 段の番号は「.」「、」か
# 空白が要る。2 段の番号を 1 段と読み違えない（「3.3消火設備」の「3.」で止まると level 1 になる）。
_DOTTED_HEADING_PATTERN = re.compile(r"([0-9]{1,2}(?:\.[0-9]{1,2})+)(?:[.、]|\s|$|(?=[^\x00-\x7f]))|([0-9]{1,2})(?:[.、]|\s|$)")


# 画面経路の形をした 1 行（[ 画面：A⇒B ]、〔A⇒B⇒C〕）。Docling は図に近いこの行を図の caption として返すことがある。
def _route_caption_pattern() -> re.Pattern[str]:
    """画面経路の形をした 1 行。括弧・矢印は言語資源(#2181)。"""
    return _marker_regex(current_pack().hash, "route_caption")


# 柱（毎ページ上下端に繰り返す章名）とみなすページ上端・下端の帯。bbox の上端がページ高さに対して占める割合。
_RUNNING_HEADER_TOP = 0.07
_RUNNING_FOOTER_TOP = 0.90
# 柱と同族とみなす高さの許容差（確認済みの柱の高さの中央値に対する比）。
_RUNNING_HEIGHT_TOLERANCE = 0.25


# 本文へ戻す手順行のうち、丸数字・番号で始まるものは List-item として手順の並びに載せる。
def _step_marker_pattern() -> re.Pattern[str]:
    """丸数字・番号で始まる手順行。丸数字・番号の閉じは言語資源(#2181)。"""
    return _marker_regex(current_pack().hash, "step_marker")


# 番号・記号だけの「見出し」（手順番号「５ .」が本文から切り離されたもの）。本文へ戻す。
def _marker_only_pattern() -> re.Pattern[str]:
    """番号・記号だけの「見出し」。"""
    return _marker_regex(current_pack().hash, "marker_only")


@lru_cache(maxsize=64)
def _marker_regex(_pack_hash: str, name: str) -> re.Pattern[str]:
    """印の正規表現(言語資源の hash ごとに 1 回だけ作る)。"""
    m = marker_patterns()
    circled_chars = m.circled[1:-1] if m.circled.startswith("[") else ""
    closers = m.number_closer[1:-1] if m.number_closer.startswith("[") else ""
    brackets = "".join(side[1:-1] for side in (m.number_open, m.number_close) if side.startswith("["))
    patterns = {
        "series_start": rf"A\s*{m.series_open}",
        "route_caption": rf"^{m.route_open}\s*[^\n]{{1,80}}{m.route_arrow}[^\n]{{1,80}}{m.route_close}$",
        "step_marker": rf"^\s*(?:{m.circled}|\d{{1,2}}\s*{m.number_closer})",
        "marker_only": rf"^[\s\d{closers}{brackets}{circled_chars}]+$",
        "toc_entry": rf"^\s*\d{{1,2}}\s*{m.number_closer}\s*\S",
    }
    return re.compile(patterns[name])


def _running_heading_keys(records: Sequence[dict[str, Any]]) -> set[str]:
    """ページ上端・下端の帯に 2 ページ以上同じ文字列で出る Section-header（と同族の見出し）を柱として返す。

    Docling は「１データ連携」「Ａシステム設定－(１)利用者設定」のような柱を Section-header に分類する。
    そのままだと毎ページ section_path が章へ戻され、前の機能の見出しが残る。文字列は
    `_boilerplate_text_key` で比べ、ページ番号だけの差は同一視する。
    """
    pages_by_key: dict[str, set[int]] = {}
    heights_by_key: dict[str, list[float]] = {}
    for record in records:
        if str(record.get("category") or "") not in SECTION_CATEGORIES or not _page_band(record):
            continue
        page = _int_value(record.get("page"))
        text = _record_text(record)
        if page is None or not text:
            continue
        key = _boilerplate_text_key(text)
        pages_by_key.setdefault(key, set()).add(page)
        heights_by_key.setdefault(key, []).append(_record_height(record))
    running = {key for key, pages in pages_by_key.items() if len(pages) >= 2}
    if not running:
        return running
    # 柱が確認できた文書では、同じ帯に同じ高さで 1 ページだけ出る見出し（1 ページしかない章の柱
    # 「Ａマスタ管理－ ( ２ ) 基本設定」）も柱とみなす。文字列が違うだけで章の切り替わりを
    # 通常の見出しにすると、前の章の柱が path の根に残る。
    reference = sorted(h for key in running for h in heights_by_key[key])
    median = reference[len(reference) // 2]
    for key, heights in heights_by_key.items():
        if key not in running and median > 0 and all(abs(h - median) / median <= _RUNNING_HEIGHT_TOLERANCE for h in heights):
            running.add(key)
    return running


def _record_height(record: dict[str, Any]) -> float:
    bbox = record.get("bbox")
    try:
        return float(bbox[3]) - float(bbox[1]) if isinstance(bbox, (list, tuple)) and len(bbox) >= 4 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _page_band(record: dict[str, Any]) -> bool:
    """record の上端がページ上端・下端の帯にあるか。bbox や page_height がなければ False。"""
    bbox = record.get("bbox")
    height = _int_value(record.get("page_height"))
    if str(record.get("coord_system") or "") != "image_top_left" or not isinstance(bbox, (list, tuple)) or len(bbox) < 4 or not height:
        return False
    try:
        top = float(bbox[1]) / float(height)
    except (TypeError, ValueError):
        return False
    return top < _RUNNING_HEADER_TOP or top > _RUNNING_FOOTER_TOP


def _record_raw(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("raw")
    return value if isinstance(value, dict) else {}


def _promote_headings(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Docling の category だけでは決まらない見出し／非見出しを正規化する。

    - 柱（`_running_heading_keys`）は初出だけ章の見出し（`raw.heading_level=0`）として残し、2 回目以降は
      `Page-header` に付け替えて既存のページ定型除外に載せる。
    - 文として終わる Section-header（手順行）は本文へ戻す。番号・丸数字で始まれば `List-item`、それ以外は `Text`。
      見出しのままだと「２ . 顧客情報を入力します。」が番号見出しと判定され、section_path 全体を置き換える。
    - 同じ文書で見出しとして出現した文字列の再出現（目次では Section-header、本文では List-item と
      分類された「２．顧客への区分の登録」）は見出しに寄せる。見出しにならないと前の節の section_path が続き、
      別機能の手順が同じ機能として扱われる。
    - 画面経路の形をした caption も見出しに寄せる。図の外の行だが、図の caption として返ると section_path に
      載らず、経路が Vision 説明にしか残らない。
    入力は変更せず、category を差し替えた写しを返す。
    """
    running = _running_heading_keys(records)
    seen_running: set[str] = set()

    def demoted_category(record: dict[str, Any], text: str) -> str | None:
        if str(record.get("category") or "") not in SECTION_CATEGORIES:
            return None
        key = _boilerplate_text_key(text)
        if key in running:
            if key in seen_running:
                # 上端・下端のどちらでも同じ category にし、繰り返し回数を合算して定型除外に載せる。
                return "Page-header"
            # 初出は章の見出し（level 0）として残す。「８データ連携－〈データ連携１〉」の 1 ページ目の
            # メニュー表や、章ごとに文字列が変わる柱の切り替わりを path の根にする。
            seen_running.add(key)
            return ""
        if _heading_patterns().sentence_heading.search(text):
            return "List-item" if _step_marker_pattern().match(text) else "Text"
        if _marker_only_pattern().match(text):
            return "Text"
        return None

    ordered = sorted(records, key=lambda item: (int(item.get("page") or 0), int(item.get("seq_no") or 0)))
    demotions = {id(r): demoted_category(r, _record_text(r).strip()) for r in ordered}
    toc_ids = _toc_entry_ids(ordered)
    # 再出現の照合は空白と全角 / 半角を無視した鍵で行う（「２．機関の廃止登録」と「2 ．機関の廃止登録」）。
    # 目次・フロー一覧の番号行も鍵に含め、本文側の出現（List-item と分類されたもの）を見出しに寄せる (#714)。
    known = {
        _heading_key(_record_text(r))
        for r in records
        if (str(r.get("category") or "") in SECTION_CATEGORIES and not demotions.get(id(r))) or id(r) in toc_ids
    } - {""}
    # 同系列の章節が見出しとして認識されている文書では、ページ上端の新しい章名が
    # Page-header に分類されていても初出を境界へ戻す。同じ章名の再掲は柱のまま残す。
    numbered = _heading_patterns().numbered_section
    supported_levels = {
        _heading_level(_record_text(r))
        for r in ordered
        if str(r.get("category") or "") in SECTION_CATEGORIES and numbered.match(_record_text(r).strip())
    }
    seen_numbered: set[str] = set()
    numbered_headers: set[int] = set()
    for record in ordered:
        text = _record_text(record).strip()
        match = numbered.match(text)
        if not match or not text[match.end() :].strip() or "\n" in text or _heading_patterns().sentence_heading.search(text):
            continue
        key = _heading_key(text)
        category = str(record.get("category") or "")
        if category == "Page-header" and key not in seen_numbered and _heading_level(text) in supported_levels:
            numbered_headers.add(id(record))
        if category in SECTION_CATEGORIES or id(record) in numbered_headers:
            seen_numbered.add(key)
    promoted: list[dict[str, Any]] = []
    for record in records:
        category = str(record.get("category") or "")
        text = _record_text(record).strip()
        if id(record) in numbered_headers:
            raw = _record_raw(record)
            promoted.append({**record, "category": "Section-header", "raw": {**raw, "promoted_from_category": category}})
            continue
        if demotions.get(id(record)):
            promoted.append({**record, "category": demotions[id(record)]})
            continue
        if demotions.get(id(record)) == "":
            raw = _record_raw(record)
            promoted.append({**record, "raw": {**raw, "heading_level": _CHAPTER_LEVEL}})
            continue
        if id(record) in toc_ids:
            # 目次・フロー一覧の番号行そのものは見出しにしない。見出しになると以降の本文の節がその配下に入る。
            promoted.append({**record, "category": "List-item"} if category in SECTION_CATEGORIES else record)
            continue
        recurring = category in {"Text", "List-item"} and len(text) <= 40 and _heading_key(text) in known
        route_caption = category == "Caption" and bool(_route_caption_pattern().match(text))
        if recurring:
            # 本文側の再出現には印を付ける。現在の節名の再掲（柱に近い行）なら節を変えないため (#764)。
            raw = _record_raw(record)
            promoted.append({**record, "category": "Section-header", "raw": {**raw, "recurring_heading": True}})
            continue
        if route_caption:
            raw = _record_raw(record)
            promoted.append({**record, "category": "Section-header", "raw": {**raw, "heading_source": ROUTE_CAPTION_HEADING_SOURCE}})
            continue
        promoted.append(record)
    return promoted


# 目次・フロー一覧の番号行（「１．営業所登録情報変更」）。短い番号付きの行で、文として終わらない。
def _toc_entry_pattern() -> re.Pattern[str]:
    """目次・フロー一覧の番号行（「１．営業所登録情報変更」）。番号の閉じは言語資源(#2181)。"""
    return _marker_regex(current_pack().hash, "toc_entry")


_TOC_MIN_ENTRIES = 3
_TOC_MIN_REAPPEARING = 2


def _heading_key(text: str) -> str:
    """見出しの再出現を照合する鍵。空白を除き全角 / 半角を同一視する。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or "")))


def _toc_entry_ids(ordered: Sequence[dict[str, Any]]) -> set[int]:
    """目次・フロー一覧とみなすページの番号行の id を返す (#714)。

    同じページに番号付きの短い行が 3 つ以上並び、そのうち 2 つ以上が後のページで見出し（Section-header）
    として再出現するページを目次とみなす。表紙のフロー一覧「１．営業所登録情報変更 ※当資料２ページを参照」が
    再出現の規則で見出しに昇格し、本文の節がその配下に入るのを防ぐ。番号の形だけで判定し、語は見ない。
    """
    by_page: dict[int, list[dict[str, Any]]] = {}
    for record in ordered:
        by_page.setdefault(int(record.get("page") or 0), []).append(record)
    later_headings: dict[str, int] = {}
    for record in ordered:
        if str(record.get("category") or "") in SECTION_CATEGORIES:
            key = _heading_key(_record_text(record))
            later_headings.setdefault(key, int(record.get("page") or 0))
    ids: set[int] = set()
    for page, page_records in by_page.items():
        entries = [
            r
            for r in page_records
            if str(r.get("category") or "") in {"List-item", "Text", *SECTION_CATEGORIES}
            and len(_record_text(r).strip()) <= 40
            and _toc_entry_pattern().match(_record_text(r).strip())
            and not _heading_patterns().sentence_heading.search(_record_text(r).strip())
        ]
        if len(entries) < _TOC_MIN_ENTRIES:
            continue
        reappearing = sum(1 for r in entries if later_headings.get(_heading_key(_record_text(r)), page) > page)
        if reappearing >= _TOC_MIN_REAPPEARING:
            ids.update(id(r) for r in entries)
    return ids


def _heading_level(text: str) -> int | None:
    """見出しの番号表記から階層の深さを推定します。推定できない場合は None を返します。

    番号の体系（「第 N 章」「N.」「N.N」「N.N.N」「（N）」など。漢数字・全角数字・空白の有無を問わない）が
    見出しの深さの第一の根拠です。Docling の見出しの level（`SectionHeaderItem.level`）は PDF の解析では
    すべて 1 になり深さを表さない（評価の PDF の 18 件で全件 1。#1517）ため使いません。番号の無い見出しは
    `_updated_section_path` が形（囲み記号・無印）と並びから置き場所を決めます。①-⑳ は NFKC で
    数字へ変わるため、正規化前の文字で判定します。
    """
    stripped = str(text or "").strip()
    if re.match(r"[①-⑳]", stripped):
        return _CIRCLED_LEVEL
    normalized = unicodedata.normalize("NFKC", stripped)
    for pattern, level in _heading_patterns().levels:
        if pattern.match(normalized):
            return level
    dotted = _DOTTED_HEADING_PATTERN.match(normalized)
    return (dotted.group(1) or dotted.group(2)).count(".") + 1 if dotted else None


def _is_part_heading(text: str) -> bool:
    """「第 N 編」「第 N 部」の見出し（章より上の区分）か。"""
    return bool(_heading_patterns().part.match(unicodedata.normalize("NFKC", str(text or "").strip())))


def _heading_form(text: str) -> str:
    """階層を推定できない見出しの「形」（囲み記号の種類か無印）。同じ形の直近の見出しを兄弟とみなす。"""
    stripped = str(text or "").strip()
    return stripped[0] if stripped and stripped[0] in marker_patterns().heading_form_openers else ""


def _heading_source(record: dict[str, Any]) -> str:
    """見出し record の出所。section_path_sources に残し、下流が見出しの信頼度を扱えるようにする (#814)。"""
    raw = _record_raw(record)
    if _int_value(raw.get("heading_level")) == _CHAPTER_LEVEL:
        return RUNNING_HEAD_HEADING_SOURCE
    if raw.get("recurring_heading"):
        return PROMOTED_TEXT_HEADING_SOURCE
    if raw.get("heading_source") == ROUTE_CAPTION_HEADING_SOURCE:
        return ROUTE_CAPTION_HEADING_SOURCE
    if str(record.get("category") or "") == "Title":
        return TITLE_HEADING_SOURCE
    return SECTION_HEADER_HEADING_SOURCE


def _updated_section_path(
    path: Sequence[str],
    levels: Sequence[int | None],
    depths: Sequence[int],
    text: str,
    level: int | None = None,
    *,
    sources: Sequence[str] = (),
    source: str = "",
) -> tuple[list[str], list[int | None], list[int], list[str]]:
    """見出し text を受けて section_path と、その推定レベル列・実効深さ列・出所列を更新する。

    実効深さ（depth）は path 上での入れ子の深さで、レベルを推定できた見出しはレベル自体、推定できない
    見出しは直上の見出しの深さ + 1 か、兄弟とみなした見出しと同じ深さになる。新しい見出しは自分以上の
    深さの見出しを末尾から外してから載る（stack）。

    - 英字系列の先頭（「A 【画面説明】」）は直近の非英字見出しの直下に新しい系列を始める。
      「D 【遷移画面】 > 〔納品書〕 > A 【画面説明】」のように、階層不明の見出しの下で系列が再開する
      操作説明書の形を保つため。系列の続き（B、C）は直近の英字見出しと同じ深さに並ぶ。
    - 同じ見出しの再出現（帳票の 2 ページ目など）は、その見出しへ戻る。
    - 階層不明の見出しは、同じ形（【…】、〔…〕、無印）の直近の階層不明見出しを兄弟として置き換える
      （「【修正方法１】」の次の「【修正方法２】」）。無印は番号見出しをまたいで遡らず、先頭の見出し
      （文書の表題）は兄弟の候補にしない。同じ形がなければ直上の見出しの配下に入る。
    - 「第 N 編・部」の配下の番号の見出し（章・「N.N」など）は、番号から決まる深さより 1 段深くする。
    """
    level = _heading_level(text) if level is None else level
    normalized = unicodedata.normalize("NFKC", text.strip())
    cut = len(path)
    depth: int | None = None
    if level == _LETTERED_LEVEL and _series_start_pattern().match(normalized):
        cut = next((index + 1 for index in range(len(path) - 1, -1, -1) if levels[index] != _LETTERED_LEVEL), 0)
        depth = depths[cut - 1] + 1 if cut else 0
    elif level == _LETTERED_LEVEL:
        depth = next((depths[index] for index in range(len(path) - 1, -1, -1) if levels[index] == _LETTERED_LEVEL), None)
    elif level is not None:
        depth = level
        # 「第 N 編・部」の配下では、章以下の番号の見出しを 1 段深くする（編・部と章が同じ深さに並ばない）。
        # 柱の章（level 0）は path の根のため対象にしない。
        if level != _CHAPTER_LEVEL and not _is_part_heading(normalized):
            depth += int(any(_is_part_heading(item) for item in path))
    elif text in path:
        cut = list(path).index(text)
        depth = depths[cut]
    else:
        form = _heading_form(text)
        for index in range(len(path) - 1, 0, -1):
            if levels[index] is not None:
                if not form:
                    break
                continue
            if _heading_form(path[index]) == form:
                depth = depths[index]
                break
    if depth is None:
        depth = depths[cut - 1] + 1 if cut else 0
    else:
        while cut and depths[cut - 1] >= depth:
            cut -= 1
    padded_sources = list(sources) + [SECTION_HEADER_HEADING_SOURCE] * (len(path) - len(sources))
    return (
        (list(path[:cut]) + [text])[-_SECTION_PATH_LIMIT:],
        (list(levels[:cut]) + [level])[-_SECTION_PATH_LIMIT:],
        (list(depths[:cut]) + [depth])[-_SECTION_PATH_LIMIT:],
        (padded_sources[:cut] + [source or SECTION_HEADER_HEADING_SOURCE])[-_SECTION_PATH_LIMIT:],
    )


# section_path の状態（path・推定レベル列・実効深さ列・出所列）。`_updated_section_path` の入出力と同じ形。
SectionPathState = tuple[list[str], list[int | None], list[int], list[str]]


def _section_path_state_after(state: SectionPathState, record: dict[str, Any]) -> SectionPathState:
    """layout record を 1 件読んだ後の section_path の状態。見出しでなければ state をそのまま返す。

    chunk の section_path（`builder`）と、章節ナビゲーションの元になる要素の section_path
    （`layout_section_paths`）が同じ規則で見出しの木を作るための 1 か所（#1517）。record は
    `_promote_headings` を通したものを読み順に渡す。
    """
    if str(record.get("category") or "") not in SECTION_CATEGORIES:
        return state
    text = _record_text(record)
    if not text:
        return state
    path, levels, depths, sources = state
    raw = _record_raw(record)
    if raw.get("recurring_heading") and _heading_key(text) in {_heading_key(h) for h in path}:
        # 本文中に現在の節名がそのまま再掲されている（「Ａマスタ管理-（ 4 ）区分管理」）。節は変わらない。
        # 見出しにすると子見出しになり、以降の手順が別の機能ユニットに割れる (#764)。
        return state
    return _updated_section_path(
        path,
        levels,
        depths,
        text,
        level=_int_value(raw.get("heading_level")),
        sources=sources,
        source=_heading_source(record),
    )


def layout_section_paths(records: Sequence[dict[str, Any]]) -> dict[str, list[str]]:
    """layout record の id → その record が属する section_path（chunk の section_path と同じ規則。#1517）。

    解析エンジン（Docling）の要素の section_path に使い、章節ナビゲーションの見出しの木を chunk の
    section_path とそろえる。見出しの record はその見出し自身を末尾に含む path、本文の record は直前の
    見出しの path になる。最初の見出しより前の record は空の path。入力は変更しない。
    """
    state: SectionPathState = ([], [], [], [])
    paths: dict[str, list[str]] = {}
    ordered = sorted(_promote_headings(records), key=lambda item: (int(item.get("page") or 0), int(item.get("seq_no") or 0)))
    for record in ordered:
        state = _section_path_state_after(state, record)
        record_id = str(record.get("id") or "")
        if record_id:
            paths[record_id] = list(state[0])
    return paths


def heading_level_candidates(records: Sequence[dict[str, Any]]) -> list[tuple[str, str, int | None]]:
    """見出しの階層を推定するとき（backend の LLM の段。#2277）に渡す見出しを読み順に返す。

    要素は (record の id, 見出しの文字列, 番号の体系から決まる階層。決まらなければ None)。見出しは
    `_promote_headings` を通したもの（chunk の section_path と同じ判定）で、深さが既に決まっている
    見出し（柱の初出・しおりの深さ）と、本文中の見出しの再掲は除く。入力は変更しない。
    """
    ordered = sorted(_promote_headings(records), key=lambda item: (int(item.get("page") or 0), int(item.get("seq_no") or 0)))
    candidates: list[tuple[str, str, int | None]] = []
    for record in ordered:
        if str(record.get("category") or "") not in SECTION_CATEGORIES:
            continue
        record_id = str(record.get("id") or "")
        text = _record_text(record).strip()
        raw = _record_raw(record)
        if not record_id or not text or raw.get("recurring_heading") or _int_value(raw.get("heading_level")) is not None:
            continue
        candidates.append((record_id, text, _heading_level(text)))
    return candidates


def _section_unit(path: Sequence[str]) -> tuple[str, ...]:
    """section_path のうち番号付き機能見出しまで（機能ユニット）。番号見出しがなければ空。"""
    return _section_unit_cached(tuple(str(item) for item in path), current_pack().hash)


@lru_cache(maxsize=4096)
def _section_unit_cached(path: tuple[str, ...], _pack_hash: str = "") -> tuple[str, ...]:
    # 親の区切り判定が child ごとに同じ path で呼ぶため、path のタプルを key に結果を共有する (#867)。
    # 見出しの階層は言語資源で変わるので、言語資源の hash も key にする (#2181)。
    for index in range(len(path) - 1, -1, -1):
        level = _heading_level(path[index])
        if level is not None and level <= _UNIT_MAX_LEVEL:
            return tuple(path[: index + 1])
    return ()


def _merge_section_paths(children: Sequence[DocumentChunk]) -> list[str]:
    merged: list[str] = []
    for child in children:
        for item in child.metadata.get("section_path") or []:
            if item and item not in merged:
                merged.append(item)
    return merged[-_SECTION_PATH_LIMIT:]


def _merge_section_path_sources(children: Sequence[DocumentChunk]) -> list[str]:
    """親の section_path（_merge_section_paths）と同じ順で、各見出しの出所を子から引き継ぐ (#814)。"""
    merged = _merge_section_paths(children)
    sources: dict[str, str] = {}
    for child in children:
        path = child.metadata.get("section_path") or []
        child_sources = child.metadata.get("section_path_sources") or []
        for item, source in zip(path, child_sources, strict=False):
            sources.setdefault(item, source)
    return [sources.get(item, SECTION_HEADER_HEADING_SOURCE) for item in merged]


def _merge_logical_page_labels(children: Sequence[DocumentChunk]) -> dict[str, int | str]:
    labels: dict[str, int | str] = {}
    for child in children:
        for page, label in ((child.metadata.get("layout") or {}).get("logical_page_labels") or {}).items():
            labels[str(page)] = label
    return labels
