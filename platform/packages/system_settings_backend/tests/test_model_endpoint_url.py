"""接続の Endpoint URL から呼ぶ URL を決める（#1440）。

3 製品はこの関数で URL を決める。path は補わず、入力どおり使う。
"""

from __future__ import annotations

import pytest

from pr_system_settings.model import (
    EnterpriseAiConnection,
    enterprise_ai_base_url,
    enterprise_ai_request_url,
)

# (Endpoint URL, base URL, Responses API の URL)
CASES = [
    (
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "https://dashscope.aliyuncs.com/compatible-mode/v1/responses",
    ),
    (
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com/v1/responses",
    ),
    ("https://api.deepseek.com", "https://api.deepseek.com", "https://api.deepseek.com/responses"),
    # path の無い root URL にも path を補わない（利用者が API の base URL まで書く）。
    (
        "https://llm.example",
        "https://llm.example",
        "https://llm.example/responses",
    ),
    # 前後の空白と末尾の / だけ除く。
    (
        "  https://api.openai.com/v1/  ",
        "https://api.openai.com/v1",
        "https://api.openai.com/v1/responses",
    ),
    ("", "", "/responses"),
]
CASE_IDS = [
    "compatible-with-long-path",
    "compatible-with-path",
    "compatible-without-path",
    "root-url",
    "trim",
    "empty",
]


@pytest.mark.parametrize(("endpoint", "base_url", "request_url"), CASES, ids=CASE_IDS)
def test_endpoint_is_used_as_is(endpoint: str, base_url: str, request_url: str) -> None:
    assert enterprise_ai_base_url(endpoint) == base_url
    assert enterprise_ai_request_url(endpoint) == request_url
    connection = EnterpriseAiConnection(
        connection_id="primary", endpoint=endpoint, project_id="", api_key="sk-test"
    )
    assert connection.base_url == base_url
    assert connection.request_url() == request_url


@pytest.mark.parametrize(
    ("api_path", "expected"),
    [
        ("/responses", "https://api.deepseek.com/v1/responses"),
        ("responses", "https://api.deepseek.com/v1/responses"),
        ("", "https://api.deepseek.com/v1/responses"),
        ("  ", "https://api.deepseek.com/v1/responses"),
        ("/files", "https://api.deepseek.com/v1/files"),
        ("https://other.example/v1/responses", "https://other.example/v1/responses"),
    ],
    ids=["slash", "no-slash", "empty", "blank", "files", "absolute"],
)
def test_request_url_joins_api_path(api_path: str, expected: str) -> None:
    assert enterprise_ai_request_url("https://api.deepseek.com/v1/", api_path) == expected
