"""呼び出し元が選んだルール（pinned。業務ガイド #1238）は語の一致に関係なく使い、手がかりを検索文に足す。"""

import json
import unicodedata
from pathlib import Path
from tempfile import TemporaryDirectory

from rag_engine.knowledge.runtime_knowledge import (
    MAX_TEXT_CHARS,
    PINNED_RULE_TAG,
    RUNTIME_KNOWLEDGE_FILE_NAME,
    SUPPORT_GUIDE_RULE_TAG,
    build_runtime_knowledge_context,
)


def test_pinned_rule_is_always_matched_first_and_adds_its_triggers() -> None:
    payload = {
        "schema_version": 1,
        "terms": [],
        "rules": [
            {"id": "r1", "title": "取消の注意", "triggers": ["取消"], "content": "取消後は戻せません。", "status": "approved"},
            {
                "id": "guide-g1",
                "title": "権限の付与",
                "triggers": ["権限タブ"],
                "content": "手順 1. 権限タブを開く",
                "status": "approved",
                "tags": [PINNED_RULE_TAG, "support_guide"],
            },
        ],
    }
    with TemporaryDirectory() as work:
        output_dir = Path(work)
        (output_dir / RUNTIME_KNOWLEDGE_FILE_NAME).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        context = build_runtime_knowledge_context("取消したい", output_dir)
    assert [rule.rule_id for rule in context.matched_rules] == ["guide-g1", "r1"]
    assert "権限タブ" in context.expanded_question
    # pinned の題名は質問に無いので検索文に足さない（手がかりだけ）。
    assert "権限の付与" not in context.expanded_question


def test_support_guide_rule_is_not_cut_at_the_user_rule_limit() -> None:
    """業務ガイドのルールは利用者の登録の上限で切らず、末尾の行まで生成の入力に渡す（#3838）。"""
    lines = [
        f"手順 {index}（s{index}）. 合成の手順 {index} の操作を行う。終わりの条件: 画面 {index} が表示される" for index in range(1, 40)
    ]
    last = "資料で確かめられない手順・値は答えに含めず、確かめられない点として示す。"
    guide_content = "\n".join([*lines, last])
    long_user_rule = "利用者の登録のルール。" + "あ" * (MAX_TEXT_CHARS + 100)
    assert len(guide_content) > MAX_TEXT_CHARS
    payload = {
        "schema_version": 1,
        "terms": [],
        "rules": [
            {
                "id": "guide-g1",
                "title": "合成のガイド",
                "content": guide_content,
                "status": "approved",
                "tags": [PINNED_RULE_TAG, SUPPORT_GUIDE_RULE_TAG],
            },
            {"id": "r1", "title": "合成のルール", "triggers": ["合成"], "content": long_user_rule, "status": "approved"},
        ],
    }
    with TemporaryDirectory() as work:
        output_dir = Path(work)
        (output_dir / RUNTIME_KNOWLEDGE_FILE_NAME).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        context = build_runtime_knowledge_context("合成の質問", output_dir)
    rules = {rule.rule_id: rule for rule in context.matched_rules}
    # 改行は空白に畳み、NFKC で正規化する（他のルールと同じ）。文は落とさない。
    assert rules["guide-g1"].content == unicodedata.normalize("NFKC", " ".join([*lines, last]))
    assert rules["guide-g1"].content.endswith(last)
    assert last in context.prompt_context()
    # 利用者の登録のルールの上限は変えない。
    assert len(rules["r1"].content) == MAX_TEXT_CHARS
