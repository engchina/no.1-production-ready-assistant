"""確認・生成の用途、明示優先度、構造化排他と版の記録。意味判定と外部 I/O は使わない。"""

import json
from dataclasses import replace

from rag_engine.knowledge.runtime_knowledge import build_runtime_knowledge_context


def rule(identity, content="資料を確認する。", **fields):
    return {"id": identity, "title": "設定", "triggers": ["設定"], "content": content, **fields}


def context(tmp_path, *rules, limit=6):
    (tmp_path / "runtime_knowledge.json").write_text(json.dumps({"rules": list(rules)}, ensure_ascii=False))
    return build_runtime_knowledge_context("設定の手順", tmp_path, rule_limit=limit)


def generation_ids(ctx):
    return [r.rule_id for r in ctx.rules_for_stage("generation")]


def test_unset_rules_keep_generation_and_confirmation_without_dummy_content(tmp_path):
    confirmation = {"question": "対象は？", "options": []}
    ctx = context(
        tmp_path, rule("legacy"), rule("confirm", "", purpose="confirmation", clarification=confirmation), rule("off", status="deprecated")
    )
    assert generation_ids(ctx) == ["legacy"]
    assert [r.rule_id for r in ctx.rules_for_stage("confirmation")] == ["confirm"]
    assert "id=legacy" in ctx.prompt_context() and "id=confirm" not in ctx.prompt_context()
    assert ctx.matched_rules[0].purpose == "both"
    assert not any(d["id"] == "off" and d["accepted"] for d in ctx.rule_applications)


def test_explicit_priority_wins_before_ranking_and_budget_is_recorded(tmp_path):
    ctx = context(tmp_path, rule("a", "説明 A"), rule("b", "説明 B", priority=4), rule("c", "説明 C"), limit=1)
    assert generation_ids(ctx) == ["b"]
    assert {d["id"] for d in ctx.rule_applications if d["reason"] == "application_budget"} == {"a", "c"}


def test_exclusive_conflict_holds_only_the_group_and_never_passes_conflicting_content(tmp_path):
    ctx = context(tmp_path, rule("a", "指示 A", exclusive_group="g"), rule("b", "指示 B", exclusive_group="g"), rule("c", "併用指示"))
    assert generation_ids(ctx) == ["c"]
    assert "指示 A" not in ctx.prompt_context() and "指示 B" not in ctx.prompt_context()
    assert {d["id"] for d in ctx.rule_applications if d["reason"] == "unresolved_exclusive_conflict"} == {"a", "b"}
    higher = context(tmp_path, rule("a", "指示 A", exclusive_group="g", priority=1), rule("b", "指示 B", exclusive_group="g"))
    assert generation_ids(higher) == ["a"]
    assert any(d["id"] == "b" and d["reason"] == "lower_priority" for d in higher.rule_applications)


def test_conflict_and_budget_are_stage_specific(tmp_path):
    confirmation = {"question": "対象は？", "options": []}
    ctx = context(
        tmp_path,
        rule("confirm", "", purpose="confirmation", exclusive_group="g", clarification=confirmation),
        rule("generate", "指示 B", purpose="generation", exclusive_group="g"),
    )
    assert generation_ids(ctx) == ["generate"]
    assert [r.rule_id for r in ctx.rules_for_stage("confirmation")] == ["confirm"]
    assert not any(d["reason"] == "unresolved_exclusive_conflict" for d in ctx.rule_applications)


def test_compatible_instructions_combine_and_exact_duplicates_are_recorded(tmp_path):
    ctx = context(
        tmp_path,
        rule("a", "全手順を説明する。"),
        rule("b", "簡潔に書く。", instruction_kind="presentation"),
        rule("c", "簡潔に書く。", instruction_kind="presentation"),
    )
    assert generation_ids(ctx) == ["a", "b"]
    assert any(d["id"] == "c" and d["reason"] == "duplicate_instruction" for d in ctx.rule_applications)


def test_snapshots_use_the_executed_version_and_remain_stable_after_updates(tmp_path):
    first = context(tmp_path, rule("a", "旧指示"))
    snapshot = first.to_payload()
    original = snapshot["matched_rules"][0]["version"]
    second = context(tmp_path, rule("a", "新指示"))
    assert second.matched_rules[0].version != original
    assert snapshot["matched_rules"][0]["content"] == "旧指示"
    assert snapshot["rule_applications"][0]["version"] == original
    resolved = replace(first, matched_rules=(replace(first.matched_rules[0], content="条件解決後の指示"),))
    executed = resolved.to_payload()
    assert executed["matched_rules"][0]["version"] != original
    assert all(d["version"] == executed["matched_rules"][0]["version"] for d in executed["rule_applications"])


def test_matching_eligibility_does_not_claim_application_after_conflict(tmp_path):
    ctx = context(tmp_path, rule("a", "指示 A", exclusive_group="g"), rule("b", "指示 B", exclusive_group="g"))
    assert ctx.is_active and not ctx.has_matches
    assert ctx.prompt_context() == ""
    assert all(d["matching_eligible"] and not d["accepted"] for d in ctx.expansion_decisions)
    assert all(d["reason"] == "unresolved_exclusive_conflict" for d in ctx.expansion_decisions)


def test_a_rule_without_confirmation_cannot_shadow_a_configured_confirmation(tmp_path):
    confirmation = {"question": "対象は？", "options": []}
    ctx = context(
        tmp_path,
        rule("generation", "生成の指示", priority=10, exclusive_group="g"),
        rule("confirmation", "", purpose="confirmation", priority=0, exclusive_group="g", clarification=confirmation),
    )
    assert generation_ids(ctx) == ["generation"]
    assert [r.rule_id for r in ctx.rules_for_stage("confirmation")] == ["confirmation"]
