"""評価の案内した画面の採点（#2402）。

評価のケースの「案内すべき画面」（`EvaluationCase.expected_screens`。名前と別名）を、最後の回答が
案内した画面と比べる。

1. 決定的な判定（LLM を呼ばない）: 画面の候補は、案内すべき画面の名前・別名と、回答の引用の chunk の
   画面名（図の説明の `visible_screen_names` の行。行の見出しの語は言語資源の
   `content_labels.vision_fields`）。回答の手順の見出し（番号・Markdown の見出し・太字で始まる行）に
   出る候補を、長い名前から順に重ならないように拾う（「画面A」と「画面A一覧」の両方が候補なら、
   「画面A一覧」の中の「画面A」は数えない）。手順の見出しに 1 つも無ければ、回答の全文で拾う。
2. 判定器（LLM）: 回答に候補が 1 つも出ないとき（回答が別の呼び方で画面を案内しているかもしれない）
   だけ、回答と候補を渡して、案内している画面を判定させる。判定器が無ければ未判定（unresolved）にし、
   案内すべき画面は案内していないとみなす。

採点は、案内すべき画面をすべて案内したか（`screen_match`）と、案内すべき画面のほかの画面を案内したか
（`wrong_screen`）。文字の比較は NFKC・大小文字・空白を無視する。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pr_backend_core.observability.model_purpose import async_call_with_model_purpose
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from rag_parser_core.language import LanguagePack, current_pack

from app.schemas.evaluation import EvaluationScreen

# 図の説明の画面名の項目（言語資源の content_labels.vision_fields の値）。
SCREEN_NAME_FIELD = "visible_screen_names"
# 図の説明で、1 つの項目の値を並べる区切り（`picture_descriptions._join_vision_values`）。
_VALUE_SEPARATOR = " / "
# 手順の見出しの行（番号つきの行・Markdown の見出し・太字で始まる行）。
_STEP_LINE = re.compile(r"^\s*(?:#{1,6}\s+|\d{1,3}\s*[.)．）]\s*|\*\*)")
# 判定器に渡す回答の長さと候補の数の上限。
SCREEN_JUDGE_ANSWER_MAX_CHARS = 6000
SCREEN_JUDGE_MAX_CANDIDATES = 40

ScreenJudgedBy = Literal["deterministic", "judge", "unresolved"]


def _key(text: str) -> str:
    """比較の形（NFKC・大小文字を区別しない・空白なし）。"""
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


@dataclass(frozen=True)
class ScreenScore:
    """1 ケースの案内した画面の採点。"""

    screen_match: bool
    wrong_screen: bool
    guided: list[str] = field(default_factory=list)
    matched: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    wrong: list[str] = field(default_factory=list)
    judged_by: ScreenJudgedBy = "deterministic"


def screen_name_labels(pack: LanguagePack | None = None) -> tuple[str, ...]:
    """図の説明の画面名の行の見出しの語（言語資源。「画面名=visible_screen_names」の左側）。"""
    mapping = (pack or current_pack()).mapping("content_labels", "vision_fields")
    return tuple(label for label, name in mapping.items() if name == SCREEN_NAME_FIELD and label)


def cited_screen_names(texts: Iterable[str], labels: Sequence[str]) -> list[str]:
    """引用の chunk の本文から、図の説明の画面名を読む（出た順・重複なし）。

    `<見出し>: 画面A / 画面B` の 1 行と、`<見出し>:` の後に `- 画面A` の行が続く形の両方を読む。
    """
    heads = {_key(label) for label in labels if label.strip()}
    found: dict[str, str] = {}
    for text in texts:
        listing = False
        for raw in str(text or "").splitlines():
            line = raw.strip()
            if listing:
                if line.startswith("- "):
                    _add_name(found, line[2:])
                    continue
                listing = False
            head, sep, rest = unicodedata.normalize("NFKC", line).partition(":")
            if not sep or _key(head) not in heads:
                continue
            if rest.strip():
                for name in rest.split(_VALUE_SEPARATOR):
                    _add_name(found, name)
            else:
                listing = True
    return list(found.values())


def _add_name(found: dict[str, str], name: str) -> None:
    cleaned = name.strip()
    key = _key(cleaned)
    if len(key) >= 2 and key not in found:
        found[key] = cleaned


def answer_step_lines(answer: str) -> list[str]:
    """回答の手順の見出しの行（番号つきの行・Markdown の見出し・太字で始まる行）。"""
    return [line for line in answer.splitlines() if _STEP_LINE.match(line)]


@dataclass(frozen=True)
class _Candidate:
    key: str
    name: str
    # 案内すべき画面の位置（ほかの画面は None）。
    expected: int | None


def _candidates(
    expected: Sequence[EvaluationScreen], cited: Sequence[str]
) -> tuple[list[_Candidate], dict[str, int]]:
    """画面の候補（比較の形 → 候補）と、案内すべき画面の名前・別名の比較の形 → 位置。"""
    expected_keys: dict[str, int] = {}
    for index, screen in enumerate(expected):
        for name in (screen.name, *screen.aliases):
            key = _key(name)
            if key and key not in expected_keys:
                expected_keys[key] = index
    candidates: dict[str, _Candidate] = {}
    for index, screen in enumerate(expected):
        for name in (screen.name, *screen.aliases):
            key = _key(name)
            if key and key not in candidates:
                candidates[key] = _Candidate(key, screen.name, index)
    for name in cited:
        key = _key(name)
        if key and key not in candidates:
            candidates[key] = _Candidate(key, name, expected_keys.get(key))
    return list(candidates.values()), expected_keys


def _found_candidates(text: str, candidates: Sequence[_Candidate]) -> list[_Candidate]:
    """本文に出る候補を、長い名前から順に、ほかの候補と重ならない位置で拾う（出た位置の順）。"""
    normalized = _key(text)
    claimed: list[tuple[int, int]] = []
    found: dict[str, tuple[int, _Candidate]] = {}
    for candidate in sorted(candidates, key=lambda item: -len(item.key)):
        start = normalized.find(candidate.key)
        while start >= 0:
            end = start + len(candidate.key)
            if not any(start < right and left < end for left, right in claimed):
                claimed.append((start, end))
                if candidate.key not in found:
                    found[candidate.key] = (start, candidate)
            start = normalized.find(candidate.key, start + 1)
    return [candidate for _, candidate in sorted(found.values(), key=lambda item: item[0])]


def _score(
    expected: Sequence[EvaluationScreen],
    matched_indexes: Iterable[int],
    others: Iterable[str],
    judged_by: ScreenJudgedBy,
) -> ScreenScore:
    matched_set = set(matched_indexes)
    matched = [screen.name for index, screen in enumerate(expected) if index in matched_set]
    missing = [screen.name for index, screen in enumerate(expected) if index not in matched_set]
    wrong = list(dict.fromkeys(name for name in others if name.strip()))
    return ScreenScore(
        screen_match=not missing,
        wrong_screen=bool(wrong),
        guided=[*matched, *wrong],
        matched=matched,
        missing=missing,
        wrong=wrong,
        judged_by=judged_by,
    )


def score_screens(
    answer: str, expected: Sequence[EvaluationScreen], cited: Sequence[str]
) -> ScreenScore | None:
    """回答の手順の見出し（無ければ全文）に出る画面で採点する。候補が 1 つも出なければ None。"""
    if not expected:
        return None
    candidates, _ = _candidates(expected, cited)
    for text in ("\n".join(answer_step_lines(answer)), answer):
        found = _found_candidates(text, candidates) if text.strip() else []
        if found:
            return _score(
                expected,
                [item.expected for item in found if item.expected is not None],
                [item.name for item in found if item.expected is None],
                "deterministic",
            )
    return None


def unresolved_score(expected: Sequence[EvaluationScreen]) -> ScreenScore:
    """決められず判定器も使えないとき（案内すべき画面は案内していないとみなす）。"""
    return _score(expected, [], [], "unresolved")


# ---- 判定器（LLM） --------------------------------------------------------------------------


class ScreenJudgement(BaseModel):
    """判定器の応答（回答が案内している画面）。"""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    guided_screens: list[str] = Field(
        default_factory=list,
        max_length=SCREEN_JUDGE_MAX_CANDIDATES,
        description="回答が操作を案内している画面の名前。",
    )
    matched_expected: list[str] = Field(
        default_factory=list,
        max_length=SCREEN_JUDGE_MAX_CANDIDATES,
        description="expected_screens のうち、回答が案内している画面の name。",
    )


SCREEN_JUDGE_RESPONSE_SCHEMA = ScreenJudgement.model_json_schema()

SCREEN_JUDGE_SYSTEM_PROMPT = (
    "あなたは、問い合わせへの回答が、利用者にどの画面での操作を案内してい"
    "るかを判定します。\n"
    "\n"
    "## 手順\n"
    "\n"
    "1. 回答に書いてあることだけで判定し、回答に無い画面を足さないでく"
    "ださい。\n"
)

SCREEN_JUDGE_PROMPT = (
    "## 目的と手順\n"
    "\n"
    "入力の JSON は、回答（answer）、回答が案内すべき画面（e"
    "xpected_screens。name と別名の aliases）"
    "、\n"
    "回答の根拠に写っていた画面の名前（candidates）です。\n"
    "\n"
    "- guided_screens: 回答が操作を案内している画面を入"
    "れます。expected_screens か candidates "
    "の\n"
    "  画面ならその名前で、どちらにも無い画面なら回答に書かれた呼び方で"
    "入れます。画面を案内して\n"
    "  いなければ空にします。\n"
    "- matched_expected: expected_scree"
    "ns のうち、回答が案内している画面の name を入れます。別名や"
    "\n"
    "  言い換えで書かれていても、同じ画面なら入れます。名前が似ていても"
    "別の画面なら入れません。\n"
)


class ScreenJudgeLlm(Protocol):
    async def generate(
        self,
        prompt: str,
        context: str,
        *,
        system_prompt: str | None = None,
        response_schema: Mapping[str, Any] | None = None,
        response_schema_name: str = ...,
    ) -> str: ...


class ScreenJudge(Protocol):
    """回答と案内すべき画面・候補から、回答が案内している画面を判定する。"""

    async def __call__(
        self,
        *,
        answer: str,
        expected: Sequence[EvaluationScreen],
        candidates: Sequence[str],
    ) -> ScreenJudgement: ...


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def parse_screen_judgement(raw: str) -> ScreenJudgement:
    """判定器の応答を検証する（JSON の前後の文は外す）。形が合わなければ ``ValueError``。"""
    match = _JSON_OBJECT.search(str(raw or ""))
    if match is None:
        raise ValueError("判定器の応答に JSON がありません。")
    try:
        return ScreenJudgement.model_validate(json.loads(match.group(0)))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise ValueError("判定器の応答の JSON が画面の判定の形ではありません。") from exc


def screen_judge_context(
    answer: str, expected: Sequence[EvaluationScreen], candidates: Sequence[str]
) -> str:
    """判定器に渡す入力（回答・案内すべき画面・候補）。"""
    return json.dumps(
        {
            "answer": answer[:SCREEN_JUDGE_ANSWER_MAX_CHARS],
            "expected_screens": [
                {"name": screen.name, "aliases": list(screen.aliases)} for screen in expected
            ],
            "candidates": list(candidates)[:SCREEN_JUDGE_MAX_CANDIDATES],
        },
        ensure_ascii=False,
    )


def llm_screen_judge(llm: ScreenJudgeLlm) -> ScreenJudge:
    """LLM（OCI Enterprise AI）で判定する判定器。"""

    async def judge(
        *,
        answer: str,
        expected: Sequence[EvaluationScreen],
        candidates: Sequence[str],
    ) -> ScreenJudgement:
        raw = await async_call_with_model_purpose(
            "evaluation.judge",
            llm.generate,
            SCREEN_JUDGE_PROMPT,
            screen_judge_context(answer, expected, candidates),
            system_prompt=SCREEN_JUDGE_SYSTEM_PROMPT,
            response_schema=SCREEN_JUDGE_RESPONSE_SCHEMA,
            response_schema_name="evaluation_screen_judgement",
        )
        return parse_screen_judgement(raw)

    return judge


def score_from_judgement(
    expected: Sequence[EvaluationScreen], judgement: ScreenJudgement
) -> ScreenScore:
    """判定器の応答で採点する（案内すべき画面の名前・別名に当たる画面は一致、ほかは別の画面）。"""
    _, expected_keys = _candidates(expected, [])
    matched = {
        expected_keys[key] for key in map(_key, judgement.matched_expected) if key in expected_keys
    }
    others: list[str] = []
    for name in judgement.guided_screens:
        index = expected_keys.get(_key(name))
        if index is None:
            others.append(name)
        else:
            matched.add(index)
    return _score(expected, matched, others, "judge")


__all__ = [
    "SCREEN_JUDGE_PROMPT",
    "SCREEN_JUDGE_RESPONSE_SCHEMA",
    "SCREEN_JUDGE_SYSTEM_PROMPT",
    "ScreenJudge",
    "ScreenJudgement",
    "ScreenScore",
    "answer_step_lines",
    "cited_screen_names",
    "llm_screen_judge",
    "parse_screen_judgement",
    "score_from_judgement",
    "score_screens",
    "screen_judge_context",
    "screen_name_labels",
    "unresolved_score",
]
