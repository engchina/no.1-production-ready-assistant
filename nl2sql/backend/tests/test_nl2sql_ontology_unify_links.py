"""#1729: 同じ物を指す概念の統合・物理での併合の判定・関係の作成・改善案・用語の対応表。

実 OCI / Oracle は使わない（sql_assist_sample の表 3 つ・view 2 つを模した schema context）。
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from app.features.nl2sql.ontology_definition_validation import validate_definitions
from app.features.nl2sql.ontology_definitions import (
    DefinitionMapping,
    LinkTypeDefinition,
    ObjectTypeDefinition,
    ProfileOntologyBundle,
    PropertyDefinition,
)
from app.features.nl2sql.ontology_improvement_suggestion import improvement_suggestions
from app.features.nl2sql.ontology_terms import CONCEPT_LABELS, FIELD_LABELS
from app.features.nl2sql.ontology_unified_model import (
    base_data_type,
    merge_definitions,
    render_concepts,
    repair_definitions_report,
    schema_resolved_conflicts,
)

COLUMNS = {
    "DEPARTMENT": {
        "DEPARTMENT_ID": "NUMBER",
        "DEPARTMENT_NAME": "VARCHAR2(50)",
        "LOCATION": "VARCHAR2(50)",
        "CREATED_AT": "DATE",
    },
    "EMPLOYEE": {
        "EMPLOYEE_ID": "NUMBER",
        "DEPARTMENT_ID": "NUMBER",
        "EMPLOYEE_NAME": "VARCHAR2(100)",
        "SALARY": "NUMBER(10,2)",
    },
    "PROJECT": {
        "PROJECT_ID": "NUMBER",
        "DEPARTMENT_ID": "NUMBER",
        "PROJECT_NAME": "VARCHAR2(100)",
        "START_DATE": "DATE",
        "BUDGET": "NUMBER(12,2)",
    },
    "V_DEPT_PROJECT": {
        "PROJECT_ID": "NUMBER",
        "PROJECT_NAME": "VARCHAR2(100)",
        "DEPARTMENT_NAME": "VARCHAR2(50)",
    },
    "V_EMP_DEPT": {"EMPLOYEE_ID": "NUMBER", "DEPARTMENT_NAME": "VARCHAR2(50)"},
}
PRIMARY_KEYS = {"DEPARTMENT": "DEPARTMENT_ID", "EMPLOYEE": "EMPLOYEE_ID", "PROJECT": "PROJECT_ID"}
SCHEMA: dict[str, Any] = {
    "objects": [
        {
            "object": f"ADMIN.{name}",
            "owner": "ADMIN",
            "object_name": name,
            "object_type": "view" if name.startswith("V_") else "table",
            "columns": [
                {
                    "column": column,
                    "data_type": data_type,
                    # 表の主キーと所属の部門 ID は NOT NULL（ビューは制約を持たない）。
                    "nullable": name.startswith("V_")
                    or column not in {PRIMARY_KEYS.get(name), "DEPARTMENT_ID"},
                }
                for column, data_type in columns.items()
            ],
            "constraints": (
                [{"constraint_type": "P", "columns": [PRIMARY_KEYS[name]]}]
                if name in PRIMARY_KEYS
                else []
            ),
        }
        for name, columns in COLUMNS.items()
    ],
    "relationships": [],
}


def table(name: str) -> list[DefinitionMapping]:
    return [DefinitionMapping(owner="ADMIN", object_name=name)]


def column(name: str, col: str) -> list[DefinitionMapping]:
    return [DefinitionMapping(owner="ADMIN", object_name=name, column_name=col)]


def obj(api: str, name: str, tbl: str = "", props: list[str] | None = None, **kw: Any) -> Any:
    return ObjectTypeDefinition(
        api_name=api,
        name_ja=name,
        properties=props or [],
        mappings=table(tbl) if tbl else [],
        **kw,
    )


def prop(api: str, name: str, owner: str, tbl: str, col: str, **kw: Any) -> Any:
    return PropertyDefinition(
        api_name=api, name_ja=name, object_type=owner, mappings=column(tbl, col), **kw
    )


def bundle(definitions: list[Any]) -> ProfileOntologyBundle:
    return ProfileOntologyBundle(
        id="b",
        profile_id="sample",
        job_id="j",
        schema_fingerprint="s",
        profile_fingerprint="p",
        definitions=definitions,
    )


def test_kind_name_api_names_and_different_columns_or_links_are_not_merged() -> None:
    """AI が API 名を種類の名前にしても、別の列・別の関係を 1 つに併合しない（競合にしない）。"""

    values = [
        obj("employee", "従業員", "EMPLOYEE"),
        obj("project", "プロジェクト", "PROJECT"),
        obj("department", "部門", "DEPARTMENT"),
        prop("property", "部門ID", "employee", "EMPLOYEE", "DEPARTMENT_ID", data_type="NUMBER"),
        prop("property", "開始日", "project", "PROJECT", "START_DATE", data_type="DATE"),
        LinkTypeDefinition(
            api_name="link_type",
            name_ja="所属",
            source="employee",
            target="department",
            join_expression_sql="ADMIN.EMPLOYEE.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID",
        ),
        LinkTypeDefinition(
            api_name="link_type",
            name_ja="担当",
            source="project",
            target="department",
            join_expression_sql="ADMIN.PROJECT.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID",
        ),
    ]
    merged, conflicts = merge_definitions("sample", values)
    assert conflicts == []
    names = {d.api_name for d in merged}
    assert {"employee_department_id", "project_start_date"} <= names
    assert {"employee_to_department", "project_to_department"} <= names
    assert not {"property", "link_type"} & names


def test_same_name_with_different_columns_is_split_and_types_are_separated() -> None:
    first = prop("department_id", "部門ID", "employee", "EMPLOYEE", "DEPARTMENT_ID")
    second = prop("department_id", "部門ID", "project", "PROJECT", "DEPARTMENT_ID")
    merged, conflicts = merge_definitions("sample", [first, second])
    assert len(merged) == 2 and conflicts == []
    typed, _ = merge_definitions(
        "sample", [prop("budget", "予算", "project", "PROJECT", "BUDGET", data_type="NUMBER(12,2)")]
    )
    budget: Any = typed[0]
    assert (budget.data_type, budget.physical_type) == ("number", "NUMBER(12,2)")


@pytest.mark.parametrize(
    "value,expected",
    [
        ("NUMBER(12,2)", "number"),
        ("NUMBER(10)", "integer"),
        ("NUMBER", "number"),
        ("VARCHAR2(50 CHAR)", "string"),
        ("DATE", "date"),
        ("TIMESTAMP(6) WITH TIME ZONE", "datetime"),
        ("整数（integer）", "integer"),
        ("decimal", "number"),
        ("geometry", ""),
    ],
    ids=["decimal", "integer", "number", "varchar", "date", "timestamp", "label", "alias", "other"],
)
def test_base_data_type(value: str, expected: str) -> None:
    assert base_data_type(value) == expected


def sample_with_duplicates() -> list[Any]:
    """利用者の画面の再現: 前の下書きの「部署」（表あり・プロパティ無し）と、LLM の解析で
    表の対応が落ちた「部門」（プロパティあり）が同じ DEPARTMENT を指す。"""

    return [
        obj("ADMIN.DEPARTMENT", "部署", "DEPARTMENT", aliases=["部署マスタ", "department"]),
        obj("ADMIN.EMPLOYEE", "従業員", "EMPLOYEE"),
        obj(
            "department",
            "部門",
            props=["department_id", "department_name"],
            aliases=["profile_concept_15f6dedd361d01f2d48f6afb"],
        ),
        obj("employee", "従業員", props=["employee_id", "department_id_fk_emp"]),
        prop("department_id", "部門ID", "department", "DEPARTMENT", "DEPARTMENT_ID"),
        prop("department_name", "部門名", "department", "DEPARTMENT", "DEPARTMENT_NAME"),
        prop("employee_id", "従業員ID", "employee", "EMPLOYEE", "EMPLOYEE_ID"),
        prop("department_id_fk_emp", "部門ID（従業員）", "employee", "EMPLOYEE", "DEPARTMENT_ID"),
        LinkTypeDefinition(
            api_name="employee_to_department",
            name_ja="所属",
            source="employee",
            target="department",
            cardinality="many_to_one",
            join_expression_sql="ADMIN.EMPLOYEE.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID",
        ),
        obj("concept_only", "資料だけの概念", props=[], description_ja="表の無い概念"),
        prop("concept_note", "メモ", "concept_only", "", ""),
    ]


def test_objects_pointing_to_the_same_table_are_unified_and_unmapped_ones_are_dropped() -> None:
    merged, _ = merge_definitions("sample", sample_with_duplicates())
    report = repair_definitions_report("sample", merged, SCHEMA)
    objects = {d.api_name: d for d in report.definitions if d.kind == "object_type"}
    assert set(objects) == {"department", "employee"}
    department = objects["department"]
    assert [(m.object_name, m.column_name) for m in department.mappings] == [("DEPARTMENT", "")]
    # 消えた方の名前は別名（Alias）に。内部の ID は別名にしない。
    assert "部署" in department.aliases and "部署マスタ" in department.aliases
    assert not any(a.startswith("profile_concept_") for a in department.aliases)
    assert report.remap == {"ADMIN.DEPARTMENT": "department", "ADMIN.EMPLOYEE": "employee"}
    # 表の決まらないオブジェクト型と、それに属する定義は公開に含めない（公開を止めない）。
    assert {d.api_name for d in report.dropped} == {"concept_only", "concept_note"}
    findings = validate_definitions(bundle(report.definitions), SCHEMA)
    assert [f.code for f in findings if f.severity == "error"] == []


def test_links_are_created_from_primary_keys_with_the_same_name_and_type() -> None:
    values = [
        obj("department", "部門", "DEPARTMENT"),
        obj("employee", "従業員", "EMPLOYEE"),
        obj("project", "プロジェクト", "PROJECT"),
        obj("v_dept_project", "部門別プロジェクトビュー", "V_DEPT_PROJECT"),
        obj("v_emp_dept", "従業員部門ビュー", "V_EMP_DEPT"),
    ]
    report = repair_definitions_report("sample", values, SCHEMA, ai_output=True)
    links = {
        (d.source, d.target): d for d in report.definitions if isinstance(d, LinkTypeDefinition)
    }
    assert set(links) == {
        ("employee", "department"),
        ("project", "department"),
        ("v_dept_project", "project"),
        ("v_emp_dept", "employee"),
    }
    employee = links[("employee", "department")]
    assert employee.join_expression_sql == (
        "ADMIN.EMPLOYEE.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID"
    )
    assert employee.cardinality == "many_to_one"
    assert (employee.name_ja, employee.inverse_name_ja) == ("従業員の部門", "部門の従業員")
    assert employee.evidence[0].source_kind == "database"
    # 既に関係があれば足さず、利用者の Markdown（ai_output でない）には足さない。
    again = repair_definitions_report("sample", report.definitions, SCHEMA, ai_output=True)
    assert sum(d.kind == "link_type" for d in again.definitions) == 4
    plain = repair_definitions_report("sample", values, SCHEMA)
    assert not any(d.kind == "link_type" for d in plain.definitions)


def test_foreign_keys_are_used_and_generic_id_columns_are_not_guessed() -> None:
    schema = {
        "objects": [
            {
                "object": f"APP.{name}",
                "owner": "APP",
                "object_name": name,
                "object_type": "table",
                "columns": [{"column": c, "data_type": "NUMBER"} for c in columns],
                "constraints": [{"constraint_type": "P", "columns": ["ID"]}],
            }
            for name, columns in {"ORDERS": ["ID", "BUYER"], "CUSTOMERS": ["ID"]}.items()
        ],
        "relationships": [
            {
                "kind": "foreign_key",
                "id": "FK_ORDERS_BUYER",
                "source_object": "APP.ORDERS",
                "target_object": "APP.CUSTOMERS",
                "cardinality": "many_to_one",
                "join_conditions": [{"left": "APP.ORDERS.BUYER", "right": "APP.CUSTOMERS.ID"}],
            }
        ],
    }
    values: list[Any] = [
        ObjectTypeDefinition(
            api_name="order",
            name_ja="受注",
            mappings=[DefinitionMapping(owner="APP", object_name="ORDERS")],
        ),
        ObjectTypeDefinition(
            api_name="customer",
            name_ja="顧客",
            mappings=[DefinitionMapping(owner="APP", object_name="CUSTOMERS")],
        ),
    ]
    links = [
        d
        for d in repair_definitions_report("s", values, schema, ai_output=True).definitions
        if isinstance(d, LinkTypeDefinition)
    ]
    # `ID` だけの列名の一致では作らず、外部キーの 1 本だけ。
    assert [(d.source, d.target, d.join_expression_sql) for d in links] == [
        ("order", "customer", "APP.ORDERS.BUYER = APP.CUSTOMERS.ID")
    ]
    assert "FK_ORDERS_BUYER" in links[0].description_ja


def test_conflicts_are_plain_sentences_with_business_names() -> None:
    owner = obj("employee", "従業員", "EMPLOYEE")
    first = prop("dept", "部門ID", "employee", "EMPLOYEE", "DEPARTMENT_ID", required=True)
    second = prop("dept", "部門ID", "employee", "EMPLOYEE", "DEPARTMENT_ID", required=False)
    _, conflicts = merge_definitions("sample", [owner, first, second])
    assert conflicts == [
        "『従業員』の『部門ID』の必須が 2 通り（はい と いいえ）作られました。はい を使います。"
        "違う場合は Markdown の『部門ID』の「必須」の行を直してください。"
    ]
    detail = conflicts[0].detail()  # type: ignore[attr-defined]
    assert detail == {"api_name": "dept", "field": "required", "values": ["はい", "いいえ"]}
    for text in conflicts:
        assert not re.search(r"profile_concept_|mappings|link_type|DEFINITION_CONFLICT", text)


def test_improvement_suggestions_state_facts_and_leave_unknowns_blank() -> None:
    definitions = [
        obj("department", "部門", "DEPARTMENT", aliases=["部署"]),
        obj("employee", "従業員", "EMPLOYEE"),
        obj("v_emp_dept", "従業員部門ビュー", "V_EMP_DEPT"),
        prop("employee_department_id", "部門ID", "employee", "EMPLOYEE", "DEPARTMENT_ID"),
        LinkTypeDefinition(
            api_name="employee_to_department",
            name_ja="所属",
            source="employee",
            target="department",
            cardinality="many_to_one",
            join_expression_sql="ADMIN.EMPLOYEE.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID",
        ),
    ]
    findings: list[dict[str, Any]] = [
        {"severity": "info", "code": "OBJECT_GRAIN_REQUIRED", "definition_id": "department"},
        {"severity": "info", "code": "OBJECT_GRAIN_REQUIRED", "definition_id": "employee"},
        {"severity": "info", "code": "OBJECT_IDENTITY_REQUIRED", "definition_id": "v_emp_dept"},
        {"severity": "info", "code": "OBJECT_GRAIN_REQUIRED", "definition_id": "v_emp_dept"},
        {
            "severity": "warning",
            "code": "DEFINITION_CONFLICT",
            "detail": {
                "api_name": "employee_department_id",
                "field": "data_type",
                "values": ["数値（decimal）", "日付（date）"],
            },
        },
        {"severity": "error", "code": "OBJECT_EXCLUDED_WITHOUT_TABLE", "definition_id": "memo"},
    ]
    excluded = [obj("memo", "メモ")]
    result = improvement_suggestions(definitions, findings, SCHEMA, excluded=excluded)
    texts = [item["text"] for item in result]
    # 公開を止めるエラーを解く文を先に、警告・補う項目を後に並べる。
    assert [item["severity"] for item in result] == sorted(
        (item["severity"] for item in result), key=["error", "warning", "info"].index
    )
    assert texts[0] == (
        "メモ（memo）は（対応する表・ビューの名前を確認して書いてください）の表で、"
        "（1 行が何を表すかを確認して書いてください）、"
        "主キーは（1 行を特定する列を確認して書いてください）。"
    )
    # 型の食い違いは DB の列の型（事実）を書く。
    assert (
        "従業員の部門IDは ADMIN.EMPLOYEE.DEPARTMENT_ID の列で、型は NUMBER（数値（decimal））。"
        in texts
    )
    assert (
        "部門・部署（department）は ADMIN.DEPARTMENT の表で、1 行が 1 つの部門、"
        "主キーは DEPARTMENT_ID。"
    ) in texts
    assert (
        "従業員（employee）は ADMIN.EMPLOYEE の表で、1 行が 1 つの従業員、主キーは EMPLOYEE_ID、"
        "DEPARTMENT_ID で部門に属する（多対 1（many-to-one））。"
    ) in texts
    # ビューの主キーは推測しない。
    assert (
        "従業員部門ビュー（v_emp_dept）は ADMIN.V_EMP_DEPT のビューで、"
        "（1 行が何を表すかを確認して書いてください）、"
        "主キーは（1 行を特定する列を確認して書いてください）。"
    ) in texts
    assert len(texts) == len(set(texts))
    for text in texts:
        assert "profile_concept_" not in text and "OBJECT_" not in text
    assert improvement_suggestions(definitions, [], SCHEMA) == []


def test_terms_are_shared_with_the_frontend_i18n() -> None:
    """用語の訳は 1 つの対応表。画面の i18n（ontologyResults.kind.* / field.*）と同じ訳を使う。"""

    i18n = (Path(__file__).resolve().parents[2] / "frontend" / "src" / "lib" / "i18n.ts").read_text(
        encoding="utf-8"
    )

    def label(key: str) -> str | None:
        match = re.search(rf'"{re.escape(key)}":\s*"([^"]*)"', i18n)
        return match.group(1) if match else None

    for kind, text in CONCEPT_LABELS.items():
        assert label(f"ontologyResults.kind.{kind}") == text, kind
    for field, text in FIELD_LABELS.items():
        assert label(f"ontologyResults.field.{field}") == text, field
    terms = json.loads(
        (
            Path(__file__).resolve().parents[1] / "app/features/nl2sql/ontology_terms.json"
        ).read_text()
    )
    assert set(terms["kinds"]) == set(CONCEPT_LABELS)


def test_markdown_uses_the_standard_structure_and_japanese_labels() -> None:
    report = repair_definitions_report(
        "sample",
        [
            obj(
                "department",
                "部門",
                "DEPARTMENT",
                props=["department_id"],
                primary_key=["department_id"],
                title_property="department_id",
                description_ja="会社の組織の単位。",
                aliases=["部署"],
            ),
            prop("department_id", "部門ID", "department", "DEPARTMENT", "DEPARTMENT_ID"),
            obj("employee", "従業員", "EMPLOYEE"),
        ],
        SCHEMA,
        ai_output=True,
    )
    markdown = render_concepts(report.definitions)
    for line in (
        "#### オブジェクト型（Object Type）",
        "##### 部門 (`department`)",
        "- 説明（Definition）: 会社の組織の単位。",
        "- 別名（Alias）: 部署",
        "- 対応する表・列（Datasource）: ADMIN.DEPARTMENT",
        "- 主キー（Primary Key）: department_id",
        "- タイトルのプロパティ（Title Property）: department_id",
        "#### プロパティ（Property）",
        "- 業務の型（Base Type）: 数値（decimal）",
        "- 物理の型（Physical Type）: NUMBER",
        "#### リンク型（Link Type）",
        "- 多重度（Cardinality）: 多対 1（many-to-one）",
        "- 結合のキー（Join Key）: ADMIN.EMPLOYEE.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID",
        "- 逆向きの名前（Inverse Name）: 部門の従業員",
    ):
        assert line in markdown, line
    # 英語だけの見出し・項目名を使わない。
    for english in ("Object Type:", "link_type", "mappings", "data_type"):
        assert f"- {english}" not in markdown and f"#### {english}" not in markdown


def test_schema_decides_types_and_required_and_physical_names_borrow_business_names() -> None:
    """DB の列の型・NOT NULL は列が正。決まった競合は利用者に出さない。列名のままの表示名は
    同じ列名の別のプロパティの業務の名前を使う（推測で訳さない）。"""

    owner = obj("employee", "従業員", "EMPLOYEE")
    first = prop("dept", "部門ID", "employee", "EMPLOYEE", "DEPARTMENT_ID", required=True)
    second = prop("dept", "部門ID", "employee", "EMPLOYEE", "DEPARTMENT_ID", required=False)
    view = obj("v_emp_dept", "従業員部門ビュー", "V_EMP_DEPT")
    merged, conflicts = merge_definitions(
        "sample",
        [
            owner,
            first,
            second,
            view,
            prop("v_name", "部門名", "v_emp_dept", "V_EMP_DEPT", "DEPARTMENT_NAME"),
            obj("department", "部門", "DEPARTMENT"),
            prop("dname", "DEPARTMENT_NAME", "department", "DEPARTMENT", "DEPARTMENT_NAME"),
        ],
    )
    assert len(conflicts) == 1
    report = repair_definitions_report("sample", merged, SCHEMA)
    by_name: dict[str, Any] = {d.api_name: d for d in report.definitions}
    assert by_name["dept"].required is True  # NOT NULL の列
    assert schema_resolved_conflicts(conflicts, report.definitions, SCHEMA, report.remap) == []
    assert by_name["dname"].name_ja == "部門名"
    assert "DEPARTMENT_NAME" in by_name["dname"].aliases


@pytest.mark.rule_parser
def test_rendered_markdown_is_read_back_without_llm() -> None:
    """下書きの決まった形は決定論で読み戻す。自由な文・知らない項目は LLM に任せる（None）。"""

    from app.features.nl2sql.ontology_markdown_parser import parse_rendered_markdown

    merged, _ = merge_definitions("sample", sample_with_duplicates()[:9])
    original = repair_definitions_report("sample", merged, SCHEMA, ai_output=True).definitions
    markdown = (
        "## Q/A 由来 SQL 生成ルール\n- 部門は ADMIN.DEPARTMENT を使う。\n\n"
        + render_concepts(original)
    )
    parsed = parse_rendered_markdown(markdown)
    assert parsed is not None
    by_name = {d.api_name: d for d in parsed["definitions"]}
    assert set(by_name) == {d.api_name for d in original}
    department = by_name["department"]
    assert [(m.object_name, m.column_name) for m in department.mappings] == [("DEPARTMENT", "")]
    assert "部署" in department.aliases
    link = by_name["employee_to_department"]
    assert (link.cardinality, link.join_expression_sql) == (
        "many_to_one",
        "ADMIN.EMPLOYEE.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID",
    )
    prop_ = by_name["department_id"]
    assert (prop_.data_type, prop_.physical_type) == ("number", "NUMBER")
    covered = {
        n for line in parsed["lines"] for n in range(line["start_line"], line["end_line"] + 1)
    }
    assert all(n in covered for n, text in enumerate(markdown.splitlines(), 1) if text.strip())
    assert parsed["lines"][1]["disposition"] == "context"
    # 自由な文や、種類に無い項目があれば読まない（LLM の解析に任せる）。
    assert parse_rendered_markdown("受注を受注番号で識別する。") is None
    assert parse_rendered_markdown(markdown + "\n部門は会社の組織です。\n") is None
    assert parse_rendered_markdown(markdown.replace("- 多重度", "- 謎の項目")) is None


@pytest.mark.rule_parser
def test_previous_draft_format_is_read_back() -> None:
    """#1729 より前の下書き（項目名が「対応する表・列」「対象オブジェクト」「データ型」）も読む。"""

    from app.features.nl2sql.ontology_markdown_parser import parse_rendered_markdown

    markdown = (
        "## オントロジーの概念定義\n\n#### オブジェクト型（Object Type）\n\n"
        "##### 部署 (`ADMIN.DEPARTMENT`)\n- 説明: 部署情報。\n- 別名: 部署 / department\n"
        "- 対応する表・列: ADMIN.DEPARTMENT\n\n"
        "##### 部門 (`department`)\n- 対応する表・列: ADMIN.DEPARTMENT\n"
        "- 主識別子: department_id\n- プロパティ: department_id\n\n"
        "#### プロパティ（Property）\n\n##### 部門ID (`department_id`)\n"
        "- 対応する表・列: ADMIN.DEPARTMENT.DEPARTMENT_ID\n- 対象オブジェクト: department\n"
        "- データ型: NUMBER\n- 必須: はい\n"
    )
    parsed = parse_rendered_markdown(markdown)
    assert parsed is not None
    merged, conflicts = merge_definitions("sample", parsed["definitions"])
    report = repair_definitions_report("sample", merged, SCHEMA)
    objects = [d for d in report.definitions if d.kind == "object_type"]
    assert [(o.api_name, o.name_ja) for o in objects] == [("department", "部門")]
    assert report.remap == {"ADMIN.DEPARTMENT": "department"}
    assert conflicts == []


