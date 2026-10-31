"""context builder の挙動を保護するテスト。"""

import unittest
from dataclasses import replace

from rag_engine.generation.answering import AnswerRecord
from rag_engine.retrieval.context_builder import (
    ContextBuildRequest,
    build_chunk_context_bundle,
    context_bundle_from_records,
    record_has_image_evidence,
)


class ContextBuilderTests(unittest.TestCase):
    def test_visual_context_recovers_explicit_text_across_parent_boundary(self):
        source = dict(source_run_id="parse-a", source="manual.pdf")
        picture = replace(
            _chunk_record("image", "parent", 1, "", "画像の説明", chunk_uid="v1:image", **source),
            source_record_refs=(
                {
                    "record_id": "picture-1",
                    "page": 1,
                    "category": "Picture",
                    "vision_context_record_refs": [
                        {"record_id": "instruction", "page": 1, "category": "List-item", "partially_visible": False}
                    ],
                },
            ),
        )
        child = _chunk_record("hit", "child", 1, "image", "画像", chunk_uid="v1:hit", parent_chunk_uid="v1:image", **source)
        body = replace(
            _chunk_record("body", "parent", 2, "", "希望日を入力して右側のアイコンを押し、保存する。", chunk_uid="v1:body", **source),
            source_record_refs=({"record_id": "instruction", "page": 1, "category": "List-item"},),
        )
        unrelated = replace(
            body,
            id="other",
            chunk_id="other",
            chunk_uid="v1:other",
            source_record_refs=({"record_id": "other-text", "page": 1, "category": "Text"},),
        )

        def build(anchor=picture, candidate=body, limit=1):
            return build_chunk_context_bundle(
                ContextBuildRequest(
                    question="希望日を追加したい",
                    ranked_children=[child],
                    active_records=[child, anchor, candidate, unrelated],
                    top_k=1,
                    neighbor_child_count=1,
                    max_records=1,
                    support_record_limit=limit,
                    max_chars=5000,
                )
            )

        bundle = build()
        self.assertEqual([r.id for r in bundle.records], ["image", "body"])
        self.assertIn("希望日を入力", bundle.text)
        self.assertEqual(bundle.supporting_evidence[0].reason, "visual_context_source")
        self.assertEqual(len(build(limit=0).records), 1)
        # ID が同じでも文書・解析版・チャンク版・ページが異なる本文は補完しない。
        for changes in (
            {"source": "other.pdf"},
            {"source_run_id": "parse-b"},
            {"chunk_uid": "v2:body"},
            {"source_record_refs": ({"record_id": "instruction", "page": 2, "category": "List-item"},)},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(len(build(candidate=replace(body, **changes)).records), 1)
        for visibility in (True, None):
            ref = dict(picture.source_record_refs[0])
            ref["vision_context_record_refs"] = [dict(ref["vision_context_record_refs"][0], partially_visible=visibility)]
            self.assertEqual(len(build(anchor=replace(picture, source_record_refs=(ref,))).records), 1)

    def test_neighbors_from_another_page_scope_are_not_reused(self):
        scope = '[{"document_id":"doc-1","page_start":2,"page_end":2}]'
        anchor = _chunk_record("hit", "child", 2, "", "指定した本文", metadata={"chunk_strategy": "fixed", "context_page_ranges": scope})
        other = _chunk_record("outside", "child", 3, "", "別の範囲の本文", metadata={"chunk_strategy": "fixed", "context_page_ranges": ""})
        result = build_chunk_context_bundle(
            ContextBuildRequest(
                question="合成の質問",
                ranked_children=[anchor],
                active_records=[anchor, other],
                top_k=1,
                neighbor_child_count=3,
                max_records=5,
                max_chars=5000,
            )
        )
        self.assertNotIn("別の範囲の本文", result.text)
        self.assertIn("指定した本文", result.text)

    def test_build_chunk_context_uses_parent_once_and_child_tree_roles(self):
        long_parent_text = "parent evidence " + ("full synthesis " * 120) + "tail marker"
        records = [
            _chunk_record("before", "child", 1, "parent-1", "前の注意"),
            _chunk_record("target", "child", 2, "parent-1", "needle evidence"),
            _chunk_record("after", "child", 3, "parent-1", "後の注意"),
            _chunk_record("parent-1", "parent", 1, "", long_parent_text, child_ids=["before", "target", "after"]),
        ]

        bundle = build_chunk_context_bundle(
            ContextBuildRequest(
                question="needle",
                ranked_children=[records[1]],
                active_records=records,
                top_k=1,
                neighbor_child_count=1,
                max_records=4,
                max_chars=3000,
            )
        )

        self.assertTrue(bundle.ready)
        self.assertEqual([record.id for record in bundle.records], ["parent-1"])
        self.assertEqual([item.role for item in bundle.evidence], ["primary"])
        self.assertEqual(bundle.primary_evidence[0].reason, "parent_of_retrieved_child")
        self.assertEqual(bundle.evidence_tree[0].anchor_child_ids, ("target",))
        self.assertEqual(
            {child.record.id: child.role for child in bundle.evidence_tree[0].children},
            {
                "target": "retrieved_anchor",
            },
        )
        self.assertIn("parent-1 / p.1", bundle.text)
        self.assertIn("tail marker", bundle.text)

    def test_saved_strategy_selects_parent_or_neighbor_exclusively(self):
        scope = dict(document_id="doc-a", recipe_id="recipe-a", chunk_set_id="set-a", chunk_strategy="fixed_length")
        before = _chunk_record("before", "child", 1, "synthetic", "前の原文", metadata=dict(scope))
        hit = _chunk_record("hit", "child", 2, "synthetic", "命中原文", metadata=dict(scope))
        after = _chunk_record("after", "child", 3, "synthetic", "後の原文", metadata=dict(scope))
        synthetic = _chunk_record("synthetic", "parent", 1, "", "合成した親を使わない", metadata=dict(scope))
        foreign = [
            replace(after, id=f"foreign-{key}", chunk_id=f"foreign-{key}", metadata={**scope, key: "other"})
            for key in ("document_id", "recipe_id", "chunk_set_id")
        ]

        def build(records, ranked, count=1, max_records=8):
            return build_chunk_context_bundle(
                ContextBuildRequest(
                    question="命中",
                    ranked_children=ranked,
                    active_records=records,
                    top_k=4,
                    neighbor_child_count=count,
                    max_records=max_records,
                    max_chars=1000,
                )
            )

        records = [before, hit, after, synthetic, *foreign]
        bundle = build(records, [hit])
        self.assertEqual([r.id for r in bundle.records], ["hit", "before", "after"])
        self.assertEqual([e.reason for e in bundle.evidence], ["retrieved_chunk", "adjacent_child", "adjacent_child"])
        self.assertEqual([r.id for r in build(records, [hit], count=0).records], ["hit"])
        self.assertEqual([r.id for r in build(records, [before]).records], ["before", "hit"])
        self.assertEqual([r.id for r in build(records, [hit], max_records=1).records], ["hit"])
        self.assertEqual([r.id for r in build(records, [after, before], count=0).records], ["after", "before"])
        self.assertEqual([r.id for r in build(records, [after, before, hit], count=0).records], ["after", "before", "hit"])
        parent_scope = {**scope, "chunk_strategy": "small_to_big"}
        parent_hit = replace(hit, metadata=parent_scope)
        parent = replace(synthetic, text="保存された親原文", metadata=parent_scope)
        mixed = build([*records, parent_hit, parent], [parent_hit], count=5)
        self.assertEqual([r.id for r in mixed.records], ["synthetic"])
        self.assertEqual([c.role for c in mixed.evidence_tree[0].children], ["retrieved_anchor"])
        missing = build([before, parent_hit, after], [parent_hit], count=5)
        self.assertEqual([r.id for r in missing.records], ["hit"])
        self.assertEqual(missing.evidence[0].reason, "parent_unavailable_child_fallback")
        other_parent = replace(parent, metadata={**parent_scope, "chunk_set_id": "other"})
        self.assertEqual([r.id for r in build([parent_hit, other_parent], [parent_hit]).records], ["hit"])

    def test_multiple_child_hits_in_same_parent_emit_one_parent_context(self):
        records = [
            _chunk_record("child-1", "child", 1, "parent-1", "first needle"),
            _chunk_record("child-2", "child", 2, "parent-1", "second needle"),
            _chunk_record("parent-1", "parent", 1, "", "complete parent synthesis", child_ids=["child-1", "child-2"]),
        ]

        bundle = build_chunk_context_bundle(
            ContextBuildRequest(
                question="needle",
                ranked_children=[records[0], records[1]],
                active_records=records,
                top_k=2,
                neighbor_child_count=0,
                max_records=2,
                max_chars=2000,
            )
        )

        self.assertTrue(bundle.ready)
        self.assertEqual([record.id for record in bundle.records], ["parent-1"])
        self.assertEqual(bundle.text.count("Synthesis text:"), 1)
        self.assertEqual(bundle.evidence_tree[0].anchor_child_ids, ("child-1", "child-2"))

    def test_parent_with_multiple_anchor_children_is_ranked_as_stronger_evidence(self):
        records = [
            _chunk_record("child-a1", "child", 1, "parent-a", "first matched step"),
            _chunk_record("child-b1", "child", 2, "parent-b", "single matched step"),
            _chunk_record("child-a2", "child", 3, "parent-a", "second matched step"),
            _chunk_record("parent-a", "parent", 1, "", "parent A synthesis", child_ids=["child-a1", "child-a2"]),
            _chunk_record("parent-b", "parent", 2, "", "parent B synthesis", child_ids=["child-b1"]),
        ]

        bundle = build_chunk_context_bundle(
            ContextBuildRequest(
                question="matched",
                ranked_children=[records[0], records[1], records[2]],
                active_records=records,
                top_k=3,
                neighbor_child_count=0,
                max_records=1,
                max_chars=2000,
            )
        )

        self.assertTrue(bundle.ready)
        self.assertEqual([record.id for record in bundle.records], ["parent-a"])
        self.assertEqual(bundle.evidence_tree[0].anchor_child_ids, ("child-a1", "child-a2"))
        self.assertGreater(bundle.evidence_tree[0].evidence_score, 1.0)
        self.assertEqual(
            bundle.evidence_tree[0].to_payload()["parent_evidence_score"],
            bundle.evidence_tree[0].evidence_score,
        )

    def test_anchors_from_the_same_parent_are_capped_so_other_parents_reach_top_k(self):
        # 同じ親の child が上位を占めても、起点は親あたり 2 件までにして別の親（別の要求の節）を top_k に入れる (#669)。
        records = [
            _chunk_record("child-a1", "child", 1, "parent-a", "step one"),
            _chunk_record("child-a2", "child", 2, "parent-a", "step two"),
            _chunk_record("child-a3", "child", 3, "parent-a", "step three"),
            _chunk_record("child-b1", "child", 4, "parent-b", "other request"),
            _chunk_record("parent-a", "parent", 1, "", "parent A synthesis", child_ids=["child-a1", "child-a2", "child-a3"]),
            _chunk_record("parent-b", "parent", 2, "", "parent B synthesis", child_ids=["child-b1"]),
        ]

        bundle = build_chunk_context_bundle(
            ContextBuildRequest(
                question="request",
                ranked_children=records[:4],
                active_records=records,
                top_k=3,
                neighbor_child_count=0,
                max_records=2,
                max_chars=4000,
            )
        )

        self.assertEqual(sorted(record.id for record in bundle.records), ["parent-a", "parent-b"])
        tree = {parent.record.id: parent for parent in bundle.evidence_tree}
        self.assertEqual(tree["parent-a"].anchor_child_ids, ("child-a1", "child-a2"))
        self.assertEqual(tree["parent-b"].anchor_child_ids, ("child-b1",))

        # 上限は request で変えられる（RAG_ENGINE_MAX_ANCHORS_PER_PARENT、#889）。1 なら親 A の起点は 1 件。
        bundle = build_chunk_context_bundle(
            ContextBuildRequest(
                question="request",
                ranked_children=records[:4],
                active_records=records,
                top_k=3,
                neighbor_child_count=0,
                max_records=2,
                max_chars=4000,
                max_anchors_per_parent=1,
            )
        )
        tree = {parent.record.id: parent for parent in bundle.evidence_tree}
        self.assertEqual(tree["parent-a"].anchor_child_ids, ("child-a1",))
        self.assertEqual(tree["parent-b"].anchor_child_ids, ("child-b1",))

    def test_child_parent_matching_uses_chunk_uid_when_chunk_ids_repeat(self):
        parent_a = _chunk_record(
            "chunk-docling-p000001",
            "parent",
            1,
            "",
            "document A parent synthesis needle",
            child_ids=["chunk-docling-c000001"],
            source_run_id="run-a",
            source="a.pdf",
            chunk_uid="chunk-run-a:chunk-docling-p000001",
        )
        parent_b = _chunk_record(
            "chunk-docling-p000001",
            "parent",
            1,
            "",
            "document B parent synthesis should not be used",
            child_ids=["chunk-docling-c000001"],
            source_run_id="run-b",
            source="b.pdf",
            chunk_uid="chunk-run-b:chunk-docling-p000001",
        )
        child_a = _chunk_record(
            "chunk-docling-c000001",
            "child",
            1,
            "chunk-docling-p000001",
            "document A child needle",
            source_run_id="run-a",
            source="a.pdf",
            chunk_uid="chunk-run-a:chunk-docling-c000001",
            parent_chunk_uid="chunk-run-a:chunk-docling-p000001",
        )

        bundle = build_chunk_context_bundle(
            ContextBuildRequest(
                question="needle",
                ranked_children=[child_a],
                active_records=[child_a, parent_a, parent_b],
                top_k=1,
                neighbor_child_count=0,
                max_records=3,
                max_chars=2000,
            )
        )

        self.assertTrue(bundle.ready)
        self.assertEqual(bundle.records[0].source, "a.pdf")
        self.assertIn("document A parent synthesis needle", bundle.text)
        self.assertNotIn("document B parent synthesis should not be used", bundle.text)

    def test_parent_size_gate_measures_parent_text_not_trace_formatting(self):
        # 除外判定は親本文の長さで行う。trace の前置きや Children: 行を含めた整形後の長さでは判定しない。
        body = "alpha parent " + ("A" * 600)
        records = [
            _chunk_record("child-1", "child", 1, "parent-1", "alpha needle"),
            _chunk_record("parent-1", "parent", 1, "", body, child_ids=["child-1"]),
        ]

        bundle = build_chunk_context_bundle(
            ContextBuildRequest(
                question="needle",
                ranked_children=[records[0]],
                active_records=records,
                top_k=1,
                neighbor_child_count=0,
                max_records=4,
                max_chars=len(body),
            )
        )

        self.assertTrue(bundle.ready)
        self.assertEqual([record.id for record in bundle.records], ["parent-1"])
        self.assertGreater(len(bundle.text), len(body))

    def test_oversized_parent_context_is_skipped_when_other_parent_fits(self):
        records = [
            _chunk_record("child-big", "child", 1, "parent-big", "big needle"),
            _chunk_record("parent-big", "parent", 1, "", "big parent " + ("X" * 1000), child_ids=["child-big"]),
            _chunk_record("child-small", "child", 2, "parent-small", "small needle"),
            _chunk_record("parent-small", "parent", 2, "", "small parent synthesis", child_ids=["child-small"]),
        ]

        bundle = build_chunk_context_bundle(
            ContextBuildRequest(
                question="needle",
                ranked_children=[records[0], records[2]],
                active_records=records,
                top_k=2,
                neighbor_child_count=0,
                max_records=4,
                max_chars=400,
            )
        )

        self.assertTrue(bundle.ready)
        self.assertEqual([record.id for record in bundle.records], ["parent-small"])
        self.assertIn("small parent synthesis", bundle.text)
        self.assertNotIn("parent-big", bundle.text)
        self.assertEqual(
            bundle.trace_payload()["excluded_evidence"],
            [{"id": "parent-big", "reason": "context_character_budget", "context_reason": "parent_of_retrieved_child"}],
        )

    def test_context_budget_keeps_citation_for_first_record(self):
        record = _chunk_record("target", "child", 1, "", "needle " * 100)

        bundle = context_bundle_from_records([record], max_chars=40)

        self.assertTrue(bundle.ready)
        self.assertEqual([item.role for item in bundle.evidence], ["primary"])
        self.assertIn("target / p.1", bundle.text)

    def test_returns_insufficient_when_no_active_children_are_available(self):
        inactive = _chunk_record("inactive", "child", 1, "", "needle", active=False)

        bundle = build_chunk_context_bundle(
            ContextBuildRequest(
                question="needle",
                ranked_children=[inactive],
                active_records=[inactive],
                top_k=1,
                neighbor_child_count=1,
                max_records=3,
                max_chars=1000,
            )
        )

        self.assertFalse(bundle.ready)
        self.assertEqual(bundle.status, "insufficient")
        self.assertIn("no active child chunks", bundle.insufficient_reason)

    def test_image_evidence_does_not_depend_on_question(self):
        """根拠の画像の有無は根拠の metadata だけで決め、質問の語を見ない（#3444）。"""
        record = _chunk_record(
            "target",
            "child",
            1,
            "",
            "画像説明",
            metadata={
                "active": True,
                "image_evidence": [{"image_id": "picture-1"}],
            },
        )

        self.assertTrue(record_has_image_evidence(record))

    def test_table_visual_evidence_counts_as_image_evidence(self):
        record = _chunk_record(
            "target",
            "child",
            1,
            "",
            "表内に確認ダイアログがあります。",
            metadata={
                "active": True,
                "table_context": [
                    {
                        "table_id": "table-1",
                        "visual_evidence": [
                            {
                                "image_id": "picture-1",
                                "record_id": "picture-1",
                                "vision_crop": "docling/vision/picture.png",
                            }
                        ],
                    }
                ],
            },
        )

        self.assertTrue(record_has_image_evidence(record))

    def test_ocr_aggregate_evidence_does_not_count_as_image_evidence(self):
        record = _chunk_record(
            "target",
            "child",
            1,
            "",
            "OCR抽出テキストです。",
            metadata={
                "active": True,
                "image_evidence": [{"image_id": "ocr-aggregate", "raw_type": "picture_ocr_text"}],
                "table_context": [
                    {
                        "table_id": "table-1",
                        "visual_evidence": [
                            {
                                "image_id": "table-ocr",
                                "record_id": "table-ocr",
                                "raw_type": "picture_ocr_text",
                            }
                        ],
                    }
                ],
            },
        )

        self.assertFalse(record_has_image_evidence(record))


def _chunk_record(
    record_id: str,
    level: str,
    chunk_seq: int,
    parent_id: str,
    text: str,
    *,
    child_ids: list[str] | None = None,
    active: bool = True,
    metadata: dict | None = None,
    source_run_id: str = "",
    source: str = "",
    chunk_uid: str = "",
    parent_chunk_uid: str = "",
) -> AnswerRecord:
    return AnswerRecord(
        id=record_id,
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=chunk_seq,
        category="ParentChunk" if level == "parent" else "ChildChunk",
        text=text,
        source=source,
        source_run_id=source_run_id,
        chunk_id=record_id,
        chunk_uid=chunk_uid,
        chunk_level=level,
        chunk_seq=chunk_seq,
        parent_chunk_id=parent_id,
        parent_chunk_uid=parent_chunk_uid,
        child_chunk_ids=tuple(child_ids or []),
        page_end=1,
        source_seq_ranges=({"page": 1, "seq_start": chunk_seq, "seq_end": chunk_seq},),
        source_record_refs=({"record_id": f"r{chunk_seq}", "page": 1, "seq_no": chunk_seq, "category": "Text"},),
        metadata=metadata
        if metadata is not None
        else {
            "active": active,
            "file_name": "manual.pdf",
            "lifecycle_status": "active" if active else "inactive",
        },
    )


if __name__ == "__main__":
    unittest.main()
