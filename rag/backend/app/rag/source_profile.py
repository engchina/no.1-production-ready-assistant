"""アップロード原本の source profile 作成。"""

import mimetypes
from pathlib import PurePath

from charset_normalizer import from_bytes
from rag_parser_core.source import PLAIN_TEXT_MARKUP_EXTENSIONS, is_plain_text_markup

from app.schemas.document import SourceModality, SourcePreviewKind, SourceProfile

LARGE_FILE_WARNING_BYTES = 50 * 1024 * 1024

# アップロードで受け付けない形式と、利用者に示す変換の方法(#3351)。解析に要る依存
# (HEIC / HEIF は pi-heif の libheif・EPUB は pandoc)をライセンスの理由で入れないため、
# 保存せずに止める。音声・動画は取り込まない形式で、同じく保存せずに止める(#3369)。
# 画面の案内(frontend の upload-selection.ts の REJECTED_UPLOAD_FORMATS と i18n.ts)と
# 同じ形式・文言にする(tests/test_upload_format_contract.py で照合する)。
HEIC_UPLOAD_REJECTED_MESSAGE = (
    "HEIC / HEIF の画像は対応していません。JPEG / PNG に変換してからアップロードしてください"
    "（iPhone は「設定 › カメラ › フォーマット › 互換性優先」で JPEG で保存できます）。"
)
EPUB_UPLOAD_REJECTED_MESSAGE = (
    "EPUB の電子書籍は対応していません。PDF に変換してからアップロードしてください。"
)
AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE = (
    "音声・動画のファイル（MP3・WAV・OGG・MP4 など）は対応していません。"
    "内容をテキストに書き起こしてからアップロードしてください。"
)
# 音声・動画の代表の拡張子。MIME type は audio/* と video/* をまとめて止める。
AUDIO_VIDEO_UPLOAD_EXTENSIONS = (
    ".aac",
    ".aif",
    ".aiff",
    ".amr",
    ".flac",
    ".m4a",
    ".mp3",
    ".oga",
    ".ogg",
    ".opus",
    ".wav",
    ".weba",
    ".wma",
    ".3gp",
    ".avi",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".ogv",
    ".webm",
    ".wmv",
)
REJECTED_UPLOAD_EXTENSIONS: dict[str, str] = {
    ".heic": HEIC_UPLOAD_REJECTED_MESSAGE,
    ".heif": HEIC_UPLOAD_REJECTED_MESSAGE,
    ".epub": EPUB_UPLOAD_REJECTED_MESSAGE,
    **dict.fromkeys(AUDIO_VIDEO_UPLOAD_EXTENSIONS, AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE),
}
REJECTED_UPLOAD_MEDIA_TYPES: dict[str, str] = {
    "image/heic": HEIC_UPLOAD_REJECTED_MESSAGE,
    "image/heif": HEIC_UPLOAD_REJECTED_MESSAGE,
    "image/heic-sequence": HEIC_UPLOAD_REJECTED_MESSAGE,
    "image/heif-sequence": HEIC_UPLOAD_REJECTED_MESSAGE,
    "image/x-heic": HEIC_UPLOAD_REJECTED_MESSAGE,
    "application/epub+zip": EPUB_UPLOAD_REJECTED_MESSAGE,
    # Ogg のコンテナ(音声・動画)。
    "application/ogg": AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE,
}
REJECTED_UPLOAD_MEDIA_TYPE_PREFIXES: dict[str, str] = {
    "audio/": AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE,
    "video/": AUDIO_VIDEO_UPLOAD_REJECTED_MESSAGE,
}

TEXT_MEDIA_TYPES = {
    "application/json",
    "application/jsonl",
    "application/jsonlines",
    "application/ndjson",
    "application/xml",
    "application/csv",
    "application/x-ndjson",
}

TEXT_EXTENSIONS = {
    ".csv",
    ".jsonl",
    ".md",
    ".markdown",
    ".ndjson",
    ".tsv",
    ".txt",
    # reStructuredText・Org はプレーンテキストとして読む(#3351)。
    *PLAIN_TEXT_MARKUP_EXTENSIONS,
}

HTML_MEDIA_TYPES = {
    "text/html",
    "application/xhtml+xml",
}

