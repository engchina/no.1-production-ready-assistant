"""業務ガイドを回答に使う（#1238）。照合・条件の状態・進め方・確認・MCP。

内容は架空の「サンプル業務ポータル」の例だけを使う。
"""

from __future__ import annotations

import json
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch
from rag_engine.knowledge.runtime_knowledge import (
    MAX_TEXT_CHARS,
    RUNTIME_KNOWLEDGE_FILE_NAME,
    build_runtime_knowledge_context,
)

from app.api.routes import search as search_route
from app.api.routes import search_answer_profile_knowledge as knowledge_route
from app.config import get_settings
from app.main import app
from app.mcp import tools as mcp_tools
from app.rag.search_answer_profile_config import SearchAnswerProfileConfig
from app.rag.support_guide_runtime import (
    GUIDE_LOAD_FAILED_KEY,
    GUIDE_PREVIEW_KEY,
    GUIDE_PREVIEW_TRACE_PREFIX,
    GuidePreview,
    apply_guide_to_diagnostics,
    build_guide_context,
    clarification_questions,
    guide_clarification,
    guide_clarification_record,
    guide_rule,
    guide_short_circuit_outcome,
    impact_applies,
    is_guide_preview_record,
    is_guide_preview_trace,
    match_guide,
    rank_guides,
    resolve_guide_clarification,
    short_circuit_answer,
    visible_plan,
    with_draft,
    with_guide_rule,
)
from app.schemas.search import SearchDiagnostics, SearchRequest, SearchResponse
from app.schemas.support_guide import SupportGuideContent
from tests.support import AsgiTestClient
from tests.test_mcp_api import _call, _token, auth  # noqa: F401 - fixture を使う
from tests.test_search_answer_profile_domain_keywords import FakeKnowledgeOracle

client = AsgiTestClient(app)


def grant_guide(**overrides: Any) -> SupportGuideContent:
    payload: dict[str, Any] = {
        "title": "アクセス権限を付与する",
        "goal": {
            "expected_result": "対象の利用者が機能を使える",
            "intent_examples": ["アクセス権限を付与したい"],
            "match_terms": ["権限を付与", "アクセス権限"],
        },
        "conditions": [
            {
                "id": "target",
                "label": "付与先",
                "allowed_values": ["個別", "グループ"],
                "question": "権限は個別の利用者とグループのどちらに付けますか？",
            },
            {
                "id": "approved",
                "label": "部門長の承認",
                "type": "boolean",
                "required": False,
                "unknown_handling": "branch",
            },
        ],
        "steps": [
            {"id": "open", "title": "詳細画面の権限タブを開く", "retrieval_hints": ["権限タブ"]},
            {"id": "grant", "title": "権限を選んで付与する", "depends_on": ["open"]},
        ],
        "branches": [
            {
                "id": "b1",
                "when": {"condition_id": "target", "values": ["グループ"]},
                "goto_step": "grant",
                "note": "承認を得てから",
            },
        ],
        "impact": {"scope": "group", "approval_required": True},
        "handoff": {"conditions": ["承認者が不在"], "contact": "サポート窓口"},
    }
    payload.update(overrides)
    return SupportGuideContent.model_validate(payload)


def export_guide() -> SupportGuideContent:
    return SupportGuideContent.model_validate(
        {
            "title": "地域別集計表を出力する",
            "goal": {"expected_result": "集計表を得る", "match_terms": ["地域別集計表"]},
            "steps": [{"id": "s1", "title": "集計メニューから出力する"}],
        }
    )


GUIDES = [("g-grant", 2, grant_guide()), ("g-export", 1, export_guide())]


# ---- 照合と条件 --------------------------------------------------------------------------


def test_match_picks_the_best_guide_and_reads_conditions() -> None:
    match = match_guide(GUIDES, "アクセス権限を付与したいです")
    assert match is not None
    assert (match.guide_id, match.revision) == ("g-grant", 2)
    # 付与先が分からず、確かめる（承認は必須でないので聞かない）。
    assert match.decision == "clarify"
    assert [condition.id for condition in match.unknown] == ["target"]

    # 質問に選択肢の語が 1 つだけ出れば既知。
    known = match_guide(GUIDES, "グループにアクセス権限を付与したい")
    assert known is not None and known.decision == "answer"
    assert known.summary()["known_conditions"][0] == {
        "id": "target",
        "label": "付与先",
        "value": "グループ",
        "source": "question",
        "state": "known",
    }
    # 両方出たら決めつけない（矛盾。#1278 で別に確かめる）。
    assert match_guide(GUIDES, "個別とグループの権限を付与したい").decision == "clarify"  # type: ignore[union-attr]
    # 渡した値（確認の答え）を優先する。不正な値は使わない。
    assert match_guide(GUIDES, "権限を付与", {"target": "個別"}).decision == "answer"  # type: ignore[union-attr]
    assert match_guide(GUIDES, "権限を付与", {"target": "全員"}).decision == "clarify"  # type: ignore[union-attr]
    # 言い換え（value_aliases）が質問に出れば、その選択肢が分かっているとみなす。
    aliased = [
        (
            "g-a",
            1,
            grant_guide(
                conditions=[
                    {
                        "id": "target",
                        "label": "付与先",
                        "allowed_values": ["個別", "グループ"],
                        "question": "どちらですか？",
                        "value_aliases": {"個別": ["検証用アカウント"]},
                    }
                ]
            ),
        )
    ]
    by_alias = match_guide(aliased, "検証用アカウントにアクセス権限を付与したい")
    assert by_alias is not None and by_alias.decision == "answer"
    assert by_alias.summary()["known_conditions"][0]["value"] == "個別"
    # チャット（送信の前に確かめる）では、残った不明の条件は分岐で答える。
    assert match_guide(GUIDES, "権限を付与", interactive=True).decision == "branch"  # type: ignore[union-attr]

    assert match_guide(GUIDES, "パスワードの長さは？") is None
    expired = [("g-old", 1, grant_guide(applicability={"effective_to": "2026-01-01"}))]
    assert match_guide(expired, "権限を付与", today=date(2026, 10, 7)) is None


