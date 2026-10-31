"""journald / JSON / native text を JST で読む。破損行で後続を失わない。"""

import contextlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "WARN": 30, "ERROR": 40, "CRITICAL": 50}
SAFE_FIELDS = {
    "schema_version",
    "timestamp",
    "level",
    "name",
    "event",
    "message",
    "service_name",
    "service_version",
    "environment",
    "component",
    "process_id",
    "request_id",
    "run_id",
    "job_id",
    "worker_id",
    "document_id",
    "profile_id",
    "trace_id",
    "parent_span_id",
    "trace_flags",
    "http_method",
    "http_route",
    "http_status",
    "duration_ms",
    "headers_duration_ms",
    "outcome",
    "exception_type",
    "error_code",
    "attempt",
    "retryable",
}
PRIVATE = re.compile(r"(?i)(bearer\s+|(?:password|secret|token|api[_-]?key)\s*[=:]\s*)[^\s\"',;]+")
min_level = LEVELS.get(os.environ.get("TAIL_LOGS_MIN_LEVEL", "").upper(), 0)
show_unit = os.environ.get("TAIL_LOGS_SHOW_UNIT") == "true"
json_export = os.environ.get("TAIL_LOGS_JSON") == "true"
id_filter = os.environ.get("TAIL_LOGS_ID_FILTER", "").replace("-", "_", 1)
product = os.environ.get("TAIL_LOGS_PRODUCT", "nl2sql")


def loads(value):
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return None


def timestamp(record, realtime):
    # 旧 naive アプリ時刻は UTC と明記して解釈。journald の epoch があればそちらが正本。
    value = record.get("timestamp") or record.get("asctime")
    stamp = None
    if value:
        try:
            stamp = datetime.fromisoformat(str(value).replace(",", "."))
            if stamp.tzinfo is None:
                stamp = (
                    datetime.fromtimestamp(float(realtime) / 1e6, UTC)
                    if realtime
                    else stamp.replace(tzinfo=UTC)
                )
        except (ValueError, OverflowError, OSError):
            pass
    if stamp is None:
        epoch = record.get("timestamp_epoch")
        try:
            stamp = datetime.fromtimestamp(
                float(epoch if epoch is not None else float(realtime) / 1e6), UTC
            )
        except (ValueError, TypeError, OverflowError, OSError):
            return None
    return stamp.astimezone(JST).isoformat(timespec="milliseconds")


def render(line):
    unit, realtime, message = "", None, line
    outer = loads(line)
    if isinstance(outer, dict) and "MESSAGE" in outer:
        unit = outer.get("_SYSTEMD_UNIT") or outer.get("SYSLOG_IDENTIFIER") or ""
        raw = outer.get("MESSAGE")
        if isinstance(raw, list):
            raw = bytes(v for v in raw if isinstance(v, int) and 0 <= v <= 255).decode(
                "utf-8", "replace"
            )
        message = raw if isinstance(raw, str) else "[invalid MESSAGE]"
        realtime = outer.get("__REALTIME_TIMESTAMP")
    record = loads(message)
    native = not isinstance(record, dict) or "message" not in record
    if native:
        record = {
            "level": "INFO",
            "event": "native_log",
            "name": "native",
            "message": "ネイティブログ（原文は --raw で確認）",
        }
    level = str(record.get("level") or record.get("levelname") or "INFO").upper()
    if LEVELS.get(level, 20) < min_level and not native:
        return
    if id_filter:
        field, value = id_filter.split(":", 1)
        if record.get(field) != value:
            return
    stamp = timestamp(record, realtime)
    if json_export:
        safe = {
            key: value
            for key, value in record.items()
            if key in SAFE_FIELDS and isinstance(value, (str, int, float, bool, type(None)))
        }
        # 旧形式の自由本文は export しない。新 schema の event/message は producer の安全化済み。
        if record.get("schema_version") != 1:
            safe["message"] = "旧形式の診断イベント（原文は --raw で確認）"
        safe.update(schema_version=1, timestamp=stamp, level=level)
        text = json.dumps(safe, ensure_ascii=False, allow_nan=False)
    else:
        prefix = (
            (str(unit).removeprefix(f"production-ready-{product}-").removesuffix(".service") + "  ")
            if show_unit and unit
            else ""
        )
        if native:
            text = prefix + message
        else:
            extras = " ".join(
                f"{key}={json.dumps(value, ensure_ascii=False)}"
                for key, value in record.items()
                if key
                not in {"timestamp", "asctime", "level", "levelname", "name", "message", "taskName"}
            )
            text = (
                f"{stamp or '[時刻不明]'}  {level:<8}  {prefix}{record.get('name', '-')}  "
                f"{record.get('message')}  {extras}"
            )
    # 表示は一行、軽い二次防御。raw export は明示的な元データ閲覧。
    print(
        PRIVATE.sub(lambda match: match[1] + "[REDACTED]", text[:32768])
        .replace("\r", "\\r")
        .replace("\n", "\\n"),
        flush=True,
    )


def main():
    for line in sys.stdin:
        try:
            render(line.rstrip("\n"))
        except Exception:
            print(
                '{"event":"log_record_invalid","level":"WARNING","message":"破損ログをスキップしました"}',
                flush=True,
            )


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt, BrokenPipeError):
        main()
