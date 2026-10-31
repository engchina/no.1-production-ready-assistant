"""MCP サーバーの共通ヘルパー（#230）。

RAG / NL2SQL が Agent にツールを公開するための最小の MCP（Model Context Protocol）実装。
Streamable HTTP の「POST に JSON で応答する」モードを基本にする（resources・prompts は持たない）。
`tools/call` に `params._meta.progressToken` があり、`Accept` が `text/event-stream` を
含むときだけ、応答を SSE にして実行中の進み具合（`notifications/progress`）を送り、最後に
結果を送る（#1864。長くかかるツールで、進み具合を受けて待ち時間を延ばす client が時間切れに
ならないように）。
読み取り専用のツールの `tools/call` は、client が接続を切ったら実行を取り消す（#2691。Agent の
Run の取消・外部の client の中断。応答を受け取る相手がいない処理の検索・LLM の呼び出しを続けない）。
この server は切れた接続の応答を後で受け取る仕組み（SSE の再開）を持たないため、切断は取消と同じに
扱う。書き込みのツールは途中で止めず、最後まで実行する。
認証と利用者の特定は製品の認可 dependency（サービストークン）が行い、ここではツールの権限を
`has_any_permission` で判定する。

ponytail: 公式 SDK を入れずに `initialize` / `ping` / `tools/list` / `tools/call` だけを手書きする。
`nextCursor` の paging が必要になったら、この module に足す。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic
from typing import Any

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool

from .api.validation import validation_tool_errors
from .observability.tracing import observed_span

logger = logging.getLogger(__name__)

LATEST_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({LATEST_PROTOCOL_VERSION, "2025-03-26"})

# JSON-RPC のエラーコード
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602

# ツールの実行エラー（`isError: true` の `error_code`）
TOOL_FORBIDDEN_CODE = "MCP_TOOL_FORBIDDEN"
TOOL_ARGUMENTS_INVALID_CODE = "MCP_TOOL_ARGUMENTS_INVALID"
TOOL_INTERNAL_ERROR_CODE = "MCP_TOOL_INTERNAL_ERROR"

HasAnyPermission = Callable[[frozenset[str]], bool]

# 進み具合の通知（`notifications/progress`）の間隔（秒）。ツールが段階を知らせなくても、この間隔で
# 経過の秒を送る（client の待ち時間の延長と、利用者への経過の表示のため。#1864）。
PROGRESS_HEARTBEAT_SECONDS = 10.0

# client が接続を切ったときに返す status（応答は届かない。アクセスログで取消と分かるように nginx の
# 「client closed request」と同じ値にする）。
CLIENT_CLOSED_REQUEST_STATUS = 499

# 実行中のツールの進み具合を受け取る関数（`report_tool_progress` が呼ぶ）。progressToken の
# 無い呼び出しでは None。
_progress_reporter: ContextVar[Callable[[str], None] | None] = ContextVar(
    "mcp_progress_reporter", default=None
)


def report_tool_progress(message: str) -> None:
    """実行中のツールの段階を、呼び出し元へ進み具合（`notifications/progress`）で知らせる。

    client が progressToken を渡さなかった呼び出し（JSON の応答）では何もしない。ツールの
    thread から呼んでもよい（event loop へ渡す）。通知の失敗はツールを止めない。
    """
    reporter = _progress_reporter.get()
    if reporter is not None and message:
        reporter(message)


class McpToolError(Exception):
    """ツールが利用者に返す業務エラー（`isError: true`）。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        details: Mapping[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class McpToolResult:
    """`structuredContent` に加えて、`content` に足すブロック（画像など）を返すときの結果。

    `output` は通常の handler の戻り値と同じ（`structuredContent` と `content` の text になる）。
    `content` は MCP の content ブロック（例: `{"type": "image", "data": <base64>,
    "mimeType": "image/png"}`）で、text の後ろに足す。大きなデータ（画像）を
    `structuredContent` に入れず、呼び出し側のモデルの文脈を膨らませないために使う（#1282）。
    """

    output: Any
    content: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class McpTool:
    """公開するツール。

    - `handler`: 検証済みの `input_model` を受け取り、`BaseModel` か dict を返す（画像などの
      content を足すときは `McpToolResult`）。
      同期関数は threadpool で実行する（FastAPI の同期 route と同じ）。
    - `permissions`: すべてのグループを満たすこと（グループ内はどれか 1 つ）。
      例: `(frozenset({"a"}), frozenset({"b"}))` は a と b の両方が必要。
    - `output_model`: 指定すると `tools/list` に `outputSchema` を出す（#250）。handler はこの形の
      `structuredContent` を返すこと（呼び出し側と契約で出力の形を確かめられる）。
    - `title`: 人が読む表示名（MCP の仕様のツールの `title`）。指定すると `tools/list` に出す。
      呼び出し側の画面（Agent の処理の経過など）が内部名の代わりに出す。モデルの判断には使わない。
    - `listed`: False のツールは `tools/list` に出さない（`tools/call` では呼べ、権限は同じに
      確かめる）。製品間の画面の操作だけが名前で呼ぶ内部のツール（Agent の画面が保存した根拠を
      出し直すときの確かめなど。#3252）に使い、モデルと外部の MCP クライアントに見せない。
    - `destructive` / `idempotent` / `open_world`: 指定したツールだけ `annotations` に
      `destructiveHint` / `idempotentHint` / `openWorldHint` を出す（MCP 2025-06-18 の tool
      annotations。書き込みのツールの性質を呼び出し側に示す。#3240）。None（既定）は出さない。
      `read_only` の扱い（切断での取消・再試行。#2691・#854）は変えない。
    """

    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[Any], Any]
    permissions: tuple[frozenset[str], ...] = ()
    read_only: bool = True
    output_model: type[BaseModel] | None = None
    title: str | None = None
    listed: bool = True
    destructive: bool | None = None
    idempotent: bool | None = None
    open_world: bool | None = None

    def allowed(self, has_any_permission: HasAnyPermission) -> bool:
        return all(has_any_permission(group) for group in self.permissions)

    def annotations(self) -> dict[str, bool]:
        annotations = {"readOnlyHint": self.read_only}
        for key, value in (
            ("destructiveHint", self.destructive),
            ("idempotentHint", self.idempotent),
            ("openWorldHint", self.open_world),
        ):
            if value is not None:
                annotations[key] = value
        return annotations

    def descriptor(self) -> dict[str, Any]:
        descriptor: dict[str, Any] = {"name": self.name}
        if self.title:
            descriptor["title"] = self.title
        descriptor |= {
            "description": self.description,
            "inputSchema": self.input_model.model_json_schema(),
            "annotations": self.annotations(),
        }
        if self.output_model is not None:
            descriptor["outputSchema"] = self.output_model.model_json_schema(mode="serialization")
        return descriptor


