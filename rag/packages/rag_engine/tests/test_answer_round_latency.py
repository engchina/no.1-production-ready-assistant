"""回答文の生成と根拠確認の時間の短縮（#1912）を、LLM を差し替えて確かめる。

- 作り直した後に残った指摘が降格（原文のまま示した説明）だけなら、次の組を始めない。
- 是正の組の回数の上限（`answer_max_correction_rounds`）で止める。
- 監査の通った判定の reason を空にさせる規則（`answer_audit_compact_reasons`）を監査の system prompt に足す。
"""

import unittest
from dataclasses import replace

from test_grounded_answer import LIST_TEXT, QUESTION, FakeModel, audit, context, draft, ids, item, observed_audit, record

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import answering, grounded
from rag_engine.generation.answering import synthesize_grounded_answer
from rag_engine.models.audit_wire import is_audit_wire
from rag_engine.models.llm import GroundedAudit

GOOD = "一覧表を選択し、処理を選択します。"
DONE = "実行ボタンを押して完了します。"


def _run(model, ctx, **overrides):
    def unused(*a, **k):
        return (_ for _ in ()).throw(AssertionError("unexpected I/O"))

    settings = replace(get_settings(environ={}, dotenv_path=None), **overrides)
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        return synthesize_grounded_answer(QUESTION, ctx, settings, image_prompt_mode="text_only").response


def _drafts(model):
    return [prompt for name, prompt in model.prompts if name == "GroundedDraftWire"]


class SystemCapturingModel(FakeModel):
    """FakeModel に、schema ごとの system prompt の記録を足す。"""

    def __init__(self, drafts, audits):
        super().__init__(drafts, audits)
        self.systems = []

    def __call__(self, system, prompt, settings, schema, **options):
        self.systems.append(((schema.canonical_schema if is_audit_wire(schema) else schema).__name__, system))
        return super().__call__(system, prompt, settings, schema, **options)


class DowngradedOnlyUnitTest(unittest.TestCase):
    def test_downgraded_only(self):
        self.assertTrue(grounded.downgraded_only({"downgraded_items": [{}], "supported_items": [{}]}))
        self.assertFalse(grounded.downgraded_only({}))
        for key, value in (
            ("dropped_items", [{}]),
            ("unanswered_requests", [{"request_id": "Q1", "status": "partial"}]),
            ("unused_evidence_ids", [{"evidence_id": "e"}]),
            ("goal_alignment", "partial"),
            ("no_cited_evidence", "x"),
        ):
            with self.subTest(key=key):
                self.assertFalse(grounded.downgraded_only({"downgraded_items": [{}], key: value}))

    def test_downgraded_only_stops_only_after_a_correction(self):
        previous = {"unused_evidence_ids": [{"evidence_id": "e"}], "downgraded_items": [{}]}
        down = {"downgraded_items": [{}]}
        # 1 組目の後は、付け足した前提を直せることがあるので作り直す。
        self.assertEqual(grounded.no_improvement_reason(0, accepted=True, feedback=down, previous=None), "")
        self.assertEqual(
            grounded.no_improvement_reason(1, accepted=True, feedback=down, previous=previous), grounded.NO_IMPROVEMENT_DOWNGRADED_ONLY
        )
        # 写し誤りなど内容の指摘が残るなら作り直す。
        mixed = {**down, "dropped_items": [{}]}
        self.assertEqual(grounded.no_improvement_reason(1, accepted=True, feedback=mixed, previous=previous), "")
        # 採用されなかった・同じ指摘の理由が先。
        self.assertEqual(
            grounded.no_improvement_reason(1, accepted=False, feedback=down, previous=previous), grounded.NO_IMPROVEMENT_NOT_ACCEPTED
        )
        self.assertEqual(
            grounded.no_improvement_reason(1, accepted=True, feedback=down, previous=down), grounded.NO_IMPROVEMENT_SAME_FEEDBACK
        )
        self.assertIn(grounded.NO_IMPROVEMENT_DOWNGRADED_ONLY, answering._NO_IMPROVEMENT_RESULTS)


class DowngradedOnlySynthesizeTest(unittest.TestCase):
    def test_material_observation_does_not_start_context_or_correction_rounds(self):
        """未校正の材料不足判定だけで文脈追加や作り直しを始めない。"""
        hidden_text = "抽出条件で店舗を指定します。\n抽出結果を保存します。"
        shown = replace(record(0, "●集計表出力\n一覧表を選択します。", "売上-(3)年次集計明細作成"), chunk_uid="run:r0")
        hidden = replace(record(1, hidden_text, "売上-(3)年次集計明細作成", page=21), chunk_uid="run:r1", seq_no=2)
        ctx = replace(context(shown), expansion_records=(shown, hidden))
        first = draft(item(ids(ctx)[shown.text], "一覧表を選択します。", "一覧表を選択します。", request_id="Q1"))

        def second(prompt):
            # 足した資料の根拠の ID は、広げた後の context の span から引く。
            expanded = ids(context(shown, hidden))
            return draft(
                item(expanded[shown.text], "一覧表を選択します。", "一覧表を選択します。", request_id="Q1"),
                item(expanded[hidden_text], "抽出条件で店舗を指定します。", "抽出条件で店舗を指定します。", request_id="Q1"),
                item(expanded[hidden_text], "抽出結果を CSV で共有フォルダへ保存します。", "抽出結果を保存します。", request_id="Q1"),
            )

        model = FakeModel(
            [first, second, second],
            [
                audit(
                    (0, "supported", "matched", ""),
                    requests=[
                        {
                            "request_id": "Q1",
                            "status": "partial",
                            "reason": "抽出条件の原文が未取得",
                            "evidence_status": "missing",
                            "evidence_ids": [],
                        }
                    ],
                ),
                audit(
                    (0, "supported", "matched", ""),
                    (1, "supported", "matched", ""),
                    (2, "unsupported", "matched", ""),
                    requests=[("Q1", "addressed")],
                ),
            ],
        )
        result = _run(model, ctx)
        rounds = result.generation_trace["rounds"]
        self.assertEqual(len(_drafts(model)), 1)
        self.assertEqual([r["accepted"] for r in rounds], [True])
        self.assertNotIn("remedy", rounds[0])
        self.assertNotIn("stop", result.generation_trace["round_budget"])
        self.assertEqual(observed_audit(result)["request_reviews"][0]["evidence_status"], "missing")
        self.assertFalse(result.envelope["passage_marks"])
        self.assertIn(first["items"][0]["text"], result.answer_text)
        self.assertNotIn("抽出結果を CSV で共有フォルダへ保存します。", result.answer_text)


