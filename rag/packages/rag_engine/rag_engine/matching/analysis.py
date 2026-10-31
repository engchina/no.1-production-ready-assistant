"""文字解析と原文の位置。形態素解析・意味の推定は暗黙に行わない。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

ANALYSIS_VERSION = f"unicode-{unicodedata.unidata_version}-1"


@dataclass(frozen=True, order=True)
class Span:
    """Python の文字位置による半開区間（UTF-8 の byte 位置ではない）。"""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end <= self.start:
            raise ValueError("命中範囲は空でない半開区間で指定してください。")


@dataclass(frozen=True)
class AnalysisPolicy:
    """名称・自然文と識別子では、呼出側が解析方式を明示する。"""

    normalization: Literal["nfkc", "literal"] = "nfkc"
    casefold: bool = True
    whitespace: Literal["collapse", "preserve"] = "collapse"
    segmentation: Literal["unicode_words", "cjk_characters"] = "unicode_words"

    def __post_init__(self) -> None:
        if self.normalization not in ("nfkc", "literal") or self.whitespace not in ("collapse", "preserve"):
            raise ValueError("未対応の正規化方式です。")
        if self.segmentation not in ("unicode_words", "cjk_characters"):
            raise ValueError("未対応の分詞方式です。")


DEFAULT_POLICY = AnalysisPolicy()


@dataclass(frozen=True)
class AnalyzedText:
    """解析値・語の位置と、各解析文字から原文への対応。原文を repr に出さない。"""

    original: str = field(repr=False)
    text: str = field(repr=False)
    origins: tuple[Span, ...] = field(repr=False)
    words: tuple[Span, ...] = field(repr=False)
    policy: AnalysisPolicy
    version: str = ANALYSIS_VERSION

    def source_span(self, start: int, end: int) -> Span:
        if start < 0 or end <= start or end > len(self.origins):
            raise ValueError("解析文字列の範囲外です。")
        return Span(self.origins[start].start, self.origins[end - 1].end)


def _normalize(text: str, policy: AnalysisPolicy) -> str:
    value = unicodedata.normalize("NFKC", text) if policy.normalization == "nfkc" else text
    return value.casefold() if policy.casefold else value


def normalize_text(text: str, policy: AnalysisPolicy = DEFAULT_POLICY) -> str:
    """位置が不要な設定語・キーを同じ方式で正規化する。"""
    value = _normalize(text, policy)
    if policy.whitespace == "collapse":
        value = re.sub(r"[\x00-\x1f\x7f]+", " ", value)
        value = re.sub(r"\s+", " ", value).strip()
    return value


def _clusters(text: str):
    """結合文字・濁点・Hangul の合成を分断せず、変換元の範囲を保つ。"""
    start = 0
    for index in range(1, len(text)):
        char = text[index]
        previous = text[start:index]
        normalized = unicodedata.normalize("NFKC", char)
        combines = bool(normalized and unicodedata.category(normalized[0]).startswith("M"))
        composes = unicodedata.normalize("NFKC", previous + char) != (unicodedata.normalize("NFKC", previous) + normalized)
        if not combines and not composes:
            yield start, index
            start = index
    if text:
        yield start, len(text)


def analyze(text: str, policy: AnalysisPolicy = DEFAULT_POLICY) -> AnalyzedText:
    """NFKC の展開・合成、casefold、空白統合を原文位置付きで実行する。"""
    chars: list[str] = []
    origins: list[Span] = []
    clusters = _clusters(text) if policy.normalization == "nfkc" else ((i, i + 1) for i in range(len(text)))
    for start, end in clusters:
        for char in _normalize(text[start:end], policy):
            if policy.whitespace == "collapse" and (char.isspace() or ord(char) < 32 or char == "\x7f"):
                if not chars:
                    continue
                if chars[-1] == " ":
                    origins[-1] = Span(origins[-1].start, end)
                    continue
                char = " "
            chars.append(char)
            origins.append(Span(start, end))
    if policy.whitespace == "collapse" and chars and chars[-1] == " ":
        chars.pop()
        origins.pop()
    value = "".join(chars)
    # 日本語・中国語の無空白文を「完全な単語」と偽らない。文字単位を選ぶか、phrase を使う。
    pattern = r"[\u3040-\u30ff\u3400-\u9fff]|[^\W\u3040-\u30ff\u3400-\u9fff]+" if (policy.segmentation == "cjk_characters") else r"\w+"
    words = tuple(Span(match.start(), match.end()) for match in re.finditer(pattern, value))
    return AnalyzedText(
        text,
        value,
        tuple(origins),
        words,
        policy,
        f"{ANALYSIS_VERSION}:{policy.normalization}:{int(policy.casefold)}:{policy.whitespace}:{policy.segmentation}",
    )
