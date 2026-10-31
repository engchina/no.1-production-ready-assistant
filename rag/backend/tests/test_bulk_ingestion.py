"""文書の一括の投入(#2674)の受付のテスト。

保存先は偽の実装(``FakeBulkStore``)にし、判定・永続化・再送の冪等・流量制限・権限・不正な入力と、
受付の結果と永続化した job の一致を確かめる。SQL の形と実 Oracle での動きは
``test_bulk_ingestion_oracle.py``。
"""

from __future__ import annotations

import time
import tracemalloc
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pytest import MonkeyPatch

from app.api.routes import documents as documents_route
from app.config import Settings, get_settings
from app.main import app
from app.rag.bulk_ingestion import (
    BulkIngestionDocumentState,
    BulkInsertResult,
    BulkJobDraft,
    accept_bulk_ingestion,
    normalize_document_ids,
)
from app.rag.rate_limit import RATE_LIMIT_MESSAGE
from app.schemas.document import (
    BULK_INGESTION_MAX_DOCUMENTS,
    BulkIngestionOutcome,
    BulkIngestionResult,
    DocumentProcessingConfig,
    FileStatus,
    IngestionJob,
    IngestionJobPhase,
    IngestionJobStatus,
    SourceProfile,
)
from tests.security_support import ProductionAuth, enable_production_auth, login
from tests.support import AsgiTestClient

client = AsgiTestClient(app)
URL = "/api/documents/ingestion-jobs/bulk"


def _state(document_id: str, **overrides: Any) -> BulkIngestionDocumentState:
    base = BulkIngestionDocumentState(
        document_id=document_id,
        file_name=f"{document_id}.txt",
        status=FileStatus.UPLOADED,
        document_status=FileStatus.UPLOADED,
        has_source=True,
        content_type="text/plain",
        file_size_bytes=10,
        content_sha256="a" * 64,
        recipe_seed={"document_id": document_id, "status": "UPLOADED"},
    )
    return replace(base, **overrides)


class FakeBulkStore:
    """一括の投入の偽の保存先。job を辞書に持ち、Oracle の実装と同じく待機中を確かめ直す。"""

    def __init__(self, states: Sequence[BulkIngestionDocumentState]) -> None:
        self.states = {state.document_id: state for state in states}
        # document_id -> 登録した job(QUEUED)。永続化の正本。
        self.jobs: dict[str, list[IngestionJob]] = {}
        self.load_calls: list[int] = []
        self.insert_calls: list[int] = []
        self.created_recipes: list[str] = []
        self.fail_batches: set[int] = set()
        # 登録の transaction の直前に別の要求が job を作った文書(競合の再現)。
        self.race_active: set[str] = set()

    async def load_states(
        self, document_ids: Sequence[str]
    ) -> Mapping[str, BulkIngestionDocumentState]:
        self.load_calls.append(len(document_ids))
        result: dict[str, BulkIngestionDocumentState] = {}
        for document_id in document_ids:
            state = self.states.get(document_id)
            if state is None:
                continue
            jobs = self.jobs.get(document_id, [])
            result[document_id] = replace(
                state,
                has_job_history=state.has_job_history or bool(jobs),
                has_active_job=state.has_active_job
                or any(job.status == IngestionJobStatus.QUEUED for job in jobs),
            )
        return result

    async def insert_queued_jobs(
        self,
        drafts: Sequence[BulkJobDraft],
        *,
        queued_at: datetime,
        max_attempts: int,
    ) -> BulkInsertResult:
        batch_index = len(self.insert_calls)
        self.insert_calls.append(len(drafts))
        if batch_index in self.fail_batches:
            raise RuntimeError("DPY-4011: the database or network closed the connection")
        created: dict[str, str] = {}
        active: set[str] = set()
        missing: set[str] = set()
        for draft in drafts:
            if draft.document_id not in self.states:
                missing.add(draft.document_id)
                continue
            if draft.document_id in self.race_active or any(
                job.status == IngestionJobStatus.QUEUED
                for job in self.jobs.get(draft.document_id, [])
            ):
                active.add(draft.document_id)
                continue
            if not draft.has_default_recipe:
                self.created_recipes.append(draft.document_id)
            job = IngestionJob(
                id=uuid4().hex,
                document_id=draft.document_id,
                recipe_id=f"recipe-{draft.document_id}",
                recipe_revision=1,
                status=IngestionJobStatus.QUEUED,
                phase=IngestionJobPhase.PREPROCESS,
                parser_profile=draft.parser_profile,
                quality_warnings=list(draft.quality_warnings),
                max_attempts=max_attempts,
                queued_at=queued_at,
            )
            self.jobs.setdefault(draft.document_id, []).append(job)
            created[draft.document_id] = job.id
        return BulkInsertResult(
            created=created, active=frozenset(active), missing=frozenset(missing)
        )

    def queued_document_ids(self) -> list[str]:
        return [
            job.document_id
            for jobs in self.jobs.values()
            for job in jobs
            if job.status == IngestionJobStatus.QUEUED
        ]


