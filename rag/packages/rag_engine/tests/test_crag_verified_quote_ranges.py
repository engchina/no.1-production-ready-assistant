"""CRAG の評価が写しを確かめた原文を、生成の根拠の準備で必須の範囲として保つことを確かめる（#3840）。

合成の親の本文で、評価の抄録と生成の抜粋が同じ親の別の窓を選ぶ場合を作る。モデル・OCI は呼ばない。
"""

from test_crag_context_recovery import record

from rag_engine.generation import answering as a
from rag_engine.generation import crag_generation
from rag_engine.retrieval.character_forms import find_width_folded
from rag_engine.retrieval.evidence_selection import evidence_spans

QUESTION = "設定を保存する手順は？"
# 質問の語を含む窓（生成の抜粋が関連度で選ぶ窓）と、質問の語を含まない写し（評価が確かめた原文）を離して置く。
RELEVANT = "設定を保存する手順は、保存ボタンを押します。\n"
QUOTE = "変更内容は翌営業日に反映されます。"
FILLER = "".join(f"項目{index:03d}の欄は参照用の表示です。\n" for index in range(60))
PARENT_TEXT = RELEVANT + FILLER + QUOTE + "\n" + FILLER
BUDGET = 400


def parent(text=PARENT_TEXT):
    return record("p1", 1, text, "parent", "")


def quote_entries(diagnostics):
    return [entry for entry in diagnostics if entry.get("stage") == "verified_quote"]


def holds(spans, quote, source="run:p1"):
    return any(span["source_id"] == source and quote in span["text"] for span in spans)


def test_verified_quote_outside_the_relevant_window_is_kept_within_the_budget():
    before = evidence_spans(QUESTION, [parent()], budget=BUDGET)
    assert not holds(before, QUOTE)

    diagnostics: list[dict] = []
    after = evidence_spans(QUESTION, [parent()], budget=BUDGET, verified_quotes={"run:p1": [QUOTE]}, diagnostics=diagnostics)
    assert holds(after, QUOTE)
    # 予算の総額は広げない。
    assert sum(len(span["text"]) for span in after) <= BUDGET
    start = PARENT_TEXT.index(QUOTE)
    assert quote_entries(diagnostics) == [
        {
            "stage": "verified_quote",
            "source_id": "run:p1",
            "quote_chars": len(QUOTE),
            "parent_id": "run:p1",
            "start": start,
            "end": start + len(QUOTE),
            "status": "included",
        }
    ]


def test_quote_is_located_with_the_same_width_and_space_folding_as_the_grader():
    # 評価の照合（_crag_quote_matches）と同じく、全角・半角と空白だけを同一視する。
    text = "前置き。\n変更内容は　翌営業日に反映されます。\n後書き。"
    start, end = find_width_folded(text, "変更内容は翌営業日に反映されます。")
    assert text[start:end] == "変更内容は　翌営業日に反映されます。"
    assert find_width_folded("ＡＢＣ－１２３", "ABC－123") == (0, 7)
    # 意味の近さ・部分の一致には広げない（1 文字違う写しは見つけない）。
    assert find_width_folded(text, "変更内容は翌日に反映されます。") is None
    assert find_width_folded(text, "   ") is None


def test_quotes_that_cannot_be_kept_are_recorded_with_the_reason():
    diagnostics: list[dict] = []
    spans = evidence_spans(
        QUESTION,
        [parent()],
        budget=BUDGET,
        verified_quotes={
            "run:p1": ["変更内容は翌日に反映されます。"],
            "run:other": [QUOTE],
        },
        diagnostics=diagnostics,
    )
    assert not holds(spans, "変更内容は翌日に反映されます。")
    assert [(entry["source_id"], entry["status"]) for entry in quote_entries(diagnostics)] == [
        ("run:p1", "quote_not_in_source_text"),
        ("run:other", "source_not_in_generation_records"),
    ]


def test_quote_that_does_not_fit_the_remaining_budget_is_reported_as_excluded():
    diagnostics: list[dict] = []
    spans = evidence_spans(QUESTION, [parent()], budget=12, verified_quotes={"run:p1": [QUOTE]}, diagnostics=diagnostics)
    assert not holds(spans, QUOTE)
    assert [entry["status"] for entry in quote_entries(diagnostics)] == ["excluded_by_budget"]


