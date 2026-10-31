"""回答の生成の失敗を、利用者向けの文と「詳細」の項目にする（#2023）。

検索（REST・SSE）・チャット・MCP・品質評価の 1 ケースは、回答の pipeline の例外を同じ形で返す。

- LLM の応答が未完了（`rag_engine.adapters.oci.IncompleteResponse`。Responses API の
  `status=incomplete`）で終わった失敗は、理由ごとの文（出力の上限・安全の判定）と直し方を返す。
  送り直し（上限を広げる・推論の量を下げる）は adapter が済ませている。
- それ以外の予期しない例外は、定型文（``fallback``）のまま返す（内部の文を利用者に出さない）。
- AI の API の失敗（``describe_answer_api_failure``）は、種類・再試行の目安・中断した工程の
  文にする。MCP のツール（`rag_search` など）は種類つきのツールのエラー（#2475）、REST の
  検索・業務ガイドの下書きの試答は種類ごとの HTTP status（#2667）で返す。呼び出し元・利用者が、
  検索の失敗を根拠の不足と区別し、待つか設定を確かめるかを判断できるようにするため。

どちらも「詳細」（messaging.md §10.3）に出す項目（エラーの種類・失敗した工程・未完了の理由・
上限と token）を持つ。trace_id は呼び出し側が持つので、ここでは扱わない。
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from pr_backend_core.bounded_calls import KIND_LABELS, ErrorKind, classify_error
from pr_backend_core.logging import safe_error_code
from rag_engine.adapters.oci import IncompleteResponse
from rag_engine.generation.execution_record import AnswerExecutionError

# 未完了の応答の error_type（画面・SSE・MCP の error code）。3 製品の共通の error code と同じ値。
ANSWER_INCOMPLETE_ERROR_TYPE = "IncompleteResponse"
ANSWER_INCOMPLETE_ERROR_CODE = "ENTERPRISE_AI_OUTPUT_INCOMPLETE"
# 工程の記録（`execution_steps`）で、例外で終わった工程の status
# （`rag_engine.generation.execution_record`）。
_FAILED_STEP_STATUS = "エラーで中断"


@dataclass(frozen=True)
class AnswerFailure:
    """回答の失敗の、利用者向けの文と「詳細」の項目。"""

    message: str
    error_type: str
    # 未完了の理由（`incomplete_details.reason`）。未完了でなければ None。
    reason: str | None = None
    # 「詳細」に出す技術的な文（出力の上限・使った token・推論の量）。
    detail: str | None = None
    # 例外で終わった工程の名前（分かるときだけ）。
    stage: str | None = None
    # エラーコード（ORA-03146 など。例外の本文は持たない。画面の「詳細」に出す。#2342）。
    error_code: str | None = None

    @property
    def incomplete(self) -> bool:
        return self.reason is not None

    def event_fields(self) -> dict[str, Any]:
        """SSE の `error` event に足す項目（値の無いものは入れない）。"""
        fields: dict[str, Any] = {"message": self.message, "error_type": self.error_type}
        for key in ("reason", "detail", "stage", "error_code"):
            if (value := getattr(self, key)) is not None:
                fields[key] = value
        return fields


def _causes(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )


def answer_error_identity(exc: BaseException) -> tuple[str, str | int | None]:
    """実行記録のラッパーの内側の型と安全なコードだけを返し、本文は保存しない。"""
    chain = tuple(_causes(exc))
    source = chain[-1] if isinstance(exc, AnswerExecutionError) else exc
    code = next((code for item in chain if (code := safe_error_code(item)) is not None), None)
    return type(source).__name__, code


def find_incomplete_response(exc: BaseException) -> IncompleteResponse | None:
    """例外の連鎖の中の、利用者向けの文を持つ未完了の応答（一番外の IncompleteResponse）。"""
    found: IncompleteResponse | None = None
    for item in _causes(exc):
        if isinstance(item, IncompleteResponse):
            if item.user_message:
                return item
            found = found or item
    return found


def failed_stage(exc: BaseException) -> str | None:
    """回答の工程の記録（`AnswerExecutionError.execution_steps`）で、例外で終わった最も内側の工程の名前。"""
    for item in _causes(exc):
        steps = getattr(item, "execution_steps", None)
        if not steps:
            continue
        failed = [
            str(step.get("name"))
            for step in steps
            if isinstance(step, dict)
            and step.get("status") == _FAILED_STEP_STATUS
            and step.get("name")
        ]
        if failed:
            return failed[-1]
    return None


def describe_answer_failure(exc: BaseException, fallback: str) -> AnswerFailure:
    """回答の失敗を、利用者向けの文と「詳細」の項目にする。

    未完了でなければ ``fallback`` の定型文。
    """
    stage = failed_stage(exc)
    incomplete = find_incomplete_response(exc)
    if incomplete is not None:
        return AnswerFailure(
            message=incomplete.user_message or fallback,
            error_type=ANSWER_INCOMPLETE_ERROR_TYPE,
            reason=incomplete.reason or "unknown",
            detail=incomplete.detail or None,
            stage=stage,
        )
    error_type, code = answer_error_identity(exc)
    return AnswerFailure(
        message=fallback,
        error_type=error_type,
        stage=stage,
        error_code=str(code) if code is not None else None,
    )


# 回答の pipeline の中で AI の API（LLM・Embedding・Rerank）の呼び出しが失敗したときの、MCP の
# ツールのエラー（#2475）。検索を終えられなかったことを、呼び出し元（業務 Agent）が「資料に根拠が
# 無い」と区別できるように、汎用の内部エラーにせず、種類つきの 5xx で返す。
ANSWER_API_FAILED_ERROR_CODE = "RAG_ANSWER_AI_API_FAILED"
# 呼び先の SDK・HTTP の例外の module（この例外が原因の連鎖にあれば AI の API の失敗とみなす）。
_API_EXCEPTION_MODULES = ("openai", "httpx", "httpcore", "oci")
# 呼び先が返した error code として残す値の形（本文・識別子を残さない）。
_SAFE_PROVIDER_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
# 種類ごとの HTTP status（呼び先の失敗なので 5xx。時間切れは 504、流量・一時的な不可用は 503）。
_KIND_STATUS: dict[ErrorKind, int] = {
    ErrorKind.RATE_LIMITED: 503,
    ErrorKind.QUOTA_EXHAUSTED: 503,
    ErrorKind.UNAVAILABLE: 503,
    ErrorKind.TIMEOUT: 504,
}
# 待てば直る見込みの種類（それ以外は設定・入力を確かめる案内にする）。
_TRANSIENT_KINDS = frozenset(
    {
        ErrorKind.RATE_LIMITED,
        ErrorKind.UNAVAILABLE,
        ErrorKind.SERVER_ERROR,
        ErrorKind.NETWORK,
        ErrorKind.TIMEOUT,
        ErrorKind.UNKNOWN,
    }
)


@dataclass(frozen=True)
class AnswerApiFailure:
    """AI の API の失敗の、利用者向けの文と安全な分類。

    質問・文書・呼び先の応答の本文・資格情報は持たない。
    """

    kind: str
    status: int
    message: str
    # 呼び先の例外の型（`APIError` など）と、呼び先が返した error code（形が安全なときだけ）。
    error_type: str
    provider_status: int | None = None
    provider_code: str | None = None
    # 例外で終わった回答の工程の名前。
    stage: str | None = None
    # 呼び先が示した待ちの秒数（`Retry-After`。示さなければ None）。
    retry_after_seconds: int | None = None

    def details(self) -> dict[str, Any]:
        """MCP のツールのエラーの `details`（値の無いものは入れない）。"""
        fields: dict[str, Any] = {"kind": self.kind, "error_type": self.error_type}
        for key in ("provider_status", "provider_code", "stage", "retry_after_seconds"):
            if (value := getattr(self, key)) is not None:
                fields[key] = value
        return fields

    def headers(self) -> dict[str, str] | None:
        """REST の応答の header（呼び先が待ちの秒数を示したときだけ `Retry-After`）。"""
        if self.retry_after_seconds is None:
            return None
        return {"Retry-After": str(self.retry_after_seconds)}


def describe_answer_api_failure(exc: BaseException) -> AnswerApiFailure | None:
    """回答の pipeline の例外が AI の API の失敗なら、その分類（そうでなければ None）。

    原因の連鎖（`AnswerExecutionError` が包んだ例外）に SDK・HTTP の例外があるときだけ分類する。
    未完了の応答（`IncompleteResponse`）は別の文で返すので対象にしない。
    """
    if find_incomplete_response(exc) is not None:
        return None
    api_error = next(
        (
            item
            for item in _causes(exc)
            if type(item).__module__.split(".", 1)[0] in _API_EXCEPTION_MODULES
        ),
        None,
    )
    if api_error is None:
        return None
    classified = classify_error(exc)
    kind = classified.kind
    label = KIND_LABELS.get(kind, KIND_LABELS[ErrorKind.UNKNOWN])
    retry_after = (
        max(1, math.ceil(classified.retry_after)) if classified.retry_after is not None else None
    )
    stage = failed_stage(exc)
    where = f"。中断した工程: {stage}" if stage else ""
    provider_code = getattr(api_error, "code", None)
    return AnswerApiFailure(
        kind=kind.value,
        status=_KIND_STATUS.get(kind, 502),
        message=(
            "回答の作成中に AI の API の呼び出しに失敗したため、検索を終えられませんでした"
            f"（{label}{where}）。{_advice(kind, retry_after)}"
        ),
        error_type=type(api_error).__name__,
        provider_status=classified.status,
        provider_code=provider_code
        if isinstance(provider_code, str) and _SAFE_PROVIDER_CODE.match(provider_code)
        else None,
        stage=stage,
        retry_after_seconds=retry_after,
    )


def _advice(kind: ErrorKind, retry_after: int | None) -> str:
    """種類ごとの、利用者が次にすること（待つか、設定・割り当てを確かめてもらうか）。"""
    wait = f"{retry_after} 秒ほど待ってから" if retry_after is not None else "時間をおいて"
    if kind is ErrorKind.QUOTA_EXHAUSTED:
        # 割り当て（期間の上限）は、すぐに送り直しても戻らない。
        return (
            f"AI の API の割り当てが戻るまで{wait}再試行してください。"
            "続くときは管理者に割り当てを確かめてもらってください。"
        )
    if kind in _TRANSIENT_KINDS:
        return f"{wait}再試行してください。"
    return "続くときは管理者に AI の API の設定を確かめてもらってください。"
