"""表内画像の検出から Vision 補足、検索 chunk までの回帰テスト。"""

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pypdfium2 as pdfium
from PIL import Image

from rag_engine.chunking import ChunkingConfig, build_small_to_big_chunks
from rag_engine.config import get_settings
from rag_engine.models.layout import LayoutRecord
from rag_engine.parsing.picture_descriptions import describe_docling_pictures
from rag_engine.parsing.rendering import render_pdf_pages
from rag_engine.parsing.table_vision import missing_table_image_regions


def _fixture(root, rotation=0):
    image_path = root / "dialog.jpg"
    Image.new("RGB", (100, 50), "gray").save(image_path)
    pdf_path = root / "manual.pdf"
    doc = pdfium.PdfDocument.new()
    page = doc.new_page(200, 300)
    image = pdfium.PdfImage.new(doc)
    image.load_jpeg(str(image_path))
    image.set_matrix(pdfium.PdfMatrix(60, 0, 0, 40, 20, 210))
    page.insert_obj(image)
    page.set_rotation(rotation)
    page.gen_content()
    doc.save(str(pdf_path))
    page.close()
    doc.close()
    pages = render_pdf_pages(pdf_path, [1], root, 72)
    # 0 度では画像 bbox は [20, 50, 80, 90]、90 度では [210, 20, 250, 80]。
    bbox = [10, 40, 180, 100] if rotation == 0 else [200, 10, 260, 180]
    table = LayoutRecord(
        "table-1",
        "docling",
        1,
        1,
        bbox,
        "image_top_left",
        pages[0].width,
        pages[0].height,
        "Table",
        "<table><tr><th>説明</th></tr><tr><td>右の説明</td></tr></table>",
        raw_type="table",
    )
    return pdf_path, pages, table


def test_detects_uncovered_images_and_excludes_text_only_or_covered_tables():
    with TemporaryDirectory() as tmp:
        for rotation in (0, 90):
            root = Path(tmp)
            pdf_path, pages, table = _fixture(root, rotation)
            regions = missing_table_image_regions([table], pages, pdf_path)
            assert len(regions[table.id]) == 1
            picture = replace(table, id="picture-1", category="Picture", raw_type="picture", bbox=regions[table.id][0], text="既存画像説明")
            assert missing_table_image_regions([table, picture], pages, pdf_path) == {}
            plain = replace(table, bbox=[0, 0, 10, 10])
            assert missing_table_image_regions([plain], pages, pdf_path) == {}


def test_table_vision_preserves_html_and_adds_full_supplement_to_parent_and_child():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdf_path, pages, table = _fixture(root)
        original = table.text
        supplement = "次月処理を行います。キャンセルで中止。" + "補足" * 150 + "末尾の重要条件"
        with patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value={"retrieval_text": supplement}) as vision:
            stats = describe_docling_pictures(
                [table], pages, run_dir=root, pdf_name=pdf_path.name, settings=get_settings(), pdf_path=pdf_path
            )
        assert (stats.targets, stats.succeeded, stats.failed) == (1, 1, 0)
        assert table.text == original
        assert table.raw["table_vision_text"].endswith("末尾の重要条件")
        assert vision.call_args.args[1]["existing_table_html"] == original
        assert "extraction_instruction" not in vision.call_args.args[1]
        assert vision.call_args.kwargs["target_kind"] == "table"
        payload = {"run_id": "test", "pdf_name": pdf_path.name, "records": [table.to_dict()]}
        chunks = build_small_to_big_chunks(payload, source_run_id="test", selected_engine_ids=["docling"], config=ChunkingConfig())
        assert {c.chunk_level for c in chunks} == {"child", "parent"}
        assert all(supplement in c.text for c in chunks)
        with patch("rag_engine.parsing.picture_descriptions.describe_picture") as vision:
            stats = describe_docling_pictures(
                [table], pages, run_dir=root, pdf_name=pdf_path.name, settings=get_settings(), pdf_path=pdf_path
            )
            assert stats.targets == 0
            vision.assert_not_called()


