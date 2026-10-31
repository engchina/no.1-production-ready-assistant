"""analysis full file の挙動を保護するテスト。"""

import json
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from rag_engine.adapters.parsers.docling_adapter import DoclingAdapter
from rag_engine.config import get_settings
from rag_engine.knowledge.classification import classification_from_selection
from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.analysis import analyze_pdf
from rag_engine.parsing.parse_inputs import parse_input_path


class FullFileAnalysisTests(unittest.TestCase):
    def test_entire_file_ignores_selected_page_and_populates_viewer_and_split_input(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"pdf")
            settings = replace(get_settings(), output_dir=root / "runs", render_dpi=200)
            adapter = _Adapter("docling")

            with _analysis_patches(adapter, page_count=3) as prepare:
                run = analyze_pdf(
                    source,
                    "2",
                    ["docling"],
                    settings,
                    min_confidence=0.25,
                    dpi=144,
                    parse_entire_file=True,
                )

            self.assertEqual(prepare.call_args.args[1], [1, 2, 3])
            self.assertEqual(prepare.call_args.args[3], 144)
            self.assertEqual(adapter.calls, [[1, 2, 3]])
            self.assertEqual([page.page for page in run.pages], [1, 2, 3])
            self.assertEqual([record.page for record in run.records], [1, 2, 3])
            viewer_payload = json.loads(Path(run.viewer_data_path).read_text(encoding="utf-8"))
            self.assertEqual([page["page"] for page in viewer_payload["pages"]], [1, 2, 3])
            self.assertEqual([record["page"] for record in viewer_payload["records"]], [1, 2, 3])
            saved_path = parse_input_path(settings.output_dir, source.name, "docling")
            self.assertEqual(run.statuses[0].saved_input_path, str(saved_path))
            self.assertEqual(viewer_payload["engines"][0]["saved_input_path"], str(saved_path))
            self.assertTrue(saved_path.exists())
            self.assertIn("分割用入力を保存しました", run.statuses[0].message)

    def test_analysis_saves_document_classification_metadata(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"pdf")
            settings = replace(get_settings(), output_dir=root / "runs")
            classification = classification_from_selection(
                source_path=source,
                large_category="在庫管理",
                middle_category="操作説明書",
                small_category="倉庫連携",
            )

            with _analysis_patches(_Adapter("docling"), page_count=1):
                run = analyze_pdf(
                    source,
                    "1",
                    ["docling"],
                    settings,
                    parse_entire_file=True,
                    classification=classification,
                )

            results_payload = json.loads(Path(run.json_path).read_text(encoding="utf-8"))
            viewer_payload = json.loads(Path(run.viewer_data_path).read_text(encoding="utf-8"))
            saved_payload = json.loads(parse_input_path(settings.output_dir, source.name, "docling").read_text(encoding="utf-8"))

            self.assertEqual(run.classification["large_category"], "在庫管理")
            self.assertEqual(results_payload["classification"]["middle_category"], "操作説明書")
            self.assertEqual(viewer_payload["classification"]["small_category"], "倉庫連携")
            self.assertEqual(saved_payload["document"]["classification"]["source"], "manual")

    def test_entire_file_saves_docling_vision_option(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"pdf")
            settings = replace(get_settings(), output_dir=root / "runs")

            with (
                _analysis_patches(_Adapter("docling"), page_count=1),
                patch(
                    "rag_engine.parsing.picture_descriptions.describe_docling_pictures",
                    return_value=SimpleNamespace(succeeded=0, targets=0, failed=0),
                ),
            ):
                run = analyze_pdf(
                    source,
                    "1",
                    ["docling"],
                    settings,
                    parse_entire_file=True,
                    use_docling_vision=True,
                )

            saved_payload = json.loads(parse_input_path(settings.output_dir, source.name, "docling").read_text(encoding="utf-8"))
            self.assertTrue(saved_payload["parse"]["options"]["use_docling_vision"])
            self.assertTrue(run.statuses[0].use_docling_vision)

    def test_single_page_analysis_does_not_update_split_input(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"pdf")
            settings = replace(get_settings(), output_dir=root / "runs")
            adapter = _Adapter("docling")

            with _analysis_patches(adapter, page_count=3) as prepare:
                run = analyze_pdf(source, "2", ["docling"], settings)

            self.assertEqual(prepare.call_args.args[1], [2])
            self.assertEqual(adapter.calls, [[2]])
            self.assertIsNone(run.statuses[0].saved_input_path)
            self.assertFalse((settings.output_dir / "parse_inputs").exists())

    def test_failed_reparse_keeps_previous_engine_input(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"pdf")
            settings = replace(get_settings(), output_dir=root / "runs")
            saved_path = parse_input_path(settings.output_dir, source.name, "docling")

            with _analysis_patches(_Adapter("docling"), page_count=1):
                analyze_pdf(source, "1", ["docling"], settings, parse_entire_file=True)
            previous = saved_path.read_bytes()

            with _analysis_patches(_Adapter("docling", error=RuntimeError("engine failed")), page_count=1):
                failed_run = analyze_pdf(source, "1", ["docling"], settings, parse_entire_file=True)

            self.assertEqual(saved_path.read_bytes(), previous)
            self.assertFalse(failed_run.statuses[0].available)
            self.assertIn("engine failed", failed_run.statuses[0].message)

    def test_save_failure_keeps_preview_records_and_reports_failure(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"pdf")
            settings = replace(get_settings(), output_dir=root / "runs")

            with (
                _analysis_patches(_Adapter("docling"), page_count=1),
                patch("rag_engine.parsing.analysis.save_parse_input", side_effect=OSError("disk full")),
            ):
                run = analyze_pdf(source, "1", ["docling"], settings, parse_entire_file=True)

            self.assertTrue(run.statuses[0].available)
            self.assertEqual(len(run.records), 1)
            self.assertIsNone(run.statuses[0].saved_input_path)
            self.assertIn("以前の保存結果は変更されていません", run.statuses[0].message)

    def test_docling_partial_success_preserves_complete_saved_input(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"pdf")
            settings = replace(get_settings(), output_dir=root / "runs")
            adapter = DoclingAdapter(settings)
            converted = SimpleNamespace(
                status=SimpleNamespace(value="success"),
                errors=[],
                document=SimpleNamespace(export_to_dict=lambda: {}, tables=[]),
            )
            converter = SimpleNamespace(convert=lambda *args, **kwargs: converted)
            with (
                _analysis_patches(adapter, page_count=2),
                patch.object(adapter, "availability", return_value=SimpleNamespace(available=True)),
                patch.object(adapter, "_converter", return_value=converter),
                patch("rag_engine.adapters.parsers.docling_adapter._prepare_docling_env"),
                patch.object(adapter, "_records_from_payload", return_value=[_record("docling", 1), _record("docling", 2)]) as records,
            ):
                analyze_pdf(source, "all", ["docling"], settings, parse_entire_file=True)
                saved_path = parse_input_path(settings.output_dir, source.name, "docling")
                previous = saved_path.read_bytes()
                converted.status = SimpleNamespace(value="partial_success")
                converted.errors = [SimpleNamespace(error_message="Page 2 conversion failed")]
                records.return_value = [_record("docling", 1)]
                failed_run = analyze_pdf(source, "all", ["docling"], settings, parse_entire_file=True)

            self.assertEqual(saved_path.read_bytes(), previous)
            self.assertEqual(records.call_count, 1)
            self.assertFalse(failed_run.statuses[0].available)
            self.assertIsNone(failed_run.statuses[0].saved_input_path)
            self.assertEqual(failed_run.records, [])
            self.assertIn("partial_success", failed_run.statuses[0].message)
            self.assertIn("Page 2 conversion failed", failed_run.statuses[0].message)


