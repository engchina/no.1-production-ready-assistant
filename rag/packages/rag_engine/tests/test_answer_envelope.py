"""回答の構造（AnswerEnvelope。#1235）。LLM を差し替え、回答の確定の分岐ごとに対応を確かめる。"""

import json
import unittest
from pathlib import Path

from test_grounded_answer import LIST_TEXT, FakeModel, audit, context, draft, ids, item, record, run

from rag_engine.generation.answer_envelope import ENVELOPE_SCHEMA_VERSION, build_envelope, envelope_requests, fallback_envelope


class AnswerEnvelopeFromGroundedAnswerTest(unittest.TestCase):
    def test_observed_supported_items_keep_body_with_unverified_requests(self):
        ctx = context(record(0, LIST_TEXT, "売上-(3)年次集計明細作成"))
        eid = ids(ctx)[LIST_TEXT]
        steps = draft(item(eid, "一覧表を選択し、処理を選択します。", "一覧表を選択し、処理を選択します。", request_id="Q1"))
        envelope = run(
            FakeModel(
                [steps],
                [audit((0, "supported", "matched", ""), requests=[("Q1", "addressed"), ("Q1.M1", "addressed"), ("Q1.M2", "addressed")])],
            ),
            ctx,
        ).response.envelope
        self.assertEqual(envelope["schema_version"], ENVELOPE_SCHEMA_VERSION)
        self.assertEqual(envelope["outcome"], "conditional")
        self.assertEqual(envelope["requests"][0]["status"], "unknown")
        self.assertEqual(envelope["items"][0]["kind"], "operation")
        self.assertEqual(envelope["items"][0]["evidence_id"], eid)
        self.assertEqual(envelope["items"][0]["location"], "p.20")
        self.assertEqual(envelope["items"][0]["mark"], "")
        self.assertEqual(envelope["handoff_reasons"], ["unverified_requests"])
        self.assertEqual((envelope["passage_marks"], envelope["removed_passages"], envelope["answer_warnings"]), ([], [], []))

    def test_observed_condition_does_not_rewrite_generated_item(self):
        ctx = context(record(0, LIST_TEXT, "売上-(3)年次集計明細作成"))
        eid = ids(ctx)[LIST_TEXT]
        steps = draft(item(eid, "一覧表を選択し、処理を選択します。", "一覧表を選択し、処理を選択します。", request_id="Q1"))
        conditional = audit((0, "supported", "conditional", "処理を選択"), requests=[("Q1", "addressed")])
        envelope = run(FakeModel([steps], [conditional]), ctx).response.envelope
        self.assertEqual(envelope["outcome"], "conditional")
        self.assertEqual(envelope["conditions"], [])
        self.assertEqual(envelope["items"][0]["applies"], "matched")

    def test_observed_missing_request_remains_unverified_and_listed(self):
        ctx = context(record(0, LIST_TEXT, "売上-(3)年次集計明細作成"))
        eid = ids(ctx)[LIST_TEXT]
        steps = draft(item(eid, "一覧表を選択し、処理を選択します。", "一覧表を選択し、処理を選択します。", request_id="Q1"))
        partial = audit((0, "supported", "matched", ""), requests=[("Q1", "missing", "印刷の手順が無い")])
        envelope = run(FakeModel([steps], [partial]), ctx).response.envelope
        self.assertEqual(envelope["outcome"], "conditional")
        statuses = {r["id"]: r["status"] for r in envelope["requests"]}
        self.assertEqual(statuses["Q1"], "unknown")
        self.assertIn("unverified_requests", envelope["handoff_reasons"])

    def test_refusal_is_insufficient_evidence_even_when_related_quotes_are_shown(self):
        rule = "⑤本部からの連絡メモが届くと「連」と表示されるので、ボタンを押します。"
        ctx = context(record(0, rule, "［販売管理⇒関連情報⇒取引先メモ管理］", page=3))
        question = "メインメニューに「本部からの連絡メモがあります」と表示されるが、どのような場合に表示されるのか。"
        refusal = draft({"kind": "gap", "text": "表示条件は資料に記載がありません。"}, confidence="low")
        envelope = run(FakeModel([refusal, refusal], [audit()]), ctx, question).response.envelope
        self.assertEqual(envelope["outcome"], "insufficient_evidence")
        self.assertEqual(envelope["items"], [])
        self.assertEqual(envelope["gaps"], ["表示条件は資料に記載がありません。"])

    def test_external_data_makes_the_answer_need_environment_data(self):
        ctx = context(record(0, LIST_TEXT, "売上-(3)年次集計明細作成"))
        eid = ids(ctx)[LIST_TEXT]
        steps = {
            **draft(item(eid, "一覧表を選択し、処理を選択します。", "一覧表を選択し、処理を選択します。", request_id="Q1")),
            "external_data_required": True,
            "external_data_items": ["対象の帳票の出力履歴"],
        }
        response = run(FakeModel([steps], [audit((0, "supported", "matched", ""), requests=[("Q1", "addressed")])]), ctx).response
        self.assertEqual(response.envelope["outcome"], "needs_environment_data")
        self.assertIn("environment_data", response.envelope["handoff_reasons"])


