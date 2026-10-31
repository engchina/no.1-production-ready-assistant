"""Corrective RAG の variant 評価結果を集計する。

回答の正否は CRAG benchmark と同じ 4 段階（Perfect / Acceptable / Missing / Incorrect）で
採点する。誤答を −1、回答不能の表明を 0 とすることで、幻覚的な誤答を未回答より重く扱う。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pr_backend_core.observability.model_purpose import call_with_model_purpose
from pydantic import BaseModel, ConfigDict

CRAG_EVAL_VARIANTS = ("standard_rag", "crag", "crag_rerank")

CragJudgement = Literal["Perfect", "Acceptable", "Missing", "Incorrect"]
CRAG_JUDGEMENT_SCORES: dict[str, float] = {"Perfect": 1.0, "Acceptable": 0.5, "Missing": 0.0, "Incorrect": -1.0}

# 回答不能の表明とみなす語句。決定的判定で Missing（0 点）と Incorrect（−1 点）を分ける。
_MISSING_MARKERS = ("わかりません", "分かりません", "見つかりません", "見つけられません", "確認できません")

CRAG_JUDGE_SYSTEM_PROMPT = """\
与えられた問題のground_truthとanswerを比較してその結果を"Perfect", "Acceptable", "Missing", "Incorrect"の中から一つだけ選んで答えてください. それぞれの定義と規則は以下の通り.
# 定義
Perfect: answerが問題に正しく回答しており, 幻覚的な内容を含んでいない.
Acceptable: answerが問題の回答として有効な内容を含んでいるが, わずかな誤りも含んでいる. ただし, 有効性を壊すほどではない.
Missing: answerが「わかりません」,「見つかりません」, 空の回答, または元の質問を明確にするための要求を含んでいる.
Incorrect: answerが間違っているか問題と無関係な内容を含んでいる.

# 数値問題に関する規則
正解と完全一致する場合のみ「Perfect」とする。
「Acceptable」と判定できるのは、正解値を所定の桁数で四捨五入した結果と一致する場合に限る。
単位の有無や接尾辞・補足語の違い（例：「5」と「5ページ」）は同一とみなす。

# 要素列挙問題に関する規則
すべての要素が完全一致した場合のみ「Perfect」とする。
部分一致はすべて「Incorrect」とする。
「Acceptable」は使用しない。

