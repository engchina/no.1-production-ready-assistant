"""前置き・答え方の案内つきの確認の質問（#1454）の決定論テスト。

#1362 の deepseek-flash の全量の評価で、`rag_lookup_guides` だけを呼んで確認の質問を返した回答に、
前置きの文（「その点がまだ分かっていません。」）と答え方の案内の括弧が付き、確認の質問だけの回答と
認められずに「この回答は資料の根拠を使っておらず…」の注記が足されていた。回答はその Run の
回答そのもの（注記を足す前の本文）。
"""

from __future__ import annotations

import pytest

from app.features.agent.answer_outcome import answer_outcome
from app.features.agent.answer_passages import (
    asks_for_request,
    greets,
    is_clarification_only,
    is_greeting_only,
    non_claim_passages,
)
from app.features.agent.answer_validation import (
    REASON_CLARIFICATION_ONLY,
    STATUS_SKIPPED,
    validation_content,
)
from app.features.agent.runtime import RunStep, StepStatus
from app.features.agent.tools import ToolCall, ToolResult

# cr-grant-permission（run_c70c4b03d01e416291a367c361e91ff6）
GRANT_PERMISSION = (
    "権限の付与先によって手順が変わり、その点がまだ分かっていません。\n\n"
    "**権限の付与先は、個別の利用者ですか、グループですか？**\n\n"
    "（「個別」または「グループ」でお答えください。お答えいただいた内容に応じて、"
    "手順と必要な承認をご案内します。）"
)
# cr-delete-account（run_6e1e5d78acda464c96e14dfa1c65434c）
DELETE_ACCOUNT = (
    "ご質問に答える前に、条件を 1 つ確かめさせてください。\n\n"
    "**所属部署の部門長の承認は得ていますか？**（はい／いいえ）\n\n"
    "（業務ガイド「アカウントの削除」では、この条件によって手順・答えが変わるため、"
    "まず確認しています。）"
)
# cr-grant-permission（e1467、run_2dc5eb2909e547ebb3c3d3fd29823ff2）: 答えが分かった後の案内の結び。
GRANT_PERMISSION_CLOSING = (
    "権限の付与先によって手順が変わるため、まず確認させてください。\n\n"
    "**権限の付与先は、個別の利用者ですか、グループですか？**（選択肢: 個別 / グループ）\n\n"
    "付与先が分かりましたら、その場合の手順をご案内します。"
)
CLARIFICATIONS = {
    "cr-grant-permission": GRANT_PERMISSION,
    "cr-delete-account": DELETE_ACCOUNT,
    "cr-grant-permission-closing": GRANT_PERMISSION_CLOSING,
}


def _lookup_guides() -> RunStep:
    name = "rag__rag_lookup_guides"
    return RunStep(
        run_id="run-1454",
        status=StepStatus.COMPLETED,
        tool_call=ToolCall(name=name, arguments={"query": "権限の付与"}),
        tool_result=ToolResult(name=name, success=True, output={"guides": []}),
    )


@pytest.mark.parametrize("answer", CLARIFICATIONS.values(), ids=CLARIFICATIONS.keys())
def test_question_with_preface_and_answer_guidance_is_clarification_only(answer: str) -> None:
    assert is_clarification_only(answer)
    validation = validation_content(STATUS_SKIPPED, reason=REASON_CLARIFICATION_ONLY)
    content = answer_outcome(answer, steps=[_lookup_guides()], validation=validation)
    assert (content["value"], content["basis"]) == ("needs_clarification", "clarification_question")


# あいさつ・お礼の文を添えた、用件を聞く確認の質問（#1879。#1863 の実モデルの回答）。
GREETINGS = {
    "hello-help": "こんにちは。お手伝いできることがありましたら、お知らせください。",
    "hello-request": "こんにちは。ご用件をお知らせください。",
    "hello-question": "こんにちは。どのようなご用件でしょうか？",
    "hello-exclaim": "こんにちは！\nご用件をお知らせください。",
    "good-work": "お疲れさまです。どのようなご用件でしょうか？",
    "good-evening-thanks": "こんばんは。ありがとうございます。ご用件をお知らせください。",
    # お礼への返事（#1929）。
    "welcome": "どういたしまして。ほかに知りたいことはありますか？",
    "glad-to-help": "お役に立ててよかったです。ほかに知りたいことがあれば、お知らせください。",
    "regards": "よろしくお願いします。どのようなことをお調べしましょうか？",
}


@pytest.mark.parametrize("answer", GREETINGS.values(), ids=GREETINGS.keys())
def test_greeting_with_a_question_is_clarification_only(answer: str) -> None:
    assert is_clarification_only(answer)


@pytest.mark.parametrize(
    "answer",
    [
        # あいさつだけ（質問・依頼が無い）。
        "こんにちは。",
        "こんにちは！\nお疲れさまです。",
        # あいさつに資料の主張を添えた。
        "お疲れ様です。経費精算の締め日は毎月 20 日です。ほかにご質問はありますか？",
        # 決まった文ではないあいさつ（事実の語が入る）。
        "こんにちは、経理部です。ご用件をお知らせください。",
    ],
    ids=["greeting-only", "greetings-only", "greeting-and-claim", "greeting-with-a-name"],
)
def test_greeting_without_a_question_or_with_a_claim_is_not_clarification_only(
    answer: str,
) -> None:
    assert not is_clarification_only(answer)


