"""service 系 parser backend(oci_document_understanding)の契約テスト。

- registry: 明示選択時に core が sentinel(extraction=None)を返すこと
- ingestion routing: service backend は microservice 成功時のみ採用し、失敗時は縮退しない
- OCI Document Understanding client: 注入 fake SDK で非同期 job フローと remap を検証
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from rag_parser_core.registry import (
    SERVICE_ADAPTER_BACKENDS,
    ParserRegistryResult,
    parse_with_registry,
)

from app.clients.oci_document_understanding import (
    OciDocumentUnderstandingClient,
    document_understanding_result_to_payload,
)
from app.config import Settings
from app.rag.ingestion import IngestionPipeline, IngestionUserError
from app.schemas.document import SourceModality, SourceProfile
from app.schemas.extraction import StructuredExtraction


def _pdf_profile() -> SourceProfile:
    return SourceProfile(
        original_file_name="scan.pdf",
        sanitized_file_name="scan.pdf",
        content_type="application/pdf",
        file_size_bytes=16,
        content_sha256="0" * 64,
        modality=SourceModality.PDF,
        parser_profile="pdf",
    )


@pytest.mark.parametrize("backend", sorted(SERVICE_ADAPTER_BACKENDS))
def test_parse_with_registry_service_backend_returns_sentinel(backend: str) -> None:
    """service backend 明示時は core で実行せず sentinel を返し、ローカル抽出しない。"""
    result = parse_with_registry(
        b"%PDF-1.7 fake",
        source_profile=_pdf_profile(),
        content_type="application/pdf",
        adapter_backend=backend,
    )
    assert result.extraction is None
    assert result.parser_backend == backend
    assert result.fallback_used is False
    assert result.template == "enterprise_ai_fallback"


def _du_result_json() -> dict[str, Any]:
    return {
        "documentMetadata": {"pageCount": 1},
        "detectedDocumentTypes": [{"documentType": "INVOICE"}],
        "pages": [
            {
                "pageNumber": 1,
                "dimensions": {"width": 800, "height": 1000, "unit": "PIXEL"},
                "lines": [{"text": "請求書"}, {"text": "合計 1,200 円"}],
                "words": [{"text": "請求書", "confidence": 0.9}],
                "tables": [
                    {
                        "rowCount": 2,
                        "columnCount": 2,
                        "headerRows": [
                            {
                                "cells": [
                                    {"text": "品目", "rowIndex": 0, "columnIndex": 0},
                                    {"text": "金額", "rowIndex": 0, "columnIndex": 1},
                                ]
                            }
                        ],
                        "bodyRows": [
                            {
                                "cells": [
                                    {"text": "りんご", "rowIndex": 1, "columnIndex": 0},
                                    {"text": "1,200", "rowIndex": 1, "columnIndex": 1},
                                ]
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_document_understanding_remap_to_structured_extraction() -> None:
    """DU 結果 JSON が raw_text / pages / tables を持つ StructuredExtraction へ写る。"""
    payload = document_understanding_result_to_payload(_du_result_json())
    extraction = StructuredExtraction.model_validate(payload)
    assert "請求書" in extraction.raw_text
    assert "合計 1,200 円" in extraction.raw_text
    assert extraction.document_type == "INVOICE"
    assert extraction.confidence == pytest.approx(0.9)
    assert len(extraction.tables) == 1
    cells = {(cell.row, cell.col): cell.text for cell in extraction.tables[0].cells}
    assert cells[(0, 0)] == "品目"
    assert cells[(1, 1)] == "1,200"
    assert extraction.pages and extraction.pages[0].page_number == 1


# --- OCI Document Understanding client(注入 fake SDK)---
class _FakeResponse:
    def __init__(self, data: object) -> None:
        self.data = data


class _FakeJob:
    def __init__(self, job_id: str, state: str) -> None:
        self.id = job_id
        self.lifecycle_state = state
        self.lifecycle_details = ""


class _FakeDocumentClient:
    def __init__(self, states: list[str]) -> None:
        self._states = states
        self._poll = 0
        self.created: list[Any] = []

    def create_processor_job(self, create_processor_job_details: object) -> _FakeResponse:
        self.created.append(create_processor_job_details)
        return _FakeResponse(_FakeJob("job-1", self._states[0]))

    def get_processor_job(self, processor_job_id: str) -> _FakeResponse:
        state = self._states[min(self._poll, len(self._states) - 1)]
        self._poll += 1
        return _FakeResponse(_FakeJob(processor_job_id, state))


class _FakeObjectEntry:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeStorageClient:
    def __init__(self, output_objects: dict[str, bytes], *, fail_delete: bool = False) -> None:
        self._output_objects = output_objects
        self.put_calls: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, str]] = []
        self._fail_delete = fail_delete

    def delete_object(
        self, namespace_name: str, bucket_name: str, object_name: str, **kwargs: Any
    ) -> _FakeResponse:
        if self._fail_delete:
            raise RuntimeError("delete failed")
        self.deleted.append((bucket_name, object_name))
        self._output_objects.pop(object_name, None)
        return _FakeResponse(None)

    def put_object(
        self,
        namespace_name: str,
        bucket_name: str,
        object_name: str,
        put_object_body: bytes,
        **kwargs: Any,
    ) -> _FakeResponse:
        self.put_calls.append((namespace_name, bucket_name, object_name))
        return _FakeResponse(None)

    def list_objects(self, namespace_name: str, bucket_name: str, **kwargs: Any) -> _FakeResponse:
        prefix = str(kwargs.get("prefix", ""))
        names = [name for name in self._output_objects if name.startswith(prefix)]
        return _FakeResponse(type("L", (), {"objects": [_FakeObjectEntry(n) for n in names]})())

    def get_object(
        self, namespace_name: str, bucket_name: str, object_name: str, **kwargs: Any
    ) -> _FakeResponse:
        return _FakeResponse(type("D", (), {"content": self._output_objects[object_name]})())


def _du_settings() -> Settings:
    return Settings.model_construct(
        oci_compartment_id="ocid1.compartment.oc1..test",
        oci_region="us-chicago-1",
        oci_config_file="~/.oci/config",
        oci_config_profile="DEFAULT",
        object_storage_region="ap-osaka-1",
        oci_document_understanding_object_storage_region="",
        oci_document_understanding_namespace="ns",
        oci_document_understanding_input_bucket="in-bucket",
        oci_document_understanding_output_bucket="out-bucket",
        oci_document_understanding_input_prefix="document-understanding/input",
        oci_document_understanding_output_prefix="document-understanding/output",
        oci_document_understanding_language="JPN",
        oci_document_understanding_features=["DOCUMENT_TEXT_EXTRACTION", "TABLE_EXTRACTION"],
        oci_document_understanding_poll_interval_seconds=0.01,
        oci_document_understanding_timeout_seconds=5.0,
    )


async def test_du_client_analyze_runs_async_job_and_remaps() -> None:
    """put → create → poll(SUCCEEDED) → 結果取得 → remap の一連が動く。"""
    output_key = "document-understanding/output/job-1/ns_in-bucket_doc.json"
    document_client = _FakeDocumentClient(["ACCEPTED", "IN_PROGRESS", "SUCCEEDED"])
    storage_client = _FakeStorageClient({output_key: json.dumps(_du_result_json()).encode("utf-8")})
    client = OciDocumentUnderstandingClient(
        _du_settings(),
        document_client=document_client,
        object_storage_client=storage_client,
    )
    payload = await client.analyze(b"%PDF fake", content_type="application/pdf", document_id="doc")
    assert payload is not None
    extraction = StructuredExtraction.model_validate(payload)
    assert "請求書" in extraction.raw_text
    assert document_client.created  # job が作成された
    assert storage_client.put_calls  # 入力が put された
    processor_config = document_client.created[0].processor_config
    assert processor_config.language == "ja"
    assert [feature.feature_type for feature in processor_config.features] == [
        "TEXT_EXTRACTION",
        "TABLE_EXTRACTION",
    ]


def test_du_client_uses_region_override_when_building_document_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Document Understanding client は PLATFORM_OCI_REGION で endpoint 用 region を上書きする。"""
    captured: dict[str, object] = {}
    fake_config_module = object()

    class _FakeAiDocumentModule:
        class AIServiceDocumentClient:
            def __init__(self, config: dict[str, object]) -> None:
                captured["client_config"] = config

    def _fake_import_module(name: str) -> object:
        if name == "oci.config":
            return fake_config_module
        if name == "oci.ai_document":
            return _FakeAiDocumentModule
        raise AssertionError(name)

    def _fake_load_config(
        oci_config_module: object,
        config_file: str,
        profile: str,
        *,
        region: str | None = None,
    ) -> dict[str, object]:
        captured["oci_config_module"] = oci_config_module
        captured["config_file"] = config_file
        captured["profile"] = profile
        captured["region"] = region
        return {"region": region}

    # import_module の差し替えは最後に行う（pytest 9.1 以降の monkeypatch は文字列の target を
    # importlib.import_module で解決するため、先に差し替えると後続の setattr が偽物を通る）。
    monkeypatch.setattr(
        "rag_parser_core.oci_document_understanding.load_oci_config_without_prompt",
        _fake_load_config,
    )
    monkeypatch.setattr(
        "rag_parser_core.oci_document_understanding.importlib.import_module",
        _fake_import_module,
    )

    client = OciDocumentUnderstandingClient(_du_settings())
    client._documents()

    assert captured["oci_config_module"] is fake_config_module
    assert captured["config_file"] == "~/.oci/config"
    assert captured["profile"] == "DEFAULT"
    assert captured["region"] == "us-chicago-1"
    assert captured["client_config"] == {"region": "us-chicago-1"}


