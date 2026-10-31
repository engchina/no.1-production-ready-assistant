"""故意に壊した回答（seeded perturbation）: 判定の校正の合成の対照（#3418・#2577・#3578）。

回答の本文をコードで決まった形に壊し、壊した観点を fail と決めた合成の項目を作る。人手の判定とは
分けて記録し（`origin=synthetic`）、校正の合否（holdout の人手の判定）には入れず、判定が壊れた回答
を合格にしないかの参考として別に集計する（参照の要らない反例。標準の校正の手法）。

壊し方は文書・業務に依らない文の形だけを使う（特定の語・規則を書かない）:

- `swap_values`: 回答の中の違う 2 つの数値を入れ替える（属性の値の取り違え）。
- `drop_condition`: 条件・範囲を表す文のうち、標準回答といちばん重なる 1 文を消す（条件の欠落）。
- `add_unsupported_claim`: 根拠に無い断定の文を足す（根拠に無い主張）。
- `reverse_steps`: 番号つきの手順の並びを逆にする（手順の前後関係の誤り）。

合成の fail は「壊したので不合格のはず」というコードの仮定で、人の判定ではない。壊した結果が誤りに
ならない回答もあり得るので、記入用のブック・RAG の判定の校正の画面で人が除外できる。

suite の `label-template` と RAG の判定の校正の画面が、同じ壊し方と選び方（`perturb_items`）を
使う。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from pr_evaluation_scoring.labeling import FAIL


@dataclass(frozen=True)
class PerturbationResult:
    answer: str
    description: str


@dataclass(frozen=True)
class Perturbation:
    """壊し方（fail にする観点と、本文を壊す関数）。"""

    kind: str
    title: str
    fail_aspects: tuple[str, ...]
    needs_standard_answer: bool
    apply: Callable[[str, str | None, str], PerturbationResult | None]


# 数値（英字・記号に続く ID の一部・citation の番号は除く）。
_NUMBER = re.compile(r"(?<![0-9A-Za-z_.#\-\[［])\d+(?:[.,．]\d+)?(?![0-9A-Za-z_\]］])")
_LIST_MARK = re.compile(r"^\s*$")
_LIST_SUFFIX = (".", ")", "．", "）", "、")
_SENTENCE = re.compile(r"[^。！？!?\n]+[。！？!?]?")
# 条件・範囲・例外を表す日本語の一般の言い回し（文書・業務に依らない文法の語）。
CONDITION_MARKERS: tuple[str, ...] = (
    "場合",
    "ただし",
    "但し",
    "除き",
    "除く",
    "限り",
    "以外",
    "条件",
    "のみ",
    "まで",
    "以上",
    "以下",
    "未満",
    "超え",
    "前に",
    "後に",
)
# 標準回答と重なる割合（文の 2 文字の組のうち標準回答にもあるもの）の下限。
MIN_STANDARD_OVERLAP = 0.2
# 根拠に無い断定の文（どの文書にも書かれていないはずの、例外・確認・範囲を断定する一般の文）。
UNSUPPORTED_CLAIMS: tuple[str, ...] = (
    "なお、この扱いには例外がなく、事前の確認や追加の手続きは一切不要です。",
    "また、この内容は今後も変更されないことが確定しています。",
    "さらに、同じ扱いは関連するすべての対象にも、条件を問わずそのまま適用されます。",
)
_NUMBERED_LINE = re.compile(r"^(?P<mark>\s*(?:\d+[.)．）]|[①-⑳]|[(（]\d+[)）]))\s*(?P<body>\S.*)$")


def _stable_index(key: str, size: int) -> int:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % size


def _is_list_marker(text: str, start: int, end: int) -> bool:
    line_start = text.rfind("\n", 0, start) + 1
    return bool(_LIST_MARK.match(text[line_start:start])) and text[end : end + 1] in _LIST_SUFFIX


def swap_values(answer: str, standard_answer: str | None, key: str) -> PerturbationResult | None:
    matches = [
        match
        for match in _NUMBER.finditer(answer)
        if not _is_list_marker(answer, match.start(), match.end())
    ]
    if not matches:
        return None
    first = matches[0]
    second = next((match for match in matches[1:] if match.group() != first.group()), None)
    if second is None:
        return None
    swapped = (
        answer[: first.start()]
        + second.group()
        + answer[first.end() : second.start()]
        + first.group()
        + answer[second.end() :]
    )
    return PerturbationResult(
        swapped, f"数値 {first.group()} と {second.group()} を入れ替えた（属性の値の取り違え）"
    )


def _bigrams(text: str) -> set[str]:
    compact = re.sub(r"\s+", "", text)
    return {compact[index : index + 2] for index in range(len(compact) - 1)}


def drop_condition(answer: str, standard_answer: str | None, key: str) -> PerturbationResult | None:
    if not standard_answer:
        return None
    sentences = [match for match in _SENTENCE.finditer(answer) if match.group().strip()]
    if len(sentences) < 2:
        return None
    reference = _bigrams(standard_answer)
    best: tuple[float, re.Match[str]] | None = None
    for match in sentences:
        sentence = match.group()
        if not any(marker in sentence for marker in CONDITION_MARKERS):
            continue
        grams = _bigrams(sentence)
        if not grams:
            continue
        overlap = len(grams & reference) / len(grams)
        if overlap >= MIN_STANDARD_OVERLAP and (best is None or overlap > best[0]):
            best = (overlap, match)
    if best is None:
        return None
    match = best[1]
    dropped = (answer[: match.start()] + answer[match.end() :]).strip()
    dropped = re.sub(r"\n{3,}", "\n\n", dropped)
    if not dropped or dropped == answer.strip():
        return None
    return PerturbationResult(dropped, f"条件の文を消した: {match.group().strip()[:80]}")


def add_unsupported_claim(
    answer: str, standard_answer: str | None, key: str
) -> PerturbationResult | None:
    if not answer.strip():
        return None
    claim = UNSUPPORTED_CLAIMS[_stable_index(key, len(UNSUPPORTED_CLAIMS))]
    paragraphs = answer.strip().split("\n\n")
    paragraphs.insert(1, claim)
    return PerturbationResult("\n\n".join(paragraphs), f"根拠に無い断定の文を足した: {claim}")


def reverse_steps(answer: str, standard_answer: str | None, key: str) -> PerturbationResult | None:
    lines = answer.split("\n")
    blocks: list[list[int]] = []
    current: list[int] = []
    for index, line in enumerate(lines):
        if _NUMBERED_LINE.match(line):
            current.append(index)
        elif line.strip():
            if len(current) >= 2:
                blocks.append(current)
            current = []
    if len(current) >= 2:
        blocks.append(current)
    for block in blocks:
        parsed = [
            (index, match)
            for index in block
            if (match := _NUMBERED_LINE.match(lines[index])) is not None
        ]
        bodies = [match.group("body") for _, match in parsed]
        if len(set(bodies)) < 2:
            continue
        changed = list(lines)
        for (index, match), body in zip(parsed, reversed(bodies), strict=True):
            changed[index] = f"{match.group('mark')} {body}"
        return PerturbationResult(
            "\n".join(changed), f"番号つきの手順 {len(block)} 件の並びを逆にした"
        )
    return None


PERTURBATIONS: dict[str, Perturbation] = {
    item.kind: item
    for item in (
        Perturbation(
            kind="swap_values",
            title="属性の値の入れ替え",
            fail_aspects=("answer_correctness", "claim_support"),
            needs_standard_answer=False,
            apply=swap_values,
        ),
        Perturbation(
            kind="drop_condition",
            title="条件の欠落",
            fail_aspects=("requirement_coverage",),
            needs_standard_answer=True,
            apply=drop_condition,
        ),
        Perturbation(
            kind="add_unsupported_claim",
            title="根拠に無い主張",
            fail_aspects=("claim_support",),
            needs_standard_answer=False,
            apply=add_unsupported_claim,
        ),
        Perturbation(
            kind="reverse_steps",
            title="手順の前後関係の誤り",
            fail_aspects=("answer_correctness",),
            needs_standard_answer=False,
            apply=reverse_steps,
        ),
    )
}


def parse_kinds(value: str | None) -> list[str]:
    """`--perturb` の値（`all` かカンマ区切りの種類）を種類の並びにする。"""
    if not value:
        return []
    if value.strip() == "all":
        return list(PERTURBATIONS)
    kinds = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [kind for kind in kinds if kind not in PERTURBATIONS]
    if unknown:
        raise ValueError(
            f"知らない壊し方です: {', '.join(unknown)}（{', '.join(PERTURBATIONS)}・all）"
        )
    return kinds


def perturbed_labels(
    perturbation: Perturbation, applicable_aspects: Sequence[str]
) -> dict[str, str]:
    """合成の項目の fail のラベル（判定の対象の観点のうち、壊した観点だけ）。"""
    return {
        aspect: FAIL for aspect in perturbation.fail_aspects if aspect in set(applicable_aspects)
    }


@dataclass(frozen=True)
class PerturbationSource:
    """壊す元の項目（人が判定する項目の ID・回答・標準回答・判定する観点）。"""

    id: str
    answer: str
    standard_answer: str | None
    aspects: tuple[str, ...]


@dataclass(frozen=True)
class PerturbedItem:
    """合成の項目（`<元の ID>~<壊し方>`）と、fail にした観点。"""

    id: str
    base_item_id: str
    kind: str
    answer: str
    description: str
    labels: dict[str, str] = field(default_factory=dict)


def _order_key(salt: str, item_id: str) -> str:
    return hashlib.sha256(f"{salt}:perturb:{item_id}".encode()).hexdigest()


def perturb_items(
    items: Sequence[PerturbationSource], kinds: Sequence[str], *, perturb_max: int, salt: str
) -> list[PerturbedItem]:
    """壊し方ごとに、sha256 の順（決定的）に `perturb_max` 件まで合成の項目を作る。

    壊せない回答（数値が 1 つしかない・条件の文が無い・番号つきの手順が無いなど）と、壊した観点が
    判定の対象に無い項目は飛ばす。
    """
    synthetic: list[PerturbedItem] = []
    ordered = sorted(items, key=lambda item: _order_key(salt, item.id))
    for kind in kinds:
        perturbation = PERTURBATIONS[kind]
        count = 0
        for item in ordered:
            if count >= perturb_max:
                break
            if perturbation.needs_standard_answer and not item.standard_answer:
                continue
            labels = perturbed_labels(perturbation, item.aspects)
            if not labels:
                continue
            result = perturbation.apply(item.answer, item.standard_answer, item.id)
            if result is None or result.answer == item.answer:
                continue
            synthetic.append(
                PerturbedItem(
                    id=f"{item.id}~{kind}",
                    base_item_id=item.id,
                    kind=kind,
                    answer=result.answer,
                    description=result.description,
                    labels=labels,
                )
            )
            count += 1
    return synthetic
