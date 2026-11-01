"""画面の図の項目の種類（form_fields の kind。#2404）の抽出・chunk の本文・台帳・回答の根拠の注記。

VLM は呼ばない（録画した応答の dict を describe_picture の差し替えで返す）。画面の図・項目名は合成の値だけを使う。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from rag_parser_core.language import FORM_FIELD_KIND_CHOICES, current_pack

from rag_engine.config import get_settings
from rag_engine.generation import form_field_kinds
from rag_engine.generation.answer_models import AnswerContext
from rag_engine.generation.answering import _grounded_prompt
from rag_engine.models.layout import LayoutRecord, PageImage
from rag_engine.models.llm import FORM_FIELD_KINDS, FormFieldOutput, GroundedCitation, PictureDescriptionOutput
from rag_engine.parsing.content_labels import content_labels
from rag_engine.parsing.picture_descriptions import _bounded_vision_description, describe_docling_pictures, vision_description_text
from rag_engine.parsing.vision_checkpoints import VisionCheckpoints
from rag_engine.parsing.vision_prompt_rules import refine_image_retrieval_prompt, vision_extraction_rules

# 合成の画面の図の VLM の応答（録画応答）。選択の項目・自由入力の項目・種類の分からない項目を 1 つずつ持つ。
RECORDED_RESPONSE = {
    "main_topic": "設定画面",
    "visual_kind": "screenshot",
    "form_fields": [
        {"name": "区分コード", "value": "A1", "state": "enabled", "kind": "select"},
        {"name": "備考欄", "value": "", "state": "enabled", "kind": "text"},
        {"name": "表示順", "value": "1", "state": "", "kind": "unknown"},
    ],
    "visible_fields": ["区分コード", "備考欄", "表示順"],
    "retrieval_text": "設定画面の項目",
}


def _record(record_id: str = "docling-p1-1") -> LayoutRecord:
    return LayoutRecord(record_id, "docling", 1, 1, [10, 10, 80, 80], "image_top_left", 100, 100, "Picture", raw_type="picture")


def _chunk(uid: str, fields: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(
        chunk_uid=uid,
        id=uid,
        metadata={"image_evidence": [{"image_id": f"{uid}-img", "visual_structure": {"visual_kind": "form", "form_fields": fields}}]},
    )


def _context(*records: SimpleNamespace) -> AnswerContext:
    return AnswerContext(records=list(records), text="")


class OutputContractTests(unittest.TestCase):
    def test_kind_is_a_fixed_enum_and_falls_back_to_unknown(self) -> None:
        schema = FormFieldOutput.model_json_schema()["properties"]["kind"]
        self.assertEqual(tuple(schema["enum"]), FORM_FIELD_KINDS)
        self.assertEqual(FormFieldOutput(name="a", value="", state="", kind=" Select ").kind, "select")
        # 構造化出力を強制しない接続先の決まった値の外の語・空は unknown（推測で埋めない）。
        self.assertEqual(FormFieldOutput(name="a", value="", state="", kind="dropdown").kind, "unknown")
        self.assertEqual(FormFieldOutput(name="a", value="", state="", kind="").kind, "unknown")
        self.assertIn("kind", PictureDescriptionOutput.model_json_schema()["$defs"]["FormFieldOutput"]["required"])

    def test_rules_give_visual_criteria_and_unknown_for_kind(self) -> None:
        rules = vision_extraction_rules()
        self.assertIn("name / value / state / kind", rules)
        self.assertIn("unknown にし、項目名や値から推測しない", rules)

    def test_saved_json_is_preserved_and_fixed_contract_defines_kind(self) -> None:
        old = (
            '  "form_fields": [{"name": "項目名", "value": "表示値。空欄なら空文字", "state": '
            '"selected / unselected / enabled / disabled / required / optional / unknown"}],'
        )
        from rag_engine.adapters.model_api import _picture_output_contract, _render_prompt

        saved = "方針\n{\n" + old + "\n}"
        refined = refine_image_retrieval_prompt(saved)
        self.assertIn(saved, refined)
        rendered = _render_prompt({}, template=saved)
        self.assertIn(saved, rendered)
        contract = _picture_output_contract()
        # Step の見出しの無いテンプレートの後では、対象の指示が Step 1、出力契約が Step 2 になる(#3810)。
        self.assertTrue(rendered.endswith(contract.replace("## Step 7:", "## Step 2:", 1)))
        schema = json.loads(contract.rsplit("\n", 1)[1])
        field = schema["$defs"]["FormFieldOutput"]
        self.assertIn("kind", field["required"])
        self.assertEqual(tuple(field["properties"]["kind"]["enum"]), FORM_FIELD_KINDS)

    def test_language_pack_labels_every_kind_except_unknown(self) -> None:
        self.assertEqual(FORM_FIELD_KIND_CHOICES, tuple(kind for kind in FORM_FIELD_KINDS if kind != "unknown"))
        labels = content_labels(current_pack())
        for kind in FORM_FIELD_KIND_CHOICES:
            self.assertTrue(labels.form_field_kind(kind), kind)
        self.assertEqual(labels.form_field_kind("unknown"), "")


class ChunkTextTests(unittest.TestCase):
    def test_form_field_lines_carry_kind_label(self) -> None:
        text = vision_description_text(_bounded_vision_description(RECORDED_RESPONSE))
        self.assertIn("- 区分コード = A1 [enabled] (選択)", text)
        self.assertIn("- 備考欄 [enabled] (自由入力)", text)
        # 種類が分からない項目は種類を付けない。
        self.assertIn("- 表示順 = 1\n", text + "\n")

    def test_bounded_description_normalizes_kind(self) -> None:
        description = _bounded_vision_description({"form_fields": [{"name": "a", "kind": "SELECT"}, {"name": "b"}, "x"]})
        self.assertEqual(description["form_fields"], [{"name": "a", "kind": "select"}, {"name": "b", "kind": "unknown"}, "x"])

    def test_ingest_separates_select_and_text_fields_in_text_and_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp)
            page_path = run_dir / "page.png"
            Image.new("RGB", (100, 100), "white").save(page_path)
            page = PageImage(page=1, width=100, height=100, pdf_width=100, pdf_height=100, image_path=str(page_path))
            record = _record()
            with patch("rag_engine.parsing.picture_descriptions.describe_picture", return_value=dict(RECORDED_RESPONSE)):
                stats = describe_docling_pictures([record], [page], run_dir=run_dir, pdf_name="synthetic.pdf", settings=get_settings())
        self.assertEqual((stats.succeeded, stats.failed), (1, 0))
        self.assertIn("区分コード = A1 [enabled] (選択)", record.text)
        self.assertIn("備考欄 [enabled] (自由入力)", record.text)
        kinds = {field["name"]: field["kind"] for field in record.raw["visual_structure"]["form_fields"]}
        self.assertEqual(kinds, {"区分コード": "select", "備考欄": "text", "表示順": "unknown"})


class LedgerTests(unittest.TestCase):
    def _save(self, ledger: VisionCheckpoints, key: str, response: dict) -> None:
        ledger.save(key, response, _record(), state="succeeded", reusable=True)

    def test_response_without_kind_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = VisionCheckpoints(Path(tmp), "hash")
            legacy = json.loads(json.dumps(RECORDED_RESPONSE))
            for field in legacy["form_fields"]:
                field.pop("kind")
            self._save(ledger, "a" * 64, legacy)
            self._save(ledger, "b" * 64, dict(RECORDED_RESPONSE))
            self._save(ledger, "c" * 64, {"retrieval_text": "図", "form_fields": []})
            self.assertIsNone(ledger.load("a" * 64))
            self.assertEqual(ledger.load("b" * 64), RECORDED_RESPONSE)
            self.assertIsNotNone(ledger.load("c" * 64))


class AnswerEvidenceTests(unittest.TestCase):
    SELECT = [{"name": "区分コード", "value": "", "state": "", "kind": "select"}]
    TEXT = [{"name": "区分コード", "value": "", "state": "", "kind": "text"}]
    # 業務ガイドの条件: 選択肢が項目の種類の語（言語資源）と同じ。言い換えでも対応できる。
    CONDITION = ("field_kind", "項目の種類", (("選択", ()), ("入力欄", ("自由入力",))))

    def test_only_fields_named_in_question_are_observed(self) -> None:
        context = _context(_chunk("c1", [*self.SELECT, {"name": "表示順", "kind": "text"}, {"name": "x", "kind": "select"}]))
        observed = form_field_kinds.question_field_kinds("区分コードの選択肢を追加したい", context)
        self.assertEqual(observed, [form_field_kinds.FieldKindObservation("c1", "区分コード", "select")])

    def test_child_fields_are_attributed_to_parent(self) -> None:
        child = _chunk("child-1", self.SELECT)
        parent = SimpleNamespace(chunk_uid="parent-1", id="parent-1", metadata={})
        tree = (SimpleNamespace(record=parent, children=(SimpleNamespace(record=child),)),)
        context = AnswerContext(records=[parent], text="", evidence_tree=tree)
        observed = form_field_kinds.question_field_kinds("区分コード", context)
        self.assertEqual([item.source_id for item in observed], ["parent-1"])

    def test_condition_resolves_when_kind_is_single(self) -> None:
        observed = form_field_kinds.question_field_kinds("区分コード", _context(_chunk("c1", self.SELECT)))
        resolved = form_field_kinds.resolve_conditions(observed, [self.CONDITION])
        self.assertEqual(resolved, [form_field_kinds.ResolvedCondition("field_kind", "項目の種類", "選択", "select")])
        text = form_field_kinds.question_field_kinds("区分コード", _context(_chunk("c2", self.TEXT)))
        self.assertEqual(form_field_kinds.resolve_conditions(text, [self.CONDITION])[0].value, "入力欄")

    def test_condition_stays_unknown_when_kinds_conflict_or_missing(self) -> None:
        both = form_field_kinds.question_field_kinds("区分コード", _context(_chunk("c1", self.SELECT), _chunk("c2", self.TEXT)))
        self.assertEqual(len(both), 2)
        self.assertEqual(form_field_kinds.resolve_conditions(both, [self.CONDITION]), [])
        self.assertEqual(form_field_kinds.resolve_conditions([], [self.CONDITION]), [])

    def test_overlapping_button_name_does_not_conflict_with_the_named_field(self) -> None:
        fields = [{"name": "対象種別", "kind": "select"}, {"name": "対象", "kind": "button"}]
        observed = form_field_kinds.question_field_kinds("対象種別を変更したい", _context(_chunk("c1", fields)))
        condition = ("field_kind", "対象種別の項目の種類", self.CONDITION[2])
        resolved = form_field_kinds.resolve_conditions(observed, [condition])
        self.assertEqual(resolved, [form_field_kinds.ResolvedCondition("field_kind", condition[1], "選択", "select")])

    def test_each_condition_binds_to_its_own_field(self) -> None:
        observed = [
            form_field_kinds.FieldKindObservation("c1", "項目Ａ", "select"),
            form_field_kinds.FieldKindObservation("c2", "項目Ｂ", "text"),
        ]
        conditions = [("a", "項目Aの種類", self.CONDITION[2]), ("b", "項目Bの種類", self.CONDITION[2])]
        resolved = form_field_kinds.resolve_conditions(observed, conditions)
        self.assertEqual([(item.condition_id, item.value) for item in resolved], [("a", "選択"), ("b", "入力欄")])

    def test_same_named_field_with_conflicting_sources_stays_unknown(self) -> None:
        observed = [
            form_field_kinds.FieldKindObservation("c1", "対象種別", "select"),
            form_field_kinds.FieldKindObservation("c2", "対象種別", "text"),
            form_field_kinds.FieldKindObservation("c3", "対象", "button"),
        ]
        self.assertEqual(form_field_kinds.resolve_conditions(observed, [("field_kind", "対象種別の種類", self.CONDITION[2])]), [])

    def test_missing_field_is_not_filled_from_another_conditions_field(self) -> None:
        observed = [form_field_kinds.FieldKindObservation("c1", "項目A", "select")]
        conditions = [("a", "項目Aの種類", self.CONDITION[2]), ("b", "項目Bの種類", self.CONDITION[2])]
        resolved = form_field_kinds.resolve_conditions(observed, conditions)
        self.assertEqual([(item.condition_id, item.value) for item in resolved], [("a", "選択")])

    def test_multiple_fields_without_a_binding_stay_unknown_even_with_the_same_kind(self) -> None:
        observed = [
            form_field_kinds.FieldKindObservation("c1", "項目A", "select"),
            form_field_kinds.FieldKindObservation("c2", "項目B", "select"),
        ]
        self.assertEqual(form_field_kinds.resolve_conditions(observed, [self.CONDITION]), [])
        self.assertEqual(form_field_kinds.resolve_conditions(observed, [("field_kind", "項目Aと項目Bの種類", self.CONDITION[2])]), [])

    def test_short_named_button_can_be_the_target(self) -> None:
        observed = [
            form_field_kinds.FieldKindObservation("c1", "対象", "button"),
            form_field_kinds.FieldKindObservation("c2", "対象種別", "select"),
        ]
        condition = ("field_kind", "対象の種類", (("ボタン", ("button",)), ("選択", ("select",))))
        resolved = form_field_kinds.resolve_conditions(observed, [condition])
        self.assertEqual([(item.value, item.kind) for item in resolved], [("ボタン", "button")])

    def test_only_conditions_whose_options_map_to_kinds_are_used(self) -> None:
        # 選択肢が項目の種類に 1 つしか対応しない条件・項目の種類と関係の無い条件は決めない。
        self.assertEqual(form_field_kinds.condition_kind_options([("選択", ()), ("その他", ())]), {})
        self.assertEqual(form_field_kinds.condition_kind_options([("はい", ()), ("いいえ", ())]), {})
        # 1 つの種類に 2 つの選択肢が当たる種類は使わない。
        self.assertEqual(
            form_field_kinds.condition_kind_options([("選択", ()), ("プルダウン", ()), ("自由入力", ())]),
            {},
        )
        self.assertEqual(
            form_field_kinds.condition_kind_options([("選択", ()), ("自由入力", ()), ("日付", ())]),
            {"select": "選択", "text": "自由入力", "date": "日付"},
        )

    def test_prompt_note_labels_evidence_and_known_condition(self) -> None:
        observed = form_field_kinds.question_field_kinds("区分コード", _context(_chunk("c1", self.SELECT), _chunk("c9", self.SELECT)))
        resolved = form_field_kinds.resolve_conditions(observed, [self.CONDITION])
        spans = [{"label": "E2", "source_id": "c1", "source_aliases": []}]
        note = form_field_kinds.prompt_note(observed, resolved, spans)
        # 生成に渡していない根拠（c9）の観測は出さない。
        self.assertEqual(
            note,
            "根拠の画面の図の項目の種類（質問に名前が出た項目）:\n- E2: 区分コード = 選択\n"
            "業務ガイドの条件のうち、根拠の画面の図の項目の種類で分かった値（この値に当たる分岐の手順で答える）: 項目の種類=選択",
        )
        self.assertEqual(form_field_kinds.prompt_note(observed, resolved, []), "")
        payload = form_field_kinds.trace_payload(observed, resolved)
        self.assertEqual(payload["resolved_conditions"], [{"id": "field_kind", "label": "項目の種類", "value": "選択", "kind": "select"}])

    def test_grounded_prompt_carries_note(self) -> None:
        prompt = _grounded_prompt(
            "区分コード",
            [],
            _context(),
            [],
            None,
            None,
            None,
            None,
            None,
            template="{{question}}\n{{answer_context}}",
            field_kind_note="NOTE-LINE",
        )
        self.assertIn("NOTE-LINE", prompt)


class ResolvedPlanTests(unittest.TestCase):
    """条件が決まったときの業務ガイドの手順の計画への切り替え（#2689）。"""

    PLAN = ((("field_kind", "選択"),), (("screen", True), ("code", True)), "guide-g1", "RESOLVED-RULE")

    def _knowledge(self):
        from rag_engine.knowledge.runtime_knowledge import RuntimeKnowledgeContext, RuntimeRule

        return RuntimeKnowledgeContext(
            original_question="q",
            expanded_question="q",
            path=Path("rk.json"),
            matched_rules=(RuntimeRule("guide-g1", "ガイド", content="UNRESOLVED-RULE"), RuntimeRule("r2", "別", content="OTHER")),
            guide_steps=(("screen", "画面", ("c1",)), ("code", "コード", ("c2",)), ("ask", "確認", ("c3",))),
            guide_optional_steps=("code",),
            guide_step_terms=(("code", ("コード",)), ("ask", ("確認",))),
        )

    def _resolved(self, value: str = "選択") -> list[form_field_kinds.ResolvedCondition]:
        return [form_field_kinds.ResolvedCondition("field_kind", "項目の種類", value, "select")]

    def test_switches_steps_required_and_rule(self) -> None:
        knowledge = form_field_kinds.with_resolved_plan(self._knowledge(), self._resolved(), [self.PLAN])
        self.assertEqual([step[0] for step in knowledge.guide_steps], ["screen", "code"])
        self.assertEqual(knowledge.guide_optional_steps, ())
        self.assertEqual(knowledge.guide_step_terms, (("code", ("コード",)),))
        self.assertEqual([rule.content for rule in knowledge.matched_rules], ["RESOLVED-RULE", "OTHER"])
        self.assertIn("RESOLVED-RULE", knowledge.prompt_context())

    def test_named_field_resolution_selects_the_required_branch(self) -> None:
        observed = [
            form_field_kinds.FieldKindObservation("c1", "対象種別", "select"),
            form_field_kinds.FieldKindObservation("c2", "対象", "button"),
        ]
        condition = ("field_kind", "対象種別の種類", (("選択", ()), ("自由入力", ())))
        resolved = form_field_kinds.resolve_conditions(observed, [condition])
        knowledge = form_field_kinds.with_resolved_plan(self._knowledge(), resolved, [self.PLAN])
        self.assertEqual([step[0] for step in knowledge.guide_steps], ["screen", "code"])
        self.assertEqual(knowledge.guide_optional_steps, ())
        self.assertEqual(knowledge.matched_rules[0].content, "RESOLVED-RULE")

    def test_acquisition_scope_and_notice_follow_only_the_resolved_owners(self) -> None:
        from dataclasses import replace

        knowledge = replace(
            self._knowledge(),
            guide_acquisition=(
                {"target_id": "shared", "step_ids": ["screen", "ask"], "retained_source_ids": [], "reason": "deadline"},
                {"target_id": "old", "step_ids": ["ask"], "retained_source_ids": []},
            ),
            guide_acquisition_notices=("OLD-NOTICE",),
        )
        narrowed = form_field_kinds.with_resolved_plan(knowledge, self._resolved(), [self.PLAN])
        self.assertEqual(len(narrowed.guide_acquisition), 1)
        self.assertEqual(narrowed.guide_acquisition[0]["step_ids"], ["screen"])
        self.assertIn("screen", narrowed.guide_acquisition_notices[0])
        self.assertNotIn("ask", narrowed.guide_acquisition_notices[0])
        self.assertNotIn("OLD-NOTICE", narrowed.guide_acquisition_notices)
        self.assertEqual(knowledge.guide_acquisition[0]["step_ids"], ["screen", "ask"])

    def test_keeps_knowledge_without_matching_plan(self) -> None:
        base = self._knowledge()
        self.assertIs(form_field_kinds.with_resolved_plan(base, [], [self.PLAN]), base)
        self.assertIs(form_field_kinds.with_resolved_plan(base, self._resolved("入力"), [self.PLAN]), base)
        self.assertIs(form_field_kinds.with_resolved_plan(base, self._resolved(), []), base)
        self.assertIsNone(form_field_kinds.with_resolved_plan(None, self._resolved(), [self.PLAN]))
        payload = form_field_kinds.trace_payload([], self._resolved(), [self.PLAN])
        self.assertEqual(payload["guide_plan"], [{"step_id": "screen", "required": True}, {"step_id": "code", "required": True}])
        self.assertNotIn("guide_plan", form_field_kinds.trace_payload([], self._resolved("入力"), [self.PLAN]))


