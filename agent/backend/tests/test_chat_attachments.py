"""チャットの添付（会話の期間だけ使う作業ファイル。#2517）。

- API: 添付して送ると、Run に添付の名前が残り、添付が会話に結び付く。ほかの利用者の添付・会話は
  404。会話を消すと添付も消える。
- Runtime: 会話に添付がある Run にだけ、添付を読むツールと添付の索引を渡す。モデルが添付を読んで
  答えた回答は、資料の根拠が無くても「確かめていない」の注記を付けない。

保存は platform のメモリの store（テストごとに作る）、モデルは SDK の `ScriptedModel`。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from pr_system_settings.chat_attachments import (
    ChatAttachmentService,
    InMemoryChatAttachmentStore,
)
from pytest import MonkeyPatch
from security_support import client, enable_production_auth, login

from app.features.agent import builtin_runtime, router
from app.features.agent import chat_attachments as attachment_module
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.chat_attachments import set_chat_attachment_service
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunState,
    RunStatus,
    runtime_repository,
)
from app.security.permissions import MENU_CHAT, RUNS_OPERATE, RUNS_VIEW
from app.security.service import set_security_service

CSV = "コード,名前,金額\nA-1,品目甲,1200\nA-2,品目乙,3400\n".encode()


@dataclass
class Setup:
    agent_id: str
    alice: str
    bob: str
    alice_login: str
    bob_login: str
    service: ChatAttachmentService


@dataclass(frozen=True)
class _Principal:
    user_uuid: str
    is_system_admin: bool = False


def _complete(run: RunState) -> None:
    if runtime_repository.begin_builtin_run(run.id) is not None:
        runtime_repository.complete_builtin_run(run.id, f"回答: {run.goal}")


@pytest.fixture
def setup(monkeypatch: MonkeyPatch) -> Iterator[Setup]:
    auth = enable_production_auth(monkeypatch)
    suffix = uuid4().hex[:8]
    agent_id = f"agent2517-{suffix}"
    runtime_repository.create_agent(
        AgentProfile(id=agent_id, name="添付の Agent", instructions="答える。")
    )
    permissions = [MENU_CHAT, RUNS_VIEW, RUNS_OPERATE]
    alice = auth.user_with_permissions(f"alice-{suffix}", permissions, agent_ids=[agent_id])
    bob = auth.user_with_permissions(f"bob-{suffix}", permissions, agent_ids=[agent_id])
    service = ChatAttachmentService(
        InMemoryChatAttachmentStore(), product="agent", settings=lambda: SimpleNamespace()
    )
    set_chat_attachment_service(service)
    monkeypatch.setattr(router, "_schedule_builtin_run", _complete)
    try:
        yield Setup(
            agent_id=agent_id,
            alice=alice.user_uuid,
            bob=bob.user_uuid,
            alice_login=alice.login_user_id,
            bob_login=bob.login_user_id,
            service=service,
        )
    finally:
        set_chat_attachment_service(None)
        set_security_service(None)
        repository: Any = runtime_repository
        with repository._lock:  # noqa: SLF001 - テストの後始末
            for run_id in [
                run_id for run_id, run in repository._runs.items() if run.agent_id == agent_id
            ]:
                repository._runs.pop(run_id)
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(agent_id)


def _upload(headers: dict[str, str], name: str = "a.csv", data: bytes = CSV, **form: str) -> Any:
    return client.post(
        "/api/chat-attachments",
        files={"file": (name, data, "text/csv")},
        data=form,
        headers=headers,
    )


def test_attach_and_send_binds_attachment_to_thread(setup: Setup) -> None:
    alice = login(setup.alice_login)
    uploaded = _upload(alice)
    assert uploaded.status_code == 200, uploaded.text
    item = uploaded.json()["data"]
    assert (item["kind"], item["conversation_id"]) == ("csv", None)
    pending = client.get("/api/chat-attachments", headers=alice).json()["data"]
    assert [i["attachment_id"] for i in pending["items"]] == [item["attachment_id"]]

    sent = client.post(
        "/api/runs",
        json={
            "goal": "添付の金額の合計は？",
            "agent_id": setup.agent_id,
            "attachment_ids": [item["attachment_id"]],
        },
        headers=alice,
    )
    assert sent.status_code == 200, sent.text
    run = sent.json()["data"]
    assert run["attachments"] == [
        {
            "attachment_id": item["attachment_id"],
            "file_name": "a.csv",
            "kind": "csv",
            "size_bytes": len(CSV),
            "content_sha256": hashlib.sha256(CSV).hexdigest(),
        }
    ]
    thread_id = run["thread_id"]
    in_thread = client.get(
        "/api/chat-attachments", params={"conversation_id": thread_id}, headers=alice
    ).json()["data"]["items"]
    assert [i["attachment_id"] for i in in_thread] == [item["attachment_id"]]
    assert client.get("/api/chat-attachments", headers=alice).json()["data"]["items"] == []
    # 会話の表示に添付が残る。
    thread = client.get(f"/api/threads/{thread_id}", headers=alice).json()["data"]
    assert thread["runs"][0]["attachments"][0]["file_name"] == "a.csv"
    # 会話の続きの添付も、アップロードでは会話に結び付けない（送信で結び付ける。#2652）。
    second = _upload(alice, "b.csv", conversation_id=thread_id).json()["data"]
    assert (second["conversation_id"], second["parse_status"]) == (None, "pending")
    in_thread = client.get(
        "/api/chat-attachments", params={"conversation_id": thread_id}, headers=alice
    ).json()["data"]["items"]
    assert [i["attachment_id"] for i in in_thread] == [item["attachment_id"]]


def test_other_users_cannot_use_attachment_or_thread(setup: Setup) -> None:
    alice, bob = login(setup.alice_login), login(setup.bob_login)
    item = _upload(alice).json()["data"]
    sent = client.post(
        "/api/runs",
        json={
            "goal": "質問",
            "agent_id": setup.agent_id,
            "attachment_ids": [item["attachment_id"]],
        },
        headers=bob,
    )
    assert sent.status_code == 404
    assert (
        client.delete(f"/api/chat-attachments/{item['attachment_id']}", headers=bob).status_code
        == 404
    )
    own = client.post(
        "/api/runs", json={"goal": "質問", "agent_id": setup.agent_id}, headers=alice
    ).json()["data"]
    assert _upload(bob, conversation_id=own["thread_id"]).status_code == 404
    assert (
        client.get(
            "/api/chat-attachments", params={"conversation_id": own["thread_id"]}, headers=bob
        ).status_code
        == 404
    )


def test_system_admin_lists_but_cannot_attach_to_others_thread(setup: Setup) -> None:
    """system_admin は他の利用者の会話の添付を見られるが、添付は足せない（403。#3244）。"""
    from pr_system_settings.auth.visibility import OTHERS_CONTENT_FORBIDDEN_MESSAGE
    from security_support import login_configured_admin

    alice, admin = login(setup.alice_login), login_configured_admin()
    own = client.post(
        "/api/runs", json={"goal": "質問", "agent_id": setup.agent_id}, headers=alice
    ).json()["data"]
    refused = _upload(admin, conversation_id=own["thread_id"])
    assert refused.status_code == 403, refused.text
    assert refused.json()["error_messages"] == [OTHERS_CONTENT_FORBIDDEN_MESSAGE]
    listed = client.get(
        "/api/chat-attachments", params={"conversation_id": own["thread_id"]}, headers=admin
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"]["items"] == []
    # 持ち主は今どおり会話に添付できる。
    assert _upload(alice, conversation_id=own["thread_id"]).status_code == 200


def test_deleting_thread_deletes_its_attachments(setup: Setup) -> None:
    alice = login(setup.alice_login)
    item = _upload(alice).json()["data"]
    run = client.post(
        "/api/runs",
        json={
            "goal": "質問",
            "agent_id": setup.agent_id,
            "attachment_ids": [item["attachment_id"]],
        },
        headers=alice,
    ).json()["data"]
    assert client.delete(f"/api/threads/{run['thread_id']}", headers=alice).status_code == 200
    assert setup.service.store.get(item["attachment_id"]) is None


def _script(monkeypatch: MonkeyPatch, *steps: Any, vision: bool = False) -> ScriptedModel:
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


def _run_with_attachment(
    setup: Setup, file_name: str = "a.csv", data: bytes = CSV
) -> tuple[str, str]:
    principal = _Principal(setup.alice)
    item = asyncio.run(setup.service.upload(principal, file_name=file_name, data=data))
    references = setup.service.prepare_attachments(
        principal, [item.attachment_id], conversation_id=None
    )
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="品目乙の金額は？", agent_id=setup.agent_id),
        created_by_user_uuid=setup.alice,
        attachments=references,
    )
    assert run.thread_id is not None
    setup.service.bind_references(
        references, conversation_id=run.thread_id, owner_user_uuid=setup.alice
    )
    return run.id, item.attachment_id


