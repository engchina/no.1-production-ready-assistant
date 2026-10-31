"""型付き Q/A 関係の入力 SQL 接地と入力ごとの隔離。"""

import json
from typing import Any

import pytest
from test_nl2sql_ontology_build import _FakeEnterpriseAiClient, _wait_for_job
from test_nl2sql_ontology_definitions import definition_payload, runtime

from app.features.nl2sql.ontology_build import OntologyBuildService, merge_build_extractions
from app.features.nl2sql.ontology_build_grounding import ground_qa_links
from app.features.nl2sql.ontology_definition_service import ProfileOntologyDefinitionService
from app.features.nl2sql.ontology_definitions import DefinitionEvidence
from app.features.nl2sql.ontology_models import OntologyBuildExtraction, QaPair
from app.features.nl2sql.ontology_unified_model import merge_definitions
from app.settings import get_settings

SCHEMA: dict[str, Any] = {
    "objects": [
        {"owner": "APP", "object_name": "ORDERS", "columns": ["ID", "CUSTOMER_ID", "AMOUNT"]},
        {"owner": "APP", "object_name": "CUSTOMERS", "columns": ["ID", "AMOUNT"]},
        {"owner": "SALES", "object_name": '"Mixed_Case"', "columns": ['"Amount"', "ID"]},
    ]
}
JOIN = "APP.ORDERS.CUSTOMER_ID = APP.CUSTOMERS.ID"


def extraction(expression: str = JOIN) -> OntologyBuildExtraction:
    return OntologyBuildExtraction.model_validate(
        {
            "coverage": [{"kind": "link_type", "status": "generated", "count": 1}],
            "definitions": [
                {
                    "kind": "link_type",
                    "api_name": "OrderCustomer",
                    "name_ja": "受注顧客",
                    "source": "Order",
                    "target": "Customer",
                    "join_expression_sql": expression,
                },
                {
                    "kind": "shared_property",
                    "api_name": "Amount",
                    "name_ja": "金額",
                    "data_type": "number",
                    # 使われない AI の共有プロパティは外すため、列の対応を持たせる（#1734）。
                    "mappings": [
                        {"owner": "APP", "object_name": "ORDERS", "column_name": "AMOUNT"}
                    ],
                },
            ],
        }
    )


@pytest.mark.parametrize(
    "sql,expression,accepted",
    [
        (["SELECT o.ID FROM APP.ORDERS o JOIN APP.CUSTOMERS c ON o.CUSTOMER_ID=c.ID"], JOIN, True),
        (
            [
                "WITH x AS (SELECT CUSTOMER_ID AS CID FROM APP.ORDERS) "
                "SELECT x.CID FROM x JOIN APP.CUSTOMERS c ON x.CID=c.ID"
            ],
            JOIN,
            True,
        ),
        (["SELECT o.ID FROM APP.ORDERS o, APP.CUSTOMERS c WHERE o.CUSTOMER_ID=c.ID"], JOIN, True),
        (["SELECT ID FROM APP.ORDERS"], "APP.ORDERS.AMOUNT = APP.CUSTOMERS.ID", False),
        (["SELECT ID, AMOUNT FROM APP.CUSTOMERS"], "APP.ORDERS.AMOUNT = APP.CUSTOMERS.ID", False),
        (["SELECT CUSTOMER_ID FROM APP.ORDERS", "SELECT ID FROM APP.CUSTOMERS"], JOIN, False),
        (["SELECT 'AMOUNT' FROM APP.ORDERS"], "APP.ORDERS.AMOUNT = APP.CUSTOMERS.ID", False),
        (["SELECT * FROM APP.ORDERS o JOIN APP.CUSTOMERS c ON o.CUSTOMER_ID=c.ID"], JOIN, True),
        (["SELECT broken FROM"], JOIN, False),
        (["SELECT ID FROM APP.ORDERS"], "", False),
        (["SELECT ID FROM APP.ORDERS"], "1=1", False),
        (
            [
                'SELECT m."Amount", c.ID FROM SALES."Mixed_Case" m '
                'JOIN APP.CUSTOMERS c ON m."Amount"=c.ID'
            ],
            'SALES."Mixed_Case"."Amount" = APP.CUSTOMERS.ID',
            True,
        ),
        (
            ['SELECT m.ID, c.ID FROM SALES."Mixed_Case" m JOIN APP.CUSTOMERS c ON m.ID=c.ID'],
            'SALES."Mixed_Case"."Amount" = APP.CUSTOMERS.ID',
            False,
        ),
    ],
)
def test_typed_link_requires_physical_columns_in_one_qa(
    sql: list[str], expression: str, accepted: bool
) -> None:
    value = extraction(expression)
    result = ground_qa_links(value, sql, SCHEMA)
    assert any(d.kind == "link_type" for d in result.definitions) == accepted
    assert any(d.api_name == "Amount" for d in result.definitions)
    assert bool(result.warnings_ja) != accepted
    if not accepted:
        assert "OrderCustomer / join_expression_sql" in result.warnings_ja[0]
        assert result.coverage[0].status == "insufficient_evidence"
        assert result.coverage[0].count == 0 and result.coverage[0].reason_ja
    assert len(value.definitions) == 2  # checkpoint の入力自体は書き換えない


