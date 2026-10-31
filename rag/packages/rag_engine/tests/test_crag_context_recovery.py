"""CRAG の文脈回復と機能境界を検証する。"""

import unittest
from dataclasses import replace
from unittest.mock import patch

from fictional_examples import ERROR_UNIT_7, ERROR_UNIT_15

from rag_engine.config import get_settings
from rag_engine.generation import answering as a
from rag_engine.models.llm import CragRetrievalGradeOutput
from rag_engine.retrieval.context_recovery import recover_context_records


def record(name, seq, text, level="child", parent="p1", operation="管理-(1)登録", run="run"):
    return a.AnswerRecord(
        id=name,
        engine="docling",
        engine_label="Docling",
        page=seq,
        seq_no=seq,
        category="Chunk",
        text=text,
        source="manual.pdf",
        source_run_id="source",
        chunk_id=name,
        chunk_uid=f"{run}:{name}",
        chunk_level=level,
        chunk_seq=seq,
        parent_chunk_uid=f"{run}:{parent}" if parent else "",
        metadata={"section_path": [operation]},
    )


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.child = record("c1", 1, "入力画面を開く。")
        self.next = record("c2", 2, "登録後は再ログインする。")
        self.parent = record("p1", 1, "入力画面を開く。登録後は再ログインする。", "parent", "")
        self.parent2 = record("p2", 2, "登録は未確定情報だけに適用する。", "parent", "")
        self.pool = [self.child, self.next, self.parent, self.parent2]

    def recover(self, mode, visible=None, pool=None, ids=None, **kwargs):
        return recover_context_records(
            visible or [self.child], self.pool if pool is None else pool, ["run:c1"] if ids is None else ids, mode, **kwargs
        )

    def test_child_neighbor(self):
        self.assertEqual(self.recover("neighbor_children").records, (self.next,))

    def test_parent_contains_missing_condition(self):
        self.assertEqual(self.recover("parent").records, (self.parent,))

    def test_parent_neighbors_from_child_and_parent(self):
        self.assertEqual(self.recover("neighbor_parents").records, (self.parent, self.parent2))
        self.assertEqual(self.recover("neighbor_parents", visible=[self.parent], ids=["run:p1"]).records, (self.parent2,))

    def test_foreign_document_version_engine_and_operation_rejected(self):
        for foreign in [
            replace(self.next, source="other.pdf"),
            replace(self.next, source_run_id="other"),
            replace(self.next, engine="other"),
            replace(self.next, chunk_uid="other:c2"),
            replace(self.next, metadata={"section_path": ["管理-(2)削除"]}),
        ]:
            with self.subTest(foreign=foreign):
                self.assertFalse(self.recover("neighbor_children", pool=[self.child, foreign]).records)

    def test_same_unit_returns_sibling_parents_of_the_same_numbered_heading(self):
        # 同じ機能ユニット（番号付き見出しまで）の親を起点に近い順で補い、別ユニット・別文書は補わない (#666)。
        unit = ["２．修正方法", ERROR_UNIT_15, "【修正方法２】"]
        anchor = replace(self.parent, metadata={"section_path": unit})
        sibling_far = replace(
            self.parent2,
            id="p4",
            chunk_uid="run:p4",
            chunk_seq=4,
            text="⑥振替数量を入力します。",
            metadata={"section_path": ["２．修正方法", ERROR_UNIT_15, "【修正方法２】"]},
        )
        sibling_near = replace(
            self.parent2,
            id="p3",
            chunk_uid="run:p3",
            chunk_seq=3,
            text="④出荷数量を０にします。⑤確認ボタンを押します。",
            metadata={"section_path": ["２．修正方法", ERROR_UNIT_15, "【修正方法１】"]},
        )
        other_unit = replace(
            self.parent2,
            id="p5",
            chunk_uid="run:p5",
            chunk_seq=5,
            text="得意先区分を通常に変更します。",
            metadata={"section_path": ["２．修正方法", ERROR_UNIT_7, "【修正方法】"]},
        )
        other_doc = replace(sibling_near, id="p6", chunk_uid="run:p6", source="other.pdf")
        pool = [anchor, sibling_far, sibling_near, other_unit, other_doc]
        recovered = recover_context_records([anchor], pool, ["run:p1"], "same_unit")
        self.assertEqual([r.id for r in recovered.records], ["p3", "p4"])
        # 予算で打ち切る。
        limited = recover_context_records([anchor], pool, ["run:p1"], "same_unit", max_chars=30)
        self.assertEqual([r.id for r in limited.records], ["p3"])
        # 番号見出しのない起点はユニットが空なので補わない。
        plain = replace(anchor, metadata={"section_path": ["概要"]})
        self.assertFalse(
            recover_context_records(
                [plain], [plain, replace(sibling_near, metadata={"section_path": ["概要"]})], ["run:p1"], "same_unit"
            ).records
        )

    def test_unknown_or_local_id_is_not_an_anchor(self):
        for ids in [["c1"], ["run:missing"], ["run:c2"]]:
            self.assertFalse(self.recover("parent", ids=ids).records)

    def test_conflicting_uid_is_not_an_anchor(self):
        other = replace(self.child, source="other.pdf")
        self.assertFalse(self.recover("parent", visible=[self.child, other]).records)

    def test_grader_canonicalizes_recovery_anchor_and_preserves_action(self):
        output = CragRetrievalGradeOutput(
            sufficient=False,
            confidence=0,
            reason="条件不足",
            rewritten_query="",
            recovery_action="parent",
            recovery_anchor_ids=["c1", "unknown"],
        )
        context = a.AnswerContext([self.child], self.child.text, expansion_records=tuple(self.pool))
        with patch.object(a, "parse_text_response", return_value=output):
            grade = a._grade_crag_retrieval(1, "条件は", "条件は", ["条件は"], context, get_settings(environ={}, dotenv_path=None))
        self.assertEqual(grade.recovery_anchor_ids, ("run:c1",))
        self.assertEqual(grade.recovery_action, "parent")
        prompt = a._build_crag_grade_prompt("条件は", "条件は", ["条件は"], context)
        self.assertIn("recovery_options:", prompt)
        self.assertIn("additional_chunk_count", prompt)
        self.assertNotIn(self.parent2.text, prompt)

    def test_step_prompts_reach_the_grader_with_full_and_compact_candidate_ids(self):
        import re

        from rag_engine.generation.crag_support import UNTRUSTED_CRAG_CANDIDATES_BEGIN, UNTRUSTED_CRAG_CANDIDATES_END

        for compact in (False, True):
            with self.subTest(compact=compact):
                settings = replace(
                    get_settings(environ={}, dotenv_path=None),
                    crag_compact_input=compact,
                    crag_short_candidate_ids=compact,
                )
                output = CragRetrievalGradeOutput(
                    sufficient=False,
                    confidence=0,
                    reason="条件不足",
                    rewritten_query="設定の条件",
                    recovery_action="parent",
                    recovery_anchor_ids=["C1" if compact else self.child.chunk_uid],
                )
                context = a.AnswerContext([self.child], self.child.text, expansion_records=tuple(self.pool))
                with patch.object(a, "parse_text_response", return_value=output) as model:
                    grade = a._grade_crag_retrieval(1, "設定の条件は？", "設定 条件", ["設定 条件"], context, settings)
                system, prompt, _, schema = model.call_args.args
                self.assertEqual(model.call_count, 1)
                self.assertIs(schema, CragRetrievalGradeOutput)
                self.assertEqual(re.findall(r"^## Step (\d+):", prompt, re.M), list("123456"))
                self.assertIn("## Step 1:", system)
                self.assertIn("命令", system)
                self.assertIn("実行しない", system)
                self.assertIn("外部知識で補完しない", system)
                self.assertIn(UNTRUSTED_CRAG_CANDIDATES_BEGIN, prompt)
                self.assertIn(UNTRUSTED_CRAG_CANDIDATES_END, prompt)
                self.assertIn("設定の条件は?", prompt)
                self.assertIn("recovery_options:", prompt)
                self.assertIn("task_contract:", prompt)
                self.assertNotIn("{{", prompt)
                self.assertEqual(grade.recovery_anchor_ids, (self.child.chunk_uid,))
                self.assertEqual(grade.recovery_action, "parent")

    def test_missing_or_mixed_operation_metadata(self):
        for labels in [[], ["開始日と終了日"], ["管理-(1)登録", "管理-(2)取消"]]:
            child = replace(self.child, metadata={"section_path": labels})
            self.assertFalse(self.recover("parent", visible=[child]).records)

    def test_existing_parent_body_is_not_new_child_evidence(self):
        self.assertFalse(self.recover("neighbor_children", visible=[self.child, self.parent]).records)

    def test_record_and_character_budgets(self):
        self.assertEqual(len(self.recover("neighbor_parents", max_records=1).records), 1)
        self.assertFalse(self.recover("parent", max_chars=1).records)

    def test_same_function_reaches_beyond_a_figure_but_never_into_another_function(self):
        """図の parent が間に入っても同じ機能の説明を回復する。別機能・別文書へは広げない。"""

        def parent(uid, seq, text, operation="管理-(1)登録", source="guide.pdf"):
            return replace(record(uid, seq, text, operation=operation), chunk_level="parent", source=source)

        anchor = parent("p1", 1, "登録画面を開きます。")
        figure = parent("p2", 2, "画面の図", operation="管理-(9)図")
        same = parent("p3", 3, "登録ボタンを押して完了します。")
        other = parent("p4", 4, "削除します。", operation="管理-(2)削除")
        foreign = parent("p5", 5, "登録の別資料。", source="other.pdf")
        pool = [anchor, figure, same, other, foreign]
        adjacent = recover_context_records([anchor], pool, ["run:p1"], "neighbor_parents")
        self.assertEqual([r.text for r in adjacent.records], [])  # 隣は別機能の図
        result = recover_context_records([anchor], pool, ["run:p1"], "same_function")
        self.assertEqual([r.text for r in result.records], [same.text])

    def test_does_not_jump_over_other_operation(self):
        middle = record("c2", 2, "削除操作", operation="管理-(2)削除")
        far = record("c3", 3, "遠い同名操作")
        self.assertFalse(self.recover("neighbor_children", pool=[self.child, middle, far]).records)

    def test_grade_schema_is_backward_compatible(self):
        value = CragRetrievalGradeOutput(sufficient=False, confidence=0, reason="不足", rewritten_query="追加条件")
        self.assertEqual(value.recovery_action, "rewrite")

    def test_loop_expands_without_rewrite_and_regrades(self):
        context = a.AnswerContext([self.child], self.child.text, expansion_records=tuple(self.pool))
        first = a.CragRetrievalAttempt(1, "条件", ("条件",), False, recovery_action="neighbor_parents", recovery_anchor_ids=("run:c1",))
        second = a.CragRetrievalAttempt(2, "条件", ("条件",), True)
        with (
            patch.object(a, "build_adb_hybrid_answer_context", return_value=context) as search,
            patch.object(a, "_grade_crag_retrieval", side_effect=[first, second]) as grade,
        ):
            result, rewrites, attempts = a.build_crag_answer_context(
                "登録条件は", "source", ["docling"], get_settings(environ={}, dotenv_path=None), answer_llm_provider="osaka"
            )
        self.assertEqual(search.call_count, 1)
        self.assertEqual(grade.call_count, 2)
        self.assertIn(self.parent2.text, result.text)
        self.assertEqual(rewrites, ())
        self.assertEqual(attempts[0].recovery_trace["reason"], "expanded")

    def test_empty_expansion_falls_back_to_rewrite(self):
        context = a.AnswerContext([self.child], self.child.text)
        first = a.CragRetrievalAttempt(
            1, "条件", ("条件",), False, rewritten_query="登録の適用条件", recovery_action="parent", recovery_anchor_ids=("run:c1",)
        )
        with (
            patch.object(a, "build_adb_hybrid_answer_context", return_value=context) as search,
            patch.object(a, "_grade_crag_retrieval", return_value=first),
        ):
            _, rewrites, attempts = a.build_crag_answer_context(
                "登録条件は", "source", ["docling"], get_settings(environ={}, dotenv_path=None)
            )
        self.assertEqual(search.call_count, 2)
        self.assertTrue(rewrites)
        self.assertEqual(attempts[-1].stop_reason, "no_new_evidence")

    def test_repeated_rewrite_recovers_context_before_stopping(self):
        context = a.AnswerContext([self.child], self.child.text, expansion_records=tuple(self.pool))
        first = a.CragRetrievalAttempt(1, "条件", ("条件",), False, rewritten_query="登録の対象条件")
        second = a.CragRetrievalAttempt(2, "条件", ("条件",), True)
        with (
            patch.object(a, "build_adb_hybrid_answer_context", return_value=context) as search,
            patch.object(a, "_grade_crag_retrieval", side_effect=[first, second]),
        ):
            result, _, attempts = a.build_crag_answer_context(
                "登録条件は", "source", ["docling"], get_settings(environ={}, dotenv_path=None)
            )
        self.assertEqual(search.call_count, 2)
        self.assertIn(self.next.text, result.text)
        self.assertEqual(attempts[0].recovery_trace["trigger"], "rewrite_no_new_evidence")
        self.assertTrue(attempts[-1].sufficient)

    def test_insufficient_recovery_stops_at_attempt_budget(self):
        context = a.AnswerContext([self.child], self.child.text, expansion_records=tuple(self.pool))
        insufficient = a.CragRetrievalAttempt(
            1, "条件", ("条件",), False, rewritten_query="登録の対象条件", recovery_action="parent", recovery_anchor_ids=("run:c1",)
        )
        with (
            patch.object(a, "build_adb_hybrid_answer_context", return_value=context),
            patch.object(a, "_grade_crag_retrieval", return_value=insufficient) as grade,
        ):
            _, _, attempts = a.build_crag_answer_context("登録条件は", "source", ["docling"], get_settings(environ={}, dotenv_path=None))
        self.assertLessEqual(grade.call_count, a.CRAG_MAX_RETRIEVAL_ATTEMPTS)
        self.assertFalse(attempts[-1].sufficient)

    def test_missing_rewrite_can_expand_again_until_conditions_are_found(self):
        context = a.AnswerContext([self.child], self.child.text, expansion_records=tuple(self.pool))
        grades = [a.CragRetrievalAttempt(i, "条件", ("条件",), i == 3) for i in range(1, 4)]
        with (
            patch.object(a, "build_adb_hybrid_answer_context", return_value=context) as search,
            patch.object(a, "_grade_crag_retrieval", side_effect=grades),
        ):
            result, _, attempts = a.build_crag_answer_context(
                "登録条件は", "source", ["docling"], get_settings(environ={}, dotenv_path=None)
            )
        self.assertEqual(search.call_count, 1)
        self.assertEqual(len(attempts), 3)
        self.assertTrue(attempts[-1].sufficient)
        self.assertIn(self.parent.text, result.text)
        self.assertEqual(attempts[0].recovery_trace["trigger"], "rewrite_unavailable")

    def test_only_actual_data_confirmation_does_not_expand(self):
        question = "登録条件は"
        checks = tuple(
            {
                "aspect": aspect,
                "status": "data_confirmation" if aspect == "applicability" else "supported",
                "source_ids": ["run:c1"],
                "reason": "一般規則は確認済み。実値を照会する。",
            }
            for aspect in a.task_contract(question)["required_aspects"]
        )
        context = a.AnswerContext([self.child], self.child.text, expansion_records=tuple(self.pool))
        grade = a.CragRetrievalAttempt(
            1, question, (question,), False, aspect_checks=checks, recovery_action="parent", recovery_anchor_ids=("run:c1",)
        )
        with (
            patch.object(a, "build_adb_hybrid_answer_context", return_value=context),
            patch.object(a, "_grade_crag_retrieval", return_value=grade),
            patch.object(a, "recover_context_records") as recovery,
        ):
            _, _, attempts = a.build_crag_answer_context(question, "source", ["docling"], get_settings(environ={}, dotenv_path=None))
        recovery.assert_not_called()
        self.assertEqual(attempts[-1].stop_reason, "data_confirmation_only")

    def test_sufficient_with_unknown_aspects_expands_once_without_regrade(self):
        # 不確定な観点だけなら pool から文脈を補い、取得済み根拠で部分回答へ進む。
        context = a.AnswerContext([self.child], self.child.text, expansion_records=tuple(self.pool))
        grade = a.CragRetrievalAttempt(
            1,
            "条件",
            ("条件",),
            True,
            relevant_chunk_ids=("run:c1",),
            unknown_aspects=("procedure",),
            recovery_action="parent",
            recovery_anchor_ids=("run:c1",),
        )
        with (
            patch.object(a, "build_adb_hybrid_answer_context", return_value=context) as search,
            patch.object(a, "_grade_crag_retrieval", return_value=grade) as grader,
        ):
            result, _, attempts = a.build_crag_answer_context("条件は", "source", ["docling"], get_settings(environ={}, dotenv_path=None))
        self.assertEqual((search.call_count, grader.call_count), (1, 1))
        self.assertEqual(attempts[-1].stop_reason, "expanded_for_unverified_aspects")
        self.assertIn(self.parent.text, result.text)
        self.assertEqual(result.expansion_records, tuple(self.pool))

    def test_sufficient_or_evaluator_error_does_not_expand(self):
        context = a.AnswerContext([self.child], self.child.text, expansion_records=tuple(self.pool))
        for error in [False, True]:
            with (
                patch.object(a, "build_adb_hybrid_answer_context", return_value=context) as search,
                patch.object(
                    a,
                    "_grade_crag_retrieval",
                    side_effect=RuntimeError("unavailable") if error else None,
                    return_value=a.CragRetrievalAttempt(1, "", (), True),
                ),
                patch.object(a, "recover_context_records") as recovery,
            ):
                a.build_crag_answer_context("登録条件は", "source", ["docling"], get_settings(environ={}, dotenv_path=None))
            recovery.assert_not_called()
            self.assertEqual(search.call_count, 1)


