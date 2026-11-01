"""#1741〜#1743: 一意でない列で結合するリンク型・構築で外したものの次の構築への引き継ぎ・業務
プロファイルの削除で残るオントロジーの成果物。

実際の LLM / Oracle は使わない（スタブと sql_assist_sample を模した schema context。実 Oracle は
`test_nl2sql_profile_delete_oracle_live.py`）。
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from test_nl2sql_incremental_state import _profile_delete_repository
from test_nl2sql_ontology_definitions import runtime
from test_nl2sql_ontology_quality import _ai, _by_name, _schema, _view_dependencies, _view_objects
from test_nl2sql_ontology_unify_links import bundle, prop
from test_nl2sql_ontology_workspace import model

from app.features.nl2sql.ontology_build import _EXTRACTION_SYSTEM_PROMPT, OntologyBuildService
from app.features.nl2sql.ontology_build_prior import (
    MAX_PRIOR_REMOVALS,
    PRIOR_PROMPT,
    plain_build_removals,
)
from app.features.nl2sql.ontology_definition_service import profile_ontology_session_id
from app.features.nl2sql.ontology_definition_validation import validate_definitions
from app.features.nl2sql.ontology_definitions import LinkTypeDefinition
from app.features.nl2sql.ontology_link_keys import link_join_check
from app.features.nl2sql.ontology_markdown_workspace import MarkdownOntologyWorkspace
from app.features.nl2sql.ontology_unified_model import repair_definitions_report
from app.settings import get_settings

_NAME_JOIN = "ADMIN.V_DEPT_PROJECT.DEPARTMENT_NAME = ADMIN.DEPARTMENT.DEPARTMENT_NAME"
_ID_JOIN = "ADMIN.V_DEPT_PROJECT.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID"


def _name_link(**kw: Any) -> LinkTypeDefinition:
    values: dict[str, Any] = {
        "api_name": "v_dept_project_department",
        "name_ja": "部門別プロジェクトの部門",
        "source": "v_dept_project",
        "target": "department",
        "cardinality": "many_to_one",
        "join_expression_sql": _NAME_JOIN,
        "origin": "ai",
        **kw,
    }
    return LinkTypeDefinition(**values)


def _with_view_department_id(schema: dict[str, Any]) -> dict[str, Any]:
    """ビューに部門 ID の列がある schema（付け替えられる場合）。"""

    for o in schema["objects"]:
        if o["object_name"] == "V_DEPT_PROJECT":
            o["columns"].append(
                {"column": "DEPARTMENT_ID", "data_type": "NUMBER", "nullable": True}
            )
    return schema


# --- #1741 一意でない列で結合するリンク型 -------------------------------------------------


@pytest.mark.parametrize(
    ("join", "keyed"),
    [
        # 相手の主キー（制約）。
        ("ADMIN.PROJECT.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID", True),
        # 向きが逆でも、どちらかの端の主キーなら結合のキー。
        ("ADMIN.DEPARTMENT.DEPARTMENT_ID = ADMIN.PROJECT.DEPARTMENT_ID", True),
        ("ADMIN.PROJECT.PROJECT_NAME = ADMIN.DEPARTMENT.DEPARTMENT_NAME", False),
        # 単純な列の等号でない式は判断しない。
        ("UPPER(ADMIN.PROJECT.PROJECT_NAME) = ADMIN.DEPARTMENT.DEPARTMENT_NAME", None),
    ],
    ids=["target-primary-key", "reversed", "name-columns", "expression"],
)
def test_link_join_check_accepts_only_primary_unique_or_foreign_keys(
    join: str, keyed: bool | None
) -> None:
    values = [
        *_view_objects(),
        LinkTypeDefinition(
            api_name="l",
            name_ja="l",
            source="project",
            target="department",
            join_expression_sql=join,
        ),
    ]
    by_name = _by_name(values)
    assert link_join_check(by_name["l"], by_name, _schema()).keyed is keyed


def test_link_join_check_accepts_foreign_key_and_unconstrained_id_columns() -> None:
    schema = _schema(
        relationships=[
            {
                "id": "FK_PROJ_DEPT_NAME",
                "kind": "foreign_key",
                "source_object": "ADMIN.PROJECT",
                "target_object": "ADMIN.DEPARTMENT",
                "join_conditions": [
                    {
                        "left": "ADMIN.PROJECT.PROJECT_NAME",
                        "right": "ADMIN.DEPARTMENT.DEPARTMENT_NAME",
                    }
                ],
            }
        ]
    )
    values = [
        *_view_objects(),
        LinkTypeDefinition(
            api_name="fk",
            name_ja="fk",
            source="project",
            target="department",
            join_expression_sql="ADMIN.PROJECT.PROJECT_NAME = ADMIN.DEPARTMENT.DEPARTMENT_NAME",
        ),
    ]
    by_name = _by_name(values)
    # 外部キーの制約の列なら、一意キーでなくても結合のキー。
    assert link_join_check(by_name["fk"], by_name, schema).keyed is True
    # 制約の無い表の「表名_ID」の列（同じ名前・型の ID 列の規則）。
    for o in schema["objects"]:
        if o["object_name"] == "DEPARTMENT":
            o["constraints"] = []
    by_id = "ADMIN.PROJECT.DEPARTMENT_ID = ADMIN.DEPARTMENT.DEPARTMENT_ID"
    link = by_name["fk"].model_copy(update={"join_expression_sql": by_id})
    assert link_join_check(link, {**by_name, "fk": link}, schema).keyed is True
    # 制約の無い表でも、「表名_ID」でない一意でない列は結合のキーにしない。
    by_name_column = "ADMIN.PROJECT.PROJECT_NAME = ADMIN.DEPARTMENT.LOCATION"
    link = by_name["fk"].model_copy(update={"join_expression_sql": by_name_column})
    assert link_join_check(link, {**by_name, "fk": link}, schema).keyed is False


def test_ai_link_on_non_unique_column_is_rekeyed_by_the_id_column() -> None:
    """一意でない列でつなぐ AI のリンク型は、同じ両端の ID 列の結合のキーで付け替える。"""

    values = [*_view_objects(), _name_link()]
    result = repair_definitions_report(
        "p", values, _with_view_department_id(_schema()), ai_output=True
    )
    link = _by_name(result.definitions)["v_dept_project_department"]
    assert link.join_expression_sql == _ID_JOIN
    assert link.cardinality == "many_to_one"
    assert result.nonkey_links == []


def test_ai_link_on_non_unique_column_is_dropped_when_it_cannot_be_rekeyed() -> None:
    """付け替える列が無ければ下書きから外し、外したものと結合のキーの列を返す。"""

    values = [*_view_objects(), _name_link()]
    result = repair_definitions_report(
        "p", values, _schema(relationships=_view_dependencies()), ai_output=True
    )
    assert "v_dept_project_department" not in _by_name(result.definitions)
    assert [d.api_name for d in result.nonkey_links] == ["v_dept_project_department"]
    assert getattr(result.nonkey_links[0], "join_expression_sql", "") == _NAME_JOIN
    assert result.nonkey_columns["v_dept_project_department"] == (
        "V_DEPT_PROJECT.DEPARTMENT_NAME",
        "DEPARTMENT.DEPARTMENT_NAME",
    )
    # 結合のキーの無いリンク型として二重に数えない。
    assert result.keyless_links == []
    # 結合のキーのあるもの（ビュー → プロジェクト）は残す。
    assert any(d.kind == "link_type" and d.target == "project" for d in result.definitions), (
        result.definitions
    )


def test_ai_link_on_non_unique_column_joins_the_keyed_link_with_the_same_ends() -> None:
    """同じ両端に結合のキーのあるリンク型があれば、そちらに寄せる（名前は別名）。"""

    keyed = _name_link(api_name="dept_by_id", name_ja="所属部門", join_expression_sql=_ID_JOIN)
    values = [*_view_objects(), keyed, _name_link()]
    result = repair_definitions_report(
        "p", values, _with_view_department_id(_schema()), ai_output=True
    )
    links = [
        d
        for d in result.definitions
        if d.kind == "link_type" and {d.source, d.target} == {"v_dept_project", "department"}
    ]
    assert [d.api_name for d in links] == ["dept_by_id"]
    assert "部門別プロジェクトの部門" in links[0].aliases
    assert links[0].join_expression_sql == _ID_JOIN
    assert result.nonkey_links == []


@pytest.mark.parametrize("origin", ["manual", "previous"], ids=["user", "previous"])
def test_user_and_previous_non_unique_links_are_kept_with_a_warning(origin: str) -> None:
    link = _name_link(origin="manual" if origin == "manual" else "ai")
    previous = {link.api_name} if origin == "previous" else set()
    values = [*_view_objects(), link]
    result = repair_definitions_report("p", values, _schema(), ai_output=True, previous=previous)
    kept = _by_name(result.definitions)["v_dept_project_department"]
    assert kept.join_expression_sql == _NAME_JOIN
    assert result.nonkey_links == []
    findings = [
        f
        for f in validate_definitions(bundle(result.definitions), _schema())
        if f.code == "LINK_JOIN_NOT_UNIQUE"
    ]
    assert len(findings) == 1
    assert findings[0].severity == "warning"
    assert findings[0].definition_id == kept.id
    # 平易な 2 文。何が問題か（列）と、どうすればよいか。
    message = findings[0].message_ja
    assert message.count("。") == 2
    assert "DEPARTMENT_NAME" in message and "「結合のキー」" in message
    # 付け替え先（同じ両端のキーの列）が無いときは、確かめ方を書く（#1760）。
    assert "一意につながるかを確かめ" in message


def test_keyed_links_have_no_non_unique_warning() -> None:
    values = [
        *_view_objects(),
        _name_link(
            join_expression_sql="ADMIN.V_DEPT_PROJECT.PROJECT_ID = ADMIN.PROJECT.PROJECT_ID",
            target="project",
            api_name="v_project",
        ),
    ]
    findings = validate_definitions(bundle(values), _schema())
    assert not [f for f in findings if f.code == "LINK_JOIN_NOT_UNIQUE"]


def test_extraction_prompt_limits_join_keys_to_keys() -> None:
    assert "外部キー・主キー・一意制約の列で結合し" in _EXTRACTION_SYSTEM_PROMPT
    assert "DEPARTMENT_NAME" in _EXTRACTION_SYSTEM_PROMPT


# --- #1742 構築で外したものを次の構築の「直すべき点」に --------------------------------------


def test_build_removals_are_plain_sentences_with_reason_and_condition() -> None:
    link = _name_link()
    removals = plain_build_removals(
        unused_shared=[prop("dept_shared", "部門（共有）", "", "DEPARTMENT", "DEPARTMENT_ID")],
        unmapped_properties=_ai(
            prop("department_head_count", "人数", "department", "DEPARTMENT", "X").model_copy(
                update={"mappings": []}
            )
        ),
        keyless_links=[link.model_copy(update={"join_expression_sql": ""})],
        nonkey_links=[link],
        nonkey_columns={link.api_name: ("V_DEPT_PROJECT.DEPARTMENT_NAME",)},
        names={"department": "部門", "v_dept_project": "部門別プロジェクト"},
    )
    assert len(removals) == 4
    assert all(r.startswith("前回の構築で外したもの: ") for r in removals)
    assert "『部門』の『人数』（department_head_count）は対応する列が分からない" in removals[1]
    assert "列が分かるときだけ" in removals[1]
    assert "『部門別プロジェクト』（v_dept_project）と『部門』（department）" in removals[2]
    assert "V_DEPT_PROJECT.DEPARTMENT_NAME" in removals[3]
    assert "外部キー・主キー・一意キーの列でつなげるときだけ" in removals[3]
    # 内部の ID・診断コードは入れない。
    assert not any(code in "".join(removals) for code in ("LINK_JOIN", "profile_concept"))
    many = plain_build_removals(
        unused_shared=[
            prop(f"s{i}", f"共有{i}", "", "DEPARTMENT", "DEPARTMENT_ID") for i in range(30)
        ]
    )
    assert len(many) == MAX_PRIOR_REMOVALS


@pytest.mark.rule_parser
def test_removals_come_first_in_prior_findings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    rt, _legacy = runtime()
    svc = MarkdownOntologyWorkspace(rt)
    assert svc.build_removals("sales") == []
    note = "前回の構築で外したもの: A"
    svc.record_build_removals("sales", "job-1", [note, note])
    prior = svc.prior_build_context("sales")
    assert prior is not None
    assert prior["previous_check_findings"][0] == "前回の構築で外したもの: A"
    assert prior["previous_check_findings"].count("前回の構築で外したもの: A") == 1
    # 次の構築で外したものが無ければ、空で上書きする（前の注記を残し続けない）。
    svc.record_build_removals("sales", "job-2", [])
    assert svc.build_removals("sales") == []
    assert "前回の構築で下書きから外したもの" in PRIOR_PROMPT


def test_removed_definitions_reach_the_next_build_llm_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1 回目の構築で外した共有プロパティが、2 回目の構築の LLM の入力の直すべき点に入る。"""

    from test_nl2sql_ontology_build import _FakeEnterpriseAiClient, _wait_for_job

    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    rt, legacy = runtime()
    payload = [d.model_dump(mode="json") for d in model()]
    payload.append(
        {
            "kind": "shared_property",
            "api_name": "OrderNumberShared",
            "name_ja": "受注番号（共有）",
            "description_ja": "どのプロパティからも使われない",
        }
    )
    client = _FakeEnterpriseAiClient(json.dumps({"definitions": payload}))
    legacy._enterprise_ai_client = client
    service = OntologyBuildService(rt)
    first = _wait_for_job(service, service.start("sales").id)
    assert first.status == "succeeded", first.error_message_ja
    assert not any("前回の構築で外したもの" in c for c in client.contexts)
    removals = MarkdownOntologyWorkspace(rt).build_removals("sales")
    assert removals and "受注番号（共有）" in removals[0]

    client.contexts.clear()
    second = _wait_for_job(service, service.start("sales").id)
    assert second.status == "succeeded", second.error_message_ja
    findings = [
        json.loads(c).get("previous_check_findings") or [] for c in client.contexts if c.strip()
    ]
    assert any(removals[0] in items for items in findings), findings


