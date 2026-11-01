"""回答の対応（outcome。#1305）の決定論テスト。

Run の step・最終の検証の成果物の内容・回答の段落から、RAG の AnswerEnvelope と同じ語彙の対応を
モデルを呼ばずに決めることを、業務支援の評価（#1289 の D）で取り違えた回答の形で確かめる。
最終の検証の内容は `combine_validations` で実際と同じ形に組み立てる。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.features.agent.answer_outcome import ANSWER_OUTCOMES, answer_outcome
from app.features.agent.answer_passages import (
    asks_environment_data,
    case_labels,
    passage_spans,
    supplement_passages,
)
from app.features.agent.answer_validation import (
    REASON_CLARIFICATION_ONLY,
    REASON_NO_RAG_EVIDENCE,
    STATUS_COMPLETED,
    STATUS_SKIPPED,
    STATUS_UNVALIDATED,
    combine_validations,
    validation_content,
)
from app.features.agent.runtime import RunStep, StepStatus
from app.features.agent.tools import ToolCall, ToolResult

EVIDENCE = [{"document_id": "doc-1", "chunk_id": "chunk-1"}]


def _step(name: str, output: dict[str, Any], *, success: bool = True) -> RunStep:
    return RunStep(
        run_id="run-1",
        status=StepStatus.COMPLETED if success else StepStatus.FAILED,
        tool_call=ToolCall(name=name, arguments={}),
        tool_result=ToolResult(name=name, success=success, output=output if success else None),
    )


def _search(outcome: str) -> RunStep:
    return _step("rag__rag_search", {"outcome": outcome, "evidence": EVIDENCE})


def _retrieve() -> RunStep:
    return _step("rag__rag_retrieve_evidence", {"evidence": EVIDENCE})


def _claim(quote: str, status: str) -> dict[str, Any]:
    return {"answer_quote": quote, "status": status, "reason": ""}


def _validated(answer: str, *claims: dict[str, Any], status: str = "completed") -> dict[str, Any]:
    """`rag_validate_answer` の判定から、最終の検証の成果物の内容を組み立てる。"""
    counts: dict[str, int] = {}
    for claim in claims:
        counts[claim["status"]] = counts.get(claim["status"], 0) + 1
    supported = counts.get("supported", 0)
    result = {
        "valid": status == "completed" and supported > 0 and len(claims) == supported,
        "status": status,
        "counts": counts,
        "claims": list(claims),
        "missing_evidence": [],
        "stale_evidence": [],
        "evidence_truncated": False,
    }
    content, _published = combine_validations(
        answer, [validation_content(STATUS_COMPLETED, result=result, evidence=EVIDENCE)]
    )
    return content


def _outcome(
    answer: str, steps: list[RunStep], validation: dict[str, Any] | None = None
) -> tuple[str, str]:
    content = answer_outcome(answer, steps=steps, validation=validation)
    assert content["schema_version"] == 1
    assert content["value"] in ANSWER_OUTCOMES
    return content["value"], content["basis"]


CLARIFICATION = "\n".join(
    [
        "権限の付与先によって手順が変わるため、確認させてください。",
        "付与先は、個別の利用者ですか、それともグループですか？",
    ]
)


def test_empty_answer_is_insufficient_evidence() -> None:
    assert _outcome("  \n", []) == ("insufficient_evidence", "empty_answer")


def test_clarification_question_without_rag_search_is_needs_clarification() -> None:
    # cr-grant-permission: 確認の質問として扱う（推定は「根拠不足」だった）。
    validation = validation_content(STATUS_SKIPPED, reason=REASON_CLARIFICATION_ONLY)
    content = answer_outcome(
        CLARIFICATION,
        steps=[_step("rag__rag_lookup_guides", {"guides": []})],
        validation=validation,
    )
    assert (content["value"], content["basis"]) == ("needs_clarification", "clarification_question")
    assert content["rag_outcome"] is None
    assert content["signals"] == ["question"]


def test_validation_without_readable_evidence_is_insufficient_evidence() -> None:
    """根拠を 1 件も読めない（no_evidence）と、本文は残すがすべての段落が未確認なので拒答
    （#2738。本文を置き換えない）。"""
    answer = "契約は 60 日前までに解約できます。"
    validation = _validated(answer, _claim(answer, "unsupported"), status="no_evidence")
    assert validation["withheld"]["all"] is False
    assert [item["code"] for item in validation["answer_warnings"]] == ["no_evidence"]
    content = answer_outcome(answer, steps=[_retrieve()], validation=validation)
    assert (content["value"], content["basis"], content["signals"]) == (
        "insufficient_evidence",
        "validation_nothing_confirmed",
        ["unverified_passages"],
    )


def test_answer_that_relies_on_rag_search_uses_its_outcome() -> None:
    answer = "契約の更新は 30 日前までに申し出ます。"
    validation = _validated(answer, _claim(answer, "supported"))
    content = answer_outcome(answer, steps=[_search("answered")], validation=validation)
    assert (content["value"], content["basis"], content["rag_outcome"]) == (
        "answered",
        "rag_search",
        "answered",
    )
    assert content["signals"] == []
    assert _outcome(answer, [_search("needs_human")]) == ("needs_human", "rag_search")
    # 最後に成功した rag_search の対応を使う（失敗した呼び出しは数えない）。
    steps = [_search("conditional"), _step("rag__rag_search", {}, success=False)]
    assert _outcome(answer, steps) == ("conditional", "rag_search")


def test_answered_rag_search_with_unverified_paragraphs_is_conditional() -> None:
    kept = "契約の更新は 30 日前までに申し出ます。"
    fee = "手数料として 5,000 円を支払います。"
    answer = f"{kept}\n{fee}"
    validation = _validated(answer, _claim(kept, "supported"), _claim(fee, "unsupported"))
    content = answer_outcome(answer, steps=[_search("answered")], validation=validation)
    assert (content["value"], content["basis"]) == ("conditional", "rag_search")
    assert content["signals"] == ["unverified_passages"]
    # 警告の印（規則の検査）だけなら answered のまま、記録の rule_warnings だけを付ける。
    rule = {**_claim(fee, "unsupported"), "support_check": "value_unbound"}
    validation = _validated(answer, _claim(kept, "supported"), rule)
    content = answer_outcome(answer, steps=[_search("answered")], validation=validation)
    assert (content["value"], content["signals"]) == ("answered", ["rule_warnings"])


