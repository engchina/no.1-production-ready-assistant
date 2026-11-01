"""最終の検証に渡す段落ごとの出典（`passage_citations`。#104）の決定論テスト。

業務 Agent の回答は使ったツールと根拠を本文とは別に示す（#2328）ので、段落に根拠の ID や出典の行を
書かないことが多い。RAG の `rag_validate_answer` は段落の実際の出典の集合だけで主張を裏付ける
（未引用の資料で裏付けない）ため、出典を渡さないと正しい回答もすべて「確かめが終わっていない」
（unassessed）になり、品質評価で根拠不足の拒答に数えられていた。Control Plane が、回答の根拠と
同じ決定的な照合で段落ごとの出典を作って渡すことと、「使用したツール」の行を検証の前に外すことを
確かめる。
"""

from __future__ import annotations

from typing import Any

import anyio
from agents.testing import assistant_message, function_call
from mcp_support import FakeProductMcp
from pytest import MonkeyPatch
from test_answer_validation import AGENT_ID, USER_UUID, _script, mcp  # noqa: F401 - fixture

from app.features.agent import builtin_runtime
from app.features.agent.answer_evidence import validation_passage_citations
from app.features.agent.answer_passages import is_lookup_note
from app.features.agent.answer_validation import without_lookup_notes
from app.features.agent.runtime import RunCreateRequest, run_answer_text, runtime_repository

DOC = "e58aecbc4ab249a99374851000ec24e3"
CHUNKS = {
    name: f"{DOC}:cs:{index}" for index, name in enumerate(["title", "art1", "art3", "art3b"])
}
LEAD = (
    "社内規程（株式会社サンプル 勤務・出張・経費規程 第3条「出張旅費」、1ページ目）によると、"
    "出張の宿泊費の上限（1泊）は役職で以下のとおりです。"
)
ROW_STAFF = "| 一般社員 | 12,000円 |"
ROW_MANAGER = "| 課長以上 | 15,000円 |"
SOURCE_LINE = (
    "根拠資料: **社内規程サンプル.pdf** ／ 株式会社サンプル 勤務・出張・経費規程 ／ "
    "第3条（出張旅費）／ 1ページ目"
)
# Run run_a30a298b… の回答の形（本文・表・使ったツールの節・出典の行）。
LODGING_ANSWER = "\n".join(
    [
        LEAD,
        "",
        "| 区分 | 宿泊費の上限（1泊） |",
        "|---|---|",
        ROW_STAFF,
        ROW_MANAGER,
        "",
        "---",
        "",
        "**使用したツールと根拠**",
        "- `rag__rag_recommend_search_answer_profile`（社内規程 Q&A を推薦）",
        "- `rag__rag_retrieve_evidence`",
        "",
        SOURCE_LINE,
    ]
)


def _item(name: str, section: str, excerpt: str, page: int = 1) -> dict[str, Any]:
    return {
        "document_id": DOC,
        "chunk_id": CHUNKS[name],
        "file_name": "社内規程サンプル.pdf",
        "excerpt": excerpt,
        "locator": {
            "section_path": ["株式会社サンプル 勤務・出張・経費規程", section],
            "page_start": page,
            "page_end": page,
        },
    }


ITEMS = [
    _item(
        "art3",
        "第3条（出張旅費）",
        "第3条（出張旅費）\n<table><tr><td>一般社員</td><td>12,000円</td></tr></table>",
    ),
    _item("art3b", "第3条（出張旅費）", "1. 新幹線は普通車指定席を原則とする。"),
    _item(
        "art1",
        "第1条（時間外勤務）",
        "第1条（時間外勤務）\n時間外勤務の上限は月45時間とする。",
        page=2,
    ),
]


