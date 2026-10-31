"""項目抽出の入力の単位(入力の予算に収まる本文の区切り)と、単位ごとの候補の統合(#2720)。

項目抽出は文書の全体から項目の値を探す。本文を 1 回の要求にまとめるとモデルの入力の上限を
超えるため、解析の成果物の要素(見出し・段落・表)の境界で、入力の予算に収まる単位に分ける。

- **予算**: token の数で決める。tokenizer を持たないため、platform の ``estimate_input_tokens``
  (ASCII は 4 字で 1 token、それ以外は 1 字で 1 token)で見積もる。実際の token は見積もりより
  多いことがある(合成の入力の実測で、まれな漢字が続く本文は見積もりの約 1.8 倍)ため、予算は
  モデルの入力の上限の割合(設定)で余裕を残す。文字数を token の数として扱わない。
- **分割**: 要素の順に詰め、予算を超える要素は行・文・文字の順に分ける。表は表頭(Markdown の
  表の 1・2 行目)を各部分に付け直す。単位の頭には「文書の一部 i/n」と見出しの位置を付け、前の
  単位の末尾を重ね(境界をまたぐ値のため)、重ねた範囲と新しく読む範囲を分けて持つ。
  検索用の chunk・解析の segment とは目的と予算が違うため、境界・件数をそろえない。
- **統合**: 項目の定義は 1 項目 1 値(単値)。単位ごとの候補を型に正規化して重ね、同じ値の
  根拠(要素か単位)の数が多い値、同じなら文書の前にある値を採る。最後の値・confidence の
  高い値で置き換えない。違う値が残った項目は競合として数える(値はログに出さない)。

本文・値はこのモジュールの外へログとして出さない。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field

from pr_system_settings.model import estimate_input_tokens, input_token_budget
from rag_parser_core.extraction import is_derived_content

from app.rag.ingestion_prompts import FIELD_EXTRACTION_PART_HEADER, FIELD_EXTRACTION_PART_PATH
from app.schemas.extraction import ExtractionField, StructuredExtraction

# 前の単位の末尾から重ねる量(単位の本文の予算に対する割合)。境界をまたぐ値(項目名と値が
# 別の要素に分かれるなど)を両方の単位で読めるようにする。
OVERLAP_RATIO = 0.1
# 単位の本文に使える予算の下限(tokens)。指示・項目の定義で予算を使い切ると、本文が入らない。
MIN_UNIT_TEXT_TOKENS = 256
# 本文の外側(system の指示・本文を包む文・メッセージと JSON の枠)の見積もりに足す余地(tokens)。
PROTOCOL_ALLOWANCE_TOKENS = 512
# 文の区切り(分割の 2 段目。行で分けても予算を超える長い行を、文の終わりで分ける)。
_SENTENCE_END = re.compile(r"(?<=[。．！？!?])|(?<=\.\s)")
_MARKDOWN_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


@dataclass(frozen=True)
class FieldExtractionAtom:
    """単位を作る最小の本文(1 要素か、大きい要素を分けた 1 部分)。"""

    text: str
    tokens: int
    element_id: str | None = None
    page_number: int | None = None
    section_path: tuple[str, ...] = ()


@dataclass(frozen=True)
class FieldExtractionUnit:
    """項目抽出の 1 回の要求に渡す本文。

    ``atoms`` はこの単位で新しく読む範囲、``overlap`` は前の単位の末尾から重ねた範囲(文脈)。
    ``index`` / ``total`` は分割の計画の位置(予算の超過で分け直した単位は元の位置を引き継ぎ、
    ``part`` で区別する)。
    """

    index: int
    total: int
    atoms: tuple[FieldExtractionAtom, ...]
    overlap: tuple[FieldExtractionAtom, ...] = ()
    part: tuple[int, ...] = ()
    text: str = ""
    tokens: int = 0

    @property
    def order(self) -> tuple[int, ...]:
        """文書の中の順(候補の前後を比べる)。"""
        return (self.index, *self.part)

    @property
    def label(self) -> str:
        """ログ・進捗の名前(位置だけ。本文を含めない)。"""
        return ".".join(str(number + 1) for number in self.order)

    @property
    def element_ids(self) -> tuple[str, ...]:
        return tuple(atom.element_id for atom in self.atoms if atom.element_id)

    @property
    def pages(self) -> tuple[int, ...]:
        return tuple(sorted({atom.page_number for atom in self.atoms if atom.page_number}))


@dataclass(frozen=True)
class FieldValueCandidate:
    """1 つの単位の応答から読んだ 1 項目の値(``raw_value`` はモデルが返した表記)。"""

    field: ExtractionField
    raw_value: str


@dataclass(frozen=True)
class UnitCandidates:
    """1 つの単位の応答から読んだ候補と、その単位。"""

    unit: FieldExtractionUnit
    candidates: Sequence[FieldValueCandidate]


@dataclass(frozen=True)
class FieldMergeResult:
    """統合の結果。``conflict_fields`` は違う値が残った項目の名前(値は持たない)。"""

    fields: list[ExtractionField]
    candidate_count: int = 0
    conflict_fields: list[str] = field(default_factory=list)


def field_extraction_text_budget(
    *, max_input_tokens: int, percent: int, fixed_texts: Sequence[str]
) -> int:
    """1 つの単位の本文に使える予算(tokens)。

    モデルの入力の上限の割合(``percent``)から、毎回送る文(指示・項目の定義・応答の schema・
    system の指示)の見積もりと枠の余地を引く。残りは出力の枠と見積もりの誤差の余裕に使う。
    """
    total = input_token_budget(max_input_tokens, percent)
    fixed = sum(estimate_input_tokens(text) for text in fixed_texts)
    return total - fixed - PROTOCOL_ALLOWANCE_TOKENS


def plan_field_extraction_units(
    extraction: StructuredExtraction, *, text_budget_tokens: int
) -> list[FieldExtractionUnit]:
    """解析の成果物の要素を、本文の予算(tokens)に収まる単位に分ける。

    本文の無い抽出は空の list。すべての要素の本文がいずれかの単位の ``atoms`` にちょうど 1 回
    入る(重ねた範囲は ``overlap`` に別に持つ)。
    """
    budget = max(MIN_UNIT_TEXT_TOKENS, int(text_budget_tokens))
    atoms = _extraction_atoms(extraction, budget)
    return _group_atoms(atoms, budget, index=None, total=None, part=())


def split_field_extraction_unit(
    unit: FieldExtractionUnit, *, text_budget_tokens: int
) -> list[FieldExtractionUnit]:
    """モデルが入力の上限の超過で拒んだ単位を、小さい予算で分け直す(重ねた範囲は外す)。

    分け直せない(予算が下限を下回る・1 つにしかならない)ときは空の list。
    """
    budget = int(text_budget_tokens)
    if budget < MIN_UNIT_TEXT_TOKENS:
        return []
    atoms: list[FieldExtractionAtom] = []
    for atom in unit.atoms:
        atoms.extend(_split_atom(atom, budget - _header_tokens(atom) - 1))
    parts = _group_atoms(atoms, budget, index=unit.index, total=unit.total, part=unit.part)
    return parts if len(parts) > 1 else []


def merge_field_candidates(
    results: Sequence[UnitCandidates], field_names: Sequence[str]
) -> FieldMergeResult:
    """単位ごとの候補を、項目の定義の順(``field_names``)に 1 項目 1 値へまとめる。

    値には書かれた要素(と頁)を出典として付ける。見つからなければ単位の頁(1 頁のときだけ)。
    """
    names = list(field_names)
    groups: dict[str, dict[str, _ValueGroup]] = {name.casefold(): {} for name in names}
    candidate_count = 0
    for result in sorted(results, key=lambda item: item.unit.order):
        for candidate in result.candidates:
            name_key = candidate.field.name.casefold()
            if name_key not in groups:
                continue
            candidate_count += 1
            element_id, page = _locate(candidate.raw_value, result.unit)
            value_key = _value_key(candidate.field)
            group = groups[name_key].get(value_key)
            if group is None:
                groups[name_key][value_key] = group = _ValueGroup(
                    first_order=result.unit.order,
                    value=candidate.field.model_copy(
                        update={"source_element_id": element_id, "page_number": page}
                    ),
                )
            group.evidence.add(element_id or f"unit:{result.unit.label}")
            if candidate.field.confidence is not None and (
                group.confidence is None or candidate.field.confidence > group.confidence
            ):
                group.confidence = candidate.field.confidence
    fields: list[ExtractionField] = []
    conflicts: list[str] = []
    for name in names:
        values = groups.get(name.casefold()) or {}
        if not values:
            continue
        chosen = min(values.values(), key=lambda item: (-len(item.evidence), item.first_order))
        fields.append(chosen.value.model_copy(update={"confidence": chosen.confidence}))
        if len(values) > 1:
            conflicts.append(chosen.value.name)
    return FieldMergeResult(
        fields=fields, candidate_count=candidate_count, conflict_fields=conflicts
    )


@dataclass
class _ValueGroup:
    first_order: tuple[int, ...]
    value: ExtractionField
    evidence: set[str] = field(default_factory=set)
    confidence: float | None = None


def _value_key(value: ExtractionField) -> str:
    """同じ値かを比べる形(型ごとの正規化の後の値を、幅・大文字小文字・空白をそろえて比べる)。"""
    text = unicodedata.normalize("NFKC", value.value).casefold()
    return re.sub(r"\s+", "", text)


def _locate(raw_value: str, unit: FieldExtractionUnit) -> tuple[str | None, int | None]:
    """値が書かれた要素(と頁)。新しく読む範囲を先に、重ねた範囲を後に探す。"""
    needle = re.sub(r"\s+", "", unicodedata.normalize("NFKC", raw_value)).casefold()
    # 1 字の値(数字 1 桁など)はどこにでも現れるため、書かれた要素を決めない。
    if len(needle) >= 2:
        for atom in (*unit.atoms, *unit.overlap):
            haystack = re.sub(r"\s+", "", unicodedata.normalize("NFKC", atom.text)).casefold()
            if needle in haystack:
                return atom.element_id, atom.page_number
    pages = unit.pages
    return None, pages[0] if len(pages) == 1 else None


def _extraction_atoms(extraction: StructuredExtraction, budget: int) -> list[FieldExtractionAtom]:
    """原文の要素(派生の要素を除く)を、文書の順に最小の本文にする。"""
    atoms: list[FieldExtractionAtom] = []
    for element in extraction.elements:
        text = element.text.strip()
        if not text or is_derived_content(element.content_kind, element.metadata):
            continue
        atom = FieldExtractionAtom(
            text=text,
            tokens=estimate_input_tokens(text),
            element_id=element.element_id,
            page_number=element.page_number,
            section_path=tuple(element.section_path),
        )
        atoms.extend(
            _split_atom(atom, budget - _header_tokens(atom) - 1, table=element.kind == "table")
        )
    return atoms


def _group_atoms(
    atoms: Sequence[FieldExtractionAtom],
    budget: int,
    *,
    index: int | None,
    total: int | None,
    part: tuple[int, ...],
) -> list[FieldExtractionUnit]:
    """最小の本文を、頭書き・重ねる範囲を含めて予算に収まるように詰める。"""
    groups: list[tuple[tuple[FieldExtractionAtom, ...], tuple[FieldExtractionAtom, ...]]] = []
    current: list[FieldExtractionAtom] = []
    overlap: tuple[FieldExtractionAtom, ...] = ()
    used = 0
    overlap_budget = int(budget * OVERLAP_RATIO)
    # 要素の間の改行の分として、要素ごとに 1 token を足して数える(見積もりを下回らない)。
    for atom in atoms:
        if current and used + atom.tokens + 1 > budget:
            groups.append((overlap, tuple(current)))
            overlap = _tail_overlap(current, overlap_budget)
            current = []
        if not current:
            used = _header_tokens(atom) + sum(item.tokens + 1 for item in overlap)
            if used + atom.tokens + 1 > budget:
                overlap = ()
                used = _header_tokens(atom)
        current.append(atom)
        used += atom.tokens + 1
    if current:
        groups.append((overlap, tuple(current)))
    count = len(groups)
    units: list[FieldExtractionUnit] = []
    for position, (carried, new) in enumerate(groups):
        unit_index = position if index is None else index
        unit_total = count if total is None else total
        unit_part = part if index is None else (*part, position)
        header = _header(unit_index, unit_total, new[0]) if unit_total > 1 else ""
        text = "\n".join(
            part_text for part_text in (header, *_texts(carried), *_texts(new)) if part_text
        )
        units.append(
            FieldExtractionUnit(
                index=unit_index,
                total=unit_total,
                atoms=new,
                overlap=carried,
                part=unit_part,
                text=text,
                tokens=estimate_input_tokens(text),
            )
        )
    return units


def _texts(atoms: Sequence[FieldExtractionAtom]) -> list[str]:
    return [atom.text for atom in atoms]


def _tail_overlap(
    atoms: Sequence[FieldExtractionAtom], budget: int
) -> tuple[FieldExtractionAtom, ...]:
    """前の単位の末尾の要素を、重ねる予算の分だけ持ってくる(大きい要素は末尾の部分)。"""
    if budget <= 0 or not atoms:
        return ()
    carried: list[FieldExtractionAtom] = []
    used = 0
    for atom in reversed(atoms):
        if used + atom.tokens + 1 <= budget:
            carried.insert(0, atom)
            used += atom.tokens + 1
            continue
        if not carried:
            tail = _tail_text(atom.text, budget)
            if tail:
                carried.insert(
                    0,
                    FieldExtractionAtom(
                        text=tail,
                        tokens=estimate_input_tokens(tail),
                        element_id=atom.element_id,
                        page_number=atom.page_number,
                        section_path=atom.section_path,
                    ),
                )
        break
    return tuple(carried)


def _header(index: int, total: int, first: FieldExtractionAtom) -> str:
    header = FIELD_EXTRACTION_PART_HEADER.format(index=index + 1, total=total)
    if first.section_path:
        header += FIELD_EXTRACTION_PART_PATH.format(path=" > ".join(first.section_path))
    return header


def _header_tokens(atom: FieldExtractionAtom) -> int:
    """頭書きの予算(位置の数は桁の多い側で見積もる)。"""
    return estimate_input_tokens(_header(99_999, 99_999, atom)) + 1


def _split_atom(
    atom: FieldExtractionAtom, limit: int, *, table: bool = False
) -> list[FieldExtractionAtom]:
    """予算を超える最小の本文を、行・文・文字の順に分ける(表は表頭を各部分に付け直す)。"""
    limit = max(1, limit)
    if atom.tokens <= limit:
        return [atom]
    lines = atom.text.split("\n")
    header: list[str] = []
    looks_like_table = table or lines[0].lstrip().startswith("|")
    if looks_like_table and len(lines) > 2 and _MARKDOWN_TABLE_SEPARATOR.match(lines[1]):
        header = lines[:2]
        # 表頭だけで予算の半分を超えるなら、付け直さない(各部分の本文が入らない)。
        if (estimate_input_tokens("\n".join(header)) + 1) * 2 > limit:
            header = []
    body = lines[len(header) :]
    body_limit = limit - (estimate_input_tokens("\n".join(header)) + 1 if header else 0)
    pieces: list[str] = []
    current: list[str] = []
    used = 0
    for segment in _segments(body, body_limit):
        cost = estimate_input_tokens(segment) + 1
        if current and used + cost > body_limit:
            pieces.append("\n".join(current))
            current, used = [], 0
        current.append(segment)
        used += cost
    if current:
        pieces.append("\n".join(current))
    return [
        FieldExtractionAtom(
            text=text,
            tokens=estimate_input_tokens(text),
            element_id=atom.element_id,
            page_number=atom.page_number,
            section_path=atom.section_path,
        )
        for text in ("\n".join([*header, piece]) if header else piece for piece in pieces)
        if text.strip()
    ]


def _segments(lines: Sequence[str], limit: int) -> list[str]:
    """行を、予算に収まる長さの断片にする(長い行は文の終わり、それでも長ければ文字で切る)。"""
    segments: list[str] = []
    for line in lines:
        if estimate_input_tokens(line) + 1 <= limit:
            segments.append(line)
            continue
        for sentence in (part for part in _SENTENCE_END.split(line) if part):
            if estimate_input_tokens(sentence) + 1 <= limit:
                segments.append(sentence)
            else:
                segments.extend(_hard_split(sentence, limit - 1))
    return segments


def _hard_split(text: str, limit: int) -> list[str]:
    """文字で切る(見積もりが予算を超えない最長の先頭を順に取る)。"""
    limit = max(1, limit)
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = start
        ascii_count = 0
        other = 0
        while end < len(text):
            if ord(text[end]) < 0x80:
                ascii_count += 1
            else:
                other += 1
            if other + (ascii_count + 3) // 4 > limit:
                break
            end += 1
        end = max(end, start + 1)
        pieces.append(text[start:end])
        start = end
    return pieces


def _tail_text(text: str, budget: int) -> str:
    """末尾から、見積もりが予算を超えない最長の部分。"""
    if budget <= 0:
        return ""
    ascii_count = 0
    other = 0
    start = len(text)
    while start > 0:
        char = text[start - 1]
        if ord(char) < 0x80:
            ascii_count += 1
        else:
            other += 1
        if other + (ascii_count + 3) // 4 > budget:
            break
        start -= 1
    return text[start:].strip()
