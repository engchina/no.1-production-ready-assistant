"""検索・回答プロファイル単位の用語・ルール(runtime knowledge)API と検索時の反映。"""

import pytest
from pytest import MonkeyPatch

from app.api.routes import search as search_route
from app.api.routes import search_answer_profile_knowledge as knowledge_route
from app.main import app
from app.rag.search_answer_profile_config import SearchAnswerProfileConfig
from tests import test_search_search_answer_profile as search_tests
from tests.support import AsgiTestClient
from tests.test_search_answer_profile_domain_keywords import FakeKnowledgeOracle

client = AsgiTestClient(app)
BASE = "/api/search-answer-profiles/bv-1/runtime-knowledge"


@pytest.fixture
def fake_oracle(monkeypatch: pytest.MonkeyPatch) -> FakeKnowledgeOracle:
    fake = FakeKnowledgeOracle()
    monkeypatch.setattr(knowledge_route, "OracleClient", lambda: fake)
    return fake


def test_edit_terms_rules_and_preview(fake_oracle: FakeKnowledgeOracle) -> None:
    assert client.get(BASE).json()["data"] == {
        "search_answer_profile_id": "bv-1",
        "terms": [],
        "rules": [],
    }

    term = client.post(
        f"{BASE}/edit",
        json={
            "kind": "terms",
            "name": "受注",
            "labels": "オーダー\n注文",
            "content": "顧客からの注文",
        },
    )
    assert term.status_code == 200
    assert term.json()["data"]["terms"][0]["aliases"] == ["オーダー", "注文"]
    rule = client.post(
        f"{BASE}/edit",
        json={
            "kind": "rules",
            "name": "R1",
            "title": "取消の注意",
            "labels": "取消",
            "content": "取消後は元に戻せません。",
        },
    )
    assert rule.json()["data"]["rules"][0]["id"] == "R1"

    preview = client.post(f"{BASE}/preview", json={"question": "オーダーを取消したい"})
    data = preview.json()["data"]
    assert data["matched_terms"] == ["受注"]
    assert data["matched_rules"] == ["取消の注意"]
    assert "受注" in data["expanded_question"]

    duplicate = client.post(f"{BASE}/edit", json={"kind": "terms", "name": "受注"})
    assert duplicate.status_code == 422
    deleted = client.post(
        f"{BASE}/edit", json={"kind": "terms", "selected": "受注", "delete": True}
    )
    assert deleted.json()["data"]["terms"] == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"kind": "terms", "name": "  "}, "用語を入力してください。"),
        (
            {"kind": "rules", "name": "", "title": "t", "content": "c"},
            "照合キーワードを入力してください。",
        ),
        (
            {"kind": "rules", "name": "R1", "title": "", "content": "c"},
            "ルール名を入力してください。",
        ),
        (
            {"kind": "rules", "name": "R1", "title": "t", "labels": "合成語", "content": " "},
            "ルール内容を入力してください。",
        ),
    ],
    ids=["term", "rule-keywords", "rule-title", "rule-content"],
)
def test_edit_rejects_empty_required_fields_like_the_screen(
    fake_oracle: FakeKnowledgeOracle, payload: dict[str, str], message: str
) -> None:
    """用語・ルールの必須は backend が正本（#540）。画面と同じ文言の 422 を返し、保存しない。"""
    response = client.post(f"{BASE}/edit", json=payload)
    assert response.status_code == 422
    assert response.json()["error_messages"] == [message]
    assert client.get(BASE).json()["data"]["terms"] == []
    assert client.get(BASE).json()["data"]["rules"] == []


def test_search_context_carries_search_answer_profile_runtime_knowledge(
    monkeypatch: MonkeyPatch,
) -> None:
    payload = {"schema_version": 1, "terms": [{"term": "受注", "aliases": ["注文"]}], "rules": []}

    class KnowledgeViewOracle(search_tests.FakeViewOracle):
        async def get_search_answer_profile_knowledge(
            self, search_answer_profile_id: str, kind: str
        ) -> dict[str, object] | None:
            return payload if kind == "runtime_knowledge" else None

    config = SearchAnswerProfileConfig(knowledge_base_ids=["kb-1"])
    search_tests._install(monkeypatch, {"bv-1": config})
    monkeypatch.setattr(
        search_route,
        "OracleClient",
        lambda *_args, **_kwargs: KnowledgeViewOracle({"bv-1": config}),
    )

    response = client.post(
        "/api/search", json={"query": "注文の登録", "search_answer_profile_id": "bv-1"}
    )

    assert response.status_code == 200
    settings = search_tests.RecordingPipeline.captured_settings
    assert settings is not None
    assert settings.rag_runtime_knowledge == payload
    # 回答の記録に残す検索・回答プロファイルの版（#1276）。
    revision = settings.rag_search_answer_profile_revision
    assert revision["id"] == "bv-1"
    assert isinstance(revision["updated_at"], str) and revision["config_sha256"]


