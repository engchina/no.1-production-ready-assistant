"""parser adapter compatibility matrix のテスト。"""

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest
from pytest import MonkeyPatch
from rag_parser_core import registry as parser_registry
from rag_parser_core.result import ParserRegistryResult

from app.config import Settings
from app.rag import parser_adapter_contract as parser_adapter_contract_module
from app.rag import parser_adapter_contract_cli, parser_adapter_readiness
from app.rag.mineru_license import set_mineru_license_acknowledgement
from app.rag.parser_adapter_contract import (
    ParserAdapterCompatibilityCase,
    ParserAdapterCompatibilityMatrix,
    ParserAdapterFixtureSpec,
    parser_adapter_contract_artifact_payload,
    parser_adapter_contract_summary,
    parser_adapter_fixture_root_from_manifest,
    parser_adapter_fixture_specs_from_manifest,
    run_parser_adapter_compatibility_matrix,
)
from app.schemas.extraction import DocumentElement, ExtractionPage, StructuredExtraction


def test_compatibility_matrix_does_not_block_disabled_adapters() -> None:
    """local 設定では外部 adapter が disabled でも compatibility は失敗にしない。"""
    matrix = run_parser_adapter_compatibility_matrix(
        Settings(
            rag_parser_adapter_backend="local",
            rag_parser_docling_enabled=False,
        ),
        source_kinds=["pdf"],
        backends=["docling"],
    )

    assert matrix.passed is True
    case = matrix.cases[0]
    assert case.backend == "docling"
    assert case.source_kind == "pdf"
    assert case.status in {"available", "disabled"}
    assert case.blocking is False


def test_compatibility_matrix_blocks_enabled_missing_adapter(
    monkeypatch: MonkeyPatch,
) -> None:
    """有効化された adapter package がない場合は matrix を失敗にする。"""
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (False, None, None),
    )

    matrix = run_parser_adapter_compatibility_matrix(
        Settings(
            rag_parser_adapter_backend="docling",
            rag_parser_docling_enabled=True,
        ),
        source_kinds=["pdf"],
        backends=["docling"],
    )

    assert matrix.passed is False
    assert matrix.blocking_failure_count == 1
    case = matrix.cases[0]
    assert case.status == "missing"
    assert case.blocking is True
    assert case.warning_codes == ("adapter_package_missing",)


def test_compatibility_matrix_does_not_block_unrouted_backend_by_default(
    monkeypatch: MonkeyPatch,
) -> None:
    """coverage matrix では非対象 backend/source pair を unsupported として記録するだけにする。"""
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda import_name, _distribution_names: (
            import_name == "docling",
            "2.103.0" if import_name == "docling" else None,
            import_name if import_name == "docling" else None,
        ),
    )

    matrix = run_parser_adapter_compatibility_matrix(
        Settings(
            rag_parser_adapter_backend="docling",
            rag_parser_docling_enabled=True,
        ),
        source_kinds=["email"],
        backends=["docling"],
    )

    assert matrix.passed is True
    assert matrix.blocking_failure_count == 0
    case = matrix.cases[0]
    assert case.status == "unsupported"
    assert case.blocking is False
    assert case.reason_codes == ("adapter_not_routed_for_source",)


def test_strict_compatibility_matrix_requires_backend_schema_remap_evidence(
    monkeypatch: MonkeyPatch,
) -> None:
    """strict smoke は active adapter に少なくとも1件の passed remap 証跡を要求する。"""
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda import_name, _distribution_names: (
            import_name == "docling",
            "2.103.0" if import_name == "docling" else None,
            import_name if import_name == "docling" else None,
        ),
    )

    matrix = run_parser_adapter_compatibility_matrix(
        Settings(
            rag_parser_adapter_backend="docling",
            rag_parser_docling_enabled=True,
        ),
        source_kinds=["email"],
        backends=["docling"],
        require_backend_evidence=True,
    )

    assert matrix.passed is False
    assert matrix.blocking_failure_count == 1
    assert matrix.case_count == 2
    unsupported_case = matrix.cases[0]
    evidence_case = matrix.cases[1]
    assert unsupported_case.status == "unsupported"
    assert unsupported_case.blocking is False
    assert evidence_case.status == "failed"
    assert evidence_case.blocking is True
    assert evidence_case.source_kind == "unknown"
    assert evidence_case.fixture_name == "manifest-fixture-set"
    assert evidence_case.reason_codes == ("adapter_schema_remap_evidence_missing",)


def test_compatibility_matrix_runs_installed_adapter_remap(
    monkeypatch: MonkeyPatch,
) -> None:
    """runtime に package があれば parser service runner 経由で schema remap を検証する。"""
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda import_name, _distribution_names: (
            import_name == "unstructured",
            "7.8.9" if import_name == "unstructured" else None,
            import_name if import_name == "unstructured" else None,
        ),
    )

    def parser_service_runner(
        _self: object,
        backend: str,
        _source_bytes: bytes,
        _source_profile: object,
        content_type: str,
        *,
        fail_fast: bool = False,
    ) -> ParserRegistryResult:
        assert backend == "unstructured"
        assert content_type == "application/pdf"
        assert fail_fast is False
        return ParserRegistryResult(
            extraction=StructuredExtraction(
                raw_text="artifact には raw_text を出さない",
                pages=[ExtractionPage(page_number=1, element_ids=["compatibility-element-1"])],
                elements=[
                    DocumentElement(
                        kind="text",
                        text="非機密 artifact には入れない本文",
                        element_id="compatibility-element-1",
                        source_parser="unstructured_adapter",
                        page_number=1,
                    )
                ],
            ),
            parser_backend="unstructured",
            parser_version="7.8.9",
            template="pdf_layout",
        )

    monkeypatch.setattr(
        parser_adapter_contract_module.ParserServiceClient,
        "runner",
        parser_service_runner,
    )

    matrix = run_parser_adapter_compatibility_matrix(
        Settings(
            rag_parser_adapter_backend="unstructured",
            rag_parser_unstructured_enabled=True,
        ),
        source_kinds=["pdf"],
        backends=["unstructured"],
    )

    assert matrix.passed is True
    case = matrix.cases[0]
    assert case.status == "passed"
    assert case.parser_backend == "unstructured"
    assert case.adapter_import_name == "unstructured"
    assert case.adapter_distribution_name == "unstructured"
    assert case.adapter_package_version == "7.8.9"
    assert case.element_count == 1
    assert "schema_remap_contract_ok" in case.reason_codes
    assert "非機密 artifact" not in json.dumps(asdict(matrix), ensure_ascii=False)


