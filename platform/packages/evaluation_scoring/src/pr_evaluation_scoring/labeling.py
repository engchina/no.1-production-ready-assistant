"""人手の判定（ラベル）を集める部品（#3418・#2577・#3578）。

評価器（LLM による判定）を校正する人手の判定の、記入の項目の区分・誤りの型と目標・記入者の間の
一致・判定の揺れを、suite の `label-template` / `label-import` / `calibrate` と RAG の判定の校正の
画面で共有する。人の判定はコードで作らない（ここは区分の決め方と一致の集計だけ）。

- `assign_splits`: 単位（ケース）ごとの dev / holdout。層（category）の中を sha256 の順に並べ、
  先頭の `1 - holdout_ratio` を dev にする（決定的）。
- `ERROR_TYPES`: #2577 の判定器の誤りの型と、誤合格・誤不合格を分ける対照（holdout の目標の件数）。
- `agreed_label` / `pairwise_agreement`: 記入者の間の一致（全員一致のラベル・組ごとの Cohen の κ）。
- `label_variance`: 同じ項目を複数回判定したときの、回の間でラベルが変わった項目の率。
- `LABEL_SET_FIELDS` / `LABEL_SET_ITEM_FIELDS`: 人手の判定の JSON（LabelSet）の項目の名前（suite と
  RAG が同じ形を読み書きすることをテストで確かめる）。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import Literal

from pr_evaluation_scoring.agreement import LabelPair, agreement

PASS = "pass"  # nosec B105 - 判定のラベル（資格情報ではない）
FAIL = "fail"
CANNOT_JUDGE = "cannot_judge"
# 記入者が選ぶラベル（cannot_judge は校正に使わない）と、校正に使う人手のラベル。
LABEL_CHOICES = (PASS, FAIL, CANNOT_JUDGE)
HUMAN_LABELS = (PASS, FAIL)

DEFAULT_HOLDOUT_RATIO = 0.7
DEFAULT_SPLIT_SALT = "answer-judge-calibration-v1"
DEFAULT_PERTURB_MAX = 25
# 区分の層の無い単位の層の名前。
NO_CATEGORY = "-"

# 観点ごとの holdout の目標（校正の既定の閾値の件数 50 と、pass・fail の両方）。
ASPECT_HOLDOUT_TARGET = 50
ASPECT_LABEL_TARGET = 20

# 記入者・裁定者の匿名の ID（実名・メールを書かない）。
ANNOTATOR_ID = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")

# 人手の判定の JSON（LabelSet）の項目の名前。
LABEL_SET_FIELDS = ("rubric_id", "description", "provenance", "items")
LABEL_SET_ITEM_FIELDS = (
    "id",
    "split",
    "question",
    "answer",
    "standard_answer",
    "evidence",
    "labels",
    "labeler",
    "note",
    "origin",
    "perturbation",
    "error_types",
    "annotator_labels",
)


@dataclass(frozen=True)
class ErrorType:
    id: str
    group: Literal["誤りの型", "対照"]
    title: str
    aspects: str
    holdout_target: int


# #2577・#3418 で挙がった判定器の誤りの型と、誤合格・誤不合格を分ける対照（件数は holdout の目標）。
ERROR_TYPES: tuple[ErrorType, ...] = (
    ErrorType(
        "attribute_swap",
        "誤りの型",
        "対象・属性・比較の相手の取り違え（別々の属性を同じものとして扱う）",
        "answer_correctness・claim_support",
        5,
    ),
    ErrorType(
        "general_rule_overreach",
        "誤りの型",
        "一般の規則を、質問の個別の対象・範囲を超えて当てはめる（一般論の注記で免責しない）",
        "answer_correctness",
        5,
    ),
    ErrorType(
        "step_order",
        "誤りの型",
        "手順の前後関係の誤り（個々の文は正しくても順序が前提を満たさない）",
        "answer_correctness",
        5,
    ),
    ErrorType(
        "scope_expansion",
        "誤りの型",
        "処理の段階・時間の単位を根拠なく広げる（ある段階の扱いを別の段階へ、日を月へ など）",
        "answer_correctness・claim_support",
        5,
    ),
    ErrorType(
        "unsupported_claim",
        "誤りの型",
        "根拠に無い主張・根拠と矛盾する主張",
        "claim_support",
        5,
    ),
    ErrorType(
        "partial_omission",
        "誤りの型",
        "必要な論点（結論・条件・例外・数量・期間）の一部の不足",
        "requirement_coverage",
        5,
    ),
    ErrorType(
        "correct_paraphrase",
        "対照",
        "正しい言い換え（意味が同じなら pass。誤不合格を見る）",
        "（すべて pass）",
        10,
    ),
    ErrorType(
        "correct_refusal",
        "対照",
        "正しい拒答・利用者への確認（資料に無い・条件が足りない）",
        "（すべて pass）",
        5,
    ),
)
ERROR_TYPE_IDS = tuple(item.id for item in ERROR_TYPES)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def assign_splits(
    strata: Mapping[str, str], *, holdout_ratio: float, salt: str
) -> dict[str, Literal["dev", "holdout"]]:
    """単位（ケース）ごとの区分。層（category）の中を sha256 の順に並べ、先頭を dev にする。"""
    if not 0 < holdout_ratio <= 1:
        raise ValueError("holdout の割合は 0 より大きく 1 以下にしてください。")
    groups: dict[str, list[str]] = {}
    for unit, stratum in strata.items():
        groups.setdefault(stratum, []).append(unit)
    splits: dict[str, Literal["dev", "holdout"]] = {}
    for units in groups.values():
        ordered = sorted(units, key=lambda unit: _hash(f"{salt}:{unit}"))
        dev_count = int(len(ordered) * (1 - holdout_ratio) + 0.5)
        for index, unit in enumerate(ordered):
            splits[unit] = "dev" if index < dev_count else "holdout"
    return splits


def item_order_key(salt: str, item_id: str) -> str:
    """記入の並び（同じケースの回答が続かないよう、sha256 の順。決定的）。"""
    return _hash(f"{salt}:order:{item_id}")


def agreed_label(labels: Sequence[str | None]) -> str | None:
    """全員が同じラベルを付けた行のラベル（未記入・食い違いは None）。"""
    if labels and labels[0] is not None and all(label == labels[0] for label in labels):
        return labels[0]
    return None


@dataclass(frozen=True)
class PairAgreement:
    """2 人の記入者の一致（両方が pass / fail を付けた行。cannot_judge・未記入は数えない）。"""

    annotators: tuple[str, str]
    compared: int
    agreement: float | None
    kappa: float | None


def pairwise_agreement[K: Hashable](
    keys: Sequence[K], annotations: Mapping[str, Mapping[K, str]]
) -> list[PairAgreement]:
    """記入者の組ごとの一致率と Cohen の κ（`annotations` は記入者 → 行 → ラベル）。"""
    pairs: list[PairAgreement] = []
    for first, second in combinations(annotations, 2):
        compared = [
            LabelPair(item_id=str(key), human=a, judge=b)
            for key in keys
            if (a := annotations[first].get(key)) in HUMAN_LABELS
            and (b := annotations[second].get(key)) in HUMAN_LABELS
        ]
        result = agreement(compared, HUMAN_LABELS)
        pairs.append(
            PairAgreement(
                annotators=(first, second),
                compared=result.total,
                agreement=result.accuracy,
                kappa=result.kappa,
            )
        )
    return pairs


@dataclass(frozen=True)
class LabelVariance:
    """同じ項目を複数回判定したときの、回の間の揺れ。"""

    runs: int
    items: int
    unstable: int
    unstable_rate: float | None
    unstable_items: list[str] = field(default_factory=list)


def label_variance(runs: Sequence[Mapping[str, str]], *, digits: int = 4) -> LabelVariance:
    """回の間で判定（ラベルか、判定できなかったことを表す値）が変わった項目を数える。

    `runs` は回ごとの 項目の ID → 判定。その回に無い項目（対象外）は数えない。項目の並びは最初に
    現れた順。
    """
    order: dict[str, None] = {}
    for run in runs:
        for item_id in run:
            order.setdefault(item_id, None)
    unstable: list[str] = []
    for item_id in order:
        if len({run[item_id] for run in runs if item_id in run}) > 1:
            unstable.append(item_id)
    counted = len(order)
    return LabelVariance(
        runs=len(runs),
        items=counted,
        unstable=len(unstable),
        unstable_rate=round(len(unstable) / counted, digits) if counted else None,
        unstable_items=unstable,
    )
