"""チャンキングの実行成果物の永続化。"""

from __future__ import annotations

import hashlib
import json
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rag_engine.chunking import (
    CHUNK_METADATA_SCHEMA_VERSION,
    CHUNK_SCHEMA_VERSION,
    CHUNK_STRATEGY,
    CHUNKS_DIRECTORY,
    LATEST_CHUNKS_FILE,
    RUN_ID_PATTERN,
    SEARCH_TEXT_SCHEMA_VERSION,
    ChunkingConfig,
    ChunkingResult,
    _bool_value,
    _chunk_from_payload,
    _chunking_config_from_payload,
    _config_hash,
    _create_chunk_run_id,
    _effective_engine_ids,
    _payload_classification,
    _required_string,
    _source_page_count,
    _string_list,
    audit_chunk_retrieval_text,
    build_small_to_big_chunks,
    validate_run_id,
)
from rag_engine.knowledge.classification import DocumentClassification, classification_from_metadata
from rag_engine.knowledge.document_metadata import normalize_document_metadata
from rag_engine.retrieval.inquiry_conditions import INQUIRY_CHUNK_METADATA_SCHEMA_VERSION, inquiry_profile_contract_hash


def create_chunk_run(
    *,
    output_dir: str | Path,
    run_id: Any,
    preferred_engine_ids: Iterable[str],
    config: ChunkingConfig | None = None,
    classification: DocumentClassification | dict[str, Any] | None = None,
    document_metadata: dict[str, Any] | None = None,
) -> ChunkingResult:
    """解析結果から文書情報を引き継いだ Small-to-Big チャンクを作成・保存する。

    document_metadata が None の場合は解析結果を使用し、明示値は全体を置き換える。
    不正な文書情報は保存前に ValueError とする。既存解析の品質値は再推測しない。
    """
    source_run_id = validate_run_id(run_id)
    payload = _load_viewer_payload(output_dir, source_run_id)
    payload["document_metadata"] = normalize_document_metadata(
        document_metadata if document_metadata is not None else payload.get("document_metadata"),
        source_file_name=str(payload.get("pdf_name") or ""),
    )
    document_classification = classification_from_metadata(classification if classification is not None else payload.get("classification"))
    selected_engine_ids = _effective_engine_ids(payload, preferred_engine_ids)
    chunking_config = (config or ChunkingConfig()).validate()
    created_at = datetime.now(UTC).isoformat()
    source_file_sha256 = _optional_file_sha256(Path(output_dir) / source_run_id / "source.pdf")
    source_page_count = _source_page_count(payload)
    chunks = build_small_to_big_chunks(
        payload,
        source_run_id=source_run_id,
        selected_engine_ids=selected_engine_ids,
        config=chunking_config,
        created_at_utc=created_at,
        source_file_sha256=source_file_sha256,
        source_page_count=source_page_count,
        classification=document_classification,
    )
    if not chunks:
        raise ValueError("チャンキング対象の解析テキストがありません。")

    config_hash = _config_hash(chunking_config, selected_engine_ids)
    chunk_run_id = _create_chunk_run_id(source_run_id, config_hash)
    run_chunks_dir = Path(output_dir) / source_run_id / CHUNKS_DIRECTORY
    chunk_dir = run_chunks_dir / chunk_run_id
    chunk_dir.mkdir(parents=True, exist_ok=True)
    json_path = chunk_dir / "chunks.json"
    jsonl_path = chunk_dir / "chunks.jsonl"
    latest_path = run_chunks_dir / LATEST_CHUNKS_FILE
    result = ChunkingResult(
        source_run_id=source_run_id,
        chunk_run_id=chunk_run_id,
        source_file_name=str(payload.get("pdf_name") or ""),
        selected_engine_ids=selected_engine_ids,
        config=chunking_config,
        config_hash=config_hash,
        created_at_utc=created_at,
        active=True,
        source_file_sha256=source_file_sha256,
        source_page_count=source_page_count,
        chunks=chunks,
        json_path=str(json_path),
        jsonl_path=str(jsonl_path),
        latest_path=str(latest_path),
        classification=document_classification.to_metadata(),
        document_metadata=dict(chunks[0].metadata["document"]),
        source_document_id=str(payload.get("source_document_id") or ""),
    )
    _write_json(json_path, chunk_payload(result))
    _write_jsonl(jsonl_path, (chunk.to_dict() for chunk in chunks))
    _write_json(
        latest_path,
        {
            "schema_version": CHUNK_SCHEMA_VERSION,
            "strategy": CHUNK_STRATEGY,
            "source_run_id": source_run_id,
            "chunk_run_id": chunk_run_id,
            "created_at_utc": created_at,
            "config_hash": config_hash,
            "active": True,
            "chunks_json_path": str(json_path),
            "chunks_jsonl_path": str(jsonl_path),
            # 元ファイルの照合用。これが無いと照合のたびに全 run の chunks.json を読むことになる (#855)。
            "source_file_sha256": result.source_file_sha256,
            "source_page_count": result.source_page_count,
            "source_file_name": result.source_file_name,
        },
    )
    return result


