"""Ontology 構築資料の安全な保存と決定論的な多形式抽出。"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import csv
import hashlib
import io
import json
import logging
import mimetypes
import threading
import unicodedata
import zipfile
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import UploadFile

from app.settings import Settings, get_settings

from .ontology_models import (
    OntologyEvidence,
    OntologyEvidenceLocatorKind,
    OntologySourceDocument,
    OntologySourceRole,
    QaPair,
)
from .tabular_files import (
    WORKBOOK_SUFFIXES,
    XLS_OLE_SIGNATURE,
    TabularFileReadError,
    normalize_workbook_scalar,
    read_workbook_sheets,
)

SUPPORTED_ONTOLOGY_SOURCE_SUFFIXES = frozenset(
    {".pdf", ".docx", ".txt", ".md", ".csv", ".xlsx", ".xls", ".xlsm"}
)
_CHUNK_SIZE = 1024 * 1024
_QA_QUESTION_KEYS = {"QUESTION", "質問", "TEXT", "PROMPT"}
_QA_SQL_KEYS = {"SQL", "ANSWERSQL", "回答SQL", "正解SQL"}
_ALLOWED_MEDIA_TYPES: dict[str, frozenset[str]] = {
    ".pdf": frozenset({"application/pdf"}),
    ".docx": frozenset({"application/vnd.openxmlformats-officedocument.wordprocessingml.document"}),
    ".txt": frozenset({"text/plain"}),
    ".md": frozenset({"text/markdown", "text/plain"}),
    ".csv": frozenset({"text/csv", "application/csv", "text/plain"}),
    ".xlsx": frozenset({"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}),
    ".xls": frozenset({"application/vnd.ms-excel"}),
    ".xlsm": frozenset({"application/vnd.ms-excel.sheet.macroenabled.12"}),
}


# 以前の保存先(OCI Object Storage。#11 で削除)の資料の参照の接頭辞。読み取れない資料として扱う。
_REMOVED_OBJECT_STORAGE_URI_PREFIX = "oci://"
logger = logging.getLogger(__name__)


class OntologySourceError(ValueError):
    def __init__(self, code: str, message_ja: str) -> None:
        super().__init__(message_ja)
        self.code = code
        self.message_ja = message_ja


@dataclass(frozen=True)
class ExtractedSourceChunk:
    text: str
    locator_kind: OntologyEvidenceLocatorKind
    locator: str

    def evidence(self, source: OntologySourceDocument) -> OntologyEvidence:
        normalized = normalize_source_text(self.text)
        return OntologyEvidence(
            source_document_id=source.id,
            source_sha256=source.sha256,
            locator_kind=self.locator_kind,
            locator=self.locator,
            excerpt_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            excerpt_ja=normalized[:500],
        )


@dataclass(frozen=True)
class ExtractedOntologySource:
    chunks: list[ExtractedSourceChunk]
    qa_pairs: list[QaPair]
    warnings_ja: list[str]

    @property
    def business_text(self) -> str:
        return "\n\n".join(chunk.text for chunk in self.chunks if chunk.text.strip())


def normalize_source_text(value: str) -> str:
    lines = [" ".join(line.split()) for line in unicodedata.normalize("NFC", value).splitlines()]
    return "\n".join(line for line in lines if line).strip()


def decode_source_text(content: bytes) -> str:
    """日本語資料で一般的な encoding を固定順で判定し、置換文字を混入させない。"""

    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return content.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise OntologySourceError(
                "ONTOLOGY_SOURCE_ENCODING_INVALID", "資料の文字コードを判定できません。"
            ) from exc
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise OntologySourceError(
        "ONTOLOGY_SOURCE_ENCODING_INVALID", "資料の文字コードを判定できません。"
    )


def validate_source_signature(filename: str, content: bytes) -> None:
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_ONTOLOGY_SOURCE_SUFFIXES:
        raise OntologySourceError(
            "ONTOLOGY_SOURCE_FORMAT_UNSUPPORTED",
            "PDF、DOCX、TXT、MD、CSV、XLSX、XLS、XLSM のいずれかを指定してください。",
        )
    if not content:
        raise OntologySourceError("ONTOLOGY_SOURCE_EMPTY", "空の資料は登録できません。")
    if suffix == ".pdf" and not content.startswith(b"%PDF-"):
        raise OntologySourceError("ONTOLOGY_SOURCE_MIME_MISMATCH", "PDF の内容を確認できません。")
    if suffix == ".xls" and not content.startswith(XLS_OLE_SIGNATURE):
        raise OntologySourceError(
            "ONTOLOGY_SOURCE_MIME_MISMATCH",
            "拡張子と XLS ファイル内容が一致しません。ファイルを確認して再選択してください。",
        )
    if suffix in {".docx", ".xlsx", ".xlsm"}:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = frozenset(archive.namelist())
        except (OSError, zipfile.BadZipFile) as exc:
            raise OntologySourceError(
                "ONTOLOGY_SOURCE_ARCHIVE_INVALID", "Office ファイルが壊れています。"
            ) from exc
        expected = "word/document.xml" if suffix == ".docx" else "xl/workbook.xml"
        if expected not in names:
            raise OntologySourceError(
                "ONTOLOGY_SOURCE_MIME_MISMATCH", "拡張子と Office ファイル内容が一致しません。"
            )
    if suffix in {".txt", ".md", ".csv"} and (
        b"\x00" in content[:4096]
        or content.startswith((XLS_OLE_SIGNATURE, b"PK\x03\x04", b"%PDF-"))
    ):
        raise OntologySourceError(
            "ONTOLOGY_SOURCE_BINARY_TEXT",
            "テキスト資料の拡張子と内容が一致しません。正しい形式で再選択してください。",
        )


def validate_source_path(filename: str, path: Path) -> None:
    """API のメモリへ原本全体を戻さず、保存済みファイルの magic bytes を検査する。"""

    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_ONTOLOGY_SOURCE_SUFFIXES:
        raise OntologySourceError(
            "ONTOLOGY_SOURCE_FORMAT_UNSUPPORTED",
            "PDF、DOCX、TXT、MD、CSV、XLSX、XLS、XLSM のいずれかを指定してください。",
        )
    if path.stat().st_size == 0:
        raise OntologySourceError("ONTOLOGY_SOURCE_EMPTY", "空の資料は登録できません。")
    with path.open("rb") as stream:
        prefix = stream.read(4096)
    if suffix == ".pdf" and not prefix.startswith(b"%PDF-"):
        raise OntologySourceError("ONTOLOGY_SOURCE_MIME_MISMATCH", "PDF の内容を確認できません。")
    if suffix == ".xls" and not prefix.startswith(XLS_OLE_SIGNATURE):
        raise OntologySourceError(
            "ONTOLOGY_SOURCE_MIME_MISMATCH",
            "拡張子と XLS ファイル内容が一致しません。ファイルを確認して再選択してください。",
        )
    if suffix in {".docx", ".xlsx", ".xlsm"}:
        try:
            with zipfile.ZipFile(path) as archive:
                names = frozenset(archive.namelist())
        except (OSError, zipfile.BadZipFile) as exc:
            raise OntologySourceError(
                "ONTOLOGY_SOURCE_ARCHIVE_INVALID", "Office ファイルが壊れています。"
            ) from exc
        expected = "word/document.xml" if suffix == ".docx" else "xl/workbook.xml"
        if expected not in names:
            raise OntologySourceError(
                "ONTOLOGY_SOURCE_MIME_MISMATCH", "拡張子と Office ファイル内容が一致しません。"
            )
    if suffix in {".txt", ".md", ".csv"} and (
        b"\x00" in prefix or prefix.startswith((XLS_OLE_SIGNATURE, b"PK\x03\x04", b"%PDF-"))
    ):
        raise OntologySourceError(
            "ONTOLOGY_SOURCE_BINARY_TEXT",
            "テキスト資料の拡張子と内容が一致しません。正しい形式で再選択してください。",
        )


def validate_source_media_type(filename: str, media_type: str) -> None:
    normalized = media_type.partition(";")[0].strip().lower()
    if not normalized or normalized == "application/octet-stream":
        return
    suffix = Path(filename).suffix.lower()
    if normalized not in _ALLOWED_MEDIA_TYPES.get(suffix, frozenset()):
        raise OntologySourceError(
            "ONTOLOGY_SOURCE_MIME_MISMATCH",
            "資料の Content-Type と拡張子が一致しません。",
        )


def _unlink_missing_ok(path: Path) -> None:
    path.unlink(missing_ok=True)


def _rmdir_if_empty(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.rmdir()


class OntologySourceStorage:
    """構築資料をローカルのディレクトリ(`local_storage_dir`)に保存する。

    以前の OCI Object Storage に保存した資料(参照が `oci://`)は読み取れない資料として扱い、
    削除は保存の実体が無いものとして済ませる(保存済みの行はそのまま残す。#11)。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def save_upload(
        self,
        *,
        profile_id: str,
        upload: UploadFile,
        source_role: OntologySourceRole = OntologySourceRole.SOURCE,
    ) -> OntologySourceDocument:
        filename = Path(upload.filename or "source").name
        suffix = Path(filename).suffix.lower()
        if suffix not in SUPPORTED_ONTOLOGY_SOURCE_SUFFIXES:
            raise OntologySourceError(
                "ONTOLOGY_SOURCE_FORMAT_UNSUPPORTED",
                "PDF、DOCX、TXT、MD、CSV、XLSX、XLS、XLSM のいずれかを指定してください。",
            )
        source_id = f"ontology_source_{uuid4().hex}"
        profile_storage_key = hashlib.sha256(profile_id.encode("utf-8")).hexdigest()[:24]
        target_dir = Path(self.settings.local_storage_dir).expanduser() / "ontology-sources"
        target_dir = target_dir / profile_storage_key / source_id
        await asyncio.to_thread(target_dir.mkdir, parents=True, exist_ok=True)
        target = target_dir / filename
        digest = hashlib.sha256()
        total = 0
        try:
            stream = await asyncio.to_thread(target.open, "wb")
            try:
                while chunk := await upload.read(_CHUNK_SIZE):
                    total += len(chunk)
                    if total > self.settings.max_upload_bytes:
                        raise OntologySourceError(
                            "ONTOLOGY_SOURCE_TOO_LARGE",
                            "資料がアップロード上限を超えています。",
                        )
                    digest.update(chunk)
                    await asyncio.to_thread(stream.write, chunk)
            finally:
                await asyncio.to_thread(stream.close)
        except Exception:
            await asyncio.to_thread(_unlink_missing_ok, target)
            await asyncio.to_thread(_rmdir_if_empty, target.parent)
            raise
        media_type = (
            upload.content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
        )
        try:
            validate_source_media_type(filename, media_type)
            await asyncio.to_thread(validate_source_path, filename, target)
        except Exception:
            await asyncio.to_thread(_unlink_missing_ok, target)
            await asyncio.to_thread(_rmdir_if_empty, target.parent)
            raise
        storage_uri = str(target)
        return OntologySourceDocument(
            id=source_id,
            profile_id=profile_id,
            filename=filename,
            source_role=source_role,
            media_type=media_type,
            size_bytes=total,
            sha256=digest.hexdigest(),
            storage_uri=storage_uri,
        )

    def load(self, document: OntologySourceDocument) -> bytes:
        if document.storage_uri.startswith(_REMOVED_OBJECT_STORAGE_URI_PREFIX):
            raise OntologySourceError(
                "ONTOLOGY_SOURCE_OBJECT_NOT_FOUND",
                "保存済みの資料が見つかりません。資料をアップロードし直してください。",
            )
        try:
            return Path(document.storage_uri).read_bytes()
        except FileNotFoundError as exc:
            raise OntologySourceError(
                "ONTOLOGY_SOURCE_OBJECT_NOT_FOUND",
                "保存済みの資料が見つかりません。資料をアップロードし直してください。",
            ) from exc
        except OSError as exc:
            logger.warning(
                "ontology_source_local_read_failed",
                exc_info=True,
                extra={"source_document_id": document.id},
            )
            raise OntologySourceError(
                "ONTOLOGY_SOURCE_STORAGE_READ_FAILED",
                "保存済みの資料を読み取れませんでした。資料をアップロードし直してください。",
            ) from exc

    def delete(self, document: OntologySourceDocument) -> None:
        """アップロード実体を削除する(Profile 削除時の cleanup 用)。失敗は握り潰さない。

        以前の Object Storage の参照(`oci://`)は、ローカルに実体が無いので削除済みとして扱う。
        """

        if document.storage_uri.startswith(_REMOVED_OBJECT_STORAGE_URI_PREFIX):
            return
        target = Path(document.storage_uri)
        target.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            target.parent.rmdir()


