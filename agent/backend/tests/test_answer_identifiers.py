"""横線の違う値を一律置換せず、根拠と一意に対応するデータだけを復元する。"""

from copy import deepcopy

import pytest
from test_answer_evidence import _evidence, _step

from app.features.agent.answer_identifiers import IdentifierIndex, identifier_index


@pytest.mark.parametrize("dash", ["‐", "‑", "‒", "–", "—", "―", "−", "﹣", "－"])
def test_source_literal_is_restored_before_the_answer_is_published(dash: str) -> None:
    text = f"| id | value |\n|---|---|\n| item{dash}01 | 10 |\n"
    checked = IdentifierIndex(["item-01"]).answer(text)
    assert "| item-01 | 10 |" in checked.text
    assert checked.counts == {"corrected": 1, "ambiguous": 0}


def test_legitimate_unicode_and_case_are_preserved() -> None:
    index = IdentifierIndex(["row-a row‑b ROW-A"])
    checked = index.answer("id | value\n:---|---:\nrow‑a | 10\nrow-b | 20\nROW‑A | 30\n")
    assert checked.text == "id | value\n:---|---:\nrow-a | 10\nrow‑b | 20\nROW-A | 30\n"
    assert checked.corrected == 3
    assert index.scalar("row‑b").text == "row‑b"


def test_ambiguous_values_are_not_guessed_and_exact_values_remain_distinct() -> None:
    index = IdentifierIndex(["row-a row‑a"])
    assert index.scalar("row-a").counts == {"corrected": 0, "ambiguous": 0}
    assert index.scalar("row‑a").counts == {"corrected": 0, "ambiguous": 0}
    checked = index.scalar("row–a")
    assert checked.text == "row–a"
    assert checked.counts == {"corrected": 0, "ambiguous": 1}


def test_general_prose_code_blocks_and_urls_are_not_changed() -> None:
    text = (
        "一般文の row‑a と word‑word は変えない。\n\n"
        "```text\n`row‑a`\n| id |\n|---|\n| row‑a |\n```\n\n"
        "    `row‑a`\n"
        "https://example.test/row‑a\n"
        "`https://example.test/?q=row‑a`\n"
        "| id | link |\n|---|---|\n| row‑a | [row‑a](https://example.test/?q=row‑a) |\n"
    )
    checked = IdentifierIndex(["row-a word-word"]).answer(text)
    expected = text.replace("| row‑a | [row‑a]", "| row-a | [row-a]")
    assert checked.text == expected
    assert checked.corrected == 2


def test_inline_code_and_escaped_backticks_are_distinguished() -> None:
    text = "値は `row‑a`、一般文の row‑a と \\`row‑a\\` はそのまま。\n"
    assert IdentifierIndex(["row-a"]).answer(text).text == text.replace("`row‑a`", "`row-a`", 1)


def test_unknown_values_paths_and_parts_of_longer_identifiers_are_not_changed() -> None:
    index = IdentifierIndex(["row-a"])
    for value in [
        "row-z",
        "prefix-row‑a",
        "row‑a-suffix",
        "dir/row‑a",
        "row‑a.txt",
        "row‑a の説明",
    ]:
        assert index.scalar(value).text == value
    assert index.answer("| id |\n|---|\n| row‑z |\n").corrected == 0


def test_only_original_successful_source_values_are_used() -> None:
    sources = [
        _step(
            "rag_search",
            {
                "answer": "wrong‑01",
                "evidence": [_evidence("d:c:1", excerpt="actual-01", origin="source")],
            },
        ),
        _step(
            "rag_retrieve_evidence",
            {"evidence": [_evidence("d:c:2", excerpt="generated-01", origin="generated")]},
        ),
        _step("rag_read_source", {"document_id": "d", "chunk_id": "c3", "text": "read-01"}),
        _step("rag_search", {"evidence": [_evidence("d:c:4", excerpt="failed-01")]}, success=False),
        _step("nl2sql_query", {"status": "done", "rows": [{"id": "data-01"}]}),
        _step("nl2sql_query", {"status": "error", "rows": [{"id": "error-01"}]}),
    ]
    before = deepcopy(sources)
    index = identifier_index(sources)
    assert index.scalar("actual‑01").text == "actual-01"
    assert index.scalar("read‑01").text == "read-01"
    assert index.scalar("data‑01").text == "data-01"
    for value in ["wrong-01", "generated‑01", "failed‑01", "error‑01"]:
        assert index.scalar(value).text == value
    assert sources == before