def test_du_client_uses_du_region_for_object_storage_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DU 入出力 Object Storage は明示 override が無ければ PLATFORM_OCI_REGION に揃える。"""
    captured: dict[str, object] = {}
    fake_config_module = object()

    class _FakeObjectStorageModule:
        class ObjectStorageClient:
            def __init__(self, config: dict[str, object]) -> None:
                captured["client_config"] = config

    def _fake_import_module(name: str) -> object:
        if name == "oci.config":
            return fake_config_module
        if name == "oci.object_storage":
            return _FakeObjectStorageModule
        raise AssertionError(name)

    def _fake_load_config(
        oci_config_module: object,
        config_file: str,
        profile: str,
        *,
        region: str | None = None,
    ) -> dict[str, object]:
        captured["oci_config_module"] = oci_config_module
        captured["config_file"] = config_file
        captured["profile"] = profile
        captured["region"] = region
        return {"region": region}

    # import_module の差し替えは最後に行う（pytest 9.1 以降の monkeypatch は文字列の target を
    # importlib.import_module で解決するため、先に差し替えると後続の setattr が偽物を通る）。
    monkeypatch.setattr(
        "rag_parser_core.oci_document_understanding.load_oci_config_without_prompt",
        _fake_load_config,
    )
    monkeypatch.setattr(
        "rag_parser_core.oci_document_understanding.importlib.import_module",
        _fake_import_module,
    )

    client = OciDocumentUnderstandingClient(_du_settings())
    client._storage()

    assert captured["oci_config_module"] is fake_config_module
    assert captured["config_file"] == "~/.oci/config"
    assert captured["profile"] == "DEFAULT"
    assert captured["region"] == "us-chicago-1"
    assert captured["client_config"] == {"region": "us-chicago-1"}


async def test_du_client_analyze_deletes_job_input_and_output_objects() -> None:
    """解析の後に、job の入力と出力（folder の object も）を bucket から消す（#2440）。"""
    folder_key = "document-understanding/output/job-1/"
    output_key = "document-understanding/output/job-1/ns_in-bucket/results/doc.pdf.json"
    other_job_key = "document-understanding/output/job-10/ns_in-bucket/results/other.pdf.json"
    storage_client = _FakeStorageClient(
        {
            folder_key: b"",
            output_key: json.dumps(_du_result_json()).encode("utf-8"),
            other_job_key: b"{}",
        }
    )
    client = OciDocumentUnderstandingClient(
        _du_settings(),
        document_client=_FakeDocumentClient(["ACCEPTED", "SUCCEEDED"]),
        object_storage_client=storage_client,
    )
    payload = await client.analyze(b"%PDF fake", content_type="application/pdf", document_id="doc")
    assert payload is not None
    assert storage_client.deleted == [
        ("in-bucket", "document-understanding/input/doc.pdf"),
        ("out-bucket", folder_key),
        ("out-bucket", output_key),
    ]
    # 別の job（id の前方が同じ）の出力は消さない。
    assert other_job_key in storage_client._output_objects