def test_run_reads_attachment_with_tools(setup: Setup, monkeypatch: MonkeyPatch) -> None:
    run_id, attachment_id = _run_with_attachment(setup)
    _script(
        monkeypatch,
        [
            function_call(
                "chat_attachment_read_rows",
                {"attachment_id": attachment_id, "sheet": "a", "start_row": 1, "limit": 10},
                call_id="call-1",
            )
        ],
        [assistant_message("品目乙の金額は 3400 です。")],
    )

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    [step] = run.steps
    assert step.tool_call is not None and step.tool_call.name == "chat_attachment_read_rows"
    assert step.tool_result is not None and step.tool_result.success
    assert step.tool_result.output is not None
    assert step.tool_result.output["rows"][1]["values"] == {
        "コード": "A-2",
        "名前": "品目乙",
        "金額": "3400",
    }
    answer = next(item for item in run.artifacts if item.kind == "answer")
    assert answer.content["text"] == "品目乙の金額は 3400 です。"
    validation = next(item for item in run.artifacts if item.kind == "answer_validation")
    assert validation.content["reason"] == "attachment_evidence"


def test_run_parses_sent_attachment_on_first_read_and_reuses(
    setup: Setup, monkeypatch: MonkeyPatch
) -> None:
    """アップロードでは解析せず、Run のツールが初めて読むときに 1 回だけ解析する（#2652）。"""
    from pr_system_settings.chat_attachments import parsing

    parsed: list[str] = []
    original = parsing._parse_csv

    def counted(*args: Any) -> Any:
        parsed.append(args[0])
        return original(*args)

    monkeypatch.setattr(parsing, "_parse_csv", counted)
    unsent = asyncio.run(
        setup.service.upload(_Principal(setup.alice), file_name="unsent.csv", data=CSV)
    )
    run_id, attachment_id = _run_with_attachment(setup)
    assert parsed == []
    _script(
        monkeypatch,
        [function_call("chat_attachment_list", {}, call_id="call-1")],
        [
            function_call(
                "chat_attachment_read_rows",
                {"attachment_id": attachment_id, "sheet": "a", "start_row": 1, "limit": 10},
                call_id="call-2",
            )
        ],
        [assistant_message("品目乙の金額は 3400 です。")],
    )

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    listed = run.steps[0].tool_result
    assert listed is not None and listed.output is not None
    [item] = listed.output["attachments"]
    assert (item["attachment_id"], item["parse_status"]) == (attachment_id, "ready")
    assert unsent.attachment_id not in str(listed.output)
    assert parsed == ["a.csv"]
    # 同じ会話の後続の質問では、保存した結果を再利用する。
    assert setup.service.ensure_parsed(attachment_id).reused is True
    assert parsed == ["a.csv"]


