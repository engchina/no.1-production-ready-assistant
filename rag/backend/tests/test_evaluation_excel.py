"""品質評価の評価リクエストの Excel（#2065・#2086）: テンプレート・構造の検証・文書の解決・API。"""

from __future__ import annotations

from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import pytest
from openpyxl import Workbook, load_workbook  # type: ignore[import-untyped]

from app.api.routes import evaluation as evaluation_route
from app.main import app
from app.rag.evaluation import evidence_matches_chunk
from app.rag.evaluation_excel import (
    CASES_SHEET_TITLE,
    DOCUMENTS_SHEET_TITLE,
    EVALUATION_EXCEL_MAX_BYTES,
    EVALUATION_EXCEL_MAX_ROWS,
    EXAMPLES_SHEET_TITLE,
    GUIDE_SHEET_TITLE,
    HEADERS,
    EvaluationDocumentRef,
    build_evaluation_cases,
    evaluation_cases_xlsx,
    parse_evaluation_workbook,
    parse_evidence_line,
    template_xlsx,
)
from app.schemas.document import DocumentSummary
from app.schemas.evaluation import EvaluationEvidence
from app.schemas.search import RetrievedChunk
from tests.support import AsgiTestClient

client = AsgiTestClient(app)

DOCUMENTS = [
    EvaluationDocumentRef(document_id="doc-expense", file_name="経費精算規程.pdf"),
    EvaluationDocumentRef(document_id="doc-travel", file_name="出張旅費規程.docx"),
    EvaluationDocumentRef(
        document_id="doc-travel-old",
        file_name="出張旅費規程_旧版.docx",
        superseded=True,
        superseded_by_file_name="出張旅費規程.docx",
    ),
]


def _workbook(rows: list[list[object]], *, header: list[object] | None = None) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = CASES_SHEET_TITLE
    sheet.append(header if header is not None else list(HEADERS))
    for row in rows:
        sheet.append(row)
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


ALL_SHEETS = [CASES_SHEET_TITLE, GUIDE_SHEET_TITLE, EXAMPLES_SHEET_TITLE, DOCUMENTS_SHEET_TITLE]


def _sheet_text(sheet: Any) -> str:
    return "\n".join(str(cell.value) for row in sheet.iter_rows() for cell in row if cell.value)


def test_template_has_headers_guide_examples_and_document_sheets() -> None:
    book = load_workbook(BytesIO(template_xlsx()))

    assert book.sheetnames == ALL_SHEETS
    cases = book[CASES_SHEET_TITLE]
    assert [cell.value for cell in cases[1]] == [
        "質問",
        "期待する根拠",
        "標準回答",
        "必須の回答要点",
        "期待する応答の判断",
        "回答の過程",
    ]
    # 見出しのセルに意味・必須か任意か・書き方を注記する（形式ごとの位置の書き方も短く）。
    assert "必須" in cases["A1"].comment.text
    evidence_note = cases["B1"].comment.text
    for phrase in ("任意", "p.12", "スライド: 3", "シート: 名前 | 行: 10-12", "文書ID:"):
        assert phrase in evidence_note
    assert cases.max_row == 1
    # 文書を渡さなければ選択肢を付けず、「文書一覧」には案内だけを入れる。
    assert not cases.data_validations.dataValidation
    assert "ナレッジベースを選んでから" in _sheet_text(book[DOCUMENTS_SHEET_TITLE])
    guide_text = _sheet_text(book[GUIDE_SHEET_TITLE])
    for phrase in (
        "必須",
        "任意",
        "p.12-14",
        "章節:",
        "抜粋:",
        "照合の規則",
        "形式ごとの「期待する根拠」の書き方",
        "PowerPoint（PDF にしたもの）",
        "スライド: 3",
        "シート: 名前 | 行: 10-12",
        "Markdown・テキスト・HTML",
        "文書ID: 〈ID〉",
        "同じ名前の文書があるとき",
        f"1 ファイル {EVALUATION_EXCEL_MAX_ROWS} 行・10MB まで",
    ):
        assert phrase in guide_text


def test_template_examples_parse_but_are_not_imported() -> None:
    """記入例は別のシート（取込は「評価リクエスト」だけを読む）で、例の根拠はどれも読める形。"""
    data = template_xlsx()
    book = load_workbook(BytesIO(data))
    examples = list(book[EXAMPLES_SHEET_TITLE].iter_rows(min_row=2, values_only=True))
    assert len(examples) >= 6
    lines = [line for row in examples if row[1] for line in str(row[1]).split("\n")]
    parsed = [parse_evidence_line(line) for line in lines]
    specs = [item for item in parsed if not isinstance(item, str)]
    assert len(specs) == len(parsed), parsed
    assert any(item.slide for item in specs)
    assert any(item.sheet for item in specs)
    assert any(item.document_id for item in specs)
    # テンプレートのまま取り込むと、ケースが無い（記入例を評価しない）。
    assert "1 行もありません" in parse_evaluation_workbook(data).issues[0].message


