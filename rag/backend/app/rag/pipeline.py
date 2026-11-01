"""検索 RAG パイプライン: 会話の履歴での質問の書き換え -> 回答フロー -> 回答の記録の保存。

回答は回答フロー(``AnswerEngine``)だけで行う(#594)。旧 standard の回答エンジン
(検索モード・根拠確認・回答スタイル・高度な検索)は #595 で削除した。
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from time import perf_counter, time
from typing import Any, Literal, cast

from pr_backend_core.chat_history import (
    ChatHistoryBudget,
    ChatHistoryTurn,
    build_chat_history,
)
from pr_backend_core.logging import safe_error_code, safe_exception_fields
from pr_backend_core.observability.genai import record_genai_usage
from pr_backend_core.observability.model_purpose import async_call_with_model_purpose
from pr_backend_core.observability.tracing import (
    ObservedSpan,
    SpanDisplay,
    finish_operation_trace,
    observed_span,
    operation_trace,
)
from pydantic import ValidationError
from rag_engine.evaluation.answer_validation import MAX_PREMISES
from rag_engine.resources.deadline import current_deadline
from rag_engine.resources.runtime import resolve_prompt
from rag_engine.resources.tracing import CallObservation, bind_trace_observer

from app.clients.oci_enterprise_ai import OciEnterpriseAiClient
from app.clients.oci_genai import OciGenAiClient
from app.clients.oracle import OracleClient
from app.clients.oracle_read_stats import track_db_calls
from app.config import Settings, get_settings
from app.rag.answer_engine import ANSWER_ENGINE, AnswerEngine, AnswerScope, answer_step_stage
from app.rag.answer_notice import AnswerNoticeSource, compose_rag_answer_notice
from app.rag.answer_route import answer_route
from app.rag.audit import record_rag_search_audit
from app.rag.chat_question_token import context_digest
from app.rag.degraded_steps import DegradedSteps, ReportDegraded, not_recorded
from app.rag.diagnostics import build_search_diagnostics
from app.rag.extraction_field_adapter import load_field_schema, resolve_field_definitions
from app.rag.field_filter_reader import read_field_conditions
from app.rag.history_question import HISTORY_REWRITE_SYSTEM_PROMPT as HISTORY_REWRITE_SYSTEM_PROMPT
from app.rag.history_question import HistoryConfirmation, HistoryRewrite
from app.rag.observability import (
    SEARCH_METRIC_MODE,
    elapsed_ms,
    new_trace_id,
    record_answer_route,
    record_rag_request,
    record_rag_stage,
    record_trace_span,
)
from app.rag.question_understanding import profile_language_scope
from app.rag.run_history import RunRecorder
from app.rag.span_display import (
    ANSWER_STEP_FAILED_STATUS,
    ANSWER_STEP_KIND,
    PIPELINE_STEP_KINDS,
    answer_step_details,
    answer_step_params,
    failure_details,
)
from app.rag.step_details import (
    field_filter_details,
    history_rewrite_details,
    step_details_model,
)
from app.rag.stored_answer import stored_citations, warn_if_large_answer_record
from app.rag.support_guide_evidence import with_evidence_conditions
from app.rag.support_guide_runtime import (
    GUIDE_PREVIEW_KEY,
    apply_guide_to_diagnostics,
    guide_short_circuit_outcome,
)
from app.rag.trace_store import AnswerTrace, operation_trace_parent
from app.schemas.common import JsonValue
from app.schemas.search import (
    ExtractionFieldCondition,
    RetrievedChunk,
    SearchRequest,
    SearchResponse,
    StorageFailure,
    StorageFailureStep,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SearchStageProgress:
    """工程の開始・終了の低機密の知らせ（時間切れの工程・MCP の進み具合・失敗の工程に使う）。

    画面の処理の経過は工程の span（表示用のデータを付けた trace。#3226）で、この知らせは使わない。
    """

    trace_id: str
    stage: str
    outcome: str
    elapsed_ms: float
    attributes: Mapping[str, object]
    occurred_at_ms: float = field(default_factory=lambda: time() * 1000)


type SearchStageProgressCallback = Callable[[SearchStageProgress], Awaitable[None]]

# 回答の記録の保存の工程（#2342）。失敗しても回答は返し、処理の経過で失敗にする。
ANSWER_RECORD_SAVE_STAGE: StorageFailureStep = "answer_record_save"
# 回答を作らない RAG 検索の全体の工程（質問の理解から根拠の検索まで。#3506）。KB の検索テストの
# 「retrieval」（回答フローを通らない検索だけ）とは分ける。
EVIDENCE_STAGE = "evidence"


def storage_failure(step: StorageFailureStep, exc: BaseException) -> StorageFailure:
    """保存できなかった記録（例外の型とコードだけ。本文・業務データは入れない。#2342）。"""
    code = safe_error_code(exc)
    return StorageFailure(
        step=step, error_type=type(exc).__name__, error_code=str(code) if code is not None else None
    )


@dataclass(frozen=True)
class SearchTokenDelta:
    """SSE 用の回答 token/chunk delta。"""

    trace_id: str
    text: str


type SearchTokenCallback = Callable[[SearchTokenDelta], Awaitable[None]]


