"""SQL から質問を生成(deep モード)の処理手順が UI に届くことの回帰テスト。

UI(`LogicalStepsList`)は `logical_step_details` を優先して描画するため、LLM の
`logical_steps` を文字列だけ差し替えても表示されなかった(Issue: deep モードで LLM の
処理手順が表示されない)。details にも写し、件数上限を設ける。
"""

from __future__ import annotations

import json

from app.features.nl2sql.models import (
    ReverseSqlRequest,
    SchemaCatalog,
    SchemaColumn,
    SchemaTable,
)
from app.features.nl2sql.service import (
    _REVERSE_DEEP_MAX_STEPS,
    Nl2SqlService,
    _reverse_deep_step_details,
    _reverse_deep_steps,
)
from app.features.nl2sql.store import MemoryNl2SqlStore

_SQL = "SELECT DEPARTMENT_ID, SUM(SALARY) FROM APP.EMPLOYEE GROUP BY DEPARTMENT_ID"


class _FakeEnterpriseAiClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def is_configured(self) -> bool:
        return True

    def model_id(self) -> str:
        return "enterprise-nl2sql-model"

    def generate(
        self,
        *,
        prompt: str,
        context: str,
        system_prompt: str,
        timeout_seconds: float | None = None,
        max_output_tokens: int | None = None,
        max_retries: int | None = None,
        response_format: object | None = None,
    ) -> str:
        del prompt, context, system_prompt, timeout_seconds, max_output_tokens, max_retries
        return json.dumps(
            {"logical_structure": "SQL 論理構造\n- SELECT: 給与", **self.payload},
            ensure_ascii=False,
        )


def _service(payload: dict[str, object]) -> Nl2SqlService:
    service = Nl2SqlService(store=MemoryNl2SqlStore())
    service._catalog = SchemaCatalog(  # noqa: SLF001 - white-box contract test
        refreshed_at="2026-09-02T00:00:00+00:00",
        current_owner="APP",
        tables=[
            SchemaTable(
                owner="APP",
                table_name="EMPLOYEE",
                logical_name="社員",
                columns=[
                    SchemaColumn(
                        column_name="EMPLOYEE_ID",
                        logical_name="社員 ID",
                        data_type="NUMBER",
                        nullable=False,
                    ),
                    SchemaColumn(
                        column_name="DEPARTMENT_ID", logical_name="部署 ID", data_type="NUMBER"
                    ),
                    SchemaColumn(column_name="SALARY", logical_name="給与", data_type="NUMBER"),
                    SchemaColumn(column_name="HIRED_AT", logical_name="入社日", data_type="DATE"),
                ],
            )
        ],
    )
    service._enterprise_ai_client = _FakeEnterpriseAiClient(payload)  # noqa: SLF001
    return service


def test_deep_steps_are_projected_into_details_when_counts_differ() -> None:
    service = _service(
        {
            "question": "部署ごとの給与合計を知りたい",
            "explanation": "部署単位で給与を集計します。",
            "logical_steps": ["社員を部署ごとにまとめる", "給与を合計する"],
        }
    )

    data = service.reverse_sql_deep(ReverseSqlRequest(sql=_SQL, profile_id=None))

    assert data.source == "oci_enterprise_ai"
    assert data.question == "部署ごとの給与合計を知りたい"
    assert data.logical_steps == ["社員を部署ごとにまとめる", "給与を合計する"]
    # UI が優先する details にも LLM の手順が業務行として入る。
    assert [step.business for step in data.logical_step_details] == data.logical_steps
    assert all(step.kind == "llm" and step.technical == "" for step in data.logical_step_details)
    # AI の構造を決定論の部分要約で隠さない。
    assert not data.logical_structure_items
    assert "給与" in data.logical_structure


def test_deep_steps_keep_technical_rows_when_counts_align() -> None:
    deterministic = _service({}).reverse_sql(ReverseSqlRequest(sql=_SQL, profile_id=None))
    assert deterministic.logical_step_details
    llm_steps = [
        f"LLM 手順 {index + 1}" for index in range(len(deterministic.logical_step_details))
    ]
    service = _service({"question": "q", "logical_steps": llm_steps})

    data = service.reverse_sql_deep(ReverseSqlRequest(sql=_SQL, profile_id=None))

    assert [step.business for step in data.logical_step_details] == llm_steps
    assert [step.technical for step in data.logical_step_details] == [
        step.technical for step in deterministic.logical_step_details
    ]
    assert [step.kind for step in data.logical_step_details] == [
        step.kind for step in deterministic.logical_step_details
    ]


def test_deep_without_steps_keeps_deterministic_details() -> None:
    deterministic = _service({}).reverse_sql(ReverseSqlRequest(sql=_SQL, profile_id=None))
    service = _service({"question": "q のみ", "logical_steps": []})

    data = service.reverse_sql_deep(ReverseSqlRequest(sql=_SQL, profile_id=None))

    assert data.question == "q のみ"
    assert data.logical_steps == deterministic.logical_steps
    assert data.logical_step_details == deterministic.logical_step_details


def test_deep_steps_are_capped_and_blank_items_dropped() -> None:
    raw = ["", "  ", *[f"step {index}" for index in range(_REVERSE_DEEP_MAX_STEPS + 15)]]

    steps = _reverse_deep_steps(raw)

    assert len(steps) == _REVERSE_DEEP_MAX_STEPS
    assert steps[0] == "step 0"
    assert _reverse_deep_steps("not a list") == []
    assert _reverse_deep_step_details([], []) == []
