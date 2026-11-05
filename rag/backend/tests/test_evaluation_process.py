"""評価の Excel の「案内すべき画面」「回答の過程」と、回答の過程から作る期待の下書き（#2402）。

内容は合成の例（架空の画面・資料の名前）だけを使う。LLM はスタブ（#2397 の録画した応答）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook  # type: ignore[import-untyped]
from pytest import MonkeyPatch
from rag_parser_core.language import builtin_pack

from app.api.routes import evaluation as evaluation_route
from app.clients import ai_calls
from app.main import app
from app.rag.evaluation_excel import (
    CASES_SHEET_TITLE,
    HEADERS,
    build_evaluation_cases,
    parse_evaluation_workbook,
    parse_screen_line,
)
from app.rag.evaluation_process import (
    KnowledgeDocument,
    draft_context,
    failed_draft,
    page_range,
    parse_draft_output,
    process_draft,
    process_headings,
    readable_as_pages,
    resolve_document_name,
    split_process_sections,
)
from app.schemas.document import DocumentSummary
from app.schemas.evaluation import EvaluationProcessRow, EvaluationScreen
from tests.support import AsgiTestClient

client = AsgiTestClient(app)
RECORDED = (
    Path(__file__).parent / "fixtures" / "evaluation_process" / "draft_response.json"
).read_text(encoding="utf-8")
PROCESS_TEXT = "【回答の手順】\n1. 設定画面Aを開く\n【検索する資料】\n01_操作手順書A.pdf p.12-14"


def _workbook(rows: list[list[object]]) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = CASES_SHEET_TITLE
    sheet.append(list(HEADERS))
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


# ---- 案内すべき画面の列 ----------------------------------------------------------------------


def test_screen_line_reads_name_and_aliases() -> None:
    assert parse_screen_line("設定画面A | 別名: 環境設定、設定 A") == EvaluationScreen(
        name="設定画面A", aliases=["環境設定", "設定 A"]
    )
    assert parse_screen_line("画面B") == EvaluationScreen(name="画面B")
    assert "読めません" in str(parse_screen_line("画面B | p.3"))


def test_workbook_reads_screens_and_answer_process_columns() -> None:
    parsed = parse_evaluation_workbook(
        _workbook([["通知が出ない", None, None, "設定画面A\n画面B", "回答", PROCESS_TEXT]])
    )

    assert parsed.issues == []
    row = parsed.rows[0]
    assert row.response_decision == "answer"
    assert row.screen_lines == ("設定画面A", "画面B")
    assert row.answer_process == PROCESS_TEXT
    built = build_evaluation_cases(parsed.rows, [])
    assert built.cases[0].expected_response_decision == "answer"
    # 回答の過程はケースの期待にしない（下書きにして確かめてから足す）。
    assert built.cases[0].required_answer_points == ["設定画面A", "画面B"]


def test_workbook_reports_unreadable_screen_line_with_row_and_column() -> None:
    parsed = parse_evaluation_workbook(_workbook([["質問", None, None, "画面A", "不明な判断"]]))
    assert parsed.rows == []
    assert parsed.issues[0].row == 2
    assert parsed.issues[0].column == "期待する応答の判断"


class _FakeOracle:
    async def list_documents(self, **kwargs: Any) -> list[DocumentSummary]:
        now = datetime(2026, 10, 1, tzinfo=UTC)
        return [
            DocumentSummary(
                id="doc-a", file_name="操作手順書A.pdf", status="INDEXED", uploaded_at=now
            ),
            DocumentSummary(
                id="doc-c", file_name="別の資料C.xlsx", status="INDEXED", uploaded_at=now
            ),
        ]


def test_import_api_returns_screens_and_process_in_preview(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(evaluation_route, "OracleClient", _FakeOracle)

    response = client.post(
        "/api/evaluation/cases/import",
        files={
            "file": (
                "cases.xlsx",
                _workbook([["通知が出ない", None, None, "設定画面A", "回答", PROCESS_TEXT]]),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={"knowledge_base_ids": ["kb-1"]},
    )

    data = response.json()["data"]
    assert data["errors"] == []
    assert data["rows"][0]["required_answer_points"] == ["設定画面A"]
    assert data["rows"][0]["answer_process"] == PROCESS_TEXT
    assert data["cases"][0]["required_answer_points"] == ["設定画面A"]


# ---- 回答の過程から期待の下書き ----------------------------------------------------------------


def _row(**overrides: Any) -> EvaluationProcessRow:
    values: dict[str, Any] = {
        "case_id": "row-2",
        "row": 2,
        "question": "画面Aで通知が出ない",
        "answer_process": PROCESS_TEXT,
    }
    values.update(overrides)
    return EvaluationProcessRow(**values)


def test_page_range_reads_written_pages() -> None:
    assert page_range("12-14") == (12, 14)
    assert page_range("p.3") == (3, None)
    assert page_range("１２〜１２頁") == (12, None)
    assert page_range("") is None


def test_process_draft_maps_recorded_response_to_evaluation_expectations() -> None:
    """録画の応答を、手順・条件・対応・根拠の期待にする。"""
    draft = process_draft(
        _row(), parse_draft_output(RECORDED), [KnowledgeDocument("doc-a", "操作手順書A.pdf")]
    )

    assert draft.error is None
    # 手順は検索の手がかり（画面名・機能名）の先頭。
    assert draft.required_answer_points == [
        "設定画面Aを開く — 通知の設定の画面を開く",
        "通知の設定を有効にする — 注意: 保存を押すまで反映されない",
    ]
    assert draft.required_conditions == ["利用者が管理者か"]
    # 手順があり、窓口への引き継ぎがある。
    assert draft.expected_outcomes == ["answered", "conditional", "needs_human"]
    assert draft.relevant_document_ids == ["doc-a"]
    evidence = draft.required_evidence[0]
    assert (evidence.document_id, evidence.page_start, evidence.page_end) == ("doc-a", 12, 14)
    # 解決できない資料は警告にする（期待には入れない）。
    assert [ref.status for ref in draft.references] == ["resolved", "unresolved"]
    assert any("未登録の資料B.pdf" in warning for warning in draft.warnings)


def test_process_draft_does_not_turn_spreadsheet_rows_into_pages() -> None:
    """表計算の資料の行・頁として読めない位置は、根拠の頁の期待にしない（#2663）。"""
    recorded = json.loads(RECORDED)
    recorded["references"] = [
        {"document_name": "設定一覧D.xlsx", "sheet_name": "シートA", "rows": "42"},
        {"document_name": "操作手順書A.pdf", "pages": "行42"},
    ]
    draft = process_draft(
        _row(),
        parse_draft_output(json.dumps(recorded)),
        [
            KnowledgeDocument("doc-a", "操作手順書A.pdf"),
            KnowledgeDocument("doc-s", "設定一覧D.xlsx", spreadsheet=True),
        ],
    )

    assert draft.relevant_document_ids == ["doc-s", "doc-a"]
    assert draft.required_evidence == []
    assert any("頁の範囲として読めません" in warning for warning in draft.warnings)


