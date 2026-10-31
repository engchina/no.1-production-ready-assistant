"""#1758〜#1760: 孤立したオントロジーのジョブの記録の掃除・再構築の粒度の競合・一意でない列で
結合するリンク型の直し方と改善案。

実 OCI / Oracle は使わない（スタブと sql_assist_sample を模した schema context。実 Oracle は
`test_nl2sql_profile_delete_oracle_live.py`）。
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

import pytest
from test_nl2sql_ontology_quality import _ai, _by_name, _schema, _view_objects
from test_nl2sql_ontology_unify_links import bundle, obj, prop
from test_system_schema_manager import _WorkflowManager

from app.features.nl2sql.incremental_store import purge_orphan_profile_ontology_records
from app.features.nl2sql.ontology_definition_validation import validate_definitions
from app.features.nl2sql.ontology_definitions import LinkTypeDefinition, ObjectTypeDefinition
from app.features.nl2sql.ontology_improvement_suggestion import (
    apply_business_text_facts,
    apply_business_text_links,
    business_text_grain_objects,
    improvement_suggestions,
)
from app.features.nl2sql.ontology_link_keys import link_join_check, suggest_link_join
from app.features.nl2sql.ontology_unified_model import (
    ConflictNote,
    merge_definitions,
    repair_definitions_report,
    settle_grain_conflicts,
)
from app.settings import Settings

# --- #1758 孤立したオントロジーのジョブの記録 ----------------------------------------------


class _RecordingCursor:
    def __init__(self, connection: _RecordingConnection) -> None:
        self.connection = connection
        self.rowcount = 0

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, binds: Any = None) -> None:
        if self.connection.fail:
            raise RuntimeError("ORA-00942: table or view does not exist")
        self.connection.executed.append((sql, binds))
        self.rowcount = 3 if "NL2SQL_ONTOLOGY_JOBS" in sql else 2


class _RecordingConnection:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.executed: list[tuple[str, Any]] = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self) -> _RecordingCursor:
        return _RecordingCursor(self)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_orphan_purge_deletes_only_finished_jobs_of_missing_profiles() -> None:
    """存在しない業務プロファイルの終わったジョブだけを消す（実行中・全体のジョブは残す）。"""

    connection = _RecordingConnection()
    with connection.cursor() as cursor:
        assert purge_orphan_profile_ontology_records(cursor) == (2, 3)
    (artifacts_sql, artifacts_binds), (jobs_sql, jobs_binds) = connection.executed
    assert artifacts_sql.startswith("DELETE FROM NL2SQL_ONTOLOGY_ARTIFACTS")
    assert artifacts_binds == {"session_prefix": "profile-ontology:"}
    assert jobs_sql.startswith("DELETE FROM NL2SQL_ONTOLOGY_JOBS")
    assert "PROFILE_ID <> '-'" in jobs_sql
    assert "STATUS IN ('succeeded', 'succeeded_with_warnings', 'failed', 'cancelled')" in jobs_sql
    assert "NOT EXISTS (SELECT 1 FROM NL2SQL_PROFILES p WHERE p.PROFILE_ID = j.PROFILE_ID)" in (
        jobs_sql
    )
    assert jobs_binds is None
    # NL2SQL の 2 つの表だけ（他の製品の表・業務プロファイルの表は消さない）。
    for sql, _binds in connection.executed:
        assert sql.split()[2].startswith("NL2SQL_ONTOLOGY_")
    # commit は呼び出し側（業務プロファイルの削除・system schema の初期化）。
    assert connection.commits == 0


class _PurgingManager(_WorkflowManager):
    def __init__(self, initial_status: str, connection: _RecordingConnection) -> None:
        super().__init__(initial_status)
        self.recording = connection

    @contextmanager
    def _connection(self) -> Any:
        yield self.recording


@pytest.mark.parametrize("initial", ["ready", "missing"], ids=["no-op", "migrate"])
def test_system_schema_initialize_purges_orphan_ontology_records(initial: str) -> None:
    """冪等な初期化（変更なし・作成）のたびに、孤立した成果物・ジョブの記録を消す（#1758）。"""

    connection = _RecordingConnection()
    result = _PurgingManager(initial, connection).initialize()

    assert result["status"] == "ready"
    deleted = [sql.split()[2] for sql, _binds in connection.executed]
    assert deleted == ["NL2SQL_ONTOLOGY_ARTIFACTS", "NL2SQL_ONTOLOGY_JOBS"]
    assert connection.commits == 1