if __name__ == "__main__":
    unittest.main()


class CitableFieldKindTests(unittest.TestCase):
    def _span(self, page=3, text="別の図の説明"):
        return {
            "source_id": "parent",
            "evidence_id": "original",
            "text": text,
            "source": "操作.pdf",
            "page": page,
            "origin": "document_text",
        }

    def test_structured_kind_is_citable_without_borrowing_unrelated_text(self):
        from rag_engine.generation import grounded
        from rag_engine.models.llm import GroundedDraft, GroundedItem

        observed = [form_field_kinds.FieldKindObservation("parent", "項目A", "select")]
        original = self._span()
        spans = grounded.tag_spans("項目Aの種類", form_field_kinds.with_field_kind_evidence(observed, [original]))
        evidence = spans[-1]
        self.assertEqual(evidence["text"], "項目A = 選択")
        self.assertEqual(evidence["origin"], "image_extraction")
        self.assertEqual(evidence["page"], 3)
        self.assertIn("image_generated", evidence["tags"])
        self.assertFalse(evidence["pinned"])
        self.assertEqual(original["text"], "別の図の説明")
        draft = GroundedDraft(
            summary="",
            confidence="medium",
            items=[
                GroundedItem(
                    kind="rule",
                    text="項目Aは選択項目です。",
                    citations=[GroundedCitation(quote=evidence["text"], evidence_id=evidence["label"])],
                )
            ],
        )
        checked, dropped = grounded.verify("項目Aの種類", draft, spans)
        self.assertFalse(dropped)
        self.assertTrue(checked[0].confirmed)

    def test_same_parent_fields_point_to_their_own_evidence(self):
        from rag_engine.generation import grounded

        observed = [
            form_field_kinds.FieldKindObservation("parent", "項目A", "select"),
            form_field_kinds.FieldKindObservation("parent", "項目B", "button"),
        ]
        spans = grounded.tag_spans("項目Aと項目B", form_field_kinds.with_field_kind_evidence(observed, [self._span()]))
        note = form_field_kinds.prompt_note(observed, [], spans)
        self.assertIn("- E2: 項目A = 選択", note)
        self.assertIn("- E3: 項目B = ボタン", note)
        self.assertNotIn("- E1:", note)

    def test_missing_or_ambiguous_page_does_not_fabricate_a_citation(self):
        observed = [form_field_kinds.FieldKindObservation("parent", "項目A", "select")]
        for spans in [[], [self._span(page=None)], [self._span(3), self._span(4)]]:
            self.assertEqual(form_field_kinds.with_field_kind_evidence(observed, spans), spans)

    def test_conflicting_kinds_remain_distinct_and_are_not_resolved(self):
        observed = [form_field_kinds.FieldKindObservation("parent", "項目A", kind) for kind in ["select", "text", "select"]]
        result = form_field_kinds.with_field_kind_evidence(observed, [self._span()])
        self.assertEqual(len(result), 3)
        self.assertNotEqual(result[1]["evidence_id"], result[2]["evidence_id"])
        condition = ("type", "項目Aの種類", (("選択", ()), ("自由入力", ())))
        self.assertEqual(form_field_kinds.resolve_conditions(observed, [condition]), [])

    def test_generation_material_keeps_structured_kind_when_body_has_no_kind(self):
        from dataclasses import replace

        from test_grounded_answer import context, record

        from rag_engine.generation.answering import _grounded_spans

        source = record(0, "画面の説明だけ", "設定画面", page=3)
        source = replace(source, metadata=_chunk("parent", [{"name": "項目A", "kind": "select"}]).metadata)
        spans = _grounded_spans("項目Aの種類を確認する", context(source), [], None)
        evidence = next(span for span in spans if span.get("field_kind"))
        self.assertEqual(evidence["text"], "項目A = 選択")
        self.assertEqual(evidence["page"], 3)
        self.assertEqual(evidence["source"], source.source)

    def test_alias_source_note_still_points_to_the_structured_field(self):
        from rag_engine.generation import grounded

        original = {**self._span(), "source_aliases": ["alias"]}
        observed = [form_field_kinds.FieldKindObservation("alias", "項目A", "select")]
        spans = grounded.tag_spans("項目A", form_field_kinds.with_field_kind_evidence(observed, [original]))
        self.assertIn("- E2: 項目A = 選択", form_field_kinds.prompt_note(observed, [], spans))

    def test_image_page_selects_its_own_page_in_a_multi_page_parent(self):
        source = _chunk("parent", [{"name": "項目A", "kind": "select"}])
        source.page = 3
        source.page_end = 4
        source.metadata["image_evidence"][0]["page"] = 4
        observed = form_field_kinds.question_field_kinds("項目Aの種類", _context(source))
        self.assertEqual(observed[0].page, 4)
        result = form_field_kinds.with_field_kind_evidence(observed, [self._span(3), self._span(4)])
        self.assertEqual(result[-1]["page"], 4)
        self.assertEqual(result[-1]["text"], "項目A = 選択")
        self.assertEqual(form_field_kinds.trace_payload(observed, [])["observations"][0]["page"], 4)

    def test_multi_page_record_does_not_infer_an_image_page_from_parent_start(self):
        source = _chunk("parent", [{"name": "項目A", "kind": "select"}])
        source.page = 3
        source.page_end = 4
        observed = form_field_kinds.question_field_kinds("項目Aの種類", _context(source))
        self.assertIsNone(observed[0].page)
        spans = [self._span(3), self._span(4)]
        self.assertEqual(form_field_kinds.with_field_kind_evidence(observed, spans), spans)