def test_template_lists_documents_with_ids_and_offers_file_names() -> None:
    documents = [
        EvaluationDocumentRef(
            document_id="doc-sheet",
            file_name="料金表.xlsx",
            uploaded_at=datetime(2026, 10, 2, tzinfo=UTC),
            sheet_names=("料金", "一覧"),
        ),
        EvaluationDocumentRef(document_id="doc-slides", file_name="説明資料.pptx", page_count=12),
        EvaluationDocumentRef(document_id="doc-md", file_name="よくある質問.md"),
    ]

    book = load_workbook(BytesIO(template_xlsx(documents)))

    listing = list(book[DOCUMENTS_SHEET_TITLE].iter_rows(values_only=True))
    assert listing[0] == ("ファイル名", "文書 ID", "形式", "ページ数 / シート名", "アップロード日")
    assert listing[1] == (
        "料金表.xlsx",
        "doc-sheet",
        "表計算（Excel）",
        "シート: 料金、一覧",
        "2026-10-02",
    )
    assert listing[2][:4] == ("説明資料.pptx", "doc-slides", "PowerPoint", "12 スライド")
    assert listing[3][:3] == ("よくある質問.md", "doc-md", "Markdown")
    validations = book[CASES_SHEET_TITLE].data_validations.dataValidation
    assert len(validations) == 1
    assert validations[0].formula1 == f"'{DOCUMENTS_SHEET_TITLE}'!$A$2:$A$4"
    assert validations[0].showErrorMessage is False
    assert "B2" in str(validations[0].sqref)


def test_template_round_trips_through_the_parser() -> None:
    # 書き出した Excel（フィードバックの 1 行と同じ形）をそのまま読み込める。
    data = evaluation_cases_xlsx(
        [("経費精算の締め日はいつですか？", ["経費精算規程.pdf | p.3"], "毎月 25 日です。")]
    )

    parsed = parse_evaluation_workbook(data)

    assert parsed.issues == []
    assert [(row.row, row.question, row.standard_answer) for row in parsed.rows] == [
        (2, "経費精算の締め日はいつですか？", "毎月 25 日です。")
    ]
    assert parsed.rows[0].evidence[0].page_start == 3