def test_rule_identity_is_generated_and_retained_on_edit(fake_oracle: FakeKnowledgeOracle) -> None:
    values = {
        "kind": "rules",
        "title": "合成ルール A",
        "labels": "合成語 A",
        "content": "合成の注意点。",
    }
    first = client.post(f"{BASE}/edit", json=values)
    assert first.status_code == 200
    identity = first.json()["data"]["rules"][0]["id"]
    assert identity.startswith("rule_")
    second = client.post(f"{BASE}/edit", json={**values, "title": "合成ルール B"})
    assert second.status_code == 200
    rules = second.json()["data"]["rules"]
    assert len({rule["id"] for rule in rules}) == 2
    edited = client.post(
        f"{BASE}/edit", json={**values, "selected": identity, "title": "合成ルール C"}
    )
    assert edited.status_code == 200
    assert edited.json()["data"]["rules"][0]["id"] == identity
    assert edited.json()["data"]["rules"][0]["title"] == "合成ルール C"


@pytest.mark.parametrize("labels", ["", " \n ", "、,，\n"])
def test_rule_keywords_require_a_nonempty_value(
    fake_oracle: FakeKnowledgeOracle, labels: str
) -> None:
    response = client.post(
        f"{BASE}/edit",
        json={
            "kind": "rules",
            "title": "合成ルール A",
            "labels": labels,
            "content": "合成の注意点。",
        },
    )
    assert response.status_code == 422
    assert response.json()["error_messages"] == ["照合キーワードを入力してください。"]
    assert client.get(BASE).json()["data"]["rules"] == []


def test_confirmation_only_rule_accepts_empty_content_and_preserves_contract_on_legacy_edit(
    fake_oracle: FakeKnowledgeOracle,
) -> None:
    response = client.post(
        f"{BASE}/edit",
        json={
            "kind": "rules",
            "title": "合成の確認",
            "labels": "設定",
            "purpose": "confirmation",
            "content": "",
            "instruction_kind": "verification",
            "exclusive_group": "g",
            "priority": 3,
        },
    )
    assert response.status_code == 200
    row = response.json()["data"]["rules"][0]
    updated = client.post(
        f"{BASE}/edit",
        json={
            "kind": "rules",
            "selected": row["id"],
            "title": "名前を変更",
            "labels": "設定",
            "content": "",
        },
    )
    assert updated.status_code == 200
    saved = updated.json()["data"]["rules"][0]
    assert (
        saved["purpose"],
        saved["instruction_kind"],
        saved["exclusive_group"],
        saved["priority"],
    ) == ("confirmation", "verification", "g", 3)
    generation = client.post(
        f"{BASE}/edit",
        json={
            "kind": "rules",
            "title": "合成の指示",
            "labels": "設定",
            "purpose": "generation",
            "content": "",
        },
    )
    assert generation.status_code == 422


@pytest.mark.parametrize(
    "extra",
    [
        {"purpose": "unknown"},
        {"instruction_kind": "unknown"},
        {"priority": True},
        {"priority": -1},
        {"priority": 101},
    ],
)
def test_rule_application_contract_rejects_invalid_values(
    fake_oracle: FakeKnowledgeOracle, extra: dict[str, object]
) -> None:
    response = client.post(
        f"{BASE}/edit",
        json={
            "kind": "rules",
            "title": "合成の指示",
            "labels": "設定",
            "content": "資料を確認する。",
            **extra,
        },
    )
    assert response.status_code == 422


def test_preview_reports_exclusive_conflict_without_passing_opposed_rules(
    fake_oracle: FakeKnowledgeOracle,
) -> None:
    for title in ("合成ルール A", "合成ルール B"):
        response = client.post(
            f"{BASE}/edit",
            json={
                "kind": "rules",
                "title": title,
                "labels": "設定",
                "content": title,
                "exclusive_group": "group-a",
            },
        )
        assert response.status_code == 200
    response = client.post(f"{BASE}/preview", json={"question": "設定の手順"})
    assert response.status_code == 200
    assert response.json()["data"]["matched_rules"] == []
    assert response.json()["data"]["conflicting_rules"] == ["合成ルール A", "合成ルール B"]


@pytest.mark.parametrize(
    ("purpose", "expected"), [("confirmation", True), ("generation", False), ("both", True)]
)
def test_rule_purpose_controls_confirmation_and_keeps_known_conditions(
    purpose: str, expected: bool
) -> None:
    from app.rag.search_answer_profile_knowledge import unanswered_rule_clarifications

    data = {
        "rules": [
            {
                "id": "rule-a",
                "title": "設定",
                "triggers": ["設定"],
                "content": "資料に沿って説明する。",
                "purpose": purpose,
                "clarification": {
                    "question": "対象は？",
                    "options": [{"id": "a", "label": "対象 A"}, {"id": "b", "label": "対象 B"}],
                },
            }
        ]
    }
    assert bool(unanswered_rule_clarifications(data, "設定の手順")) is expected
    assert bool(unanswered_rule_clarifications(data, "対象 A の設定の手順")) is expected
    data["rules"][0]["status"] = "deprecated"
    assert unanswered_rule_clarifications(data, "設定の手順") == []