def test_compatibility_matrix_uses_parser_service_runner_for_dots_ocr(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """GPU OCR adapter は backend in-process ではなく parser service runner で検証する。"""
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / "scan.png").write_bytes(b"\x89PNG scanned")
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (False, None, None),
    )
    calls: list[str] = []

    def parser_service_runner(
        _self: object,
        backend: str,
        source_bytes: bytes,
        source_profile: Any,
        content_type: str,
        *,
        fail_fast: bool = False,
    ) -> ParserRegistryResult:
        calls.append(backend)
        assert source_bytes == b"\x89PNG scanned"
        assert source_profile.sanitized_file_name == "scan.png"
        assert content_type == "image/png"
        assert fail_fast is False
        return ParserRegistryResult(
            extraction=StructuredExtraction(
                raw_text="artifact には raw_text を出さない",
                elements=[
                    DocumentElement(
                        kind="text",
                        text="非機密 artifact には入れない本文",
                        element_id="dots-ocr-el-1",
                        source_parser="dots_ocr_adapter",
                        page_number=1,
                        bbox=[0.1, 0.1, 0.9, 0.2],
                    )
                ],
            ),
            parser_backend="dots_ocr",
            parser_version="dots_ocr:test",
            template="ocr_page",
        )

    monkeypatch.setattr(
        parser_adapter_contract_module.ParserServiceClient,
        "runner",
        parser_service_runner,
    )

    matrix = run_parser_adapter_compatibility_matrix(
        Settings(
            rag_parser_adapter_backend="dots_ocr",
            rag_parser_dots_ocr_enabled=True,
            rag_parser_dots_ocr_api_host="http://external-dots",
        ),
        fixture_root=fixture_root,
        fixture_specs=(
            ParserAdapterFixtureSpec(
                source_kind="image",
                file_name="scan.png",
                content_type="image/png",
            ),
        ),
        backends=["dots_ocr"],
    )

    assert calls == ["dots_ocr"]
    assert matrix.passed is True
    assert matrix.cases[0].status == "passed"


@pytest.mark.usefixtures("mineru_selectable")
def test_compatibility_matrix_mineru_unconfigured_connection_is_missing(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """外部接続未設定時は GPU OCR を in-process 起動せず missing として記録する。"""
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / "scan.pdf").write_bytes(b"%PDF-1.7 scanned")
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (False, None, None),
    )
    monkeypatch.setattr(parser_registry, "_module_available", lambda name: name == "mineru")
    settings = Settings(
        rag_parser_adapter_backend="mineru",
        rag_parser_mineru_enabled=True,
        rag_parser_mineru_api_host="",
    )
    set_mineru_license_acknowledgement(settings, by="", at="2026-10-26T00:00:00+00:00")

    matrix = run_parser_adapter_compatibility_matrix(
        settings,
        fixture_root=fixture_root,
        fixture_specs=(
            ParserAdapterFixtureSpec(
                source_kind="pdf",
                file_name="scan.pdf",
                content_type="application/pdf",
            ),
        ),
        backends=["mineru"],
    )

    case = matrix.cases[0]
    assert matrix.passed is False
    assert case.status == "missing"
    assert case.warning_codes == ("external_parser_unconfigured",)


