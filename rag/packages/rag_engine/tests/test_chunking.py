"""chunking の挙動を保護するテスト。"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from rag_engine.chunking import (
    CHILD_CHUNK_LEVEL,
    CHUNK_METADATA_SCHEMA_VERSION,
    CHUNK_SCHEMA_VERSION,
    CHUNK_STRATEGY,
    PARENT_CHUNK_LEVEL,
    SEARCH_TEXT_SCHEMA_VERSION,
    ChunkingConfig,
    ChunkingResult,
    audit_chunk_retrieval_text,
    build_small_to_big_chunks,
    chunk_payload,
    chunk_result_from_payload,
    chunk_table_rows,
    create_chunk_run,
    load_chunk_run_by_id,
    load_latest_chunk_run,
    load_latest_chunk_run_for_run_source,
    load_latest_chunk_run_for_source,
    load_latest_or_source_chunk_run,
)
from rag_engine.retrieval.inquiry_conditions import (
    INQUIRY_CHUNK_METADATA_SCHEMA_VERSION,
    inquiry_profile_contract_hash,
)


class ChunkingTests(unittest.TestCase):
    def test_builds_parent_child_chunks_with_seq_metadata(self):
        payload = _viewer_payload(
            [
                _record(1, 1, "Section-header", "Ⅰ. 仕入先の登録"),
                _record(1, 2, "Text", "仕入先登録の説明です。"),
                _record(1, 3, "List-item", "支払区分に5を設定します。"),
                _record(1, 4, "Page-footer", "31"),
                _record(2, 1, "Text", "次ページの説明です。"),
                _record(2, 2, "Page-footer", "32"),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(
                child_target_chars=80, table_child_target_chars=80, parent_target_chars=200, parent_max_pages=2, parent_max_children=8
            ),
        )

        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]
        parents = [chunk for chunk in chunks if chunk.chunk_level == PARENT_CHUNK_LEVEL]

        self.assertEqual(len(children), 1)
        self.assertEqual(len(parents), 1)
        self.assertEqual(children[0].parent_chunk_id, parents[0].chunk_id)
        self.assertEqual(parents[0].child_chunk_ids, [children[0].chunk_id])
        self.assertEqual(
            children[0].source_seq_ranges, [{"page": 1, "seq_start": 1, "seq_end": 3}, {"page": 2, "seq_start": 1, "seq_end": 1}]
        )
        self.assertEqual(children[0].metadata["section_path"], ["Ⅰ. 仕入先の登録"])
        self.assertEqual(children[0].metadata["layout"]["logical_page_labels"], {"1": 31, "2": 32})
        self.assertNotIn("31", children[0].text)
        self.assertNotIn("32", children[0].text)
        for chunk in [*children, *parents]:
            ranges = chunk.metadata["layout"]["native_text_ranges"]
            native_text = [chunk.text[r["start"] : r["end"]] for r in ranges]
            self.assertIn("仕入先登録の説明です。", native_text)
            self.assertIn("支払区分に5を設定します。", native_text)
            self.assertNotIn("Ⅰ. 仕入先の登録", native_text)

    def test_picture_and_ocr_records_with_same_bbox_are_one_atomic_child(self):
        bbox = [10, 20, 300, 400]
        payload = _viewer_payload(
            [
                _record(1, 1, "Section-header", "帳票説明"),
                _record(
                    1,
                    2,
                    "Picture",
                    "回答用本文: 請求先欄は会社名で固定。",
                    raw_type="picture",
                    bbox=bbox,
                    raw={
                        "vision_crop": "docling/vision/docling-p1-2.png",
                        "vision_context_crop": "docling/vision/docling-p1-2.context.png",
                        "vision_context_record_refs": [{"record_id": "nearby", "page": 1, "category": "Text", "partially_visible": False}],
                        "vision_input_mode": "target_crop_with_context",
                        "vision_model": "vendor.model-a",
                    },
                ),
                _record(1, 3, "Picture", "請求先欄 会社名", raw_type="picture_ocr_text", bbox=bbox),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]
        picture_ref = next(ref for ref in children[0].source_record_refs if ref.get("vision_context_record_refs"))
        self.assertEqual(picture_ref["vision_context_record_refs"][0]["record_id"], "nearby")
        self.assertFalse(picture_ref["vision_context_record_refs"][0]["partially_visible"])

        self.assertEqual(len(children), 1)
        self.assertTrue(children[0].metadata["atomic"])
        self.assertIn("Picture", children[0].metadata["source_categories"])
        self.assertEqual(len(children[0].metadata["image_evidence"]), 1)
        self.assertEqual(children[0].metadata["image_evidence"][0]["raw_type"], "picture")
        self.assertEqual(children[0].metadata["image_evidence"][0]["crop_path"], "docling/vision/docling-p1-2.png")
        self.assertEqual(
            children[0].metadata["image_evidence"][0]["context_crop_path"],
            "docling/vision/docling-p1-2.context.png",
        )
        self.assertEqual(children[0].metadata["image_evidence"][0]["vision_input_mode"], "target_crop_with_context")
        self.assertEqual(children[0].source_seq_ranges, [{"page": 1, "seq_start": 1, "seq_end": 3}])
        self.assertIn("回答用本文", children[0].text)
        self.assertIn("OCR抽出テキスト:\n請求先欄 会社名", children[0].text)

    def test_picture_ocr_is_folded_into_its_picture_image_once(self):
        """図と同じ bbox の OCR は図の画像の ocr_text になり、画像は 1 件だけ（#3455）。"""
        bbox = [10, 30, 90, 80]
        children = self._picture_children(
            [
                _record(1, 1, "Text", "本文。", bbox=[10, 10, 70, 20]),
                _record(1, 2, "Picture", "", raw_type="picture", bbox=bbox, raw={"vision_crop": "docling/vision/p.png"}),
                _record(1, 3, "Picture", "OCR抽出テキスト:\n保存 取消", raw_type="picture_ocr_text", bbox=bbox),
            ]
        )

        self.assertEqual(len(children), 1)
        [image] = children[0].metadata["image_evidence"]
        self.assertEqual((image["image_id"], image["raw_type"], image["crop_path"]), ("docling-p1-2", "picture", "docling/vision/p.png"))
        self.assertEqual(image["ocr_text"], "OCR抽出テキスト: 保存 取消")
        ocr_ref = next(ref for ref in children[0].source_record_refs if ref["raw_type"] == "picture_ocr_text")
        self.assertEqual(ocr_ref["picture_record_id"], "docling-p1-2")

    def test_picture_ocr_joins_its_picture_across_bbox_rounding_and_by_explicit_link(self):
        """丸めの境目の bbox の差・明示の結び付き（source_picture_ref）でも、OCR は図と同じ child（#3455）。"""
        for name, ocr_bbox, ocr_raw in (
            ("bbox_rounding", [10.06, 30, 90, 80], {}),
            ("explicit_self_ref", [12, 33, 88, 78], {"source_picture_ref": "#/pictures/0"}),
        ):
            with self.subTest(name):
                children = self._picture_children(
                    [
                        _record(1, 1, "Text", "本文。", bbox=[10, 10, 70, 20]),
                        _record(1, 2, "Picture", "", raw_type="picture", bbox=[10, 30, 90, 80], raw={"self_ref": "#/pictures/0"}),
                        _record(1, 3, "Picture", "OCR抽出テキスト:\n保存", raw_type="picture_ocr_text", bbox=ocr_bbox, raw=ocr_raw),
                    ]
                )

                self.assertEqual(len(children), 1)
                self.assertEqual([r["record_id"] for r in children[0].source_record_refs], ["docling-p1-2", "docling-p1-3"])
                self.assertEqual([i["image_id"] for i in children[0].metadata["image_evidence"]], ["docling-p1-2"])

    def test_ocr_only_picture_child_carries_the_picture_image(self):
        """図の record が無く OCR だけの図も、OCR の page・bbox を図の領域にした画像の根拠を持つ（#3455）。"""
        children = self._picture_children(
            [
                _record(1, 1, "Text", "本文。", bbox=[10, 10, 70, 20]),
                _record(1, 2, "Picture", "OCR抽出テキスト:\n保存 取消", raw_type="picture_ocr_text", bbox=[10, 30, 90, 80]),
            ]
        )

        [image] = children[0].metadata["image_evidence"]
        self.assertEqual(
            (image["image_id"], image["raw_type"], image["page"], image["bbox"], image["asset_kind"]),
            ("docling-p1-2", "picture", 1, [10, 30, 90, 80], "page_region"),
        )
        self.assertEqual(image["ocr_text"], "OCR抽出テキスト: 保存 取消")

    def test_ocr_of_inline_icon_is_not_image_evidence(self):
        """inline icon の図とその OCR は、OCR があっても画像の根拠にしない（#3455）。"""
        bbox = [72, 40, 80, 48]
        children = self._picture_children(
            [
                _record(1, 1, "Text", "「履歴」ボタンを押すと連携の送信記録を照会します。", bbox=[10, 40, 70, 55]),
                _record(1, 2, "Picture", "", raw_type="picture", bbox=bbox),
                _record(1, 3, "Picture", "OCR抽出テキスト:\n履歴", raw_type="picture_ocr_text", bbox=bbox),
            ]
        )

        self.assertEqual(len(children), 1)
        self.assertNotIn("image_evidence", children[0].metadata)

    def _picture_children(self, records):
        chunks = build_small_to_big_chunks(
            _viewer_payload(records), source_run_id="abcdef", selected_engine_ids=["docling"], config=ChunkingConfig()
        )
        return [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL and "Picture" in chunk.metadata["source_categories"]]

    def test_content_picture_without_text_is_kept_as_image_evidence_chunk(self):
        payload = _viewer_payload(
            [
                _record(
                    1,
                    1,
                    "Picture",
                    "",
                    raw_type="picture",
                    bbox=[0, 0, 100, 100],
                    raw={
                        "vision_crop": "docling/vision/docling-p1-1.png",
                        "vision_error": "timeout",
                    },
                )
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]

        self.assertEqual(len(children), 1)
        self.assertIn("Image evidence", children[0].text)
        self.assertEqual(children[0].metadata["image_evidence"][0]["image_id"], "docling-p1-1")
        self.assertEqual(children[0].metadata["image_evidence"][0]["embedding_modality"], "image_evidence")
        self.assertEqual(children[0].metadata["image_evidence"][0]["crop_path"], "docling/vision/docling-p1-1.png")

    def test_scanned_table_picture_preserves_visual_structure_for_text_and_image_retrieval(self):
        payload = _viewer_payload(
            [
                _record(
                    1,
                    1,
                    "Picture",
                    "表構造: 区分 / 件数 / A / 10",
                    raw_type="picture",
                    bbox=[10, 10, 90, 90],
                    raw={
                        "crop_path": "docling/visuals/docling-p1-1.png",
                        "picture_kind": "table",
                        "visual_kind": "table",
                        "vision_description": {
                            "retrieval_text": "区分Aは10件。",
                            "table_rows": [["A", "10"]],
                        },
                        "visual_structure": {
                            "visual_kind": "table",
                            "table_headers": ["区分", "件数"],
                            "table_rows": [["A", "10"]],
                            "table_summary": "区分Aは10件。",
                        },
                    },
                )
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)
        image = child.metadata["image_evidence"][0]

        self.assertEqual(image["visual_kind"], "table")
        self.assertEqual(image["visual_structure"]["table_headers"], ["区分", "件数"])
        self.assertNotIn("vision_description", child.source_record_refs[0])
        self.assertIn("区分", child.retrieval_text)
        # 検索文脈には JSON の key ではなく、表の見出し・行・要約という中身を語として入れる (#592)。
        visual_context = next(line for line in child.retrieval_text.splitlines() if line.startswith("Visual context:"))
        for word in ("table", "区分 件数", "A 10", "区分Aは10件。"):
            self.assertIn(word, visual_context)
        self.assertNotIn("visual_structure", visual_context)
        self.assertNotIn("{", visual_context)

    def test_flatten_table_html_finishes_quickly_on_many_cell_separators(self):
        """`/` の無い `| | | …` でも時間内に終わる（以前の正規表現は指数的に後戻りした。#1646）。"""
        import time

        from rag_engine.chunking import _flatten_table_html

        started = time.perf_counter()
        flat = _flatten_table_html("<table>x" + "</td>" * 50_000 + "y</table>")
        self.assertLess(time.perf_counter() - started, 1.0)
        self.assertEqual(flat, "x " + "| " * 50_000 + "y")
        # 行の末尾のセルの区切りは外し、`/` の前に `|` の無い区間は変えない。
        self.assertEqual(_flatten_table_html("<table><tr><td>a</td><td>b</td></tr></table>"), "a | b")
        self.assertEqual(_flatten_table_html("<p>2024/01/01</p>"), "2024/01/01")

    def test_retrieval_text_context_is_plain_text_without_cut_html_or_json(self):
        """埋め込み用の補足は、HTML や JSON の途中で切れた文字列を含まず、表の内容を半端に二重化しない (#592)。"""
        from rag_engine.chunking import _context_preview, _flatten_table_html, _table_search_text, _table_visual_evidence_text

        table = (
            "<table><tbody><tr><th>項目</th><th>改正前</th><th>改正後</th></tr><tr><td>① 月間上限額 の引き上げ</td>"
            "<td>月間上限額は 100 万円です。</td><td>月間上限額は、 120 万円まで 引き上げられます。</td></tr></tbody></table>"
        )
        self.assertEqual(
            _flatten_table_html("概要\n" + table + "\n次の段落。"),
            "概要 項目 | 改正前 | 改正後 / ① 月間上限額 の引き上げ | 月間上限額は 100 万円です。 | "
            "月間上限額は、 120 万円まで 引き上げられます。 / 次の段落。",
        )
        # 上限に収まらないときは文・セルの区切りで切り、tag や語の途中では切らない。
        preview = _context_preview("平成 27 年 6 月のお知らせです。" + table, 90)
        self.assertEqual(
            preview, "平成 27 年 6 月のお知らせです。 項目 | 改正前 | 改正後 / ① 月間上限額 の引き上げ | 月間上限額は 100 万円です。…"
        )
        self.assertLessEqual(len(preview), 90)
        self.assertEqual(_context_preview("短い本文。", 120), "短い本文。")

        structure = {"column_headers": [{"column_index": 1, "text": "項目"}, {"column_index": 2, "text": "内容"}], "caption": ""}
        own_crop = {
            "image_id": "docling-p1-6",
            "record_id": "docling-p1-6",
            "page": 1,
            "seq_no": 6,
            "relationship": "source_table_crop",
            "text_preview": table[:240] + "…",
            "ocr_text": "",
        }
        picture = {
            "image_id": "docling-p1-9",
            "record_id": "docling-p1-9",
            "page": 1,
            "seq_no": 9,
            "relationship": "contained",
            "text_preview": "駅前店のアイコン",
            "ocr_text": "BANK",
        }
        context = _table_search_text(
            [
                {
                    "table_id": "docling-p1-6",
                    "structure": structure,
                    "row_group": {"row_start": 2, "row_end": 3, "row_count": 2},
                    "visual_evidence": [own_crop, picture],
                }
            ]
        )
        self.assertEqual(context, "表 docling-p1-6。列見出し 項目 / 内容。2〜3 行目（2 行）。表内画像 docling-p1-9")
        # 表自身の切り出し画像は表 HTML の複製なので本文に載せない。表内の Picture は載せる。
        record = {"raw": {"table_visual_evidence": [own_crop, picture], "table_visual_texts": {}}}
        text = _table_visual_evidence_text(record)
        self.assertNotIn("<table", text)
        self.assertNotIn("docling-p1-6", text)
        self.assertIn("docling-p1-9", text)
        self.assertIn("駅前店のアイコン", text)
        self.assertEqual(_table_visual_evidence_text({"raw": {"table_visual_evidence": [own_crop]}}), "")

    def _children(self, payload, config=None):
        chunks = build_small_to_big_chunks(
            payload, source_run_id="abcdef", selected_engine_ids=["docling"], config=config or ChunkingConfig()
        )
        return [c for c in chunks if c.chunk_level == CHILD_CHUNK_LEVEL], [c for c in chunks if c.chunk_level == PARENT_CHUNK_LEVEL]

    def test_ocr_text_of_an_excluded_decorative_picture_does_not_become_a_child(self):
        # ロゴの Picture 本体は除外されるのに、同じ bbox の OCR 集約が 15 字の単独 child として残っていた (#594)。
        bbox = [1674, 3071, 2159, 3193]
        children, _ = self._children(
            _viewer_payload(
                [
                    _record(1, 1, "Text", "本文です。", bbox=[10, 10, 500, 60]),
                    _record(
                        1,
                        2,
                        "Picture",
                        "",
                        raw_type="picture",
                        bbox=bbox,
                        raw={"visual_role": "decorative", "visual_role_reason": "vision_logo", "rag_excluded": True},
                    ),
                    _record(1, 3, "Picture", "OCR抽出テキスト:\nMUFG", raw_type="picture_ocr_text", bbox=bbox),
                ]
            )
        )
        self.assertEqual([c.text for c in children], ["本文です。"])
        # 除外されない Picture の OCR 集約は従来どおり残る。
        children, _ = self._children(
            _viewer_payload(
                [
                    _record(1, 1, "Text", "本文です。", bbox=[10, 10, 500, 60]),
                    _record(
                        1,
                        2,
                        "Picture",
                        "画像概要: 画面の説明。",
                        raw_type="picture",
                        bbox=[10, 100, 900, 700],
                        raw={"visual_role": "content"},
                    ),
                    _record(1, 3, "Picture", "OCR抽出テキスト:\n保存", raw_type="picture_ocr_text", bbox=[10, 100, 900, 700]),
                ]
            )
        )
        self.assertTrue(any("保存" in c.text for c in children))

    def test_retrieval_text_drops_picture_ocr_only_when_vision_described_the_picture(self):
        """Vision 説明が本文になった図の OCR は検索文から外し、回答用 text には残す。失敗・未実施なら残す (#900)。"""
        from rag_engine.chunking import audit_chunk_retrieval_text

        bbox = [10, 100, 900, 700]
        described = {"visual_role": "content", "vision_description": {"visible_buttons": ["保存"]}}

        def picture_child(picture_raw, picture_text="■ 画面\n操作: 保存ボタンを押す。"):
            children, _ = self._children(
                _viewer_payload(
                    [
                        _record(1, 1, "Text", "本文です。", bbox=[10, 10, 500, 60]),
                        _record(1, 2, "Picture", picture_text, raw_type="picture", bbox=bbox, raw=picture_raw),
                        _record(1, 3, "Picture", "OCR抽出テキスト:\n保存 キャンセル", raw_type="picture_ocr_text", bbox=bbox),
                    ]
                )
            )
            return next(c for c in children if "Picture" in c.metadata["source_categories"])

        child = picture_child(described)
        self.assertIn("OCR抽出テキスト:\n保存 キャンセル", child.text)
        self.assertNotIn("OCR抽出テキスト", child.retrieval_text)
        self.assertNotIn("キャンセル", child.retrieval_text)
        self.assertIn("保存ボタンを押す。", child.retrieval_text)
        self.assertEqual(audit_chunk_retrieval_text([child])["body_truncated_count"], 0)
        # Vision 失敗（vision_error）や未実施（vision_description なし）は OCR が唯一の文字情報なので残す。
        self.assertIn("保存 キャンセル", picture_child({**described, "vision_error": "TimeoutError: x"}).retrieval_text)
        self.assertIn("保存 キャンセル", picture_child({"visual_role": "content"}, "画像概要: 画面の説明。").retrieval_text)
        self.assertIn("保存 キャンセル", picture_child(described, "").retrieval_text)

    def test_retrieval_text_drops_table_picture_ocr_when_the_picture_has_a_description(self):
        """表内 Picture の OCR 行も、画像説明があれば検索文から外し、text には残す (#900)。"""
        table_text = "<table><tr><th>項目</th><th>説明</th></tr><tr><td></td><td>ダイアログの内容を確認します。</td></tr></table>"
        bbox = [20, 30, 45, 55]

        def table_child(picture_text, picture_raw):
            children, _ = self._children(
                _viewer_payload(
                    [
                        _record(1, 1, "Table", table_text, raw_type="table", bbox=[10, 10, 90, 90]),
                        _record(1, 2, "Picture", picture_text, raw_type="picture", bbox=bbox, raw=picture_raw),
                        _record(1, 3, "Picture", "OK キャンセル", raw_type="picture_ocr_text", bbox=bbox),
                    ]
                )
            )
            self.assertEqual(len(children), 1)
            return children[0]

        child = table_child("回答用本文: 処理確認ダイアログで OK を押す。", {"vision_description": {"operation_steps": ["OK"]}})
        self.assertIn("OCR抽出テキスト:\n  OK キャンセル", child.text)
        self.assertIn("画像説明: 回答用本文: 処理確認ダイアログで OK を押す。", child.retrieval_text)
        self.assertNotIn("OCR抽出テキスト", child.retrieval_text)
        self.assertNotIn("キャンセル", child.retrieval_text)
        self.assertEqual(audit_chunk_retrieval_text([child])["body_truncated_count"], 0)
        self.assertIn("OK キャンセル", table_child("回答用本文: 説明。", {"vision_error": "RuntimeError: x"}).retrieval_text)
        self.assertIn("OK キャンセル", table_child("", {}).retrieval_text)

    def test_table_supplement_stays_in_the_table_child(self):
        # text layer から補った表の原文（#564）は表と同じ child に入れ、別 child / parent へ切り離さない (#594)。
        supplement = _record(
            1,
            3,
            "Text",
            "表の補足（セルに割り当てられなかった原文）:\n優遇期間 投資した年から最長 5 年間",
            raw_type="table_unassigned_text",
            bbox=[10, 10, 90, 90],
        )
        big_table = (
            "<table><tr><th>項目</th><th>説明</th></tr>"
            + "".join(f"<tr><td>項目{i}</td><td>{'説明' * 30}</td></tr>" for i in range(1, 12))
            + "</table>"
        )
        children, parents = self._children(
            _viewer_payload(
                [
                    _record(1, 1, "Text", "前置きの本文です。" * 40),
                    _record(1, 2, "Table", big_table, raw_type="table", bbox=[10, 10, 90, 90]),
                    supplement,
                    _record(1, 4, "Text", "後続の本文です。"),
                ]
            ),
            ChunkingConfig(child_target_chars=1600, table_child_target_chars=1600, parent_target_chars=1800, parent_max_children=8),
        )
        table_child = next(c for c in children if "<table" in c.text)
        self.assertIn("最長 5 年間", table_child.text)
        self.assertEqual([r["raw_type"] for r in table_child.source_record_refs], ["table", "table_unassigned_text"])
        self.assertNotIn("最長 5 年間", next(c for c in children if "後続の本文" in c.text).text)
        # 行分割した表では末尾の行グループに付く（表が子の目標文字数を超えるよう目標を小さくする）。
        children, _ = self._children(
            _viewer_payload(
                [
                    _record(1, 1, "Section-header", "手数料一覧"),
                    _record(1, 2, "Table", big_table, raw_type="table", bbox=[10, 10, 90, 90]),
                    supplement,
                ]
            ),
            ChunkingConfig(child_target_chars=900, table_child_target_chars=900),
        )
        groups = [c for c in children if any(r["raw_type"] == "table_row_group" for r in c.source_record_refs)]
        self.assertGreater(len(groups), 1)
        self.assertIn("最長 5 年間", groups[-1].text)
        self.assertFalse(any("最長 5 年間" in c.text for c in children if c is not groups[-1]))

    def test_picture_over_an_unstructured_table_is_not_duplicated(self):
        # HTML 構造化できない表では、表内 Picture の判定を表 child と単独 child で一致させる (#594)。
        children, _ = self._children(
            _viewer_payload(
                [
                    _record(1, 1, "Table", "項目 値\nA 1\nB 2", raw_type="table", bbox=[10, 10, 90, 90]),
                    _record(
                        1,
                        2,
                        "Picture",
                        "回答用本文: 表内のアイコン説明。",
                        raw_type="picture",
                        bbox=[20, 30, 45, 55],
                        raw={"vision_crop": "docling/vision/docling-p1-2.png"},
                    ),
                ]
            )
        )
        mentions = sum("表内のアイコン説明" in c.text for c in children)
        evidence = sum(1 for c in children for i in c.metadata.get("image_evidence", []) if i["image_id"] == "docling-p1-2")
        self.assertEqual((mentions, evidence), (1, 1))

    def test_repeated_heading_does_not_duplicate_the_section_path(self):
        # 帳票の 2 ページ目に同じ表題が再出現しても、1 ページ目の下位見出しを引き継がない (#594)。
        children, parents = self._children(
            _viewer_payload(
                [
                    _record(1, 1, "Section-header", "自動出荷依頼書"),
                    _record(1, 2, "Section-header", "[ ご記入の注意事項 ]"),
                    _record(1, 3, "Text", "1ページ目の注意事項です。"),
                    _record(2, 1, "Section-header", "自動出荷依頼書"),
                    _record(2, 2, "Section-header", "【印鑑の押印例】"),
                    _record(2, 3, "Text", "2ページ目の押印例です。"),
                ]
            )
        )
        second = next(c for c in children if "2ページ目" in c.text)
        self.assertEqual(second.metadata["section_path"], ["自動出荷依頼書", "【印鑑の押印例】"])
        self.assertEqual(
            next(c for c in children if "1ページ目" in c.text).metadata["section_path"], ["自動出荷依頼書", "[ ご記入の注意事項 ]"]
        )

    def test_recurring_heading_text_is_a_heading_even_when_labeled_list_item(self):
        # 目次で Section-header だった「２．…」が本文で List-item と分類されても見出しとして扱う (#626)。
        children, _ = self._children(
            _viewer_payload(
                [
                    _record(1, 1, "Section-header", "１．掛率の登録"),
                    _record(1, 2, "Section-header", "２．顧客への掛率の登録"),
                    _record(2, 1, "Section-header", "１．掛率の登録"),
                    _record(2, 2, "Text", "掛率の登録を行います。"),
                    _record(3, 1, "List-item", "２．顧客への掛率の登録"),
                    _record(3, 2, "Text", "顧客ごとに掛率の登録を行います。"),
                ]
            )
        )
        self.assertEqual(next(c for c in children if "顧客ごとに" in c.text).metadata["section_path"], ["２．顧客への掛率の登録"])
        self.assertEqual(next(c for c in children if "掛率の登録を行います" in c.text).metadata["section_path"], ["１．掛率の登録"])

    def test_flow_list_on_the_cover_page_is_not_a_heading(self):
        # 表紙のフロー一覧（番号行が 3 つ以上並び、後のページで見出しとして再出現する）は見出しにしない。
        # 本文側の「1 ．登録倉庫の確認」（空白入り・List-item）は再出現として見出しに寄せる (#714)。
        children, _ = self._children(
            _viewer_payload(
                [
                    _record(1, 1, "Section-header", "手順書"),
                    _record(1, 2, "List-item", "１．登録倉庫の確認"),
                    _record(1, 3, "Text", "※この資料の２ページを参照してください。"),
                    _record(1, 4, "List-item", "２．倉庫の廃止登録"),
                    _record(1, 5, "List-item", "３．倉庫の登録"),
                    _record(1, 6, "List-item", "１．保管委託先の変更"),
                    _record(2, 1, "Section-header", "＜倉庫統廃合＞"),
                    _record(2, 2, "List-item", "1 ．登録倉庫の確認"),
                    _record(2, 3, "Text", "登録有無を確認します。"),
                    _record(3, 1, "Section-header", "2 ．倉庫の廃止登録"),
                    _record(3, 2, "Text", "廃止を行います。"),
                    _record(4, 1, "Section-header", "３．倉庫の登録"),
                    _record(4, 2, "Text", "新規に登録します。"),
                    _record(5, 1, "Section-header", "１．保管委託先の変更"),
                    _record(5, 2, "Text", "委託先を変更します。"),
                ]
            )
        )

        def path(needle):
            return next(c for c in children if needle in c.text).metadata["section_path"]

        self.assertEqual(path("登録有無")[-1], "1 ．登録倉庫の確認")
        self.assertNotIn("１．保管委託先の変更", path("登録有無"))
        self.assertEqual(path("廃止を行います")[-1], "2 ．倉庫の廃止登録")
        self.assertEqual(path("委託先を変更")[-1], "１．保管委託先の変更")
        self.assertEqual(sum(1 for c in children if "１．保管委託先の変更" in c.metadata["section_path"]), 1)

    def test_restated_current_heading_in_the_body_does_not_change_the_section(self):
        # 節の途中に現在の節名が本文（List-item）として再掲されても節を変えない。Section-header の再出現は戻る (#764)。
        children, _ = self._children(
            _viewer_payload(
                [
                    _record(1, 1, "Section-header", "Ａシステム管理－ ( ４ ) 利用権限設定"),
                    _record(1, 2, "Section-header", "B 【操作説明】"),
                    _record(1, 3, "Text", "①グループ一覧からグループを選択します。"),
                    _record(1, 4, "List-item", "Ａシステム管理-（ 4 ）利用権限設定"),  # 柱に近い再掲
                    _record(1, 5, "Text", "④実行ボタンを押します。"),
                    _record(2, 1, "Section-header", "Ａシステム管理－ ( ４ ) 利用権限設定"),  # 2 ページ目の見出し
                    _record(2, 2, "Text", "⑤戻るボタンを押します。"),
                ]
            )
        )

        def path(needle):
            return next(c for c in children if needle in c.text).metadata["section_path"]

        self.assertEqual(path("④実行"), ["Ａシステム管理－ ( ４ ) 利用権限設定", "B 【操作説明】"])
        self.assertEqual(path("①グループ"), path("④実行"))
        self.assertEqual(path("⑤戻る"), ["Ａシステム管理－ ( ４ ) 利用権限設定"])

    def test_route_shaped_caption_becomes_a_heading(self):
        # 図の caption として返った画面経路行は見出しとして section_path に載せる (#626)。
        children, _ = self._children(
            _viewer_payload(
                [
                    _record(1, 1, "Section-header", "１．掛率の登録"),
                    _record(1, 2, "Caption", "[ 画面：マスタ管理⇒マスタ管理 2 タブ⇒掛率登録 ]"),
                    _record(1, 3, "List-item", "①新しい掛率コードを入力します。"),
                    _record(1, 4, "Caption", "図1 掛率登録画面"),
                    _record(1, 5, "Text", "以上"),
                ]
            )
        )
        self.assertEqual(
            next(c for c in children if "新しい掛率コード" in c.text).metadata["section_path"],
            ["１．掛率の登録", "[ 画面：マスタ管理⇒マスタ管理 2 タブ⇒掛率登録 ]"],
        )
        self.assertNotIn("図1 掛率登録画面", " ".join(p for c in children for p in c.metadata["section_path"]))

    def test_legacy_picture_ocr_heading_is_normalized_in_chunks(self):
        bbox = [10, 20, 300, 400]
        payload = _viewer_payload(
            [
                _record(
                    1,
                    1,
                    "Picture",
                    "回答用本文: 請求先欄は会社名で固定。",
                    raw_type="picture",
                    bbox=bbox,
                    raw={"vision_crop": "docling/vision/docling-p1-1.png"},
                ),
                _record(1, 2, "Picture", "Docling OCR:\n請求先欄 会社名", raw_type="picture_ocr_text", bbox=bbox),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]

        self.assertEqual(len(children), 1)
        self.assertIn("OCR抽出テキスト:\n請求先欄 会社名", children[0].text)
        self.assertNotIn("Docling OCR", children[0].text)

    def test_small_footer_logo_picture_is_excluded_from_chunks(self):
        payload = _viewer_payload(
            [
                _record(1, 1, "Section-header", "画面説明"),
                _record(1, 2, "Text", "本部連携の在庫台帳を処理する画面です。"),
                _record(
                    1,
                    3,
                    "Picture",
                    "画像概要: 青い円形のロゴマーク。",
                    raw_type="picture",
                    bbox=[45, 92, 55, 98],
                ),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]

        self.assertEqual(len(children), 1)
        self.assertNotIn("ロゴマーク", children[0].text)
        self.assertNotIn("Picture", children[0].metadata["source_categories"])
        self.assertNotIn("image_evidence", children[0].metadata)

    def test_small_operation_icon_is_merged_into_nearby_text_chunk_without_image_evidence(self):
        payload = _viewer_payload(
            [
                _record(1, 1, "Text", "「履歴」ボタンを押すと連携の送信記録を照会します。", bbox=[10, 40, 70, 55]),
                _record(1, 2, "Picture", "", raw_type="picture", bbox=[72, 40, 80, 48]),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]

        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].text, "「履歴」ボタンを押すと連携の送信記録を照会します。")
        self.assertEqual([ref["category"] for ref in children[0].source_record_refs], ["Text", "Picture"])
        self.assertEqual(children[0].source_record_refs[1]["visual_role"], "inline_icon")
        self.assertNotIn("image_evidence", children[0].metadata)

    def test_inline_icon_next_to_a_table_is_merged_into_the_following_text_chunk(self):
        # Table は inline icon を取り込まないので merge 先にしない。落とさず次の地の文へ寄せる (#789)。
        payload = _viewer_payload(
            [
                _record(1, 1, "Table", "項目 | 「保存」ボタンを押します。", raw_type="table", bbox=[10, 20, 70, 35]),
                _record(1, 2, "Picture", "", raw_type="picture", bbox=[72, 36, 80, 44]),
                _record(1, 3, "Text", "「履歴」ボタンを押すと連携の送信記録を照会します。", bbox=[10, 50, 70, 60]),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]

        self.assertEqual(len(children), 2)
        table_child = next(child for child in children if "「保存」" in child.text)
        text_child = next(child for child in children if "「履歴」" in child.text)
        self.assertEqual([ref["category"] for ref in table_child.source_record_refs], ["Table"])
        self.assertEqual(
            [(ref["category"], ref.get("visual_role")) for ref in text_child.source_record_refs],
            [("Picture", "inline_icon"), ("Text", None)],
        )

    def test_chunking_does_not_mutate_the_input_payload(self):
        # Picture の役割付けで呼び出し側の payload を書き換えない。同じ payload で 2 回呼んでも同じ結果 (#798)。
        import copy

        payload = _viewer_payload(
            [
                _record(1, 1, "Text", "「履歴」ボタンを押すと連携の送信記録を照会します。", bbox=[10, 40, 70, 55]),
                _record(1, 2, "Picture", "", raw_type="picture", bbox=[72, 40, 80, 48]),
                _record(1, 3, "Picture", "", raw_type="picture", bbox=[0, 95, 8, 100]),
            ]
        )
        before = copy.deepcopy(payload)

        first = build_small_to_big_chunks(payload, source_run_id="abcdef", selected_engine_ids=["docling"], config=ChunkingConfig())
        self.assertEqual(payload, before)
        second = build_small_to_big_chunks(payload, source_run_id="abcdef", selected_engine_ids=["docling"], config=ChunkingConfig())

        self.assertEqual([c.source_record_refs for c in first], [c.source_record_refs for c in second])
        self.assertTrue(any(r.get("visual_role") for c in first for r in c.source_record_refs if r["category"] == "Picture"))

    def test_table_html_is_parsed_once_per_table(self):
        # 同じ表を 4 回解析していたのを、文字列 key の共有で 1 回にする (#804)。
        from unittest.mock import patch

        from rag_engine import chunking
        from rag_engine.parsing.table_structure import parse_html_table_structure

        html = "<table><tr><th>項目</th><th>値</th></tr><tr><td>A</td><td>1</td></tr></table>"
        payload = _viewer_payload(
            [
                _record(1, 1, "Table", html, raw_type="table", bbox=[10, 20, 70, 60]),
                _record(1, 2, "Picture", "", raw_type="picture", bbox=[20, 30, 40, 50]),
            ]
        )
        chunking._table_structure.cache_clear()
        try:
            with patch("rag_engine.chunking.records.parse_html_table_structure", wraps=parse_html_table_structure) as parse:
                build_small_to_big_chunks(payload, source_run_id="abcdef", selected_engine_ids=["docling"], config=ChunkingConfig())
        finally:
            chunking._table_structure.cache_clear()
        self.assertEqual(parse.call_count, 1)

    def test_small_table_is_kept_atomic_with_structured_metadata(self):
        table_text = (
            "<table><caption>処理確認</caption>"
            "<tr><th>画面</th><th>説明</th></tr>"
            "<tr><td>出荷情報</td><td>OK を押します。</td></tr>"
            "</table>"
        )
        payload = _viewer_payload([_record(1, 1, "Table", table_text, raw_type="table")])

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(child_target_chars=300, table_child_target_chars=300, parent_target_chars=1200),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]

        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].text, table_text)
        self.assertTrue(children[0].metadata["atomic"])
        self.assertIn("Table", children[0].metadata["source_categories"])
        table_context = children[0].metadata["table_context"][0]
        self.assertEqual(table_context["structure"]["caption"], "処理確認")
        self.assertEqual(table_context["structure"]["row_count"], 2)
        self.assertEqual(table_context["structure"]["column_count"], 2)
        self.assertEqual(table_context["structure"]["column_headers"][0]["text"], "画面")
        self.assertEqual(table_context["chunking_strategy"], "structured_table")

    def test_native_table_crop_becomes_image_embedding_evidence(self):
        table_text = (
            "<table><caption>処理確認</caption>"
            "<tr><th>画面</th><th>説明</th></tr>"
            "<tr><td>出荷情報</td><td>OK を押します。</td></tr>"
            "</table>"
        )
        payload = _viewer_payload(
            [
                _record(
                    1,
                    1,
                    "Table",
                    table_text,
                    raw_type="table",
                    bbox=[10, 10, 90, 90],
                    raw={"crop_path": "docling/visuals/docling-p1-1.png"},
                )
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertEqual(len(child.metadata["image_evidence"]), 1)
        evidence = child.metadata["image_evidence"][0]
        self.assertEqual(evidence["image_id"], "docling-p1-1")
        self.assertEqual(evidence["raw_type"], "table")
        self.assertEqual(evidence["relationship"], "source_table_crop")
        self.assertEqual(evidence["asset_kind"], "crop")
        self.assertEqual(evidence["crop_path"], "docling/visuals/docling-p1-1.png")
        self.assertTrue(child.metadata["image_evidence"])
        self.assertEqual(
            child.metadata["table_context"][0]["visual_evidence"][0]["relationship"],
            "source_table_crop",
        )

    def test_oversized_table_is_split_into_row_group_children(self):
        table_text = (
            "<table><tr><th>項目</th><th>説明</th></tr>"
            + "".join(f"<tr><td>項目{i}</td><td>{'長い説明' * 20}</td></tr>" for i in range(1, 8))
            + "</table>"
        )
        payload = _viewer_payload([_record(1, 1, "Table", table_text, raw_type="table")])

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(child_target_chars=300, table_child_target_chars=300, parent_target_chars=500),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]
        parents = [chunk for chunk in chunks if chunk.chunk_level == PARENT_CHUNK_LEVEL]
        parent_child_ids = [child_id for parent in parents for child_id in parent.child_chunk_ids]

        self.assertGreater(len(children), 1)
        self.assertEqual(parent_child_ids, [child.chunk_id for child in children])
        self.assertTrue(all(child.metadata["atomic"] for child in children))
        self.assertTrue(all("Table" in child.metadata["source_categories"] for child in children))
        self.assertTrue(all("列見出し: 項目 / 説明" in child.text for child in children))
        self.assertEqual(children[0].source_record_refs[0]["raw_type"], "table_row_group")
        self.assertEqual(children[0].metadata["table_context"][0]["row_group"]["source_table_record_id"], "docling-p1-1")
        self.assertEqual(children[0].metadata["table_context"][0]["chunking_strategy"], "row_group")

    def test_oversized_table_crop_is_attached_only_to_first_row_group(self):
        table_text = (
            "<table><tr><th>項目</th><th>説明</th></tr>"
            + "".join(f"<tr><td>項目{i}</td><td>{'長い説明' * 20}</td></tr>" for i in range(1, 8))
            + "</table>"
        )
        payload = _viewer_payload(
            [
                _record(
                    1,
                    1,
                    "Table",
                    table_text,
                    raw_type="table",
                    raw={"crop_path": "docling/visuals/docling-p1-1.png"},
                )
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(child_target_chars=300, table_child_target_chars=300, parent_target_chars=500),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]
        table_crop_children = [
            child
            for child in children
            if any(image.get("relationship") == "source_table_crop" for image in child.metadata.get("image_evidence", []))
        ]

        self.assertGreater(len(children), 1)
        self.assertEqual([child.chunk_id for child in table_crop_children], [children[0].chunk_id])

    def test_table_row_group_visual_evidence_is_limited_to_matching_rows(self):
        table_text = (
            "<table><tr><th>画像</th><th>説明</th></tr>"
            + "".join(f"<tr><td></td><td>項目{i} {'長い説明' * 80}</td></tr>" for i in range(1, 6))
            + "</table>"
        )
        payload = _viewer_payload(
            [
                _record(1, 1, "Table", table_text, raw_type="table", bbox=[0, 0, 100, 100]),
                _record(
                    1,
                    2,
                    "Picture",
                    "上段の図",
                    raw_type="picture",
                    bbox=[10, 20, 25, 30],
                    raw={"vision_crop": "docling/vision/upper.png"},
                ),
                _record(
                    1,
                    3,
                    "Picture",
                    "下段の図",
                    raw_type="picture",
                    bbox=[10, 85, 25, 95],
                    raw={"vision_crop": "docling/vision/lower.png"},
                ),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(child_target_chars=300, table_child_target_chars=300, parent_target_chars=500),
        )
        row_children = [
            chunk
            for chunk in chunks
            if chunk.chunk_level == CHILD_CHUNK_LEVEL and chunk.source_record_refs[0]["raw_type"] == "table_row_group"
        ]
        children_by_start = {child.metadata["table_context"][0]["row_group"]["row_start"]: child for child in row_children}

        self.assertGreaterEqual(len(row_children), 5)
        self.assertEqual(
            [item["record_id"] for item in children_by_start[2].metadata["table_context"][0]["visual_evidence"]],
            ["docling-p1-2"],
        )
        self.assertEqual(children_by_start[3].metadata["table_context"][0]["visual_evidence"], [])
        self.assertEqual(
            [item["record_id"] for item in children_by_start[6].metadata["table_context"][0]["visual_evidence"]],
            ["docling-p1-3"],
        )
        self.assertEqual([image["image_id"] for image in children_by_start[2].metadata["image_evidence"]], ["docling-p1-2"])
        self.assertEqual([image["image_id"] for image in children_by_start[6].metadata["image_evidence"]], ["docling-p1-3"])
        self.assertIn("上段の図", children_by_start[2].text)
        self.assertNotIn("下段の図", children_by_start[2].text)
        self.assertNotIn("上段の図", children_by_start[3].text)
        self.assertNotIn("下段の図", children_by_start[3].text)

    def test_picture_barely_overlapping_row_grouped_table_is_kept_in_nearest_group(self):
        # 表に重なる Picture は単独 chunk にならないため、行帯の判定に漏れても失われてはならない。
        table_text = (
            "<table><tr><th>画像</th><th>説明</th></tr>"
            + "".join(f"<tr><td></td><td>項目{i} {'長い説明' * 80}</td></tr>" for i in range(1, 6))
            + "</table>"
        )
        payload = _viewer_payload(
            [
                _record(1, 1, "Table", table_text, raw_type="table", bbox=[0, 0, 100, 100]),
                _record(
                    1,
                    2,
                    "Picture",
                    "表の下にはみ出す図",
                    raw_type="picture",
                    bbox=[10, 99, 90, 200],
                    raw={"vision_crop": "docling/vision/below.png"},
                ),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(child_target_chars=300, table_child_target_chars=300, parent_target_chars=500),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]
        holders = [child for child in children if "表の下にはみ出す図" in child.text]

        self.assertEqual(len(holders), 1)
        self.assertEqual(holders[0].metadata["table_context"][0]["row_group"]["row_start"], 6)
        self.assertEqual([image["image_id"] for image in holders[0].metadata["image_evidence"]], ["docling-p1-2"])

    def test_contained_picture_description_is_not_truncated_in_table_chunk_text(self):
        # 表内 Picture の説明が本文へ入る経路は表 chunk だけなので、表示用 preview の長さで切らない。
        description = "図の説明" + "あ" * 600 + "末尾の条件"
        payload = _viewer_payload(
            [
                _record(1, 1, "Table", "<table><tr><th>a</th></tr><tr><td>b</td></tr></table>", raw_type="table", bbox=[0, 0, 100, 100]),
                _record(
                    1,
                    2,
                    "Picture",
                    description,
                    raw_type="picture",
                    bbox=[10, 10, 90, 90],
                    raw={"vision_crop": "docling/vision/in-table.png"},
                ),
            ]
        )

        chunks = build_small_to_big_chunks(payload, source_run_id="abcdef", selected_engine_ids=["docling"], config=ChunkingConfig())
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertIn(description, child.text)
        # 全文は本文だけに置き、metadata には表示用の preview だけを残す。
        self.assertNotIn("末尾の条件", json.dumps(child.metadata, ensure_ascii=False))
        self.assertNotIn("末尾の条件", json.dumps(child.source_record_refs, ensure_ascii=False))

    def test_table_chunk_keeps_contained_picture_as_visual_evidence_without_orphan_chunk(self):
        table_text = "<table><tr><th>画像</th><th>説明</th></tr><tr><td></td><td>OK で選んだ処理を取り消す場合。</td></tr></table>"
        payload = _viewer_payload(
            [
                _record(1, 1, "Table", table_text, raw_type="table", bbox=[10, 10, 90, 90]),
                _record(
                    1,
                    2,
                    "Picture",
                    "回答用本文: 処理確認ダイアログで OK を押す。",
                    raw_type="picture",
                    bbox=[20, 30, 45, 55],
                    raw={
                        "vision_crop": "docling/vision/docling-p1-2.png",
                        "vision_context_crop": "docling/vision/docling-p1-2.context.png",
                    },
                ),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]

        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].metadata["source_categories"], ["Table"])
        table_visuals = children[0].metadata["table_context"][0]["visual_evidence"]
        self.assertEqual([image["record_id"] for image in table_visuals], ["docling-p1-2"])
        self.assertEqual(table_visuals[0]["relationship"], "contained_in_table")
        self.assertEqual(table_visuals[0]["vision_crop"], "docling/vision/docling-p1-2.png")
        self.assertEqual(children[0].metadata["image_evidence"][0]["image_id"], "docling-p1-2")
        self.assertEqual(children[0].metadata["image_evidence"][0]["crop_path"], "docling/vision/docling-p1-2.png")

    def test_table_chunk_merges_contained_picture_ocr_without_orphan_picture_chunk(self):
        table_text = "<table><tr><th>画像</th><th>説明</th></tr><tr><td></td><td>ダイアログの内容を確認します。</td></tr></table>"
        bbox = [20, 30, 45, 55]
        payload = _viewer_payload(
            [
                _record(1, 1, "Table", table_text, raw_type="table", bbox=[10, 10, 90, 90]),
                _record(
                    1,
                    2,
                    "Picture",
                    "回答用本文: 処理確認ダイアログで OK を押す。",
                    raw_type="picture",
                    bbox=bbox,
                    raw={"vision_crop": "docling/vision/docling-p1-2.png"},
                ),
                _record(1, 3, "Picture", "OK キャンセル", raw_type="picture_ocr_text", bbox=bbox),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]

        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].metadata["source_categories"], ["Table"])
        self.assertIn("表内画像:", children[0].text)
        self.assertIn("画像説明: 回答用本文: 処理確認ダイアログで OK を押す。", children[0].text)
        self.assertIn("OCR抽出テキスト:\n  OK キャンセル", children[0].text)
        self.assertEqual([image["image_id"] for image in children[0].metadata["image_evidence"]], ["docling-p1-2"])

    def test_caption_is_attached_to_nearby_picture_chunk(self):
        payload = _viewer_payload(
            [
                _record(
                    1,
                    1,
                    "Picture",
                    "画像概要: 登録画面の操作エリア。",
                    raw_type="picture",
                    bbox=[10, 10, 80, 45],
                ),
                _record(1, 2, "Caption", "図 1 登録画面", bbox=[10, 47, 80, 55]),
                _record(1, 3, "Text", "次の説明です。"),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]

        self.assertEqual(len(children), 2)
        self.assertEqual(children[0].metadata["source_categories"], ["Picture", "Caption"])
        self.assertIn("図 1 登録画面", children[0].text)
        caption = children[0].metadata["layout"]["caption_context"][0]
        self.assertEqual(caption["relationship"], "caption_for")
        self.assertEqual(caption["target_record_id"], "docling-p1-1")

    def test_caption_for_picture_inside_table_is_attached_to_table_chunk(self):
        table_text = "<table><tr><th>画像</th><th>説明</th></tr><tr><td></td><td>状態を確認します。</td></tr></table>"
        payload = _viewer_payload(
            [
                _record(1, 1, "Table", table_text, raw_type="table", bbox=[10, 10, 90, 80]),
                _record(
                    1,
                    2,
                    "Picture",
                    "画像概要: 確認ダイアログ。",
                    raw_type="picture",
                    bbox=[15, 20, 35, 45],
                ),
                _record(1, 3, "Caption", "図 2 確認ダイアログ", bbox=[15, 47, 35, 55]),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertEqual(child.metadata["source_categories"], ["Table", "Caption"])
        self.assertIn("図 2 確認ダイアログ", child.text)
        self.assertEqual(child.metadata["layout"]["caption_context"][0]["target_record_id"], "docling-p1-1")
        self.assertEqual(child.metadata["table_context"][0]["visual_evidence"][0]["record_id"], "docling-p1-2")

    def test_footnote_is_attached_to_nearby_table_chunk(self):
        table_text = "<table><tr><th>項目</th><th>説明</th></tr><tr><td>A</td><td>登録します。</td></tr></table>"
        payload = _viewer_payload(
            [
                _record(1, 1, "Table", table_text, raw_type="table", bbox=[10, 10, 90, 50]),
                _record(1, 2, "Footnote", "注: 権限がない場合は処理できません。", bbox=[10, 52, 90, 60]),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertEqual(child.metadata["source_categories"], ["Table", "Footnote"])
        self.assertIn("権限がない場合", child.text)
        footnote = child.metadata["layout"]["footnote_context"][0]
        self.assertEqual(footnote["relationship"], "footnote_for")
        self.assertEqual(footnote["target_record_id"], "docling-p1-1")

    def test_list_items_preserve_sequence_metadata(self):
        payload = _viewer_payload(
            [
                _record(1, 1, "Section-header", "登録手順"),
                _record(1, 2, "List-item", "1. 受付情報を確認します。"),
                _record(1, 3, "List-item", "2. チェックボタンを押します。"),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertEqual(child.metadata["layout"]["list_context"][0]["marker"], "1.")
        self.assertEqual(child.metadata["layout"]["list_context"][0]["next_record_id"], "docling-p1-3")
        self.assertEqual(child.metadata["layout"]["list_context"][1]["previous_record_id"], "docling-p1-2")

    def test_list_sequence_metadata_does_not_cross_independent_lists(self):
        payload = _viewer_payload(
            [
                _record(1, 1, "List-item", "1. 受付情報を確認します。"),
                _record(1, 2, "List-item", "2. チェックボタンを押します。"),
                _record(1, 3, "Text", "別の操作です。"),
                _record(1, 4, "List-item", "1. 送信結果を確認します。"),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)
        by_id = {item["record_id"]: item for item in child.metadata["layout"]["list_context"]}

        self.assertEqual(by_id["docling-p1-2"]["next_record_id"], "")
        self.assertEqual(by_id["docling-p1-4"]["previous_record_id"], "")

    def test_formula_records_preserve_expression_metadata(self):
        payload = _viewer_payload(
            [
                _record(
                    1,
                    1,
                    "Formula",
                    "E = mc^2",
                    raw_type="formula",
                    raw={"latex": "E = mc^2", "display": True},
                )
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertEqual(child.metadata["layout"]["formula_context"][0]["expression"], "E = mc^2")
        self.assertEqual(child.metadata["layout"]["formula_context"][0]["format"], "latex")

    def test_selection_mark_without_text_is_kept_as_form_context(self):
        payload = _viewer_payload(
            [
                _record(
                    1,
                    1,
                    "Text",
                    "",
                    raw_type="selection_element",
                    raw={"field_name": "送信対象", "selected": True},
                )
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertIn("フォーム抽出: 送信対象 / 選択済み", child.text)
        self.assertTrue(child.metadata["layout"]["form_context"][0]["selected"])
        self.assertEqual(len(child.metadata["image_evidence"]), 1)
        self.assertEqual(child.metadata["image_evidence"][0]["image_id"], "abcdef-form-page-1")
        self.assertEqual(child.metadata["image_evidence"][0]["asset_kind"], "page")
        self.assertEqual(child.metadata["image_evidence"][0]["relationship"], "form_page_layout")
        self.assertEqual(child.metadata["image_evidence"][0]["visual_kind"], "form")

    def test_form_region_crop_is_preferred_over_page_image(self):
        payload = _viewer_payload(
            [
                _record(
                    1,
                    1,
                    "Text",
                    "申込区分: 新規",
                    raw_type="form",
                    bbox=[10, 20, 90, 80],
                    raw={
                        "field_name": "申込区分",
                        "value": "新規",
                        "crop_path": "docling/visuals/docling-p1-1.png",
                    },
                )
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)
        image = child.metadata["image_evidence"][0]

        self.assertEqual(image["image_id"], "docling-p1-1")
        self.assertEqual(image["asset_kind"], "crop")
        self.assertEqual(image["crop_path"], "docling/visuals/docling-p1-1.png")
        self.assertEqual(image["relationship"], "form_region")
        self.assertEqual(image["visual_structure"]["form_fields"][0]["key"], "申込区分")

    def test_multiple_form_regions_on_one_page_use_full_page_evidence(self):
        payload = _viewer_payload(
            [
                _record(
                    1,
                    1,
                    "Text",
                    "申込区分: 新規",
                    raw_type="form",
                    bbox=[5, 10, 45, 45],
                    raw={
                        "field_name": "申込区分",
                        "value": "新規",
                        "crop_path": "docling/visuals/docling-p1-1.png",
                    },
                ),
                _record(
                    1,
                    2,
                    "Text",
                    "審査状態: 承認",
                    raw_type="form",
                    bbox=[55, 55, 95, 90],
                    raw={
                        "field_name": "審査状態",
                        "value": "承認",
                        "crop_path": "docling/visuals/docling-p1-2.png",
                    },
                ),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)
        image = child.metadata["image_evidence"][0]

        self.assertEqual(image["image_id"], "abcdef-form-page-1")
        self.assertEqual(image["asset_kind"], "page")
        self.assertEqual(image["relationship"], "form_page_layout")
        self.assertEqual(image["bbox"], [5.0, 10.0, 95.0, 90.0])
        self.assertEqual(len(image["visual_structure"]["form_fields"]), 2)

    def test_empty_complex_layout_region_is_kept_as_form_image_evidence(self):
        payload = _viewer_payload(
            [
                _record(
                    1,
                    1,
                    "Text",
                    "",
                    raw_type="complex_layout",
                    bbox=[5, 5, 95, 95],
                    raw={"crop_path": "docling/visuals/docling-p1-1.png"},
                )
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertIn("Form image evidence", child.text)
        self.assertTrue(child.metadata["layout"]["form_context"])
        self.assertEqual(child.metadata["image_evidence"][0]["asset_kind"], "crop")
        self.assertEqual(child.metadata["image_evidence"][0]["visual_kind"], "form")

    def test_plain_docling_label_does_not_create_form_context(self):
        payload = _viewer_payload(
            [
                _record(
                    1,
                    1,
                    "Text",
                    "普通の説明です。",
                    raw_type="paragraph",
                    raw={"label": "paragraph"},
                )
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertNotIn("form_context", child.metadata)

    def test_heading_between_same_bbox_picture_records_is_kept_in_following_chunk(self):
        payload = _viewer_payload(
            [
                _record(1, 1, "Picture", "図A", raw_type="picture", bbox=[0, 0, 50, 50]),
                _record(1, 2, "Section-header", "次の節の見出し"),
                _record(1, 3, "Picture", "ocr", raw_type="picture_ocr_text", bbox=[0, 0, 50, 50]),
                _record(1, 4, "Text", "次の節の本文です。"),
            ]
        )

        chunks = build_small_to_big_chunks(payload, source_run_id="abcdef", selected_engine_ids=["docling"], config=ChunkingConfig())
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]

        self.assertEqual(len(children), 2)
        self.assertEqual(children[1].text, "次の節の見出し\n次の節の本文です。")

    def _section_paths(self, headings):
        records = []
        for index, heading in enumerate(headings):
            records.append(_record(1, 2 * index + 1, "Section-header", heading))
            records.append(_record(1, 2 * index + 2, "Text", f"{heading} の本文です。"))
        chunks = build_small_to_big_chunks(
            _viewer_payload(records), source_run_id="abcdef", selected_engine_ids=["docling"], config=ChunkingConfig()
        )
        return [chunk.metadata["section_path"] for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]

    def test_section_path_drops_previous_sibling_and_parent_sections(self):
        paths = self._section_paths(["第1章 概要", "1.1 目的", "1.2 範囲", "第2章 手順", "2.1 登録", "（1）入力", "（2）確認", "2.2 修正"])

        self.assertEqual(paths[2], ["第1章 概要", "1.2 範囲"])
        self.assertEqual(paths[4], ["第2章 手順", "2.1 登録"])
        self.assertEqual(paths[6], ["第2章 手順", "2.1 登録", "（2）確認"])
        self.assertEqual(paths[7], ["第2章 手順", "2.2 修正"])

    def test_section_path_replaces_previous_function_heading_and_its_subheadings(self):
        paths = self._section_paths(["B 仕入先", "B-(1) 新規登録", "【画面説明】", "【操作説明】", "B-(2) 修正", "【画面説明】"])

        # 同じ形（【…】）の階層不明見出しは兄弟として置き換える。
        self.assertEqual(paths[3], ["B 仕入先", "B-(1) 新規登録", "【操作説明】"])
        self.assertEqual(paths[5], ["B 仕入先", "B-(2) 修正", "【画面説明】"])

    def test_section_path_treats_same_form_unknown_headings_as_siblings(self):
        # 番号のない見出しは同じ形の直近の見出しを兄弟として置き換え、先頭の見出し（表題）は残す (#660)。
        paths = self._section_paths(["概要", "目的", "3つの方法", "【補足】", "範囲"])

        self.assertEqual(paths[2], ["概要", "3つの方法"])
        self.assertEqual(paths[3], ["概要", "3つの方法", "【補足】"])
        self.assertEqual(paths[4], ["概要", "範囲"])

    def test_section_path_restarts_lettered_series_under_unknown_heading(self):
        # 操作説明書の「（２） > D 【遷移画面】 > 〔納品書〕 > A/B」の入れ子と、〔…〕 の兄弟を保つ (#660)。
        paths = self._section_paths(
            [
                "（２）納品書発行依頼入力",
                "A 【画面説明】",
                "B 【操作説明】",
                "D 【遷移画面】",
                "〔納品書〕",
                "A 【画面説明】",
                "B 【操作説明】",
                "〔受領書〕",
                "A 【画面説明】",
                "（３）納品書発行依頼一括入力",
            ]
        )

        self.assertEqual(paths[2], ["（２）納品書発行依頼入力", "B 【操作説明】"])
        self.assertEqual(paths[6], ["（２）納品書発行依頼入力", "D 【遷移画面】", "〔納品書〕", "B 【操作説明】"])
        self.assertEqual(paths[8], ["（２）納品書発行依頼入力", "D 【遷移画面】", "〔受領書〕", "A 【画面説明】"])
        self.assertEqual(paths[9], ["（３）納品書発行依頼一括入力"])

    def test_section_path_keeps_parent_error_heading_across_unknown_subheadings(self):
        # 「２． > （７）担当者なし > 【修正方法】」。前のエラーの【修正方法】が（７）の文脈に残らない (#660)。
        paths = self._section_paths(
            [
                "月次締めエラー一覧の見方",
                "２． 「エラー」の対処方法",
                "（１）入荷実績がない",
                "【修正方法１ 入荷を確定する場合】",
                "【修正方法２ 仮入荷を取り消す場合】",
                "（ 12 ）同じ伝票番号が２件以上",
                "【修正方法】",
                "（ A ）システムログ",
                "A 【画面説明】",
            ]
        )

        self.assertEqual(
            paths[4],
            ["月次締めエラー一覧の見方", "２． 「エラー」の対処方法", "（１）入荷実績がない", "【修正方法２ 仮入荷を取り消す場合】"],
        )
        self.assertEqual(
            paths[6], ["月次締めエラー一覧の見方", "２． 「エラー」の対処方法", "（ 12 ）同じ伝票番号が２件以上", "【修正方法】"]
        )
        self.assertEqual(paths[8], ["月次締めエラー一覧の見方", "２． 「エラー」の対処方法", "（ A ）システムログ", "A 【画面説明】"])

    def test_running_header_and_step_sentence_are_not_headings(self):
        # 毎ページ上端に繰り返す柱と、文として終わる「見出し」（手順行）は section_path に載せない (#660)。
        children, parents = self._children(
            _viewer_payload(
                [
                    _record(1, 1, "Section-header", "１データ連携", bbox=[0, 2, 10, 5]),
                    _record(1, 2, "Section-header", "（１）基幹データ取込"),
                    _record(1, 3, "Text", "基幹データを取り込みます。"),
                    _record(1, 4, "Section-header", "２ . 承認情報を入力します。"),
                    _record(1, 5, "Section-header", "５ ."),
                    _record(1, 6, "Text", "取込の理由を入力します。"),
                    _record(2, 1, "Section-header", "１データ連携", bbox=[0, 2, 10, 5]),
                    _record(2, 2, "Section-header", "（２）売上データ取込"),
                    _record(2, 3, "Text", "売上データを取り込みます。"),
                    _record(2, 4, "Section-header", "１データ連携", bbox=[0, 95, 10, 98]),
                    # 1 ページしかない章の柱。同じ帯・同じ高さなので柱の同族として章の根になる。
                    _record(3, 1, "Section-header", "２売上連動", bbox=[0, 2, 10, 5]),
                    _record(3, 2, "Section-header", "（１）売上情報照会"),
                    _record(3, 3, "Text", "売上情報を照会します。"),
                ]
            )
        )
        self.assertEqual(next(c for c in children if "売上情報を" in c.text).metadata["section_path"], ["２売上連動", "（１）売上情報照会"])
        joined = " ".join(p for c in children for p in c.metadata["section_path"])
        self.assertNotIn("承認情報", joined)
        self.assertNotIn("５ .", joined)
        step = next(c for c in children if "承認情報" in c.text)
        # 柱の初出は章として path の根に残り、2 回目以降（2 ページ目の上端・下端）は path にも本文にも出ない。
        self.assertEqual(step.metadata["section_path"], ["１データ連携", "（１）基幹データ取込"])
        self.assertEqual(
            next(c for c in children if "売上データを" in c.text).metadata["section_path"], ["１データ連携", "（２）売上データ取込"]
        )
        self.assertIn("List-item", step.metadata["source_categories"])
        self.assertEqual(sum("１データ連携" in c.text for c in children), 1)
        # 親は番号付き機能見出しの単位をまたがない。
        self.assertTrue(all(len({tuple(_unit(c)) for c in ch}) == 1 for ch in _children_by_parent(children, parents)))

    def test_long_text_record_is_split_at_sentence_boundaries(self):
        sentences = [f"これは{i}番目の文で、手順の説明が続きます（補足「注意。」を含む）。" for i in range(1, 121)]
        long_text = "".join(sentences)
        payload = _viewer_payload(
            [
                _record(1, 1, "Section-header", "1. 手順"),
                _record(1, 2, "Text", long_text),
                _record(1, 3, "Text", "短い後続の段落です。"),
            ]
        )
        config = ChunkingConfig(child_target_chars=300, table_child_target_chars=300, parent_target_chars=1200)

        chunks = build_small_to_big_chunks(payload, source_run_id="abcdef", selected_engine_ids=["docling"], config=config)
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]

        self.assertGreater(len(children), 5)
        # 見出しは本文から切り離さないため、先頭 child だけ見出しの分を超える。
        self.assertLessEqual(children[0].char_count, 300 + len("1. 手順\n"))
        self.assertTrue(all(child.char_count <= 300 for child in children[1:]))
        self.assertEqual(
            "".join(child.text for child in children).replace("\n", ""),
            "1. 手順" + long_text + "短い後続の段落です。",
        )
        # 文の途中や閉じ括弧の前では切らない。
        self.assertTrue(all(child.text.endswith("。") for child in children))
        self.assertTrue(all(ref["record_id"] == "docling-p1-2" for ref in children[1].source_record_refs))
        self.assertEqual(audit_chunk_retrieval_text(children)["over_target_count"], 0)

    def test_long_text_without_sentence_boundary_is_split_by_target_chars(self):
        payload = _viewer_payload([_record(1, 1, "Text", "あ" * 2000)])

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(child_target_chars=300, table_child_target_chars=300, parent_target_chars=1200),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]

        self.assertEqual([child.char_count for child in children], [300] * 6 + [200])

    def test_logical_page_labels_are_listed_in_numeric_page_order(self):
        payload = _viewer_payload(
            [
                _record(9, 1, "Text", "九ページ目の本文です。"),
                _record(9, 2, "Page-footer", "9"),
                _record(10, 1, "Text", "十ページ目の本文です。"),
                _record(10, 2, "Page-footer", "10"),
            ]
        )

        chunks = build_small_to_big_chunks(payload, source_run_id="abcdef", selected_engine_ids=["docling"], config=ChunkingConfig())
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertIn("Pages: p.9-10; p.9=logical 9 / p.10=logical 10", child.retrieval_text)

    def test_picture_inside_table_structured_only_by_enhanced_html_has_no_orphan_chunk(self):
        payload = _viewer_payload(
            [
                _record(
                    1,
                    1,
                    "Table",
                    "画面 説明 登録画面 登録ボタンを押します",
                    raw_type="table",
                    bbox=[0, 0, 100, 100],
                    raw={
                        "table_enhanced_html": "<table><tr><th>画面</th><th>説明</th></tr>"
                        "<tr><td>登録画面</td><td>登録ボタンを押します</td></tr></table>"
                    },
                ),
                _record(
                    1,
                    2,
                    "Picture",
                    "登録画面の図",
                    raw_type="picture",
                    bbox=[10, 10, 40, 40],
                    raw={"vision_crop": "docling/vision/in-table.png"},
                ),
            ]
        )

        chunks = build_small_to_big_chunks(payload, source_run_id="abcdef", selected_engine_ids=["docling"], config=ChunkingConfig())
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]

        self.assertEqual(len(children), 1)
        self.assertEqual(children[0].text.count("登録画面の図"), 1)

    def test_repeated_page_header_footer_are_excluded_without_chunk_level_diagnostics(self):
        payload = _viewer_payload(
            [
                _record(1, 1, "Page-header", "操作マニュアル"),
                _record(1, 2, "Text", "登録手順です。"),
                _record(1, 3, "Page-footer", "1"),
                _record(2, 1, "Page-header", "操作マニュアル"),
                _record(2, 2, "Text", "確認手順です。"),
                _record(2, 3, "Page-footer", "2"),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(child_target_chars=300, table_child_target_chars=300, parent_target_chars=1200),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertNotIn("操作マニュアル", child.text)
        self.assertEqual(child.metadata["layout"]["logical_page_labels"], {"1": 1, "2": 2})
        self.assertNotIn("page_context", child.metadata)
        self.assertNotIn("retrieval", child.metadata)

    def test_japanese_and_decorated_page_number_footers_are_excluded_and_labeled(self):
        # 漢字・かなに続く数字は \\b で区切れないため、数字だけのフッターと別に判定が要る。
        for labels in (("1ページ", "2ページ"), ("1頁", "2頁"), ("- 1 -", "- 2 -"), ("P.1", "P.2"), ("1 / 20", "2 / 20")):
            with self.subTest(labels=labels):
                payload = _viewer_payload(
                    [
                        _record(1, 1, "Text", "登録手順です。"),
                        _record(1, 2, "Page-footer", labels[0]),
                        _record(2, 1, "Text", "確認手順です。"),
                        _record(2, 2, "Page-footer", labels[1]),
                    ]
                )

                chunks = build_small_to_big_chunks(
                    payload,
                    source_run_id="abcdef",
                    selected_engine_ids=["docling"],
                    config=ChunkingConfig(child_target_chars=300, table_child_target_chars=300, parent_target_chars=1200),
                )
                child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

                self.assertEqual(child.text, "登録手順です。\n確認手順です。")
                self.assertEqual(child.metadata["layout"]["logical_page_labels"], {"1": 1, "2": 2})

    def test_page_header_with_japanese_page_number_is_excluded_but_chapter_header_is_kept(self):
        payload = _viewer_payload(
            [
                _record(1, 1, "Page-header", "操作マニュアル 1ページ"),
                _record(1, 2, "Page-header", "第1章 登録"),
                _record(1, 3, "Text", "登録手順です。"),
                _record(2, 1, "Page-header", "操作マニュアル 2ページ"),
                _record(2, 2, "Page-header", "第2章 確認"),
                _record(2, 3, "Text", "確認手順です。"),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(child_target_chars=300, table_child_target_chars=300, parent_target_chars=1200),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertNotIn("操作マニュアル", child.text)
        self.assertIn("第1章 登録", child.text)
        self.assertIn("第2章 確認", child.text)

    def test_unique_meaningful_page_header_footer_are_kept_in_retrieval_text(self):
        payload = _viewer_payload(
            [
                _record(1, 1, "Page-header", "第1章 登録概要"),
                _record(1, 2, "Text", "登録手順です。"),
                _record(1, 3, "Page-footer", "重要: 修正後に再チェックしてください。"),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertIn("第1章 登録概要", child.text)
        self.assertIn("重要: 修正後に再チェックしてください。", child.text)
        self.assertNotIn("page_context", child.metadata)
        self.assertNotIn("retrieval", child.metadata)

    def test_saved_chunk_run_with_legacy_schema_raises_rechunk_error_from_both_loaders(self):
        # 旧 schema は None（チャンクなし）に丸めず「再チャンキング」の ValueError を伝播させる (#800)。
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            _write_viewer_data(output_dir, "abcdef", [_record(1, 1, "Text", "契約区分の登録手順です。")])
            result = create_chunk_run(output_dir=output_dir, run_id="abcdef", preferred_engine_ids=["docling"], config=ChunkingConfig())
            json_path = Path(result.json_path)
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            payload["schema_version"] = 2
            json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "再チャンキング"):
                load_latest_chunk_run(output_dir, "abcdef")
            with self.assertRaisesRegex(ValueError, "再チャンキング"):
                load_chunk_run_by_id(output_dir, result.chunk_run_id)

    def test_latest_pointer_with_invalid_chunk_run_id_is_treated_as_missing(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            _write_viewer_data(output_dir, "abcdef", [_record(1, 1, "Text", "契約区分の登録手順です。")])
            result = create_chunk_run(output_dir=output_dir, run_id="abcdef", preferred_engine_ids=["docling"], config=ChunkingConfig())
            latest_path = Path(result.latest_path)
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            latest["chunk_run_id"] = "../../abcdef"
            latest_path.write_text(json.dumps(latest), encoding="utf-8")

            self.assertIsNone(load_latest_chunk_run(output_dir, "abcdef"))

    def test_source_identity_lookup_reads_only_matching_runs_via_latest_pointer(self):
        # latest.json の照合項目で不一致 run を除外し、chunks.json は一致した run だけ読む。旧 pointer は従来どおり (#855)。
        from unittest.mock import patch

        from rag_engine.chunking import storage

        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            _write_viewer_data(output_dir, "aaaaaa01", [_record(1, 1, "Text", "契約区分の登録手順です。")])
            _write_viewer_data(output_dir, "bbbbbb01", [_record(1, 1, "Text", "別の文書です。")])
            (output_dir / "bbbbbb01" / "source.pdf").write_bytes(b"%PDF-other")  # 別文書（sha256 が異なる）
            first = create_chunk_run(output_dir=output_dir, run_id="aaaaaa01", preferred_engine_ids=["docling"], config=ChunkingConfig())
            create_chunk_run(output_dir=output_dir, run_id="bbbbbb01", preferred_engine_ids=["docling"], config=ChunkingConfig())
            latest = json.loads(Path(first.latest_path).read_text(encoding="utf-8"))
            self.assertEqual(
                (latest["source_file_sha256"], latest["source_page_count"], latest["source_file_name"]),
                (first.source_file_sha256, first.source_page_count, first.source_file_name),
            )
            with patch("rag_engine.chunking.storage.load_latest_chunk_run", wraps=storage.load_latest_chunk_run) as loader:
                found = storage._load_latest_chunk_run_for_source_identity(
                    output_dir,
                    source_sha256=first.source_file_sha256,
                    source_page_count=first.source_page_count,
                    source_file_name=first.source_file_name,
                )
            self.assertEqual(found.chunk_run_id, first.chunk_run_id)
            self.assertEqual([call.args[1] for call in loader.call_args_list], ["aaaaaa01"])  # 不一致の run は読まない
            # 旧 pointer（照合項目なし）でも照合できる
            latest_path = Path(first.latest_path)
            legacy = {k: v for k, v in latest.items() if not k.startswith("source_")}
            latest_path.write_text(json.dumps(legacy), encoding="utf-8")
            found = storage._load_latest_chunk_run_for_source_identity(
                output_dir,
                source_sha256=first.source_file_sha256,
                source_page_count=first.source_page_count,
                source_file_name=first.source_file_name,
            )
            self.assertEqual(found.chunk_run_id, first.chunk_run_id)

    def test_file_sha256_is_cached_until_the_file_changes(self):
        # 打鍵ごとの候補更新で同じ PDF をハッシュし直さない。書き換われば再計算する (#859)。
        import os
        from unittest.mock import patch

        from rag_engine.chunking import storage

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.pdf"
            path.write_bytes(b"%PDF-1")
            first = storage._optional_file_sha256(path)
            with patch.object(Path, "open", side_effect=AssertionError("再読込した")):
                self.assertEqual(storage._optional_file_sha256(path), first)
            path.write_bytes(b"%PDF-22")
            os.utime(path, ns=(os.stat(path).st_mtime_ns + 1_000_000, os.stat(path).st_mtime_ns + 1_000_000))
            self.assertNotEqual(storage._optional_file_sha256(path), first)
            self.assertEqual(storage._optional_file_sha256(Path(tmp) / "missing.pdf"), "")

    def test_table_row_groups_use_the_table_specific_target(self):
        # 表の分割閾値は table_child_target_chars（既定 3000）。本文の child_target_chars（既定 1000）では割れない (#894)。
        rows = "".join(f"<tr><td>項目{i:03d}</td><td>説明 {i} の内容をここに書きます</td></tr>" for i in range(60))
        html = "<table><tr><th>項目</th><th>説明</th></tr>" + rows + "</table>"
        payload = _viewer_payload([_record(1, 1, "Table", html, raw_type="table", bbox=[10, 10, 90, 90])])

        def groups(config):
            children = build_small_to_big_chunks(payload, source_run_id="abcdef", selected_engine_ids=["docling"], config=config)
            return [
                c
                for c in children
                if c.chunk_level == CHILD_CHUNK_LEVEL and any(r["raw_type"] == "table_row_group" for r in c.source_record_refs)
            ]

        table_text = groups(ChunkingConfig(table_child_target_chars=8000)) or []
        self.assertEqual(table_text, [])  # 十分大きい閾値では 1 child
        self.assertEqual(groups(ChunkingConfig()), [])  # 既定 3000 でも 1 child（この表は約 2,000 字）
        self.assertGreater(len(groups(ChunkingConfig(child_target_chars=1600, table_child_target_chars=900))), 1)

    def test_section_banner_picture_is_not_a_child(self):
        # OCR が「節見出しの再掲 + 著作権表示」だけの帯画像は child にしない。見出し以外の行があれば child になる (#897)。
        def children_for(ocr):
            bbox = [50, 300, 550, 340]
            payload = _viewer_payload(
                [
                    _record(1, 1, "Section-header", "Ａ受注管理 －（３）受注入力"),
                    _record(1, 2, "Text", "①伝票を選びます。②保存ボタンを押します。"),
                    _record(1, 3, "Picture", "", raw_type="picture", bbox=bbox),
                    _record(1, 4, "Picture", ocr, raw_type="picture_ocr_text", bbox=bbox),
                ]
            )
            chunks = build_small_to_big_chunks(payload, source_run_id="abcdef", selected_engine_ids=["docling"], config=ChunkingConfig())
            return [c for c in chunks if c.chunk_level == CHILD_CHUNK_LEVEL]

        banner = children_for("OCR抽出テキスト:\nＡ受注管理 －（３）受注入力\nCopyright© サンプル株式会社 All rights reserved.")
        self.assertFalse(any("Copyright" in c.text for c in banner))
        self.assertEqual(len(banner), 1)
        real = children_for("OCR抽出テキスト:\nＡ受注管理 －（３）受注入力\n登録ボタン\nCopyright© サンプル株式会社 All rights reserved.")
        self.assertTrue(any("登録ボタン" in c.text for c in real))

    def test_short_nonempty_text_is_kept_without_a_length_threshold(self):
        for category in ["Text", "Page-header"]:
            for text in ["a", "ab", "abc", "abcd", "２", "＊"]:
                with self.subTest(category=category, text=text):
                    children, _ = self._children(_viewer_payload([_record(1, 1, category, text)]))
                    self.assertEqual([child.text for child in children], [text])

    def test_short_heading_only_is_preserved(self):
        from rag_engine.chunking.records import _buffer_has_body

        for category in ["Title", "Section-header"]:
            with self.subTest(category=category):
                record = _record(1, 1, category, "章")
                self.assertFalse(_buffer_has_body([record]))
                children, _ = self._children(_viewer_payload([record]))
                self.assertEqual([child.text for child in children], ["章"])

    def test_whitespace_does_not_supply_body(self):
        from rag_engine.chunking.records import _buffer_has_body

        for text in ["", " ", "　", "\n\t　 "]:
            with self.subTest(text=text):
                self.assertFalse(_buffer_has_body([_record(1, 1, "Text", text)]))
                children, _ = self._children(_viewer_payload([_record(1, 1, "Text", text)]))
                self.assertEqual(children, [])

    def test_short_text_before_visual_is_kept_as_body(self):
        bbox = [50, 300, 550, 500]
        payload = _viewer_payload(
            [
                _record(1, 1, "Section-header", "（１）登録"),
                _record(1, 2, "Text", "２"),
                _record(1, 3, "Picture", "", raw_type="picture", bbox=bbox),
                _record(1, 4, "Picture", "OCR抽出テキスト:\n保存ボタン", raw_type="picture_ocr_text", bbox=bbox),
                _record(1, 5, "Text", "保存ボタンを押します。"),
                _record(1, 6, "Page-footer", "以上"),
            ]
        )
        chunks = build_small_to_big_chunks(payload, source_run_id="abcdef", selected_engine_ids=["docling"], config=ChunkingConfig())
        texts = [c.text for c in chunks if c.chunk_level == CHILD_CHUNK_LEVEL]
        self.assertTrue(any("（１）登録\n２" in text for text in texts))
        self.assertTrue(any("保存ボタン" in text for text in texts))
        self.assertTrue(any("以上" in text for text in texts))

    def test_retrieval_profile_is_computed_once_per_chunk(self):
        # child は検索文確定後の 1 回、親は生成時の 1 回だけ問い合わせ profile を推定する (#867)。
        from unittest.mock import patch

        from rag_engine.retrieval.inquiry_conditions import build_inquiry_chunk_metadata

        payload = _viewer_payload(
            [
                _record(1, 1, "Section-header", "（１）登録"),
                _record(1, 2, "Text", "①伝票を選びます。②保存ボタンを押します。"),
                _record(1, 3, "Text", "③確認ボタンを押します。"),
            ]
        )
        # child は builder（検索文確定後）、親は metadata（生成時）が呼ぶ (#891)。
        with (
            patch("rag_engine.chunking.builder.build_inquiry_chunk_metadata", wraps=build_inquiry_chunk_metadata) as build_children,
            patch("rag_engine.chunking.metadata.build_inquiry_chunk_metadata", wraps=build_inquiry_chunk_metadata) as build_parents,
        ):
            chunks = build_small_to_big_chunks(payload, source_run_id="abcdef", selected_engine_ids=["docling"], config=ChunkingConfig())
        children = [c for c in chunks if c.chunk_level == CHILD_CHUNK_LEVEL]
        parents = [c for c in chunks if c.chunk_level == PARENT_CHUNK_LEVEL]
        self.assertEqual(build_children.call_count, len(children))
        self.assertEqual(build_parents.call_count, len(parents))
        self.assertTrue(all("retrieval_profile" in c.metadata for c in children))

    def test_create_chunk_run_writes_json_jsonl_and_latest_pointer(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp)
            _write_viewer_data(output_dir, "abcdef", [_record(1, 1, "Text", "契約区分の登録手順です。")])

            result = create_chunk_run(
                output_dir=output_dir,
                run_id="abcdef",
                preferred_engine_ids=["docling"],
                config=ChunkingConfig(),
            )
            loaded = load_latest_chunk_run(output_dir, "abcdef")

            self.assertEqual(result.source_run_id, "abcdef")
            self.assertEqual(loaded.chunk_run_id, result.chunk_run_id)
            self.assertEqual(loaded.chunks[0].source_file_name, "manual.pdf")
            self.assertTrue(Path(result.json_path).exists())
            self.assertTrue(Path(result.jsonl_path).exists())
            self.assertTrue(Path(result.latest_path).exists())
            payload = json.loads(Path(result.json_path).read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], CHUNK_SCHEMA_VERSION)
            self.assertEqual(payload["strategy"], CHUNK_STRATEGY)
            self.assertTrue(payload["active"])
            self.assertEqual(payload["source_page_count"], 1)
            self.assertEqual(payload["summary"]["child_count"], 1)
            self.assertEqual(
                payload["contracts"]["chunk_metadata_schema_version"],
                CHUNK_METADATA_SCHEMA_VERSION,
            )
            self.assertEqual(payload["contracts"]["search_text_schema_version"], SEARCH_TEXT_SCHEMA_VERSION)
            self.assertEqual(
                payload["contracts"]["inquiry_chunk_metadata_schema_version"],
                INQUIRY_CHUNK_METADATA_SCHEMA_VERSION,
            )
            self.assertEqual(payload["contracts"]["inquiry_profile_contract_hash"], inquiry_profile_contract_hash())
            self.assertRegex(payload["contracts"]["fingerprint"], r"^[0-9a-f]{64}$")
            self.assertEqual(
                payload["summary"]["retrieval_text_audit"],
                audit_chunk_retrieval_text(
                    result.chunks,
                    child_search_text_max_chars=result.config.child_search_text_max_chars,
                ),
            )
            from rag_engine.chunking import _chunk_from_payload

            jsonl_chunks = [
                _chunk_from_payload(json.loads(line)) for line in Path(result.jsonl_path).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([chunk.chunk_id for chunk in jsonl_chunks], [chunk.chunk_id for chunk in result.chunks])
            latest = json.loads(Path(result.latest_path).read_text(encoding="utf-8"))
            self.assertTrue(latest["active"])

    def test_load_latest_chunk_run_for_source_matches_checksum_page_count_and_name(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "runs"
            source_path = Path(tmp) / "manual.pdf"
            source_path.write_bytes(b"matching source")
            _write_viewer_data(output_dir, "abcdef", [_record(1, 1, "Text", "登録手順です。")])
            (output_dir / "abcdef" / "source.pdf").write_bytes(source_path.read_bytes())
            result = create_chunk_run(
                output_dir=output_dir,
                run_id="abcdef",
                preferred_engine_ids=["docling"],
                config=ChunkingConfig(),
            )

            loaded = load_latest_chunk_run_for_source(output_dir, source_path, 1)

            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.chunk_run_id, result.chunk_run_id)
            self.assertIsNone(load_latest_chunk_run_for_source(output_dir, source_path, 2))
            other_path = Path(tmp) / "other.pdf"
            other_path.write_bytes(source_path.read_bytes())
            self.assertIsNone(load_latest_chunk_run_for_source(output_dir, other_path, 1))

    def test_load_chunk_run_by_id_and_preview_source_restore_existing_chunking(self):
        with TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "runs"
            source_path = Path(tmp) / "manual.pdf"
            source_path.write_bytes(b"matching source")
            _write_viewer_data(output_dir, "abcdef", [_record(1, 1, "Text", "登録手順です。")])
            (output_dir / "abcdef" / "source.pdf").write_bytes(source_path.read_bytes())
            result = create_chunk_run(
                output_dir=output_dir,
                run_id="abcdef",
                preferred_engine_ids=["docling"],
                config=ChunkingConfig(),
            )

            _write_viewer_data(output_dir, "fedcba", [_record(1, 1, "Text", "登録手順です。")])
            (output_dir / "fedcba" / "source.pdf").write_bytes(source_path.read_bytes())

            by_id = load_chunk_run_by_id(output_dir, result.chunk_run_id)
            by_preview_source = load_latest_chunk_run_for_run_source(output_dir, "fedcba")
            by_combined_loader = load_latest_or_source_chunk_run(output_dir, "fedcba")

            self.assertEqual(by_id.chunk_run_id, result.chunk_run_id)
            self.assertEqual(by_preview_source.chunk_run_id, result.chunk_run_id)
            self.assertEqual(by_combined_loader.source_run_id, "abcdef")

    def test_chunk_table_rows_expose_metadata_columns(self):
        payload = _viewer_payload([_record(1, 1, "Text", "契約区分の登録手順です。")])
        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )

        rows = chunk_table_rows(chunks)

        self.assertEqual(rows[0][0], "chunk-docling-c000001")
        self.assertEqual(rows[0][1], CHILD_CHUNK_LEVEL)
        self.assertIn("p.1 #1", rows[0][6])
        self.assertIn("source_categories", rows[0][11])
        self.assertIn('"active": true', rows[0][11])
        self.assertNotIn('"file_name"', rows[0][11])

    def test_new_chunks_include_best_practice_metadata(self):
        payload = _viewer_payload([_record(1, 1, "Text", "契約区分の登録手順です。")])

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
            created_at_utc="2026-09-01T00:00:00+00:00",
            source_file_sha256="source-sha",
            source_page_count=1,
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertTrue(child.metadata["active"])
        self.assertFalse(child.metadata["atomic"])
        self.assertEqual(child.metadata["layout"]["display_regions"][0]["page"], 1)
        self.assertEqual(child.metadata["layout"]["display_regions"][0]["boxes"][0]["bbox"], [0.0, 40.0, 10.0, 50.0])
        self.assertEqual(child.metadata["document"]["classification"]["large_category"], "")
        self.assertEqual(set(child.metadata["document"]["classification"]), {"large_category", "middle_category", "small_category"})
        self.assertNotIn("schema_version", child.metadata["retrieval_profile"])
        self.assertIn("operation_steps", child.metadata["retrieval_profile"]["active_profiles"])
        self.assertRegex(child.metadata["content_hash"], r"^[0-9a-f]{64}$")
        self.assertTrue(child.source_record_refs)
        self.assertEqual(child.metadata["schema_version"], 4)
        # v4: 文書属性は document、位置系は layout、問い合わせ profile は retrieval_profile (#814)。
        self.assertEqual(
            set(child.metadata),
            {
                "schema_version",
                "active",
                "atomic",
                "content_hash",
                "document",
                "section_path",
                "section_path_sources",
                "source_categories",
                "layout",
                "retrieval_profile",
            },
        )
        self.assertEqual(set(child.metadata["layout"]), {"display_regions", "native_text_ranges"})
        self.assertEqual(child.metadata["section_path_sources"], ["section_header"] * len(child.metadata["section_path"]))

    def test_section_path_sources_record_where_each_heading_came_from(self):
        # 見出しの出所を section_path と同じ長さで残す。Docling の分類、柱の初出、経路 caption、本文の再出現を区別する (#814)。
        payload = _viewer_payload(
            [
                _record(1, 1, "Section-header", "２．顧客の登録"),
                _record(1, 2, "Caption", "〔マスタ管理⇒顧客管理⇒基本設定〕", raw_type="caption"),
                _record(1, 3, "Text", "①顧客コードを入力します。"),
                _record(2, 1, "Text", "２．顧客の登録"),  # 本文側の再出現（節は変わらない）
                _record(2, 2, "Text", "②保存ボタンを押します。"),
            ]
        )
        chunks = build_small_to_big_chunks(payload, source_run_id="abcdef", selected_engine_ids=["docling"], config=ChunkingConfig())
        for chunk in chunks:
            self.assertEqual(len(chunk.metadata["section_path_sources"]), len(chunk.metadata["section_path"]))
        child = next(c for c in chunks if c.chunk_level == CHILD_CHUNK_LEVEL and "顧客コード" in c.text)
        self.assertEqual(child.metadata["section_path"], ["２．顧客の登録", "〔マスタ管理⇒顧客管理⇒基本設定〕"])
        self.assertEqual(child.metadata["section_path_sources"], ["section_header", "route_caption"])
        parent = next(c for c in chunks if c.chunk_level == PARENT_CHUNK_LEVEL)
        self.assertEqual(parent.metadata["section_path_sources"], ["section_header", "route_caption"])

    def test_metadata_omits_fields_nobody_reads(self):
        # boxes.raw_type は category と重複、list_context の text / bbox は本文・display_regions と重複 (#812)。
        payload = _viewer_payload(
            [
                _record(1, 1, "List-item", "1. 伝票を選びます。", raw={"list_item": {"ordinal": 1, "level": 1, "marker": "1."}}),
                _record(1, 2, "List-item", "2. 保存します。", raw={"list_item": {"ordinal": 2, "level": 1, "marker": "2."}}),
            ]
        )
        chunks = build_small_to_big_chunks(payload, source_run_id="abcdef", selected_engine_ids=["docling"], config=ChunkingConfig())
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)
        self.assertEqual(
            set(child.metadata["layout"]["display_regions"][0]["boxes"][0]), {"record_id", "seq_no", "category", "bbox", "text_preview"}
        )
        self.assertEqual(
            set(child.metadata["layout"]["list_context"][0]),
            {"record_id", "page", "seq_no", "ordinal", "level", "marker", "previous_record_id", "next_record_id"},
        )

    def test_child_retrieval_text_gets_context_without_changing_display_regions(self):
        payload = _viewer_payload(
            [
                _record(1, 1, "Section-header", "Ⅰ. 倉庫マスタ登録"),
                _record(1, 2, "Text", "拠点倉庫名を変更し、F5実行で登録します。", bbox=[10, 20, 50, 60]),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(child_target_chars=120, table_child_target_chars=120, parent_target_chars=500),
            classification={
                "large_category": "在庫管理",
                "middle_category": "操作説明書",
                "small_category": "倉庫連携",
                "source": "manual",
            },
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)
        parent = next(chunk for chunk in chunks if chunk.chunk_level == PARENT_CHUNK_LEVEL)
        self.assertEqual(parent.retrieval_text, parent.text)
        self.assertNotIn("Source file:", child.text)
        self.assertIn("Source file: manual.pdf", child.retrieval_text)
        self.assertIn("Classification: 在庫管理 / 操作説明書 / 倉庫連携", child.retrieval_text)
        self.assertIn("Section path: Ⅰ. 倉庫マスタ登録", child.retrieval_text)
        self.assertNotIn(parent.chunk_id, child.retrieval_text)
        self.assertIn("Parent summary:", child.retrieval_text)
        self.assertIn("Child text:", child.retrieval_text)
        self.assertIn("拠点倉庫名を変更し、F5実行で登録します。", child.retrieval_text)
        self.assertEqual(child.metadata["layout"]["display_regions"][0]["boxes"][0]["bbox"], [0.0, 40.0, 10.0, 50.0])
        self.assertEqual(child.metadata["layout"]["display_regions"][0]["boxes"][1]["bbox"], [10.0, 20.0, 50.0, 60.0])
        self.assertNotIn("retrieval", child.metadata)

    def test_search_text_excludes_temporary_paths_and_random_upload_hashes(self):
        random_hash = "ca4bd4efc066c65552b9897151b6744afacd8f40bc8b4a8b76c6aa2679ea0beb"
        payload = _viewer_payload([_record(1, 1, "Text", "配送連携の説明です。")])
        payload["pdf_name"] = f"/tmp/gradio/{random_hash}/98_配送連携.pdf"

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
            classification={
                "small_category": "配送連携",
                "source": "path_default",
                "path_parts": ["tmp", "gradio", random_hash, "98_配送連携.pdf"],
            },
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertIn("98_配送連携.pdf", child.retrieval_text)
        self.assertNotIn("tmp/gradio", child.retrieval_text)
        self.assertNotIn(random_hash, child.retrieval_text)
        self.assertEqual(
            set(child.metadata["document"]["classification"]),
            {"large_category", "middle_category", "small_category"},
        )

    def test_v3_metadata_reduces_representative_v2_record_by_at_least_sixty_percent(self):
        payload = _viewer_payload(
            [
                _record(2, 1, "Section-header", "（２）連携先の一覧"),
                _record(2, 2, "Text", "在庫管理システムと連携できるデータの種類を説明します。"),
            ]
        )
        child = next(
            chunk
            for chunk in build_small_to_big_chunks(
                payload,
                source_run_id="abcdef",
                selected_engine_ids=["docling"],
                config=ChunkingConfig(),
                classification={"small_category": "配送連携"},
            )
            if chunk.chunk_level == CHILD_CHUNK_LEVEL
        )
        compact = child.metadata
        regions = compact.get("display_regions", [])
        legacy = {
            **compact,
            "schema_version": 2,
            "file_name": "98_配送連携.pdf",
            "lifecycle_status": "active",
            "created_at_utc": "2026-09-14T10:30:54+00:00",
            "source_file_sha256": "a" * 64,
            "source_page_count": 4,
            "parser": {"engine_id": "docling", "engine_label": "Docling", "source_run_id": "abcdef"},
            "hierarchy": {
                "chunk_level": "child",
                "parent_chunk_id": "chunk-docling-p000001",
                "child_chunk_ids": [],
                "previous_child_chunk_id": "chunk-docling-c000001",
                "next_child_chunk_id": "chunk-docling-c000003",
                "sibling_index": 2,
                "sibling_count": 5,
            },
            "provenance": {
                "display_regions": regions,
                "logical_page_labels": {"2": 2},
                "page_start": 2,
                "page_end": 2,
                "source_categories": ["Section-header", "Text"],
                "source_seq_ranges": [{"page": 2, "seq_start": 1, "seq_end": 2}],
            },
            "content_flags": {
                "atomic": False,
                "contains_caption": False,
                "contains_footnote": False,
                "contains_form_field": False,
                "contains_formula": False,
                "contains_image_evidence": False,
                "contains_list": False,
                "contains_page_boilerplate": True,
                "contains_picture": False,
                "contains_structured_table": False,
                "contains_table": False,
                "content_type": "heading_text",
                "language": "und",
                "oversized_atomic": False,
            },
            "retrieval": {
                "base_text_hash": compact["content_hash"],
                "body_char_count": len(child.text),
                "body_preserved": True,
                "body_truncated": False,
                "char_count": len(child.text),
                "child_search_text_max_chars": 2200,
                "contextual_search_text_enabled": True,
                "embedding_modes": ["text"],
                "excluded_layout_roles": ["page_number"],
                "image_embedding_status": "not_applicable",
                "layout_context_types": ["page_boilerplate"],
                "search_text_char_count": len(child.retrieval_text),
                "search_text_components": ["source_file", "classification", "page_span", "section_path", "parent_blurb", "child_text"],
                "search_text_context_max_chars": 700,
                "search_text_hash": "b" * 64,
                "search_text_over_target": False,
                "search_text_schema_version": 5,
                "search_text_token_estimate": 100,
                "section_path": compact["section_path"],
                "table_chunking_strategy": "not_applicable",
                "token_estimate": 50,
            },
            "contains_image_evidence": False,
            "contains_picture": False,
            "contains_table": False,
            "oversized_atomic": False,
            "parent_chunk_id": "chunk-docling-p000001",
            "previous_child_chunk_id": "chunk-docling-c000001",
            "next_child_chunk_id": "chunk-docling-c000003",
            "sibling_index": 2,
            "sibling_count": 5,
            "child_chunk_ids": [],
        }
        before = len(json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        after = len(json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

        self.assertLessEqual(after, before * 0.4, f"before={before}, after={after}")

    def test_search_text_ignores_parent_identity_and_rejects_legacy_metadata(self):
        from dataclasses import replace

        from rag_engine.chunking import _child_search_text, _chunk_from_payload

        chunks = build_small_to_big_chunks(
            _viewer_payload([_record(1, 1, "Text", "English source text.")]),
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)
        parent = next(chunk for chunk in chunks if chunk.chunk_level == PARENT_CHUNK_LEVEL)
        changed, components = _child_search_text(
            child,
            parent=replace(parent, chunk_id="unrelated-parent-id"),
            config=ChunkingConfig(),
        )
        self.assertEqual(changed, child.retrieval_text)
        self.assertNotIn("parent_chunk", components)
        restored = _chunk_from_payload(child.to_dict())
        self.assertEqual(restored.source_record_refs, child.source_record_refs)
        self.assertEqual(restored.metadata["layout"]["display_regions"], child.metadata["layout"]["display_regions"])
        legacy = child.to_dict()
        legacy["metadata"]["schema_version"] = 2
        with self.assertRaisesRegex(ValueError, "再チャンキング"):
            _chunk_from_payload(legacy)

    def test_long_atomic_body_survives_embedding_budget_and_audit_detects_legacy_loss(self):
        body = "説明" * 2000 + "末尾の例外条件"
        chunks = build_small_to_big_chunks(
            _viewer_payload([_record(1, 1, "Table", body, raw_type="table")]),
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)
        self.assertIn(child.text, child.retrieval_text)
        self.assertTrue(child.retrieval_text.endswith("末尾の例外条件"))
        audit = audit_chunk_retrieval_text(chunks, child_search_text_max_chars=2200)
        self.assertEqual(audit["over_target_count"], 1)
        self.assertEqual(audit["body_truncated_count"], 0)
        child.retrieval_text = " ".join(child.text.split())
        self.assertEqual(audit_chunk_retrieval_text(chunks)["body_truncated_count"], 0)
        child.retrieval_text = child.text[:1800]
        self.assertEqual(audit_chunk_retrieval_text(chunks)["body_truncated_rate"], 1.0)

    def test_table_below_parent_limit_splits_with_headers_and_keeps_long_row_tail(self):
        tail = "最終行の例外"
        table = (
            "<table><tr><th>項目</th><th>説明</th></tr>"
            + "".join(f"<tr><td>行{i}</td><td>{'説明' * 200}</td></tr>" for i in range(4))
            + f"<tr><td>最終行</td><td>{tail}</td></tr></table>"
        )
        self.assertLess(len(table), 3000)
        # 既定の表閾値（3000）では 1 child になるため、行グループの性質は閾値 1000 で確認する (#894)。
        chunks = build_small_to_big_chunks(
            _viewer_payload([_record(1, 1, "Table", table, raw_type="table")]),
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(table_child_target_chars=1000),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]
        self.assertGreater(len(children), 1)
        self.assertTrue(all("列見出し: 項目 / 説明" in child.text for child in children))
        self.assertTrue(all(child.text in child.retrieval_text for child in children))
        self.assertIn(tail, children[-1].retrieval_text)

    def test_contextual_search_text_can_be_disabled(self):
        payload = _viewer_payload(
            [
                _record(1, 1, "Section-header", "画面説明"),
                _record(1, 2, "Text", "契約区分を登録します。"),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(contextual_search_text_enabled=False),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)
        self.assertEqual(child.retrieval_text, child.text)
        self.assertNotIn("retrieval", child.metadata)

    def test_chunks_include_selected_document_classification(self):
        payload = _viewer_payload([_record(1, 1, "Text", "倉庫連携の操作説明です。")])

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
            classification={
                "large_category": "在庫管理",
                "middle_category": "操作説明書",
                "small_category": "倉庫連携",
                "source": "manual",
            },
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)
        payload = chunk_payload(loadless_result(chunks=chunks, source_run_id="abcdef", source_file_name="manual.pdf"))
        loaded = chunk_result_from_payload(payload)

        self.assertEqual(child.metadata["document"]["classification"]["large_category"], "在庫管理")
        self.assertEqual(child.metadata["document"]["classification"]["middle_category"], "操作説明書")
        self.assertEqual(loaded.chunks[0].metadata["document"]["classification"]["small_category"], "倉庫連携")

    def test_chunks_use_analysis_payload_classification_by_default(self):
        payload = _viewer_payload([_record(1, 1, "Text", "倉庫連携の操作説明です。")])
        payload["classification"] = {
            "large_category": "在庫管理",
            "middle_category": "操作説明書",
            "small_category": "倉庫連携",
            "source": "manual",
        }

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        child = next(chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL)

        self.assertEqual(child.metadata["document"]["classification"]["large_category"], "在庫管理")
        self.assertEqual(child.metadata["document"]["classification"]["middle_category"], "操作説明書")
        self.assertEqual(child.metadata["document"]["classification"]["small_category"], "倉庫連携")

    def test_parent_child_relationships_live_only_in_authoritative_chunk_fields(self):
        payload = _viewer_payload(
            [
                _record(1, 1, "Text", "最初の手順です。" * 8),
                _record(1, 2, "Text", "次の手順です。" * 8),
                _record(1, 3, "Text", "最後の手順です。" * 8),
            ]
        )

        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(
                child_target_chars=30, table_child_target_chars=30, parent_target_chars=1000, parent_max_pages=2, parent_max_children=8
            ),
        )
        children = [chunk for chunk in chunks if chunk.chunk_level == CHILD_CHUNK_LEVEL]
        parent = next(chunk for chunk in chunks if chunk.chunk_level == PARENT_CHUNK_LEVEL)

        self.assertGreaterEqual(len(children), 3)
        self.assertTrue(all(child.parent_chunk_id == parent.chunk_id for child in children))
        self.assertEqual(parent.child_chunk_ids, [child.chunk_id for child in children])
        self.assertTrue(all("hierarchy" not in chunk.metadata for chunk in chunks))

    def test_chunk_payload_rejects_missing_v3_contracts(self):
        payload = _viewer_payload([_record(1, 1, "Text", "契約区分の登録手順です。")])
        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        result_payload = chunk_payload(
            loadless_result(
                chunks=chunks,
                source_run_id="abcdef",
                source_file_name="manual.pdf",
            )
        )
        result_payload.pop("contracts")

        with self.assertRaisesRegex(ValueError, "再チャンキング"):
            chunk_result_from_payload(result_payload)

    def test_chunk_payload_rejects_unknown_metadata_fields(self):
        payload = _viewer_payload([_record(1, 1, "Picture", "画像説明", raw_type="picture", bbox=[10, 20, 70, 80])])
        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        result_payload = chunk_payload(
            loadless_result(
                chunks=chunks,
                source_run_id="abcdef",
                source_file_name="manual.pdf",
            )
        )
        result_payload["chunks"][0]["metadata"]["file_name"] = "manual.pdf"

        with self.assertRaisesRegex(ValueError, "再チャンキング"):
            chunk_result_from_payload(result_payload)

    def test_chunk_payload_rejects_outer_v2_contract(self):
        payload = _viewer_payload([_record(1, 1, "Text", "本文")])
        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        result_payload = chunk_payload(
            loadless_result(
                chunks=chunks,
                source_run_id="abcdef",
                source_file_name="manual.pdf",
            )
        )
        result_payload["schema_version"] = 2

        with self.assertRaisesRegex(ValueError, "再チャンキング"):
            chunk_result_from_payload(result_payload)

    def test_chunking_config_rejects_fractional_values_instead_of_truncating(self):
        # int(12.7) の黙った切り捨てを許さない。整数値の float は従来どおり受け付ける (#802)。
        default = ChunkingConfig().child_target_chars
        self.assertEqual(ChunkingConfig(child_target_chars=float(default)).validate().child_target_chars, default)
        with self.assertRaisesRegex(ValueError, "整数で指定"):
            ChunkingConfig(child_target_chars=default + 0.5).validate()
        with self.assertRaisesRegex(ValueError, "整数で指定"):
            ChunkingConfig(parent_max_pages=True).validate()

    def test_chunking_config_excludes_answer_generation_settings(self):
        payload = ChunkingConfig().to_dict()

        self.assertNotIn("retrieval_top_k", payload)
        self.assertNotIn("neighbor_child_count", payload)

    def test_legacy_chunk_payload_ignores_answer_generation_settings(self):
        payload = _viewer_payload([_record(1, 1, "Text", "契約区分の登録手順です。")])
        chunks = build_small_to_big_chunks(
            payload,
            source_run_id="abcdef",
            selected_engine_ids=["docling"],
            config=ChunkingConfig(),
        )
        result_payload = chunk_payload(
            loadless_result(
                chunks=chunks,
                source_run_id="abcdef",
                source_file_name="manual.pdf",
            )
        )
        result_payload["config"]["retrieval_top_k"] = 3
        result_payload["config"]["neighbor_child_count"] = 2

        result = chunk_result_from_payload(result_payload)

        self.assertEqual(result.config, ChunkingConfig())
        self.assertNotIn("retrieval_top_k", result.config.to_dict())
        self.assertNotIn("neighbor_child_count", result.config.to_dict())


def _unit(chunk):
    from rag_engine.chunking import _section_unit

    return _section_unit(chunk.metadata["section_path"])


def _children_by_parent(children, parents):
    return [[c for c in children if c.chunk_id in p.child_chunk_ids] for p in parents]


def _viewer_payload(records):
    return {
        "run_id": "abcdef",
        "pdf_name": "manual.pdf",
        "records": records,
        "engines": [{"engine": "docling", "label": "Docling"}],
    }


def _write_viewer_data(output_dir: Path, run_id: str, records: list[dict]) -> None:
    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "viewer-data.json").write_text(json.dumps(_viewer_payload(records), ensure_ascii=False), encoding="utf-8")


def loadless_result(*, chunks, source_run_id: str, source_file_name: str) -> ChunkingResult:
    return ChunkingResult(
        source_run_id=source_run_id,
        chunk_run_id="chunkrun",
        source_file_name=source_file_name,
        selected_engine_ids=["docling"],
        config=ChunkingConfig(),
        config_hash="hash",
        created_at_utc="2026-09-01T00:00:00+00:00",
        active=True,
        source_file_sha256="",
        source_page_count=1,
        chunks=chunks,
        json_path="",
        jsonl_path="",
        latest_path="",
    )


def _record(page, seq_no, category, text, *, engine="docling", raw_type="", bbox=None, raw=None):
    return {
        "id": f"{engine}-p{page}-{seq_no}",
        "engine": engine,
        "page": page,
        "seq_no": seq_no,
        # 既定は本文位置（ページ上端・下端の帯は柱の判定に使うため、既定の record を帯に置かない）。
        "bbox": bbox or [0, 40, 10, 50],
        "coord_system": "image_top_left",
        "page_width": 100,
        "page_height": 100,
        "category": category,
        "text": text,
        "raw_type": raw_type or category.lower(),
        "raw": raw or {},
    }


if __name__ == "__main__":
    unittest.main()


def test_metadata_coordinates_are_rounded_without_touching_other_numbers():
    """座標だけを小数1桁にする。ページ番号や順序、入力の辞書は変えない。"""
    from rag_engine.chunking import _rounded_coordinates

    source = [
        {
            "page": 3,
            "boxes": [
                {
                    "bbox": [155.7710034780906, 393.66399963815786, 3353.929648024301, 890.4669781885613],
                    "seq_no": 19,
                    "text_preview": "1.25倍",
                }
            ],
            "context_bbox": [1.04, 2.06, 3, 4.0],
        }
    ]
    rounded = _rounded_coordinates(source)
    assert rounded[0]["boxes"][0]["bbox"] == [155.8, 393.7, 3353.9, 890.5]
    assert rounded[0]["context_bbox"] == [1.0, 2.1, 3.0, 4.0]
    assert (rounded[0]["page"], rounded[0]["boxes"][0]["seq_no"], rounded[0]["boxes"][0]["text_preview"]) == (3, 19, "1.25倍")
    assert source[0]["boxes"][0]["bbox"][0] == 155.7710034780906


def test_single_letter_series_headings_are_not_roman_numeral_chapters():
    """「I 【画面説明】」「V 【操作説明】」は英字系列の見出しで、ローマ数字の章ではない (#747)。"""
    from rag_engine.chunking import _LETTERED_LEVEL, _heading_level

    assert [_heading_level(f"{letter} 【画面説明】") for letter in "AIVX"] == [_LETTERED_LEVEL] * 4
    assert [_heading_level(h) for h in ("Ⅰ 月次締め処理", "Ⅴ．締め処理", "Ⅲ 【補足】", "X. 付録")] == [1, 1, 1, 1]


def test_search_text_drops_screen_example_values_but_keeps_them_in_text():
    """生成説明の画面例の値の文は検索用テキストから除き、回答用の text には残す (#753)。"""
    from rag_engine.chunking import DocumentChunk, _without_screen_examples, audit_chunk_retrieval_text

    described = (
        "回答用本文: 更新確認ダイアログが表示される。画面例では締め対象年月=令和6年12月、出荷予定年月=令和6年9月。"
        "更新確認のメッセージで「はい」を選択する。\n画面例: コード=E2。\n入力例として 001 を入れる。"
    )
    stripped = _without_screen_examples(described)
    assert "出荷予定年月" not in stripped and "画面例" not in stripped and "001" not in stripped
    assert "更新確認のメッセージで「はい」を選択する。" in stripped
    child = DocumentChunk(
        chunk_id="c1",
        chunk_level="child",
        chunk_seq=1,
        parent_chunk_id="p1",
        child_chunk_ids=[],
        text=described,
        retrieval_text="Child text:\n" + stripped,
        char_count=len(described),
        token_estimate=1,
        source_run_id="r",
        source_file_name="a.pdf",
        source_engine_id="docling",
        source_engine_label="Docling",
        page_start=1,
        page_end=1,
        source_seq_ranges=[],
        source_record_refs=[],
        metadata={},
    )
    assert audit_chunk_retrieval_text([child])["body_truncated_count"] == 0  # 意図的な除去は欠落に数えない


def test_screen_value_lines_are_removed_without_relying_on_a_prefix():
    """接頭辞が付かなくても、行ラベルで画面の値を検索用テキストから外す (#778)。"""
    from rag_engine.chunking import _without_screen_examples

    text = "\n".join(
        [
            "■ 可視情報",
            "項目: 区分 / 名称",
            "値: 区分=01 / 電話番号=0123-45-6789",
            "コード・エラー: E2",
            "■ 構造",
            "フォームの項目:",
            "- 区分 = 01 [enabled]",
            "- 名称 = サンプル [enabled]",
            "フォームの配置: 左側に入力欄。",
        ]
    )
    stripped = _without_screen_examples(text)

    assert "0123-45-6789" not in stripped and "サンプル" not in stripped
    assert "区分=01" not in stripped and "区分 = 01" not in stripped
    # 項目名・コード・配置は検索語として残す。
    assert "項目: 区分 / 名称" in stripped
    assert "コード・エラー: E2" in stripped
    assert "フォームの配置: 左側に入力欄。" in stripped


def test_screen_example_removal_keeps_following_items_on_the_same_line():
    """ " / " 連結の行では例示値の項目だけを除き、後続のコードや項目名を残す (#766)。"""
    from rag_engine.chunking import _without_screen_examples

    line = (
        "画面/メニュー: 〔マスタ管理⇒基本設定〕 / 基本設定画面 / display example: 対象年月=2026年1月"
        " / エラーコード E2 / input example: 区分=01 / 印刷区分"
    )
    stripped = _without_screen_examples(line)
    assert "対象年月=2026年1月" not in stripped and "区分=01" not in stripped
    assert "エラーコード E2" in stripped and "印刷区分" in stripped and "基本設定画面" in stripped
    assert " /  / " not in stripped
