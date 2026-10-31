"""既存の高度な回答フローへ I/O 関数を注入する境界。

公開 SDK の Protocol と併用し、CRAG や multi-query の既存挙動も保護する。
互換呼び出しの既定実装は composition root で遅延構築する。
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pr_backend_core.observability.model_purpose import model_call_purpose

if TYPE_CHECKING:
    from rag_engine.ports import Embedder


@dataclass(frozen=True)
class AnswerDependencies:
    """既存関数と同じ引数・結果を受け渡す回答フロー用 I/O。

    search/check_ready は chunk_run_id・settings・検索条件を keyword で受け取る。
    parse_text/parse_images は既存 schema 型を受け取り、その型の結果を返す。
    rerank は質問・候補・settings と top_n を受け取り RerankTextRank を返す。
    business_domains は質問文を受け取り、質問が名指しした業務名（文書の大分類の値）を返す。省略時は
    domain profile の `business_patterns` で推定する。呼び出し元が検索範囲の業務名の一覧を持つときに渡す。
    screen_catalog / screen_chunks は画面目録の連携（#1108）の入力。検索結果が検索範囲の全 chunk を含まない
    呼び出し元が、目録と選んだ画面の chunk を検索範囲から読むときに渡す。省略時は検索結果の chunk から作る。
    screen_catalog は settings を受け取り {文書名: 番号付きの見出し} を返す。screen_chunks は links（選んだ
    （文書名、見出し）の列）・existing（候補にある chunk_uid）・settings を受け取り、その画面の child と親の
    StoredChunk を返す。
    section_outline / section_chunks は章節の拡張（#1436）の入力。章節と chunk の対応を持つ呼び出し元が渡す。
    section_outline は chunk_uid を受け取り、その chunk の文書の章節の一覧（`section_expansion.SectionOutline`。
    対応が無い chunk は None）を返す。section_chunks は document_id・chunk_set_id・section_id を受け取り、
    章節の chunk（child と親の StoredChunk。child は章節の中の順）を返す。部分取得や失敗は
    `SectionRead(complete=False)` で区別する。省略時は章節で広げない。
    prefetch_search は、質問拡張の LLM と並行に元の質問の検索を先に読む入力（#1499）。query・candidate_limit・
    inquiry_conditions・settings を keyword で受け取り、続く search が同じ検索文の結果を読めるよう呼び出し元の
    cache に入れる（戻り値は使わない）。結果を変えない（search の結果は並行にしないときと同じ）実装だけを渡す。
    省略時は先に読まない。
    embedder は業務ガイドの手順の充足で、根拠を共有する手順のどれを item が説明したかを語で決められないときに、
    意味の近さで決める embedding（`ports.Embedder`。索引と同じ OCI Generative AI の Cohere Embed。#2649）。省略時は
    語の判定だけで決める。
    report_degraded は補助の処理（画面の選択など）が失敗して空のまま続けたことを、処理の種類と例外を受け取って
    呼び出し元の診断に残す（#3259。例外の文は呼び出し元が残さない）。省略時は残さない。
    """

    search: Callable[..., Any]
    check_ready: Callable[..., Any]
    parse_text: Callable[..., Any]
    parse_images: Callable[..., Any]
    rerank: Callable[..., Any]
    business_domains: Callable[[str], Sequence[str]] | None = None
    screen_catalog: Callable[..., dict[str, list[str]]] | None = None
    screen_chunks: Callable[..., Sequence[Any]] | None = None
    section_outline: Callable[..., Any] | None = None
    section_chunks: Callable[..., Sequence[Any]] | None = None
    prefetch_search: Callable[..., Any] | None = None
    embedder: "Embedder | None" = None
    report_degraded: Callable[[str, BaseException], None] | None = None
    # 精排・起点選択の後に親/近傍を読む。未指定の SDK は取得済みの原文だけで構築する。
    context_chunks: Callable[..., Sequence[Any]] | None = None
    record_retrieval: Callable[[Mapping[str, Any]], None] | None = None


_active: ContextVar[AnswerDependencies | None] = ContextVar("rag_engine_answer_dependencies", default=None)

# 回答 1 件あたりの LLM 呼出回数。実行記録が工程ごとの差分を取って表示する。呼出単位で分離するため
# 実行記録側が開始時に 0 へ設定し、終了時に元へ戻す。
llm_call_count: ContextVar[int] = ContextVar("rag_engine_llm_call_count", default=0)


def _count_llm_call() -> None:
    llm_call_count.set(llm_call_count.get() + 1)


@contextmanager
def bind_dependencies(dependencies: AnswerDependencies) -> Iterator[None]:
    """スレッド・タスクごとに I/O を切り替え、例外時も復元する。"""
    token = _active.set(dependencies)
    try:
        yield
    finally:
        _active.reset(token)


def _get() -> AnswerDependencies:
    current = _active.get()
    if current is not None:
        return current
    from rag_engine.composition import oracle_answer_dependencies

    return oracle_answer_dependencies()


def question_business_domains(question: str) -> list[str] | None:
    """注入された業務名の照合で、質問が名指しした業務名を返す。注入が無ければ None（profile で推定する）。"""
    current = _active.get()
    if current is None or current.business_domains is None:
        return None
    return list(current.business_domains(question))


def injected_screen_catalog(**kwargs) -> dict[str, list[str]] | None:
    """注入された画面目録を返す。注入が無ければ None（検索結果の chunk から作る）。"""
    current = _active.get()
    if current is None or current.screen_catalog is None:
        return None
    return current.screen_catalog(**kwargs)


def injected_screen_chunks(**kwargs) -> list[Any] | None:
    """注入された読み込みで、選んだ画面の chunk を返す。注入が無ければ None（検索結果の chunk から選ぶ）。"""
    current = _active.get()
    if current is None or current.screen_chunks is None:
        return None
    return list(current.screen_chunks(**kwargs))


def section_dependencies() -> tuple[Callable[..., Any], Callable[..., Sequence[Any]]] | None:
    """注入された章節の読み込み（章節の一覧, 章節の chunk）。注入が無ければ None（章節で広げない。#1436）。"""
    current = _active.get()
    if current is None or current.section_outline is None or current.section_chunks is None:
        return None
    return current.section_outline, current.section_chunks