def test_parse_reads_rows_and_skips_empty_rows() -> None:
    data = _workbook(
        [
            ["経費精算の締め日はいつですか？", "経費精算規程.pdf", "毎月 25 日です。"],
            [None, None, None],
            [
                "出張の宿泊費の上限は？",
                "出張旅費規程.docx | p.4-5 | 章節: 宿泊費\n"
                "出張旅費規程.docx｜抜粋：1 泊あたりの上限",
                None,
            ],
            ["社員食堂の来月の献立は？", "", ""],
        ],
        # 見出しの注記（「（必須）」）と英語の列名でも読める。
        header=["質問（必須）", "reference_contexts", "ground_truth"],
    )

    parsed = parse_evaluation_workbook(data)

    assert parsed.issues == []
    assert [row.row for row in parsed.rows] == [2, 4, 5]
    travel = parsed.rows[1]
    assert travel.case_id == "row-4"
    assert [
        (spec.file_name, spec.page_start, spec.page_end, spec.section, spec.text)
        for spec in travel.evidence
    ] == [
        ("出張旅費規程.docx", 4, 5, "宿泊費", None),
        ("出張旅費規程.docx", None, None, None, "1 泊あたりの上限"),
    ]
    assert parsed.rows[2].evidence == ()
    assert parsed.rows[2].standard_answer is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("経費精算規程.pdf", ("経費精算規程.pdf", None, None, None, None)),
        ("経費精算規程.pdf | p.3", ("経費精算規程.pdf", 3, None, None, None)),
        ("経費精算規程.pdf | P12-14", ("経費精算規程.pdf", 12, 14, None, None)),
        ("経費精算規程.pdf | １２〜１４ページ", ("経費精算規程.pdf", 12, 14, None, None)),
        ("経費精算規程.pdf | p.5-5", ("経費精算規程.pdf", 5, None, None, None)),
        (
            "経費精算規程.pdf | 抜粋: 部門長が承認する | 章節：申請の手続き",
            ("経費精算規程.pdf", None, None, "申請の手続き", "部門長が承認する"),
        ),
    ],
    ids=["file-only", "page", "page-range", "fullwidth-page", "same-page", "excerpt-and-section"],
)
def test_parse_evidence_line(line: str, expected: tuple[object, ...]) -> None:
    spec = parse_evidence_line(line)

    assert not isinstance(spec, str)
    assert (spec.file_name, spec.page_start, spec.page_end, spec.section, spec.text) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "説明資料.pptx | スライド: 3",
            {"file_name": "説明資料.pptx", "page_start": 3, "slide": True},
        ),
        (
            "料金表.xlsx | シート：料金 | 行: 10〜12",
            {"file_name": "料金表.xlsx", "sheet": "料金", "row_start": 10, "row_end": 12},
        ),
        ("料金表.xlsx | 行: 7", {"file_name": "料金表.xlsx", "row_start": 7, "row_end": None}),
        (
            "文書ID: 0123abcd | p.5",
            {"file_name": None, "document_id": "0123abcd", "page_start": 5},
        ),
        (
            "製品マニュアル.pdf | 文書 ID: 0123abcd",
            {"file_name": "製品マニュアル.pdf", "document_id": "0123abcd"},
        ),
    ],
    ids=["slide", "sheet-and-rows", "single-row", "document-id-first", "document-id-after-name"],
)
def test_parse_evidence_line_locations_and_document_id(line: str, expected: dict[str, Any]) -> None:
    """形式ごとの位置（スライド・シート・行）と文書 ID を読む（#2086）。"""
    spec = parse_evidence_line(line)

    assert not isinstance(spec, str)
    assert {key: getattr(spec, key) for key in expected} == expected


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("p.3 | 経費精算規程.pdf", "先頭に文書のファイル名"),
        ("経費精算規程.pdf | 3 ページ目あたり", "を読めません"),
        ("経費精算規程.pdf | p.14-12", "小さい番号から"),
        ("経費精算規程.pdf | p.0", "1 以上の番号"),
        ("経費精算規程.pdf | p.3 | p.4", "ページとスライドは 1 行に 1 つ"),
        ("経費精算規程.pdf | p.3 | スライド: 4", "どちらか 1 つ"),
        ("料金表.xlsx | 行: 12-10", "小さい番号から"),
        ("料金表.xlsx | 行: 上の方", "番号で書いて"),
        ("料金表.xlsx | 行: 2 | 行: 3", "行は 1 行に 1 つ"),
        ("料金表.xlsx | シート:", "シートの名前を書いて"),
        ("文書ID:", "文書の ID を書いて"),
        ("シート: 料金 | 料金表.xlsx", "先頭に文書のファイル名か「文書ID"),
        ("経費精算規程.pdf | 章節:", "見出しを書いて"),
        ("経費精算規程.pdf | 抜粋: " + "あ" * 301, "300 文字以内"),
    ],
    ids=[
        "no-file-name",
        "unknown-part",
        "reversed-pages",
        "page-zero",
        "two-pages",
        "page-and-slide",
        "reversed-rows",
        "row-not-number",
        "two-rows",
        "empty-sheet",
        "empty-document-id",
        "sheet-first",
        "empty-section",
        "long-excerpt",
    ],
)
def test_parse_evidence_line_errors(line: str, message: str) -> None:
    result = parse_evidence_line(line)

    assert isinstance(result, str)
    assert message in result


def test_parse_reports_row_and_column_errors() -> None:
    data = _workbook(
        [
            [None, "経費精算規程.pdf", "回答"],
            ["質" * 2001, None, None],
            ["正しい行", "経費精算規程.pdf\n経費精算規程.pdf | p.9-1", None],
            ["正しい行 2", None, "長" * 8001],
        ]
    )

    parsed = parse_evaluation_workbook(data)

    assert [(issue.row, issue.column) for issue in parsed.issues] == [
        (2, "質問"),
        (3, "質問"),
        (4, "期待する根拠"),
        (5, "標準回答"),
    ]
    assert "質問を入力してください" in parsed.issues[0].message
    assert "2000 文字以内" in parsed.issues[1].message
    # セルの中の何行目の根拠かも示す。
    assert parsed.issues[2].message.startswith("2 行目: ")
    assert parsed.rows == []


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"not an excel file", "Excel（.xlsx）として読めません"),
        (b"0" * (EVALUATION_EXCEL_MAX_BYTES + 1), "ファイルが大きすぎます"),
        (_workbook([], header=["問い", "答え"]), "「質問」の見出しがありません"),
        (_workbook([]), "1 行もありません"),
        (
            _workbook([[f"質問 {index}"] for index in range(EVALUATION_EXCEL_MAX_ROWS + 1)]),
            "200 行まで",
        ),
    ],
    ids=["not-xlsx", "too-large", "no-header", "no-rows", "too-many-rows"],
)
def test_parse_reports_file_errors(data: bytes, message: str) -> None:
    parsed = parse_evaluation_workbook(data)

    assert len(parsed.issues) == 1
    assert message in parsed.issues[0].message
    assert parsed.rows == []


