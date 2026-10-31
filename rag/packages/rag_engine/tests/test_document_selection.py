"""粗→細の検索: rerank 分数を文書ごとに集約して起点の文書を選び、後回しの文書は不足時に戻す (#1028)。"""

import unittest
from dataclasses import replace
from unittest.mock import patch

from rag_engine.config import get_settings
from rag_engine.generation import answering as a
from rag_engine.retrieval.context_builder import ContextChildEvidence, ContextParentEvidence, context_bundle_from_parent_evidence


def settings(enabled=True, ratio=0.5):
    return replace(
        get_settings(environ={}, dotenv_path=None),
        document_selection_enabled=enabled,
        document_selection_score_ratio=ratio,
    )


def child(name, seq, text, source, parent, score=None, run="run", question_score=None):
    metadata = {"section_path": ["管理-(1)登録"]}
    if score is not None:
        metadata["rerank"] = {"candidate_index": seq, "rank": seq, "relevance_score": score, "min_relevance_score": 0.0, "filtered": False}
        if question_score is not None:
            metadata["rerank"]["question_relevance_score"] = question_score
    return a.AnswerRecord(
        id=name,
        engine="docling",
        engine_label="Docling",
        page=seq,
        seq_no=seq,
        category="Chunk",
        text=text,
        source=source,
        source_run_id="source",
        chunk_id=name,
        chunk_uid=f"{run}:{name}",
        chunk_level="child",
        chunk_seq=seq,
        parent_chunk_uid=f"{run}:{parent}",
        metadata=metadata,
    )


def parent(name, seq, text, source, run="run"):
    return a.AnswerRecord(
        id=name,
        engine="docling",
        engine_label="Docling",
        page=seq,
        seq_no=seq,
        category="Chunk",
        text=text,
        source=source,
        source_run_id="source",
        chunk_id=name,
        chunk_uid=f"{run}:{name}",
        chunk_level="parent",
        chunk_seq=seq,
        parent_chunk_uid="",
        metadata={"section_path": ["管理-(1)登録"]},
    )


