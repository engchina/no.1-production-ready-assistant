"""実体（システム・部署・属性の値など）と「実体と chunk の関連」の決定的な抽出（#1362）。

文書レシピの処理（``rag_entity_index_enabled``。既定は使わない。#3008）で、索引の保存の後に chunk
から実体を抜き出し、Oracle の ``rag_entities`` / ``rag_entity_aliases`` / ``rag_entity_chunks`` に
保存する。検索のときは ``app.rag.entity_expansion`` が、この表との SQL の join で関連する chunk を
1 段だけ足す。

LLM は使わない（チャンクごとに LLM で事件を抜き出す取込は採らない。#1335）。抽出は次の決定的な規則
だけで行う。

- **表の行の記録**（``content_kind=record``。前処理 ``excel_to_jsonl`` の「列名: 値 / 列名: 値」）:
  - 名前の列（ID・正式名・略称・別表記など。レシピの ``entity_name_columns`` で指定でき、無ければ
    列名で決める）の値を、その行の 1 つの実体の別名にし、その行の chunk を「定義する行」
    （``definition``）にする。
  - 属性の列（担当部署・重要度など。``entity_attribute_columns`` で指定でき、無ければ名前の列・長
    い本文の列・日付や数値の列を除いた列）の値を実体にし、その行の chunk を「属性」（``attribute``）
    として関連づける。値には「列名＋値」の別名（``重要度A``）も付ける（1 文字の英数字の値は列名つ
    きの別名だけ）。
- **本文**:
  - 定義の形（``略号「経」: 経理部``、``経理部（略称: 経）``、行の先頭の ``ＨＲＭ: 毎月…``、同じ文
    書で定義した名前で始まる ``経理部の承認者は…``）を「定義する行」にする。同じ行の
    ``通称「情シス」`` は別名。
  - ID の形（``SYS-104``）・英字の名前（``HRM``・``Document Portal``）・語尾がシステム / ポータル
    / 部 / 課 / 室 / 本部の語・かぎ括弧の語（2 文字以上）を「言及」（``mention``）にする。
- **見出し・列挙のラベル**（本文の「語＋1 文字の英字か 1〜2 桁の数字」。``原因 A``・``手順 1``・
  ``案 B``・``Step 2``）は、その文書の中だけで使う記号のことが多いため、それだけでは実体にしない。別
  名の種類を ``label`` にして保存し、検索のときは、別の文書が同じ別名をラベル以外の種類（表の値・定
  義の形・ID や名前）で持つとき（台帳の「重要度: A」と障害連絡規程の「重要度 A: …」）だけ、拡張の起
  点・経路に使う（#1393。``app.clients.entity_store``）。表の値（``重要度A``）・定義の形・1 文字の
  部署の略号・ID・正式名は従来どおり実体にする。

名寄せは ``entity_key``（NFKC・波線とダッシュの同一視・大文字小文字と空白の無視。全文検索の索引と
同じ ``fold_text``。#1336・#1350）でそろえた別名の表で行う。``ＨＲＭ`` と ``HRM``、
``ＳＹＳ－１０４`` と ``SYS-104`` は同じ別名になる。1 文字の実体（部署の略号「経」など）は捨てない
が、表の列の値と定義する行からだけ作り、本文の任意の 1 文字（「経費」の「経」など）には一致させな
い（言及は 2 文字以上）。

実体は chunk_set ごとに作る（別の文書の実体とは、検索のときに別名の一致で結び付ける）。文書の題名・
前書きにある会社の名前（``サンプル社``）を実体の ``scope_label`` にし、別の会社の同じ略号（「経」
「OMS」）を検索のときに区別する。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from rag_engine.retrieval.character_forms import fold_text
from rag_parser_core.language import LanguagePack, per_pack
from rag_parser_core.language.text import alternation

from app.rag.chunking import Chunk

EntityChunkRole = Literal["definition", "attribute", "mention"]
ENTITY_ROLE_DEFINITION: EntityChunkRole = "definition"
ENTITY_ROLE_ATTRIBUTE: EntityChunkRole = "attribute"
ENTITY_ROLE_MENTION: EntityChunkRole = "mention"
# 1 つの (実体, chunk) の関連が複数の役割を持つときに残す順（強いほうを残す）。
_ROLE_PRIORITY: dict[str, int] = {
    ENTITY_ROLE_DEFINITION: 0,
    ENTITY_ROLE_ATTRIBUTE: 1,
    ENTITY_ROLE_MENTION: 2,
}
SHEET_RECORD_CONTENT_KIND = "record"
# 実体の種類（同じ名前が複数の種類で出たときは強いほうにする）。record: 表の行、defined:本文の定義
# の形、value: 表の属性の列の値、term: 本文の言及・行の先頭の名前。
_TYPE_PRIORITY: dict[str, int] = {"record": 0, "defined": 1, "value": 2, "term": 3}
# 本文の見出し・列挙のラベル（「原因 A」）の別名の種類（#1393）。検索のときは、別の文書がこの別名を
# ラベル以外の種類で持つときだけ拡張に使う（``app.clients.entity_store``）。
ALIAS_KIND_LABEL = "label"

# 1 つの chunk_set から作る実体・1 つの実体の別名・1 つの chunk の言及の上限（巨大な文書の安全弁）。
MAX_ENTITIES_PER_CHUNK_SET = 5000
MAX_ALIASES_PER_ENTITY = 16
MAX_MENTIONS_PER_CHUNK = 40
# 表示名・別名の長さの上限（DB の列の長さより短く）。
_MAX_NAME_CHARS = 120
# 属性の値として実体にする長さの上限（長い本文の列の値は実体にしない）。
_MAX_ATTRIBUTE_VALUE_CHARS = 30

_FIELD_SEPARATOR = " / "
_FIELD_NAME_VALUE = ": "
# 列の役割の表示（``既定値［資料の既定値］``。#1281）を列名の照合から外す。
_COLUMN_ROLE_SUFFIX = re.compile(r"［[^］]*］$")
# 列名・本文の定義の形・言及の語・見出しのラベルの形・会社の名前の手がかりは言語資源
# （entity_cues。取込の項目。#2181）で、``_cues()`` が言語資源の hash ごとに正規表現に組み立てる。
# 言及にする ID の形（英字と数字。言語に依らない）。
_ID_TERM = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2,6}-\d{2,6}(?![0-9])")
_TRAILING_PAREN = re.compile(r"\((?P<inner>[^)]{1,20})\)$")
_SCOPE_LABEL_TEXT_CHARS = 400


@dataclass(frozen=True)
class _EntityCues:
    """言語資源（entity_cues。取込の項目。#2181 段 5）から組み立てた、実体の索引の正規表現と語。"""

    id_column: re.Pattern[str]
    alias_column: re.Pattern[str]
    name_column: re.Pattern[str]
    skip_column: re.Pattern[str]
    value_split: re.Pattern[str]
    numeric_or_date: re.Pattern[str]
    single_cjk: re.Pattern[str]
    code_definition: re.Pattern[str]
    paren_alias: re.Pattern[str]
    nickname: re.Pattern[str]
    line_subject: re.Pattern[str]
    subject_attribute: re.Pattern[str]
    latin_term: re.Pattern[str]
    suffix_term: re.Pattern[str]
    quoted_term: re.Pattern[str]
    entity_shaped: tuple[re.Pattern[str], ...]
    label_shaped: re.Pattern[str]
    scope_label: re.Pattern[str]
    generic_scope_words: tuple[str, ...]
    sentence_marks: tuple[str, ...]