class MaxCorrectionRoundsTest(unittest.TestCase):
    def _miscopied(self):
        ctx = context(record(0, LIST_TEXT, "売上-(3)年次集計明細作成"))
        eid = ids(ctx)[LIST_TEXT]
        wrong = draft(item(eid, GOOD, GOOD), item(eid, "CSVを保存先へ出力します。", "CSVを C:\\temp へ出力します。"))
        fixed = draft(item(eid, GOOD, GOOD), item(eid, DONE, DONE))
        return ctx, FakeModel([wrong, fixed], [audit((0, "supported", "matched", ""), (1, "supported", "matched", ""))])

    def test_zero_correction_rounds_finalizes_the_first_round(self):
        ctx, model = self._miscopied()
        result = _run(model, ctx, answer_max_correction_rounds=0)
        self.assertEqual(len(_drafts(model)), 1)
        stop = result.generation_trace["round_budget"]["stop"]
        self.assertEqual(stop["reason"], answering.MAX_CORRECTION_ROUNDS_STOP)
        self.assertEqual(stop["round"], 2)
        self.assertIn(GOOD, result.answer_text)
        self.assertNotIn("作り直しをしていません", result.answer_text)

    def test_default_keeps_the_correction(self):
        ctx, model = self._miscopied()
        result = _run(model, ctx)
        self.assertEqual(len(_drafts(model)), 2)
        self.assertNotIn("stop", result.generation_trace["round_budget"])
        self.assertIn(DONE, result.answer_text)

    def test_engine_setting_is_clamped(self):
        self.assertEqual(get_settings(environ={}, dotenv_path=None).answer_max_correction_rounds, 2)
        for raw, expected in (("0", 0), ("1", 1), ("5", 2), ("-1", 0)):
            with self.subTest(raw=raw):
                env = {"RAG_ENGINE_ANSWER_MAX_CORRECTION_ROUNDS": raw}
                self.assertEqual(get_settings(environ=env, dotenv_path=None).answer_max_correction_rounds, expected)


class CompactAuditReasonsTest(unittest.TestCase):
    def test_prompt_rule(self):
        self.assertEqual(grounded.audit_system_prompt(), grounded.AUDIT_SYSTEM_PROMPT)
        compact = grounded.audit_system_prompt(compact_reasons=True)
        self.assertTrue(compact.startswith(grounded.AUDIT_SYSTEM_PROMPT))
        self.assertTrue(compact.endswith(grounded.AUDIT_COMPACT_REASON_RULE))
        # 規則が名指す値は監査の出力の型の値と同じ。
        for value in ("supported", "matched", "addressed", "unsupported", "contradicted", "conditional", "not_applicable", "unknown"):
            self.assertIn(value, grounded.AUDIT_COMPACT_REASON_RULE)
        # 空の reason は監査の出力として受け取れる。
        GroundedAudit.model_validate(
            {
                "reviews": [{"index": 0, "support": "supported", "applicability": "matched", "condition": "", "reason": ""}],
                "request_reviews": [{"request_id": "Q1", "status": "addressed", "reason": ""}],
                "summary_supported": True,
                "goal_alignment": "aligned",
            }
        )

    def test_setting_switches_the_audit_system_prompt(self):
        self.assertFalse(get_settings(environ={}, dotenv_path=None).answer_audit_compact_reasons)
        on = get_settings(environ={"RAG_ENGINE_ANSWER_AUDIT_COMPACT_REASONS": "1"}, dotenv_path=None)
        self.assertTrue(on.answer_audit_compact_reasons)
        for compact in (False, True):
            with self.subTest(compact=compact):
                ctx = context(record(0, LIST_TEXT, "売上-(3)年次集計明細作成"))
                eid = ids(ctx)[LIST_TEXT]
                model = SystemCapturingModel(
                    [draft(item(eid, GOOD, GOOD), item(eid, DONE, DONE))],
                    [audit((0, "supported", "matched", ""), (1, "supported", "matched", ""))],
                )
                _run(model, ctx, answer_audit_compact_reasons=compact)
                systems = [system for name, system in model.systems if name == "GroundedAudit"]
                self.assertEqual(systems, [grounded.audit_system_prompt(compact_reasons=compact)])


if __name__ == "__main__":
    unittest.main()
