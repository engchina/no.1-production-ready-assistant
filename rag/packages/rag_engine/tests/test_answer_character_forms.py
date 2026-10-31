"""回答のモデルに渡す根拠の文字の形（全角・半角）をそろえ、引用の照合は原文で通ることを確かめる (#1350)。

モデルはスタブ。資料の全角の実体名（ＨＲＭ）と質問の半角（HRM）を、モデルが同じ語として読める形で渡し、
表示・保存する根拠と引用の原文は資料の表記のままにする。
"""

import json
from unittest.mock import patch

from test_grounded_answer import FakeModel, audit, context, draft, ids, item, record, run

from rag_engine.evaluation.answer_validation import ClaimAuditOutput, validate_answer_claims
from rag_engine.generation import grounded
from rag_engine.generation.answering import _grounded_spans
from rag_engine.models.llm import GroundedDraft
from rag_engine.retrieval.character_forms import fold_model_view, fold_text, fold_text_with_positions, fold_width
from rag_engine.retrieval.text_search_tokenizer import normalize_text_search_index_text

PLAN = "定期保守計画\nＨＲＭ：第２水曜 22:00〜24:00\nＯＭＳ：第３木曜 21:00〜23:00\n上記以外のシステムは最終金曜に実施します。"
QUESTION = "HRM の人事評価の変更はいつの保守枠で実施しますか"
# モデルが読む形（全角・半角だけをそろえた形）で書き写した引用と説明。
QUOTE = "HRM:第2水曜 22:00〜24:00"
TEXT = "HRM の変更は第2水曜 22:00〜24:00 の保守枠で実施します。"


def _plan_context():
    return context(record(0, PLAN, "定期保守計画", page=3, source="保守計画.pdf"))


def test_fold_width_only_folds_full_and_half_width_forms():
    assert fold_width("ＨＲＭ：ＳＹＳ－１０４") == "HRM:SYS-104"
    assert fold_width("ｼｽﾃﾑ管理ｶﾞｲﾄﾞ　ﾊﾟｽ") == "システム管理ガイド パス"
    # 丸数字（手順の区切り）・ローマ数字・波ダッシュ・改行・大文字小文字は残す。
    assert fold_width("①申請\nⅤ章 10〜20 Abc") == "①申請\nⅤ章 10〜20 Abc"
    # JSON に入れる値は値の側でそろえる（全角の引用符を JSON の記号にしない）。
    view = fold_model_view({"機能（１）": ["＂ＨＲＭ＂", 3], "n": None})
    assert view == {"機能(1)": ['"HRM"', 3], "n": None}
    assert json.loads(json.dumps(view, ensure_ascii=False)) == view


def test_fold_text_matches_the_text_search_index_rule_with_positions():
    for raw in (PLAN, "ｶﾞｲﾄﾞ ㈱ ① 10〜20—30", "a\x00\x01b\tc", "が", ""):
        folded, positions = fold_text_with_positions(raw)
        assert folded == fold_text(raw) == normalize_text_search_index_text(raw)
        assert len(positions) == len(folded)
        assert all(0 <= p < len(raw) for p in positions)
    folded, positions = fold_text_with_positions("ｶﾞｲﾄﾞ")
    assert folded == "ガイド" and positions == [0, 2, 3]


def test_folded_quote_resolves_to_the_original_text():
    assert grounded.quote_in_text(QUOTE, PLAN)
    # 波ダッシュ・ダッシュの違いも索引と同じ規則で同一視する。
    assert grounded.quote_in_text("HRM:第2水曜 22:00~24:00", PLAN)
    assert grounded.quote_in_text("システム管理ガイドを開きます", "ｼｽﾃﾑ管理ｶﾞｲﾄﾞを開きます。")
    # 表示する引用は原文の範囲（全角のまま）。
    assert grounded._original_quote(QUOTE, PLAN) == "ＨＲＭ：第２水曜 22:00〜24:00"
    assert grounded._original_quote("システム管理ガイドを開きます", "ｼｽﾃﾑ管理ｶﾞｲﾄﾞを開きます。") == "ｼｽﾃﾑ管理ｶﾞｲﾄﾞを開きます。"
    # 文字や数字が違う引用は通さない。
    assert not grounded.quote_in_text("HRM:第3水曜 22:00〜24:00", PLAN)