def _body(items: tuple[str, ...]) -> str:
    """1 文字か「最初-最後」の範囲の並びを、文字の集合の中身にする。"""
    parts = []
    for item in items:
        if len(item) == 1:
            parts.append(re.escape(item))
        elif len(item) == 3 and item[1] == "-" and item[0] <= item[2]:
            parts.append(f"{re.escape(item[0])}-{re.escape(item[2])}")
    return "".join(parts)


def _build_entity_cues(pack: LanguagePack) -> _EntityCues:
    def words(key: str) -> tuple[str, ...]:
        return pack.words("entity_cues", key)

    def alt(key: str) -> str:
        return alternation(words(key))

    def chars(key: str) -> str:
        return re.escape("".join(words(key)))

    # 本文の定義の形は ``fold_text`` でそろえた行に当てる。
    cjk = _body(words("entity_script_chars"))
    kana_kanji = _body(words("entity_kana_kanji_chars"))
    quotes = pack.words("symbols", "quote_brackets")
    opens = re.escape("".join(pair[0] for pair in quotes))
    closes = re.escape("".join(pair[1] for pair in quotes))
    first = quotes[0] if quotes else "「」"
    marks = chars("sentence_marks") + chars("clause_marks")
    organizations = alt("organization_suffixes")
    scope_chars = f"{kana_kanji}A-Za-z0-9"
    return _EntityCues(
        # 名前の列: ID・番号・コード（実体の ID）、名前（正式名・名称・〜名）、
        # 別名（略称・別表記・通称）。
        id_column=re.compile(rf"{alt('id_column_words')}$", re.IGNORECASE),
        alias_column=re.compile(alt("alias_column_words")),
        name_column=re.compile(rf"{alt('name_column_words')}$"),
        # 実体にしない列（長い本文・日付・数値になりやすい列）。
        skip_column=re.compile(alt("skip_column_words")),
        # 値の区切り（略称の列の「OMS、受発注」など）。
        value_split=re.compile(rf"\s*[{chars('value_separators')}]\s*"),
        numeric_or_date=re.compile(rf"^[\d\s.,:/\-{chars('numeric_value_chars')}]+$"),
        single_cjk=re.compile(rf"^[{_body(words('entity_kanji_chars'))}]$"),
        code_definition=re.compile(
            rf"{alt('code_definition_words')}\s*[{opens}](?P<alias>[^{closes}\s]{{1,24}})[{closes}]\s*"
            rf"{alt('definition_separators')}\s*"
            rf"(?P<name>[^{marks},(){re.escape(first)}\s]{{1,40}})"
        ),
        paren_alias=re.compile(
            rf"(?P<name>[{cjk}A-Za-z0-9][{cjk}A-Za-z0-9 \-]{{0,38}}[{cjk}A-Za-z0-9])"
            rf"\({alt('paren_alias_words')}[:]?\s*[{opens}]?(?P<alias>[^){closes}]{{1,24}})[{closes}]?\)"
        ),
        nickname=re.compile(
            rf"{alt('nickname_words')}[{opens}](?P<alias>[^{closes}]{{1,24}})[{closes}]"
        ),
        # 行の先頭の「名前: 値」（「ＨＲＭ: 毎月第 2 土曜日」「重要度 A: 検知から」）。
        line_subject=re.compile(rf"^\s*(?P<subject>[^:{marks}]{{1,40}}?)\s*:\s*\S"),
        # 「名前の〜は」（「経理部の承認者は管理本部長です」）。主語は同じ文書で定義した名前に限る。
        subject_attribute=re.compile(
            rf"^\s*(?P<subject>[^{chars('subject_link_particles')}{marks}]{{1,40}}){alt('subject_link_particles')}"
            rf"[^{marks}]{{1,16}}{alt('subject_topic_particles')}"
        ),
        # 英字の名前（「HRM」「Document Portal」「経費 Portal」）。前に付ける語はカナ・漢字だけ
        # （「と C」を取らない）。ID の形の前半（「SYS-104」の「SYS」）は取らない。
        latin_term=re.compile(
            rf"(?<![A-Za-z0-9])(?:[{kana_kanji}]{{1,8}} ?)?[A-Z][A-Za-z0-9&]*"
            r"(?: [A-Z][A-Za-z0-9&]*){0,3}"
            r"(?![A-Za-z0-9]|-\d)"
        ),
        suffix_term=re.compile(rf"[{kana_kanji}]{{1,14}}?{organizations}(?![{kana_kanji}])"),
        quoted_term=re.compile(rf"[{opens}](?P<term>[^{closes}\s]{{2,24}})[{closes}]"),
        # 実体として認める見出し・主語の形（定義の形の主語）。
        entity_shaped=(
            re.compile(r"^[A-Za-z]{2,6}-\d{2,6}$"),
            re.compile(r"^[A-Za-z][A-Za-z0-9&.\-]*(?: [A-Za-z][A-Za-z0-9&.\-]*){0,3}$"),
            re.compile(rf"^[{cjk}]{{1,12}} ?[A-Za-z0-9][A-Za-z0-9 ]{{0,15}}$"),
            re.compile(rf"^[{cjk}]{{1,16}}{organizations}$"),
        ),
        # 見出し・列挙のラベルの形（「原因 A」「手順 1」「案 B」「Step 2」。#1393）。
        label_shaped=re.compile(
            rf"^(?:[{cjk}]{{1,8}} ?|[A-Z][a-z]{{1,11}} )(?:[A-Za-z]|[0-9]{{1,2}})$"
        ),
        # 文書の会社の名前（題名・前書き。「サンプル社」）。
        scope_label=re.compile(
            rf"(?<![{scope_chars}])(?P<label>[{scope_chars}]{{1,20}}{alt('company_scope_suffixes')})"
        ),
        generic_scope_words=words("generic_company_words"),
        sentence_marks=words("sentence_marks"),
    )


