"""帳票の値の選択の記号の読み方（#2526）。

帳票は選択肢の前に記号を付け、選んだものを塗りつぶしの記号にする（「■A □B」「☑A ☐B」「✓A ☐B」
「●A ○B」「[x] A [ ] B」）。記号は汎用の集合で判断し、業務の語は使わない。

値を選ばれた選択肢にするのは、記号の付いた選択肢が 2 つ以上あり、選んだ記号と選んでいない記号が
両方あるときだけ（記号 1 つの見出し「■概要」、選んだものの無い「□A □B」、記号だけの「✓」は
変えない）。最初の記号の前の文は前置きとして残す（「区分 ■A □B」→「区分 A」）。選択肢が複数
選ばれていれば「、」でつなぐ。
"""

from __future__ import annotations

import re

# 選んだ記号と、選んでいない記号。
SELECTED_MARKS = "■☑☒✓✔●◉◼▣"
UNSELECTED_MARKS = "□☐○◯◻⬜"
_MARK = re.compile(r"(\[[xX✓✔]\]|\[\s*\]|[" + re.escape(SELECTED_MARKS + UNSELECTED_MARKS) + r"])")
_SELECTED_BRACKET = re.compile(r"^\[[xX✓✔]\]$")
# 選択肢の前後の区切り（「■A／□B」「■A・□B」）。
_SEPARATORS = " \t\u3000/／・、,，|｜"


def _selected(mark: str) -> bool:
    return mark in SELECTED_MARKS or bool(_SELECTED_BRACKET.match(mark))


def is_choice_mark(text: str) -> bool:
    """セルの文字が選択の記号だけか（記号と選択肢を別のセルに置いた帳票）。"""
    return bool(_MARK.fullmatch(text.strip()))


def starts_with_choice_mark(text: str) -> bool:
    """セルの文字が選択の記号で始まるか（「☑ A」のチェックボックス・選択肢ごとのセル）。"""
    return bool(_MARK.match(text.strip()))


def read_choice(value: str) -> str | None:
    """選択の記号の付いた値を、選ばれた選択肢にする（記号の選択肢でなければ None）。"""
    parts = _MARK.split(value)
    if len(parts) < 5:  # 前置き + (記号, 選択肢) × 2 以上
        return None
    prefix = parts[0].strip(_SEPARATORS)
    options: list[tuple[bool, str]] = []
    for index in range(1, len(parts), 2):
        text = parts[index + 1].strip(_SEPARATORS)
        if not text:
            return None
        options.append((_selected(parts[index]), text))
    chosen = [text for selected, text in options if selected]
    if not chosen or len(chosen) == len(options):
        return None
    joined = "、".join(chosen)
    return f"{prefix} {joined}" if prefix else joined
