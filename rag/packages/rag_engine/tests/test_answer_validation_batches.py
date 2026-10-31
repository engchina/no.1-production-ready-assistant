"""最終の検証の、段落の組ごとの並行の監査と期限（#2700）。モデルはスタブ（実時間の待ちは使わない）。"""

import json
import threading
from unittest.mock import patch

import pytest

from rag_engine.evaluation.answer_eval import EvaluationContractError
from rag_engine.evaluation.answer_validation import (
    QUESTION_ID,
    STATUS_PARTIAL,
    STATUS_TIMED_OUT,
    TIMED_OUT_REASON,
    ClaimAuditOutput,
    passage_batches,
    validate_answer_claims,
)
from rag_engine.resources.deadline import Deadline, DeadlineExceeded, bind_deadline, expire_deadline

EVIDENCE = [{"id": "c1", "source": "manual.pdf", "page_start": 1, "page_end": 1, "text": "検証用アカウントの有効期限は最長 30 日です。"}]
# 主張の段落が5つの回答。各段落に実際の出典を付け、並行・期限の境界だけを検査する。
ANSWER = "\n\n".join(f"区分 {index} の検証用アカウントの有効期限は最長 30 日です。\n根拠：manual.pdf p.1" for index in range(1, 6))
PATCH = "rag_engine.evaluation.answer_eval.parse_text_response"


def _request(inputs: str) -> tuple[list[str], list[str], str]:
    """モデルに渡した段落の ID・本文と、資料の根拠の evidence_id。"""
    payload = json.loads(inputs)
    passages = payload["answer_passages"]
    evidence = next(item for item in payload["evidence_items"] if item.get("id") != QUESTION_ID)
    return [item["id"] for item in passages], [item["text"] for item in passages], evidence["evidence_id"]


def _supported(ids: list[str], evidence_id: str) -> ClaimAuditOutput:
    return ClaimAuditOutput.model_validate(
        {
            "claim_checks": [
                {
                    "answer_quote": "段落",
                    "answer_passage_id": passage_id,
                    "status": "supported",
                    "evidence_id": evidence_id,
                    "source_id": "",
                    "evidence_quote": "",
                    "reason": "根拠に記載",
                }
                for passage_id in ids
            ]
        }
    )


def test_passage_batches_keep_the_answer_order() -> None:
    items = [{"id": str(index), "text": "t"} for index in range(5)]
    assert [[item["id"] for item in batch] for batch in passage_batches(items, 2)] == [["0", "1"], ["2", "3"], ["4"]]
    assert passage_batches(items, None) == [items]
    assert passage_batches(items, 10) == [items]
    assert passage_batches([], 2) == []


def test_without_batches_the_whole_answer_is_audited_in_one_call() -> None:
    calls: list[list[str]] = []

    def parse(system, inputs, settings, schema, provider_id=None):
        ids, _texts, evidence_id = _request(inputs)
        calls.append(ids)
        return _supported(ids, evidence_id)

    with patch(PATCH, side_effect=parse):
        result = validate_answer_claims("有効期限は？", ANSWER, EVIDENCE, settings=None)
    assert len(calls) == 1 and len(calls[0]) == 5
    assert result["status"] == "completed" and result["timed_out_passage_ids"] == []


