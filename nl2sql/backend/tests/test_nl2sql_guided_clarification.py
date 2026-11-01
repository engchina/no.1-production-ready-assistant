"""回答前の確認（SQL の要件の下書き）の回帰テスト（#1439）。

LLM は決定論のスタブ。表のデータを LLM に渡さない・範囲の外と実在しない名前・未承認の結合・根拠の
無い候補を拒む・相対的な期間を具体的な日付で確かめる・回答の後の再検査・上限・SELECT / FROM の
最低の基準・物理名・危険な問い合わせの確認・SQL の骨組みを確かめる。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import date, timedelta
from typing import Any

import httpx
import pytest

import app.features.nl2sql.ontology_router as ontology_router_module
from app.features.mcp.tools import MCP_OUTPUT_SCHEMA_VERSION
from app.features.nl2sql.models import (
    AllowedObjects,
    Nl2SqlProfile,
    SchemaCatalog,
    SchemaColumn,
    SchemaConstraintDetail,
    SchemaTable,
)
from app.features.nl2sql.ontology_models import (
    ClarificationMode,
    QuerySessionStatus,
    RequirementItem,
    RequirementPhase,
    RequirementSection,
    RequirementState,
    RequirementStatus,
    SqlRequirementDraft,
)
from app.features.nl2sql.ontology_router import (
    ClarificationAnswerRequest,
    GenerateSqlRequest,
    OntologyApiRuntime,
    QuerySessionApiCreate,
    QuerySessionData,
    RequirementApplyRequest,
)
from app.features.nl2sql.ontology_service import (
    OntologyGateBlockedError,
    OntologyIntegrityError,
    OntologyStateConflictError,
    OntologyVersionConflictError,
)
from app.features.nl2sql.ontology_store import (
    IDEMPOTENCY_KEY_STORAGE_MAX_BYTES,
    InMemoryOntologyStore,
    OntologyCollection,
)
from app.features.nl2sql.sql_requirement import (
    LlmRequirementOutput,
    RequirementAiUnavailableError,
    build_draft,
    build_requirement_context,
    from_tables,
    llm_text,
    period_expression_from_question,
    period_ranges,
    relative_period_from_expression,
    requirement_response_format,
)
from app.main import app
from app.security.dependencies import local_debug_principal
from app.settings import Settings, get_settings

TODAY = date(2026, 10, 11)


@pytest.fixture(autouse=True)
def _disable_profile_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_ontology_profile_confirmation_required", False)
    monkeypatch.setattr(get_settings(), "nl2sql_requirement_max_review_rounds", 3)


def _catalog() -> SchemaCatalog:
    return SchemaCatalog(
        refreshed_at="2026-10-11T00:00:00Z",
        schema_fingerprint="requirement-schema",
        tables=[
            SchemaTable(
                table_name="ORDERS",
                owner="APP",
                logical_name="受注",
                comment="受注の明細",
                columns=[
                    SchemaColumn(
                        column_name="ORDER_ID",
                        logical_name="受注ID",
                        data_type="NUMBER",
                        nullable=False,
                        comment="受注を識別する主キー",
                        sample_values=["1001"],
                    ),
                    SchemaColumn(
                        column_name="CUSTOMER_ID",
                        logical_name="顧客ID",
                        data_type="NUMBER",
                        comment="顧客",
                    ),
                    SchemaColumn(
                        column_name="ORDER_DATE",
                        logical_name="受注日",
                        data_type="DATE",
                        comment="受注した日",
                    ),
                    SchemaColumn(
                        column_name="SHIP_DATE",
                        logical_name="出荷日",
                        data_type="DATE",
                        comment="出荷した日",
                    ),
                    SchemaColumn(
                        column_name="STATUS",
                        logical_name="受注状態",
                        data_type="VARCHAR2",
                        comment="受注の状態（SHIPPED: 出荷済み、CANCELLED: 取消）",
                        sample_values=["SECRET_SAMPLE_VALUE"],
                    ),
                    SchemaColumn(
                        column_name="AMOUNT",
                        logical_name="受注金額",
                        data_type="NUMBER",
                        comment="税込の金額（円）",
                    ),
                ],
                constraint_details=[
                    SchemaConstraintDetail(
                        constraint_name="PK_ORDERS",
                        constraint_type="P",
                        owner="APP",
                        table_name="ORDERS",
                        columns=["ORDER_ID"],
                    ),
                    SchemaConstraintDetail(
                        constraint_name="FK_ORDERS_CUSTOMER",
                        constraint_type="R",
                        owner="APP",
                        table_name="ORDERS",
                        columns=["CUSTOMER_ID"],
                        referenced_owner="APP",
                        referenced_table="CUSTOMERS",
                        referenced_columns=["CUSTOMER_ID"],
                    ),
                ],
            ),
            SchemaTable(
                table_name="CUSTOMERS",
                owner="APP",
                logical_name="顧客",
                comment="顧客の台帳",
                columns=[
                    SchemaColumn(
                        column_name="CUSTOMER_ID",
                        logical_name="顧客ID",
                        data_type="NUMBER",
                        nullable=False,
                        comment="顧客の主キー",
                    ),
                    SchemaColumn(
                        column_name="REGION",
                        logical_name="地域",
                        data_type="VARCHAR2",
                        comment="顧客の地域",
                    ),
                ],
                constraint_details=[
                    SchemaConstraintDetail(
                        constraint_name="PK_CUSTOMERS",
                        constraint_type="P",
                        owner="APP",
                        table_name="CUSTOMERS",
                        columns=["CUSTOMER_ID"],
                    )
                ],
            ),
            SchemaTable(
                table_name="SECRET_TABLE",
                owner="APP",
                logical_name="秘密",
                comment="業務プロファイルの範囲の外",
                columns=[
                    SchemaColumn(column_name="ID", logical_name="ID", data_type="NUMBER"),
                ],
            ),
        ],
    )


class _LegacyService:
    uses_incremental_store = False

    def __init__(self, *, glossary: dict[str, str] | None = None) -> None:
        self.profile = Nl2SqlProfile(
            id="sales",
            name="販売分析",
            description="受注と顧客を分析する",
            allowed_tables=["APP.ORDERS", "APP.CUSTOMERS"],
            glossary=glossary or {"売上": "受注金額（AMOUNT）の合計"},
            sql_rules=["取消（STATUS = 'CANCELLED'）を除く"],
            default_row_limit=100,
        )
        self.catalog = _catalog()
        self._enterprise_ai_client: Any = None

    def get_catalog(self) -> SchemaCatalog:
        return self.catalog.model_copy(deep=True)

    def get_profile(self, profile_id: str, **_kwargs: Any) -> Nl2SqlProfile:
        if profile_id != self.profile.id:
            raise ValueError("profile not found")
        return self.profile.model_copy(deep=True)

    def resolve_allowed_objects(self, profile_id: str, requested: AllowedObjects) -> AllowedObjects:
        self.get_profile(profile_id)
        if requested.table_names:
            return requested.model_copy(deep=True)
        return AllowedObjects(
            table_names=list(self.profile.allowed_tables), enforce_table_scope=True
        )


class _ScriptedLlm:
    """呼ばれた順に応答を返すスタブ。応答は context（dict）から作る関数でもよい。"""

    def __init__(self, *responses: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]):
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def is_configured(self) -> bool:
        return True

    def model_id(self) -> str:
        return "stub-requirement-llm"

    def generate(self, *, prompt: str, context: str, system_prompt: str, **kwargs: Any) -> str:
        payload = json.loads(context)
        self.calls.append(
            {"prompt": prompt, "context": payload, "raw_context": context, "system": system_prompt}
        )
        if not self.responses:
            raise AssertionError("LLM がスクリプトより多く呼ばれた")
        response = self.responses.pop(0)
        value = response(payload) if callable(response) else response
        return json.dumps(value, ensure_ascii=False)


def _item(
    key: str,
    section: str,
    status: str,
    *,
    label: str = "",
    value: str = "",
    tables: list[str] | None = None,
    columns: list[str] | None = None,
    relationship_ids: list[str] | None = None,
    sql_hint: str = "",
    question: str = "",
    candidates: list[dict[str, Any]] | None = None,
    period: dict[str, Any] | None = None,
    answer_kind: str = "single_select",
) -> dict[str, Any]:
    return {
        "key": key,
        "section": section,
        "label_ja": label or key,
        "value_ja": value,
        "status": status,
        "reason_ja": "",
        "question_ja": question,
        "answer_kind": answer_kind,
        "source": "question",
        "tables": tables or [],
        "columns": columns or [],
        "relationship_ids": relationship_ids or [],
        "sql_hint": sql_hint,
        "period": period,
        "candidates": candidates or [],
    }


def _candidate(
    label: str,
    *,
    evidence: str = "列のコメント",
    tables: list[str] | None = None,
    columns: list[str] | None = None,
    sql_hint: str = "",
    source: str = "schema",
) -> dict[str, Any]:
    return {
        "label_ja": label,
        "description_ja": "",
        "evidence_ja": evidence,
        "source": source,
        "tables": tables or [],
        "columns": columns or [],
        "relationship_ids": [],
        "sql_hint": sql_hint,
    }


def _output(
    *items: dict[str, Any],
    clarified: str = "",
    unanswerable: str = "",
    answerable: bool = True,
) -> dict[str, Any]:
    return {
        "clarified_question_ja": clarified,
        "items": list(items),
        "answerable": answerable,
        "unanswerable_reason_ja": unanswerable,
    }


def _from_orders(status: str = "confirmed") -> dict[str, Any]:
    return _item(
        "from_orders", "from", status, label="使う表", value="APP.ORDERS", tables=["APP.ORDERS"]
    )


def _select_total(status: str = "confirmed") -> dict[str, Any]:
    return _item(
        "select_total",
        "select",
        status,
        label="受注金額の合計",
        value="SUM(ORDERS.AMOUNT)",
        columns=["APP.ORDERS.AMOUNT"],
        sql_hint="SUM(ORDERS.AMOUNT)",
    )


def _where_shipped() -> dict[str, Any]:
    return _item(
        "where_status",
        "where",
        "confirmed",
        label="受注状態",
        value="出荷済み",
        columns=["APP.ORDERS.STATUS"],
        sql_hint="ORDERS.STATUS = 'SHIPPED'",
    )


def _runtime(llm: _ScriptedLlm, *, glossary: dict[str, str] | None = None) -> OntologyApiRuntime:
    legacy = _LegacyService(glossary=glossary)
    legacy._enterprise_ai_client = llm
    runtime = OntologyApiRuntime(legacy_service=legacy, store=InMemoryOntologyStore())
    runtime._requirement_today = lambda: TODAY  # type: ignore[method-assign]
    return runtime


def _create(
    runtime: OntologyApiRuntime,
    question: str = "出荷済みの受注金額の合計",
    *,
    actor_user_uuid: str = "user-1",
) -> QuerySessionData:
    return runtime.create_session(
        QuerySessionApiCreate(
            question=question,
            profile_id="sales",
            clarification_mode=ClarificationMode.GUIDED,
        ),
        actor_user_uuid=actor_user_uuid,
    )


def _draft(data: QuerySessionData) -> SqlRequirementDraft:
    assert data.session.requirement is not None
    return data.session.requirement


def _items(data: QuerySessionData) -> list[RequirementItem]:
    return _draft(data).items


def _state(data: QuerySessionData) -> RequirementState:
    assert data.requirement_state is not None
    return data.requirement_state


def _answer(
    runtime: OntologyApiRuntime,
    data: QuerySessionData,
    *,
    item_id: str | None = None,
    candidate_index: int | None = 0,
    free_text: str = "",
) -> QuerySessionData:
    state = data.requirement_state
    assert state is not None
    item = (
        next(value for value in _items(data) if value.id == item_id)
        if item_id
        else state.current_item
    )
    assert item is not None
    selected = (
        [item.candidates[candidate_index].id]
        if candidate_index is not None and not free_text and item.candidates
        else []
    )
    return runtime.answer_clarification(
        data.session.id,
        ClarificationAnswerRequest(
            base_version=data.session.current_intent_version,
            item_id=item.id,
            selected_candidate_ids=selected,
            free_text=free_text,
        ),
    )


# --- 1. 表のデータを LLM の入力に入れない ---------------------------------------------


def test_llm_input_uses_schema_without_table_data() -> None:
    llm = _ScriptedLlm(_output(_from_orders(), _select_total(), _where_shipped()))
    runtime = _runtime(llm)

    created = _create(runtime)

    assert len(llm.calls) == 1
    raw = llm.calls[0]["raw_context"]
    assert "SECRET_SAMPLE_VALUE" not in raw
    assert "1001" not in raw
    assert "sample_values" not in raw
    assert "row_count" not in raw
    context = llm.calls[0]["context"]
    tables = {item["table"] for item in context["schema"]}
    # 業務プロファイルの範囲の表だけ（範囲の外の表は名前も渡さない）。
    assert tables == {"APP.ORDERS", "APP.CUSTOMERS"}
    columns = {column["name"] for item in context["schema"] for column in item["columns"]}
    assert "APP.ORDERS.ORDER_DATE" in columns
    orders = next(item for item in context["schema"] if item["table"] == "APP.ORDERS")
    assert orders["primary_key"] == ["ORDERS.ORDER_ID"]
    assert any(
        relation["condition"] == "ORDERS.CUSTOMER_ID = CUSTOMERS.CUSTOMER_ID"
        for relation in context["relationships"]
    )
    assert context["profile"]["glossary"] == {"売上": "受注金額（AMOUNT）の合計"}
    assert context["reference_date"] == "2026-10-11"
    assert created.requirement_state is not None


def test_stale_published_ontology_is_skipped_but_schema_is_still_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.nl2sql import ontology_markdown_workspace, ontology_published_context

    llm = _ScriptedLlm(
        _output(_from_orders(), _select_total(), _where_shipped()),
        _output(_from_orders(), _select_total(), _where_shipped()),
    )
    runtime = _runtime(llm)
    revision_id = runtime._query_ontology("sales").revision.id
    monkeypatch.setattr(
        ontology_markdown_workspace.MarkdownOntologyWorkspace,
        "head",
        lambda self, profile_id: {"snapshot_id": revision_id},
    )
    monkeypatch.setattr(
        ontology_markdown_workspace.MarkdownOntologyWorkspace,
        "snapshot",
        lambda self, profile_id, *args, **kwargs: None,
    )

    def stale(*_args: Any, **_kwargs: Any) -> Any:
        raise OntologyGateBlockedError(
            "MARKDOWN_STALE",
            "Profile または Schema が変更されました。Markdown を再検証してください。",
        )

    monkeypatch.setattr(ontology_published_context, "published_context", stale)
    created = _create(runtime)
    draft = _draft(created)
    # オントロジーは「あれば使う」。公開版が古ければ業務の概念を使わず、Schema で確かめる。
    assert any("オントロジーの業務の概念を使わずに" in warning for warning in draft.warnings_ja)
    assert llm.calls[0]["context"]["ontology"] == []
    assert _state(created).can_apply is True

    # 確認だけの session（review_only）は、これまでどおり止める。
    with pytest.raises(OntologyGateBlockedError):
        runtime.create_session(
            QuerySessionApiCreate(question="受注", profile_id="sales"), actor_user_uuid="user-1"
        )


def test_llm_not_configured_does_not_fall_back_to_rule_draft() -> None:
    legacy = _LegacyService()
    runtime = OntologyApiRuntime(legacy_service=legacy, store=InMemoryOntologyStore())
    with pytest.raises(RequirementAiUnavailableError) as raised:
        _create(runtime)
    assert raised.value.code == "REQUIREMENT_AI_NOT_CONFIGURED"


def test_invalid_llm_output_is_rejected() -> None:
    class _BrokenLlm(_ScriptedLlm):
        def generate(self, **kwargs: Any) -> str:
            return '{"items": "not-a-list"}'

    runtime = _runtime(_BrokenLlm())
    with pytest.raises(RequirementAiUnavailableError) as raised:
        _create(runtime)
    assert raised.value.code == "REQUIREMENT_AI_INVALID_OUTPUT"


@pytest.mark.parametrize(
    ("status", "configuration_error"),
    [(404, True), (503, False)],
    ids=["authorization", "temporary"],
)
def test_llm_http_failure_guidance_depends_on_kind(status: int, configuration_error: bool) -> None:
    """設定の誤り（OCI の 404 など）は「もう一度」と案内せず、確かめる所を出す（#1684）。"""
    from pr_system_settings.enterprise_ai_errors import classify_enterprise_ai_http_failure

    from app.features.nl2sql.enterprise_ai_client import EnterpriseAiDirectError

    class _FailingLlm(_ScriptedLlm):
        def generate(self, **kwargs: Any) -> str:
            failure = classify_enterprise_ai_http_failure(status, "synthetic")
            raise EnterpriseAiDirectError(
                f"OCI Enterprise AI HTTP {status}: synthetic",
                code=failure.kind,
                retryable=failure.retryable,
                http_failure=failure,
                model_id="openai.gpt-oss-120b",
                endpoint_host="inference.generativeai.us-chicago-1.oci.oraclecloud.com",
            )

    runtime = _runtime(_FailingLlm())
    with pytest.raises(RequirementAiUnavailableError) as raised:
        _create(runtime)
    assert raised.value.code == "REQUIREMENT_AI_FAILED"
    if configuration_error:
        assert "もう一度" not in raised.value.message_ja
        assert "時間をおいても直りません" in raised.value.message_ja
        assert "openai.gpt-oss-120b" in raised.value.message_ja
        assert "既定のテキストモデル" in raised.value.message_ja
    else:
        assert "サーバーのエラー" in raised.value.message_ja
        assert "HTTP 503" in raised.value.message_ja