def test_answer_context_folds_widths_and_keeps_the_original_evidence():
    ctx = _plan_context()
    spans = _grounded_spans(QUESTION, ctx, (), None)
    block = grounded.build_context_block(QUESTION, spans, preface="", feedback=None)
    assert "HRM:第2水曜 22:00〜24:00" in block and "OMS:第3木曜" in block
    assert "原文の文字表記" in block and "ＨＲＭ" in block and "ＯＭＳ" in block
    # 根拠（表示・保存・引用の原文）は資料の表記のまま。
    assert any("ＨＲＭ：第２水曜" in span["text"] for span in spans)
    assert ctx.records[0].text == PLAN


def test_verify_binds_a_folded_quote_and_checks_in_the_same_form():
    spans = _grounded_spans(QUESTION, _plan_context(), (), None)
    eid = spans[0]["evidence_id"]
    checked, dropped = grounded.verify(QUESTION, GroundedDraft.model_validate(draft(item(eid, TEXT, QUOTE, kind="rule"))), spans)
    assert dropped == []
    entry = next(e for e in checked if e.item.text == TEXT)
    assert entry.span is not None and entry.span["evidence_id"] == eid
    assert entry.item.quote == "ＨＲＭ：第２水曜 22:00〜24:00"
    assert not entry.quote_only and entry.reasons == []


def test_check_view_folds_span_text_fields_for_checks():
    span = {
        "text": "ＯＫボタンを押します。",
        "function_texts": ["ＨＲＭ"],
        "section_path": ["（１）設定"],
        "function": ("doc", "（１）設定"),
        "evidence_id": "e1",
    }
    view = grounded.check_view(span)
    assert view["text"] == "OKボタンを押します。" and view["function_texts"] == ["HRM"]
    assert view["section_path"] == ["(1)設定"]
    # 機能のキーと ID は結び付けに使うので変えず、元の span も変えない。
    assert view["function"] == span["function"] and view["evidence_id"] == "e1"
    assert span["text"] == "ＯＫボタンを押します。"
    # 説明の「OKボタン」は根拠の「ＯＫボタン」と同じボタン名として扱う。
    checked_item = grounded._check_item(
        grounded.GroundedItem(
            kind="operation",
            text="OKボタンを押します。",
            citations=[grounded.GroundedCitation(evidence_id="e1", quote="ＯＫボタンを押します。")],
        )
    )
    assert grounded._check_button_names("", checked_item, view) == ""


def test_grounded_answer_reads_half_width_and_cites_the_original():
    ctx = _plan_context()
    eid = ids(ctx, QUESTION)[PLAN]
    model = FakeModel(
        [draft(item(eid, TEXT, QUOTE, kind="rule"), summary="HRM は第2水曜の保守枠です。")], [audit((0, "supported", "matched", ""))]
    )
    result = run(model, ctx, QUESTION).response
    generation = next(prompt for name, prompt in model.prompts if name == "GroundedDraftWire")
    assert "HRM:第2水曜 22:00〜24:00" in generation and "ＨＲＭ" in generation
    audit_prompt = json.loads(next(prompt for name, prompt in model.prompts if name == "GroundedAudit"))
    assert audit_prompt["items"][0]["quote"] == "HRM:第2水曜 22:00〜24:00"
    assert "ＨＲＭ" not in json.dumps(audit_prompt, ensure_ascii=False)
    # 公開する説明は引用の照合を通り、出典の引用と保存する根拠は原文のまま。
    assert TEXT in result.answer_text and grounded.answer_labels().quote_only_section not in result.answer_text
    assert [fact["quote"] for fact in result.evidence_facts] == ["ＨＲＭ：第２水曜 22:00〜24:00"]
    assert any("ＨＲＭ：第２水曜" in span["text"] for span in result.generation_trace["selected_evidence"])