class SelectDocumentsTests(unittest.TestCase):
    def setUp(self):
        # 文書 A: 3 件で高分、文書 B: 2 件で中分、文書 C: 1 件で低分。
        self.children = [
            child("a1", 1, "A の説明 1", "a.pdf", "pa", 0.9),
            child("b1", 2, "B の説明 1", "b.pdf", "pb", 0.6),
            child("a2", 3, "A の説明 2", "a.pdf", "pa", 0.8),
            child("c1", 4, "C の説明", "c.pdf", "pc", 0.2),
            child("a3", 5, "A の説明 3", "a.pdf", "pa", 0.7),
            child("b2", 6, "B の説明 2", "b.pdf", "pb", 0.1),
        ]

    def test_low_scoring_document_outside_the_anchors_is_deferred_and_order_is_kept(self):
        kept, deferred, trace = a.select_documents(self.children, settings(), top_k=3)
        self.assertEqual([r.id for r in kept], ["a1", "b1", "a2", "a3", "b2"])
        self.assertEqual([r.id for r in deferred], ["c1"])
        self.assertEqual([e["source"] for e in trace["selected"]], ["a.pdf", "b.pdf"])
        self.assertEqual(trace["deferred"], [{"source": "c.pdf", "score": 0.2, "question_score": 0.2, "best_rank": 4, "hits": 1}])
        self.assertFalse(trace["restored"])

    def test_documents_with_close_scores_are_all_kept(self):
        kept, deferred, _ = a.select_documents(self.children, settings(ratio=0.05), top_k=3)  # 点数の落差の閾値を満たさない
        self.assertEqual(deferred, [])

    def test_hit_count_does_not_keep_a_weaker_document_or_defer_a_stronger_one(self):
        # 文書 X は rerank 5 位の 1 件（0.40）、文書 B は X より点数の低い 2 件（0.30）。件数で B を残し X を後回しに
        # していた（#3504）。点数だけで比べると、X も B も最上位（0.90）の 0.5 倍未満で、起点の外なら後回し。
        children = [
            *(child(f"a{i}", i, f"A{i}", "a.pdf", f"pa{i}", 0.9 - i * 0.01) for i in range(1, 5)),
            child("x1", 5, "X の答え", "x.pdf", "px", 0.40),
            child("b1", 6, "B1", "b.pdf", "pb", 0.30),
            child("b2", 7, "B2", "b.pdf", "pb2", 0.30),
        ]
        kept, deferred, trace = a.select_documents(children, settings(), top_k=4)
        self.assertEqual([r.id for r in deferred], ["x1", "b1", "b2"])
        self.assertEqual([e["source"] for e in trace["deferred"]], ["x.pdf", "b.pdf"])  # 点数の順（件数の順ではない）

        # rerank の上位 top_k 件（起点）に chunk を持つ文書は、命中 1 件で点数が低くても後回しにしない。
        kept, deferred, trace = a.select_documents(children, settings(), top_k=5)
        self.assertEqual([r.id for r in deferred], ["b1", "b2"])
        self.assertIn("x1", [r.id for r in kept])
        self.assertEqual([e["source"] for e in trace["selected"]], ["a.pdf", "x.pdf"])

    def test_single_top_reranked_chunk_is_kept_first_over_many_weaker_hits(self):
        children = [
            child("d1", 1, "D の説明", "d.pdf", "pd", 0.95),
            *(child(f"a{i}", 1 + i, f"A{i}", "a.pdf", f"pa{i}", 0.5) for i in range(1, 9)),
        ]
        kept, deferred, trace = a.select_documents(children, settings(), top_k=2)
        self.assertEqual(deferred, [])
        self.assertEqual(trace["selected"][0], {"source": "d.pdf", "score": 0.95, "question_score": 0.95, "best_rank": 1, "hits": 1})
        self.assertEqual(trace["selected"][1]["hits"], 8)

    def test_document_relevant_to_the_original_question_is_kept_even_if_a_search_query_scores_higher(self):
        # 検索文の rerank で文書 A が 0.95。文書 Q は検索文では 0.30 だが原質問では最上位（0.60）。原質問の点数でも
        # 比べるので、並べ替えの点数だけで最上位の 0.5 倍未満でも後回しにしない（#3504）。
        children = [
            child("a1", 1, "A", "a.pdf", "pa", 0.95, question_score=0.20),
            child("a2", 2, "A2", "a.pdf", "pa2", 0.9, question_score=0.20),
            child("q1", 3, "Q", "q.pdf", "pq", 0.40, question_score=0.60),
            child("z1", 4, "Z", "z.pdf", "pz", 0.10, question_score=0.10),
        ]
        kept, deferred, trace = a.select_documents(children, settings(), top_k=1)
        self.assertEqual([r.id for r in deferred], ["z1"])
        self.assertEqual([e["source"] for e in trace["selected"]], ["q.pdf", "a.pdf"])  # 原質問との点数の順

    def test_disabled_selection_keeps_everything_and_records_only_the_flag(self):
        kept, deferred, trace = a.select_documents(self.children, settings(enabled=False), top_k=3)
        self.assertEqual(len(kept), 6)
        self.assertEqual((deferred, trace), ([], {"enabled": False, "selected": [], "deferred": [], "restored": False}))

    def test_without_rerank_scores_nothing_is_deferred(self):
        # rerank の点数が無ければ落差を判断できないので後回しにしない (#1057)。表示は取得の順位の順。
        plain = [replace(c, metadata={"section_path": ["管理-(1)登録"]}) for c in self.children]
        kept, deferred, trace = a.select_documents(plain, settings(ratio=0.5), top_k=1)
        self.assertEqual(deferred, [])
        self.assertEqual([e["source"] for e in trace["selected"]], ["a.pdf", "b.pdf", "c.pdf"])
        self.assertIsNone(trace["selected"][0]["score"])

    def test_step_details_show_the_rerank_score_source_and_not_the_hit_count_rule(self):
        class Step:
            def __init__(self):
                self.fields, self.lines = {}, []

            def field(self, name, value):
                self.fields[name] = value

            def add(self, *lines):
                self.lines.extend(lines)

        step = Step()
        children = [*self.children, child("q1", 7, "Q", "q.pdf", "pq", 0.85, question_score=0.5)]
        a.select_documents(children, settings(), step, top_k=3)
        rule = step.fields["判定の基準"]
        self.assertIn("rerank の点数", rule)
        self.assertIn("元の質問との点数", rule)
        self.assertIn("上位 3 件の起点", rule)
        self.assertIn("命中の件数は判定に使いません", rule)
        self.assertNotIn("命中が", rule)
        self.assertEqual(step.fields["文書"], "4 文書（残す 3・後回し 1）")
        joined = "\n".join(step.lines)
        self.assertIn("残す: a.pdf（元の質問との並べ替えの点数 0.900・最上位 1 位・命中 3 件）", joined)
        self.assertIn("残す: q.pdf（元の質問との点数 0.500・検索文を含む並べ替えの点数 0.850・最上位 7 位・命中 1 件）", joined)
        self.assertIn("後回し: c.pdf", joined)

        plain = Step()
        a.select_documents([replace(c, metadata={}) for c in self.children], settings(), plain, top_k=3)
        self.assertIn("rerank の点数が無いため", plain.fields["判定の基準"])