# --- 2. 範囲の外・実在しない名前・未承認の結合を拒む ---------------------------------


def test_out_of_scope_and_unknown_names_are_rejected() -> None:
    llm = _ScriptedLlm(
        _output(
            _item(
                "from_tables",
                "from",
                "confirmed",
                tables=["APP.ORDERS", "APP.SECRET_TABLE", "APP.NO_SUCH_TABLE"],
            ),
            _item(
                "select_amount",
                "select",
                "confirmed",
                columns=["APP.ORDERS.AMOUNT", "APP.ORDERS.NO_SUCH_COLUMN"],
                sql_hint="SUM(ORDERS.NO_SUCH_COLUMN)",
            ),
        )
    )
    created = _create(_runtime(llm))
    draft = created.session.requirement
    assert draft is not None
    from_item = next(item for item in draft.items if item.key == "from_tables")
    assert from_item.tables == ["APP.ORDERS"]
    # 実在しない名前があった項目は、推測のままにせず確認に回す。
    assert from_item.status == RequirementStatus.AMBIGUOUS
    assert "APP.SECRET_TABLE" in from_item.reason_ja
    select_item = next(item for item in draft.items if item.key == "select_amount")
    assert select_item.columns == ["APP.ORDERS.AMOUNT"]
    assert select_item.sql_hint == ""
    assert any("APP.NO_SUCH_TABLE" in warning for warning in draft.warnings_ja)


def test_unapproved_join_is_rejected_and_approved_relationship_is_used() -> None:
    llm = _ScriptedLlm(
        _output(
            _item("from_tables", "from", "confirmed", tables=["APP.ORDERS", "APP.CUSTOMERS"]),
            _item("select_region", "select", "confirmed", columns=["APP.CUSTOMERS.REGION"]),
            _item(
                "join_orders_customers",
                "join",
                "confirmed",
                relationship_ids=["made_up_relationship"],
                sql_hint="ORDERS.ORDER_ID = CUSTOMERS.CUSTOMER_ID",
            ),
        )
    )
    created = _create(_runtime(llm), "地域ごとの受注")
    draft = created.session.requirement
    assert draft is not None
    join = next(item for item in draft.items if item.key == "join_orders_customers")
    assert join.relationship_ids == []
    assert join.status == RequirementStatus.AMBIGUOUS
    assert "承認済み" in join.reason_ja


def test_unconnectable_tables_are_unanswerable() -> None:
    llm = _ScriptedLlm(
        _output(
            _item("from_tables", "from", "confirmed", tables=["APP.ORDERS", "APP.CUSTOMERS"]),
            _select_total(),
        )
    )
    legacy = _LegacyService()
    # 外部キーを外す（承認済みの関係が無い）。
    legacy.catalog.tables[0].constraint_details = legacy.catalog.tables[0].constraint_details[:1]
    legacy._enterprise_ai_client = llm
    runtime = OntologyApiRuntime(legacy_service=legacy, store=InMemoryOntologyStore())
    runtime._requirement_today = lambda: TODAY  # type: ignore[method-assign]

    created = _create(runtime)
    state = created.requirement_state
    assert state is not None
    assert state.status == RequirementPhase.UNANSWERABLE
    assert "この業務プロファイルでは答えられません" in state.message_ja
    assert state.can_apply is False


def _runtime_with_detail_view(llm: _ScriptedLlm) -> OntologyApiRuntime:
    """受注・顧客の表と、同じ内容の明細のビュー（外部キーの無い）を持つ業務プロファイル（#1991）。"""

    legacy = _LegacyService()
    legacy.profile.allowed_views = ["APP.V_ORDER_DETAIL"]
    legacy.catalog.tables.append(
        SchemaTable(
            table_name="V_ORDER_DETAIL",
            owner="APP",
            table_type="view",
            logical_name="受注の明細",
            comment="受注と顧客を結合したビュー",
            columns=[
                SchemaColumn(column_name="ORDER_ID", logical_name="受注ID", data_type="NUMBER"),
                SchemaColumn(column_name="REGION", logical_name="地域", data_type="VARCHAR2"),
                SchemaColumn(column_name="AMOUNT", logical_name="受注金額", data_type="NUMBER"),
            ],
        )
    )
    scope = AllowedObjects(
        table_names=[*legacy.profile.allowed_tables, *legacy.profile.allowed_views],
        enforce_table_scope=True,
    )
    legacy.resolve_allowed_objects = lambda *_args: scope.model_copy(deep=True)  # type: ignore[method-assign]
    legacy._enterprise_ai_client = llm
    runtime = OntologyApiRuntime(legacy_service=legacy, store=InMemoryOntologyStore())
    runtime._requirement_today = lambda: TODAY  # type: ignore[method-assign]
    return runtime


_ALL_SALES_OBJECTS = ["APP.ORDERS", "APP.CUSTOMERS", "APP.V_ORDER_DETAIL"]


def test_unused_view_beside_joined_tables_is_dropped_from_from() -> None:
    """回答が元の表とビューを両方挙げても、列を使わないビューを外して外部キーで答える（#1991）。"""

    llm = _ScriptedLlm(
        _output(
            _item(
                "from_tables",
                "from",
                "confirmed",
                value="APP.ORDERS、APP.CUSTOMERS、APP.V_ORDER_DETAIL",
                tables=_ALL_SALES_OBJECTS,
            ),
            _item(
                "select_region",
                "select",
                "confirmed",
                tables=_ALL_SALES_OBJECTS,
                columns=["APP.CUSTOMERS.REGION"],
                sql_hint="CUSTOMERS.REGION",
            ),
            _where_shipped(),
        )
    )
    created = _create(_runtime_with_detail_view(llm), "出荷済みの受注の顧客の地域")
    state = _state(created)
    assert state.status != RequirementPhase.UNANSWERABLE
    draft = _draft(created)
    assert not draft.unanswerable_reason_ja
    assert from_tables(draft.items) == ["APP.ORDERS", "APP.CUSTOMERS"]
    from_item = next(item for item in draft.items if item.key == "from_tables")
    assert from_item.value_ja == "APP.ORDERS、APP.CUSTOMERS"
    assert "APP.V_ORDER_DETAIL（受注の明細） は、ほかの表と承認済みの関係" in from_item.reason_ja
    select = next(item for item in draft.items if item.key == "select_region")
    assert "APP.V_ORDER_DETAIL" not in select.tables
    join = next(item for item in draft.items if item.key == "join_relationships")
    assert join.sql_hint == "ORDERS.CUSTOMER_ID = CUSTOMERS.CUSTOMER_ID"
    assert "V_ORDER_DETAIL" not in draft.sql_skeleton


def test_view_with_used_columns_is_kept_and_unused_tables_are_dropped() -> None:
    """列をビューだけが持つなら、ビューで答える（列を使わない元の表を外す。#1991）。"""

    llm = _ScriptedLlm(
        _output(
            _item(
                "from_tables",
                "from",
                "confirmed",
                tables=["APP.V_ORDER_DETAIL", "APP.CUSTOMERS"],
                sql_hint="APP.V_ORDER_DETAIL",
            ),
            _item(
                "select_region",
                "select",
                "confirmed",
                columns=["APP.V_ORDER_DETAIL.REGION"],
                sql_hint="V_ORDER_DETAIL.REGION",
            ),
        )
    )
    created = _create(_runtime_with_detail_view(llm), "受注の明細の地域")
    draft = _draft(created)
    assert not draft.unanswerable_reason_ja
    assert from_tables(draft.items) == ["APP.V_ORDER_DETAIL"]
    from_item = next(item for item in draft.items if item.key == "from_tables")
    assert from_item.sql_hint == "APP.V_ORDER_DETAIL"
    assert "APP.CUSTOMERS（顧客） は" in from_item.reason_ja


def test_view_used_only_in_sql_hints_is_kept_over_unused_tables() -> None:
    """列の欄が空でも、SQL の断片が物理名で書いたビューで答える（実モデルの出力の形。#1991）。"""

    llm = _ScriptedLlm(
        _output(
            _item("from_tables", "from", "confirmed", tables=_ALL_SALES_OBJECTS),
            _item("select_region", "select", "confirmed", sql_hint="APP.V_ORDER_DETAIL.REGION"),
            _item("where_amount", "where", "confirmed", sql_hint="V_ORDER_DETAIL.AMOUNT > 0"),
        )
    )
    draft = _draft(_create(_runtime_with_detail_view(llm), "受注の明細の地域"))
    assert not draft.unanswerable_reason_ja
    assert from_tables(draft.items) == ["APP.V_ORDER_DETAIL"]


def test_clarified_question_decides_when_no_item_names_a_column() -> None:
    """どの項目も列を書かないときは、AI が書き直した質問の文の表で決める（#1991）。"""

    llm = _ScriptedLlm(
        _output(
            _item("from_tables", "from", "confirmed", tables=_ALL_SALES_OBJECTS),
            _item("select_all", "select", "confirmed", value="受注の一覧", sql_hint="*"),
            clarified="APP.ORDERS と APP.CUSTOMERS を結合して受注の一覧を表示してください。",
        )
    )
    draft = _draft(_create(_runtime_with_detail_view(llm), "受注の一覧"))
    assert not draft.unanswerable_reason_ja
    assert from_tables(draft.items) == ["APP.ORDERS", "APP.CUSTOMERS"]


def test_view_and_table_both_used_by_columns_stay_unanswerable() -> None:
    """列を使う表がビューと元の表にまたがるなら、結合を作らず答えられない理由を返す（#1741）。"""

    llm = _ScriptedLlm(
        _output(
            _item(
                "from_tables",
                "from",
                "confirmed",
                tables=["APP.V_ORDER_DETAIL", "APP.CUSTOMERS"],
            ),
            _item(
                "select_region",
                "select",
                "confirmed",
                columns=["APP.V_ORDER_DETAIL.AMOUNT", "APP.CUSTOMERS.REGION"],
                sql_hint="V_ORDER_DETAIL.AMOUNT, CUSTOMERS.REGION",
            ),
        )
    )
    created = _create(_runtime_with_detail_view(llm), "地域と受注金額")
    state = _state(created)
    assert state.status == RequirementPhase.UNANSWERABLE
    assert from_tables(_draft(created).items) == ["APP.V_ORDER_DETAIL", "APP.CUSTOMERS"]


# --- 3. 根拠の無い候補・マスクの値を出さない ------------------------------------------


def test_candidates_without_evidence_or_grounded_value_are_dropped() -> None:
    llm = _ScriptedLlm(
        _output(
            _from_orders(),
            _select_total(),
            _item(
                "where_status",
                "where",
                "ambiguous",
                question="どの受注状態に絞りますか？",
                columns=["APP.ORDERS.STATUS"],
                candidates=[
                    _candidate(
                        "出荷済み（SHIPPED）",
                        columns=["APP.ORDERS.STATUS"],
                        sql_hint="ORDERS.STATUS = 'SHIPPED'",
                    ),
                    _candidate("根拠の無い候補", evidence="", sql_hint="ORDERS.STATUS = 'X'"),
                    _candidate(
                        "データからの推測",
                        columns=["APP.ORDERS.STATUS"],
                        sql_hint="ORDERS.STATUS = 'SECRET_SAMPLE_VALUE'",
                    ),
                    _candidate("範囲の外", tables=["APP.SECRET_TABLE"]),
                ],
            ),
        )
    )
    created = _create(_runtime(llm))
    state = created.requirement_state
    assert state is not None and state.current_item is not None
    assert state.current_item.key == "where_status"
    assert [candidate.label_ja for candidate in state.current_item.candidates] == [
        "出荷済み（SHIPPED）"
    ]


def test_masked_column_values_are_not_offered() -> None:
    profile = _LegacyService().profile
    context = build_requirement_context(
        question="状態で絞る",
        profile=profile,
        catalog_tables=_catalog().tables,
        allowed=AllowedObjects(),
        ontology=None,
        view=None,
        today=TODAY,
    )
    context.tables["APP.ORDERS"].columns["APP.ORDERS.STATUS"].masked = True
    output = LlmRequirementOutput.model_validate(
        _output(
            _from_orders(),
            _select_total(),
            _item(
                "where_status",
                "where",
                "inferred",
                columns=["APP.ORDERS.STATUS"],
                sql_hint="ORDERS.STATUS = 'SHIPPED'",
                candidates=[
                    _candidate(
                        "出荷済み",
                        columns=["APP.ORDERS.STATUS"],
                        sql_hint="ORDERS.STATUS = 'SHIPPED'",
                    )
                ],
            ),
        )
    )
    draft = build_draft(output, context, question="状態で絞る")
    item = next(value for value in draft.items if value.key == "where_status")
    assert item.candidates == []
    assert item.sql_hint == ""
    assert item.status == RequirementStatus.AMBIGUOUS
    assert "マスク" in item.reason_ja


# --- 4. 相対的な期間を具体的な日付の範囲で確かめる ------------------------------------


def _fiscal_period_item(status: str = "confirmed") -> dict[str, Any]:
    return _item(
        "period_this_fiscal_year",
        "period",
        status,
        label="期間",
        value="今年度",
        period={
            "expression_ja": "今年度",
            "relative_kind": "this_fiscal_year",
            "count": 0,
            "fiscal_year_start_month": 0,
            "date_column": "APP.ORDERS.ORDER_DATE",
        },
    )


def test_relative_period_is_always_confirmed_as_concrete_dates() -> None:
    llm = _ScriptedLlm(_output(_from_orders(), _select_total(), _fiscal_period_item()))
    created = _create(_runtime(llm), "今年度の受注金額の合計")
    draft = created.session.requirement
    assert draft is not None
    # 日付の列が 2 つ（ORDER_DATE・SHIP_DATE）あるので、まず列を確かめる。
    column_item = next(item for item in draft.items if item.key == "period_date_column")
    assert column_item.status == RequirementStatus.AMBIGUOUS
    assert column_item.candidates[0].label_ja == "ORDERS.ORDER_DATE（受注日）"
    assert "AI の推測" in column_item.candidates[0].description_ja
    period = next(item for item in draft.items if item.key == "period_this_fiscal_year")
    # LLM が confirmed と返しても、相対的な期間は必ず確かめる。
    assert period.status == RequirementStatus.AMBIGUOUS
    labels = [candidate.label_ja for candidate in period.candidates]
    # 年度の開始月が定義されていないので、4 月始まりと 1 月始まりを具体的な日付で出す。
    assert "2026-04-01〜2027-03-31" in labels
    assert "2026-01-01〜2026-12-31" in labels
    assert "データの最新日を基準にする" in labels
    assert "実行日（2026-10-11）" in period.reason_ja
    assert "開始日と終了日を含みます" in period.reason_ja


def test_fiscal_year_start_from_glossary_is_the_candidate() -> None:
    llm = _ScriptedLlm(_output(_from_orders(), _select_total(), _fiscal_period_item()))
    runtime = _runtime(llm, glossary={"年度": "4月1日から翌年3月31日までの期間"})
    created = _create(runtime, "今年度の受注金額の合計")
    draft = created.session.requirement
    assert draft is not None
    period = next(item for item in draft.items if item.key == "period_this_fiscal_year")
    labels = [candidate.label_ja for candidate in period.candidates]
    assert "2026-01-01〜2026-12-31" not in labels
    assert labels[0] == "2026-04-01〜2027-03-31"
    assert period.candidates[0].source.value == "glossary"
    assert "用語集の定義（4月）" in period.reason_ja


