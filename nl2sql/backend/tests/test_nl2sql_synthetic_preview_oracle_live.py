"""Opt-in Oracle: isolated staging and atomic receipt; no business mutations."""

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.features.nl2sql.oracle_adapter import OracleNl2SqlAdapter
from app.features.nl2sql.synthetic_models import SyntheticRun, SyntheticTarget, now
from app.features.nl2sql.synthetic_preview import SyntheticPreview, qualified
from app.features.nl2sql.synthetic_store import SyntheticStore
from app.settings import Settings


@pytest.mark.skipif(os.getenv("NL2SQL_SYNTHETIC_LIVE_TEST") != "1", reason="isolated Oracle opt-in")
def test_preview_oracle_staging_and_atomic_apply() -> None:
    from dotenv import dotenv_values

    # 接続設定は共通 .env（platform/.env の PLATFORM_ORACLE_*。#211）
    env = dotenv_values(Path(__file__).parents[3] / "platform" / ".env")
    connection_settings: dict[str, Any] = {
        k.removeprefix("PLATFORM_").lower(): v
        for k, v in env.items()
        if k.startswith("PLATFORM_ORACLE_")
        and k.removeprefix("PLATFORM_").lower() in Settings.model_fields
        and v is not None
    }
    settings = Settings(_env_file=None, **connection_settings)
    adapter = OracleNl2SqlAdapter(settings)
    preview = SyntheticPreview(adapter)
    prefix = "NL2SQL_PT_" + uuid4().hex[:12].upper()
    parent = f"{settings.oracle_user.upper()}.{prefix}_P"
    child = f"{settings.oracle_user.upper()}.{prefix}_C"
    receipt = prefix + "_R"
    made: list[str] = []
    run = SyntheticRun(
        run_id=str(uuid4()),
        actor_id="test",
        context_id="test",
        idempotency_key="test",
        request_hash="test",
        preview=True,
        request={"sample_rows": 1},
        targets=[
            SyntheticTarget(table_name=child, requested_rows=1, loaded_rows=1),
            SyntheticTarget(table_name=parent, requested_rows=1, loaded_rows=1),
        ],
    )

    class Cursor:
        def __init__(self, cur: Any):
            self.cur = cur

        def __getattr__(self, name: str) -> Any:
            return getattr(self.cur, name)

        def execute(self, sql: str, *args: Any) -> Any:
            return self.cur.execute(sql.replace("NL2SQL_SYNTHETIC_RUNS", receipt), *args)

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            self.cur.close()

    class Connection:
        def __init__(self, conn: Any):
            self.conn = conn

        def cursor(self) -> Any:
            return Cursor(self.conn.cursor())

        def commit(self) -> None:
            self.conn.commit()

        def rollback(self) -> None:
            self.conn.rollback()

    @contextmanager
    def connect() -> Any:
        with adapter.connection() as conn:
            yield Connection(conn)

    try:
        with adapter.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE {qualified(parent)} "
                "(ID NUMBER PRIMARY KEY, NAME VARCHAR2(100) NOT NULL)"
            )
            made.append(parent)
            cur.execute(
                f"CREATE TABLE {qualified(child)} (ID NUMBER PRIMARY KEY, "
                f"PARENT_ID NUMBER REFERENCES {qualified(parent)}(ID), "
                "NAME VARCHAR2(100) CHECK (LENGTH(NAME)>1))"
            )
            made.append(child)
            cur.execute(f"INSERT INTO {qualified(parent)} VALUES (1, '参考行')")
            cur.execute(f"INSERT INTO {qualified(child)} VALUES (1, 1, '参考子行')")
            conn.commit()
            cur.execute(
                f"CREATE TABLE {receipt} (RUN_ID VARCHAR2(36) PRIMARY KEY, "
                "PAYLOAD CLOB CHECK (PAYLOAD IS JSON), VERSION_NO NUMBER)"
            )
            made.append(f"{settings.oracle_user.upper()}.{receipt}")
            ids = {}
            for name in [parent, child]:
                cur.execute(
                    "SELECT OBJECT_ID FROM USER_OBJECTS WHERE OBJECT_NAME=:name "
                    "AND OBJECT_TYPE='TABLE'",
                    {"name": name.split(".")[1]},
                )
                ids[name] = int(cur.fetchone()[0])
            run.request["_object_ids"] = ids
        run.staging = preview.plan(run)
        preview.prepare(run, lambda _: None)
        # Deterministic values stand in for the LLM; all Oracle clone/constraint/apply SQL is real.
        with adapter.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {qualified(run.staging[parent]['name'])} VALUES (2, '確認済み親')"
            )
            cur.execute(
                f"INSERT INTO {qualified(run.staging[child]['name'])} VALUES (2, 2, '確認済み子')"
            )
            conn.commit()
            for name in [parent, child]:
                cur.execute(f"SELECT COUNT(*) FROM {qualified(name)}")
                assert cur.fetchone()[0] == 1
        run.status, run.finished_at = "completed", now()
        preview.seal(run)
        for name in [parent, child]:
            result = preview.results(run, name, 100)
            assert result["results"]["total"] == 1
            assert result["results"]["rows"][0]["ID"] == 2
        with adapter.connection() as conn, conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {receipt} VALUES (:id,:payload,0)",
                {"id": run.run_id, "payload": run.model_dump_json()},
            )
            conn.commit()
        store = SyntheticStore(connect)

        # A failure after INSERT must roll back both tables and leave the receipt ready.
        def fail(conn: Any, value: SyntheticRun) -> None:
            preview.apply(conn.conn, value)
            value.review_status = "applied"
            raise RuntimeError("simulated receipt failure")

        with pytest.raises(RuntimeError):
            store.transact(run.run_id, fail)
        saved = store.get(run.run_id)
        assert saved is not None and saved.review_status == "ready"
        with adapter.connection() as conn, conn.cursor() as cur:
            for name in [parent, child]:
                cur.execute(f"SELECT COUNT(*) FROM {qualified(name)}")
                assert cur.fetchone()[0] == 1

        def apply(conn: Any, value: SyntheticRun) -> None:
            if value.review_status == "applied":
                return
            preview.apply(conn.conn, value)
            value.review_status = "applied"

        store.transact(run.run_id, apply)
        store.transact(run.run_id, apply)
        with adapter.connection() as conn, conn.cursor() as cur:
            for name in [parent, child]:
                cur.execute(f"SELECT COUNT(*) FROM {qualified(name)}")
                assert cur.fetchone()[0] == 2
            cur.execute(
                f"UPDATE {qualified(run.staging[parent]['name'])} SET NAME='変更' WHERE ID=2"
            )
            conn.commit()
        with pytest.raises(HTTPException):
            preview.results(run, parent, 100)
    finally:
        if run.staging:
            preview.cleanup(run)
        with adapter.connection() as conn, conn.cursor() as cur:
            for name in reversed(made):
                cur.execute(f"DROP TABLE {qualified(name)} CASCADE CONSTRAINTS PURGE")