EMAIL_MEDIA_TYPES = {
    "message/rfc822",
    "application/eml",
}

OUTLOOK_MSG_MEDIA_TYPES = {
    "application/vnd.ms-outlook",
    "application/x-msg",
}

IMAGE_EXTENSIONS = {
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}

TIFF_IMAGE_EXTENSIONS = {
    ".tif",
    ".tiff",
}

TIFF_IMAGE_MEDIA_TYPES = {
    "image/tif",
    "image/tiff",
}

OFFICE_EXTENSIONS = {
    ".doc",
    ".docx",
    ".docm",
    ".ppt",
    ".pptx",
    ".pptm",
    ".xls",
    ".xlsx",
    ".xlsm",
}

LEGACY_OFFICE_EXTENSIONS = {
    ".doc",
    ".ppt",
    ".xls",
}

LEGACY_OFFICE_MEDIA_TYPES = {
    "application/msword",
    "application/vnd.ms-powerpoint",
    "application/vnd.ms-excel",
}

# マクロ付きの OpenXML の Office。原本のままでは解析せず、ファイル準備で変換してから読む
# （マクロは実行しない。旧形式のバイナリと同じ扱い。#2470）。
MACRO_OFFICE_EXTENSIONS = {
    ".docm",
    ".pptm",
    ".xlsm",
}

MACRO_OFFICE_MEDIA_TYPES = {
    "application/vnd.ms-word.document.macroenabled.12",
    "application/vnd.ms-powerpoint.presentation.macroenabled.12",
    "application/vnd.ms-excel.sheet.macroenabled.12",
}

# ODT・RTF。原本のままでは解析せず、ファイル準備の Office→PDF で PDF にしてから読む(#3351。
# unstructured は pandoc で読むが、pandoc は GPL のため入れない)。
ODT_RTF_EXTENSIONS = {
    ".odt",
    ".rtf",
}

ODT_RTF_MEDIA_TYPES = {
    "application/vnd.oasis.opendocument.text",
    "application/rtf",
    "application/x-rtf",
    "text/rtf",
}

OPENXML_OFFICE_EXTENSIONS = {
    ".docx",
    ".pptx",
    ".xlsx",
}

OPENXML_OFFICE_MEDIA_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

WHATWG_LABELS = {
    "cp932": "shift_jis",
    "ms932": "shift_jis",
    "shift-jis": "shift_jis",
    "sjis": "shift_jis",
    "euc-jp": "euc-jp",
    "eucjp": "euc-jp",
    "euc-jis-2004": "euc-jp",
    "euc-jisx0213": "euc-jp",
    "cp936": "gbk",
    "gbk": "gbk",
    "gb2312": "gbk",
    "cp949": "euc-kr",
    "euc-kr": "euc-kr",
    "cp950": "big5",
    "big5hkscs": "big5",
}


def build_source_profile(
    *,
    original_file_name: str,
    sanitized_file_name: str,
    content_type: str | None,
    file_size_bytes: int | None,
    content_sha256: str | None,
    duplicate_of_document_id: str | None = None,
    data: bytes | None = None,
) -> SourceProfile:
    """原本メタデータから source profile を作る。"""
    normalized_content_type = _normalized_content_type(content_type)
    inferred_content_type = _inferred_content_type(sanitized_file_name)
    extension = PurePath(sanitized_file_name).suffix.lower() or None
    modality = _source_modality(normalized_content_type, extension)
    warnings = _quality_warnings(
        content_type=normalized_content_type,
        inferred_content_type=inferred_content_type,
        extension=extension,
        file_size_bytes=file_size_bytes or 0,
        modality=modality,
        duplicate_of_document_id=duplicate_of_document_id,
    )
    text_charset = (
        _detect_text_charset(data)
        if data is not None and _is_text_media_type(normalized_content_type)
        else None
    )
    return SourceProfile(
        original_file_name=original_file_name or sanitized_file_name,
        sanitized_file_name=sanitized_file_name,
        extension=extension,
        content_type=normalized_content_type,
        inferred_content_type=inferred_content_type,
        file_size_bytes=file_size_bytes or 0,
        content_sha256=content_sha256 or "",
        modality=modality,
        parser_profile=_parser_profile(
            modality,
            extension=extension,
            content_type=normalized_content_type,
        ),
        parser_backend=_parser_backend(
            modality,
            extension=extension,
            content_type=normalized_content_type,
        ),
        parser_version="v1",
        preview_kind=_preview_kind(
            modality,
            extension=extension,
            content_type=normalized_content_type,
        ),
        text_charset=text_charset,
        duplicate_of_document_id=duplicate_of_document_id,
        unsupported_reason=_unsupported_reason(
            modality,
            extension=extension,
            content_type=normalized_content_type,
        ),
        quality_status="warning" if warnings else "ready",
        quality_warnings=warnings,
    )