_cues = per_pack(_build_entity_cues)


def entity_key(text: object) -> str:
    """名寄せの形（NFKC・波線とダッシュの同一視・大文字小文字と空白の無視）。

    ``ＨＲＭ`` と ``HRM``、``ＳＹＳ－１０４`` と ``SYS-104``、``経費 Portal`` と ``経費portal`` は
    同じ形になる。
    """
    return "".join(fold_text(text).casefold().split())


def is_label_shaped(text: object) -> bool:
    """見出し・列挙のラベルの形（「原因 A」「手順 1」「案 B」）か（#1393）。"""
    return bool(_cues().label_shaped.match(" ".join(fold_text(text).split())))


def entity_chunk_id(document_id: str, chunk: Chunk, *, chunk_set_id: str | None) -> str:
    """保存する chunk の ID（``OracleClient._chunk_insert_rows`` と同じ形）。"""
    if chunk_set_id is not None:
        return f"{document_id}:{chunk_set_id}:{chunk.index}"
    return f"{document_id}:{chunk.index}"


@dataclass(frozen=True)
class EntityAlias:
    """実体の別名（正規化した形と、資料の表記）。"""

    alias_key: str
    alias_text: str
    alias_kind: str


@dataclass(frozen=True)
class EntityRecord:
    """``rag_entities`` に保存する実体（1 つの chunk_set の中で正規化した名前ごとに 1 つ）。"""

    entity_id: str
    entity_key: str
    display_name: str
    entity_type: str
    scope_label: str | None
    aliases: tuple[EntityAlias, ...]