def test_handoff_and_ranking() -> None:
    handoff = grant_guide(
        conditions=[
            {
                "id": "target",
                "label": "付与先",
                "allowed_values": ["個別", "グループ"],
                "unknown_handling": "handoff",
            }
        ]
    )
    match = match_guide([("g", 1, handoff)], "権限を付与したい")
    assert match is not None and match.decision == "handoff"
    assert "サポート窓口へお問い合わせください" in short_circuit_answer(match)

    ranked = rank_guides(GUIDES, "アクセス権限を付与して地域別集計表も出したい", limit=5)
    assert [item.guide_id for item in ranked] == ["g-grant", "g-export"]
    assert rank_guides(GUIDES, "アクセス権限を付与", limit=1)[0].guide_id == "g-grant"


def test_guide_becomes_a_pinned_rule_with_steps_and_hints() -> None:
    match = match_guide(GUIDES, "グループにアクセス権限を付与したい")
    assert match is not None
    rule = guide_rule(match)
    assert rule["tags"] == ["pinned", "support_guide"]
    assert rule["triggers"] == ["権限タブ"]
    assert "分かっている条件: 付与先=グループ" in rule["content"]
    # 別の場合に限った資料の記載だけを外し、残った手順そのものを外す指示は出さない（#2813）。
    assert (
        "資料にある、付与先が「グループ」以外の場合に限った手順・注意は答えに含めない。"
        in rule["content"]
    )
    assert "当たる場合の手順・注意だけを答え" not in rule["content"]
    assert "手順 2（grant）. 権限を選んで付与する（open の後）" in rule["content"]
    assert "必要な根拠:" not in rule["content"]
    assert "実施の前に承認が要る" in rule["content"]
    payload = with_guide_rule({"terms": [{"term": "x"}], "rules": [{"id": "r1"}]}, match)
    assert [item["id"] for item in payload["rules"]] == ["guide-g-grant", "r1"]
    assert payload["terms"] == [{"term": "x"}]


def test_long_guide_rule_reaches_the_generation_input_to_its_last_line(tmp_path: Path) -> None:
    """800 字を超える業務ガイドのルールも、最後の制約の行まで生成の入力に渡す（#3838）。"""
    steps = [{"id": "open", "title": "詳細画面の権限タブを開く", "retrieval_hints": ["権限タブ"]}]
    steps += [
        {
            "id": f"s{index}",
            "title": f"合成の手順 {index} の操作を行う",
            "depends_on": ["open"],
            "purpose": f"合成の目的 {index} を満たす",
            "done_when": f"合成の画面 {index} に完了が表示される",
        }
        for index in range(1, 12)
    ]
    steps.append({"id": "grant", "title": "権限を選んで付与する", "depends_on": ["open"]})
    guide = grant_guide(
        steps=steps,
        completion=[
            {
                "id": "c1",
                "description": "合成の完了の状態になる",
                "check_method": "合成の一覧で状態を確かめる",
            }
        ],
    )
    match = match_guide([("g-long", 1, guide)], "グループにアクセス権限を付与したい")
    assert match is not None
    rule = guide_rule(match)
    assert len(rule["content"]) > MAX_TEXT_CHARS
    (tmp_path / RUNTIME_KNOWLEDGE_FILE_NAME).write_text(
        json.dumps(with_guide_rule({}, match), ensure_ascii=False), encoding="utf-8"
    )
    prompt = build_runtime_knowledge_context(
        "グループにアクセス権限を付与したい", tmp_path
    ).prompt_context()
    assert "[Runtime Glossary / Rules]" in prompt
    lines = rule["content"].split("\n")
    for prefix in ("完了の条件", "人へ引き継ぐ条件", "資料で確かめられない"):
        assert any(line.startswith(prefix) for line in lines)
    # 読み込みは NFKC で正規化し、改行を空白に畳む（他のルールと同じ）。行は落とさない。
    for line in lines:
        assert " ".join(unicodedata.normalize("NFKC", line).split()) in prompt


