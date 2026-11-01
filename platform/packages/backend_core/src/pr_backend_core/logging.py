"""サービス横断の JST JSON Lines。業務本文・認証情報は出力しない。"""

import json
import logging
import math
import re
import sys
import traceback
from collections import deque
from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from pythonjsonlogger import json as jsonlogger

from .observability.request_context import access_summary_var, correlation_fields

JST = ZoneInfo("Asia/Tokyo")
_EVENT = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")
_SENSITIVE = re.compile(
    r"authorization|cookie|password|passwd|secret|token(?!s$|_count$)|api.?key|credential|private.?key|"
    r"wallet|dsn|prompt|question|query(?!_count|_id)|sql(?!_count|_id)|answer|content|"
    r"payload|raw.?text|component.?stack|exception.?text",
    re.IGNORECASE,
)
_CREDENTIAL = re.compile(
    r"(?i)(bearer\s+)[^\s\"',;]+|"
    r"((?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)[^\s\"',;]+"
)
_URL = re.compile(r"https?://[^\s\"<>]+")
_RESERVED = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}
# 自前の logger（本文を残す）。これ以外は外部 SDK とみなし、本文を「外部ライブラリの診断」に伏せる。
# RAG の解析エンジン（rag_engine。Vision の進捗など）も自前（#1997）。
_OWNED_PREFIXES = (
    "rag_engine",
    "rag_pipeline_core",
    "app.",
    "pr_backend_core",
    "pr_system_settings",
    "rag_parser_core",
    "__main__",
)
_MAX_STRING = 1024
# 外部の HTTP クライアント・SDK の logger の既定の level（#1935）。INFO の行（`HTTP Request: POST …`
# など）は本文を伏せると「外部ライブラリの診断」だけになり、呼び出しごとに出ても情報が無い。
# 警告・失敗は残す。戻すときは `configure_logging` の `quiet_loggers` で同じ名前に level を渡すか、
# 全体の level を DEBUG にする。子の logger（`httpx._client` など）は親の level を継ぐ。
DEPENDENCY_LOGGER_LEVELS: Mapping[str, int] = {
    "httpx": logging.WARNING,
    # openai-agents / mcp が使う次世代の httpx（logger 名が `httpx2` / `httpcore2`）。
    "httpx2": logging.WARNING,
    "httpcore": logging.WARNING,
    "httpcore2": logging.WARNING,
    "urllib3": logging.WARNING,
    "requests": logging.WARNING,
}
_MAX_ITEMS = 32
_MAX_DEPTH = 5


def safe_url(value: str) -> str:
    """URL の userinfo・query・fragment は診断に使わない。"""
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port is not None:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        return "[invalid_url]"


def _safe_text(value: str) -> str:
    value = value[: _MAX_STRING * 2]
    value = _URL.sub(lambda match: safe_url(match[0]), value)
    return _CREDENTIAL.sub(lambda match: (match[1] or match[2] or "") + "[REDACTED]", value)[
        :_MAX_STRING
    ]


_ORACLE_CODE_RE = re.compile(r"\b(?:ORA|DPY|DPI)-[0-9]{4,5}\b")


def safe_error_code(error: BaseException) -> str | int | None:
    """例外のコード（``ORA-03146`` / ``DPY-4011`` / 製品の code）だけを返す。本文は返さない。

    python-oracledb の例外は ``args[0]`` が ``_Error``（``full_code`` に ``ORA-03146`` などを
    持つ）なので、文字列の本文ではなく ``full_code`` から取る（#2342。以前はコードが出なかった）。
    画面の「詳細」とログに出してよい値だけにする（業務データを含む本文は出さない）。
    """
    code = error.__dict__.get("code")
    if isinstance(code, int) or isinstance(code, str) and re.fullmatch(r"[A-Z0-9_:-]{1,64}", code):
        return code
    first = error.args[0] if error.args else None
    driver_code = getattr(first, "full_code", None)
    if isinstance(driver_code, str) and _ORACLE_CODE_RE.fullmatch(driver_code):
        return driver_code
    if isinstance(first, str):
        # Oracle の既知コードだけを抽出し、例外本文は出力しない。
        match = _ORACLE_CODE_RE.search(first[:_MAX_STRING])
        if match:
            return match[0]
    return None


