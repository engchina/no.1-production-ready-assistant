"""根拠の取得（rag_retrieve_evidence）が返す検索・回答プロファイルのルール・用語（#2701）のテスト。

RAG が回答を作らない取得では、回答を作る呼び出し元（業務 Agent）へ、利用者の元の質問に当たる
承認済みのルールの本文と用語の説明を、資料の根拠（evidence）と分けて返す。合成のプロファイルで、
原文に無い判断の方針が届くこと、無効・未承認・別のプロファイルの登録が届かないこと、件数・字数の
上限で省いた数を示すこと、読めなかったことを当たるものが無いことと区別することを確かめる。
Oracle は fake、検索は記録だけのスタブで、推論用の LLM・埋め込みは呼ばない。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pytest import MonkeyPatch

from app.api.routes import search as search_route
from app.config import get_settings
from app.mcp import tools as mcp_tools
from app.rag.search_answer_profile_config import SearchAnswerProfileConfig
from app.schemas.search import SearchRequest, SearchResponse
from tests.security_support import ProductionAuth, enable_production_auth
from tests.test_mcp_api import SECRET, _call, _chunk, _token

PROFILE = "bv-1"
OTHER_PROFILE = "bv-2"
QUESTION = "機器 A の合成手続きアルファはいつまでに行うか"
# 原文（根拠の本文）に無い、検索・回答プロファイルで承認した判断の方針。
APPROVED_POLICY = "合成手続きアルファの期限は、受付の翌営業日から数える。"


def _rule(rule_id: str, status: str, *, trigger: str = "合成手続きアルファ", **extra: Any) -> Any:
    return {
        "id": rule_id,
        "title": f"ルール {rule_id}",
        "triggers": [trigger],
        "content": extra.pop("content", f"{rule_id} の方針。"),
        "source": "運用の取り決め",
        "status": status,
        "updated_at": "2026-10-01T00:00:00+00:00",
        **extra,
    }


KNOWLEDGE: dict[str, dict[str, Any]] = {
    PROFILE: {
        "schema_version": 1,
        "rules": [
            _rule("r-approved", "approved", content=APPROVED_POLICY),
            _rule("r-stale", "stale_review_needed"),
            _rule("r-disabled", "deprecated"),
            _rule("r-draft", "draft"),
            _rule("r-unmatched", "approved", trigger="合成手続きベータ"),
        ],
        "terms": [
            {
                "term": "合成手続きアルファ",
                "aliases": ["手続き α"],
                "description": "合成の手続きの正式名。",
                "status": "approved",
            },
            {"term": "機器 A", "description": "下書きの説明。", "status": "draft"},
        ],
    },
    OTHER_PROFILE: {
        "schema_version": 1,
        "rules": [_rule("r-other-profile", "approved")],
        "terms": [],
    },
}


class _KnowledgeOracle:
    """利用者が使えるプロファイルと、その用語・ルールを返す fake。"""

    knowledge: dict[str, dict[str, Any]] = KNOWLEDGE
    usable = frozenset({PROFILE, OTHER_PROFILE})
    fail = False

    async def get_search_answer_profile(self, profile_id: str) -> Any:
        if _KnowledgeOracle.fail:
            raise RuntimeError("db down")
        if profile_id not in _KnowledgeOracle.usable:
            return None
        return SimpleNamespace(
            id=profile_id, config=SearchAnswerProfileConfig(knowledge_base_ids=["kb-1"])
        )

    async def get_search_answer_profile_knowledge(self, profile_id: str, kind: str) -> Any:
        assert kind == "runtime_knowledge"
        return _KnowledgeOracle.knowledge.get(profile_id)

    async def chunk_set_extraction_recipe_ids(self, _ids: Any) -> dict[str, str]:
        return {}

    async def retrieval_older_versions(self, *_args: Any, **_kwargs: Any) -> list[Any]:
        return []


class _NoGenerationClient:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("根拠の取得で推論用の AI のクライアントを作った")


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> ProductionAuth:
    production = enable_production_auth(monkeypatch)
    monkeypatch.setattr(get_settings(), "app_service_token_secret", SECRET)
    return production


@pytest.fixture
def searches(monkeypatch: MonkeyPatch) -> list[SearchRequest]:
    """検索は記録だけ（根拠 1 件）。ルールの確認は出さない（確認の判定は別のテスト）。"""
    captured: list[SearchRequest] = []

    async def fake_run(request: SearchRequest, **_options: Any) -> SearchResponse:
        captured.append(request)
        return SearchResponse(
            answer="",
            citations=[_chunk("c1", text="機器 A の手続きの本文。", used=False)],
            trace_id="trace-k",
            elapsed_ms=1.0,
        )

    async def no_rule(_arguments: Any, request: SearchRequest) -> Any:
        return request, None

    _KnowledgeOracle.knowledge = KNOWLEDGE
    _KnowledgeOracle.fail = False
    monkeypatch.setattr(search_route, "_run_search_with_timeout", fake_run)
    monkeypatch.setattr(mcp_tools, "rule_clarification_step", no_rule)
    monkeypatch.setattr(mcp_tools, "OracleClient", _KnowledgeOracle)
    monkeypatch.setattr(mcp_tools, "GenAiClient", _NoGenerationClient)
    return captured


def _retrieve(auth: ProductionAuth, **arguments: Any) -> dict[str, Any]:
    user = auth.user_with_permissions("searcher", ["menu.search"])
    result = _call(
        "rag_retrieve_evidence",
        {"search_answer_profile_id": PROFILE, **arguments},
        _token(user.user_uuid),
    )
    assert result.get("isError") is not True, result
    body: dict[str, Any] = result["structuredContent"]
    return body


def test_caller_retrieval_returns_approved_rules_matched_by_the_original_question(
    auth: ProductionAuth, searches: list[SearchRequest]
) -> None:
    # 呼び出し元の短い検索文は照合の語を含まない。照合は利用者の元の質問で行う。
    body = _retrieve(
        auth, query="期限", planning="caller", queries=["受付 期限"], original_question=QUESTION
    )

    knowledge = body["profile_knowledge"]
    assert knowledge["search_answer_profile_id"] == PROFILE
    assert knowledge["matched_on"] == "original_question"
    assert knowledge["load_failed"] is False
    assert knowledge["revision"]
    # 承認済み・見直し待ちだけ（無効・下書き・当たらない・別のプロファイルのルールは渡さない）。
    assert [rule["id"] for rule in knowledge["rules"]] == ["r-approved", "r-stale"]
    approved = knowledge["rules"][0]
    assert approved == {
        "id": "r-approved",
        "title": "ルール r-approved",
        "content": APPROVED_POLICY,
        "source": "運用の取り決め",
        "status": "approved",
        "updated_at": "2026-10-01T00:00:00+00:00",
    }
    assert knowledge["rules"][1]["status"] == "stale_review_needed"
    assert [term["term"] for term in knowledge["terms"]] == ["合成手続きアルファ"]
    assert knowledge["terms"][0]["description"] == "合成の手続きの正式名。"
    assert (knowledge["rules_omitted"], knowledge["terms_omitted"]) == (0, 0)
    # 設定の本文は資料の根拠に混ぜない。
    assert [item["chunk_id"] for item in body["evidence"]] == ["c1"]
    assert APPROVED_POLICY not in str(body["evidence"])
    # 検索は呼び出し元の検索文のまま（ルール・用語で検索文を変えない）。
    assert searches[0].query == "期限"


def test_rag_planning_also_returns_the_profile_knowledge(
    auth: ProductionAuth, searches: list[SearchRequest]
) -> None:
    """回答を作らない取得は、どちらの主体でも回答を作る側へ渡す。query で照合する。"""
    body = _retrieve(auth, query=QUESTION)

    knowledge = body["profile_knowledge"]
    assert knowledge["matched_on"] == "query"
    assert [rule["id"] for rule in knowledge["rules"]] == ["r-approved", "r-stale"]


def test_other_profile_rules_are_not_mixed(
    auth: ProductionAuth, searches: list[SearchRequest]
) -> None:
    body = _retrieve(
        auth,
        query="期限",
        planning="caller",
        original_question=QUESTION,
        search_answer_profile_id=OTHER_PROFILE,
    )

    assert [rule["id"] for rule in body["profile_knowledge"]["rules"]] == ["r-other-profile"]
    assert body["profile_knowledge"]["terms"] == []


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param({"original_question": "関係の無い質問"}, id="no-match"),
        pytest.param({"search_answer_profile_id": None}, id="no-profile"),
    ],
)
def test_no_profile_knowledge_without_a_match_or_profile(
    auth: ProductionAuth, searches: list[SearchRequest], arguments: dict[str, Any]
) -> None:
    body = _retrieve(auth, query="期限", planning="caller", **arguments)

    assert body["profile_knowledge"] is None
    assert body["evidence"]


def test_profile_outside_the_user_scope_returns_no_knowledge(
    auth: ProductionAuth, searches: list[SearchRequest], monkeypatch: MonkeyPatch
) -> None:
    """利用者の範囲外のプロファイル（fake では無いもの）の登録は渡さない。"""
    monkeypatch.setattr(_KnowledgeOracle, "usable", frozenset({OTHER_PROFILE}))

    body = _retrieve(auth, query="期限", planning="caller", original_question=QUESTION)

    assert body["profile_knowledge"] is None


def test_limits_report_the_omitted_rules_and_terms(
    auth: ProductionAuth, searches: list[SearchRequest]
) -> None:
    """件数と字数の上限を超えたルール・用語は返さず、省いた数を示す。"""
    # 1 件の本文は登録の上限（800 字）に近い長さ。件数の上限より先に字数の上限に掛かる。
    long_content = "方針" * 390
    count = mcp_tools.PROFILE_KNOWLEDGE_RULES_MAX + 2
    _KnowledgeOracle.knowledge = {
        PROFILE: {
            "schema_version": 1,
            "rules": [
                _rule(f"r{index}", "approved", content=f"{long_content}{index}")
                for index in range(count)
            ],
            "terms": [
                {"term": f"合成手続きアルファ{index}", "description": "説明", "status": "approved"}
                for index in range(mcp_tools.PROFILE_KNOWLEDGE_TERMS_MAX + 1)
            ],
        }
    }
    question = (
        QUESTION
        + " "
        + " ".join(
            f"合成手続きアルファ{index}"
            for index in range(mcp_tools.PROFILE_KNOWLEDGE_TERMS_MAX + 1)
        )
    )

    knowledge = _retrieve(auth, query="期限", planning="caller", original_question=question)[
        "profile_knowledge"
    ]

    rules = knowledge["rules"]
    returned_chars = sum(
        len(rule["title"]) + len(rule["content"]) + len(rule["source"]) for rule in rules
    ) + sum(
        len(term["term"])
        + sum(map(len, term["aliases"]))
        + len(term["description"])
        + len(term["source"])
        for term in knowledge["terms"]
    )
    assert returned_chars <= mcp_tools.PROFILE_KNOWLEDGE_MAX_CHARS
    assert 0 < len(rules) < mcp_tools.PROFILE_KNOWLEDGE_RULES_MAX
    assert knowledge["rules_omitted"] == count - len(rules)
    assert len(knowledge["terms"]) <= mcp_tools.PROFILE_KNOWLEDGE_TERMS_MAX
    assert knowledge["terms_omitted"] >= 1


def test_load_failure_is_reported_and_the_evidence_is_still_returned(
    auth: ProductionAuth, searches: list[SearchRequest]
) -> None:
    _KnowledgeOracle.fail = True

    body = _retrieve(auth, query="期限", planning="caller", original_question=QUESTION)

    assert body["profile_knowledge"]["load_failed"] is True
    assert body["profile_knowledge"]["rules"] == []
    assert [item["chunk_id"] for item in body["evidence"]] == ["c1"]


def test_retrieved_profile_rules_only_include_adopted_generation_instructions(
    auth: ProductionAuth, searches: list[SearchRequest]
) -> None:
    """確認だけの内容と未解決の排他指示を、呼び出し元の生成へ渡さない。"""
    _KnowledgeOracle.knowledge = {
        PROFILE: {
            "schema_version": 1,
            "rules": [
                {
                    **_rule("confirm", "approved"),
                    "purpose": "confirmation",
                    "clarification": {"question": "対象は？", "options": []},
                },
                {**_rule("a", "approved", content="指示 A"), "exclusive_group": "g"},
                {**_rule("b", "approved", content="指示 B"), "exclusive_group": "g"},
                _rule("generation", "approved", content="資料を確認する。"),
            ],
            "terms": [],
        }
    }
    knowledge = _retrieve(auth, query="期限", planning="caller", original_question=QUESTION)[
        "profile_knowledge"
    ]
    assert [rule["id"] for rule in knowledge["rules"]] == ["generation"]
    assert knowledge["rules_omitted"] == 0
