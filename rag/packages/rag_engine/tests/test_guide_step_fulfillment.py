"""業務ガイドの手順ごとの充足（#2491）を、合成の資料と決定論の LLM の差し替えで確かめる。

手順 A → B のガイドで、手順 B の根拠を回答の材料に取れているのに、B の説明が「参考情報」（原文のみ提示）に
留まった回答を、全手順を充足したとして確定しない。根拠が材料にあれば B を対象に作り直し、直らなければ
本文の不足と回答の構造の要求（`guide:<手順 id>`）に示す。
"""

import json
import unittest
from dataclasses import replace
from pathlib import Path

from test_grounded_answer import FakeModel, audit, context, draft, item, marks, record

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import grounded
from rag_engine.generation.answer_envelope import GUIDE_STEP_REQUEST_PREFIX, build_envelope, guide_step_requests
from rag_engine.generation.answering import _grounded_spans, synthesize_grounded_answer
from rag_engine.knowledge.runtime_knowledge import RuntimeKnowledgeContext
from rag_engine.models.llm import GroundedCitation, GroundedItem
from rag_engine.retrieval.guide_steps import GuidePlan

QUESTION = "設定 A を変更する手順を教えてください"
STEP_A = "設定画面で設定 A の値を入力します。"
STEP_B = "入力したあとは、反映画面で反映ボタンを押して変更を確定します。"
STEP_C = "種類が乙の場合は、承認画面で承認を依頼します。"


def _record(index, text, function, uid):
    return replace(record(index, text, function, page=10 + index, source="操作説明.pdf"), chunk_uid=uid)


def _context(*records):
    return context(*records)


def _knowledge(guide_steps, optional=(), terms=()):
    return RuntimeKnowledgeContext(
        original_question=QUESTION,
        expanded_question=QUESTION,
        path=Path("rk.json"),
        guide_steps=guide_steps,
        guide_optional_steps=tuple(optional),
        guide_step_terms=tuple(terms),
    )


def _run(model, ctx, knowledge, question=QUESTION):
    def unused(*a, **k):
        return (_ for _ in ()).throw(AssertionError("unexpected I/O"))

    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        return synthesize_grounded_answer(question, ctx, get_settings(), image_prompt_mode="text_only", runtime_knowledge=knowledge)


def _ids(ctx, knowledge, question=QUESTION):
    return {span["text"]: span["evidence_id"] for span in _grounded_spans(question, ctx, (), knowledge)}


def _drafts(model):
    return [prompt for name, prompt in model.prompts if name == "GroundedDraftWire"]


def _audits(model):
    return [json.loads(prompt) for name, prompt in model.prompts if name == "GroundedAudit"]


def _requests(response):
    return {entry["id"]: entry["status"] for entry in response.envelope["requests"]}


GUIDE = (("s1", "設定 A を入力する", ("run:a",)), ("s2", "反映ボタンで確定する", ("run:b",)))


def _ab_context():
    return _context(_record(0, STEP_A, "設定画面", "run:a"), _record(1, STEP_B, "反映画面", "run:b"))


class GuideStepCoverageUnitTest(unittest.TestCase):
    def _spans(self):
        return [
            {"source_id": "run:a", "evidence_id": "ea", "label": "E1", "text": STEP_A},
            {"source_id": "run:b", "evidence_id": "eb", "label": "E2", "text": STEP_B},
            {"source_id": "run:x", "evidence_id": "ex", "label": "E3", "text": "前置き。" + STEP_B},
        ]

    def _entry(self, span, *, quote_only=False, applies="matched", kind="operation", quote=None):
        entry = grounded.CheckedItem(
            GroundedItem(
                kind=kind,
                text="t",
                citations=[GroundedCitation(evidence_id="", quote=span["text"] if quote is None else quote)],
                applies=applies,
            ),
            span=span,
        )
        entry.quote_only = quote_only
        return entry

    def test_statuses(self):
        spans = self._spans()
        steps = (("s1", "A", ("run:a",)), ("s2", "B", ("run:b",)), ("s3", "C", ("run:c",)))
        checked = [self._entry(spans[0]), self._entry(spans[1], quote_only=True)]
        coverage = grounded.guide_step_coverage(steps, checked, spans)
        self.assertEqual(
            [(step["step_id"], step["status"]) for step in coverage],
            [("s1", "unknown"), ("s2", "reference_only"), ("s3", "no_evidence")],
        )
        # 適用未確認（unverified）の説明は「参考情報」の節に出るので、手順の説明に数えない。
        unverified = grounded.guide_step_coverage(steps[1:2], [self._entry(spans[1], applies="unverified")], spans)
        self.assertEqual(unverified[0]["status"], "reference_only")
        # 根拠が材料にあるのに説明に引いていない。
        self.assertEqual(grounded.guide_step_coverage(steps[1:2], [], spans)[0]["status"], "unused")
        # gap の item は手順の説明に数えない。
        gap = self._entry(spans[1], kind="gap")
        self.assertEqual(grounded.guide_step_coverage(steps[1:2], [gap], spans)[0]["status"], "unused")

    def test_same_text_in_another_chunk_counts_as_the_step_evidence(self):
        spans = self._spans()
        steps = (("s2", "B", ("run:b",)),)
        other = self._entry(spans[2], quote=STEP_B)
        self.assertEqual(grounded.guide_step_coverage(steps, [other], spans)[0]["status"], "unknown")
        # 短い引用（どの本文にも当たる語句）は数えない。
        short = self._entry(spans[2], quote="反映ボタン")
        self.assertEqual(grounded.guide_step_coverage(steps, [short], spans)[0]["status"], "unused")

    def test_optional_steps_are_not_required(self):
        spans = self._spans()
        coverage = grounded.guide_step_coverage((("s1", "A", ("run:a",)), ("s2", "B", ("run:b",))), [], spans, optional=("s2",))
        self.assertEqual([step["required"] for step in coverage], [True, False])
        self.assertEqual([step["step_id"] for step in grounded.unexplained_guide_steps(coverage)], ["s1"])

    def test_feedback_keys_keep_correction_going(self):
        feedback = {
            "downgraded_items": [{"text": "t", "reasons": ["監査: supported/not_applicable r"]}],
            "guide_steps_unexplained": [{"step_id": "s2", "status": "reference_only"}],
        }
        self.assertFalse(grounded.downgraded_only(feedback))
        self.assertFalse(grounded.evidence_shortage_only(feedback, grounded.timed_out_round(QUESTION)))
        self.assertIn("説明していない業務ガイドの手順 1 件", grounded.pending_feedback_labels(feedback))
        changed = {**feedback, "guide_steps_unexplained": [{"step_id": "s2", "status": "unused"}]}
        self.assertNotEqual(grounded.feedback_signature(feedback), grounded.feedback_signature(changed))


