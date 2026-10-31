"""確認の項目を、モデルの自己申告の分類ではなく観察できる事実と規則で分ける (#1492)。

評価 e1467 で、手順を答えたうえで「利用者がロールを持つか」「付与先は個別とグループのどちらか」を「実データ」に
挙げ、回答が needs_environment_data になっていた。承認済みの条件（conditions）を聞き直し、廃止済みの第 1 版の
「適用されている版」も実データとして聞いていた。モデルには項目ごとに誰が答えられるか（who_can_answer）・すでに
分かっているか（already_known）・結論がその値で分かれるか（decides）だけを書かせ、対応は規則で決める。
LLM は差し替える。
"""

import itertools
import unittest
from dataclasses import replace

from test_grounded_answer import FakeModel, audit, context, draft, ids

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import grounded
from rag_engine.generation.answer_envelope import (
    EXCLUDED_ALREADY_KNOWN,
    EXCLUDED_KNOWN_CONDITION,
    EXCLUDED_NOT_DECISIVE,
    EXCLUDED_VERSION_SETTLED,
    build_envelope,
    confirmation_item_facts,
    requires_environment_data,
    settle_confirmation_items,
)
from rag_engine.generation.answer_models import AnswerRecord
from rag_engine.generation.answering import synthesize_grounded_answer

ROLE = "操作する利用者が「ポータル管理者」ロールを持つか（自分で操作するか、依頼するかを決める）"
APPROVAL = "削除対象アカウントの所属部署の部門長の承認が得られているか（承認の無い削除を行わないため）"
AUTH_LOG = "エラー発生時刻の認証ログ（auth.log）の該当行（原因 A と原因 B のどちらかを確定するため）"
CODE_TABLE = "「支払区分コード表」の区分の定義（区分の意味を確かめる）"
VERSION = "利用中のポータルに適用されている版（第1版と第2版のどちらの出力手順を使うかの判定）"


def fact(text, who, *, known=False, decides=True):
    return {"text": text, "who_can_answer": who, "already_known": known, "decides": decides}


class SettleConfirmationItemsTest(unittest.TestCase):
    def test_every_combination_of_facts(self):
        # 分かっている項目と結論を分けない項目は数えず、残りは誰が答えられるかだけで分ける。
        for who, known, decides in itertools.product(("user", "system", "document"), (False, True), (False, True)):
            with self.subTest(who=who, known=known, decides=decides):
                settled = settle_confirmation_items([fact("確かめる点", who, known=known, decides=decides)])
                groups = {"user": settled.user, "system": settled.system, "document": settled.document}
                if known or not decides:
                    self.assertEqual(groups, {"user": (), "system": (), "document": ()})
                    reason = EXCLUDED_ALREADY_KNOWN if known else EXCLUDED_NOT_DECISIVE
                    self.assertEqual(settled.excluded, ({"text": "確かめる点", "who_can_answer": who, "reason": reason},))
                else:
                    self.assertEqual(groups[who], ("確かめる点",))
                    self.assertEqual(sum(len(value) for value in groups.values()), 1)
                    self.assertEqual(settled.excluded, ())

    def test_missing_facts_are_filled_conservatively(self):
        # 文字列だけ・#1453 の種類（kind）だけの項目: 種類から写し、無ければシステムの値。未確認・結論を分ける。
        self.assertEqual(
            confirmation_item_facts("ログ"), {"text": "ログ", "who_can_answer": "system", "already_known": False, "decides": True}
        )
        self.assertEqual(confirmation_item_facts({"text": "ロール", "kind": "user_condition"})["who_can_answer"], "user")
        self.assertEqual(confirmation_item_facts({"text": "ログ", "kind": "environment"})["who_can_answer"], "system")
        self.assertEqual(confirmation_item_facts({"text": "x"})["who_can_answer"], "system")

    def test_known_conditions_remove_the_items_that_ask_them_again(self):
        # mt-delete-account-approved: conditions={approved: はい}（業務ガイドの条件「部門長の承認」）を聞き直さない。
        items = [fact(APPROVAL, "user"), fact(ROLE, "user")]
        settled = settle_confirmation_items(items, known_conditions=[("部門長の承認", "はい")])
        self.assertEqual(settled.user, (ROLE,))
        self.assertEqual([entry["reason"] for entry in settled.excluded], [EXCLUDED_KNOWN_CONDITION])
        # 値の無い条件・1 文字の名前では外さない。
        self.assertEqual(settle_confirmation_items(items, known_conditions=[("部門長の承認", " ")]).user, (APPROVAL, ROLE))
        self.assertEqual(settle_confirmation_items(items, known_conditions=[("承", "はい")]).user, (APPROVAL, ROLE))

    def test_version_items_are_removed_only_when_the_evidence_settles_the_version(self):
        items = [fact(VERSION, "system"), fact(AUTH_LOG, "system")]
        self.assertEqual(settle_confirmation_items(items).system, (VERSION, AUTH_LOG))
        settled = settle_confirmation_items(items, version_settled=True)
        self.assertEqual(settled.system, (AUTH_LOG,))
        self.assertEqual(settled.excluded[0]["reason"], EXCLUDED_VERSION_SETTLED)

    def test_requires_environment_data_follows_the_settled_items(self):
        system = settle_confirmation_items([fact(AUTH_LOG, "system")])
        self.assertTrue(requires_environment_data(system, items=[fact(AUTH_LOG, "system")], external_data_required=False))
        # 項目を挙げてすべて外れたら、モデルが印を立てても要らない。
        known = [fact(AUTH_LOG, "system", known=True)]
        self.assertFalse(requires_environment_data(settle_confirmation_items(known), items=known, external_data_required=True))
        # 項目が無く印だけなら今までどおり印に従う。
        self.assertTrue(requires_environment_data(settle_confirmation_items([]), items=[], external_data_required=True))


