"""最終の検証の、業務ガイドの手順ごとの充足（#2708）。

Agent が組み立てた回答（Markdown の段落）を、RAG の回答と同じ規則で手順と対応付ける。監査のモデルは録画した応答の
スタブ、embedding は決定論の vector。根拠・手順・回答は合成。
"""

import json
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from rag_engine.evaluation.answer_validation import QUESTION_ID, ClaimAuditOutput, validate_answer_claims
from rag_engine.evaluation.guide_step_validation import step_sources, validate_guide_steps
from rag_engine.retrieval.guide_steps import GuidePlan

# 2 つの手順が共有する章節の chunk（手順 1・2 の主の操作と、両手順に共通の末尾の操作）。
MAIN_1 = "設定画面で設定 A の値を入力します。"
MAIN_2 = "反映画面で反映ボタンを押して変更を反映します。"
COMMON = "最後に保存ボタンを押して画面を閉じます。"
SHARED = MAIN_1 + MAIN_2 + COMMON
OTHER = "別の章節の説明です。承認の依頼は承認画面で行います。"
EVIDENCE = [
    {"id": "c-shared", "document_id": "doc-x", "page_start": 3, "page_end": 3, "text": SHARED},
    {"id": "c-other", "document_id": "doc-x", "page_start": 8, "page_end": 8, "text": OTHER},
]
REFERENCE = [{"document_id": "doc-x", "page_ranges": [{"page_start": 3, "page_end": 3}]}]
# 手順の題名は根拠の原文と言い回しが違う（手順の語が根拠の文に無い）。2 手順とも同じ頁を参照する。
STEPS = [
    {"id": "s1", "title": "値の登録", "references": REFERENCE},
    {"id": "s2", "title": "内容の確定", "references": REFERENCE},
]
# 合成の vector。手順 1・2 の主の操作の文は各手順の記述に近く、共通の末尾の操作は両手順に同じくらい近い。
VECTORS = (
    ("値の登録", (1.0, 0.0, 0.0)),
    ("内容の確定", (0.0, 1.0, 0.0)),
    ("設定 A の値", (1.0, 0.0, 0.3)),
    ("反映ボタンを押", (0.0, 1.0, 0.3)),
    ("保存ボタンを押", (0.5, 0.5, 1.0)),
    ("承認", (0.3, 0.3, 0.3)),
)


class StubEmbed:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, texts: Any) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise RuntimeError("embedding unavailable")
        return [next(list(vector) for key, vector in VECTORS if key in text) for text in texts]


def _plan(steps: list[dict[str, Any]] = STEPS) -> GuidePlan:
    plan = GuidePlan.from_payload("", 1, steps)
    assert plan is not None
    return plan


def _audit(quotes: list[str]) -> Callable[..., ClaimAuditOutput]:
    """段落の順に、裏付けとして写した原文（quote）を返す監査のスタブ（すべて supported）。"""

    def parse(system: str, inputs: str, settings: Any, schema: Any, provider_id: str | None = None) -> ClaimAuditOutput:
        payload = json.loads(inputs)
        evidence = [item for item in payload["evidence_items"] if item["id"] != QUESTION_ID]
        claims = []
        for passage, quote in zip(payload["answer_passages"], quotes, strict=True):
            chunk = next(item for item in evidence if all(part in item["text"] for part in quote.split("…")))
            claims.append(
                {
                    "answer_quote": "段落",
                    "answer_passage_id": passage["id"],
                    "evidence_id": chunk["evidence_id"],
                    "evidence_quote": quote,
                    "applicability": "matched",
                    "status": "supported",
                    "source_id": "",
                    "reason": "資料に記載されている",
                }
            )
        return ClaimAuditOutput.model_validate({"claim_checks": claims})

    return parse


def _statuses(answer: str, quotes: list[str], *, embed: Any = None, steps: list[dict[str, Any]] = STEPS) -> dict[str, str]:
    items = [{key: item[key] for key in ("id", "page_start", "page_end", "text")} | {"source": "manual.pdf"} for item in EVIDENCE]
    # 各説明の実際の出典を付け、意味の対応付けと手順の充足を検査する。
    answer = "\n\n".join(part if part.startswith("#") else part + "\n根拠：manual.pdf p.3" for part in answer.split("\n\n"))
    with patch("rag_engine.evaluation.answer_eval.parse_text_response", side_effect=_audit(quotes)):
        claims = validate_answer_claims("質問", answer, items, settings=None)
    result = validate_guide_steps(_plan(steps), claims["claim_checks"], EVIDENCE, embed=embed)
    return {step["step_id"]: step["status"] for step in result["steps"]}


