"""回答の最終の検証の決定的な検査（#1276、handoff §12 の 1・4・5）。モデルは呼ばない。

主張の監査（モデル 1 回。`answer_validation`）と並べて、呼び出し側が任意で渡した材料から
次を確かめる。

- requests: AnswerEnvelope の要求ごとの充足（addressed / partial / missing / unknown）。
  partial・missing の要求を報告する。回答の本文に不足として示していれば warning、黙って落ちて
  いれば error。
- guide_steps: 業務ガイドの公開の版の手順。回答に出た手順の順序が依存（depends_on）と逆でないか、
  既知の条件に当たらない分岐の手順を含まないか、依存の手順・続きの手順を落としていないか。
- impact: 業務ガイドの影響範囲（グループ / 全体）と承認の要否を回答に書いているか。影響範囲・
  承認が係る手順（`impact.steps`）が、分かっている条件で外れた分岐の手順だけなら確かめない（#1320）。

手順・語の照合は、NFKC・大文字小文字・空白を揃えた文字列の部分一致で行う。手順の充足の判定
（`guide_step_validation`。#2708）が手順の説明にした段落があれば、その段落の位置を手順の位置に
する（言い換えた回答でも順序を確かめる）。充足の判定が無く、手順の名前が回答に 1 つも見つから
なければ、順序は確かめられないことを warning で示す。手順の照合は
実行の指示の段落（行）だけで行い、満たした前提の言及（「→ この前提は満たしています」「承認済み」の
行と、見出しが「満たしている条件」などの節）は数えない（#1491）。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

from rag_engine.generation.answer_labels import answer_labels
from rag_parser_core.language import current_pack

from app.schemas.support_guide import SupportGuideBranchWhen, SupportGuideContent

FindingCheck = Literal["requests", "guide", "guide_steps", "impact"]
FindingSeverity = Literal["error", "warning"]


def gap_section_titles() -> tuple[str, ...]:
    """回答の本文で不足を示す節の見出し（rag_engine の言語資源の answer_labels。#2040）。"""
    labels = answer_labels()
    return (labels.gaps_section, labels.unverified_section, labels.deadline_section)


# 影響範囲ごとに、回答に書いてあれば範囲を示したとみなす語。
def impact_scope_terms() -> dict[str, tuple[str, ...]]:
    """影響範囲ごとに、回答に書いてあれば範囲を示したとみなす語。

    語は言語資源の guide_check_cues（#2181）。
    """
    pack = current_pack()
    return {
        "group": pack.words("guide_check_cues", "impact_group_terms"),
        "all": pack.words("guide_check_cues", "impact_all_terms"),
    }


IMPACT_SCOPE_LABELS = {"individual": "個別", "group": "グループ", "all": "全体"}

# 照合に使う手順の名前の最短の長さ（1 文字の名前は本文のどこにでも当たる）。
_MIN_TITLE_CHARS = 2


# 満たした前提の言及の印（手順の実行の指示ではない。#1491）・承認の語・真偽の値の言い方・
# 見出しと箇条の行の印は言語資源（guide_check_cues。#2181）。NFKC・空白を揃えた後の文字列で照らす。
# 見出しの行（Markdown の見出し・全体が強調・末尾がコロン）。
def _heading() -> re.Pattern[str]:
    """見出しの行（Markdown の見出し・全体が強調・末尾がコロン）。

    文末の字は言語資源の question_cues、コロンは evidence_cues。
    """
    pack = current_pack()
    ends = re.escape("".join(pack.words("question_cues", "sentence_split_chars")))
    colons = re.escape("".join(pack.words("evidence_cues", "label_colon_chars")))
    return re.compile(
        rf"^\s*(#{{1,6}}\s+.+|\*\*[^*]+\*\*[{colons}]?|[^{ends}]{{1,40}}[{colons}])\s*$"
    )


# 箇条書き・番号付きの行（節の続き）。
def _list_item() -> re.Pattern[str]:
    """箇条書き・番号の行の始まり（印は言語資源の guide_check_cues）。"""
    pack = current_pack()
    marks = re.escape("".join(pack.words("guide_check_cues", "guide_list_marks")))
    closers = re.escape("".join(pack.words("guide_check_cues", "guide_number_closers")))
    return re.compile(rf"^\s*([{marks}]|\d+[{closers}])")