def test_removed_contradiction_with_an_unbound_citation_is_a_gap() -> None:
    """RAG が根拠の ID を照合できず判定を変えた段落も、監査の元の判定が矛盾なら外して
    不足の印にする（#2622）。"""
    kept = "設定Bは毎日 0 時に実行します。"
    disputed = "設定Aは有効です。"
    answer = f"{kept}\n{disputed}"
    unbound = {**_claim(disputed, "citation_error"), "audit_status": "contradicted"}
    validation = _validated(answer, _claim(kept, "supported"), unbound)
    content = answer_outcome(answer, steps=[_search("answered")], validation=validation)
    assert (content["value"], content["signals"]) == ("conditional", ["contradicted_removed"])


def test_environment_data_is_answered_after_the_environment_tools() -> None:
    answer = "今のセッションの有効期限は 45 分です（NL2SQL の照会の結果）。"
    search = _search("needs_environment_data")
    query = _step("nl2sql__nl2sql_query", {"status": "succeeded", "rows": [[45]]})
    content = answer_outcome(answer, steps=[search, query])
    assert (content["value"], content["basis"]) == ("answered", "environment_tools")
    assert content["rag_outcome"] == "needs_environment_data"
    assert content["signals"] == ["environment_tools"]
    # 道具が無い・失敗したなら、現場のデータが要るまま。
    assert _outcome(answer, [search]) == ("needs_environment_data", "rag_search")
    failed = _step("nl2sql__nl2sql_query", {}, success=False)
    assert _outcome(answer, [search, failed]) == ("needs_environment_data", "rag_search")
    # rag_search より前に呼んだ道具は、確かめたことにしない。
    assert _outcome(answer, [query, search]) == ("needs_environment_data", "rag_search")


def test_answer_without_the_clarification_rag_asked_is_conditional() -> None:
    answer = "個別の利用者なら利用者の詳細画面、グループならグループの詳細画面で付与します。"
    assert _outcome(answer, [_search("needs_clarification")]) == ("conditional", "rag_search")


def test_evidence_gathered_after_rag_search_is_judged_from_the_answer() -> None:
    answer = "契約の更新は 30 日前までに申し出ます。"
    # rag_search の後に根拠を集め直した回答は、rag_search の対応に拠らない。
    content = answer_outcome(answer, steps=[_search("insufficient_evidence"), _retrieve()])
    assert (content["value"], content["basis"]) == ("answered", "answer_passages")
    assert content["rag_outcome"] == "insufficient_evidence"


def test_answer_with_an_absence_statement_is_conditional() -> None:
    # ed-current-session-timeout: 既定値を答え、今の値は資料で確かめられないと示した。
    answer = "セッションの有効期限の既定値は 30 分です。\n今の設定値は資料からは確認できません。"
    content = answer_outcome(answer, steps=[_retrieve()])
    assert (content["value"], content["basis"]) == ("conditional", "answer_passages")
    assert content["signals"] == ["absence"]


def test_absence_only_answer_is_insufficient_evidence() -> None:
    # km-license-fee: 資料に記載が無いことだけを述べた回答（推定は「回答」）。
    answer = "ライセンス費用は資料に記載がありません。"
    assert _outcome(answer, [_retrieve()]) == ("insufficient_evidence", "answer_passages")
    # 根拠で確かめられない助言だけが不足の文に添えてあっても、確かめた段落が無いので拒答
    # （助言は本文に残して印を付ける。#2738）。
    advice = "営業部に問い合わせると 1 日で分かります。"
    with_advice = f"{answer}\n{advice}"
    validation = _validated(with_advice, _claim(advice, "unsupported"))
    assert [item["text"] for item in validation["passage_marks"]] == [advice]
    assert _outcome(with_advice, [_retrieve()], validation) == (
        "insufficient_evidence",
        "validation_nothing_confirmed",
    )


def test_data_confirmation_paragraphs_need_environment_data() -> None:
    # ed-error-e1023-cause: 実データ（ログ）の確認を促すだけの回答。
    confirm = "認証ログで E1023 が出た時刻のトークンの状態を確認してください。"
    validation = _validated(confirm, _claim(confirm, "data_confirmation"))
    content = answer_outcome(confirm, steps=[_retrieve()], validation=validation)
    assert (content["value"], content["basis"]) == ("needs_environment_data", "answer_passages")
    assert content["signals"] == ["data_confirmation"]
    # 原因を資料で答え、見分け方として実データの確認を促すなら条件付き。
    cause = "E1023 はトークンの期限切れか、時刻のずれで起きます。"
    answer = f"{cause}\n{confirm}"
    validation = _validated(
        answer, _claim(cause, "supported"), _claim(confirm, "data_confirmation")
    )
    assert _outcome(answer, [_retrieve()], validation) == ("conditional", "answer_passages")


def test_guide_handoff_without_rag_search_is_needs_human() -> None:
    lookup = _step("rag__rag_lookup_guides", {"guides": [{"guide_id": "g", "decision": "handoff"}]})
    answer = "この操作は情報システム部の担当者が行います。担当者に依頼してください。"
    assert _outcome(answer, [lookup]) == ("needs_human", "guide_handoff")
    # rag_search に拠る回答は、rag_search の対応を使う。
    assert _outcome(answer, [lookup, _search("answered")]) == ("answered", "rag_search")


def test_environment_tools_without_rag_answer_the_question() -> None:
    query = _step("nl2sql__nl2sql_query", {"status": "succeeded"})
    content = answer_outcome("先月の売上は 100 件です。", steps=[query])
    assert (content["value"], content["basis"]) == ("answered", "environment_tools")


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ("契約の更新は 30 日前までに申し出ます。", "answered"),
        (
            "契約の更新は 30 日前までに申し出ます。\n\n"
            "**資料で確認できず除いた内容**\n\n- 違約金の額",
            "conditional",
        ),
        ("契約の更新は 30 日前までに申し出ます。\n更新の種類は年間ですか？", "conditional"),
    ],
    ids=["claims", "unverified-section", "question"],
)
def test_unvalidated_answer_is_judged_from_its_passages(answer: str, expected: str) -> None:
    validation = validation_content(STATUS_UNVALIDATED, reason=REASON_NO_RAG_EVIDENCE)
    assert _outcome(answer, [], validation) == (expected, "answer_passages")


# ---- #1317: 実環境の評価（#1305 の D）で外れた回答の形 ----------------------------------


def _verdicts(answer: str, statuses: dict[str, str]) -> list[dict[str, Any]]:
    """回答の段落ごとの判定（`statuses` に無い段落は supported）。"""
    return [
        _claim(text, statuses.get(text, "supported"))
        for line in answer.split("\n")
        for _start, _end, text in passage_spans(line)
    ]


