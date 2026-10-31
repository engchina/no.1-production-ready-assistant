"""全ファイル解析結果を後続チャンキング用入力として保存・復元する。"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rag_engine.knowledge.classification import classification_from_metadata
from rag_engine.knowledge.document_metadata import normalize_document_metadata
from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.coordinates import clamp_bbox

PARSE_INPUT_SCHEMA_VERSION = 1
PARSE_INPUT_DIRECTORY = "parse_inputs"


@dataclass
class RestoredParseInput:
    """保存済み全ファイル解析入力から復元した engine 別データを保持します。"""

    engine_id: str
    engine_label: str
    completed_at: str
    path: Path
    records: list[LayoutRecord]
    classification: dict[str, Any] = field(default_factory=dict)
    use_docling_vision: bool = False
    document_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseInputRestoreResult:
    """復元できた解析入力、警告、利用可能 engine を保持します。"""

    inputs: list[RestoredParseInput] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ParseInputValidationError(ValueError):
    """保存済み parse input が現在のファイルと一致しない状態を表します。"""

    pass


def filename_key(file_name: str) -> str:
    """分割パイプラインで使う大文字小文字を区別した安定 ID を返します。"""
    normalized_name = unicodedata.normalize("NFC", Path(file_name).name)
    return hashlib.sha256(normalized_name.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str:
    """ファイル内容の SHA-256 digest を返します。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_input_path(output_dir: str | Path, file_name: str, engine_id: str) -> Path:
    """ファイル名と engine ID から parse input 保存パスを返します。"""
    return Path(output_dir) / PARSE_INPUT_DIRECTORY / filename_key(file_name) / f"{engine_id}.json"


