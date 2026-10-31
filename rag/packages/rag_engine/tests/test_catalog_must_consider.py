"""条の目録で選んだ条を、回答の生成に印を付けて渡し、設定で有効なら引かなかった条を検討する（#3526）。

目録の段が選んだ条の child に重なる根拠に印（catalog）を付けて生成に渡す。設定（``article_catalog_review``。既定は
無効）で有効なら、草稿の説明が引かず、計画も unrelated としなかった条を、条ごとに原文が支える説明があるかを別に
検討させ、支える説明の出典に足す。足した出典も原文照合と条の出典の監査に掛け、通らなければ外す（引用は強いない）。
"""

from __future__ import annotations

import json
from dataclasses import replace

from audit_fixtures import legacy_audit_result

from rag_engine.config import get_settings
from rag_engine.dependencies import AnswerDependencies, bind_dependencies
from rag_engine.generation import grounded
from rag_engine.generation.answering import AnswerContext, AnswerRecord, _grounded_spans, synthesize_grounded_answer
from rag_engine.models.audit_wire import is_audit_wire
from rag_engine.models.llm import GroundedDraft
from rag_engine.retrieval.article_catalog import ARTICLE_CATALOG_KEY, ARTICLE_UNIT_KEY
from rag_engine.retrieval.context_builder import ContextChildEvidence, ContextParentEvidence
from rag_engine.retrieval.evidence_selection import evidence_spans

QUESTION = "申請書の提出期限を教えてください"
ARTICLE_1 = "第一条\n申請書は、事由の発生した日から三十日以内に提出しなければならない。ただし、第三号に該当する者を除く。"
ARTICLE_2 = "第二条\n申請書には、事由を証する書類を添えなければならない。"
GUIDE = "提出の手順\n申請書は、事由の発生した日から三十日以内に窓口へ提出します。"
CLAIM = "申請書は、事由の発生した日から三十日以内に提出します。"


def _record(uid: str, text: str, level: str, source: str, **kwargs) -> AnswerRecord:
    return AnswerRecord(
        id=uid,
        chunk_uid=uid,
        chunk_id=uid,
        source=source,
        source_run_id="run",
        engine="docling",
        engine_label="Docling",
        page=1,
        seq_no=1,
        category="Chunk",
        chunk_level=level,
        text=text,
        **kwargs,
    )


RULES = _record(
    "rule:p", ARTICLE_1 + "\n" + ARTICLE_2, "parent", "規則A.xml", child_chunk_ids=("rule:c1", "rule:c2"), metadata={ARTICLE_UNIT_KEY: True}
)
# 目録の段は選んだ条の章節の最初の child に印を付けて返す（`_catalog_article_children`）。
CHOSEN = _record(
    "rule:c1",
    ARTICLE_1,
    "child",
    "規則A.xml",
    parent_chunk_uid="rule:p",
    metadata={"section_path": ["総則", "第一条"], ARTICLE_CATALOG_KEY: {"title": "第一条", "order": 0}},
)
OTHER = _record("rule:c2", ARTICLE_2, "child", "規則A.xml", parent_chunk_uid="rule:p", metadata={"section_path": ["総則", "第二条"]})
MANUAL = _record("guide:p", GUIDE, "parent", "手順書B.pdf", metadata={"section_path": ["提出の手順"]})


def _context() -> AnswerContext:
    tree = (
        ContextParentEvidence(
            record=RULES,
            role="synthesis_parent",
            reason="catalog_article",
            children=(
                ContextChildEvidence(record=CHOSEN, role="retrieved_anchor", reason="catalog_article", retrieval_rank=2),
                ContextChildEvidence(record=OTHER, role="neighbor_context", reason="adjacent_child"),
            ),
        ),
        ContextParentEvidence(record=MANUAL, role="synthesis_parent", reason="r"),
    )
    return AnswerContext(records=[MANUAL, RULES], text=GUIDE + "\n" + RULES.text, evidence_tree=tree)


def _labels() -> dict[str, str]:
    spans = _grounded_spans(QUESTION, _context(), (), None)
    return {"article_1": _label(spans, "第一条"), "article_2": _label(spans, "第二条"), "guide": _label(spans, "提出の手順")}


def _label(spans, head: str) -> str:
    return next(span["evidence_id"] for span in spans if span["text"].startswith(head))


class _Model:
    """schema ごとに用意した応答を順に返し、受け取った入力を記録する。"""

    def __init__(self, drafts: list, audits: list = (), reviews: list = ()):
        self.sources = {"GroundedDraftWire": list(drafts), "CatalogEvidenceReviews": list(reviews)}
        self.audits, self.prompts = list(audits), []

    def __call__(self, system, prompt, settings, schema, **options):
        observed = schema.canonical_schema if is_audit_wire(schema) else schema
        self.prompts.append((observed.__name__, prompt))
        source = self.sources.get(observed.__name__, self.audits)
        value = source.pop(0) if len(source) > 1 else source[0]
        # 生成の既存quote fixtureをcanonical契約で再生する。
        canonical_schema = GroundedDraft if schema.__name__ == "GroundedDraftWire" else schema
        payload = value(prompt) if callable(value) else value
        if is_audit_wire(schema):
            return legacy_audit_result(payload, schema)
        return canonical_schema.model_validate(payload)

    def names(self) -> list[str]:
        return [name for name, _prompt in self.prompts]