@dataclass(frozen=True)
class ChatTurn:
    """会話履歴の 1 ターン。最新の質問を単独の質問へ書き換えるときにだけ使う。"""

    role: str  # "USER" | "ASSISTANT"
    content: str
    confirmed_facts: tuple[HistoryConfirmation, ...] = ()
    confirmation_resets: tuple[str, ...] = ()


def history_confirmation_facts(history: Sequence[ChatTurn]) -> dict[str, HistoryConfirmation]:
    """同じ確認の質問は最新の回答だけを候補とし、古い値を再採用しない。"""
    latest: dict[str, HistoryConfirmation] = {}
    for turn in history:
        if turn.role == "USER":
            for question in turn.confirmation_resets:
                latest.pop(question, None)
            for fact in turn.confirmed_facts:
                latest.pop(fact.key or fact.question, None)
                latest[fact.key or fact.question] = fact
    return {fact.id: fact for fact in list(latest.values())[-MAX_PREMISES:]}


def scope_with_history_confirmations(
    scope: AnswerScope | None, facts: Sequence[HistoryConfirmation]
) -> AnswerScope | None:
    """本輪で採用した履歴回答を生成と最終照合へ同じ組で渡す。本輪の明示回答を優先する。"""
    if not facts:
        return scope
    base = scope or AnswerScope(context="")
    current_questions = {question for question, _answer in base.confirmed_premises}
    current_keys = {context_digest(question) for question in current_questions}
    inherited = [
        (fact.question, fact.answer)
        for fact in facts
        if fact.question not in current_questions and fact.key not in current_keys
    ]
    if not inherited:
        return scope
    context = "既に確認した利用者の回答（データ）:\n" + json.dumps(
        [{"question": question, "answer": answer} for question, answer in inherited],
        ensure_ascii=False,
    )
    return replace(
        base,
        context="\n".join(filter(None, (base.context, context))),
        confirmed_premises=tuple([*inherited, *base.confirmed_premises]),
        confirmed_premise_sources=tuple(
            [
                *((question, answer, "user") for question, answer in inherited),
                *base.confirmed_premise_sources,
            ]
        ),
    )


def chat_history_turns(history: Sequence[ChatTurn]) -> list[ChatHistoryTurn]:
    """質問と回答のメッセージの並びを、前の往復（質問と回答。古い順）にまとめる。

    回答の無い質問（回答の失敗・遮断）は質問だけのターンにする。
    """
    pairs: list[list[str]] = []
    for turn in history:
        content = turn.content.strip()
        if not content:
            continue
        if turn.role == "USER" or not pairs or pairs[-1][1]:
            pairs.append([content, ""] if turn.role == "USER" else ["", content])
        else:
            pairs[-1][1] = content
    return [
        ChatHistoryTurn.of(("question", question), ("answer", answer)) for question, answer in pairs
    ]


def _format_chat_history(history: Sequence[ChatTurn], budget: ChatHistoryBudget) -> str:
    """前の往復を、共通の予算で詰めた未信頼のデータの文にする（3 製品共通。#1818）。

    新しい往復から順に共通の予算（`PLATFORM_CHAT_HISTORY_*`）に入るだけ入れ、古い往復を落とす。
    利用者の発話や過去の回答に `</turn></conversation_history>` のような文字列があっても、
    囲みを閉じて外側へ指示を書けないよう、タグに見える `<` を無害化する（#277）。
    """
    return build_chat_history(chat_history_turns(history), budget)


def _engine_cancelled() -> bool:
    deadline = current_deadline()
    return bool(deadline is not None and deadline.cancelled)


