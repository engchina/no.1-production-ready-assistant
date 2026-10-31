"""回答の確定に必要な実データ・別の資料の確認を案内する (#688)。"""

from test_grounded_answer import FakeModel, audit, context, draft, record, run

from rag_engine.generation import grounded
from rag_engine.models.llm import GroundedDraft


def _draft(**extra):
    return {**draft({"kind": "gap", "text": "今回の支払区分は資料からは確認できません。"}, summary="", confidence="low"), **extra}


def test_confirmation_lines_keep_data_and_documents_named_in_the_evidence_or_question():
    spans = [{"text": "支払区分は「支払区分コード表」を参照してください。"}]
    value = GroundedDraft.model_validate(
        _draft(
            external_data_items=["対象の取引先の支払区分の設定値（どの手順に当たるかを決める）", " "],
            reference_materials=[
                "「支払区分コード表」（区分の意味を確かめる）",
                "料金表（手数料を確かめる）",
                "「架空の運用規程」（創作された資料名）",
            ],
        )
    )
    assert grounded.confirmation_lines(value, "支払区分の意味は？", spans) == [
        "実データ: 対象の取引先の支払区分の設定値（どの手順に当たるかを決める）",
        "資料: 「支払区分コード表」（区分の意味を確かめる）",
        "資料: 料金表（手数料を確かめる）",
    ]


def test_render_adds_the_confirmation_section_last():
    text = grounded.render("結論です。", [], confirmations=["実データ: 設定値（区分を決める）"])
    assert text.endswith("回答を確定するために確認すること\n\n・実データ: 設定値（区分を決める）")


def test_no_evidence_with_confirmations_guides_instead_of_refusing():
    ctx = context(record(0, "帳票の一覧です。", "その他"))
    guided = _draft(external_data_items=["対象伝票の状態（取消できるかを決める）"])
    result = run(FakeModel([guided], [audit()]), ctx).response
    assert result.answer_text.startswith(grounded.answer_labels().confirmation_only_summary)
    assert "・実データ: 対象伝票の状態（取消できるかを決める）" in result.answer_text
    assert "回答できません" not in result.answer_text
    # 確認することも無ければ、今までどおり正直に拒答する（作り話をしない）。
    refused = run(FakeModel([_draft()], [audit()]), ctx).response
    assert refused.answer_text.startswith("検索された資料に回答を裏付ける十分な根拠がない")
    assert grounded.answer_labels().confirmations_section not in refused.answer_text


def test_reference_materials_need_human_review():
    ctx = context(record(0, "帳票の一覧です。", "その他"))
    result = run(FakeModel([_draft(reference_materials=["料金表（手数料を確かめる）"])], [audit()]), ctx).response
    assert result.needs_human_review is True
    assert "・資料: 料金表（手数料を確かめる）" in result.answer_text