async def test_du_client_analyze_deletes_input_when_job_fails() -> None:
    """job が失敗しても、入力の object を消す。"""
    storage_client = _FakeStorageClient({})
    client = OciDocumentUnderstandingClient(
        _du_settings(),
        document_client=_FakeDocumentClient(["ACCEPTED", "FAILED"]),
        object_storage_client=storage_client,
    )
    payload = await client.analyze(b"x", content_type="application/pdf", document_id="doc")
    assert payload is None
    assert storage_client.deleted == [("in-bucket", "document-understanding/input/doc.pdf")]


async def test_du_client_analyze_keeps_result_when_cleanup_fails() -> None:
    """入出力を消せなくても、解析の結果は返す。"""
    output_key = "document-understanding/output/job-1/ns_in-bucket_doc.json"
    storage_client = _FakeStorageClient(
        {output_key: json.dumps(_du_result_json()).encode("utf-8")}, fail_delete=True
    )
    client = OciDocumentUnderstandingClient(
        _du_settings(),
        document_client=_FakeDocumentClient(["SUCCEEDED"]),
        object_storage_client=storage_client,
    )
    payload = await client.analyze(b"%PDF fake", content_type="application/pdf", document_id="doc")
    assert payload is not None


async def test_du_client_analyze_returns_none_when_unconfigured() -> None:
    """compartment/namespace/bucket 未設定なら None を返し安全に縮退する。"""
    client = OciDocumentUnderstandingClient(
        Settings.model_construct(
            oci_compartment_id="",
            oci_document_understanding_namespace="",
            oci_document_understanding_input_bucket="",
            object_storage_namespace="",
            object_storage_bucket="",
        )
    )
    assert client.is_configured() is False
    payload = await client.analyze(b"x", content_type="application/pdf", document_id="doc")
    assert payload is None