# --- #1743 業務プロファイルの削除で残るオントロジーの成果物 ------------------------------------


def test_oracle_profile_delete_removes_artifacts_and_finished_jobs_in_one_transaction() -> None:
    repository, connection = _profile_delete_repository()

    repository.delete_profile("profile-1", expected_etag="etag-1")

    sql = [statement for statement, _binds in connection.executed]
    parent = next(i for i, s in enumerate(sql) if s.startswith("DELETE FROM NL2SQL_PROFILES"))
    artifacts = next(i for i, s in enumerate(sql) if "DELETE FROM NL2SQL_ONTOLOGY_ARTIFACTS" in s)
    jobs = next(i for i, s in enumerate(sql) if "DELETE FROM NL2SQL_ONTOLOGY_JOBS" in s)
    # 業務プロファイルを消した後、同じ transaction（commit 1 回）で存在しない業務プロファイルの
    # 成果物と終わったジョブ（この業務プロファイルの分と、前に削除したものの残り。#1758）を消す。
    assert parent < artifacts and parent < jobs
    assert "NOT EXISTS (SELECT 1 FROM NL2SQL_PROFILES" in sql[artifacts]
    assert "STATUS IN ('succeeded', 'succeeded_with_warnings', 'failed', 'cancelled')" in sql[jobs]
    assert "NOT EXISTS (SELECT 1 FROM NL2SQL_PROFILES" in sql[jobs]
    assert "PROFILE_ID <> '-'" in sql[jobs]
    assert connection.executed[artifacts][1] == {"session_prefix": "profile-ontology:"}
    assert connection.commits == 1
    assert connection.rollbacks == 0


