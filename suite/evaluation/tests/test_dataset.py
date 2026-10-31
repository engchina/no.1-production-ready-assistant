"""Dataset: 評価セットの読み込み・`file:` の解決・hash（#3240）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pr_suite_evaluation.dataset import (
    DatasetError,
    canonical_sha256,
    file_references,
    load_dataset,
    load_document_map,
    resolve_file_references,
)

REPO = Path(__file__).resolve().parents[3]
# 製品の評価の評価セット（合成）。suite は同じ JSON を自分の型で読む。
DATASETS = [
    REPO / "rag" / "evaluation" / "business-support" / "business-support.json",
    REPO / "rag" / "evaluation" / "multi-hop" / "multi-hop.json",
]


def _write(path: Path, value: Any) -> Path:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


RAW: dict[str, Any] = {
    "cases": [
        {
            "id": "a",
            "query": " 質問 A ",
            "relevant_document_ids": ["file:manual.pdf"],
            "equivalent_document_ids": {"file:manual.pdf": ["file:manual-copy.pdf"]},
            "required_evidence": [{"id": "e1", "document_id": "file:ledger.xlsx", "sheet": "表"}],
            "expected_outcomes": ["answered"],
            # suite が読まない項目（製品の評価だけが使う）は無視する。
            "required_answer_points": ["手順"],
        }
    ],
    "thresholds": {"mrr": 0.5, "answer_correctness": None},
    "rag_overrides": {"top_k": 5},
}
DOCUMENTS = {"manual.pdf": "doc-1", "manual-copy.pdf": "doc-2", "ledger.xlsx": "doc-3"}


def test_file_references_are_resolved_with_the_document_map(tmp_path: Path) -> None:
    assert file_references(RAW) == ["manual.pdf", "ledger.xlsx", "manual-copy.pdf"]
    resolved = resolve_file_references(RAW, DOCUMENTS)
    case = resolved["cases"][0]
    assert case["relevant_document_ids"] == ["doc-1"]
    assert case["equivalent_document_ids"] == {"doc-1": ["doc-2"]}
    assert case["required_evidence"][0]["document_id"] == "doc-3"
    # 元の評価セットは変えない。
    assert RAW["cases"][0]["relevant_document_ids"] == ["file:manual.pdf"]
    path = _write(tmp_path / "set.json", RAW)
    loaded = load_dataset(path, documents=DOCUMENTS)
    assert loaded.dataset.cases[0].query == "質問 A"
    assert loaded.dataset.cases[0].canonical_document_id("doc-2") == "doc-1"
    assert loaded.dataset.thresholds == {"mrr": 0.5}
    assert loaded.name == "set.json"
    assert loaded.sha256 == canonical_sha256(loaded.dataset.model_dump(mode="json"))


def test_unknown_or_unresolved_file_references_are_errors(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="ledger.xlsx"):
        resolve_file_references(RAW, {"manual.pdf": "doc-1", "manual-copy.pdf": "doc-2"})
    path = _write(tmp_path / "set.json", RAW)
    with pytest.raises(DatasetError, match="file:"):
        load_dataset(path)


def test_hash_follows_the_resolved_content(tmp_path: Path) -> None:
    path = _write(tmp_path / "set.json", RAW)
    first = load_dataset(path, documents=DOCUMENTS)
    other = load_dataset(path, documents={**DOCUMENTS, "manual.pdf": "doc-9"})
    reordered = _write(tmp_path / "again.json", dict(reversed(list(RAW.items()))))
    assert first.sha256 != other.sha256
    assert load_dataset(reordered, documents=DOCUMENTS).sha256 == first.sha256


@pytest.mark.parametrize(
    ("document_map", "message"),
    [({"documents": {"a.pdf": ""}}, "形が不正"), (["a.pdf"], "形が不正")],
    ids=["empty-id", "not-an-object"],
)
def test_document_map_shape(tmp_path: Path, document_map: Any, message: str) -> None:
    with pytest.raises(DatasetError, match=message):
        load_document_map(_write(tmp_path / "map.json", document_map))
    assert load_document_map(_write(tmp_path / "ok.json", {"documents": {"a.pdf": "d"}})) == {
        "a.pdf": "d"
    }


@pytest.mark.parametrize(
    "raw",
    [
        {"cases": [{"id": "a", "query": "q"}, {"id": "a", "query": "q"}]},
        {"cases": [{"id": "a", "query": "q"}], "thresholds": {"unknown_metric": 0.5}},
        {"cases": [{"id": "a", "query": "q", "required_evidence": [{"id": "e"}]}]},
        {"cases": []},
        # 行の番号の基準は行と一緒にだけ書ける（#3547）。
        {
            "cases": [
                {
                    "id": "a",
                    "query": "q",
                    "required_evidence": [{"id": "e", "text": "t", "row_basis": "parsed_table"}],
                }
            ]
        },
        {
            "cases": [
                {
                    "id": "a",
                    "query": "q",
                    "required_evidence": [{"id": "e", "row_start": 3, "row_basis": "page"}],
                }
            ]
        },
    ],
    ids=[
        "duplicate-id",
        "unknown-threshold",
        "evidence-without-condition",
        "no-case",
        "row-basis-without-row",
        "unknown-row-basis",
    ],
)
def test_invalid_datasets_are_rejected(tmp_path: Path, raw: dict[str, Any]) -> None:
    with pytest.raises(DatasetError):
        load_dataset(_write(tmp_path / "set.json", raw))


@pytest.mark.parametrize("path", DATASETS, ids=lambda path: path.parent.name)
def test_product_evaluation_datasets_are_readable(path: Path) -> None:
    """製品の評価の評価セットは、文書の対応表（合成の ID）で解決して読める。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    documents = {name: f"doc-{index}" for index, name in enumerate(file_references(raw))}
    loaded = load_dataset(path, documents=documents)
    assert len(loaded.dataset.cases) == len(raw["cases"])


def test_row_basis_selects_the_rows_of_the_same_basis(tmp_path: Path) -> None:
    """必要な根拠の行の番号の基準（#3547）: 書けばその基準の行だけ、省けば原本の行に一致する。"""
    from pr_evaluation_scoring.evidence import location_matches

    raw = {
        "cases": [
            {
                "id": "a",
                "query": "q",
                "required_evidence": [
                    {"id": "parsed", "row_start": 3, "row_basis": "parsed_table"},
                    {"id": "original", "row_start": 3},
                ],
            }
        ]
    }
    case = load_dataset(_write(tmp_path / "set.json", raw)).dataset.cases[0]
    parsed, original = case.required_evidence
    assert parsed.row_basis == "parsed_table" and original.row_basis is None
    grid_row = {"row_basis": "parsed_table", "table_index": 1, "row_start": 3, "row_end": 3}
    sheet_row = {"sheet_name": "表", "row_basis": "sheet", "row_start": 3, "row_end": 3}
    assert location_matches(parsed, document_id="d", locator=grid_row)
    assert not location_matches(parsed, document_id="d", locator=sheet_row)
    assert not location_matches(original, document_id="d", locator=grid_row)
    assert location_matches(original, document_id="d", locator=sheet_row)
