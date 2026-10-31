"""取得元と一意に対応する識別子だけを、元の文字で回答へ戻す。

横線だけを照合用にそろえ、保存する値を一律に正規化しない。本文では Markdown の表のデータ行と
インラインコードだけを扱い、一般文・URL・コードブロックを変えない。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.features.agent.answer_validation import evidence_source_texts

if TYPE_CHECKING:
    from app.features.agent.runtime import RunStep

_INLINE_CODE = re.compile(r"(?<![\\`])(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_TABLE_RULE = re.compile(r"\s*:?-+:?\s*")
_PROTECTED = re.compile(r"https?://[^\s<>]+|\]\([^\n)]*\)|<[^>\n]*>")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def _is_dash(char: str) -> bool:
    return unicodedata.category(char) == "Pd" or char == "\N{MINUS SIGN}"


def _fold(value: str) -> str:
    return "".join("-" if _is_dash(char) else char for char in value)


def _tokens(text: str) -> Iterable[re.Match[str]]:
    dashes = "".join(sorted({char for char in text if _is_dash(char)}))
    if not dashes:
        return ()
    # URL・ファイルパス・別の長い識別子の一部を、単独の値として取り違えない。
    return re.finditer(
        rf"(?<![\w./:@%{re.escape(dashes)}])"
        rf"\b[A-Za-z0-9_]+(?:[{re.escape(dashes)}][A-Za-z0-9_]+)+\b"
        rf"(?![\w./:@%{re.escape(dashes)}])",
        text,
    )


@dataclass(frozen=True)
class IdentifierCheck:
    text: str
    corrected: int = 0
    ambiguous: int = 0

    @property
    def counts(self) -> dict[str, int]:
        """監査には値そのものを複製せず件数だけ残す。"""
        return {"corrected": self.corrected, "ambiguous": self.ambiguous}


class IdentifierIndex:
    def __init__(self, sources: Iterable[str]) -> None:
        self.values: dict[str, set[str]] = {}
        for text in sources:
            for token in _tokens(text):
                value = token.group()
                self.values.setdefault(_fold(value), set()).add(value)

    def literal(self, text: str) -> IdentifierCheck:
        """表の値・コードの中の識別子。元から存在する表記と、曖昧な候補は変更しない。"""
        protected = [(match.start(), match.end()) for match in _PROTECTED.finditer(text)]
        parts: list[str] = []
        cursor = corrected = ambiguous = 0
        for token in _tokens(text):
            if any(start <= token.start() < end for start, end in protected):
                continue
            value = token.group()
            candidates = self.values.get(_fold(value), set())
            replacement = value
            if value not in candidates:
                if len(candidates) == 1:
                    replacement = next(iter(candidates))
                    corrected += 1
                elif candidates:
                    ambiguous += 1
            parts.extend((text[cursor : token.start()], replacement))
            cursor = token.end()
        parts.append(text[cursor:])
        return IdentifierCheck("".join(parts), corrected, ambiguous)

    def scalar(self, text: str) -> IdentifierCheck:
        """構造化された文字の値は、全体が識別子のときだけ扱う。"""
        tokens = list(_tokens(text))
        if len(tokens) != 1 or tokens[0].span() != (0, len(text)):
            return IdentifierCheck(text)
        return self.literal(text)

    def answer(self, text: str) -> IdentifierCheck:
        lines = text.splitlines(keepends=True)
        output: list[str] = []
        table = False
        fence = ""
        fence_length = corrected = ambiguous = 0
        for line in lines:
            marker = _FENCE.match(line)
            if fence:
                if marker and marker[1][0] == fence and len(marker[1]) >= fence_length:
                    fence = ""
                output.append(line)
                continue
            if line.startswith(("    ", "\t")):
                table = False
                output.append(line)
                continue
            if marker:
                fence, fence_length = marker[1][0], len(marker[1])
                table = False
                output.append(line)
                continue
            cells = line.strip().strip("|").split("|")
            if "|" in line and all(_TABLE_RULE.fullmatch(cell) for cell in cells):
                table = True
                output.append(line)
                continue
            if table and "|" in line and line.strip():
                checked = self.literal(line)
                output.append(checked.text)
                corrected += checked.corrected
                ambiguous += checked.ambiguous
                continue
            table = False

            # 一般文の中でも、コードとして明示された値だけを照合する。
            def code(match: re.Match[str]) -> str:
                nonlocal corrected, ambiguous
                checked = self.literal(match[2])
                corrected += checked.corrected
                ambiguous += checked.ambiguous
                return match[1] + checked.text + match[1]

            output.append(_INLINE_CODE.sub(code, line))
        return IdentifierCheck("".join(output), corrected, ambiguous)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def identifier_index(steps: Sequence[RunStep]) -> IdentifierIndex:
    """成功した根拠の原文と、照会・添付のデータ行だけを使う。生成回答は出典にしない。"""
    sources = list(evidence_source_texts(steps))
    for step in steps:
        call, result = step.tool_call, step.tool_result
        if call is None or result is None or not result.success or step.status != "completed":
            continue
        output = result.output
        if not isinstance(output, dict) or output.get("status") not in {None, "done", "completed"}:
            continue
        rows = output.get("rows")
        if isinstance(rows, list):
            sources.extend(_strings(rows))
    return IdentifierIndex(sources)
