"""最終の検証（`rag_validate_answer`）の、業務ガイドの手順ごとの充足（#2708）。

呼び出し側（Agent など）が自分で組み立てた回答を、RAG の回答と同じ規則（`grounded.guide_step_assignment`。主の操作の
文・手順に固有な度合い・共有の item の扱い・検索しなかった手順の扱い。#2491・#2649・#2685・#2706）で確かめる。

- 手順の根拠: 渡した根拠のうち、手順の参照の頁の範囲に当たる根拠と、本文に手順の検索の手がかりが出る根拠（手順に
  参照の文書があれば、その文書の根拠だけ）。業務ガイドの定義から決めるので、同じ根拠を複数の手順が持つ（共有）ことも分かる。
- 手順の説明の item: 主張の監査で裏付けた（supported）段落ごとに、監査が裏付けとして写した原文（離れた箇所は「…」で
  区切った片ごと）を引用とする。写した原文が無い段落（RAG の回答の監査の判定を再利用した段落）は段落の本文を引用にする。
  裏付けの無い段落は呼び出し側が回答から外すので数えない。
- 渡した根拠に根拠の無い手順は、呼び出し側がその手順の根拠を集めていない（未検索）。根拠を共有しうる手順として
  ほかの手順の item を照らし（共通の文だけの item を手順の説明にしない）、状態は no_evidence にする。

モデルは呼ばない。意味の近さの判定の embedding は、語で決まらない共有の item があるときだけ 1 回呼ぶ（失敗・無ければ
語の判定。安全側）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from rag_engine.generation import grounded
from rag_engine.models.llm import GroundedCitation, GroundedItem
from rag_engine.retrieval.guide_steps import GuidePlan, GuideStep

# 監査が離れた 2 か所の原文をつなぐ印（`ValidationClaimCheck.evidence_quote` の説明）。
_QUOTE_JOINS = re.compile(r"…+|\.{3,}")
# 本文の照合に使う手がかりの最短の長さ（1 文字の語は本文のどこにでも出る）。
_MIN_HINT_CHARS = 2
# GroundedItem の text・quote の上限。
_MAX_ITEM_CHARS = 600


def _norm(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _referenced(step: GuideStep, item: Mapping[str, Any]) -> bool:
    """根拠が手順の参照の頁の範囲に当たるか（頁の範囲の無い参照〔文書の全体〕は手順に固有な範囲を決めない）。"""
    document_id = str(item.get("document_id") or "")
    start = _int(item.get("page_start"))
    if not document_id or start is None:
        return False
    end = _int(item.get("page_end"))
    end = end if end is not None and end >= start else start
    return any(
        reference.document_id == document_id and pages.page_start <= end and start <= pages.page_end
        for reference in step.references
        for pages in reference.page_ranges
    )


def _hinted(step: GuideStep, item: Mapping[str, Any]) -> bool:
    """根拠の本文に手順の検索の手がかりが出るか（手順に参照の文書があれば、その文書の根拠だけ）。"""
    documents = {reference.document_id for reference in step.references}
    if documents and str(item.get("document_id") or "") not in documents:
        return False
    text = _norm(str(item.get("text") or ""))
    return any(len(hint := _norm(value)) >= _MIN_HINT_CHARS and hint in text for value in step.retrieval_hints)


def step_sources(plan: GuidePlan, evidence: Sequence[Mapping[str, Any]]) -> dict[str, list[str]]:
    """手順 id → 渡した根拠のうち手順の根拠の id（参照の頁・検索の手がかり。順に、重複なし）。"""
    return {
        step.step_id: list(
            dict.fromkeys(
                str(item.get("id") or "") for item in evidence if item.get("id") and (_referenced(step, item) or _hinted(step, item))
            )
        )
        for step in plan.steps
    }


def _quotes(claim: Mapping[str, Any]) -> list[str]:
    quote = str(claim.get("support_quote") or "").strip()
    parts = [part.strip() for part in _QUOTE_JOINS.split(quote)] if quote else []
    return [part for part in parts if part] or [str(claim.get("answer_quote") or "").strip()]


def _items(claims: Sequence[Mapping[str, Any]], spans: Sequence[dict]) -> tuple[list[grounded.CheckedItem], list[str]]:
    """裏付けた段落の手順の説明の item（引用の片ごと）と、item ごとの段落の本文。"""
    by_id = {span["source_id"]: span for span in spans}
    checked: list[grounded.CheckedItem] = []
    paragraphs: list[str] = []
    for claim in claims:
        if claim.get("status") != "supported":
            continue
        paragraph = str(claim.get("answer_quote") or "").strip()
        own = by_id.get(str(claim.get("source_id") or ""))
        for quote in _quotes(claim):
            if not quote or not paragraph:
                continue
            # 引用の原文がある根拠（段落を結び付けた根拠を先に見る）。無ければ段落を結び付けた根拠。
            ordered = [own, *spans] if own is not None else list(spans)
            span = next((item for item in ordered if item is not None and grounded.quote_in_text(quote, item["text"])), own)
            if span is None:
                continue
            checked.append(
                grounded.CheckedItem(
                    GroundedItem(
                        kind="operation",
                        text=paragraph[:_MAX_ITEM_CHARS],
                        citations=[GroundedCitation(evidence_id="", quote=quote[:_MAX_ITEM_CHARS])],
                    ),
                    span=span,
                )
            )
            paragraphs.append(paragraph)
    return checked, paragraphs


def validate_guide_steps(
    plan: GuidePlan,
    claim_checks: Sequence[Mapping[str, Any]],
    evidence: Sequence[Mapping[str, Any]],
    *,
    embed: Callable[[Sequence[str]], Sequence[Sequence[float]]] | None = None,
) -> dict[str, Any]:
    """回答の段落と業務ガイドの手順の主の操作の文を対応付け、手順ごとの充足を返す。

    ``plan`` は確かめる手順（分かっている条件で外れた分岐の手順を除き、条件が決まらない分岐の手順は required=False）、
    ``claim_checks`` は主張の監査の判定（`validate_answer_claims` の ``claim_checks``）、``evidence`` は渡した根拠
    （``{id, document_id, page_start, page_end, text}``）。戻り値の ``steps`` は手順ごとの ``step_id``・``title``・
    ``required``・候補の状態 ``status``・出典の対応と本文充足の検査状態 ``validation`` と、対応する段落（``answer_quote``。
    説明していなければ空）。``embedding`` は意味の判定の embedding の呼び出し（呼ばなければ None）。
    """
    sources = step_sources(plan, evidence)
    spans = [
        {"source_id": str(item["id"]), "evidence_id": str(item["id"]), "label": str(item["id"]), "text": str(item.get("text") or "")}
        for item in evidence
        if item.get("id")
    ]
    checked, paragraphs = _items(claim_checks, spans)
    guide_steps = [(step.step_id, step.title, tuple(sources.get(step.step_id, ()))) for step in plan.steps]
    # 渡した根拠に根拠の無い手順は、呼び出し側が根拠を集めていない（根拠の共有が分からない）手順として照らす（#2706）。
    missing = [step_id for step_id, _title, keys in guide_steps if not keys]
    semantics = grounded.GuideStepSemantics(embed)
    coverage, _assigned = grounded.guide_step_assignment(
        guide_steps,
        checked,
        spans,
        plan.optional_step_ids,
        dict(plan.step_terms),
        semantics,
        missing,
    )
    explaining = {
        step["step_id"]: "\n".join(dict.fromkeys(paragraphs[index] for index in step["assignment"]["assigned_item_indexes"]))
        for step in coverage
    }
    steps = [
        {
            "step_id": step["step_id"],
            "title": step["title"],
            "required": step["required"],
            # 最終の検証では、手順の根拠が渡した根拠に無いことを no_evidence（未検索）とする。
            "status": grounded.GUIDE_STEP_NO_EVIDENCE if step["status"] == grounded.GUIDE_STEP_NOT_SEARCHED else step["status"],
            "answer_quote": explaining.get(step["step_id"], ""),
            "validation": step["validation"],
        }
        for step in coverage
    ]
    return {"steps": steps, "embedding": semantics.trace() if semantics.calls else None}


__all__ = ["step_sources", "validate_guide_steps"]