@pytest.mark.rule_parser
def test_preparation_reads_the_rendered_draft_without_llm() -> None:
    """公開前の確認は、下書きの決まった形を LLM を呼ばずに読み、同じ表の概念を 1 つにまとめる。"""

    from test_nl2sql_markdown_unification import Parser
    from test_nl2sql_ontology_definitions import runtime

    from app.features.nl2sql.ontology_markdown_workspace import MarkdownOntologyWorkspace

    rt, _ = runtime()
    base = rt.profile_view("sales")[1]
    markdown = (
        "## オントロジーの概念定義\n\n#### オブジェクト型（Object Type）\n\n"
        "##### 受注 (`APP.ORDERS`)\n- 対応する表・列（Datasource）: APP.ORDERS\n\n"
        "##### 注文 (`order`)\n- 別名（Alias）: 受注\n- プロパティ（Property）: order_id\n\n"
        "#### プロパティ（Property）\n\n##### 受注番号 (`order_id`)\n"
        "- 対応する表・列（Datasource）: APP.ORDERS.ID\n"
        "- 所属のオブジェクト型（Object Type）: order\n"
    )
    rt.create_build_markdown_draft(
        profile_id="sales",
        base_revision_id=base.revision.id,
        payloads=[],
        titles=[],
        markdown=markdown,
        note="確認",
    )
    parser = Parser([])
    rt.legacy_service._enterprise_ai_client = parser
    svc = MarkdownOntologyWorkspace(rt)
    state = rt.ontology_markdown_state("sales")
    preparation = svc.prepare("sales", state.draft_etag, "rule", None)
    svc.run_preparation("sales", preparation["id"])
    result = svc.preparation("sales", preparation["id"])
    for _ in range(200):  # worker が先に取ったときは、終わるまで待つ
        if result["status"] not in {"queued", "running"}:
            break
        threading.Event().wait(0.05)
        result = svc.preparation("sales", preparation["id"])
    assert parser.calls == 0 and result.get("parser") == "rule"
    assert result["status"] == "ready", result["findings"]
    objects = [d for d in result["definitions"] if d["kind"] == "object_type"]
    assert [(o["api_name"], o["name_ja"]) for o in objects] == [("order", "注文")]
    assert "受注" in objects[0]["aliases"]


