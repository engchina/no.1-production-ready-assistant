"""公開 Profile 能力の設定・独立した使用入口。"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pr_backend_core import ApiResponse

from .ontology_capabilities import (
    CapabilityBindingRequest,
    CapabilityCallRequest,
    CapabilityExecuteRequest,
    ProfileOntologyCapabilityService,
)
from .ontology_presentation import execution_view
from .ontology_service import OntologyGateBlockedError
from .profile_access import assert_profile_access, principal_from_request


def create_capability_router(runtime: Any, raise_error: Any) -> APIRouter:
    router = APIRouter()

    def service(request: Request, profile_id: str) -> ProfileOntologyCapabilityService:
        assert_profile_access(request, profile_id)
        if request.method != "GET":
            from .ontology_definition_workspace import authorize_definition_operation

            authorize_definition_operation(profile_id, principal_from_request(request))
            raise HTTPException(
                410, "この独立した操作は廃止されました。Markdown オントロジーを使用してください。"
            )
        return ProfileOntologyCapabilityService(runtime())

    @router.get("/profiles/{profile_id}/ontology-capabilities")
    def catalog(profile_id: str, request: Request) -> ApiResponse[Any]:
        try:
            svc = service(request, profile_id)
            return ApiResponse(data=execution_view(svc, profile_id, svc.catalog(profile_id)))
        except Exception as exc:
            raise_error(exc)
            raise

    @router.patch("/profiles/{profile_id}/ontology-capabilities/{definition_id}/binding")
    def bind(
        profile_id: str,
        definition_id: str,
        body: CapabilityBindingRequest,
        request: Request,
        if_match: str = Header(alias="If-Match"),
    ) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).bind(
                    profile_id, definition_id, body, if_match, principal_from_request(request)
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.post("/profiles/{profile_id}/ontology-capabilities/{definition_id}/invoke")
    def invoke(
        profile_id: str,
        definition_id: str,
        body: CapabilityCallRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).invoke(
                    profile_id,
                    definition_id,
                    body,
                    idempotency_key,
                    principal_from_request(request),
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.post("/profiles/{profile_id}/ontology-capabilities/{definition_id}/preview")
    def preview(
        profile_id: str,
        definition_id: str,
        body: CapabilityCallRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).preview(
                    profile_id,
                    definition_id,
                    body,
                    idempotency_key,
                    principal_from_request(request),
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.get(
        "/profiles/{profile_id}/ontology-capabilities/{definition_id}/previews/{preview_id}/outcome"
    )
    def outcome(
        profile_id: str, definition_id: str, preview_id: str, request: Request
    ) -> ApiResponse[Any]:
        try:
            svc = service(request, profile_id)
            value = svc.outcome(
                profile_id, definition_id, preview_id, principal_from_request(request)
            )
            if value.get("execution"):
                value = {**value, "execution": execution_view(svc, profile_id, value["execution"])}
            return ApiResponse(data=value)
        except Exception as exc:
            raise_error(exc)
            raise

    @router.post("/profiles/{profile_id}/ontology-capabilities/{definition_id}/execute")
    def execute(
        profile_id: str,
        definition_id: str,
        body: CapabilityExecuteRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key"),
    ) -> ApiResponse[Any]:
        try:
            return ApiResponse(
                data=service(request, profile_id).execute(
                    profile_id,
                    definition_id,
                    body.preview_id,
                    idempotency_key,
                    principal_from_request(request),
                )
            )
        except Exception as exc:
            raise_error(exc)
            raise

    @router.get("/profiles/{profile_id}/ontology-capabilities/executions/{execution_id}")
    def execution(profile_id: str, execution_id: str, request: Request) -> ApiResponse[Any]:
        try:
            svc = service(request, profile_id)
            record = svc.store.get_artifact(execution_id)
            if not record or record.get("artifact_type") not in (
                "ontology_action_execution",
                "ontology_function_call",
                "ontology_action_preview",
            ):
                raise OntologyGateBlockedError("EXECUTION_NOT_FOUND", "実行記録が見つかりません。")
            value = json.loads(
                svc.document(profile_id, execution_id, record["artifact_type"])["content"]
            )
            actor = principal_from_request(request)
            if actor and not actor.is_system_admin and value.get("actor") != actor.user_uuid:
                raise OntologyGateBlockedError(
                    "EXECUTION_NOT_FOUND", "この実行記録の参照権限がありません。"
                )
            return ApiResponse(data=execution_view(svc, profile_id, value))
        except Exception as exc:
            raise_error(exc)
            raise

    return router
