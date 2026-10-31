"""Oracle Text に索引した既存の search_text を、質問と同じ文字の形に直す CLI（#1336）。

#1336 から、chunk とフィードバックの調査用の本文の ``search_text`` は保存の時点で正規化する
（``normalize_text_search_index_text``。NFKC・波線・ダッシュ）。それより前に保存した行は、
互換文字（``①`` ``Ⅴ`` ``㈱`` ``㌔`` ``℃`` など）が元のままで、全文検索で当たらない。
Oracle の SQL には NFKC が無いため、この CLI が行を読み、正規化した値が違う行だけを更新する。
表示の本文・embedding は変えない。
Oracle Text の索引は ``SYNC (ON COMMIT)`` なので、更新の commit で索引も直る。

    uv run python -m app.rag.text_search_index_cli normalize --dry-run   # 件数だけを数える
    uv run python -m app.rag.text_search_index_cli normalize             # 更新する
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from pr_backend_core.logging import configure_cli_logging
from rag_engine.retrieval.text_search_tokenizer import normalize_text_search_index_text

from app.clients.oracle import _init_oracle_client, _oracle_connect_kwargs
from app.config import get_settings
from app.rag.request_context import with_system_access

DEFAULT_BATCH_SIZE = 500
# (表, 主キー)。どちらも search_text に CONTEXT 索引がある。
TARGET_TABLES: tuple[tuple[str, str], ...] = (
    ("rag_chunks", "chunk_id"),
    ("rag_feedback_details", "feedback_id"),
)


@dataclass
class TableResult:
    table: str
    scanned: int = 0
    changed: int = 0


def normalize_search_texts(
    connection: Any,
    *,
    dry_run: bool,
    batch_size: int = DEFAULT_BATCH_SIZE,
    tables: Sequence[tuple[str, str]] = TARGET_TABLES,
    normalize: Callable[[str], str] = normalize_text_search_index_text,
) -> list[TableResult]:
    """各表の search_text を主キーの順に読み、正規化した値が違う行だけを更新する。"""
    results: list[TableResult] = []
    for table, key in tables:
        result = TableResult(table=table)
        last_key: str | None = None
        while True:
            # 表と列の名前は固定の TARGET_TABLES だけ（利用者の入力は入らない）。Oracle は空文字を
            # NULL として扱い ``> ''`` が何も返さないため、最初の回は条件を付けない。
            where = "" if last_key is None else f"WHERE {key} > :last_key "
            binds: dict[str, object] = {"batch_size": batch_size}
            if last_key is not None:
                binds["last_key"] = last_key
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {key}, search_text FROM {table} {where}"
                    f"ORDER BY {key} FETCH FIRST :batch_size ROWS ONLY",
                    binds,
                )
                rows = cursor.fetchall()
            if not rows:
                break
            updates: list[dict[str, str]] = []
            for row_key, value in rows:
                text = _lob_text(value)
                result.scanned += 1
                normalized = normalize(text)
                if text and normalized != text:
                    updates.append({"search_text": normalized, "row_key": str(row_key)})
            result.changed += len(updates)
            if updates and not dry_run:
                with connection.cursor() as cursor:
                    cursor.executemany(
                        f"UPDATE {table} SET search_text = :search_text WHERE {key} = :row_key",
                        updates,
                    )
                connection.commit()
            last_key = str(rows[-1][0])
        results.append(result)
    return results


def _lob_text(value: object) -> str:
    if value is None:
        return ""
    read = getattr(value, "read", None)
    return str(read() if callable(read) else value)


def _connect() -> Any:
    settings = get_settings()
    oracledb = importlib.import_module("oracledb")
    _init_oracle_client(oracledb, settings)
    return oracledb.connect(**_oracle_connect_kwargs(settings))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="全文検索の索引の本文（search_text）を、質問と同じ文字の形に直します（#1336）。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    normalize = subparsers.add_parser(
        "normalize", help="正規化した値が違う行の search_text を更新します。"
    )
    normalize.add_argument(
        "--dry-run", action="store_true", help="更新せず、対象の件数だけを数えます。"
    )
    normalize.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"1 回に読む行数（既定 {DEFAULT_BATCH_SIZE}）。",
    )
    return parser


@with_system_access
def main(argv: Sequence[str] | None = None) -> int:
    configure_cli_logging("rag")
    args = build_parser().parse_args(argv)
    if args.batch_size < 1:
        print("--batch-size は 1 以上にしてください。", file=sys.stderr)
        return 2
    connection = _connect()
    try:
        results = normalize_search_texts(
            connection, dry_run=args.dry_run, batch_size=args.batch_size
        )
    finally:
        connection.close()
    print(
        json.dumps(
            {"dry_run": args.dry_run, "tables": [asdict(result) for result in results]},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
