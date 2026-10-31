"""OCI Enterprise AI の呼び方の共通の規則（#3418）。"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from pr_enterprise_ai.errors import (
    classify_enterprise_ai_http_failure,
    enterprise_ai_provider_message,
)
from pr_enterprise_ai.urls import enterprise_ai_base_url, enterprise_ai_request_url

SRC = Path(__file__).resolve().parents[1] / "src" / "pr_enterprise_ai"


@pytest.mark.parametrize(
    ("endpoint", "path", "expected"),
    [
        ("https://h/openai/v1/", "", "https://h/openai/v1/responses"),
        (" https://h/v1 ", "/chat", "https://h/v1/chat"),
        ("https://h/v1", "https://other/x", "https://other/x"),
    ],
    ids=["default-path", "custom-path", "absolute-path"],
)
def test_request_url(endpoint: str, path: str, expected: str) -> None:
    assert enterprise_ai_request_url(endpoint, path) == expected
    assert enterprise_ai_base_url("https://h/v1/") == "https://h/v1"


@pytest.mark.parametrize(
    ("status", "body", "kind"),
    [
        (401, "", "authentication"),
        (404, "", "authorization"),
        (429, "", "rate_limit"),
        (503, "", "unavailable"),
        (400, "Entity with key m not found", "model_not_found"),
        (400, "bad", "request"),
    ],
    ids=["401", "404", "429", "503", "model-not-found", "request"],
)
def test_http_failure_classification(status: int, body: str, kind: str) -> None:
    assert classify_enterprise_ai_http_failure(status, body).kind == kind


def test_provider_message_is_limited() -> None:
    assert len(enterprise_ai_provider_message("x" * 1000)) <= 301


def test_package_uses_only_the_standard_library() -> None:
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module.split(".")[0]]
            for name in names:
                assert name in sys.stdlib_module_names or name in {
                    "__future__",
                    "pr_enterprise_ai",
                }, (
                    path.name,
                    name,
                )
