"""前処理(Preprocess)アダプター + 固定長 chunking + 派生系譜のテスト。"""

import pytest
from rag_parser_core.preprocess import (
    ConvertOutcome,
    ConvertResponse,
    SourceDerivation,
    normalize_preprocess_profile,
)

from app.clients.preprocess_service import (
    PreprocessServiceClient,
    PreprocessServiceError,
)
from app.config import Settings
from app.rag.chunking import CHUNKING_STRATEGIES, chunk_extraction_with_strategy
from app.rag.chunking_strategy import (
    CHUNKING_STRATEGY_ORDER,
    CHUNKING_STRATEGY_SPECS,
)
from app.rag.kb_adapter_config import (
    KnowledgeBaseAdapterConfig,
    resolve_effective_settings,
)
from app.rag.preprocess_strategy import (
    PREPROCESS_PROFILE_ORDER,
    preprocess_runtime_settings,
    resolve_preprocess_profile,
)
from app.schemas.document import SourceModality, SourceProfile
from app.schemas.extraction import StructuredExtraction


def _text_profile(content_type: str = "text/plain") -> SourceProfile:
    return SourceProfile(
        original_file_name="note.txt",
        sanitized_file_name="note.txt",
        content_type=content_type,
        file_size_bytes=16,
        content_sha256="abc",
        modality=SourceModality.TEXT,
        parser_profile="text",
    )


# --- 契約(rag_parser_core)---


def test_convert_response_base64_roundtrip() -> None:
    outcome = ConvertOutcome(
        converted=True,
        converter_name="libreoffice",
        converter_version="v1",
        derived_bytes=b"%PDF-1.7 body",
        derived_content_type="application/pdf",
        page_map={"1": 1},
    )
    response = ConvertResponse.from_outcome(outcome)
    assert response.converted is True
    assert response.derived_bytes() == b"%PDF-1.7 body"
    assert response.page_map == {"1": 1}


def test_convert_response_passthrough_has_no_payload() -> None:
    response = ConvertResponse.from_outcome(ConvertOutcome.passthrough(reason="x"))
    assert response.converted is False
    assert response.derived_bytes() is None


def test_normalize_preprocess_profile_falls_back() -> None:
    assert normalize_preprocess_profile("OFFICE_TO_PDF") == "office_to_pdf"
    assert normalize_preprocess_profile("nope") == "passthrough"


def test_source_derivation_defaults() -> None:
    derivation = SourceDerivation(derivation_id="d1", preprocess_profile="text_normalize")
    assert derivation.converted is False
    assert derivation.created_at  # ISO timestamp


# --- readiness snapshot ---


def test_preprocess_runtime_snapshot_defaults_to_passthrough() -> None:
    snapshot = preprocess_runtime_settings(
        Settings(rag_preprocess_enabled=False, rag_preprocess_profile="passthrough")
    )
    assert snapshot.profile == "passthrough"
    assert snapshot.service_enabled is False
    names = tuple(status.name for status in snapshot.profiles)
    assert names == PREPROCESS_PROFILE_ORDER
    selected = [status for status in snapshot.profiles if status.selected]
    assert [status.name for status in selected] == ["passthrough"]


def test_service_profiles_unavailable_without_service() -> None:
    snapshot = preprocess_runtime_settings(Settings(rag_preprocess_enabled=False))
    by_name = {status.name: status for status in snapshot.profiles}
    assert by_name["office_to_pdf"].available is False


def test_resolve_preprocess_profile_normalizes() -> None:
    # 廃止済み text_normalize は passthrough(no-op)へ正規化される。
    assert resolve_preprocess_profile(Settings(rag_preprocess_profile="text_normalize")) == (
        "passthrough"
    )


