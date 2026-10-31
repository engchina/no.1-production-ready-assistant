"""アップロードで受け付ける形式の宣言を、画面・backend・解析の依存でそろえる(#3351)。

- 画面のファイル選択(frontend の ``upload-selection.ts``)の MIME type は、backend の既定の
  ``allowed_upload_content_types`` と同じ。画面の拡張子は、backend が形式を判定できる
  (415 にしない)。
- HEIC / HEIF・EPUB は解析の依存(pi-heif の libheif・pandoc)をライセンスの理由で入れないので、
  画面と API の両方で変換の方法を添えて止める。音声・動画は取り込まない形式で、同じく止める
  (#3369)。文言は画面と API で同じ。
- Unstructured の解析サービスと backend の任意の extra は、pi-heif・pypandoc-binary を入れない。
"""

from __future__ import annotations

import mimetypes
import re
import tomllib
from pathlib import Path

import pytest
from rag_parser_core.capabilities import ADAPTER_CAPABILITIES
from rag_parser_core.source import SourceModality

from app.config import Settings
from app.rag.source_profile import (
    AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE,
    EPUB_UPLOAD_REJECTED_MESSAGE,
    HEIC_UPLOAD_REJECTED_MESSAGE,
    REJECTED_UPLOAD_EXTENSIONS,
    build_source_profile,
    rejected_upload_format_message,
)

RAG_ROOT = Path(__file__).resolve().parents[2]
UPLOAD_SELECTION = RAG_ROOT / "frontend" / "src" / "lib" / "upload-selection.ts"
I18N = RAG_ROOT / "frontend" / "src" / "lib" / "i18n.ts"
LICENSE_EXCLUDED_PACKAGES = ("pi-heif", "pypandoc-binary")


def _accepted_upload_types() -> list[str]:
    source = UPLOAD_SELECTION.read_text(encoding="utf-8")
    block = re.search(r"ACCEPTED_UPLOAD_TYPES = \[(.*?)\]\.join", source, re.S)
    assert block is not None
    return re.findall(r'"([^"]+)"', block.group(1))


def _rejected_upload_extensions() -> set[str]:
    source = UPLOAD_SELECTION.read_text(encoding="utf-8")
    block = re.search(r"REJECTED_UPLOAD_FORMATS = \{(.*?)\}", source, re.S)
    assert block is not None
    return set(re.findall(r'"(\.[a-z0-9]+)":', block.group(1)))


def test_frontend_mime_types_match_backend_allowed_upload_content_types() -> None:
    frontend = {item.casefold() for item in _accepted_upload_types() if not item.startswith(".")}
    backend = {item.casefold() for item in Settings().allowed_upload_content_types}
    # application/octet-stream は backend が拡張子で判定して受ける(画面の accept には出さない)。
    assert frontend == backend - {"application/octet-stream"}


def test_frontend_extensions_are_recognized_by_backend() -> None:
    """画面が受ける拡張子は、ブラウザが送る MIME type(無ければ octet-stream)で 415 にならない。"""
    allowed = {item.casefold() for item in Settings().allowed_upload_content_types}
    for extension in (item for item in _accepted_upload_types() if item.startswith(".")):
        file_name = f"sample{extension}"
        content_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
        if content_type.casefold() not in allowed:
            content_type = "application/octet-stream"
        profile = build_source_profile(
            original_file_name=file_name,
            sanitized_file_name=file_name,
            content_type=content_type,
            file_size_bytes=1,
            content_sha256="a" * 64,
        )
        assert profile.unsupported_reason != "unknown_file_type", (extension, content_type)
        assert rejected_upload_format_message(file_name, content_type) is None, extension


