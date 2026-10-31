"""外部 adapter の失敗の扱いを決定論検証する（fake で adapter の実行を落とす）。

package の実体（docling など）は CI に入れないため、`_external_adapter_package_available` と
adapter の実行を差し替え、warning とログの契約だけを確かめる。
"""

from __future__ import annotations

import logging

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


def test_external_adapter_failure_logs_traceback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(registry, "_external_adapter_package_available", lambda _backend: True)
    monkeypatch.setattr(
        registry,
        "_docling_adapter_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    with caplog.at_level(logging.ERROR, logger="rag_parser_core.registry"):
        result = _external_adapter_result(
            "docling",
            source_bytes=b"%PDF",
            source_profile=_pdf_profile(),
            content_type="application/pdf",
        )

    assert result.extraction is None
    # 例外の型名だけを添え、メッセージ("boom")は warning に載せない(#310)。
    assert result.warnings == ("docling_adapter_failed: RuntimeError",)
    record = next(
        item for item in caplog.records if item.message == "external parser adapter failed"
    )
    assert record.exc_info is not None
    assert record.__dict__["source_sha256"] == "0" * 64