class DefaultSettingTests(unittest.TestCase):
    def test_document_selection_is_on_by_default_and_can_be_disabled(self):
        self.assertTrue(get_settings(environ={}, dotenv_path=None).document_selection_enabled)  # #1040
        self.assertFalse(get_settings(environ={"RAG_ENGINE_DOCUMENT_SELECTION": "false"}, dotenv_path=None).document_selection_enabled)
        self.assertEqual(get_settings(environ={}, dotenv_path=None).document_selection_score_ratio, 0.5)


class DeferredRestoreTests(unittest.TestCase):
    def setUp(self):
        self.pa = parent("pa", 1, "A の説明 1 A の説明 2", "a.pdf")
        self.pc = parent("pc", 4, "C の説明", "c.pdf")
        self.a1 = child("a1", 1, "A の説明 1", "a.pdf", "pa", 0.9)
        self.c1 = child("c1", 4, "C の説明", "c.pdf", "pc", 0.2)
        self.pool = (self.a1, self.c1, self.pa, self.pc)
        bundle = context_bundle_from_parent_evidence(
            (
                ContextParentEvidence(
                    record=self.pa,
                    role="synthesis_parent",
                    reason="retrieved",
                    children=(ContextChildEvidence(record=self.a1, role="retrieved_anchor", reason="hit", retrieval_rank=1),),
                ),
            ),
            max_chars=10000,
        )
        self.selected = a.AnswerContext(
            list(bundle.records),
            bundle.text,
            evidence=bundle.evidence,
            evidence_tree=bundle.evidence_tree,
            expansion_records=self.pool,
            ranked_candidates=(self.a1, self.c1),
            deferred_records=(self.c1,),
            document_selection={"enabled": True, "selected": [{"source": "a.pdf"}], "deferred": [{"source": "c.pdf"}], "restored": False},
        )

    def test_crag_restores_deferred_documents_before_rewriting_and_records_it(self):
        def attempt(number, sufficient, **kw):
            return a.CragRetrievalAttempt(number, "q", ("q",), sufficient, **kw)

        grades = [attempt(1, False, rewritten_query="登録 別の検索文"), attempt(2, True)]
        with (
            patch.object(a, "build_adb_hybrid_answer_context", return_value=self.selected),
            patch.object(a, "_grade_crag_retrieval", side_effect=grades),
        ):
            result, rewrites, attempts = a.build_crag_answer_context("登録条件は", "source", ["docling"], settings())
        self.assertEqual(rewrites, ())  # 改写検索の前に後回しの文書を戻す
        self.assertEqual(attempts[0].stop_reason, "deferred_documents_restored")
        self.assertTrue(attempts[1].document_selection["restored"])
        self.assertIn("pc", [r.id for r in result.records])  # 戻した文書 C の親が根拠に入る
        self.assertEqual(result.deferred_records, ())

    def test_crag_does_not_spend_an_attempt_when_restoring_adds_no_evidence(self):
        # 後回しの文書は起点（rerank の上位 top_k）の外だけなので、戻しても根拠が同じことがある（#3504）。
        # そのときは評価の回を使わず、改写検索へ進む。
        def attempt(number, sufficient, **kw):
            return a.CragRetrievalAttempt(number, "q", ("q",), sufficient, **kw)

        grades = [attempt(1, False, rewritten_query="登録 別の検索文"), attempt(2, True)]
        same = replace(self.selected, deferred_records=())
        with (
            patch.object(a, "build_adb_hybrid_answer_context", return_value=self.selected),
            patch.object(a, "_restore_deferred_context", return_value=same),
            patch.object(a, "_grade_crag_retrieval", side_effect=grades),
        ):
            _result, rewrites, attempts = a.build_crag_answer_context("登録条件は", "source", ["docling"], settings())
        self.assertEqual(rewrites, ("登録 別の検索文",))
        self.assertNotEqual(attempts[0].stop_reason, "deferred_documents_restored")

    def test_generation_side_remedy_adds_the_deferred_document_parents_first(self):
        expanded, trace = a._expand_context_from_pool("登録条件は", self.selected)
        self.assertEqual(trace["mode"], "deferred_documents")
        self.assertEqual(trace["added_chunk_ids"], ["run:pc"])
        self.assertIn("pc", [r.id for r in expanded.records])
        self.assertEqual(expanded.deferred_records, ())
        self.assertTrue(expanded.document_selection["restored"])

    def test_generation_side_remedy_caps_added_parents_so_existing_evidence_is_kept(self):
        # 可視 1 件に対し後回しの文書が 3 件あっても、併合が既存の根拠を落とさない件数（可視の数）だけ足す。
        extra = [
            (child(f"d{i}", 10 + i, f"D{i} の説明", f"d{i}.pdf", f"pd{i}", 0.1), parent(f"pd{i}", 10 + i, f"D{i} の説明", f"d{i}.pdf"))
            for i in range(3)
        ]
        context = replace(
            self.selected,
            expansion_records=self.pool + tuple(r for pair in extra for r in pair),
            deferred_records=(self.c1, *(c for c, _ in extra)),
        )
        expanded, trace = a._expand_context_from_pool("登録条件は", context)
        self.assertIsNotNone(expanded, trace)
        self.assertEqual(trace["added_chunk_ids"], ["run:pc"])
        self.assertIn("pa", [r.id for r in expanded.records])

    def test_pre_generation_same_unit_fill_does_not_consume_deferred_documents(self):
        # 生成前の同単元の補充は後回しの文書を使わない（答えていない要求が出てからの回退に取っておく）。
        filled, trace = a._fill_same_unit("登録条件は", self.selected)
        self.assertFalse(trace["applied"], trace)
        self.assertEqual([r.id for r in filled.deferred_records], ["c1"])

    def test_refine_keeps_the_selection_state(self):
        refined = a._refine_crag_context(self.selected, ("run:a1",), max_chars=10000)
        self.assertEqual([r.id for r in refined.deferred_records], ["c1"])
        self.assertEqual(len(refined.ranked_candidates), 2)
        self.assertTrue(refined.document_selection["enabled"])