def test_build_cases_resolves_file_names_in_the_knowledge_bases() -> None:
    parsed = parse_evaluation_workbook(
        _workbook(
            [
                ["締め日は？", "ｹｲﾋﾁｮｳ.PDF", None],
                [
                    "宿泊費の上限は？",
                    "経費精算規程.PDF\n出張旅費規程.docx | p.4 | 抜粋: 1 泊あたりの上限",
                    "1 泊 1 万円までです。",
                ],
                ["献立は？", None, None],
            ]
        )
    )
    documents = [
        *DOCUMENTS,
        EvaluationDocumentRef(document_id="doc-kana", file_name="ケイヒチョウ.pdf"),
    ]

    built = build_evaluation_cases(parsed.rows, documents)
    cases = built.cases

    assert built.issues == []
    assert built.warnings == []
    # NFKC（半角カナ）と大小文字の違いは無視する。
    assert cases[0].relevant_document_ids == ["doc-kana"]
    assert cases[0].required_evidence == []
    lodging = cases[1]
    assert lodging.id == "row-3"
    assert lodging.relevant_document_ids == ["doc-expense", "doc-travel"]
    assert [evidence.model_dump(exclude_none=True) for evidence in lodging.required_evidence] == [
        {
            "id": "e2",
            "document_id": "doc-travel",
            "text": "1 泊あたりの上限",
            "page_start": 4,
            "label": "出張旅費規程.docx | p.4 | 抜粋: 1 泊あたりの上限",
        }
    ]
    assert lodging.standard_answer == "1 泊 1 万円までです。"
    # 根拠も標準回答も無い行は、資料に答えが無い質問（拒答の正しさを測る）。
    assert cases[2].expects_answer is False


def test_build_cases_reports_unknown_and_superseded_documents() -> None:
    parsed = parse_evaluation_workbook(
        _workbook(
            [
                ["q1", "存在しない規程.pdf", None],
                ["q2", "文書ID: doc-missing", None],
                ["q3", "出張旅費規程_旧版.docx", None],
                ["q4", "文書ID: doc-travel-old | p.2", None],
                ["q5", "経費精算規程.pdf", None],
            ]
        )
    )

    built = build_evaluation_cases(parsed.rows, DOCUMENTS)

    assert [(issue.row, issue.column) for issue in built.issues] == [
        (2, "期待する根拠"),
        (3, "期待する根拠"),
        (4, "期待する根拠"),
        (5, "期待する根拠"),
    ]
    assert "選んだナレッジベースの文書にありません" in built.issues[0].message
    assert "文書 ID「doc-missing」の文書は" in built.issues[1].message
    assert "新しい版（出張旅費規程.docx）" in built.issues[2].message
    assert "新しい版の文書 ID" in built.issues[3].message
    assert [case.id for case in built.cases] == ["row-6"]


def test_build_cases_treats_same_named_documents_as_one_with_a_warning() -> None:
    """同じ名前の文書は誤りにせず警告にし、どれが引用されても一致とする（#2086）。"""
    parsed = parse_evaluation_workbook(
        _workbook([["q1", "手順書.pdf | p.3", None], ["q2", "文書ID: doc-old | p.3", None]])
    )
    documents = [
        EvaluationDocumentRef(
            document_id="doc-old",
            file_name="手順書.pdf",
            uploaded_at=datetime(2026, 9, 1, tzinfo=UTC),
        ),
        EvaluationDocumentRef(
            document_id="doc-new",
            file_name="手順書.PDF",
            uploaded_at=datetime(2026, 10, 1, tzinfo=UTC),
        ),
    ]

    built = build_evaluation_cases(parsed.rows, documents)

    assert built.issues == []
    by_name, by_id = built.cases
    # 新しくアップロードした文書を正解の文書にし、ほかを同じ文書とみなす。
    assert by_name.relevant_document_ids == ["doc-new"]
    assert by_name.equivalent_document_ids == {"doc-new": ["doc-old"]}
    assert by_name.required_evidence[0].document_id == "doc-new"
    assert by_name.canonical_document_id("doc-old") == "doc-new"
    # 文書 ID で書けば、その文書だけで照合する。
    assert by_id.relevant_document_ids == ["doc-old"]
    assert by_id.equivalent_document_ids == {}
    assert [(item.row, item.column) for item in built.warnings] == [(2, "期待する根拠")]
    assert "2 件あります" in built.warnings[0].message
    assert "文書ID: 〈ID〉" in built.warnings[0].message


