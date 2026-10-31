"""本文の出典行と利用印を、主張を支える子の chunk に結ぶ（#2710）。

同じ親（まとまり）・同じ頁に、見出し・注意の子 H と図の説明の子 F がある合成の文書で確かめる。
"""

from audit_fixtures import legacy_audit_result

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import grounded
from rag_engine.generation.answer_payload import answer_evidence_items
from rag_engine.generation.answering import AnswerContext, AnswerRecord, _grounded_spans, synthesize_grounded_answer
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.models.audit_wire import is_audit_wire
from rag_engine.models.llm import GroundedCitation, GroundedDraft, GroundedItem
from rag_engine.retrieval.context_builder import ContextChildEvidence, ContextParentEvidence
from rag_engine.retrieval.evidence_selection import evidence_spans

HEADING = "合成の見出し A\n注意: 合成の注意書きです。"
FIGURE = "図の説明: 合成の装置の左の枠に値 B を入れると、右の枠に結果 C が出ます。"
QUESTION = "合成の装置で結果 C を出すには？"


def _record(uid: str, text: str, level: str, **kwargs) -> AnswerRecord:
    return AnswerRecord(
        id=uid,
        chunk_uid=uid,
        chunk_id=uid,
        source="文書 A.pdf",
        source_run_id="run",
        engine="docling",
        engine_label="Docling",
        page=3,
        seq_no=1,
        category="Chunk",
        chunk_level=level,
        text=text,
        **kwargs,
    )


PARENT = _record("doc:p-a", HEADING + "\n" + FIGURE, "parent", child_chunk_ids=("doc:c-h", "doc:c-f"))
CHILD_H = _record("doc:c-h", HEADING, "child", parent_chunk_uid="doc:p-a")
# 子の検索文は画面の例の値などを除くので、親の本文と一致しない。元の本文（body_text）で親の中の位置を探す。
CHILD_F = _record(
    "doc:c-f",
    "Source file: 文書 A.pdf\nChild text:\n図の説明: 合成の装置の左の枠に入れると、右の枠に出ます。",
    "child",
    parent_chunk_uid="doc:p-a",
    body_text=FIGURE,
)


def _context() -> AnswerContext:
    tree = (
        ContextParentEvidence(
            record=PARENT,
            role="synthesis_parent",
            reason="r",
            children=(
                # 引用一覧の順は H が先（H を先頭の子として選ばないことを確かめる）。
                ContextChildEvidence(record=CHILD_H, role="retrieved_anchor", reason="r", retrieval_rank=1),
                ContextChildEvidence(record=CHILD_F, role="retrieved_anchor", reason="r", retrieval_rank=2),
            ),
        ),
    )
    return AnswerContext(records=[PARENT], text=PARENT.text, evidence_tree=tree)


class _Model:
    def __init__(self, draft: dict, audit: dict):
        self.draft, self.audit = draft, audit

    def __call__(self, system, prompt, settings, schema, **options):
        if schema in (GroundedDraft, generation_draft_wire_model()):
            # 既存quote fixtureを履歴契約で再生する。
            return GroundedDraft.model_validate(self.draft)
        if is_audit_wire(schema):
            return legacy_audit_result(self.audit, schema)
        return schema.model_validate(self.audit)


def _run(items: list[dict]):
    reviews = [{"index": i, "support": "supported", "applicability": "matched", "condition": "", "reason": "r"} for i in range(len(items))]
    model = _Model(
        {"summary": "結果 C は右の枠に出ます。", "items": items, "confidence": "high"},
        {"goal_alignment": "aligned", "summary_supported": True, "reviews": reviews, "request_reviews": [], "unused_evidence_ids": []},
    )

    def unused(*a, **k):
        raise AssertionError("unexpected I/O")

    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        return synthesize_grounded_answer(QUESTION, _context(), get_settings(), image_prompt_mode="text_only")


def _evidence_id() -> str:
    spans = _grounded_spans(QUESTION, _context(), (), None)
    return next(span["evidence_id"] for span in spans if "結果 C" in span["text"])


def _rule(quote: str) -> dict:
    return {"kind": "rule", "text": quote, "citations": [{"evidence_id": _evidence_id(), "quote": quote}]}


def test_spans_keep_the_children_and_their_ranges_in_the_parent() -> None:
    spans = evidence_spans(QUESTION, [PARENT], scope_records=[CHILD_H, CHILD_F])
    ranges = [item for span in spans for item in span.get("child_ranges", [])]
    assert {item["id"] for item in ranges} == {"doc:c-h", "doc:c-f"}
    for span in spans:
        for item in span.get("child_ranges", []):
            # 範囲は親の本文の位置で、抜粋の範囲の中に収まる。
            assert span["start"] <= item["start"] < item["end"] <= span["end"]


