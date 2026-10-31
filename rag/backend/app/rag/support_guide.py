"""業務ガイド（SupportGuide。#1237）の検証と内容の指紋。

保存のたびと公開の前に、内容の整合（id の重複・依存の参照先と循環・分岐の行き先と網羅・道具）を
確かめる。公開の前だけ、参照する文書がプロファイルのナレッジベースにあり索引済みかも確かめる。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable, Sequence

from pydantic import BaseModel

from app.schemas.support_guide import (
    SUPPORT_GUIDE_BOOLEAN_VALUES,
    SUPPORT_GUIDE_TOOLS,
    SupportGuideChange,
    SupportGuideContent,
    SupportGuideDiffSection,
    SupportGuideIssue,
)


def content_sha256(content: SupportGuideContent) -> str:
    """内容の指紋（キーの順を固定した JSON の sha256）。"""
    payload = json.dumps(
        content.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _error(code: str, path: str, message: str) -> SupportGuideIssue:
    return SupportGuideIssue(severity="error", code=code, path=path, message=message)


def _warning(code: str, path: str, message: str) -> SupportGuideIssue:
    return SupportGuideIssue(severity="warning", code=code, path=path, message=message)


def _duplicates(ids: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for value in ids:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates


def _cycle(steps: dict[str, list[str]]) -> list[str] | None:
    """依存の循環（見つけた順の手順 id）。無ければ None。"""
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(step_id: str) -> list[str] | None:
        state[step_id] = 1
        stack.append(step_id)
        for dependency in steps.get(step_id, []):
            if dependency not in steps:
                continue
            if state.get(dependency) == 1:
                return [*stack[stack.index(dependency) :], dependency]
            if state.get(dependency) is None and (found := visit(dependency)):
                return found
        stack.pop()
        state[step_id] = 2
        return None

    for step_id in steps:
        if state.get(step_id) is None and (found := visit(step_id)):
            return found
    return None


def validate_content(content: SupportGuideContent) -> list[SupportGuideIssue]:
    """内容の整合を確かめる（文書の参照は確かめない）。"""
    issues: list[SupportGuideIssue] = []
    if not content.goal.intent_examples and not content.goal.match_terms:
        issues.append(
            _error(
                "goal_unmatchable",
                "goal",
                "質問の例か照合の語を 1 つ以上入れてください（どの質問に使うかを決められません）。",
            )
        )
    if not content.steps:
        issues.append(_error("steps_missing", "steps", "手順を 1 つ以上入れてください。"))
    for name, ids in (
        ("conditions", [condition.id for condition in content.conditions]),
        ("steps", [step.id for step in content.steps]),
        ("branches", [branch.id for branch in content.branches]),
        ("completion", [item.id for item in content.completion]),
    ):
        for duplicate in _duplicates(ids):
            issues.append(_error("duplicate_id", name, f"id「{duplicate}」が重複しています。"))

    step_ids = {step.id for step in content.steps}
    graph: dict[str, list[str]] = {}
    for index, step in enumerate(content.steps):
        graph[step.id] = list(step.depends_on)
        for dependency in step.depends_on:
            if dependency == step.id:
                issues.append(
                    _error(
                        "self_dependency",
                        f"steps[{index}].depends_on",
                        f"手順「{step.title}」が自分自身に依存しています。",
                    )
                )
            elif dependency not in step_ids:
                issues.append(
                    _error(
                        "unknown_dependency",
                        f"steps[{index}].depends_on",
                        f"手順「{step.title}」の依存先「{dependency}」がありません。",
                    )
                )
        unknown_tools = [tool for tool in step.allowed_tools if tool not in SUPPORT_GUIDE_TOOLS]
        if unknown_tools:
            issues.append(
                _error(
                    "unknown_tool",
                    f"steps[{index}].allowed_tools",
                    f"手順「{step.title}」の道具「{'、'.join(unknown_tools)}」は使えません。",
                )
            )
    if cycle := _cycle({key: [d for d in value if d != key] for key, value in graph.items()}):
        issues.append(
            _error("dependency_cycle", "steps", f"手順の依存が循環しています: {' → '.join(cycle)}")
        )

    conditions = {condition.id: condition for condition in content.conditions}
    covered: dict[str, set[str]] = {}
    has_unknown: set[str] = set()
    for index, branch in enumerate(content.branches):
        condition = conditions.get(branch.when.condition_id)
        if condition is None:
            issues.append(
                _error(
                    "unknown_condition",
                    f"branches[{index}].when",
                    f"分岐「{branch.id}」の条件「{branch.when.condition_id}」がありません。",
                )
            )
            continue
        if branch.goto_step not in step_ids:
            issues.append(
                _error(
                    "unknown_goto",
                    f"branches[{index}].goto_step",
                    f"分岐「{branch.id}」の行き先「{branch.goto_step}」の手順がありません。",
                )
            )
        if branch.when.operator == "unknown":
            has_unknown.add(condition.id)
            continue
        if condition.type == "enum":
            invalid = [
                value for value in branch.when.values if value not in condition.allowed_values
            ]
            if invalid:
                issues.append(
                    _error(
                        "unknown_value",
                        f"branches[{index}].when.values",
                        f"分岐「{branch.id}」の値「{'、'.join(invalid)}」は"
                        f"条件「{condition.label}」の選択肢にありません。",
                    )
                )
        if condition.type == "boolean":
            invalid = [
                value
                for value in branch.when.values
                if value.casefold() not in SUPPORT_GUIDE_BOOLEAN_VALUES
            ]
            if invalid:
                issues.append(
                    _error(
                        "unknown_value",
                        f"branches[{index}].when.values",
                        f"分岐「{branch.id}」の値は、はい／いいえを指定してください。",
                    )
                )
        covered.setdefault(condition.id, set()).update(branch.when.values)
    for index, condition in enumerate(content.conditions):
        if condition.unknown_handling == "branch" and condition.id not in has_unknown:
            issues.append(
                _error(
                    "unknown_branch_missing",
                    f"conditions[{index}]",
                    f"条件「{condition.label}」は不明のとき分岐しますが、"
                    "unknown の分岐がありません。",
                )
            )
        if condition.type == "enum" and condition.id in covered:
            missing = [
                value for value in condition.allowed_values if value not in covered[condition.id]
            ]
            if missing:
                issues.append(
                    _warning(
                        "branch_not_covered",
                        f"conditions[{index}]",
                        f"条件「{condition.label}」の「{'、'.join(missing)}」に当たる分岐がありません。",
                    )
                )
    unknown_impact_steps = [step for step in content.impact.steps if step not in step_ids]
    if unknown_impact_steps:
        issues.append(
            _error(
                "unknown_impact_step",
                "impact.steps",
                f"影響範囲・承認が係る手順「{'、'.join(unknown_impact_steps)}」がありません。",
            )
        )
    if content.impact.scope != "individual" and not content.impact.approval_required:
        issues.append(
            _warning(
                "wide_impact_without_approval",
                "impact",
                "影響範囲がグループ・全体なのに、承認を求めていません。",
            )
        )
    return issues


DocumentLookup = Callable[[str], Awaitable[str | None]]


async def reference_issues(
    content: SupportGuideContent, lookup: DocumentLookup
) -> list[SupportGuideIssue]:
    """参照する文書が使えるか（公開の前だけ。ガイドの資料と手順の資料）。

    ``lookup`` は文書 ID から問題（見つからない・プロファイルの対象外・索引前など）の説明を返し、
    使えれば None を返す。
    """
    issues: list[SupportGuideIssue] = []
    located = [
        (f"references[{index}]", reference) for index, reference in enumerate(content.references)
    ]
    # 手順の参照（#2395）も同じく確かめる。
    for step_index, step in enumerate(content.steps):
        located.extend(
            (f"steps[{step_index}].references[{index}]", reference)
            for index, reference in enumerate(step.references)
        )
    problems: dict[str, str | None] = {}
    for path, reference in located:
        if reference.document_id not in problems:
            problems[reference.document_id] = await lookup(reference.document_id)
        problem = problems[reference.document_id]
        if problem:
            name = reference.title or reference.document_id
            issues.append(_error("reference_unavailable", path, f"資料「{name}」: {problem}"))
    return issues


def has_errors(issues: Sequence[SupportGuideIssue]) -> bool:
    return any(issue.severity == "error" for issue in issues)


def _changed_fields(before: BaseModel, after: BaseModel) -> list[str]:
    old, new = before.model_dump(mode="json"), after.model_dump(mode="json")
    return [name for name in new if old.get(name) != new.get(name)]


def _object_change(
    section: SupportGuideDiffSection,
    before: BaseModel,
    after: BaseModel,
    *,
    label: str = "",
) -> list[SupportGuideChange]:
    fields = _changed_fields(before, after)
    if not fields:
        return []
    return [SupportGuideChange(section=section, kind="changed", label=label, fields=fields)]


def _row_changes(
    section: SupportGuideDiffSection,
    before: Sequence[BaseModel],
    after: Sequence[BaseModel],
    key: Callable[[BaseModel], str],
    label: Callable[[BaseModel], str],
) -> list[SupportGuideChange]:
    """id（資料は document_id）で行を突き合わせ、追加・削除・変更を並べる。

    並びは取り込む側の順で、削除は後ろ。
    同じ id が重複する行は最初の行で比べる（重複は検証が別に問題として出す）。
    """
    old = {key(row): row for row in reversed(before)}
    new_keys: list[str] = []
    changes: list[SupportGuideChange] = []
    for row in after:
        row_key = key(row)
        if row_key in new_keys:
            continue
        new_keys.append(row_key)
        previous = old.get(row_key)
        if previous is None:
            changes.append(
                SupportGuideChange(section=section, kind="added", key=row_key, label=label(row))
            )
        elif fields := _changed_fields(previous, row):
            changes.append(
                SupportGuideChange(
                    section=section, kind="changed", key=row_key, label=label(row), fields=fields
                )
            )
    common = set(old) & set(new_keys)
    old_order = list(dict.fromkeys(key(row) for row in before if key(row) in common))
    new_order = [row_key for row_key in new_keys if row_key in common]
    if old_order != new_order:
        for row_key in new_order:
            if old_order.index(row_key) == new_order.index(row_key):
                continue
            changed = next(
                (item for item in changes if item.key == row_key and item.kind == "changed"), None
            )
            if changed is not None:
                changed.fields.append("order")
            else:
                changes.append(
                    SupportGuideChange(
                        section=section,
                        kind="changed",
                        key=row_key,
                        label=label(old[row_key]),
                        fields=["order"],
                    )
                )
    seen: set[str] = set()
    for row in before:
        row_key = key(row)
        if row_key in new_keys or row_key in seen:
            continue
        seen.add(row_key)
        changes.append(
            SupportGuideChange(section=section, kind="removed", key=row_key, label=label(row))
        )
    return changes


def _attr(name: str) -> Callable[[BaseModel], str]:
    return lambda row: str(getattr(row, name))


def diff_contents(
    before: SupportGuideContent, after: SupportGuideContent
) -> list[SupportGuideChange]:
    """既存のガイド（before）と取り込むガイド（after）の違い（#1288。モデルは呼ばない）。

    条件・手順・分岐・完了の条件は id、資料は document_id で突き合わせる。目的・適用範囲・
    影響範囲・引き継ぎは項目ごとの変更として出す。同じ内容なら空。
    """
    basic = [
        name for name in ("title", "description") if getattr(before, name) != getattr(after, name)
    ]
    changes = (
        [SupportGuideChange(section="basic", kind="changed", label=after.title, fields=basic)]
        if basic
        else []
    )
    return [
        *changes,
        *_object_change("goal", before.goal, after.goal),
        *_object_change("applicability", before.applicability, after.applicability),
        *_row_changes(
            "conditions", before.conditions, after.conditions, _attr("id"), _attr("label")
        ),
        *_row_changes("steps", before.steps, after.steps, _attr("id"), _attr("title")),
        *_row_changes(
            "branches",
            before.branches,
            after.branches,
            _attr("id"),
            lambda row: str(getattr(row, "note", "") or getattr(row, "id", "")),
        ),
        *_row_changes(
            "references",
            before.references,
            after.references,
            _attr("document_id"),
            lambda row: str(getattr(row, "title", "") or getattr(row, "document_id", "")),
        ),
        *_row_changes(
            "completion", before.completion, after.completion, _attr("id"), _attr("description")
        ),
        *_object_change("impact", before.impact, after.impact),
        *_object_change("handoff", before.handoff, after.handoff),
    ]


__all__ = [
    "content_sha256",
    "diff_contents",
    "has_errors",
    "reference_issues",
    "validate_content",
]
