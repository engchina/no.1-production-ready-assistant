"""検索・回答プロファイルのルールの確認の質問(チャットの確認。#717)。"""

import json
from typing import Any

import pytest

from app.api.routes import search as search_route
from app.api.routes import search_answer_profile_knowledge as knowledge_route
from app.main import app
from app.mcp import tools as mcp_tools
from app.rag import document_sections_service
from app.rag.search_answer_profile_knowledge import (
    clarification_record,
    resolve_clarification,
    rule_answer,
)
from app.schemas.search import SearchDiagnostics, SearchRequest, SearchResponse
from app.schemas.search_answer_profile_knowledge import ClarificationAnswer
from tests.security_support import ProductionAuth
from tests.support import AsgiTestClient
from tests.test_mcp_api import _call, _token, auth  # noqa: F401 - fixture を使う
from tests.test_search_answer_profile_domain_keywords import FakeKnowledgeOracle

client = AsgiTestClient(app)
BASE = "/api/search-answer-profiles/bv-1"

CLARIFICATION: dict[str, Any] = {
    "question": "どの規程についてのご質問ですか？",
    "multiple": True,
    "allow_other": True,
    "options": [
        {
            "id": "travel",
            "label": "出張旅費",
            "search_terms": ["出張", "日当"],
            "premise": "利用者は出張旅費規程について質問している",
            "sections": [
                {
                    "document_id": "doc-travel",
                    "document_name": "出張旅費規程.pdf",
                    "section_id": "sec-6",
                    "title": "第6条 申請と精算",
                    "page_start": 2,
                    "page_end": 3,
                }
            ],
        },
        {"id": "expense", "label": "経費精算", "search_terms": ["精算マニュアル"]},
    ],
}


@pytest.fixture
def fake_oracle(monkeypatch: pytest.MonkeyPatch) -> FakeKnowledgeOracle:
    fake = FakeKnowledgeOracle()
    monkeypatch.setattr(knowledge_route, "OracleClient", lambda: fake)
    client.post(
        f"{BASE}/runtime-knowledge/edit",
        json={
            "kind": "rules",
            "name": "R01",
            "title": "期限の確認",
            "labels": "期限\n締め切り",
            "content": "期限は規程ごとに違う。",
        },
    )
    return fake


