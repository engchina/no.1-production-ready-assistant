"""注記の断定を落とさずに Markdown の体裁だけをそろえる。"""

from pr_backend_core.markdown import is_blank_text, normalize_heading_markup


def test_note_remains_part_of_the_heading():
    assert normalize_heading_markup("**結果**（上限は 7 日）") == "**結果（上限は 7 日）**"
    assert normalize_heading_markup("**結果** 説明です。") == "**結果** 説明です。"


def test_format_characters_are_only_blank_when_there_is_no_content():
    assert is_blank_text(" \u200b\ufeff\t")
    assert not is_blank_text("値\u200bは 7 です。")