class PoolHandoffTests(unittest.TestCase):
    """CRAG と回答生成側の是正のあいだで、検索済みの pool と evidence_tree を失わない (#543)。"""

    def setUp(self):
        from rag_engine.retrieval.context_builder import ContextChildEvidence, ContextParentEvidence, context_bundle_from_parent_evidence

        self.child = record("c1", 1, "入力画面を開く。")
        self.parent = record("p1", 1, "入力画面を開く。登録後は再ログインする。", "parent", "")
        self.parent2 = record("p2", 2, "登録は未確定情報だけに適用する。", "parent", "")
        self.pool = (self.child, record("c2", 2, "登録後は再ログインする。"), self.parent, self.parent2)
        bundle = context_bundle_from_parent_evidence(
            (
                ContextParentEvidence(
                    record=self.parent,
                    role="synthesis_parent",
                    reason="retrieved",
                    children=(ContextChildEvidence(record=self.child, role="retrieved_anchor", reason="hit", retrieval_rank=1),),
                ),
            ),
            max_chars=10000,
        )
        self.context = a.AnswerContext(
            list(bundle.records), bundle.text, evidence=bundle.evidence, evidence_tree=bundle.evidence_tree, expansion_records=self.pool
        )

    def test_sufficient_crag_context_keeps_the_pool_for_answer_side_recovery(self):
        # relevant_chunk_ids があると context を作り直す。pool を落とすと是正が CRAG flow でだけ空振りする。
        for context in (self.context, a.AnswerContext([self.child], self.child.text, expansion_records=self.pool)):
            with self.subTest(tree=bool(context.evidence_tree)):
                refined = a._refine_crag_context(context, ("run:c1",), max_chars=10000)
                self.assertEqual(refined.expansion_records, self.pool)
                expanded, trace = a._expand_context_from_pool("登録条件は", refined)
                self.assertIsNotNone(expanded, trace)

    def test_answer_side_recovery_keeps_the_evidence_tree(self):
        expanded, trace = a._expand_context_from_pool("登録条件は", self.context)
        self.assertEqual(trace["added_chunk_ids"], ["run:p2"])
        self.assertEqual([r.id for r in expanded.records], ["p1", "p2"])
        # 既存分の子チャンクが残る。tree なしの fallback に落ちると保存 payload の children と子単位の抜粋が消える。
        self.assertEqual([p.record.id for p in expanded.evidence_tree], ["p1", "p2"])
        self.assertEqual([c.record.id for c in expanded.evidence_tree[0].children], ["c1"])
        self.assertEqual(len(expanded.evidence), 2)
        self.assertEqual(expanded.expansion_records, self.pool)

    def test_crag_pool_is_the_union_of_all_attempts(self):
        other = record("x1", 9, "別機能の説明。", "parent", "", operation="管理-(9)別")
        contexts = iter(
            [
                a.AnswerContext([self.child], self.child.text, expansion_records=self.pool),
                a.AnswerContext([other], other.text, expansion_records=(other,)),
                a.AnswerContext([other], other.text, expansion_records=(other,)),
            ]
        )

        def attempt(number, sufficient, **kw):
            return a.CragRetrievalAttempt(number, "q", ("q",), sufficient, **kw)

        grades = [
            attempt(1, False, rewritten_query="登録 適用条件 未確定"),
            # 2回目の検索は別の pool を返すが、評価器は1回目の根拠を起点に文脈の追加を選ぶ。
            attempt(2, False, recovery_action="neighbor_parents", recovery_anchor_ids=("run:c1",)),
            attempt(3, True),
        ]
        with (
            patch.object(a, "build_adb_hybrid_answer_context", side_effect=lambda *args, **kwargs: next(contexts)),
            patch.object(a, "_grade_crag_retrieval", side_effect=grades),
        ):
            result, _, attempts = a.build_crag_answer_context(
                "登録条件は", "source", ["docling"], get_settings(environ={}, dotenv_path=None)
            )
        self.assertEqual(attempts[1].recovery_trace["added_chunk_ids"], ["run:p1", "run:p2"])
        self.assertEqual({r.id for r in result.expansion_records}, {"c1", "c2", "p1", "p2", "x1"})