TRIAL_EXPIRY_FACT = "検証用アカウントの有効期限は **最長 30日** です。"
TRIAL_EXPIRY_SECTION = (
    "- セクション: 「サンプル業務ポータル 運用手順書 第3版」 → **2. 検証用アカウントの登録**"
)
TRIAL_EXPIRY = "\n".join(
    [
        TRIAL_EXPIRY_FACT,
        "",
        "**根拠**  ",
        "- 文書名: *portal-operations-manual.pdf*  ",
        f"{TRIAL_EXPIRY_SECTION}  ",
        "- ページ: 1  ",
        "- 抜粋: 「…有効期限は最長 30 日です。」  ",
        "",
        "この記述に基づき、検証用アカウントは30日以内に期限切れとなります。",
    ]
)


def test_citation_location_lines_are_not_withheld_and_the_answer_is_answered() -> None:
    # da-trial-account-expiry: 出典の行（セクション・ページ）を unassessed として外し、
    # withheld_claims で conditional になっていた（期待は answered）。
    statuses = {
        "- 文書名: *portal-operations-manual.pdf*": "unassessed",
        TRIAL_EXPIRY_SECTION: "unassessed",
        "- ページ: 1": "unassessed",
    }
    validation = _validated(TRIAL_EXPIRY, *_verdicts(TRIAL_EXPIRY, statuses))
    assert (validation["passage_marks"], validation["removed_passages"]) == ([], [])
    marked = {
        item["answer_quote"]: item.get("non_claim") for item in validation["result"]["claims"]
    }
    assert marked[TRIAL_EXPIRY_SECTION] == "citation"
    assert marked["- ページ: 1"] == "citation"
    assert marked[TRIAL_EXPIRY_FACT] is None
    content = answer_outcome(TRIAL_EXPIRY, steps=[_retrieve()], validation=validation)
    assert (content["value"], content["basis"], content["signals"]) == (
        "answered",
        "answer_passages",
        [],
    )


def test_unverified_or_removed_paragraphs_are_a_gap() -> None:
    # 確かめが終わらなかった名前だけの項目（unassessed）には印を付けず、条件・不足を示したことに
    # しない。
    fact = "検証用アカウントの有効期限は最長 30 日です。"
    pending = "1. **対象の選択**"
    extension = "延長は 90 日までできます。"
    answer = f"{fact}\n{pending}\n{extension}"
    unassessed = _validated(
        answer,
        _claim(fact, "supported"),
        _claim(pending, "unassessed"),
        _claim(extension, "supported"),
    )
    assert unassessed["passage_marks"] == []
    content = answer_outcome(answer, steps=[_retrieve()], validation=unassessed)
    assert (content["value"], content["signals"]) == ("answered", [])
    # rag_search に拠る回答でも同じ（da-regional-report-export）。
    content = answer_outcome(answer, steps=[_search("answered")], validation=unassessed)
    assert (content["value"], content["basis"]) == ("answered", "rag_search")
    # 未確認の印（確かめが終わっていない主張を含む）・矛盾で外した段落は不足の印（#2738）。
    for status, signal in (
        ("unassessed", "unverified_passages"),
        ("unsupported", "unverified_passages"),
        ("contradicted", "contradicted_removed"),
    ):
        disputed = _validated(
            answer,
            _claim(fact, "supported"),
            _claim(pending, "unassessed"),
            _claim(extension, status),
        )
        content = answer_outcome(answer, steps=[_retrieve()], validation=disputed)
        assert (content["value"], content["signals"]) == ("conditional", [signal])


def test_answer_kept_with_only_unresolved_citations_is_insufficient_evidence() -> None:
    # #1391（br-document-portal-retention）: 出典を照合できない段落だけの回答は、本文を残して
    # 印を付ける。根拠で確かめた段落が 1 つも無いので、回答の対応は拒答（#2738）。
    fact = "ドキュメントポータルの機密区分は「社内限り」で、データは **3 年間** 保管します。"
    conclusion = "したがって、ドキュメントポータルのデータの保管期間は **3 年** です。"
    answer = f"{fact}\n\n{conclusion}"
    validation = _validated(
        answer, _claim(fact, "citation_error"), _claim(conclusion, "citation_error")
    )
    assert validation["withheld"] == {
        "removed": 0,
        "unverified": 2,
        "warnings": 0,
        "all": False,
        "listed": 2,
    }
    content = answer_outcome(answer, steps=[_retrieve()], validation=validation)
    assert (content["value"], content["basis"], content["signals"]) == (
        "insufficient_evidence",
        "validation_nothing_confirmed",
        ["unverified_passages"],
    )
    # 確かめた段落が 1 つでもあれば、条件付きの回答。
    validation = _validated(answer, _claim(fact, "supported"), _claim(conclusion, "citation_error"))
    content = answer_outcome(answer, steps=[_retrieve()], validation=validation)
    assert (content["value"], content["signals"]) == ("conditional", ["unverified_passages"])


def test_unassessed_claims_without_a_confirmed_paragraph_are_insufficient_evidence() -> None:
    # 不足の文と、確かめが終わらなかった助言だけの回答は、確かめた段落が無いので拒答のまま
    # （助言は本文に残して印を付ける。#2738）。
    absence = "パスワードの文字数要件は、資料に記載がありません。"
    advice = "現場の契約書や価格表など、別途確認できる情報が必要です。"
    answer = f"{absence}\n{advice}"
    validation = _validated(answer, _claim(absence, "supported"), _claim(advice, "unassessed"))
    assert validation["withheld"]["unverified"] == 1
    assert _outcome(answer, [_retrieve()], validation) == (
        "insufficient_evidence",
        "validation_nothing_confirmed",
    )


LICENSE_FEE_REFUSAL = "\n".join(
    [
        "**回答**  ",
        "現在参照できる資料（「サンプル業務ポータル 運用手順書 第3版」等）には、"
        "サンプル業務ポータルの年間ライセンス費用に関する記載は見当たりませんでした。",
        "",
        "**根拠**  ",
        "- rag__rag_retrieve_evidence で取得した証拠は、アクセス権限の付与手順や検証用アカウントの"
        "登録などの運用手順に関する内容であり、金額や費用に関する記述は含まれていませんでした。  ",
        "- rag__rag_lookup_guides の検索結果でも、今回の質問に該当する業務ガイドは"
        "返ってきませんでした。",
        "",
        "**結論**  ",
        "資料だけでは「サンプル業務ポータルの年間ライセンス費用」がいくらかは特定できません。"
        "現場の契約書や価格表など、別途確認できる情報が必要です。推測で金額を示すことはできません。",
    ]
)
PASSWORD_REFUSAL = (
    "現在参照できる資料（「サンプル業務ポータル 運用手順書」やパラメータ一覧等）には、"
    "パスワードの文字数要件に関する記載がありません。そのため、パスワードは何文字以上にすべきかに"
    "ついて根拠のある回答はできません。該当するパスワードポリシー文書をご確認ください。"
)


