"""包装された例外の根因の位置を、本文を漏らさずに記録する（#2733）。"""

import json
import logging
from typing import Any

from pr_backend_core.logging import StructuredFormatter, safe_exception_fields


def _root_failure() -> None:
    sensitive_local = "LOCAL_SECRET_SENTINEL"
    raise AttributeError(f"QUESTION_SENTINEL DOCUMENT_SENTINEL {sensitive_local}")


def _wrapped_failure() -> None:
    try:
        _root_failure()
    except AttributeError as exc:
        raise RuntimeError("TOKEN_SENTINEL") from exc


def test_formatter_preserves_original_frame_without_exception_body_or_locals() -> None:
    try:
        _wrapped_failure()
    except RuntimeError as exc:
        record = logging.LogRecord(
            "test", logging.ERROR, __file__, 1, "failed", (), (type(exc), exc, exc.__traceback__)
        )
    wire = StructuredFormatter(
        service_name="test",
        service_version="1",
        environment="test",
        component="api",
        max_record_bytes=16384,
    ).format(record)
    fields = json.loads(wire)
    assert fields["exception_causes"] == ["AttributeError"]
    detail = fields["exception_cause_details"][0]
    assert detail["exception_type"] == "AttributeError"
    assert detail["exception_frames"][-1]["function"] == "_root_failure"
    assert detail["exception_frames"][-1]["file"] == "test_exception_cause_frames.py"
    assert detail["exception_frames"][-1]["line"] > 0
    assert "SENTINEL" not in wire
    assert "sensitive_local" not in wire


def test_implicit_context_and_suppressed_context_follow_python_exception_semantics() -> None:
    try:
        try:
            _root_failure()
        except AttributeError:
            raise RuntimeError("PRIVATE_SENTINEL")  # noqa: B904 - 暗黙の context の合成ケース
    except RuntimeError as exc:
        fields = safe_exception_fields(exc)
    assert fields["exception_cause_details"]
    try:
        try:
            _root_failure()
        except AttributeError:
            raise RuntimeError("PRIVATE_SENTINEL") from None
    except RuntimeError as exc:
        fields = safe_exception_fields(exc)
    assert "exception_causes" not in fields
    assert "exception_cause_details" not in fields


def test_exception_cycles_and_chain_depth_are_bounded() -> None:
    first = RuntimeError("ROOT_SENTINEL")
    second = ValueError("CAUSE_SENTINEL")
    first.__cause__ = second
    second.__cause__ = first
    assert safe_exception_fields(first)["exception_causes"] == ["ValueError"]
    chain: BaseException = first
    for _ in range(10):
        outer = RuntimeError("PRIVATE_SENTINEL")
        outer.__cause__ = chain
        chain = outer
    fields = safe_exception_fields(chain)
    details: Any = fields["exception_cause_details"]
    assert len(details) == 5
    assert "SENTINEL" not in str(fields)


def test_cause_frames_keep_only_the_last_eight_positions() -> None:
    def recurse(depth: int) -> None:
        if depth:
            recurse(depth - 1)
        else:
            _root_failure()

    try:
        try:
            recurse(20)
        except AttributeError as cause:
            raise RuntimeError("PRIVATE_SENTINEL") from cause
    except RuntimeError as exc:
        fields = safe_exception_fields(exc)
    details: Any = fields["exception_cause_details"]
    frames = details[0]["exception_frames"]
    assert len(frames) == 8
    assert frames[-1]["function"] == "_root_failure"