class _Adapter:
    def __init__(self, engine_id: str, error: Exception | None = None):
        self.engine_id = engine_id
        self.error = error
        self.calls: list[list[int]] = []

    def availability(self):
        return SimpleNamespace(available=True, message="")

    def analyze(self, context):
        self.calls.append([page.page for page in context.pages])
        if self.error:
            raise self.error
        return [_record(self.engine_id, page.page) for page in context.pages]


@contextmanager
def _analysis_patches(adapter: _Adapter, page_count: int):
    def prepare(source_path, page_numbers, output_dir, dpi):
        pages_dir = Path(output_dir) / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        pages = [
            PageImage(
                page=page,
                width=100,
                height=200,
                pdf_width=50,
                pdf_height=100,
                image_path=str(pages_dir / f"page_{page:04d}.png"),
            )
            for page in page_numbers
        ]
        return str(source_path), pages

    with (
        patch("rag_engine.parsing.analysis.ENGINE_ORDER", [adapter.engine_id]),
        patch("rag_engine.parsing.analysis.ENGINE_LABELS", {adapter.engine_id: adapter.engine_id.title()}),
        patch("rag_engine.parsing.analysis.build_adapters", return_value={adapter.engine_id: adapter}),
        patch("rag_engine.parsing.analysis.get_source_page_count", return_value=page_count),
        patch("rag_engine.parsing.analysis.prepare_source_for_analysis", side_effect=prepare) as prepare_mock,
        patch("rag_engine.parsing.analysis.model_pool.trim_cuda_cache"),
    ):
        yield prepare_mock


def _record(engine: str, page: int) -> LayoutRecord:
    return LayoutRecord(
        id=f"{engine}-p{page}-1",
        engine=engine,
        page=page,
        seq_no=1,
        bbox=[1, 2, 3, 4],
        coord_system="image_top_left",
        page_width=100,
        page_height=200,
        category="Text",
        text=f"page {page}",
    )


if __name__ == "__main__":
    unittest.main()