@pytest.mark.skipif(
    os.getenv("NL2SQL_SYNTHETIC_LLM_LIVE_TEST") != "1", reason="isolated Select AI opt-in"
)
def test_select_ai_generates_into_staging_only() -> None:
    from dotenv import dotenv_values

    # 接続設定は共通 .env（platform/.env の PLATFORM_ORACLE_*。#211）
    env = dotenv_values(Path(__file__).parents[3] / "platform" / ".env")
    connection_settings: dict[str, Any] = {
        k.removeprefix("PLATFORM_").lower(): v
        for k, v in env.items()
        if k.startswith("PLATFORM_ORACLE_")
        and k.removeprefix("PLATFORM_").lower() in Settings.model_fields
        and v is not None
    }
    settings = Settings(_env_file=None, **connection_settings)
    adapter = OracleNl2SqlAdapter(settings)
    preview = SyntheticPreview(adapter)
    name = f"{settings.oracle_user.upper()}.NL2SQL_LT_{uuid4().hex[:12].upper()}"
    run = SyntheticRun(
        run_id=str(uuid4()),
        actor_id="test",
        context_id="test",
        idempotency_key="test",
        request_hash="test",
        preview=True,
        targets=[SyntheticTarget(table_name=name, requested_rows=1)],
    )
    made = False
    try:
        with adapter.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT PROFILE_NAME FROM USER_CLOUD_AI_PROFILE_ATTRIBUTES "
                "WHERE ATTRIBUTE_NAME='provider' "
                "AND DBMS_LOB.SUBSTR(ATTRIBUTE_VALUE, 100, 1)='oci' "
                "ORDER BY PROFILE_NAME FETCH FIRST 1 ROWS ONLY"
            )
            row = cur.fetchone()
            assert row, "Configured OCI Select AI profile is required"
            profile = str(row[0])
            cur.execute(f"CREATE TABLE {qualified(name)} (ID NUMBER, NOTE VARCHAR2(100))")
            made = True
        run.staging = preview.plan(run)
        preview.prepare(run, lambda _: None)
        adapter.generate_synthetic_data(
            table_name=run.staging[name]["name"],
            row_count=1,
            profile_name=profile,
            user_prompt="テスト用の行を1件生成してください。NOTE は短い日本語です。",
            staging=True,
        )
        with adapter.connection() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM {qualified(name)}")
            assert cur.fetchone()[0] == 0
            _, count, _ = preview.snapshot(cur, run.staging[name])
            assert count == 1
        run.targets[0].loaded_rows = count
        preview.seal(run)
        assert preview.results(run, name, 100)["results"]["total"] == 1
    finally:
        if run.staging:
            preview.cleanup(run)
        if made:
            with adapter.connection() as conn, conn.cursor() as cur:
                cur.execute(f"DROP TABLE {qualified(name)} PURGE")
