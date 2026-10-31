"""OCI / Oracle staging 用の RAG smoke test CLI。

CI では実行せず、staging 環境で実接続情報を設定してから
`python -m app.rag.staging_smoke` として実行する。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import uuid4

from app.clients.object_storage import ObjectStorageClient
from app.clients.oracle import OracleClient, close_oracle_pool
from app.config import Settings, get_settings
from app.rag.ingestion import IngestionPipeline
from app.rag.pipeline import RagPipeline
from app.rag.request_context import with_system_access
from app.readiness import READINESS_INVALID, readiness_checks, readiness_checks_are_ok
from app.schemas.document import FileStatus
from app.schemas.search import SearchRequest

DEFAULT_SMOKE_QUERY_TEMPLATE = "確認用キーワード {marker} をそのまま引用してください。"


@dataclass(frozen=True)
class SmokeResult:
    """staging smoke test の JSON 出力。"""

    ok: bool
    document_id: str
    marker: str
    query: str
    object_uri: str
    status: str
    chunk_count: int
    citation_count: int
    answer_contains_marker: bool
    trace_id: str
    elapsed_ms: float
    diagnostics: dict[str, object]
    cleanup: dict[str, str]


@dataclass(frozen=True)
class SmokePreflightResult:
    """staging smoke 実行前の非機密 preflight 結果。"""

    ok: bool
    checks: dict[str, str]
    message: str


class StagingSmokeError(RuntimeError):
    """staging smoke の失敗 stage を安全に伝える例外。"""

    def __init__(
        self,
        stage: str,
        cause: Exception,
        *,
        cleanup: dict[str, str] | None = None,
    ) -> None:
        super().__init__(f"staging smoke failed at {stage}: {type(cause).__name__}")
        self.stage = stage
        self.cause_type = type(cause).__name__
        self.cause_details = _safe_cause_details(cause)
        self.cleanup = dict(cleanup or {})


class StagingSmokePreflightError(RuntimeError):
    """外部依存へ接続する前に staging smoke を止める設定エラー。"""

    def __init__(self, preflight: SmokePreflightResult) -> None:
        super().__init__(preflight.message)
        self.preflight = preflight


@with_system_access
def main() -> int:
    """CLI entrypoint。"""
    parser = argparse.ArgumentParser(description="Run OCI / Oracle RAG staging smoke test.")
    parser.add_argument(
        "--query",
        default=DEFAULT_SMOKE_QUERY_TEMPLATE,
        help="検索クエリ。{marker} は今回作成した一意な smoke marker に置換されます。",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="外部依存へ接続せず、staging smoke 実行前の設定チェックだけを JSON 出力する。",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="実行後に作成した Oracle document/chunk と Object Storage object を削除する。",
    )
    args = parser.parse_args()

    if args.preflight_only:
        preflight = staging_smoke_preflight(settings=get_settings())
        print(json.dumps(asdict(preflight), ensure_ascii=False))
        return 0 if preflight.ok else 1

    try:
        result = asyncio.run(
            run_staging_smoke(
                query=args.query,
                cleanup=args.cleanup,
            )
        )
    except Exception as exc:
        print(json.dumps(_error_payload(exc), ensure_ascii=False))
        return 1

    print(json.dumps(asdict(result), ensure_ascii=False))
    return 0 if result.ok else 1


async def run_staging_smoke(
    *,
    query: str = DEFAULT_SMOKE_QUERY_TEMPLATE,
    cleanup: bool = False,
    settings: Settings | None = None,
) -> SmokeResult:
    """staging で RAG の主要外部依存を 1 回ずつ通す。"""
    resolved_settings = settings or get_settings()
    preflight = staging_smoke_preflight(settings=resolved_settings)
    if not preflight.ok:
        raise StagingSmokePreflightError(preflight)

    started_at = datetime.now(UTC)
    suffix = uuid4().hex
    marker = f"SMOKE-{suffix}"
    require_answer_marker = "{marker}" in query
    smoke_query = _format_smoke_query(query, marker)
    body = _smoke_document_body(marker)
    object_key = f"staging-smoke/{suffix}.txt"

    storage = ObjectStorageClient(settings=resolved_settings)
    oracle = OracleClient(settings=resolved_settings)

    stage = "prepare"
    object_uri: str | None = None
    document_id: str | None = None
    indexed_status: str | None = None
    chunk_count = 0
    citation_count = 0
    answer_contains_marker = False
    trace_id = ""
    diagnostics: dict[str, object] = {}
    failure: Exception | None = None
    cleanup_status = _initial_cleanup_status(cleanup)
    try:
        stage = "object_storage_put"
        object_uri = await storage.put(object_key, body, "text/plain")
        stage = "object_storage_get"
        fetched = await storage.get(object_uri)
        if fetched != body:
            raise RuntimeError("Object Storage の put/get roundtrip が一致しません。")

        stage = "oracle_create_document"
        document = await oracle.create_document(
            file_name=f"staging-smoke-{suffix}.txt",
            object_storage_path=object_uri,
            content_type="text/plain",
            file_size_bytes=len(body),
            content_sha256=hashlib.sha256(body).hexdigest(),
        )
        document_id = document.id
        stage = "rag_ingestion"
        indexed = await IngestionPipeline(oracle=oracle, settings=resolved_settings).ingest(
            document.id,
            fetched,
            "staging smoke 用文書を日本語で OCR し、本文テキストを抽出してください。",
            content_type="text/plain",
        )
        if indexed.status != FileStatus.INDEXED:
            raise RuntimeError(f"staging smoke の取込状態が不正です。status={indexed.status}")
        indexed_status = indexed.status.value

        stage = "oracle_count_chunks"
        chunk_count = await oracle.count_document_chunks(document.id)
        if chunk_count < 1:
            raise RuntimeError("staging smoke の索引済み chunk が 0 件です。")

        stage = "rag_search"
        search_response = await RagPipeline(oracle=oracle, settings=resolved_settings).run(
            SearchRequest(
                query=smoke_query,
                top_k=5,
                filters={"document_id": document.id},
            )
        )
        if not any(citation.document_id == document.id for citation in search_response.citations):
            raise RuntimeError("staging smoke 文書が RAG 検索結果に含まれません。")
        citation_count = len(search_response.citations)
        trace_id = search_response.trace_id
        diagnostics = search_response.diagnostics.model_dump()
        stage = "rag_answer_marker"
        answer_contains_marker = marker in search_response.answer
        if require_answer_marker and not answer_contains_marker:
            raise RuntimeError("Enterprise AI LLM の回答に staging smoke marker が含まれません。")
    except Exception as exc:
        failure = exc
    finally:
        if cleanup:
            cleanup_status = await _cleanup_smoke_resources(
                storage=storage,
                oracle=oracle,
                object_uri=object_uri,
                document_id=document_id,
            )
        close_oracle_pool()

    if failure is not None:
        raise StagingSmokeError(
            stage,
            failure,
            cleanup=cleanup_status if cleanup else None,
        ) from failure
    if object_uri is None or document_id is None or indexed_status is None:
        raise StagingSmokeError(
            stage,
            RuntimeError("staging smoke の成功状態が不完全です。"),
            cleanup=cleanup_status if cleanup else None,
        )

    elapsed_ms = (datetime.now(UTC) - started_at).total_seconds() * 1000
    return SmokeResult(
        ok=True,
        document_id=document_id,
        marker=marker,
        query=smoke_query,
        object_uri=object_uri,
        status=indexed_status,
        chunk_count=chunk_count,
        citation_count=citation_count,
        answer_contains_marker=answer_contains_marker,
        trace_id=trace_id,
        elapsed_ms=round(elapsed_ms, 3),
        diagnostics=diagnostics,
        cleanup=cleanup_status,
    )


def staging_smoke_preflight(
    *,
    settings: Settings | None = None,
) -> SmokePreflightResult:
    """staging smoke 前に設定だけを検証する。"""
    resolved_settings = settings or get_settings()
    checks = readiness_checks(resolved_settings)
    if resolved_settings.upload_storage_backend != "oci":
        checks = {**checks, "smoke_object_storage_backend": READINESS_INVALID}

    ok = readiness_checks_are_ok(checks)
    message = (
        "staging smoke preflight ok"
        if ok
        else "staging smoke preflight failed; fix checks before running external smoke"
    )
    return SmokePreflightResult(
        ok=ok,
        checks=checks,
        message=message,
    )


def _format_smoke_query(query_template: str, marker: str) -> str:
    """smoke query template に一意 marker を埋め込む。"""
    if "{marker}" in query_template:
        return query_template.format(marker=marker)
    return query_template


def _smoke_document_body(marker: str) -> bytes:
    """検索で一意に拾いやすい staging smoke 文書を生成する。"""
    text = (
        f"文書番号: {marker}\n"
        "文書種別: staging smoke\n"
        f"確認用キーワード: {marker}\n"
        "この文書は RAG staging smoke test のために自動作成されました。\n"
        "Oracle AI Vector Search と keyword search の疎通確認に使います。"
    )
    return text.encode("utf-8")


def _initial_cleanup_status(cleanup: bool) -> dict[str, str]:
    """smoke 出力用の cleanup 初期状態を返す。"""
    status = "pending" if cleanup else "skipped"
    return {"document": status, "object": status}


async def _cleanup_smoke_resources(
    *,
    storage: ObjectStorageClient,
    oracle: OracleClient,
    object_uri: str | None,
    document_id: str | None,
) -> dict[str, str]:
    """作成済み staging smoke resource を best-effort で削除する。"""
    status: dict[str, str] = {}
    if document_id is None:
        status["document"] = "not_created"
    else:
        try:
            deleted_document = await oracle.delete_document(document_id)
        except Exception:
            status["document"] = "error"
        else:
            status["document"] = "deleted" if deleted_document else "missing"

    if object_uri is None:
        status["object"] = "not_created"
    else:
        try:
            deleted_object = await storage.delete(object_uri)
        except Exception:
            status["object"] = "error"
        else:
            status["object"] = "deleted" if deleted_object else "missing"
    return status


def _error_payload(error: Exception) -> dict[str, object]:
    """CLI 用に機密を含めない失敗 payload を作る。"""
    payload: dict[str, object] = {"ok": False, "error_type": type(error).__name__}
    if isinstance(error, StagingSmokePreflightError):
        payload.update(asdict(error.preflight))
        return payload
    if isinstance(error, StagingSmokeError):
        payload["stage"] = error.stage
        payload["cause_type"] = error.cause_type
        if error.cause_details:
            payload["cause_details"] = error.cause_details
        if error.cleanup:
            payload["cleanup"] = error.cleanup
    return payload


def _safe_cause_details(error: Exception) -> dict[str, str]:
    """OCI SDK 等の低機密診断値だけを取り出す。raw message は含めない。"""
    details: dict[str, str] = {}
    for attr in ("status", "code", "opc_request_id", "request_id"):
        value = getattr(error, attr, None)
        if value is not None:
            details[attr] = str(value)
    return details


if __name__ == "__main__":
    raise SystemExit(main())
