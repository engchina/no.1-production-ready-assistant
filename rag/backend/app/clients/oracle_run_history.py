"""実行履歴（チャット・RAG 検索の 1 回の実行。#1890）の Oracle の保存。

1 回の実行（trace_id）を `rag_run_history` に 1 行で残す。回答を作った実行の回答・引用・診断は
回答の記録（`rag_answer_records`）を正本にし、ここには一覧と絞り込みに使う要約（状態・回答の対応・
回答の検証・所要時間）と、処理の段（`stages_json`）、回答を作らない RAG 検索の根拠
（`citations_json`）だけを持つ。

見え方（#1889・#1954）: system_admin（`all_users_visible`）は全員の実行。それ以外は、自分が実行した
行（`user_id_hash`）と、自分の会話（`rag_conversations` の持ち主）で他の人（system_admin）が実行した
行（`pr_system_settings.auth.visibility.can_access_run` と同じ範囲。利用者の分からない context は
0 件）。会話の無い実行（RAG 検索）は実行した人だけ。利用できる検索・回答プロファイルが制限されて
いるときは、その検索・回答プロファイルの実行だけ（回答の記録と同じ。#214）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from pr_backend_core.api import offset_fetch_binds, offset_fetch_clause
from pr_system_settings.auth.visibility import RunExecutorFilter

from app.clients.oracle import (
    OracleClient,
    OracleConnectionProtocol,
    _execute,
    _execute_count,
    _feedback_owner_scope,
    _json_bind,
    _json_input_sizes,
    _like_pattern,
    _oracle_tenant_predicate,
    _row_count_value,
    _search_answer_profile_scope_predicates,
    _with_search_answer_profile_scope_bind,
    _with_tenant_bind,
)
from app.rag.request_context import access_scope_context, current_audit_request_context

RUN_HISTORY_TABLE = "rag_run_history"
# 会話の表（実行の会話の持ち主を引く。#1954）。
CONVERSATION_TABLE = "rag_conversations"
RUN_SURFACES = ("search", "chat")
RUN_STATUSES = ("success", "blocked", "error")
RUN_VERIFICATIONS = ("verified", "partial", "unverified")
# 一覧に出す質問の文字数（全文は詳細で読む）。
QUESTION_PREVIEW_CHARS = 300
# 詳細に出すフィードバックの件数の上限。
RUN_FEEDBACK_LIMIT = 20


def oracle_run_history_schema_sql(table_name: str = RUN_HISTORY_TABLE) -> str:
    """実行履歴の表の DDL（#1890）。

    `question` は安全チェックの後（機微情報をマスクした後）の質問。安全チェックで止めた質問は
    保存しない（NULL。チャットの会話と同じ）。`user_uuid` は実行した利用者（一覧の「実行者」の
    表示名を引く）、`user_id_hash` は見え方の判定に使う。`conversation_id` の索引は、会話の持ち主に
    見せる条件（持ち主の会話の実行。#1954）に使う。
    """
    surfaces = ", ".join(f"'{item}'" for item in RUN_SURFACES)
    statuses = ", ".join(f"'{item}'" for item in RUN_STATUSES)
    verifications = ", ".join(f"'{item}'" for item in RUN_VERIFICATIONS)
    return f"""
CREATE TABLE {table_name} (
    run_id                    VARCHAR2(64) PRIMARY KEY,
    surface                   VARCHAR2(16) NOT NULL,
    search_answer_profile_id  VARCHAR2(64),
    tenant_id_hash            CHAR(64),
    user_id_hash              CHAR(64),
    user_uuid                 VARCHAR2(256),
    question                  CLOB,
    answer_generated          NUMBER(1) DEFAULT 0 NOT NULL,
    status                    VARCHAR2(16) NOT NULL,
    answer_outcome            VARCHAR2(32),
    verification              VARCHAR2(16),
    confidence                VARCHAR2(16),
    needs_human_review        NUMBER(1),
    citation_count            NUMBER(10) DEFAULT 0 NOT NULL,
    elapsed_ms                NUMBER(12, 3),
    error_stage               VARCHAR2(128),
    error_type                VARCHAR2(128),
    model_id                  VARCHAR2(256),
    conversation_id           VARCHAR2(64),
    message_id                VARCHAR2(64),
    stages_json               JSON,
    citations_json            JSON,
    created_at                TIMESTAMP WITH TIME ZONE DEFAULT SYSTIMESTAMP NOT NULL,
    CONSTRAINT {table_name}_surface_ck CHECK (surface IN ({surfaces})),
    CONSTRAINT {table_name}_status_ck CHECK (status IN ({statuses})),
    CONSTRAINT {table_name}_verification_ck
        CHECK (verification IS NULL OR verification IN ({verifications}))
);

