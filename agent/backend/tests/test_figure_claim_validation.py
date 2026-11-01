"""図の見た目だけで決まる段落の最終の検証（#2678）の決定論テスト。

RAG の `rag_validate_answer` が図の見た目だけで決まる段落を `figure_only`（根拠の図の chunk）と
返したとき、Control Plane が、その Run で図の画像がモデルへ届いたか
（`rag_read_source(include_image=true)` の `content_images` の `model_input`）で扱いを決めることを
確かめる。

- 画像を受け取った Run: 図の見た目の段落を回答に残し、根拠に画像の参照（image_id・版・領域）を付け、
  画像で確かめたことを回答に示す。
- 画像を受け取っていない Run（画像に対応しないモデル・画像の取得の失敗）: OCR の文字の並びから推した
  図の見た目の段落を確認済みにせず外す（回答の対応は answered にしない）。
- 図の見た目の段落の無い検証の結果は変わらない。

モデルは SDK の `ScriptedModel`、RAG は契約どおりの fake の MCP（`mcp_support`）。図は合成の PNG と
合成の文字（業務の内容を含まない）。
"""

from __future__ import annotations

import base64
import contextlib
import struct
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from mcp_support import (
    _EVIDENCE,
    DEFAULT_OUTPUTS,
    FakeProductMcp,
    McpToolError,
    fake_product_mcp,
)
from pr_backend_core.mcp import McpToolResult
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.answer_validation import (
    FIGURE_READ_NOTE,
    PASSAGE_REASONS,
    REVIEW_HEADING,
    apply_figure_readings,
    carried_figure_readings,
    figure_readings,
    run_evidence_groups,
    validation_figure_readings,
)
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunState,
    RunStatus,
    RunStep,
    StepStatus,
    run_answer_text,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tool_media import ToolImage
from app.features.agent.tools import ToolCall, ToolResult
from app.settings import get_settings

SKILL_ID = "test2678-skill"
AGENT_ID = "test2678-agent"
USER_UUID = "11111111-2222-3333-4444-555555552678"
DEFAULT_READ_SOURCE: dict[str, Any] = DEFAULT_OUTPUTS["rag_read_source"]
DEFAULT_RETRIEVE: dict[str, Any] = DEFAULT_OUTPUTS["rag_retrieve_evidence"]
DEFAULT_VALIDATE: dict[str, Any] = DEFAULT_OUTPUTS["rag_validate_answer"]


