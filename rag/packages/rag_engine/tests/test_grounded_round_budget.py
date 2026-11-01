"""生成と監査の是正の round の時間の配分（#1516）を、LLM と時刻を差し替えて確かめる。

回答の期限の残りが 1 組の見積もり（直前の組の実測）と確定の分に足りなければ是正をせずに確定し、round の途中で
確定の分を残した期限が切れても error にせず、確かめ終わった分で条件つきの回答を返す。
"""

import time
import unittest
from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import patch

from test_grounded_answer import LIST_TEXT, FakeModel, audit, context, draft, ids, item, marks, record, run

from rag_engine.adapters import model_api
from rag_engine.generation import execution_record, grounded
from rag_engine.models.llm import GroundedAudit, GroundedDraft
from rag_engine.resources import deadline as deadlines
from rag_engine.resources.deadline import Deadline, DeadlineExceeded, bind_deadline, check_deadline
from rag_engine.resources.llm_usage import LlmUsage, llm_usage, record_llm_usage, usage_line

GOOD = "一覧表を選択し、処理を選択します。"


def _context():
    ctx = context(record(0, LIST_TEXT, "売上-(3)年次集計明細作成"))
    return ctx, ids(ctx)[LIST_TEXT]


def _draft_with_dropped(eid):
    """公開できる説明 1 件と、原文と一致しない引用 1 件（是正の指摘が残る）。"""
    return draft(item(eid, GOOD, GOOD), item(eid, "CSVを保存先へ出力します。", "CSVを C:\\temp へ出力します。"))


class _Clock:
    """回答の期限の残り（`remaining_seconds`）と round の秒（grounded の `monotonic`）を同じ偽の時刻で進める。"""

    def __init__(self, limit):
        self.limit, self.now = limit, 0.0

    def remaining(self):
        return max(0.0, self.limit - self.now)


def _drafts(model):
    return [prompt for name, prompt in model.prompts if name == "GroundedDraftWire"]


class RoundBudgetUnitTest(unittest.TestCase):
    def test_estimate_is_the_last_measured_round_or_the_default(self):
        budget = grounded.RoundBudget(lambda: 1000.0)
        self.assertEqual(budget.estimate(), deadlines.DEFAULT_GENERATION_ROUND_SECONDS)
        budget.seconds.extend([300.0, 120.0])
        self.assertEqual(budget.estimate(), 120.0)

    def test_allows_round_keeps_the_finalize_reserve(self):
        budget = grounded.RoundBudget(lambda: 230.0, seconds=[200.0])
        # 1 組目は草稿が無いので常に始める。
        self.assertTrue(budget.allows_round(0))
        # 残り 230 − 確定 30 = 200 ≧ 見積もり 200。
        self.assertTrue(budget.allows_round(1))
        budget.seconds.append(201.0)
        self.assertFalse(budget.allows_round(2))
        self.assertEqual(
            budget.stop,
            {
                "reason": grounded.DEADLINE_BEFORE_ROUND,
                "round": 3,
                "remaining_seconds": 230.0,
                "estimate_seconds": 201.0,
                "reserve_seconds": 30.0,
            },
        )

    def test_without_deadline_nothing_is_limited(self):
        budget = grounded.RoundBudget(lambda: None)
        self.assertTrue(budget.allows_round(2))
        with budget.round():
            self.assertIsNone(deadlines.current_deadline())
        self.assertFalse(budget.cut_round(0, has_draft=False))
        self.assertEqual(budget.stop, {})

    def test_round_deadline_leaves_the_finalize_reserve(self):
        budget = grounded.RoundBudget(lambda: 100.0)
        with budget.round():
            remaining = deadlines.remaining_seconds()
        self.assertIsNotNone(remaining)
        self.assertLessEqual(remaining, 70.0)
        self.assertGreater(remaining, 60.0)

    def test_pending_feedback_labels(self):
        labels = grounded.pending_feedback_labels({"unanswered_requests": [{}, {}], "dropped_items": [{}], "goal_alignment": "partial"})
        self.assertEqual(labels, ["答えていない要求 2 件", "原文と一致しない引用 1 件", "質問の目的とのずれ"])