class GuideStepEnvelopeTest(unittest.TestCase):
    def test_guide_step_requests(self):
        coverage = [
            {"step_id": "s1", "title": "A", "required": True, "status": "explained"},
            {"step_id": "s2", "title": "B", "required": True, "status": "reference_only"},
            {"step_id": "s3", "title": "C", "required": True, "status": "unused"},
            {"step_id": "s4", "title": "D", "required": True, "status": "no_evidence"},
            {"step_id": "s5", "title": "E", "required": False, "status": "no_evidence"},
            {"step_id": "x" * 70, "title": "F", "required": True, "status": "explained"},
        ]
        requests = guide_step_requests(coverage)
        self.assertEqual(
            [(entry["id"], entry["status"]) for entry in requests[:4]],
            [("guide:s1", "addressed"), ("guide:s2", "partial"), ("guide:s3", "missing"), ("guide:s4", "missing")],
        )
        self.assertEqual(requests[0]["text"], "業務ガイドの手順: A")
        # 必須でない手順は載せず、ID は要求の ID の上限（64 文字）に収める。
        self.assertEqual(len(requests), 5)
        self.assertEqual(len(requests[-1]["id"]), 64)
        self.assertTrue(requests[-1]["id"].startswith(GUIDE_STEP_REQUEST_PREFIX))

    def test_unexplained_step_makes_the_answer_conditional(self):
        item = {"kind": "operation", "text": "t", "applies": "matched"}
        base = {"requests": [{"id": "Q1", "text": "q"}], "reviews": [{"request_id": "Q1", "status": "addressed"}], "items": [item]}
        answered = build_envelope(**base, gaps=[], confirmations=[], external_data_required=False)
        self.assertEqual(answered["outcome"], "answered")
        partial = build_envelope(
            **base,
            gaps=[],
            confirmations=[],
            external_data_required=False,
            guide_steps=[{"step_id": "s2", "title": "B", "required": True, "status": "reference_only"}],
        )
        self.assertEqual(partial["outcome"], "conditional")
        self.assertIn("unanswered_requests", partial["handoff_reasons"])


class GuideStepFulfillmentTest(unittest.TestCase):
    def test_followup_in_another_document_uses_the_guide_and_application_audit(self):
        question = "接続先を変更する手順を教えてください"
        first = "設定画面で接続先を入力します。"
        followup = "反映画面で反映ボタンを押します。"
        ctx = _context(
            _record(0, first, "設定画面", "run:a"),
            replace(_record(1, followup, "反映画面", "run:b"), source="反映操作.pdf"),
        )
        for included, applicability in ((True, "matched"), (False, "matched"), (True, "conditional")):
            with self.subTest(included=included, applicability=applicability):
                knowledge = _knowledge(GUIDE if included else GUIDE[:1])
                labels = _ids(ctx, knowledge, question)
                model = FakeModel(
                    [draft(item(labels[first], first, first), item(labels[followup], followup, followup))],
                    [
                        audit(
                            (0, "supported", "matched", ""),
                            (1, "supported", applicability, "原文に無い条件" if applicability == "conditional" else ""),
                        )
                    ],
                )
                response = _run(model, ctx, knowledge, question).response
                if included and applicability == "matched":
                    self.assertNotIn(followup, marks(response))
                    self.assertEqual(_requests(response)["guide:s2"], "unknown")
                elif not included:
                    self.assertIn(followup, response.answer_text)
                    self.assertNotIn("item_check", marks(response).get(followup, []))
                else:
                    self.assertEqual(_requests(response)["guide:s2"], "unknown")

    def test_observed_reference_only_step_does_not_trigger_regeneration(self):
        """B が監査で質問の対象ではないとされても、作り直しの指摘に手順を渡し、説明できれば充足にする。"""
        ctx, knowledge = _ab_context(), _knowledge(GUIDE)
        eids = _ids(ctx, knowledge)
        a = item(eids[STEP_A], "設定画面で設定 A の値を入力します。", STEP_A, request_id="Q1")
        b = item(eids[STEP_B], "反映画面で反映ボタンを押して確定します。", STEP_B, request_id="Q1")
        model = FakeModel(
            [draft(a, b)],
            [
                audit((0, "supported", "matched", ""), (1, "supported", "conditional", "原文に無い条件"), requests=[("Q1", "addressed")]),
                audit((0, "supported", "matched", ""), (1, "supported", "matched", ""), requests=[("Q1", "addressed")]),
            ],
        )
        result = _run(model, ctx, knowledge).response

        drafts = _drafts(model)
        self.assertEqual(len(drafts), 1)
        # 未校正の意味観察では、本文を作り直したり未確認の条件を付けたりしない。
        self.assertIn(b["text"], result.answer_text)
        audits = _audits(model)
        self.assertEqual([step["step_id"] for step in audits[0]["guide_steps"]], ["s1", "s2"])
        self.assertTrue(all(step["evidence_ids"] for step in audits[0]["guide_steps"]))
        rounds = result.generation_trace["rounds"]
        self.assertNotIn("improvement_stop", rounds[0])
        self.assertEqual([step["status"] for step in rounds[0]["guide_steps"]], ["unknown", "unknown"])
        self.assertEqual(_requests(result), {"Q1": "unknown", "guide:s1": "unknown", "guide:s2": "unknown"})
        self.assertEqual(result.envelope["outcome"], "conditional")
        self.assertIn(grounded.answer_labels().marked_section, result.answer_text)

    def test_step_left_as_reference_is_not_settled_as_answered(self):
        """作り直しても B が参考情報に留まれば、要求 Q1 の判定とは別に B を一部の充足として示す。"""
        ctx, knowledge = _ab_context(), _knowledge(GUIDE)
        eids = _ids(ctx, knowledge)
        a = item(eids[STEP_A], "設定画面で設定 A の値を入力します。", STEP_A, request_id="Q1")
        b = item(eids[STEP_B], "反映画面で反映ボタンを押して確定します。", STEP_B, request_id="Q1")
        model = FakeModel(
            [draft(a, b)],
            [audit((0, "supported", "matched", ""), (1, "supported", "conditional", "原文に無い条件"), requests=[("Q1", "addressed")])],
        )
        result = _run(model, ctx, knowledge).response

        self.assertEqual(_requests(result), {"Q1": "unknown", "guide:s1": "unknown", "guide:s2": "unknown"})
        self.assertEqual(result.envelope["outcome"], "conditional")
        self.assertTrue(result.needs_human_review)
        self.assertIn(b["text"], result.answer_text)
        self.assertNotIn("原文に無い条件", result.answer_text)
        self.assertFalse(result.generation_trace["semantic_audit_observation"]["adopted"])
        coverage = result.generation_trace["guide_step_coverage"]
        self.assertEqual([step["status"] for step in coverage], ["unknown", "unknown"])
        # 適用が未確認の B は「参考情報」の原文で示し、手順を確かめた説明にはしない（対象違いの印は付けない。#2837）。
        self.assertFalse(
            any(reason["code"] == "target_mismatch" for mark in result.envelope["passage_marks"] for reason in mark["reasons"])
        )

    def test_missing_step_evidence_is_shown_without_regeneration(self):
        """手順 B の根拠が回答の材料に無ければ、同じ根拠での作り直しはせず、不足として示す。"""
        ctx, knowledge = _context(_record(0, STEP_A, "設定画面", "run:a")), _knowledge(GUIDE)
        eids = _ids(ctx, knowledge)
        a = item(eids[STEP_A], "設定画面で設定 A の値を入力します。", STEP_A, request_id="Q1")
        model = FakeModel([draft(a)], [audit((0, "supported", "matched", ""), requests=[("Q1", "addressed")])])
        result = _run(model, ctx, knowledge).response

        self.assertEqual(len(_drafts(model)), 1)
        self.assertEqual(_requests(result)["guide:s2"], "missing")
        self.assertEqual(result.envelope["outcome"], "conditional")
        self.assertIn(
            "業務ガイドの手順「反映ボタンで確定する」は、今回の回答に渡した資料で必要な根拠を確認できていません。", result.answer_text
        )

    def test_optional_branch_step_is_not_required(self):
        """条件が決まらない分岐の手順は、説明が無くても充足の判定に数えない。"""
        guide = (*GUIDE, ("s3", "乙の場合に承認を依頼する", ("run:c",)))
        ctx = _context(
            _record(0, STEP_A, "設定画面", "run:a"), _record(1, STEP_B, "反映画面", "run:b"), _record(2, STEP_C, "承認画面", "run:c")
        )
        knowledge = _knowledge(guide, optional=("s3",))
        eids = _ids(ctx, knowledge)
        a = item(eids[STEP_A], "設定画面で設定 A の値を入力します。", STEP_A, request_id="Q1")
        b = item(eids[STEP_B], "反映画面で反映ボタンを押して確定します。", STEP_B, request_id="Q1")
        model = FakeModel(
            [draft(a, b)], [audit((0, "supported", "matched", ""), (1, "supported", "matched", ""), requests=[("Q1", "addressed")])]
        )
        result = _run(model, ctx, knowledge).response

        self.assertEqual(len(_drafts(model)), 1)
        self.assertNotIn("guide:s3", _requests(result))
        self.assertEqual(result.envelope["outcome"], "conditional")
        self.assertEqual([step["required"] for step in result.generation_trace["guide_step_coverage"]], [True, True, False])

    def test_explicit_requests_and_guide_steps_are_judged_separately(self):
        """明示的な複数要求の充足（監査）と、ガイドの後続手順の充足を別の単位で示す。"""
        question = "設定 A を変更する手順を教えてください。また、変更できる権限も教えてください。"
        rule = "設定 A を変更できるのは管理者です。"
        ctx = _context(_record(0, STEP_A, "設定画面", "run:a"), _record(1, STEP_B, "反映画面", "run:b"), _record(2, rule, "権限", "run:r"))
        knowledge = _knowledge(GUIDE)
        eids = _ids(ctx, knowledge, question)
        a = item(eids[STEP_A], "設定画面で設定 A の値を入力します。", STEP_A, request_id="Q1")
        b = item(eids[STEP_B], "反映画面で反映ボタンを押して確定します。", STEP_B, request_id="Q1")
        r = {**item(eids[rule], rule, rule, request_id="Q2"), "kind": "rule"}
        model = FakeModel(
            [draft(a, b, r)],
            [
                audit(
                    (0, "supported", "matched", ""),
                    (1, "supported", "conditional", "原文に無い条件"),
                    (2, "supported", "matched", ""),
                    requests=[("Q1", "addressed"), ("Q2", "addressed")],
                )
            ],
        )
        result = _run(model, ctx, knowledge, question).response

        requests = _requests(result)
        self.assertEqual(
            {key: value for key, value in requests.items() if not key.startswith("guide:")}, {"Q1": "unknown", "Q2": "unknown"}
        )
        self.assertEqual(requests["guide:s2"], "unknown")
        self.assertEqual(result.envelope["outcome"], "conditional")

    def test_without_guide_steps_nothing_changes(self):
        ctx = _ab_context()
        knowledge = _knowledge(())
        eids = _ids(ctx, knowledge)
        a = item(eids[STEP_A], "設定画面で設定 A の値を入力します。", STEP_A, request_id="Q1")
        model = FakeModel([draft(a)], [audit((0, "supported", "matched", ""), requests=[("Q1", "addressed")])])
        result = _run(model, ctx, knowledge).response

        self.assertNotIn("guide_steps", _audits(model)[0])
        self.assertEqual(_requests(result), {"Q1": "unknown"})
        self.assertNotIn("guide_step_coverage", result.generation_trace)