@pytest.mark.parametrize(
    ("line", "file_name", "message"),
    [
        ("料金表.xlsx | p.3", "料金表.xlsx", "表計算の文書で、ページがありません"),
        ("料金表.xlsx | スライド: 3", "料金表.xlsx", "表計算の文書で、ページがありません"),
        ("よくある質問.md | p.2", "よくある質問.md", "ページがありません。「章節: 見出し」"),
        ("製品マニュアル.pdf | シート: 料金", "製品マニュアル.pdf", "表計算の文書ではない"),
        ("説明資料.pptx | 行: 3", "説明資料.pptx", "表計算の文書ではない"),
        (
            "別の名前.pdf | 文書ID: doc-1 | p.1",
            "製品マニュアル.pdf",
            "ファイル名は「製品マニュアル.pdf」です",
        ),
    ],
    ids=["sheet-page", "sheet-slide", "markdown-page", "pdf-sheet", "slides-row", "id-name"],
)
def test_build_cases_warns_about_locations_that_do_not_fit_the_format(
    line: str, file_name: str, message: str
) -> None:
    """文書の形式に合わない位置の条件は警告にする（評価は実行できる。#2086）。"""
    parsed = parse_evaluation_workbook(_workbook([["q", line, None]]))

    built = build_evaluation_cases(
        parsed.rows, [EvaluationDocumentRef(document_id="doc-1", file_name=file_name)]
    )

    assert built.issues == []
    assert len(built.cases) == 1
    assert len(built.warnings) == 1
    assert built.warnings[0].message.startswith("1 行目: ")
    assert message in built.warnings[0].message


def test_build_cases_accepts_locations_that_fit_the_format() -> None:
    rows: list[list[object]] = [
        ["q1", "料金表.xlsx | シート: 料金 | 行: 10-12", None],
        ["q2", "説明資料.pptx | スライド: 3", None],
        ["q3", "製品マニュアル.pdf | p.3 | 章節: 初期設定", None],
        ["q4", "よくある質問.md | 章節: はじめに | 抜粋: 対象", None],
    ]
    documents = [
        EvaluationDocumentRef(document_id=f"doc-{index}", file_name=str(row[1]).split(" | ")[0])
        for index, row in enumerate(rows)
    ]

    built = build_evaluation_cases(parse_evaluation_workbook(_workbook(rows)).rows, documents)

    assert built.issues == []
    assert built.warnings == []
    sheet = built.cases[0].required_evidence[0]
    assert (sheet.sheet, sheet.row_start, sheet.row_end) == ("料金", 10, 12)
    assert built.cases[1].required_evidence[0].page_start == 3


def _chunk(**metadata: Any) -> RetrievedChunk:
    return RetrievedChunk(
        document_id=metadata.pop("document_id", "doc-1"),
        chunk_id="doc-1:c1",
        text=metadata.pop("text", "申請は部門長が承認する。"),
        score=1.0,
        metadata=metadata,
    )


@pytest.mark.parametrize(
    ("evidence", "chunk", "matched"),
    [
        ({"page_start": 3}, _chunk(page_start=2, page_end=4), True),
        ({"page_start": 5, "page_end": 6}, _chunk(page_start=2, page_end=4), False),
        ({"page_start": 3}, _chunk(page_number="3"), True),
        ({"page_start": 3}, _chunk(), False),
        ({"section": "申請の手続き"}, _chunk(section_path="第 2 章 > 申請の手続き"), True),
        ({"section": "宿泊費"}, _chunk(section_path="第 2 章 > 申請の手続き"), False),
        ({"text": "部門長が承認"}, _chunk(), True),
        ({"text": "部門長が承認", "page_start": 3}, _chunk(page_start=3), True),
        ({"text": "部門長が承認"}, _chunk(document_id="doc-2"), False),
        ({"sheet": "料金"}, _chunk(sheet_name=" 料金 "), True),
        ({"sheet": "料金"}, _chunk(sheet_name="一覧"), False),
        ({"sheet": "料金"}, _chunk(page_start=1), False),
        ({"row_start": 10, "row_end": 12}, _chunk(row_start=12, row_end=20), True),
        ({"row_start": 10, "row_end": 12}, _chunk(row_start=13, row_end=20), False),
        ({"row_start": 10}, _chunk(page_start=10), False),
        (
            {"sheet": "料金", "row_start": 5},
            _chunk(sheet_name="料金", row_start="5", row_end="5"),
            True,
        ),
    ],
    ids=[
        "page-overlap",
        "page-outside",
        "page-number",
        "no-page-info",
        "section-partial",
        "section-other",
        "text",
        "text-and-page",
        "other-document",
        "sheet",
        "sheet-other",
        "sheet-missing",
        "rows-overlap",
        "rows-outside",
        "rows-missing",
        "sheet-and-row",
    ],
)
def test_evidence_matches_chunk(
    evidence: dict[str, Any], chunk: RetrievedChunk, matched: bool
) -> None:
    item = EvaluationEvidence(id="e1", document_id="doc-1", **evidence)

    assert evidence_matches_chunk(item, chunk) is matched


