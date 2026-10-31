"""ページ進捗の順序逆転・失敗・実行分離を検証する。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rag_engine.adapters.parsers.base import AnalysisContext
from rag_engine.adapters.parsers.docling_adapter import DoclingAdapter
from rag_engine.config import get_settings
from rag_engine.models.layout import PageImage
from rag_engine.parsing.progress import ACTIVE_PAGE_PROGRESS, PageProgress, ProgressOutputQueue


def item(page, failed=False):
    return SimpleNamespace(page_no=page, is_failed=failed, error=None, payload=None if failed else object())


class ParseProgressTests(unittest.TestCase):
    def test_out_of_order_duplicate_and_failed_pages(self):
        with TemporaryDirectory() as directory, patch("builtins.print"):
            progress = PageProgress(Path(directory), [3, 4, 5])
            progress.observe([item(5), item(5), item(1), item(4, failed=True)])
            state = json.loads(progress.path.read_text())
            self.assertEqual(state["completed_page_numbers"], [5])
            self.assertEqual(state["failed_page_numbers"], [4])
            self.assertEqual(state["pending_pages"], 1)
            self.assertEqual(state["stage"], "parsing_pages")
            progress.observe([item(3)])
            state = json.loads(progress.path.read_text())
            self.assertEqual(state["completed_pages"], 2)
            self.assertEqual(state["last_page"], 3)
            self.assertEqual(state["stage"], "assembling_document")
            progress.finish(False)
            self.assertEqual(json.loads(progress.path.read_text())["stage"], "docling_failed")

    def test_queue_preserves_batch_timeout_and_close(self):
        queue = Mock()
        batch = [item(2)]
        queue.get_batch.return_value = batch
        progress = Mock()
        wrapped = ProgressOutputQueue(queue, progress)
        self.assertIs(wrapped.get_batch(32, timeout=0.05), batch)
        queue.get_batch.assert_called_once_with(32, timeout=0.05)
        progress.observe.assert_called_once_with(batch)
        wrapped.close()
        queue.close.assert_called_once()

    def test_conversion_failure_resets_context_and_keeps_pending_pages(self):
        with TemporaryDirectory() as directory, patch("builtins.print"):
            context = AnalysisContext(Path("source.pdf"), Path(directory), [PageImage(1, 10, 10, 10, 10, "page.png")], get_settings())
            adapter = DoclingAdapter(context.settings)
            converter = Mock()
            converter.convert.side_effect = RuntimeError("conversion failed")
            with (
                patch.object(adapter, "_converter", return_value=converter),
                patch("rag_engine.adapters.parsers.docling_adapter._prepare_docling_env"),
                self.assertRaisesRegex(RuntimeError, "conversion failed"),
            ):
                adapter.analyze(context)
            self.assertIsNone(ACTIVE_PAGE_PROGRESS.get())
            state = json.loads((Path(directory) / "progress.json").read_text())
            self.assertEqual(state["stage"], "docling_failed")
            self.assertEqual(state["completed_pages"], 0)
            self.assertEqual(state["pending_pages"], 1)

    def test_partial_success_is_accepted_only_when_requested_pages_completed(self):
        # 指定 1,3 は Docling では 1〜3 の連続範囲として変換される。
        for failed_page, accepted in ((2, True), (3, False)):
            with self.subTest(failed_page=failed_page), TemporaryDirectory() as directory, patch("builtins.print"):
                context = AnalysisContext(
                    Path("source.pdf"),
                    Path(directory),
                    [PageImage(1, 10, 10, 10, 10, "p1.png"), PageImage(3, 10, 10, 10, 10, "p3.png")],
                    get_settings(),
                )
                adapter = DoclingAdapter(context.settings)

                # converter の side_effect として同じ反復の中でだけ呼ぶため、failed_page の遅延束縛は問題にならない。
                def convert(*_args, **_kwargs):
                    ACTIVE_PAGE_PROGRESS.get().observe([item(page, failed=page == failed_page) for page in (1, 2, 3)])  # noqa: B023
                    return SimpleNamespace(
                        status=SimpleNamespace(value="partial_success"),
                        errors=[SimpleNamespace(error_message=f"page {failed_page} failed")],  # noqa: B023
                        document=SimpleNamespace(export_to_dict=lambda: {}, tables=[]),
                    )

                converter = Mock()
                converter.convert.side_effect = convert
                with (
                    patch.object(adapter, "_converter", return_value=converter),
                    patch("rag_engine.adapters.parsers.docling_adapter._prepare_docling_env"),
                ):
                    if accepted:
                        self.assertEqual(adapter.analyze(context), [])
                    else:
                        with self.assertRaisesRegex(RuntimeError, "page 3 failed"):
                            adapter.analyze(context)
                state = json.loads((Path(directory) / "progress.json").read_text())
                self.assertEqual(state["stage"], "docling_completed" if accepted else "docling_failed")
                self.assertEqual(converter.convert.call_args.kwargs["page_range"], (1, 3))

    def test_progress_io_failure_does_not_abort_conversion(self):
        with TemporaryDirectory() as directory, patch("builtins.print"), patch("rag_engine.parsing.progress._LOG") as log:
            with patch.object(Path, "write_text", side_effect=OSError("disk full")):
                progress = PageProgress(Path(directory), [1])
                progress.observe([item(1)])
            self.assertEqual(progress.completed, {1})
            self.assertTrue(log.warning.called)

    def test_independent_runs_do_not_share_counts(self):
        with TemporaryDirectory() as directory, patch("builtins.print"):
            first = PageProgress(Path(directory) / "first", [1, 2])
            second = PageProgress(Path(directory) / "second", [1, 2])
            first.observe([item(1)])
            second.observe([item(2)])
            self.assertEqual(first.completed, {1})
            self.assertEqual(second.completed, {2})