class AnswerEnvelopeBuildTest(unittest.TestCase):
    def test_requests_skip_context_units_and_mark_unaudited_requests_unknown(self):
        units = [{"id": "Q1", "kind": "request", "text": "説明 原文: 出力したい"}, {"id": "Q2", "kind": "context", "text": "背景"}]
        self.assertEqual(envelope_requests(units, None), [{"id": "Q1", "text": "出力したい", "status": "unknown"}])
        # 監査が返さなかった id は欠落と決めつけない。
        self.assertEqual(envelope_requests(units, [])[0]["status"], "unknown")

    def test_quote_only_items_are_conditional_and_confirmations_without_items_need_data(self):
        quote = {"kind": "rule", "text": "原文", "applies": "quote_only", "condition": ""}
        common = {"requests": (), "reviews": [], "gaps": (), "external_data_required": False}
        self.assertEqual(build_envelope(items=[quote], confirmations=(), **common)["outcome"], "conditional")
        self.assertEqual(build_envelope(items=[], confirmations=["ログを確認"], **common)["outcome"], "needs_environment_data")
        self.assertEqual(build_envelope(items=[], confirmations=(), **common)["outcome"], "insufficient_evidence")

    def test_reference_material_lines_are_not_environment_data(self):
        # 別の資料の確認（「資料:」の行）は現場の実データではない（#1491）。
        quote = {"kind": "rule", "text": "原文", "applies": "quote_only", "condition": ""}
        common = {
            "requests": [{"id": "Q1", "kind": "request", "text": "設定の場所"}],
            "reviews": [{"request_id": "Q1", "status": "partial"}],
            "gaps": (),
            "external_data_required": False,
            "reference_materials": ["設定資料"],
        }
        envelope = build_envelope(items=[quote], confirmations=["資料: 設定資料"], **common)
        self.assertEqual(envelope["outcome"], "conditional")
        self.assertNotIn("environment_data", envelope["handoff_reasons"])
        self.assertIn("reference_materials", envelope["handoff_reasons"])
        self.assertEqual(build_envelope(items=[], confirmations=["資料: 設定資料"], **common)["outcome"], "insufficient_evidence")
        # 実データの行があれば今までどおり needs_environment_data。
        self.assertEqual(
            build_envelope(items=[quote], confirmations=["実データ: 設定値", "資料: 設定資料"], **common)["outcome"],
            "needs_environment_data",
        )

    def test_all_missing_requests_without_actionable_items_are_insufficient_evidence(self):
        # 実行できる説明が無く要求がすべて missing なら、実データの確認があっても資料から答えられない（#1491）。
        quote = {"kind": "rule", "text": "原文", "applies": "quote_only", "condition": ""}
        common = {
            "requests": [{"id": "Q1", "kind": "request", "text": "費用"}],
            "reviews": [{"request_id": "Q1", "status": "missing"}],
            "gaps": ["費用の記載が無い"],
            "confirmations": ["実データ: 契約の請求額"],
        }
        for items in ([], [quote]):
            self.assertEqual(build_envelope(items=items, external_data_required=True, **common)["outcome"], "insufficient_evidence")
        # 実行できる説明があれば、要求が missing でも答えた部分があるので変えない。
        action = {"kind": "rule", "text": "説明", "applies": "matched", "condition": ""}
        self.assertEqual(build_envelope(items=[action], external_data_required=True, **common)["outcome"], "needs_environment_data")

    def test_passage_marks_decide_the_outcome_and_handoff(self):
        """矛盾で外した段落・未確認の段落は条件付き、警告だけは answered、すべて未確認は insufficient_evidence（#2738）。"""
        common = {"requests": [], "reviews": None, "gaps": [], "confirmations": [], "external_data_required": False}
        confirmed = {"kind": "rule", "text": "説明", "applies": "matched", "condition": "", "mark": ""}
        unverified = {**confirmed, "text": "未確認の説明", "mark": "unverified"}
        warning = {**confirmed, "text": "要注意の説明", "mark": "warning"}
        removed = [{"text": "外した説明", "reasons": [{"code": "contradicted", "label": "根拠と矛盾", "detail": ""}]}]

        def mark(text, state, code):
            return {"text": f"・{text}", "state": state, "reasons": [{"code": code, "label": "", "detail": ""}]}

        envelope = build_envelope(items=[confirmed], removed_passages=removed, **common)
        self.assertEqual((envelope["outcome"], envelope["handoff_reasons"]), ("conditional", ["contradicted_removed"]))
        self.assertEqual(envelope["removed_passages"], removed)
        envelope = build_envelope(
            items=[confirmed, unverified], passage_marks=[mark("未確認の説明", "unverified", "unsupported")], **common
        )
        self.assertEqual((envelope["outcome"], envelope["handoff_reasons"]), ("conditional", ["unverified_passages"]))
        envelope = build_envelope(items=[confirmed, warning], passage_marks=[mark("要注意の説明", "warning", "item_check")], **common)
        self.assertEqual((envelope["outcome"], envelope["handoff_reasons"]), ("answered", ["rule_warnings"]))
        envelope = build_envelope(items=[unverified], passage_marks=[mark("未確認の説明", "unverified", "unsupported")], **common)
        self.assertEqual(envelope["outcome"], "insufficient_evidence")

    def test_fallback_envelope_for_answers_without_grounded_structure(self):
        self.assertEqual(fallback_envelope(answered=False, insufficient_reason="根拠なし")["outcome"], "insufficient_evidence")
        self.assertEqual(fallback_envelope(answered=True)["outcome"], "answered")
        self.assertEqual(fallback_envelope(answered=True, needs_human_review=True)["outcome"], "conditional")
        self.assertEqual(fallback_envelope(answered=True, external_data_required=True)["outcome"], "needs_environment_data")


E1467 = json.loads((Path(__file__).with_name("fixtures") / "answer_envelope_e1467.json").read_text(encoding="utf-8"))


class AnswerEnvelopeE1467ReplayTest(unittest.TestCase):
    """評価 e1467 の記録の材料（fixture）を通し直し、取り違えた対応が直ることを確かめる（#1491）。"""

    def test_recorded_materials_give_the_expected_outcome(self):
        for name, case in E1467.items():
            with self.subTest(name):
                requests = [{"id": r["id"], "kind": "request", "text": r["text"]} for r in case["requests"]]
                reviews = [{"request_id": r["id"], "status": r["status"]} for r in case["requests"]]
                envelope = build_envelope(
                    requests=requests,
                    reviews=reviews,
                    items=case["items"],
                    gaps=case["gaps"],
                    confirmations=case["confirmations"],
                    external_data_required=case["external_data_required"],
                    reference_materials=case["reference_materials"],
                )
                self.assertEqual(envelope["outcome"], case["expected"])


if __name__ == "__main__":
    unittest.main()