def test_tools_and_index_only_for_runs_with_attachments(
    setup: Setup, monkeypatch: MonkeyPatch
) -> None:
    _script(monkeypatch)
    run_id, attachment_id = _run_with_attachment(setup)
    run = runtime_repository.get_run(run_id)
    sdk_agent = builtin_runtime.build_sdk_agent(
        run_id,
        name="a",
        instructions="答える。",
        skill_ids=[],
        model_id="",
        thread_id=run.thread_id,
    )
    names = {tool.name for tool in sdk_agent.tools}
    assert {
        "chat_attachment_list",
        "chat_attachment_read_section",
        "chat_attachment_read_rows",
        "chat_attachment_find",
    } <= names
    # 索引は種類と解析の状態だけ（解析はツールが初めて読むとき。#2652）。
    assert f"- a.csv（attachment_id={attachment_id}、種類=csv、未解析）" in str(
        sdk_agent.instructions
    )
    plain = builtin_runtime.build_sdk_agent(
        run_id,
        name="a",
        instructions="答える。",
        skill_ids=[],
        model_id="",
        thread_id="thread_" + "0" * 32,
    )
    assert not {tool.name for tool in plain.tools} & names
    assert "利用者が添付したファイル" not in str(plain.instructions)


def test_settings_default_disabled_and_admin_enables(monkeypatch: MonkeyPatch) -> None:
    """運用設定 › チャットの添付（#2517）。既定は無効で、添付は 403。有効にすると添付できる。"""
    from security_support import login_configured_admin

    enable_production_auth(monkeypatch)
    set_chat_attachment_service(None)
    try:
        admin = login_configured_admin()
        settings = client.get("/api/settings/chat-attachments", headers=admin).json()["data"]
        assert (settings["enabled"], settings["max_files"]) == (False, 10)
        limits = client.get("/api/chat-attachments", headers=admin).json()["data"]["limits"]
        assert limits["enabled"] is False
        refused = _upload(admin)
        assert refused.status_code == 403
        assert (
            refused.json()["error_code"] == "CHAT_ATTACHMENT_DISABLED"
            or "チャットの添付" in refused.text
        )

        saved = client.put("/api/settings/chat-attachments", json={"enabled": True}, headers=admin)
        assert saved.status_code == 200, saved.text
        assert saved.json()["data"]["enabled"] is True
        assert _upload(admin).status_code == 200
    finally:
        set_chat_attachment_service(None)
        set_security_service(None)


