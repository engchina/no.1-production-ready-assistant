"""回答の本文の見出し・定型文・出典の表示（言語資源の answer_labels。#2181 段 5）。

回答の組み立て（render）が書く語と、回答の検査・評価・指標が回答の本文を読み分ける語を、同じ言語資源から作る。
組み立ては言語資源の hash ごとに 1 回だけ。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rag_parser_core.language import LanguagePack, per_pack


@dataclass(frozen=True)
class AnswerLabels:
    """回答の本文の見出し・定型文・出典の表示。"""

    rules_section: str
    quote_only_section: str
    quote_only_note: str
    gaps_section: str
    unverified_section: str
    deadline_section: str
    confirmations_section: str
    confirmation_only_summary: str
    neutral_summary: str
    insufficient_evidence_summary: str
    empty_search_guidance: str
    abstention_guidance: str
    citation_prefix: str
    deadline_summary: str
    off_goal_gap: str
    cause_gap: str
    crag_refusal_summary: str
    crag_missing_aspect_template: str
    crag_unknown_aspect_template: str
    crag_quote_only_note: str
    crag_definition_aspect_template: str
    operation_steps_heading: str
    confirmation_steps_heading: str
    page_location: str
    sheet_location: str
    item_bullet: str
    steps_heading_template: str
    quote_template: str
    condition_template: str
    condition_suffix: str
    condition_link: str
    # 小問ごとの見出しの型（{label} に小問の印、{text} に小問の文。#2399）。
    subquestion_heading_template: str
    missing_request_template: str
    # 回答の末尾の節（#2738）: 根拠と矛盾して外した内容と、印の付いた段落・回答全体の警告の一覧。
    removed_section: str
    removed_section_note: str
    marked_section: str
    marked_section_note: str
    # 検索した資料の原文と照合できない説明の節（#2792。資料の出典を付けない）と、業務ガイドの案内の節（業務ガイドの
    # 設定の窓口・引き継ぐ条件・承認。{title} に業務ガイドの題名、{contact} に窓口、{conditions} に条件、{note} に承認の補足）。
    unsourced_section: str
    unsourced_section_note: str
    # 印の付いた説明（確かめていない・注意が要る）の節（#3394。「資料により確認できた事項」に入れない）。
    marked_rules_section: str
    marked_rules_section_note: str
    guide_notes_section: str
    guide_notes_note_template: str
    guide_handoff_template: str
    guide_approval_template: str
    guide_acquisition_incomplete_template: str
    # 節の 1 行の型（{text} に段落の短い引用、{reasons} に理由）と、回答全体の警告の行の型。
    passage_note_template: str
    answer_warning_template: str
    # 用言の終わり（「有効期限を過ぎた」）。条件がこれで終わるときは「の」を挟まない (#644)。
    predicate_endings: tuple[str, ...]
    # 観点名（task_contract.required_aspects）→ 利用者向けの語 (#986)。
    aspect_labels: dict[str, str]
    # 段落の印・外した段落・回答全体の警告の理由の code → 利用者向けの短い文（#2738。
    # platform/contracts/answer-passages/passage-marks.json の reason_codes）。
    passage_reason_labels: dict[str, str]
    # render() が決定的に出力する見出し行と出典行、「参考情報」の節の原文の行と節の箇条の行。
    structural_line: re.Pattern[str]
    quote_only_line: re.Pattern[str]
    section_item_line: re.Pattern[str]
    subquestion_heading_line: re.Pattern[str]

    def page(self, page: object) -> str:
        return self.page_location.format(page=page)

    def sheet(self, sheet: str, cell_range: str) -> str:
        return self.sheet_location.format(sheet=sheet, cell_range=cell_range)

    def quote(self, text: str) -> str:
        return self.quote_template.format(quote=text)

    def condition(self, text: str) -> str:
        return self.condition_template.format(condition=text)

    def steps_heading(self, heading: str, title: str) -> str:
        return self.steps_heading_template.format(heading=heading, title=title) if title else heading

    def subquestion_heading(self, label: str, text: str) -> str:
        return self.subquestion_heading_template.format(label=label, text=text)

    def passage_note(self, text: str, reasons: str) -> str:
        return self.passage_note_template.format(text=text, reasons=reasons)

    def answer_warning(self, reasons: str) -> str:
        return self.answer_warning_template.format(reasons=reasons)

    def guide_notes_note(self, title: str) -> str:
        return self.guide_notes_note_template.format(title=title)

    def guide_handoff(self, contact: str, conditions: str) -> str:
        return self.guide_handoff_template.format(contact=contact, conditions=conditions)

    def guide_approval(self, note: str) -> str:
        return self.guide_approval_template.format(note=note).strip()


def _build(pack: LanguagePack) -> AnswerLabels:
    def text(key: str) -> str:
        return pack.text("answer_labels", key)

    note = text("quote_only_note")
    note_sentences = tuple(sentence for sentence in re.split(r"(?<=。)", note) if sentence)
    page_head = re.escape(text("page_location").split("{", 1)[0])
    sheet_head = re.escape(text("sheet_location").split("{", 1)[0])
    steps = "|".join(re.escape(text(key)) for key in ("operation_steps_heading", "confirmation_steps_heading"))
    # 見出しの型の「{title}」の前後（「（」「）」）。
    title_open, _sep, title_close = text("steps_heading_template").partition("{title}")
    title_open = title_open.replace("{heading}", "")
    quote_open, _sep, quote_close = text("quote_template").partition("{quote}")
    item = rf"(?:{re.escape(text('item_bullet'))}|[0-9]+\.\s)"
    # 小問の見出しの型の {label}・{text} 以外の部分（「質問」「：」）。
    subquestion = re.escape(text("subquestion_heading_template"))
    subquestion = subquestion.replace(re.escape("{label}"), r".{1,12}?").replace(re.escape("{text}"), r".*")
    # 業務ガイドの案内の節の注記（{title} は業務ガイドの題名。#2792）。
    guide_note = re.escape(text("guide_notes_note_template")).replace(re.escape("{title}"), r".+")
    titles = (
        text("rules_section"),
        text("quote_only_section"),
        note,
        *note_sentences,
        text("gaps_section"),
        text("unverified_section"),
        text("deadline_section"),
        text("removed_section"),
        text("removed_section_note"),
        text("marked_section"),
        text("marked_section_note"),
        text("unsourced_section"),
        text("unsourced_section_note"),
        text("marked_rules_section"),
        text("marked_rules_section_note"),
        text("guide_notes_section"),
    )
    return AnswerLabels(
        rules_section=text("rules_section"),
        quote_only_section=text("quote_only_section"),
        quote_only_note=note,
        gaps_section=text("gaps_section"),
        unverified_section=text("unverified_section"),
        deadline_section=text("deadline_section"),
        confirmations_section=text("confirmations_section"),
        confirmation_only_summary=text("confirmation_only_summary"),
        neutral_summary=text("neutral_summary"),
        insufficient_evidence_summary=text("insufficient_evidence_summary"),
        empty_search_guidance=text("empty_search_guidance"),
        abstention_guidance=text("abstention_guidance"),
        citation_prefix=text("citation_prefix"),
        deadline_summary=text("deadline_summary"),
        off_goal_gap=text("off_goal_gap"),
        cause_gap=text("cause_gap"),
        crag_refusal_summary=text("crag_refusal_summary"),
        crag_missing_aspect_template=text("crag_missing_aspect_template"),
        crag_unknown_aspect_template=text("crag_unknown_aspect_template"),
        crag_quote_only_note=text("crag_quote_only_note"),
        crag_definition_aspect_template=text("crag_definition_aspect_template"),
        operation_steps_heading=text("operation_steps_heading"),
        confirmation_steps_heading=text("confirmation_steps_heading"),
        page_location=text("page_location"),
        sheet_location=text("sheet_location"),
        item_bullet=text("item_bullet"),
        steps_heading_template=text("steps_heading_template"),
        quote_template=text("quote_template"),
        condition_template=text("condition_template"),
        condition_suffix=text("condition_suffix"),
        condition_link=text("condition_link"),
        subquestion_heading_template=text("subquestion_heading_template"),
        missing_request_template=text("missing_request_template"),
        removed_section=text("removed_section"),
        removed_section_note=text("removed_section_note"),
        marked_section=text("marked_section"),
        marked_section_note=text("marked_section_note"),
        unsourced_section=text("unsourced_section"),
        unsourced_section_note=text("unsourced_section_note"),
        marked_rules_section=text("marked_rules_section"),
        marked_rules_section_note=text("marked_rules_section_note"),
        guide_notes_section=text("guide_notes_section"),
        guide_notes_note_template=text("guide_notes_note_template"),
        guide_handoff_template=text("guide_handoff_template"),
        guide_approval_template=text("guide_approval_template"),
        guide_acquisition_incomplete_template=text("guide_acquisition_incomplete_template"),
        passage_note_template=text("passage_note_template"),
        answer_warning_template=text("answer_warning_template"),
        predicate_endings=pack.words("answer_labels", "predicate_endings"),
        aspect_labels={aspect: label for label, aspect in pack.mapping("answer_labels", "aspect_labels").items()},
        passage_reason_labels={code: label for label, code in pack.mapping("answer_labels", "passage_reason_labels").items()},
        structural_line=re.compile(
            "|".join(re.escape(title) for title in titles)
            + rf"|(?:{steps})(?:{re.escape(title_open)}.+{re.escape(title_close)})?|"
            + re.escape(text("citation_prefix"))
            + rf".*(?: {page_head}.*| {sheet_head}.+)"
            + f"|{subquestion}"
            + f"|{guide_note}"
        ),
        quote_only_line=re.compile(rf"{item}{re.escape(quote_open)}.*{re.escape(quote_close)}"),
        section_item_line=re.compile(item),
        subquestion_heading_line=re.compile(subquestion),
    )


answer_labels = per_pack(_build)