def test_schema_and_document_links_are_not_filtered_as_qa() -> None:
    value = extraction()
    assert ground_qa_links(value, None, SCHEMA) is value


@pytest.mark.parametrize(
    "subquery,accepted",
    [
        ("SELECT 0 FROM DUAL", True),
        ("SELECT 0 FROM sys.dual", True),
        ("SELECT SYS.DUAL.DUMMY FROM SYS.DUAL", True),
        ('SELECT d.DUMMY FROM "SYS"."DUAL" d', True),
        ('SELECT 0 FROM "dual"', False),
        ('SELECT 0 FROM "sys".DUAL', False),
        ("SELECT 0 FROM OTHER.DUAL", False),
        ("SELECT 0 FROM APP.UNKNOWN_TABLE", False),
        ("SELECT UNKNOWN_COLUMN FROM DUAL", False),
        ("SELECT UNKNOWN_COLUMN FROM APP.ORDERS", False),
    ],
)
def test_qa_dual_does_not_relax_business_scope(subquery: str, accepted: bool) -> None:
    sql = (
        "SELECT o.ID FROM APP.ORDERS o JOIN APP.CUSTOMERS c ON o.CUSTOMER_ID=c.ID "
        f"WHERE o.ID > ({subquery})"
    )
    result = ground_qa_links(extraction(), [sql], SCHEMA)
    assert any(d.kind == "link_type" for d in result.definitions) == accepted
    assert bool(result.warnings_ja) != accepted


def test_dual_cte_and_profile_table_keep_their_own_columns() -> None:
    sql = (
        "WITH dual AS (SELECT CUSTOMER_ID FROM APP.ORDERS) "
        "SELECT d.CUSTOMER_ID FROM dual d JOIN APP.CUSTOMERS c ON d.CUSTOMER_ID=c.ID"
    )
    assert not ground_qa_links(extraction(), [sql], SCHEMA).warnings_ja
    schema = {
        "objects": [
            *SCHEMA["objects"],
            {"owner": "APP", "object_name": "DUAL", "columns": ["CUSTOMER_ID"]},
        ]
    }
    sql = (
        "SELECT d.CUSTOMER_ID FROM DUAL d JOIN APP.CUSTOMERS c ON d.CUSTOMER_ID=c.ID "
        "WHERE c.ID > (SELECT 0 FROM SYS.DUAL)"
    )
    value = extraction("APP.DUAL.CUSTOMER_ID = APP.CUSTOMERS.ID")
    assert not ground_qa_links(value, [sql], schema).warnings_ja
    # システム列を Profile の業務列へ読み替えない。
    result = ground_qa_links(extraction(), ["SELECT DUMMY FROM DUAL"], SCHEMA)
    assert not any(d.kind == "link_type" for d in result.definitions)


@pytest.mark.parametrize("same_id", [False, True])
def test_completed_link_keeps_original_evidence_aliases_and_conflicts(same_id: bool) -> None:
    base, addition = extraction(""), extraction()
    original, completed = base.definitions[0], addition.definitions[0]
    assert original.kind == completed.kind == "link_type"
    original.aliases = ["旧名称"]
    original.evidence = [
        DefinitionEvidence(source_id="qa", locator="question_sql", excerpt_ja="受注")
    ]
    completed.aliases = ["補完名称"]
    completed.cardinality = "one_to_many"
    if same_id:
        base.definitions[0].id = completed.id = "same-link"
        completed.api_name = "CompletedOrderCustomer"
    combined, _ = merge_build_extractions(base, addition)
    result = ground_qa_links(
        combined,
        ["SELECT o.ID FROM APP.ORDERS o JOIN APP.CUSTOMERS c ON o.CUSTOMER_ID=c.ID"],
        SCHEMA,
    )
    assert not result.warnings_ja
    merged, conflicts = merge_definitions("sales", result.definitions)
    link = next(d for d in merged if d.kind == "link_type")
    assert link.join_expression_sql == JOIN
    assert link.aliases == ["旧名称", "補完名称"]
    assert link.evidence[0].excerpt_ja == "受注"
    # unknown は未設定と同じ。補完で分かった基数を使い、競合にしない（#1720）。
    assert link.cardinality == "one_to_many"
    assert not conflicts
    assert original.join_expression_sql == ""  # checkpoint の原本は不変


