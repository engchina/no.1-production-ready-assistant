"""staging smoke CLI の境界テスト。"""

from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pytest import MonkeyPatch

from app.config import Settings
from app.rag import staging_smoke
from app.schemas.document import FileStatus
from app.schemas.search import RetrievedChunk, SearchDiagnostics, SearchResponse
from tests.support import shared_thin_wallet_dir


async def test_staging_smoke_uses_unique_marker_query_and_document_filter(
    monkeypatch: MonkeyPatch,
) -> None:
    """smoke 検索は新規 document を一意 marker と document filter で確認する。"""
    FakeObjectStorageClient.put_body = b""
    FakeObjectStorageClient.deleted_objects = []
    FakeOracleClient.deleted_documents = []
    FakeRagPipeline.last_request = None
    FakeRagPipeline.answer_template = None
    closed = False

    def close_pool() -> None:
        nonlocal closed
        closed = True

    monkeypatch.setattr(staging_smoke, "ObjectStorageClient", FakeObjectStorageClient)
    monkeypatch.setattr(staging_smoke, "OracleClient", FakeOracleClient)
    monkeypatch.setattr(staging_smoke, "IngestionPipeline", FakeIngestionPipeline)
    monkeypatch.setattr(staging_smoke, "RagPipeline", FakeRagPipeline)
    monkeypatch.setattr(staging_smoke, "close_oracle_pool", close_pool)

    result = await staging_smoke.run_staging_smoke(settings=_complete_oci_settings())

    assert result.ok is True
    assert result.document_id == "doc-smoke"
    assert result.marker.startswith("SMOKE-")
    assert result.marker in result.query
    assert result.marker.encode() in FakeObjectStorageClient.put_body
    assert result.answer_contains_marker is True
    assert result.cleanup == {"document": "skipped", "object": "skipped"}
    assert closed is True
    assert FakeObjectStorageClient.deleted_objects == []
    assert FakeOracleClient.deleted_documents == []

    assert FakeRagPipeline.last_request is not None
    assert FakeRagPipeline.last_request.query == result.query
    assert FakeRagPipeline.last_request.filters == {"document_id": "doc-smoke"}


async def test_staging_smoke_cleanup_deletes_created_resources(
    monkeypatch: MonkeyPatch,
) -> None:
    """cleanup 指定時は成功後に Oracle document/chunk と Object Storage object を消す。"""
    FakeObjectStorageClient.put_body = b""
    FakeObjectStorageClient.deleted_objects = []
    FakeOracleClient.deleted_documents = []
    FakeRagPipeline.answer_template = None

    monkeypatch.setattr(staging_smoke, "ObjectStorageClient", FakeObjectStorageClient)
    monkeypatch.setattr(staging_smoke, "OracleClient", FakeOracleClient)
    monkeypatch.setattr(staging_smoke, "IngestionPipeline", FakeIngestionPipeline)
    monkeypatch.setattr(staging_smoke, "RagPipeline", FakeRagPipeline)
    monkeypatch.setattr(staging_smoke, "close_oracle_pool", lambda: None)

    result = await staging_smoke.run_staging_smoke(
        settings=_complete_oci_settings(),
        cleanup=True,
    )

    assert result.cleanup == {"document": "deleted", "object": "deleted"}
    assert FakeOracleClient.deleted_documents == ["doc-smoke"]
    assert len(FakeObjectStorageClient.deleted_objects) == 1
    assert FakeObjectStorageClient.deleted_objects[0].startswith(
        "oci://namespace/bucket/staging-smoke/"
    )


async def test_staging_smoke_requires_answer_marker_for_default_query(
    monkeypatch: MonkeyPatch,
) -> None:
    """既定 query では Enterprise AI LLM が marker を回答へ戻すことも確認する。"""
    FakeObjectStorageClient.put_body = b""
    FakeObjectStorageClient.deleted_objects = []
    FakeOracleClient.deleted_documents = []
    FakeRagPipeline.answer_template = "smoke ok without marker"

    monkeypatch.setattr(staging_smoke, "ObjectStorageClient", FakeObjectStorageClient)
    monkeypatch.setattr(staging_smoke, "OracleClient", FakeOracleClient)
    monkeypatch.setattr(staging_smoke, "IngestionPipeline", FakeIngestionPipeline)
    monkeypatch.setattr(staging_smoke, "RagPipeline", FakeRagPipeline)
    monkeypatch.setattr(staging_smoke, "close_oracle_pool", lambda: None)

    try:
        await staging_smoke.run_staging_smoke(settings=_complete_oci_settings())
    except staging_smoke.StagingSmokeError as exc:
        assert exc.stage == "rag_answer_marker"
        assert exc.cause_type == "RuntimeError"
    else:
        raise AssertionError("marker を含まない回答は staging smoke 失敗にする")
    finally:
        FakeRagPipeline.answer_template = None


