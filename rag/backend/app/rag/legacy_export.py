"""削除する旧テーブルの行を、削除の前に JSON Lines へ書き出す CLI（#596）。

旧 standard の回答エンジンだけが使っていたテーブルは、システムテーブルの更新（migration
`20260930_005_retire_standard_engine_objects`）で DROP する。行を残したい環境は、更新の前に
このコマンドで書き出す。書き出せるのは下の許可リストのテーブルだけ（どれも secret の列を持たない）。

    uv run python -m app.rag.legacy_export --table rag_agent_memories \\
        --output ../artifacts/rag_agent_memories.jsonl

1 行が 1 レコードの JSON（列名は小文字）。日時は ISO 8601、CLOB は文字列、VECTOR は数値の配列、
BLOB / RAW は base64 の文字列にする。テーブルが無いときは何も書かずに終わる（exit 0）。
"""

from __future__ import annotations

import argparse
import array
import base64
import json
import os
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# 書き出せるテーブルと、行の並び（主キー）。SQL に埋め込むので許可リストの外は受け付けない。
EXPORTABLE_TABLES: dict[str, str] = {
    "rag_agent_memories": "memory_id",
    "rag_prompt_versions": "version_id",
    "rag_generation_settings": "settings_key",
}
_FETCH_BATCH_SIZE = 200


def export_table(connection: Any, table: str, output: Path) -> int | None:
    """テーブルの全行を JSON Lines で書き出し、件数を返す。テーブルが無ければ None。

    書き終えてから置き換えるので、途中で失敗しても不完全なファイルを出力先に残さない。
    行には利用者の入力（記憶の本文など）が入るため、ファイルは所有者だけが読めるようにする。
    """

    order_column = EXPORTABLE_TABLES[table]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT COUNT(*) FROM user_tables WHERE table_name = :table_name",
            {"table_name": table.upper()},
        )
        row = cursor.fetchone()
    if row is None or int(row[0] or 0) == 0:
        return None

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f"{output.name}.partial")
    count = 0
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT * FROM {table} ORDER BY {order_column}")  # nosec B608 - 許可リストの固定名
            columns = [str(description[0]).lower() for description in cursor.description]
            fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with open(fd, "w", encoding="utf-8") as handle:
                while rows := cursor.fetchmany(_FETCH_BATCH_SIZE):
                    for values in rows:
                        record = {
                            column: _json_value(value)
                            for column, value in zip(columns, values, strict=True)
                        }
                        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                        count += 1
        partial.replace(output)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return count


def _json_value(value: Any) -> Any:
    """Oracle から取り出した値を JSON で表せる値にする。"""

    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, array.array):
        return value.tolist()
    if isinstance(value, bytes | bytearray):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_value(item) for item in value]
    read = getattr(value, "read", None)
    if callable(read):  # CLOB / BLOB
        return _json_value(read())
    return str(value)


@contextmanager
def _default_connection() -> Iterator[Any]:
    from app.clients.oracle import OracleClient

    connection = OracleClient().acquire_connection()
    try:
        yield connection
    finally:
        connection.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="削除する旧テーブルの行を、削除の前に JSON Lines へ書き出します（#596）。"
    )
    parser.add_argument("--table", required=True, choices=sorted(EXPORTABLE_TABLES))
    parser.add_argument(
        "--output",
        required=True,
        type=Path,
        help="書き出す先のファイル（JSON Lines。あれば上書きします）。",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    connection_factory: Callable[[], AbstractContextManager[Any]] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    with (connection_factory or _default_connection)() as connection:
        count = export_table(connection, args.table, args.output)
    if count is None:
        result = {"table": args.table, "status": "table_missing", "row_count": 0}
    else:
        result = {
            "table": args.table,
            "status": "exported",
            "row_count": count,
            "output": str(args.output),
        }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