def _exception_details(error: BaseException, *, max_frames: int) -> dict[str, object]:
    """例外1件の安全な分類と位置。メッセージ・locals・ソース行は含めない。"""
    fields: dict[str, object] = {"exception_type": type(error).__name__[:128]}
    code = safe_error_code(error)
    if code is not None:
        fields["error_code"] = code
    frames = deque(traceback.walk_tb(error.__traceback__), maxlen=max_frames)
    if frames:
        fields["exception_frames"] = [
            {
                "file": Path(frame.f_code.co_filename).name[:128],
                "function": frame.f_code.co_name[:128],
                "line": line,
            }
            for frame, line in frames
        ]
    return fields


def safe_exception_fields(error: BaseException) -> dict[str, object]:
    """型・コード・stack の位置を cause まで残す。例外本文・locals・ソース行は残さない。"""
    fields = _exception_details(error, max_frames=16)
    causes: list[str] = []
    details: list[dict[str, object]] = []
    seen = {id(error)}
    cause = error.__cause__ or (None if error.__suppress_context__ else error.__context__)
    while cause is not None and id(cause) not in seen and len(causes) < 5:
        seen.add(id(cause))
        causes.append(type(cause).__name__[:128])
        details.append(_exception_details(cause, max_frames=8))
        cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
    if causes:
        fields["exception_causes"] = causes
        fields["exception_cause_details"] = details
    return fields


def _safe_value(
    value: object,
    *,
    depth: int = 0,
    seen: frozenset[int] = frozenset(),
    truncated: list[bool] | None = None,
    budget: list[int] | None = None,
) -> object:
    if budget is None:
        budget = [256]
    budget[0] -= 1
    if budget[0] < 0:
        if truncated is not None:
            truncated[0] = True
        return "[truncated]"
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        if truncated is not None and len(value) > _MAX_STRING:
            truncated[0] = True
        return _safe_text(value)
    if isinstance(value, BaseException):
        return safe_exception_fields(value)
    if depth >= _MAX_DEPTH or id(value) in seen:
        if truncated is not None:
            truncated[0] = True
        return "[truncated]"
    nested_seen = seen | {id(value)}
    if isinstance(value, Mapping):
        if truncated is not None and len(value) > _MAX_ITEMS:
            truncated[0] = True
        return {
            _safe_text(key if isinstance(key, str) else "[key]"): "[REDACTED]"
            if _SENSITIVE.search(key if isinstance(key, str) else "")
            else _safe_value(
                item, depth=depth + 1, seen=nested_seen, truncated=truncated, budget=budget
            )
            for key, item in islice(value.items(), _MAX_ITEMS)
        }
    if isinstance(value, list | tuple | set | frozenset):
        if truncated is not None and len(value) > _MAX_ITEMS:
            truncated[0] = True
        return [
            _safe_value(item, depth=depth + 1, seen=nested_seen, truncated=truncated, budget=budget)
            for item in islice(value, _MAX_ITEMS)
        ]
    # __str__ は任意コードや SDK response body を返すことがある。
    return f"[{type(value).__name__}]"


def _uvicorn_access_fields(record: logging.LogRecord) -> dict[str, object] | None:
    if (
        record.name != "uvicorn.access"
        or not isinstance(record.args, tuple)
        or len(record.args) != 5
    ):
        return None
    _, method, path, _, status = record.args
    if not isinstance(method, str) or not isinstance(path, str) or not isinstance(status, int):
        return None
    return {
        "http_method": method,
        "http_route": urlsplit(path).path,
        "http_status": status,
        "event": "http_access",
        "message": "HTTP リクエスト",
    }