@pytest.mark.parametrize("initial", ["", "APP.ORDERS.AMOUNT = APP.CUSTOMERS.ID"])
def test_unresolved_or_invalid_link_still_warns(initial: str) -> None:
    base, addition = extraction(initial), extraction()
    if not initial:
        addition.definitions[0].api_name = "OtherLink"
    combined, _ = merge_build_extractions(base, addition)
    result = ground_qa_links(
        combined,
        ["SELECT o.ID FROM APP.ORDERS o JOIN APP.CUSTOMERS c ON o.CUSTOMER_ID=c.ID"],
        SCHEMA,
    )
    assert any("OrderCustomer / join_expression_sql" in w for w in result.warnings_ja)


def test_differing_join_conditions_are_kept_as_separate_links() -> None:
    """同じ名前でも結合のキーが違うリンク型は別の関係として残す（併合して競合にしない。#1729）。"""

    combined, _ = merge_build_extractions(extraction(), extraction(JOIN.replace("=", "<>")))
    result = ground_qa_links(
        combined,
        ["SELECT o.ID FROM APP.ORDERS o JOIN APP.CUSTOMERS c ON o.CUSTOMER_ID=c.ID"],
        SCHEMA,
    )
    assert not result.warnings_ja
    merged, conflicts = merge_definitions("sales", result.definitions)
    links = [d for d in merged if d.kind == "link_type"]
    assert sorted(d.join_expression_sql for d in links) == sorted([JOIN, JOIN.replace("=", "<>")])
    assert len({d.api_name for d in links}) == 2
    assert not conflicts


@pytest.mark.parametrize("glean", [False, True])
def test_worker_keeps_dual_join_and_drops_repaired_warning(
    glean: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_extraction_gleaning_passes", int(glean))
    rt, legacy = runtime()

    class Client(_FakeEnterpriseAiClient):
        def generate(
            self, *, prompt: str, context: str, system_prompt: str, response_format: Any = None
        ) -> str:
            self.calls.append(prompt)
            definitions = definition_payload()[:3]
            if not glean or "【追加パス" in prompt:
                definitions[2]["join_expression_sql"] = "APP.ORDERS.CUSTOMER_ID = APP.ORDERS.ID"
            return json.dumps({"definitions": definitions})

    client = Client("")
    legacy._enterprise_ai_client = client
    service = OntologyBuildService(rt)
    job = _wait_for_job(
        service,
        service.start(
            "sales",
            run_schema_naming=False,
            qa_pairs=[
                QaPair(
                    question="関連受注",
                    sql=(
                        "SELECT o.ID FROM APP.ORDERS o JOIN APP.ORDERS p ON o.CUSTOMER_ID=p.ID "
                        "WHERE o.ID > (SELECT 0 FROM DUAL)"
                    ),
                )
            ],
        ).id,
    )
    assert job.status == "succeeded", job.error_message_ja
    assert len(client.calls) == 1 + int(glean)
    bundle = ProfileOntologyDefinitionService(rt).get("sales", job.result_bundle_id)
    link = next(d for d in bundle.definitions if d.kind == "link_type")
    assert link.join_expression_sql == "APP.ORDERS.CUSTOMER_ID = APP.ORDERS.ID"
    assert not job.warnings_ja
    assert "related / join_expression_sql を採用しません" not in job.markdown_output
    coverage = next(c for c in bundle.coverage if c.kind == "link_type")
    assert coverage.count == 1


@pytest.mark.parametrize("mixed", [False, True])
def test_worker_excludes_only_unsupported_qa_link_before_merge(
    mixed: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_extraction_gleaning_passes", 0)
    rt, legacy = runtime()
    # Q/A にない関係を返す。同じ関係がスキーマ抽出にもあれば、その独立した結果は保持する。
    legacy._enterprise_ai_client = _FakeEnterpriseAiClient(extraction().model_dump_json())
    service = OntologyBuildService(rt)
    job = _wait_for_job(
        service,
        service.start(
            "sales",
            run_schema_naming=mixed,
            qa_pairs=[QaPair(question="受注ID", sql="SELECT ID FROM APP.ORDERS")],
        ).id,
    )
    assert job.status == "succeeded"
    bundle = ProfileOntologyDefinitionService(rt).get("sales", job.result_bundle_id)
    assert any(d.kind == "link_type" for d in bundle.definitions) == mixed
    coverage = next(c for c in bundle.coverage if c.kind == "link_type")
    assert coverage.count == int(mixed)
    if not mixed:
        assert coverage.status == "insufficient_evidence" and coverage.reason_ja
    assert any(d.api_name == "Amount" for d in bundle.definitions)
    assert any("OrderCustomer / join_expression_sql" in w for w in job.warnings_ja)
    if not mixed:
        assert "##### 受注顧客" not in job.markdown_output
    checkpoints = [
        json.loads(a["content"])
        for a in rt.store.list_artifacts(session_id="profile-ontology:sales")
        if a["artifact_type"] == "ontology_build_checkpoint"
    ]
    assert checkpoints and all(
        any(d["kind"] == "link_type" for d in c["definitions"]) for c in checkpoints
    )
