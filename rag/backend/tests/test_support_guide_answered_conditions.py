"""確認に自由入力で答えた選択式の条件を、答えの文が選んだ選択肢に決める（#2726）。

資料・値は合成（項目 A・共有グループ）、OCI は決定論スタブ。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pytest import MonkeyPatch

from app.api.routes import search as search_route
from app.config import get_settings
from app.mcp import tools as mcp_tools
from app.rag import support_guide_conditions as recognition
from app.rag.support_guide_runtime import GuideMatch, match_guide, rank_guides
from app.schemas.search import SearchRequest
from app.schemas.support_guide import SupportGuideContent
from tests.test_mcp_api import _call, _token, auth  # noqa: F401 - fixture を使う
from tests.test_support_guide_matching import _ProfileOracle, _published_store

QUESTION = "表示設定Aを変更したい"
FREE_TEXT = "項目Aだけを変更し、共有グループ全体は変更しない"
GUIDE = SupportGuideContent.model_validate(
    {
        "title": "表示設定Aを変更する",
        "goal": {"expected_result": "表示設定Aが変わる", "match_terms": ["表示設定A"]},
        "conditions": [
            {
                "id": "scope",
                "label": "変更範囲",
                "type": "enum",
                "allowed_values": ["個別の項目", "共有グループ"],
                "question": "変更するのは個別の項目ですか、共有グループ全体ですか？",
            }
        ],
        "steps": [
            {"id": "open", "title": "表示設定Aの画面を開く"},
            {"id": "item", "title": "項目の所属先を変える"},
            {"id": "group", "title": "共有グループの表示区分を変える"},
        ],
    }
)
GUIDES = [("g-scope", 1, GUIDE)]


def stub(monkeypatch: MonkeyPatch, output: Any = None, *, error: bool = False) -> list[Any]:
    calls: list[Any] = []

    class Client:
        async def generate(self, _prompt: str, context: str, **kwargs: Any) -> str:
            calls.append((json.loads(context), kwargs))
            if error:
                raise RuntimeError("synthetic failure")
            body = output
            if body is None:
                body = {
                    "conditions": [
                        {"condition_id": "scope", "status": "known", "value": "個別の項目"}
                    ]
                }
            return body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)

    monkeypatch.setattr(recognition, "condition_client", lambda _settings: Client())
    return calls


def matched(provided: dict[str, str] | None = None) -> GuideMatch:
    result = match_guide(GUIDES, QUESTION, provided)
    assert result is not None
    return result


def test_option_with_a_note_is_the_option_without_the_model() -> None:
    assert matched({"scope": "個別の項目"}).decision == "answer"
    # カードで選んだ選択肢に自由入力の補足を足した答え（「、」で連結）。
    note = matched({"scope": "個別の項目、項目Aだけ"})
    assert (note.decision, note.states[0].value) == ("answer", "個別の項目")
    # 2 つの選択肢を並べた答えは決めない。
    assert matched({"scope": "個別の項目、共有グループ"}).decision == "clarify"
    assert matched({"scope": FREE_TEXT}).decision == "clarify"


async def test_free_text_answer_becomes_the_chosen_option(monkeypatch: MonkeyPatch) -> None:
    calls = stub(monkeypatch)
    result = await recognition.resolve_answered_conditions(
        matched({"scope": FREE_TEXT}), {"scope": FREE_TEXT}, get_settings()
    )
    assert isinstance(result, GuideMatch)
    assert result.decision == "answer"
    state = result.states[0]
    assert (state.value, state.source) == ("個別の項目", "user")
    [(context, kwargs)] = calls
    [condition] = context["conditions"]
    assert condition["answer"] == FREE_TEXT
    assert condition["options"] == ["個別の項目", "共有グループ"]
    assert "否定" in kwargs["system_prompt"]


@pytest.mark.parametrize(
    "output",
    [
        {"conditions": [{"condition_id": "scope", "status": "unknown", "value": ""}]},
        # 選択肢に無い値・別の条件の id は使わない。
        {"conditions": [{"condition_id": "scope", "status": "known", "value": "項目Aだけ"}]},
        {"conditions": [{"condition_id": "other", "status": "known", "value": "個別の項目"}]},
        "not json",
    ],
    ids=["unknown", "invented-value", "other-condition", "invalid-json"],
)
async def test_unresolved_answers_keep_the_question(monkeypatch: MonkeyPatch, output: Any) -> None:
    stub(monkeypatch, output)
    result = await recognition.resolve_answered_conditions(
        matched({"scope": FREE_TEXT}), {"scope": FREE_TEXT}, get_settings()
    )
    assert isinstance(result, GuideMatch) and result.decision == "clarify"


async def test_model_failure_and_known_options_do_not_change_the_match(
    monkeypatch: MonkeyPatch,
) -> None:
    stub(monkeypatch, error=True)
    failed = await recognition.resolve_answered_conditions(
        matched({"scope": FREE_TEXT}), {"scope": FREE_TEXT}, get_settings()
    )
    assert isinstance(failed, GuideMatch) and failed.decision == "clarify"
    # 選択肢そのものの答え・答えの無い条件ではモデルを呼ばない。
    calls = stub(monkeypatch)
    known = matched({"scope": "共有グループ"})
    assert (
        await recognition.resolve_answered_conditions(
            known, {"scope": "共有グループ"}, get_settings()
        )
        is known
    )
    unanswered = matched()
    assert (
        await recognition.resolve_answered_conditions(unanswered, {}, get_settings()) is unanswered
    )
    assert not calls


@pytest.mark.usefixtures("system_access")
async def test_search_uses_the_chosen_option_for_the_branch(monkeypatch: MonkeyPatch) -> None:
    stub(monkeypatch)
    monkeypatch.setattr(search_route, "OracleClient", lambda: _ProfileOracle())
    monkeypatch.setattr(search_route, "SupportGuideStore", _published_store(GUIDES))
    _, settings, _, _ = await search_route._resolve_query_context(
        SearchRequest(
            query=QUESTION,
            search_answer_profile_id="bv-1",
            conditions={"scope": FREE_TEXT},
            generate_answer=False,
        ),
        get_settings(),
    )
    guide: Any = settings.rag_support_guide
    assert guide["decision"] == "answer"
    assert [(item["id"], item["value"]) for item in guide["known_conditions"]] == [
        ("scope", "個別の項目")
    ]


@pytest.mark.usefixtures("auth")
def test_mcp_lookup_guides_resolves_the_free_text_answer(
    auth: Any,  # noqa: F811
    monkeypatch: MonkeyPatch,
) -> None:
    async def published(_oracle: object, _profile: str) -> tuple[list[Any], bool]:
        return GUIDES, False

    stub(monkeypatch)
    monkeypatch.setattr(mcp_tools, "OracleClient", _ProfileOracle)
    monkeypatch.setattr(search_route, "load_published_guides", published)
    user = auth.user_with_permissions("searcher", ["menu.search"])
    body = _call(
        "rag_lookup_guides",
        {"query": QUESTION, "search_answer_profile_id": "bv-1", "conditions": {"scope": FREE_TEXT}},
        _token(user.user_uuid),
    )["structuredContent"]
    [guide] = body["guides"]
    assert guide["decision"] == "answer"
    assert [(item["id"], item["value"]) for item in guide["known_conditions"]] == [
        ("scope", "個別の項目")
    ]
    assert guide["clarifications"] == []
    # 答えの無い照合は確かめる判断のまま。
    assert rank_guides(GUIDES, QUESTION, {})[0].decision == "clarify"
