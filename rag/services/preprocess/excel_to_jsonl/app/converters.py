"""Excel(.xls/.xlsx)→構造化レコード JSONL 前処理マイクロサービスの変換実装（#1221・#2457）。

Excel の読み方（表頭の推定・列の役割・手順書・帳票・1 シートの複数の表・図形の文字）は platform の
`pr_backend_core.spreadsheet.read_excel` にある（#2525。チャットの添付なども同じ読み方にする）。
ここでは選択肢を検証して読み、シートごとの記録（`SheetRecordsDocument`）を構造化レコードの JSONL
（`rag_parser_core.structured_records`）にする。parser のサービスはこの JSONL の記録（表の 1 行・
手順書の 1 手順・帳票の 1 項目）を 1 つずつ要素にし、検索の根拠にシートと行の場所が残る。

- 未対応・依存欠如・解析失敗・空・選択肢の誤りのときは passthrough（変換せず原本を使う）へ縮退する。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pr_backend_core.spreadsheet import (
    ExcelOptions,
    openpyxl_available,
    read_excel,
    xlrd_available,
)
from pydantic import ValidationError
from rag_parser_core.preprocess import ConvertOutcome
from rag_parser_core.source import SourceProfile
from rag_parser_core.structured_records import (
    STRUCTURED_RECORDS_CONTENT_TYPE,
    dump_structured_records,
    sheet_records_to_lines,
)

CONVERTER_NAME = "excel_to_jsonl"
# 表の下の注記の行を表にせず注記の記録にした（#3566）ので v9。
CONVERTER_VERSION = "v9"

__all__ = [
    "CONVERTER_NAME",
    "CONVERTER_VERSION",
    "convert",
    "openpyxl_available",
    "read_excel",
    "xlrd_available",
]


def convert(
    source_bytes: bytes,
    content_type: str,
    preprocess_profile: str,
    source_profile: SourceProfile | None,
    *,
    options: Mapping[str, Any] | None = None,
) -> ConvertOutcome:
    """選択プリセットで変換する。対象外・依存欠如・失敗は passthrough へ縮退する。"""
    del content_type
    if preprocess_profile != "excel_to_jsonl":
        return ConvertOutcome.passthrough(
            reason=f"preprocess_unsupported_profile:{preprocess_profile}"
        )
    try:
        parsed = ExcelOptions.model_validate(dict(options or {}))
    except ValidationError:
        return ConvertOutcome.passthrough(reason="excel_options_invalid")
    extension = source_profile.extension if source_profile is not None else None
    result = read_excel(source_bytes, extension=extension, options=parsed)
    if result.document is None:
        return ConvertOutcome.passthrough(reason=result.failure or "excel_parse_failed")
    return ConvertOutcome(
        converted=True,
        converter_name=CONVERTER_NAME,
        converter_version=CONVERTER_VERSION,
        derived_bytes=dump_structured_records(
            sheet_records_to_lines(
                result.document, converter=CONVERTER_NAME, converter_version=CONVERTER_VERSION
            )
        ),
        derived_content_type=STRUCTURED_RECORDS_CONTENT_TYPE,
        warnings=result.warnings,
    )