def test_guide_rule_includes_only_visible_step_evidence_requirements() -> None:
    content = grant_guide(
        steps=[
            {
                "id": "open",
                "title": "詳細画面を開く",
                "evidence_requirements": ["入口のメニュー", "対象を選択する条件"],
            },
            {
                "id": "grant",
                "title": "グループの権限を付与する",
                "depends_on": ["open"],
                "evidence_requirements": ["グループの承認者"],
            },
        ]
    )
    single = match_guide([("g", 1, content)], "個別の利用者にアクセス権限を付与したい")
    assert single is not None
    single_text = guide_rule(single)["content"]
    for requirement in content.steps[0].evidence_requirements:
        assert single_text.count(requirement) == 1
    assert "グループの承認者" not in single_text

    group = match_guide([("g", 1, content)], "グループにアクセス権限を付与したい")
    assert group is not None
    group_text = guide_rule(group)["content"]
    assert group_text.count("グループの承認者") == 1
    assert group_text.index("対象を選択する条件") < group_text.index("手順 2（grant）")
    assert group_text.index("手順 2（grant）") < group_text.index("グループの承認者")


def test_short_circuit_and_diagnostics() -> None:
    match = match_guide(GUIDES, "アクセス権限を付与したい")
    assert match is not None
    questions = clarification_questions(match)
    assert questions == [
        {
            "condition_id": "target",
            "label": "付与先",
            "question": "権限は個別の利用者とグループのどちらに付けますか？",
            "options": ["個別", "グループ"],
        }
    ]
    guide = {
        **match.summary(),
        "clarifications": questions,
        "short_answer": short_circuit_answer(match),
    }
    outcome = guide_short_circuit_outcome(guide)
    assert outcome.citations == []
    assert outcome.diagnostics["outcome"] == "needs_clarification"
    assert outcome.diagnostics["envelope"]["clarifications"] == questions
    assert outcome.diagnostics["guide"]["guide_id"] == "g-grant"
    assert "（個別／グループ）" in outcome.answer

    branch = {
        **match.summary(),
        "decision": "branch",
        "unknown_conditions": [{"id": "target", "label": "付与先", "handling": "ask"}],
    }
    diagnostics: dict[str, Any] = {
        "outcome": "answered",
        "envelope": {"outcome": "answered", "conditions": []},
    }
    apply_guide_to_diagnostics(diagnostics, branch)
    assert diagnostics["outcome"] == "conditional"
    assert diagnostics["envelope"]["conditions"] == ["付与先"]
    assert diagnostics["envelope"]["guide"]["revision"] == 2


def test_chat_clarification_round_trip() -> None:
    match = match_guide(GUIDES, "アクセス権限を付与したい")
    assert match is not None
    found = guide_clarification(match)
    assert found is not None
    rule_id, title, clarification = found
    assert (rule_id, title) == ("guide:g-grant:target", "アクセス権限を付与する")
    assert [option.label for option in clarification.options] == ["個別", "グループ"]
    assert clarification.multiple is False
    resolved = resolve_guide_clarification(GUIDES, rule_id, ["o2"])
    assert resolved is not None
    conditions, context = resolved
    assert conditions == {"target": "グループ"}
    assert "選んだ答え: グループ" in context
    assert resolve_guide_clarification(GUIDES, rule_id, ["o9"]) is None
    assert resolve_guide_clarification(GUIDES, "guide:missing:target", ["o1"]) is None
    assert resolve_guide_clarification(GUIDES, "R1", ["o1"]) is None


def test_guide_clarification_accepts_other_text_without_setting_the_value() -> None:
    """業務ガイドの確認にも「その他（自由入力）」を出す（#1839）。値には当てはめず前提の文で渡す。"""
    match = match_guide(GUIDES, "アクセス権限を付与したい")
    assert match is not None
    found = guide_clarification(match)
    assert found is not None
    rule_id, _title, clarification = found
    assert clarification.allow_other is True
    resolved = resolve_guide_clarification(GUIDES, rule_id, [], "部署の共有フォルダ")
    assert resolved is not None
    conditions, context = resolved
    assert conditions == {}
    assert "その他（利用者の入力）: 部署の共有フォルダ" in context
    assert "当てはまる値を確かめる" in context
    both = resolve_guide_clarification(GUIDES, rule_id, ["o1"], "急ぎ")
    assert both is not None
    assert both[0] == {"target": "個別"}
    assert "その他（利用者の入力）: 急ぎ" in both[1]
    assert resolve_guide_clarification(GUIDES, rule_id, [], "  ") is None


def test_guide_clarification_record_for_the_conversation() -> None:
    """会話に残す「回答前に確認した内容」（#1839）。"""
    match = match_guide(GUIDES, "アクセス権限を付与したい")
    assert match is not None
    found = guide_clarification(match)
    assert found is not None
    rule_id, _title, clarification = found
    record = guide_clarification_record(GUIDES, rule_id, ["o2"], "急ぎ")
    assert record == {
        "outcome": "answered",
        "entries": [
            {"prompt": clarification.question, "selected": ["グループ"], "other_text": "急ぎ"}
        ],
    }
    assert guide_clarification_record(GUIDES, rule_id, None) == {
        "outcome": "skipped",
        "entries": [{"prompt": clarification.question}],
    }
    assert guide_clarification_record(GUIDES, "guide:missing:target", None) is None


