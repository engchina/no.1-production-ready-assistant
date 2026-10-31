"""三段階の情報伝達、構造からの再生成と安全・認可境界を検証する。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import HTTPException, Request
from test_nl2sql_reverse_access import _principal, _request, _service

from app.features.nl2sql import router
from app.features.nl2sql.enterprise_ai_client import EnterpriseAiDirectError
from app.features.nl2sql.models import ReverseSqlRequest, StructureToSqlRequest
from app.features.nl2sql.reverse_prompts import (
    BUSINESS_QUESTION_CORRECTIONS,
    LOGICAL_STRUCTURE_CORRECTIONS,
    PHYSICAL_STRUCTURE_CORRECTIONS,
    source_prompt,
)


class StagedClient:
    def __init__(self, outputs: list[Any], *, configured: bool = True):
        self.outputs = iter(outputs)
        self.calls: list[dict[str, Any]] = []
        self.configured = configured

    def is_configured(self) -> bool:
        return self.configured

    def generate(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        return json.dumps(output, ensure_ascii=False)


def test_sql_assist_source_prompts_match_pinned_manifest() -> None:
    root = Path(__file__).parents[1] / "app/features/nl2sql/prompts/sql_assist"
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["revision"] == "cacd0959a5fa2a279cb1147de2d04e5a9b01815f"
    for filename, digest in manifest["files"].items():
        assert hashlib.sha256((root / filename).read_bytes()).hexdigest() == digest
        assert source_prompt(filename.removesuffix(".txt"))


def test_three_stages_pass_complete_outputs_and_regeneration_uses_only_edited_structure() -> None:
    service = _service()
    sql = (
        "SELECT ID, SUM(AMOUNT) OVER (ORDER BY ID ROWS UNBOUNDED PRECEDING) AS TOTAL "
        "FROM APP.INVOICES WHERE NAME LIKE 'A_%' AND (AMOUNT >= 100 OR AMOUNT IS NULL) "
        "ORDER BY ID DESC NULLS LAST OFFSET 2 ROWS FETCH NEXT 5 ROWS ONLY"
    )
    physical = "全物理構造: " + sql
    logical = "全論理構造: " + sql
    edited = logical.replace("AMOUNT >= 100", "AMOUNT >= 200")
    regenerated = sql.replace("AMOUNT >= 100", "AMOUNT >= 200")
    client = StagedClient(
        [
            {"logical_structure": physical},
            {"logical_structure": logical},
            {
                "question": "全条件を満たす請求情報を取得したい",
                "logical_steps": ["請求情報を絞り込む"],
            },
            {"sql": regenerated, "explanation": "閾値を200に変更"},
        ]
    )
    cast(Any, service)._enterprise_ai_client = client
    data = service.reverse_sql_deep(ReverseSqlRequest(sql=sql, profile_id="finance"))
    assert data.logical_structure == logical
    assert data.sql_structure == physical
    assert data.logical_structure_items == []
    assert client.calls[0]["prompt"] == sql
    assert json.loads(client.calls[1]["prompt"]) == {"sql_structure": physical, "sql": sql}
    assert client.calls[2]["prompt"] == logical
    assert all("schema:" in call["context"] for call in client.calls)
    result = service.structure_to_sql(
        StructureToSqlRequest(logical_structure=edited, profile_id="finance")
    )
    assert result.sql == regenerated
    assert client.calls[3]["prompt"] == edited
    assert "LIKE は前方・後方・部分一致" in client.calls[3]["system_prompt"]
    assert "AMOUNT >= 100" not in client.calls[3]["context"]
    assert result.source == "oci_enterprise_ai"


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM APP.INVOICES",
        "SELECT ID FROM APP.INVOICES; DELETE FROM APP.INVOICES",
        "SELECT ID FROM APP.ORDERS",
        "SELECT UTL_HTTP.REQUEST('https://example.com') FROM APP.INVOICES",
        "not sql",
    ],
)
def test_regeneration_rejects_unsafe_or_out_of_profile_sql(sql: str) -> None:
    service = _service()
    cast(Any, service)._enterprise_ai_client = StagedClient([{"sql": sql}])
    with pytest.raises(ValueError):
        service.structure_to_sql(
            StructureToSqlRequest(logical_structure="請求一覧", profile_id="finance")
        )


@pytest.mark.parametrize("output", [{}, {"sql": []}, {"sql": " "}, {"sql": ""}])
def test_regeneration_rejects_invalid_response(output: dict[str, Any]) -> None:
    service = _service()
    cast(Any, service)._enterprise_ai_client = StagedClient([output])
    with pytest.raises((ValueError, EnterpriseAiDirectError)):
        service.structure_to_sql(
            StructureToSqlRequest(logical_structure="請求一覧", profile_id="finance")
        )


def test_unconfigured_and_empty_input_never_fabricate_regenerated_sql() -> None:
    service = _service()
    client = StagedClient([], configured=False)
    cast(Any, service)._enterprise_ai_client = client
    for structure in [" ", "請求一覧"]:
        with pytest.raises(ValueError):
            service.structure_to_sql(
                StructureToSqlRequest(logical_structure=structure, profile_id="finance")
            )
    assert client.calls == []


@pytest.mark.parametrize("fail_at", [0, 1, 2])
def test_failed_stage_preserves_completed_structure_or_exact_original_sql(fail_at: int) -> None:
    service = _service()
    sql = "WITH x AS (SELECT ID, 'INVOICES;漢字' AS LABEL FROM APP.INVOICES) SELECT * FROM x"
    outputs: list[Any] = [{"logical_structure": "physical"}, {"logical_structure": "logical"}]
    outputs[fail_at:] = [EnterpriseAiDirectError("unavailable")]
    cast(Any, service)._enterprise_ai_client = StagedClient(outputs)
    result = service.reverse_sql_deep(ReverseSqlRequest(sql=sql, profile_id="finance"))
    assert result.warnings
    assert result.sql_structure == ("physical" if fail_at > 0 else "")
    assert (
        (result.logical_structure == "logical")
        if fail_at == 2
        else (sql in result.logical_structure)
    )


@pytest.mark.parametrize(
    "physical",
    [
        "## 📊 SQL構造分析\n\n### 📋 SELECT句\n- *\n\n### 📁 FROM句\n- employee",
        '## SQL構造分析\n\n### SELECT句\n- \'{"key":"value"}\' AS "Label"'
        '\n\n### FROM句\n- "employee"',
    ],
)
def test_sql_assist_markdown_response_is_preserved_without_business_name_substitution(
    physical: str,
) -> None:
    service = _service()
    logical = physical.replace("SQL構造分析", "SQL論理構造").replace("employee", "従業員情報")

    class MarkdownClient(StagedClient):
        def generate(self, **kwargs: Any) -> str:
            if len(self.calls) < 2:
                output = physical if not self.calls else logical
                self.calls.append(kwargs)
                return json.dumps({"logical_structure": output}, ensure_ascii=False)
            return super().generate(**kwargs)

    client = MarkdownClient([{"question": "従業員情報を取得したい"}])
    cast(Any, service)._enterprise_ai_client = client
    result = service.reverse_sql_deep(
        ReverseSqlRequest(sql="select * from employee", profile_id="finance")
    )
    assert result.sql_structure == physical
    assert result.logical_structure == logical
    assert result.logical_structure_items == []
    assert result.source == "oci_enterprise_ai"
    assert not result.warnings
    assert client.calls[2]["prompt"] == logical


@pytest.mark.parametrize(
    "raw",
    ["", "not JSON", "## SQL構造分析\n情報を抽出できませんでした。", '{"logical_structure": []}'],
)
def test_invalid_analysis_response_remains_a_visible_fallback(raw: str) -> None:
    service = _service()

    class InvalidClient(StagedClient):
        def generate(self, **kwargs: Any) -> str:
            return raw

    cast(Any, service)._enterprise_ai_client = InvalidClient([])
    result = service.reverse_sql_deep(
        ReverseSqlRequest(sql="select * from employee", profile_id="finance")
    )
    assert result.sql_structure == ""
    assert "select * from employee" in result.logical_structure
    assert result.warnings
    assert result.source == "deterministic"


def test_structure_endpoint_requires_profile_access_before_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    client = StagedClient([{"sql": "SELECT ID FROM APP.INVOICES"}])
    cast(Any, service)._enterprise_ai_client = client
    monkeypatch.setattr(router, "nl2sql_service", service)
    req = StructureToSqlRequest(logical_structure="請求一覧", profile_id="finance")
    with pytest.raises(HTTPException) as exc:
        router.structure_to_sql(req, cast(Request, _request(_principal({"sales"}))))
    assert exc.value.status_code == 403
    assert client.calls == []
    result = router.structure_to_sql(req, cast(Request, _request(_principal({"finance"}))))
    assert result.data is not None
    assert result.data.sql == "SELECT ID FROM APP.INVOICES"


@pytest.mark.parametrize("use_glossary", [False, True])
def test_original_glossary_prompt_is_applied_only_when_requested(use_glossary: bool) -> None:
    service = _service()
    client = StagedClient(
        [
            {"logical_structure": "physical"},
            {"logical_structure": "logical"},
            {"question": "請求一覧"},
        ]
    )
    cast(Any, service)._enterprise_ai_client = client
    result = service.reverse_sql_deep(
        ReverseSqlRequest(
            sql="SELECT ID FROM APP.INVOICES", profile_id="finance", use_glossary=use_glossary
        )
    )
    assert result.source == "oci_enterprise_ai"
    assert ("本タスクでは逆最適化を行います" in client.calls[2]["system_prompt"]) == use_glossary
    assert ("- 請求: INVOICES" in client.calls[2]["context"]) == use_glossary


def test_schema_business_names_are_used_by_default_without_glossary() -> None:
    service = _service()
    physical = "## SQL構造分析\n### SELECT句\n- *\n### FROM句\n- APP.INVOICES i"
    logical = "## SQL論理構造\n### SELECT句\n- *\n### FROM句\n- [請求] i"
    client = StagedClient(
        [
            {"logical_structure": physical},
            {"logical_structure": logical},
            {"question": "すべての請求情報を教えてください。"},
            {"sql": "SELECT * FROM APP.INVOICES i"},
        ]
    )
    cast(Any, service)._enterprise_ai_client = client
    request = ReverseSqlRequest(sql="SELECT * FROM APP.INVOICES i", profile_id="finance")
    assert not request.use_glossary
    result = service.reverse_sql_deep(request)
    assert result.logical_structure == logical
    assert result.sql_structure == physical
    assert result.question == "すべての請求情報を教えてください。"
    assert client.calls[2]["prompt"] == logical
    for call in client.calls:
        assert "table APP.INVOICES logical=請求" in call["context"]
        assert "column NAME logical=名称" in call["context"]
        assert "comment=" in call["context"]
        assert "- 請求: INVOICES" not in call["context"]
        assert "本タスクでは逆最適化を行います" not in call["system_prompt"]
    assert PHYSICAL_STRUCTURE_CORRECTIONS in client.calls[0]["system_prompt"]
    assert PHYSICAL_STRUCTURE_CORRECTIONS not in client.calls[1]["system_prompt"]
    assert PHYSICAL_STRUCTURE_CORRECTIONS not in client.calls[2]["system_prompt"]
    assert LOGICAL_STRUCTURE_CORRECTIONS in client.calls[1]["system_prompt"]
    assert BUSINESS_QUESTION_CORRECTIONS in client.calls[2]["system_prompt"]
    regeneration = StructureToSqlRequest(logical_structure=logical, profile_id="finance")
    assert not regeneration.use_glossary
    service.structure_to_sql(regeneration)
    assert client.calls[3]["prompt"] == logical
    assert "table APP.INVOICES logical=請求" in client.calls[3]["context"]
    assert "- 請求: INVOICES" not in client.calls[3]["context"]


def test_structure_endpoint_handles_provider_failure_and_retains_menu_permission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.security.permissions import permission_for_route

    service = _service()
    cast(Any, service)._enterprise_ai_client = StagedClient(
        [EnterpriseAiDirectError("provider unavailable")]
    )
    monkeypatch.setattr(router, "nl2sql_service", service)
    with pytest.raises(HTTPException) as exc:
        router.structure_to_sql(
            StructureToSqlRequest(logical_structure="請求一覧", profile_id="finance"),
            cast(Request, _request(_principal({"finance"}))),
        )
    assert exc.value.status_code == 502
    assert "再試行" in exc.value.detail
    assert permission_for_route("POST", "/nl2sql/reverse/sql") == frozenset(
        {"menu.sql_to_question"}
    )


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("sql", ["select * from INVOICES", "select * from APP.INVOICES"])
def test_unchanged_simple_structure_restores_exact_sql_without_llm(
    configured: bool, sql: str
) -> None:
    service = _service()
    client = StagedClient([], configured=configured)
    cast(Any, service)._enterprise_ai_client = client
    structure = service.reverse_sql(ReverseSqlRequest(sql=sql, profile_id="finance"))
    result = service.structure_to_sql(
        StructureToSqlRequest(logical_structure=structure.logical_structure, profile_id="finance")
    )
    assert result.sql == sql
    assert result.source == "preserved_original"
    assert "元 SQL" in result.explanation
    assert client.calls == []


def test_empty_regeneration_explains_missing_information_without_validation_internals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _service()
    cast(Any, service)._enterprise_ai_client = StagedClient(
        [{"sql": "", "explanation": "対象の列名を論理構造に追加してください。"}]
    )
    monkeypatch.setattr(router, "nl2sql_service", service)
    with pytest.raises(HTTPException) as error:
        router.structure_to_sql(
            StructureToSqlRequest(logical_structure="請求一覧", profile_id="finance"),
            cast(Request, _request(_principal({"finance"}))),
        )
    assert error.value.status_code == 400
    assert error.value.detail == "対象の列名を論理構造に追加してください。"


@pytest.mark.parametrize("use_glossary", [True, False])
def test_edited_simple_structure_never_silently_restores_old_sql(use_glossary: bool) -> None:
    service = _service()
    original = "SELECT ID FROM APP.INVOICES WHERE ID > 1"
    updated = original.replace("ID > 1", "ID > 2")
    structure = service.reverse_sql(
        ReverseSqlRequest(sql=original, profile_id="finance", use_glossary=use_glossary)
    ).logical_structure
    structure += "\n追加要件: ID は 2 より大きいものに変更"
    client = StagedClient([{"sql": updated}])
    cast(Any, service)._enterprise_ai_client = client
    result = service.structure_to_sql(
        StructureToSqlRequest(
            logical_structure=structure, profile_id="finance", use_glossary=use_glossary
        )
    )
    assert result.sql == updated
    assert result.source == "oci_enterprise_ai"
    assert client.calls[0]["prompt"] == structure


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM APP.ORDERS",
        "DELETE FROM APP.INVOICES",
        "SELECT * FROM APP.INVOICES; DELETE FROM APP.INVOICES",
    ],
)
def test_embedded_original_sql_cannot_bypass_profile_or_safety(sql: str) -> None:
    service = _service()
    structure = service.reverse_sql(
        ReverseSqlRequest(sql=sql, profile_id="finance")
    ).logical_structure
    client = StagedClient([])
    cast(Any, service)._enterprise_ai_client = client
    with pytest.raises(ValueError):
        service.structure_to_sql(
            StructureToSqlRequest(logical_structure=structure, profile_id="finance")
        )
    assert client.calls == []


@pytest.mark.parametrize("output", [{}, {"sql": []}, {"sql": None}, "not JSON"])
def test_malformed_generation_is_readable_provider_error(
    output: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service()
    cast(Any, service)._enterprise_ai_client = StagedClient([output])
    monkeypatch.setattr(router, "nl2sql_service", service)
    with pytest.raises(HTTPException) as error:
        router.structure_to_sql(
            StructureToSqlRequest(logical_structure="請求一覧", profile_id="finance"),
            cast(Request, _request(_principal({"finance"}))),
        )
    assert error.value.status_code == 502
    assert "再試行" in error.value.detail
    assert "validation" not in error.value.detail
    assert "pydantic" not in error.value.detail
