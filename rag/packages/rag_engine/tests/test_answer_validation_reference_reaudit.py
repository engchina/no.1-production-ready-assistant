"""監査の応答の参照の誤りだけで citation_error になった段落の監査し直し（#112）。モデルはスタブ。

形は Agent の Run run_803ced7b691e4e44b29ef31f1a6c5339・run_13a17c51431b401d9a49cacff674a3a4（社内規程の出張の
宿泊費。ID と本文は合成した値）。監査は表の行で正しく supported と判定したのに、裏付けとして写した原文が根拠の本文に
無い（quote_unmatched）・ファイル名を根拠の ID として返した（audit_id_invalid）ため、正しい段落が未確認になっていた。
"""

import json
from unittest.mock import patch

import pytest

from rag_engine.evaluation.answer_validation import (
    REFERENCE_REAUDIT_ADOPTED,
    REFERENCE_REAUDIT_FEEDBACK,
    REFERENCE_REAUDIT_REJECTED,
    ClaimAuditOutput,
    validate_answer_claims,
)
from rag_engine.resources.deadline import Deadline, bind_deadline

DOCUMENT = "e58aecbc4ab249a99374851000ec24e3"
CHUNK_SET = "83996d6ae379da13154e7b58abff920cf0875ec2e605daaa246091624b77849f"
HEADER_CHUNK = f"{DOCUMENT}:{CHUNK_SET}:0"
TABLE_CHUNK = f"{DOCUMENT}:{CHUNK_SET}:3"
EVIDENCE = [
    {
        "id": HEADER_CHUNK,
        "source": "社内規程サンプル.pdf",
        "page_start": 1,
        "page_end": 1,
        "text": "株式会社サンプル 勤務・出張・経費規程\n規程番号 HR-2026-07 / 第3版 / 施行日 2026年10月1日",
    },
    {
        "id": TABLE_CHUNK,
        "source": "社内規程サンプル.pdf",
        "page_start": 1,
        "page_end": 1,
        "text": (
            "第3条（出張旅費）\n<table><tbody><tr><td>区分</td><td>宿泊費の上限（1泊）</td><td>日当</td></tr>"
            "<tr><td>一般社員</td><td>12,000円</td><td>2,000円</td></tr>"
            "<tr><td>課長以上</td><td>15,000円</td><td>3,000円</td></tr></tbody></table>"
        ),
    },
]
QUESTION = "社内規程で、一般社員の出張の宿泊費の上限は1泊いくらですか？"
STAFF = "一般社員の出張の宿泊費の上限は **1泊 12,000円** です（日当は 2,000円）。"
MANAGER = "※ 同一表では「課長以上」は 1泊 15,000円（日当 3,000円）と定められています。"
ANSWER = f"\n\n{STAFF}\n\n{MANAGER}"
# Agent が渡す段落ごとの根拠（#104。本文に出典が無い段落の出典）。
CITATIONS = [{"start": ANSWER.index(text), "end": ANSWER.index(text) + len(text), "chunk_ids": [TABLE_CHUNK]} for text in (STAFF, MANAGER)]
# 監査が言い換えて写した原文（根拠の本文に無い）と、本文のまま写した原文。
PARAPHRASED = {STAFF: "一般社員の宿泊費の上限は1泊12,000円", MANAGER: "課長以上の宿泊費の上限は1泊15,000円"}
VERBATIM = {STAFF: "一般社員 12,000円 2,000円", MANAGER: "課長以上 15,000円 3,000円"}
TABLE = "@table"


def _validate(first, reaudit=None, *, calls: list[dict] | None = None, answer: str = ANSWER, citations=CITATIONS, deadline=None):
    """段落の本文 → (status, evidence_id, evidence_quote) を返すスタブで検証する。

    evidence_id の ``@table`` は表の chunk の片段の ID。監査し直しの呼び出しには ``reaudit`` を返す（例外なら送出し、
    None なら呼び出しを認めない）。``calls`` には監査し直し以外の呼び出しも入る。
    """
    seen: list[dict] = [] if calls is None else calls

    def parse(system, inputs, settings, schema, provider_id=None):
        payload = json.loads(inputs)
        seen.append(payload)
        reauditing = str(payload.get("validation_feedback") or "").startswith(REFERENCE_REAUDIT_FEEDBACK)
        # ID を書かない段落の確かめ直し（#1412）には、最初と同じ判定を返す（確かめ直しでも決まらない）。
        answers = reaudit if reauditing else first
        assert answers is not None, "監査し直しを想定していない"
        if isinstance(answers, Exception):
            raise answers
        if len(seen) == 1 and deadline is not None:
            deadline.expire()
        ids = {item["id"]: item["evidence_id"] for item in payload["evidence_items"]}
        checks = []
        for passage in payload["answer_passages"]:
            status, evidence_id, quote = answers[passage["text"]]
            checks.append(
                {
                    "answer_quote": "段落",
                    "answer_passage_id": passage["id"],
                    "status": status,
                    "evidence_id": evidence_id.replace(TABLE, ids[TABLE_CHUNK]),
                    "evidence_quote": quote,
                    "claim_target": "一般社員の宿泊費",
                    "evidence_target": "第3条の宿泊費の表",
                    "applicability": "matched",
                    "source_id": "",
                    "reason": "第3条の表の行に記載されている。",
                }
            )
        return ClaimAuditOutput.model_validate({"claim_checks": checks})

    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=parse), bind_deadline(deadline):
        return validate_answer_claims(QUESTION, answer, EVIDENCE, settings=None, caller_citations=citations)