def extract_ontology_source(
    source: OntologySourceDocument,
    content: bytes,
    *,
    vlm_page_runner: Callable[[bytes, int], str] | None = None,
    vlm_concurrency: int = 1,
) -> ExtractedOntologySource:
    """資料から証拠の断片と Q/A を取り出す。

    PDF の文字の無いページは ``vlm_page_runner`` で OCR する。``vlm_concurrency`` は同時に OCR
    するページの数の上限（#2004。並行でも断片はページの順）。
    """
    validate_source_signature(source.filename, content)
    suffix = Path(source.filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        return _extract_text(content)
    if suffix == ".csv":
        return _extract_delimited(source.filename, content, delimiter=",")
    if suffix in WORKBOOK_SUFFIXES:
        return _extract_workbook(source.filename, content)
    if suffix == ".docx":
        return _extract_docx(content)
    if suffix == ".pdf":
        return _extract_pdf(
            content, vlm_page_runner=vlm_page_runner, vlm_concurrency=vlm_concurrency
        )
    raise OntologySourceError("ONTOLOGY_SOURCE_FORMAT_UNSUPPORTED", "未対応の資料形式です。")


def _extract_text(content: bytes) -> ExtractedOntologySource:
    text = normalize_source_text(decode_source_text(content))
    chunks = [
        ExtractedSourceChunk(line, OntologyEvidenceLocatorKind.LINE, f"line:{index}")
        for index, line in enumerate(text.splitlines(), start=1)
        if line
    ]
    return ExtractedOntologySource(chunks=chunks, qa_pairs=[], warnings_ja=[])


def _header_key(value: str) -> str:
    return value.strip().upper().replace("_", "")


def _qa_header_indexes(headers: list[str]) -> tuple[int, int] | None:
    normalized = [_header_key(value) for value in headers]
    question_index = next(
        (i for i, value in enumerate(normalized) if value in _QA_QUESTION_KEYS), None
    )
    sql_index = next((i for i, value in enumerate(normalized) if value in _QA_SQL_KEYS), None)
    if question_index is None or sql_index is None:
        return None
    return question_index, sql_index


def _qa_pairs_from_rows(rows: list[list[str]]) -> list[QaPair]:
    if not rows:
        return []
    qa_indexes = _qa_header_indexes(rows[0])
    if qa_indexes is None:
        return []
    question_index, sql_index = qa_indexes
    result: list[QaPair] = []
    for row in rows[1:]:
        question = row[question_index].strip() if len(row) > question_index else ""
        sql = row[sql_index].strip() if len(row) > sql_index else ""
        if question and sql and sql.split(None, 1)[0].upper() in {"SELECT", "WITH"}:
            result.append(QaPair(question=question, sql=sql))
    return result


def _column_letter(index: int) -> str:
    """0-origin の列番号を Excel と同じ A/B/.../AA 形式へ変換する。"""

    number = index + 1
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def _tabular_headers(raw_headers: list[str]) -> list[str]:
    headers: list[str] = []
    seen_base: set[str] = set()
    seen_labels: set[str] = set()
    for index, raw_header in enumerate(raw_headers):
        column = _column_letter(index)
        base = normalize_source_text(raw_header) or f"column_{column}"
        label = base
        if base in seen_base:
            label = f"{base}__{column}"
            suffix = 2
            while label in seen_labels:
                label = f"{base}__{column}_{suffix}"
                suffix += 1
        seen_base.add(base)
        seen_labels.add(label)
        headers.append(label)
    return headers


def _row_fields(headers: list[str], row: list[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for index, value in enumerate(row):
        normalized = normalize_source_text(value)
        if not normalized:
            continue
        header = headers[index] if index < len(headers) else f"column_{_column_letter(index)}"
        fields[header] = normalized
    return fields


def _json_line(value: dict[str, Any]) -> str:
    return json.dumps(
        {key: item for key, item in value.items() if item not in ("", None, [], {})},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _qa_jsonl_chunks_from_rows(
    rows: list[list[str]],
    *,
    source_file: str,
    source_type: str,
    sheet: str = "",
    locator_kind: OntologyEvidenceLocatorKind,
) -> list[ExtractedSourceChunk] | None:
    if not rows:
        return None
    qa_indexes = _qa_header_indexes(rows[0])
    if qa_indexes is None:
        return None
    question_index, sql_index = qa_indexes
    headers = _tabular_headers(rows[0])
    chunks: list[ExtractedSourceChunk] = []
    for row_number, row in enumerate(rows[1:], start=2):
        question = row[question_index].strip() if len(row) > question_index else ""
        sql = row[sql_index].strip() if len(row) > sql_index else ""
        if not (question and sql and sql.split(None, 1)[0].upper() in {"SELECT", "WITH"}):
            continue
        block = {
            "source_file": source_file,
            "source_type": source_type,
            "block_type": "qa_pair",
            "sheet": sheet,
            "row_start": row_number,
            "row_end": row_number,
            "question": question,
            "sql": sql,
            "fields": _row_fields(headers, row),
        }
        locator = f"sheet:{sheet};row:{row_number}" if sheet else f"row:{row_number}"
        chunks.append(ExtractedSourceChunk(_json_line(block), locator_kind, locator))
    return chunks


def _extract_delimited(
    filename: str,
    content: bytes,
    *,
    delimiter: str,
) -> ExtractedOntologySource:
    text = decode_source_text(content)
    rows = [
        [normalize_source_text(value) for value in row]
        for row in csv.reader(io.StringIO(text), delimiter=delimiter)
    ]
    qa_chunks = _qa_jsonl_chunks_from_rows(
        rows,
        source_file=filename,
        source_type="csv",
        locator_kind=OntologyEvidenceLocatorKind.QA_ROW,
    )
    if qa_chunks is not None:
        return ExtractedOntologySource(
            chunks=qa_chunks, qa_pairs=_qa_pairs_from_rows(rows), warnings_ja=[]
        )
    chunks = [
        ExtractedSourceChunk(
            json.dumps(row, ensure_ascii=False),
            OntologyEvidenceLocatorKind.QA_ROW if index > 1 else OntologyEvidenceLocatorKind.LINE,
            f"row:{index}",
        )
        for index, row in enumerate(rows, start=1)
        if any(row)
    ]
    return ExtractedOntologySource(
        chunks=chunks, qa_pairs=_qa_pairs_from_rows(rows), warnings_ja=[]
    )


def _extract_workbook(filename: str, content: bytes) -> ExtractedOntologySource:
    try:
        workbook_sheets = read_workbook_sheets(filename, content)
    except TabularFileReadError as exc:
        raise OntologySourceError("ONTOLOGY_SOURCE_EXCEL_INVALID", str(exc)) from exc
    chunks: list[ExtractedSourceChunk] = []
    qa_pairs: list[QaPair] = []
    for sheet in workbook_sheets:
        rows = [
            [normalize_source_text(normalize_workbook_scalar(value)) for value in row]
            for row in sheet.rows
        ]
        qa_pairs.extend(_qa_pairs_from_rows(rows))
        qa_chunks = _qa_jsonl_chunks_from_rows(
            rows,
            source_file=filename,
            source_type="excel",
            sheet=sheet.title,
            locator_kind=OntologyEvidenceLocatorKind.SHEET_ROW,
        )
        if qa_chunks is not None:
            chunks.extend(qa_chunks)
            continue
        chunks.extend(
            ExtractedSourceChunk(
                json.dumps(row, ensure_ascii=False),
                OntologyEvidenceLocatorKind.SHEET_ROW,
                f"sheet:{sheet.title};row:{index}",
            )
            for index, row in enumerate(rows, start=1)
            if any(row)
        )
    return ExtractedOntologySource(chunks=chunks, qa_pairs=qa_pairs, warnings_ja=[])


def _extract_docx(content: bytes) -> ExtractedOntologySource:
    from docx import Document

    try:
        document = Document(io.BytesIO(content))
    except Exception as exc:
        raise OntologySourceError(
            "ONTOLOGY_SOURCE_DOCX_INVALID", "Word 資料を読み取れません。"
        ) from exc
    chunks = [
        ExtractedSourceChunk(
            normalize_source_text(paragraph.text),
            OntologyEvidenceLocatorKind.PARAGRAPH,
            f"paragraph:{index}",
        )
        for index, paragraph in enumerate(document.paragraphs, start=1)
        if normalize_source_text(paragraph.text)
    ]
    for table_index, table in enumerate(document.tables, start=1):
        for row_index, row in enumerate(table.rows, start=1):
            values = [normalize_source_text(cell.text) for cell in row.cells]
            if any(values):
                chunks.append(
                    ExtractedSourceChunk(
                        json.dumps(values, ensure_ascii=False),
                        OntologyEvidenceLocatorKind.SHEET_ROW,
                        f"table:{table_index};row:{row_index}",
                    )
                )
    return ExtractedOntologySource(chunks=chunks, qa_pairs=[], warnings_ja=[])


_OCR_FAILED = "の VLM OCR に失敗したためスキップしました。"

# PDFium はスレッド安全ではない（pypdfium2 の Known limitations「Incompatibility with
# Threading」）。
# 資料の抽出は構築のジョブごとのスレッドで並行に走るため、PDFium の呼び出し（開く・描く・閉じる）は
# process 全体のこの lock の中で行う（#3362）。
_PDFIUM_LOCK = threading.RLock()


def _render_pdf_page_jpeg(document: Any, page_index: int) -> bytes:
    """PDFium で 1 頁を描いて JPEG にする。呼ぶ側が ``_PDFIUM_LOCK`` を持つ。"""
    page = document[page_index]
    try:
        bitmap = page.render(scale=2)
        try:
            image = bitmap.to_pil()
            image_buffer = io.BytesIO()
            image.save(image_buffer, format="JPEG", quality=90)
        finally:
            bitmap.close()
    finally:
        page.close()
    return image_buffer.getvalue()


def _extract_pdf(
    content: bytes,
    *,
    vlm_page_runner: Callable[[bytes, int], str] | None,
    vlm_concurrency: int = 1,
) -> ExtractedOntologySource:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise OntologySourceError(
                "ONTOLOGY_SOURCE_PDF_ENCRYPTED", "暗号化 PDF は使用できません。"
            )
    except OntologySourceError:
        raise
    except Exception as exc:
        raise OntologySourceError(
            "ONTOLOGY_SOURCE_PDF_INVALID", "PDF 資料を読み取れません。"
        ) from exc
    # ページの順の結果（文字・OCR の future・失敗の警告）。OCR は描画した順に投げ、上限つきの
    # 並行で呼ぶ（#2004）。断片・警告はページの順に組み立てる（並行でも順番を変えない）。
    pages: list[tuple[int, str | Future[str] | None, str]] = []
    pdfium_document: Any | None = None
    workers = max(1, int(vlm_concurrency))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ontology-ocr") as pool:
        try:
            for index, page in enumerate(reader.pages, start=1):
                text = normalize_source_text(page.extract_text() or "")
                if not text and vlm_page_runner is not None:
                    try:
                        with _PDFIUM_LOCK:
                            if pdfium_document is None:
                                import pypdfium2  # type: ignore[import-untyped]

                                pdfium_document = pypdfium2.PdfDocument(content)
                            page_image = _render_pdf_page_jpeg(pdfium_document, index - 1)
                    except Exception:
                        pages.append((index, None, _OCR_FAILED))
                        continue
                    pages.append(
                        (
                            index,
                            pool.submit(
                                contextvars.copy_context().run,
                                vlm_page_runner,
                                page_image,
                                index,
                            ),
                            "",
                        )
                    )
                    continue
                pages.append((index, text, ""))
            chunks: list[ExtractedSourceChunk] = []
            warnings: list[str] = []
            skipped_pages: list[str] = []
            for index, value, failure in pages:
                text = ""
                if isinstance(value, Future):
                    try:
                        text = normalize_source_text(value.result())
                    except Exception as exc:
                        # 権限・設定の誤り（呼び出しの全体を止める失敗）は資料の外へ出す。
                        if getattr(exc, "stops_batch", False):
                            raise
                        failure = _OCR_FAILED
                elif isinstance(value, str):
                    text = value
                if failure:
                    # 1 ページの OCR 失敗で資料全体を失敗させない(警告してスキップ)
                    warnings.append(f"PDF page:{index} {failure}")
                    skipped_pages.append(f"page:{index}")
                    continue
                if not text:
                    # テキスト無しページ(画像のみ等)は警告してスキップ。全ページ空のときだけ失敗
                    warnings.append(
                        f"PDF page:{index} はテキストを抽出できないためスキップしました。"
                    )
                    skipped_pages.append(f"page:{index}")
                    continue
                chunks.append(
                    ExtractedSourceChunk(text, OntologyEvidenceLocatorKind.PAGE, f"page:{index}")
                )
        except BaseException:
            for _index, value, _failure in pages:
                if isinstance(value, Future):
                    value.cancel()
            raise
        finally:
            if pdfium_document is not None:
                with _PDFIUM_LOCK:
                    pdfium_document.close()
    if not chunks:
        skipped_detail = f"({', '.join(skipped_pages)})" if skipped_pages else ""
        raise OntologySourceError(
            "ONTOLOGY_SOURCE_PDF_TEXT_MISSING",
            f"PDF のどのページ{skipped_detail}からもテキストを抽出できませんでした。"
            "OCR 設定を確認してください。",
        )
    return ExtractedOntologySource(chunks=chunks, qa_pairs=[], warnings_ja=warnings)