# 画面の図の項目の種類で分かれる業務ガイド（#2689）。共通の画面の確認 → 選択の項目ならコードの登録・自由入力なら画面で
# 書き換え・種類が分からなければ確認先へ問い合わせる。合成の項目名と手順だけを使う。
KIND_QUESTION = "区分コードの項目の選択肢を追加したい"
KIND_SCREEN = "設定画面で区分コードの項目を開きます。"
KIND_CODE = "コード登録画面で選択肢のコードを追加します。"
KIND_ASK = "項目の種類が分からない場合は、管理担当に確認します。"
KIND_GUIDE = (
    ("screen", "画面で項目を確かめる", ("run:screen",)),
    ("code", "コードの登録で選択肢を追加する", ("run:code",)),
    ("ask", "種類が分からなければ確認する", ("run:ask",)),
)
# backend が照合で分からなかった条件（選択肢と言い換え）と、決まった値ごとの手順の計画として渡す形。
KIND_CONDITIONS = (("field_kind", "項目の種類", (("選択の項目", ("選択",)), ("入力の項目", ("自由入力",)))),)
KIND_PLANS = (
    ((("field_kind", "選択の項目"),), (("screen", True), ("code", True)), "guide-g1", "分かっている条件: 項目の種類=選択の項目"),
    ((("field_kind", "入力の項目"),), (("screen", True), ("edit", True)), "guide-g1", "分かっている条件: 項目の種類=入力の項目"),
)


class GuidePlanAfterEvidenceConditionTest(unittest.TestCase):
    """根拠の画面の図で業務ガイドの条件が決まったら、分岐と必須の手順を決まった条件の計画にする（#2689）。"""

    def _context(self, kind):
        screen = replace(
            _record(0, KIND_SCREEN, "設定画面", "run:screen"),
            metadata={"image_evidence": [{"visual_structure": {"form_fields": [{"name": "区分コード", "kind": kind}]}}]},
        )
        return _context(screen, _record(1, KIND_CODE, "コード登録画面", "run:code"), _record(2, KIND_ASK, "問い合わせ", "run:ask"))

    def _run(self, kind, plans=KIND_PLANS):
        ctx = self._context(kind)
        # 条件が決まっていない照合の計画: 選択の分岐（code）は必須でなく、「分からない場合」（ask）が必須。
        knowledge = _knowledge(KIND_GUIDE, optional=("code",))
        eids = _ids(ctx, knowledge, KIND_QUESTION)
        screen = item(eids[KIND_SCREEN], "設定画面で区分コードの項目を開きます。", KIND_SCREEN, request_id="Q1")
        code = item(eids[KIND_CODE], "コード登録画面で選択肢のコードを追加します。", KIND_CODE, request_id="Q1")
        model = FakeModel(
            [draft(screen, code)], [audit((0, "supported", "matched", ""), (1, "supported", "matched", ""), requests=[("Q1", "addressed")])]
        )
        settings = replace(get_settings(), evidence_conditions=KIND_CONDITIONS, evidence_plans=plans)

        def unused(*a, **k):
            return (_ for _ in ()).throw(AssertionError("unexpected I/O"))

        with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
            result = synthesize_grounded_answer(
                KIND_QUESTION, ctx, settings, image_prompt_mode="text_only", runtime_knowledge=knowledge
            ).response
        return result, model

    def test_resolved_condition_switches_branch_and_required_steps(self):
        result, model = self._run("select")
        coverage = result.generation_trace["guide_step_coverage"]
        # 外れた「分からない場合」の手順は回答の手順から外れ、決まった選択の分岐の手順が必須になる。
        self.assertEqual([(step["step_id"], step["required"]) for step in coverage], [("screen", True), ("code", True)])
        self.assertEqual(_requests(result), {"Q1": "unknown", "guide:screen": "unknown", "guide:code": "unknown"})
        self.assertEqual(result.envelope["outcome"], "conditional")
        self.assertEqual([step["step_id"] for step in _audits(model)[0]["guide_steps"]], ["screen", "code"])
        trace = result.generation_trace["form_field_kinds"]
        self.assertEqual(trace["guide_plan"], [{"step_id": "screen", "required": True}, {"step_id": "code", "required": True}])
        self.assertEqual(trace["resolved_conditions"][0]["value"], "選択の項目")

    def test_resolved_branch_step_cannot_be_skipped(self):
        """決まった分岐の主の操作を説明しなければ、必須の手順の不足として示す（参考の原文だけで充足しない）。"""
        ctx = self._context("select")
        knowledge = _knowledge(KIND_GUIDE, optional=("code",))
        eids = _ids(ctx, knowledge, KIND_QUESTION)
        screen = item(eids[KIND_SCREEN], "設定画面で区分コードの項目を開きます。", KIND_SCREEN, request_id="Q1")
        model = FakeModel([draft(screen)], [audit((0, "supported", "matched", ""), requests=[("Q1", "addressed")])])
        settings = replace(get_settings(), evidence_conditions=KIND_CONDITIONS, evidence_plans=KIND_PLANS)

        def unused(*a, **k):
            return (_ for _ in ()).throw(AssertionError("unexpected I/O"))

        with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
            result = synthesize_grounded_answer(
                KIND_QUESTION, ctx, settings, image_prompt_mode="text_only", runtime_knowledge=knowledge
            ).response
        requests = _requests(result)
        self.assertNotEqual(requests["guide:code"], "addressed")
        self.assertNotIn("guide:ask", requests)
        self.assertEqual(result.envelope["outcome"], "conditional")

    def test_split_or_unknown_kind_keeps_the_unresolved_plan(self):
        """種類が拾えない・計画が無いときは、条件が決まっていない計画（分からない場合の手順が必須）のまま。"""
        for kind, plans in (("unknown", KIND_PLANS), ("select", ())):
            with self.subTest(kind=kind, plans=bool(plans)):
                result, _model = self._run(kind, plans)
                coverage = {step["step_id"]: step["required"] for step in result.generation_trace["guide_step_coverage"]}
                self.assertEqual(coverage, {"screen": True, "code": False, "ask": True})
                self.assertEqual(_requests(result)["guide:ask"], "missing")
                self.assertNotIn("guide_plan", result.generation_trace.get("form_field_kinds", {}))


