"""列挙・網羅を求める質問の要求（件数・章の範囲・読む範囲）と、根拠で満たせた項目の数え方（#1503）。

CRAG の十分性の判定（評価器）は「見えている根拠で答えられるか」を判定するため、「すべて挙げて」の問で、
ほかにも該当する記載が残っているかを判定できない（見えている数件で「足りる」と答える）。そこで、

- 質問から列挙の要求を決定的に取り出す（``enumeration_request``）: 網羅の語（「すべて」「漏れなく」「章ごとに」）、
  示された件数（「5 つすべて」）、章の範囲（「第 2 章から第 6 章まで」）、読む範囲（文書全体か、当たった章節か）。
- 評価器には項目ごとの根拠（``enumerated_items``。項目・根拠の chunk_uid・根拠の原文の写し）を出させ、プログラムで
  原文の写しが根拠の本文にあるかを確かめてから、件数・章の範囲の欠けを数える（``enumeration_check``）。評価器の
  ``status`` は件数・範囲の無い問でだけ使い、件数・範囲のある問では数えた結果を正にする（自己申告に頼らない）。
- 件数も範囲も無い網羅の問は、根拠の数からは足りるかを決められない。章節の拡張（``section_expansion``）は、
  評価器が足りると判定しても、読む範囲（``minimum_scope``。当たった章節 = ``leaf``、文書全体 = ``document``）まで
  読んでから判定し直す。

LLM・DB を呼ばない（決定論のテストができる）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal

from rag_parser_core.language import current_pack
from rag_parser_core.language.text import alternation, char_class, parse_number, text_patterns

# 列挙の観点の名前（``task_contract`` の required_aspects と CRAG の aspect_checks）。
ENUMERATION_ASPECT = "enumeration_coverage"

# 網羅を求める語・一覧を求める述語・範囲の語は言語資源（enumeration_cues。#2181 段 4）。


@dataclass(frozen=True)
class _NumberPatterns:
    # 示された件数（「5 つすべて」「6 件」）。助数詞は言語資源（「点」は「点検」と紛れるので既定に入れない）。
    count: re.Pattern[str]
    # 章の番号と、章の範囲（「第 2 章から第 6 章まで」）。前置き・単位・範囲のつなぎは言語資源（#2181）。
    chapter: re.Pattern[str]
    chapter_range: re.Pattern[str]
    # 網羅の語（「すべての〇〇に適用されますか」のような名詞の修飾は網羅の要求ではないので除く）と、一覧を求める述語
    # （網羅の語と組で列挙の要求とみなす）。
    exhaustive: re.Pattern[str]
    listing: re.Pattern[str]
    each: re.Pattern[str]
    numbered_section: re.Pattern[str]
    # 文書全体を範囲にする語と、文書の名前で範囲を示す語（「〜要領で、…をすべて」）。
    document_wide: re.Pattern[str]
    document_named: re.Pattern[str]


_number_patterns_by_hash: dict[str, _NumberPatterns] = {}


def _number_patterns() -> _NumberPatterns:
    pack = current_pack()
    cached = _number_patterns_by_hash.get(pack.hash)
    if cached is not None:
        return cached
    text = text_patterns(pack)
    counters = alternation(pack.words("numbers", "count_counters"))
    connectors = alternation(pack.words("numbers", "range_connectors"))
    chapter = rf"{text.prefix}\s*({text.number})\s*{text.chapter_units}"

    def alt(key: str) -> str:
        return alternation(pack.words("enumeration_cues", key))

    comma = char_class(pack.words("question_cues", "comma_chars"))
    patterns = _NumberPatterns(
        count=re.compile(rf"({text.number})\s*{counters}"),
        chapter=re.compile(chapter),
        chapter_range=re.compile(rf"{chapter}\s*{connectors}\s*{text.prefix}?\s*({text.number})\s*{text.chapter_units}"),
        exhaustive=re.compile(
            rf"{alt('exhaustive_quantifiers')}(?!{alt('quantifier_modifier_particles')})|{alt('exhaustive_words')}"
            rf"|{alt('per_unit_words')}"
        ),
        listing=re.compile(alt("listing_words")),
        each=re.compile(alt("each_words")),
        numbered_section=re.compile(rf"\d+(?:\.\d+)+\s*{text.section_units}|{text.prefix}\s*\d+\s*{text.section_units}"),
        document_wide=re.compile(alt("document_wide_words")),
        document_named=re.compile(rf"{alt('document_name_words')}{alt('document_inside_words')}?{alt('document_scope_particles')}{comma}"),
    )
    if len(_number_patterns_by_hash) >= 16:
        _number_patterns_by_hash.clear()
    _number_patterns_by_hash[pack.hash] = patterns
    return patterns


# 件数の上限（質問の数字の読み違いで無理な件数を求めない）。
MAX_EXPECTED_COUNT = 50
# 章の範囲の上限。
MAX_CHAPTERS = 30
# 評価器に挙げさせる項目の上限（章の範囲の問）と、件数も範囲も無い問で挙げさせる数（足りるかは評価器の判定と
# 読む範囲で決めるので、根拠のある項目が確かめられれば足りる）。
MAX_LISTED_ITEMS = 20
OPEN_LISTED_ITEMS = 5

Scope = Literal["section", "document"]


@dataclass(frozen=True)
class EnumerationRequest:
    """質問の列挙の要求。``expected_count`` は示された件数、``chapters`` は網羅する章の番号。"""

    scope: Scope
    expected_count: int | None = None
    chapters: tuple[int, ...] = ()

    @property
    def countable(self) -> bool:
        """根拠の数（件数・章）で足りるかを決められる要求か。"""
        return self.expected_count is not None or bool(self.chapters)

    @property
    def minimum_scope(self) -> str | None:
        """足りると判定する前に読む範囲。``leaf`` / ``document`` / None（範囲の規則なし）。

        件数だけの問は数えた結果で決める（None）。2 つ以上の章の範囲の問は、範囲の章が 1 つの章節に入らないため
        文書全体まで読んでから判定する（下の段で評価器を呼ばない）。件数も範囲も無い問は、範囲の語で決める。
        """
        if len(self.chapters) >= 2:
            return "document"
        if self.countable:
            return None
        return "document" if self.scope == "document" else "leaf"

    @property
    def max_items(self) -> int:
        """評価器に挙げさせる項目の上限（判定に要る数だけ。出力の token と時間を増やしすぎない）。"""
        if self.expected_count is not None:
            return min(MAX_EXPECTED_COUNT, self.expected_count + 2)
        if self.chapters:
            return min(MAX_LISTED_ITEMS, 2 * len(self.chapters))
        return OPEN_LISTED_ITEMS

    def to_payload(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "expected_count": self.expected_count,
            "chapters": list(self.chapters),
            "minimum_scope": self.minimum_scope,
            "max_items": self.max_items,
        }


def _number(value: str) -> int | None:
    return parse_number(value)


def enumeration_request(question: str) -> EnumerationRequest | None:
    """列挙・網羅を求める質問なら要求を返す（求めなければ None）。"""
    # 言語資源が変われば読み直す（cache の key に言語資源の hash を入れる）。
    return _enumeration_request(unicodedata.normalize("NFKC", str(question or "")), current_pack().hash)


@lru_cache(maxsize=256)
def _enumeration_request(text: str, _pack_hash: str) -> EnumerationRequest | None:
    numbers = _number_patterns()
    if not (numbers.exhaustive.search(text) and numbers.listing.search(text)):
        return None
    counts = [number for raw in numbers.count.findall(text) if (number := _number(raw)) is not None and 2 <= number <= MAX_EXPECTED_COUNT]
    chapters: tuple[int, ...] = ()
    span = numbers.chapter_range.search(text)
    bounds = (_number(span.group(1)), _number(span.group(2))) if span else (None, None)
    if bounds[0] is not None and bounds[1] is not None:
        start, end = sorted((bounds[0], bounds[1]))
        if end - start < MAX_CHAPTERS:
            chapters = tuple(range(start, end + 1))
    named = tuple(dict.fromkeys(n for value in numbers.chapter.findall(text) if (n := _number(value)) is not None))
    if not chapters and len(named) >= 2 and numbers.each.search(text):
        chapters = named
    if len(chapters) >= 2 or numbers.document_wide.search(text):
        scope: Scope = "document"
    elif not named and not numbers.numbered_section.search(text) and numbers.document_named.search(text):
        scope = "document"
    else:
        scope = "section"
    return EnumerationRequest(scope=scope, expected_count=max(counts) if counts else None, chapters=chapters)


def minimum_scope(question: str) -> str | None:
    """章節の拡張で、足りると判定する前に読む範囲（``EnumerationRequest.minimum_scope``）。"""
    request = enumeration_request(question)
    return request.minimum_scope if request is not None else None


def _folded(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text or ""))).lower()


def _chapter_of(record: Any) -> int | None:
    """根拠の見出し（``section_path``）の最も深い「第 N 章」。見出しは配列か「A > B」の文字列。

    解析の見出しの深さが崩れて、後ろの章が前の章の節の下に入ることがある（「第 3 章 > 3.3 > 第 5 章」）ため、
    浅い章ではなく最も深い章をその根拠の章にする。
    """
    path = (getattr(record, "metadata", None) or {}).get("section_path")
    if isinstance(path, str):
        path = path.split(" > ")
    joined = unicodedata.normalize("NFKC", " > ".join(str(item) for item in path or () if item))
    found = [n for value in _number_patterns().chapter.findall(joined) if (n := _number(value)) is not None]
    return found[-1] if found else None


@dataclass(frozen=True)
class _Unit:
    text: str
    chapters: frozenset[int]


def _units(context: Any, records: Sequence[Any]) -> dict[str, _Unit]:
    """候補の chunk_uid → 根拠の本文（親・同じ親の子）と章。子の uid も親の単位に写す。"""
    units: dict[str, _Unit] = {}

    def chapters(items: Sequence[Any]) -> frozenset[int]:
        return frozenset(number for item in items if (number := _chapter_of(item)) is not None)

    for parent in getattr(context, "evidence_tree", ()) or ():
        members = [parent.record, *(child.record for child in parent.children)]
        unit = _Unit(text="\n".join(str(getattr(item, "text", "") or "") for item in members), chapters=chapters(members))
        for item in members:
            uid = str(getattr(item, "chunk_uid", "") or getattr(item, "id", "") or "")
            if uid:
                units.setdefault(uid, unit)
    for record in records:
        uid = str(getattr(record, "chunk_uid", "") or getattr(record, "id", "") or "")
        if uid and uid not in units:
            units[uid] = _Unit(text=str(getattr(record, "text", "") or ""), chapters=chapters([record]))
    return units


def _field(item: Any, key: str) -> Any:
    return item.get(key) if isinstance(item, Mapping) else getattr(item, key, None)


def enumeration_check(
    question: str,
    checks: Sequence[Mapping[str, Any]],
    items: Sequence[Any],
    context: Any,
    records: Sequence[Any],
    canonical: Callable[[str], str],
    *,
    unverified_items: Sequence[Any] = (),
) -> list[dict[str, Any]]:
    """評価器の観点の判定に、列挙の観点（``ENUMERATION_ASPECT``）の数えた結果を入れて返す。

    ``items`` は評価器の ``enumerated_items``（``item`` / ``source_ids`` / ``quote``）。原文の写し（``quote``）が根拠の
    本文（親と同じ親の子）にある項目だけを数え、同じ原文の項目は 1 つにする。``unverified_items`` は呼び出し側で
    原文の写しを確かめられなかった項目（数えないが、原文を確かめられない項目として扱う）。

    - 件数のある問: 数えた項目が件数に足りなければ missing。
    - 章の範囲のある問: 範囲の章のうち、数えた項目の根拠の見出しに無い章があれば missing。
    - どちらも無い問: 評価器の status（根拠のある項目が 1 つも無ければ missing）。読む範囲の規則は章節の拡張が持つ。

    原文の写しを確かめられないこと（写しの照合の失敗）は、項目が無いことの根拠にならない。足りない分を原文を
    確かめられない項目で埋められ、評価器が不足と判定していなければ、missing にせず unknown にする
    （``shortfall_cause`` = ``quote_unverified``）。missing は、確かめた項目で数えても足りないとき
    （``verified_shortfall``）、評価器が網羅を確かめていないとき（``grader_status``）、評価器が項目を挙げていないとき
    （``no_items``）だけにする。

    列挙の問でなければ ``checks`` をそのまま返す。
    """
    request = enumeration_request(question)
    result = [dict(item) for item in checks]
    if request is None:
        return result
    units = _units(context, records)
    counted: list[tuple[str, str]] = []  # (原文の写しの形, 根拠の uid)
    seen: set[str] = set()
    # 原文を確かめられない項目: 写しの形 → 根拠の章（根拠の chunk を候補に結べなければ None = どの章か分からない）。
    unverified: dict[str, frozenset[int] | None] = {}

    def note_unverified(index: int, quote: str, sources: Sequence[str]) -> None:
        key = quote if len(quote) >= 2 else f"#{index}"
        if key in seen or key in unverified:
            return
        unverified[key] = frozenset(number for uid in sources for number in units[uid].chapters) if sources else None

    for index, item in enumerate(items):
        quote = _folded(_field(item, "quote") or "")
        sources = [uid for value in _field(item, "source_ids") or () if (uid := canonical(str(value))) and uid in units]
        if len(quote) >= 2 and quote in seen:
            continue
        found = next((uid for uid in sources if quote in _folded(units[uid].text)), "") if len(quote) >= 2 else ""
        if not found:
            note_unverified(index, quote, sources)
            continue
        seen.add(quote)
        unverified.pop(quote, None)
        counted.append((quote, found))
    for index, item in enumerate(unverified_items, start=len(items)):
        quote = _folded(_field(item, "quote") or "")
        sources = [uid for value in _field(item, "source_ids") or () if (uid := canonical(str(value))) and uid in units]
        note_unverified(index, quote, sources)
    source_ids = list(dict.fromkeys(uid for _quote, uid in counted))
    reasons: list[str] = []
    llm = [item for item in result if item.get("aspect") == ENUMERATION_ASPECT]
    claimed = llm[0].get("status") if len(llm) == 1 else None
    cause: str | None = None
    if request.countable:
        ok = True
        closable = bool(unverified)
        if request.expected_count is not None:
            if len(counted) < request.expected_count:
                ok = False
                closable = closable and len(counted) + len(unverified) >= request.expected_count
            reasons.append(f"要求 {request.expected_count} 件に対し、原文を確かめた項目 {len(counted)} 件")
        if request.chapters:
            covered = {number for uid in source_ids for number in units[uid].chapters}
            lacking = [number for number in request.chapters if number not in covered]
            if lacking:
                ok = False
                possible = list(unverified.values())
                closable = closable and (None in possible or set(lacking) <= {n for chapters in possible if chapters for n in chapters})
                reasons.append("根拠の無い章: " + "、".join(f"第 {number} 章" for number in lacking))
            else:
                reasons.append("範囲の章（" + "、".join(f"第 {number} 章" for number in request.chapters) + "）すべてに根拠がある")
        if ok:
            status = "supported"
        elif not closable:
            status, cause = "missing", "verified_shortfall"
        elif claimed == "missing":
            status, cause = "missing", "grader_status"
        else:
            status, cause = "unknown", "quote_unverified"
    else:
        if claimed == "supported" and counted:
            status = "supported"
        elif claimed == "supported" and unverified:
            status, cause = "unknown", "quote_unverified"
        else:
            status, cause = "missing", "grader_status" if claimed != "supported" else "no_items"
        reasons.append(f"原文を確かめた項目 {len(counted)} 件" + ("" if claimed == "supported" else "（評価器は網羅を確かめていない）"))
    if unverified:
        reasons.append(
            f"原文を確かめられない項目 {len(unverified)} 件"
            + ("（写しの照合の失敗は項目が無い根拠にならないため、不足を確定しない）" if cause == "quote_unverified" else "")
        )
    entry = {
        "aspect": ENUMERATION_ASPECT,
        "status": status,
        "source_ids": source_ids if status == "supported" else [],
        "reason": "。".join(reasons),
        "counted_items": len(counted),
        "unverified_items": len(unverified),
        "shortfall_cause": cause,
        "expected_count": request.expected_count,
        "chapters": list(request.chapters),
    }
    return [*(item for item in result if item.get("aspect") != ENUMERATION_ASPECT), entry]


__all__ = [
    "ENUMERATION_ASPECT",
    "EnumerationRequest",
    "enumeration_check",
    "enumeration_request",
    "minimum_scope",
]
