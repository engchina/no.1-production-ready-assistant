"""docling adapter の挙動を保護するテスト。"""

import importlib.util
import os
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from rag_engine.adapters.parsers.base import AnalysisContext
from rag_engine.adapters.parsers.docling_adapter import (
    DoclingAdapter,
    _build_docling_converter,
    _docling_artifacts_path,
    _item_text,
    _table_data_to_html,
    _table_html_by_ref,
    docling_table_cells,
)
from rag_engine.config import get_settings
from rag_engine.models.layout import PageImage


class DoclingAdapterTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("docling"), "docling 未導入")
    def test_builds_cpu_converter(self):
        settings = replace(get_settings(), docling_device="cpu", docling_num_threads=2)

        converter = _build_docling_converter(settings)

        self.assertIsNotNone(converter)

    def test_uses_downloaded_models_as_artifacts_path(self):
        """取得済みのモデルがあれば HuggingFace Hub へ問い合わせずにそれを使う(#829)。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DOCLING_ARTIFACTS_PATH", None)
            cache_dir = Path(tmp)
            # 未取得(models が無い・空)は None: 今までどおり初回の解析で取得する。
            self.assertIsNone(_docling_artifacts_path(cache_dir))
            (cache_dir / "models").mkdir()
            self.assertIsNone(_docling_artifacts_path(cache_dir))
            (cache_dir / "models" / "docling-project--docling-layout-heron").mkdir()
            self.assertEqual(_docling_artifacts_path(cache_dir), cache_dir / "models")

    def test_explicit_artifacts_path_env_is_left_to_docling(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp)
            (cache_dir / "models" / "x").mkdir(parents=True)
            with patch.dict(os.environ, {"DOCLING_ARTIFACTS_PATH": "/opt/docling-models"}):
                self.assertIsNone(_docling_artifacts_path(cache_dir))

    def test_reads_picture_child_text_debug_setting(self):
        with patch.dict(os.environ, {"DOCLING_KEEP_PICTURE_CHILD_TEXT": "true"}):
            self.assertTrue(get_settings().docling_keep_picture_child_text)

    def test_orders_mixed_types_by_bbox_and_renumbers_each_page(self):
        settings = get_settings()
        context = AnalysisContext(
            pdf_path=Path("source.pdf"),
            run_dir=Path("."),
            pages=[
                PageImage(page=1, width=1000, height=1000, pdf_width=1000, pdf_height=1000, image_path="page-1.png"),
                PageImage(page=2, width=1000, height=1000, pdf_width=1000, pdf_height=1000, image_path="page-2.png"),
            ],
            settings=settings,
        )
        payload = {
            "texts": [
                _item("page 1 footer", "page_footer", 1, [400, 900, 500, 930]),
                _item("page 2 bottom", "text", 2, [20, 300, 200, 330]),
                _item("page 1 top", "text", 1, [20, 100, 200, 130]),
                _item("page 1 heading", "section_header", 1, [10, 300, 180, 340]),
                _item("page 2 top", "text", 2, [20, 100, 200, 130]),
            ],
            "tables": [_item("", "table", 1, [20, 500, 900, 650])],
            # top が見出しより 5 px 上でも、同じ視覚行では left の小さい見出しを先にする。
            "pictures": [
                _item("", "picture", 1, [250, 295, 300, 345]),
                _item("", "picture", 1, [20, 700, 900, 850]),
            ],
        }

        records = DoclingAdapter(settings)._records_from_payload(payload, context)

        self.assertEqual(
            [(record.page, record.seq_no, record.raw_type, record.text) for record in records],
            [
                (1, 1, "text", "page 1 top"),
                (1, 2, "section_header", "page 1 heading"),
                (1, 3, "picture", ""),
                (1, 4, "table", ""),
                (1, 5, "picture", ""),
                (1, 6, "page_footer", "page 1 footer"),
                (2, 1, "text", "page 2 top"),
                (2, 2, "text", "page 2 bottom"),
            ],
        )
        self.assertEqual(
            [record.id for record in records],
            [
                "docling-p1-1",
                "docling-p1-2",
                "docling-p1-3",
                "docling-p1-4",
                "docling-p1-5",
                "docling-p1-6",
                "docling-p2-1",
                "docling-p2-2",
            ],
        )

    def test_uses_source_order_as_final_bbox_tie_breaker(self):
        settings = get_settings()
        context = AnalysisContext(
            pdf_path=Path("source.pdf"),
            run_dir=Path("."),
            pages=[PageImage(page=1, width=100, height=100, pdf_width=100, pdf_height=100, image_path="page.png")],
            settings=settings,
        )
        payload = {
            "texts": [
                _item("first", "text", 1, [10, 10, 20, 20]),
                _item("second", "text", 1, [10, 10, 20, 20]),
            ]
        }

        records = DoclingAdapter(settings)._records_from_payload(payload, context)

        self.assertEqual([record.text for record in records], ["first", "second"])

    def test_picture_child_caption_is_kept_as_document_text(self):
        # 画面経路の行は図の caption として図の子になるが、図内の文字ではないので通常の record に残す (#626)。
        settings = replace(get_settings(), docling_keep_picture_child_text=False)
        payload = {
            "texts": [
                {**_linked_item("[ 画面：マスタ管理⇒掛率登録 ]", "#/texts/0", "#/pictures/0", [20, 5, 80, 9]), "label": "caption"},
                _linked_item("inner ocr text", "#/texts/1", "#/pictures/0", [20, 30, 40, 35]),
            ],
            "pictures": [
                {
                    **_linked_item("", "#/pictures/0", "#/body", [10, 10, 90, 90]),
                    "label": "picture",
                    "children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/1"}],
                }
            ],
        }

        records = DoclingAdapter(settings)._records_from_payload(payload, _context())

        caption = next(record for record in records if record.category == "Caption")
        self.assertEqual(caption.text, "[ 画面：マスタ管理⇒掛率登録 ]")
        aggregate = next(record for record in records if record.raw_type == "picture_ocr_text")
        self.assertNotIn("画面：", aggregate.text)  # OCR 集約には入れない
        self.assertNotIn(
            "inner ocr text", [record.text for record in records if record.category != "Picture" and record.raw_type != "picture_ocr_text"]
        )

    def test_numbered_instruction_callouts_inside_a_picture_are_kept_as_text(self):
        # 画面キャプチャの上の吹き出し手順は図内の文字ではない。行が分かれていても 1 件の Text にまとめ、
        # UI ラベルだけを OCR 集約に残す (#646)。
        settings = replace(get_settings(), docling_keep_picture_child_text=False)
        payload = {
            "texts": [
                _linked_item("①追加するグループのコードと 名", "#/texts/0", "#/pictures/0", [20, 5, 60, 9]),
                _linked_item("前を入力します。", "#/texts/1", "#/pictures/0", [20, 10, 60, 14]),
                _linked_item("管理者", "#/texts/2", "#/pictures/0", [20, 30, 40, 34]),
                _linked_item("②実行ボタンをクリックし ます。", "#/texts/3", "#/pictures/0", [20, 40, 60, 44]),
                _linked_item("③", "#/texts/4", "#/pictures/0", [20, 50, 24, 54]),
                _linked_item("サンプルシステム Ver 1.0 販売管理システム", "#/texts/5", "#/pictures/0", [20, 60, 80, 64]),
            ],
            "pictures": [
                {
                    **_linked_item("", "#/pictures/0", "#/body", [10, 0, 90, 90]),
                    "label": "picture",
                    "children": [{"$ref": f"#/texts/{n}"} for n in range(6)],
                }
            ],
        }

        records = DoclingAdapter(settings)._records_from_payload(payload, _context())

        texts = [record.text for record in records if record.category == "Text"]
        self.assertEqual(texts, ["①追加するグループのコードと 名前を入力します。", "②実行ボタンをクリックし ます。"])
        aggregate = next(record for record in records if record.raw_type == "picture_ocr_text")
        self.assertIn("管理者", aggregate.text)
        self.assertIn("③", aggregate.text)  # 文末に届かない番号だけの行は手順にしない
        self.assertNotIn("入力します", aggregate.text)

    def test_short_labels_inside_a_picture_bbox_are_treated_as_picture_text(self):
        # 図の子でなくても、図の bbox 内の短いラベルは画面の文字。本文の根拠にせず OCR 集約に入れる (#655)。
        settings = replace(get_settings(), docling_keep_picture_child_text=False)
        payload = {
            "texts": [
                _linked_item("管理者", "#/texts/0", "#/body", [30, 30, 40, 34]),
                _linked_item("55555", "#/texts/1", "#/body", [30, 40, 40, 44]),
                _linked_item("グループを新しく追加します。", "#/texts/2", "#/body", [30, 50, 80, 54]),
                _linked_item("パート社員", "#/texts/3", "#/body", [5, 95, 15, 99]),
            ],
            "pictures": [
                {
                    **_linked_item("", "#/pictures/0", "#/body", [20, 20, 90, 90]),
                    "label": "picture",
                    "children": [],
                }
            ],
        }

        records = DoclingAdapter(settings)._records_from_payload(payload, _context())

        texts = [record.text for record in records if record.category == "Text"]
        self.assertEqual(texts, ["グループを新しく追加します。", "パート社員"])  # 文は残す。図の外のラベルも残す
        aggregate = next(record for record in records if record.raw_type == "picture_ocr_text")
        self.assertIn("管理者", aggregate.text)
        self.assertIn("55555", aggregate.text)

    def test_aggregates_picture_children_and_hides_child_text_by_default(self):
        settings = replace(get_settings(), docling_keep_picture_child_text=False)
        payload = {
            "texts": [
                # 図の bbox 内でも文（句点で終わる）は本文に残す。短いラベルだけが図の文字になる (#655)。
                _linked_item("本文の説明です。", "#/texts/0", "#/body", [20, 20, 40, 25]),
                # children の順序が座標順と逆でも、Docling の明示順を優先する。
                _linked_item("child first", "#/texts/1", "#/pictures/0", [20, 70, 40, 75]),
                _linked_item("child second", "#/texts/2", "#/pictures/0", [20, 60, 40, 65]),
                # 同じ視覚行では、わずかな top 差より left を優先する。
                _linked_item("fallback right", "#/texts/3", "#/pictures/0", [50, 40, 70, 45]),
                _linked_item("fallback left", "#/texts/4", "#/pictures/0", [20, 40.5, 40, 45.5]),
                _linked_item("   ", "#/texts/5", "#/pictures/0", [20, 80, 40, 85]),
            ],
            "pictures": [
                {
                    **_linked_item("must stay empty", "#/pictures/0", "#/body", [10, 10, 90, 90]),
                    "label": "picture",
                    "children": [
                        {"$ref": "#/texts/1"},
                        {"$ref": "#/texts/2"},
                        {"$ref": "#/texts/5"},
                    ],
                }
            ],
        }

        records = DoclingAdapter(settings)._records_from_payload(payload, _context())

        self.assertEqual([record.raw_type for record in records], ["picture", "picture_ocr_text", "text"])
        original, aggregate, ordinary = records
        self.assertEqual(original.text, "")
        self.assertEqual(original.bbox, aggregate.bbox)
        self.assertEqual(
            aggregate.text,
            "OCR抽出テキスト:\nchild first\nchild second\nfallback left\nfallback right",
        )
        self.assertEqual(aggregate.category, "Picture")
        self.assertEqual(aggregate.raw["aggregation"], "docling_picture_children")
        self.assertEqual(aggregate.raw["source_picture_ref"], "#/pictures/0")
        self.assertEqual(
            aggregate.raw["child_refs"],
            ["#/texts/1", "#/texts/2", "#/texts/4", "#/texts/3"],
        )
        self.assertEqual(ordinary.text, "本文の説明です。")

    def test_can_keep_individual_picture_child_text_for_debugging(self):
        settings = replace(get_settings(), docling_keep_picture_child_text=True)
        payload = {
            "texts": [_linked_item("child", "#/texts/0", "#/pictures/0", [20, 20, 40, 30])],
            "pictures": [
                {
                    **_linked_item("", "#/pictures/0", "#/body", [10, 10, 90, 90]),
                    "label": "picture",
                    "children": [{"$ref": "#/texts/0"}],
                }
            ],
        }

        records = DoclingAdapter(settings)._records_from_payload(payload, _context())

        self.assertEqual(
            [(record.raw_type, record.text) for record in records],
            [("picture", ""), ("picture_ocr_text", "OCR抽出テキスト:\nchild"), ("text", "child")],
        )

    def test_separates_picture_aggregates_and_skips_empty_aggregate(self):
        settings = replace(get_settings(), docling_keep_picture_child_text=False)
        payload = {
            "texts": [
                _linked_item("picture one", "#/texts/0", "#/pictures/0", [15, 15, 35, 20]),
                _linked_item("picture two", "#/texts/1", "#/pictures/1", [60, 15, 80, 20]),
                _linked_item("", "#/texts/2", "#/pictures/2", [15, 60, 35, 65]),
                # bbox は Picture 内でも、文（句点で終わる）は階層どおり body の本文として残す (#655)。
                _linked_item("関係ない説明です。", "#/texts/3", "#/body", [20, 20, 30, 25]),
            ],
            "pictures": [
                _picture("#/pictures/0", [10, 10, 40, 30], ["#/texts/0"]),
                _picture("#/pictures/1", [50, 10, 90, 30], ["#/texts/1"]),
                _picture("#/pictures/2", [10, 50, 40, 80], ["#/texts/2"]),
            ],
        }

        records = DoclingAdapter(settings)._records_from_payload(payload, _context())

        aggregates = [record for record in records if record.raw_type == "picture_ocr_text"]
        self.assertEqual([record.text for record in aggregates], ["OCR抽出テキスト:\npicture one", "OCR抽出テキスト:\npicture two"])
        self.assertEqual([record.raw["source_picture_ref"] for record in aggregates], ["#/pictures/0", "#/pictures/1"])
        self.assertEqual([record.text for record in records if record.raw_type == "text"], ["関係ない説明です。"])
        self.assertEqual(sum(record.raw_type == "picture" for record in records), 3)

    def test_uses_native_table_html_by_self_ref(self):
        settings = get_settings()
        context = _context()
        payload = {
            "tables": [
                {
                    **_item("", "table", 1, [10, 10, 90, 90]),
                    "self_ref": "#/tables/0",
                    "data": {"table_cells": []},
                }
            ]
        }

        records = DoclingAdapter(settings)._records_from_payload(
            payload,
            context,
            table_html_by_ref={"#/tables/0": "<table><tbody><tr><td>native</td></tr></tbody></table>"},
        )

        self.assertEqual(records[0].text, "<table><tbody><tr><td>native</td></tr></tbody></table>")

    def test_table_text_that_no_cell_received_is_kept_as_a_supplement_record(self):
        """グリフの bbox が不正な PDF では Docling が文字をセルへ入れ損ねる。text layer の原文で欠けた行だけを補う (#564)。"""
        from unittest.mock import patch

        table = {
            **_item("", "table", 1, [10, 10, 90, 90]),
            "self_ref": "#/tables/0",
            "data": {
                "grid": [
                    [{"text": "制度開始時期"}, {"text": "4 月：取引開始 5"}],  # 下の行の「5」が混入
                    [{"text": "優遇期間"}, {"text": "投資した年から最長 年間"}],
                    [{"text": "運用管理"}, {"text": "1 3 31 18 1 1"}],  # 日本語が欠落
                    [{"text": "手数料"}, {"text": "1回目は無料\n2回目以降は有料"}],
                ]
            },
        }
        payload = {"tables": [table], "texts": [_body_text(0, "（注）表の下の脚注です。", [10, 92, 90, 98])]}
        native_lines = [
            "制度開始時期 4 月：取引開始",
            "優遇期間 投資した年から最長 5 年間",
            "運用管理 ・原則として、親権者等が代理して運用を行う",
            "手数料 1回目は無料",
            "2回目以降は有料",
            "（注）表の下の脚注です。",
        ]
        html = {"#/tables/0": "<table><tbody><tr><td>x</td></tr></tbody></table>"}
        with patch("rag_engine.adapters.parsers.docling_adapter.pdf_text_lines_in_bbox", return_value=native_lines) as lines:
            records = DoclingAdapter(get_settings())._records_from_payload(payload, _context(), table_html_by_ref=html)

        self.assertEqual(lines.call_args.args[:2], (Path("source.pdf"), 1))
        self.assertEqual([r.raw_type for r in records], ["table", "table_unassigned_text", "text"])
        self.assertEqual(records[0].text, html["#/tables/0"])  # 表の HTML は変えない
        supplement = records[1]
        self.assertEqual(
            (supplement.category, supplement.bbox, supplement.raw["source_table_ref"]), ("Text", records[0].bbox, "#/tables/0")
        )
        # 正しくセルに入っている行、複数行のセルの各行、別 record になっている脚注は補足に含めない。
        self.assertEqual(
            supplement.text.splitlines(),
            [
                "表の補足（セルに割り当てられなかった原文）:",
                "優遇期間 投資した年から最長 5 年間",
                "運用管理 ・原則として、親権者等が代理して運用を行う",
            ],
        )

    def test_table_cell_text_moved_to_the_neighbor_row_is_repaired_before_building_html(self):
        """隣の行へ入ったセルの文字は text layer で戻し、HTML を作り直して修正を raw に記録する (#597)。"""
        from unittest.mock import patch

        def cell(row, col, text, **extra):
            return {**_cell(row, col, text, **extra), "bbox": {}}

        cells = [
            cell(0, 0, "連携データ", column_header=True),
            cell(0, 1, "連携の向き", column_header=True),
            cell(1, 0, "取引先情報"),
            cell(1, 1, "Input"),
            cell(2, 0, "倉庫入出庫情報"),
            cell(2, 1, "Input Input"),
            cell(3, 0, "取引先与信情報"),
            cell(3, 1, ""),
            cell(4, 0, "取引先契約資格"),
            cell(4, 1, "Input"),
        ]
        grid = [[dict(c) for c in cells if c["start_row_offset_idx"] == row] for row in range(5)]
        table = {
            **_item("", "table", 1, [10, 10, 90, 90]),
            "self_ref": "#/tables/0",
            "data": {"num_rows": 5, "num_cols": 2, "table_cells": cells, "grid": grid},
        }
        lines = ["連携データ 連携の向き", "取引先情報 Input", "倉庫入出庫情報 Input", "取引先与信情報 Input", "取引先契約資格 Input"]
        native = {"#/tables/0": "<table><tbody><tr><td>native</td></tr></tbody></table>"}
        with patch("rag_engine.adapters.parsers.docling_adapter.pdf_text_lines_in_bbox", return_value=lines):
            records = DoclingAdapter(get_settings())._records_from_payload({"tables": [table]}, _context(), table_html_by_ref=native)

        # 修正した表は Docling の HTML ではなく dict から作り直し、未割当の補足は出ない。
        self.assertEqual([r.raw_type for r in records], ["table"])
        self.assertIn("<tr><td>倉庫入出庫情報</td><td>Input</td></tr><tr><td>取引先与信情報</td><td>Input</td></tr>", records[0].text)
        self.assertEqual(
            records[0].raw["table_cell_repairs"],
            [{"row": 2, "column": 1, "before": "Input Input", "after": "Input"}, {"row": 3, "column": 1, "before": "", "after": "Input"}],
        )
        self.assertEqual([c["text"] for c in cells if c["start_col_offset_idx"] == 1], ["連携の向き", "Input", "Input", "Input", "Input"])

    def test_table_supplement_is_skipped_when_cells_are_complete_or_the_text_layer_is_unavailable(self):
        from unittest.mock import patch

        table = {
            **_item("", "table", 1, [10, 10, 90, 90]),
            "self_ref": "#/tables/0",
            "data": {"grid": [[{"text": "項目"}, {"text": "内容"}], [{"text": "対象投資額"}, {"text": "毎年 80 万円"}]]},
        }
        html = {"#/tables/0": "<table><tbody><tr><td>x</td></tr></tbody></table>"}
        adapter = DoclingAdapter(get_settings())
        for lines in (["項目 内容", "対象投資額 毎年 80 万円"], []):
            with patch("rag_engine.adapters.parsers.docling_adapter.pdf_text_lines_in_bbox", return_value=lines):
                self.assertEqual(
                    [r.raw_type for r in adapter._records_from_payload({"tables": [table]}, _context(), table_html_by_ref=html)], ["table"]
                )
        # text layer の取得に失敗しても解析は止めない。
        with patch("rag_engine.adapters.parsers.docling_adapter.pdf_text_lines_in_bbox", side_effect=RuntimeError("no text layer")):
            self.assertEqual(len(adapter._records_from_payload({"tables": [table]}, _context(), table_html_by_ref=html)), 1)

    def test_collects_native_table_html_without_caption(self):
        table = _NativeTable("#/tables/0", "<table><tr><td>native</td></tr></table>")
        document = _Document([table])

        table_html = _table_html_by_ref(document)

        self.assertEqual(table_html, {"#/tables/0": "<table><tr><td>native</td></tr></table>"})
        self.assertEqual(table.call, (document, False))

    def test_native_table_export_failure_is_ignored(self):
        document = _Document([_NativeTable("#/tables/0", error=RuntimeError("boom"))])

        self.assertEqual(_table_html_by_ref(document), {})

    def test_builds_fallback_html_with_headers_spans_and_escaped_text(self):
        html = _table_data_to_html(
            {
                "num_rows": 3,
                "num_cols": 3,
                "table_cells": [
                    _cell(0, 0, "見出し <A>\n次", column_header=True, col_span=2),
                    _cell(0, 2, "B & C", column_header=True),
                    _cell(1, 0, '縦 "結合"', row_header=True, row_span=2),
                    _cell(1, 1, "値1"),
                    _cell(1, 2, "値2"),
                    _cell(2, 1, "値3"),
                    _cell(2, 2, ""),
                ],
            }
        )

        self.assertEqual(
            html,
            '<table><tbody><tr><th colspan="2">見出し &lt;A&gt;<br>次</th><th>B &amp; C</th></tr>'
            '<tr><th rowspan="2">縦 &quot;結合&quot;</th><td>値1</td><td>値2</td></tr>'
            "<tr><td>値3</td><td></td></tr></tbody></table>",
        )

    def test_falls_back_to_table_cells_and_tolerates_missing_data(self):
        settings = get_settings()
        context = _context()
        payload = {
            "tables": [
                {
                    **_item("", "table", 1, [10, 10, 90, 40]),
                    "self_ref": "#/tables/0",
                    "data": {"num_rows": 1, "num_cols": 2, "table_cells": [_cell(0, 0, "左"), _cell(0, 1, "右")]},
                },
                {
                    **_item("", "table", 1, [10, 50, 90, 90]),
                    "self_ref": "#/tables/1",
                    "data": {},
                },
            ]
        }

        records = DoclingAdapter(settings)._records_from_payload(payload, context)

        self.assertEqual(records[0].text, "<table><tbody><tr><td>左</td><td>右</td></tr></tbody></table>")
        self.assertEqual(records[1].text, "")

    def test_document_index_table_keeps_table_category_and_html(self):
        payload = {
            "tables": [
                {
                    **_item("", "document_index", 1, [10, 10, 90, 40]),
                    "self_ref": "#/tables/0",
                    "data": {"num_rows": 1, "num_cols": 2, "table_cells": [_cell(0, 0, "第1章 概要"), _cell(0, 1, "3")]},
                }
            ]
        }

        records = DoclingAdapter(get_settings())._records_from_payload(payload, _context())

        self.assertEqual(records[0].category, "Table")
        self.assertEqual(records[0].raw_type, "document_index")
        self.assertEqual(records[0].text, "<table><tbody><tr><td>第1章 概要</td><td>3</td></tr></tbody></table>")

    def test_multi_prov_text_is_split_by_charspan(self):
        item = _item("前半の段落後半の段落", "text", 1, [10, 10, 90, 20])
        item["prov"][0]["charspan"] = [0, 5]
        item["prov"].append({**_item("", "text", 1, [10, 30, 90, 40])["prov"][0], "charspan": [5, 10]})

        records = DoclingAdapter(get_settings())._records_from_payload({"texts": [item]}, _context())

        self.assertEqual([record.text for record in records], ["前半の段落", "後半の段落"])

    def test_multi_prov_without_valid_charspan_keeps_text_once(self):
        text_item = _item("段落", "text", 1, [10, 10, 90, 20])
        text_item["prov"].append(_item("", "text", 1, [10, 30, 90, 40])["prov"][0])
        table = {
            **_item("", "table", 1, [10, 50, 90, 60]),
            "self_ref": "#/tables/0",
            "data": {"num_rows": 1, "num_cols": 1, "table_cells": [_cell(0, 0, "値")]},
        }
        table["prov"].append(_item("", "table", 1, [10, 70, 90, 80])["prov"][0])

        records = DoclingAdapter(get_settings())._records_from_payload({"texts": [text_item], "tables": [table]}, _context())

        self.assertEqual(
            [record.text for record in records],
            ["段落", "", "<table><tbody><tr><td>値</td></tr></tbody></table>", ""],
        )

    def test_multi_prov_picture_on_same_page_aggregates_ocr_once(self):
        picture = _picture("#/pictures/0", [10, 10, 90, 40], ["#/texts/0"])
        picture["prov"].append(_item("", "picture", 1, [10, 50, 90, 80])["prov"][0])
        payload = {
            "texts": [_linked_item("図内の文字", "#/texts/0", "#/pictures/0", [20, 20, 80, 30])],
            "pictures": [picture],
        }

        records = DoclingAdapter(get_settings())._records_from_payload(payload, _context())

        self.assertEqual(sum(record.raw_type == "picture_ocr_text" for record in records), 1)
        self.assertEqual(sum(record.raw_type == "picture" for record in records), 2)

    def test_multi_column_page_follows_docling_reading_order(self):
        # 左段 → 右段の読み順。座標順（上端 → 左端）では左右の段が交互に並ぶ。
        texts = [
            _body_text(0, "左段1", [5, 10, 45, 20]),
            _body_text(1, "左段2", [5, 60, 45, 70]),
            _body_text(2, "右段1", [55, 10, 95, 20]),
            _body_text(3, "右段2", [55, 60, 95, 70]),
        ]
        table = {**_item("", "table", 1, [55, 30, 95, 50]), "self_ref": "#/tables/0", "data": {}}
        payload = {
            "body": {"children": [{"$ref": ref} for ref in ("#/texts/0", "#/texts/1", "#/texts/2", "#/tables/0", "#/texts/3")]},
            "texts": texts,
            "tables": [table],
        }

        records = DoclingAdapter(get_settings())._records_from_payload(payload, _context())

        self.assertEqual(
            [(record.seq_no, record.raw_type, record.text) for record in records],
            [(1, "text", "左段1"), (2, "text", "左段2"), (3, "text", "右段1"), (4, "table", ""), (5, "text", "右段2")],
        )
        self.assertEqual([record.id for record in records], [f"docling-p1-{n}" for n in range(1, 6)])

    def test_single_column_page_keeps_visual_order_despite_list_grouping(self):
        # body ツリーはリスト項目を group にまとめるため、ツリー順では間の本文が後ろへ回る。
        texts = [
            {**_body_text(0, "（1）概要", [5, 10, 95, 20]), "label": "list_item", "parent": {"$ref": "#/groups/0"}},
            _body_text(1, "商号：サンプル銀行", [10, 30, 95, 40]),
            {**_body_text(2, "（2）業務", [5, 50, 95, 60]), "label": "list_item", "parent": {"$ref": "#/groups/0"}},
        ]
        payload = {
            "body": {"children": [{"$ref": "#/groups/0"}, {"$ref": "#/texts/1"}]},
            "groups": [{"self_ref": "#/groups/0", "children": [{"$ref": "#/texts/0"}, {"$ref": "#/texts/2"}]}],
            "texts": texts,
        }

        records = DoclingAdapter(get_settings())._records_from_payload(payload, _context())

        self.assertEqual([record.text for record in records], ["（1）概要", "商号：サンプル銀行", "（2）業務"])

    def test_narrow_fragments_do_not_mark_page_as_multi_column(self):
        # 表の周辺で出る断片テキストは「前より上かつ右」へ頻繁に飛ぶが、段組ではない。
        texts = [
            _body_text(0, "本文", [5, 10, 95, 20]),
            _body_text(1, "（＊", [5, 60, 8, 65]),
            _body_text(2, "4", [20, 40, 22, 45]),
        ]
        payload = {"body": {"children": [{"$ref": f"#/texts/{n}"} for n in (0, 1, 2)]}, "texts": texts}

        records = DoclingAdapter(get_settings())._records_from_payload(payload, _context())

        self.assertEqual([record.text for record in records], ["本文", "4", "（＊"])

    def test_picture_grandchild_text_is_hidden_and_aggregated(self):
        # 図内の箇条書きは group を挟んだ孫になる。
        grandchild = _linked_item("図内の箇条書き", "#/texts/0", "#/groups/0", [20, 20, 80, 30])
        payload = {
            "texts": [grandchild],
            "groups": [{"self_ref": "#/groups/0", "parent": {"$ref": "#/pictures/0"}, "children": [{"$ref": "#/texts/0"}]}],
            "pictures": [_picture("#/pictures/0", [10, 10, 90, 90], [])],
        }

        records = DoclingAdapter(replace(get_settings(), docling_keep_picture_child_text=False))._records_from_payload(payload, _context())

        self.assertEqual([record.raw_type for record in records], ["picture", "picture_ocr_text"])
        self.assertIn("図内の箇条書き", records[1].text)

    def test_one_page_table_keeps_cell_regions_and_grid_cells(self):
        """1 頁の表はセルの領域を record と同じ画像の座標で残し、格子のセル（結合は左上）にできる (#3429)。"""

        def cell(row, col, text, box, **extra):
            left, top, right, bottom = box
            return {**_cell(row, col, text, **extra), "bbox": {"l": left, "t": top, "r": right, "b": bottom, "coord_origin": "TOPLEFT"}}

        cells = [
            cell(0, 0, "Item", [10, 10, 30, 20], column_header=True, row_span=2),
            cell(0, 1, "H1", [30, 10, 90, 15], column_header=True, col_span=2),
            cell(1, 1, "Plan", [30, 15, 60, 20], column_header=True),
            cell(1, 2, "Actual", [60, 15, 90, 20], column_header=True),
            cell(2, 0, "Alpha", [10, 20, 30, 30], row_header=True),
            cell(2, 1, "10", [30, 20, 60, 30]),
            cell(2, 2, "11", [60, 20, 90, 30]),
        ]
        table = {
            **_item("", "table", 1, [10, 10, 90, 30]),
            "self_ref": "#/tables/0",
            "data": {"num_rows": 3, "num_cols": 3, "table_cells": cells},
        }
        context = AnalysisContext(
            pdf_path=Path("source.pdf"),
            run_dir=Path("."),
            pages=[PageImage(page=1, width=200, height=200, pdf_width=100, pdf_height=100, image_path="page.png")],
            settings=get_settings(),
        )

        records = DoclingAdapter(get_settings())._records_from_payload({"tables": [table]}, context)

        boxes = records[0].raw["table_cell_bboxes"]
        self.assertEqual(boxes[0], {"row": 0, "col": 0, "bbox": [20.0, 20.0, 60.0, 40.0]})
        self.assertEqual(len(boxes), len(cells))
        grid = docling_table_cells(records[0].raw)
        assert grid is not None
        self.assertEqual(
            [(c.row, c.col, c.row_span, c.col_span, c.text, c.metadata) for c in grid[:3]],
            [
                (0, 0, 2, 1, "Item", {"column_header": True}),
                (0, 1, 1, 2, "H1", {"column_header": True}),
                (1, 1, 1, 1, "Plan", {"column_header": True}),
            ],
        )
        self.assertEqual(grid[0].bbox, [20.0, 20.0, 60.0, 40.0])
        self.assertEqual(grid[4].metadata, {"row_header": True})

    def test_multi_page_table_has_no_cell_regions_and_bad_cells_are_not_a_grid(self):
        """頁をまたぐ表はセルの領域を作らない。位置の無い・同じ位置のセルは格子にしない (#3429)。"""
        table = {
            **_item("", "table", 1, [10, 10, 90, 90]),
            "self_ref": "#/tables/0",
            "data": {"table_cells": [{**_cell(0, 0, "A"), "bbox": {"l": 1, "t": 1, "r": 5, "b": 5, "coord_origin": "TOPLEFT"}}]},
        }
        table["prov"] = [*table["prov"], {**table["prov"][0], "page_no": 2}]
        context = replace(
            _context(),
            pages=[PageImage(page=page, width=100, height=100, pdf_width=100, pdf_height=100, image_path="page.png") for page in (1, 2)],
        )

        records = DoclingAdapter(get_settings())._records_from_payload({"tables": [table]}, context)

        self.assertNotIn("table_cell_bboxes", records[0].raw)
        self.assertEqual(docling_table_cells({"data": {"table_cells": [_cell(0, 0, "A"), _cell(0, 0, "B")]}}), None)
        self.assertEqual(docling_table_cells({"data": {"table_cells": [{"text": "A"}]}}), None)
        self.assertEqual(docling_table_cells({"data": {"table_cells": []}}), None)


def _item(text: str, label: str, page: int, bbox: list[int]) -> dict:
    left, top, right, bottom = bbox
    return {
        "text": text,
        "label": label,
        "prov": [
            {
                "page_no": page,
                "bbox": {"l": left, "t": top, "r": right, "b": bottom, "coord_origin": "TOPLEFT"},
            }
        ],
    }


def _body_text(index: int, text: str, bbox: list[int]) -> dict:
    return {**_item(text, "text", 1, bbox), "self_ref": f"#/texts/{index}", "parent": {"$ref": "#/body"}}


def _linked_item(text: str, self_ref: str, parent_ref: str, bbox: list[int]) -> dict:
    return {
        **_item(text, "text", 1, bbox),
        "self_ref": self_ref,
        "parent": {"$ref": parent_ref},
        "children": [],
    }


def _picture(self_ref: str, bbox: list[int], child_refs: list[str]) -> dict:
    return {
        **_linked_item("", self_ref, "#/body", bbox),
        "label": "picture",
        "children": [{"$ref": child_ref} for child_ref in child_refs],
    }


def _context() -> AnalysisContext:
    return AnalysisContext(
        pdf_path=Path("source.pdf"),
        run_dir=Path("."),
        pages=[PageImage(page=1, width=100, height=100, pdf_width=100, pdf_height=100, image_path="page.png")],
        settings=get_settings(),
    )


def _cell(
    row: int,
    col: int,
    text: str,
    *,
    row_span: int = 1,
    col_span: int = 1,
    column_header: bool = False,
    row_header: bool = False,
) -> dict:
    return {
        "start_row_offset_idx": row,
        "end_row_offset_idx": row + row_span,
        "start_col_offset_idx": col,
        "end_col_offset_idx": col + col_span,
        "row_span": row_span,
        "col_span": col_span,
        "text": text,
        "column_header": column_header,
        "row_header": row_header,
        "row_section": False,
    }


class _NativeTable:
    def __init__(self, self_ref: str, html: str = "", error: Exception | None = None):
        self.self_ref = self_ref
        self.html = html
        self.error = error
        self.call = None

    def export_to_html(self, *, doc, add_caption):
        self.call = (doc, add_caption)
        if self.error:
            raise self.error
        return self.html


class _Document:
    def __init__(self, tables: list[_NativeTable]):
        self.tables = tables


if __name__ == "__main__":
    unittest.main()


class OrderedListTextTests(unittest.TestCase):
    def test_preserves_ordered_list_markers_without_duplicating_them(self):
        for marker in ("2.", "2.1", "2.1.1", "１．２", "(3)"):
            with self.subTest(marker=marker):
                item = {"label": "list_item", "marker": marker, "text": "Section A", "orig": f"{marker} Section A"}
                self.assertEqual(_item_text(item, "List-item", {}), f"{marker} Section A")
                item["text"] = item["orig"]
                self.assertEqual(_item_text(item, "List-item", {}), item["orig"])
                item.pop("orig")
                self.assertEqual(_item_text(item, "List-item", {}), item["text"])

    def test_uses_marker_when_orig_is_missing_or_has_different_ocr_text(self):
        item = {"label": "list_item", "marker": "2.1", "text": "Section A", "orig": "Section B"}
        self.assertEqual(_item_text(item, "List-item", {}), "2.1 Section A")
        item.pop("orig")
        self.assertEqual(_item_text(item, "List-item", {}), "2.1 Section A")

    def test_unordered_bullets_and_other_item_kinds_keep_their_text(self):
        for marker in ("・", "-", "•"):
            self.assertEqual(_item_text({"label": "list_item", "marker": marker, "text": "Section A"}, "List-item", {}), "Section A")
        self.assertEqual(_item_text({"label": "text", "marker": "2.", "text": "Section A"}, "Text", {}), "Section A")

    def test_restored_marker_does_not_truncate_multi_page_charspans(self):
        settings = get_settings()
        context = AnalysisContext(
            pdf_path=Path("synthetic.pdf"),
            run_dir=Path("."),
            pages=[PageImage(page=i, width=1000, height=1000, pdf_width=600, pdf_height=600, image_path=f"page-{i}.png") for i in (1, 2)],
            settings=settings,
        )
        item = {
            "label": "list_item",
            "text": "AAAA BBBB",
            "orig": "2.1 AAAA BBBB",
            "marker": "2.1",
            "prov": [
                {"page_no": i, "charspan": span, "bbox": {"l": 10, "t": 10, "r": 100, "b": 50, "coord_origin": "TOPLEFT"}}
                for i, span in [(1, [0, 5]), (2, [5, 9])]
            ],
        }
        records = DoclingAdapter(settings)._records_from_payload({"texts": [item]}, context)
        self.assertEqual([record.text for record in records], ["2.1 AAAA", "BBBB"])

    def test_original_disambiguates_a_marker_from_numbered_body_text(self):
        item = {"label": "list_item", "marker": "2.1", "text": "2.1.1 Section A", "orig": "2.1 2.1.1 Section A"}
        self.assertEqual(_item_text(item, "List-item", {}), item["orig"])


def _pdf_with_image(path: Path, image_box: tuple[float, float, float, float]) -> Path:
    """100×100 point の 1 ページに、画像を ``image_box``（左上原点の left, top, right, bottom）に描いた合成の PDF。"""
    import pypdfium2 as pdfium
    from PIL import Image

    left, top, right, bottom = image_box
    document = pdfium.PdfDocument.new()
    page = document.new_page(100, 100)
    image = pdfium.PdfImage.new(document)
    image.set_bitmap(pdfium.PdfBitmap.from_pil(Image.new("RGB", (8, 8), "white")))
    image.set_matrix(pdfium.PdfMatrix().scale(right - left, bottom - top).translate(left, 100 - bottom))
    page.insert_obj(image)
    page.gen_content()
    document.save(str(path))
    page.close()
    document.close()
    return path


def _picture_page_payload(picture_box: list[int], texts: list[tuple[str, str, list[int]]]) -> dict:
    return {
        "texts": [
            {**_item(text, label, 1, bbox), "self_ref": f"#/texts/{index}", "parent": {"$ref": "#/body"}, "children": []}
            for index, (text, label, bbox) in enumerate(texts)
        ],
        "pictures": [_picture("#/pictures/0", picture_box, [])],
    }


def test_pdf_image_object_bounds_reads_the_placement(tmp_path: Path) -> None:
    from rag_engine.parsing.rendering import pdf_image_object_bounds

    pdf = _pdf_with_image(tmp_path / "source.pdf", (20, 10, 80, 70))

    ((left, bottom, right, top),) = pdf_image_object_bounds(pdf, 1)
    assert [round(value) for value in (left, bottom, right, top)] == [20, 30, 80, 90]


def test_text_inside_the_picture_image_but_outside_the_picture_bbox_is_picture_text(tmp_path: Path) -> None:
    """Docling の図の bbox が画像より狭くても、画像の中のタイトル・目盛りは図の文字（#3485）。"""
    settings = replace(get_settings(), docling_keep_picture_child_text=False)
    context = replace(_context(), pdf_path=_pdf_with_image(tmp_path / "source.pdf", (20, 10, 80, 70)))
    payload = _picture_page_payload(
        [22, 20, 78, 72],
        [
            ("3.1 章の題", "section_header", [10, 2, 40, 7]),  # 画像の外の見出しは残す
            ("グラフの題（件）", "section_header", [25, 12, 50, 17]),  # 画像の中・図の bbox の上
            ("200", "text", [20, 18, 24, 23]),  # 図の bbox の端にまたがる目盛り
            ("グラフの説明です。", "text", [30, 40, 60, 45]),  # 画像の中でも文は本文に残す（#655）
            ("図 1 グラフの例", "text", [30, 80, 70, 85]),  # 画像の下（外）の文字は残す
        ],
    )

    records = DoclingAdapter(settings)._records_from_payload(payload, context)

    ordinary = [(record.category, record.text) for record in records if record.category != "Picture"]
    assert ordinary == [("Section-header", "3.1 章の題"), ("Text", "グラフの説明です。"), ("Text", "図 1 グラフの例")]
    aggregate = next(record for record in records if record.raw_type == "picture_ocr_text")
    assert "グラフの題（件）" in aggregate.text and "200" in aggregate.text


def test_a_full_page_image_does_not_widen_the_picture(tmp_path: Path) -> None:
    """全ページの画像（スキャン）は図の画像とみなさず、図の外のラベルは今までどおり残す。"""
    settings = replace(get_settings(), docling_keep_picture_child_text=False)
    context = replace(_context(), pdf_path=_pdf_with_image(tmp_path / "source.pdf", (0, 0, 100, 100)))
    payload = _picture_page_payload([20, 20, 60, 60], [("ラベル", "text", [65, 65, 75, 70])])

    records = DoclingAdapter(settings)._records_from_payload(payload, context)

    assert [record.text for record in records if record.category == "Text"] == ["ラベル"]
    assert not any(record.raw_type == "picture_ocr_text" for record in records)
