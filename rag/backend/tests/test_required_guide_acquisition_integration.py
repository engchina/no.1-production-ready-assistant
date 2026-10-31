"""分岐・設定・取得・生成入力・保存本文をつなぐ合成回帰。外部 I/O はスタブ。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any, cast

import pytest
from pydantic import BaseModel

from app.config import Settings
from app.rag.answer_engine import AnswerEngine
from app.rag.pipeline import RagPipeline
from app.rag.support_guide_runtime import guide_step_plan, match_guide
from app.schemas.search import RetrievedChunk, SearchMode, SearchRequest
from app.schemas.support_guide import SupportGuideContent
from tests.test_answer_engine import FakeGenAi, FakeOracle, _doc_chunk

BODY = "合成パネルを開き、保存ボタンを押します。"


class AcquisitionOracle(FakeOracle):
    def __init__(self) -> None:
        super().__init__()
        self.chunks = [_doc_chunk("base", "共通の説明です。")]
        self.required_filters: list[dict[str, str]] = []
        self.saved: list[Mapping[str, object]] = []

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        scope = dict(filters or {})
        if "page_ranges" not in scope:
            return self.chunks
        self.required_filters.append(scope)
        targets = json.loads(scope["page_ranges"])
        assert len(targets) == 1 and targets[0]["document_id"] == "doc-a"
        return [_doc_chunk("doc-a", BODY)]

    async def context_group_siblings(
        self,
        anchors: list[RetrievedChunk],
        *,
        max_chunks_per_group: int,
        neighbor_child_count: int | None = None,
    ) -> list[RetrievedChunk]:
        return []

    async def save_answer_record(self, record: Mapping[str, object]) -> None:
        self.saved.append(dict(record))


async def test_branch_required_acquisition_reaches_none_generation_and_saved_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rag_engine.adapters.oci as engine_oci

    content = SupportGuideContent.model_validate(
        {
            "title": "合成設定ガイド",
            "goal": {"expected_result": "合成設定を変更する", "match_terms": ["合成設定"]},
            "conditions": [
                {
                    "id": "kind",
                    "label": "種類",
                    "allowed_values": ["甲", "乙"],
                    "question": "種類は？",
                }
            ],
            "steps": [
                {
                    "id": "common",
                    "title": "設定を変更する",
                    "references": [
                        {"document_id": name, "page_ranges": [{"page_start": 1, "page_end": 1}]}
                        for name in ("doc-a", "doc-b")
                    ],
                },
                {
                    "id": "other",
                    "title": "別の分岐の操作",
                    "references": [
                        {"document_id": "doc-other", "page_ranges": [{"page_start": 1}]}
                    ],
                },
            ],
            "branches": [
                {
                    "id": "branch-other",
                    "when": {"condition_id": "kind", "values": ["乙"]},
                    "goto_step": "other",
                }
            ],
        }
    )
    match = match_guide([("synthetic-guide", 2, content)], "合成設定の方法は？", {"kind": "甲"})
    assert match is not None
    compiled = guide_step_plan(match)
    assert [step["id"] for step in compiled] == ["common"]
    guide = {**match.summary(), "step_plan": compiled}
    calls: list[tuple[str, str]] = []

    def model(
        system: str, prompt: str, settings: Any, schema: type[BaseModel], **options: Any
    ) -> Any:
        calls.append((schema.__name__, prompt))
        assert schema.__name__ == "GroundedDraftWire", "生成以外のモデル呼び出しは禁止"
        evidence_id = re.findall(r"\[(E[0-9]+)\]", prompt[: prompt.index(BODY)])[-1]
        # 実際の生成wireを検証する。旧canonical quote fixtureとしては返さない。
        return schema.model_validate(
            {
                "request_coverage": [{"request_id": "Q1", "evidence_ids": [evidence_id]}],
                "items": [
                    {
                        "request_id": "Q1",
                        "kind": "operation",
                        "text": BODY,
                        "citations": [{"evidence_id": evidence_id, "line_start": 1, "line_end": 1}],
                    }
                ],
                "summary": "",
                "confidence": "high",
            }
        )

    monkeypatch.setattr(engine_oci, "parse_text_response", model)
    settings = Settings(
        _env_file=None,
        rag_query_strategy="simple_retrieval",
        rag_answer_flow="standard_rag",
        rag_answer_audit_mode="none",
        rag_answer_max_correction_rounds=0,
        rag_section_expansion_enabled=False,
        rag_rerank_enabled=False,
        rag_support_guide_enabled=True,
        rag_support_guide=guide,
        rag_extra_retrieval_max_queries=1,
        rag_guide_step_max_queries=0,
    )
    oracle, genai = AcquisitionOracle(), FakeGenAi()
    request = SearchRequest(
        query="合成設定の方法は？", filters={"knowledge_base_id": "synthetic-kb"}
    )
    outcome = await AnswerEngine(settings, oracle=cast(Any, oracle), genai=cast(Any, genai)).run(
        request
    )
    # 1つの論理取得は既存の2検索文へ展開される。SDK/SQL回数を論理枠と混同しない。
    assert len(oracle.required_filters) == 2
    assert oracle.required_filters[0]["knowledge_base_id"] == "synthetic-kb"
    acquisition = outcome.diagnostics["guide_step_retrieval"]["required_acquisition"]
    assert acquisition["planned_targets"] == 2 and acquisition["logical_attempts"] == 1
    rows = acquisition["targets"]
    assert rows[0]["retained_body_chars"] > 0
    assert rows[1]["execution"] == "not_run" and rows[1]["reason"] == "required_budget_exhausted"
    assert acquisition["incomplete_step_ids"] == ["common"]
    assert len(calls) == 1
    assert "guide_acquisition:" in calls[0][1] and "incomplete_ref_count" in calls[0][1]
    assert "required_budget_exhausted" in calls[0][1]
    assert BODY in outcome.answer
    assert "取得または回答材料への保持を確認できていません" in outcome.answer
    assert outcome.diagnostics["answer_validation"]["status"] == "not_run"
    assert outcome.diagnostics["answer_validation"]["reason"] == "audit_disabled"
    assert all(row["status"] == "unknown" for row in outcome.diagnostics["guide_step_coverage"])
    review = outcome.diagnostics["generation_review"]
    assert review["execution"] == "not_run"
    assert len(review["items"]) == 1
    # 原文の位置は確定するが、none の主張の意味支持を確認済みにしない。
    assert review["items"][0]["citation_resolution"]["authenticity"] == "verified"
    assert review["items"][0]["quote_match"] == "exact"

    captures = outcome.diagnostics["generation_citation_traces"]
    assert len(captures) == 1
    capture = captures[0]
    assert capture["document_scope_status"] == "complete"
    assert set(capture["document_ids"]) == {"base", "doc-a"}
    snapshot = capture["input"]
    assert snapshot["snapshot_status"] == "recorded"
    assert snapshot["prompt"] == calls[0][1]
    assert snapshot["prompt_sha256"] == hashlib.sha256(calls[0][1].encode()).hexdigest()
    assert snapshot["transport_capture"] == "not_recorded"
    views = snapshot["evidence"]
    assert len({view["display_id"] for view in views}) == len(views)
    assert len({view["evidence_id"] for view in views}) == len(views)
    operation = next(view for view in views if BODY in view["text"])
    assert operation["display_id"].startswith("E")
    assert operation["numbered"] is True
    assert operation["display_id"] != operation["evidence_id"]
    assert operation["source_id"] in rows[0]["retained_source_ids"]
    assert operation["provenance"]["document_scope"][-1] == "doc-a"
    assert any(BODY in operation["text"][start:end] for start, end in operation["citation_units"])
    assert all(view["present_in_prompt"] and view["prompt_block"] in calls[0][1] for view in views)
    outputs = capture["outputs"]
    assert len(outputs) == 1 and outputs[0]["input_prompt"] == calls[0][1]
    assert outputs[0]["raw_output_status"] == "not_recorded"
    assert outputs[0]["provider_output_text"] is None
    assert outputs[0]["parsed_output_representation"] == "generation_wire"
    citation = outputs[0]["parsed_output"]["items"][0]["citations"][0]
    assert citation == {
        "evidence_id": operation["display_id"],
        "line_start": 1,
        "line_end": 1,
    }

    # 保存の正規化も実処理を通し、DB の書き込みだけをメモリに置き換える。
    pipeline = RagPipeline(
        settings=settings, oracle=cast(Any, oracle), genai=cast(Any, genai), llm=cast(Any, object())
    )
    await pipeline._save_answer_record(
        trace_id="synthetic-required-acquisition",
        request=request,
        question=request.query,
        rewritten_question=request.query,
        answer=outcome.answer,
        citations=outcome.citations,
        diagnostics=cast(Any, outcome.diagnostics),
        surface="chat",
    )
    assert len(oracle.saved) == 1 and oracle.saved[0]["answer"] == outcome.answer
    stored = cast(dict[str, Any], oracle.saved[0]["diagnostics"])
    assert stored["answer_validation"]["reason"] == "audit_disabled"
    assert stored["generation_citation_traces"] == captures
    assert stored["guide_step_retrieval"]["required_acquisition"]["incomplete_step_ids"] == [
        "common"
    ]
