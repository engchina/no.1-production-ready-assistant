"""回答文と引用本文に対する決定的な判定。LLM を呼ばず、入力を変更しない。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any

from rag_parser_core.language import LanguagePack, per_pack
from rag_parser_core.language.text import alternation, numbered, range_class, text_patterns

from rag_engine.generation.answer_passage_vocabulary import alt as _v
from rag_engine.generation.check_cues import check_cues
from rag_engine.parsing.structural_text import is_table_rule as is_table_rule
from rag_engine.parsing.vision_prompt_rules import is_vision_enumeration_line
from rag_engine.retrieval.task_contract import generic_target_words

_QUOTE_ONLY_LINE = re.compile(r"\s*(?:・|[0-9]+[.)]\s*)?「.*」\s*")
# 中の「。」で文を分けない括弧（開きと閉じ。#1455）。
_BRACKET_PAIRS = {"「": "」", "『": "』", "（": "）", "(": ")", "【": "】"}
_CLOSING_BRACKETS = frozenset(_BRACKET_PAIRS.values())
# 前の文に付ける断片（太字・強調の印と閉じ括弧・句点だけ。#1455）。
_MARKS_ONLY = re.compile(r"[\s*_`」』）)】。]*")


def sentence_ranges(line: str, *, ends: str = "。") -> list[tuple[int, int]]:
    """1 行を文に分けた範囲（開始・終了の位置。前後の空白を含む）。

    「。」で文を分けるが、開いた括弧（「」『』（）()【】）の中の「。」では分けない（括弧の深さを数える。
    #1455）。閉じない括弧は行の終わりまで続くとみなし、行の終わりで切る。開いていない閉じ括弧は数えない。
    文の区切りの「。」の直後の閉じ括弧は前の文に含め、太字の印（`**`）・閉じ括弧だけの断片は前の文に付ける。
    Agent の ``answer_passages.sentence_ranges`` と同じ規則（両方のテストが
    platform/contracts/answer-passages/sentence-splits.json の同じ事例で確かめる）。``ends`` は文の終わりの
    文字（既定は回答の段落の「。」。標準回答の単位は「！」「？」でも分ける。#3575）。
    """
    ranges: list[tuple[int, int]] = []
    expected: list[str] = []
    start = index = 0
    while index < len(line):
        char = line[index]
        if char in _BRACKET_PAIRS:
            expected.append(_BRACKET_PAIRS[char])
        elif char in _CLOSING_BRACKETS and char in expected:
            # 対応する開きの括弧まで閉じる（その間の閉じていない括弧も閉じたとみなす）。
            while expected.pop() != char:
                pass
        elif char in ends and not expected:
            end = index + 1
            # 続く閉じ括弧と文の終わりの文字（「！？」「。。」）も前の文に含める。
            while end < len(line) and (line[end] in _CLOSING_BRACKETS or line[end] in ends):
                end += 1
            ranges.append((start, end))
            start = index = end
            continue
        index += 1
    if start < len(line):
        ranges.append((start, len(line)))
    merged: list[tuple[int, int]] = []
    for begin, end in ranges:
        if merged and _MARKS_ONLY.fullmatch(line[begin:end]):
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((begin, end))
    return merged


_PASSAGE_CHARS = 600


def answer_passage_ranges(text: str) -> list[tuple[str, int, int]]:
    """``answer_passages`` の段落の ID と、``text`` の中の位置（開始・終了。``text[start:end]`` が段落の原文）。

    MCP の ``rag_validate_answer`` は段落の判定にこの位置を付けて返し、呼び出し側は段落の分け方を写さずに
    位置で段落を特定する（#3240）。
    """
    return text_unit_ranges(text, prefix="A", whole_line=_QUOTE_ONLY_LINE)


# 1 行を 1 つの単位にする表の行（Markdown の表は「|」で始まる。セルの中の句点で行を切らない）。
_TABLE_ROW = re.compile(r"\s*\|.*")


def standard_answer_unit_ranges(text: str) -> list[tuple[str, int, int]]:
    """標準回答の単位（品質評価の比較範囲の固定でモデルが ID で指す）の ID（``S1``…）と位置（#3575）。

    回答の段落（``answer_passage_ranges``）と同じ分け方（行 → 括弧の外の文の終わり。空行は除く）に、
    「！」「？」でも文を分け、表の行と「…」だけの行は 1 行を 1 つの単位にする。箇条書きの項目は行なので
    前後の項目と混ざらない。
    """
    return text_unit_ranges(text, prefix="S", ends="。！？", whole_line=_STANDARD_WHOLE_LINE)


_STANDARD_WHOLE_LINE = re.compile(f"(?:{_QUOTE_ONLY_LINE.pattern})|(?:{_TABLE_ROW.pattern})")


def text_unit_ranges(text: str, *, prefix: str, ends: str = "。", whole_line: re.Pattern[str]) -> list[tuple[str, int, int]]:
    """本文を行・文に分けた単位の ID（``prefix`` と 1 始まりの番号）と位置（``text[start:end]`` が単位の原文）。

    行ごとに、``whole_line`` に合う行は 1 つの単位、それ以外は ``sentence_ranges`` で文に分ける。前後の空白を
    除き、空の行・断片は単位にしない。長い単位は ``_PASSAGE_CHARS`` 字ごとの連続した範囲に分ける。
    """
    result: list[tuple[str, int, int]] = []
    offset = 0
    for line in text.split("\n"):
        ranges = [(0, len(line))] if whole_line.fullmatch(line) else sentence_ranges(line, ends=ends)
        for begin, end in ranges:
            piece = line[begin:end]
            value = piece.strip()
            if not value:
                continue
            lead = begin + len(piece) - len(piece.lstrip())
            for start in range(0, len(value), _PASSAGE_CHARS):
                stop = min(start + _PASSAGE_CHARS, len(value))
                result.append((f"{prefix}{len(result) + 1}", offset + lead + start, offset + lead + stop))
        offset += len(line) + 1
    return result


def answer_passages(text: str) -> list[dict[str, str]]:
    """改写せず文・改行で区切る。長文も600文字の連続範囲として保持する。

    句点直後の閉じ括弧は同じ文に含める。括弧の中の「。」では切らない（``sentence_ranges``。#1455）。
    括弧の途中で切ると、検証で前の半分を外したときに閉じ括弧の残る断片が本文に残る。行全体が1つの
    「…」の引用（原文のみ提示の行。表の引用は複数の文を含む）は、引用の途中で切らずに1段落として扱う。
    段落の位置は ``answer_passage_ranges``。
    """
    return [{"id": passage_id, "text": text[start:end]} for passage_id, start, end in answer_passage_ranges(text)]


# 見出しの記法（Markdown の見出し・【…】・太字だけの行）と、その語。
_MARKUP_HEADING = re.compile(r"\s*(?:#{1,6}\s+(?P<md>[^。]{1,35})|【(?P<bracket>[^】]{1,25})】|\*\*(?P<bold>[^*。]{1,30})\*\*[:：]?)\s*")


def is_value_heading(text: str) -> bool:
    """見出しの記法だが、語に数量（「最長 90 日」）を含む行か（#2625）。

    数量は主張の値なので、見出しの記法に書き換えても監査から外さない（見出しの名詞句の規則 ``heading_phrase``
    と同じ。年度・章節の番号は数量に数えない）。
    """
    match = _MARKUP_HEADING.fullmatch(text)
    if not match:
        return False
    title = match.group("md") or match.group("bracket") or match.group("bold") or ""
    return bool(_HEADING_QUANTITY.search(title))


def is_heading(text: str) -> bool:
    """短い操作見出しと見出し記法を識別する。普通の文を長さだけで除外しない。

    見出しの記法でも、語に数量を含む行は主張（``is_value_heading``。#2625）。
    """
    if is_value_heading(text):
        return False
    markup = _MARKUP_HEADING.fullmatch(text)
    if markup:
        title = markup.group("md") or markup.group("bracket") or markup.group("bold") or ""
        if _SENTENCE_END.search(title) or is_operation_instruction(title) or "は" in _HEADING_QUOTED.sub(" ", title):
            return is_lead_in(text)

    # 確定までの短い節名を本文と誤認すると、未監査の見出しだけで全手順が落ちる。
    # 状態条件（登録済みの場合等）や操作を含む文はこの限定規則に含めない。
    operation_word = r"(?:操作|入力|登録|追加|削除|変更|保存|確定|完了|実行)"
    if re.fullmatch(rf"{operation_word}(?:(?:を|の)?{operation_word}){{0,2}}(?:する|までの|の)?手順", text.strip()):
        return True
    if re.fullmatch(r"(?:確認|変更|操作|設定)(?:手順|方法)|結果の見方|未確定の点|確認が必要な点|注意事項", text.strip()):
        return True
    if re.fullmatch(
        r"\s*(?:#{1,6}\s+[^。]{1,35}|【[^】]{1,25}】|\*\*[^*。]{1,30}\*\*[:：]?|[^。\n:：]{0,28}(?:内容|点|事項|操作|手順|条件|規則|結果|確認)(?:[（(][^）)\n]{1,20}[）)])?[:：]|(?:操作)?手順|確認事項|注意事項|関連操作|未確定事項)\s*",
        text,
    ):
        return True
    # 箇条書きでも、既知の名詞ラベルとコロンだけなら内容を持たない見出し。
    if text.strip().endswith((":", "：")):
        found = _heading_value(text)
        if found and _HEADING_END.search(found[0]):
            return True
    return heading_phrase(text) is not None or is_lead_in(text)


# ---- 見出しの名詞句と導入の文（#1469）。Agent の ``answer_passages`` と同じ規則で、両方のテストが
# platform/contracts/answer-passages/headings.json の同じ事例で確かめる。
# 見出しの印（箇条書き・番号）。
_HEADING_LIST_MARK = re.compile(r"^(?:[-*+・•]\s*|[0-9０-９]{1,3}[.)．）]\s*|[（(][0-9０-９]{1,3}[）)]\s*)")
# 見出しの後ろの注記の括弧（「補足（資料の記載）」「2. 略号の意味（組織規程 第 2 章 部署と略号／1 頁）」）。
_HEADING_NOTE = re.compile(r"[（(](?P<note>[^（）()\n]{1,60})[）)]\s*$")
# 「…」の語（「略号「運」の意味」）。中の「は」「が」は言い切りの助詞に数えない。
_HEADING_QUOTED = re.compile(r"[「『][^「」『』\n]{1,40}[」』]")
# 数量（「30 分」「10 年」。主張の値）。年度（「2026年度」）・行・頁の番号は数えない。
_HEADING_QUANTITY = re.compile(r"[0-9０-９]+\s*(?:円|日|時間|分|秒|件|%|％|回|か月|ヶ月|カ月|年(?!度)|名|人|GB|MB)")


# 見出しの語に残してよい番号（章・節の番号と年度）。ほかの数字（値の「3 点」「SYS-103」）を含む行は主張のまま。
def _section_number() -> str:
    """章・節・条・項の番号（「第 3 章」）。前置き・数・単位は言語資源(#2181)。"""
    text = text_patterns()
    units = "|".join((text.chapter_units, text.section_units, text.article_units, text.paragraph_units))
    return numbered(text, f"(?:{units})")


def _heading_numbered_name() -> re.Pattern[str]:
    """見出しの語に残してよい番号（章・節の番号と年度）。"""
    return re.compile(rf"{_section_number()}|[0-9０-９]+\s*{text_patterns().fiscal_year_units}")


# 見出しの名詞句の終わりの語（項目・区分・資料の名前。値の名前「運行管理課長」「極秘」では終わらない）。
_HEADING_END = re.compile(rf"{_v('heading_end_words')}$")
# 番号（「1.」「（2）」）と箇条書きの印（「- 」「・」。強調の「**」は印に数えない）。番号つき・箇条書きの行は段落
# だけでは決めず、次の行がその下の内容のときだけ見出し（``list_heading_lines``。#1481 / #1482 / #1486）。番号・
# 箇条書きの行が続く一覧の項目は主張のまま。
_HEADING_NUMBER = re.compile(r"^(?:[0-9０-９]{1,3}[.)．）]|[（(][0-9０-９]{1,3}[）)])")
_HEADING_BULLET = re.compile(r"^(?:[-+・•]|\*(?=\s))")
# 「…の…」の名詞句（「重要度 B の復旧目標」「連携元のシステム」）。「ので」「のに」「のみ」は名詞をつなぐ「の」に数えない。
_HEADING_NO_PHRASE = re.compile(r"[^\sの]\s*の(?![でにみ])\s*[^\sの]")
# 番号つきの見出しの下の内容の行（箇条書き・「→」。字下げした行も、字下げした番号の行も下の内容）。箇条書きの
# 見出しの下の内容は、字下げした行と「→」の行だけ（同じ字下げの箇条書きの行は同じ一覧の次の項目。#1486）。
_SUB_CONTENT = re.compile(r"[-*+]\s|[・•→]")
_BULLET_SUB_CONTENT = re.compile(r"→")
# 値の名前（人・組織・役職）の終わり。番号つきの「1. 情報システム部の課長」は見出しではなく値の主張のまま。
_HEADING_VALUE_END = re.compile(rf"{_v('heading_value_end_words')}$")
# 番号・箇条書きの印の後が太字のラベルだけの行（「1. **実施権限**」「- **アカウント削除**：」。#1898）。終わりの語が
# 一覧に無く「…の…」でもない短い名詞でも、太字だけで書いた番号つき・箇条書きの行は見出しの形（次の行で決める）。
_HEADING_BOLD_LABEL = re.compile(r"\*\*[^*\n]{1,30}\*\*\s*[:：]?")
# 分岐・問いの形の見出しの終わり（この形だけ、中の「が」「を」を許す）。
_HEADING_CLAUSE_END = re.compile(r"(?:とき|場合|か)$")
# 見出しの中の、分岐の節（「承認者が不在のときの代理」の「が不在のとき」）。
_HEADING_CASE_CLAUSE = re.compile(r"[がを][^はがを、。]{1,15}?(?:とき|場合)")
# 導入の文の先頭の接続の語（「そのため、作業できる時間帯は次のとおりです。」。この後の「、」は主語の区切りに数えない。#1481）。
_LEAD_IN_CONNECTIVE = re.compile(rf"^{_v('lead_in_connectives')}[、，,]\s*")
# 以降の箇条書きを導くだけの導入の文（「根拠は次のとおりです。」「以下に示します。」）。
_LEAD_IN = re.compile(
    rf"(?P<subject>[^。！？!?\n]{{1,24}}?){_v('lead_in_subject_particles')}、?\s*{_v('lead_in_next_words')}の"
    rf"{_v('lead_in_forms')}{_v('copulas')}?[。．:：]?"
    rf"|(?:(?P<object>[^。！？!?\n]{{1,24}}?)を)?{_v('lead_in_next_words')}に{_v('lead_in_verbs')}ます[。．:：]?"
)
# 利用者の答え・質問への礼の定型の文（「ご回答ありがとうございます。」）。
_COURTESY = re.compile(rf"{_v('courtesy_objects')}(?:いただき)?、?ありがとうございます[。．!！]?")
# 回答の締めくくりの定型の文（#1833。「以上、資料に基づく回答です。」「以上です。」）。語の組み合わせを固定し、
# 事実の語（値・名前・条件）が入る余地を置かない（検証の抜け道にしない）。
_CLOSING = re.compile(
    rf"以上{_v('copulas')}[。．]?"
    rf"|(?:以上[、，,]?\s*(?:が)?\s*)?(?:{_v('closing_source_verbs')}{_v('closing_source_verb_forms')}\s*)?"
    rf"{_v('closing_sources')}(?:の(?:内容|記載))?に{_v('closing_based_on')}"
    rf"{_v('closing_kinds')}{_v('copulas')}[。．]?"
)
# 前の内容をまとめて指すだけの結びの文（#1879。「以上が、締め日を過ぎた場合の取扱いです。」）と結びの
# あいさつ（「ご参考になれば幸いです。」）。指す対象（topic）は質問の対象を名詞で言い直す語に限り、言い切りの
# 「は」・数量・操作・引用を含むもの（「以上が、締め日は 25 日という取扱いです。」）は主張のまま。Agent の
# `answer_passages` と同じ規則。
_CLOSING_SUMMARY = re.compile(
    r"以上(?:が|で)[、，,]?\s*(?P<topic>[^。！？!?\n「」『』]{0,40}?)"
    rf"{_v('closing_summary_nouns')}{_v('copulas')}[。．]?"
)
_CLOSING_WISH = re.compile(rf"(?:少しでも)?{_v('closing_wish_objects')}{_v('closing_wish_conditions')}幸いです[。．!！]?")


def _heading_note_ok(note: str) -> bool:
    """見出しの注記の括弧が、言い切り・操作・数量を含まない補足か（「（承認者は人事部長です）」は主張）。"""
    outside = _HEADING_QUOTED.sub(" ", note)
    return (
        "。" not in note
        and not _SENTENCE_END.search(note)
        and not is_operation_instruction(note)
        and not _HEADING_QUANTITY.search(note)
        and "は" not in outside
    )


def heading_phrase(text: str) -> str | None:
    """主張でない見出しの名詞句なら、注記の括弧・番号・強調を除いた見出しの語（見出しでなければ None。#1469）。

    「補足（資料の記載）」「根拠」「回答に使った資料」「**重要度 C のシステムで障害が起きたとき**」のように、句点で
    終わらない短い名詞句で、項目・区分・資料の名前（``_HEADING_END``）か分岐・問いの形（「…とき」「…か」）で終わるもの。述語（です・ます・する…）・
    操作・数量・言い切りの「は」を含む行（「重要度は A です」「承認者は管理本部長」）は主張のまま。番号つき・箇条書きの
    行（「1. 人事評価システムの重要度」「- 組織規程（ご担当部署の承認者）」）は段落だけでは見出しにせず、回答の中の
    次の行で決める（番号・箇条書きの行が続く一覧の項目は主張。``list_heading_lines``。#1481 / #1482 / #1486）。
    """
    found = _heading_value(text)
    if not found or _is_list_item(text):
        return None
    return found[0] if _HEADING_END.search(found[0]) else None


def _is_list_item(text: str) -> bool:
    """番号つきか箇条書きの行か（「1. …」「（2）…」「- …」「・…」。#1482 / #1486）。"""
    value = text.strip()
    return bool(_HEADING_NUMBER.match(value) or _HEADING_BULLET.match(value))


def _list_heading_phrase(text: str) -> str | None:
    """番号つき・箇条書きの見出しの語（「1. 人事評価システムの重要度」「- 重要度 B の復旧目標」。#1481 / #1482 / #1486）。

    終わりの語が一覧（``_HEADING_END``）にあるか、一覧に無くても「…の…」の名詞句か、印の後が太字のラベルだけの行
    （「1. **実施権限**」「3. **アカウント削除**」。#1898）のもの。人・組織・役職の名前で終わる「…の…」・太字の行
    （「1. 情報システム部の課長」「1. **人事部長**」）と、述語・操作・数量・言い切りの「は」を含む行（「1. **ID 管理
    システムの重要度は A**」）は値の主張のまま。見出しかどうかは次の行で決める（``list_heading_lines``）。
    """
    found = _heading_value(text)
    if not found or not _is_list_item(text):
        return None
    value, outside = found
    if _HEADING_END.search(value):
        return value
    if _HEADING_VALUE_END.search(value):
        return None
    bold = _HEADING_BOLD_LABEL.fullmatch(_HEADING_LIST_MARK.sub("", text.strip(), count=1).strip())
    return value if bold or _HEADING_NO_PHRASE.search(outside) else None


def list_heading_lines(text: str) -> set[str]:
    """回答の中の、番号つき・箇条書きの見出しの行（前後の空白を除いた行。#1481 / #1482 / #1486）。

    番号つき・箇条書きの見出しの語（``_list_heading_phrase``）の行で、次の（空でない）行がその下の内容のもの。番号つきの
    行の下の内容は、字下げした行か、箇条書き・「→」の行。箇条書きの行の下の内容は、字下げした行（下位の箇条書き）か
    「→」の行で、同じ字下げの箇条書きの行は同じ一覧の次の項目。番号・箇条書きの行が続く一覧（「申請書に書く 4 項目」の
    「1. 変更の内容と理由」〜「4. 作業の日時」、「ご確認いただきたい点」の「- 組織規程（ご担当部署の承認者）」）の項目と、
    下に内容の無い行は、終わりの語が一覧にあっても主張のまま（段落だけで決めると、一覧の項目を見出しとして監査から
    外す。#1482 / #1486）。Agent の ``answer_passages.list_heading_lines`` と同じ規則。
    """
    lines = [line for line in text.split("\n") if line.strip()]
    found = set()
    for line, following in zip(lines, lines[1:], strict=False):
        indent = len(line) - len(line.lstrip())
        sub_content = _BULLET_SUB_CONTENT if _HEADING_BULLET.match(line.strip()) else _SUB_CONTENT
        below = len(following) - len(following.lstrip()) > indent or bool(sub_content.match(following.strip()))
        if below and _list_heading_phrase(line):
            found.add(line.strip())
    return found


def _heading_value(text: str) -> tuple[str, str] | None:
    """見出しの名詞句の共通の確かめ（注記の括弧・番号・強調を除いた語と、「…」の語を除いた残り。#1469）。"""
    if is_citation_line(text):
        # 文書名と場所だけの行（「1. **システム変更手順書**（第 2章 申請）」）は出典の行（#1370）。
        return None
    value = _HEADING_LIST_MARK.sub("", text.strip(), count=1)
    value = _EMPHASIS.sub("", value).strip().rstrip(":：").strip()
    while match := _HEADING_NOTE.search(value):
        if not _heading_note_ok(match.group("note")):
            return None
        value = value[: match.start()].strip()
    if not value or len(value) > 30 or re.search(r"[。．！？!?、,，:：]", value):
        return None
    if _SENTENCE_END.search(value) or is_operation_instruction(value) or re.search(r"[0-9０-９]", _heading_numbered_name().sub(" ", value)):
        return None
    outside = _HEADING_QUOTED.sub(" ", value)
    if "は" in outside:
        return None
    # 「が」「を」は、分岐・問いの形の見出しか、「…が不在のとき」の節の中だけに許す。
    if not _HEADING_CLAUSE_END.search(value) and re.search(r"[がを]", _HEADING_CASE_CLAUSE.sub(" ", outside)):
        return None
    return value, outside


# 表題を示すだけで、表の値を述べない導入行。
_TABLE_LEAD_IN = re.compile(rf"{_v('table_lead_in_labels')}[「『][^「」『』。\n]{{1,80}}[」』](?:では|は)[、，,:：]?")


def is_lead_in(text: str) -> bool:
    """以降の箇条書きを導くだけの導入の文（「根拠の積み上げは次のとおりです。」）と礼・締めくくりの定型の文か（#1469・#1833）。

    先頭の接続の語の後の「、」（「そのため、作業できる時間帯は次のとおりです。」）は許す（#1481）。主語・目的語に
    数量・操作・言い切りの「は」・「、」を含む文（「保管期間は 10 年で、内訳は次のとおりです。」）は主張のまま。
    """
    value = _EMPHASIS.sub("", _HEADING_LIST_MARK.sub("", text.strip(), count=1)).strip()
    if _TABLE_LEAD_IN.fullmatch(value) or _COURTESY.fullmatch(value) or _CLOSING.fullmatch(value) or _CLOSING_WISH.fullmatch(value):
        return True
    summary = _CLOSING_SUMMARY.fullmatch(value)
    if summary:
        topic = summary.group("topic")
        return not (_HEADING_QUANTITY.search(topic) or is_operation_instruction(topic) or "は" in topic)
    match = _LEAD_IN.fullmatch(_LEAD_IN_CONNECTIVE.sub("", value, count=1))
    if not match:
        return False
    head = match.group("subject") or match.group("object") or ""
    return not (_HEADING_QUANTITY.search(head) or is_operation_instruction(head) or "は" in head or "、" in head)


_LIST_MARK = re.compile(r"^(?:[-*+・•]\s*|[0-9０-９]{1,3}[.)．）]\s*)")
_CITATION_LABEL = re.compile(
    rf"[（(【〔\[]?\s*{_v('citation_labels')}\s*[:：]\s*(?P<rest>.+?)\s*[）)】〕\]]?",
    re.I,
)
# 出典の場所のラベル（#1833。「- 場所：第 2 章「締め日と支払日」」「- 該当箇所：p.3」）。「場所」「位置」は業務の値にも
# 使う語（「場所：本社 3 階の会議室」は主張）なので、値が文書の場所だけのとき（``_is_location_value``）に限って出典にする。
_LOCATION_LABEL = re.compile(rf"[（(【〔\[]?\s*{_v('location_labels')}\s*[:：]\s*(?P<rest>.+?)\s*[）)】〕\]]?")
# Markdown の強調（「**根拠**：…」「*Locator*:」のラベル。出典の判定だけで外す。#1370 で斜体の `*` も外す）。
_EMPHASIS = re.compile(r"\*{1,2}|__|`")
# 根拠の ID の括弧（【証拠 ID: 03ac…:1】・〔証拠 ID: 07e8…:5〕・【証拠ID cb0a…:2】・【証拠1】・【evidence_id: …】。
# 〔〕［］[] は #1370）。
_EVIDENCE_REF = re.compile(
    r"[【〔［\[]\s*(?:証拠|根拠|出典|evidence|source)(?:\s*_?(?:ID|id))?\s*[:：]?[\s0-9A-Za-z:_\-,，、…①-⑳]{0,200}[】〕］\]]", re.I
)
_TITLE_QUOTE = re.compile(r"[「『]([^「」『』\n]{1,80})[」』]")
_DOCUMENT_TITLE = re.compile(r"手順書|マニュアル|ガイド|規程|規定|資料|文書|メモ|第\s*[0-9０-９]+\s*版")


def _section_ref() -> re.Pattern[str]:
    return re.compile(rf"(?:{_section_number()}|[0-9０-９]+(?:\.[0-9０-９]+)*[.．]\s*)[^。\n]{{0,30}}")


_REFERENCE_FILLER = re.compile(r"[|\s→>＞\-–—‑―、,，:：/／]+|同上|(?:ページ|頁|p\.|page)\s*[0-9０-９]+", re.I)
_FILE_NAME = re.compile(r"[^\s「」『』（）()【】\[\]、,，|]+\.(?:pdf|docx?|xlsx?|pptx?|md|txt|html?|csv)\b", re.I)
_TABLE_ROW = re.compile(r"\|.*\|")
_DOCUMENT_WORD = re.compile(
    r"\.(?:pdf|docx?|xlsx?|pptx?|md|txt|html?|csv)\b|\bpage\b|\bp\.\s*\d|\bpp\.|ページ|頁|\bsection\b|\bsheet\b|シート",
    re.I,
)


class _DocumentMark:
    """文書の場所の印（拡張子・ページ・シート・章節の番号）。章節の番号は言語資源(#2181)。"""

    def search(self, value: str) -> re.Match[str] | None:
        return _DOCUMENT_WORD.search(value) or re.search(_section_number(), value)


_DOCUMENT_MARK = _DocumentMark()
_BRACKETED = re.compile(r"(?:[【\[（(][^】\]）)\n]{1,200}[】\]）)][\s、,，]*)+")
_BRACKET_PART = re.compile(r"[【\[（(]([^】\]）)\n]{1,200})[】\]）)]")
_LINK_LINE = re.compile(r"(?:\[[^\]\n]{1,200}\]\([^)\s]{1,500}\)[\s、,，]*)+|https?://\S+")
_SENTENCE_END = re.compile(r"(?:ます|ません|です|でした|ました|ください|ない|無い|ある|する|できる|れる|った|いた|した|だ)[。．.!！]?$")
# 後ろの注記の括弧（「（はい／いいえ）」「【clarification: target】」）は許す (#1317)。
_QUESTION = re.compile(r".*[？?]\s*(?:[（(【][^）)】\n]{1,40}[）)】]\s*)*。?")
_INFO_REQUEST = re.compile(
    r".*(?:教えてください|お知らせください|お教えください|お聞かせください|ご回答ください|お答えください|確認させてください)[。．!！]?"
)
_BUNDLED_CLAIM = re.compile(r"ますが|ですが|ましたが|ませんが|ものの|けれど|けど|ただし")


def table_header_lines(text: str) -> set[str]:
    """Markdown の表の見出しの行（区切りの行の直前の、短い語だけのセルの行）。表の本文の行は主張のまま (#1317)。"""

    def header_cell(cell: str) -> bool:
        value = _EMPHASIS.sub("", cell).strip()
        return len(value) <= 20 and "。" not in value and not _SENTENCE_END.search(value) and not is_operation_instruction(value)

    lines = [line.strip() for line in text.split("\n")]
    return {
        line
        for line, following in zip(lines, lines[1:], strict=False)
        if _TABLE_ROW.fullmatch(line)
        and "|" in following
        and is_table_rule(following)
        and any(_EMPHASIS.sub("", cell).strip() for cell in line.strip("|").split("|"))
        and all(header_cell(cell) for cell in line.strip("|").split("|"))
    }


def _is_reference_only(value: str) -> bool:
    """根拠の ID・文書名・頁の括弧・文の無い「…」の題（と節の参照）だけの段落か (#1317)。

    「| 「1. 前提」 （ページ1）【証拠 ID: 03ac…:1】 |」や「- 「サンプル業務ポータル 運用手順書 第3版」 6. アカウントの削除」。
    文書を示す印（根拠の ID・文書名・頁の括弧・文書の題）が無ければ出典にしない。
    """
    if is_operation_instruction(value) or _SENTENCE_END.search(value.strip("|").strip()):
        return False
    found = False

    def drop_document(match: re.Match[str]) -> str:
        nonlocal found
        if _DOCUMENT_MARK.search(match.group(1)):
            found = True
            return " "
        return match.group(0)

    def drop_title(match: re.Match[str]) -> str:
        nonlocal found
        title = match.group(1)
        if "。" in title or _SENTENCE_END.search(title) or is_operation_instruction(title):
            return match.group(0)
        found = found or bool(_DOCUMENT_TITLE.search(title))
        return " "

    rest, count = _EVIDENCE_REF.subn(" ", value)
    found = count > 0
    rest = _TITLE_QUOTE.sub(drop_title, _BRACKET_PART.sub(drop_document, rest))
    rest, count = _FILE_NAME.subn(" ", rest)
    found = found or count > 0
    rest = _REFERENCE_FILLER.sub(" ", rest).strip()
    return found and (not rest or bool(_section_ref().fullmatch(rest) and not _SENTENCE_END.search(rest)))


# 行末の場所の括弧（「（第 2章 申請）」「（p.12）」）。
_LOCATION_TAIL = re.compile(r"[（(〔\[]([^（()）〔〕\[\]\n]{1,80})[）)〕\]]\s*$")
# 文書名に無い、言い切りの助詞（「承認者は課長（第 3 章）」「申請先は規程が定める承認者」は主張）。
_STATEMENT_PARTICLE = re.compile(r"[はがを]")
_ANY_BRACKET = re.compile(r"[（(【〔\[「『][^（()）【】〔〕\[\]「」『』\n]*[）)】〕\]」』]")


def _is_document_location(value: str) -> bool:
    """文書名と場所の括弧だけの段落か（#1370。「システム変更手順書（第 2章 申請）」「運用マニュアル（p.12）」）。

    行末の場所の括弧（章・節・頁・シートなどの印を含むもの）を外した残りが、文書を示す語（手順書・規程・
    ファイル名など）を含む短い名前で、述語・操作・言い切りの助詞（は・が・を）を含まないときだけ出典にする。
    場所の後に本文が続く行（「…（第 2章）では、…」）は主張のまま。
    """
    rest = value.strip().rstrip("。．").strip()
    found = False
    while match := _LOCATION_TAIL.search(rest):
        location = match.group(1)
        if not _DOCUMENT_MARK.search(location) or _SENTENCE_END.search(location) or is_operation_instruction(location):
            break
        rest, found = rest[: match.start()].strip(), True
    if not found or not rest or len(rest) > 80 or "。" in rest:
        return False
    if _SENTENCE_END.search(rest) or is_operation_instruction(rest):
        return False
    outside = rest
    while (stripped := _ANY_BRACKET.sub(" ", outside)) != outside:
        outside = stripped
    if _STATEMENT_PARTICLE.search(outside):
        return False
    return bool(_DOCUMENT_TITLE.search(rest) or _FILE_NAME.search(rest))


def _is_location_value(value: str) -> bool:
    """場所のラベルの値が文書の場所だけか（#1833。「第 2 章「締め日と支払日」」「経費精算マニュアル.md 第 2 章」「p.3」）。

    章・節・頁・シート・ファイル名の印を含み、節の題（文でない「…」）・場所の括弧・ファイル名・頁を外した残りが、
    空か、節の参照か、文書の名前（手順書・マニュアル・規程など）だけのもの。数量・言い切りの助詞（は・が・を）・述語・
    操作を含む値（「本社 3 階の会議室（第 2 章）」「第 2 章 申請は 3 日以内」）は主張のまま。
    """
    if not _DOCUMENT_MARK.search(value) or _SENTENCE_END.search(value) or is_operation_instruction(value):
        return False

    def drop_title(match: re.Match[str]) -> str:
        title = match.group(1)
        return match.group(0) if "。" in title or _SENTENCE_END.search(title) or is_operation_instruction(title) else " "

    rest = _BRACKET_PART.sub(lambda match: " " if _DOCUMENT_MARK.search(match.group(1)) else match.group(0), value)
    rest = _REFERENCE_FILLER.sub(" ", _FILE_NAME.sub(" ", _TITLE_QUOTE.sub(drop_title, rest))).strip()
    if not rest:
        return True
    if _STATEMENT_PARTICLE.search(rest) or _HEADING_QUANTITY.search(rest) or "「" in rest or "『" in rest:
        return False
    return bool(_section_ref().fullmatch(rest) or (len(rest) <= 60 and _DOCUMENT_TITLE.search(rest)))


def _trailing_sections() -> re.Pattern[str]:
    """行末に続く章・節・条・項の番号（「第12条第5項」「第 8 章 第105条」「第6条の2」）。"""
    return re.compile(rf"(?:\s*{_section_number()}(?:\s*の\s*[0-9０-９]+)?)+$")


# 文書名に数えない文字（数量・値の番号・句読点・引用）。
_SECTION_NAME_EXCLUDED = re.compile(r"[0-9０-９。．、,，:：「」『』]")


def _is_document_section(value: str) -> bool:
    """文書名と条・項・章などの番号（と見出しの括弧）だけの段落か（#2225）。

    「サンプル法 第12条第5項（届出）」「第 8 章 第105条第1項（届出等）」。行末の括弧（条の見出し）を外した
    残りが章・節・条・項の番号で終わり、その前の名前が短く、数字・句読点・言い切りの助詞（は・が・を）・述語・
    操作を含まないときだけ出典にする。見出しの括弧に文・数量があるもの（「第12条（14 日以内）」）、番号の後に
    本文が続くもの（「第12条により…」）は主張のまま。Agent の ``answer_passages._is_document_section`` と同じ規則。
    """
    rest = value.strip().rstrip("。．").strip()
    if match := _LOCATION_TAIL.search(rest):
        caption = match.group(1)
        if "。" in caption or _SENTENCE_END.search(caption) or is_operation_instruction(caption):
            return False
        if _HEADING_QUANTITY.search(caption):
            return False
        rest = rest[: match.start()].strip()
    sections = _trailing_sections().search(rest)
    if not sections:
        return False
    name = rest[: sections.start()].strip()
    if len(name) > 40 or _SECTION_NAME_EXCLUDED.search(name):
        return False
    return not name or not (_STATEMENT_PARTICLE.search(name) or _SENTENCE_END.search(name) or is_operation_instruction(name))


def is_citation_line(text: str) -> bool:
    """出典の行（出典・定位子・場所のラベル・文書名・頁・節の括弧・リンク・根拠の ID・文書名と場所だけの段落）か。

    主張を含む文は出典にしない (#1306 / #1317 / #1370 / #1833)。Agent の ``answer_passages.is_citation`` と同じ規則で、
    両方のテストが platform/contracts/answer-passages/citation-lines.json の同じ事例で確かめる。
    """
    value = _EMPHASIS.sub("", _LIST_MARK.sub("", text.strip(), count=1)).strip()
    if not value or "。" in value.rstrip("。"):
        return False
    label = _CITATION_LABEL.fullmatch(value)
    if label:
        return not _SENTENCE_END.search(label.group("rest")) and not is_operation_instruction(label.group("rest"))
    # 場所のラベルは値が文書の場所のときだけ出典にし、そうでなければ他の出典の形（文書名と場所・題と頁など）で決める。
    location = _LOCATION_LABEL.fullmatch(value)
    if location and _is_location_value(location.group("rest")):
        return True
    titles = _TITLE_QUOTE.findall(value)
    if (
        titles
        and not any(_STATEMENT_PARTICLE.search(title) or _HEADING_QUANTITY.search(title) for title in titles)
        and _is_location_value(value)
    ):
        return True
    if _LINK_LINE.fullmatch(value):
        return True
    if _BRACKETED.fullmatch(value) and all(_DOCUMENT_MARK.search(part) for part in _BRACKET_PART.findall(value)):
        return True
    return _is_reference_only(value) or _is_document_location(value) or _is_document_section(value)


def is_question_to_user(text: str) -> bool:
    """利用者への確認の質問・情報を求める依頼の文か。主張を抱き合わせた文（「〜できますが、〜か？」）は除く (#1306)。"""
    value = _LIST_MARK.sub("", text.strip(), count=1).strip()
    if not (_QUESTION.fullmatch(value) or _INFO_REQUEST.fullmatch(value)):
        return False
    return not _BUNDLED_CLAIM.search(value)


# 業務ガイドが見つからなかったことだけを述べる文 (#1452。「補足：この内容に当たる公開の業務ガイドは見つかりませんでした
# （`rag_lookup_guides` の結果は0件）。」)。業務ガイドの検索の結果は検証の根拠に無いので、主張として監査すると必ず外れる。
# 前置きは業務ガイドを修飾するだけの語（は・が・を・数字・引用を含まない）に限る（「部門長の承認が要る業務ガイドはありません」は主張）。
# Agent の ``answer_passages.is_guide_not_found`` と同じ規則で、両方のテストが platform/contracts/answer-passages/process-notes.json
# の同じ事例で確かめる。
_GUIDE_NOT_FOUND = re.compile(
    rf"(?:[^。:：\n]{{1,8}}[:：]\s*)?(?:なお|また|ただし)?[、,]?\s*(?P<lead>[^。\n]{{0,30}}?){_v('guide_words')}"
    rf"(?:は|が|も)[、,]?(?:まだ)?{_v('guide_not_found_endings')}(?:でした)?[。．]?"
)
_GUIDE_NOT_FOUND_LEAD = re.compile(r"[はがを0-9０-９「」『』]")
# 文末の、ツールの名前を挙げた作業の経過の括弧（「（rag_lookup_guides の結果は0件）」）。
_TOOL_NOTE = re.compile(r"\s*[（(][^（()）\n]*\brag_[a-z_]+[^（()）\n]*[）)](?=\s*[。．]?\s*$)")


def is_guide_not_found(text: str) -> bool:
    """業務ガイドが見つからなかったことだけを述べる文か (#1452。作業の経過で、資料の主張ではない)。"""
    value = _TOOL_NOTE.sub("", _EMPHASIS.sub("", _LIST_MARK.sub("", text.strip(), count=1)).strip())
    match = _GUIDE_NOT_FOUND.fullmatch(value)
    if not match or _GUIDE_NOT_FOUND_LEAD.search(match.group("lead")):
        return False
    return not is_operation_instruction(value)


def is_non_claim_passage(text: str, *, table_headers: Collection[str] = (), heading_lines: Collection[str] = ()) -> bool:
    """回答の最終の検証で主張として監査しない段落（見出し・出典の行・表の区切りと見出しの行・利用者への質問・
    業務ガイドが見つからなかったことだけを述べる文）か (#1306 / #1317 / #1452)。

    表の見出しの行と、番号つき・箇条書きの見出しの行は次の行で決まるので、呼び出し側が回答全体の
    ``table_header_lines`` と ``list_heading_lines``（#1481 / #1482 / #1486）を渡す。
    """
    value = text.strip()
    return (
        is_heading(text)
        or is_citation_line(text)
        or is_table_rule(text)
        or value in table_headers
        or value in heading_lines
        or is_question_to_user(text)
        or is_guide_not_found(text)
    )


@dataclass(frozen=True)
class _OperationCues:
    """言語資源（operation_audit_cues。#2181 段 5）から組み立てた、操作の指示・操作の語の族・削除の対象の正規表現。"""

    instruction: re.Pattern[str]
    families: tuple[tuple[str, ...], ...]
    deletion: str
    quoted_target: re.Pattern[str]
    named_target: re.Pattern[str]


def _build_operation_cues(pack: LanguagePack) -> _OperationCues:
    def alt(key: str) -> str:
        return alternation(pack.words("operation_audit_cues", key))

    english = pack.words("operation_audit_cues", "english_instruction_words")
    deletion = alternation(pack.words("answer_check_cues", "deletion_words"))
    families: dict[str, list[str]] = {}
    for item in pack.words("operation_audit_cues", "operation_families"):
        word, _sep, family = item.partition("=")
        families.setdefault(family.strip(), []).append(word.strip())
    quotes = pack.words("symbols", "quote_brackets")
    opens = re.escape("".join(pair[0] for pair in quotes))
    closes = re.escape("".join(pair[1] for pair in quotes))
    particles = alternation(pack.words("question_cues", "target_particles"))
    manner = alt("deletion_manner_words")
    kana_kanji_latin = range_class(
        (*pack.words("search_terms", "kanji_chars"), *pack.words("search_terms", "katakana_chars"), "A-Z", "a-z", "0-9")
    )
    return _OperationCues(
        instruction=re.compile(
            rf"{alt('instruction_stems')}{alt('operation_instruction_endings')}|{alt('instruction_phrases')}"
            rf"|{alt('conditional_operation_verbs')}{alt('conditional_operation_endings')}"
            rf"|{alt('action_verbs')}{alt('action_endings')}"
            rf"|{alt('control_words')}.{{0,30}}{alt('control_actions')}"
            rf"|{deletion}{alt('deletion_endings')}" + (rf"|\b{alternation(english)}\b" if english else ""),
            re.I,
        ),
        families=tuple(tuple(words) for words in families.values()),
        deletion=deletion,
        quoted_target=re.compile(rf"[{opens}]([^{closes}]+)[{closes}]{particles}?{manner}?{deletion}"),
        named_target=re.compile(rf"({kana_kanji_latin}+){particles}{manner}?{deletion}"),
    )


_operation_cues = per_pack(_build_operation_cues)


def is_operation_instruction(text: str) -> bool:
    """具体的な操作指示を確認留保として無引用で通さないための補助判定（語は言語資源）。"""
    return bool(_operation_cues().instruction.search(text))


def missing_operation_terms(text: str, cited: list[dict[str, Any]]) -> list[str]:
    """変更・出力操作の語が引用本文に全くない場合を保守的に検出する。

    語の存在は操作の十分な証拠ではなく、適用性・否定・順序は独立監査に残す。
    一般助言という体裁でも実操作を追加させないため、原文にない操作だけを拒む。
    見出し・検索語は支持本文として使わない。
    """
    if not is_operation_instruction(text):
        return []
    body = "\n".join(line for span in cited for line in str(span.get("text", "")).splitlines() if not is_vision_enumeration_line(line))
    families = _operation_cues().families
    return [
        next(term for term in family if term in text)
        for family in families
        if any(term in text for term in family) and not any(term in body for term in family)
    ]


# ボタン名とみなす最短の字数。1 字（「各ボタン」「同ボタン」）は指示語・接頭語で、ボタンの名前ではないことが多い。
_MIN_BUTTON_NAME_CHARS = 2
# 名前に付いたファンクションキー（「F5実行ボタン」）。キーの書き方（「(F5)実行」）は原文と違い得るので、名前から除いて比べる。
_BUTTON_FUNCTION_KEY = re.compile(r"(?<![A-Za-z0-9])F(?:1[0-2]|[1-9])(?![0-9])")


def button_names_absent(text: str, cited_texts: Collection[str], question: str = "") -> list[str]:
    """説明が案内したボタン名（「実行ボタン」の「実行」）のうち、引いた根拠のどの本文にも名前が無いもの（#3822）。

    説明が引いた原文より細かい操作（「入力して実行ボタンを押す」）を、画面を開く手順だけの原文で裏付けた形を見つける。
    名前の文字列そのものが、引いたすべての根拠に無いことだけを見る。書き方の違い（「実行のボタン」「"実行"ボタン」）
    では判定せず、名前が原文にあれば、その操作を原文が支えるか（意味の支持）は判定しない。質問にある名前、総称語、
    1 字の名前は対象にしない。呼ぶ側は説明・根拠・質問を同じ文字の形（``check_view``）にそろえて渡す。
    """

    def compact(value: str) -> str:
        return _BUTTON_FUNCTION_KEY.sub("", re.sub(r"\s+", "", value))

    body, asked, generic = compact("\n".join(cited_texts)), compact(question), set(generic_target_words())
    names = (compact(name) for name in check_cues().button.findall(re.sub(r"\s+", "", text)))
    return [
        name
        for name in dict.fromkeys(names)
        if len(name) >= _MIN_BUTTON_NAME_CHARS and name not in generic and name not in body and name not in asked
    ]


# 総称語だけの削除対象。「データを削除」は資料の具体名（「履歴情報」「行」）と一致しないので対象にしない (#696)。
# 操作対象の検査（#700）と同じ語彙（task_contract.generic_target_words。言語資源）を使う。


def deletion_targets(question: str) -> list[str]:
    """削除・消去の直前に明示された対象だけを抽出し、未知の対象を推測しない。

    総称語（「データ」「情報」）だけの対象は返さない。検査の趣旨は、質問が具体名を挙げたときに別の対象の
    削除手順を当てないことにあり、総称語には具体名との照合が成り立たない。「〇〇データ」は対象にする。
    """
    text = unicodedata.normalize("NFKC", question)
    cues = _operation_cues()
    targets = cues.quoted_target.findall(text)
    targets += cues.named_target.findall(text)
    generic = generic_target_words()
    return [t for t in dict.fromkeys(targets) if t not in generic]


def deletion_target_supported(question: str, cited: list[dict[str, Any]]) -> bool:
    """対象名は削除操作を支える同じ本文で照合する。資料名だけの一致は使わない。"""
    targets = deletion_targets(question)

    def compact(value):
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))

    return not targets or any(
        all(compact(target) in compact(str(s.get("text", ""))) for target in targets)
        and re.search(_operation_cues().deletion, str(s.get("text", "")))
        for s in cited
    )
