"""質問から検索・回答プロファイルを推薦する MCP のツール（#1850）。

`rag_recommend_search_answer_profile`。外部の Agent が、プロファイルの名前を書かない質問で
関係の無いプロファイルを順に検索しないよう（#1846）、推薦は使えるプロファイルの参照先を 1 回だけ
検索し（LLM は呼ばない）、当たった資料で決める。
Oracle・埋め込みは fake（監査 context の範囲で絞る）に差し替え、実サービスは呼ばない。SQL は
`tests/test_profile_recommendation_oracle.py` が実 Oracle で確かめる。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from pr_system_settings.auth.service_token import issue_service_token
from pytest import MonkeyPatch

from app.config import get_settings
from app.main import app
from app.mcp import profile_scope
from app.mcp import tools as mcp_tools
from app.rag.profile_recommendation import rank_profiles
from app.rag.request_context import current_audit_request_context
from app.rag.search_answer_profile_config import SearchAnswerProfileConfig
from app.schemas.knowledge_base import KnowledgeBaseRef
from app.schemas.search import RetrievedChunk, SearchMode
from app.schemas.search_answer_profile import SearchAnswerProfileTarget
from tests.security_support import ProductionAuth, enable_production_auth
from tests.support import AsgiTestClient
from tests.test_mcp_api import SECRET  # 署名鍵は既存のテストのもの（新しい鍵を足さない）
from tests.test_security_scope import ScopedViewOracle

client = AsgiTestClient(app)
QUESTION = "サーバー室の定期点検の前に行う準備をすべて挙げてください。"

# 検索・回答プロファイル → (名前, 説明, 参照先の KB)。
# DEFAULT が先頭（一覧の並び）でも、推薦は資料で決まる。
PROFILES: dict[str, tuple[str, str, list[str]]] = {
    "bv-default": (
        "DEFAULT",
        "DEFAULT ナレッジベースを検索・回答に使う既定のプロファイル",
        ["kb-default"],
    ),
    "bv-rules": ("社内規程", "経費精算・出張旅費などの社内規程", ["kb-rules"]),
    "bv-it": ("IT 運用", "システムの運用の手順書", ["kb-it"]),
}
KB_NAMES = {
    "kb-default": "DEFAULT",
    "kb-rules": "規程",
    "kb-it": "運用手順",
    "kb-it-copy": "運用手順",
}
# 文書 → (文書名, 属する KB)。
DOCUMENTS: dict[str, tuple[str, set[str]]] = {
    "doc-server": ("サーバー室の定期点検要領.pdf", {"kb-it"}),
    "doc-expense": ("経費精算規程.pdf", {"kb-rules"}),
    "doc-esg": ("ESG ファイナンスのお知らせ.pdf", {"kb-default"}),
}


def _chunk(document_id: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(
        document_id=document_id,
        chunk_id=f"{document_id}:{abs(hash(text)) % 1000}",
        text=text,
        score=0.5,
        file_name=DOCUMENTS[document_id][0],
    )


class RecommendFakeOracle:
    """Oracle の SQL と同じく、監査 context の範囲（プロファイル・KB）で絞る fake。"""

    def __init__(self, hits: list[RetrievedChunk]) -> None:
        self.hits = hits
        self.searches: list[dict[str, Any]] = []

    async def search_answer_profile_targets(
        self, *, search_answer_profile_ids: Sequence[str] | None = None
    ) -> list[SearchAnswerProfileTarget]:
        context = current_audit_request_context()
        allowed_profiles = context.allowed_search_answer_profile_ids
        allowed_kbs = context.allowed_knowledge_base_ids
        targets = []
        for profile_id, (name, description, kb_ids) in PROFILES.items():
            if allowed_profiles is not None and profile_id not in allowed_profiles:
                continue
            if (
                search_answer_profile_ids is not None
                and profile_id not in search_answer_profile_ids
            ):
                continue
            targets.append(
                SearchAnswerProfileTarget(
                    id=profile_id,
                    name=name,
                    description=description,
                    knowledge_bases=[
                        KnowledgeBaseRef(id=kb, name=KB_NAMES[kb])
                        for kb in kb_ids
                        if allowed_kbs is None or kb in allowed_kbs
                    ],
                )
            )
        return targets

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        kb_filter = set((filters or {}).get("knowledge_base_id", "").split(","))
        self.searches.append({"query": query, "mode": mode, "knowledge_base_ids": kb_filter})
        return [hit for hit in self.hits if DOCUMENTS[hit.document_id][1] & kb_filter][:top_k]

    async def knowledge_base_ids_by_retrieved_document(
        self, document_ids: Sequence[str], knowledge_base_ids: Sequence[str]
    ) -> dict[str, set[str]]:
        return {
            document_id: DOCUMENTS[document_id][1] & set(knowledge_base_ids)
            for document_id in document_ids
        }


class FakeEmbedder:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.fail = fail

    async def embed(
        self, texts: list[str], *, input_type: str = "SEARCH_DOCUMENT"
    ) -> list[list[float]]:
        self.calls.append((texts, input_type))
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return [[0.1] * 4 for _ in texts]


SERVER_HITS = [
    _chunk("doc-server", "第 2 章 点検の前の準備 点検の 5 営業日前までに入室の予約をします。"),
    _chunk("doc-server", "点検の前日までに監視の一時停止を依頼します。"),
    _chunk("doc-esg", "ESG ファイナンスの取組みについて"),
    _chunk("doc-expense", "立替経費は翌月 10 日までに申請する。"),
]


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> ProductionAuth:
    production = enable_production_auth(monkeypatch)
    monkeypatch.setattr(get_settings(), "app_service_token_secret", SECRET)
    views = {
        profile_id: SearchAnswerProfileConfig(knowledge_base_ids=kb_ids)
        for profile_id, (_, _, kb_ids) in PROFILES.items()
    }
    monkeypatch.setattr(profile_scope, "OracleClient", lambda *_a, **_k: ScopedViewOracle(views))
    return production


def _install(
    monkeypatch: MonkeyPatch, hits: list[RetrievedChunk], *, embed_fails: bool = False
) -> tuple[RecommendFakeOracle, FakeEmbedder]:
    oracle = RecommendFakeOracle(hits)
    embedder = FakeEmbedder(fail=embed_fails)
    monkeypatch.setattr(mcp_tools, "OracleClient", lambda *_a, **_k: oracle)
    monkeypatch.setattr(mcp_tools, "OciGenAiClient", lambda *_a, **_k: embedder)
    return oracle, embedder


def _headers(user_uuid: str, profile_ids: list[str] | None = None) -> dict[str, str]:
    token = issue_service_token(
        SECRET,
        subject=user_uuid,
        audience="rag",
        issuer="agent",
        claims={"run_id": "run-1", "agent_id": "agent-1"},
        profile_ids=profile_ids,
    )
    return {"Authorization": f"Bearer {token}"}


def _recommend(arguments: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    response = client.post(
        "/api/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "rag_recommend_search_answer_profile", "arguments": arguments},
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()["result"]
    assert result["isError"] is False, result
    content: dict[str, Any] = result["structuredContent"]
    return content


def _searcher(auth: ProductionAuth) -> Any:
    return auth.user_with_permissions(
        "searcher",
        ["menu.search"],
        search_answer_profile_ids=list(PROFILES),
        knowledge_base_ids=["kb-default", "kb-rules", "kb-it"],
    )


def test_recommends_the_profile_whose_documents_answer_a_question_without_its_name(
    auth: ProductionAuth, monkeypatch: MonkeyPatch
) -> None:
    """プロファイルの名前を書かない質問でも、当たった資料のプロファイルを推薦する（#1846 の例）。"""
    oracle, embedder = _install(monkeypatch, SERVER_HITS)
    result = _recommend({"question": QUESTION}, _headers(_searcher(auth).user_uuid))

    assert result["recommended_profile_id"] == "bv-it"
    top = result["candidates"][0]
    assert (top["id"], top["name"], top["score"]) == ("bv-it", "IT 運用", 1.0)
    assert top["documents"] == ["サーバー室の定期点検要領.pdf"]
    assert "サーバー室の定期点検要領.pdf" in top["reason"]
    assert [item["id"] for item in result["candidates"]] == ["bv-it", "bv-default", "bv-rules"]
    # 検索は 1 回（使える KB をまとめて）。埋め込みは質問の 1 回だけで、LLM は呼ばない。
    assert len(oracle.searches) == 1
    assert oracle.searches[0]["knowledge_base_ids"] == {"kb-default", "kb-rules", "kb-it"}
    assert embedder.calls == [([QUESTION], "SEARCH_QUERY")]


