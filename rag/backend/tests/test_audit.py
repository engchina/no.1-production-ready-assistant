"""RAG 監査イベントのテスト。"""

import logging
from typing import Any, cast

from pytest import LogCaptureFixture

from app.rag.audit import record_rag_ingestion_audit, record_rag_search_audit
from app.rag.guardrails import GuardrailFinding
from app.rag.request_context import (
    AuditRequestContext,
    reset_audit_request_context,
    set_audit_request_context,
)
from app.schemas.search import RetrievedChunk, SearchDiagnostics, SearchMode


def test_rag_search_audit_redacts_query_text(caplog: LogCaptureFixture) -> None:
    """監査ログは query 本文を出さず、hash とメタデータだけを残す。"""
    query = "社内規程番号 INV-001 の金額を教えて"

    with caplog.at_level(logging.INFO, logger="app.audit"):
        event = record_rag_search_audit(
            trace_id="trace-1",
            outcome="success",
            sanitized_query=query,
            filters={"status": "INDEXED", "file_name": "policy"},
            findings=[
                GuardrailFinding(
                    code="sql_mutation_intent",
                    severity="warning",
                    message="検索のみ実行します。",
                )
            ],
            retrieved_count=3,
            citations=[
                RetrievedChunk(
                    document_id="doc-a",
                    chunk_id="doc-a:0",
                    text="A",
                    score=0.9,
                ),
                RetrievedChunk(
                    document_id="doc-a",
                    chunk_id="doc-a:1",
                    text="A2",
                    score=0.8,
                ),
                RetrievedChunk(
                    document_id="doc-b",
                    chunk_id="doc-b:0",
                    text="B",
                    score=0.7,
                ),
            ],
            elapsed_ms=12.3,
            diagnostics=SearchDiagnostics(config_fingerprint="fp-audit"),
        )

    assert event.query_chars == len(query)
    assert event.query_hash
    assert event.query_hash != query
    assert event.filter_keys == ["file_name", "status"]
    assert event.guardrail_codes == ["sql_mutation_intent"]
    assert event.config_fingerprint == "fp-audit"
    # 回答は回答フロー(hybrid + RRF)だけ。旧 standard の計測の列は既定値のまま(列の削除は #596)。
    assert event.mode == SearchMode.HYBRID
    assert event.memory_plan_id is None
    assert event.query_variant_count == 1
    assert event.context_expanded_count == 0
    assert event.agent_memory_writeback_status == "skipped"
    assert event.document_ids == ["doc-a", "doc-b"]

    record = next(item for item in caplog.records if item.message == "rag_search_audit")
    logged = cast(Any, record).audit_event
    assert logged["query_hash"] == event.query_hash
    assert logged["config_fingerprint"] == "fp-audit"
    assert logged["mode"] == "hybrid"
    assert "INV-001" not in str(logged)
    assert query not in str(logged)


def test_rag_search_audit_records_error_type_without_error_message(
    caplog: LogCaptureFixture,
) -> None:
    """検索失敗時も例外本文を出さず、stage/type だけを残す。"""
    query = "秘密の社内規程番号 INV-SECRET"

    with caplog.at_level(logging.INFO, logger="app.audit"):
        event = record_rag_search_audit(
            trace_id="trace-error",
            outcome="error",
            sanitized_query=query,
            filters={},
            findings=[],
            retrieved_count=0,
            citations=[],
            elapsed_ms=4.2,
            error=RuntimeError("raw secret detail"),
            error_stage="embedding",
        )

    assert event.outcome == "error"
    assert event.error_stage == "embedding"
    assert event.error_type == "RuntimeError"

    record = next(item for item in caplog.records if item.message == "rag_search_audit")
    logged = cast(Any, record).audit_event
    assert logged["error_stage"] == "embedding"
    assert logged["error_type"] == "RuntimeError"
    assert "raw secret detail" not in str(logged)
    assert "INV-SECRET" not in str(logged)


def test_rag_search_audit_includes_hashed_request_context(
    caplog: LogCaptureFixture,
) -> None:
    """監査ログは現在の request context を raw id なしで含める。"""
    token = set_audit_request_context(
        AuditRequestContext(
            request_id="request-1",
            tenant_id_hash="a" * 64,
            user_id_hash="b" * 64,
        )
    )
    try:
        with caplog.at_level(logging.INFO, logger="app.audit"):
            event = record_rag_search_audit(
                trace_id="trace-context",
                outcome="no_results",
                sanitized_query="社内規程",
                filters={},
                findings=[],
                retrieved_count=0,
                citations=[],
                elapsed_ms=1.0,
            )
    finally:
        reset_audit_request_context(token)

    assert event.request_id == "request-1"
    assert event.tenant_id_hash == "a" * 64
    assert event.user_id_hash == "b" * 64

    record = next(item for item in caplog.records if item.message == "rag_search_audit")
    logged = cast(Any, record).audit_event
    assert logged["request_id"] == "request-1"
    assert logged["tenant_id_hash"] == "a" * 64
    assert logged["user_id_hash"] == "b" * 64


def test_rag_ingestion_audit_includes_hashed_request_context(
    caplog: LogCaptureFixture,
) -> None:
    """取込監査ログにも request context を含める。"""
    token = set_audit_request_context(
        AuditRequestContext(
            request_id="request-ingestion",
            tenant_id_hash="c" * 64,
            user_id_hash="d" * 64,
        )
    )
    try:
        with caplog.at_level(logging.INFO, logger="app.audit"):
            event = record_rag_ingestion_audit(
                trace_id="trace-ingestion",
                document_id="doc-1",
                outcome="success",
                source_bytes=b"policy bytes",
                parser_backend="local_partition",
                parser_profile="markdown_by_heading",
                segment_count=3,
                fallback_count=0,
                failed_segment_count=1,
                elapsed_ms=2.0,
            )
    finally:
        reset_audit_request_context(token)

    assert event.request_id == "request-ingestion"
    assert event.tenant_id_hash == "c" * 64
    assert event.user_id_hash == "d" * 64
    assert event.parser_backend == "local_partition"
    assert event.parser_profile == "markdown_by_heading"
    assert event.segment_count == 3
    assert event.fallback_count == 0
    assert event.failed_segment_count == 1

    record = next(item for item in caplog.records if item.message == "rag_ingestion_audit")
    logged = cast(Any, record).audit_event
    assert logged["request_id"] == "request-ingestion"
    assert logged["tenant_id_hash"] == "c" * 64
    assert logged["user_id_hash"] == "d" * 64
    assert logged["parser_backend"] == "local_partition"
    assert logged["segment_count"] == 3
    assert logged["failed_segment_count"] == 1
    assert "raw_text" not in logged
    assert "ocr_text" not in logged