@pytest.mark.parametrize(
    ("answer", "statuses"),
    [
        (
            LICENSE_FEE_REFUSAL,
            {
                "- rag__rag_lookup_guides の検索結果でも、今回の質問に該当する業務ガイドは"
                "返ってきませんでした。": "unsupported",
                "現場の契約書や価格表など、別途確認できる情報が必要です。": "unassessed",
            },
        ),
        (PASSWORD_REFUSAL, {"該当するパスワードポリシー文書をご確認ください。": "unassessed"}),
    ],
    ids=["km-license-fee", "km-password-policy"],
)
def test_refusal_that_ends_with_cannot_answer_is_insufficient_evidence(
    answer: str, statuses: dict[str, str]
) -> None:
    # 「推測で金額を示すことはできません。」「…根拠のある回答はできません。」が主張として数えられ、
    # conditional になっていた（期待は insufficient_evidence）。
    validation = _validated(answer, *_verdicts(answer, statuses))
    content = answer_outcome(answer, steps=[_retrieve()], validation=validation)
    # 確かめられない段落は本文に残して印を付け（#2738）、確かめた主張が無いので拒答のまま。
    assert (content["value"], content["basis"]) == (
        "insufficient_evidence",
        "validation_nothing_confirmed",
    )
    assert content["signals"] == ["absence", "unverified_passages"]
    # rag_search の後に根拠を集め直した回答も同じ（km-password-policy）。
    steps = [_search("insufficient_evidence"), _retrieve()]
    assert answer_outcome(answer, steps=steps, validation=validation)["value"] == (
        "insufficient_evidence"
    )


def test_refusal_with_an_amount_is_still_a_claim() -> None:
    # 金額を含む文は拒答の文にしない（#1306 の安全側の規則）。
    answer = "年間ライセンス費用は 120,000 円のため、それ以上の金額はお示しできません。"
    assert _outcome(answer, [_retrieve()]) == ("answered", "answer_passages")


E1023_LOG = (
    "※どちらが原因かを判断するには、サーバーの認証ログ（`auth.log`）でエラー発生時刻の行を確認し、"
)
E1023_CAUSE = "\n".join(
    [
        "エラー **E-1023** の原因は次の 2 つのどちらかです（※画面表示だけではどちらかの判別は"
        "できません）【証拠1】  ",
        "",
        "1. **原因 A：認証トークンの期限切れ**  ",
        "2. **原因 B：操作対象に対する権限不足**  ",
        "",
        f"{E1023_LOG}  ",
        "- 「`token expired`」と記録されていれば原因 A、  ",
        "- 「`permission denied`」と記録されていれば原因 B　と判別できます【証拠3】。  ",
        "",
        "**出典**  ",
        "- `error‑e1023‑notes.pdf` 「障害対応メモ: エラー E-1023「処理を続行できません」」 – "
        "1. 考えられる原因（ページ 1）  ",
        "- 同上 – 2. 原因の見分け方（ページ 1）  ",
    ]
)


def test_paragraph_asking_to_check_logs_needs_environment_data_even_if_supported() -> None:
    # ed-error-e1023-cause: 資料が「認証ログを確かめる」と書いているので検証は supported にし、
    # answered になっていた（期待は needs_environment_data / conditional）。
    validation = _validated(E1023_CAUSE, *_verdicts(E1023_CAUSE, {}))
    checks = [
        item["answer_quote"]
        for item in validation["result"]["claims"]
        if item.get("environment_check")
    ]
    assert checks == [E1023_LOG]
    content = answer_outcome(E1023_CAUSE, steps=[_retrieve()], validation=validation)
    assert (content["value"], content["basis"], content["signals"]) == (
        "conditional",
        "answer_passages",
        ["data_confirmation"],
    )
    # 確認を求めるだけの回答は needs_environment_data。
    only = "認証ログ（auth.log）でエラー発生時刻の行を確認してください。"
    validation = _validated(only, _claim(only, "supported"))
    assert _outcome(only, [_retrieve()], validation) == (
        "needs_environment_data",
        "answer_passages",
    )
    # 検証が矛盾で外した確認の段落は、利用者に見えないので印にしない。
    fact = "E-1023 の原因は、認証トークンの期限切れか権限不足です。"
    answer = f"{fact}\n{only}"
    validation = _validated(answer, _claim(fact, "supported"), _claim(only, "contradicted"))
    assert answer_outcome(answer, steps=[_retrieve()], validation=validation)["signals"] == [
        "contradicted_removed"
    ]
    # 本文に残して印を付けた確認の段落は、利用者に見えるので実データの確認の印にする（#2738）。
    validation = _validated(answer, _claim(fact, "supported"), _claim(only, "unassessed"))
    assert answer_outcome(answer, steps=[_retrieve()], validation=validation)["signals"] == [
        "data_confirmation",
        "unverified_passages",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "該当するパスワードポリシー文書をご確認ください。",
        "追加の資料やシステム設定画面の確認が必要です。",
        "いったんログアウトし、ログインし直したことを確認してください。",
        "利用者の詳細画面を開き、「権限」タブの記録を確認してください。",
        "認証ログはどこで確認できますか？",
    ],
    ids=["document", "not-a-request", "login", "operation", "question"],
)
def test_environment_check_is_only_for_data_and_records(text: str) -> None:
    assert asks_environment_data(text) is False


# ---- #1317 の変更の後の実環境の D で、新たに外れた回答の形 ------------------------------

PASSWORD_REFUSAL_WITH_NOTE = "\n".join(
    [
        "**回答**  ",
        "パスワードの文字数要件について、提供された資料（「サンプル業務ポータル 運用手順書 "
        "第3版」）の中に該当する記載は見つかりませんでした。したがって、資料に基づいて正確な文字数要件を"
        "お伝えすることはできません。",
        "",
        "**根拠**  ",
        "- 取得した証拠は、主に「アクセス権限の付与」「検証用アカウントの登録」などの手順に関する"
        "内容であり、パスワードの文字数に関する情報は含まれていませんでした（取得した全証拠の抜粋を"
        "参照）。",
        "",
        "**結論**  ",
        "資料にパスワードの最低文字数に関する記載がなく、根拠がないため回答できません。"
        "必要であれば、別の関連ドキュメントやポリシーを確認してください。",
    ]
)