def test_conflicts_resolved_by_merging_the_same_column_and_wrong_shared_references() -> None:
    """同じ列のプロパティの統合で同じ定義を指すようになった主キーの競合は出さない。AI の共有
    プロパティの参照が別の種類を指すときは、同じ名前の共有プロパティに付け替える。"""

    first = obj("department", "部門", "DEPARTMENT", primary_key=["department_department_id"])
    second = obj("department", "部門", "DEPARTMENT", primary_key=["department_id"])
    values = [
        first,
        second,
        prop("department_department_id", "部門ID", "department", "DEPARTMENT", "DEPARTMENT_ID"),
        prop(
            "department_id",
            "部門ID",
            "department",
            "DEPARTMENT",
            "DEPARTMENT_ID",
            shared_property="department_id",
        ),
        PropertyDefinition(
            kind="shared_property",
            api_name="department_id_shared",
            name_ja="部門ID",
            description_ja="部門を識別する番号。",
        ),
    ]
    merged, conflicts = merge_definitions("sample", values)
    assert len(conflicts) == 1 and "主キー" in conflicts[0]
    report = repair_definitions_report("sample", merged, SCHEMA, ai_output=True)
    assert schema_resolved_conflicts(conflicts, report.definitions, SCHEMA, report.remap) == []
    props = [d for d in report.definitions if d.kind == "property"]
    assert {d.api_name for d in report.definitions if d.kind == "shared_property"} == {
        "department_id_shared"
    }
    assert len(props) == 1
    assert props[0].shared_property == "department_id_shared"
    findings = validate_definitions(bundle(report.definitions), SCHEMA)
    assert [f for f in findings if f.code == "REFERENCE_INVALID"] == []


