"""#1765: 再構築のリンク型の多重度（Cardinality）の競合と、使われない移行の経路の削除。

実 OCI / Oracle は使わない（sql_assist_sample を模した schema context）。
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest
from test_nl2sql_ontology_quality import _by_name, _schema, _view_objects
from test_nl2sql_ontology_unify_links import obj, prop

from app.features.nl2sql import store as store_module
from app.features.nl2sql.ontology_definitions import LinkTypeDefinition
from app.features.nl2sql.ontology_improvement_suggestion import (
    business_text_cardinalities,
    improvement_suggestions,
)
from app.features.nl2sql.ontology_link_keys import link_cardinality
from app.features.nl2sql.ontology_unified_model import (
    ConflictNote,
    merge_definitions,
    settle_link_cardinalities,
)
from app.settings import Settings

_EMP_DEPT = "ADMIN.EMPLOYEE.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID"
_VIEW_PROJECT = "ADMIN.V_DEPT_PROJECT.PROJECT_ID = ADMIN.PROJECT.PROJECT_ID"
_VIEW_DEPT_NAME = "ADMIN.V_EMP_DEPT.DEPARTMENT_NAME = ADMIN.DEPARTMENT.DEPARTMENT_NAME"


def _objects() -> list[Any]:
    return [
        *_view_objects(),
        obj("employee", "従業員", "EMPLOYEE", ["employee_id"], primary_key=["employee_id"]),
        prop("employee_id", "従業員ID", "employee", "EMPLOYEE", "EMPLOYEE_ID"),
        obj("v_emp_dept", "従業員部門ビュー", "V_EMP_DEPT"),
    ]


def _link(
    api: str, source: str, target: str, join: str, cardinality: str = "unknown", **kw: Any
) -> LinkTypeDefinition:
    values: dict[str, Any] = {
        "api_name": api,
        "id": f"id-{api}",
        "name_ja": f"{api} のリンク",
        "source": source,
        "target": target,
        "join_expression_sql": join,
        "cardinality": cardinality,
        "origin": "ai",
        **kw,
    }
    return LinkTypeDefinition(**values)


def _cardinality_conflicts(conflicts: list[str]) -> list[str]:
    return [c for c in conflicts if isinstance(c, ConflictNote) and c.field == "cardinality"]


# --- 結合のキーと両端のキーから決まる多重度 ----------------------------------------------


@pytest.mark.parametrize(
    ("source", "target", "join", "expected"),
    [
        # 終点の主キー（制約）でつなぐ: 多対 1。
        ("employee", "department", _EMP_DEPT, "many_to_one"),
        # 向きが逆なら 1 対多。
        ("department", "employee", _EMP_DEPT, "one_to_many"),
        # ビューの主キー（オブジェクト型）と終点の主キー: 1 対 1。
        ("v_dept_project", "project", _VIEW_PROJECT, "one_to_one"),
        # 一意でない列でつなぐ・式が単純でないものは決めない。
        ("v_emp_dept", "department", _VIEW_DEPT_NAME, None),
        (
            "employee",
            "department",
            "UPPER(ADMIN.EMPLOYEE.DEPARTMENT_ID) = ADMIN.DEPARTMENT.DEPARTMENT_ID",
            None,
        ),
    ],
    ids=["many-to-one", "one-to-many", "view-one-to-one", "non-unique", "expression"],
)
def test_link_cardinality_comes_from_join_key_and_end_keys(
    source: str, target: str, join: str, expected: str | None
) -> None:
    values = [*_objects(), _link("l", source, target, join)]
    by_name = _by_name(values)
    assert link_cardinality(by_name["l"], by_name, _schema()) == expected


# --- 統合と構築の正の順 -------------------------------------------------------------------


def test_previous_cardinality_wins_and_ai_variation_is_not_a_conflict() -> None:
    """前の定義の多重度が正。AI の出力の揺れは警告にしない（#1765 の実 LLM の再構築）。"""

    previous = _link("v_dept_project_project", "v_dept_project", "project", _VIEW_PROJECT)
    previous = previous.model_copy(update={"cardinality": "one_to_one", "origin": "manual"})
    ai = _link("v_dept_project_project", "v_dept_project", "project", _VIEW_PROJECT, "many_to_one")
    merged, conflicts = merge_definitions(
        "p", [*_objects(), previous, ai], previous={"v_dept_project_project"}
    )
    assert _cardinality_conflicts(conflicts) == []
    assert _by_name(merged)["v_dept_project_project"].cardinality == "one_to_one"