# ---- 画像の原本（#2652。ツールの結果の画像は #2650 の仕組みでモデルへ渡す） ----

# 4 × 3 px の PNG のヘッダー（中身は合成。モデルへは原本のバイト列をそのまま渡す）。
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + struct.pack(">II", 4, 3) + b"\x08\x02\x00\x00\x00"


def _function_output(model_input: Any, call_id: str) -> Any:
    for item in model_input if isinstance(model_input, list) else []:
        data = item if isinstance(item, dict) else item.model_dump()
        if data.get("type") == "function_call_output" and data.get("call_id") == call_id:
            return data["output"]
    raise AssertionError(f"{call_id} の結果がモデルの入力にありません")


def _view(attachment_id: str, focus: str, call_id: str) -> Any:
    return function_call(
        "chat_attachment_view_image",
        {"attachment_id": attachment_id, "focus": focus},
        call_id=call_id,
    )


def test_image_model_receives_the_original_for_each_question(
    setup: Setup, monkeypatch: MonkeyPatch
) -> None:
    """画像に対応するモデルには、観点ごとに原画像そのものを渡す（前の解釈を使い回さない）。"""
    vision_calls: list[str] = []
    monkeypatch.setattr(
        attachment_module, "_image_reader", lambda prompt="": vision_calls.append(prompt)
    )
    run_id, attachment_id = _run_with_attachment(setup, "a.png", PNG)
    model = _script(
        monkeypatch,
        [_view(attachment_id, "左上の数値", "call-1")],
        [_view(attachment_id, "凡例の色", "call-2")],
        [assistant_message("a.png の左上の数値と凡例を確かめました。")],
        vision=True,
    )

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    expected = f"data:image/png;base64,{base64.b64encode(PNG).decode()}"
    for index, call_id in ((1, "call-1"), (2, "call-2")):
        output = _function_output(model.calls[index].input, call_id)
        assert isinstance(output, list)
        assert [part["type"] for part in output] == ["input_text", "input_image"]
        assert output[1]["image_url"] == expected
    first = json.loads(_function_output(model.calls[1].input, "call-1")[0]["text"])
    second = json.loads(_function_output(model.calls[2].input, "call-2")[0]["text"])
    assert (first["focus"], second["focus"]) == ("左上の数値", "凡例の色")
    assert first["content_images"][0]["model_input"] == "attached"
    # 既定の Vision モデルは呼ばない（読むのは Run のモデル）。base64 は Run に残さない。
    assert vision_calls == []
    assert base64.b64encode(PNG).decode() not in run.model_dump_json()
    evidence = next(item for item in run.artifacts if item.kind == "answer_evidence")
    [image] = evidence.content["items"]
    assert image["content_kind"] == "image"
    assert image["image_reading"]["model_input"] == "attached"


