"""選んだ文書解析エンジンで扱えない形式を、取込を始める前に止める判定(#286)。

既定の解析エンジン Docling(parser-docling。Docling のレイアウト解析)は PDF と画像だけを解析する。
それ以外(テキスト・Markdown・CSV・JSON・XML・HTML・Office・メール など)を既定のまま取り込むと、
worker が parser サービスを呼んだ後で失敗する。アップロードの結果と、処理レシピの取込の投入の両方で
この判定を使い、取込を始める前に理由と対処(処理レシピで Unstructured を選ぶ・サービスを
起動する)を返す。

判定しない(取込時の判定に任せる)もの:
- 形式を変えるファイル準備(passthrough 以外): 変換後の形式が解析エンジンに渡る
  (例: Office→PDF)。ただし構造化レコードの JSONL を作るファイル準備
  (CSV→JSONL・Excel→JSONL・Word→JSONL)は、構造化レコード JSONL 以外の解析エンジンでは読めないため、
  ここで止める(#2457)。
- ``source_profile.unsupported_reason`` がある形式(TIFF・Outlook .msg など): 既存の
  「非対応形式」として別に止まる。ただし旧形式の Office(.doc / .xls / .ppt)とマクロ付きの Office
  (.docm / .xlsm / .pptm)、ODT・RTF(.odt / .rtf。#3351)は、原本のままでは解析しないが、
  ファイル準備で変換できる(#2470)。
  変換できるファイル準備を選んでいれば変換後の形式で判定を続け、選んでいなければ「ファイル準備で
  変換を選ぶ」案内で止める(`office_conversion_formats`)。
- 削除済み・未知の解析エンジン: 取込時に ``engine_removed`` として止まる。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePath

from rag_parser_core.capabilities import ADAPTER_CAPABILITIES, adapter_supports_source
from rag_parser_core.structured_records import (
    STRUCTURED_RECORDS_JSONL_BACKEND,
    STRUCTURED_RECORDS_REQUIRES_JSONL_BACKEND_WARNING,
)

from app.clients.parser_service import (
    structured_records_requires_jsonl_backend_message,
    supported_formats_label,
)
from app.config import DEFAULT_PARSER_ADAPTER_BACKEND, Settings
from app.rag.preprocess_strategy import resolve_preprocess_profile
from app.schemas.document import SourceModality, SourceProfile

PARSER_SOURCE_UNSUPPORTED_CODE = "parser_source_unsupported"
# 形式を変えないファイル準備(原本と同じ形式が解析エンジンに渡る)。
_FORMAT_PRESERVING_PREPROCESS = frozenset({"passthrough"})
# 構造化レコードの JSONL を作るファイル準備(解析エンジンは構造化レコード JSONL だけ。#2457)。
STRUCTURED_RECORDS_PREPROCESS = frozenset({"csv_to_jsonl", "excel_to_jsonl", "word_to_jsonl"})
# 原本のままでは解析しない Office(旧形式のバイナリ・マクロ付き・ODT / RTF。source_profile の
# unsupported_reason)と、それを変換できるファイル準備の対応(#2470・#3351)。マクロは実行しない。
CONVERSION_REQUIRED_OFFICE_REASONS = frozenset(
    {
        "legacy_office_binary_not_supported",
        "macro_enabled_office_not_supported",
        "odt_rtf_not_supported",
    }
)
PREPROCESS_REQUIRED_CODE = "office_conversion_required"
# 変換が要る Office の品質の warning(変換した派生物の解析では確認事項にしない)。
_CONVERSION_REQUIRED_WARNINGS = frozenset(
    {"unsupported_legacy_office_binary", "unsupported_macro_enabled_office", "unsupported_odt_rtf"}
)
_OFFICE_CONVERSION_FORMATS: dict[str, frozenset[str]] = {
    # ODT・RTF は Office→PDF だけが変換する(unstructured は pandoc で読むが入れない。#3351)。
    "office_to_pdf": frozenset({".doc", ".docm", ".ppt", ".pptm", ".odt", ".rtf"}),
    "excel_to_jsonl": frozenset({".xls", ".xlsm"}),
    "word_to_jsonl": frozenset({".doc", ".docm"}),
}
# Office→PDF は Word・PowerPoint だけを変換する。表計算は Excel→JSONL で読む(#2470)。
OFFICE_TO_PDF_SPREADSHEET_CODE = "office_to_pdf_spreadsheet_not_supported"
_SPREADSHEET_FORMATS = frozenset({".xls", ".xlsx", ".xlsm", ".ods"})
_SPREADSHEET_CONTENT_TYPES = frozenset(
    {
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-excel.sheet.macroenabled.12",
    }
)
_PREPROCESS_LABELS = {
    "office_to_pdf": "Office→PDF",
    "excel_to_jsonl": "Excel→JSONL",
    "word_to_jsonl": "Word→JSONL",
}
# 拡張子の無い原本は content type で形式を決める。
_OFFICE_FORMAT_BY_CONTENT_TYPE = {
    "application/msword": ".doc",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.ms-powerpoint": ".ppt",
    "application/vnd.ms-word.document.macroenabled.12": ".docm",
    "application/vnd.ms-excel.sheet.macroenabled.12": ".xlsm",
    "application/vnd.ms-powerpoint.presentation.macroenabled.12": ".pptm",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/rtf": ".rtf",
    "application/x-rtf": ".rtf",
    "text/rtf": ".rtf",
}
UNSTRUCTURED_BACKEND = "unstructured"
UNSTRUCTURED_SERVICE_ID = "parser-unstructured"
_BACKEND_LABELS = {
    "docling": "Docling",
    "unstructured": "Unstructured",
    "structured_records_jsonl": "構造化レコード JSONL",
    "xml_tree_json": "XML ツリー JSON",
}
# サービス管理の状態(app.services.status)のうち、Unstructured を使える状態。
_SERVICE_AVAILABLE_STATUSES = frozenset({"running", "degraded", "starting"})


@dataclass(frozen=True)
class ParserSourceBlock:
    """取込を始める前に止める理由(利用者向けの日本語の文言を含む)。"""

    code: str
    backend: str
    file_format: str
    suggested_backend: str | None
    message: str


def effective_parser_backend(settings: Settings) -> str:
    """取込で使う解析エンジン(ingestion の ``_partition_source`` と同じ正規化)。"""
    backend = (
        str(getattr(settings, "rag_parser_adapter_backend", DEFAULT_PARSER_ADAPTER_BACKEND))
        .strip()
        .casefold()
        or DEFAULT_PARSER_ADAPTER_BACKEND
    )
    # 廃止済み baseline 値 local / local_partition は既定エンジンのサービスへ渡る。
    if backend in {"local", "local_partition"}:
        return DEFAULT_PARSER_ADAPTER_BACKEND
    return backend


def parser_backend_label(backend: str) -> str:
    """解析エンジンの表示名(未知の値はそのまま)。"""
    return _BACKEND_LABELS.get(backend, backend)


def _file_format(source_profile: SourceProfile) -> str:
    return source_profile.extension or source_profile.content_type or "不明な形式"


def _supports(backend: str, source_profile: SourceProfile) -> bool:
    return adapter_supports_source(
        backend, source_profile=source_profile, content_type=source_profile.content_type
    )


def _office_format(source_profile: SourceProfile) -> str | None:
    extension = (source_profile.extension or "").casefold()
    if extension:
        return extension
    return _OFFICE_FORMAT_BY_CONTENT_TYPE.get((source_profile.content_type or "").casefold())


def is_spreadsheet(source_profile: SourceProfile) -> bool:
    """表計算(Excel)の原本か。"""
    return is_spreadsheet_file(source_profile.extension or "", source_profile.content_type)


def is_spreadsheet_file(file_name: str, content_type: str | None = None) -> bool:
    """ファイル名(拡張子)か content type が表計算(Excel)の原本か。"""
    suffix = PurePath(file_name).suffix or (file_name if file_name.startswith(".") else "")
    return suffix.casefold() in _SPREADSHEET_FORMATS or (
        (content_type or "").casefold() in _SPREADSHEET_CONTENT_TYPES
    )


def office_to_pdf_spreadsheet_message(source_profile: SourceProfile) -> str:
    """Office→PDF で表計算を取り込もうとしたときの理由と対処。"""
    return (
        f"ファイル準備の「Office→PDF」は Word・PowerPoint だけを変換します"
        f"({_file_format(source_profile)} は対象外です)。"
        "Excel は文書レシピのファイル準備で「Excel→JSONL」を選んでください。"
    )


def requires_office_conversion(source_profile: SourceProfile) -> bool:
    """原本のままでは解析せず、ファイル準備で変換が要る Office か(#2470)。"""
    return source_profile.unsupported_reason in CONVERSION_REQUIRED_OFFICE_REASONS


def preprocess_converts_source(preprocess_profile: str, source_profile: SourceProfile) -> bool:
    """選んだファイル準備が、変換の要る Office(旧形式・マクロ付き・ODT / RTF)を変換できるか。"""
    if not requires_office_conversion(source_profile):
        return False
    office_format = _office_format(source_profile)
    return office_format in _OFFICE_CONVERSION_FORMATS.get(preprocess_profile, frozenset())


def settings_convert_source(settings: Settings, source_profile: SourceProfile) -> bool:
    """設定(文書レシピの実効設定)のファイル準備が、変換の要る Office を変換できるか。"""
    return preprocess_converts_source(resolve_preprocess_profile(settings), source_profile)


def office_conversion_preprocess_labels(source_profile: SourceProfile) -> list[str]:
    """変換の要る Office を変換できるファイル準備の表示名(画面の並び)。"""
    office_format = _office_format(source_profile)
    return [
        _PREPROCESS_LABELS[name]
        for name, formats in _OFFICE_CONVERSION_FORMATS.items()
        if office_format in formats
    ]


def office_conversion_required_message(source_profile: SourceProfile) -> str:
    """変換の要る Office をそのまま取り込もうとしたときの理由と対処。"""
    labels = office_conversion_preprocess_labels(source_profile) or ["Office→PDF"]
    return (
        f"{_file_format(source_profile)} は原本のままでは解析できません"
        "(旧形式の Office・マクロ付きの Office・ODT・RTF)。"
        f"文書レシピのファイル準備で「{'」か「'.join(labels)}」を選んでから取り込んでください"
        "(マクロは実行しません)。"
    )


def converted_source_profile(
    source_profile: SourceProfile | None, *, converted: bool
) -> SourceProfile | None:
    """ファイル準備で変換した派生物を解析するときの source profile(#2470)。

    旧形式・マクロ付きの Office・ODT / RTF の ``unsupported_reason`` は原本の形式の理由なので、
    変換した派生物(PDF・JSONL)の解析では止める理由にしない。変換しなかったときは原本のまま返す。
    """
    if source_profile is None or not converted or not requires_office_conversion(source_profile):
        return source_profile
    warnings = [
        warning
        for warning in source_profile.quality_warnings
        if warning not in _CONVERSION_REQUIRED_WARNINGS
    ]
    return source_profile.model_copy(
        update={
            "unsupported_reason": None,
            "quality_warnings": warnings,
            "quality_status": "warning" if warnings else "ready",
        }
    )


def parser_source_block(
    settings: Settings,
    source_profile: SourceProfile,
    *,
    unstructured_service_status: str | None = None,
) -> ParserSourceBlock | None:
    """選んだ解析エンジン(既定は Docling)で扱えない形式なら、止める理由を返す。

    ``unstructured_service_status`` はサービス管理の状態(``app.services.status``)。
    渡すと、Unstructured の案内に「未配備」「停止中」を添える。
    """
    preprocess_profile = resolve_preprocess_profile(settings)
    if preprocess_profile == "office_to_pdf" and is_spreadsheet(source_profile):
        return ParserSourceBlock(
            code=OFFICE_TO_PDF_SPREADSHEET_CODE,
            backend=effective_parser_backend(settings),
            file_format=_file_format(source_profile),
            suggested_backend=None,
            message=office_to_pdf_spreadsheet_message(source_profile),
        )
    if requires_office_conversion(source_profile):
        if not preprocess_converts_source(preprocess_profile, source_profile):
            return ParserSourceBlock(
                code=PREPROCESS_REQUIRED_CODE,
                backend=effective_parser_backend(settings),
                file_format=_file_format(source_profile),
                suggested_backend=None,
                message=office_conversion_required_message(source_profile),
            )
    elif source_profile.unsupported_reason:
        return None
    if preprocess_profile in STRUCTURED_RECORDS_PREPROCESS:
        backend = effective_parser_backend(settings)
        if backend == STRUCTURED_RECORDS_JSONL_BACKEND:
            return None
        return ParserSourceBlock(
            code=STRUCTURED_RECORDS_REQUIRES_JSONL_BACKEND_WARNING,
            backend=backend,
            file_format=_file_format(source_profile),
            suggested_backend=STRUCTURED_RECORDS_JSONL_BACKEND,
            message=structured_records_requires_jsonl_backend_message(
                parser_backend_label(backend)
            ),
        )
    if preprocess_profile not in _FORMAT_PRESERVING_PREPROCESS:
        return None
    backend = effective_parser_backend(settings)
    if backend not in ADAPTER_CAPABILITIES:
        return None
    if _supports(backend, source_profile):
        return None
    suggested = (
        UNSTRUCTURED_BACKEND
        if backend != UNSTRUCTURED_BACKEND and _supports(UNSTRUCTURED_BACKEND, source_profile)
        else None
    )
    file_format = _file_format(source_profile)
    return ParserSourceBlock(
        code=PARSER_SOURCE_UNSUPPORTED_CODE,
        backend=backend,
        file_format=file_format,
        suggested_backend=suggested,
        message=parser_source_block_message(
            backend,
            file_format=file_format,
            office=source_profile.modality == SourceModality.OFFICE,
            spreadsheet=is_spreadsheet(source_profile),
            suggested_backend=suggested,
            unstructured_service_status=unstructured_service_status,
        ),
    )


def parser_source_block_message(
    backend: str,
    *,
    file_format: str,
    office: bool,
    spreadsheet: bool = False,
    suggested_backend: str | None,
    unstructured_service_status: str | None,
) -> str:
    """利用者向けの理由と対処(画面名はナビ・処理レシピの表記に合わせる)。"""
    label = parser_backend_label(backend)
    # 全角括弧の後は空けない(「Docling（既定の解析エンジン）は」/「Unstructured は」)。
    default_note = "（既定の解析エンジン）" if backend == DEFAULT_PARSER_ADAPTER_BACKEND else " "
    formats = supported_formats_label(backend)
    formats_note = f"{label} が解析できるのは {formats}です。" if formats else ""
    parts = [
        f"文書解析エンジン {label}{default_note}はこのファイル形式（{file_format}）を"
        f"解析できないため、取込を開始しませんでした。{formats_note}"
    ]
    if suggested_backend == UNSTRUCTURED_BACKEND:
        parts.append(
            "この文書の「処理レシピ」で「文書解析」を Unstructured に変えてから"
            "「処理を開始」してください。"
        )
        parts.append(unstructured_service_hint(unstructured_service_status))
    else:
        parts.append(
            "対応形式に変換するか、この文書の「処理レシピ」で別の文書解析エンジンを選んでください。"
        )
    if office and spreadsheet:
        # Office→PDF は Excel を変換しない(#2470)。
        parts.append(
            "Excel は「処理レシピ」の「ファイル準備」で Excel→JSONL を選ぶと、"
            "表の行を記録にして構造化レコード JSONL で解析できます。"
        )
    elif office and backend == "docling":
        parts.append(
            "Word・PowerPoint の文書は「処理レシピ」の「ファイル準備」で Office→PDF を選ぶと、"
            "PDF に変換してから Docling で解析できます。"
        )
    return "".join(part for part in parts if part)


def unstructured_service_hint(status: str | None) -> str:
    """Unstructured の解析サービス(parser-unstructured)の状態に応じた案内。"""
    if status is None or status in _SERVICE_AVAILABLE_STATUSES:
        return ""
    if status == "not_installed":
        return (
            "Unstructured の解析サービス（parser-unstructured）は配備されていません。"
            "本番は Terraform の stack で Unstructured parser（rag_enable_parser_unstructured）を"
            "有効にして再配備し、開発は rag/scripts/rag-services.sh install parser-unstructured で"
            "登録してから、「運用設定 › サービス管理」で起動してください。"
        )
    if status == "unconfigured":
        return (
            "Unstructured の解析サービスの接続先（RAG_PARSER_UNSTRUCTURED_SERVICE_URL）が"
            "未設定です。"
        )
    return (
        "Unstructured の解析サービス（parser-unstructured）は停止しています。"
        "「運用設定 › サービス管理」で Unstructured を起動してください。"
    )


async def check_parser_source(
    settings: Settings, source_profile: SourceProfile
) -> ParserSourceBlock | None:
    """``parser_source_block`` に、Unstructured のサービスの状態(未配備・停止中)を添えて返す。"""
    block = parser_source_block(settings, source_profile)
    if block is None or block.suggested_backend != UNSTRUCTURED_BACKEND:
        return block
    status = await _unstructured_service_status(settings)
    return parser_source_block(settings, source_profile, unstructured_service_status=status)


async def _unstructured_service_status(settings: Settings) -> str | None:
    """サービス管理と同じ判定(systemctl show + /health)で parser-unstructured の状態を返す。"""
    from app.services.catalog import get_catalog_entry
    from app.services.status import probe_service_status

    entry = get_catalog_entry(UNSTRUCTURED_SERVICE_ID)
    if entry is None:
        return None
    try:
        return str(await probe_service_status(settings, entry))
    except Exception:  # noqa: BLE001 - 状態は案内の補足。取れなければ添えない。
        return None