@dataclass(frozen=True)
class AnswerFinding:
    check: FindingCheck
    code: str
    severity: FindingSeverity
    message: str
    request_id: str | None = None
    step_id: str | None = None
    related_step_id: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


def _normalized(text: str) -> str:
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def _value(text: str) -> str:
    value = _normalized(text)
    return _normalized(
        current_pack().mapping("guide_check_cues", "boolean_aliases").get(value, value)
    )


def check_requests(
    answer: str, requests: Sequence[Mapping[str, str]], gaps: Sequence[str] = ()
) -> list[AnswerFinding]:
    """要求ごとの充足。partial / missing を報告し、本文で不足を示していなければ error にする。"""
    normalized = _normalized(answer)
    section = any(_normalized(title) in normalized for title in gap_section_titles())
    gap_shown = any(_normalized(gap) and _normalized(gap) in normalized for gap in gaps)
    findings: list[AnswerFinding] = []
    for request in requests:
        request_id = str(request.get("id") or "")
        text = " ".join(str(request.get("text") or "").split())
        status = request.get("status")
        label = f"要求「{text}」" if text else f"要求 {request_id}"
        if status == "unknown":
            findings.append(
                AnswerFinding(
                    "requests",
                    "request_unverified",
                    "warning",
                    f"{label}に答えたかは確かめられていません。",
                    request_id=request_id,
                )
            )
            continue
        if status not in {"partial", "missing"}:
            continue
        disclosed = section or gap_shown or bool(text and _normalized(text) in normalized)
        state = "一部しか答えていません" if status == "partial" else "答えていません"
        findings.append(
            AnswerFinding(
                "requests",
                f"request_{status}",
                "warning" if disclosed else "error",
                f"{label}に{state}"
                + (
                    "（回答に不足として示しています）。"
                    if disclosed
                    else "。不足も示していません。"
                ),
                request_id=request_id,
            )
        )
    return findings


def _branch_matches(when: SupportGuideBranchWhen, known: Mapping[str, str]) -> bool | None:
    """分岐の条件が今の条件で成り立つか（条件の値が分からなければ None）。"""
    value = known.get(when.condition_id)
    if when.operator == "unknown":
        return value is None
    if value is None:
        return None
    return value in {_value(item) for item in when.values}


def _branch_decisions(
    content: SupportGuideContent, conditions: Mapping[str, str]
) -> list[tuple[str, bool | None]]:
    """分岐ごとの (行き先の手順, 当たるか)。当たる True / 外れる False / 決まらない None。

    どの分岐にも当たらない値（選択肢に無い値など）は、分岐を決められない（外さない。#1320）。
    """
    known = {key: _value(value) for key, value in conditions.items() if value and value.strip()}
    decided = [(branch, _branch_matches(branch.when, known)) for branch in content.branches]
    matched_conditions = {branch.when.condition_id for branch, matched in decided if matched}
    return [
        (
            branch.goto_step,
            False
            if matched is False and branch.when.condition_id in matched_conditions
            else (True if matched else None),
        )
        for branch, matched in decided
    ]


def _with_dependents(content: SupportGuideContent, steps: set[str], stop: set[str]) -> set[str]:
    """``steps`` と、``steps`` の手順だけに依存する後続の手順（``stop`` の手順は足さない）。"""
    result = set(steps)
    changed = True
    while changed:
        changed = False
        for step in content.steps:
            if step.id in result or step.id in stop or not step.depends_on:
                continue
            if all(dependency in result for dependency in step.depends_on):
                result.add(step.id)
                changed = True
    return result


def excluded_steps(content: SupportGuideContent, conditions: Mapping[str, str]) -> set[str]:
    """既知の条件に当たらない分岐の手順（と、それだけに依存する手順）。"""
    decisions = _branch_decisions(content, conditions)
    taken = {step for step, applies in decisions if applies is not False}
    skipped = {step for step, applies in decisions if applies is False}
    return _with_dependents(content, skipped - taken, taken)


