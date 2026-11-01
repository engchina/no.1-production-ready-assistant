"""DB 管理チャットの列の検査（#2352）: 参照する object の実際の列・直させる・止める・警告。

object の列は合成の値（`_FakeDescriber`）、LLM（モデルの API）は決定論のスタブ。実 Oracle で
データ辞書のビューと動的性能ビューを調べるのは
`test_nl2sql_db_admin_chat_history_oracle_live.py` の後半。
"""

from __future__ import annotations

import pytest
from test_nl2sql_db_admin_chat import (
    _catalog_service,
    _execute,
    _FakeAdapter,
    _FakeDescriber,
    _FakeEnterpriseAi,
    _saved,
    _saved_analysis_turn,
    _Service,
    _store_turn,
)

from app.features.nl2sql.db_admin_chat_columns import (
    CachedDescriber,
    ObjectColumnsCache,
    ObjectRef,
    StatementColumnCheck,
    check_statement,
    referenced_objects,
    unknown_column_reason,
)
from app.features.nl2sql.models import DbAdminChatAnalyzeRequest

LOCK = ("ADDR", "SID", "TYPE", "LMODE", "REQUEST")
SESSION = ("SID", "SERIAL#", "USERNAME", "STATUS")
OBJECTS = ("OWNER", "OBJECT_NAME", "OBJECT_TYPE", "STATUS")
SYNTHETIC = {
    "V$LOCK": LOCK,
    "V$SESSION": SESSION,
    "DBA_OBJECTS": OBJECTS,
    "SYS.DBA_OBJECTS": OBJECTS,
    '"Ab"."Cd"': ("Col",),
    "T1": ("A", "ID"),
    "T2": ("B", "ID"),
}


def _check(
    sql: str, *, describer: _FakeDescriber | None = None, select_only: bool = True
) -> StatementColumnCheck | None:
    return check_statement(
        sql,
        describer or _FakeDescriber(SYNTHETIC),
        select_only=select_only,
    )


# ---- 参照する object と列（静的な検査）----


def test_unknown_columns_of_a_single_view_are_listed_with_its_real_columns() -> None:
    check = _check("SELECT SID, SERIAL#, LOCK_TYPE FROM V$LOCK WHERE REQUEST > 0 ORDER BY SID")
    assert check is not None
    assert [item.column for item in check.unknown] == ["SERIAL#", "LOCK_TYPE"]
    assert check.invalid is True
    assert unknown_column_reason(check) == (
        "列が存在しません: SERIAL#, LOCK_TYPE（V$LOCK の列: ADDR, SID, TYPE, LMODE, REQUEST）"
    )


def test_aliases_resolve_to_the_referenced_view() -> None:
    check = _check(
        "SELECT l.sid, s.serial#, s.NOPE FROM v$lock l JOIN V$SESSION s ON s.SID = l.SID"
    )
    assert check is not None
    [unknown] = check.unknown
    assert unknown.column == "NOPE"
    assert unknown.sources == (("V$SESSION", SESSION),)


def test_cte_outputs_are_checked_and_the_cte_is_not_described() -> None:
    describer = _FakeDescriber(SYNTHETIC)
    check = _check(
        "WITH t AS (SELECT owner o, COUNT(*) c FROM sys.dba_objects GROUP BY owner) "
        "SELECT o, c, z FROM t ORDER BY c DESC",
        describer=describer,
    )
    assert check is not None
    assert [item.column for item in check.unknown] == ["Z"]
    assert describer.described == ["SYS.DBA_OBJECTS"]


@pytest.mark.parametrize(
    "sql",
    [
        # `*` の副問い合わせの列は確かめない。
        "SELECT a.anything FROM (SELECT * FROM V$LOCK) a",
        # 疑似列・括弧の無い関数・関数の中の列。
        "SELECT ROWNUM, SYSDATE, USER, UPPER(USERNAME) FROM V$SESSION WHERE ROWNUM < 5",
        # 相関副問い合わせは外側の参照先で確かめる。
        "SELECT e.A FROM T1 e WHERE EXISTS (SELECT 1 FROM T2 d WHERE d.B = e.A)",
        # ORDER BY の別名。
        "SELECT A AS X FROM T1 ORDER BY X",
        # 引用識別子は大文字にしない。
        'SELECT "Col" FROM "Ab"."Cd"',
        # 順序の NEXTVAL など、参照先でない修飾。
        "SELECT SEQ1.NEXTVAL FROM DUAL",
        # 表関数の列は確かめない。
        "SELECT COLUMN_VALUE FROM TABLE(SYS.ODCINUMBERLIST(1, 2))",
        # UNION の各 SELECT。
        "SELECT A FROM T1 UNION ALL SELECT B FROM T2",
    ],
    ids=["star", "pseudo", "correlated", "order-alias", "quoted", "sequence", "table-fn", "union"],
)
def test_valid_or_unverifiable_references_are_not_reported(sql: str) -> None:
    check = _check(sql)
    assert check is not None
    assert check.unknown == []


