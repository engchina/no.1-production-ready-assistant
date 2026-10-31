"""チャットの回答前の確認の回帰テスト（#1442）。

LLM は決定論のスタブ。チャットの session は会話の履歴（前の質問と SQL）を要件の検査に
渡し、業務プロファイルの推薦の確認を求めない。確認の要らない質問はすぐ反映でき、取り消した
session は使えない。反映した要件はチャットのジョブ（同じ会話の続き・利用者が送った質問の文）
にだけ渡り、生成は確かめた質問の文で行う。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from pr_system_settings.auth.visibility import (
    OTHERS_CONTENT_FORBIDDEN_MESSAGE,
    OTHERS_CONTENT_FORBIDDEN_STATUS,
)
from pydantic import ValidationError
from test_nl2sql_guided_clarification import (
    _ambiguous_status_output,
    _answer,
    _disable_profile_confirmation,
    _draft,
    _from_orders,
    _item,
    _LegacyService,
    _output,
    _runtime,
    _ScriptedLlm,
    _select_total,
    _state,
    _where_shipped,
)
from test_sql_chat import ChatFixture, chat, request, run

import app.features.nl2sql.ontology_router as ontology_router_module
from app.features.nl2sql import router as nl2sql_router
from app.features.nl2sql.models import JobCreateRequest, SqlRequirementSpec
from app.features.nl2sql.ontology_models import (
    ClarificationMode,
    QuerySessionStatus,
    RequirementPhase,
)
from app.features.nl2sql.ontology_router import (
    OntologyApiRuntime,
    QuerySessionApiCreate,
    QuerySessionData,
    RequirementApplyRequest,
)
from app.features.nl2sql.ontology_service import (
    OntologyGateBlockedError,
    OntologyNotFoundError,
    OntologyStateConflictError,
)
from app.features.nl2sql.service import OthersContentForbiddenError
from app.settings import get_settings

# pytest の fixture を読み込む（推薦の確認を既定で外す autouse と、本物の service のチャット）。
__all__ = ["_disable_profile_confirmation", "chat"]

_HISTORY = [
    {"query": "受注金額の合計", "sql": "SELECT SUM(AMOUNT) FROM APP.ORDERS", "status": "done"},
]


class _ChatLegacyService(_LegacyService):
    """会話の履歴を返す legacy service のスタブ（本物は chat_requirement_history）。"""

    def __init__(self, history: list[dict[str, str]] | None = None) -> None:
        super().__init__()
        self.history = history if history is not None else list(_HISTORY)
        self.history_calls: list[dict[str, Any]] = []

    def chat_requirement_history(self, **kwargs: Any) -> list[dict[str, str]]:
        self.history_calls.append(kwargs)
        if kwargs["actor_user_uuid"] != "user-1":
            # 会話を続けられるのは持ち主（user-1）だけ。見える system_admin は 403、見えない
            # 一般の利用者は 404（#3244。本物の `_assert_chat_owner_adds` と同じ）。
            if kwargs.get("actor_is_system_admin"):
                raise OthersContentForbiddenError()
            raise PermissionError("他の利用者の会話は参照できません。")
        return [dict(turn) for turn in self.history]


def _chat_runtime(
    llm: _ScriptedLlm, history: list[dict[str, str]] | None = None
) -> tuple[OntologyApiRuntime, _ChatLegacyService]:
    runtime = _runtime(llm)
    legacy = _ChatLegacyService(history)
    legacy._enterprise_ai_client = llm
    runtime.legacy_service = legacy
    return runtime, legacy


def _create_chat(
    runtime: OntologyApiRuntime,
    question: str = "出荷済みだけにして",
    *,
    previous_job_id: str | None = "job-previous",
    actor: str = "user-1",
    admin: bool = False,
) -> QuerySessionData:
    return runtime.create_session(
        QuerySessionApiCreate(
            question=question,
            profile_id="sales",
            clarification_mode=ClarificationMode.GUIDED,
            chat=True,
            previous_job_id=previous_job_id,
        ),
        actor_user_uuid=actor,
        actor_is_system_admin=admin,
    )


def _clear_output(clarified: str = "出荷済みの ORDERS.AMOUNT の合計") -> dict[str, Any]:
    return _output(_from_orders(), _select_total(), _where_shipped(), clarified=clarified)


# --- 自動の検査に会話の履歴を使う ----------------------------------------------------------


def test_chat_session_passes_conversation_history_to_requirement_check() -> None:
    llm = _ScriptedLlm(_clear_output())
    runtime, legacy = _chat_runtime(llm)

    created = _create_chat(runtime)

    assert legacy.history_calls == [
        {
            "question": "出荷済みだけにして",
            "profile_id": "sales",
            "previous_job_id": "job-previous",
            "actor_user_uuid": "user-1",
            # 確認は会話の続きなので、持ち主だけが始められる（#3244）。
            "actor_is_system_admin": False,
        }
    ]
    payload = llm.calls[0]["context"]
    assert payload["conversation_history"] == [
        {
            "question": "受注金額の合計",
            "sql": "SELECT SUM(AMOUNT) FROM APP.ORDERS",
            "status": "done",
        }
    ]
    assert "conversation_history" in llm.calls[0]["system"]
    session = created.session
    assert session.chat is True
    assert session.chat_previous_job_id == "job-previous"
    assert [turn.question for turn in session.conversation_history] == ["受注金額の合計"]


def test_chat_history_is_used_again_in_review_after_answers() -> None:
    llm = _ScriptedLlm(_ambiguous_status_output(), _clear_output())
    runtime, _legacy = _chat_runtime(llm)
    created = _create_chat(runtime, "状態で絞って")
    assert _state(created).status == RequirementPhase.NEEDS_ANSWER

    answered = _answer(runtime, created)

    assert len(llm.calls) == 2
    assert llm.calls[1]["context"]["mode"] == "review"
    assert llm.calls[1]["context"]["conversation_history"][0]["question"] == "受注金額の合計"
    assert _state(answered).can_apply


def test_first_chat_turn_has_no_history_and_needs_signed_in_user() -> None:
    llm = _ScriptedLlm(_clear_output())
    runtime, legacy = _chat_runtime(llm)

    created = _create_chat(runtime, "出荷済みの受注金額の合計", previous_job_id=None)

    assert legacy.history_calls == []
    assert "conversation_history" not in llm.calls[0]["context"]
    assert created.session.conversation_history == []
    with pytest.raises(OntologyStateConflictError, match="認証済み"):
        _create_chat(runtime, "出荷済みの受注金額の合計", previous_job_id=None, actor="")


def test_chat_session_rejects_other_users_conversation() -> None:
    """一般の利用者は他の利用者の会話の確認を始められない（存在を漏らさない 404。#1889）。"""
    runtime, _legacy = _chat_runtime(_ScriptedLlm(_clear_output()))
    with pytest.raises(OntologyNotFoundError, match="会話が見つかりません"):
        _create_chat(runtime, actor="user-2")