class RejectedCandidateTests(unittest.TestCase):
    """評価器が relevant=false と明示した候補は生成用 context の末尾へ回し、回復用 pool から除く (#1010, #1122)。"""

    def setUp(self):
        from rag_engine.retrieval.context_builder import ContextChildEvidence, ContextParentEvidence, context_bundle_from_parent_evidence

        self.c1 = record("c1", 1, "対象の操作説明。")
        self.p1 = record("p1", 1, "対象の操作説明。登録後は再ログインする。", "parent", "")
        self.x1 = record("x1", 9, "別業務の操作説明。", "parent", "", operation="管理-(9)別")
        self.u1 = record("u1", 5, "未評価の候補。", "parent", "", operation="管理-(5)未評価")
        self.pool = (self.c1, self.p1, self.x1, self.u1, record("x2", 10, "別業務の続き。", "parent", "", operation="管理-(9)別"))
        tree = tuple(
            ContextParentEvidence(
                record=r,
                role="synthesis_parent",
                reason="retrieved",
                children=(ContextChildEvidence(record=self.c1, role="retrieved_anchor", reason="hit", retrieval_rank=1),)
                if r is self.p1
                else (),
            )
            for r in (self.p1, self.x1, self.u1)
        )
        bundle = context_bundle_from_parent_evidence(tree, max_chars=10000)
        self.tree_context = a.AnswerContext(
            list(bundle.records), bundle.text, evidence=bundle.evidence, evidence_tree=bundle.evidence_tree, expansion_records=self.pool
        )
        self.flat_context = a.AnswerContext([self.p1, self.x1, self.u1], "text", expansion_records=self.pool)

    def test_rejected_candidate_moves_after_unassessed_candidate(self):
        for context in (self.tree_context, self.flat_context):
            with self.subTest(tree=bool(context.evidence_tree)):
                refined = a._refine_crag_context(context, ("run:p1",), max_chars=10000, rejected_chunk_ids=("run:x1",))
                # 関連 → 未評価 → 不適合の順。評価器の誤判定で正解の根拠を失わないよう不適合も残す
                self.assertEqual([r.id for r in refined.records], ["p1", "u1", "x1"])
                self.assertEqual([r.id for r in refined.expansion_records], ["c1", "p1", "u1", "x2"])  # pool からも除く

    def test_rejected_child_of_a_relevant_parent_keeps_the_parent(self):
        refined = a._refine_crag_context(self.tree_context, ("run:p1",), max_chars=10000, rejected_chunk_ids=("run:c1",))
        self.assertIn("p1", [r.id for r in refined.records])

    def test_all_candidates_rejected_returns_an_empty_context_not_the_original(self):
        for context in (self.tree_context, self.flat_context):
            with self.subTest(tree=bool(context.evidence_tree)):
                refined = a._refine_crag_context(context, (), max_chars=10000, rejected_chunk_ids=("run:p1", "run:x1", "run:u1"))
                self.assertEqual(refined.records, [])
                self.assertEqual(refined.insufficient_reason, "all_candidates_rejected")
                self.assertEqual([r.id for r in refined.expansion_records], ["x2"])

    def test_rejected_ids_go_last_in_the_final_fallback_context(self):
        # 評価枠を使い切って取得済みの根拠で回答する場合も、1 回目に不適合とした候補は末尾に置く。
        contexts = iter([self.flat_context, self.flat_context, self.flat_context])

        def attempt(number, **kw):
            return a.CragRetrievalAttempt(number, "q", ("q",), False, **kw)

        grades = [
            attempt(1, rejected_chunk_ids=("run:x1",), rewritten_query="登録 適用条件"),
            attempt(2, rejected_chunk_ids=("run:x1",)),
            attempt(3, rejected_chunk_ids=("run:x1",)),
        ]
        with (
            patch.object(a, "build_adb_hybrid_answer_context", side_effect=lambda *args, **kwargs: next(contexts)),
            patch.object(a, "_grade_crag_retrieval", side_effect=grades),
        ):
            result, _, attempts = a.build_crag_answer_context(
                "登録条件は", "source", ["docling"], get_settings(environ={}, dotenv_path=None)
            )
        self.assertEqual([r.id for r in result.records][-1], "x1")
        self.assertIn("p1", [r.id for r in result.records])
        self.assertEqual(attempts[0].to_payload()["rejected_chunk_ids"], ["run:x1"])

    def test_grader_verdicts_map_false_candidates_and_their_anchor_children_to_rejected_ids(self):
        from rag_engine.models.llm import CragCandidateVerdict, CragRetrievalGradeOutput

        output = CragRetrievalGradeOutput(
            candidate_verdicts=[
                CragCandidateVerdict(chunk_uid="run:p1", relevant=True, reason="対象"),
                CragCandidateVerdict(chunk_uid="run:x1", relevant=False, reason="別業務"),
                CragCandidateVerdict(chunk_uid="run:zz", relevant=False, reason="不明な id"),
            ],
            aspect_checks=[],
            reason="r",
            sufficient=True,
            confidence=0.9,
            rewritten_query="",
        )
        with patch.object(a, "parse_text_response", return_value=output):
            grade = a._grade_crag_retrieval(
                1, "登録条件は", "登録条件は", ("登録条件は",), self.tree_context, get_settings(environ={}, dotenv_path=None)
            )
        self.assertEqual(grade.relevant_chunk_ids, ("run:p1", "run:c1"))
        self.assertEqual(grade.rejected_chunk_ids, ("run:x1",))


