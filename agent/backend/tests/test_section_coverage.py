"""回答に使った章節の未読の続き（#2492）の決定論テスト（合成の fixture。RAG はスタブ）。

`rag_read_section` が `section_complete=false` と `next_cursor` を返した章節を、続きを読まずに
手順の回答に使ったとき、答える前に 1 回だけ差し戻すこと、それでも読まなければ回答に読み切れて
いない章節を示し、最終の検証の成果物に主張の照合とは別の `coverage` として残すことを確かめる。
続きを読んだ・探索だけの章節・予算の上限・古い続きの位置・取り消した Run も確かめる。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from copy import deepcopy
from typing import Any

import anyio
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call
from mcp_support import DEFAULT_OUTPUTS, FakeProductMcp, McpToolError, fake_product_mcp
from pytest import MonkeyPatch

from app.features.agent import builtin_runtime
from app.features.agent.answer_validation import ANSWER_VALIDATION_KIND
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.runtime import (
    AgentProfile,
    RunCreateRequest,
    RunState,
    RunStep,
    runtime_repository,
)
from app.features.agent.section_coverage import (
    UNREAD_SECTIONS_HEADING,
    UNREAD_SECTIONS_RETRY_HINT,
    section_coverage,
    unread_sections_message,
    with_unread_sections_notice,
)
from app.features.agent.skills import AgentSkillDefinition, SkillMcpRequirement, skill_registry
from app.features.agent.support_task import BUDGET_EXCEEDED_CODE
from app.features.agent.tool_grounding import METADATA_KEY
from app.features.agent.tools import ToolCall, ToolResult
from app.settings import get_settings

SKILL_ID = "test2492-skill"
AGENT_ID = "test2492-agent"
USER_UUID = "11111111-2222-3333-4444-555555555592"
GOAL = "設定 A を変える手順を教えてください。"

PART_1 = "手順 1: 画面 A を開いて設定 A の項目を選びます。"
PART_2 = "手順 2: 画面 B で変更を保存し、反映を確かめます。"
OTHER = "参考: 別の画面 X には表示の切り替えの項目があります。"


def _section_output(
    section_id: str,
    text: str,
    *,
    title: str | None = None,
    chunk_id: str | None = None,
    next_cursor: str | None = None,
    document_id: str = "doc-a",
    parent_section_id: str | None = "sec-root",
    chunk_set_id: str = "cs-1",
) -> dict[str, Any]:
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_read_section"])
    chunk: dict[str, Any] = deepcopy(output["chunks"][0])
    chunk.update(chunk_id=chunk_id or f"{document_id}:{section_id}:{len(text)}", start=0)
    chunk["end"] = len(text)
    output.update(
        document_id=document_id,
        file_name="文書 A.pdf",
        chunk_set_id=chunk_set_id,
        text=text,
        chunks=[chunk],
        child_sections=[],
        section_complete=next_cursor is None,
        next_cursor=next_cursor,
    )
    output["section"] = {
        **output["section"],
        "section_id": section_id,
        "title": title or f"章節 {section_id}",
        "level": 2,
        "parent_section_id": parent_section_id,
        "page_start": 3,
        "page_end": 4,
    }
    return output


def _step(
    arguments: dict[str, Any],
    output: dict[str, Any] | None = None,
    *,
    error_code: str | None = None,
    details: dict[str, Any] | None = None,
) -> RunStep:
    success = output is not None
    return RunStep(
        run_id="run-2492",
        title="rag_read_section",
        status="completed" if success else "failed",
        tool_call=ToolCall(name="rag__rag_read_section", arguments=arguments),
        tool_result=ToolResult(
            name="rag__rag_read_section",
            success=success,
            output=output,
            error_code=error_code,
            error_details=details or {},
        ),
    )


# ---- 章節ごとの読み取りの状態（決定的） ------------------------------------------------------


def test_coverage_tracks_continuations_of_sections_used_in_the_answer() -> None:
    first = _step(
        {"document_id": "doc-a", "section_id": "sec-a"},
        _section_output("sec-a", PART_1, next_cursor="cur-a-2"),
    )
    answer = f"{PART_1}"
    coverage = section_coverage([first], answer, [GOAL])
    assert coverage is not None
    assert coverage["status"] == "incomplete"
    [section] = coverage["sections"]
    assert section["section_id"] == "sec-a"
    assert section["next_cursor"] == "cur-a-2"
    assert section["reason"] == "unread"

    # 続きを読み切れば complete。
    rest = _step(
        {"document_id": "doc-a", "cursor": "cur-a-2"},
        _section_output("sec-a", PART_2),
    )
    assert section_coverage([first, rest], f"{PART_1}\n{PART_2}", [GOAL]) == {
        "status": "complete",
        "sections": [],
    }
    # 手順・一覧・条件のまとまりを求めない質問は判定しない。
    assert section_coverage([first], answer, ["設定 A はどこにありますか？"]) is None
    # 章節を読んでいない Run も判定しない。
    assert section_coverage([], answer, [GOAL]) is None


def test_coverage_counts_multiple_sections_and_skips_unused_ones() -> None:
    steps = [
        _step(
            {"document_id": "doc-a", "section_id": "sec-a"},
            _section_output("sec-a", PART_1, next_cursor="cur-a-2"),
        ),
        _step(
            {"document_id": "doc-a", "section_id": "sec-b"},
            _section_output("sec-b", PART_2, next_cursor="cur-b-2"),
        ),
        # 探索で読んだだけの章節（回答に使っていない）。
        _step(
            {"document_id": "doc-a", "section_id": "sec-x"},
            _section_output("sec-x", OTHER, next_cursor="cur-x-2"),
        ),
    ]
    coverage = section_coverage(steps, f"{PART_1}\n{PART_2}", [GOAL])
    assert coverage is not None
    assert [item["section_id"] for item in coverage["sections"]] == ["sec-a", "sec-b"]

    # 回答が章節の chunk の id を書いていれば、本文が当たらなくても使ったとみなす（別の id は
    # 当たらない）。
    cited = section_coverage(steps, "設定は根拠 doc-a:sec-x:99 のとおりです。", [GOAL])
    assert cited is not None
    assert cited["status"] == "complete"
    chunk_id = f"doc-a:sec-x:{len(OTHER)}"
    cited = section_coverage(steps, f"設定は根拠 {chunk_id} のとおりです。", [GOAL])
    assert cited is not None
    assert [item["section_id"] for item in cited["sections"]] == ["sec-x"]


def test_coverage_treats_a_completed_parent_section_as_read() -> None:
    steps = [
        _step(
            {"document_id": "doc-a", "section_id": "sec-a"},
            _section_output("sec-a", PART_1, next_cursor="cur-a-2", parent_section_id="sec-p"),
        ),
        _step(
            {"document_id": "doc-a", "section_id": "sec-a", "up": 1},
            _section_output("sec-p", f"{PART_1}\n{PART_2}", parent_section_id=None),
        ),
    ]
    assert section_coverage(steps, f"{PART_1}\n{PART_2}", [GOAL]) == {
        "status": "complete",
        "sections": [],
    }


def test_coverage_reasons_for_stale_cursor_budget_and_failure() -> None:
    first = _step(
        {"document_id": "doc-a", "section_id": "sec-a"},
        _section_output("sec-a", PART_1, next_cursor="cur-a-2"),
    )
    stale = _step(
        {"document_id": "doc-a", "cursor": "cur-a-2"},
        error_code="mcp.tool_error",
        details={"error_code": "source_stale"},
    )
    budget = _step(
        {"document_id": "doc-a", "cursor": "cur-a-2"},
        error_code=BUDGET_EXCEEDED_CODE,
        details={"budget": {"scope": "task_tool_calls"}},
    )
    failed = _step(
        {"document_id": "doc-a", "cursor": "cur-a-2"},
        error_code="mcp.rpc_error",
    )

    def reason(*steps: RunStep, exhausted: bool = False) -> str:
        coverage = section_coverage(list(steps), PART_1, [GOAL], budget_exhausted=exhausted)
        assert coverage is not None
        return str(coverage["sections"][0]["reason"])

    assert reason(first, stale) == "stale"
    assert reason(first, budget) == "budget"
    assert reason(first, failed) == "failed"
    # 続きを呼んでいないまま上限に達した。
    assert reason(first, exhausted=True) == "budget"

    # 古い位置の後に、取り込み直した文書の同じ題名の章節を読み切れば complete。
    reread = _step(
        {"document_id": "doc-a", "section_id": "sec-a2"},
        _section_output("sec-a2", f"{PART_1}\n{PART_2}", title="章節 sec-a", chunk_set_id="cs-2"),
    )
    assert section_coverage([first, stale, reread], PART_1, [GOAL]) == {
        "status": "complete",
        "sections": [],
    }


def test_messages_name_the_section_and_how_to_continue() -> None:
    sections = [
        {
            "document_id": "doc-a",
            "file_name": "文書 A.pdf",
            "section_id": "sec-a",
            "title": "章節 A",
            "page_start": 3,
            "page_end": 4,
            "next_cursor": "cur-a-2",
            "reason": "unread",
        },
        {
            "document_id": "doc-a",
            "file_name": "文書 A.pdf",
            "section_id": "sec-b",
            "title": "章節 B",
            "page_start": 5,
            "page_end": 5,
            "next_cursor": "cur-b-2",
            "reason": "stale",
        },
    ]
    message = unread_sections_message(sections)
    assert message.startswith(UNREAD_SECTIONS_RETRY_HINT)
    assert "next_cursor: cur-a-2" in message
    assert "rag_outline" in message.splitlines()[-1]
    notice = with_unread_sections_notice("回答", sections)
    assert f"**{UNREAD_SECTIONS_HEADING}**" in notice
    assert "「章節 A」（文書 A.pdf、p.3-4）: 続きを読んでいません" in notice
    assert "「章節 B」（文書 A.pdf、p.5）: 文書が更新され、続きの位置が古くなりました" in notice


# ---- 答える前に 1 回だけ戻す・読み切れなければ示す（組み込み Runtime） --------------------------


ReadSection = Callable[[Any], dict[str, Any]]


def _read_section(argument: Any) -> dict[str, Any]:
    if argument.cursor == "cur-a-2":
        return _section_output("sec-a", PART_2)
    if argument.cursor == "cur-b-2":
        return _section_output("sec-b", "手順 4: 画面 D で完了を確かめます。")
    if argument.section_id == "sec-b":
        return _section_output(
            "sec-b", "手順 3: 画面 C で承認を依頼します。", next_cursor="cur-b-2"
        )
    if argument.section_id == "sec-x":
        return _section_output("sec-x", OTHER, next_cursor="cur-x-2")
    return _section_output("sec-a", PART_1, next_cursor="cur-a-2")


@pytest.fixture
def mcp(monkeypatch: MonkeyPatch) -> Iterator[FakeProductMcp]:
    fake = fake_product_mcp(monkeypatch, outputs={"rag_read_section": _read_section})
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", False)
    skill_registry.upsert_custom(
        AgentSkillDefinition(
            id=SKILL_ID,
            name=SKILL_ID,
            instructions="手順は rag_read_section で章節を読んで調べる。",
            mcp_requirements=[
                SkillMcpRequirement(
                    server_id="rag",
                    tool_names=["rag_retrieve_evidence", "rag_read_section", "rag_outline"],
                )
            ],
        )
    )
    runtime_repository.create_agent(AgentProfile(id=AGENT_ID, name=AGENT_ID, skill_ids=[SKILL_ID]))
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


def _script(monkeypatch: MonkeyPatch, *steps: Any) -> ScriptedModel:
    model = ScriptedModel(list(steps))
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id=model_id or "test-model",
            endpoint="https://llm.example",
            project_id="",
            api_key="k",
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _run(goal: str = GOAL) -> RunState:
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal=goal, agent_id=AGENT_ID), created_by_user_uuid=USER_UUID
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    return runtime_repository.get_run(run.id)


def _read(call_id: str, **arguments: Any) -> list[Any]:
    return [
        function_call(
            "rag__rag_read_section", {"document_id": "doc-a", **arguments}, call_id=call_id
        )
    ]


def _retry_message(model: ScriptedModel, call_index: int) -> str:
    users = [item for item in model.calls[call_index].input if isinstance(item, dict)]
    return str([item for item in users if item.get("role") == "user"][-1]["content"])


def _answer(run: RunState) -> dict[str, Any]:
    return [item for item in run.artifacts if item.kind == "answer"][-1].content


def test_answer_with_an_unread_continuation_is_sent_back_once(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    first = f"{PART_1}\n以上で完了です。"
    model = _script(
        monkeypatch,
        _read("c1", section_id="sec-a"),
        [assistant_message(first)],
        _read("c2", cursor="cur-a-2"),
        [assistant_message(f"{PART_1}\n{PART_2}")],
    )

    run = _run()

    assert len(model.calls) == 4
    retry = _retry_message(model, 2)
    assert retry.startswith(UNREAD_SECTIONS_RETRY_HINT)
    assert "next_cursor: cur-a-2" in retry
    assert first in retry
    assert run.metadata[METADATA_KEY] == {"hop_retries": 1, "section_retry": True}
    answer = _answer(run)
    assert answer["text"] == f"{PART_1}\n{PART_2}"
    assert "unread_sections" not in answer["outcome"]["signals"]


def test_continuation_read_before_answering_is_not_sent_back(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(
        monkeypatch,
        _read("c1", section_id="sec-a"),
        _read("c2", cursor="cur-a-2"),
        [assistant_message(f"{PART_1}\n{PART_2}")],
    )

    run = _run()

    assert len(model.calls) == 3
    assert METADATA_KEY not in run.metadata
    assert UNREAD_SECTIONS_HEADING not in _answer(run)["text"]


def test_unread_continuation_after_the_retry_is_shown_in_the_answer(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(
        monkeypatch,
        _read("c1", section_id="sec-a"),
        [assistant_message(PART_1)],
        [assistant_message(PART_1)],
    )

    run = _run()

    assert len(model.calls) == 3
    answer = _answer(run)
    assert answer["text"].startswith(PART_1)
    assert f"**{UNREAD_SECTIONS_HEADING}**" in answer["text"]
    assert "「章節 sec-a」（文書 A.pdf、p.3-4）: 続きを読んでいません" in answer["text"]
    # 回答の対応は不足を示した条件付きの回答。
    assert answer["outcome"]["value"] == "conditional"
    assert "unread_sections" in answer["outcome"]["signals"]


def test_multiple_sections_are_sent_back_together_and_unused_ones_are_not(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    part_3 = "手順 3: 画面 C で承認を依頼します。"
    model = _script(
        monkeypatch,
        _read("c1", section_id="sec-a"),
        _read("c2", section_id="sec-b"),
        # 探索で読んだだけの章節（回答に使わない）。
        _read("c3", section_id="sec-x"),
        [assistant_message(f"{PART_1}\n{part_3}")],
        _read("c4", cursor="cur-a-2"),
        _read("c5", cursor="cur-b-2"),
        [assistant_message(f"{PART_1}\n{PART_2}\n{part_3}\n手順 4: 画面 D で完了を確かめます。")],
    )

    run = _run()

    assert len(model.calls) == 7
    retry = _retry_message(model, 4)
    assert "next_cursor: cur-a-2" in retry
    assert "next_cursor: cur-b-2" in retry
    assert "sec-x" not in retry
    assert UNREAD_SECTIONS_HEADING not in _answer(run)["text"]


def test_section_only_explored_does_not_force_reading(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    model = _script(
        monkeypatch,
        _read("c1", section_id="sec-x"),
        _read("c2", section_id="sec-a"),
        _read("c3", cursor="cur-a-2"),
        [assistant_message(f"{PART_1}\n{PART_2}")],
    )

    run = _run()

    assert len(model.calls) == 4
    assert METADATA_KEY not in run.metadata
    assert UNREAD_SECTIONS_HEADING not in _answer(run)["text"]


def test_budget_limit_is_not_sent_back_and_is_shown(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    monkeypatch.setattr(get_settings(), "agent_max_tool_calls_per_task", 1)
    model = _script(
        monkeypatch,
        _read("c1", section_id="sec-a"),
        # 続きは予算の上限で呼ばれない。
        _read("c2", cursor="cur-a-2"),
        [assistant_message(PART_1)],
    )

    run = _run()

    # 予算が無いので差し戻さない（読めないため）。
    assert len(model.calls) == 3
    assert METADATA_KEY not in run.metadata
    assert len(mcp.calls_of("rag_read_section")) == 1
    text = _answer(run)["text"]
    assert "この実行のツールの呼び出しの上限に達しました" in text


def test_stale_cursor_is_sent_back_to_read_again_from_the_outline(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    def read(argument: Any) -> dict[str, Any]:
        if argument.cursor is not None:
            raise McpToolError("source_stale", "この位置は文書の古い版のものです。")
        return _read_section(argument)

    mcp.outputs["rag_read_section"] = read
    model = _script(
        monkeypatch,
        _read("c1", section_id="sec-a"),
        _read("c2", cursor="cur-a-2"),
        [assistant_message(PART_1)],
        [assistant_message(PART_1)],
    )

    run = _run()

    assert len(model.calls) == 4
    retry = _retry_message(model, 3)
    assert "rag_outline" in retry
    assert "cur-a-2" not in retry.split(UNREAD_SECTIONS_RETRY_HINT, 1)[1].split(PART_1)[0]
    text = _answer(run)["text"]
    assert "文書が更新され、続きの位置が古くなりました" in text


def test_cancelled_run_is_not_sent_back(monkeypatch: MonkeyPatch, mcp: FakeProductMcp) -> None:
    def read(argument: Any) -> dict[str, Any]:
        # 章節を読んでいる間に、利用者が実行を取り消した。
        repository: Any = runtime_repository
        for run_id, run in list(repository._runs.items()):  # noqa: SLF001 - テストの操作
            if run.agent_id == AGENT_ID and run.status == "running":
                runtime_repository.cancel_run(run_id)
        return _read_section(argument)

    mcp.outputs["rag_read_section"] = read
    model = _script(
        monkeypatch,
        _read("c1", section_id="sec-a"),
        [assistant_message(PART_1)],
    )

    run = _run()

    # 取消の後はモデルを呼ばない（#2609）。差し戻しもしない。
    assert len(model.calls) == 1
    assert run.status == "cancelled"
    assert not [item for item in run.artifacts if item.kind == "answer"]
    # 取消の前に送った読み取りの結果は、事実のまま記録する。
    [step] = run.steps
    assert step.status == "completed"


def test_coverage_is_kept_apart_from_the_claim_validation(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    monkeypatch.setattr(get_settings(), "agent_final_validation_enabled", True)
    validate: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_validate_answer"])
    validate["claims"] = [
        {"answer_quote": PART_1, "status": "supported", "chunk_id": "doc-a:sec-a:27"}
    ]
    mcp.outputs["rag_validate_answer"] = validate
    _script(
        monkeypatch,
        _read("c1", section_id="sec-a"),
        [assistant_message(PART_1)],
        [assistant_message(PART_1)],
    )

    run = _run()

    validation = [item for item in run.artifacts if item.kind == ANSWER_VALIDATION_KIND][-1]
    # 主張は根拠で確かめられた（照合は通る）が、手順の全体は確かめられていない。
    assert validation.content["valid"] is True
    coverage = validation.content["coverage"]
    assert coverage["status"] == "incomplete"
    assert [item["section_id"] for item in coverage["sections"]] == ["sec-a"]
    assert f"**{UNREAD_SECTIONS_HEADING}**" in _answer(run)["text"]


# ---- 読み始めていない章節（根拠の抜粋だけで答えた。#2503） ------------------------------------


def _evidence_item(
    excerpt: str,
    *,
    section_id: str = "sec-a",
    chunk_id: str = "doc-a:1",
    chars: int = 5000,
    chunk_count: int = 8,
) -> dict[str, Any]:
    item: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"]["evidence"][0])
    item.update(document_id="doc-a", chunk_id=chunk_id, excerpt=excerpt, file_name="文書 A.pdf")
    item["section"] = {
        **item["section"],
        "section_id": section_id,
        "title": f"章節 {section_id}",
        "page_start": 3,
        "page_end": 9,
        "chars": chars,
        "chunk_count": chunk_count,
        "parent_section_id": "sec-root",
    }
    return item


def _evidence_step(*items: dict[str, Any]) -> RunStep:
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"])
    output["evidence"] = list(items)
    return RunStep(
        run_id="run-2503",
        title="rag_retrieve_evidence",
        status="completed",
        tool_call=ToolCall(name="rag__rag_retrieve_evidence", arguments={"query": "設定 A"}),
        tool_result=ToolResult(name="rag__rag_retrieve_evidence", success=True, output=output),
    )


def _document_step(arguments: dict[str, Any], next_cursor: str | None) -> RunStep:
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_read_document"])
    output.update(document_id="doc-a", next_cursor=next_cursor)
    return RunStep(
        run_id="run-2503",
        title="rag_read_document",
        status="completed",
        tool_call=ToolCall(
            name="rag__rag_read_document", arguments={"document_id": "doc-a", **arguments}
        ),
        tool_result=ToolResult(name="rag__rag_read_document", success=True, output=output),
    )


def test_coverage_tracks_long_evidence_sections_that_were_not_read() -> None:
    evidence = _evidence_step(
        _evidence_item(PART_1),
        # 探索で当たっただけの章節（回答に使わない）。
        _evidence_item(OTHER, section_id="sec-x", chunk_id="doc-a:9"),
        # 抜粋で章節の全体を得ている章節。
        _evidence_item(PART_2, section_id="sec-s", chunk_id="doc-a:5", chars=len(PART_2)),
    )
    coverage = section_coverage([evidence], f"{PART_1}\n{PART_2}", [GOAL])
    assert coverage is not None
    [section] = coverage["sections"]
    assert section["section_id"] == "sec-a"
    assert section["reason"] == "not_read"
    assert section["next_cursor"] is None
    # 予算の上限に達していれば理由は上限。
    exhausted = section_coverage([evidence], PART_1, [GOAL], budget_exhausted=True)
    assert exhausted is not None
    assert exhausted["sections"][0]["reason"] == "budget"
    # 手順・一覧・条件のまとまりを求めない質問は判定しない。
    assert section_coverage([evidence], PART_1, ["設定 A はどこにありますか？"]) is None


def test_long_evidence_section_counts_as_read_when_read_or_fully_retrieved() -> None:
    evidence = _evidence_step(_evidence_item(PART_1))
    complete = {"status": "complete", "sections": []}
    # 章節を読み切った。
    read = _step(
        {"document_id": "doc-a", "section_id": "sec-a"},
        _section_output("sec-a", f"{PART_1}\n{PART_2}"),
    )
    assert section_coverage([evidence, read], PART_1, [GOAL]) == complete
    # 根拠で章節の chunk をすべて得た。
    whole = _evidence_step(
        _evidence_item(PART_1, chunk_count=2),
        _evidence_item(PART_2, chunk_id="doc-a:2", chunk_count=2),
    )
    assert section_coverage([whole], f"{PART_1}\n{PART_2}", [GOAL]) == complete
    # 文書を先頭から終わりまで読んだ（続きを 1 回たどる）。
    start = _document_step({}, "doc-cursor-1")
    rest = _document_step({"cursor": "doc-cursor-1"}, None)
    assert section_coverage([evidence, start, rest], PART_1, [GOAL]) == complete
    # 途中の頁から読んだだけ・終わりまで読んでいない文書は読み切っていない。
    for steps in ([evidence, _document_step({"page": 3}, None)], [evidence, start]):
        coverage = section_coverage(steps, PART_1, [GOAL])
        assert coverage is not None
        assert coverage["status"] == "incomplete"


def test_message_for_a_section_that_was_not_read() -> None:
    section = {
        "document_id": "doc-a",
        "file_name": "文書 A.pdf",
        "section_id": "sec-a",
        "title": "章節 A",
        "page_start": 3,
        "page_end": 9,
        "next_cursor": None,
        "reason": "not_read",
    }
    message = unread_sections_message([section])
    assert "section_id: sec-a" in message
    assert "rag_read_section に渡して終わりまで読む" in message
    assert "rag_outline" not in message
    notice = with_unread_sections_notice("回答", [section])
    assert "「章節 A」（文書 A.pdf、p.3-9）: 章節を読んでいません" in notice


def _retrieve(call_id: str) -> list[Any]:
    return [function_call("rag__rag_retrieve_evidence", {"query": "設定 A 手順"}, call_id=call_id)]


def test_answer_from_excerpts_of_a_long_section_is_sent_back_to_read_it(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    mcp.outputs["rag_retrieve_evidence"] = {
        **deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"]),
        "evidence": [_evidence_item(PART_1)],
    }
    mcp.outputs["rag_read_section"] = lambda _argument: _section_output(
        "sec-a", f"{PART_1}\n{PART_2}"
    )
    model = _script(
        monkeypatch,
        _retrieve("c1"),
        [assistant_message(PART_1)],
        _read("c2", section_id="sec-a"),
        [assistant_message(f"{PART_1}\n{PART_2}")],
    )

    run = _run()

    assert len(model.calls) == 4
    retry = _retry_message(model, 2)
    assert retry.startswith(UNREAD_SECTIONS_RETRY_HINT)
    assert "section_id: sec-a" in retry
    assert run.metadata[METADATA_KEY] == {"hop_retries": 1, "section_retry": True}
    assert UNREAD_SECTIONS_HEADING not in _answer(run)["text"]


def test_answer_from_excerpts_is_shown_when_the_section_is_still_not_read(
    monkeypatch: MonkeyPatch, mcp: FakeProductMcp
) -> None:
    mcp.outputs["rag_retrieve_evidence"] = {
        **deepcopy(DEFAULT_OUTPUTS["rag_retrieve_evidence"]),
        "evidence": [_evidence_item(PART_1)],
    }
    _script(
        monkeypatch,
        _retrieve("c1"),
        [assistant_message(PART_1)],
        [assistant_message(PART_1)],
    )

    run = _run()

    answer = _answer(run)
    assert "「章節 sec-a」（文書 A.pdf、p.3-9）: 章節を読んでいません" in answer["text"]
    assert answer["outcome"]["value"] == "conditional"
    assert "unread_sections" in answer["outcome"]["signals"]