def test_guide_clarification_record_marks_free_text_conditions() -> None:
    """決まった選択肢の無い条件（text）の答えは、記録に `free_text_only` を残す（#3410）。"""
    guide = grant_guide(
        conditions=[{"id": "name", "label": "対象の名前", "type": "text", "question": "名前は？"}]
    )
    record = guide_clarification_record([("g-text", 1, guide)], "guide:g-text:name", [], " 名前 A ")
    assert record == {
        "outcome": "answered",
        "entries": [
            {"prompt": "名前は？", "selected": [], "other_text": "名前 A", "free_text_only": True}
        ],
    }
    # 選択肢のある条件（enum・boolean）は付けない（自由入力の答えは「その他: 」のまま）。
    enum = guide_clarification_record(GUIDES, "guide:g-grant:target", [], "その他の値")
    assert enum == {
        "outcome": "answered",
        "entries": [
            {
                "prompt": "権限は個別の利用者とグループのどちらに付けますか？",
                "selected": [],
                "other_text": "その他の値",
            }
        ],
    }


# ---- 適用範囲・条件の出所・矛盾・分岐の絞り込み（#1278） ---------------------------------


def _scoped(guide_id: str, **applicability: Any) -> tuple[str, int, SupportGuideContent]:
    return (guide_id, 1, grant_guide(applicability=applicability))


def test_applicability_excludes_guides_named_out_of_scope() -> None:
    hr = _scoped("g-hr", business_domains=["人事"], versions=["v2"], object_types=["アカウント"])
    # 手がかりが無い項目は確かめていない（unverified）として使い、要約に出す。
    match = match_guide([hr], "グループにアクセス権限を付与したい")
    assert match is not None
    assert match.applicability == {
        "business_domains": "unverified",
        "object_types": "unverified",
        "versions": "unverified",
    }
    assert match.summary()["applicability"]["business_domains"] == "unverified"
    rule = guide_rule(match)["content"]
    assert "適用範囲のうち質問から確かめられていない項目: 業務（人事）" in rule
    assert "資料・システムの版（v2）" in rule

    # 質問・絞り込みが名指しした値と合えば matched、別の値だけを名指ししていれば使わない。
    named = match_guide([hr], "人事のアカウントにアクセス権限を付与したい（v2）")
    assert named is not None
    assert named.applicability == {
        "business_domains": "matched",
        "object_types": "matched",
        "versions": "matched",
    }
    context = build_guide_context(
        "経理のアクセス権限を付与したい", [hr], business_vocabulary=["01_人事", "02_経理"]
    )
    assert context.business_domains == frozenset({"経理"})
    assert match_guide([hr], "経理のアクセス権限を付与したい", context=context) is None
    # 英数字の版は語の途中に当てない（v2 を v20 の中に見つけない）。v20 だけなら版は名指しなし。
    v20 = build_guide_context("v20 でアクセス権限を付与したい", [hr])
    assert v20.versions == frozenset()
    # 絞り込み（large_category・document_version）も手がかりにする（番号の接頭辞は外して比べる）。
    filtered = build_guide_context(
        "アクセス権限を付与したい",
        [hr],
        filters={"large_category": "10_人事", "document_version": "V3"},
    )
    assert (filtered.business_domains, filtered.versions) == (
        frozenset({"人事"}),
        frozenset({"v3"}),
    )
    assert match_guide([hr], "アクセス権限を付与したい", context=filtered) is None

    # 空の項目は「このプロファイルの中」で制限なし（状態に出さない）。
    unscoped = match_guide(GUIDES, "経理のアクセス権限を付与したい", context=context)
    assert unscoped is not None and unscoped.applicability == {}
    # rank_guides も同じ判定（範囲の外のガイドを返さない）。
    ranked = rank_guides([hr, *GUIDES], "経理のアクセス権限を付与", context=context)
    assert [item.guide_id for item in ranked] == ["g-grant"]


def test_document_and_tool_conditions_ignore_user_claims() -> None:
    content = grant_guide(
        conditions=[
            {
                "id": "target",
                "label": "付与先",
                "allowed_values": ["個別", "グループ"],
                "source": "document",
            },
            {
                "id": "plan",
                "label": "契約の種類",
                "allowed_values": ["標準", "上位"],
                "source": "tool",
                "unknown_handling": "handoff",
                "required": False,
            },
        ],
        branches=[],
    )
    guides = [("g-doc", 1, content)]
    # 質問の文・渡した値では、資料・道具が出所の条件を既知にしない（不明のまま）。
    match = match_guide(
        guides, "グループにアクセス権限を付与したい（上位プラン）", {"target": "個別"}
    )
    assert match is not None
    assert [state.status for state in match.states] == ["unknown", "unknown"]
    # 利用者に聞いても決まらないので確かめず、分岐で答える（聞き直しを繰り返さない）。
    assert match.decision == "branch"
    assert [item["condition_id"] for item in clarification_questions(match)] == ["target"]
    content_text = guide_rule(match)["content"]
    assert "付与先（個別／グループ）。資料で確かめる" in content_text
    assert "契約の種類（標準／上位）。現場の記録・道具で確かめる" in content_text


