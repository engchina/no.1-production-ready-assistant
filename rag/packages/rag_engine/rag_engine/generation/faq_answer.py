"""利用者が選んだ承認済み FAQ（類似問）だけから回答する（検索しない。#702）。"""

from __future__ import annotations

import json
from typing import Any

from pr_backend_core.observability.model_purpose import call_with_model_purpose
from pydantic import BaseModel, Field

from rag_engine.resources.runtime import resolve_prompt

SYSTEM_PROMPT = (
    "## Step 1: 選択された承認済み FAQ を読む\n"
    "あなたは社内の承認済み FAQ をもとに回答する担当者です。\n"
    "利用者は、自分の質問と同じ趣旨の承認済み FAQ（類似問とその承認済み"
    "の回答）を選びました。\n"
    "入力の JSON の question（利用者の質問）に、approv"
    "ed_faq の answer（承認済みの回答）だけを根拠にして、日本"
    "語で答えてください。\n"
    "\n"
    "## Step 2: 承認済みの内容を保つ\n"
    "- 承認済みの回答に書かれていない事実・数値・条件・手順を足さない。推"
    "測や一般論で補わない。\n"
    "- 承認済みの回答の数値・条件・固有名詞は、言い換えずにそのまま使う。"
    "\n"
    "\n"
    "## Step 3: 質問に合わせて回答する\n"
    "- 利用者の質問に合わせて、必要な部分を先に、簡潔に答える。質問に関係"
    "の無い部分は省いてよい。\n"
    "- 承認済みの回答が質問の一部にしか答えていなければ、答えられる部分だ"
    "けを答え、残りは「承認済み FAQ には記載がありません」と明示する。"
    "\n"
    "- 承認済みの回答が質問に答えていなければ、その旨を正直に伝える。\n"
    "\n"
    "## Step 4: 出力を整える\n"
    "- 出典の表示は付けない（システムが付ける）。\n"
)


class FaqAnswerDraft(BaseModel):
    """承認済み FAQ から作った回答。"""

    answer: str = Field(min_length=1, max_length=4000, description="利用者への回答（本文のみ）")


def faq_answer_prompt(question: str, faq_question: str, faq_answer: str) -> str:
    """LLM に渡す入力（質問・類似問・承認済みの回答だけ）。"""
    return json.dumps(
        {"question": question, "approved_faq": {"question": faq_question, "answer": faq_answer}},
        ensure_ascii=False,
    )


def answer_from_approved_faq(question: str, faq_question: str, faq_answer: str, settings: Any) -> str:
    """質問・類似問・承認済みの回答から回答の本文を作る（既定の回答のモデル）。"""
    from rag_engine.adapters.oci import parse_text_response

    draft = call_with_model_purpose(
        "answer.generate",
        parse_text_response,
        resolve_prompt("faq_answer.system", SYSTEM_PROMPT),
        faq_answer_prompt(question, faq_question, faq_answer),
        settings,
        FaqAnswerDraft,
    )
    return draft.answer.strip()
