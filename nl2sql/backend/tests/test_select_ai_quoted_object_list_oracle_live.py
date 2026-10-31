"""実 Oracle ADB で Select AI の object_list が引用名の表を区別することを確認する (#564)。

通常 CI では実行しない。`NL2SQL_RUN_ORACLE_INTEGRATION=1` の明示指定時だけ、現在の schema に
一意な接頭辞の検証表 2 つ（大文字の表と、大文字化すると同名になる引用名の表）と Select AI profile を
作成し、finally で必ず削除する。LLM は呼ばない（`showprompt` だけを使う）。資格情報は出力しない。
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

from app.features.nl2sql.object_identity import format_object_part, qualified_object_name
from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.settings import get_settings

pytestmark = pytest.mark.skipif(
    os.getenv("NL2SQL_RUN_ORACLE_INTEGRATION") != "1",
    reason="NL2SQL_RUN_ORACLE_INTEGRATION=1 の明示指定が必要です。",
)


def _read(value: Any) -> str:
    return str(value.read() if hasattr(value, "read") else value or "")


def test_select_ai_object_list_distinguishes_quoted_same_name_table() -> None:
    adapter = OracleNl2SqlAdapter(get_settings())
    suffix = uuid.uuid4().hex[:8].upper()
    upper_table = f"ZZ_QV_{suffix}"
    quoted_table = f"Zz_Qv_{suffix}"
    profile_name = f"ZZ_QV_{suffix}_PROFILE"

    with adapter.connection() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM DUAL")
        owner = str(cursor.fetchone()[0])
    try:
        with adapter.connection() as conn, conn.cursor() as cursor:
            cursor.execute(f"CREATE TABLE {upper_table} (ID NUMBER, UPPER_ONLY_COL NUMBER)")
            cursor.execute(f'CREATE TABLE "{quoted_table}" ("Id" NUMBER, "Quoted_Only_Col" NUMBER)')
        quoted_name = qualified_object_name(owner, quoted_table)
        attributes = adapter._select_ai_profile_attributes(  # noqa: SLF001
            allowed_tables=[quoted_name], row_limit=None, description=""
        )
        assert attributes["object_list"] == [
            {"owner": format_object_part(owner), "name": f'"{quoted_table}"'}
        ]
        adapter.upsert_select_ai_profile_low_level(
            profile_name=profile_name, attributes=attributes, description=""
        )
        with adapter.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT DBMS_CLOUD_AI.GENERATE(prompt => :prompt, profile_name => :profile, "
                "action => 'showprompt') FROM DUAL",
                {"prompt": "すべての行を表示して", "profile": profile_name},
            )
            prompt = _read(cursor.fetchone()[0])
        # 引用名の表の定義だけが渡り、大文字の同名表の定義は渡らない。
        assert "Quoted_Only_Col" in prompt
        assert "UPPER_ONLY_COL" not in prompt
    finally:
        adapter.drop_select_ai_profile(profile_name=profile_name)
        with adapter.connection() as conn, conn.cursor() as cursor:
            for statement in (
                f"DROP TABLE {upper_table} PURGE",
                f'DROP TABLE "{quoted_table}" PURGE',
            ):
                try:
                    cursor.execute(statement)
                except Exception as exc:  # 作成前に失敗した場合（ORA-00942）は無視する
                    if "ORA-00942" not in str(exc):
                        raise
