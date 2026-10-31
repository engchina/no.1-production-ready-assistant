"""Enterprise AI contract probe CLI の境界テスト。"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import asdict
from typing import Any

from app.clients.oci_enterprise_ai import OciEnterpriseAiClient
from app.config import Settings
from app.rag.enterprise_ai_probe import run_enterprise_ai_probe


async def test_enterprise_ai_probe_dry_run_redacts_payload_values() -> None:
    """dry-run は endpoint を呼ばず、prompt/context の raw 値を出さない。"""
    transport = FakeEnterpriseAiTransport({"answer": "unused"})
    client = OciEnterpriseAiClient(settings=_oci_settings(), http_transport=transport)

    report = await run_enterprise_ai_probe(
        surface="llm",
        dry_run=True,
        prompt="SECRET-PROMPT",
        context="SECRET-CONTEXT",
        settings=_oci_settings(),
        client=client,
    )

    assert report.ok is True
    result = report.results[0]
    assert result.stage == "preview"
    assert result.request["surface"] == "llm"
    assert result.request["payload_keys"] == [
        "input",
        "instructions",
        "max_output_tokens",
        "model",
        # 推論の量の既定（medium。#1425）を送る。
        "reasoning",
        "temperature",
    ]
    assert result.request["response_path_set"] is False
    assert transport.calls == []
    assert "SECRET-PROMPT" not in str(asdict(report))
    assert "SECRET-CONTEXT" not in str(asdict(report))


async def test_enterprise_ai_probe_invokes_llm_and_summarizes_text() -> None:
    """LLM probe は回答本文を出さず、parse 結果を長さだけで返す。"""
    settings = _oci_settings()
    transport = FakeEnterpriseAiTransport({"answer": "根拠に基づく probe 回答"})
    client = OciEnterpriseAiClient(settings=settings, http_transport=transport)

    report = await run_enterprise_ai_probe(surface="llm", settings=settings, client=client)

    assert report.ok is True
    result = report.results[0]
    assert result.stage == "parsed"
    assert result.parsed_output == {"text_chars": len("根拠に基づく probe 回答")}
    assert "根拠に基づく" not in str(asdict(report))
    assert transport.calls[0]["url"] == "https://enterprise-ai.example/llm/generate"


async def test_enterprise_ai_probe_invokes_vlm_and_summarizes_extraction() -> None:
    """VLM probe は OCR 本文を出さず、構造化抽出の件数だけを返す。"""
    settings = _oci_settings()
    transport = FakeEnterpriseAiTransport(
        {
            "data": {
                "raw_text": "probe OCR 本文",
                "document_type": "probe",
                "confidence": 0.9,
                "warnings": [],
                "elements": [{"kind": "text", "text": "probe OCR 本文"}],
            }
        }
    )
    client = OciEnterpriseAiClient(settings=settings, http_transport=transport)

    report = await run_enterprise_ai_probe(surface="vlm", settings=settings, client=client)

    assert report.ok is True
    result = report.results[0]
    assert result.stage == "parsed"
    assert result.parsed_output == {
        "raw_text_chars": len("probe OCR 本文"),
        "element_count": 1,
        "document_type_present": True,
    }
    assert "probe OCR 本文" not in str(asdict(report))
    assert transport.calls[0]["url"] == "https://enterprise-ai.example/vlm/extract"


async def test_enterprise_ai_probe_reports_payload_errors_without_transport_call() -> None:
    """endpoint/template 不備は transport 呼び出し前に payload stage で返す。"""
    settings = _oci_settings()
    settings.oci_enterprise_ai_endpoint = ""
    transport = FakeEnterpriseAiTransport({"answer": "unused"})
    client = OciEnterpriseAiClient(settings=settings, http_transport=transport)

    report = await run_enterprise_ai_probe(surface="llm", settings=settings, client=client)

    assert report.ok is False
    result = report.results[0]
    assert result.stage == "payload"
    assert result.error_type == "ValueError"
    assert transport.calls == []


class FakeEnterpriseAiTransport:
    """Enterprise AI HTTP transport fake。"""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []

    async def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Mapping[str, Any]:
        self.calls.append(
            {
                "url": url,
                "payload": dict(payload),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        return self._response

    async def stream_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> AsyncIterator[str]:
        self.calls.append(
            {
                "url": url,
                "payload": dict(payload),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        empty_lines: tuple[str, ...] = ()
        for line in empty_lines:
            yield line

    async def upload_file(
        self,
        url: str,
        file_name: str,
        content: bytes,
        *,
        mime_type: str,
        purpose: str,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Mapping[str, Any]:
        self.uploads.append(
            {
                "url": url,
                "file_name": file_name,
                "content": content,
                "mime_type": mime_type,
                "purpose": purpose,
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        return {"id": "file-test"}

    async def delete(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Mapping[str, Any]:
        self.deletes.append({"url": url, "headers": dict(headers), "timeout": timeout})
        return {"id": "file-test", "deleted": True}


def _oci_settings() -> Settings:
    """Enterprise AI probe 用の OCI 設定を返す。"""
    return Settings.model_construct(
        oci_region="ap-osaka-1",
        oci_compartment_id="ocid1.compartment.oc1..example",
        oci_enterprise_ai_endpoint="https://enterprise-ai.example",
        oci_enterprise_ai_project_ocid="ocid1.generativeaiproject.oc1..example",
        oci_enterprise_ai_api_key="sk-test-secret",
        oci_enterprise_ai_default_text_model="enterprise-llm",
        oci_enterprise_ai_default_vision_model="enterprise-vlm",
        oci_enterprise_ai_llm_path="/llm/generate",
        oci_enterprise_ai_vlm_path="/vlm/extract",
        oci_enterprise_ai_llm_payload_template="",
        oci_enterprise_ai_vlm_payload_template="",
        oci_enterprise_ai_llm_response_path="",
        oci_enterprise_ai_vlm_response_path="",
        oci_enterprise_ai_timeout_seconds=12.0,
        oci_enterprise_ai_max_retries=0,
    )
