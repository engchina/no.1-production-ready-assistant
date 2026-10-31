"""従業員属性の表記差、整形 Markdown と公開の往復を検証する。"""

import json
from typing import Any

import pytest
from test_nl2sql_markdown_unification import Parser
from test_nl2sql_ontology_build import (
    _FakeEnterpriseAiClient,
    _FakeLegacyNl2SqlService,
    _wait_for_job,
)
from test_nl2sql_ontology_definitions import runtime

from app.features.nl2sql.models import SchemaColumn, SchemaTable
from app.features.nl2sql.ontology_build import OntologyBuildService
from app.features.nl2sql.ontology_definitions import BusinessDefinition
from app.features.nl2sql.ontology_markdown_notes import is_evidence_note
from app.features.nl2sql.ontology_markdown_workspace import (
    MarkdownConfirmRequest,
    MarkdownOntologyWorkspace,
)
from app.features.nl2sql.ontology_router import OntologyApiRuntime, OntologyMarkdownDraftPatch
from app.features.nl2sql.ontology_unified_model import (
    DEFINITIONS,
    merge_definitions,
    render_concepts,
)
from app.settings import get_settings


@pytest.fixture(autouse=True)
def settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")


def employees() -> list[BusinessDefinition]:
    fields = {
        "employee_id": ("従業員番号", "integer"),
        "employee_name": ("氏名", "string"),
        "department_id": ("所属部門番号", "number"),
        "email": ("メールアドレス", "string"),
        "hire_date": ("入社日", "date"),
        "salary": ("給与", "number"),
    }
    raw: list[dict[str, Any]] = [
        {
            "kind": "object_type",
            "api_name": "Employee",
            "name_ja": "従業員",
            "primary_key": ["employee_id"],
            "properties": list(fields),
            "grain_ja": "従業員1人",
            "mappings": [
                {"owner": "ADMIN", "object_name": "EMPLOYEE", "expression_sql": "ADMIN.EMPLOYEE"}
            ],
        }
    ]
    for name, (label, data_type) in fields.items():
        for spelling in ("".join(part.title() for part in name.split("_")), name):
            raw.append(
                {
                    "kind": "property",
                    "api_name": spelling,
                    "name_ja": label,
                    "object_type": "Employee",
                    "data_type": "number" if spelling == "employee_id" else data_type,
                    "mappings": [
                        {"owner": "ADMIN", "object_name": "EMPLOYEE", "column_name": name.upper()}
                    ],
                    "evidence": [
                        {
                            "source_id": "schema",
                            "locator": f"ADMIN.EMPLOYEE.{name.upper()}",
                            "excerpt_ja": label,
                        }
                    ],
                }
            )
    return DEFINITIONS.validate_python(raw)


def employee_runtime() -> tuple[OntologyApiRuntime, _FakeLegacyNl2SqlService]:
    rt, legacy = runtime()
    legacy.profile.allowed_tables = ["ADMIN.EMPLOYEE"]
    legacy.catalog.tables = [
        SchemaTable(
            owner="ADMIN",
            table_name="EMPLOYEE",
            logical_name="従業員",
            columns=[
                SchemaColumn(column_name=name, logical_name=name, data_type=kind)
                for name, kind in [
                    ("EMPLOYEE_ID", "NUMBER"),
                    ("EMPLOYEE_NAME", "VARCHAR2"),
                    ("DEPARTMENT_ID", "NUMBER"),
                    ("EMAIL", "VARCHAR2"),
                    ("HIRE_DATE", "DATE"),
                    ("SALARY", "NUMBER"),
                ]
            ],
        )
    ]
    return rt, legacy