def test_conflicting_values_are_clarified_with_the_named_values() -> None:
    three = grant_guide(
        conditions=[
            {
                "id": "target",
                "label": "付与先",
                "allowed_values": ["個別", "グループ", "全員"],
                "question": "権限はどこに付けますか？",
            }
        ],
        branches=[],
    )
    guides = [("g-3", 1, three)]
    match = match_guide(guides, "個別とグループのどちらにアクセス権限を付与？")
    assert match is not None
    state = match.state_of("target")
    assert state is not None
    assert (state.status, state.candidates) == ("conflicting", ("個別", "グループ"))
    assert match.decision == "clarify"
    questions = clarification_questions(match)
    assert questions[0]["options"] == ["個別", "グループ"]
    assert questions[0]["question"] == (
        "質問に付与先の「個別」と「グループ」が出ています。権限はどこに付けますか？"
    )
    assert "（個別／グループ）" in short_circuit_answer(match)
    assert match.summary()["unknown_conditions"] == [
        {
            "id": "target",
            "label": "付与先",
            "handling": "ask",
            "state": "conflicting",
            "candidates": ["個別", "グループ"],
        }
    ]
    # チャットの確認は質問に出た値だけを出し、選択肢の id は全選択肢の中の位置（答えを引き直せる）。
    found = guide_clarification(match)
    assert found is not None
    rule_id, _, clarification = found
    assert [(o.id, o.label) for o in clarification.options] == [("o1", "個別"), ("o2", "グループ")]
    resolved = resolve_guide_clarification(guides, rule_id, ["o2"])
    assert resolved is not None and resolved[0] == {"target": "グループ"}
    # 利用者が値を渡せば矛盾は解ける。チャット（送信の前に確かめる）では分岐で答える。
    given = match_guide(guides, "個別とグループのどちらにアクセス権限を付与？", {"target": "全員"})
    assert given is not None and given.decision == "answer"
    chat = match_guide(guides, "個別とグループのどちらにアクセス権限を付与？", interactive=True)
    assert chat is not None and chat.decision == "branch"
    assert (
        "質問に複数の値が出ている条件（どれかに決めつけず、値ごとに分けて示す）: "
        "付与先（個別／グループ）"
    ) in guide_rule(chat)["content"]


def _route_guide() -> SupportGuideContent:
    return grant_guide(
        conditions=[
            {
                "id": "target",
                "label": "付与先",
                "allowed_values": ["個別", "グループ"],
                "question": "どちらですか？",
            },
            {
                "id": "approved",
                "label": "部門長の承認",
                "type": "boolean",
                "required": False,
                "unknown_handling": "branch",
            },
        ],
        steps=[
            {"id": "open", "title": "詳細画面の権限タブを開く", "retrieval_hints": ["権限タブ"]},
            {
                "id": "user",
                "title": "利用者を選んで付与する",
                "depends_on": ["open"],
                "retrieval_hints": ["利用者の追加"],
            },
            {
                "id": "group",
                "title": "グループを選んで付与する",
                "depends_on": ["open"],
                "retrieval_hints": ["グループの追加"],
            },
            {"id": "notify", "title": "グループの全員へ知らせる", "depends_on": ["group"]},
            {"id": "ask", "title": "部門長に承認を依頼する"},
            {"id": "done", "title": "付与の結果を確かめる"},
        ],
        branches=[
            {
                "id": "b-user",
                "when": {"condition_id": "target", "values": ["個別"]},
                "goto_step": "user",
            },
            {
                "id": "b-group",
                "when": {"condition_id": "target", "values": ["グループ"]},
                "goto_step": "group",
            },
            {
                "id": "b-unknown",
                "when": {"condition_id": "approved", "operator": "unknown"},
                "goto_step": "ask",
            },
            {
                "id": "b-yes",
                "when": {"condition_id": "approved", "values": ["true"]},
                "goto_step": "done",
            },
        ],
    )


def test_known_conditions_narrow_steps_to_the_matched_branch() -> None:
    guides = [("g-route", 1, _route_guide())]
    match = match_guide(guides, "グループにアクセス権限を付与したい")
    assert match is not None and match.decision == "answer"
    steps, branches = visible_plan(match)
    # 個別の分岐の行き先（user）は外し、グループの分岐の行き先と依存先・共通の手順は残す。
    # 承認は不明なので、unknown の分岐（ask）は当たり、値の分岐（done）は決まらないので残す。
    assert [step.id for step in steps] == ["open", "group", "notify", "ask", "done"]
    assert [(branch.id, applies) for branch, applies in branches] == [
        ("b-group", True),
        ("b-unknown", True),
        ("b-yes", None),
    ]
    rule = guide_rule(match)
    assert "利用者を選んで付与する" not in rule["content"]
    assert (
        "分岐: 付与先 が グループ に当たるため「グループを選んで付与する」へ進む。"
        in (rule["content"])
    )
    assert "付与先 が 個別" not in rule["content"]
    assert rule["triggers"] == ["権限タブ", "グループの追加"]

    # 個別なら、グループの行き先とそれだけに依存する手順（notify）を外す。
    # 承認が分かれば unknown の分岐の行き先（ask）も外す。
    single = match_guide(guides, "個別の利用者にアクセス権限を付与したい", {"approved": "yes"})
    assert single is not None
    assert [step.id for step in visible_plan(single)[0]] == ["open", "user", "done"]
    assert "グループを選んで付与する" not in guide_rule(single)["content"]

    # 付与先が分からなければ（分岐で答える）全部の場合を残す。
    unknown = match_guide(guides, "アクセス権限を付与したい", interactive=True)
    assert unknown is not None and unknown.decision == "branch"
    assert [step.id for step in visible_plan(unknown)[0]] == [
        "open",
        "user",
        "group",
        "notify",
        "ask",
        "done",
    ]


