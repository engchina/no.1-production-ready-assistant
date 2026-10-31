"""Vision をページごとの列で並べても、順番に読んだときと同じ入力・結果になる(#2002)。"""

import copy
import hashlib
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.picture_descriptions import describe_layout_pictures


class _Describer:
    """prompt の hash を説明に入れるスタブ(同じページの前の図の説明が prompt に入るかが結果に出る)。"""

    api_mode = "responses"
    max_tokens = 1000

    def __init__(self, *, delay: float = 0.0, fail_ids: frozenset[str] = frozenset()) -> None:
        self.delay = delay
        self.fail_ids = fail_ids
        self.prompts: dict[str, str] = {}
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.active_pages: list[int] = []
        self.same_page_overlap = False

    def provider(self):
        return SimpleNamespace(provider_id="stub", model="stub-vlm", region="", base_url="", project_id="")

    def describe(self, crop_path, metadata, *, context_image_paths, target_kind, rendered_prompt):
        record_id = metadata["record_id"]
        page = metadata["page"]
        with self.lock:
            if page in self.active_pages:
                self.same_page_overlap = True
            self.active_pages.append(page)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.prompts[record_id] = rendered_prompt
        try:
            if self.delay:
                time.sleep(self.delay)
            if record_id in self.fail_ids:
                raise RuntimeError("VLM の失敗")
            digest = hashlib.sha256(rendered_prompt.encode("utf-8")).hexdigest()[:12]
            return {"retrieval_text": f"{record_id} の説明 {digest}", "main_topic": target_kind}
        finally:
            with self.lock:
                self.active -= 1
                self.active_pages.remove(page)


class _ConcurrentDescriber(_Describer):
    def __init__(self, *, max_concurrency: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.max_concurrency = max_concurrency
        self.pool = ThreadPoolExecutor(max_workers=8)

    def submit(self, crop_path, metadata, *, context_image_paths, target_kind, rendered_prompt):
        return self.pool.submit(
            self.describe,
            crop_path,
            metadata,
            context_image_paths=context_image_paths,
            target_kind=target_kind,
            rendered_prompt=rendered_prompt,
        )


def _picture(page: int, seq: int, bbox: list[float]) -> LayoutRecord:
    return LayoutRecord(f"docling-p{page}-{seq}", "docling", page, seq, bbox, "image_top_left", 400, 400, "Picture", raw_type="picture")


class VisionConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pages = []
        for number in range(1, 5):
            image = self.root / f"page-{number}.png"
            Image.new("RGB", (400, 400), "white").save(image)
            self.pages.append(PageImage(number, 400, 400, 400, 400, str(image)))
        self.records = []
        for number in range(1, 5):
            self.records.append(_picture(number, 1, [10, 10, 190, 190]))
            self.records.append(_picture(number, 2, [210, 210, 390, 390]))

    def run_vision(self, describer, run_name: str):
        records = copy.deepcopy(self.records)
        stats = describe_layout_pictures(
            records,
            self.pages,
            run_dir=self.root / run_name,
            pdf_name="source.pdf",
            describer=describer,
        )
        return stats, records

    def test_concurrent_pages_match_sequential_results_and_prompts(self):
        sequential = _Describer()
        seq_stats, seq_records = self.run_vision(sequential, "sequential")
        concurrent = _ConcurrentDescriber(max_concurrency=3, delay=0.05)
        con_stats, con_records = self.run_vision(concurrent, "concurrent")

        self.assertEqual(seq_stats.targets, 8)
        self.assertEqual((con_stats.targets, con_stats.succeeded, con_stats.failed), (8, 8, 0))
        self.assertEqual(concurrent.prompts, sequential.prompts)
        self.assertEqual([record.text for record in con_records], [record.text for record in seq_records])
        self.assertEqual(
            [record.raw.get("vision_description") for record in con_records],
            [record.raw.get("vision_description") for record in seq_records],
        )
        # 2 つ目の図の prompt は同じページの 1 つ目の図の説明を含む(順番の依存を守った)。
        self.assertIn("docling-p1-1 の説明", sequential.prompts["docling-p1-2"])
        self.assertLessEqual(concurrent.max_active, 3)
        self.assertGreater(concurrent.max_active, 1)
        self.assertFalse(concurrent.same_page_overlap)

    def test_concurrent_failure_is_recorded_and_others_continue(self):
        describer = _ConcurrentDescriber(max_concurrency=4, fail_ids=frozenset({"docling-p2-1"}))
        stats, records = self.run_vision(describer, "failure")
        self.assertEqual((stats.succeeded, stats.failed), (7, 1))
        failed = next(record for record in records if record.id == "docling-p2-1")
        self.assertEqual(failed.raw["vision_status"], "failed")
        self.assertIn("VLM の失敗", failed.raw["vision_error"])

    def test_error_marked_stops_batch_stops_the_whole_stage(self):
        class Aborted(Exception):
            stops_batch = True

        for describer in (_Describer(), _ConcurrentDescriber(max_concurrency=3)):
            original = describer.describe

            def failing(*args, _original=original, **kwargs):
                if args[1]["record_id"] == "docling-p2-1":
                    raise Aborted("権限がありません")
                return _original(*args, **kwargs)

            describer.describe = failing
            with self.subTest(type(describer).__name__), self.assertRaises(Aborted):
                self.run_vision(describer, f"abort-{type(describer).__name__}")

    def test_concurrent_resume_reuses_finished_items(self):
        first = _ConcurrentDescriber(max_concurrency=4)
        self.run_vision(first, "resume")
        again = _ConcurrentDescriber(max_concurrency=4)
        stats, _ = self.run_vision(again, "resume")
        self.assertEqual(stats.succeeded, 8)
        self.assertEqual(again.prompts, {})

    def test_concurrent_calls_pages_at_the_same_time(self):
        # 4 ページ(各 2 件)・並行 4 → 4 ページの 1 つ目の図を同時に呼ぶ(順番なら 1 件ずつ)。経過時間を順番の実行と
        # 比べると CI の負荷で逆転した(#3398)ため、4 件が同時に呼ばれたことを Barrier で確かめる(同時でなければ timeout)。
        describer = _ConcurrentDescriber(max_concurrency=4)
        barrier = threading.Barrier(4, timeout=30)
        original = describer.describe

        def gated(*args, _original=original, **kwargs):
            if args[1]["record_id"].endswith("-1"):
                barrier.wait()
            return _original(*args, **kwargs)

        describer.describe = gated
        stats, _ = self.run_vision(describer, "simultaneous")
        self.assertFalse(barrier.broken)
        self.assertEqual((stats.succeeded, stats.failed), (8, 0))


if __name__ == "__main__":
    unittest.main()
