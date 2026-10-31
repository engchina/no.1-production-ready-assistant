"""モデルが受け付けない引数を外して送り直す httpx の client（#3539）。

判定・記憶・payload の絞り込みは :mod:`pr_backend_core.llm_request_params`。ここは
httpx の ``send`` を包み、JSON の POST だけを対象に次を行う。

1. 送る前: この接続（URL）とモデル（本文の ``model``）で受け付けないと覚えた引数を外す。
2. HTTP 400 の本文が許可の一覧の引数を「受け付けない」と名指ししたら、覚えて、その引数を
   外して 1 回だけ送り直す。送り直しがまた 400 で別の引数を名指ししたら、覚えるだけで
   送り直さない（次の呼び出しから最初に外す）。

client の ``send`` を包む（:class:`ParameterDroppingClient` /
:class:`AsyncParameterDroppingClient`）ので、``client.post`` / ``client.stream`` と、OpenAI SDK
（``OpenAI(http_client=...)`` / ``AsyncOpenAI(http_client=...)``）の通常の呼び出しと
streaming の両方に効く。既存の client のままなら :func:`send_dropping_parameters` /
:func:`asend_dropping_parameters` に ``client.send`` を渡す。環境のプロキシの扱い
（``trust_env``・``mounts``）は httpx の既定のまま変えない（``transport`` を差し替えると環境の
プロキシを使わなくなるため、transport ではなく ``send`` を包む）。
httpx は使う側が依存に入れる（``pr_backend_core`` は httpx に依存しない）。
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Protocol, cast

import httpx

from pr_backend_core.llm_request_params import (
    droppable_parameter,
    has_remembered_parameters,
    note_dropped_parameters,
    rejected_parameter,
    remember_dropped_parameter,
    remembered_parameters,
    without_parameters,
)

__all__ = [
    "AsyncParameterDroppingClient",
    "ParameterDroppingClient",
    "asend_dropping_parameters",
    "send_dropping_parameters",
]


class _Request(Protocol):
    """httpx の ``Request`` の形（httpx と、OpenAI SDK の新しい版が使う httpx2 の両方）。"""

    method: str
    extensions: Any

    @property
    def url(self) -> Any: ...

    @property
    def headers(self) -> Any: ...

    @property
    def content(self) -> bytes: ...


class _Response(Protocol):
    @property
    def status_code(self) -> int: ...

    @property
    def content(self) -> bytes: ...


class _SyncResponse(_Response, Protocol):
    def read(self) -> bytes: ...

    def close(self) -> None: ...


class _AsyncResponse(_Response, Protocol):
    async def aread(self) -> bytes: ...

    async def aclose(self) -> None: ...


def _is_json_post(request: _Request) -> bool:
    content_type = str(request.headers.get("content-type", ""))
    return request.method == "POST" and "json" in content_type.lower()


def _json_body(request: _Request) -> dict[str, Any] | None:
    if not _is_json_post(request):
        return None
    try:
        body = json.loads(request.content)
    except Exception:  # noqa: BLE001 - 読めない streaming の本文（RequestNotRead）・JSON でない本文
        return None
    return body if isinstance(body, dict) else None


def _model_id(body: dict[str, Any]) -> str:
    return str(body.get("model") or "")


def _rebuilt[RequestT: _Request](request: RequestT, body: dict[str, Any]) -> RequestT:
    """本文を ``body`` に替えた同じ型の要求（Content-Length は新しい本文から付け直す）。"""
    headers = type(request.headers)(request.headers)
    headers.pop("content-length", None)
    content = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    factory = cast(Any, type(request))
    return cast(
        RequestT,
        factory(
            request.method,
            request.url,
            headers=headers,
            content=content.encode("utf-8"),
            extensions=request.extensions,
        ),
    )


def _before_send[RequestT: _Request](request: RequestT) -> tuple[RequestT, dict[str, Any] | None]:
    """覚えた引数を外した要求と、読んだ本文（読まなかったら None）。"""
    if not has_remembered_parameters():
        return request, None
    body = _json_body(request)
    if body is None:
        return request, None
    names = remembered_parameters(str(request.url), _model_id(body))
    filtered, removed = without_parameters(body, names)
    if not removed:
        return request, body
    note_dropped_parameters(removed)
    return _rebuilt(request, filtered), filtered


def _learn(
    request: _Request, body: dict[str, Any] | None, content: bytes
) -> tuple[dict[str, Any], str] | None:
    """400 の本文が外してよい引数を名指ししていれば覚え、(本文, 引数の名前) を返す。"""
    body = body if body is not None else _json_body(request)
    if body is None:
        return None
    name = droppable_parameter(rejected_parameter(400, content), body)
    if name is None:
        return None
    remember_dropped_parameter(str(request.url), _model_id(body), name)
    return body, name


def _retry_request[RequestT: _Request](
    request: RequestT, learned: tuple[dict[str, Any], str]
) -> RequestT:
    body, name = learned
    names = remembered_parameters(str(request.url), _model_id(body)) | {name}
    filtered, removed = without_parameters(body, names)
    note_dropped_parameters(removed)
    return _rebuilt(request, filtered)


def send_dropping_parameters[RequestT: _Request, SyncResponseT: _SyncResponse](
    send: Callable[[RequestT], SyncResponseT], request: RequestT
) -> SyncResponseT:
    """``send``（httpx の client の ``send``）で送り、受け付けない引数を外して 1 回だけ送り直す。

    既存の ``httpx.Client`` をそのまま使う呼び出し（JSON の POST）は、
    ``send_dropping_parameters(client.send, client.build_request("POST", url, json=...))``
    のように使う。
    """
    request, body = _before_send(request)
    response = send(request)
    if response.status_code != 400 or not _is_json_post(request):
        return response
    response.read()
    learned = _learn(request, body, response.content)
    if learned is None:
        return response
    response.close()
    retry = _retry_request(request, learned)
    second = send(retry)
    if second.status_code == 400:
        # 送り直しは 1 回まで。別の引数を名指ししていれば覚えて、次の呼び出しから外す。
        second.read()
        _learn(retry, None, second.content)
    return second


async def asend_dropping_parameters[RequestT: _Request, AsyncResponseT: _AsyncResponse](
    send: Callable[[RequestT], Awaitable[AsyncResponseT]], request: RequestT
) -> AsyncResponseT:
    """:func:`send_dropping_parameters` の非同期版（``httpx.AsyncClient`` などの ``send``）。

    httpx 以外の同じ形の client（OpenAI SDK の新しい版が使う httpx2）でも使える。
    """
    request, body = _before_send(request)
    response = await send(request)
    if response.status_code != 400 or not _is_json_post(request):
        return response
    await response.aread()
    learned = _learn(request, body, response.content)
    if learned is None:
        return response
    await response.aclose()
    retry = _retry_request(request, learned)
    second = await send(retry)
    if second.status_code == 400:
        await second.aread()
        _learn(retry, None, second.content)
    return second


class ParameterDroppingClient(httpx.Client):
    """受け付けない引数を外して 1 回だけ送り直す ``httpx.Client``（引数は同じ）。

    OpenAI SDK の ``OpenAI(http_client=...)`` に渡す（通常の呼び出しと streaming の両方に効く）。
    """

    def send(self, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        parent = super().send
        return send_dropping_parameters(lambda item: parent(item, *args, **kwargs), request)


class AsyncParameterDroppingClient(httpx.AsyncClient):
    """受け付けない引数を外して 1 回だけ送り直す ``httpx.AsyncClient``（引数は同じ）。"""

    async def send(self, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        parent = super().send
        return await asend_dropping_parameters(lambda item: parent(item, *args, **kwargs), request)