def test_answer_validation_reads_folded_evidence_and_keeps_the_original_quote():
    evidence = [{"id": "c1", "source": "保守計画.pdf", "page_start": 3, "page_end": 3, "text": PLAN}]
    answer = "HRM の変更は第2水曜 22:00〜24:00 の保守枠で実施します。\n根拠：保守計画.pdf p.3"
    seen = {}

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        seen["payload"] = payload
        passage = payload["answer_passages"][0]["id"]
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": "段落",
                        "answer_passage_id": passage,
                        "status": "supported",
                        "evidence_id": [i for i in payload["evidence_items"] if i.get("id") != "question:1"][0]["evidence_id"],
                        "source_id": "",
                        "evidence_quote": "",
                        "reason": "根拠に記載",
                    }
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims(QUESTION, answer, evidence, settings=None)
    assert "HRM:第2水曜" in [i for i in seen["payload"]["evidence_items"] if i.get("id") != "question:1"][0]["text"]
    assert "ＨＲＭ" not in json.dumps(seen["payload"], ensure_ascii=False)
    assert seen["payload"]["answer_text"] == answer
    assert result["status"] == "completed" and result["counts"] == {"supported": 1}
    supported = result["claim_checks"][0]
    assert supported["source_id"] == "c1" and "ＨＲＭ：第２水曜" in supported["evidence_quote"]


def test_figure_dash_is_folded_only_for_comparison() -> None:
    from rag_engine.retrieval.character_forms import fold_text

    assert fold_text("row‒a") == "row-a"
    assert fold_width("row‒a") == "row‒a"


def test_original_forms_are_supplementary_and_bound_to_units():
    raw = "文字表記\n全角ＡＢＣ、半角ABC、ｶﾞ、全角＂と＼。\n変更のない本文。"
    spans = _grounded_spans("原文の文字は？", context(record(0, raw, "文字表記")), (), None)
    block = grounded.evidence_block_text(spans)
    assert "全角ABC、半角ABC" in block
    assert "全角ＡＢＣ、半角ABC" in block
    start = block.index("原文の文字表記: ") + len("原文の文字表記: ")
    annotations = json.JSONDecoder().raw_decode(block[start:])[0]
    assert any("ＡＢＣ" in row["原文"] and row["引用単位"] == 2 for row in annotations)
    assert not any("変更のない本文" in row["原文"] for row in annotations)
    view = grounded._citation_view(spans[0])
    assert view.model_rendered in block
    assert view.text == spans[0]["text"]
    assert grounded._model_source_note(spans[0], view.units) in grounded.evidence_metadata_text(spans[0])


def test_no_extra_metadata_without_character_changes():
    spans = _grounded_spans("内容は？", context(record(0, "本文Aを確認する。", "本文A")), (), None)
    block = grounded.evidence_block_text(spans)
    assert "原文の文字表記" not in block
    assert block == grounded.neutralize_boundary_markers(fold_width(grounded.format_functions(spans)))


def test_answer_can_distinguish_original_full_and_half_width_characters():
    raw = "文字表記\n全角ＡＢＣ、半角ABC。"
    question = "全角と半角の英字は、原文にどのように記載されていますか。"
    ctx = context(record(0, raw, "文字表記"))
    eid = ids(ctx, question)[raw]
    answer = "全角は「ＡＢＣ」、半角は「ABC」と記載されています。"

    def generate(prompt):
        assert "全角ＡＢＣ、半角ABC。" in prompt
        assert "全角ABC、半角ABC。" in prompt
        return draft(item(eid, answer, "全角ＡＢＣ、半角ABC。", kind="rule"), summary=answer)

    model = FakeModel([generate], [audit((0, "supported", "matched", ""))])
    result = run(model, ctx, question).response
    assert answer in result.answer_text
    assert result.evidence_facts[0]["quote"] == "全角ＡＢＣ、半角ABC。"