def test_system_schema_recreate_does_not_purge() -> None:
    connection = _RecordingConnection()
    manager = _PurgingManager("ready", connection)
    manager._assert_no_active_jobs = lambda _connection: None  # type: ignore[method-assign,assignment]
    manager._drop_managed_objects = lambda _connection, _owner: 0  # type: ignore[method-assign,assignment]
    manager.initialize(recreate=True, confirmation=manager.recreate_confirmation)
    assert connection.executed == []


def test_system_schema_initialize_succeeds_when_the_purge_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """掃除に失敗しても初期化は失敗にしない（rollback して警告を残し、次の契機でやり直す）。"""

    connection = _RecordingConnection(fail=True)
    with caplog.at_level(logging.WARNING):
        result = _PurgingManager("ready", connection).initialize()
    assert result["operation"] == "no_op"
    assert connection.rollbacks == 1 and connection.commits == 0
    record = next(r for r in caplog.records if r.message == "nl2sql_orphan_ontology_purge_failed")
    assert getattr(record, "error_code", "") == "ORA-00942"


def test_legacy_snapshot_state_backend_is_removed() -> None:
    """移行前互換の `NL2SQL_STATE_BACKEND=legacy_snapshot` は無い（oracle は常に incremental）。"""

    assert "nl2sql_state_backend" not in Settings.model_fields
    assert "nl2sql_migration_mirror_enabled" not in Settings.model_fields


# --- #1759 再構築の粒度の競合 -------------------------------------------------------------


def _department(grain: str, *, key: str = "department_id", origin: str = "ai") -> list[Any]:
    column = "DEPARTMENT_ID" if key == "department_id" else "DEPARTMENT_NAME"
    return [
        obj(
            "department",
            "部署",
            "DEPARTMENT",
            [key],
            primary_key=[key],
            grain_ja=grain,
            origin=origin,
        ),
        prop(key, "部門ID", "department", "DEPARTMENT", column, origin=origin),
    ]


def _grain(values: list[Any]) -> str:
    return str(next(d.grain_ja for d in values if d.api_name == "department"))


def _grain_conflicts(conflicts: list[str]) -> list[str]:
    return [c for c in conflicts if isinstance(c, ConflictNote) and c.field == "grain_ja"]


def test_previous_grain_wins_and_ai_rewording_is_not_a_conflict() -> None:
    """前の定義の粒度が正。AI の出力の言い換えは警告にしない（#1759 の実 LLM の 2・3 回目）。"""

    values = [*_department("1 行が 1 つの部門", origin="manual"), *_department("部署")]
    merged, conflicts = merge_definitions("p", values, previous={"department", "department_id"})
    assert _grain(merged) == "1 行が 1 つの部門"
    assert _grain_conflicts(conflicts) == []


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("1 行が 1 つのプロジェクト", "プロジェクト"),
        ("部署ごとに 1 行", "1 行が 1 つの部署"),
    ],
    ids=["one-row-per", "per-row"],
)
def test_ai_rewordings_with_the_same_key_are_not_a_conflict(first: str, second: str) -> None:
    merged, conflicts = merge_definitions("p", [*_department(first), *_department(second)])
    assert _grain(merged) == first
    assert _grain_conflicts(conflicts) == []


def test_ai_grains_on_the_same_table_without_keys_are_not_a_conflict() -> None:
    """主キーが分からなくても、同じ表の 1 行の意味は同じ（文の言い換え）。"""

    values = [
        obj("department", "部署", "DEPARTMENT", grain_ja="1 行が 1 つの部門", origin="ai"),
        obj("department", "部署", "DEPARTMENT", grain_ja="部署", origin="ai"),
    ]
    _merged, conflicts = merge_definitions("p", values)
    assert _grain_conflicts(conflicts) == []