@dataclass(frozen=True)
class EntityChunkLink:
    """``rag_entity_chunks`` に保存する実体と chunk の関連。"""

    entity_id: str
    chunk_id: str
    chunk_role: EntityChunkRole
    attribute_name: str | None = None


@dataclass(frozen=True)
class EntityIndex:
    """1 つの chunk_set から作った実体の索引。"""

    entities: list[EntityRecord] = field(default_factory=list)
    links: list[EntityChunkLink] = field(default_factory=list)

    @property
    def alias_count(self) -> int:
        return sum(len(entity.aliases) for entity in self.entities)


@dataclass(frozen=True)
class EntityIndexOptions:
    """文書レシピの選択肢（列名の指定）。空なら列名から決める。"""

    name_columns: tuple[str, ...] = ()
    attribute_columns: tuple[str, ...] = ()


@dataclass
class _EntityDraft:
    key: str
    display_name: str
    entity_type: str
    aliases: dict[str, EntityAlias] = field(default_factory=dict)


class _Builder:
    def __init__(self, *, identity_scope: str, scope_label: str | None) -> None:
        self._identity_scope = identity_scope
        self._scope_label = scope_label
        self._entities: dict[str, _EntityDraft] = {}
        self._links: dict[tuple[str, str], EntityChunkLink] = {}

    def entity(
        self, name: str, *, entity_type: str, alias_kind: str = "name"
    ) -> _EntityDraft | None:
        key = entity_key(name)
        if not key or len(key) > _MAX_NAME_CHARS:
            return None
        draft = self._entities.get(key)
        if draft is not None and _TYPE_PRIORITY[entity_type] < _TYPE_PRIORITY[draft.entity_type]:
            # 言及だけだった名前を、後で定義した行・表の行の実体にする。
            draft.entity_type = entity_type
        if draft is None:
            if len(self._entities) >= MAX_ENTITIES_PER_CHUNK_SET:
                return None
            draft = _EntityDraft(key=key, display_name=_display(name), entity_type=entity_type)
            self._entities[key] = draft
        self.alias(draft, name, kind=alias_kind)
        return draft

    def alias(self, draft: _EntityDraft, text: str, *, kind: str) -> None:
        key = entity_key(text)
        if not key or len(key) > _MAX_NAME_CHARS or key in draft.aliases:
            return
        if len(draft.aliases) >= MAX_ALIASES_PER_ENTITY:
            return
        draft.aliases[key] = EntityAlias(alias_key=key, alias_text=_display(text), alias_kind=kind)

    def link(
        self,
        draft: _EntityDraft,
        chunk_id: str,
        role: EntityChunkRole,
        *,
        attribute_name: str | None = None,
    ) -> None:
        entity_id = self._entity_id(draft.key)
        current = self._links.get((entity_id, chunk_id))
        if current is not None and _ROLE_PRIORITY[current.chunk_role] <= _ROLE_PRIORITY[role]:
            return
        self._links[(entity_id, chunk_id)] = EntityChunkLink(
            entity_id=entity_id,
            chunk_id=chunk_id,
            chunk_role=role,
            attribute_name=_display(attribute_name) if attribute_name else None,
        )

    def has_defined_entity(self, name: str) -> bool:
        """同じ文書で定義した名前（表の行・定義の形）か。言及だけの名前は含めない。"""
        draft = self._entities.get(entity_key(name))
        return draft is not None and draft.entity_type in {"record", "defined"}

    def build(self) -> EntityIndex:
        entities = [
            EntityRecord(
                entity_id=self._entity_id(draft.key),
                entity_key=draft.key,
                display_name=draft.display_name,
                entity_type=draft.entity_type,
                scope_label=self._scope_label,
                aliases=tuple(draft.aliases.values()),
            )
            for draft in self._entities.values()
        ]
        return EntityIndex(entities=entities, links=list(self._links.values()))

    def _entity_id(self, key: str) -> str:
        digest = hashlib.sha256(f"{self._identity_scope}\x1f{key}".encode()).hexdigest()
        return f"ent_{digest[:40]}"


