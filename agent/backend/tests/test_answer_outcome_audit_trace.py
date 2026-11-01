"""回答の対応（outcome）の、監査の応答の側の照合の誤りと業務プロファイルの探索の扱い（#108）。

#104 の後の合成の再試験（品質評価 rag-research の合成 3 ケース × 3 試行・チャット）で、次の 2 つが
残った。どちらも実際の Run（`rag_validate_answer` の結果・Run の step）の形で確かめる。

- B1: 監査モデルは段落を根拠で裏付けた（`audit_status=supported`）のに、監査が写した原文が根拠に
  見当たらない（`quote_unmatched`。run_803ced7b…）・監査が根拠の ID にファイル名を返した（未登録の
  原文 ID。run_13a17c51…）だけで、正しい回答が `validation_nothing_confirmed` の拒答に
  数えられていた。
- B2: 範囲外の質問（「社員食堂の今日の献立」。run_d4686772…）で、空の `nl2sql_list_profiles` の
  成功を現場のデータの確認に数え、拒答が `environment_tools` の条件付きの回答になっていた。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.features.agent import tool_grounding
from app.features.agent.answer_outcome import answer_outcome, environment_tool_calls
from app.features.agent.answer_validation import (
    STATUS_COMPLETED,
    combine_validations,
    validation_content,
)
from app.features.agent.clarification_cancel import CANCEL_TRACE_PREFIX
from app.features.agent.evaluation import EvaluationCase, EvaluationCaseResult, summarize
from app.features.agent.runtime import RunStep, StepStatus
from app.features.agent.support_task import is_environment_data_tool
from app.features.agent.tools import ToolCall, ToolResult

CHUNK_HEADER = "e58aecbc:83996d6a:0"
CHUNK_TABLE = "e58aecbc:83996d6a:3"
EVIDENCE = [
    {"document_id": "e58aecbc", "chunk_id": CHUNK_HEADER},
    {"document_id": "e58aecbc", "chunk_id": CHUNK_TABLE},
]
TOOLS = [
    "rag__rag_recommend_search_answer_profile",
    "rag__rag_retrieve_evidence",
    "rag__rag_lookup_guides",
]


def _step(
    name: str, output: dict[str, Any] | None, *, success: bool = True, trace_id: str | None = None
) -> RunStep:
    return RunStep(
        run_id="run-1",
        status=StepStatus.COMPLETED if success else StepStatus.FAILED,
        tool_call=ToolCall(name=name, arguments={}, trace_id=trace_id),
        tool_result=ToolResult(name=name, success=success, output=output if success else None),
    )


def _retrieval_steps() -> list[RunStep]:
    """rag-research / default の Agent の調べ方（推薦 → 根拠を集める → 業務ガイドの照合）。"""
    return [
        _step(
            "rag__rag_recommend_search_answer_profile",
            {"recommended_profile_id": "p1", "candidates": [{"id": "p1"}]},
        ),
        _step("rag__rag_retrieve_evidence", {"degraded_steps": [], "evidence": EVIDENCE}),
        _step("rag__rag_lookup_guides", {"guides": []}),
    ]


def _supported(quote: str, chunk_id: str = CHUNK_TABLE) -> dict[str, Any]:
    return {
        "answer_quote": quote,
        "status": "supported",
        "chunk_id": chunk_id,
        "reason": "表の値と一致する。",
        "audit_status": "supported",
        "reference_check": "resolved",
    }


def _quote_unmatched(quote: str, chunk_id: str = CHUNK_TABLE) -> dict[str, Any]:
    """監査は裏付けたが、監査が写した原文が根拠に見当たらない判定（run_803ced7b… の形）。"""
    return {
        "answer_quote": quote,
        "status": "citation_error",
        "chunk_id": chunk_id,
        "reason": "裏付けとして写した原文が、結び付けた根拠に見当たりません。（採らなかった監査の"
        "判定 supported の理由: 表に「一般社員」の行があり、回答の主張と完全に一致する。）",
        "audit_status": "supported",
        "reference_check": "quote_unmatched",
        "support_check": "quote_unmatched",
    }


def _unknown_id(quote: str) -> dict[str, Any]:
    """監査が根拠の ID にファイル名を返した判定（run_13a17c51… の形。未登録の原文 ID）。"""
    return {
        "answer_quote": quote,
        "status": "citation_error",
        "chunk_id": "社内規程サンプル.pdf",
        "reason": "未登録の原文ID。規程名と第3条（出張旅費）の表が確認できる。主張と一致。",
        "audit_status": "supported",
        "reference_check": "quote_unmatched",
        "support_check": None,
    }


def _validated(answer: str, *claims: dict[str, Any]) -> dict[str, Any]:
    """`rag_validate_answer` の判定から、最終の検証の成果物の内容を組み立てる。"""
    counts: dict[str, int] = {}
    for claim in claims:
        counts[claim["status"]] = counts.get(claim["status"], 0) + 1
    result = {
        "valid": all(claim["status"] == "supported" for claim in claims),
        "status": "completed",
        "counts": counts,
        "claims": list(claims),
        "missing_evidence": [],
        "stale_evidence": [],
        "evidence_truncated": False,
    }
    content, _published = combine_validations(
        answer, [validation_content(STATUS_COMPLETED, result=result, evidence=EVIDENCE)]
    )
    return content


# run_803ced7b691e4e44b29ef31f1a6c5339（評価 hotel の 2 試行目）の回答。
HOTEL_LEAD = "一般社員の出張の宿泊費の上限は **1泊 12,000円** です（日当は 2,000円）。"
HOTEL_SOURCE = (
    "- 文書: `社内規程サンプル.pdf`（株式会社サンプル 勤務・出張・経費規程／規程番号 HR-2026-07・"
    "第3版・施行 2026年10月1日）"
)
HOTEL_NOTE = "※ 同一表では「課長以上」は 1泊 15,000円（日当 3,000円）と定められています。"
HOTEL_ANSWER = "\n".join(
    [
        "",
        "",
        HOTEL_LEAD,
        "",
        "**根拠**",
        HOTEL_SOURCE,
        "- 箇所: 第3条（出張旅費）の宿泊費・日当の一覧表（1ページ目）",
        "",
        HOTEL_NOTE,
    ]
)

# run_13a17c51431b401d9a49cacff674a3a4（チャット default）の回答。
TABLE_LEAD = (
    "社内規程（株式会社サンプル 勤務・出張・経費規程 第3条（出張旅費））によると、"
    "出張の宿泊費の上限（1泊）は区分ごとに以下の通りです。"
)
TABLE_ROWS = ["| 一般社員 | 12,000円 |", "| 課長以上 | 15,000円 |"]
TABLE_ANSWER = "\n".join(
    [
        "",
        "",
        TABLE_LEAD,
        "",
        "| 区分 | 宿泊費の上限（1泊） |",
        "|---|---|",
        *TABLE_ROWS,
        "",
        "**根拠**",
        "- 社内規程サンプル.pdf（株式会社サンプル 勤務・出張・経費規程、"
        "規程番号 HR-2026-07／第3版、"
        "施行日 2026年10月1日）",
    ]
)

# run_d468677265c44fa49a64425e4d5be9c5（チャット default。範囲外の質問）の最終の回答と調べ方。
MENU_LEAD = "申し訳ありませんが、社員食堂の今日の献立は確かめられませんでした。"
MENU_SEARCHED = (
    "社内の資料（規程など）や業務データを調べましたが、社員食堂の献立に関する情報は"
    "見つからなかったため、現時点ではお答えできません。"
)
MENU_RAG_LINE = "- 資料検索（社内規程 Q&A）:「社員食堂 献立」で検索 → 献立に関する記載なし"
MENU_DATA_LINE = "- 業務データのプロファイル一覧: 該当する業務データの利用範囲なし"
MENU_ANSWER = "\n".join(
    [
        "",
        "",
        MENU_LEAD,
        "",
        MENU_SEARCHED,
        "",
        "社内の資料や業務データについて、ほかに知りたいことがあればお知らせください。",
        "",
        "---",
        "**使ったツールと根拠**",
        MENU_RAG_LINE,
        MENU_DATA_LINE,
    ]
)


def _menu_steps() -> list[RunStep]:
    """範囲外の質問を NL2SQL の探索（推薦の失敗・空の一覧）と RAG で調べた step。

    run_d468677265c44fa49a64425e4d5be9c5 の形。
    """
    return [
        _step("nl2sql__nl2sql_recommend_profile", None, success=False),
        _step(
            "rag__rag_recommend_search_answer_profile",
            {"recommended_profile_id": "p1", "candidates": [{"id": "p1"}]},
        ),
        _step("rag__rag_retrieve_evidence", {"degraded_steps": [], "evidence": EVIDENCE}),
        _step("rag__rag_lookup_guides", {"guides": []}, trace_id="guide_check_run-1_rag"),
        _step("nl2sql__nl2sql_list_profiles", {"schema_version": 2, "profiles": []}),
        _step(
            "rag__rag_list_search_answer_profiles",
            {"search_answer_profiles": [{"id": "p1", "name": "社内規程 Q&A"}]},
        ),
        _step("nl2sql__nl2sql_list_profiles", {"schema_version": 2, "profiles": []}),
        _step("nl2sql__nl2sql_query", None, success=False),
    ]


def _menu_validation() -> dict[str, Any]:
    confirmation = {"status": "data_confirmation", "reason": "検索の結果を報告しているだけ。"}
    return _validated(
        MENU_ANSWER,
        *(
            {"answer_quote": quote, **confirmation}
            for quote in (MENU_LEAD, MENU_SEARCHED, MENU_RAG_LINE, MENU_DATA_LINE)
        ),
    )


def test_supported_audit_with_unmatched_quotes_is_not_a_refusal() -> None:
    # run_803ced7b…: 監査は 3 段落とも supported だが、写した原文の照合（quote_unmatched）で
    # すべて citation_error。根拠が無いことは示していないので拒答にしない（#108）。
    validation = _validated(
        HOTEL_ANSWER,
        _quote_unmatched(HOTEL_LEAD),
        _quote_unmatched(HOTEL_SOURCE, CHUNK_HEADER),
        _quote_unmatched(HOTEL_NOTE),
    )
    assert [mark["reasons"][0]["code"] for mark in validation["passage_marks"]] == [
        "quote_unmatched",
        "quote_unmatched",
    ]
    content = answer_outcome(HOTEL_ANSWER, steps=_retrieval_steps(), validation=validation)
    assert (content["value"], content["basis"], content["signals"]) == (
        "conditional",
        "answer_passages",
        ["unverified_passages"],
    )
    assert tool_grounding.refusal_stage(content["value"], TOOLS) is None
    # 写した原文を照合できた試行は answered（同じ回答の別の試行）。
    supported = _validated(
        HOTEL_ANSWER,
        _supported(HOTEL_LEAD),
        _supported(HOTEL_SOURCE, CHUNK_HEADER),
        _supported(HOTEL_NOTE),
    )
    content = answer_outcome(HOTEL_ANSWER, steps=_retrieval_steps(), validation=supported)
    assert (content["value"], content["basis"]) == ("answered", "answer_passages")


def test_audit_returning_a_file_name_as_evidence_id_is_not_a_refusal() -> None:
    # run_13a17c51…: 監査が導入の 1 文の根拠の ID にファイル名を返した（未登録の原文 ID）。
    validation = _validated(
        TABLE_ANSWER,
        _unknown_id(TABLE_LEAD),
        *(_supported(row) for row in TABLE_ROWS),
    )
    content = answer_outcome(TABLE_ANSWER, steps=_retrieval_steps(), validation=validation)
    assert (content["value"], content["signals"]) == ("conditional", ["unverified_passages"])
    # すべての段落で監査が ID を写し損ねても、拒答には数えない。
    validation = _validated(
        TABLE_ANSWER, _unknown_id(TABLE_LEAD), *(_unknown_id(row) for row in TABLE_ROWS)
    )
    content = answer_outcome(TABLE_ANSWER, steps=_retrieval_steps(), validation=validation)
    assert (content["value"], content["basis"]) == ("conditional", "answer_passages")


@pytest.mark.parametrize(
    ("lead", "case"),
    [
        # 回答が書いた根拠の ID が根拠に無い（回答の引用の誤り）。
        (
            {**_quote_unmatched(HOTEL_LEAD), "reference_check": "answer_id_invalid"},
            "answer_id_invalid",
        ),
        # 監査の元の判定が supported でない出典の誤り。
        ({**_quote_unmatched(HOTEL_LEAD), "audit_status": "citation_error"}, "audit_not_supported"),
        # 根拠で確かめられない。
        (
            {"answer_quote": HOTEL_LEAD, "status": "unsupported", "reason": "記載が無い。"},
            "unsupported",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_real_citation_errors_and_unsupported_passages_stay_refusals(
    lead: dict[str, Any], case: str
) -> None:
    # 検証の厳しさは変えない: 監査の応答の誤り以外で確かめられない段落が残り、確かめた段落が
    # 無ければ今までどおり拒答（#2738）。監査の応答の誤りの段落と混ざっていても同じ。
    validation = _validated(
        HOTEL_ANSWER,
        lead,
        _quote_unmatched(HOTEL_SOURCE, CHUNK_HEADER),
        _quote_unmatched(HOTEL_NOTE),
    )
    content = answer_outcome(HOTEL_ANSWER, steps=_retrieval_steps(), validation=validation)
    assert (content["value"], content["basis"]) == (
        "insufficient_evidence",
        "validation_nothing_confirmed",
    ), case


def test_contradicted_passage_is_still_removed_and_counted() -> None:
    # 誤った値（contradicted）は今までどおり本文から外し、確かめた段落が無ければ拒答。
    contradicted = {
        "answer_quote": HOTEL_LEAD,
        "status": "contradicted",
        "chunk_id": CHUNK_TABLE,
        "reason": "表では一般社員は 12,000円ではなく別の値。",
        "audit_status": "contradicted",
        "reference_check": "resolved",
    }
    validation = _validated(HOTEL_ANSWER, contradicted, _quote_unmatched(HOTEL_NOTE))
    assert [item["text"] for item in validation["removed_passages"]] == [HOTEL_LEAD]
    content = answer_outcome(HOTEL_ANSWER, steps=_retrieval_steps(), validation=validation)
    assert (content["value"], content["basis"]) == (
        "insufficient_evidence",
        "validation_nothing_confirmed",
    )
    assert "contradicted_removed" in content["signals"]


def test_empty_profile_listing_is_not_environment_confirmation() -> None:
    # run_d4686772…: 空の nl2sql_list_profiles の成功で environment_tools の conditional に
    # なっていた。一覧・推薦は使える業務データを探すだけなので、現場のデータの確認に数えない。
    steps = _menu_steps()
    assert environment_tool_calls(steps) == 0
    content = answer_outcome(MENU_ANSWER, steps=steps, validation=_menu_validation())
    assert (content["value"], content["basis"]) == ("insufficient_evidence", "absence_conclusion")
    assert "environment_tools" not in content["signals"]
    assert tool_grounding.refusal_stage(
        content["value"], [str(s.tool_call.name) for s in steps if s.tool_call]
    ) == (tool_grounding.REFUSAL_AFTER_TOOLS)


def test_environment_data_tools_still_count_real_queries() -> None:
    # 業務データの照会が成功したら、今までどおり現場のデータを確かめた呼び出しに数える。
    assert is_environment_data_tool("nl2sql__nl2sql_query")
    assert not is_environment_data_tool("nl2sql__nl2sql_list_profiles")
    assert not is_environment_data_tool("nl2sql__nl2sql_recommend_profile")
    assert not is_environment_data_tool("rag__rag_retrieve_evidence")
    steps = [*_menu_steps(), _step("nl2sql__nl2sql_query", {"rows": [], "sql": "SELECT 1"})]
    assert environment_tool_calls(steps) == 1
    content = answer_outcome(MENU_ANSWER, steps=steps, validation=_menu_validation())
    assert content["basis"] == "environment_tools"
    # Control Plane の取り消しで閉じた照会は数えない（#1441）。
    cancelled = [
        *_menu_steps(),
        _step("nl2sql__nl2sql_query", {"rows": []}, trace_id=f"{CANCEL_TRACE_PREFIX}x"),
    ]
    assert environment_tool_calls(cancelled) == 0


def test_apology_lead_unconfirmed_conclusion_is_an_absence_conclusion() -> None:
    # 結論の段落「申し訳ありませんが、（質問の対象）は確かめられませんでした。」は不足の結論。
    from app.features.agent.answer_passages import is_absence_conclusion

    assert is_absence_conclusion(MENU_LEAD)
    assert is_absence_conclusion("社員食堂の今日の献立は確かめられませんでした。")
    # 値・逆接・操作を含む文は主張のまま。
    assert not is_absence_conclusion(
        "宿泊費の上限は 12,000円ですが、日当は確かめられませんでした。"
    )
    assert not is_absence_conclusion("設定を変更してください。結果は確かめられませんでした")


def test_quality_summary_three_cases_three_trials_counts_only_actual_refusals() -> None:
    # 合成 3 ケース × 3 試行（評価 rag-research の再試験。#108）: hotel は 1 試行が
    # quote_unmatched、
    # overtime は答えた回答、out-of-scope は 3 試行とも範囲外の拒答（NL2SQL の空の探索を含む）。
    # refused_after_tools は実際の拒答の数（3）と一致する。
    hotel_supported = _validated(
        HOTEL_ANSWER,
        _supported(HOTEL_LEAD),
        _supported(HOTEL_SOURCE, CHUNK_HEADER),
        _supported(HOTEL_NOTE),
    )
    hotel_unmatched = _validated(
        HOTEL_ANSWER,
        _quote_unmatched(HOTEL_LEAD),
        _quote_unmatched(HOTEL_SOURCE, CHUNK_HEADER),
        _quote_unmatched(HOTEL_NOTE),
    )
    overtime = "時間外勤務の上限は月 45 時間、年 360 時間です。"
    overtime_validation = _validated(overtime, _supported(overtime))
    # run_48e5e63b…: 来月の献立の拒答（掲載先の例示が unsupported）。
    refusal_lead = "社員食堂の来月の献立については、調べられた資料に根拠が見つかりませんでした。"
    refusal_advice = (
        "献立の掲載先（社内ポータル、掲示、配付資料など）が分かっていれば、そちらで確認するか、"
        "食堂の担当へお問い合わせください。"
    )
    refusal = f"{refusal_lead}\n\n{refusal_advice}"
    refusal_validation = _validated(
        refusal,
        _unknown_id(refusal_lead),
        {"answer_quote": refusal_advice, "status": "unsupported", "reason": "例示は根拠に無い。"},
    )
    trials: list[tuple[str, list[str], str, list[RunStep], dict[str, Any]]] = [
        ("hotel", TOOLS, HOTEL_ANSWER, _retrieval_steps(), hotel_supported),
        ("hotel", TOOLS, HOTEL_ANSWER, _retrieval_steps(), hotel_unmatched),
        (
            "hotel",
            TOOLS,
            TABLE_ANSWER,
            _retrieval_steps(),
            _validated(
                TABLE_ANSWER, _unknown_id(TABLE_LEAD), *(_supported(row) for row in TABLE_ROWS)
            ),
        ),
        *(("overtime", TOOLS, overtime, _retrieval_steps(), overtime_validation) for _ in range(3)),
        ("out-of-scope", TOOLS, refusal, _retrieval_steps(), refusal_validation),
        ("out-of-scope", TOOLS, refusal, _retrieval_steps(), refusal_validation),
        (
            "out-of-scope",
            [str(step.tool_call.name) for step in _menu_steps() if step.tool_call],
            MENU_ANSWER,
            _menu_steps(),
            _menu_validation(),
        ),
    ]
    results = []
    outcomes = []
    for case_id, tools, answer, steps, validation in trials:
        value = answer_outcome(answer, steps=steps, validation=validation)["value"]
        outcomes.append((case_id, value))
        results.append(
            EvaluationCaseResult(
                case=EvaluationCase(id=case_id, question=case_id, expected="-"),
                refusal_stage=tool_grounding.refusal_stage(value, tools),
            )
        )
    summary = summarize(results)
    refusals = [case_id for case_id, value in outcomes if value == "insufficient_evidence"]
    assert refusals == ["out-of-scope"] * 3
    assert (summary.refused_before_tools, summary.refused_after_tools) == (0, len(refusals))