def test_impact_is_shown_only_for_the_branch_it_applies_to() -> None:
    """承認・影響範囲が係る手順が、分かっている条件で外れたら示させない（#1320）。"""
    data = json.loads(
        (
            Path(__file__).resolve().parents[2] / "evaluation/business-support/support-guides.json"
        ).read_text(encoding="utf-8")
    )
    content = SupportGuideContent.model_validate(
        next(item for item in data["guides"] if item["title"] == "アクセス権限の付与")
    )
    guides = [("g-access", 1, content)]
    # 検証用アカウント（個別の言い換え）: グループへの付与の承認は係らない。
    single = match_guide(guides, "検証用アカウントを登録し、アクセス権限を付けて")
    assert single is not None and single.decision == "answer"
    assert impact_applies(single) is False
    assert "影響範囲" not in guide_rule(single)["content"]
    assert "承認について" not in guide_rule(single)["content"]
    # グループ: 係る手順を添えて示す。
    group = match_guide(guides, "グループにアクセス権限を付与したい")
    assert group is not None and impact_applies(group) is True
    assert (
        "影響範囲: グループ。実施の前に承認が要る（手順「付与の前に部門長の承認を得る」"
        "「グループの詳細画面の「権限」タブで付与する」に係る）" in guide_rule(group)["content"]
    )
    # 付与先が分からなければ（分岐で答える）、係る手順が残るので示す。
    unknown = match_guide(guides, "アクセス権限を付与したい", interactive=True)
    assert unknown is not None and unknown.decision == "branch"
    assert impact_applies(unknown) is True
    assert "影響範囲: グループ" in guide_rule(unknown)["content"]
    # 係る手順を決めていない業務ガイドは、今までどおり常に示す。
    plain = match_guide(GUIDES, "個別の利用者にアクセス権限を付与したい")
    assert plain is not None and impact_applies(plain) is True
    assert "影響範囲: グループ。実施の前に承認が要る" in guide_rule(plain)["content"]


def test_guide_load_failure_is_recorded_in_answer_diagnostics() -> None:
    diagnostics: dict[str, Any] = {"outcome": "answered", "envelope": {"outcome": "answered"}}
    apply_guide_to_diagnostics(diagnostics, {GUIDE_LOAD_FAILED_KEY: True})
    assert diagnostics[GUIDE_LOAD_FAILED_KEY] is True
    assert "guide" not in diagnostics
    assert diagnostics["outcome"] == "answered"


@pytest.mark.usefixtures("system_access")
async def test_resolve_query_context_records_guide_load_failure(monkeypatch: MonkeyPatch) -> None:
    fake = FakeKnowledgeOracle(support_guide_enabled=True)
    monkeypatch.setattr(search_route, "OracleClient", lambda: fake)

    class _BrokenStore:
        def __init__(self, _oracle: object) -> None:
            pass

        async def published_contents(self, _profile: str) -> list[Any]:
            raise RuntimeError("support guide table is missing")

    monkeypatch.setattr(search_route, "SupportGuideStore", _BrokenStore)
    request = SearchRequest(query="アクセス権限を付与したい", search_answer_profile_id="bv-1")
    _, settings, _, applied = await search_route._resolve_query_context(request, get_settings())
    assert applied == "bv-1"
    assert settings.rag_support_guide == {GUIDE_LOAD_FAILED_KEY: True}

    # 読めれば失敗は残さず、選んだガイドの要約（適用範囲を含む）を渡す。
    class _Store(_BrokenStore):
        async def published_contents(self, _profile: str) -> list[Any]:
            return GUIDES

    monkeypatch.setattr(search_route, "SupportGuideStore", _Store)
    _, settings, _, _ = await search_route._resolve_query_context(request, get_settings())
    assert settings.rag_support_guide["guide_id"] == "g-grant"
    assert settings.rag_support_guide["applicability"] == {}
    assert GUIDE_LOAD_FAILED_KEY not in settings.rag_support_guide