def _resolve(config: DocumentProcessingConfig) -> Settings:
    return documents_route._merge_document_processing_config(config)[0]


async def _no_single(document_id: str) -> IngestionJob:
    raise AssertionError(f"1 件の投入を呼ばない: {document_id}")


async def _accept(
    store: FakeBulkStore,
    document_ids: Sequence[str],
    *,
    enqueue_single: Callable[[str], Any] = _no_single,
    parser_message: Callable[[Settings, SourceProfile], Any] | None = None,
    insert_batch_size: int = 500,
) -> BulkIngestionResult:
    return await accept_bulk_ingestion(
        document_ids,
        store=store,
        resolve_settings=_resolve,
        enqueue_single=enqueue_single,
        max_attempts=3,
        parser_message=parser_message,
        insert_batch_size=insert_batch_size,
    )


def _outcomes(result: BulkIngestionResult) -> dict[str, tuple[str, str | None]]:
    return {item.document_id: (item.outcome.value, item.reason) for item in result.items}


def _assert_persisted_matches(result: BulkIngestionResult, store: FakeBulkStore) -> None:
    """受付済みの文書と永続化した job が 1 対 1(欠落 0・重複 0)。"""
    accepted = {
        item.document_id: item.job_id
        for item in result.items
        if item.outcome == BulkIngestionOutcome.ACCEPTED
    }
    queued = store.queued_document_ids()
    assert len(queued) == len(set(queued)), "同じ文書の job が重複している"
    assert set(queued) == set(accepted)
    for document_id, job_id in accepted.items():
        assert [job.id for job in store.jobs[document_id]] == [job_id]
    assert result.accepted_count == len(accepted)


def test_normalize_document_ids_dedupes_and_separates_invalid() -> None:
    valid, invalid = normalize_document_ids(["doc-1", " doc-1 ", "doc-2", "", "bad id", "x" * 65])
    assert valid == ["doc-1", "doc-2"]
    assert invalid == ["", "bad id", "x" * 65]


async def test_fresh_documents_are_persisted_before_the_result() -> None:
    store = FakeBulkStore([_state(f"doc-{index}") for index in range(5)])

    result = await _accept(store, [f"doc-{index}" for index in range(5)])

    assert result.accepted_count == 5
    assert (result.already_active_count, result.skipped_count, result.rejected_count) == (0, 0, 0)
    assert [item.document_id for item in result.items] == [f"doc-{i}" for i in range(5)]
    _assert_persisted_matches(result, store)
    # 文書の状態は 1 回で読み、job は 1 つの batch でまとめて登録する(1 文書ずつ往復しない)。
    assert store.load_calls == [5]
    assert store.insert_calls == [5]


async def test_resend_does_not_register_twice() -> None:
    """通信断の後の再送は、受付済みの文書を「既に処理中」として返し、job を増やさない。"""
    store = FakeBulkStore([_state(f"doc-{index}") for index in range(3)])
    ids = ["doc-0", "doc-1", "doc-2"]

    first = await _accept(store, ids)
    again = await _accept(store, ids)

    assert first.accepted_count == 3
    assert again.accepted_count == 0
    assert again.already_active_count == 3
    assert {item.reason for item in again.items} == {"job_active"}
    assert sorted(store.queued_document_ids()) == ids


