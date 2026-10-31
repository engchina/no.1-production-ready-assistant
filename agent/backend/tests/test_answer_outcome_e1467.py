"""評価 e1467 の記録で取り違えた回答の対応（#1491）を、記録から作った fixture で確かめる。

fixture（`fixtures/answer_outcome_e1467.json`）は業務支援の評価（D）の Run の記録から、対応の
決定に使う最小の項目だけを残したもの（モデルの最終の回答・ツールの名前と対応・最終の検証の段落の
判定と指摘）。
モデルは呼ばない。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.features.agent.answer_outcome import answer_outcome
from app.features.agent.answer_validation import publish_answer
from app.features.agent.runtime import RunStep, StepStatus
from app.features.agent.tools import ToolCall, ToolResult

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "answer_outcome_e1467.json").read_text(encoding="utf-8")
)


def _steps(case: dict[str, Any], *, rag_outcome: str | None = None) -> list[RunStep]:
    steps = []
    for item in case["steps"]:
        output = dict(item["output"])
        if rag_outcome is not None and item["name"].endswith("rag_search"):
            output["outcome"] = rag_outcome
        steps.append(
            RunStep(
                run_id="run-1",
                status=StepStatus.COMPLETED if item["success"] else StepStatus.FAILED,
                tool_call=ToolCall(name=item["name"], arguments={}),
                tool_result=ToolResult(
                    name=item["name"],
                    success=item["success"],
                    output=output if item["success"] else None,
                ),
            )
        )
    return steps


def _current(case: dict[str, Any], validation: dict[str, Any] | None = None) -> dict[str, Any]:
    """記録の判定（`result`）から、今の最終の検証が残す段落の印・外した段落・回答の警告を作り直した
    成果物の内容（#2738。fixture は #2738 の前の記録で、段落の印を持たない）。"""
    validation = validation or case["validation"]
    return {**validation, **publish_answer(case["answer"], validation["result"]).record()}


def _outcome(name: str, **kwargs: Any) -> dict[str, Any]:
    case = FIXTURE[name]
    validation = _current(case, kwargs.pop("validation", None))
    return answer_outcome(case["answer"], steps=_steps(case, **kwargs), validation=validation)


def test_unverified_supplement_after_a_supported_conclusion_stays_answered() -> None:
    # da-trial-account-expiry: 結論（最長 30 日）は supported で、確かめられないのは補足の「なお、…
    # 見当たりませんでした。」だけ。不足の印で conditional になっていた（期待は answered）。補足は
    # 本文に残して印を付ける（#2738）。
    content = _outcome("da-trial-account-expiry")
    assert (content["value"], content["basis"]) == ("answered", "rag_search")
    assert content["signals"] == ["supplement_unverified"]


def test_unverified_conclusion_is_still_conditional() -> None:
    # 同じ回答で、結論の段落が確かめられないなら答えの不足（unverified_passages）のまま。
    case = FIXTURE["da-trial-account-expiry"]
    validation = json.loads(json.dumps(case["validation"]))
    for claim in validation["result"]["claims"]:
        if claim["status"] == "supported" and "30" in claim["answer_quote"]:
            claim["status"] = "unsupported"
    content = _outcome("da-trial-account-expiry", validation=validation)
    assert content["value"] == "conditional"
    assert "unverified_passages" in content["signals"]


def test_guide_step_error_keeps_the_answer_and_needs_a_human() -> None:
    # mt-delete-account-approved（2 回目の発言）: 業務ガイドの手順の検査の error
    # （step_wrong_branch）。本文は置き換えず回答全体の警告にする（#2738）。資料が無いのでは
    # ないので needs_human。
    case = FIXTURE["mt-delete-account-approved"]
    published = publish_answer(case["answer"], case["validation"]["result"])
    assert published.body == case["answer"]
    assert "guide_check" in [item["code"] for item in published.answer_warnings]
    content = _outcome("mt-delete-account-approved")
    assert (content["value"], content["basis"]) == ("needs_human", "validation_guide_check")
    assert "check_errors" in content["signals"]


def test_without_the_false_step_error_the_body_is_kept() -> None:
    # RAG の手順の照合が満たした前提の言及を数えなくなると（#1491）、残る error は要求の不足だけで、
    # 本文は外さず、内側の rag_search の対応に拠る。
    case = FIXTURE["mt-delete-account-approved"]
    result = json.loads(json.dumps(case["validation"]["result"]))
    result["findings"] = [item for item in result["findings"] if item["check"] != "guide_steps"]
    validation = {**case["validation"], "result": result}
    # #1453 の後の RAG は、承認・ロールを利用者の条件にして conditional を返す。
    content = _outcome(
        "mt-delete-account-approved", validation=validation, rag_outcome="conditional"
    )
    assert (content["value"], content["basis"]) == ("conditional", "rag_search")


@pytest.mark.parametrize(
    ("rag_outcome", "expected"),
    [("insufficient_evidence", "insufficient_evidence"), ("needs_environment_data", "conditional")],
    ids=["rag-insufficient", "rag-environment-data"],
)
def test_reference_material_only_rag_search_concludes_absence(
    rag_outcome: str, expected: str
) -> None:
    # km-auto-disable: 内側の rag_search は要求がすべて missing で、確認は別の資料の行だけ。RAG が
    # insufficient_evidence を返すようになると（#1491）、答えなし（absence_conclusion）になる。
    content = _outcome("km-auto-disable", rag_outcome=rag_outcome)
    assert content["value"] == expected
    if expected == "insufficient_evidence":
        assert content["basis"] == "absence_conclusion"