# 2 つの手順が共有する根拠の chunk（#2649）。手順 1 の主の操作・手順 2 の主の操作・両手順に共通の末尾の操作。
SHARED_A = "設定画面で設定 A の値を入力します。"
SHARED_B = "反映画面で反映ボタンを押して変更を反映します。"
SHARED_END = "最後に保存ボタンを押して画面を閉じます。"
SHARED = SHARED_A + SHARED_B + SHARED_END
SHARED_GUIDE = (("s1", "設定 A を入力する", ("run:s",)), ("s2", "反映ボタンで反映する", ("run:s",)))
SHARED_TERMS = (("s1", ("設定画面",)), ("s2", ("反映ボタン",)))


class GuideStepSharedEvidenceTest(unittest.TestCase):
    """手順が根拠を共有しても、共有の一部分だけで複数の手順を説明済みにしない（#2649）。"""

    def _span(self, source, text, label="E1"):
        return {"source_id": source, "evidence_id": f"e-{source}", "label": label, "text": text}

    def _entry(self, span, quote, *, quote_only=False, applies="matched"):
        entry = grounded.CheckedItem(
            GroundedItem(kind="operation", text="t", citations=[GroundedCitation(evidence_id="", quote=quote)], applies=applies), span=span
        )
        entry.quote_only = quote_only
        return entry

    def _candidate_statuses(self, steps, checked, spans, optional=(), terms=SHARED_TERMS):
        coverage = grounded.guide_step_coverage(steps, checked, spans, optional, dict(terms))
        return {step["step_id"]: step["assignment"]["candidate_status"] for step in coverage}, coverage

    def test_common_sentence_of_a_shared_chunk_explains_no_step(self):
        span = self._span("run:s", SHARED)
        statuses, coverage = self._candidate_statuses(SHARED_GUIDE, [self._entry(span, SHARED_END)], [span])
        self.assertEqual(statuses, {"s1": "unmatched", "s2": "unmatched"})
        self.assertEqual([step["step_id"] for step in grounded.unexplained_guide_steps(coverage)], ["s1", "s2"])

    def test_main_operation_left_as_reference_is_not_covered_by_the_common_sentence(self):
        """主の操作が参考原文だけで、共有の chunk の共通の操作だけを公開した手順は説明済みにしない。"""
        span = self._span("run:s", SHARED)
        checked = [
            self._entry(span, SHARED_A),
            self._entry(span, SHARED_B, quote_only=True),
            self._entry(span, SHARED_END),
        ]
        statuses, coverage = self._candidate_statuses(SHARED_GUIDE, checked, [span])
        self.assertEqual(statuses, {"s1": "matched", "s2": "reference_only"})
        self.assertEqual([step["step_id"] for step in grounded.unexplained_guide_steps(coverage)], ["s1", "s2"])
        # 適用未確認（参考情報の節に出る）の主の操作も同じ。
        checked[1] = self._entry(span, SHARED_B, applies="unverified")
        self.assertEqual(self._candidate_statuses(SHARED_GUIDE, checked, [span])[0], {"s1": "matched", "s2": "reference_only"})

    def test_each_step_explained_from_the_same_chunk(self):
        span = self._span("run:s", SHARED)
        checked = [self._entry(span, SHARED_A), self._entry(span, SHARED_B), self._entry(span, SHARED_END)]
        self.assertEqual(self._candidate_statuses(SHARED_GUIDE, checked, [span])[0], {"s1": "matched", "s2": "matched"})
        # 手順の順と item の順が逆でも、手順の語で対応付ける。
        reversed_checked = [self._entry(span, SHARED_B), self._entry(span, SHARED_A)]
        self.assertEqual(self._candidate_statuses(SHARED_GUIDE, reversed_checked, [span])[0], {"s1": "matched", "s2": "matched"})

    def test_one_item_can_explain_two_independently_matched_steps(self):
        """両手順の主の操作を含む item は、段落を分けなくても両手順を説明できる。"""
        span = self._span("run:s", SHARED)
        statuses, _ = self._candidate_statuses(SHARED_GUIDE, [self._entry(span, SHARED_A + SHARED_B)], [span])
        self.assertEqual(sorted(statuses.values()), ["matched", "matched"])

    def test_same_text_in_another_chunk(self):
        """別の chunk の同じ原文: 共通の文は手順の説明にならず、手順の主の操作の文はその手順の説明になる。"""
        spans = [self._span("run:a", SHARED_A + SHARED_END), self._span("run:b", SHARED_B + SHARED_END, "E2")]
        steps = (("s1", "設定 A を入力する", ("run:a",)), ("s2", "反映ボタンで反映する", ("run:b",)))
        common = self._candidate_statuses(steps, [self._entry(spans[0], SHARED_END)], spans)[0]
        self.assertEqual(common, {"s1": "unmatched", "s2": "unmatched"})
        both = [self._entry(spans[0], SHARED_A), self._entry(spans[1], SHARED_B), self._entry(spans[0], SHARED_END)]
        self.assertEqual(self._candidate_statuses(steps, both, spans)[0], {"s1": "matched", "s2": "matched"})
        # 手順 1 の根拠（全体の概要の chunk）にある手順 2 の主の操作の原文は、手順 2 の説明にだけ数える。
        overview = [self._span("run:o", SHARED_A + SHARED_B), self._span("run:b", SHARED_B, "E2")]
        steps = (("s1", "設定 A を入力する", ("run:o",)), ("s2", "反映ボタンで反映する", ("run:b",)))
        self.assertEqual(
            self._candidate_statuses(steps, [self._entry(overview[0], SHARED_B)], overview)[0], {"s1": "unmatched", "s2": "matched"}
        )

    def test_optional_branch_step_does_not_take_the_required_steps_item(self):
        """任意分岐の説明が同じ段落にあっても、必須の手順の説明を奪わない。"""
        span = self._span("run:s", SHARED)
        steps = (SHARED_GUIDE[1], SHARED_GUIDE[0])
        statuses, coverage = self._candidate_statuses(steps, [self._entry(span, SHARED)], [span], optional=("s2",))
        self.assertEqual(statuses, {"s1": "matched", "s2": "matched"})
        self.assertEqual([step["step_id"] for step in grounded.unexplained_guide_steps(coverage)], ["s1"])

    def test_missing_evidence_is_kept_apart(self):
        span = self._span("run:s", SHARED)
        steps = (*SHARED_GUIDE, ("s3", "承認を依頼する", ("run:z",)))
        checked = [self._entry(span, SHARED_A), self._entry(span, SHARED_B)]
        statuses, coverage = self._candidate_statuses(steps, checked, [span])
        self.assertEqual(statuses, {"s1": "matched", "s2": "matched", "s3": "no_evidence"})
        self.assertEqual([step["step_id"] for step in grounded.unexplained_guide_steps(coverage)], ["s1", "s2", "s3"])

    def test_steps_without_words_take_no_shared_item_by_words(self):
        """手順の語で照らせない手順は、語の判定だけでは共有の item をどの手順の説明にもしない（#2685。安全側）。"""
        span = self._span("run:s", SHARED)
        steps = (("s1", "A", ("run:s",)), ("s2", "B", ("run:s",)))
        statuses, _ = self._candidate_statuses(steps, [self._entry(span, SHARED_END)], [span], terms=())
        self.assertEqual(statuses, {"s1": "unmatched", "s2": "unmatched"})

    def test_exclusive_evidence_is_unchanged(self):
        """根拠を共有しない手順は、従来どおり手順の根拠を引いた公開の説明で explained。"""
        spans = [self._span("run:a", SHARED_A + SHARED_END), self._span("run:b", SHARED_B, "E2")]
        steps = (("s1", "設定 A を入力する", ("run:a",)), ("s2", "反映ボタンで反映する", ("run:b",)))
        checked = [self._entry(spans[0], SHARED_A), self._entry(spans[1], SHARED_B)]
        self.assertEqual(self._candidate_statuses(steps, checked, spans)[0], {"s1": "matched", "s2": "matched"})


