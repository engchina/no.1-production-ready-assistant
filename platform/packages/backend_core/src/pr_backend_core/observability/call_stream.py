"""SDK に依存せず、受信済みの本文を製品の snapshot に写す。telemetry には送らない。"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from ..chat_progress import CALL_CONTENT_MAX


def _value(event: Any, key: str, default: Any = None) -> Any:
    return event.get(key, default) if isinstance(event, Mapping) else getattr(event, key, default)


class CallStreamCapture:
    """受信順を保つ上限付きの prefix。途中更新を抑え、終了時には必ず反映する。"""

    def __init__(self, publish: Callable[[Any], None], *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._publish = publish
        self._parts: dict[tuple[int, int, str], str] = {}
        self._size = 0
        self._last = 0.0
        self._truncated = False
        self._terminal = False

    @property
    def has_terminal(self) -> bool:
        return self._terminal

    @property
    def has_content(self) -> bool:
        return bool(self._parts)

    def feed(self, event: Any) -> None:
        if not self._enabled:
            return
        kind = str(_value(event, "type", ""))
        response = _value(event, "response")
        if (
            kind in {"response.completed", "response.incomplete", "response.failed"}
            and response is not None
        ):
            if kind != "response.completed" and self.has_content and not _value(response, "output"):
                response = {
                    "status": _value(response, "status") or "incomplete",
                    "output": self._output(),
                    "provider_response": response,
                    "capture_partial": True,
                    "capture_truncated": self._truncated,
                }
            self._terminal = True
            self._publish(response)
            return
        types = {
            "response.output_text.delta": "output_text",
            "response.reasoning_text.delta": "reasoning_text",
            "response.reasoning_summary_text.delta": "summary_text",
            "response.function_call_arguments.delta": "function_call",
        }
        if kind not in types:
            return
        delta = _value(event, "delta")
        if not isinstance(delta, str) or not delta:
            return
        key = (
            int(_value(event, "output_index", 0)),
            int(_value(event, "content_index", _value(event, "summary_index", 0))),
            types[kind],
        )
        # 本文だけでなく、空の block の増殖も抑える。
        if key not in self._parts and len(self._parts) >= 1000:
            self._truncated = True
            return
        remaining = max(0, CALL_CONTENT_MAX - self._size)
        self._parts[key] = self._parts.get(key, "") + delta[:remaining]
        self._size += min(len(delta), remaining)
        self._truncated |= len(delta) > remaining
        if time.monotonic() - self._last >= 5.0:
            self.flush()

    def flush(self, *, interrupted: bool = False) -> None:
        if self._terminal or not self._parts:
            return
        self._publish(
            {
                "status": "interrupted" if interrupted else "in_progress",
                "output": self._output(),
                "capture_truncated": self._truncated,
            }
        )
        self._last = time.monotonic()

    def _output(self) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for (_, _, kind), text in sorted(self._parts.items()):
            if kind == "function_call":
                output.append({"type": "function_call", "arguments": text})
            elif kind in {"reasoning_text", "summary_text"}:
                output.append(
                    {
                        "type": "reasoning",
                        "summary" if kind == "summary_text" else "content": [
                            {"type": kind, "text": text}
                        ],
                    }
                )
            else:
                output.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": kind, "text": text}],
                    }
                )
        return output
