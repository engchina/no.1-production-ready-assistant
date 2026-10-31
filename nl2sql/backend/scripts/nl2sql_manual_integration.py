"""Manual NL2SQL integration smoke test (Oracle runtime + OCI Enterprise AI Direct).

Run from `backend/`:

    uv run python scripts/nl2sql_manual_integration.py --require-oracle --require-enterprise-ai

The script intentionally prints only non-secret summaries. Steps that mutate Oracle are opt-in.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from app.features.nl2sql.models import (
    AllowedObjects,
    AnnotationApplyItem,
    AnnotationApplyRequest,
    ClassifierPredictRequest,
    ClassifierTrainRequest,
    CommentApplyItem,
    CommentApplyRequest,
    CommentSuggestionRequest,
    FeedbackIndexRequest,
    JobCreateRequest,
    JobData,
    JobStatus,
    Nl2SqlEngine,
    Nl2SqlProfile,
    PreviewData,
    PreviewRequest,
    SampleDataMutationRequest,
    SampleDataStep,
    SchemaRefreshJobStatus,
    SimilarHistoryRequest,
)
from app.features.nl2sql.service import nl2sql_service
from app.settings import get_settings

DEFAULT_QUESTION = "登録済みの表から主要な列を一覧して"


@dataclass(frozen=True)
class StepResult:
    name: str
    ok: bool
    message: str


def _parse_engines(value: str) -> list[Nl2SqlEngine]:
    engines: list[Nl2SqlEngine] = []
    for raw in value.split(","):
        normalized = raw.strip()
        if not normalized:
            continue
        try:
            engine = Nl2SqlEngine(normalized)
        except ValueError as exc:
            allowed = ", ".join(engine.value for engine in Nl2SqlEngine)
            raise argparse.ArgumentTypeError(
                f"Unknown engine '{normalized}'. Choose from: {allowed}"
            ) from exc
        engines.append(engine)
    if not engines:
        raise argparse.ArgumentTypeError("At least one engine is required.")
    return engines


def _parse_tables(values: list[str]) -> list[str]:
    tables: list[str] = []
    for value in values:
        tables.extend(part.strip().upper() for part in value.split(",") if part.strip())
    return tables


def _is_disposable_table_name(table_name: str) -> bool:
    return table_name.strip().upper().startswith("NL2SQL_")


def _status_line(result: StepResult) -> str:
    prefix = "ok" if result.ok else "ng"
    return f"[{prefix}] {result.name}: {result.message}"


def _diagnostics(
    require_oracle: bool,
    require_enterprise_ai: bool = False,
    require_feedback_embedding: bool = False,
    require_oracle_persistence: bool = False,
) -> StepResult:
    settings = get_settings()
    diagnostics = nl2sql_service.diagnostics()
    warnings = [check.name for check in diagnostics.checks if check.status != "ok"]
    readiness = _readiness_summary(diagnostics.readiness)
    runtime_mode = settings.nl2sql_runtime_mode.strip().lower()
    if require_oracle and runtime_mode != "oracle":
        return StepResult(
            name="diagnostics",
            ok=False,
            message=f"NL2SQL_RUNTIME_MODE must be oracle, current={runtime_mode or '-'}",
        )
    oracle_ready = next(
        (check for check in diagnostics.checks if check.name == "ORACLE_RUNTIME_READY"), None
    )
    if require_oracle and (oracle_ready is None or oracle_ready.status != "ok"):
        message = oracle_ready.message if oracle_ready else "ORACLE_RUNTIME_READY check missing."
        return StepResult(name="diagnostics", ok=False, message=message)
    enterprise_ready = next(
        (item for item in diagnostics.readiness if item.area == "enterprise_ai_direct"), None
    )
    if require_enterprise_ai and (enterprise_ready is None or enterprise_ready.status != "ok"):
        missing = [
            check.name
            for check in diagnostics.checks
            if check.name.startswith("OCI_ENTERPRISE_AI") and check.status != "ok"
        ]
        detail = ",".join(missing)
        if not detail:
            detail = enterprise_ready.next_action if enterprise_ready else "-"
        return StepResult(
            name="diagnostics",
            ok=False,
            message=f"Enterprise AI Direct is not ready: {detail}",
        )
    feedback_ready = next(
        (item for item in diagnostics.readiness if item.area == "feedback_embedding"), None
    )
    if require_feedback_embedding and (feedback_ready is None or feedback_ready.status != "ok"):
        missing = [
            check.name
            for check in diagnostics.checks
            if (
                check.name.startswith("OCI_GENAI")
                or check.name == "NL2SQL_FEEDBACK_EMBEDDING_ENABLED"
            )
            and check.status != "ok"
        ]
        detail = ",".join(missing)
        if not detail:
            detail = feedback_ready.next_action if feedback_ready else "-"
        return StepResult(
            name="diagnostics",
            ok=False,
            message=f"Feedback embedding is not ready: {detail}",
        )
    persistence_ready = next(
        (item for item in diagnostics.readiness if item.area == "persistence"), None
    )
    if require_oracle_persistence and (
        persistence_ready is None or persistence_ready.status != "ok"
    ):
        detail = persistence_ready.next_action if persistence_ready else "-"
        return StepResult(
            name="diagnostics",
            ok=False,
            message=f"Oracle persistence is not ready: {detail}",
        )
    warning_text = ",".join(warnings) if warnings else "none"
    return StepResult(
        name="diagnostics",
        ok=True,
        message=f"runtime={runtime_mode}; warnings={warning_text}; readiness={readiness}",
    )


def _diagnostics_worker(
    require_oracle: bool,
    require_enterprise_ai: bool,
    require_feedback_embedding: bool,
    require_oracle_persistence: bool,
    queue: Any,
) -> None:
    try:
        queue.put(
            _diagnostics(
                require_oracle,
                require_enterprise_ai,
                require_feedback_embedding,
                require_oracle_persistence,
            )
        )
    except Exception as exc:  # pragma: no cover - subprocess safety boundary
        queue.put(StepResult(name="diagnostics", ok=False, message=str(exc)))


def _diagnostics_with_timeout(
    require_oracle: bool,
    timeout_seconds: float,
    require_enterprise_ai: bool = False,
    require_feedback_embedding: bool = False,
    require_oracle_persistence: bool = False,
) -> StepResult:
    if not require_oracle or timeout_seconds <= 0:
        return _diagnostics(
            require_oracle,
            require_enterprise_ai,
            require_feedback_embedding,
            require_oracle_persistence,
        )
    method = "fork" if "fork" in mp.get_all_start_methods() else "spawn"
    context: Any = mp.get_context(method)
    queue: Any = context.Queue(maxsize=1)
    process: Any = context.Process(
        target=_diagnostics_worker,
        args=(
            require_oracle,
            require_enterprise_ai,
            require_feedback_embedding,
            require_oracle_persistence,
            queue,
        ),
    )
    process.daemon = True
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join(2)
        return StepResult(
            name="diagnostics",
            ok=False,
            message=f"diagnostics timeout after {timeout_seconds:.1f}s",
        )
    if queue.empty():
        return StepResult(
            name="diagnostics", ok=False, message="diagnostics did not return a result"
        )
    return cast(StepResult, queue.get())


def _refresh_catalog(enabled: bool) -> StepResult:
    if not enabled:
        catalog = nl2sql_service.get_catalog()
        return StepResult(
            name="schema_catalog",
            ok=True,
            message=f"using cached catalog; tables={len(catalog.tables)}",
        )
    try:
        job = nl2sql_service.start_schema_refresh_job(
            dispatch=False,
            source="manual_integration",
        )
        if job.status != SchemaRefreshJobStatus.DONE:
            nl2sql_service.run_next_schema_refresh_job()
        completed = nl2sql_service.get_schema_refresh_job(job.job_id) or job
        if completed.status != SchemaRefreshJobStatus.DONE:
            status = completed.status
            status_value = status.value if hasattr(status, "value") else str(status)
            return StepResult(
                name="schema_catalog",
                ok=False,
                message=(
                    f"refresh job {completed.job_id} status={status_value}; "
                    f"error_code={completed.error_code or '-'}"
                ),
            )
        catalog = nl2sql_service.get_catalog()
        catalog_version = completed.catalog_version
    except Exception as exc:
        return StepResult(name="schema_catalog", ok=False, message=str(exc))
    return StepResult(
        name="schema_catalog",
        ok=True,
        message=(
            f"refreshed via job={completed.job_id}; tables={len(catalog.tables)}; "
            f"catalog_version={catalog_version if catalog_version is not None else '-'}; "
            f"refreshed_at={catalog.refreshed_at}"
        ),
    )


def _import_sample_data(enabled: bool) -> StepResult | None:
    if not enabled:
        return None
    try:
        data = nl2sql_service.import_sample_data(
            SampleDataMutationRequest(
                step=SampleDataStep.ALL,
                confirmation="ADMIN_EXECUTE",
            )
        )
    except Exception as exc:
        return StepResult(name="sample_data_import", ok=False, message=str(exc))
    imported = ",".join(nl2sql_service.sample_data_info().imported_objects) or "-"
    return StepResult(
        name="sample_data_import",
        ok=data.executed,
        message=(
            f"runtime={data.runtime}; executed={data.executed}; "
            f"profile={data.profile_id}; imported={imported}; warnings={len(data.warnings)}"
        ),
    )


def _prepare_profile(
    *, profile_id: str | None, allowed_tables: list[str], row_limit: int
) -> tuple[StepResult | None, str | None]:
    if not allowed_tables:
        return None, profile_id
    resolved_profile_id = profile_id or "manual_integration"
    profile = Nl2SqlProfile(
        id=resolved_profile_id,
        name="Manual integration",
        description="Manual NL2SQL integration profile",
        allowed_tables=allowed_tables,
        glossary={},
        sql_rules=["SELECT/WITH のみ", "Oracle runtime manual integration"],
        default_row_limit=row_limit,
        safety_policy="select_only",
        few_shot_examples=[],
        archived=False,
    )
    existing_ids = {item.id for item in nl2sql_service.list_profiles()}
    if resolved_profile_id in existing_ids:
        nl2sql_service.update_profile(resolved_profile_id, lambda _current: profile)
        action = "updated"
    else:
        nl2sql_service.create_profile(profile)
        action = "created"
    return (
        StepResult(
            name="manual_profile",
            ok=True,
            message=(
                f"{action}; profile_id={resolved_profile_id}; tables={','.join(allowed_tables)}"
            ),
        ),
        resolved_profile_id,
    )


def _preview(
    *,
    engine: Nl2SqlEngine,
    profile_id: str | None,
    question: str,
    allowed: AllowedObjects,
    row_limit: int,
    require_oracle: bool,
    require_enterprise_ai: bool,
) -> tuple[StepResult, PreviewData | None]:
    try:
        data = nl2sql_service.preview(
            PreviewRequest(
                question=question,
                engine=engine,
                profile_id=profile_id,
                allowed_objects=allowed,
                row_limit=row_limit,
            )
        )
    except Exception as exc:
        return StepResult(name=f"preview_{engine.value}", ok=False, message=str(exc)), None
    runtime = str(data.engine_meta.get("runtime") or "deterministic")
    ok = data.is_safe
    if require_enterprise_ai and engine == Nl2SqlEngine.ENTERPRISE_AI_DIRECT:
        ok = ok and runtime == "oci_enterprise_ai" and not data.fallback_reason
    message = (
        f"engine={data.engine.value}; runtime={runtime}; safe={data.is_safe}; "
        f"row_limit={data.row_limit}; meta={_engine_meta_summary(data.engine_meta)}; "
        f"sql={_one_line(data.sql)}"
    )
    if data.safety:
        if data.safety.blocked_reason:
            message = f"{message}; blocked={_one_line(data.safety.blocked_reason, 120)}"
        if data.safety.referenced_tables:
            message = f"{message}; tables={','.join(data.safety.referenced_tables)}"
        if data.safety.warnings:
            message = f"{message}; warnings={len(data.safety.warnings)}"
    if data.fallback_reason:
        message = f"{message}; fallback={data.fallback_reason}"
    return StepResult(name=f"preview_{engine.value}", ok=ok, message=message), data


def _job(
    *,
    engine: Nl2SqlEngine,
    profile_id: str | None,
    question: str,
    allowed: AllowedObjects,
    row_limit: int,
    timeout_seconds: float,
    require_oracle: bool,
    require_enterprise_ai: bool,
) -> StepResult:
    try:
        created = nl2sql_service.start_job(
            JobCreateRequest(
                question=question,
                engine=engine,
                profile_id=profile_id,
                allowed_objects=allowed,
                row_limit=row_limit,
            )
        )
        data = _wait_for_job(created.job_id, timeout_seconds)
    except Exception as exc:
        return StepResult(name=f"job_{engine.value}", ok=False, message=str(exc))
    if data is None:
        return StepResult(
            name=f"job_{engine.value}",
            ok=False,
            message=f"timeout after {timeout_seconds:.1f}s",
        )
    result_runtime = (
        str(data.result.engine_meta.get("runtime") or "deterministic") if data.result else "-"
    )
    ok = data.status == JobStatus.DONE and data.result is not None and data.result.safety.is_safe
    if require_enterprise_ai and engine == Nl2SqlEngine.ENTERPRISE_AI_DIRECT:
        ok = (
            ok
            and result_runtime == "oci_enterprise_ai"
            and not (data.result and data.result.fallback_reason)
        )
    message = (
        f"status={data.status.value}; runtime={result_runtime}; "
        f"elapsed_ms={data.elapsed_ms}; rows={data.result.results.total if data.result else 0}; "
        f"meta={_engine_meta_summary(data.result.engine_meta if data.result else {})}"
    )
    if data.error_message:
        message = f"{message}; error={data.error_message}"
    if data.result and data.result.fallback_reason:
        message = f"{message}; fallback={data.result.fallback_reason}"
    return StepResult(name=f"job_{engine.value}", ok=ok, message=message)


def _supporting_features() -> list[StepResult]:
    results: list[StepResult] = []
    try:
        comments = nl2sql_service.suggest_comments()
        first_comment = comments.suggestions[0] if comments.suggestions else None
        message = f"suggestions={len(comments.suggestions)}"
        if first_comment:
            message = (
                f"{message}; first={first_comment.object_name}:"
                f"{_one_line(first_comment.suggested_comment, 80)}"
            )
        results.append(
            StepResult(
                name="support_comments",
                ok=True,
                message=message,
            )
        )
    except Exception as exc:
        results.append(StepResult(name="support_comments", ok=False, message=str(exc)))

    try:
        comments = nl2sql_service.suggest_comments()
        comment_items = [
            CommentApplyItem(
                object_name=item.object_name,
                object_type=item.object_type,
                comment=item.suggested_comment,
            )
            for item in comments.suggestions[: min(len(comments.suggestions), 5)]
        ]
        comment_statements = [nl2sql_service._comment_statement(item) for item in comment_items]
        results.append(
            StepResult(
                name="support_comment_sql_generation",
                ok=len(comment_statements) == len(comment_items),
                message=(f"statements={len(comment_statements)}; warnings=0"),
            )
        )
    except Exception as exc:
        results.append(
            StepResult(name="support_comment_sql_generation", ok=False, message=str(exc))
        )

    try:
        status = nl2sql_service.feedback_index_status()
        results.append(
            StepResult(
                name="support_feedback_index",
                ok=not status.executed and status.vector_dimension == 1536,
                message=(
                    f"status={status.status}; indexable={status.indexable_count}; "
                    f"backend={status.vector_backend}; ddl={len(status.ddl)}"
                ),
            )
        )
    except Exception as exc:
        results.append(StepResult(name="support_feedback_index", ok=False, message=str(exc)))
    return results


def _seed_demo_learning() -> StepResult:
    try:
        data = nl2sql_service.seed_demo_learning_data()
    except Exception as exc:
        return StepResult(name="seed_demo_learning", ok=False, message=str(exc))
    return StepResult(
        name="seed_demo_learning",
        ok=True,
        message=(
            f"history={data.seeded_history_count}; feedback={data.seeded_feedback_count}; "
            f"profiles={','.join(data.profile_ids) or '-'}"
        ),
    )


def _feedback_index_smoke(*, include_bad: bool) -> StepResult:
    try:
        data = nl2sql_service.rebuild_feedback_index(FeedbackIndexRequest(include_bad=include_bad))
    except Exception as exc:
        return StepResult(name="feedback_index_rebuild", ok=False, message=str(exc))
    ok = (
        data.vector_dimension == 1536
        and data.vector_backend == "oracle_26ai"
        and data.executed
        and not data.warnings
    )
    message = (
        f"executed={data.executed}; runtime={data.runtime}; "
        f"status={data.status}; indexable={data.indexable_count}; "
        f"indexed={data.indexed_count}; backend={data.vector_backend}; "
        f"embedding_configured={data.embedding_configured}; warnings={len(data.warnings)}"
    )
    if data.warnings:
        message = f"{message}; first_warning={_one_line(data.warnings[0], 180)}"
    return StepResult(name="feedback_index_rebuild", ok=ok, message=message)


def _legacy_absorption_checks(
    *,
    profile_id: str | None,
    question: str,
    allowed_tables: list[str],
    execute_comments: bool,
    execute_annotations: bool,
    execute_feedback_index: bool,
    require_classifier_oracle_state: bool,
) -> list[StepResult]:
    """Run post-absorption checks for legacy no.1-sql-assist operations."""

    results = [
        _legacy_classifier_smoke(
            question=question,
            require_oracle_state=require_classifier_oracle_state,
        ),
    ]
    results.append(_legacy_comments_smoke(execute=execute_comments, allowed_tables=allowed_tables))
    results.append(
        _legacy_annotations_smoke(execute=execute_annotations, allowed_tables=allowed_tables)
    )
    results.extend(
        _legacy_feedback_vector_smoke(
            question=question,
            profile_id=profile_id,
            execute_index=execute_feedback_index,
        )
    )
    return results


def _legacy_classifier_smoke(*, question: str, require_oracle_state: bool) -> StepResult:
    try:
        store_mode = str(getattr(nl2sql_service._store, "mode", "memory"))
        if require_oracle_state and store_mode != "oracle":
            return StepResult(
                name="legacy_classifier",
                ok=False,
                message=f"Classifier Oracle state is not ready: persistence_mode={store_mode}",
            )
        payload = "\n".join(
            [
                "CATEGORY,TEXT",
                "社員管理,社員と部署の一覧を確認したい",
                "社員管理,部署別の社員数を確認したい",
                "プロジェクト管理,部署別のプロジェクトを確認したい",
                "プロジェクト管理,プロジェクトの予算を確認したい",
            ]
        ).encode("utf-8")
        imported = nl2sql_service.import_classifier_training_data(
            filename="training_data.csv",
            content=payload,
            replace=True,
        )
        trained = nl2sql_service.train_classifier(ClassifierTrainRequest())
        predicted = nl2sql_service.predict_classifier(
            ClassifierPredictRequest(question=question, top_k=2)
        )
    except Exception as exc:
        return StepResult(name="legacy_classifier", ok=False, message=str(exc))
    ok = (
        imported.imported_count >= 4
        and trained.ready
        and predicted.recommendation_source == "classifier"
    )
    return StepResult(
        name="legacy_classifier",
        ok=ok,
        message=(
            f"store={store_mode}; imported={imported.imported_count}; "
            f"ready={trained.ready}; examples={trained.example_count}; "
            f"categories={trained.category_count}; source={predicted.recommendation_source}; "
            f"version={trained.classifier_version or '-'}"
        ),
    )


def _legacy_comments_smoke(*, execute: bool, allowed_tables: list[str]) -> StepResult:
    if not execute:
        return StepResult(
            name="legacy_comments_apply",
            ok=True,
            message="skipped; explicit execute flag required",
        )
    try:
        allowed = {table.strip().upper() for table in allowed_tables if table.strip()}
        suggestions = nl2sql_service.suggest_comments(
            CommentSuggestionRequest(use_llm=False, max_items=500 if allowed else 5)
        )
        filtered_suggestions = [
            item
            for item in suggestions.suggestions
            if not allowed or item.object_name.split(".", 1)[0].strip().upper() in allowed
        ]
        items = [
            CommentApplyItem(
                object_name=item.object_name,
                object_type=item.object_type,
                comment=item.suggested_comment,
            )
            for item in filtered_suggestions[:1]
        ]
        applied = nl2sql_service.apply_comments(
            CommentApplyRequest(
                items=items,
                confirmation="ADMIN_EXECUTE",
                reason="manual-integration-comments",
            )
        )
    except Exception as exc:
        return StepResult(name="legacy_comments_apply", ok=False, message=str(exc))
    return StepResult(
        name="legacy_comments_apply",
        ok=bool(items) and applied.executed,
        message=(
            f"execute={execute}; executed={applied.executed}; runtime={applied.runtime}; "
            f"suggestions={len(suggestions.suggestions)}; "
            f"target_suggestions={len(filtered_suggestions)}; "
            f"statements={len(applied.statements)}; "
            f"warnings={len(applied.warnings)}"
        ),
    )


def _legacy_annotations_smoke(*, execute: bool, allowed_tables: list[str]) -> StepResult:
    if not execute:
        return StepResult(
            name="legacy_annotations_apply",
            ok=True,
            message="skipped; explicit execute flag required",
        )
    try:
        allowed = {table.strip().upper() for table in allowed_tables if table.strip()}
        suggestions = nl2sql_service.suggest_annotations()
        filtered_suggestions = [
            item
            for item in suggestions.suggestions
            if not allowed or item.object_name.split(".", 1)[0].strip().upper() in allowed
        ]
        items = [
            AnnotationApplyItem(
                object_name=item.object_name,
                object_type=item.object_type,
                annotation_name=item.annotation_name,
                annotation_value=(
                    f"{item.annotation_value} smoke {_iso_z(datetime.now(UTC))}"
                    if execute
                    else item.annotation_value
                ),
            )
            for item in filtered_suggestions[:1]
        ]
        applied = nl2sql_service.apply_annotations(
            AnnotationApplyRequest(
                items=items,
                confirmation="ADMIN_EXECUTE",
                reason="manual-integration-annotations",
            )
        )
    except Exception as exc:
        return StepResult(name="legacy_annotations_apply", ok=False, message=str(exc))
    return StepResult(
        name="legacy_annotations_apply",
        ok=bool(items) and applied.executed,
        message=(
            f"execute={execute}; executed={applied.executed}; runtime={applied.runtime}; "
            f"suggestions={len(suggestions.suggestions)}; "
            f"target_suggestions={len(filtered_suggestions)}; "
            f"statements={len(applied.statements)}; "
            f"warnings={len(applied.warnings)}"
        ),
    )


def _legacy_feedback_vector_smoke(
    *, question: str, profile_id: str | None, execute_index: bool
) -> list[StepResult]:
    results: list[StepResult] = []
    try:
        seeded = nl2sql_service.seed_demo_learning_data()
        rebuild = (
            nl2sql_service.rebuild_feedback_index(FeedbackIndexRequest(include_bad=False))
            if execute_index
            else nl2sql_service.feedback_index_status()
        )
        results.append(
            StepResult(
                name="legacy_feedback_rebuild",
                ok=rebuild.vector_dimension == 1536 and rebuild.vector_backend == "oracle_26ai",
                message=(
                    f"execute={execute_index}; executed={rebuild.executed}; "
                    f"runtime={rebuild.runtime}; status={rebuild.status}; "
                    f"seeded_history={seeded.seeded_history_count}; "
                    f"indexed={rebuild.indexed_count}; warnings={len(rebuild.warnings)}"
                ),
            )
        )
    except Exception as exc:
        results.append(StepResult(name="legacy_feedback_rebuild", ok=False, message=str(exc)))
    try:
        history = nl2sql_service.similar_history(
            SimilarHistoryRequest(question=question, profile_id=profile_id, limit=3)
        )
        results.append(
            StepResult(
                name="legacy_feedback_search",
                ok=True,
                message=f"matches={len(history.items)}; profile={profile_id or 'default'}",
            )
        )
    except Exception as exc:
        results.append(StepResult(name="legacy_feedback_search", ok=False, message=str(exc)))
    return results


def _wait_for_job(job_id: str, timeout_seconds: float) -> JobData | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        data = nl2sql_service.get_job(job_id)
        if data and data.status in {JobStatus.DONE, JobStatus.ERROR}:
            return data
        time.sleep(0.25)
    return nl2sql_service.get_job(job_id)


def _one_line(value: str, max_len: int = 180) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= max_len else f"{compact[: max_len - 3]}..."


def _iso_z(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _readiness_summary(items: object) -> str:
    if not isinstance(items, list) or not items:
        return "-"
    parts: list[str] = []
    for item in items:
        area = str(getattr(item, "area", "") or "-")
        status = str(getattr(item, "status", "") or "-")
        parts.append(f"{area}:{status}")
    return ",".join(parts)


def _engine_meta_summary(meta: dict[str, object]) -> str:
    keys = [
        "runtime",
        "provider",
        "mode",
        "model",
    ]
    parts = [f"{key}={_one_line(str(meta[key]), 80)}" for key in keys if meta.get(key)]
    return ",".join(parts) if parts else "-"


def _print_results(
    results: Iterable[StepResult],
    *,
    json_report_path: str | None = None,
    release_gate: bool = False,
    engines: Iterable[Nl2SqlEngine] | None = None,
    profile_id: str | None = None,
    allowed_tables: Iterable[str] | None = None,
    started_at: datetime | None = None,
) -> int:
    result_list = list(results)
    failed = False
    for result in result_list:
        print(_status_line(result))
        failed = failed or not result.ok
    exit_code = 1 if failed else 0
    if json_report_path:
        report_result = _write_json_report(
            path=json_report_path,
            results=result_list,
            exit_code=exit_code,
            release_gate=release_gate,
            engines=engines,
            profile_id=profile_id,
            allowed_tables=allowed_tables,
            started_at=started_at,
        )
        print(_status_line(report_result))
        if not report_result.ok:
            exit_code = 1
    return exit_code


def _write_json_report(
    *,
    path: str,
    results: list[StepResult],
    exit_code: int,
    release_gate: bool,
    engines: Iterable[Nl2SqlEngine] | None,
    profile_id: str | None,
    allowed_tables: Iterable[str] | None,
    started_at: datetime | None,
) -> StepResult:
    report_path = Path(path)
    finished_at = datetime.now(UTC)
    resolved_started_at = started_at or finished_at
    elapsed_ms = max(0, int((finished_at - resolved_started_at).total_seconds() * 1000))
    payload = {
        "schema_version": "nl2sql_manual_integration_report_v1",
        "generated_at": _iso_z(finished_at),
        "started_at": _iso_z(resolved_started_at),
        "finished_at": _iso_z(finished_at),
        "elapsed_ms": elapsed_ms,
        "release_gate": release_gate,
        "ok": exit_code == 0,
        "exit_code": exit_code,
        "profile_id": profile_id or "default",
        "engines": [engine.value for engine in engines or []],
        "allowed_tables": list(allowed_tables or []),
        "summary": {
            "total": len(results),
            "passed": sum(1 for result in results if result.ok),
            "failed": sum(1 for result in results if not result.ok),
        },
        "steps": [
            {"name": result.name, "ok": result.ok, "message": result.message} for result in results
        ],
    }
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as exc:
        return StepResult(name="json_report", ok=False, message=str(exc))
    return StepResult(name="json_report", ok=True, message=str(report_path))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manual NL2SQL integration smoke test (OCI Enterprise AI Direct).",
    )
    parser.add_argument("--profile-id", default=None, help="NL2SQL profile id. Default: default")
    parser.add_argument("--question", default=DEFAULT_QUESTION, help="Natural language question.")
    parser.add_argument(
        "--engines",
        type=_parse_engines,
        default=_parse_engines("enterprise_ai_direct"),
        help="Comma-separated concrete engines. Default: enterprise_ai_direct",
    )
    parser.add_argument(
        "--allowed-table",
        action="append",
        default=[],
        help="Allowed table name. Repeat or pass comma-separated values.",
    )
    parser.add_argument("--row-limit", type=int, default=20, help="Row limit for preview/job.")
    parser.add_argument(
        "--refresh-catalog",
        action="store_true",
        help="Refresh schema catalog from Oracle before running checks.",
    )
    parser.add_argument(
        "--import-sample-data",
        action="store_true",
        help=(
            "Explicitly import the optional SQL Assist sample "
            "(DEPARTMENT/EMPLOYEE/PROJECT) before previews."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Run async NL2SQL jobs and execute generated SELECT SQL. Mutates history.",
    )
    parser.add_argument(
        "--check-supporting-features",
        action="store_true",
        help=("Run non-mutating checks for comment suggestions and feedback index status."),
    )
    parser.add_argument(
        "--check-legacy-absorption",
        action="store_true",
        help=(
            "Run no.1-sql-assist legacy absorption smoke checks: classifier, "
            "comments, annotations, and feedback vector search. Destructive DB "
            "steps are skipped unless their --execute-* flag is passed."
        ),
    )
    parser.add_argument(
        "--execute-comments",
        action="store_true",
        help=(
            "Actually execute COMMENT ON statements during --check-legacy-absorption. "
            "Mutates Oracle."
        ),
    )
    parser.add_argument(
        "--execute-annotations",
        action="store_true",
        help=(
            "Actually execute ALTER ... ANNOTATIONS statements during "
            "--check-legacy-absorption. Mutates Oracle."
        ),
    )
    parser.add_argument(
        "--full-smoke",
        action="store_true",
        help=("Run previews, execute jobs, and supporting checks in one command."),
    )
    parser.add_argument(
        "--release-gate",
        action="store_true",
        help=(
            "Production gate alias for --full-smoke plus --require-oracle, "
            "--require-oracle-persistence, and --require-enterprise-ai."
        ),
    )
    parser.add_argument(
        "--json-report",
        default=None,
        help=(
            "Write a machine-readable JSON report for CI or operations dashboards. "
            "Does not change the normal console output."
        ),
    )
    parser.add_argument(
        "--seed-demo-learning",
        action="store_true",
        help="Seed demo history/feedback items before feedback vector index checks.",
    )
    parser.add_argument(
        "--execute-feedback-index",
        action="store_true",
        help=(
            "Execute feedback vector index rebuild. Requires Oracle runtime and "
            "OCI GenAI embedding configuration."
        ),
    )
    parser.add_argument(
        "--require-oracle",
        action="store_true",
        help=("Fail if runtime is not Oracle."),
    )
    parser.add_argument(
        "--require-enterprise-ai",
        action="store_true",
        help=(
            "Fail if Enterprise AI Direct is not configured or if enterprise_ai_direct "
            "falls back to deterministic mode."
        ),
    )
    parser.add_argument(
        "--require-feedback-embedding",
        action="store_true",
        help=(
            "Fail if OCI GenAI feedback embedding is not configured. "
            "Use with --execute-feedback-index for live Oracle AI Vector Search smoke."
        ),
    )
    parser.add_argument(
        "--require-oracle-persistence",
        action="store_true",
        help="Fail if NL2SQL state persistence is not backed by Oracle.",
    )
    parser.add_argument(
        "--require-classifier-oracle-state",
        action="store_true",
        help=(
            "In --check-legacy-absorption, fail classifier smoke unless the "
            "classifier artifact state store is Oracle-backed."
        ),
    )
    parser.add_argument("--timeout", type=float, default=20.0, help="Job polling timeout seconds.")
    parser.add_argument(
        "--diagnostics-timeout",
        type=float,
        default=8.0,
        help="Oracle diagnostics outer timeout seconds. Use 0 to disable.",
    )
    parser.add_argument(
        "--diagnostics-only",
        action="store_true",
        help=(
            "Run only the initial diagnostics and exit. Useful for config checklists "
            "and CI artifacts; does not preview SQL or execute jobs."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    started_at = datetime.now(UTC)
    parser = _build_parser()
    args = parser.parse_args(argv)
    release_gate = args.release_gate
    execute_jobs = args.execute or args.full_smoke or release_gate
    check_supporting = args.check_supporting_features or args.full_smoke or release_gate
    check_legacy_absorption = args.check_legacy_absorption
    require_oracle = args.require_oracle or release_gate
    require_oracle_persistence = args.require_oracle_persistence or release_gate
    require_enterprise_ai = args.require_enterprise_ai or release_gate
    allowed = AllowedObjects(table_names=_parse_tables(args.allowed_table), columns={})
    execute_table_mutation = args.execute_comments or args.execute_annotations
    if execute_table_mutation and not args.check_legacy_absorption:
        parser.error("--execute-comments/--execute-annotations require --check-legacy-absorption")
    if execute_table_mutation and not allowed.table_names:
        parser.error("table mutation smoke requires --allowed-table NL2SQL_<DISPOSABLE_TABLE>")
    unsafe_tables = [table for table in allowed.table_names if not _is_disposable_table_name(table)]
    if execute_table_mutation and unsafe_tables:
        parser.error(
            "table mutation smoke only allows disposable NL2SQL_* tables; "
            f"unsafe={','.join(unsafe_tables)}"
        )
    diagnostics_result = _diagnostics_with_timeout(
        require_oracle,
        args.diagnostics_timeout,
        require_enterprise_ai,
        args.require_feedback_embedding,
        require_oracle_persistence,
    )
    results: list[StepResult] = [diagnostics_result]
    if args.diagnostics_only:
        return _print_results(
            results,
            json_report_path=args.json_report,
            release_gate=release_gate,
            engines=args.engines,
            profile_id=args.profile_id,
            allowed_tables=allowed.table_names,
            started_at=started_at,
        )
    if (
        require_oracle
        or require_enterprise_ai
        or args.require_feedback_embedding
        or require_oracle_persistence
    ) and not diagnostics_result.ok:
        return _print_results(
            results,
            json_report_path=args.json_report,
            release_gate=release_gate,
            engines=args.engines,
            profile_id=args.profile_id,
            allowed_tables=allowed.table_names,
            started_at=started_at,
        )

    results.append(_refresh_catalog(args.refresh_catalog))
    sample_result = _import_sample_data(args.import_sample_data)
    if sample_result:
        results.append(sample_result)
    profile_result, profile_id = _prepare_profile(
        profile_id=args.profile_id,
        allowed_tables=allowed.table_names,
        row_limit=args.row_limit,
    )
    if profile_result:
        results.append(profile_result)

    if args.seed_demo_learning:
        results.append(_seed_demo_learning())

    if check_supporting:
        results.extend(_supporting_features())

    if check_legacy_absorption:
        results.extend(
            _legacy_absorption_checks(
                profile_id=profile_id,
                question=args.question,
                allowed_tables=allowed.table_names,
                execute_comments=args.execute_comments,
                execute_annotations=args.execute_annotations,
                execute_feedback_index=args.execute_feedback_index,
                require_classifier_oracle_state=args.require_classifier_oracle_state,
            )
        )

    if args.execute_feedback_index:
        results.append(_feedback_index_smoke(include_bad=True))

    for engine in args.engines:
        result, _preview_data = _preview(
            engine=engine,
            profile_id=profile_id,
            question=args.question,
            allowed=allowed,
            row_limit=args.row_limit,
            require_oracle=require_oracle,
            require_enterprise_ai=require_enterprise_ai,
        )
        results.append(result)

    if execute_jobs:
        for engine in args.engines:
            results.append(
                _job(
                    engine=engine,
                    profile_id=profile_id,
                    question=args.question,
                    allowed=allowed,
                    row_limit=args.row_limit,
                    timeout_seconds=args.timeout,
                    require_oracle=require_oracle,
                    require_enterprise_ai=require_enterprise_ai,
                )
            )

    return _print_results(
        results,
        json_report_path=args.json_report,
        release_gate=release_gate,
        engines=args.engines,
        profile_id=profile_id,
        allowed_tables=allowed.table_names,
        started_at=started_at,
    )


if __name__ == "__main__":
    sys.exit(main())
