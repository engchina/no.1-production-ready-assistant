"""#1733・#1734・#1735: ビューの列の型、共有プロパティの同一性と前の定義、自由な行の解析。

実 OCI / Oracle は使わない（スタブと sql_assist_sample を模した schema context）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_nl2sql_ontology_definitions import runtime
from test_nl2sql_ontology_unify_links import SCHEMA, column, obj, prop
from test_nl2sql_ontology_workspace import model

from app.features.nl2sql.models import SampleDataset
from app.features.nl2sql.ontology_build import (
    _ONTOLOGY_BUILD_LLM_CONTEXT_MAX_CHARS,
    _llm_call_chars,
    _with_prior_context,
)
from app.features.nl2sql.ontology_build_prior import (
    PRIOR_PROMPT,
    plain_prior_findings,
    select_prior_definitions,
    summarize_prior_definitions,
)
from app.features.nl2sql.ontology_definitions import (
    DefinitionMapping,
    PropertyDefinition,
)
from app.features.nl2sql.ontology_markdown_parser import parse_markdown_rules
from app.features.nl2sql.ontology_markdown_workspace import MarkdownOntologyWorkspace
from app.features.nl2sql.ontology_unified_model import (
    merge_definitions,
    render_concepts,
    repair_definitions_report,
)
from app.features.nl2sql.oracle_adapter import _catalog_column_type
from app.features.nl2sql.service import Nl2SqlService
from app.settings import get_settings

# --- #1733 ビューの列の型 -------------------------------------------------------------


def _sample_service() -> Nl2SqlService:
    service = Nl2SqlService.__new__(Nl2SqlService)
    service._current_schema_owner = lambda: "APP"  # type: ignore[method-assign]
    return service


def test_sample_view_columns_take_types_from_base_tables() -> None:
    """サンプルのビューの列は元の表の列の型・NOT NULL を使い、全列 VARCHAR2 にしない。"""

    views = {v.table_name: v for v in _sample_service()._sample_views_from_ddl(SampleDataset.HR)}
    columns = {c.column_name: c for c in views["V_DEPT_PROJECT"].columns}
    assert columns["PROJECT_ID"].data_type == "NUMBER"
    assert columns["PROJECT_ID"].nullable is False
    assert columns["BUDGET"].data_type == "NUMBER(12,2)"
    assert columns["DEPARTMENT_NAME"].data_type == "VARCHAR2(50)"
    assert {c.data_type for c in views["V_EMP_DEPT"].columns} != {"VARCHAR2"}


def test_sample_view_expression_column_type_is_unknown() -> None:
    aliases = {"P": "PROJECT"}
    tables = {
        table.table_name: {c.column_name: c for c in table.columns}
        for table in _sample_service()._sample_tables_from_ddl(SampleDataset.HR)
    }
    source = Nl2SqlService._sample_view_source_column
    assert source("p.BUDGET * 2 AS DOUBLE_BUDGET", aliases, tables) is None
    found = source("p.PROJECT_ID", aliases, tables)
    assert found is not None and found.data_type == "NUMBER"


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (("NUMBER", 22, 0, 12, 2, None), "NUMBER(12,2)"),
        (("NUMBER", 22, 0, None, None, None), "NUMBER"),
        (("NUMBER", 22, 0, 10, 0, None), "NUMBER(10)"),
        (("VARCHAR2", 50, 50, None, None, "B"), "VARCHAR2(50)"),
        (("VARCHAR2", 400, 100, None, None, "C"), "VARCHAR2(100 CHAR)"),
        (("DATE", 7, 0, None, None, None), "DATE"),
    ],
    ids=["number-scale", "number", "number-int", "varchar-byte", "varchar-char", "date"],
)
def test_catalog_column_type_keeps_precision_and_length(
    row: tuple[Any, ...], expected: str
) -> None:
    assert _catalog_column_type(*row) == expected


def _view_schema(view_type: str) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(json.dumps(SCHEMA))
    for o in schema["objects"]:
        if o["object_name"] == "V_DEPT_PROJECT":
            for c in o["columns"]:
                if c["column"] == "PROJECT_ID":
                    c["data_type"] = view_type
    return schema


def _view_and_project() -> list[Any]:
    return [
        obj("project", "プロジェクト", "PROJECT"),
        prop("project_project_id", "プロジェクトID", "project", "PROJECT", "PROJECT_ID"),
        obj("v_dept_project", "部門別プロジェクト", "V_DEPT_PROJECT"),
        prop(
            "v_dept_project_id", "プロジェクトID", "v_dept_project", "V_DEPT_PROJECT", "PROJECT_ID"
        ),
    ]


@pytest.mark.parametrize(
    ("view_type", "linked"),
    [("NUMBER", True), ("", True), ("VARCHAR2", False)],
    ids=["same-type", "unknown-type", "different-type"],
)
def test_view_id_column_links_to_table_regardless_of_ai_output(
    view_type: str, linked: bool
) -> None:
    """ビューの ID 列から表へのリンク型は AI の出力に関係なく補う。型が不明なら名前と主キーで。"""

    report = repair_definitions_report(
        "s", _view_and_project(), _view_schema(view_type), ai_output=True
    )
    links = [(d.source, d.target) for d in report.definitions if d.kind == "link_type"]
    assert (("v_dept_project", "project") in links) is linked
    view_prop = next(d for d in report.definitions if d.api_name == "v_dept_project_id")
    assert isinstance(view_prop, PropertyDefinition)
    assert view_prop.physical_type == view_type


# --- #1734 共有プロパティの同一性・前の定義 ------------------------------------------


def _department_ids(shared: str, **kw: Any) -> list[Any]:
    return [
        prop(
            "employee_department_id",
            "部門ID",
            "employee",
            "EMPLOYEE",
            "DEPARTMENT_ID",
            shared_property=shared,
            **kw,
        ),
        prop(
            "project_department_id",
            "部門ID",
            "project",
            "PROJECT",
            "DEPARTMENT_ID",
            shared_property=shared,
            **kw,
        ),
    ]


def test_renamed_shared_property_on_same_columns_keeps_previous_name() -> None:
    """AI が同じ列の組に別の名前を付けた共有プロパティは 1 つにし、前の名前を残す。"""

    previous = [
        obj("employee", "従業員", "EMPLOYEE"),
        obj("project", "プロジェクト", "PROJECT"),
        PropertyDefinition(
            kind="shared_property",
            api_name="shared_department_id",
            name_ja="部門ID",
            data_type="integer",
            origin="manual",
        ),
        *_department_ids("shared_department_id"),
    ]
    ai = [
        PropertyDefinition(
            kind="shared_property",
            api_name="department_id_shared",
            name_ja="部門ID（共有）",
            data_type="integer",
            mappings=[*column("EMPLOYEE", "DEPARTMENT_ID"), *column("PROJECT", "DEPARTMENT_ID")],
        ),
    ]
    merged, _ = merge_definitions("s", [*previous, *ai])
    report = repair_definitions_report(
        "s",
        merged,
        SCHEMA,
        ai_output=True,
        previous={d.api_name for d in previous},
    )
    shared = [d for d in report.definitions if d.kind == "shared_property"]
    assert [d.api_name for d in shared] == ["shared_department_id"]
    assert shared[0].name_ja == "部門ID"
    assert "部門ID（共有）" in shared[0].aliases
    assert report.remap["department_id_shared"] == "shared_department_id"


def test_unused_ai_shared_property_is_removed_but_user_one_is_kept() -> None:
    values = [
        obj("employee", "従業員", "EMPLOYEE"),
        obj("project", "プロジェクト", "PROJECT"),
        *_department_ids("shared_department_id"),
        PropertyDefinition(
            kind="shared_property",
            api_name="shared_department_id",
            name_ja="部門ID",
            description_ja="部門を表す ID",
        ),
        PropertyDefinition(
            kind="shared_property",
            api_name="department_name_shared",
            name_ja="部門名（共有）",
            description_ja="部門の名前",
        ),
        PropertyDefinition(
            kind="shared_property",
            api_name="user_note",
            name_ja="利用者の共有",
            description_ja="利用者が書いた共有プロパティ",
            origin="manual",
        ),
    ]
    report = repair_definitions_report("s", values, SCHEMA, ai_output=True)
    names = {d.api_name for d in report.definitions if d.kind == "shared_property"}
    assert names == {"shared_department_id", "user_note"}
    assert [d.api_name for d in report.unused_shared] == ["department_name_shared"]
    # 利用者が Markdown に書いた定義（ai_output でない）からは外さない。
    plain = repair_definitions_report("s", values, SCHEMA)
    assert "department_name_shared" in {d.api_name for d in plain.definitions}


def test_three_rebuilds_with_name_drift_do_not_grow_shared_properties() -> None:
    """再構築のたびに AI が名前を揺らしても、共有プロパティの件数が増えない。"""

    current: list[Any] = [
        obj("employee", "従業員", "EMPLOYEE"),
        obj("project", "プロジェクト", "PROJECT"),
    ]
    counts = []
    for suffix in ("a", "b", "c"):
        ai = [
            PropertyDefinition(
                kind="shared_property",
                api_name=f"department_id_{suffix}",
                name_ja=f"部門ID{suffix}",
                data_type="integer",
            ),
            *_department_ids(f"department_id_{suffix}"),
            PropertyDefinition(
                kind="shared_property",
                api_name=f"unused_{suffix}",
                name_ja=f"未使用{suffix}",
                description_ja="使われない共有プロパティ",
            ),
        ]
        previous = {d.api_name for d in current}
        merged, _ = merge_definitions("s", [*current, *[d.model_copy() for d in ai]])
        current = repair_definitions_report(
            "s", merged, SCHEMA, ai_output=True, previous=previous
        ).definitions
        counts.append(sum(d.kind == "shared_property" for d in current))
    assert counts == [1, 1, 1]
    shared = next(d for d in current if d.kind == "shared_property")
    assert shared.api_name == "department_id_a"


def test_same_column_property_keeps_previous_name() -> None:
    previous = prop("project_budget", "予算", "project", "PROJECT", "BUDGET", origin="manual")
    ai = prop("budget_amount", "予算額", "project", "PROJECT", "BUDGET")
    values = [obj("project", "プロジェクト", "PROJECT"), ai, previous]
    report = repair_definitions_report(
        "s", values, SCHEMA, ai_output=True, previous={"project_budget", "project"}
    )
    names = {d.api_name for d in report.definitions if d.kind == "property"}
    assert "project_budget" in names and "budget_amount" not in names


def test_prior_basis_is_draft_then_published_then_none() -> None:
    draft = [obj("draft_obj", "下書き", "PROJECT")]
    published = [obj("published_obj", "公開版", "PROJECT")]
    assert select_prior_definitions("## 下書き", draft, published) == ("draft", draft)
    # 下書きの定義が読めなくても、利用者が消した定義を公開版から戻さない。
    assert select_prior_definitions("自由な文だけ", [], published) == ("draft", [])
    assert select_prior_definitions("", [], published) == ("published", published)
    assert select_prior_definitions("  ", [], None) == ("none", [])
    assert summarize_prior_definitions("none", []) is None


def test_prior_summary_has_only_names_aliases_and_physical_mappings() -> None:
    values = [
        obj("project", "プロジェクト", "PROJECT", aliases=["案件"], description_ja="説明"),
        prop("project_budget", "予算", "project", "PROJECT", "BUDGET", description_ja="説明"),
    ]
    summary = summarize_prior_definitions("published", values)
    assert summary is not None
    assert summary["object_types"] == [
        {
            "api_name": "project",
            "name_ja": "プロジェクト",
            "aliases": ["案件"],
            "tables": ["ADMIN.PROJECT"],
        }
    ]
    assert summary["properties"][0]["columns"] == ["ADMIN.PROJECT.BUDGET"]
    assert "説明" not in json.dumps(summary, ensure_ascii=False)


def test_prior_findings_are_plain_and_without_codes() -> None:
    findings = plain_prior_findings(
        [
            {
                "severity": "warning",
                "code": "CARDINALITY_REQUIRED",
                "definition_id": "profile_concept_x",
                "message_ja": "多重度が分かりません。",
            },
            {
                "severity": "warning",
                "code": "CARDINALITY_REQUIRED",
                "message_ja": "多重度が分かりません。",
            },
            {"severity": "debug", "message_ja": "出さない"},
        ],
        {"profile_concept_x": "部門と従業員"},
    )
    assert findings == [
        "警告: 『部門と従業員』 多重度が分かりません。",
        "警告: 多重度が分かりません。",
    ]
    assert "CARDINALITY" not in "".join(findings)


def test_prior_context_is_trimmed_before_findings_within_budget() -> None:
    big = {
        "basis": "draft",
        "object_types": [],
        "properties": [
            {"api_name": f"p{i}", "name_ja": "名前" * 40, "aliases": ["別名" * 40], "columns": []}
            for i in range(2000)
        ],
        "link_types": [],
        "shared_properties": [],
    }
    prior = {"previous_ontology": big, "previous_check_findings": ["警告: 直すべき点"]}
    prompt, context = _with_prior_context("抽出してください。", json.dumps({"schema": 1}), prior)
    assert prompt.endswith(PRIOR_PROMPT)
    payload = json.loads(context)
    assert payload["previous_check_findings"] == ["警告: 直すべき点"]
    assert "properties" not in payload.get("previous_ontology", {})
    assert _llm_call_chars(prompt, context) <= _ONTOLOGY_BUILD_LLM_CONTEXT_MAX_CHARS
    # 前の定義が無ければ入力を変えない。
    assert _with_prior_context("p", "{}", None) == ("p", "{}")


# --- #1735 自由な行の解析 ---------------------------------------------------------------


def test_rule_parser_reads_fixed_form_and_returns_free_lines() -> None:
    markdown = render_concepts(model())
    rows = markdown.splitlines()
    rows.insert(3, "受注は顧客からの注文のことです。")
    rows.append("- 謎の項目: 何か")
    parsed = parse_markdown_rules("\n".join(rows))
    assert {d.api_name for d in parsed.definitions} == {"Order", "Order.id"}
    assert parsed.free_lines == [4, len(rows)]


class _FreeLineParser:
    """自由な行だけを受け取り、決まった形の定義を書き換える応答と、新しい定義を返すスタブ。"""

    def __init__(self, free_line: int) -> None:
        self.calls: list[dict[str, Any]] = []
        self.free_line = free_line
        self.fail = False

    def is_configured(self) -> bool:
        return True

    def generate(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("unavailable")
        changed = model()[1].model_copy(update={"name_ja": "書き換えた名前", "required": False})
        amount = PropertyDefinition(
            api_name="Order.amount",
            name_ja="受注金額",
            object_type="Order",
            data_type="number",
            mappings=[DefinitionMapping(owner="APP", object_name="ORDERS", column_name="AMOUNT")],
        )
        return json.dumps(
            {
                "definitions": [d.model_dump(mode="json") for d in (changed, amount)],
                "coverage": [],
                "lines": [
                    {
                        "start_line": self.free_line,
                        "end_line": self.free_line,
                        "disposition": "definition",
                        "definition_api_names": ["Order.id", "Order.amount"],
                    }
                ],
                "issues_ja": [],
            }
        )


def _free_line_workspace(
    monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> tuple[Any, MarkdownOntologyWorkspace, _FreeLineParser, str]:
    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_worker_mode", "external")
    rt, legacy = runtime()
    base = render_concepts(model()).rstrip("\n").splitlines()
    markdown = "\n".join([*base, "", *extra]) + "\n"
    rt.create_build_markdown_draft(
        profile_id="sales",
        base_revision_id=rt.profile_view("sales")[1].revision.id,
        payloads=[],
        titles=[],
        markdown=markdown,
        note="自由な行",
    )
    parser = _FreeLineParser(len(base) + 2)
    legacy._enterprise_ai_client = parser
    return rt, MarkdownOntologyWorkspace(rt), parser, markdown


def _prepare(rt: Any, svc: MarkdownOntologyWorkspace, key: str) -> dict[str, Any]:
    value = svc.prepare("sales", rt.ontology_markdown_state("sales").draft_etag, key, None)
    svc.run_preparation("sales", value["id"])
    return svc.preparation("sales", value["id"])


@pytest.mark.rule_parser
@pytest.mark.parse_cache
def test_free_lines_go_to_llm_alone_and_rule_definitions_do_not_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, svc, parser, markdown = _free_line_workspace(
        monkeypatch, ["受注金額は税込みの金額です。", "", "部門は会社の組織です。"]
    )
    results = [_prepare(rt, svc, f"run-{i}") for i in range(3)]
    # LLM には自由な行だけを渡し、同じ Markdown の結果は使い回す（3 回で 1 回だけ）。
    assert len(parser.calls) == 1
    sent = json.loads(parser.calls[0]["context"])["markdown_with_line_numbers"]
    assert "##### " not in sent and "受注金額は税込みの金額です。" in sent
    first = results[0]
    assert first["status"] == "ready", first["findings"]
    assert first["parser"] == "rule+llm"
    by_name = {d["api_name"]: d for d in first["definitions"]}
    # 決定論で読めた定義は LLM の結果で変わらない（足すだけ）。
    assert by_name["Order.id"]["name_ja"] == "受注番号"
    assert "書き換えた名前" not in json.dumps(by_name["Order.id"], ensure_ascii=False)
    assert "Order.amount" in by_name
    # 定義にならなかった自由な行は業務メモとして残し、公開を止めない。
    notes = [f for f in first["findings"] if f.get("code") == "MARKDOWN_BUSINESS_NOTE"]
    assert len(notes) == 1 and notes[0]["severity"] == "info"
    assert "業務メモ" in notes[0]["message_ja"]
    assert [n["text"] for n in first["notes"]] == ["部門は会社の組織です。"]

    def counts(result: dict[str, Any]) -> tuple[int, int, int]:
        found = [f["severity"] for f in result["findings"]]
        return found.count("error"), found.count("warning"), found.count("info")

    assert counts(results[0]) == counts(results[1]) == counts(results[2])
    assert results[0]["definitions"] == results[1]["definitions"] == results[2]["definitions"]
    assert markdown  # Markdown 自体は書き換えない


@pytest.mark.rule_parser
def test_llm_failure_keeps_rule_definitions_and_publishable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rt, svc, parser, _ = _free_line_workspace(monkeypatch, ["自由な説明の行です。"])
    parser.fail = True
    result = _prepare(rt, svc, "fail")
    assert result["status"] == "ready", result["findings"]
    assert {d["api_name"] for d in result["definitions"]} == {"Order", "Order.id"}
    assert any(f.get("code") == "MARKDOWN_FREE_LINES_UNREAD" for f in result["findings"])
    assert any(f.get("code") == "MARKDOWN_BUSINESS_NOTE" for f in result["findings"])


@pytest.mark.rule_parser
def test_fixed_form_markdown_does_not_call_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    rt, svc, parser, _ = _free_line_workspace(monkeypatch, [])
    result = _prepare(rt, svc, "rule")
    assert result["status"] == "ready", result["findings"]
    assert result["parser"] == "rule" and parser.calls == []
    assert not result["notes"]


@pytest.mark.rule_parser
def test_last_check_and_prior_context_follow_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    """前の確認の直すべき点が残り、AI 構築の入力の基準は下書きになる（#1734）。"""

    rt, svc, _, _ = _free_line_workspace(monkeypatch, [])
    _prepare(rt, svc, "check")
    prior = svc.prior_build_context("sales")
    assert prior is not None
    assert prior["previous_ontology"]["basis"] == "draft"
    assert {o["api_name"] for o in prior["previous_ontology"]["object_types"]} == {"Order"}
    assert isinstance(prior["previous_check_findings"], list)