class _EngineStageObservation:
    """回答フローの工程の span の観測。工程の表示用のデータを span に付ける（#3226）。"""

    def __init__(self, span: ObservedSpan) -> None:
        self._span = span
        self._values: dict[str, object] = {}

    def request(self, value: object, *, schema: object = None) -> None:
        pass

    def response(self, value: object) -> None:
        pass

    def usage(self, value: object) -> None:
        pass

    def display(
        self,
        kind: str | None = None,
        params: Mapping[str, object] | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self._values.update(params or {})
        failed = self._values.get("status") == ANSWER_STEP_FAILED_STATUS
        self._span.set_display(
            kind=kind,
            params=answer_step_params(self._values),
            details=(
                answer_step_details(details, failed=failed)
                if self._span.call_content_enabled and (details is not None or failed)
                else None
            ),
        )


@contextmanager
def _observe_engine_call(operation: str, model: str) -> Iterator[CallObservation]:
    if operation == "stage":
        # 工程の span は開いたときから工程の種類と工程名を持つ（実行中の行の名前。#3226）。
        with observed_span(
            "rag.engine.stage",
            attributes={"pr.stage": model},
            cancellation_check=_engine_cancelled,
            display=SpanDisplay(kind=ANSWER_STEP_KIND, params={"name": model}),
        ) as stage_span:
            yield _EngineStageObservation(stage_span)
        return
    with observed_span(
        f"gen_ai.{operation}",
        kind="client",
        cancellation_check=_engine_cancelled,
        attributes={
            "gen_ai.operation.name": operation,
            "gen_ai.provider.name": "oracle",
            "gen_ai.request.model": model,
        },
    ) as span:

        class Observation:
            capture_enabled = span.call_content_enabled

            def request(self, value: object, *, schema: object = None) -> None:
                span.set_call_input(lambda: value, schema=schema, restricted=True)

            def response(self, value: object) -> None:
                span.set_call_output(lambda: value)

            def usage(self, value: object) -> None:
                record_genai_usage(span, value)

            def display(
                self,
                kind: str | None = None,
                params: Mapping[str, object] | None = None,
                details: Mapping[str, object] | None = None,
            ) -> None:
                if operation == "rerank":
                    display_details = (
                        answer_step_details(details)
                        if details is not None and span.call_content_enabled
                        else None
                    )
                    if (
                        display_details
                        and span.display
                        and span.display.details
                        and span.display.details.call
                    ):
                        display_details = display_details.model_copy(
                            update={"call": span.display.details.call}
                        )
                    span.set_display(kind=kind, params=params, details=display_details)

        yield Observation()


class RagPipeline:
    """回答フローで検索・回答する RAG パイプライン。

    回答記録・質問履歴・会話履歴による質問の書き換え・工程の計測を
    ここで行い、検索と回答の生成は ``AnswerEngine`` に任せる。
    """

    def __init__(
        self,
        genai: OciGenAiClient | None = None,
        oracle: OracleClient | None = None,
        llm: OciEnterpriseAiClient | None = None,
        settings: Settings | None = None,
        *,
        answer_model_id: str | None = None,
        approved_faq: tuple[str, str] | None = None,
        scope: AnswerScope | None = None,
        run_recorder: RunRecorder | None = None,
        answer_notice: AnswerNoticeSource | None = None,
        attachment_conversation_id: str | None = None,
        history_question_prepared: bool = False,
        caller_queries: Sequence[str] | None = None,
        complete_evidence: bool = True,
    ) -> None:
        self._settings = settings or get_settings()
        if not self._settings.rag_support_guide_enabled:
            self._settings = self._settings.model_copy(update={"rag_support_guide": {}})
        self._genai = genai or OciGenAiClient(settings=self._settings)
        self._oracle = oracle or OracleClient(settings=self._settings)
        self._llm = llm or OciEnterpriseAiClient(settings=self._settings)
        # 回答フローの回答のモデル。チャットのモデル比較で ``llm`` と同じモデルを渡す(#593)。
        # None は既定のモデル。
        self._answer_model_id = answer_model_id or None
        # 利用者が選んだ類似の承認済み FAQ(質問・承認済みの回答。チャット。#684)。
        self._approved_faq = approved_faq
        # 利用者が確認の質問で選んだ条件と対象範囲(#717)。
        self._scope = scope
        # 実行履歴(#1890)。チャットと RAG 検索の画面の実行だけが渡す(品質評価・MCP は渡さない)。
        if run_recorder is not None and run_recorder.store is None:
            from app.clients.oracle_run_history import RunHistoryStore

            run_recorder.store = RunHistoryStore(self._oracle)
        self._run_recorder = run_recorder
        # 回答の注意書きの文（#2405）。チャットと RAG 検索（MCP を含む）の経路だけが渡す
        # （品質評価・確かめのスクリプトは渡さない）。
        self._answer_notice = answer_notice
        # チャットの会話（#2517）。会話の添付の節・行を、質問の根拠の候補として検索の結果に足す。
        self._attachment_conversation_id = attachment_conversation_id
        self._history_question_prepared = history_question_prepared
        self._selected_history_facts: tuple[HistoryConfirmation, ...] = ()
        # 呼び出し元（業務 Agent）が作った検索文（#2651）。None 以外なら回答を作らない検索だけに
        # 使い、質問に応じた推論の LLM（抽出項目の条件の読み取り・質問拡張など）を呼ばない。
        self._caller_queries = tuple(caller_queries) if caller_queries is not None else None
        # 回答を作らない検索で根拠の補いまでするか（#3506。`AnswerEngine` の同名の引数）。False は
        # 業務 Agent の根拠の取得（MCP の rag_retrieve_evidence）だけが渡す。
        self._complete_evidence = complete_evidence
        # 処理の経過の記録（`run(trace=...)`。回答の記録に trace id と工程の詳細を残す。#3226）。
        self._trace: AnswerTrace | None = None

    async def run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: SearchStageProgressCallback | None = None,
        token_callback: SearchTokenCallback | None = None,
        *,
        history: Sequence[ChatTurn] | None = None,
        trace: AnswerTrace | None = None,
    ) -> SearchResponse:
        """RAG 検索を実行する。

        検索・回答プロファイルの言語資源の上書きがあれば、この要求の間だけ束ねる(#2181)。``trace``
        は処理の経過の記録(#3226)。span の開始・表示用のデータの変更・終了を共通の span の表へ書き、
        工程の詳細を集める(回答の thread の span は thread から知らせる)。実行の全体の span は新し
        い trace に置く(画面・外部の traceparent を親にしない。サービストークンの MCP の要求の中だ
        け Agent の trace を続ける。`operation_trace_parent`)。
        """
        if request._rule_selection_scope:
            recognized = AnswerScope(**request._rule_selection_scope)
            current_sources: dict[tuple[str, str], str] = {}
            for question, answer, source in (
                self._scope.confirmed_premise_sources if self._scope else ()
            ):
                current_sources.setdefault((question, answer), source)
            self._scope = AnswerScope(
                context="\n".join(
                    value
                    for value in (self._scope.context if self._scope else "", recognized.context)
                    if value
                ),
                search_terms=tuple(
                    dict.fromkeys(
                        [
                            *(self._scope.search_terms if self._scope else ()),
                            *recognized.search_terms,
                        ]
                    )
                ),
                label="、".join(
                    value
                    for value in (self._scope.label if self._scope else "", recognized.label)
                    if value
                ),
                confirmed_premises=tuple(
                    dict.fromkeys(
                        [
                            *(self._scope.confirmed_premises if self._scope else ()),
                            *recognized.confirmed_premises,
                        ]
                    )
                ),
                confirmed_premise_sources=tuple(
                    dict.fromkeys(
                        [
                            *(
                                tuple(
                                    (
                                        question,
                                        answer,
                                        current_sources.get((question, answer), "user"),
                                    )
                                    for question, answer in self._scope.confirmed_premises
                                )
                                if self._scope
                                else ()
                            ),
                            *recognized.confirmed_premise_sources,
                        ]
                    )
                ),
            )

        trace_id = trace_id or new_trace_id()
        if trace is not None:
            trace.run_id = trace_id
        self._trace = trace
        with (
            operation_trace_parent() as links,
            operation_trace(
                "rag.answer" if request.generate_answer else "rag.search",
                operation_id=trace_id,
                listener=trace.listener() if trace is not None else None,
                links=links,
            ),
            bind_trace_observer(_observe_engine_call),
            profile_language_scope(self._settings),
        ):
            response = await self._run(
                request,
                trace_id,
                progress_callback,
                token_callback,
                history=history,
            )
            # 実行を確定する(root の span を閉じる。実行履歴に残す実行は記録のときに閉じている)。
            finish_operation_trace(trace_id)
            return response

    async def _run(
        self,
        request: SearchRequest,
        trace_id: str | None = None,
        progress_callback: SearchStageProgressCallback | None = None,
        token_callback: SearchTokenCallback | None = None,
        *,
        history: Sequence[ChatTurn] | None = None,
    ) -> SearchResponse:
        """RAG 検索を実行する。

        ``history`` を渡すと、会話履歴を踏まえて最新の質問を単独の質問へ書き換えてから
        検索・回答する(``rag_history_rewrite_enabled``)。
        """
        started_at = perf_counter()
        trace_id = trace_id or new_trace_id()
        if self._caller_queries is not None and (request.generate_answer or history):
            raise ValueError(
                "呼び出し元の検索文は、会話の履歴の無い、回答を作らない検索だけに使います。"
            )
        if self._run_recorder is not None:
            # 失敗(時間切れなど)の記録にも質問を残す(#1890)。
            self._run_recorder.question = request.query
        if request.retrieval_only:
            return await self._run_engine_retrieval(
                request,
                trace_id=trace_id,
                started_at=started_at,
                progress_callback=progress_callback,
            )
        return await self._run_engine(
            request,
            trace_id=trace_id,
            started_at=started_at,
            token_callback=token_callback,
            history=history,
            progress_callback=progress_callback,
        )

    async def _run_engine(
        self,
        request: SearchRequest,
        *,
        trace_id: str,
        started_at: float,
        token_callback: SearchTokenCallback | None,
        history: Sequence[ChatTurn] | None = None,
        progress_callback: SearchStageProgressCallback | None = None,
    ) -> SearchResponse:
        """rag_poc の根拠付き回答エンジンで回答する。

        rag_poc の回答フローは単発質問前提のため、会話履歴がある場合は最新の質問を
        履歴を踏まえた単独の質問へ書き換えてから実行する(失敗時は元の質問)。
        """
        # 実行履歴(#1890)は段(質問の書き換え・回答・回答の記録の保存など)の時間を集める。
        stage_callback = (
            self._run_recorder.observe(progress_callback)
            if self._run_recorder is not None
            else progress_callback
        )
        original_query = request.original_question or request.query
        rewritten_query = (
            request.query
            if self._history_question_prepared and request.query != original_query
            else ""
        )
        # 回答エンジンの前の段(書き換え・抽出項目の条件・添付)の縮退。回答エンジンへ渡し、回答の
        # 診断の degraded_steps に回答の中の補助の処理と同じ形で出す(#3286)。
        degraded = DegradedSteps()
        if (
            history
            and self._settings.rag_history_rewrite_enabled
            and not self._history_question_prepared
        ):
            rewritten_query = await _observe_stage(
                trace_id,
                "history_rewrite",
                self._rewrite_query_with_history(
                    original_query, history, report_degraded=degraded.record_degraded
                ),
                progress_callback=stage_callback,
                result_details=lambda rewritten: history_rewrite_details(rewritten, history),
            )
            if rewritten_query:
                request = request.model_copy(
                    update={"query": rewritten_query, "original_question": original_query}
                )
        caller_queries = (
            [query for query in self._caller_queries if query.strip()]
            if self._caller_queries is not None
            else None
        )
        # 質問に書かれた条件を抽出項目の条件として読み取る
        # (Metadata Filter Extraction。有効なときだけ。#652)。
        # チャットは会話履歴で書き換えた質問から読む。呼び出し元が検索文を作った検索(#2651)は、
        # 読み取りの LLM を呼ばない(条件は呼び出し元が filters で渡す)。
        auto_field_conditions: list[ExtractionFieldCondition] = []
        if (
            self._settings.rag_auto_field_filter_enabled
            and request.search_answer_profile_id
            and caller_queries is None
        ):
            auto_field_conditions = await _observe_stage(
                trace_id,
                "field_filter",
                self._read_field_conditions(request, report_degraded=degraded.record_degraded),
                progress_callback=stage_callback,
                result_details=field_filter_details,
            )
        guide = dict(self._settings.rag_support_guide or {})
        attachment_chunks = []
        if self._attachment_conversation_id and self._approved_faq is None:
            from app.rag.chat_attachments import attachment_evidence, chat_attachment_service

            attachment_status = chat_attachment_service().reading_status()
            # 添付の読み込みと rerank を 1 つの工程の span にする(処理の経過の段。#3475)。
            attachment_chunks = await _observe_stage(
                trace_id,
                "attachment_evidence",
                attachment_evidence(
                    request.query,
                    self._attachment_conversation_id,
                    rerank=self._genai.rerank if self._settings.rag_rerank_enabled else None,
                    report_degraded=degraded.record_degraded,
                ),
                progress_callback=stage_callback,
                result_outcome=lambda _result: (
                    "success"
                    if attachment_status == "done"
                    else "skipped"
                    if attachment_status == "skipped"
                    else "error"
                ),
            )
        effective_scope = scope_with_history_confirmations(
            self._scope, self._selected_history_facts
        )
        engine = AnswerEngine(
            self._settings,
            oracle=self._oracle,
            genai=self._genai,
            runtime_knowledge_payload=self._settings.rag_runtime_knowledge or None,
            answer_model_id=self._answer_model_id,
            auto_field_conditions=auto_field_conditions,
            approved_faq=self._approved_faq,
            scope=effective_scope,
            attachment_chunks=attachment_chunks,
            caller_queries=caller_queries,
            degraded=degraded,
            complete_evidence=self._complete_evidence,
        )

        async def emit_step(
            name: str, outcome: str, elapsed: float, record: Mapping[str, Any]
        ) -> None:
            # 工程の知らせ（時間切れの工程・MCP の進み具合）。工程の詳細は
            # 工程の span に付く（#3226）。
            await _emit_stage_progress(
                progress_callback,
                trace_id=trace_id,
                stage=answer_step_stage(name),
                outcome=outcome,
                elapsed=elapsed,
                attributes={key: record[key] for key in ("llm_calls", "status") if key in record},
            )

        # 回答エンジンは検索と回答の生成（LLM）を中で行う。
        # 進捗には全体を 1 工程（#375）とし、その中の各工程（質問の理解・文書検索など）も
        # 入れ子の工程として出す（#593）。回答を作らない RAG 検索は同じ流れを回答の生成の手前で
        # 止め、全体の工程を「質問の理解から根拠の検索まで」（evidence）にする（#649・#3506）。
        if request.generate_answer and guide.get("short_answer") and self._approved_faq is None:
            # 業務ガイドの必要な条件が分からず、確かめるか人へ引き継ぐ（#1238）。
            # 回答（モデル）は作らない。
            outcome = guide_short_circuit_outcome(guide)
        else:
            outcome = await _observe_stage(
                trace_id,
                "answer" if request.generate_answer else EVIDENCE_STAGE,
                engine.run(
                    request, step_callback=emit_step if progress_callback is not None else None
                ),
                progress_callback=stage_callback,
            )
            if guide and request.generate_answer:
                # 回答の根拠（画面の図の項目の種類）で決まった条件を、分かっている条件にする
                # （#2404）。
                guide = with_evidence_conditions(guide, outcome.diagnostics)
                apply_guide_to_diagnostics(outcome.diagnostics, guide)
        profile_revision = self._settings.rag_search_answer_profile_revision
        if profile_revision:
            # 回答を作った検索・回答プロファイルの版（#1276）。
            outcome.diagnostics.setdefault("provenance", {})["search_answer_profile"] = dict(
                profile_revision
            )
        guide_preview = self._settings.rag_guide_preview
        if guide_preview:
            # 業務ガイドの下書きで試した回答（#1288）。回答の記録に残し、履歴・評価から外す。
            outcome.diagnostics[GUIDE_PREVIEW_KEY] = dict(guide_preview)
        if request.generate_answer:
            # 回答の経路（固定の RAG）と理由、現場のデータの確認の要否を記録する（#1283）。
            route = answer_route(outcome.diagnostics)
            outcome.diagnostics["route"] = route
            record_answer_route(route["reason"], route["requires_environment_data"])
        final_answer = outcome.answer
        if outcome.evaluation_input is not None:
            outcome.evaluation_input["answer_text"] = final_answer
        # 回答の注意書き（#2405）。回答の確定の後に、本文とは別の項目として付ける。
        # 窓口の案内は検索・回答プロファイルの上書きと業務ガイドの引き継ぎで決める（#2400）。
        notice = (
            compose_rag_answer_notice(
                await asyncio.to_thread(self._answer_notice),
                outcome.diagnostics,
                contact_override=self._settings.rag_answer_contact_override,
                guide=guide,
            )
            if request.generate_answer and self._answer_notice is not None
            else None
        )
        if token_callback is not None and final_answer:
            await token_callback(SearchTokenDelta(trace_id=trace_id, text=final_answer))
        diagnostics = build_search_diagnostics(
            request,
            settings=self._settings,
            retrieval_strategy_adapter="grounded",
            answer=cast(
                dict[str, JsonValue],
                {
                    **outcome.diagnostics,
                    "original_question": original_query,
                    "rewritten_question": rewritten_query,
                },
            ),
        )
        elapsed = elapsed_ms(started_at)
        record_rag_request(SEARCH_METRIC_MODE, "success", elapsed / 1000, len(outcome.citations))
        record_rag_search_audit(
            trace_id=trace_id,
            outcome="success",
            query=request.query,
            filters=request.filters,
            retrieved_count=len(outcome.citations),
            citations=outcome.citations,
            elapsed_ms=elapsed,
            diagnostics=diagnostics,
        )
        # 回答の記録は回答を作ったときだけ保存する(RAG 検索は回答が無い。#649)。保存は工程
        # 「回答の記録の保存」にし、失敗したら処理の経過で失敗にする。回答は返す(#2342)。
        storage_failures: list[StorageFailure] = []
        if request.generate_answer:
            try:
                await _observe_stage(
                    trace_id,
                    ANSWER_RECORD_SAVE_STAGE,
                    self._save_answer_record(
                        trace_id=trace_id,
                        request=request,
                        question=original_query,
                        rewritten_question=rewritten_query,
                        answer=final_answer,
                        citations=outcome.citations,
                        diagnostics=diagnostics.answer or {},
                        surface="search" if history is None else "chat",
                        evaluation_input=outcome.evaluation_input,
                        answer_notice=notice,
                    ),
                    progress_callback=stage_callback,
                )
            except Exception as exc:  # 保存は補助。回答の返却を止めない。
                logger.warning(
                    "answer record save failed",
                    extra={"trace_id": trace_id, **safe_exception_fields(exc)},
                )
                storage_failures.append(storage_failure(ANSWER_RECORD_SAVE_STAGE, exc))
        if self._run_recorder is not None:
            await self._run_recorder.record(
                run_id=trace_id,
                question=original_query,
                search_answer_profile_id=request.search_answer_profile_id,
                answer_generated=request.generate_answer,
                status="success",
                elapsed_ms=elapsed,
                citations=outcome.citations,
                answer_diagnostics=diagnostics.answer,
                storage_failures=storage_failures,
            )
        return SearchResponse(
            answer=final_answer,
            citations=outcome.citations,
            trace_id=trace_id,
            elapsed_ms=elapsed,
            diagnostics=diagnostics,
            storage_failures=storage_failures,
            notice=notice,
            # 品質評価が標準回答で比較するときに使う(応答には出さない。#591)。
        ).with_evaluation_input(outcome.evaluation_input)

    async def _run_engine_retrieval(
        self,
        request: SearchRequest,
        *,
        trace_id: str,
        started_at: float,
        progress_callback: SearchStageProgressCallback | None = None,
    ) -> SearchResponse:
        """回答フローの検索だけを行い、回答を作らずに候補を引用として返す(#593)。

        KB の検索テストとレシピの検索比較が使う(``SearchRequest.retrieval_only``)。LLM を呼ばず、
        回答記録・質問履歴も保存しない。
        """
        engine = AnswerEngine(self._settings, oracle=self._oracle, genai=self._genai)
        citations = await _observe_stage(
            trace_id,
            "retrieval",
            engine.retrieve(request),
            result_attributes=lambda chunks: {"retrieved_count": len(chunks)},
            progress_callback=progress_callback,
        )
        diagnostics = build_search_diagnostics(
            request,
            settings=self._settings,
            retrieval_strategy_adapter="retrieval_only",
        )
        elapsed = elapsed_ms(started_at)
        record_rag_request(SEARCH_METRIC_MODE, "success", elapsed / 1000, len(citations))
        record_rag_search_audit(
            trace_id=trace_id,
            outcome="success",
            query=request.query,
            filters=request.filters,
            retrieved_count=len(citations),
            citations=citations,
            elapsed_ms=elapsed,
            diagnostics=diagnostics,
        )
        return SearchResponse(
            answer="",
            citations=citations,
            trace_id=trace_id,
            elapsed_ms=elapsed,
            diagnostics=diagnostics,
        )

    async def _save_answer_record(
        self,
        *,
        trace_id: str,
        request: SearchRequest,
        question: str,
        rewritten_question: str,
        answer: str,
        citations: list[RetrievedChunk],
        diagnostics: Mapping[str, JsonValue],
        surface: str,
        evaluation_input: Mapping[str, object] | None = None,
        answer_notice: str | None = None,
    ) -> None:
        """回答を保存する(rag_poc の answer JSON 保存に相当)。

        失敗は例外のまま返す(呼び出し側が工程の失敗として記録し、回答は返す。#2342)。
        """
        search_answer_profile_id = request.search_answer_profile_id
        # 引用は画面・評価が使う項目だけを保存する（索引の内部の項目・親の本文を持たない。#1371）。
        stored = stored_citations(citations)
        warn_if_large_answer_record(
            trace_id=trace_id, citations=stored, evaluation_input=evaluation_input
        )
        await self._oracle.save_answer_record(
            {
                "trace_id": trace_id,
                "search_answer_profile_id": search_answer_profile_id,
                "surface": surface,
                "answer_engine": ANSWER_ENGINE,
                "question": question,
                "rewritten_question": rewritten_question,
                "answer": answer,
                "citations": stored,
                "diagnostics": {
                    **dict(diagnostics),
                    # 処理の経過の記録(共通の span の表の trace id と工程の詳細。#3226)。
                    **({"trace": self._trace.record()} if self._trace is not None else {}),
                },
                "evaluation_input": evaluation_input,
                "answer_notice": answer_notice,
            }
        )
        if self._settings.rag_answer_record_retention_days > 0:
            try:
                await self._oracle.purge_answer_records(
                    self._settings.rag_answer_record_retention_days
                )
            except Exception as exc:  # 期限切れの記録の削除は補助。保存の成否に含めない。
                logger.warning(
                    "answer record purge failed",
                    extra={"trace_id": trace_id, **safe_exception_fields(exc)},
                )

    async def _read_field_conditions(
        self, request: SearchRequest, *, report_degraded: ReportDegraded = not_recorded
    ) -> list[ExtractionFieldCondition]:
        """検索・回答プロファイルの KB の抽出項目の定義で、質問から条件を読み取る(#652)。

        定義は検索の絞り込みの項目と同じ(利用者が使える有効な KB の定義の和集合。KB に無ければ
        全体の既定)。定義が無い・読めないときは LLM を呼ばずに空(条件なしで検索を続ける)。
        読めなかったとき・読み取りの LLM が失敗したときは ``report_degraded`` に残す(定義が無い
        ときと分ける。#3286)。
        """
        try:
            view = await self._oracle.get_search_answer_profile(
                request.search_answer_profile_id or ""
            )
            field_sets = (
                await self._oracle.list_knowledge_base_extraction_field_sets(
                    view.config.normalized_knowledge_base_ids()
                )
                if view is not None
                else []
            )
        except Exception as exc:  # 読み取りは補助。条件なしで検索を続ける。
            logger.warning("field definitions load failed", extra={**safe_exception_fields(exc)})
            report_degraded("field_filter", exc)
            return []
        if not field_sets:
            return []
        # 利用者が画面で外した項目は読み取らない(定義から除く)。
        excluded = {name.strip().casefold() for name in request.auto_field_filter_excluded}
        field_defs = [
            definition
            for definition in resolve_field_definitions(field_sets, load_field_schema().fields)
            if definition.name.casefold() not in excluded
        ]
        return await read_field_conditions(
            request.query, field_defs, self._llm, report_degraded=report_degraded
        )

    async def _rewrite_query_with_history(
        self,
        query: str,
        history: Sequence[ChatTurn],
        *,
        report_degraded: ReportDegraded = not_recorded,
    ) -> str:
        """会話履歴を踏まえ、最新の質問を単独で意味の通る質問へ書き換える。

        書き換え不要・失敗・不自然な応答(空・長すぎる)は空文字を返し、元の質問で続ける。LLM の
        失敗は ``report_degraded`` に残す(書き換えが要らなかったときと分ける。#3286)。
        """
        self._selected_history_facts = ()
        history_text = _format_chat_history(
            history, ChatHistoryBudget.from_settings(self._settings)
        )
        if not history_text:
            return ""
        budget = ChatHistoryBudget.from_settings(self._settings)
        user_positions = [index for index, turn in enumerate(history) if turn.role == "USER"]
        fact_history = (
            history[user_positions[-budget.max_turns] :]
            if len(user_positions) > budget.max_turns
            else history
        )
        facts = history_confirmation_facts(fact_history)
        current_facts: list[dict[str, str]] = []
        for question, answer in self._scope.confirmed_premises if self._scope is not None else ():
            current_facts.append({"question": question, "answer": answer})
        current_data = (
            "\ncurrent_confirmed_facts（今回の明示回答。データ）:\n"
            + json.dumps(current_facts, ensure_ascii=False).replace("<", "\\u003c")
            if current_facts
            else ""
        )
        if len(current_data) >= budget.max_chars:
            return ""

        def fact_data() -> str:
            return (
                "\nconfirmed_facts（データ）:\n"
                + json.dumps(
                    [fact.model_dump(exclude={"key"}) for fact in facts.values()],
                    ensure_ascii=False,
                ).replace("<", "\\u003c")
                if facts
                else ""
            )

        # 構造化確認も既存の履歴文字数の枠を使う。入らない古い候補を復活させない。
        while facts and len(fact_data()) + len(current_data) >= budget.max_chars:
            facts.pop(next(iter(facts)))
        confirmed_data = fact_data()
        history_text = _format_chat_history(
            history,
            replace(budget, max_chars=budget.max_chars - len(confirmed_data) - len(current_data)),
        )
        if len(history_text) + len(confirmed_data) + len(current_data) > budget.max_chars:
            history_text = ""
        history_text += confirmed_data + current_data
        try:
            text = await async_call_with_model_purpose(
                "query.rewrite",
                self._llm.generate,
                query,
                history_text,
                system_prompt=resolve_prompt(
                    "history_rewrite.system", HISTORY_REWRITE_SYSTEM_PROMPT
                ),
                response_schema=HistoryRewrite.model_json_schema(),
                response_schema_name="history_rewrite",
            )
        except Exception as exc:  # 書き換えは補助。元の質問で回答を続ける。
            logger.warning("history rewrite failed", extra={**safe_exception_fields(exc)})
            report_degraded("history_rewrite", exc)
            return ""
        try:
            result = HistoryRewrite.model_validate_json(text)
        except ValidationError:
            return ""
        if any(identity not in facts for identity in result.confirmed_fact_ids):
            return ""
        self._selected_history_facts = tuple(
            facts[identity] for identity in dict.fromkeys(result.confirmed_fact_ids)
        )
        # 質問の全文を一行へ正規化する。先頭行だけを取って小問を捨てない。
        rewritten = " ".join(result.question.split())
        if not rewritten or len(rewritten) > max(200, len(query) * 4):
            self._selected_history_facts = ()
            return ""
        return "" if rewritten == " ".join(query.split()) else rewritten


