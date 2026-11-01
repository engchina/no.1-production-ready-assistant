"""業務ガイドの照合の用語の別名の展開・意味の照合・近い 2 つの確認（#2398）。

内容は架空の画面・機能の名前（合成の例）だけを使う。埋め込みはスタブ（OCI は呼ばない）。
"""

from __future__ import annotations

import itertools
import unicodedata
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from pytest import MonkeyPatch

from app.api.routes import search as search_route
from app.api.routes import search_answer_profile_knowledge as knowledge_route
from app.clients.support_guide_store import StoredIntentEmbeddings
from app.config import get_settings
from app.main import app
from app.mcp import tools as mcp_tools
from app.rag import support_guide_query
from app.rag.kb_adapter_config import KnowledgeBaseQueryConfig
from app.rag.search_answer_profile_config import SearchAnswerProfileConfig
from app.rag.search_answer_profile_knowledge import RUNTIME_KNOWLEDGE_KIND
from app.rag.support_guide_query import build_guide_query, examples_sha256
from app.rag.support_guide_runtime import (
    GUIDE_CHOICE_CONDITION_ID,
    GUIDE_CHOICE_RULE_ID,
    GuideChoice,
    GuideMatch,
    GuideQuery,
    expand_guide_query,
    guide_choice_clarification,
    guide_clarification_record,
    match_guide,
    rank_guides,
    resolve_guide_clarification,
    score_guide,
    select_guide,
)
from app.schemas.search import SearchRequest
from app.schemas.search_answer_profile import (
    SearchAnswerProfileDetail,
    SearchAnswerProfileStatus,
)
from app.schemas.support_guide import SupportGuideContent
from tests.support import AsgiTestClient
from tests.test_mcp_api import _call, _token, auth  # noqa: F401 - fixture を使う
from tests.test_search_answer_profile_domain_keywords import FakeKnowledgeOracle

client = AsgiTestClient(app)


def _guide(title: str, terms: Sequence[str], examples: Sequence[str] = ()) -> SupportGuideContent:
    return SupportGuideContent.model_validate(
        {
            "title": title,
            "goal": {
                "expected_result": f"{title}が終わる",
                "match_terms": list(terms),
                "intent_examples": list(examples),
            },
            "steps": [{"id": "s1", "title": f"{title}の手順"}],
        }
    )


NOTICE = (
    "g-notice",
    1,
    _guide("通知画面Aの表示を設定する", ["通知画面A"], ["通知画面Aを設定したい"]),
)
EXPORT = ("g-export", 3, _guide("集計表Bを出力する", ["集計表B", "出力"], ["集計表Bを出力したい"]))
IMPORT = ("g-import", 2, _guide("集計表Bを取り込む", ["集計表B", "取り込"]))
GUIDES = [NOTICE, EXPORT, IMPORT]
TERMS = {
    "schema_version": 1,
    "terms": [{"term": "通知画面A", "aliases": ["NA画面"]}],
    "rules": [],
}


# ---- 用語の別名の展開 --------------------------------------------------------------------------


def test_alias_expands_the_question_before_matching() -> None:
    question = "NA画面が出ないときは"
    assert match_guide(GUIDES, question) is None

    query = expand_guide_query(question, TERMS)
    assert query.alias_terms == ("通知画面A",)
    assert "通知画面A" in query.expanded
    match = match_guide(GUIDES, query)
    assert match is not None and match.guide_id == "g-notice"
    assert match.breakdown is not None
    breakdown = match.breakdown.to_dict()
    assert (breakdown["terms"], breakdown["alias_terms"]) == ([], ["通知画面A"])
    assert (breakdown["alias_points"], breakdown["total"]) == (2, 2)
    # 条件・適用範囲は原文から読む（展開した語で条件の値を決めない）。
    assert match.summary()["match_breakdown"]["semantic_similarity"] is None


def test_formal_name_in_the_question_matches_an_abbreviated_match_term() -> None:
    abbreviated = ("g-abbr", 1, _guide("略称で書いたガイド", ["NA画面"]))
    query = expand_guide_query("通知画面Aの設定", TERMS)
    match = match_guide([abbreviated], query)
    assert match is not None and match.breakdown is not None
    assert match.breakdown.alias_terms == ("NA画面",)