class GuideStepSharedEvidenceAnswerTest(unittest.TestCase):
    """主の操作が参考原文だけの最終回答を、全必須手順 addressed・answered にしない（#2649）。"""

    def _run_shared(self, b_applicability):
        ctx = _context(_record(0, SHARED, "設定画面", "run:s"))
        knowledge = _knowledge(SHARED_GUIDE, terms=SHARED_TERMS)
        (eid,) = _ids(ctx, knowledge).values()
        a = item(eid, "設定画面で設定 A の値を入力します。", SHARED_A, request_id="Q1")
        b = item(eid, "反映画面で反映ボタンを押して変更を反映します。", SHARED_B, request_id="Q1")
        end = item(eid, "最後に保存ボタンを押して画面を閉じます。", SHARED_END, request_id="Q1")
        model = FakeModel(
            [draft(a, b, end)],
            [
                audit(
                    (0, "supported", "matched", ""),
                    (1, "supported", b_applicability, "原文に無い条件" if b_applicability == "conditional" else ""),
                    (2, "supported", "matched", ""),
                    requests=[("Q1", "addressed")],
                )
            ],
        )
        return model, _run(model, ctx, knowledge).response

    def test_shared_chunk_with_main_operation_as_reference_is_not_answered(self):
        model, result = self._run_shared("conditional")

        # 未校正の観察を充足や再生成に採用せず、既存の操作は残す。
        drafts = _drafts(model)
        self.assertEqual(len(drafts), 1)
        self.assertEqual(_requests(result), {"Q1": "unknown", "guide:s1": "unknown", "guide:s2": "unknown"})
        self.assertEqual(result.envelope["outcome"], "conditional")
        self.assertFalse(result.generation_trace["semantic_audit_observation"]["adopted"])
        self.assertNotIn("原文に無い条件", result.answer_text)
        self.assertEqual([step["status"] for step in result.generation_trace["guide_step_coverage"]], ["unknown", "unknown"])
        # 公開できる部分（手順 1 と共通の操作）は部分回答として残す。
        self.assertIn("設定画面で設定 A の値を入力します。", result.answer_text)
        self.assertIn("最後に保存ボタンを押して画面を閉じます。", result.answer_text)

    def test_shared_chunk_keeps_steps_unverified_under_diagnostic_policy(self):
        model, result = self._run_shared("matched")

        self.assertEqual(len(_drafts(model)), 1)
        self.assertEqual(_requests(result), {"Q1": "unknown", "guide:s1": "unknown", "guide:s2": "unknown"})
        self.assertEqual(result.envelope["outcome"], "conditional")


class GuideStepUnsearchedTest(unittest.TestCase):
    """手順ごとの検索が期限で止まり、後の手順を検索しなかったとき（#2706）。

    手順 1・2 は同じ chunk（手順 1・2 の主の操作と共通の末尾の操作）を参照する。手順 1 だけを検索し、手順 2 は
    期限で検索しなかった。共通の文だけの item で手順 1 を説明済みにしない。
    """

    def setUp(self):
        self.span = {"source_id": "run:s", "evidence_id": "e-s", "label": "E1", "text": SHARED}

    def _entry(self, quote, *, quote_only=False):
        entry = grounded.CheckedItem(
            GroundedItem(kind="operation", text="t", citations=[GroundedCitation(evidence_id="", quote=quote)], applies="matched"),
            span=self.span,
        )
        entry.quote_only = quote_only
        return entry

    def _candidate_statuses(self, steps, quotes, *, unsearched=("s2",), semantics=None, terms=SHARED_TERMS):
        coverage = grounded.guide_step_coverage(
            steps, [self._entry(quote) for quote in quotes], [self.span], (), dict(terms), semantics, unsearched
        )
        return {step["step_id"]: step["assignment"]["candidate_status"] for step in coverage}, coverage

    def test_common_sentence_does_not_explain_the_searched_step(self):
        # 手順 2 の根拠が分からない（参照の頁の根拠も無い）: 手順 1 だけの根拠に見えても、共通の文で説明済みにしない。
        steps = (SHARED_GUIDE[0], ("s2", "反映ボタンで反映する", ()))
        before, _ = self._candidate_statuses(steps, (SHARED_END,), unsearched=())
        self.assertEqual(before, {"s1": "matched", "s2": "no_evidence"})  # 検索しなかったことを知らない判定
        statuses, coverage = self._candidate_statuses(steps, (SHARED_END,))
        self.assertEqual(statuses, {"s1": "unmatched", "s2": "not_searched"})
        self.assertEqual([step["step_id"] for step in grounded.unexplained_guide_steps(coverage)], ["s1", "s2"])
        self.assertIn("手順ごとの検索を時間・回数の上限で打ち切った", grounded.guide_step_gap_line(coverage[1]))
        # 意味の判定でも同じ（共通の文は両手順に同じくらい近い）。
        semantics = grounded.GuideStepSemantics(
            StubEmbedder(SENTENCE_VECTORS + (("設定 A を入力", (1, 0, 0)), ("反映ボタンで", (0, 1, 0)))).embed
        )
        self.assertEqual(self._candidate_statuses(steps, (SHARED_END,), semantics=semantics)[0], {"s1": "unmatched", "s2": "not_searched"})

    def test_main_operation_still_explains_the_searched_step(self):
        steps = (SHARED_GUIDE[0], ("s2", "反映ボタンで反映する", ()))
        self.assertEqual(self._candidate_statuses(steps, (SHARED_A, SHARED_END))[0], {"s1": "matched", "s2": "not_searched"})

    def test_reference_pages_make_the_shared_chunk_visible(self):
        # 手順 2 の参照の頁の根拠（同じ chunk）を手順 2 の根拠に足すと、手順 2 は no_evidence ではなく unused。
        statuses, _ = self._candidate_statuses(SHARED_GUIDE, (SHARED_END,))
        self.assertEqual(statuses, {"s1": "unmatched", "s2": "unmatched"})
        self.assertEqual(self._candidate_statuses(SHARED_GUIDE, (SHARED_A, SHARED_B))[0], {"s1": "matched", "s2": "matched"})

    def test_unsearched_required_step_is_missing_in_the_envelope(self):
        requests = guide_step_requests([{"step_id": "s2", "title": "B", "required": True, "status": "not_searched"}])
        self.assertEqual(requests[0]["status"], "missing")