def test_system_admin_cannot_start_check_in_other_users_conversation() -> None:
    """system_admin も他の利用者の会話の続きの確認は始められない（見えるので 403。#3244）。

    確認の LLM は呼ばず、session も作らない。route は 403 と共通の文言にする。
    """
    llm = _ScriptedLlm(_clear_output())
    runtime, _legacy = _chat_runtime(llm)
    with pytest.raises(OthersContentForbiddenError) as denied:
        _create_chat(runtime, actor="admin", admin=True)
    assert llm.calls == []
    with pytest.raises(HTTPException) as mapped:
        ontology_router_module._raise_domain_error(denied.value)  # noqa: SLF001
    assert mapped.value.status_code == OTHERS_CONTENT_FORBIDDEN_STATUS == 403
    assert mapped.value.detail == OTHERS_CONTENT_FORBIDDEN_MESSAGE
    # 自分の会話（持ち主）は今どおり始められる。
    assert _create_chat(runtime).session.chat is True


def test_chat_session_does_not_require_profile_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """チャットは業務プロファイルを画面で選んでいるので、推薦の確認の token を求めない。"""

    monkeypatch.setattr(get_settings(), "nl2sql_ontology_profile_confirmation_required", True)
    runtime, _legacy = _chat_runtime(_ScriptedLlm(_clear_output(), _clear_output()))

    assert _create_chat(runtime).session.chat is True
    with pytest.raises(OntologyGateBlockedError):
        runtime.create_session(
            QuerySessionApiCreate(
                question="出荷済みの受注金額の合計",
                profile_id="sales",
                clarification_mode=ClarificationMode.GUIDED,
            ),
            actor_user_uuid="user-1",
        )


def test_chat_create_request_requires_guided_and_chat_for_history() -> None:
    with pytest.raises(ValidationError, match="guided"):
        QuerySessionApiCreate(question="q", profile_id="sales", chat=True)
    with pytest.raises(ValidationError, match="チャット"):
        QuerySessionApiCreate(
            question="q",
            profile_id="sales",
            clarification_mode=ClarificationMode.GUIDED,
            previous_job_id="job-previous",
        )


# --- 確認が要らないときの直行・取り消し --------------------------------------------------


