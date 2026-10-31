"""RAG の段落の判定を、段落の位置（`answer_range`）で Agent の段落に結び付ける（#3240）。

RAG の `rag_validate_answer` は回答を RAG の規則で段落に分けて判定する。Agent は RAG の分け方を
写さず、契約（platform/contracts/mcp/rag-tools.json）の `answer_range`（渡した回答の中の位置）で
自分の段落に結び付ける。ここでは RAG の分け方が Agent と違う合成の判定で、外す・印を付ける段落が
位置どおりに決まることを確かめる（RAG の分け方の一致を前提にしない）。
"""

from __future__ import annotations

from typing import Any

from app.features.agent.answer_validation import (
    REMOVED_HEADING,
    STATUS_COMPLETED,
    align_claims,
    cited_chunk_ids,
    combine_validations,
    validation_content,
)


def _claim(answer: str, quote: str, status: str, *, ranged: bool = True) -> dict[str, Any]:
    start = answer.index(quote)
    claim: dict[str, Any] = {
        "answer_quote": quote,
        "status": status,
        "chunk_id": "chunk-1",
        "reason": "合成の判定",
    }
    if ranged:
        claim["answer_range"] = {"start": start, "end": start + len(quote)}
    return claim


def _combine(answer: str, claims: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    result = {"status": "completed", "valid": False, "claims": claims}
    connection = validation_content(
        STATUS_COMPLETED,
        connection="rag",
        tool_name="rag__rag_validate_answer",
        step_id="step-1",
        evidence=[{"document_id": "doc-1", "chunk_id": "chunk-1"}],
        result=result,
    )
    return combine_validations(answer, [connection])


def test_rag_passage_spanning_two_agent_passages_marks_both() -> None:
    """RAG が 2 つの文を 1 つの段落として矛盾と判定したら、Agent の 2 つの段落を両方外す。"""
    answer = "有効期限は 30 日です。延長は 90 日までです。\n\n申請は管理者が行います。"
    quote = "有効期限は 30 日です。延長は 90 日までです。"
    claims = [
        _claim(answer, quote, "contradicted"),
        _claim(answer, "申請は管理者が行います。", "supported"),
    ]

    content, published = _combine(answer, claims)

    removed = [item["text"] for item in content["removed_passages"]]
    assert removed == ["有効期限は 30 日です。", "延長は 90 日までです。"]
    body = published.split(f"**{REMOVED_HEADING}**")[0]
    assert "有効期限は 30 日です。" not in body and "延長は 90 日までです。" not in body
    assert "申請は管理者が行います。" in body
    # 元の段落の原文を残す。
    aligned = content["connections"][0]["result"]["claims"]
    assert {claim["rag_answer_quote"] for claim in aligned if "rag_answer_quote" in claim} == {
        quote
    }


def test_two_rag_passages_inside_one_agent_passage_take_the_stricter_judgment() -> None:
    """Agent の 1 つの段落（括弧の中では分けない）に RAG の段落が 2 つ入れば、厳しい判定（矛盾）を
    採ってその段落を外す（矛盾した内容を本文に残さない）。"""
    answer = "画面で値を確かめます。\n\n手順は「設定を開く。保存する。」です。"
    claims = [
        _claim(answer, "画面で値を確かめます。", "supported"),
        _claim(answer, "手順は「設定を開く。", "supported"),
        _claim(answer, "保存する。」です。", "contradicted"),
    ]

    content, published = _combine(answer, claims)

    assert [item["text"] for item in content["removed_passages"]] == [
        "手順は「設定を開く。保存する。」です。"
    ]
    assert "画面で値を確かめます。" in published


def test_unverified_mark_follows_the_rag_range() -> None:
    """未確認の判定も位置で Agent の段落に結び付け、その段落に印を付ける（本文には残す）。"""
    answer = "対象は 3 件です。通知は翌日に届きます。"
    claims = [
        _claim(answer, "対象は 3 件です。", "supported"),
        _claim(answer, "通知は翌日に届きます。", "unsupported"),
    ]

    content, published = _combine(answer, claims)

    assert [item["text"] for item in content["passage_marks"]] == ["通知は翌日に届きます。"]
    assert content["removed_passages"] == []
    assert published.startswith(answer)


def test_claims_without_a_usable_range_are_kept_as_they_are() -> None:
    """位置の無い判定・位置の原文が段落と違う判定・回答の外の位置は変えない（原文で探す）。"""
    answer = "対象は 3 件です。通知は翌日に届きます。"
    without = _claim(answer, "対象は 3 件です。", "supported", ranged=False)
    mismatched = {**_claim(answer, "通知は翌日に届きます。", "unsupported")}
    mismatched["answer_range"] = {"start": 0, "end": 4}
    outside = {**_claim(answer, "通知は翌日に届きます。", "unsupported")}
    outside["answer_range"] = {"start": 0, "end": len(answer) + 5}
    result = {"status": "completed", "claims": [without, mismatched, outside]}

    assert align_claims(answer, result) is result


def test_claim_matching_one_agent_passage_is_unchanged() -> None:
    """RAG の段落と Agent の段落が同じなら判定は変えない（`rag_answer_quote` を足さない）。"""
    answer = "対象は 3 件です。通知は翌日に届きます。"
    result = {"status": "completed", "claims": [_claim(answer, "対象は 3 件です。", "supported")]}

    assert align_claims(answer, result) is result


def test_abbreviated_ids_are_matched_from_the_given_id_values() -> None:
    """回答が省いて書いた根拠の id は、渡された id の値（区切りの文字を含む）で照合する。根拠の
    id は契約では不透明な文字列なので、RAG の id の形（16 進・「:」の区切り）を前提にしない
    （#3240）。"""
    ids = ["kb7f3a91c2/sec/0007", "kb7f3a91c2/sec/0008"]

    assert cited_chunk_ids("根拠は【kb7f3a91…/0008】です。", ids) == {"kb7f3a91c2/sec/0008"}
    # どちらの根拠か決まらない書き方（番号を省いた）は数えない。
    assert cited_chunk_ids("根拠は【kb7f3a91c2/sec】です。", ids) == set()
    # そのままの形は区切りに関わらず数える。
    assert cited_chunk_ids("根拠: kb7f3a91c2/sec/0007", ids) == {"kb7f3a91c2/sec/0007"}