def test_ai_grains_with_different_keys_are_a_plain_two_sentence_conflict() -> None:
    values = [*_department("1 行が 1 つの部署"), *_department("部署名ごと", key="department_name")]
    merged, conflicts = merge_definitions("p", values)
    [note] = _grain_conflicts(conflicts)
    assert isinstance(note, ConflictNote)
    assert note.api_name == "department"
    assert note.values == ("1 行が 1 つの部署", "部署名ごと")
    # 平易な 2 文: どちらか（キーの列）と、業務説明への書き方。
    assert note.count("。") == 2
    assert "DEPARTMENT_ID ごと" in note and "DEPARTMENT_NAME ごと" in note
    assert "業務説明に正しい粒度と主キーを書いて" in note
    assert "profile_concept_" not in note and "grain_ja" not in note
    assert _grain(merged) == "1 行が 1 つの部署"


def test_text_only_grains_that_really_differ_are_still_a_conflict() -> None:
    """表も主キーも分からないときは、言い換えを除いた文で比べる（受注単位と明細単位は違う）。"""

    values: list[Any] = [
        ObjectTypeDefinition(api_name="Order", name_ja="受注", grain_ja="受注単位", origin="ai"),
        ObjectTypeDefinition(api_name="Order", name_ja="受注", grain_ja="明細単位", origin="ai"),
    ]
    _merged, conflicts = merge_definitions("p", values)
    assert len(_grain_conflicts(conflicts)) == 1


def test_grain_conflict_is_settled_by_business_text_then_database_key() -> None:
    """正の順: 前の定義 → 業務説明の文 → DB の主キー・一意キー（決まらないものだけ残す）。"""

    values = [*_department("部署名ごと", key="department_name"), *_department("1 行が 1 つの部署")]
    merged, conflicts = merge_definitions("p", values)
    assert len(_grain_conflicts(conflicts)) == 1
    # 業務説明に粒度が書かれていれば、その業務説明が正（競合は出さない）。
    text = (
        "部署（department）は ADMIN.DEPARTMENT の表で、1 行が 1 つの部署、主キーは DEPARTMENT_ID。"
    )
    assert business_text_grain_objects(text) == {"department"}
    assert settle_grain_conflicts(conflicts, merged, _schema(), decided={"department"}) == [
        c for c in conflicts if c not in _grain_conflicts(conflicts)
    ]
    # 業務説明が無ければ、DB の主キー（DEPARTMENT_ID）に当たる方の粒度を使う。
    kept = settle_grain_conflicts(conflicts, merged, _schema())
    assert _grain_conflicts(kept) == []
    assert _grain(merged) == "1 行が 1 つの部署"


def test_grain_conflict_without_database_key_is_kept() -> None:
    values = [
        obj("v", "ビュー", "V_EMP_DEPT", ["a"], primary_key=["a"], grain_ja="従業員", origin="ai"),
        prop("a", "A", "v", "V_EMP_DEPT", "EMPLOYEE_ID", origin="ai"),
        obj("v", "ビュー", "V_EMP_DEPT", ["b"], primary_key=["b"], grain_ja="部門", origin="ai"),
        prop("b", "B", "v", "V_EMP_DEPT", "DEPARTMENT_NAME", origin="ai"),
    ]
    merged, conflicts = merge_definitions("p", values)
    assert len(_grain_conflicts(settle_grain_conflicts(conflicts, merged, _schema()))) == 1


