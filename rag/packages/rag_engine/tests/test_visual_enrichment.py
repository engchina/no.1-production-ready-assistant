"""画像の業務情報、所属節、表行との対応を実際の処理経路で保護する。"""

from dataclasses import replace
from unittest.mock import patch

import pypdfium2 as pdfium
from PIL import Image

from rag_engine.chunking import ChunkingConfig, build_small_to_big_chunks
from rag_engine.config import get_settings
from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.decorative_pictures import classify_picture_record, mark_picture_record_role
from rag_engine.parsing.picture_descriptions import describe_docling_pictures
from rag_engine.parsing.rendering import render_pdf_pages
from rag_engine.parsing.table_vision import missing_table_image_regions
from rag_engine.parsing.table_visual_rows import enrich_table_visual_rows


def _page(root):
    path = root / "page.png"
    Image.new("RGB", (1000, 1000), "white").save(path)
    return PageImage(1, 1000, 1000, 1000, 1000, str(path))


def _record(id, category, seq, bbox, text="", raw=None):
    return LayoutRecord(
        id,
        "docling",
        1,
        seq,
        bbox,
        "image_top_left",
        1000,
        1000,
        category,
        text,
        raw_type="picture" if category == "Picture" else category.lower(),
        raw=raw or {},
    )


def test_mixed_footer_retains_title_and_does_not_inherit_next_section(tmp_path):
    page = _page(tmp_path)
    title = _record("title", "Section-header", 1, [10, 10, 400, 40], "B-1 締め年月の更新")
    picture = _record("picture", "Picture", 2, [20, 100, 700, 230])
    ocr = replace(
        picture,
        id="ocr",
        seq_no=3,
        raw_type="picture_ocr_text",
        text="OCR抽出テキスト:\n８倉庫連動処理\nCopyright© サンプル株式会社 All rights reserved.",
        raw={},
    )
    following = _record("next", "Section-header", 4, [20, 240, 500, 270], "B-2 出荷情報")
    with patch(
        "rag_engine.parsing.picture_descriptions.describe_picture",
        return_value={
            "visual_kind": "logo",
            "retrieval_text": "８倉庫連動処理",
            "visible_screen_names": ["８倉庫連動処理"],
        },
    ) as vision:
        result = describe_docling_pictures(
            [title, picture, ocr, following], [page], run_dir=tmp_path, pdf_name="manual.pdf", settings=get_settings()
        )
    assert result.succeeded == 1
    assert "８倉庫連動処理" in picture.text
    assert not picture.raw.get("rag_excluded")
    metadata = vision.call_args.args[1]
    assert metadata["owning_section"]["record_id"] == "title"
    assert all(r["record_id"] != "next" for r in metadata["nearby_headings"])
    assert all(r["record_id"] != "next" for r in metadata["context_source_record_refs"])
    assert metadata["next_record"]["record_id"] == "next"
    # 先に保存された装飾判定も OCR の業務情報があれば修復可能にします。
    picture.raw = {"visual_role": "decorative", "picture_kind": "logo"}
    assert classify_picture_record(picture, [picture, ocr]).role == "content"
    picture.raw.update(vision_skipped=True, vision_status="skipped", vision_description={"image_summary": "title"})
    mark_picture_record_role(picture, classify_picture_record(picture, [picture, ocr]))
    assert picture.raw["vision_skipped"] is False
    assert picture.raw["vision_status"] == "succeeded"