@pytest.mark.parametrize(
    ("kind", "count", "expected"),
    [
        ("last_month", None, [("2026-09-01", "2026-09-30")]),
        ("this_month", None, [("2026-10-01", "2026-10-11"), ("2026-10-01", "2026-10-31")]),
        (
            "last_n_months",
            3,
            [
                ("2026-07-12", "2026-10-11"),
                ("2026-07-01", "2026-09-30"),
                ("2026-08-01", "2026-10-11"),
            ],
        ),
        ("last_fiscal_year", None, [("2025-04-01", "2026-03-31"), ("2025-01-01", "2025-12-31")]),
        (
            "same_period_last_year",
            None,
            [
                ("2025-10-01", "2025-10-31"),
                ("2025-10-01", "2025-10-11"),
                ("2025-01-01", "2025-10-11"),
            ],
        ),
    ],
    ids=["last_month", "this_month", "last_3_months", "last_fiscal_year", "same_period_last_year"],
)
def test_period_ranges_are_computed_from_reference_date(
    kind: str, count: int | None, expected: list[tuple[str, str]]
) -> None:
    ranges = period_ranges(kind, count, TODAY, None)
    assert [(start.isoformat(), end.isoformat()) for _label, start, end in ranges] == expected


def _requirement_context(question: str) -> Any:
    return build_requirement_context(
        question=question,
        profile=_LegacyService().profile,
        catalog_tables=_catalog().tables,
        allowed=AllowedObjects(),
        ontology=None,
        view=None,
        today=TODAY,
    )


def _blank_period_item(relative_kind: str = "other") -> dict[str, Any]:
    # LLM が期間の項目を返したが、期間の表現（expression_ja）も値（value_ja）も空（#1878）。
    return _item(
        "period_recent",
        "period",
        "ambiguous",
        label="期間",
        period={
            "expression_ja": "",
            "relative_kind": relative_kind,
            "count": 0,
            "fiscal_year_start_month": 0,
            "date_column": "",
        },
    )


def _texts(draft: SqlRequirementDraft) -> list[str]:
    texts = [draft.clarified_question_ja, draft.unanswerable_reason_ja, *draft.warnings_ja]
    for item in draft.items:
        texts += [item.label_ja, item.value_ja, item.question_ja, item.reason_ja]
        texts += [candidate.label_ja for candidate in item.candidates]
        texts += [candidate.description_ja for candidate in item.candidates]
    return texts


def test_blank_period_expression_uses_period_word_of_question() -> None:
    question = "最近の受注金額の合計を見せて"
    output = LlmRequirementOutput.model_validate(
        _output(_from_orders(), _select_total(), _blank_period_item())
    )
    draft = build_draft(output, _requirement_context(question), question=question)
    period = next(item for item in draft.items if item.key == "period_recent")
    assert period.period is not None
    assert period.period.expression_ja == "最近"
    assert period.question_ja.startswith("「最近」は、いつからいつまでですか？")
    column_item = next(item for item in draft.items if item.key == "period_date_column")
    assert column_item.question_ja == "「最近」の期間は、どの日付の列で絞りますか？"
    assert not any("「」" in text for text in _texts(draft))


@pytest.mark.parametrize(
    "relative_kind",
    ["other", "this_fiscal_year", "last_month"],
    ids=["no_candidates", "candidates", "settled_by_reference_date"],
)
def test_blank_period_expression_without_period_word_has_no_empty_quotes(
    relative_kind: str,
) -> None:
    question = "受注金額の合計を期間で絞って見せて"
    output = LlmRequirementOutput.model_validate(
        _output(_from_orders(), _select_total(), _blank_period_item(relative_kind))
    )
    draft = build_draft(output, _requirement_context(question), question=question)
    period = next(item for item in draft.items if item.key == "period_recent")
    assert period.period is not None
    assert period.period.expression_ja == ""
    assert not any("「」" in text for text in _texts(draft))
    column_item = next(item for item in draft.items if item.key == "period_date_column")
    assert column_item.question_ja == "期間は、どの日付の列で絞りますか？"
    if relative_kind == "other":
        assert period.question_ja.startswith("期間は、いつからいつまでですか？")
    elif relative_kind == "this_fiscal_year":
        assert period.question_ja == "どの期間で絞りますか？"
        latest = next(
            candidate
            for candidate in period.candidates
            if candidate.label_ja == "データの最新日を基準にする"
        )
        assert latest.description_ja.startswith("期間を、実行日ではなく")
    else:
        assert period.reason_ja.startswith("期間は、基準日（実行日）2026-10-11 から ")


def test_llm_null_strings_are_treated_as_empty() -> None:
    question = "受注金額の合計"
    from_item = _from_orders()
    from_item["reason_ja"] = "None"
    from_item["question_ja"] = " ｎｕｌｌ "
    select_item = _select_total()
    select_item["tables"] = ["null"]
    select_item["relationship_ids"] = ["N/A"]
    select_item["candidates"] = [_candidate("null", evidence="undefined")]
    output = LlmRequirementOutput.model_validate(
        _output(from_item, select_item, clarified="NULL", unanswerable="null")
    )
    draft = build_draft(output, _requirement_context(question), question=question)
    assert draft.unanswerable_reason_ja == ""
    # 書き直した質問が空なら、元の質問を物理名で補う。
    assert draft.clarified_question_ja
    assert draft.warnings_ja == []
    select = next(item for item in draft.items if item.key == "select_total")
    # 「null」の表・関係を範囲の外として除き、確定の項目をあいまいにしない。
    assert select.status == RequirementStatus.CONFIRMED
    assert select.tables == []
    assert select.relationship_ids == []
    assert not any(
        text.strip().casefold() in {"null", "none", "n/a", "undefined", "ｎｕｌｌ"}
        for text in _texts(draft)
    )


def test_llm_null_unanswerable_reason_does_not_close_the_question() -> None:
    llm = _ScriptedLlm(
        _output(_from_orders(), _select_total(), _where_shipped(), unanswerable="null")
    )
    created = _create(_runtime(llm))
    assert _draft(created).unanswerable_reason_ja == ""
    state = _state(created)
    assert state.status != RequirementPhase.UNANSWERABLE
    assert "null" not in state.message_ja


@pytest.mark.parametrize(
    "reason",
    ["なし（質問は回答可能）", "回答可能", "特になし"],
    ids=["none-answerable", "answerable", "nothing"],
)
def test_answerable_output_ignores_reason_text(reason: str) -> None:
    # 確認に答えた後の検査で、LLM が理由の欄を「なし（回答可能）」などで埋めても、
    # answerable=true なら回答できない扱いにしない（#2222）。
    llm = _ScriptedLlm(
        _output(_from_orders(), _select_total(), _where_shipped(), unanswerable=reason)
    )
    created = _create(_runtime(llm))
    assert _draft(created).unanswerable_reason_ja == ""
    state = _state(created)
    assert state.status != RequirementPhase.UNANSWERABLE
    assert reason not in state.message_ja


def test_unanswerable_output_without_reason_uses_default_message() -> None:
    llm = _ScriptedLlm(_output(_from_orders(), _select_total(), answerable=False))
    created = _create(_runtime(llm))
    state = _state(created)
    assert state.status == RequirementPhase.UNANSWERABLE
    assert state.message_ja == "この業務プロファイルの範囲の表では答えられません。"


def test_requirement_schema_requires_answerable() -> None:
    schema = requirement_response_format()["schema"]
    assert schema["properties"]["answerable"]["type"] == "boolean"
    assert "answerable" in schema["required"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("null", ""),
        (" NULL ", ""),
        ("ｎｕｌｌ", ""),
        ("None", ""),
        ("N/A", ""),
        ("undefined", ""),
        (None, ""),
        ("  null の値を除く  ", "null の値を除く"),
        ("指定なし", "指定なし"),
    ],
    ids=[
        "null",
        "upper_with_space",
        "fullwidth",
        "none",
        "n_a",
        "undefined",
        "python_none",
        "sentence",
        "japanese",
    ],
)
def test_llm_text_treats_empty_markers_as_empty(value: str | None, expected: str) -> None:
    assert llm_text(value) == expected


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("最近の売上を見せて", "最近"),
        ("直近3か月の売上", "直近3か月"),
        ("過去 30 日の受注", "過去 30 日"),
        ("今年度の受注金額", "今年度"),
        ("前年同期と比べた売上", "前年同期"),
        ("先月の受注", "先月"),
        ("受注金額の合計", ""),
    ],
    ids=["recent", "last_3_months", "last_30_days", "fiscal", "same_period", "last_month", "none"],
)
def test_period_expression_from_question(question: str, expected: str) -> None:
    assert period_expression_from_question(question) == expected


# --- 5. 回答・再検査・上限 -------------------------------------------------------------


def test_answer_then_review_adds_question_and_stops_when_clear() -> None:
    def first_review(context: dict[str, Any]) -> dict[str, Any]:
        answered = {item["key"] for item in context["answered_items"]}
        assert "where_status" in answered
        return _output(
            # 答えた項目を変えようとしても保たれる。
            _item("where_status", "where", "ambiguous", question="変えてはいけない"),
            _from_orders(),
            _select_total(),
            _item(
                "other_currency",
                "other",
                "missing",
                question="金額は税込と税抜のどちらですか？",
                answer_kind="free_text",
            ),
            clarified="出荷済みの ORDERS の AMOUNT の合計",
        )

    def second_review(context: dict[str, Any]) -> dict[str, Any]:
        answered = {item["key"] for item in context["answered_items"]}
        assert answered == {"where_status", "other_currency"}
        return _output(
            _from_orders(),
            _select_total(),
            clarified="出荷済み（ORDERS.STATUS = 'SHIPPED'）の ORDERS.AMOUNT（税込）の合計",
        )

    llm = _ScriptedLlm(
        _output(
            _from_orders(),
            _select_total(),
            _item(
                "where_status",
                "where",
                "ambiguous",
                question="どの受注状態に絞りますか？",
                columns=["APP.ORDERS.STATUS"],
                candidates=[
                    _candidate(
                        "出荷済み（SHIPPED）",
                        columns=["APP.ORDERS.STATUS"],
                        sql_hint="ORDERS.STATUS = 'SHIPPED'",
                    )
                ],
            ),
        ),
        first_review,
        second_review,
    )
    runtime = _runtime(llm)
    created = _create(runtime)
    assert created.requirement_state is not None
    assert created.requirement_state.status == RequirementPhase.NEEDS_ANSWER
    assert created.requirement_state.can_apply is False

    after_first = _answer(runtime, created)
    assert len(llm.calls) == 2
    state = after_first.requirement_state
    assert state is not None
    assert state.review_round == 1
    assert state.current_item is not None and state.current_item.key == "other_currency"
    kept = next(item for item in _items(after_first) if item.key == "where_status")
    assert kept.answered and kept.sql_hint == "ORDERS.STATUS = 'SHIPPED'"

    after_second = _answer(runtime, after_first, free_text="税込")
    assert len(llm.calls) == 3
    final = after_second.requirement_state
    assert final is not None
    assert final.status == RequirementPhase.READY
    assert final.review_round == 2
    assert final.can_apply is True
    assert final.unresolved_items == []


def test_review_limit_lists_remaining_items_and_allows_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "nl2sql_requirement_max_review_rounds", 1)
    still_missing = _output(
        _from_orders(),
        _select_total(),
        _where_shipped(),
        _item("other_unit", "other", "missing", question="単位は？", answer_kind="free_text"),
    )
    llm = _ScriptedLlm(
        _output(
            _from_orders(),
            _select_total(),
            _where_shipped(),
            _item("other_currency", "other", "missing", question="税込？", answer_kind="free_text"),
        ),
        still_missing,
    )
    runtime = _runtime(llm)
    created = _create(runtime)
    after = _answer(runtime, created, free_text="税込")
    state = after.requirement_state
    assert state is not None
    assert state.review_limit_reached is True
    assert [item.key for item in state.unresolved_items] == ["other_unit"]
    assert "上限（1 回）" in state.message_ja
    # SELECT と FROM が明確なら、残りを AI に任せて進めるかを利用者が決められる。
    assert state.can_apply is True

    # 上限の後は回答しても LLM を呼ばない。
    final = _answer(runtime, after, free_text="円")
    assert len(llm.calls) == 2
    assert final.requirement_state is not None
    assert final.requirement_state.status == RequirementPhase.READY


def test_select_and_from_must_be_clear_before_apply() -> None:
    llm = _ScriptedLlm(
        _output(
            _item("from_tables", "from", "missing", question="どの表？"),
        )
    )
    runtime = _runtime(llm)
    created = _create(runtime, "いい感じに出して")
    state = created.requirement_state
    assert state is not None
    assert state.select_from_clear is False
    assert len(state.minimum_missing_ja) == 2
    assert state.can_apply is False
    # FROM の候補は範囲の表（物理名）、SELECT の不足もプログラムが質問にする。
    from_item = next(
        item for item in state.unresolved_items if item.section == RequirementSection.FROM
    )
    assert {candidate.label_ja for candidate in from_item.candidates} == {
        "APP.ORDERS（受注）",
        "APP.CUSTOMERS（顧客）",
    }
    assert any(item.key == "select_output" for item in state.unresolved_items)
    with pytest.raises(OntologyGateBlockedError) as raised:
        runtime.apply_requirement(created.session.id, RequirementApplyRequest(base_version=1))
    assert raised.value.code == "GUIDED_CLARIFICATION_INCOMPLETE"


def test_default_review_limit_shows_three_confirmation_rounds() -> None:
    """システムの既定の確認の上限は 3 回（AI の再検査の上限 2 回 + 最初の確認。#2313）。"""

    default = Settings.model_fields["nl2sql_requirement_max_review_rounds"].default
    assert default + 1 == 3


def test_profile_clarification_max_rounds_sets_the_review_limit() -> None:
    """業務プロファイルの「確認の上限（往復）」は AI の再検査の上限 + 1（#1839）。

    0 はシステムの既定。
    """

    runtime = _runtime(_ScriptedLlm(_ambiguous_status_output()))
    legacy = runtime.legacy_service
    default = int(get_settings().nl2sql_requirement_max_review_rounds)
    assert runtime._requirement_max_review_rounds("sales") == default
    legacy.profile = legacy.profile.model_copy(update={"clarification_max_rounds": 2})
    assert runtime._requirement_max_review_rounds("sales") == 1
    data = _create(runtime)
    assert _state(data).max_review_rounds == 1
    # 業務プロファイルが見つからないときはシステムの既定。
    assert runtime._requirement_max_review_rounds("missing") == default


def test_review_limit_still_blocks_apply_without_select() -> None:
    llm = _ScriptedLlm(_output(_from_orders(), _where_shipped()))
    state = _create(_runtime(llm)).requirement_state
    assert state is not None
    assert "何を表示するか（SELECT）が明確ではありません。" in state.minimum_missing_ja
    assert state.can_apply is False


# --- 6. 物理名 -------------------------------------------------------------------------


def test_screen_text_and_summary_use_physical_names() -> None:
    llm = _ScriptedLlm(
        _output(
            _from_orders(),
            _select_total(),
            _where_shipped(),
            _fiscal_period_item(),
            # 業務名だけで書いた質問の文。
            clarified="今年度の出荷済みの受注金額の合計",
        )
    )
    created = _create(_runtime(llm), "今年度の出荷済みの売上")
    draft = created.session.requirement
    assert draft is not None
    select_item = next(item for item in draft.items if item.key == "select_total")
    assert select_item.refs_ja == "ORDERS.AMOUNT（受注金額）"
    from_item = next(item for item in draft.items if item.key == "from_orders")
    assert from_item.refs_ja == "APP.ORDERS（受注）"
    # 書き直した質問の文に物理名を添える。
    assert "ORDERS" in draft.clarified_question_ja
    assert "ORDERS.AMOUNT" in draft.clarified_question_ja
    state = created.requirement_state
    assert state is not None
    assert "APP.ORDERS" in state.summary_ja
    assert "ORDERS.STATUS" in state.summary_ja
    column_item = next(item for item in draft.items if item.key == "period_date_column")
    assert {candidate.label_ja for candidate in column_item.candidates} == {
        "ORDERS.ORDER_DATE（受注日）",
        "ORDERS.SHIP_DATE（出荷日）",
    }


# --- 7. 危険・重い問い合わせの確認 -------------------------------------------------------


def test_detail_without_filter_or_limit_needs_no_question() -> None:
    """絞り込み・件数の上限が質問に無い明細は「指定なし」として確認しない（#1715。最低限の要件）。"""
    llm = _ScriptedLlm(
        _output(
            _from_orders(),
            _item(
                "select_columns",
                "select",
                "confirmed",
                columns=["APP.ORDERS.ORDER_ID", "APP.ORDERS.AMOUNT"],
                sql_hint="ORDERS.ORDER_ID, ORDERS.AMOUNT",
            ),
        )
    )
    created = _create(_runtime(llm), "受注の一覧")
    state = _state(created)
    assert state.unresolved_items == []
    assert state.current_item is None
    assert state.status == RequirementPhase.READY
    assert state.can_apply is True
    assert not any(item.key.startswith("risk_no_") for item in _items(created))


