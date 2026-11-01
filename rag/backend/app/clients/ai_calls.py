"""取込の AI の呼び出し（LLM・VLM・Embedding）の上限つきの並行の実行器を作る（#2002）。

共通の部品 ``pr_backend_core.bounded_calls`` に、RAG の設定（段ごとの並行の上限・待ちの上限・
件の失敗の扱い）と、RAG の client の例外の分類を渡す。取込の job の中では、実行器の
event（待ち・再試行・並行の数・成功）を job の進捗（``progress_json`` の ``calls``）へ写す。
待っている間も heartbeat の間隔で進捗が変わるため、#2003 の停滞の検出が待ちを停滞と誤らない。

SDK の再試行との分担: この実行器を通す呼び出しは SDK の再試行を切る（モデルの API の
openai SDK は ``sdk_retries_disabled``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import Callable, Iterator
from typing import Literal

from pr_backend_core.bounded_calls import (
    BoundedCaller,
    BoundedCallPolicy,
    CallEvent,
    Disposition,
    ErrorKind,
    KindRule,
    chain_classifiers,
    policy_with_concurrency,
)

AiCallStage = Literal[
    "vision",
    "heading_levels",
    "field_extraction",
    "navigation_summary",
    "raptor_summary",
    "embedding",
    "rerank",
    "evaluation",
    "guide_import",
]

STAGE_LABELS: dict[str, str] = {
    "vision": "図・表の説明（Vision）",
    "heading_levels": "見出しの階層の推定",
    "field_extraction": "メタデータ／項目抽出",
    "navigation_summary": "章節の要約",
    "raptor_summary": "RAPTOR の階層の要約",
    "embedding": "埋め込み（Embedding）",
    "rerank": "リランク",
    "evaluation": "品質評価",
    "guide_import": "業務ガイドの下書きの作成",
}

# 空の応答・件数の食い違いは「出力の形の誤り」（今の schema の再試行と同じく回数を絞って再試行）。
_INVALID_OUTPUT_MARKERS = (
    "回答 text がありません",
    "embeddings がありません",
    "document_ranks がありません",
    "embedding の件数が一致しません",
)


# 埋め込みモデル が HTTP 400 で返す拒否の文の印(#2467。実 API の応答で確認)。
# 高負荷は 400 で返るが一時的なため、再試行の対象にする(共通の分類では「要求の誤り」になる)。
_EMBEDDING_REJECTION_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("service_overloaded", ("high load", "try again later")),
    ("input_too_long", ("is too long", "max tokens per text")),
    ("too_many_inputs", ("inputs array size",)),
    ("input_empty", ("cannot be empty",)),
)
_EMBEDDING_REASON_KINDS: dict[str, ErrorKind] = {
    "service_overloaded": ErrorKind.UNAVAILABLE,
    "input_too_long": ErrorKind.TOO_LARGE,
}


def embedding_rejection_reason(exc: BaseException) -> str | None:
    """埋め込みの API の HTTP 400 の拒否を理由に分ける(本文は返さない)。400 でなければ None。"""
    if getattr(exc, "status", None) != 400:
        return None
    text = " ".join(
        str(value)
        for value in (getattr(exc, "message", None), exc)
        if value is not None and not callable(value)
    ).casefold()
    for reason, markers in _EMBEDDING_REJECTION_MARKERS:
        if any(marker in text for marker in markers):
            return reason
    return "invalid_request"


def rag_error_kind(exc: BaseException) -> ErrorKind | None:
    """RAG の client の例外のうち、共通の分類では決まらないものの種類。"""
    name = type(exc).__name__
    if name == "GenAiConfigError":
        return ErrorKind.AUTHENTICATION
    if name == "ServiceError":
        kind = _EMBEDDING_REASON_KINDS.get(embedding_rejection_reason(exc) or "")
        if kind is not None:
            return kind
    message = str(exc)
    if isinstance(exc, ValueError) and any(marker in message for marker in _INVALID_OUTPUT_MARKERS):
        return ErrorKind.INVALID_OUTPUT
    return None


def _concurrency(settings: object, stage: str) -> int:
    name = {
        "vision": "rag_vision_concurrency",
        "navigation_summary": "rag_navigation_summary_concurrency",
        "raptor_summary": "rag_raptor_concurrency",
        "embedding": "rag_embedding_concurrency",
        "evaluation": "rag_evaluation_concurrency",
        "guide_import": "rag_support_guide_import_concurrency",
    }.get(stage)
    if name is None:
        return 1
    return max(1, int(getattr(settings, name, 1) or 1))


# 品質評価のケース（回答・判定）の実行器の規則（#2004）。1 回の回答・判定は中で LLM を何回も呼び、
# 一時的な失敗（5xx・接続・時間切れ）は回答の中で再試行するため、ここでは流量の制限（429）だけを
# 全体で待って送り直す。時間切れ（回答の上限）はケースの失敗のまま送り直さない。権限・認証・
# モデルの設定の誤り・割り当ての使い切りは評価の全体を止める（同じ失敗をくり返さない）。
_EVALUATION_RULES: dict[ErrorKind, KindRule] = {
    **{kind: KindRule() for kind in ErrorKind},
    ErrorKind.RATE_LIMITED: KindRule(retries=3, throttle=True),
    **{
        kind: KindRule(disposition=Disposition.JOB)
        for kind in (
            ErrorKind.QUOTA_EXHAUSTED,
            ErrorKind.AUTHENTICATION,
            ErrorKind.PERMISSION,
            ErrorKind.NOT_FOUND,
            ErrorKind.TLS,
            ErrorKind.NOT_SUPPORTED,
        )
    },
}


def ai_call_policy(settings: object, stage: str) -> BoundedCallPolicy:
    """段の実行器の設定（並行の上限・待ちの上限・最後の再試行の待ち・件の失敗の扱い）。"""
    if stage == "evaluation":
        return BoundedCallPolicy(
            max_concurrency=_concurrency(settings, stage),
            backoff_max_seconds=float(getattr(settings, "rag_ai_call_backoff_max_seconds", 60.0)),
            rules=_EVALUATION_RULES,
        )
    return BoundedCallPolicy(
        max_concurrency=_concurrency(settings, stage),
        backoff_max_seconds=float(getattr(settings, "rag_ai_call_backoff_max_seconds", 60.0)),
        final_retry_delay_seconds=float(
            getattr(settings, "rag_ai_call_final_retry_delay_seconds", 30.0)
        ),
        fail_job_on_item_error=bool(getattr(settings, "rag_ai_call_fail_job_on_item_error", False)),
    )


async def _sleep(seconds: float) -> None:
    """再試行の待ち（テストは差し替えて実時間で待たない）。"""
    await asyncio.sleep(seconds)


def _random() -> float:
    """jitter の乱数（暗号の用途ではない）。"""
    return random.random()  # nosec B311 - backoff の jitter で、暗号の用途ではない


def make_ai_caller(
    settings: object, stage: str, *, max_concurrency: int | None = None
) -> BoundedCaller:
    """段の実行器を作る（1 回の段・1 つの job ごと）。``max_concurrency`` は上限の上書き。"""
    policy = ai_call_policy(settings, stage)
    if max_concurrency is not None:
        policy = policy_with_concurrency(policy, max_concurrency)
    return BoundedCaller(
        f"rag.{stage}",
        policy,
        label=STAGE_LABELS.get(stage, stage),
        classifier=chain_classifiers(rag_error_kind),
        sleep=lambda seconds: _sleep(seconds),
        random=lambda: _random(),
    )


@contextlib.contextmanager
def ingestion_ai_calls(
    settings: object, stage: str, *, total: int | None = None
) -> Iterator[BoundedCaller]:
    """取込の段の実行器。job の中なら、実行器の状態を job の進捗（``calls``）へ写す。"""
    caller = make_ai_caller(settings, stage)
    finish = attach_ingestion_progress(caller, stage, total=total)
    try:
        yield caller
    finally:
        finish()


def attach_ingestion_progress(
    caller: BoundedCaller, stage: str, *, total: int | None = None
) -> Callable[[], None]:
    """job の中なら、実行器の event ごとに状態を job の進捗（``calls``）へ写す。

    返す関数は段の終わりに呼ぶ（終わった状態を写す）。job の外では何もしない。
    """
    from app.rag.ingestion_progress import active_ingestion_progress

    progress = active_ingestion_progress()
    if progress is None:
        return lambda: None

    def listener(_: CallEvent) -> None:
        progress.update_calls(stage, caller.status(), total=total)

    caller.add_listener(listener)
    progress.update_calls(stage, caller.status(), total=total)

    def finish() -> None:
        progress.update_calls(stage, caller.status(), total=total, finished=True)

    return finish