if __name__ == "__main__":
    unittest.main()


def test_explicit_attachment_survives_document_selection_and_anchor_cap():
    from dataclasses import replace

    from rag_engine.retrieval.attachment_evidence import ATTACHMENT_EVIDENCE_KEY

    attached = child("attachment", 1, "入力の行", "input.csv", "attached", 0.1)
    attached.metadata[ATTACHMENT_EVIDENCE_KEY] = True
    other = child("a", 2, "規則", "a.pdf", "pa", 0.9)
    kept, deferred, trace = a.select_documents([other, attached], settings(), top_k=1)
    assert attached in kept and attached not in deferred
    assert all(e["source"] != "input.csv" for e in trace["deferred"])
    assert a.cap_anchors_per_document([attached, attached, other], replace(settings(), max_anchors_per_document=1), top_k=3) == [
        attached,
        attached,
        other,
    ]


def _context_record(name, level):
    from rag_engine.generation.answering import AnswerRecord

    return AnswerRecord(
        id=name if level == "child" else f"{name}-p",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        text=name,
        source="架空手引き.pdf",
        chunk_level=level,
        chunk_id=name if level == "child" else f"{name}-p",
        chunk_uid=f"uid:{name}" if level == "child" else f"uid:{name}-p",
        parent_chunk_uid=f"uid:{name}-p" if level == "child" else "",
        source_run_id="run",
        metadata={"section_path": ["第 1 章"]},
    )


def _bundle(children, *, top_k, max_records):
    from rag_engine.retrieval.context_builder import ContextBuildRequest, build_chunk_context_bundle

    names = [r.id for r in children]
    parents = [_context_record(name, "parent") for name in names]
    return build_chunk_context_bundle(
        ContextBuildRequest(
            question="質問",
            ranked_children=children,
            active_records=[*children, *parents],
            top_k=top_k,
            neighbor_child_count=0,
            max_records=max_records,
            max_chars=5000,
        )
    )


def _attachment_context_record(name):
    from rag_engine.retrieval.attachment_evidence import ATTACHMENT_EVIDENCE_KEY

    record = _context_record(name, "child")
    record.metadata[ATTACHMENT_EVIDENCE_KEY] = True
    return record


def test_attachment_below_the_anchor_limit_is_added_outside_it():
    """rerank で top_k より下の会話の添付も、起点・親の数の外に足して根拠に入れる（#3212）。"""
    children = [_context_record(f"m{index}", "child") for index in range(5)]
    attached = _attachment_context_record("att1")

    bundle = _bundle([*children, attached], top_k=3, max_records=3)

    # ナレッジベースの起点・親は添付が無いときと同じ 3 つで、添付の親を後ろに足す。
    assert [r.id for r in bundle.records] == ["m0-p", "m1-p", "m2-p", "att1-p"]
    assert bundle.evidence_tree[-1].reason == "conversation_attachment"
    assert [child.role for child in bundle.evidence_tree[-1].children] == ["retrieved_anchor"]


def test_attachment_inside_the_anchor_limit_does_not_take_a_knowledge_base_anchor():
    """top_k の内に並んだ添付も起点に数えず、ナレッジベースの起点を減らさない（#3212）。"""
    children = [_context_record(f"m{index}", "child") for index in range(5)]
    attached = _attachment_context_record("att1")

    bundle = _bundle([attached, *children], top_k=3, max_records=3)
    plain = _bundle(children, top_k=3, max_records=3)

    assert [r.id for r in plain.records] == ["m0-p", "m1-p", "m2-p"]
    assert [r.id for r in bundle.records] == ["m0-p", "m1-p", "m2-p", "att1-p"]
