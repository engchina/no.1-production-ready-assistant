"""採点の部品（#3240）。正しい入力と、故意に壊した入力の両方で確かめる。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from pr_evaluation_scoring import (
    OUTCOMES,
    MetricAggregate,
    ThresholdFailure,
    all_correct,
    contains_normalized,
    document_hits,
    evidence_matches,
    expected_decisions,
    location_matches,
    overall_passed,
    reciprocal_rank,
    response_decision,
    rounded_mean,
    skips_retrieval,
    table_row_texts,
    text_matches,
    text_outside_tables,
    threshold_failures,
    unique_in_order,
)


@dataclass(frozen=True)
class Criteria:
    document_id: str | None = None
    text: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None
    sheet: str | None = None
    row_start: int | None = None
    row_end: int | None = None


def test_contains_normalized_ignores_width_case_and_spaces() -> None:
    assert contains_normalized("ＡＢＣ　の 設定\n値", "abc の設定値")
    assert not contains_normalized("設定値", "")
    assert not contains_normalized("設定値", "別の値")


def test_outcome_vocabulary_and_decisions() -> None:
    assert OUTCOMES == (
        "answered",
        "conditional",
        "needs_clarification",
        "needs_environment_data",
        "needs_human",
        "insufficient_evidence",
    )
    assert [response_decision(item) for item in OUTCOMES] == [
        "answer",
        "answer",
        "clarify",
        "hold",
        "hold",
        "hold",
    ]
    assert expected_decisions("hold", ["answered"]) == {"hold"}
    assert expected_decisions(None, ["answered", "needs_clarification"]) == {"answer", "clarify"}
    assert expected_decisions(None, []) == set()


def test_skips_retrieval_only_without_citations() -> None:
    assert skips_retrieval("needs_clarification", has_citations=False)
    assert not skips_retrieval("needs_clarification", has_citations=True)
    assert not skips_retrieval("answered", has_citations=False)
    assert not skips_retrieval(None, has_citations=False)


@pytest.mark.parametrize(
    ("values", "expected"),
    [([True, True], True), ([True, None], None), ([None, False], False), ([], None)],
    ids=["all-true", "unknown", "false-wins", "empty"],
)
def test_all_correct_is_a_three_valued_and(
    values: list[bool | None], expected: bool | None
) -> None:
    assert all_correct(values) is expected


def test_retrieval_rank_and_hits() -> None:
    assert unique_in_order(["b", "a", "b"]) == ["b", "a"]
    assert reciprocal_rank(["x", "y", "a"], {"a"}) == pytest.approx(1 / 3)
    assert reciprocal_rank(["x"], {"a"}) == 0.0
    assert document_hits(["a", "x", "a", "b"], {"a", "b"}) == ["a", "b"]


LOCATOR = {"page_start": 3, "page_end": 4, "section_path": ["2. 手順"], "sheet_name": " Sheet1 "}


@pytest.mark.parametrize(
    ("criteria", "matched"),
    [
        (Criteria(document_id="d1"), True),
        (Criteria(document_id="d2"), False),
        (Criteria(page_start=4), True),
        (Criteria(page_start=5, page_end=6), False),
        (Criteria(section="手順"), True),
        (Criteria(section="別の章"), False),
        (Criteria(sheet="sheet1"), True),
        (Criteria(row_start=2), False),
    ],
    ids=["doc", "other-doc", "page", "other-page", "section", "other-section", "sheet", "no-rows"],
)
def test_location_conditions(criteria: Criteria, matched: bool) -> None:
    assert location_matches(criteria, document_id="d1", locator=LOCATOR) is matched


def test_page_number_is_the_fallback_and_unknown_places_do_not_match() -> None:
    assert location_matches(Criteria(page_start=2), document_id="d", locator={"page_number": "2"})
    assert not location_matches(Criteria(page_start=1), document_id="d", locator={})
    assert not location_matches(Criteria(page_start=1), document_id="d", locator={"page_start": 0})
    assert not location_matches(
        Criteria(page_start=1), document_id="d", locator={"page_start": True}
    )


@dataclass(frozen=True)
class BasisCriteria(Criteria):
    row_basis: str | None = None


@pytest.mark.parametrize(
    ("criteria", "locator", "matched"),
    [
        # 評価セットの行（原本の行）は、シート・CSV の行に一致する。
        (Criteria(sheet="一覧", row_start=4), {"sheet_name": "一覧", "row_start": 4}, True),
        (Criteria(row_start=4), {"row_basis": "csv", "row_start": 4, "row_end": 4}, True),
        # 解析が組み立てた表の格子の行は、同じ番号でも原本の行の条件に一致しない。
        (
            Criteria(row_start=4),
            {"row_basis": "parsed_table", "table_index": 1, "row_start": 4, "row_end": 4},
            False,
        ),
        (
            Criteria(sheet="一覧", row_start=4),
            {"row_basis": "parsed_table", "row_start": 4, "row_end": 4},
            False,
        ),
        # 基準を書いた条件は、同じ基準の行だけに一致する。
        (
            BasisCriteria(row_start=3, row_end=5, row_basis="parsed_table"),
            {"row_basis": "parsed_table", "row_start": 4, "row_end": 4},
            True,
        ),
        (
            BasisCriteria(row_start=4, row_basis="parsed_table"),
            {"row_basis": "csv", "row_start": 4, "row_end": 4},
            False,
        ),
        (
            BasisCriteria(row_start=4, row_basis="csv"),
            {"sheet_name": "一覧", "row_start": 4},
            False,
        ),
    ],
    ids=[
        "sheet",
        "csv",
        "parsed-vs-original",
        "parsed-vs-sheet",
        "parsed-criteria",
        "parsed-criteria-vs-csv",
        "csv-criteria-vs-sheet",
    ],
)
def test_rows_are_matched_within_the_same_row_basis(
    criteria: Criteria, locator: dict[str, object], matched: bool
) -> None:
    """行は同じ行の番号の基準の中だけで照合する（#3429）。"""
    assert location_matches(criteria, document_id="d", locator=locator) is matched