def test_refusal_with_other_wording_and_a_trailing_note_is_insufficient_evidence() -> None:
    # km-password-policy: 「お伝えすることはできません」と、文末の補足の括弧の付いた
    # 「…含まれていませんでした（…を参照）。」が主張として数えられた。
    advice = "必要であれば、別の関連ドキュメントやポリシーを確認してください。"
    validation = _validated(
        PASSWORD_REFUSAL_WITH_NOTE,
        *_verdicts(PASSWORD_REFUSAL_WITH_NOTE, {advice: "unassessed"}),
    )
    content = answer_outcome(PASSWORD_REFUSAL_WITH_NOTE, steps=[_retrieve()], validation=validation)
    # 確かめが終わらなかった助言は本文に残して印を付ける（#2738。確かめた主張が無いので拒答）。
    assert (content["value"], content["signals"]) == (
        "insufficient_evidence",
        ["absence", "unverified_passages"],
    )


def test_unknown_current_value_is_a_gap() -> None:
    # ed-current-session-timeout: 既定値を答え、今の値は「資料に記載がないため、**不明**です」と
    # 示した回答（answered になっていた）。
    answer = "\n".join(
        [
            "この資料では **session_timeout_minutes の既定値は 30 分、設定例は 60 分** と記載されて"
            "いますが、各環境ごとの現在の設定値は示されていません。",
            "",
            "現時点での実際の設定値は資料に記載がないため、**不明**です。",
        ]
    )
    validation = _validated(answer, *_verdicts(answer, {}))
    content = answer_outcome(answer, steps=[_retrieve()], validation=validation)
    assert (content["value"], content["signals"]) == ("conditional", ["absence"])


GRANT_BRANCHES = "\n".join(
    [
        "**アクセス権限の付与手順**",
        "",
        "1. **対象の選択**  ",
        "   - 権限付与は「個別の利用者」か「グループ」のいずれかで行います。",
        "2. **個別利用者に付与する場合**  ",
        "   - 「権限」タブを選択し、付与したい権限をチェックして **「付与」** ボタンを"
        "クリックする。",
        "3. **グループに付与する場合**  ",
        "   - 「権限」タブで付与したい権限を選び **「付与」** する。",
        "4. **検証用アカウントの場合**  ",
        "   - 検証用アカウントは個別に権限を付与し、グループへの付与は行いません。",
    ]
)


def test_answer_split_into_branches_without_asking_is_conditional() -> None:
    # cr-grant-permission: 付与先を確かめずに分岐ごとに答えた回答。分岐の見出しは unassessed で
    # 外れるだけなので、#1317 の前は withheld_claims で偶然 conditional になっていた。
    statuses = {
        "1. **対象の選択**": "unassessed",
        "2. **個別利用者に付与する場合**": "unassessed",
        "3. **グループに付与する場合**": "unassessed",
        "4. **検証用アカウントの場合**": "unassessed",
    }
    validation = _validated(GRANT_BRANCHES, *_verdicts(GRANT_BRANCHES, statuses))
    content = answer_outcome(GRANT_BRANCHES, steps=[_retrieve()], validation=validation)
    assert (content["value"], content["signals"]) == ("conditional", ["branches"])
    assert case_labels(GRANT_BRANCHES) == {
        "個別利用者に付与する場合",
        "グループに付与する場合",
        "検証用アカウントの場合",
    }
    # 分岐のラベルが 1 つだけなら、条件を添えた回答として answered のまま。
    single = "検証用アカウントは最長 30 日です。\n- 延長する場合: 新しく登録し直します。"
    assert case_labels(single) == {"延長する場合"}
    assert _outcome(single, [_retrieve()]) == ("answered", "answer_passages")


@pytest.mark.parametrize(
    "text",
    [
        "15 分を過ぎても反映されない場合は、サポート窓口へ起票してください。",
        "グループに付与する場合は部門長の承認が要ります。",
        "削除した場合",
    ],
    ids=["sentence", "claim", "too-short-context"],
)
def test_case_label_is_only_a_heading_or_a_leading_label(text: str) -> None:
    labels = case_labels(text)
    assert labels == ({"削除した場合"} if text == "削除した場合" else set())


def test_clarification_question_with_an_annotation_is_needs_clarification() -> None:
    # cr-grant-permission（2 回目の実行）: 質問の後ろの「【clarification: target】」の注記で、
    # 確認の質問と見なかった。
    answer = "権限の付与先は、個別の利用者ですか、グループですか？【clarification: target】"
    assert _outcome(answer, [_step("rag__rag_lookup_guides", {"guides": []})]) == (
        "needs_clarification",
        "clarification_question",
    )


def test_need_to_check_the_log_is_a_data_confirmation() -> None:
    # ed-error-e1023-cause（2 回目の実行）: 「認証ログ…の記述を確認する必要があります」
    # 「認証ログの確認が必要です」で結んだ回答。
    answer = "\n".join(
        [
            "エラー **E‑1023**（「処理を続行できません」）の原因は次の 2 つのいずれかです。",
            "",
            "1. **認証トークンの期限切れ**（原因 A）  ",
            "2. **操作対象に対する権限不足**（原因 B）  ",
            "",
            "※画面だけではどちらかは判断できず、サーバーの認証ログ（auth.log）で"
            "「`token expired`」か「`permission denied`」の記述を確認する必要があります。  ",
            "",
            "以上が、資料に基づくエラー E‑1023 の原因です。※具体的にどちらかを確定するには、"
            "認証ログの確認が必要です。",
        ]
    )
    validation = _validated(answer, *_verdicts(answer, {}))
    content = answer_outcome(answer, steps=[_retrieve()], validation=validation)
    assert (content["value"], content["signals"]) == ("conditional", ["data_confirmation"])


def test_enclosed_absence_and_a_stray_emphasis_mark_are_not_claims() -> None:
    # km-password-policy（2 回目の実行）: 「（根拠：検索結果に…記述は含まれていません）」と、
    # 句点の後に分かれた強調の閉じ「**」。
    answer = "\n".join(
        [
            "資料を検索しましたが、パスワードの文字数要件に関する記載は見つかりませんでした。  ",
            "**→ 資料で確かめられませんでした。**  ",
            "",
            "（根拠：検索結果にパスワード長に関する記述は含まれていません）",
        ]
    )
    validation = _validated(answer, *_verdicts(answer, {"**": "unassessed"}))
    assert validation["passage_marks"] == []
    content = answer_outcome(answer, steps=[_retrieve()], validation=validation)
    assert (content["value"], content["signals"]) == ("insufficient_evidence", ["absence"])


