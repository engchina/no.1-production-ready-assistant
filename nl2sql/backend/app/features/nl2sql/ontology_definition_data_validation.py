"""明示的な実データ検証 job。Oracle の読み取りと SHACL 対象 coverage を記録する。"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

from pyshacl import validate as shacl_validate
from rdflib import RDF, XSD, Graph, Literal, Namespace, URIRef

from app.security.domain import Principal
from app.security.permissions import SQL_EXECUTE_PERMISSION
from app.security.request_actor import actor_scope
from app.settings import get_settings

from .object_identity import object_part_name
from .ontology_definition_artifacts import render_definition_artifacts
from .ontology_definition_service import definition_fingerprint
from .ontology_definition_validation import (
    checked_expression,
    mapping_object_key,
    schema_objects,
    validate_definitions,
)
from .ontology_definition_workspace import (
    ProfileOntologyWorkspaceService,
    authorize_definition_operation,
)
from .ontology_definitions import (
    DefinitionDataValidationRequest,
    ProfileOntologyBundle,
    PropertyDefinition,
)
from .ontology_service import (
    OntologyGateBlockedError,
    OntologyNotFoundError,
    OntologyVersionConflictError,
)
from .ontology_sql_validation import identifier_token
from .ontology_store import OntologyVersionConflict, stable_ontology_id

logger = logging.getLogger(__name__)
_inprocess_jobs: set[tuple[int, str]] = set()
_dispatch_lock = threading.Lock()
_RECEIPT_TYPE = "ontology_data_validation_receipt"
_RECOVERABLE_CODES = {"ONTOLOGY_VALIDATION_TIMEOUT", "ONTOLOGY_VALIDATION_INTERRUPTED"}


class ValidationExecutionLost(RuntimeError):
    """中断された実データ検証の遅着結果を破棄する。"""


def _receipt_id(job_id: str) -> str:
    return stable_ontology_id("data_validation_receipt", job_id)


def _expired(job: dict[str, Any]) -> bool:
    try:
        if job.get("deadline_at"):
            deadline = datetime.fromisoformat(str(job["deadline_at"]))
        else:
            deadline = datetime.fromisoformat(str(job["created_at"])) + timedelta(
                seconds=get_settings().nl2sql_ontology_validation_timeout_seconds
            )
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        return datetime.now(UTC) >= deadline
    except (KeyError, ValueError, TypeError):
        return True


def _recover_job(runtime: Any, job_id: str, *, interrupted: bool = False) -> dict[str, Any] | None:
    for _ in range(3):
        current = runtime.store.get_job(job_id)
        if current is None or current.get("job_type") != "definition_validation":
            return None
        recoverable = current.get("error_code") in _RECOVERABLE_CODES
        if current["status"] in {"succeeded", "failed", "cancelled"} and not recoverable:
            return dict(current)
        if not interrupted and not recoverable and not _expired(current):
            return dict(current)
        receipt = runtime.store.get_artifact(_receipt_id(job_id))
        evidence = json.loads(receipt["content"]) if receipt else {}
        committed = (
            receipt
            and receipt.get("profile_id") == current["profile_id"]
            and receipt.get("artifact_type") == _RECEIPT_TYPE
            and evidence.get("job_id") == job_id
            and evidence.get("request_hash") == current.get("request_hash")
            and receipt.get("content_hash") == definition_fingerprint(evidence)
        )
        if recoverable and not committed:
            return dict(current)
        updates = {
            "status": "succeeded" if committed else "failed",
            "finished_at": datetime.now(UTC).isoformat(),
            "error_code": (
                ""
                if committed
                else (
                    "ONTOLOGY_VALIDATION_INTERRUPTED"
                    if interrupted
                    else "ONTOLOGY_VALIDATION_TIMEOUT"
                )
            ),
            "error_message_ja": (
                ""
                if committed
                else (
                    "実データ検証を中断しました。保存済み定義は保持されています。状態を確認して検証を再実行してください。"
                )
            ),
            **({"report": evidence["report"], "recovered_from_receipt": True} if committed else {}),
        }
        try:
            saved = runtime.store.save_job({**current, **updates}, expected_etag=current["etag"])
        except OntologyVersionConflict:
            continue
        logger.warning(
            "実データ検証 job の中断状態を回復しました。",
            extra={
                "job_id": job_id,
                "profile_id": current["profile_id"],
                "error_code": updates["error_code"],
                "status": updates["status"],
            },
        )
        return dict(saved)
    current = runtime.store.get_job(job_id)
    return dict(current) if current else None


def _assert_execution(runtime: Any, owned: dict[str, Any]) -> None:
    current = _recover_job(runtime, owned["job_id"])
    if (
        not current
        or current["status"] != "running"
        or current.get("execution_id") != owned["execution_id"]
        or current["etag"] != owned["etag"]
    ):
        raise ValidationExecutionLost(owned["job_id"])


def shutdown_validation_jobs(runtime: Any) -> None:
    with _dispatch_lock:
        jobs = [job_id for owner, job_id in _inprocess_jobs if owner == id(runtime)]
    for job_id in jobs:
        try:
            _recover_job(runtime, job_id, interrupted=True)
        except Exception:
            logger.exception(
                "実データ検証 job の停止状態を保存できませんでした。", extra={"job_id": job_id}
            )


def _run_inprocess(runtime: Any, job_id: str) -> None:
    try:
        run_validation_job(runtime, job_id)
    finally:
        with _dispatch_lock:
            _inprocess_jobs.discard((id(runtime), job_id))


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def property_literal(value: Any, data_type: str) -> Literal:
    """Oracle/JSON の正常な値だけを契約の RDF datatype に変換する。"""
    try:
        if data_type == "number" and type(value) in (int, float, Decimal):
            number = Decimal(str(value))
            if number.is_finite():
                return Literal(number, datatype=XSD.decimal)
        if data_type == "date":
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
            if isinstance(parsed, datetime) and parsed.time().isoformat() == "00:00:00":
                parsed = parsed.date()
            if type(parsed) is date:
                return Literal(parsed, datatype=XSD.date)
        if data_type == "datetime":
            parsed = (
                datetime.fromisoformat(value) if isinstance(value, str) and "T" in value else value
            )
            if isinstance(parsed, datetime):
                return Literal(parsed, datatype=XSD.dateTime)
    except (ValueError, InvalidOperation):
        pass
    # 不正な文字列・真偽値等を期待型へ強制すると SHACL が見逃すため元の型を残す。
    return Literal(value)


def check_data(
    runtime: Any,
    bundle: ProfileOntologyBundle,
    request: DefinitionDataValidationRequest,
    *,
    execution_guard: Callable[[], None] | None = None,
) -> dict[str, Any]:
    schema = json.loads(str(runtime.prepare_build_schema_context(bundle.profile_id).schema_context))
    errors = [f for f in validate_definitions(bundle, schema) if f.severity == "error"]
    if errors:
        raise OntologyGateBlockedError(
            "STATIC_VALIDATION_REQUIRED", "実データ検証の前に定義の阻害事項を修正してください。"
        )
    adapter = getattr(runtime.legacy_service, "_oracle_adapter", None)
    if adapter is None:
        raise OntologyGateBlockedError("ORACLE_UNAVAILABLE", "Oracle の実データ接続がありません。")
    graph = Graph()
    ns = Namespace(f"urn:nl2sql:profile:{bundle.profile_id}:")
    by_name = {d.api_name: d for d in bundle.definitions}
    targets, covered, samples, skipped = [], [], 0, []
    for obj in bundle.definitions:
        if obj.kind != "object_type":
            continue
        targets.append(obj.id)
        props = [by_name.get(name) for name in obj.properties]
        if (
            len(obj.mappings) != 1
            or not props
            or any(not isinstance(p, PropertyDefinition) or len(p.mappings) != 1 for p in props)
            or any(
                m.expression_sql
                for p in props
                if isinstance(p, PropertyDefinition)
                for m in p.mappings
            )
            or any(m.expression_sql for m in obj.mappings)
        ):
            skipped.append(
                {
                    "target": obj.id,
                    "reason_ja": "複数ソースまたは未確定 mapping の検証 SQL が必要です。",
                }
            )
            continue
        mapping = obj.mappings[0]
        mapped = [p for p in props if isinstance(p, PropertyDefinition)]
        if any(mapping_object_key(p.mappings[0]) != mapping_object_key(mapping) for p in mapped):
            skipped.append(
                {"target": obj.id, "reason_ja": "複数ソースを結合する検証 SQL が必要です。"}
            )
            continue
        # mapping は SQL と同じ表記（`"Amount"` / `amount`）。`"..."` で囲む前にカタログ上の名前へ
        # 戻す。そのまま囲むと `"amount"`（小文字の別列）や `"""Amount"""` になる（#573）。
        columns = ", ".join(
            f"{quote_identifier(object_part_name(p.mappings[0].column_name))} "
            f"AS {quote_identifier(p.id)}"
            for p in mapped
        )
        # Profile 内で解決した識別子は二重引用符で escape。行数は Pydantic の範囲付き整数。
        sql = (
            f"SELECT {columns} FROM "  # nosec B608
            f"{quote_identifier(object_part_name(mapping.owner))}."
            f"{quote_identifier(object_part_name(mapping.object_name))} "
            f"FETCH FIRST {request.sample_limit} ROWS ONLY"
        )
        if execution_guard:
            execution_guard()
        result = adapter.execute_select(sql, request.sample_limit)
        covered.append(obj.id)
        for index, row in enumerate(result.rows):
            subject = URIRef(ns[f"sample:{obj.id}:{index}"])
            graph.add((subject, RDF.type, URIRef(ns[obj.id])))
            samples += 1
            for prop in mapped:
                value = row.get(prop.id)
                if value is not None:
                    graph.add(
                        (subject, URIRef(ns[prop.id]), property_literal(value, prop.data_type))
                    )
    artifacts = render_definition_artifacts(bundle)
    if execution_guard:
        execution_guard()
    conforms, _report_graph, report_text = shacl_validate(
        graph,
        shacl_graph=Graph().parse(data=artifacts["shacl_turtle"], format="turtle"),
        inference="none",
    )
    cases = []
    allowed = schema_objects(schema)
    for case in request.acceptance_cases:
        independent = not any(case.question_ja in source.text for source in bundle.sources)
        has_assertions = bool(case.expected_concepts or case.expected_path or case.sql)
        resolved = all(name in by_name for name in case.expected_concepts)
        path_ok = all(name in by_name for name in case.expected_path) and all(
            any(
                link.kind == "link_type" and {link.source, link.target} == {left, right}
                for link in bundle.definitions
            )
            for left, right in zip(case.expected_path, case.expected_path[1:], strict=False)
        )
        sql_ok: bool | None = None
        if case.sql:
            from sqlglot import exp

            tree = checked_expression(case.sql, query=True)
            for table in tree.find_all(exp.Table):
                table_key = (
                    f"{identifier_token(table.args.get('db'))}.{identifier_token(table.this)}"
                )
                if table_key not in allowed:
                    raise OntologyGateBlockedError(
                        "ACCEPTANCE_SQL_SCOPE",
                        "受入 SQL は業務プロファイルの OWNER.TABLE を明記してください。",
                    )
            if execution_guard:
                execution_guard()
            result = adapter.execute_select(case.sql, request.sample_limit)
            sql_ok = result.rows == case.expected_rows
        cases.append(
            {
                "question_ja": case.question_ja,
                "independent": independent,
                "concepts_pass": resolved,
                "path_pass": path_ok,
                "sql_pass": sql_ok,
                "has_assertions": has_assertions,
                "passed": has_assertions
                and independent
                and resolved
                and path_ok
                and sql_ok is not False,
            }
        )
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "kind": "sampled_data",
        "sample_limit": request.sample_limit,
        "target_nodes": targets,
        "covered_targets": covered,
        "skipped_targets": skipped,
        "instance_count": samples,
        "instance_coverage": len(covered) / len(targets) if targets else 0,
        "coverage_basis": "object_types_sampled",
        "entire_database_validated": False,
        "instance_status": "sampled" if samples else "no_instances",
        "shacl_conforms": bool(conforms) if samples else None,
        "shacl_report": str(report_text),
        "acceptance_cases": cases,
        "acceptance_status": "completed" if cases else "not_run",
        "errors": int(not conforms and samples > 0) + sum(not case["passed"] for case in cases),
    }


def start_validation_job(
    runtime: Any,
    profile_id: str,
    bundle_id: str,
    etag: str,
    request: DefinitionDataValidationRequest,
    key: str,
    actor: Principal | None,
) -> dict[str, Any]:
    who = authorize_definition_operation(profile_id, actor, SQL_EXECUTE_PERMISSION)
    svc = ProfileOntologyWorkspaceService(runtime)
    if not key:
        raise OntologyGateBlockedError("IDEMPOTENCY_REQUIRED", "Idempotency-Key が必要です。")
    job_id = stable_ontology_id("ontology_validation", profile_id, who, key)
    payload = {
        "bundle_id": bundle_id,
        "etag": etag,
        "actor": who,
        "request": request.model_dump(mode="json"),
    }
    fingerprint = definition_fingerprint(payload)
    existing = runtime.store.get_job(job_id)
    if existing is not None:
        if existing.get("request_hash") != fingerprint:
            raise OntologyVersionConflictError(
                "IDEMPOTENCY_KEY_REUSED", "同じキーを別の検証に使用できません。"
            )
        return _recover_job(runtime, job_id) or dict(existing)
    svc._draft(profile_id, bundle_id, etag)
    job = dict(
        runtime.store.save_job(
            {
                "job_id": job_id,
                "job_type": "definition_validation",
                "profile_id": profile_id,
                "status": "queued",
                "payload": payload,
                "request_hash": fingerprint,
                "deadline_at": (
                    datetime.now(UTC)
                    + timedelta(seconds=get_settings().nl2sql_ontology_validation_timeout_seconds)
                ).isoformat(),
            }
        )
    )
    if get_settings().nl2sql_ontology_worker_mode == "inprocess":
        with _dispatch_lock:
            _inprocess_jobs.add((id(runtime), job_id))
        try:
            threading.Thread(target=_run_inprocess, args=(runtime, job_id), daemon=True).start()
        except Exception:
            with _dispatch_lock:
                _inprocess_jobs.discard((id(runtime), job_id))
            _recover_job(runtime, job_id, interrupted=True)
            raise
    return job


def run_validation_job(runtime: Any, job_id: str) -> None:
    job = _recover_job(runtime, job_id)
    if (
        job is None
        or job.get("status") not in {"queued", "claimed"}
        or job.get("execution_id")
        or (job.get("status") == "claimed" and job.get("claimed_from_status") != "queued")
    ):
        return
    try:
        job = dict(
            runtime.store.save_job(
                {
                    **job,
                    "status": "running",
                    "claimed_at": time.time(),
                    "execution_id": uuid4().hex,
                    "started_at": datetime.now(UTC).isoformat(),
                },
                expected_etag=job["etag"],
            )
        )
    except OntologyVersionConflict:
        return
    try:
        payload = job["payload"]
        actor = None
        if get_settings().app_auth_enabled and not get_settings().local_debug_enabled:
            from app.security.service import get_security_service

            actor = get_security_service().principal_for_worker(payload["actor"])
        authorize_definition_operation(job["profile_id"], actor, SQL_EXECUTE_PERMISSION)
        svc = ProfileOntologyWorkspaceService(runtime)
        bundle = svc._draft(job["profile_id"], payload["bundle_id"], payload["etag"])
        with actor_scope(payload["actor"], is_system_admin=bool(actor and actor.is_system_admin)):
            report = check_data(
                runtime,
                bundle,
                DefinitionDataValidationRequest.model_validate(payload["request"]),
                execution_guard=lambda: _assert_execution(runtime, job),
            )
        _assert_execution(runtime, job)
        # Validate scope again after the actual queries; concurrent edits invalidate this report.
        if bundle.profile_fingerprint != definition_fingerprint(
            runtime._strict_profile(bundle.profile_id).model_dump(mode="json")
        ):
            raise OntologyVersionConflictError(
                "VALIDATION_SCOPE_CHANGED", "検証中に業務プロファイルが変更されました。"
            )
        if bundle.schema_context_fingerprint != definition_fingerprint(
            str(runtime.prepare_build_schema_context(bundle.profile_id).schema_context)
        ):
            raise OntologyVersionConflictError(
                "VALIDATION_SCOPE_CHANGED", "検証中に Schema が変更されました。"
            )
        if not bundle.validation_report:
            raise OntologyGateBlockedError(
                "STATIC_VALIDATION_REQUIRED", "定義の検証を先に実行してください。"
            )
        bundle.validation_report["data_validation"] = report
        bundle.validation_report["errors"] = (
            sum(f.severity == "error" for f in bundle.findings) + report["errors"]
        )
        receipt = svc.artifact(
            bundle.profile_id,
            _receipt_id(job_id),
            _RECEIPT_TYPE,
            {"job_id": job_id, "request_hash": job["request_hash"], "report": report},
        )
        # bundle と完了証跡を同じ transaction で保存し、job 状態だけが失われても照合できる。
        svc._commit(
            bundle,
            payload["etag"],
            extra=[(receipt, None)],
            commit_guard=lambda: _assert_execution(runtime, job),
        )
        _assert_execution(runtime, job)
        runtime.store.save_job(
            {
                **job,
                "status": "succeeded",
                "report": report,
                "finished_at": datetime.now(UTC).isoformat(),
            },
            expected_etag=job["etag"],
        )
    except ValidationExecutionLost:
        logger.info("失効した実データ検証の結果を破棄しました。", extra={"job_id": job_id})
    except Exception as exc:
        logger.exception("実データ検証に失敗しました。", extra={"job_id": job_id})
        try:
            _assert_execution(runtime, job)
            # report commit 後の job 保存障害は再照合し、SQL を再送しない。
            if runtime.store.get_artifact(_receipt_id(job_id)):
                _recover_job(runtime, job_id, interrupted=True)
                return
            runtime.store.save_job(
                {
                    **job,
                    "status": "failed",
                    "error_message_ja": getattr(exc, "message_ja", "実データ検証に失敗しました。"),
                    "error_code": getattr(exc, "code", type(exc).__name__),
                    "finished_at": datetime.now(UTC).isoformat(),
                },
                expected_etag=job["etag"],
            )
        except (ValidationExecutionLost, OntologyVersionConflict):
            pass


def read_validation_job(runtime: Any, profile_id: str, job_id: str) -> dict[str, Any]:
    runtime.ensure_profile(profile_id)
    job = runtime.store.get_job(job_id)
    if (
        job is None
        or job.get("profile_id") != profile_id
        or job.get("job_type") != "definition_validation"
    ):
        raise OntologyNotFoundError("VALIDATION_JOB_NOT_FOUND", "検証 job が見つかりません。")
    return _recover_job(runtime, job_id) or dict(job)