def test_ai_cardinality_conflict_is_settled_by_the_database_keys() -> None:
    links = [
        _link("emp_dept", "employee", "department", _EMP_DEPT, "one_to_one"),
        _link("emp_dept", "employee", "department", _EMP_DEPT, "many_to_one"),
    ]
    merged, conflicts = merge_definitions("p", [*_objects(), *links])
    [note] = _cardinality_conflicts(conflicts)
    # 平易な 2 文（どちらかと、業務説明への書き方）。ID・項目名は出さない。
    assert note.count("。") == 2
    assert "1 対 1（one-to-one） と 多対 1（many-to-one）" in note
    assert "業務説明に正しい多重度を書いて" in note
    assert "cardinality" not in note and "id-emp_dept" not in note
    kept = settle_link_cardinalities(conflicts, merged, _schema())
    assert _cardinality_conflicts(kept) == []
    # DB の制約（終点の主キー）で決まる多重度にする。
    assert _by_name(merged)["emp_dept"].cardinality == "many_to_one"


def test_ai_cardinality_without_a_conflict_also_follows_the_database_keys() -> None:
    """AI の出力が 1 つでも、結合のキーで決まる多重度にそろえる（回ごとの揺れをなくす）。"""

    values = [*_objects(), _link("v", "v_dept_project", "project", _VIEW_PROJECT, "many_to_one")]
    assert settle_link_cardinalities([], values, _schema()) == []
    assert _by_name(values)["v"].cardinality == "one_to_one"


def test_previous_link_cardinality_is_never_rewritten() -> None:
    """前の定義（利用者が書いた・公開した）は、DB と違っても確認なしに書き換えない。"""

    link = _link("emp_dept", "employee", "department", _EMP_DEPT, "one_to_one", origin="manual")
    values = [*_objects(), link]
    settle_link_cardinalities([], values, _schema(), previous={"emp_dept"})
    assert link.cardinality == "one_to_one"


def test_business_text_decides_before_the_database_keys() -> None:
    links = [
        _link("emp_dept", "employee", "department", _EMP_DEPT, "one_to_one"),
        _link("emp_dept", "employee", "department", _EMP_DEPT, "one_to_many"),
    ]
    merged, conflicts = merge_definitions("p", [*_objects(), *links])
    text = (
        "従業員（employee）と部門（department）のリンク型『所属』（emp_dept）は、"
        f"{_EMP_DEPT} でつなぐ（多対 1）。"
    )
    decided = business_text_cardinalities(merged, text)
    assert decided == {"emp_dept": "many_to_one"}
    kept = settle_link_cardinalities(
        conflicts, merged, _schema(), decided={"emp_dept": "one_to_one"}
    )
    assert _cardinality_conflicts(kept) == []
    assert _by_name(merged)["emp_dept"].cardinality == "one_to_one"


def test_business_text_object_sentence_also_states_the_cardinality() -> None:
    values = [
        *_objects(),
        _link("emp_dept", "employee", "department", _EMP_DEPT, "one_to_one"),
    ]
    text = (
        "従業員（employee）は ADMIN.EMPLOYEE の表で、1 行が 1 つの従業員、主キーは EMPLOYEE_ID、"
        "DEPARTMENT_ID で部門に属する（多対 1（many-to-one））。"
    )
    assert business_text_cardinalities(values, text) == {"emp_dept": "many_to_one"}
    # 空けたまま・読めない書き方は使わない。
    assert (
        business_text_cardinalities(
            values,
            text.replace(
                "多対 1（many-to-one）", "（多重度（1 対多など）を確認して書いてください）"
            ),
        )
        == {}
    )