class DeadlineDocumentQuotesTest(unittest.TestCase):
    """1 組目の生成が切れたときに示す根拠の抜粋（#1548）。"""

    @staticmethod
    def _span(eid, text, *section, **extra):
        return {"evidence_id": eid, "text": text, "section_path": list(section), "tags": [], **extra}

    def test_question_terms_first_then_evidence_order_up_to_the_limit(self):
        question = "サーバー室の定期点検の記録には、何を書きますか？"
        spans = [
            self._span("E1", "点検の前に、入室の申請をします。", "サーバー室の定期点検", "第 2 章 入室"),
            self._span("E2", "第 5 章 点検の記録"),  # 見出しだけ
            self._span("E3", "画面の説明です。", tags=["image_generated"]),
            self._span("E4", "点検の記録には、点検の日時と点検者の氏名を書きます。", "サーバー室の定期点検", "第 5 章 点検の記録"),
            self._span("E5", "空調の温度を確かめます。", "第 3 章 空調"),
            self._span("E6", "UPS の表示を確かめます。", "第 3 章 電源"),
        ]
        quotes = grounded.deadline_document_quotes(question, spans, limit=3)
        # 質問の語（サーバー室・定期点検・記録）を多く含む E4・E1 が先、続けて根拠の順（E2・E3 は除く）。
        self.assertEqual([entry.span["evidence_id"] for entry in quotes], ["E4", "E1", "E5"])
        self.assertTrue(all(entry.quote_only and entry.item.applies == "unverified" for entry in quotes))
        self.assertEqual(grounded.deadline_document_quotes(question, []), [])


class DeadlineCutShortTest(unittest.TestCase):
    def test_child_deadline_cut_short_only_while_the_outer_deadline_is_alive(self):
        outer = Deadline(100.0)
        child = Deadline(0.0, parent=outer)
        self.assertTrue(child.cut_short)
        outer.cancel()
        # 外側が取り消された（利用者が止めた）ときは、確かめ終わった分で確定しない。
        self.assertFalse(child.cut_short)
        self.assertFalse(Deadline(100.0).cut_short)
        self.assertFalse(Deadline(None).cut_short)


