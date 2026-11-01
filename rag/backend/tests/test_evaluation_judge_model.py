"""品質評価の判定のモデル（#3574）を合成の設定・決定論スタブで確かめる。

既定の判定モデル（共通のモデル設定の ``default_judge_model_id``）は、
空（既定）= 既定のテキストモデル、
``same_as_answer`` = 回答を作ったモデル、登録モデルの ID = そのモデル。
判定の LLM はそのモデルの接続・
入力の上限で呼び、使ったモデルを判定の診断と評価の結果に残す。
"""

import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import pytest
from pr_system_settings.model import ENTERPRISE_AI_JUDGE_SAME_AS_ANSWER
from pytest import MonkeyPatch

from app.config import EnterpriseAiConfiguredModel, Settings
from app.rag.answer_engine import evaluate_answer_record
from app.rag.evaluation import EvaluationRunner
from app.rag.pipeline import SearchStageProgressCallback
from app.schemas.evaluation import EvaluationAnswerJudgement, EvaluationCase
from app.schemas.search import RetrievedChunk, SearchDiagnostics, SearchRequest, SearchResponse

PRIMARY = "https://primary.example/openai/v1"
SECONDARY = "https://secondary.example/openai/v1"
DEFAULT_MODEL = "openai.gpt-oss-120b"
PROFILE_MODEL = "openai.gpt-5.4"
FIXED_MODEL = "model-fixed"
BODY = "手順 A の後に手順 B を実行します。"

LlmCall = tuple[str, str, str, int]


def _settings(judge: str = "") -> Settings:
    return Settings(
        oci_enterprise_ai_endpoint=PRIMARY,
        oci_enterprise_ai_project_ocid="ocid1.project.primary",
        oci_enterprise_ai_api_key="sk-primary",
        oci_enterprise_ai_secondary_endpoint=SECONDARY,
        oci_enterprise_ai_secondary_project_ocid="ocid1.project.secondary",
        oci_enterprise_ai_secondary_api_key="sk-secondary",
        oci_enterprise_ai_models=[
            EnterpriseAiConfiguredModel(
                model_id=DEFAULT_MODEL, display_name="A", vision_enabled=True
            ),
            EnterpriseAiConfiguredModel(
                model_id=PROFILE_MODEL,
                display_name="B",
                connection_id="secondary",
                max_input_tokens=16_000,
            ),
            EnterpriseAiConfiguredModel(model_id=FIXED_MODEL, display_name="C"),
        ],
        oci_enterprise_ai_default_text_model=DEFAULT_MODEL,
        oci_enterprise_ai_default_vision_model=DEFAULT_MODEL,
        oci_enterprise_ai_default_judge_model=judge,
    )


class _Pipeline:
    """回答の診断に回答のモデルを載せる（載せない）固定の pipeline。"""

    def __init__(self, diagnostics_model: str | None = None) -> None:
        self.diagnostics_model = diagnostics_model

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: SearchStageProgressCallback | None = None,
    ) -> SearchResponse:
        answer: dict[str, Any] = {}
        if self.diagnostics_model:
            answer["models"] = {"llm": {"model_id": self.diagnostics_model, "label": "x"}}
        return SearchResponse(
            answer=BODY,
            citations=[
                RetrievedChunk(document_id="doc-1", chunk_id="doc-1:0", text=BODY, score=1.0)
            ],
            trace_id=trace_id or "trace",
            elapsed_ms=1.0,
            diagnostics=SearchDiagnostics(answer=answer or None),
        ).with_evaluation_input({"question": "手順", "answer_text": BODY})


class _Judge:
    def __init__(self, *, fail: bool = False) -> None:
        self.models: list[str] = []
        self.fail = fail

    async def __call__(
        self,
        *,
        trace_id: str,
        evaluation_input: Mapping[str, object],
        standard_answer: str,
        citations: Sequence[RetrievedChunk],
        timeout_seconds: float,
        judge_model_id: str,
    ) -> Mapping[str, object]:
        self.models.append(judge_model_id)
        if self.fail:
            raise RuntimeError("synthetic failure")
        return {
            "status": "completed",
            "passed": True,
            "claim_checks": [],
            "coverage_checks": [],
            "requirement_coverage": 1.0,
            "judge_diagnostics": {"judge_model_id": judge_model_id},
        }