# Agent の回答（Markdown）。手順の説明は資料の原文と言い回しが違い、手順の題名も出てこない。
REWORDED_ANSWER = (
    "## 進め方\n\n1. 設定画面を開き、設定 A に値を入れてください。\n"
    "2. 続いて反映画面の反映ボタンで変更を反映させます。\n\n最後は保存ボタンで画面を閉じます。"
)
COMMON_ANSWER = "## 進め方\n\n保存ボタンを押して画面を閉じれば完了です。"


def test_reference_pages_make_both_steps_share_the_chunk() -> None:
    assert step_sources(_plan(), EVIDENCE) == {"s1": ["c-shared"], "s2": ["c-shared"]}
    # 頁の範囲の無い参照（文書の全体）では紐づけず、本文に検索の手がかりが出る根拠を紐づける。
    hinted = _plan([{"id": "s1", "title": "t", "retrieval_hints": ["承認の依頼"], "references": [{"document_id": "doc-x"}]}])
    assert step_sources(hinted, EVIDENCE) == {"s1": ["c-other"]}


def test_reworded_correct_explanation_is_explained() -> None:
    """言い回しの違う正しい説明（各手順の主の操作を裏付けた段落）は、手順の題名が無くても explained。"""
    embed = StubEmbed()
    statuses = _statuses(REWORDED_ANSWER, [MAIN_1, MAIN_2, COMMON], embed=embed)
    assert statuses == {"s1": "unknown", "s2": "unknown"}
    # embedding は 1 回だけ。
    assert len(embed.calls) == 1
    # 2 つの操作を 1 つの段落で述べ、離れた 2 か所の原文を「…」でつないで裏付けた段落も、両手順の説明になる。
    joined = "設定画面で設定 A に値を入れ、反映画面の反映ボタンで反映させます。"
    assert _statuses(joined, [f"{MAIN_1}…{MAIN_2}"], embed=StubEmbed()) == {"s1": "unknown", "s2": "unknown"}


def test_common_sentence_only_answer_is_not_explained() -> None:
    """共有の chunk の共通の文だけを述べた回答は、どの手順も explained にしない。"""
    assert _statuses(COMMON_ANSWER, [COMMON], embed=StubEmbed()) == {"s1": "unknown", "s2": "unknown"}
    # embedding が無い・失敗したときは語の判定（安全側）。
    assert _statuses(COMMON_ANSWER, [COMMON]) == {"s1": "unknown", "s2": "unknown"}
    assert _statuses(REWORDED_ANSWER, [MAIN_1, MAIN_2, COMMON], embed=StubEmbed(fail=True)) == {"s1": "unknown", "s2": "unknown"}


def test_step_without_passed_evidence_is_no_evidence_and_shares_unknown() -> None:
    """渡した根拠に根拠の無い手順は no_evidence。その手順と根拠を共有しうるので、共通の文で他の手順を説明済みにしない。"""
    steps = [STEPS[0], {"id": "s2", "title": "内容の確定", "references": [{"document_id": "doc-y", "page_ranges": [{"page_start": 1}]}]}]
    assert _statuses(COMMON_ANSWER, [COMMON], steps=steps, embed=StubEmbed()) == {"s1": "unknown", "s2": "no_evidence"}
    assert _statuses(REWORDED_ANSWER, [MAIN_1, MAIN_2, COMMON], steps=steps, embed=StubEmbed()) == {"s1": "unknown", "s2": "no_evidence"}


def test_unsupported_paragraph_does_not_explain_a_step() -> None:
    """裏付けの無い段落（呼び出し側が回答から外す）は手順の説明に数えない。"""
    plan = _plan()
    claims = [{"answer_quote": "設定 A に値を入れる。", "status": "unsupported", "source_id": "c-shared", "support_quote": MAIN_1}]
    result = validate_guide_steps(plan, claims, EVIDENCE, embed=StubEmbed())
    assert [step["status"] for step in result["steps"]] == ["unused", "unused"]
    assert result["embedding"] is None