@pytest.mark.usefixtures("system_access")
async def test_resolve_query_context_uses_the_draft_only_for_the_preview(
    monkeypatch: MonkeyPatch,
) -> None:
    """下書きで試す（#1288）: 照合だけ下書きを使い、公開の版の一覧は変えない。"""
    fake = FakeKnowledgeOracle(support_guide_enabled=True)
    monkeypatch.setattr(search_route, "OracleClient", lambda: fake)
    # 公開の版は「権限」の質問に当たらない（照合の語が違う）。
    published = [
        (
            "g-grant",
            2,
            grant_guide(goal={"expected_result": "x", "match_terms": ["別の業務"]}),
        ),
        ("g-export", 1, export_guide()),
    ]

    class _Store:
        def __init__(self, _oracle: object) -> None:
            pass

        async def published_contents(self, _profile: str) -> list[Any]:
            return list(published)

    monkeypatch.setattr(search_route, "SupportGuideStore", _Store)
    request = SearchRequest(query="アクセス権限を付与したい", search_answer_profile_id="bv-1")
    _, settings, _, _ = await search_route._resolve_query_context(request, get_settings())
    assert settings.rag_support_guide == {}
    assert settings.rag_guide_preview == {}

    preview = GuidePreview("g-grant", 7, grant_guide(), published_revision=2)
    _, settings, _, _ = await search_route._resolve_query_context(
        request, get_settings(), guide_preview=preview
    )
    guide = settings.rag_support_guide
    assert (guide["guide_id"], guide["revision"], guide["draft"]) == ("g-grant", 7, True)
    assert settings.rag_guide_preview == {
        "guide_id": "g-grant",
        "draft_revision": 7,
        "published_revision": 2,
    }
    # 公開の版の一覧（保存）はそのまま。
    assert published[0][1] == 2
    assert published[0][2].goal.match_terms == ["別の業務"]

    # 別のガイドが選ばれたときは draft の印を付けない（下書きが使われなかったと分かる）。
    other = GuidePreview("g-export", 3, export_guide())
    _, settings, _, _ = await search_route._resolve_query_context(
        request, get_settings(), guide_preview=other
    )
    assert "draft" not in settings.rag_support_guide
    assert settings.rag_guide_preview["guide_id"] == "g-export"


def test_with_draft_replaces_or_adds_the_guide_and_marks_preview_records() -> None:
    preview = GuidePreview("g-grant", 5, grant_guide(title="改訂"))
    replaced = with_draft(GUIDES, preview)
    assert [(gid, rev) for gid, rev, _ in replaced] == [("g-export", 1), ("g-grant", 5)]
    added = with_draft([GUIDES[1]], preview)
    assert [gid for gid, _, _ in added] == ["g-export", "g-grant"]
    assert is_guide_preview_trace(GUIDE_PREVIEW_TRACE_PREFIX + "a" * 32)
    assert len(GUIDE_PREVIEW_TRACE_PREFIX + "a" * 32) <= 64
    assert not is_guide_preview_trace("a" * 32)
    assert is_guide_preview_record({GUIDE_PREVIEW_KEY: preview.marker()})
    assert not is_guide_preview_record({"confidence": "high"})
    assert not is_guide_preview_record(None)


# ---- API ---------------------------------------------------------------------------------


def test_clarification_suggest_falls_back_to_guide_conditions(monkeypatch: MonkeyPatch) -> None:
    fake = FakeKnowledgeOracle(support_guide_enabled=True)
    monkeypatch.setattr(knowledge_route, "OracleClient", lambda: fake)

    async def published(_oracle: object, _profile: str) -> list[Any]:
        return GUIDES

    monkeypatch.setattr(knowledge_route, "_published_guides", published)
    body = client.post(
        "/api/search-answer-profiles/bv-1/clarifications/suggest",
        json={"query": "アクセス権限を付与したい"},
    ).json()["data"]["suggestion"]
    assert body["rule_id"] == "guide:g-grant:target"
    assert body["clarification"]["question"].startswith("権限は個別の利用者と")
    none = client.post(
        "/api/search-answer-profiles/bv-1/clarifications/suggest",
        json={"query": "グループにアクセス権限を付与したい"},
    ).json()["data"]
    assert none["suggestion"] is None


class _Profile:
    id = "bv-1"
    status = "ACTIVE"
    config = SearchAnswerProfileConfig.model_validate(
        {"knowledge_base_ids": ["kb-1"], "query": {"support_guide_enabled": True}}
    )


class _FakeOracle:
    async def get_search_answer_profile(self, profile_id: str) -> Any:
        return _Profile() if profile_id == "bv-1" else None

    async def get_search_answer_profile_knowledge(self, _profile_id: str, _kind: str) -> None:
        return None


