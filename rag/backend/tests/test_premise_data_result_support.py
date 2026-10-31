"""照会結果の原文と複数根拠の引用照合を分ける。モデルは決定論スタブ。"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from rag_engine.evaluation.answer_validation import ClaimAuditOutput, validate_answer_claims

DOCUMENT = {
    "id": "doc-a",
    "source": "synthetic.pdf",
    "text": "条件Aの場合は確認担当へ案内します。",
    "page_start": 1,
    "page_end": 1,
}
DATA = [
    {
        "source": "合成照会",
        "question": "合成件数",
        "sql": "SELECT COUNT(*) FROM SYNTHETIC_TABLE",
        "result": "取得件数: 2",
    }
]


def evaluate(
    *, joint: bool = False, fabricated: bool = False, data_first: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    captured: dict[str, Any] = {}

    def model(system: str, inputs: str, settings: Any, schema: Any, provider_id: Any = None) -> Any:
        assert schema is ClaimAuditOutput
        payload = json.loads(inputs)
        captured.update(payload=payload)
        data = next(row for row in payload["evidence_items"] if row["id"] == "data:1")
        document = next(row for row in payload["evidence_items"] if row["id"] == "doc-a")
        chosen = ([data, document] if data_first else [document, data]) if joint else [data]
        quote = "\n".join(row["text"] for row in chosen)
        if fabricated:
            quote = "2件のデータが見つかりました。"
        captured.update(quote=quote, evidence_ids=[row["evidence_id"] for row in chosen])
        return ClaimAuditOutput.model_validate(
            {
                "claim_checks": [
                    {
                        "answer_quote": payload["answer_passages"][0]["text"],
                        "answer_passage_id": payload["answer_passages"][0]["id"],
                        "status": "supported",
                        "applicability": "matched",
                        "reason": "合成の原文照合",
                        "source_id": "",
                        "evidence_id": ",".join(row["evidence_id"] for row in chosen),
                        "evidence_quote": quote,
                    }
                ]
            }
        )

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=model):
        result = validate_answer_claims(
            "件数は？",
            "結果は2件です。\n根拠：synthetic.pdf",
            [DOCUMENT],
            settings=None,
            data_results=DATA,
        )
    return result, captured


def test_nonempty_data_result_quote_resolves_to_the_actual_data_source() -> None:
    result, captured = evaluate()
    check = result["claim_checks"][0]
    assert check["source_id"] == "data:1"
    assert check["evidence_id"] == captured["evidence_ids"][0]
    assert check["status"] == "supported" and check["support_check"] != "quote_unmatched"
    assert "取得件数: 2" in check["support_quote"]


@pytest.mark.parametrize("data_first", [False, True])
def test_document_and_data_joint_quote_keeps_both_explicit_evidence_ids(data_first: bool) -> None:
    result, captured = evaluate(joint=True, data_first=data_first)
    check = result["claim_checks"][0]
    assert check["evidence_id"].split(",") == captured["evidence_ids"]
    assert check["source_id"] == "doc-a"
    assert check["support_quote"] == captured["quote"]
    assert check["support_check"] != "quote_unmatched"
    assert check["status"] == "supported"


def test_paraphrased_count_does_not_impersonate_the_data_result_original_quote() -> None:
    result, _captured = evaluate(fabricated=True)
    check = result["claim_checks"][0]
    assert check["status"] == "citation_error"
    assert check["support_check"] == "quote_unmatched"
    assert check["source_id"] == "data:1"