def test_a_claim_with_a_file_citation_is_still_a_claim() -> None:
    # cs-closing-day（2 回目の実行）: 句点ではなく「.」で終わる、出典を添えた主張の 1 文が
    # 出典の行と判定され、主張が無い（拒答）になった。
    answer = (
        "地域別集計表の締め日は **毎月 10 日** です（第1版の毎月 5 日から変更されています）"
        "【regional-report-guide-v2.pdf, 「地域別集計表 出力手順 第2版」 > 「1. 締め日」】."
    )
    validation = _validated(answer, _claim(answer, "supported"))
    assert validation["result"]["claims"][0].get("non_claim") is None
    assert _outcome(answer, [_retrieve()], validation) == ("answered", "answer_passages")


# ---- 業務ガイドの条件が分からないまま答えた（#1321・#1322） ---------------------------------


def _lookup(decision: str, *, unknown: list[dict[str, Any]] | None = None) -> RunStep:
    return _step(
        "rag__rag_lookup_guides",
        {
            "guides": [
                {
                    "guide_id": "g-1",
                    "revision": 1,
                    "title": "アカウントの削除",
                    "decision": decision,
                    "known_conditions": [],
                    "unknown_conditions": unknown or [],
                }
            ]
        },
    )


_APPROVAL_UNKNOWN = [{"id": "approved", "label": "部門長の承認", "handling": "ask"}]
_TARGET_UNKNOWN = [{"id": "target", "label": "付与先", "handling": "ask"}]

# #1317 の実環境の D（run3）の cr-delete-account の回答（承認を前提の条件として答えた）。
_DELETE_ANSWER = "\n".join(
    [
        "不要になったアカウントは **削除して構いません**，ただし **所属部署の部門長の承認を"
        "得た場合のみ** 行ってください。",
        "",
        "- 「削除したアカウントは 30 日後に完全に消去され、復元できません。",
        "削除の前に、所属部署の部門長の承認を得てください。",
        "承認の無い削除は行いません」【portal‑operations‑manual.pdf, 6. アカウントの削除, p.2】",
        "",
        "したがって、部門長の承認があることを確認したうえで削除手順を実施してください。"
        "承認が得られない場合は削除は行わないでください。",
    ]
)

# #1317 の実環境の D（run3）の cr-grant-permission の回答（付与先を確かめずに両方の分岐で答えた。
# 見出しに「…の場合」が無いので branches の印は付かない）。
_GRANT_ANSWER = "\n".join(
    [
        "**アクセス権限の付与手順**",
        "",
        "1. **個別の利用者へ付与**",
        "   - 利用者の詳細画面を開き、**「権限」タブ**で付与したい権限を選択し、"
        "**「付与」** ボタンを押す。",
        "",
        "2. **グループへ付与**",
        "   - グループの詳細画面を開き、**「権限」タブ**で同様に権限を選択し付与する。",
        "   - グループに付与した権限はそのグループに所属する全員に適用される。",
        "   - 影響範囲が広いため、**付与前に部門長の承認**を得ること。",
        "",
        "> **注意**：権限が反映されるまで最大 **15 分** かかることがあります。",
    ]
)


def test_answer_on_an_unconfirmed_guide_condition_is_conditional() -> None:
    # 業務ガイドが承認の有無を確かめると判断したのに、承認を前提にして答えた（cr-delete-account）。
    content = answer_outcome(
        _DELETE_ANSWER,
        steps=[_retrieve(), _lookup("clarify", unknown=_APPROVAL_UNKNOWN)],
        validation=None,
    )
    assert (content["value"], content["basis"]) == ("conditional", "answer_passages")
    assert "guide_conditions" in content["signals"]
    # 条件ごとに答えると判断した業務ガイドも同じ。
    branch = answer_outcome(
        _DELETE_ANSWER,
        steps=[_lookup("branch", unknown=_APPROVAL_UNKNOWN), _retrieve()],
        validation=None,
    )
    assert branch["value"] == "conditional" and "guide_conditions" in branch["signals"]


def test_answer_split_into_branches_after_the_guide_asked_is_conditional() -> None:
    # 確かめずにすべての分岐を答えた（cr-grant-permission。見出しに「場合」が無い）。
    assert not case_labels(_GRANT_ANSWER)
    content = answer_outcome(
        _GRANT_ANSWER,
        steps=[_retrieve(), _lookup("clarify", unknown=_TARGET_UNKNOWN)],
        validation=None,
    )
    assert content["value"] == "conditional"
    assert "guide_conditions" in content["signals"] and "branches" not in content["signals"]
    # 業務ガイドを照合していなければ、今までどおり answered（決定的な手がかりが無い）。
    assert _outcome(_GRANT_ANSWER, [_retrieve()]) == ("answered", "answer_passages")


def test_answer_without_guide_conditions_stays_answered() -> None:
    # 条件が分かって（answer）答えた・業務ガイドが当たらない回答は answered のまま。
    for steps in (
        [_retrieve(), _lookup("answer")],
        [_retrieve(), _step("rag__rag_lookup_guides", {"guides": []})],
        [_retrieve()],
    ):
        content = answer_outcome(_DELETE_ANSWER, steps=steps, validation=None)
        assert content["value"] == "answered", steps
        assert "guide_conditions" not in content["signals"]


def test_clarification_after_the_guide_asked_is_needs_clarification() -> None:
    question = "所属部署の部門長の承認は得ていますか？（はい／いいえ）"
    content = answer_outcome(
        question, steps=[_retrieve(), _lookup("clarify", unknown=_APPROVAL_UNKNOWN)]
    )
    assert (content["value"], content["basis"]) == ("needs_clarification", "clarification_question")


