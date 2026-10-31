"""保存した未確認の手順を、読み取りの写しで問題の発見と区別する。"""

from collections import Counter
from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from app.features.agent.answer_validation import (
    GUIDE_STEP_UNEXPLAINED,
    REVIEW_HEADING,
    REVIEW_INTRO,
    _section_lines,
    reason_item,
)
from app.features.agent.runtime import Artifact

GUIDE_UNVERIFIED_NOTICE = "ガイド手順の対応は未検証です。過去の確認結果は診断記録に残しています。"
GUIDE_HISTORY_NOTICE = "過去のガイド確認結果です。記録から手順の検査状態を判別できません。"


def _unverified(step: dict[str, Any]) -> bool:
    validation = step.get("validation")
    coverage = validation.get("body_coverage") if isinstance(validation, dict) else None
    if step.get("status") in {"unknown", "not_searched"}:
        return True
    return isinstance(coverage, dict) and coverage.get("execution") in {
        "not_run",
        "error",
        "timed_out",
        "partial",
        "unknown",
    }


def _project(content: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    result = content.get("result")
    if not isinstance(result, dict):
        return content, []
    steps = [step for step in result.get("guide_steps") or [] if isinstance(step, dict)]
    counts = Counter(str(step.get("step_id") or "") for step in steps)
    ids = {
        str(step.get("step_id"))
        for step in steps
        if step.get("step_id") and counts[str(step["step_id"])] == 1 and _unverified(step)
    }
    candidates = [
        reason_item("guide_check", finding.get("message"))
        for finding in result.get("findings") or []
        if isinstance(finding, dict)
        and finding.get("code") == GUIDE_STEP_UNEXPLAINED
        and str(finding.get("step_id")) in ids
        and finding.get("message")
    ]
    # 同じ表示行が別の確定した指摘にも対応する場合は、未確認だけの行と断定しない。
    blockers = [
        reason_item("guide_check", finding.get("message"))
        for finding in result.get("findings") or []
        if isinstance(finding, dict) and finding.get("code") != GUIDE_STEP_UNEXPLAINED
    ]
    candidates = [item for item in candidates if item not in blockers]
    warnings = content.get("answer_warnings")
    if not isinstance(warnings, list):
        return content, []
    removed = [item for item in warnings if isinstance(item, dict) and item in candidates]
    if not removed:
        return content, []
    view = deepcopy(content)
    view["answer_warnings"] = [item for item in warnings if item not in removed]
    view["history_diagnostics"] = {
        "projection": "guide_coverage_unverified",
        "answer_warnings": deepcopy(removed),
    }
    for key, count_key in (("notice", "items"), ("withheld", "listed")):
        if isinstance(view.get(key), dict) and isinstance(view[key].get(count_key), int):
            view[key][count_key] = max(0, view[key][count_key] - len(removed))
    return view, removed


def _footer(text: str, content: dict[str, Any], removed: list[dict[str, Any]]) -> str:
    notice = content.get("notice")
    if not isinstance(notice, dict) or REVIEW_HEADING not in [
        notice.get("heading"),
        *(notice.get("headings") or []),
    ]:
        return text
    marker = f"\n\n**{REVIEW_HEADING}**\n\n{REVIEW_INTRO}\n"
    body, separator, tail = text.rpartition(marker)
    if not separator:
        return text
    # 公開時と同じ生成形式で一致した行だけを、宣言された末尾の節から除く。
    _, generated = _section_lines({}, [], [], removed)
    lines = tail.split("\n")
    kept = [line for line in lines if line not in generated]
    return body + marker + "\n".join(kept) if any(line.strip() for line in kept) else body


def project_validation_artifacts(
    artifacts: Sequence[Artifact], *, allow_history_notice: bool = True
) -> list[Artifact]:
    """権限で伏せた後の成果物へ適用する。元の記録・主張の判定・本文の操作は変更しない。"""
    projected: list[Artifact] = []
    removed: list[dict[str, Any]] = []
    validation: dict[str, Any] = {}
    historical = False
    for artifact in artifacts:
        if artifact.kind == "answer_validation":
            content, changed = _project(artifact.content)
            removed.extend(changed)
            if changed:
                validation = artifact.content
                artifact = artifact.model_copy(update={"content": content})
            else:
                raw = artifact.content
                raw_result = raw.get("result")
                has_warning = any(
                    isinstance(item, dict) and item.get("code") == "guide_check"
                    for item in raw.get("answer_warnings") or []
                )
                if (
                    allow_history_notice
                    and has_warning
                    and (
                        not isinstance(raw_result, dict)
                        or not raw_result.get("guide_steps")
                        or not raw_result.get("findings")
                    )
                ):
                    historical = True
                    content = deepcopy(raw)
                    content["history_diagnostics"] = {
                        "projection": "guide_coverage_execution_not_recorded"
                    }
                    artifact = artifact.model_copy(update={"content": content})
        projected.append(artifact)
    if not removed and not historical:
        return projected
    result: list[Artifact] = []
    for artifact in projected:
        if artifact.kind == "answer":
            content = deepcopy(artifact.content)
            if removed and isinstance(content.get("text"), str):
                content["text"] = _footer(content["text"], validation, removed)
            existing = content.get("notice")
            notice = GUIDE_UNVERIFIED_NOTICE if removed else GUIDE_HISTORY_NOTICE
            content["notice"] = (
                existing
                if isinstance(existing, str) and notice in existing.split("\n")
                else f"{existing}\n{notice}"
                if isinstance(existing, str) and existing.strip()
                else notice
            )
            artifact = artifact.model_copy(update={"content": content})
        result.append(artifact)
    return result