def test_compatibility_matrix_uses_manifest_fixtures_for_real_remap(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """manifest case fixture ごとに package 経由の schema remap 証跡を作る。"""
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / "scanned-contract-ja.pdf").write_bytes(b"%PDF-1.7 fixture")
    (fixture_root / "manual.html").write_text("<h1>検索運用</h1>", encoding="utf-8")
    manifest_path = tmp_path / "manifests" / "file-processing-golden-set.json"
    manifest_path.parent.mkdir()
    manifest = {
        "fixture_root": "../fixtures",
        "cases": [
            {
                "id": "scanned-pdf-ocr-ja",
                "fixture": "scanned-contract-ja.pdf",
                "modality": "pdf",
                "scenario": "scanned_pdf_ocr",
                "adapter_schema_remap": True,
            },
            {
                "id": "html-semantic-blocks",
                "fixture": "manual.html",
                "modality": "html",
                "scenario": "html_semantic_blocks",
                "adapter_schema_remap": True,
            },
        ],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda import_name, _distribution_names: (
            import_name == "unstructured",
            "7.8.9" if import_name == "unstructured" else None,
            import_name if import_name == "unstructured" else None,
        ),
    )
    calls: list[tuple[str, str, str]] = []

    def parse_manifest_fixture(
        *_args: Any,
        **kwargs: Any,
    ) -> ParserRegistryResult:
        source_profile = kwargs["source_profile"]
        content_type = kwargs["content_type"]
        adapter_backend = kwargs["adapter_backend"]
        calls.append(
            (
                source_profile.sanitized_file_name,
                content_type,
                adapter_backend,
            )
        )
        is_pdf = source_profile.sanitized_file_name.endswith(".pdf")
        return ParserRegistryResult(
            extraction=StructuredExtraction(
                raw_text="artifact には raw_text を出さない",
                pages=[ExtractionPage(page_number=1, element_ids=["adapter-el-1"])],
                elements=[
                    DocumentElement(
                        kind="title" if not is_pdf else "text",
                        text="非機密 artifact には入れない本文",
                        element_id="adapter-el-1",
                        source_parser="unstructured_adapter",
                        page_number=1,
                        section_path=["検索運用"] if not is_pdf else [],
                        metadata={"link_count": 1} if not is_pdf else {},
                    )
                ],
            ),
            parser_backend="unstructured",
            parser_version="7.8.9",
            template="html_semantic" if not is_pdf else "pdf_layout",
        )

    monkeypatch.setattr(
        "rag_parser_core.registry.parse_with_registry",
        parse_manifest_fixture,
    )

    specs = parser_adapter_fixture_specs_from_manifest(manifest)
    matrix = run_parser_adapter_compatibility_matrix(
        Settings(
            rag_parser_adapter_backend="unstructured",
            rag_parser_unstructured_enabled=True,
        ),
        fixture_root=parser_adapter_fixture_root_from_manifest(
            manifest,
            manifest_path=manifest_path,
        ),
        fixture_specs=specs,
        backends=["unstructured"],
    )

    assert matrix.passed is True
    assert matrix.fixture_root == str(fixture_root)
    assert matrix.case_count == 2
    assert calls == [
        ("scanned-contract-ja.pdf", "application/pdf", "unstructured"),
        ("manual.html", "text/html", "unstructured"),
    ]
    assert [case.case_id for case in matrix.cases] == [
        "scanned-pdf-ocr-ja",
        "html-semantic-blocks",
    ]
    assert [case.scenario for case in matrix.cases] == [
        "scanned_pdf_ocr",
        "html_semantic_blocks",
    ]
    assert {case.status for case in matrix.cases} == {"passed"}
    summary = parser_adapter_contract_summary(matrix)
    assert summary["backend_passed_scenarios"] == {
        "unstructured": ["html_semantic_blocks", "scanned_pdf_ocr"]
    }
    assert "schema_remap_contract_ok" in matrix.cases[0].reason_codes
    assert "非機密 artifact" not in json.dumps(asdict(matrix), ensure_ascii=False)


def test_compatibility_matrix_requires_real_package_version_evidence(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """schema remap が返っても package distribution/version 証跡がなければ失敗にする。"""
    # html を宣言する Unstructured で検証する
    # (Docling は PDF と画像だけで、html に routing されない。#366)。
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / "manual.html").write_text("<h1>検索運用</h1>", encoding="utf-8")
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda import_name, _distribution_names: (
            import_name == "unstructured",
            None,
            None,
        ),
    )

    def parse_html_fixture(*_args: Any, **_kwargs: Any) -> ParserRegistryResult:
        return ParserRegistryResult(
            extraction=StructuredExtraction(
                raw_text="artifact には raw_text を出さない",
                elements=[
                    DocumentElement(
                        kind="title",
                        text="非機密 artifact には入れない本文",
                        element_id="adapter-html-1",
                        source_parser="unstructured_adapter",
                        section_path=["検索運用"],
                    )
                ],
            ),
            parser_backend="unstructured",
            parser_version="unstructured_adapter_v1",
            template="html_semantic",
        )

    monkeypatch.setattr(
        "rag_parser_core.registry.parse_with_registry",
        parse_html_fixture,
    )

    matrix = run_parser_adapter_compatibility_matrix(
        Settings(
            rag_parser_adapter_backend="unstructured",
            rag_parser_unstructured_enabled=True,
        ),
        fixture_root=fixture_root,
        fixture_specs=(
            ParserAdapterFixtureSpec(
                source_kind="html",
                file_name="manual.html",
                content_type="text/html",
                case_id="html-semantic-blocks",
                scenario="html_semantic_blocks",
            ),
        ),
        backends=["unstructured"],
        require_backend_evidence=True,
    )

    case = matrix.cases[0]
    assert matrix.passed is False
    assert matrix.blocking_failure_count == 1
    assert case.status == "failed"
    assert case.adapter_import_name == "unstructured"
    assert case.adapter_distribution_name is None
    assert case.adapter_package_version is None
    assert case.element_count == 1
    assert case.reason_codes == (
        "adapter_distribution_name_missing",
        "adapter_package_version_missing",
    )