def test_sql_statement_question_is_checked_like_any_question_and_needs_no_question() -> None:
    """質問が SQL の文でも飛ばさず同じに確かめ、最低限が決まるので質問 0 件で進む（#1727）。

    SQL の文かどうかは判断に使わない（判断は SQL を 1 つに決める最低限の情報があるかだけ）。
    LLM の指示にも `select * from employee` を質問 0 件の例として入れる。
    """
    from app.features.nl2sql.sql_requirement import SYSTEM_PROMPT

    assert "select * from employee" in SYSTEM_PROMPT
    llm = _ScriptedLlm(
        _output(
            _from_orders(),
            _item(
                "select_all",
                "select",
                "confirmed",
                label="全列",
                value="*",
                columns=[],
                sql_hint="*",
            ),
            clarified="APP.ORDERS の全列の一覧",
        )
    )
    created = _create(_runtime(llm), "select * from orders;")
    # SQL の文でも LLM で確かめる（飛ばさない）。質問はそのまま LLM に渡す。
    assert len(llm.calls) == 1
    assert "select * from orders;" in llm.calls[0]["prompt"]
    state = _state(created)
    assert state.unresolved_items == []
    assert state.current_item is None
    assert state.status == RequirementPhase.READY
    assert state.can_apply is True
    assert state.answered_count == 0


def test_join_without_condition_is_confirmed_as_cartesian_risk() -> None:
    llm = _ScriptedLlm(
        _output(
            _item("from_tables", "from", "confirmed", tables=["APP.ORDERS", "APP.CUSTOMERS"]),
            _item(
                "select_region_total",
                "select",
                "confirmed",
                columns=["APP.CUSTOMERS.REGION", "APP.ORDERS.AMOUNT"],
                sql_hint="CUSTOMERS.REGION, SUM(ORDERS.AMOUNT)",
            ),
            _item("group_region", "group_by", "confirmed", sql_hint="CUSTOMERS.REGION"),
            _item("join_type", "join", "confirmed", label="結合の種類", value="INNER JOIN"),
            _where_shipped(),
        )
    )
    state = _create(_runtime(llm), "地域ごとの受注金額").requirement_state
    assert state is not None
    risk = next(item for item in state.unresolved_items if item.key == "risk_cartesian_join")
    assert "デカルト積" in risk.reason_ja
    assert risk.candidates[0].label_ja == "ORDERS.CUSTOMER_ID = CUSTOMERS.CUSTOMER_ID"


# --- 8. SQL の骨組み -------------------------------------------------------------------


def test_sql_skeleton_is_built_deterministically_from_requirements() -> None:
    llm = _ScriptedLlm(
        _output(
            _item("from_tables", "from", "confirmed", tables=["APP.ORDERS", "APP.CUSTOMERS"]),
            _item(
                "select_region_total",
                "select",
                "confirmed",
                sql_hint="SELECT CUSTOMERS.REGION, SUM(ORDERS.AMOUNT)",
                columns=["APP.CUSTOMERS.REGION", "APP.ORDERS.AMOUNT"],
            ),
            _item("group_region", "group_by", "confirmed", sql_hint="GROUP BY CUSTOMERS.REGION"),
            _item("order_total", "order_by", "inferred", sql_hint="SUM(ORDERS.AMOUNT) DESC"),
            _where_shipped(),
            _fiscal_period_item(),
        ),
        _output(_from_orders(), clarified="今年度の地域ごとの ORDERS.AMOUNT の合計"),
    )
    runtime = _runtime(llm)
    created = _create(runtime, "今年度の地域ごとの売上")
    skeleton = _state(created).sql_skeleton
    assert "FROM APP.ORDERS" in skeleton
    assert "JOIN APP.CUSTOMERS ON ORDERS.CUSTOMER_ID = CUSTOMERS.CUSTOMER_ID" in skeleton
    # 未確定（期間）は目印にする。
    assert "⟨未確定: 期間⟩" in skeleton

    after_column = _answer(runtime, created)  # 日付の列: ORDERS.ORDER_DATE
    after_period = _answer(runtime, after_column)  # 2026-04-01〜2027-03-31（4 月始まり・全体）
    state = after_period.requirement_state
    assert state is not None
    assert state.status == RequirementPhase.READY, state.message_ja
    skeleton = state.sql_skeleton
    assert skeleton.startswith("SELECT CUSTOMERS.REGION, SUM(ORDERS.AMOUNT)")
    assert (
        "ORDERS.ORDER_DATE >= DATE '2026-04-01' AND ORDERS.ORDER_DATE < DATE '2027-04-01'"
        in skeleton
    )
    assert "ORDERS.STATUS = 'SHIPPED'" in skeleton
    assert "GROUP BY CUSTOMERS.REGION" in skeleton
    assert "ORDER BY SUM(ORDERS.AMOUNT) DESC" in skeleton
    assert "⟨未確定" not in skeleton
    # LLM が句のキーワードを付けても、骨組みで二重にしない。
    assert "SELECT SELECT" not in skeleton
    assert "GROUP BY GROUP BY" not in skeleton
    # 相対的な期間は、確かめた具体的な日付の範囲を書き直した質問の文にも書く。
    assert "2026-04-01〜2027-03-31" in _draft(after_period).clarified_question_ja
    # 同じ要件からは同じ骨組み（決定的）。
    again = runtime.get_session(after_period.session.id).requirement_state
    assert again is not None and again.sql_skeleton == skeleton


def _clause_lines(skeleton: str, keyword: str) -> list[str]:
    return [line for line in skeleton.splitlines() if line.startswith(keyword)]


def test_skeleton_settled_items_without_fragment_are_not_undecided() -> None:
    """確定して SQL の断片の無い項目（全件・並び順の指定なし）は ⟨未確定⟩ にせず、句も出さない。

    #1466。
    """

    llm = _ScriptedLlm(
        _output(
            _from_orders(),
            _select_total(),
            # LLM が断片の代わりに文を返したものは、断片にせず注記にする。
            _item(
                "select_distinct",
                "select",
                "inferred",
                label="重複の除去",
                value="重複を除かない",
                sql_hint="DISTINCT を付けない",
            ),
            # 引用符の中の日本語は値なので、断片のまま。
            _item(
                "where_label",
                "where",
                "confirmed",
                label="区分",
                value="出荷済み",
                sql_hint="ORDERS.STATUS = 'しゅっか済み'",
            ),
            _item(
                "where_none",
                "where",
                "inferred",
                label="絞り込み条件",
                value="WHERE による絞り込みは行わず、全件を対象にする",
            ),
            _item(
                "order_none",
                "order_by",
                "inferred",
                label="並び順",
                value="並び順の指定なし\n（ORDER BY を付けない）",
            ),
        )
    )
    created = _create(_runtime(llm), "受注金額の合計")
    state = _state(created)
    assert state.status == RequirementPhase.READY, state.message_ja
    skeleton = state.sql_skeleton
    assert "⟨未確定" not in skeleton
    assert skeleton.startswith("-- 重複の除去: 重複を除かない\nSELECT SUM(ORDERS.AMOUNT)\n")
    assert "DISTINCT を付けない" not in skeleton
    assert _clause_lines(skeleton, "WHERE") == ["WHERE ORDERS.STATUS = 'しゅっか済み'"]
    assert _clause_lines(skeleton, "ORDER BY") == []
    # 確定した内容は注記（SQL のコメントの 1 行）に残す。
    assert "-- 絞り込み条件: WHERE による絞り込みは行わず、全件を対象にする" in skeleton
    assert "-- 並び順: 並び順の指定なし （ORDER BY を付けない）" in skeleton


def test_skeleton_after_delegated_order_has_no_undecided() -> None:
    """自由入力で AI に任せた並び順を答えた後の骨組みに ⟨未確定⟩ を出さない。"""

    def review(_context: dict[str, Any]) -> dict[str, Any]:
        return _output(
            _from_orders(),
            _item(
                "select_columns",
                "select",
                "confirmed",
                columns=["APP.ORDERS.ORDER_ID", "APP.ORDERS.AMOUNT"],
                sql_hint="ORDERS.ORDER_ID, ORDERS.AMOUNT",
            ),
        )

    llm = _ScriptedLlm(
        _output(
            _from_orders(),
            _item(
                "select_columns",
                "select",
                "confirmed",
                columns=["APP.ORDERS.ORDER_ID", "APP.ORDERS.AMOUNT"],
                sql_hint="ORDERS.ORDER_ID, ORDERS.AMOUNT",
            ),
            _item(
                "order_by",
                "order_by",
                "missing",
                label="並び順",
                question="結果の並び順は指定しますか？",
                answer_kind="free_text",
            ),
        ),
        review,
    )
    runtime = _runtime(llm)
    # 並び順を求める質問（求めない質問の並び順の不足は聞かない。#1948）。
    created = _create(runtime, "受注の一覧を並べて見せて")
    first = _state(created)
    # 未確定の項目だけが ⟨未確定⟩。絞り込み・件数の上限は「指定なし」で確認しない（#1715）。
    assert "⟨未確定: 並び順⟩" in first.sql_skeleton
    assert "絞り込みの条件" not in first.sql_skeleton
    assert [item.key for item in first.unresolved_items] == ["order_by"]
    data = _answer(runtime, created, free_text="質問の内容から妥当に判断してください")
    state = _state(data)
    assert state.status == RequirementPhase.READY, state.message_ja
    skeleton = state.sql_skeleton
    assert "⟨未確定" not in skeleton
    assert _clause_lines(skeleton, "WHERE") == []
    assert _clause_lines(skeleton, "ORDER BY") == []
    assert "-- 並び順: 質問の内容から妥当に判断してください" in skeleton
    # 件数の上限は質問が求めていないので、骨組みにも入れない（#1718）。
    assert "FETCH FIRST" not in skeleton


def test_settled_items_without_fragment_stay_out_of_a_clause_with_fragments() -> None:
    """断片のある句（期間の条件）に、確定して断片の無い項目（追加の条件なし）を
    ⟨未確定⟩ で足さない。
    """

    llm = _ScriptedLlm(
        _output(
            _from_orders(),
            _select_total(),
            _where_shipped(),
            _item(
                "where_extra",
                "where",
                "inferred",
                label="ほかの絞り込み",
                value="追加の絞り込み条件なし",
            ),
        )
    )
    skeleton = _state(_create(_runtime(llm))).sql_skeleton
    assert "⟨未確定" not in skeleton
    assert _clause_lines(skeleton, "WHERE") == ["WHERE ORDERS.STATUS = 'SHIPPED'"]
    assert "-- ほかの絞り込み: 追加の絞り込み条件なし" in skeleton


# --- 8b. 期間を日付の範囲で確定した後に聞き直さない（#1466） -------------------------


def _fiscal_reask_review(context: dict[str, Any]) -> dict[str, Any]:
    """期間の確定の後の再検査で、年度の開始月・基準日・境界・同じ期間を聞き直す LLM のスタブ。"""

    period = next(
        item for item in context["answered_items"] if item["key"] == "period_this_fiscal_year"
    )
    assert "2026-04-01〜2027-03-31" in period["settled_ja"]
    assert "年度の開始月" in period["settled_ja"]
    return _output(
        _from_orders(),
        _select_total(),
        _item(
            "fiscal_year_start_month",
            "other",
            "missing",
            label="年度の開始月",
            question="「今年度」は何月に始まる年度ですか（例: 4月始まり、1月始まり）。",
            answer_kind="free_text",
        ),
        _item(
            "period_reference_date",
            "period",
            "missing",
            label="期間の基準日",
            question="期間の基準日は実行日ですか？",
            answer_kind="free_text",
        ),
        _item(
            "period_end_inclusive",
            "where",
            "ambiguous",
            label="今年度の終了日",
            question="今年度の終了日（2027-03-31）を含めますか？",
            candidates=[_candidate("含める")],
        ),
        {
            **_fiscal_period_item("ambiguous"),
            "key": "period_fiscal_year_again",
            "question_ja": "「今年度」はどの期間ですか？",
        },
        _item(
            "other_currency",
            "other",
            "missing",
            label="金額の税",
            question="金額は税込と税抜のどちらですか？",
            answer_kind="free_text",
        ),
    )


@pytest.mark.parametrize(
    "free_text", ["", "2026-04-01〜2027-03-31"], ids=["candidate", "free_text_range"]
)
def test_settled_period_does_not_ask_fiscal_start_again(free_text: str) -> None:
    llm = _ScriptedLlm(
        _output(_from_orders(), _select_total(), _fiscal_period_item()), _fiscal_reask_review
    )
    runtime = _runtime(llm)
    after_period = _create(runtime, "今年度の受注金額の合計")
    # 日付の列（ORDERS.ORDER_DATE）と期間（2026-04-01〜2027-03-31）を答える。
    for _ in range(2):
        current = _state(after_period).current_item
        assert current is not None
        assert current.key in {"period_date_column", "period_this_fiscal_year"}
        period_text = free_text if current.key == "period_this_fiscal_year" else ""
        after_period = _answer(runtime, after_period, free_text=period_text)
    assert len(llm.calls) == 2
    # 再検査の入力に、期間が確定したことと、それに含まれる事項を明示する。
    assert "settled_ja" in llm.calls[1]["system"]
    keys = [item.key for item in _items(after_period)]
    assert "fiscal_year_start_month" not in keys
    assert "period_reference_date" not in keys
    assert "period_end_inclusive" not in keys
    assert "period_fiscal_year_again" not in keys
    # 期間に関係しない確認は残す。
    state = _state(after_period)
    assert [item.key for item in state.unresolved_items] == ["other_currency"]
    assert (
        "ORDERS.ORDER_DATE >= DATE '2026-04-01' AND ORDERS.ORDER_DATE < DATE '2027-04-01'"
        in state.sql_skeleton
    )


def test_fiscal_start_is_still_asked_when_the_settled_period_is_not_a_fiscal_year() -> None:
    """「先月」は実行日から日付の範囲が 1 つに決まるので聞かない（#1715）が、年度ごとの集計に要る
    年度の開始月は聞く。"""

    last_month = _item(
        "period_last_month",
        "period",
        "confirmed",
        label="期間",
        value="先月",
        period={
            "expression_ja": "先月",
            "relative_kind": "last_month",
            "count": 0,
            "fiscal_year_start_month": 0,
            "date_column": "APP.ORDERS.ORDER_DATE",
        },
    )
    fiscal_start = _item(
        "fiscal_year_start_month",
        "group_by",
        "missing",
        label="年度の開始月",
        question="年度ごとの集計の年度は何月に始まりますか？",
        answer_kind="free_text",
    )
    llm = _ScriptedLlm(_output(_from_orders(), _select_total(), last_month, fiscal_start))
    data = _create(_runtime(llm), "先月の受注金額の年度ごとの合計")
    period = next(item for item in _items(data) if item.key == "period_last_month")
    assert period.status == RequirementStatus.INFERRED
    assert period.period is not None and period.period.start_date
    keys = [item.key for item in _state(data).unresolved_items]
    assert "period_last_month" not in keys
    assert "fiscal_year_start_month" in keys


# --- 9. 反映・SQL の生成に渡す要件 -----------------------------------------------------


def test_apply_closes_session_and_builds_generation_spec() -> None:
    llm = _ScriptedLlm(
        _output(
            _from_orders(),
            _select_total(),
            _where_shipped(),
            clarified="出荷済み（ORDERS.STATUS = 'SHIPPED'）の ORDERS.AMOUNT の合計",
        )
    )
    runtime = _runtime(llm)
    created = _create(runtime)
    state = created.requirement_state
    assert state is not None
    assert state.can_apply, state.message_ja
    applied = runtime.apply_requirement(created.session.id, RequirementApplyRequest(base_version=1))
    assert applied.session.status == QuerySessionStatus.DONE
    assert applied.session.requirement_applied_at is not None
    # 反映した session には答えられない。
    with pytest.raises(OntologyStateConflictError):
        runtime.answer_clarification(
            created.session.id,
            ClarificationAnswerRequest(
                base_version=1,
                item_id=_items(applied)[0].id,
                free_text="x",
            ),
        )
    # job の質問の文は利用者の質問のまま（入力欄を書き換えない。書き直した文は生成にだけ使う。
    # #1727）。
    question = applied.session.original_question
    spec = runtime.applied_requirement_spec(
        created.session.id,
        actor_user_uuid="user-1",
        actor_is_system_admin=False,
        profile_id="sales",
        question=question,
    )
    assert spec.tables == ["APP.ORDERS"]
    assert any(item.sql_hint == "ORDERS.STATUS = 'SHIPPED'" for item in spec.items)
    block = spec.prompt_block()
    assert "必ず守る条件" in block
    assert "APP.ORDERS" in block
    assert "SUM(ORDERS.AMOUNT)" in block
    for kwargs, code in [
        ({"question": "書き換えた質問"}, "REQUIREMENT_QUESTION_CHANGED"),
        ({"actor_user_uuid": "other-user"}, "REQUIREMENT_SESSION_FORBIDDEN"),
        ({"profile_id": "other"}, "REQUIREMENT_PROFILE_MISMATCH"),
    ]:
        arguments: dict[str, Any] = {
            "actor_user_uuid": "user-1",
            "actor_is_system_admin": False,
            "profile_id": "sales",
            "question": question,
            **kwargs,
        }
        with pytest.raises(OntologyStateConflictError) as raised:
            runtime.applied_requirement_spec(created.session.id, **arguments)
        assert raised.value.code == code