def test_table_vision_failure_preserves_table_and_records_error():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        pdf_path, pages, table = _fixture(root)
        original = table.text
        with patch("rag_engine.parsing.picture_descriptions.describe_picture", side_effect=RuntimeError("unavailable")):
            stats = describe_docling_pictures(
                [table], pages, run_dir=root, pdf_name=pdf_path.name, settings=get_settings(), pdf_path=pdf_path
            )
        assert (stats.succeeded, stats.failed) == (0, 1)
        assert table.text == original
        assert "unavailable" in table.raw["vision_error"]
        assert "table_vision_text" not in table.raw


def test_large_table_supplement_survives_row_group_chunking_once():
    with TemporaryDirectory() as tmp:
        _, _, table = _fixture(Path(tmp))
        table.text = (
            "<table><tr><th>説明</th></tr>" + "".join(f"<tr><td>{i}: {'手続きの説明' * 50}</td></tr>" for i in range(20)) + "</table>"
        )
        table.raw["table_vision_text"] = "固有のダイアログ補足"
        chunks = build_small_to_big_chunks(
            {"pdf_name": "manual.pdf", "records": [table.to_dict()]},
            source_run_id="test",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(child_target_chars=300, table_child_target_chars=300, parent_target_chars=1200),
        )
        children = [c for c in chunks if c.chunk_level == "child"]
        assert len(children) > 1
        assert sum("固有のダイアログ補足" in c.text for c in children) == 1
        assert any("固有のダイアログ補足" in c.text for c in chunks if c.chunk_level == "parent")


def test_nested_form_image_coordinates_and_scanned_table_are_detected():
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        original, _, table = _fixture(root)
        source = pdfium.PdfDocument(str(original))
        dest = pdfium.PdfDocument.new()
        page = dest.new_page(400, 600)
        xobject = source.page_as_xobject(0, dest)
        form = xobject.as_pageobject()
        form.set_matrix(pdfium.PdfMatrix(2, 0, 0, 2, 0, 0))
        page.insert_obj(form)
        page.gen_content()
        path = root / "form.pdf"
        dest.save(str(path))
        page.close()
        xobject.close()
        dest.close()
        source.close()
        pages = render_pdf_pages(path, [1], root, 72)
        table.bbox = [20, 80, 360, 200]
        assert missing_table_image_regions([table], pages, path)[table.id] == [[40, 100, 160, 180]]
        # 全面スキャン画像の中の表は、表に重なる部分だけで判定します。
        table.bbox = [50, 110, 100, 170]
        assert missing_table_image_regions([table], pages, path)[table.id] == [table.bbox]


def test_picture_finishes_before_table_fallback_detection():
    for fail_picture in (False, True):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            pdf_path, pages, table = _fixture(root)
            region = missing_table_image_regions([table], pages, pdf_path)[table.id][0]
            picture = replace(table, id="picture", seq_no=2, category="Picture", raw_type="picture", bbox=region, text="", raw={})
            calls = []

            # describe_picture の side_effect として同じ反復の中でだけ呼ぶため、遅延束縛は問題にならない。
            def describe(_path, metadata, _settings, **kwargs):
                calls.append(metadata["record_id"])  # noqa: B023
                if fail_picture and metadata["record_id"] == "picture":  # noqa: B023
                    raise RuntimeError("picture unavailable")
                return {"retrieval_text": "確認ダイアログで登録を実行します。", "visual_kind": "screenshot"}

            with patch("rag_engine.parsing.picture_descriptions.describe_picture", side_effect=describe):
                stats = describe_docling_pictures(
                    [table, picture], pages, run_dir=root, pdf_name=pdf_path.name, settings=get_settings(), pdf_path=pdf_path
                )
            assert calls == (["picture", table.id] if fail_picture else ["picture"])
            assert stats.targets == (2 if fail_picture else 1)
            assert stats.failed == int(fail_picture)
            assert stats.succeeded == 1