def test_strict_manifest_requires_schema_remap_fixture_for_each_routed_source(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """strict manifest では source kind を正向き fixture 不足で黙って縮小しない。"""
    # html を宣言する Unstructured で検証する
    # (Docling は PDF と画像だけで、html に routing されない。#366)。
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / "manual.html").write_text("<h1>検索運用</h1>", encoding="utf-8")
    manifest = {
        "cases": [
            {
                "id": "corrupted-pdf",
                "fixture": "broken.pdf",
                "modality": "pdf",
                "scenario": "corrupted_file",
                "expected_warning": "pdf_segment_parse_failed",
            },
            {
                "id": "html-semantic-blocks",
                "fixture": "manual.html",
                "modality": "html",
                "scenario": "html_semantic_blocks",
                "adapter_schema_remap": True,
            },
        ],
    }
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda import_name, _distribution_names: (
            import_name == "unstructured",
            "0.23.1" if import_name == "unstructured" else None,
            import_name if import_name == "unstructured" else None,
        ),
    )

    def parse_html_fixture(*_args: Any, **_kwargs: Any) -> ParserRegistryResult:
        return ParserRegistryResult(
            extraction=StructuredExtraction(
                raw_text="artifact には raw_text を出さない",
                elements=[
                    DocumentElement(
                        kind="title",
                        text="非機密 artifact には入れない本文",
                        element_id="adapter-html-1",
                        source_parser="unstructured_adapter",
                        section_path=["検索運用"],
                    )
                ],
            ),
            parser_backend="unstructured",
            parser_version="0.23.1",
            template="html_semantic",
        )

    monkeypatch.setattr(
        "rag_parser_core.registry.parse_with_registry",
        parse_html_fixture,
    )

    matrix = run_parser_adapter_compatibility_matrix(
        Settings(
            rag_parser_adapter_backend="unstructured",
            rag_parser_unstructured_enabled=True,
        ),
        fixture_root=fixture_root,
        source_kinds=["pdf", "html"],
        fixture_specs=parser_adapter_fixture_specs_from_manifest(manifest),
        backends=["unstructured"],
        require_backend_evidence=True,
    )

    assert matrix.passed is False
    assert matrix.source_kinds == ("pdf", "html")
    assert matrix.blocking_failure_count == 1
    assert matrix.case_count == 2
    assert matrix.cases[0].status == "passed"
    source_gap_case = matrix.cases[1]
    assert source_gap_case.source_kind == "pdf"
    assert source_gap_case.status == "failed"
    assert source_gap_case.blocking is True
    assert source_gap_case.reason_codes == ("adapter_schema_remap_fixture_missing_for_source",)
    summary = parser_adapter_contract_summary(matrix)
    assert summary["missing_source_kinds"] == ["pdf"]
    assert summary["blocking_failure_source_kinds"] == ["pdf"]
    assert summary["blocking_failure_reason_counts"] == {
        "adapter_schema_remap_fixture_missing_for_source": 1
    }


def test_manifest_fixture_specs_skip_negative_staging_cases() -> None:
    """unsupported/corrupted case は safe-error gate 用であり adapter remap smoke から外す。"""
    specs = parser_adapter_fixture_specs_from_manifest(
        {
            "cases": [
                {
                    "id": "image-ocr-bbox",
                    "fixture": "receipt-ja.png",
                    "modality": "image",
                    "scenario": "image_ocr_bbox",
                    "expected_chunk_template": "ocr_page",
                },
                {
                    "id": "tiff-image-unsupported",
                    "fixture": "scan.tiff",
                    "modality": "image",
                    "expected_chunk_template": "unsupported_tiff_image",
                    "expected_unsupported_reason": "tiff_image_not_supported",
                },
                {
                    "id": "corrupted-file-partial-failure",
                    "fixture": "broken.xlsx",
                    "modality": "office",
                    "expected_warning": "office_segment_parse_failed",
                },
            ]
        }
    )

    assert [spec.case_id for spec in specs] == ["image-ocr-bbox"]
    assert [spec.file_name for spec in specs] == ["receipt-ja.png"]


def test_manifest_fixture_specs_strict_requires_declared_schema_remap() -> None:
    """strict smoke は adapter_schema_remap=true の manifest fixture だけを証跡にする。"""
    manifest = {
        "cases": [
            {
                "id": "scanned-pdf-ocr-ja",
                "fixture": "scanned-contract-ja.pdf",
                "modality": "pdf",
                "scenario": "scanned_pdf_ocr",
            },
            {
                "id": "html-semantic-blocks",
                "fixture": "manual.html",
                "modality": "html",
                "scenario": "html_semantic_blocks",
                "adapter_schema_remap": True,
            },
        ]
    }

    legacy_specs = parser_adapter_fixture_specs_from_manifest(manifest)
    strict_specs = parser_adapter_fixture_specs_from_manifest(
        manifest,
        require_declared_schema_remap=True,
    )

    assert [spec.case_id for spec in legacy_specs] == [
        "scanned-pdf-ocr-ja",
        "html-semantic-blocks",
    ]
    assert [spec.case_id for spec in strict_specs] == ["html-semantic-blocks"]


def test_compatibility_matrix_requires_pdf_page_lineage(
    monkeypatch: MonkeyPatch,
) -> None:
    """PDF adapter smoke は element があっても page lineage がなければ失敗にする。"""
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda import_name, _distribution_names: (
            import_name == "docling",
            "2.103.0" if import_name == "docling" else None,
            import_name if import_name == "docling" else None,
        ),
    )

    def parse_without_page_lineage(*_args: object, **_kwargs: object) -> ParserRegistryResult:
        return ParserRegistryResult(
            extraction=StructuredExtraction(
                raw_text="PDF text",
                elements=[
                    DocumentElement(
                        kind="text",
                        text="PDF text",
                        element_id="adapter-el-1",
                        source_parser="docling_adapter",
                    )
                ],
            ),
            parser_backend="docling",
            parser_version="2.103.0",
            template="pdf_layout",
        )

    monkeypatch.setattr(
        "rag_parser_core.registry.parse_with_registry",
        parse_without_page_lineage,
    )

    matrix = run_parser_adapter_compatibility_matrix(
        Settings(
            rag_parser_adapter_backend="docling",
            rag_parser_docling_enabled=True,
        ),
        source_kinds=["pdf"],
        backends=["docling"],
    )

    assert matrix.passed is False
    assert matrix.blocking_failure_count == 1
    case = matrix.cases[0]
    assert case.status == "failed"
    assert case.element_count == 1
    assert case.page_count == 0
    assert case.reason_codes == ("schema_remap_page_lineage_missing",)


