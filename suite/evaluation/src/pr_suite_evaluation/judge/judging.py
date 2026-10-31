"""LLM による判定（LLM-as-judge）: 回答を rubric の観点ごとに判定する（#3418・#2577）。

- 判定の結果は決定的な採点（六指標の report）と分けて記録する（`JudgeRun`）。決定的な report の回
  答の指標（`answer_correctness` など）は埋めない。
- 判定の出力は構造化出力（rubric の JSON schema）で受け、Pydantic で確かめる。形の違う出力は決めら
  れた回数だけ呼び直し、それでも違えば評価不能（`judge_invalid_output`）。呼べなければ評価不能
  （`judge_call_failed` など）。`cannot_judge` も評価不能（`judge_cannot_judge`）。推測の点数にし
  ない。
- 判定の結果を合否に使えるのは、同じ rubric（sha256）・同じモデルで校正に通った観点だけ
  （`calibration`）。校正が無い・通らない観点は参考値で、`passed` は null。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from pr_suite_evaluation.dataset import Case, canonical_sha256
from pr_suite_evaluation.judge.client import JudgeCallError, JudgeClient
from pr_suite_evaluation.judge.rubric import CANNOT_JUDGE, PASS, Rubric
from pr_suite_evaluation.views import CaseView

JUDGE_SCHEMA_VERSION = 1
SCHEMA_NAME = "answer_judgement"
DEFAULT_MAX_ATTEMPTS = 2
# 判定に渡す根拠の本文の合計の上限（文字）。超えた根拠は渡さず、`evidence_truncated` に残す。
DEFAULT_MAX_EVIDENCE_CHARS = 40_000
REASON_MAX_CHARS = 1000
JUDGE_CANNOT_JUDGE = "judge_cannot_judge"
JUDGE_INVALID_OUTPUT = "judge_invalid_output"
NO_ANSWER = "no_answer"

AspectStatusName = Literal["scored", "not_applicable", "unevaluable"]


class AspectOutput(BaseModel):
    """モデルが返す 1 観点の判定。"""

    aspect_id: str
    label: str
    reason: str = Field(default="", max_length=REASON_MAX_CHARS * 4)


class JudgeOutput(BaseModel):
    """モデルが返す判定（rubric の JSON schema と同じ形）。"""

    aspects: list[AspectOutput]


class AspectJudgement(BaseModel):
    status: AspectStatusName
    label: str | None = None
    reason: str | None = None
    unevaluable_reason: str | None = None


class Judgement(BaseModel):
    """1 件（ケース・校正の項目）の判定。"""

    item_id: str
    status: Literal["completed", "failed", "skipped"]
    aspects: dict[str, AspectJudgement] = Field(default_factory=dict)
    attempts: int = 0
    error_code: str | None = None
    error_message: str | None = None
    evidence_truncated: bool = False
    usage: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class JudgeInput:
    """判定の入力（質問・標準回答・回答・根拠）。"""

    item_id: str
    question: str
    answer: str
    standard_answer: str | None
    evidence: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class JudgeSettings:
    """判定の動かし方（結果に記録し、hash を取る）。"""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    max_evidence_chars: int = DEFAULT_MAX_EVIDENCE_CHARS

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.max_evidence_chars < 1:
            raise ValueError("max_attempts と max_evidence_chars は 1 以上にしてください。")

    def provenance(self) -> dict[str, Any]:
        return {"max_attempts": self.max_attempts, "max_evidence_chars": self.max_evidence_chars}


def prompt_sha256(rubric: Rubric) -> str:
    """判定の指示と出力の形の hash（rubric の hash と別に、モデルに渡す文そのものを記録する）。"""
    return canonical_sha256({"instructions": rubric.instructions, "schema": rubric.output_schema()})


def case_input(case: Case, view: CaseView) -> JudgeInput:
    """ケースの最後の回答を判定の入力にする（質問は最初の質問とそれまでの返答をつないだもの）。"""
    final = view.turns[-1]
    question = "\n".join([case.query, *(turn.reply for turn in case.turns[: len(view.turns) - 1])])
    evidence = tuple(
        (f"{item.document_id}#{item.chunk_id}", item.text)
        for item in final.citations
        if item.text and not item.text_omitted
    )
    return JudgeInput(
        item_id=case.id,
        question=question,
        answer=final.answer,
        standard_answer=case.standard_answer,
        evidence=evidence,
    )


def _applicable(rubric: Rubric, item: JudgeInput) -> tuple[list[str], dict[str, AspectJudgement]]:
    targets: list[str] = []
    skipped: dict[str, AspectJudgement] = {}
    for aspect in rubric.aspects:
        if "standard_answer" in aspect.requires and not item.standard_answer:
            skipped[aspect.id] = AspectJudgement(status="not_applicable")
        else:
            targets.append(aspect.id)
    return targets, skipped


def _prompt(item: JudgeInput, aspects: Sequence[str], max_chars: int) -> tuple[str, bool]:
    evidence: list[dict[str, str]] = []
    used = 0
    truncated = False
    for evidence_id, text in item.evidence:
        if used + len(text) > max_chars:
            truncated = True
            continue
        used += len(text)
        evidence.append({"id": evidence_id, "text": text})
    payload = {
        "aspects": list(aspects),
        "question": item.question,
        "standard_answer": item.standard_answer,
        "answer": item.answer,
        "evidence": evidence,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2), truncated


def _parse(text: str, rubric: Rubric, aspects: Sequence[str]) -> dict[str, AspectOutput]:
    """出力を確かめる（JSON・schema・観点の過不足・ラベルの語彙）。違えば ValueError。"""
    output = JudgeOutput.model_validate_json(text)
    found: dict[str, AspectOutput] = {}
    for item in output.aspects:
        if item.aspect_id not in aspects:
            raise ValueError(f"頼んでいない観点です: {item.aspect_id}")
        if item.aspect_id in found:
            raise ValueError(f"観点が重複しています: {item.aspect_id}")
        if item.label not in rubric.labels:
            raise ValueError(f"知らないラベルです: {item.label}")
        found[item.aspect_id] = item
    missing = [aspect for aspect in aspects if aspect not in found]
    if missing:
        raise ValueError(f"判定の無い観点があります: {', '.join(missing)}")
    return found


def judge_item(
    item: JudgeInput,
    rubric: Rubric,
    client: JudgeClient,
    settings: JudgeSettings | None = None,
) -> Judgement:
    """1 件を判定する（回答が空なら判定しない）。"""
    config = settings or JudgeSettings()
    aspects, skipped = _applicable(rubric, item)
    if not item.answer.strip():
        return Judgement(
            item_id=item.item_id,
            status="skipped",
            error_code=NO_ANSWER,
            aspects={
                aspect.id: AspectJudgement(status="not_applicable") for aspect in rubric.aspects
            },
        )
    if not aspects:
        return Judgement(item_id=item.item_id, status="skipped", aspects=skipped)
    prompt, truncated = _prompt(item, aspects, config.max_evidence_chars)
    error_code = JUDGE_INVALID_OUTPUT
    error_message = ""
    usage: dict[str, Any] = {}
    for attempt in range(1, config.max_attempts + 1):
        try:
            reply = client.complete(
                instructions=rubric.instructions,
                prompt=prompt,
                schema=rubric.output_schema(),
                schema_name=SCHEMA_NAME,
            )
        except JudgeCallError as error:
            error_code, error_message = error.error_code, str(error)[:300]
            break
        usage = dict(reply.usage)
        try:
            found = _parse(reply.text, rubric, aspects)
        except (ValidationError, ValueError) as error:
            error_code = JUDGE_INVALID_OUTPUT
            error_message = str(error).splitlines()[0][:300]
            continue
        results = dict(skipped)
        for aspect_id, output in found.items():
            reason = output.reason[:REASON_MAX_CHARS]
            if output.label == CANNOT_JUDGE:
                results[aspect_id] = AspectJudgement(
                    status="unevaluable", reason=reason, unevaluable_reason=JUDGE_CANNOT_JUDGE
                )
            else:
                results[aspect_id] = AspectJudgement(
                    status="scored", label=output.label, reason=reason
                )
        return Judgement(
            item_id=item.item_id,
            status="completed",
            aspects={aspect.id: results[aspect.id] for aspect in rubric.aspects},
            attempts=attempt,
            evidence_truncated=truncated,
            usage=usage,
        )
    failed = dict(skipped)
    for aspect_id in aspects:
        failed[aspect_id] = AspectJudgement(status="unevaluable", unevaluable_reason=error_code)
    return Judgement(
        item_id=item.item_id,
        status="failed",
        aspects={aspect.id: failed[aspect.id] for aspect in rubric.aspects},
        attempts=attempt,
        error_code=error_code,
        error_message=error_message,
        evidence_truncated=truncated,
        usage=usage,
    )


# ---- 判定の集計と校正の照合 ----------------------------------------------------------------


class AspectSummary(BaseModel):
    """1 観点の判定の集計（成功したケースの内訳。合格の率は判定できたケースだけ）。"""

    metric: str
    scored: int = 0
    not_applicable: int = 0
    unevaluable: int = 0
    unevaluable_reasons: dict[str, int] = Field(default_factory=dict)
    pass_count: int = 0
    pass_rate: float | None = None
    threshold: float | None = None
    calibrated: bool = False
    # 校正に通った観点だけ合否を出す（通らなければ null。参考値）。
    passed: bool | None = None


class JudgeSummary(BaseModel):
    case_count: int
    judged_case_count: int
    skipped_case_count: int
    failed_case_count: int
    calibration_status: Literal["calibrated", "partially_calibrated", "uncalibrated", "mismatch"]
    aspects: dict[str, AspectSummary] = Field(default_factory=dict)


class JudgeRun(BaseModel):
    """判定の結果（決定的な採点と分けて、結果のファイルの `judge` に置く）。"""

    schema_version: int = JUDGE_SCHEMA_VERSION
    created_at: str
    provenance: dict[str, Any] = Field(default_factory=dict)
    judgements: list[Judgement] = Field(default_factory=list)
    summary: JudgeSummary


def summarize_judgements(
    judgements: Sequence[Judgement],
    rubric: Rubric,
    *,
    thresholds: Mapping[str, float],
    calibration: Mapping[str, Any] | None,
    model: str,
) -> JudgeSummary:
    """判定を観点ごとに集計し、校正の結果（同じ rubric・モデル）に通った観点だけ合否を出す。"""
    gate, status = calibration_gate(calibration, rubric=rubric, model=model)
    summary: dict[str, AspectSummary] = {}
    for aspect in rubric.aspects:
        item = AspectSummary(
            metric=aspect.metric,
            threshold=thresholds.get(aspect.metric),
            calibrated=gate.get(aspect.id, False),
        )
        for judgement in judgements:
            result = judgement.aspects.get(aspect.id)
            if result is None:
                continue
            if result.status == "scored":
                item.scored += 1
                item.pass_count += int(result.label == PASS)
            elif result.status == "not_applicable":
                item.not_applicable += 1
            else:
                item.unevaluable += 1
                key = result.unevaluable_reason or "unknown"
                item.unevaluable_reasons[key] = item.unevaluable_reasons.get(key, 0) + 1
        item.pass_rate = round(item.pass_count / item.scored, 4) if item.scored else None
        if item.calibrated and item.threshold is not None and item.pass_rate is not None:
            item.passed = item.pass_rate >= item.threshold and item.unevaluable == 0
        summary[aspect.id] = item
    return JudgeSummary(
        case_count=len(judgements),
        judged_case_count=sum(1 for item in judgements if item.status == "completed"),
        skipped_case_count=sum(1 for item in judgements if item.status == "skipped"),
        failed_case_count=sum(1 for item in judgements if item.status == "failed"),
        calibration_status=status,
        aspects=summary,
    )


def calibration_gate(
    calibration: Mapping[str, Any] | None, *, rubric: Rubric, model: str
) -> tuple[
    dict[str, bool], Literal["calibrated", "partially_calibrated", "uncalibrated", "mismatch"]
]:
    """校正の結果から、合否に使える観点を決める（rubric の hash とモデルが同じときだけ）。"""
    if not calibration:
        return {}, "uncalibrated"
    provenance = calibration.get("provenance") or {}
    judge = provenance.get("judge") or {}
    if (provenance.get("rubric") or {}).get("sha256") != rubric.sha256 or judge.get(
        "model"
    ) != model:
        return {}, "mismatch"
    aspects = calibration.get("aspects") or {}
    gate = {
        aspect.id: bool((aspects.get(aspect.id) or {}).get("passed") is True)
        for aspect in rubric.aspects
    }
    if all(gate.values()):
        return gate, "calibrated"
    return gate, "partially_calibrated" if any(gate.values()) else "uncalibrated"
