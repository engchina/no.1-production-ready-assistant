"""実 Oracle AI Database を使う統合テスト用のヘルパー。

環境変数（`PLATFORM_ORACLE_*`）の接続情報で実 DB に接続し、RAG スキーマの存在保証と
テストが作成した行のクリーンアップを提供する。テストは手元の `.env` を読まない（#1972）ので、
開発機の共通 `.env` の DB（共有の開発の ADB）には接続しない。CI は Oracle AI Database Free の接続を
環境変数で渡す（`oracle-integration.yml`）。手元で流すときはテスト用の DB の接続を環境変数で渡す。
接続が無い・DB が未到達の環境では `db_available()` が False を返し、依存テストは skip できる。
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from functools import lru_cache
from typing import Any

from app.clients.oracle import _init_oracle_client, _oracle_connect_kwargs
from app.config import Settings
from app.rag.system_schema import SystemSchemaManager

# 環境変数から読んだ実接続設定（テスト中に singleton が書き換わっても影響を受けない）
_REAL_SETTINGS = Settings()

# 既存（テスト開始前から存在する）ドキュメント ID。実運用データを誤って消さない基準。
_BASELINE_DOCUMENT_IDS: set[str] = set()
_BASELINE_KNOWLEDGE_BASE_IDS: set[str] = set()


def real_oracle_connection_kwargs() -> dict[str, Any]:
    """実 Oracle へ直接 connect するための kwargs を返す。"""
    return _oracle_connect_kwargs(_REAL_SETTINGS)


def apply_real_oracle_settings(settings: Settings) -> None:
    """テスト用 singleton に実 Oracle 接続設定を反映する。"""
    settings.oracle_user = _REAL_SETTINGS.oracle_user
    settings.oracle_password = _REAL_SETTINGS.oracle_password
    settings.oracle_dsn = _REAL_SETTINGS.oracle_dsn
    settings.oracle_client_lib_dir = _REAL_SETTINGS.oracle_client_lib_dir
    settings.oracle_wallet_dir = _REAL_SETTINGS.oracle_wallet_dir
    settings.oracle_wallet_password = _REAL_SETTINGS.oracle_wallet_password


def _connect() -> Any:
    oracledb = importlib.import_module("oracledb")
    # PLATFORM_ORACLE_CLIENT_LIB_DIR を指定した実 DB は thick client を使う。thin 接続を先に作ると
    # アプリ側の thick 初期化が DPY-2019 で失敗するため、connect 前に初期化する
    # (冪等。既定の thin では何もしない)。
    _init_oracle_client(oracledb, _REAL_SETTINGS)
    return oracledb.connect(**real_oracle_connection_kwargs())


@lru_cache(maxsize=1)
def db_available() -> bool:
    """実 Oracle に接続できるかを 1 回だけ判定する。"""
    if not _REAL_SETTINGS.oracle_dsn.strip():
        return False
    try:
        connection = _connect()
    except Exception:
        return False
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT 1 FROM dual")
        cursor.fetchone()
        return True
    except Exception:
        return False
    finally:
        connection.close()


@contextmanager
def _schema_connection() -> Iterator[Any]:
    connection = _connect()
    try:
        yield connection
    finally:
        connection.close()


def destructive_migration_skip_reason(status: dict[str, Any]) -> str | None:
    """データを消す未適用の migration があれば、実 Oracle のテストを skip する理由を返す（#619）。

    テストの開始時に、共有の開発 DB のデータを書き出しの前に消さないため、fixture は
    破壊的な migration を当てない。適用は利用者が書き出しを済ませてから明示的に行う。
    """

    pending = status.get("pending_destructive_migrations") or []
    if not pending:
        return None
    names = ", ".join(str(item["name"]) for item in pending)
    return (
        f"データを削除する migration（{names}）が未適用のため、実 Oracle のテストを skip します"
        "（テストの fixture は破壊的な migration を当てません。#619）。テスト専用の schema で"
        "流すか、必要なデータを書き出してから `uv run python -m app.rag.system_schema_cli "
        "initialize --allow-destructive` で適用してください。"
    )


# 実 Oracle のテストを skip する理由（`ensure_schema` が決める。None なら実行してよい）。
_schema_skip_reason: str | None = None


def schema_skip_reason() -> str | None:
    """`ensure_schema` が実 Oracle のテストを止めた理由（止めていなければ None）。"""
    return _schema_skip_reason


@lru_cache(maxsize=1)
def ensure_schema() -> str | None:
    """RAG スキーマ（rag_documents / rag_chunks など）を冪等に作成する。

    データを消す未適用の migration があるときは何も当てず、skip の理由を返す（#619）。
    非破壊の migration は今までどおり当てる。
    """
    global _schema_skip_reason
    manager = SystemSchemaManager(_schema_connection)
    reason = destructive_migration_skip_reason(manager.status())
    if reason is not None:
        _schema_skip_reason = reason
        return reason
    # allow_destructive は渡さない（状態の確認と実行の間に増えても、manager が止める）。
    result = manager.initialize()
    if result["status"] != "ready":
        raise RuntimeError("RAG system schema の初期化後 status が ready ではありません。")
    _schema_skip_reason = None
    return None


def capture_baseline() -> None:
    """テスト開始前に存在する document_id を基準として記録する。"""
    connection = _connect()
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT document_id FROM rag_documents")
        _BASELINE_DOCUMENT_IDS.clear()
        _BASELINE_DOCUMENT_IDS.update(row[0] for row in cursor.fetchall())
        try:
            cursor.execute("SELECT knowledge_base_id FROM rag_knowledge_bases")
        except Exception:
            _BASELINE_KNOWLEDGE_BASE_IDS.clear()
        else:
            _BASELINE_KNOWLEDGE_BASE_IDS.clear()
            _BASELINE_KNOWLEDGE_BASE_IDS.update(row[0] for row in cursor.fetchall())
    finally:
        connection.close()


# 文書に従属する表。正本の schema は `rag_documents` への ON DELETE CASCADE を持つが、
# 古い環境で作った表には FK がないことがあるため、文書の前に明示して消す（子 → 親の順。
# 表がなければ飛ばす）。
_DOCUMENT_CHILD_TABLES = (
    "rag_artifact_layers",
    "rag_graph_entity_chunks",
    "rag_ingestion_segments",
    "rag_ingestion_jobs",
    "rag_chunks",
    "rag_chunk_sets",
    "rag_document_extractions",
    "rag_document_knowledge_bases",
    "rag_document_recipes",
)


def _not_in_predicate(
    column: str, prefix: str, values: tuple[str, ...]
) -> tuple[str, dict[str, str]]:
    if not values:
        return "1 = 1", {}
    placeholders = ", ".join(f":{prefix}{i}" for i in range(len(values)))
    return f"{column} NOT IN ({placeholders})", {
        f"{prefix}{i}": value for i, value in enumerate(values)
    }


def cleanup_to_baseline() -> None:
    """テストが作成した文書・KB と、それに従属する行だけを削除する。

    テスト開始前から存在した baseline の文書・KB（将来の実運用データ）は残し、テスト中に
    作成された行（と、削除済みの文書を指したまま残った行）だけを削除して分離を担保する。
    """
    connection = _connect()
    try:
        cursor = connection.cursor()
        document_sql, document_binds = _not_in_predicate(
            "document_id", "b", tuple(_BASELINE_DOCUMENT_IDS)
        )
        kb_sql, kb_binds = _not_in_predicate(
            "knowledge_base_id", "kb", tuple(_BASELINE_KNOWLEDGE_BASE_IDS)
        )
        with suppress(Exception):
            cursor.execute(f"DELETE FROM rag_document_knowledge_bases WHERE {kb_sql}", kb_binds)
        for table in _DOCUMENT_CHILD_TABLES:
            with suppress(Exception):
                cursor.execute(f"DELETE FROM {table} WHERE {document_sql}", document_binds)
        cursor.execute(
            f"""
            UPDATE rag_documents SET duplicate_of_document_id = NULL
            WHERE duplicate_of_document_id IS NOT NULL
              AND {document_sql.replace("document_id", "duplicate_of_document_id")}
            """,
            document_binds,
        )
        cursor.execute(f"DELETE FROM rag_documents WHERE {document_sql}", document_binds)
        with suppress(Exception):
            cursor.execute(f"DELETE FROM rag_knowledge_bases WHERE {kb_sql}", kb_binds)
        connection.commit()
    finally:
        connection.close()