def test_quoted_identifier_case_is_respected() -> None:
    check = _check('SELECT "col" FROM "Ab"."Cd"')
    assert check is not None
    assert [item.column for item in check.unknown] == ["col"]


def test_referenced_objects_skip_db_links_and_ctes() -> None:
    refs = referenced_objects(
        'WITH w AS (SELECT 1 x FROM DUAL) SELECT * FROM w, emp@remote, APP.T1, "Ab"."Cd"'
    )
    assert refs is not None
    # CTE の名前（W）と DB link の表は調べない。CTE の中の DUAL は調べる。
    assert sorted(ref.key for ref in refs) == ["APP.T1", "Ab.Cd", "DUAL"]
    assert sorted(str(ref.reference_sql()) for ref in refs) == ['"Ab"."Cd"', "APP.T1", "DUAL"]
    assert ObjectRef((('a"b', True),)).reference_sql() is None


def test_undescribable_object_is_a_warning_not_an_error() -> None:
    check = _check("SELECT FOO FROM NO_SUCH_VIEW")
    assert check is not None
    assert check.undescribed == ["NO_SUCH_VIEW"]
    assert check.invalid is False
    assert check.needs_repair is False


def test_oracle_parse_decides_over_the_static_check() -> None:
    sql = "SELECT SID, LOCK_TYPE FROM V$LOCK"
    # Oracle の解析が通れば、静的な検査が誤っても止めない。
    passed = _check(sql, describer=_FakeDescriber(SYNTHETIC, parse_errors={sql: ""}))
    assert passed is not None and passed.invalid is False
    # Oracle の ORA-00904 は、静的に分からない列でも止める。
    dml = "UPDATE T1 SET MISSING = 1"
    failed = _check(
        dml,
        describer=_FakeDescriber(
            SYNTHETIC, parse_errors={dml: 'ORA-00904: "MISSING": invalid identifier'}
        ),
        select_only=False,
    )
    assert failed is not None and failed.invalid is True
    assert unknown_column_reason(failed) == "列が存在しません: MISSING（T1 の列: A, ID）"


def test_objects_changed_earlier_in_the_turn_are_not_checked() -> None:
    describer = _FakeDescriber(SYNTHETIC)
    check = check_statement(
        "SELECT NEW_COLUMN FROM T1", describer, select_only=True, changed_names={"T1", "NUMBER"}
    )
    assert check is None
    assert describer.described == []


def test_cache_expires_and_keeps_undescribable_objects_shorter() -> None:
    now = [0.0]
    cache = ObjectColumnsCache(ttl_seconds=10, negative_ttl_seconds=2, clock=lambda: now[0])
    describer = _FakeDescriber({"V$LOCK": LOCK})
    cached = CachedDescriber(describer, cache)
    assert cached.describe("V$LOCK") == LOCK
    assert cached.describe("NO_SUCH_VIEW") is None
    assert cached.describe("V$LOCK") == LOCK and cached.describe("NO_SUCH_VIEW") is None
    assert describer.described == ["V$LOCK", "NO_SUCH_VIEW"]
    now[0] = 3.0
    cached.describe("NO_SUCH_VIEW")
    cached.describe("V$LOCK")
    assert describer.described == ["V$LOCK", "NO_SUCH_VIEW", "NO_SUCH_VIEW"]
    now[0] = 11.0
    cached.describe("V$LOCK")
    assert describer.described[-1] == "V$LOCK"


def test_cache_does_not_keep_results_without_a_connection() -> None:
    cache = ObjectColumnsCache()
    CachedDescriber(_FakeDescriber(available=False), cache).describe("V$LOCK")
    assert cache.get("V$LOCK") == (False, None)


# ---- 生成: 直させる・止める・警告 ----


