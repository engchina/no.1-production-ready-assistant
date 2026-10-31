"""実 Oracle の統合テスト用の取込 job の実行補助（#483）。

取込は段階（ファイル準備 → 解析 → 分割 → 索引）ごとに job を分け、段階が終わると次の段階の job を
待ち行列に入れてワーカーへ通知するだけにする（HTTP の中では実行しない）。テストはワーカーを
動かさないため、最初の job を実行した後に、同じ文書の待ち行列の job を順に実行する。
確認待ちのゲート（自動で次へ進めない設定）では次の job が作られないので、そこで止まる。
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from app.api.routes import documents as documents_route
from tests.support import AsgiTestClient

_MAX_ROUNDS = 8


def run_ingestion_job_and_queued_followups(
    client: AsgiTestClient,
    job_id: str,
    document_id: str,
    *,
    headers: dict[str, str] | None = None,
) -> None:
    asyncio.run(documents_route._run_ingestion_job(job_id))
    for _ in range(_MAX_ROUNDS):
        response = client.get(
            "/api/documents/ingestion-jobs",
            params={"status": "QUEUED", "limit": 200},
            headers=headers,
        )
        assert response.status_code == 200
        items = cast(list[dict[str, Any]], response.json()["data"]["items"])
        queued = [item for item in items if item.get("document_id") == document_id]
        if not queued:
            return
        for item in queued:
            asyncio.run(documents_route._run_ingestion_job(cast(str, item["id"])))
    raise AssertionError(f"取込 job が {_MAX_ROUNDS} 回で終わりません: {document_id}")