def _statuses(result: dict) -> dict[str, tuple[str, str, str]]:
    return {
        claim["answer_quote"]: (claim["status"], claim["reference_check"], claim["reference_reaudit"]) for claim in result["claim_checks"]
    }


def test_quote_unmatched_passages_are_reaudited_once_and_adopted_when_the_quote_is_verbatim() -> None:
    """写した原文が根拠に無い supported の段落だけを、誤りを差し戻して 1 回だけ監査し直し、本文のまま写した
    supported を採る（#112）。"""
    calls: list[dict] = []
    first = {text: ("supported", TABLE, PARAPHRASED[text]) for text in (STAFF, MANAGER)}
    reaudit = {text: ("supported", TABLE, VERBATIM[text]) for text in (STAFF, MANAGER)}
    result = _validate(first, reaudit, calls=calls)

    assert len(calls) == 2
    assert "validation_feedback" not in calls[0]
    feedback = calls[1]["validation_feedback"]
    assert feedback.startswith(REFERENCE_REAUDIT_FEEDBACK)
    # 前回の誤りの種類・返した ID・写した原文を段落ごとに伝える。
    table_id = next(item["evidence_id"] for item in calls[0]["evidence_items"] if item["id"] == TABLE_CHUNK)
    assert f"A1: quote_unmatched（evidence_id: {table_id}、evidence_quote: 「{PARAPHRASED[STAFF]}」）" in feedback
    assert PARAPHRASED[MANAGER] in feedback
    # 監査し直しは同じ根拠で、対象の段落だけ。
    assert [item["text"] for item in calls[1]["answer_passages"]] == [STAFF, MANAGER]
    assert calls[1]["evidence_items"] == calls[0]["evidence_items"]

    assert _statuses(result) == {
        STAFF: ("supported", "resolved", REFERENCE_REAUDIT_ADOPTED),
        MANAGER: ("supported", "resolved", REFERENCE_REAUDIT_ADOPTED),
    }
    assert result["counts"] == {"supported": 2}
    assert result["reference_reaudit"] == {"status": "completed", "target_passage_ids": ["A1", "A2"], "adopted_passage_ids": ["A1", "A2"]}
    staff = result["claim_checks"][0]
    assert staff["source_id"] == TABLE_CHUNK and staff["support_quote"] == VERBATIM[STAFF]


def test_reaudit_that_repeats_the_error_or_judges_otherwise_keeps_the_citation_error() -> None:
    """監査し直しでも写した原文が根拠に無い・否定の判定・呼び出しの失敗は、最初の citation_error のまま（#112）。"""
    first = {text: ("supported", TABLE, PARAPHRASED[text]) for text in (STAFF, MANAGER)}
    for reaudit in (
        first,
        {STAFF: ("unsupported", "", ""), MANAGER: ("contradicted", TABLE, VERBATIM[MANAGER])},
        RuntimeError("合成の失敗"),
    ):
        result = _validate(first, reaudit)
        assert _statuses(result) == {
            STAFF: ("citation_error", "quote_unmatched", REFERENCE_REAUDIT_REJECTED),
            MANAGER: ("citation_error", "quote_unmatched", REFERENCE_REAUDIT_REJECTED),
        }, reaudit
        # 最初の監査が写した原文（失敗の原因）を残す。
        assert [claim["support_quote"] for claim in result["claim_checks"]] == [PARAPHRASED[STAFF], PARAPHRASED[MANAGER]]
        assert result["reference_reaudit"]["adopted_passage_ids"] == []
        assert result["reference_reaudit"]["status"] == ("failed" if isinstance(reaudit, Exception) else "completed")
    # 片方だけ通れば、その段落だけ替える。
    result = _validate(first, {STAFF: ("supported", TABLE, VERBATIM[STAFF]), MANAGER: first[MANAGER]})
    assert _statuses(result) == {
        STAFF: ("supported", "resolved", REFERENCE_REAUDIT_ADOPTED),
        MANAGER: ("citation_error", "quote_unmatched", REFERENCE_REAUDIT_REJECTED),
    }