def test_inactive_terms_and_word_boundaries_do_not_expand() -> None:
    inactive = {"terms": [{"term": "通知画面A", "aliases": ["NA"], "status": "draft"}]}
    assert expand_guide_query("NA の設定", inactive).alias_terms == ()
    ascii_alias = {"terms": [{"term": "通知画面A", "aliases": ["NA"]}]}
    # 英数字の別名は語の途中（NAME の中の NA）に当てない。
    assert expand_guide_query("NAME の設定", ascii_alias).alias_terms == ()
    assert expand_guide_query("NA の設定", ascii_alias).alias_terms == ("通知画面A",)
    assert expand_guide_query("NA の設定", None).expanded == ""


# ---- 意味の照合（埋め込みはスタブ）---------------------------------------------------------------


def _semantic_query(text: str, vector: Sequence[float], threshold: float = 0.6) -> GuideQuery:
    return GuideQuery(
        text=text,
        vector=tuple(vector),
        example_vectors={"g-notice": [[1.0, 0.0, 0.0]], "g-export": [[0.0, 1.0, 0.0]]},
        semantic_threshold=threshold,
        semantic="on",
    )


def test_semantic_similarity_adds_points_above_the_threshold() -> None:
    # 語は当たらないが、質問の例と埋め込みが近い。
    query = _semantic_query("表示がおかしい", [0.9, 0.1, 0.0])
    match = match_guide(GUIDES, query)
    assert match is not None and match.guide_id == "g-notice"
    assert match.breakdown is not None
    assert match.breakdown.semantic_points == 3
    assert match.summary()["match_breakdown"]["semantic_similarity"] == pytest.approx(0.994, 1e-3)

    # 閾値未満なら点を足さない（照合されない）。
    assert match_guide(GUIDES, _semantic_query("表示がおかしい", [0.5, 0.5, 0.7])) is None
    # 例の埋め込みが無いガイドは、意味の点を持たない。
    assert score_guide(IMPORT[2], query, "g-import").semantic_similarity is None


def _legacy_score(content: SupportGuideContent, text: str) -> int:
    """#2398 の前の照合の点（照合の語 1 つで 2 点、質問の例の 2-gram の近さ 0.5 以上で 3 点）。"""

    def normalized(value: str) -> str:
        return "".join(unicodedata.normalize("NFKC", value).casefold().split())

    def bigrams(value: str) -> set[str]:
        item = normalized(value)
        return {item[index : index + 2] for index in range(max(0, len(item) - 1))}

    def similarity(left: str, right: str) -> float:
        a, b = bigrams(left), bigrams(right)
        return len(a & b) / len(a | b) if a and b else 0.0

    score = sum(2 for term in content.goal.match_terms if normalized(term) in normalized(text))
    if any(similarity(example, text) >= 0.5 for example in content.goal.intent_examples):
        score += 3
    return score


@pytest.mark.parametrize(
    "question",
    [
        "通知画面Aを設定したい",
        "集計表Bを出力したい",
        "集計表Bを取り込みたい",
        "集計表Bの出力と取り込み",
        "NA画面が出ない",
        "関係のない質問",
    ],
    ids=["notice", "export", "import", "both", "alias-only", "none"],
)
async def test_semantic_off_keeps_the_existing_match(question: str) -> None:
    """意味の照合が OFF で、別名の登録が無ければ、照合の点と選ぶガイドは今までと同じ。"""

    class _NoEmbedder:
        async def embed(self, texts: list[str], **_: Any) -> list[list[float]]:
            raise AssertionError("OFF では埋め込みを呼ばない")

    settings = get_settings().model_copy(
        update={
            "rag_support_guide_enabled": True,
            "rag_support_guide_semantic_match_enabled": False,
        }
    )
    query = await build_guide_query(object(), settings, "bv-1", GUIDES, question, None)  # type: ignore[arg-type]
    assert query.semantic == "off" and query.vector is None and query.expanded == ""
    expected = sorted(
        (
            (-_legacy_score(content, question), guide_id)
            for guide_id, _, content in GUIDES
            if _legacy_score(content, question) >= 2
        ),
    )
    ranked = rank_guides(GUIDES, query, limit=10)
    assert [(-match.score, match.guide_id) for match in ranked] == expected
    selected = select_guide(GUIDES, query)
    legacy = match_guide(GUIDES, question)
    assert (selected.guide_id if isinstance(selected, GuideMatch) else None) == (
        legacy.guide_id if legacy else None
    )


# ---- 近い 2 つの確認 ---------------------------------------------------------------------------


