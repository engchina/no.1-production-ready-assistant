"""OCI Enterprise AI(Vision)parser の env 解決(#211)。"""

from __future__ import annotations

from rag_parser_core.oci_enterprise_ai import OciEnterpriseAiConfig


def test_config_from_env_reads_platform_names() -> None:
    """共通設定は backend と同じ PLATFORM_OCI_* を読む。"""
    config = OciEnterpriseAiConfig.from_env(
        {
            "PLATFORM_OCI_ENTERPRISE_AI_ENDPOINT": "https://inference.example/openai/v1",
            "PLATFORM_OCI_ENTERPRISE_AI_API_KEY": "sk-test",
            "PLATFORM_OCI_ENTERPRISE_AI_PROJECT_OCID": "ocid1.generativeaiproject.oc1..x",
            "PLATFORM_OCI_COMPARTMENT_ID": "ocid1.compartment.oc1..x",
            "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL": "vendor.vision",
            "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_TEXT_MODEL": "vendor.llm",
            "PLATFORM_OCI_ENTERPRISE_AI_TIMEOUT_SECONDS": "30",
        }
    )

    assert config.oci_enterprise_ai_endpoint == "https://inference.example/openai/v1"
    assert config.oci_enterprise_ai_api_key == "sk-test"
    assert config.oci_compartment_id == "ocid1.compartment.oc1..x"
    assert config.vision_model_id == "vendor.vision"
    assert config.default_model_id == "vendor.llm"
    assert config.oci_enterprise_ai_timeout_seconds == 30.0


def test_config_from_env_text_model_falls_back_to_vision_model() -> None:
    """既定のテキストモデルがなければ既定の Vision モデルを使う。旧名(#499 以前)は読まない。"""
    config = OciEnterpriseAiConfig.from_env(
        {
            "PLATFORM_OCI_ENTERPRISE_AI_DEFAULT_VISION_MODEL": "vendor.vision",
            "PLATFORM_OCI_ENTERPRISE_AI_LLM_MODEL": "vendor.legacy-llm",
            "PLATFORM_OCI_ENTERPRISE_AI_VLM_MODEL": "vendor.legacy-vlm",
        }
    )

    assert config.vision_model_id == "vendor.vision"
    assert config.default_model_id == "vendor.vision"


def test_config_from_env_ignores_legacy_names() -> None:
    """旧名(接頭辞なし)は読まない。"""
    config = OciEnterpriseAiConfig.from_env(
        {
            "OCI_ENTERPRISE_AI_ENDPOINT": "https://legacy.example",
            "OCI_ENTERPRISE_AI_API_KEY": "sk-legacy",
        }
    )

    assert config.oci_enterprise_ai_endpoint == ""
    assert config.oci_enterprise_ai_api_key == ""


def test_for_vision_uses_vision_connection_only_when_set() -> None:
    """Vision の接続(#533)。None なら text と同じ接続(同じ config)を使う。"""
    config = OciEnterpriseAiConfig(
        oci_enterprise_ai_endpoint="https://primary.example",
        oci_enterprise_ai_api_key="sk-primary",
        oci_enterprise_ai_project_ocid="ocid1.primary",
        vision_model_id="vendor.vision",
    )
    assert config.for_vision() is config

    separate = OciEnterpriseAiConfig(
        oci_enterprise_ai_endpoint="https://primary.example",
        oci_enterprise_ai_api_key="sk-primary",
        oci_enterprise_ai_project_ocid="ocid1.primary",
        vision_oci_enterprise_ai_endpoint="https://secondary.example",
        vision_oci_enterprise_ai_api_key="sk-secondary",
        vision_oci_enterprise_ai_project_ocid="",
        vision_model_id="vendor.vision",
    )
    vision = separate.for_vision()
    assert vision.oci_enterprise_ai_endpoint == "https://secondary.example"
    assert vision.oci_enterprise_ai_api_key == "sk-secondary"
    assert vision.oci_enterprise_ai_project_ocid == ""
    assert vision.vision_model_id == "vendor.vision"
    assert vision.for_vision() is vision


def test_config_from_env_reads_model_output_limits() -> None:
    """出力の上限はモデルごとの値(#1719。backend が書く。無ければ 32768)。"""
    config = OciEnterpriseAiConfig.from_env(
        {
            "PLATFORM_OCI_ENTERPRISE_AI_TEXT_MAX_OUTPUT_TOKENS": "16000",
            "PLATFORM_OCI_ENTERPRISE_AI_VISION_MAX_OUTPUT_TOKENS": "64000",
        }
    )
    assert (config.text_max_output_tokens, config.vision_max_output_tokens) == (16000, 64000)

    assert OciEnterpriseAiConfig.from_env({}).text_max_output_tokens == 32768
