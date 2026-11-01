"""文脈の圧縮（`context_window`）の後も、次の手を選ぶのに要る作業の状態が残るかの評価（#71）。

入力が上限に収まること（#2383 の容量テスト）とは別に、縮めた後のモデル用の view で次を確かめる。

- 利用者の質問・指示（支援タスクの状態＝前の Run までの条件・保留・根拠の参照）は縮めない
- 結論（outcome）・分かった条件と不明な条件（値・出典・不明の扱い）・確かめられない点（gaps）
- 除外した候補とその理由（推薦しなかった検索・回答プロファイル）
- 読み終えたか（section_complete）と、続きを読む cursor（1 文字でも欠けると読めない）
- 業務データの続き（has_more・status）
- 根拠の ID・旧版と置き換え先（版の題名）
- 縮めた結果を更に縮めても（複数回の圧縮）残ること、呼び出しと結果の対応（call_id）が変わらないこと

合成のタスクは「2025 年度の定期保守計画で A 棟の点検周期を調べる」。無関係な検索の結果を多く
積み（上限未満でも情報が多い条件・長い会話）、関係する古い結果が最も強い段階（ID と状態だけ）まで
縮まる予算にする。外部への通信はしない。

改善前（main・#71 より前）は段階 3 で条件・候補の状態と理由、段階 4 で ID 以外のすべて（cursor・
読み終えたか・結論・条件の値・除外の理由）を失っていた（PR の本文に件数を記録）。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from app.features.agent.context_window import (
    COMPACTION_KEY,
    OLDER_MAX_LEVEL,
    STATE_VALUE_MAX_CHARS,
    ContextBudget,
    ContextLimits,
    compact_items,
    compact_output,
)

GOAL = "2025年度の定期保守計画で、A棟の点検周期を教えて。"
INSTRUCTIONS = (
    "## 支援タスクの状態\n- 分かった条件: 対象の棟=A棟（利用者が回答）\n"
    "- 保留: 時期（未回答）\n- 根拠の参照: doc-2025/chunk-7"
)
CURSOR = "a1b2c3d4e5f6:sec-3:2:" + "4" * 40
_BODY = ("合成の本文の文。" * 400)[:3000]

PROFILE = {
    "recommended_profile_id": "p-facility",
    "candidates": [
        {
            "id": "p-facility",
            "name": "設備保守",
            "description": "設備の保守の資料。" * 20,
            "reason": "A棟の保守計画が当たった",
            "score": 1.0,
            "documents": ["定期保守計画 2025年度"],
        },
        {
            "id": "p-hr",
            "name": "人事規程",
            "description": "人事の規程。" * 20,
            "reason": "保守の資料が無いため除外",
            "score": 0.1,
            "documents": [],
        },
    ],
}
SEARCH = {
    "answer": "A棟の点検周期は資料で確認しています。" + _BODY,
    "trace_id": "trace-search",
    "outcome": "needs_clarification",
    "conditions": ["対象の棟: A棟"],
    "gaps": ["B棟の点検周期は資料に無い"],
    "clarifications": [
        {
            "condition_id": "season",
            "label": "時期",
            "question": "どの時期の点検ですか？",
            "options": ["上期", "下期"],
        }
    ],
    "guide": {
        "id": "guide-maintenance",
        "title": "定期保守の案内",
        "conditions": [
            {
                "id": "building",
                "label": "対象の棟",
                "state": "known",
                "value": "A棟",
                "source": "user",
            },
            {"id": "season", "label": "時期", "state": "unknown", "handling": "ask"},
        ],
    },
    "evidence": [
        {
            "evidence_id": f"chunk-{index}",
            "document_id": "doc-2025",
            "chunk_id": f"chunk-{index}",
            "excerpt": _BODY[:1000],
        }
        for index in range(8)
    ],
    "evidence_omitted": 3,
    "older_versions": [
        {
            "document_id": "doc-2024",
            "title": "定期保守計画 2024年度",
            "superseded_by_document_id": "doc-2025",
        }
    ],
}
SECTION = {
    "document_id": "doc-2025",
    "chunk_set_id": "cs-2025",
    "section": {"section_id": "sec-3", "title": "3. 点検周期", "page_start": 4, "page_end": 9},
    "text": _BODY,
    "chunks": [{"chunk_id": f"chunk-{index}", "text": _BODY[:500]} for index in range(4)],
    "section_complete": False,
    "next_cursor": CURSOR,
    "superseded": False,
}
DATA = {
    "status": "succeeded",
    "job_id": "job-42",
    "rows": [{"building": "A棟", "interval": "6か月", "note": _BODY[:300]} for _ in range(30)],
    "has_more": True,
    "next_cursor": "rows:30",
}

# 確かめる作業の状態（名前 → (結果, 縮めた結果で残っているか)）。
Check = Callable[[dict[str, Any]], bool]
CHECKS: dict[str, tuple[dict[str, Any], Check]] = {
    "推薦したプロファイル": (PROFILE, lambda d: d.get("recommended_profile_id") == "p-facility"),
    "除外した候補と理由": (
        PROFILE,
        lambda d: (
            {"id": "p-hr", "reason": "保守の資料が無いため除外"}.items()
            <= next((c for c in d["candidates"] if c.get("id") == "p-hr"), {}).items()
        ),
    ),
    "結論（確認が要る）": (SEARCH, lambda d: d.get("outcome") == "needs_clarification"),
    "分かった条件の文": (SEARCH, lambda d: d.get("conditions") == ["対象の棟: A棟"]),
    "確かめられない点": (SEARCH, lambda d: d.get("gaps") == ["B棟の点検周期は資料に無い"]),
    "分かった条件の値と出典": (
        SEARCH,
        lambda d: (
            {"id": "building", "state": "known", "value": "A棟", "source": "user"}.items()
            <= d["guide"]["conditions"][0].items()
        ),
    ),
    "不明な条件と扱い": (
        SEARCH,
        lambda d: (
            {"id": "season", "label": "時期", "state": "unknown", "handling": "ask"}.items()
            <= d["guide"]["conditions"][1].items()
        ),
    ),
    "確かめる問いの条件": (
        SEARCH,
        lambda d: (
            {"condition_id": "season", "label": "時期"}.items() <= d["clarifications"][0].items()
        ),
    ),
    "根拠の ID": (
        SEARCH,
        lambda d: (
            [item.get("chunk_id") for item in d["evidence"]]
            == [f"chunk-{index}" for index in range(8)]
        ),
    ),
    "返さなかった根拠の数": (SEARCH, lambda d: d.get("evidence_omitted") == 3),
    "旧版と版の題名": (
        SEARCH,
        lambda d: (
            d["older_versions"][0]
            == {
                "document_id": "doc-2024",
                "title": "定期保守計画 2024年度",
                "superseded_by_document_id": "doc-2025",
            }
        ),
    ),
    "章節を読み終えていない": (SECTION, lambda d: d.get("section_complete") is False),
    "続きを読む cursor": (SECTION, lambda d: d.get("next_cursor") == CURSOR),
    "章節の ID と題名": (
        SECTION,
        lambda d: (
            d["section"].get("section_id") == "sec-3" and d["section"].get("title") == "3. 点検周期"
        ),
    ),
    "業務データの状態": (DATA, lambda d: d.get("status") == "succeeded"),
    "業務データの続き": (
        DATA,
        lambda d: d.get("has_more") is True and d.get("next_cursor") == "rows:30",
    ),
}


def _compacted(output: dict[str, Any], level: int) -> dict[str, Any]:
    text = json.dumps(output, ensure_ascii=False)
    shrunk = compact_output(text, level, rerunnable=True)
    loaded: dict[str, Any] = json.loads(shrunk)
    return loaded


def _holds(check: Check, data: dict[str, Any]) -> bool:
    try:
        return bool(check(data))
    except (KeyError, IndexError, TypeError):
        return False


@pytest.mark.parametrize("level", range(1, OLDER_MAX_LEVEL + 1))
@pytest.mark.parametrize("name", list(CHECKS))
def test_state_survives_every_compaction_level(name: str, level: int) -> None:
    output, check = CHECKS[name]
    assert _holds(check, output)
    assert _holds(check, _compacted(output, level)), f"段階 {level} で「{name}」を失った"


@pytest.mark.parametrize("name", list(CHECKS))
def test_state_survives_repeated_compaction(name: str) -> None:
    output, check = CHECKS[name]
    once = _compacted(output, 2)
    twice = _compacted(once, OLDER_MAX_LEVEL)
    assert _holds(check, twice)
    # 元の文字数は最初の結果の長さのまま（縮めた結果を元として数え直さない）。
    assert twice[COMPACTION_KEY]["original_chars"] == len(json.dumps(output, ensure_ascii=False))


def test_long_free_text_is_still_compacted() -> None:
    """状態の項目でも長い自由文は残さない（際限なく保存しない）。"""
    long_reason = "理由の文。" * 200
    output = {
        "candidates": [{"id": "p1", "reason": long_reason}],
        "gaps": ["長い不足の文。" * 100],
        "status": "x" * (STATE_VALUE_MAX_CHARS + 1),
    }
    shrunk = _compacted(output, OLDER_MAX_LEVEL)
    assert shrunk["candidates"] == [{"id": "p1"}]
    assert "gaps" not in shrunk
    assert "status" not in shrunk
    body = _compacted(SEARCH, OLDER_MAX_LEVEL)
    assert "answer" not in body
    assert all("excerpt" not in item for item in body["evidence"])


def _conversation(noise: int) -> list[Any]:
    """長い会話: 質問 → 関係する結果 4 件 → 無関係な検索の結果 `noise` 件 → 最新の結果。"""
    items: list[Any] = [{"role": "user", "content": GOAL}]
    calls = [
        ("rag__rag_recommend_search_answer_profile", PROFILE),
        ("rag__rag_search", SEARCH),
        ("rag__rag_read_section", SECTION),
        ("nl2sql__nl2sql_query", DATA),
        *[
            (
                "rag__rag_search",
                {"trace_id": f"trace-noise-{index}", "answer": _BODY, "evidence": []},
            )
            for index in range(noise)
        ],
        (
            "rag__rag_read_source",
            {"document_id": "doc-2025", "chunk_id": "chunk-7", "text": "短い"},
        ),
    ]
    for index, (name, output) in enumerate(calls):
        call_id = f"call-{index}"
        items.append({"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"})
        items.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": json.dumps(output, ensure_ascii=False),
            }
        )
    return items


def test_long_conversation_keeps_the_state_and_the_pairs() -> None:
    items = _conversation(noise=12)
    budget = ContextBudget(
        ContextLimits(model_id="m", max_input_tokens=6000, max_output_tokens=1000),
        margin_ratio=0.0,
    )
    result = compact_items(
        items, budget=budget, fixed_text=INSTRUCTIONS, rerunnable=lambda _name: True
    )

    assert result.fits
    assert result.tokens_after < result.tokens_before / 10
    levels = {item.call_id: item.level for item in result.outputs}
    # 関係する古い結果は、最も強い段階（ID と状態だけ）まで縮んだ（候補の一覧は段階 3 で足りた）。
    assert levels["call-0"] >= 3
    assert all(levels[f"call-{index}"] == OLDER_MAX_LEVEL for index in range(1, 4))
    # 質問・モデルの呼び出し（引数）は縮めず、呼び出しと結果の対応も変えない。
    assert result.items[0] == {"role": "user", "content": GOAL}
    assert [item.get("call_id") for item in result.items[1:]] == [
        item.get("call_id") for item in items[1:]
    ]
    assert [item for item in result.items if item.get("type") == "function_call"] == [
        item for item in items if item.get("type") == "function_call"
    ]
    outputs = {
        item["call_id"]: json.loads(item["output"])
        for item in result.items
        if item.get("type") == "function_call_output"
    }
    by_output = {
        id(PROFILE): "call-0",
        id(SEARCH): "call-1",
        id(SECTION): "call-2",
        id(DATA): "call-3",
    }
    lost = [
        name
        for name, (output, check) in CHECKS.items()
        if not _holds(check, outputs[by_output[id(output)]])
    ]
    assert lost == []
