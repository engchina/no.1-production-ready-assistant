"""評価の案内した画面・目的外の割合・ナレッジベースとプロファイルの比較のテスト（#2402）。

画面の名前・回答・根拠は合成の内容（業務の内容は書かない）。
"""

from collections.abc import Mapping, Sequence
from typing import Any

import pytest
from pytest import MonkeyPatch

from app.main import app
from app.rag.answer_engine import adopted_goal_alignment
from app.rag.evaluation import EvaluationRunner, score_case_answers, summarize_case_results
from app.rag.evaluation_screens import (
    ScreenJudgement,
    answer_step_lines,
    cited_screen_names,
    llm_screen_judge,
    parse_screen_judgement,
    score_from_judgement,
    score_screens,
    screen_name_labels,
)
from app.rag.pipeline import SearchStageProgressCallback
from app.schemas.evaluation import (
    EvaluationCase,
    EvaluationMetrics,
    EvaluationRunRequest,
    EvaluationScreen,
)
from app.schemas.search import RetrievedChunk, SearchDiagnostics, SearchRequest, SearchResponse
from tests.support import AsgiTestClient

client = AsgiTestClient(app)

SCREEN_A = EvaluationScreen(name="画面A", aliases=["エー画面"])
SCREEN_B = EvaluationScreen(name="画面B")


def _label() -> str:
    labels = screen_name_labels()
    assert labels, "言語資源に画面名の見出しの語がある"
    return labels[0]


def _chunk(text: str, chunk_id: str = "doc-1:0") -> RetrievedChunk:
    return RetrievedChunk(document_id="doc-1", chunk_id=chunk_id, text=text, score=1.0)


def _response(
    answer: str,
    *,
    citations: Sequence[RetrievedChunk] = (),
    answer_details: Mapping[str, Any] | None = None,
    trace_id: str = "trace-1",
) -> SearchResponse:
    return SearchResponse(
        answer=answer,
        citations=list(citations) or [_chunk("本文")],
        trace_id=trace_id,
        guardrail_warnings=[],
        elapsed_ms=1.0,
        diagnostics=SearchDiagnostics(answer=dict(answer_details or {"outcome": "answered"})),
    )


def _case(*screens: EvaluationScreen, **kwargs: Any) -> EvaluationCase:
    return EvaluationCase(
        id=kwargs.pop("id", "case-1"),
        query="設定を変えるには？",
        relevant_document_ids=["doc-1"],
        required_answer_points=[screen.name for screen in screens],
        **kwargs,
    )


# ---- 画面名の読み取り ----------------------------------------------------------------------


def test_cited_screen_names_reads_single_and_listed_vision_lines() -> None:
    """図の説明の画面名の行（1 行の「 / 」区切りと、「- 」の箇条書き）を、出た順に重複なく読む。"""
    label = _label()
    texts = [
        f"■ 見えるもの\n{label}: 画面A / 画面C\nボタン: 保存",
        f"{label}:\n- 画面D\n- 画面A\nほかの行",
        "画面名の行が無い本文",
    ]

    assert cited_screen_names(texts, screen_name_labels()) == ["画面A", "画面C", "画面D"]


def test_answer_step_lines_picks_numbered_heading_and_bold_lines() -> None:
    answer = "概要の文。\n1. 画面A を開く\n## 画面B の操作\n**注意**: 画面C\n本文の画面D"

    assert answer_step_lines(answer) == ["1. 画面A を開く", "## 画面B の操作", "**注意**: 画面C"]


# ---- 決定的な採点 --------------------------------------------------------------------------


def test_score_screens_matches_expected_screen_by_alias_in_step_lines() -> None:
    score = score_screens("1. エー画面 で設定を変える", [SCREEN_A], [])

    assert score is not None
    assert score.screen_match is True
    assert score.wrong_screen is False
    assert score.matched == ["画面A"]
    assert score.judged_by == "deterministic"


def test_score_screens_counts_cited_screen_not_expected_as_wrong_screen() -> None:
    """根拠の画面名のうち、案内すべき画面でない画面を手順で案内したら別の画面にする。"""
    score = score_screens("1. 画面C を開く\n2. 画面A で保存する", [SCREEN_A], ["画面C"])

    assert score is not None
    assert score.screen_match is True
    assert score.wrong_screen is True
    assert score.guided == ["画面A", "画面C"]
    assert score.wrong == ["画面C"]


