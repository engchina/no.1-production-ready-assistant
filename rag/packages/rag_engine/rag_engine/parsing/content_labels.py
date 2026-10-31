"""表・図の内容のラベル（言語資源の content_labels。#2181 段 2c）。

表の行グループの見出し・表内画像・画像の説明（Vision）の項目とグループ・OCR の欄の名前は言語資源の
データで、chunk の本文と検索の文に入る。書く側（解析・分割）と読む側（回答の検査）は同じ言語資源から
ラベルと正規表現を作る。組み立ては言語資源の hash ごとに 1 回だけ。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from rag_parser_core.language import LanguagePack, current_pack

_COLON = "[:：]"


def _label_line(labels: list[str]) -> str:
    """「ラベル:」で始まる行の部品（ラベルが無ければ何にも当たらない）。"""
    return "(?:" + "|".join(re.escape(label) for label in labels if label) + ")" + _COLON if labels else "(?!)"


@dataclass(frozen=True)
class ContentLabels:
    """表・図の内容のラベルと、それを読む正規表現。"""

    pack: LanguagePack
    # Vision の項目 → ラベル、グループ → ラベル。
    fields: dict[str, str]
    groups: dict[str, str]
    # グループ見出しの接頭辞（「■ 」）。
    group_prefix: str
    # 生成説明・OCR の始まり（回答用本文・視覚種別・画面/メニュー・OCR の欄）。この前が原文。
    image_marker: re.Pattern[str]
    # 生成説明の印（要点のグループ見出しと主な欄）。
    generated: re.Pattern[str]
    # OCR の欄（次のグループ見出しか末尾まで）。
    ocr_section: re.Pattern[str]
    # 値の欄の行（「値: …」）。
    screen_value_field: re.Pattern[str]
    # 生成説明の画面名の欄（#2797。画面の ID は画面を指す名前で、画面に写った表示例の値ではない）。
    screen_name_field: re.Pattern[str]
    # 表の行の欄（「- N行目: …」）と、その欄の見出し（「表の行:」）。
    table_row: re.Pattern[str]
    table_rows_heading: re.Pattern[str]
    # 検索の手がかりだけの欄（画面/メニュー・検索語・主題）で始まる文。
    index_line: re.Pattern[str]
    # 画面の例の値を示す文（「画面例: …」）。
    screen_example: re.Pattern[str]
    # 画面の図の項目の種類（form_fields の kind）→ その語（最初の語が表示の語。#2404）。
    form_field_kind_words: dict[str, tuple[str, ...]]

    def text(self, key: str) -> str:
        return self.pack.text("content_labels", key)

    def field(self, name: str) -> str:
        """Vision の項目のラベル（言語資源に無ければ項目名）。"""
        return self.fields.get(name, name)

    def group(self, name: str) -> str:
        return self.groups.get(name, name)

    def form_field_kind(self, kind: str) -> str:
        """項目の種類の表示の語（unknown・言語資源に語の無い種類は空）。"""
        words = self.form_field_kind_words.get(kind)
        return words[0] if words else ""

    def field_line(self, *names: str) -> str:
        """指定した項目の「ラベル:」の正規表現の部品。"""
        return _label_line([self.field(name) for name in names])


_by_hash: dict[str, ContentLabels] = {}


def content_labels(pack: LanguagePack | None = None) -> ContentLabels:
    """言語資源（既定は今の言語資源）から作ったラベル（hash ごとに 1 回だけ作る）。"""
    pack = pack or current_pack()
    cached = _by_hash.get(pack.hash)
    if cached is not None:
        return cached
    fields = {field: label for label, field in pack.mapping("content_labels", "vision_fields").items()}
    groups = {group: label for label, group in pack.mapping("content_labels", "vision_groups").items()}

    def text(key: str) -> str:
        return pack.text("content_labels", key)

    def label(name: str) -> str:
        return fields.get(name, name)

    prefix = f"{text('vision_group_prefix')} "
    ocr = text("picture_ocr")
    marker_labels = [label("retrieval_text"), label("visual_kind"), label("menu_route"), ocr]
    row_number = re.escape(text("row_number")).replace(re.escape("{n}"), "[0-9０-９]+").replace(r"\ ", r"\s*")
    cues = []
    for cue in pack.words("content_labels", "screen_example_cues"):
        stem = cue.rstrip(":：")
        cues.append(re.escape(stem) + (_COLON if stem != cue else f"{_COLON}?"))
    kind_words: dict[str, tuple[str, ...]] = {}
    for word, kind in pack.mapping("content_labels", "form_field_kinds").items():
        kind_words[kind] = (*kind_words.get(kind, ()), word)
    labels = ContentLabels(
        pack=pack,
        fields=fields,
        groups=groups,
        group_prefix=prefix,
        image_marker=re.compile(_label_line(marker_labels)),
        generated=re.compile(
            "|".join([re.escape(prefix + groups.get("summary", "summary")), _label_line([*marker_labels, label("search_keywords")])])
        ),
        ocr_section=re.compile(rf"^\s*{re.escape(ocr)}\s*{_COLON}.*?(?=^\s*{re.escape(prefix.strip())}|\Z)", re.M | re.S),
        screen_value_field=re.compile(rf"^{re.escape(label('visible_values'))}{_COLON}\s*(.+)$", re.M),
        screen_name_field=re.compile(rf"^{re.escape(label('visible_screen_names'))}{_COLON}\s*(.+)$", re.M),
        table_row=re.compile(rf"^\s*-\s*{row_number}\s*{_COLON}.*$", re.M),
        table_rows_heading=re.compile(rf"^\s*{re.escape(label('table_rows'))}\s*{_COLON}\s*$", re.M),
        index_line=re.compile(r"\s*" + _label_line([label("menu_route"), label("search_keywords"), label("main_topic")])),
        screen_example=re.compile("(?:" + "|".join(cues) + r")(?:(?!\s/\s)[^。\n])*[。]?" if cues else "(?!)"),
        form_field_kind_words=kind_words,
    )
    _by_hash[pack.hash] = labels
    return labels