def test_appended_suggestions_fill_grain_and_primary_key_on_the_next_build() -> None:
    """業務説明に追記した改善案の文は、次の構築で粒度・主キーに入る（空けた所は使わない）。"""

    from app.features.nl2sql.ontology_improvement_suggestion import apply_business_text_facts

    definitions: list[Any] = [
        obj("v_emp_dept", "従業員部門ビュー", "V_EMP_DEPT"),
        prop("v_emp_dept_employee_id", "従業員ID", "v_emp_dept", "V_EMP_DEPT", "EMPLOYEE_ID"),
        obj("v_dept_project", "部門別プロジェクトビュー", "V_DEPT_PROJECT"),
        obj("department", "部門", "DEPARTMENT", grain_ja="部門の単位"),
    ]
    text = (
        "受注の業務です。\n"
        "従業員部門ビュー（v_emp_dept）は ADMIN.V_EMP_DEPT のビューで、"
        "1 行が 1 人の従業員と所属の部門、主キーは EMPLOYEE_ID。\n"
        "部門別プロジェクトビュー（v_dept_project）は ADMIN.V_DEPT_PROJECT のビューで、"
        "（1 行が何を表すかを確認して書いてください）、"
        "主キーは（1 行を特定する列を確認して書いてください）。\n"
        "部門（department）は ADMIN.DEPARTMENT の表で、1 行が 1 つの部門、主キーは DEPARTMENT_ID。"
    )
    assert apply_business_text_facts(definitions, text) == ["v_emp_dept"]
    view = definitions[0]
    assert (view.grain_ja, view.primary_key) == (
        "1 行が 1 人の従業員と所属の部門",
        ["v_emp_dept_employee_id"],
    )
    assert (definitions[2].grain_ja, definitions[2].primary_key) == ("", [])
    assert definitions[3].grain_ja == "部門の単位"  # 値の入った定義は変えない