async def test_staging_smoke_cleanup_runs_after_marker_failure(
    monkeypatch: MonkeyPatch,
) -> None:
    """本実行で失敗しても cleanup 指定時は作成済み resource を best-effort で消す。"""
    FakeObjectStorageClient.put_body = b""
    FakeObjectStorageClient.deleted_objects = []
    FakeOracleClient.deleted_documents = []
    FakeRagPipeline.answer_template = "marker を返さない回答"

    monkeypatch.setattr(staging_smoke, "ObjectStorageClient", FakeObjectStorageClient)
    monkeypatch.setattr(staging_smoke, "OracleClient", FakeOracleClient)
    monkeypatch.setattr(staging_smoke, "IngestionPipeline", FakeIngestionPipeline)
    monkeypatch.setattr(staging_smoke, "RagPipeline", FakeRagPipeline)
    monkeypatch.setattr(staging_smoke, "close_oracle_pool", lambda: None)

    try:
        await staging_smoke.run_staging_smoke(
            settings=_complete_oci_settings(),
            cleanup=True,
        )
    except staging_smoke.StagingSmokeError as exc:
        assert exc.stage == "rag_answer_marker"
        assert exc.cleanup == {"document": "deleted", "object": "deleted"}
        assert staging_smoke._error_payload(exc)["cleanup"] == exc.cleanup
    else:
        raise AssertionError("marker を含まない回答は staging smoke 失敗にする")
    finally:
        FakeRagPipeline.answer_template = None

    assert FakeOracleClient.deleted_documents == ["doc-smoke"]
    assert len(FakeObjectStorageClient.deleted_objects) == 1


def test_format_smoke_query_keeps_custom_query_without_marker() -> None:
    """任意 query に marker placeholder がない場合はそのまま使う。"""
    assert staging_smoke._format_smoke_query("固定クエリ", "SMOKE-1") == "固定クエリ"


def test_staging_smoke_preflight_reports_missing_oci_config_without_secrets() -> None:
    """preflight は未設定グループだけを返し、secret 値は出さない。"""
    result = staging_smoke.staging_smoke_preflight(
        settings=Settings(
            oci_compartment_id="",
            oci_enterprise_ai_endpoint="",
            oci_enterprise_ai_project_ocid="",
            oci_enterprise_ai_api_key="",
            oci_enterprise_ai_models=[],
            oci_enterprise_ai_default_text_model="",
            oracle_user="",
            oracle_dsn="",
            object_storage_namespace="",
            object_storage_bucket="",
            upload_storage_backend="oci",
            oracle_password="super-secret-password",
        )
    )

    assert result.ok is False
    assert result.checks == {
        "oci_common": "missing",
        "enterprise_ai": "missing",
        "genai": "ok",
        "oracle": "missing",
        "object_storage": "missing",
    }
    assert "super-secret-password" not in str(asdict(result))


def test_staging_smoke_preflight_requires_oci_object_storage_for_real_smoke(
    tmp_path: Path,
) -> None:
    """実 staging smoke は AI/Oracle だけでなく OCI Object Storage も必須にする。"""
    result = staging_smoke.staging_smoke_preflight(
        settings=_complete_oci_settings(
            upload_storage_backend="local",
            local_storage_dir=str(tmp_path / "storage"),
        )
    )

    assert result.ok is False
    assert result.checks["local_storage"] == "ok"
    assert result.checks["smoke_object_storage_backend"] == "invalid"


