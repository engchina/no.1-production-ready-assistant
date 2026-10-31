"""parser backend × 対応ファイル形式の宣言(単一正本)。

`registry._external_adapter_supports_source` の if-chain を宣言的データへ抽出したもの。
判定は従来どおり「modality OR content_type OR 拡張子」の OR セマンティクス。

`result.py` と同じ軽量モジュールとして置き、backend / parser microservice / API 公開
(設定画面の対応形式表示)が重い `registry` を import せずに参照できるようにする。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePath

from rag_parser_core.source import SourceModality, SourceProfile
from rag_parser_core.structured_records import STRUCTURED_RECORDS_CONTENT_TYPE

_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
_OPENXML_EXTENSIONS = frozenset({".docx", ".pptx", ".xlsx"})


@dataclass(frozen=True)
class ParserBackendCapability:
    """backend が処理できる原本の宣言。いずれかに一致すれば対応とみなす(OR)。"""

    modalities: frozenset[SourceModality]
    extensions: frozenset[str] = frozenset()
    content_types: frozenset[str] = frozenset()
    content_type_prefixes: frozenset[str] = frozenset()


_IMAGE_ONLY = ParserBackendCapability(
    modalities=frozenset({SourceModality.IMAGE}),
    extensions=_IMAGE_EXTENSIONS,
    content_type_prefixes=frozenset({"image/"}),
)

ADAPTER_CAPABILITIES: Mapping[str, ParserBackendCapability] = {
    # Docling の parser サービス(services/parsers/docling。Docling のレイアウト解析)は
    # PDF と画像だけを解析する(rag_engine.parsing.rendering.SUPPORTED_SOURCE_FILE_TYPES)。
    # Office / HTML / テキスト / メール は受けないため、ファイル準備で PDF に変換する
    # (office_to_pdf)か、文書解析で Unstructured を選ぶ(#286)。
    "docling": ParserBackendCapability(
        modalities=frozenset({SourceModality.PDF, SourceModality.IMAGE}),
        extensions=frozenset({".pdf", ".gif", ".jfif"}) | _IMAGE_EXTENSIONS,
        content_types=frozenset({"application/pdf"}),
        content_type_prefixes=frozenset({"image/"}),
    ),
    "unstructured": ParserBackendCapability(
        # 汎用 partition。UNKNOWN も受ける(従来挙動)。判定は modality だけで決まるため、
        # extensions は UI の拡張子表示用の代表列挙(parser のサービスに入れる extra の対応形式)。
        # .rst / .org はプレーンテキストとして読む。pandoc・pi-heif を入れないため、EPUB・ODT・
        # RTF・HEIC / HEIF は読まない(ODT / RTF はファイル準備の Office→PDF で PDF にする。#3351)。
        modalities=frozenset(
            {
                SourceModality.PDF,
                SourceModality.IMAGE,
                SourceModality.TEXT,
                SourceModality.HTML,
                SourceModality.EMAIL,
                SourceModality.OFFICE,
                SourceModality.UNKNOWN,
            }
        ),
        extensions=(
            frozenset(
                {
                    ".pdf",
                    ".txt",
                    ".md",
                    ".markdown",
                    ".rst",
                    ".org",
                    ".csv",
                    ".tsv",
                    ".json",
                    ".html",
                    ".htm",
                    ".xml",
                    ".eml",
                }
            )
            | _IMAGE_EXTENSIONS
            | _OPENXML_EXTENSIONS
        ),
    ),
    # 構造化レコード JSONL(#2457)。使用エンジンで選んだときだけ使う(形式で自動には回さない)。
    # 前処理(csv_to_jsonl / excel_to_jsonl / word_to_jsonl)の成果物と、契約どおりの .jsonl を
    # 受ける。契約に合わない入力は、解析のサービスが ``structured_records_invalid:<理由>@line<n>``
    # を返して取込を止める。
    "structured_records_jsonl": ParserBackendCapability(
        modalities=frozenset(),
        extensions=frozenset({".jsonl"}),
        content_types=frozenset({STRUCTURED_RECORDS_CONTENT_TYPE}),
    ),
    # XML ツリー JSON(#2024・#2042)。使用エンジンで選んだときだけ使う(形式で自動には回さない)。
    # modality を持たない(source kind の経路の候補に入らない)。対応しない形の JSON は、解析の
    # サービスが ``xml_tree_json_unsupported_tree`` を返して取込を止める。
    "xml_tree_json": ParserBackendCapability(
        modalities=frozenset(),
        extensions=frozenset({".json"}),
        content_types=frozenset({"application/json"}),
    ),
}

# 表示用の modality 並び(SourceModality の定義順。UNKNOWN は表示しない)
_DISPLAY_MODALITY_ORDER: tuple[SourceModality, ...] = (
    SourceModality.PDF,
    SourceModality.IMAGE,
    SourceModality.TEXT,
    SourceModality.HTML,
    SourceModality.EMAIL,
    SourceModality.OFFICE,
)


def _source_extension(source_profile: SourceProfile | None) -> str:
    if source_profile is None:
        return ""
    return (source_profile.extension or PurePath(source_profile.sanitized_file_name).suffix).lower()


def _normalized_content_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().casefold()


# 前処理の構造化レコードの JSONL を受け付ける解析エンジン（#2457）。Docling / Unstructured は
# 受けない（黙って変換せず、構造化レコード JSONL を選ぶよう診断で止める）。
STRUCTURED_RECORDS_BACKENDS = frozenset({"structured_records_jsonl"})


def adapter_supports_source(
    backend: str,
    *,
    source_profile: SourceProfile | None,
    content_type: str,
) -> bool:
    """backend が原本を処理できるかの宣言判定。"""
    capability = ADAPTER_CAPABILITIES.get(backend)
    if capability is None:
        return False
    normalized = _normalized_content_type(
        content_type or (source_profile.content_type if source_profile is not None else "")
    )
    if normalized == STRUCTURED_RECORDS_CONTENT_TYPE:
        # 前処理（csv_to_jsonl / excel_to_jsonl / word_to_jsonl）の構造化レコードの JSONL は、
        # 構造化レコード JSONL の解析エンジンだけが読む（#2457）。
        return backend in STRUCTURED_RECORDS_BACKENDS
    modality = source_profile.modality if source_profile is not None else SourceModality.UNKNOWN
    return (
        modality in capability.modalities
        or normalized in capability.content_types
        or any(normalized.startswith(prefix) for prefix in capability.content_type_prefixes)
        or _source_extension(source_profile) in capability.extensions
    )


def supported_modalities(backend: str) -> tuple[SourceModality, ...]:
    """表示用の対応 modality 一覧(定義順・UNKNOWN 除外)。"""
    capability = ADAPTER_CAPABILITIES.get(backend)
    if capability is None:
        return ()
    return tuple(
        modality for modality in _DISPLAY_MODALITY_ORDER if modality in capability.modalities
    )