def test_evidence_matches_chunk_of_an_equivalent_document() -> None:
    """同じ文書とみなす文書の chunk は、正解の文書の ID にそろえて比べる（#2086）。"""
    item = EvaluationEvidence(id="e1", document_id="doc-new", text="部門長が承認")
    chunk = _chunk(document_id="doc-old")

    assert evidence_matches_chunk(item, chunk) is False
    assert evidence_matches_chunk(item, chunk, document_id="doc-new") is True


def test_evidence_requires_a_condition_and_ordered_ranges() -> None:
    with pytest.raises(ValueError, match="どれかを指定"):
        EvaluationEvidence(id="e1", document_id="doc-1")
    with pytest.raises(ValueError, match="page_start 以上"):
        EvaluationEvidence(id="e1", page_start=5, page_end=3)
    with pytest.raises(ValueError, match="row_start 以上"):
        EvaluationEvidence(id="e1", row_start=5, row_end=3)
    with pytest.raises(ValueError, match="row_start と一緒"):
        EvaluationEvidence(id="e1", row_end=3)
    assert EvaluationEvidence(id="e1", sheet="料金").sheet == "料金"


class _FakeOracle:
    def __init__(self) -> None:
        self.knowledge_base_ids: list[str | None] = []
        self.location_ids: list[str] = []

    async def evaluation_document_locations(
        self, document_ids: list[str]
    ) -> dict[str, tuple[int | None, tuple[str, ...]]]:
        self.location_ids.extend(document_ids)
        return {"doc-expense": (12, ())}

    async def list_documents(self, **kwargs: Any) -> list[DocumentSummary]:
        self.knowledge_base_ids.append(kwargs.get("knowledge_base_id"))
        now = datetime(2026, 10, 1, tzinfo=UTC)
        return [
            DocumentSummary(
                id="doc-expense", file_name="経費精算規程.pdf", status="INDEXED", uploaded_at=now
            ),
            DocumentSummary(
                id="doc-other", file_name="その他.pdf", status="INDEXED", uploaded_at=now
            ),
        ]