def test_candidates_split_over_different_documents_are_returned_for_confirmation(
    auth: ProductionAuth, monkeypatch: MonkeyPatch
) -> None:
    """近い点の候補が違う資料に当たったら推薦せず、候補の一覧を返す（利用者に確かめる）。"""
    _install(
        monkeypatch,
        [
            _chunk("doc-expense", "申請の期限は翌月 10 日。"),
            _chunk("doc-server", "点検の申請は 5 営業日前まで。"),
        ],
    )
    result = _recommend({"question": "申請の期限は？"}, _headers(_searcher(auth).user_uuid))

    assert result["recommended_profile_id"] is None
    assert [item["id"] for item in result["candidates"]] == ["bv-rules", "bv-it"]
    assert [item["documents"] for item in result["candidates"]] == [
        ["経費精算規程.pdf"],
        ["サーバー室の定期点検要領.pdf"],
    ]


def test_scope_of_the_user_and_the_business_agent_limits_the_candidates(
    auth: ProductionAuth, monkeypatch: MonkeyPatch
) -> None:
    """利用者が使えるプロファイル・業務 Agent のデータの範囲（claim）・profile_ids の中から選ぶ。"""
    oracle, _ = _install(monkeypatch, SERVER_HITS)
    user = auth.user_with_permissions(
        "limited",
        ["menu.search"],
        search_answer_profile_ids=["bv-rules", "bv-it"],
        knowledge_base_ids=["kb-rules", "kb-it"],
    )

    allowed = _recommend({"question": QUESTION}, _headers(user.user_uuid))
    assert allowed["recommended_profile_id"] == "bv-it"
    assert {item["id"] for item in allowed["candidates"]} == {"bv-it", "bv-rules"}
    assert oracle.searches[-1]["knowledge_base_ids"] == {"kb-rules", "kb-it"}

    # 業務 Agent のデータの範囲（claim）の外のプロファイルは候補にしない。
    scoped = _recommend({"question": QUESTION}, _headers(user.user_uuid, ["bv-rules"]))
    assert scoped["recommended_profile_id"] == "bv-rules"
    assert [item["id"] for item in scoped["candidates"]] == ["bv-rules"]
    assert oracle.searches[-1]["knowledge_base_ids"] == {"kb-rules"}

    # profile_ids を渡すと、その中からだけ推薦する（使えない ID は無視される）。
    narrowed = _recommend(
        {"question": QUESTION, "profile_ids": ["bv-rules", "bv-default"]},
        _headers(user.user_uuid),
    )
    assert [item["id"] for item in narrowed["candidates"]] == ["bv-rules"]