CREATE INDEX {table_name}_created_idx
    ON {table_name} (created_at DESC, run_id);

CREATE INDEX {table_name}_owner_idx
    ON {table_name} (user_id_hash, created_at DESC);

CREATE INDEX {table_name}_conversation_idx
    ON {table_name} (conversation_id);
""".strip()


def _run_scope(alias: str = "r") -> tuple[list[str], dict[str, object]]:
    """実行履歴を、tenant・利用できる検索・回答プロファイル・見え方で絞る条件と bind。

    #1889・#1954。

    一般の利用者は、自分が実行した行と、自分の会話で他の人（system_admin）が実行した行
    （`can_access_run` と同じ範囲）。会話の持ち主は会話の表の `user_id_hash` で決める。
    """
    context = access_scope_context("run_history_scope")
    clauses = [_oracle_tenant_predicate(alias=alias)]
    clauses.extend(_search_answer_profile_scope_predicates(f"{alias}.search_answer_profile_id"))
    binds = _with_search_answer_profile_scope_bind(_with_tenant_bind({}))
    if not context.all_users_visible:
        if context.user_id_hash is None:
            # 利用者の分からない context は、全件ではなく 0 件にする（fail-closed）。
            clauses.append("1 = 0")
        else:
            clauses.append(
                f"({alias}.user_id_hash = :run_owner_user_id_hash"
                f" OR {alias}.conversation_id IN ("
                f"SELECT oc.conversation_id FROM {CONVERSATION_TABLE} oc"
                " WHERE oc.user_id_hash = :run_owner_user_id_hash"
                f" AND {_oracle_tenant_predicate(alias='oc')}))"
            )
            binds["run_owner_user_id_hash"] = context.user_id_hash
    return clauses, binds


def _executor_clause(
    executor: RunExecutorFilter, alias: str = "r"
) -> tuple[str | None, dict[str, object]]:
    """「実行者」の絞り込み（すべて / 自分が実行 / 他の人が実行。#1954）の条件と bind。

    `run_executor_matches`（in-memory の判定）と同じ規則。実行した人の分からない行は「他の人」。
    """
    if executor == "all":
        return None, {}
    me = current_audit_request_context().user_id_hash
    if me is None:
        # 利用者の分からない context に「自分が実行」した行は無い。
        return ("1 = 0" if executor == "mine" else None), {}
    if executor == "mine":
        return f"{alias}.user_id_hash = :run_executor_user_id_hash", {
            "run_executor_user_id_hash": me
        }
    return (
        f"({alias}.user_id_hash <> :run_executor_user_id_hash OR {alias}.user_id_hash IS NULL)",
        {"run_executor_user_id_hash": me},
    )


def run_history_list_where(
    *,
    query: str | None,
    surface: str | None,
    status: str | None,
    executor: RunExecutorFilter = "all",
    since: datetime | None = None,
) -> tuple[str, dict[str, object]]:
    """一覧・件数の WHERE と bind（見え方・質問と実行 ID の検索・種類・状態・実行者・期間）。

    検索語は質問の部分一致か、実行 ID の完全一致（詳細の URL・ログから実行を探す。#1955）。
    期間は開始日時（`created_at`）が `since` 以後の実行（#1955）。
    """
    clauses, binds = _run_scope()
    executor_clause, executor_binds = _executor_clause(executor)
    if executor_clause:
        clauses.append(executor_clause)
        binds.update(executor_binds)
    if query and query.strip():
        clauses.append(
            "(LOWER(r.question) LIKE :run_query ESCAPE '\\' OR r.run_id = :run_id_query)"
        )
        binds["run_query"] = _like_pattern(query.strip())
        binds["run_id_query"] = query.strip()
    if surface:
        clauses.append("r.surface = :run_surface")
        binds["run_surface"] = surface
    if status:
        clauses.append("r.status = :run_status")
        binds["run_status"] = status
    if since is not None:
        clauses.append("r.created_at >= :run_since")
        binds["run_since"] = since
    return " AND ".join(clauses), binds


# 一覧の列。利用量（#3419）は `stages_json` の全体（処理の段・trace の記録）を読まず、
# 合計の token と計測の状態の 2 つの値だけを `JSON_VALUE` で取り出す（#3466。DDL・索引は
# 足さない）。
_LIST_COLUMNS = f"""
    r.run_id, r.surface, r.search_answer_profile_id, p.name AS search_answer_profile_name,
    r.user_id_hash, r.user_uuid,
    DBMS_LOB.SUBSTR(r.question, {QUESTION_PREVIEW_CHARS}, 1) AS question,
    r.answer_generated, r.status, r.answer_outcome, r.verification, r.confidence,
    r.needs_human_review, r.citation_count, r.elapsed_ms, r.error_stage, r.model_id,
    r.conversation_id, r.message_id, r.created_at,
    rc.user_id_hash AS conversation_owner_hash,
    JSON_VALUE(r.stages_json, '$.validation_execution' RETURNING VARCHAR2(16) NULL ON ERROR)
        AS validation_execution,
    JSON_VALUE(r.stages_json, '$.usage.total_tokens' RETURNING NUMBER(19) NULL ON ERROR)
        AS usage_total_tokens,
    JSON_VALUE(r.stages_json, '$.usage.measurement' RETURNING VARCHAR2(16) NULL ON ERROR)
        AS usage_measurement