def _normalized_content_type(content_type: str | None) -> str:
    if not content_type:
        return "application/octet-stream"
    return content_type.split(";", maxsplit=1)[0].strip().lower() or "application/octet-stream"


def _inferred_content_type(file_name: str) -> str | None:
    media_type, _ = mimetypes.guess_type(file_name)
    return media_type.lower() if media_type else None


def _source_modality(content_type: str, extension: str | None) -> SourceModality:
    if content_type == "application/pdf" or extension == ".pdf":
        return SourceModality.PDF
    if content_type.startswith("image/") or extension in IMAGE_EXTENSIONS:
        return SourceModality.IMAGE
    if content_type in HTML_MEDIA_TYPES or extension in {".html", ".htm", ".xhtml"}:
        return SourceModality.HTML
    if (
        content_type in EMAIL_MEDIA_TYPES
        or content_type in OUTLOOK_MSG_MEDIA_TYPES
        or extension in {".eml", ".msg"}
    ):
        return SourceModality.EMAIL
    # RTF は text/rtf でも送られるので、テキストより先に Office として判定する。
    if _is_odt_rtf(extension=extension, content_type=content_type):
        return SourceModality.OFFICE
    if (
        _is_text_media_type(content_type)
        or extension in TEXT_EXTENSIONS
        or is_plain_text_markup(extension, content_type)
    ):
        return SourceModality.TEXT
    if extension in OFFICE_EXTENSIONS or content_type in {
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        *MACRO_OFFICE_MEDIA_TYPES,
    }:
        return SourceModality.OFFICE
    return SourceModality.UNKNOWN


def _parser_profile(
    modality: SourceModality,
    *,
    extension: str | None,
    content_type: str,
) -> str:
    if modality == SourceModality.PDF:
        return "enterprise_ai_pdf_layout"
    if modality == SourceModality.IMAGE:
        if _is_unsupported_tiff_image(extension=extension, content_type=content_type):
            return "unsupported_tiff_image"
        return "enterprise_ai_image_ocr"
    if modality == SourceModality.TEXT:
        return "local_text_structure"
    if modality == SourceModality.HTML:
        return "local_html_semantic"
    if modality == SourceModality.EMAIL:
        if _is_outlook_msg(extension=extension, content_type=content_type):
            return "unsupported_outlook_msg"
        return "local_email_thread"
    if modality == SourceModality.OFFICE:
        if _is_macro_office(extension=extension, content_type=content_type):
            return "unsupported_macro_enabled_office"
        if _is_openxml_office(extension=extension, content_type=content_type):
            return "local_office_structure"
        if _is_legacy_office_binary(extension=extension, content_type=content_type):
            return "unsupported_legacy_office_binary"
        if _is_odt_rtf(extension=extension, content_type=content_type):
            return "unsupported_odt_rtf"
        return "enterprise_ai_generic"
    return "enterprise_ai_generic"