JSON形式でkeyとして"judged"を含みそのvalueに結果を記載して出力すること.
"""


class CragJudgeOutput(BaseModel):
    """LLM 判定の構造化出力。"""

    model_config = ConfigDict(extra="forbid")
    judged: CragJudgement


@dataclass(frozen=True)
class CragEvalVariantResult:
    """CRAG 評価における variant 別の正誤と回答を保持します。"""

    retrieved_ids: tuple[str, ...] = ()
    used_evidence_ids: tuple[str, ...] = ()
    answer: str = ""


@dataclass(frozen=True)
class CragEvalCase:
    """CRAG goldset の 1 問と期待回答を表します。"""

    case_id: str
    question: str
    expected_evidence_ids: tuple[str, ...]
    expected_answer_terms: tuple[str, ...]
    variants: dict[str, CragEvalVariantResult]
    expected_answer: str = ""

    @property
    def ground_truth(self) -> str:
        """LLM 判定に渡す模範解答。未指定の goldset では期待語句の連結で代用します。"""
        return self.expected_answer or "、".join(self.expected_answer_terms)


CragJudge = Callable[[CragEvalCase, str], str]


def load_crag_goldset(path: str | Path) -> tuple[CragEvalCase, ...]:
    """CRAG goldset JSON から評価ケースを読み込みます。"""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_cases = payload.get("cases") if isinstance(payload, dict) else payload
    if not isinstance(raw_cases, list):
        raise ValueError("CRAG eval goldset must contain a cases list.")
    return tuple(_crag_eval_case(item) for item in raw_cases if isinstance(item, dict))


def judge_by_terms(case: CragEvalCase, answer: str) -> str:
    """LLM を呼ばずに 4 段階判定を返す、オフライン回帰検査用の既定の判定です。

    期待語句がすべて含まれれば Perfect、空または回答不能の表明は Missing、それ以外は Incorrect。
    回答不能が正解のケースは期待語句にその表明を持つため、先に Perfect と判定されます。
    """
    # ponytail: 語句一致では軽微な誤りを測れないため Acceptable は返さない。必要なら llm_crag_judge を使う。
    if _answer_correct(case.expected_answer_terms, answer):
        return "Perfect"
    comparable = _key(answer)
    if not comparable or any(_key(marker) in comparable for marker in _MISSING_MARKERS):
        return "Missing"
    return "Incorrect"


def llm_crag_judge(settings: Any, *, provider_id: str | None = None) -> CragJudge:
    """模範解答と回答を LLM に比較させる判定関数を返します。

    返された関数は呼び出しごとに LLM へ 1 回リクエストします（再試行は `parse_text_response` に従う）。
    空の回答は LLM を呼ばずに Missing とします。
    """
    from rag_engine.dependencies import parse_text_response

    def judge(case: CragEvalCase, answer: str) -> str:
        if not answer.strip():
            return "Missing"
        prompt = f"question: {case.question}\nground_truth: {case.ground_truth}\nanswer: {answer}\n"
        return call_with_model_purpose(
            "evaluation.judge", parse_text_response, CRAG_JUDGE_SYSTEM_PROMPT, prompt, settings, CragJudgeOutput, provider_id=provider_id
        ).judged

    return judge


def evaluate_crag_goldset(cases: Sequence[CragEvalCase], judge: CragJudge | None = None) -> dict[str, Any]:
    """CRAG variant の回答を正解集合と比較して集計します。

    `judge` は (case, answer) から 4 段階判定を返す関数で、未指定時は `judge_by_terms` を使います。
    `crag_score` は判定の点数（1 / 0.5 / 0 / −1）の平均で、範囲は −1〜1 です。
    """
    judge = judge or judge_by_terms
    case_count = len(cases)
    variants: dict[str, dict[str, Any]] = {}
    for variant in CRAG_EVAL_VARIANTS:
        retrieval_hits = 0
        grounded_hits = 0
        correctness_hits = 0
        judgements = dict.fromkeys(CRAG_JUDGEMENT_SCORES, 0)
        for case in cases:
            result = case.variants.get(variant, CragEvalVariantResult())
            if _retrieval_hit(case.expected_evidence_ids, result.retrieved_ids):
                retrieval_hits += 1
            if _grounded(result):
                grounded_hits += 1
            if _answer_correct(case.expected_answer_terms, result.answer):
                correctness_hits += 1
            judgements[judge(case, result.answer)] += 1
        variants[variant] = {
            "case_count": case_count,
            "retrieval_hits": retrieval_hits,
            "retrieval_hit_rate": _rate(retrieval_hits, case_count),
            "grounded_hits": grounded_hits,
            "grounded_rate": _rate(grounded_hits, case_count),
            "answer_correctness_hits": correctness_hits,
            "answer_correctness_rate": _rate(correctness_hits, case_count),
            "judgements": judgements,
            "crag_score": _rate(sum(CRAG_JUDGEMENT_SCORES[name] * count for name, count in judgements.items()), case_count),
        }
    return {"case_count": case_count, "variants": variants}


def format_crag_eval_summary(summary: dict[str, Any]) -> str:
    """CRAG 評価 summary を Markdown 表示へ整形します。"""
    lines = [f"CRAG goldset cases: {int(summary.get('case_count') or 0)}"]
    variants = summary.get("variants") if isinstance(summary.get("variants"), dict) else {}
    for variant in CRAG_EVAL_VARIANTS:
        metrics = variants.get(variant) if isinstance(variants.get(variant), dict) else {}
        lines.append(
            f"- {variant}: retrieval_hit={_pct(metrics.get('retrieval_hit_rate'))}, "
            f"grounded={_pct(metrics.get('grounded_rate'))}, "
            f"answer_correct={_pct(metrics.get('answer_correctness_rate'))}, "
            f"crag_score={float(metrics.get('crag_score') or 0.0):+.3f} "
            f"({', '.join(f'{name}={count}' for name, count in (metrics.get('judgements') or {}).items())})"
        )
    return "\n".join(lines)


def _crag_eval_case(raw: dict[str, Any]) -> CragEvalCase:
    variants = raw.get("variants") if isinstance(raw.get("variants"), dict) else {}
    return CragEvalCase(
        case_id=str(raw.get("case_id") or "").strip(),
        question=str(raw.get("question") or "").strip(),
        expected_evidence_ids=tuple(_string_list(raw.get("expected_evidence_ids"))),
        expected_answer_terms=tuple(_string_list(raw.get("expected_answer_terms"))),
        expected_answer=str(raw.get("expected_answer") or "").strip(),
        variants={str(name): _variant_result(value) for name, value in variants.items() if isinstance(value, dict)},
    )


def _variant_result(raw: dict[str, Any]) -> CragEvalVariantResult:
    return CragEvalVariantResult(
        retrieved_ids=tuple(_string_list(raw.get("retrieved_ids"))),
        used_evidence_ids=tuple(_string_list(raw.get("used_evidence_ids"))),
        answer=str(raw.get("answer") or ""),
    )


def _retrieval_hit(expected_ids: Sequence[str], retrieved_ids: Sequence[str]) -> bool:
    expected = {_key(value) for value in expected_ids if _key(value)}
    retrieved = {_key(value) for value in retrieved_ids if _key(value)}
    if not expected:
        return not retrieved
    return bool(expected & retrieved)


def _grounded(result: CragEvalVariantResult) -> bool:
    retrieved = {_key(value) for value in result.retrieved_ids if _key(value)}
    used = {_key(value) for value in result.used_evidence_ids if _key(value)}
    if not result.answer.strip():
        return False
    if not used:
        return not retrieved
    return bool(retrieved) and used <= retrieved


def _answer_correct(expected_terms: Sequence[str], answer: str) -> bool:
    if not expected_terms:
        return bool(answer.strip())
    comparable = _key(answer)
    return all(_key(term) in comparable for term in expected_terms if _key(term))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = []
    return [str(item).strip() for item in values if str(item).strip()]


def _key(value: Any) -> str:
    return "".join(str(value or "").casefold().split())


def _rate(count: float, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _pct(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    return f"{number * 100:.1f}%"