def _profile_resolver(answer_model: str | None) -> Any:
    async def resolve(
        request: SearchRequest, settings: Settings, _context: Sequence[str]
    ) -> tuple[SearchRequest, Settings]:
        if answer_model:
            settings = settings.model_copy(
                update={"oci_enterprise_ai_default_text_model": answer_model}
            )
        return request, settings

    return resolve


async def _run(
    settings: Settings,
    *,
    profile_model: str | None = None,
    profile: bool = False,
    pipeline: _Pipeline | None = None,
    judge: _Judge | None = None,
) -> tuple[_Judge, EvaluationAnswerJudgement]:
    judge = judge or _Judge()
    runner = EvaluationRunner(
        pipeline=pipeline or _Pipeline(),
        settings=settings,
        answer_judge=judge,
        profile_resolver=_profile_resolver(profile_model),
    )
    metrics = await runner.run(
        cases=[EvaluationCase(id="case-1", query="手順", standard_answer=BODY)],
        top_k=3,
        knowledge_base_ids=["kb-1"],
        search_answer_profile_id="profile-1" if profile else None,
    )
    judgement = metrics.case_results[0].answer_evaluation
    assert judgement is not None
    return judge, judgement


async def test_default_judges_every_pass_with_default_text_model() -> None:
    """既定（空）は、プロファイルの回答のモデルにかかわらず既定のテキストモデルで判定する。"""
    judge, judgement = await _run(_settings(), profile=True, profile_model=PROFILE_MODEL)
    assert judge.models == [DEFAULT_MODEL]
    assert judgement.judge_model_id == DEFAULT_MODEL
    assert judgement.judge_diagnostics is not None
    assert judgement.judge_diagnostics.judge_model_id == DEFAULT_MODEL


async def test_same_as_answer_judges_profile_pass_with_profile_model() -> None:
    """「回答と同じモデル」では、プロファイルの回答のモデルで答えた pass をそのモデルで判定する。"""
    judge, judgement = await _run(
        _settings(ENTERPRISE_AI_JUDGE_SAME_AS_ANSWER), profile=True, profile_model=PROFILE_MODEL
    )
    assert judge.models == [PROFILE_MODEL]
    assert judgement.judge_model_id == PROFILE_MODEL


@pytest.mark.parametrize(
    "profile",
    [False, True],
    ids=["knowledge-base-pass", "profile-without-answer-model"],
)
async def test_same_as_answer_without_answer_model_uses_default(profile: bool) -> None:
    """ナレッジベースの pass・回答のモデルの無いプロファイルは、既定のテキストモデルで判定する。"""
    judge, judgement = await _run(_settings(ENTERPRISE_AI_JUDGE_SAME_AS_ANSWER), profile=profile)
    assert judge.models == [DEFAULT_MODEL]
    assert judgement.judge_model_id == DEFAULT_MODEL


async def test_same_as_answer_prefers_answer_diagnostics_model() -> None:
    """回答の診断の ``models.llm.model_id``（回答を実際に作ったモデル）を設定より優先する。"""
    judge, _ = await _run(
        _settings(ENTERPRISE_AI_JUDGE_SAME_AS_ANSWER),
        profile=True,
        profile_model=FIXED_MODEL,
        pipeline=_Pipeline(diagnostics_model=PROFILE_MODEL),
    )
    assert judge.models == [PROFILE_MODEL]


async def test_fixed_judge_model_judges_every_answer() -> None:
    """登録モデルを指定したら、どの pass の回答もそのモデルで判定する。"""
    judge, judgement = await _run(_settings(FIXED_MODEL), profile=True, profile_model=PROFILE_MODEL)
    assert judge.models == [FIXED_MODEL]
    assert judgement.judge_model_id == FIXED_MODEL


async def test_judge_model_is_kept_when_judge_fails() -> None:
    """判定を完了できなかったケースにも、判定のモデルを結果に残す。"""
    _, judgement = await _run(
        _settings(ENTERPRISE_AI_JUDGE_SAME_AS_ANSWER),
        profile=True,
        profile_model=PROFILE_MODEL,
        judge=_Judge(fail=True),
    )
    assert judgement.status == "error"
    assert judgement.judge_model_id == PROFILE_MODEL