def test_guided_session_does_not_generate_sql_inside_session() -> None:
    llm = _ScriptedLlm(_output(_from_orders(), _select_total(), _where_shipped()))
    runtime = _runtime(llm)
    created = _create(runtime)
    with pytest.raises(OntologyStateConflictError) as raised:
        runtime.generate_sql(
            created.session.id,
            GenerateSqlRequest(
                base_version=1,
                intent_version=1,
                confirm_intent=True,
                ontology_revision_id=created.session.ontology_revision_id,
            ),
        )
    assert raised.value.code == "GUIDED_SESSION_GENERATES_IN_JOB"


# --- 10. 回答の検査・版・二重の送信 -------------------------------------------------------


def _ambiguous_status_output() -> dict[str, Any]:
    return _output(
        _from_orders(),
        _select_total(),
        _item(
            "where_status",
            "where",
            "ambiguous",
            question="どの受注状態に絞りますか？",
            answer_kind="multi_select",
            candidates=[
                _candidate(
                    "出荷済み", columns=["APP.ORDERS.STATUS"], sql_hint="ORDERS.STATUS = 'SHIPPED'"
                ),
                _candidate(
                    "取消", columns=["APP.ORDERS.STATUS"], sql_hint="ORDERS.STATUS = 'CANCELLED'"
                ),
            ],
        ),
    )


def test_answer_validation_rejects_unknown_and_mixed_inputs() -> None:
    runtime = _runtime(_ScriptedLlm(_ambiguous_status_output()))
    created = _create(runtime)
    item = _state(created).current_item
    assert item is not None
    cases: list[tuple[dict[str, Any], str]] = [
        ({"selected_candidate_ids": ["unknown"]}, "表示されていない選択肢"),
        ({"selected_candidate_ids": [item.candidates[0].id], "free_text": "x"}, "どちらか 1 つ"),
        ({}, "選択肢を選ぶか"),
    ]
    for request, message in cases:
        with pytest.raises(OntologyIntegrityError) as raised:
            runtime.answer_clarification(
                created.session.id,
                ClarificationAnswerRequest(base_version=1, item_id=item.id, **request),
            )
        assert message in raised.value.message_ja
    with pytest.raises(OntologyVersionConflictError):
        runtime.answer_clarification(
            created.session.id,
            ClarificationAnswerRequest(base_version=9, item_id=item.id, free_text="x"),
        )


def test_multi_select_answer_keeps_all_selected_conditions() -> None:
    llm = _ScriptedLlm(_ambiguous_status_output(), _output(_from_orders(), _select_total()))
    runtime = _runtime(llm)
    created = _create(runtime)
    item = _state(created).current_item
    assert item is not None
    updated = runtime.answer_clarification(
        created.session.id,
        ClarificationAnswerRequest(
            base_version=1,
            item_id=item.id,
            selected_candidate_ids=[candidate.id for candidate in item.candidates],
        ),
    )
    answered = next(value for value in _items(updated) if value.key == "where_status")
    assert answered.value_ja == "出荷済み、取消"
    assert answered.status == RequirementStatus.CONFIRMED
    assert updated.session.current_intent_version == 2


def test_correcting_inferred_item_from_summary_replaces_only_that_item() -> None:
    llm = _ScriptedLlm(
        _output(_from_orders(), _select_total("inferred"), _where_shipped()),
        _output(_from_orders(), _where_shipped()),
    )
    runtime = _runtime(llm)
    created = _create(runtime)
    select_item = next(item for item in _items(created) if item.key == "select_total")
    corrected = _answer(
        runtime, created, item_id=select_item.id, free_text="ORDERS.AMOUNT の平均を表示する"
    )
    items = {item.key: item for item in _items(corrected)}
    assert items["select_total"].answered is True
    assert items["select_total"].value_ja == "ORDERS.AMOUNT の平均を表示する"
    assert items["select_total"].columns == ["APP.ORDERS.AMOUNT"]
    assert items["where_status"].sql_hint == "ORDERS.STATUS = 'SHIPPED'"


class _OracleSizedIdempotencyStore(InMemoryOntologyStore):
    """Oracle schema の IDEMPOTENCY_KEY VARCHAR2(160) 制約を再現する。"""

    def save_document(
        self,
        collection: OntologyCollection,
        document: Mapping[str, Any] | Any,
        *,
        expected_etag: str | None = None,
    ) -> dict[str, Any]:
        if collection == "idempotency":
            storage_key = str(document["idempotency_key"])
            if len(storage_key.encode("utf-8")) > IDEMPOTENCY_KEY_STORAGE_MAX_BYTES:
                raise RuntimeError("ORA-12899: value too large for IDEMPOTENCY_KEY")
        return super().save_document(collection, document, expected_etag=expected_etag)


