"""API エラーを漸進互換の problem 契約へ正規化する。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from fastapi import HTTPException, Request
from pr_backend_core.api import InvalidCursorError
from pr_backend_core.api.validation import validation_field_errors
from pr_backend_core.observability import generate_request_id
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse


def _is_none(value: object) -> bool:
    return value is None


class ApiFieldProblem(BaseModel):
    """入力 payload 上の問題。pointer は RFC 6901 JSON Pointer。

    入力の検証エラー（422）は、利用者に見せる位置（`location`）と技術的な原文
    （`raw_location` / `raw_message`）も持つ（#1065）。無いときは応答に出さない。
    """

    pointer: str
    code: str
    message: str
    location: str | None = Field(default=None, exclude_if=_is_none)
    raw_location: str | None = Field(default=None, exclude_if=_is_none)
    raw_message: str | None = Field(default=None, exclude_if=_is_none)


class ApiProblem(BaseModel):
    """RFC 9457 の語彙を既存 envelope 内で段階導入する。"""

    type: str
    title: str
    status: int
    detail: str
    code: str
    request_id: str
    retryable: bool = False
    field_errors: list[ApiFieldProblem] = Field(default_factory=list)


_STATUS_TITLES: dict[int, str] = {
    400: "リクエストを処理できません",
    401: "認証が必要です",
    403: "この操作を実行する権限がありません",
    404: "対象が見つかりません",
    405: "この操作方法は利用できません",
    409: "現在の状態では操作を完了できません",
    422: "入力内容を確認してください",
    429: "リクエストが集中しています",
    500: "サーバー内部でエラーが発生しました",
    502: "外部サービスから応答を取得できません",
    503: "サービスを一時的に利用できません",
    504: "処理がタイムアウトしました",
}

_STATUS_CODES: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "REQUEST_VALIDATION_FAILED",
    429: "RATE_LIMITED",
    500: "INTERNAL_SERVER_ERROR",
    502: "BAD_GATEWAY",
    503: "SERVICE_UNAVAILABLE",
    504: "GATEWAY_TIMEOUT",
}

_RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})


def invalid_cursor_exception(exc: InvalidCursorError) -> HTTPException:
    """壊れたページングの cursor（共通 codec の `InvalidCursorError`）を 422 にする（#1266）。

    利用者の文言は製品の既存の形（「cursor が不正です。」）にそろえ、codec の理由は detail に
    含めない。
    """
    return HTTPException(status_code=422, detail="cursor が不正です。")


def request_id_for(request: Request) -> str:
    """middleware が採番した request id を安全に取得する。"""

    value = getattr(request.state, "request_id", "")
    if isinstance(value, str) and value:
        return value
    generated = generate_request_id(request.headers.get("X-Request-ID"))
    request.state.request_id = generated
    return generated


def default_problem_title(status_code: int) -> str:
    return _STATUS_TITLES.get(status_code, "リクエストの処理に失敗しました")


def default_problem_code(status_code: int) -> str:
    return _STATUS_CODES.get(status_code, f"HTTP_{status_code}")


def problem_type_for(code: str) -> str:
    slug = code.strip().lower().replace("_", "-") or "unknown"
    return f"urn:nl2sql:problem:{slug}"


def api_problem_response(
    request: Request,
    *,
    status_code: int,
    detail: str,
    code: str | None = None,
    title: str | None = None,
    retryable: bool | None = None,
    field_errors: Iterable[ApiFieldProblem | Mapping[str, str]] = (),
    headers: Mapping[str, str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> JSONResponse:
    """既存 envelope を保ったまま構造化 problem を追加する。"""

    resolved_code = code or default_problem_code(status_code)
    request_id = request_id_for(request)
    normalized_field_errors = [
        item if isinstance(item, ApiFieldProblem) else ApiFieldProblem.model_validate(item)
        for item in field_errors
    ]
    problem = ApiProblem(
        type=problem_type_for(resolved_code),
        title=title or default_problem_title(status_code),
        status=status_code,
        detail=detail,
        code=resolved_code,
        request_id=request_id,
        retryable=(status_code in _RETRYABLE_STATUSES if retryable is None else retryable),
        field_errors=normalized_field_errors,
    )
    response_headers = dict(headers or {})
    if request_id:
        response_headers["X-Request-ID"] = request_id
    content: dict[str, Any] = {
        "data": None,
        "error_messages": [detail],
        "warning_messages": [],
        "error_code": resolved_code,
        "problem": problem.model_dump(mode="json"),
    }
    if extra:
        content.update(extra)
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers=response_headers,
    )


def validation_field_problems(errors: Sequence[Mapping[str, Any]]) -> list[ApiFieldProblem]:
    """Pydantic/FastAPI validation errors を安全な日本語 field error へ変換する。

    文と位置は 3 製品共通の整形（`pr_backend_core.api.validation`。#1065）に任せ、
    技術的な原文（`raw_location` / `raw_message`）は画面の「詳細」用に残す。
    """

    return [ApiFieldProblem.model_validate(item) for item in validation_field_errors(errors)]