def test_score_screens_prefers_longer_similar_name() -> None:
    """似た名前の別の画面（「画面A一覧」）の中の「画面A」は、案内すべき画面の一致に数えない。"""
    score = score_screens("1. 画面A一覧 を開く", [SCREEN_A], ["画面A一覧"])

    assert score is not None
    assert score.screen_match is False
    assert score.missing == ["画面A"]
    assert score.wrong == ["画面A一覧"]


def test_score_screens_requires_every_expected_screen() -> None:
    """案内すべき画面が 2 つあり、1 つだけ案内したら不一致（手順の後の画面が抜けた）。"""
    score = score_screens("1. 画面A で申請する", [SCREEN_A, SCREEN_B], [])

    assert score is not None
    assert score.screen_match is False
    assert score.missing == ["画面B"]


def test_score_screens_falls_back_to_whole_answer_then_undetermined() -> None:
    """手順の見出しに無ければ全文で探し、全文にも無ければ決められない（None）。"""
    in_body = score_screens("設定は画面B から変えられます。", [SCREEN_B], [])
    assert in_body is not None and in_body.screen_match is True

    assert score_screens("設定を変えられます。", [SCREEN_B], []) is None


def test_score_from_judgement_maps_names_and_aliases() -> None:
    judgement = ScreenJudgement(guided_screens=["エー画面", "画面Z"], matched_expected=[])

    score = score_from_judgement([SCREEN_A], judgement)

    assert score.screen_match is True
    assert score.wrong == ["画面Z"]
    assert score.judged_by == "judge"


def test_parse_screen_judgement_rejects_non_json() -> None:
    assert parse_screen_judgement('前置き {"guided_screens": ["画面A"]} 後ろ').guided_screens == [
        "画面A"
    ]
    with pytest.raises(ValueError):
        parse_screen_judgement("JSON ではない")


async def test_llm_screen_judge_sends_schema_and_parses_response() -> None:
    """判定器は LLM に構造化出力の schema を渡し、応答を検証する（LLM はスタブ）。"""
    calls: list[dict[str, Any]] = []

    class StubLlm:
        async def generate(
            self,
            prompt: str,
            context: str,
            *,
            system_prompt: str | None = None,
            response_schema: Mapping[str, Any] | None = None,
            response_schema_name: str = "",
        ) -> str:
            calls.append({"context": context, "schema": response_schema})
            return '{"guided_screens": ["画面A"], "matched_expected": ["画面A"]}'

    judge = llm_screen_judge(StubLlm())
    judgement = await judge(answer="手順の文", expected=[SCREEN_A], candidates=["画面C"])

    assert judgement.matched_expected == ["画面A"]
    assert calls and calls[0]["schema"] and "画面C" in calls[0]["context"]


# ---- ケースの採点と集計 --------------------------------------------------------------------


def test_case_scoring_and_summary_report_screen_and_off_target_rates() -> None:
    """画面の要点も意味上の網羅性へ統合し、独立した画面指標を出さない。"""
    from app.schemas.evaluation import EvaluationAnswerJudgement

    matched = score_case_answers(
        _case(SCREEN_A, id="matched"),
        [(_response("画面Aで変更する"), {})],
        EvaluationAnswerJudgement(
            status="completed", answer_correctness=1, requirement_coverage=1, claim_support_rate=1
        ),
    )
    wrong = score_case_answers(
        _case(SCREEN_A, id="wrong"),
        [(_response("画面Cで変更する"), {})],
        EvaluationAnswerJudgement(
            status="completed", answer_correctness=0, requirement_coverage=0, claim_support_rate=0
        ),
    )
    unmeasured = score_case_answers(_case(id="no-points"), [(_response("変更する"), {})])
    assert matched.requirement_coverage == 1
    assert wrong.requirement_coverage == 0
    assert wrong.answer_correctness == 0
    assert "answer_failed" in wrong.failure_reasons
    assert unmeasured.requirement_coverage is None
    metrics = summarize_case_results([matched, wrong, unmeasured])
    assert metrics.requirement_coverage == 0.5
    assert metrics.answer_correctness == 0.5
    assert metrics.claim_support_rate == 0.5
    assert metrics.metric_case_counts["requirement_coverage"] == 2
    assert (
        not {"screen_match_rate", "wrong_screen_rate", "off_target_rate"}
        & metrics.model_dump().keys()
    )


