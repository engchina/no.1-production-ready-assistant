"""OCI Document Understanding(service 系 parser backend)共有 core。

`oci.ai_document` の非同期 processor job で日本語 OCR / 表抽出を行い、結果 JSON を
`StructuredExtraction` 互換 payload へ決定論で remap する。入出力は Object Storage 経由。

backend `Settings` には依存せず、`OciDocumentUnderstandingConfig`(env からも構築可能)で
駆動する。backend は本 core を Settings 由来の config で呼ぶ薄い adapter
(`app.clients.oci_document_understanding`)経由で in-process 利用し、parser マイクロ
サービス(`services/parsers/oci_document_understanding`)は env 由来 config で利用する。

実 OCI 呼び出しは遅延 import + thread 実行で隔離し、テストでは ``document_client`` /
``object_storage_client`` を注入して fake SDK で決定論検証する。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import mimetypes
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from rag_parser_core.extraction import normalize_bbox
from rag_parser_core.oci_auth import load_oci_config_without_prompt

logger = logging.getLogger(__name__)

# processor job のライフサイクル状態(getattr で大文字小文字を吸収する)。
_SUCCEEDED_STATES = {"SUCCEEDED"}
_TERMINAL_FAILURE_STATES = {"FAILED", "CANCELED", "CANCELLED"}

# 設定 feature 名 → SDK feature クラス名の対応。未知の値は無視する。
_FEATURE_CLASS_BY_NAME: dict[str, str] = {
    "DOCUMENT_TEXT_EXTRACTION": "DocumentTextExtractionFeature",
    "TEXT_EXTRACTION": "DocumentTextExtractionFeature",
    "TABLE_EXTRACTION": "DocumentTableExtractionFeature",
    "DOCUMENT_TABLE_EXTRACTION": "DocumentTableExtractionFeature",
    "KEY_VALUE_EXTRACTION": "DocumentKeyValueExtractionFeature",
    "DOCUMENT_KEY_VALUE_EXTRACTION": "DocumentKeyValueExtractionFeature",
}

_DEFAULT_FEATURES = ("DOCUMENT_TEXT_EXTRACTION", "TABLE_EXTRACTION")
_LANGUAGE_ALIASES = {
    "JPN": "ja",
    "JP": "ja",
    "JA_JP": "ja-JP",
}


@dataclass(frozen=True)
class OciDocumentUnderstandingConfig:
    """DU 実行に必要な非機密設定。backend Settings / env のどちらからも構築できる。"""

    compartment_id: str = ""
    fallback_compartment_id: str = ""
    namespace: str = ""
    fallback_namespace: str = ""
    input_bucket: str = ""
    fallback_input_bucket: str = ""
    output_bucket: str = ""
    input_prefix: str = "document-understanding/input"
    output_prefix: str = "document-understanding/output"
    language: str = "ja"
    features: Sequence[str] = field(default_factory=lambda: list(_DEFAULT_FEATURES))
    poll_interval_seconds: float = 5.0
    timeout_seconds: float = 600.0
    oci_config_file: str = "~/.oci/config"
    oci_config_profile: str = "DEFAULT"
    oci_region: str = ""
    object_storage_region: str = ""

    # --- 設定解決(空欄は object_storage_* / compartment へ fallback)---
    def resolve_compartment_id(self) -> str:
        return self.compartment_id.strip() or self.fallback_compartment_id.strip()

    def resolve_namespace(self) -> str:
        return self.namespace.strip() or self.fallback_namespace.strip()

    def resolve_input_bucket(self) -> str:
        return self.input_bucket.strip() or self.fallback_input_bucket.strip()

    def resolve_output_bucket(self) -> str:
        return self.output_bucket.strip() or self.resolve_input_bucket()

    def is_configured(self) -> bool:
        """job を投入できる最小設定(compartment / namespace / 入力 bucket)が揃うか。"""
        return bool(
            self.resolve_compartment_id()
            and self.resolve_namespace()
            and self.resolve_input_bucket()
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OciDocumentUnderstandingConfig:
        """環境変数から構築する。

        共通の値は backend と同じ PLATFORM_OCI_* / PLATFORM_OBJECT_STORAGE_*、Document Understanding
        固有の値は RAG_OCI_DOCUMENT_UNDERSTANDING_* を読む(#211)。
        """
        src = os.environ if env is None else env

        def _get(name: str, default: str = "") -> str:
            return str(src.get(name, default) or default)

        features_raw = _get("RAG_OCI_DOCUMENT_UNDERSTANDING_FEATURES").strip()
        features: Sequence[str]
        if features_raw:
            try:
                parsed = json.loads(features_raw)
                features = [str(item) for item in parsed] if isinstance(parsed, list) else []
            except (json.JSONDecodeError, ValueError):
                features = [part.strip() for part in features_raw.split(",") if part.strip()]
            if not features:
                features = list(_DEFAULT_FEATURES)
        else:
            features = list(_DEFAULT_FEATURES)

        return cls(
            compartment_id=_get("RAG_OCI_DOCUMENT_UNDERSTANDING_COMPARTMENT_ID"),
            fallback_compartment_id=_get("PLATFORM_OCI_COMPARTMENT_ID"),
            namespace=_get("RAG_OCI_DOCUMENT_UNDERSTANDING_NAMESPACE"),
            fallback_namespace=_get("PLATFORM_OBJECT_STORAGE_NAMESPACE"),
            input_bucket=_get("RAG_OCI_DOCUMENT_UNDERSTANDING_INPUT_BUCKET"),
            fallback_input_bucket=_get("PLATFORM_OBJECT_STORAGE_BUCKET"),
            output_bucket=_get("RAG_OCI_DOCUMENT_UNDERSTANDING_OUTPUT_BUCKET"),
            input_prefix=_get(
                "RAG_OCI_DOCUMENT_UNDERSTANDING_INPUT_PREFIX", "document-understanding/input"
            ),
            output_prefix=_get(
                "RAG_OCI_DOCUMENT_UNDERSTANDING_OUTPUT_PREFIX", "document-understanding/output"
            ),
            language=_get("RAG_OCI_DOCUMENT_UNDERSTANDING_LANGUAGE", "ja"),
            features=features,
            poll_interval_seconds=_float(
                _get("RAG_OCI_DOCUMENT_UNDERSTANDING_POLL_INTERVAL_SECONDS"), 5.0
            ),
            timeout_seconds=_float(_get("RAG_OCI_DOCUMENT_UNDERSTANDING_TIMEOUT_SECONDS"), 600.0),
            oci_config_file=_get("PLATFORM_OCI_CONFIG_FILE", "~/.oci/config"),
            oci_config_profile=_get("PLATFORM_OCI_CONFIG_PROFILE", "DEFAULT"),
            oci_region=_get("PLATFORM_OCI_REGION"),
            object_storage_region=_get(
                "RAG_OCI_DOCUMENT_UNDERSTANDING_OBJECT_STORAGE_REGION",
                _get("PLATFORM_OCI_REGION") or _get("PLATFORM_OBJECT_STORAGE_REGION"),
            ),
        )


class DocumentUnderstandingSdkProtocol(Protocol):
    """`oci.ai_document.AIServiceDocumentClient` の利用部分。"""

    def create_processor_job(self, create_processor_job_details: object) -> object:
        """processor job を作成し、data.id / lifecycle_state を持つ response を返す。"""

    def get_processor_job(self, processor_job_id: str) -> object:
        """processor job の現在状態を返す。"""


class ObjectStorageSdkProtocol(Protocol):
    """DU 入出力に使う Object Storage SDK の利用部分。"""

    def put_object(
        self,
        namespace_name: str,
        bucket_name: str,
        object_name: str,
        put_object_body: bytes,
        **kwargs: Any,
    ) -> object:
        """入力ファイルを Object Storage へアップロードする。"""

    def list_objects(self, namespace_name: str, bucket_name: str, **kwargs: Any) -> object:
        """prefix 配下の object を列挙する。"""

    def get_object(
        self, namespace_name: str, bucket_name: str, object_name: str, **kwargs: Any
    ) -> object:
        """結果 JSON を取得する。"""

    def delete_object(
        self, namespace_name: str, bucket_name: str, object_name: str, **kwargs: Any
    ) -> object:
        """job の入出力の object を消す。"""


class OciDocumentUnderstandingService:
    """OCI Document Understanding を非同期 processor job で呼ぶ共有サービス。"""

    def __init__(
        self,
        config: OciDocumentUnderstandingConfig,
        *,
        document_client: DocumentUnderstandingSdkProtocol | None = None,
        object_storage_client: ObjectStorageSdkProtocol | None = None,
    ) -> None:
        self._config = config
        self._document_client = document_client
        self._object_storage_client = object_storage_client

    def is_configured(self) -> bool:
        return self._config.is_configured()

    async def analyze(
        self, source_bytes: bytes, *, content_type: str, document_id: str
    ) -> dict[str, object] | None:
        """DU で 1 文書を解析し、StructuredExtraction 互換 payload を返す。

        未設定・SDK/通信失敗・job 失敗・timeout のときは ``None`` を返す。
        """
        if not self.is_configured():
            logger.info("OCI Document Understanding 未設定のため縮退します。")
            return None
        object_name = self._input_object_name(document_id, content_type)
        job_id: str | None = None
        try:
            try:
                job_id = await asyncio.to_thread(
                    self._submit_job, source_bytes, object_name, content_type
                )
            except Exception as exc:  # noqa: BLE001 - 失敗時は安全に縮退する
                logger.warning("DU job 投入に失敗しました。", extra={"error": str(exc)})
                return None
            try:
                await self._await_job(job_id)
                result = await asyncio.to_thread(self._read_first_result, job_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "DU job の完了待ち/結果取得に失敗しました。",
                    extra={"error": str(exc), "job_id": job_id},
                )
                return None
        finally:
            # job の入力（原本の写し）と出力は解析の後に使わない（結果は取込が抽出の artifact に
            # 保存し直す）。失敗・時間切れでも消し、文書を削除した後に bucket に残さない（#2440）。
            await asyncio.to_thread(self._delete_job_objects, object_name, job_id)
        if result is None:
            return None
        return document_understanding_result_to_payload(result)

    # --- job ライフサイクル ---
    def _submit_job(self, source_bytes: bytes, object_name: str, content_type: str) -> str:
        """入力を put し、processor job を作成して job id を返す(同期)。"""
        models = importlib.import_module("oci.ai_document.models")
        namespace = self._config.resolve_namespace()
        input_bucket = self._config.resolve_input_bucket()
        self._storage().put_object(
            namespace,
            input_bucket,
            object_name,
            source_bytes,
            content_type=content_type or "application/octet-stream",
        )
        details = models.CreateProcessorJobDetails(
            compartment_id=self._config.resolve_compartment_id(),
            input_location=models.ObjectStorageLocations(
                object_locations=[
                    models.ObjectLocation(
                        namespace_name=namespace,
                        bucket_name=input_bucket,
                        object_name=object_name,
                    )
                ]
            ),
            output_location=models.OutputLocation(
                namespace_name=namespace,
                bucket_name=self._config.resolve_output_bucket(),
                prefix=self._config.output_prefix.strip(),
            ),
            processor_config=models.GeneralProcessorConfig(
                features=self._build_features(models),
                language=_bcp47_language(self._config.language),
            ),
        )
        response = self._documents().create_processor_job(create_processor_job_details=details)
        job = getattr(response, "data", response)
        job_id = getattr(job, "id", None)
        if not job_id:
            raise RuntimeError("DU create_processor_job が job id を返しませんでした。")
        return str(job_id)

    async def _await_job(self, job_id: str) -> None:
        """SUCCEEDED まで poll する。FAILED/timeout は例外で縮退させる。"""
        interval = float(self._config.poll_interval_seconds)
        deadline = time.monotonic() + float(self._config.timeout_seconds)
        while True:
            job = await asyncio.to_thread(self._get_job, job_id)
            state = str(getattr(job, "lifecycle_state", "") or "").upper()
            if state in _SUCCEEDED_STATES:
                return
            if state in _TERMINAL_FAILURE_STATES:
                detail = str(getattr(job, "lifecycle_details", "") or "")
                raise RuntimeError(f"DU job が失敗しました(state={state} {detail}).")
            if time.monotonic() >= deadline:
                raise TimeoutError("DU job が制限時間内に完了しませんでした。")
            await asyncio.sleep(interval)

    def _get_job(self, job_id: str) -> object:
        response = self._documents().get_processor_job(job_id)
        return getattr(response, "data", response)

    def _read_first_result(self, job_id: str) -> dict[str, object] | None:
        """output prefix/job_id 配下の最初の結果 JSON を読み込む(同期)。"""
        namespace = self._config.resolve_namespace()
        output_bucket = self._config.resolve_output_bucket()
        prefix = f"{self._job_output_prefix(job_id)}/"
        listed = self._storage().list_objects(namespace, output_bucket, prefix=prefix)
        objects = getattr(getattr(listed, "data", listed), "objects", []) or []
        for entry in objects:
            name = str(getattr(entry, "name", "") or "")
            if not name.endswith(".json"):
                continue
            response = self._storage().get_object(namespace, output_bucket, name)
            raw = _response_bytes(response)
            if raw is None:
                continue
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed
        logger.warning("DU 結果 JSON が見つかりませんでした。", extra={"job_id": job_id})
        return None

    def _job_output_prefix(self, job_id: str) -> str:
        """job の出力を置く prefix(`<output_prefix>/<job_id>`)。"""
        return "/".join(
            part for part in (self._config.output_prefix.strip().strip("/"), job_id) if part
        )

    def _delete_job_objects(self, object_name: str, job_id: str | None) -> None:
        """job の入力と出力(`<output_prefix>/<job_id>/` の下)を消す(同期・best effort)。

        消せなくても解析の結果は変えず、ログに残す。無い object(404)は消えたものとして扱う。
        """
        namespace = self._config.resolve_namespace()
        targets: list[tuple[str, str]] = [(self._config.resolve_input_bucket(), object_name)]
        if job_id:
            output_bucket = self._config.resolve_output_bucket()
            try:
                names = self._list_object_names(
                    namespace, output_bucket, f"{self._job_output_prefix(job_id)}/"
                )
            except Exception as exc:  # noqa: BLE001 - 後始末の失敗で結果を変えない
                logger.warning(
                    "DU job の出力の一覧に失敗したため、出力を消せませんでした。",
                    extra={"error_type": type(exc).__name__, "job_id": job_id},
                )
                names = []
            targets.extend((output_bucket, name) for name in names)
        for bucket, name in targets:
            try:
                self._storage().delete_object(namespace, bucket, name)
            except Exception as exc:  # noqa: BLE001 - 後始末の失敗で結果を変えない
                if getattr(exc, "status", None) == 404:
                    continue
                logger.warning(
                    "DU job の入出力の object を消せませんでした。",
                    extra={"error_type": type(exc).__name__, "job_id": job_id},
                )

    def _list_object_names(self, namespace: str, bucket: str, prefix: str) -> list[str]:
        """prefix の下の object 名をすべて返す(ページを辿る)。"""
        names: list[str] = []
        start: str | None = None
        while True:
            kwargs: dict[str, Any] = {"prefix": prefix}
            if start:
                kwargs["start"] = start
            listed = self._storage().list_objects(namespace, bucket, **kwargs)
            data = getattr(listed, "data", listed)
            for entry in getattr(data, "objects", None) or []:
                name = str(getattr(entry, "name", "") or "")
                if name.startswith(prefix):
                    names.append(name)
            next_start = getattr(data, "next_start_with", None)
            if not isinstance(next_start, str) or not next_start or next_start == start:
                return names
            start = next_start

    def _build_features(self, models: Any) -> list[object]:
        features: list[object] = []
        for name in self._config.features:
            class_name = _FEATURE_CLASS_BY_NAME.get(str(name).strip().upper())
            feature_cls = getattr(models, class_name, None) if class_name else None
            if feature_cls is not None:
                features.append(feature_cls())
        if not features:
            features.append(models.DocumentTextExtractionFeature())
        return features

    def _input_object_name(self, document_id: str, content_type: str) -> str:
        prefix = self._config.input_prefix.strip().strip("/")
        mime = content_type.split(";")[0].strip() if content_type else ""
        ext = mimetypes.guess_extension(mime) if mime else None
        safe_id = "".join(ch for ch in document_id if ch.isalnum() or ch in "-_") or "document"
        name = f"{safe_id}{ext or ''}"
        return f"{prefix}/{name}" if prefix else name

    # --- 遅延 SDK 構築 ---
    def _documents(self) -> DocumentUnderstandingSdkProtocol:
        if self._document_client is not None:
            return self._document_client
        oci_config = importlib.import_module("oci.config")
        ai_document = importlib.import_module("oci.ai_document")
        config = load_oci_config_without_prompt(
            oci_config,
            self._config.oci_config_file,
            self._config.oci_config_profile,
            region=self._config.oci_region.strip() or None,
        )
        self._document_client = ai_document.AIServiceDocumentClient(config)
        return self._document_client

    def _storage(self) -> ObjectStorageSdkProtocol:
        if self._object_storage_client is not None:
            return self._object_storage_client
        oci_config = importlib.import_module("oci.config")
        object_storage = importlib.import_module("oci.object_storage")
        region = self._config.object_storage_region.strip() or None
        config = load_oci_config_without_prompt(
            oci_config,
            self._config.oci_config_file,
            self._config.oci_config_profile,
            region=region,
        )
        self._object_storage_client = object_storage.ObjectStorageClient(config)
        return self._object_storage_client


def _response_bytes(response: object) -> bytes | None:
    """get_object response から本文 bytes を取り出す(SDK / fake 双方を許容)。"""
    data = getattr(response, "data", response)
    raw = getattr(data, "content", None)
    if raw is None:
        raw = getattr(data, "raw", None)
    if raw is None:
        raw = data
    if isinstance(raw, str):
        return raw.encode("utf-8")
    if isinstance(raw, bytes | bytearray):
        return bytes(raw)
    return None


def document_understanding_result_to_payload(
    result: Mapping[str, object],
) -> dict[str, object]:
    """DU の解析結果 JSON を StructuredExtraction 互換 payload へ決定論で remap する。

    DU 出力 JSON は camelCase。pages[].lines[].text を読み順テキストへ、tables[] を
    ExtractionTable へ写す。raw_text だけでも StructuredExtraction 側で要素/ページを
    補完できるが、表構造は DU の強みなので明示的に持たせる。
    """
    pages_raw = _as_sequence(result.get("pages"))
    page_texts: list[str] = []
    pages_payload: list[dict[str, object]] = []
    elements_payload: list[dict[str, object]] = []
    tables_payload: list[dict[str, object]] = []
    confidences: list[float] = []
    table_counter = 0
    order = 0

    for index, page in enumerate(pages_raw):
        if not isinstance(page, Mapping):
            continue
        page_number = _as_int(page.get("pageNumber"), default=index + 1)

        dimensions = page.get("dimensions")
        page_width: float | None = None
        page_height: float | None = None
        page_entry: dict[str, object] = {"page_number": page_number}
        if isinstance(dimensions, Mapping):
            width = dimensions.get("width")
            height = dimensions.get("height")
            if isinstance(width, int | float) and width > 0:
                page_width = float(width)
                page_entry["width"] = page_width
            if isinstance(height, int | float) and height > 0:
                page_height = float(height)
                page_entry["height"] = page_height
        pages_payload.append(page_entry)

        line_texts: list[str] = []
        for line in _as_sequence(page.get("lines")):
            if not isinstance(line, Mapping):
                continue
            text = str(line.get("text", "")).strip()
            if not text:
                continue
            line_texts.append(text)
            element: dict[str, object] = {
                "kind": "text",
                "text": text,
                "order": order,
                "page_number": page_number,
            }
            # 有効な bbox に正規化できた時だけ lineage metadata を付ける(孤立 metadata を作らない)。
            bbox = normalize_bbox(_bounding_polygon(line))
            if bbox is not None:
                element["bbox"] = bbox
                element["metadata"] = _bbox_lineage("ratio", page_width, page_height, "line")
            elements_payload.append(element)
            order += 1
        if line_texts:
            page_texts.append("\n".join(line_texts))

        for word in _as_sequence(page.get("words")):
            if isinstance(word, Mapping):
                conf = word.get("confidence")
                if isinstance(conf, int | float):
                    confidences.append(float(conf))

        for table in _as_sequence(page.get("tables")):
            if not isinstance(table, Mapping):
                continue
            table_counter += 1
            cells = _remap_table_cells(table)
            tables_payload.append(
                {
                    "table_id": f"du-table-{table_counter}",
                    "page_number": page_number,
                    "cells": cells,
                }
            )

    raw_text = "\n\n".join(text for text in page_texts if text).strip()
    confidence = round(sum(confidences) / len(confidences), 4) if confidences else 0.0
    payload: dict[str, object] = {
        "raw_text": raw_text,
        "document_type": _detected_document_type(result),
        "confidence": confidence,
        "warnings": [],
        "elements": elements_payload,
        "pages": pages_payload,
        "tables": tables_payload,
        "parser_artifacts": {
            "parser_backend": "oci_document_understanding",
            "source_parser": "oci_document_understanding",
        },
    }
    return payload


def _bounding_polygon(obj: Mapping[str, object]) -> list[object] | None:
    """DU の ``boundingPolygon.normalizedVertices``(0..1, ratio)を頂点列として返す。

    OCI Document Understanding は正規化頂点のみを出す。xyxy 集約は schema validator に委ねる。
    絶対座標 variant は DU が出さないため扱わない(必要が確認できたら別途追加)。
    """
    polygon = obj.get("boundingPolygon")
    if not isinstance(polygon, Mapping):
        return None
    normalized = polygon.get("normalizedVertices")
    if isinstance(normalized, list) and normalized:
        return normalized
    return None


def _bbox_lineage(
    unit: str,
    page_width: float | None,
    page_height: float | None,
    source: str,
) -> dict[str, object]:
    """citation→preview 用の bbox lineage metadata を組む。"""
    metadata: dict[str, object] = {
        "bbox_coordinate_mode": "xyxy",
        "bbox_unit": unit,
        "bbox_source": source,
    }
    if page_width is not None:
        metadata["page_width"] = page_width
    if page_height is not None:
        metadata["page_height"] = page_height
    return metadata


def _remap_table_cells(table: Mapping[str, object]) -> list[dict[str, object]]:
    """DU table の header/body/footer 行を ExtractionTableCell payload へ写す。"""
    cells: list[dict[str, object]] = []
    for row_key in ("headerRows", "bodyRows", "footerRows"):
        for row in _as_sequence(table.get(row_key)):
            if not isinstance(row, Mapping):
                continue
            for cell in _as_sequence(row.get("cells")):
                if not isinstance(cell, Mapping):
                    continue
                text = str(cell.get("text", "")).strip()
                row_index = _as_int(cell.get("rowIndex"), default=0)
                col_index = _as_int(cell.get("columnIndex"), default=0)
                cell_payload: dict[str, object] = {
                    "row": max(row_index, 0),
                    "col": max(col_index, 0),
                    "text": text,
                    "row_span": max(_as_int(cell.get("rowSpan"), default=1), 1),
                    "col_span": max(_as_int(cell.get("columnSpan"), default=1), 1),
                }
                bbox = normalize_bbox(_bounding_polygon(cell))
                if bbox is not None:
                    cell_payload["bbox"] = bbox
                    cell_payload["metadata"] = {
                        "bbox_coordinate_mode": "xyxy",
                        "bbox_unit": "ratio",
                        "bbox_source": "cell",
                    }
                cells.append(cell_payload)
    return cells


def _detected_document_type(result: Mapping[str, object]) -> str:
    for entry in _as_sequence(result.get("detectedDocumentTypes")):
        if isinstance(entry, Mapping):
            doc_type = str(entry.get("documentType", "")).strip()
            if doc_type:
                return doc_type
    return "ドキュメント"


def _as_sequence(value: object) -> Sequence[object]:
    return value if isinstance(value, list | tuple) else ()


def _as_int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return default


def _float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bcp47_language(value: str) -> str:
    raw = value.strip()
    if not raw:
        return "ja"
    key = raw.replace("-", "_").upper()
    if key in _LANGUAGE_ALIASES:
        return _LANGUAGE_ALIASES[key]
    parts = [part for part in raw.replace("_", "-").split("-") if part]
    if not parts:
        return "ja"
    language = parts[0].lower()
    subtags = [part.upper() if len(part) == 2 else part for part in parts[1:]]
    return "-".join([language, *subtags])
