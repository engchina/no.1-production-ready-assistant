"""#1720: AI 構築のオントロジーを best effort で graph にし、graph の破損だけで公開を止める。

実 OCI / Oracle は使わない（sql_assist_sample の表 3 つ・view 2 つを模した schema context）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.features.nl2sql.ontology_build import qa_markdown_sections
from app.features.nl2sql.ontology_definition_validation import validate_definitions
from app.features.nl2sql.ontology_definitions import (
    DefinitionEvidence,
    DefinitionMapping,
    LinkTypeDefinition,
    MetricDefinitionV2,
    ObjectTypeDefinition,
    ProfileOntologyBundle,
    PropertyDefinition,
)
from app.features.nl2sql.ontology_unified_model import (
    merge_definitions,
    render_concepts,
    render_llm_context,
    repair_definitions,
)

COLUMNS = {
    "DEPARTMENT": ["DEPARTMENT_ID", "DEPARTMENT_NAME", "LOCATION", "CREATED_AT"],
    "EMPLOYEE": ["EMPLOYEE_ID", "DEPARTMENT_ID", "EMPLOYEE_NAME", "EMAIL", "HIRE_DATE", "SALARY"],
    "PROJECT": ["PROJECT_ID", "DEPARTMENT_ID", "PROJECT_NAME", "START_DATE", "BUDGET"],
    "V_DEPT_PROJECT": ["PROJECT_ID", "PROJECT_NAME", "BUDGET", "DEPARTMENT_NAME"],
    "V_EMP_DEPT": ["EMPLOYEE_ID", "EMPLOYEE_NAME", "SALARY", "DEPARTMENT_NAME", "LOCATION"],
}
SCHEMA: dict[str, Any] = {
    "objects": [
        {
            "owner": "ADMIN",
            "object_name": name,
            "object_type": "view" if name.startswith("V_") else "table",
            "columns": [
                {"column": column, "data_type": "NUMBER" if column.endswith("_ID") else "VARCHAR2"}
                for column in columns
            ],
            "constraints": [],
        }
        for name, columns in COLUMNS.items()
    ]
}


def mapping(table: str, column: str = "", expression: str = "") -> DefinitionMapping:
    return DefinitionMapping(
        owner="ADMIN", object_name=table, column_name=column, expression_sql=expression
    )


def evidence(locator: str) -> DefinitionEvidence:
    # LLM は資料の検証値を "N/A" と書き、原文の引用は schema の JSON と一致しない。
    return DefinitionEvidence(
        source_id="schema",
        locator=locator,
        source_kind="database",
        source_sha256="N/A",
        excerpt_ja="SQL Assist sample",
    )


def obj(api_name: str, name_ja: str, table: str, properties: list[str]) -> ObjectTypeDefinition:
    return ObjectTypeDefinition(
        id=f"object_{api_name}",
        api_name=api_name,
        name_ja=name_ja,
        properties=properties,
        # view 全体を `*` で写した対応（#1720 の MAPPING_OUTSIDE_PROFILE の原因）。
        mappings=[mapping(table, "*", f"ADMIN.{table}.*")],
        evidence=[evidence(f"ADMIN.{table}")],
    )


def prop(api_name: str, name_ja: str, owner: str, table: str, column: str) -> PropertyDefinition:
    return PropertyDefinition(
        id=f"property_{api_name}",
        api_name=api_name,
        name_ja=name_ja,
        object_type=owner,
        data_type="VARCHAR2",
        mappings=[mapping(table, column, f"ADMIN.{table}.{column}")],
        evidence=[evidence(f"ADMIN.{table}.{column}")],
    )


def ai_output() -> list[Any]:
    """OCI で公開を止めた AI の出力の形（列名が同じプロパティを 1 つだけ作り、共有した）。"""

    return [
        obj("department", "部門", "DEPARTMENT", ["department_id", "department_name"]),
        obj("employee", "従業員", "EMPLOYEE", ["employee_id", "department_id", "employee_name"]),
        obj("project", "プロジェクト", "PROJECT", ["project_id", "budget", "start_date"]),
        obj(
            "v_dept_project",
            "部門別プロジェクトビュー",
            "V_DEPT_PROJECT",
            ["project_id", "budget", "department_name"],
        ),
        obj(
            "v_emp_dept",
            "従業員部門ビュー",
            "V_EMP_DEPT",
            ["employee_id", "department_name", "missing_column"],
        ),
        prop("department_id", "部門ID", "department", "DEPARTMENT", "DEPARTMENT_ID"),
        prop("department_name", "部門名", "department", "DEPARTMENT", "DEPARTMENT_NAME"),
        prop("employee_id", "従業員ID", "employee", "EMPLOYEE", "EMPLOYEE_ID"),
        prop("employee_name", "従業員名", "employee", "EMPLOYEE", "EMPLOYEE_NAME"),
        prop("project_id", "プロジェクトID", "project", "PROJECT", "PROJECT_ID"),
        LinkTypeDefinition(
            id="link_employee_department",
            api_name="employee_department",
            name_ja="所属",
            source="employee",
            target="department",
            join_expression_sql="ADMIN.EMPLOYEE.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID",
            evidence=[evidence("ADMIN.EMPLOYEE.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID")],
        ),
        PropertyDefinition(
            kind="value_type", api_name="number_type", name_ja="数値型", data_type="NUMBER"
        ),
    ]


def bundle(definitions: list[Any]) -> ProfileOntologyBundle:
    return ProfileOntologyBundle(
        id="b",
        job_id="job",
        profile_id="sample",
        schema_fingerprint="s",
        profile_fingerprint="p",
        definitions=definitions,
    )


def repaired() -> list[Any]:
    merged, conflicts = merge_definitions("sample", ai_output())
    assert not conflicts
    return repair_definitions("sample", merged, SCHEMA)


def test_ai_output_before_repair_blocks_publication_like_the_oci_report() -> None:
    findings = validate_definitions(bundle(ai_output()), SCHEMA)
    codes = {f.code for f in findings if f.severity == "error"}
    assert {"PROPERTY_OWNER_MISMATCH", "REFERENCE_INVALID"} <= codes
    # `*` の対応は表全体の対応として扱い、範囲外にしない。
    assert "MAPPING_OUTSIDE_PROFILE" not in codes


def test_repair_splits_shared_columns_per_object_and_publishes_without_errors() -> None:
    definitions = repaired()
    by_name = {d.api_name: d for d in definitions}
    view = by_name["v_emp_dept"]
    assert view.properties == ["v_emp_dept_employee_id", "v_emp_dept_department_name"]
    split = by_name["v_emp_dept_department_name"]
    assert (split.object_type, split.name_ja) == ("v_emp_dept", "部門名")
    assert [(m.object_name, m.column_name) for m in split.mappings] == [
        ("V_EMP_DEPT", "DEPARTMENT_NAME")
    ]
    assert by_name["v_dept_project"].properties == [
        "v_dept_project_project_id",
        "v_dept_project_budget",
        "v_dept_project_department_name",
    ]
    assert by_name["employee"].properties == [
        "employee_id",
        "employee_department_id",
        "employee_name",
    ]
    # 未定義の名前は、オブジェクトの表に同じ列があればプロパティを作る（型は schema から）。
    assert by_name["budget"].object_type == "project"
    # 業務の型（Base Type）と物理の型（Physical Type）を分ける（#1729）。
    assert (by_name["budget"].data_type, by_name["budget"].physical_type) == (
        "string",
        "VARCHAR2",
    )
    assert "start_date" in by_name
    # 列が無い参照は外す（推測で列を足さない）。
    assert "missing_column" not in by_name
    assert all(not m.column_name for m in view.mappings)

    findings = validate_definitions(bundle(definitions), SCHEMA)
    assert [f for f in findings if f.severity == "error"] == []
    assert all(f.definition_id for f in findings)
    # view の識別子・粒度が無くても公開でき、補えば精度が上がる項目（info）にだけ出る。
    identity = [f for f in findings if f.code == "OBJECT_IDENTITY_REQUIRED"]
    assert identity and {f.severity for f in identity} == {"info"}
    # DB の schema を根拠にした evidence は照合済み（資料の検証値が無くても）。
    unverified = {f.definition_id for f in findings if f.code == "INFERENCE_WITHOUT_EVIDENCE"}
    assert unverified == {d.id for d in definitions if not d.evidence}
    assert all(e.verified for d in definitions for e in d.evidence)


def test_repair_is_idempotent() -> None:
    once = repaired()
    twice = repair_definitions("sample", once, SCHEMA)
    assert [d.model_dump() for d in twice] == [d.model_dump() for d in once]


def test_mapping_outside_profile_names_the_object_and_the_fix() -> None:
    outside = obj("other", "他", "OTHER", [])
    missing = prop("ghost", "幽霊", "department", "DEPARTMENT", "GHOST")
    findings = validate_definitions(bundle([*repaired(), outside, missing]), SCHEMA)
    messages = {
        f.definition_id: f.message_ja for f in findings if f.code == "MAPPING_OUTSIDE_PROFILE"
    }
    assert (
        "表・ビュー ADMIN.OTHER は業務プロファイルの対象に入っていません"
        in (messages["object_other"])
    )
    assert "業務プロファイルの対象に追加" in messages["object_other"]
    assert "列 ADMIN.DEPARTMENT.GHOST は表・ビューに無いか" in messages["property_ghost"]


def test_merge_unions_property_lists_instead_of_conflicting() -> None:
    first = obj("employee", "従業員", "EMPLOYEE", ["employee_id"])
    second = obj("employee", "従業員", "EMPLOYEE", ["employee_id", "employee_name"])
    merged, conflicts = merge_definitions("sample", [first, second])
    assert not conflicts
    assert isinstance(merged[0], ObjectTypeDefinition)
    assert merged[0].properties == ["employee_id", "employee_name"]


def test_llm_context_keeps_business_knowledge_and_drops_internal_details() -> None:
    definitions = [
        *repaired(),
        MetricDefinitionV2(
            api_name="headcount",
            name_ja="人数",
            expression_sql="COUNT(ADMIN.EMPLOYEE.EMPLOYEE_ID)",
            aggregation="count",
            missing_information_ja=["単位が未設定"],
        ),
    ]
    context = render_llm_context(definitions)
    assert context.startswith("# 業務オントロジー\n")
    assert "### 従業員部門ビュー（ADMIN.V_EMP_DEPT）" in context
    # 標準の用語（業務の型・物理の型・結合のキー・測定値・集計方法）で書く（#1729）。
    assert "## オブジェクト型（Object Type）" in context
    assert (
        "  - 部門名（列: ADMIN.V_EMP_DEPT.DEPARTMENT_NAME、業務の型: 文字列（string）、"
        "物理の型: VARCHAR2）"
    ) in context
    assert "## リンク型（Link Type）" in context
    assert "結合のキー: ADMIN.EMPLOYEE.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID" in context
    assert "- 人数（測定値: COUNT(ADMIN.EMPLOYEE.EMPLOYEE_ID)、集計方法: count）" in context
    for internal in (
        "なし",
        "未設定",
        "verified",
        "source_id",
        "N/A",
        "数値型",
        "下書き",
        "単位が未設定",
        "（式: ADMIN.",
        "api_name",
    ):
        assert internal not in context, internal
    assert render_llm_context([]) == ""


def test_draft_markdown_drops_defaults_redundant_expressions_and_notes() -> None:
    markdown = render_concepts(repaired())
    assert "業務で使用する対象" not in markdown
    assert "必須: いいえ" not in markdown and "更新可能: いいえ" not in markdown
    assert "（式: ADMIN.DEPARTMENT.DEPARTMENT_NAME）" not in markdown
    assert "補足:" not in markdown
    assert "ADMIN.DEPARTMENT.DEPARTMENT_NAME" in markdown


@pytest.mark.parametrize(
    "markdown,expected",
    [
        ("## Q/A 由来 SQL 生成ルール\n- なし\n\n## Q/A SQL 構造パターン\n- なし\n", ""),
        (
            "## Q/A 由来 SQL 生成ルール\n- 受注は APP.ORDERS を使う。\n\n"
            "## Q/A SQL 構造パターン\n- なし\n\n## オントロジーの概念定義\n",
            "## Q/A 由来 SQL 生成ルール\n- 受注は APP.ORDERS を使う。",
        ),
    ],
    ids=["empty", "rules-only"],
)
def test_qa_sections_are_kept_only_when_they_have_content(markdown: str, expected: str) -> None:
    assert qa_markdown_sections(markdown) == expected


def test_ai_placeholders_duplicates_and_structural_notes_are_pruned() -> None:
    """AI が種類ごとに出した空の雛形・同じ列の重複・空の項目の書き写しを除く（AI の出力だけ）。"""

    placeholders = [
        ObjectTypeDefinition(api_name="object_type", name_ja="オブジェクト"),
        PropertyDefinition(api_name="property", name_ja="プロパティ"),
        LinkTypeDefinition(api_name="link_type", name_ja="関係", source="", target=""),
        MetricDefinitionV2(api_name="metric", name_ja="指標"),
    ]
    duplicate = prop(
        "department_department_name", "部門名", "department", "DEPARTMENT", "DEPARTMENT_NAME"
    )
    department = obj("department", "部門", "DEPARTMENT", ["department_name"])
    department.title_property = "department_department_name"
    department.missing_information_ja = [
        "description_ja: 記載なし",
        "資料 A と B で部門の定義が違う",
    ]
    values: list[Any] = [
        department,
        prop("department_name", "部門名", "department", "DEPARTMENT", "DEPARTMENT_NAME"),
        duplicate,
        *placeholders,
    ]
    pruned: dict[str, Any] = {
        d.api_name: d for d in repair_definitions("sample", values, SCHEMA, ai_output=True)
    }
    assert set(pruned) == {"department", "department_name"}
    assert pruned["department"].title_property == "department_name"
    assert pruned["department"].missing_information_ja == ["資料 A と B で部門の定義が違う"]
    # 利用者が Markdown に書いた定義（ai_output でない）は消さず、検証の診断に任せる。
    # ただし表の決まらないオブジェクト型は公開に含めず、同じ列のプロパティは 1 つにする（#1729）。
    kept = {d.api_name for d in repair_definitions("sample", values, SCHEMA)}
    assert {"property", "department_name", "metric"} <= kept
    assert not {"object_type", "department_department_name"} & kept


def test_orphan_property_is_adopted_by_the_object_that_lists_it() -> None:
    orphan = prop("email", "メール", "", "EMPLOYEE", "EMAIL")
    employee = obj("employee", "従業員", "EMPLOYEE", ["email"])
    by_name: dict[str, Any] = {
        d.api_name: d for d in repair_definitions("sample", [employee, orphan], SCHEMA)
    }
    assert by_name["employee"].properties == ["email"]
    assert by_name["email"].object_type == "employee"


def test_kind_name_ids_from_ai_do_not_merge_different_concepts() -> None:
    """AI が全定義の id に種類名を付けても、別の表・列の定義を 1 つに併合しない。"""

    values = ai_output()
    for d in values:
        d.id = d.kind
    merged, _ = merge_definitions("sample", values)
    objects = [d.api_name for d in merged if d.kind == "object_type"]
    assert objects == ["department", "employee", "project", "v_dept_project", "v_emp_dept"]
    assert len({d.id for d in merged}) == len(merged)


def test_ai_property_mappings_are_limited_to_the_owning_object_tables() -> None:
    bundled = prop("department_id", "部門ID", "department", "DEPARTMENT", "DEPARTMENT_ID")
    bundled.mappings = [*bundled.mappings, mapping("EMPLOYEE", "EMPLOYEE_ID")]
    values: list[Any] = [obj("department", "部門", "DEPARTMENT", ["department_id"]), bundled]
    by_name: dict[str, Any] = {
        d.api_name: d for d in repair_definitions("sample", values, SCHEMA, ai_output=True)
    }
    assert [(m.object_name, m.column_name) for m in by_name["department_id"].mappings] == [
        ("DEPARTMENT", "DEPARTMENT_ID")
    ]
