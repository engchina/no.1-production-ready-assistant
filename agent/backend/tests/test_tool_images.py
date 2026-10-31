"""ツールの結果の画像をモデルの入力まで届ける（#2650）の決定論テスト。

- MCP の `tools/call` の結果の text / image / structuredContent を区別して受け取り、構造化データの
  有無で画像を落とさない。構造化データに無い説明文は残し、構造化データを直列化しただけの互換用の
  text は二重に入れない。
- 画像は SDK の正式な画像入力（function tool の出力の `ToolOutputImage` → `input_image`）で、
  同じ呼び出しの次のモデルの入力に届く。画像の参照（image_id・文書の版・page・bbox）も同じ入力の
  文字の結果にある。画像に対応しないモデルには添付せず、読めなかったことを結果に書く。
- 画像のデータ（base64）は Run の step・イベント・ログ・承認待ちで保存する状態に残さない。
- 重複・枚数の上限・入力の予算で外した画像は、ID と理由・読み直しの手の文に置き換える。

MCP は `httpx.MockTransport` の合成のサーバー、モデルは SDK の `ScriptedModel`、保存先は memory。
画像は合成のバイト列で、外部への通信はしない。業務の内容は含まない。
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import struct
from collections.abc import Iterator
from typing import Any

import anyio
import httpx
import pytest
from agents import ToolOutputImage, ToolOutputText
from agents.testing import ScriptedModel, assistant_message, function_call
from pr_backend_core.mcp import McpToolResult, tool_result
from pytest import LogCaptureFixture, MonkeyPatch

import app.features.agent.tools as tools_module
from app.features.agent import builtin_runtime
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.config import runtime_config_store
from app.features.agent.context_window import ContextBudget, ContextLimits, compact_items
from app.features.agent.runtime import (
    AgentProfile,
    ApprovalDecisionRequest,
    RunCreateRequest,
    RunStatus,
    runtime_repository,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.tool_media import (
    CONTENT_IMAGES_KEY,
    IMAGE_INPUT_NOTE_KEY,
    IMAGE_REMOVED_ON_SAVE_NOTE,
    MAX_IMAGE_BYTES,
    MAX_IMAGES_IN_INPUT,
    MAX_IMAGES_PER_RESULT,
    ToolImage,
    ToolOutputWithImages,
    estimate_image_tokens,
    model_tool_output,
    plan_image_delivery,
    strip_image_data,
)
from app.features.agent.tools import (
    ToolCall,
    ToolDefinition,
    ToolInvocationContext,
    ToolPermissionLevel,
    ToolPolicy,
    parse_mcp_tool_result,
    tool_registry,
    tool_result_images,
)

USER_UUID = "11111111-2222-3333-4444-555555552650"
SERVER_ID = "test2650"
FIGURE_TOOL = "read_figure"
FUNCTION = f"{SERVER_ID}__{FIGURE_TOOL}"
SKILL_ID = "test2650-skill"
AGENT_ID = "test2650-agent"
WRITE = "test2650_write"


def _png(width: int, height: int, marker: bytes) -> bytes:
    """大きさを読める PNG のヘッダーと、画像ごとに違う合成のバイト列。"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
        + marker
    )


IMAGE = _png(640, 480, b"synthetic-figure-2650")
IMAGE_B64 = base64.b64encode(IMAGE).decode("ascii")
STRUCTURED: dict[str, Any] = {
    "evidence_id": "chunk-a",
    "document_id": "doc-a",
    "chunk_id": "chunk-a",
    "chunk_set_id": "set-v2",
    "evidence_type": "figure",
    "text": "図の文字の説明（合成）",
    "image_ref": {
        "document_id": "doc-a",
        "chunk_id": "chunk-a",
        "chunk_set_id": "set-v2",
        "page": 3,
        "bbox": [0.1, 0.2, 0.6, 0.7],
        "bbox_unit": "ratio",
    },
    "image": {
        "mime_type": "image/png",
        "width": 640,
        "height": 480,
        "byte_size": len(IMAGE),
        "sha256": ToolImage(data=IMAGE, mime_type="image/png").sha256,
        "content_index": 1,
    },
}
EXTRA_TEXT = "構造化データに無い説明文（合成）"