def test_employee_spelling_references_and_numeric_types_are_normalized() -> None:
    definitions, conflicts = merge_definitions("sales", employees())
    assert not conflicts
    assert len(definitions) == 7
    entity = next(d for d in definitions if d.kind == "object_type")
    assert entity.primary_key == ["EmployeeId"]
    assert set(entity.properties) == {d.api_name for d in definitions if d.kind == "property"}
    assert entity.mappings[0].expression_sql == ""
    assert (
        next(
            d for d in definitions if d.kind == "property" and d.api_name == "EmployeeId"
        ).data_type
        == "number"
    )
    markdown = render_concepts(definitions)
    for value in (
        "profile_concept_",
        "source_id",
        "verified",
        "evidence",
        "検証不能",
        "要確認事項",
        "### 補助概念",
    ):
        assert value not in markdown
    assert "従業員番号" in markdown and "ADMIN.EMPLOYEE.EMPLOYEE_ID" in markdown
    assert (
        "\n- 所属のオブジェクト型（Object Type）: Employee"
        "\n- 業務の型（Base Type）: 数値（decimal）"
    ) in markdown


def test_same_column_with_distinct_meanings_or_scopes_is_not_merged() -> None:
    values = employees()[1:3]
    values[1] = values[1].model_copy(update={"api_name": "PayrollIdentifier"})
    merged, _ = merge_definitions("sales", values)
    assert len(merged) == 2
    values[1] = values[1].model_copy(
        update={"api_name": "employee_id", "object_type": "Contractor"}
    )
    merged, _ = merge_definitions("sales", values)
    assert len(merged) == 2


def test_real_build_clean_markdown_publishes_without_second_llm() -> None:
    rt, legacy = employee_runtime()
    client = _FakeEnterpriseAiClient(
        json.dumps({"definitions": [d.model_dump(mode="json") for d in employees()]})
    )
    legacy._enterprise_ai_client = client
    service = OntologyBuildService(rt)
    queued = service.start("sales", business_text="従業員の基本情報を管理する。")
    service.run_persisted(queued.id)
    job = _wait_for_job(service, queued.id)
    assert job.status == "succeeded", job.error_message_ja
    calls = len(client.calls)
    assert "profile_concept_" not in job.markdown_output
    assert "source_id" not in job.markdown_output
    workspace = MarkdownOntologyWorkspace(rt)
    prep = workspace.prepare("sales", job.draft_etag, "clean-build", None)
    assert prep.get("generated")
    # 公開に LLM の設定・応答を要求しない。
    legacy._enterprise_ai_client = None
    workspace.run_preparation("sales", prep["id"])
    prep = workspace.preparation("sales", prep["id"])
    assert prep["status"] == "ready", prep["findings"]
    assert len(prep["definitions"]) == 7
    assert len(client.calls) == calls
    workspace.publish(
        "sales",
        MarkdownConfirmRequest(
            preparation_id=prep["id"],
            draft_etag=job.draft_etag,
            confirmed=True,
        ),
        "publish-clean",
        None,
    )
    snapshot = workspace.snapshot("sales")
    assert snapshot is not None
    assert snapshot["markdown"] == job.markdown_output
    assert any(d["evidence"] for d in snapshot["definitions"])


def test_edited_generated_draft_is_reparsed_and_removed_properties_stay_removed() -> None:
    rt, legacy = employee_runtime()
    definitions, _ = merge_definitions("sales", employees())
    base = rt.profile_view("sales")[1]
    rt.create_build_markdown_draft(
        profile_id="sales",
        base_revision_id=base.revision.id,
        payloads=[],
        titles=[],
        markdown=render_concepts(definitions),
        unified_definitions=definitions,
        note="生成",
    )
    kept = [d for d in definitions if d.api_name in {"Employee", "EmployeeId"}]
    kept[0] = kept[0].model_copy(update={"properties": ["EmployeeId"]})
    edited = "従業員を従業員番号で識別する。"
    state = rt.save_ontology_markdown_draft(
        "sales",
        OntologyMarkdownDraftPatch(
            markdown=edited, base_etag=rt.ontology_markdown_state("sales").draft_etag
        ),
    )
    parser = Parser(kept)
    legacy._enterprise_ai_client = parser
    workspace = MarkdownOntologyWorkspace(rt)
    prep = workspace.prepare("sales", state.draft_etag, "edited", None)
    assert not prep.get("generated")
    workspace.run_preparation("sales", prep["id"])
    prep = workspace.preparation("sales", prep["id"])
    assert prep["status"] == "ready", prep["findings"]
    assert parser.calls == 1
    assert {d["api_name"] for d in prep["definitions"]} == {"Employee", "EmployeeId"}