class EnvelopeFromFactsTest(unittest.TestCase):
    MATCHED = {"kind": "operation", "text": "手順", "applies": "matched", "condition": ""}

    def outcome_of(self, items, **options):
        settled = settle_confirmation_items(items, **options)
        envelope = build_envelope(
            requests=(),
            reviews=[],
            items=[self.MATCHED],
            gaps=(),
            confirmations=[*(f"実データ: {text}" for text in settled.system), *(f"資料: {text}" for text in settled.document)],
            user_conditions=settled.user,
            document_items=settled.document,
            external_data_required=requires_environment_data(settled, items=items, external_data_required=False),
        )
        return envelope["outcome"]

    def test_outcome_by_who_can_answer(self):
        self.assertEqual(self.outcome_of([fact(ROLE, "user")]), "conditional")
        self.assertEqual(self.outcome_of([fact(AUTH_LOG, "system")]), "needs_environment_data")
        self.assertEqual(self.outcome_of([fact(CODE_TABLE, "document")]), "conditional")
        self.assertEqual(self.outcome_of([fact(ROLE, "user"), fact(AUTH_LOG, "system")]), "needs_environment_data")
        self.assertEqual(self.outcome_of([]), "answered")

    def test_known_or_indecisive_items_do_not_decide_the_outcome(self):
        self.assertEqual(self.outcome_of([fact(AUTH_LOG, "system", known=True)]), "answered")
        self.assertEqual(self.outcome_of([fact(AUTH_LOG, "system", decides=False)]), "answered")
        self.assertEqual(self.outcome_of([fact(APPROVAL, "user")], known_conditions=[("部門長の承認", "はい")]), "answered")
        self.assertEqual(self.outcome_of([fact(VERSION, "system")], version_settled=True), "answered")


# 実サービス m1492-deepseek の grounded_draft（#1500）。cr-grant-permission の「検証用か」と ir-backup-restore の
# 台帳の列の値を、モデルが system にして needs_environment_data になっていた。
GRANT_ITEMS = [
    fact("権限の付与先を「個別の利用者」と「グループ」のどちらにするか（どちらの付与手順を使うかを決める）", "user"),
    fact("操作する利用者が「ポータル管理者」ロールを持つか（自分で操作するか、所属部署のポータル管理者に依頼するかを決める）", "user"),
    fact("グループへ付与する場合に、付与の前に部門長の承認を得ているか（グループへの付与を行えるかを決める）", "user"),
]
GRANT_TEST_ACCOUNT = "付与対象のアカウントが検証用かどうか（グループへの付与を行わず個別付与に限るかの判断）"
RESTORE_SYSTEM = "復元したいデータのシステム(どのシステムのデータを戻すか。承認を求める担当部署と承認者が決まる)"
RESTORE_LEDGER = "対象システムのシステム台帳「担当部署」列の値(承認を求める部署を決める)"
RESTORE_APPROVER = "対象システムを担当する部署の承認者が誰か(組織規程に定める部署ごとの承認者。どの承認者に承認を求めるかが決まる)"
E1023_ITEMS = [
    fact(
        "エラーが出た時刻の認証ログ（auth.log）の該当行が「token expired」か「permission denied」か"
        "（原因 A と原因 B のどちらに当たるかを決める）",
        "system",
    ),
    fact(
        "操作している利用者が「ポータル管理者」ロールを持つか（認証ログを自分で確認できるか、確認を依頼する必要があるかを決める）", "user"
    ),
]