def _image_block(data: str = IMAGE_B64, **extra: Any) -> dict[str, Any]:
    return {"type": "image", "data": data, "mimeType": "image/png", **extra}


def _compat_text(structured: dict[str, Any]) -> dict[str, Any]:
    return {"type": "text", "text": json.dumps(structured, ensure_ascii=False)}


# ---- MCP の結果の受け取り ---------------------------------------------------------------------


def _parse(result: dict[str, Any]) -> ToolOutputWithImages:
    return parse_mcp_tool_result(result, server_id=SERVER_ID, tool_name=FIGURE_TOOL)


def test_text_only_result_is_kept_as_content() -> None:
    parsed = _parse({"content": [{"type": "text", "text": "本文（合成）"}]})

    assert parsed.output == {"content": "本文（合成）"}
    assert parsed.images == ()


def test_image_only_result_keeps_the_image_and_no_raw_data_in_text() -> None:
    parsed = _parse({"content": [_image_block()]})

    [image] = parsed.images
    assert image.data == IMAGE
    assert parsed.output[CONTENT_IMAGES_KEY] == [image.reference()]
    assert IMAGE_B64 not in json.dumps(parsed.output)
    # 従来の「結果をそのまま」の扱いで base64 を文字の結果に入れない。
    assert "result" not in parsed.output


def test_text_and_image_without_structured_content() -> None:
    parsed = _parse({"content": [{"type": "text", "text": EXTRA_TEXT}, _image_block()]})

    assert parsed.output["content"] == EXTRA_TEXT
    assert len(parsed.images) == 1
    assert parsed.output[CONTENT_IMAGES_KEY][0]["source"]["content_index"] == 1


def test_structured_text_and_image_keep_all_parts_without_duplicating_json() -> None:
    parsed = _parse(
        {
            "content": [
                _compat_text(STRUCTURED),
                _image_block(),
                {"type": "text", "text": EXTRA_TEXT},
            ],
            "structuredContent": STRUCTURED,
        }
    )

    output = parsed.output
    # 構造化データはそのまま、互換用の text（同じ JSON）は入れず、固有の説明文は残す。
    assert {key: output[key] for key in STRUCTURED} == STRUCTURED
    assert output["content_text"] == EXTRA_TEXT
    assert json.dumps(STRUCTURED, ensure_ascii=False) not in json.dumps(output, ensure_ascii=False)
    [image] = parsed.images
    # 画像の参照を、文書の版・page・bbox と結び付ける（構造化データの sha256 と同じ画像だけ）。
    [ref] = output[CONTENT_IMAGES_KEY]
    assert ref["image_id"] == f"sha256:{STRUCTURED['image']['sha256']}"
    assert ref["width"] == 640 and ref["height"] == 480
    assert ref["source"]["document_id"] == "doc-a"
    assert ref["source"]["chunk_set_id"] == "set-v2"
    assert ref["source"]["region"]["page"] == 3
    assert ref["source"]["region"]["bbox"] == [0.1, 0.2, 0.6, 0.7]
    assert image.source == ref["source"]


def test_platform_mcp_result_with_image_is_received_whole() -> None:
    """3 製品の MCP の結果の形（`pr_backend_core.mcp.tool_result` の `McpToolResult`）との契約。"""
    result = tool_result(McpToolResult(output=STRUCTURED, content=(_image_block(),)))

    parsed = _parse(result)

    assert {key: parsed.output[key] for key in STRUCTURED} == STRUCTURED
    # 結果の text は構造化データの直列化なので、二重に入れない。
    assert "content_text" not in parsed.output
    [image] = parsed.images
    assert image.data == IMAGE
    assert image.source["content_index"] == STRUCTURED["image"]["content_index"]
    assert image.source["region"] == STRUCTURED["image_ref"]


def test_image_with_a_different_hash_does_not_take_the_structured_region() -> None:
    other = _png(10, 10, b"other")
    parsed = _parse(
        {
            "content": [_image_block(base64.b64encode(other).decode("ascii"))],
            "structuredContent": STRUCTURED,
        }
    )

    [ref] = parsed.output[CONTENT_IMAGES_KEY]
    assert "region" not in ref["source"]
    assert "document_id" not in ref["source"]