def test_build_marks_ai_output_as_ai_and_drops_its_unused_shared_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM が origin を manual と書いても AI の出力は AI のもの。使われない共有プロパティは外す。"""

    from test_nl2sql_ontology_build import _FakeEnterpriseAiClient, _wait_for_job

    from app.features.nl2sql.ontology_build import OntologyBuildService
    from app.features.nl2sql.ontology_definition_service import ProfileOntologyDefinitionService

    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    rt, legacy = runtime()
    payload = [d.model_dump(mode="json") for d in model()]
    payload.append(
        {
            "kind": "shared_property",
            "api_name": "OrderNumberShared",
            "name_ja": "受注番号（共有）",
            "description_ja": "どのプロパティからも使われない",
            "origin": "manual",
        }
    )
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(json.dumps({"definitions": payload}))
    service = OntologyBuildService(rt)
    job = _wait_for_job(service, service.start("sales").id)
    assert job.status == "succeeded", job.error_message_ja
    bundle = ProfileOntologyDefinitionService(rt).get("sales", job.result_bundle_id)
    assert all(d.origin == "ai" for d in bundle.definitions)
    assert "OrderNumberShared" not in {d.api_name for d in bundle.definitions}


def test_kind_name_owner_references_are_repaired_by_physical_tables() -> None:
    """AI が所属・両端に種類の名前（`object_type`）を書いても、列・結合のキーの表で直す。"""

    from app.features.nl2sql.ontology_definitions import LinkTypeDefinition

    values = [
        obj("department", "部門", "DEPARTMENT"),
        obj("employee", "従業員", "EMPLOYEE"),
        prop("department_created_at", "作成日", "object_type", "DEPARTMENT", "CREATED_AT"),
        LinkTypeDefinition(
            api_name="employee_department_link",
            name_ja="所属部門",
            source="employee",
            target="object_type",
            cardinality="many_to_one",
            join_expression_sql="ADMIN.EMPLOYEE.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID",
        ),
    ]
    report = repair_definitions_report("s", values, SCHEMA, ai_output=True)
    by_name = {d.api_name: d for d in report.definitions}
    created = by_name["department_created_at"]
    assert isinstance(created, PropertyDefinition) and created.object_type == "department"
    link = by_name["employee_department_link"]
    assert isinstance(link, LinkTypeDefinition)
    assert (link.source, link.target) == ("employee", "department")


def test_ai_link_without_join_key_gets_join_from_view_id_column() -> None:
    """AI が結合のキーの無いビュー → 表のリンク型を作っても、ID 列の結合のキーで補う。"""

    from app.features.nl2sql.ontology_definitions import LinkTypeDefinition

    values = [
        *_view_and_project(),
        LinkTypeDefinition(
            api_name="v_dept_project_to_project",
            name_ja="プロジェクト",
            source="project",
            target="v_dept_project",
        ),
    ]
    report = repair_definitions_report("s", values, _view_schema("NUMBER"), ai_output=True)
    links = [d for d in report.definitions if isinstance(d, LinkTypeDefinition)]
    assert len(links) == 1
    assert links[0].join_expression_sql == (
        "ADMIN.V_DEPT_PROJECT.PROJECT_ID = ADMIN.PROJECT.PROJECT_ID"
    )
    assert links[0].cardinality == "one_to_many"