def injected_embedder() -> "Embedder | None":
    """注入された embedding（#2649）。注入が無ければ None（業務ガイドの手順の充足を語の判定だけで決める）。"""
    current = _active.get()
    return None if current is None else current.embedder


def report_degraded(kind: str, exc: BaseException) -> None:
    """補助の処理 ``kind`` の失敗を、注入された記録に残す（#3259）。注入が無ければ何もしない。"""
    current = _active.get()
    if current is not None and current.report_degraded is not None:
        current.report_degraded(kind, exc)


def injected_prefetch_search() -> Callable[..., Any] | None:
    """注入された元の質問の先読み（#1499）。注入が無ければ None（先に読まない）。"""
    current = _active.get()
    return None if current is None else current.prefetch_search


def search_adb_hybrid_chunks(**kwargs):
    """注入された検索実装へ既存検索要求を転送する。"""
    return _get().search(**kwargs)


def check_adb_hybrid_search_ready(**kwargs):
    """準備不足は AdbHybridSearchUnavailable として呼び出し元へ伝播する。"""
    return _get().check_ready(**kwargs)


def parse_text_response(*args, **kwargs):
    """指定 schema のテキスト回答を取得する。呼出回数を llm_call_count に数える。"""
    _count_llm_call()
    with model_call_purpose(kwargs.pop("purpose", None)):
        return _get().parse_text(*args, **kwargs)


def parse_multimodal_response(*args, **kwargs):
    """指定 schema の画像付き回答を取得する。呼出回数を llm_call_count に数える。"""
    _count_llm_call()
    with model_call_purpose(kwargs.pop("purpose", None)):
        return _get().parse_images(*args, **kwargs)


def rerank_text_with_scores(*args, **kwargs):
    """指定した候補の順位と score を取得する。"""
    return _get().rerank(*args, **kwargs)


def injected_context_chunks(**kwargs) -> list[Any] | None:
    """選択済み起点の原文を現在の検索範囲で遅延取得する。"""
    current = _active.get()
    if current is None or current.context_chunks is None:
        return None
    return list(current.context_chunks(**kwargs))


def record_retrieval_stage(stage: Mapping[str, Any]) -> None:
    """内容を推定せず、実際の入力・採用・打切りを呼出元へ記録する。"""
    current = _active.get()
    if current is not None and current.record_retrieval is not None:
        current.record_retrieval(stage)