class GuideStepUnsearchedAnswerTest(unittest.TestCase):
    """手順ごとの検索を期限で打ち切った回答を、共通の文だけで answered にしない（#2706）。"""

    def test_common_sentence_answer_with_unsearched_step(self):
        ctx = _context(_record(0, SHARED, "設定画面", "run:s"))
        guide = (SHARED_GUIDE[0], ("s2", "反映ボタンで反映する", ()))
        knowledge = replace(_knowledge(guide, terms=SHARED_TERMS), guide_unsearched_steps=("s2",))
        (eid,) = _ids(ctx, knowledge).values()
        end = item(eid, "最後に保存ボタンを押して画面を閉じます。", SHARED_END, request_id="Q1")
        model = FakeModel([draft(end)] * 3, [audit((0, "supported", "matched", ""), requests=[("Q1", "addressed")])] * 3)
        result = _run(model, ctx, knowledge).response

        self.assertEqual([step["status"] for step in result.generation_trace["guide_step_coverage"]], ["unknown", "not_searched"])
        self.assertEqual(_requests(result), {"Q1": "unknown", "guide:s1": "unknown", "guide:s2": "missing"})
        self.assertNotEqual(result.envelope["outcome"], "answered")
        self.assertIn("業務ガイドの手順「反映ボタンで反映する」は、手順ごとの検索を時間・回数の上限で打ち切った", result.answer_text)


class StubEmbedder:
    """決定論の embedding（#2649）。文に含まれる鍵で合成の vector を返し、呼び出しの入力を記録する。"""

    def __init__(self, table, *, fail=False):
        self.table = table
        self.fail = fail
        self.calls = []

    def embed(self, texts, *, query=False):
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return [self._vector(text) for text in texts]

    def _vector(self, text):
        for key, vector in self.table:
            if key in text:
                return list(vector)
        raise AssertionError(f"unexpected text: {text}")


# 手順の記述と根拠の文の合成の vector。手順 1 の主の操作・手順 2 の主の操作は各手順に近く、共通の末尾の操作は
# 両手順に同じくらい近い。
SENTENCE_VECTORS = (("設定 A の値", (1.0, 0.0, 0.3)), ("反映ボタンを押", (0.0, 1.0, 0.3)), ("保存ボタンを押", (0.5, 0.5, 1.0)))
# 手順の題名・手がかりと根拠の言い回しが違う手順（手順の語が根拠の文に無い。手順 2 の手がかりは共通の文にだけ当たる）。
REWORDED_GUIDE = (("s1", "値の登録", ("run:s",)), ("s2", "内容の確定", ("run:s",)))
REWORDED_VECTORS = (("値の登録", (1.0, 0.0, 0.0)), ("内容の確定", (0.0, 1.0, 0.0)), *SENTENCE_VECTORS)
# 手順 2 の語（ボタン）を主の操作の文と共通の文が同じ数だけ含む手順。
TIED_GUIDE = (("s1", "設定 A を入力する", ("run:s",)), ("s2", "ボタンを押す", ("run:s",)))
TIED_VECTORS = (("設定 A を入力", (1.0, 0.0, 0.0)), ("ボタンを押す", (0.0, 1.0, 0.0)), *SENTENCE_VECTORS)


