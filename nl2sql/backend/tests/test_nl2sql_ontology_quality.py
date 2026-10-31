"""#1736: オントロジーの AI 構築の品質（列の無いプロパティ・結合のキーの無いリンク型・説明の連結・
更新可能の食い違い・DB を使わない経路のカタログ）と、#1737 の残課題（ビュー → 表の多重度・二分割の
子タスクの前の定義）。

実 OCI / Oracle は使わない（スタブと sql_assist_sample を模した schema context）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_nl2sql_ontology_unify_links import SCHEMA, column, obj, prop

from app.features.nl2sql.incremental_store import OracleIncrementalNl2SqlRepository
from app.features.nl2sql.models import (
    SampleDataMutationRequest,
    SampleDataset,
    SampleDataStep,
    SchemaCatalog,
)
from app.features.nl2sql.ontology_build import _OntologyBuildLlmTask, _with_prior_context
from app.features.nl2sql.ontology_build_prior import PRIOR_PROMPT
from app.features.nl2sql.ontology_definitions import (
    ActionAssignment,
    ActionTypeDefinition,
    LinkTypeDefinition,
    PropertyDefinition,
)
from app.features.nl2sql.ontology_models import OntologyBuildStepName
from app.features.nl2sql.ontology_unified_model import (
    merge_definitions,
    repair_definitions_report,
    schema_resolved_conflicts,
)
from app.features.nl2sql.service import Nl2SqlService
from app.features.nl2sql.store import MemoryNl2SqlStore

SAMPLE_DATA_CONFIRMATION = "ADMIN_EXECUTE"


def _schema(**changes: Any) -> dict[str, Any]:
    schema: dict[str, Any] = json.loads(json.dumps(SCHEMA))
    schema.update(changes)
    return schema


def _view_dependencies() -> list[dict[str, Any]]:
    return [
        {
            "id": f"view_dependency:ADMIN.{view}:ADMIN.{table}",
            "kind": "view_dependency",
            "source_object": f"ADMIN.{view}",
            "target_object": f"ADMIN.{table}",
            "cardinality": "unknown",
            "join_conditions": [],
        }
        for view, table in (
            ("V_DEPT_PROJECT", "PROJECT"),
            ("V_DEPT_PROJECT", "DEPARTMENT"),
            ("V_EMP_DEPT", "EMPLOYEE"),
            ("V_EMP_DEPT", "DEPARTMENT"),
        )
    ]


def _ai(*values: Any) -> list[Any]:
    return [v.model_copy(update={"origin": "ai"}) for v in values]


def _by_name(values: list[Any]) -> dict[str, Any]:
    return {d.api_name: d for d in values}


# --- 1. 列の対応の無いプロパティ ---------------------------------------------------------


def _department(*extra: Any) -> list[Any]:
    return _ai(
        obj("department", "部門", "DEPARTMENT", ["department_id", "department_name"]),
        prop("department_name", "部門名", "department", "DEPARTMENT", "DEPARTMENT_NAME"),
        *extra,
    )


def test_unmapped_ai_property_borrows_the_column_of_its_shared_property() -> None:
    """AI が列を共有プロパティにだけ書いたプロパティは、所属の表の列で補う（公開を止めない）。"""

    values = _department(
        PropertyDefinition(
            api_name="department_id",
            name_ja="部門ID",
            object_type="department",
            shared_property="department_id_shared_property",
        ),
        PropertyDefinition(
            kind="shared_property",
            api_name="department_id_shared_property",
            name_ja="部門ID",
            mappings=[*column("DEPARTMENT", "DEPARTMENT_ID"), *column("EMPLOYEE", "DEPARTMENT_ID")],
        ),
    )
    result = repair_definitions_report("p", values, _schema(), ai_output=True)
    names = _by_name(result.definitions)
    assert [(m.object_name, m.column_name) for m in names["department_id"].mappings] == [
        ("DEPARTMENT", "DEPARTMENT_ID")
    ]
    assert names["department_id"].physical_type == "NUMBER"
    assert result.unmapped_properties == []


def test_unmapped_ai_property_uses_the_column_of_the_same_name() -> None:
    """共有プロパティも無ければ、所属の表の API 名と同じ列（DB にある列）で補う。"""

    values = _department(
        PropertyDefinition(
            api_name="department_location", name_ja="所在地", object_type="department"
        )
    )
    names = _by_name(repair_definitions_report("p", values, _schema(), ai_output=True).definitions)
    assert [m.column_name for m in names["department_location"].mappings] == ["LOCATION"]


def test_unmapped_ai_property_is_dropped_with_its_references() -> None:
    """列が決まらない AI のプロパティは下書きから外し、オブジェクト型の参照も外す。"""

    values = _department(
        PropertyDefinition(api_name="department_id", name_ja="部門ID", object_type="department"),
        PropertyDefinition(api_name="headcount", name_ja="人数", object_type="department"),
    )
    values[0].properties.append("headcount")
    values[0].title_property = "headcount"
    result = repair_definitions_report("p", values, _schema(), ai_output=True)
    names = _by_name(result.definitions)
    assert "headcount" not in names
    assert [d.api_name for d in result.unmapped_properties] == ["headcount"]
    assert "headcount" not in names["department"].properties
    assert names["department"].title_property == ""
    # API 名と同じ列のあるものは補われて残る。
    assert [m.column_name for m in names["department_id"].mappings] == ["DEPARTMENT_ID"]


@pytest.mark.parametrize("origin", ["manual", "previous"], ids=["user", "previous-ai"])
def test_user_and_previous_unmapped_properties_are_kept(origin: str) -> None:
    """利用者のプロパティと前の定義のプロパティは外さない（検証の診断で直してもらう）。"""

    headcount = PropertyDefinition(api_name="headcount", name_ja="人数", object_type="department")
    if origin == "manual":
        values = [*_department(), headcount.model_copy(update={"origin": "manual"})]
        previous: tuple[str, ...] = ()
    else:
        values = _department(headcount)
        previous = ("headcount",)
    result = repair_definitions_report("p", values, _schema(), ai_output=True, previous=previous)
    assert "headcount" in _by_name(result.definitions)
    assert result.unmapped_properties == []


def test_markdown_repair_does_not_drop_unmapped_properties() -> None:
    """公開前の確認（利用者の Markdown）では外さない。"""

    values = _department(
        PropertyDefinition(api_name="headcount", name_ja="人数", object_type="department")
    )
    result = repair_definitions_report("p", values, _schema())
    assert "headcount" in _by_name(result.definitions)


# --- 1. 更新可能（Writable）の食い違い ------------------------------------------------------


def _writable_extractions(*extra: Any) -> list[Any]:
    first = PropertyDefinition(
        api_name="budget",
        name_ja="予算",
        object_type="project",
        mappings=column("PROJECT", "BUDGET"),
        writable=False,
        origin="ai",
    )
    second = first.model_copy(update={"writable": True})
    return [obj("project", "プロジェクト", "PROJECT"), first, second, *extra]


def test_ai_writable_conflict_is_not_reported_and_not_guessed() -> None:
    """抽出の段の間の更新可能の食い違いは警告にせず、アクション型の根拠が無ければ更新不可にする。"""

    merged, conflicts = merge_definitions("p", _writable_extractions())
    assert conflicts == []
    assert _by_name(merged)["budget"].writable is False


def test_ai_writable_conflict_follows_action_type() -> None:
    action = ActionTypeDefinition(
        api_name="update_budget",
        name_ja="予算を変更する",
        object_type="project",
        assignments=[ActionAssignment(property="budget", parameter="new_budget")],
        origin="ai",
    )
    merged, conflicts = merge_definitions("p", _writable_extractions(action))
    assert conflicts == []
    assert _by_name(merged)["budget"].writable is True


def test_previous_writable_wins_over_ai_and_user_conflicts_are_reported() -> None:
    previous = PropertyDefinition(
        api_name="budget",
        name_ja="予算",
        object_type="project",
        mappings=column("PROJECT", "BUDGET"),
        writable=True,
        origin="manual",
    )
    ai = previous.model_copy(update={"writable": False, "origin": "ai"})
    merged, conflicts = merge_definitions("p", [previous, ai], previous={"budget"})
    assert conflicts == []
    assert _by_name(merged)["budget"].writable is True
    # 利用者の Markdown どうしの食い違いは今どおり警告にする。
    manual = previous.model_copy(update={"writable": False})
    _merged, conflicts = merge_definitions("p", [previous, manual])
    assert len(conflicts) == 1


# --- 3. 説明の連結 ----------------------------------------------------------------------


def test_rebuild_keeps_previous_description_and_does_not_concatenate() -> None:
    """再構築を 3 回重ねても、前の定義の説明を正にして長くしない。"""

    description = "部門・部署を表すテーブル。1 行が 1 つの部門。"
    current = [obj("department", "部門", "DEPARTMENT", description_ja=description)]
    for wording in ("SQL Assist sample table", "部門の情報。", description):
        merged, _ = merge_definitions(
            "p",
            [
                *current,
                obj("department", "部門", "DEPARTMENT", description_ja=wording, origin="ai"),
            ],
            previous={"department"},
        )
        current = merged
    assert _by_name(current)["department"].description_ja == description


def test_same_description_sentences_are_not_repeated_in_one_build() -> None:
    sentence = "部門・部署を表すテーブル。1 行が 1 つの部門。"
    merged, _ = merge_definitions(
        "p",
        [
            obj("department", "部門", "DEPARTMENT", description_ja="SQL Assist sample table"),
            obj("department", "部門", "DEPARTMENT", description_ja=sentence, origin="ai"),
            obj("department", "部門", "DEPARTMENT", description_ja=f" {sentence}", origin="ai"),
            obj(
                "department",
                "部門",
                "DEPARTMENT",
                description_ja=f"SQL Assist sample table / {sentence}",
                origin="ai",
            ),
        ],
    )
    assert _by_name(merged)["department"].description_ja == (f"SQL Assist sample table\n{sentence}")


def test_physical_name_description_is_replaced_by_business_description() -> None:
    """表名だけの説明（`V_DEPT_PROJECT`）は、前の定義でも業務の説明に置き換える。"""

    merged, _ = merge_definitions(
        "p",
        [
            obj(
                "v_dept_project",
                "部門別プロジェクト",
                "V_DEPT_PROJECT",
                description_ja="V_DEPT_PROJECT",
            ),
            obj(
                "v_dept_project",
                "部門別プロジェクト",
                "V_DEPT_PROJECT",
                description_ja="1 行が 1 つのプロジェクトと所属の部門名。",
                origin="ai",
            ),
        ],
        previous={"v_dept_project"},
    )
    assert _by_name(merged)["v_dept_project"].description_ja == (
        "1 行が 1 つのプロジェクトと所属の部門名。"
    )


# --- 2. 結合のキーの無いリンク型 ----------------------------------------------------------


def _view_objects() -> list[Any]:
    return _ai(
        obj("department", "部門", "DEPARTMENT", ["department_id"], primary_key=["department_id"]),
        prop("department_id", "部門ID", "department", "DEPARTMENT", "DEPARTMENT_ID"),
        obj("project", "プロジェクト", "PROJECT", ["project_id"], primary_key=["project_id"]),
        prop("project_id", "プロジェクトID", "project", "PROJECT", "PROJECT_ID"),
        obj(
            "v_dept_project",
            "部門別プロジェクト",
            "V_DEPT_PROJECT",
            ["v_dept_project_project_id"],
            primary_key=["v_dept_project_project_id"],
        ),
        prop(
            "v_dept_project_project_id",
            "プロジェクトID",
            "v_dept_project",
            "V_DEPT_PROJECT",
            "PROJECT_ID",
        ),
    )


def _keyless(api: str, source: str, target: str, **kw: Any) -> LinkTypeDefinition:
    return LinkTypeDefinition(
        api_name=api, name_ja=api, source=source, target=target, origin="ai", **kw
    )


def test_view_dependency_link_without_join_key_is_dropped() -> None:
    """ビュー → 元の表の依存だけで結合のキーが無い AI のリンク型は外す（リンク型にならない）。"""

    values = [
        *_view_objects(),
        _keyless(
            "v_dept_project_dep_dept",
            "v_dept_project",
            "department",
            missing_information_ja=["join_expression_sql が不明"],
        ),
        _keyless(
            "v_dept_project_dep_proj",
            "v_dept_project",
            "project",
            missing_information_ja=["join_expression_sql が不明（ビューの内部結合ロジック）"],
        ),
    ]
    result = repair_definitions_report(
        "p", values, _schema(relationships=_view_dependencies()), ai_output=True
    )
    names = _by_name(result.definitions)
    assert "v_dept_project_dep_dept" not in names
    assert [d.api_name for d in result.keyless_links] == ["v_dept_project_dep_dept"]
    # 結合のキーを補えたものは残し、結合のキーの不足情報は外す。主キーが同じ列なので 1 対 1。
    kept = names["v_dept_project_dep_proj"]
    assert kept.join_expression_sql == "ADMIN.V_DEPT_PROJECT.PROJECT_ID = ADMIN.PROJECT.PROJECT_ID"
    assert kept.missing_information_ja == []
    assert kept.cardinality == "one_to_one"
    # ビューの列は NULL 可（制約が無い）。
    assert kept.optional is True


def test_keyless_links_with_the_same_ends_become_one() -> None:
    """同じ両端の結合のキーの無いリンク型は 1 つにする（残すのは前の定義の名前）。"""

    values = [
        *_view_objects(),
        _keyless("project_department", "project", "department"),
        _keyless("belongs_to", "department", "project"),
    ]
    result = repair_definitions_report(
        "p", values, _schema(), ai_output=True, previous={"belongs_to"}
    )
    links = [d for d in result.definitions if d.kind == "link_type"]
    # 外部キーの無い schema でも、同じ名前の ID 列で結合のキーを補い、1 つにまとめる。
    assert len([d for d in links if {d.source, d.target} == {"project", "department"}]) == 1
    assert result.remap.get("project_department") == "belongs_to"


def test_keyless_ai_link_joins_the_keyed_link_with_the_same_ends() -> None:
    schema = _schema(
        relationships=[
            {
                "id": "FK_PROJ_DEPT",
                "kind": "foreign_key",
                "source_object": "ADMIN.PROJECT",
                "target_object": "ADMIN.DEPARTMENT",
                "cardinality": "many_to_one",
                "join_conditions": [
                    {
                        "left": "ADMIN.PROJECT.DEPARTMENT_ID",
                        "right": "ADMIN.DEPARTMENT.DEPARTMENT_ID",
                    }
                ],
            }
        ]
    )
    keyed = LinkTypeDefinition(
        api_name="proj_dept_fk",
        name_ja="所属部門",
        source="project",
        target="department",
        cardinality="many_to_one",
        join_expression_sql="ADMIN.PROJECT.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID",
        origin="ai",
    )
    values = [
        *_view_objects(),
        prop("project_department_id", "部門ID", "project", "PROJECT", "DEPARTMENT_ID", origin="ai"),
        keyed,
        keyed.model_copy(
            update={"api_name": "dept_project", "name_ja": "担当", "join_expression_sql": ""}
        ),
    ]
    result = repair_definitions_report("p", values, schema, ai_output=True)
    links = [
        d
        for d in result.definitions
        if d.kind == "link_type" and {d.source, d.target} == {"project", "department"}
    ]
    assert [d.api_name for d in links] == ["proj_dept_fk"]
    assert "担当" in links[0].aliases
    # 起点の部門 ID は NOT NULL なので任意の関係ではない。
    assert links[0].optional is False


def test_user_keyless_links_are_kept() -> None:
    values = [
        *_view_objects(),
        LinkTypeDefinition(
            api_name="v_dept_project_dep_dept",
            name_ja="部門",
            source="v_dept_project",
            target="department",
            origin="manual",
        ),
    ]
    result = repair_definitions_report(
        "p", values, _schema(relationships=_view_dependencies()), ai_output=True
    )
    assert "v_dept_project_dep_dept" in _by_name(result.definitions)
    assert result.keyless_links == []


def test_view_link_is_many_to_one_without_unique_key() -> None:
    """ビューの列が主キー・一意キーでなければ多対 1。"""

    values = _view_objects()
    values[4] = values[4].model_copy(update={"primary_key": []})
    result = repair_definitions_report("p", values, _schema(), ai_output=True)
    link = next(
        d for d in result.definitions if d.kind == "link_type" and d.source == "v_dept_project"
    )
    assert link.cardinality == "many_to_one"


def test_link_optional_conflict_is_resolved_by_the_database() -> None:
    """任意の関係の食い違いは、起点の結合のキーの列の NULL 可（DB）で決まるため警告にしない。"""

    keyed = LinkTypeDefinition(
        api_name="proj_dept",
        name_ja="所属部門",
        source="project",
        target="department",
        join_expression_sql="ADMIN.PROJECT.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID",
        optional=True,
        origin="ai",
    )
    values = [*_view_objects(), keyed, keyed.model_copy(update={"optional": False})]
    merged, conflicts = merge_definitions("p", values)
    assert len(conflicts) == 1
    repair = repair_definitions_report("p", merged, _schema(), ai_output=True)
    assert schema_resolved_conflicts(conflicts, repair.definitions, _schema(), repair.remap) == []
    assert _by_name(repair.definitions)["proj_dept"].optional is False


# --- 4. DB を使わない経路のカタログ -----------------------------------------------------


class _RecordingOracleRepository(OracleIncrementalNl2SqlRepository):
    """Oracle の永続化先の代わり（接続しない）。カタログの書き込みを記録する。"""

    def __init__(self) -> None:  # 接続の factory を持たない
        self.writes: list[Any] = []

    def apply_schema_refresh(self, **kwargs: Any) -> Any:
        self.writes.append(kwargs)
        raise AssertionError("Oracle のカタログへ書いてはいけない")

    def schema_manifest(self) -> Any:
        raise AssertionError("Oracle のカタログを読んではいけない")


def _deterministic_service_with_oracle_persistence() -> tuple[Nl2SqlService, Any]:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = SchemaCatalog(
        current_owner="APP", tables=[], refreshed_at="2026-10-14T00:00:00Z"
    )
    repository = _RecordingOracleRepository()
    service._incremental_repository = repository
    return service, repository


@pytest.mark.parametrize("operation", ["import", "delete"])
def test_deterministic_sample_data_does_not_write_the_oracle_catalog(operation: str) -> None:
    """DB を使わない実行環境のサンプルデータの取り込み・削除は、Oracle のカタログを上書きしない。"""

    service, repository = _deterministic_service_with_oracle_persistence()
    request = SampleDataMutationRequest(
        dataset=SampleDataset.HR, confirmation=SAMPLE_DATA_CONFIRMATION
    )
    run = service.import_sample_data if operation == "import" else service.delete_sample_data
    result = run(request)
    assert result.runtime == "deterministic"
    assert result.executed is False
    assert {s.status for s in result.statements} == {"blocked"}
    assert any("NL2SQL_RUNTIME_MODE=oracle" in w for w in result.warnings)
    assert repository.writes == []
    assert service._catalog.tables == []


def test_persist_local_catalog_never_writes_oracle_in_deterministic_runtime() -> None:
    service, repository = _deterministic_service_with_oracle_persistence()
    service._apply_sample_import_to_catalog(SampleDataStep.ALL)
    service._persist_local_catalog()
    assert repository.writes == []


# --- 二分割の子タスクの前の定義（#1737 の残課題） -----------------------------------------


def test_split_child_tasks_keep_the_prior_context() -> None:
    """失敗して二分割した子タスクにも、前の定義の要約と直すべき点を付け直す。"""

    from app.features.nl2sql.ontology_build import _BuildTextUnit, _dump_text_context
    from app.features.nl2sql.ontology_models import OntologyEvidenceLocatorKind

    schema_payload: dict[str, Any] = {"objects": [], "relationships": []}
    units = [
        _BuildTextUnit(
            source_document=None,
            source_label="業務の説明",
            locator_kind=next(iter(OntologyEvidenceLocatorKind)),
            locator=str(i),
            text=f"業務の説明 {i}",
        )
        for i in range(4)
    ]
    prior = {
        "previous_ontology": {"basis": "draft", "object_types": [{"api_name": "department"}]},
        "previous_check_findings": ["警告: 『部門』 主キーを書いてください。"],
    }
    task = _OntologyBuildLlmTask(
        name=OntologyBuildStepName.TEXT_EXTRACTION,
        prompt="資料から抽出してください。",
        context=_dump_text_context(schema_payload, units),
        progress_ja="",
        text_batch=units,
        schema_payload=schema_payload,
    ).with_prior(prior)
    assert task.prompt.endswith(PRIOR_PROMPT)
    children = task.split()
    assert children is not None and len(children) == 2
    for child in children:
        assert child.prompt.count(PRIOR_PROMPT) == 1
        payload = json.loads(child.context)
        assert payload["previous_ontology"]["basis"] == "draft"
        assert payload["previous_check_findings"] == prior["previous_check_findings"]
        assert len(payload["business_text_chunks"]) == 2
    # 前の定義が無ければ今どおり。
    plain = _with_prior_context("p", "{}", None)
    assert plain == ("p", "{}")