"""

# 会話の持ち主（実行の詳細の「会話の持ち主」・会話を開ける人の判定。#1954）。削除された会話は NULL。
_FROM = f"""
    FROM {RUN_HISTORY_TABLE} r
    LEFT JOIN rag_search_answer_profiles p
      ON p.search_answer_profile_id = r.search_answer_profile_id
    LEFT JOIN {CONVERSATION_TABLE} rc
      ON rc.conversation_id = r.conversation_id
"""


def _json_value(value: object) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return value


class RunHistoryStore:
    """実行履歴の読み書き（OracleClient の接続と transaction を使う）。"""

    def __init__(self, oracle: OracleClient | None = None) -> None:
        self._oracle = oracle or OracleClient()

    async def save_run(self, record: Mapping[str, object]) -> None:
        """実行を 1 行で保存する（同じ run_id は上書き。成功の後の失敗は失敗で残す）。

        実行した利用者は今の context から決める（呼び出し側の値は信じない）。
        """
        context = current_audit_request_context()
        binds = {
            "run_id": record["run_id"],
            "surface": record["surface"],
            "search_answer_profile_id": record.get("search_answer_profile_id"),
            "tenant_id_hash": context.tenant_id_hash,
            "user_id_hash": context.user_id_hash,
            "user_uuid": context.user_uuid,
            "question": record.get("question") or None,
            "answer_generated": 1 if record.get("answer_generated") else 0,
            "status": record["status"],
            "answer_outcome": record.get("answer_outcome"),
            "verification": record.get("verification"),
            "confidence": record.get("confidence") or None,
            "needs_human_review": (
                None
                if record.get("needs_human_review") is None
                else int(bool(record.get("needs_human_review")))
            ),
            "citation_count": int(record.get("citation_count") or 0),  # type: ignore[call-overload]
            "elapsed_ms": record.get("elapsed_ms"),
            "error_stage": record.get("error_stage"),
            "error_type": record.get("error_type"),
            "model_id": record.get("model_id") or None,
            "conversation_id": record.get("conversation_id"),
            "message_id": record.get("message_id"),
            "stages_json": _json_bind(record.get("stages") or {}),
            "citations_json": _json_bind(record.get("citations")),
        }

        def operation(connection: OracleConnectionProtocol) -> None:
            _execute(
                connection,
                f"""
                MERGE INTO {RUN_HISTORY_TABLE} target
                USING (SELECT :run_id AS run_id FROM dual) source
                ON (target.run_id = source.run_id)
                WHEN MATCHED THEN UPDATE SET
                    target.status = :status,
                    target.answer_outcome = :answer_outcome,
                    target.verification = :verification,
                    target.confidence = :confidence,
                    target.needs_human_review = :needs_human_review,
                    target.citation_count = :citation_count,
                    target.elapsed_ms = :elapsed_ms,
                    target.error_stage = :error_stage,
                    target.error_type = :error_type,
                    target.stages_json = :stages_json,
                    target.citations_json = :citations_json
                WHEN NOT MATCHED THEN INSERT (
                    run_id, surface, search_answer_profile_id, tenant_id_hash, user_id_hash,
                    user_uuid, question, answer_generated, status, answer_outcome, verification,
                    confidence, needs_human_review, citation_count, elapsed_ms, error_stage,
                    error_type, model_id, conversation_id, message_id, stages_json,
                    citations_json
                ) VALUES (
                    :run_id, :surface, :search_answer_profile_id, :tenant_id_hash, :user_id_hash,
                    :user_uuid, :question, :answer_generated, :status, :answer_outcome,
                    :verification, :confidence, :needs_human_review, :citation_count,
                    :elapsed_ms, :error_stage, :error_type, :model_id, :conversation_id,
                    :message_id, :stages_json, :citations_json
                )
                """,
                binds,
                input_sizes=_json_input_sizes("stages_json", "citations_json"),
            )

        await self._oracle._run_transaction(operation)  # noqa: SLF001

    async def list_runs(
        self,
        *,
        query: str | None,
        surface: str | None,
        status: str | None,
        limit: int,
        offset: int,
        executor: RunExecutorFilter = "all",
        since: datetime | None = None,
    ) -> tuple[list[dict[str, object]], int]:
        """実行を新しい順に返す（質問は先頭の一部だけ）と、同じ条件の総件数。"""
        where, binds = run_history_list_where(
            query=query, surface=surface, status=status, executor=executor, since=since
        )
        # ページの実行（run_id）を先に決め、列（質問の先頭・利用量の JSON_VALUE・名前の
        # 結合）はページの行だけで読む（#3466）。1 つの SELECT にすると、並べ替えが全行の
        # `stages_json`（処理の段・trace の記録）を抱えて運び、行が増えるほど重くなる。
        rows = await self._oracle._fetch_all(  # noqa: SLF001
            f"""
            WITH run_page AS (
                SELECT r.run_id
                FROM {RUN_HISTORY_TABLE} r
                WHERE {where}
                ORDER BY r.created_at DESC, r.run_id DESC
                {offset_fetch_clause()}
            )
            SELECT {_LIST_COLUMNS}
            {_FROM}
            JOIN run_page pg ON pg.run_id = r.run_id
            ORDER BY r.created_at DESC, r.run_id DESC
            """,
            {**binds, **offset_fetch_binds(offset=offset, limit=limit)},
        )
        count = await self._oracle._fetch_one(  # noqa: SLF001
            f"SELECT COUNT(*) AS count_value FROM {RUN_HISTORY_TABLE} r WHERE {where}",
            binds,
        )
        return rows, _row_count_value(count)

    async def get_run(self, run_id: str) -> dict[str, object] | None:
        """実行を 1 件返す（見え方の外は None。質問の全文・処理の段・根拠を含む）。"""
        clauses, binds = _run_scope()
        row = await self._oracle._fetch_one(  # noqa: SLF001
            f"""
            SELECT {_LIST_COLUMNS}, r.question AS question_full, r.error_type,
                   r.stages_json, r.citations_json
            {_FROM}
            WHERE r.run_id = :run_id AND {" AND ".join(clauses)}
            """,
            {**binds, "run_id": run_id},
        )
        if row is None:
            return None
        row["question"] = row.pop("question_full", None) or row.get("question")
        row["stages_json"] = _json_value(row.get("stages_json"))
        row["citations_json"] = _json_value(row.get("citations_json"))
        return row

    async def list_run_feedback(self, trace_id: str) -> list[dict[str, object]]:
        """実行（trace_id）への利用者フィードバックの最新の票（送った利用者・対象ごと）。

        フィードバックの画面と同じ見え方（SYSTEM_ADMIN は全員の分、ほかは自分が送った分。#408）。
        """
        owner_clauses, owner_binds = _feedback_owner_scope("f.user_id_hash")
        clauses = [_oracle_tenant_predicate(alias="f"), *owner_clauses]
        return await self._oracle._fetch_all(  # noqa: SLF001
            f"""
            SELECT feedback_id, target_type, rating, reason, document_id, created_at
            FROM (
                SELECT f.feedback_id, f.target_type, f.rating, f.reason, f.document_id,
                       f.created_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY f.user_id_hash, f.target_type,
                                        NVL(f.document_id, '-'), NVL(f.chunk_id, '-')
                           ORDER BY f.created_at DESC, f.feedback_id DESC
                       ) AS feedback_rank
                FROM rag_citation_feedback f
                WHERE f.trace_id = :trace_id AND {" AND ".join(clauses)}
            )
            WHERE feedback_rank = 1
            ORDER BY created_at DESC, feedback_id DESC
            FETCH FIRST {RUN_FEEDBACK_LIMIT} ROWS ONLY
            """,
            {**_with_tenant_bind({}), **owner_binds, "trace_id": trace_id},
        )

    async def purge_runs(self, retention_days: int) -> int:
        """保持期間を過ぎた実行を削除し、削除件数を返す（0 以下は無期限で削除しない）。"""
        if retention_days <= 0:
            return 0

        def operation(connection: OracleConnectionProtocol) -> int:
            return _execute_count(
                connection,
                f"DELETE FROM {RUN_HISTORY_TABLE} "
                "WHERE created_at < SYSTIMESTAMP - NUMTODSINTERVAL(:days, 'DAY')",
                {"days": retention_days},
            )

        return await self._oracle._run_transaction(operation)  # noqa: SLF001


def owner_user_uuids(rows: Sequence[Mapping[str, object]]) -> list[str]:
    """一覧の行の、実行した利用者の UUID（重複なし・順序を保つ）。"""
    return list(dict.fromkeys(str(row["user_uuid"]) for row in rows if row.get("user_uuid")))