def test_business_text_grain_replaces_ai_values_but_not_previous_definitions() -> None:
    definitions: list[Any] = [
        obj("department", "部署", "DEPARTMENT", grain_ja="部署", origin="ai"),
        obj("project", "プロジェクト", "PROJECT", grain_ja="案件ごと", origin="manual"),
    ]
    text = (
        "部署（department）は ADMIN.DEPARTMENT の表で、1 行が 1 つの部署、主キーは"
        " DEPARTMENT_ID。\n"
        "プロジェクト（project）は ADMIN.PROJECT の表で、1 行が 1 つのプロジェクト、主キーは"
        " PROJECT_ID。"
    )
    assert apply_business_text_facts(definitions, text, previous={"project"}) == ["department"]
    assert definitions[0].grain_ja == "1 行が 1 つの部署"
    # 前の定義（利用者が書いた・公開した）は確認なしに書き換えない。
    assert definitions[1].grain_ja == "案件ごと"
    # previous を渡さない呼び出し（今までの使い方）は、値の入った定義を変えない。
    other: list[Any] = [obj("department", "部署", "DEPARTMENT", grain_ja="部署")]
    assert apply_business_text_facts(other, text) == []


def test_grain_conflict_suggestion_is_an_object_sentence_that_the_next_build_reads() -> None:
    """粒度の競合の改善案は、主キーと一緒のオブジェクト型の文（どちらかを空けておく）。"""

    definitions = _department("1 行が 1 つの部署")
    findings = [
        {
            "severity": "warning",
            "code": "DEFINITION_CONFLICT",
            "definition_id": "department",
            "field": "grain_ja",
            "detail": {
                "api_name": "department",
                "field": "grain_ja",
                "values": ["1 行が 1 つの部署", "部署名ごと"],
            },
        }
    ]
    [suggestion] = improvement_suggestions(definitions, findings, _schema())
    assert suggestion["severity"] == "warning"
    assert suggestion["text"] == (
        "部署（department）は ADMIN.DEPARTMENT の表で、"
        "（1 行が 1 つの部署 と 部署名ごと のどちらかを確認して書いてください）、"
        "主キーは DEPARTMENT_ID。"
    )
    # 空けたままでは使わず、利用者が選んで書いた文だけが次の構築に効く。
    ai = _department("部署名ごと", key="department_name")
    assert apply_business_text_facts(ai, suggestion["text"], previous=set()) == []
    filled = suggestion["text"].replace(
        "（1 行が 1 つの部署 と 部署名ごと のどちらかを確認して書いてください）",
        "1 行が 1 つの部署",
    )
    assert apply_business_text_facts(ai, filled, previous=set()) == ["department"]
    assert _grain(ai) == "1 行が 1 つの部署"


# --- #1760 一意でない列で結合する利用者のリンク型 ------------------------------------------

_EMP_NAME_JOIN = "ADMIN.V_EMP_DEPT.EMPLOYEE_NAME = ADMIN.EMPLOYEE.EMPLOYEE_NAME"
_EMP_ID_JOIN = "ADMIN.V_EMP_DEPT.EMPLOYEE_ID = ADMIN.EMPLOYEE.EMPLOYEE_ID"


def _emp_schema() -> dict[str, Any]:
    """ビュー V_EMP_DEPT に従業員名の列がある schema（従業員 ID に付け替えられる場合）。"""

    schema = _schema()
    for o in schema["objects"]:
        if o["object_name"] == "V_EMP_DEPT":
            o["columns"].append(
                {"column": "EMPLOYEE_NAME", "data_type": "VARCHAR2(100)", "nullable": True}
            )
    return schema


def _emp_objects(origin: str = "manual") -> list[Any]:
    values = [
        obj("employee", "従業員", "EMPLOYEE", ["employee_id"], primary_key=["employee_id"]),
        prop("employee_id", "従業員ID", "employee", "EMPLOYEE", "EMPLOYEE_ID"),
        obj(
            "emp_dept_view",
            "従業員部門ビュー",
            "V_EMP_DEPT",
            ["emp_dept_view_employee_id"],
            primary_key=["emp_dept_view_employee_id"],
        ),
        prop(
            "emp_dept_view_employee_id",
            "従業員ID",
            "emp_dept_view",
            "V_EMP_DEPT",
            "EMPLOYEE_ID",
        ),
    ]
    return [v.model_copy(update={"origin": origin}) for v in values]