async def test_answer_and_apply_are_idempotent_over_http(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = _ScriptedLlm(_ambiguous_status_output(), _output(_from_orders(), _select_total()))
    legacy = _LegacyService()
    legacy._enterprise_ai_client = llm
    runtime = OntologyApiRuntime(legacy_service=legacy, store=_OracleSizedIdempotencyStore())
    runtime._requirement_today = lambda: TODAY  # type: ignore[method-assign]
    created = _create(runtime, actor_user_uuid=local_debug_principal().user_uuid)
    item = _state(created).current_item
    assert item is not None
    monkeypatch.setattr(ontology_router_module, "ontology_runtime", runtime)
    transport = httpx.ASGITransport(app=app)
    key = (
        f"nl2sql:/api/nl2sql/query-sessions/{created.session.id}/clarification-answers:"
        "base_version.free_text.item_id.selected_candidate_ids:"
        "00000000-0000-0000-0000-000000000000"
    )
    payload = {
        "base_version": 1,
        "item_id": item.id,
        "selected_candidate_ids": [item.candidates[0].id],
        "free_text": "",
    }
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        path = f"/api/nl2sql/query-sessions/{created.session.id}"
        response = await client.post(
            f"{path}/clarification-answers", headers={"Idempotency-Key": key}, json=payload
        )
        replay = await client.post(
            f"{path}/clarification-answers", headers={"Idempotency-Key": key}, json=payload
        )
        assert response.status_code == 200, response.text
        assert replay.status_code == 200
        data = response.json()["data"]
        assert data["requirement_state"]["status"] == "ready"
        assert data["requirement_state"]["can_apply"] is True
        applied = await client.post(
            f"{path}/apply",
            headers={"Idempotency-Key": "apply-1"},
            json={"base_version": data["session"]["current_intent_version"]},
        )
        assert applied.status_code == 200, applied.text
        assert applied.json()["data"]["session"]["status"] == "done"
    # 回答の後の再検査を含めて LLM は 2 回だけ（再送では呼ばない）。
    assert len(llm.calls) == 2


# --- 11. 期限切れ・中止 ---------------------------------------------------------------


def _expire_after_ttl(created: QuerySessionData) -> Any:
    ttl = get_settings().nl2sql_query_session_idle_ttl_seconds
    return created.session.updated_at + timedelta(seconds=ttl + 1)


def test_idle_guided_session_expires_and_rejects_answers_without_llm() -> None:
    from app.features.nl2sql.ontology_service import QUERY_SESSION_EXPIRED_CODE

    llm = _ScriptedLlm(_ambiguous_status_output())
    runtime = _runtime(llm)
    created = _create(runtime)
    item = _state(created).current_item
    assert item is not None
    assert runtime.expire_idle_sessions(now=_expire_after_ttl(created)) == 1
    with pytest.raises(OntologyStateConflictError) as raised:
        runtime.answer_clarification(
            created.session.id,
            ClarificationAnswerRequest(base_version=1, item_id=item.id, free_text="今月"),
        )
    assert raised.value.code == QUERY_SESSION_EXPIRED_CODE
    assert len(llm.calls) == 1
    assert runtime.cancel_session(created.session.id).session.status == QuerySessionStatus.CANCELLED


@pytest.mark.parametrize(
    "status",
    [QuerySessionStatus.EXECUTING, QuerySessionStatus.DONE, QuerySessionStatus.ERROR],
    ids=["executing", "done", "error"],
)
def test_session_expiry_skips_executing_and_finished_sessions(status: QuerySessionStatus) -> None:
    runtime = _runtime(_ScriptedLlm(_ambiguous_status_output()))
    created = _create(runtime)
    session = created.session.model_copy(deep=True, update={"status": status})
    runtime.sessions.replace_session(session)
    runtime._persist_session(session)
    assert runtime.expire_idle_sessions(now=_expire_after_ttl(created)) == 0
    assert runtime.get_session(created.session.id).session.status == status


def test_cancelled_guided_session_is_terminal() -> None:
    runtime = _runtime(_ScriptedLlm(_ambiguous_status_output()))
    created = _create(runtime)
    cancelled = runtime.cancel_session(created.session.id)
    assert cancelled.session.status == QuerySessionStatus.CANCELLED
    with pytest.raises(OntologyStateConflictError):
        runtime.apply_requirement(created.session.id, RequirementApplyRequest(base_version=1))


# --- 12. SQL の生成の job に渡す -------------------------------------------------------


def test_job_request_takes_requirement_only_from_applied_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.features.nl2sql import router as nl2sql_router
    from app.features.nl2sql.models import JobCreateRequest, SqlRequirementSpec

    llm = _ScriptedLlm(
        _output(
            _from_orders(),
            _select_total(),
            _where_shipped(),
            clarified="出荷済みの ORDERS.AMOUNT の合計",
        )
    )
    runtime = _runtime(llm)
    created = _create(runtime)
    applied = runtime.apply_requirement(created.session.id, RequirementApplyRequest(base_version=1))
    assert applied.session.requirement is not None
    # 画面は入力欄の値（利用者の質問）を送る。確かめた質問の文は生成にだけ使う（#1727）。
    question = applied.session.original_question
    monkeypatch.setattr(ontology_router_module, "ontology_runtime", runtime)
    principal = SimpleNamespace(user_uuid="user-1", is_system_admin=False)
    forged = SqlRequirementSpec(
        session_id="forged", profile_id="sales", clarified_question="x", tables=["APP.SECRET"]
    )

    # session の無い要件の本文（画面が送ったもの）は使わない。
    plain = nl2sql_router._with_requirement(
        JobCreateRequest(question=question, profile_id="sales", sql_requirement=forged), principal
    )
    assert plain.sql_requirement is None

    request = JobCreateRequest(
        question=question,
        profile_id="sales",
        requirement_session_id=created.session.id,
        sql_requirement=forged,
    )
    resolved = nl2sql_router._with_requirement(request, principal)
    assert resolved.sql_requirement is not None
    assert resolved.sql_requirement.session_id == created.session.id
    assert resolved.sql_requirement.tables == ["APP.ORDERS"]
    assert resolved.allowed_objects.table_names == ["APP.ORDERS"]
    assert resolved.question == "出荷済みの受注金額の合計"
    assert resolved.generation_question == _draft(applied).clarified_question_ja
    # 確かめた質問の文（書き直した文）を質問として送っても、利用者の質問と違うので受け付けない。
    rewritten = request.model_copy(update={"question": _draft(applied).clarified_question_ja})
    with pytest.raises(HTTPException) as raised:
        nl2sql_router._with_requirement(rewritten, principal)
    assert raised.value.status_code == 409

    changed = request.model_copy(update={"question": "書き換えた質問"})
    with pytest.raises(HTTPException) as raised:
        nl2sql_router._with_requirement(changed, principal)
    assert raised.value.status_code == 409


def test_tables_used_by_select_are_added_to_from_and_joined() -> None:
    """実サービスで見つけた食い違い: FROM が 1 表なのに SELECT が別の表の列を使う（#1439）。"""

    llm = _ScriptedLlm(
        _output(
            _from_orders("inferred"),
            _item(
                "select_region",
                "select",
                "inferred",
                columns=["APP.CUSTOMERS.REGION", "APP.ORDERS.AMOUNT"],
                sql_hint="CUSTOMERS.REGION, ORDERS.AMOUNT",
            ),
            _where_shipped(),
            _item("limit_rows", "limit", "inferred", value="100 件", sql_hint="FETCH FIRST 100"),
        )
    )
    created = _create(_runtime(llm), "地域と受注金額")
    draft = _draft(created)
    from_item = next(item for item in draft.items if item.key == "from_orders")
    assert from_item.tables == ["APP.ORDERS", "APP.CUSTOMERS"]
    assert "APP.CUSTOMERS（顧客） を FROM に足しました" in from_item.reason_ja
    join = next(item for item in draft.items if item.key == "join_relationships")
    assert join.sql_hint == "ORDERS.CUSTOMER_ID = CUSTOMERS.CUSTOMER_ID"
    assert "JOIN APP.CUSTOMERS ON ORDERS.CUSTOMER_ID = CUSTOMERS.CUSTOMER_ID" in draft.sql_skeleton


# --- 13. MCP（Agent）からの回答前の確認（#1441） ------------------------------------------


class _McpJobs:
    """MCP の nl2sql_query が作るジョブを記録し、すぐ完了させる fake の service。"""

    def __init__(self) -> None:
        self.started: list[Any] = []
        # 業務プロファイルの「曖昧な質問の扱い」（#1839）。
        self.clarification_policy = "confirm"

    def ensure_persistence_available(self) -> None:
        return None

    def get_profile(self, profile_id: str, **_kwargs: Any) -> Any:
        from app.features.nl2sql.models import Nl2SqlProfile

        return Nl2SqlProfile(
            id=profile_id, name=profile_id.upper(), clarification_policy=self.clarification_policy
        )

    def start_job(self, request: Any, **_kwargs: Any) -> Any:
        from app.features.nl2sql.models import JobCreateData, JobStatus

        self.started.append(request)
        return JobCreateData(
            job_id=f"job-{len(self.started)}",
            status=JobStatus.PENDING,
            created_at="2026-10-11T00:00:00+00:00",
        )

    def get_job(self, job_id: str, **_kwargs: Any) -> Any:
        from app.features.nl2sql.models import (
            JobData,
            JobStatus,
            Nl2SqlEngine,
            Nl2SqlResult,
            QueryResults,
            SafetyReport,
            TimingEnvelope,
        )

        request = self.started[int(job_id.removeprefix("job-")) - 1]
        return JobData(
            job_id=job_id,
            status=JobStatus.DONE,
            profile_id=request.profile_id,
            created_at="2026-10-11T00:00:00+00:00",
            result=Nl2SqlResult(
                history_id="history-1",
                engine=Nl2SqlEngine.ENTERPRISE_AI_DIRECT,
                original_question=request.question,
                rewritten_question=request.question,
                generated_sql="SELECT SUM(AMOUNT) FROM APP.ORDERS WHERE STATUS = 'SHIPPED'",
                executable_sql="SELECT SUM(AMOUNT) FROM APP.ORDERS WHERE STATUS = 'SHIPPED'",
                explanation="出荷済みの受注金額の合計です。",
                safety=SafetyReport(is_safe=True, is_select_only=True, row_limit_applied=100),
                results=QueryResults(columns=["TOTAL"], rows=[{"TOTAL": 1200}], total=1),
                timing=TimingEnvelope(created_at="2026-10-11T00:00:00+00:00"),
            ),
        )


@pytest.fixture
def mcp_requirement(monkeypatch: pytest.MonkeyPatch) -> Callable[[_ScriptedLlm], _McpJobs]:
    """MCP を、スタブの LLM の回答前の確認と fake のジョブで動かす（認証は無効）。"""

    from app.features.mcp import tools as mcp_tools

    monkeypatch.setattr(get_settings(), "auth_mode", "local")
    monkeypatch.setattr(mcp_tools, "JOB_POLL_INTERVAL_SECONDS", 0.01)

    def install(llm: _ScriptedLlm) -> _McpJobs:
        jobs = _McpJobs()
        runtime = _runtime(llm)
        monkeypatch.setattr(ontology_router_module, "ontology_runtime", runtime)
        monkeypatch.setattr(mcp_tools, "nl2sql_service", jobs)
        return jobs

    return install


def _mcp(name: str, arguments: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    import asyncio

    async def post() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                "/api/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                },
            )

    response = asyncio.run(post())
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return result["isError"], result["structuredContent"]


def test_mcp_query_asks_one_question_then_generates_after_answer(
    mcp_requirement: Callable[[_ScriptedLlm], _McpJobs],
) -> None:
    llm = _ScriptedLlm(
        _ambiguous_status_output(),
        _output(
            _from_orders(),
            _select_total(),
            clarified="出荷済み（ORDERS.STATUS = 'SHIPPED'）の ORDERS.AMOUNT の合計",
        ),
    )
    jobs = mcp_requirement(llm)
    question = {"question": "出荷済みの受注金額の合計", "profile_id": "sales"}

    is_error, first = _mcp("nl2sql_query", question)
    assert is_error is False, first
    # 確かめる項目がある間は SQL を作らない（ジョブを作らない）。
    assert first["status"] == "needs_clarification"
    assert first["job_id"] is None
    assert jobs.started == []
    requirement = first["requirement"]
    assert requirement["status"] == "needs_answer"
    assert requirement["remaining_count"] == 1
    asked = requirement["question"]
    assert asked["question"] == "どの受注状態に絞りますか？"
    assert asked["section"] == "where"
    assert asked["answer_kind"] == "multi_select"
    # 候補・項目の表・列は物理名で返す。
    assert [candidate["label"] for candidate in asked["candidates"]] == ["出荷済み", "取消"]
    assert asked["candidates"][0]["refs"] == "ORDERS.STATUS（受注状態）"

    shipped = asked["candidates"][0]["id"]
    is_error, done = _mcp(
        "nl2sql_query",
        {
            **question,
            "requirement_session_id": requirement["session_id"],
            "answer": {"item_id": asked["item_id"], "selected_candidate_ids": [shipped]},
        },
    )
    assert is_error is False, done
    assert done["status"] == "done"
    assert done["rows"] == [{"TOTAL": 1200}]
    assert done["requirement"]["status"] == "applied"
    # 回答で要件を更新し、確かめた要件（書き直した質問の文・構造化した要件・表）で SQL を作る。
    assert len(jobs.started) == 1
    job = jobs.started[0]
    # job の質問の文は利用者の質問のまま、生成は確かめた質問の文で行う（#1727）。
    assert job.question == "出荷済みの受注金額の合計"
    assert job.generation_question == done["requirement"]["clarified_question"]
    assert job.requirement_session_id == requirement["session_id"]
    assert job.sql_requirement is not None
    assert job.sql_requirement.tables == ["APP.ORDERS"]
    assert any("SHIPPED" in item.sql_hint for item in job.sql_requirement.items)
    assert job.allowed_objects.table_names == ["APP.ORDERS"]
    # 下書きと、回答の後の再検査の 2 回。
    assert len(llm.calls) == 2


def test_mcp_query_without_questions_generates_without_asking(
    mcp_requirement: Callable[[_ScriptedLlm], _McpJobs],
) -> None:
    llm = _ScriptedLlm(
        _output(_from_orders(), _select_total(), _where_shipped(), clarified="出荷済みの合計")
    )
    jobs = mcp_requirement(llm)
    is_error, content = _mcp(
        "nl2sql_query", {"question": "出荷済みの受注金額の合計", "profile_id": "sales"}
    )
    assert is_error is False, content
    assert content["status"] == "done"
    assert content["requirement"]["status"] == "applied"
    assert content["requirement"]["question"] is None
    assert len(jobs.started) == 1
    assert len(llm.calls) == 1


def test_mcp_query_does_not_generate_until_select_and_from_are_clear(
    mcp_requirement: Callable[[_ScriptedLlm], _McpJobs],
) -> None:
    llm = _ScriptedLlm(_output(_item("from_tables", "from", "missing", question="どの表？")))
    jobs = mcp_requirement(llm)
    question = {"question": "いい感じに出して", "profile_id": "sales"}
    is_error, content = _mcp("nl2sql_query", question)
    assert is_error is False, content
    assert content["status"] == "needs_clarification"
    requirement = content["requirement"]
    assert requirement["select_from_clear"] is False
    assert len(requirement["minimum_missing"]) == 2
    assert requirement["question"]["section"] in {"from", "select"}
    assert jobs.started == []


def test_mcp_query_with_assume_policy_does_not_ask_and_generates_without_check(
    mcp_requirement: Callable[[_ScriptedLlm], _McpJobs],
) -> None:
    """業務プロファイルが「前提を示して SQL を生成する」なら、確認事項があっても聞かない（#1839）。

    要件をまだ反映できない（確認事項が残り、再検査の上限の前）ので、確認を使わずに作る。
    """

    llm = _ScriptedLlm(_ambiguous_status_output())
    jobs = mcp_requirement(llm)
    jobs.clarification_policy = "assume"
    is_error, content = _mcp(
        "nl2sql_query", {"question": "出荷済みの受注金額の合計", "profile_id": "sales"}
    )
    assert is_error is False, content
    assert content["status"] == "done"
    assert content["requirement"]["status"] == "skipped"
    assert len(jobs.started) == 1
    assert jobs.started[0].requirement_session_id is None


def test_mcp_query_with_assume_policy_still_applies_a_clear_requirement(
    mcp_requirement: Callable[[_ScriptedLlm], _McpJobs],
) -> None:
    llm = _ScriptedLlm(
        _output(_from_orders(), _select_total(), _where_shipped(), clarified="出荷済みの合計")
    )
    jobs = mcp_requirement(llm)
    jobs.clarification_policy = "assume"
    is_error, content = _mcp(
        "nl2sql_query", {"question": "出荷済みの受注金額の合計", "profile_id": "sales"}
    )
    assert is_error is False, content
    assert content["requirement"]["status"] == "applied"
    assert jobs.started[0].requirement_session_id == content["requirement"]["session_id"]


def test_mcp_query_with_assume_policy_skips_check_when_select_and_from_are_unclear(
    mcp_requirement: Callable[[_ScriptedLlm], _McpJobs],
) -> None:
    """SELECT / FROM が決まらず要件を反映できないときは、確認を使わずに作る（#1839）。"""

    llm = _ScriptedLlm(_output(_item("from_tables", "from", "missing", question="どの表？")))
    jobs = mcp_requirement(llm)
    jobs.clarification_policy = "assume"
    is_error, content = _mcp(
        "nl2sql_query", {"question": "いい感じに出して", "profile_id": "sales"}
    )
    assert is_error is False, content
    assert content["status"] == "done"
    assert content["requirement"]["status"] == "skipped"
    assert "前提" in content["requirement"]["message"]
    assert len(jobs.started) == 1
    assert jobs.started[0].requirement_session_id is None


def test_mcp_query_skip_clarification_generates_without_asking_after_a_question(
    mcp_requirement: Callable[[_ScriptedLlm], _McpJobs],
) -> None:
    """利用者が「確認せずに SQL を生成」を選んだら、確認を閉じて前提を示して作る（#1839）。"""

    llm = _ScriptedLlm(_ambiguous_status_output())
    jobs = mcp_requirement(llm)
    question = {"question": "出荷済みの受注金額の合計", "profile_id": "sales"}
    is_error, first = _mcp("nl2sql_query", question)
    assert is_error is False, first
    assert first["status"] == "needs_clarification"
    session_id = first["requirement"]["session_id"]

    is_error, done = _mcp(
        "nl2sql_query",
        {**question, "requirement_session_id": session_id, "skip_clarification": True},
    )
    assert is_error is False, done
    assert done["status"] == "done"
    assert done["requirement"]["status"] == "skipped"
    assert "利用者が確認せずに" in done["requirement"]["message"]
    assert len(jobs.started) == 1
    assert jobs.started[0].requirement_session_id is None
    # 開いていた確認は閉じる（続きで使わない）。
    is_error, again = _mcp(
        "nl2sql_query",
        {
            **question,
            "requirement_session_id": session_id,
            "answer": {"item_id": "x", "free_text": "出荷済み"},
        },
    )
    assert is_error is True, again


def test_mcp_query_skip_clarification_cannot_be_combined_with_an_answer(
    mcp_requirement: Callable[[_ScriptedLlm], _McpJobs],
) -> None:
    mcp_requirement(_ScriptedLlm(_ambiguous_status_output()))
    is_error, content = _mcp(
        "nl2sql_query",
        {
            "question": "出荷済みの受注金額の合計",
            "profile_id": "sales",
            "requirement_session_id": "s1",
            "answer": {"item_id": "x", "free_text": "y"},
            "skip_clarification": True,
        },
    )
    assert is_error is True, content


def test_mcp_query_unanswerable_closes_session_without_sql(
    mcp_requirement: Callable[[_ScriptedLlm], _McpJobs],
) -> None:
    llm = _ScriptedLlm(
        _output(
            _from_orders(),
            _select_total(),
            unanswerable="この業務プロファイルでは答えられません",
            answerable=False,
        )
    )
    jobs = mcp_requirement(llm)
    is_error, content = _mcp("nl2sql_query", {"question": "秘密の表", "profile_id": "sales"})
    assert is_error is False, content
    assert content["status"] == "unanswerable"
    assert content["error_code"] == "REQUIREMENT_UNANSWERABLE"
    assert "答えられません" in content["error_message"]
    assert jobs.started == []
    runtime = ontology_router_module.ontology_runtime
    session = runtime.get_session(content["requirement"]["session_id"]).session
    assert session.status == QuerySessionStatus.CANCELLED


def test_mcp_cancel_requirement_closes_session_and_rejects_answers(
    mcp_requirement: Callable[[_ScriptedLlm], _McpJobs],
) -> None:
    jobs = mcp_requirement(_ScriptedLlm(_ambiguous_status_output()))
    question = {"question": "出荷済みの受注金額の合計", "profile_id": "sales"}
    _, first = _mcp("nl2sql_query", question)
    session_id = first["requirement"]["session_id"]

    is_error, cancelled = _mcp("nl2sql_cancel_requirement", {"requirement_session_id": session_id})
    assert (is_error, cancelled) == (
        False,
        {
            "schema_version": MCP_OUTPUT_SCHEMA_VERSION,
            "requirement_session_id": session_id,
            "status": "cancelled",
        },
    )
    runtime = ontology_router_module.ontology_runtime
    assert runtime.get_session(session_id).session.status == QuerySessionStatus.CANCELLED
    # 2 回目は閉じた後（取り消しの操作を繰り返しても失敗にしない）。
    _, again = _mcp("nl2sql_cancel_requirement", {"requirement_session_id": session_id})
    assert again["status"] == "closed"

    item = first["requirement"]["question"]
    is_error, error = _mcp(
        "nl2sql_query",
        {
            **question,
            "requirement_session_id": session_id,
            "answer": {"item_id": item["item_id"], "free_text": "出荷済み"},
        },
    )
    assert is_error is True
    assert "終了しています" in error["message"]
    is_error, error = _mcp("nl2sql_cancel_requirement", {"requirement_session_id": "missing"})
    assert is_error is True
    assert error["status"] == 404
    assert jobs.started == []


def test_mcp_query_answer_requires_session_and_an_answer(
    mcp_requirement: Callable[[_ScriptedLlm], _McpJobs],
) -> None:
    mcp_requirement(_ScriptedLlm(_ambiguous_status_output()))
    for arguments in (
        {"answer": {"item_id": "x", "free_text": "y"}},
        {"requirement_session_id": "s", "answer": {"item_id": "x"}},
        # 画面と同じく、候補と自由入力のどちらか一方だけで答える。
        {
            "requirement_session_id": "s",
            "answer": {"item_id": "x", "selected_candidate_ids": ["a"], "free_text": "b"},
        },
    ):
        is_error, error = _mcp(
            "nl2sql_query", {"question": "出荷済み", "profile_id": "sales", **arguments}
        )
        assert is_error is True
        assert error["error_code"] == "MCP_TOOL_ARGUMENTS_INVALID"


# --- #1718 指定なし・要確認・条件を追加・件数の上限 -------------------------------------


def _employee_like_output() -> dict[str, Any]:
    """`select * from employee` 相当: 確定 2 つと、値の無い推測（＝指定なし）の部分。"""

    return _output(
        _from_orders(),
        _item(
            "select_all",
            "select",
            "confirmed",
            label="表示列",
            value="APP.ORDERS.*",
            tables=["APP.ORDERS"],
            sql_hint="ORDERS.*",
        ),
        _item("join_none", "join", "inferred", label="結合"),
        _item(
            "period_none",
            "period",
            "inferred",
            label="期間",
            columns=["APP.ORDERS.ORDER_DATE"],
        ),
        _item("where_none", "where", "inferred", label="絞り込み"),
        _item("group_none", "group_by", "inferred", label="集計"),
        _item("order_none", "order_by", "inferred", label="並び順"),
        _item("limit_none", "limit", "missing", label="件数上限", question="何件まで出しますか？"),
        _item("subquery_none", "subquery", "inferred", label="サブクエリ"),
        _item("other_none", "other", "inferred", label="その他"),
    )


def test_items_without_value_are_unspecified_not_unresolved() -> None:
    from app.features.nl2sql.sql_requirement import is_unspecified

    llm = _ScriptedLlm(_employee_like_output())
    created = _create(_runtime(llm), "select * from orders")
    state = _state(created)
    items = _items(created)
    # 値のある 2 つ（確定）だけが一覧に出し、残りは「指定なし」。件数の上限を「不足」にしない。
    valued = [item for item in items if not is_unspecified(item)]
    assert {item.key for item in valued} == {"from_orders", "select_all"}
    limit = next(item for item in items if item.key == "limit_none")
    assert limit.status == RequirementStatus.INFERRED
    assert limit.question_ja == ""
    assert state.unresolved_items == []
    assert state.can_apply
    assert "（指定なし）" in state.summary_ja
    assert "未確認" not in state.summary_ja
    assert "FETCH FIRST" not in state.sql_skeleton


def test_limit_is_kept_when_the_question_asks_for_top_n() -> None:
    llm = _ScriptedLlm(
        _output(
            _from_orders(),
            _select_total(),
            _item(
                "limit_top",
                "limit",
                "confirmed",
                label="件数上限",
                value="上位 5 件",
                sql_hint="FETCH FIRST 5 ROWS ONLY",
            ),
            _item(
                "limit_ambiguous",
                "limit",
                "ambiguous",
                label="上位の件数",
                question="上位何件を出しますか？",
                candidates=[_candidate("5 件", evidence="業務プロファイルの既定")],
            ),
        )
    )
    created = _create(_runtime(llm), "受注金額の上位")
    items = {item.key: item for item in _items(created)}
    assert items["limit_top"].value_ja == "上位 5 件"
    # 件数の解釈が複数あるときは聞く（不足だけを指定なしにする）。
    assert items["limit_ambiguous"].status == RequirementStatus.AMBIGUOUS
    assert "FETCH FIRST 5 ROWS ONLY" in _state(created).sql_skeleton


def test_add_condition_to_an_unspecified_section_creates_a_confirmed_item() -> None:
    llm = _ScriptedLlm(
        _output(_from_orders(), _select_total(), _where_shipped()),
        # 追加の後の再検査（足りない情報なし）。
        lambda _context: _output(_from_orders(), _select_total(), _where_shipped()),
    )
    runtime = _runtime(llm)
    created = _create(runtime)
    assert _state(created).unresolved_items == []
    answered = runtime.answer_clarification(
        created.session.id,
        ClarificationAnswerRequest(
            base_version=created.session.current_intent_version,
            item_id="add:order_by",
            free_text="ORDERS.AMOUNT の多い順",
        ),
    )
    added = [item for item in _items(answered) if item.section == RequirementSection.ORDER_BY]
    assert len(added) == 1
    assert added[0].answered
    assert added[0].status == RequirementStatus.CONFIRMED
    assert added[0].value_ja == "ORDERS.AMOUNT の多い順"
    assert added[0].columns == ["APP.ORDERS.AMOUNT"]
    assert answered.session.requirement_answers[-1].item_id == "add:order_by"


def test_add_condition_reuses_the_unspecified_item_of_the_section() -> None:
    llm = _ScriptedLlm(
        _employee_like_output(),
        lambda _context: _output(_from_orders()),
    )
    runtime = _runtime(llm)
    created = _create(runtime, "select * from orders")
    answered = runtime.answer_clarification(
        created.session.id,
        ClarificationAnswerRequest(
            base_version=created.session.current_intent_version,
            item_id="add:where",
            free_text="ORDERS.STATUS が SHIPPED",
        ),
    )
    where = [item for item in _items(answered) if item.section == RequirementSection.WHERE]
    assert [item.key for item in where] == ["where_none"]
    assert where[0].answered


@pytest.mark.parametrize(
    "item_id",
    ["add:select", "add:from", "add:unknown", "add:"],
    ids=["select", "from", "unknown", "empty"],
)
def test_add_condition_rejects_sections_that_cannot_be_added(item_id: str) -> None:
    llm = _ScriptedLlm(_output(_from_orders(), _select_total()))
    runtime = _runtime(llm)
    created = _create(runtime)
    with pytest.raises(OntologyIntegrityError):
        runtime.answer_clarification(
            created.session.id,
            ClarificationAnswerRequest(
                base_version=created.session.current_intent_version,
                item_id=item_id,
                free_text="何か",
            ),
        )


# --- #1761: 質問 0 件でも、対象の表を選び直し・その他（自由入力）で直せる ---------------------


def _select_all(status: str = "inferred") -> dict[str, Any]:
    return _item("select_all", "select", status, label="全列", value="*", sql_hint="*")


def test_prompt_asks_target_table_when_several_tables_fit() -> None:
    """表が複数当てはまる短い入力（「employee」）は、推測で 1 つに決めず対象を選ぶ質問にする。"""
    from app.features.nl2sql.sql_requirement import REQUIREMENT_PROMPT_VERSION, SYSTEM_PROMPT

    assert REQUIREMENT_PROMPT_VERSION == "sql_requirement_v7_steps"
    assert "表を推測で 1 つに決めないでください" in SYSTEM_PROMPT
    assert "「employee」" in SYSTEM_PROMPT
    # 最低限の要件の方針（細部まで聞かない）は保つ。
    assert "細部まで確認させません" in SYSTEM_PROMPT
    # 「その他（自由入力）」の回答は、AI の解釈を直す指示として再検査に渡す。
    assert "section=other の answered_items" in SYSTEM_PROMPT


def test_inferred_table_offers_the_profile_tables_as_choices_without_asking() -> None:
    """AI が推測で決めた表には、業務プロファイルの表を選び直す候補を添える（質問にはしない）。"""
    llm = _ScriptedLlm(_output(_from_orders("inferred"), _select_all()))
    created = _create(_runtime(llm), "受注")
    state = _state(created)
    assert state.unresolved_items == []
    assert state.current_item is None
    assert state.can_apply is True
    from_item = next(item for item in _items(created) if item.section == RequirementSection.FROM)
    assert from_item.status == RequirementStatus.INFERRED
    assert [candidate.tables for candidate in from_item.candidates] == [
        ["APP.ORDERS"],
        ["APP.CUSTOMERS"],
    ]
    assert from_item.answer_kind.value == "single_select"


def test_table_named_in_the_question_gets_no_choices() -> None:
    """質問が表の名前を書いている（`select * from orders`）ときは候補を添えない。"""
    llm = _ScriptedLlm(_output(_from_orders(), _select_all("confirmed")))
    created = _create(_runtime(llm), "select * from orders;")
    from_item = next(item for item in _items(created) if item.section == RequirementSection.FROM)
    assert from_item.candidates == []


def test_choosing_another_table_from_the_confirmation_changes_from_and_reviews() -> None:
    llm = _ScriptedLlm(
        _output(_from_orders("inferred"), _select_all()),
        # 選び直した後の再検査（APP.CUSTOMERS の全列）。
        lambda _context: _output(_select_all(), clarified="APP.CUSTOMERS の全列を表示する"),
    )
    runtime = _runtime(llm)
    created = _create(runtime, "受注")
    from_item = next(item for item in _items(created) if item.section == RequirementSection.FROM)
    answered = _answer(runtime, created, item_id=from_item.id, candidate_index=1)
    assert len(llm.calls) == 2
    assert llm.calls[1]["context"]["mode"] == "review"
    items = {item.key: item for item in _items(answered)}
    assert items["from_orders"].answered is True
    assert items["from_orders"].tables == ["APP.CUSTOMERS"]
    # 答えた表には、もう候補を添えない。
    assert items["from_orders"].candidates == []
    assert _state(answered).can_apply is True


def test_other_free_text_is_sent_to_review_as_a_correction() -> None:
    """「その他（自由入力）」（`add:other`）は AI の再検査に直しの指示として渡す。

    書かれた表を FROM に足さない（再検査の結果の FROM のまま）。
    """

    def review(context: dict[str, Any]) -> dict[str, Any]:
        answered = context["answered_items"]
        assert any(
            value["section"] == "other" and "APP.CUSTOMERS" in value["answer_ja"]
            for value in answered
        )
        return _output(
            _item(
                "from_orders",
                "from",
                "confirmed",
                value="APP.CUSTOMERS",
                tables=["APP.CUSTOMERS"],
            ),
            _select_all(),
            clarified="APP.CUSTOMERS の全列を表示する",
        )

    llm = _ScriptedLlm(_output(_from_orders("inferred"), _select_all()), review)
    runtime = _runtime(llm)
    created = _create(runtime, "受注")
    answered = runtime.answer_clarification(
        created.session.id,
        ClarificationAnswerRequest(
            base_version=created.session.current_intent_version,
            item_id="add:other",
            free_text="APP.CUSTOMERS の全列にしてください",
        ),
    )
    other = next(item for item in _items(answered) if item.section == RequirementSection.OTHER)
    assert other.answered is True
    # 再検査の指示は、前のまとめの検査ではなく、直しの指示での作り直し。
    review_prompt = llm.calls[1]["prompt"]
    assert "利用者の直しの指示（最後が最新）: APP.CUSTOMERS の全列にしてください" in review_prompt
    assert "この指示を反映して要件を作り直してください" in review_prompt
    assert other.tables == []
    assert other.columns == []
    draft = _draft(answered)
    assert draft.clarified_question_ja == "APP.CUSTOMERS の全列を表示する"
    from_item = next(item for item in draft.items if item.section == RequirementSection.FROM)
    # 再検査の結果のとおり、FROM は APP.CUSTOMERS だけ（自由入力の表を足して 2 つにしない）。
    assert from_item.tables == ["APP.CUSTOMERS"]
    assert _state(answered).review_round == 1


# --- #1917: 確認事項の上限と優先度・選択肢の名前（内部の値を出さない） ----------------------


def _many_open_items_output() -> dict[str, Any]:
    """実モデルが同じ質問で多くの確認事項を返した形（#1917。期間・条件・集計・並び順・そのほか）。"""
    return _output(
        _from_orders(),
        _select_total(),
        _item(
            "period_this_month",
            "period",
            "ambiguous",
            label="期間",
            value="今月",
            question="今月はどの期間ですか？",
            period={
                "expression_ja": "今月",
                "relative_kind": "this_month",
                "count": 0,
                "fiscal_year_start_month": 0,
                "date_column": "APP.ORDERS.ORDER_DATE",
            },
        ),
        _item(
            "order_by_amount",
            "order_by",
            "ambiguous",
            label="並び順",
            question="どの順に並べますか？",
            candidates=[
                _candidate("金額の多い順", sql_hint="SUM(ORDERS.AMOUNT) DESC"),
                _candidate("金額の少ない順", sql_hint="SUM(ORDERS.AMOUNT) ASC"),
            ],
        ),
        _item(
            "group_by_customer",
            "group_by",
            "ambiguous",
            label="集計の単位",
            question="何の単位で集計しますか？",
            candidates=[
                _candidate(
                    "顧客ごと",
                    columns=["APP.ORDERS.CUSTOMER_ID"],
                    sql_hint="ORDERS.CUSTOMER_ID",
                ),
                _candidate("月ごと", sql_hint="TRUNC(ORDERS.ORDER_DATE, 'MM')"),
            ],
        ),
        _item(
            "where_status",
            "where",
            "missing",
            label="受注状態",
            question="どの受注状態に絞りますか？",
            columns=["APP.ORDERS.STATUS"],
            answer_kind="free_text",
        ),
        _item(
            "other_rounding",
            "other",
            "missing",
            label="金額の丸め",
            question="金額を丸めますか？",
            answer_kind="free_text",
        ),
    )


def test_open_questions_are_capped_by_priority() -> None:
    """確認事項は答えが大きく変わるものから上限（3 件）まで。超えた分は AI の推測（#1917）。"""
    from app.features.nl2sql.sql_requirement import MAX_OPEN_QUESTIONS

    llm = _ScriptedLlm(_many_open_items_output())
    created = _create(_runtime(llm), "今月の受注金額を集計して多い順に")
    state = _state(created)
    keys = [item.key for item in state.unresolved_items]
    assert len(keys) == MAX_OPEN_QUESTIONS == 3
    # 期間（日付の列と範囲）→ WHERE の順に残す（集計・並び順・そのほかは推測）。
    assert sorted(keys) == ["period_date_column", "period_this_month", "where_status"]
    items = {item.key: item for item in _items(created)}
    group_by = items["group_by_customer"]
    assert group_by.status == RequirementStatus.INFERRED
    # 候補があれば先頭の候補を推測にする（値と SQL の断片を候補から写す）。
    assert group_by.value_ja == "顧客ごと"
    assert group_by.sql_hint == "ORDERS.CUSTOMER_ID"
    assert group_by.candidates == []
    assert group_by.question_ja == ""
    assert "何の単位で集計しますか" in group_by.reason_ja
    order_by = items["order_by_amount"]
    assert order_by.status == RequirementStatus.INFERRED
    assert order_by.value_ja == "金額の多い順"
    # 候補の無い項目は、SQL の生成で AI が判断する（値に確認の内容を残す）。
    other = items["other_rounding"]
    assert other.status == RequirementStatus.INFERRED
    assert "金額を丸めますか" in other.value_ja
    assert any("3 件までにしました" in warning for warning in _draft(created).warnings_ja)


def test_open_question_cap_keeps_from_and_select() -> None:
    """最低の基準（FROM・SELECT）は上限を超えても推測にしない（SQL の生成に進めなくなる）。"""
    output = _output(
        _item(
            "from_target",
            "from",
            "ambiguous",
            question="どの表ですか？",
            candidates=[_candidate("APP.ORDERS", tables=["APP.ORDERS"])],
        ),
        _item("select_metric", "select", "missing", question="何を出しますか？"),
        _item("select_metric_2", "select", "missing", question="ほかに何を出しますか？"),
        _item("select_metric_3", "select", "missing", question="さらに何を出しますか？"),
        _item(
            "order_by_x",
            "order_by",
            "ambiguous",
            question="並び順は？",
            candidates=[_candidate("金額の多い順", sql_hint="ORDERS.AMOUNT DESC")],
        ),
    )
    question = "受注"
    draft = build_draft(
        LlmRequirementOutput.model_validate(output),
        _requirement_context(question),
        question=question,
    )
    open_keys = [
        item.key
        for item in draft.items
        if item.status in {RequirementStatus.MISSING, RequirementStatus.AMBIGUOUS}
    ]
    assert open_keys == ["from_target", "select_metric", "select_metric_2", "select_metric_3"]
    order_by = next(item for item in draft.items if item.key == "order_by_x")
    assert order_by.status == RequirementStatus.INFERRED


def test_answering_keeps_open_questions_within_the_cap() -> None:
    """答えた後に規則が推測の項目を開き直しても、1 回に出す確認事項は上限の中（#1917）。"""
    llm = _ScriptedLlm(_many_open_items_output())
    runtime = _runtime(llm)
    created = _create(runtime, "今月の受注金額を集計して多い順に")
    answered = _answer(runtime, created)
    assert 0 < len(_state(answered).unresolved_items) <= 3


def test_internal_values_are_not_shown_as_names() -> None:
    """`this_month` のような内部の値を、選択肢・項目の名前・質問の文に出さない（#1917）。"""
    question = "今月の受注金額の合計を状態で絞って"
    period_item = _item(
        "period",
        "period",
        "ambiguous",
        label="period",
        value="this_month",
        question="this_month はどの期間ですか？",
        period={
            "expression_ja": "this_month",
            "relative_kind": "this_month",
            "count": 0,
            "fiscal_year_start_month": 0,
            "date_column": "APP.ORDERS.ORDER_DATE",
        },
    )
    where_item = _item(
        "where_period_kind",
        "where",
        "ambiguous",
        label="where_kind",
        question="期間の種類は？",
        candidates=[
            _candidate("this_month", evidence="質問の「今月」"),
            _candidate("last_month", evidence="質問の「今月」"),
            # Schema の名前に直せない内部の値は、説明を名前にする。
            {**_candidate("order_date_basis"), "description_ja": "受注日"},
            # 名前にできるものが無い候補は出さない（その他（自由入力）で答えられる）。
            _candidate("ship_basis_x"),
        ],
    )
    output = LlmRequirementOutput.model_validate(
        _output(_from_orders(), _select_total(), period_item, where_item)
    )
    draft = build_draft(output, _requirement_context(question), question=question)
    period = next(item for item in draft.items if item.key == "period")
    assert period.period is not None
    assert period.period.expression_ja == "今月"
    assert period.label_ja == "期間（WHERE）"
    where = next(item for item in draft.items if item.key == "where_period_kind")
    assert [candidate.label_ja for candidate in where.candidates] == ["今月", "先月", "受注日"]
    assert where.label_ja == "条件（WHERE）"
    texts = _texts(draft)
    for value in ("this_month", "last_month", "order_date_basis", "ship_basis_x", "where_kind"):
        assert not any(value in text for text in texts), value


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("this_month", "今月"),
        ("THIS_MONTH", "今月"),
        ("last_fiscal_year", "昨年度"),
        ("this_month の売上", "今月 の売上"),
        ("ORDERS.ORDER_DATE", "ORDERS.ORDER_DATE"),
        ("今月", "今月"),
    ],
    ids=["kind", "upper", "fiscal", "in_text", "physical_name", "japanese"],
)
def test_display_name_replaces_period_kind_values(value: str, expected: str) -> None:
    from app.features.nl2sql.sql_requirement import display_name

    assert display_name(value) == expected


