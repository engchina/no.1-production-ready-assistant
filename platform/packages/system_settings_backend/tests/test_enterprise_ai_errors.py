"""OCI Enterprise AI の HTTP の失敗の分類と案内（#1684。3 製品で共通の対応表）。"""

from __future__ import annotations

import pytest

from pr_system_settings.enterprise_ai_errors import (
    classify_enterprise_ai_http_failure,
    enterprise_ai_endpoint_host,
    enterprise_ai_failure_actions,
    enterprise_ai_http_status_from_message,
)
from pr_system_settings.model import _model_test_troubleshooting

# 2026-10-13 の実測の応答の本文。
OCI_404 = '{ "code": "404", "message": "Authorization failed or requested resource not found." }'
OCI_MODEL_NOT_FOUND = (
    '{"error":{"code":null,"message":"Entity with key xai.grok-4.6 not found",'
    '"param":null,"type":"invalid_request_error","valid":true}}'
)


@pytest.mark.parametrize(
    ("status", "body", "kind", "retryable", "configuration_error"),
    [
        (400, OCI_MODEL_NOT_FOUND, "model_not_found", False, True),
        (400, '{"error":{"code":"invalid_value"}}', "request", False, False),
        (401, "", "authentication", False, True),
        (403, "", "authorization", False, True),
        (404, OCI_404, "authorization", False, True),
        (429, "", "rate_limit", True, False),
        (503, "", "unavailable", True, False),
        (501, "", "unavailable", False, False),
    ],
    ids=["model-not-found", "other-400", "401", "403", "oci-404", "429", "503", "501"],
)
def test_classify_http_failure(
    status: int, body: str, kind: str, retryable: bool, configuration_error: bool
) -> None:
    failure = classify_enterprise_ai_http_failure(status, body)
    assert failure.kind == kind
    assert failure.status_code == status
    assert failure.retryable is retryable
    assert failure.configuration_error is configuration_error
    assert failure.error_code.startswith("ENTERPRISE_AI_")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("OCI Enterprise AI HTTP 404: { ... }", 404),
        ("Enterprise AI responses returned 401: denied", 401),
        ("Client error '403 Forbidden' for url 'https://example.test'", 403),
        ("Server error '503 Service Unavailable' for url 'https://example.test'", 503),
        ("timeout after 10.0s", None),
    ],
    ids=["nl2sql-agent", "rag", "httpx-client", "httpx-server", "no-status"],
)
def test_status_from_message(message: str, expected: int | None) -> None:
    assert enterprise_ai_http_status_from_message(message) == expected


def test_endpoint_host_drops_path_and_query() -> None:
    endpoint = "https://inference.generativeai.us-chicago-1.oci.oraclecloud.com/openai/v1?x=1"
    assert (
        enterprise_ai_endpoint_host(endpoint)
        == "inference.generativeai.us-chicago-1.oci.oraclecloud.com"
    )
    assert enterprise_ai_endpoint_host("") == ""


def test_authorization_actions_name_model_connection_and_iam_policy() -> None:
    actions = " ".join(
        enterprise_ai_failure_actions(
            classify_enterprise_ai_http_failure(404, OCI_404),
            model_id="xai.grok-4.6",
            endpoint_host="inference.generativeai.us-chicago-1.oci.oraclecloud.com",
        )
    )
    assert "「xai.grok-4.6」" in actions
    assert "inference.generativeai.us-chicago-1.oci.oraclecloud.com" in actions
    assert "HTTP 404" in actions
    assert "generativeaiapikey" in actions
    assert "Project OCID" in actions


def test_model_not_found_actions_point_to_region() -> None:
    actions = " ".join(
        enterprise_ai_failure_actions(
            classify_enterprise_ai_http_failure(400, OCI_MODEL_NOT_FOUND),
            model_id="xai.grok-4.6",
            endpoint_host="inference.generativeai.ap-osaka-1.oci.oraclecloud.com",
        )
    )
    assert "リージョンにありません" in actions
    assert "「xai.grok-4.6」" in actions


def test_model_test_404_tip_is_authorization_not_missing_model() -> None:
    """モデルの接続テストの 404 の案内は、IAM policy・Project を示す（#1684）。"""
    tips = _model_test_troubleshooting(
        "enterprise_text", f"OCI Enterprise AI HTTP 404: {OCI_404}", "EnterpriseAiDirectError"
    )
    joined = " ".join(tips)
    assert "generativeaiapikey" in joined
    assert "見つかっていません" not in joined
    # HTTP の失敗でないとき（タイムアウトなど）は、これまでどおりの案内。
    timeout_tips = _model_test_troubleshooting("enterprise_text", "timeout after 5s", "Timeout")
    assert any("タイムアウト" in tip for tip in timeout_tips)


# --- HTTP 以外の種類と、応答の理由の取り出し（#1715） -------------------------------------


def test_call_failure_kinds_have_codes_and_labels() -> None:
    from typing import get_args

    from pr_system_settings.enterprise_ai_errors import (
        ENTERPRISE_AI_CALL_FAILURE_ERROR_CODES,
        ENTERPRISE_AI_FAILURE_KIND_LABELS_JA,
        EnterpriseAiCallFailureKind,
    )

    kinds = set(get_args(EnterpriseAiCallFailureKind))
    assert kinds == set(ENTERPRISE_AI_CALL_FAILURE_ERROR_CODES)
    assert kinds == set(ENTERPRISE_AI_FAILURE_KIND_LABELS_JA)
    assert ENTERPRISE_AI_CALL_FAILURE_ERROR_CODES["timeout"] == "ENTERPRISE_AI_TIMEOUT"
    assert ENTERPRISE_AI_CALL_FAILURE_ERROR_CODES["authorization"] == (
        "ENTERPRISE_AI_AUTHORIZATION_FAILED"
    )


def test_provider_message_reads_openai_error_body() -> None:
    from pr_system_settings.enterprise_ai_errors import enterprise_ai_provider_message

    body = (
        '{"error":{"message":"Internal server error","type":"server_error",'
        '"param":null,"code":"internal_error"}}'
    )
    assert enterprise_ai_provider_message(body) == "Internal server error (internal_error)"
    assert enterprise_ai_provider_message("") == ""
    assert enterprise_ai_provider_message("upstream   connect\n error") == "upstream connect error"


def test_provider_message_hides_secrets_and_truncates() -> None:
    from pr_system_settings.enterprise_ai_errors import enterprise_ai_provider_message

    # 検出の対象にならないよう、実在しない key の形を組み立てる。
    fake_key = "sk-" + "fake0" * 4
    body = f"bad key {fake_key} Authorization: Bearer abc.def api_key=xyz"
    message = enterprise_ai_provider_message(f'{{"error": {{"message": "{body}"}}}}')
    assert fake_key not in message
    assert "abc.def" not in message
    assert "xyz" not in message
    long = enterprise_ai_provider_message("x" * 1000)
    assert len(long) == 300
    assert long.endswith("…")