def test_non_image_model_reads_with_default_vision_model_and_focus(
    setup: Setup, monkeypatch: MonkeyPatch
) -> None:
    """Run のモデルが画像に対応しなければ、観点を添えて既定の Vision モデルで読み、由来を残す。"""
    prompts: list[str] = []

    def reader(prompt: str = "") -> Any:
        async def read(data: bytes, media_type: str) -> str:
            prompts.append(prompt)
            assert data == PNG and media_type == "image/png"
            return "左上の数値は 42"

        return read

    monkeypatch.setattr(attachment_module, "_image_model_id", lambda: "vision-model-a")
    monkeypatch.setattr(attachment_module, "_image_reader", reader)
    run_id, attachment_id = _run_with_attachment(setup, "a.png", PNG)
    model = _script(
        monkeypatch,
        [_view(attachment_id, "左上の数値", "call-1")],
        [assistant_message("a.png の左上の数値は 42 です。")],
    )

    anyio.run(builtin_runtime.execute_run, run_id)

    run = runtime_repository.get_run(run_id)
    assert run.status == RunStatus.COMPLETED, run.events[-1].message
    output = json.loads(_function_output(model.calls[1].input, "call-1"))
    assert output["content"] == "左上の数値は 42"
    assert (output["content_source"], output["read_by"]) == ("vision_reading", "vision-model-a")
    assert len(prompts) == 1 and "観点: 左上の数値" in prompts[0]
    evidence = next(item for item in run.artifacts if item.kind == "answer_evidence")
    [image] = evidence.content["items"]
    assert image["image_reading"]["read_by"] == "vision-model-a"


def test_image_without_any_image_model_is_reported_as_not_read(
    setup: Setup, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(attachment_module, "_image_model_id", lambda: None)
    run_id, attachment_id = _run_with_attachment(setup, "a.png", PNG)
    model = _script(
        monkeypatch,
        [_view(attachment_id, "左上の数値", "call-1")],
        [assistant_message("画像は確認できませんでした。")],
    )

    anyio.run(builtin_runtime.execute_run, run_id)

    output = json.loads(_function_output(model.calls[1].input, "call-1"))
    assert output["content_images"][0]["model_input"] == "unsupported_model"
    assert "画像を読めていません" in output["image_input_note"]
    run = runtime_repository.get_run(run_id)
    evidence = [item for item in run.artifacts if item.kind == "answer_evidence"]
    assert all(not artifact.content.get("items") for artifact in evidence)


@pytest.mark.asyncio
async def test_image_reader_records_document_read_purpose_without_source_content(
    monkeypatch: MonkeyPatch,
) -> None:
    import openai
    from pr_backend_core.observability.tracing import operation_trace

    calls: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(output_text="合成の読み取り結果", usage=None)

    monkeypatch.setattr(attachment_module, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(attachment_module, "_image_model_id", lambda: "vision-model-a")
    monkeypatch.setattr(
        attachment_module,
        "enterprise_ai_connection_for_model",
        lambda *_: SimpleNamespace(
            base_url="https://model.example.test/v1", api_key="synthetic", project_ocid=None
        ),
    )
    monkeypatch.setattr(attachment_module, "enterprise_ai_reasoning", lambda *_: None)
    monkeypatch.setattr(attachment_module, "enterprise_ai_output_token_budget", lambda *_: 16)
    monkeypatch.setattr(builtin_runtime, "enterprise_ai_http_client", lambda *_: None)
    monkeypatch.setattr(
        openai, "AsyncOpenAI", lambda **_: SimpleNamespace(responses=SimpleNamespace(create=create))
    )
    reader = attachment_module._image_reader("合成の指示")
    assert reader is not None
    with operation_trace("agent.execute", operation_id="synthetic-image-purpose") as summary:
        assert await reader(b"synthetic-image", "image/png") == "合成の読み取り結果"
    chat = next(span for span in summary.spans if span.operation_name == "gen_ai.chat")
    assert chat.attributes["pr.gen_ai.purpose"] == "document.read"
    assert chat.attributes["gen_ai.request.model"] == "vision-model-a"
    assert len(calls) == 1 and calls[0]["store"] is False
    assert calls[0]["input"][0]["content"][0]["text"] == "合成の指示"
    assert "合成の指示" not in summary.model_dump_json()
    assert "synthetic-image" not in json.dumps(chat.attributes)
