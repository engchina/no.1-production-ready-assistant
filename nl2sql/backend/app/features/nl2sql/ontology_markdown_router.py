"""Markdown 公開準備・確認の Profile 所有 API。"""

from typing import Any

from fastapi import APIRouter, Header, Request
from pr_backend_core import ApiResponse

from .ontology_definitions import DefinitionDataValidationRequest
from .ontology_markdown_workspace import (
    MarkdownConfirmRequest,
    MarkdownOntologyWorkspace,
    MarkdownPrepareRequest,
)
from .profile_access import assert_profile_access, principal_from_request


def create_markdown_router(runtime: Any, raise_error: Any) -> APIRouter:
    router = APIRouter()

    def service(request: Request, profile_id: str) -> MarkdownOntologyWorkspace:
        assert_profile_access(request, profile_id)
        return MarkdownOntologyWorkspace(runtime())

    @router.post("/profiles/{profile_id}/ontology-markdown/prepare", status_code=202)
    def prepare(
        profile_id: str,
        body: MarkdownPrepareRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).prepare(
                    profile_id, body.draft_etag, idempotency_key, principal_from_request(request)
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.get("/profiles/{profile_id}/ontology-markdown/preparations/{preparation_id}")
    def preparation(profile_id: str, preparation_id: str, request: Request) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).preparation(profile_id, preparation_id)
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.post(
        "/profiles/{profile_id}/ontology-markdown/preparations/{preparation_id}/validate-data"
    )
    def validate_data(
        profile_id: str,
        preparation_id: str,
        body: DefinitionDataValidationRequest,
        request: Request,
    ) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).validate_data(
                    profile_id, preparation_id, body, principal_from_request(request)
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.get("/profiles/{profile_id}/ontology-markdown/publications/{snapshot_id}/diagnostics")
    def diagnostics(profile_id: str, snapshot_id: str, request: Request) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).publication_diagnostics(profile_id, snapshot_id)
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.post("/profiles/{profile_id}/ontology-markdown/publish")
    def publish(
        profile_id: str,
        body: MarkdownConfirmRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data={
                    "job": service(request, profile_id).publish(
                        profile_id, body, idempotency_key, principal_from_request(request)
                    )
                }
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.get("/profiles/{profile_id}/ontology-markdown/publication-outcome")
    def outcome(profile_id: str, key: str, request: Request) -> ApiResponse[Any]:
        try:
            from .ontology_definition_workspace import authorize_definition_operation
            from .ontology_markdown_workspace import SNAPSHOT
            from .ontology_store import stable_ontology_id

            svc = service(request, profile_id)
            who = authorize_definition_operation(profile_id, principal_from_request(request))
            identity = stable_ontology_id(SNAPSHOT, profile_id, who, key)
            snapshot = (
                svc.snapshot(profile_id, identity) if svc.store.get_artifact(identity) else None
            )
            return ApiResponse(data={"job": snapshot["publish_job"] if snapshot else None})
        except Exception as exc:
            raise_error(exc)
            raise

    return router
