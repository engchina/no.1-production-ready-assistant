"""モデルの API の失敗の分類（#1684）。正本は platform の `pr_enterprise_ai.errors`（#3418）。

3 製品が `pr_system_settings.enterprise_ai_errors` から import する名前を、そのまま出す。
"""

from pr_enterprise_ai.errors import (
    ENTERPRISE_AI_CALL_FAILURE_ERROR_CODES,
    ENTERPRISE_AI_CONFIGURATION_FAILURE_KINDS,
    ENTERPRISE_AI_FAILURE_ERROR_CODES,
    ENTERPRISE_AI_FAILURE_KIND_LABELS_JA,
    ENTERPRISE_AI_INCOMPLETE_ERROR_CODE,
    ENTERPRISE_AI_INCOMPLETE_OUTPUT_LIMIT_REASON,
    ENTERPRISE_AI_RETRYABLE_STATUS_CODES,
    EnterpriseAiCallFailureKind,
    EnterpriseAiFailureKind,
    EnterpriseAiHttpFailure,
    EnterpriseAiIncomplete,
    classify_enterprise_ai_http_failure,
    enterprise_ai_endpoint_host,
    enterprise_ai_failure_actions,
    enterprise_ai_http_status_from_message,
    enterprise_ai_incomplete_from_response,
    enterprise_ai_incomplete_message,
    enterprise_ai_provider_message,
    incomplete_retry_phrase,
)

__all__ = [
    "ENTERPRISE_AI_CALL_FAILURE_ERROR_CODES",
    "ENTERPRISE_AI_CONFIGURATION_FAILURE_KINDS",
    "ENTERPRISE_AI_FAILURE_ERROR_CODES",
    "ENTERPRISE_AI_FAILURE_KIND_LABELS_JA",
    "ENTERPRISE_AI_INCOMPLETE_ERROR_CODE",
    "ENTERPRISE_AI_INCOMPLETE_OUTPUT_LIMIT_REASON",
    "ENTERPRISE_AI_RETRYABLE_STATUS_CODES",
    "EnterpriseAiCallFailureKind",
    "EnterpriseAiFailureKind",
    "EnterpriseAiHttpFailure",
    "EnterpriseAiIncomplete",
    "classify_enterprise_ai_http_failure",
    "enterprise_ai_endpoint_host",
    "enterprise_ai_failure_actions",
    "enterprise_ai_http_status_from_message",
    "enterprise_ai_incomplete_from_response",
    "enterprise_ai_incomplete_message",
    "enterprise_ai_provider_message",
    "incomplete_retry_phrase",
]