def _upload(
    data: bytes, *, name: str = "cases.xlsx", knowledge_base_ids: list[str] | None = None
) -> Any:
    return client.post(
        "/api/evaluation/cases/import",
        files={
            "file": (
                name,
                data,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={
            "knowledge_base_ids": knowledge_base_ids if knowledge_base_ids is not None else ["kb-1"]
        },
    )


def test_import_api_returns_cases_preview_and_document_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOracle()
    monkeypatch.setattr(evaluation_route, "OracleClient", lambda: fake)

    response = _upload(
        _workbook(
            [
                ["締め日は？", "経費精算規程.pdf | p.3", "毎月 25 日です。" * 30],
                ["献立は？", None, None],
            ]
        ),
        knowledge_base_ids=["kb-1", "kb-2", "kb-1"],
    )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["errors"] == []
    assert data["file_name"] == "cases.xlsx"
    assert data["row_count"] == 2
    assert data["knowledge_base_ids"] == ["kb-1", "kb-2"]
    assert fake.knowledge_base_ids == ["kb-1", "kb-2"]
    assert [case["id"] for case in data["cases"]] == ["row-2", "row-3"]
    assert data["cases"][0]["relevant_document_ids"] == ["doc-expense"]
    assert data["document_names"] == {"doc-expense": "経費精算規程.pdf"}
    preview = data["rows"][0]
    assert preview["evidence"] == ["経費精算規程.pdf | p.3"]
    assert preview["has_standard_answer"] is True
    assert preview["standard_answer_preview"].endswith("…")
    assert data["rows"][1]["expected_response_decision"] is None
    assert data["max_rows"] == EVALUATION_EXCEL_MAX_ROWS


def test_import_api_returns_row_errors_without_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evaluation_route, "OracleClient", _FakeOracle)

    response = _upload(
        _workbook([["締め日は？", "無い規程.pdf", None], [None, "経費精算規程.pdf", None]])
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["cases"] == []
    assert data["error_count"] == 2
    assert [(issue["row"], issue["column"]) for issue in data["errors"]] == [
        (2, "期待する根拠"),
        (3, "質問"),
    ]


def test_import_api_does_not_read_documents_without_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOracle()
    monkeypatch.setattr(evaluation_route, "OracleClient", lambda: fake)

    response = _upload(_workbook([["献立は？", None, None]]))

    assert response.json()["data"]["cases"][0]["id"] == "row-2"
    assert fake.knowledge_base_ids == []


def test_import_api_requires_knowledge_base_and_xlsx() -> None:
    no_kb = _upload(_workbook([["q", None, None]]), knowledge_base_ids=[" "])
    assert no_kb.status_code == 422
    assert "ナレッジベースを選んでください" in no_kb.json()["error_messages"][0]

    csv = _upload(b"q,a\n", name="cases.csv")
    assert csv.status_code == 200
    assert "Excel（.xlsx）" in csv.json()["data"]["errors"][0]["message"]


def test_template_api_returns_xlsx() -> None:
    response = client.get("/api/evaluation/cases/template")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "evaluation_request_template.xlsx" in response.headers["content-disposition"]
    assert load_workbook(BytesIO(response.content)).sheetnames == ALL_SHEETS


def test_template_api_lists_documents_of_the_selected_knowledge_bases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeOracle()
    monkeypatch.setattr(evaluation_route, "OracleClient", lambda: fake)

    response = client.get(
        "/api/evaluation/cases/template",
        params=[("knowledge_base_ids", "kb-1"), ("knowledge_base_ids", "kb-2")],
    )

    assert response.status_code == 200
    assert fake.knowledge_base_ids == ["kb-1", "kb-2"]
    listing = list(
        load_workbook(BytesIO(response.content))[DOCUMENTS_SHEET_TITLE].iter_rows(
            min_row=2, values_only=True
        )
    )
    # ファイル名の順。ページ数は chunk から求めたもの（取れない文書は空）。
    assert [row[:4] for row in listing] == [
        ("その他.pdf", "doc-other", "PDF", None),
        ("経費精算規程.pdf", "doc-expense", "PDF", "12 ページ"),
    ]
    assert sorted(fake.location_ids) == ["doc-expense", "doc-other"]


def test_import_api_returns_warnings_with_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evaluation_route, "OracleClient", _FakeOracle)

    response = _upload(_workbook([["締め日は？", "経費精算規程.pdf | シート: 料金", None]]))

    data = response.json()["data"]
    assert data["errors"] == []
    assert [case["id"] for case in data["cases"]] == ["row-2"]
    assert data["warning_count"] == 1
    assert data["warnings"][0]["row"] == 2
    assert "表計算の文書ではない" in data["warnings"][0]["message"]


@pytest.mark.parametrize(
    "text", ["=1+1", '=HYPERLINK("https://example.invalid","synthetic")', "#N/A"]
)
def test_generated_evaluation_cells_preserve_literal_text(text: str) -> None:
    data = evaluation_cases_xlsx([(text, ["synthetic.pdf | p.1"], text)])
    book = load_workbook(BytesIO(data), data_only=False)
    sheet = book[CASES_SHEET_TITLE]
    for address in ("A2", "C2"):
        assert sheet[address].data_type == "s"
        assert sheet[address].value == text
    parsed = parse_evaluation_workbook(data)
    assert parsed.issues == []
    assert [(row.question, row.standard_answer) for row in parsed.rows] == [(text, text)]


def test_template_document_names_and_evidence_are_literal_text() -> None:
    name = "=synthetic.pdf"
    document = EvaluationDocumentRef(document_id="synthetic-doc", file_name=name)
    data = evaluation_cases_xlsx([("合成の質問", [name], None)], documents=[document])
    book = load_workbook(BytesIO(data), data_only=False)
    for cell in (book[CASES_SHEET_TITLE]["B2"], book[DOCUMENTS_SHEET_TITLE]["A2"]):
        assert cell.value == name
        assert cell.data_type == "s"
    parsed = parse_evaluation_workbook(data)
    built = build_evaluation_cases(parsed.rows, [document])
    assert parsed.issues == built.issues == []
    assert built.cases[0].relevant_document_ids == [document.document_id]


def test_template_examples_keep_all_six_case_columns_and_round_trip() -> None:
    book = load_workbook(BytesIO(template_xlsx()))
    assert book.sheetnames == [
        CASES_SHEET_TITLE,
        GUIDE_SHEET_TITLE,
        EXAMPLES_SHEET_TITLE,
        DOCUMENTS_SHEET_TITLE,
    ]
    cases = book[CASES_SHEET_TITLE]
    assert tuple(cell.value for cell in cases[1]) == HEADERS
    assert all(cell.comment and cell.comment.text for cell in cases[1])
    examples = book[EXAMPLES_SHEET_TITLE]
    assert tuple(cell.value for cell in examples[1]) == (*HEADERS[:3], "この例の説明", *HEADERS[3:])
    rows = list(examples.iter_rows(min_row=2, values_only=True))
    assert any(row[4] for row in rows)
    assert {row[5] for row in rows if row[5]} == {"回答", "確認", "保留"}
    assert any(row[6] for row in rows)
    assert not any("別名:" in str(value) for row in rows for value in row if value)
    for row in rows:
        cases.append([*row[:3], *row[4:]])
    buffer = BytesIO()
    book.save(buffer)
    parsed = parse_evaluation_workbook(buffer.getvalue())
    assert parsed.issues == []
    assert len(parsed.rows) == len(rows)
    assert {row.response_decision for row in parsed.rows if row.response_decision} == {
        "answer",
        "clarify",
        "hold",
    }
    assert any(row.screen_lines for row in parsed.rows)
    assert any(row.answer_process for row in parsed.rows)
    documents = [
        EvaluationDocumentRef(document_id=f"synthetic-{index}", file_name=name)
        for index, name in enumerate(
            [
                "製品マニュアル.pdf",
                "操作手順.docx",
                "説明資料.pptx",
                "料金表.xlsx",
                "よくある質問.md",
            ]
        )
    ] + [EvaluationDocumentRef(document_id="0123456789abcdef", file_name="synthetic-id.pdf")]
    built = build_evaluation_cases(parsed.rows, documents)
    assert built.issues == []
    assert len(built.cases) == len(rows)
    assert any(case.required_answer_points for case in built.cases)
    assert {
        case.expected_response_decision for case in built.cases if case.expected_response_decision
    } == {"answer", "clarify", "hold"}


def test_template_guide_describes_current_metrics_and_unassessed_results() -> None:
    book = load_workbook(BytesIO(template_xlsx()))
    rows = list(book[GUIDE_SHEET_TITLE].iter_rows(values_only=True))
    metrics = [row for row in rows if row[0] in {"検索", "回答", "応答の判断"}]
    assert [row[0] for row in metrics] == ["検索", "検索", "回答", "回答", "回答", "応答の判断"]
    assert all(row[2] and row[3] for row in metrics)
    text = "\n".join(str(value) for row in rows for value in row if value)
    for phrase in [
        "未評価は 0 点でも合格でもありません",
        "同じ六指標・閾値",
        "所要時間は設定や処理状況",
        "表示用に丸める前",
        "入力しただけでは評価の期待に追加されません",
    ]:
        assert phrase in text
    assert "約 2 倍" not in text
    assert "閾値 90%" not in text
    assert "正確性・網羅性" in book[CASES_SHEET_TITLE]["C1"].comment.text


@pytest.mark.parametrize("endpoint", ["template", "import"])
def test_excel_scope_comes_from_selected_profile(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    from types import SimpleNamespace

    fake = _FakeOracle()

    async def get_profile(profile_id: str) -> Any:
        assert profile_id == "profile-a"
        return SimpleNamespace(
            status="ACTIVE",
            config=SimpleNamespace(normalized_knowledge_base_ids=lambda: ["kb-profile"]),
        )

    monkeypatch.setattr(fake, "get_search_answer_profile", get_profile, raising=False)
    monkeypatch.setattr(evaluation_route, "OracleClient", lambda: fake)
    if endpoint == "template":
        response = client.get(
            "/api/evaluation/cases/template", params={"search_answer_profile_id": "profile-a"}
        )
        assert response.status_code == 200, response.text
        assert fake.knowledge_base_ids == ["kb-profile"]
    else:
        response = client.post(
            "/api/evaluation/cases/import",
            files={"file": ("cases.xlsx", b"invalid", "application/octet-stream")},
            data={"search_answer_profile_id": "profile-a", "knowledge_base_ids": "kb-other"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["data"]["knowledge_base_ids"] == ["kb-profile"]
        assert response.json()["data"]["search_answer_profile_id"] == "profile-a"