def test_oracle_profile_delete_rolls_back_when_artifact_delete_fails() -> None:
    repository, connection = _profile_delete_repository(
        fail_on="DELETE FROM NL2SQL_ONTOLOGY_ARTIFACTS"
    )

    with pytest.raises(RuntimeError, match="scripted delete failure"):
        repository.delete_profile("profile-1", expected_etag="etag-1")

    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_memory_profile_state_delete_removes_artifacts_and_finished_jobs() -> None:
    rt, _legacy = runtime()
    store = rt.store
    session = profile_ontology_session_id("sales")
    store.save_artifact(
        {
            "artifact_id": "ckpt",
            "session_id": session,
            "profile_id": "sales",
            "artifact_type": "ontology_build_checkpoint",
            "content": "{}",
            "content_hash": "h",
        }
    )
    store.save_artifact(
        {
            "artifact_id": "draft",
            "session_id": "ontology_revision_x",
            "profile_id": "sales",
            "artifact_type": "ontology_markdown_draft",
            "content": "",
            "content_hash": "h",
        }
    )
    for artifact_id, profile_id in (("other", "support"), ("shared", None)):
        store.save_artifact(
            {
                "artifact_id": artifact_id,
                "session_id": "ontology_revision_x",
                "artifact_type": "ontology_owl_turtle",
                "content": "",
                "content_hash": "h",
                **({"profile_id": profile_id} if profile_id else {}),
            }
        )
    for job_id, status in (("done", "succeeded"), ("running", "running")):
        store.save_job(
            {
                "job_id": job_id,
                "job_type": "build",
                "profile_id": "sales",
                "status": status,
                "payload": {},
            }
        )

    rt.delete_profile_state("sales")

    assert {d["artifact_id"] for d in store.list_documents("artifacts", {})} == {"other", "shared"}
    # 実行中のジョブは消さない（削除の後に取り消す）。
    assert {d["job_id"] for d in store.list_documents("jobs", {})} == {"running"}


def test_checkpoint_is_not_saved_after_the_profile_is_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_nl2sql_ontology_build import _FakeEnterpriseAiClient, _wait_for_job

    from app.features.nl2sql.ontology_service import OntologyNotFoundError

    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    rt, legacy = runtime()
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(
        json.dumps({"definitions": [d.model_dump(mode="json") for d in model()]})
    )
    service = OntologyBuildService(rt)
    monkeypatch.setattr(service, "_profile_exists", lambda _profile_id: False)
    _wait_for_job(service, service.start("sales").id)
    checkpoints = [
        d
        for d in rt.store.list_documents("artifacts", {})
        if d.get("artifact_type") == "ontology_build_checkpoint"
    ]
    assert checkpoints == []
    # 削除済みの判定は、業務プロファイルが見つからないときだけ。
    monkeypatch.undo()
    monkeypatch.setattr(get_settings(), "auth_mode", "local")

    def missing(_profile_id: str) -> Any:
        raise OntologyNotFoundError("NL2SQL_PROFILE_NOT_FOUND", "x")

    monkeypatch.setattr(rt, "ensure_profile", missing)
    assert service._profile_exists("sales") is False  # noqa: SLF001
