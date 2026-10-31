"""言語資源（書き方パック。#2181）。

文書と質問を読むときの言語の書き方（数・単位・記号・助詞・参照の書き方など）を、コードではなく
データで持つ。設計は ``rag/docs/language-resources.md``。

- 組み込みのパック（``packs/<locale>.json``）が既定値。今までのコードの値をそのまま持つ。
- 上書きは「節 → 項目 → 値」の形で、項目ごとに組み込みの値を置き換える（``resolve``）。
- 実行のときは ``current_pack()`` で読む。backend が全体の上書きを ``set_default_pack`` で置き、
  取込の job・検索と回答の要求の間だけ ``bind_pack`` で、その要求の上書きを束ねる。
- 項目の種類（``FieldKind``）は語の一覧か決まった型だけ。正規表現は受け付けない。

このモジュールは仕組み（スキーマ・検証・重ね合わせ）だけを持ち、言語の書き方の値は持たない。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from importlib import resources
from typing import Any

DEFAULT_LOCALE = "ja"
# 語の一覧の上限（1 項目の語の数・1 つの語の文字数）。
MAX_ITEMS = 300
MAX_ITEM_CHARS = 40
# 文の項目（回答の定型文）の長さの上限。
MAX_SENTENCE_CHARS = 200
# 「語=値」の区切り。
MAP_SEPARATOR = "="
# chunk の metadata に残す、分割に使った言語資源の hash の key。
LANGUAGE_PACK_HASH_KEY = "language_pack_hash"


# 画像の説明（Vision）の項目。ラベルは言語資源の content_labels.vision_fields で付ける。
VISION_FIELDS = (
    "retrieval_text",
    "main_topic",
    "visual_kind",
    "menu_route",
    "visible_screen_names",
    "visible_buttons",
    "visible_fields",
    "visible_values",
    "codes_and_errors",
    "diagram_title",
    "diagram_nodes",
    "diagram_edges",
    "diagram_summary",
    "chart_title",
    "chart_type",
    "chart_axes",
    "chart_legends",
    "chart_series",
    "chart_summary",
    "table_headers",
    "table_rows",
    "table_summary",
    "form_fields",
    "form_layout",
    "operation_steps",
    "condition_result_pairs",
    "exception_or_cautions",
    "surrounding_context",
    "correction_notes",
    "search_keywords",
    "query_rewrites",
    "answerable_questions",
)

# 画面の図の項目の種類のうち、表示の語を持つ値
# （rag_engine の FORM_FIELD_KINDS から unknown を除いたもの）。
FORM_FIELD_KIND_CHOICES = ("select", "text", "checkbox", "radio", "date", "button")


class FieldKind(StrEnum):
    """項目の値の種類（画面の欄と検証が使う）。"""

    WORDS = "words"  # 語の一覧
    PAIRS = "pairs"  # 開きと閉じの 2 文字の組
    NUMBER_PATTERNS = "number_patterns"  # N を番号の位置にした型（N が要る）
    PATTERNS = "patterns"  # 型（N・YYYY などの記号は項目ごとの意味）
    CHAR_VALUES = "char_values"  # 「字=数」（字は 1 文字）
    WORD_MAP = "word_map"  # 「語=値」
    ENUM_MAP = "enum_map"  # 「語=決まった値」
    CHAR_RANGES = "char_ranges"  # 1 文字か「最初-最後」
    CHOICE = "choice"  # 決まった値から 1 つ
    TEXT = "text"  # 1 つの語（表示のラベル・検索の文の見出し）


@dataclass(frozen=True)
class FieldSpec:
    """言語資源の項目の定義（値は持たない）。"""

    section: str
    key: str
    kind: FieldKind
    # ENUM_MAP の値・CHOICE の選択肢（処理が対応する値。言語の書き方ではない）。
    choices: tuple[str, ...] = ()
    # 取込のときに使う書き方か（変えたら文書の再分割が要る）。
    ingest: bool = True
    # 1 つの語の長さの上限（回答の定型文のような文の項目は長くできる）。
    max_chars: int = MAX_ITEM_CHARS
    # 数値マッピングの値の下限（除数に使う倍率は正の数）。
    min_value: int = 0


def _fields(section: str, kind: FieldKind, *keys: str, **options: Any) -> tuple[FieldSpec, ...]:
    return tuple(FieldSpec(section, key, kind, **options) for key in keys)


_W, _P, _NP, _PT = FieldKind.WORDS, FieldKind.PAIRS, FieldKind.NUMBER_PATTERNS, FieldKind.PATTERNS
_CV, _WM, _EM, _CR = (
    FieldKind.CHAR_VALUES,
    FieldKind.WORD_MAP,
    FieldKind.ENUM_MAP,
    FieldKind.CHAR_RANGES,
)

_T = FieldKind.TEXT

# 節と項目の定義（画面の並び）。値は組み込みのパックと上書きにある。
FIELD_SPECS: tuple[FieldSpec, ...] = (
    # 番号の書き方。
    *_fields(
        "numbers",
        _W,
        "number_prefixes",
        "part_units",
        "chapter_units",
        "section_units",
        "subsection_units",
        "division_units",
        "article_units",
        "paragraph_units",
        "item_units",
    ),
    *_fields("numbers", _PT, "item_patterns"),
    *_fields("numbers", _CV, "kanji_digits"),
    *_fields("numbers", _CV, "kanji_multipliers", min_value=1),
    # ローマ数字の字と値（「ii」「xiv」。前付きの頁番号を読む。英字の大小は区別しない）。
    *_fields("numbers", _CV, "roman_digits", min_value=1),
    *_fields(
        "numbers", _W, "branch_connectors", "branch_stop_chars", "era_names", "first_year_words"
    ),
    # 「ア」「(ア)」のように番号の代わりに使う仮名の並び（1 文字か範囲）。
    *_fields("numbers", _CR, "listing_kana"),
    # 本文の数の後の助数詞（「六十五歳」の「歳」。数の算用数字の表記を検索の文に足す）と、数の後の
    # 語に使う字（1 文字か範囲）・語の頭にしない字（「第一項又は」の「又」）。
    *_fields("numbers", _W, "counters"),
    *_fields("numbers", _CR, "number_term_chars"),
    *_fields("numbers", _W, "term_stop_chars"),
    # 元号の略記（「R6」の「R」= 令和）・西暦を表す語・日付の単位・年度の単位。
    *_fields("numbers", _WM, "era_letters"),
    *_fields("numbers", _W, "calendar_words", "date_units", "fiscal_year_units"),
    # 件数を数える助数詞（「5 つ」「6 件」）と、範囲のつなぎの語（「第 2 章から第 6 章」）。
    *_fields("numbers", _W, "count_counters", "range_connectors"),
    # 記号と文字。
    FieldSpec("symbols", "normalization", FieldKind.CHOICE, choices=("NFKC", "NFC", "none")),
    *_fields(
        "symbols",
        _W,
        "title_ignored_chars",
        "sentence_end_chars",
        "heading_terminal_chars",
        "paragraph_terminal_chars",
        "sentence_closers",
        "sentence_like_endings",
        "sentence_ending_followers",
        "clause_separator_chars",
    ),
    *_fields("symbols", _P, "bracket_pairs", "quote_brackets", "document_title_brackets"),
    *_fields("symbols", _CR, "name_chars"),
    # 参照の書き方。
    *_fields(
        "references",
        _W,
        "cue_words",
        "connector_words",
        "document_suffixes",
        "numbered_document_suffixes",
        "excluded_name_endings",
        "generic_names",
        "demonstrative_words",
        "self_words",
        "document_nouns",
        "name_prefix_particles",
        "possessive_particles",
        "name_label_connectors",
        "part_prefixes",
    ),
    *_fields("references", _NP, "separate_part_patterns"),
    *_fields("references", _W, "previous_words", "next_words", "same_words"),
    *_fields("references", _WM, "appendix_words"),
    *_fields("references", _W, "document_name_predicates", "coordination_particles"),
    *_fields("references", _W, "derived_document_suffixes"),
    *_fields("references", _WM, "abbreviation_targets"),
    # 文書名の照合で外す表記。
    *_fields("titles", _PT, "edition_patterns"),
    *_fields("titles", _W, "edition_words", "company_suffixes"),
    # 英語の参照の書き方。
    *_fields("english", _W, "en_cue_words"),
    FieldSpec(
        "english",
        "en_section_words",
        FieldKind.ENUM_MAP,
        choices=("chapter", "section", "article", "appendix"),
    ),
    *_fields("english", _W, "en_document_suffixes"),
    *_fields("english", _P, "en_quote_brackets"),
    *_fields("english", _W, "en_document_connectors", "en_articles"),
    # 段落・箇条書き・ページの印（解析・分割が行の種類と見出しを見分ける）。
    *_fields("markers", _W, "bullet_chars"),
    *_fields("markers", _CR, "circled_numbers"),
    *_fields("markers", _W, "number_closers"),
    *_fields("markers", _P, "number_brackets", "route_brackets"),
    *_fields("markers", _W, "route_arrows", "series_openers", "heading_form_openers"),
    *_fields("markers", _W, "page_words", "header_words", "footer_words", "range_dashes"),
    *_fields("markers", _W, "all_pages_words", "instruction_endings"),
    FieldSpec(
        "markers",
        "element_kind_words",
        FieldKind.ENUM_MAP,
        choices=(
            "title",
            "text",
            "table",
            "list",
            "figure",
            "figure_caption",
            "header",
            "footer",
            "equation",
        ),
    ),
    *_fields("markers", _W, "form_checked_values", "form_unchecked_values"),
    # 定型文と装飾（ページの帯・著作権・社名・ロゴ・透かし・操作の語）。解析と分割が読み飛ばす・
    # 見分ける。
    *_fields(
        "boilerplate",
        _W,
        "company_words",
        "boilerplate_words",
        "logo_words",
        "decorative_type_words",
        "operation_words",
        "business_words",
        "compare_ignored_chars",
    ),
    # 解析が作る表示のラベル（XML ツリー JSON の部・別表・前文・範囲・版・文書の情報の項目の名前）。
    *_fields(
        "labels",
        _T,
        "default_document_type",
        "whole_document",
        "graph_contains",
        "graph_document",
        "graph_section",
        "main_part",
        "original_supplement",
        "remarks",
        "appendix",
        "preamble",
        "excerpt",
        "range_start",
        "range_end",
        "year",
        "month",
        "day",
        "xml_tree_document_type",
        "field_title",
        "field_number",
        "field_id",
        "field_type",
        "field_abbrev",
        "field_category",
        "field_revision_date",
    ),
    *_fields("labels", _P, "label_brackets"),
    # 表・図の内容のラベル（chunk の本文と検索の文に入る）。変えたら文書の分割し直しが要る。
    *_fields(
        "content_labels",
        _T,
        "related_headings",
        "table_title",
        "column_headers",
        "column_number",
        "column_range",
        "table",
        "table_caption",
        "row_range",
        "table_pictures",
        "table_picture_supplement",
        "table_vision_supplement",
        "table_unassigned",
        "picture_description",
        "picture_ocr",
        "vision_group_prefix",
        "row_number",
        "condition_evidence",
    ),
    *_fields("content_labels", _W, "screen_example_cues"),
    FieldSpec(
        "content_labels",
        "vision_groups",
        FieldKind.ENUM_MAP,
        choices=("summary", "visible", "structure", "relation", "supplement", "search"),
    ),
    FieldSpec(
        "content_labels",
        "vision_fields",
        FieldKind.ENUM_MAP,
        choices=VISION_FIELDS,
    ),
    # 画面の図の項目の種類（Vision の form_fields の kind。#2404）の表示の語。最初の語を chunk の
    # 本文に出し、どの語も業務ガイドの条件の選択肢との照合に使う。unknown は表示しない。
    FieldSpec(
        "content_labels",
        "form_field_kinds",
        FieldKind.ENUM_MAP,
        choices=FORM_FIELD_KIND_CHOICES,
    ),
    # 検索の文（全文検索と埋め込みに入る）の行の見出し。変えたら文書の分割し直しが要る。
    *_fields(
        "search_labels",
        _T,
        "document",
        "abbrev",
        "citation",
        "items",
        "part",
        "caption",
        "path",
        "numbers",
        "body",
        "references",
    ),
    # 全文検索の質問の語（質問を語に分けるときだけ使う。索引は変えないので、文書の分割し直しは
    # 要らない）。
    *_fields(
        "search_terms",
        _W,
        "stopwords",
        "generic_terms",
        "single_char_terms",
        "excluded_terms",
        "particle_edge_chars",
        "polite_suffixes",
        "kept_pos",
        "dropped_pos_details",
        "split_boundary_chars",
        "feedback_split_words",
        "feedback_strip_suffixes",
        ingest=False,
    ),
    *_fields("search_terms", _CR, "kanji_chars", "katakana_chars", "hiragana_chars", ingest=False),
    *_fields("search_terms", _WM, "en_irregular_forms", ingest=False),
    # 質問の理解の手がかりの語（求める内容・条件・経緯・対象を見分ける。検索・回答のときだけ使う）。
    *_fields(
        "question_cues",
        _W,
        "object_suffixes",
        "document_kind_chars",
        "existential_locatives",
        "existential_particles",
        "existential_predicates",
        "requester_terms",
        "requester_sides",
        "requester_particles",
        "requester_verbs",
        "past_markers",
        "contrast_particles",
        "change_words",
        "changed_field_particles",
        "changed_field_endings",
        "clause_split_chars",
        "contrast_clause_endings",
        "sentence_split_chars",
        "unit_split_chars",
        "comma_chars",
        "hypothesis_cues",
        "means_cues",
        "wish_endings",
        "withdrawn_cues",
        "tried_action_cues",
        "done_state_verbs",
        "done_marks",
        "observed_state_cues",
        "passive_state_endings",
        "condition_cues",
        "uncertainty_cues",
        "recipient_subject_words",
        "recipient_action_words",
        "recipient_list_words",
        "list_words",
        "list_output_words",
        "choice_words",
        "absence_words",
        "count_cues",
        "member_cues",
        "basis_cues",
        "change_verbs",
        "change_verb_endings",
        "change_wish_cues",
        "method_cues",
        "procedure_cues",
        "requested_action_words",
        "output_format_words",
        "answer_instruction_cues",
        "mixed_number_notation_cues",
        "answer_instruction_endings",
        "explicit_condition_cues",
        "aggregation_words",
        "member_subject_words",
        "cause_cues",
        "cause_nouns",
        "cause_noun_question_endings",
        "cause_noun_question_leads",
        "generic_target_words",
        "object_particles",
        "manner_words",
        "action_target_verbs",
        "action_request_endings",
        "error_words",
        "error_connectors",
        "error_clause_particles",
        "error_clause_particle_exceptions",
        "question_final_particles",
        "request_cues",
        "facet_method_cues",
        "facet_impact_cues",
        "facet_output_cues",
        "facet_necessity_cues",
        "facet_withdrawal_objects",
        "facet_withdrawal_words",
        "topic_particles",
        "parallel_connectors",
        "target_particles",
        "split_operation_words",
        "chain_connectors",
        "count_query_words",
        "list_query_words",
        "hidden_field_words",
        "hidden_field_states",
        "fetch_failure_cues",
        "grouping_extra_words",
        "grouping_suffixes",
        "filter_words",
        "update_words",
        "software_version_words",
        ingest=False,
    ),
    # 質問に並べた小問の印（丸数字・「(N)」など）と、印の直後に来れば小問ではなく
    # 資料の番号への言及（「①の欄」）になる語。資料の手順の印（markers.circled_numbers）
    # とは分け、質問の文だけに使う。丸数字は範囲ごとに 1 つの並び。
    *_fields("question_cues", _CR, "subquestion_circled_numbers", ingest=False),
    *_fields("question_cues", _NP, "subquestion_number_patterns", ingest=False),
    *_fields("question_cues", _W, "subquestion_reference_followers", ingest=False),
    # 問い合わせの条件（文書の種類・判断の種類・問い合わせの種別の手がかり）。文書の種類・
    # 種別の手がかり・条件とファイルの語は、取込で chunk の metadata にも使う
    # （変えたら分割し直しが要る）。
    FieldSpec("inquiry_cues", "document_kind_words", FieldKind.WORD_MAP),
    FieldSpec("inquiry_cues", "file_data_kind", FieldKind.TEXT),
    *_fields(
        "inquiry_cues",
        _W,
        "operation_trigger_terms",
        "condition_trigger_terms",
        "condition_terms",
        "file_data_terms",
    ),
    FieldSpec(
        "inquiry_cues",
        "decision_words",
        FieldKind.ENUM_MAP,
        choices=("procedure", "necessity", "permission", "reason", "count", "output_impact"),
        ingest=False,
    ),
    *_fields(
        "inquiry_cues",
        _W,
        "visual_terms",
        "external_context_terms",
        "screen_suffixes",
        "screen_name_bracket_chars",
        "route_arrow_chars",
        "interrogative_words",
        "term_joiner_chars",
        "page_ordinal_suffixes",
        "column_words",
        ingest=False,
    ),
    # 列挙・網羅を求める質問の手がかり（件数・章の範囲・読む範囲。#1503）。
    *_fields(
        "enumeration_cues",
        _W,
        "exhaustive_quantifiers",
        "quantifier_modifier_particles",
        "exhaustive_words",
        "per_unit_words",
        "listing_words",
        "each_words",
        "document_wide_words",
        "document_name_words",
        "document_inside_words",
        "document_scope_particles",
        ingest=False,
    ),
    # 項目の意味を尋ねる質問と、原文の定義の行の手がかり。
    FieldSpec("definition_cues", "definition_label_brackets", FieldKind.PAIRS, ingest=False),
    *_fields(
        "definition_cues",
        _W,
        "definition_question_cues",
        "label_separators",
        "field_words",
        "label_particles",
        "label_preceders",
        "label_followers",
        "definition_suffixes",
        "definition_bullets",
        "definition_ellipsis_chars",
        "definition_delimiters",
        "stop_heading_words",
        "stop_bullet_chars",
        "definition_body_cues",
        "definition_topic_words",
        ingest=False,
    ),
    # 根拠の選び方の手がかり（根拠の関連度・引用の窓・手順の行・機能の見出し・図の要否・
    # 操作の接続）。
    *_fields(
        "evidence_cues",
        _W,
        "aggregate_value_words",
        "detail_words",
        "link_verbs",
        "link_negations",
        "navigation_words",
        "navigation_evidence_words",
        "screen_place_words",
        "place_particles",
        "entry_action_words",
        "function_number_dashes",
        "heading_label_openers",
        "operation_section_words",
        "alternative_query_words",
        "focus_particles",
        "target_stop_terms",
        "heading_bullet_chars",
        "item_bullet_chars",
        "note_mark_chars",
        "label_colon_chars",
        "step_number_closers",
        "relevance_stop_terms",
        "operation_text_cues",
        "default_question_cues",
        "default_text_cues",
        "list_text_cues",
        "list_action_words",
        "list_execute_words",
        "step_condition_cues",
        "step_action_words",
        ingest=False,
    ),
    FieldSpec("evidence_cues", "section_label_brackets", FieldKind.PAIRS, ingest=False),
    FieldSpec("evidence_cues", "step_circled_numbers", FieldKind.CHAR_RANGES, ingest=False),
    # 回答の本文の見出し・定型文・出典・引用と条件の表示（回答の検査と評価も同じ語で読み分ける）。
    *_fields(
        "answer_labels",
        _T,
        "rules_section",
        "quote_only_section",
        "quote_only_note",
        "gaps_section",
        "unverified_section",
        "deadline_section",
        "confirmations_section",
        "confirmation_only_summary",
        "neutral_summary",
        "insufficient_evidence_summary",
        "empty_search_guidance",
        "abstention_guidance",
        "answer_evidence_budget_limited",
        "citation_prefix",
        "deadline_summary",
        "off_goal_gap",
        "cause_gap",
        "crag_refusal_summary",
        "crag_missing_aspect_template",
        "crag_unknown_aspect_template",
        "crag_quote_only_note",
        "crag_definition_aspect_template",
        "operation_steps_heading",
        "confirmation_steps_heading",
        "page_location",
        "sheet_location",
        "item_bullet",
        "steps_heading_template",
        "quote_template",
        "condition_template",
        "condition_suffix",
        "condition_link",
        "subquestion_heading_template",
        "missing_request_template",
        "removed_section",
        "removed_section_note",
        "marked_section",
        "marked_section_note",
        "unsourced_section",
        "unsourced_section_note",
        "marked_rules_section",
        "marked_rules_section_note",
        "guide_notes_section",
        "guide_notes_note_template",
        "guide_handoff_template",
        "guide_approval_template",
        "guide_acquisition_incomplete_template",
        "passage_note_template",
        "answer_warning_template",
        ingest=False,
        max_chars=MAX_SENTENCE_CHARS,
    ),
    *_fields("answer_labels", _W, "predicate_endings", ingest=False),
    FieldSpec(
        "answer_labels",
        "aspect_labels",
        FieldKind.ENUM_MAP,
        choices=(
            "business_object",
            "requested_result",
            "applicability",
            "procedure",
            "member_details",
            "population_membership",
            "enumeration_coverage",
        ),
        ingest=False,
    ),
    # 回答の段落の印・外した段落・回答全体の警告の理由の表示（#2738。値は
    # platform/contracts/answer-passages/passage-marks.json の reason_codes）。
    FieldSpec(
        "answer_labels",
        "passage_reason_labels",
        FieldKind.ENUM_MAP,
        choices=(
            "unsupported",
            "citation_error",
            "quote_unmatched",
            "citation_id_invalid",
            "citation_id_namespace",
            "citation_id_ambiguous",
            "citation_range_missing",
            "citation_range_invalid",
            "citation_range_out_of_bounds",
            "citation_extraction_failed",
            "citation_heading_only",
            "citation_content_unusable",
            "citation_version_mismatch",
            "unassessed",
            "figure_not_read",
            "target_mismatch",
            "value_unbound",
            "table_corrected",
            "item_check",
            "superseded",
            "after_contradicted_step",
            "after_unverified_step",
            "summary_unaudited",
            "application_unaudited",
            "guide_text",
            "contradicted",
            "guide_check",
            "no_evidence",
            "off_goal",
            "requests",
            "correction_failed",
        ),
        ingest=False,
    ),
    # 回答の検査の手がかり（確定の操作・限定・未説明・保証・推測・条件の句・但し書き・
    # 版の廃止など）。
    *_fields(
        "answer_check_cues",
        _W,
        "completion_verbs",
        "closing_quote_chars",
        "button_words",
        "press_words",
        "completion_connectors",
        "completion_words",
        "completion_negations",
        "constraint_words",
        "condition_preceders",
        "condition_nouns",
        "condition_followers",
        "heading_screen_words",
        "field_label_followers",
        "unexplained_words",
        "guarantee_verbs",
        "guarantee_endings",
        "guarantee_exceptions",
        "match_claims",
        "speculation_words",
        "ignored_quote_chars",
        "quote_sentence_end_chars",
        "step_split_closers",
        "ellipsis_chars",
        "repeated_ellipsis_chars",
        "report_suffixes",
        "screen_object_suffixes",
        "condition_sentence_endings",
        "condition_clause_nouns",
        "condition_time_nouns",
        "condition_time_followers",
        "condition_strip_particles",
        "condition_trailing_particles",
        "condition_range_particles",
        "condition_topic_particles",
        "condition_adverb_kanji",
        "condition_adverb_particles",
        "proviso_words",
        "bare_label_chars",
        "sentence_line_endings",
        "sentence_line_closers",
        "navigation_residue_words",
        "related_stopwords",
        "version_units",
        "abolish_words",
        "abolish_particles",
        "abolished_forms",
        "version_question_cues",
        "quote_operation_words",
        "process_mode_words",
        "mode_topic_particles",
        "mode_condition_words",
        "deletion_words",
        "bare_title_marks",
        "evidence_id_labels",
        "output_artifact_words",
        "output_field_words",
        ingest=False,
    ),
    FieldSpec("answer_check_cues", "heading_bracket_pairs", FieldKind.PAIRS, ingest=False),
    # 回答の操作の指示の手がかり（引用の無い操作の指示・原文に無い操作の語・削除の対象）。
    *_fields(
        "operation_audit_cues",
        _W,
        "instruction_stems",
        "operation_instruction_endings",
        "instruction_phrases",
        "conditional_operation_verbs",
        "conditional_operation_endings",
        "action_verbs",
        "action_endings",
        "control_words",
        "control_actions",
        "deletion_endings",
        "english_instruction_words",
        "deletion_manner_words",
        ingest=False,
    ),
    FieldSpec("operation_audit_cues", "operation_families", FieldKind.WORD_MAP, ingest=False),
    # 回答の値と操作部品の裏付けの手がかり（数の規則・今回の値・ボタンとキー・別名・算式の単位・
    # 画像の操作）。
    *_fields(
        "grounding_cues",
        _W,
        "numeric_rule_words",
        "digit_count_units",
        "denial_verbs",
        "denial_endings",
        "denial_words",
        "digit_range_labels",
        "current_subject_words",
        "attribute_words",
        "attribute_particles",
        "hedge_words",
        "example_words",
        "rule_denial_prefixes",
        "source_qualifiers",
        "user_subject_words",
        "user_subject_particles",
        "rule_condition_words",
        "alternative_connectors",
        "key_words",
        "alias_followers",
        "alias_words",
        "alias_negations",
        "internal_label_words",
        "amount_units",
        "fact_counters",
        "native_operation_words",
        "grounding_stopwords",
        ingest=False,
    ),
    FieldSpec("grounding_cues", "amount_scale_units", FieldKind.CHAR_VALUES, ingest=False),
    # 実体の索引（システム・部署・属性の値と別名。#1362）の手がかり。取込で chunk から実体を抜き出す
    # （変えたら文書の分割し直しが要る）。
    *_fields(
        "entity_cues",
        _W,
        "id_column_words",
        "alias_column_words",
        "name_column_words",
        "skip_column_words",
        "value_separators",
        "numeric_value_chars",
        "code_definition_words",
        "definition_separators",
        "paren_alias_words",
        "nickname_words",
        "organization_suffixes",
        "company_scope_suffixes",
        "generic_company_words",
        "subject_link_particles",
        "subject_topic_particles",
        "sentence_marks",
        "clause_marks",
    ),
    *_fields(
        "entity_cues", _CR, "entity_script_chars", "entity_kana_kanji_chars", "entity_kanji_chars"
    ),
    # 業務ガイドの手順と回答の照らし合わせの手がかり（影響範囲・承認・満たした前提・
    # 見出しと箇条の行）。
    *_fields(
        "guide_check_cues",
        _W,
        "impact_group_terms",
        "impact_all_terms",
        "approval_terms",
        "premise_met_markers",
        "guide_list_marks",
        "guide_number_closers",
        ingest=False,
    ),
    FieldSpec("guide_check_cues", "boolean_aliases", FieldKind.WORD_MAP, ingest=False),
    # 回答の過程を書いた表から業務ガイドの下書きを作るときの、節の見出しの語と業務ガイドの項目
    # （「見出し=項目」。項目は手順・確認事項・資料と頁・検索の語・補足）。
    FieldSpec(
        "guide_import_cues",
        "process_headings",
        FieldKind.ENUM_MAP,
        choices=("steps", "conditions", "references", "search_terms", "notes"),
        ingest=False,
    ),
    # 項目抽出の値の書き方（はい / いいえの語）。取込で抽出した値をそろえる
    # （変えたら分割し直しが要る）。
    *_fields("field_value_cues", _W, "true_values", "false_values"),
)
SECTIONS: tuple[str, ...] = tuple(dict.fromkeys(spec.section for spec in FIELD_SPECS))
_SPECS = {(spec.section, spec.key): spec for spec in FIELD_SPECS}


class LanguagePackError(ValueError):
    """言語資源の形の誤り。``errors`` は「節.項目」→ 理由。"""

    def __init__(self, errors: Mapping[str, str]) -> None:
        super().__init__("; ".join(f"{key}: {message}" for key, message in errors.items()))
        self.errors = dict(errors)


@dataclass(frozen=True)
class LanguagePack:
    """検証済みの言語資源（節 → 項目 → 値。値は語の tuple か文字列）。"""

    locale: str
    sections: Mapping[str, Mapping[str, Any]]
    # 全体の hash（組み立てたものの cache の鍵）と、取込の項目だけの hash（chunk に残し、
    # 分割し直しが要る文書を見分ける。検索・回答だけの項目を変えても文書は古くならない）。
    hash: str
    ingest_hash: str

    def value(self, section: str, key: str) -> Any:
        return self.sections[section][key]

    def text(self, section: str, key: str) -> str:
        """1 つの語の項目の値。"""
        value = self.sections[section][key]
        return value if isinstance(value, str) else ""

    def words(self, section: str, key: str) -> tuple[str, ...]:
        value = self.sections[section][key]
        return value if isinstance(value, tuple) else ()

    def mapping(self, section: str, key: str) -> dict[str, str]:
        """「語=値」の項目を、語 → 値の dict にする。"""
        result: dict[str, str] = {}
        for item in self.words(section, key):
            word, _sep, value = item.partition(MAP_SEPARATOR)
            result[word.strip()] = value.strip()
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "locale": self.locale,
            **{
                section: {
                    key: list(value) if isinstance(value, tuple) else value
                    for key, value in fields.items()
                }
                for section, fields in self.sections.items()
            },
        }


def _clean_words(value: Any) -> tuple[str, ...]:
    items = value.split("\n") if isinstance(value, str) else list(value or [])
    return tuple(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))


def _check(spec: FieldSpec, value: Any) -> tuple[Any, str | None]:
    """値をそろえ、形の誤りがあれば理由を返す。"""
    if spec.kind is FieldKind.CHOICE:
        text = str(value or "").strip()
        return text, None if text in spec.choices else f"{'・'.join(spec.choices)} のどれかです。"
    if spec.kind is FieldKind.TEXT:
        text = str(value or "").strip()
        if not text:
            return text, "1 つの語を書いてください。"
        return text, None if len(
            text
        ) <= spec.max_chars else f"{spec.max_chars} 文字以内で書いてください。"
    words = _clean_words(value)
    if len(words) > MAX_ITEMS:
        return words, f"{MAX_ITEMS} 件以内で書いてください。"
    if any(len(word) > spec.max_chars for word in words):
        return words, f"1 つは {spec.max_chars} 文字以内で書いてください。"
    for word in words:
        left, sep, right = word.partition(MAP_SEPARATOR)
        left, right = left.strip(), right.strip()
        if spec.kind is FieldKind.PAIRS and len(word) != 2:
            return words, "開きと閉じの 2 文字で書いてください(例: 「」)。"
        if spec.kind is FieldKind.NUMBER_PATTERNS and "N" not in word:
            return words, "番号の位置を N で書いてください(例: 第N号)。"
        if spec.kind is FieldKind.CHAR_VALUES and not (
            sep and len(left) == 1 and right.isascii() and right.isdecimal()
        ):
            return words, "「字=数」の形で、字は 1 文字で書いてください(例: 一=1)。"
        if spec.kind is FieldKind.CHAR_VALUES and int(right) < spec.min_value:
            return words, f"数は {spec.min_value} 以上で書いてください。"
        if spec.kind is FieldKind.WORD_MAP and not (sep and left and right):
            return words, "「語=値」の形で書いてください(例: 別添=別紙)。"
        if spec.kind is FieldKind.ENUM_MAP and not (sep and left and right in spec.choices):
            return words, f"「語=値」の形で、値は {'・'.join(spec.choices)} のどれかです。"
        if spec.kind is FieldKind.CHAR_RANGES and not (
            len(word) == 1 or (len(word) == 3 and word[1] == "-" and word[0] <= word[2])
        ):
            return words, "1 文字か「最初-最後」の範囲で書いてください(例: ー、一-鿿)。"
    return words, None


# 既存の公開版を読み取る際だけ補える、索引に影響しない追加の表示文言。
_COMPATIBLE_ANSWER_LABELS = frozenset(
    {
        "insufficient_evidence_summary",
        "empty_search_guidance",
        "abstention_guidance",
        "answer_evidence_budget_limited",
        "crag_refusal_summary",
        "crag_missing_aspect_template",
        "crag_unknown_aspect_template",
        "crag_quote_only_note",
        "crag_definition_aspect_template",
        "guide_acquisition_incomplete_template",
    }
)


@lru_cache(maxsize=4)
def _builtin_data(locale: str = DEFAULT_LOCALE) -> dict[str, Any]:
    text = resources.files(__package__).joinpath("packs", f"{locale}.json").read_text("utf-8")
    data: dict[str, Any] = json.loads(text)
    return data


def complete_compatible_pack(data: Mapping[str, Any]) -> dict[str, Any]:
    """追加した表示文言の欠けだけを補い、呼び出し元の保存内容は変更しない。"""
    result = dict(data)
    labels = data.get("answer_labels")
    if isinstance(labels, Mapping):
        completed = dict(labels)
        defaults = _builtin_data()["answer_labels"]
        for key in _COMPATIBLE_ANSWER_LABELS:
            if key not in completed:
                completed[key] = defaults[key]
        result["answer_labels"] = completed
    return result


def stored_pack_hash(data: Mapping[str, Any], pack: LanguagePack) -> str:
    """保存時の項目集合で照合する。許可した欠け以外の正規化は受け入れない。"""
    if complete_compatible_pack(data) != pack.to_dict():
        raise ValueError("言語資源は全項目を含む完全な設定で保存してください。")
    labels = data.get("answer_labels") or {}
    specs = tuple(
        spec
        for spec in FIELD_SPECS
        if not (
            spec.section == "answer_labels"
            and spec.key in _COMPATIBLE_ANSWER_LABELS
            and spec.key not in labels
        )
    )
    return _digest(pack.locale, pack.sections, specs)


def build_pack(data: Mapping[str, Any]) -> LanguagePack:
    """追加表示文言の欠けだけ補い、それ以外の欠け・余り・形の誤りを拒否する。"""
    data = complete_compatible_pack(data)
    errors: dict[str, str] = {}
    sections: dict[str, dict[str, Any]] = {section: {} for section in SECTIONS}
    for spec in FIELD_SPECS:
        raw = (data.get(spec.section) or {}).get(spec.key)
        if raw is None:
            errors[f"{spec.section}.{spec.key}"] = "値がありません。"
            continue
        value, error = _check(spec, raw)
        if error:
            errors[f"{spec.section}.{spec.key}"] = error
        sections[spec.section][spec.key] = value
    for section, fields in data.items():
        if section in ("locale", "version"):
            continue
        for key in fields or {}:
            if (section, key) not in _SPECS:
                errors[f"{section}.{key}"] = "知らない項目です。"
    if errors:
        raise LanguagePackError(errors)
    locale = str(data.get("locale") or DEFAULT_LOCALE)
    return LanguagePack(
        locale=locale,
        sections=sections,
        hash=_digest(locale, sections, FIELD_SPECS),
        ingest_hash=_digest(locale, sections, tuple(spec for spec in FIELD_SPECS if spec.ingest)),
    )


def _digest(
    locale: str, sections: Mapping[str, Mapping[str, Any]], specs: tuple[FieldSpec, ...]
) -> str:
    canonical = json.dumps(
        [
            [spec.section, spec.key, list(value) if isinstance(value, tuple) else value]
            for spec in sorted(specs, key=lambda spec: (spec.section, spec.key))
            for value in (sections[spec.section][spec.key],)
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(f"{locale}\n{canonical}".encode()).hexdigest()[:16]


@lru_cache(maxsize=4)
def builtin_pack(locale: str = DEFAULT_LOCALE) -> LanguagePack:
    """組み込みのパック（パッケージに同梱した既定値）。"""
    return build_pack(_builtin_data(locale))


def resolve(base: LanguagePack, *overrides: Mapping[str, Mapping[str, Any]] | None) -> LanguagePack:
    """組み込み（か下の層）の言語資源に、上書きを順に重ねる（項目ごとに置き換える）。"""
    data: dict[str, Any] = {
        "locale": base.locale,
        **{section: dict(fields) for section, fields in base.sections.items()},
    }
    unknown: dict[str, str] = {}
    for override in overrides:
        for section, fields in (override or {}).items():
            for key, value in (fields or {}).items():
                if (section, key) not in _SPECS:
                    unknown[f"{section}.{key}"] = "知らない項目です。"
                    continue
                data[section][key] = value
    if unknown:
        raise LanguagePackError(unknown)
    return build_pack(data)


def overrides_from(pack: LanguagePack, base: LanguagePack) -> dict[str, dict[str, Any]]:
    """``base`` と違う項目だけの上書き（保存の形）。"""
    result: dict[str, dict[str, Any]] = {}
    for spec in FIELD_SPECS:
        value = pack.value(spec.section, spec.key)
        if value != base.value(spec.section, spec.key):
            result.setdefault(spec.section, {})[spec.key] = (
                list(value) if isinstance(value, tuple) else value
            )
    return result


_default: LanguagePack | None = None
_bound: ContextVar[LanguagePack | None] = ContextVar("rag_language_pack", default=None)


def set_default_pack(pack: LanguagePack | None) -> None:
    """束ねていないときに使う言語資源（backend の全体の上書き）。None は組み込みのパック。"""
    global _default
    _default = pack


def default_pack() -> LanguagePack:
    """要求に束ねた値を含まない全体の既定。プロファイル間で上書きを継承しない。"""
    return _default or builtin_pack()


def current_pack() -> LanguagePack:
    """今の言語資源（束ねたもの → 全体の既定 → 組み込みの順）。"""
    return _bound.get() or default_pack()


def per_pack[T](build: Callable[[LanguagePack], T]) -> Callable[[], T]:
    """言語資源から組み立てるもの（語の集合・正規表現）を、今の言語資源の hash ごとに 1 回だけ
    作る関数。

    ponytail: hash ごとの cache は消さない。言語資源の版は保存の回数しか増えない。
    """
    built: dict[str, T] = {}

    def get() -> T:
        pack = current_pack()
        value = built.get(pack.hash)
        if value is None:
            value = built[pack.hash] = build(pack)
        return value

    return get


def overrides_payload(pack: LanguagePack | None = None) -> dict[str, dict[str, Any]]:
    """別のプロセス（解析・分割のサービス）へ送る、組み込みとの差（今の言語資源。差が無ければ空）。"""
    return overrides_from(pack or current_pack(), builtin_pack())


def pack_from_payload(payload: Mapping[str, Mapping[str, Any]] | str | None) -> LanguagePack | None:
    """要求に付いた組み込みとの差から言語資源を作る（無い・空なら None = 組み込みのまま）。"""
    if isinstance(payload, str):
        payload = json.loads(payload) if payload.strip() else None
    if not payload:
        return None
    if "snapshot" in payload:
        if set(payload) != {"snapshot"} or not isinstance(payload["snapshot"], Mapping):
            raise LanguagePackError({"snapshot": "完全な言語資源の形式が正しくありません。"})
        return build_pack(payload["snapshot"])
    return resolve(builtin_pack(), payload)


@contextmanager
def bind_pack(pack: LanguagePack | None) -> Iterator[None]:
    """取込の job・回答の要求・解析の要求の間だけ言語資源を束ねる（None は束ねない）。"""
    token = _bound.set(pack)
    try:
        yield
    finally:
        _bound.reset(token)


__all__ = [
    "DEFAULT_LOCALE",
    "LANGUAGE_PACK_HASH_KEY",
    "FIELD_SPECS",
    "FieldKind",
    "FieldSpec",
    "LanguagePack",
    "LanguagePackError",
    "SECTIONS",
    "bind_pack",
    "build_pack",
    "builtin_pack",
    "current_pack",
    "default_pack",
    "overrides_from",
    "overrides_payload",
    "pack_from_payload",
    "per_pack",
    "resolve",
    "set_default_pack",
]
