"""質問理解の範囲・保存契約・認識結果。外部モデルは呼ばない。"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from rag_engine.retrieval.inquiry_conditions import parse_inquiry_conditions
from rag_engine.retrieval.task_contract import task_contract
from rag_parser_core.language import bind_pack, builtin_pack, current_pack, resolve

from app.api.routes import question_understanding as routes
from app.config import Settings, get_settings
from app.main import app
from app.rag.question_understanding import profile_language_scope, replace_question_understanding
from app.rag.search_answer_profile_config import (
    SearchAnswerProfileConfig,
    resolve_search_answer_profile_settings,
)
from app.schemas.search_answer_profile import SearchAnswerProfileDetail, SearchAnswerProfileStatus
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


def _view(
    profile_id: str = "profile-a", overrides: dict[str, dict[str, object]] | None = None
) -> SearchAnswerProfileDetail:
    return SearchAnswerProfileDetail(
        id=profile_id,
        name="Profile A",
        description="合成の設定",
        status=SearchAnswerProfileStatus.ACTIVE,
        config=SearchAnswerProfileConfig(
            knowledge_base_ids=["kb-a"], language_pack=overrides or {}
        ),
        knowledge_bases=[],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def oracle(monkeypatch: pytest.MonkeyPatch) -> Any:
    class Fake:
        view = _view(overrides={"answer_labels": {"citation_prefix": "参照"}})
        writes = 0

        async def get_search_answer_profile(
            self, profile_id: str
        ) -> SearchAnswerProfileDetail | None:
            return self.view if profile_id == self.view.id else None

        async def update_question_understanding(
            self, profile_id: str, overrides: dict[str, dict[str, object]]
        ) -> SearchAnswerProfileDetail:
            assert profile_id == self.view.id
            self.writes += 1
            config = SearchAnswerProfileConfig.model_validate(
                {
                    **self.view.config.model_dump(),
                    "language_pack": replace_question_understanding(
                        self.view.config.language_pack, overrides
                    ),
                }
            )
            self.view = self.view.model_copy(update={"config": config})
            return self.view

    fake = Fake()
    monkeypatch.setattr(routes, "OracleClient", lambda: fake)
    return fake


def test_profile_only_metadata_and_preview_use_draft_without_saving(oracle: Any) -> None:
    endpoint = "/api/search-answer-profiles/profile-a/question-understanding"
    initial = client.get(endpoint)
    assert initial.status_code == 200
    data = initial.json()["data"]
    assert data["overrides"] == {}
    assert all(not field["ingest"] for field in data["fields"])
    assert "answer_labels" not in data["effective"]
    response = client.post(
        endpoint + "/preview",
        json={
            "text": "対象 A の理屈を教えてください。",
            "overrides": {"question_cues": {"cause_cues": ["理屈"]}},
        },
    )
    assert response.status_code == 200
    result = response.json()["data"]
    assert result["original_text"] == "対象 A の理屈を教えてください。"
    assert result["saved"]["hash"] != result["draft"]["hash"]
    assert result["draft"]["task_contract"]["asks_cause"] is True
    assert oracle.writes == 0


def test_save_reset_empty_replacement_and_other_fields_preserved(oracle: Any) -> None:
    endpoint = "/api/search-answer-profiles/profile-a/question-understanding"
    saved = client.put(endpoint, json={"overrides": {"question_cues": {"cause_cues": ["理屈"]}}})
    assert saved.status_code == 200
    assert saved.json()["data"]["overrides"] == {"question_cues": {"cause_cues": ["理屈"]}}
    assert oracle.view.config.language_pack["answer_labels"] == {"citation_prefix": "参照"}
    empty = client.put(endpoint, json={"overrides": {"question_cues": {"cause_cues": []}}})
    assert empty.status_code == 200
    assert empty.json()["data"]["effective"]["question_cues"]["cause_cues"] == []
    reset = client.put(endpoint, json={"overrides": {}})
    assert reset.status_code == 200
    assert reset.json()["data"]["overrides"] == {}
    assert oracle.view.config.language_pack == {"answer_labels": {"citation_prefix": "参照"}}
    assert (
        client.get("/api/search-answer-profiles/profile-b/question-understanding").status_code
        == 404
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"question_cues": {"unknown": ["x"]}},
        {"numbers": {"article_units": ["x"]}},
        {"answer_labels": {"citation_prefix": ["x"]}},
        {"question_cues": {"cause_cues": ["x" * 41]}},
        {"question_cues": {"cause_cues": [str(i) for i in range(301)]}},
        {"question_cues": {"cause_cues": "not-a-list"}},
        {"inquiry_cues": {"decision_words": ["x=unknown"]}},
    ],
)
def test_invalid_settings_fail_without_writing(oracle: Any, overrides: dict[str, object]) -> None:
    result = client.put(
        "/api/search-answer-profiles/profile-a/question-understanding",
        json={"overrides": overrides},
    )
    assert result.status_code == 422
    assert oracle.writes == 0


def test_scope_isolated_across_concurrent_requests_nested_profiles_and_failures() -> None:
    global_hash = current_pack().hash
    a, _ = resolve_search_answer_profile_settings(
        get_settings(), _view(overrides={"question_cues": {"cause_cues": ["理屈"]}}).config
    )
    b, _ = resolve_search_answer_profile_settings(a, _view("profile-b").config)
    assert b.rag_language_pack_overrides == {}

    async def probe(settings: Settings, expected: bool) -> None:
        with profile_language_scope(settings) as pack:
            await asyncio.sleep(0)
            assert task_contract("対象 A の理屈を教えてください。")["asks_cause"] is expected
            parsed = parse_inquiry_conditions("対象 A の理屈を教えてください。")
            assert parsed.to_payload()["language_pack_hash"] == pack.hash
            assert current_pack().hash == pack.hash

    async def run() -> None:
        await asyncio.gather(probe(a, True), probe(b, False))

    asyncio.run(run())
    with profile_language_scope(a):
        with profile_language_scope(b):
            assert current_pack().hash == global_hash
        assert current_pack().hash != global_hash
    with pytest.raises(RuntimeError), profile_language_scope(a):
        raise RuntimeError("synthetic")
    assert current_pack().hash == global_hash
    # 継承元の全体設定を要求中の他プロファイルから作らない。
    with (
        bind_pack(resolve(builtin_pack(), {"question_cues": {"cause_cues": ["合成"]}})),
        profile_language_scope(b),
    ):
        assert current_pack().hash == global_hash


def test_search_api_binds_question_language_before_assumptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.routes import search as search_route
    from tests import test_search_search_answer_profile as search_tests

    config = SearchAnswerProfileConfig(
        knowledge_base_ids=["kb-1"],
        language_pack={"question_cues": {"cause_cues": ["理屈"]}},
    )
    search_tests._install(monkeypatch, {"profile-a": config})
    from app.rag.answer_assumptions import with_answer_assumptions

    original = with_answer_assumptions
    observed: list[bool] = []

    def prepare(*args: Any, **kwargs: Any) -> Any:
        observed.append(task_contract("対象 A の理屈を教えてください。")["asks_cause"])
        return original(*args, **kwargs)

    monkeypatch.setattr(search_route, "with_answer_assumptions", prepare)
    response = client.post(
        "/api/search",
        json={"query": "対象 A の理屈を教えてください。", "search_answer_profile_id": "profile-a"},
    )
    assert response.status_code == 200
    assert observed == [True]
    assert task_contract("対象 A の理屈を教えてください。")["asks_cause"] is False


def test_question_settings_require_profile_management_permission(
    oracle: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.security_support import enable_production_auth, login

    auth = enable_production_auth(monkeypatch)
    auth.user_with_permissions(
        "synthetic-reader", ["menu.search"], search_answer_profile_ids=["profile-a"]
    )
    restricted = AsgiTestClient(app)
    headers = login(restricted, "synthetic-reader")
    path = "/api/search-answer-profiles/profile-a/question-understanding"
    assert restricted.get(path, headers=headers).status_code == 403
    assert restricted.put(path, json={"overrides": {}}, headers=headers).status_code == 403
    assert (
        restricted.post(
            path + "/preview", json={"overrides": {}, "text": "合成"}, headers=headers
        ).status_code
        == 403
    )
    assert oracle.writes == 0
