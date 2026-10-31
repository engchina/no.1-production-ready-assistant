"""backend_core の純ユニット（schemas / pagination / settings / logging）。"""

import json
import logging

from pr_backend_core import ApiResponse, Page, configure_logging
from pr_backend_core.api import paginate
from pr_backend_core.config import BaseServiceSettings


def test_api_response_defaults() -> None:
    resp = ApiResponse[dict[str, int]](data={"a": 1})
    dumped = resp.model_dump()
    assert dumped == {"data": {"a": 1}, "error_messages": [], "warning_messages": []}


def test_paginate_has_next() -> None:
    page: Page[int] = paginate([1, 2, 3], total=10, limit=3, offset=0)
    assert page.has_next is True
    last: Page[int] = paginate([10], total=10, limit=3, offset=9)
    assert last.has_next is False


def test_settings_cors_csv_parsing() -> None:
    settings = BaseServiceSettings(cors_origins="http://a, http://b")  # type: ignore[arg-type]
    assert settings.cors_origins == ["http://a", "http://b"]


def test_settings_is_production() -> None:
    assert BaseServiceSettings(environment="production").is_production is True
    assert BaseServiceSettings(environment="local").is_production is False


def test_configure_logging_emits_json(capsys) -> None:  # type: ignore[no-untyped-def]
    configure_logging("INFO", quiet_loggers={"noisy": logging.ERROR})
    logging.getLogger("pr_backend_core.test").info("hello", extra={"k": "v"})
    err = capsys.readouterr().err.strip().splitlines()[-1]
    record = json.loads(err)
    assert record["message"] == "hello"
    assert record["level"] == "INFO"
    assert logging.getLogger("noisy").level == logging.ERROR