@pytest.mark.parametrize(
    ("block", "status"),
    [
        (_image_block(annotations={"audience": ["user"]}), "user_only"),
        ({"type": "image", "data": IMAGE_B64, "mimeType": "image/svg+xml"}, "unsupported_type"),
        (_image_block("***not-base64***"), "invalid"),
        (
            _image_block(base64.b64encode(b"x" * (MAX_IMAGE_BYTES + 10)).decode("ascii")),
            "too_large",
        ),
    ],
    ids=["user-only", "svg", "invalid", "too-large"],
)
def test_unusable_images_are_not_sent_and_say_why(block: dict[str, Any], status: str) -> None:
    parsed = _parse({"content": [block]})

    assert parsed.images == ()
    [ref] = parsed.output[CONTENT_IMAGES_KEY]
    assert ref["model_input"] == status
    assert "data" not in ref


def test_other_content_kinds_are_listed_but_not_sent() -> None:
    parsed = _parse(
        {
            "content": [
                {"type": "text", "text": "本文"},
                {"type": "audio", "data": "AAAA", "mimeType": "audio/wav"},
                {"type": "resource_link", "uri": "file:///x"},
            ]
        }
    )

    assert parsed.output["content_omitted"] == [
        {"type": "audio", "count": 1},
        {"type": "resource_link", "count": 1},
    ]
    assert "AAAA" not in json.dumps(parsed.output)


# ---- ツールの境界・届け方 -----------------------------------------------------------------------


def test_registry_keeps_images_out_of_the_serialized_result() -> None:
    name = "test2650_generic"
    image = ToolImage(data=IMAGE, mime_type="image/png", source={"kind": "attachment"})

    def handle(_arguments: dict[str, Any], _context: ToolInvocationContext) -> Any:
        return ToolOutputWithImages(
            output={"ok": True, CONTENT_IMAGES_KEY: [image.reference()]}, images=(image,)
        )

    tool_registry.register(
        ToolDefinition(
            name=name,
            description="合成の画像のツール",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            permission_level=ToolPermissionLevel.READ,
        ),
        handle,
    )
    try:
        result = tool_registry.invoke(
            ToolCall(name=name, arguments={}), policy=ToolPolicy(), context=ToolInvocationContext()
        )
    finally:
        tool_registry._definitions.pop(name, None)  # noqa: SLF001
        tool_registry._handlers.pop(name, None)  # noqa: SLF001

    assert result.success is True
    assert tool_result_images(result) == (image,)
    # 複製でも画像は保たれ、直列化には出ない。
    assert tool_result_images(result.model_copy(update={"output": {"ok": True}})) == (image,)
    assert IMAGE_B64 not in result.model_dump_json()
    assert result.output is not None and result.output["ok"] is True


def _images(count: int) -> list[ToolImage]:
    return [
        ToolImage(data=_png(100, 100, f"img-{index}".encode()), mime_type="image/png")
        for index in range(count)
    ]


def test_delivery_attaches_up_to_the_per_result_limit() -> None:
    images = _images(MAX_IMAGES_PER_RESULT + 1)
    delivery = plan_image_delivery(
        {CONTENT_IMAGES_KEY: [image.reference() for image in images]}, images, image_input=True
    )

    assert len(delivery.attached) == MAX_IMAGES_PER_RESULT
    statuses = [ref["model_input"] for ref in delivery.output[CONTENT_IMAGES_KEY]]
    assert statuses == ["attached"] * MAX_IMAGES_PER_RESULT + ["over_limit"]
    assert str(MAX_IMAGES_PER_RESULT) in delivery.output[IMAGE_INPUT_NOTE_KEY]
    assert delivery.summary["estimated_tokens"] == sum(
        image.estimated_tokens for image in images[:MAX_IMAGES_PER_RESULT]
    )


def test_delivery_to_a_model_without_image_input_says_the_image_was_not_read() -> None:
    [image] = _images(1)
    delivery = plan_image_delivery(
        {CONTENT_IMAGES_KEY: [image.reference()]}, [image], image_input=False
    )

    assert delivery.attached == ()
    assert delivery.output[CONTENT_IMAGES_KEY][0]["model_input"] == "unsupported_model"
    assert "画像を読めていません" in delivery.output[IMAGE_INPUT_NOTE_KEY]


