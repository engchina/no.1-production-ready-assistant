"""一括の LLM・Embedding の呼び出しの上限つきの並行の実行器（#2004）。

共通の部品 ``pr_backend_core.bounded_calls`` に、NL2SQL の設定（段ごとの並行の上限）と、
NL2SQL の client の例外の分類を渡す。NL2SQL の client は同期（httpx・OCI SDK）なので、
``SyncBoundedCaller`` を通して thread から呼ぶ。1 つの処理（業務プロファイルの AI 構築の
job・メタデータ SQL の生成・Embedding の一括）の呼び出しは 1 つの実行器を共有し、429 を
受けたら全体で待って並行の数を下げる。

SDK の再試行との分担: この実行器を通す呼び出しは、client の再試行を切る（Enterprise AI の
direct client は ``max_retries=0``、OCI Generative AI の SDK は ``NoneRetryStrategy``）。
"""

from __future__ import annotations

import asyncio
import inspect
import random
from collections.abc import Callable, Mapping
from typing import Any, Literal

from pr_backend_core.bounded_calls import (
    BoundedCallAborted,
    BoundedCaller,
    BoundedCallFailed,
    BoundedCallPolicy,
    Disposition,
    ErrorKind,
    KindRule,
    SyncBoundedCaller,
    chain_classifiers,
)

from app.settings import Settings

from .enterprise_ai_client import EnterpriseAiDirectError

AiCallStage = Literal["ontology_build", "metadata_sql", "embedding", "quality_evaluation"]

STAGE_LABELS: dict[str, str] = {
    "ontology_build": "業務プロファイルの AI 構築",
    "metadata_sql": "メタデータ SQL の生成",
    "embedding": "埋め込み（Embedding）",
    "quality_evaluation": "SQL生成評価",
}

_CONCURRENCY_SETTINGS: dict[str, str] = {
    "ontology_build": "nl2sql_ontology_build_concurrency",
    "metadata_sql": "nl2sql_metadata_sql_concurrency",
    "embedding": "nl2sql_embedding_concurrency",
    "quality_evaluation": "nl2sql_quality_evaluation_concurrency",
}

# SQL生成評価の試行（SQL の生成・判定）の規則（#2059。RAG・Agent の品質評価と同じ）。
# 評価は試行ごとの結果（時間切れ・応答の誤りを含む）を記録するため、流量の制限（429）だけを
# 全体で待って送り直す。
# 時間切れ・5xx などはその試行の失敗のまま送り直さない（#2059 より前と同じ）。権限・認証・モデルの
# 設定の誤り・割り当ての使い切りは、残りの試行を始めずに評価を止める（同じ失敗をくり返さない）。
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

# HTTP 以外の失敗の code → 種類（HTTP の失敗は status で共通の分類が決める）。
_KINDS_BY_CODE: dict[str, ErrorKind] = {
    "response_format": ErrorKind.INVALID_OUTPUT,
    "refusal": ErrorKind.SAFETY,
    "incomplete": ErrorKind.TRUNCATED,
}


def nl2sql_error_kind(exc: BaseException) -> ErrorKind | None:
    """NL2SQL の client の例外のうち、共通の分類では決まらないものの種類。"""
    if isinstance(exc, EnterpriseAiDirectError) and exc.http_failure is None:
        return _KINDS_BY_CODE.get(exc.code)
    return None


def stage_concurrency(settings: Settings, stage: AiCallStage) -> int:
    """段の並行の上限（設定。1 以上）。"""
    return max(1, int(getattr(settings, _CONCURRENCY_SETTINGS[stage], 1) or 1))


async def _sleep(seconds: float) -> None:
    """再試行の待ち（テストは差し替えて実時間で待たない）。"""
    await asyncio.sleep(seconds)


def _random() -> float:
    """jitter の乱数（暗号の用途ではない）。"""
    return random.random()  # nosec B311 - backoff の jitter で、暗号の用途ではない


def make_ai_caller(settings: Settings, stage: AiCallStage) -> BoundedCaller:
    """段の実行器を作る（1 つの処理・1 つの job ごと）。"""
    policy = (
        BoundedCallPolicy(
            max_concurrency=stage_concurrency(settings, stage), rules=_EVALUATION_RULES
        )
        if stage == "quality_evaluation"
        else BoundedCallPolicy(max_concurrency=stage_concurrency(settings, stage))
    )
    return BoundedCaller(
        f"nl2sql.{stage}",
        policy,
        label=STAGE_LABELS.get(stage, stage),
        classifier=chain_classifiers(nl2sql_error_kind),
        sleep=lambda seconds: _sleep(seconds),
        random=lambda: _random(),
    )


