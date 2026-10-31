"""Incremental NL2SQL state / lazy startup regression tests。"""

from __future__ import annotations

import importlib
import io
import json
import sys
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import FastAPI, HTTPException, Request, Response
from pr_backend_core.api import CursorParams

from app.features.nl2sql.incremental_store import (
    IncrementalVersionConflict,
    MemoryIncrementalNl2SqlRepository,
    OracleIncrementalNl2SqlRepository,
    VersionedTtlCache,
    _read_lob,
)
from app.features.nl2sql.models import (
    DEFAULT_FEEDBACK_SIMILARITY_THRESHOLD,
    AdminFeedbackReviewRequest,
    ClassifierTrainingExample,
    FeedbackIndexRequest,
    FeedbackRating,
    FeedbackSearchConfigRequest,
    HistoryItem,
    JobCreateRequest,
    JobStatus,
    Nl2SqlEngine,
    Nl2SqlProfile,
    PreviewRequest,
    ProfilePatchRequest,
    SchemaCatalog,
    SchemaColumn,
    SchemaRefreshJob,
    SchemaRefreshJobStatus,
    SchemaRefreshMode,
    SchemaRefreshTargetObject,
    SchemaTable,
    SchemaViewDependency,
    SimilarHistoryRequest,
)
from app.features.nl2sql.oracle_adapter import OracleAdapterError, OracleNl2SqlAdapter
from app.features.nl2sql.service import (
    Nl2SqlPersistenceUnavailable,
    Nl2SqlRepositoryOperationFailed,
    Nl2SqlService,
    StoredJob,
    _new_job_steps,
)
from app.features.nl2sql.store import MemoryNl2SqlStore
from app.settings import Settings, get_settings


def _profile(index: int) -> Nl2SqlProfile:
    return Nl2SqlProfile(
        id=f"profile-{index:04d}",
        name=f"業務プロファイル {index:04d}",
        category="sales" if index % 2 else "finance",
        allowed_tables=[f"APP.TABLE_{index:04d}"],
    )


def _table(name: str, *, comment: str = "") -> SchemaTable:
    return SchemaTable(
        owner="APP",
        table_name=name,
        logical_name=name,
        comment=comment,
        columns=[
            SchemaColumn(
                column_name="ID",
                logical_name="ID",
                data_type="NUMBER",
                nullable=False,
            )
        ],
    )


def _history(index: int, *, admin: FeedbackRating | None = None) -> HistoryItem:
    return HistoryItem(
        id=f"hist-{index:03d}",
        question=f"請求金額を確認したい {index}",
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        generated_sql="SELECT TOTAL_AMOUNT FROM APP.INVOICES",
        created_at=f"2026-09-02T00:{index // 60:02d}:{index % 60:02d}+00:00",
        profile_id="default",
        profile_name="標準プロファイル",
        feedback_rating=FeedbackRating.GOOD,
        feedback_comment="正しい SQL",
        safety_is_safe=True,
        admin_feedback_rating=admin,
        admin_feedback_content="管理者確認済み" if admin else "",
        admin_feedback_updated_at=(
            f"2026-09-02T01:{index // 60:02d}:{index % 60:02d}+00:00" if admin else ""
        ),
    )


def _seed_history(
    repository: MemoryIncrementalNl2SqlRepository,
    items: list[HistoryItem],
) -> None:
    for item in items:
        repository.put_document(
            "history",
            item.id,
            item.model_dump(mode="json"),
            profile_id=item.profile_id,
            status=item.feedback_rating.value if item.feedback_rating else "unrated",
        )


class _FeedbackEmbeddingClient:
    def is_configured(self) -> bool:
        return True

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for index, _text in enumerate(texts):
            vector = [0.0] * 1536
            vector[index % 1536] = 1.0
            vectors.append(vector)
        return vectors