def test_compatibility_matrix_accepts_docx_heading_lineage(
    monkeypatch: MonkeyPatch,
) -> None:
    """DOCX は page がなくても heading/section lineage があれば Office contract とする。"""
    # Office を宣言する Unstructured で検証する
    # (Docling は PDF と画像だけで、office に routing されない。#366)。
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda import_name, _distribution_names: (
            import_name == "unstructured",
            "0.23.1" if import_name == "unstructured" else None,
            import_name if import_name == "unstructured" else None,
        ),
    )

    def parse_docx_heading_lineage(*_args: object, **_kwargs: object) -> ParserRegistryResult:
        return ParserRegistryResult(
            extraction=StructuredExtraction(
                raw_text="経費ポリシー\n本文",
                elements=[
                    DocumentElement(
                        kind="title",
                        text="経費ポリシー",
                        element_id="adapter-title-1",
                        source_parser="unstructured_adapter",
                        metadata={
                            "chunk_template": "office_document",
                            "section_level": 1,
                            "adapter_element_type": "text",
                        },
                    ),
                    DocumentElement(
                        kind="text",
                        text="本文",
                        element_id="adapter-body-1",
                        source_parser="unstructured_adapter",
                    ),
                ],
            ),
            parser_backend="unstructured",
            parser_version="0.23.1",
            template="office_document",
        )

    monkeypatch.setattr(
        "rag_parser_core.registry.parse_with_registry",
        parse_docx_heading_lineage,
    )

    matrix = run_parser_adapter_compatibility_matrix(
        Settings(
            rag_parser_adapter_backend="unstructured",
            rag_parser_unstructured_enabled=True,
        ),
        source_kinds=["office"],
        backends=["unstructured"],
    )

    assert matrix.passed is True
    case = matrix.cases[0]
    assert case.status == "passed"
    assert case.reason_codes == ("schema_remap_contract_ok",)


def test_compatibility_matrix_accepts_office_pages_lineage(
    monkeypatch: MonkeyPatch,
) -> None:
    """PPTX などは elements の page_number がなくても pages[] で slide lineage とする。"""
    # Office を宣言する Unstructured で検証する
    # (Docling は PDF と画像だけで、office に routing されない。#366)。
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda import_name, _distribution_names: (
            import_name == "unstructured",
            "0.23.1" if import_name == "unstructured" else None,
            import_name if import_name == "unstructured" else None,
        ),
    )

    def parse_pptx_pages_lineage(*_args: object, **_kwargs: object) -> ParserRegistryResult:
        return ParserRegistryResult(
            extraction=StructuredExtraction(
                raw_text="Slide 1\nSlide 2",
                pages=[
                    ExtractionPage(page_number=1, element_ids=["slide-text-1"]),
                    ExtractionPage(page_number=2, element_ids=["slide-text-2"]),
                ],
                elements=[
                    DocumentElement(
                        kind="text",
                        text="Slide 1",
                        element_id="slide-text-1",
                        source_parser="unstructured_adapter",
                    ),
                    DocumentElement(
                        kind="text",
                        text="Slide 2",
                        element_id="slide-text-2",
                        source_parser="unstructured_adapter",
                    ),
                ],
            ),
            parser_backend="unstructured",
            parser_version="0.23.1",
            template="office_slide",
        )

    monkeypatch.setattr(
        "rag_parser_core.registry.parse_with_registry",
        parse_pptx_pages_lineage,
    )

    matrix = run_parser_adapter_compatibility_matrix(
        Settings(
            rag_parser_adapter_backend="unstructured",
            rag_parser_unstructured_enabled=True,
        ),
        source_kinds=["office"],
        backends=["unstructured"],
    )

    assert matrix.passed is True
    case = matrix.cases[0]
    assert case.status == "passed"
    assert case.page_count == 2
    assert case.reason_codes == ("schema_remap_contract_ok",)


def test_parser_adapter_contract_cli_writes_non_sensitive_artifact(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """CLI は non-blocking disabled matrix を JSON artifact として保存できる。"""
    output_path = tmp_path / "adapter-contract.json"
    monkeypatch.setattr(
        parser_adapter_contract_cli,
        "get_settings",
        lambda: Settings(
            rag_parser_adapter_backend="local",
            rag_parser_docling_enabled=False,
        ),
    )
    _patch_service_health(monkeypatch, healthy=set())

    exit_code = parser_adapter_contract_cli.main(
        ["--backend", "docling", "--source-kind", "pdf", "--output", str(output_path)]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["passed"] is True
    assert payload["cases"][0]["status"] in {"available", "disabled"}
    assert payload["summary"]["passed"] is True
    assert payload["summary"]["case_count"] == payload["case_count"]
    assert payload["summary"]["backend_source_status"]["docling"]["pdf"] in {
        "available",
        "disabled",
    }
    assert "adapter_disabled" in payload["summary"]["reason_code_counts"] or (
        "adapter_available" in payload["summary"]["reason_code_counts"]
    )
    assert "raw_text" not in output_path.read_text(encoding="utf-8")
    assert "policy-ja.pdf" not in output_path.read_text(encoding="utf-8")
    assert "file-processing-fixtures" not in output_path.read_text(encoding="utf-8")


def _patch_service_health(monkeypatch: MonkeyPatch, *, healthy: set[str]) -> None:
    """parser サービスの /health を差し替える(healthy にない backend は未達)。

    in-process の package は常に無い状態にし、可用性がサービスだけで決まることを確かめる。
    """
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (False, None, None),
    )
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_probe_service_health",
        lambda _settings, backend: (
            (True, "2.129.0", backend) if backend in healthy else (False, None, None)
        ),
    )


