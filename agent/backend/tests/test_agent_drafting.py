"""業務の説明から業務 Agent の下書きを提案する・公開前の評価の門（#2519）。"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from agents.testing import ScriptedModel, assistant_message
from pytest import MonkeyPatch
from security_support import ProductionAuth, client, enable_production_auth, login

from app.features.agent import builtin_runtime
from app.features.agent import router as agent_router
from app.features.agent.builtin_runtime import ModelTarget
from app.features.agent.data_scope import (
    DataScopeCandidate,
    DataScopeCandidatesData,
    DataScopeCandidatesError,
)
from app.features.agent.drafting import MAX_EVALUATION_CASES
from app.features.agent.evaluation import EvaluationCase, JobStatus, draft_definition_hash
from app.features.agent.runtime import AgentProfilePatch, runtime_repository
from app.features.agent.skills import AgentSkillDefinition, skill_registry
from app.security.permissions import MENU_AGENTS, RUNS_OPERATE
from app.security.service import set_security_service
from app.settings import Settings, get_settings

AGENT_ID = "agent-drafting-2519"


def _script(monkeypatch: MonkeyPatch, *steps: Any) -> ScriptedModel:
    model = ScriptedModel(list(steps))
    monkeypatch.setattr(
        builtin_runtime,
        "resolve_model_target",
        lambda model_id="": ModelTarget(
            model_id=model_id or "draft-model",
            endpoint="https://llm.example",
            project_id="",
            api_key="k",
        ),
    )
    monkeypatch.setattr(builtin_runtime, "model_factory", lambda _target: model)
    return model


def _candidates(monkeypatch: MonkeyPatch) -> None:
    """RAG はプロファイル A・B を返し、NL2SQL は候補を取得できない。"""

    def fake(connection: str, *, user_uuid: str | None) -> DataScopeCandidatesData:
        if connection == "nl2sql":
            raise DataScopeCandidatesError(409, "NL2SQL の候補を取得できません。", "unavailable")
        return DataScopeCandidatesData(
            connection=connection,
            label="ナレッジ検索（RAG）",
            profiles=[
                DataScopeCandidate(id="profile-a", name="プロファイル A", description="資料 A"),
                DataScopeCandidate(id="profile-b", name="プロファイル B"),
            ],
        )

    monkeypatch.setattr(agent_router, "list_candidates", fake)


def _known_skill_id() -> str:
    return next(skill.id for skill in skill_registry.list() if skill.enabled)


def test_suggestion_keeps_only_candidates_and_limits(monkeypatch: MonkeyPatch) -> None:
    _candidates(monkeypatch)
    skill_id = _known_skill_id()
    model = _script(
        monkeypatch,
        [
            assistant_message(
                json.dumps(
                    {
                        "name": "  資料を調べる Agent  ",
                        "description": "資料を調べて答える",
                        "instructions": "根拠の箇所を示して答える。",
                        "skill_ids": [skill_id, "unknown-skill", skill_id],
                        "data_scopes": [
                            {"connection": "rag", "profile_ids": ["profile-a", "profile-x"]},
                            {"connection": "nl2sql", "profile_ids": ["profile-y"]},
                            {"connection": "other", "profile_ids": ["profile-a"]},
                        ],
                        "output_fields": [
                            {
                                "key": "item_name",
                                "label": "  項目  ",
                                "type": "text",
                                "required": True,
                                "description": "",
                            },
                            {
                                "key": "amount",
                                "label": "金額",
                                "type": "number",
                                "required": False,
                                "description": "円",
                            },
                            # 形の違う key は形に合わせて付け、重なる key・空の表示名は外す。
                            {
                                "key": "支払期日",
                                "label": "支払期日",
                                "type": "date",
                                "required": False,
                                "description": "",
                            },
                            {
                                "key": "SQL条件",
                                "label": "条件",
                                "type": "text",
                                "required": False,
                                "description": "",
                            },
                            {
                                "key": "amount",
                                "label": "金額 2",
                                "type": "number",
                                "required": False,
                                "description": "",
                            },
                            {
                                "key": "note",
                                "label": " ",
                                "type": "text",
                                "required": False,
                                "description": "",
                            },
                        ],
                        "evaluation_cases": [
                            {"question": f"質問 {index}", "expected": f"要点 {chr(0x3042 + index)}"}
                            for index in range(MAX_EVALUATION_CASES + 2)
                        ]
                        + [{"question": " ", "expected": "空の質問"}],
                    },
                    ensure_ascii=False,
                )
            )
        ],
    )

    response = client.post(
        "/api/agents/draft-suggestion", json={"description": "  資料 A の問い合わせに答えたい  "}
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["name"] == "資料を調べる Agent"
    assert data["skill_ids"] == [skill_id]
    # 候補に無いプロファイル・接続は外し、1 つだけ残った接続はそれを既定にする。
    assert data["data_scopes"] == {
        "rag": {"profile_ids": ["profile-a"], "default_profile_id": "profile-a"}
    }
    assert len(data["evaluation_cases"]) == MAX_EVALUATION_CASES
    assert data["evaluation_cases"][0] == {"question": "質問 0", "expected": "要点 あ"}
    # 下書きは評価の比べ方を提案しない（既定の完全に一致のまま。#2566）。
    assert data["output_fields"] == [
        {
            "key": "item_name",
            "label": "項目",
            "type": "text",
            "required": True,
            "description": "",
            "match": "exact",
        },
        {
            "key": "amount",
            "label": "金額",
            "type": "number",
            "required": False,
            "description": "円",
            "match": "exact",
        },
        {
            "key": "field_3",
            "label": "支払期日",
            "type": "date",
            "required": False,
            "description": "",
            "match": "exact",
        },
        {
            "key": "sql",
            "label": "条件",
            "type": "text",
            "required": False,
            "description": "",
            "match": "exact",
        },
    ]
    assert data["notices"] == [
        "NL2SQL の候補を取得できません。",
        "出力の項目を 4 件入れました。回答を文だけで返す業務なら、"
        "「出力の項目」から外してください。",
        "形の正しくない出力の項目を 2 件外しました。",
        "出力の項目 2 件の key を付けました。key を確かめてください。",
    ]
    # モデルには業務の説明と、候補（スキル・取得できたプロファイル）だけを渡す。
    [call] = model.calls
    text = json.dumps(call.input, ensure_ascii=False)
    assert "資料 A の問い合わせに答えたい" in text
    assert "profile-a: プロファイル A（資料 A）" in text
    assert skill_id in text
    assert "接続 nl2sql" not in text
    # 何も保存しない。
    assert all(agent.name != "資料を調べる Agent" for agent in runtime_repository.list_agents())


def _proposal(*expected: str) -> Any:
    return [
        assistant_message(
            json.dumps(
                {
                    "name": "Agent B",
                    "description": "",
                    "instructions": "",
                    "skill_ids": [],
                    "data_scopes": [],
                    "output_fields": [],
                    "evaluation_cases": [
                        {"question": f"質問 {index}", "expected": text}
                        for index, text in enumerate(expected)
                    ],
                },
                ensure_ascii=False,
            )
        )
    ]


def test_invented_numbers_are_sent_back_once(monkeypatch: MonkeyPatch) -> None:
    """期待する要点に数字があれば、その要点を示して 1 回だけ差し戻す。"""
    _candidates(monkeypatch)
    model = _script(
        monkeypatch,
        _proposal("上限は第3章により 1 万円と示す", "根拠の箇所を示す"),
        _proposal("上限の金額と根拠の箇所を示す", "根拠の箇所を示す"),
    )

    response = client.post("/api/agents/draft-suggestion", json={"description": "業務の説明"})

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert [case["expected"] for case in data["evaluation_cases"]] == [
        "上限の金額と根拠の箇所を示す",
        "根拠の箇所を示す",
    ]
    assert len(model.calls) == 2
    # 項目を求めない業務では、出力の項目は空のまま。
    assert data["output_fields"] == []
    resent = json.dumps(model.calls[1].input, ensure_ascii=False)
    assert "直してほしい点" in resent and "上限は第3章により 1 万円と示す" in resent


def test_cases_still_with_numbers_are_dropped(monkeypatch: MonkeyPatch) -> None:
    _candidates(monkeypatch)
    _script(
        monkeypatch,
        _proposal("１０日以内と示す", "根拠の箇所を示す"),
        _proposal("１０日以内と示す", "根拠の箇所を示す"),
    )

    response = client.post("/api/agents/draft-suggestion", json={"description": "業務の説明"})

    data = response.json()["data"]
    assert [case["expected"] for case in data["evaluation_cases"]] == ["根拠の箇所を示す"]
    assert "評価の問いを 1 件外しました" in data["notices"][-1]


def test_suggestion_failure_returns_reason(monkeypatch: MonkeyPatch) -> None:
    _candidates(monkeypatch)
    _script(monkeypatch, [assistant_message("JSON ではない応答")])

    response = client.post("/api/agents/draft-suggestion", json={"description": "業務の説明"})

    assert response.status_code == 502, response.text
    assert "下書きを提案できませんでした" in response.json()["error_messages"][0]


def test_suggestion_needs_description() -> None:
    response = client.post("/api/agents/draft-suggestion", json={"description": "   "})
    assert response.status_code == 422


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def test_suggestion_needs_agent_admin(auth: ProductionAuth) -> None:
    auth.user_with_permissions("operator-2519", [MENU_AGENTS, RUNS_OPERATE], agent_ids=["default"])

    response = client.post(
        "/api/agents/draft-suggestion",
        json={"description": "業務の説明"},
        headers=login("operator-2519"),
    )

    assert response.status_code == 403


# ---- 公開前の評価の門 -------------------------------------------------------------------


@pytest.fixture
def agent() -> Iterator[None]:
    created = client.post(
        "/api/agents", json={"id": AGENT_ID, "name": "Agent A", "instructions": "v1 の指示"}
    )
    assert created.status_code == 200, created.text
    try:
        yield
    finally:
        with contextlib.suppress(KeyError, ValueError):
            runtime_repository.delete_agent(AGENT_ID)


def _jobs(monkeypatch: MonkeyPatch, *jobs: SimpleNamespace) -> None:
    store = SimpleNamespace(list=lambda: list(jobs))
    monkeypatch.setattr(agent_router, "evaluation_store", store)


def _job(*, pass_rate: float | None, offset_seconds: float, **overrides: Any) -> SimpleNamespace:
    agent = next(item for item in runtime_repository.list_agents() if item.id == AGENT_ID)
    assert agent.draft_updated_at is not None
    values: dict[str, Any] = {
        "agent_id": AGENT_ID,
        "agent_version": "draft",
        "definition_hash": draft_definition_hash(agent),
        "status": JobStatus.COMPLETED,
        "created_at": agent.draft_updated_at + timedelta(seconds=offset_seconds),
        "set_id": "evset_a",
        "set_name": "評価セット A",
        "results": _cases("質問 1", "質問 2"),
        "summary": SimpleNamespace(
            pass_rate=pass_rate, judged_cases=overrides.pop("judged_cases", 10)
        ),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _cases(*questions: str) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(case=EvaluationCase(id=f"c{index}", question=question, expected="要点"))
        for index, question in enumerate(questions)
    ]


def _publish() -> Any:
    return client.post(f"/api/agents/{AGENT_ID}/publish", json={})


def test_publish_without_gate_needs_no_evaluation(agent: None, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "agent_publish_min_pass_rate", 0.0)
    _jobs(monkeypatch)
    assert _publish().status_code == 200


def test_publish_needs_evaluation_after_last_draft_change(
    agent: None, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "agent_publish_min_pass_rate", 0.8)

    _jobs(monkeypatch)
    missing = _publish()
    assert missing.status_code == 409, missing.text
    assert "合格率 80% 以上" in missing.json()["error_messages"][0]

    # 合格率が足りない・下書きを変える前の評価・公開中の版の評価・失敗した評価は数えない。
    _jobs(
        monkeypatch,
        _job(pass_rate=0.5, offset_seconds=10),
        _job(pass_rate=1.0, offset_seconds=-10),
        _job(pass_rate=1.0, offset_seconds=20, agent_version=1),
        _job(pass_rate=1.0, offset_seconds=20, status=JobStatus.FAILED),
    )
    low = _publish()
    assert low.status_code == 409, low.text
    assert "合格率 50%" in low.json()["error_messages"][0]
    assert "評価セット A" in low.json()["error_messages"][0]

    _jobs(monkeypatch, _job(pass_rate=0.9, offset_seconds=10))
    published = _publish()
    assert published.status_code == 200, published.text
    assert published.json()["data"]["published_version"] == 1

    # 変更の無い再公開は求めない。
    _jobs(monkeypatch)
    assert _publish().status_code == 200


def test_draft_time_moves_only_when_versioned_fields_change(agent: None) -> None:
    def draft_time() -> Any:
        return next(
            item for item in runtime_repository.list_agents() if item.id == AGENT_ID
        ).draft_updated_at

    created = draft_time()
    assert created is not None
    assert client.patch(f"/api/agents/{AGENT_ID}", json={"enabled": False}).status_code == 200
    assert draft_time() == created
    assert client.patch(f"/api/agents/{AGENT_ID}", json={"instructions": "v2"}).status_code == 200
    assert draft_time() > created


# ---- 公開中の版との回帰の門・ケースの数の下限（#2569） ---------------------------------------


def _gate(
    monkeypatch: MonkeyPatch,
    *,
    min_pass_rate: float = 0.0,
    max_drop: float | None = None,
    min_cases: int = 0,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "agent_publish_min_pass_rate", min_pass_rate)
    monkeypatch.setattr(settings, "agent_publish_max_pass_rate_drop", max_drop)
    monkeypatch.setattr(settings, "agent_publish_min_cases", min_cases)


def test_publish_gate_needs_enough_cases(agent: None, monkeypatch: MonkeyPatch) -> None:
    _gate(monkeypatch, min_pass_rate=0.8, min_cases=5)

    _jobs(monkeypatch, _job(pass_rate=1.0, offset_seconds=10, judged_cases=3))
    few = _publish()
    assert few.status_code == 409, few.text
    assert "判定できたケースが 3 件" in few.json()["error_messages"][0]
    assert "必要な 5 件" in few.json()["error_messages"][0]

    _jobs(monkeypatch, _job(pass_rate=1.0, offset_seconds=10, judged_cases=5))
    assert _publish().status_code == 200


def test_publish_gate_compares_with_the_published_version(
    agent: None, monkeypatch: MonkeyPatch
) -> None:
    _gate(monkeypatch, max_drop=0.05)

    # 初めての公開は、下書きの評価だけを求める（比べる公開中の版が無い）。
    _jobs(monkeypatch)
    first = _publish()
    assert first.status_code == 409, first.text
    assert "品質評価で確かめてください" in first.json()["error_messages"][0]
    _jobs(monkeypatch, _job(pass_rate=0.5, offset_seconds=10))
    assert _publish().json()["data"]["published_version"] == 1

    assert client.patch(f"/api/agents/{AGENT_ID}", json={"instructions": "v2"}).status_code == 200
    draft = _job(pass_rate=0.8, offset_seconds=10)

    # 同じ評価セット・同じ評価ケースで公開中の版を評価していなければ断る
    # （ケース・評価セットが違う評価は基準にしない）。
    other_cases = _job(pass_rate=0.9, offset_seconds=5, agent_version=1, results=_cases("別の質問"))
    other_set = _job(pass_rate=0.9, offset_seconds=5, agent_version=1, set_id="evset_b")
    _jobs(monkeypatch, draft, other_cases, other_set)
    missing = _publish()
    assert missing.status_code == 409, missing.text
    assert "公開中の版（v1）も同じ評価セット" in missing.json()["error_messages"][0]

    # 基準 90% から許す下げ幅 5% を超えて下がったら断る。
    baseline = _job(pass_rate=0.9, offset_seconds=5, agent_version=1)
    _jobs(monkeypatch, draft, baseline)
    dropped = _publish()
    assert dropped.status_code == 409, dropped.text
    message = dropped.json()["error_messages"][0]
    assert "合格率 80%" in message and "合格率 90%" in message and "5%" in message

    # 許す下げ幅の中なら公開できる（絶対の閾値は使わない設定）。
    _jobs(monkeypatch, _job(pass_rate=0.85, offset_seconds=10), baseline)
    assert _publish().json()["data"]["published_version"] == 2


@pytest.mark.parametrize(("raw", "expected"), [("", None), ("0.05", 0.05)])
def test_max_pass_rate_drop_reads_empty_as_unset(
    monkeypatch: MonkeyPatch, raw: str, expected: float | None
) -> None:
    """`AGENT_PUBLISH_MAX_PASS_RATE_DROP=` の空の値は、未設定（門を使わない）と同じにする。"""
    monkeypatch.setenv("AGENT_PUBLISH_MAX_PASS_RATE_DROP", raw)
    assert Settings().agent_publish_max_pass_rate_drop == expected


def test_publish_gate_rejects_a_changed_definition_hash(
    agent: None, monkeypatch: MonkeyPatch
) -> None:
    _gate(monkeypatch, min_pass_rate=0.8)
    _jobs(
        monkeypatch, _job(pass_rate=1.0, offset_seconds=10, definition_hash="different-definition")
    )
    assert _publish().status_code == 409


def test_publish_gate_checks_skill_only_changes(agent: None, monkeypatch: MonkeyPatch) -> None:
    _gate(monkeypatch, min_pass_rate=0)
    skill = AgentSkillDefinition(
        id="synthetic-evaluation-gate", name="合成のスキル", instructions="合成の指示"
    )
    skill_registry.upsert_custom(skill)
    try:
        runtime_repository.patch_agent(AGENT_ID, AgentProfilePatch(skill_ids=[skill.id]))
        assert _publish().status_code == 200
        old_job = _job(pass_rate=1, offset_seconds=10)
        skill_registry.upsert_custom(
            skill.model_copy(update={"instructions": "変更後の合成の指示"})
        )
        _gate(monkeypatch, min_pass_rate=0.8)
        _jobs(monkeypatch, old_job)
        assert _publish().status_code == 409
        _jobs(monkeypatch, _job(pass_rate=1, offset_seconds=10))
        assert _publish().status_code == 200
    finally:
        skill_registry.remove(skill.id)
