"""GPU OCR の remap 層を fake SDK module で決定論検証する。

GPU は CI 非搭載のため、実 OCR(GPU シーム)が呼ぶ SDK を fake module へ差し替え、
出力(markdown / 要素)が `StructuredExtraction` へ正しく
再マップされることだけを検証する。実 GPU 実行は手動 integration で確認する。
"""

from __future__ import annotations

import importlib
import logging
import sys
import types
from pathlib import Path

import pytest

from rag_parser_core import registry
from rag_parser_core.registry import _external_adapter_result
from rag_parser_core.source import SourceModality, SourceProfile


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


@pytest.mark.parametrize(
    ("backend", "module_name", "entry"),
    [
        ("dots_ocr", "dots_ocr", "parse"),
    ],
)
def test_ocr_engine_markdown_remaps_to_structured_extraction(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    module_name: str,
    entry: str,
) -> None:
    fake = types.ModuleType(module_name)
    setattr(fake, entry, lambda _path: "# 請求書\n\n合計 1,200 円")
    monkeypatch.setitem(sys.modules, module_name, fake)

    result = _external_adapter_result(
        backend,
        source_bytes=b"%PDF-1.4 scanned",
        source_profile=_pdf_profile(),
        content_type="application/pdf",
    )

    assert result.parser_backend == backend
    assert result.fallback_used is False
    assert result.extraction is not None
    assert "請求書" in result.extraction.raw_text
    assert result.extraction.parser_artifacts["external_adapter"] == backend
    assert result.extraction.parser_artifacts["ocr_engine"] is True
    assert any(
        element.source_parser == f"{backend}_adapter" for element in result.extraction.elements
    )


def test_ocr_engine_without_entry_point_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = types.ModuleType("dots_ocr")  # エントリポイント無し
    monkeypatch.setitem(sys.modules, "dots_ocr", fake)

    result = _external_adapter_result(
        "dots_ocr",
        source_bytes=b"%PDF-1.4 scanned",
        source_profile=_pdf_profile(),
        content_type="application/pdf",
    )

    assert result.extraction is None
    assert result.fallback_used is True
    # 失敗の warning には例外の型名が付く(#310)。code は ``:`` の前。
    assert [w.partition(":")[0] for w in result.warnings] == ["dots_ocr_adapter_failed"]


def test_mineru_is_not_run_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """MinerU は外部の HTTP API だけで呼び、package の中では import・実行しない(#3359)。"""
    calls: list[str] = []

    def parse_document(path: str) -> str:
        calls.append(path)
        return "# 本文"

    fake = types.ModuleType("mineru")
    fake.parse_document = parse_document  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "mineru", fake)

    result = _external_adapter_result(
        "mineru",
        source_bytes=b"%PDF-1.4 scanned",
        source_profile=_pdf_profile(),
        content_type="application/pdf",
    )

    assert calls == []
    assert result.extraction is None
    assert result.fallback_used is True
    assert result.warnings == ("mineru_adapter_unsupported",)


def test_dots_ocr_vllm_parser_reads_generated_markdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry._DOTS_OCR_VLLM_PARSER_CACHE.clear()

    class FakeDotsOCRParser:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["use_hf"] is False
            # vLLM をイメージへ内包したため既定は同一コンテナ内 localhost。
            assert kwargs["ip"] == "127.0.0.1"
            assert kwargs["model_name"] == "model"

        def parse_file(
            self,
            _input_path: str,
            *,
            output_dir: str,
            prompt_mode: str,
            fitz_preprocess: bool,
        ) -> list[dict[str, str]]:
            assert prompt_mode == "prompt_layout_all_en"
            assert fitz_preprocess is True
            md_path = Path(output_dir) / "doc" / "doc.md"
            md_path.parent.mkdir(parents=True)
            md_path.write_text("# Dots OCR\n", encoding="utf-8")
            return [{"md_content_path": str(md_path)}]

    fake_parser_module = types.ModuleType("dots_ocr.parser")
    fake_parser_module.__dict__["DotsOCRParser"] = FakeDotsOCRParser
    monkeypatch.setitem(sys.modules, "dots_ocr.parser", fake_parser_module)
    monkeypatch.setattr(importlib, "import_module", lambda name: fake_parser_module)

    source = tmp_path / "source.png"
    source.write_bytes(b"png")

    assert registry._run_dots_ocr_parser(source) == "# Dots OCR\n"


def test_dots_ocr_hf_parser_reads_generated_markdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry._DOTS_OCR_PARSER_CACHE.clear()
    monkeypatch.setenv("DOTS_OCR_RUNTIME", "hf_explicit_cuda")

    class FakeDotsOCRParser:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["use_hf"] is True

        def parse_file(
            self,
            _input_path: str,
            *,
            output_dir: str,
            prompt_mode: str,
            fitz_preprocess: bool,
        ) -> list[dict[str, str]]:
            assert prompt_mode == "prompt_layout_all_en"
            assert fitz_preprocess is True
            md_path = Path(output_dir) / "doc" / "doc.md"
            md_path.parent.mkdir(parents=True)
            md_path.write_text("# Dots OCR\n", encoding="utf-8")
            return [{"md_content_path": str(md_path)}]

    fake_parser_module = types.ModuleType("dots_ocr.parser")
    fake_parser_module.__dict__["DotsOCRParser"] = FakeDotsOCRParser
    monkeypatch.setitem(sys.modules, "dots_ocr.parser", fake_parser_module)
    monkeypatch.setattr(importlib, "import_module", lambda name: fake_parser_module)

    source = tmp_path / "source.png"
    source.write_bytes(b"png")

    assert registry._run_dots_ocr_parser(source) == "# Dots OCR\n"


def test_external_adapter_failure_logs_traceback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(registry, "_external_adapter_package_available", lambda _backend: True)
    monkeypatch.setattr(
        registry,
        "_dots_ocr_adapter_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with caplog.at_level(logging.ERROR, logger="rag_parser_core.registry"):
        result = _external_adapter_result(
            "dots_ocr",
            source_bytes=b"%PDF",
            source_profile=_pdf_profile(),
            content_type="application/pdf",
        )

    assert result.extraction is None
    # 例外の型名だけを添え、メッセージ("boom")は warning に載せない(#310)。
    assert result.warnings == ("dots_ocr_adapter_failed: RuntimeError",)
    record = next(
        item for item in caplog.records if item.message == "external parser adapter failed"
    )
    assert record.exc_info is not None
    assert record.__dict__["source_sha256"] == "0" * 64
