"""回答と Vision の LLM provider が、それぞれのモデルの接続を使う(#533)。"""

from rag_engine.config import ENTERPRISE_AI_LLM_PROVIDER, ENTERPRISE_AI_VISION_LLM_PROVIDER, get_settings

BASE = {
    "ENTERPRISE_AI_ENDPOINT": "https://primary.example",
    "ENTERPRISE_AI_API_KEY": "sk-primary",
    "ENTERPRISE_AI_PROJECT_ID": "proj-primary",
    "ENTERPRISE_AI_DEFAULT_MODEL": "vendor.text",
    "ENTERPRISE_AI_VLM_MODEL": "vendor.vision",
}


def test_vision_provider_uses_vlm_connection_when_given():
    settings = get_settings(
        environ={
            **BASE,
            "ENTERPRISE_AI_VLM_ENDPOINT": "https://secondary.example",
            "ENTERPRISE_AI_VLM_API_KEY": "sk-secondary",
            "ENTERPRISE_AI_VLM_PROJECT_ID": "proj-secondary",
        },
        dotenv_path=None,
    )
    answer = settings.llm_providers[ENTERPRISE_AI_LLM_PROVIDER]
    vision = settings.llm_providers[ENTERPRISE_AI_VISION_LLM_PROVIDER]
    assert (answer.endpoint, answer.api_key, answer.project_id) == ("https://primary.example", "sk-primary", "proj-primary")
    assert (vision.endpoint, vision.api_key, vision.project_id) == ("https://secondary.example", "sk-secondary", "proj-secondary")


def test_vision_provider_falls_back_to_answer_connection():
    settings = get_settings(environ=BASE, dotenv_path=None)
    vision = settings.llm_providers[ENTERPRISE_AI_VISION_LLM_PROVIDER]
    assert (vision.endpoint, vision.api_key, vision.project_id) == ("https://primary.example", "sk-primary", "proj-primary")