def test_clarification_with_an_options_line_is_needs_clarification() -> None:
    # #1320 の実環境の D の cr-grant-permission の回答（問いの後に「選択肢: …」の行）。
    question = (
        "権限の付与先は、個別の利用者ですか、グループですか？  \n選択肢: **個別**、**グループ**"
    )
    content = answer_outcome(
        question, steps=[_retrieve(), _lookup("clarify", unknown=_TARGET_UNKNOWN)]
    )
    assert (content["value"], content["basis"]) == ("needs_clarification", "clarification_question")
    # 回答を求める依頼を添えた確認の質問（#1320 の実環境の D の run2 の cr-grant-permission）。
    asked = (
        "権限の付与先は、個別の利用者ですか、グループですか？  \n\n- 個別  \n- グループ\n\n"
        "ご回答をお願いします。"
    )
    assert answer_outcome(asked, steps=[_retrieve()])["value"] == "needs_clarification"
    # 選んでもらう依頼を添えた確認の質問（同じく run3 の cr-grant-permission）。
    choose = (
        "権限の付与先はどちらですか？\n\n- 個別の利用者  \n- グループ  \n\n"
        "ご希望の方をお選びください。"
    )
    assert answer_outcome(choose, steps=[_retrieve()])["value"] == "needs_clarification"
    # 画面の操作の指示は主張のまま。
    operation = (
        "権限の付与先はどちらですか？\n権限タブで付与する権限を選び、「付与」を押してください。"
    )
    assert answer_outcome(operation, steps=[_retrieve()])["value"] != "needs_clarification"
    # 選択肢の行でも、文・操作の指示は主張のまま。
    for text in (
        "権限の付与先は、個別の利用者ですか？\n選択肢: 個別を選んで「付与」を押してください。",
        "権限の付与先は、個別の利用者ですか？\n候補: 個別には詳細画面の権限タブで付与します。",
    ):
        assert answer_outcome(text, steps=[_retrieve()])["value"] != "needs_clarification", text


def test_guide_decision_before_a_later_rag_search_is_not_used() -> None:
    # 照合の後に条件を渡して rag_search で答えたら、rag_search の対応で決める。
    answer = "承認を得たアカウントは、詳細画面の「削除」から削除します。"
    content = answer_outcome(
        answer,
        steps=[_lookup("clarify", unknown=_APPROVAL_UNKNOWN), _search("answered")],
        validation=None,
    )
    assert (content["value"], content["basis"]) == ("answered", "rag_search")
    assert "guide_conditions" not in content["signals"]


# ---- 利用者に聞けば分かる条件は現場のデータではない（#1453） ------------------------------

# #1362 の deepseek-flash の全量の D の業務支援 mt-grant-permission-individual の 2 回目
# （run_b006caa651204fcfaeea40c1b5f1d2e0）の回答の形。手順を答え、利用者のロールを確かめる。
GRANT_WITH_ROLE_CHECK = "\n".join(
    [
        "個別の利用者への付与手順は、次のとおりです。",
        "",
        "**手順（個別の利用者に付与する場合）**",
        "1. 利用者の詳細画面の「権限」タブを開きます。",
        "2. 付与する権限を選び、「付与」を押します。",
        "",
        "**ご確認いただきたい点**",
        "- 操作を行う方が「ポータル管理者」ロールを持っているかどうかを、画面でご確認ください。"
        "ロールをお持ちでない場合は、所属部署のポータル管理者にご依頼ください。",
    ]
)


@pytest.mark.parametrize(
    "text",
    [
        "操作を行う方が「ポータル管理者」ロールを持っているかどうかを、画面でご確認ください。",
        "**実データ**：対象システムの「重要度」列の実際の値をご確認ください。",
        "削除の前に、所属部署の部門長の承認を得たかの現在の状態をご確認ください。",
    ],
    ids=["role", "target-system", "approval"],
)
def test_user_condition_checks_are_not_environment_data(text: str) -> None:
    assert asks_environment_data(text) is False


def test_record_checks_about_a_target_are_still_environment_data() -> None:
    # 対象のシステムを名指ししても、ログ・履歴そのものを確かめるなら現場のデータ。
    assert asks_environment_data(
        "対象システムの認証ログ（auth.log）でエラーの時刻の行を確認してください。"
    )
    assert asks_environment_data("対象の利用者の権限の付与の履歴を確認してください。")


def test_answer_that_asks_the_users_role_is_not_environment_data() -> None:
    # RAG が利用者に確かめる条件だけで conditional を返したら、その対応を使う。
    assert _outcome(GRANT_WITH_ROLE_CHECK, [_search("conditional")]) == (
        "conditional",
        "rag_search",
    )
    # Agent が自分で組み立てた回答でも、ロールの確認は現場の確認の印にしない。
    content = answer_outcome(GRANT_WITH_ROLE_CHECK, steps=[_retrieve()])
    assert content["value"] == "answered"
    assert "data_confirmation" not in content["signals"]


# ---- #1475: 結論の段落で答えた後の、補足の段落の不足の文 ------------------------------

PARAMETER_DEFAULT_FACT = "session_timeout_minutes の既定値は **30**（分）です。"
PARAMETER_DEFAULT_SUPPLEMENT_ABSENCE = "現在の各環境の設定値は資料からは分かりません。"
# da-parameter-default（e1467、run_c91bafebe4794ecfb266ee6be336968b）の回答。
PARAMETER_DEFAULT = "\n".join(
    [
        f"{PARAMETER_DEFAULT_FACT}説明は「操作が無いときにログインが切れるまでの分数」とされています。",
        "",
        "出典：portal-parameters.xlsx シート「パラメータ一覧」の当該行"
        "（A4:D4、パラメータ名 session_timeout_minutes）",
        "",
        "補足（資料の記載）",
        "- 同表には設定例として 60 も示されていますが、これは既定値ではなく設定の書き方の例です"
        "（同シート A6:D6 など並びの行も同じ構成）。",
        "- 同シートの注記（A1:A2）に「既定値は出荷時の値、設定例は設定の書き方の例で、どちらも"
        "各環境の現在の設定値ではありません」とあり、この 30 は現在の環境の設定値ではありません。"
        f"{PARAMETER_DEFAULT_SUPPLEMENT_ABSENCE}",
    ]
)


def test_supplement_passages_are_the_supplement_section_and_lead_paragraphs() -> None:
    supplement = supplement_passages(PARAMETER_DEFAULT)
    assert PARAMETER_DEFAULT_SUPPLEMENT_ABSENCE in supplement
    assert PARAMETER_DEFAULT_FACT not in supplement
    assert "補足（資料の記載）" not in supplement
    # 「なお、」「補足：」で始まる段落と、太字の見出し「**注記**」の節。
    answer = "\n".join(
        [
            "既定値は 30 分です。",
            "なお、現在の設定値は資料からは分かりません。",
            "補足：設定例は 60 分です。",
            "",
            "**注記**",
            "- 出荷時の値です。",
            "",
            "設定の変更は管理画面で行います。",
        ]
    )
    assert supplement_passages(answer) == {
        "なお、現在の設定値は資料からは分かりません。",
        "補足：設定例は 60 分です。",
        "- 出荷時の値です。",
    }
    # 補足の外にも同じ原文の段落があれば、補足として扱わない。
    repeated = "現在の値は資料からは分かりません。\n\n補足\n- 現在の値は資料からは分かりません。"
    assert supplement_passages(repeated) == {"- 現在の値は資料からは分かりません。"}
    same = "現在の値は資料からは分かりません。\n\n補足\n現在の値は資料からは分かりません。"
    assert supplement_passages(same) == set()