def test_matched_rows_follow_original_order_and_survive_separate_chunks(tmp_path):
    page = _page(tmp_path)
    descriptions = [
        "最初の操作を取り消す場合は担当者まで連絡してください。" + "説明" * 80,
        "登録が完了していない場合は詳細画面から登録してください。" + "補足" * 80,
    ]
    table = _record(
        "table",
        "Table",
        1,
        [10, 10, 900, 900],
        "<table><tr><th>説明</th></tr>" + "".join(f"<tr><td>{text}</td></tr>" for text in descriptions) + "</table>",
        {
            "data": {
                "table_cells": [
                    {"bbox": {"t": 100, "b": 200}, "start_row_offset_idx": 1, "end_row_offset_idx": 2},
                    {"bbox": {"t": 500, "b": 600}, "start_row_offset_idx": 2, "end_row_offset_idx": 3},
                ]
            },
            "table_missing_picture_regions": [[20, 90, 200, 220], [20, 490, 200, 650]],
        },
    )
    # Vision の行順が逆でも、元の説明本文と一意に対応付けます。
    enrich_table_visual_rows(
        table, page, {"table_rows": [["後半専用ダイアログ", descriptions[1]], ["前半専用ダイアログ", descriptions[0]]]}, tmp_path
    )
    assert len(table.raw["table_row_image_evidence"]) == 2
    assert table.raw["table_enhanced_html"].index("前半専用") < table.raw["table_enhanced_html"].index("後半専用")
    chunks = build_small_to_big_chunks(
        {"pdf_name": "manual.pdf", "records": [table.to_dict()]},
        source_run_id="test",
        selected_engine_ids=["docling"],
        config=ChunkingConfig(child_target_chars=150, table_child_target_chars=150, parent_target_chars=300),
    )
    children = [c for c in chunks if c.chunk_level == "child"]
    assert len(children) == 2
    for index, label in enumerate(["前半専用", "後半専用"]):
        chunk = next(c for c in children if label in c.text)
        assert chunk.metadata["table_context"][0]["structure"]["column_count"] == 2
        evidence = chunk.metadata["image_evidence"]
        assert len(evidence) == 1
        assert f"row-{index + 2}" in evidence[0]["image_id"]


def test_ambiguous_table_rows_are_not_assigned_by_position(tmp_path):
    page = _page(tmp_path)
    text = "同じ操作説明が繰り返されています。"
    table = _record(
        "table", "Table", 1, [0, 0, 900, 900], f"<table><tr><th>説明</th></tr><tr><td>{text}</td></tr><tr><td>{text}</td></tr></table>"
    )
    table.raw.update(table_enhanced_html="stale", table_row_image_evidence=[{"image_id": "stale"}])
    enrich_table_visual_rows(table, page, {"table_rows": [["確認ダイアログ", text]]})
    assert table.raw["table_vision_rows"] == []
    assert "table_enhanced_html" not in table.raw
    assert "table_row_image_evidence" not in table.raw


def test_failed_or_excluded_picture_does_not_hide_table_gap(tmp_path):
    path = tmp_path / "manual.pdf"
    jpg = tmp_path / "dialog.jpg"
    Image.new("RGB", (100, 60), "gray").save(jpg)
    with pdfium.PdfDocument.new() as doc:
        page = doc.new_page(200, 300)
        obj = pdfium.PdfImage.new(doc)
        obj.load_jpeg(str(jpg))
        obj.set_matrix(pdfium.PdfMatrix(100, 0, 0, 60, 20, 200))
        page.insert_obj(obj)
        page.gen_content()
        doc.save(str(path))
        page.close()
    pages = render_pdf_pages(path, [1], tmp_path, 72)
    table = _record("table", "Table", 1, [10, 30, 190, 110], "<table><tr><td>説明</td></tr></table>")
    picture = _record("picture", "Picture", 2, [20, 40, 120, 100], "古い説明", {"vision_error": "timeout"})
    assert "table" in missing_table_image_regions([table, picture], pages, path)
    picture.raw = {"rag_excluded": True}
    assert "table" in missing_table_image_regions([table, picture], pages, path)
    picture.raw = {}
    assert missing_table_image_regions([table, picture], pages, path) == {}