def test_compatibility_matrix_records_missing_runtime_when_not_required(
    monkeypatch: MonkeyPatch,
) -> None:
    """実行環境を必須にしない通常実行では、選択済み adapter の missing を記録だけにする。"""
    monkeypatch.setattr(
        parser_adapter_readiness,
        "_package_info",
        lambda *_args: (False, None, None),
    )

    matrix = run_parser_adapter_compatibility_matrix(
        Settings(
            rag_parser_adapter_backend="docling",
            rag_parser_docling_enabled=True,
        ),
        source_kinds=["pdf"],
        backends=["docling"],
        require_selected_adapter_runtime=False,
    )

    assert matrix.passed is True
    assert matrix.blocking_failure_count == 0
    case = matrix.cases[0]
    assert case.status == "missing"
    assert case.blocking is False
    assert case.reason_codes == ("adapter_missing",)


def test_compatibility_matrix_still_blocks_selected_disabled_adapter_when_not_required() -> None:
    """選択した adapter の feature flag が OFF の設定矛盾は、通常実行でも blocking にする。"""
    matrix = run_parser_adapter_compatibility_matrix(
        Settings(
            rag_parser_adapter_backend="unstructured",
            rag_parser_unstructured_enabled=False,
        ),
        source_kinds=["pdf"],
        backends=["unstructured"],
        require_selected_adapter_runtime=False,
    )

    assert matrix.passed is False
    case = matrix.cases[0]
    assert case.status == "disabled"
    assert case.blocking is True


def test_compatibility_matrix_still_blocks_reachable_adapter_fallback_when_not_required(
    monkeypatch: MonkeyPatch,
) -> None:
    """実行環境が見つかった adapter の fallback は、通常実行でも blocking にする。"""
    _patch_service_health(monkeypatch, healthy={"docling"})

    def unreachable_runner(
        _self: object,
        backend: str,
        _source_bytes: bytes,
        _source_profile: object,
        _content_type: str,
        *,
        fail_fast: bool = False,
    ) -> ParserRegistryResult:
        return ParserRegistryResult(
            extraction=None,
            parser_backend=backend,
            fallback_used=True,
            warnings=(f"{backend}_adapter_service_unreachable",),
        )

    monkeypatch.setattr(
        parser_adapter_contract_module.ParserServiceClient,
        "runner",
        unreachable_runner,
    )

    matrix = run_parser_adapter_compatibility_matrix(
        Settings(
            rag_parser_adapter_backend="docling",
            rag_parser_docling_enabled=True,
            rag_parser_readiness_probe_enabled=True,
        ),
        source_kinds=["pdf"],
        backends=["docling"],
        require_selected_adapter_runtime=False,
    )

    assert matrix.passed is False
    case = matrix.cases[0]
    assert case.status == "fallback"
    assert case.blocking is True