def test_close_top_two_guides_are_clarified_with_their_titles() -> None:
    question = "集計表Bの手順"
    # 既定（close_margin なし）は今までどおり 1 位（同点は guide_id の順）。
    selected = select_guide(GUIDES, question)
    assert isinstance(selected, GuideMatch) and selected.guide_id == "g-export"

    choice = select_guide(GUIDES, question, close_margin=1)
    assert isinstance(choice, GuideChoice)
    assert choice.options() == ["集計表Bを出力する", "集計表Bを取り込む"]
    [clarification] = choice.clarifications()
    assert clarification["condition_id"] == GUIDE_CHOICE_CONDITION_ID
    assert clarification["options"] == choice.options()
    assert "集計表Bを出力する／集計表Bを取り込む" in choice.short_answer()
    summary = choice.summary()
    assert summary["decision"] == "clarify"
    assert [item["guide_id"] for item in summary["choice_candidates"]] == ["g-export", "g-import"]
    assert summary["choice_candidates"][0]["match_breakdown"]["terms"] == ["集計表B"]

    # 選んだガイド（題名か id）があれば、確かめずにそのガイドを使う。
    for value in ("集計表Bを取り込む", "g-import"):
        chosen = select_guide(GUIDES, question, {GUIDE_CHOICE_CONDITION_ID: value}, close_margin=1)
        assert isinstance(chosen, GuideMatch) and chosen.guide_id == "g-import"
    # 点の差が margin より大きければ確かめない。
    clear = select_guide(GUIDES, "集計表Bを出力したい", close_margin=1)
    assert isinstance(clear, GuideMatch) and clear.guide_id == "g-export"


def test_same_titles_are_not_offered_as_choices() -> None:
    twin = ("g-twin", 1, _guide("集計表Bを出力する", ["集計表B"]))
    selected = select_guide([EXPORT, twin], "集計表Bの手順", close_margin=5)
    assert isinstance(selected, GuideMatch)


def test_chat_guide_choice_round_trip() -> None:
    choice = select_guide(GUIDES, "集計表Bの手順", close_margin=1)
    assert isinstance(choice, GuideChoice)
    rule_id, title, clarification = guide_choice_clarification(choice)
    assert (rule_id, title) == (GUIDE_CHOICE_RULE_ID, "業務ガイド")
    assert [option.id for option in clarification.options] == ["g-export", "g-import"]
    resolved = resolve_guide_clarification(GUIDES, rule_id, ["g-import"])
    assert resolved is not None
    conditions, context = resolved
    assert conditions == {GUIDE_CHOICE_CONDITION_ID: "g-import"}
    assert "集計表Bを取り込む" in context
    assert resolve_guide_clarification(GUIDES, rule_id, ["g-missing"]) is None
    other = resolve_guide_clarification(GUIDES, rule_id, [], "どちらでもない")
    assert other is not None and other[0] == {}
    record = guide_clarification_record(GUIDES, rule_id, ["g-import"])
    assert record is not None
    entries: Any = record["entries"]
    assert entries[0]["selected"] == ["集計表Bを取り込む"]
    skipped = guide_clarification_record(GUIDES, rule_id, None)
    assert skipped is not None and skipped["outcome"] == "skipped"


# ---- 検索・チャットの確認・MCP の 3 か所 ----------------------------------------------


