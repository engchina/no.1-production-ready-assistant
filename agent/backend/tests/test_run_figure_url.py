"""Run の RAG の図の根拠を画面で開く短命の URL（`GET /api/runs/{run_id}/figure-url`。#1311）。

RAG の MCP は契約どおりの fake（`mcp_support`）。Agent は画面を見ている利用者を `sub`、
`purpose=figure_url` をサービストークンに入れて `rag_read_source`（`include_image_url`）を呼び、
返った URL（公開の起点があれば付け替え）を返す。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from mcp_support import FakeProductMcp, McpToolError, fake_product_mcp
from pytest import MonkeyPatch
from security_support import (
    ProductionAuth,
    client,
    enable_production_auth,
    login,
    login_configured_admin,
)

from app.features.agent.config import runtime_config_store
from app.features.agent.runtime import (
    Artifact,
    RunState,
    RunStatus,
    RunStep,
    StepStatus,
    runtime_repository,
)
from app.features.agent.tools import (
    ExternalMcpToolInfo,
    ToolCall,
    ToolInvocationContext,
    ToolResult,
    mcp_function_name,
    mcp_tool_handler,
)
from app.security.domain import CONFIGURED_SYSTEM_ADMIN_USER_UUID
from app.security.permissions import MENU_RUNS, RUNS_VIEW
from app.security.service import set_security_service
from app.settings import get_settings

RUN_ID = "run-figure-1311"
CREATOR = "aaaaaaaa-0000-0000-0000-000000001311"
RAG_PATH = "/api/figures/f1.payload.signature"
RAG_URL = f"http://10.0.0.5{RAG_PATH}"
FIGURE_REF = {
    "document_id": "doc-1",
    "chunk_id": "fig-1",
    "chunk_set_id": "cs-1",
    "page": 2,
    "page_label": None,
    "bbox": [60.0, 80.0, 300.0, 200.0],
    "bbox_unit": "absolute",
}


def _evidence(chunk_id: str, *, image_ref: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "evidence_id": chunk_id,
        "document_id": "doc-1",
        "chunk_id": chunk_id,
        "file_name": "申請手順.pdf",
        "evidence_type": "figure_description" if image_ref else "text",
        "image_ref": image_ref,
        "locator": {"section_path": [], "page_start": 2, "page_end": 2},
        "excerpt": "申請画面",
        "used_in_answer": True,
    }


def _run(agent_id: str = "default") -> RunState:
    return RunState(
        id=RUN_ID,
        goal="申請の手順は？",
        agent_id=agent_id,
        runtime_id="builtin",
        status=RunStatus.COMPLETED,
        created_by_user_uuid=CREATOR,
        artifacts=[
            Artifact(
                name=f"{mcp_function_name('rag', 'rag_search')}:step-1",
                kind="rag_evidence",
                content={
                    "evidence": [
                        _evidence("fig-1", image_ref=FIGURE_REF),
                        _evidence("text-1", image_ref=None),
                    ]
                },
            )
        ],
    )


def _read_source_output(argument: Any) -> dict[str, Any]:
    assert argument.include_image_url is True
    return {
        "schema_version": 4,
        "evidence_id": argument.chunk_id,
        "document_id": argument.document_id,
        "chunk_id": argument.chunk_id,
        "image_url": {
            "url": RAG_URL,
            "path": RAG_PATH,
            "expires_at": "2026-10-08T00:05:00+00:00",
            "expires_in_seconds": 300,
        },
        "locator": {"section_path": []},
        "text": "申",
        "offset": 0,
        "text_length": 4,
        "truncated": True,
    }


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


@pytest.fixture
def rag(monkeypatch: MonkeyPatch) -> FakeProductMcp:
    return fake_product_mcp(monkeypatch, outputs={"rag_read_source": _read_source_output})


@pytest.fixture
def seeded_run() -> Iterator[RunState]:
    repository: Any = runtime_repository
    run = _run()
    with repository._lock:  # noqa: SLF001 - テスト用に Run を直接置く
        repository._runs[run.id] = run  # noqa: SLF001
    try:
        yield run
    finally:
        with repository._lock:  # noqa: SLF001
            repository._runs.pop(run.id, None)  # noqa: SLF001


def _owner_viewer(
    auth: ProductionAuth, run: RunState, login_user_id: str = "figure-viewer"
) -> None:
    """Run を実行した利用者として図を開く利用者（「AI 活用」は自分の Run だけ。#1889）。"""
    user = auth.user_with_permissions(login_user_id, [MENU_RUNS, RUNS_VIEW], agent_ids=["default"])
    run.created_by_user_uuid = user.user_uuid