def test_model_tool_output_uses_the_sdk_image_output() -> None:
    [image] = _images(1)

    assert model_tool_output("{}", []) == "{}"
    output = model_tool_output("{}", [image])
    assert isinstance(output, list)
    text, picture = output
    assert isinstance(text, ToolOutputText) and text.text == "{}"
    assert isinstance(picture, ToolOutputImage)
    assert picture.image_url == image.data_url()


def test_image_token_estimate_follows_the_image_size() -> None:
    small = estimate_image_tokens(256, 256)
    large = estimate_image_tokens(2048, 4096)
    assert small < large
    assert estimate_image_tokens(None, None) >= estimate_image_tokens(768, 768)


def test_saved_state_has_no_image_data() -> None:
    [image] = _images(1)
    state = json.dumps(
        {
            "items": [
                {
                    "type": "function_call_output",
                    "output": [
                        {"type": "input_text", "text": "{}"},
                        {"type": "input_image", "image_url": image.data_url()},
                    ],
                },
                {"raw": {"type": "image", "image_url": image.data_url(), "detail": "auto"}},
                {"type": "input_image", "image_url": "https://example.test/x.png"},
            ]
        }
    )

    stripped, removed = strip_image_data(state)

    assert removed == 2
    assert image.data_url() not in stripped
    assert stripped.count(IMAGE_REMOVED_ON_SAVE_NOTE) == 2
    # data URL でない画像（URL）はそのまま。
    assert "https://example.test/x.png" in stripped
    assert strip_image_data('{"a": 1}') == ('{"a": 1}', 0)


# ---- 入力の管理（重複・枚数・予算） ------------------------------------------------------------


def _call(call_id: str, name: str = FUNCTION) -> dict[str, Any]:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}


def _output(call_id: str, text: str, *images: ToolImage) -> dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": [
            {"type": "input_text", "text": text},
            *({"type": "input_image", "image_url": image.data_url()} for image in images),
        ],
    }


def _budget(max_input_tokens: int = 1_000_000) -> ContextBudget:
    return ContextBudget(
        ContextLimits(model_id="m", max_input_tokens=max_input_tokens, max_output_tokens=1),
        margin_ratio=0.0,
    )


def _attached(items: list[Any]) -> list[str]:
    return [
        part["image_url"]
        for item in items
        if isinstance(item, dict) and item.get("type") == "function_call_output"
        for part in item["output"]
        if isinstance(part, dict) and part.get("type") == "input_image"
    ]


def test_duplicate_image_is_attached_once_and_keeps_the_reference() -> None:
    [image] = _images(1)
    items = [
        {"role": "user", "content": "質問"},
        _call("c1"),
        _output("c1", json.dumps({"n": 1}), image),
        _call("c2"),
        _output("c2", json.dumps({"n": 2}), image),
    ]

    result = compact_items(items, budget=_budget(), fixed_text="")

    assert _attached(result.items) == [image.data_url()]
    # 最新の結果に残し、前の結果は ID を書いた文にする。
    assert _attached([result.items[4]]) == [image.data_url()]
    note = result.items[2]["output"][1]["text"]
    assert image.image_id in note
    [record] = result.outputs
    assert record.call_id == "c1" and record.image_reason == "duplicate"
    assert result.images == 1
    # 元の入力は変えない。
    assert _attached(items) == [image.data_url(), image.data_url()]


def test_images_in_input_are_limited_from_the_newest_results() -> None:
    images = _images(MAX_IMAGES_IN_INPUT + 2)
    items: list[Any] = [{"role": "user", "content": "質問"}]
    for index, image in enumerate(images):
        items += [_call(f"c{index}"), _output(f"c{index}", "{}", image)]

    result = compact_items(items, budget=_budget(), fixed_text="")

    attached = _attached(result.items)
    assert attached == [image.data_url() for image in images[2:]]
    assert {record.image_reason for record in result.outputs} == {"image_limit"}
    assert "rag_read_source" not in json.dumps(result.items, ensure_ascii=False)