if __name__ == "__main__":
    unittest.main()


class CauseRoutingTests(unittest.TestCase):
    """評価の結果の原因で CRAG の回復を分ける（#3755）。評価器・検索はスタブで、モデルの呼び出しを数える。"""

    QUESTION = "登録条件は"
    REWRITE = "登録の確認画面の操作"

    def setUp(self):
        self.child = record("c1", 1, "入力画面を開く。")
        self.next = record("c2", 2, "登録後は再ログインする。")
        self.parent = record("p1", 1, "入力画面を開く。登録後は再ログインする。", "parent", "")
        self.other = record("c9", 9, "確認画面で登録を確定する。", parent="p9")
        self.pool = (self.child, self.next, self.parent)

    def context(self, *records):
        return a.AnswerContext(list(records), "\n".join(r.text for r in records), expansion_records=self.pool)

    def grade(self, number, *, sufficient=False, **fields):
        fields.setdefault("relevant_chunk_ids", ("run:c1",))
        fields.setdefault("grade_execution", "completed")
        fields.setdefault("rewritten_query", self.REWRITE)
        return a.CragRetrievalAttempt(number, self.QUESTION, (self.QUESTION,), sufficient, **fields)

    def run_crag(self, grades, searches, budget=None):
        from rag_engine.retrieval.extra_retrieval import ExtraRetrievalBudget

        budget = budget or ExtraRetrievalBudget(max_queries=3)
        with (
            patch.object(a, "build_adb_hybrid_answer_context", side_effect=searches) as search,
            patch.object(a, "_grade_crag_retrieval", side_effect=grades) as grader,
            # 評価器のほかにモデルを呼ばない（呼べば失敗する）。
            patch.object(a, "call_with_model_purpose", side_effect=AssertionError("unexpected model call")),
        ):
            result, rewrites, attempts = a.build_crag_answer_context(
                self.QUESTION, "source", ["docling"], get_settings(environ={}, dotenv_path=None), extra_budget=budget
            )
        return result, rewrites, attempts, search.call_count, grader.call_count, budget

    def test_unverified_only_does_not_spend_the_rewrite_budget_and_expands_in_pool(self):
        # 列挙の写しの照合の失敗などで未確認の観点だけ。評価器は書き換えを選んだが、探し直さずに pool で補う。
        grades = [self.grade(1, unknown_aspects=("enumeration_coverage",), recovery_action="rewrite")]
        result, rewrites, attempts, searches, grader_calls, budget = self.run_crag(grades, [self.context(self.child)])

        self.assertEqual((searches, grader_calls), (1, 1))
        self.assertEqual(rewrites, ())
        self.assertEqual(budget.used, {})
        self.assertEqual(attempts[-1].recovery_cause, "unverified")
        self.assertEqual(attempts[-1].stop_reason, "expanded_for_unverified_aspects")
        self.assertEqual(attempts[-1].recovery_trace["cause"], "unverified")
        self.assertEqual(attempts[-1].recovery_trace["trigger"], "unverified_aspects")
        self.assertIn(self.next.text, result.text)
        self.assertEqual(attempts[-1].to_payload()["recovery_cause"], "unverified")

    def test_unverified_only_keeps_the_pool_when_nothing_can_be_added(self):
        # pool に前後が無い（補えない）ときも探し直さず、取得済みの根拠のまま進む。
        grades = [
            self.grade(
                1,
                unknown_aspects=("procedure",),
                relevant_chunk_ids=("run:c9",),
                recovery_action="parent",
                recovery_anchor_ids=("run:c9",),
            )
        ]
        isolated = a.AnswerContext([self.other], self.other.text, expansion_records=(self.other,))
        result, rewrites, attempts, searches, grader_calls, budget = self.run_crag(grades, [isolated])

        self.assertEqual((searches, grader_calls, rewrites, budget.used), (1, 1, (), {}))
        self.assertEqual(attempts[-1].stop_reason, "unverified_aspects_retained")
        self.assertEqual([r.chunk_uid for r in result.records], ["run:c9"])

    def test_incomplete_grade_is_unverified(self):
        # 観点の判定が欠けた（partial）だけでは不足と確かめていない。
        grades = [self.grade(1, grade_execution="partial", recovery_action="rewrite")]
        _, rewrites, attempts, searches, grader_calls, _ = self.run_crag(grades, [self.context(self.child)])
        self.assertEqual((searches, grader_calls, rewrites), (1, 1, ()))
        self.assertEqual(attempts[-1].recovery_cause, "unverified")

    def test_verified_shortfall_keeps_the_rewrite_search(self):
        grades = [
            self.grade(1, missing_aspects=("requested_result",), unknown_aspects=("procedure",), recovery_action="rewrite"),
            self.grade(2, sufficient=True, relevant_chunk_ids=("run:c9",)),
        ]
        searches = [self.context(self.child), self.context(self.other)]
        _, rewrites, attempts, search_calls, grader_calls, budget = self.run_crag(grades, searches)

        self.assertEqual((search_calls, grader_calls), (2, 2))
        self.assertEqual(rewrites, (self.REWRITE,))
        self.assertEqual(budget.used, {"crag": 1})
        self.assertEqual(attempts[0].recovery_cause, "verified_shortfall")
        self.assertEqual(attempts[-1].recovery_cause, "sufficient")

    def test_unusable_or_irrelevant_evidence_keeps_the_existing_recovery(self):
        for cause, fields in [
            ("generation_unusable", {}),
            ("no_relevant_evidence", {"relevant_chunk_ids": (), "unknown_aspects": ("procedure",)}),
        ]:
            with self.subTest(cause=cause):
                grades = [
                    self.grade(1, recovery_action="rewrite", **fields),
                    self.grade(2, sufficient=True, relevant_chunk_ids=("run:c9",)),
                ]
                searches = [self.context(self.child), self.context(self.other)]
                _, rewrites, attempts, search_calls, grader_calls, budget = self.run_crag(grades, searches)
                self.assertEqual((search_calls, grader_calls, rewrites), (2, 2, (self.REWRITE,)))
                self.assertEqual(budget.used, {"crag": 1})
                self.assertEqual(attempts[0].recovery_cause, cause)

    def test_grade_error_is_recorded_with_its_own_cause(self):
        result, rewrites, attempts, searches, grader_calls, budget = self.run_crag(RuntimeError("unavailable"), [self.context(self.child)])
        self.assertEqual((searches, grader_calls, rewrites, budget.used), (1, 1, (), {}))
        self.assertEqual(attempts[-1].recovery_cause, "grade_error")
        self.assertEqual(attempts[-1].to_payload()["grade_execution"], "error")
        self.assertTrue(result.records)

    def test_cause_prefers_missing_over_unknown(self):
        base = self.grade(1)
        self.assertEqual(a.crag_recovery_cause(replace(base, missing_aspects=("procedure",), unknown_aspects=("x",))), "verified_shortfall")
        self.assertEqual(a.crag_recovery_cause(replace(base, sufficient=True)), "sufficient")
        self.assertEqual(a.crag_recovery_cause(replace(base, sufficient=True, unknown_aspects=("x",))), "unverified")
        self.assertEqual(a.crag_recovery_cause(replace(base, grade_error="RuntimeError")), "grade_error")