def _repair_service(
    responses: list[dict[str, object]], describer: _FakeDescriber | None = None
) -> tuple[_Service, _FakeAdapter, _FakeEnterpriseAi]:
    adapter = _FakeAdapter(describer=describer or _FakeDescriber(SYNTHETIC))
    ai = _FakeEnterpriseAi(list(responses))
    return _catalog_service(adapter, ai), adapter, ai


def test_unknown_columns_are_repaired_once_with_the_real_columns() -> None:
    service, adapter, ai = _repair_service(
        [
            {
                "sql": "SELECT SID, LOCK_TYPE FROM V$LOCK;\nSELECT USERNAME FROM V$SESSION",
                "explanation": "ロックとセッション",
            },
            {"statements": [{"index": 1, "sql": "SELECT SID, TYPE FROM V$LOCK;"}]},
        ]
    )
    data = service.generate_db_admin_chat_sql("ロックを見たい")
    assert [item.sql for item in data.plan.statements] == [
        "SELECT SID, TYPE FROM V$LOCK",
        "SELECT USERNAME FROM V$SESSION",
    ]
    assert data.plan.blocked is False
    assert all(not item.unknown_columns for item in data.plan.statements)
    assert len(ai.calls) == 2
    repair = ai.calls[1]
    assert "存在しない列" in repair["system_prompt"]
    assert "## 文 1" in repair["context"] and "## 文 2" not in repair["context"]
    assert "- V$LOCK: ADDR, SID, TYPE, LMODE, REQUEST" in repair["context"]
    assert "列が存在しません: LOCK_TYPE" in repair["context"]
    # 実行の前の確かめは解析だけ（SELECT は実行しない）。
    assert adapter.select_calls == [] and adapter.statement_calls == []


def test_statement_still_invalid_after_repair_is_kept_and_blocked() -> None:
    service, adapter, ai = _repair_service(
        [
            {"sql": "SELECT SID, LOCK_TYPE FROM V$LOCK; SELECT 1 FROM DUAL", "explanation": ""},
            {"statements": [{"index": 1, "sql": "SELECT SID, MODE_HELD FROM V$LOCK"}]},
        ]
    )
    data = service.generate_db_admin_chat_sql("ロック")
    first, second = data.plan.statements
    assert first.sql == "SELECT SID, MODE_HELD FROM V$LOCK"
    assert first.unknown_columns == ["MODE_HELD"]
    assert first.blocked_reason.startswith("列が存在しません: MODE_HELD（V$LOCK の列: ADDR")
    assert first.requires_confirmation is False
    assert second.blocked_reason == ""
    assert data.plan.blocked is True
    # 直させるのは 1 回だけ。
    assert len(ai.calls) == 2

    ids = _store_turn(service, data.plan.sql)
    blocked = _execute(service, ids, index=1)
    [outcome] = blocked.statements
    assert outcome.status == "blocked"
    assert outcome.result is not None
    assert outcome.result.statements[0].error_message.startswith("列が存在しません")
    assert adapter.select_calls == []
    [audit] = service._admin_audit_store.list_recent()
    assert (audit.outcome, audit.executed) == ("blocked", False)
    with pytest.raises(ValueError, match="すべて実行できません"):
        _execute(service, ids, mode="all")
    # ほかの文は 1 文ずつ実行できる。
    assert _execute(service, ids, index=2).statements[0].status == "succeeded"


def test_repair_without_an_answer_keeps_the_original_statement_blocked() -> None:
    service, _adapter, ai = _repair_service(
        [{"sql": "SELECT LOCK_TYPE FROM V$LOCK", "explanation": ""}, {"statements": []}]
    )
    data = service.generate_db_admin_chat_sql("ロック")
    [statement] = data.plan.statements
    assert statement.sql == "SELECT LOCK_TYPE FROM V$LOCK"
    assert statement.unknown_columns == ["LOCK_TYPE"]
    assert len(ai.calls) == 2


def test_undescribable_object_warns_without_repair_or_block() -> None:
    service, _adapter, ai = _repair_service(
        [{"sql": "SELECT A FROM NO_SUCH_VIEW", "explanation": ""}]
    )
    data = service.generate_db_admin_chat_sql("何か")
    [statement] = data.plan.statements
    assert statement.blocked_reason == "" and statement.unknown_columns == []
    assert statement.warnings == [
        "NO_SUCH_VIEW の列を確かめられませんでした（存在しないか、読む権限がありません）。"
    ]
    assert data.warnings == statement.warnings
    assert len(ai.calls) == 1


