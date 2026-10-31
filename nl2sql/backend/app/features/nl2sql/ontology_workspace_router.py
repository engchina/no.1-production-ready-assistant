"""新形式 workspace API。各更新は専用 DTO と ETag を持つ。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pr_backend_core import ApiResponse

from .ontology_definition_workspace import ProfileOntologyWorkspaceService
from .ontology_definitions import (
    DefinitionAnalyzeRequest,
    DefinitionApplyRequest,
    DefinitionDataValidationRequest,
    DefinitionEditRequest,
    DefinitionNotesRequest,
    DefinitionPublishRequest,
    DefinitionResolveRequest,
    DefinitionReviewRequest,
)
from .ontology_presentation import bundle_view, release_view
from .ontology_service import OntologyVersionConflictError
from .profile_access import assert_profile_access, principal_from_request


def create_workspace_router(runtime: Any, raise_error: Any) -> APIRouter:
    router = APIRouter()

    def service(request: Request, profile_id: str) -> ProfileOntologyWorkspaceService:
        assert_profile_access(request, profile_id)
        if request.method != "GET":
            from .ontology_definition_workspace import authorize_definition_operation

            authorize_definition_operation(profile_id, principal_from_request(request))
            raise HTTPException(
                410, "この独立した操作は廃止されました。Markdown オントロジーを使用してください。"
            )
        return ProfileOntologyWorkspaceService(runtime())

    @router.get("/profiles/{profile_id}/ontology-results/{result_id}/workspace")
    def workspace(profile_id: str, result_id: str, request: Request) -> ApiResponse[Any]:
        from .ontology_definition_artifacts import render_definition_artifacts

        try:
            svc = service(request, profile_id)
            bundle = svc.get(profile_id, result_id)
            published = release_view(svc, profile_id)
            return ApiResponse(
                data={
                    "bundle": bundle_view(svc.store, bundle),
                    "artifacts": render_definition_artifacts(bundle),
                    "head": {
                        **svc.head(profile_id),
                        "display_version": published["display_version"] if published else None,
                    },
                    "releases": [
                        {
                            "id": json.loads(record["content"])["id"],
                            "published_at": json.loads(record["content"])["published_at"],
                            "display_version": bundle_view(
                                svc.store, json.loads(record["content"])["bundle"]
                            )["display_version"],
                        }
                        for record in svc.store.list_artifacts(svc._session(profile_id))
                        if record.get("artifact_type") == "ontology_release_v2"
                        and record.get("profile_id") == profile_id
                    ],
                }
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.patch("/profiles/{profile_id}/ontology-results/{result_id}")
    def edit(
        profile_id: str,
        result_id: str,
        body: DefinitionEditRequest,
        request: Request,
        if_match: str = Header(alias="If-Match"),
    ) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).edit(
                    profile_id,
                    result_id,
                    if_match,
                    body.definitions,
                    principal_from_request(request),
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.post("/profiles/{profile_id}/ontology-results/{result_id}/notes")
    def notes(
        profile_id: str,
        result_id: str,
        body: DefinitionNotesRequest,
        request: Request,
        if_match: str = Header(alias="If-Match"),
    ) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).notes(
                    profile_id, result_id, if_match, body.notes_ja, principal_from_request(request)
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.post("/profiles/{profile_id}/ontology-results/{result_id}/validate")
    def validate(
        profile_id: str, result_id: str, request: Request, if_match: str = Header(alias="If-Match")
    ) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).validate(
                    profile_id, result_id, if_match, principal_from_request(request)
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.post("/profiles/{profile_id}/ontology-results/{result_id}/review")
    def review(
        profile_id: str,
        result_id: str,
        body: DefinitionReviewRequest,
        request: Request,
        if_match: str = Header(alias="If-Match"),
    ) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).review(
                    profile_id,
                    result_id,
                    if_match,
                    body.definition_ids,
                    principal_from_request(request),
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.post("/profiles/{profile_id}/ontology-results/{result_id}/publish")
    def publish(
        profile_id: str,
        result_id: str,
        body: DefinitionPublishRequest,
        request: Request,
        if_match: str = Header(alias="If-Match"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).publish(
                    profile_id,
                    result_id,
                    if_match,
                    body.expected_head,
                    idempotency_key,
                    principal_from_request(request),
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.post("/profiles/{profile_id}/ontology-results/{result_id}/analyze")
    def analyze(
        profile_id: str,
        result_id: str,
        body: DefinitionAnalyzeRequest,
        request: Request,
        if_match: str = Header(alias="If-Match"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).analyze(
                    profile_id,
                    result_id,
                    if_match,
                    body.instruction_ja,
                    idempotency_key,
                    principal_from_request(request),
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.post("/profiles/{profile_id}/ontology-results/{result_id}/changes/{change_id}/apply")
    def apply_changes(
        profile_id: str,
        result_id: str,
        change_id: str,
        body: DefinitionApplyRequest,
        request: Request,
        if_match: str = Header(alias="If-Match"),
    ) -> ApiResponse[Any]:
        del body
        try:
            svc = service(request, profile_id)
            change = json.loads(svc.document(profile_id, change_id, "ontology_changes")["content"])
            if change["bundle_id"] != result_id or change["base_etag"].strip('"') != if_match.strip(
                '"'
            ):
                raise OntologyVersionConflictError(
                    "CHANGE_BASE_STALE", "解析時の定義から変更されました。再解析してください。"
                )
            definitions = DefinitionEditRequest.model_validate(
                {"definitions": change["after"]}
            ).definitions
            return ApiResponse(
                data=svc.edit(
                    profile_id, result_id, if_match, definitions, principal_from_request(request)
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.post("/profiles/{profile_id}/ontology-results/{result_id}/conflicts/{index}/resolve")
    def resolve(
        profile_id: str,
        result_id: str,
        index: int,
        body: DefinitionResolveRequest,
        request: Request,
        if_match: str = Header(alias="If-Match"),
    ) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).resolve(
                    profile_id,
                    result_id,
                    if_match,
                    index,
                    body.choice,
                    principal_from_request(request),
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.get("/profiles/{profile_id}/ontology-published")
    def published(profile_id: str, request: Request) -> ApiResponse[Any]:
        try:
            return ApiResponse(data=release_view(service(request, profile_id), profile_id))
        except Exception as exc:
            raise_error(exc)
            raise

    @router.get("/profiles/{profile_id}/ontology-releases/{release_id}")
    def release(profile_id: str, release_id: str, request: Request) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=release_view(service(request, profile_id), profile_id, release_id)
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.post("/profiles/{profile_id}/ontology-releases/{release_id}/rollback")
    def rollback(
        profile_id: str,
        release_id: str,
        body: DefinitionPublishRequest,
        request: Request,
        if_match: str = Header(alias="If-Match"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).rollback(
                    profile_id,
                    release_id,
                    body.expected_head,
                    principal_from_request(request),
                    expected_etag=if_match,
                    key=idempotency_key,
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    from .ontology_definition_data_validation import read_validation_job, start_validation_job

    @router.post("/profiles/{profile_id}/ontology-results/{result_id}/validation-jobs")
    def start_data_check(
        profile_id: str,
        result_id: str,
        body: DefinitionDataValidationRequest,
        request: Request,
        if_match: str = Header(alias="If-Match"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> ApiResponse[Any]:
        try:
            service(request, profile_id)
            return ApiResponse(
                data=start_validation_job(
                    runtime(),
                    profile_id,
                    result_id,
                    if_match,
                    body,
                    idempotency_key,
                    principal_from_request(request),
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.get("/profiles/{profile_id}/ontology-validation-jobs/{job_id}")
    def data_check(profile_id: str, job_id: str, request: Request) -> ApiResponse[Any]:
        try:
            service(request, profile_id)
            return ApiResponse(data=read_validation_job(runtime(), profile_id, job_id))
        except Exception as exc:
            raise_error(exc)
            raise

    return router
