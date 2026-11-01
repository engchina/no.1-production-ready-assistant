"""外部の AI の呼び出し（生成・判定・質問の拡張・埋め込み・再ランク）の記録と再生（#59）。

評価の CLI だけが ``use_external_calls`` で文脈（``ContextVar``）に記録・再生の session を置く。session が
無い文脈（通常の質問・取込・job の評価）は何もしない（実の API を呼ぶ）。worker thread は文脈を引き継ぐ
（``asyncio.to_thread`` / ``contextvars.copy_context``）ので、回答の thread の中の呼び出しも同じ session を見る。

- 記録（``record``）: 実の API を呼び、入力の hash（種類・path・本文の正規化した JSON）ごとに応答を残す。
  失敗（時間切れ・HTTP の失敗）も種類と status を残し、再生で同じ例外を起こす（再試行・時間切れの経路の回帰）。
  入力の本文（プロンプト・根拠の原文）は残さない。応答の本文は残すため、実データの記録は明示した保存先だけに置く。
- 再生（``replay``）: 実の API を呼ばず、同じ入力の hash の記録を記録の順に返す。記録に無い入力・同じ入力の
  余分な呼び出しは ``ExternalCallMismatch`` で失敗し、**実の API へ fallback しない**。使わなかった記録
  （呼び出しが減った）は ``unused`` で分かる。並行の呼び出しがあるため、順は同じ入力の中で照合する。
"""

from __future__ import annotations

import base64
import hashlib
import json
import threading
from collections import deque
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

import httpx

RECORDING_FORMAT = "rag-external-calls/v1"
# 再生の HTTP の応答に戻す header（SDK が使うもの。ほかの header・認証情報は残さない）。
_KEPT_HEADERS = ("content-type", "retry-after", "retry-after-ms")
_ERROR_MESSAGE_CHARS = 500
Mode = Literal["record", "replay"]
ErrorBuilder = Callable[[Mapping[str, Any]], BaseException]


class ExternalCallMismatch(RuntimeError):
    """再生で、記録と合わない呼び出し（記録に無い入力・余分な呼び出し）。実の API には接続しない。"""


class ExternalCallSession:
    """1 回の評価の外部の呼び出しの記録（``record``）または再生（``replay``）。thread safe。"""

    def __init__(self, mode: Mode, entries: list[dict[str, Any]] | None = None) -> None:
        self.mode: Mode = mode
        self.entries: list[dict[str, Any]] = list(entries or [])
        self.mismatches: list[str] = []
        self._lock = threading.Lock()
        self._pending: dict[str, deque[int]] = {}
        for index, entry in enumerate(self.entries):
            self._pending.setdefault(str(entry["key"]), deque()).append(index)
        self._known = set(self._pending)

    @classmethod
    def from_recording(cls, recording: Mapping[str, Any]) -> ExternalCallSession:
        """``to_recording`` の JSON から再生の session を作る。形式が違えば ValueError。"""
        if recording.get("format") != RECORDING_FORMAT or not isinstance(recording.get("entries"), list):
            raise ValueError(f"外部の呼び出しの記録の形式が違います（{RECORDING_FORMAT} を期待）。")
        return cls("replay", recording["entries"])

    def to_recording(self, manifest: Mapping[str, Any]) -> dict[str, Any]:
        """記録を JSON にする（評価の条件の manifest を添える。秘密・入力の原文は含まない）。"""
        return {"format": RECORDING_FORMAT, "manifest": dict(manifest), "entries": self.entries}

    def unused(self) -> list[dict[str, Any]]:
        """再生で使わなかった記録（呼び出しが記録より少ない）。"""
        with self._lock:
            indexes = sorted(index for queue in self._pending.values() for index in queue)
        return [self.entries[index] for index in indexes]

    def exchange[T](
        self,
        kind: str,
        request: object,
        live: Callable[[], T],
        *,
        label: str = "",
        build_error: ErrorBuilder | None = None,
    ) -> T:
        """記録では ``live`` を呼んで結果（JSON にできる値）を残し、再生では記録を返す。"""
        key = request_key(kind, request)
        if self.mode == "record":
            try:
                result = live()
            except Exception as exc:
                self._append({"kind": kind, "label": label, "key": key, "error": _error_record(exc)})
                raise
            self._append({"kind": kind, "label": label, "key": key, "result": result})
            return result
        with self._lock:
            queue = self._pending.get(key)
            index = queue.popleft() if queue else None
            if index is None:
                reason = "同じ入力の余分な呼び出し" if key in self._known else "記録に無い入力"
                where = f"{kind} {label}".strip()
                message = f"再生の不一致: {reason}（{where}、入力の hash {key[:12]}）。実の API には接続しません。"
                self.mismatches.append(message)
                raise ExternalCallMismatch(message)
        entry = self.entries[index]
        if "error" in entry:
            raise (build_error or _builtin_error)(entry["error"])
        return entry["result"]  # type: ignore[no-any-return]

    def _append(self, entry: dict[str, Any]) -> None:
        with self._lock:
            self.entries.append(entry)