class McpServer:
    def __init__(
        self,
        *,
        name: str,
        version: str,
        tools: Iterable[McpTool],
        instructions: str | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.instructions = instructions
        self.tools = {tool.name: tool for tool in tools}

    async def handle(
        self, message: Any, *, has_any_permission: HasAnyPermission
    ) -> dict[str, Any] | None:
        """JSON-RPC の 1 メッセージを処理する。通知・応答なら None。"""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "JSON-RPC 2.0 の request を 1 件送ってください。")
        if "id" not in message:
            return None  # 通知（notifications/initialized など）と、client からの応答
        request_id = message["id"]
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(method, str) or not isinstance(params, dict):
            return _error(request_id, INVALID_REQUEST, "method と params を確認してください。")
        if method == "initialize":
            return _result(request_id, self._initialize(params))
        if method == "ping":
            return _result(request_id, {})
        if method == "tools/list":
            tools = [
                t.descriptor()
                for t in self.tools.values()
                if t.listed and t.allowed(has_any_permission)
            ]
            return _result(request_id, {"tools": tools})
        if method == "tools/call":
            return await self._call_tool(request_id, params, has_any_permission)
        return _error(request_id, METHOD_NOT_FOUND, f"未対応の method です: {method}")

    def cancellable_tool(self, message: Any) -> McpTool | None:
        """client の切断で取り消してよい `tools/call` のツール（読み取り専用だけ。#2691）。"""
        if not isinstance(message, dict) or message.get("method") != "tools/call":
            return None
        if "id" not in message:
            return None
        params = message.get("params")
        name = params.get("name") if isinstance(params, dict) else None
        tool = self.tools.get(name) if isinstance(name, str) else None
        return tool if tool is not None and tool.read_only else None

    def _initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    async def _call_tool(
        self, request_id: Any, params: Mapping[str, Any], has_any_permission: HasAnyPermission
    ) -> dict[str, Any]:
        tool = self.tools.get(str(params.get("name", "")))
        if tool is None:
            return _error(request_id, INVALID_PARAMS, f"未知のツールです: {params.get('name')}")
        arguments = params.get("arguments") or {}
        if not tool.allowed(has_any_permission):
            return _result(
                request_id,
                tool_error(TOOL_FORBIDDEN_CODE, "このツールを利用する権限がありません。"),
            )
        try:
            validated = tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            # 位置は cases[0].query の形、文は日本語にする（原文は type / raw_message。#1065）。
            errors = validation_tool_errors(exc.errors(include_url=False, include_input=False))
            return _result(
                request_id,
                tool_error(
                    TOOL_ARGUMENTS_INVALID_CODE,
                    "ツールの引数が正しくありません。",
                    details={"errors": errors},
                ),
            )
        try:
            with observed_span(
                "gen_ai.execute_tool", attributes={"gen_ai.tool.name": tool.name}
            ) as span:
                span.set_call_input(arguments, schema=tool.input_model.model_json_schema())
                output = await _invoke(tool.handler, validated)
                span.set_call_output(output)
        except McpToolError as exc:
            return _result(
                request_id,
                tool_error(exc.code, exc.message, status=exc.status, details=exc.details),
            )
        except Exception as exc:  # noqa: BLE001 - 業務エラーを isError にし、それ以外は隠す
            converted = _http_error(exc)
            if converted is None:
                logger.exception("mcp tool failed", extra={"tool": tool.name})
                converted = tool_error(TOOL_INTERNAL_ERROR_CODE, "ツールの実行に失敗しました。")
            return _result(request_id, converted)
        return _result(request_id, tool_result(output))