def _run(model, mode=grounded.AUDIT_MODE_NONE, *, review=False):
    def unused(*a, **k):
        raise AssertionError("unexpected I/O")

    settings = replace(get_settings(), answer_audit_mode=mode, article_catalog_review=review)
    with bind_dependencies(AnswerDependencies(unused, unused, model, unused, unused)):
        return synthesize_grounded_answer(QUESTION, _context(), settings, image_prompt_mode="text_only").response


def _draft(items: list[dict], scope: list[tuple[str, str]], evidence_ids: list[str]) -> dict:
    plan = {
        "request_id": "Q1",
        "evidence_scope": [{"evidence_id": eid, "target": "申請書", "relation": relation} for eid, relation in scope],
        "evidence_ids": evidence_ids,
    }
    return {"request_coverage": [plan], "items": items, "summary": "", "confidence": "high"}


def _claim(*evidence_ids: str, text: str = CLAIM) -> dict:
    return {"kind": "rule", "request_id": "Q1", "text": text, "citations": [{"evidence_id": eid} for eid in evidence_ids]}


def _timing(result) -> dict:
    return result.generation_trace["rounds"][0]["timing"]


def test_only_the_evidence_over_the_chosen_article_is_marked():
    spans = evidence_spans(QUESTION, [RULES], scope_records=[CHOSEN, OTHER])
    marked = {span["text"].split("\n", 1)[0]: span[ARTICLE_CATALOG_KEY] for span in spans}
    assert marked == {"第一条": True, "第二条": False}


def test_the_prompt_shows_the_catalog_mark_on_the_evidence_line():
    spans = grounded.tag_spans(QUESTION, _grounded_spans(QUESTION, _context(), (), None))
    lines = [line for line in grounded.format_functions(spans).splitlines() if line.startswith("[E")]
    assert [line.endswith(" catalog") for line in lines].count(True) == 1
    assert "catalog" in grounded.GENERATE_SYSTEM_PROMPT


def _review(*supports: tuple[int, str]):
    """検討に渡した条ごとに、同じ説明を支えると答える（supports が空なら支える説明なし）。"""

    def respond(prompt: str) -> dict:
        evidence = json.loads(prompt)["evidence"]
        return {
            "reviews": [
                {"evidence_id": entry["evidence_id"], "supports": [{"item_index": i, "lines": lines} for i, lines in supports]}
                for entry in evidence
            ]
        }

    return respond


def test_catalog_observation_does_not_add_an_uncited_article():
    labels = _labels()
    draft = _draft([_claim(labels["guide"])], [(labels["guide"], "direct")], [labels["guide"]])
    model = _Model([draft], [_audit("supported", "matched", "")], [_review((0, ""))])

    result = _run(model, grounded.AUDIT_MODE_STANDARD, review=True)

    assert model.names() == ["GroundedDraftWire", "CatalogEvidenceReviews", "GroundedFactAudit"]
    review_input = json.loads(model.prompts[1][1])
    # 検討するのは目録で選んだ条だけ。説明は index つきで渡す。
    assert [entry["text"].split("\n", 1)[0] for entry in review_input["evidence"]] == ["第一条"]
    assert review_input["items"] == [{"index": 0, "kind": "rule", "text": CLAIM}]
    assert _sources(result) == {"手順書B.pdf"}
    assert result.answer_text.count(CLAIM) == 1
    review = _timing(result)["catalog_review"]
    assert review["execution"] == "completed" and review["adopted"] is False
    assert len(review["observation"]["reviews"]) == 1
    assert review["observation"]["reviews"][0]["supports"]
    assert "cited" not in review
    assert _timing(result)["generation_calls"] == 2


def test_an_article_supporting_no_claim_is_not_cited():
    labels = _labels()
    draft = _draft(
        [_claim(labels["guide"])], [(labels["guide"], "direct"), (labels["article_1"], "direct")], [labels["guide"], labels["article_1"]]
    )
    model = _Model([draft], [_audit("supported", "matched", "")], [_review()])

    result = _run(model, grounded.AUDIT_MODE_STANDARD, review=True)

    assert model.names() == ["GroundedDraftWire", "CatalogEvidenceReviews", "GroundedFactAudit"]
    assert _sources(result) == {"手順書B.pdf"}
    observation = _timing(result)["catalog_review"]
    assert observation["adopted"] is False
    assert observation["observation"]["reviews"][0]["supports"] == []