def test_falls_back_to_keyword_search_when_embedding_is_unavailable(
    auth: ProductionAuth, monkeypatch: MonkeyPatch
) -> None:
    oracle, _ = _install(monkeypatch, SERVER_HITS, embed_fails=True)
    result = _recommend({"question": QUESTION}, _headers(_searcher(auth).user_uuid))
    assert result["recommended_profile_id"] == "bv-it"
    assert oracle.searches[0]["mode"] == SearchMode.KEYWORD


def test_no_candidates_without_hits_or_matching_names(
    auth: ProductionAuth, monkeypatch: MonkeyPatch
) -> None:
    _install(monkeypatch, [])
    result = _recommend({"question": "火星の天気"}, _headers(_searcher(auth).user_uuid))
    assert result == {
        "schema_version": mcp_tools.MCP_OUTPUT_SCHEMA_VERSION,
        "recommended_profile_id": None,
        "candidates": [],
    }


# ---------------------------------------------------------------------------
# rank_profiles（点の付け方）
# ---------------------------------------------------------------------------


def _target(
    profile_id: str, name: str, kb_ids: list[str], description: str = ""
) -> SearchAnswerProfileTarget:
    return SearchAnswerProfileTarget(
        id=profile_id,
        name=name,
        description=description,
        knowledge_bases=[KnowledgeBaseRef(id=kb, name=kb) for kb in kb_ids],
    )


def test_same_documents_in_several_profiles_still_recommend_one() -> None:
    """同じ資料を複数の KB に入れたプロファイル（評価の写しなど）は同じ順位で同点になる。

    どれを選んでも根拠は同じなので、名前の順の 1 位を推薦する（割れたとみなさない）。
    """
    targets = [_target("bv-b", "評価 B", ["kb-b"]), _target("bv-a", "評価 A", ["kb-a"])]
    text = "点検の 5 営業日前までに入室の予約をします。"
    hits = [
        RetrievedChunk(
            document_id="d-a", chunk_id="c1", text=text, score=0.5, file_name="点検要領.pdf"
        ),
        RetrievedChunk(
            document_id="d-b", chunk_id="c2", text=text, score=0.5, file_name="点検要領.pdf"
        ),
    ]
    result = rank_profiles(QUESTION, targets, hits, {"d-a": {"kb-a"}, "d-b": {"kb-b"}})

    assert result.recommended_profile_id == "bv-a"
    assert [(item.id, item.score) for item in result.candidates] == [("bv-a", 1.0), ("bv-b", 1.0)]


def test_names_and_descriptions_decide_when_nothing_is_retrieved() -> None:
    targets = [
        _target("bv-rules", "社内規程", ["kb-1"], "経費精算・出張旅費の規程"),
        _target("bv-sales", "営業資料", ["kb-2"], "製品の提案書"),
    ]
    result = rank_profiles("出張旅費の規程を教えて", targets, [], {})
    assert result.recommended_profile_id == "bv-rules"
    assert [item.id for item in result.candidates] == ["bv-rules"]