async def test_staging_smoke_stops_before_external_clients_when_preflight_fails(
    monkeypatch: MonkeyPatch,
) -> None:
    """preflight 失敗時は Object Storage / Oracle client を初期化しない。"""

    def fail_object_storage(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("external client must not be constructed")

    monkeypatch.setattr(staging_smoke, "ObjectStorageClient", fail_object_storage)

    try:
        await staging_smoke.run_staging_smoke(settings=Settings())
    except staging_smoke.StagingSmokePreflightError as exc:
        assert exc.preflight.ok is False
    else:
        raise AssertionError("設定不足は実 staging smoke 前に失敗する")


def test_error_payload_includes_failed_stage_without_raw_message() -> None:
    """smoke CLI の失敗 payload は stage と cause type だけを返す。"""
    error = staging_smoke.StagingSmokeError(
        "rag_search",
        RuntimeError("raw secret detail: password=secret"),
    )

    payload = staging_smoke._error_payload(error)

    assert payload == {
        "ok": False,
        "error_type": "StagingSmokeError",
        "stage": "rag_search",
        "cause_type": "RuntimeError",
    }
    assert "password" not in str(payload)


def test_error_payload_includes_safe_external_error_details() -> None:
    """OCI SDK の status/code/request id は raw message なしで診断に使える。"""

    class ExternalServiceError(RuntimeError):
        status = 404
        code = "BucketNotFound"
        request_id = "request-123"

    error = staging_smoke.StagingSmokeError(
        "object_storage_put",
        ExternalServiceError("raw secret detail: bucket=my-private-bucket"),
    )

    payload = staging_smoke._error_payload(error)

    assert payload == {
        "ok": False,
        "error_type": "StagingSmokeError",
        "stage": "object_storage_put",
        "cause_type": "ExternalServiceError",
        "cause_details": {
            "status": "404",
            "code": "BucketNotFound",
            "request_id": "request-123",
        },
    }
    assert "my-private-bucket" not in str(payload)


def test_error_payload_includes_preflight_checks_without_raw_values() -> None:
    """preflight 失敗 payload は安全な check status だけを含める。"""
    preflight = staging_smoke.SmokePreflightResult(
        ok=False,
        checks={"oracle": "missing_credentials"},
        message="staging smoke preflight failed; fix checks before running external smoke",
    )
    error = staging_smoke.StagingSmokePreflightError(preflight)

    payload = staging_smoke._error_payload(error)

    assert payload == {
        "ok": False,
        "error_type": "StagingSmokePreflightError",
        "checks": {"oracle": "missing_credentials"},
        "message": "staging smoke preflight failed; fix checks before running external smoke",
    }


def _complete_oci_settings(**overrides: Any) -> Settings:
    """staging smoke preflight を通す OCI 設定を作る。"""
    values: dict[str, Any] = {
        "oci_region": "ap-osaka-1",
        "oci_compartment_id": "ocid1.compartment.oc1..example",
        "oci_enterprise_ai_endpoint": "https://enterprise-ai.example.com",
        "oci_enterprise_ai_project_ocid": "ocid1.generativeaiproject.oc1..example",
        "oci_enterprise_ai_api_key": "sk-test-secret",
        "oci_enterprise_ai_default_text_model": "llm-deployment",
        "oci_enterprise_ai_default_vision_model": "vlm-deployment",
        "oracle_user": "rag_user",
        "oracle_password": "oracle-password",
        "oracle_dsn": "adb.example.com/rag",
        # Wallet mTLS の Wallet がそろっていないと Oracle の readiness は ok にならない（#325）。
        "oracle_wallet_dir": shared_thin_wallet_dir(),
        "object_storage_region": "ap-osaka-1",
        "object_storage_namespace": "namespace",
        "object_storage_bucket": "bucket",
        "upload_storage_backend": "oci",
    }
    values.update(overrides)
    return Settings(**values)


class FakeObjectStorageClient:
    """Object Storage fake。"""

    put_body = b""
    deleted_objects: list[str] = []

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def put(self, key: str, body: bytes, content_type: str) -> str:
        assert key.startswith("staging-smoke/")
        assert content_type == "text/plain"
        self.__class__.put_body = body
        return f"oci://namespace/bucket/{key}"

    async def get(self, object_uri: str) -> bytes:
        assert object_uri.startswith("oci://namespace/bucket/staging-smoke/")
        return self.__class__.put_body

    async def delete(self, object_uri: str) -> bool:
        assert object_uri.startswith("oci://namespace/bucket/staging-smoke/")
        self.__class__.deleted_objects.append(object_uri)
        return True


class FakeOracleClient:
    """Oracle fake。"""

    deleted_documents: list[str] = []

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def create_document(self, **kwargs: Any) -> SimpleNamespace:
        assert kwargs["file_name"].startswith("staging-smoke-")
        assert kwargs["content_sha256"]
        return SimpleNamespace(id="doc-smoke")

    async def count_document_chunks(self, document_id: str) -> int:
        assert document_id == "doc-smoke"
        return 1

    async def delete_document(self, document_id: str) -> bool:
        assert document_id == "doc-smoke"
        self.__class__.deleted_documents.append(document_id)
        return True


class FakeIngestionPipeline:
    """Ingestion fake。"""

    def __init__(self, oracle: FakeOracleClient, settings: Settings) -> None:
        self.oracle = oracle
        self.settings = settings

    async def ingest(
        self,
        document_id: str,
        image_bytes: bytes,
        prompt: str,
        *,
        content_type: str = "application/octet-stream",
        source_profile: object | None = None,
        chunk_set_id: str | None = None,
    ) -> SimpleNamespace:
        _ = source_profile, chunk_set_id
        assert document_id == "doc-smoke"
        assert image_bytes == FakeObjectStorageClient.put_body
        assert "OCR" in prompt
        assert content_type == "text/plain"
        return SimpleNamespace(status=FileStatus.INDEXED)


class FakeRagPipeline:
    """RAG pipeline fake。"""

    last_request = None
    answer_template: str | None = None

    def __init__(self, oracle: FakeOracleClient, settings: Settings) -> None:
        self.oracle = oracle
        self.settings = settings

    async def run(self, request: Any) -> SearchResponse:
        self.__class__.last_request = request
        answer = self.answer_template or f"{request.query} を確認しました。"
        return SearchResponse(
            answer=answer,
            citations=[
                RetrievedChunk(
                    document_id="doc-smoke",
                    chunk_id="doc-smoke:0",
                    text="smoke",
                    score=1.0,
                )
            ],
            trace_id="trace-smoke",
            elapsed_ms=12.0,
            diagnostics=SearchDiagnostics(retrieved_count=1, citation_count=1),
        )