def load_parse_inputs(
    *,
    output_dir: str | Path,
    source_path: str | Path,
    page_count: int,
    pages: list[PageImage],
    engine_order: list[str],
) -> ParseInputRestoreResult:
    """壊れた engine の保存結果で他 engine の復元を止めず、有効な全ファイル解析結果を読み込みます。"""
    source = Path(source_path)
    directory = Path(output_dir) / PARSE_INPUT_DIRECTORY / filename_key(source.name)
    if not directory.exists():
        return ParseInputRestoreResult()

    try:
        paths = list(directory.glob("*.json"))
    except OSError as exc:
        return ParseInputRestoreResult(warnings=[f"保存済み解析結果の一覧を読み込めませんでした: {exc}"])
    if not paths:
        return ParseInputRestoreResult()

    priority = {engine_id: index for index, engine_id in enumerate(engine_order)}
    paths.sort(key=lambda path: (priority.get(path.stem, len(priority)), path.stem))
    try:
        source_digest = file_sha256(source)
    except OSError as exc:
        return ParseInputRestoreResult(warnings=[f"アップロードしたファイルを照合できませんでした: {exc}"])

    current_pages = {page.page: page for page in pages}
    allowed_engines = set(engine_order)
    restored: list[RestoredParseInput] = []
    warnings: list[str] = []
    for path in paths:
        engine_hint = path.stem
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            loaded = _restore_parse_input(
                payload,
                path=path,
                source_name=source.name,
                source_sha256=source_digest,
                page_count=page_count,
                current_pages=current_pages,
                allowed_engines=allowed_engines,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            warnings.append(f"{engine_hint} の保存済み解析結果をスキップしました: {exc}")
            continue
        restored.append(loaded)
    return ParseInputRestoreResult(inputs=restored, warnings=warnings)


def save_parse_input(
    *,
    output_dir: str | Path,
    source_path: str | Path,
    source_sha256: str,
    page_count: int,
    engine_id: str,
    engine_label: str,
    pages: list[PageImage],
    records: list[LayoutRecord],
    dpi: int,
    min_confidence: float,
    use_docling_vision: bool,
    classification: Any = None,
    document_metadata: dict[str, Any] | None = None,
) -> Path:
    """1 ファイル名・1 engine の全ファイル解析入力を原子的に置き換えます。"""
    source = Path(source_path)
    target = parse_input_path(output_dir, source.name, engine_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    engine_records = sorted(
        (record for record in records if record.engine == engine_id),
        key=lambda record: (record.page, record.seq_no),
    )
    payload: dict[str, Any] = {
        "schema_version": PARSE_INPUT_SCHEMA_VERSION,
        "document": {
            "filename_key": filename_key(source.name),
            "file_name": source.name,
            "source_sha256": source_sha256,
            "page_count": page_count,
            "classification": classification_from_metadata(classification).to_metadata(),
            "metadata": normalize_document_metadata(document_metadata, source_file_name=source.name),
        },
        "parse": {
            "engine_id": engine_id,
            "engine_label": engine_label,
            "scope": "whole_file",
            "completed_at": datetime.now(UTC).isoformat(),
            "options": {
                "dpi": dpi,
                "min_confidence": min_confidence,
                "use_docling_vision": use_docling_vision,
            },
            "record_count": len(engine_records),
        },
        "pages": [
            {
                "page": page.page,
                "width": page.width,
                "height": page.height,
                "pdf_width": page.pdf_width,
                "pdf_height": page.pdf_height,
            }
            for page in sorted(pages, key=lambda page: page.page)
        ],
        "records": [record.to_dict() for record in engine_records],
    }
    _atomic_write_json(target, payload)
    return target


def _restore_parse_input(
    payload: Any,
    *,
    path: Path,
    source_name: str,
    source_sha256: str,
    page_count: int,
    current_pages: dict[int, PageImage],
    allowed_engines: set[str],
) -> RestoredParseInput:
    if not isinstance(payload, dict):
        raise ParseInputValidationError("JSON のルートがオブジェクトではありません。")
    if payload.get("schema_version") != PARSE_INPUT_SCHEMA_VERSION:
        raise ParseInputValidationError("対応していない schema_version です。")

    document = _required_dict(payload, "document")
    expected_key = filename_key(source_name)
    if document.get("filename_key") != expected_key or filename_key(str(document.get("file_name") or "")) != expected_key:
        raise ParseInputValidationError("ファイル名が一致しません。")
    if document.get("source_sha256") != source_sha256:
        raise ParseInputValidationError("アップロードしたファイルの内容と一致しません。")
    if _required_int(document, "page_count", minimum=1) != page_count:
        raise ParseInputValidationError("ページ数が一致しません。")
    classification = classification_from_metadata(document.get("classification")).to_metadata()

    parse = _required_dict(payload, "parse")
    engine_id = _required_string(parse, "engine_id")
    if engine_id != path.stem:
        raise ParseInputValidationError("engine ID とファイル名が一致しません。")
    if engine_id not in allowed_engines:
        raise ParseInputValidationError("現在サポートされていない engine です。")
    if parse.get("scope") != "whole_file":
        raise ParseInputValidationError("ファイル全体の解析結果ではありません。")
    engine_label = _required_string(parse, "engine_label")
    completed_at = _required_string(parse, "completed_at")
    options = parse.get("options") or {}
    if not isinstance(options, dict):
        raise ParseInputValidationError("options がオブジェクトではありません。")
    use_docling_vision = _optional_bool(options, "use_docling_vision", default=False)
    try:
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ParseInputValidationError("保存日時が不正です。") from exc
    if completed.tzinfo is None:
        raise ParseInputValidationError("保存日時にタイムゾーンがありません。")

    stored_pages = _stored_page_dimensions(payload.get("pages"), page_count)
    records_payload = payload.get("records")
    if not isinstance(records_payload, list):
        raise ParseInputValidationError("records が配列ではありません。")
    declared_count = _required_int(parse, "record_count", minimum=0)
    if declared_count != len(records_payload):
        raise ParseInputValidationError("record_count と records 件数が一致しません。")

    records: list[LayoutRecord] = []
    seen_ids: set[str] = set()
    for index, record_payload in enumerate(records_payload, start=1):
        record = _restore_record(
            record_payload,
            engine_id=engine_id,
            stored_pages=stored_pages,
            current_pages=current_pages,
            record_index=index,
        )
        if record.id in seen_ids:
            raise ParseInputValidationError(f"records[{index}] の id が重複しています。")
        seen_ids.add(record.id)
        records.append(record)
    records.sort(key=lambda record: (record.page, record.seq_no))
    return RestoredParseInput(
        engine_id=engine_id,
        engine_label=engine_label,
        completed_at=completed_at,
        path=path,
        records=records,
        classification=classification,
        document_metadata=normalize_document_metadata(document.get("metadata"), source_file_name=source_name),
        use_docling_vision=use_docling_vision,
    )


def _stored_page_dimensions(value: Any, page_count: int) -> dict[int, tuple[float, float]]:
    if not isinstance(value, list) or len(value) != page_count:
        raise ParseInputValidationError("pages が全ページを含んでいません。")
    dimensions: dict[int, tuple[float, float]] = {}
    for index, page in enumerate(value, start=1):
        if not isinstance(page, dict):
            raise ParseInputValidationError(f"pages[{index}] がオブジェクトではありません。")
        page_number = _required_int(page, "page", minimum=1)
        if page_number in dimensions:
            raise ParseInputValidationError("pages のページ番号が重複しています。")
        width = _required_number(page, "width", positive=True)
        height = _required_number(page, "height", positive=True)
        _required_number(page, "pdf_width", positive=True)
        _required_number(page, "pdf_height", positive=True)
        dimensions[page_number] = (width, height)
    if set(dimensions) != set(range(1, page_count + 1)):
        raise ParseInputValidationError("pages のページ範囲が不正です。")
    return dimensions


def _restore_record(
    value: Any,
    *,
    engine_id: str,
    stored_pages: dict[int, tuple[float, float]],
    current_pages: dict[int, PageImage],
    record_index: int,
) -> LayoutRecord:
    if not isinstance(value, dict):
        raise ParseInputValidationError(f"records[{record_index}] がオブジェクトではありません。")
    if value.get("engine") != engine_id:
        raise ParseInputValidationError(f"records[{record_index}] の engine が一致しません。")
    page_number = _required_int(value, "page", minimum=1)
    if page_number not in stored_pages or page_number not in current_pages:
        raise ParseInputValidationError(f"records[{record_index}] のページ番号が不正です。")
    seq_no = _required_int(value, "seq_no", minimum=1)
    record_id = _required_string(value, "id")
    if value.get("coord_system") != "image_top_left":
        raise ParseInputValidationError(f"records[{record_index}] の座標系が不正です。")

    stored_width, stored_height = stored_pages[page_number]
    record_width = _required_number(value, "page_width", positive=True)
    record_height = _required_number(value, "page_height", positive=True)
    if not math.isclose(record_width, stored_width) or not math.isclose(record_height, stored_height):
        raise ParseInputValidationError(f"records[{record_index}] のページ寸法が一致しません。")
    bbox = value.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ParseInputValidationError(f"records[{record_index}] の bbox が不正です。")
    try:
        x1, y1, x2, y2 = [float(item) for item in bbox]
    except (TypeError, ValueError) as exc:
        raise ParseInputValidationError(f"records[{record_index}] の bbox が数値ではありません。") from exc
    if not all(math.isfinite(item) for item in (x1, y1, x2, y2)):
        raise ParseInputValidationError(f"records[{record_index}] の bbox が有限値ではありません。")
    if x1 > x2 or y1 > y2 or x1 < 0 or y1 < 0 or x2 > stored_width or y2 > stored_height:
        raise ParseInputValidationError(f"records[{record_index}] の bbox がページ範囲外です。")

    current_page = current_pages[page_number]
    scaled_bbox = clamp_bbox(
        [
            x1 * current_page.width / stored_width,
            y1 * current_page.height / stored_height,
            x2 * current_page.width / stored_width,
            y2 * current_page.height / stored_height,
        ],
        current_page.width,
        current_page.height,
    )
    confidence_value = value.get("confidence")
    confidence = None
    if confidence_value is not None:
        if isinstance(confidence_value, bool):
            raise ParseInputValidationError(f"records[{record_index}] の confidence が不正です。")
        try:
            confidence = float(confidence_value)
        except (TypeError, ValueError) as exc:
            raise ParseInputValidationError(f"records[{record_index}] の confidence が不正です。") from exc
        if not math.isfinite(confidence):
            raise ParseInputValidationError(f"records[{record_index}] の confidence が有限値ではありません。")
    raw = value.get("raw")
    if not isinstance(raw, dict):
        raise ParseInputValidationError(f"records[{record_index}] の raw がオブジェクトではありません。")
    raw = dict(raw)
    # 再描画 DPI が変わる場合も表内画像を正しい位置から切り出し直します。
    if "table_missing_picture_regions" in raw:
        scaled_regions = []
        if not isinstance(raw["table_missing_picture_regions"], list):
            raise ParseInputValidationError("表内画像領域が配列ではありません。")
        for region in raw["table_missing_picture_regions"]:
            if not isinstance(region, list) or len(region) != 4:
                raise ParseInputValidationError("表内画像領域の bbox が不正です。")
            try:
                box = [float(value) for value in region]
            except (TypeError, ValueError) as exc:
                raise ParseInputValidationError("表内画像領域の bbox が不正です。") from exc
            if not all(math.isfinite(value) for value in box):
                raise ParseInputValidationError("表内画像領域の bbox が有限値ではありません。")
            scaled_regions.append(
                clamp_bbox(
                    [
                        box[0] * current_page.width / stored_width,
                        box[1] * current_page.height / stored_height,
                        box[2] * current_page.width / stored_width,
                        box[3] * current_page.height / stored_height,
                    ],
                    current_page.width,
                    current_page.height,
                )
            )
        raw["table_missing_picture_regions"] = scaled_regions
    return LayoutRecord(
        id=record_id,
        engine=engine_id,
        page=page_number,
        seq_no=seq_no,
        bbox=scaled_bbox,
        coord_system="image_top_left",
        page_width=current_page.width,
        page_height=current_page.height,
        category=_required_string(value, "category"),
        text=_required_string(value, "text", allow_empty=True),
        confidence=confidence,
        raw_type=_required_string(value, "raw_type", allow_empty=True),
        raw=raw,
    )


def _required_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ParseInputValidationError(f"{key} がオブジェクトではありません。")
    return value


def _required_string(payload: dict[str, Any], key: str, *, allow_empty: bool = False) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ParseInputValidationError(f"{key} が文字列ではありません。")
    return value


def _required_int(payload: dict[str, Any], key: str, *, minimum: int) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ParseInputValidationError(f"{key} が不正です。")
    return value


def _optional_bool(payload: dict[str, Any], key: str, *, default: bool) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise ParseInputValidationError(f"{key} が真偽値ではありません。")
    return value


def _required_number(payload: dict[str, Any], key: str, *, positive: bool) -> float:
    value = payload.get(key)
    if isinstance(value, bool):
        raise ParseInputValidationError(f"{key} が数値ではありません。")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ParseInputValidationError(f"{key} が数値ではありません。") from exc
    if not math.isfinite(number) or (positive and number <= 0):
        raise ParseInputValidationError(f"{key} が不正です。")
    return number


def _atomic_write_json(target: Path, payload: Any) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