class GuideStepSemanticMatchTest(unittest.TestCase):
    """語で決まらない共有の根拠の item を、意味の近さで手順に対応付ける（#2649）。"""

    def setUp(self):
        self.span = {"source_id": "run:s", "evidence_id": "e-s", "label": "E1", "text": SHARED}

    def _entry(self, quote, *, quote_only=False):
        entry = grounded.CheckedItem(
            GroundedItem(kind="operation", text="t", citations=[GroundedCitation(evidence_id="", quote=quote)], applies="matched"),
            span=self.span,
        )
        entry.quote_only = quote_only
        return entry

    def _candidate_statuses(self, steps, quotes, embedder=None, terms=(), semantics=None):
        if semantics is None and embedder is not None:
            semantics = grounded.GuideStepSemantics(embedder.embed)
        checked = [self._entry(quote) for quote in quotes]
        coverage = grounded.guide_step_coverage(steps, checked, [self.span], (), dict(terms), semantics)
        return {step["step_id"]: step["assignment"]["candidate_status"] for step in coverage}

    def test_reworded_steps_are_explained_by_meaning(self):
        """手順の語が根拠に無くても、各手順の主の操作を説明した item で両手順を explained にする。"""
        embedder = StubEmbedder(REWORDED_VECTORS)
        for quotes in ((SHARED_A, SHARED_B, SHARED_END), (SHARED_END, SHARED_B, SHARED_A), (SHARED_A, SHARED_B)):
            self.assertEqual(self._candidate_statuses(REWORDED_GUIDE, quotes, embedder), {"s1": "matched", "s2": "matched"})
        # 手がかりが共通の文にだけ当たる（語の判定では主の操作の文が共通の文になる）手順も、意味で主の操作の文を決める。
        # 語の判定だけでは、語で決まらない共有の item をどの手順の説明にもしない（#2685。安全側）。
        terms = (("s2", ("保存ボタン",)),)
        self.assertEqual(
            self._candidate_statuses(REWORDED_GUIDE, (SHARED_A, SHARED_B), terms=terms), {"s1": "unmatched", "s2": "unmatched"}
        )
        self.assertEqual(
            self._candidate_statuses(REWORDED_GUIDE, (SHARED_A, SHARED_B), StubEmbedder(REWORDED_VECTORS), terms),
            {"s1": "matched", "s2": "matched"},
        )

    def test_common_sentence_alone_explains_no_reworded_step(self):
        """言い回しの違う手順でも、両手順に同じくらい近い共通の文だけの公開では説明済みにしない。"""
        self.assertEqual(
            self._candidate_statuses(REWORDED_GUIDE, (SHARED_END,), StubEmbedder(REWORDED_VECTORS)), {"s1": "unmatched", "s2": "unmatched"}
        )
        # 一部分を引いた共通の文も同じ。
        self.assertEqual(
            self._candidate_statuses(REWORDED_GUIDE, ("保存ボタンを押して画面を閉じます。",), StubEmbedder(REWORDED_VECTORS)),
            {"s1": "unmatched", "s2": "unmatched"},
        )
        # 主の操作が参考原文だけなら reference_only（共通の文の公開で補わない）。
        checked = [self._entry(SHARED_A), self._entry(SHARED_B, quote_only=True), self._entry(SHARED_END)]
        semantics = grounded.GuideStepSemantics(StubEmbedder(REWORDED_VECTORS).embed)
        coverage = grounded.guide_step_coverage(REWORDED_GUIDE, checked, [self.span], (), {}, semantics)
        self.assertEqual([step["assignment"]["candidate_status"] for step in coverage], ["matched", "reference_only"])

    def test_common_sentence_with_as_many_step_words_is_not_counted(self):
        """共通の文が主の操作の文と同じ数の手順の語を含んでも、共通の文を手順の説明に数えない。"""
        # 語の判定だけでも、主の操作の文が語で 1 つに決まらない手順 2 には共通の文を数えない（#2685）。
        self.assertEqual(self._candidate_statuses(TIED_GUIDE, (SHARED_A, SHARED_END)), {"s1": "matched", "s2": "unmatched"})
        embedder = StubEmbedder(TIED_VECTORS)
        self.assertEqual(self._candidate_statuses(TIED_GUIDE, (SHARED_A, SHARED_END), embedder), {"s1": "matched", "s2": "unmatched"})
        self.assertEqual(self._candidate_statuses(TIED_GUIDE, (SHARED_A, SHARED_B), embedder), {"s1": "matched", "s2": "matched"})

    def test_without_or_failing_embedder_falls_back_to_words(self):
        """Embedder が無い・失敗したら、語の判定（直前の判定）と同じ結果に戻り、その回答では呼び直さない。"""
        cases = [
            (REWORDED_GUIDE, (SHARED_END,), ()),
            (REWORDED_GUIDE, (SHARED_A, SHARED_B), (("s2", ("保存ボタン",)),)),
            (TIED_GUIDE, (SHARED_A, SHARED_END), ()),
            (SHARED_GUIDE, (SHARED_A, SHARED_END), SHARED_TERMS),
        ]
        failing = StubEmbedder(REWORDED_VECTORS, fail=True)
        semantics = grounded.GuideStepSemantics(failing.embed)
        for steps, quotes, terms in cases:
            expected = self._candidate_statuses(steps, quotes, terms=terms)
            self.assertEqual(self._candidate_statuses(steps, quotes, terms=terms, semantics=grounded.GuideStepSemantics()), expected)
            self.assertEqual(self._candidate_statuses(steps, quotes, terms=terms, semantics=semantics), expected)
        self.assertEqual(len(failing.calls), 1)
        self.assertTrue(semantics.failed)
        self.assertEqual(semantics.trace()["calls"], 1)
        # 記述（題名・手がかり）の無い手順は意味で照らせないので、呼ばずに語の判定に戻る。
        untitled = (("s1", "", ("run:s",)), REWORDED_GUIDE[1])
        working = StubEmbedder(REWORDED_VECTORS)
        self.assertEqual(self._candidate_statuses(untitled, (SHARED_END,), working), self._candidate_statuses(untitled, (SHARED_END,)))
        self.assertEqual(working.calls, [])
        # 数の合わない応答・零の vector も失敗として語の判定に戻る。
        for broken in (lambda texts: [], lambda texts: [[0.0, 0.0, 0.0] for _ in texts]):
            self.assertEqual(
                self._candidate_statuses(REWORDED_GUIDE, (SHARED_END,), semantics=grounded.GuideStepSemantics(broken)),
                self._candidate_statuses(REWORDED_GUIDE, (SHARED_END,)),
            )

    def test_embedding_only_for_undecided_shared_items_and_reused(self):
        """embedding は語で決まらない共有の根拠の item があるときだけ呼び、同じ文は呼び直さない。"""
        embedder = StubEmbedder(REWORDED_VECTORS)
        semantics = grounded.GuideStepSemantics(embedder.embed)
        # 根拠を共有しない手順。
        spans = [
            {"source_id": "run:a", "evidence_id": "ea", "label": "E1", "text": SHARED_A},
            {"source_id": "run:b", "evidence_id": "eb", "label": "E2", "text": SHARED_B},
        ]
        steps = (("s1", "値の登録", ("run:a",)), ("s2", "内容の確定", ("run:b",)))
        checked = [
            grounded.CheckedItem(
                GroundedItem(
                    kind="operation", text="t", citations=[GroundedCitation(evidence_id="", quote=span["text"])], applies="matched"
                ),
                span=span,
            )
            for span in spans
        ]
        coverage = grounded.guide_step_coverage(steps, checked, spans, (), {}, semantics)
        self.assertEqual([step["assignment"]["candidate_status"] for step in coverage], ["matched", "matched"])
        # 根拠を共有しても、手順の語で決まる item。
        self.assertEqual(
            self._candidate_statuses(SHARED_GUIDE, (SHARED_A, SHARED_B), terms=SHARED_TERMS, semantics=semantics)["s2"], "matched"
        )
        self.assertEqual(embedder.calls, [])
        # 語で決まらない共有の item は 1 回で embedding にし、同じ文は再利用する（新しい引用だけを足す）。
        self._candidate_statuses(REWORDED_GUIDE, (SHARED_A, SHARED_B), semantics=semantics)
        self._candidate_statuses(REWORDED_GUIDE, (SHARED_B, SHARED_A, SHARED_END), semantics=semantics)
        self._candidate_statuses(REWORDED_GUIDE, ("保存ボタンを押して画面を閉じます。",), semantics=semantics)
        self.assertEqual(len(embedder.calls), 2)
        self.assertEqual(embedder.calls[1], ["保存ボタンを押して画面を閉じます。"])
        self.assertEqual(semantics.trace()["calls"], 2)


# 言い回しの違う手順の根拠の章節（#2685）。手順 1・2 の主の操作の文、章節全体に掛かる共通の文、末尾の共通の操作、
# 別の操作の文が 1 つの chunk に並ぶ。手順 2 の手がかりは共通の文の 1 語（「必要」）にだけ当たり、手順 1 の語は根拠に無い。
SECTION_COMMON = "全体で必要な項目をすべて確認します。"
SECTION = SHARED_A + SHARED_B + SECTION_COMMON + SHARED_END + STEP_C
SECTION_TERMS = (("s2", ("必要な手続",)),)
# 合成の vector（成分: 章節の話題・手順 1 の操作・手順 2 の操作・確認の一般の語・保存・承認・手順 2 の主の操作だけの内容）。
# 共通の文は話題と一般の語を多く持つので、手順 2 の記述に主の操作の文より近く、手順 1 との差も大きい（絶対の近さで
# 選ぶと主の操作の文になる）。末尾の共通の操作は両手順に同じくらい近い。
SECTION_DESCRIPTIONS = (("値の登録", (0.6, 0.6, 0.0, 0.3, 0.0, 0.0, 0.0)), ("内容の確定", (0.6, 0.0, 0.5, 0.5, 0.0, 0.0, 0.0)))
SECTION_SENTENCES = (
    ("設定 A の値", (0.6, 0.7, 0.0, 0.1, 0.0, 0.0, 0.0)),
    ("反映ボタンを押", (0.5, 0.0, 0.45, 0.1, 0.0, 0.0, 0.5)),
    ("全体で必要な項目", (0.8, 0.0, 0.15, 0.6, 0.0, 0.0, 0.0)),
    ("保存ボタンを押", (0.7, 0.0, 0.0, 0.2, 0.6, 0.0, 0.0)),
    ("承認を依頼", (0.6, 0.0, 0.0, 0.0, 0.0, 0.7, 0.0)),
)
SECTION_VECTORS = (*SECTION_DESCRIPTIONS, *SECTION_SENTENCES)
# 公開した item（引用と原文のみ提示か）と、正しい手順の状態（Issue #2685 の表の 4 行と、末尾の共通の操作だけ）。
SECTION_ROWS = (
    ("共通の文だけ", ((SECTION_COMMON, False),), {"s1": "unmatched", "s2": "unmatched"}),
    ("手順 1 の主の操作と共通の文", ((SHARED_A, False), (SECTION_COMMON, False)), {"s1": "matched", "s2": "unmatched"}),
    ("手順 1・2 の主の操作", ((SHARED_A, False), (SHARED_B, False)), {"s1": "matched", "s2": "matched"}),
    ("手順 2 の主の操作の原文のみ提示と共通の文", ((SHARED_B, True), (SECTION_COMMON, False)), {"s1": "unmatched", "s2": "reference_only"}),
    ("末尾の共通の操作だけ", ((SHARED_END, False),), {"s1": "unmatched", "s2": "unmatched"}),
)


