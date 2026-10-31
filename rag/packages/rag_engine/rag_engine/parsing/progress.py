"""Doclingの実ページ処理結果を実行単位で記録する。"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_LOG = logging.getLogger(__name__)
ACTIVE_PAGE_PROGRESS: ContextVar[PageProgress | None] = ContextVar("docling_page_progress", default=None)


class PageProgress:
    """ページ完了を重複排除し、JSONをatomic更新して標準出力へ通知する。

    pagesは物理ページ番号（1始まり）。ページ結果は変換呼出し元スレッドから
    通知する。進捗I/O失敗は警告に留め、解析結果の保存を妨げない。
    """

    def __init__(self, run_dir: Path, pages: list[int]):
        self.path = run_dir / "progress.json"
        self.expected = set(pages)
        self.completed: set[int] = set()
        self.failed: set[int] = set()
        self.stage = "parsing_pages"
        self.last_page: int | None = None
        self.write()

    def observe(self, items: list[Any]) -> None:
        """最終出力queueの成功・失敗を記録する。未処理ページは成功と数えない。"""
        for item in items:
            page = item.page_no
            if page not in self.expected or page in self.completed or page in self.failed:
                continue
            failed = bool(item.is_failed or item.error or item.payload is None)
            (self.failed if failed else self.completed).add(page)
            self.last_page = page
            if len(self.completed | self.failed) == len(self.expected):
                self.stage = "assembling_document"
            self.write()

    def finish(self, success: bool) -> None:
        """Docling変換全体の終了を記録する。Visionやembeddingの完了とは別。"""
        self.stage = "docling_completed" if success else "docling_failed"
        self.write()

    def write(self) -> None:
        """実行ディレクトリへ現状を保存する。失敗時も解析は継続する。"""
        payload = {
            "engine": "docling",
            "stage": self.stage,
            "total_pages": len(self.expected),
            "completed_pages": len(self.completed),
            "failed_pages": len(self.failed),
            "pending_pages": len(self.expected - self.completed - self.failed),
            "completed_page_numbers": sorted(self.completed),
            "failed_page_numbers": sorted(self.failed),
            "last_page": self.last_page,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        except OSError:
            _LOG.warning("ページ進捗の保存に失敗しました: %s", self.path, exc_info=True)
        try:
            print(
                f"[docling-progress] run={self.path.parent.name} stage={self.stage} "
                f"completed={len(self.completed)}/{len(self.expected)} "
                f"failed={len(self.failed)} last_page={self.last_page}",
                flush=True,
            )
        except OSError:
            _LOG.warning("ページ進捗のログ出力に失敗しました", exc_info=True)


class ProgressOutputQueue:
    """Doclingの最終queueを透過的に包み、取り出したページだけを通知する。

    producerは元queueへ書き込み、consumerのget_batchだけを観測する。
    モデルや共有converterに実行固有callbackを残さない。
    """

    def __init__(self, queue: Any, progress: PageProgress):
        self.queue = queue
        self.progress = progress

    def get_batch(self, *args: Any, **kwargs: Any) -> list[Any]:
        """元queueのblocking・timeout・返却順序を維持して進捗を記録する。"""
        items = self.queue.get_batch(*args, **kwargs)
        self.progress.observe(items)
        return items

    def __getattr__(self, name: str) -> Any:
        return getattr(self.queue, name)