class _ProfileOracle(FakeKnowledgeOracle):
    def __init__(self, **query: Any) -> None:
        super().__init__()
        self.query = KnowledgeBaseQueryConfig(**{"support_guide_enabled": True, **query})
        self.payloads[("bv-1", RUNTIME_KNOWLEDGE_KIND)] = dict(TERMS)

    async def get_search_answer_profile(self, search_answer_profile_id: str) -> Any:
        if search_answer_profile_id != "bv-1":
            return None
        return SearchAnswerProfileDetail(
            id="bv-1",
            name="プロファイル A",
            status=SearchAnswerProfileStatus.ACTIVE,
            knowledge_base_count=1,
            config=SearchAnswerProfileConfig(knowledge_base_ids=["kb-1"], query=self.query),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def _published_store(guides: list[Any]) -> type:
    class _Store:
        def __init__(self, _oracle: object) -> None:
            pass

        async def published_contents(self, _profile: str) -> list[Any]:
            return list(guides)

    return _Store


@pytest.mark.usefixtures("system_access")
async def test_search_expands_aliases_and_records_the_breakdown(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(search_route, "OracleClient", lambda: _ProfileOracle())
    monkeypatch.setattr(search_route, "SupportGuideStore", _published_store(GUIDES))
    request = SearchRequest(query="NA画面が出ない", search_answer_profile_id="bv-1")
    _, settings, _, _ = await search_route._resolve_query_context(request, get_settings())
    guide: Any = settings.rag_support_guide
    assert guide["guide_id"] == "g-notice"
    assert guide["alias_expansion"] == ["通知画面A"]
    assert guide["semantic"] == "off"
    assert guide["match_breakdown"]["alias_terms"] == ["通知画面A"]


@pytest.mark.usefixtures("system_access")
async def test_search_clarifies_close_guides_when_enabled(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(
        search_route,
        "OracleClient",
        lambda: _ProfileOracle(support_guide_close_match_clarification_enabled=True),
    )
    monkeypatch.setattr(search_route, "SupportGuideStore", _published_store(GUIDES))
    request = SearchRequest(query="集計表Bの手順", search_answer_profile_id="bv-1")
    _, settings, _, _ = await search_route._resolve_query_context(request, get_settings())
    guide: Any = settings.rag_support_guide
    assert guide["decision"] == "clarify"
    assert guide["clarifications"][0]["condition_id"] == GUIDE_CHOICE_CONDITION_ID
    assert guide["short_answer"]
    assert "guide_id" not in guide
    # 業務ガイドのルールは渡さない（どのガイドか決まっていない）。
    rules: Any = (settings.rag_runtime_knowledge or {}).get("rules", [])
    assert not any(str(rule.get("id", "")).startswith("guide-") for rule in rules)

    # 答え（題名）を conditions に入れて呼び直せば、そのガイドで答える。
    answered = request.model_copy(
        update={"conditions": {GUIDE_CHOICE_CONDITION_ID: "集計表Bを取り込む"}}
    )
    _, settings, _, _ = await search_route._resolve_query_context(answered, get_settings())
    assert settings.rag_support_guide["guide_id"] == "g-import"

    # チャット（送信の前に確認する）と、回答を作らない検索では確かめない。
    _, settings, _, _ = await search_route._resolve_query_context(
        request, get_settings(), interactive=True
    )
    assert settings.rag_support_guide["guide_id"] == "g-export"
    retrieval = request.model_copy(update={"generate_answer": False})
    _, settings, _, _ = await search_route._resolve_query_context(retrieval, get_settings())
    assert settings.rag_support_guide["guide_id"] == "g-export"


def test_chat_suggestion_asks_which_guide(monkeypatch: MonkeyPatch) -> None:
    fake = _ProfileOracle(support_guide_close_match_clarification_enabled=True)
    monkeypatch.setattr(knowledge_route, "OracleClient", lambda: fake)

    async def published(_oracle: object, _profile: str) -> list[Any]:
        return GUIDES

    monkeypatch.setattr(knowledge_route, "_published_guides", published)
    body = client.post(
        "/api/search-answer-profiles/bv-1/clarifications/suggest",
        json={"query": "集計表Bの手順"},
    ).json()["data"]["suggestion"]
    assert body["rule_id"] == GUIDE_CHOICE_RULE_ID
    assert [option["label"] for option in body["clarification"]["options"]] == [
        "集計表Bを出力する",
        "集計表Bを取り込む",
    ]
    # 別名で展開した質問でも、確認の判定は検索と同じガイドを選ぶ（条件の無いガイドは確認しない）。
    none = client.post(
        "/api/search-answer-profiles/bv-1/clarifications/suggest",
        json={"query": "NA画面が出ない"},
    ).json()["data"]
    assert none["suggestion"] is None


@pytest.mark.usefixtures("auth")
def test_mcp_lookup_guides_expands_aliases(auth: Any, monkeypatch: MonkeyPatch) -> None:  # noqa: F811
    async def published(_oracle: object, _profile: str) -> tuple[list[Any], bool]:
        return GUIDES, False

    monkeypatch.setattr(mcp_tools, "OracleClient", _ProfileOracle)
    monkeypatch.setattr(search_route, "load_published_guides", published)
    user = auth.user_with_permissions("searcher", ["menu.search"])
    body = _call(
        "rag_lookup_guides",
        {"query": "NA画面が出ない", "search_answer_profile_id": "bv-1"},
        _token(user.user_uuid),
    )["structuredContent"]
    assert [guide["guide_id"] for guide in body["guides"]] == ["g-notice"]


# ---- 質問の例の埋め込みの作成・保存 ------------------------------------------------


class _Embedder:
    """テキストごとに決まったベクトルを返すスタブ（呼んだテキストを記録する）。"""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.vectors = vectors
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str], **_: Any) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.vectors.get(text, [0.0, 0.0, 1.0]) for text in texts]


