"""検索・回答プロファイル(Search Answer Profile)API のテスト。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.api.routes import search_answer_profiles as search_answer_profiles_route
from app.main import app
from app.rag.search_answer_profile_config import (
    PROFILE_GLOBAL_QUERY_FIELDS,
    SearchAnswerProfileConfig,
    parse_search_answer_profile_config,
)
from app.schemas.knowledge_base import DESCRIPTION_REQUIRED_MESSAGE, KnowledgeBaseStatus
from app.schemas.search_answer_profile import (
    DEFAULT_SEARCH_ANSWER_PROFILE_DESCRIPTION,
    DEFAULT_SEARCH_ANSWER_PROFILE_NAME,
    SearchAnswerProfileDetail,
    SearchAnswerProfileKnowledgeBaseRef,
    SearchAnswerProfileStatus,
)
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


class FakeSearchAnswerProfileOracle:
    """search answer profile API テスト用のインメモリ fake。"""

    def __init__(self) -> None:
        self.views: dict[str, SearchAnswerProfileDetail] = {}
        self.knowledge_bases: dict[str, str] = {
            "kb-default": "DEFAULT",
            "kb-1": "社内規程",
            "kb-2": "製品 FAQ",
        }

    async def create_search_answer_profile(
        self,
        *,
        name: str,
        description: str | None = None,
        config: SearchAnswerProfileConfig | None = None,
    ) -> SearchAnswerProfileDetail:
        resolved = config or SearchAnswerProfileConfig()
        detail = SearchAnswerProfileDetail(
            id=f"bv-{uuid4().hex[:8]}",
            name=name,
            description=description,
            status=SearchAnswerProfileStatus.ACTIVE,
            knowledge_base_count=len(resolved.normalized_knowledge_base_ids()),
            config=resolved,
            knowledge_bases=self._refs(resolved),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        self.views[detail.id] = detail
        return detail

    async def ensure_default_search_answer_profile(self) -> SearchAnswerProfileDetail:
        existing = next(
            (
                view
                for view in self.views.values()
                if view.name.casefold() == DEFAULT_SEARCH_ANSWER_PROFILE_NAME.casefold()
            ),
            None,
        )
        if existing is None:
            return await self.create_search_answer_profile(
                name=DEFAULT_SEARCH_ANSWER_PROFILE_NAME,
                description=DEFAULT_SEARCH_ANSWER_PROFILE_DESCRIPTION,
                config=SearchAnswerProfileConfig(knowledge_base_ids=["kb-default"]),
            )
        config = existing.config.model_copy(update={"knowledge_base_ids": ["kb-default"]})
        normalized = existing.model_copy(
            update={
                "description": existing.description or DEFAULT_SEARCH_ANSWER_PROFILE_DESCRIPTION,
                "status": SearchAnswerProfileStatus.ACTIVE,
                "archived_at": None,
                "config": config,
                "knowledge_base_count": 1,
                "knowledge_bases": self._refs(config),
            }
        )
        self.views[existing.id] = normalized
        return normalized

    async def list_search_answer_profiles(
        self,
        *,
        status: SearchAnswerProfileStatus | None = None,
        query: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SearchAnswerProfileDetail]:
        items = [
            view
            for view in self.views.values()
            if (status is None or view.status == status)
            and (query is None or query.casefold() in view.name.casefold())
        ]
        items.sort(
            key=lambda view: view.name.casefold() != DEFAULT_SEARCH_ANSWER_PROFILE_NAME.casefold()
        )
        return items[offset : (offset + limit) if limit is not None else None]

    async def count_search_answer_profiles(
        self,
        *,
        status: SearchAnswerProfileStatus | None = None,
        query: str | None = None,
    ) -> int:
        return len(await self.list_search_answer_profiles(status=status, query=query))

    async def get_search_answer_profile(
        self, search_answer_profile_id: str
    ) -> SearchAnswerProfileDetail | None:
        return self.views.get(search_answer_profile_id)

    async def update_search_answer_profile(
        self,
        search_answer_profile_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        config: SearchAnswerProfileConfig | None = None,
        update_fields: set[str] | None = None,
    ) -> SearchAnswerProfileDetail:
        existing = self.views.get(search_answer_profile_id)
        if existing is None:
            raise KeyError(search_answer_profile_id)
        fields = update_fields or set()
        if existing.name.casefold() == DEFAULT_SEARCH_ANSWER_PROFILE_NAME.casefold():
            if "name" in fields:
                raise ValueError("DEFAULT 検索・回答プロファイルの名前は変更できません。")
            if (
                "config" in fields
                and config is not None
                and config.knowledge_base_ids != ["kb-default"]
            ):
                raise ValueError("DEFAULT 検索・回答プロファイルの参照 KB は変更できません。")
        updates: dict[str, object] = {}
        if "name" in fields and name is not None:
            updates["name"] = name
        if "description" in fields:
            updates["description"] = description
        if "config" in fields and config is not None:
            updates["config"] = config
            updates["knowledge_base_count"] = len(config.normalized_knowledge_base_ids())
            updates["knowledge_bases"] = self._refs(config)
        updated = existing.model_copy(update=updates)
        self.views[search_answer_profile_id] = updated
        return updated

    async def archive_search_answer_profile(
        self, search_answer_profile_id: str
    ) -> SearchAnswerProfileDetail:
        existing = self.views.get(search_answer_profile_id)
        if existing is None:
            raise KeyError(search_answer_profile_id)
        if existing.name.casefold() == DEFAULT_SEARCH_ANSWER_PROFILE_NAME.casefold():
            raise ValueError("DEFAULT 検索・回答プロファイルはアーカイブできません。")
        archived = existing.model_copy(update={"status": SearchAnswerProfileStatus.ARCHIVED})
        self.views[search_answer_profile_id] = archived
        return archived

    def _refs(self, config: SearchAnswerProfileConfig) -> list[SearchAnswerProfileKnowledgeBaseRef]:
        return [
            SearchAnswerProfileKnowledgeBaseRef(
                id=kb_id, name=self.knowledge_bases[kb_id], status=KnowledgeBaseStatus.ACTIVE
            )
            for kb_id in config.normalized_knowledge_base_ids()
            if kb_id in self.knowledge_bases
        ]


@pytest.fixture
def fake_oracle(monkeypatch: pytest.MonkeyPatch) -> FakeSearchAnswerProfileOracle:
    """search answer profile router の OracleClient を fake へ差し替える。"""
    fake = FakeSearchAnswerProfileOracle()
    monkeypatch.setattr(search_answer_profiles_route, "OracleClient", lambda: fake)
    return fake


def test_create_and_get_search_answer_profile(fake_oracle: FakeSearchAnswerProfileOracle) -> None:
    """複数 KB と回答の設定を束ねて作成し、参照 KB 名が解決される。"""
    resp = client.post(
        "/api/search-answer-profiles",
        json={
            "name": " 経理アシスタント ",
            "description": "経理規程の問い合わせ窓口",
            "config": {
                "knowledge_base_ids": ["kb-1", "kb-2"],
                "query": {
                    "max_anchors_per_document": 3,
                    "vector_index_profile": "accurate",
                    # 検索・回答プロファイルは品質評価を上書きしない。送られても保存しない(#301)。
                    "evaluation_suite": "strict_ci",
                    # 旧 standard の回答スタイル(#595 で削除)。送られても保存しない。
                    "generation_profile": "detailed_cited",
                },
                # 旧 standard の persona(#595 で削除)。送られても保存しない。
                "system_prompt": "あなたは経理規程アシスタントです。",
                "default_language": "日本語",
            },
        },
    )

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["name"] == "経理アシスタント"
    assert data["knowledge_base_count"] == 2
    assert data["config"]["query"]["max_anchors_per_document"] == 3
    assert "vector_index_profile" not in data["config"]["query"]
    assert "evaluation_suite" not in data["config"]["query"]
    assert "generation_profile" not in data["config"]["query"]
    assert "system_prompt" not in data["config"]
    assert "default_language" not in data["config"]
    assert [ref["name"] for ref in data["knowledge_bases"]] == ["社内規程", "製品 FAQ"]

    get_resp = client.get(f"/api/search-answer-profiles/{data['id']}")
    assert get_resp.status_code == 200
    assert get_resp.json()["data"]["id"] == data["id"]


def test_profile_api_does_not_accept_or_return_global_only_query_fields(
    fake_oracle: FakeSearchAnswerProfileOracle,
) -> None:
    """全体設定だけの項目(PROFILE_GLOBAL_QUERY_FIELDS)は送られても保存・応答しない(#3523)。"""
    resp = client.post(
        "/api/search-answer-profiles",
        json={
            "name": "プロファイル A",
            "description": "合成の設定の検証用",
            "config": {
                "knowledge_base_ids": ["kb-1"],
                "query": {
                    "max_anchors_per_document": 3,
                    "graph_expansion_max_chunks": 3,
                    "guide_step_max_queries": 7,
                    "guide_step_deadline_seconds": 37.5,
                },
            },
        },
    )

    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    get_resp = client.get(f"/api/search-answer-profiles/{data['id']}")
    assert get_resp.status_code == 200
    for query in (data["config"]["query"], get_resp.json()["data"]["config"]["query"]):
        assert query["max_anchors_per_document"] == 3
        assert not set(PROFILE_GLOBAL_QUERY_FIELDS) & set(query)


def test_list_search_answer_profiles(fake_oracle: FakeSearchAnswerProfileOracle) -> None:
    """作成した検索・回答プロファイルを一覧・検索できる。"""
    client.post(
        "/api/search-answer-profiles",
        json={"name": "経理アシスタント", "description": "経理アシスタントの説明"},
    )
    client.post(
        "/api/search-answer-profiles",
        json={"name": "営業アシスタント", "description": "営業アシスタントの説明"},
    )

    page = client.get("/api/search-answer-profiles?q=経理").json()["data"]
    assert page["total"] == 1
    assert page["items"][0]["name"] == "経理アシスタント"


def test_list_ensures_default_search_answer_profile(
    fake_oracle: FakeSearchAnswerProfileOracle,
) -> None:
    """初回一覧で DEFAULT KB だけを参照する DEFAULT 検索・回答プロファイルを冪等に保証する。"""
    client.post(
        "/api/search-answer-profiles",
        json={"name": "経理アシスタント", "description": "経理アシスタントの説明"},
    )
    first = client.get("/api/search-answer-profiles").json()["data"]
    second = client.get("/api/search-answer-profiles").json()["data"]

    assert first["total"] == second["total"] == 2
    assert first["items"][0]["name"] == "DEFAULT"
    default = next(view for view in fake_oracle.views.values() if view.name == "DEFAULT")
    assert default.status == SearchAnswerProfileStatus.ACTIVE
    assert default.config.knowledge_base_ids == ["kb-default"]
    assert [kb.name for kb in default.knowledge_bases] == ["DEFAULT"]
    # DEFAULT は改名できないため、既定の説明を持つ（#521）。
    assert default.description == DEFAULT_SEARCH_ANSWER_PROFILE_DESCRIPTION


@pytest.mark.parametrize("reserved_name", ["DEFAULT", "default", " DEFAULT "])
def test_default_is_a_reserved_search_answer_profile_name(
    fake_oracle: FakeSearchAnswerProfileOracle,
    reserved_name: str,
) -> None:
    """DEFAULT は大文字小文字・前後空白にかかわらずユーザー名に使えない。"""
    response = client.post(
        "/api/search-answer-profiles", json={"name": reserved_name, "description": "予約名の確認"}
    )

    assert response.status_code == 422
    assert "DEFAULT は予約名のため使用できません。" in response.json()["error_messages"][0]


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "経理ビュー"},
        {"name": "経理ビュー", "description": ""},
        {"name": "経理ビュー", "description": " \u3000\n"},
        {"name": "経理ビュー", "description": None},
    ],
    ids=["missing", "empty", "blank", "null"],
)
def test_create_search_answer_profile_requires_description(
    fake_oracle: FakeSearchAnswerProfileOracle,
    payload: dict[str, object],
) -> None:
    """検索・回答プロファイルの説明は必須。未指定・空・空白
    だけ・null は 422 で作成しない（#521）。"""
    response = client.post("/api/search-answer-profiles", json=payload)

    assert response.status_code == 422
    # 未指定は欄の位置付きの日本語、空・空白は自前の検証の文（#979 / #1065）。
    assert any(
        message.startswith("description: ") or message == DESCRIPTION_REQUIRED_MESSAGE
        for message in response.json()["error_messages"]
    )
    assert fake_oracle.views == {}