class SynthesizeRoundBudgetTest(unittest.TestCase):
    def run_with_clock(self, model, ctx, clock):
        with patch.object(deadlines, "remaining_seconds", clock.remaining), patch.object(grounded, "monotonic", lambda: clock.now):
            return run(model, ctx)

    def test_stops_before_a_correction_round_that_does_not_fit(self):
        ctx, eid = _context()
        clock = _Clock(600.0)

        def slow_draft(prompt):
            clock.now += 250.0  # 生成 250 秒
            return _draft_with_dropped(eid)

        def slow_audit(prompt):
            clock.now += 100.0  # 監査 100 秒
            return audit((0, "supported", "matched", ""))

        model = FakeModel([slow_draft], [slow_audit])
        result = self.run_with_clock(model, ctx, clock).response

        # 1 組目の後: 残り 250 − 確定 30 = 220 秒 < 見積もり（1 組目の実測）350 秒。是正の round を始めない。
        self.assertEqual(len(_drafts(model)), 1)
        budget = result.generation_trace["round_budget"]
        self.assertEqual(budget["round_seconds"], [350.0])
        # round の所要と、次へ渡す是正の指摘の量を残す（遅い round の理由を確かめる）。
        self.assertEqual(budget["rounds"][0]["feedback"], ["原文と一致しない引用 1 件"])
        self.assertGreater(budget["rounds"][0]["feedback_chars"], 0)
        self.assertGreater(budget["rounds"][0]["prompt_chars"], 0)
        self.assertEqual(budget["stop"]["reason"], grounded.DEADLINE_BEFORE_ROUND)
        self.assertEqual(result.generation_trace["rounds"][0]["deadline_stop"]["estimate_seconds"], 350.0)
        # 確かめ終わった説明は公開し、直していない不足を「回答時間の上限により省略した処理」に出して条件つきにする。
        self.assertIn(GOOD, result.answer_text)
        # 原文と一致しない引用の説明は削除せず印を付ける（#2738）。
        self.assertEqual(marks(result)["CSVを保存先へ出力します。"], ["quote_unmatched"])
        self.assertIn(grounded.answer_labels().deadline_section + "\n\n・回答の時間の上限が近いため", result.answer_text)
        # 章節の節とは分ける（#2040）。
        self.assertNotIn(grounded.answer_labels().unverified_section, result.answer_text)
        self.assertIn("原文と一致しない引用 1 件）を直す作り直しをしていません", result.answer_text)
        self.assertEqual(result.envelope["outcome"], "conditional")
        self.assertTrue(result.needs_human_review)

    def test_correction_round_runs_when_time_remains(self):
        ctx, eid = _context()
        clock = _Clock(600.0)

        def fast_draft(prompt):
            clock.now += 50.0
            return _draft_with_dropped(eid)

        model = FakeModel([fast_draft], [audit((0, "supported", "matched", ""))])
        result = self.run_with_clock(model, ctx, clock).response

        # 残りが十分なら従来どおり是正する（同じ指摘だけの是正は 1 回で打ち切る）。時間の打ち切りは無い。
        self.assertEqual(len(_drafts(model)), 2)
        self.assertEqual(result.generation_trace["round_budget"]["stop"]["reason"], "max_correction_rounds")
        self.assertEqual(result.generation_trace["round_budget"]["stop"]["scope"], "content_correction")
        self.assertNotIn("作り直しをしていません", result.answer_text)

    def test_first_generation_cut_returns_related_quotes_instead_of_error(self):
        ctx, _eid = _context()
        clock = _Clock(30.0)  # 残りは確定の分だけ: round の期限は 0 秒

        def cut_draft(prompt):
            check_deadline()  # 本番の LLM の呼び出しと同じく、期限を過ぎていれば DeadlineExceeded
            raise AssertionError("期限が切れているはず")

        model = FakeModel([cut_draft], [audit()])
        result = self.run_with_clock(model, ctx, clock).response

        self.assertTrue(result.answer_text.startswith(grounded.answer_labels().deadline_summary))
        self.assertIn("回答文の生成が終わらなかったため", result.answer_text)
        self.assertEqual(result.generation_trace["round_budget"]["stop"]["reason"], grounded.DEADLINE_NO_DRAFT)
        self.assertTrue(result.generation_trace["rounds"][0]["deadline_cut"])
        self.assertEqual(result.confidence, "low")
        # 根拠は回答に渡すと決めた後で、草稿が無いのは時間の上限のため。拒答（insufficient_evidence）にせず、
        # 根拠の抜粋を原文のみ提示（適用は未確認）で示す条件つきの回答にする（#1548）。質問の語を 2 つ含む原文が
        # 無くても、根拠の順で抜粋を示す。
        self.assertEqual(result.envelope["outcome"], "conditional")
        self.assertEqual([entry["applies"] for entry in result.envelope["items"]], ["quote_only"])
        self.assertIn("一覧表を選択し、処理を選択します。", result.envelope["items"][0]["text"])
        self.assertIn("一覧表を選択し、処理を選択します。", result.answer_text)
        self.assertTrue(result.needs_human_review)
        # 監査は呼ばない（草稿が無い）。
        self.assertEqual([name for name, _ in model.prompts], ["GroundedDraftWire"])

    def test_first_generation_capped_by_the_deadline_keeps_the_cut_call_usage(self):
        """1 組目の生成が、期限の残りで抑えた出力の上限に届いて切れた（#1548）。秒・token を round_budget に残す。"""
        ctx, _eid = _context()
        clock = _Clock(600.0)

        def capped_draft(prompt):
            # adapter（`_openai_parsed`）と同じく、抑えた上限で未完了なら今の期限を切って DeadlineExceeded にする。
            clock.now += 520.0
            record_llm_usage(
                {
                    "input_tokens": 13000,
                    "output_tokens": 18000,
                    "reasoning_tokens": 15000,
                    "seconds": 520.0,
                    "incomplete_reason": "max_output_tokens",
                }
            )
            deadlines.expire_deadline()
            raise DeadlineExceeded("回答の期限までに LLM の出力が終わりません")

        model = FakeModel([capped_draft], [audit()])
        usage_token = llm_usage.set(LlmUsage())
        try:
            result = self.run_with_clock(model, ctx, clock).response
        finally:
            llm_usage.reset(usage_token)

        stop = result.generation_trace["round_budget"]["stop"]
        self.assertEqual(stop["reason"], grounded.DEADLINE_NO_DRAFT)
        self.assertEqual(
            (stop["usage"]["output_tokens"], stop["usage"]["reasoning_tokens"], stop["usage"]["seconds"]), (18000, 15000, 520.0)
        )
        self.assertEqual(stop["usage"]["incomplete_reasons"], {"max_output_tokens": 1})
        self.assertEqual(result.envelope["outcome"], "conditional")

    def test_audit_cut_keeps_the_verified_draft(self):
        ctx, eid = _context()
        clock = _Clock(30.05)  # round の期限は 0.05 秒（生成は間に合い、監査で切れる）

        def cut_audit(prompt):
            time.sleep(0.1)
            check_deadline()
            raise AssertionError("期限が切れているはず")

        def quick_draft(prompt):
            clock.now += 1.0  # 1 組目の実測 1 秒 > 残り 0.05 秒: 監査が切れた後に是正の round を始めない
            return draft(item(eid, GOOD, GOOD))

        model = FakeModel([quick_draft], [cut_audit])
        result = self.run_with_clock(model, ctx, clock).response

        rounds = result.generation_trace["rounds"]
        self.assertEqual(len(rounds), 1)
        self.assertTrue(rounds[0]["audit_cut"])
        self.assertIsNone(rounds[0]["audit"])
        self.assertTrue(result.generation_trace["round_budget"]["adopted_audit_cut"])
        # 監査未完了を本文の印や資料不足へ変換せず、草稿と実際の時間切れ診断を残す。
        self.assertIn(GOOD, result.answer_text)
        self.assertNotIn(GOOD, marks(result))
        self.assertTrue(result.answer_text.startswith(rounds[0]["draft"]["summary"]))
        self.assertEqual(result.generation_trace["finalization"]["summary"]["status"], "kept")
        self.assertIn("確認（監査）が終わらなかったため", result.answer_text)
        self.assertNotEqual(result.envelope["outcome"], "insufficient_evidence")

    def test_audit_cut_correction_round_does_not_replace_the_audited_round(self):
        """是正の round の監査が切れたら、その round（監査なし）を、監査を通った round より採用しない。"""
        ctx, eid = _context()
        values = iter([600.0, 600.0])  # 1 組目の round・2 組目の判定 → 2 組目の round は 0.05 秒で切れる

        def remaining():
            return next(values, 30.05)

        def cut_audit(prompt):
            time.sleep(0.1)
            check_deadline()
            raise AssertionError("期限が切れているはず")

        more = draft(item(eid, GOOD, GOOD), item(eid, "実行ボタンを押して完了します。", "実行ボタンを押して完了します。"))
        model = FakeModel([_draft_with_dropped(eid), more], [audit((0, "supported", "matched", "")), cut_audit])
        with patch.object(deadlines, "remaining_seconds", remaining):
            result = run(model, ctx).response

        rounds = result.generation_trace["rounds"]
        self.assertEqual([(r["accepted"], r["audit_cut"]) for r in rounds], [(True, False), (False, True)])
        self.assertNotIn("実行ボタンを押して完了します。", result.answer_text.split(grounded.answer_labels().quote_only_section)[0])
        self.assertNotIn("確認（監査）が終わらなかったため", result.answer_text)

    def test_correction_round_cut_keeps_the_adopted_round(self):
        ctx, eid = _context()
        values = iter([600.0, 30.0])  # 1 組目の round・2 組目の判定 → 以後は確定の分だけ

        def remaining():
            return next(values, 30.0)

        def second_draft_cut(prompt):
            check_deadline()
            raise AssertionError("期限が切れているはず")

        model = FakeModel([_draft_with_dropped(eid), second_draft_cut], [audit((0, "supported", "matched", ""))])
        with patch.object(deadlines, "remaining_seconds", remaining), patch.object(grounded.RoundBudget, "estimate", lambda self: 0.0):
            result = run(model, ctx).response

        rounds = result.generation_trace["rounds"]
        self.assertEqual([r["accepted"] for r in rounds], [True, False])
        self.assertTrue(rounds[1]["deadline_cut"])
        self.assertEqual(result.generation_trace["round_budget"]["stop"]["reason"], grounded.DEADLINE_IN_ROUND)
        self.assertIn(GOOD, result.answer_text)
        self.assertIn("を直す作り直しをしていません", result.answer_text)

    def test_outer_deadline_cancel_is_not_absorbed(self):
        ctx, _eid = _context()
        outer = Deadline(100.0)
        outer.cancel()

        def cancelled_draft(prompt):
            check_deadline()
            raise AssertionError("取り消されているはず")

        model = FakeModel([cancelled_draft], [audit()])
        with bind_deadline(outer), self.assertRaises(DeadlineExceeded):
            run(model, ctx)


