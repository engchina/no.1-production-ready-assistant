"""業種テンプレート（#780）。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pytest import MonkeyPatch
from security_support import ProductionAuth, client, enable_production_auth, login

from app.features.agent.skills import skill_registry
from app.features.agent.templates import AGENT_TEMPLATES
from app.security.permissions import MENU_AGENTS, MENU_RUNS
from app.security.service import set_security_service


def test_templates_are_complete_and_use_registered_skills() -> None:
    ids = [template.id for template in AGENT_TEMPLATES]
    assert len(ids) == len(set(ids)) == 9
    for template in AGENT_TEMPLATES:
        assert template.name.strip() and template.description.strip() and template.category.strip()
        assert "## 共通のルール" in template.instructions
        assert template.skill_ids, template.id
        for skill_id in template.skill_ids:
            # 組み込みの Skill だけを使う（テンプレートを選んでも Skill が見つからないことが無い）。
            assert skill_registry.get(skill_id) is not None, (template.id, skill_id)
        assert len(template.sample_questions) >= 2, template.id
        assert template.evaluation_cases, template.id
        # 出力の項目は検証済みのモデル（key の重なりが無い）。
        keys = [field.key for field in template.output_fields]
        assert len(keys) == len(set(keys)), template.id


def test_document_check_template_has_output_fields() -> None:
    """書類と業務データの照合のテンプレートは、項目ごとの照合の行を記録する（#2549）。"""
    template = next(item for item in AGENT_TEMPLATES if item.id == "document-data-check")
    assert [field.key for field in template.output_fields] == [
        "item",
        "document_value",
        "system_value",
        "matched",
        "note",
    ]
    assert {field.key for field in template.output_fields if field.required} == {
        "item",
        "document_value",
        "matched",
    }


def test_templates_api() -> None:
    response = client.get("/api/agent-templates")
    assert response.status_code == 200, response.text
    templates = response.json()["data"]["templates"]
    assert [item["id"] for item in templates] == [template.id for template in AGENT_TEMPLATES]
    first = templates[0]
    assert set(first) >= {
        "id",
        "category",
        "name",
        "description",
        "instructions",
        "skill_ids",
        "sample_questions",
        "evaluation_cases",
    }


@pytest.fixture
def auth(monkeypatch: MonkeyPatch) -> Iterator[ProductionAuth]:
    yield enable_production_auth(monkeypatch)
    set_security_service(None)


def test_templates_need_the_agents_menu(auth: ProductionAuth) -> None:
    auth.user_with_permissions("runs-780", [MENU_RUNS])
    assert client.get("/api/agent-templates", headers=login("runs-780")).status_code == 403
    auth.user_with_permissions("agents-780", [MENU_AGENTS])
    assert client.get("/api/agent-templates", headers=login("agents-780")).status_code == 200