def test_vector_diagram_fallback_ignores_thin_table_rules(tmp_path):
    path = tmp_path / "vectors.pdf"
    with pdfium.PdfDocument.new() as doc:
        page = doc.new_page(200, 300)
        for i in range(8):
            obj = pdfium.raw.FPDFPageObj_CreateNewRect(25 + (i % 4) * 15, 215 + (i // 4) * 20, 10, 10)
            pdfium.raw.FPDFPath_SetDrawMode(obj, pdfium.raw.FPDF_FILLMODE_WINDING, False)
            pdfium.raw.FPDFPage_InsertObject(page.raw, obj)
        # 下の表は罫線のみであり、図として再処理しません。
        for i in range(8):
            obj = pdfium.raw.FPDFPageObj_CreateNewRect(10, 20 + i * 5, 170, 0.5)
            pdfium.raw.FPDFPage_InsertObject(page.raw, obj)
        page.gen_content()
        doc.save(str(path))
        page.close()
    pages = render_pdf_pages(path, [1], tmp_path, 72)
    table = _record("diagram", "Table", 1, [10, 30, 190, 110])
    plain = _record("plain", "Table", 2, [5, 230, 195, 290])
    regions = missing_table_image_regions([table, plain], pages, path)
    assert "diagram" in regions
    assert table.raw["table_vector_fallback"] is True
    assert "plain" not in regions


def test_restored_table_rebuilds_row_images_at_new_dpi(tmp_path):
    from rag_engine.parsing.parse_inputs import file_sha256, load_parse_inputs, save_parse_input
    from rag_engine.parsing.visual_artifacts import persist_semantic_visual_crops

    page = _page(tmp_path)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source fingerprint")
    text = "出荷情報が登録されていない場合の処理説明です。"
    table = _record(
        "table",
        "Table",
        1,
        [0, 0, 900, 900],
        f"<table><tr><th>説明</th></tr><tr><td>{text}</td></tr></table>",
        {
            "vision_description": {"table_rows": [["得意先の出荷情報が未処理です。", text]]},
            "data": {"table_cells": [{"bbox": {"t": 100, "b": 200}, "start_row_offset_idx": 1, "end_row_offset_idx": 2}]},
            "table_missing_picture_regions": [[20, 90, 200, 220]],
        },
    )
    save_parse_input(
        output_dir=tmp_path,
        source_path=source,
        source_sha256=file_sha256(source),
        page_count=1,
        engine_id="docling",
        engine_label="Docling",
        pages=[page],
        records=[table],
        dpi=72,
        min_confidence=0,
        use_docling_vision=True,
    )
    resized = tmp_path / "resized.png"
    Image.new("RGB", (500, 500), "white").save(resized)
    current = replace(page, width=500, height=500, image_path=str(resized))
    restored = load_parse_inputs(output_dir=tmp_path, source_path=source, page_count=1, pages=[current], engine_order=["docling"])
    record = restored.inputs[0].records[0]
    assert record.raw["table_missing_picture_regions"] == [[10, 45, 100, 110]]
    run = tmp_path / "new-run"
    stats = persist_semantic_visual_crops([record], [current], run_dir=run)
    assert stats.failed == 0
    image = record.raw["table_row_image_evidence"][0]
    with Image.open(run / image["crop_path"]) as crop:
        assert crop.size == (90, 65)


def test_image_columns_keep_left_middle_and_right_positions_and_caption(tmp_path):
    from rag_engine.parsing.table_structure import parse_html_table_structure

    left, right = "対象となる申込区分です。", "登録完了後の詳細な操作説明です。"
    for position in range(3):
        table = _record(
            "table",
            "Table",
            1,
            [0, 0, 900, 900],
            f"<table><caption>年度別割引基準2026</caption><tr><th>区分</th><th>説明</th></tr>"
            f"<tr><th>{left}</th><td>{right}</td></tr></table>",
        )
        cells = [left, right]
        cells.insert(position, "確認ダイアログ [OK]")
        enrich_table_visual_rows(table, _page(tmp_path), {"table_rows": [cells]})
        structure = parse_html_table_structure(table.raw["table_enhanced_html"])
        assert [cell["text"] for cell in structure["rows"][1]["cells"]] == cells
        assert structure["caption"] == "年度別割引基準2026"
        assert next(cell for cell in structure["rows"][1]["cells"] if cell["text"] == left)["is_header"]
        chunks = build_small_to_big_chunks(
            {"pdf_name": "manual.pdf", "records": [table.to_dict()]},
            source_run_id="test",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        assert all("年度別割引基準2026" in c.text for c in chunks)


def test_conflicting_column_positions_do_not_produce_false_restoration(tmp_path):
    texts = ["最初の行の取り消し操作について説明します。", "次の行では登録情報の照会を実行します。"]
    table = _record(
        "table",
        "Table",
        1,
        [0, 0, 900, 900],
        "<table><tr><th>説明</th></tr>" + "".join(f"<tr><td>{text}</td></tr>" for text in texts) + "</table>",
    )
    enrich_table_visual_rows(table, _page(tmp_path), {"table_rows": [["左側画像", texts[0]], [texts[1], "右側画像"]]})
    assert len(table.raw["table_vision_rows"]) == 2
    assert "table_enhanced_html" not in table.raw


def test_merged_cells_preserve_original_structure_without_guessing(tmp_path):
    text = "複数列にまたがる説明はそのまま保持してください。"
    html = f'<table><caption>結合表</caption><tr><th colspan="2">説明</th></tr><tr><td colspan="2">{text}</td></tr></table>'
    table = _record("table", "Table", 1, [0, 0, 900, 900], html)
    enrich_table_visual_rows(table, _page(tmp_path), {"table_rows": [["画像", text]]})
    assert table.text == html
    assert table.raw["table_vision_rows"][0]["text"] == "画像"
    assert "table_enhanced_html" not in table.raw


def test_context_crop_is_the_whole_page_with_the_target_outlined(tmp_path):
    # 周辺文脈画像は対象図を含むページ全体 (#638)。隣接 record の窓や節見出しで切らない。
    from rag_engine.parsing.picture_descriptions import VISION_TARGET_OUTLINE, _save_context_crop

    _page(tmp_path)
    picture = _record("picture", "Picture", 3, [400, 400, 500, 500])
    giant = _record("table", "Table", 2, [0, 0, 1000, 1000], "遠い行の無関係な説明")
    following = _record("next", "Section-header", 4, [10, 550, 900, 580], "次の操作")
    with Image.open(tmp_path / "page.png") as page:
        context = _save_context_crop(page, picture, [giant, picture, following], tmp_path)
    assert context.bbox == [0, 0, 1000, 1000]
    refs = {ref["record_id"]: ref for ref in context.source_record_refs}
    assert not refs["picture"]["partially_visible"]
    assert not refs["table"]["partially_visible"]  # ページ全体なので全 record が写る
    with Image.open(context.path) as saved:
        assert saved.size == (1000, 1000)
        assert saved.getpixel((400, 450)) == VISION_TARGET_OUTLINE  # 対象図の位置にマゼンタの枠
        assert saved.getpixel((200, 200)) == (255, 255, 255)


def test_context_records_for_the_prompt_still_exclude_the_earlier_section(tmp_path):
    # 画像はページ全体でも、prompt に渡す周辺 record の選び方は変えない。
    from rag_engine.parsing.picture_descriptions import _save_context_crop

    _page(tmp_path)
    picture = _record("picture", "Picture", 3, [400, 400, 500, 500])
    previous = _record("previous", "Table", 1, [0, 0, 1000, 350], "前節の巨大表")
    heading = _record("heading", "Section-header", 2, [350, 370, 600, 390], "対象の操作")
    with Image.open(tmp_path / "page.png") as page:
        context = _save_context_crop(page, picture, [previous, heading, picture], tmp_path)
    assert context.bbox == [0, 0, 1000, 1000]
    refs = {ref["record_id"]: ref for ref in context.source_record_refs}
    # 前節の表もページ全体には写るので参照に入るが、prompt の周辺 record の選び方（_context_records）は変わらない。
    assert set(refs) == {"picture", "heading", "previous"}
    assert not refs["previous"]["partially_visible"]


def test_pdf_geometry_corrects_reversed_vision_image_column(tmp_path):
    from rag_engine.parsing.table_structure import parse_html_table_structure

    text = "原本の右側にある詳しい処理説明です。"
    table = _record(
        "table",
        "Table",
        1,
        [0, 0, 900, 900],
        f"<table><tr><th>説明</th></tr><tr><td>{text}</td></tr></table>",
        {
            "data": {
                "table_cells": [
                    {
                        "bbox": {"l": 400, "r": 800, "t": 100, "b": 200},
                        "start_row_offset_idx": 1,
                        "end_row_offset_idx": 2,
                        "start_col_offset_idx": 0,
                    }
                ]
            },
            "table_missing_picture_regions": [[20, 90, 200, 220]],
        },
    )
    enrich_table_visual_rows(table, _page(tmp_path), {"table_rows": [[text, "左側ダイアログ"]]})
    row = parse_html_table_structure(table.raw["table_enhanced_html"])["rows"][1]
    assert [cell["text"] for cell in row["cells"]] == ["左側ダイアログ", text]
    assert table.raw["table_vision_rows"][0]["column_position_source"] == "pdf_cell_geometry"


def test_blank_vision_columns_do_not_shift_following_cells(tmp_path):
    from rag_engine.parsing.table_structure import parse_html_table_structure

    text = "操作を取り消す場合の詳しい処理説明です。"
    table = _record("table", "Table", 1, [0, 0, 900, 900], f"<table><tr><th>説明</th></tr><tr><td>{text}</td></tr></table>")
    cells = ["", text, "確認画面"]
    enrich_table_visual_rows(table, _page(tmp_path), {"table_rows": [cells]})
    row = parse_html_table_structure(table.raw["table_enhanced_html"])["rows"][1]
    assert [cell["text"] for cell in row["cells"]] == cells


def test_near_match_below_threshold_still_prevents_ambiguous_row_assignment(tmp_path):
    text = "abcdefghijklmnopqrstuvwxy"
    originals = [text[:23] + "ZZ", text[:21] + "ZZZZ"]
    table = _record(
        "table",
        "Table",
        1,
        [0, 0, 900, 900],
        "<table><tr><th>説明</th></tr>" + "".join(f"<tr><td>{value}</td></tr>" for value in originals) + "</table>",
    )
    enrich_table_visual_rows(table, _page(tmp_path), {"table_rows": [["確認画像", text]]})
    assert "table_enhanced_html" not in table.raw
    assert table.raw["table_vision_rows"] == []


def test_context_includes_smaller_subheadings_of_the_owning_section_but_stops_at_a_peer_heading(tmp_path):
    # 画面画像の直下の「画面説明」「操作説明」は同じ節の小見出しで、その本文が画像の説明になる (#599)。
    from rag_engine.parsing.picture_descriptions import _save_context_crop

    page = _page(tmp_path)
    owning = _record("owning", "Section-header", 1, [10, 10, 600, 50], "別紙倉庫連携 －（2）連携先の一覧")
    picture = _record("picture", "Picture", 2, [100, 60, 700, 400])
    sub_a = _record("sub-a", "Section-header", 3, [100, 410, 300, 430], "A【画面説明】")
    body_a = _record(
        "body-a",
        "Text",
        4,
        [120, 435, 700, 455],
        "予約を登録しておくと、毎日など決まった間隔で指定した条件のファイルを出力できる設定画面です。",
    )
    sub_b = _record("sub-b", "Section-header", 5, [100, 460, 300, 480], "B【操作説明】")
    peer = _record("peer", "Section-header", 6, [10, 500, 600, 540], "別紙倉庫連携 －（3）帳票一覧")
    body_peer = _record("body-peer", "Text", 7, [120, 545, 700, 565], "次節の本文")
    records = [owning, picture, sub_a, body_a, sub_b, peer, body_peer]
    with Image.open(tmp_path / "page.png") as image:
        context = _save_context_crop(image, picture, records, tmp_path)
    refs = {ref["record_id"] for ref in context.source_record_refs}
    assert {"owning", "picture", "sub-a", "body-a", "sub-b"} <= refs
    assert not {"peer", "body-peer"} & refs  # 同格の次節は prompt の周辺 record に入れない
    assert context.bbox == [0, 0, 1000, 1000]  # 画像はページ全体 (#638)

    with patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value={"retrieval_text": "画面"}) as vision:
        describe_docling_pictures(records, [page], run_dir=tmp_path, pdf_name="manual.pdf", settings=get_settings())
    metadata = vision.call_args.args[1]
    assert metadata["owning_section"]["record_id"] == "owning"
    assert metadata["next_record"]["record_id"] == "sub-a"
    assert metadata["next_record"]["section_role"] == "subheading_in_owning_section"
    assert "sub-a" not in {r["record_id"] for r in metadata["nearby_headings"]}

    # 所属見出しがないページでは、後続の見出しはすべて境界のまま。
    with Image.open(tmp_path / "page.png") as image:
        context = _save_context_crop(image, picture, [picture, sub_a, body_a], tmp_path)
    assert {ref["record_id"] for ref in context.source_record_refs} == {"picture"}


def test_wrapped_subheading_is_compared_by_line_height_from_the_text_layer(tmp_path):
    # 2 行に折り返した小見出しは bbox が高くなる。text layer の行数で割って文字サイズで比べる (#603)。
    from rag_engine.parsing.picture_descriptions import _boundary_sections, _heading_line_count

    def prov(bbox):
        return {"prov": [{"page_no": 1, "bbox": {"l": bbox[0], "t": bbox[1], "r": bbox[2], "b": bbox[3], "coord_origin": "TOPLEFT"}}]}

    owning = _record("owning", "Section-header", 1, [10, 10, 600, 50], "別紙倉庫連携", prov([10, 10, 600, 50]))
    picture = _record("picture", "Picture", 2, [100, 60, 700, 400])
    wrapped = _record("sub", "Section-header", 3, [100, 410, 300, 454], "A【画面説明】長い小見出し", prov([100, 410, 300, 454]))
    records = [owning, picture, wrapped]
    lines = {(10.0, 10.0): ["別紙倉庫連携"], (100.0, 410.0): ["A【画面説明】", "長い小見出し"]}
    _heading_line_count.cache_clear()
    with patch(
        "rag_engine.parsing.picture_descriptions.pdf_text_lines_in_bbox", side_effect=lambda path, page, bbox: lines[(bbox["l"], bbox["t"])]
    ) as reader:
        assert _boundary_sections(picture, records, pdf_path=tmp_path / "source.pdf") == []
        _boundary_sections(picture, records, pdf_path=tmp_path / "source.pdf")
    assert reader.call_count == 2  # 同じ見出しは cache から返す
    assert reader.call_args_list[0].args[:2] == (str(tmp_path / "source.pdf"), 1)
    # pdf_path がない、または text layer が読めない場合は bbox の高さで比べ、折り返した小見出しは境界のまま。
    assert _boundary_sections(picture, records) == [wrapped]
    _heading_line_count.cache_clear()
    with patch("rag_engine.parsing.picture_descriptions.pdf_text_lines_in_bbox", side_effect=RuntimeError("no text layer")):
        assert _boundary_sections(picture, records, pdf_path=tmp_path / "source.pdf") == [wrapped]