def _png(width: int, height: int, marker: bytes) -> bytes:
    """大きさを読める PNG のヘッダーと、合成のバイト列。"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + marker
    )


IMAGE = _png(864, 555, b"synthetic-flow-figure-2678")
IMAGE_ID = f"sha256:{ToolImage(data=IMAGE, mime_type='image/png').sha256}"
FIGURE_REGION = {
    "document_id": "doc-1",
    "chunk_id": "chunk-fig",
    "chunk_set_id": "cs-1",
    "page": 2,
    "page_label": None,
    "bbox": [0.1, 0.2, 0.9, 0.8],
    "bbox_unit": "ratio",
}
# 合成の判定フロー図の根拠（OCR の文字だけで、矢印・配置は文字に無い）。
FIGURE_EVIDENCE = {
    **_EVIDENCE,
    "evidence_id": "chunk-fig",
    "chunk_id": "chunk-fig",
    "content_kind": "figure",
    "evidence_type": "ocr",
    "image_ref": FIGURE_REGION,
    "excerpt": "開始 判定 処理A 処理B 終了",
    "text_length": 15,
    "retrieval_rank": 2,
}
FIGURE_SOURCE = {
    **DEFAULT_READ_SOURCE,
    "evidence_id": "chunk-fig",
    "chunk_id": "chunk-fig",
    "content_kind": "figure",
    "evidence_type": "ocr",
    "image_ref": FIGURE_REGION,
    "image": {
        "mime_type": "image/png",
        "width": 864,
        "height": 555,
        "byte_size": len(IMAGE),
        "sha256": IMAGE_ID.removeprefix("sha256:"),
    },
    "text": "開始 判定 処理A 処理B 終了",
    "text_length": 15,
}
TEXT_CLAIM = "申請は判定の後に処理します。"
ARROW_CLAIM = "判定の右の分岐は処理Aにつながります。"
MEMBER_CLAIM = "処理Bは左の分岐に属します。"
ANSWER = "\n\n".join([TEXT_CLAIM, ARROW_CLAIM, MEMBER_CLAIM])


def _verdict(*claims: tuple[str, str, str | None]) -> dict[str, Any]:
    """`rag_validate_answer` の録画の応答（段落・判定・根拠の chunk）。"""
    counts: dict[str, int] = {}
    for _quote, status, _chunk in claims:
        counts[status] = counts.get(status, 0) + 1
    return {
        **deepcopy(DEFAULT_VALIDATE),
        "valid": all(status == "supported" for _q, status, _c in claims),
        "counts": counts,
        "claims": [
            {
                "answer_quote": quote,
                "status": status,
                "chunk_id": chunk,
                "reason": "合成の理由。",
                "audit_status": status,
                "reference_check": "resolved" if chunk else None,
            }
            for quote, status, chunk in claims
        ],
    }


# 図の見た目の 2 つの段落を、RAG が図の本文では確かめられない（figure_only）と返した応答。
FIGURE_VERDICT = _verdict(
    (TEXT_CLAIM, "supported", "chunk-1"),
    (ARROW_CLAIM, "figure_only", "chunk-fig"),
    (MEMBER_CLAIM, "figure_only", "chunk-fig"),
)


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    """根拠の取得・本文と図の読み取りを使う業務 Agent（RAG は fake の MCP）。"""
    fake = fake_product_mcp(
        monkeypatch,
        outputs={
            "rag_retrieve_evidence": {
                **deepcopy(DEFAULT_RETRIEVE),
                "evidence": [{**_EVIDENCE, "used_in_answer": False}, FIGURE_EVIDENCE],
            },
            "rag_read_source": _read_source,
            "rag_validate_answer": FIGURE_VERDICT,
        },
    )
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", True)
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="図の検証の Skill",
            instructions="RAG で根拠と図を読んで答える。",
            mcp_requirements=[
                SkillMcpRequirement(
                    server_id="rag", tool_names=["rag_retrieve_evidence", "rag_read_source"]
                )
            ],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="図の検証の業務 Agent", skill_ids=[SKILL_ID])
    )
    try:
        yield fake
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id for run_id, run in repository._runs.items() if run.agent_id == AGENT_ID
            ]:
                repository._runs.pop(run_id)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(AGENT_ID)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(SKILL_ID)


def _read_source(argument: Any) -> Any:
    """図の chunk は構造化データ + 画像（`McpToolResult`）、ほかは本文だけ。"""
    if argument.chunk_id != "chunk-fig":
        return deepcopy(DEFAULT_READ_SOURCE)
    if not argument.include_image:
        return {**deepcopy(FIGURE_SOURCE), "image": None}
    block = {"type": "image", "data": base64.b64encode(IMAGE).decode(), "mimeType": "image/png"}
    return McpToolResult(output=deepcopy(FIGURE_SOURCE), content=(block,))


def _run(monkeypatch: MonkeyPatch, *, vision: bool, chunk_id: str = "chunk-fig") -> RunState:
    model = ScriptedModel(
        [
            [function_call("rag__rag_retrieve_evidence", {"query": "判定の後"}, call_id="c1")],
            [
                function_call(
                    "rag__rag_read_source",
                    {"document_id": "doc-1", "chunk_id": chunk_id, "include_image": True},
                    call_id="c2",
                )
            ],
            [assistant_message(ANSWER)],
        ]
    )
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id=model_id or "test-model",
            endpoint="https://llm.example",
            project_id="",
            api_key="k",
            vision_enabled=vision,
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="判定の後の分岐は？", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return runtime_repository.get_run(run.id)


def _artifact(run: RunState, kind: str) -> dict[str, Any]:
    [artifact] = [item for item in run.artifacts if item.kind == kind]
    return artifact.content


def _claims(content: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {claim["answer_quote"]: claim for claim in content["result"]["claims"]}


def test_run_that_read_the_figure_image_keeps_the_figure_claims_with_the_image(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """画像を受け取った Run: 図の見た目の段落は外さず、画像の参照を根拠に付けて示す。"""
    run = _run(monkeypatch, vision=True)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    # 図の chunk は検証の根拠に入れる（回答が書いていなくても先にする）。
    [call] = mcp.calls_of("rag_validate_answer")
    assert {"document_id": "doc-1", "chunk_id": "chunk-fig"} in call["arguments"]["evidence"]
    answer = run_answer_text(run) or ""
    assert answer == f"{ANSWER}\n\n{FIGURE_READ_NOTE}"
    assert REVIEW_HEADING not in answer
    content = _artifact(run, "answer_validation")
    assert content["valid"] is True
    claims = _claims(content)
    for quote in (ARROW_CLAIM, MEMBER_CLAIM):
        claim = claims[quote]
        assert (claim["status"], claim["rag_status"], claim["figure_check"]) == (
            "supported",
            "figure_only",
            "image_read",
        )
        image = claim["figure_image"]
        assert (image["image_id"], image["chunk_set_id"], image["region"]["page"]) == (
            IMAGE_ID,
            "cs-1",
            2,
        )
    assert [item["answer_quote"] for item in content["figure_confirmed"]] == [
        ARROW_CLAIM,
        MEMBER_CLAIM,
    ]
    # 回答の根拠の図に、届いた画像の参照（image_id・版・領域）を付ける。
    evidence = {item["chunk_id"]: item for item in _artifact(run, "answer_evidence")["items"]}
    reading = evidence["chunk-fig"]["image_reading"]
    assert evidence["chunk-fig"]["cited"] is True
    assert (reading["model_input"], reading["image_id"], reading["chunk_set_id"]) == (
        "attached",
        IMAGE_ID,
        "cs-1",
    )
    assert reading["region"]["bbox"] == FIGURE_REGION["bbox"]
    assert "image_reading" not in evidence["chunk-1"]
    assert _artifact(run, "answer")["outcome"]["value"] == "answered"


@pytest.mark.parametrize(
    ("vision", "chunk_id"),
    [(False, "chunk-fig"), (True, "chunk-fig-mistyped")],
    ids=["model-without-image-input", "image-not-fetched"],
)
def test_run_that_did_not_read_the_figure_image_marks_the_figure_claims(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp, vision: bool, chunk_id: str
) -> None:
    """画像を受け取っていない Run: OCR の文字から推した図の見た目の段落を確認済みにせず、本文に
    残して未確認の印（図の画像を読めていない）を付ける（#2738）。"""
    if chunk_id != "chunk-fig":
        mcp.outputs["rag_read_source"] = McpToolError("source_not_found", "根拠が見つかりません")
    run = _run(monkeypatch, vision=vision, chunk_id=chunk_id)

    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    answer = run_answer_text(run) or ""
    body, _heading, notice = answer.partition(f"**{REVIEW_HEADING}**")
    assert TEXT_CLAIM in body
    assert ARROW_CLAIM in body and MEMBER_CLAIM in body
    assert FIGURE_READ_NOTE not in answer
    label = PASSAGE_REASONS["figure_not_read"][1]
    assert label == "図の画像を読めておらず確かめられない"
    assert f"- {label}: 「{ARROW_CLAIM}」" in notice
    content = _artifact(run, "answer_validation")
    assert content["valid"] is False
    assert "figure_confirmed" not in content
    claims = _claims(content)
    assert (claims[ARROW_CLAIM]["status"], claims[ARROW_CLAIM]["figure_check"]) == (
        "unsupported",
        "image_not_read",
    )
    if not vision:
        # 画像を返したが届かなかった理由を残す。
        assert claims[ARROW_CLAIM]["figure_image"] == {"model_input": "unsupported_model"}
    assert content["withheld"]["unverified"] == 2
    assert {item["reasons"][0]["code"] for item in content["passage_marks"]} == {"figure_not_read"}
    evidence = _artifact(run, "answer_evidence")["items"]
    assert all("image_reading" not in item for item in evidence)
    assert _artifact(run, "answer")["outcome"]["value"] != "answered"