def test_same_api_name_of_different_kinds_and_same_column_references_are_separated() -> None:
    """種類の違う同じ API 名は分け、同じ名前の列を指す主キーの食い違いは出さない。"""

    values: list[Any] = [
        obj("project", "プロジェクト", "PROJECT", props=["project_id"], primary_key=["project_id"]),
        prop(
            "project_id",
            "プロジェクトID",
            "project",
            "PROJECT",
            "PROJECT_ID",
            shared_property="project_id",
        ),
        PropertyDefinition(
            kind="shared_property",
            api_name="project_id",
            name_ja="プロジェクトID",
            description_ja="プロジェクトを識別する番号。",
        ),
        obj(
            "v_dept_project",
            "部門別プロジェクトビュー",
            "V_DEPT_PROJECT",
            primary_key=["v_dept_project_project_id"],
        ),
        obj(
            "v_dept_project",
            "部門別プロジェクトビュー",
            "V_DEPT_PROJECT",
            primary_key=["project_id"],
        ),
        prop(
            "v_dept_project_project_id",
            "プロジェクトID",
            "v_dept_project",
            "V_DEPT_PROJECT",
            "PROJECT_ID",
        ),
    ]
    merged, conflicts = merge_definitions("sample", values)
    report = repair_definitions_report("sample", merged, SCHEMA, ai_output=True)
    by_name: dict[str, Any] = {d.api_name: d for d in report.definitions}
    assert by_name["project_id"].kind == "property"
    assert by_name["project_id"].shared_property == "project_id_shared_property"
    assert by_name["project_id_shared_property"].kind == "shared_property"
    assert schema_resolved_conflicts(conflicts, report.definitions, SCHEMA, report.remap) == []
    findings = validate_definitions(bundle(report.definitions), SCHEMA)
    assert [f.code for f in findings if f.severity == "error"] == []