def _parser_backend(
    modality: SourceModality,
    *,
    extension: str | None,
    content_type: str,
) -> str:
    """v1 parser registry の既定 backend。"""
    if modality in {SourceModality.TEXT, SourceModality.HTML, SourceModality.EMAIL}:
        if modality == SourceModality.EMAIL and _is_outlook_msg(
            extension=extension,
            content_type=content_type,
        ):
            return "unsupported"
        return "local_partition"
    if modality == SourceModality.IMAGE and _is_unsupported_tiff_image(
        extension=extension,
        content_type=content_type,
    ):
        return "unsupported"
    if modality == SourceModality.OFFICE and _is_macro_office(
        extension=extension,
        content_type=content_type,
    ):
        return "unsupported"
    if modality == SourceModality.OFFICE and _is_openxml_office(
        extension=extension,
        content_type=content_type,
    ):
        return "local_partition"
    if modality == SourceModality.OFFICE and (
        _is_legacy_office_binary(extension=extension, content_type=content_type)
        or _is_odt_rtf(extension=extension, content_type=content_type)
    ):
        return "unsupported"
    return "enterprise_ai"


def _preview_kind(
    modality: SourceModality,
    *,
    extension: str | None,
    content_type: str,
) -> SourcePreviewKind:
    """UI で使う原本 preview 種別。"""
    if modality == SourceModality.PDF:
        return SourcePreviewKind.PDF
    if modality == SourceModality.IMAGE:
        if _is_unsupported_tiff_image(extension=extension, content_type=content_type):
            return SourcePreviewKind.UNSUPPORTED
        return SourcePreviewKind.IMAGE
    if modality == SourceModality.TEXT:
        return SourcePreviewKind.TEXT
    if modality == SourceModality.HTML:
        return SourcePreviewKind.HTML
    if modality == SourceModality.EMAIL:
        if _is_outlook_msg(extension=extension, content_type=content_type):
            return SourcePreviewKind.UNSUPPORTED
        return SourcePreviewKind.EMAIL
    if modality == SourceModality.OFFICE:
        if (
            _is_legacy_office_binary(extension=extension, content_type=content_type)
            or _is_macro_office(extension=extension, content_type=content_type)
            or _is_odt_rtf(extension=extension, content_type=content_type)
        ):
            return SourcePreviewKind.UNSUPPORTED
        return SourcePreviewKind.OFFICE
    return SourcePreviewKind.UNSUPPORTED


def _unsupported_reason(
    modality: SourceModality,
    *,
    extension: str | None,
    content_type: str,
) -> str | None:
    """v1 で明示的に未対応にする入力の理由。"""
    if modality == SourceModality.EMAIL and _is_outlook_msg(
        extension=extension,
        content_type=content_type,
    ):
        return "outlook_msg_not_supported"
    if modality == SourceModality.IMAGE and _is_unsupported_tiff_image(
        extension=extension,
        content_type=content_type,
    ):
        return "tiff_image_not_supported"
    if modality == SourceModality.OFFICE and _is_legacy_office_binary(
        extension=extension,
        content_type=content_type,
    ):
        return "legacy_office_binary_not_supported"
    if modality == SourceModality.OFFICE and _is_macro_office(
        extension=extension,
        content_type=content_type,
    ):
        return "macro_enabled_office_not_supported"
    if modality == SourceModality.OFFICE and _is_odt_rtf(
        extension=extension,
        content_type=content_type,
    ):
        return "odt_rtf_not_supported"
    if modality == SourceModality.UNKNOWN:
        return "unknown_file_type"
    return None


def _quality_warnings(
    *,
    content_type: str,
    inferred_content_type: str | None,
    extension: str | None,
    file_size_bytes: int,
    modality: SourceModality,
    duplicate_of_document_id: str | None,
) -> list[str]:
    warnings: list[str] = []
    if duplicate_of_document_id:
        warnings.append("duplicate_content")
    if content_type == "application/octet-stream":
        warnings.append("content_type_missing")
    elif inferred_content_type and not _content_types_compatible(
        content_type,
        inferred_content_type,
    ):
        warnings.append("content_type_extension_mismatch")
    if file_size_bytes >= LARGE_FILE_WARNING_BYTES:
        warnings.append("large_file")
    if modality == SourceModality.EMAIL and _is_outlook_msg(
        extension=extension,
        content_type=content_type,
    ):
        warnings.append("unsupported_outlook_msg")
    if modality == SourceModality.IMAGE and _is_unsupported_tiff_image(
        extension=extension,
        content_type=content_type,
    ):
        warnings.append("unsupported_tiff_image")
    if modality == SourceModality.OFFICE and _is_legacy_office_binary(
        extension=extension,
        content_type=content_type,
    ):
        warnings.append("unsupported_legacy_office_binary")
    if modality == SourceModality.OFFICE and _is_macro_office(
        extension=extension,
        content_type=content_type,
    ):
        warnings.append("unsupported_macro_enabled_office")
    if modality == SourceModality.OFFICE and _is_odt_rtf(
        extension=extension,
        content_type=content_type,
    ):
        warnings.append("unsupported_odt_rtf")
    if modality == SourceModality.UNKNOWN:
        warnings.append("unknown_modality")
    return warnings