# --- #1948: 日付で書かれた期間は聞き返さず、質問に無い集計の単位・並び順は聞かない -----------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2025年4月1日から2025年6月30日まで", ("2025-04-01", "2025-06-30")),
        ("2025-04-01〜2025-06-30", ("2025-04-01", "2025-06-30")),
        ("2025 年 4 月から 6 月まで", ("2025-04-01", "2025-06-30")),
        ("2024年11月から2025年2月", ("2024-11-01", "2025-02-28")),
        ("2025 年 5 月", ("2025-05-01", "2025-05-31")),
        ("２０２５年５月１日", ("2025-05-01", "2025-05-01")),
        ("2025 年度", None),
        ("今月", None),
        ("2025年13月", None),
    ],
    ids=[
        "two_dates",
        "iso",
        "month_range",
        "across_years",
        "month",
        "day",
        "fiscal",
        "relative",
        "bad",
    ],
)
def test_absolute_period_range(text: str, expected: tuple[str, str] | None) -> None:
    from app.features.nl2sql.sql_requirement import absolute_period_range

    result = absolute_period_range(text)
    assert (None if result is None else (result[0].isoformat(), result[1].isoformat())) == expected


def test_period_written_with_dates_is_not_asked_again() -> None:
    """日付で書いた期間を「いつからいつまでですか？」と聞き返さない（#1948。実モデルの評価で見つけた）。"""
    question = "2025 年 4 月から 6 月までの受注金額の合計はいくらですか？"
    period_item = _item(
        "period_q1",
        "period",
        "ambiguous",
        label="期間",
        value="2025年4月から6月まで",
        period={
            "expression_ja": "2025年4月1日から2025年6月30日まで",
            "relative_kind": "other",
            "count": 0,
            "fiscal_year_start_month": 0,
            "date_column": "APP.ORDERS.ORDER_DATE",
        },
    )
    output = LlmRequirementOutput.model_validate(
        _output(_from_orders(), _select_total(), period_item)
    )
    draft = build_draft(output, _requirement_context(question), question=question)
    period = next(item for item in draft.items if item.key == "period_q1")
    assert period.status == RequirementStatus.CONFIRMED
    assert period.question_ja == ""
    assert period.period is not None
    assert (period.period.start_date, period.period.end_date) == ("2025-04-01", "2025-06-30")
    assert period.value_ja == "2025-04-01〜2025-06-30"


def test_unrequested_grouping_and_ordering_are_not_asked() -> None:
    """質問に無い集計の単位・並び順の不足は「指定なし」にする（#1948）。書かれていれば聞く。"""
    group_item = _item(
        "group_by_unit",
        "group_by",
        "missing",
        question="集計の単位（例: 地域別、商品別など）は指定しますか？",
        answer_kind="free_text",
    )
    order_item = _item(
        "order_by", "order_by", "missing", question="並び順は？", answer_kind="free_text"
    )
    plain = "受注金額の合計はいくらですか？"
    output = LlmRequirementOutput.model_validate(
        _output(_from_orders(), _select_total(), group_item, order_item)
    )
    draft = build_draft(output, _requirement_context(plain), question=plain)
    assert not [item for item in draft.items if item.status == RequirementStatus.MISSING]

    asked = "顧客別の受注金額を多い順に見せて"
    draft = build_draft(output, _requirement_context(asked), question=asked)
    missing = {item.key for item in draft.items if item.status == RequirementStatus.MISSING}
    assert missing == {"group_by_unit", "order_by"}


# --- FROM の項目が無い LLM の出力（#2239） -------------------------------------------------


