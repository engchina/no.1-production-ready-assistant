"""業務ガイドを回答に使う（#1238。handoff §8.1・§9）。

公開した業務ガイドのうち質問に最も合う 1 つを決定的に選び（モデルは呼ばない）、条件の状態（既知 /
不明 / 矛盾）を決めて、回答の進め方を決める。

- answer: 必要な条件がそろっている。業務ガイドの手順を回答の材料に渡して答える。
- branch: 不明の条件はあるが、分岐で答える（条件ごとに分けた回答）。
- clarify: 不明・矛盾の条件を利用者に確かめる（検索・MCP は回答を作らずに確認の質問を返す）。
- handoff: 不明の条件は人へ引き継ぐ（窓口を示す）。

適用範囲（#1278。handoff §6.2）: 空の項目は「このプロファイルの中」で制限なし（schema・画面の説明の
とおり）。値のある項目（業務・対象の種類・版）は、質問・検索の絞り込みが名指しした値（手がかり）と
比べ、手がかりがあって合わなければそのガイドを使わない。手がかりが無い項目は ``unverified`` として
回答の記録・MCP に出す（推測で埋めない）。

業務ガイドの内容は、回答ルールと同じ経路（runtime knowledge のルール。`pinned` で必ず使う）で
回答の材料に渡す。手順の検索の手がかりは検索文に足す。分かっている条件で決まる分岐だけを残し、
合わない分岐の行き先の手順は材料から外す（#1278）。

照合の点（#2398）: 照合の語の一致（語 1 つで 2 点）は、質問の原文と、検索・回答プロファイルの用語の
別名で展開した文（``GuideQuery``）で数える（別名で当たった語は内訳の ``alias_terms``）。質問の例に
近ければ 3 点（文字の 2-gram）。意味の照合を ON にしたときだけ、質問と質問の例の埋め込みの近さが
閾値以上で 3 点を足す。上位の 2 つの点が近ければ、どのガイドかを確かめる（``GuideChoice``）。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from rag_engine.generation.answer_envelope import ENVELOPE_SCHEMA_VERSION
from rag_engine.knowledge.runtime_knowledge import (
    PINNED_RULE_TAG,
    SUPPORT_GUIDE_RULE_TAG,
    label_matches,
    parse_runtime_terms,
)

from app.config import settings_today
from app.schemas.classification import category_label
from app.schemas.support_guide import (
    SUPPORT_GUIDE_BOOLEAN_VALUES,
    SupportGuideBranch,
    SupportGuideCondition,
    SupportGuideContent,
    SupportGuideStep,
)

GuideDecision = Literal["answer", "branch", "clarify", "handoff"]
# 条件の状態（handoff §8.1）。conflicting = 質問に 1 つの条件の選択肢が 2 つ以上出た。
ConditionStatus = Literal["known", "unknown", "conflicting"]
# 適用範囲の項目の状態。matched = 手がかりと合った / unverified = 手がかりが無く確かめていない。
ApplicabilityStatus = Literal["matched", "unverified"]
ApplicabilityDimension = Literal["business_domains", "object_types", "versions"]
# 照合の点（照合の語 1 つで 2 点、質問の例に近ければ 3 点）。これ未満は使わない。
MATCH_THRESHOLD = 2
_TERM_POINTS = 2
_EXAMPLE_POINTS = 3
_EXAMPLE_SIMILARITY = 0.5
# 意味の照合（#2398）で、質問の例と埋め込みが近いときに足す点（質問の例の文字の近さと同じ重み）。
SEMANTIC_POINTS = 3
# 上位の 2 つのガイドの点が近いとき、どのガイドかを確かめる確認の条件の id（#2398）。業務ガイドの
# 条件の id（英字で始まる）と重ならない。MCP の conditions には、選んだガイドの題名か id を入れる。
GUIDE_CHOICE_CONDITION_ID = "_guide"
# 確かめるときに選択肢に並べるガイドの数の上限。
_GUIDE_CHOICE_LIMIT = 4
# 質問の中から名指しを探す値の最短の長さ（#553 の業務の名前と同じ）。
_MIN_NAME_CHARS = 2
_BOOLEAN_VALUES = SUPPORT_GUIDE_BOOLEAN_VALUES
# 公開の業務ガイドを読めなかったとき、回答の診断に残す key（回答は続ける。#1278）。
GUIDE_LOAD_FAILED_KEY = "guide_load_failed"
# 回答の診断の、業務ガイドの手順ごとの検索の記録の key（回答エンジンが置き、業務ガイドの記録へ
# 移す。#2395）。
GUIDE_STEP_RETRIEVAL_KEY = "guide_step_retrieval"
# 回答の診断の、業務ガイドの手順ごとの充足（手順の id・状態・必須か）と、手順の対応付けの
# embedding の呼び出しの記録の key（回答エンジンが置き、業務ガイドの記録の step_coverage・
# step_embedding へ移す。#2686）。
GUIDE_STEP_COVERAGE_KEY = "guide_step_coverage"
GUIDE_STEP_EMBEDDING_KEY = "guide_step_embedding"
# 業務ガイドの記録へ移す回答の診断の key と、移した先の key。
_GUIDE_STEP_RECORD_KEYS = (
    (GUIDE_STEP_RETRIEVAL_KEY, "step_retrieval"),
    (GUIDE_STEP_COVERAGE_KEY, "step_coverage"),
    (GUIDE_STEP_EMBEDDING_KEY, "step_embedding"),
)
_DIMENSION_LABELS: dict[str, str] = {
    "business_domains": "業務",
    "object_types": "対象の種類",
    "versions": "資料・システムの版",
}
_SOURCE_NOTES = {"document": "資料で確かめる", "tool": "現場の記録・道具で確かめる"}


def _normalized(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _bigrams(text: str) -> set[str]:
    value = _normalized(text)
    return {value[index : index + 2] for index in range(max(0, len(value) - 1))}


def _similarity(left: str, right: str) -> float:
    a, b = _bigrams(left), _bigrams(right)
    return len(a & b) / len(a | b) if a and b else 0.0


def _label(value: str) -> str:
    """適用範囲の値の比較用の名前（NFKC・大文字小文字・空白の連続をそろえる）。"""
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _domain_label(value: str) -> str:
    """業務の比較用の名前（分類の番号の接頭辞 `10_` を外す。#547 / #553 と同じ）。"""
    return _label(category_label(value))


def _ascii_word(char: str) -> bool:
    return bool(char) and char.isascii() and char.isalnum()


def _named(text: str, labels: Iterable[str]) -> set[str]:
    """質問が名指しした値（比較用の名前）。

    長い名前から探し、見つけた所は短い名前の照合に使わない。英数字の値は英数字の途中に当たる所を
    名指しとみなさない（「v1」を「v10」の中に見つけない）。
    """
    haystack = _label(text)
    found: set[str] = set()
    candidates = {label for label in labels if len(label) >= _MIN_NAME_CHARS}
    for label in sorted(candidates, key=lambda item: (-len(item), item)):
        index = 0
        while (start := haystack.find(label, index)) >= 0:
            end = start + len(label)
            before = haystack[start - 1] if start else ""
            after = haystack[end] if end < len(haystack) else ""
            if (_ascii_word(label[0]) and _ascii_word(before)) or (
                _ascii_word(label[-1]) and _ascii_word(after)
            ):
                index = start + 1
                continue
            found.add(label)
            haystack = haystack[:start] + "\0" + haystack[end:]
            index = start + 1
    return found