def _content_types_compatible(actual: str, inferred: str) -> bool:
    if actual == inferred:
        return True
    if actual.startswith("text/") and inferred in TEXT_MEDIA_TYPES:
        return True
    if actual.startswith("text/") and inferred in HTML_MEDIA_TYPES:
        return True
    if actual in EMAIL_MEDIA_TYPES and inferred in EMAIL_MEDIA_TYPES:
        return True
    if actual in OUTLOOK_MSG_MEDIA_TYPES and inferred in OUTLOOK_MSG_MEDIA_TYPES:
        return True
    return actual.startswith("image/") and inferred.startswith("image/")


def _is_text_media_type(media_type: str) -> bool:
    return media_type.startswith("text/") or media_type in TEXT_MEDIA_TYPES


def _is_unsupported_tiff_image(*, extension: str | None, content_type: str) -> bool:
    """v1 ではブラウザ preview / Enterprise AI image payload と未整合の TIFF か判定する。"""
    return extension in TIFF_IMAGE_EXTENSIONS or content_type in TIFF_IMAGE_MEDIA_TYPES


def _is_openxml_office(*, extension: str | None, content_type: str) -> bool:
    """標準ライブラリで構造化抽出できる OpenXML Office か判定する。"""
    return extension in OPENXML_OFFICE_EXTENSIONS or content_type in OPENXML_OFFICE_MEDIA_TYPES


def _is_legacy_office_binary(*, extension: str | None, content_type: str) -> bool:
    """v1 local parser が扱えない旧バイナリ Office か判定する。"""
    return extension in LEGACY_OFFICE_EXTENSIONS or content_type in LEGACY_OFFICE_MEDIA_TYPES


def _is_macro_office(*, extension: str | None, content_type: str) -> bool:
    """マクロ付きの OpenXML の Office（.docm / .xlsm / .pptm）か判定する。"""
    return extension in MACRO_OFFICE_EXTENSIONS or content_type in MACRO_OFFICE_MEDIA_TYPES


def _is_odt_rtf(*, extension: str | None, content_type: str) -> bool:
    """ファイル準備の Office→PDF で PDF にしてから読む ODT・RTF か判定する(#3351)。"""
    return extension in ODT_RTF_EXTENSIONS or content_type in ODT_RTF_MEDIA_TYPES


def rejected_upload_format_message(file_name: str, content_type: str | None) -> str | None:
    """受け付けない形式(HEIC / HEIF・EPUB・音声・動画)なら、変換の方法を添えた文言を返す。"""
    extension = PurePath(file_name).suffix.lower()
    if extension in REJECTED_UPLOAD_EXTENSIONS:
        return REJECTED_UPLOAD_EXTENSIONS[extension]
    media_type = _normalized_content_type(content_type)
    if media_type in REJECTED_UPLOAD_MEDIA_TYPES:
        return REJECTED_UPLOAD_MEDIA_TYPES[media_type]
    for prefix, message in REJECTED_UPLOAD_MEDIA_TYPE_PREFIXES.items():
        if media_type.startswith(prefix):
            return message
    return None


def _is_outlook_msg(*, extension: str | None, content_type: str) -> bool:
    """v1 local email parser が扱えない Outlook MSG 形式か判定する。"""
    return extension == ".msg" or content_type in OUTLOOK_MSG_MEDIA_TYPES


def _detect_text_charset(data: bytes) -> str:
    if not data:
        return "utf-8"
    try:
        data.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        pass
    match = from_bytes(data).best()
    if match is None or not match.encoding:
        return "utf-8"
    label = match.encoding.replace("_", "-").lower()
    return WHATWG_LABELS.get(label, label)