class GuideStepSpecificOwnerTest(unittest.TestCase):
    """手順の主の操作の文を、絶対の近さではなく手順に固有な度合いで決める（#2685）。"""

    def setUp(self):
        self.span = {"source_id": "run:s", "evidence_id": "e-s", "label": "E1", "text": SECTION}

    def _candidate_statuses(self, items, semantics):
        checked = []
        for quote, quote_only in items:
            entry = grounded.CheckedItem(
                GroundedItem(kind="operation", text="t", citations=[GroundedCitation(evidence_id="", quote=quote)], applies="matched"),
                span=self.span,
            )
            entry.quote_only = quote_only
            checked.append(entry)
        coverage = grounded.guide_step_coverage(REWORDED_GUIDE, checked, [self.span], (), dict(SECTION_TERMS), semantics)
        return {step["step_id"]: step["assignment"]["candidate_status"] for step in coverage}

    def test_common_sentence_is_nearer_than_the_main_operation(self):
        """前提: 共通の文は手順 2 の記述に主の操作の文より近く、手順 1 との差も閾値以上（絶対の近さでは共通の文を選ぶ）。"""
        vectors = {key: grounded._unit_vector(vector) for key, vector in SECTION_VECTORS}

        def near(sentence, description):
            return grounded._cosine(vectors[sentence], vectors[description])

        self.assertGreater(near("全体で必要な項目", "内容の確定"), near("反映ボタンを押", "内容の確定"))
        self.assertGreaterEqual(
            near("全体で必要な項目", "内容の確定") - near("全体で必要な項目", "値の登録"), grounded.GUIDE_STEP_COMMON_MARGIN
        )

    def test_main_operation_is_chosen_by_specificity(self):
        for name, items, expected in SECTION_ROWS:
            with self.subTest(name):
                self.assertEqual(
                    self._candidate_statuses(items, grounded.GuideStepSemantics(StubEmbedder(SECTION_VECTORS).embed)), expected
                )

    def test_word_fallback_does_not_map_a_one_word_match(self):
        """語の判定だけ（Embedder が無い・失敗）では、手順の語が 1 語だけ当たる共通の文の item を手順の説明にしない。"""
        for name, items, _expected in SECTION_ROWS:
            with self.subTest(name):
                self.assertEqual(self._candidate_statuses(items, None), {"s1": "unmatched", "s2": "unmatched"})
                failing = grounded.GuideStepSemantics(StubEmbedder(SECTION_VECTORS, fail=True).embed)
                self.assertEqual(self._candidate_statuses(items, failing), {"s1": "unmatched", "s2": "unmatched"})

    def test_undetermined_main_operation_falls_to_the_safe_side(self):
        """手順に固有な度合いが同じくらいの文が 2 つあり主の操作の文が決まらない手順は、どの item の説明にもしない。"""
        tied = tuple((key, SECTION_SENTENCES[1][1] if key == "全体で必要な項目" else vector) for key, vector in SECTION_VECTORS)
        for name, items, expected in SECTION_ROWS:
            with self.subTest(name):
                safe = {**expected, "s2": "unmatched"}
                self.assertEqual(self._candidate_statuses(items, grounded.GuideStepSemantics(StubEmbedder(tied).embed)), safe)


class GuideStepSemanticAnswerTest(unittest.TestCase):
    """回答の round と確定で embedding を再利用し、trace に呼び出しを残す（#2649）。"""

    def test_embedding_is_reused_without_semantic_regeneration(self):
        ctx = _context(_record(0, SHARED, "設定画面", "run:s"))
        knowledge = _knowledge(REWORDED_GUIDE)
        (eid,) = _ids(ctx, knowledge).values()
        a = item(eid, "設定画面で設定 A の値を入力します。", SHARED_A, request_id="Q1")
        b = item(eid, "反映画面で反映ボタンを押して変更を反映します。", SHARED_B, request_id="Q1")
        end = item(eid, "最後に保存ボタンを押して画面を閉じます。", SHARED_END, request_id="Q1")
        model = FakeModel(
            [draft(a, b, end)],
            [
                audit(
                    (0, "supported", "matched", ""),
                    (1, "supported", "conditional", "原文に無い条件"),
                    (2, "supported", "matched", ""),
                    requests=[("Q1", "addressed")],
                ),
                audit(
                    (0, "supported", "matched", ""),
                    (1, "supported", "matched", ""),
                    (2, "supported", "matched", ""),
                    requests=[("Q1", "addressed")],
                ),
            ],
        )
        embedder = StubEmbedder(REWORDED_VECTORS)

        def unused(*a, **k):
            return (_ for _ in ()).throw(AssertionError("unexpected I/O"))

        with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused, embedder=embedder)):
            result = synthesize_grounded_answer(
                QUESTION, ctx, get_settings(), image_prompt_mode="text_only", runtime_knowledge=knowledge
            ).response

        rounds = result.generation_trace["rounds"]
        self.assertEqual(len(_drafts(model)), 1)
        # 観察による追加生成はしない。対応候補のembeddingは確定処理でも再利用する。
        self.assertEqual([step["status"] for step in rounds[0]["guide_steps"]], ["unknown", "unknown"])
        self.assertEqual([step["status"] for step in result.generation_trace["guide_step_coverage"]], ["unknown", "unknown"])
        self.assertEqual(result.envelope["outcome"], "conditional")
        # 同じ文の embedding は round と回答の確定で再利用する。
        self.assertEqual(len(embedder.calls), 1)
        self.assertEqual(result.generation_trace["guide_step_embedding"]["calls"], 1)
        self.assertFalse(result.generation_trace["guide_step_embedding"]["failed"])

    def test_no_embedding_trace_without_shared_evidence(self):
        ctx, knowledge = _ab_context(), _knowledge(GUIDE)
        eids = _ids(ctx, knowledge)
        a = item(eids[STEP_A], "設定画面で設定 A の値を入力します。", STEP_A, request_id="Q1")
        b = item(eids[STEP_B], "反映画面で反映ボタンを押して確定します。", STEP_B, request_id="Q1")
        model = FakeModel(
            [draft(a, b)], [audit((0, "supported", "matched", ""), (1, "supported", "matched", ""), requests=[("Q1", "addressed")])]
        )
        embedder = StubEmbedder(())

        def unused(*a, **k):
            return (_ for _ in ()).throw(AssertionError("unexpected I/O"))

        with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused, embedder=embedder)):
            result = synthesize_grounded_answer(
                QUESTION, ctx, get_settings(), image_prompt_mode="text_only", runtime_knowledge=knowledge
            ).response

        self.assertEqual(embedder.calls, [])
        self.assertNotIn("guide_step_embedding", result.generation_trace)
        self.assertEqual(result.envelope["outcome"], "conditional")


class GuidePlanRequiredTest(unittest.TestCase):
    def test_required_flag_from_payload(self):
        plan = GuidePlan.from_payload(
            "g",
            1,
            [{"id": "s1", "title": "A"}, {"id": "s2", "title": "B", "required": False}, {"id": "s3", "title": "C", "required": True}],
        )
        assert plan is not None
        self.assertEqual([step.required for step in plan.steps], [True, False, True])
        self.assertEqual(plan.optional_step_ids, ("s2",))

    def test_step_terms_from_payload(self):
        plan = GuidePlan.from_payload("g", 1, [{"id": "s1", "title": "A", "retrieval_hints": ["x", " ", "y"]}, {"id": "s2", "title": "B"}])
        assert plan is not None
        self.assertEqual(plan.step_terms, (("s1", ("x", "y")),))


if __name__ == "__main__":
    unittest.main()