def test_process_draft_reports_failed_rows() -> None:
    draft = failed_draft(_row(), "失敗")

    assert draft.error == "失敗"
    assert draft.required_answer_points == []


class _Llm:
    def __init__(self, responses: dict[str, str | Exception]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def generate(self, prompt: str, context: str, **kwargs: Any) -> str:
        question = json.loads(context)["question"]
        self.calls.append({"question": question, "context": context, **kwargs})
        response = self.responses[question]
        if isinstance(response, Exception):
            raise response
        return response


def test_process_drafts_api_structures_each_row(monkeypatch: MonkeyPatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(ai_calls, "_sleep", no_sleep)
    monkeypatch.setattr(evaluation_route, "OracleClient", _FakeOracle)
    llm = _Llm({"画面Aで通知が出ない": RECORDED, "画面Bの出力": ValueError("応答なし")})
    monkeypatch.setattr(evaluation_route, "evaluation_process_llm", lambda _settings: llm)

    response = client.post(
        "/api/evaluation/cases/process-drafts",
        json={
            "knowledge_base_ids": ["kb-1"],
            "rows": [
                _row().model_dump(),
                _row(case_id="row-3", row=3, question="画面Bの出力").model_dump(),
            ],
        },
    )

    assert response.status_code == 200, response.text
    first, second = response.json()["data"]["drafts"]
    assert first["case_id"] == "row-2" and first["required_answer_points"] == [
        "設定画面Aを開く — 通知の設定の画面を開く",
        "通知の設定を有効にする — 注意: 保存を押すまで反映されない",
    ]
    assert first["required_evidence"][0]["document_id"] == "doc-a"
    assert second["case_id"] == "row-3" and second["error"]
    # 構造化出力の schema・見出しで分けた節。
    assert llm.calls[0]["response_schema_name"] == "evaluation_process_draft"
    assert json.loads(llm.calls[0]["context"])["sections"][0]["field"] == "steps"


def test_process_drafts_api_limits_rows() -> None:
    rows = [_row(case_id=f"row-{index}", row=index).model_dump() for index in range(2, 14)]
    response = client.post(
        "/api/evaluation/cases/process-drafts", json={"knowledge_base_ids": ["kb-1"], "rows": rows}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("field", ["knowledge_base_ids", "rows"])
def test_process_drafts_api_requires_fields(field: str) -> None:
    body: dict[str, Any] = {"knowledge_base_ids": ["kb-1"], "rows": [_row().model_dump()]}
    body.pop(field)
    assert client.post("/api/evaluation/cases/process-drafts", json=body).status_code == 422


# ---- 節に分ける（見出しの語は言語資源）------------------------------------------------------

_SECTIONED_PROCESS = """【回答の手順】
1. 設定画面Aを開く
【必要な確認事項】
利用者が管理者か。
検索する資料：01_操作手順書A.pdf p.12-14
■検索の語
設定画面A、通知
補足:
言い切らない。手順書を開いて確かめる。"""
_HEADINGS = process_headings(builtin_pack())


def test_process_text_is_split_by_language_pack_headings() -> None:
    sections = split_process_sections(_SECTIONED_PROCESS, _HEADINGS)
    assert [section.field for section in sections] == [
        "steps",
        "conditions",
        "references",
        "search_terms",
        "notes",
    ]
    # 見出しの後ろの内容（「検索する資料：…」）は節の本文にする。
    assert sections[2].text.startswith("01_操作手順書A.pdf")
    # 本文の中の「手順書を開いて」は見出しにしない。
    assert "手順書を開いて確かめる" in sections[4].text
    context = json.loads(draft_context(_row(answer_process=_SECTIONED_PROCESS), _HEADINGS))
    assert context["question"] == "画面Aで通知が出ない"
    assert [item["heading"] for item in context["sections"]][:2] == ["回答の手順", "必要な確認事項"]


def test_text_without_headings_is_one_section_and_headings_are_configurable() -> None:
    sections = split_process_sections("設定画面Aを開いて通知を有効にする", _HEADINGS)
    assert [(section.field, section.text) for section in sections] == [
        (None, "設定画面Aを開いて通知を有効にする")
    ]
    custom = split_process_sections("[やること]\n画面を開く", {"やること": "steps"})
    assert [(section.field, section.text) for section in custom] == [("steps", "画面を開く")]


# ---- 資料名の解決 ---------------------------------------------------------------------------

_DOCUMENTS = [
    KnowledgeDocument("doc-a", "操作手順書A.pdf"),
    KnowledgeDocument("doc-c", "別の資料C.xlsx"),
]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("操作手順書A.pdf", "doc-a"),
        ("操作手順書A", "doc-a"),
        ("01_操作手順書A.pdf", "doc-a"),
        ("別の資料C", "doc-c"),
    ],
    ids=["same", "no-extension", "prefixed", "other"],
)
def test_reference_names_resolve_to_knowledge_base_documents(name: str, expected: str) -> None:
    found, candidates = resolve_document_name(name, _DOCUMENTS)
    assert found is not None and found.document_id == expected
    assert candidates == []


def test_unresolved_and_ambiguous_reference_names() -> None:
    assert resolve_document_name("未登録の資料B.pdf", _DOCUMENTS) == (None, [])
    twins = [KnowledgeDocument("d1", "手順書A.pdf"), KnowledgeDocument("d2", "手順書A.docx")]
    found, candidates = resolve_document_name("手順書A", twins)
    assert found is None
    assert [item.document_id for item in candidates] == ["d1", "d2"]


def test_duplicate_uploads_of_one_canonical_resolve_to_the_canonical() -> None:
    canonical = KnowledgeDocument("doc-a", "手順書A.pdf")
    duplicate = KnowledgeDocument("doc-b", "手順書A.pdf", duplicate_of="doc-a")
    for documents in ([canonical, duplicate], [duplicate, canonical]):
        found, candidates = resolve_document_name("01_手順書A.pdf", documents)
        assert found == canonical
        assert candidates == []
    # 名前が重複のファイル名にだけ当たっても、見える正本の文書にする（chunk は正本にある）。
    renamed = KnowledgeDocument("doc-b", "手順書A_写し.pdf", duplicate_of="doc-a")
    assert resolve_document_name("手順書A_写し", [canonical, renamed]) == (canonical, [])


def test_same_name_with_other_content_or_invisible_canonical_stays_ambiguous() -> None:
    # 同じ名前でも別の内容（重複でない）の文書は、どちらかに決めない。
    other = [KnowledgeDocument("doc-a", "手順書A.pdf"), KnowledgeDocument("doc-c", "手順書A.pdf")]
    found, candidates = resolve_document_name("手順書A.pdf", other)
    assert found is None
    assert [item.document_id for item in candidates] == ["doc-a", "doc-c"]
    # 正本が選んだ文書に無い（範囲の外・見られない・無い）重複は、まとめない。
    hidden = [
        KnowledgeDocument("doc-b", "手順書A.pdf", duplicate_of="doc-x"),
        KnowledgeDocument("doc-d", "手順書A.pdf", duplicate_of="doc-x"),
    ]
    found, candidates = resolve_document_name("手順書A.pdf", hidden)
    assert found is None
    assert [item.document_id for item in candidates] == ["doc-b", "doc-d"]
    # 1 件だけなら、その文書のまま（見えない正本の id を参照にしない）。
    found, _ = resolve_document_name("手順書A.pdf", hidden[:1])
    assert found is not None and found.document_id == "doc-b"


# ---- 資料の位置（#2663）----------------------------------------------------------------------


def test_readable_as_pages_uses_page_words_only() -> None:
    for text in ("12-14", "p.12〜14、20", "ｐ．３～５", "12頁", "12〜14ページ", "pp. 3-5", ""):
        assert readable_as_pages(text), text
    for text in ("行42", "42行目", "A1:C3", "シートA 3"):
        assert not readable_as_pages(text), text


@pytest.mark.parametrize(
    ("reference", "message"),
    [
        # 表計算の資料の頁の欄の行の番号は頁にしない。
        ({"document_name": "設定一覧D.xlsx", "pages": "42"}, "根拠の位置にできません"),
        # シートの名前の無い行は、どのシートの行か決まらない。
        ({"document_name": "設定一覧D.xlsx", "rows": "42"}, "読めません"),
        # 読めないセルの範囲。
        (
            {"document_name": "設定一覧D.xlsx", "sheet_name": "シートA", "cells": "どこか"},
            "読めません",
        ),
        # 頁の資料にシート・行は使えない。
        (
            {"document_name": "操作手順書A.pdf", "sheet_name": "シートA", "rows": "3"},
            "根拠の位置に",
        ),
    ],
)
def test_location_problems_are_warnings_and_never_pages(
    reference: dict[str, str], message: str
) -> None:
    draft = process_draft(
        _row(),
        parse_draft_output(json.dumps({"references": [reference]})),
        [
            KnowledgeDocument("doc-a", "操作手順書A.pdf"),
            KnowledgeDocument("doc-s", "設定一覧D.xlsx", spreadsheet=True),
        ],
    )
    assert any(message in warning for warning in draft.warnings), draft.warnings
    assert draft.required_evidence == []


def test_readable_sheet_location_has_no_warning() -> None:
    references = [
        {"document_name": "設定一覧D.xlsx", "sheet_name": "シートA", "rows": "42"},
        {"document_name": "設定一覧D.xlsx", "sheet_name": "シートA", "cells": "ｂ３：ｄ１０"},
    ]
    draft = process_draft(
        _row(),
        parse_draft_output(json.dumps({"references": references})),
        [KnowledgeDocument("doc-s", "設定一覧D.xlsx", spreadsheet=True)],
    )
    assert draft.warnings == []
    assert draft.relevant_document_ids == ["doc-s"]


def test_unparsable_response_is_rejected() -> None:
    for raw in ("応答なし", '{"steps": "手順"}'):
        with pytest.raises(ValueError):
            parse_draft_output(raw)
