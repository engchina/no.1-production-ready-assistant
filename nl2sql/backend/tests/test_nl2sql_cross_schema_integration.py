"""実 Oracle ADB 向けの cross-schema Select AI / Agent integration。

通常 CI では実行せず、明示 opt-in 時だけ一意な一時 asset を作成して finally で削除する。
資格情報・sample value・生成結果本文は出力しない。
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.features.nl2sql.object_identity import parse_object_identity
from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.nl2sql.sql_semantics import parse_oracle_sql
from app.settings import get_settings

pytestmark = pytest.mark.skipif(
    os.getenv("NL2SQL_RUN_ORACLE_INTEGRATION") != "1",
    reason="NL2SQL_RUN_ORACLE_INTEGRATION=1 の明示指定が必要です。",
)


def test_cross_schema_select_ai_profile_and_agent_round_trip() -> None:
    adapter = OracleNl2SqlAdapter(get_settings())
    owners = adapter.fetch_schema_owners()
    external_owners = [item.owner for item in owners.owners if not item.is_current]
    if not external_owners:
        pytest.skip("現在ユーザーから参照可能な外部 schema がありません。")

    catalog = adapter.fetch_catalog(include_samples=False)
    preferred_owners: list[str] = [
        owner for owner in ("SH", "SSB", "OML_USER") if owner in external_owners
    ]
    preferred_owners.extend(owner for owner in external_owners if owner not in preferred_owners)
    target = next(
        (
            table
            for owner in preferred_owners
            for table in catalog.tables
            if table.owner.upper() == owner and "$" not in table.table_name
        ),
        None,
    )
    if target is None:
        pytest.skip("外部 schema に integration 対象 object がありません。")

    suffix = uuid.uuid4().hex[:8].upper()
    profile_name = f"CX_XS_{suffix}_PROFILE"
    tool_name = f"CX_XS_{suffix}_TOOL"
    agent_name = f"CX_XS_{suffix}_AGENT"
    task_name = f"CX_XS_{suffix}_TASK"
    team_name = f"CX_XS_{suffix}_TEAM"
    qualified_name = f"{target.owner.upper()}.{target.table_name.upper()}"
    attributes = adapter._select_ai_profile_attributes(  # noqa: SLF001
        allowed_tables=[qualified_name],
        row_limit=10,
        description="Codex cross-schema integration",
    )

    try:
        adapter.upsert_select_ai_profile_low_level(
            profile_name=profile_name,
            attributes=attributes,
            description="Codex cross-schema integration",
        )
        detail = adapter.get_select_ai_profile_detail(profile_name=profile_name)
        object_list = detail.get("object_list") or detail.get("attributes", {}).get(
            "object_list", []
        )
        actual_scope = {
            parse_object_identity(
                str(item.get("name") or ""),
                default_owner=str(item.get("owner") or ""),
            ).qualified_name
            for item in object_list
            if isinstance(item, dict) and item.get("name")
        }
        assert actual_scope == {qualified_name}
        if not attributes.get("credential_name"):
            pytest.skip(
                "Select AI profile の cross-schema scope は確認済みですが、"
                "NL2SQL_SELECT_AI_CREDENTIAL_NAME が未設定のため生成/Agent は実行できません。"
            )

        select_ai_sql = adapter.generate_select_ai_sql(
            profile_name=profile_name,
            question=(
                f"{qualified_name} から利用可能な列を使って先頭の行を取得してください。"
                "FROM の object は owner で修飾してください。"
            ),
        )
        select_ai_graph = parse_oracle_sql(select_ai_sql).graph
        assert select_ai_graph is not None
        assert any(
            table.owner.upper() == target.owner.upper()
            and table.name.upper() == target.table_name.upper()
            for table in select_ai_graph.tables
        )

        adapter.refresh_select_ai_agent_assets(
            profile_name=profile_name,
            tool_name=tool_name,
            agent_name=agent_name,
            task_name=task_name,
            team_name=team_name,
            allowed_tables=[qualified_name],
            row_limit=10,
            description="cross-schema read-only SQL assistant",
        )
        agent_sql, _conversation_id = adapter.run_select_ai_agent_team(
            team_name=team_name,
            tool_name=tool_name,
            question=(
                f"{qualified_name} から利用可能な列を使って先頭の行を取得してください。"
                "owner を必ず明示してください。"
            ),
        )
        agent_graph = parse_oracle_sql(agent_sql).graph
        assert agent_graph is not None
        assert any(
            table.owner.upper() == target.owner.upper()
            and table.name.upper() == target.table_name.upper()
            for table in agent_graph.tables
        )
    finally:
        try:
            adapter.drop_select_ai_agent_assets(
                profile_name=profile_name,
                tool_name=tool_name,
                agent_name=agent_name,
                task_name=task_name,
                team_name=team_name,
            )
        finally:
            adapter.drop_select_ai_profile(profile_name=profile_name)