@pytest.mark.parametrize(
    ("join", "ask_key"),
    [
        # 結合のキーが判断できない（式）: 結合のキーはそのまま、多重度を空ける。
        ("UPPER(ADMIN.V_EMP_DEPT.DEPARTMENT_NAME) = ADMIN.DEPARTMENT.DEPARTMENT_NAME", False),
        # 一意でない列でつなぐ: まず結合のキーを空ける（#1760 の文）。
        (_VIEW_DEPT_NAME, True),
    ],
    ids=["expression", "non-unique"],
)
def test_undetermined_ai_cardinality_conflict_is_kept_and_suggested(
    join: str, ask_key: bool
) -> None:
    """結合のキーで決まらず、前の定義も業務説明も無いときだけ警告し、改善案に出す。"""

    links = [
        _link("emp_view_dept", "v_emp_dept", "department", join, "many_to_one"),
        _link("emp_view_dept", "v_emp_dept", "department", join, "many_to_many"),
    ]
    merged, conflicts = merge_definitions("p", [*_objects(), *links])
    kept = settle_link_cardinalities(conflicts, merged, _schema())
    [note] = _cardinality_conflicts(kept)
    assert isinstance(note, ConflictNote)
    finding = {
        "severity": "warning",
        "code": "DEFINITION_CONFLICT",
        "definition_id": "id-emp_view_dept",
        "field": "cardinality",
        "detail": note.detail(),
        "message_ja": str(note),
    }
    [suggestion] = improvement_suggestions(merged, [finding], _schema())
    text = suggestion["text"]
    assert text.startswith(
        "従業員部門ビュー（v_emp_dept）と部門（department）のリンク型"
        "『emp_view_dept のリンク』（emp_view_dept）は、"
    )
    if ask_key:
        assert "一意につながる結合のキーの列を確認して書いてください" in text
    else:
        assert text.endswith(
            f"{join} でつなぐ（多対 1（many-to-one） か 多対多（many-to-many） か"
            "を確認して書いてください）。"
        )
    # 空けたままの文は使わず、利用者が選んで書いた文だけが次の構築に効く。
    assert business_text_cardinalities(merged, text) == {}


def test_keyed_ai_link_suggestion_keeps_its_join_and_leaves_the_choice() -> None:
    link = _link("emp_dept", "employee", "department", _EMP_DEPT, "one_to_one")
    values = [*_objects(), link]
    finding = {
        "severity": "warning",
        "code": "DEFINITION_CONFLICT",
        "definition_id": "id-emp_dept",
        "detail": {
            "api_name": "emp_dept",
            "field": "cardinality",
            "values": ["1 対 1（one-to-one）", "多対 1（many-to-one）"],
        },
    }
    [suggestion] = improvement_suggestions(values, [finding], _schema())
    assert (
        f"は、{_EMP_DEPT} でつなぐ（1 対 1（one-to-one） か 多対 1（many-to-one） か"
        in (suggestion["text"])
    )
    filled = suggestion["text"].replace(
        "1 対 1（one-to-one） か 多対 1（many-to-one） かを確認して書いてください", "多対 1"
    )
    assert business_text_cardinalities(values, filled) == {"emp_dept": "many_to_one"}


# --- 使われない移行の経路の削除 -----------------------------------------------------------


def test_legacy_snapshot_migration_path_is_removed() -> None:
    """旧 snapshot の表を読む store・移行の CLI・表名の設定は持たない（未リリース。#1765）。"""

    assert not hasattr(store_module, "OracleJsonNl2SqlStore")
    assert "nl2sql_oracle_state_table" not in Settings.model_fields
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.cli.nl2sql_migrate_state")