async def test_double_submission_race_is_detected_in_the_insert_transaction() -> None:
    """判定の後・登録の前に別の要求が job を作った文書は、登録の transaction で既に処理中にする。"""
    store = FakeBulkStore([_state("doc-a"), _state("doc-b")])
    store.race_active = {"doc-b"}

    result = await _accept(store, ["doc-a", "doc-b"])

    assert _outcomes(result) == {
        "doc-a": ("accepted", None),
        "doc-b": ("already_active", "job_active"),
    }
    _assert_persisted_matches(result, store)


async def test_each_document_gets_an_outcome_and_reason() -> None:
    states = [
        _state("fresh"),
        _state("missing-source", has_source=False),
        _state("running", status=FileStatus.PREPROCESSING),
        _state("queued", has_active_job=True, has_job_history=True),
        _state("indexed", status=FileStatus.INDEXED, has_job_history=True),
        _state("review", status=FileStatus.REVIEW, has_job_history=True),
        _state("dup", duplicate_of_document_id="fresh"),
        _state("binary", file_name="binary.bin", content_type="application/octet-stream"),
        _state("blocked", file_name="blocked.txt"),
        _state("no-recipe", has_default_recipe=False),
    ]
    store = FakeBulkStore(states)

    async def parser_message(settings: Settings, profile: SourceProfile) -> str | None:
        return (
            "解析エンジンで扱えない形式です。"
            if profile.original_file_name == "blocked.txt"
            else None
        )

    ids = [state.document_id for state in states] + ["unknown", "bad id", "fresh"]
    result = await _accept(store, ids, parser_message=parser_message)

    assert _outcomes(result) == {
        "fresh": ("accepted", None),
        "missing-source": ("rejected", "not_found"),
        "running": ("already_active", "processing"),
        "queued": ("already_active", "job_active"),
        "indexed": ("skipped", "already_indexed"),
        "review": ("skipped", "awaiting_confirmation"),
        "dup": ("skipped", "duplicate_content"),
        "binary": ("skipped", "unknown_file_type"),
        "blocked": ("rejected", "parser_unsupported"),
        "no-recipe": ("accepted", None),
        "unknown": ("rejected", "not_found"),
        "bad id": ("rejected", "invalid_document_id"),
    }
    # 要求の数(重複を含む)と、重複を除いた数を分けて返す。
    assert result.requested_count == len(ids)
    assert result.unique_count == len(ids) - 1
    assert (
        result.accepted_count,
        result.already_active_count,
        result.skipped_count,
        result.rejected_count,
    ) == (2, 2, 4, 4)
    blocked = next(item for item in result.items if item.document_id == "blocked")
    assert blocked.message == "解析エンジンで扱えない形式です。"
    assert store.created_recipes == ["no-recipe"]
    reasons = {(entry.outcome.value, entry.reason): entry.count for entry in result.reasons}
    assert reasons[("rejected", "not_found")] == 2
    assert all(entry.message for entry in result.reasons)
    _assert_persisted_matches(result, store)


async def test_documents_with_previous_outputs_use_the_single_enqueue_path() -> None:
    """失敗の後のやり直しなど出力の初期化が要る文書は、1 件の投入と同じ処理で受け付ける。"""
    states = [
        _state("error", status=FileStatus.ERROR, has_job_history=True),
        _state("conflict", status=FileStatus.ERROR, has_job_history=True),
        _state("gone", status=FileStatus.ERROR, has_job_history=True),
        _state("skipped", status=FileStatus.UPLOADED, has_job_history=True),
        _state("broken", status=FileStatus.UPLOADED, has_outputs=True),
    ]
    store = FakeBulkStore(states)
    called: list[str] = []

    async def enqueue_single(document_id: str) -> IngestionJob:
        called.append(document_id)
        if document_id == "conflict":
            raise HTTPException(status_code=409, detail="このドキュメントは現在取込中です。")
        if document_id == "gone":
            raise HTTPException(status_code=404, detail="ドキュメントが見つかりません。")
        if document_id == "broken":
            raise RuntimeError("ORA-03113: end-of-file on communication channel")
        return IngestionJob(
            id=f"job-{document_id}",
            document_id=document_id,
            status=(
                IngestionJobStatus.SKIPPED
                if document_id == "skipped"
                else IngestionJobStatus.QUEUED
            ),
            skip_reason="already_indexed" if document_id == "skipped" else None,
            parser_profile="local_text_structure",
            queued_at=datetime.now(UTC),
        )

    result = await _accept(
        store, [state.document_id for state in states], enqueue_single=enqueue_single
    )

    assert sorted(called) == sorted(state.document_id for state in states)
    assert _outcomes(result) == {
        "error": ("accepted", None),
        "conflict": ("rejected", "conflict"),
        "gone": ("rejected", "not_found"),
        "skipped": ("skipped", "already_indexed"),
        "broken": ("rejected", "temporary_failure"),
    }
    conflict = next(item for item in result.items if item.document_id == "conflict")
    assert conflict.message == "このドキュメントは現在取込中です。"
    assert store.insert_calls == []