def test_case_scoring_refusal_is_not_matched_and_clarification_is_not_measured() -> None:
    refused = score_case_answers(
        _case(SCREEN_A, expected_response_decision="answer"),
        [(_response("確認できません", answer_details={"outcome": "insufficient_evidence"}), {})],
    )
    clarification = SearchResponse(
        answer="どちらの場合ですか？",
        citations=[],
        trace_id="trace-2",
        elapsed_ms=1,
        diagnostics=SearchDiagnostics(answer={"outcome": "needs_clarification"}),
    )
    asked = score_case_answers(
        _case(SCREEN_A, expected_response_decision="clarify"), [(clarification, {})]
    )
    assert refused.response_decision_accuracy == 0
    assert "unexpected_handling" in refused.failure_reasons
    assert asked.response_decision_accuracy == 1
    assert asked.requirement_coverage is None


def test_case_without_judge_marks_undetermined_answer_unresolved() -> None:
    result = score_case_answers(_case(SCREEN_A), [(_response("設定を変えます。"), {})])
    assert result.requirement_coverage is None
    assert result.answer_correctness is None
    assert result.claim_support_rate is None


class _ScreenPipeline:
    def __init__(self, answer: str) -> None:
        self.answer = answer

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: SearchStageProgressCallback | None = None,
    ) -> SearchResponse:
        del request, progress_callback
        return _response(self.answer, trace_id=trace_id or "trace")


async def test_runner_calls_screen_judge_only_when_undetermined() -> None:
    """文字列一致の近道を使わず、必須要点は意味上の評価に渡す。"""
    calls = []

    async def judge(**kwargs: Any) -> Mapping[str, Any]:
        calls.append(kwargs)
        return {
            "status": "completed",
            "answer_correctness": 1,
            "requirement_coverage": 1,
            "claim_support_rate": 1,
        }

    for answer in ("設定を開いて変更します。", "画面Aで変更します。"):
        metrics = await EvaluationRunner(pipeline=_ScreenPipeline(answer), answer_judge=judge).run(
            cases=[_case(SCREEN_A)], top_k=5
        )
        assert metrics.case_results[0].requirement_coverage == 1
        assert metrics.case_results[0].answer_correctness == 1
    assert len(calls) == 2
    assert all(call["standard_answer"] == "画面A" for call in calls)


async def test_runner_keeps_case_when_screen_judge_fails() -> None:
    async def judge(**_: object) -> Mapping[str, Any]:
        raise RuntimeError("judge down")

    metrics = await EvaluationRunner(
        pipeline=_ScreenPipeline("設定を開いて変更します。"), answer_judge=judge
    ).run(cases=[_case(SCREEN_A)], top_k=5)
    result = metrics.case_results[0]
    assert result.status == "success"
    assert result.requirement_coverage is None
    assert result.answer_evaluation is not None and result.answer_evaluation.status == "error"
    assert result.failure_reasons == ["answer_evaluation_error"]
    assert metrics.passed is False


# ---- 回答の監査の目的との合い方 --------------------------------------------------------------


def test_adopted_goal_alignment_uses_last_accepted_round() -> None:
    trace = {
        "rounds": [
            {"accepted": True, "audit": {"goal_alignment": "off_target"}},
            {"accepted": True, "audit": {"goal_alignment": "aligned"}},
            {"accepted": False, "audit": {"goal_alignment": "partial"}},
        ]
    }

    assert adopted_goal_alignment(trace) == "aligned"
    assert adopted_goal_alignment({"rounds": [{"accepted": True, "audit": None}]}) is None
    assert adopted_goal_alignment(None) is None


# ---- 案内すべき画面のスキーマ ----------------------------------------------------------------


def test_screen_schema_cleans_aliases_and_rejects_blank_name() -> None:
    screen = EvaluationScreen(name=" 画面A ", aliases=[" エー画面 ", "", "エー画面"])
    assert screen.name == "画面A" and screen.aliases == ["エー画面"]
    with pytest.raises(ValueError):
        EvaluationScreen(name="  ")


def test_run_request_compares_distinct_profiles_and_rejects_same_profile() -> None:
    request = EvaluationRunRequest(
        cases=[_case()],
        search_answer_profile_id=" profile-1 ",
        compare_search_answer_profile_id=" profile-2 ",
    )
    assert request.search_answer_profile_id == "profile-1"
    assert request.compare_search_answer_profile_id == "profile-2"
    with pytest.raises(ValueError, match="別にしてください"):
        EvaluationRunRequest(
            cases=[_case()],
            search_answer_profile_id="profile-1",
            compare_search_answer_profile_id=" profile-1 ",
        )


# ---- プロファイル同士の比較 --------------------------------------------------------