_SESSION: ContextVar[ExternalCallSession | None] = ContextVar("rag_external_call_session", default=None)


@contextmanager
def use_external_calls(session: ExternalCallSession | None) -> Iterator[None]:
    """文脈の外部の呼び出しを ``session`` で記録・再生する（None は実の API）。"""
    token = _SESSION.set(session)
    try:
        yield
    finally:
        _SESSION.reset(token)


def external_call_mode() -> str:
    """文脈の外部の呼び出しの扱い（``live`` / ``record`` / ``replay``）。評価の結果に残す。"""
    session = _SESSION.get()
    return "live" if session is None else session.mode


def external_call[T](
    kind: str,
    request: object,
    live: Callable[[], T],
    *,
    label: str = "",
    build_error: ErrorBuilder | None = None,
) -> T:
    """session があれば記録・再生し、無ければ ``live`` をそのまま呼ぶ。"""
    session = _SESSION.get()
    if session is None:
        return live()
    return session.exchange(kind, request, live, label=label, build_error=build_error)


def send_http(send: Callable[[httpx.Request], httpx.Response], request: httpx.Request) -> httpx.Response:
    """httpx の client の ``send`` を記録・再生する（OpenAI 互換の SDK の生成・埋め込み）。

    記録では応答の本文を読み切ってから返す（stream の応答も同じ本文を順に読める）。
    """
    if _SESSION.get() is None:
        return send(request)

    def live() -> dict[str, Any]:
        response = send(request)
        try:
            body = response.read()
        finally:
            response.close()
        headers = {name: response.headers[name] for name in _KEPT_HEADERS if name in response.headers}
        return {"status": response.status_code, "headers": headers, **_encode_body(body)}

    def build_error(error: Mapping[str, Any]) -> BaseException:
        error_type = getattr(httpx, str(error.get("type")), None)
        if isinstance(error_type, type) and issubclass(error_type, httpx.RequestError):
            return error_type(str(error.get("message", "")), request=request)
        return _builtin_error(error)

    payload = {"method": request.method, "path": request.url.path, "body": _decode_request_body(request.content)}
    model = payload["body"].get("model") if isinstance(payload["body"], dict) else None
    result = external_call("http", payload, live, label=f"{request.url.path} {model or ''}".strip(), build_error=build_error)
    content = base64.b64decode(result["body_base64"]) if "body_base64" in result else str(result["body"]).encode("utf-8")
    return httpx.Response(int(result["status"]), headers=dict(result.get("headers") or {}), content=content, request=request)


def request_key(kind: str, request: object) -> str:
    """呼び出しの入力の hash（種類と、正規化した JSON の sha256）。"""
    encoded = json.dumps({"kind": kind, "request": request}, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _decode_request_body(content: bytes) -> object:
    try:
        return json.loads(content.decode("utf-8")) if content else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"sha256": hashlib.sha256(content).hexdigest()}


def _encode_body(body: bytes) -> dict[str, str]:
    try:
        return {"body": body.decode("utf-8")}
    except UnicodeDecodeError:
        return {"body_base64": base64.b64encode(body).decode("ascii")}


def _error_record(exc: BaseException) -> dict[str, Any]:
    record: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)[:_ERROR_MESSAGE_CHARS]}
    status = getattr(exc, "status", None) or getattr(exc, "code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        record["status"] = status
    return record


_BUILTIN_ERRORS: dict[str, type[Exception]] = {
    "TimeoutError": TimeoutError,
    "ConnectionError": ConnectionError,
    "ValueError": ValueError,
}


def _builtin_error(error: Mapping[str, Any]) -> BaseException:
    """記録した失敗の例外（標準の種類はその型、ほかは RuntimeError）。"""
    error_type = _BUILTIN_ERRORS.get(str(error.get("type")), RuntimeError)
    return error_type(str(error.get("message", "")))


__all__ = [
    "RECORDING_FORMAT",
    "ExternalCallMismatch",
    "ExternalCallSession",
    "external_call",
    "external_call_mode",
    "request_key",
    "send_http",
    "use_external_calls",
]
