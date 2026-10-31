"""parse input restore の挙動を保護するテスト。"""

import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from rag_engine.knowledge.classification import classification_from_selection
from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.parse_inputs import file_sha256, load_parse_inputs, save_parse_input


class ParseInputRestoreTests(unittest.TestCase):
    def test_loads_all_engines_in_registry_order_and_rescales_bbox(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"same source")
            output_dir = root / "runs"
            saved_pages = [_page(1, 100, 200)]
            _save(output_dir, source, "docling", saved_pages, [_record("docling", [10, 20, 30, 40])])
            _save(output_dir, source, "archived_parser", saved_pages, [_record("archived_parser", [5, 10, 15, 20])])

            restored = load_parse_inputs(
                output_dir=output_dir,
                source_path=source,
                page_count=1,
                pages=[_page(1, 200, 100)],
                engine_order=["archived_parser", "docling"],
            )

            self.assertEqual([item.engine_id for item in restored.inputs], ["archived_parser", "docling"])
            self.assertEqual(restored.warnings, [])
            archived_record = restored.inputs[0].records[0]
            docling_record = restored.inputs[1].records[0]
            self.assertEqual(archived_record.bbox, [10.0, 5.0, 30.0, 10.0])
            self.assertEqual(docling_record.bbox, [20.0, 10.0, 60.0, 20.0])
            self.assertEqual((docling_record.page_width, docling_record.page_height), (200, 100))

    def test_restores_saved_classification_metadata(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"same source")
            output_dir = root / "runs"
            classification = classification_from_selection(
                source_path=source,
                large_category="在庫管理",
                middle_category="操作説明書",
                small_category="倉庫連携",
            )
            _save(output_dir, source, "docling", [_page(1, 100, 200)], [_record("docling")], classification)

            restored = load_parse_inputs(
                output_dir=output_dir,
                source_path=source,
                page_count=1,
                pages=[_page(1, 100, 200)],
                engine_order=["docling"],
            )

            self.assertEqual(restored.inputs[0].classification["large_category"], "在庫管理")
            self.assertEqual(restored.inputs[0].classification["middle_category"], "操作説明書")
            self.assertEqual(restored.inputs[0].classification["small_category"], "倉庫連携")

    def test_restores_saved_docling_vision_option(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"same source")
            output_dir = root / "runs"
            _save(
                output_dir,
                source,
                "docling",
                [_page(1, 100, 200)],
                [_record("docling")],
                use_docling_vision=True,
            )

            restored = load_parse_inputs(
                output_dir=output_dir,
                source_path=source,
                page_count=1,
                pages=[_page(1, 100, 200)],
                engine_order=["docling"],
            )

            self.assertTrue(restored.inputs[0].use_docling_vision)

    def test_content_mismatch_skips_cached_engine_with_warning(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"old")
            output_dir = root / "runs"
            pages = [_page(1, 100, 200)]
            _save(output_dir, source, "docling", pages, [_record("docling")])
            source.write_bytes(b"new")

            restored = load_parse_inputs(
                output_dir=output_dir,
                source_path=source,
                page_count=1,
                pages=pages,
                engine_order=["docling"],
            )

            self.assertEqual(restored.inputs, [])
            self.assertEqual(len(restored.warnings), 1)
            self.assertIn("内容と一致しません", restored.warnings[0])

    def test_bad_engine_file_does_not_block_other_engine(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"source")
            output_dir = root / "runs"
            pages = [_page(1, 100, 200)]
            docling_path = _save(output_dir, source, "docling", pages, [_record("docling")])
            _save(output_dir, source, "archived_parser", pages, [_record("archived_parser")])
            docling_path.write_text("{not json", encoding="utf-8")

            restored = load_parse_inputs(
                output_dir=output_dir,
                source_path=source,
                page_count=1,
                pages=pages,
                engine_order=["archived_parser", "docling"],
            )

            self.assertEqual([item.engine_id for item in restored.inputs], ["archived_parser"])
            self.assertEqual(len(restored.warnings), 1)
            self.assertIn("docling", restored.warnings[0])

    def test_rejects_invalid_schema_engine_page_and_coordinates(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"source")
            output_dir = root / "runs"
            pages = [_page(1, 100, 200)]
            path = _save(output_dir, source, "docling", pages, [_record("docling")])
            original = json.loads(path.read_text(encoding="utf-8"))
            mutations = {
                "schema": lambda payload: payload.update(schema_version=99),
                "engine": lambda payload: payload["parse"].update(engine_id="archived_parser"),
                "page": lambda payload: payload["records"][0].update(page=2),
                "bbox": lambda payload: payload["records"][0].update(bbox=[-1, 0, 10, 10]),
            }

            for name, mutate in mutations.items():
                with self.subTest(name=name):
                    payload = deepcopy(original)
                    mutate(payload)
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    restored = load_parse_inputs(
                        output_dir=output_dir,
                        source_path=source,
                        page_count=1,
                        pages=pages,
                        engine_order=["docling", "archived_parser"],
                    )
                    self.assertEqual(restored.inputs, [])
                    self.assertEqual(len(restored.warnings), 1)

    def test_page_count_mismatch_is_not_restored(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"source")
            output_dir = root / "runs"
            _save(output_dir, source, "docling", [_page(1, 100, 200)], [_record("docling")])

            restored = load_parse_inputs(
                output_dir=output_dir,
                source_path=source,
                page_count=2,
                pages=[_page(1, 100, 200), _page(2, 100, 200)],
                engine_order=["docling"],
            )

            self.assertEqual(restored.inputs, [])
            self.assertIn("ページ数が一致しません", restored.warnings[0])


def _save(output_dir, source, engine, pages, records, classification=None, use_docling_vision=False):
    return save_parse_input(
        output_dir=output_dir,
        source_path=source,
        source_sha256=file_sha256(source),
        page_count=len(pages),
        engine_id=engine,
        engine_label=engine,
        pages=pages,
        records=records,
        dpi=300,
        min_confidence=0.5,
        use_docling_vision=use_docling_vision,
        classification=classification,
    )


def _page(page: int, width: int, height: int) -> PageImage:
    return PageImage(
        page=page,
        width=width,
        height=height,
        pdf_width=width / 2,
        pdf_height=height / 2,
        image_path=f"page-{page}.png",
    )


def _record(engine: str, bbox=None) -> LayoutRecord:
    return LayoutRecord(
        id=f"{engine}-p1-1",
        engine=engine,
        page=1,
        seq_no=1,
        bbox=bbox or [10, 20, 30, 40],
        coord_system="image_top_left",
        page_width=100,
        page_height=200,
        category="Text",
        text="cached text",
    )


if __name__ == "__main__":
    unittest.main()
