"""文書の一括の投入(#2674)の受付を、テスト用の DB で件数を指定して測る。

合成の文書の行(本文は持たない text/plain の行)を ``--count`` 件作り、一括の受付
(``accept_bulk_ingestion`` + ``OracleBulkIngestionStore``)の所要時間・最大の memory・
登録した job の欠落と重複を測り、同じ要求の再送で job が増えないことを確かめる。最後に
作った行を消す(``--keep`` で残す)。

注意:
- 共用の DB(開発・検証環境の ADB)では実行しない。テスト用の DB ユーザーの接続を書いた
  ファイルを ``PLATFORM_ENV_FILE`` で渡し、``--yes-test-schema`` を付けて実行する。
- 同じ DB を見ている取込 worker が動いていると、登録した job を処理し始める(解析・Embedding の
  費用がかかる)。worker を止めてから実行する。job は測った後に取り消して消す。

使い方(rag/backend で):
  PLATFORM_ENV_FILE=/path/to/test-db.env uv run python scripts/measure_bulk_ingestion.py \
      --count 10000 --yes-test-schema
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import time
import tracemalloc
from uuid import uuid4

from app.api.routes.documents import _merge_document_processing_config
from app.clients.oracle import (
    OracleClient,
    OracleConnectionProtocol,
    _execute,
    delete_document_rows,
)
from app.clients.oracle_bulk_ingestion_store import OracleBulkIngestionStore
from app.config import Settings, get_settings
from app.rag.bulk_ingestion import accept_bulk_ingestion
from app.rag.request_context import with_system_access
from app.schemas.document import DocumentProcessingConfig, IngestionJob

_IN_CHUNK = 500


def _resolve(config: DocumentProcessingConfig) -> Settings:
    return _merge_document_processing_config(config)[0]


async def _no_single(document_id: str) -> IngestionJob:
    raise RuntimeError(f"合成の文書は新しい文書のはず: {document_id}")


async def _create_documents(client: OracleClient, tag: str, count: int) -> list[str]:
    ids: list[str] = []
    for index in range(count):
        file_name = f"bulk-measure-{tag}-{index:05d}.txt"
        detail = await client.create_document(
            file_name=file_name,
            object_storage_path=f"local://bulk-measure/{file_name}",
            content_type="text/plain",
            file_size_bytes=1,
            content_sha256=hashlib.sha256(file_name.encode()).hexdigest(),
        )
        ids.append(detail.id)
    return ids


async def _queued_counts(client: OracleClient, document_ids: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for start in range(0, len(document_ids), _IN_CHUNK):
        chunk = document_ids[start : start + _IN_CHUNK]
        binds = {f"d{index}": value for index, value in enumerate(chunk)}
        placeholders = ", ".join(f":{key}" for key in binds)
        rows = await client._fetch_all(
            "SELECT document_id, COUNT(*) AS job_count FROM rag_ingestion_jobs "
            f"WHERE status = 'QUEUED' AND document_id IN ({placeholders}) GROUP BY document_id",
            binds,
        )
        counts.update({str(row["document_id"]): int(str(row["job_count"])) for row in rows})
    return counts


async def _cleanup(client: OracleClient, document_ids: list[str]) -> None:
    for start in range(0, len(document_ids), _IN_CHUNK):
        chunk = document_ids[start : start + _IN_CHUNK]
        binds = {f"d{index}": value for index, value in enumerate(chunk)}
        placeholders = ", ".join(f":{key}" for key in binds)

        def cancel(
            connection: OracleConnectionProtocol,
            binds: dict[str, str] = binds,
            sql: str = placeholders,
        ) -> None:
            _execute(
                connection,
                "UPDATE rag_ingestion_jobs SET status = 'CANCELLED', finished_at = SYSTIMESTAMP "
                f"WHERE status = 'QUEUED' AND document_id IN ({sql})",
                binds,
            )

        await client._run_transaction(cancel)
    for document_id in document_ids:

        def delete(connection: OracleConnectionProtocol, document_id: str = document_id) -> bool:
            return delete_document_rows(connection, document_id)

        await client._run_transaction(delete)


async def _main(count: int, keep: bool) -> int:
    client = OracleClient()
    store = OracleBulkIngestionStore(client)
    tag = uuid4().hex[:8]
    started = time.perf_counter()
    document_ids = await _create_documents(client, tag, count)
    print(f"documents_created count={count} elapsed={time.perf_counter() - started:.1f}s")
    max_attempts = get_settings().ingestion_job_max_attempts
    try:
        tracemalloc.start()
        started = time.perf_counter()
        result = await accept_bulk_ingestion(
            document_ids,
            store=store,
            resolve_settings=_resolve,
            enqueue_single=_no_single,
            max_attempts=max_attempts,
        )
        elapsed = time.perf_counter() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        print(
            f"accepted count={result.accepted_count} already_active={result.already_active_count} "
            f"skipped={result.skipped_count} rejected={result.rejected_count} "
            f"elapsed={elapsed:.2f}s peak_memory={peak / 1e6:.1f}MB"
        )
        body_bytes = len(result.model_dump_json().encode())
        print(f"response_body_bytes={body_bytes}")
        counts = await _queued_counts(client, document_ids)
        missing = [document_id for document_id in document_ids if counts.get(document_id) != 1]
        duplicated = [document_id for document_id, value in counts.items() if value > 1]
        print(f"persisted queued={len(counts)} missing={len(missing)} duplicated={len(duplicated)}")

        started = time.perf_counter()
        again = await accept_bulk_ingestion(
            document_ids,
            store=store,
            resolve_settings=_resolve,
            enqueue_single=_no_single,
            max_attempts=max_attempts,
        )
        resend_elapsed = time.perf_counter() - started
        after = await _queued_counts(client, document_ids)
        print(
            f"resend accepted={again.accepted_count} already_active={again.already_active_count} "
            f"elapsed={resend_elapsed:.2f}s unchanged={after == counts}"
        )
        ok = (
            result.accepted_count == count
            and not missing
            and not duplicated
            and again.accepted_count == 0
            and after == counts
        )
        return 0 if ok else 1
    finally:
        if not keep:
            started = time.perf_counter()
            await _cleanup(client, document_ids)
            print(f"cleanup elapsed={time.perf_counter() - started:.1f}s")


@with_system_access
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--count", type=int, default=10_000)
    parser.add_argument("--keep", action="store_true", help="作った文書と job を残す")
    parser.add_argument(
        "--yes-test-schema",
        action="store_true",
        help="接続先がテスト用の DB ユーザーで、取込 worker が止まっていることを確かめた",
    )
    args = parser.parse_args()
    if not args.yes_test_schema:
        print(
            "テスト用の DB(PLATFORM_ENV_FILE)で、取込 worker を止めてから "
            "--yes-test-schema を付けて実行してください。",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(asyncio.run(_main(args.count, args.keep)))


if __name__ == "__main__":
    main()
