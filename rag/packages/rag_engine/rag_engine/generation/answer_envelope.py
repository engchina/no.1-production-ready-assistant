"""回答の構造（AnswerEnvelope。#1235、handoff §12）。

根拠付き回答（grounded）の確定の段で、すでにある草稿・監査・公開した説明から、回答の対応・要求ごとの
充足・適用の条件・不足・実データの確認を決定的に組み立てる。モデルは呼ばない。本文（`grounded.render`）と
同じ材料から作るので、本文と食い違わない。

対応（outcome）:

- answered: 公開した説明があり、未回答の要求・不足・条件付きの説明が無い。段落の警告（warning）だけがある回答も
  ここ（handoff_reasons に rule_warnings。#2738）。
- conditional: 公開した説明はあるが、未回答の要求・不足・適用の条件（未確認を含む）がある。または原文の
  提示だけ（適用は未確認）で答えた。利用者が答えられる確認の項目（`who_can_answer=user`）・別の資料で
  分かる項目（`who_can_answer=document`）だけを確かめる回答もここ（#1453、#1492）。根拠と矛盾して外した段落
  （contradicted_removed）か、根拠で確かめられない段落（unverified_passages）がある回答もここ（#2738）。
- needs_environment_data: 回答の確定に、システムだけが持つ値（設定値・記録・ログ。確認の項目の
  `who_can_answer=system`）の確認が要る。別の資料の確認（`confirmation_lines` の「資料:」の行）は実データに
  数えない（#1491）。

確認の項目（草稿の external_data_items）の分け方は `settle_confirmation_items`（#1492）。モデルには観察できる
事実（誰が答えられるか・すでに分かっているか・結論がその値で分かれるか）だけを答えさせ、どの対応にするかは
ここの規則で決める（selective prediction / clarifying question の判定と同じく、自己申告の分類に頼らない）。
- insufficient_evidence: 公開できる説明も原文も無く、資料から答えられない。実行できる説明が無く、要求が
  すべて答えられていない（missing）ときも、実データ・別の資料の確認より先にここにする（#1491）。本文の説明が
  すべて根拠で確かめられない（unverified）ときもここ（本文は出す。#2738）。
- needs_clarification / needs_human: 利用者への確認・人への引き継ぎ（業務ガイドと会話の段で決める。
  ここでは作らない）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

# 2: 段落の印（passage_marks）・外した段落（removed_passages）・回答全体の警告（answer_warnings）と、
#    items の印（mark）を足した（#2738）。
ENVELOPE_SCHEMA_VERSION = 2
AnswerOutcome = Literal[
    "answered",
    "conditional",
    "needs_clarification",
    "needs_environment_data",
    "needs_human",
    "insufficient_evidence",
]
# 要求の状態。unknown は監査が無く充足を確かめていない要求。
RequestStatus = Literal["addressed", "partial", "missing", "unknown"]
# 業務ガイドの手順の充足を載せる要求の ID の接頭辞（#2491。`guide:<手順 id>`）と、手順の状態から要求の状態への写し。
GUIDE_STEP_REQUEST_PREFIX = "guide:"
GUIDE_STEP_REQUEST_ID_CHARS = 64
_GUIDE_STEP_STATUS: dict[str, RequestStatus] = {
    "explained": "addressed",
    "unknown": "unknown",
    "partial": "partial",
    "reference_only": "partial",
    "unused": "missing",
    "no_evidence": "missing",
    # 手順ごとの検索を打ち切って検索しなかった手順（#2706）。確かめていないので答えたことにしない。
    "not_searched": "missing",
}
# 確認の行のうち、回答の確定に必要な別の資料の行の接頭辞（grounded.confirmation_lines）。現場の実データ
# ではないので、needs_environment_data の判定に数えない（#1491）。
REFERENCE_CONFIRMATION_PREFIX = "資料: "
# 確認の項目に答えられる人・もの（#1492、#1500）。user=質問した利用者本人（自分の状況・対象の属性・選ぶもの）、
# system=稼働中のシステムにだけある値（設定の現在値・ログ・処理の状態・実績）、document=KB の資料（規程・マニュアル・
# コード表・台帳・一覧の値）。
WhoCanAnswer = Literal["user", "system", "document"]
WHO_CAN_ANSWER: tuple[WhoCanAnswer, ...] = ("user", "system", "document")
# #1492 より前の草稿・記録の種類（kind）からの写し。
LEGACY_KIND_WHO: dict[str, WhoCanAnswer] = {"environment": "system", "user_condition": "user"}
# 対応の決定に数えなかった項目の理由（診断に残す）。
EXCLUDED_ALREADY_KNOWN = "already_known"
EXCLUDED_NOT_DECISIVE = "not_decisive"
EXCLUDED_KNOWN_CONDITION = "known_condition"
EXCLUDED_VERSION_SETTLED = "version_settled"
# 版を確かめる項目（「適用されている版」「どの版か」「第1版と第2版のどちらか」）。根拠が旧版・廃止の版を
# 決めているときは確認の項目にしない（#1492）。
_VERSION_ITEM = re.compile(
    r"第\s*[0-9０-９一二三四五六七八九十]+\s*版|(?:どの|適用(?:され(?:てい)?る|する)|有効な|使う|最新の?|新しい|今の)版"
    r"|版(?:の(?:確認|違い|別)|が(?:異な|違))|旧版|新版|改訂版|バージョン"
)


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))


def _request_text(unit: Mapping[str, Any]) -> str:
    text = str(unit.get("text") or "")
    text = text.split("原文: ", 1)[1] if "原文: " in text else text
    return " ".join(text.split())


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or ""))


def confirmation_item_facts(item: Mapping[str, Any] | str) -> dict[str, Any]:
    """確認の項目 1 件を、規則が読む事実（text / who_can_answer / already_known / decides）にそろえる。

    欠けた値は保守的に埋める（#1492）: 誰が答えられるかが無ければ #1453 の種類（kind）から写し、種類も無い
    文字列だけの項目は今までどおりシステムの値（現場の実データ）とする。already_known が無ければ未確認、
    decides が無ければ結論を分ける（対応の決定に数える）とする。
    """
    if isinstance(item, str):
        return {"text": item.strip(), "who_can_answer": "system", "already_known": False, "decides": True}
    who = item.get("who_can_answer")
    if who not in WHO_CAN_ANSWER:
        who = LEGACY_KIND_WHO.get(str(item.get("kind") or ""), "system")
    already_known = item.get("already_known")
    decides = item.get("decides")
    return {
        "text": str(item.get("text") or "").strip(),
        "who_can_answer": who,
        "already_known": already_known if isinstance(already_known, bool) else False,
        "decides": decides if isinstance(decides, bool) else True,
    }


@dataclass(frozen=True)
class SettledConfirmations:
    """確認の項目を規則で分けた結果（#1492）。

    user は利用者に確かめる条件（適用の条件・conditional）、system はシステムだけが持つ値
    （needs_environment_data）、document は別の資料で分かる点（資料の確認・conditional）。excluded は対応の
    決定に数えなかった項目と理由（`EXCLUDED_*`）。
    """

    user: tuple[str, ...] = ()
    system: tuple[str, ...] = ()
    document: tuple[str, ...] = ()
    excluded: tuple[dict[str, str], ...] = ()

    def diagnostics(self) -> dict[str, Any]:
        return {
            "user": list(self.user),
            "system": list(self.system),
            "document": list(self.document),
            "excluded": [dict(entry) for entry in self.excluded],
        }


def _known_condition(text: str, known_conditions: Sequence[tuple[str, str]]) -> str:
    """項目が、分かっている条件（業務ガイドの条件の名前）を確かめるものなら、その名前。"""
    compact = _compact(text)
    for label, value in known_conditions:
        name = _compact(label)
        if len(name) >= 2 and str(value or "").strip() and name in compact:
            return label
    return ""


def settle_confirmation_items(
    items: Sequence[Mapping[str, Any] | str],
    *,
    known_conditions: Sequence[tuple[str, str]] = (),
    version_settled: bool = False,
) -> SettledConfirmations:
    """確認の項目を、観察できる事実から規則で分ける（#1492）。

    - 対応の決定に数えない: すでに分かっている（already_known）、結論がその値で分かれない（decides=false）、
      分かっている条件（known_conditions。業務ガイドの条件の名前と値。MCP の conditions など）を確かめる項目、
      根拠が旧版・廃止の版を決めているとき（version_settled）の版を確かめる項目。
    - 残りを誰が答えられるかで分ける: user → 利用者に確かめる条件、system → システムだけが持つ値、
      document → 別の資料で分かる点。

    「迷うときは〜」のような既定の寄せ方はしない。値が欠けた項目は `confirmation_item_facts` が保守的に埋める。
    """
    groups: dict[str, list[str]] = {name: [] for name in WHO_CAN_ANSWER}
    excluded: list[dict[str, str]] = []
    for raw in items:
        facts = confirmation_item_facts(raw)
        text = facts["text"]
        if not text:
            continue
        reason = ""
        if facts["already_known"]:
            reason = EXCLUDED_ALREADY_KNOWN
        elif not facts["decides"]:
            reason = EXCLUDED_NOT_DECISIVE
        elif _known_condition(text, known_conditions):
            reason = EXCLUDED_KNOWN_CONDITION
        elif version_settled and _VERSION_ITEM.search(unicodedata.normalize("NFKC", text)):
            reason = EXCLUDED_VERSION_SETTLED
        if reason:
            excluded.append({"text": text, "who_can_answer": facts["who_can_answer"], "reason": reason})
            continue
        groups[facts["who_can_answer"]].append(text)
    return SettledConfirmations(
        user=tuple(_unique(groups["user"])),
        system=tuple(_unique(groups["system"])),
        document=tuple(_unique(groups["document"])),
        excluded=tuple(excluded),
    )


def requires_environment_data(
    settled: SettledConfirmations, *, items: Sequence[Mapping[str, Any] | str], external_data_required: bool
) -> bool:
    """回答の確定にシステムだけが持つ値の確認が要るか（#1453、#1492）。

    システムだけが持つ値の項目が残れば要る。項目を 1 つも挙げずに印（external_data_required）だけを立てた
    草稿は、今までどおり印に従う。項目を挙げてすべて規則で外れたなら要らない。
    """
    if settled.system:
        return True
    return external_data_required and not any(confirmation_item_facts(item)["text"] for item in items)


def envelope_requests(requests: Sequence[Mapping[str, Any]], reviews: Sequence[Mapping[str, Any]] | None) -> list[dict[str, str]]:
    """要求単位ごとの充足。背景（kind=context）の要求は含めない。"""
    by_id = {str(review.get("request_id")): review for review in reviews or ()}
    result: list[dict[str, str]] = []
    for unit in requests:
        if unit.get("kind") == "context":
            continue
        request_id = str(unit.get("id") or "")
        review = by_id.get(request_id)
        if reviews is None:
            status = "unknown"
        elif review is None:
            # 対応する結果がない要求は、充足も材料不足も確定していない。
            status = "unknown"
        else:
            status = str(review.get("status") or "unknown")
            if status == "context":
                continue
        result.append({"id": request_id, "text": _request_text(unit), "status": status})
    return result


def guide_step_requests(guide_steps: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """業務ガイドの必須の手順の充足を、要求と同じ形にする（#2491）。

    手順の根拠を引いた説明を公開した手順は addressed、原文を参考情報として示しただけの手順は partial、
    説明に使わなかった・根拠の無い手順は missing。必須に数えない手順（条件が決まらない分岐の手順）は載せない。
    """
    return [
        {
            "id": f"{GUIDE_STEP_REQUEST_PREFIX}{step.get('step_id')}"[:GUIDE_STEP_REQUEST_ID_CHARS],
            "text": f"業務ガイドの手順: {step.get('title') or step.get('step_id')}",
            "status": _GUIDE_STEP_STATUS.get(str(step.get("status")), "unknown"),
        }
        for step in guide_steps
        if step.get("required", True)
    ]


def envelope_item(entry: Any) -> dict[str, Any]:
    """公開した説明 1 件（grounded の CheckedItem）。mark は段落の印の状態（unverified / warning。無ければ空。#2738）。"""
    item = entry.item
    span = entry.span or {}
    applies = "quote_only" if entry.quote_only else str(item.applies)
    return {
        "kind": str(item.kind),
        "text": str(item.quote if entry.quote_only else item.text),
        "evidence_id": str(span.get("evidence_id") or ""),
        "source": str(span.get("source") or ""),
        "location": str(span.get("location") or (f"p.{span['page']}" if span.get("page") else "")),
        "applies": applies,
        "condition": str(item.condition or ""),
        "mark": str(getattr(entry, "state", "") or ""),
    }