async def test_du_client_analyze_returns_none_on_job_failure() -> None:
    """job が FAILED のときは None を返す。"""
    document_client = _FakeDocumentClient(["ACCEPTED", "FAILED"])
    storage_client = _FakeStorageClient({})
    client = OciDocumentUnderstandingClient(
        _du_settings(),
        document_client=document_client,
        object_storage_client=storage_client,
    )
    payload = await client.analyze(b"x", content_type="application/pdf", document_id="doc")
    assert payload is None


# --- ingestion routing ---
def _routing_pipeline() -> IngestionPipeline:
    return IngestionPipeline(
        settings=Settings.model_construct(
            rag_extraction_artifact_cache_enabled=True,
            rag_extraction_artifact_prefix="artifacts/extractions",
        )
    )


def _service_unreachable_result(backend: str) -> ParserRegistryResult:
    """parser microservice 未到達時の fallback(extraction=None)を表す。"""
    return ParserRegistryResult(
        extraction=None,
        parser_backend=backend,
        parser_version="service_unavailable",
        fallback_used=True,
        template=f"{backend}_fallback",
        warnings=(f"{backend}_adapter_service_unreachable",),
    )


def _service_success_result(backend: str, extraction: StructuredExtraction) -> ParserRegistryResult:
    """parser microservice が抽出を返した場合の結果。"""
    return ParserRegistryResult(
        extraction=extraction,
        parser_backend=backend,
        parser_version=backend,
        template=backend,
    )


async def test_service_backend_du_stops_when_microservice_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """microservice 未到達時、DU 明示は別経路へ縮退せず取込を止める。"""
    pipeline = _routing_pipeline()
    monkeypatch.setattr(
        pipeline._parser_service,
        "run_service_backend",
        lambda *a, **k: _service_unreachable_result("oci_document_understanding"),
    )

    with pytest.raises(IngestionUserError, match="OCI Document Understanding"):
        await pipeline._extract_with_service_backend(
            "oci_document_understanding",
            trace_id="t",
            document_id="doc",
            source_bytes=b"x",
            content_type="application/pdf",
            parser_profile="enterprise_ai_pdf_layout",
            checkpoint_segments=(),
            cancel_checker=None,
        )


async def test_service_backend_du_does_not_use_in_process_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """microservice 未到達なら DU は別経路へ縮退せず取込を止める(in-process fallback 無し)。"""
    pipeline = _routing_pipeline()
    monkeypatch.setattr(
        pipeline._parser_service,
        "run_service_backend",
        lambda *a, **k: _service_unreachable_result("oci_document_understanding"),
    )

    with pytest.raises(IngestionUserError, match="OCI Document Understanding"):
        await pipeline._extract_with_service_backend(
            "oci_document_understanding",
            trace_id="t",
            document_id="doc",
            source_bytes=b"x",
            content_type="application/pdf",
            parser_profile="enterprise_ai_pdf_layout",
            checkpoint_segments=(),
            cancel_checker=None,
        )


async def test_service_backend_du_uses_microservice_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """microservice が抽出を返したらそれを使う(Document Understanding service backend)。"""
    pipeline = _routing_pipeline()
    extraction = StructuredExtraction.model_validate(
        document_understanding_result_to_payload(_du_result_json())
    )
    monkeypatch.setattr(
        pipeline._parser_service,
        "run_service_backend",
        lambda *a, **k: _service_success_result("oci_document_understanding", extraction),
    )

    result = await pipeline._extract_with_service_backend(
        "oci_document_understanding",
        trace_id="t",
        document_id="doc",
        source_bytes=b"x",
        content_type="application/pdf",
        parser_profile="enterprise_ai_pdf_layout",
        checkpoint_segments=(),
        cancel_checker=None,
    )
    assert isinstance(result, StructuredExtraction)
    assert "請求書" in result.raw_text