def build_entity_index(
    *,
    document_id: str,
    chunks: Sequence[Chunk],
    chunk_set_id: str | None,
    document_title: str = "",
    options: EntityIndexOptions | None = None,
) -> EntityIndex:
    """chunk から実体・別名・実体と chunk の関連を作る（決定的。LLM は使わない）。"""
    if not chunks:
        return EntityIndex()
    resolved = options or EntityIndexOptions()
    builder = _Builder(
        identity_scope=chunk_set_id or document_id,
        scope_label=document_scope_label([document_title, *(chunk.text for chunk in chunks[:3])]),
    )
    prose: list[tuple[str, Chunk]] = []
    # 表の行を先に読み、本文の「名前の〜は」の主語に台帳の名前も使えるようにする。
    for chunk in chunks:
        chunk_id = entity_chunk_id(document_id, chunk, chunk_set_id=chunk_set_id)
        if chunk.metadata.get("content_kind") == SHEET_RECORD_CONTENT_KIND:
            _index_record(builder, chunk_id, chunk.text, resolved)
        else:
            prose.append((chunk_id, chunk))
    # 定義の形を先に読み、同じ文書で定義した名前を「名前の〜は」の主語に使う。
    for chunk_id, chunk in prose:
        _index_definitions(builder, chunk_id, chunk.text)
    for chunk_id, chunk in prose:
        _index_subject_lines(builder, chunk_id, chunk.text)
        _index_mentions(builder, chunk_id, chunk.text)
    return builder.build()


def document_scope_label(texts: Iterable[object]) -> str | None:
    """文書の題名・前書きにある会社の名前（「サンプル社」）。無ければ None。"""
    for text in texts:
        folded = fold_text(text)[:_SCOPE_LABEL_TEXT_CHARS]
        for match in _cues().scope_label.finditer(folded):
            label = match.group("label")
            # 「会社」「子会社」など一般の語で終わる語は会社の名前にしない。
            if not label.endswith(_cues().generic_scope_words):
                return label
    return None