@dataclass(frozen=True)
class GuideContext:
    """適用範囲を確かめる手がかり（質問・検索の絞り込みが名指しした値の比較用の名前）。

    空の項目は手がかりなし（その項目は ``unverified``）。
    """

    business_domains: frozenset[str] = frozenset()
    object_types: frozenset[str] = frozenset()
    versions: frozenset[str] = frozenset()


def build_guide_context(
    text: str,
    guides: Iterable[tuple[str, int, SupportGuideContent]],
    *,
    filters: Mapping[str, str] | None = None,
    business_vocabulary: Iterable[str] = (),
) -> GuideContext:
    """質問と検索の絞り込みから、適用範囲の手がかりを読む（モデルは呼ばない）。

    - 業務: 絞り込み ``large_category`` と、質問が名指しした業務（``business_vocabulary`` =
      プロファイルの KB の大分類と、公開ガイドの業務の語）。
    - 対象の種類: 質問が名指しした、公開ガイドの対象の種類の語。
    - 版: 絞り込み ``document_version`` と、質問が名指しした、公開ガイドの版の語。
    """
    filters = filters or {}
    scopes = [content.applicability for _, _, content in guides]
    domains = _named(
        text,
        {
            _domain_label(value)
            for value in [*business_vocabulary, *(v for s in scopes for v in s.business_domains)]
        },
    )
    if value := filters.get("large_category", "").strip():
        domains.add(_domain_label(value))
    objects = _named(text, {_label(v) for s in scopes for v in s.object_types})
    versions = _named(text, {_label(v) for s in scopes for v in s.versions})
    if value := filters.get("document_version", "").strip():
        versions.add(_label(value))
    return GuideContext(frozenset(domains), frozenset(objects), frozenset(versions))


def applicability_status(
    content: SupportGuideContent, context: GuideContext
) -> dict[str, ApplicabilityStatus] | None:
    """値のある適用範囲の項目ごとの状態。手がかりと合わない項目があれば None（使わない）。

    空の項目は「このプロファイルの中」で制限なし（状態に出さない）。
    """
    scope = content.applicability
    result: dict[str, ApplicabilityStatus] = {}
    dimensions: tuple[tuple[str, list[str], Callable[[str], str], frozenset[str]], ...] = (
        ("business_domains", scope.business_domains, _domain_label, context.business_domains),
        ("object_types", scope.object_types, _label, context.object_types),
        ("versions", scope.versions, _label, context.versions),
    )
    for name, values, key, signal in dimensions:
        if not values:
            continue
        if not signal:
            result[name] = "unverified"
            continue
        if not {key(value) for value in values} & signal:
            return None
        result[name] = "matched"
    return result


@dataclass(frozen=True)
class ConditionState:
    condition: SupportGuideCondition
    value: str | None
    # user=利用者が答えた・渡した / question=質問の文から読んだ / None=不明
    source: str | None = None
    # 質問に出た選択肢（2 つ以上なら矛盾。どれかに決めつけない）。
    candidates: tuple[str, ...] = ()

    @property
    def known(self) -> bool:
        return self.value is not None

    @property
    def conflicting(self) -> bool:
        return self.value is None and len(self.candidates) > 1

    @property
    def status(self) -> ConditionStatus:
        if self.known:
            return "known"
        return "conflicting" if self.conflicting else "unknown"


@dataclass(frozen=True)
class GuideMatch:
    guide_id: str
    revision: int
    content: SupportGuideContent
    score: int
    states: tuple[ConditionState, ...] = ()
    decision: GuideDecision = "answer"
    unknown: tuple[SupportGuideCondition, ...] = field(default_factory=tuple)
    # 値のある適用範囲の項目ごとの状態（matched / unverified）。
    applicability: Mapping[str, ApplicabilityStatus] = field(default_factory=dict)
    # 照合の点の内訳（#2398。回答の記録の診断に残す）。
    breakdown: GuideScore | None = None

    def state_of(self, condition_id: str) -> ConditionState | None:
        return next((state for state in self.states if state.condition.id == condition_id), None)

    def summary(self) -> dict[str, Any]:
        """回答の記録・envelope・MCP に出す要約（既知 / 不明・矛盾の条件と適用範囲）。"""
        unknown_conditions: list[dict[str, Any]] = []
        for condition in self.unknown:
            state = self.state_of(condition.id)
            unknown_conditions.append(
                {
                    "id": condition.id,
                    "label": condition.label,
                    "handling": condition.unknown_handling,
                    "state": state.status if state else "unknown",
                    "candidates": list(state.candidates) if state and state.conflicting else [],
                }
            )
        return {
            "guide_id": self.guide_id,
            "revision": self.revision,
            "title": self.content.title,
            "decision": self.decision,
            "known_conditions": [
                {
                    "id": state.condition.id,
                    "label": state.condition.label,
                    "value": state.value,
                    "source": state.source,
                    "state": "known",
                }
                for state in self.states
                if state.known
            ],
            "unknown_conditions": unknown_conditions,
            "applicability": dict(self.applicability),
            **({"match_breakdown": self.breakdown.to_dict()} if self.breakdown else {}),
        }


Vector = Sequence[float]
SemanticStatus = Literal["off", "on", "failed"]


@dataclass(frozen=True)
class GuideQuery:
    """業務ガイドの照合に使う質問（#2398）。

    ``text`` は質問（と会話）の原文で、条件の値・適用範囲の手がかりはこれから読む。``expanded`` は
    用語の別名で展開した文で、照合の語の一致だけに使う。意味の照合を ON にしたときは、質問の
    埋め込み（``vector``）と、ガイドごとの質問の例の埋め込み（``example_vectors``。guide_id →
    ベクトルの列）を持つ。
    """

    text: str
    expanded: str = ""
    alias_terms: tuple[str, ...] = ()
    vector: tuple[float, ...] | None = None
    example_vectors: Mapping[str, Sequence[Vector]] = field(default_factory=dict)
    semantic_threshold: float = 1.0
    semantic: SemanticStatus = "off"

    @property
    def match_text(self) -> str:
        return self.expanded or self.text


def _as_query(query: GuideQuery | str) -> GuideQuery:
    return query if isinstance(query, GuideQuery) else GuideQuery(text=query)


