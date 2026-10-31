"""Office→PDF 前処理マイクロサービスの変換実装。

LibreOffice headless(`soffice`)で Word・PowerPoint の文書(旧形式の .doc / .ppt、マクロ付きの
.docm / .pptm を含む)を PDF へ変換する。表計算(.xls / .xlsx / .xlsm / .ods)は変換しない
(Excel は前処理 excel_to_jsonl で読む。#2470)。LibreOffice は変換ごとに新しいプロファイルで
マクロの実行を無効にして動かす(マクロは実行しない)。LibreOffice はこのサービスにのみ含め、
他 parser / backend に非干渉。未導入・変換失敗・対象外のときは passthrough(変換しない)へ縮退する。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from rag_parser_core.preprocess import ConvertOutcome
from rag_parser_core.source import SourceProfile

_OFFICE_SUFFIX_BY_EXTENSION = {
    "docx": ".docx",
    "doc": ".doc",
    "docm": ".docm",
    "pptx": ".pptx",
    "ppt": ".ppt",
    "pptm": ".pptm",
    "odt": ".odt",
    "odp": ".odp",
    "rtf": ".rtf",
}
# 表計算は変換しない(Excel→JSONL で読む)。
_SPREADSHEET_EXTENSIONS = frozenset({"xls", "xlsx", "xlsm", "ods"})
_SCRIPTING = "/org.openoffice.Office.Common/Security/Scripting"
# 変換ごとに作る LibreOffice の利用者のプロファイル。マクロを実行しない。
_MACRO_DISABLED_REGISTRY = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<oor:items xmlns:oor="http://openoffice.org/2001/registry"'
    ' xmlns:xs="http://www.w3.org/2001/XMLSchema"'
    ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
    f'<item oor:path="{_SCRIPTING}"><prop oor:name="MacroSecurityLevel" oor:op="fuse">'
    "<value>3</value></prop></item>\n"
    f'<item oor:path="{_SCRIPTING}"><prop oor:name="DisableMacrosExecution" oor:op="fuse">'
    "<value>true</value></prop></item>\n"
    "</oor:items>\n"
)


def soffice_path() -> str | None:
    """利用可能な LibreOffice 実行ファイルを返す(未導入なら None)。"""
    return shutil.which("soffice") or shutil.which("libreoffice")


def convert(
    source_bytes: bytes,
    content_type: str,
    preprocess_profile: str,
    source_profile: SourceProfile | None,
) -> ConvertOutcome:
    """選択プリセットで変換する。office_to_pdf 以外・依存欠如・失敗は passthrough へ縮退する。"""
    if preprocess_profile != "office_to_pdf":
        return ConvertOutcome.passthrough(
            reason=f"preprocess_unsupported_profile:{preprocess_profile}"
        )
    return _office_to_pdf(source_bytes, source_profile)


def _extension(source_profile: SourceProfile | None) -> str:
    extension = (source_profile.extension if source_profile is not None else None) or ""
    return extension.strip().lower().lstrip(".")


def _office_suffix(source_profile: SourceProfile | None) -> str:
    return _OFFICE_SUFFIX_BY_EXTENSION.get(_extension(source_profile), ".docx")


def _macro_disabled_profile(root: str) -> str:
    """マクロの実行を無効にした LibreOffice のプロファイルを作り、その URI を返す。"""
    profile = Path(root) / "profile"
    (profile / "user").mkdir(parents=True)
    (profile / "user" / "registrymodifications.xcu").write_text(
        _MACRO_DISABLED_REGISTRY, encoding="utf-8"
    )
    return profile.as_uri()


def _office_to_pdf(source_bytes: bytes, source_profile: SourceProfile | None) -> ConvertOutcome:
    if _extension(source_profile) in _SPREADSHEET_EXTENSIONS:
        return ConvertOutcome.passthrough(reason="office_to_pdf_spreadsheet_not_supported")
    soffice = soffice_path()
    if soffice is None:
        return ConvertOutcome.passthrough(reason="libreoffice_unavailable")
    suffix = _office_suffix(source_profile)
    with tempfile.TemporaryDirectory() as tmp:
        in_path = os.path.join(tmp, f"input{suffix}")
        with open(in_path, "wb") as handle:
            handle.write(source_bytes)
        # soffice は同時実行で profile を奪い合うため、専用 HOME を渡して衝突を避ける。
        env = {**os.environ, "HOME": tmp}
        command = [
            soffice,
            f"-env:UserInstallation={_macro_disabled_profile(tmp)}",
            "--headless",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            tmp,
            in_path,
        ]
        try:
            subprocess.run(
                command,
                check=True,
                capture_output=True,
                timeout=240,
                env=env,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return ConvertOutcome.passthrough(reason="office_to_pdf_failed")
        outputs = [name for name in os.listdir(tmp) if name.lower().endswith(".pdf")]
        if not outputs:
            return ConvertOutcome.passthrough(reason="office_to_pdf_no_output")
        with open(os.path.join(tmp, outputs[0]), "rb") as handle:
            pdf_bytes = handle.read()
    if not pdf_bytes:
        return ConvertOutcome.passthrough(reason="office_to_pdf_empty")
    return ConvertOutcome(
        converted=True,
        converter_name="libreoffice",
        converter_version="v1",
        derived_bytes=pdf_bytes,
        derived_content_type="application/pdf",
    )