def test_clear_chat_question_is_ready_without_questions_and_applies() -> None:
    """SELECT・FROM が明確で不足・あいまいが無ければ、質問を出さずに反映できる。

    画面はこのとき確認の質問を出さずに SQL を生成する。
    """

    runtime, _legacy = _chat_runtime(_ScriptedLlm(_clear_output()))
    created = _create_chat(runtime)
    state = _state(created)
    assert state.status == RequirementPhase.READY
    assert state.can_apply and state.select_from_clear
    assert state.unresolved_items == [] and state.current_item is None
    assert state.answered_count == 0

    applied = runtime.apply_requirement(created.session.id, RequirementApplyRequest(base_version=1))
    assert applied.session.status == QuerySessionStatus.DONE


def test_cancelled_chat_session_cannot_be_applied_or_used() -> None:
    runtime, _legacy = _chat_runtime(_ScriptedLlm(_ambiguous_status_output()))
    created = _create_chat(runtime, "状態で絞って")

    cancelled = runtime.cancel_session(created.session.id)

    assert cancelled.session.status == QuerySessionStatus.CANCELLED
    with pytest.raises(OntologyStateConflictError):
        runtime.apply_requirement(created.session.id, RequirementApplyRequest(base_version=1))
    with pytest.raises(OntologyStateConflictError):
        runtime.applied_requirement_spec(
            created.session.id,
            actor_user_uuid="user-1",
            actor_is_system_admin=False,
            profile_id="sales",
            question="状態で絞って",
            chat=True,
            previous_job_id="job-previous",
        )


# --- 反映した要件はチャットのジョブにだけ渡る -------------------------------------------


def _applied_chat(
    runtime: OntologyApiRuntime, question: str = "出荷済みだけにして"
) -> tuple[str, str]:
    created = _create_chat(runtime, question)
    runtime.apply_requirement(created.session.id, RequirementApplyRequest(base_version=1))
    return created.session.id, _draft(created).clarified_question_ja


def test_chat_job_takes_requirement_for_same_conversation_and_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, _legacy = _chat_runtime(_ScriptedLlm(_clear_output()))
    session_id, clarified = _applied_chat(runtime)
    assert clarified.startswith("出荷済みの ORDERS.AMOUNT の合計")
    monkeypatch.setattr(ontology_router_module, "ontology_runtime", runtime)
    principal = SimpleNamespace(user_uuid="user-1", is_system_admin=False)
    chat_request = JobCreateRequest(
        question="出荷済みだけにして",
        profile_id="sales",
        chat=True,
        previous_job_id="job-previous",
        requirement_session_id=session_id,
    )

    resolved = nl2sql_router._with_requirement(chat_request, principal)

    assert resolved.sql_requirement is not None
    assert resolved.sql_requirement.clarified_question == clarified
    # 吹き出しの質問は利用者の文のまま、生成は確かめた質問の文で行う。
    assert resolved.question == "出荷済みだけにして"
    assert resolved.generation_question == clarified
    assert resolved.allowed_objects.table_names == ["APP.ORDERS"]

    for changed in (
        chat_request.model_copy(update={"previous_job_id": "job-other"}),
        chat_request.model_copy(update={"question": "書き換えた質問"}),
        # チャットで確かめた要件は SQL 生成の画面のジョブに使えない。
        JobCreateRequest(question=clarified, profile_id="sales", requirement_session_id=session_id),
    ):
        with pytest.raises(HTTPException) as raised:
            nl2sql_router._with_requirement(changed, principal)
        assert raised.value.status_code == 409


def test_sql_screen_requirement_cannot_be_used_in_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime(_ScriptedLlm(_clear_output()))
    created = runtime.create_session(
        QuerySessionApiCreate(
            question="出荷済みの受注金額の合計",
            profile_id="sales",
            clarification_mode=ClarificationMode.GUIDED,
        ),
        actor_user_uuid="user-1",
    )
    runtime.apply_requirement(created.session.id, RequirementApplyRequest(base_version=1))
    monkeypatch.setattr(ontology_router_module, "ontology_runtime", runtime)
    with pytest.raises(HTTPException) as raised:
        nl2sql_router._with_requirement(
            JobCreateRequest(
                question=_draft(created).clarified_question_ja,
                profile_id="sales",
                chat=True,
                requirement_session_id=created.session.id,
            ),
            SimpleNamespace(user_uuid="user-1", is_system_admin=False),
        )
    assert raised.value.status_code == 409


# --- 会話の履歴と生成の質問（本物の Nl2SqlService） ---------------------------------------


def _spec(question: str) -> SqlRequirementSpec:
    return SqlRequirementSpec(
        session_id="query_session_1",
        profile_id=request().profile_id or "",
        clarified_question=question,
        tables=["APP.ORDERS"],
    )