@pytest.mark.parametrize(
    "steps", [[_search("answered")], [_retrieve()]], ids=["rag_search", "answer_passages"]
)
def test_absence_only_in_the_supplement_after_an_answer_stays_answered(
    steps: list[RunStep],
) -> None:
    # da-parameter-default: 既定値を答え（supported）、補足に「現在の各環境の設定値は資料からは
    # 分かりません。」と書いた回答が、absence の印で conditional になっていた（期待は answered）。
    validation = _validated(PARAMETER_DEFAULT, *_verdicts(PARAMETER_DEFAULT, {}))
    content = answer_outcome(PARAMETER_DEFAULT, steps=steps, validation=validation)
    assert (content["value"], content["signals"]) == ("answered", ["supplement_absence"])


def test_absence_after_nao_following_a_supported_answer_stays_answered() -> None:
    answer = "既定値は 30 分です。\nなお、現在の各環境の設定値は資料からは分かりません。"
    validation = _validated(answer, *_verdicts(answer, {}))
    content = answer_outcome(answer, steps=[_search("answered")], validation=validation)
    assert (content["value"], content["signals"]) == ("answered", ["supplement_absence"])


def test_supplement_absence_without_a_supported_conclusion_is_still_a_gap() -> None:
    # 最終の検証が判定を出さなかった回答は変えない。
    content = answer_outcome(PARAMETER_DEFAULT, steps=[_search("answered")])
    assert (content["value"], content["signals"]) == ("conditional", ["absence"])
    # 結論の段落を根拠で確かめられなかった（外した）回答も変えない。
    fact_line = PARAMETER_DEFAULT.split("\n")[0]
    statuses = {text: "unsupported" for _start, _end, text in passage_spans(fact_line)}
    validation = _validated(PARAMETER_DEFAULT, *_verdicts(PARAMETER_DEFAULT, statuses))
    content = answer_outcome(PARAMETER_DEFAULT, steps=[_retrieve()], validation=validation)
    assert content["value"] == "conditional"
    assert "absence" in content["signals"]
    assert "supplement_absence" not in content["signals"]


def test_absence_in_the_conclusion_is_still_a_gap_even_with_a_supplement() -> None:
    # 資料に答えが無いことを結論にした回答（#1454）は、補足があっても今までどおり。
    refusal = "\n".join(
        [
            "session_timeout_minutes の現在の設定値は資料からは分かりません。",
            "",
            "補足",
            "- 既定値は 30 分です。",
            "- 現在の各環境の設定値は資料からは分かりません。",
        ]
    )
    validation = _validated(refusal, *_verdicts(refusal, {}))
    content = answer_outcome(refusal, steps=[_retrieve()], validation=validation)
    assert (content["value"], content["signals"]) == ("conditional", ["absence"])
    searched = answer_outcome(
        refusal, steps=[_search("insufficient_evidence")], validation=validation
    )
    assert searched["value"] == "insufficient_evidence"
    # 拒答だけの回答の後の補足に不足の文があっても拒答のまま。
    only = "\n".join(
        [
            "ライセンス費用は資料に記載がありません。",
            "",
            "補足",
            "- 見積もりの窓口も資料からは分かりません。",
        ]
    )
    validation = _validated(only, *_verdicts(only, {}))
    content = answer_outcome(only, steps=[_retrieve()], validation=validation)
    assert (content["value"], content["signals"]) == ("insufficient_evidence", ["absence"])
    # 補足の節は、空行の後の箇条書きではない行で終わる（その行の不足の文は結論の側）。
    ended = "\n".join(
        [
            "既定値は 30 分です。",
            "",
            "補足",
            "- 設定例は 60 分です。",
            "",
            "現在の設定値は資料からは分かりません。",
        ]
    )
    validation = _validated(ended, *_verdicts(ended, {}))
    content = answer_outcome(ended, steps=[_retrieve()], validation=validation)
    assert (content["value"], content["signals"]) == ("conditional", ["absence"])


ABSENCE_ANSWER = "ご質問の手続きの期限は、検索した資料には記載がありませんでした。"


@pytest.mark.parametrize(
    ("search_output", "basis"),
    [
        ({}, "absence_conclusion"),
        # 補助の処理の縮退で 0 件（読めなかった可能性がある）。資料に無いとは結論しない。
        ({"degraded_steps": [{"kind": "reference_load", "count": 1}]}, "answer_passages"),
    ],
    ids=["no-hit", "degraded"],
)
def test_absence_conclusion_does_not_count_a_degraded_zero_evidence_search(
    search_output: dict[str, Any], basis: str
) -> None:
    """縮退で 0 件の rag_search は、答えなしの根拠に数えない（#3546）。"""
    steps = [
        _step(
            "rag__rag_search",
            {"outcome": "insufficient_evidence", "evidence": [], **search_output},
        ),
        _step("rag__rag_retrieve_evidence", {"evidence": []}),
    ]
    content = answer_outcome(ABSENCE_ANSWER, steps=steps)
    assert (content["value"], content["basis"]) == ("insufficient_evidence", basis)
    assert ("rag_degraded" in content["signals"]) is (basis != "absence_conclusion")


def test_record_signals_do_not_change_the_outcome_value() -> None:
    """縮退した検索・表の対応の検査が読めない根拠での裏付けは、記録だけのシグナル（#3546）。"""
    answer = "申請の期限は 30 日です。"
    table_evidence = [
        {"document_id": "doc-1", "chunk_id": "chunk-1", "table_binding": "not_checkable"}
    ]
    steps = [
        _step(
            "rag__rag_search",
            {
                "outcome": "answered",
                "evidence": table_evidence,
                "degraded_steps": [{"kind": "rerank", "count": 1}],
            },
        )
    ]
    validation = _validated(answer, {**_claim(answer, "supported"), "chunk_id": "chunk-1"})
    content = answer_outcome(answer, steps=steps, validation=validation)
    assert (content["value"], content["basis"]) == ("answered", "rag_search")
    assert content["signals"] == ["rag_degraded", "table_binding_unchecked"]
    # 検査が読める根拠・裏付けに使っていない根拠では付けない。
    checkable = [{**table_evidence[0], "table_binding": "checkable"}]
    content = answer_outcome(
        answer,
        steps=[_step("rag__rag_search", {"outcome": "answered", "evidence": checkable})],
        validation=validation,
    )
    assert content["signals"] == []