def test_object_properties_pointing_to_a_shared_property_get_their_own_column() -> None:
    """オブジェクト型の properties が共有プロパティを指すときは、その表の列のプロパティにする。"""

    shared = PropertyDefinition(
        kind="shared_property",
        api_name="department_id",
        name_ja="部門ID",
        description_ja="部門を識別する番号。",
        mappings=[*column("DEPARTMENT", "DEPARTMENT_ID"), *column("EMPLOYEE", "DEPARTMENT_ID")],
    )
    values: list[Any] = [
        obj("department", "部門", "DEPARTMENT", props=["department_id"]),
        obj("employee", "従業員", "EMPLOYEE", props=["department_id", "employee_id"]),
        prop("employee_id", "従業員ID", "employee", "EMPLOYEE", "EMPLOYEE_ID"),
        shared,
    ]
    merged, _ = merge_definitions("sample", values)
    report = repair_definitions_report("sample", merged, SCHEMA, ai_output=True)
    by_name: dict[str, Any] = {d.api_name: d for d in report.definitions}
    assert by_name["department"].properties == ["department_department_id"]
    assert by_name["employee"].properties == ["employee_department_id", "employee_id"]
    own = by_name["employee_department_id"]
    assert (own.kind, own.object_type, own.shared_property) == (
        "property",
        "employee",
        "department_id",
    )
    findings = validate_definitions(bundle(report.definitions), SCHEMA)
    assert [f.code for f in findings if f.severity == "error"] == []
