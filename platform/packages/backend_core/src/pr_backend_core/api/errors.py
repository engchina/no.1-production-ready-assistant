"""例外を ApiResponse 形式へ統一するハンドラ群（サービス横断で共通）。"""

import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse

from ..schemas import ApiResponse
from .validation import DEFAULT_VALIDATION_MESSAGE, validation_error_response

logger = logging.getLogger(__name__)

DEFAULT_UNHANDLED_MESSAGE = "サーバー内部でエラーが発生しました。時間をおいて再度お試しください。"
DEFAULT_NOT_FOUND_MESSAGE = "リソースが見つかりません。"
DEFAULT_METHOD_NOT_ALLOWED_MESSAGE = "許可されていない HTTP メソッドです。"

# DEFAULT_VALIDATION_MESSAGE は検証エラーの整形（`.validation`）へ移した。互換のため再公開する。
__all__ = [
    "DEFAULT_METHOD_NOT_ALLOWED_MESSAGE",
    "DEFAULT_NOT_FOUND_MESSAGE",
    "DEFAULT_UNHANDLED_MESSAGE",
    "DEFAULT_VALIDATION_MESSAGE",
    "api_error_response",
    "http_exception_messages",
    "install_exception_handlers",
]


def api_error_response(
    status_code: int,
    messages: list[str],
    *,
    headers: Mapping[str, str] | None = None,
    request_id: str | None = None,
) -> JSONResponse:
    """ApiResponse 形式のエラーレスポンスを返す。"""
    body = ApiResponse[object](data=None, error_messages=messages)
    response_headers = dict(headers or {})
    if request_id:
        response_headers["X-Request-ID"] = request_id
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(mode="json"),
        headers=response_headers,
    )


def http_exception_messages(
    detail: Any,
    status_code: int,
    *,
    not_found_message: str = DEFAULT_NOT_FOUND_MESSAGE,
    method_not_allowed_message: str = DEFAULT_METHOD_NOT_ALLOWED_MESSAGE,
    fallback: str = "リクエストの処理に失敗しました。",
) -> list[str]:
    """HTTPException.detail を API エラー配列へ正規化する。"""
    if isinstance(detail, str):
        if status_code == 404 and detail == "Not Found":
            return [not_found_message]
        if status_code == 405 and detail == "Method Not Allowed":
            return [method_not_allowed_message]
        return [detail]
    if isinstance(detail, list):
        return [str(item) for item in detail]
    if isinstance(detail, dict):
        return [str(detail)]
    return [fallback]


def _default_request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def install_exception_handlers(
    app: FastAPI,
    *,
    unhandled_message: str = DEFAULT_UNHANDLED_MESSAGE,
    request_id_getter: Callable[[Request], str | None] = _default_request_id,
) -> None:
    """HTTPException / 検証エラー / 未処理例外を ApiResponse 形式へ統一する。

    request id は既定で `request.state.request_id` を参照する（MetricsMiddleware が設定）。
    """

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        return api_error_response(
            exc.status_code,
            http_exception_messages(exc.detail, exc.status_code),
            headers=exc.headers,
            request_id=request_id_getter(request),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # 利用者向けの日本語の文と位置にし、技術的な原文は problem.field_errors に残す（#1065）。
        return validation_error_response(exc.errors(), request_id=request_id_getter(request))

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = request_id_getter(request)
        logger.exception(
            "unhandled_api_error",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "exception_type": type(exc).__name__,
            },
        )
        return api_error_response(500, [unhandled_message], request_id=request_id)
