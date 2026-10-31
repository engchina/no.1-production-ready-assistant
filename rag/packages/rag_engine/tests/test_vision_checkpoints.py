"""中断・失敗・入力変更時のVision再利用とdurable保存を検証する。"""

import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from rag_engine.config import get_llm_provider, get_settings
from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.analysis import analyze_pdf
from rag_engine.parsing.checkpoints import CheckpointError, atomic_json, checkpoint_lock, load_base_checkpoint, save_base_checkpoint
from rag_engine.parsing.picture_descriptions import describe_docling_pictures
from rag_engine.parsing.vision_checkpoints import VisionCheckpoints


class VisionCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.image = self.root / "page.png"
        Image.new("RGB", (200, 200), "white").save(self.image)
        self.page = PageImage(1, 200, 200, 200, 200, str(self.image))
        self.record = LayoutRecord(
            "docling-p1-1", "docling", 1, 1, [10, 10, 170, 170], "image_top_left", 200, 200, "Picture", raw_type="picture"
        )
        self.settings = get_settings()

    def describe(self, records=None, **kwargs):
        return describe_docling_pictures(
            copy.deepcopy(records or [self.record]), [self.page], run_dir=self.root, pdf_name="source.pdf", settings=self.settings, **kwargs
        )

    def progress(self):
        return json.loads((self.root / "docling/vision/progress.json").read_text())

    def test_resume_reuses_completed_item_after_interruption(self):
        second = replace(copy.deepcopy(self.record), id="docling-p1-2", seq_no=2)
        with (
            patch("rag_engine.parsing.picture_descriptions.describe_picture", side_effect=[{"retrieval_text": "first"}, KeyboardInterrupt]),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.describe([self.record, second])
        self.assertEqual(self.progress()["counts"]["succeeded"], 1)
        with patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value={"retrieval_text": "second"}) as call:
            stats = self.describe([self.record, second])
        self.assertEqual(call.call_count, 1)
        self.assertEqual((stats.succeeded, stats.failed), (2, 0))
        self.assertTrue(self.progress()["items"][self.record.id]["reused"])
        self.assertEqual(self.progress()["phase"], "completed")

    def test_progress_is_logged_not_printed(self):
        """進捗は logging へ出し、stdout へ print しない(backend の Vision の段。#502)。"""
        ledger = VisionCheckpoints(self.root / "vision", "hash")
        ledger.register([self.record])
        with patch("builtins.print") as printed, self.assertLogs("rag_engine.parsing.vision_checkpoints", level="INFO") as logs:
            ledger.update(self.record, "succeeded")
        printed.assert_not_called()
        self.assertIn("[vision-progress] phase=pictures", logs.output[0])
        self.assertIn("succeeded=1", logs.output[0])

    def test_listener_receives_saved_progress_and_failures_do_not_stop(self):
        """保存した途中状態を listener へ渡す(取込の job の進捗。#1999)。失敗しても止めない。"""
        received = []
        ledger = VisionCheckpoints(self.root / "vision", "hash", listener=received.append)
        ledger.register([self.record])
        ledger.update(self.record, "running")
        self.assertEqual(received[-1]["counts"]["running"], 1)
        self.assertEqual(received[-1]["current"], {"record_id": self.record.id, "page": 1, "kind": "Picture"})
        self.assertEqual(received[-1], json.loads((self.root / "vision/progress.json").read_text()))

        broken = VisionCheckpoints(self.root / "broken", "hash", listener=Mock(side_effect=RuntimeError("down")))
        broken.register([self.record])
        with self.assertLogs("rag_engine.parsing.vision_checkpoints", level="WARNING"):
            broken.update(self.record, "succeeded")
        self.assertEqual(json.loads((self.root / "broken/progress.json").read_text())["counts"]["succeeded"], 1)

    def test_describe_passes_progress_callback_to_ledger(self):
        received = []
        from rag_engine.parsing.picture_descriptions import _SettingsPictureDescriber, describe_layout_pictures

        with patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value={"retrieval_text": "図"}):
            describe_layout_pictures(
                [copy.deepcopy(self.record)],
                [self.page],
                run_dir=self.root,
                pdf_name="source.pdf",
                describer=_SettingsPictureDescriber(self.settings),
                progress_callback=received.append,
            )
        self.assertEqual(received[-1]["phase"], "completed")
        self.assertEqual(received[-1]["counts"]["succeeded"], 1)
        self.assertTrue(received[-1]["total_final"])

    def test_failed_api_retries_and_empty_response_is_not_reused(self):
        for response in [RuntimeError("network"), {}]:
            with patch(
                "rag_engine.parsing.picture_descriptions.describe_picture",
                side_effect=response if isinstance(response, Exception) else None,
                return_value=response,
            ):
                self.assertEqual(self.describe().failed, 1)
        with patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value={"retrieval_text": "recovered"}) as call:
            self.assertEqual(self.describe().succeeded, 1)
        self.assertEqual(call.call_count, 1)

    def test_received_response_survives_postprocessing_failure(self):
        with (
            patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value={"retrieval_text": "received"}),
            patch("rag_engine.parsing.picture_descriptions._bounded_vision_description", side_effect=RuntimeError("postprocess failed")),
        ):
            self.assertEqual(self.describe().failed, 1)
        with patch("rag_engine.parsing.picture_descriptions.describe_picture") as call:
            self.assertEqual(self.describe().succeeded, 1)
        call.assert_not_called()

    def test_changed_prompt_model_context_or_image_invalidates_cache(self):
        provider = get_llm_provider(self.settings, self.settings.default_vision_llm)
        with patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value={"retrieval_text": "description"}) as call:
            self.describe()
            self.describe()
            self.assertEqual(call.call_count, 1)
            with patch("rag_engine.parsing.picture_descriptions._render_prompt", return_value="changed prompt"):
                self.describe()
            self.assertEqual(call.call_count, 2)
            with patch(
                "rag_engine.parsing.picture_descriptions.get_llm_provider", return_value=replace(provider, model=provider.model + "-new")
            ):
                self.describe()
            self.assertEqual(call.call_count, 3)
            context = replace(self.record, id="text", seq_no=0, category="Text", raw_type="text", text="new context", bbox=[0, 0, 180, 8])
            self.describe([self.record, context])
            self.assertEqual(call.call_count, 4)
            Image.new("RGB", (200, 200), "black").save(self.image)
            self.describe()
            self.assertEqual(call.call_count, 5)

    def test_prompt_saved_during_analysis_does_not_mix_templates_within_one_run(self):
        # 対象ごとにファイルを読むと、解析中の保存で同じ文書の説明が新旧の方針で混ざる (#485)。
        second = replace(copy.deepcopy(self.record), id="docling-p1-2", seq_no=2)
        templates = iter(["旧方針 {{image_metadata}}", "新方針 {{image_metadata}}"])
        with (
            patch("rag_engine.adapters.oci.read_prompt", side_effect=lambda key: next(templates)),
            patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value={"retrieval_text": "description"}) as call,
        ):
            self.assertEqual(self.describe([self.record, second]).succeeded, 2)
        prompts = [item.kwargs["rendered_prompt"] for item in call.call_args_list]
        self.assertEqual(len(prompts), 2)
        self.assertTrue(all(prompt.startswith("旧方針") for prompt in prompts))

    def test_corrupted_cached_response_is_not_reused(self):
        with patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value={"retrieval_text": "ok"}):
            self.describe()
        checkpoint = next((self.root / "docling/vision/checkpoints").glob("*.json"))
        data = json.loads(checkpoint.read_text())
        data["response"] = {"retrieval_text": "tampered"}
        checkpoint.write_text(json.dumps(data))
        with patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value={"retrieval_text": "regenerated"}) as call:
            self.describe()
        self.assertEqual(call.call_count, 1)

    def test_checkpoints_are_read_as_utf8_regardless_of_locale(self):
        # 書込みは UTF-8 固定。読込みをロケール既定に任せると cp932 等で日本語の
        # checkpoint が復元できず、再開不能と Vision API の再呼出しになる。
        real_read_text = Path.read_text

        def locale_is_ascii(path, *args, **kwargs):
            if not args:
                kwargs["encoding"] = kwargs.get("encoding") or "ascii"
            return real_read_text(path, *args, **kwargs)

        base = self.root / "base_checkpoint.json"
        save_base_checkpoint(base, identity={"source_name": "日本語.pdf"}, request={}, pages=[], records=[], pdf_path=str(self.image))
        ledger = VisionCheckpoints(self.root / "vision", "source")
        ledger.save("k", {"retrieval_text": "説明"}, self.record, state="succeeded")
        with patch.object(Path, "read_text", locale_is_ascii):
            self.assertEqual(load_base_checkpoint(base)["identity"], {"source_name": "日本語.pdf"})
            self.assertEqual(ledger.load("k"), {"retrieval_text": "説明"})

    def test_atomic_failure_preserves_previous_file(self):
        target = self.root / "checkpoint.json"
        atomic_json(target, {"before": True})
        with patch("rag_engine.parsing.checkpoints.os.replace", side_effect=OSError("disk failure")), self.assertRaises(CheckpointError):
            atomic_json(target, {"after": True})
        self.assertEqual(json.loads(target.read_text()), {"before": True})

    def test_checkpoint_failure_stops_without_marking_success(self):
        from rag_engine.parsing.vision_checkpoints import atomic_json as real_write

        def fail_response(path, value):
            if "checkpoints" in path.parts:
                raise CheckpointError("disk full")
            real_write(path, value)

        with (
            patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value={"retrieval_text": "received"}),
            patch("rag_engine.parsing.vision_checkpoints.atomic_json", side_effect=fail_response),
            self.assertRaises(CheckpointError),
        ):
            self.describe()
        self.assertEqual(self.progress()["counts"]["succeeded"], 0)

    def test_same_run_cannot_execute_vision_concurrently(self):
        with checkpoint_lock(self.root, ".vision.lock"), self.assertRaises(CheckpointError):
            self.describe()

    def test_table_discovery_failure_does_not_report_completion(self):
        pdf = self.root / "source.pdf"
        pdf.write_bytes(b"pdf")
        with (
            patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value={"retrieval_text": "ok"}),
            patch("rag_engine.parsing.table_vision.missing_table_image_regions", side_effect=RuntimeError("detection")),
        ):
            stats = self.describe(pdf_path=pdf)
        self.assertTrue(stats.discovery_failed)
        self.assertFalse(self.progress()["total_final"])
        self.assertEqual(self.progress()["phase"], "completed_with_errors")

    def test_table_response_is_reused_after_postprocessing_failure(self):
        pdf = self.root / "source.pdf"
        pdf.write_bytes(b"pdf")
        table = replace(copy.deepcopy(self.record), category="Table", raw_type="table", text="<table><tr><td>説明</td></tr></table>")
        with patch("rag_engine.parsing.table_vision.missing_table_image_regions", return_value={table.id: [[10, 10, 30, 30]]}):
            with (
                patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value={"retrieval_text": "table explanation"}),
                patch("rag_engine.parsing.table_visual_rows.enrich_table_visual_rows", side_effect=RuntimeError("postprocess")),
            ):
                self.assertEqual(self.describe([table], pdf_path=pdf).failed, 1)
            with (
                patch("rag_engine.parsing.picture_descriptions.describe_picture") as call,
                patch("rag_engine.parsing.table_visual_rows.enrich_table_visual_rows"),
            ):
                self.assertEqual(self.describe([table], pdf_path=pdf).succeeded, 1)
            call.assert_not_called()
        self.assertEqual(self.progress()["pages"]["1"]["Table:succeeded"], 1)
        self.assertTrue(self.progress()["total_final"])

    def test_old_run_without_base_checkpoint_is_rejected_without_parsing(self):
        source = self.root / "source.pdf"
        source.write_bytes(b"pdf")
        adapter = Mock()
        settings = replace(self.settings, output_dir=self.root / "runs")
        with (
            patch("rag_engine.parsing.analysis.build_adapters", return_value={"docling": adapter}),
            patch("rag_engine.parsing.analysis.get_source_page_count", return_value=1),
            self.assertRaises(CheckpointError),
        ):
            analyze_pdf(source, "1", ["docling"], settings, resume_run_id="1234567890abcdef")
        adapter.analyze.assert_not_called()

    def test_base_resume_skips_parser_and_preserves_final_outputs(self):
        source = self.root / "source.pdf"
        source.write_bytes(b"pdf")
        run_id = "1234567890abcdef"
        settings = replace(self.settings, output_dir=self.root / "runs")
        adapter = Mock()
        adapter.availability.return_value = SimpleNamespace(available=True, message="")
        adapter.analyze.return_value = [copy.deepcopy(self.record)]
        with (
            patch("rag_engine.parsing.analysis.build_adapters", return_value={"docling": adapter}),
            patch("rag_engine.parsing.analysis.get_source_page_count", return_value=1),
            patch("rag_engine.parsing.analysis.prepare_source_for_analysis", return_value=(str(source), [self.page])) as render,
            patch("rag_engine.parsing.analysis.create_run_id", return_value=run_id),
            patch("rag_engine.parsing.analysis.model_pool.trim_cuda_cache"),
            patch("rag_engine.parsing.table_vision.missing_table_image_regions", return_value={}),
        ):
            with (
                patch("rag_engine.parsing.picture_descriptions.describe_picture", side_effect=KeyboardInterrupt),
                self.assertRaises(KeyboardInterrupt),
            ):
                analyze_pdf(source, "1", ["docling"], settings, use_docling_vision=True)
            self.assertTrue((settings.output_dir / run_id / "base_checkpoint.json").is_file())
            with patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value={"retrieval_text": "ok"}):
                run = analyze_pdf(source, "1", ["docling"], settings, use_docling_vision=True, resume_run_id=run_id)
            self.assertEqual(adapter.analyze.call_count, 1)
            self.assertEqual(render.call_count, 1)
            self.assertTrue(Path(run.json_path).is_file())
            self.assertIn("ok", run.records[0].text)
            # 完成後の再開でもbaseから再構成し、外部APIを再送しない。
            with patch("rag_engine.parsing.picture_descriptions.describe_picture") as call:
                analyze_pdf(source, "1", ["docling"], settings, use_docling_vision=True, resume_run_id=run_id)
            call.assert_not_called()
            with self.assertRaises(CheckpointError):
                analyze_pdf(
                    source,
                    "1",
                    ["docling"],
                    replace(settings, docling_do_ocr=not settings.docling_do_ocr),
                    use_docling_vision=True,
                    resume_run_id=run_id,
                )
            source.write_bytes(b"changed pdf")
            with self.assertRaises(CheckpointError):
                analyze_pdf(source, "1", ["docling"], settings, use_docling_vision=True, resume_run_id=run_id)
            source.write_bytes(b"pdf")
            self.image.write_bytes(b"changed image")
            with self.assertRaises(CheckpointError):
                analyze_pdf(source, "1", ["docling"], settings, use_docling_vision=True, resume_run_id=run_id)

    def test_resume_uses_run_artifacts_when_source_is_deleted_and_output_dir_moved(self):
        source = self.root / "upload.pdf"
        source.write_bytes(b"pdf")
        run_id = "1234567890abcdef"
        settings = replace(self.settings, output_dir=self.root / "runs")

        def prepare(_source, _pages, run_dir, _dpi):
            (Path(run_dir) / "pages").mkdir(parents=True)
            image = Path(run_dir) / "pages" / "page_0001.png"
            image.write_bytes(self.image.read_bytes())
            (Path(run_dir) / "source.pdf").write_bytes(b"pdf")
            return str(Path(run_dir) / "source.pdf"), [replace(self.page, image_path=str(image))]

        adapter = Mock()
        adapter.availability.return_value = SimpleNamespace(available=True, message="")
        adapter.analyze.return_value = [copy.deepcopy(self.record)]
        with (
            patch("rag_engine.parsing.analysis.build_adapters", return_value={"docling": adapter}),
            patch("rag_engine.parsing.analysis.get_source_page_count", return_value=1),
            patch("rag_engine.parsing.analysis.prepare_source_for_analysis", side_effect=prepare),
            patch("rag_engine.parsing.analysis.create_run_id", return_value=run_id),
            patch("rag_engine.parsing.analysis.model_pool.trim_cuda_cache"),
        ):
            analyze_pdf(source, "1", ["docling"], settings)
            # Gradio の一時ファイル削除と、保存済み絶対 path が無効になる出力先の移動を再現する。
            source.unlink()
            (self.root / "runs").rename(self.root / "moved")
            moved = replace(settings, output_dir=self.root / "moved")
            run = analyze_pdf(source, "1", ["docling"], moved, resume_run_id=run_id)
            self.assertEqual(adapter.analyze.call_count, 1)
            self.assertEqual(run.pages[0].image_path, str(self.root / "moved" / run_id / "pages" / "page_0001.png"))
            self.assertEqual(run.pdf_path, str(self.root / "moved" / run_id / "source.pdf"))
            (self.root / "moved" / run_id / "source.pdf").write_bytes(b"tampered")
            with self.assertRaises(CheckpointError):
                analyze_pdf(source, "1", ["docling"], moved, resume_run_id=run_id)

    def test_missing_source_without_resume_is_still_rejected(self):
        with self.assertRaises(FileNotFoundError):
            analyze_pdf(self.root / "missing.pdf", "1", ["docling"], self.settings)
