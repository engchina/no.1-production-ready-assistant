"""明示された数字と位取りの混在表記を、数量にだけ適用する。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from rag_parser_core.language import current_pack
from rag_parser_core.language.text import alternation, arabic_numbers, parse_number, text_patterns

from rag_engine.models.llm import GroundedDraft
from rag_engine.retrieval.task_contract import answer_instruction_texts


def format_draft_numbers(question: str, draft: GroundedDraft, spans: Sequence[Mapping[str, Any]]) -> GroundedDraft:
    """値を変えずに草稿の数量を整え、続く通常の資料照合へ渡す。引用・条件は変更しない。"""
    pack = current_pack()
    instructions = [
        text
        for text in answer_instruction_texts(question)
        if any(cue in text for cue in pack.words("question_cues", "mixed_number_notation_cues"))
    ]
    patterns = text_patterns()
    units = {value: char for char, value in patterns.kanji_multipliers.items() if any(char in text for text in instructions)}
    if not units:
        return draft
    chars = "0-9０-９," + re.escape("".join((*patterns.kanji_digits, *patterns.kanji_multipliers)))
    counters = (
        set(pack.words("grounding_cues", "amount_units")) - set(pack.words("numbers", "date_units")) - set(patterns.kanji_multipliers)
    )
    run = rf"[{chars}]+"
    quantity = re.compile(rf"(?<![A-Za-z0-9_.{chars}])({run}(?:[ \u3000\n]{run})*)(?=\s*{alternation(tuple(counters))})")
    quotes = [
        re.escape(pair[0]) + r"[^" + re.escape(pair[1]) + r"\n]*" + re.escape(pair[1]) for pair in pack.words("symbols", "quote_brackets")
    ]
    url_ends = re.escape("".join(char for char in pack.words("symbols", "sentence_end_chars") if not char.isascii()))
    protected = re.compile(
        rf"```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`|https?://[^\s{url_ends}]+" + ("|" + "|".join(quotes) if quotes else "")
    )

    def convert(match: re.Match[str]) -> str:
        original = match.group()
        raw = unicodedata.normalize("NFKC", original)
        if "," in raw and not re.fullmatch(r"[0-9]{1,3}(?:,[0-9]{3})+", raw):
            return original
        if len(raw) > 1 and raw.startswith("0"):
            return original
        # 排版で分かれた位取りも全体で読む。独立した数の並びは結ばない。
        if re.search(rf"[{chars}][ \u3000\n]$", match.string[: match.start()]):
            return original
        number = parse_number(arabic_numbers(raw))
        if number is None:
            return original
        remaining = number
        parts = []
        for scale, char in sorted(units.items(), reverse=True):
            count, remaining = divmod(remaining, scale)
            if count:
                parts.append(f"{count}{char}")
        if remaining or not parts:
            parts.append(str(remaining))
        rendered = "".join(parts)
        return rendered if parse_number(rendered) == number else original

    def text(value: str) -> str:
        parts = []
        end = 0
        for match in protected.finditer(value):
            parts.extend((quantity.sub(convert, value[end : match.start()]), match.group()))
            end = match.end()
        parts.append(quantity.sub(convert, value[end:]))
        return "".join(parts)

    by_id = {str(span.get("evidence_id")): span for span in spans}
    items = [
        item
        if any("multi_condition" in by_id.get(citation.evidence_id, {}).get("tags", ()) for citation in item.citations)
        else item.model_copy(update={"text": text(item.text)})
        for item in draft.items
    ]
    return draft.model_copy(update={"summary": text(draft.summary), "items": items})
