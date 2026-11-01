"""OCI Enterprise AI adapter 境界のテスト。"""

import base64
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast

import httpx
import pytest

from app.clients.enterprise_ai import (
    EnterpriseAiClient,
    EnterpriseAiIncompleteResponseError,
    EnterpriseAiTimeoutError,
    EnterpriseAiUnsupportedInputError,
    EnterpriseAiValidationError,
    _raise_for_status_with_body,
)
from app.config import EnterpriseAiConfiguredModel, Settings


async def test_oci_vlm_posts_structured_extraction_payload() -> None:
    """OCI VLM adapter は Enterprise AI endpoint へ構造化抽出 payload を送る。"""
    transport = FakeEnterpriseAiTransport(
        {
            "data": {
                "raw_text": "社内規程: 経費申請",
                "document_type": "社内規程",
                "confidence": 0.91,
                "warnings": [],
            }
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    result = await client.extract_with_vlm(
        b"policy-bytes",
        "抽出してください",
        mime_type="application/pdf; charset=binary",
    )

    assert result["raw_text"] == "社内規程: 経費申請"
    assert result["document_type"] == "社内規程"
    assert result["elements"]
    assert "fields" not in result
    assert result["confidence"] == 0.91
    assert transport.uploads[0]["url"] == "https://enterprise-ai.example/files"
    assert transport.uploads[0]["file_name"] == "enterprise-ai-input.pdf"
    assert transport.uploads[0]["content"] == b"policy-bytes"
    assert transport.uploads[0]["mime_type"] == "application/pdf"
    assert transport.uploads[0]["purpose"] == "user_data"
    assert "content-type" not in transport.uploads[0]["headers"]
    assert transport.calls[0]["url"] == "https://enterprise-ai.example/vlm/extract"
    payload = transport.calls[0]["payload"]
    assert payload["model"] == "enterprise-vlm"
    assert payload["instructions"]
    assert "bbox" in payload["instructions"]
    assert "推測で作らない" in payload["instructions"]
    input_payload = payload["input"]
    assert isinstance(input_payload, list)
    content = input_payload[0]["content"]
    assert content[0] == {"type": "input_file", "file_id": "file-test"}
    assert content[1] == {"type": "input_text", "text": "抽出してください"}
    text_format = payload["text"]["format"]
    assert text_format["type"] == "json_schema"
    assert text_format["name"] == "structured_extraction"
    assert "elements" in text_format["schema"]["properties"]
    elements_schema = text_format["schema"]["properties"]["elements"]
    assert elements_schema["maxItems"] == 120
    element_properties = elements_schema["items"]["properties"]
    assert "text" in element_properties["kind"]["enum"]
    assert "equation" in element_properties["kind"]["enum"]
    assert "bbox" in element_properties
    assert "element_id" in element_properties
    assert "parent_id" in element_properties
    assert "content_kind" in element_properties
    assert "confidence" in element_properties
    assert "metadata" not in element_properties
    assert "pages" in text_format["schema"]["properties"]
    assert "tables" in text_format["schema"]["properties"]
    assert "assets" in text_format["schema"]["properties"]
    table_schema = text_format["schema"]["properties"]["tables"]["items"]
    assert "cells" in table_schema["properties"]
    cell_properties = table_schema["properties"]["cells"]["items"]["properties"]
    assert "bbox" in cell_properties
    # 出力の上限は Vision のモデルの値(#1719。登録の無いモデルは既定の 32768)。
    assert payload["max_output_tokens"] == 32768
    assert transport.deletes[0]["url"] == "https://enterprise-ai.example/files/file-test"


async def test_oci_vlm_decodes_text_documents_without_calling_vlm() -> None:
    """text/plain 等は OCR(VLM)を呼ばず、バイト列を直接本文にする。"""
    transport = FakeEnterpriseAiTransport({})
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    result = await client.extract_with_vlm(
        "社内規程\nクラウド利用料は申請が必要です。".encode(),
        "OCR",
        mime_type="text/plain; charset=utf-8",
    )

    assert result["raw_text"] == "社内規程\nクラウド利用料は申請が必要です。"
    assert result["confidence"] == 1.0
    assert result["elements"]
    # VLM endpoint / Files API には一切アクセスしない。
    assert transport.calls == []
    assert transport.uploads == []
    assert transport.deletes == []


async def test_oci_vlm_decodes_cp932_text_documents() -> None:
    """UTF-8 で復号できない日本語テキストも cp932 で復号する。"""
    transport = FakeEnterpriseAiTransport({})
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    result = await client.extract_with_vlm(
        "経費精算メモ".encode("cp932"),
        "OCR",
        mime_type="text/plain",
    )

    assert result["raw_text"] == "経費精算メモ"
    assert transport.calls == []


async def test_oci_vlm_decodes_gb18030_text_documents() -> None:
    """GB18030 で保存された日本語テキストも文字コード検出で復号する。"""
    transport = FakeEnterpriseAiTransport({})
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    # 中国語環境などで GB18030 保存された日本語本文。固定候補(cp932)では誤デコードされる。
    plain = "昭和三十年代の日本の農村を舞台に、姉妹とトトロの交流を描いたアニメーション映画である。"
    result = await client.extract_with_vlm(
        (plain * 4).encode("gb18030"),
        "OCR",
        mime_type="text/plain",
    )

    assert result["raw_text"] == plain * 4
    assert transport.calls == []


async def test_oci_vlm_posts_image_data_url_without_files_api() -> None:
    """画像入力は OpenAI Responses の base64 data URL として送る。"""
    settings = _llm_settings()
    settings.enterprise_ai_vlm_input_mode = "inline_image"
    transport = FakeEnterpriseAiTransport(
        {
            "data": {
                "raw_text": "PNG OCR",
                "document_type": "画像",
                "confidence": 0.9,
                "warnings": [],
            }
        }
    )
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    await client.extract_with_vlm(b"png-bytes", "OCR", mime_type="image/png")

    assert transport.uploads == []
    content = transport.calls[0]["payload"]["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "OCR"}
    image_url = content[1]["image_url"]
    assert content[1]["type"] == "input_image"
    assert image_url.startswith("data:image/png;base64,")
    assert base64.b64decode(image_url.split(",", maxsplit=1)[1]) == b"png-bytes"


async def test_oci_vlm_files_api_mode_uploads_images() -> None:
    """VLM 入力方式を Files API にすると画像も /files 経由で送る。"""
    settings = _llm_settings()
    settings.enterprise_ai_vlm_input_mode = "files_api"
    transport = FakeEnterpriseAiTransport(
        {
            "data": {
                "raw_text": "PNG OCR",
                "document_type": "画像",
                "confidence": 0.9,
                "warnings": [],
            }
        }
    )
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    await client.extract_with_vlm(b"png-bytes", "OCR", mime_type="image/png")

    assert transport.uploads[0]["url"] == "https://enterprise-ai.example/files"
    assert transport.uploads[0]["file_name"] == "enterprise-ai-input.png"
    assert transport.uploads[0]["content"] == b"png-bytes"
    assert transport.uploads[0]["mime_type"] == "image/png"
    assert transport.uploads[0]["purpose"] == "vision"
    content = transport.calls[0]["payload"]["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "OCR"}
    assert content[1] == {"type": "input_image", "file_id": "file-test"}
    assert transport.deletes[0]["url"] == "https://enterprise-ai.example/files/file-test"


async def test_oci_vlm_inline_image_mode_rejects_pdf() -> None:
    """inline_image は PDF fallback を暗黙変換せず、設定変更を促す。"""
    settings = _llm_settings()
    settings.enterprise_ai_vlm_input_mode = "inline_image"
    transport = FakeEnterpriseAiTransport({})
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    with pytest.raises(EnterpriseAiUnsupportedInputError, match="Files API"):
        await client.extract_with_vlm(b"pdf-bytes", "OCR", mime_type="application/pdf")

    assert transport.uploads == []
    assert transport.calls == []
    assert transport.deletes == []


async def test_oci_vlm_omits_json_schema_for_gemini_provider() -> None:
    """Gemini provider には Responses JSON Schema ではなく prompt 契約で JSON 出力させる。"""
    settings = _llm_settings()
    settings.enterprise_ai_default_vision_model = "google.gemini-2.5-flash"
    transport = FakeEnterpriseAiTransport(
        {
            "output": (
                '{"raw_text":"PDF OCR 本文","document_type":"ドキュメント",'
                '"confidence":0.8,"warnings":[]}'
            )
        }
    )
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    result = await client.extract_with_vlm(
        b"pdf-bytes",
        "OCR してください",
        mime_type="application/pdf",
    )

    assert result["raw_text"] == "PDF OCR 本文"
    assert transport.uploads[0]["purpose"] == "user_data"
    payload = transport.calls[0]["payload"]
    assert payload["model"] == "google.gemini-2.5-flash"
    assert "text" not in payload
    assert "説明文なしの JSON object" in payload["instructions"]
    content = payload["input"][0]["content"]
    assert content[0] == {"type": "input_file", "file_id": "file-test"}
    assert content[1]["type"] == "input_text"
    assert "OCR してください" in content[1]["text"]
    assert "必須キー: raw_text" in content[1]["text"]
    assert transport.deletes[0]["url"] == "https://enterprise-ai.example/files/file-test"


async def test_oci_vision_smoke_test_uses_minimal_openai_image_payload() -> None:
    """Vision 接続確認は JSON schema なしの最小 OpenAI image payload を使う。"""
    settings = _llm_settings()
    settings.enterprise_ai_vlm_input_mode = "files_api"
    transport = FakeEnterpriseAiTransport({"output_text": "画像を確認しました。"})
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    result = await client.generate_from_image(b"png-bytes", "画像を確認", mime_type="image/png")

    assert result == "画像を確認しました。"
    assert transport.uploads == []
    payload = transport.calls[0]["payload"]
    assert payload["model"] == "enterprise-vlm"
    assert "text" not in payload
    content = payload["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "画像を確認"}
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")


async def test_oci_vlm_accepts_json_string_payload() -> None:
    """VLM response が JSON 文字列の場合も StructuredExtraction として検証する。"""
    transport = FakeEnterpriseAiTransport(
        {
            "output": (
                '{"raw_text":"本文","document_type":"マニュアル","confidence":0.8,"warnings":[]}'
            )
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    result = await client.extract_with_vlm(b"document", "prompt")

    assert result["raw_text"] == "本文"
    assert result["document_type"] == "マニュアル"
    assert result["confidence"] == 0.8


async def test_oci_vlm_accepts_prediction_content_json_payload() -> None:
    """model deployment 風の predictions/content envelope も VLM 結果として受け取る。"""
    transport = FakeEnterpriseAiTransport(
        {
            "predictions": [
                {
                    "content": (
                        '{"raw_text":"規程本文","document_type":"社内規程",'
                        '"confidence":0.87,"warnings":[]}'
                    )
                }
            ]
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    result = await client.extract_with_vlm(b"document", "prompt")

    assert result["raw_text"] == "規程本文"
    assert result["document_type"] == "社内規程"
    assert result["confidence"] == 0.87


async def test_oci_vlm_accepts_choices_message_content_json_payload() -> None:
    """gateway が choices[].message.content に返す JSON も VLM 結果として受け取る。"""
    transport = FakeEnterpriseAiTransport(
        {
            "choices": [
                {
                    "message": {
                        "content": (
                            '{"raw_text":"請求書 OCR 本文","document_type":"請求書",'
                            '"confidence":0.92,"warnings":[]}'
                        )
                    }
                }
            ]
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    result = await client.extract_with_vlm(b"invoice", "OCR")

    assert result["raw_text"] == "請求書 OCR 本文"
    assert result["document_type"] == "請求書"
    assert result["confidence"] == 0.92


async def test_oci_vlm_accepts_tool_call_arguments_payload() -> None:
    """tool_calls[].function.arguments に包まれた VLM JSON も受け取る。"""
    transport = FakeEnterpriseAiTransport(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "structured_extraction",
                                    "arguments": (
                                        '{"raw_text":"tool OCR 本文",'
                                        '"document_type":"マニュアル",'
                                        '"confidence":0.88,"warnings":[]}'
                                    ),
                                }
                            }
                        ]
                    }
                }
            ]
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    result = await client.extract_with_vlm(b"manual", "OCR")

    assert result["raw_text"] == "tool OCR 本文"
    assert result["document_type"] == "マニュアル"
    assert result["confidence"] == 0.88


async def test_oci_vlm_accepts_fenced_json_payload() -> None:
    """Markdown fenced JSON に包まれた VLM response も検証する。"""
    transport = FakeEnterpriseAiTransport(
        {
            "choices": [
                {
                    "message": {
                        "content": (
                            "```json\n"
                            '{"raw_text":"fenced OCR 本文",'
                            '"document_type":"仕様書",'
                            '"confidence":0.86,"warnings":[]}'
                            "\n```"
                        )
                    }
                }
            ]
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    result = await client.extract_with_vlm(b"spec", "OCR")

    assert result["raw_text"] == "fenced OCR 本文"
    assert result["document_type"] == "仕様書"
    assert result["confidence"] == 0.86


async def test_oci_vlm_repairs_unescaped_quotes_in_json_payload() -> None:
    """文字列値の途中にエスケープ漏れの二重引用符があっても修復して読む。"""
    transport = FakeEnterpriseAiTransport(
        {
            "output": (
                "{\n"
                '  "raw_text": "項目 "A" の説明です。",\n'
                '  "document_type": "マニュアル",\n'
                '  "confidence": 0.81,\n'
                '  "warnings": []\n'
                "}"
            )
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    result = await client.extract_with_vlm(b"document", "prompt")

    assert result["raw_text"] == '項目 "A" の説明です。'
    assert result["document_type"] == "マニュアル"
    assert result["confidence"] == 0.81


async def test_oci_vlm_repairs_raw_newlines_and_trailing_commas_in_json_payload() -> None:
    """文字列内の生の改行と末尾カンマを含む壊れた JSON も修復して読む。"""
    transport = FakeEnterpriseAiTransport(
        {
            "output": (
                "{\n"
                '  "raw_text": "1 行目\n2 行目",\n'
                '  "document_type": "報告書",\n'
                '  "confidence": 0.7,\n'
                '  "warnings": [],\n'
                "}"
            )
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    result = await client.extract_with_vlm(b"document", "prompt")

    assert result["raw_text"] == "1 行目\n2 行目"
    assert result["document_type"] == "報告書"


async def test_oci_vlm_coerces_string_section_path_in_elements() -> None:
    """elements[].section_path が ``/章/節`` 文字列でも list へ正規化して検証する。"""
    transport = FakeEnterpriseAiTransport(
        {
            "output": (
                '{"raw_text":"本文","document_type":"資料","confidence":0.8,'
                '"warnings":[],"elements":[{"kind":"text","text":"段落",'
                '"order":0,"section_path":"/第1章/1.2 概要"}]}'
            )
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    result = await client.extract_with_vlm(b"document", "prompt")

    elements = cast(list[dict[str, Any]], result["elements"])
    assert elements[0]["section_path"] == ["第1章", "1.2 概要"]


async def test_oci_vlm_reports_schema_validation_details() -> None:
    """VLM の schema 不整合は ValidationError 型名だけでなく失敗項目を返す。"""
    transport = FakeEnterpriseAiTransport(
        {"output": ('{"raw_text":"本文","document_type":"資料","confidence":1.4,"warnings":[]}')}
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    with pytest.raises(EnterpriseAiValidationError, match="confidence") as exc_info:
        await client.extract_with_vlm(b"document", "prompt")

    assert getattr(exc_info.value, "safe_for_user", False) is True
    assert "StructuredExtraction schema" in str(exc_info.value)
    assert "失敗項目" in str(exc_info.value)


async def test_oci_vlm_retries_schema_validation_with_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一時的な VLM schema 不整合は指数 backoff で再取得する。"""
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    # retry の sleep は共有 core(rag_parser_core)へ移設済み。
    monkeypatch.setattr("rag_parser_core.enterprise_ai.asyncio.sleep", fake_sleep)
    settings = _llm_settings()
    settings.enterprise_ai_max_retries = 2
    transport = SequentialEnterpriseAiTransport(
        [
            {"output": '{"raw_text":"本文","confidence":1.4}'},
            {
                "output": (
                    '{"raw_text":"再取得した本文","document_type":"資料",'
                    '"confidence":0.74,"warnings":[]}'
                )
            },
        ]
    )
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    result = await client.extract_with_vlm(b"png-bytes", "OCR", mime_type="image/png")

    assert result["raw_text"] == "再取得した本文"
    assert len(transport.calls) == 2
    assert delays == [0.25]


async def test_oci_vlm_raises_actionable_error_when_file_input_unsupported() -> None:
    """provider が ZDR で file 入力を拒否したら actionable な safe エラーへ変換する。"""
    transport = UnsupportedFileInputTransport({})
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    with pytest.raises(EnterpriseAiUnsupportedInputError, match="ファイル") as exc_info:
        await client.extract_with_vlm(b"%PDF-1.7", "prompt", mime_type="application/pdf")

    assert getattr(exc_info.value, "safe_for_user", False) is True
    # アップロード済み一時ファイルは best-effort で削除される。
    assert transport.deletes


async def test_oci_vlm_uses_configured_response_path() -> None:
    """VLM response が深い custom envelope でも JSON Pointer で候補を選べる。"""
    settings = _llm_settings()
    settings.enterprise_ai_vlm_response_path = "/payload/results/0/document"
    transport = FakeEnterpriseAiTransport(
        {
            "payload": {
                "results": [
                    {
                        "document": (
                            '{"raw_text":"深い VLM 本文",'
                            '"document_type":"契約書",'
                            '"confidence":0.83,"warnings":[]}'
                        )
                    }
                ]
            }
        }
    )
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    result = await client.extract_with_vlm(b"document", "OCR")

    assert result["raw_text"] == "深い VLM 本文"
    assert result["document_type"] == "契約書"
    assert result["confidence"] == 0.83


async def test_oci_vlm_uses_configured_payload_template() -> None:
    """VLM endpoint 固有の request shape は JSON template で差し替えられる。"""
    settings = _llm_settings()
    settings.enterprise_ai_default_vision_model = ""
    settings.enterprise_ai_vlm_payload_template = (
        '{"servingMode":"custom","inputs":[{"mimeType":"${mime_type}",'
        '"bytes":"${data_base64}"}],"schema":"${structured_extraction_schema}",'
        '"prompt":"${prompt}"}'
    )
    transport = FakeEnterpriseAiTransport(
        {
            "data": {
                "raw_text": "本文",
                "document_type": "ドキュメント",
                "confidence": 0.7,
                "warnings": [],
            }
        }
    )
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    await client.extract_with_vlm(b"pdf", "OCR", mime_type="application/pdf")

    payload = transport.calls[0]["payload"]
    assert payload["servingMode"] == "custom"
    assert payload["inputs"][0]["mimeType"] == "application/pdf"
    assert base64.b64decode(payload["inputs"][0]["bytes"]) == b"pdf"
    assert payload["schema"]["title"] == "StructuredExtraction"
    assert payload["prompt"] == "OCR"
    assert transport.uploads == []
    assert transport.deletes == []


async def test_oci_generate_posts_rag_generation_payload() -> None:
    """OCI LLM adapter は RAG 専用 prompt を Enterprise AI endpoint へ送る。"""
    transport = FakeEnterpriseAiTransport(
        {"choices": [{"message": {"content": "根拠に基づく回答です。"}}]}
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    answer = await client.generate("承認条件は？", "[policy.txt#doc-1:0]\n承認条件: 120,000")

    assert answer == "根拠に基づく回答です。"
    assert transport.calls[0]["url"] == "https://enterprise-ai.example/llm/generate"
    payload = transport.calls[0]["payload"]
    assert payload["model"] == "enterprise-llm"
    assert payload["instructions"]
    assert payload["temperature"] == 0.0
    # 出力の上限はテキストのモデルの値(#1719。登録の無いモデルは既定の 32768)。
    assert payload["max_output_tokens"] == 32768
    input_items = payload["input"]
    assert isinstance(input_items, list)
    assert input_items[0]["role"] == "user"
    assert "OCI Generative AI" not in str(input_items)
    assert "承認条件は？" in input_items[0]["content"]
    assert "[policy.txt#doc-1:0]" in input_items[0]["content"]


async def test_oci_generate_uses_configured_default_model() -> None:
    """複数 LLM 登録時は既定モデルを回答生成に使う。"""
    settings = _llm_settings()
    settings.enterprise_ai_models = [
        EnterpriseAiConfiguredModel(model_id="enterprise-small", display_name="Small"),
        EnterpriseAiConfiguredModel(
            model_id="enterprise-default",
            display_name="Default",
            vision_enabled=True,
        ),
    ]
    settings.enterprise_ai_default_text_model = "enterprise-default"
    transport = FakeEnterpriseAiTransport({"answer": "回答"})
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    assert await client.generate("質問", "根拠") == "回答"

    assert transport.calls[0]["payload"]["model"] == "enterprise-default"


async def test_oci_vlm_uses_default_model_when_it_supports_vision() -> None:
    """既定の Vision モデルを明示しない環境では、既定モデルが Vision 対応なら OCR でも使う。"""
    settings = _llm_settings()
    settings.enterprise_ai_models = [
        EnterpriseAiConfiguredModel(model_id="enterprise-text", display_name="Text"),
        EnterpriseAiConfiguredModel(
            model_id="enterprise-default",
            display_name="Default Vision",
            vision_enabled=True,
        ),
    ]
    settings.enterprise_ai_default_text_model = "enterprise-default"
    settings.enterprise_ai_default_vision_model = ""
    transport = FakeEnterpriseAiTransport(
        {
            "data": {
                "raw_text": "画像本文",
                "document_type": "ドキュメント",
                "confidence": 0.9,
                "warnings": [],
            }
        }
    )
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    await client.extract_with_vlm(b"image", "OCR")

    assert transport.calls[0]["payload"]["model"] == "enterprise-default"


async def test_oci_vlm_uses_first_vision_model_when_default_is_text_only() -> None:
    """既定の Vision モデルを明示せず既定モデルが text-only なら、Vision 対応モデルを使う。"""
    settings = _llm_settings()
    settings.enterprise_ai_models = [
        EnterpriseAiConfiguredModel(model_id="enterprise-default", display_name="Default"),
        EnterpriseAiConfiguredModel(
            model_id="enterprise-vision",
            display_name="Vision",
            vision_enabled=True,
        ),
    ]
    settings.enterprise_ai_default_text_model = "enterprise-default"
    settings.enterprise_ai_default_vision_model = ""
    transport = FakeEnterpriseAiTransport(
        {
            "data": {
                "raw_text": "画像本文",
                "document_type": "ドキュメント",
                "confidence": 0.9,
                "warnings": [],
            }
        }
    )
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    await client.extract_with_vlm(b"image", "OCR")

    assert transport.calls[0]["payload"]["model"] == "enterprise-vision"


async def test_oci_adapter_adds_project_and_api_key_headers() -> None:
    """API key 認証では project OCID と Bearer token を header に載せる。"""
    settings = _llm_settings()
    settings.enterprise_ai_api_key = "sk-test-secret"
    transport = FakeEnterpriseAiTransport({"answer": "回答"})
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    assert await client.generate("質問", "根拠") == "回答"

    headers = transport.calls[0]["headers"]
    assert headers["OpenAI-Project"] == "ocid1.generativeaiproject.oc1..example"
    assert headers["Authorization"] == "Bearer sk-test-secret"


async def test_oci_adapter_requires_api_key_before_endpoint_call() -> None:
    """Enterprise AI gateway の API key 未設定は HTTP 送信前に検出する。"""
    settings = _llm_settings()
    settings.enterprise_ai_api_key = ""
    transport = FakeEnterpriseAiTransport({"answer": "回答"})
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    with pytest.raises(ValueError, match="OCI Enterprise AI API key"):
        await client.generate("質問", "根拠")

    assert transport.calls == []


async def test_oci_adapter_wraps_read_timeout_and_deletes_uploaded_file() -> None:
    """Responses の ReadTimeout は利用者向け timeout エラーへ正規化する。"""
    settings = _llm_settings()
    settings.enterprise_ai_timeout_seconds = 600.0
    transport = TimeoutEnterpriseAiTransport({})
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    with pytest.raises(EnterpriseAiTimeoutError, match="600 秒"):
        await client.extract_with_vlm(b"pdf-bytes", "OCR", mime_type="application/pdf")

    assert transport.uploads
    assert transport.calls
    assert transport.deletes[0]["url"] == "https://enterprise-ai.example/files/file-test"


async def test_oci_generate_accepts_inference_response_content_parts() -> None:
    """LLM response は inference_response / content parts 形式も text として解釈する。"""
    transport = FakeEnterpriseAiTransport(
        {
            "inference_response": {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"text": "根拠1に基づく回答です。"},
                                {"text": "補足は根拠2です。"},
                            ]
                        }
                    }
                ]
            }
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    answer = await client.generate("質問", "根拠")

    assert answer == "根拠1に基づく回答です。\n補足は根拠2です。"


async def test_oci_generate_accepts_openai_responses_output_text() -> None:
    """標準 OpenAI Responses object は top-level text ではなく output から本文を読む。"""
    transport = FakeEnterpriseAiTransport(
        {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "text": {"format": {"type": "text"}},
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Responses API 由来の回答です。",
                        }
                    ],
                }
            ],
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    answer = await client.generate("質問", "根拠")

    assert answer == "Responses API 由来の回答です。"


async def test_oci_vlm_accepts_openai_responses_output_json() -> None:
    """標準 Responses object の output_text JSON を StructuredExtraction として読む。"""
    transport = FakeEnterpriseAiTransport(
        {
            "id": "resp_vlm",
            "object": "response",
            "status": "completed",
            "text": {"format": {"type": "json_schema"}},
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": (
                                '{"raw_text":"Responses OCR 本文",'
                                '"document_type":"画像",'
                                '"confidence":0.82,"warnings":[],'
                                '"elements":[{"kind":"text","text":"Responses OCR 本文",'
                                '"bbox":[755,17,765,66,755,964,765,970]}]}'
                            ),
                        }
                    ],
                }
            ],
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    result = await client.extract_with_vlm(b"image", "OCR")

    assert result["raw_text"] == "Responses OCR 本文"
    assert result["document_type"] == "画像"
    elements = cast(list[dict[str, object]], result["elements"])
    assert elements[0]["bbox"] == [755.0, 17.0, 765.0, 970.0]


async def test_oci_vlm_reports_incomplete_max_output_tokens() -> None:
    """Responses API の max_output_tokens incomplete は専用エラーにする。"""
    transport = FakeEnterpriseAiTransport(
        {
            "id": "resp_incomplete",
            "object": "response",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [],
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    with pytest.raises(EnterpriseAiIncompleteResponseError, match="max_output_tokens"):
        await client.extract_with_vlm(b"pdf-bytes", "OCR", mime_type="application/pdf")


async def test_oci_incomplete_message_reports_reason_limit_and_used_tokens() -> None:
    """出力の上限で未完了の失敗の文に、理由・上限・使った token(うち推論)を出す(#1719)。"""
    transport = FakeEnterpriseAiTransport(
        {
            "id": "resp_incomplete",
            "object": "response",
            "status": "incomplete",
            "model": "enterprise-llm",
            "max_output_tokens": 1200,
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {
                "input_tokens": 900,
                "output_tokens": 1200,
                "output_tokens_details": {"reasoning_tokens": 1198},
            },
            "output": [],
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    with pytest.raises(EnterpriseAiIncompleteResponseError) as raised:
        await client.generate("質問", "根拠")

    message = str(raised.value)
    assert "モデル「enterprise-llm」" in message
    assert "理由=max_output_tokens" in message
    assert "出力の上限=1200 tokens" in message
    assert "使った出力=1200 tokens(うち推論 1198)" in message
    assert "出力の上限(tokens)" in message


@pytest.mark.parametrize("vision_output_limit", [8192, 32768])
async def test_oci_payloads_use_each_model_output_limit(vision_output_limit: int) -> None:
    """テキストと Vision の呼び出しは、それぞれのモデルの出力の上限を送る(#1719)。"""
    settings = _llm_settings()
    settings.enterprise_ai_models = [
        EnterpriseAiConfiguredModel(model_id="enterprise-llm", max_output_tokens=16000),
        EnterpriseAiConfiguredModel(
            model_id="enterprise-vlm",
            max_input_tokens=200000,
            max_output_tokens=vision_output_limit,
        ),
    ]
    transport = FakeEnterpriseAiTransport({"output_text": "回答"})
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    await client.generate("質問", "根拠")
    await client.generate_from_images([b"png"], "読み取ってください")
    await client.generate_from_image(b"png", "確認してください")

    assert transport.calls[0]["payload"]["max_output_tokens"] == 16000
    assert transport.calls[1]["payload"]["max_output_tokens"] == vision_output_limit
    assert transport.calls[2]["payload"]["max_output_tokens"] == vision_output_limit


async def test_oci_generate_reports_openai_responses_error_before_empty_text() -> None:
    """Responses API の failed status/error は空 text ではなく実エラーとして返す。"""
    transport = FakeEnterpriseAiTransport(
        {
            "id": "resp_failed",
            "object": "response",
            "status": "failed",
            "error": {
                "type": "invalid_request_error",
                "message": "Unsupported request field: parameters",
            },
            "output": [],
            "text": {"format": {"type": "text"}},
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    with pytest.raises(ValueError, match="Unsupported request field"):
        await client.generate("質問", "根拠")


def test_oci_http_status_error_includes_response_body() -> None:
    """HTTP 400 は OCI の response body を含む例外にする。"""
    request = httpx.Request("POST", "https://enterprise-ai.example/responses")
    response = httpx.Response(
        400,
        text='{"error":{"message":"image input must be uploaded with Files API"}}',
        request=request,
    )

    with pytest.raises(httpx.HTTPStatusError, match="Files API"):
        _raise_for_status_with_body(response, "OCI Enterprise AI endpoint")


async def test_oci_generate_accepts_tool_call_answer_payload() -> None:
    """LLM の tool/function arguments にある answer も回答として扱う。"""
    transport = FakeEnterpriseAiTransport(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {"function": {"arguments": '{"answer":"tool call 由来の回答です。"}'}}
                        ]
                    }
                }
            ]
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    answer = await client.generate("質問", "根拠")

    assert answer == "tool call 由来の回答です。"


async def test_oci_generate_uses_configured_response_path() -> None:
    """LLM response が深い custom envelope でも JSON Pointer で候補を選べる。"""
    settings = _llm_settings()
    settings.enterprise_ai_llm_response_path = "/payload/results/0/generated/text"
    transport = FakeEnterpriseAiTransport(
        {"payload": {"results": [{"generated": {"text": "深い envelope 由来の回答です。"}}]}}
    )
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    answer = await client.generate("質問", "根拠")

    assert answer == "深い envelope 由来の回答です。"


async def test_oci_generate_uses_configured_payload_template() -> None:
    """LLM endpoint 固有の request shape は JSON template で差し替えられる。"""
    settings = _llm_settings()
    settings.enterprise_ai_default_text_model = ""
    settings.enterprise_ai_llm_payload_template = (
        '{"input":{"messages":"${messages}","params":"${parameters}"},'
        '"metadata":{"task":"${task}","language":"${language}"}}'
    )
    transport = FakeEnterpriseAiTransport({"output_text": "テンプレート回答"})
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    answer = await client.generate("質問", "根拠")

    assert answer == "テンプレート回答"
    payload = transport.calls[0]["payload"]
    assert payload["metadata"] == {"task": "rag_answer_generation", "language": "ja"}
    assert payload["input"]["params"] == {"temperature": 0.0, "max_output_tokens": 32768}
    assert payload["input"]["messages"][0]["role"] == "system"
    assert payload["input"]["messages"][1]["role"] == "user"
    assert "質問" in payload["input"]["messages"][1]["content"]
    assert "根拠" in payload["input"]["messages"][1]["content"]


async def test_oci_generate_rejects_empty_text_response() -> None:
    """LLM response に text がない場合は fail fast する。"""
    client = EnterpriseAiClient(
        settings=_llm_settings(),
        http_transport=FakeEnterpriseAiTransport({"data": {"unexpected": "shape"}}),
    )

    with pytest.raises(ValueError, match="回答 text"):
        await client.generate("質問", "根拠")


async def test_oci_adapter_requires_endpoint_before_transport_call() -> None:
    """Enterprise AI endpoint 未設定は外部呼び出し前に検出する。"""
    transport = FakeEnterpriseAiTransport({"answer": "unused"})
    settings = _llm_settings()
    settings.enterprise_ai_endpoint = ""
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    with pytest.raises(ValueError, match="endpoint"):
        await client.generate("質問", "根拠")

    assert transport.calls == []


async def test_oci_adapter_uses_absolute_path_override() -> None:
    """path に完全 URL を指定した場合は endpoint base より優先する。"""
    settings = _llm_settings()
    settings.enterprise_ai_llm_path = "https://private.example/custom/generate"
    transport = FakeEnterpriseAiTransport({"answer": "回答"})
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    assert await client.generate("質問", "根拠") == "回答"
    assert transport.calls[0]["url"] == "https://private.example/custom/generate"


async def test_oci_adapter_rejects_unknown_payload_template_placeholder() -> None:
    """payload template の placeholder typo は外部呼び出し前に検出する。"""
    settings = _llm_settings()
    settings.enterprise_ai_llm_payload_template = '{"input":"${missing}"}'
    transport = FakeEnterpriseAiTransport({"answer": "unused"})
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    with pytest.raises(ValueError, match="未対応の placeholder"):
        await client.generate("質問", "根拠")

    assert transport.calls == []


async def test_oci_adapter_rejects_invalid_response_path() -> None:
    """response path typo は空回答として扱わず明示的に失敗する。"""
    settings = _llm_settings()
    settings.enterprise_ai_llm_response_path = "/missing/text"
    transport = FakeEnterpriseAiTransport({"payload": {"text": "unused"}})
    client = EnterpriseAiClient(settings=settings, http_transport=transport)

    with pytest.raises(ValueError, match="key が見つかりません"):
        await client.generate("質問", "根拠")

    assert transport.calls


class FakeEnterpriseAiTransport:
    """Enterprise AI HTTP transport の fake。"""

    def __init__(
        self,
        response: Mapping[str, Any],
        *,
        stream_lines: list[str] | None = None,
    ) -> None:
        self._response = response
        self._stream_lines = stream_lines or []
        self.calls: list[dict[str, Any]] = []
        self.stream_calls: list[dict[str, Any]] = []
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
        self.stream_calls.append(
            {
                "url": url,
                "payload": dict(payload),
                "headers": dict(headers),
                "timeout": timeout,
            }
        )
        for line in self._stream_lines:
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


class SequentialEnterpriseAiTransport(FakeEnterpriseAiTransport):
    """JSON POST ごとに別 response を返す fake。"""

    def __init__(self, responses: list[Mapping[str, Any]]) -> None:
        super().__init__({})
        self._responses = responses

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
        index = min(len(self.calls) - 1, len(self._responses) - 1)
        return self._responses[index]


class TimeoutEnterpriseAiTransport(FakeEnterpriseAiTransport):
    """JSON POST だけ ReadTimeout を返す fake。"""

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
        raise httpx.ReadTimeout("read timed out")


class UnsupportedFileInputTransport(FakeEnterpriseAiTransport):
    """JSON POST で provider の ZDR file 非対応 400 を返す fake。"""

    async def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> Mapping[str, Any]:
        self.calls.append({"url": url, "payload": dict(payload)})
        request = httpx.Request("POST", url)
        response = httpx.Response(
            400,
            request=request,
            text=(
                '{"error":{"code":"invalid_value","message":'
                '"Status Code from provider: 400, Provider response: '
                '{\\"code\\":\\"invalid-argument\\",\\"error\\":'
                '\\"File content is currently unsupported for ZDR customers.\\"}",'
                '"type":"invalid_request_error"}}'
            ),
        )
        raise httpx.HTTPStatusError("400 Bad Request", request=request, response=response)


def _llm_settings() -> Settings:
    return Settings.model_construct(
        enterprise_ai_endpoint="https://enterprise-ai.example",
        enterprise_ai_project_id="ocid1.generativeaiproject.oc1..example",
        enterprise_ai_api_key="sk-test-secret",
        enterprise_ai_default_text_model="enterprise-llm",
        enterprise_ai_default_vision_model="enterprise-vlm",
        enterprise_ai_llm_path="/llm/generate",
        enterprise_ai_vlm_path="/vlm/extract",
        enterprise_ai_llm_payload_template="",
        enterprise_ai_vlm_payload_template="",
        enterprise_ai_llm_response_path="",
        enterprise_ai_vlm_response_path="",
        enterprise_ai_timeout_seconds=12.0,
        enterprise_ai_max_retries=0,
    )


# --- 推論(思考)を返すモデルの応答(#1423) ---------------------------------------------------
# 2026-10-11 に OCI Enterprise AI の `/responses` から実際に返った形(claude-opus-5 と
# openai.gpt-oss-120b)を縮めて録画したもの。`output` の先頭に `type=reasoning` の item が付き、
# その `content` に英語の思考(`reasoning_text`)が入る。

_REASONING_TEXT = (
    'The user is asking "What is the capital of Japan?" The evidence clearly states it. '
    'I could answer {"answer": "wrong"} but I\'ll answer in one sentence in Japanese.'
)


def _recorded_reasoning_response(message_text: str) -> dict[str, Any]:
    return {
        "id": "resp_recorded",
        "object": "response",
        "status": "completed",
        "model": "claude-opus-5",
        "reasoning": None,
        "text": None,
        "output": [
            {
                "id": "rs_recorded",
                "summary": [],
                "type": "reasoning",
                "content": [{"text": _REASONING_TEXT, "type": "reasoning_text"}],
                "encrypted_content": None,
                "status": None,
            },
            {
                "id": "msg_recorded",
                "content": [
                    {
                        "annotations": [],
                        "text": message_text,
                        "type": "output_text",
                        "logprobs": None,
                    }
                ],
                "role": "assistant",
                "status": "completed",
                "type": "message",
            },
        ],
        "usage": {
            "input_tokens": 150,
            "output_tokens": 137,
            "output_tokens_details": {"reasoning_tokens": 128},
        },
    }


async def test_oci_generate_drops_reasoning_item_from_answer() -> None:
    """Responses の reasoning item の思考は本文に入れない(#1423)。"""
    response = _recorded_reasoning_response("\n\n日本の首都は東京です。")
    transport = FakeEnterpriseAiTransport(response)
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    answer = await client.generate("質問", "根拠")

    assert answer == "日本の首都は東京です。"


async def test_oci_generate_parses_structured_answer_after_reasoning() -> None:
    """思考に `{...}` が含まれても、構造化出力の JSON は message の本文から読む(#1423)。"""
    transport = FakeEnterpriseAiTransport(
        _recorded_reasoning_response('{\n  "answer": "日本の首都は東京です。"\n}')
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    answer = await client.generate("質問", "根拠")

    # 構造化出力の本文(JSON)だけを返し、呼び出し側の JSON の解析が通る。
    assert json.loads(answer) == {"answer": "日本の首都は東京です。"}


async def test_oci_vlm_parses_structured_extraction_after_reasoning() -> None:
    """VLM の構造化抽出も、思考の item を除いた JSON から作る(#1423)。"""
    transport = FakeEnterpriseAiTransport(
        _recorded_reasoning_response(
            '{"raw_text":"請求書 本文","document_type":"請求書","confidence":0.9,"warnings":[]}'
        )
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    result = await client.extract_with_vlm(b"image", "OCR")

    assert result["raw_text"] == "請求書 本文"
    assert "capital" not in str(result)


async def test_oci_generate_without_reasoning_is_unchanged() -> None:
    """思考の無い応答はこれまでどおり本文を返す(#1423)。"""
    response = _recorded_reasoning_response("日本の首都は東京です。")
    response["output"] = response["output"][1:]
    transport = FakeEnterpriseAiTransport(response)
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    assert await client.generate("質問", "根拠") == "日本の首都は東京です。"


async def test_oci_generate_drops_thinking_blocks_in_content_array() -> None:
    """content の配列の思考の block(thinking / reasoning_text)は本文に入れない(#1423)。"""
    transport = FakeEnterpriseAiTransport(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "Let me think.", "text": "Hmm."},
                            {"type": "reasoning_text", "text": "Reasoning in English."},
                            {"type": "output_text", "text": "1 文目です。"},
                            {"type": "text", "text": "2 文目です。"},
                        ],
                    }
                }
            ]
        }
    )
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    answer = await client.generate("質問", "根拠")

    assert answer == "1 文目です。\n2 文目です。"


@pytest.mark.parametrize(
    "message",
    [
        {"role": "assistant", "content": "東京です。", "reasoning_content": "Thinking..."},
        {"role": "assistant", "content": "<think>Thinking...</think>\n\n東京です。"},
        {"role": "assistant", "content": "<thinking>\nThinking...\n</thinking>東京です。"},
    ],
    ids=["reasoning-content-field", "think-tag", "thinking-tag"],
)
async def test_oci_generate_drops_chat_completions_reasoning(message: dict[str, Any]) -> None:
    """Chat Completions の `reasoning_content` と本文の先頭の思考のタグを除く(#1423)。"""
    transport = FakeEnterpriseAiTransport({"choices": [{"message": message}]})
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    assert await client.generate("質問", "根拠") == "東京です。"


async def test_oci_generate_does_not_return_reasoning_when_message_is_empty() -> None:
    """本文が空で思考だけの応答は、思考を回答にせず失敗にする(#1423)。"""
    transport = FakeEnterpriseAiTransport(_recorded_reasoning_response(""))
    client = EnterpriseAiClient(settings=_llm_settings(), http_transport=transport)

    with pytest.raises(ValueError, match="回答 text"):
        await client.generate("質問", "根拠")


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        ({"type": "response.reasoning_text.delta", "delta": "Thinking"}, ""),
        ({"type": "response.reasoning_summary_text.delta", "delta": "Summary"}, ""),
        ({"type": "response.output_text.delta", "delta": "東京"}, "東京"),
        ({"choices": [{"delta": {"reasoning_content": "Thinking"}}]}, ""),
        ({"choices": [{"delta": {"content": "です"}}]}, "です"),
    ],
    ids=[
        "reasoning-delta",
        "reasoning-summary-delta",
        "output-delta",
        "chat-reasoning",
        "chat-content",
    ],
)
def test_stream_delta_drops_reasoning_events(event: dict[str, Any], expected: str) -> None:
    """stream の思考の delta は本文の delta にしない(#1423)。"""
    from rag_parser_core.enterprise_ai import _extract_stream_text_delta

    assert _extract_stream_text_delta(event) == expected


async def test_model_call_snapshot_is_attached_to_product_trace() -> None:
    from pr_backend_core.observability.tracing import operation_trace

    client = EnterpriseAiClient(
        settings=_llm_settings(),
        http_transport=FakeEnterpriseAiTransport({"output_text": "synthetic-result"}),
    )
    with operation_trace("synthetic", operation_id="a") as trace:
        await client._post_enterprise_ai(
            "/responses",
            {
                "model": "enterprise-llm",
                "input": "synthetic-input",
                "text": {"format": {"type": "json_schema"}},
            },
        )
    call = next(s for s in trace.spans if s.operation_name == "gen_ai.chat")
    assert call.display and call.display.details and call.display.details.call
    captured = call.display.details.call
    assert "synthetic-input" in captured.input.text
    assert "synthetic-result" in captured.output.text
    assert captured.schema_.status == "recorded"
    assert captured.visibility == "system_admin"


@pytest.fixture(autouse=True)
def enable_trace_content_for_snapshot_tests() -> Any:
    from pr_backend_core.observability.call_policy import call_content_policy

    with call_content_policy(lambda: True):
        yield