@pytest.mark.parametrize(
    "payload",
    [{"name": ""}, {"name": "   "}],
    ids=["empty", "blank"],
)
def test_create_search_answer_profile_requires_name(
    fake_oracle: FakeSearchAnswerProfileOracle,
    payload: dict[str, object],
) -> None:
    """検索・回答プロファイルの名前は必須。空・空白だけは 422 で作成しない。"""
    response = client.post("/api/search-answer-profiles", json={**payload, "description": "説明"})

    assert response.status_code == 422
    assert fake_oracle.views == {}


def test_create_search_answer_profile_trims_description(
    fake_oracle: FakeSearchAnswerProfileOracle,
) -> None:
    """説明の前後の空白は取り除いて保存する。"""
    response = client.post(
        "/api/search-answer-profiles",
        json={"name": "経理ビュー", "description": "  経理の問い合わせ  "},
    )

    assert response.status_code == 200
    assert response.json()["data"]["description"] == "経理の問い合わせ"


@pytest.mark.parametrize(
    "description",
    ["", "   ", None],
    ids=["empty", "blank", "null"],
)
def test_update_search_answer_profile_rejects_empty_description(
    fake_oracle: FakeSearchAnswerProfileOracle,
    description: str | None,
) -> None:
    """更新で説明を空・空白だけ・null にはできない（#521）。"""
    detail = client.post(
        "/api/search-answer-profiles",
        json={"name": "経理ビュー", "description": "経理の問い合わせ"},
    ).json()["data"]

    response = client.patch(
        f"/api/search-answer-profiles/{detail['id']}", json={"description": description}
    )

    assert response.status_code == 422
    assert "説明を入力してください。" in response.json()["error_messages"][0]
    assert fake_oracle.views[detail["id"]].description == "経理の問い合わせ"