def _cited(answer: str, items: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {
        answer[citation["answer_range"]["start"] : citation["answer_range"]["end"]]: citation[
            "chunk_ids"
        ]
        for citation in validation_passage_citations(answer, items)
    }


def test_tool_name_lines_are_lookup_notes_and_removed_before_validation() -> None:
    # 「使用したツール」の行は作業の経過で資料の主張ではない（RAG が unsupported と判定していた）。
    assert is_lookup_note("- `rag__rag_retrieve_evidence`")
    assert is_lookup_note("- `rag__rag_recommend_search_answer_profile`（社内規程 Q&A を推薦）")
    assert is_lookup_note("- `rag__rag_search`、`nl2sql__nl2sql_query`")
    # `__` の無い snake_case（設定のキーなど）の説明・主張は外さない。
    assert not is_lookup_note("- `max_retries`（再試行の回数）")
    assert not is_lookup_note("- `rag__rag_search` の結果、上限は 12,000円 です。")
    body = without_lookup_notes(LODGING_ANSWER)
    assert "rag__" not in body
    # 中身が無くなった節の見出しも消し、本文と出典の行は残す。
    assert "使用したツール" not in body
    assert LEAD in body and ROW_STAFF in body and SOURCE_LINE in body


def test_passage_citations_bind_each_claim_to_the_evidence_the_answer_used() -> None:
    answer = without_lookup_notes(LODGING_ANSWER)
    cited = _cited(answer, ITEMS)
    # 見出し・表の形・出典の行は主張ではないので渡さない。
    assert set(cited) == {LEAD, ROW_STAFF, ROW_MANAGER}
    # 段落が書いた章節（第3条）に当たる根拠を先にする（共通の文書名で当たる根拠も含む。裏付けは
    # RAG が根拠の本文で確かめるので、出典の集合が広くても誤った段落は supported にならない）。
    assert cited[LEAD][0] == CHUNKS["art3"]
    # 段落そのものに結び付く根拠の無い表の行は、回答全体が出典にした根拠（出典の行の文書と頁・
    # 他の段落の根拠）。
    assert cited[ROW_STAFF] == cited[ROW_MANAGER]
    assert CHUNKS["art3"] in cited[ROW_STAFF]
    assert set(cited[ROW_STAFF]) >= set(cited[LEAD])
    # 位置は RAG の answer_range と同じ数え方（回答の中の段落の原文の位置）。
    for citation in validation_passage_citations(answer, ITEMS):
        assert set(citation) == {"answer_range", "chunk_ids"}


def test_no_passage_citations_without_any_binding() -> None:
    # 回答のどこにも根拠が結び付かなければ渡さない（RAG は今までどおり unassessed にする）。
    assert validation_passage_citations("期限は 30 日です。", ITEMS) == []
    assert validation_passage_citations(LEAD, []) == []


def test_old_validator_without_the_input_is_not_sent_passage_citations() -> None:
    # 契約に passage_citations の無い RAG（extra=forbid）には渡さない。
    accepts = builtin_runtime._accepts_input  # noqa: SLF001 - 契約の版の判定
    assert accepts({"properties": {"passage_citations": {}}}, "passage_citations")
    assert not accepts({"properties": {"query": {}}}, "passage_citations")
    assert not accepts({}, "passage_citations")


def test_final_validation_sends_passage_citations(
    monkeypatch: MonkeyPatch,
    mcp: FakeProductMcp,  # noqa: F811 - fixture
) -> None:
    lead = "契約書の第5条（更新）によると、契約の更新の期限は次のとおりです。"
    row = "| 更新 | 30 日前まで |"
    answer = "\n".join(
        [lead, "", "| 区分 | 期限 |", "|---|---|", row, "", "- `rag__rag_retrieve_evidence`"]
    )
    _script(
        monkeypatch,
        [function_call("rag__rag_retrieve_evidence", {"query": "契約の更新"}, call_id="call-1")],
        [assistant_message(answer)],
    )
    run = runtime_repository.create_builtin_run(
        RunCreateRequest(goal="契約の更新の期限は？", agent_id=AGENT_ID),
        created_by_user_uuid=USER_UUID,
    )
    anyio.run(builtin_runtime.execute_run, run.id)
    run = runtime_repository.get_run(run.id)

    [call] = mcp.calls_of("rag_validate_answer")
    sent = call["arguments"]["answer"]
    # 使ったツールの行は検証の前に外す（RAG に unsupported と判定させない）。
    assert "rag__" not in sent
    assert "rag__" not in (run_answer_text(run) or "")
    citations = call["arguments"]["passage_citations"]
    quoted = {
        sent[item["answer_range"]["start"] : item["answer_range"]["end"]]: item["chunk_ids"]
        for item in citations
    }
    assert quoted == {lead: ["chunk-1"], row: ["chunk-1"]}
    # 渡す根拠は evidence に含めたものだけ。
    evidence = {ref["chunk_id"] for ref in call["arguments"]["evidence"]}
    assert all(set(item["chunk_ids"]) <= evidence for item in citations)