@pytest.mark.parametrize("removed", ["url_to_markdown", "image_enhance", " Image_Enhance "])
def test_removed_preprocess_profiles_fall_back_to_passthrough(
    removed: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """削除した変換方式(#2042)が保存済みの設定・レシピに残っていても「変換なし」で読める。"""
    with caplog.at_level("WARNING"):
        assert Settings(rag_preprocess_profile=removed).rag_preprocess_profile == "passthrough"
    assert "removed_preprocess_profile_normalized" in caplog.text
    monkeypatch.setenv("RAG_PREPROCESS_PROFILE", removed)
    assert Settings(_env_file=None).rag_preprocess_profile == "passthrough"
    config = KnowledgeBaseAdapterConfig.model_validate(
        {"ingestion": {"preprocess_profile": removed, "chunk_size": 900}}
    )
    assert config.ingestion.preprocess_profile == "passthrough"
    assert config.ingestion.chunk_size == 900
    names = [status.name for status in preprocess_runtime_settings(Settings()).profiles]
    assert removed.strip().casefold() not in names


# --- 前処理プロファイル委譲(text_normalize は廃止し passthrough へ寄せる)---


def test_client_text_normalize_legacy_maps_to_passthrough() -> None:
    # 廃止済み text_normalize は in-process 実装を持たず passthrough(no-op)になる。
    client = PreprocessServiceClient(Settings(rag_preprocess_profile="text_normalize"))
    outcome = client.convert(
        "ＡＢＣ".encode(),
        content_type="text/plain",
        source_profile=_text_profile(),
        profile="text_normalize",
    )
    assert outcome.converted is False


def test_client_passthrough_for_binary_text_normalize() -> None:
    client = PreprocessServiceClient(Settings())
    pdf_profile = SourceProfile(
        original_file_name="a.pdf",
        sanitized_file_name="a.pdf",
        content_type="application/pdf",
        file_size_bytes=4,
        content_sha256="x",
        modality=SourceModality.PDF,
        parser_profile="pdf",
    )
    outcome = client.convert(
        b"%PDF",
        content_type="application/pdf",
        source_profile=pdf_profile,
        profile="text_normalize",
    )
    assert outcome.converted is False


def test_client_office_profile_raises_without_service() -> None:
    client = PreprocessServiceClient(Settings(rag_preprocess_enabled=False))
    with pytest.raises(PreprocessServiceError, match="前処理サービスが無効"):
        client.convert(
            b"PK\x03\x04",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            source_profile=None,
            profile="office_to_pdf",
        )


def test_client_service_unreachable_raises() -> None:
    # 到達不能 URL を指定 → HTTP 失敗 → 取込停止用エラー。
    client = PreprocessServiceClient(
        Settings(
            rag_preprocess_enabled=True,
            rag_preprocess_office_to_pdf_service_url="http://127.0.0.1:9/",
            rag_preprocess_service_timeout_seconds=0.2,
            rag_http_service_retry_attempts=1,
        )
    )
    with pytest.raises(PreprocessServiceError, match="office to pdf"):
        client.convert(
            b"PK\x03\x04",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            source_profile=None,
            profile="office_to_pdf",
        )


def _word_client_with_response(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> PreprocessServiceClient:
    """前処理サービスの応答を差し替えた Word→JSONL のクライアント(HTTP は呼ばない)。"""
    import httpx

    from app.clients import preprocess_service

    def fake_request(_client: object, method: str, url: str, **_kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=payload, request=httpx.Request(method, url))

    monkeypatch.setattr(preprocess_service, "request_with_retry", fake_request)
    return PreprocessServiceClient(
        Settings(
            rag_preprocess_enabled=True,
            rag_preprocess_word_to_jsonl_service_url="http://127.0.0.1:9/",
        )
    )


_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("word_encrypted", "パスワードで保護"),
        ("word_input_too_large", "64 MB"),
        ("word_no_content", "空の文書"),
    ],
    ids=["encrypted", "too-large", "empty"],
)
def test_client_source_reason_is_user_actionable(
    monkeypatch: pytest.MonkeyPatch, reason: str, expected: str
) -> None:
    """原本の側の理由は、サービスのログの確認ではなく対処を案内する(#2068)。"""
    client = _word_client_with_response(
        monkeypatch,
        ConvertResponse.from_outcome(ConvertOutcome.passthrough(reason=reason)).model_dump(),
    )
    with pytest.raises(PreprocessServiceError) as caught:
        client.convert(
            b"PK\x03\x04", content_type=_DOCX, source_profile=None, profile="word_to_jsonl"
        )
    assert caught.value.reason == reason
    assert expected in str(caught.value)
    assert "ログを確認" not in str(caught.value)