def build_envelope(
    *,
    requests: Sequence[Mapping[str, Any]],
    reviews: Sequence[Mapping[str, Any]] | None,
    items: Sequence[Mapping[str, Any]],
    gaps: Sequence[str],
    confirmations: Sequence[str],
    external_data_required: bool,
    reference_materials: Sequence[str] = (),
    off_goal: bool = False,
    user_conditions: Sequence[str] = (),
    document_items: Sequence[str] = (),
    guide_steps: Sequence[Mapping[str, Any]] = (),
    passage_marks: Sequence[Mapping[str, Any]] = (),
    removed_passages: Sequence[Mapping[str, Any]] = (),
    answer_warnings: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """回答の構造を組み立てる。

    confirmations は現場の実データ・別の資料の確認、user_conditions は利用者に確かめる条件（#1453）。
    利用者に確かめる条件は適用の条件（conditions）に載せ、現場の実データの確認には数えない。別の資料の
    確認（「資料:」の行）も現場の実データではないので、needs_environment_data の判定に数えない（#1491）。
    document_items は結論を分けるのに別の資料で確かめる点（確認の項目の who_can_answer=document。#1492）で、
    資料の不足として回答を条件付きにし、reference_materials に載せる。
    guide_steps は業務ガイドの手順ごとの充足（#2491。grounded の `guide_step_coverage`）で、必須の手順を要求に足す。
    説明できていない手順は未回答の要求として回答を条件付きにする。
    passage_marks・removed_passages・answer_warnings は段落の印・外した段落・回答全体の警告（#2738。形は
    ``platform/contracts/answer-passages/passage-marks.json``）。矛盾で外した段落か未確認の段落があれば条件付き、
    警告だけなら answered のまま handoff_reasons に rule_warnings を付ける。本文の説明（items の mark）がすべて
    未確認なら insufficient_evidence（本文は出す）。
    """
    request_entries = [*envelope_requests(requests, reviews), *guide_step_requests(guide_steps)]
    actionable = [item for item in items if item.get("applies") != "quote_only"]
    unsettled = [item for item in actionable if item.get("applies") in {"conditional", "unverified"}]
    unanswered = [entry for entry in request_entries if entry["status"] in {"partial", "missing"}]
    unverified_requests = reviews is not None and any(entry["status"] == "unknown" for entry in request_entries)
    environment = [line for line in confirmations if not line.startswith(REFERENCE_CONFIRMATION_PREFIX)]
    # 実行できる説明が無く、監査した要求がすべて答えられていない: 資料から答えられない（#1491）。
    no_answer = not actionable and bool(request_entries) and all(entry["status"] == "missing" for entry in request_entries)
    conditions = _unique([*(str(item.get("condition") or "") for item in unsettled), *user_conditions])
    handoff: list[str] = []
    if unanswered:
        handoff.append("unanswered_requests")
    if unverified_requests:
        handoff.append("unverified_requests")
    if gaps:
        handoff.append("gaps")
    if external_data_required or environment:
        handoff.append("environment_data")
    if any(item.get("applies") == "unverified" for item in unsettled):
        handoff.append("unverified_applicability")
    if user_conditions:
        handoff.append("user_conditions")
    if reference_materials or document_items:
        handoff.append("reference_materials")
    if off_goal:
        handoff.append("off_goal")
    unverified_marks = [mark for mark in passage_marks if mark.get("state") == "unverified"]
    if removed_passages:
        handoff.append("contradicted_removed")
    if unverified_marks:
        handoff.append("unverified_passages")
    if any(mark.get("state") == "warning" for mark in passage_marks):
        handoff.append("rule_warnings")
    # 本文の説明がすべて根拠で確かめられない（確かめた段落が 1 つも無い）。
    all_unverified = bool(actionable) and all(item.get("mark") == "unverified" for item in actionable)

    outcome: AnswerOutcome
    if no_answer:
        outcome = "insufficient_evidence"
    elif external_data_required or (environment and not actionable):
        outcome = "needs_environment_data"
    elif not items or all_unverified:
        outcome = "insufficient_evidence"
    elif (
        not actionable
        or unanswered
        or unverified_requests
        or gaps
        or unsettled
        or off_goal
        or user_conditions
        or document_items
        or removed_passages
        or unverified_marks
    ):
        outcome = "conditional"
    else:
        outcome = "answered"
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "outcome": outcome,
        "requests": request_entries,
        "items": [dict(item) for item in items],
        "conditions": conditions,
        "gaps": _unique(gaps),
        "confirmations": _unique(confirmations),
        "reference_materials": _unique([*reference_materials, *document_items]),
        "handoff_reasons": handoff,
        "passage_marks": [dict(mark) for mark in passage_marks],
        "removed_passages": [dict(item) for item in removed_passages],
        "answer_warnings": [dict(item) for item in answer_warnings],
    }