def _url(chunk_id: str = "fig-1", document_id: str = "doc-1") -> str:
    return f"/api/runs/{RUN_ID}/figure-url?document_id={document_id}&chunk_id={chunk_id}"


def test_viewer_gets_short_lived_url_issued_as_viewer(
    auth: ProductionAuth, rag: FakeProductMcp, seeded_run: RunState, monkeypatch: MonkeyPatch
) -> None:
    del seeded_run
    # Run を実行していない利用者は、同じ業務 Agent の範囲でも開けない（404。#1889）。
    auth.user_with_permissions("figure-viewer", [MENU_RUNS, RUNS_VIEW], agent_ids=["default"])
    assert client.get(_url(), headers=login("figure-viewer")).status_code == 404
    # system_admin は全員の Run を見られる。URL は Run の利用者ではなく、見ている利用者として作る。
    headers = login_configured_admin()

    response = client.get(_url(), headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["data"] == {"url": RAG_URL, "expires_at": "2026-10-08T00:05:00+00:00"}
    # URL はトークンを含むので cache に残さない。
    assert response.headers["cache-control"] == "private, no-store"

    [call] = rag.calls_of("rag_read_source")
    assert call["arguments"] == {
        "document_id": "doc-1",
        "chunk_id": "fig-1",
        "include_image_url": True,
        "max_chars": 1,
    }
    # 画面を見ている利用者として呼ぶ（Run の利用者の権限は借りない）。画面の操作の印を付ける。
    claims = call["claims"]
    assert (claims["sub"], claims["aud"], claims["purpose"]) == (
        CONFIGURED_SYSTEM_ADMIN_USER_UUID,
        "rag",
        "figure_url",
    )
    assert claims["sub"] != CREATOR
    assert claims["run_id"] == RUN_ID

    # 公開の起点（ブラウザから RAG を開く URL）があれば、RAG の path をそこに付ける。
    monkeypatch.setattr(get_settings(), "agent_external_rag_public_url", "https://rag.example.com/")
    public = client.get(_url(), headers=headers).json()["data"]
    assert public["url"] == f"https://rag.example.com{RAG_PATH}"

    # 1 台の Compute（#1316）: 起点は同じ origin の /rag
    # （前段の reverse proxy が /rag/api/ を RAG へ渡す構成）。
    monkeypatch.setattr(get_settings(), "agent_external_rag_public_url", "/rag")
    same_origin = client.get(_url(), headers=headers).json()["data"]
    assert same_origin["url"] == f"/rag{RAG_PATH}"


def test_only_figures_in_the_run_and_accessible_agents_can_be_opened(
    auth: ProductionAuth, rag: FakeProductMcp, seeded_run: RunState
) -> None:
    _owner_viewer(auth, seeded_run)
    headers = login("figure-viewer")
    # 図でない根拠・Run に無い根拠は RAG を呼ばずに 404。
    assert client.get(_url("text-1"), headers=headers).status_code == 404
    assert client.get(_url("fig-1", "doc-2"), headers=headers).status_code == 404
    assert client.get(_url("other"), headers=headers).status_code == 404
    assert rag.calls_of("rag_read_source") == []
    # ほかの利用者の Run（その業務 Agent を使えない利用者を含む）は 404、
    # 実行を見る権限の無い利用者は経路で 403。
    auth.user_with_permissions("sales-viewer", [MENU_RUNS, RUNS_VIEW], agent_ids=["sales"])
    assert client.get(_url(), headers=login("sales-viewer")).status_code == 404
    auth.user_with_permissions("no-runs", [], agent_ids=["default"])
    assert client.get(_url(), headers=login("no-runs")).status_code == 403
    assert client.get(_url()).status_code == 401
    assert rag.calls_of("rag_read_source") == []


def test_figures_from_earlier_rag_calls_can_be_opened(
    auth: ProductionAuth, rag: FakeProductMcp, seeded_run: RunState
) -> None:
    """成果物「検索の根拠」に無い、前の `rag_retrieve_evidence` の図も開ける（#2427）。"""
    name = mcp_function_name("rag", "rag_retrieve_evidence")
    earlier_ref = {**FIGURE_REF, "chunk_id": "fig-earlier"}
    seeded_run.steps = [
        RunStep(
            run_id=RUN_ID,
            status=StepStatus.COMPLETED,
            tool_call=ToolCall(name=name, arguments={}),
            tool_result=ToolResult(
                name=name,
                success=True,
                output={
                    "evidence": [
                        _evidence("fig-earlier", image_ref=earlier_ref),
                        _evidence("text-earlier", image_ref=None),
                    ]
                },
            ),
        )
    ]
    _owner_viewer(auth, seeded_run)
    headers = login("figure-viewer")
    assert client.get(_url("fig-earlier"), headers=headers).status_code == 200
    # 図でない根拠は、前の呼び出しのものでも RAG を呼ばずに 404。
    assert client.get(_url("text-earlier"), headers=headers).status_code == 404
    assert [call["arguments"]["chunk_id"] for call in rag.calls_of("rag_read_source")] == [
        "fig-earlier"
    ]


@pytest.mark.parametrize(
    ("error_code", "status"),
    [
        ("source_stale", 409),
        ("source_not_found", 404),
        ("image_not_available", 404),
        ("image_url_unavailable", 503),
        ("internal_error", 502),
    ],
)
def test_rag_errors_are_returned_as_screen_messages(
    auth: ProductionAuth,
    rag: FakeProductMcp,
    seeded_run: RunState,
    error_code: str,
    status: int,
) -> None:
    rag.outputs["rag_read_source"] = McpToolError(error_code, "RAG のエラー")
    _owner_viewer(auth, seeded_run)
    response = client.get(_url(), headers=login("figure-viewer"))
    assert response.status_code == status
    assert response.json()["error_messages"]


def test_unexpected_url_from_rag_is_not_returned(
    auth: ProductionAuth, rag: FakeProductMcp, seeded_run: RunState
) -> None:
    def bad(argument: Any) -> dict[str, Any]:
        output = _read_source_output(argument)
        output["image_url"] = {**output["image_url"], "url": "javascript:alert(1)", "path": "/x"}
        return output

    rag.outputs["rag_read_source"] = bad
    _owner_viewer(auth, seeded_run)
    assert client.get(_url(), headers=login("figure-viewer")).status_code == 502


def test_run_tool_calls_do_not_carry_figure_url_purpose(rag: FakeProductMcp) -> None:
    """Run の中のツール呼び出し（モデル）には画面の操作の印を付けない（RAG は URL を作らない）。"""
    config = runtime_config_store.get_mcp("rag")
    info = ExternalMcpToolInfo(
        name="rag_read_source",
        server_id="rag",
        read_only=True,
        function_name=mcp_function_name("rag", "rag_read_source"),
    )
    rag.outputs["rag_read_source"] = {"schema_version": 4, "text": ""}
    mcp_tool_handler(config, info)(
        {"document_id": "doc-1", "chunk_id": "fig-1"},
        ToolInvocationContext(user_uuid=CREATOR, run_id=RUN_ID, agent_id="default"),
    )
    [call] = rag.calls_of("rag_read_source")
    assert "purpose" not in call["claims"]
