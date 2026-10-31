"""公開版の要求単位固定、SSE と子タスクの継承、非業務 API の分離。"""

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from rag_parser_core.language import current_pack

from app.api.router import api_router
from app.api.routes import (
    chat,
    chat_attachments,
    documents,
    evaluation,
    health,
    history,
    knowledge_bases,
    mcp,
    search,
    search_answer_profile_knowledge,
    support_guides,
)
from app.rag import published_language_runtime as runtime
from app.rag.language_pack_releases import active_release
from tests.language_release_api_support import FakeReleaseStore
from tests.support import AsgiTestClient


def test_runtime_dependency_is_on_model_entrypoints_but_not_health_or_auth() -> None:
    bound = {
        id(route.original_router)
        for route in api_router.routes
        if hasattr(route, "original_router")
        and hasattr(route, "include_context")
        and any(
            item.dependency is runtime.published_language_dependency
            for item in route.include_context.dependencies
        )
    }
    for router in (
        search.router,
        documents.router,
        knowledge_bases.router,
        history.router,
        chat_attachments.router,
        chat.router,
        mcp.router,
        evaluation.router,
        search_answer_profile_knowledge.router,
        support_guides.router,
    ):
        assert id(router) in bound
    assert id(health.router) not in bound


def test_stream_and_child_task_use_request_snapshot_until_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeReleaseStore()
    expected = store.snapshot.current

    async def initialized() -> Any:
        return store

    monkeypatch.setattr(runtime, "initialized_release_store", initialized)
    app = FastAPI()

    @app.get("/stream", dependencies=[Depends(runtime.published_language_dependency)])
    async def stream() -> StreamingResponse:
        async def child() -> str:
            await asyncio.sleep(0)
            assert active_release() == expected
            assert current_pack().hash == expected.hash
            return expected.id

        task = asyncio.create_task(child())

        async def events() -> AsyncIterator[str]:
            assert active_release() == expected
            yield await task
            await asyncio.sleep(0)
            assert active_release() == expected
            yield ":done"

        return StreamingResponse(events(), media_type="text/event-stream")

    response = AsgiTestClient(app).get("/stream")
    assert response.status_code == 200
    assert response.text == f"{expected.id}:done"
    assert active_release() is None
