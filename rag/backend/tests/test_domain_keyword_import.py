"""Excel の検証・プレビュー非保存・取込モードを実データを使わず確かめる。"""

import io
from collections.abc import Sequence

import pytest
from openpyxl import Workbook  # type: ignore[import-untyped]

from app.api.routes import search_answer_profile_knowledge as route
from app.main import app
from tests.support import AsgiTestClient
from tests.test_search_answer_profile_domain_keywords import FakeKnowledgeOracle

BASE = "/api/search-answer-profiles/bv-1/domain-keywords"
client = AsgiTestClient(app)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeKnowledgeOracle:
    fake = FakeKnowledgeOracle()
    monkeypatch.setattr(route, "OracleClient", lambda: fake)
    return fake


def excel(rows: Sequence[Sequence[object]]) -> dict[str, tuple[str, bytes, str]]:
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    for row in rows:
        sheet.append(row)
    data = io.BytesIO()
    workbook.save(data)
    workbook.close()
    return {"file": ("keywords.xlsx", data.getvalue(), "application/octet-stream")}


def test_preview_deduplicates_without_saving_and_import_modes(store: FakeKnowledgeOracle) -> None:
    client.put(BASE, json={"keywords": ["既存語", "ABC"]})
    files = excel([[" keyword "], [" ＡＢＣ "], ["abc"], [""], ["追加語"]])
    preview = client.post(f"{BASE}/import/preview", files=files)
    assert preview.status_code == 200
    assert preview.json()["data"] == {
        "total": 2,
        "rows": [{"row": 2, "keyword": "ABC"}, {"row": 5, "keyword": "追加語"}],
    }
    assert client.get(BASE).json()["data"]["keywords"] == ["既存語", "ABC"]
    added = client.post(f"{BASE}/import", files=files)
    assert added.json()["data"]["keywords"] == ["既存語", "ABC", "追加語"]
    assert added.json()["data"]["inserted_count"] == 1
    again = client.post(f"{BASE}/import", files=files)
    assert again.json()["data"]["inserted_count"] == 0
    replaced = client.post(f"{BASE}/import", files=files, data={"mode": "REPLACE"})
    assert replaced.json()["data"]["keywords"] == ["ABC", "追加語"]
    assert ("bv-1", "domain_keywords") in store.payloads


@pytest.mark.parametrize(
    "rows",
    [
        [["OTHER"], ["語"]],
        [["KEYWORD"]],
        [["KEYWORD"], [" "]],
        [["KEYWORD", "keyword"], ["a", "b"]],
        [["KEYWORD"], ["a" * 121]],
        [["KEYWORD"], *[[f"語{i}"] for i in range(1001)]],
        [["KEYWORD"], *[["同じ語"] for _ in range(10001)]],
    ],
    ids=["header", "empty", "blank", "duplicate-header", "length", "count", "rows"],
)
def test_invalid_excel_never_saves(store: FakeKnowledgeOracle, rows: list[list[object]]) -> None:
    client.put(BASE, json={"keywords": ["保存語"]})
    before = dict(store.payloads)
    files = excel(rows)
    for suffix in ("/import/preview", "/import"):
        assert client.post(BASE + suffix, files=files).status_code == 422
        assert store.payloads == before


def test_preview_is_bounded_and_append_overflow_is_rejected(store: FakeKnowledgeOracle) -> None:
    client.put(BASE, json={"keywords": [f"語{i}" for i in range(1000)]})
    files = excel([["KEYWORD"], *[[f"追加{i}"] for i in range(11)]])
    preview = client.post(f"{BASE}/import/preview", files=files).json()["data"]
    assert preview["total"] == 11
    assert len(preview["rows"]) == 10
    assert client.post(f"{BASE}/import", files=files).status_code == 422
    assert len(client.get(BASE).json()["data"]["keywords"]) == 1000
    assert client.put(BASE, json={"keywords": [f"語{i}" for i in range(1001)]}).status_code == 422
    assert len(client.get(BASE).json()["data"]["keywords"]) == 1000
    duplicates = client.put(BASE, json={"keywords": [" Ａ ", "a", ""] * 600})
    assert duplicates.status_code == 200
    assert duplicates.json()["data"]["keywords"] == ["A"]


def test_bad_file_mode_size_and_missing_profile_do_not_save(
    store: FakeKnowledgeOracle, monkeypatch: pytest.MonkeyPatch
) -> None:
    files = excel([["KEYWORD"], ["語"]])
    assert client.post(f"{BASE}/import", files=files, data={"mode": "BAD"}).status_code == 422
    for suffix in ("/import/preview", "/import"):
        assert client.post(BASE.replace("bv-1", "missing") + suffix, files=files).status_code == 404
        for name, content in (("bad.csv", b"KEYWORD\na"), ("bad.xlsx", b"invalid")):
            assert client.post(BASE + suffix, files={"file": (name, content)}).status_code == 422
    monkeypatch.setattr(route, "MAX_FAQ_EXCEL_BYTES", 10)
    assert client.post(f"{BASE}/import/preview", files=files).status_code == 413
    assert client.post(f"{BASE}/import", files=files).status_code == 413
    assert not store.payloads