def test_chat_requirement_history_reads_previous_turns(chat: ChatFixture) -> None:
    service, _, _ = chat
    first = run(service, request("注文一覧"))
    clarified = request("多い順にして", first.job_id).model_copy(
        update={"sql_requirement": _spec("APP.ORDERS の ID を多い順に並べた一覧")}
    )
    second = run(service, clarified)
    # 吹き出しの質問は利用者の文、確かめた質問の文は別に返す。
    assert second.question == "多い順にして"
    assert second.requirement_question == "APP.ORDERS の ID を多い順に並べた一覧"

    history = service.chat_requirement_history(
        question="上位 5 件だけ",
        profile_id=request().profile_id or "",
        previous_job_id=second.job_id,
        actor_user_uuid="user-1",
    )

    assert [turn["query"] for turn in history] == [
        "注文一覧",
        "APP.ORDERS の ID を多い順に並べた一覧",
    ]
    assert all(turn["sql"] == "SELECT ID FROM APP.ORDERS" for turn in history)
    assert all("rows" not in turn for turn in history)
    with pytest.raises(PermissionError):
        service.chat_requirement_history(
            question="上位 5 件だけ",
            profile_id=request().profile_id or "",
            previous_job_id=second.job_id,
            actor_user_uuid="other",
        )
    with pytest.raises(PermissionError):
        service.chat_requirement_history(
            question="上位 5 件だけ",
            profile_id=request().profile_id or "",
            previous_job_id=second.job_id,
            actor_user_uuid="",
        )


def test_chat_history_keeps_recent_turns_within_shared_budget(
    chat: ChatFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前のターンは共通の予算で新しい順に詰め、古いターンを落とし、0 なら渡さない（#1818）。"""
    import app.features.nl2sql.service as service_module

    service, _, client = chat
    first = run(service, request("注文一覧"))
    second = run(service, request("多い順にして", first.job_id))
    settings = get_settings().model_copy(update={"chat_history_max_turns": 1})
    monkeypatch.setattr(service_module, "get_settings", lambda: settings)

    history = service.chat_requirement_history(
        question="上位 5 件だけ",
        profile_id=request().profile_id or "",
        previous_job_id=second.job_id,
        actor_user_uuid="user-1",
    )
    assert [turn["query"] for turn in history] == ["多い順にして"]

    run(service, request("上位 5 件だけ", second.job_id))
    prompt = client.prompts[-1]
    # 履歴は未信頼のデータとして囲み、予算の外の古いターンは渡さない。
    assert '<conversation_history untrusted="true">' in prompt
    assert "<question>多い順にして</question>" in prompt
    assert "<question>注文一覧</question>" not in prompt

    disabled = settings.model_copy(update={"chat_history_max_turns": 0})
    monkeypatch.setattr(service_module, "get_settings", lambda: disabled)
    assert (
        service.chat_requirement_history(
            question="上位 5 件だけ",
            profile_id=request().profile_id or "",
            previous_job_id=second.job_id,
            actor_user_uuid="user-1",
        )
        == []
    )


def test_chat_generation_uses_clarified_question_with_history(chat: ChatFixture) -> None:
    service, _, client = chat
    first = run(service, request("注文一覧"))

    run(
        service,
        request("多い順にして", first.job_id).model_copy(
            update={"sql_requirement": _spec("APP.ORDERS の ID を多い順に並べた一覧")}
        ),
    )

    prompt = client.prompts[-1]
    assert "会話履歴" in prompt and "注文一覧" in prompt
    assert "最新の質問: APP.ORDERS の ID を多い順に並べた一覧" in prompt


def test_first_chat_turn_generation_uses_clarified_question(chat: ChatFixture) -> None:
    service, _, client = chat

    job = run(
        service,
        request("注文を見たい").model_copy(update={"sql_requirement": _spec("APP.ORDERS の一覧")}),
    )

    assert job.question == "注文を見たい"
    assert "APP.ORDERS の一覧" in client.prompts[-1]
    assert "会話履歴" not in client.prompts[-1]


def test_skeleton_does_not_repeat_the_same_fragment() -> None:
    """会話の続きで、答えた並び順と AI の推測の並び順が同じ断片のとき、骨組みに 2 回出さない。"""

    order = _item(
        "order_count",
        "order_by",
        "inferred",
        value="合計の降順",
        sql_hint="SUM(ORDERS.AMOUNT) DESC",
    )
    again = _item(
        "order_again",
        "order_by",
        "confirmed",
        value="合計の降順",
        sql_hint="SUM(ORDERS.AMOUNT) DESC",
    )
    runtime, _legacy = _chat_runtime(
        _ScriptedLlm(_output(_from_orders(), _select_total(), _where_shipped(), order, again))
    )

    skeleton = _state(_create_chat(runtime, "多い順にして")).sql_skeleton

    assert "ORDER BY SUM(ORDERS.AMOUNT) DESC\n" in skeleton + "\n"
    assert skeleton.count("SUM(ORDERS.AMOUNT) DESC") == 1