class _EmbeddingStore:
    def __init__(self, stored: dict[str, StoredIntentEmbeddings]) -> None:
        self.stored = stored
        self.saved: list[tuple[str, int]] = []

    async def published_intent_embeddings(self, _profile: str) -> dict[str, Any]:
        return dict(self.stored)

    async def save_intent_embeddings(self, guide_id: str, revision: int, **_: Any) -> None:
        self.saved.append((guide_id, revision))


async def test_semantic_on_builds_and_saves_missing_example_embeddings(
    monkeypatch: MonkeyPatch,
) -> None:
    settings = get_settings().model_copy(
        update={
            "rag_support_guide_enabled": True,
            "rag_support_guide_semantic_match_enabled": True,
            "rag_support_guide_semantic_threshold": 0.8,
        }
    )
    fresh = StoredIntentEmbeddings(
        revision=3,
        model_id=settings.genai_embedding_model,
        dimension=settings.genai_embedding_dim,
        examples_sha256=examples_sha256(EXPORT[2]),
        vectors=((0.0, 1.0, 0.0),),
    )
    store = _EmbeddingStore({"g-export": fresh})
    embedder = _Embedder(
        {"通知画面Aを設定したい": [1.0, 0.0, 0.0], "表示がおかしい": [1.0, 0.1, 0.0]}
    )
    monkeypatch.setattr(support_guide_query, "SupportGuideStore", lambda _oracle: store)
    monkeypatch.setattr(support_guide_query, "guide_embedder", lambda _settings: embedder)

    query = await build_guide_query(object(), settings, "bv-1", GUIDES, "表示がおかしい", None)  # type: ignore[arg-type]
    assert query.semantic == "on"
    # 公開の版の埋め込みが新しければ作り直さない。無い版だけを作って保存する。
    assert embedder.calls == [["通知画面Aを設定したい"], ["表示がおかしい"]]
    assert store.saved == [("g-notice", 1)]
    assert query.example_vectors["g-export"] == [[0.0, 1.0, 0.0]]
    match = match_guide(GUIDES, query)
    assert match is not None and match.guide_id == "g-notice"

    # 下書きで試すガイドは保存しない。版・例が変わった埋め込みは作り直す。
    stale = StoredIntentEmbeddings(**{**fresh.__dict__, "revision": 2})
    store = _EmbeddingStore({"g-export": stale})
    monkeypatch.setattr(support_guide_query, "SupportGuideStore", lambda _oracle: store)
    embedder.calls.clear()
    await build_guide_query(
        object(),  # type: ignore[arg-type]
        settings,
        "bv-1",
        GUIDES,
        "表示がおかしい",
        None,
        unsaved=frozenset({"g-notice"}),
    )
    assert sorted(itertools.chain.from_iterable(embedder.calls[:1])) == [
        "通知画面Aを設定したい",
        "集計表Bを出力したい",
    ]
    assert store.saved == [("g-export", 3)]


async def test_semantic_failure_falls_back_to_term_matching(monkeypatch: MonkeyPatch) -> None:
    class _Broken:
        async def embed(self, texts: list[str], **_: Any) -> list[list[float]]:
            raise RuntimeError("embedding unavailable")

    settings = get_settings().model_copy(
        update={"rag_support_guide_enabled": True, "rag_support_guide_semantic_match_enabled": True}
    )
    monkeypatch.setattr(
        support_guide_query, "SupportGuideStore", lambda _oracle: _EmbeddingStore({})
    )
    monkeypatch.setattr(support_guide_query, "guide_embedder", lambda _settings: _Broken())
    query = await build_guide_query(object(), settings, "bv-1", GUIDES, "NA画面が出ない", TERMS)  # type: ignore[arg-type]
    assert query.semantic == "failed" and query.vector is None
    assert query.alias_terms == ("通知画面A",)
    match = match_guide(GUIDES, query)
    assert match is not None and match.guide_id == "g-notice"