@pytest.mark.parametrize(
    "message, expected",
    [
        ("profile_concept_abc / evidence: 証拠の資料・位置・原文を照合できません。", True),
        ("profile_concept_* 各IDのevidence検証不能", True),
        ("employee_id / evidence検証不能・データ型競合", False),
        ("Oracle SQL 式を検証できません: 対象列がありません。", False),
    ],
)
def test_only_evidence_diagnostics_are_nonblocking(message: str, expected: bool) -> None:
    assert is_evidence_note(message) is expected


def test_old_markdown_naming_and_evidence_reports_do_not_block_resolved_definitions() -> None:
    rt, legacy = employee_runtime()
    original = employees()
    markdown = render_concepts(original)
    # 模擬旧稿の開発診断は入力から除外し、行番号の網羅性は保持する。
    markdown += "- profile_concept_abc / evidence: 証拠の資料・位置・原文を照合できません。\n"
    base = rt.profile_view("sales")[1]
    rt.create_build_markdown_draft(
        profile_id="sales",
        base_revision_id=base.revision.id,
        payloads=[],
        titles=[],
        markdown=markdown,
        note="旧稿",
    )

    class LegacyParser(Parser):
        def generate(self, **kwargs: Any) -> str:
            assert "profile_concept_abc" not in kwargs["context"]
            value = json.loads(super().generate(**kwargs))
            value["issues_ja"] = [
                "Employee / プロパティ命名競合: snake_case vs PascalCase",
                "employee_id / データ型: number vs integer",
                "profile_concept_* 各IDのevidence検証不能",
            ]
            return json.dumps(value)

    parser = LegacyParser(original)
    parser.lines = [
        {
            "start_line": 1,
            "end_line": len(markdown.splitlines()) - 1,
            "disposition": "unresolved",
            "reason_ja": "profile_concept_* 各IDのevidence検証不能",
            "definition_api_names": [d.api_name for d in original],
        }
    ]
    legacy._enterprise_ai_client = parser
    workspace = MarkdownOntologyWorkspace(rt)
    prep = workspace.prepare(
        "sales", rt.ontology_markdown_state("sales").draft_etag, "legacy", None
    )
    workspace.run_preparation("sales", prep["id"])
    result = workspace.preparation("sales", prep["id"])
    assert result["status"] == "ready", result["findings"]
    assert len(result["definitions"]) == 7


def test_generated_definition_reuse_still_checks_current_schema() -> None:
    rt, legacy = employee_runtime()
    definitions, _ = merge_definitions("sales", employees())
    base = rt.profile_view("sales")[1]
    rt.create_build_markdown_draft(
        profile_id="sales",
        base_revision_id=base.revision.id,
        payloads=[],
        titles=[],
        markdown=render_concepts(definitions),
        unified_definitions=definitions,
        note="生成",
    )
    legacy.catalog.tables[0].columns = [
        c for c in legacy.catalog.tables[0].columns if c.column_name != "SALARY"
    ]
    workspace = MarkdownOntologyWorkspace(rt)
    prep = workspace.prepare(
        "sales", rt.ontology_markdown_state("sales").draft_etag, "schema-changed", None
    )
    assert prep.get("generated")
    workspace.run_preparation("sales", prep["id"])
    result = workspace.preparation("sales", prep["id"])
    assert result["status"] == "failed"
    assert any(f["severity"] == "error" and "SALARY" in f["message_ja"] for f in result["findings"])


def test_normalization_preserves_known_identity_and_unspecified_defaults() -> None:
    original = employees()[1:3]
    original[1].id = "known-property-id"
    assert original[1].kind == "property"
    original[1].required = True
    merged, conflicts = merge_definitions("sales", original)
    assert not conflicts
    assert len(merged) == 1
    assert merged[0].id == "known-property-id"
    assert merged[0].kind == "property" and merged[0].required