def _emp_link(join: str = _EMP_NAME_JOIN, **kw: Any) -> LinkTypeDefinition:
    values: dict[str, Any] = {
        "api_name": "emp_dept_view_to_employee",
        "id": "link-emp-dept-view",
        "name_ja": "従業員部門ビューの従業員",
        "source": "emp_dept_view",
        "target": "employee",
        "cardinality": "one_to_one",
        "join_expression_sql": join,
        "origin": "manual",
        **kw,
    }
    return LinkTypeDefinition(**values)


def _not_unique(values: list[Any], schema: dict[str, Any]) -> list[Any]:
    return [
        f for f in validate_definitions(bundle(values), schema) if f.code == "LINK_JOIN_NOT_UNIQUE"
    ]


def test_suggest_link_join_uses_the_key_column_of_the_same_ends() -> None:
    values = [*_emp_objects(), _emp_link()]
    by_name = _by_name(values)
    fix = suggest_link_join(by_name["emp_dept_view_to_employee"], by_name, _emp_schema())
    assert fix is not None
    assert fix.join_expression_sql == _EMP_ID_JOIN
    assert fix.columns == ("EMPLOYEE_ID",)
    # ビューの主キー（オブジェクト型）も EMPLOYEE_ID のため 1 対 1。
    assert fix.cardinality == "one_to_one"


def test_suggest_link_join_uses_the_foreign_key_in_the_link_direction() -> None:
    schema = _schema(
        relationships=[
            {
                "id": "FK_EMP_DEPT",
                "kind": "foreign_key",
                "source_object": "ADMIN.EMPLOYEE",
                "target_object": "ADMIN.DEPARTMENT",
                "cardinality": "many_to_one",
                "join_conditions": [
                    {
                        "left": "ADMIN.EMPLOYEE.DEPARTMENT_ID",
                        "right": "ADMIN.DEPARTMENT.DEPARTMENT_ID",
                    }
                ],
            }
        ]
    )
    values = [
        obj("employee", "従業員", "EMPLOYEE"),
        obj("department", "部署", "DEPARTMENT"),
        LinkTypeDefinition(
            api_name="dept_employees",
            name_ja="部署の従業員",
            source="department",
            target="employee",
            join_expression_sql="ADMIN.DEPARTMENT.LOCATION = ADMIN.EMPLOYEE.EMPLOYEE_NAME",
        ),
    ]
    by_name = _by_name(values)
    fix = suggest_link_join(by_name["dept_employees"], by_name, schema)
    assert fix is not None
    assert fix.join_expression_sql == (
        "ADMIN.DEPARTMENT.DEPARTMENT_ID = ADMIN.EMPLOYEE.DEPARTMENT_ID"
    )
    assert fix.cardinality == "one_to_many"


def test_user_link_warning_names_the_concrete_join_key() -> None:
    """付け替え先があれば、警告の「どうすればよいか」に具体の結合のキーを書く（2 文）。"""

    values = [*_emp_objects(), _emp_link()]
    [finding] = _not_unique(values, _emp_schema())
    message = finding.message_ja
    assert message.count("。") == 2
    assert "V_EMP_DEPT.EMPLOYEE_NAME、EMPLOYEE.EMPLOYEE_NAME" in message
    assert f"「結合のキー」を EMPLOYEE_ID でつなぐ形（{_EMP_ID_JOIN}）に直してください。" in message
    assert "LINK_JOIN_NOT_UNIQUE" not in message and "profile_concept_" not in message