def test_search_answer_profile_without_description_still_loads_and_updates_settings(
    fake_oracle: FakeSearchAnswerProfileOracle,
) -> None:
    """説明が必須になる前の説明なしの検索・回答プロファイルも読み込め、説明を送らない更新（設定だけ）もできる。"""
    legacy = SearchAnswerProfileDetail(
        id="bv-legacy",
        name="旧ビュー",
        description=None,
        status=SearchAnswerProfileStatus.ACTIVE,
        knowledge_base_count=1,
        config=SearchAnswerProfileConfig(knowledge_base_ids=["kb-1"]),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    fake_oracle.views[legacy.id] = legacy

    listed = client.get("/api/search-answer-profiles").json()["data"]["items"]
    assert next(item for item in listed if item["id"] == "bv-legacy")["description"] is None
    assert client.get("/api/search-answer-profiles/bv-legacy").json()["data"]["description"] is None

    update = client.patch(
        "/api/search-answer-profiles/bv-legacy",
        json={"config": {"knowledge_base_ids": ["kb-2"]}},
    )
    assert update.status_code == 200
    assert update.json()["data"]["knowledge_base_count"] == 1

    described = client.patch(
        "/api/search-answer-profiles/bv-legacy",
        json={"name": "旧ビュー", "description": "旧の用途"},
    )
    assert described.status_code == 200
    assert described.json()["data"]["description"] == "旧の用途"


def test_update_and_archive_search_answer_profile(
    fake_oracle: FakeSearchAnswerProfileOracle,
) -> None:
    """検索・回答プロファイルの更新とアーカイブができる。"""
    detail = client.post(
        "/api/search-answer-profiles",
        json={"name": "FAQ ビュー", "description": "FAQ ビューの説明"},
    ).json()["data"]

    update_resp = client.patch(
        f"/api/search-answer-profiles/{detail['id']}",
        json={"config": {"knowledge_base_ids": ["kb-1"]}},
    )
    assert update_resp.status_code == 200
    assert update_resp.json()["data"]["knowledge_base_count"] == 1

    archive_resp = client.post(f"/api/search-answer-profiles/{detail['id']}/archive")
    assert archive_resp.status_code == 200
    assert archive_resp.json()["data"]["status"] == "ARCHIVED"


def test_default_search_answer_profile_allows_settings_but_protects_identity_and_scope(
    fake_oracle: FakeSearchAnswerProfileOracle,
) -> None:
    """DEFAULT は設定だけ更新でき、名前・参照 KB・アーカイブは変更できない。"""
    default = client.get("/api/search-answer-profiles").json()["data"]["items"][0]
    search_answer_profile_id = default["id"]

    update = client.patch(
        f"/api/search-answer-profiles/{search_answer_profile_id}",
        json={
            "description": "全社共通の検索設定",
            "config": {
                "knowledge_base_ids": ["kb-default"],
                "query": {"max_anchors_per_document": 3},
            },
        },
    )
    assert update.status_code == 200
    assert update.json()["data"]["description"] == "全社共通の検索設定"
    assert update.json()["data"]["config"]["knowledge_base_ids"] == ["kb-default"]
    assert update.json()["data"]["config"]["query"]["max_anchors_per_document"] == 3

    # 画面の保存と同じ形（名前を送らず説明と設定を送る）で DEFAULT を保存できる（#521）。
    assert default["description"] == DEFAULT_SEARCH_ANSWER_PROFILE_DESCRIPTION
    empty_description = client.patch(
        f"/api/search-answer-profiles/{search_answer_profile_id}",
        json={"description": " ", "config": {"knowledge_base_ids": ["kb-default"]}},
    )
    assert empty_description.status_code == 422

    rename = client.patch(
        f"/api/search-answer-profiles/{search_answer_profile_id}",
        json={"name": "全社ビュー"},
    )
    assert rename.status_code == 409
    assert rename.json()["error_messages"] == [
        "DEFAULT 検索・回答プロファイルの名前は変更できません。"
    ]

    replace_scope = client.patch(
        f"/api/search-answer-profiles/{search_answer_profile_id}",
        json={"config": {"knowledge_base_ids": ["kb-1"]}},
    )
    assert replace_scope.status_code == 409
    assert replace_scope.json()["error_messages"] == [
        "DEFAULT 検索・回答プロファイルの参照 KB は変更できません。"
    ]

    archive = client.post(f"/api/search-answer-profiles/{search_answer_profile_id}/archive")
    assert archive.status_code == 409
    assert archive.json()["error_messages"] == [
        "DEFAULT 検索・回答プロファイルはアーカイブできません。"
    ]


def test_get_missing_search_answer_profile_returns_404(
    fake_oracle: FakeSearchAnswerProfileOracle,
) -> None:
    """存在しない ID は 404 を返す。"""
    resp = client.get("/api/search-answer-profiles/does-not-exist")
    assert resp.status_code == 404


def test_detail_config_roundtrips_through_schema() -> None:
    """detail の config は SearchAnswerProfileConfig として解釈できる。"""
    config = SearchAnswerProfileConfig(knowledge_base_ids=["kb-1"])
    restored = parse_search_answer_profile_config(config.model_dump(mode="json"))
    assert restored.normalized_knowledge_base_ids() == ["kb-1"]
