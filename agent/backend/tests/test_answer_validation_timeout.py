"""最終の検証が RAG の時間の上限で確かめ終わらなかったときの回答（#2700）の決定論テスト。

RAG は時間の上限までに監査し終わらなかった段落を unassessed（status=partial）で返し、
どの段落も終わらなければ status=timed_out を返す。Agent は確かめ終わった判定で回答を
組み立て、確かめ終わらなかった段落は外さない。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from mcp_support import DEFAULT_OUTPUTS, FakeProductMcp
from pytest import MonkeyPatch
from test_answer_validation import (  # noqa: F401 - fixture を使う
    ANSWER,
    _searched_run,
    _validation,
    mcp,
)

from app.features.agent.answer_evidence import ANSWER_EVIDENCE_KIND
from app.features.agent.answer_validation import (
    REMOVED_HEADING,
    REVIEW_HEADING,
    UNVERIFIED_NOTICE,
    merge_results,
    publish_answer,
)
from app.features.agent.runtime import RunState, run_answer_text

FIRST = "契約の更新は 30 日前までに申し出ます。"
SECOND = "申し出は所定の様式で行います。"
THIRD = "更新の手数料はかかりません。"
PARTIAL_ANSWER = f"{FIRST}\n\n{SECOND}\n\n{THIRD}"


def _claim(quote: str, status: str) -> dict[str, Any]:
    claim: dict[str, Any] = {"answer_quote": quote, "status": status, "reason": "理由"}
    if status in {"supported", "contradicted"}:
        claim["chunk_id"] = "chunk-1"
    return claim


def _result(status: str, *claims: dict[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = deepcopy(DEFAULT_OUTPUTS["rag_validate_answer"])
    counts: dict[str, int] = {}
    for claim in claims:
        counts[claim["status"]] = counts.get(claim["status"], 0) + 1
    output.update({"valid": False, "status": status, "counts": counts, "claims": list(claims)})
    return output


def test_partial_result_keeps_the_unfinished_passages_and_removes_contradicted_ones() -> None:
    result = _result(
        "partial",
        _claim(FIRST, "supported"),
        _claim(SECOND, "unassessed"),
        _claim(THIRD, "contradicted"),
    )

    published = publish_answer(PARTIAL_ANSWER, result)

    # 根拠と矛盾した段落だけを外し、時間切れで確かめ終わらなかった段落は本文に残して印を付ける。
    assert published.body == f"{FIRST}\n\n{SECOND}"
    assert [item["text"] for item in published.removed_passages] == [THIRD]
    assert published.passage_marks == [
        {
            "text": SECOND,
            "state": "unverified",
            "reasons": [
                {"code": "unassessed", "label": "確かめが終わっていない", "detail": "理由"}
            ],
        }
    ]
    assert published.headings == [REMOVED_HEADING, REVIEW_HEADING]
    assert published.withheld == {
        "removed": 1,
        "unverified": 1,
        "warnings": 0,
        "all": False,
        "listed": 2,
    }


def test_partial_result_with_only_unfinished_passages_keeps_the_whole_answer() -> None:
    result = _result("partial", _claim(FIRST, "supported"), _claim(SECOND, "unassessed"))
    answer = f"{FIRST}\n\n{SECOND}"

    published = publish_answer(answer, result)

    body, section = published.text.split(f"\n\n**{REVIEW_HEADING}**\n\n", 1)
    assert body == answer
    assert section.splitlines()[1:] == [f"- 確かめが終わっていない: 「{SECOND}」"]
    assert REMOVED_HEADING not in published.text
    assert published.withheld["unverified"] == 1


def test_merge_results_keeps_partial_when_one_connection_timed_out() -> None:
    merged = merge_results(
        [
            _result("completed", _claim(FIRST, "supported")),
            _result("partial", _claim(FIRST, "unassessed")),
        ]
    )
    assert merged["status"] == "partial"
    assert merged["valid"] is False
    # 同じ段落は、どれかの接続で確かめ終わった判定を使う。
    assert [claim["status"] for claim in merged["claims"]] == ["supported"]


def test_timed_out_validation_keeps_the_answer_with_a_notice(
    monkeypatch: MonkeyPatch,
    mcp: FakeProductMcp,  # noqa: F811 - fixture
) -> None:
    mcp.outputs["rag_validate_answer"] = _result("timed_out")

    run, _ = _searched_run(monkeypatch)

    assert run_answer_text(run) == f"{ANSWER}\n\n{UNVERIFIED_NOTICE}"
    content = _validation(run)
    assert (content["status"], content["reason"]) == ("unvalidated", "validation_timed_out")
    # 回答の根拠は照合が使えないときと同じ決定的な結び付け（参考情報。#2707）。
    assert _evidence(run)["binding"] == "text"


def test_partial_validation_run_keeps_the_unfinished_answer(
    monkeypatch: MonkeyPatch,
    mcp: FakeProductMcp,  # noqa: F811 - fixture
) -> None:
    mcp.outputs["rag_validate_answer"] = _result("partial", _claim(ANSWER, "unassessed"))

    run, _ = _searched_run(monkeypatch)

    assert (run_answer_text(run) or "").startswith(f"{ANSWER}\n\n**{REVIEW_HEADING}**")
    content = _validation(run)
    assert content["status"] == "completed"
    assert content["result"]["status"] == "partial"
    assert content["withheld"]["unverified"] == 1
    assert [item["state"] for item in content["passage_marks"]] == ["unverified"]
    # 確かめ終わらなかった段落は本文の根拠の行にせず、参考情報の根拠にする（#2707）。
    evidence = _evidence(run)
    assert evidence["binding"] == "partial"
    assert all(passage["text"] != ANSWER for passage in evidence["passages"])


def _evidence(run: RunState) -> dict[str, Any]:
    [artifact] = [item for item in run.artifacts if item.kind == ANSWER_EVIDENCE_KIND]
    return artifact.content