def record_fields(text: str) -> list[tuple[str, str]]:
    """行の記録の本文（「列名: 値 / 列名: 値」）を (列名, 値) の並びにする。

    値の中の「 / 」は、次の部分が「列名: 」で始まらなければ値の続きとして扱う。
    """
    fields: list[list[str]] = []
    for line in text.splitlines():
        if _FIELD_NAME_VALUE not in line:
            continue
        for part in line.split(_FIELD_SEPARATOR):
            name, separator, value = part.partition(_FIELD_NAME_VALUE)
            if separator and name.strip() and len(name.strip()) <= 40:
                fields.append([name.strip(), value.strip()])
            elif fields:
                fields[-1][1] = f"{fields[-1][1]}{_FIELD_SEPARATOR}{part}".strip()
    return [(name, value) for name, value in fields if value]


def _column_label(name: str) -> str:
    return _COLUMN_ROLE_SUFFIX.sub("", fold_text(name)).strip()


def _identity_kind(label: str) -> str | None:
    """列名から決める名前の列の種類（id / alias / name）。名前の列でなければ None。"""
    if _cues().id_column.search(label):
        return "id"
    if _cues().alias_column.search(label):
        return "alias"
    if _cues().name_column.search(label) and not _cues().skip_column.search(label):
        return "name"
    return None


def _column_kind(name: str, options: EntityIndexOptions) -> str | None:
    """列の役割（id / name / alias / attribute）。実体にしない列は None。

    レシピが名前の列（``name_columns``）を指定したときは、その列だけを名前の列にする。属性の列
    （``attribute_columns``）を指定したときは、その列だけを属性の列にする。指定が無ければ列名で決
    める。
    """
    label = _column_label(name)
    key = entity_key(label)
    identity = _identity_kind(label)
    if options.name_columns:
        if key in {entity_key(column) for column in options.name_columns}:
            return identity or "name"
    elif identity is not None:
        return identity
    if options.attribute_columns:
        wanted = {entity_key(column) for column in options.attribute_columns}
        return "attribute" if key in wanted else None
    if identity is not None or _cues().skip_column.search(label):
        return None
    return "attribute"


def _index_record(builder: _Builder, chunk_id: str, text: str, options: EntityIndexOptions) -> None:
    fields = record_fields(text)
    if not fields:
        return
    names: list[tuple[str, str]] = []
    attributes: list[tuple[str, str]] = []
    for column, value in fields:
        kind = _column_kind(column, options)
        if kind is None:
            continue
        if kind == "attribute":
            attributes.append((_column_label(column), value))
            continue
        values = _cues().value_split.split(value) if kind == "alias" else [value]
        names.extend((kind, item.strip()) for item in values if item.strip())
    if names:
        # 表示名は名前の列 > 別名の列 > ID の列の順で最初の値。
        display = next(
            (
                value
                for wanted in ("name", "alias", "id")
                for kind, value in names
                if kind == wanted
            ),
            names[0][1],
        )
        draft = builder.entity(display, entity_type="record")
        if draft is not None:
            for kind, value in names:
                builder.alias(draft, value, kind=kind)
            builder.link(draft, chunk_id, ENTITY_ROLE_DEFINITION)
    for column, value in attributes:
        _index_attribute_value(builder, chunk_id, column, value)


def _index_attribute_value(builder: _Builder, chunk_id: str, column: str, value: str) -> None:
    folded = fold_text(value).strip()
    if (
        not folded
        or len(folded) > _MAX_ATTRIBUTE_VALUE_CHARS
        or any(mark in folded for mark in _cues().sentence_marks)
    ):
        return
    if _cues().numeric_or_date.match(folded):
        return
    key = entity_key(folded)
    qualified = f"{column}{folded}"
    # 1 文字の英数字の値（重要度の「A」）は、列名つきの別名（「重要度 A」）だけで実体にする。
    standalone = len(key) >= 2 or bool(_cues().single_cjk.match(key))
    draft = builder.entity(folded if standalone else qualified, entity_type="value")
    if draft is None:
        return
    builder.alias(draft, qualified, kind="qualified")
    builder.link(draft, chunk_id, ENTITY_ROLE_ATTRIBUTE, attribute_name=column)


def _lines(text: str) -> list[str]:
    return [line.strip() for line in fold_text(text).splitlines() if line.strip()]


