"""RAG の六指標評価。検索二・回答三・応答の判断一を独立して集計する。

必要な根拠は本文・位置、MRR は重複を除いた引用文書の順位、回答は意味上の正確性・網羅性・
主張の支持を測る。期待する応答の判断は明示入力だけを使う。前提不足は None とし平均に入れない。
会話の履歴・条件・区分・種類別の内訳を保ち、実行エラーと評価の未完了を合格にしない。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from time import perf_counter
from typing import Any, Protocol

from pr_backend_core.bounded_calls import (
    Attempt,
    BoundedCallAborted,
    BoundedCaller,
    BoundedCallFailed,
)
from pr_evaluation_scoring import (
    MetricAggregate,
    ThresholdFailure,
    count_reasons,
    evidence_matches,
    expected_decisions,
    overall_passed,
    reciprocal_rank,
    response_decision,
    rounded_mean,
    skips_retrieval,
    threshold_failures,
    unique_in_order,
)
from pydantic import ValidationError
from rag_engine.evaluation.answer_eval import standard_scope_cache

from app.clients.ai_calls import make_ai_caller
from app.clients.oracle import OracleClient
from app.config import (
    OCI_ENTERPRISE_AI_TIMEOUT_MAX_SECONDS,
    Settings,
    enterprise_ai_default_model_id,
    enterprise_ai_judge_model_id,
    get_settings,
)
from app.rag.answer_engine import answer_model_of, evaluate_answer_record
from app.rag.answer_failure import describe_answer_failure
from app.rag.answer_metrics import (
    REFERENCE_ONLY_METRICS,
    claims_supported,
    is_abstained_answer,
)
from app.rag.answer_threads import run_answer_in_thread
from app.rag.answer_timeout import (
    AnswerTimeoutError,
    answer_stage_label,
    answer_timeout_seconds,
    format_timeout_limit,
    run_answer_with_timeout,
)
from app.rag.audit import record_rag_search_audit
from app.rag.diagnostics import build_search_diagnostics
from app.rag.evaluation_handling import observed_outcome
from app.rag.observability import (
    SEARCH_METRIC_MODE,
    elapsed_ms,
    new_trace_id,
    record_evaluation_case,
    record_rag_request,
)
from app.rag.pipeline import ChatTurn, RagPipeline, SearchStageProgressCallback
from app.schemas.evaluation import (
    EVALUATION_METRIC_NAMES,
    EVALUATION_METRICS_VERSION,
    EVALUATION_REASONING_METRIC_NAMES,
    EVALUATION_REPORT_ONLY_METRIC_NAMES,
    EVALUATION_UNASSIGNED_SPLIT,
    EVALUATION_UNCATEGORIZED,
    EVALUATION_UNSPECIFIED_REASONING,
    EvaluationAnswerJudgement,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationCategorySummary,
    EvaluationEvidence,
    EvaluationJudgeDiagnostics,
    EvaluationMetrics,
    EvaluationRagOverrides,
    EvaluationReasoningSummary,
    EvaluationSplitSummary,
    EvaluationThresholdFailure,
    EvaluationThresholds,
    EvaluationTurnResult,
)
from app.schemas.search import (
    INCLUDE_SUPERSEDED_FILTER_KEY,
    RetrievedChunk,
    SearchRequest,
    SearchResponse,
)

EVALUATION_CASE_ERROR_MESSAGE = (
    "評価ケースの検索処理に失敗しました。trace_id で監査ログを確認してください。"
)
# 評価全体の上限（`time_budget_seconds`）に達して実行しなかったケースの error_type（#383）。
EVALUATION_TIME_BUDGET_ERROR_TYPE = "EvaluationTimeBudgetExceeded"
EVALUATION_TIME_BUDGET_MESSAGE_PREFIX = "評価全体の時間の上限に達したため、"
EVALUATION_TIME_BUDGET_MESSAGE_SUFFIX = "ケースを減らすか、分けて評価してください。"
# 標準回答による評価 1 件の時間の上限(保存済みの回答の評価の API と同じ。#304)。
ANSWER_JUDGE_TIMEOUT_SECONDS = OCI_ENTERPRISE_AI_TIMEOUT_MAX_SECONDS
ANSWER_JUDGE_UNAVAILABLE_MESSAGE = (
    "この回答には標準回答で評価するための記録がありません(安全ポリシーで止めた質問など)。"
)
ANSWER_JUDGE_TIMEOUT_MESSAGE = "標準回答による評価が時間内に終わりませんでした。"
ANSWER_JUDGE_ERROR_MESSAGE = "標準回答による評価を完了できませんでした。"
# 返答の往復で、業務ガイドの照合に足す前の質問の数
# （チャットの GUIDE_CONTEXT_TURNS と同じ。#1238 / #1284）。
EVALUATION_GUIDE_CONTEXT_TURNS = 3
logger = logging.getLogger(__name__)


class SearchPipeline(Protocol):
    """評価ランナーが必要とする検索 pipeline の最小インターフェース。"""

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: SearchStageProgressCallback | None = None,
    ) -> SearchResponse:
        """検索を実行する。

        `progress_callback` へ工程を通知する（時間切れになった工程の特定に使う）。
        """


class AnswerJudge(Protocol):
    """標準回答で回答を評価する(LLM を複数回呼ぶ)。

    戻り値は `evaluate_answer_record` の結果(status / passed / metrics / claim_checks /
    coverage_checks / requirement_coverage など。#680)。時間切れは TimeoutError を送出する。
    ``judge_model_id`` は判定に使うモデル(#3574。runner が回答を作ったモデルから解決した ID)。
    """

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
        """評価結果を返す。"""


class EvaluationProgressCallback(Protocol):
    """評価の進捗の通知先（評価 job の進捗の保存に使う。#390）。

    ケースを始める前に、終わったケースの数と今のケースの id を渡す。最後のケースの後は
    `current_case_id=None` で呼ぶ。
    """

    async def __call__(
        self,
        *,
        completed_cases: int,
        current_case_id: str | None,
    ) -> None:
        """進捗を受け取る。"""


async def judge_answer_with_standard(
    *,
    trace_id: str,
    evaluation_input: Mapping[str, object],
    standard_answer: str,
    citations: Sequence[RetrievedChunk],
    timeout_seconds: float,
    settings: Settings | None = None,
    judge_model_id: str | None = None,
) -> Mapping[str, object]:
    """回答を標準回答で評価し、結果を回答の記録にも保存する(保存は best-effort)。

    ``judge_model_id`` は判定のモデル(#3574。省略時は `evaluate_answer_record` が解決する)。

    時間切れのときは結果を捨てる(#304 と同じ)。評価の thread には同じ期限を渡し、時間切れの後は
    LLM を呼ばない(#1422)。
    """
    resolved = settings or get_settings()
    limit = max(0.001, timeout_seconds)
    evaluation = await asyncio.wait_for(
        run_answer_in_thread(
            evaluate_answer_record,
            evaluation_input,
            standard_answer,
            resolved,
            citations=citations,
            judge_model_id=judge_model_id,
            timeout_seconds=limit,
        ),
        timeout=limit,
    )
    saved = {
        **evaluation,
        "standard_answer": standard_answer,
        "evaluated_at": datetime.now(UTC).isoformat(),
    }
    try:
        # 回答の記録の画面から、指標ごとの値・主張ごとの判定を確かめられるようにする。
        await OracleClient(settings=resolved).save_answer_evaluation(trace_id, saved)
    except Exception as exc:  # noqa: BLE001 - 保存は補助。評価の結果は返す。
        logger.info(
            "evaluation_answer_judgement_save_skipped",
            extra={"error_type": type(exc).__name__},
        )
    return evaluation


# 集計する指標（閾値の判定に使う指標と、結果に出すだけの参考の集計。#1284）。
_AGGREGATED_METRIC_NAMES: tuple[str, ...] = (
    *EVALUATION_METRIC_NAMES,
    *EVALUATION_REPORT_ONLY_METRIC_NAMES,
)


def _aggregate() -> MetricAggregate:
    """指標ごとに、測れたケースの値を集める（platform の採点の部品。#3240）。"""
    return MetricAggregate(names=_AGGREGATED_METRIC_NAMES)


@dataclass(frozen=True)
class _TurnRun:
    """1 回の回答と、その質問・返答までに渡した条件（#1284）。"""

    response: SearchResponse
    conditions: Mapping[str, str] = field(default_factory=dict)


# (検索・回答の request, Settings, 業務ガイドの照合に足す前の質問) → 解決した request と Settings。
ProfileResolver = Callable[
    [SearchRequest, Settings, Sequence[str]], Awaitable[tuple[SearchRequest, Settings]]
]


async def _resolve_profile_context(
    request: SearchRequest, settings: Settings, guide_context: Sequence[str] = ()
) -> tuple[SearchRequest, Settings]:
    """検索・回答と同じ解決で、プロファイルの KB・回答の設定・業務ガイドを当てる（#1249）。

    返答の往復（#1284）では、チャットと同じく前の質問も業務ガイドの照合に使う。確認の質問は回答として
    測るため、検索の API と同じく ``interactive`` にしない（不明の条件は確認の質問になる）。
    """
    from app.api.routes.search import _resolve_query_context

    resolved, resolved_settings, _kb, _view = await _resolve_query_context(
        request, settings, guide_context=guide_context
    )
    return resolved, resolved_settings


def _case_queries(case: EvaluationCase) -> list[tuple[str, dict[str, str]]]:
    """最初の質問と返答を、送る順に（質問・返答, その発話で分かった条件）で返す（#1284）。"""
    return [
        (case.query, dict(case.conditions)),
        *((turn.reply, dict(turn.conditions)) for turn in case.turns),
    ]


def _case_filters(case: EvaluationCase, filters: Mapping[str, str] | None) -> dict[str, str]:
    """ケースの検索条件。旧版も検索するケース（#1366）は include_superseded=true を足す。"""
    case_filters = dict(filters or {})
    if case.include_superseded:
        case_filters[INCLUDE_SUPERSEDED_FILTER_KEY] = "true"
    return case_filters


class EvaluationRunner:
    """小規模な golden set を使って検索・根拠・回答の品質を評価する。"""

    def __init__(
        self,
        pipeline: SearchPipeline | None = None,
        settings: Settings | None = None,
        answer_judge: AnswerJudge | None = None,
        profile_resolver: ProfileResolver | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._pipeline = pipeline
        self._profile_resolver = profile_resolver or _resolve_profile_context
        # 既定の pipeline(実環境)では、標準回答のあるケースを LLM で評価する。
        # テストで pipeline を注入したときは、明示した judge だけを使う。
        self._answer_judge: AnswerJudge | None = answer_judge
        if answer_judge is None and pipeline is None:
            self._answer_judge = _settings_judge(self._settings)

    async def run(
        self,
        cases: list[EvaluationCase],
        top_k: int,
        filters: dict[str, str] | None = None,
        knowledge_base_ids: Sequence[str] | None = None,
        thresholds: EvaluationThresholds | None = None,
        rag_overrides: EvaluationRagOverrides | None = None,
        *,
        search_answer_profile_id: str | None = None,
        time_budget_seconds: float | None = None,
        progress: EvaluationProgressCallback | None = None,
    ) -> EvaluationMetrics:
        """評価ケースを実行し、集計指標を返す。

        1 ケースは回答生成の上限（`rag_answer_timeout_seconds`）で打ち切り、時間切れになった工程を
        ケースの結果に残して次のケースへ進む（#383）。`time_budget_seconds` を渡すと、評価全体を
        その秒数で打ち切る（同期の HTTP の待ちを超えないため）。上限に達したら実行中のケースを
        打ち切り、残りのケースは実行せずに失敗として記録する。`progress` には、ケースを始める
        前と最後のケースの後に進捗を渡す（評価 job。#390）。

        `search_answer_profile_id` を渡すと、ケースごとに検索・回答と同じ解決（参照 KB・回答の
        設定・用語・同義語・回答ルール・業務ガイド）をしてから回答する（#1249）。渡さなければ全体の既定（#301）。

        標準回答の比較範囲は、同じ評価の中（比較の pass を含む）で質問・標準回答・判定のモデルごとに
        1 回だけ固定する（#3570）。
        """
        with standard_scope_cache():
            return await self._run(
                cases,
                top_k,
                filters,
                knowledge_base_ids,
                thresholds,
                rag_overrides,
                search_answer_profile_id=search_answer_profile_id,
                time_budget_seconds=time_budget_seconds,
                progress=progress,
            )

    async def _run(
        self,
        cases: list[EvaluationCase],
        top_k: int,
        filters: dict[str, str] | None,
        knowledge_base_ids: Sequence[str] | None,
        thresholds: EvaluationThresholds | None,
        rag_overrides: EvaluationRagOverrides | None,
        *,
        search_answer_profile_id: str | None,
        time_budget_seconds: float | None,
        progress: EvaluationProgressCallback | None,
    ) -> EvaluationMetrics:
        deadline = _deadline(time_budget_seconds)
        effective_settings = evaluation_settings(self._settings, rag_overrides)
        pipeline = self._pipeline or RagPipeline(settings=effective_settings)

        concurrency = evaluation_concurrency(self._settings)
        caller = make_ai_caller(self._settings, "evaluation", max_concurrency=concurrency)
        results: list[EvaluationCaseResult | None] = [None] * len(cases)
        pending = iter(enumerate(cases))
        finished = 0

        async def worker() -> None:
            nonlocal finished
            for case_index, case in pending:
                if progress is not None:
                    try:
                        await progress(
                            completed_cases=finished,
                            current_case_id=case.id,
                        )
                    except asyncio.CancelledError:
                        # 取り消し・中断（job の行が RUNNING でなくなった）。ほかのケースも止める。
                        raise _EvaluationStopped from None
                results[case_index] = await self._run_case(
                    case,
                    caller=caller,
                    pipeline=pipeline,
                    effective_settings=effective_settings,
                    deadline=deadline,
                    top_k=top_k,
                    filters=filters,
                    knowledge_base_ids=knowledge_base_ids,
                    search_answer_profile_id=search_answer_profile_id,
                )
                finished += 1

        # ケースは上限つきの並行（`RAG_EVALUATION_CONCURRENCY`。#2004）で、評価ケースの順に
        # 始める。結果はケースの位置に書くため、並行でも順番は変わらない。1 ケースの中の往復と
        # 判定は順に行う（前の往復が次の質問の履歴になる）。
        workers = max(1, min(concurrency, len(cases)))
        try:
            async with asyncio.TaskGroup() as group:
                for _ in range(workers):
                    group.create_task(worker())
        except BaseExceptionGroup as group_error:
            if group_error.subgroup(_EvaluationStopped) is not None:
                raise asyncio.CancelledError from None
            raise _first_error(group_error) from None

        if progress is not None:
            await progress(
                completed_cases=len(cases),
                current_case_id=None,
            )
        case_results = [result for result in results if result is not None]
        return summarize_case_results(case_results, thresholds=thresholds)

    async def _run_case(
        self,
        case: EvaluationCase,
        *,
        caller: BoundedCaller,
        pipeline: Any,
        effective_settings: Settings,
        deadline: float | None,
        top_k: int,
        filters: dict[str, str] | None,
        knowledge_base_ids: Sequence[str] | None,
        search_answer_profile_id: str | None,
    ) -> EvaluationCaseResult:
        """1 ケース（最初の質問と確認の質問への返答の往復、判定）を実行して結果を返す。

        回答が流量の制限（429）で失敗したら、共通の実行器が全体で待って送り直す（#2004）。
        ほかの失敗はケースの失敗にする（今までと同じ）。権限・設定の誤り・割り当ての使い切りは
        ``BoundedCallAborted`` で評価の全体を止める。
        """
        trace_id = new_trace_id()
        remaining = _remaining_seconds(deadline)
        if remaining is not None and remaining <= 0:
            # 評価全体の上限に達した。残りのケースは pipeline を呼ばずに失敗として記録する。
            return _case_skipped_result(case=case, trace_id=trace_id)
        case_started_at = perf_counter()
        # 最初の質問と、確認の質問への返答（#1284）を順に送る。返答はチャットと同じく、
        # 前の往復を会話の履歴と業務ガイドの照合の文脈にし、分かった条件を足して渡す。
        turn_runs: list[_TurnRun] = []
        history: list[ChatTurn] = []
        prior_queries: list[str] = []
        conditions: dict[str, str] = {}
        error_result: EvaluationCaseResult | None = None
        # 回答に使った設定（プロファイルの pass はプロファイルの回答のモデルを当てた設定。#3574）。
        answer_settings = effective_settings
        for turn_index, (query, turn_conditions) in enumerate(_case_queries(case)):
            conditions = {**conditions, **turn_conditions}
            request = SearchRequest(
                query=query,
                top_k=top_k,
                filters=_case_filters(case, filters),
                knowledge_base_ids=list(knowledge_base_ids or []),
                conditions=dict(conditions),
            )
            turn_trace_id = trace_id if turn_index == 0 else new_trace_id()
            remaining = _remaining_seconds(deadline)
            if remaining is not None and remaining <= 0:
                # 往復の途中で評価全体の上限に達した。ケースは失敗にする。
                error_result = _case_skipped_result(
                    case=case, trace_id=turn_trace_id, partially_run=True
                )
                break
            case_limit = answer_timeout_seconds(effective_settings)
            limited_by_budget = remaining is not None and remaining < case_limit
            try:
                case_pipeline: Any = pipeline
                if search_answer_profile_id:
                    # プロファイルの解決（KB・回答の設定・業務ガイド）。
                    # 失敗はケースの失敗にする。
                    request, case_settings = await self._profile_resolver(
                        request.model_copy(
                            update={"search_answer_profile_id": search_answer_profile_id}
                        ),
                        effective_settings,
                        prior_queries[-EVALUATION_GUIDE_CONTEXT_TURNS:],
                    )
                    case_pipeline = self._pipeline or RagPipeline(settings=case_settings)
                    answer_settings = case_settings
                attempt_trace_ids = [turn_trace_id]

                async def answer_once(
                    attempt: Attempt,
                    *,
                    case_pipeline: Any = case_pipeline,
                    request: SearchRequest = request,
                    trace_ids: list[str] = attempt_trace_ids,
                    turn_history: tuple[ChatTurn, ...] = tuple(history),
                    limited: bool = limited_by_budget,
                ) -> SearchResponse:
                    # 流量の制限の後の送り直しは、新しい trace id で記録する（#2004）。
                    if attempt.number > 1:
                        trace_ids.append(new_trace_id())
                    run_case: Any = partial(case_pipeline.run, request, trace_ids[-1])
                    if turn_history:
                        # 返答はチャットと同じく、会話の履歴を踏まえて単独の質問に
                        # 書き換えてから答える。
                        run_case = partial(run_case, history=list(turn_history))
                    # 工程を記録する tracker は、3 番目の引数（progress_callback）として渡る。
                    return await run_answer_with_timeout(
                        run_case,
                        effective_settings,
                        timeout_seconds=_remaining_seconds(deadline) if limited else None,
                    )

                try:
                    response = await _call_unwrapped(caller, answer_once, label=case.id)
                finally:
                    turn_trace_id = attempt_trace_ids[-1]
            except BoundedCallAborted:
                raise
            except AnswerTimeoutError as exc:
                elapsed = elapsed_ms(case_started_at)
                record_evaluation_case(SEARCH_METRIC_MODE, "error", elapsed / 1000)
                _record_case_error_audit(
                    trace_id=turn_trace_id,
                    request=request,
                    elapsed=elapsed,
                    # 監査と結果の error_type は、従来どおり元の TimeoutError にする。
                    error=exc.original_error,
                    settings=effective_settings,
                    error_stage="timeout",
                )
                error_result = _case_error_result(
                    case=case,
                    trace_id=turn_trace_id,
                    elapsed=elapsed,
                    error=exc.original_error,
                    error_stage=exc.stage,
                    error_message=_case_timeout_message(
                        exc,
                        # 工程の中の時間切れ（LLM 1 回の timeout など）は、全体の上限ではない。
                        limited_by_budget=limited_by_budget and exc.timeout_seconds is not None,
                    ),
                )
                break
            except Exception as exc:
                elapsed = elapsed_ms(case_started_at)
                record_evaluation_case(SEARCH_METRIC_MODE, "error", elapsed / 1000)
                _record_case_error_audit(
                    trace_id=turn_trace_id,
                    request=request,
                    elapsed=elapsed,
                    error=exc,
                    settings=effective_settings,
                    error_stage="evaluation",
                )
                # LLM の応答が送り直しても未完了なら、理由と直し方の文と工程を残す（#2023）。
                failure = describe_answer_failure(exc, EVALUATION_CASE_ERROR_MESSAGE)
                error_result = _case_error_result(
                    case=case,
                    trace_id=turn_trace_id,
                    elapsed=elapsed,
                    error=exc,
                    error_stage=failure.stage,
                    error_message=failure.message,
                    error_type=failure.error_type,
                )
                break
            turn_runs.append(_TurnRun(response=response, conditions=dict(conditions)))
            history.extend(
                [
                    ChatTurn(role="USER", content=query),
                    ChatTurn(role="ASSISTANT", content=response.answer),
                ]
            )
            prior_queries.append(query)

        if error_result is not None:
            return error_result

        record_evaluation_case(
            SEARCH_METRIC_MODE,
            "success",
            elapsed_ms(case_started_at) / 1000,
        )
        final_response = turn_runs[-1].response
        judgement = await self._judge(
            case,
            final_response,
            deadline,
            caller,
            judge_model_id=_judge_model_id(self._settings, final_response, answer_settings),
        )
        return _case_result(case, final_response, judgement, turn_runs=turn_runs)

    async def _judge(
        self,
        case: EvaluationCase,
        response: SearchResponse,
        deadline: float | None,
        caller: BoundedCaller | None = None,
        *,
        judge_model_id: str,
    ) -> EvaluationAnswerJudgement | None:
        """標準回答のあるケースを LLM で評価する。標準回答が無ければ None。

        ``judge_model_id`` は判定のモデル(#3574)。判定を完了できなかったケースにも残す。
        """
        if not (
            case.standard_answer
            or case.required_answer_points
            or case.required_conditions
            or response.citations
        ):
            return None
        evaluation_input = response.evaluation_input or {
            "question": case.query,
            "answer_text": response.answer,
            "evidence_items": [
                {
                    "id": item.chunk_id,
                    "chunk_uid": item.chunk_id,
                    "source": item.file_name or item.document_id,
                    "text": item.text,
                }
                for item in response.citations
            ],
        }
        if self._answer_judge is None:
            return None
        remaining = _remaining_seconds(deadline)
        timeout = ANSWER_JUDGE_TIMEOUT_SECONDS if remaining is None else remaining
        timeout = min(timeout, ANSWER_JUDGE_TIMEOUT_SECONDS)
        if timeout <= 0:
            return EvaluationAnswerJudgement(
                status="timeout",
                message=ANSWER_JUDGE_TIMEOUT_MESSAGE,
                judge_model_id=judge_model_id,
            )
        judge = self._answer_judge
        standard_answer = "\n".join(
            filter(
                None,
                [
                    case.standard_answer or "",
                    *case.required_answer_points,
                    *case.required_conditions,
                ],
            )
        )

        async def judge_once(_attempt: Attempt) -> Mapping[str, object]:
            return await judge(
                trace_id=response.trace_id,
                evaluation_input=evaluation_input,
                standard_answer=standard_answer,
                citations=response.citations,
                timeout_seconds=timeout,
                judge_model_id=judge_model_id,
            )

        try:
            # 流量の制限（429）は、ケースの回答と同じ実行器で全体で待って送り直す（#2004）。
            evaluation = await _call_unwrapped(
                caller or make_ai_caller(self._settings, "evaluation"), judge_once, label=case.id
            )
        except BoundedCallAborted:
            raise
        except TimeoutError:
            return EvaluationAnswerJudgement(
                status="timeout",
                message=ANSWER_JUDGE_TIMEOUT_MESSAGE,
                judge_model_id=judge_model_id,
            )
        except Exception:  # noqa: BLE001 - 評価の失敗はケースの結果に残して続ける。
            return EvaluationAnswerJudgement(
                status="error",
                message=ANSWER_JUDGE_ERROR_MESSAGE,
                judge_model_id=judge_model_id,
            )
        summary = summarize_answer_judgement(evaluation).model_copy(
            update={"judge_model_id": judge_model_id}
        )
        if not standard_answer:
            return summary.model_copy(
                update={
                    "answer_correctness": None,
                    "requirement_coverage": None,
                    "missing_content": None,
                }
            )
        return summary


def _judge_model_id(settings: Settings, response: SearchResponse, answer_settings: Settings) -> str:
    """ケースの回答を判定するモデル(#3574)。

    回答を作ったモデルは回答の診断の ``models.llm.model_id``、無ければ回答に使った設定の既定の
    テキストモデル(プロファイルの pass はプロファイルの回答のモデル)。共通のモデル設定の既定の
    判定モデルで、判定のモデルに解決する(既定は既定のテキストモデル)。
    """
    answer_model = answer_model_of(response.diagnostics.answer) or enterprise_ai_default_model_id(
        answer_settings
    )
    return enterprise_ai_judge_model_id(settings, answer_model)


class _EvaluationStopped(Exception):
    """進捗の保存で、評価の取り消し・中断が分かった（並行のケースをすべて止める）。"""


def evaluation_concurrency(settings: Settings) -> int:
    """同時に実行するケースの数の上限（``RAG_EVALUATION_CONCURRENCY``。#2004）。"""
    return max(1, int(getattr(settings, "rag_evaluation_concurrency", 1) or 1))


async def _call_unwrapped[T](
    caller: BoundedCaller, fn: Callable[[Attempt], Awaitable[T]], *, label: str
) -> T:
    """実行器で呼ぶ。件の失敗は元の例外で出す（ケースの失敗の記録・監査を変えない）。"""
    try:
        return await caller.call(fn, label=label)
    except BoundedCallFailed as exc:
        cause = exc.__cause__
        if isinstance(cause, Exception):
            raise cause  # noqa: B904 - 元の例外の原因の連鎖を変えずに出し直す
        raise


def _first_error(group: BaseExceptionGroup) -> BaseException:
    """並行のケースの失敗のうち、評価を止めた失敗（中断を優先し、無ければ最初の失敗）。"""
    leaves: list[BaseException] = []
    stack: list[BaseException] = [group]
    while stack:
        current = stack.pop(0)
        if isinstance(current, BaseExceptionGroup):
            stack[:0] = list(current.exceptions)
        else:
            leaves.append(current)
    aborted = [error for error in leaves if isinstance(error, BoundedCallAborted)]
    return aborted[0] if aborted else leaves[0]


def _settings_judge(settings: Settings) -> AnswerJudge:
    """Settings を固定した既定の judge。"""

    async def judge(
        *,
        trace_id: str,
        evaluation_input: Mapping[str, object],
        standard_answer: str,
        citations: Sequence[RetrievedChunk],
        timeout_seconds: float,
        judge_model_id: str,
    ) -> Mapping[str, object]:
        return await judge_answer_with_standard(
            trace_id=trace_id,
            evaluation_input=evaluation_input,
            standard_answer=standard_answer,
            citations=citations,
            timeout_seconds=timeout_seconds,
            settings=settings,
            judge_model_id=judge_model_id,
        )

    return judge


def _judge_diagnostics(evaluation: Mapping[str, object]) -> EvaluationJudgeDiagnostics | None:
    """判定の診断（比較範囲の使い回し・再試行の回数）。記録が無い・形が違えば None。"""
    value = evaluation.get("judge_diagnostics")
    if not isinstance(value, Mapping):
        return None
    try:
        return EvaluationJudgeDiagnostics.model_validate(value)
    except ValidationError:
        return None


def summarize_answer_judgement(evaluation: Mapping[str, object]) -> EvaluationAnswerJudgement:
    """標準回答による評価の結果を、指標に使う要約へ変換する。"""
    status = str(evaluation.get("status") or "error")
    message = evaluation.get("message")
    diagnostics = _judge_diagnostics(evaluation)
    if status != "completed":
        return EvaluationAnswerJudgement(
            status=status,
            message=str(message) if message else ANSWER_JUDGE_ERROR_MESSAGE,
            judge_diagnostics=diagnostics,
        )
    claims = [
        claim for claim in _mapping_list(evaluation.get("claim_checks")) if claim.get("status")
    ]
    coverage_checks = _mapping_list(evaluation.get("coverage_checks"))
    coverage = _optional_float(evaluation.get("requirement_coverage"))
    passed = evaluation.get("passed")
    return EvaluationAnswerJudgement(
        status="completed",
        passed=passed if isinstance(passed, bool) else None,
        claims_supported=claims_supported(claims),
        answer_correctness=_optional_float(evaluation.get("answer_correctness")),
        claim_support_rate=_optional_float(evaluation.get("claim_support_rate")),
        requirement_coverage=(
            round(min(1.0, max(0.0, coverage)), 4) if coverage is not None else None
        ),
        missing_content=any(str(check.get("status")) == "missing" for check in coverage_checks),
        message=str(message) if message else None,
        judge_diagnostics=diagnostics,
    )


def evaluation_settings(
    settings: Settings,
    overrides: EvaluationRagOverrides | None,
) -> Settings:
    """評価に使う設定。評価の上書き（rag_overrides）を一時適用する。

    評価は検索・回答プロファイルを受け取らず、全体の既定で動く(#301)。回答は根拠付き回答(回答の記録を
    残す回答フロー)だけなので(#594)、回答エンジンの指定は要らない(#591 では固定していた)。
    """
    update: dict[str, object] = {}
    if overrides is not None:
        mapping = {
            "query_strategy": "rag_query_strategy",
            "answer_flow": "rag_answer_flow",
            "neighbor_child_count": "rag_neighbor_child_count",
            "rerank_enabled": "rag_rerank_enabled",
            "rrf_k": "rag_rrf_k",
            "context_group_max_chunks": "rag_context_group_max_chunks",
            "reference_expansion_enabled": "rag_reference_expansion_enabled",
            "reference_expansion_max_chunks": "rag_reference_expansion_max_chunks",
            "entity_expansion_enabled": "rag_entity_expansion_enabled",
            "entity_expansion_max_chunks": "rag_entity_expansion_max_chunks",
            "graph_expansion_enabled": "rag_graph_expansion_enabled",
            "graph_expansion_max_chunks": "rag_graph_expansion_max_chunks",
            "section_expansion_enabled": "rag_section_expansion_enabled",
            "section_expansion_max_levels": "rag_section_expansion_max_levels",
            "section_expansion_input_token_percent": (  # nosec B105 - 設定の名前
                "rag_section_expansion_input_token_percent"
            ),
            "max_anchors_per_document": "rag_max_anchors_per_document",
            "oracle_vector_target_accuracy": "oracle_vector_target_accuracy",
            "strategy_reasoning_effort": "rag_strategy_reasoning_effort",
            "judge_reasoning_effort": "rag_judge_reasoning_effort",
            "generation_reasoning_effort": "rag_generation_reasoning_effort",
            "audit_reasoning_effort": "rag_audit_reasoning_effort",
            "crag_compact_input": "rag_crag_compact_input",
            "crag_short_candidate_ids": "rag_crag_short_candidate_ids",
            "answer_audit_compact_reasons": "rag_answer_audit_compact_reasons",
            "answer_max_correction_rounds": "rag_answer_max_correction_rounds",
            "answer_audit_mode": "rag_answer_audit_mode",
            "section_skip_regrade_at_scope": "rag_section_skip_regrade_at_scope",
            "crag_skip_on_strong_signals": "rag_crag_skip_on_strong_signals",
            "crag_skip_min_rerank_score": "rag_crag_skip_min_rerank_score",
            "query_expansion_max_queries": "rag_query_expansion_max_queries",
        }
        update.update(
            {mapping[key]: value for key, value in overrides.model_dump(exclude_none=True).items()}
        )
    return settings.model_copy(update=update)


def summarize_case_results(
    case_results: Sequence[EvaluationCaseResult],
    *,
    thresholds: EvaluationThresholds | None = None,
) -> EvaluationMetrics:
    """ケースの結果を集計して評価の結果にする（評価ランナーと `score_case_answers` で共通）。

    指標は成功したケースのうち測れたものだけの平均。失敗したケースや、標準回答で評価できなかった
    ケースがあれば合格にしない。
    """
    results = list(case_results)
    aggregate = _aggregate()
    failure_reason_counts: dict[str, int] = {}
    error_count = 0
    judge_incomplete_count = 0
    for result in results:
        count_reasons(failure_reason_counts, result.failure_reasons)
        if result.status != "success":
            error_count += 1
            continue
        judgement = result.answer_evaluation
        if judgement is not None and judgement.status != "completed":
            judge_incomplete_count += 1
        _accumulate_case_metrics(aggregate, result)
    aggregate_values = aggregate.means()
    failures = _threshold_failures(thresholds, aggregate_values)
    return EvaluationMetrics(
        metrics_version=EVALUATION_METRICS_VERSION,
        decision_breakdown=decision_breakdown(results),
        case_count=len(results),
        error_count=error_count,
        category_breakdown=category_breakdown(results),
        split_breakdown=split_breakdown(results),
        reasoning_type_breakdown=reasoning_type_breakdown(results),
        hops_breakdown=hops_breakdown(results),
        passed=overall_passed(
            failures=failures,
            error_count=error_count,
            incomplete_count=judge_incomplete_count,
            measured_counts=aggregate.counts(),
            thresholds=_threshold_values(thresholds),
            values=aggregate_values,
        ),
        applied_thresholds=_threshold_values(thresholds),
        threshold_failures=[
            EvaluationThresholdFailure(
                metric=item.metric, actual=item.actual, threshold=item.threshold
            )
            for item in failures
        ],
        failure_reason_counts=failure_reason_counts,
        metric_case_counts=aggregate.counts(),
        case_results=results,
        **aggregate_values,
    )


def score_case_answers(
    case: EvaluationCase,
    answers: Sequence[tuple[SearchResponse, Mapping[str, str]]],
    judgement: EvaluationAnswerJudgement | None = None,
) -> EvaluationCaseResult:
    """得た RAG の回答を、評価ランナーと同じ採点にかける（ケースの採点の入口）。

    `answers` は最初の質問から順の（回答, その発話までに渡した既知の条件）。採点（対応・手順と別解・
    危険な回答・条件・正解の文書・必要な根拠）は `EvaluationRunner.run` のケースと同じ関数で行う。
    製品横断の評価（業務 Agent の Run の採点）は suite（`suite/evaluation`。#3240）が持ち、ここは
    RAG の回答（`SearchResponse`）だけを扱う。
    """
    if not answers:
        raise ValueError("採点する回答がありません。")
    runs = [
        _TurnRun(response=response, conditions=dict(conditions)) for response, conditions in answers
    ]
    return _case_result(case, runs[-1].response, judgement, turn_runs=runs)


def _expected_case_decisions(case: EvaluationCase) -> list[str]:
    return sorted(
        {
            decision
            for expectation in [case, *case.turns]
            for decision in expected_decisions(
                expectation.expected_response_decision, expectation.expected_outcomes
            )
        }
    )


def _case_result(
    case: EvaluationCase,
    response: SearchResponse,
    judgement: EvaluationAnswerJudgement | None,
    *,
    turn_runs: Sequence[_TurnRun] | None = None,
) -> EvaluationCaseResult:
    """1 ケースの回答から、測れる指標と失敗理由を求める。

    `response` は最後の回答。`turn_runs`（#1284）は最初の質問からの全部の回答で、省略したときは
    `response` だけの 1 往復とみなす。`screen_score`（#2402）は判定器も使った案内した画面の採点で、
    省略したときは決定的な判定だけで採点する（決められなければ未判定）。
    """
    runs = list(turn_runs) if turn_runs else [_TurnRun(response, dict(case.conditions))]
    # 同じ文書とみなす文書（同じ名前の文書。#2086）は、正解の文書の ID にそろえて数える。
    retrieved_ids = unique_in_order(
        [case.canonical_document_id(chunk.document_id) for chunk in response.citations]
    )
    relevant = set(case.relevant_document_ids)
    # 答えるべきでない質問(#301)は拒答の正しさだけを測る。正解の文書が無いケースは検索の
    # 指標の対象外(#591)。
    abstained = is_abstained(response)
    hits: list[str] = []
    rank: float | None = None
    if relevant and not _skips_retrieval(response):
        hits = [doc_id for doc_id in retrieved_ids if doc_id in relevant]
        rank = reciprocal_rank(retrieved_ids, relevant)

    evidence_recall, missing_evidence = _evidence_scores(case, response)
    observed = observed_outcome(response.diagnostics.answer, abstained=abstained)

    expectations_by_turn = [case, *case.turns]
    decisions = []
    turn_results = []
    case_decisions: set[str] = set()
    for index, (expectation, run) in enumerate(zip(expectations_by_turn, runs, strict=False)):
        expected = expected_decisions(
            expectation.expected_response_decision, expectation.expected_outcomes
        )
        actual = observed_outcome(
            run.response.diagnostics.answer, abstained=is_abstained(run.response)
        )
        correct = response_decision(actual.outcome) in expected if expected else None
        if correct is not None:
            decisions.append(correct)
            case_decisions.update(expected)
        turn_results.append(
            EvaluationTurnResult(
                turn=index,
                trace_id=run.response.trace_id,
                observed_outcome=actual.outcome,
                outcome_source=actual.source,
                response_decision_accuracy=_bool_value(correct),
                expected_decisions=sorted(expected),
                elapsed_ms=run.response.elapsed_ms,
            )
        )
    failure_reasons: list[str] = []
    if missing_evidence:
        failure_reasons.append("evidence_miss")
    if relevant and not hits and not _skips_retrieval(response):
        failure_reasons.append("retrieval_miss")
    if decisions and not all(decisions):
        failure_reasons.append("unexpected_handling")
    if judgement is not None:
        if judgement.status != "completed":
            failure_reasons.append("answer_evaluation_error")
        if judgement.answer_correctness is not None and judgement.answer_correctness < 1:
            failure_reasons.append("answer_failed")
        if judgement.missing_content:
            failure_reasons.append("missing_content")
        if judgement.claim_support_rate is not None and judgement.claim_support_rate < 1:
            failure_reasons.append("unsupported_claim")
        if judgement.claims_supported is None and judgement.status == "completed":
            failure_reasons.append("claim_unassessed")
    return EvaluationCaseResult(
        case_id=case.id,
        trace_id=response.trace_id,
        category=case.category,
        split=case.split,
        reasoning_type=case.reasoning_type,
        hops=case.hops,
        retrieved_document_ids=retrieved_ids,
        relevant_document_ids=list(case.relevant_document_ids),
        hit_document_ids=unique_in_order(hits),
        mrr=rank,
        requirement_coverage=judgement.requirement_coverage if judgement else None,
        claim_support_rate=judgement.claim_support_rate if judgement else None,
        answer_correctness=judgement.answer_correctness if judgement else None,
        response_decision_accuracy=_bool_value(all(decisions)) if decisions else None,
        expected_decisions=sorted(case_decisions),
        observed_outcome=observed.outcome,
        outcome_source=observed.source,
        required_evidence_recall=evidence_recall,
        missing_evidence=missing_evidence,
        turn_results=turn_results if case.turns else [],
        answer_evaluation=judgement,
        failure_reasons=list(failure_reasons),
        diagnostics=response.diagnostics,
        elapsed_ms=round(sum(run.response.elapsed_ms for run in runs), 3),
    )


def _evidence_scores(
    case: EvaluationCase, response: SearchResponse
) -> tuple[float | None, list[str]]:
    """必要な根拠の再現率と、取れなかった根拠の id（#1284）。

    根拠の無いケースと、検索をしない回答（確認の質問・人への引き継ぎで引用の無い回答）は対象外。
    """
    if not case.required_evidence or _skips_retrieval(response):
        return None, []
    missing = [
        evidence.id
        for evidence in case.required_evidence
        if not any(
            evidence_matches_chunk(
                evidence, chunk, document_id=case.canonical_document_id(chunk.document_id)
            )
            for chunk in response.citations
        )
    ]
    total = len(case.required_evidence)
    return (total - len(missing)) / total, missing


def evidence_matches_chunk(
    evidence: EvaluationEvidence, chunk: RetrievedChunk, *, document_id: str | None = None
) -> bool:
    """引用の 1 つの chunk が根拠の条件（文書・ページ・シート・行・章節・本文）をすべて満たすか。

    #2065・#2086。`document_id` は chunk の文書の ID の代わりに比べる ID（同じ文書とみなす文書を
    正解の文書の ID にそろえたもの）。照合は platform の採点の部品（#3240）。行の条件は MCP の根拠の
    場所と同じ行の番号の基準と行（`row_basis`・`row_start`・`row_end`）で照合する（#3547。suite の
    評価と同じ）。
    """
    # MCP の層は検索・回答の多くを読むので、使うときに読む（循環の import を避ける）。
    from app.mcp.tools import evidence_row_location

    return evidence_matches(
        evidence,
        document_id=document_id if document_id is not None else chunk.document_id,
        text=chunk.text,
        locator={**chunk.metadata, **evidence_row_location(dict(chunk.metadata))},
    )


def _explicit_outcome(response: SearchResponse) -> str | None:
    details = response.diagnostics.answer or {}
    outcome = details.get("outcome")
    return outcome if isinstance(outcome, str) and outcome else None


def _skips_retrieval(response: SearchResponse) -> bool:
    """確認の質問・人への引き継ぎで引用の無い回答（検索の指標の対象外）。"""
    return skips_retrieval(_explicit_outcome(response), has_citations=bool(response.citations))


def is_abstained(response: SearchResponse) -> bool:
    """回答が「資料から答えられない」旨だけか(拒答)を、回答の記録から判定する。

    回答の記録が対応（outcome。#1235）を持つときは insufficient_evidence だけを拒答とする（確認の
    質問・人への引き継ぎは拒答ではない。#1259）。持たない古い記録は本文・引用・不足の理由で判定する。
    """
    outcome = _explicit_outcome(response)
    if outcome is not None:
        return outcome == "insufficient_evidence"
    details = response.diagnostics.answer or {}
    return is_abstained_answer(
        response.answer, response.citations, str(details.get("insufficient_reason") or "")
    )


def _accumulate_case_metrics(aggregate: MetricAggregate, result: EvaluationCaseResult) -> None:
    """成功したケースの測れた指標を集計へ足す。"""
    aggregate.add("required_evidence_recall", result.required_evidence_recall)
    aggregate.add("mrr", result.mrr)
    aggregate.add("answer_correctness", result.answer_correctness)
    aggregate.add("response_decision_accuracy", result.response_decision_accuracy)
    judgement = result.answer_evaluation
    if judgement is not None and judgement.status == "completed":
        aggregate.add("claim_support_rate", judgement.claim_support_rate)
        aggregate.add("requirement_coverage", judgement.requirement_coverage)


def _deadline(time_budget_seconds: float | None) -> float | None:
    """評価全体の上限の期限（`perf_counter` の時刻）。上限がなければ None。"""
    return None if time_budget_seconds is None else perf_counter() + time_budget_seconds


def _remaining_seconds(deadline: float | None) -> float | None:
    """評価全体の上限までの残り秒数。上限がなければ None。"""
    return None if deadline is None else deadline - perf_counter()


def _case_timeout_message(error: AnswerTimeoutError, *, limited_by_budget: bool) -> str:
    """時間切れのケースの文言。時間切れになった工程を含め、query 本文は含めない（#383）。"""
    stage = answer_stage_label(error.stage)
    if limited_by_budget:
        return (
            EVALUATION_TIME_BUDGET_MESSAGE_PREFIX
            + f"評価ケースの回答生成を打ち切りました（時間切れになった工程: {stage}）。"
            + EVALUATION_TIME_BUDGET_MESSAGE_SUFFIX
        )
    limit = "時間内に"
    if error.timeout_seconds is not None:
        limit = f"上限の {format_timeout_limit(error.timeout_seconds)}以内に"
    return (
        f"評価ケースの回答生成が{limit}終わりませんでした（時間切れになった工程: {stage}）。"
        "trace_id で監査ログを確認してください。"
    )


def _case_error_result(
    *,
    case: EvaluationCase,
    trace_id: str,
    elapsed: float,
    error: Exception,
    error_stage: str | None = None,
    error_message: str = EVALUATION_CASE_ERROR_MESSAGE,
    error_type: str | None = None,
) -> EvaluationCaseResult:
    """評価 case の失敗を query 本文なしの診断結果に変換する。"""
    return EvaluationCaseResult(
        case_id=case.id,
        category=case.category,
        split=case.split,
        reasoning_type=case.reasoning_type,
        hops=case.hops,
        trace_id=trace_id,
        status="error",
        relevant_document_ids=list(case.relevant_document_ids),
        expected_decisions=_expected_case_decisions(case),
        failure_reasons=["case_error"],
        elapsed_ms=elapsed,
        error_type=error_type or type(error).__name__,
        error_stage=error_stage,
        error_message=error_message,
    )


def _case_skipped_result(
    *, case: EvaluationCase, trace_id: str, partially_run: bool = False
) -> EvaluationCaseResult:
    """評価全体の上限に達して実行しなかったケースを、失敗として記録する（#383）。

    `partially_run` は、複数往復のケースの途中の返答で上限に達したとき（#1284）。
    """
    skipped = (
        "このケースの残りの返答は実行していません。"
        if partially_run
        else "このケースは実行していません。"
    )
    return EvaluationCaseResult(
        case_id=case.id,
        category=case.category,
        split=case.split,
        reasoning_type=case.reasoning_type,
        hops=case.hops,
        trace_id=trace_id,
        status="error",
        relevant_document_ids=list(case.relevant_document_ids),
        expected_decisions=_expected_case_decisions(case),
        failure_reasons=["case_error"],
        elapsed_ms=0.0,
        error_type=EVALUATION_TIME_BUDGET_ERROR_TYPE,
        error_message=(
            EVALUATION_TIME_BUDGET_MESSAGE_PREFIX + skipped + EVALUATION_TIME_BUDGET_MESSAGE_SUFFIX
        ),
    )


def _record_case_error_audit(
    *,
    trace_id: str,
    request: SearchRequest,
    elapsed: float,
    error: Exception,
    settings: Settings,
    error_stage: str,
) -> None:
    """評価 runner 側で捕捉した case 失敗を RAG 監査へ残す。"""
    record_rag_request(SEARCH_METRIC_MODE, "error", elapsed / 1000, 0)
    diagnostics = build_search_diagnostics(
        request, settings=settings, retrieval_strategy_adapter="grounded"
    )
    record_rag_search_audit(
        trace_id=trace_id,
        outcome="error",
        query=request.query,
        filters=request.filters,
        retrieved_count=0,
        citations=[],
        elapsed_ms=elapsed,
        diagnostics=diagnostics,
        error=error,
        error_stage=error_stage,
    )


def _threshold_values(thresholds: EvaluationThresholds | None) -> dict[str, float]:
    """設定された最低閾値（指標 → 値）。"""
    return thresholds.model_dump(exclude_none=True) if thresholds is not None else {}


def _threshold_failures(
    thresholds: EvaluationThresholds | None,
    aggregate_values: Mapping[str, float | None],
) -> list[ThresholdFailure]:
    """設定された最低閾値を下回った aggregate metric（測れなかった指標は判定しない）。

    参考だけの指標（REFERENCE_ONLY_METRICS）は閾値を目安として表示し、合否に使わない（#711）。
    """
    return threshold_failures(
        _threshold_values(thresholds), aggregate_values, reference_only=REFERENCE_ONLY_METRICS
    )


def _mapping_list(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _optional_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _bool_value(value: bool | None) -> float | None:
    return None if value is None else (1.0 if value else 0.0)


def decision_breakdown(
    results: list[EvaluationCaseResult],
) -> dict[str, EvaluationReasoningSummary]:
    """期待する回答・確認・保留ごとの、測定可能なケースの内訳。"""
    result = {}
    for decision in ("answer", "clarify", "hold"):
        cases = [item for item in results if decision in item.expected_decisions]
        if not cases:
            continue
        values = [
            item.response_decision_accuracy
            for item in cases
            if item.response_decision_accuracy is not None
        ]
        result[decision] = EvaluationReasoningSummary(
            case_count=len(cases),
            error_count=sum(item.status == "error" for item in cases),
            metrics={"response_decision_accuracy": rounded_mean(values)},
            metric_case_counts={"response_decision_accuracy": len(values)},
        )
    return result


def category_breakdown(results: list[EvaluationCaseResult]) -> dict[str, EvaluationCategorySummary]:
    """ケース分類別の件数。指標は六指標の集計に統一する。"""
    if not any(item.category for item in results):
        return {}
    grouped: dict[str, list[EvaluationCaseResult]] = {}
    for item in results:
        grouped.setdefault(item.category or EVALUATION_UNCATEGORIZED, []).append(item)
    summaries = {}
    for name, members in grouped.items():
        aggregate = _aggregate()
        for item in members:
            if item.status == "success":
                _accumulate_case_metrics(aggregate, item)
        summaries[name] = EvaluationCategorySummary(
            case_count=len(members),
            error_count=sum(item.status == "error" for item in members),
            metrics=aggregate.means(),
            metric_case_counts=aggregate.counts(),
        )
    return summaries


def split_breakdown(results: list[EvaluationCaseResult]) -> dict[str, EvaluationSplitSummary]:
    """区分（dev / holdout）ごとの結果（#1284）。区分のあるケースが 1 つも無ければ空にする。

    指標は全体と同じく、成功したケースのうち測れたものだけの平均。区分の無いケースは unassigned。
    """
    if not any(result.split for result in results):
        return {}
    grouped: dict[str, list[EvaluationCaseResult]] = {}
    for result in results:
        grouped.setdefault(result.split or EVALUATION_UNASSIGNED_SPLIT, []).append(result)
    breakdown: dict[str, EvaluationSplitSummary] = {}
    for split, members in grouped.items():
        aggregate = _aggregate()
        failure_reason_counts: dict[str, int] = {}
        for result in members:
            if result.status == "success":
                _accumulate_case_metrics(aggregate, result)
            count_reasons(failure_reason_counts, result.failure_reasons)
        breakdown[split] = EvaluationSplitSummary(
            case_count=len(members),
            error_count=sum(1 for result in members if result.status != "success"),
            metrics=aggregate.means(),
            metric_case_counts=aggregate.counts(),
            failure_reason_counts=failure_reason_counts,
            reasoning_type_breakdown=reasoning_type_breakdown(members),
            hops_breakdown=hops_breakdown(members),
        )
    return breakdown


def reasoning_type_breakdown(
    results: Sequence[EvaluationCaseResult],
) -> dict[str, EvaluationReasoningSummary]:
    """多段の質問の種類ごとの結果（#1335）。種類のあるケースが 1 つも無ければ空にする。

    種類の無いケースは unspecified。
    """
    if not any(result.reasoning_type for result in results):
        return {}
    return _reasoning_breakdown(
        results, lambda result: result.reasoning_type or EVALUATION_UNSPECIFIED_REASONING
    )


def hops_breakdown(
    results: Sequence[EvaluationCaseResult],
) -> dict[str, EvaluationReasoningSummary]:
    """段の数ごとの結果（#1335。key は "1"・"2" などで、段の数の順）。

    段の数のあるケースが 1 つも無ければ空にする。段の数の無いケースは unspecified（最後）。
    """
    if not any(result.hops is not None for result in results):
        return {}
    ordered = sorted(results, key=lambda result: (result.hops is None, result.hops or 0))
    return _reasoning_breakdown(
        ordered,
        lambda result: (
            str(result.hops) if result.hops is not None else EVALUATION_UNSPECIFIED_REASONING
        ),
    )


def _reasoning_breakdown(
    results: Sequence[EvaluationCaseResult],
    key: Callable[[EvaluationCaseResult], str],
) -> dict[str, EvaluationReasoningSummary]:
    """ケースを key で分け、根拠と回答の指標を成功したケースのうち測れたものだけで求める。"""
    grouped: dict[str, list[EvaluationCaseResult]] = {}
    for result in results:
        grouped.setdefault(key(result), []).append(result)
    breakdown: dict[str, EvaluationReasoningSummary] = {}
    for name, members in grouped.items():
        aggregate = _aggregate()
        for result in members:
            if result.status == "success":
                _accumulate_case_metrics(aggregate, result)
        means = aggregate.means()
        counts = aggregate.counts()
        breakdown[name] = EvaluationReasoningSummary(
            case_count=len(members),
            error_count=sum(1 for result in members if result.status != "success"),
            metrics={metric: means[metric] for metric in EVALUATION_REASONING_METRIC_NAMES},
            metric_case_counts={
                metric: counts[metric] for metric in EVALUATION_REASONING_METRIC_NAMES
            },
        )
    return breakdown