@pytest.mark.usefixtures("auth")
def test_mcp_lookup_guides_returns_step_contents(auth: Any, monkeypatch: MonkeyPatch) -> None:  # noqa: F811
    """手順の中身（目的・手がかり・参照の資料と頁・必要な根拠・終わりの条件）と共通の資料・完了の
    条件を返す（#2396）。"""
    content = grant_guide(
        steps=[
            {
                "id": "open",
                "title": "詳細画面の権限タブを開く",
                "purpose": "権限の画面に入る",
                "retrieval_hints": ["権限タブ"],
                "evidence_requirements": ["画面の入口"],
                "done_when": "権限タブが開いた",
                "references": [
                    {
                        "document_id": "doc-b",
                        "title": "画面の説明",
                        "page_ranges": [{"page_start": 4, "page_end": 6}],
                    }
                ],
            },
            {"id": "grant", "title": "権限を選んで付与する", "depends_on": ["open"]},
        ],
        references=[{"document_id": "doc-a", "title": "運用手順書", "section_path": ["3 章"]}],
        completion=[{"id": "done", "description": "付与した権限が表示される"}],
    )

    async def published(_oracle: object, _profile: str) -> tuple[list[Any], bool]:
        return [("g-grant", 3, content)], False

    monkeypatch.setattr(mcp_tools, "OracleClient", _FakeOracle)
    monkeypatch.setattr(search_route, "load_published_guides", published)
    user = auth.user_with_permissions("searcher", ["menu.search"])
    guide = _call(
        "rag_lookup_guides",
        {
            "query": "アクセス権限を付与したい",
            "search_answer_profile_id": "bv-1",
            "conditions": {"target": "グループ"},
        },
        _token(user.user_uuid),
    )["structuredContent"]["guides"][0]

    first = guide["steps"][0]
    assert first["purpose"] == "権限の画面に入る"
    assert first["retrieval_hints"] == ["権限タブ"]
    assert first["evidence_requirements"] == ["画面の入口"]
    assert first["done_when"] == "権限タブが開いた"
    assert first["references"] == [
        {
            "document_id": "doc-b",
            "title": "画面の説明",
            "section_path": [],
            "page_ranges": [{"page_start": 4, "page_end": 6}],
            "sheet_ranges": [],
        }
    ]
    assert guide["references"] == [
        {
            "document_id": "doc-a",
            "title": "運用手順書",
            "section_path": ["3 章"],
            "page_ranges": [],
            "sheet_ranges": [],
        }
    ]
    assert guide["completion"] == [
        {"id": "done", "description": "付与した権限が表示される", "check_method": ""}
    ]


@pytest.mark.usefixtures("auth")
def test_mcp_lookup_guides_and_search_clarifications(auth: Any, monkeypatch: MonkeyPatch) -> None:  # noqa: F811
    async def published(_oracle: object, _profile: str) -> tuple[list[Any], bool]:
        return GUIDES, False

    monkeypatch.setattr(mcp_tools, "OracleClient", _FakeOracle)
    monkeypatch.setattr(search_route, "load_published_guides", published)
    user = auth.user_with_permissions("searcher", ["menu.search"])
    body = _call(
        "rag_lookup_guides",
        {"query": "アクセス権限を付与したい", "search_answer_profile_id": "bv-1"},
        _token(user.user_uuid),
    )["structuredContent"]
    guide = body["guides"][0]
    assert (guide["guide_id"], guide["revision"], guide["decision"]) == ("g-grant", 2, "clarify")
    assert guide["clarifications"][0]["options"] == ["個別", "グループ"]
    assert [step["id"] for step in guide["steps"]] == ["open", "grant"]
    assert (guide["impact_scope"], guide["approval_required"]) == ("group", True)
    assert (guide["impact_steps"], guide["impact_applies"]) == ([], True)
    known = _call(
        "rag_lookup_guides",
        {
            "query": "アクセス権限を付与したい",
            "search_answer_profile_id": "bv-1",
            "conditions": {"target": "個別"},
        },
        _token(user.user_uuid),
    )["structuredContent"]["guides"][0]
    assert known["decision"] == "answer"
    assert known["known_conditions"][0]["value"] == "個別"
    # 分かっている条件で外れた分岐（グループ → grant）の手順と分岐は返さない（#2396）。
    assert [step["id"] for step in known["steps"]] == ["open"]
    assert known["branches"] == []
    assert known["steps"][0]["retrieval_hints"] == ["権限タブ"]
    group = _call(
        "rag_lookup_guides",
        {
            "query": "アクセス権限を付与したい",
            "search_answer_profile_id": "bv-1",
            "conditions": {"target": "グループ"},
        },
        _token(user.user_uuid),
    )["structuredContent"]["guides"][0]
    assert [step["id"] for step in group["steps"]] == ["open", "grant"]
    assert group["branches"] == [
        {
            "id": "b1",
            "condition_id": "target",
            "operator": "equals",
            "values": ["グループ"],
            "goto_step": "grant",
            "applies": True,
            "note": "承認を得てから",
        }
    ]
    missing = _call(
        "rag_lookup_guides",
        {"query": "x", "search_answer_profile_id": "other"},
        _token(user.user_uuid),
    )
    assert missing["isError"] is True

    match = match_guide(GUIDES, "アクセス権限を付与したい")
    assert match is not None
    outcome = guide_short_circuit_outcome(
        {
            **match.summary(),
            "clarifications": clarification_questions(match),
            "short_answer": short_circuit_answer(match),
        }
    )
    captured: dict[str, Any] = {}

    async def fake_run(request: Any, **_: Any) -> SearchResponse:
        captured["conditions"] = request.conditions
        return SearchResponse(
            answer=outcome.answer,
            citations=[],
            trace_id="t-1",
            elapsed_ms=1.0,
            diagnostics=SearchDiagnostics(answer=outcome.diagnostics),
        )

    monkeypatch.setattr(search_route, "_run_search_with_timeout", fake_run)
    result = _call(
        "rag_search",
        {
            "query": "アクセス権限を付与したい",
            "search_answer_profile_id": "bv-1",
            "conditions": {"approved": "はい"},
        },
        _token(user.user_uuid),
    )["structuredContent"]
    assert captured["conditions"] == {"approved": "はい"}
    assert result["outcome"] == "needs_clarification"
    assert result["clarifications"][0]["condition_id"] == "target"
    assert result["guide"]["guide_id"] == "g-grant"
    assert result["evidence"] == []
