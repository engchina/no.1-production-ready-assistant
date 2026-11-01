"""最終検証の実際の引用範囲。合成判定はモデルの意味判定の精度を証明しない。"""

import json
from unittest.mock import patch

import pytest

from rag_engine.evaluation.answer_validation import ClaimAuditOutput, validate_answer_claims

OPERATION = "対象Aの設定画面で保存ボタンを押します。"
RELATED = "対象Aの設定画面には状態が表示されます。"
CONDITION = "承認済みの場合だけ保存できます。"
FIRST, SECOND = "aaaaaa:bbbbbb:1", "aaaaaa:bbbbbb:2"


@pytest.mark.parametrize("first_text", [RELATED, "対象Bの設定画面で保存ボタンを押します。"])
def test_uncited_evidence_cannot_replace_the_actual_citation(first_text):
    answer = f"{OPERATION}【証拠 ID {FIRST}】"
    evidence = [{"id": FIRST, "source": "a.pdf", "text": first_text}, {"id": SECOND, "source": "b.pdf", "text": OPERATION}]

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        if schema is not ClaimAuditOutput:
            return schema(checks=[])
        chosen = next(row for row in payload["evidence_items"] if row["id"] == SECOND)
        return schema(
            claim_checks=[
                {
                    "answer_quote": answer,
                    "answer_passage_id": payload["answer_passages"][0]["id"],
                    "status": "supported",
                    "evidence_id": chosen["evidence_id"],
                    "evidence_quote": OPERATION,
                    "source_id": "",
                    "applicability": "matched",
                    "reason": "合成の誤った引用選択",
                }
            ]
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("設定手順は？", answer, evidence, None)
    claim = result["claim_checks"][0]
    assert claim["status"] == "unassessed"
    assert claim["support_check"] == "uncited_evidence"
    assert claim["audit_status"] == "supported"


@pytest.mark.parametrize("support_quote", [OPERATION, OPERATION + "…" + CONDITION])
def test_two_actual_citations_can_jointly_support_operation_and_condition(support_quote):
    answer = f"承認済みの場合だけ、{OPERATION}【証拠 ID {FIRST}】【証拠 ID {SECOND}】"
    evidence = [{"id": FIRST, "source": "a.pdf", "text": OPERATION}, {"id": SECOND, "source": "b.pdf", "text": CONDITION}]

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        if schema is not ClaimAuditOutput:
            return schema(checks=[])
        docs = [row for row in payload["evidence_items"] if row["id"] in {FIRST, SECOND}]
        passage = payload["answer_passages"][0]
        assert set(payload["passage_citations"][passage["id"]]) == {row["evidence_id"] for row in docs}
        return schema(
            claim_checks=[
                {
                    "answer_quote": answer,
                    "answer_passage_id": passage["id"],
                    "status": "supported",
                    "evidence_id": ",".join(row["evidence_id"] for row in docs),
                    "evidence_quote": support_quote,
                    "source_id": "",
                    "applicability": "matched",
                    "reason": "操作と条件を実際の2引用で共同確認",
                }
            ]
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("設定手順は？", answer, evidence, None)
    assert result["counts"] == {"supported": 1}


def test_unknown_target_does_not_become_supported_even_with_an_actual_citation():
    answer = f"{OPERATION}【証拠 ID {FIRST}】"

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        if schema is not ClaimAuditOutput:
            return schema(checks=[])
        evidence = next(row for row in payload["evidence_items"] if row["id"] == FIRST)
        return schema(
            claim_checks=[
                {
                    "answer_quote": answer,
                    "answer_passage_id": payload["answer_passages"][0]["id"],
                    "status": "supported",
                    "evidence_id": evidence["evidence_id"],
                    "evidence_quote": OPERATION,
                    "source_id": "",
                    "applicability": "unknown",
                    "reason": "対象との対応は未確認",
                }
            ]
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse):
        result = validate_answer_claims("設定手順は？", answer, [{"id": FIRST, "text": OPERATION}], None)
    assert result["counts"] == {"unassessed": 1}
    assert result["claim_checks"][0]["support_check"] == "target_unverified"


def _supported_by(chosen_id: str, seen: list[dict]):
    """監査が段落を ``chosen_id`` の根拠で supported と返す合成の応答（渡された入力を ``seen`` に残す）。"""

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        if schema is not ClaimAuditOutput:
            return schema(checks=[])
        seen.append(payload)
        passage = payload["answer_passages"][0]
        chosen = next(row for row in payload["evidence_items"] if row["id"] == chosen_id)
        return schema(
            claim_checks=[
                {
                    "answer_quote": passage["text"],
                    "answer_passage_id": passage["id"],
                    "status": "supported",
                    "evidence_id": chosen["evidence_id"],
                    "evidence_quote": OPERATION,
                    "source_id": "",
                    "applicability": "matched",
                    "reason": "合成の判定",
                }
            ]
        )

    return parse


def test_caller_citations_are_the_citation_of_a_passage_without_one():
    # 本文に出典を書かない呼び出し側（業務 Agent。#104）: 段落の位置と chunk_id を渡せば、その根拠で
    # 裏付けを確かめる。渡さなければ今までどおり unassessed（未引用の資料で裏付けない）。
    evidence = [{"id": FIRST, "source": "a.pdf", "text": OPERATION}, {"id": SECOND, "source": "b.pdf", "text": RELATED}]
    citations = [{"start": 0, "end": len(OPERATION), "chunk_ids": [FIRST]}]
    seen: list[dict] = []
    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=_supported_by(FIRST, seen)):
        without = validate_answer_claims("設定手順は？", OPERATION, evidence, None)
        cited = validate_answer_claims("設定手順は？", OPERATION, evidence, None, caller_citations=citations)
    assert without["claim_checks"][0]["status"] == "unassessed"
    assert without["claim_checks"][0]["support_check"] == "uncited_evidence"
    assert seen[0]["passage_citations"][seen[0]["answer_passages"][0]["id"]] == []
    assert cited["counts"] == {"supported": 1}
    first = next(row["evidence_id"] for row in seen[-1]["evidence_items"] if row["id"] == FIRST)
    assert seen[-1]["passage_citations"][seen[-1]["answer_passages"][0]["id"]] == [first]


def test_caller_citations_do_not_widen_the_citation_the_answer_wrote():
    # 本文が出典を書いた段落は本文の出典だけを使う（呼び出し側の根拠で未引用の資料に広げない）。
    answer = f"{OPERATION}【証拠 ID {FIRST}】"
    evidence = [{"id": FIRST, "source": "a.pdf", "text": RELATED}, {"id": SECOND, "source": "b.pdf", "text": OPERATION}]
    citations = [{"start": 0, "end": len(answer), "chunk_ids": [SECOND]}]
    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=_supported_by(SECOND, [])):
        result = validate_answer_claims("設定手順は？", answer, evidence, None, caller_citations=citations)
    assert result["claim_checks"][0]["status"] == "unassessed"
    assert result["claim_checks"][0]["support_check"] == "uncited_evidence"


@pytest.mark.parametrize(
    "citation",
    [
        {"start": 0, "end": len(OPERATION), "chunk_ids": ["ffffff:eeeeee:9"]},  # evidence に無い chunk
        {"start": len(OPERATION) + 5, "end": len(OPERATION) + 9, "chunk_ids": [FIRST]},  # 段落と重ならない位置
    ],
    ids=["unknown-chunk", "other-range"],
)
def test_caller_citations_outside_the_evidence_or_the_passage_are_ignored(citation):
    evidence = [{"id": FIRST, "source": "a.pdf", "text": OPERATION}]
    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=_supported_by(FIRST, [])):
        result = validate_answer_claims("設定手順は？", OPERATION, evidence, None, caller_citations=[citation])
    assert result["claim_checks"][0]["status"] == "unassessed"
