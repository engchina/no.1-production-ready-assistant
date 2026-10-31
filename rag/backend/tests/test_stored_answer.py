"""回答の記録・会話の回答に保存する引用と評価の入力の形（#1371）。"""

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from rag_engine.evaluation.answer_eval import _evidence_fragments

from app.api.routes import chat as chat_route
from app.api.routes import search as search_route
from app.clients.oracle import StoredMessage
from app.rag.stored_answer import (
    ANSWER_RECORD_WARN_CHARS,
    STORED_CITATION_METADATA_KEYS,
    stored_citation,
    stored_citation_metadata,
    stored_citations,
    stored_evaluation_input,
    warn_if_large_answer_record,
)
from app.schemas.search import RetrievedChunk

# 画面の表示が同じことを frontend の src/lib/stored-citations.test.ts が同じ fixture で確かめる。
FIXTURE = Path(__file__).resolve().parents[2] / "frontend/src/lib/stored-citations.fixture.json"
INTERNAL_KEYS = ("parent_text", "engine_metadata_json", "engine_search_text")


def _cases() -> list[dict[str, Any]]:
    return list(json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"])


@pytest.mark.parametrize("case", _cases(), ids=lambda case: str(case["name"]))
def test_stored_citation_matches_frontend_fixture(case: dict[str, Any]) -> None:
    """fixture の保存の形は stored_citation の結果と同じ（変えたら fixture も作り直す）。"""
    stored = stored_citation(RetrievedChunk.model_validate(case["full"]))

    assert stored.model_dump(mode="json") == case["stored"]
    assert stored.metadata.keys() <= STORED_CITATION_METADATA_KEYS
    for key in (*INTERNAL_KEYS, "source_record_refs_json"):
        assert key not in stored.metadata
    # 本文・ID・score は検索の結果のまま。
    assert stored.text == case["full"]["text"]
    assert stored.chunk_id == case["full"]["chunk_id"]
    assert stored.score == case["full"]["score"]


def test_stored_citation_metadata_takes_display_regions_like_the_screen() -> None:
    """要素の表示領域は画面（displayRegionsFromMetadata）と同じ順で探す。"""
    regions = [{"page": 1, "boxes": [{"bbox": [1, 2, 3, 4], "text_preview": "見出し"}]}]
    engine = {"layout": {"display_regions": regions}, "image_evidence": [{"page": 1}]}

    from_string = stored_citation_metadata({"engine_metadata_json": json.dumps(engine)})
    from_object = stored_citation_metadata({"engine_metadata_json": engine})
    from_layout = stored_citation_metadata({"layout": {"display_regions": regions}})
    # layout に無ければ metadata.display_regions（画面の `??` と同じ）。
    fallback = stored_citation_metadata(
        {"layout": {"display_regions": None}, "display_regions": regions}
    )

    assert from_string == from_object == from_layout == fallback == {"display_regions": regions}
    # engine_metadata_json の layout が先（metadata.layout は見ない）。
    assert stored_citation_metadata(
        {"engine_metadata_json": engine, "layout": {"display_regions": []}}
    ) == {"display_regions": regions}
    # 読めない JSON・配列でない値は持たない（画面も強調しない）。
    assert stored_citation_metadata({"engine_metadata_json": "{"}) == {}
    assert stored_citation_metadata({"display_regions": {"page": 1}}) == {}


def test_stored_citation_metadata_keeps_how_the_answer_used_the_evidence() -> None:
    """使われ方（#2040・#2721）・親だけの参照の印と、出典行の確定・候補の番号（#2710）を保存する。"""
    metadata = {
        "evidence_model_used": False,
        "evidence_answer_use": "reference",
        "evidence_parent_used": True,
        "answer_citation_lines": [1, 3],
        "answer_citation_candidate_lines": [2, 4],
        "answer_citation_provenance": "child",
    }

    assert stored_citation_metadata(metadata) == metadata


def test_stored_citation_metadata_drops_null_and_unknown_keys() -> None:
    # 分割の版と統合の値は監査・調査のために残す（#1376）。
    assert stored_citation_metadata(
        {
            "page_start": 3,
            "section_title": None,
            "retrieval_mode": "hybrid",
            "rrf_score": 0.01,
            "chunk_set_id": "cs-1",
            "text_sha256": "x",
            "context_header": "a > b",
        }
    ) == {"page_start": 3, "rrf_score": 0.01, "chunk_set_id": "cs-1", "context_header": "a > b"}


def _evaluation_input() -> dict[str, Any]:
    parent_text = "第1条 申請\n申請は上長が承認する。\n第2条 期限\n期限は30日。"
    noise = {
        "source_record_refs": [{"record_id": "r-1", "page": 1, "seq_no": 1}] * 5,
        "display_regions": [{"page": 1, "boxes": [{"bbox": [1, 2, 3, 4]}]}],
        "citation": "doc:chunk-1 / p.1",
        "rerank_score": 0.9,
        "retrieval_role": "retrieved_anchor",
        "is_model_used": True,
    }
    return {
        "question": "申請の承認者は？",
        "answer_text": "上長です。",
        "reasoning_summary": "",
        "insufficient_reason": "",
        "cited_evidence": [],
        "external_data_required": False,
        "external_data_items": [],
        "evidence_items": [
            {
                **noise,
                "id": "doc:p1",
                "chunk_uid": "doc:p1",
                "chunk_id": "doc:p1",
                "record_id": "doc:p1",
                "source": "rules.pdf",
                "source_run_id": "0" * 16,
                "page_start": 1,
                "page_end": 1,
                "text": parent_text,
                "children": [
                    {
                        **noise,
                        "id": "doc:c1",
                        "chunk_uid": "doc:c1",
                        "source": "rules.pdf",
                        "source_run_id": "0" * 16,
                        "page_start": 1,
                        "page_end": 1,
                        "text": "申請は上長が承認する。",
                        "children": [],
                    },
                    {
                        **noise,
                        # chunk_uid と id が違うときは両方を残す。
                        "id": "c2",
                        "chunk_uid": "doc:c2",
                        "source": "rules.pdf",
                        "source_run_id": "0" * 16,
                        "page_start": 1,
                        "page_end": 1,
                        "text": "子だけの本文",
                        "children": [],
                    },
                ],
            }
        ],
    }


def test_stored_evaluation_input_keeps_only_what_the_evaluation_reads() -> None:
    full = _evaluation_input()

    compact = stored_evaluation_input(full)

    assert compact is not None
    parent = compact["evidence_items"][0]
    assert parent == {
        "id": "doc:p1",
        "source": "rules.pdf",
        "source_run_id": "0" * 16,
        "page_start": 1,
        "page_end": 1,
        "text": full["evidence_items"][0]["text"],
        "children": [
            {
                "id": "doc:c1",
                "source": "rules.pdf",
                "source_run_id": "0" * 16,
                "page_start": 1,
                "page_end": 1,
                "text": "申請は上長が承認する。",
            },
            {
                "id": "c2",
                "chunk_uid": "doc:c2",
                "source": "rules.pdf",
                "source_run_id": "0" * 16,
                "page_start": 1,
                "page_end": 1,
                "text": "子だけの本文",
            },
        ],
    }
    # 根拠以外の項目はそのまま。元の入力は変えない。
    assert {key: compact[key] for key in compact if key != "evidence_items"} == {
        key: full[key] for key in full if key != "evidence_items"
    }
    assert "source_record_refs" in full["evidence_items"][0]


def test_stored_evaluation_input_gives_the_same_evidence_to_the_evaluation() -> None:
    """評価（evaluate_answer_payload）がモデルに渡す根拠の断片は、削る前と同じ。"""
    full = _evaluation_input()
    compact = stored_evaluation_input(full)
    assert compact is not None

    assert list(_evidence_fragments(compact["evidence_items"])) == list(
        _evidence_fragments(full["evidence_items"])
    )


def test_stored_evaluation_input_passes_through_missing_values() -> None:
    assert stored_evaluation_input(None) is None
    assert stored_evaluation_input({"question": "q"}) == {"question": "q"}
    assert stored_evaluation_input({"evidence_items": None}) == {"evidence_items": None}


def test_large_answer_record_is_logged_without_truncation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    citations = stored_citations(
        [
            RetrievedChunk(
                document_id="d", chunk_id="c", text="x" * ANSWER_RECORD_WARN_CHARS, score=1
            )
        ]
    )
    with caplog.at_level(logging.WARNING, logger="app.rag.stored_answer"):
        warn_if_large_answer_record(trace_id="t-small", citations=[], evaluation_input=None)
        warn_if_large_answer_record(trace_id="t-large", citations=citations, evaluation_input=None)

    records = [record for record in caplog.records if record.name == "app.rag.stored_answer"]
    assert [getattr(record, "trace_id", None) for record in records] == ["t-large"]
    assert getattr(records[0], "citations_chars", 0) > ANSWER_RECORD_WARN_CHARS
    assert citations[0]["text"] == "x" * ANSWER_RECORD_WARN_CHARS


def test_saved_answer_detail_returns_stored_shape_for_older_records() -> None:
    """以前に保存した記録（metadata をすべて持つ）も、保存の形で返す。"""
    case = _cases()[0]

    detail = search_route._answer_record_detail(
        {
            "trace_id": "trace-old",
            "search_answer_profile_id": None,
            "surface": "search",
            "answer_engine": "grounded",
            "question": "q",
            "rewritten_question": None,
            "answer": "a",
            "created_at": datetime(2026, 10, 9, tzinfo=UTC),
            "citations_json": [case["full"]],
            "diagnostics_json": {},
            "evaluation_input_json": None,
        }
    )

    assert [citation.model_dump(mode="json") for citation in detail.citations] == [case["stored"]]


def test_restored_chat_message_returns_stored_shape() -> None:
    case = _cases()[0]
    message = StoredMessage(
        id="m-1",
        conversation_id="c-1",
        role="ASSISTANT",
        content="回答",
        citations=[case["full"]],
        status="COMPLETE",
        created_at=datetime(2026, 10, 9, tzinfo=UTC),
    )

    restored = chat_route._to_chat_message(message)

    assert [citation.model_dump(mode="json") for citation in restored.citations] == [case["stored"]]


@pytest.mark.parametrize("enabled", [True, False])
def test_stored_header_survives_json_round_trip_without_original_chunk(enabled: bool) -> None:
    metadata = {"context_header_enabled": enabled, "section_path": "分類 A > 機能 A > 説明"}
    if enabled:
        metadata["context_header"] = "文書 A > 分類 A > 機能 A > 説明"
    stored = stored_citation_metadata(metadata)
    assert json.loads(json.dumps(stored, ensure_ascii=False)) == metadata
    assert stored_citation_metadata({}) == {}


@pytest.mark.parametrize("legacy", [False, True])
def test_item_citation_ordinals_and_body_survive_saved_message_round_trip(legacy: bool) -> None:
    body = "資料により確認できた事項\n\n・合成の説明 A。\n"
    if not legacy:
        body += "根拠：document-a.pdf p.1\n"
    body += "・合成の説明 B。\n根拠：document-a.pdf p.1"
    chunks = [
        RetrievedChunk(
            document_id="document-a",
            chunk_id=f"chunk-{index}",
            file_name="document-a.pdf",
            text=f"合成の原文 {index}。",
            score=0.8,
            metadata={
                "page_start": 1,
                "answer_citation_lines": [1 if legacy else index + 1],
                "answer_citation_candidate_lines": [3],
                "answer_citation_provenance": "child",
            },
        )
        for index in range(2)
    ]
    saved = json.loads(json.dumps(stored_citations(chunks)))
    message = StoredMessage(
        id="message-a",
        conversation_id="conversation-a",
        role="ASSISTANT",
        content=body,
        citations=saved,
        status="COMPLETE",
        created_at=datetime(2026, 10, 9, tzinfo=UTC),
    )
    restored = chat_route._to_chat_message(message)
    assert restored.content == body
    assert [chunk.metadata["answer_citation_lines"] for chunk in restored.citations] == (
        [[1], [1]] if legacy else [[1], [2]]
    )
    assert all(
        chunk.metadata["answer_citation_candidate_lines"] == [3] for chunk in restored.citations
    )
    assert all(
        chunk.metadata["answer_citation_provenance"] == "child" for chunk in restored.citations
    )