@pytest.mark.usefixtures("system_access")
@pytest.mark.parametrize("enabled", [None, False])
async def test_master_off_skips_all_guide_work_and_keeps_regular_knowledge(
    enabled: bool | None, monkeypatch: MonkeyPatch
) -> None:
    fake = _ProfileOracle(
        support_guide_enabled=enabled,
        support_guide_semantic_match_enabled=True,
        support_guide_close_match_clarification_enabled=True,
    )
    monkeypatch.setattr(search_route, "OracleClient", lambda: fake)

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("総合 OFF では業務ガイドを処理しない")

    monkeypatch.setattr(search_route, "load_published_guides", forbidden)
    monkeypatch.setattr(search_route, "build_guide_query", forbidden)
    monkeypatch.setattr(search_route, "resolve_text_conditions", forbidden)
    request = SearchRequest(query="NA画面の設定", search_answer_profile_id="bv-1")
    _, settings, _, _ = await search_route._resolve_query_context(
        request,
        get_settings().model_copy(update={"rag_support_guide_enabled": True}),
    )
    assert settings.rag_support_guide_enabled is False
    assert settings.rag_support_guide == {}
    assert settings.rag_runtime_knowledge == TERMS
    assert settings.rag_support_guide_semantic_match_enabled is True
    assert settings.rag_support_guide_close_match_clarification_enabled is True


def test_master_off_chat_does_not_load_or_confirm_guides(monkeypatch: MonkeyPatch) -> None:
    fake = _ProfileOracle(
        support_guide_enabled=False,
        support_guide_semantic_match_enabled=True,
        support_guide_close_match_clarification_enabled=True,
    )
    monkeypatch.setattr(knowledge_route, "OracleClient", lambda: fake)

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("総合 OFF では業務ガイドを読み込まない")

    monkeypatch.setattr(knowledge_route, "_published_guides", forbidden)
    result = client.post(
        "/api/search-answer-profiles/bv-1/clarifications/suggest",
        json={"query": "集計表Bの手順"},
    )
    assert result.status_code == 200
    assert result.json()["data"]["suggestion"] is None


@pytest.mark.usefixtures("auth")
def test_master_off_mcp_does_not_lookup_guides(auth: Any, monkeypatch: MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(
        mcp_tools,
        "OracleClient",
        lambda: _ProfileOracle(
            support_guide_enabled=False,
            support_guide_semantic_match_enabled=True,
            support_guide_close_match_clarification_enabled=True,
        ),
    )

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("総合 OFF では MCP も業務ガイドを読み込まない")

    monkeypatch.setattr(search_route, "load_published_guides", forbidden)
    user = auth.user_with_permissions("guide-master-off", ["menu.search"])
    result = _call(
        "rag_lookup_guides",
        {"search_answer_profile_id": "bv-1", "query": "NA画面の設定"},
        _token(user.user_uuid),
    )
    assert result["structuredContent"]["guides"] == []


@pytest.mark.parametrize("skipped", [False, True])
async def test_master_off_ignores_a_previous_guide_confirmation(
    skipped: bool, monkeypatch: MonkeyPatch
) -> None:
    from app.api.routes import chat as chat_route
    from app.schemas.chat import ChatMessageRequest
    from tests.test_chat_api import FakeChatOracle

    oracle = FakeChatOracle()
    monkeypatch.setattr(
        oracle,
        "get_search_answer_profile",
        _ProfileOracle(support_guide_enabled=False).get_search_answer_profile,
    )
    conversation = await oracle.create_conversation(search_answer_profile_id="bv-1")

    async def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("OFF へ切替後は過去のガイドの確認も適用しない")

    async def resolve(request: SearchRequest, settings: Any, **kwargs: Any) -> Any:
        return request, settings, None, "bv-1"

    monkeypatch.setattr(chat_route, "_published_guides", forbidden)
    monkeypatch.setattr(chat_route, "_resolve_query_context", resolve)
    request = ChatMessageRequest.model_validate(
        {
            "content": "合成の質問",
            **(
                {"clarification_skipped_rule_id": GUIDE_CHOICE_RULE_ID}
                if skipped
                else {
                    "clarification": {"rule_id": GUIDE_CHOICE_RULE_ID, "option_ids": ["g-import"]}
                }
            ),
        }
    )
    prepared = await chat_route._prepare_chat_turn(
        oracle,  # type: ignore[arg-type]
        conversation.id,
        "bv-1",
        request,
        get_settings(),
    )
    assert prepared.scope is None
    assert prepared.request.conditions == {}
    assert prepared.user_message.clarification is None
