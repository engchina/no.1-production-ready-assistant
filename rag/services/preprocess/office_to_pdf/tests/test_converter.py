"""Office→PDF 変換の決定論・縮退検証。

LibreOffice(soffice)は CI に無いことがあるため、実変換ではなく profile 振り分けと
依存欠如時の passthrough 縮退を検証する(soffice_path を monkeypatch して決定論化)。
"""

from __future__ import annotations

import app.converters as converters
from app.converters import _office_suffix, convert


def test_non_office_profile_passes_through() -> None:
    outcome = convert(b"\x00data", "application/octet-stream", "csv_to_jsonl", None)
    assert outcome.converted is False
    assert any("preprocess_unsupported_profile" in w for w in outcome.warnings)


def test_default_suffix_is_docx() -> None:
    assert _office_suffix(None) == ".docx"


def test_convert_without_libreoffice_passes_through(monkeypatch) -> None:
    monkeypatch.setattr(converters, "soffice_path", lambda: None)
    outcome = convert(b"\x00doc", "application/vnd.openxmlformats", "office_to_pdf", None)
    assert outcome.converted is False
    assert "libreoffice_unavailable" in outcome.warnings


def test_passthrough_is_deterministic(monkeypatch) -> None:
    monkeypatch.setattr(converters, "soffice_path", lambda: None)
    source = b"\x00doc"
    first = convert(source, "application/octet-stream", "office_to_pdf", None)
    second = convert(source, "application/octet-stream", "office_to_pdf", None)
    assert first.converted is second.converted is False
    assert first.derived_bytes == second.derived_bytes


def _profile(extension: str):
    from types import SimpleNamespace

    return SimpleNamespace(extension=extension)


def test_spreadsheet_is_not_converted(monkeypatch) -> None:
    # Office→PDF は Word・PowerPoint だけ。Excel は Excel→JSONL で読む(#2470)。
    monkeypatch.setattr(converters, "soffice_path", lambda: "/usr/bin/soffice")
    for extension in (".xls", ".xlsx", ".xlsm", ".ods"):
        outcome = convert(b"\x00", "application/octet-stream", "office_to_pdf", _profile(extension))
        assert outcome.converted is False
        assert "office_to_pdf_spreadsheet_not_supported" in outcome.warnings


def test_legacy_and_macro_word_powerpoint_keep_suffix() -> None:
    for extension in (".doc", ".docm", ".ppt", ".pptm"):
        assert _office_suffix(_profile(extension)) == extension


def test_libreoffice_runs_with_macro_disabled_profile(monkeypatch) -> None:
    from pathlib import Path

    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        profile = Path(command[1].split("=", 1)[1].removeprefix("file://"))
        registry = (profile / "user" / "registrymodifications.xcu").read_text(encoding="utf-8")
        assert "DisableMacrosExecution" in registry and "<value>true</value>" in registry
        outdir = Path(command[command.index("--outdir") + 1])
        (outdir / "input.pdf").write_bytes(b"%PDF-1.4 synthetic")

    monkeypatch.setattr(converters, "soffice_path", lambda: "/usr/bin/soffice")
    monkeypatch.setattr(converters.subprocess, "run", fake_run)
    outcome = convert(b"\xd0\xcf\x11\xe0", "application/msword", "office_to_pdf", _profile(".doc"))
    assert outcome.converted is True
    assert outcome.derived_content_type == "application/pdf"
    assert calls[0][1].startswith("-env:UserInstallation=file://")
    assert calls[0][-1].endswith("input.doc")