def load_latest_chunk_run(output_dir: str | Path, run_id: Any) -> ChunkingResult | None:
    """指定 source run に紐づく最新チャンク実行を読み込みます。

    None は chunk run が無い場合（latest.json が無い・読めない・`chunk_run_id` が不正）。保存済みの
    chunks.json が現行 schema でない場合は `chunk_result_from_payload` の ValueError（「再チャンキングして
    ください」）をそのまま伝播させる。None に丸めると呼び出し側が「チャンクがありません」と誤案内するため、
    利用者に次の操作を示せる例外を残す (#800)。

    Raises:
        ValueError: chunks.json が旧 schema / 不正な contracts の場合（run_id 自体が不正な場合も同じ型）。
    """
    source_run_id = validate_run_id(run_id)
    latest_path = Path(output_dir) / source_run_id / CHUNKS_DIRECTORY / LATEST_CHUNKS_FILE
    if not latest_path.exists():
        return None
    try:
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
        chunk_run_id = _required_string(latest, "chunk_run_id")
        # 自アプリが書く pointer だが、パスに連結する前に load_chunk_run_by_id と同じ検証を通す (#800)。
        if not RUN_ID_PATTERN.fullmatch(chunk_run_id):
            return None
        chunk_json_path = Path(output_dir) / source_run_id / CHUNKS_DIRECTORY / chunk_run_id / "chunks.json"
        payload = json.loads(chunk_json_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return chunk_result_from_payload(payload, json_path=chunk_json_path, latest_path=latest_path)


def load_chunk_run_by_id(output_dir: str | Path, chunk_run_id: Any) -> ChunkingResult | None:
    """chunk_run_id から保存済みチャンク実行を読み込みます。

    None は該当する chunk run が無い場合（ID が不正・ディレクトリが無い・chunks.json が読めない）。
    ID で特定した chunks.json が旧 schema なら、load_latest_chunk_run と同じく ValueError を伝播させる。
    None に丸めると chunk_run_id を固定した回答生成が「存在しない実行」と誤案内する (#800)。

    Raises:
        ValueError: 該当 chunks.json が旧 schema / 不正な contracts の場合。
    """
    normalized_chunk_run_id = str(chunk_run_id or "").strip()
    if not normalized_chunk_run_id or not RUN_ID_PATTERN.fullmatch(normalized_chunk_run_id):
        return None

    try:
        candidates = Path(output_dir).glob(f"*/{CHUNKS_DIRECTORY}/{normalized_chunk_run_id}/chunks.json")
        for chunk_json_path in candidates:
            try:
                payload = json.loads(chunk_json_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            result = chunk_result_from_payload(
                payload,
                json_path=chunk_json_path,
                latest_path=chunk_json_path.parent.parent / LATEST_CHUNKS_FILE,
            )
            if result.chunk_run_id == normalized_chunk_run_id:
                return result
    except OSError:
        return None
    return None


def load_latest_or_source_chunk_run(output_dir: str | Path, run_id: Any) -> ChunkingResult | None:
    """最新チャンクがなければ source run 自身を fallback として読み込みます。

    Raises:
        ValueError: 最新チャンクの chunks.json が旧 schema の場合（load_latest_chunk_run と同じ）。
    """
    direct = load_latest_chunk_run(output_dir, run_id)
    if direct is not None:
        return direct
    return load_latest_chunk_run_for_run_source(output_dir, run_id)


def load_latest_chunk_run_for_source(
    output_dir: str | Path,
    source_path: str | Path,
    source_page_count: int,
) -> ChunkingResult | None:
    """アップロード元ファイルに紐づく最新チャンク実行を返します。

    preview run はファイル選択のたびに新しい run ID を受け取るため、run ID だけでは
    以前の訪問で作成した chunk を復元できません。この照合のために chunk payload へ
    source checksum と page count を保持しています。
    """
    source_file = Path(source_path)
    if not source_file.is_file():
        return None
    source_sha256 = _optional_file_sha256(source_file)
    if not source_sha256 or source_page_count < 1:
        return None
    return _load_latest_chunk_run_for_source_identity(
        output_dir,
        source_sha256=source_sha256,
        source_page_count=source_page_count,
        source_file_name=source_file.name,
    )


def load_latest_chunk_run_for_run_source(
    output_dir: str | Path,
    run_id: Any,
) -> ChunkingResult | None:
    """run_id の元ファイルと同一内容に対する最新チャンク実行を読み込みます。"""
    source_run_id = validate_run_id(run_id)
    try:
        payload = _load_viewer_payload(output_dir, source_run_id)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RuntimeError):
        return None

    source_path = Path(output_dir) / source_run_id / "source.pdf"
    source_sha256 = _optional_file_sha256(source_path)
    source_page_count = _source_page_count(payload)
    source_file_name = str(payload.get("pdf_name") or "").strip()
    if not source_sha256 or source_page_count < 1 or not source_file_name:
        return None
    return _load_latest_chunk_run_for_source_identity(
        output_dir,
        source_sha256=source_sha256,
        source_page_count=source_page_count,
        source_file_name=source_file_name,
    )


def _load_latest_chunk_run_for_source_identity(
    output_dir: str | Path,
    *,
    source_sha256: str,
    source_page_count: int,
    source_file_name: str,
) -> ChunkingResult | None:
    candidates: list[ChunkingResult] = []
    try:
        run_directories = Path(output_dir).iterdir()
    except OSError:
        return None
    for run_dir in run_directories:
        if not run_dir.is_dir():
            continue
        # latest.json に照合項目があれば、それだけで不一致を除外し本体を読まない。項目の無い旧 pointer は
        # 従来どおり chunks.json を読んで照合する (#855)。
        pointer = _latest_pointer_identity(Path(output_dir) / run_dir.name / CHUNKS_DIRECTORY / LATEST_CHUNKS_FILE)
        if pointer is not None and pointer != (source_sha256, source_page_count, source_file_name, True):
            continue
        try:
            result = load_latest_chunk_run(output_dir, run_dir.name)
        except ValueError:
            continue
        if result is None or not result.active:
            continue
        if (
            result.source_file_sha256 == source_sha256
            and result.source_page_count == source_page_count
            and result.source_file_name == source_file_name
        ):
            candidates.append(result)
    if not candidates:
        return None
    return max(candidates, key=lambda result: result.created_at_utc)


def _latest_pointer_identity(latest_path: Path) -> tuple[str, int, str, bool] | None:
    """latest.json の照合項目 (sha256, page_count, file_name, active)。項目が無い・読めない場合は None。"""
    try:
        latest = json.loads(latest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(latest, dict) or "source_file_sha256" not in latest:
        return None
    try:
        return (
            str(latest.get("source_file_sha256") or ""),
            int(latest.get("source_page_count") or 0),
            str(latest.get("source_file_name") or ""),
            bool(latest.get("active", True)),
        )
    except (TypeError, ValueError):
        return None


def chunk_result_from_payload(
    payload: dict[str, Any],
    *,
    json_path: str | Path = "",
    latest_path: str | Path = "",
) -> ChunkingResult:
    """保存済み chunks.json payload を ChunkingResult に復元します。"""
    if payload.get("schema_version") != CHUNK_SCHEMA_VERSION:
        raise ValueError("対応していない chunk schema_version です。再チャンキングしてください。")
    if payload.get("strategy") != CHUNK_STRATEGY:
        raise ValueError("対応していない chunk strategy です。")
    _validate_chunk_contracts(payload.get("contracts"))
    config = _chunking_config_from_payload(payload.get("config", {}))
    chunks_payload = payload.get("chunks")
    if not isinstance(chunks_payload, list):
        raise ValueError("chunks が配列ではありません。")
    chunks = [_chunk_from_payload(chunk) for chunk in chunks_payload]
    json_path_text = str(json_path or payload.get("json_path") or "")
    latest_path_text = str(latest_path or payload.get("latest_path") or "")
    resolved_json_path = Path(json_path_text) if json_path_text else Path("")
    resolved_latest_path = Path(latest_path_text) if latest_path_text else Path("")
    jsonl_path = resolved_json_path.with_suffix(".jsonl") if resolved_json_path.name else Path("")
    return ChunkingResult(
        source_run_id=_required_string(payload, "source_run_id"),
        chunk_run_id=_required_string(payload, "chunk_run_id"),
        source_file_name=_required_string(payload, "source_file_name", allow_empty=True),
        selected_engine_ids=_string_list(payload.get("selected_engine_ids")),
        config=config,
        config_hash=_required_string(payload, "config_hash"),
        created_at_utc=_required_string(payload, "created_at_utc"),
        active=_bool_value(payload.get("active"), default=True),
        source_file_sha256=str(payload.get("source_file_sha256") or ""),
        source_page_count=int(payload.get("source_page_count") or 0),
        chunks=chunks,
        json_path=str(resolved_json_path) if json_path_text else "",
        jsonl_path=str(jsonl_path) if jsonl_path.name else "",
        latest_path=str(resolved_latest_path) if latest_path_text else "",
        classification=_payload_classification(payload, chunks),
        document_metadata=payload.get("document_metadata") or {},
        source_document_id=str(payload.get("source_document_id") or ""),
    )


def chunk_payload(result: ChunkingResult) -> dict[str, Any]:
    """ChunkingResult を保存用 JSON payload に変換します。"""
    return {
        "schema_version": CHUNK_SCHEMA_VERSION,
        "strategy": CHUNK_STRATEGY,
        "contracts": _chunk_contracts(),
        "chunk_run_id": result.chunk_run_id,
        "source_run_id": result.source_run_id,
        "source_file_name": result.source_file_name,
        "selected_engine_ids": result.selected_engine_ids,
        "created_at_utc": result.created_at_utc,
        "active": result.active,
        "source_file_sha256": result.source_file_sha256,
        "source_document_id": result.source_document_id,
        "source_page_count": result.source_page_count,
        "classification": result.classification,
        "document_metadata": result.document_metadata,
        "config": result.config.to_dict(),
        "config_hash": result.config_hash,
        "summary": {
            "parent_count": result.parent_count,
            "child_count": result.child_count,
            "chunk_count": len(result.chunks),
            "retrieval_text_audit": audit_chunk_retrieval_text(
                result.chunks,
                child_search_text_max_chars=result.config.child_search_text_max_chars,
            ),
        },
        "chunks": [chunk.to_dict() for chunk in result.chunks],
    }


def _chunk_contracts() -> dict[str, Any]:
    """run 全体で共有する保存・検索契約を返します。"""
    contracts = {
        "chunk_metadata_schema_version": CHUNK_METADATA_SCHEMA_VERSION,
        "search_text_schema_version": SEARCH_TEXT_SCHEMA_VERSION,
        "inquiry_chunk_metadata_schema_version": INQUIRY_CHUNK_METADATA_SCHEMA_VERSION,
        "inquiry_profile_contract_hash": inquiry_profile_contract_hash(),
    }
    contracts["fingerprint"] = hashlib.sha256(
        json.dumps(contracts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return contracts


def _validate_chunk_contracts(value: Any) -> None:
    """新規 run の契約を厳密に検証し、欠損や旧版を拒否します。"""
    if value != _chunk_contracts():
        raise ValueError("対応していない chunk contracts です。再チャンキングしてください。")


def _load_viewer_payload(output_dir: str | Path, run_id: str) -> dict[str, Any]:
    viewer_data_path = Path(output_dir) / run_id / "viewer-data.json"
    if not viewer_data_path.exists():
        raise FileNotFoundError("解析結果が見つかりません。ファイル解析 tab で解析を実行してください。")
    try:
        payload = json.loads(viewer_data_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("解析結果 JSON を読み込めません。") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("解析結果 JSON の形式が不正です。")
    return payload


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def _write_jsonl(path: Path, payloads: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            for payload in payloads:
                temporary.write(json.dumps(payload, ensure_ascii=False))
                temporary.write("\n")
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def persist_batch(batch, *, output_dir: str | Path) -> ChunkingResult:
    """SDK の分割済みデータを既存 manifest 形式で保存する。再分割はしない。"""
    from rag_engine.chunking import CHUNKS_DIRECTORY, LATEST_CHUNKS_FILE, _config_hash, _create_chunk_run_id

    document = batch.document
    source_id = validate_run_id(document.source_run_id or document.document_id)
    engines = list(dict.fromkeys(chunk.source_engine_id for chunk in batch.chunks))
    config_hash = _config_hash(batch.config, engines)
    run_id = _create_chunk_run_id(source_id, config_hash)
    directory = Path(output_dir) / source_id / CHUNKS_DIRECTORY / run_id
    latest = directory.parent / LATEST_CHUNKS_FILE
    created = datetime.now(UTC).isoformat()
    result = ChunkingResult(
        source_id,
        run_id,
        document.source_name,
        engines,
        batch.config,
        config_hash,
        created,
        True,
        _optional_file_sha256(Path(output_dir) / source_id / "source.pdf"),
        document.page_count,
        list(batch.chunks),
        str(directory / "chunks.json"),
        str(directory / "chunks.jsonl"),
        str(latest),
        dict(document.classification),
        dict(document.metadata),
        source_document_id=document.document_id,
    )
    directory.mkdir(parents=True, exist_ok=True)
    _write_json(Path(result.json_path), chunk_payload(result))
    _write_jsonl(Path(result.jsonl_path), (chunk.to_dict() for chunk in result.chunks))
    _write_json(
        latest,
        {
            "schema_version": CHUNK_SCHEMA_VERSION,
            "strategy": CHUNK_STRATEGY,
            "source_run_id": source_id,
            "chunk_run_id": run_id,
            "created_at_utc": created,
            "config_hash": config_hash,
            "active": True,
            "chunks_json_path": result.json_path,
            "chunks_jsonl_path": result.jsonl_path,
        },
    )
    return result


# (path, size, mtime_ns) → sha256。同じファイルを繰り返し読まないための process 内 cache (#859)。
_FILE_SHA256_CACHE: dict[tuple[str, int, int], str] = {}
_FILE_SHA256_CACHE_LIMIT = 256


def _optional_file_sha256(path: Path) -> str:
    """元ファイルの hash を読む。旧成果物の欠損時は処理継続のため空文字とする。

    質問欄の打鍵ごとに approved FAQ の候補更新が同じ PDF（数十 MB）をハッシュしていたため、
    サイズと mtime が同じ間は前回の値を返す。ファイルが書き換われば mtime が変わり再計算する (#859)。
    """
    try:
        stat = path.stat()
    except OSError:
        return ""
    key = (str(path), int(stat.st_size), int(stat.st_mtime_ns))
    cached = _FILE_SHA256_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        digest = hashlib.sha256()
        with path.open("rb") as source_file:
            for block in iter(lambda: source_file.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        return ""
    value = digest.hexdigest()
    if len(_FILE_SHA256_CACHE) >= _FILE_SHA256_CACHE_LIMIT:
        _FILE_SHA256_CACHE.clear()
    _FILE_SHA256_CACHE[key] = value
    return value