class StructuredFormatter(jsonlogger.JsonFormatter):
    """既存 reader の4項目を維持し、危険な JSON の既定 stringify を使わない。"""

    def __init__(
        self,
        *,
        service_name: str,
        service_version: str,
        environment: str,
        component: str,
        max_record_bytes: int,
    ) -> None:
        super().__init__(json_ensure_ascii=False)
        self.identity = {
            "service_name": service_name,
            "service_version": service_version,
            "environment": environment,
            "component": component,
        }
        self.identity = {key: _safe_text(value)[:128] for key, value in self.identity.items()}
        self.max_record_bytes = max_record_bytes

    def format(self, record: logging.LogRecord) -> str:
        try:
            logger_name = record.name if isinstance(record.name, str) else "app.unknown"
            truncated = [False]
            budget = [256]
            extras = list(
                islice(
                    (
                        (key, value)
                        for key, value in record.__dict__.items()
                        if key not in _RESERVED
                    ),
                    _MAX_ITEMS + 1,
                )
            )
            truncated[0] = len(extras) > _MAX_ITEMS
            fields: dict[str, Any] = {
                key: "[REDACTED]"
                if _SENSITIVE.search(key)
                else _safe_value(value, truncated=truncated, budget=budget)
                for key, value in extras[:_MAX_ITEMS]
            }
            # 既存の trace_id 属性は業務 ID。active な W3C trace と同一視しない。
            if "trace_id" in fields:
                fields["domain_trace_id"] = fields.pop("trace_id")
            fields.update(correlation_fields())
            if record.exc_info and isinstance(record.exc_info[1], BaseException):
                fields.update(safe_exception_fields(record.exc_info[1]))
            # メッセージの遅延引数に例外があっても本文へ変換しない。
            safe_args = _safe_value(record.args, truncated=truncated, budget=budget)
            message = str(record.msg) if isinstance(record.msg, str) else "診断イベント"
            if record.args:
                try:
                    message = message % (
                        tuple(safe_args) if isinstance(safe_args, list) else safe_args
                    )
                except (TypeError, ValueError, KeyError):
                    message = "診断イベント（引数の形式が不正）"
            access = _uvicorn_access_fields(record)
            if access:
                fields.update(access)
                message = str(access["message"])
            elif not logger_name.startswith(_OWNED_PREFIXES + ("uvicorn", "gunicorn")):
                # 外部 SDK の自由形式の本文は通常ログに持ち込まない。
                message = "外部ライブラリの診断"
                fields.setdefault("event", "dependency_log")
            event = fields.get("event")
            fields["event"] = (
                event
                if isinstance(event, str) and _EVENT.fullmatch(event)
                else (message if _EVENT.fullmatch(message) else "application_log")
            )
            if truncated[0] or len(message) > _MAX_STRING:
                fields["truncated"] = True
            fields.update(
                schema_version=1,
                timestamp=datetime.fromtimestamp(record.created, JST).isoformat(
                    timespec="milliseconds"
                ),
                level=_safe_text(record.levelname)[:32],
                name=logger_name[:128],
                message=_safe_text(message),
                process_id=record.process,
                **self.identity,
            )
            encoded = json.dumps(fields, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > self.max_record_bytes:
                # 大きな業務属性を捨てても相関・失敗分類は残す。
                keep = set(self.identity) | {
                    "schema_version",
                    "timestamp",
                    "level",
                    "name",
                    "event",
                    "message",
                    "process_id",
                    "request_id",
                    "run_id",
                    "job_id",
                    "worker_id",
                    "exception_type",
                    "error_code",
                    "http_status",
                    "trace_id",
                    "parent_span_id",
                }
                fields = {
                    key: _safe_text(value)[:128] if isinstance(value, str) else value
                    for key, value in fields.items()
                    if key in keep and isinstance(value, str | int | float | bool | type(None))
                }
                fields["message"] = str(fields["message"])[:128]
                fields["truncated"] = True
                encoded = json.dumps(
                    fields, ensure_ascii=False, allow_nan=False, separators=(",", ":")
                )
                # 相関属性自体が巨大でも byte 上限を厳密に守る。
                for key in ("exception_frames", "worker_id", "job_id", "run_id", "message"):
                    if len(encoded.encode("utf-8")) <= self.max_record_bytes:
                        break
                    fields.pop(key, None)
                    encoded = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
            return encoded
        except Exception:
            # ログ出力の失敗で本来の業務例外を隠さない。再帰 logging もしない。
            return json.dumps(
                {
                    "schema_version": 1,
                    "timestamp": datetime.now(JST).isoformat(timespec="milliseconds"),
                    "name": "pr_backend_core.logging",
                    **self.identity,
                    "process_id": record.process,
                    "event": "logging_format_failed",
                    "level": "ERROR",
                    "message": "診断ログの形式変換に失敗しました",
                },
                ensure_ascii=False,
            )


class _ContextAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # ASGI middleware の summary を正本とし、Uvicorn の headers 到達時の重複を除く。
        return not (
            record.name in {"uvicorn.access", "gunicorn.access"} and access_summary_var.get()
        )


class _DiagnosticHandler(logging.StreamHandler[Any]):
    """再構成する際に所有権を判定できる stderr handler。"""

    def handleError(self, record: logging.LogRecord) -> None:
        # logging の既定 handleError は record の未処理 args を stderr に出すため使わない。
        with suppress(Exception):
            if sys.__stderr__ is not None:
                sys.__stderr__.write('{"event":"logging_sink_failed","level":"ERROR"}\n')


def configure_logging(
    level: str = "INFO",
    *,
    quiet_loggers: Mapping[str, int] | None = None,
    service_name: str = "production-ready",
    service_version: str = "0.1.0",
    environment: str = "local",
    component: str = "api",
) -> None:
    """API / worker / server に同じ policy を適用し、外部 collector は消さない。

    外部の HTTP クライアントの logger は `DEPENDENCY_LOGGER_LEVELS`（WARNING）にする。
    `level` が DEBUG のときは診断のため絞らない。`quiet_loggers` は既定より優先する
    （同じ名前で level を戻せる）。
    """
    root = logging.getLogger()
    for handler in root.handlers[:]:
        if isinstance(handler, _DiagnosticHandler):
            root.removeHandler(handler)
            handler.close()
    handler = _DiagnosticHandler()
    limit = 32768
    handler.setFormatter(
        StructuredFormatter(
            service_name=service_name,
            service_version=service_version,
            environment=environment,
            component=component,
            max_record_bytes=limit,
        )
    )
    handler.addFilter(_ContextAccessFilter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "gunicorn.error", "gunicorn.access"):
        server = logging.getLogger(name)
        # サーバーの独立した plain text handler を共通の stderr 出力へつなぐ。
        for native in server.handlers[:]:
            if isinstance(native, logging.StreamHandler):
                server.removeHandler(native)
        server.propagate = True
        server.setLevel(logging.NOTSET)
    verbose = root.getEffectiveLevel() <= logging.DEBUG
    for name, logger_level in DEPENDENCY_LOGGER_LEVELS.items():
        # DEBUG のときは前の構成で上げた level も外し、root に従わせる。
        logging.getLogger(name).setLevel(logging.NOTSET if verbose else logger_level)
    for name, logger_level in (quiet_loggers or {}).items():
        logging.getLogger(name).setLevel(logger_level)


def configure_http_logging(app: Any, *, service_name: str) -> None:
    """RAG の独立した service factory 用。DB / 認証 / 製品 backend は初期化しない。"""
    import os

    from .config.runtime import load_runtime_environment
    from .observability.metrics import MetricsMiddleware

    configure_logging(
        os.environ.get("RAG_LOG_LEVEL", "INFO"),
        service_name=service_name,
        environment=load_runtime_environment(),
        component="microservice",
    )
    app.add_middleware(MetricsMiddleware, enable_metrics=False)


def configure_cli_logging(product: str) -> None:
    """offline CLI は settings / DB を初期化せず、診断だけを stderr へ接続する。"""
    import os

    from .config.runtime import load_runtime_environment

    configure_logging(
        os.environ.get(f"{product.upper()}_LOG_LEVEL", "INFO"),
        service_name=f"production-ready-{product}",
        environment=load_runtime_environment(),
        component="cli",
    )
    # python-oracledb の pool の背景のスレッドの traceback を 1 行の log にまとめる（#1744）。
    from .oracle_connect_diagnosis import install_oracledb_thread_excepthook

    install_oracledb_thread_excepthook()