def test_batches_are_audited_in_parallel_up_to_the_limit_with_the_same_inputs() -> None:
    """組ごとに段落だけを替えて監査し（質問・回答の全文・根拠は同じ）、同時の呼び出しは上限を超えない。"""
    lock = threading.Lock()
    active = 0
    peak = 0
    # 2 組が同時に呼ばれないと先へ進めない（並行していることを、実時間の待ちを使わずに確かめる）。
    barrier = threading.Barrier(2, timeout=10)
    calls: list[tuple[list[str], str, list[str]]] = []

    def parse(system, inputs, settings, schema, provider_id=None):
        nonlocal active, peak
        payload = json.loads(inputs)
        ids, texts, evidence_id = _request(inputs)
        with lock:
            active += 1
            peak = max(peak, active)
            calls.append((ids, payload["answer_text"], [item["evidence_id"] for item in payload["evidence_items"]]))
            first_wave = len(calls) <= 2
        try:
            if first_wave:
                barrier.wait()
            return _supported(ids, evidence_id)
        finally:
            with lock:
                active -= 1

    with patch(PATCH, side_effect=parse):
        result = validate_answer_claims("有効期限は？", ANSWER, EVIDENCE, settings=None, batch_passages=2, max_concurrency=2)
    assert peak == 2
    assert sorted(len(ids) for ids, _answer, _evidence in calls) == [1, 2, 2]
    # どの組にも回答の全文と同じ根拠を渡す。
    assert {answer for _ids, answer, _evidence in calls} == {ANSWER}
    assert len({tuple(evidence) for _ids, _answer, evidence in calls}) == 1
    assert result["status"] == "completed"
    assert result["counts"] == {"supported": 5}
    # 判定は回答の段落の順。
    assert [claim["answer_quote"] for claim in result["claim_checks"]] == [part.split("\n", 1)[0] for part in ANSWER.split("\n\n")]


def test_batches_past_the_deadline_become_unassessed_and_the_rest_is_returned() -> None:
    """期限までに返らなかった組の段落だけを unassessed（partial）にし、確かめ終わった組の判定は使う。"""

    def parse(system, inputs, settings, schema, provider_id=None):
        ids, texts, evidence_id = _request(inputs)
        if any("区分 3" in text for text in texts):
            # この組の呼び出しは期限まで返らない（残りの時間で出せる出力に届いて期限を切った）。
            expire_deadline()
            raise DeadlineExceeded("回答の期限までに LLM の出力が終わりません。")
        return _supported(ids, evidence_id)

    deadline = Deadline(600)
    with bind_deadline(deadline), patch(PATCH, side_effect=parse):
        # 並行 1: 期限を切った組の後の組も、外側の期限は残るので監査する（組の期限は組ごと）。
        result = validate_answer_claims("有効期限は？", ANSWER, EVIDENCE, settings=None, batch_passages=2, max_concurrency=1)
    assert result["status"] == STATUS_PARTIAL
    assert result["counts"] == {"supported": 3, "unassessed": 2}
    unassessed = [claim for claim in result["claim_checks"] if claim["status"] == "unassessed"]
    assert [claim["answer_quote"] for claim in unassessed] == [part.split("\n", 1)[0] for part in ANSWER.split("\n\n")][2:4]
    assert {claim["reason"] for claim in unassessed} == {TIMED_OUT_REASON}
    assert len(result["timed_out_passage_ids"]) == 2
    assert deadline.remaining() and not deadline.expired


def test_errors_after_the_outer_deadline_are_treated_as_timed_out() -> None:
    """LLM の read timeout（期限の残りに縮めたもの）で切れた呼び出しも、期限を過ぎていれば時間切れとして扱う。"""
    deadline = Deadline(600)

    def parse(system, inputs, settings, schema, provider_id=None):
        deadline.cancel()
        raise TimeoutError("read timeout")

    with bind_deadline(deadline), patch(PATCH, side_effect=parse):
        result = validate_answer_claims("有効期限は？", ANSWER, EVIDENCE, settings=None, batch_passages=2, max_concurrency=2)
    # どの段落も監査し終わらなかった。
    assert result["status"] == STATUS_TIMED_OUT
    assert result["claim_checks"] == []


def test_failures_before_the_deadline_still_fail_the_validation() -> None:
    """期限の前の失敗（出力の契約の違反が続いた等）は、分けない監査と同じく失敗として送出する。"""

    def parse(system, inputs, settings, schema, provider_id=None):
        ids, texts, evidence_id = _request(inputs)
        if any("区分 5" in text for text in texts):
            raise EvaluationContractError("回答段落IDが存在しません")
        return _supported(ids, evidence_id)

    with bind_deadline(Deadline(600)), patch(PATCH, side_effect=parse), pytest.raises(EvaluationContractError):
        validate_answer_claims("有効期限は？", ANSWER, EVIDENCE, settings=None, batch_passages=2, max_concurrency=2)
