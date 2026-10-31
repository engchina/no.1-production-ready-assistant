"""parse inputs の挙動を保護するテスト。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.parse_inputs import file_sha256, filename_key, parse_input_path, save_parse_input


class ParseInputTests(unittest.TestCase):
    def test_same_filename_and_engine_atomically_overwrites_one_json(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_source = root / "first" / "manual.pdf"
            second_source = root / "second" / "manual.pdf"
            first_source.parent.mkdir()
            second_source.parent.mkdir()
            first_source.write_bytes(b"first-version")
            second_source.write_bytes(b"second-version")
            output_dir = root / "runs"
            pages = [_page(2), _page(1)]

            first_path = save_parse_input(
                output_dir=output_dir,
                source_path=first_source,
                source_sha256=file_sha256(first_source),
                page_count=2,
                engine_id="docling",
                engine_label="Docling",
                pages=pages,
                records=[_record("docling", 2, 2, "later"), _record("docling", 1, 1, "first")],
                dpi=200,
                min_confidence=0.25,
                use_docling_vision=True,
            )
            second_path = save_parse_input(
                output_dir=output_dir,
                source_path=second_source,
                source_sha256=file_sha256(second_source),
                page_count=2,
                engine_id="docling",
                engine_label="Docling",
                pages=pages,
                records=[
                    _record("docling", 2, 2, "replacement page 2"),
                    _record("other", 1, 1, "ignored"),
                    _record("docling", 1, 3, "replacement page 1"),
                ],
                dpi=300,
                min_confidence=0.5,
                use_docling_vision=False,
            )

            self.assertEqual(first_path, second_path)
            self.assertEqual(list(second_path.parent.glob("docling.json")), [second_path])
            payload = json.loads(second_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 1)
            self.assertEqual(payload["document"]["file_name"], "manual.pdf")
            self.assertEqual(payload["document"]["source_sha256"], file_sha256(second_source))
            self.assertEqual(payload["parse"]["scope"], "whole_file")
            self.assertFalse(payload["parse"]["options"]["use_docling_vision"])
            self.assertEqual(payload["parse"]["record_count"], 2)
            self.assertEqual(
                [(record["page"], record["text"]) for record in payload["records"]],
                [(1, "replacement page 1"), (2, "replacement page 2")],
            )
            self.assertEqual([page["page"] for page in payload["pages"]], [1, 2])
            self.assertNotIn("image_path", payload["pages"][0])
            self.assertNotIn("image_url", payload["pages"][0])

    def test_different_engines_do_not_overwrite_each_other(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"source")
            output_dir = root / "runs"
            common = {
                "output_dir": output_dir,
                "source_path": source,
                "source_sha256": file_sha256(source),
                "page_count": 1,
                "pages": [_page(1)],
                "dpi": 300,
                "min_confidence": 0.5,
                "use_docling_vision": False,
            }

            docling_path = save_parse_input(
                **common,
                engine_id="docling",
                engine_label="Docling",
                records=[_record("docling", 1, 1, "docling")],
            )
            original_docling = docling_path.read_bytes()
            archived_path = save_parse_input(
                **common,
                engine_id="archived_parser",
                engine_label="Archived parser",
                records=[_record("archived_parser", 1, 1, "archived")],
            )

            self.assertNotEqual(docling_path, archived_path)
            self.assertEqual(docling_path.read_bytes(), original_docling)
            self.assertTrue(archived_path.exists())

    def test_failed_replace_keeps_previous_input_and_removes_temporary_file(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "manual.pdf"
            source.write_bytes(b"source")
            output_dir = root / "runs"
            target = parse_input_path(output_dir, source.name, "docling")
            target.parent.mkdir(parents=True)
            target.write_text("previous", encoding="utf-8")

            with patch("rag_engine.parsing.parse_inputs.os.replace", side_effect=OSError("replace failed")), self.assertRaises(OSError):
                save_parse_input(
                    output_dir=output_dir,
                    source_path=source,
                    source_sha256=file_sha256(source),
                    page_count=1,
                    engine_id="docling",
                    engine_label="Docling",
                    pages=[_page(1)],
                    records=[_record("docling", 1, 1, "new")],
                    dpi=300,
                    min_confidence=0.5,
                    use_docling_vision=False,
                )

            self.assertEqual(target.read_text(encoding="utf-8"), "previous")
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_filename_identity_is_nfc_normalized_and_case_sensitive(self):
        self.assertEqual(filename_key("café.pdf"), filename_key("cafe\u0301.pdf"))
        self.assertNotEqual(filename_key("Manual.pdf"), filename_key("manual.pdf"))


def _page(page: int) -> PageImage:
    return PageImage(
        page=page,
        width=100,
        height=200,
        pdf_width=50,
        pdf_height=100,
        image_path=f"/temporary/page-{page}.png",
        image_url=f"/temporary/page-{page}.png",
    )


def _record(engine: str, page: int, seq_no: int, text: str) -> LayoutRecord:
    return LayoutRecord(
        id=f"{engine}-p{page}-{seq_no}",
        engine=engine,
        page=page,
        seq_no=seq_no,
        bbox=[1, 2, 3, 4],
        coord_system="image_top_left",
        page_width=100,
        page_height=200,
        category="Text",
        text=text,
    )


if __name__ == "__main__":
    unittest.main()
