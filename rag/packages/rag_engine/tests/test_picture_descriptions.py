"""picture descriptions の挙動を保護するテスト。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from rag_engine.adapters.oci import VISION_SYSTEM_PROMPT
from rag_engine.config import get_settings
from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.parsing.picture_descriptions import (
    MAX_CHART_SERIES_VALUES,
    MAX_TABLE_COLUMNS,
    VISION_TARGET_OUTLINE,
    _bounded_vision_description,
    describe_docling_pictures,
    vision_description_text,
)
from rag_engine.parsing.vision_prompt_rules import MAX_DIAGRAM_NODES, MAX_FORM_FIELDS, MAX_TABLE_ROWS
from rag_engine.resources.jsonl import record_to_legacy_jsonl


class PictureDescriptionTests(unittest.TestCase):
    def test_formats_supported_fields_as_labeled_answer_oriented_text(self):
        description = {
            "retrieval_text": "retrieval",
            "surrounding_context": "B 【操作説明】の倉庫マスタ登録画面に属する",
            "correction_notes": "赤枠付き context crop で実行ボタンの位置を補足",
            "look_at": {"where": "upper-right", "reason": "target"},
            "search_keywords": ["keyword-a", "keyword-b"],
            "query_rewrites": ["query-a", "query-b"],
            "answerable_questions": ["question-a"],
            "exception_or_cautions": [{"condition": "warning", "detail": "confirm", "empty": ""}],
            "condition_result_pairs": [{"condition": "when-a", "result": "then-b"}],
            "operation_steps": [{"step": 1, "action": "open", "empty": ""}],
            "codes_and_errors": ["E001"],
            "visible_values": ["enabled"],
            "visible_fields": ["field-a"],
            "visible_buttons": ["run", "cancel"],
            "visible_screen_names": ["screen-a"],
            "menu_route": "menu > item",
            "main_topic": "topic",
            "document_kind": "manual",
            "business_domain": "business",
            "image_summary": "summary\n\n\ncontinued",
            "ignored_field": "must not be included",
        }

        self.assertEqual(
            vision_description_text(description),
            "\n".join(
                [
                    "■ 要点",
                    "回答用本文: retrieval",
                    "主題: topic",
                    "",
                    "■ 可視情報",
                    "画面/メニュー: menu > item",
                    "画面名: screen-a",
                    "ボタン: run / cancel",
                    "項目: field-a",
                    "値: enabled",
                    "コード・エラー: E001",
                    "",
                    "■ 関係",
                    "操作: 1 open",
                    "条件と結果: when-a → then-b",
                    "注意: warning confirm",
                    "",
                    "■ 補足",
                    "周辺コンテキスト: B 【操作説明】の倉庫マスタ登録画面に属する",
                    "修正・補足: 赤枠付き context crop で実行ボタンの位置を補足",
                    "",
                    "■ 検索",
                    "検索語: keyword-a / keyword-b",
                    "言い換え: query-a / query-b",
                    "想定質問: question-a",
                ]
            ),
        )

    def test_formatter_accepts_customer_rag_description_wrapper_and_omits_empty_fields(self):
        payload = {
            "description": {
                "surrounding_context": "context only",
                "visible_buttons": [],
                "retrieval_text": "",
            }
        }

        self.assertEqual(vision_description_text(payload), "■ 補足\n周辺コンテキスト: context only")

    def test_formatter_omits_fields_that_no_downstream_step_reads(self):
        """look_at / image_summary / business_domain / document_kind は下流で未使用 (#768)。"""
        payload = {
            "retrieval_text": "本文",
            "look_at": [{"region": "上部", "description": "見るべき箇所"}],
            "image_summary": "概要",
            "business_domain": "software",
            "document_kind": "manual",
            "main_topic": "掛率登録",
        }

        self.assertEqual(vision_description_text(payload), "■ 要点\n回答用本文: 本文\n主題: 掛率登録")
        self.assertEqual(vision_description_text({"look_at": [{"region": "上部"}], "image_summary": "概要"}), "")

    def test_formatter_flattens_nested_lists_and_omits_empty_nested_values(self):
        payload = {
            "description": {
                "retrieval_text": [
                    {"topic": "出荷数量", "empty": ""},
                    {"steps": ["在庫振替", "返品伝票0件"]},
                    None,
                ],
                "condition_result_pairs": [
                    {"condition": "欠品が発生", "result": "取寄区分を選択"},
                ],
            }
        }

        self.assertEqual(
            vision_description_text(payload),
            "\n".join(
                [
                    "■ 要点",
                    "回答用本文: 出荷数量 / 在庫振替 / 返品伝票0件",
                    "",
                    "■ 関係",
                    "条件と結果: 欠品が発生 → 取寄区分を選択",
                ]
            ),
        )

    def test_formatter_includes_diagram_chart_table_and_form_structure(self):
        payload = {
            "visual_kind": "chart",
            "diagram_nodes": ["受付", "審査"],
            "diagram_edges": [{"source": "受付", "target": "審査", "label": "申請あり"}],
            "chart_title": "月別件数",
            "chart_axes": ["x=月", "y=件数"],
            "chart_series": [{"name": "申請", "values": ["4月=10"], "trend": "増加"}],
            "table_headers": ["区分", "件数"],
            "table_rows": [["A", "10"]],
            "form_fields": [{"name": "対象", "value": "A", "state": "selected"}],
            "form_layout": "入力セクション",
        }

        text = vision_description_text(payload)

        self.assertIn("■ 要点\n視覚種別: chart", text)
        self.assertIn("■ 構造\n図のノード: 受付 / 審査\n図の接続: 受付 → 審査（申請あり）", text)
        self.assertIn("グラフのタイトル: 月別件数\nグラフの軸: x=月 / y=件数\nグラフの系列: 申請: 4月=10（増加）", text)
        self.assertIn("表の見出し: 区分 | 件数\n表の行: 1行目: A | 10", text)
        self.assertIn("フォームの項目: 対象 = A [selected]\nフォームの配置: 入力セクション", text)

    def test_bounds_nested_visual_structure_arrays_before_persistence(self):
        description = _bounded_vision_description(
            {
                "diagram_nodes": [f"node-{index}" for index in range(MAX_DIAGRAM_NODES + 5)],
                "chart_series": [{"name": "requests", "values": list(range(MAX_CHART_SERIES_VALUES + 5)), "trend": "up"}],
                "table_rows": [list(range(MAX_TABLE_COLUMNS + 5)) for _ in range(MAX_TABLE_ROWS + 5)],
                "form_fields": [{"name": str(index)} for index in range(MAX_FORM_FIELDS + 5)],
            }
        )

        self.assertEqual(len(description["diagram_nodes"]), MAX_DIAGRAM_NODES)
        self.assertEqual(len(description["chart_series"][0]["values"]), MAX_CHART_SERIES_VALUES)
        self.assertEqual(len(description["table_rows"]), MAX_TABLE_ROWS)
        self.assertEqual(len(description["table_rows"][0]), MAX_TABLE_COLUMNS)
        self.assertEqual(len(description["form_fields"]), MAX_FORM_FIELDS)

    def test_persists_normalized_visual_kind_and_diagram_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            page_path = run_dir / "page.png"
            Image.new("RGB", (100, 100), "white").save(page_path)
            page = PageImage(page=1, width=100, height=100, pdf_width=100, pdf_height=100, image_path=str(page_path))
            record = _record("docling-p1-1", "docling", "Picture", 1, [10, 10, 80, 80])
            description = {
                "visual_kind": "flow chart",
                "diagram_title": "申請フロー",
                "diagram_nodes": ["受付", "完了"],
                "diagram_edges": [{"source": "受付", "target": "完了", "label": "承認"}],
                "diagram_summary": "受付から承認後に完了する。",
                "retrieval_text": "申請フローの説明",
            }

            with patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value=description):
                stats = describe_docling_pictures([record], [page], run_dir=run_dir, pdf_name="manual.pdf", settings=get_settings())

            self.assertEqual((stats.targets, stats.succeeded, stats.failed), (1, 1, 0))
            self.assertEqual(record.raw["picture_kind"], "flowchart")
            self.assertEqual(record.raw["visual_kind"], "flowchart")
            self.assertEqual(record.raw["visual_structure"]["diagram_title"], "申請フロー")
            self.assertEqual(record.raw["visual_structure"]["diagram_edges"][0]["label"], "承認")

    def test_processes_only_empty_docling_pictures_in_sequence_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            page_path = run_dir / "page.png"
            Image.new("RGB", (100, 100), "white").save(page_path)
            page = PageImage(page=1, width=100, height=100, pdf_width=100, pdf_height=100, image_path=str(page_path))
            later = _record("docling-p1-2", "docling", "Picture", 2, [40, 40, 60, 60])
            earlier = _record("docling-p1-1", "docling", "Picture", 1, [10, 10, 20, 20])
            other_engine = _record("archived-p1-1", "archived_parser", "Picture", 1, [1, 1, 9, 9])
            non_picture = _record("docling-p1-3", "docling", "Text", 3, [1, 1, 9, 9])
            existing = _record("docling-p1-4", "docling", "Picture", 4, [70, 70, 90, 90], text="existing")
            aggregate = _record(
                "docling-p1-5",
                "docling",
                "Picture",
                5,
                [10, 10, 20, 20],
                raw_type="picture_ocr_text",
            )
            calls = []

            def describe(image_path, metadata, settings, *, context_image_paths=(), rendered_prompt=None, system_prompt=None):
                self.assertEqual(system_prompt, VISION_SYSTEM_PROMPT)
                calls.append(
                    (
                        Path(image_path),
                        dict(metadata),
                        settings.vision_model,
                        tuple(Path(path) for path in context_image_paths),
                    )
                )
                return {"retrieval_text": f"description {metadata['record_id']}"}

            with patch("rag_engine.parsing.picture_descriptions.describe_picture", side_effect=describe):
                stats = describe_docling_pictures(
                    [later, other_engine, non_picture, existing, aggregate, earlier],
                    [page],
                    run_dir=run_dir,
                    pdf_name="manual.pdf",
                    # Vision の model は provider 設定が決める。コードに既定値が無いので env で与える (#1034)。
                    settings=get_settings(environ={"OCI_ENTERPRISE_AI_VLM_MODEL": "vendor.vision-1"}, dotenv_path=None),
                )

            self.assertEqual((stats.targets, stats.succeeded, stats.failed), (2, 2, 0))
            self.assertEqual([call[1]["record_id"] for call in calls], ["docling-p1-1", "docling-p1-2"])
            self.assertEqual([call[2] for call in calls], ["vendor.vision-1", "vendor.vision-1"])
            self.assertEqual([input["role"] for input in calls[0][1]["image_inputs"]], ["target_picture_crop", "page_context_crop"])
            self.assertEqual(calls[0][1]["input_strategy"], "target_crop_plus_context_crop")
            self.assertEqual(calls[0][1]["paired_ocr_text"], "")
            self.assertEqual(len(calls[0][3]), 1)
            self.assertEqual(calls[0][3][0].name, "docling-p1-1.context.png")
            self.assertEqual(earlier.text, "■ 要点\n回答用本文: description docling-p1-1")
            self.assertEqual(later.text, "■ 要点\n回答用本文: description docling-p1-2")
            self.assertEqual(existing.text, "existing")
            self.assertEqual(aggregate.text, "")
            self.assertEqual(earlier.raw["vision_description"]["retrieval_text"], "description docling-p1-1")
            self.assertEqual(earlier.raw["vision_model"], "vendor.vision-1")
            self.assertEqual(earlier.raw["vision_crop"], "docling/vision/docling-p1-1.png")
            self.assertEqual(earlier.raw["vision_context_crop"], "docling/vision/docling-p1-1.context.png")
            self.assertEqual(earlier.raw["vision_input_mode"], "target_crop_with_context")
            self.assertEqual([item["role"] for item in earlier.raw["vision_input_images"]], ["target_picture_crop", "page_context_crop"])
            self.assertEqual(record_to_legacy_jsonl(earlier)["sentence"], earlier.text)
            with Image.open(calls[0][0]) as crop:
                self.assertEqual(crop.size, (26, 26))
            with Image.open(calls[0][3][0]) as context:
                self.assertEqual(context.size, (100, 100))
                self.assertEqual(context.getpixel((10, 10)), VISION_TARGET_OUTLINE)

    def test_picture_metadata_includes_same_bbox_ocr_as_reading_aid(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            page_path = run_dir / "page.png"
            Image.new("RGB", (100, 100), "white").save(page_path)
            page = PageImage(page=1, width=100, height=100, pdf_width=100, pdf_height=100, image_path=str(page_path))
            picture = _record("docling-p1-1", "docling", "Picture", 1, [10, 10, 60, 60])
            ocr = _record(
                "docling-p1-2",
                "docling",
                "Picture",
                2,
                [10, 10, 60, 60],
                text="OCR抽出テキスト:\n得意先情報\n売上計上",
                raw_type="picture_ocr_text",
            )

            def describe(image_path, metadata, settings, *, context_image_paths=(), rendered_prompt=None, system_prompt=None):
                self.assertEqual(system_prompt, VISION_SYSTEM_PROMPT)
                self.assertIn("得意先情報", metadata["paired_ocr_text"])
                self.assertEqual(len(context_image_paths), 1)
                return {"retrieval_text": "with ocr"}

            with patch("rag_engine.parsing.picture_descriptions.describe_picture", side_effect=describe):
                stats = describe_docling_pictures(
                    [picture, ocr],
                    [page],
                    run_dir=run_dir,
                    pdf_name="manual.pdf",
                    settings=get_settings(),
                )

            self.assertEqual((stats.targets, stats.succeeded, stats.failed), (1, 1, 0))
            self.assertIn("回答用本文: with ocr", picture.text)

    def test_picture_metadata_includes_headings_neighbors_and_containing_table_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            page_path = run_dir / "page.png"
            Image.new("RGB", (200, 200), "white").save(page_path)
            page = PageImage(page=1, width=200, height=200, pdf_width=200, pdf_height=200, image_path=str(page_path))
            title = _record("docling-p1-1", "docling", "Title", 1, [10, 5, 150, 18], text="8 倉庫連携処理")
            section = _record(
                "docling-p1-2",
                "docling",
                "Section-header",
                2,
                [10, 24, 185, 38],
                text="（1）【在庫業務】 B-1. 締め年月の更新",
            )
            previous_text = _record(
                "docling-p1-3",
                "docling",
                "Text",
                3,
                [25, 56, 170, 66],
                text="矢印ボタンを押して締め年月を翌月に更新します。",
            )
            table = _record(
                "docling-p1-4",
                "docling",
                "Table",
                4,
                [20, 50, 180, 150],
                text="<table><tr><td>処理確認</td><td>説明</td></tr></table>",
                raw_type="table",
            )
            picture = _record("docling-p1-5", "docling", "Picture", 5, [35, 72, 75, 108])
            next_text = _record(
                "docling-p1-6",
                "docling",
                "Text",
                6,
                [85, 74, 175, 108],
                text="OK で選んだ処理を取り消す場合は担当SEまで連絡します。",
            )

            captured_metadata = {}

            def describe(image_path, metadata, settings, *, context_image_paths=(), rendered_prompt=None, system_prompt=None):
                self.assertEqual(system_prompt, VISION_SYSTEM_PROMPT)
                captured_metadata.update(metadata)
                return {"retrieval_text": "table contextual description"}

            with patch("rag_engine.parsing.picture_descriptions.describe_picture", side_effect=describe):
                stats = describe_docling_pictures(
                    [title, section, previous_text, table, picture, next_text],
                    [page],
                    run_dir=run_dir,
                    pdf_name="manual.pdf",
                    settings=get_settings(),
                )

            self.assertEqual((stats.targets, stats.succeeded, stats.failed), (1, 1, 0))
            self.assertEqual(
                [heading["record_id"] for heading in captured_metadata["nearby_headings"]],
                ["docling-p1-1", "docling-p1-2"],
            )
            self.assertEqual(captured_metadata["previous_record"]["record_id"], "docling-p1-4")
            self.assertEqual(captured_metadata["next_record"]["record_id"], "docling-p1-6")
            self.assertEqual(captured_metadata["containing_table"]["record_id"], "docling-p1-4")
            self.assertEqual(captured_metadata["containing_table"]["relationship"], "contains_target_picture")
            cell_hint = captured_metadata["containing_table"]["cell_hint"]
            self.assertEqual(cell_hint["strategy"], "table_bbox_position_and_nearby_text")
            self.assertEqual(cell_hint["target_position"]["horizontal_region"], "left")
            self.assertEqual(cell_hint["target_position"]["vertical_region"], "middle")
            self.assertIn(
                "docling-p1-6",
                [item["record_id"] for item in cell_hint["nearby_cell_text_records"]],
            )
            self.assertEqual(picture.raw["vision_nearby_headings"], captured_metadata["nearby_headings"])
            self.assertEqual(picture.raw["vision_containing_table"], captured_metadata["containing_table"])

    def test_skips_small_footer_logo_pictures_without_vlm_description(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            page_path = run_dir / "page.png"
            Image.new("RGB", (100, 100), "white").save(page_path)
            page = PageImage(page=1, width=100, height=100, pdf_width=100, pdf_height=100, image_path=str(page_path))
            footer_logo = _record("docling-p1-1", "docling", "Picture", 1, [45, 92, 55, 98])
            content_picture = _record("docling-p1-2", "docling", "Picture", 2, [10, 10, 60, 60])

            with patch(
                "rag_engine.parsing.picture_descriptions.describe_picture",
                return_value={"retrieval_text": "business screenshot"},
            ) as describe:
                stats = describe_docling_pictures(
                    [footer_logo, content_picture],
                    [page],
                    run_dir=run_dir,
                    pdf_name="manual.pdf",
                    settings=get_settings(),
                )

            self.assertEqual((stats.targets, stats.succeeded, stats.failed), (1, 1, 0))
            self.assertEqual(describe.call_count, 1)
            self.assertEqual(describe.call_args.args[1]["record_id"], "docling-p1-2")
            self.assertEqual(footer_logo.text, "")
            self.assertEqual(footer_logo.raw["visual_role"], "decorative")
            self.assertTrue(footer_logo.raw["vision_skipped"])
            self.assertTrue(footer_logo.raw["rag_excluded"])

    def test_excludes_large_generic_logo_but_preserves_diagnostic_crops(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            page_path = run_dir / "page.png"
            Image.new("RGB", (100, 100), "white").save(page_path)
            page = PageImage(page=1, width=100, height=100, pdf_width=100, pdf_height=100, image_path=str(page_path))
            logo = _record("docling-p1-1", "docling", "Picture", 1, [5, 5, 95, 95])

            with patch(
                "rag_engine.parsing.picture_descriptions.describe_picture",
                return_value={"visual_kind": "logo", "image_summary": "企業Logo", "retrieval_text": ""},
            ):
                stats = describe_docling_pictures(
                    [logo],
                    [page],
                    run_dir=run_dir,
                    pdf_name="manual.pdf",
                    settings=get_settings(),
                )

            self.assertEqual((stats.targets, stats.succeeded, stats.failed), (1, 1, 0))
            self.assertEqual(logo.text, "")
            self.assertEqual(logo.raw["visual_kind"], "logo")
            self.assertEqual(logo.raw["visual_role"], "decorative")
            self.assertTrue(logo.raw["rag_excluded"])
            self.assertTrue(logo.raw["vision_excluded_after_classification"])
            self.assertFalse(logo.raw["vision_skipped"])
            self.assertTrue((run_dir / logo.raw["crop_path"]).is_file())
            self.assertTrue((run_dir / logo.raw["vision_context_crop"]).is_file())
            from rag_engine.parsing.decorative_pictures import classify_picture_record, mark_picture_record_role

            mark_picture_record_role(logo, classify_picture_record(logo, [logo]))
            self.assertEqual(logo.raw["vision_status"], "succeeded")
            self.assertFalse(logo.raw["vision_skipped"])

    def test_uses_summary_fallback_and_continues_after_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            page_path = run_dir / "page.png"
            Image.new("RGB", (50, 50), "white").save(page_path)
            page = PageImage(page=1, width=50, height=50, pdf_width=50, pdf_height=50, image_path=str(page_path))
            failed = _record("docling-p1-1", "docling", "Picture", 1, [10, 20, 45, 45])
            succeeded = _record("docling-p1-2", "docling", "Picture", 2, [15, 25, 45, 48])

            with patch(
                "rag_engine.parsing.picture_descriptions.describe_picture",
                side_effect=[RuntimeError("temporary failure"), {"retrieval_text": "summary fallback"}],
            ):
                stats = describe_docling_pictures(
                    [failed, succeeded],
                    [page],
                    run_dir=run_dir,
                    pdf_name="manual.pdf",
                    settings=get_settings(),
                )

            self.assertEqual((stats.targets, stats.succeeded, stats.failed), (2, 1, 1))
            self.assertEqual(failed.text, "")
            self.assertIn("temporary failure", failed.raw["vision_error"])
            self.assertEqual(succeeded.text, "■ 要点\n回答用本文: summary fallback")

    def test_empty_supported_fields_preserve_raw_json_and_continue(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            page_path = run_dir / "page.png"
            Image.new("RGB", (50, 50), "white").save(page_path)
            page = PageImage(page=1, width=50, height=50, pdf_width=50, pdf_height=50, image_path=str(page_path))
            empty = _record("docling-p1-1", "docling", "Picture", 1, [10, 20, 45, 45])
            succeeded = _record("docling-p1-2", "docling", "Picture", 2, [15, 25, 45, 48])
            empty_response = {"answer": "unusable", "reason": "schema mismatch"}

            with patch(
                "rag_engine.parsing.picture_descriptions.describe_picture",
                side_effect=[empty_response, {"retrieval_text": "next picture"}],
            ):
                stats = describe_docling_pictures(
                    [empty, succeeded],
                    [page],
                    run_dir=run_dir,
                    pdf_name="manual.pdf",
                    settings=get_settings(),
                )

            self.assertEqual((stats.targets, stats.succeeded, stats.failed), (2, 1, 1))
            self.assertEqual(empty.text, "")
            self.assertEqual(empty.raw["vision_description"], empty_response)
            self.assertIn("説明フィールド", empty.raw["vision_error"])
            self.assertEqual(succeeded.text, "■ 要点\n回答用本文: next picture")

    def test_empty_bbox_is_recorded_as_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            page_path = run_dir / "page.png"
            Image.new("RGB", (20, 20), "white").save(page_path)
            page = PageImage(page=1, width=20, height=20, pdf_width=20, pdf_height=20, image_path=str(page_path))
            record = _record("docling-p1-1", "docling", "Picture", 1, [30, 30, 30, 30])

            with patch("rag_engine.parsing.picture_descriptions.describe_picture") as describe:
                stats = describe_docling_pictures([record], [page], run_dir=run_dir, pdf_name="manual.pdf", settings=get_settings())

            self.assertEqual((stats.targets, stats.succeeded, stats.failed), (1, 0, 1))
            self.assertFalse(describe.called)
            self.assertIn("bbox", record.raw["vision_error"])


def _record(
    record_id: str,
    engine: str,
    category: str,
    seq_no: int,
    bbox: list[float],
    *,
    text: str = "",
    raw_type: str | None = None,
) -> LayoutRecord:
    return LayoutRecord(
        id=record_id,
        engine=engine,
        page=1,
        seq_no=seq_no,
        bbox=bbox,
        coord_system="image_top_left",
        page_width=100,
        page_height=100,
        category=category,
        text=text,
        raw_type=raw_type or category.lower(),
        raw={},
    )


if __name__ == "__main__":
    unittest.main()