def test_documents_shared_by_a_parent_profile_do_not_split_the_recommendation() -> None:
    """1 位の資料を含む広いプロファイル（複数の KB）が近くても、当たった資料が同じなら推薦する。"""
    targets = [_target("bv-all", "全社", ["kb-it", "kb-hr"]), _target("bv-it", "IT", ["kb-it"])]
    hits = [
        RetrievedChunk(
            document_id="d", chunk_id="c", text="点検", score=0.5, file_name="点検要領.pdf"
        )
    ]
    result = rank_profiles("点検の準備", targets, hits, {"d": {"kb-it"}})
    assert result.recommended_profile_id is not None
    assert {item.id for item in result.candidates} == {"bv-all", "bv-it"}


def test_limit_caps_the_candidates_and_scores_are_relative_to_the_top() -> None:
    targets = [_target(f"bv-{index}", f"P{index}", [f"kb-{index}"]) for index in range(10)]
    hits = [
        RetrievedChunk(
            document_id=f"d{index}",
            chunk_id="c",
            text=f"本文 {index}",
            score=0.5,
            file_name=f"文書{index}",
        )
        for index in range(10)
    ]
    membership = {f"d{index}": {f"kb-{index}"} for index in range(10)}
    result = rank_profiles("質問", targets, hits, membership, limit=3)
    assert [item.id for item in result.candidates] == ["bv-0", "bv-1", "bv-2"]
    assert result.candidates[0].score == 1.0
    assert 0 < result.candidates[2].score < result.candidates[1].score < 1.0


def _hit(document_id: str, text: str, file_name: str) -> RetrievedChunk:
    return RetrievedChunk(
        document_id=document_id,
        chunk_id=f"{document_id}:{text}",
        text=text,
        score=0.5,
        file_name=file_name,
    )


def test_many_weak_hits_of_a_large_profile_do_not_outscore_the_top_hits() -> None:
    """下位の弱い当たりを多く持つ大きいプロファイルが、上位の当たりの数で勝たない（#1865）。

    開発の環境で、評価の写しの多い資料（多段の質問の 16 文書）が、出張旅費規程の 1 位の当たりを
    下位の当たりの数で上回り、出張の質問で多段の質問のプロファイルを推薦していた。
    """
    targets = [
        _target("bv-trip", "出張", ["kb-trip"]),
        _target("bv-hr", "人事", ["kb-hr"]),
        _target("bv-big", "大きい資料", ["kb-big"]),
    ]
    # 1〜3 位は出張旅費規程、4〜5 位は人事の資料、6 位から下に大きい資料の弱い当たりが 30 件。
    hits = [_hit("d-trip", f"出張の申請 {index}", "出張旅費規程.md") for index in range(3)]
    hits += [_hit("d-hr", f"休暇の申請 {index}", "休暇規程.pdf") for index in range(2)]
    hits += [_hit("d-big", f"申請の期限 {index}", "承認規程.pdf") for index in range(30)]
    membership = {"d-trip": {"kb-trip"}, "d-hr": {"kb-hr"}, "d-big": {"kb-big"}}

    result = rank_profiles("出張の申請は何日前まで？", targets, hits, membership)

    # 点に数えるのは上位 10 件の本文だけ（以前は 6 位から下の 30 件の和で大きい資料が 1 位だった）。
    assert result.recommended_profile_id == "bv-trip"
    assert [item.id for item in result.candidates] == ["bv-trip", "bv-big", "bv-hr"]
    assert result.candidates[1].score < 0.7


def test_copies_that_share_a_top_document_do_not_split() -> None:
    """近い候補が 1 位と同じ文書にも当たっていれば割れにしない（同じ資料の写し。#1865）。

    取り込みの版が違う写しは chunk の本文が少し違い、上位 3 文書の並びがずれる。上位の文書が 1 つも
    重ならないときだけ割れにする（重ならない例は
    `test_candidates_split_over_different_documents_are_returned_for_confirmation`）。
    """
    targets = [_target("bv-a", "評価 A", ["kb-a"]), _target("bv-b", "評価 B", ["kb-b"])]
    hits = [
        _hit("a1", "台帳の行", "台帳.xlsx"),
        _hit("b1", "台帳の行（版違い）", "台帳.xlsx"),
        _hit("a2", "規程の章", "規程.pdf"),
        _hit("b2", "要領の章", "要領.pdf"),
    ]
    membership = {"a1": {"kb-a"}, "a2": {"kb-a"}, "b1": {"kb-b"}, "b2": {"kb-b"}}

    result = rank_profiles("台帳の担当は？", targets, hits, membership)

    assert result.recommended_profile_id == "bv-a"
    assert [item.documents for item in result.candidates] == [
        ["台帳.xlsx", "規程.pdf"],
        ["台帳.xlsx", "要領.pdf"],
    ]