class _FeedbackVectorAdapter:
    def __init__(self) -> None:
        self.rebuild_rows: list[dict[str, Any]] = []
        self.clear_calls: list[tuple[str, str]] = []
        self.deleted_history_ids: list[str] = []
        self.vector_history_ids: set[str] = set()

    def rebuild_feedback_vector_index(
        self,
        *,
        table_name: str,
        index_name: str,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        del table_name, index_name
        self.rebuild_rows = list(rows)
        self.vector_history_ids = {str(row["history_id"]) for row in rows}
        return {"runtime": "oracle", "row_count": len(rows)}

    def upsert_feedback_vector_entry(
        self,
        *,
        table_name: str,
        index_name: str,
        row: dict[str, Any],
    ) -> dict[str, Any]:
        del table_name, index_name
        history_id = str(row["history_id"])
        self.vector_history_ids.add(history_id)
        return {"runtime": "oracle", "history_id": history_id, "executed": True}

    def clear_feedback_vector_index(
        self,
        *,
        table_name: str,
        index_name: str,
    ) -> dict[str, Any]:
        self.clear_calls.append((table_name, index_name))
        self.vector_history_ids = set()
        return {"runtime": "oracle"}

    def delete_feedback_vector_entry(
        self,
        *,
        table_name: str,
        history_id: str,
    ) -> dict[str, Any]:
        del table_name
        self.deleted_history_ids.append(history_id)
        self.vector_history_ids.discard(history_id)
        return {"runtime": "oracle", "history_id": history_id, "executed": True}


class _FailingFeedbackVectorAdapter(_FeedbackVectorAdapter):
    def rebuild_feedback_vector_index(
        self,
        *,
        table_name: str,
        index_name: str,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        del table_name, index_name, rows
        raise OracleAdapterError("rebuild failed")


class _BlockingFeedbackVectorAdapter(_FeedbackVectorAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def rebuild_feedback_vector_index(
        self,
        *,
        table_name: str,
        index_name: str,
        rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("blocking rebuild was not released")
        return super().rebuild_feedback_vector_index(
            table_name=table_name,
            index_name=index_name,
            rows=rows,
        )


class _FakeEnterpriseAiClient:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict[str, str]] = []

    def is_configured(self) -> bool:
        return True

    def model_id(self) -> str:
        return "enterprise-nl2sql-model"

    def generate(
        self,
        *,
        prompt: str,
        context: str,
        system_prompt: str,
        timeout_seconds: float | None = None,
        max_output_tokens: int | None = None,
        max_retries: int | None = None,
        response_format: object | None = None,
    ) -> str:
        del timeout_seconds, max_output_tokens, max_retries
        self.calls.append({"prompt": prompt, "context": context, "system_prompt": system_prompt})
        return self.text


class _LobPayload:
    """Scripted cursor が connection-bound LOB に変換する値。"""

    def __init__(self, value: str) -> None:
        self.value = value


class _ConnectionBoundLob:
    def __init__(self, connection: _ScriptedOracleConnection, value: str) -> None:
        self._connection = connection
        self._value = value

    def read(self) -> str:
        if self._connection.closed:
            raise RuntimeError("LOB locator is no longer valid")
        return self._value


class _ScriptedOracleCursor:
    def __init__(
        self,
        connection: _ScriptedOracleConnection,
        result_sets: list[list[tuple[Any, ...]]],
    ) -> None:
        self._connection = connection
        self._result_sets = result_sets
        self._current: list[tuple[Any, ...]] = []
        self.prefetchrows: int | None = None
        self.arraysize: int | None = None

    def __enter__(self) -> _ScriptedOracleCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _sql: str, _binds: Any = None) -> None:
        self._connection.executed.append((_sql, _binds))
        if not self._result_sets:
            raise AssertionError("scripted result set is missing")
        self._current = list(self._result_sets.pop(0))

    def setinputsizes(self, **kwargs: Any) -> None:
        self._connection.input_sizes.append(kwargs)

    def fetchone(self) -> tuple[Any, ...] | None:
        if not self._current:
            return None
        return self._materialize(self._current.pop(0))

    def fetchall(self) -> list[tuple[Any, ...]]:
        rows = [self._materialize(row) for row in self._current]
        self._current = []
        return rows

    def _materialize(self, row: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(
            (
                _ConnectionBoundLob(self._connection, value.value)
                if isinstance(value, _LobPayload)
                else value
            )
            for value in row
        )


class _ScriptedOracleConnection:
    def __init__(self, result_sets: list[list[tuple[Any, ...]]]) -> None:
        self.closed = False
        self.executed: list[tuple[str, Any]] = []
        self.input_sizes: list[dict[str, Any]] = []
        self._cursor = _ScriptedOracleCursor(self, result_sets)

    def cursor(self) -> _ScriptedOracleCursor:
        return self._cursor

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


def _oracle_repository(
    *connection_results: list[list[tuple[Any, ...]]],
) -> tuple[OracleIncrementalNl2SqlRepository, list[_ScriptedOracleConnection]]:
    pending = list(connection_results)
    connections: list[_ScriptedOracleConnection] = []

    @contextmanager
    def connection_factory() -> Iterator[_ScriptedOracleConnection]:
        if not pending:
            raise AssertionError("scripted connection is missing")
        connection = _ScriptedOracleConnection(pending.pop(0))
        connections.append(connection)
        try:
            yield connection
        finally:
            connection.closed = True

    return OracleIncrementalNl2SqlRepository(connection_factory=connection_factory), connections


def _incremental_service(
    repository: Any,
) -> Nl2SqlService:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._incremental_repository = repository  # noqa: SLF001 - white-box contract test
    service._refresh_job_repository = repository  # noqa: SLF001
    service._persistence_ready = True  # noqa: SLF001
    service._persistence_writable = True  # noqa: SLF001
    service._cache_token_poll_seconds = 0.0  # noqa: SLF001
    return service


def _apply_incremental_catalog(
    repository: MemoryIncrementalNl2SqlRepository,
    catalog: SchemaCatalog,
) -> None:
    manifest = {
        (table.owner.upper(), table.table_name.upper()): catalog.refreshed_at
        for table in catalog.tables
    }
    repository.apply_schema_refresh(
        catalog=catalog,
        manifest=manifest,
        changed_keys=set(manifest),
        deleted_keys=set(),
    )


def _single_sheet_workbook_bytes(title: str, rows: list[list[Any]]) -> bytes:
    openpyxl = importlib.import_module("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_incremental_legacy_learning_material_is_restored_lazily_after_restart() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    first = _incremental_service(repository)
    first.import_legacy_terms(
        filename="terms.xlsx",
        content=_single_sheet_workbook_bytes(
            "terms",
            [["TERM", "DEFINITION"], ["売上", "INVOICES.TOTAL_AMOUNT"]],
        ),
    )
    first.import_legacy_rules(
        filename="rules.xlsx",
        content=_single_sheet_workbook_bytes("rules", [["RULE"], ["SELECT のみ"]]),
    )

    restarted = _incremental_service(repository)

    assert restarted._legacy_learning_material.glossary == {}  # noqa: SLF001
    material = restarted.get_legacy_learning_material()
    assert material.glossary == {"売上": "INVOICES.TOTAL_AMOUNT"}
    assert material.rules == ["SELECT のみ"]


def test_incremental_profile_learning_material_missing_profile_maps_to_key_error() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    service = _incremental_service(repository)

    with pytest.raises(KeyError):
        service.import_profile_learning_material(
            profile_id="missing-profile",
            filename="terms.xlsx",
            content=_single_sheet_workbook_bytes(
                "terms",
                [["TERM", "DEFINITION"], ["売上", "INVOICES.TOTAL_AMOUNT"]],
            ),
        )


def test_incremental_legacy_import_preserves_counterpart_and_other_singletons() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    repository.put_document(
        "singletons",
        "legacy_learning_material",
        {
            "value": {
                "glossary": {"売上": "INVOICES.TOTAL_AMOUNT"},
                "rules": ["SELECT のみ"],
            }
        },
    )
    feedback_config = {"value": {"similarity_threshold": 0.75, "match_limit": 7}}
    repository.put_document(
        "singletons",
        "feedback_search_config",
        feedback_config,
    )

    terms_service = _incremental_service(repository)
    terms = terms_service.import_legacy_terms(
        filename="terms.xlsx",
        content=_single_sheet_workbook_bytes(
            "terms",
            [["TERM", "DEFINITION"], ["粗利", "INVOICES.PROFIT"]],
        ),
    )
    assert terms.glossary == {"粗利": "INVOICES.PROFIT"}
    assert terms.rules == ["SELECT のみ"]
    assert repository.get_document("singletons", "feedback_search_config") == feedback_config

    rules_service = _incremental_service(repository)
    rules = rules_service.import_legacy_rules(
        filename="rules.xlsx",
        content=_single_sheet_workbook_bytes(
            "rules",
            [["RULE"], ["集計時は NULL を除外する"]],
        ),
    )
    assert rules.glossary == {"粗利": "INVOICES.PROFIT"}
    assert rules.rules == ["集計時は NULL を除外する"]
    assert repository.get_document("singletons", "feedback_search_config") == feedback_config


def test_incremental_feedback_rebuild_uses_repository_history_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_runtime_mode", "oracle")
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    histories = [_history(index, admin=FeedbackRating.GOOD) for index in range(200)]
    _seed_history(repository, histories)
    service = _incremental_service(repository)
    cast(Any, service)._embedding_client = _FeedbackEmbeddingClient()
    adapter = _FeedbackVectorAdapter()
    cast(Any, service)._oracle_adapter = adapter

    assert service._history == []  # noqa: SLF001

    data = service.rebuild_feedback_index(FeedbackIndexRequest())

    assert data.executed is True
    assert data.source_history_count == 200
    assert data.indexable_count == 200
    assert data.indexed_count == 200
    assert data.status == "ready"
    assert len(adapter.rebuild_rows) == 200
    assert {row["history_id"] for row in adapter.rebuild_rows} == {item.id for item in histories}
    indexed = repository.get_document("singletons", "feedback_indexed_ids")
    assert indexed is not None
    assert len(indexed["value"]) == 200


def test_incremental_feedback_rebuild_excludes_unsafe_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_runtime_mode", "oracle")
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    safe = _history(1, admin=FeedbackRating.GOOD)
    unsafe = _history(2, admin=FeedbackRating.GOOD).model_copy(update={"safety_is_safe": False})
    _seed_history(repository, [safe, unsafe])
    service = _incremental_service(repository)
    cast(Any, service)._embedding_client = _FeedbackEmbeddingClient()
    adapter = _FeedbackVectorAdapter()
    cast(Any, service)._oracle_adapter = adapter

    data = service.rebuild_feedback_index(FeedbackIndexRequest())

    assert data.executed is True
    assert data.indexable_count == 1
    assert data.indexed_count == 1
    assert {row["history_id"] for row in adapter.rebuild_rows} == {safe.id}
    assert adapter.vector_history_ids == {safe.id}


def test_incremental_feedback_publish_waits_for_rebuild_and_keeps_index_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_runtime_mode", "oracle")
    monkeypatch.setattr(settings, "nl2sql_feedback_embedding_enabled", True)
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    indexed = _history(1, admin=FeedbackRating.GOOD)
    pending = _history(2).model_copy(
        update={
            "admin_feedback_rating": None,
            "admin_feedback_content": "",
            "admin_feedback_updated_at": "",
        }
    )
    _seed_history(repository, [indexed, pending])
    service = _incremental_service(repository)
    cast(Any, service)._embedding_client = _FeedbackEmbeddingClient()
    adapter = _BlockingFeedbackVectorAdapter()
    cast(Any, service)._oracle_adapter = adapter

    with ThreadPoolExecutor(max_workers=2) as executor:
        rebuild = executor.submit(service.rebuild_feedback_index, FeedbackIndexRequest())
        assert adapter.entered.wait(timeout=5)
        publish = executor.submit(
            service.save_admin_feedback_review,
            AdminFeedbackReviewRequest(
                history_id=pending.id,
                rating=FeedbackRating.GOOD,
                feedback_content="管理者確認済み",
            ),
        )
        time.sleep(0.05)
        assert not publish.done()
        adapter.release.set()
        rebuild_data = rebuild.result(timeout=5)
        publish_data = publish.result(timeout=5)

    assert rebuild_data.executed is True
    assert publish_data.similar_history_publish is not None
    assert publish_data.similar_history_publish.status == "published"
    assert adapter.vector_history_ids == {indexed.id, pending.id}
    stored_index = repository.get_document("singletons", "feedback_indexed_ids")
    assert stored_index == {"value": [indexed.id, pending.id]}


def test_incremental_feedback_double_rebuild_keeps_index_state_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_runtime_mode", "oracle")
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    histories = [_history(1, admin=FeedbackRating.GOOD), _history(2, admin=FeedbackRating.GOOD)]
    _seed_history(repository, histories)
    first = _incremental_service(repository)
    second = _incremental_service(repository)
    adapter = _FeedbackVectorAdapter()
    for service in [first, second]:
        cast(Any, service)._embedding_client = _FeedbackEmbeddingClient()
        cast(Any, service)._oracle_adapter = adapter

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda service: service.rebuild_feedback_index(FeedbackIndexRequest()),
                [first, second],
            )
        )

    assert all(result.executed for result in results)
    expected_ids = {item.id for item in histories}
    assert adapter.vector_history_ids == expected_ids
    stored_index = repository.get_document("singletons", "feedback_indexed_ids")
    assert stored_index == {"value": sorted(expected_ids)}


def test_incremental_feedback_state_is_restored_lazily_after_restart() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    histories = [_history(1, admin=FeedbackRating.GOOD), _history(2, admin=FeedbackRating.GOOD)]
    _seed_history(repository, histories)
    repository.put_document(
        "singletons",
        "feedback_search_config",
        {"value": {"similarity_threshold": 0.65, "match_limit": 9}},
    )
    repository.put_document(
        "singletons",
        "feedback_indexed_ids",
        {"value": [item.id for item in histories]},
    )

    restarted = _incremental_service(repository)

    assert restarted._feedback_similarity_threshold == DEFAULT_FEEDBACK_SIMILARITY_THRESHOLD  # noqa: SLF001
    assert restarted._feedback_indexed_ids == set()  # noqa: SLF001
    config = restarted.feedback_search_config()
    entries = restarted.list_feedback_entries()

    assert config.similarity_threshold == 0.65
    assert config.match_limit == 9
    assert entries.indexed_count == 2
    assert {item.history_id for item in entries.items if item.indexed} == {
        item.id for item in histories
    }


def test_incremental_feedback_search_config_defaults_to_0_9_when_not_saved() -> None:
    # 未保存の環境と、最低スコアを持たない保存値では、最低スコアの既定 0.9 を使う（#1721）。
    assert DEFAULT_FEEDBACK_SIMILARITY_THRESHOLD == 0.9
    unsaved = _incremental_service(MemoryIncrementalNl2SqlRepository(seed_default=False))
    assert unsaved.feedback_search_config().similarity_threshold == 0.9
    assert unsaved.feedback_search_config().match_limit == 3

    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    repository.put_document("singletons", "feedback_search_config", {"value": {"match_limit": 5}})
    partial = _incremental_service(repository).feedback_search_config()
    assert partial.similarity_threshold == 0.9
    assert partial.match_limit == 5


def test_incremental_feedback_search_config_patch_preserves_omitted_fields() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    repository.put_document(
        "singletons",
        "feedback_search_config",
        {"value": {"similarity_threshold": 0.65, "match_limit": 9}},
    )
    service = _incremental_service(repository)

    first = service.update_feedback_search_config(FeedbackSearchConfigRequest(match_limit=5))
    second = service.update_feedback_search_config(
        FeedbackSearchConfigRequest(similarity_threshold=0.8)
    )

    assert first.similarity_threshold == 0.65
    assert first.match_limit == 5
    assert second.similarity_threshold == 0.8
    assert second.match_limit == 5
    assert repository.get_document("singletons", "feedback_search_config") == {
        "value": {"similarity_threshold": 0.8, "match_limit": 5}
    }


def test_incremental_user_feedback_and_admin_review_preserve_each_other() -> None:
    class _StaleHistoryReadRepository(MemoryIncrementalNl2SqlRepository):
        def __init__(self, stale_document: dict[str, Any]) -> None:
            super().__init__(seed_default=False)
            self._stale_document = stale_document
            self._return_stale_once = False

        def return_stale_history_once(self) -> None:
            self._return_stale_once = True

        def get_document(self, collection: str, entity_id: str) -> dict[str, Any] | None:
            if (
                collection == "history"
                and entity_id == self._stale_document["id"]
                and self._return_stale_once
            ):
                self._return_stale_once = False
                return dict(self._stale_document)
            return super().get_document(collection, entity_id)

    original = _history(1).model_copy(
        update={
            "feedback_rating": None,
            "feedback_comment": "",
            "feedback_updated_at": "",
            "admin_feedback_rating": None,
            "admin_feedback_content": "",
            "admin_feedback_updated_at": "",
        }
    )
    repository = _StaleHistoryReadRepository(original.model_dump(mode="json"))
    _seed_history(repository, [original])
    user_service = _incremental_service(repository)
    admin_service = _incremental_service(repository)

    user_service.save_feedback(original.id, FeedbackRating.BAD, "利用者の指摘")
    repository.return_stale_history_once()
    admin_service.save_admin_feedback_review(
        AdminFeedbackReviewRequest(
            history_id=original.id,
            rating=FeedbackRating.GOOD,
            feedback_content="管理者確認済み",
        )
    )

    stored = HistoryItem.model_validate(repository.get_document("history", original.id))
    assert stored.feedback_rating == FeedbackRating.BAD
    assert stored.feedback_comment == "利用者の指摘"
    assert stored.admin_feedback_rating == FeedbackRating.GOOD
    assert stored.admin_feedback_content == "管理者確認済み"


def test_incremental_feedback_review_keeps_created_at_order_and_offset_page() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    histories = [
        _history(0),
        _history(1),
        _history(2),
    ]
    _seed_history(repository, histories)
    service = _incremental_service(repository)

    first = service.list_feedback(limit=2, rating="", profile_id="", query="")
    second = service.list_feedback(
        offset=2,
        limit=2,
        rating="",
        profile_id="",
        query="",
    )
    assert [item.id for item in first.items] == ["hist-002", "hist-001"]
    assert [item.id for item in second.items] == ["hist-000"]

    service.save_admin_feedback_review(
        AdminFeedbackReviewRequest(
            history_id="hist-000",
            rating=FeedbackRating.GOOD,
            feedback_content="2 ページ目で確認",
        )
    )

    first_after = service.list_feedback(limit=2, rating="", profile_id="", query="")
    second_after = service.list_feedback(
        offset=2,
        limit=2,
        rating="",
        profile_id="",
        query="",
    )
    assert [item.id for item in first_after.items] == ["hist-002", "hist-001"]
    assert [item.id for item in second_after.items] == ["hist-000"]
    assert second_after.items[0].admin_feedback_content == "2 ページ目で確認"


def test_incremental_feedback_list_pushes_allowed_profiles_into_page_query() -> None:
    class _ScopedPageRepository(MemoryIncrementalNl2SqlRepository):
        def __init__(self) -> None:
            super().__init__(seed_default=False)
            self.history_page_calls = 0
            self.profile_id_scopes: list[set[str] | None] = []
            self.count_scopes: list[tuple[set[str] | None, dict[str, str] | None]] = []

        def list_documents_page(  # type: ignore[override]
            self,
            collection: str,
            *,
            cursor: str | None,
            limit: int,
            profile_id: str = "",
            status: str = "",
            query: str = "",
            payload_filters: dict[str, str] | None = None,
            payload_any_filters: dict[str, str] | None = None,
            payload_exclude_filters: dict[str, str] | None = None,
            profile_ids: Iterable[str] | None = None,
            payload_present_filters: Mapping[str, bool] | None = None,
            created_since: str = "",
            sort_key: str = "created_at",
            direction: str = "desc",
            offset: int | None = None,
        ) -> tuple[list[dict[str, object]], str | None, int]:
            if collection == "history":
                self.history_page_calls += 1
                self.profile_id_scopes.append(None if profile_ids is None else set(profile_ids))
            return super().list_documents_page(
                collection,
                cursor=cursor,
                limit=limit,
                profile_id=profile_id,
                status=status,
                query=query,
                payload_filters=payload_filters,
                payload_any_filters=payload_any_filters,
                payload_exclude_filters=payload_exclude_filters,
                profile_ids=profile_ids,
                payload_present_filters=payload_present_filters,
                created_since=created_since,
                sort_key=sort_key,
                direction=direction,
                offset=offset,
            )

        def count_documents(  # type: ignore[override]
            self,
            collection: str,
            *,
            profile_id: str = "",
            status: str = "",
            query: str = "",
            payload_filters: dict[str, str] | None = None,
            payload_any_filters: dict[str, str] | None = None,
            payload_exclude_filters: dict[str, str] | None = None,
            profile_ids: Iterable[str] | None = None,
            payload_present_filters: Mapping[str, bool] | None = None,
            created_since: str = "",
        ) -> int:
            self.count_scopes.append(
                (None if profile_ids is None else set(profile_ids), payload_filters)
            )
            return super().count_documents(
                collection,
                profile_id=profile_id,
                status=status,
                query=query,
                payload_filters=payload_filters,
                payload_any_filters=payload_any_filters,
                payload_exclude_filters=payload_exclude_filters,
                profile_ids=profile_ids,
                payload_present_filters=payload_present_filters,
                created_since=created_since,
            )

    repository = _ScopedPageRepository()
    histories = [
        _history(0).model_copy(update={"profile_id": "profile-a", "profile_name": "A"}),
        _history(1).model_copy(update={"profile_id": "profile-b", "profile_name": "B"}),
        _history(2).model_copy(update={"profile_id": "profile-a", "profile_name": "A"}),
    ]
    _seed_history(repository, histories)
    service = _incremental_service(repository)

    page = service.list_feedback(
        limit=1,
        rating="",
        profile_id="",
        query="",
        allowed_profile_ids={"profile-a"},
    )

    assert [item.id for item in page.items] == ["hist-002"]
    assert page.total == page.unfiltered_total == 2
    assert page.has_next
    assert repository.history_page_calls == 1
    assert repository.profile_id_scopes == [{"profile-a"}]
    # 絞り込みが無いときは「全 M 件」を数え直さない。
    assert repository.count_scopes == []

    filtered = service.list_feedback(
        limit=1,
        rating="",
        profile_id="",
        query="確認したい 0",
        allowed_profile_ids={"profile-a"},
    )

    assert [item.id for item in filtered.items] == ["hist-000"]
    assert (filtered.total, filtered.unfiltered_total) == (1, 2)
    # 「全 M 件」も利用できる業務プロファイルの範囲で数える（絞り込みだけを外す）。
    assert repository.count_scopes == [({"profile-a"}, None)]


def test_incremental_singleton_save_does_not_overwrite_feedback_config() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    feedback_config = {"value": {"similarity_threshold": 0.7, "match_limit": 8}}
    repository.put_document("singletons", "feedback_search_config", feedback_config)
    service = _incremental_service(repository)

    service._replace_classifier_artifact({"version": "classifier-1"})  # noqa: SLF001

    assert repository.get_document("singletons", "feedback_search_config") == feedback_config
    classifier = repository.get_document("singletons", "classifier_artifact")
    assert classifier == {"value": {"version": "classifier-1"}}


def test_incremental_feedback_delete_removes_oracle_vector_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_runtime_mode", "oracle")
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    keep = _history(1, admin=FeedbackRating.GOOD)
    delete = _history(2, admin=FeedbackRating.GOOD)
    _seed_history(repository, [keep, delete])
    repository.put_document(
        "singletons",
        "feedback_indexed_ids",
        {"value": [keep.id, delete.id]},
    )
    service = _incremental_service(repository)
    adapter = _FeedbackVectorAdapter()
    cast(Any, service)._oracle_adapter = adapter

    data = service.delete_feedback_entries([delete.id])

    assert adapter.deleted_history_ids == [delete.id]
    assert repository.get_document("history", delete.id) is None
    assert repository.get_document("history", keep.id) is not None
    indexed = repository.get_document("singletons", "feedback_indexed_ids")
    assert indexed == {"value": [keep.id]}
    assert data.indexed_count == 1
    assert [item.history_id for item in data.items] == [keep.id]


def test_incremental_feedback_delete_removes_classifier_examples() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    history = _history(1)
    _seed_history(repository, [history])
    example = ClassifierTrainingExample(
        id="classifier-example-feedback",
        category="default",
        text=history.question,
        profile_id=history.profile_id,
        source="feedback",
        source_type="feedback",
        source_history_id=history.id,
    )
    repository.put_document(
        "classifier_examples",
        example.id,
        example.model_dump(mode="json"),
        profile_id=example.profile_id,
        status=example.source_type,
    )
    service = _incremental_service(repository)

    service.delete_feedback_entries([history.id])

    assert repository.get_document("history", history.id) is None
    assert repository.get_document("classifier_examples", example.id) is None
    assert service.classifier_training_data().examples == []


def test_incremental_feedback_rebuild_failure_preserves_indexed_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_runtime_mode", "oracle")
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    history = _history(1, admin=FeedbackRating.GOOD)
    _seed_history(repository, [history])
    repository.put_document("singletons", "feedback_indexed_ids", {"value": [history.id]})
    service = _incremental_service(repository)
    cast(Any, service)._embedding_client = _FeedbackEmbeddingClient()
    cast(Any, service)._oracle_adapter = _FailingFeedbackVectorAdapter()

    data = service.rebuild_feedback_index(FeedbackIndexRequest())

    assert data.executed is False
    assert data.status == "ready"
    assert data.indexed_count == 1
    assert any("rebuild failed" in warning for warning in data.warnings)
    assert repository.get_document("singletons", "feedback_indexed_ids") == {"value": [history.id]}


def test_incremental_generation_context_loads_global_material_without_page_read() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    repository.put_document(
        "singletons",
        "legacy_learning_material",
        {
            "value": {
                "glossary": {"売上": "INVOICES.TOTAL_AMOUNT"},
                "rules": ["SELECT のみ"],
            }
        },
    )
    service = _incremental_service(repository)
    profile = Nl2SqlProfile(
        id="billing",
        name="請求管理",
        glossary={"請求": "INVOICES"},
        sql_rules=["上限 100 件"],
    )

    assert service._effective_glossary(profile) == {  # noqa: SLF001
        "売上": "INVOICES.TOTAL_AMOUNT",
        "請求": "INVOICES",
    }
    assert service._effective_sql_rules(profile) == [  # noqa: SLF001
        "SELECT のみ",
        "上限 100 件",
    ]


def test_enterprise_ai_direct_uses_incremental_schema_when_legacy_catalog_is_empty() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    _apply_incremental_catalog(
        repository,
        SchemaCatalog(
            refreshed_at="2026-08-14T00:00:00+00:00",
            schema_fingerprint="incremental-schema-v1",
            current_owner="APP",
            tables=[_table("ORDERS", comment="注文"), _table("PAYMENTS", comment="支払")],
        ),
    )
    repository.save_profile(
        Nl2SqlProfile(
            id="orders-profile",
            name="注文管理",
            allowed_tables=["APP.ORDERS"],
        ),
        expected_etag=None,
    )
    service = _incremental_service(repository)
    service._catalog = SchemaCatalog(refreshed_at="legacy-empty", tables=[])  # noqa: SLF001
    fake_client = _FakeEnterpriseAiClient(
        '{"sql":"SELECT ID FROM APP.ORDERS","explanation":"注文 ID を取得します。"}'
    )
    service._enterprise_ai_client = fake_client  # noqa: SLF001

    preview = service.preview(
        PreviewRequest(
            question="注文一覧を確認したい",
            engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
            profile_id="orders-profile",
        )
    )

    assert preview.engine == Nl2SqlEngine.ENTERPRISE_AI_DIRECT
    assert preview.sql == "SELECT ID FROM APP.ORDERS"
    # row_limit(既定 100)は取得時の fetch 上限だけで効かせ、SQL へは書き足さない。
    assert preview.executable_sql == "SELECT ID FROM APP.ORDERS"
    assert fake_client.calls
    context = fake_client.calls[0]["context"]
    assert "table APP.ORDERS" in context
    assert "column ID" in context
    assert "APP.PAYMENTS" not in context


def test_job_history_records_actor_user_uuid_in_incremental_store() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    _apply_incremental_catalog(
        repository,
        SchemaCatalog(
            refreshed_at="2026-08-14T00:00:00+00:00",
            schema_fingerprint="incremental-schema-v1",
            current_owner="APP",
            tables=[
                _table("ORDERS", comment="注文").model_copy(
                    update={
                        "columns": [
                            SchemaColumn(
                                column_name="ID",
                                logical_name="ID",
                                data_type="NUMBER",
                                nullable=False,
                            ),
                            SchemaColumn(
                                column_name="ORDER_NAME",
                                logical_name="注文名",
                                data_type="VARCHAR2",
                                nullable=True,
                                sample_values=["注文A"],
                            ),
                            SchemaColumn(
                                column_name="AMOUNT",
                                logical_name="金額",
                                data_type="NUMBER",
                                nullable=True,
                            ),
                            SchemaColumn(
                                column_name="CREATED_AT",
                                logical_name="作成日",
                                data_type="DATE",
                                nullable=True,
                            ),
                        ]
                    }
                )
            ],
        ),
    )
    repository.save_profile(
        Nl2SqlProfile(
            id="orders-profile",
            name="注文管理",
            allowed_tables=["APP.ORDERS"],
        ),
        expected_etag=None,
    )
    service = _incremental_service(repository)
    service._catalog = repository.load_catalog()  # noqa: SLF001
    service._enterprise_ai_client = _FakeEnterpriseAiClient(  # noqa: SLF001
        '{"sql":"SELECT ID FROM APP.ORDERS","explanation":"注文 ID を取得します。"}'
    )

    created = service.start_job(
        JobCreateRequest(
            question="注文一覧を確認したい",
            engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
            profile_id="orders-profile",
        ),
        actor_user_uuid="user-1",
        actor_is_system_admin=True,
    )
    job = None
    for _ in range(50):
        job = service.get_job(created.job_id)
        if job and job.status == JobStatus.DONE:
            break
        time.sleep(0.01)

    assert job is not None
    assert job.status == JobStatus.DONE, job.error_message
    own_history = service.list_history(viewer_user_uuid="user-1", all_users=False).items
    assert len(own_history) == 1
    assert own_history[0].actor_user_uuid == "user-1"
    assert service.list_history(viewer_user_uuid="other-user", all_users=False).items == []
    job_snapshot = repository.get_document("jobs", created.job_id)
    assert job_snapshot is not None
    assert job_snapshot["actor_user_uuid"] == "user-1"
    assert job_snapshot["actor_is_system_admin"] is True


def test_completed_job_stays_done_when_final_result_persistence_fails() -> None:
    class FinalResultWriteFailRepository(MemoryIncrementalNl2SqlRepository):
        def put_document(
            self,
            collection: str,
            entity_id: str,
            payload: Any,
            **kwargs: Any,
        ) -> None:
            if collection == "jobs" and payload.get("result") is not None:
                raise RuntimeError("ORA-03146: invalid buffer length for TTC field")
            super().put_document(collection, entity_id, payload, **kwargs)

    repository = FinalResultWriteFailRepository(seed_default=False)
    _apply_incremental_catalog(
        repository,
        SchemaCatalog(
            refreshed_at="2026-08-14T00:00:00+00:00",
            schema_fingerprint="incremental-schema-v1",
            current_owner="APP",
            tables=[
                _table("ORDERS", comment="注文").model_copy(
                    update={
                        "columns": [
                            SchemaColumn(
                                column_name="ID",
                                logical_name="ID",
                                data_type="NUMBER",
                                nullable=False,
                            ),
                            SchemaColumn(
                                column_name="ORDER_NAME",
                                logical_name="注文名",
                                data_type="VARCHAR2",
                                nullable=True,
                                sample_values=["注文A"],
                            ),
                            SchemaColumn(
                                column_name="AMOUNT",
                                logical_name="金額",
                                data_type="NUMBER",
                                nullable=True,
                            ),
                            SchemaColumn(
                                column_name="CREATED_AT",
                                logical_name="作成日",
                                data_type="DATE",
                                nullable=True,
                            ),
                        ]
                    }
                )
            ],
        ),
    )
    repository.save_profile(
        Nl2SqlProfile(
            id="orders-profile",
            name="注文管理",
            allowed_tables=["APP.ORDERS"],
        ),
        expected_etag=None,
    )
    service = _incremental_service(repository)
    service._catalog = repository.load_catalog()  # noqa: SLF001
    service._enterprise_ai_client = _FakeEnterpriseAiClient(  # noqa: SLF001
        '{"sql":"SELECT ID FROM APP.ORDERS","explanation":"注文 ID を取得します。"}'
    )
    request = JobCreateRequest(
        question="注文一覧を確認したい",
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        profile_id="orders-profile",
    )
    job_id = "job-final-result-persist-fails"
    service._jobs[job_id] = StoredJob(  # noqa: SLF001
        job_id=job_id,
        request=request,
        actor_user_uuid="user-1",
        actor_is_system_admin=True,
        steps=_new_job_steps(),
    )

    service._persist_job(job_id)
    assert service._claim_nl2sql_job(worker_id="owner", job_id=job_id) is not None
    service._run_job(job_id)  # noqa: SLF001

    job = service.get_job(job_id)
    assert job is not None
    assert job.status == JobStatus.DONE
    assert job.error_message is None
    assert job.warning_message is not None
    assert "履歴/ジョブ保存に失敗しました" in job.warning_message
    assert job.result is not None
    assert job.result.generated_sql == "SELECT ID FROM APP.ORDERS"


def test_enterprise_ai_direct_reports_profile_scope_when_incremental_object_is_missing() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    _apply_incremental_catalog(
        repository,
        SchemaCatalog(
            refreshed_at="2026-08-14T00:00:00+00:00",
            schema_fingerprint="incremental-schema-v1",
            current_owner="APP",
            tables=[_table("ORDERS", comment="注文")],
        ),
    )
    repository.save_profile(
        Nl2SqlProfile(
            id="missing-profile",
            name="削除済み許可表",
            allowed_tables=["APP.MISSING_TABLE"],
        ),
        expected_etag=None,
    )
    service = _incremental_service(repository)
    service._catalog = SchemaCatalog(refreshed_at="legacy-empty", tables=[])  # noqa: SLF001
    service._enterprise_ai_client = _FakeEnterpriseAiClient(
        '{"sql":"SELECT ID FROM APP.MISSING_TABLE","explanation":"should not run"}'
    )  # noqa: SLF001

    with pytest.raises(ValueError, match="業務 Profile の許可表"):
        service.preview(
            PreviewRequest(
                question="削除済み表を見たい",
                engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
                profile_id="missing-profile",
            )
        )


def test_enterprise_ai_direct_deterministic_fallback_uses_incremental_schema_snapshot() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    _apply_incremental_catalog(
        repository,
        SchemaCatalog(
            refreshed_at="2026-08-14T00:00:00+00:00",
            schema_fingerprint="incremental-schema-v1",
            current_owner="APP",
            tables=[_table("ORDERS", comment="注文")],
        ),
    )
    repository.save_profile(
        Nl2SqlProfile(
            id="orders-profile",
            name="注文管理",
            allowed_tables=["APP.ORDERS"],
        ),
        expected_etag=None,
    )
    service = _incremental_service(repository)
    service._catalog = SchemaCatalog(refreshed_at="legacy-empty", tables=[])  # noqa: SLF001
    service._enterprise_ai_client = _FakeEnterpriseAiClient("説明だけで SQL はありません。")  # noqa: SLF001

    preview = service.preview(
        PreviewRequest(
            question="注文一覧を確認したい",
            engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
            profile_id="orders-profile",
        )
    )

    assert preview.sql == "SELECT ID FROM APP.ORDERS"
    # row_limit(既定 100)は取得時の fetch 上限だけで効かせ、SQL へは書き足さない。
    assert preview.executable_sql == "SELECT ID FROM APP.ORDERS"
    assert "enterprise_ai_direct:" in preview.fallback_reason


def test_incremental_legacy_material_missing_document_returns_empty() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    service = _incremental_service(repository)

    material = service.get_legacy_learning_material()

    assert material.glossary == {}
    assert material.rules == []
    assert repository.get_document("singletons", "legacy_learning_material") is None


def test_incremental_legacy_material_invalid_document_is_operation_failure() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    repository.put_document(
        "singletons",
        "legacy_learning_material",
        {"value": {"glossary": [], "rules": "invalid"}},
    )
    service = _incremental_service(repository)

    with pytest.raises(Nl2SqlRepositoryOperationFailed) as exc_info:
        service.get_legacy_learning_material()

    assert exc_info.value.reason_code == "legacy_learning_material_query_failed"


def test_incremental_legacy_material_connection_failure_is_unavailable() -> None:
    class FailingReadRepository(MemoryIncrementalNl2SqlRepository):
        def get_document(self, collection: str, entity_id: str) -> dict[str, Any] | None:
            del collection, entity_id
            raise RuntimeError("ORA-12541: listener unavailable")

    service = _incremental_service(FailingReadRepository(seed_default=False))

    with pytest.raises(Nl2SqlPersistenceUnavailable) as exc_info:
        service.get_legacy_learning_material()

    assert exc_info.value.reason_code == "oracle_connection_unavailable"


def test_incremental_legacy_import_rolls_back_memory_when_save_fails() -> None:
    class FailingWriteRepository(MemoryIncrementalNl2SqlRepository):
        fail_write = False

        def put_document(self, *args: Any, **kwargs: Any) -> None:
            if self.fail_write:
                raise RuntimeError("singleton write failed")
            super().put_document(*args, **kwargs)

    repository = FailingWriteRepository(seed_default=False)
    repository.put_document(
        "singletons",
        "legacy_learning_material",
        {
            "value": {
                "glossary": {"売上": "INVOICES.TOTAL_AMOUNT"},
                "rules": ["SELECT のみ"],
            }
        },
    )
    service = _incremental_service(repository)
    repository.fail_write = True

    with pytest.raises(Nl2SqlRepositoryOperationFailed):
        service.import_legacy_terms(
            filename="terms.xlsx",
            content=_single_sheet_workbook_bytes(
                "terms",
                [["TERM", "DEFINITION"], ["粗利", "INVOICES.PROFIT"]],
            ),
        )

    assert service._legacy_learning_material.glossary == {  # noqa: SLF001
        "売上": "INVOICES.TOTAL_AMOUNT"
    }
    assert service._legacy_learning_material.rules == ["SELECT のみ"]  # noqa: SLF001


def test_incremental_migrations_backfill_before_not_null_constraints() -> None:
    migration_root = Path(__file__).resolve().parents[1] / "migrations"
    state_ddl = (migration_root / "003_incremental_nl2sql_state.sql").read_text(encoding="utf-8")
    lease_ddl = (migration_root / "006_incremental_job_leases.sql").read_text(encoding="utf-8")
    lifecycle_ddl = (migration_root / "007_profile_ontology_lifecycle.sql").read_text(
        encoding="utf-8"
    )

    proposal_add = "ADD (PROFILE_ID VARCHAR2(128));"
    proposal_backfill = "UPDATE NL2SQL_ONTOLOGY_PROPOSALS proposal"
    proposal_constraint = "MODIFY (PROFILE_ID NOT NULL);"
    assert state_ddl.index(proposal_add) < state_ddl.index(proposal_backfill)
    assert state_ddl.index(proposal_backfill) < state_ddl.index(proposal_constraint)
    assert "PROFILE_ID VARCHAR2(128) DEFAULT '' NOT NULL" not in state_ddl

    lease_add = "ATTEMPT_NO NUMBER(10)"
    lease_backfill = "UPDATE NL2SQL_SCHEMA_REFRESH_JOBS"
    lease_constraint = "MODIFY (ATTEMPT_NO DEFAULT 0 NOT NULL);"
    assert lease_ddl.index(lease_add) < lease_ddl.index(lease_backfill)
    assert lease_ddl.index(lease_backfill) < lease_ddl.index(lease_constraint)
    assert "WORKER_ID VARCHAR2(256) DEFAULT '' NOT NULL" not in lease_ddl
    assert lifecycle_ddl.count("ON DELETE CASCADE") == 2
    assert lifecycle_ddl.index("DELETE FROM NL2SQL_ONTOLOGY_PROFILE_VIEW_REVISIONS") < (
        lifecycle_ddl.index("ALTER TABLE NL2SQL_ONTOLOGY_PROFILE_VIEW_REVISIONS")
    )


class _ProfileDeleteCursor:
    def __init__(self, connection: _ProfileDeleteConnection) -> None:
        self.connection = connection
        self._row: tuple[str] | None = None

    def __enter__(self) -> _ProfileDeleteCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, binds: Any = None) -> None:
        normalized = " ".join(sql.split())
        self.connection.executed.append((normalized, binds))
        if self.connection.fail_on and self.connection.fail_on in normalized:
            raise RuntimeError("scripted delete failure")
        self._row = ("etag-1",) if "SELECT ETAG" in normalized else None

    def fetchone(self) -> tuple[str] | None:
        return self._row


class _ProfileDeleteConnection:
    def __init__(self, *, fail_on: str = "") -> None:
        self.fail_on = fail_on
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _ProfileDeleteCursor:
        return _ProfileDeleteCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _profile_delete_repository(
    *, fail_on: str = ""
) -> tuple[OracleIncrementalNl2SqlRepository, _ProfileDeleteConnection]:
    connection = _ProfileDeleteConnection(fail_on=fail_on)

    @contextmanager
    def connect() -> Iterator[_ProfileDeleteConnection]:
        yield connection

    return OracleIncrementalNl2SqlRepository(connection_factory=connect), connection


def test_oracle_profile_delete_removes_all_view_revisions_in_same_transaction() -> None:
    repository, connection = _profile_delete_repository()

    repository.delete_profile("profile-1", expected_etag="etag-1")

    sql = [statement for statement, _binds in connection.executed]
    assert "NL2SQL_ONTOLOGY_PROFILE_VIEW_REVISIONS" in sql[1]
    assert "NL2SQL_ONTOLOGY_PROFILE_VIEWS" in sql[2]
    assert sql[3].startswith("DELETE FROM NL2SQL_PROFILES")
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_oracle_profile_delete_rolls_back_before_parent_delete_on_view_failure() -> None:
    repository, connection = _profile_delete_repository(fail_on="NL2SQL_ONTOLOGY_PROFILE_VIEWS")

    with pytest.raises(RuntimeError, match="scripted delete failure"):
        repository.delete_profile("profile-1", expected_etag="etag-1")

    assert not any(
        statement.startswith("DELETE FROM NL2SQL_PROFILES")
        for statement, _binds in connection.executed
    )
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_oracle_profile_timestamp_bind_is_nls_independent() -> None:
    profile = _profile(1)
    updated_at = "2026-07-19T12:34:56+00:00"

    binds = OracleIncrementalNl2SqlRepository._profile_binds(  # noqa: SLF001
        profile,
        profile.model_dump(mode="json"),
        1,
        "etag",
        updated_at,
    )

    assert binds["updated_at"] == datetime.fromisoformat(updated_at)
    assert isinstance(binds["updated_at"], datetime)


def test_incremental_repository_accepts_oracle_native_json_values() -> None:
    assert json.loads(_read_lob({"items": ["値"]})) == {"items": ["値"]}
    assert json.loads(_read_lob([{"id": "one"}])) == [{"id": "one"}]


def test_oracle_state_document_payload_uses_clob_bind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clob_type = object()
    monkeypatch.setitem(sys.modules, "oracledb", SimpleNamespace(DB_TYPE_CLOB=clob_type))
    repository, connections = _oracle_repository([[], []])

    repository.put_document("jobs", "job-1", {"id": "job-1", "payload": "値"})

    assert connections[0].input_sizes == [{"payload": clob_type}]
    assert connections[0].executed[0][1]["payload"] == '{"id":"job-1","payload":"値"}'


def test_oracle_profile_lobs_are_materialized_before_connection_closes() -> None:
    profile = _profile(1)
    payload = json.dumps(profile.model_dump(mode="json"), ensure_ascii=False)
    detail_repository, detail_connections = _oracle_repository(
        [[(_LobPayload(payload), 1, "etag-1", "2026-07-20T00:00:00+00:00")]]
    )
    list_repository, list_connections = _oracle_repository(
        [[(_LobPayload(payload), 1, "etag-1", "2026-07-20T00:00:00+00:00")]]
    )

    detail = detail_repository.get_profile(profile.id)
    profiles = list_repository.list_profiles(include_archived=True)

    assert detail is not None
    assert detail.id == profile.id
    assert [item.id for item in profiles] == [profile.id]
    assert all(connection.closed for connection in detail_connections + list_connections)


def test_oracle_state_document_lobs_are_materialized_before_connection_closes() -> None:
    payload = '{"id":"history-1","question":"部署名"}'
    detail_repository, detail_connections = _oracle_repository([[(_LobPayload(payload),)]])
    list_repository, list_connections = _oracle_repository([[(_LobPayload(payload),)]])
    page_repository, page_connections = _oracle_repository(
        [
            [(1,)],
            [
                (
                    _LobPayload(payload),
                    datetime(2026, 7, 20, tzinfo=UTC),
                    "history-1",
                )
            ],
        ]
    )

    detail = detail_repository.get_document("history", "history-1")
    documents = list_repository.list_documents("history", limit=10)
    page, next_cursor, total = page_repository.list_documents_page("history", cursor=None, limit=10)

    assert detail == {"id": "history-1", "question": "部署名"}
    assert documents == [detail]
    assert page == [detail]
    assert next_cursor is None
    assert total == 1
    assert all(
        connection.closed for connection in detail_connections + list_connections + page_connections
    )


def test_oracle_state_document_page_filters_payload_before_paging() -> None:
    payload = '{"id":"history-own","question":"自分の履歴","actor_user_uuid":"user-1"}'
    repository, connections = _oracle_repository(
        [
            [(1,)],
            [
                (
                    _LobPayload(payload),
                    datetime(2026, 7, 20, tzinfo=UTC),
                    "history-own",
                )
            ],
        ]
    )

    page, next_cursor, total = repository.list_documents_page(
        "history",
        cursor=None,
        limit=1,
        payload_filters={"actor_user_uuid": "user-1"},
    )

    assert [item["id"] for item in page] == ["history-own"]
    assert next_cursor is None
    assert total == 1
    executed = "\n".join(sql for connection in connections for sql, _binds in connection.executed)
    assert "JSON_VALUE(PAYLOAD_JSON, '$.actor_user_uuid'" in executed
    assert all(
        binds.get("payload_filter_0") == "user-1"
        for connection in connections
        for _sql, binds in connection.executed
    )


def test_oracle_state_document_page_filters_profile_ids_before_paging() -> None:
    payload = '{"id":"history-sales","question":"営業の履歴","profile_id":"sales"}'
    repository, connections = _oracle_repository(
        [
            [(2,)],
            [
                (
                    _LobPayload(payload),
                    datetime(2026, 7, 20, tzinfo=UTC),
                    "history-sales",
                ),
                (
                    _LobPayload('{"id":"history-finance","profile_id":"finance"}'),
                    datetime(2026, 7, 19, tzinfo=UTC),
                    "history-finance",
                ),
            ],
        ]
    )

    page, next_cursor, total = repository.list_documents_page(
        "history",
        cursor=None,
        limit=1,
        profile_ids={"sales", "finance"},
    )

    assert [item["id"] for item in page] == ["history-sales"]
    assert next_cursor is not None
    assert total == 2
    count_sql, count_binds = connections[0].executed[0]
    page_sql, page_binds = connections[0].executed[1]
    assert "PROFILE_ID IN (:profile_id_0, :profile_id_1)" in count_sql
    assert "PROFILE_ID IN (:profile_id_0, :profile_id_1)" in page_sql
    assert count_binds["profile_id_0"] == "finance"
    assert count_binds["profile_id_1"] == "sales"
    assert page_binds["profile_id_0"] == "finance"
    assert page_binds["profile_id_1"] == "sales"


@pytest.mark.parametrize(
    ("sort_key", "direction", "order_by"),
    [
        (
            "question",
            "asc",
            "ORDER BY NLSSORT(COALESCE(JSON_VALUE(PAYLOAD_JSON, '$.question'",
        ),
        (
            "created_at",
            "desc",
            "ORDER BY COALESCE(JSON_VALUE(PAYLOAD_JSON, '$.created_at'",
        ),
    ],
    ids=["question-asc", "created-desc"],
)
def test_oracle_state_document_page_with_offset_uses_offset_fetch(
    sort_key: str, direction: str, order_by: str
) -> None:
    """offset を渡すと、カーソルを使わず同じ順のまま OFFSET … FETCH NEXT で取る（#1891）。"""
    payload = '{"id":"history-3","question":"3 件目"}'
    repository, connections = _oracle_repository(
        [
            [(7,)],
            [(_LobPayload(payload), "3 件目", "history-3")],
        ]
    )

    page, next_cursor, total = repository.list_documents_page(
        "history",
        cursor="ignored-because-offset-is-given",
        offset=2,
        limit=2,
        query="件目",
        sort_key=sort_key,
        direction=direction,
    )

    assert [item["id"] for item in page] == ["history-3"]
    assert next_cursor is None
    assert total == 7
    count_sql, count_binds = connections[0].executed[0]
    page_sql, page_binds = connections[0].executed[1]
    assert "OFFSET" not in count_sql
    assert count_binds["query"] == "件目"
    assert order_by in page_sql
    order = direction.upper()
    assert page_sql.endswith(f", ENTITY_ID {order} OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY")
    if sort_key == "question":
        assert "'NLS_SORT=JAPANESE_M'" in page_sql
    # keyset の条件（:after_sort_key）は付けず、limit は 1 件多く取らない。
    assert ":after_sort_key" not in page_sql
    assert (page_binds["offset"], page_binds["limit"]) == (2, 2)


def test_oracle_count_documents_counts_the_scope_without_paging() -> None:
    repository, connections = _oracle_repository([[(4,)]])

    total = repository.count_documents(
        "history",
        payload_filters={"actor_user_uuid": "user-1"},
        profile_ids={"sales"},
    )

    assert total == 4
    [(sql, binds)] = connections[0].executed
    assert sql.startswith("SELECT COUNT(*) FROM NL2SQL_STATE_DOCUMENTS WHERE ")
    assert "PROFILE_ID IN (:profile_id_0)" in sql
    assert "ORDER BY" not in sql and "FETCH" not in sql
    assert binds["payload_filter_0"] == "user-1"
    assert binds["profile_id_0"] == "sales"
    # 対象の業務プロファイルが無ければ DB に問い合わせない。
    empty_repository, empty_connections = _oracle_repository()
    assert empty_repository.count_documents("history", profile_ids=set()) == 0
    assert empty_connections == []


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_memory_state_document_offset_pages_match_the_keyset_order(direction: str) -> None:
    """offset のページを順に読むと、keyset で全件を読んだ順と同じで抜け・重なりが無い。"""
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    histories = [
        _history(index).model_copy(update={"question": f"質問 {index % 3}"}) for index in range(7)
    ]
    _seed_history(repository, histories)

    keyset: list[str] = []
    cursor: str | None = None
    while True:
        page, cursor, _total = repository.list_documents_page(
            "history", cursor=cursor, limit=3, sort_key="question", direction=direction
        )
        keyset.extend(item["id"] for item in page)
        if not cursor:
            break
    offset_ids: list[str] = []
    for offset in range(0, 9, 3):
        page, next_cursor, total = repository.list_documents_page(
            "history",
            cursor=None,
            offset=offset,
            limit=3,
            sort_key="question",
            direction=direction,
        )
        assert next_cursor is None
        assert total == 7
        offset_ids.extend(item["id"] for item in page)

    assert offset_ids == keyset
    assert len(set(offset_ids)) == 7
    beyond, _cursor, beyond_total = repository.list_documents_page(
        "history", cursor=None, offset=20, limit=3
    )
    assert (beyond, beyond_total) == ([], 7)
    assert repository.count_documents("history", query="質問 1") == 2
    assert repository.count_documents("history") == 7


@pytest.mark.asyncio
async def test_similar_history_lob_read_does_not_block_following_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql import router as nl2sql_router
    from app.settings import get_settings

    history_payload = json.dumps(
        {
            "id": "history-1",
            "question": "部署名を検索",
            "engine": "select_ai",
            "generated_sql": "SELECT DEPARTMENT_NAME FROM ADMIN.DEPARTMENT",
            "created_at": "2026-07-20T00:00:00+00:00",
            "feedback_rating": "good",
            "admin_feedback_rating": "good",
            "profile_id": "default",
            "profile_name": "標準プロファイル",
            "safety_is_safe": True,
        },
        ensure_ascii=False,
    )
    repository, connections = _oracle_repository(
        [[]],
        [[]],
        [
            [(1,)],
            [
                (
                    _LobPayload(history_payload),
                    datetime(2026, 7, 20, tzinfo=UTC),
                    "history-1",
                )
            ],
        ],
    )
    service = _incremental_service(repository)
    # 最低スコアの既定（0.9。#1721）ではなく、候補の絞り込み・並びを確かめるため 0 にする。
    service._feedback_similarity_threshold = 0.0  # noqa: SLF001
    profile = Nl2SqlProfile(
        id="default",
        name="標準プロファイル",
        object_scope_version=2,
    )
    service._profile_cache.put(profile.id, profile)  # noqa: SLF001
    service._cache_token_checked_at["profiles"] = time.monotonic()  # noqa: SLF001
    service._cache_token_poll_seconds = 60.0  # noqa: SLF001
    # similar-history 経路の glossary 読込が追加接続を消費しないようキャッシュを温める
    service._legacy_learning_material_loaded = True  # noqa: SLF001
    service._legacy_learning_material_checked_at = time.monotonic()  # noqa: SLF001
    service._deepsec_enabled = False  # noqa: SLF001
    monkeypatch.setattr(service._embedding_client, "is_configured", lambda: False)
    monkeypatch.setattr(service, "list_profiles", lambda include_archived=False: [profile])
    monkeypatch.setattr(service, "_persist_job", lambda _job_id: None)
    monkeypatch.setattr(service, "_run_job_safely", lambda _job_id: None)
    monkeypatch.setattr(get_settings(), "nl2sql_job_worker_mode", "external")
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    similar_response = nl2sql_router.similar_history(
        SimilarHistoryRequest(question="部署名を検索", profile_id="default", limit=3),
        cast(Request, SimpleNamespace(state=SimpleNamespace(principal=None))),
    )
    persistence_after_similar = service.persistence_status()
    job_response = nl2sql_router.create_job(
        JobCreateRequest(
            question="部署名を検索",
            engine="select_ai",
            profile_id="default",
        ),
        cast(Request, SimpleNamespace(state=SimpleNamespace(principal=None))),
    )

    assert len(similar_response.data.items) == 1  # type: ignore[union-attr]
    assert persistence_after_similar.ready is True
    assert job_response.data.status == "pending"  # type: ignore[union-attr]
    assert all(connection.closed for connection in connections)


def test_oracle_schema_lobs_are_materialized_before_connection_closes() -> None:
    constraint_payload = json.dumps(
        {
            "constraint_name": "PK_DEPARTMENT",
            "constraint_type": "P",
            "owner": "ADMIN",
            "table_name": "DEPARTMENT",
            "columns": ["DEPARTMENT_ID"],
        }
    )
    object_row = ("ADMIN", "DEPARTMENT", "TABLE", "部署", "部署情報", 10)
    column_row = (
        "ADMIN",
        "DEPARTMENT",
        "DEPARTMENT_ID",
        "部署ID",
        "NUMBER",
        False,
        "主キー",
        _LobPayload('["10"]'),
    )
    constraint_row = (
        "ADMIN",
        "DEPARTMENT",
        "PRIMARY KEY (DEPARTMENT_ID)",
        _LobPayload(constraint_payload),
    )
    head_row = (1, "fingerprint", "2026-07-20T00:00:00+00:00", 1, 1, "etag", 1)
    catalog_repository, catalog_connections = _oracle_repository(
        [[head_row]],
        [[object_row], [column_row], [constraint_row], []],
    )
    detail_repository, detail_connections = _oracle_repository(
        [
            [object_row],
            [(*column_row[2:7], column_row[7])],
            [(constraint_row[2], constraint_row[3])],
            [],
        ],
        [[head_row]],
    )

    catalog = catalog_repository.load_catalog()
    detail = detail_repository.get_schema_object("ADMIN", "DEPARTMENT")

    assert catalog.tables[0].columns[0].sample_values == ["10"]
    assert catalog.tables[0].constraint_details[0].constraint_name == "PK_DEPARTMENT"
    assert detail is not None
    assert detail.table.columns[0].sample_values == ["10"]
    assert detail.table.constraint_details[0].constraint_name == "PK_DEPARTMENT"
    assert all(connection.closed for connection in catalog_connections + detail_connections)


def test_oracle_refresh_job_lob_is_materialized_before_connection_closes() -> None:
    job = SchemaRefreshJob(
        job_id="refresh-1",
        created_at="2026-07-20T00:00:00+00:00",
    )
    repository, connections = _oracle_repository([[(_LobPayload(job.model_dump_json()),)]])

    restored = repository.get_refresh_job(job.job_id)

    assert restored == job
    assert connections[0].closed is True


def test_oracle_refresh_job_submission_coalesces_active_job_atomically() -> None:
    active = SchemaRefreshJob(
        job_id="active-refresh",
        created_at="2026-07-20T00:00:00+00:00",
    )
    candidate = SchemaRefreshJob(
        job_id="candidate-refresh",
        created_at="2026-07-21T00:00:00+00:00",
    )
    repository, connections = _oracle_repository([[], [(_LobPayload(active.model_dump_json()),)]])

    submitted = repository.submit_refresh_job(candidate)

    assert submitted.job_id == active.job_id
    assert connections[0].closed is True


def test_oracle_refresh_job_claim_uses_skip_locked_without_row_limiting_clause() -> None:
    job = SchemaRefreshJob(
        job_id="refresh-1",
        created_at="2026-07-20T00:00:00+00:00",
    )
    repository, connections = _oracle_repository(
        [[("refresh-1", _LobPayload(job.model_dump_json()))], []]
    )

    claimed = repository.claim_refresh_job(
        worker_id="schema-worker-1",
        lease_seconds=60,
    )

    assert claimed is not None
    assert claimed.job_id == "refresh-1"
    assert claimed.status == SchemaRefreshJobStatus.RUNNING
    select_sql = connections[0].executed[0][0].upper()
    assert "FOR UPDATE SKIP LOCKED" in select_sql
    assert "FETCH FIRST" not in select_sql
    assert connections[0]._cursor.prefetchrows == 0  # noqa: SLF001
    assert connections[0]._cursor.arraysize == 1  # noqa: SLF001


def test_profile_repository_uses_cursor_summary_and_etag_conflict() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    for index in range(120):
        repository.save_profile(_profile(index), expected_etag=None)

    first = repository.search_profiles(
        cursor=None,
        limit=50,
        query="業務",
        include_archived=False,
    )
    second = repository.search_profiles(
        cursor=first.next_cursor,
        limit=50,
        query="業務",
        include_archived=False,
    )

    assert len(first.items) == 50
    assert len(second.items) == 50
    assert first.total == 120
    assert set(item.id for item in first.items).isdisjoint(item.id for item in second.items)
    current = repository.get_profile(first.items[0].id)
    assert current is not None
    stored = repository.save_profile(
        current.model_copy(update={"description": "updated"}),
        expected_etag=current.etag,
    )
    assert stored.version == current.version + 1
    with pytest.raises(IncrementalVersionConflict):
        repository.save_profile(current, expected_etag=current.etag)


def test_profile_search_memory_paths_share_order_and_literal_query_matching() -> None:
    profiles = [
        Nl2SqlProfile(id="profile-ab", name="AB", category="sales"),
        Nl2SqlProfile(id="profile-ac", name="AC", category="sales"),
        Nl2SqlProfile(id="profile-a-b", name="A_B", category="sales"),
    ]
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.delete_profile("default")
    for profile in profiles:
        repository.save_profile(profile, expected_etag=None)
        service.create_profile(profile)

    repository_page = repository.search_profiles(
        cursor=None,
        limit=10,
        query="",
        include_archived=False,
    )
    service_page = service.search_profiles(
        cursor=None,
        limit=10,
        query="",
        include_archived=False,
    )
    assert [item.id for item in service_page.items] == [item.id for item in repository_page.items]
    assert [item.id for item in service_page.items] == [
        "profile-ab",
        "profile-ac",
        "profile-a-b",
    ]

    repository_wildcard = repository.search_profiles(
        cursor=None,
        limit=10,
        query="_",
        include_archived=False,
    )
    service_wildcard = service.search_profiles(
        cursor=None,
        limit=10,
        query="_",
        include_archived=False,
    )
    assert [item.id for item in service_wildcard.items] == [
        item.id for item in repository_wildcard.items
    ]
    assert [item.id for item in service_wildcard.items] == ["profile-a-b"]


def test_profile_search_sorts_across_pages_by_allowed_object_counts() -> None:
    profiles = [
        Nl2SqlProfile(
            id=f"profile-{index}",
            name=f"P{index}",
            allowed_tables=[f"APP.T{index}_{seq}" for seq in range(index)],
            allowed_views=[f"APP.V{index}_{seq}" for seq in range(5 - index)],
        )
        for index in range(5)
    ]
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service.delete_profile("default")
    for profile in profiles:
        repository.save_profile(profile, expected_etag=None)
        service.create_profile(profile)

    def collect(source: object, **kwargs: object) -> list[str]:
        # keyset cursor を辿り、ページを跨いで並びが一貫することを確認する。
        ids: list[str] = []
        cursor: str | None = None
        while True:
            page = source.search_profiles(  # type: ignore[attr-defined]
                cursor=cursor,
                limit=2,
                query="",
                include_archived=False,
                **kwargs,
            )
            ids.extend(item.id for item in page.items)
            cursor = page.next_cursor
            if not cursor:
                return ids

    descending_tables = collect(repository, sort_key="tables", direction="desc")
    assert descending_tables == [
        "profile-4",
        "profile-3",
        "profile-2",
        "profile-1",
        "profile-0",
    ]
    assert collect(service, sort="tables", direction="desc") == descending_tables

    ascending_views = collect(repository, sort_key="views", direction="asc")
    assert ascending_views == [
        "profile-4",
        "profile-3",
        "profile-2",
        "profile-1",
        "profile-0",
    ]
    assert collect(service, sort="views", direction="asc") == ascending_views

    assert collect(repository, sort_key="name", direction="asc") == [
        f"profile-{index}" for index in range(5)
    ]


def test_profile_search_rejects_unknown_sort_key() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    service = _incremental_service(repository)

    with pytest.raises(ValueError):
        service.search_profiles(
            cursor=None,
            limit=10,
            query="",
            include_archived=False,
            sort="updated_at",
        )
    with pytest.raises(ValueError):
        service.search_profiles(
            cursor=None,
            limit=10,
            query="",
            include_archived=False,
            direction="sideways",
        )


def test_oracle_profile_search_orders_and_pages_by_requested_sort() -> None:
    repository, connections = _oracle_repository([[], [(0,)], [(7,)]])

    repository.search_profiles(
        cursor=None,
        limit=10,
        query="",
        include_archived=False,
        sort_key="tables",
        direction="desc",
    )

    select_sql, _ = connections[0].executed[0]
    assert "ORDER BY LPAD(TO_CHAR(NVL(ALLOWED_TABLE_COUNT, 0)), 12, '0') DESC" in select_sql
    assert "PROFILE_ID DESC" in select_sql


def test_oracle_profile_search_escapes_like_wildcards() -> None:
    repository, connections = _oracle_repository([[], [(0,)], [(7,)]])

    page = repository.search_profiles(
        cursor=None,
        limit=10,
        query="_%",
        include_archived=False,
    )

    assert page.items == []
    assert page.total == 0
    select_sql, select_binds = connections[0].executed[0]
    count_sql, count_binds = connections[0].executed[1]
    assert "ESCAPE '\\'" in select_sql
    assert "ESCAPE '\\'" in count_sql
    assert select_binds["query"] == "%\\_\\%%"
    assert count_binds["query"] == "%\\_\\%%"


def test_state_document_page_uses_stable_keyset_cursor() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    for index in range(5):
        repository.put_document("history", f"history-{index}", {"id": f"history-{index}"})

    first, cursor, first_total = repository.list_documents_page("history", cursor=None, limit=2)
    assert [item["id"] for item in first] == ["history-4", "history-3"]
    assert cursor is not None
    assert first_total == 5

    repository.put_document("history", "history-5", {"id": "history-5"})
    second, _next_cursor, second_total = repository.list_documents_page(
        "history", cursor=cursor, limit=2
    )
    assert [item["id"] for item in second] == ["history-2", "history-1"]
    assert second_total == 6


def test_memory_state_document_page_filters_payload_before_paging() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    repository.put_document(
        "history",
        "history-own",
        {"id": "history-own", "actor_user_uuid": "user-1"},
    )
    for index in range(3):
        repository.put_document(
            "history",
            f"history-other-{index}",
            {"id": f"history-other-{index}", "actor_user_uuid": "other-user"},
        )

    page, next_cursor, total = repository.list_documents_page(
        "history",
        cursor=None,
        limit=1,
        payload_filters={"actor_user_uuid": "user-1"},
    )

    assert [item["id"] for item in page] == ["history-own"]
    assert next_cursor is None
    assert total == 1


def test_memory_history_keyword_search_uses_visible_feedback_fields() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    repository.put_document(
        "history",
        "history-profile",
        {
            "id": "history-profile",
            "question": "請求金額を確認したい",
            "generated_sql": "SELECT TOTAL FROM APP.INVOICES",
            "feedback_comment": "",
            "profile_id": "default",
            "feedback_rating": "good",
            "actor_user_uuid": "user-1",
        },
        profile_id="default",
        status="good",
    )

    visible, _cursor, visible_total = repository.list_documents_page(
        "history",
        cursor=None,
        limit=10,
        query="請求金額",
    )
    hidden, _cursor, hidden_total = repository.list_documents_page(
        "history",
        cursor=None,
        limit=10,
        query="default",
    )

    assert [item["id"] for item in visible] == ["history-profile"]
    assert visible_total == 1
    assert hidden == []
    assert hidden_total == 0


def test_two_services_converge_through_change_token() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    original = repository.save_profile(_profile(1), expected_etag=None)
    first = _incremental_service(repository)
    second = _incremental_service(repository)

    assert second.get_profile(original.id).description == ""
    updated = first.update_profile(
        original.id,
        lambda profile: profile.model_copy(update={"description": "new value"}),
        expected_etag=original.etag,
    )

    assert updated.description == "new value"
    assert second.get_profile(original.id).description == "new value"


def test_cache_is_used_only_inside_ttl_when_change_token_store_is_unreachable() -> None:
    class FailingTokenRepository(MemoryIncrementalNl2SqlRepository):
        fail_token = False

        def get_change_token(self, namespace: str) -> int:
            if self.fail_token:
                raise RuntimeError("ORA-12514: listener does not know service")
            return super().get_change_token(namespace)

    repository = FailingTokenRepository(seed_default=False)
    stored = repository.save_profile(_profile(2), expected_etag=None)
    service = _incremental_service(repository)
    service._profile_cache = VersionedTtlCache(  # noqa: SLF001
        max_entries=10,
        ttl_seconds=0.01,
        name="profile-test",
    )

    assert service.get_profile(stored.id).id == stored.id
    repository.fail_token = True
    assert service.get_profile(stored.id).id == stored.id
    time.sleep(0.02)
    with pytest.raises(Nl2SqlPersistenceUnavailable):
        service.get_profile(stored.id)


def test_profile_api_supports_summary_detail_etag_and_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql import router as profile_router

    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    stored = repository.save_profile(_profile(7), expected_etag=None)
    service = _incremental_service(repository)
    monkeypatch.setattr(profile_router, "nl2sql_service", service)

    anon_request = cast(Request, SimpleNamespace(state=SimpleNamespace(principal=None)))
    page_response = Response()
    page = profile_router.search_profiles(
        anon_request,
        page_response,
        CursorParams(cursor=None, limit=10),
        q="0007",
    )
    page_not_modified = profile_router.search_profiles(
        anon_request,
        Response(),
        CursorParams(cursor=None, limit=10),
        q="0007",
        if_none_match=page_response.headers["etag"],
    )
    detail_response = Response()
    detail = profile_router.get_profile_detail(stored.id, anon_request, detail_response)
    not_modified = profile_router.get_profile_detail(
        stored.id,
        anon_request,
        Response(),
        if_none_match=detail_response.headers["etag"],
    )

    with pytest.raises(HTTPException) as missing_precondition:
        profile_router.update_profile(
            stored.id,
            ProfilePatchRequest(name="updated"),
            anon_request,
            Response(),
        )
    with pytest.raises(HTTPException) as conflict:
        profile_router.update_profile(
            stored.id,
            ProfilePatchRequest(name="updated"),
            anon_request,
            Response(),
            if_match='"stale"',
        )

    assert page.data.items[0].id == stored.id  # type: ignore[union-attr]
    assert page_not_modified.status_code == 304  # type: ignore[union-attr]
    assert detail.data.id == stored.id  # type: ignore[union-attr]
    assert detail_response.headers["etag"] == f'"{stored.etag}"'
    assert not_modified.status_code == 304  # type: ignore[union-attr]
    assert missing_precondition.value.status_code == 428
    assert conflict.value.status_code == 409
    assert conflict.value.headers["ETag"] == f'"{stored.etag}"'  # type: ignore[index]


def test_schema_api_supports_page_and_detail_etag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.schema import router as schema_router

    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    catalog = SchemaCatalog(refreshed_at="now", tables=[_table("ORDERS"), _table("PAYMENTS")])
    repository.apply_schema_refresh(
        catalog=catalog,
        manifest={("APP", "ORDERS"): "v1", ("APP", "PAYMENTS"): "v1"},
        changed_keys={("APP", "ORDERS"), ("APP", "PAYMENTS")},
        deleted_keys=set(),
    )
    service = _incremental_service(repository)
    monkeypatch.setattr(schema_router, "nl2sql_service", service)

    page_response = Response()
    page = schema_router.search_objects(page_response, CursorParams(cursor=None, limit=1))
    page_304 = schema_router.search_objects(
        Response(),
        CursorParams(cursor=None, limit=1),
        if_none_match=page_response.headers["etag"],
    )
    cursor_page = schema_router.search_objects(
        Response(),
        CursorParams(cursor=page.data.next_cursor, limit=1),  # type: ignore[union-attr]
        include_counts=False,
    )
    detail_response = Response()
    detail = schema_router.object_detail("APP", "ORDERS", detail_response)
    detail_304 = schema_router.object_detail(
        "APP",
        "ORDERS",
        Response(),
        if_none_match=detail_response.headers["etag"],
    )

    assert page.data.counts_included is True  # type: ignore[union-attr]
    assert page_response.headers["etag"] == '"schema-1"'
    assert page_304.status_code == 304  # type: ignore[union-attr]
    assert cursor_page.data.counts_included is False  # type: ignore[union-attr]
    assert cursor_page.data.total is None  # type: ignore[union-attr]
    assert detail.data.table.table_name == "ORDERS"  # type: ignore[union-attr]
    assert detail_304.status_code == 304  # type: ignore[union-attr]


def test_db_admin_object_page_is_lightweight_filterable_and_etagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import Response

    from app.features.nl2sql import router as nl2sql_router

    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    monkeypatch.setattr(get_settings(), "oracle_user", "APP")
    catalog = SchemaCatalog(
        refreshed_at="2026-07-22T00:00:00+00:00",
        tables=[
            _table("ORDERS").model_copy(update={"row_count": 10}),
            _table("EMPTY_ORDERS").model_copy(update={"row_count": 0}),
            _table("PAYMENTS").model_copy(update={"row_count": 5}),
            _table("V_ORDERS").model_copy(update={"table_type": "VIEW", "row_count": None}),
        ],
    )
    manifest = {(table.owner, table.table_name): "v1" for table in catalog.tables}
    repository.apply_schema_refresh(
        catalog=catalog,
        manifest=manifest,
        changed_keys=set(manifest),
        deleted_keys=set(),
    )
    service = _incremental_service(repository)
    monkeypatch.setattr(
        service,
        "get_catalog",
        lambda: pytest.fail("lightweight object page must not load full catalog"),
    )
    monkeypatch.setattr(
        service,
        "get_catalog_head",
        lambda: pytest.fail("object page metadata must come from the same read-model query"),
    )
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    response = Response()
    result = nl2sql_router.db_admin_objects(
        response=response,
        paging=CursorParams(cursor=None, limit=1),
        type="table",
        row_state="with_rows",
    )
    etag = response.headers["etag"]
    not_modified = nl2sql_router.db_admin_objects(
        response=Response(),
        paging=CursorParams(cursor=None, limit=1),
        type="table",
        row_state="with_rows",
        if_none_match=etag,
    )

    assert not isinstance(result, Response)
    data = result.data
    assert data is not None
    next_page = nl2sql_router.db_admin_objects(
        response=Response(),
        paging=CursorParams(cursor=data.next_cursor, limit=1),
        type="table",
        row_state="with_rows",
        include_counts=False,
    )
    assert [item.name for item in data.items] == ["ORDERS"]
    assert data.total == 2
    assert data.table_count == 2
    assert data.view_count == 0
    assert data.counts_included is True
    assert data.catalog_version == 1
    assert not isinstance(next_page, Response)
    assert next_page.data is not None
    assert [item.name for item in next_page.data.items] == ["PAYMENTS"]
    assert next_page.data.total == 0
    assert next_page.data.counts_included is False
    assert isinstance(not_modified, Response)
    assert not_modified.status_code == 304


def test_schema_query_programming_error_does_not_close_persistence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Repository(MemoryIncrementalNl2SqlRepository):
        def search_schema_objects(self, **_kwargs: Any) -> Any:
            raise RuntimeError("ORA-00937: not a single-group group function")

    service = _incremental_service(Repository(seed_default=False))
    monkeypatch.setattr(get_settings(), "oracle_user", "APP")

    with pytest.raises(Nl2SqlRepositoryOperationFailed) as error:
        service.list_db_admin_objects_page(
            cursor=None,
            limit=10,
            query="",
            object_type="all",
            row_state="all",
        )

    assert error.value.reason_code == "schema_object_query_failed"
    status = service.persistence_status()
    assert status.ready is True
    assert status.writable is True
    assert status.circuit_state == "closed"


def test_db_admin_objects_page_filters_by_owner() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    repository.apply_schema_refresh(
        catalog=SchemaCatalog(
            refreshed_at="now",
            tables=[
                _table("ORDERS").model_copy(update={"owner": "APP"}),
                _table("ORDERS").model_copy(update={"owner": "ADMIN"}),
            ],
        ),
        manifest={("APP", "ORDERS"): "v1", ("ADMIN", "ORDERS"): "v1"},
        changed_keys={("APP", "ORDERS"), ("ADMIN", "ORDERS")},
        deleted_keys=set(),
    )
    service = _incremental_service(repository)

    page = service.list_db_admin_objects_page(
        cursor=None,
        limit=10,
        query="",
        object_type="all",
        row_state="all",
        owner="ADMIN",
    )

    assert [item.qualified_name for item in page.items] == ["ADMIN.ORDERS"]


def test_db_admin_objects_page_filters_by_owner_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import Response

    from app.features.nl2sql import router as nl2sql_router

    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    repository.apply_schema_refresh(
        catalog=SchemaCatalog(
            refreshed_at="now",
            tables=[
                _table("AAI_FILES").model_copy(update={"owner": "OML_USER"}),
                _table("EVENTS").model_copy(update={"owner": "OML_APP"}),
                _table("ORDERS").model_copy(update={"owner": "APP"}),
            ],
        ),
        manifest={
            ("OML_USER", "AAI_FILES"): "v1",
            ("OML_APP", "EVENTS"): "v1",
            ("APP", "ORDERS"): "v1",
        },
        changed_keys={
            ("OML_USER", "AAI_FILES"),
            ("OML_APP", "EVENTS"),
            ("APP", "ORDERS"),
        },
        deleted_keys=set(),
    )
    service = _incremental_service(repository)
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    result = nl2sql_router.db_admin_objects(
        response=Response(),
        paging=CursorParams(cursor=None, limit=10),
        q="",
        type="all",
        row_state="all",
        owner_prefix="oml_",
    )

    assert not isinstance(result, Response)
    assert result.data is not None
    assert [item.qualified_name for item in result.data.items] == [
        "OML_APP.EVENTS",
        "OML_USER.AAI_FILES",
    ]


def test_db_admin_objects_name_comment_scope_excludes_owner_logical_name_and_columns() -> None:
    billing_orders = _table("BILLING_ORDERS")
    billing_comment = _table("ARCHIVE", comment="BILLING report")
    owner_only = _table("CUSTOMERS").model_copy(update={"owner": "BILLING"})
    logical_name_only = _table("INVOICES").model_copy(update={"logical_name": "BILLING"})
    column_only = _table("PAYMENTS").model_copy(
        update={
            "columns": [
                SchemaColumn(
                    column_name="BILLING_CODE",
                    logical_name="BILLING",
                    data_type="VARCHAR2",
                    nullable=True,
                )
            ]
        }
    )
    tables = [
        billing_orders,
        billing_comment,
        owner_only,
        logical_name_only,
        column_only,
    ]
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    manifest = {(table.owner.upper(), table.table_name.upper()): "v1" for table in tables}
    repository.apply_schema_refresh(
        catalog=SchemaCatalog(refreshed_at="now", tables=tables),
        manifest=manifest,
        changed_keys=set(manifest),
        deleted_keys=set(),
    )
    service = _incremental_service(repository)

    scoped = service.list_db_admin_objects_page(
        cursor=None,
        limit=10,
        query="billing",
        query_scope="name_comment",
        object_type="all",
        row_state="all",
    )
    compatible_default = service.list_db_admin_objects_page(
        cursor=None,
        limit=10,
        query="billing",
        object_type="all",
        row_state="all",
    )

    assert {item.qualified_name for item in scoped.items} == {
        "APP.ARCHIVE",
        "APP.BILLING_ORDERS",
    }
    assert {item.qualified_name for item in compatible_default.items} == {
        "APP.ARCHIVE",
        "APP.BILLING_ORDERS",
        "APP.INVOICES",
        "APP.PAYMENTS",
        "BILLING.CUSTOMERS",
    }


def test_db_admin_objects_rejects_invalid_query_scope() -> None:
    from app.features.nl2sql import router as nl2sql_router

    with pytest.raises(HTTPException) as error:
        nl2sql_router.db_admin_objects(
            response=Response(),
            paging=CursorParams(cursor=None, limit=50),
            query_scope="owner",  # type: ignore[arg-type]
        )

    assert error.value.status_code == 422
    assert error.value.detail == "query_scope が不正です。"


def test_connection_failure_opens_circuit_and_next_probe_recovers(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Repository(MemoryIncrementalNl2SqlRepository):
        fail_search = True
        check_calls = 0

        def search_schema_objects(self, **kwargs: Any) -> Any:
            if self.fail_search:
                raise RuntimeError("ORA-12514: listener does not know service")
            return super().search_schema_objects(**kwargs)

        def check(self) -> tuple[bool, str]:
            self.check_calls += 1
            return True, "ready"

    repository = Repository(seed_default=False)
    catalog = SchemaCatalog(refreshed_at="now", tables=[_table("ORDERS")])
    repository.apply_schema_refresh(
        catalog=catalog,
        manifest={("APP", "ORDERS"): "v1"},
        changed_keys={("APP", "ORDERS")},
        deleted_keys=set(),
    )
    service = _incremental_service(repository)
    monkeypatch.setattr(get_settings(), "oracle_user", "APP")

    with pytest.raises(Nl2SqlPersistenceUnavailable) as error:
        service.list_db_admin_objects_page(
            cursor=None,
            limit=10,
            query="",
            object_type="all",
            row_state="all",
        )
    assert error.value.reason_code == "oracle_connection_unavailable"
    assert service.persistence_status().circuit_state == "open"
    record = next(
        r for r in caplog.records if r.message == "nl2sql_incremental_repository_connection_failed"
    )
    assert record.__dict__["diagnostic_category"] == "service_not_registered"
    assert record.__dict__["oracle_error_codes"] == ["ORA-12514"]
    assert record.__dict__["operation"]

    repository.fail_search = False
    service._persistence_retry_at = 0.0  # noqa: SLF001 - half-open contract
    service.ensure_persistence_available()
    page = service.list_db_admin_objects_page(
        cursor=None,
        limit=10,
        query="",
        object_type="all",
        row_state="all",
    )

    assert repository.check_calls == 1
    assert [item.name for item in page.items] == ["ORDERS"]
    assert service.persistence_status().circuit_state == "closed"


def test_half_open_recovery_probe_is_singleflight() -> None:
    class Repository(MemoryIncrementalNl2SqlRepository):
        def __init__(self) -> None:
            super().__init__(seed_default=False)
            self.check_calls = 0
            self.entered = threading.Event()
            self.release = threading.Event()

        def check(self) -> tuple[bool, str]:
            self.check_calls += 1
            self.entered.set()
            assert self.release.wait(timeout=1)
            return True, "ready"

    repository = Repository()
    service = _incremental_service(repository)
    service._mark_persistence_unavailable("oracle_connection_unavailable")  # noqa: SLF001
    service._persistence_retry_at = 0.0  # noqa: SLF001

    def attempt() -> str:
        try:
            service.ensure_persistence_available()
        except Nl2SqlPersistenceUnavailable:
            return "unavailable"
        return "ready"

    with ThreadPoolExecutor(max_workers=8) as executor:
        first = executor.submit(attempt)
        assert repository.entered.wait(timeout=1)
        followers = [executor.submit(attempt) for _ in range(7)]
        follower_results = [future.result(timeout=1) for future in followers]
        repository.release.set()
        first_result = first.result(timeout=1)

    assert first_result == "ready"
    assert follower_results == ["unavailable"] * 7
    assert repository.check_calls == 1


def test_half_open_probe_keeps_migration_required_distinct() -> None:
    class Repository(MemoryIncrementalNl2SqlRepository):
        def check(self) -> tuple[bool, str]:
            return False, "migration 6 is required"

    service = _incremental_service(Repository(seed_default=False))
    service._mark_persistence_unavailable("oracle_connection_unavailable")  # noqa: SLF001
    service._persistence_retry_at = 0.0  # noqa: SLF001

    with pytest.raises(Nl2SqlPersistenceUnavailable) as error:
        service.ensure_persistence_available()

    assert error.value.reason_code == "incremental_migration_required"
    assert service.persistence_status().reason_code == "incremental_migration_required"


def test_schema_refresh_submission_coalesces_active_job_without_running_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    service = _incremental_service(repository)
    called = 0

    def fail_if_run(_job_id: str | None) -> bool:
        nonlocal called
        called += 1
        return False

    monkeypatch.setattr(service, "_run_schema_refresh_job", fail_if_run)
    first = service.start_schema_refresh_job(dispatch=False)
    second = service.start_schema_refresh_job(dispatch=False)

    assert first.job_id == second.job_id
    assert first.status == SchemaRefreshJobStatus.PENDING
    assert called == 0


def test_active_schema_refresh_job_returns_only_pending_or_running_job() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    service = _incremental_service(repository)

    assert service.get_active_schema_refresh_job() is None

    submitted = service.start_schema_refresh_job(dispatch=False)
    active = service.get_active_schema_refresh_job()
    assert active is not None
    assert active.job_id == submitted.job_id

    repository.save_refresh_job(
        submitted.model_copy(
            update={
                "status": SchemaRefreshJobStatus.DONE,
                "finished_at": datetime.now(UTC).isoformat(),
            }
        )
    )
    assert service.get_active_schema_refresh_job() is None


def test_schema_refresh_submission_wakes_coalesced_pending_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    service = _incremental_service(repository)
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_schema_refresh_worker_enabled", True)
    monkeypatch.setattr(settings, "nl2sql_schema_refresh_worker_mode", "inprocess")
    dispatched: list[str] = []
    monkeypatch.setattr(
        service,
        "_dispatch_schema_refresh_job",
        lambda job_id: dispatched.append(job_id) or True,  # type: ignore[func-returns-value]
    )

    first = service.start_schema_refresh_job(dispatch=False)
    second = service.start_schema_refresh_job()

    assert second.job_id == first.job_id
    assert second.status == SchemaRefreshJobStatus.PENDING
    assert dispatched == [first.job_id]


def test_schema_refresh_poll_wakes_expired_running_job_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    service = _incremental_service(repository)
    settings = get_settings()
    monkeypatch.setattr(settings, "nl2sql_schema_refresh_worker_enabled", True)
    monkeypatch.setattr(settings, "nl2sql_schema_refresh_worker_mode", "inprocess")
    dispatched: list[str] = []
    monkeypatch.setattr(
        service,
        "_dispatch_schema_refresh_job",
        lambda job_id: dispatched.append(job_id) or True,  # type: ignore[func-returns-value]
    )
    now = datetime.now(UTC)
    expired = SchemaRefreshJob(
        job_id="expired-refresh",
        created_at=now.isoformat(),
        status=SchemaRefreshJobStatus.RUNNING,
        lease_expires_at=(now - timedelta(seconds=1)).isoformat(),
    )
    active = SchemaRefreshJob(
        job_id="active-refresh",
        created_at=now.isoformat(),
        status=SchemaRefreshJobStatus.RUNNING,
        lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
    )
    repository.save_refresh_job(expired)
    repository.save_refresh_job(active)

    assert service.get_schema_refresh_job(expired.job_id) is not None
    assert service.get_schema_refresh_job(active.job_id) is not None
    assert dispatched == [expired.job_id]

    monkeypatch.setattr(settings, "nl2sql_schema_refresh_worker_mode", "external")
    repository.save_refresh_job(expired.model_copy(update={"job_id": "external-refresh"}))
    assert service.get_schema_refresh_job("external-refresh") is not None
    assert dispatched == [expired.job_id]


def test_twenty_object_reads_do_not_wait_for_schema_refresh_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    catalog = SchemaCatalog(refreshed_at="now", tables=[_table("ORDERS")])
    repository.apply_schema_refresh(
        catalog=catalog,
        manifest={("APP", "ORDERS"): "v1"},
        changed_keys={("APP", "ORDERS")},
        deleted_keys=set(),
    )
    service = _incremental_service(repository)
    monkeypatch.setattr(get_settings(), "oracle_user", "APP")
    assert service._schema_refresh_lock.acquire(blocking=False)  # noqa: SLF001
    try:
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = [
                executor.submit(
                    service.list_db_admin_objects_page,
                    cursor=None,
                    limit=10,
                    query="",
                    object_type="all",
                    row_state="all",
                )
                for _ in range(10)
            ] + [executor.submit(service.get_schema_object, "APP", "ORDERS") for _ in range(10)]
            results = [future.result(timeout=1) for future in futures]
    finally:
        service._schema_refresh_lock.release()  # noqa: SLF001

    assert len(results) == 20
    assert all(result is not None for result in results)


class _RefreshAdapter:
    def __init__(
        self,
        manifest: dict[tuple[str, str], str],
        changed_catalog: SchemaCatalog,
        *,
        fail: bool = False,
    ) -> None:
        self.manifest = manifest
        self.changed_catalog = changed_catalog
        self.fail = fail
        self.requested_keys: set[tuple[str, str]] = set()
        self.manifest_requested_keys: set[tuple[str, str]] | None = None

    def fetch_schema_manifest(
        self,
        object_keys: set[tuple[str, str]] | None = None,
    ) -> dict[tuple[str, str], str]:
        if self.fail:
            raise RuntimeError("manifest failed")
        self.manifest_requested_keys = set(object_keys) if object_keys is not None else None
        if object_keys is None:
            return dict(self.manifest)
        return {key: value for key, value in self.manifest.items() if key in object_keys}

    def fetch_catalog_objects(self, keys: set[tuple[str, str]]) -> SchemaCatalog:
        self.requested_keys = set(keys)
        return self.changed_catalog.model_copy(deep=True)

    def catalog_fingerprint(self, catalog: SchemaCatalog) -> str:
        return "fingerprint:" + ",".join(sorted(table.table_name for table in catalog.tables))


def test_schema_refresh_is_incremental_and_deletes_missing_objects() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    initial = SchemaCatalog(
        refreshed_at="before",
        tables=[_table("A"), _table("B"), _table("D")],
        view_dependencies=[
            SchemaViewDependency(
                owner="APP",
                view_name="D",
                referenced_owner="APP",
                referenced_name="A",
                referenced_type="TABLE",
            )
        ],
    )
    repository.apply_schema_refresh(
        catalog=initial,
        manifest={("APP", "A"): "v1", ("APP", "B"): "v1", ("APP", "D"): "v1"},
        changed_keys={("APP", "A"), ("APP", "B"), ("APP", "D")},
        deleted_keys=set(),
    )
    adapter = _RefreshAdapter(
        {("APP", "A"): "v2", ("APP", "C"): "v1", ("APP", "D"): "v1"},
        SchemaCatalog(
            refreshed_at="next",
            tables=[_table("A", comment="changed"), _table("C")],
        ),
    )
    service = _incremental_service(repository)
    service._oracle_adapter = adapter  # type: ignore[assignment]  # noqa: SLF001
    service._use_oracle_runtime = lambda: True  # type: ignore[method-assign]  # noqa: SLF001

    job = service.start_schema_refresh_job(dispatch=False)
    service._run_schema_refresh_job(job.job_id)  # noqa: SLF001

    completed = service.get_schema_refresh_job(job.job_id)
    assert completed is not None
    assert completed.status == SchemaRefreshJobStatus.DONE
    assert completed.changed_objects == 2
    assert completed.deleted_objects == 1
    assert adapter.requested_keys == {("APP", "A"), ("APP", "C")}
    catalog = repository.load_catalog()
    table_by_name = {table.table_name: table for table in catalog.tables}
    assert set(table_by_name) == {"A", "C", "D"}
    assert table_by_name["A"].comment == "changed"
    assert [(item.view_name, item.referenced_name) for item in catalog.view_dependencies] == [
        ("D", "A")
    ]


def test_targeted_schema_refresh_fetches_only_target_objects() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    initial = SchemaCatalog(
        refreshed_at="before",
        tables=[_table("A"), _table("B")],
    )
    repository.apply_schema_refresh(
        catalog=initial,
        manifest={("APP", "A"): "v1", ("APP", "B"): "v1"},
        changed_keys={("APP", "A"), ("APP", "B")},
        deleted_keys=set(),
    )
    adapter = _RefreshAdapter(
        {("APP", "A"): "v2"},
        SchemaCatalog(refreshed_at="next", tables=[_table("A", comment="targeted")]),
    )
    service = _incremental_service(repository)
    service._oracle_adapter = adapter  # type: ignore[assignment]  # noqa: SLF001
    service._use_oracle_runtime = lambda: True  # type: ignore[method-assign]  # noqa: SLF001

    job = service.start_schema_refresh_job(
        dispatch=False,
        mode=SchemaRefreshMode.TARGETED,
        source="test",
        target_objects=[
            SchemaRefreshTargetObject(
                owner="APP",
                object_name="A",
                object_type="table",
                expected_state="present",
            )
        ],
    )
    service._run_schema_refresh_job(job.job_id)  # noqa: SLF001

    completed = service.get_schema_refresh_job(job.job_id)
    assert completed is not None
    assert completed.status == SchemaRefreshJobStatus.DONE
    assert completed.mode == SchemaRefreshMode.TARGETED
    assert completed.changed_objects == 1
    assert completed.deleted_objects == 0
    assert adapter.manifest_requested_keys == {("APP", "A")}
    assert adapter.requested_keys == {("APP", "A")}
    catalog = repository.load_catalog()
    table_by_name = {table.table_name: table for table in catalog.tables}
    assert set(table_by_name) == {"A", "B"}
    assert table_by_name["A"].comment == "targeted"
    assert repository.schema_manifest() == {("APP", "A"): "v2", ("APP", "B"): "v1"}


def test_targeted_schema_refresh_deletes_only_target_object() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    initial = SchemaCatalog(
        refreshed_at="before",
        tables=[_table("A"), _table("B")],
    )
    repository.apply_schema_refresh(
        catalog=initial,
        manifest={("APP", "A"): "v1", ("APP", "B"): "v1"},
        changed_keys={("APP", "A"), ("APP", "B")},
        deleted_keys=set(),
    )
    adapter = _RefreshAdapter({}, SchemaCatalog(refreshed_at="next", tables=[]))
    service = _incremental_service(repository)
    service._oracle_adapter = adapter  # type: ignore[assignment]  # noqa: SLF001
    service._use_oracle_runtime = lambda: True  # type: ignore[method-assign]  # noqa: SLF001

    job = service.start_schema_refresh_job(
        dispatch=False,
        mode=SchemaRefreshMode.TARGETED,
        source="test",
        target_objects=[
            SchemaRefreshTargetObject(
                owner="APP",
                object_name="A",
                object_type="table",
                expected_state="absent",
            )
        ],
    )
    service._run_schema_refresh_job(job.job_id)  # noqa: SLF001

    completed = service.get_schema_refresh_job(job.job_id)
    assert completed is not None
    assert completed.status == SchemaRefreshJobStatus.DONE
    assert completed.changed_objects == 0
    assert completed.deleted_objects == 1
    assert adapter.manifest_requested_keys == {("APP", "A")}
    assert adapter.requested_keys == set()
    assert [table.table_name for table in repository.load_catalog().tables] == ["B"]
    assert repository.schema_manifest() == {("APP", "B"): "v1"}


def test_targeted_schema_refresh_requires_full_when_expected_state_differs() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    initial = SchemaCatalog(refreshed_at="before", tables=[_table("A")])
    repository.apply_schema_refresh(
        catalog=initial,
        manifest={("APP", "A"): "v1"},
        changed_keys={("APP", "A")},
        deleted_keys=set(),
    )
    adapter = _RefreshAdapter(
        {("APP", "A"): "v2"},
        SchemaCatalog(refreshed_at="next", tables=[_table("A", comment="unexpected")]),
    )
    service = _incremental_service(repository)
    service._oracle_adapter = adapter  # type: ignore[assignment]  # noqa: SLF001
    service._use_oracle_runtime = lambda: True  # type: ignore[method-assign]  # noqa: SLF001

    job = service.start_schema_refresh_job(
        dispatch=False,
        mode=SchemaRefreshMode.TARGETED,
        source="test",
        target_objects=[
            SchemaRefreshTargetObject(
                owner="APP",
                object_name="A",
                object_type="table",
                expected_state="absent",
            )
        ],
    )
    service._run_schema_refresh_job(job.job_id)  # noqa: SLF001

    failed = service.get_schema_refresh_job(job.job_id)
    assert failed is not None
    assert failed.status == SchemaRefreshJobStatus.ERROR
    assert failed.requires_full_refresh is True
    assert failed.error_code == "schema_refresh_full_required"
    assert [table.comment for table in repository.load_catalog().tables] == [""]
    assert repository.schema_manifest() == {("APP", "A"): "v1"}


def test_failed_schema_refresh_keeps_previous_catalog() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    initial = SchemaCatalog(refreshed_at="before", tables=[_table("A")])
    repository.apply_schema_refresh(
        catalog=initial,
        manifest={("APP", "A"): "v1"},
        changed_keys={("APP", "A")},
        deleted_keys=set(),
    )
    service = _incremental_service(repository)
    service._oracle_adapter = _RefreshAdapter(  # type: ignore[assignment]  # noqa: SLF001
        {}, SchemaCatalog(refreshed_at="", tables=[]), fail=True
    )
    service._use_oracle_runtime = lambda: True  # type: ignore[method-assign]  # noqa: SLF001

    job = service.start_schema_refresh_job(dispatch=False)
    service._run_schema_refresh_job(job.job_id)  # noqa: SLF001

    failed = service.get_schema_refresh_job(job.job_id)
    assert failed is not None
    assert failed.status == SchemaRefreshJobStatus.ERROR
    assert [table.table_name for table in repository.load_catalog().tables] == ["A"]
    assert repository.get_catalog_head().catalog_version == 1


def test_schema_refresh_job_reclaims_only_expired_lease() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=False)
    pending = SchemaRefreshJob(job_id="refresh-1", created_at=datetime.now(UTC).isoformat())
    repository.save_refresh_job(pending)

    claimed = repository.claim_refresh_job(
        worker_id="worker-a", lease_seconds=300, job_id=pending.job_id
    )
    assert claimed is not None
    assert claimed.status == SchemaRefreshJobStatus.RUNNING
    assert claimed.attempt == 1
    assert (
        repository.claim_refresh_job(worker_id="worker-b", lease_seconds=300, job_id=pending.job_id)
        is None
    )

    repository.save_refresh_job(
        claimed.model_copy(
            update={"lease_expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
        )
    )
    reclaimed = repository.claim_refresh_job(
        worker_id="worker-b", lease_seconds=300, job_id=pending.job_id
    )
    assert reclaimed is not None
    assert reclaimed.worker_id == "worker-b"
    assert reclaimed.attempt == 2


def test_incremental_service_construction_does_not_open_oracle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql import service as service_module

    settings = Settings(
        nl2sql_runtime_mode="oracle",
        nl2sql_persistence_mode="oracle",
    )
    opened = 0

    def forbidden_connection(_self: Any) -> Any:
        nonlocal opened
        opened += 1
        raise AssertionError("constructor must not connect")

    monkeypatch.setattr(service_module, "get_settings", lambda: settings)
    monkeypatch.setattr(OracleNl2SqlAdapter, "connection", forbidden_connection)

    service = service_module.Nl2SqlService()

    assert service.uses_incremental_store is True
    assert opened == 0
    assert service.persistence_status().model_dump()["snapshot_loaded"] is False


async def test_fastapi_lifespan_does_not_bootstrap_security_or_load_business_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.main import lifespan
    from app.security.service import SecurityService

    calls = 0

    def forbidden_bootstrap(_self: SecurityService) -> None:
        nonlocal calls
        calls += 1
        raise AssertionError("security bootstrap belongs to the first login request")

    monkeypatch.setattr(SecurityService, "ensure_bootstrapped", forbidden_bootstrap)
    application = FastAPI()

    async with lifespan(application):
        assert application.state.services.nl2sql is not None

    assert calls == 0


def test_incremental_readiness_is_one_scalar_query_and_reads_no_clob() -> None:
    class Cursor:
        def __init__(self) -> None:
            self.executed: list[str] = []

        def __enter__(self) -> Cursor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, sql: str, _binds: Any = None) -> None:
            self.executed.append(sql)

        def fetchall(self) -> list[tuple[int]]:
            return [(3,), (5,), (6,)]

    class Connection:
        def __init__(self, cursor: Cursor) -> None:
            self._cursor = cursor

        def cursor(self) -> Cursor:
            return self._cursor

    cursor = Cursor()

    @contextmanager
    def connection_factory() -> Iterator[Connection]:
        yield Connection(cursor)

    repository = OracleIncrementalNl2SqlRepository(connection_factory=connection_factory)
    ready, _detail = repository.check()

    assert ready is True
    assert len(cursor.executed) == 1
    assert "NL2SQL_SCHEMA_MIGRATIONS" in cursor.executed[0]
    assert "CLOB" not in cursor.executed[0].upper()


def test_oracle_schema_object_search_aggregates_before_joining_catalog_head() -> None:
    repository, connections = _oracle_repository(
        [
            [
                (
                    "APP",
                    "ORDERS",
                    "TABLE",
                    "受注",
                    "",
                    10,
                    3,
                    "2026-07-22T00:00:00+00:00",
                )
            ],
            [(1, 1, 0, 7, "2026-07-22T00:00:00+00:00")],
        ],
        [[], [(0, None, None, None, None)]],
    )

    page = repository.search_schema_objects(
        cursor=None,
        limit=10,
        query="受注",
        owner="APP",
        object_type="TABLE",
        allowed_names=None,
        row_state="with_rows",
    )
    empty = repository.search_schema_objects(
        cursor=None,
        limit=10,
        query="missing",
        owner="APP",
        object_type="",
        allowed_names=None,
        row_state="all",
    )

    assert [item.object_name for item in page.items] == ["ORDERS"]
    assert (page.total, page.table_count, page.view_count, page.catalog_version) == (
        1,
        1,
        0,
        7,
    )
    assert (empty.total, empty.table_count, empty.view_count, empty.catalog_version) == (
        0,
        0,
        0,
        0,
    )
    aggregate_sql = connections[0].executed[1][0]
    assert "FROM (SELECT COUNT(*) TOTAL_COUNT" in aggregate_sql
    assert "LEFT JOIN NL2SQL_SCHEMA_CATALOG_HEAD" in aggregate_sql
    assert "(SELECT h.CATALOG_VERSION" not in aggregate_sql
    assert connections[0].executed[1][1] == {
        "owner": "APP",
        "object_type": "TABLE",
        "query": "%受注%",
    }


def test_oracle_schema_object_search_can_skip_counts() -> None:
    repository, connections = _oracle_repository(
        [
            [
                (
                    "APP",
                    "PAYMENTS",
                    "TABLE",
                    "入金",
                    "",
                    5,
                    3,
                    "2026-07-22T00:00:00+00:00",
                )
            ],
        ],
    )

    page = repository.search_schema_objects(
        cursor=None,
        limit=10,
        query="",
        owner="APP",
        object_type="TABLE",
        allowed_names=None,
        row_state="all",
        include_counts=False,
    )

    assert [item.object_name for item in page.items] == ["PAYMENTS"]
    assert page.counts_included is False
    assert page.total is None
    assert page.table_count == 0
    assert page.view_count == 0
    assert page.catalog_version == 0
    assert len(connections[0].executed) == 1
    assert "COUNT(*)" not in connections[0].executed[0][0]


def test_oracle_schema_object_search_uses_literal_owner_prefix() -> None:
    repository, connections = _oracle_repository(
        [
            [
                (
                    "OML_USER",
                    "AAI_FILES",
                    "TABLE",
                    "AAI_FILES",
                    "",
                    5,
                    3,
                    "2026-07-22T00:00:00+00:00",
                )
            ],
        ],
    )

    page = repository.search_schema_objects(
        cursor=None,
        limit=10,
        query="",
        owner="",
        owner_prefix="oml_",
        object_type="TABLE",
        allowed_names=None,
        row_state="all",
        include_counts=False,
    )

    assert [(item.owner, item.object_name) for item in page.items] == [("OML_USER", "AAI_FILES")]
    sql, binds = connections[0].executed[0]
    assert "SUBSTR(UPPER(o.OWNER_NAME), 1, LENGTH(:owner_prefix))" in sql
    assert "OWNER_NAME LIKE :owner_prefix" not in sql
    assert binds == {"limit": 11, "owner_prefix": "OML_", "object_type": "TABLE"}


def test_oracle_schema_object_search_name_comment_scope_uses_only_object_fields() -> None:
    repository, connections = _oracle_repository(
        [
            [
                (
                    "APP",
                    "BILLING_ORDERS",
                    "TABLE",
                    "請求",
                    "Billing report",
                    5,
                    3,
                    "2026-07-22T00:00:00+00:00",
                )
            ],
        ],
    )

    page = repository.search_schema_objects(
        cursor=None,
        limit=10,
        query="billing",
        query_scope="name_comment",
        owner="",
        object_type="TABLE",
        allowed_names=None,
        row_state="all",
        include_counts=False,
    )

    assert [item.object_name for item in page.items] == ["BILLING_ORDERS"]
    sql, binds = connections[0].executed[0]
    assert "UPPER(o.OBJECT_NAME) LIKE :query OR UPPER(o.COMMENTS) LIKE :query" in sql
    assert "UPPER(o.OWNER_NAME) LIKE :query" not in sql
    assert "UPPER(o.LOGICAL_NAME) LIKE :query" not in sql
    assert "NL2SQL_SCHEMA_COLUMNS c" not in sql
    assert binds == {
        "limit": 11,
        "object_type": "TABLE",
        "query": "%BILLING%",
    }


async def test_lifespan_interrupts_build_jobs_off_loop_before_closing_pools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import app.main as main

    loop_thread = threading.get_ident()
    calls: list[str] = []

    def shutdown() -> None:
        assert threading.get_ident() != loop_thread
        calls.append("build_shutdown")

    def preparation_shutdown(runtime: Any) -> None:
        assert threading.get_ident() != loop_thread
        from app.features.nl2sql.ontology_router import ontology_runtime

        assert runtime is ontology_runtime
        calls.append("preparation_shutdown")

    def profile_shutdown() -> None:
        assert threading.get_ident() != loop_thread
        calls.append("profile_shutdown")

    def publish_shutdown() -> None:
        assert threading.get_ident() != loop_thread
        calls.append("publish_shutdown")

    def validation_shutdown(runtime: Any) -> None:
        assert threading.get_ident() != loop_thread
        from app.features.nl2sql.ontology_router import ontology_runtime

        assert runtime is ontology_runtime
        calls.append("validation_shutdown")

    def profile_list_shutdown() -> None:
        assert threading.get_ident() != loop_thread
        calls.append("profile_list_shutdown")

    monkeypatch.setattr(
        "app.main.nl2sql_service.shutdown_select_ai_db_profile_refresh_jobs", profile_list_shutdown
    )
    monkeypatch.setattr(main, "shutdown_validation_jobs", validation_shutdown)
    monkeypatch.setattr("app.main.ontology_publish_service.shutdown", publish_shutdown)
    monkeypatch.setattr("app.main.profile_sync_service.shutdown", profile_shutdown)
    monkeypatch.setattr(main, "shutdown_markdown_preparations", preparation_shutdown)
    monkeypatch.setattr("app.main.ontology_build_service.shutdown", shutdown)
    monkeypatch.setattr(main, "close_oracle_pools", lambda: calls.append("close_pools"))
    async with main.lifespan(FastAPI()):
        assert calls == []
    assert calls == [
        "profile_shutdown",
        "preparation_shutdown",
        "build_shutdown",
        "publish_shutdown",
        "validation_shutdown",
        "profile_list_shutdown",
        "close_pools",
    ]