def test_client_failure_keeps_service_message(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _word_client_with_response(
        monkeypatch,
        ConvertResponse.from_outcome(
            ConvertOutcome.passthrough(reason="word_to_jsonl_failed")
        ).model_dump(),
    )
    with pytest.raises(PreprocessServiceError) as caught:
        client.convert(
            b"PK\x03\x04", content_type=_DOCX, source_profile=None, profile="word_to_jsonl"
        )
    assert caught.value.reason == "no_conversion"
    assert "preprocess-word-to-jsonl のログを確認" in str(caught.value)


def test_client_converted_outcome_keeps_warnings(monkeypatch: pytest.MonkeyPatch) -> None:
    """変換した派生物と warning をそのまま返し、取込を続ける。"""
    outcome = ConvertOutcome(
        converted=True,
        converter_name="word_to_jsonl",
        converter_version="v1",
        derived_bytes=b"{}\n",
        derived_content_type="application/x-ndjson",
        warnings=("word_sample_warning",),
    )
    client = _word_client_with_response(
        monkeypatch, ConvertResponse.from_outcome(outcome).model_dump()
    )
    result = client.convert(
        b"PK\x03\x04", content_type=_DOCX, source_profile=None, profile="word_to_jsonl"
    )
    assert result.converted is True
    assert result.derived_bytes == b"{}\n"
    assert result.warnings == ("word_sample_warning",)


def test_csv_to_jsonl_profile_routes_to_dedicated_service() -> None:
    # csv_to_jsonl は専用サービス URL へルーティングされ、未達時は取込停止用エラー。
    client = PreprocessServiceClient(
        Settings(
            rag_preprocess_enabled=True,
            rag_preprocess_csv_to_jsonl_service_url="http://127.0.0.1:9/",
            rag_preprocess_service_timeout_seconds=0.2,
            rag_http_service_retry_attempts=1,
        )
    )
    with pytest.raises(PreprocessServiceError, match="csv to json"):
        client.convert(
            b"a,b\n1,2\n",
            content_type="text/csv",
            source_profile=None,
            profile="csv_to_jsonl",
        )


def test_csv_to_jsonl_profile_unconfigured_raises() -> None:
    # サービス URL が空 → 未設定として取込停止用エラー。
    client = PreprocessServiceClient(
        Settings(rag_preprocess_enabled=True, rag_preprocess_csv_to_jsonl_service_url="")
    )
    with pytest.raises(PreprocessServiceError, match="接続先 URL が未設定"):
        client.convert(
            b"a,b\n1,2\n",
            content_type="text/csv",
            source_profile=None,
            profile="csv_to_jsonl",
        )


def test_excel_to_jsonl_profile_routes_to_dedicated_service() -> None:
    # excel_to_jsonl は専用サービス URL へルーティングされ、未達時は取込停止用エラー。
    client = PreprocessServiceClient(
        Settings(
            rag_preprocess_enabled=True,
            rag_preprocess_excel_to_jsonl_service_url="http://127.0.0.1:9/",
            rag_preprocess_service_timeout_seconds=0.2,
            rag_http_service_retry_attempts=1,
        )
    )
    with pytest.raises(PreprocessServiceError, match="excel to json"):
        client.convert(
            b"PK\x03\x04",
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            source_profile=None,
            profile="excel_to_jsonl",
        )


# --- 固定長 chunking + KB 上書き ---


def test_fixed_size_strategy_registered() -> None:
    assert "fixed_size" in CHUNKING_STRATEGIES
    assert "fixed_size" in CHUNKING_STRATEGY_ORDER
    assert CHUNKING_STRATEGY_SPECS["fixed_size"].origin == "ragflow_general_fixed"
    assert "fixed_delimiter" in CHUNKING_STRATEGIES
    assert "fixed_delimiter" in CHUNKING_STRATEGY_ORDER
    assert CHUNKING_STRATEGY_SPECS["fixed_delimiter"].origin == "fixed_delimiter_split"


def test_fixed_size_chunking_is_deterministic_fixed_length() -> None:
    extraction = StructuredExtraction(
        raw_text="あいうえお。" * 200,
        document_type="ドキュメント",
        summary="x",
        elements=[],
    )
    chunks = chunk_extraction_with_strategy(
        extraction, strategy="fixed_size", chunk_size=300, overlap=50
    )
    assert len(chunks) > 1
    # 末尾以外は chunk_size ちょうどの固定長。
    assert all(len(chunk.text) == 300 for chunk in chunks[:-1])
    assert chunks[0].metadata.get("chunk_strategy") == "fixed_size"
    assert chunks[0].metadata.get("chunk_fixed_size") is True


def test_fixed_delimiter_chunking_splits_without_separator() -> None:
    extraction = StructuredExtraction(
        raw_text="第1章\n本文1\n---SPLIT---\n\n第2章\n本文2\n---SPLIT---\n第3章",
        document_type="ドキュメント",
        summary="x",
        elements=[],
    )
    chunks = chunk_extraction_with_strategy(
        extraction,
        strategy="fixed_delimiter",
        delimiter="---SPLIT---",
    )
    assert [chunk.text for chunk in chunks] == ["第1章\n本文1", "第2章\n本文2", "第3章"]
    assert all("---SPLIT---" not in chunk.text for chunk in chunks)
    assert chunks[0].metadata.get("chunk_strategy") == "fixed_delimiter"
    assert chunks[0].metadata.get("chunk_fixed_delimiter") is True
    assert "chunk_size_compliance" not in chunks[0].metadata


def test_fixed_delimiter_decodes_escape_sequences() -> None:
    extraction = StructuredExtraction(raw_text="alpha\n\nbeta", elements=[])
    chunks = chunk_extraction_with_strategy(
        extraction,
        strategy="fixed_delimiter",
        delimiter="\\n\\n",
    )
    assert [chunk.text for chunk in chunks] == ["alpha", "beta"]


def test_fixed_size_kb_override() -> None:
    settings = Settings()
    config = KnowledgeBaseAdapterConfig.model_validate(
        {"ingestion": {"chunking_strategy": "fixed_size", "chunk_size": 512, "chunk_overlap": 64}}
    )
    effective = resolve_effective_settings(settings, config, scope="ingestion")
    assert effective.rag_chunking_strategy == "fixed_size"
    assert effective.rag_chunk_size == 512
    assert effective.rag_chunk_overlap == 64


def test_preprocess_profile_kb_override() -> None:
    settings = Settings()
    config = KnowledgeBaseAdapterConfig.model_validate(
        {"ingestion": {"preprocess_profile": "office_to_pdf"}}
    )
    effective = resolve_effective_settings(settings, config, scope="ingestion")
    assert effective.rag_preprocess_profile == "office_to_pdf"


# --- 派生系譜(溯源)の貫通 ---


def test_source_derivation_threads_into_extraction_and_chunks() -> None:
    from app.rag.chunking import Chunk
    from app.rag.ingestion import (
        _chunks_with_source_derivation,
        _extraction_with_source_derivation,
        _passthrough_derivation,
        _source_derivation_id,
    )

    derivation = _passthrough_derivation(
        profile="text_normalize", source_sha="deadbeef", content_type="text/plain"
    )
    extraction = StructuredExtraction(raw_text="x", document_type="ドキュメント", summary="s")
    enriched = _extraction_with_source_derivation(extraction, derivation)
    stored = enriched.parser_artifacts["source_derivation"]
    assert isinstance(stored, dict)
    assert stored["derivation_id"] == derivation.derivation_id
    assert _source_derivation_id(enriched) == derivation.derivation_id

    chunks = [Chunk(text="a", index=0, start_offset=0, end_offset=1, metadata={})]
    stamped = _chunks_with_source_derivation(chunks, _source_derivation_id(enriched))
    assert stamped[0].metadata["source_derivation_id"] == derivation.derivation_id


def test_source_derivation_id_absent_returns_none() -> None:
    from app.rag.ingestion import _source_derivation_id

    extraction = StructuredExtraction(raw_text="x", document_type="ドキュメント", summary="s")
    assert _source_derivation_id(extraction) is None