def test_file_names_returned_as_evidence_ids_are_reaudited() -> None:
    """監査がファイル名を根拠の ID として返し、写した原文でも根拠に決まらない supported（audit_id_invalid）も
    監査し直す（#112。写した原文が根拠にあれば、ファイル名の ID でも今までどおり結び付ける）。"""
    calls: list[dict] = []
    first = {STAFF: ("supported", "社内規程サンプル.pdf", PARAPHRASED[STAFF]), MANAGER: ("supported", TABLE, VERBATIM[MANAGER])}
    result = _validate(first, {STAFF: ("supported", TABLE, VERBATIM[STAFF])}, calls=calls)
    # ID を書かない段落なので、先に他の段落が引いた根拠で確かめ直し（#1412。ここでは決まらない）、その後に監査し直す。
    reaudit = calls[-1]
    assert len(calls) == 3 and "validation_feedback" not in calls[1]
    assert [item["text"] for item in reaudit["answer_passages"]] == [STAFF]
    assert "A1: audit_id_invalid（evidence_id: 社内規程サンプル.pdf、" in reaudit["validation_feedback"]
    assert _statuses(result) == {
        STAFF: ("supported", "resolved", REFERENCE_REAUDIT_ADOPTED),
        MANAGER: ("supported", "resolved", ""),
    }
    # 監査し直しても根拠に決まらない ID なら citation_error のまま。返した ID は audit_evidence_id に残す。
    result = _validate(first, {STAFF: first[STAFF]})
    staff = result["claim_checks"][0]
    assert (staff["status"], staff["reference_check"], staff["reference_reaudit"]) == (
        "citation_error",
        "audit_id_invalid",
        REFERENCE_REAUDIT_REJECTED,
    )
    assert staff["audit_evidence_id"] == "社内規程サンプル.pdf"


@pytest.mark.parametrize(
    "first",
    [
        # 否定の判定・図の判定・裏付けの判定は監査し直さない。
        {STAFF: ("unsupported", "", ""), MANAGER: ("contradicted", TABLE, PARAPHRASED[MANAGER])},
        {STAFF: ("supported", TABLE, VERBATIM[STAFF]), MANAGER: ("supported", TABLE, VERBATIM[MANAGER])},
        # 監査が自ら citation_error とした段落（監査の判定は supported ではない）も監査し直さない。
        {STAFF: ("citation_error", "社内規程サンプル.pdf", ""), MANAGER: ("supported", TABLE, VERBATIM[MANAGER])},
    ],
)
def test_passages_without_audit_reference_failures_are_not_reaudited(first) -> None:
    calls: list[dict] = []
    result = _validate(first, calls=calls)
    assert not any(str(call.get("validation_feedback") or "").startswith(REFERENCE_REAUDIT_FEEDBACK) for call in calls)
    assert result["reference_reaudit"]["status"] == "not_required"
    assert {claim["reference_reaudit"] for claim in result["claim_checks"]} == {""}


def test_ids_written_by_the_answer_that_are_not_in_the_evidence_are_not_reaudited() -> None:
    """回答が書いた ID が根拠に無い（answer_id_invalid。回答の引用の誤り）段落は監査し直さない（#112）。"""
    unknown = f"{DOCUMENT}:{CHUNK_SET}:9"
    staff = STAFF.replace("。", f"【証拠 ID {unknown}】。")
    answer = f"\n\n{staff}\n\n{MANAGER}"
    calls: list[dict] = []
    first = {staff: ("supported", unknown, VERBATIM[STAFF]), MANAGER: ("supported", TABLE, VERBATIM[MANAGER])}
    result = _validate(first, calls=calls, answer=answer, citations=[])
    assert len(calls) == 1
    claim = result["claim_checks"][0]
    assert (claim["status"], claim["reference_check"], claim["reference_reaudit"]) == ("citation_error", "answer_id_invalid", "")


def test_reaudit_is_not_called_after_the_deadline() -> None:
    """照合の時間の上限を過ぎていれば監査し直さない（最初の判定のまま。#112）。"""
    calls: list[dict] = []
    first = {text: ("supported", TABLE, PARAPHRASED[text]) for text in (STAFF, MANAGER)}
    result = _validate(first, calls=calls, deadline=Deadline(60.0))
    assert len(calls) == 1
    assert result["reference_reaudit"] == {"status": "deadline_exceeded", "target_passage_ids": ["A1", "A2"], "adopted_passage_ids": []}
    assert {claim["status"] for claim in result["claim_checks"]} == {"citation_error"}


def test_reference_reaudit_diagnostics_are_not_in_the_audit_schema() -> None:
    """監査が返した ID・監査し直しの結果はシステムが付ける診断で、監査モデルの schema に出さない（#112）。"""
    schema = json.dumps(ClaimAuditOutput.model_json_schema())
    assert "audit_evidence_id" not in schema and "reference_reaudit" not in schema
