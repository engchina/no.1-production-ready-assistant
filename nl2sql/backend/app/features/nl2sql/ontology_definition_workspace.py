"""Profile 業務定義の編集・検証・レビュー・原子的な公開。"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from pr_backend_core.observability.model_purpose import call_with_model_purpose

from app.security.domain import Principal
from app.security.permissions import PROFILE_MANAGE_PERMISSION
from app.settings import get_settings

from .ontology_definition_service import ProfileOntologyDefinitionService, definition_fingerprint
from .ontology_definition_validation import validate_definitions
from .ontology_definitions import (
    BusinessDefinition,
    DefinitionChangeExtraction,
    ProfileOntologyBundle,
)
from .ontology_service import (
    OntologyGateBlockedError,
    OntologyNotFoundError,
    OntologyVersionConflictError,
)
from .ontology_store import canonical_json, stable_ontology_id


def authorize_definition_operation(
    profile_id: str, actor: Principal | None, permission: str = "menu.ontology_build"
) -> str:
    settings = get_settings()
    if settings.app_auth_enabled and not settings.local_debug_enabled:
        if actor is None:
            raise HTTPException(403, "認証が必要です。")
        from app.security.service import get_security_service

        actor = get_security_service().principal_for_worker(actor.user_uuid)
    if actor is not None and (
        not actor.can_use_profile(profile_id)
        or not actor.has_any_permission(
            {permission, PROFILE_MANAGE_PERMISSION}
            if permission == "menu.ontology_build"
            else {permission}
        )
    ):
        raise HTTPException(403, "この業務プロファイルの操作権限がありません。")
    return actor.user_uuid if actor else "auth-disabled"


class ProfileOntologyWorkspaceService(ProfileOntologyDefinitionService):
    def document(self, profile_id: str, artifact_id: str, kind: str) -> dict[str, Any]:
        return self.checked_document(profile_id, self.store.get_artifact(artifact_id), kind)

    def checked_document(
        self, profile_id: str, record: dict[str, Any] | None, kind: str
    ) -> dict[str, Any]:
        """読んだ成果物が、この業務プロファイルの指定の種類のものか確かめる。"""

        if (
            record is None
            or record.get("session_id") != self._session(profile_id)
            or record.get("profile_id") != profile_id
            or record.get("artifact_type") != kind
        ):
            raise OntologyNotFoundError("ONTOLOGY_ARTIFACT_NOT_FOUND", "成果物が見つかりません。")
        return dict(record)

    def artifact(self, profile_id: str, artifact_id: str, kind: str, value: Any) -> dict[str, Any]:
        return {
            "artifact_id": artifact_id,
            "session_id": self._session(profile_id),
            "profile_id": profile_id,
            "artifact_type": kind,
            "content": canonical_json(value),
            "content_hash": definition_fingerprint(value),
        }

    def head(self, profile_id: str) -> dict[str, Any]:
        record = self.store.get_artifact(stable_ontology_id("ontology_head", profile_id))
        if record is None:
            self._session(profile_id)
            return {"release_id": "", "etag": ""}
        record = self.document(profile_id, str(record["artifact_id"]), "ontology_published_head")
        return {**json.loads(record["content"]), "etag": record["etag"]}

    def release(self, profile_id: str, release_id: str = "") -> dict[str, Any] | None:
        selected = release_id or str(self.head(profile_id)["release_id"])
        if not selected:
            return None
        record = self.document(profile_id, selected, "ontology_release_v2")
        value: dict[str, Any] = json.loads(record["content"])
        if definition_fingerprint(value) != record["content_hash"]:
            raise OntologyGateBlockedError(
                "ARTIFACT_HASH_MISMATCH", "成果物の整合性を確認できません。"
            )
        return value

    def _draft(self, profile_id: str, bundle_id: str, etag: str) -> ProfileOntologyBundle:
        bundle = self.get(profile_id, bundle_id)
        if not etag or bundle.etag != etag.strip('"'):
            raise OntologyVersionConflictError(
                "ONTOLOGY_ETAG_MISMATCH", "定義が更新されました。最新情報を取得してください。"
            )
        if bundle.status == "published":
            raise OntologyVersionConflictError(
                "ONTOLOGY_IMMUTABLE",
                "公開済みの版は変更できません。再構築で新しい草稿を作成してください。",
            )
        return bundle

    def _commit(
        self,
        bundle: ProfileOntologyBundle,
        expected_etag: str,
        *,
        extra: list[tuple[dict[str, Any], str | None]] | None = None,
        commit_guard: Callable[[], None] | None = None,
    ) -> ProfileOntologyBundle:
        current = self.document(bundle.profile_id, bundle.id, "profile_ontology_bundle_v2")
        if ProfileOntologyBundle.model_validate_json(
            current["content"]
        ).etag != expected_etag.strip('"'):
            raise OntologyVersionConflictError("ONTOLOGY_ETAG_MISMATCH", "定義が更新されました。")
        from .ontology_definitions import DEFINITION_KINDS, ConceptCoverage

        previous_coverage = {c.kind: c for c in bundle.coverage}
        bundle.coverage = [
            (
                ConceptCoverage(
                    kind=kind,
                    status="generated",
                    count=sum(d.kind == kind for d in bundle.definitions),
                )
                if any(d.kind == kind for d in bundle.definitions)
                else ConceptCoverage(
                    kind=kind,
                    status="insufficient_evidence",
                    reason_ja=(
                        previous_coverage[kind].reason_ja
                        if kind in previous_coverage
                        else "定義がありません。"
                    ),
                )
            )
            for kind in DEFINITION_KINDS
        ]
        bundle.etag = definition_fingerprint(bundle.model_dump(mode="json", exclude={"etag"}))
        if commit_guard:
            commit_guard()
        self.store.save_documents_atomic(
            "artifacts",
            [
                (
                    self.artifact(
                        bundle.profile_id,
                        bundle.id,
                        "profile_ontology_bundle_v2",
                        bundle.model_dump(mode="json"),
                    ),
                    str(current["etag"]),
                ),
                *(extra or []),
            ],
        )
        return bundle

    def edit(
        self,
        profile_id: str,
        bundle_id: str,
        etag: str,
        definitions: list[BusinessDefinition],
        actor: Principal | None,
    ) -> ProfileOntologyBundle:
        who = authorize_definition_operation(profile_id, actor)
        bundle = self._draft(profile_id, bundle_id, etag)
        old = {item.id: item for item in bundle.definitions}
        updated: list[BusinessDefinition] = []
        for definition in definitions:
            item = definition.model_copy(deep=True)
            if item.id and item.id not in old:
                raise OntologyGateBlockedError(
                    "FOREIGN_DEFINITION_ID", "別の版または業務プロファイルの ID は使用できません。"
                )
            item.id = item.id or stable_ontology_id(
                "profile_concept", profile_id, item.kind, item.api_name
            )
            if any(existing.id == item.id for existing in updated):
                raise OntologyGateBlockedError(
                    "DUPLICATE_DEFINITION_ID", "同じ概念 ID を複数の定義に使用できません。"
                )
            if item.id in old and item.model_dump() == old[item.id].model_dump():
                updated.append(old[item.id])
                continue
            item.origin, item.review_status = "manual", "unreviewed"
            updated.append(item)
        bundle.definitions = updated
        bundle.validation_report = {}
        bundle.findings = []
        bundle.review_records = [
            r
            for r in bundle.review_records
            if any(
                d.id == r.get("definition_id") and d.review_status == "reviewed" for d in updated
            )
        ]
        audit = self.artifact(
            profile_id,
            f"ontology_edit_{uuid4().hex}",
            "ontology_definition_audit",
            {
                "actor": who,
                "bundle_id": bundle_id,
                "before": etag,
                "at": datetime.now(UTC).isoformat(),
                "definition_ids": [d.id for d in updated],
            },
        )
        return self._commit(bundle, etag, extra=[(audit, None)])

    def notes(
        self, profile_id: str, bundle_id: str, etag: str, notes: str, actor: Principal | None
    ) -> ProfileOntologyBundle:
        authorize_definition_operation(profile_id, actor)
        bundle = self._draft(profile_id, bundle_id, etag)
        bundle.notes_ja = notes
        return self._commit(bundle, etag)

    def validate(
        self, profile_id: str, bundle_id: str, etag: str, actor: Principal | None
    ) -> ProfileOntologyBundle:
        authorize_definition_operation(profile_id, actor)
        bundle = self._draft(profile_id, bundle_id, etag)
        prepared = self.runtime.prepare_build_schema_context(profile_id)
        if prepared.errors:
            raise OntologyGateBlockedError(
                "SCHEMA_UNAVAILABLE", "業務プロファイルの Schema を取得できません。"
            )
        context = str(prepared.schema_context)
        previous_report = bundle.validation_report
        previous_scope = (bundle.profile_fingerprint, bundle.schema_context_fingerprint)
        bundle.findings = validate_definitions(bundle, json.loads(context))
        bundle.profile_fingerprint = definition_fingerprint(
            self.runtime._strict_profile(profile_id).model_dump(mode="json")
        )
        bundle.schema_context_fingerprint = definition_fingerprint(context)
        bundle.requires_revalidation = False
        bundle.validation_report = {
            "checked_at": datetime.now(UTC).isoformat(),
            "kind": "static",
            "definition_count": len(bundle.definitions),
            "definition_hash": definition_fingerprint(
                [d.model_dump(mode="json", exclude={"review_status"}) for d in bundle.definitions]
            ),
            "target_nodes": [d.id for d in bundle.definitions if d.kind == "object_type"],
            "instance_count": 0,
            "instance_coverage": 0,
            "instance_status": "not_run",
            "message_ja": "定義のみの静的検証です。SHACL の対象データインスタンスは未検証です。",
            "errors": sum(f.severity == "error" for f in bundle.findings),
        }
        if (
            previous_report.get("definition_hash") == bundle.validation_report["definition_hash"]
            and previous_scope == (bundle.profile_fingerprint, bundle.schema_context_fingerprint)
            and previous_report.get("data_validation")
        ):
            bundle.validation_report["data_validation"] = previous_report["data_validation"]
            bundle.validation_report["errors"] += int(
                previous_report["data_validation"].get("errors", 0)
            )
        return self._commit(bundle, etag)

    def review(
        self, profile_id: str, bundle_id: str, etag: str, ids: list[str], actor: Principal | None
    ) -> ProfileOntologyBundle:
        who = authorize_definition_operation(profile_id, actor)
        bundle = self._draft(profile_id, bundle_id, etag)
        if not set(ids) <= {d.id for d in bundle.definitions}:
            raise OntologyGateBlockedError(
                "REVIEW_TARGET_INVALID", "レビュー対象がこの版にありません。"
            )
        for definition in bundle.definitions:
            if definition.id in ids:
                definition.review_status = "reviewed"
                bundle.review_records.append(
                    {
                        "definition_id": definition.id,
                        "actor": who,
                        "at": datetime.now(UTC).isoformat(),
                        "hash": definition_fingerprint(definition.model_dump(mode="json")),
                    }
                )
        # 人手レビューは静的/実データ検証と独立して保持する。
        return self._commit(bundle, etag)

    def resolve(
        self,
        profile_id: str,
        bundle_id: str,
        etag: str,
        index: int,
        choice: str,
        actor: Principal | None,
    ) -> ProfileOntologyBundle:
        authorize_definition_operation(profile_id, actor)
        bundle = self._draft(profile_id, bundle_id, etag)
        if index < 0 or index >= len(bundle.conflicts) or choice not in {"current", "proposed"}:
            raise OntologyGateBlockedError(
                "CONFLICT_TARGET_INVALID", "競合候補を選択してください。"
            )
        conflict = bundle.conflicts.pop(index)
        if choice == "proposed":
            candidate = conflict.proposed.model_copy(deep=True)
            candidate.review_status = "unreviewed"
            bundle.definitions = [
                candidate if d.id == conflict.definition_id else d for d in bundle.definitions
            ]
            bundle.validation_report = {}
        return self._commit(bundle, etag)

    def analyze(
        self,
        profile_id: str,
        bundle_id: str,
        etag: str,
        instruction: str,
        key: str,
        actor: Principal | None,
    ) -> dict[str, Any]:
        who = authorize_definition_operation(profile_id, actor)
        bundle = self._draft(profile_id, bundle_id, etag)
        if not key:
            raise OntologyGateBlockedError("IDEMPOTENCY_REQUIRED", "Idempotency-Key が必要です。")
        change_id = stable_ontology_id("ontology_changes", profile_id, who, key)
        request_hash = definition_fingerprint([bundle_id, etag, instruction])
        cached = self.store.get_artifact(change_id)
        if cached:
            result: dict[str, Any] = json.loads(
                self.document(profile_id, change_id, "ontology_changes")["content"]
            )
            if result["request_hash"] != request_hash:
                raise OntologyVersionConflictError(
                    "IDEMPOTENCY_KEY_REUSED", "同じキーを別の解析に使用できません。"
                )
            return result
        from .structured_outputs import response_format

        client = getattr(self.runtime.legacy_service, "_enterprise_ai_client", None)
        if client is None or not client.is_configured():
            raise OntologyGateBlockedError(
                "ENTERPRISE_AI_UNAVAILABLE", "Enterprise AI が設定されていません。"
            )
        output = call_with_model_purpose(
            "ontology.review",
            client.generate,
            prompt=instruction,
            context=bundle.model_dump_json(),
            system_prompt=(
                "日本語の変更指示を型付き definitions の差分候補へ変換する。"
                "変更する定義のみ返す。ID と api_name は維持し、権限やコードは創作しない。"
                "削除指示は deleted_definition_ids に既存 ID を明示する。"
                "削除と更新を同じ ID に指定しない。依存する定義も指示に応じて更新する。"
                "これは解析でありレビュー・公開ではない。"
            ),
            response_format=response_format(DefinitionChangeExtraction),
        )
        extraction = DefinitionChangeExtraction.model_validate_json(str(output))
        deleted = set(extraction.deleted_definition_ids)
        by_id = {d.id: d for d in bundle.definitions}
        if not deleted <= by_id.keys() or any(
            d.id in deleted or d.api_name in {by_id[i].api_name for i in deleted}
            for d in extraction.definitions
        ):
            raise OntologyGateBlockedError(
                "CHANGE_DELETE_INVALID", "削除対象が存在しないか、同じ定義の更新と競合しています。"
            )
        if len({d.api_name for d in extraction.definitions}) != len(extraction.definitions):
            raise OntologyGateBlockedError("DUPLICATE_NAME", "変更候補の概念名が重複しています。")
        candidates = {d.api_name: d for d in extraction.definitions}
        merged = [
            candidates.pop(d.api_name, d).model_copy(update={"id": d.id})
            for d in bundle.definitions
            if d.id not in deleted
        ]
        merged.extend(d.model_copy(update={"id": ""}) for d in candidates.values())
        result = {
            "id": change_id,
            "bundle_id": bundle_id,
            "base_etag": etag,
            "request_hash": request_hash,
            "instruction_ja": instruction,
            "deleted_definition_ids": sorted(deleted),
            "before": [
                d.model_dump(mode="json", exclude={"review_status"}) for d in bundle.definitions
            ],
            "after": [d.model_dump(mode="json") for d in merged],
        }
        self.store.save_artifact(self.artifact(profile_id, change_id, "ontology_changes", result))
        return result

    def publish(
        self,
        profile_id: str,
        bundle_id: str,
        etag: str,
        expected_head: str,
        key: str,
        actor: Principal | None,
    ) -> dict[str, Any]:
        who = authorize_definition_operation(profile_id, actor)
        if not key:
            raise OntologyGateBlockedError("IDEMPOTENCY_REQUIRED", "Idempotency-Key が必要です。")
        operation_id = stable_ontology_id("ontology_publish_operation", profile_id, who, key)
        request_hash = definition_fingerprint([bundle_id, etag, expected_head])
        operation = self.store.get_artifact(operation_id)
        if operation is not None:
            saved = json.loads(
                self.document(profile_id, operation_id, "ontology_publish_operation")["content"]
            )
            if saved["request_hash"] != request_hash:
                raise OntologyVersionConflictError(
                    "IDEMPOTENCY_KEY_REUSED", "同じキーを別の公開に使用できません。"
                )
            return self.release(profile_id, saved["release_id"]) or {}
        bundle = self._draft(profile_id, bundle_id, etag)
        current_profile = definition_fingerprint(
            self.runtime._strict_profile(profile_id).model_dump(mode="json")
        )
        current_schema = definition_fingerprint(
            str(self.runtime.prepare_build_schema_context(profile_id).schema_context)
        )
        if (
            bundle.requires_revalidation
            or bundle.profile_fingerprint != current_profile
            or bundle.schema_context_fingerprint != current_schema
        ):
            raise OntologyGateBlockedError(
                "SCOPE_REVALIDATION_REQUIRED",
                "業務プロファイルまたは Schema が変更されました。再検証してください。",
            )
        report = bundle.validation_report
        if (
            not report
            or report.get("definition_hash")
            != definition_fingerprint(
                [d.model_dump(mode="json", exclude={"review_status"}) for d in bundle.definitions]
            )
            or report.get("errors")
        ):
            raise OntologyGateBlockedError(
                "VALIDATION_REQUIRED", "現在の定義の検証と阻害事項の解決が必要です。"
            )
        if (
            not bundle.definitions
            or bundle.conflicts
            or any(d.review_status != "reviewed" for d in bundle.definitions)
        ):
            raise OntologyGateBlockedError(
                "REVIEW_REQUIRED", "すべての定義と競合のレビューを完了してください。"
            )
        if any(
            not any(
                record.get("definition_id") == definition.id
                and record.get("hash") == definition_fingerprint(definition.model_dump(mode="json"))
                for record in bundle.review_records
            )
            for definition in bundle.definitions
        ):
            raise OntologyGateBlockedError(
                "REVIEW_STALE", "レビュー後に定義が変更されました。再レビューしてください。"
            )
        from .ontology_definition_artifacts import render_definition_artifacts

        head = self.head(profile_id)
        if head["release_id"] != expected_head:
            raise OntologyVersionConflictError(
                "PUBLISH_HEAD_CHANGED", "公開版が変更されました。変更セットを再確認してください。"
            )
        bundle.status = "published"
        bundle.etag = definition_fingerprint(bundle.model_dump(mode="json", exclude={"etag"}))
        release_id = stable_ontology_id("ontology_release", profile_id, bundle_id, etag)
        value = {
            "id": release_id,
            "profile_id": profile_id,
            "bundle": bundle.model_dump(mode="json"),
            "artifacts": render_definition_artifacts(bundle),
            "published_by": who,
            "published_at": datetime.now(UTC).isoformat(),
            "previous_release_id": expected_head,
        }
        self._commit(
            bundle,
            etag,
            extra=[
                (self.artifact(profile_id, release_id, "ontology_release_v2", value), None),
                (
                    self.artifact(
                        profile_id,
                        stable_ontology_id("ontology_head", profile_id),
                        "ontology_published_head",
                        {"release_id": release_id},
                    ),
                    head["etag"] or None,
                ),
                (
                    self.artifact(
                        profile_id,
                        operation_id,
                        "ontology_publish_operation",
                        {"release_id": release_id, "request_hash": request_hash},
                    ),
                    None,
                ),
            ],
        )
        return value

    def rollback(
        self,
        profile_id: str,
        release_id: str,
        expected_head: str,
        actor: Principal | None,
        *,
        expected_etag: str,
        key: str,
    ) -> dict[str, Any]:
        who = authorize_definition_operation(profile_id, actor)
        operation_id = stable_ontology_id("ontology_rollback", profile_id, who, key)
        request_hash = definition_fingerprint([release_id, expected_head, expected_etag])
        if not key:
            raise OntologyGateBlockedError("IDEMPOTENCY_REQUIRED", "Idempotency-Key が必要です。")
        if self.store.get_artifact(operation_id):
            operation = json.loads(
                self.document(profile_id, operation_id, "ontology_definition_audit")["content"]
            )
            if operation.get("request_hash") != request_hash:
                raise OntologyVersionConflictError(
                    "IDEMPOTENCY_KEY_REUSED", "同じキーを別の操作に使用できません。"
                )
            return self.release(profile_id, release_id) or {}
        value = self.release(profile_id, release_id)
        if value is None:
            raise OntologyNotFoundError("RELEASE_NOT_FOUND", "公開版が見つかりません。")
        bundle = ProfileOntologyBundle.model_validate(value["bundle"])
        if bundle.profile_fingerprint != definition_fingerprint(
            self.runtime._strict_profile(profile_id).model_dump(mode="json")
        ) or bundle.schema_context_fingerprint != definition_fingerprint(
            str(self.runtime.prepare_build_schema_context(profile_id).schema_context)
        ):
            raise OntologyGateBlockedError(
                "SCOPE_REVALIDATION_REQUIRED", "旧版の範囲が現在と異なります。再構築してください。"
            )
        head = self.head(profile_id)
        if head["release_id"] != expected_head or head["etag"] != expected_etag.strip('"'):
            raise OntologyVersionConflictError("PUBLISH_HEAD_CHANGED", "公開版が変更されました。")
        self.store.save_documents_atomic(
            "artifacts",
            [
                (
                    self.artifact(
                        profile_id,
                        stable_ontology_id("ontology_head", profile_id),
                        "ontology_published_head",
                        {"release_id": release_id},
                    ),
                    head["etag"] or None,
                ),
                (
                    self.artifact(
                        profile_id,
                        operation_id,
                        "ontology_definition_audit",
                        {
                            "actor": who,
                            "request_hash": request_hash,
                            "before": expected_head,
                            "after": release_id,
                            "at": datetime.now(UTC).isoformat(),
                        },
                    ),
                    None,
                ),
            ],
        )
        return value