def _index_definitions(builder: _Builder, chunk_id: str, text: str) -> None:
    for line in _lines(text):
        defined: list[_EntityDraft] = []
        for match in _cues().code_definition.finditer(line):
            draft = builder.entity(match.group("name"), entity_type="defined")
            if draft is None:
                continue
            builder.alias(draft, match.group("alias"), kind="code")
            builder.link(draft, chunk_id, ENTITY_ROLE_DEFINITION)
            defined.append(draft)
        for match in _cues().paren_alias.finditer(line):
            draft = builder.entity(match.group("name").strip(), entity_type="defined")
            if draft is None:
                continue
            builder.alias(draft, match.group("alias"), kind="alias")
            builder.link(draft, chunk_id, ENTITY_ROLE_DEFINITION)
            defined.append(draft)
        if len(defined) == 1:
            # 同じ行の「通称「情シス」と呼びます」は、その行で定義した実体の別名。
            for match in _cues().nickname.finditer(line):
                builder.alias(defined[0], match.group("alias"), kind="alias")


def _index_subject_lines(builder: _Builder, chunk_id: str, text: str) -> None:
    for line in _lines(text):
        if _cues().code_definition.search(line):
            continue
        match = _cues().line_subject.match(line)
        if match is not None:
            subject = match.group("subject").strip()
            qualifier = _TRAILING_PAREN.search(subject)
            base = subject[: qualifier.start()].strip() if qualifier else subject
            if _entity_shaped(base):
                draft = builder.entity(
                    subject, entity_type="term", alias_kind=_prose_alias_kind(subject, "name")
                )
                if draft is not None:
                    if qualifier:
                        builder.alias(draft, base, kind=_prose_alias_kind(base, "alias"))
                    builder.link(draft, chunk_id, ENTITY_ROLE_DEFINITION)
                continue
        match = _cues().subject_attribute.match(line)
        if match is not None and builder.has_defined_entity(match.group("subject")):
            draft = builder.entity(match.group("subject"), entity_type="defined")
            if draft is not None:
                builder.link(draft, chunk_id, ENTITY_ROLE_DEFINITION)


def _index_mentions(builder: _Builder, chunk_id: str, text: str) -> None:
    folded = fold_text(text)
    terms: dict[str, str] = {}
    for pattern in (_ID_TERM, _cues().latin_term, _cues().suffix_term):
        for match in pattern.finditer(folded):
            terms.setdefault(entity_key(match.group(0)), match.group(0).strip())
    for match in _cues().quoted_term.finditer(folded):
        terms.setdefault(entity_key(match.group("term")), match.group("term"))
    count = 0
    for key, term in terms.items():
        # 本文の言及は 2 文字以上（1 文字の実体は表の値と定義する行からだけ作る）。
        if len(key) < 2 or count >= MAX_MENTIONS_PER_CHUNK:
            continue
        draft = builder.entity(term, entity_type="term", alias_kind=_prose_alias_kind(term, "name"))
        if draft is None:
            continue
        builder.link(draft, chunk_id, ENTITY_ROLE_MENTION)
        count += 1


def _prose_alias_kind(text: str, kind: str) -> str:
    """本文の言及・行の先頭の名前の別名の種類。ラベルの形（「原因 A」）は ``label`` にする。

    同じ文書の表の値・定義の形が先に同じ別名を作っていれば、その種類が残る（``_Builder.alias`` は
    同じ別名を上書きしない）。
    """
    return ALIAS_KIND_LABEL if is_label_shaped(text) else kind


def _entity_shaped(text: str) -> bool:
    return any(pattern.match(text) for pattern in _cues().entity_shaped)


def _display(text: object) -> str:
    return " ".join(fold_text(text).split())[:_MAX_NAME_CHARS]


__all__ = [
    "ALIAS_KIND_LABEL",
    "ENTITY_ROLE_ATTRIBUTE",
    "ENTITY_ROLE_DEFINITION",
    "ENTITY_ROLE_MENTION",
    "EntityAlias",
    "EntityChunkLink",
    "EntityChunkRole",
    "EntityIndex",
    "EntityIndexOptions",
    "EntityRecord",
    "build_entity_index",
    "document_scope_label",
    "entity_chunk_id",
    "entity_key",
    "is_label_shaped",
    "record_fields",
]