def test_missing_from_is_inferred_from_select_hints_and_clarified_question() -> None:
    """FROM の項目が無くても、SELECT の手がかり・書き直した質問の物理名から表を推す（#2239）。

    実サービスで見つけた崩れ: LLM が SELECT（tables・columns は空）と、出力の最上位の欄
    （answerable・mode）の項目だけを返した。表を聞き直さず、最上位の欄の項目は捨てる。
    """

    llm = _ScriptedLlm(
        _output(
            _item(
                "select",
                "select",
                "confirmed",
                label="表示列",
                value="APP.CUSTOMERS.REGION, SUM(APP.ORDERS.AMOUNT)",
                sql_hint="APP.CUSTOMERS.REGION, SUM(APP.ORDERS.AMOUNT)",
            ),
            _item("answerable", "other", "confirmed", label="回答可能か", value="true"),
            _item("mode", "other", "confirmed", label="モード", value="draft"),
            clarified="APP.CUSTOMERS.REGION ごとに APP.ORDERS.AMOUNT の合計を教えてください。",
        )
    )
    created = _create(_runtime(llm), "地域ごとの受注金額の合計")
    draft = _draft(created)
    from_item = next(item for item in draft.items if item.section == RequirementSection.FROM)
    assert from_item.status == RequirementStatus.INFERRED
    assert set(from_item.tables) == {"APP.ORDERS", "APP.CUSTOMERS"}
    assert from_item.question_ja == ""
    assert {item.key for item in draft.items}.isdisjoint({"answerable", "mode"})
    state = created.requirement_state
    assert state is not None
    assert not any(item.section == RequirementSection.FROM for item in state.unresolved_items)
    assert state.select_from_clear is True


def test_missing_from_without_physical_names_still_asks_for_the_table() -> None:
    llm = _ScriptedLlm(
        _output(
            _item(
                "select", "select", "confirmed", label="表示列", value="件数", sql_hint="COUNT(*)"
            ),
            clarified="件数を教えてください。",
        )
    )
    created = _create(_runtime(llm), "件数を教えて")
    state = created.requirement_state
    assert state is not None
    from_item = next(
        item for item in state.unresolved_items if item.section == RequirementSection.FROM
    )
    assert from_item.question_ja == "どの表のデータを使いますか？"


# --- columns の式（#2242） ------------------------------------------------------------------


def test_aggregate_expressions_in_columns_are_not_rejected_as_unknown_names() -> None:
    """columns の COUNT(*)・SUM(表.列) は名前として確かめず、中の参照だけを解決する（#2242）。"""

    llm = _ScriptedLlm(
        _output(
            _from_orders(),
            _item(
                "select_total",
                "select",
                "confirmed",
                label="件数と合計",
                value="件数と受注金額の合計",
                columns=["COUNT(*)", "SUM(APP.ORDERS.AMOUNT)"],
                sql_hint="COUNT(*), SUM(ORDERS.AMOUNT)",
            ),
        )
    )
    created = _create(_runtime(llm), "受注の件数と金額の合計")
    draft = _draft(created)
    select = next(item for item in draft.items if item.key == "select_total")
    assert select.status == RequirementStatus.CONFIRMED
    assert select.columns == ["APP.ORDERS.AMOUNT"]
    assert "実在しない" not in select.reason_ja
    state = created.requirement_state
    assert state is not None
    assert state.select_from_clear is True


def test_unknown_reference_inside_an_expression_is_still_rejected() -> None:
    llm = _ScriptedLlm(
        _output(
            _from_orders(),
            _item(
                "select_total",
                "select",
                "confirmed",
                label="合計",
                value="合計",
                columns=["SUM(APP.ORDERS.NOT_A_COLUMN)"],
                sql_hint="SUM(ORDERS.NOT_A_COLUMN)",
            ),
        )
    )
    draft = _draft(_create(_runtime(llm), "受注の合計"))
    select = next(item for item in draft.items if item.key == "select_total")
    assert select.status == RequirementStatus.AMBIGUOUS
    assert "APP.ORDERS.NOT_A_COLUMN" in select.reason_ja


# --- 質問の期間の語から期間の項目を足す（#2244） ------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("直近1年", ("last_n_years", 1)),
        ("過去３か月", ("last_n_months", 3)),
        ("直近十二ヶ月", ("last_n_months", 12)),
        ("直近2週間", ("last_n_weeks", 2)),
        ("過去30日", ("last_n_days", 30)),
        ("先月", ("last_month", None)),
        ("今年度", ("this_fiscal_year", None)),
        ("最近", ("other", None)),
        ("", ("", None)),
    ],
    ids=[
        "years",
        "months-fullwidth",
        "months-kanji",
        "weeks",
        "days",
        "last-month",
        "fiscal",
        "vague",
        "empty",
    ],
)
def test_relative_period_from_expression(expression: str, expected: tuple[str, int | None]) -> None:
    assert relative_period_from_expression(expression) == expected


def test_question_period_is_confirmed_when_llm_omits_the_period_item() -> None:
    """LLM の出力に期間の項目が無くても、質問の「直近1年」を日付の範囲で確かめる（#2244）。"""

    question = "直近1年の受注金額の合計を教えて。期間は受注日で絞って"
    output = LlmRequirementOutput.model_validate(
        _output(
            _from_orders(),
            _select_total(),
            clarified="APP.ORDERS.ORDER_DATE が直近1年の APP.ORDERS.AMOUNT の合計",
        )
    )
    draft = build_draft(output, _requirement_context(question), question=question)
    period = next(item for item in draft.items if item.key == "period_question")
    assert period.section == RequirementSection.PERIOD
    assert period.period is not None
    assert (period.period.relative_kind, period.period.count) == ("last_n_years", 1)
    assert period.period.expression_ja == "直近1年"
    # 日付の範囲の候補で利用者に確かめる（具体的な日付・データの最新日の基準）。
    assert period.status == RequirementStatus.AMBIGUOUS
    assert period.question_ja.startswith("「直近1年」は")
    assert any(
        candidate.period is not None and candidate.period.start_date
        for candidate in period.candidates
    )


def test_question_without_period_word_adds_no_period_item() -> None:
    question = "受注金額の合計を教えて"
    output = LlmRequirementOutput.model_validate(_output(_from_orders(), _select_total()))
    draft = build_draft(output, _requirement_context(question), question=question)
    assert not any(item.section == RequirementSection.PERIOD for item in draft.items)


def test_llm_period_item_is_used_as_is() -> None:
    question = "最近の受注金額の合計を見せて"
    output = LlmRequirementOutput.model_validate(
        _output(_from_orders(), _select_total(), _blank_period_item())
    )
    draft = build_draft(output, _requirement_context(question), question=question)
    assert not any(item.key == "period_question" for item in draft.items)


# --- 質問で指定した期間の日付の列（#2246） ----------------------------------------------------


def _period_item_with_column(date_column: str = "") -> dict[str, Any]:
    return _item(
        "period_recent",
        "period",
        "ambiguous",
        label="期間",
        period={
            "expression_ja": "直近3か月",
            "relative_kind": "last_n_months",
            "count": 3,
            "fiscal_year_start_month": 0,
            "date_column": date_column,
        },
    )


def _date_column_question(question: str, date_column: str = "") -> list[Any]:
    output = LlmRequirementOutput.model_validate(
        _output(_from_orders(), _select_total(), _period_item_with_column(date_column))
    )
    draft = build_draft(output, _requirement_context(question), question=question)
    return [
        item
        for item in draft.items
        if item.key == "period_date_column"
        and item.status in {RequirementStatus.AMBIGUOUS, RequirementStatus.MISSING}
    ]


def test_date_column_named_in_the_question_is_not_asked_again() -> None:
    """質問の「受注日」（列の論理名）で日付の列が 1 つに決まれば、列を聞かない（#2246）。"""

    question = "直近3か月の受注金額の合計を教えて。期間は受注日で絞って"
    assert _date_column_question(question) == []
    output = LlmRequirementOutput.model_validate(
        _output(_from_orders(), _select_total(), _period_item_with_column())
    )
    draft = build_draft(output, _requirement_context(question), question=question)
    period = next(item for item in draft.items if item.key == "period_recent")
    assert period.period is not None
    assert period.period.date_column == "APP.ORDERS.ORDER_DATE"


def test_date_column_instruction_uses_the_column_the_llm_named() -> None:
    """論理名に当たらなくても、利用者が日付の列を指定し、LLM が 1 つを名指ししたらその列。"""

    question = "直近3か月の受注金額の合計を教えて。期間は送った日で絞って"
    assert _date_column_question(question, "APP.ORDERS.SHIP_DATE") == []


def test_date_column_is_asked_without_an_instruction_or_with_two_names() -> None:
    # 列の指定が無い（LLM の推測だけでは決めない）。
    assert _date_column_question("直近3か月の受注金額の合計", "APP.ORDERS.ORDER_DATE") != []
    # 論理名が 2 つの列に当たる。
    names = "直近3か月の受注金額の合計。受注日と出荷日のどちらで絞るか決めていない"
    assert _date_column_question(names) != []


# --- 「〜ごとに」の集計の単位（#2248） --------------------------------------------------------


def _select_sum(status: str = "inferred") -> dict[str, Any]:
    return _item(
        "select_total",
        "select",
        status,
        label="受注金額の合計",
        value="受注金額の合計",
        columns=["APP.ORDERS.AMOUNT"],
        sql_hint="SUM(APP.ORDERS.AMOUNT)",
    )


def _grouping_draft(question: str, clarified: str, *items: dict[str, Any]) -> Any:
    output = LlmRequirementOutput.model_validate(
        _output(_from_orders(), *(items or (_select_sum(),)), clarified=clarified)
    )
    return build_draft(output, _requirement_context(question), question=question)


def test_grouping_column_named_in_the_clarified_question_is_added() -> None:
    """LLM が GROUP BY を落としても、書き直した質問の「〈列〉ごとに」を集計の単位にする。"""

    draft = _grouping_draft(
        "地域ごとの受注金額の合計",
        "地域（APP.CUSTOMERS.REGION）ごとに APP.ORDERS.AMOUNT の合計を教えてください。",
    )
    group = next(item for item in draft.items if item.section == RequirementSection.GROUP_BY)
    assert group.status == RequirementStatus.INFERRED
    assert group.columns == ["APP.CUSTOMERS.REGION"]
    select = next(item for item in draft.items if item.key == "select_total")
    assert select.sql_hint.startswith("APP.CUSTOMERS.REGION, SUM(")
    assert "APP.CUSTOMERS" in from_tables(draft.items)
    assert "GROUP BY APP.CUSTOMERS.REGION" in draft.sql_skeleton


def test_grouping_column_from_the_logical_name_in_the_question() -> None:
    draft = _grouping_draft("地域ごとの受注金額の合計", "受注金額の合計を教えてください。")
    group = next(item for item in draft.items if item.section == RequirementSection.GROUP_BY)
    assert group.columns == ["APP.CUSTOMERS.REGION"]


def test_grouping_is_not_added_without_aggregate_or_with_llm_group_by() -> None:
    # 集計の無い表示列（一覧）は足さない。
    listing = _item(
        "select_rows",
        "select",
        "inferred",
        label="受注",
        value="受注",
        sql_hint="APP.ORDERS.AMOUNT",
    )
    draft = _grouping_draft("地域ごとの受注を一覧で", "APP.CUSTOMERS.REGION ごとの受注", listing)
    assert not any(item.section == RequirementSection.GROUP_BY for item in draft.items)
    # LLM が GROUP BY を返したときは変えない。
    llm_group = _item(
        "group_status",
        "group_by",
        "inferred",
        label="状態",
        value="受注状態",
        columns=["APP.ORDERS.STATUS"],
        sql_hint="APP.ORDERS.STATUS",
    )
    draft = _grouping_draft(
        "地域ごとの受注金額の合計", "APP.CUSTOMERS.REGION ごと", _select_sum(), llm_group
    )
    groups = [item for item in draft.items if item.section == RequirementSection.GROUP_BY]
    assert [item.key for item in groups] == ["group_status"]
    # 列が決まらない質問は足さない。
    draft = _grouping_draft("いい感じの単位ごとの受注金額の合計", "受注金額の合計")
    assert not any(item.section == RequirementSection.GROUP_BY for item in draft.items)


def test_plain_select_column_beside_an_aggregate_becomes_the_grouping() -> None:
    """表示列が集計と集計でない列の組なら、集計でない列を GROUP BY にする（#2250）。"""

    select = _item(
        "select",
        "select",
        "inferred",
        label="表示列",
        value="件数と地域",
        sql_hint="COUNT(*) AS ORDER_COUNT, APP.CUSTOMERS.REGION",
    )
    # 書き直した質問は「〜ごとに」の前に物理名を書かず、質問の語も論理名に当たらない。
    draft = _grouping_draft("顧客の住む所ごとの受注の件数", "顧客の住む所ごとの受注の件数", select)
    group = next(item for item in draft.items if item.section == RequirementSection.GROUP_BY)
    assert group.columns == ["APP.CUSTOMERS.REGION"]
    assert group.reason_ja.startswith("表示列の集計でない列")
    # 表示列は変えない（列は既に入っている）。
    kept = next(item for item in draft.items if item.key == "select")
    assert kept.sql_hint == "COUNT(*) AS ORDER_COUNT, APP.CUSTOMERS.REGION"
    assert "GROUP BY APP.CUSTOMERS.REGION" in draft.sql_skeleton


@pytest.mark.parametrize("section,valid", [("order_by", True), ("where", False)])
def test_projection_alias_is_valid_only_for_order_by(section: str, valid: bool) -> None:
    context = _requirement_context("合計を降順で並べる")
    output = LlmRequirementOutput.model_validate(
        _output(
            _from_orders(),
            _item(
                "total",
                "select",
                "confirmed",
                columns=["APP.ORDERS.AMOUNT"],
                sql_hint="SUM(APP.ORDERS.AMOUNT) AS TOTAL_VALUE",
            ),
            _item(
                "sort", section, "confirmed", columns=["TOTAL_VALUE"], sql_hint="TOTAL_VALUE DESC"
            ),
        )
    )
    draft = build_draft(output, context, question="合計を降順で並べる")
    item = next(item for item in draft.items if item.key == "sort")
    assert (item.status == RequirementStatus.CONFIRMED) is valid
    if valid:
        assert item.columns == ["APP.ORDERS.AMOUNT"]
        assert item.sql_hint == "TOTAL_VALUE DESC"
    else:
        assert not item.sql_hint


def test_projection_alias_cannot_hide_an_unknown_column() -> None:
    context = _requirement_context("合計を降順で並べる")
    output = LlmRequirementOutput.model_validate(
        _output(
            _from_orders(),
            _item(
                "total",
                "select",
                "confirmed",
                sql_hint="SUM(APP.ORDERS.UNKNOWN_VALUE) AS TOTAL_VALUE",
            ),
            _item(
                "sort",
                "order_by",
                "confirmed",
                columns=["TOTAL_VALUE"],
                sql_hint="TOTAL_VALUE DESC",
            ),
        )
    )
    draft = build_draft(output, context, question="合計を降順で並べる")
    item = next(item for item in draft.items if item.key == "sort")
    assert item.status == RequirementStatus.AMBIGUOUS
    assert not item.sql_hint


def test_requirement_context_includes_authorized_attachment_tables() -> None:
    from app.features.nl2sql.chat_attachments import (
        AttachmentColumn,
        AttachmentContext,
        AttachmentTable,
    )

    attachments = AttachmentContext(
        tables=(
            AttachmentTable(
                name="ATTACHMENT_1",
                attachment_id="attachment-a",
                file_name="table-a.csv",
                sheet="sheet-a",
                columns=(AttachmentColumn(name="VALUE", sql_type="NUMBER"),),
                rows=((10,),),
            ),
        )
    )
    context = build_requirement_context(
        question="添付の値の合計",
        profile=_LegacyService().profile,
        catalog_tables=_catalog().tables,
        allowed=AllowedObjects(),
        ontology=None,
        view=None,
        today=TODAY,
        attachments=attachments,
    )
    assert context.resolve_table("ATTACHMENT_1") == "ATTACHMENT_1"
    assert context.resolve_column("ATTACHMENT_1.VALUE") == "ATTACHMENT_1.VALUE"
    from app.features.nl2sql.sql_requirement import context_payload

    payload = context_payload(context, mode="draft")
    table = next(table for table in payload["schema"] if table["table"] == "ATTACHMENT_1")
    assert table["columns"][0]["data_type"] == "NUMBER"
    assert "rows" not in table
    assert "sample_values" not in table["columns"][0]


def test_requirement_today_uses_common_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """相対的な期間の基準日は共通の設定の時間帯（PLATFORM_TIMEZONE。#3255）の今日。"""
    calls: list[object] = []

    def fake_local_today(settings: object) -> date:
        calls.append(settings)
        return TODAY

    monkeypatch.setattr(ontology_router_module, "local_today", fake_local_today)
    runtime = OntologyApiRuntime(legacy_service=_LegacyService(), store=InMemoryOntologyStore())
    assert runtime._requirement_today() == TODAY
    assert calls == [get_settings()]
