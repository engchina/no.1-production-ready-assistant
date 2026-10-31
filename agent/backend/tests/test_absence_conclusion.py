"""資料に答えが無いと結論した回答の対応（#1454）の決定論テスト。

#1362 の deepseek-flash の全量の評価の業務支援で、知識の無い 3 問（`km-*`。期待は
insufficient_evidence / needs_human）の回答は冒頭で「資料から確認できませんでした」と言い、推測で
補っていないのに、調べた資料の一覧・近い規定の紹介が主張の段落として残り、外した段落（作業の経過。
#1452）もあって conditional になっていた。回答はその Run の回答（最終の検証で外した段落を戻し、
「資料で確認できず除いた内容」の節を除いた本文）。steps は Run のツールの順（rag_search の
outcome は
insufficient_evidence）。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.features.agent.answer_outcome import answer_outcome
from app.features.agent.answer_passages import is_absence_conclusion
from app.features.agent.answer_validation import (
    STATUS_COMPLETED,
    combine_validations,
    validation_content,
)
from app.features.agent.runtime import RunStep, StepStatus
from app.features.agent.tools import ToolCall, ToolResult

EVIDENCE = [{"document_id": "doc-1", "chunk_id": "chunk-1"}]

# km-license-fee（run_9e4b65413a47492a9a3781fad26e4a2b）
LICENSE_FEE = "\n".join(
    [
        "ご質問の「サンプル業務ポータルの年間ライセンス費用」については、"
        "**資料から確認できませんでした**。",
        "",
        "## 調べた結果",
        "- 検索（`rag_search`）の結果は `insufficient_evidence` で、回答に使える根拠が"
        "取得できませんでした。",
        "- 検索結果では `needs_human_review: true`（人による確認が必要）と示されています。",
        "- 言い換え（「ライセンス費用」「利用料 年間」「費用 一覧」「年間ライセンス 費用 契約」）"
        "でも、費用・料金に関する記述は見つかりませんでした。",
        "",
        "## 検索範囲に存在した資料（いずれも費用の記載なし）",
        "- 「サンプル業務ポータル 運用手順書 第3版」（portal-operations-manual.pdf）… 前提、"
        "検証用アカウント登録、権限付与、通知設定、アカウント削除",
        "- 「パラメータ一覧」（portal-parameters.xlsx）… パラメータの既定値・設定例のみ",
        "",
        "## 補足",
        "- 根拠が無いため、金額を推測で補うことはできません。",
        "費用の情報は、契約・経理の担当窓口など、この検索範囲外の資料・窓口でご確認ください。",
    ]
)
LICENSE_FEE_WITHHELD = [
    "- 検索（`rag_search`）の結果は `insufficient_evidence` で、回答に使える根拠が"
    "取得できませんでした。",
    "- 検索結果では `needs_human_review: true`（人による確認が必要）と示されています。",
]
# km-password-policy（run_788ffac31aca43888c62f1049df7c473）
PASSWORD_POLICY = "\n".join(
    [
        "ご質問の「パスワードの文字数（最小桁数）」について、確認できる根拠を検索しましたが、"
        "**資料の中に該当する記載を見つけられませんでした**。",
        "",
        "## 調べた結果",
        "",
        "- **rag_search（質問全体）**: outcome は `insufficient_evidence`"
        "（回答に使える文書の根拠が取得できず）。",
        "- **パラメータ一覧（portal-parameters.xlsx、シート「パラメータ一覧」）**: 文書全体を"
        "確認しましたが、載っているパラメータは次の 4 件のみで、パスワードの文字数に関する項目は"
        "ありません。",
        "  - `session_timeout_minutes`（既定値 30 / 設定例 60、操作が無いときにログインが切れる"
        "までの分数）",
        "  - `max_upload_mb`（既定値 20 / 設定例 100）",
        "",
        "## 結論",
        "",
        "いただいた検索範囲の資料（パラメータ一覧・運用手順書・障害対応メモ）には、"
        "**パスワードの最小文字数を定めた記載は確認できませんでした**。",
    ]
)
PASSWORD_POLICY_WITHHELD = [
    "- **rag_search（質問全体）**: outcome は `insufficient_evidence`"
    "（回答に使える文書の根拠が取得できず）。",
]
# km-auto-disable（run_43a85f4f83ee45c4a0843d501845cb4d）
AUTO_DISABLE = "\n".join(
    [
        "ご質問の「退職者のアカウントを自動で無効にする設定」について、資料を確認しましたが、"
        "該当する設定・手順は見つかりませんでした（推測では補いません）。",
        "",
        "## 確認した資料と結果",
        "",
        "**1. サンプル業務ポータル 運用手順書 第3版（portal-operations-manual.pdf）**",
        "- 文書構成は全7節で、内容は次のとおりです（`rag_outline` で確認）。",
        "- アカウントに関する記載は「6. アカウントの削除」（2頁）で、内容は「削除したアカウントは"
        "30日後に完全に消去され、復元できません。削除の前に、所属部署の部門長の承認を得て"
        "ください」という**手動削除＋部門長承認**のルールです。",
        "",
        "## 結論",
        "- なお、資料に近いものとして**アカウントの削除**の規定はありますが、これは自動無効化では"
        "なく、部門長の承認を得て行う手動の削除です"
        "（運用手順書 第3版「6. アカウントの削除」2頁）。",
    ]
)
AUTO_DISABLE_WITHHELD = ["- 文書構成は全7節で、内容は次のとおりです（`rag_outline` で確認）。"]

KNOWLEDGE_MISSING = {
    "km-license-fee": (
        LICENSE_FEE,
        LICENSE_FEE_WITHHELD,
        ["rag_search", *["rag_retrieve_evidence"] * 4],
    ),
    "km-password-policy": (
        PASSWORD_POLICY,
        PASSWORD_POLICY_WITHHELD,
        ["rag_search", "rag_retrieve_evidence", "rag_read_document", "rag_retrieve_evidence"],
    ),
    "km-auto-disable": (
        AUTO_DISABLE,
        AUTO_DISABLE_WITHHELD,
        [
            "rag_lookup_guides",
            "rag_search",
            "rag_retrieve_evidence",
            "rag_retrieve_evidence",
            "rag_outline",
            "rag_read_document",
        ],
    ),
}


def _step(tool: str, output: dict[str, Any]) -> RunStep:
    name = f"rag__{tool}"
    return RunStep(
        run_id="run-1454",
        status=StepStatus.COMPLETED,
        tool_call=ToolCall(name=name, arguments={}),
        tool_result=ToolResult(name=name, success=True, output=output),
    )


def _steps(tools: list[str], search_outcome: str = "insufficient_evidence") -> list[RunStep]:
    outputs: dict[str, dict[str, Any]] = {
        "rag_search": {"outcome": search_outcome, "evidence": EVIDENCE},
        "rag_retrieve_evidence": {"evidence": EVIDENCE},
        "rag_read_document": {"document_id": "doc-1", "chunks": [{"chunk_id": "chunk-1"}]},
        "rag_lookup_guides": {"guides": []},
        "rag_outline": {"document_id": "doc-1"},
    }
    return [_step(tool, outputs[tool]) for tool in tools]


def _validated(answer: str, withheld: list[str]) -> dict[str, Any]:
    """根拠で確かめられない段落（unsupported。本文に残して印を付ける。#2738）がある最終の検証の
    内容（`combine_validations` で組み立てる）。"""
    claims = [{"answer_quote": quote, "status": "unsupported", "reason": ""} for quote in withheld]
    result = {
        "valid": False,
        "status": "completed",
        "counts": {"unsupported": len(claims)},
        "claims": claims,
        "missing_evidence": [],
        "stale_evidence": [],
        "evidence_truncated": False,
    }
    content, _published = combine_validations(
        answer, [validation_content(STATUS_COMPLETED, result=result, evidence=EVIDENCE)]
    )
    return content


@pytest.mark.parametrize(
    ("answer", "withheld", "tools"), KNOWLEDGE_MISSING.values(), ids=KNOWLEDGE_MISSING.keys()
)
def test_answer_concluding_the_target_is_not_in_the_documents_is_insufficient_evidence(
    answer: str, withheld: list[str], tools: list[str]
) -> None:
    validation = _validated(answer, withheld)
    assert [item["text"] for item in validation["passage_marks"]] == withheld

    content = answer_outcome(answer, steps=_steps(tools), validation=validation)

    assert (content["value"], content["basis"]) == ("insufficient_evidence", "absence_conclusion")
    assert content["rag_outcome"] == "insufficient_evidence"


def test_absence_conclusion_needs_an_insufficient_rag_search() -> None:
    """rag_search が答えた（answered）なら、冒頭の不足の文だけで答えなしにしない。"""
    answer, withheld, tools = KNOWLEDGE_MISSING["km-auto-disable"]
    content = answer_outcome(
        answer, steps=_steps(tools, "answered"), validation=_validated(answer, withheld)
    )
    assert content["value"] == "conditional"


def test_answer_whose_conclusion_is_a_fact_is_not_insufficient_evidence() -> None:
    """冒頭が資料の事実の回答は、rag_search が insufficient_evidence でも答えなしにしない。"""
    answer = "\n".join(
        [
            "検証用アカウントの有効期限は最長 30 日です。",
            "- なお、年間の費用は資料から確認できませんでした。",
        ]
    )
    content = answer_outcome(answer, steps=_steps(["rag_search", "rag_retrieve_evidence"]))
    assert (content["value"], content["basis"]) == ("conditional", "answer_passages")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (LICENSE_FEE.split("\n")[0], True),
        (PASSWORD_POLICY.split("\n")[0], True),
        (AUTO_DISABLE.split("\n")[0], True),
        # 調べた節の後に事実の主張が続く。
        ("資料を確認しましたが、承認者は部長です。", False),
        # 数量を含む。
        ("資料を確認しましたが、30 日を超える設定の記載はありません。", False),
    ],
    ids=["license-fee", "password-policy", "auto-disable", "fact-after-lead", "quantity"],
)
def test_absence_conclusion_passages(text: str, expected: bool) -> None:
    assert is_absence_conclusion(text) is expected