def test_verdict_without_figure_claims_is_unchanged(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    """正しい場合は変えない: 図の見た目の段落の無い判定は、画像を読んだ Run でも今までどおり。"""
    mcp.outputs["rag_validate_answer"] = _verdict(
        (TEXT_CLAIM, "supported", "chunk-1"),
        (ARROW_CLAIM, "supported", "chunk-fig"),
        (MEMBER_CLAIM, "unsupported", None),
    )
    run = _run(monkeypatch, vision=True)

    answer = run_answer_text(run) or ""
    assert ARROW_CLAIM in answer and FIGURE_READ_NOTE not in answer
    body, _heading, notice = answer.partition(f"**{REVIEW_HEADING}**")
    assert MEMBER_CLAIM in body and MEMBER_CLAIM in notice
    claims = _claims(_artifact(run, "answer_validation"))
    assert all("figure_check" not in claim for claim in claims.values())


# ---- 図の画像の読み取りの記録（Run の step から決める） ------------------------------------------


def _read_step(refs: list[dict[str, Any]], *, success: bool = True, name: str = "rag") -> RunStep:
    return RunStep(
        run_id="run-1",
        status=StepStatus.COMPLETED if success else StepStatus.FAILED,
        tool_call=ToolCall(
            name=f"{name}__rag_read_source",
            arguments={"document_id": "doc-1", "chunk_id": "chunk-fig", "include_image": True},
        ),
        tool_result=ToolResult(
            name=f"{name}__rag_read_source",
            success=success,
            output={**FIGURE_SOURCE, "content_images": refs} if success else None,
        ),
    )


def _ref(model_input: str) -> dict[str, Any]:
    return {
        "image_id": IMAGE_ID,
        "mime_type": "image/png",
        "model_input": model_input,
        "source": {
            "server_id": "rag",
            "tool_name": "rag_read_source",
            "content_index": 1,
            "document_id": "doc-1",
            "chunk_id": "chunk-fig",
            "chunk_set_id": "cs-1",
            "region": FIGURE_REGION,
        },
    }


def test_figure_readings_prefer_the_image_that_reached_the_model() -> None:
    attached = _read_step([_ref("attached")])
    steps = [attached, _read_step([_ref("over_limit")]), _read_step([], success=False)]

    readings = figure_readings(steps)

    reading = readings["rag"]["chunk-fig"]
    assert (reading["model_input"], reading["image_id"], reading["step_id"]) == (
        "attached",
        IMAGE_ID,
        attached.id,
    )
    assert figure_readings([_read_step([_ref("unsupported_model")])])["rag"]["chunk-fig"][
        "model_input"
    ] == ("unsupported_model")
    # 由来に文書・chunk の無い画像（図の根拠に結び付けられない）は数えない。
    assert figure_readings([_read_step([{"image_id": IMAGE_ID, "model_input": "attached"}])]) == {}


def test_attached_figure_chunk_is_kept_within_the_evidence_limit() -> None:
    search = RunStep(
        run_id="run-1",
        status=StepStatus.COMPLETED,
        tool_call=ToolCall(name="rag__rag_retrieve_evidence", arguments={"query": "q"}),
        tool_result=ToolResult(
            name="rag__rag_retrieve_evidence",
            success=True,
            output={
                "evidence": [
                    {**_EVIDENCE, "chunk_id": f"chunk-{index}", "excerpt": f"本文 {index}"}
                    for index in range(40)
                ]
            },
        ),
    )
    groups = run_evidence_groups([_read_step([_ref("attached")]), search], ANSWER)

    [(_tool, refs)] = groups
    assert refs[0] == {"document_id": "doc-1", "chunk_id": "chunk-fig"}


def test_apply_figure_readings_recounts_and_keeps_other_results() -> None:
    result = {**deepcopy(FIGURE_VERDICT), "status": "completed"}
    readings = {"chunk-fig": {"model_input": "attached", "image_id": IMAGE_ID}}

    read = apply_figure_readings(result, readings)
    not_read = apply_figure_readings(result, {})

    assert (read["valid"], read["counts"]) == (True, {"supported": 3})
    assert (not_read["valid"], not_read["counts"]) == (False, {"supported": 1, "unsupported": 2})
    plain = deepcopy(DEFAULT_VALIDATE)
    assert apply_figure_readings(plain, readings) is plain


def test_previous_turn_figure_readings_are_carried() -> None:
    """前のターンで図の画像で確かめた図は、言い換えのターンで画像を読み直さなくても使う。"""
    previous = {
        "status": "completed",
        "connections": [
            {
                "status": "completed",
                "tool_name": "rag__rag_validate_answer",
                "result": {
                    "claims": [
                        {
                            "answer_quote": ARROW_CLAIM,
                            "status": "supported",
                            "chunk_id": "chunk-fig",
                            "figure_check": "image_read",
                            "figure_image": {"image_id": IMAGE_ID, "chunk_set_id": "cs-1"},
                        }
                    ]
                },
            }
        ],
    }

    carried = carried_figure_readings([previous])
    combined = validation_figure_readings([_read_step([_ref("unsupported_model")])], [previous])

    assert carried["rag"]["chunk-fig"]["model_input"] == "attached"
    assert combined["rag"]["chunk-fig"]["image_id"] == IMAGE_ID
    assert combined["rag"]["chunk-fig"]["carried"] is True
