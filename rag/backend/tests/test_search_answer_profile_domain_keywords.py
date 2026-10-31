"""検索・回答プロファイル単位のドメインキーワード API と全文検索クエリへの反映。"""

from datetime import UTC, datetime

import pytest

from app.api.routes import search_answer_profile_knowledge as knowledge_route
from app.clients.oracle import _oracle_text_query
from app.config import Settings
from app.main import app
from app.rag.search_answer_profile_config import SearchAnswerProfileConfig
from app.schemas.search_answer_profile import SearchAnswerProfileDetail, SearchAnswerProfileStatus
from tests.support import AsgiTestClient

client = AsgiTestClient(app)


class FakeKnowledgeOracle:
    def __init__(self, *, support_guide_enabled: bool = False) -> None:
        self.support_guide_enabled = support_guide_enabled
        self.payloads: dict[tuple[str, str], dict[str, object]] = {}
        self.chunk_texts = [
            ("c1", "d1", "伝票区分を変更するには伝票区分マスタを開きます。"),
            ("c2", "d1", "伝票区分の初期値は通常伝票です。受注番号は必須です。"),
            ("c3", "d2", "受注番号を入力して伝票区分を選択します。"),
        ]
        self.requested_kbs: list[str] = []

    async def get_search_answer_profile(
        self, search_answer_profile_id: str
    ) -> SearchAnswerProfileDetail | None:
        if search_answer_profile_id != "bv-1":
            return None
        return SearchAnswerProfileDetail(
            id="bv-1",
            name="受注サポート",
            status=SearchAnswerProfileStatus.ACTIVE,
            knowledge_base_count=1,
            config=SearchAnswerProfileConfig.model_validate(
                {
                    "knowledge_base_ids": ["kb-1"],
                    "query": {"support_guide_enabled": self.support_guide_enabled},
                }
            ),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def get_search_answer_profile_knowledge(
        self, search_answer_profile_id: str, kind: str
    ) -> dict[str, object] | None:
        return self.payloads.get((search_answer_profile_id, kind))

    async def save_search_answer_profile_knowledge(
        self, search_answer_profile_id: str, kind: str, payload: dict[str, object]
    ) -> None:
        self.payloads[(search_answer_profile_id, kind)] = payload

    async def list_search_answer_profile_chunk_texts(
        self, knowledge_base_ids: list[str], *, limit: int
    ) -> list[tuple[str, str, str]]:
        self.requested_kbs = list(knowledge_base_ids)
        return self.chunk_texts[:limit]


@pytest.fixture
def fake_oracle(monkeypatch: pytest.MonkeyPatch) -> FakeKnowledgeOracle:
    fake = FakeKnowledgeOracle()
    monkeypatch.setattr(knowledge_route, "OracleClient", lambda: fake)
    return fake


def test_domain_keywords_are_normalized_and_saved_per_search_answer_profile(
    fake_oracle: FakeKnowledgeOracle,
) -> None:
    resp = client.put(
        "/api/search-answer-profiles/bv-1/domain-keywords",
        json={"keywords": [" 伝票区分 ", "伝票区分", "", "ORA-01555"]},
    )

    assert resp.status_code == 200
    assert resp.json()["data"]["keywords"] == ["伝票区分", "ORA-01555"]
    got = client.get("/api/search-answer-profiles/bv-1/domain-keywords")
    assert got.json()["data"]["keywords"] == ["伝票区分", "ORA-01555"]
    assert client.get("/api/search-answer-profiles/missing/domain-keywords").status_code == 404


def test_domain_keyword_suggestions_use_search_answer_profile_knowledge_bases(
    fake_oracle: FakeKnowledgeOracle,
) -> None:
    resp = client.post("/api/search-answer-profiles/bv-1/domain-keywords/suggest")

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert fake_oracle.requested_kbs == ["kb-1"]
    assert data["processed_chunk_count"] == 3
    keywords = [item["keyword"] for item in data["candidates"]]
    assert {"伝票", "受注"} <= set(keywords)


def test_oracle_text_query_keeps_domain_keyword_as_single_term() -> None:
    settings = Settings(rag_domain_keywords=["伝票区分"])

    query = _oracle_text_query("伝票区分を変更する方法", settings=settings)

    assert query is not None
    # ドメインキーワードは 1 語として重く(*2)、先頭に置く。語は ACCUM で結ぶ(#588)。
    assert query.startswith("{伝票区分}*2 ACCUM ")
    assert " OR " not in query
    # キーワードが無ければ重みを付けない(設定を渡さない呼び出しと同じ)。
    plain = _oracle_text_query("伝票区分を変更する方法", settings=Settings())
    assert plain == _oracle_text_query("伝票区分を変更する方法")
    assert plain is not None and "*2" not in plain