def fallback_envelope(
    *,
    answered: bool,
    insufficient_reason: str = "",
    needs_human_review: bool | None = None,
    external_data_required: bool | None = None,
    external_data_items: Sequence[str] = (),
) -> dict[str, Any]:
    """grounded の確定を経ない回答（根拠が無く打ち切った回答・承認済み FAQ など）の構造。

    説明の構造が無いので、回答の有無と不足・人の確認・実データの印から対応だけを決める。
    """
    envelope = build_envelope(
        requests=(),
        reviews=None,
        items=(),
        gaps=[insufficient_reason] if insufficient_reason else [],
        confirmations=list(external_data_items),
        external_data_required=bool(external_data_required),
    )
    if answered and envelope["outcome"] == "insufficient_evidence":
        envelope["outcome"] = "conditional" if (insufficient_reason or needs_human_review) else "answered"
    return envelope


__all__ = [
    "ENVELOPE_SCHEMA_VERSION",
    "EXCLUDED_ALREADY_KNOWN",
    "EXCLUDED_KNOWN_CONDITION",
    "EXCLUDED_NOT_DECISIVE",
    "EXCLUDED_VERSION_SETTLED",
    "GUIDE_STEP_REQUEST_PREFIX",
    "LEGACY_KIND_WHO",
    "REFERENCE_CONFIRMATION_PREFIX",
    "WHO_CAN_ANSWER",
    "AnswerOutcome",
    "SettledConfirmations",
    "WhoCanAnswer",
    "build_envelope",
    "confirmation_item_facts",
    "envelope_item",
    "envelope_requests",
    "fallback_envelope",
    "guide_step_requests",
    "requires_environment_data",
    "settle_confirmation_items",
]