def test_budget_removes_older_images_before_shrinking_text_and_keeps_the_latest() -> None:
    old, new = _images(2)
    text = json.dumps({"document_id": "doc-a", "chunk_id": "c", "text": "本文" * 20})
    items = [
        {"role": "user", "content": "質問"},
        _call("c1", "rag__rag_read_source"),
        _output("c1", text, old),
        _call("c2", "rag__rag_read_source"),
        _output("c2", text, new),
    ]
    full = compact_items(items, budget=_budget(), fixed_text="")
    image_tokens = new.estimated_tokens
    # 画像 1 枚と文字（と外した画像の案内）は収まるが、2 枚は収まらない予算。
    budget = _budget(full.tokens_before - image_tokens + 150)

    result = compact_items(
        items,
        budget=budget,
        fixed_text="",
        read_tool_for=lambda name: name if name.endswith("rag_read_source") else None,
    )

    assert result.fits
    assert _attached(result.items) == [new.data_url()]
    # 文字は縮めていない（画像を先に外した）。
    assert result.items[2]["output"][0]["text"] == text
    note = result.items[2]["output"][1]["text"]
    assert old.image_id in note and "rag__rag_read_source" in note
    [record] = result.outputs
    assert record.level == 0 and record.images_removed == 1 and record.image_reason == "budget"
    assert result.image_tokens == image_tokens


def test_latest_image_is_removed_only_when_text_alone_does_not_fit() -> None:
    [image] = _images(1)
    items = [
        {"role": "user", "content": "質問"},
        _call("c1"),
        _output("c1", "{}", image),
    ]
    tiny = _budget(60)

    result = compact_items(items, budget=tiny, fixed_text="")

    assert _attached(result.items) == []
    assert result.outputs[0].image_reason == "budget"
    assert result.outputs[0].latest is True


# ---- Run の通し（MCP → ツール → SDK のモデルの入力） ------------------------------------------


