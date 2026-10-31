"""SQL feedback から classifier training data へ連携する回帰テスト。"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException, Request
from pr_backend_core.api import OffsetParams

from app.features.nl2sql.incremental_store import MemoryIncrementalNl2SqlRepository
from app.features.nl2sql.models import (
    ClassifierFeedbackImportRequest,
    ClassifierFeedbackSelection,
    ClassifierPredictRequest,
    ClassifierTrainingExampleUpdateRequest,
    ClassifierTrainRequest,
    FeedbackRating,
    FeedbackRequest,
    HistoryItem,
    Nl2SqlEngine,
    Nl2SqlProfile,
)
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore


class _DisabledEmbeddingClient:
    def is_configured(self) -> bool:
        return False

    def embed_texts(self, _texts: list[str]) -> list[list[float]]:
        raise AssertionError("deterministic fallback should be used")


class _CountingIncrementalRepository(MemoryIncrementalNl2SqlRepository):
    def __init__(self) -> None:
        super().__init__(seed_default=True)
        self.classifier_example_page_reads = 0
        self.classifier_artifact_reads = 0

    def list_documents_page(
        self,
        collection: str,
        *,
        cursor: str | None,
        limit: int,
        profile_id: str = "",
        status: str = "",
        query: str = "",
        payload_filters: Mapping[str, str] | None = None,
        payload_any_filters: Mapping[str, str] | None = None,
        payload_exclude_filters: Mapping[str, str] | None = None,
        profile_ids: Iterable[str] | None = None,
        payload_present_filters: Mapping[str, bool] | None = None,
        created_since: str = "",
        conversation_active_within_days: int = 0,
        sort_key: str = "created_at",
        direction: str = "desc",
        offset: int | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, int]:
        if collection == "classifier_examples":
            self.classifier_example_page_reads += 1
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
            conversation_active_within_days=conversation_active_within_days,
            sort_key=sort_key,
            direction=direction,
            offset=offset,
        )

    def get_document(self, collection: str, entity_id: str) -> dict[str, Any] | None:
        if collection == "singletons" and entity_id == "classifier_artifact":
            self.classifier_artifact_reads += 1
        return super().get_document(collection, entity_id)


def _service() -> Nl2SqlService:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._embedding_client = _DisabledEmbeddingClient()  # type: ignore[assignment]  # noqa: SLF001
    return service


def _history(
    history_id: str,
    question: str,
    *,
    profile_id: str = "default",
    profile_name: str = "標準プロファイル",
) -> HistoryItem:
    return HistoryItem(
        id=history_id,
        question=question,
        engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
        generated_sql="SELECT 1 FROM DUAL",
        created_at="2026-07-19T00:00:00+00:00",
        profile_id=profile_id,
        profile_name=profile_name,
    )


def _append_history(service: Nl2SqlService, item: HistoryItem) -> None:
    service._history.append(item)  # noqa: SLF001 - aggregate setup
    service._persist_entities(  # noqa: SLF001 - persistence contract setup
        [("history", item.id, item.model_dump(mode="json"))]
    )


def _training_xlsx(rows: list[tuple[str, str]]) -> bytes:
    import openpyxl  # type: ignore[import-untyped]

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "ClassifierTraining"
    sheet.append(["PROFILE_ID", "TEXT"])
    for profile_id, text in rows:
        sheet.append([profile_id, text])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_good_feedback_is_reviewed_and_added_idempotently() -> None:
    service = _service()
    item = _history("history-good", "請求金額を確認したい")
    _append_history(service, item)
    service.save_feedback(item.id, FeedbackRating.GOOD, "期待どおり")

    candidates = service.classifier_training_candidates(
        limit=20, status="all", profile_id="", query=""
    )
    assert candidates.pending_count == 1
    assert candidates.items[0].status == "pending"
    assert candidates.items[0].eligible is True

    request = ClassifierFeedbackImportRequest(
        items=[ClassifierFeedbackSelection(history_id=item.id)]
    )
    imported = service.import_classifier_feedback_examples(request)
    repeated = service.import_classifier_feedback_examples(request)

    assert imported.imported_count == 1
    assert repeated.imported_count == 0
    assert repeated.results[0].status == "added"
    example = service.classifier_training_data().examples[0]
    assert example.profile_id == "default"
    assert example.text == item.question
    assert example.source_type == "feedback"
    assert example.source_history_id == item.id

    service.clear_feedback(item.id)
    changed = service.classifier_training_candidates(
        limit=20, status="source_changed", profile_id="", query=""
    )
    assert changed.items[0].training_example_id == example.id


def test_bad_feedback_is_not_a_training_candidate() -> None:
    service = _service()
    item = _history("history-bad", "誤った SQL の質問")
    _append_history(service, item)
    service.save_feedback(item.id, FeedbackRating.BAD, "SQL が違う")

    candidates = service.classifier_training_candidates(
        limit=20, status="all", profile_id="", query=""
    )
    imported = service.import_classifier_feedback_examples(
        ClassifierFeedbackImportRequest(items=[ClassifierFeedbackSelection(history_id=item.id)])
    )

    assert candidates.items == []
    assert imported.imported_count == 0
    assert imported.results[0].status == "source_changed"


def test_concurrent_feedback_confirmation_keeps_one_deterministic_example() -> None:
    service = _service()
    item = _history("history-concurrent", "  請求\u3000金額を確認したい  ")
    _append_history(service, item)
    service.save_feedback(item.id, FeedbackRating.GOOD)
    request = ClassifierFeedbackImportRequest(
        items=[ClassifierFeedbackSelection(history_id=item.id)]
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda _index: service.import_classifier_feedback_examples(request), range(2)
            )
        )

    examples = service.classifier_training_data().examples
    assert len(examples) == 1
    assert examples[0].id == results[0].results[0].training_example_id
    assert examples[0].id == results[1].results[0].training_example_id
    assert sum(result.imported_count for result in results) == 1


def test_conflict_edit_delete_and_stale_model_lifecycle() -> None:
    service = _service()
    service.create_profile(Nl2SqlProfile(id="sales", name="販売", category="sales"))
    service.import_classifier_training_data(
        filename="base.xlsx",
        content=_training_xlsx(
            [
                ("default", "標準の請求を確認したい"),
                ("default", "標準の売上を確認したい"),
                ("sales", "販売の請求を確認したい"),
                ("sales", "販売の売上を確認したい"),
            ]
        ),
        replace=True,
    )
    trained = service.train_classifier(ClassifierTrainRequest())
    assert trained.ready is True
    assert trained.stale is False

    conflict_history = _history(
        "history-conflict",
        "標準の請求を確認したい",
        profile_id="sales",
        profile_name="販売",
    )
    _append_history(service, conflict_history)
    service.save_feedback(conflict_history.id, FeedbackRating.GOOD)
    conflict = service.classifier_training_candidates(
        limit=20, status="conflict", profile_id="", query=""
    )
    assert conflict.items[0].conflict_profile_ids == ["default"]
    assert conflict.items[0].profile_category == "sales"

    fresh = _history(
        "history-fresh",
        "販売地域別の件数を確認したい",
        profile_id="sales",
        profile_name="販売",
    )
    _append_history(service, fresh)
    service.save_feedback(fresh.id, FeedbackRating.GOOD)
    imported = service.import_classifier_feedback_examples(
        ClassifierFeedbackImportRequest(items=[ClassifierFeedbackSelection(history_id=fresh.id)])
    )
    status = service.classifier_status()
    assert status.ready is True
    assert status.stale is True

    example_id = imported.results[0].training_example_id
    assert service.classifier_training_data().examples[-1].profile_category == "sales"
    updated = service.update_classifier_training_example(
        example_id,
        ClassifierTrainingExampleUpdateRequest(
            text="販売地域別の合計を確認したい", profile_id="sales"
        ),
    )
    assert updated.text == "販売地域別の合計を確認したい"
    assert service.train_classifier(ClassifierTrainRequest()).stale is False
    assert service.delete_classifier_training_example(example_id).total_examples == 4
    assert service.classifier_status().stale is True


def test_incremental_restart_can_update_feedback_and_restore_training_examples() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=True)
    first = _service()
    first._incremental_repository = repository  # noqa: SLF001
    first._persistence_ready = True  # noqa: SLF001
    first._persistence_writable = True  # noqa: SLF001
    item = _history("history-restart", "再起動後も利用する質問")
    repository.put_document(
        "history",
        item.id,
        item.model_dump(mode="json"),
        profile_id=item.profile_id,
        status="unrated",
    )

    first.save_feedback(item.id, FeedbackRating.GOOD, "永続化")
    first.import_classifier_feedback_examples(
        ClassifierFeedbackImportRequest(items=[ClassifierFeedbackSelection(history_id=item.id)])
    )

    restarted = _service()
    restarted._incremental_repository = repository  # noqa: SLF001
    restarted._persistence_ready = True  # noqa: SLF001
    restarted._persistence_writable = True  # noqa: SLF001

    feedback = restarted.list_feedback(
        limit=20,
        rating="good",
        profile_id="",
        query="",
    )
    assert feedback.items[0].feedback_comment == "永続化"
    assert feedback.items[0].training_status == "added"
    assert restarted.classifier_training_data().examples[0].source_history_id == item.id


def test_incremental_feedback_pagination_is_not_limited_to_recent_fifty() -> None:
    repository = MemoryIncrementalNl2SqlRepository(seed_default=True)
    for index in range(75):
        item = _history(f"history-{index:03d}", f"ページング確認 {index:03d}")
        payload = item.model_copy(
            update={
                "feedback_rating": FeedbackRating.GOOD if index % 2 == 0 else FeedbackRating.BAD,
                "feedback_comment": "候補" if index % 2 == 0 else "除外",
            }
        ).model_dump(mode="json")
        repository.put_document(
            "history",
            item.id,
            payload,
            profile_id="default",
            status="good" if index % 2 == 0 else "bad",
        )
    service = _service()
    service._incremental_repository = repository  # noqa: SLF001

    first_page = service.list_feedback(
        limit=20, rating="good", profile_id="default", query="ページング"
    )
    second_page = service.list_feedback(
        offset=20,
        limit=20,
        rating="good",
        profile_id="default",
        query="ページング",
    )
    beyond = service.list_feedback(
        offset=100, limit=20, rating="good", profile_id="default", query="ページング"
    )
    unfiltered = service.list_feedback(limit=20, rating="all", profile_id="", query="")
    candidates = service.classifier_training_candidates(
        limit=100, status="pending", profile_id="default", query=""
    )
    candidates_page_2 = service.classifier_training_candidates(
        offset=30, limit=10, status="pending", profile_id="default", query=""
    )

    assert first_page.total == 38
    assert first_page.unfiltered_total == 75
    assert len(first_page.items) == 20
    assert first_page.has_next
    assert len(second_page.items) == 18
    assert second_page.has_next is False
    # 2 ページの間に抜け・重なりが無い。
    assert len({item.id for item in [*first_page.items, *second_page.items]}) == 38
    assert (beyond.items, beyond.total, beyond.has_next) == ([], 38, False)
    assert unfiltered.total == unfiltered.unfiltered_total == 75
    assert candidates.total == 38
    assert candidates.unfiltered_total == 38
    assert (len(candidates_page_2.items), candidates_page_2.offset) == (8, 30)
    assert candidates_page_2.has_next is False


def test_feedback_list_builds_classifier_profile_lookup_once_per_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    for index in range(3):
        item = _history(f"history-{index}", f"候補質問 {index}").model_copy(
            update={
                "feedback_rating": FeedbackRating.GOOD,
                "feedback_comment": "採用候補",
            }
        )
        _append_history(service, item)

    list_profile_calls = 0

    def list_profiles(*, include_archived: bool = False) -> list[Nl2SqlProfile]:
        nonlocal list_profile_calls
        assert include_archived is False
        list_profile_calls += 1
        return [Nl2SqlProfile(id="default", name="標準プロファイル")]

    monkeypatch.setattr(service, "list_profiles", list_profiles)

    feedback = service.list_feedback(
        limit=20,
        rating="good",
        profile_id="",
        query="",
    )

    assert len(feedback.items) == 3
    assert {item.training_status for item in feedback.items} == {"pending"}
    assert list_profile_calls == 1


def test_failed_retraining_preserves_the_active_model(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    service.create_profile(Nl2SqlProfile(id="sales", name="販売"))
    service.import_classifier_training_data(
        filename="base.xlsx",
        content=_training_xlsx(
            [
                ("default", "請求金額を確認したい"),
                ("default", "請求件数を確認したい"),
                ("sales", "販売金額を確認したい"),
                ("sales", "販売件数を確認したい"),
            ]
        ),
        replace=True,
    )
    trained = service.train_classifier(ClassifierTrainRequest())
    service.import_classifier_training_data(
        filename="change.xlsx",
        content=_training_xlsx([("default", "請求明細を確認したい")]),
    )

    def fail_vectors(_texts: list[str]) -> tuple[list[list[float]], list[str], str]:
        raise RuntimeError("embedding failure")

    monkeypatch.setattr(service, "_classifier_vectors", fail_vectors)
    with pytest.raises(ValueError, match="embedding failure"):
        service.train_classifier(ClassifierTrainRequest())

    failed = service.classifier_status()
    assert failed.ready is True
    assert failed.stale is True
    assert failed.classifier_version == trained.classifier_version


def test_classifier_train_requires_two_categories_returns_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql import router as nl2sql_router

    service = _service()
    service.import_classifier_training_data(
        filename="single_category.xlsx",
        content=_training_xlsx(
            [
                ("default", "請求金額を確認したい"),
                ("default", "請求件数を確認したい"),
            ]
        ),
        replace=True,
    )
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    request = cast(Request, SimpleNamespace(state=SimpleNamespace(principal=None)))
    with pytest.raises(HTTPException) as exc_info:
        nl2sql_router.train_classifier(ClassifierTrainRequest(), request)

    assert exc_info.value.status_code == 422
    assert "2 category" in str(exc_info.value.detail)


def test_classifier_train_failure_returns_422(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql import router as nl2sql_router

    service = _service()
    service.create_profile(Nl2SqlProfile(id="sales", name="販売"))
    service.import_classifier_training_data(
        filename="base.xlsx",
        content=_training_xlsx(
            [
                ("default", "請求金額を確認したい"),
                ("sales", "販売金額を確認したい"),
            ]
        ),
        replace=True,
    )

    def fail_vectors(_texts: list[str]) -> tuple[list[list[float]], list[str], str]:
        raise RuntimeError("embedding failure")

    monkeypatch.setattr(service, "_classifier_vectors", fail_vectors)
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)

    request = cast(Request, SimpleNamespace(state=SimpleNamespace(principal=None)))
    with pytest.raises(HTTPException) as exc_info:
        nl2sql_router.train_classifier(ClassifierTrainRequest(), request)

    assert exc_info.value.status_code == 422
    assert "embedding failure" in str(exc_info.value.detail)


def test_classifier_training_upload_rejects_large_file_before_read() -> None:
    from app.features.nl2sql import router as nl2sql_router

    class TooLargeUpload:
        filename = "training_data.xlsx"
        size = nl2sql_router.CLASSIFIER_UPLOAD_MAX_BYTES + 1

        async def read(self, _size: int = -1) -> bytes:
            raise AssertionError("oversized classifier upload must be rejected before read")

    request = cast(Request, SimpleNamespace(state=SimpleNamespace(principal=None)))
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            nl2sql_router.import_classifier_training_data(
                request,
                cast(Any, TooLargeUpload()),
            )
        )

    assert exc_info.value.status_code == 413


def test_consecutive_classifier_predicts_reuse_incremental_state_cache() -> None:
    trainer = _service()
    trainer.create_profile(Nl2SqlProfile(id="sales", name="販売"))
    trainer.import_classifier_training_data(
        filename="base.xlsx",
        content=_training_xlsx(
            [
                ("default", "請求金額を確認したい"),
                ("default", "請求件数を確認したい"),
                ("sales", "販売金額を確認したい"),
                ("sales", "販売件数を確認したい"),
            ]
        ),
        replace=True,
    )
    trainer.train_classifier(ClassifierTrainRequest())
    with trainer._lock:  # noqa: SLF001 - incremental cache fixture
        examples = list(trainer._classifier_examples)  # noqa: SLF001
        artifact = dict(trainer._classifier_artifact or {})  # noqa: SLF001

    repository = _CountingIncrementalRepository()
    for example in examples:
        repository.put_document(
            "classifier_examples",
            example.id,
            example.model_dump(mode="json"),
            profile_id=example.profile_id,
        )
    repository.put_document("singletons", "classifier_artifact", {"value": artifact})

    service = _service()
    service.create_profile(Nl2SqlProfile(id="sales", name="販売"))
    service._incremental_repository = repository  # noqa: SLF001
    service._persistence_ready = True  # noqa: SLF001
    service._persistence_writable = True  # noqa: SLF001

    first = service.predict_classifier(ClassifierPredictRequest(question="販売金額を確認したい"))
    second = service.predict_classifier(ClassifierPredictRequest(question="販売件数を確認したい"))

    assert first.recommendation_source == "classifier"
    assert second.recommendation_source == "classifier"
    assert repository.classifier_example_page_reads == 1
    assert repository.classifier_artifact_reads == 1


def test_feedback_and_training_candidate_api_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql import router as nl2sql_router

    service = _service()
    monkeypatch.setattr(nl2sql_router, "nl2sql_service", service)
    item = _history("history-api", "API から請求金額を確認したい")
    _append_history(service, item)

    anon_request = cast(Request, SimpleNamespace(state=SimpleNamespace(principal=None)))
    with pytest.raises(HTTPException) as missing:
        nl2sql_router.feedback(
            FeedbackRequest(history_id="missing", rating=FeedbackRating.GOOD, comment=""),
            anon_request,
        )
    saved = nl2sql_router.feedback(
        FeedbackRequest(history_id=item.id, rating=FeedbackRating.GOOD, comment="確認済み"),
        anon_request,
    )
    feedback = nl2sql_router.list_feedback(
        anon_request,
        OffsetParams(limit=20, offset=0),
        rating="good",
        profile_id="default",
        q="請求",
    )
    candidates = nl2sql_router.classifier_training_candidates(
        anon_request, OffsetParams(limit=20, offset=0)
    )
    imported = nl2sql_router.import_classifier_training_data_from_feedback(
        ClassifierFeedbackImportRequest(
            items=[ClassifierFeedbackSelection(history_id=item.id, profile_id="default")]
        ),
        anon_request,
    )
    example_id = imported.data.results[0].training_example_id  # type: ignore[union-attr]
    updated = nl2sql_router.update_classifier_training_example(
        example_id,
        ClassifierTrainingExampleUpdateRequest(
            text="API から請求合計を確認したい", profile_id="default"
        ),
        anon_request,
    )
    deleted = nl2sql_router.delete_classifier_training_example(example_id, anon_request)
    with pytest.raises(HTTPException) as delete_missing:
        nl2sql_router.delete_classifier_training_example(example_id, anon_request)
    cleared = nl2sql_router.clear_feedback(item.id, anon_request)
    with pytest.raises(HTTPException) as clear_missing:
        nl2sql_router.clear_feedback("missing", anon_request)

    assert missing.value.status_code == 404
    assert saved.data.history_id == item.id  # type: ignore[union-attr]
    assert feedback.data.items[0].training_status == "pending"  # type: ignore[union-attr]
    assert candidates.data.items[0].status == "pending"  # type: ignore[union-attr]
    assert candidates.data.items[0].profile_category == ""  # type: ignore[union-attr]
    assert imported.data.results[0].profile_id == "default"  # type: ignore[union-attr]
    assert updated.data.text == "API から請求合計を確認したい"  # type: ignore[union-attr]
    assert deleted.data.total_examples == 0  # type: ignore[union-attr]
    assert delete_missing.value.status_code == 404
    assert cleared.data.history_id == item.id  # type: ignore[union-attr]
    assert cleared.data.cleared is True  # type: ignore[union-attr]
    assert clear_missing.value.status_code == 404
