"""回答の本文と根拠の本文の決定的な照合（最終の検証に渡す根拠の並べ替え。#1451）。

最終の検証は根拠の参照を接続ごとに上限（30 件）で切るため、多段の質問では前の段の根拠（古い
呼び出しの根拠）が上限の外に落ち、回答が使った根拠で確かめられずに正しい結論が外れる。回答が
`chunk_id` を書けば先にできる（#1364）が、文書名・章・行で出典を書くモデルでは働かない。
ここでは回答の本文に現れる根拠の特徴で、回答が使った根拠を見分ける（モデルは呼ばない）:

- ID・コード（`SYS-157` のような英字と数字の組。NFKC で全角も半角にそろえる）
- 回答の引用（「…」『…』）が根拠の本文にあるか
- 章節の見出し・シート名が回答にあるか（「第 1 章」だけの短い見出しは数えない）
- 根拠の本文と回答が同じ長い文字の並び（表の行の値の書き写しなど）を持つか
- 回答の章節の参照（「組織規程 第 4 章」のような文書名＋番号）が根拠の章節（`section_path`）と
  文書名に当たるか（見出しの名前を書かず番号だけで参照する回答。#1451）
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

# 英字で始まり数字を含む ID・コード（`SYS-157`・`ISO27001`・`A-12-3`）。
_CODE_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*")
_MIN_CODE_CHARS = 3
# 回答の引用の括弧。
_QUOTE_PATTERN = re.compile(r"「([^「」]+)」|『([^『』]+)』|“([^“”]+)”")
_MIN_QUOTE_CHARS = 3
# 見出し・シート名として数える最小の文字数と、番号だけの見出し（「第 22 章」「3.1」）。
_MIN_LABEL_CHARS = 4
_NUMBER_ONLY_LABEL = re.compile(r"^(第?[0-9.]+[章節条項部編]?|[0-9.]+)$")
# 回答の章節の参照（「第 4 章」「第十二条」）と、根拠の章節の見出し（`section_path` の
# 「第 4 章 承認者の代理」）の先頭の番号。
_SECTION_NUMBER = r"第([0-9]+|[一二三四五六七八九十]+)([章節条])"
_SECTION_REF = re.compile(_SECTION_NUMBER)
_SECTION_LABEL = re.compile("^" + _SECTION_NUMBER)
# 章節の参照の前で文書名を探す長さと、文書名と番号の間に許す文字の数（「の」「」」「、」など）。
_SECTION_NAME_WINDOW = 40
_SECTION_NAME_GAP = 3
# 「第 3 章・第 4 章」のように前の参照に続く参照（間がこの文字数まで）は、前の文書名を引き継ぐ。
_SECTION_LIST_GAP = 3
_MIN_NAME_CHARS = 2
# 文書名の括弧の補足（「組織規程（システムの担当と承認者）」を「組織規程」でも当てる）。
_NAME_NOTE = re.compile(r"（[^（）]*）|\([^()]*\)")
_KANJI_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
# 回答と根拠が同じ並びを持つとみなす長さ（空白を除いた文字数）。
SHARED_RUN_CHARS = 16
# 回答に固有の特徴とみなす、その特徴を持つ根拠の数の上限（多くの根拠が持つ特徴は弱い）。
MAX_DISTINCT_REFS = 5


def normalize(text: str) -> str:
    """NFKC にして空白を除く（全角の英数字・空白の違いで照合を外さない）。"""
    return "".join(unicodedata.normalize("NFKC", text).split())


def _codes(normalized: str) -> set[str]:
    return {
        code
        for code in _CODE_PATTERN.findall(normalized)
        if len(code) >= _MIN_CODE_CHARS and any(char.isdigit() for char in code)
    }


def _section_number(number: str) -> int | None:
    """章節の番号（算用数字・十までの組の漢数字）。読めなければ None。"""
    if number.isdigit():
        return int(number)
    tens, _sep, ones = number.rpartition("十")
    if not _sep:
        return _KANJI_DIGITS.get(number) if len(number) == 1 else None
    if len(tens) > 1 or len(ones) > 1:
        return None
    value = (_KANJI_DIGITS.get(tens, 0) if tens else 1) * 10
    return value + (_KANJI_DIGITS.get(ones, 0) if ones else 0)


@dataclass(frozen=True)
class SectionRef:
    """回答の章節の参照（「組織規程 第 4 章」）。`prefix` は番号の前の文（文書名を探す）。"""

    prefix: str
    number: int
    unit: str


def _section_refs(text: str) -> tuple[SectionRef, ...]:
    refs: list[SectionRef] = []
    previous_end = 0
    previous_prefix = ""
    for match in _SECTION_REF.finditer(text):
        number = _section_number(match.group(1))
        if number is None:
            continue
        start = match.start()
        if refs and start - previous_end <= _SECTION_LIST_GAP:
            prefix = previous_prefix
        else:
            prefix = text[max(previous_end, start - _SECTION_NAME_WINDOW) : start]
        refs.append(SectionRef(prefix=prefix, number=number, unit=match.group(2)))
        previous_end, previous_prefix = match.end(), prefix
    return tuple(refs)


@dataclass(frozen=True)
class AnswerFeatures:
    """回答の本文から取り出した、根拠と照らす特徴。"""

    text: str
    codes: frozenset[str]
    quotes: tuple[str, ...]
    runs: frozenset[str]
    sections: tuple[SectionRef, ...] = ()

    @property
    def empty(self) -> bool:
        return not self.text


def answer_features(answer: str) -> AnswerFeatures:
    text = normalize(answer)
    quotes: list[str] = []
    for match in _QUOTE_PATTERN.finditer(answer):
        quote = normalize(next(group for group in match.groups() if group))
        if len(quote) >= _MIN_QUOTE_CHARS and quote not in quotes:
            quotes.append(quote)
    runs = frozenset(
        text[index : index + SHARED_RUN_CHARS] for index in range(len(text) - SHARED_RUN_CHARS + 1)
    )
    return AnswerFeatures(
        text=text,
        codes=frozenset(_codes(text)),
        quotes=tuple(quotes),
        runs=runs,
        sections=_section_refs(text),
    )


@dataclass
class EvidenceText:
    """根拠 1 件の照合に使う本文・見出し・文書名（同じ根拠を複数のツールが返したら足す）。"""

    texts: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    # 文書のファイル名（章節の参照の文書名に当てる。`section_path` の先頭の題名も使う）。
    names: list[str] = field(default_factory=list)

    def add(self, text: object, labels: list[object], names: list[object] | None = None) -> None:
        if isinstance(text, str) and text.strip():
            self.texts.append(text)
        for label in labels:
            if isinstance(label, str) and label.strip() and label not in self.labels:
                self.labels.append(label)
        for name in names or []:
            if isinstance(name, str) and name.strip() and name not in self.names:
                self.names.append(name)


def _document_names(evidence: EvidenceText) -> set[str]:
    """章節の参照の文書名として当てる名前（ファイル名・その拡張子を除いた名前・章節でない見出し
    〔`section_path` の先頭の文書の題名など〕と、括弧の補足を除いた名前）。"""
    names: set[str] = set()
    for raw in evidence.names:
        name = normalize(raw)
        names.update({name, name.rsplit(".", 1)[0]})
    for label in evidence.labels:
        name = normalize(label)
        if not _SECTION_LABEL.match(name):
            names.update({name, _NAME_NOTE.sub("", name)})
    return {
        name.strip("「」『』") for name in names if len(name.strip("「」『』")) >= _MIN_NAME_CHARS
    }


def _section_keys(features: AnswerFeatures, evidence: EvidenceText) -> set[str]:
    """回答の章節の参照のうち、根拠の章節の番号と文書名の両方に当たるもの（`section:<参照の順>`）。

    番号だけの参照（「第 4 章を参照」）は文書を決めないので数えない。文書名は参照の直前
    （間は「の」「」」など `_SECTION_NAME_GAP` 文字まで）に書かれたものだけを当てる。
    """
    if not features.sections:
        return set()
    numbers: set[tuple[int, str]] = set()
    for label in evidence.labels:
        match = _SECTION_LABEL.match(normalize(label))
        if match and (number := _section_number(match.group(1))) is not None:
            numbers.add((number, match.group(2)))
    if not numbers:
        return set()
    names = _document_names(evidence)
    found: set[str] = set()
    for index, ref in enumerate(features.sections):
        if (ref.number, ref.unit) not in numbers:
            continue
        for name in names:
            position = ref.prefix.rfind(name)
            if position >= 0 and len(ref.prefix) - position - len(name) <= _SECTION_NAME_GAP:
                found.add(f"section:{index}")
                break
    return found


def matched_features(features: AnswerFeatures, evidence: EvidenceText) -> frozenset[str]:
    """根拠の特徴のうち回答の本文に現れたもの（空なら回答と当たらない）。

    鍵は特徴の種類と値（`code:SYS-157`・`quote:…`・`label:…`・`run:<同じ並び>`・
    `section:<回答の章節の参照の順>`）。同じ鍵を持つ
    根拠の数で重みを決める（`order_weights`）。
    """
    if features.empty:
        return frozenset()
    text = normalize("\n".join(evidence.texts))
    found = {f"code:{code}" for code in _codes(text) & features.codes}
    found.update(f"quote:{quote}" for quote in features.quotes if quote in text)
    for label in evidence.labels:
        normalized = normalize(label)
        if (
            len(normalized) >= _MIN_LABEL_CHARS
            and not _NUMBER_ONLY_LABEL.match(normalized)
            and normalized in features.text
        ):
            found.add(f"label:{normalized}")
    if features.runs:
        found.update(
            f"run:{run}"
            for index in range(len(text) - SHARED_RUN_CHARS + 1)
            if (run := text[index : index + SHARED_RUN_CHARS]) in features.runs
        )
    found.update(_section_keys(features, evidence))
    return frozenset(found)


def order_weights(matched: list[frozenset[str]]) -> list[tuple[bool, float]]:
    """根拠ごとの（回答に固有の特徴で当たったか, 当たりの重み）。

    多くの根拠が共有する特徴（台帳のすべての行が持つシート名・表頭など）は回答が使った根拠を
    見分けないので、特徴の重みは 1 / それを持つ根拠の数にし、持つ根拠が
    `MAX_DISTINCT_REFS` 件までの特徴で当たったものだけを「固有の特徴で当たった」とする。
    """
    counts: dict[str, int] = {}
    for keys in matched:
        for key in keys:
            counts[key] = counts.get(key, 0) + 1
    return [
        (
            any(counts[key] <= MAX_DISTINCT_REFS for key in keys),
            sum(1 / counts[key] for key in keys),
        )
        for keys in matched
    ]
