"""NL2SQL の実呼出入口で用途を付け、引数と結果を変えない。"""

from types import SimpleNamespace
from typing import Any

import pytest
from pr_backend_core.observability.model_purpose import model_purpose_attributes
from pr_system_settings.chat_attachments import IMAGE_READING_PROMPT

from app.features.nl2sql import chat_attachments
from app.features.settings import router as settings_router
from app.settings import Settings


@pytest.mark.parametrize("target", ["enterprise_text", "enterprise_vision", "embedding", "rerank"])
async def test_connection_check_purpose_reaches_actual_client(
    monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    calls: list[dict[str, str]] = []

    class Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def generate(self, *_args: Any, **_kwargs: Any) -> str:
            calls.append(model_purpose_attributes())
            return "合成応答"

        generate_from_image = generate

        async def embed(self, *_args: Any, **_kwargs: Any) -> list[list[float]]:
            calls.append(model_purpose_attributes())
            return [[0.1]]

        async def rerank(self, *_args: Any, **_kwargs: Any) -> list[tuple[int, float]]:
            calls.append(model_purpose_attributes())
            return [(0, 0.5)]

    monkeypatch.setattr(settings_router, "OciEnterpriseAiClient", Client)
    monkeypatch.setattr(settings_router, "OciGenAiClient", Client)
    await settings_router._run_model_settings_test(Settings(), any_request(target))
    assert calls == [{"pr.gen_ai.purpose": "connection.check"}]
    assert model_purpose_attributes() == {}


def any_request(target: str) -> Any:
    return SimpleNamespace(target_type=target)


async def test_attachment_image_read_has_document_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.clients.oci_enterprise_ai import OciEnterpriseAiClient

    async def generate(_self: Any, data: bytes, prompt: str, *, mime_type: str) -> str:
        assert data == b"synthetic" and prompt == IMAGE_READING_PROMPT
        assert mime_type == "image/png"
        assert model_purpose_attributes() == {"pr.gen_ai.purpose": "document.read"}
        return "合成画像の説明"

    monkeypatch.setattr(chat_attachments, "_image_model_id", lambda: "model-a")
    monkeypatch.setattr(OciEnterpriseAiClient, "generate_from_image", generate)
    reader = chat_attachments._image_reader()
    assert reader is not None
    assert await reader(b"synthetic", "image/png") == "合成画像の説明"
    assert model_purpose_attributes() == {}


def test_requirement_purpose_reaches_selected_generate_function() -> None:
    import json
    from datetime import date

    from app.features.nl2sql.sql_requirement import RequirementContext, call_requirement_llm

    class Client:
        def is_configured(self) -> bool:
            return True

        def generate(self, **kwargs: Any) -> str:
            assert model_purpose_attributes() == {"pr.gen_ai.purpose": "requirement.clarify"}
            assert kwargs["prompt"] == "合成の質問"
            return json.dumps(
                {
                    "clarified_question_ja": "合成の質問",
                    "items": [],
                    "answerable": True,
                    "unanswerable_reason_ja": "",
                }
            )

    context = RequirementContext(
        question="合成の質問",
        tables={},
        relationships=[],
        profile_context={},
        ontology_concepts=[],
        evidence_corpus="",
        fiscal_year_start=None,
        today=date(2026, 1, 1),
    )
    output, _model = call_requirement_llm(
        Client(), question="合成の質問", context=context, mode="draft"
    )
    assert output.answerable
    assert model_purpose_attributes() == {}