class WhoCanAnswerDefinitionTest(unittest.TestCase):
    """who_can_answer の定義を値の出どころで排他に分ける (#1500)。

    資料（台帳・一覧・表）に載る値は document、利用者が自分の状況として答えられる属性は user、system は稼働中の
    システムにだけある値（ログ・設定の現在値・処理の状態・実績）。
    """

    def sections(self):
        from rag_engine.models.llm import ExternalDataItem

        prompt = grounded.GENERATE_SYSTEM_PROMPT.split("### 未確認事項・実データ・別の資料", 1)[1].split("### 方針と items の対応", 1)[0]
        return prompt, ExternalDataItem.model_fields["who_can_answer"].description

    def test_ledger_values_are_document_not_system(self):
        prompt, schema = self.sections()
        for text in (prompt, schema):
            with self.subTest(text=text[:20]):
                self.assertIn("台帳・一覧・表の値", text.split("document=", 1)[1].split("system=", 1)[0])
                system = text.split("system=", 1)[1].split("。", 1)[0]
                self.assertNotIn("台帳", system)
                self.assertNotIn("対象レコード", system)
                self.assertIn("ログの行", system)
                # 旧い反例（台帳の担当部署は system）を残さない。
                self.assertNotIn("台帳の値なので system", text)
                self.assertIn("台帳の担当部署は台帳が資料なので", text.replace("「", "").replace("」", ""))
        self.assertIn("根拠に台帳の行があれば挙げず、無ければ document", prompt)

    def test_attributes_of_the_target_the_user_knows_are_user(self):
        prompt, schema = self.sections()
        for text in (prompt, schema):
            with self.subTest(text=text[:20]):
                user = text.split("user=", 1)[1].split("document=", 1)[0]
                self.assertIn("対象のアカウントの種類（検証用か・共用か）", user)
                self.assertIn("付与対象のアカウントが検証用か", text.replace("「", "").replace("」", ""))
        # ログの行は今までどおり system の反例。
        self.assertIn("「エラーの時刻の認証ログ（auth.log）の行」は資料にも利用者の記憶にも無いので system", prompt)

    def test_items_classified_by_the_definition_settle_the_outcome(self):
        outcome = EnvelopeFromFactsTest().outcome_of
        # 「検証用か」を user にすれば、条件で分けて答えた回答は conditional（system にすると needs_environment_data）。
        self.assertEqual(outcome([*GRANT_ITEMS, fact(GRANT_TEST_ACCOUNT, "user")]), "conditional")
        self.assertEqual(outcome([*GRANT_ITEMS, fact(GRANT_TEST_ACCOUNT, "system")]), "needs_environment_data")
        # 台帳の列の値は、根拠に台帳の行が無ければ document（資料の不足）。対象のシステムは user。
        restore = [fact(RESTORE_SYSTEM, "user"), fact(RESTORE_APPROVER, "document")]
        self.assertEqual(outcome([*restore, fact(RESTORE_LEDGER, "document")]), "conditional")
        self.assertEqual(outcome(restore), "conditional")
        # ログの行（ed-error-e1023-cause）は今までどおり needs_environment_data。
        self.assertEqual(outcome(E1023_ITEMS), "needs_environment_data")


V1_TITLE = "地域別集計表 出力手順 第1版"
V2_TITLE = "地域別集計表 出力手順 第2版"
V1_STEP = "メニューの「レポート」を開き、「地域別集計」を選んで対象月を入力し、「出力」を押します。"
V2_STEP = "メニューの「集計」を開き、「地域別」を選びます。続いて対象月を選び、「出力」を押します。"
V2_ABOLISHED = "第2版の施行日は 2026年4月1日で、第1版は第2版の施行により廃止されています。"


def versioned(index, text, title, source, *, superseded=False):
    metadata = {"section_path": [title, "出力の手順"], **({"document_superseded": True} if superseded else {})}
    return AnswerRecord(
        id=f"r{index}",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=index + 1,
        category="Chunk",
        text=text,
        source=source,
        chunk_level="parent",
        chunk_id=f"r{index}",
        source_run_id="run",
        metadata=metadata,
    )


class SupersededEvidenceTest(unittest.TestCase):
    def _spans(self, *, superseded=False, abolished=True):
        v2 = [{"evidence_id": "E2", "text": V2_STEP, "section_path": [V2_TITLE], "source": "guide-v2.pdf"}]
        if abolished:
            v2.append({"evidence_id": "E3", "text": V2_ABOLISHED, "section_path": [V2_TITLE], "source": "guide-v2.pdf"})
        v1 = {"evidence_id": "E1", "text": V1_STEP, "section_path": [V1_TITLE], "source": "guide-v1.pdf", "superseded": superseded}
        return [*v2, v1]

    def test_version_abolished_by_the_current_evidence(self):
        # da-regional-report-export: 第 2 版の原文が第 1 版の廃止を明記している。
        self.assertEqual(grounded.superseded_evidence_ids("地域別集計表を出力する手順を教えてください。", self._spans()), {"E1"})

    def test_superseded_metadata(self):
        spans = self._spans(superseded=True, abolished=False)
        self.assertEqual(grounded.superseded_evidence_ids("地域別集計表を出力する手順は？", spans), {"E1"})
        # 廃止の記述もメタデータも無ければ、版を推測しない。
        self.assertEqual(grounded.superseded_evidence_ids("地域別集計表を出力する手順は？", self._spans(abolished=False)), set())

    def test_questions_about_versions_keep_the_old_version(self):
        for question in (
            "地域別集計表の出力手順は、第1版と第2版で何が変わりましたか？",
            "第1版の地域別集計表の締め日は何日でしたか？",
            "今の承認規程と 2023 年度版の承認規程では、期限はどう変わりましたか？",
        ):
            with self.subTest(question):
                self.assertEqual(grounded.superseded_evidence_ids(question, self._spans(superseded=True)), set())