class LlmUsageTest(unittest.TestCase):
    def test_usage_is_recorded_per_step(self):
        steps: list = []
        step_token = execution_record._structured_steps.set(steps)
        usage_token = llm_usage.set(LlmUsage())
        try:
            with execution_record._execution_step("生成"):
                record_llm_usage(
                    {"input_tokens": 1200, "cached_tokens": 200, "output_tokens": 300, "reasoning_tokens": 250, "seconds": 12.34}
                )
                # 並列の呼び出し（抽出）は文脈を写した thread から同じ累積に足す。
                copy_context().run(record_llm_usage, {"input_tokens": 100, "output_tokens": 10, "seconds": 1.0})
            with execution_record._execution_step("LLM なし"):
                pass
        finally:
            llm_usage.reset(usage_token)
            execution_record._structured_steps.reset(step_token)
        self.assertEqual(
            steps[0]["llm_usage"],
            {"calls": 2, "input_tokens": 1300, "cached_tokens": 200, "output_tokens": 310, "reasoning_tokens": 250, "seconds": 13.3},
        )
        self.assertNotIn("llm_usage", steps[1])
        self.assertEqual(usage_line(steps[0]["llm_usage"]), "入力 1,300 tokens（cache 200）・出力 310 tokens（推論 250）・13.3 秒")

    def test_adapter_reads_responses_usage(self):
        response = SimpleNamespace(
            status="completed",
            usage=SimpleNamespace(
                input_tokens=5000,
                input_tokens_details=SimpleNamespace(cached_tokens=1000),
                output_tokens=800,
                output_tokens_details=SimpleNamespace(reasoning_tokens=600),
            ),
        )
        usage_token = llm_usage.set(LlmUsage())
        try:
            with self.assertLogs(model_api.__name__, level="INFO") as logs:
                model_api._record_usage(response, SimpleNamespace(model="m"), GroundedDraft, 42.0)
            snapshot = llm_usage.get().snapshot()
        finally:
            llm_usage.reset(usage_token)
        self.assertEqual(
            snapshot,
            {"calls": 1, "input_tokens": 5000, "cached_tokens": 1000, "output_tokens": 800, "reasoning_tokens": 600, "seconds": 42.0},
        )
        self.assertIn("schema=GroundedDraft seconds=42.0 input_tokens=5000", logs.output[0])
        # backend の構造化 log は外部の logger の本文を出さないので、値は extra の項目でも渡す。
        logged = logs.records[0]
        self.assertEqual(
            (logged.event, logged.llm_schema, logged.llm_seconds, logged.input_tokens, logged.reasoning_tokens),
            ("llm_call_usage", "GroundedDraft", 42.0, 5000, 600),
        )
        # usage の無い応答（スタブ・古い endpoint）でも失敗しない。
        model_api._record_usage(SimpleNamespace(status="completed"), SimpleNamespace(model="m"), GroundedAudit, 1.0)

    def test_round_timing_line(self):
        current = SimpleNamespace(
            audit_cut=True,
            timing={
                "generation_seconds": 200.0,
                "audit_seconds": 30.0,
                "generation_usage": {"calls": 1, "input_tokens": 9000, "output_tokens": 4000, "reasoning_tokens": 3000, "seconds": 200.0},
            },
        )
        self.assertEqual(
            grounded.round_timing_line(current),
            "所要: 生成 200.0 秒（入力 9,000 tokens・出力 4,000 tokens（推論 3,000）・200.0 秒）、監査 30.0 秒、時間の上限で打ち切り",
        )


if __name__ == "__main__":
    unittest.main()