def test_a_citation_added_by_the_review_is_dropped_when_the_audit_does_not_support_it():
    """足した条の出典も条の出典の監査に掛け、支持されなければ外す（説明は元の出典で残る）。"""
    labels = _labels()
    draft = _draft([_claim(labels["guide"])], [(labels["guide"], "direct")], [labels["guide"]])

    def audit_by_source(prompt: str) -> dict:
        items = json.loads(prompt)["items"]
        reviews = [(entry["index"], "unsupported" if entry["evidence_id"] == labels["article_1"] else "supported") for entry in items]
        return {
            "goal_alignment": "aligned",
            "summary_supported": True,
            "reviews": [{"index": i, "support": s, "applicability": "matched", "condition": "", "reason": "r"} for i, s in reviews],
            "request_reviews": [{"request_id": "Q1", "status": "addressed", "reason": "r"}],
        }

    model = _Model([draft], [audit_by_source], [_review((0, ""))])

    result = _run(model, grounded.AUDIT_MODE_STANDARD, review=True)

    assert _sources(result) == {"手順書B.pdf"}
    assert not result.envelope["passage_marks"]


def test_articles_cited_or_judged_unrelated_are_not_reviewed():
    labels = _labels()
    for draft in (
        _draft([_claim(labels["guide"])], [(labels["guide"], "direct"), (labels["article_1"], "unrelated")], [labels["guide"]]),
        _draft([_claim(labels["article_1"])], [(labels["article_1"], "direct")], [labels["article_1"]]),
    ):
        model = _Model([draft], [_audit("supported", "matched", "")])

        result = _run(model, grounded.AUDIT_MODE_STANDARD, review=True)

        assert "CatalogEvidenceReviews" not in model.names()
        assert "catalog_review" not in _timing(result)


def test_the_review_is_off_by_default_and_makes_no_call():
    """既定（設定が無効）では、引かなかった条があっても検討の呼び出しをしない。印は付ける。"""
    assert get_settings().article_catalog_review is False
    labels = _labels()
    model = _Model([_draft([_claim(labels["guide"])], [(labels["guide"], "direct")], [labels["guide"]])], [], [_review((0, ""))])

    result = _run(model)

    assert model.names() == ["GroundedDraftWire"]
    assert "catalog_review" not in _timing(result)
    assert _sources(result) == {"手順書B.pdf"}
    assert any(line.endswith(" catalog") for line in model.prompts[0][1].splitlines() if line.startswith("[E"))


def _sources(result) -> set[str]:
    return {entry["source"] for entry in result.cited_evidence}


def _audit(support: str, applicability: str, condition: str):
    """監査に渡した説明ごとに同じ判定を返す（条の出典の説明の監査）。"""

    def respond(prompt: str) -> dict:
        items = json.loads(prompt)["items"]
        return {
            "goal_alignment": "aligned",
            "summary_supported": True,
            "reviews": [
                {"index": entry["index"], "support": support, "applicability": applicability, "condition": condition, "reason": "r"}
                for entry in items
            ],
            "request_reviews": [{"request_id": "Q1", "status": "addressed", "reason": "r"}],
        }

    return respond


def test_observed_exception_does_not_inject_condition_and_keeps_citation_line():
    """本則を述べ例外を省いた説明は、監査が conditional と判定すれば条件を添えて出典行を残す（unsupported にしない）。"""
    labels = _labels()
    draft = _draft([_claim(labels["article_1"])], [(labels["article_1"], "direct")], [labels["article_1"]])
    model = _Model([draft], [_audit("supported", "conditional", "第三号に該当する者を除く")])

    result = _run(model, grounded.AUDIT_MODE_STANDARD)

    assert model.names() == ["GroundedDraftWire", "GroundedFactAudit"]
    assert not result.envelope["passage_marks"]
    body = result.answer_text
    assert CLAIM in body
    assert grounded.answer_labels().condition(grounded._condition_phrase("第三号に該当する者を除く")) + CLAIM not in body
    raw = _timing(result)["semantic_audit_observation"]
    assert raw["adopted"] is False
    assert raw["audit"]["reviews"][0]["applicability"] == "conditional"
    prefix = grounded.answer_labels().citation_prefix
    assert [line for line in body.splitlines() if line.startswith(prefix) and "規則A.xml" in line]
    assert "例外" in grounded.audit_system_prompt(detailed=False)


def test_none_skips_saved_catalog_review_setting_without_changing_it():
    labels = _labels()
    model = _Model([_draft([_claim(labels["guide"])], [(labels["guide"], "direct")], [labels["guide"]])])
    result = _run(model, grounded.AUDIT_MODE_NONE, review=True)
    assert model.names() == ["GroundedDraftWire"]
    assert _timing(result)["catalog_review"] == {"execution": "not_run", "skipped": "audit_disabled", "reviewed": 0}
    assert result.generation_trace["rounds"][0]["audit_calls"] == 0
    assert _sources(result) == {"手順書B.pdf"}