def test_link_suggestion_is_appended_only_by_the_user_and_fixes_the_next_build() -> None:
    """改善案にリンク型の正しい結合のキーと多重度が出て、追記した構築でだけ直り、警告が消える。"""

    schema = _emp_schema()
    previous = [*_emp_objects(), _emp_link()]
    [finding] = _not_unique(previous, schema)
    suggestions = improvement_suggestions(previous, [finding.model_dump(mode="json")], schema)
    texts = [s["text"] for s in suggestions]
    sentence = (
        "従業員部門ビュー（emp_dept_view）と従業員（employee）のリンク型"
        "『従業員部門ビューの従業員』（emp_dept_view_to_employee）は、"
        f"{_EMP_ID_JOIN} でつなぐ（1 対 1（one-to-one））。"
    )
    assert texts == [sentence]
    assert suggestions[0]["severity"] == "warning"

    # 業務説明に追記していない構築では、利用者・前の定義のリンク型を書き換えない。
    untouched = [d.model_copy(deep=True) for d in previous]
    assert apply_business_text_links(untouched, "受注の業務です。", schema) == []
    assert _by_name(untouched)["emp_dept_view_to_employee"].join_expression_sql == _EMP_NAME_JOIN

    # 追記した構築（構築と同じ順: 統合 → 改善案の結合のキー → 修復）。AI が同じ両端に作った
    # キーのリンク型とは 1 つにまとまり、前の定義の名前が残る。
    ai_link = _emp_link(_EMP_ID_JOIN, api_name="v_emp_dept_to_employee", id="", origin="ai")
    names = {d.api_name for d in previous}
    merged, _conflicts = merge_definitions("p", [*previous, *_ai(ai_link)], previous=names)
    business_text = "受注の業務です。\n" + sentence
    assert apply_business_text_links(merged, business_text, schema) == ["emp_dept_view_to_employee"]
    result = repair_definitions_report("p", merged, schema, ai_output=True, previous=names)
    links = [d for d in result.definitions if d.kind == "link_type"]
    assert [(d.api_name, d.join_expression_sql, d.cardinality) for d in links] == [
        ("emp_dept_view_to_employee", _EMP_ID_JOIN, "one_to_one")
    ]
    assert _not_unique(result.definitions, schema) == []


def test_link_facts_do_not_change_keyed_links_or_unverified_joins() -> None:
    schema = _emp_schema()
    keyed = [*_emp_objects(), _emp_link(_EMP_ID_JOIN)]
    text = (
        "従業員部門ビュー（emp_dept_view）と従業員（employee）のリンク型"
        "『従業員部門ビューの従業員』（emp_dept_view_to_employee）は、"
        f"{_EMP_NAME_JOIN} でつなぐ（多対 1（many-to-one））。"
    )
    # キーでつなぐリンク型は、業務説明の文で一意でない列に戻さない。
    assert apply_business_text_links(keyed, text, schema) == []
    broken = [*_emp_objects(), _emp_link()]
    # 文の結合のキーもキーでつながらなければ使わない（推測で埋めない）。
    assert apply_business_text_links(broken, text, schema) == []
    assert link_join_check(broken[-1], _by_name(broken), schema).keyed is False


def test_link_suggestion_without_a_key_asks_how_to_check() -> None:
    """付け替え先が無いときは、確かめ方を示し、改善案の結合のキーは空けておく。"""

    link = LinkTypeDefinition(
        api_name="v_dept_project_department",
        id="link-dept-project",
        name_ja="部門別プロジェクトの部門",
        source="v_dept_project",
        target="department",
        cardinality="many_to_one",
        join_expression_sql=(
            "ADMIN.V_DEPT_PROJECT.DEPARTMENT_NAME = ADMIN.DEPARTMENT.DEPARTMENT_NAME"
        ),
        origin="manual",
    )
    values = [*_view_objects(), link]
    [finding] = _not_unique(values, _schema())
    assert "一意につながるかを確かめ" in finding.message_ja
    assert finding.message_ja.count("。") == 2
    [suggestion] = improvement_suggestions(values, [finding.model_dump(mode="json")], _schema())
    assert suggestion["text"] == (
        "部門別プロジェクト（v_dept_project）と部門（department）のリンク型"
        "『部門別プロジェクトの部門』（v_dept_project_department）は、"
        "（主キー・一意キー・外部キーで一意につながる結合のキーの列を確認して書いてください）"
        "でつなぐ。"
    )
    assert apply_business_text_links(values, suggestion["text"], _schema()) == []