async def _invoke(handler: Callable[[Any], Any], argument: BaseModel) -> Any:
    if inspect.iscoroutinefunction(handler):
        return await handler(argument)
    result = await run_in_threadpool(handler, argument)
    if inspect.isawaitable(result):
        return await result
    return result


def mcp_error_from_exception(
    exc: Exception, *, details: Mapping[str, Any] | None = None
) -> McpToolError | None:
    """`HTTPException` と、`status_code` を持つ製品の例外（`SecurityApiError` など）を
    ツールのエラーにする。500 系の内部エラー（502 / 503 / 504 以外）は None（内容を隠す）。

    ツールが失敗に詳細（例: 作成済みの ID）を添えたいときに、`details` を足して raise する（#252）。
    """
    status = getattr(exc, "status_code", None)
    if not isinstance(status, int) or status >= 500 and status not in {502, 503, 504}:
        return None
    detail: Any = getattr(exc, "public_message", None)
    if detail is None and isinstance(exc, HTTPException):
        detail = exc.detail
    code = getattr(exc, "code", None)
    if isinstance(detail, dict):
        code = code or detail.get("code") or detail.get("error_code")
        detail = detail.get("message") or detail.get("detail") or json.dumps(detail)
    return McpToolError(
        str(code or f"HTTP_{status}"),
        str(detail or "処理できませんでした。"),
        status=status,
        details=details,
    )


def _http_error(exc: Exception) -> dict[str, Any] | None:
    converted = mcp_error_from_exception(exc)
    if converted is None:
        return None
    return tool_error(converted.code, converted.message, status=converted.status)


def tool_result(output: Any) -> dict[str, Any]:
    extra: tuple[dict[str, Any], ...] = ()
    if isinstance(output, McpToolResult):
        output, extra = output.output, output.content
    body = output.model_dump(mode="json") if isinstance(output, BaseModel) else output
    body = jsonable_encoder(body)
    if not isinstance(body, dict):
        body = {"result": body}
    return {
        "content": [
            {"type": "text", "text": json.dumps(body, ensure_ascii=False)},
            *(dict(block) for block in extra),
        ],
        "structuredContent": body,
        "isError": False,
    }


def tool_error(
    code: str,
    message: str,
    *,
    status: int | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"error_code": code, "message": message}
    if status is not None:
        body["status"] = status
    if details:
        body["details"] = jsonable_encoder(dict(details))
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": body,
        "isError": True,
    }


def _result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _progress_token(message: Any, accept: str) -> str | int | None:
    """進み具合を SSE で送る `tools/call` なら progressToken を返す（それ以外は None）。"""
    if "text/event-stream" not in accept.lower() or not isinstance(message, dict):
        return None
    if message.get("method") != "tools/call" or "id" not in message:
        return None
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    token = meta.get("progressToken") if isinstance(meta, dict) else None
    # 仕様の progressToken は string か integer（bool は integer として扱わない）。
    if (isinstance(token, str) and token) or (
        isinstance(token, int) and not isinstance(token, bool)
    ):
        return token
    return None


def _sse(payload: Mapping[str, Any]) -> str:
    return f"event: message\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _until_disconnected(request: Request) -> None:
    """client が接続を切るまで待つ（本文を読み終えた後の `http.disconnect` を受け取る）。"""
    while True:
        message = await request.receive()
        if message.get("type") == "http.disconnect":
            return


def _log_cancelled(tool: McpTool, started: float) -> None:
    logger.info(
        "mcp_tool_call_cancelled",
        extra={
            "tool": tool.name,
            "reason": "client_disconnected",
            "elapsed_ms": round((monotonic() - started) * 1000),
        },
    )


async def _stop(task: asyncio.Task[Any]) -> None:
    """task を取り消し、終わるまで待つ（取り消しの例外は外へ出さない）。"""
    if not task.done():
        task.cancel()
    await asyncio.wait({task})