def test_compare_api_runs_both_profiles(monkeypatch: MonkeyPatch) -> None:
    """比べるプロファイルを指定すると、それぞれのプロファイルで 2 回流して並べる。"""
    runs: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []

    class FakeRunner:
        async def run(self, **kwargs: Any) -> EvaluationMetrics:
            runs.append(kwargs)
            rate = 0.5 if kwargs["search_answer_profile_id"] == "profile-1" else 0.25
            return EvaluationMetrics(case_count=1, response_decision_accuracy=rate)

    class FakeOracleClient:
        async def save_evaluation_artifact(self, artifact: dict[str, Any]) -> str:
            artifacts.append(artifact)
            return "eval-1"

    monkeypatch.setattr("app.api.routes.evaluation.EvaluationRunner", FakeRunner)
    monkeypatch.setattr("app.api.routes.evaluation.OracleClient", FakeOracleClient)

    response = client.post(
        "/api/evaluation/run",
        json={
            "cases": [{"id": "case-1", "query": "設定を変えるには？"}],
            "search_answer_profile_id": "profile-base",
            "compare_search_answer_profile_id": "profile-1",
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["response_decision_accuracy"] == 0.25
    assert data["comparison"]["search_answer_profile_id"] == "profile-1"
    assert data["comparison"]["metrics"]["response_decision_accuracy"] == 0.5
    assert [run["search_answer_profile_id"] for run in runs] == ["profile-base", "profile-1"]
    assert runs[0]["knowledge_base_ids"] == []
    # プロファイルの範囲は、プロファイルの参照 KB で決める（KB の指定は渡さない）。
    assert runs[1]["knowledge_base_ids"] == []
    assert "knowledge_base_id" not in runs[1]["filters"]
    assert artifacts[0]["request_summary"]["compare_search_answer_profile_id"] == "profile-1"


async def test_compare_progress_counts_both_passes(monkeypatch: MonkeyPatch) -> None:
    """2 回流す評価の進捗は、job 全体（ケース × 2）の数で通知する。"""
    from app.api.routes import evaluation as evaluation_route

    seen: list[tuple[int, str | None]] = []

    async def progress(*, completed_cases: int, current_case_id: str | None) -> None:
        seen.append((completed_cases, current_case_id))

    class FakeRunner:
        async def run(self, **kwargs: Any) -> EvaluationMetrics:
            report = kwargs["progress"]
            await report(completed_cases=0, current_case_id="case-1")
            await report(completed_cases=1, current_case_id=None)
            return EvaluationMetrics(case_count=1)

    class FakeOracleClient:
        async def save_evaluation_artifact(self, artifact: dict[str, Any]) -> str:
            del artifact
            return "eval-1"

    monkeypatch.setattr(evaluation_route, "EvaluationRunner", FakeRunner)
    monkeypatch.setattr(evaluation_route, "OracleClient", FakeOracleClient)
    request = EvaluationRunRequest(
        cases=[_case()], knowledge_base_ids=["kb-1"], compare_search_answer_profile_id="p-1"
    )

    await evaluation_route._execute_run(request, time_budget_seconds=60.0, progress=progress)

    assert seen == [(0, "case-1"), (1, "case-1"), (2, None)]


async def test_compare_passes_share_the_standard_scope_record(monkeypatch: MonkeyPatch) -> None:
    """2 回流す評価（#2402）の 2 つの pass は、標準回答の比較範囲の記録を共有する（#3570）。"""
    from rag_engine.evaluation.answer_eval import StandardScopeCache, standard_scope_cache

    from app.api.routes import evaluation as evaluation_route

    seen: list[StandardScopeCache] = []

    class FakeRunner:
        async def run(self, **kwargs: Any) -> EvaluationMetrics:
            del kwargs
            with standard_scope_cache() as cache:
                seen.append(cache)
            return EvaluationMetrics(case_count=1)

    class FakeOracleClient:
        async def save_evaluation_artifact(self, artifact: dict[str, Any]) -> str:
            del artifact
            return "eval-1"

    monkeypatch.setattr(evaluation_route, "EvaluationRunner", FakeRunner)
    monkeypatch.setattr(evaluation_route, "OracleClient", FakeOracleClient)
    request = EvaluationRunRequest(
        cases=[_case()], knowledge_base_ids=["kb-1"], compare_search_answer_profile_id="p-1"
    )

    await evaluation_route._execute_run(request, time_budget_seconds=60.0)

    assert len(seen) == 2 and seen[0] is seen[1]
