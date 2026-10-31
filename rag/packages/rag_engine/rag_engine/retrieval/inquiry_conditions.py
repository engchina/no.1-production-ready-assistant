"""質問文から問い合わせ種別や channel 条件を推定し検索 metadata を作る。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from rag_parser_core.language import LanguagePack, current_pack, per_pack
from rag_parser_core.language.markers import marker_patterns
from rag_parser_core.language.text import alternation, char_class, range_class, text_patterns

from rag_engine.retrieval.task_contract import cause_noun_targeted

# 2: 補助の検索文（retrieval_queries）を持たない（#3496）。検索文の追加は質問の言い換えの工程が受け持つ。
INQUIRY_CONDITION_SCHEMA_VERSION = 2
INQUIRY_PROFILE_SCHEMA_VERSION = 1
INQUIRY_CHUNK_METADATA_SCHEMA_VERSION = 3
QUERY_UNDERSTANDING_PROCESS_NAME = "Fine-grained Query Understanding"
INTENT_CLASSIFICATION_TASK = "Intent Classification"
SLOT_FILLING_TASK = "Slot Filling"
QUERY_UNDERSTANDING_TASKS = (INTENT_CLASSIFICATION_TASK, SLOT_FILLING_TASK)

OPERATION_PROFILE = "operation_steps"
CONDITION_PROFILE = "conditions_and_exceptions"
FILE_DATA_PROFILE = "file_data_confirmation"

# エラーコードは大文字始まり（E1234、ORA-00904、W1114）。大文字小文字を無視すると xlsx2023 / csv2024 / ver100 のような
# ファイル種別・版表記まで拾い、検索語とスコアを歪める (#826)。
_ERROR_CODE_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])(?:[A-Z]{1,5}[-_]?\d{3,6}|\d{2}[A-Z]\d{3}|[WE]\d{3,5})(?![0-9A-Za-z])",
)
_PARAMETER_PATTERN = re.compile(r"(?P<name>[A-Za-z][A-Za-z0-9_]{2,})\s*(?:[（(][^）)]*[）)]\s*)?[:：,、]\s*(?P<value>[0-9A-Za-z_-]{1,12})")


def _date_pattern() -> re.Pattern[str]:
    """質問の日付（「R6.4.1」「令和6年4月」「2024-04-01」「4/1」）。元号・略記は言語資源(#2181)。

    以前は元号を字の集合（[平成令和]）で書いていて、元号の語として働いていなかった。
    """
    text = text_patterns()
    return re.compile(
        rf"(?<![0-9A-Za-z])(?:{text.era_letter}\d{{1,2}}[./年]\d{{1,2}}(?:[./月]\d{{1,2}}日?)?|"
        rf"{text.era}\s*\d{{1,2}}年\s*\d{{1,2}}月(?:\s*\d{{1,2}}日)?|"
        r"\d{4}[-/]\d{1,2}[-/]\d{1,2}|"
        r"\d{1,2}/\d{1,2})(?![0-9A-Za-z])",
        re.IGNORECASE,
    )


_FILE_PATTERN = re.compile(
    r"[\wぁ-んァ-ン一-龯々ー・（）()【】\\/\-.]+?\."
    r"(?:pdf|csv|tsv|xlsx?|docx?|pptx?|json|txt)",
    re.IGNORECASE,
)
DECISION_TYPES = ("procedure", "necessity", "permission", "reason", "count", "output_impact")
_DECISION_TYPE_DISPLAY_LABELS = {
    "procedure": "操作手順",
    "necessity": "要否",
    "permission": "可否",
    "reason": "理由・原因",
    "count": "件数・集計",
    "output_impact": "出力影響",
}


@dataclass(frozen=True)
class _Cues:
    """言語資源（inquiry_cues。#2181 段 4）から組み立てた問い合わせの条件の語と正規表現。

    業務固有の連携先・資料名・製品名は含めず、言語資源の汎用の判定語だけを使う。
    """

    term: re.Pattern[str]
    # 「2ページ目」「2頁」「p.2」。「P2」のように点のない形は製品名・型番と区別できないため対象にしない。
    page: re.Pattern[str]
    column: re.Pattern[str]
    screen: re.Pattern[str]
    # 画面名は名詞句であり疑問詞を含まない。接尾語だけで判定すると「どこから登録」「どのように設定」も
    # 画面名になり、検索語と名指し画面の並び替え（#1044）が質問の疑問部分に引きずられる (#1066)。
    interrogative: re.Pattern[str]
    route: re.Pattern[str]
    document_kinds: tuple[tuple[str, str], ...]
    file_data_kind: str
    decisions: tuple[tuple[str, str], ...]
    operation_triggers: tuple[str, ...]
    condition_triggers: tuple[str, ...]
    condition_terms: tuple[str, ...]
    file_data_terms: tuple[str, ...]
    visual_terms: tuple[str, ...]
    external_context_terms: tuple[str, ...]


def _grouped(items: tuple[str, ...]) -> dict[str, list[str]]:
    """「語=値」の並びを、値ごとの語の並び（最初に出た値の順）にする。"""
    groups: dict[str, list[str]] = {}
    for item in items:
        word, _sep, value = item.partition("=")
        if word.strip() and value.strip():
            groups.setdefault(value.strip(), []).append(word.strip())
    return groups


def _build_cues(pack: LanguagePack) -> _Cues:
    def words(key: str) -> tuple[str, ...]:
        return pack.words("inquiry_cues", key)

    scripts = (
        *pack.words("search_terms", "hiragana_chars"),
        *pack.words("search_terms", "katakana_chars"),
        *pack.words("search_terms", "kanji_chars"),
        *words("term_joiner_chars"),
    )
    name = range_class((*scripts, "0-9", "A-Z", "a-z", *words("screen_name_bracket_chars")))
    sentence = re.escape("".join(pack.words("question_cues", "sentence_split_chars")))
    markers = marker_patterns(pack)
    return _Cues(
        term=re.compile(rf"[0-9A-Za-z_]+|{range_class(scripts)}{{2,}}"),
        page=re.compile(
            rf"(?<![0-9A-Za-z])(?:(\d{{1,4}})\s*{markers.page_words}{alternation(words('page_ordinal_suffixes'))}?"
            rf"|[pP]\.\s*(\d{{1,4}})(?![0-9A-Za-z]))"
        ),
        column=re.compile(rf"(?<![A-Za-z])([A-Z]{{1,3}})\s*({alternation(words('column_words'))})"),
        screen=re.compile(rf"{name}+{alternation(words('screen_suffixes'))}"),
        interrogative=re.compile(alternation(words("interrogative_words"))),
        route=re.compile(rf"{name}+{char_class(words('route_arrow_chars'))}[^{sentence}\n]{{1,120}}"),
        document_kinds=tuple(
            (label, "|".join(re.escape(word) for word in kind_words))
            for label, kind_words in _grouped(words("document_kind_words")).items()
        ),
        file_data_kind=pack.text("inquiry_cues", "file_data_kind"),
        decisions=tuple(
            (kind, "|".join(re.escape(word) for word in decision_words))
            for kind in DECISION_TYPES
            if (decision_words := _grouped(words("decision_words")).get(kind))
        ),
        operation_triggers=words("operation_trigger_terms"),
        condition_triggers=words("condition_trigger_terms"),
        condition_terms=words("condition_terms"),
        file_data_terms=words("file_data_terms"),
        visual_terms=words("visual_terms"),
        external_context_terms=words("external_context_terms"),
    )


_cues = per_pack(_build_cues)


@dataclass(frozen=True)
class InquiryProfileDefinition:
    """問い合わせ種別ごとの trigger、channel 重み、prompt 指示を定義します。"""

    profile_id: str
    label: str
    weight: float
    prompt_text: str

    @property
    def prompt_hash(self) -> str:
        """profile prompt の変更検知に使う hash を返します。"""
        return _sha256_json(
            {
                "schema_version": INQUIRY_PROFILE_SCHEMA_VERSION,
                "profile_id": self.profile_id,
                "prompt_text": self.prompt_text,
            }
        )


INQUIRY_PROFILES: tuple[InquiryProfileDefinition, ...] = (
    InquiryProfileDefinition(
        profile_id=OPERATION_PROFILE,
        label="操作手順",
        weight=1.15,
        prompt_text="画面名、メニュー経路、項目名、ボタン名、実施順を検索用に保持する。",
    ),
    InquiryProfileDefinition(
        profile_id=CONDITION_PROFILE,
        label="条件・例外",
        weight=1.25,
        prompt_text="条件、結果、不要、不可、対象外、注意、例外、可否判断を条件と結論の組で保持する。",
    ),
    InquiryProfileDefinition(
        profile_id=FILE_DATA_PROFILE,
        label="ファイル/データ確認",
        weight=1.35,
        prompt_text="CSV、Excel、連携結果、取込結果、外部データ、列名、エラーリストなど確認対象を保持する。",
    ),
)
_PROFILE_BY_ID = {profile.profile_id: profile for profile in INQUIRY_PROFILES}


def _file_data_terms() -> tuple[str, ...]:
    """ファイル/データ確認とみなす語。言語資源の汎用語を使う。"""
    return _cues().file_data_terms


def _external_context_terms() -> tuple[str, ...]:
    """外部連携の確認とみなす語。言語資源の汎用語を使う。"""
    return _cues().external_context_terms


def _trigger_terms(profile: InquiryProfileDefinition) -> tuple[str, ...]:
    """問い合わせ種別の trigger 語（言語資源）。"""
    if profile.profile_id == FILE_DATA_PROFILE:
        return _file_data_terms()
    cues = _cues()
    return cues.operation_triggers if profile.profile_id == OPERATION_PROFILE else cues.condition_triggers


def _document_kind_patterns() -> tuple[tuple[str, str], ...]:
    """文書種別の判定規則（言語資源）。"""
    cues = _cues()
    return cues.document_kinds


def inquiry_profile_contract_hash() -> str:
    """chunk run ごとに保存する問い合わせ profile 契約の fingerprint を返します。"""
    return _sha256_json(
        {
            "schema_version": INQUIRY_CHUNK_METADATA_SCHEMA_VERSION,
            "profiles": [
                {
                    "profile_id": profile.profile_id,
                    "weight": profile.weight,
                    "prompt_hash": profile.prompt_hash,
                    "trigger_terms": _trigger_terms(profile) if profile.profile_id != FILE_DATA_PROFILE else _cues().file_data_terms,
                }
                for profile in INQUIRY_PROFILES
            ],
        }
    )


@dataclass(frozen=True)
class InquiryMetadataFilter:
    """問い合わせ条件から派生した chunk metadata filter を保持します。"""

    source_file_terms: tuple[str, ...] = ()
    metadata_terms: tuple[str, ...] = ()
    # 質問が指定したページ番号（1 始まり）。source_file_terms と組でだけ使う（#601）。
    page_numbers: tuple[int, ...] = ()

    @property
    def active(self) -> bool:
        """検索や prompt へ反映する有効条件かどうかを返します。"""
        return bool(self.source_file_terms or self.metadata_terms or self.page_numbers)

    def to_payload(self) -> dict[str, Any]:
        """UI と trace 保存に使う JSON 互換 payload へ変換します。"""
        return {
            "source_file_terms": list(self.source_file_terms),
            "metadata_terms": list(self.metadata_terms),
            "page_numbers": list(self.page_numbers),
            "active": self.active,
        }


@dataclass(frozen=True)
class InquiryConditionParse:
    """質問文から推定した問い合わせ種別、channel、制約を保持します。"""

    original_question: str
    language_pack_hash: str = ""
    business_domains: tuple[str, ...] = ()
    document_kinds: tuple[str, ...] = ()
    screen_terms: tuple[str, ...] = ()
    menu_routes: tuple[str, ...] = ()
    file_terms: tuple[str, ...] = ()
    column_terms: tuple[str, ...] = ()
    codes_and_errors: tuple[str, ...] = ()
    parameter_refs: tuple[dict[str, str], ...] = ()
    date_terms: tuple[str, ...] = ()
    decision_types: tuple[str, ...] = ()
    requires_file_data_confirmation: bool = False
    requires_external_context: bool = False
    requires_visual_evidence: bool = False
    requires_condition_answer: bool = False
    search_terms: tuple[str, ...] = ()
    active_profiles: tuple[str, ...] = ()
    metadata_filter: InquiryMetadataFilter = InquiryMetadataFilter()

    @property
    def has_signals(self) -> bool:
        """質問から検索条件に使える signal を得られたかを返します。"""
        return any(
            (
                self.business_domains,
                self.document_kinds,
                self.screen_terms,
                self.menu_routes,
                self.file_terms,
                self.column_terms,
                self.codes_and_errors,
                self.parameter_refs,
                self.date_terms,
                self.decision_types,
            )
        )

    def to_payload(self) -> dict[str, Any]:
        """UI と trace 保存に使う JSON 互換 payload へ変換します。"""
        return {
            "schema_version": INQUIRY_CONDITION_SCHEMA_VERSION,
            "language_pack_hash": self.language_pack_hash,
            "process_name": QUERY_UNDERSTANDING_PROCESS_NAME,
            "nlu_tasks": list(QUERY_UNDERSTANDING_TASKS),
            "original_question": self.original_question,
            "business_domains": list(self.business_domains),
            "document_kinds": list(self.document_kinds),
            "screen_terms": list(self.screen_terms),
            "menu_routes": list(self.menu_routes),
            "file_terms": list(self.file_terms),
            "column_terms": list(self.column_terms),
            "codes_and_errors": list(self.codes_and_errors),
            "parameter_refs": list(self.parameter_refs),
            "date_terms": list(self.date_terms),
            "decision_types": list(self.decision_types),
            "requires_file_data_confirmation": self.requires_file_data_confirmation,
            "requires_external_context": self.requires_external_context,
            "requires_visual_evidence": self.requires_visual_evidence,
            "requires_condition_answer": self.requires_condition_answer,
            "search_terms": list(self.search_terms),
            "active_profiles": list(self.active_profiles),
            "metadata_filter": self.metadata_filter.to_payload(),
        }

    def prompt_context(self) -> str:
        """LLM prompt に追加する補助 context 文字列を返します。"""
        if not self.has_signals:
            return ""
        lines = [f"[{QUERY_UNDERSTANDING_PROCESS_NAME}]"]
        _append_line(lines, "nlu_tasks", QUERY_UNDERSTANDING_TASKS)
        _append_line(lines, "business_domains", self.business_domains)
        _append_line(lines, "document_kinds", self.document_kinds)
        _append_line(lines, "intents", self.decision_types)
        _append_line(lines, "screens", self.screen_terms)
        _append_line(lines, "menu_routes", self.menu_routes)
        _append_line(lines, "files", self.file_terms)
        _append_line(lines, "columns", self.column_terms)
        _append_line(lines, "codes_and_errors", self.codes_and_errors)
        if self.parameter_refs:
            refs = [f"{item['name']}={item['value']}" for item in self.parameter_refs]
            _append_line(lines, "parameters", refs)
        _append_line(lines, "dates", self.date_terms)
        _append_line(lines, "retrieval_profiles", self.active_profiles)
        flags = []
        if self.requires_file_data_confirmation:
            flags.append("file_data_confirmation")
        if self.requires_external_context:
            flags.append("external_context_possible")
        if self.requires_visual_evidence:
            flags.append("visual_evidence")
        if self.requires_condition_answer:
            flags.append("condition_answer")
        _append_line(lines, "flags", flags)
        return "\n".join(lines)

    def display_lines(self) -> list[str]:
        """問い合わせ条件を UI 表示用の行リストへ整形します。"""
        if not self.has_signals:
            return ["シグナル: なし"]
        lines: list[str] = [
            f"処理: {QUERY_UNDERSTANDING_PROCESS_NAME}",
            "NLUタスク: " + " / ".join(QUERY_UNDERSTANDING_TASKS),
        ]
        _append_line(lines, "業務領域", self.business_domains)
        _append_line(lines, "文書種別", self.document_kinds)
        _append_line(lines, "Intent", _display_decision_types(self.decision_types))
        _append_line(lines, "画面", self.screen_terms)
        _append_line(lines, "メニュー経路", self.menu_routes)
        _append_line(lines, "ファイル", self.file_terms)
        _append_line(lines, "ページ", [f"p.{number}" for number in self.metadata_filter.page_numbers])
        _append_line(lines, "列", self.column_terms)
        _append_line(lines, "コード/エラー", self.codes_and_errors)
        if self.parameter_refs:
            lines.append("パラメータ: " + " / ".join(f"{item['name']}={item['value']}" for item in self.parameter_refs))
        _append_line(lines, "日付", self.date_terms)
        _append_line(lines, "検索語", self.search_terms[:12])
        _append_line(lines, "プロファイル", _display_profiles(self.active_profiles))
        if self.metadata_filter.active:
            lines.append("メタデータフィルタ: 有効")
        if self.requires_external_context:
            lines.append("外部コンテキスト: 可能性あり")
        return lines or ["シグナル: なし"]


def _display_decision_types(values: Sequence[str]) -> list[str]:
    return [_DECISION_TYPE_DISPLAY_LABELS.get(value, value) for value in values]


def _display_profiles(values: Sequence[str]) -> list[str]:
    return [_PROFILE_BY_ID[value].label if value in _PROFILE_BY_ID else value for value in values]


def parse_inquiry_conditions(question: Any) -> InquiryConditionParse:
    """質問文から問い合わせ profile、channel、制約語を抽出します。"""
    original = _clean_text(question)
    comparable = _comparable(original)
    business_domains = _question_business_domains(original)
    document_kinds = _match_patterns(original, _document_kind_patterns())
    cues = _cues()
    screen_terms = _ordered_unique(term for term in cues.screen.findall(original) if not cues.interrogative.search(term))
    menu_routes = _ordered_unique(cues.route.findall(original))
    file_terms = _ordered_unique(_FILE_PATTERN.findall(original))
    column_terms = _ordered_unique(f"{match.group(1).upper()}{match.group(2)}" for match in cues.column.finditer(original))
    codes_and_errors = _ordered_unique(match.group(0) for match in _ERROR_CODE_PATTERN.finditer(original))
    parameter_refs = _parameter_refs(original)
    date_terms = _ordered_unique(match.group(0) for match in _date_pattern().finditer(original))
    decision_types = _match_patterns(original, cues.decisions)
    if "reason" not in decision_types and cause_noun_targeted(original):
        # 「原因」「理由」の名詞は、問う形の的のときだけ理由の判断にする（「理由は何文字以上」は項目の名前。#3491）。
        decision_types = [kind for kind in DECISION_TYPES if kind in {*decision_types, "reason"}]
    requires_file_data_confirmation = _contains_any(comparable, _file_data_terms())
    requires_external_context = _contains_any(comparable, _external_context_terms())
    requires_visual_evidence = _contains_any(comparable, cues.visual_terms)
    requires_condition_answer = bool({"necessity", "permission", "reason", "output_impact"} & set(decision_types)) or _contains_any(
        comparable, cues.condition_terms
    )
    source_file_terms = [term for term in file_terms if _looks_like_source_document(term)]
    # ページ番号は「申込書は 2 ページあります」のような本文の記述と区別できないため、
    # 資料のファイル名を伴う質問でだけ検索条件にする（#601）。
    page_numbers = _page_numbers(original) if source_file_terms else ()
    search_terms = _search_terms(
        original,
        business_domains=business_domains,
        document_kinds=document_kinds,
        screen_terms=screen_terms,
        menu_routes=menu_routes,
        file_terms=file_terms,
        column_terms=column_terms,
        codes_and_errors=codes_and_errors,
        parameter_refs=parameter_refs,
        date_terms=date_terms,
    )
    active_profiles = _active_profiles(
        decision_types=decision_types,
        requires_file_data_confirmation=requires_file_data_confirmation,
        requires_condition_answer=requires_condition_answer,
        requires_visual_evidence=requires_visual_evidence,
    )
    metadata_filter = InquiryMetadataFilter(
        source_file_terms=tuple(source_file_terms),
        metadata_terms=tuple([*business_domains, *document_kinds]),
        page_numbers=page_numbers,
    )
    return InquiryConditionParse(
        original_question=original,
        language_pack_hash=current_pack().hash,
        business_domains=tuple(business_domains),
        document_kinds=tuple(document_kinds),
        screen_terms=tuple(screen_terms),
        menu_routes=tuple(menu_routes),
        file_terms=tuple(file_terms),
        column_terms=tuple(column_terms),
        codes_and_errors=tuple(codes_and_errors),
        parameter_refs=tuple(parameter_refs),
        date_terms=tuple(date_terms),
        decision_types=tuple(decision_types),
        requires_file_data_confirmation=requires_file_data_confirmation,
        requires_external_context=requires_external_context,
        requires_visual_evidence=requires_visual_evidence,
        requires_condition_answer=requires_condition_answer,
        search_terms=tuple(search_terms),
        active_profiles=tuple(active_profiles),
        metadata_filter=metadata_filter,
    )


def _question_business_domains(question: str) -> list[str]:
    """呼び出し元が検索範囲の保存済み大分類から照合した業務名だけを使う。"""
    from rag_engine.dependencies import question_business_domains

    injected = question_business_domains(question)
    if injected is not None:
        # 値は候補の大分類と完全一致で比べる（`_same_business_records`）ため、表記を変えずに重複だけ除く。
        return list(dict.fromkeys(str(value) for value in injected if str(value).strip()))
    return []


def build_inquiry_chunk_metadata(
    *,
    text: str,
    retrieval_text: str,
    source_file_name: str,
    source_categories: Sequence[str],
    page_start: int,
    page_end: int,
) -> dict[str, Any]:
    """検索 filter と profile channel に必要な chunk metadata だけを構築します。"""
    combined = _clean_text("\n".join([source_file_name, retrieval_text, text]))
    document_kinds = _match_patterns(combined, _document_kind_patterns())
    active_profiles = _active_chunk_profiles(combined, source_categories)
    # 版は chunk metadata の top（schema_version）と chunks.json の contracts が持つ。ここには入れない (#814)。
    metadata: dict[str, Any] = {}
    if document_kinds:
        metadata["document_kinds"] = document_kinds
    if active_profiles:
        metadata["active_profiles"] = active_profiles
    return metadata


def profile_channel_rankings(
    chunks: Sequence[Any],
    conditions: InquiryConditionParse | None,
    *,
    limit: int,
) -> list[tuple[str, float, list[tuple[str, float]]]]:
    """問い合わせ profile に合う chunk channel を score 順に集計します。"""
    if conditions is None or not conditions.has_signals:
        return []
    rankings: list[tuple[str, float, list[tuple[str, float]]]] = []
    for profile_id in conditions.active_profiles:
        profile = _PROFILE_BY_ID.get(profile_id)
        if profile is None:
            continue
        scored: list[tuple[str, float]] = []
        for chunk in chunks:
            if getattr(chunk, "chunk_level", "") != "child":
                continue
            score = profile_chunk_score(chunk, conditions, profile_id)
            if score > 0:
                scored.append((str(getattr(chunk, "chunk_uid", "")), score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        if scored:
            rankings.append((f"profile:{profile_id}", profile.weight, scored[: max(1, int(limit or 1))]))
    return rankings


def profile_chunk_score(chunk: Any, conditions: InquiryConditionParse, profile_id: str) -> float:
    """chunk metadata が profile とどの程度一致するかを score 化します。"""
    text = _comparable(
        "\n".join(
            [
                str(getattr(chunk, "source_file_name", "")),
                str(getattr(chunk, "retrieval_text", "")),
                str(getattr(chunk, "text", "")),
            ]
        )
    )
    if not text:
        return 0.0
    profile_boost = 0.4 if profile_id in _chunk_active_profiles(getattr(chunk, "metadata", {})) else 0.0
    term_hits = _term_hits(text, conditions.search_terms)
    code_hits = _term_hits(text, conditions.codes_and_errors)
    param_hits = sum(
        1 for ref in conditions.parameter_refs if _comparable(ref.get("name", "")) in text and _comparable(ref.get("value", "")) in text
    )
    field_hits = 0
    for field in (
        conditions.screen_terms,
        conditions.menu_routes,
        conditions.file_terms,
        conditions.column_terms,
        conditions.date_terms,
        conditions.document_kinds,
        conditions.business_domains,
    ):
        field_hits += _term_hits(text, field)
    trigger_hits = _term_hits(text, _trigger_terms(_PROFILE_BY_ID[profile_id]))
    score = profile_boost + min(term_hits * 0.18, 2.4) + code_hits * 0.4 + param_hits * 0.5 + field_hits * 0.25
    if profile_id == FILE_DATA_PROFILE and conditions.requires_file_data_confirmation:
        score += min(trigger_hits * 0.2, 1.2)
    elif profile_id == CONDITION_PROFILE and conditions.requires_condition_answer:
        score += min(trigger_hits * 0.18, 1.0)
    elif profile_id == OPERATION_PROFILE and ("procedure" in conditions.decision_types or conditions.requires_visual_evidence):
        score += min(trigger_hits * 0.16, 0.9)
    return round(score, 4)


def _active_chunk_profiles(text: str, source_categories: Sequence[str]) -> list[str]:
    """chunk 本文に該当する profile ID だけを返します。"""
    comparable = _comparable(text)
    active: list[str] = []
    # operation profile は trigger 語の有無だけで決まる。Picture / Section-header を含む chunk を trigger 語なしでも
    # operation とみなすかは業務判断（active_profiles と _profile_score の加点が変わる）で、回帰評価と併せて別途扱う (#885)。
    for profile in INQUIRY_PROFILES:
        term_hits = [term for term in _trigger_terms(profile) if _comparable(term) in comparable]
        enabled = bool(term_hits)
        if profile.profile_id == CONDITION_PROFILE and _contains_any(comparable, _cues().condition_terms):
            enabled = True
        if profile.profile_id == FILE_DATA_PROFILE and _contains_any(comparable, _file_data_terms()):
            enabled = True
        if enabled:
            active.append(profile.profile_id)
    return active


def _parameter_refs(text: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in _PARAMETER_PATTERN.finditer(text):
        name = match.group("name").strip()
        value = match.group("value").strip()
        key = (_comparable(name), _comparable(value))
        if key in seen:
            continue
        refs.append({"name": name, "value": value})
        seen.add(key)
    return refs


def _search_terms(
    text: str,
    *,
    business_domains: Sequence[str],
    document_kinds: Sequence[str],
    screen_terms: Sequence[str],
    menu_routes: Sequence[str],
    file_terms: Sequence[str],
    column_terms: Sequence[str],
    codes_and_errors: Sequence[str],
    parameter_refs: Sequence[dict[str, str]],
    date_terms: Sequence[str],
) -> list[str]:
    terms: list[str] = []
    for source in (
        _cues().term.findall(text),
        business_domains,
        document_kinds,
        screen_terms,
        menu_routes,
        file_terms,
        column_terms,
        codes_and_errors,
        date_terms,
    ):
        for value in source:
            _append_unique(terms, _clean_text(value))
    for ref in parameter_refs:
        _append_unique(terms, ref.get("name", ""))
        _append_unique(terms, ref.get("value", ""))
        joined = f"{ref.get('name', '')} {ref.get('value', '')}".strip()
        _append_unique(terms, joined)
    return sorted([term for term in terms if len(_comparable(term)) >= 2], key=lambda item: (-len(_comparable(item)), item))


def _active_profiles(
    *,
    decision_types: Sequence[str],
    requires_file_data_confirmation: bool,
    requires_condition_answer: bool,
    requires_visual_evidence: bool,
) -> list[str]:
    profiles: list[str] = []
    if "procedure" in decision_types or requires_visual_evidence:
        profiles.append(OPERATION_PROFILE)
    if requires_condition_answer:
        profiles.append(CONDITION_PROFILE)
    if requires_file_data_confirmation:
        profiles.append(FILE_DATA_PROFILE)
    return _ordered_unique(profiles)


def _page_numbers(text: str) -> tuple[int, ...]:
    """質問中のページ指定（2ページ目 / 2頁 / p.2）を 1 始まりの番号の組で返す。0 と重複は除く。"""
    numbers = [int(a or b) for a, b in _cues().page.findall(text)]
    return tuple(dict.fromkeys(number for number in numbers if number > 0))


def _looks_like_source_document(value: str) -> bool:
    return _comparable(value).endswith((".pdf", ".doc", ".docx", ".ppt", ".pptx"))


def _match_patterns(text: str, patterns: tuple[tuple[str, str], ...]) -> list[str]:
    return [label for label, pattern in patterns if re.search(pattern, text, flags=re.IGNORECASE)]


def _extract_labeled_lines(text: str, label: str) -> list[str]:
    pattern = re.compile(rf"{re.escape(label)}:\s*(?P<value>[^\n]+)")
    return _ordered_unique(match.group("value").strip() for match in pattern.finditer(text))


def _term_hits(text: str, terms: Sequence[str]) -> int:
    seen: set[str] = set()
    count = 0
    for term in terms:
        key = _comparable(term)
        if len(key) < 2 or key in seen:
            continue
        if key in text:
            seen.add(key)
            count += 1
    return count


def _chunk_active_profiles(metadata: Any) -> set[str]:
    """chunk metadata の retrieval_profile から有効な profile ID を返します。"""
    if not isinstance(metadata, dict):
        return set()
    inquiry = metadata.get("retrieval_profile")
    values = inquiry.get("active_profiles") if isinstance(inquiry, dict) else None
    if not isinstance(values, list):
        return set()
    return {str(value) for value in values if str(value)}


def _contains_any(text: str, terms: Sequence[str]) -> bool:
    comparable_terms = (_comparable(term) for term in terms)
    return any(term and term in text for term in comparable_terms)


def _append_line(lines: list[str], label: str, values: Sequence[Any]) -> None:
    clean = [str(value).strip() for value in values if str(value or "").strip()]
    if clean:
        lines.append(f"{label}: " + ", ".join(clean))


def _append_unique(values: list[str], value: Any) -> None:
    text = _clean_text(value)
    key = _comparable(text)
    if not text or key in {_comparable(item) for item in values}:
        return
    values.append(text)


def _ordered_unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _clean_text(value)
        key = _comparable(text)
        if not text or key in seen:
            continue
        result.append(text)
        seen.add(key)
    return result


def _clean_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or ""))
    normalized = "".join(character for character in normalized if unicodedata.category(character) not in {"Cc", "Cf"})
    return re.sub(r"\s+", " ", normalized).strip()


def _comparable(value: Any) -> str:
    return _clean_text(value).casefold()


def _sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