def conditional_steps(content: SupportGuideContent, conditions: Mapping[str, str]) -> set[str]:
    """分かっている条件で決まらない分岐の手順（と、それだけに依存する後続の手順。#2708）。

    どの場合に当たるかで要否が変わるので、回答の充足では必須の手順に数えない（RAG の回答の
    `support_guide_runtime.conditional_step_ids` と同じ考え）。
    """
    decisions = _branch_decisions(content, conditions)
    decided = {step for step, applies in decisions if applies}
    undecided = {step for step, applies in decisions if applies is None} - decided
    return _with_dependents(content, undecided, decided | excluded_steps(content, conditions))


def _premise_met(text: str) -> bool:
    normalized = _normalized(text)
    return any(
        marker in normalized
        for marker in current_pack().words("guide_check_cues", "premise_met_markers")
    )


def instruction_text(answer: str) -> str:
    """手順の照合に使う本文（実行の指示の段落だけ。#1491）。

    満たした前提の言及は、手順の名前を含んでも手順の実行ではない（例: 「部門長の承認を得る必要があり
    → この前提は満たしています。」）。その行と、見出しが満たした前提の節（「削除の前提（満たしている
    条件）」）の行は除く。節は次の見出しか、空行の後の箇条書きではない行まで続く。
    """
    kept: list[str] = []
    section = False
    blank = False
    for line in answer.split("\n"):
        if not line.strip():
            blank = True
            continue
        if blank and not _list_item().match(line):
            section = False
        blank = False
        if _heading().match(line):
            section = _premise_met(line)
            if section:
                continue
        elif section or _premise_met(line):
            continue
        kept.append(line)
    return "\n".join(kept)


# 番号つきの手順の行（「1.」「1)」「(1)」「①」で始まる行）。
# 実行の指示の列の中の段落かを決める（#2732）。
_NUMBERED_STEP_LINE = re.compile(r"^\s*(?:[0-9０-９]+[.．、)）]|[（(][0-9０-９]+[)）]|[①-⑳])")


def check_guide_steps(
    answer: str,
    content: SupportGuideContent,
    conditions: Mapping[str, str],
    located: Mapping[str, str] | None = None,
) -> list[AnswerFinding]:
    """回答に出た手順の順序・分岐・依存を業務ガイドの手順と照らす（実行の指示の段落だけ。#1491）。

    ``located`` は手順の充足の判定（`guide_step_validation`。#2708）が手順の説明にした段落
    （手順 id → 段落の本文）。その手順の位置は段落の位置にする（言い回しの違う回答でも順序を
    確かめる）。渡したときは、手順の名前が見つからないことを ``guide_steps_unmatched`` に
    しない（説明の無い手順は充足の判定が示す）。

    段落の位置で決めた手順の順序の逆は、どちらかの段落が番号つきの手順の行でなければ
    warning にする（#2732・#2795）。結論を先に書き、前の手順の内容を根拠として後ろに書く
    回答では、段落の並びは実行の順を表さない。番号つきの手順の列の中で後の手順を先に
    指示した逆と、手順の名前の位置で決めた逆は error のまま。
    """
    if not content.steps:
        return []
    normalized = _normalized(instruction_text(answer))
    titles = {step.id: step.title for step in content.steps}
    positions: dict[str, int] = {}
    # 段落の位置で決めた手順（名前の位置ではない）。
    by_paragraph: set[str] = set()
    numbered = {
        step_id
        for step_id, paragraph in (located or {}).items()
        if _NUMBERED_STEP_LINE.match(str(paragraph or ""))
    }
    for step in content.steps:
        paragraph = _normalized((located or {}).get(step.id, ""))
        title = _normalized(step.title)
        index = normalized.find(paragraph) if paragraph else -1
        if index >= 0:
            by_paragraph.add(step.id)
        elif len(title) >= _MIN_TITLE_CHARS:
            index = normalized.find(title)
        if index >= 0:
            positions[step.id] = index
    if not positions and located is not None:
        return []
    if not positions:
        return [
            AnswerFinding(
                "guide_steps",
                "guide_steps_unmatched",
                "warning",
                "業務ガイドの手順の名前が回答に見つからず、手順の順序を確かめられません。",
            )
        ]
    excluded = excluded_steps(content, conditions)
    findings: list[AnswerFinding] = []
    for step in content.steps:
        if step.id not in positions:
            continue
        if step.id in excluded:
            findings.append(
                AnswerFinding(
                    "guide_steps",
                    "step_wrong_branch",
                    "error",
                    f"手順「{step.title}」は、分かっている条件に当たらない場合の手順です。",
                    step_id=step.id,
                )
            )
            continue
        for dependency in step.depends_on:
            if dependency in excluded or dependency not in titles:
                continue
            if dependency not in positions:
                findings.append(
                    AnswerFinding(
                        "guide_steps",
                        "step_dependency_missing",
                        "warning",
                        f"手順「{step.title}」の前の手順「{titles[dependency]}」が回答にありません。",
                        step_id=step.id,
                        related_step_id=dependency,
                    )
                )
            elif positions[dependency] > positions[step.id]:
                paragraph_order = (step.id in by_paragraph or dependency in by_paragraph) and not (
                    {step.id, dependency} <= numbered
                )
                findings.append(
                    AnswerFinding(
                        "guide_steps",
                        "step_order",
                        "warning" if paragraph_order else "error",
                        f"手順「{step.title}」を、先に行う手順「{titles[dependency]}」より前に"
                        "書いています。"
                        + (
                            "（段落の並びで判定。実行の順の誤りとは限りません）"
                            if paragraph_order
                            else ""
                        ),
                        step_id=step.id,
                        related_step_id=dependency,
                    )
                )
    for step in content.steps:
        if step.id in positions or step.id in excluded:
            continue
        before = next((item for item in step.depends_on if item in positions), None)
        if before is not None:
            findings.append(
                AnswerFinding(
                    "guide_steps",
                    "step_following_missing",
                    "warning",
                    f"手順「{titles[before]}」の後の手順「{step.title}」が回答にありません。",
                    step_id=step.id,
                    related_step_id=before,
                )
            )
    return findings