async def _observe_stage[T](
    trace_id: str,
    stage: str,
    operation: Awaitable[T],
    *,
    attributes: Mapping[str, object] | None = None,
    result_attributes: Callable[[T], Mapping[str, object]] | None = None,
    result_details: Callable[[T], Mapping[str, object] | None] | None = None,
    progress_callback: SearchStageProgressCallback | None = None,
    result_outcome: Callable[[T], Literal["success", "skipped", "error"]] | None = None,
) -> T:
    """非同期 stage の処理時間を outcome 付きで記録する。

    stage の中で実行した SQL の回数・行数・LOB の読み取りの回数・時間を span の属性に足す
    (``db_*``。SQL を実行しなかった stage には足さない。#1490)。

    工程の span には表示用のデータ(回答フローの外の工程は種類、終わった工程の詳細、
    失敗の要約)を付け、画面の処理の経過にする(#3226)。
    """
    kind = stage if stage in PIPELINE_STEP_KINDS else None
    with observed_span(
        f"rag.{stage}",
        attributes={"pr.stage": stage},
        display=SpanDisplay(kind=kind) if kind is not None else None,
    ) as span:
        started_at = perf_counter()
        base_attributes = dict(attributes or {})
        await _emit_stage_progress(
            progress_callback,
            trace_id=trace_id,
            stage=stage,
            outcome="started",
            elapsed=0.0,
            attributes=base_attributes,
        )
        try:
            with track_db_calls() as db_stats:
                try:
                    result = await operation
                finally:
                    if db_stats.execute_count or db_stats.lob_read_count:
                        base_attributes.update(db_stats.snapshot())
        except asyncio.CancelledError as exc:
            elapsed = perf_counter() - started_at
            record_rag_stage(SEARCH_METRIC_MODE, stage, "cancelled", elapsed)
            record_trace_span(
                trace_id=trace_id,
                span_name=stage,
                outcome="cancelled",
                seconds=elapsed,
                attributes=base_attributes,
                error=exc,
            )
            await _emit_stage_progress(
                progress_callback,
                trace_id=trace_id,
                stage=stage,
                outcome="cancelled",
                elapsed=elapsed,
                attributes=base_attributes,
            )
            raise
        except Exception as exc:
            elapsed = perf_counter() - started_at
            error_attributes = {**base_attributes, "error_type": type(exc).__name__}
            error_code = safe_error_code(exc)
            if error_code is not None:
                # 工程の失敗の詳細には安全なコードだけを出す（#2342）。
                error_attributes["error_code"] = error_code
            record_rag_stage(SEARCH_METRIC_MODE, stage, "error", elapsed)
            # 失敗した工程の詳細(短い要約とエラーの種類・コード。例外の本文は入れない。#2342)。
            span.set_display(details=failure_details(kind, error_attributes))
            record_trace_span(
                trace_id=trace_id,
                span_name=stage,
                outcome="error",
                seconds=elapsed,
                attributes=error_attributes,
                error=exc,
            )
            await _emit_stage_progress(
                progress_callback,
                trace_id=trace_id,
                stage=stage,
                outcome="error",
                elapsed=elapsed,
                attributes=error_attributes,
            )
            raise
        elapsed = perf_counter() - started_at
        outcome = result_outcome(result) if result_outcome is not None else "success"
        span.set_status({"success": "done", "skipped": "skipped", "error": "failed"}[outcome])
        if result_attributes is not None:
            base_attributes.update(result_attributes(result))
        record_rag_stage(SEARCH_METRIC_MODE, stage, outcome, elapsed)
        record_trace_span(
            trace_id=trace_id,
            span_name=stage,
            outcome=outcome,
            seconds=elapsed,
            attributes=base_attributes,
        )
        if result_details is not None:
            # 詳細は処理の経過の補助。作れなくても工程は成功のまま続ける。
            try:
                details = step_details_model(result_details(result))
            except Exception as exc:
                logger.warning("stage details failed", extra={**safe_exception_fields(exc)})
            else:
                if details is not None:
                    span.set_display(details=details)
        await _emit_stage_progress(
            progress_callback,
            trace_id=trace_id,
            stage=stage,
            outcome=outcome,
            elapsed=elapsed,
            attributes=base_attributes,
        )
        return result


async def _emit_stage_progress(
    progress_callback: SearchStageProgressCallback | None,
    *,
    trace_id: str,
    stage: str,
    outcome: str,
    elapsed: float,
    attributes: Mapping[str, object],
) -> None:
    if progress_callback is None:
        return
    await progress_callback(
        SearchStageProgress(
            trace_id=trace_id,
            stage=stage,
            outcome=outcome,
            elapsed_ms=round(elapsed * 1000, 3),
            attributes=dict(attributes),
        )
    )