def expand_guide_query(text: str, runtime_knowledge: Mapping[str, Any] | None) -> GuideQuery:
    """質問を、検索・回答プロファイルの用語の別名で展開する（#2398。モデルは呼ばない）。

    質問に出た用語（正式名か別名のどれか）の、ほかの名前（正式名と別名のすべて）を後ろに足す。
    略称で書いた質問が正式名の照合の語に、正式名で書いた質問が略称の照合の語に当たる。検索・
    チャットの確認・MCP の ``rag_lookup_guides`` が同じ関数を使う。
    """
    additions: list[str] = []
    expanded_terms: list[str] = []
    for term in parse_runtime_terms(dict(runtime_knowledge or {})):
        if not term.active:
            continue
        labels = term.labels()
        if not any(label_matches(text, label) for label in labels):
            continue
        missing = [label for label in labels if not label_matches(text, label)]
        if missing:
            additions.extend(missing)
            expanded_terms.append(term.term)
    if not additions:
        return GuideQuery(text=text)
    return GuideQuery(
        text=text,
        expanded=f"{text}\n" + " ".join(dict.fromkeys(additions)),
        alias_terms=tuple(dict.fromkeys(expanded_terms)),
    )


def cosine(left: Vector, right: Vector) -> float:
    """2 つのベクトルの cosine の近さ（長さが違う・0 のベクトルは 0）。"""
    if len(left) != len(right) or not left:
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    return dot / norm if norm else 0.0


@dataclass(frozen=True)
class GuideScore:
    """照合の点の内訳（語・別名・質問の例・意味。#2398）。"""

    terms: tuple[str, ...] = ()
    alias_terms: tuple[str, ...] = ()
    example_similarity: float = 0.0
    # 意味の照合を使っていなければ None。
    semantic_similarity: float | None = None
    semantic_threshold: float = 1.0

    @property
    def example_points(self) -> int:
        return _EXAMPLE_POINTS if self.example_similarity >= _EXAMPLE_SIMILARITY else 0

    @property
    def semantic_points(self) -> int:
        similarity = self.semantic_similarity
        return (
            SEMANTIC_POINTS
            if similarity is not None and (similarity >= self.semantic_threshold)
            else 0
        )

    @property
    def total(self) -> int:
        return (
            _TERM_POINTS * (len(self.terms) + len(self.alias_terms))
            + self.example_points
            + self.semantic_points
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "terms": list(self.terms),
            "term_points": _TERM_POINTS * len(self.terms),
            "alias_terms": list(self.alias_terms),
            "alias_points": _TERM_POINTS * len(self.alias_terms),
            "example_similarity": round(self.example_similarity, 3),
            "example_points": self.example_points,
            "semantic_similarity": (
                round(self.semantic_similarity, 3) if self.semantic_similarity is not None else None
            ),
            "semantic_points": self.semantic_points,
        }


def score_guide(
    content: SupportGuideContent, query: GuideQuery | str, guide_id: str = ""
) -> GuideScore:
    """照合の点の内訳。

    照合の語は、原文で当たれば ``terms``、別名の展開でだけ当たれば ``alias_terms``。
    """
    query = _as_query(query)
    original = _normalized(query.text)
    expanded = _normalized(query.match_text)
    terms: list[str] = []
    aliases: list[str] = []
    for term in content.goal.match_terms:
        key = _normalized(term)
        if key in original:
            terms.append(term)
        elif key in expanded:
            aliases.append(term)
    example = max(
        (_similarity(example, query.text) for example in content.goal.intent_examples),
        default=0.0,
    )
    semantic: float | None = None
    vectors = query.example_vectors.get(guide_id) if query.vector is not None else None
    if query.vector is not None and vectors:
        semantic = max(cosine(query.vector, vector) for vector in vectors)
    return GuideScore(
        terms=tuple(terms),
        alias_terms=tuple(aliases),
        example_similarity=example,
        semantic_similarity=semantic,
        semantic_threshold=query.semantic_threshold,
    )


def guide_score(content: SupportGuideContent, text: GuideQuery | str, guide_id: str = "") -> int:
    return score_guide(content, text, guide_id).total


def _in_period(content: SupportGuideContent, today: date) -> bool:
    period = content.applicability
    if period.effective_from and today < period.effective_from:
        return False
    return not (period.effective_to and today > period.effective_to)