def test_parser_adapter_contract_cli_default_records_unreachable_parser_service(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """既定設定(Docling 選択)で parser サービスが無い通常実行は、記録だけで exit 0 にする。"""
    output_path = tmp_path / "adapter-contract-default.json"
    monkeypatch.setattr(parser_adapter_contract_cli, "get_settings", lambda: Settings())
    _patch_service_health(monkeypatch, healthy=set())

    exit_code = parser_adapter_contract_cli.main(
        ["--source-kind", "pdf", "--output", str(output_path)]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["passed"] is True
    docling = [case for case in payload["cases"] if case["backend"] == "docling"]
    assert docling[0]["status"] == "missing"
    assert docling[0]["blocking"] is False
    unstructured = [case for case in payload["cases"] if case["backend"] == "unstructured"]
    assert unstructured[0]["status"] == "disabled"
    assert unstructured[0]["blocking"] is False


def test_parser_adapter_contract_cli_judges_adapter_by_parser_service(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """backend の venv に package が無くても、parser サービスが健全なら remap を検証する。"""
    output_path = tmp_path / "adapter-contract-service.json"
    monkeypatch.setattr(parser_adapter_contract_cli, "get_settings", lambda: Settings())
    _patch_service_health(monkeypatch, healthy={"docling"})

    def parser_service_runner(
        _self: object,
        backend: str,
        _source_bytes: bytes,
        _source_profile: object,
        _content_type: str,
        *,
        fail_fast: bool = False,
    ) -> ParserRegistryResult:
        return ParserRegistryResult(
            extraction=StructuredExtraction(
                raw_text="本文",
                pages=[ExtractionPage(page_number=1, element_ids=["element-1"])],
                elements=[
                    DocumentElement(
                        kind="text",
                        text="本文",
                        element_id="element-1",
                        source_parser="docling_adapter",
                        page_number=1,
                    )
                ],
            ),
            parser_backend=backend,
            parser_version="2.129.0",
            template="pdf_layout",
        )

    monkeypatch.setattr(
        parser_adapter_contract_module.ParserServiceClient,
        "runner",
        parser_service_runner,
    )

    exit_code = parser_adapter_contract_cli.main(
        ["--strict", "--backend", "docling", "--source-kind", "pdf", "--output", str(output_path)]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert payload["passed"] is True
    assert payload["cases"][0]["status"] == "passed"
    assert payload["cases"][0]["adapter_package_version"] == "2.129.0"


def test_parser_adapter_contract_artifact_redacts_fixture_identifiers() -> None:
    """artifact payload は fixture path/name/case id を hash label にする。"""
    matrix = ParserAdapterCompatibilityMatrix(
        passed=False,
        fixture_root="/private/customer-fixtures/acme",
        source_kinds=("pdf",),
        backends=("docling",),
        case_count=1,
        blocking_failure_count=1,
        cases=(
            ParserAdapterCompatibilityCase(
                backend="docling",
                source_kind="pdf",
                fixture_name="staging/acme-contract-2026.pdf",
                content_type="application/pdf",
                status="fixture_missing",
                blocking=True,
                case_id="acme-contract-case",
                scenario="scanned_pdf_ocr",
                reason_codes=("fixture_missing",),
            ),
        ),
    )

    payload = parser_adapter_contract_artifact_payload(matrix)
    payload_text = json.dumps(payload, ensure_ascii=False)
    cases = cast(list[dict[str, object]], payload["cases"])
    summary = cast(dict[str, object], payload["summary"])
    blocking_failures = cast(
        list[dict[str, object]],
        summary["blocking_failures"],
    )
    blocking_failure_case_refs = cast(list[str], summary["blocking_failure_case_refs"])

    assert payload["fixture_root"] != "/private/customer-fixtures/acme"
    assert str(payload["fixture_root"]).startswith("fixture_root:")
    assert str(cases[0]["fixture_name"]).startswith("pdf_fixture:")
    assert cases[0]["fixture_name_hash"]
    assert cases[0]["case_ref_hash"]
    assert blocking_failures[0]["case_ref_hash"]
    assert blocking_failure_case_refs[0].startswith("case:")
    assert "/private/customer-fixtures/acme" not in payload_text
    assert "acme-contract-2026.pdf" not in payload_text
    assert "acme-contract-case" not in payload_text


def test_parser_adapter_contract_cli_strict_blocks_missing_adapter(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """CLI strict は feature flag を有効化し、未導入 adapter を gate 失敗にする。"""
    output_path = tmp_path / "adapter-contract-strict.json"
    monkeypatch.setattr(
        parser_adapter_contract_cli,
        "get_settings",
        lambda: Settings(
            rag_parser_adapter_backend="local",
            rag_parser_docling_enabled=False,
            rag_parser_unstructured_enabled=False,
        ),
    )
    _patch_service_health(monkeypatch, healthy=set())

    exit_code = parser_adapter_contract_cli.main(
        [
            "--strict",
            "--backend",
            "docling",
            "--source-kind",
            "pdf",
            "--output",
            str(output_path),
        ]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["passed"] is False
    assert payload["blocking_failure_count"] == 1
    assert payload["cases"][0]["status"] == "missing"
    assert payload["cases"][0]["blocking"] is True
    assert payload["summary"]["blocking_failures"][0] == {
        "backend": "docling",
        "source_kind": "pdf",
        "status": "missing",
        "warning_codes": ["adapter_package_missing"],
        "reason_codes": ["adapter_missing"],
    }


def test_parser_adapter_contract_cli_strict_blocks_explicit_unrouted_backend(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """CLI strict で backend/source を明示した場合、未対応 route を合格にしない。"""
    output_path = tmp_path / "adapter-contract-unrouted.json"
    monkeypatch.setattr(
        parser_adapter_contract_cli,
        "get_settings",
        lambda: Settings(
            rag_parser_adapter_backend="local",
            rag_parser_docling_enabled=False,
            rag_parser_unstructured_enabled=False,
        ),
    )
    _patch_service_health(monkeypatch, healthy={"docling"})

    exit_code = parser_adapter_contract_cli.main(
        [
            "--strict",
            "--backend",
            "docling",
            "--source-kind",
            "email",
            "--output",
            str(output_path),
        ]
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert payload["passed"] is False
    assert payload["blocking_failure_count"] == 1
    assert payload["cases"][0]["status"] == "unsupported"
    assert payload["cases"][0]["blocking"] is True
    assert payload["summary"]["blocking_failures"][0] == {
        "backend": "docling",
        "source_kind": "email",
        "status": "unsupported",
        "warning_codes": [],
        "reason_codes": ["adapter_not_routed_for_source"],
    }


def test_parser_adapter_contract_cli_strict_manifest_uses_real_fixtures(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    """CLI strict smoke は staging manifest の fixture specs を contract runner へ渡す。"""
    fixture_root = tmp_path / "fixtures"
    fixture_root.mkdir()
    (fixture_root / "scanned-contract-ja.pdf").write_bytes(b"%PDF-1.7")
    (fixture_root / "manual.html").write_text("<h1>検索運用</h1>", encoding="utf-8")
    manifest_path = tmp_path / "manifests" / "file-processing-golden-set.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text(
        json.dumps(
            {
                "fixture_root": "../fixtures",
                "cases": [
                    {
                        "id": "scanned-pdf-ocr-ja",
                        "fixture": "scanned-contract-ja.pdf",
                        "modality": "pdf",
                        "scenario": "scanned_pdf_ocr",
                        "adapter_schema_remap": True,
                    },
                    {
                        "id": "html-semantic-blocks",
                        "fixture": "manual.html",
                        "modality": "html",
                        "scenario": "html_semantic_blocks",
                        "adapter_schema_remap": True,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    output_path = tmp_path / "adapter-contract-manifest.json"
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        parser_adapter_contract_cli,
        "get_settings",
        lambda: Settings(
            rag_parser_adapter_backend="local",
            rag_parser_docling_enabled=False,
            rag_parser_unstructured_enabled=False,
        ),
    )

    def fake_matrix(settings: Settings, **kwargs: object) -> ParserAdapterCompatibilityMatrix:
        captured["backend"] = settings.rag_parser_adapter_backend
        captured["docling_enabled"] = settings.rag_parser_docling_enabled
        captured["unstructured_enabled"] = settings.rag_parser_unstructured_enabled
        captured.update(kwargs)
        return ParserAdapterCompatibilityMatrix(
            passed=True,
            fixture_root=str(kwargs["fixture_root"]),
            source_kinds=("pdf", "html"),
            backends=("unstructured",),
            case_count=2,
            blocking_failure_count=0,
            cases=(),
        )

    monkeypatch.setattr(
        parser_adapter_contract_cli,
        "run_parser_adapter_compatibility_matrix",
        fake_matrix,
    )

    exit_code = parser_adapter_contract_cli.main(
        [
            "--strict",
            "--manifest",
            str(manifest_path),
            "--backend",
            "unstructured",
            "--output",
            str(output_path),
        ]
    )

    fixture_specs = cast(
        tuple[ParserAdapterFixtureSpec, ...],
        captured["fixture_specs"],
    )
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert captured["backend"] == "docling"
    assert captured["docling_enabled"] is True
    assert captured["unstructured_enabled"] is True
    assert captured["fixture_root"] == fixture_root
    assert captured["require_backend_evidence"] is True
    assert captured["require_selected_adapter_runtime"] is True
    assert [spec.file_name for spec in fixture_specs] == [
        "scanned-contract-ja.pdf",
        "manual.html",
    ]
    assert [spec.case_id for spec in fixture_specs] == [
        "scanned-pdf-ocr-ja",
        "html-semantic-blocks",
    ]
    payload_text = output_path.read_text(encoding="utf-8")
    assert payload["fixture_root"] != str(fixture_root)
    assert payload["fixture_root"].startswith("fixture_root:")
    assert payload["fixture_root_hash"]
    assert str(fixture_root) not in payload_text
    assert "scanned-contract-ja.pdf" not in payload_text
    assert "manual.html" not in payload_text
    assert payload["summary"]["case_count"] == 2


def test_parser_adapter_contract_summary_reports_blocking_failures() -> None:
    """matrix summary は backend/source/status だけで失敗箇所を示す。"""
    matrix = ParserAdapterCompatibilityMatrix(
        passed=False,
        fixture_root="/tmp/fixtures",
        source_kinds=("pdf",),
        backends=("docling",),
        case_count=1,
        blocking_failure_count=1,
        cases=(
            ParserAdapterCompatibilityCase(
                backend="docling",
                source_kind="pdf",
                fixture_name="policy-ja.pdf",
                content_type="application/pdf",
                status="fallback",
                blocking=True,
                scenario="two_column_pdf_reading_order",
                warning_codes=("docling_adapter_failed",),
                reason_codes=("adapter_fallback_used",),
            ),
        ),
    )

    summary = parser_adapter_contract_summary(matrix)

    assert summary["passed"] is False
    assert summary["passed_source_kinds"] == []
    assert summary["missing_source_kinds"] == ["pdf"]
    assert summary["scenarios"] == ["two_column_pdf_reading_order"]
    assert summary["passed_scenarios"] == []
    assert summary["missing_scenarios"] == ["two_column_pdf_reading_order"]
    assert summary["blocking_failure_scenarios"] == ["two_column_pdf_reading_order"]
    assert summary["blocking_failure_source_kinds"] == ["pdf"]
    assert summary["blocking_failure_backends"] == ["docling"]
    assert summary["backend_status_counts"] == {"docling": {"fallback": 1}}
    assert summary["backend_source_status"] == {"docling": {"pdf": "fallback"}}
    assert summary["source_kind_status_counts"] == {"pdf": {"fallback": 1}}
    assert summary["backend_passed_source_kinds"] == {}
    assert summary["backend_passed_scenarios"] == {}
    assert summary["reason_code_counts"] == {"adapter_fallback_used": 1}
    assert summary["warning_code_counts"] == {"docling_adapter_failed": 1}
    assert summary["blocking_failure_reason_counts"] == {"adapter_fallback_used": 1}
    assert summary["blocking_failures"] == [
        {
            "backend": "docling",
            "source_kind": "pdf",
            "status": "fallback",
            "warning_codes": ["docling_adapter_failed"],
            "reason_codes": ["adapter_fallback_used"],
            "scenario": "two_column_pdf_reading_order",
        }
    ]
    assert "policy-ja.pdf" not in json.dumps(summary, ensure_ascii=False)


def test_parser_adapter_contract_summary_aggregates_backend_source_status() -> None:
    """同一 backend/source の一部失敗を最後の passed case で隠さない。"""
    matrix = ParserAdapterCompatibilityMatrix(
        passed=False,
        fixture_root="/tmp/fixtures",
        source_kinds=("pdf",),
        backends=("docling",),
        case_count=2,
        blocking_failure_count=1,
        cases=(
            ParserAdapterCompatibilityCase(
                backend="docling",
                source_kind="pdf",
                fixture_name="two-column-report-ja.pdf",
                content_type="application/pdf",
                status="fallback",
                blocking=True,
                scenario="two_column_pdf_reading_order",
                reason_codes=("adapter_fallback_used",),
            ),
            ParserAdapterCompatibilityCase(
                backend="docling",
                source_kind="pdf",
                fixture_name="duplicate-policy-ja.pdf",
                content_type="application/pdf",
                status="passed",
                blocking=True,
                scenario="duplicate_file_canonical_kb",
                reason_codes=("schema_remap_contract_ok",),
            ),
        ),
    )

    summary = parser_adapter_contract_summary(matrix)

    assert summary["backend_source_status"] == {"docling": {"pdf": "fallback"}}
    assert summary["backend_source_status_counts"] == {
        "docling": {"pdf": {"fallback": 1, "passed": 1}}
    }
    assert summary["passed_source_kinds"] == ["pdf"]
    assert summary["missing_source_kinds"] == []
    assert summary["blocking_failure_source_kinds"] == ["pdf"]