def test_clarification_is_saved_on_the_rule_and_suggested_for_matching_questions(
    fake_oracle: FakeKnowledgeOracle,
) -> None:
    saved = client.put(
        f"{BASE}/runtime-knowledge/rules/R01/clarification", json={"clarification": CLARIFICATION}
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["rules"][0]["clarification"]["question"] == (
        "どの規程についてのご質問ですか？"
    )

    suggestion = client.post(
        f"{BASE}/clarifications/suggest", json={"query": "申請の期限はいつですか"}
    ).json()["data"]["suggestion"]
    assert suggestion["rule_id"] == "R01"
    assert [option["id"] for option in suggestion["clarification"]["options"]] == [
        "travel",
        "expense",
    ]

    # 語の出現だけでは回答済みにしない。意味を認識できない場合は確認を保つ。
    answered = client.post(
        f"{BASE}/clarifications/suggest", json={"query": "出張の申請の期限はいつですか"}
    ).json()["data"]
    assert answered["suggestion"] is not None
    # ルールに一致しない質問にも出さない。
    unrelated = client.post(f"{BASE}/clarifications/suggest", json={"query": "日当はいくら"})
    assert unrelated.json()["data"]["suggestion"] is None

    # 外すと出さない。
    client.put(f"{BASE}/runtime-knowledge/rules/R01/clarification", json={"clarification": None})
    removed = client.post(f"{BASE}/clarifications/suggest", json={"query": "申請の期限は"})
    assert removed.json()["data"]["suggestion"] is None


def test_assume_policy_does_not_suggest_clarifications(
    fake_oracle: FakeKnowledgeOracle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """方針が「前提を示して回答する」なら、ルールに当たっても確認を出さない（#1839）。"""
    from app.rag.search_answer_profile_config import SearchAnswerProfileConfig

    client.put(
        f"{BASE}/runtime-knowledge/rules/R01/clarification", json={"clarification": CLARIFICATION}
    )
    original = fake_oracle.get_search_answer_profile

    async def assume(search_answer_profile_id: str) -> Any:
        view = await original(search_answer_profile_id)
        assert view is not None
        return view.model_copy(
            update={
                "config": SearchAnswerProfileConfig(
                    knowledge_base_ids=["kb-1"], clarification_policy="assume"
                )
            }
        )

    monkeypatch.setattr(fake_oracle, "get_search_answer_profile", assume)
    data = client.post(
        f"{BASE}/clarifications/suggest", json={"query": "申請の期限はいつですか"}
    ).json()["data"]
    assert data["suggestion"] is None


def test_clarification_validation(fake_oracle: FakeKnowledgeOracle) -> None:
    url = f"{BASE}/runtime-knowledge/rules/R01/clarification"
    one_option = {**CLARIFICATION, "options": CLARIFICATION["options"][:1]}
    assert client.put(url, json={"clarification": one_option}).status_code == 422
    duplicate = {**CLARIFICATION, "options": [CLARIFICATION["options"][0]] * 2}
    assert client.put(url, json={"clarification": duplicate}).status_code == 422
    missing = client.put(
        f"{BASE}/runtime-knowledge/rules/R99/clarification", json={"clarification": CLARIFICATION}
    )
    assert missing.status_code == 404


def _payload() -> dict[str, Any]:
    return {"rules": [{"id": "R01", "title": "期限の確認", "clarification": CLARIFICATION}]}


def test_resolve_clarification_builds_the_scope_and_page_ranges() -> None:
    resolved = resolve_clarification(
        _payload(),
        ClarificationAnswer(rule_id="R01", option_ids=["travel"], other_text="海外出張"),
    )
    assert resolved is not None
    scope, page_ranges = resolved
    assert json.loads(page_ranges) == [
        {"document_id": "doc-travel", "page_start": 2, "page_end": 3}
    ]
    assert scope.search_terms == ("出張", "日当")
    assert scope.label == "「出張旅費規程.pdf」の「第6条 申請と精算」（p.2–3）"
    assert "選んだ答え: 出張旅費" in scope.context
    assert "前提: 利用者は出張旅費規程について質問している" in scope.context
    assert "その他（利用者の入力）: 海外出張" in scope.context

    # 確認を保存した後に章節のページが変わったら、今のページで絞る(#721)。
    moved = resolve_clarification(
        _payload(),
        ClarificationAnswer(rule_id="R01", option_ids=["travel"]),
        {("doc-travel", "sec-6"): (4, 5), ("doc-travel", "other"): (9, 9)},
    )
    assert moved is not None
    assert json.loads(moved[1]) == [{"document_id": "doc-travel", "page_start": 4, "page_end": 5}]
    assert moved[0].label.endswith("（p.4–5）")

    # 章節の無い選択肢は範囲を絞らない。
    expense = resolve_clarification(
        _payload(), ClarificationAnswer(rule_id="R01", option_ids=["expense"])
    )
    assert expense is not None and expense[1] == ""


@pytest.mark.parametrize(
    "answer",
    [
        ClarificationAnswer(rule_id="R99", option_ids=["travel"]),
        ClarificationAnswer(rule_id="R01", option_ids=["unknown"]),
    ],
    ids=["unknown-rule", "unknown-option"],
)
def test_resolve_clarification_rejects_unknown_rules_and_options(
    answer: ClarificationAnswer,
) -> None:
    assert resolve_clarification(_payload(), answer) is None


def test_other_text_is_used_even_when_the_rule_saved_allow_other_false() -> None:
    """「その他（自由入力）」は常に出す（#1839）。古い保存の allow_other=False でも受け付ける。"""
    payload = {"rules": [{"id": "R01", "clarification": {**CLARIFICATION, "allow_other": False}}]}
    resolved = resolve_clarification(
        payload, ClarificationAnswer(rule_id="R01", other_text="海外出張の日当")
    )
    assert resolved is not None
    assert "その他（利用者の入力）: 海外出張の日当" in resolved[0].context


def test_clarification_record_for_the_conversation() -> None:
    """会話に残す「回答前に確認した内容」（#1839）。名前は保存済みのルールから引き直す。"""
    answer = ClarificationAnswer(rule_id="R01", option_ids=["travel"], other_text=" 海外 ")
    assert clarification_record(_payload(), "R01", answer) == {
        "outcome": "answered",
        "entries": [
            {
                "prompt": CLARIFICATION["question"],
                "selected": ["出張旅費"],
                "other_text": "海外",
            }
        ],
    }
    assert clarification_record(_payload(), "R01", None) == {
        "outcome": "skipped",
        "entries": [{"prompt": CLARIFICATION["question"]}],
    }
    assert clarification_record(_payload(), "R99", None) is None


def test_clarification_record_marks_free_text_only_rules() -> None:
    """選択肢の無い確認のルールの答えは、記録に `free_text_only` を残す（#3410）。"""
    payload = {
        "rules": [{"id": "R01", "clarification": {"question": "対象の名前は？", "options": []}}]
    }
    answer = ClarificationAnswer(rule_id="R01", other_text=" 名前 A ")
    assert clarification_record(payload, "R01", answer) == {
        "outcome": "answered",
        "entries": [
            {
                "prompt": "対象の名前は？",
                "selected": [],
                "other_text": "名前 A",
                "free_text_only": True,
            }
        ],
    }


def test_single_choice_rejects_multiple_options() -> None:
    payload = {"rules": [{"id": "R01", "clarification": {**CLARIFICATION, "multiple": False}}]}
    answer = ClarificationAnswer(rule_id="R01", option_ids=["travel", "expense"])
    assert resolve_clarification(payload, answer) is None


# ---- MCP の rag_search のルールの確認（Agent から呼ぶとき。#2117）-------------------------


def test_rule_answer_splits_labels_and_other_text() -> None:
    """Agent が返す「表示名、自由入力」の値を、選択肢と自由入力に分ける。"""
    payload = _payload()
    both = rule_answer(payload, "R01", "出張旅費、経費精算")
    assert both is not None and (both.option_ids, both.other_text) == (["travel", "expense"], "")
    other = rule_answer(payload, "R01", "出張旅費、海外出張")
    assert other is not None and (other.option_ids, other.other_text) == (["travel"], "海外出張")
    free = rule_answer(payload, "R01", "海外出張")
    assert free is not None and (free.option_ids, free.other_text) == ([], "海外出張")
    # 1 つだけ選ぶ確認は先頭の選択肢だけを使い、残りを自由入力にする。
    single = {"rules": [{"id": "R01", "clarification": {**CLARIFICATION, "multiple": False}}]}
    first = rule_answer(single, "R01", "出張旅費、経費精算")
    assert first is not None and (first.option_ids, first.other_text) == (["travel"], "経費精算")
    assert rule_answer(payload, "R99", "出張旅費") is None
    assert rule_answer(payload, "R01", "、 ") is None


@pytest.fixture
def mcp_search(
    fake_oracle: FakeKnowledgeOracle, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, Any]]:
    """確認を持つルール R01 と、rag_search の検索の呼び出しを記録する fake。"""
    client.put(
        f"{BASE}/runtime-knowledge/rules/R01/clarification", json={"clarification": CLARIFICATION}
    )
    calls: list[dict[str, Any]] = []

    async def fake_run(request: SearchRequest, **kwargs: Any) -> SearchResponse:
        calls.append({"request": request, **kwargs})
        return SearchResponse(
            answer="回答",
            citations=[],
            trace_id="t-1",
            elapsed_ms=1.0,
            diagnostics=SearchDiagnostics(),
        )

    async def no_sections(*_args: object) -> None:
        return None

    monkeypatch.setattr(mcp_tools, "OracleClient", lambda: fake_oracle)
    monkeypatch.setattr(search_route, "_run_search_with_timeout", fake_run)
    monkeypatch.setattr(document_sections_service, "document_sections", no_sections)
    return calls


def _search_headers(production: ProductionAuth) -> dict[str, str]:
    user = production.user_with_permissions(
        "searcher", ["menu.search"], search_answer_profile_ids=["bv-1"], knowledge_base_ids=["kb-1"]
    )
    return _token(user.user_uuid)


def test_mcp_search_asks_the_rule_clarification_before_searching(
    mcp_search: list[dict[str, Any]],
    auth: ProductionAuth,  # noqa: F811
) -> None:
    """方針が確認なら、RAG のチャットと同じルールの確認を、検索・回答をせずに返す。"""
    body = _call(
        "rag_search",
        {"query": "申請の期限はいつですか", "search_answer_profile_id": "bv-1"},
        _search_headers(auth),
    )["structuredContent"]
    assert body["outcome"] == "needs_clarification"
    assert body["clarifications"] == [
        {
            "condition_id": "rule:R01",
            "label": "期限の確認",
            "question": CLARIFICATION["question"],
            "options": ["出張旅費", "経費精算"],
            "multiple": True,
        }
    ]
    assert body["evidence"] == []
    assert mcp_search == []


def test_mcp_search_uses_the_rule_answer_as_scope_and_page_ranges(
    mcp_search: list[dict[str, Any]],
    auth: ProductionAuth,  # noqa: F811
) -> None:
    body = _call(
        "rag_search",
        {
            "query": "申請の期限はいつですか",
            "search_answer_profile_id": "bv-1",
            "conditions": {"rule:R01": "出張旅費、海外出張"},
        },
        _search_headers(auth),
    )["structuredContent"]
    assert body["outcome"] != "needs_clarification"
    (call,) = mcp_search
    assert "選んだ答え: 出張旅費" in call["scope"].context
    assert "その他（利用者の入力）: 海外出張" in call["scope"].context
    assert json.loads(call["request"].filters["page_ranges"]) == [
        {"document_id": "doc-travel", "page_start": 2, "page_end": 3}
    ]
    # ルールの答えは業務ガイドの条件として渡さない。
    assert call["request"].conditions == {}


@pytest.mark.parametrize(
    "arguments",
    [
        {"query": "申請の期限はいつですか", "skip_clarification": True},
        {"query": "日当はいくら"},
        # 会話の前の答えは、ルールに一致しない質問に当てない。
        {"query": "日当はいくら", "conditions": {"rule:R01": "出張旅費"}},
    ],
    ids=["skip", "no-rule", "answer-for-another-question"],
)
def test_mcp_search_without_the_rule_clarification(
    mcp_search: list[dict[str, Any]],
    auth: ProductionAuth,  # noqa: F811
    arguments: dict[str, Any],
) -> None:
    body = _call(
        "rag_search", {"search_answer_profile_id": "bv-1", **arguments}, _search_headers(auth)
    )["structuredContent"]
    assert body["outcome"] != "needs_clarification"
    (call,) = mcp_search
    assert call["scope"] is None
    assert "page_ranges" not in call["request"].filters


def test_mcp_search_assume_policy_does_not_ask(
    mcp_search: list[dict[str, Any]],
    auth: ProductionAuth,  # noqa: F811
    fake_oracle: FakeKnowledgeOracle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.rag.search_answer_profile_config import SearchAnswerProfileConfig

    original = fake_oracle.get_search_answer_profile

    async def assume(search_answer_profile_id: str) -> Any:
        view = await original(search_answer_profile_id)
        assert view is not None
        return view.model_copy(
            update={
                "config": SearchAnswerProfileConfig(
                    knowledge_base_ids=["kb-1"], clarification_policy="assume"
                )
            }
        )

    monkeypatch.setattr(fake_oracle, "get_search_answer_profile", assume)
    body = _call(
        "rag_search",
        {"query": "申請の期限はいつですか", "search_answer_profile_id": "bv-1"},
        _search_headers(auth),
    )["structuredContent"]
    assert body["outcome"] != "needs_clarification"
    assert len(mcp_search) == 1


def test_disabled_rule_stops_generation_and_clarification_without_losing_settings(
    fake_oracle: FakeKnowledgeOracle, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import Settings

    monkeypatch.setattr(
        knowledge_route, "get_settings", lambda: Settings(rag_support_guide_enabled=False)
    )
    created = client.post(
        f"{BASE}/runtime-knowledge/edit",
        json={
            "kind": "rules",
            "title": "合成ルール A",
            "labels": "合成語",
            "content": "合成の注意点。",
        },
    )
    identity = created.json()["data"]["rules"][-1]["id"]
    clarification = {
        "question": "対象を選んでください。",
        "options": [{"id": "a", "label": "選択肢 A"}, {"id": "b", "label": "選択肢 B"}],
    }
    saved = client.put(
        f"{BASE}/runtime-knowledge/rules/{identity}/clarification",
        json={"clarification": clarification},
    )
    assert saved.status_code == 200
    stored = saved.json()["data"]["rules"][-1]["clarification"]
    for enabled in (True, False, True):
        response = client.post(
            f"{BASE}/runtime-knowledge/edit",
            json={
                "kind": "rules",
                "selected": identity,
                "title": "合成ルール A",
                "labels": "合成語",
                "content": "合成の注意点。",
                "enabled": enabled,
            },
        )
        assert response.status_code == 200
        rule = response.json()["data"]["rules"][-1]
        assert rule["id"] == identity
        assert rule["clarification"] == stored
        preview = client.post(f"{BASE}/runtime-knowledge/preview", json={"question": "合成語"})
        assert preview.json()["data"]["matched_rules"] == (["合成ルール A"] if enabled else [])
        suggestion = client.post(f"{BASE}/clarifications/suggest", json={"query": "合成語"})
        assert suggestion.status_code == 200
        assert (suggestion.json()["data"]["suggestion"] is not None) is enabled