async def _handle_until_disconnected(
    request: Request,
    server: McpServer,
    message: Any,
    *,
    has_any_permission: HasAnyPermission,
) -> tuple[dict[str, Any] | None, bool]:
    """JSON-RPC の 1 メッセージを処理する。読み取り専用のツールの `tools/call` は、client が
    接続を切ったら取り消す（#2691）。戻り値は（応答、切断で取り消したか）。"""
    tool = server.cancellable_tool(message)
    if tool is None:
        return await server.handle(message, has_any_permission=has_any_permission), False
    started = monotonic()
    work = asyncio.create_task(server.handle(message, has_any_permission=has_any_permission))
    watcher = asyncio.create_task(_until_disconnected(request))
    try:
        await asyncio.wait({work, watcher}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        await _stop(watcher)
        if not work.done() and (watcher.done() and not watcher.cancelled()):
            await _stop(work)
            _log_cancelled(tool, started)
        elif not work.done():
            # この処理自体が取り消された（server の停止など）。ツールも止める。
            await _stop(work)
    if work.cancelled():
        return None, True
    return work.result(), False


def _progress_response(
    request: Request,
    server: McpServer,
    message: Mapping[str, Any],
    token: str | int,
    *,
    has_any_permission: HasAnyPermission,
    heartbeat_seconds: float,
) -> StreamingResponse:
    """ツールを実行しながら進み具合を SSE で送り、最後に JSON-RPC の応答を送る。

    ツールは request の文脈（利用者・request id などの contextvar）のまま、ここで作る task で
    動かす。client が切断したら task を取り消す（回答の生成なども止まる）。読み取り専用のツールは、
    次の進み具合を送るのを待たずに、切断を受け取った時点で取り消す（#2691）。
    """
    loop = asyncio.get_running_loop()
    stages: asyncio.Queue[str] = asyncio.Queue()

    def report(text: str) -> None:
        loop.call_soon_threadsafe(stages.put_nowait, text)

    reset = _progress_reporter.set(report)
    try:
        task = asyncio.create_task(server.handle(message, has_any_permission=has_any_permission))
    finally:
        _progress_reporter.reset(reset)

    cancellable = server.cancellable_tool(message)

    async def stream() -> AsyncIterator[str]:
        started = monotonic()
        count = 0
        stage = ""
        disconnect = (
            asyncio.ensure_future(_until_disconnected(request)) if cancellable is not None else None
        )
        try:
            while True:
                waiter = asyncio.ensure_future(stages.get())
                waiting: set[asyncio.Future[Any]] = {task, waiter}
                if disconnect is not None:
                    waiting.add(disconnect)
                done, _ = await asyncio.wait(
                    waiting, timeout=heartbeat_seconds, return_when=asyncio.FIRST_COMPLETED
                )
                if waiter in done:
                    stage = waiter.result()
                else:
                    waiter.cancel()
                if task in done:
                    break
                if disconnect is not None and disconnect in done and cancellable is not None:
                    await _stop(task)
                    _log_cancelled(cancellable, started)
                    return
                count += 1
                elapsed = int(monotonic() - started)
                text = f"経過 {elapsed} 秒: {stage or '実行中'}"
                yield _sse(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/progress",
                        "params": {"progressToken": token, "progress": count, "message": text},
                    }
                )
            response = task.result()
            if response is not None:
                yield _sse(response)
        finally:
            if disconnect is not None and not disconnect.done():
                disconnect.cancel()
            if not task.done():
                task.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def mcp_http_response(
    request: Request,
    server: McpServer,
    *,
    has_any_permission: HasAnyPermission,
    progress_heartbeat_seconds: float = PROGRESS_HEARTBEAT_SECONDS,
) -> Response:
    """Streamable HTTP の POST を処理する。通知だけなら 202、request には JSON で応答する。

    progressToken 付きの `tools/call` を `Accept: text/event-stream` で受けたときは、SSE で
    進み具合（`notifications/progress`）を送ってから結果を送る（#1864）。
    """
    version = request.headers.get("mcp-protocol-version")
    if version and version not in SUPPORTED_PROTOCOL_VERSIONS:
        return JSONResponse(
            _error(None, INVALID_REQUEST, f"未対応の MCP-Protocol-Version です: {version}"),
            status_code=400,
        )
    try:
        message = json.loads(await request.body())
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(_error(None, PARSE_ERROR, "JSON を解析できません。"), status_code=400)
    token = _progress_token(message, request.headers.get("accept", ""))
    if token is not None:
        return _progress_response(
            request,
            server,
            message,
            token,
            has_any_permission=has_any_permission,
            heartbeat_seconds=progress_heartbeat_seconds,
        )
    response, disconnected = await _handle_until_disconnected(
        request, server, message, has_any_permission=has_any_permission
    )
    if disconnected:
        return Response(status_code=CLIENT_CLOSED_REQUEST_STATUS)
    if response is None:
        return Response(status_code=202)
    return JSONResponse(response)
