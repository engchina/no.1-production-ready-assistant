"""GPU OCR parser adapter 候補として整備した登録のテスト(PoweRAG 由来)。

実 package は未導入のため、登録口・readiness・routing・安全な fallback を検証する。
実 OCR は OCI Enterprise AI VLM へ再マップする前提で、半状態(導入を掲げて実際は parse 不可)を
避けることを確認する。
"""

from __future__ import annotations

from rag_parser_core.capabilities import ADAPTER_CAPABILITIES
from rag_parser_core.registry import (
    EXTERNAL_ADAPTER_PACKAGES,
    ParserRegistryResult,
    _external_adapter_result,
    parse_with_registry,
)

from app.clients.external_parser import ENGINE_SPECS
from app.config import Settings
from app.rag.parser_adapter_readiness import (
    ADAPTER_ORDER,
    ADAPTER_PACKAGES,
    parser_adapter_runtime_settings,
)
from app.rag.parser_adapter_routing import adapter_order_for_source_kind
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
        parser_profile="enterprise_ai_pdf_layout",
    )


def test_gpu_ocr_engines_are_registered_candidates() -> None:
    assert "mineru" in ADAPTER_PACKAGES
    assert "dots_ocr" in ADAPTER_PACKAGES
    # 構造化レコード JSONL は Unstructured の後、XML ツリー JSON の前(#2457)。
    assert ADAPTER_ORDER == (
        "docling",
        "unstructured",
        "structured_records_jsonl",
        "xml_tree_json",
        "mineru",
        "dots_ocr",
    )
    # xml_tree_json・structured_records_jsonl は Docling・Unstructured と同じ使用エンジン(#2042)。
    assert set(EXTERNAL_ADAPTER_PACKAGES) == {
        "docling",
        "unstructured",
        "mineru",
        "dots_ocr",
        "structured_records_jsonl",
        "xml_tree_json",
    }


def test_removed_engines_are_not_registered() -> None:
    """Marker / Unlimited-OCR / GLM-OCR は削除した(#270)。どの登録口にも残さない。"""
    for removed in ("marker", "unlimited_ocr", "glm_ocr", "oci_genai_vision", "enterprise_ai_vlm"):
        assert removed not in ADAPTER_PACKAGES
        assert removed not in EXTERNAL_ADAPTER_PACKAGES
        assert removed not in ADAPTER_CAPABILITIES
        assert removed not in ENGINE_SPECS
        for source_kind in ("pdf", "image", "office", "html", "email", "unknown"):
            assert removed not in adapter_order_for_source_kind(source_kind)


def test_routing_adds_ocr_engines_for_pdf_and_image() -> None:
    pdf_order = adapter_order_for_source_kind("pdf")
    assert pdf_order == ("docling", "unstructured", "mineru")
    image_order = adapter_order_for_source_kind("image")
    assert image_order == ("unstructured", "docling", "dots_ocr", "mineru")


def test_routing_only_offers_backends_that_declare_the_source_kind() -> None:
    """routing の候補は、対応形式の正本(ADAPTER_CAPABILITIES)が宣言する backend だけ(#366)。

    Docling の parser サービスは PDF と画像だけを解析する。office / html / unknown に
    Docling を候補として出すと、strict の contract が必ず fallback で落ち、scorecard も
    「対応している」と誤って示す。
    """
    from rag_parser_core.routing import ADAPTER_ORDER_BY_SOURCE_KIND

    for source_kind, order in ADAPTER_ORDER_BY_SOURCE_KIND.items():
        for backend in order:
            assert SourceModality(source_kind) in ADAPTER_CAPABILITIES[backend].modalities, (
                backend,
                source_kind,
            )
    for source_kind in ("office", "html", "email", "text", "unknown"):
        assert "docling" not in adapter_order_for_source_kind(source_kind)
    assert adapter_order_for_source_kind("office") == ("unstructured", "mineru")
    assert adapter_order_for_source_kind("html") == ("unstructured",)
    assert adapter_order_for_source_kind("unknown") == ("unstructured",)


def test_readiness_reports_missing_when_package_absent() -> None:
    snapshot = parser_adapter_runtime_settings(
        Settings.model_construct(
            rag_parser_adapter_backend="mineru",
            rag_parser_docling_enabled=False,
            rag_parser_unstructured_enabled=False,
            rag_parser_mineru_enabled=True,
            rag_parser_dots_ocr_enabled=False,
        )
    )
    status = {adapter.backend: adapter for adapter in snapshot.adapters}
    # 有効化したが未導入 → missing。flag off かつ未導入 → disabled。
    assert status["mineru"].status == "missing"
    assert status["mineru"].install_package == "外部 MinerU API"
    assert status["dots_ocr"].status == "disabled"


def test_uninstalled_adapter_falls_back_safely() -> None:
    # 未導入 backend を呼んでも例外なく package_missing fallback を返す(半状態を避ける)。
    result = _external_adapter_result(
        "mineru",
        source_bytes=b"%PDF-1.4 dummy",
        source_profile=None,
        content_type="application/pdf",
    )
    assert result.extraction is None
    assert result.fallback_used is True
    assert "mineru_adapter_package_missing" in result.warnings


def test_parse_with_registry_routes_enabled_mineru_pdf_to_runner() -> None:
    """MinerU が選択・有効化されていれば PDF を外部 runner へ渡す。"""
    calls: list[str] = []

    def runner(
        backend: str,
        source_bytes: bytes,
        source_profile: SourceProfile | None,
        content_type: str,
    ) -> ParserRegistryResult:
        calls.append(backend)
        assert source_bytes == b"%PDF-1.4 scanned"
        assert source_profile is not None
        assert content_type == "application/pdf"
        return ParserRegistryResult(
            extraction=StructuredExtraction(raw_text="MinerU OCR 本文"),
            parser_backend=backend,
            parser_version="mineru:test",
            template="structure_aware",
        )

    result = parse_with_registry(
        b"%PDF-1.4 scanned",
        source_profile=_pdf_profile(),
        content_type="application/pdf",
        adapter_backend="mineru",
        mineru_enabled=True,
        external_adapter_runner=runner,
    )

    assert calls == ["mineru"]
    assert result.parser_backend == "mineru"
    assert result.extraction is not None
    assert result.extraction.raw_text == "MinerU OCR 本文"
    assert "mineru_adapter_feature_flag_disabled" not in result.warnings