# 用件（何を知りたいか）を聞くだけの回答（#1929。あいさつ・お礼・質問でない入力への返事）。
ASK_REQUESTS = {
    "scope": "社内の規程や手順について、知りたいことをお知らせください。",
    "what": "何についてお調べしましょうか？",
    "hello": "こんにちは。業務データについて、知りたいことをお知らせください。",
    "welcome": "どういたしまして。ほかに社内の資料や業務データについて知りたいことがあれば、"
    "お知らせください。",
    "anything-else": "どういたしまして。ほかに知りたいことはありますか？",
}


@pytest.mark.parametrize("answer", ASK_REQUESTS.values(), ids=ASK_REQUESTS.keys())
def test_reply_that_asks_what_to_know_is_a_request_ask(answer: str) -> None:
    assert asks_for_request(answer)
    assert is_clarification_only(answer)


@pytest.mark.parametrize(
    "answer",
    [
        # 対象を確かめる確認の質問（質問の解釈を確かめる）。
        "どのシステムの申請ですか？",
        "こんにちは。どの申請についてのご質問ですか？",
        # 主張を抱き合わせた・数字を含む。
        "締め日は毎月 20 日ですが、ほかに知りたいことはありますか？",
        "経費精算の締め日は毎月 20 日です。ほかに知りたいことはありますか？",
        # 決まった文ではない応じる文（入力をなぞる）。
        "テストですね。社内の規程や手順について、知りたいことをお知らせください。",
        # 用件を聞いていない。
        "どういたしまして。",
    ],
    ids=["target", "greeting-and-target", "adversative", "claim", "echo", "no-ask"],
)
def test_clarification_or_claim_is_not_a_request_ask(answer: str) -> None:
    assert not asks_for_request(answer)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("こんにちは", True),
        ("ありがとう", True),
        ("ありがとうございました！", True),
        ("よろしくお願いします", True),
        ("お疲れさま", True),
        ("テスト", False),
        ("こんにちは、休暇の申請方法は？", False),
    ],
    ids=["hello", "thanks", "thanks-past", "regards", "good-work", "test", "with-a-question"],
)
def test_greeting_only_input(question: str, expected: bool) -> None:
    assert is_greeting_only(question) is expected


def test_casual_greeting_in_an_answer_does_not_count_as_a_greeting() -> None:
    """回答の結びの「よろしくお願いします。」は、あいさつへの返事に数えない（#1929。本文の聞き返しの
    差し戻しを外さない）。確認の質問の前置きとしては扱う。"""
    answer = "どの申請ですか？\nよろしくお願いします。"
    assert not greets(answer)
    assert is_clarification_only(answer)
    assert greets("どういたしまして。ほかに知りたいことはありますか？")


def test_passage_kinds_are_not_changed_by_the_clarification_judgment() -> None:
    """段落の判定（主張ではない段落。RAG とそろえる）は変えない。前置きは確認の質問だけの回答かの
    判定にだけ質問の一部として扱う。"""
    preface = "権限の付与先によって手順が変わり、その点がまだ分かっていません。"
    assert preface not in non_claim_passages(GRANT_PERMISSION)


@pytest.mark.parametrize(
    "answer",
    [
        # 前置きに資料の主張がある（確かめる）。
        "権限の付与には部門長の承認が要ります。\n付与先は個別の利用者ですか、グループですか？",
        # 案内の括弧に操作の指示がある。
        "付与先は個別の利用者ですか？\n（個別の場合は「権限」タブで付与してください。）",
        # 前置きに数量の主張がある。
        "削除したアカウントは 30 日後に消去されるため、確かめさせてください。\n"
        "承認は得ていますか？",
        # 質問が無い。
        "その点がまだ分かっていません。\n（「個別」または「グループ」でお答えください。）",
        # 結びに操作の指示・数量・逆接がある（確かめる）。
        "付与先は個別の利用者ですか？\n付与先が分かりましたら、「権限」タブで付与してください。",
        "付与先は個別の利用者ですか？\n付与先が分かりましたら、3 営業日以内の手順をご案内します。",
        "付与先は個別の利用者ですか？\n"
        "付与先が分かりましたら手順をご案内しますが、承認は部門長が行います。",
        # 資料の主張の「案内します」（敬語でない述語）は答えた後の案内として数えない。
        "付与先は個別の利用者ですか？\n申請が決まり次第、情報システム部が手順を案内します。",
    ],
    ids=[
        "claim-preface",
        "operation-guidance",
        "quantity-preface",
        "no-question",
        "operation-closing",
        "quantity-closing",
        "contrast-closing",
        "plain-guidance-claim",
    ],
)
def test_preface_with_a_claim_is_not_clarification_only(answer: str) -> None:
    assert not is_clarification_only(answer)