def _run(model, ctx, question, **settings):
    unused = lambda *a, **k: (_ for _ in ()).throw(AssertionError("unexpected I/O"))  # noqa: E731
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        return synthesize_grounded_answer(question, ctx, replace(get_settings(), **settings), image_prompt_mode="text_only").response


def rule(eid, text, kind="rule"):
    return {"kind": kind, "text": text, "citations": [{"evidence_id": eid, "quote": text}], "request_id": "Q1"}


class GroundedConfirmationFactsTest(unittest.TestCase):
    def test_abolished_version_gets_a_warning_and_is_not_asked(self):
        question = "地域別集計表を出力する手順を教えてください。"
        ctx = context(
            versioned(0, V2_STEP, V2_TITLE, "guide-v2.pdf"),
            versioned(1, V2_ABOLISHED, V2_TITLE, "guide-v2.pdf"),
            versioned(2, V1_STEP, V1_TITLE, "guide-v1.pdf"),
        )
        eids = ids(ctx, question)
        value = {
            **draft(
                rule(eids[V2_STEP], V2_STEP, "operation"),
                rule(eids[V2_ABOLISHED], V2_ABOLISHED),
                rule(eids[V1_STEP], V1_STEP, "operation"),
                summary="",
            ),
            "external_data_required": True,
            "external_data_items": [fact(VERSION, "system")],
        }
        reviews = [(index, "supported", "matched", "") for index in range(3)]
        response = _run(FakeModel([value], [audit(*reviews, requests=[("Q1", "addressed")])]), ctx, question)
        envelope = response.envelope
        # 旧版の説明は言い換えを残して警告（superseded）を付ける（#2738）。警告だけなので answered。
        self.assertEqual(envelope["outcome"], "conditional")
        self.assertIn("rule_warnings", envelope["handoff_reasons"])
        self.assertEqual(envelope["confirmations"], [])
        self.assertFalse(response.external_data_required)
        # 旧版の警告は検査の不合格ではない（人の確認に回さない）。
        self.assertTrue(response.needs_human_review)
        marks = {item["text"]: (item["applies"], item["mark"]) for item in envelope["items"]}
        self.assertEqual(marks[V1_STEP], ("matched", "warning"))
        self.assertEqual(marks[V2_STEP], ("matched", ""))
        (passage,) = envelope["passage_marks"]
        self.assertTrue(passage["text"].endswith(V1_STEP))
        self.assertEqual([reason["code"] for reason in passage["reasons"]], ["superseded"])
        structure = response.generation_trace["grounded_draft"]
        self.assertEqual(structure["superseded_evidence_ids"], [eids[V1_STEP]])
        self.assertEqual(structure["superseded_withheld"], 1)
        self.assertEqual(structure["settled_confirmations"]["excluded"][0]["reason"], EXCLUDED_VERSION_SETTLED)
        self.assertEqual(structure["confirmation_items"], [fact(VERSION, "system")])

    def test_known_condition_is_not_asked_again(self):
        # mt-delete-account-approved と同じ形（承認は conditions で分かっている）。削除の検査に掛からない操作にする。
        question = "利用者アカウントを無効にしたいです。所属部署の部門長の承認はもらっています。"
        text = "無効にする前に、所属部署の部門長の承認を得てください。"
        ctx = context(versioned(0, text, "アカウントの無効化", "portal-operations-manual.pdf"))
        eids = ids(ctx, question)
        value = {
            **draft(rule(eids[text], text), summary=""),
            "external_data_required": True,
            "external_data_items": [fact(APPROVAL, "system")],
        }
        model = lambda: FakeModel([value], [audit((0, "supported", "matched", ""), requests=[("Q1", "addressed")])])  # noqa: E731
        self.assertEqual(_run(model(), ctx, question).envelope["outcome"], "needs_environment_data")
        response = _run(model(), ctx, question, known_conditions=(("部門長の承認", "はい"),))
        self.assertEqual(response.envelope["outcome"], "conditional")
        self.assertNotIn("実データ", response.answer_text)


if __name__ == "__main__":
    unittest.main()