class _FigureMcp:
    """合成の MCP サーバー（構造化データ + 互換用の text + 画像 + 説明文を返す）。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.error: dict[str, Any] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        method = body["method"]
        if method == "initialize":
            return self._result(body, {"capabilities": {}})
        if method == "tools/list":
            return self._result(
                body,
                {
                    "tools": [
                        {
                            "name": FIGURE_TOOL,
                            "description": "合成の図を読む",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"chunk_id": {"type": "string"}},
                                "required": ["chunk_id"],
                                "additionalProperties": False,
                            },
                            "annotations": {"readOnlyHint": True},
                        }
                    ]
                },
            )
        self.calls.append(body["params"])
        if self.error is not None:
            return self._result(body, self.error)
        return self._result(
            body,
            {
                "content": [
                    _compat_text(STRUCTURED),
                    _image_block(),
                    {"type": "text", "text": EXTRA_TEXT},
                ],
                "structuredContent": STRUCTURED,
                "isError": False,
            },
        )

    @staticmethod
    def _result(body: dict[str, Any], result: dict[str, Any]) -> httpx.Response:
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.fixture
def figure_mcp(monkeypatch: MonkeyPatch) -> Iterator[_FigureMcp]:
    server = _FigureMcp()
    real_client = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(server.handle), timeout=kwargs["timeout"])

    monkeypatch.setattr("app.features.agent.tools.httpx.Client", client)
    monkeypatch.setattr(tools_module, "_retry_sleep", lambda _seconds: None)
    runtime_config_store.upsert_mcp_server(
        SERVER_ID,
        base_url=f"https://{SERVER_ID}.example.test/mcp",
        auth_mode="none",
        trust_tool_annotations=True,
    )

    def write(_arguments: dict[str, Any], _context: ToolInvocationContext) -> dict[str, Any]:
        return {"written": True}

    tool_registry.register(
        ToolDefinition(
            name=WRITE,
            description="合成の書き込みのツール",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema={"type": "object"},
            permission_level=ToolPermissionLevel.WRITE,
            side_effects=True,
        ),
        write,
    )
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name="合成の図の Skill",
            instructions="図を読む。",
            mcp_requirements=[
                SkillMcpRequirement(server_id=SERVER_ID, tool_names=[FIGURE_TOOL]),
                SkillMcpRequirement(server_id="control-plane", tool_names=[WRITE]),
            ],
        )
    )
    runtime_repository.create_agent(
        AgentProfile(id=AGENT_ID, name="合成の図の業務 Agent", skill_ids=[SKILL_ID])
    )
    try:
        yield server
    finally:
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id for run_id, run in repository._runs.items() if run.agent_id == AGENT_ID
            ]:
                run = repository._runs.pop(run_id)
                for approval in run.approvals:
                    repository._approvals.pop(approval.id, None)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(AGENT_ID)
        with contextlib.suppress(KeyError, ValueError):
            skill_registry.remove(SKILL_ID)
        with contextlib.suppress(KeyError):
            runtime_config_store.remove_mcp_server(SERVER_ID)
        tool_registry._definitions.pop(WRITE, None)  # noqa: SLF001
        tool_registry._handlers.pop(WRITE, None)  # noqa: SLF001


def _script(monkeypatch: MonkeyPatch, *steps: Any, vision: bool = True) -> ScriptedModel:
    model = ScriptedModel(list(steps))
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id=model_id or "test-model",
            endpoint="https://oci.example",
            project_ocid="",
            api_key="k",
            vision_enabled=vision,
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _run(goal: str = "図の内容を確かめて") -> str:
    return runtime_repository.create_builtin_run(
        RunCreateRequest(goal=goal, agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    ).id


def _tool_output(model_input: Any, call_id: str) -> Any:
    items = model_input if isinstance(model_input, list) else []
    for item in items:
        data = item if isinstance(item, dict) else item.model_dump()
        if data.get("type") == "function_call_output" and data.get("call_id") == call_id:
            return data["output"]
    raise AssertionError(f"{call_id} の結果がモデルの入力にありません")


def _log_text(caplog: LogCaptureFixture) -> str:
    return "\n".join(
        f"{record.getMessage()} {json.dumps(record.__dict__, ensure_ascii=False, default=str)}"
        for record in caplog.records
    )


def test_structured_text_and_image_reach_the_same_model_input(
    monkeypatch: MonkeyPatch, caplog: LogCaptureFixture, figure_mcp: _FigureMcp
) -> None:
    model = _script(
        monkeypatch,
        [function_call(FUNCTION, {"chunk_id": "chunk-a"}, call_id="call-fig")],
        [assistant_message("図を確かめました。")],
    )
    run_id = _run()
    caplog.set_level(logging.DEBUG)

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    # trace の `_meta`（#3226）を除いた呼び出しの中身。
    assert [
        {key: value for key, value in call.items() if key != "_meta"} for call in figure_mcp.calls
    ] == [{"name": FIGURE_TOOL, "arguments": {"chunk_id": "chunk-a"}}]
    output = _tool_output(model.calls[1].input, "call-fig")
    # SDK の画像の出力（Responses API の input_image）で、文字の結果と同じ呼び出しに届く。
    assert isinstance(output, list)
    [text_part, image_part] = output
    assert text_part["type"] == "input_text"
    assert image_part == {
        "type": "input_image",
        "image_url": f"data:image/png;base64,{IMAGE_B64}",
        "detail": "auto",
    }
    text = json.loads(text_part["text"])
    # 構造化データと固有の説明文は文字の結果に 1 回だけ、互換用の text は入れない。
    assert text["text"] == STRUCTURED["text"]
    assert text["content_text"] == EXTRA_TEXT
    assert text_part["text"].count(STRUCTURED["text"]) == 1
    # 画像の参照（image_id・文書の版・page・bbox）が添付した画像に対応する。
    [ref] = text[CONTENT_IMAGES_KEY]
    assert ref["model_input"] == "attached"
    assert ref["image_id"] == f"sha256:{STRUCTURED['image']['sha256']}"
    assert ref["source"]["chunk_set_id"] == "set-v2"
    assert ref["source"]["region"]["page"] == 3
    assert IMAGE_B64 not in text_part["text"]
    # base64 は Run の step・イベント・ログに残さない。参照と見積もりは step に残す。
    assert IMAGE_B64 not in run.model_dump_json()
    assert IMAGE_B64 not in _log_text(caplog)
    [step] = run.steps
    assert step.tool_result is not None
    assert step.tool_result.output is not None
    assert step.tool_result.output[CONTENT_IMAGES_KEY][0]["image_id"] == ref["image_id"]
    image_input = step.tool_result.audit_metadata["image_input"]
    assert image_input["attached"] == 1
    assert image_input["estimated_tokens"] == estimate_image_tokens(640, 480)


def test_model_without_image_input_is_told_the_image_was_not_read(
    monkeypatch: MonkeyPatch, figure_mcp: _FigureMcp
) -> None:
    del figure_mcp
    model = _script(
        monkeypatch,
        [function_call(FUNCTION, {"chunk_id": "chunk-a"}, call_id="call-fig")],
        [assistant_message("図の画像は確認できませんでした。")],
        vision=False,
    )
    run_id = _run()

    anyio.run(builtin_runtime.execute_run, run_id)

    assert runtime_repository.get_run(run_id).status == RunStatus.COMPLETED
    output = _tool_output(model.calls[1].input, "call-fig")
    # 画像を添付せず、読めなかったことを文字の結果に書く。
    assert isinstance(output, str)
    text = json.loads(output)
    assert text[CONTENT_IMAGES_KEY][0]["model_input"] == "unsupported_model"
    assert "画像を読めていません" in text[IMAGE_INPUT_NOTE_KEY]
    assert IMAGE_B64 not in output


@pytest.mark.parametrize(
    "error",
    [
        {
            "content": [{"type": "text", "text": "権限がありません（合成）"}],
            "structuredContent": {
                "error_code": "forbidden",
                "message": "権限がありません（合成）",
                "status": 403,
            },
            "isError": True,
        },
        {
            # 文書の版が変わった後の取り直し（現在の版へ黙って差し替えない）。
            "content": [{"type": "text", "text": "古い版の根拠です（合成）"}],
            "structuredContent": {
                "error_code": "source_stale",
                "message": "古い版の根拠です（合成）",
            },
            "isError": True,
        },
        {"content": [_image_block()], "isError": True},
    ],
    ids=["permission-denied", "stale-version", "error-with-image"],
)
def test_error_results_are_unchanged_and_carry_no_image(
    monkeypatch: MonkeyPatch, figure_mcp: _FigureMcp, error: dict[str, Any]
) -> None:
    figure_mcp.error = error
    model = _script(
        monkeypatch,
        [function_call(FUNCTION, {"chunk_id": "chunk-a"}, call_id="call-fig")],
        [assistant_message("読めませんでした。")],
    )
    run_id = _run()

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    [step] = run.steps
    assert step.tool_result is not None and step.tool_result.success is False
    assert step.tool_result.error_code == "mcp.tool_error"
    output = _tool_output(model.calls[1].input, "call-fig")
    assert isinstance(output, str)
    assert IMAGE_B64 not in output
    assert IMAGE_B64 not in run.model_dump_json()


def test_approval_state_drops_image_data_and_resume_says_to_read_again(
    monkeypatch: MonkeyPatch, figure_mcp: _FigureMcp
) -> None:
    del figure_mcp
    model = _script(
        monkeypatch,
        [function_call(FUNCTION, {"chunk_id": "chunk-a"}, call_id="call-fig")],
        [function_call(WRITE, {}, call_id="call-w")],
        [assistant_message("登録しました。")],
    )
    run_id = _run("図を確かめて登録して")

    anyio.run(builtin_runtime.execute_run, run_id)

    waiting = runtime_repository.get_run(run_id)
    assert waiting.status == RunStatus.WAITING_APPROVAL
    # 承認の前のモデルの呼び出しには画像が届いている。
    assert isinstance(_tool_output(model.calls[1].input, "call-fig"), list)
    # 保存する状態（Run の metadata）に画像のデータを残さない。
    assert IMAGE_B64 not in waiting.model_dump_json()
    assert IMAGE_REMOVED_ON_SAVE_NOTE in json.dumps(waiting.metadata, ensure_ascii=False)
    [approval] = waiting.approvals
    runtime_repository.decide_approval(
        approval.id, ApprovalDecisionRequest(approved=True, decided_by="approver1")
    )

    anyio.run(builtin_runtime.resume_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED
    resumed = _tool_output(model.calls[2].input, "call-fig")
    # 再開の入力に画像は無く、読み直しの案内が代わりにある（画像の参照は文字の結果に残る）。
    assert "input_image" not in json.dumps(resumed, ensure_ascii=False)
    assert IMAGE_REMOVED_ON_SAVE_NOTE in json.dumps(resumed, ensure_ascii=False)
    assert STRUCTURED["image"]["sha256"] in json.dumps(resumed, ensure_ascii=False)
    assert IMAGE_B64 not in run.model_dump_json()