def impact_applies(content: SupportGuideContent, conditions: Mapping[str, str]) -> bool:
    """業務ガイドの影響範囲・承認が、分かっている条件の場合に係るか（#1320）。

    影響範囲・承認が係る手順（`impact.steps`）を決めていない業務ガイドは、すべての場合に係る。
    決めていれば、分かっている条件で外れた分岐の手順（`excluded_steps`）だけのときに係らない。
    条件が分からず分岐を決められないときは外れた手順が無いので、係る（安全側）。
    """
    steps = content.impact.steps
    if not steps:
        return True
    excluded = excluded_steps(content, conditions)
    return not all(step in excluded for step in steps)


def check_impact(
    answer: str, content: SupportGuideContent, conditions: Mapping[str, str] | None = None
) -> list[AnswerFinding]:
    """業務ガイドの影響範囲（グループ / 全体）と承認の要否を回答に書いているか。

    分かっている条件（`conditions`）で影響範囲・承認が係る手順が当たらなければ確かめない
    （例: 個別の利用者に付与する回答に、グループへの付与の承認を求めない。#1320）。
    """
    if not impact_applies(content, conditions or {}):
        return []
    normalized = _normalized(answer)
    impact = content.impact
    findings: list[AnswerFinding] = []
    terms = impact_scope_terms().get(impact.scope, ())
    if terms and not any(_normalized(term) in normalized for term in terms):
        findings.append(
            AnswerFinding(
                "impact",
                "impact_scope_missing",
                "error",
                f"影響範囲（{IMPACT_SCOPE_LABELS[impact.scope]}）を回答に書いていません。",
            )
        )
    approval_terms = current_pack().words("guide_check_cues", "approval_terms")
    if impact.approval_required and not any(term in normalized for term in approval_terms):
        findings.append(
            AnswerFinding(
                "impact",
                "approval_missing",
                "error",
                "実施の前に承認が要ることを回答に書いていません。",
            )
        )
    return findings


__all__ = [
    "AnswerFinding",
    "check_guide_steps",
    "check_impact",
    "check_requests",
    "conditional_steps",
    "excluded_steps",
    "impact_applies",
    "instruction_text",
]