def _provided_value(condition: SupportGuideCondition, raw: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if condition.type == "boolean":
        return _BOOLEAN_VALUES.get(value.casefold())
    if condition.type == "enum":
        exact = next(
            (
                option
                for option in condition.allowed_values
                if _normalized(option) == _normalized(value)
            ),
            None,
        )
        if exact is not None:
            return exact
        # 選んだ選択肢に補足を足した答え（「個別、検証用だけ」）。区切った部分のうち選択肢そのもの
        # が 1 つだけなら、その選択肢（2 つ以上・0 個なら決めない。自由入力の文は呼び出し元）。
        parts = {_normalized(part) for part in _ANSWER_PARTS.split(value) if part.strip()}
        chosen = [option for option in condition.allowed_values if _normalized(option) in parts]
        return chosen[0] if len(chosen) == 1 else None
    return value


# 確認の答え（選んだ選択肢と自由入力を「、」でつないだ値）の区切り。
_ANSWER_PARTS = re.compile(r"[、,，;；/／\n]")


def unresolved_answers(
    match: GuideMatch, provided: Mapping[str, str]
) -> dict[str, tuple[SupportGuideCondition, str]]:
    """渡した答えが選択肢そのものでなく、まだ決まらない選択式の条件（条件の id → (条件, 答え)）。

    利用者が確認に自由入力で答えたとき（「A だけを変え、グループは変えない」）。答えの文から選択肢を
    決めるのは呼び出し元（`support_guide_conditions.resolve_answered_conditions`）。
    """
    unresolved: dict[str, tuple[SupportGuideCondition, str]] = {}
    for state in match.states:
        condition = state.condition
        raw = provided.get(condition.id, "").strip()
        if (
            raw
            and not state.known
            and condition.type == "enum"
            and condition.source == "user"
            and condition.allowed_values
        ):
            unresolved[condition.id] = (condition, raw)
    return unresolved


def condition_states(
    content: SupportGuideContent, text: str, provided: Mapping[str, str]
) -> list[ConditionState]:
    """条件ごとの状態。

    利用者が渡した値を優先し、無ければ選択肢の語が質問に 1 つだけ出たときに既知とする。2 つ以上
    出たら矛盾（conflicting）。質問の文・渡した値で既知にするのは出所が利用者（``source=user``）の
    条件だけで、資料・道具の条件は利用者の申告では決めない（handoff §8.1。#1278）。
    """
    normalized = _normalized(text)
    states: list[ConditionState] = []
    for condition in content.conditions:
        if condition.source != "user":
            states.append(ConditionState(condition, None))
            continue
        if condition.id in provided:
            value = _provided_value(condition, provided[condition.id])
            if value is not None:
                states.append(ConditionState(condition, value, "user"))
                continue
        if condition.type == "enum":
            # 選択肢そのものか、その言い換え（value_aliases。#1237）が質問に出た選択肢。
            found = [
                option
                for option in condition.allowed_values
                if any(
                    _normalized(word) in normalized
                    for word in (option, *condition.value_aliases.get(option, []))
                    if _normalized(word)
                )
            ]
            if len(found) == 1:
                states.append(ConditionState(condition, found[0], "question"))
                continue
            if len(found) > 1:
                states.append(ConditionState(condition, None, candidates=tuple(found)))
                continue
        states.append(ConditionState(condition, None))
    return states


def _askable(state: ConditionState) -> bool:
    """利用者に聞けば決まる条件か（矛盾、または利用者が出所で「確かめる」扱いの不明）。"""
    condition = state.condition
    return state.conflicting or (condition.unknown_handling == "ask" and condition.source == "user")


def decide(
    states: Sequence[ConditionState], *, interactive: bool
) -> tuple[GuideDecision, list[SupportGuideCondition]]:
    """不明・矛盾で必須の条件の扱いから、回答の進め方を決める。

    ``interactive``（チャット）は送信の前に確認の質問を出すので、残った条件は分岐で答える。
    資料・道具が出所の条件は利用者に聞いても決まらないので、確かめずに分岐で答える。
    """
    pending = [state for state in states if not state.known and state.condition.required]
    if not pending:
        return "answer", []
    askable = [state.condition for state in pending if _askable(state)]
    if askable and not interactive:
        return "clarify", askable
    handoff = [
        state.condition
        for state in pending
        if not state.conflicting and state.condition.unknown_handling == "handoff"
    ]
    if handoff:
        return "handoff", handoff
    return "branch", [state.condition for state in pending]


GuideTriple = tuple[str, int, SupportGuideContent]
_Candidate = tuple[GuideScore, str, int, SupportGuideContent, dict[str, ApplicabilityStatus]]


def _candidates(
    guides: Iterable[GuideTriple],
    query: GuideQuery,
    today: date | None,
    context: GuideContext | None,
) -> list[_Candidate]:
    """期間・照合の点・適用範囲に通る候補（点の高い順、同点は guide_id の順。決定的）。"""
    items = list(guides)
    today = today or settings_today()
    context = context if context is not None else build_guide_context(query.text, items)
    result: list[_Candidate] = []
    for guide_id, revision, content in items:
        if not _in_period(content, today):
            continue
        score = score_guide(content, query, guide_id)
        if score.total < MATCH_THRESHOLD:
            continue
        applicability = applicability_status(content, context)
        if applicability is None:
            continue
        result.append((score, guide_id, revision, content, applicability))
    result.sort(key=lambda item: (-item[0].total, item[1]))
    return result


def _match(
    item: _Candidate,
    text: str,
    provided: Mapping[str, str],
    *,
    interactive: bool,
) -> GuideMatch:
    score, guide_id, revision, content, applicability = item
    states = condition_states(content, text, provided)
    decision, unknown = decide(states, interactive=interactive)
    return GuideMatch(
        guide_id,
        revision,
        content,
        score.total,
        tuple(states),
        decision,
        tuple(unknown),
        applicability,
        score,
    )


def _chosen(candidates: Sequence[_Candidate], provided: Mapping[str, str]) -> _Candidate | None:
    """確認で選んだガイド（``GUIDE_CHOICE_CONDITION_ID`` の値。題名か guide_id）の候補。"""
    value = provided.get(GUIDE_CHOICE_CONDITION_ID, "").strip()
    if not value:
        return None
    key = _normalized(value)
    return next(
        (item for item in candidates if value == item[1] or key == _normalized(item[3].title)),
        None,
    )


@dataclass(frozen=True)
class GuideChoice:
    """上位の業務ガイドの点が近く、どれに沿うかを利用者に確かめる（#2398）。"""

    candidates: tuple[GuideMatch, ...]

    def options(self) -> list[str]:
        return [match.content.title for match in self.candidates]

    def question(self) -> str:
        return "次のどれについての質問ですか？"

    def clarifications(self) -> list[dict[str, Any]]:
        """確認の問い（検索・MCP の needs_clarification。業務ガイドの条件の確認と同じ形）。"""
        return [
            {
                "condition_id": GUIDE_CHOICE_CONDITION_ID,
                "label": "業務ガイド",
                "question": self.question(),
                "options": self.options(),
            }
        ]

    def short_answer(self) -> str:
        lines = ["質問に合う業務ガイドが複数あるため、次を教えてください。", ""]
        lines.append(f"・{self.question()}（{'／'.join(self.options())}）")
        return "\n".join(lines)

    def summary(self) -> dict[str, Any]:
        """回答の記録・診断に残す要約（候補ごとの点の内訳）。"""
        return {
            "decision": "clarify",
            "choice_candidates": [
                {
                    "guide_id": match.guide_id,
                    "revision": match.revision,
                    "title": match.content.title,
                    "score": match.score,
                    **({"match_breakdown": match.breakdown.to_dict()} if match.breakdown else {}),
                }
                for match in self.candidates
            ],
        }


def select_guide(
    guides: Iterable[GuideTriple],
    query: GuideQuery | str,
    provided: Mapping[str, str] | None = None,
    *,
    interactive: bool = False,
    today: date | None = None,
    context: GuideContext | None = None,
    close_margin: int | None = None,
) -> GuideMatch | GuideChoice | None:
    """質問に沿う業務ガイドを選ぶ。上位の点が近ければ、どれかを確かめる ``GuideChoice``。

    ``close_margin``（None なら確かめない）以下の点の差の候補を、1 位と合わせて選択肢にする。
    確認で選んだガイド（``provided`` の ``GUIDE_CHOICE_CONDITION_ID``）があれば、それを使う。
    """
    query = _as_query(query)
    provided = provided or {}
    candidates = _candidates(guides, query, today, context)
    if not candidates:
        return None
    chosen = _chosen(candidates, provided)
    if chosen is not None:
        return _match(chosen, query.text, provided, interactive=interactive)
    if close_margin is not None and len(candidates) > 1:
        top = candidates[0][0].total
        close = [item for item in candidates if top - item[0].total <= close_margin]
        titles = {_normalized(item[3].title) for item in close}
        # 題名が同じガイドは選択肢で見分けられないので確かめない。
        if len(close) > 1 and len(titles) == len(close):
            return GuideChoice(
                tuple(
                    _match(item, query.text, provided, interactive=interactive)
                    for item in close[:_GUIDE_CHOICE_LIMIT]
                )
            )
    return _match(candidates[0], query.text, provided, interactive=interactive)


def match_guide(
    guides: Iterable[GuideTriple],
    query: GuideQuery | str,
    provided: Mapping[str, str] | None = None,
    *,
    interactive: bool = False,
    today: date | None = None,
    context: GuideContext | None = None,
) -> GuideMatch | None:
    """質問（と会話）に最も合う公開の業務ガイド。閾値未満・期間外・適用範囲の外なら None。

    ``context`` を渡さなければ、質問の文と公開ガイドの語だけから手がかりを読む。
    """
    selected = select_guide(
        guides, query, provided, interactive=interactive, today=today, context=context
    )
    return selected if isinstance(selected, GuideMatch) else None


def rank_guides(
    guides: Iterable[GuideTriple],
    query: GuideQuery | str,
    provided: Mapping[str, str] | None = None,
    *,
    limit: int = 3,
    today: date | None = None,
    context: GuideContext | None = None,
) -> list[GuideMatch]:
    """質問に当たる公開の業務ガイドを点の高い順に（MCP の rag_lookup_guides）。"""
    query = _as_query(query)
    return [
        _match(item, query.text, provided or {}, interactive=False)
        for item in _candidates(guides, query, today, context)[:limit]
    ]


def _branch_applies(branch: SupportGuideBranch, state: ConditionState | None) -> bool | None:
    """分岐が今の条件に当たるか。条件が決まっていなければ None（どちらとも言えない）。"""
    if state is None or state.conflicting:
        return None
    if branch.when.operator == "unknown":
        return not state.known
    if not state.known:
        return None
    values = {_provided_value(state.condition, value) or value for value in branch.when.values}
    return state.value in values


def visible_plan(
    match: GuideMatch,
) -> tuple[list[SupportGuideStep], list[tuple[SupportGuideBranch, bool | None]]]:
    """回答の材料に渡す手順と分岐（分かっている条件で外れた分岐の行き先を外す。#1278）。

    当たる・決まらない分岐の行き先とその依存先は残す。外れた分岐の行き先と、外した手順だけに依存する
    手順は外す。どの分岐の行き先でもない手順（共通の手順）は残す。
    """
    content = match.content
    steps = {step.id: step for step in content.steps}
    decided = [
        (branch, _branch_applies(branch, match.state_of(branch.when.condition_id)))
        for branch in content.branches
    ]
    keep: set[str] = set()
    stack = [branch.goto_step for branch, applies in decided if applies is not False]
    while stack:
        step_id = stack.pop()
        if step_id in keep or step_id not in steps:
            continue
        keep.add(step_id)
        stack.extend(steps[step_id].depends_on)
    excluded = {branch.goto_step for branch, applies in decided if applies is False} - keep
    changed = bool(excluded)
    while changed:
        changed = False
        for step in content.steps:
            if step.id in excluded or step.id in keep or not step.depends_on:
                continue
            if all(dependency in excluded for dependency in step.depends_on):
                excluded.add(step.id)
                changed = True
    return (
        [step for step in content.steps if step.id not in excluded],
        [(branch, applies) for branch, applies in decided if applies is not False],
    )


def impact_applies(match: GuideMatch, steps: Iterable[SupportGuideStep] | None = None) -> bool:
    """影響範囲・承認が、分かっている条件の場合に係るか（#1320）。

    係る手順（`impact.steps`）を決めていなければ、すべての場合に係る。決めていれば、係る手順の
    どれかが回答の材料に残る（外れた分岐の行き先でない）ときだけ係る。`steps` は `visible_plan`
    の手順（渡さなければ求める）。
    """
    targets = match.content.impact.steps
    if not targets:
        return True
    visible = {step.id for step in (steps if steps is not None else visible_plan(match)[0])}
    return any(step in visible for step in targets)


def _condition_text(condition: SupportGuideCondition) -> str:
    options = f"（{'／'.join(condition.allowed_values)}）" if condition.allowed_values else ""
    note = f"。{_SOURCE_NOTES[condition.source]}" if condition.source in _SOURCE_NOTES else ""
    return condition.label + options + note


def _lines(match: GuideMatch) -> list[str]:
    content = match.content
    lines = [
        f"業務ガイド「{content.title}」（版 {match.revision}）に沿って答える。",
        f"期待する結果: {content.goal.expected_result}",
    ]
    unverified = [
        _DIMENSION_LABELS[name] + f"（{'／'.join(getattr(content.applicability, name))}）"
        for name, status in match.applicability.items()
        if status == "unverified"
    ]
    if unverified:
        lines.append(
            "適用範囲のうち質問から確かめられていない項目: "
            + "、".join(unverified)
            + "。この範囲に当たる場合の案内であることを示す。"
        )
    known = [state for state in match.states if state.known]
    if known:
        lines.append(
            "分かっている条件: " + "、".join(f"{s.condition.label}={s.value}" for s in known)
        )
        # 外れた分岐の手順は visible_plan が外す。残った手順は条件が不成立でも説明させ（#2813）、
        # 資料にある別の場合の記載だけを混ぜないようにする（#1238）。
        lines.append(
            "資料にある、"
            + "、".join(f"{s.condition.label}が「{s.value}」以外の場合" for s in known)
            + "に限った手順・注意は答えに含めない。"
        )
    conflicting = [state for state in match.states if state.conflicting]
    if conflicting:
        lines.append(
            "質問に複数の値が出ている条件（どれかに決めつけず、値ごとに分けて示す）: "
            + "、".join(f"{s.condition.label}（{'／'.join(s.candidates)}）" for s in conflicting)
        )
    unknown = [
        state.condition for state in match.states if not state.known and not state.conflicting
    ]
    if unknown:
        lines.append(
            "分かっていない条件（断定せず、条件ごとに分けて示す）: "
            + "、".join(_condition_text(condition) for condition in unknown)
        )
    steps, branches = visible_plan(match)
    for index, step in enumerate(steps, start=1):
        after = f"（{'・'.join(step.depends_on)} の後）" if step.depends_on else ""
        purpose = f": {step.purpose}" if step.purpose else ""
        done = f" 終わりの条件: {step.done_when}" if step.done_when else ""
        # 手順の id は、回答の材料の「手順ごとの根拠」（#2395）と対応させる印。
        lines.append(f"手順 {index}（{step.id}）. {step.title}{after}{purpose}{done}")
        lines.extend(f"  必要な根拠: {requirement}" for requirement in step.evidence_requirements)
    for branch, applies in branches:
        state = match.state_of(branch.when.condition_id)
        label = state.condition.label if state else branch.when.condition_id
        when = "不明" if branch.when.operator == "unknown" else "／".join(branch.when.values)
        target = next(
            (step.title for step in content.steps if step.id == branch.goto_step), branch.goto_step
        )
        if applies:
            lines.append(f"分岐: {label} が {when} に当たるため「{target}」へ進む。")
        else:
            lines.append(f"分岐: {label} が {when} のときは「{target}」へ進む。")
    for item in content.completion:
        lines.append(
            f"完了の条件: {item.description}"
            + (f"（確かめ方: {item.check_method}）" if item.check_method else "")
        )
    impact = content.impact
    # 分かっている条件で影響範囲・承認が係る手順が外れたら、影響範囲・承認を示させない（#1320）。
    if impact_applies(match, steps):
        scope = {"individual": "個別", "group": "グループ", "all": "全体"}[impact.scope]
        titles = {step.id: step.title for step in content.steps}
        target = (
            "（手順「" + "」「".join(titles.get(i, i) for i in impact.steps) + "」に係る）"
            if impact.steps
            else ""
        )
        lines.append(
            f"影響範囲: {scope}"
            + ("。実施の前に承認が要る" if impact.approval_required else "")
            + target
        )
        if impact.approval_note:
            lines.append(f"承認について: {impact.approval_note}")
    if content.handoff.conditions:
        lines.append("人へ引き継ぐ条件: " + "、".join(content.handoff.conditions))
    lines.append(
        "資料で確かめられない手順・値は答えに含めず、確かめられない点として示す。"
        "案内した操作が実際に終わったとは書かない。"
    )
    return lines


def guide_rule(match: GuideMatch) -> dict[str, Any]:
    """業務ガイドを runtime knowledge のルール（pinned）にする。"""
    steps, _ = visible_plan(match)
    hints = [hint for step in steps for hint in step.retrieval_hints]
    return {
        "id": f"guide-{match.guide_id}",
        "title": match.content.title,
        "triggers": list(dict.fromkeys(hints)),
        "content": "\n".join(_lines(match)),
        "source": f"業務ガイド {match.guide_id}@{match.revision}",
        "tags": [PINNED_RULE_TAG, SUPPORT_GUIDE_RULE_TAG],
        "status": "approved",
    }


def conditional_step_ids(
    steps: Sequence[SupportGuideStep], branches: Sequence[tuple[SupportGuideBranch, bool | None]]
) -> set[str]:
    """条件が決まらない分岐の行き先の手順と、それだけに依存する後続の手順の id（#2491）。

    `visible_plan` の手順と分岐から求める。分岐の前の共通の手順（行き先の依存先）は含めない。
    どの場合に当たるかで要否が変わる手順なので、回答の充足では必須の手順に数えない。
    """
    decided = {branch.goto_step for branch, applies in branches if applies}
    conditional = {branch.goto_step for branch, applies in branches if applies is None} - decided
    changed = True
    while changed:
        changed = False
        for step in steps:
            if step.id in conditional or step.id in decided or not step.depends_on:
                continue
            if all(dependency in conditional for dependency in step.depends_on):
                conditional.add(step.id)
                changed = True
    return conditional


def guide_notices(match: GuideMatch) -> dict[str, Any]:
    """業務ガイドの窓口・引き継ぐ条件・承認（#2792）。回答の「業務ガイドの案内」の節に、資料と分けて出す材料。

    承認は、分かっている条件で影響範囲・承認が係る手順が残るとき（`impact_applies`）だけ。
    """
    impact = match.content.impact
    approval = impact.approval_required and impact_applies(match)
    return {
        "contact": match.content.handoff.contact,
        "handoff_conditions": list(match.content.handoff.conditions),
        "approval_required": approval,
        "approval_note": impact.approval_note if approval else "",
        "branch_notes": list(
            dict.fromkeys(
                branch.note.strip()
                for branch, applies in visible_plan(match)[1]
                if applies is True and branch.note.strip()
            )
        ),
    }


def guide_step_plan(match: GuideMatch) -> list[dict[str, Any]]:
    """手順ごとの検索（#2395）に渡す手順（分かっている条件で外れた手順を除く）。

    手順の id・題名・検索の手がかりと、手順の参照の資料と頁の範囲。ガイドの共通の資料は手順ごとの
    検索を絞らない（全手順に同じ絞り込みを重ねない）。`required` は回答の充足で必須の手順か
    （条件が決まらない分岐の行き先とその後続は false。#2491）。
    """
    steps, branches = visible_plan(match)
    conditional = conditional_step_ids(steps, branches)
    return [
        {
            "id": step.id,
            "title": step.title,
            "required": step.id not in conditional,
            "purpose": step.purpose,
            "evidence_requirements": list(step.evidence_requirements),
            "done_when": step.done_when,
            "depends_on": list(step.depends_on),
            "retrieval_hints": list(step.retrieval_hints),
            "references": [
                {
                    "document_id": reference.document_id,
                    "title": reference.title,
                    "page_ranges": [
                        {"page_start": item.page_start, "page_end": item.page_end}
                        for item in reference.page_ranges
                    ],
                    # 表計算の資料のシートの行の範囲（#2663。頁とは別の位置）。
                    "sheet_ranges": [
                        {
                            "sheet_name": item.sheet_name,
                            "row_start": item.row_start,
                            "row_end": item.row_end,
                        }
                        for item in reference.sheet_ranges
                    ],
                }
                for reference in step.references
            ],
        }
        for step in steps
    ]


def with_guide_rule(payload: Mapping[str, Any] | None, match: GuideMatch) -> dict[str, Any]:
    """用語・同義語と回答ルールの payload に業務ガイドのルールを先頭に足した写し。"""
    base = dict(payload or {})
    rules = [rule for rule in base.get("rules", []) if isinstance(rule, dict)]
    return {
        "schema_version": base.get("schema_version", 1),
        "terms": list(base.get("terms", [])),
        "rules": [guide_rule(match), *rules],
    }


def _condition_options(condition: SupportGuideCondition) -> list[str]:
    if condition.type == "boolean":
        return ["はい", "いいえ"]
    return list(condition.allowed_values)


def _clarification(match: GuideMatch, condition: SupportGuideCondition) -> tuple[str, list[str]]:
    """確かめる問いと選択肢。矛盾した条件は、質問に出た値を問いと選択肢に並べる。"""
    base = condition.question or f"{condition.label}を教えてください。"
    state = match.state_of(condition.id)
    if state is not None and state.conflicting:
        values = "」と「".join(state.candidates)
        return f"質問に{condition.label}の「{values}」が出ています。{base}", list(state.candidates)
    return base, _condition_options(condition)


def clarification_questions(match: GuideMatch) -> list[dict[str, Any]]:
    """確かめる条件と問い・選択肢（検索・MCP の needs_clarification）。"""
    questions: list[dict[str, Any]] = []
    for condition in match.unknown:
        question, options = _clarification(match, condition)
        questions.append(
            {
                "condition_id": condition.id,
                "label": condition.label,
                "question": question,
                "options": options,
            }
        )
    return questions


def short_circuit_answer(match: GuideMatch) -> str:
    """回答を作らずに返す本文（確認の質問・人への引き継ぎ）。"""
    content = match.content
    if match.decision == "clarify":
        lines = [f"「{content.title}」の進め方は条件によって変わるため、次を教えてください。", ""]
        for item in clarification_questions(match):
            options = f"（{'／'.join(item['options'])}）" if item["options"] else ""
            lines.append(f"・{item['question']}{options}")
        return "\n".join(lines)
    contact = content.handoff.contact or "担当の窓口"
    labels = "、".join(condition.label for condition in match.unknown)
    return (
        f"「{content.title}」は、{labels}が分からないと資料だけでは案内できないため、"
        f"{contact}へお問い合わせください。"
    )


_GUIDE_SUMMARY_KEYS = (
    "guide_id",
    "revision",
    "title",
    "decision",
    "known_conditions",
    "unknown_conditions",
    "applicability",
    # 下書きで試した回答で、下書きの版を使ったとき True（#1288）。
    "draft",
    # 照合の点の内訳と、上位が近くて確かめたときの候補（#2398）。
    "match_breakdown",
    "choice_candidates",
    "alias_expansion",
    "semantic",
)


def _guide_summary(guide: Mapping[str, Any]) -> dict[str, Any]:
    return {key: guide.get(key) for key in _GUIDE_SUMMARY_KEYS if key in guide}


def query_summary(query: GuideQuery) -> dict[str, Any]:
    """照合に使った質問の診断（別名で展開した用語・意味の照合の状態。#2398）。"""
    return {"alias_expansion": list(query.alias_terms), "semantic": query.semantic}


def guide_short_circuit_outcome(guide: Mapping[str, Any]) -> Any:
    """確認の質問・人への引き継ぎを、回答（AnswerOutcome）の形にする（引用なし・モデルを呼ばない）。"""
    from app.rag.answer_engine import AnswerOutcome

    clarify = guide.get("decision") == "clarify"
    outcome = "needs_clarification" if clarify else "needs_human"
    summary = _guide_summary(guide)
    envelope = {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "outcome": outcome,
        "requests": [],
        "items": [],
        "conditions": [],
        "gaps": [],
        "confirmations": [],
        # 段落の印・外した段落・回答全体の警告（#2738）。確認・引き継ぎは本文の段落を検証しない。
        "passage_marks": [],
        "removed_passages": [],
        "answer_warnings": [],
        "reference_materials": [],
        "handoff_reasons": ["clarification" if clarify else "handoff"],
        "clarifications": list(guide.get("clarifications") or []),
        "guide": summary,
    }
    return AnswerOutcome(
        answer=str(guide.get("short_answer") or ""),
        citations=[],
        diagnostics={
            "answer_flow": "support_guide",
            "outcome": outcome,
            "envelope": envelope,
            "guide": summary,
            "needs_human_review": not clarify,
            "insufficient_reason": "",
        },
    )


def apply_guide_to_diagnostics(diagnostics: dict[str, Any], guide: Mapping[str, Any]) -> None:
    """回答の記録に業務ガイドの版・条件を残す。分岐で答えたときは「答えた」を条件付きにする。

    公開の業務ガイドを読めなかったときは ``guide_load_failed`` を残す（ガイドを使わずに答えた
    ことを、ガイドが無かったときと区別する。#1278）。
    """
    if guide.get(GUIDE_LOAD_FAILED_KEY):
        diagnostics[GUIDE_LOAD_FAILED_KEY] = True
        return
    summary = _guide_summary(guide)
    # 手順ごとの検索の記録（#2395。手順ごとの検索文・根拠の件数・参照の頁の有無）と、手順ごとの
    # 充足・手順の対応付けの embedding の呼び出し（#2686）は、回答の記録の業務ガイドの記録に置く
    # （回答の構造 envelope には出さない）。
    records = {
        target: value
        for key, target in _GUIDE_STEP_RECORD_KEYS
        if isinstance(value := diagnostics.pop(key, None), dict | list)
    }
    diagnostics["guide"] = {**summary, **records}
    envelope = diagnostics.get("envelope")
    if isinstance(envelope, dict):
        envelope["guide"] = summary
        if guide.get("decision") == "branch" and envelope.get("outcome") == "answered":
            envelope["outcome"] = "conditional"
            diagnostics["outcome"] = "conditional"
            unknown = [str(item.get("label")) for item in guide.get("unknown_conditions") or []]
            envelope["conditions"] = list(
                dict.fromkeys([*envelope.get("conditions", []), *unknown])
            )


GUIDE_CLARIFICATION_PREFIX = "guide:"

# 下書きで試す（#1288）。回答の記録・診断の key と、trace_id の接頭辞（利用者の回答の履歴・
# フィードバック・評価から外すのに使う。uuid の hex 32 字と合わせて 64 字に収まる）。
GUIDE_PREVIEW_KEY = "guide_preview"
GUIDE_PREVIEW_TRACE_PREFIX = "guide-preview-"


@dataclass(frozen=True)
class GuidePreview:
    """公開の版の代わりに、1 回の回答だけに使う下書き（#1288）。公開の版・保存は変えない。"""

    guide_id: str
    draft_revision: int
    content: SupportGuideContent
    published_revision: int | None = None

    def marker(self) -> dict[str, Any]:
        """回答の記録・診断に残す印（どのガイドのどの下書きの版で試したか）。"""
        return {
            "guide_id": self.guide_id,
            "draft_revision": self.draft_revision,
            "published_revision": self.published_revision,
        }


def with_draft(guides: Iterable[GuideTriple], preview: GuidePreview) -> list[GuideTriple]:
    """公開のガイドのうち、試すガイドだけを下書きに置き換えた一覧（未公開なら足す）。

    照合はほかの公開のガイドと同じ規則で行い、公開したときと同じガイドが選ばれるかを確かめる。
    """
    return [
        *(item for item in guides if item[0] != preview.guide_id),
        (preview.guide_id, preview.draft_revision, preview.content),
    ]


def is_guide_preview_trace(trace_id: str) -> bool:
    """下書きで試した回答の trace_id か。"""
    return trace_id.startswith(GUIDE_PREVIEW_TRACE_PREFIX)


def is_guide_preview_record(diagnostics: object) -> bool:
    """回答の記録の診断が、下書きで試した回答のものか。"""
    return isinstance(diagnostics, Mapping) and bool(diagnostics.get(GUIDE_PREVIEW_KEY))


def guide_clarification(match: GuideMatch) -> tuple[str, str, Any] | None:
    """チャットの確認の質問（ルールの確認と同じ形）。選択肢で答えられる最初の不明・矛盾の条件だけ。

    戻り値は (rule_id, 題名, RuleClarification)。rule_id は ``guide:<guide_id>:<condition_id>``。
    選択肢の id は条件の全選択肢の中の位置（``o<1 始まり>``）で、矛盾した条件は質問に出た値だけを
    出す（答えは ``resolve_guide_clarification`` が同じ位置で引き直す）。
    """
    from app.schemas.search_answer_profile_knowledge import (
        ClarificationOption,
        RuleClarification,
    )

    if match.decision != "clarify":
        return None
    for condition in match.unknown:
        values = _condition_options(condition)
        question, shown = _clarification(match, condition)
        if condition.type != "text" and not 2 <= len(shown) <= 8:
            continue
        clarification = RuleClarification(
            question=question[:200],
            multiple=False,
            # 「その他（自由入力）」は常に出す（#1839）。値に当てはめられない答えは前提の文で渡す。
            allow_other=True,
            options=[
                ClarificationOption(
                    id=f"o{values.index(value) + 1}",
                    label=value[:80],
                    search_terms=[value[:50]],
                    premise=f"{condition.label}は「{value}」"[:300],
                )
                for value in shown
            ],
        )
        rule_id = f"{GUIDE_CLARIFICATION_PREFIX}{match.guide_id}:{condition.id}"
        return rule_id, match.content.title, clarification
    return None


# 上位の業務ガイドが近いときの、どのガイドかの確認の id（#2398）。選択肢の id は guide_id。
GUIDE_CHOICE_RULE_ID = f"{GUIDE_CLARIFICATION_PREFIX}{GUIDE_CHOICE_CONDITION_ID}"


def guide_choice_clarification(choice: GuideChoice) -> tuple[str, str, Any]:
    """どのガイドかを確かめるチャットの確認（ルールの確認と同じ形）。選択肢の id は guide_id。"""
    from app.schemas.search_answer_profile_knowledge import (
        ClarificationOption,
        RuleClarification,
    )

    clarification = RuleClarification(
        question=choice.question()[:200],
        multiple=False,
        allow_other=True,
        options=[
            ClarificationOption(
                id=match.guide_id,
                label=match.content.title[:80],
                description=match.content.goal.expected_result[:200],
                premise=f"質問は業務ガイド「{match.content.title}」についてのもの"[:300],
            )
            for match in choice.candidates
        ],
    )
    return GUIDE_CHOICE_RULE_ID, "業務ガイド", clarification


def _resolve_guide_choice(
    guides: Iterable[tuple[str, int, SupportGuideContent]],
    option_ids: Sequence[str],
    other_text: str,
) -> tuple[dict[str, str], str] | None:
    titles = {guide_id: content.title for guide_id, _, content in guides}
    other = other_text.strip()
    if len(option_ids) > 1 or (not option_ids and not other):
        return None
    question = "確認の質問: 次のどれについての質問ですか？"
    if not option_ids:
        return {}, "\n".join([question, f"その他（利用者の入力）: {other}"])
    title = titles.get(option_ids[0])
    if title is None:
        return None
    lines = [question, f"選んだ答え: {title}", f"前提: 質問は業務ガイド「{title}」についてのもの"]
    if other:
        lines.append(f"その他（利用者の入力）: {other}")
    return {GUIDE_CHOICE_CONDITION_ID: option_ids[0]}, "\n".join(lines)


def _guide_condition(
    guides: Iterable[tuple[str, int, SupportGuideContent]], rule_id: str
) -> SupportGuideCondition | None:
    """確認の id（``guide:<guide_id>:<condition_id>``）の業務ガイドの条件（公開の版）。"""
    if not rule_id.startswith(GUIDE_CLARIFICATION_PREFIX):
        return None
    guide_id, _, condition_id = rule_id.removeprefix(GUIDE_CLARIFICATION_PREFIX).partition(":")
    content = next((item for gid, _, item in guides if gid == guide_id), None)
    if content is None:
        return None
    return next((item for item in content.conditions if item.id == condition_id), None)


def _guide_option_value(condition: SupportGuideCondition, option_id: str) -> str | None:
    values = _condition_options(condition)
    index = int(option_id[1:]) if option_id[1:].isdigit() else 0
    return values[index - 1] if 1 <= index <= len(values) else None


def resolve_guide_clarification(
    guides: Iterable[tuple[str, int, SupportGuideContent]],
    rule_id: str,
    option_ids: Sequence[str],
    other_text: str = "",
) -> tuple[dict[str, str], str] | None:
    """チャットの確認の答えを、業務ガイドの条件の値と回答の前提の文にする（保存済みの公開の版で引き直す）。

    text 条件の自由入力は利用者が与えた値として保持する。
    選択・はい／いいえの「その他（自由入力）」（#1839）は条件の値に当てはめない（業務ガイドの値で分かれる手順を、利用者の書いた
    文で決めない）。条件は分からないままにし、利用者の入力を前提の文で回答に渡す（回答は値ごとに分けるか、
    当てはまる値を確かめるよう案内する）。

    どのガイドかの確認（``GUIDE_CHOICE_RULE_ID``。#2398）は、選んだガイドを
    ``GUIDE_CHOICE_CONDITION_ID`` の値にする（照合はそのガイドを使う）。
    """
    if rule_id == GUIDE_CHOICE_RULE_ID:
        return _resolve_guide_choice(list(guides), option_ids, other_text)
    condition = _guide_condition(guides, rule_id)
    other = other_text.strip()
    if condition is None or len(option_ids) > 1 or (not option_ids and not other):
        return None
    question = f"確認の質問: {condition.question or condition.label}"
    if condition.type == "text" and condition.source == "user" and not option_ids and other:
        return {condition.id: other}, "\n".join(
            [question, f"利用者の入力: {other}", f"前提: {condition.label}は「{other}」"]
        )
    if not option_ids:
        context = "\n".join(
            [
                question,
                f"その他（利用者の入力）: {other}",
                f"前提: {condition.label}は業務ガイドの値に当てはまらない"
                f"（利用者の入力: {other}）。"
                "値で分かれる手順は、当てはまる値を確かめるよう案内する",
            ]
        )
        return {}, context
    value = _guide_option_value(condition, option_ids[0])
    if value is None:
        return None
    lines = [question, f"選んだ答え: {value}", f"前提: {condition.label}は「{value}」"]
    if other:
        lines.append(f"その他（利用者の入力）: {other}")
    return {condition.id: value}, "\n".join(lines)


def guide_clarification_record(
    guides: Iterable[tuple[str, int, SupportGuideContent]],
    rule_id: str,
    option_ids: Sequence[str] | None,
    other_text: str = "",
) -> dict[str, object] | None:
    """会話に残す「回答前に確認した内容」（#1839）。``option_ids`` が None なら飛ばした記録。"""
    if rule_id == GUIDE_CHOICE_RULE_ID:
        prompt = "次のどれについての質問ですか？"
        if option_ids is None:
            return {"outcome": "skipped", "entries": [{"prompt": prompt}]}
        titles = {guide_id: content.title for guide_id, _, content in guides}
        return {
            "outcome": "answered",
            "entries": [
                {
                    "prompt": prompt,
                    "selected": [titles[item] for item in option_ids if item in titles],
                    "other_text": other_text.strip(),
                }
            ],
        }
    condition = _guide_condition(guides, rule_id)
    if condition is None:
        return None
    prompt = condition.question or condition.label
    if option_ids is None:
        return {"outcome": "skipped", "entries": [{"prompt": prompt}]}
    selected = [
        value for value in (_guide_option_value(condition, item) for item in option_ids) if value
    ]
    entry: dict[str, object] = {
        "prompt": prompt,
        "selected": selected,
        "other_text": other_text.strip(),
    }
    if not _condition_options(condition):
        # 選択肢の無い条件（text）の答えは条件の値そのもの。「その他: 」を付けない（#3410）。
        entry["free_text_only"] = True
    return {"outcome": "answered", "entries": [entry]}


__all__ = [
    "GUIDE_CHOICE_CONDITION_ID",
    "GUIDE_CHOICE_RULE_ID",
    "GUIDE_CLARIFICATION_PREFIX",
    "GUIDE_LOAD_FAILED_KEY",
    "GUIDE_PREVIEW_KEY",
    "GUIDE_PREVIEW_TRACE_PREFIX",
    "GUIDE_STEP_COVERAGE_KEY",
    "GUIDE_STEP_EMBEDDING_KEY",
    "GUIDE_STEP_RETRIEVAL_KEY",
    "MATCH_THRESHOLD",
    "SEMANTIC_POINTS",
    "ConditionState",
    "GuideChoice",
    "GuideContext",
    "GuideMatch",
    "GuidePreview",
    "GuideQuery",
    "GuideScore",
    "applicability_status",
    "apply_guide_to_diagnostics",
    "build_guide_context",
    "clarification_questions",
    "condition_states",
    "cosine",
    "decide",
    "expand_guide_query",
    "guide_choice_clarification",
    "guide_clarification",
    "guide_clarification_record",
    "guide_rule",
    "guide_score",
    "guide_short_circuit_outcome",
    "guide_notices",
    "guide_step_plan",
    "is_guide_preview_record",
    "is_guide_preview_trace",
    "match_guide",
    "query_summary",
    "rank_guides",
    "resolve_guide_clarification",
    "score_guide",
    "select_guide",
    "short_circuit_answer",
    "unresolved_answers",
    "visible_plan",
    "with_draft",
    "with_guide_rule",
]