def test_rejected_formats_match_between_frontend_and_backend() -> None:
    assert _rejected_upload_extensions() == set(REJECTED_UPLOAD_EXTENSIONS)
    accepted = {item.casefold() for item in _accepted_upload_types()}
    assert accepted.isdisjoint(REJECTED_UPLOAD_EXTENSIONS)
    assert accepted.isdisjoint({"image/heic", "image/heif", "application/epub+zip"})
    assert not [item for item in accepted if item.startswith(("audio/", "video/"))]
    assert "application/ogg" not in accepted
    i18n = I18N.read_text(encoding="utf-8")
    # 画面の案内と API の 415 は同じ文言(変換の方法を含む)。
    assert HEIC_UPLOAD_REJECTED_MESSAGE in i18n
    assert EPUB_UPLOAD_REJECTED_MESSAGE in i18n
    assert AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE in i18n
    assert "JPEG / PNG" in HEIC_UPLOAD_REJECTED_MESSAGE
    assert "互換性優先" in HEIC_UPLOAD_REJECTED_MESSAGE


@pytest.mark.parametrize(
    ("file_name", "content_type", "expected"),
    [
        ("a.heic", None, HEIC_UPLOAD_REJECTED_MESSAGE),
        ("a.HEIF", "application/octet-stream", HEIC_UPLOAD_REJECTED_MESSAGE),
        ("a", "image/heic; charset=binary", HEIC_UPLOAD_REJECTED_MESSAGE),
        ("a.epub", None, EPUB_UPLOAD_REJECTED_MESSAGE),
        ("a", "application/epub+zip", EPUB_UPLOAD_REJECTED_MESSAGE),
        ("a.mp3", None, AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE),
        ("a.WAV", "application/octet-stream", AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE),
        ("a.mov", None, AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE),
        ("a", "audio/flac", AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE),
        ("a", "video/webm; codecs=vp9", AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE),
        ("a", "application/ogg", AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE),
        ("a.png", "image/png", None),
        ("a.odt", "application/vnd.oasis.opendocument.text", None),
    ],
    ids=[
        "heic",
        "heif-upper",
        "heic-mime",
        "epub",
        "epub-mime",
        "mp3",
        "wav-upper-octet-stream",
        "mov",
        "audio-mime",
        "video-mime-params",
        "ogg-application",
        "png",
        "odt",
    ],
)
def test_rejected_upload_format_message(
    file_name: str, content_type: str | None, expected: str | None
) -> None:
    assert rejected_upload_format_message(file_name, content_type) == expected


def test_audio_is_not_a_source_modality() -> None:
    """音声は取り込む形式に無い(#3369)。保存済みの音声の原本も未知の形式として解析しない。"""
    profile = build_source_profile(
        original_file_name="voice.mp3",
        sanitized_file_name="voice.mp3",
        content_type="audio/mpeg",
        file_size_bytes=3,
        content_sha256="a" * 64,
    )
    assert "audio" not in {item.value for item in SourceModality}
    assert profile.modality == SourceModality.UNKNOWN
    assert profile.unsupported_reason == "unknown_file_type"


def test_unstructured_capability_lists_plain_text_markup_but_not_removed_formats() -> None:
    """設定画面の Unstructured の対応形式(代表の拡張子)に、読まない形式を出さない。"""
    extensions = ADAPTER_CAPABILITIES["unstructured"].extensions
    assert {".rst", ".org"} <= extensions
    assert extensions.isdisjoint({".heic", ".heif", ".epub", ".odt", ".rtf"})


@pytest.mark.parametrize(
    "project",
    ["services/parsers/unstructured", "backend"],
    ids=["parser-unstructured", "backend"],
)
def test_license_excluded_packages_are_not_installed(project: str) -> None:
    root = RAG_ROOT / project
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    excluded = pyproject["tool"]["uv"]["exclude-dependencies"]
    assert set(LICENSE_EXCLUDED_PACKAGES) <= set(excluded)
    requirements = [
        *pyproject["project"].get("dependencies", []),
        *pyproject["project"].get("optional-dependencies", {}).get("unstructured", []),
    ]
    assert not any("all-docs" in item for item in requirements)
    lock = tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))
    names = {package["name"] for package in lock["package"]}
    assert names.isdisjoint(LICENSE_EXCLUDED_PACKAGES)
