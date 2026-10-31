"""回答設定のモデルの継承・単一要求の優先・登録削除を合成の設定で検証する。"""

import pytest

from app.api.routes import chat as chat_route
from app.api.routes import search as search_route
from app.api.routes import search_answer_profiles as profile_route
from app.config import Settings, get_settings
from app.main import app
from app.rag.search_answer_profile_config import (
    ProfileAnswerModelUnavailableError,
    SearchAnswerProfileConfig,
    resolve_search_answer_profile_settings,
)
from app.schemas.chat import ChatMessageRequest
from app.schemas.search import SearchRequest
from tests.support import AsgiTestClient


def settings() -> Settings:
    return get_settings().model_copy(
        update={
            "oci_enterprise_ai_default_text_model": "model-a",
            "oci_enterprise_ai_default_vision_model": "model-b",
            "oci_enterprise_ai_models": [
                {"model_id": "model-a", "display_name": "モデル A"},
                {"model_id": "model-b", "display_name": "モデル B", "vision_enabled": True},
                {"model_id": "model-c", "display_name": "モデル C"},
            ],
        }
    )


def test_profile_selects_registered_model_without_mutating_global_defaults() -> None:
    original = settings()
    effective, applied = resolve_search_answer_profile_settings(
        original, SearchAnswerProfileConfig(answer_model_id="model-c")
    )
    assert applied
    assert effective.oci_enterprise_ai_default_text_model == "model-c"
    assert original.oci_enterprise_ai_default_text_model == "model-a"
    assert effective.oci_enterprise_ai_default_vision_model == "model-b"
    assert (
        search_route._answer_model_id(
            SearchRequest(query="操作方法", model_id="model-a"), effective
        )
        == "model-a"
    )
    assert (
        chat_route._resolve_compare_models(ChatMessageRequest(content="操作方法"), effective)[0][
            "model_id"
        ]
        == "model-c"
    )
    assert (
        chat_route._resolve_compare_models(
            ChatMessageRequest(content="操作方法", model_ids=["model-a"]), effective
        )[0]["model_id"]
        == "model-a"
    )


@pytest.mark.parametrize("value", [None, "", "   "])
def test_empty_selection_keeps_inherited_defaults(value: str | None) -> None:
    original = settings()
    effective, applied = resolve_search_answer_profile_settings(
        original, SearchAnswerProfileConfig(answer_model_id=value)
    )
    assert effective is original
    assert not applied


def test_removed_model_has_actionable_error_but_does_not_block_retrieval_only() -> None:
    config = SearchAnswerProfileConfig(answer_model_id="removed-model")
    with pytest.raises(ProfileAnswerModelUnavailableError, match="選び直す"):
        resolve_search_answer_profile_settings(settings(), config)
    effective, _ = resolve_search_answer_profile_settings(
        settings(), config, apply_answer_model=False
    )
    assert effective.oci_enterprise_ai_default_text_model == "model-a"


def test_candidates_only_return_public_model_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(profile_route, "get_settings", settings)
    response = AsgiTestClient(app).get("/api/search-answer-profiles/answer-models")
    assert response.status_code == 200
    candidates = response.json()["data"]
    assert [row["model_id"] for row in candidates] == ["model-a", "model-b", "model-c"]
    assert all(set(row) == {"model_id", "display_name", "kind"} for row in candidates)


def test_saving_unregistered_model_returns_422_before_database_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(profile_route, "get_settings", settings)
    response = AsgiTestClient(app).post(
        "/api/search-answer-profiles",
        json={
            "name": "回答設定 A",
            "description": "操作の質問に回答する",
            "config": {"answer_model_id": "removed-model"},
        },
    )
    assert response.status_code == 422
    assert "選び直す" in "".join(response.json()["error_messages"])


@pytest.mark.usefixtures("system_access")
@pytest.mark.parametrize(
    "profile_model,explicit,generate,expected",
    [
        ("model-c", None, True, "model-c"),
        ("model-c", "model-a", True, "model-a"),
        ("removed-model", "model-a", True, "model-a"),
        ("removed-model", None, False, "model-a"),
        # 回答を作らない検索も、根拠を集める LLM はチャットと同じ回答のモデルで呼ぶ（#3506）。
        ("model-c", None, False, "model-c"),
    ],
)
async def test_query_resolution_applies_request_then_profile_then_defaults(
    monkeypatch: pytest.MonkeyPatch,
    profile_model: str,
    explicit: str | None,
    generate: bool,
    expected: str,
) -> None:
    from app.schemas.search_answer_profile import SearchAnswerProfileDetail
    from tests.test_search_answer_profile_domain_keywords import FakeKnowledgeOracle

    class Oracle(FakeKnowledgeOracle):
        async def get_search_answer_profile(
            self, profile_id: str
        ) -> SearchAnswerProfileDetail | None:
            detail = await super().get_search_answer_profile(profile_id)
            if detail is not None:
                detail.config.answer_model_id = profile_model
            return detail

    async def no_guides(_oracle: object, _profile: str) -> tuple[list[object], bool]:
        return [], False

    fake = Oracle()
    monkeypatch.setattr(search_route, "OracleClient", lambda: fake)
    monkeypatch.setattr(search_route, "load_published_guides", no_guides)
    request = SearchRequest(
        query="操作方法",
        search_answer_profile_id="bv-1",
        model_id=explicit,
        generate_answer=generate,
    )
    _, effective, _, _ = await search_route._resolve_query_context(request, settings())
    assert effective.oci_enterprise_ai_default_text_model == expected