def sync_ai_caller(settings: Settings, stage: AiCallStage) -> SyncBoundedCaller:
    """同期のコード（thread）から使う段の実行器（``with`` で使う）。"""
    return SyncBoundedCaller(make_ai_caller(settings, stage))


def _parameters(function: Callable[..., Any]) -> Mapping[str, inspect.Parameter]:
    try:
        return inspect.signature(function).parameters
    except (TypeError, ValueError):
        return {}


class BoundedEnterpriseAiClient:
    """Enterprise AI の direct client の呼び出しを、上限つきの並行の実行器に通す包み。

    ``generate`` だけを実行器に通し、ほか（``is_configured`` など）は元の client に渡す
    （画像は ``image_call`` で包む）。元の client が ``max_retries`` を受けるときは 0 を渡し、
    再試行を実行器に任せる。元の client が受けない引数（出力の上限など）は渡さない。
    thread safe。
    """

    def __init__(
        self, inner: Any, bridge: SyncBoundedCaller, *, unwrap_aborted: bool = False
    ) -> None:
        self._inner = inner
        self._bridge = bridge
        self._unwrap_aborted = unwrap_aborted

    @property
    def inner(self) -> Any:
        return self._inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _call(self, function: Callable[[], Any], *, label: str) -> str:
        """実行器で呼ぶ。件の失敗は元の例外で出す（呼び出し側の扱い・失敗の案内を変えない）。

        job を止める失敗（``BoundedCallAborted``。権限・設定の誤り・割り当ての使い切り）は、
        ``unwrap_aborted`` が偽ならそのまま出す（呼び出し側が処理の全体を止める）。
        """
        try:
            return str(self._bridge.call(lambda _attempt: function(), label=label))
        except BoundedCallAborted as exc:
            cause = exc.__cause__
            if self._unwrap_aborted and isinstance(cause, Exception):
                raise cause  # noqa: B904 - 元の例外の原因の連鎖を変えずに出し直す
            raise
        except BoundedCallFailed as exc:
            cause = exc.__cause__
            if isinstance(cause, Exception):
                raise cause  # noqa: B904 - 元の例外の原因の連鎖を変えずに出し直す
            raise

    def generate(
        self,
        *,
        prompt: str,
        context: str,
        system_prompt: str,
        timeout_seconds: float | None = None,
        max_output_tokens: int | None = None,
        max_retries: int | None = None,
        response_format: Mapping[str, Any] | None = None,
    ) -> str:
        del max_retries  # 再試行は実行器が受け持つ。
        function = self._inner.generate
        parameters = _parameters(function)
        kwargs: dict[str, Any] = {
            "prompt": prompt,
            "context": context,
            "system_prompt": system_prompt,
            "response_format": response_format,
        }
        if max_output_tokens is not None and "max_output_tokens" in parameters:
            kwargs["max_output_tokens"] = max_output_tokens
        if timeout_seconds is not None and "timeout_seconds" in parameters:
            kwargs["timeout_seconds"] = timeout_seconds
        if "max_retries" in parameters:
            kwargs["max_retries"] = 0
        return self._call(lambda: function(**kwargs), label="generate")


def image_call(function: Callable[..., Any], bridge: SyncBoundedCaller) -> Callable[..., str]:
    """``generate_from_image`` を実行器に通す関数にする（受けるなら ``max_retries=0``）。

    件の失敗は元の例外で出し、job を止める失敗（``BoundedCallAborted``）はそのまま出す。
    """
    retries = {"max_retries": 0} if "max_retries" in _parameters(function) else {}

    def call(image_bytes: bytes, prompt: str, **kwargs: Any) -> str:
        try:
            return str(
                bridge.call(
                    lambda _attempt: function(image_bytes, prompt, **kwargs, **retries),
                    label="image",
                )
            )
        except BoundedCallFailed as exc:
            cause = exc.__cause__
            if isinstance(cause, Exception):
                raise cause  # noqa: B904 - 元の例外の原因の連鎖を変えずに出し直す
            raise

    return call