def test_table_rows_are_matched_one_row_at_a_time() -> None:
    html = (
        "前の本文<table><caption>表の見出し</caption>"
        "<tr><th>項目</th><th>値</th></tr>"
        "<tr><td>上限</td><td>30</td>"
        "<tr><td>下限<br>（任意）</td><td>5<table><tr><td>内側</td></tr></table></td></tr>"
        "</table>後の本文"
    )
    assert table_row_texts(html) == ["項目 値", "上限 30", "下限\n（任意） 5内側 \n"]
    assert text_outside_tables(html) == ["前の本文", "後の本文"]
    assert text_matches(html, "上限 30")
    assert text_matches(html, "下限（任意）")
    # 別の行の値を組み合わせない。表の見出しは行に入れない。表をまたいで本文をつながない。
    assert not text_matches(html, "30 下限")
    assert not text_matches(html, "表の見出し")
    assert not text_matches(html, "前の本文後の本文")


def test_evidence_matches_requires_every_written_condition() -> None:
    criteria = Criteria(document_id="d1", page_start=3, text="承認の期限")
    assert evidence_matches(criteria, document_id="d1", text="承認の期限は 5 日", locator=LOCATOR)
    assert not evidence_matches(criteria, document_id="d1", text="別の本文", locator=LOCATOR)
    assert not evidence_matches(criteria, document_id="d1", text="承認の期限", locator={})


def test_aggregate_means_count_only_measured_cases() -> None:
    aggregate = MetricAggregate(names=("mrr", "required_evidence_recall"))
    aggregate.add_all({"mrr": 1.0, "required_evidence_recall": None})
    aggregate.add("mrr", 0.5)
    assert aggregate.means() == {"mrr": 0.75, "required_evidence_recall": None}
    assert aggregate.counts() == {"mrr": 2, "required_evidence_recall": 0}
    assert rounded_mean([1.0, 0.0, 0.0]) == 0.3333
    assert rounded_mean([]) is None


def _passed(
    failures: list[ThresholdFailure],
    *,
    error_count: int = 0,
    thresholds: dict[str, float] | None = None,
    measured: dict[str, int] | None = None,
    values: dict[str, float | None] | None = None,
) -> bool | None:
    return overall_passed(
        failures=failures,
        error_count=error_count,
        incomplete_count=0,
        measured_counts={"mrr": 2} if measured is None else measured,
        thresholds=thresholds or {},
        values={"mrr": 0.5, "required_evidence_recall": None} if values is None else values,
    )


def test_thresholds_and_passed() -> None:
    values: dict[str, float | None] = {"mrr": 0.5, "required_evidence_recall": None}
    failures = threshold_failures({"mrr": 0.9, "required_evidence_recall": 0.9}, values)
    assert failures == [ThresholdFailure(metric="mrr", actual=0.5, threshold=0.9)]
    assert threshold_failures({"mrr": 0.9}, values, reference_only=["mrr"]) == []
    assert _passed(failures) is False
    assert _passed([], error_count=1) is False
    # 閾値を付けた指標が測れなければ合格にしない。どの指標も測れなければ合格にしない。
    assert _passed([], thresholds={"required_evidence_recall": 0.5}) is None
    assert _passed([], measured={"mrr": 0}, values={}) is None
    assert _passed([]) is True