def test_missing_object_found_by_oracle_is_repaired_and_then_only_warned() -> None:
    sql = "SELECT A FROM NO_SUCH_VIEW"
    describer = _FakeDescriber(
        SYNTHETIC,
        parse_errors={sql: 'ORA-00942: table or view "APP"."NO_SUCH_VIEW" does not exist'},
    )
    service, _adapter, ai = _repair_service(
        [
            {"sql": sql, "explanation": ""},
            {"statements": [{"index": 1, "sql": "SELECT A FROM OTHER_MISSING_VIEW"}]},
        ],
        describer,
    )
    data = service.generate_db_admin_chat_sql("何か")
    [statement] = data.plan.statements
    assert statement.sql == "SELECT A FROM OTHER_MISSING_VIEW"
    assert statement.blocked_reason == ""
    assert "OTHER_MISSING_VIEW の列を確かめられませんでした" in statement.warnings[0]
    assert "存在しないか、読む権限がありません" in ai.calls[1]["context"]


def test_policy_blocked_statement_objects_are_not_described() -> None:
    describer = _FakeDescriber(SYNTHETIC)
    service, _adapter, _ai = _repair_service(
        [{"sql": "SELECT * FROM NL2SQL_PROFILES", "explanation": ""}], describer
    )
    data = service.generate_db_admin_chat_sql("何か")
    assert data.plan.blocked is True
    assert describer.described == [] and describer.parsed == []


def test_without_the_oracle_runtime_columns_are_not_checked() -> None:
    describer = _FakeDescriber(SYNTHETIC)
    service = _Service(_FakeAdapter(describer=describer), oracle=False)
    plan = service._db_admin_chat_plan("SELECT LOCK_TYPE FROM V$LOCK")
    assert plan.blocked is False
    assert describer.described == []


def test_corrected_statement_is_checked_again_and_unblocked() -> None:
    service = _Service(_FakeAdapter(describer=_FakeDescriber(SYNTHETIC)))
    ids = _store_turn(service, "SELECT LOCK_TYPE FROM V$LOCK; SELECT A FROM NO_SUCH_VIEW")
    assert _saved(service, ids).plan.statements[0].unknown_columns == ["LOCK_TYPE"]
    still = service.replace_db_admin_chat_turn_sql(
        ids[0],
        ids[1],
        "SELECT MODE_HELD FROM V$LOCK",
        actor="chat-user",
        all_users=False,
        statement_index=1,
    )
    assert still.plan.statements[0].unknown_columns == ["MODE_HELD"]
    fixed = service.replace_db_admin_chat_turn_sql(
        ids[0],
        ids[1],
        "SELECT TYPE FROM V$LOCK",
        actor="chat-user",
        all_users=False,
        statement_index=1,
    )
    assert fixed.plan.blocked is False
    # 文ごとの注意はターンの警告に入れ直す（重ねない）。
    assert fixed.warnings == [
        "文 2: NO_SUCH_VIEW の列を確かめられませんでした（存在しないか、読む権限がありません）。"
    ]


def test_error_analysis_gets_the_real_columns_and_checks_the_corrected_sql() -> None:
    adapter = _FakeAdapter(describer=_FakeDescriber(SYNTHETIC))
    ai = _FakeEnterpriseAi(
        [
            {
                "cause": "列の名前が違います。",
                "fix": "修正方法です。",
                "conclusion": "確認して実行します。",
                "corrected_sql": "SELECT SID, LMODE FROM V$LOCK",
            }
        ]
    )
    service = _catalog_service(adapter, ai)
    ids = _saved_analysis_turn(
        service, "SELECT SID, MODE_HELD FROM V$LOCK", "列が存在しません: MODE_HELD"
    )
    data = service.analyze_db_admin_chat_failure(
        *ids,
        DbAdminChatAnalyzeRequest(statement_index=1),
        actor="chat-user",
        all_users=False,
    )
    [call] = ai.calls
    assert "referenced_objects" in call["context"]
    assert "- V$LOCK: ADDR, SID, TYPE, LMODE, REQUEST" in call["context"]
    assert "referenced_objects" in call["system_prompt"]
    assert data.corrected_plan is not None
    assert data.corrected_plan.blocked is False