def _fake_evaluation_llm(calls: list[LlmCall]) -> Callable[..., Any]:
    """判定の LLM の決定論スタブ。呼ばれた (schema, model, endpoint, 入力の上限) を記録する。"""
    from rag_engine.config import get_llm_provider
    from rag_engine.evaluation.answer_eval import (
        AnswerCorrectnessOutput,
        ClaimJudgement,
        CoverageOutput,
        StandardAnswerScope,
    )

    def fake(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
        provider = get_llm_provider(
            settings, options.get("provider_id") or settings.default_answer_llm
        )
        calls.append(
            (schema.__name__, provider.model, provider.endpoint, settings.answer_max_input_tokens)
        )
        if schema is AnswerCorrectnessOutput:
            return AnswerCorrectnessOutput(verdict="correct", reason="一致")
        if schema is StandardAnswerScope:
            return StandardAnswerScope.model_validate(
                {
                    "requirements": [{"standard_answer_unit_ids": ["S1"], "requirement": "手順"}],
                    "excluded_case_data": [],
                }
            )
        if schema is CoverageOutput:
            passage = json.loads(prompt)["answer_passages"][0]["id"]
            return CoverageOutput.model_validate(
                {
                    "coverage_checks": [
                        {
                            "requirement_index": 1,
                            "reason": "手順の説明",
                            "status": "addressed",
                            "answer_passage_id": passage,
                        }
                    ]
                }
            )
        if schema is ClaimJudgement:
            return ClaimJudgement(reason="手順の確認の案内", status="data_confirmation")
        raise AssertionError(f"unexpected schema: {schema.__name__}")

    return fake


def _stub_llm(monkeypatch: MonkeyPatch) -> list[LlmCall]:
    import rag_engine.adapters.oci as engine_oci

    calls: list[LlmCall] = []
    monkeypatch.setattr(engine_oci, "parse_text_response", _fake_evaluation_llm(calls))
    return calls


def _evaluate(settings: Settings, judge_model_id: str | None) -> dict[str, Any]:
    return evaluate_answer_record(
        {"question": "手順", "answer_text": BODY, "evidence_items": []},
        BODY,
        settings,
        citations=[],
        judge_model_id=judge_model_id,
    )


def test_judge_calls_use_judge_model_connection_and_input_limit(
    monkeypatch: MonkeyPatch,
) -> None:
    """判定の LLM は判定のモデルの接続・入力の上限で呼び、判定の診断にモデルを残す。"""
    calls = _stub_llm(monkeypatch)

    evaluation = _evaluate(_settings(), PROFILE_MODEL)

    assert evaluation["status"] == "completed", evaluation
    assert evaluation["judge_diagnostics"]["judge_model_id"] == PROFILE_MODEL
    assert calls
    assert {(model, endpoint, limit) for _, model, endpoint, limit in calls} == {
        (PROFILE_MODEL, SECONDARY, 16_000)
    }


def test_judge_without_explicit_model_uses_setting(monkeypatch: MonkeyPatch) -> None:
    """判定のモデルを渡さなければ、既定の判定モデル（既定は既定のテキストモデル）で判定する。"""
    calls = _stub_llm(monkeypatch)

    assert _evaluate(_settings(), None)["judge_diagnostics"]["judge_model_id"] == DEFAULT_MODEL
    fixed = _evaluate(_settings(FIXED_MODEL), None)
    assert fixed["judge_diagnostics"]["judge_model_id"] == FIXED_MODEL
    assert {model for _, model, _, _ in calls} == {DEFAULT_MODEL, FIXED_MODEL}


def test_standard_scope_is_fixed_once_per_judge_model(monkeypatch: MonkeyPatch) -> None:
    """評価の job の比較範囲の記録（#3570）は判定のモデルごとに分かれ、同じモデルでは使い回す。"""
    from rag_engine.evaluation.answer_eval import standard_scope_cache

    calls = _stub_llm(monkeypatch)

    with standard_scope_cache():
        first = _evaluate(_settings(), DEFAULT_MODEL)
        again = _evaluate(_settings(), DEFAULT_MODEL)
        other = _evaluate(_settings(), PROFILE_MODEL)

    scope_calls = [model for schema, model, _, _ in calls if schema == "StandardAnswerScope"]
    assert scope_calls == [DEFAULT_MODEL, PROFILE_MODEL]
    assert first["judge_diagnostics"]["standard_scope_reused"] is False
    assert again["judge_diagnostics"]["standard_scope_reused"] is True
    assert other["judge_diagnostics"]["standard_scope_reused"] is False