async def test_failed_batch_is_reported_and_other_batches_are_kept() -> None:
    """batch の transaction が失敗したら、その batch だけを受付不可にし、全件受付済みにしない。"""
    store = FakeBulkStore([_state(f"doc-{index:02d}") for index in range(10)])
    store.fail_batches = {1}

    result = await _accept(store, [f"doc-{index:02d}" for index in range(10)], insert_batch_size=4)

    assert store.insert_calls == [4, 4, 2]
    assert result.accepted_count == 6
    assert result.rejected_count == 4
    failed = [item.document_id for item in result.items if item.reason == "temporary_failure"]
    assert failed == ["doc-04", "doc-05", "doc-06", "doc-07"]
    _assert_persisted_matches(result, store)

    # 再送すると、失敗した batch の文書だけを受け付け、受付済みの文書は二重に登録しない。
    store.fail_batches = set()
    again = await _accept(store, [f"doc-{index:02d}" for index in range(10)], insert_batch_size=4)
    assert again.accepted_count == 4
    assert again.already_active_count == 6
    assert len(store.queued_document_ids()) == 10


@pytest.mark.parametrize("count", [BULK_INGESTION_MAX_DOCUMENTS], ids=["10000"])
async def test_ten_thousand_documents_are_accepted_without_loss_or_duplicates(count: int) -> None:
    """1 万件の受付で、欠落 0・重複 0。時間と memory を測る(偽の保存先の上の判定・組み立ての分)。"""
    ids = [f"doc-{index:05d}" for index in range(count)]
    store = FakeBulkStore([_state(document_id) for document_id in ids])

    tracemalloc.start()
    started = time.perf_counter()
    result = await _accept(store, ids)
    elapsed = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert result.accepted_count == count
    assert result.unique_count == count
    assert [item.document_id for item in result.items] == ids
    _assert_persisted_matches(result, store)
    assert store.insert_calls == [500] * (count // 500)
    print(f"bulk_ingestion_fake count={count} elapsed={elapsed:.3f}s peak={peak / 1e6:.1f}MB")
    # 偽の保存先では判定と組み立てだけ。CI の runner でも十分に収まる上限で回帰を見る。
    assert elapsed < 30
    assert peak < 200 * 1024 * 1024


# ---------------------------------------------------------------------------
# API(流量制限・不正な入力・権限)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_store(monkeypatch: MonkeyPatch) -> FakeBulkStore:
    store = FakeBulkStore([_state(f"doc-{index:03d}") for index in range(60)])
    monkeypatch.setattr(documents_route, "_bulk_ingestion_store", lambda: store)
    return store


def test_bulk_request_counts_once_for_the_rate_limit(
    fake_store: FakeBulkStore, monkeypatch: MonkeyPatch
) -> None:
    """一括の要求は 1 回として数え、中の各文書が単件の流量制限で途中から落ちない。"""
    settings = get_settings()
    monkeypatch.setattr(settings, "rate_limit_ingest_requests", 2)
    monkeypatch.setattr(settings, "rate_limit_window_seconds", 60.0)
    ids = [f"doc-{index:03d}" for index in range(60)]

    first = client.post(URL, json={"document_ids": ids})
    second = client.post(URL, json={"document_ids": ids})
    third = client.post(URL, json={"document_ids": ids})

    assert first.status_code == 200
    data = first.json()["data"]
    assert data["accepted_count"] == 60
    assert len(fake_store.queued_document_ids()) == 60
    # 再送は 2 回目の 1 回として数え、二重に登録しない。
    assert second.status_code == 200
    assert second.json()["data"]["already_active_count"] == 60
    # 上限を超えた要求は全体を 429 にし、1 件も登録しない(部分的に受け付けない)。
    assert third.status_code == 429
    assert third.headers["retry-after"]
    assert third.json()["error_messages"] == [RATE_LIMIT_MESSAGE]
    assert len(fake_store.queued_document_ids()) == 60


@pytest.mark.parametrize(
    "body",
    [
        {"document_ids": []},
        {"document_ids": ["doc-000"] * (BULK_INGESTION_MAX_DOCUMENTS + 1)},
        {"document_ids": "doc-000"},
        {"document_ids": ["doc-000"], "force": True},
        {},
    ],
    ids=["empty", "over-limit", "not-a-list", "unknown-field", "missing"],
)
def test_invalid_bulk_request_is_rejected_without_registering(
    fake_store: FakeBulkStore, body: dict[str, object]
) -> None:
    response = client.post(URL, json=body)
    assert response.status_code == 422
    assert fake_store.insert_calls == []


def test_bulk_requires_the_document_workspace_permission(
    fake_store: FakeBulkStore, monkeypatch: MonkeyPatch
) -> None:
    auth: ProductionAuth = enable_production_auth(monkeypatch)
    auth.user_with_permissions("searcher", ["menu.search"])
    auth.user_with_permissions("builder", ["menu.file_list"])
    searcher = login(client, "searcher")
    builder = login(client, "builder")

    denied = client.post(URL, json={"document_ids": ["doc-000"]}, headers=searcher)
    assert denied.status_code == 403
    assert fake_store.insert_calls == []

    allowed = client.post(URL, json={"document_ids": ["doc-000"]}, headers=builder)
    assert allowed.status_code == 200
    assert allowed.json()["data"]["accepted_count"] == 1


class _IdsOracle:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def list_document_ids(self, **kwargs: object) -> tuple[list[str], int]:
        self.calls.append(kwargs)
        return ["doc-2", "doc-1"], 2


def test_document_ids_use_the_list_filters(monkeypatch: MonkeyPatch) -> None:
    oracle = _IdsOracle()
    monkeypatch.setattr(documents_route, "OracleClient", lambda: oracle)

    response = client.get(
        "/api/documents/ids",
        params={"status": "UPLOADED", "q": "report", "knowledge_base_id": "kb-1"},
    )

    assert response.status_code == 200
    assert response.json()["data"] == {"ids": ["doc-2", "doc-1"], "total": 2, "limit": 10000}
    assert oracle.calls == [
        {
            "status": FileStatus.UPLOADED,
            "query": "report",
            "knowledge_base_id": "kb-1",
            "queued_phase": None,
            "limit": BULK_INGESTION_MAX_DOCUMENTS,
        }
    ]
    assert client.get("/api/documents/ids", params={"limit": 10001}).status_code == 422


def test_rejected_formats_keep_their_own_message_and_count() -> None:
    from app.rag.bulk_ingestion import _Decision, _result

    xlsx = "既定の解析エンジンは .xlsx を処理できません。Excel→JSONL を選んでください。"
    markdown = "既定の解析エンジンは .md を処理できません。別の解析エンジンを選んでください。"
    decisions = {
        "xlsx-a": _Decision(
            outcome=BulkIngestionOutcome.REJECTED, reason="parser_unsupported_source", message=xlsx
        ),
        "md-a": _Decision(
            outcome=BulkIngestionOutcome.REJECTED,
            reason="parser_unsupported_source",
            message=markdown,
        ),
        "md-b": _Decision(
            outcome=BulkIngestionOutcome.REJECTED,
            reason="parser_unsupported_source",
            message=markdown,
        ),
    }
    result = _result(raw_count=3, ordered_ids=list(decisions), decisions=decisions)
    assert result.rejected_count == 3
    assert {entry.message: entry.count for entry in result.reasons} == {xlsx: 1, markdown: 2}
