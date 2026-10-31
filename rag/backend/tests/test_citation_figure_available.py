"""根拠（citation）の ``figure_image_available``（元の図の画像を切り出せるか。#2433）のテスト。

画面は真のときだけ「図を開く」を出す（MCP の ``image_ref`` と同じ判定。``app.rag.figure_region``）。
検索で作るときは chunk の metadata から決め、保存した引用（metadata を許可の一覧に絞る）を読み直す
ときは保存した値を使う。内容はすべて合成のデータ。
"""

from __future__ import annotations

import json
from typing import Any

from pytest import MonkeyPatch

from app.api.routes import search as search_route
from app.main import app
from app.rag.figure_region import figure_region
from app.rag.stored_answer import stored_citations
from app.schemas.search import RetrievedChunk, SearchRequest, SearchResponse
from tests.support import AsgiTestClient

client = AsgiTestClient(app)
_PAGE = {"page_start": 2, "page_width": 600, "page_height": 800}


def _chunk(chunk_id: str, **metadata: Any) -> RetrievedChunk:
    return RetrievedChunk(
        document_id="doc-a",
        chunk_id=chunk_id,
        text="合成の本文。",
        score=0.5,
        file_name="文書 A.pdf",
        metadata=metadata,
    )


def test_flag_is_true_only_for_figures_with_a_stored_region() -> None:
    cases = {
        "figure": _chunk("c-1", content_kind="figure", bbox="[60, 80, 300, 200]", **_PAGE),
        "ratio": _chunk(
            "c-2", content_kind="figure", page_start=3, bbox=[0.1, 0.2, 0.5, 0.6], bbox_unit="ratio"
        ),
        # 図の領域を保存していない古い根拠・頁の大きさが無い・回転・はみ出し・図でないものは偽。
        "no_region": _chunk("c-3", content_kind="figure", page_start=2),
        "no_page_size": _chunk("c-4", content_kind="figure", page_start=4, bbox="[1, 2, 30, 40]"),
        "rotated": _chunk(
            "c-5", content_kind="figure", page_rotation=90, bbox="[60, 80, 300, 200]", **_PAGE
        ),
        "outside": _chunk("c-6", content_kind="figure", bbox="[60, 80, 900, 200]", **_PAGE),
        "table": _chunk("c-7", content_kind="table", bbox="[1, 2, 3, 4]", **_PAGE),
        "text": _chunk("c-8", content_kind="text"),
    }
    assert {key: chunk.figure_image_available for key, chunk in cases.items()} == {
        "figure": True,
        "ratio": True,
        "no_region": False,
        "no_page_size": False,
        "rotated": False,
        "outside": False,
        "table": False,
        "text": False,
    }
    # MCP の image_ref・図の API と同じ判定。
    for chunk in cases.values():
        assert chunk.figure_image_available is (figure_region(dict(chunk.metadata)) is not None)


def test_stored_citation_keeps_the_flag_after_the_metadata_is_narrowed() -> None:
    # 親子階層（small-to-big）の図の領域は engine_metadata_json にあり、保存する引用には残らない。
    chunk = _chunk(
        "c-s2b",
        content_kind="figure",
        engine_metadata_json=json.dumps(
            {"image_evidence": [{"page": 2, "bbox": [100, 200, 400, 500]}]}
        ),
        **_PAGE,
    )
    assert chunk.figure_image_available is True
    [stored] = stored_citations([chunk])
    assert "engine_metadata_json" not in stored["metadata"]
    assert stored["figure_image_available"] is True
    assert RetrievedChunk.model_validate(stored).figure_image_available is True
    # 保存した値が偽なら、読み直しても偽のまま。
    assert (
        RetrievedChunk.model_validate(
            {**stored, "figure_image_available": False}
        ).figure_image_available
        is False
    )


def test_citation_saved_before_the_flag_is_resolved_from_the_stored_metadata() -> None:
    legacy = _chunk("c-1", content_kind="figure", bbox="[60, 80, 300, 200]", **_PAGE).model_dump(
        mode="json"
    )
    del legacy["figure_image_available"]
    assert RetrievedChunk.model_validate(legacy).figure_image_available is True
    del legacy["metadata"]["bbox"]
    assert RetrievedChunk.model_validate(legacy).figure_image_available is False


def test_search_response_returns_the_flag_per_citation(monkeypatch: MonkeyPatch) -> None:
    async def fake_run(request: SearchRequest, **_: Any) -> SearchResponse:
        del request
        return SearchResponse(
            answer="",
            citations=[
                _chunk("c-figure", content_kind="figure", bbox="[60, 80, 300, 200]", **_PAGE),
                _chunk("c-legacy", content_kind="figure", page_start=2),
                _chunk("c-text", content_kind="text"),
            ],
            trace_id="trace-figure",
            elapsed_ms=1.0,
        )

    monkeypatch.setattr(search_route, "_run_search_with_timeout", fake_run)
    response = client.post("/api/search", json={"query": "合成の質問", "retrieval_only": True})

    assert response.status_code == 200, response.text
    citations = response.json()["data"]["citations"]
    assert {item["chunk_id"]: item["figure_image_available"] for item in citations} == {
        "c-figure": True,
        "c-legacy": False,
        "c-text": False,
    }