def test_citation_line_and_used_mark_point_to_the_child_that_holds_the_quote() -> None:
    """同じ親・同じ頁の H と F が並んでも、出典行と「回答で引用」は引用のある F に付く（並びの先の H にしない）。"""
    result = _run([_rule("左の枠に値 B を入れると、右の枠に結果 C が出ます。")]).response
    refs = result.generation_trace["citation_lines"]
    assert len(refs) == 1
    assert refs[0]["scope_source_ids"] == ["doc:c-f"]
    assert refs[0]["candidate_source_ids"] == []
    assert [entry["evidence_id"] for entry in result.cited_evidence] == ["doc:c-f"]


def test_used_mark_does_not_spread_to_siblings() -> None:
    response = _run([_rule("左の枠に値 B を入れると、右の枠に結果 C が出ます。")]).response
    used = [entry["evidence_id"] for entry in response.cited_evidence]
    items = answer_evidence_items([PARENT], used, evidence_tree=_context().evidence_tree)
    children = {child["chunk_id"]: child for parent in items for child in parent["children"]}
    assert children["doc:c-f"]["is_model_used"] is True
    assert children["doc:c-h"]["is_model_used"] is False
    assert children["doc:c-h"]["parent_model_used"] is False


def _entry(quote: str, span: dict) -> grounded.CheckedItem:
    return grounded.CheckedItem(
        GroundedItem(kind="rule", text=quote, citations=[GroundedCitation(evidence_id=span["evidence_id"], quote=quote)]), span=span
    )


def _parent_span() -> dict:
    text = PARENT.text
    split = len(HEADING) + 1
    return {
        "evidence_id": "E-a",
        "source_id": "doc:p-a",
        "source": "文書 A.pdf",
        "page": 3,
        "page_end": 3,
        "start": 0,
        "end": len(text),
        "text": text,
        "child_ranges": [
            {"id": "doc:c-h", "start": 0, "end": len(HEADING)},
            {"id": "doc:c-f", "start": split, "end": len(text)},
        ],
    }


def test_each_item_citation_line_keeps_only_its_supporting_child() -> None:
    """同じ出典・頁でも項目ごとの出典行へ分け、その説明を支える子だけを残す。"""
    span = _parent_span()
    entries = [_entry("合成の注意書きです。", span), _entry("右の枠に結果 C が出ます。", dict(span, evidence_id="E-b"))]
    body, refs = grounded.render_with_citations("", entries)
    assert body.count(grounded.answer_labels().citation_prefix) == 2
    assert [ref["scope_source_ids"] for ref in refs] == [["doc:c-h"], ["doc:c-f"]]
    assert [ref["evidence_ids"] for ref in refs] == [["E-a"], ["E-b"]]


def test_unlocated_quote_leaves_candidates_instead_of_the_first_child() -> None:
    """引用の位置が決まらず、根拠が 2 つの子にまたがれば、どちらも確定にせず候補に残す。"""
    span = _parent_span()
    exact, candidates = grounded.citation_children(span, "根拠に無い文")
    assert (exact, candidates) == ([], ["doc:c-h", "doc:c-f"])
    _body, refs = grounded.render_with_citations("", [_entry("根拠に無い文", span)])
    assert refs[0]["scope_source_ids"] == []
    assert refs[0]["candidate_source_ids"] == ["doc:c-h", "doc:c-f"]


def test_quote_in_one_child_with_normalized_spacing_is_still_located() -> None:
    span = _parent_span()
    assert grounded.citation_children(span, "右の枠に 結果C が出ます。") == (["doc:c-f"], [])


def test_parent_only_reference_marks_children_as_parent_context_not_as_cited() -> None:
    """親の ID だけの参照は、兄弟の子を「回答で引用」にせず、親を参照した印にする。"""
    tree = _context().evidence_tree
    items = answer_evidence_items([PARENT], ["doc:p-a"], evidence_tree=tree)
    children = {child["chunk_id"]: child for child in items[0]["children"]}
    assert items[0]["is_model_used"] is True
    assert [children[key]["is_model_used"] for key in ("doc:c-h", "doc:c-f")] == [False, False]
    assert [children[key]["parent_model_used"] for key in ("doc:c-h", "doc:c-f")] == [True, True]

    items = answer_evidence_items([PARENT], ["doc:c-f"], evidence_tree=tree)
    children = {child["chunk_id"]: child for child in items[0]["children"]}
    assert (children["doc:c-f"]["is_model_used"], children["doc:c-h"]["is_model_used"]) == (True, False)
    assert children["doc:c-h"]["parent_model_used"] is False
    # 子を引用すれば、その親のまとまりも回答に使った。
    assert items[0]["is_model_used"] is True
