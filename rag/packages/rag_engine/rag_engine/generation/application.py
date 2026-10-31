"""既存の multi-query / CRAG を UI なしで利用するアプリケーションサービス。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rag_engine.generation.answering import AnswerQuestionResult
    from rag_engine.knowledge.answers import SavedAnswer
from rag_engine.config import Settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.ports import ArtifactStore
from rag_engine.resources.runtime import Runtime


@dataclass(frozen=True)
class AnswerRequest:
    """高度な質問応答の要求。run_id は current_chunk_run を選んだ場合に必要。"""

    question: str
    run_id: str = ""
    engines: tuple[str, ...] = ()
    top_k: int = 20
    neighbor_count: int = 3
    query_strategy: str = "simple"
    answer_flow: str = "standard"
    retrieval_scope: str = "knowledge_base"
    rerank_enabled: bool | None = None
    provider: str | None = None
    filters: Mapping[str, str] = field(default_factory=dict)


class AnswerService:
    """高度な既存フローを設定・外部 I/O・業務 profile から組み立てる。"""

    def __init__(
        self, settings: Settings, runtime: Runtime, dependencies: AnswerDependencies, *, artifact_store: ArtifactStore | None = None
    ):
        self.settings, self.runtime, self.dependencies = settings, runtime, dependencies
        self.artifact_store = artifact_store

    def run(self, request: AnswerRequest) -> AnswerQuestionResult:
        """構造化回答と工程記録を返す。回答ファイルの保存は明示的に別途呼び出す。"""
        from rag_engine.generation.answering import answer_question_result
        from rag_engine.knowledge.classification import REQUEST_FILTER_KEYS, classification_filter_from_values
        from rag_engine.models.contracts import SearchRequest

        SearchRequest(request.question, request.retrieval_scope, request.top_k, request.neighbor_count).validate()
        unknown = set(request.filters) - REQUEST_FILTER_KEYS
        if unknown:
            raise ValueError(f"Unsupported classification filters: {sorted(unknown)}")
        with self.runtime.activate(), bind_dependencies(self.dependencies):
            return answer_question_result(
                request.question,
                request.run_id,
                request.engines,
                self.settings,
                chunk_top_k=request.top_k,
                chunk_neighbor_count=request.neighbor_count,
                query_strategy=request.query_strategy,
                answer_flow=request.answer_flow,
                retrieval_scope=request.retrieval_scope,
                rerank_enabled=request.rerank_enabled,
                answer_llm_provider=request.provider,
                classification_filter=classification_filter_from_values(**dict(request.filters)),
            )

    def run_and_save(self, request: AnswerRequest, *, standard_answer: str = "") -> tuple[AnswerQuestionResult, SavedAnswer | None]:
        """通常回答を生成し、標準回答があれば同じ provider で評価して回答と履歴を保存する。

        評価にも注入した I/O を使う。評価失敗は回答保存を妨げず、出典なしの結果は未保存。
        """
        from rag_engine.knowledge.answers import save_answer_result

        result = self.run(request)
        with self.runtime.activate(), bind_dependencies(self.dependencies):
            saved = save_answer_result(
                result,
                self.settings,
                run_id=request.run_id,
                standard_answer=standard_answer,
                store=self.artifact_store,
                evaluation_provider=request.provider,
            )
        return result, saved


def answer_question_result(question, run_id, preferred_engines, settings, **options):
    """Gradio 互換引数を受け、共有サービスで高度な回答フローを実行する。"""
    from pathlib import Path

    from rag_engine.composition import create_oracle_application

    application = create_oracle_application(settings.workspace_dir or Path.cwd(), settings=settings)
    classification = options.pop("classification_filter", None)
    request = AnswerRequest(
        question=question,
        run_id=run_id,
        engines=tuple(preferred_engines),
        top_k=options.pop("chunk_top_k", 20),
        neighbor_count=options.pop("chunk_neighbor_count", 3),
        query_strategy=options.pop("query_strategy", "simple"),
        answer_flow=options.pop("answer_flow", "standard"),
        retrieval_scope=options.pop("retrieval_scope", "current_chunk_run"),
        rerank_enabled=options.pop("rerank_enabled", None),
        provider=options.pop("answer_llm_provider", None),
        # to_metadata() は as_of を含まないため、UI の基準日が検索層に届かなかった (#976)。
        filters=classification.to_request_filters() if classification is not None else {},
    )
    if options:
        raise TypeError(f"Unknown answer options: {sorted(options)}")
    with application:
        return application.answers.run(request)