def test_span_boundary_is_not_placed_inside_a_verified_quote():
    first, second = "保存の前に入力を確認します。", "保存ボタンを押します。"
    text = first + second
    children = [record("c1", 1, first), record("c2", 2, second)]
    quote = "確認します。保存ボタン"

    before = evidence_spans(QUESTION, [parent(text)], scope_records=children)
    # 子の境界（文末の後）で 2 つの span に分かれ、写しはどちらの span にも入らない。
    assert [span["text"] for span in before] == [first, second]
    assert not holds(before, quote)

    diagnostics: list[dict] = []
    after = evidence_spans(QUESTION, [parent(text)], scope_records=children, verified_quotes={"run:p1": [quote]}, diagnostics=diagnostics)
    assert holds(after, quote)
    assert {"stage": "boundary_preserved", "reason_code": "verified_quote_range", "source_id": "run:p1", "positions": [len(first)]} in (
        diagnostics
    )
    assert [entry["status"] for entry in quote_entries(diagnostics)] == ["included"]


def test_verified_quotes_take_only_quotes_the_grader_verified():
    assessment = {
        "answerable_parts": [
            {"status": "supported", "evidence": [{"source_id": "run:p1", "quote": QUOTE}]},
            {"status": "unknown", "evidence": [{"source_id": "run:p1", "quote": "評価が確かめていない写し"}]},
        ],
        "aspect_checks": [
            {"aspect": "procedure", "evidence_validation": "matched", "evidence": [{"source_id": "run:p1", "quote": QUOTE}]},
            {
                "aspect": "requested_result",
                "evidence_validation": "invalid_source_or_quote",
                "evidence": [{"source_id": "run:p2", "quote": RELEVANT}],
            },
            {"aspect": "applicability", "evidence_validation": "unverified", "evidence": [{"source_id": "run:p3", "quote": "未確認"}]},
        ],
    }
    assert crag_generation.verified_quotes(assessment) == {"run:p1": [QUOTE], "run:p2": [RELEVANT]}
    assert crag_generation.verified_quotes(None) == {}


def test_generation_gate_binds_the_grader_verified_part_after_preparation():
    context = a.AnswerContext(records=[parent()], text=PARENT_TEXT)
    assessment = {
        "grade_execution": "completed",
        "aspect_checks": [
            {
                "aspect": "requested_result",
                "status": "supported",
                "evidence_validation": "matched",
                "evidence": [{"source_id": "run:p1", "quote": QUOTE}],
            }
        ],
        "answerable_parts": [{"aspect": "requested_result", "status": "supported", "evidence": [{"source_id": "run:p1", "quote": QUOTE}]}],
    }
    before = a._grounded_spans(QUESTION, context, (), None, budget=BUDGET)
    before_view = crag_generation.generation_view(assessment, before, context)
    assert before_view["generation_decision"] == crag_generation.GATE_UNVERIFIED_PART
    assert before_view["unverified_binding_reasons"] == {"quote_not_in_generation_source": 1}

    after = a._grounded_spans(QUESTION, context, (), None, budget=BUDGET, crag_assessment=assessment)
    view = crag_generation.generation_view(assessment, after, context)
    assert view["generation_decision"] == crag_generation.GATE_VERIFIED_PART
    assert [part["status"] for part in view["answerable_parts"]] == ["supported"]
    assert view["aspect_checks"][0]["status"] == "supported"
    assert sum(len(span["text"]) for span in after) <= BUDGET


def test_verified_quote_is_kept_when_the_parent_is_cut_to_guide_step_children():
    # 業務ガイドの手順の根拠（priority_source_groups）では、親から子の本文だけを切り出す。
    child = record("c1", 1, RELEVANT.strip())
    options = {"anchors": [child], "scope_records": [child], "priority_source_groups": [["run:c1"]], "budget": BUDGET}
    before = evidence_spans(QUESTION, [parent()], **options)
    assert holds(before, RELEVANT.strip()) and not holds(before, QUOTE)

    diagnostics: list[dict] = []
    after = evidence_spans(QUESTION, [parent()], verified_quotes={"run:p1": [QUOTE]}, diagnostics=diagnostics, **options)
    assert holds(after, RELEVANT.strip()) and holds(after, QUOTE)
    assert [entry["status"] for entry in quote_entries(diagnostics)] == ["included"]
    assert sum(len(span["text"]) for span in after) <= BUDGET
