"""Docling parser マイクロサービス(Docling の解析実装)。

rag_poc の Docling 解析(読み順・段組補正、表セル補修、図内 OCR 集約)を実行し、
`StructuredExtraction` を返す。LayoutRecord 全体は parser_artifacts["layout_records"] に保持する。
図・画像の Vision は backend の解析後の共通の段が行う(#497)。このサービスは LLM を呼ばず、
parser_options は受け取っても使わない(旧 backend の vision_enabled などは無視する)。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from rag_parser_core.registry import run_external_adapter
from rag_parser_core.result import ParseHealth, ParseResponse, service_failure_warning
from rag_parser_core.service import _detect_version, _parse_source_profile
from rag_parser_core.structured_records import is_structured_records_content_type

from app.extraction import analyze_source, read_progress

_BACKEND = "docling"
# 取込の worker が付ける進捗の id(#1999)。パスの一部に使うため形を絞る。
_PROGRESS_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
logger = logging.getLogger(__name__)
# API ドキュメント（/docs・/redoc・/openapi.json）は公開しない（#748）。
app = FastAPI(title="parser-docling", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health", response_model=ParseHealth)
async def health() -> ParseHealth:
    installed, version = _detect_version("docling", ("docling",))
    return ParseHealth(
        status="ok" if installed else "degraded",
        backend=_BACKEND,
        package_name="docling",
        package_version=version,
    )


@app.get("/api/ready", response_model=ParseHealth)
async def ready() -> ParseHealth:
    return await health()


@app.get("/parse/progress/{progress_id}")
async def parse_progress(progress_id: str) -> dict[str, object]:
    """解析中のページの進捗(#1999)。取込の worker が解析の呼び出しの間に読む。

    解析中でない id は 404。内容はページの数と状態だけで、原本の内容を含まない。
    """
    payload = read_progress(progress_id) if _PROGRESS_ID.match(progress_id) else None
    if payload is None:
        raise HTTPException(status_code=404, detail="progress not found")
    return payload


@app.post("/parse", response_model=ParseResponse)
async def parse(
    file: Annotated[UploadFile, File()],
    content_type: Annotated[str, Form()] = "",
    source_profile: Annotated[str | None, Form()] = None,
    parser_options: Annotated[str, Form()] = "",
    progress_id: Annotated[str, Form()] = "",
) -> ParseResponse:
    source_bytes = await file.read()
    profile = _parse_source_profile(source_profile)
    effective_content_type = content_type or (profile.content_type if profile else "")
    file_name = (profile.sanitized_file_name if profile else "") or (file.filename or "")
    _ = parser_options  # 旧 backend が送る Vision の指定(#497 で廃止)は使わない。
    if is_structured_records_content_type(effective_content_type):
        # 前処理(csv_to_jsonl / excel_to_jsonl / word_to_jsonl)の構造化レコードの JSONL は
        # Docling に渡さず、共通の rag_parser_core が契約どおりに記録を要素にする(#2457・#21)。
        result = await asyncio.to_thread(
            run_external_adapter, _BACKEND, source_bytes, profile, effective_content_type
        )
        return ParseResponse.from_result(result)
    _, version = _detect_version("docling", ("docling",))
    try:
        extraction = await asyncio.to_thread(
            analyze_source,
            source_bytes,
            file_name=file_name,
            content_type=effective_content_type,
            progress_id=progress_id if _PROGRESS_ID.match(progress_id) else None,
        )
    except Exception as exc:  # noqa: BLE001 - 縮退せず原因を上流へ返す
        logger.exception("docling parse failed")
        return ParseResponse(
            extraction=None,
            parser_backend=_BACKEND,
            parser_version=version or _BACKEND,
            fallback_used=True,
            template=f"{_BACKEND}_fallback",
            warnings=[service_failure_warning(_BACKEND, exc)],
        )
    return ParseResponse(
        extraction=extraction,
        parser_backend=_BACKEND,
        parser_version=version or _BACKEND,
        template="docling_layout",
        warnings=list(extraction.warnings),
    )
