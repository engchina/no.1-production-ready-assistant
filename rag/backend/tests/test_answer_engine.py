"""回答エンジン(rag_poc 回答フロー + backend 検索)の統合テスト(外部 I/O はスタブ)。"""

import json
import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, cast

import pytest
from rag_engine.generation.citation_wire import generation_draft_wire_model
from rag_engine.models.llm import (
    CragRetrievalGradeOutput,
    GroundedDraft,
    QueryRoutingOutput,
    RequestScopePlan,
)

import app.rag.answer_engine as answer_engine_module
from app.clients.section_map_store import SECTION_CHUNKS_TABLE
from app.config import EnterpriseAiConfiguredModel, Settings
from app.rag.answer_engine import AnswerEngine
from app.rag.stored_answer import STORED_CITATION_METADATA_KEYS
from app.schemas.search import RetrievedChunk, SearchMode, SearchRequest
from tests.audit_fixtures import is_audit_schema, legacy_audit_result

PARENT_TEXT = "受注入力画面\n受注番号を入力し、登録ボタンを押します。"


def _chunk(chunk_id: str, text: str, seq: int) -> RetrievedChunk:
    return RetrievedChunk(
        document_id="doc-1",
        chunk_id=chunk_id,
        text=text,
        score=0.9,
        file_name="manual.pdf",
        metadata={
            "chunk_group_id": "chunk-docling-p000001",
            "parent_text": PARENT_TEXT,
            "chunk_strategy": "small_to_big",
            "engine_search_text": f"Source file: manual.pdf\nChild text: {text}",
            "engine_chunk_seq": seq,
            "page_start": 1,
            "page_end": 1,
            "engine_metadata_json": json.dumps(
                {
                    "schema_version": 4,
                    "active": True,
                    "atomic": False,
                    "content_hash": f"h{seq}",
                    "classification": {},
                    "source_categories": ["Text"],
                    "section_path": ["受注入力画面"],
                    "section_path_sources": ["section_header"],
                }
            ),
            "source_record_refs_json": json.dumps(
                [{"record_id": f"docling-p1-{seq}", "page": 1, "seq_no": seq}]
            ),
        },
    )


class FakeOracle:
    def __init__(self) -> None:
        self.filters: list[dict[str, str]] = []
        self.chunks = [
            _chunk("doc-1:c1", "受注番号を入力し、登録ボタンを押します。", 1),
            _chunk("doc-1:c2", "登録後は受注一覧に表示されます。", 2),
        ]
        self.classifications: dict[str, dict[str, object]] = {}
        self.classification_calls: list[list[str]] = []
        self.first_page_contexts: dict[str, dict[str, object]] = {}
        self.first_page_calls: list[list[str]] = []
        self.probes: list[dict[str, str]] = []
        # 検索範囲の文書に保存済みの大分類(#553)。
        self.large_categories: list[str] = []
        self.large_category_calls: list[dict[str, str]] = []
        # 画面で編集した回答生成のプロンプト({key: content})。既定は未編集。
        self.prompt_overrides: dict[str, str] = {}

    async def answer_prompt_overrides(self) -> dict[str, str]:
        return dict(self.prompt_overrides)

    # 章節の拡張(#1436)が読む章節と chunk の対応(#1438)。この代役の文書は対応の行を持たず、
    # chunk を ID では読まない(`retrievable_chunk` は None)ので、対応の作り直しもせず、章節で
    # 広げずに回答を続ける。章節の拡張を確かめる試験は `tests/test_section_expansion_answer.py`。
    async def _fetch_one(
        self, statement: str, binds: Mapping[str, object] | None = None
    ) -> dict[str, object] | None:
        if SECTION_CHUNKS_TABLE in statement:
            return None
        raise AssertionError("FakeOracle は章節の対応のほかの SQL を読まない")

    async def retrievable_chunk(self, document_id: str, chunk_id: str) -> RetrievedChunk | None:
        return None

    async def retrieval_large_categories(self, filters: dict[str, str]) -> list[str]:
        self.large_category_calls.append(dict(filters))
        return list(self.large_categories)

    async def has_retrieval_chunks(self, filters: dict[str, str]) -> bool:
        self.probes.append(dict(filters))
        name = filters.get("file_name", "").casefold()
        return any(name in (chunk.file_name or "").casefold() for chunk in self.chunks)

    async def document_classifications(
        self, document_ids: list[str]
    ) -> dict[str, dict[str, object]]:
        self.classification_calls.append(list(document_ids))
        return {
            document_id: self.classifications[document_id]
            for document_id in document_ids
            if document_id in self.classifications
        }

    async def chunk_set_first_page_contexts(
        self, chunk_set_ids: list[str]
    ) -> dict[str, dict[str, object]]:
        self.first_page_calls.append(list(chunk_set_ids))
        return {
            chunk_set_id: self.first_page_contexts[chunk_set_id]
            for chunk_set_id in chunk_set_ids
            if chunk_set_id in self.first_page_contexts
        }

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        self.filters.append(dict(filters or {}))
        return self.chunks[:1]

    async def context_group_siblings(
        self,
        anchors: list[RetrievedChunk],
        *,
        max_chunks_per_group: int,
        neighbor_child_count: int | None = None,
    ) -> list[RetrievedChunk]:
        return self.chunks[1:]


class FakeGenAi:
    async def embed(
        self, texts: list[str], *, input_type: str = "SEARCH_DOCUMENT"
    ) -> list[list[float]]:
        return [[0.1] * 4 for _ in texts]

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        return [(index, 1.0 - index * 0.1) for index in range(len(documents))][:top_n]


def _fake_llm(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
    if schema is RequestScopePlan:
        data = json.loads(prompt)
        ids = [item["evidence_id"] for item in data["evidence"]]
        return RequestScopePlan.model_validate(
            {
                "request_coverage": [
                    {"request_id": unit["id"], "evidence_ids": ids}
                    for unit in data["question_contract"]["request_units"]
                    if unit.get("kind") != "context"
                ]
            }
        )
    if schema is QueryRoutingOutput:
        return QueryRoutingOutput(
            strategy="simple_retrieval", reason="単純な手順の質問", queries=[]
        )
    if schema is CragRetrievalGradeOutput:
        candidates = json.loads(
            prompt.split("BEGIN_UNTRUSTED_CRAG_CANDIDATES\n", 1)[1].split(
                "END_UNTRUSTED_CRAG_CANDIDATES", 1
            )[0]
        )
        body = "受注番号を入力し、登録ボタンを押します。"
        candidate = next(row for row in candidates if body in row["text"])
        source_id = candidate["chunk_uid"]
        evidence = [{"source_id": source_id, "quote": body}]
        return CragRetrievalGradeOutput(
            reason="登録操作の直接原文がある",
            sufficient=True,
            confidence=0.9,
            rewritten_query="",
            candidate_verdicts=[
                dict(chunk_uid=source_id, relevant=True, reason="登録操作を説明する")
            ],
            aspect_checks=[
                dict(
                    aspect=aspect,
                    status="supported",
                    source_ids=[source_id],
                    evidence=evidence,
                    reason="候補原文を確認",
                )
                for aspect in ["business_object", "requested_result", "applicability", "procedure"]
            ],
            answerable_parts=[
                dict(aspect="procedure", kind="operation", claim=body, evidence=evidence)
            ],
        )
    if schema in (GroundedDraft, generation_draft_wire_model()):
        # 既存quote fixtureを履歴互換の草稿として返す。生成wireの検証は行わない。
        body = "受注番号を入力し、登録ボタンを押します。"
        match = re.search(r"\[(E[0-9]+)\].*?" + re.escape(body), prompt, re.DOTALL)
        items = (
            [
                {
                    "kind": "rule",
                    "text": body,
                    "citations": [{"evidence_id": match.group(1), "quote": body}],
                }
            ]
            if match
            else []
        )
        return GroundedDraft.model_validate(
            {"summary": "手順", "items": items, "confidence": "high"}
        )
    # 通常の回答の監査（#2795）は事実の監査の schema で呼ぶ。
    if is_audit_schema(schema):
        return legacy_audit_result(
            schema,
            {
                "goal_alignment": "aligned",
                "summary_supported": True,
                "reviews": [
                    {
                        "index": 0,
                        "support": "supported",
                        "applicability": "matched",
                        "reason": "原文",
                    }
                ],
            },
        )
    raise AssertionError(f"unexpected schema: {schema.__name__}")


@pytest.mark.parametrize("custom_format", [False, True])
async def test_answer_engine_answers_with_backend_search_and_evidence(
    monkeypatch: pytest.MonkeyPatch,
    custom_format: bool,
) -> None:
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    oracle = FakeOracle()
    engine = AnswerEngine(
        Settings(rag_domain_keywords=["受注番号"]),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )
    request = SearchRequest(query="受注の登録方法は？", filters={"knowledge_base_id": "kb-1"})

    from rag_parser_core.language import bind_pack, current_pack, resolve

    pack = (
        resolve(
            current_pack(),
            {"answer_labels": {"rules_section": "確認結果[例]", "citation_prefix": "出典[例]："}},
        )
        if custom_format
        else current_pack()
    )
    with bind_pack(pack):
        outcome = await engine.run(request)
    assert outcome.diagnostics["answer_format"]["rules_section"] == pack.text(
        "answer_labels", "rules_section"
    )
    assert outcome.diagnostics["answer_format"]["citation_prefix"] == pack.text(
        "answer_labels", "citation_prefix"
    )
    assert pack.text("answer_labels", "citation_prefix") in outcome.answer

    assert "登録ボタン" in outcome.answer
    assert oracle.filters and oracle.filters[0]["knowledge_base_id"] == "kb-1"
    assert outcome.citations
    assert outcome.citations[0].chunk_id == "doc-1:c1"
    assert "evidence_role" in outcome.citations[0].metadata
    # 検索で当たった子は関連度の順位を持ち、前後の文脈は持たない（MCP の根拠の並び。#1348）。
    for citation in outcome.citations:
        rank = citation.metadata.get("evidence_retrieval_rank")
        if citation.metadata["evidence_role"] == "retrieved_anchor":
            assert isinstance(rank, int) and rank >= 1
        else:
            assert rank is None
    assert any(c.metadata["evidence_role"] == "retrieved_anchor" for c in outcome.citations)
    diagnostics = outcome.diagnostics
    # このスタブは rule だけを返すため、操作の引用の各件数は 0。
    assert diagnostics["procedure_quotes"] == {
        "original": 0,
        "paraphrase_verified": 0,
        "unmatched": 0,
    }
    # rag_poc の回答 viewer と同じく外部データの確認・問い合わせ型も診断に載せる(#651)。
    assert diagnostics["external_data_items"] == []
    assert diagnostics["question_type"] == []
    assert "external_data_required" in diagnostics
    # 採用した草稿の構造（確認の項目の事実と規則の分け方。#1492）を replay の材料に残す。
    draft = diagnostics["grounded_draft"]
    assert draft["confirmation_items"] == []
    assert draft["settled_confirmations"] == {
        "user": [],
        "system": [],
        "document": [],
        "excluded": [],
    }
    assert [item["kind"] for item in draft["items"]]
    assert diagnostics["execution_steps"]
    assert diagnostics["evidence_tree"]
    assert diagnostics["evidence_tree"][0]["children"]


async def test_answer_diagnostics_keep_llm_usage_and_round_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工程ごとの LLM の token の数と、是正の round の時間の配分を診断に残す(#1516)。"""
    import rag_engine.adapters.model_api as engine_api
    from rag_engine.resources.llm_usage import record_llm_usage

    def fake_with_usage(system: str, prompt: str, settings: Any, schema: Any, **_: Any) -> Any:
        # 本番の adapter（`_record_usage`）と同じく、1 回の呼び出しの usage を累積に足す。
        record_llm_usage(
            {"input_tokens": 1000, "output_tokens": 200, "reasoning_tokens": 150, "seconds": 2.5}
        )
        return _fake_llm(system, prompt, settings, schema)

    monkeypatch.setattr(engine_api, "parse_text_response", fake_with_usage)
    engine = AnswerEngine(
        Settings(rag_domain_keywords=["受注番号"], rag_answer_audit_mode="none"),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )

    outcome = await engine.run(
        SearchRequest(query="受注の登録方法は？", filters={"knowledge_base_id": "kb-1"})
    )

    steps = outcome.diagnostics["execution_steps"]
    generation = next(step for step in steps if step["name"].startswith("回答文の生成と根拠確認"))
    assert generation["llm_usage"]["input_tokens"] == 1000 * generation["llm_calls"]
    assert generation["llm_usage"]["reasoning_tokens"] == 150 * generation["llm_calls"]
    # LLM を呼ばない工程には載せない。
    assert all("llm_usage" not in step for step in steps if not step.get("llm_calls"))
    budget = outcome.diagnostics["round_budget"]
    assert len(budget["round_seconds"]) >= 1
    assert budget["reserve_seconds"] == 30.0
    assert "stop" not in budget


async def test_answer_diagnostics_keep_the_evidence_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全体設定の根拠の文字予算が回答の経路まで届き、使った予算と送った文字数を診断に残す(#3776)。"""
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    engine = AnswerEngine(
        Settings(rag_domain_keywords=["受注番号"], rag_evidence_budget_chars=64000),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )

    outcome = await engine.run(
        SearchRequest(query="受注の登録方法は？", filters={"knowledge_base_id": "kb-1"})
    )

    budget = outcome.diagnostics["evidence_budget"]
    assert budget["configured_chars"] == 64000
    assert budget["effective_chars"] >= 1
    assert budget["source"] in {"configured", "section_expansion"}
    assert budget["evidence_block_chars"] >= budget["evidence_text_chars"] > 0
    assert set(budget) == {
        "configured_chars",
        "effective_chars",
        "source",
        "effective_exceeds_configured",
        "span_count",
        "budget_counts",
        "evidence_text_chars",
        "evidence_metadata_chars",
        "evidence_format_chars",
        "evidence_block_chars",
        "evidence_block_tokens_estimate",
    }


async def test_execution_steps_carry_structured_details(monkeypatch: pytest.MonkeyPatch) -> None:
    """回答の記録の工程は、共有の詳細の形（見出しと値・行）の詳細を持つ（#2341）。"""
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    engine = AnswerEngine(
        Settings(rag_domain_keywords=["受注番号"]),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )

    outcome = await engine.run(
        SearchRequest(query="受注の登録方法は？", filters={"knowledge_base_id": "kb-1"})
    )

    steps = outcome.diagnostics["execution_steps"]
    assert steps
    for step in steps:
        details = step.get("details")
        assert isinstance(details, dict), step["name"]
        assert details.get("fields") or details.get("lines"), step["name"]
    search = next(step for step in steps if step["name"].startswith("文書検索"))
    labels = {field["label"] for field in search["details"]["fields"]}
    assert {"目的", "検索の候補", "結果"} <= labels


@pytest.mark.usefixtures("trace_body_enabled")
async def test_pipeline_delegates_to_answer_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    import rag_engine.adapters.model_api as engine_api

    from app.rag.pipeline import RagPipeline

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    pipeline = RagPipeline(
        settings=Settings(rag_answer_audit_mode="none"),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )

    from pr_backend_core.chat_progress import fold_chat_progress_events

    from tests.span_progress_support import RecordingTrace

    observed: list[tuple[str, str]] = []

    async def capture(progress: Any) -> None:
        observed.append((progress.stage, progress.outcome))

    tracker = RecordingTrace()
    response = await pipeline.run(
        SearchRequest(query="受注の登録方法は？"),
        progress_callback=capture,
        trace=tracker,
    )
    # 処理の経過は回答の trace の span（#3226）。常に通る安全チェックの段は無い（#37）。
    steps = fold_chat_progress_events(tracker.events).step_list()
    assert not any(step.kind == "answer_guardrail" for step in steps)
    # 回答フローの工程は種類 answer_step と工程名の span で、回答の段（rag.answer）の中に
    # 入れ子にする。
    answer_span = next(
        step
        for step in steps
        if (step.span or {}).get("name") == "rag.answer" and not (step.span or {}).get("root")
    )
    engine_steps = [step for step in steps if step.kind == "answer_step"]
    assert engine_steps
    assert all(step.status == "done" for step in engine_steps)
    understand = next(s for s in engine_steps if (s.params or {}).get("name") == "質問の理解")
    assert (understand.span or {}).get("parent_id") == answer_span.step_id
    # 用語・同義語と回答ルールは別の工程の span で、回答の段の子としてこの順に並ぶ（#3451）。
    engine_names = [str((s.params or {}).get("name")) for s in engine_steps]
    terms_index = engine_names.index("用語・同義語の確認")
    assert engine_names[terms_index + 1] == "回答ルールの確認"
    assert "用語・ルールの確認" not in engine_names
    # 検索の前提の確認は工程にしない（#3495。backend は確認を注入しない）。
    assert "検索の準備確認" not in engine_names
    for step in engine_steps[terms_index : terms_index + 2]:
        assert (step.span or {}).get("parent_id") == answer_span.step_id
        assert step.details is not None and "結果" in {field.label for field in step.details.fields}
    # 工程の詳細（目的・結果など）は終わった工程の span に付く。
    search = next(
        s for s in engine_steps if str((s.params or {}).get("name")).startswith("文書検索")
    )
    assert search.details is not None
    assert {"目的", "結果"} <= {field.label for field in search.details.fields}
    # 回答の記録に残す処理の経過は、表を引く trace id と工程の詳細（表には入れない）だけ（#3226）。
    record = tracker.record()
    assert record["trace_id"] == tracker.updates[0].trace_id
    assert search.step_id in record["span_details"]

    assert "登録ボタン" in response.answer
    # 最後は回答の記録の保存の工程（#2342。この FakeOracle は保存できないので失敗、回答は返す）。
    assert observed[-2:] == [("answer_record_save", "started"), ("answer_record_save", "error")]
    assert [item.step for item in response.storage_failures] == ["answer_record_save"]
    observed = observed[:-2]
    # 回答エンジン（検索と LLM の回答生成）も進捗の工程として通知する（#375）。
    assert observed[0] == ("answer", "started")
    # 回答の後は回答の記録の保存だけで、回答側の安全チェックの工程は無い（#37）。
    assert observed[-1] == ("answer", "success")
    # 回答フローの中の各工程は、入れ子の工程として開始と終了を通知する（#593）。
    inner = observed[1:-1]
    assert inner
    assert all(stage.startswith("answer_step:") for stage, _ in inner)
    assert ("answer_step:質問の理解", "started") in inner
    assert ("answer_step:質問の理解", "success") in inner
    started = [stage for stage, outcome in inner if outcome == "started"]
    finished = [stage for stage, outcome in inner if outcome != "started"]
    assert sorted(started) == sorted(finished)
    assert response.citations[0].chunk_id == "doc-1:c1"
    assert response.diagnostics.retrieval_strategy == "hybrid"
    assert response.diagnostics.answer is not None
    assert response.diagnostics.answer["evidence_tree"]
    # 回答の経路（固定の RAG）と理由を記録する。答えられた回答は現場のデータを求めない（#1283）。
    route = cast(dict[str, Any], response.diagnostics.answer["route"])
    assert route["path"] == "rag"
    assert route["reason"] == response.diagnostics.answer["outcome"]
    assert route["requires_environment_data"] is False


async def test_answer_engine_pipeline_records_search_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    import rag_engine.adapters.model_api as engine_api

    import app.rag.pipeline as pipeline_module

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(
        pipeline_module, "record_rag_search_audit", lambda **kwargs: audits.append(kwargs)
    )
    pipeline = pipeline_module.RagPipeline(
        settings=Settings(),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )

    response = await pipeline.run(SearchRequest(query="受注の登録方法は？"))

    assert len(audits) == 1
    assert audits[0]["outcome"] == "success"
    assert audits[0]["citations"] == response.citations
    assert audits[0]["diagnostics"].answer is not None
    assert audits[0]["diagnostics"].retrieval_strategy_adapter == "grounded"


def test_search_answer_profile_ignores_removed_text_search_tokenizer_override() -> None:
    """全文検索の分割方式の上書きは #588 で削除した。保存済みの値は読み捨てる。"""
    from app.rag.kb_adapter_config import KnowledgeBaseQueryConfig
    from app.rag.search_answer_profile_config import (
        SearchAnswerProfileConfig,
        resolve_search_answer_profile_settings,
    )

    query = KnowledgeBaseQueryConfig.model_validate({"text_search_tokenizer": "sudachi"})
    config = SearchAnswerProfileConfig(knowledge_base_ids=["kb-1"], query=query)

    settings, _ = resolve_search_answer_profile_settings(Settings(), config)

    assert "text_search_tokenizer" not in query.model_dump()
    assert not hasattr(settings, "rag_text_search_tokenizer")


def test_search_answer_profile_ignores_global_answer_options() -> None:
    from app.rag.kb_adapter_config import KnowledgeBaseQueryConfig
    from app.rag.search_answer_profile_config import (
        SearchAnswerProfileConfig,
        resolve_search_answer_profile_settings,
    )

    config = SearchAnswerProfileConfig(
        knowledge_base_ids=["kb-1"],
        query=KnowledgeBaseQueryConfig(
            query_strategy="hyde",
            answer_flow="standard_rag",
            neighbor_child_count=0,
            rerank_enabled=False,
        ),
    )

    base = Settings(
        rag_query_strategy="rag_fusion",
        rag_answer_flow="crag",
        rag_neighbor_child_count=3,
        rag_rerank_enabled=True,
    )
    settings, _ = resolve_search_answer_profile_settings(base, config)

    assert settings.rag_query_strategy == "rag_fusion"
    assert settings.rag_answer_flow == "crag"
    assert settings.rag_neighbor_child_count == 3
    assert settings.rag_rerank_enabled is True


@pytest.mark.parametrize(
    ("settings", "expected"),
    [
        # 全体の既定は単純検索と標準 RAG(#2747)。
        (Settings(), ("simple_retrieval", "standard_rag", 3, True)),
        (
            Settings(
                rag_query_strategy="rag_fusion",
                rag_answer_flow="standard_rag",
                rag_neighbor_child_count=7,
                rag_rerank_enabled=False,
            ),
            ("rag_fusion", "standard_rag", 7, False),
        ),
    ],
)
async def test_answer_engine_passes_answer_options(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, expected: tuple[object, ...]
) -> None:
    import rag_engine.generation.answering as answering

    captured: dict[str, Any] = {}

    def fake_answer(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(answering, "answer_question_result", fake_answer)
    engine = AnswerEngine(
        settings,
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="stop"):
        await engine.run(SearchRequest(query="受注の登録方法は？"))

    assert (
        captured["query_strategy"],
        captured["answer_flow"],
        captured["chunk_neighbor_count"],
        captured["rerank_enabled"],
    ) == expected


async def test_answer_engine_passes_classification_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    import rag_engine.generation.answering as answering

    captured: dict[str, Any] = {}

    def fake_answer(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(answering, "answer_question_result", fake_answer)
    engine = AnswerEngine(
        Settings(),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )
    request = SearchRequest(
        query="受注の登録方法は？",
        filters={"large_category": "受注", "small_category": "登録", "as_of": "2026-04-01"},
    )

    with pytest.raises(RuntimeError, match="stop"):
        await engine.run(request)

    classification = captured["classification_filter"]
    assert classification.active
    assert classification.large_category == "受注"
    assert classification.middle_category == ""
    assert classification.small_category == "登録"
    assert classification.as_of == "2026-04-01"


async def test_answer_engine_standard_flow_without_rerank_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rag_engine.adapters.model_api as engine_api

    class NoRerankGenAi(FakeGenAi):
        async def rerank(
            self, query: str, documents: list[str], top_n: int
        ) -> list[tuple[int, float]]:
            raise AssertionError("rerank は無効")

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    engine = AnswerEngine(
        Settings(
            rag_query_strategy="simple_retrieval",
            rag_answer_flow="standard_rag",
            rag_rerank_enabled=False,
        ),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=NoRerankGenAi(),  # type: ignore[arg-type]
    )

    outcome = await engine.run(SearchRequest(query="受注の登録方法は？"))

    assert "登録ボタン" in outcome.answer


def test_build_engine_settings_configures_rerank(tmp_path: Any) -> None:
    """rag_engine が rerank を実行する条件(model と Base URL)を backend の設定から渡す(#275)。"""
    from rag_engine.generation.answer_records import _rerank_configured

    from app.rag.answer_engine import build_engine_settings

    engine_settings = build_engine_settings(
        Settings(
            enterprise_ai_endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            genai_rerank_model="qwen3-rerank",
        ),
        output_dir=tmp_path,
    )

    assert engine_settings.embedding_base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert engine_settings.rerank_model == "qwen3-rerank"
    assert _rerank_configured(engine_settings) is True


def test_build_engine_settings_passes_embedding_model_connection(tmp_path: Any) -> None:
    """埋め込み / 再ランクは、そのモデルの接続の endpoint と key を rag_engine に渡す(#45)。"""
    from pr_system_settings.model import EnterpriseAiConfiguredModel

    from app.rag.answer_engine import build_engine_settings

    bailian = "https://llm-xxxx.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    settings = Settings(
        enterprise_ai_endpoint="https://api.deepseek.com",
        enterprise_ai_api_key="sk-deepseek",
        enterprise_ai_tertiary_endpoint=bailian,
        enterprise_ai_tertiary_api_key="sk-aliyun",
        enterprise_ai_models=[
            EnterpriseAiConfiguredModel(model_id="deepseek-flash"),
            EnterpriseAiConfiguredModel(
                model_id="text-embedding-v4", kind="embedding", connection_id="tertiary"
            ),
            EnterpriseAiConfiguredModel(
                model_id="qwen3-rerank", kind="rerank", connection_id="tertiary"
            ),
        ],
        enterprise_ai_default_text_model="deepseek-flash",
        genai_embedding_model="text-embedding-v4",
        genai_rerank_model="qwen3-rerank",
    )

    engine_settings = build_engine_settings(settings, output_dir=tmp_path)

    assert engine_settings.embedding_base_url == bailian
    assert engine_settings.embedding_api_key == "sk-aliyun"
    assert engine_settings.rerank_base_url == bailian.replace(
        "/compatible-mode/", "/compatible-api/"
    )
    assert engine_settings.rerank_api_key == "sk-aliyun"
    # LLM はプライマリ接続（DeepSeek）のまま。
    assert engine_settings.llm_providers["enterprise-ai"].api_key == "sk-deepseek"


def test_answer_engine_has_no_domain_profile_setting(tmp_path: Any) -> None:
    from app.rag.answer_engine import build_engine_settings

    assert "rag_answer_profile" not in Settings.model_fields
    assert not hasattr(build_engine_settings(Settings(), output_dir=tmp_path), "profile")


async def test_rerank_enabled_calls_backend_rerank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rerank ON(既定)なら、検索候補を backend の Cohere rerank で並べ替える(#275)。

    以前は rag_engine の Settings に接続先が渡らず、Rerank ON でも常に「未実行」だった。
    """
    import rag_engine.adapters.model_api as engine_api

    class RecordingGenAi(FakeGenAi):
        def __init__(self) -> None:
            self.rerank_calls: list[int] = []

        async def rerank(
            self, query: str, documents: list[str], top_n: int
        ) -> list[tuple[int, float]]:
            self.rerank_calls.append(len(documents))
            return await super().rerank(query, documents, top_n)

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    genai = RecordingGenAi()
    engine = AnswerEngine(
        Settings(
            enterprise_ai_endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            rag_query_strategy="simple_retrieval",
            rag_answer_flow="standard_rag",
        ),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=genai,  # type: ignore[arg-type]
    )

    outcome = await engine.run(SearchRequest(query="受注の登録方法は？"))

    assert "登録ボタン" in outcome.answer
    assert genai.rerank_calls, "Rerank ON なのに backend の rerank が呼ばれていない"


class _ScoredGenAi(FakeGenAi):
    """rerank の関連度を固定値で返すスタブ(モデルの API は呼ばない)。"""

    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        return [(index, 0.87 - index * 0.1) for index in range(len(documents))][:top_n]


def _rerank_step_status(diagnostics: dict[str, Any]) -> str:
    steps = [step for step in diagnostics["execution_steps"] if step["name"] == "Rerank"]
    assert steps, "実行記録に Rerank の工程が無い"
    return str(steps[0]["status"])


@pytest.mark.parametrize("generate_answer", [True, False], ids=["answer", "search"])
async def test_rerank_score_and_rank_reach_citations(
    monkeypatch: pytest.MonkeyPatch, generate_answer: bool
) -> None:
    """rerank した候補の関連度と順位を引用の ``rerank_score`` / ``rerank_rank`` に写す(#662)。

    以前は rerank を実行しても引用へ写さず、RAG 検索の引用カードが常に「Rerank 未実行」だった。
    rerank の候補ではない前後の chunk(同じ節の前後)は ``None`` のまま。
    """
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    engine = AnswerEngine(
        Settings(
            enterprise_ai_endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
            rag_query_strategy="simple_retrieval",
            rag_answer_flow="standard_rag",
        ),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=_ScoredGenAi(),  # type: ignore[arg-type]
    )

    outcome = await engine.run(
        SearchRequest(query="受注の登録方法は？", generate_answer=generate_answer)
    )

    by_id = {citation.chunk_id: citation for citation in outcome.citations}
    anchor = by_id["doc-1:c1"]
    assert anchor.rerank_score == pytest.approx(0.87)
    assert anchor.metadata["rerank_rank"] == 1
    if "doc-1:c2" in by_id:
        assert by_id["doc-1:c2"].rerank_score is None
        assert "rerank_rank" not in by_id["doc-1:c2"].metadata
    assert _rerank_step_status(outcome.diagnostics) == "complete"


@pytest.mark.parametrize(
    ("settings", "reason"),
    [
        (
            {
                "enterprise_ai_endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                "rag_rerank_enabled": False,
            },
            "無効の設定",
        ),
        ({"enterprise_ai_endpoint": ""}, "モデルまたは Base URL が未設定"),
    ],
    ids=["disabled", "unconfigured"],
)
async def test_rerank_skip_is_recorded_with_reason(
    monkeypatch: pytest.MonkeyPatch, settings: dict[str, Any], reason: str
) -> None:
    """rerank を実行しないときは、実行記録の状態に理由を付け、引用の rerank は空のまま(#662)。"""
    import rag_engine.adapters.model_api as engine_api

    class NoRerankGenAi(FakeGenAi):
        async def rerank(
            self, query: str, documents: list[str], top_n: int
        ) -> list[tuple[int, float]]:
            raise AssertionError("rerank は実行しない")

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    engine = AnswerEngine(
        Settings(rag_query_strategy="simple_retrieval", rag_answer_flow="standard_rag", **settings),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=NoRerankGenAi(),  # type: ignore[arg-type]
    )

    outcome = await engine.run(SearchRequest(query="受注の登録方法は？", generate_answer=False))

    assert outcome.citations
    assert all(citation.rerank_score is None for citation in outcome.citations)
    assert all("rerank_rank" not in citation.metadata for citation in outcome.citations)
    assert _rerank_step_status(outcome.diagnostics) == f"未実行: {reason}"


def _pdf_with_figure() -> bytes:
    from tests.fixtures.synthetic_pdf import SyntheticPdf

    document = SyntheticPdf()
    document.page(500, 700).rect((0, 0, 50, 35), fill=(0, 0, 1))
    return document.to_bytes()


def _figure_oracle() -> FakeOracle:
    oracle = FakeOracle()
    figure = oracle.chunks[0]
    metadata = json.loads(str(figure.metadata["engine_metadata_json"]))
    metadata["image_evidence"] = [
        {
            "image_id": "docling-p1-5",
            "record_id": "docling-p1-5",
            "page": 1,
            "bbox": [0, 0, 100, 70],
            "crop_path": "docling/visuals/missing.png",
            "embedding_modality": "image_caption_fallback",
        }
    ]
    oracle.chunks[0] = figure.model_copy(
        update={
            "metadata": {
                **figure.metadata,
                "page_width": 1000,
                "page_height": 1400,
                "engine_metadata_json": json.dumps(metadata),
            }
        }
    )
    return oracle


def _model_settings(**overrides: Any) -> Settings:
    """回答のモデルの候補: テキストだけのモデル 2 つと画像を読めるモデル 2 つ(既定の Vision)。"""
    values: dict[str, Any] = {
        "enterprise_ai_default_text_model": "text-model",
        "enterprise_ai_default_vision_model": "vision-model",
        **overrides,
    }
    return Settings(
        _env_file=None,
        enterprise_ai_models=[
            EnterpriseAiConfiguredModel(model_id="text-model", display_name="Text A"),
            EnterpriseAiConfiguredModel(model_id="text-model-b", display_name="Text B"),
            EnterpriseAiConfiguredModel(
                model_id="vlm-answer", display_name="VLM A", vision_enabled=True
            ),
            EnterpriseAiConfiguredModel(
                model_id="vision-model", display_name="Vision", vision_enabled=True
            ),
        ],
        **values,
    )


def _resolved_model(settings: Any, options: dict[str, Any]) -> str:
    """rag_engine が呼ぶ provider のモデル(None は回答のモデル)。"""
    from rag_engine.config import get_llm_provider

    provider_id = options.get("provider_id") or settings.default_answer_llm
    return get_llm_provider(settings, provider_id).model


@pytest.mark.parametrize(
    ("answer_model_id", "model"),
    [(None, "vlm-answer"), ("vlm-answer", "vlm-answer")],
    ids=["default-model", "column-model"],
)
async def test_answer_engine_attaches_evidence_images_for_vision_answer_model(
    monkeypatch: pytest.MonkeyPatch, answer_model_id: str | None, model: str
) -> None:
    """回答のモデルが画像を読めるなら、根拠の chunk の画像を切り出してそのモデルへ添付する(#3444)。

    質問は画像を名指ししない。既定の Vision モデルへは切り替えず、診断のモデルも 1 つだけ。
    """
    import rag_engine.adapters.model_api as engine_api

    import app.rag.answer_engine as engine_module

    loaded: list[str] = []

    async def fake_source(oracle: object, document_id: str) -> bytes:
        loaded.append(document_id)
        return _pdf_with_figure()

    images_seen: list[list[str]] = []
    called: list[str] = []

    def fake_text(system: str, prompt: str, settings: Any, schema: type, **kw: Any) -> Any:
        called.append(_resolved_model(settings, kw))
        return _fake_llm(system, prompt, settings, schema, **kw)

    def fake_multimodal(
        system: str, prompt: str, image_paths: list[Any], settings: Any, schema: type, **kw: Any
    ) -> Any:
        from pathlib import Path

        images_seen.append([str(path) for path in image_paths])
        called.append(_resolved_model(settings, kw))
        assert all(Path(path).read_bytes().startswith(b"\x89PNG") for path in image_paths)
        return _fake_llm(system, prompt, settings, schema, **kw)

    monkeypatch.setattr(engine_module, "load_parsed_source", fake_source)
    monkeypatch.setattr(engine_api, "parse_text_response", fake_text)
    monkeypatch.setattr(engine_api, "parse_multimodal_response", fake_multimodal)
    overrides = {} if answer_model_id else {"enterprise_ai_default_text_model": "vlm-answer"}
    engine = AnswerEngine(
        _model_settings(**overrides),
        oracle=_figure_oracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        answer_model_id=answer_model_id,
    )

    outcome = await engine.run(SearchRequest(query="受注登録の手順を教えてください"))

    assert loaded == ["doc-1"]
    assert images_seen and images_seen[0][0].endswith("crops/docling-p1-5.png")
    assert "登録ボタン" in outcome.answer
    # 回答の実行の LLM の呼び出しは、すべて回答のモデル(既定の Vision モデルを呼ばない)。
    assert called and set(called) == {model}
    models = outcome.diagnostics["models"]
    assert models["llm"]["model_id"] == model
    assert models["attached_images"] == 1
    assert "vision" not in models


@pytest.mark.parametrize(
    ("settings", "answer_model_id", "model"),
    [
        (_model_settings(), None, "text-model"),
        # 比較の列のモデル(テキストだけのモデル)。既定の Vision モデルがあっても切り替えない。
        (_model_settings(), "text-model-b", "text-model-b"),
        # 既定の Vision モデルが無い環境。
        (
            Settings(_env_file=None, enterprise_ai_default_text_model="text-model"),
            None,
            "text-model",
        ),
    ],
    ids=["default-text-model", "column-text-model", "no-vision-model"],
)
async def test_answer_engine_never_attaches_images_for_text_answer_model(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, answer_model_id: str | None, model: str
) -> None:
    """回答のモデルが画像を読めないなら、画像を名指しする質問でも画像を添付しない(#3444)。

    原本からの切り出しも、既定の Vision モデルの呼び出しもしない。

    図の内容は取り込み時に作った文字の説明(根拠の本文)で答える。
    """
    import rag_engine.adapters.model_api as engine_api

    import app.rag.answer_engine as engine_module

    async def fail_source(oracle: object, document_id: str) -> bytes:
        raise AssertionError("画像を読めない回答のモデルでは原本を読まない")

    def fail_multimodal(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("画像を読めない回答のモデルへ画像を添付しない")

    called: list[str] = []

    def fake_text(system: str, prompt: str, settings: Any, schema: type, **kw: Any) -> Any:
        called.append(_resolved_model(settings, kw))
        return _fake_llm(system, prompt, settings, schema, **kw)

    monkeypatch.setattr(engine_module, "load_parsed_source", fail_source)
    monkeypatch.setattr(engine_api, "parse_text_response", fake_text)
    monkeypatch.setattr(engine_api, "parse_multimodal_response", fail_multimodal)
    engine = AnswerEngine(
        settings,
        oracle=_figure_oracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        answer_model_id=answer_model_id,
    )

    outcome = await engine.run(SearchRequest(query="受注登録画面のボタンの画像は？"))

    assert "登録ボタン" in outcome.answer
    assert called and set(called) == {model}
    models = outcome.diagnostics["models"]
    assert models["llm"]["model_id"] == model
    assert models["attached_images"] == 0


def test_engine_settings_pass_the_answer_model_image_capability() -> None:
    """rag_engine の回答のモデルが画像を読めるかは、登録モデルの vision_enabled で決める(#3444)。"""
    from pathlib import Path

    from rag_engine.config import answer_model_reads_images

    from app.rag.answer_engine import build_engine_settings

    settings = _model_settings()
    for model_id, expected in (("text-model", False), ("vlm-answer", True)):
        engine_settings = build_engine_settings(
            settings, output_dir=Path("unused"), answer_model_id=model_id
        )
        assert answer_model_reads_images(engine_settings) is expected
    # 既定(テキストだけのモデル)は読めない。既定の Vision モデルは取り込み用に残る。
    default = build_engine_settings(settings, output_dir=Path("unused"))
    assert answer_model_reads_images(default) is False
    assert default.vision_model == "vision-model"


def _nfkc(text: str) -> str:
    """rag_poc は質問を NFKC 正規化してから検索する(全角「？」→「?」)。"""
    import unicodedata

    return unicodedata.normalize("NFKC", text)


class QueryRecordingOracle(FakeOracle):
    def __init__(self) -> None:
        super().__init__()
        self.queries: list[str] = []

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        self.queries.append(query)
        return await super().hybrid_search(query, embedding, top_k, mode, filters)


class RewriteLlm:
    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []
        self.system_prompts: list[str] = []

    async def generate(self, prompt: str, context: str, **kwargs: Any) -> str:
        self.calls.append((prompt, context))
        self.system_prompts.append(str(kwargs.get("system_prompt") or ""))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


async def _run_chat(
    monkeypatch: pytest.MonkeyPatch,
    llm: RewriteLlm,
    *,
    history: bool,
) -> tuple[Any, QueryRecordingOracle]:
    import rag_engine.adapters.model_api as engine_api

    from app.rag.pipeline import ChatTurn, RagPipeline

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    oracle = QueryRecordingOracle()
    pipeline = RagPipeline(
        settings=Settings(),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
    )
    turns = (
        [
            ChatTurn(role="USER", content="受注入力画面の使い方は？"),
            ChatTurn(role="ASSISTANT", content="受注番号を入力して登録します。"),
        ]
        if history
        else None
    )
    response = await pipeline.run(SearchRequest(query="それの登録方法は？"), history=turns)
    return response, oracle


async def test_answer_engine_chat_rewrites_question_from_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = RewriteLlm(json.dumps({"question": "受注入力画面での受注の登録方法は？"}))

    response, oracle = await _run_chat(monkeypatch, llm, history=True)

    assert llm.calls and "受注入力画面の使い方" in llm.calls[0][1]
    assert oracle.queries[0] == _nfkc("受注入力画面での受注の登録方法は？")
    assert response.diagnostics.answer is not None
    assert response.diagnostics.answer["original_question"] == "それの登録方法は？"
    assert response.diagnostics.answer["rewritten_question"] == "受注入力画面での受注の登録方法は？"


async def test_answer_engine_single_question_does_not_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = RewriteLlm("使われないはず")

    response, oracle = await _run_chat(monkeypatch, llm, history=False)

    assert llm.calls == []
    assert oracle.queries[0] == _nfkc("それの登録方法は？")
    assert response.diagnostics.answer is not None
    assert response.diagnostics.answer["rewritten_question"] == ""


async def test_answer_engine_rewrite_failure_keeps_original_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, oracle = await _run_chat(monkeypatch, RewriteLlm(RuntimeError("down")), history=True)

    assert oracle.queries[0] == _nfkc("それの登録方法は？")
    assert "登録ボタン" in response.answer


class SavingOracle(FakeOracle):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.saved: list[dict[str, Any]] = []
        self.purged: list[int] = []
        self.fail = fail

    async def save_answer_record(self, record: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("db down")
        self.saved.append(dict(record))

    async def purge_answer_records(self, retention_days: int) -> int:
        self.purged.append(retention_days)
        return 0


def _walk_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """評価の入力の根拠を、子も含めて順に返す。"""
    walked: list[dict[str, Any]] = []
    for item in items:
        walked.append(item)
        walked.extend(_walk_evidence(item.get("children") or []))
    return walked


async def test_answer_record_is_saved_per_surface(monkeypatch: pytest.MonkeyPatch) -> None:
    import rag_engine.adapters.model_api as engine_api

    from app.rag.pipeline import RagPipeline

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    oracle = SavingOracle()
    pipeline = RagPipeline(
        settings=Settings(rag_answer_record_retention_days=90),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )

    response = await pipeline.run(
        SearchRequest(query="受注の登録方法は？", search_answer_profile_id="bv-1"),
        trace_id="trace-1",
    )
    await pipeline.run(SearchRequest(query="受注の登録方法は？"), trace_id="trace-2", history=[])

    first, second = oracle.saved
    assert first["trace_id"] == "trace-1"
    assert first["search_answer_profile_id"] == "bv-1"
    assert first["surface"] == "search"
    assert first["answer"] == response.answer
    assert first["citations"][0]["chunk_id"] == "doc-1:c1"
    assert first["diagnostics"]["evidence_tree"]
    # 標準回答での評価に使う入力(根拠の本文を含む)も保存する。
    evaluation_input = first["evaluation_input"]
    assert evaluation_input["question"] == "受注の登録方法は？"
    assert "登録ボタン" in evaluation_input["answer_text"]
    assert "登録ボタン" in json.dumps(evaluation_input["evidence_items"], ensure_ascii=False)
    # 引用は画面・評価が使う項目だけを保存し、索引の内部の項目と親の本文を持たない（#1371）。
    # 応答の引用（MCP・品質評価が同じプロセスで読む）は検索の結果のまま。
    assert "parent_text" in response.citations[0].metadata
    for citation in first["citations"]:
        assert citation["metadata"].keys() <= STORED_CITATION_METADATA_KEYS
        assert citation["text"]
    assert first["citations"][0]["metadata"]["page_start"] == 1
    # 評価の入力の根拠も、評価が読む項目（ID・出典・頁・本文・子）だけにする。
    for item in _walk_evidence(evaluation_input["evidence_items"]):
        assert item.keys() <= {
            "id",
            "chunk_uid",
            "source",
            "source_run_id",
            "page_start",
            "page_end",
            "text",
            "children",
        }
    assert second["surface"] == "chat"
    assert oracle.purged == [90, 90]  # 保持日数を決めたときは保存のたびに期限切れを削除する


async def test_answer_record_purge_skipped_when_retention_unlimited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """既定の保持日数は 0（無期限）で、回答の記録を自動では削除しない（#3235）。"""
    import rag_engine.adapters.model_api as engine_api

    from app.rag.pipeline import RagPipeline

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    oracle = SavingOracle()
    assert Settings().rag_answer_record_retention_days == 0
    pipeline = RagPipeline(
        settings=Settings(),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )

    await pipeline.run(SearchRequest(query="受注の登録方法は？"), trace_id="trace-1")

    assert len(oracle.saved) == 1
    assert oracle.purged == []


async def test_answer_record_save_failure_still_returns_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rag_engine.adapters.model_api as engine_api

    from app.rag.pipeline import RagPipeline

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    pipeline = RagPipeline(
        settings=Settings(),
        oracle=SavingOracle(fail=True),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )

    response = await pipeline.run(SearchRequest(query="受注の登録方法は？"))

    assert "登録ボタン" in response.answer


def _fake_evaluation_llm(
    system: str, prompt: str, settings: Any, schema: type, **options: Any
) -> Any:
    from rag_engine.evaluation.answer_eval import (
        AnswerCorrectnessOutput,
        ClaimJudgement,
        CoverageOutput,
        StandardAnswerScope,
    )

    if schema is AnswerCorrectnessOutput:
        return AnswerCorrectnessOutput(verdict="correct", reason="標準回答と一致")
    if schema is StandardAnswerScope:
        return StandardAnswerScope.model_validate(
            {
                "requirements": [{"standard_answer_unit_ids": ["S1"], "requirement": "登録の手順"}],
                "excluded_case_data": [],
            }
        )
    if schema is CoverageOutput:
        passage = json.loads(prompt)["answer_passages"][0]["id"]
        return CoverageOutput.model_validate(
            {
                "coverage_checks": [
                    {
                        "requirement_index": 1,
                        "reason": "手順の説明",
                        "status": "addressed",
                        "answer_passage_id": passage,
                    }
                ]
            }
        )
    if schema is ClaimJudgement:
        return ClaimJudgement(reason="手順の確認の案内", status="data_confirmation")
    raise AssertionError(f"unexpected schema: {schema.__name__}")


async def test_saved_answer_is_evaluated_with_standard_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rag_engine.adapters.model_api as engine_api

    from app.rag.answer_engine import evaluate_answer_record

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    engine = AnswerEngine(
        Settings(),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )
    outcome = await engine.run(SearchRequest(query="受注の登録方法は？"))
    assert outcome.evaluation_input is not None

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_evaluation_llm)
    evaluation = evaluate_answer_record(
        outcome.evaluation_input,
        "受注番号を入力し、登録ボタンを押します。",
        Settings(),
        citations=outcome.citations,
    )

    assert evaluation["status"] == "completed", evaluation
    assert evaluation["requirement_coverage"] == 1.0
    # 評価の基準の指標(1 件の回答で測れるもの)と閾値で合否を付ける(#680)。
    assert evaluation["suite"] == "standard"
    metrics = {metric["name"]: metric for metric in evaluation["metrics"]}
    assert {"claim_support_rate", "requirement_coverage", "answer_correctness"} == set(metrics)
    assert not {"context_recall", "mrr", "answer_keyword_hit_rate"} & set(metrics)
    assert evaluation["passed"] is all(
        metric["passed"] for metric in metrics.values() if metric["passed"] is not None
    )


def test_evaluation_without_standard_answer_does_not_call_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import rag_engine.evaluation.answer_eval as evaluator

    from app.rag.answer_engine import evaluate_answer_record

    calls = []

    def fake(payload: Any, settings: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(payload)
        return {"status": "completed", "claim_checks": [], "requirement_coverage": None}

    monkeypatch.setattr(evaluator, "evaluate_answer_payload", fake)
    evaluation = evaluate_answer_record(
        {"question": "q", "answer_text": "a"}, " ", Settings(), citations=[]
    )
    assert evaluation["status"] == "completed"
    assert calls[0]["allow_without_standard"] is True
    assert calls[0]["six_metrics"] is True
    assert evaluation["claim_support_rate"] is None
    assert evaluation["passed"] is None


# --- 文書の分類を回答フローへ渡す(#545)・質問の理解を検索に使う(#546) ---


@pytest.fixture
def retired_profile_file(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """廃止した外部ファイルが残っていても、検索範囲の分類だけで照合する。"""
    path = tmp_path / "domain_profile.json"
    path.write_text(
        json.dumps(
            {"business_patterns": [["業務A", "業務A"], ["業務B", "業務B"], ["業務C", "業務C"]]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("RAG_ENGINE_DOMAIN_PROFILE_FILE", str(path))
    yield path


def _doc_chunk(
    document_id: str,
    text: str,
    *,
    file_name: str = "manual.pdf",
    retrieval_profile: dict[str, object] | None = None,
) -> RetrievedChunk:
    chunk = _chunk(f"{document_id}:c1", text, 1)
    metadata = json.loads(str(chunk.metadata["engine_metadata_json"]))
    if retrieval_profile is not None:
        metadata["retrieval_profile"] = retrieval_profile
    return chunk.model_copy(
        update={
            "document_id": document_id,
            "file_name": file_name,
            "metadata": {
                **chunk.metadata,
                "chunk_group_id": f"{document_id}-p1",
                "parent_text": text,
                "chunk_strategy": "small_to_big",
                "engine_search_text": f"Source file: {file_name}\nChild text: {text}",
                "engine_metadata_json": json.dumps(metadata, ensure_ascii=False),
            },
        }
    )


class TwoDocumentOracle(FakeOracle):
    """業務B の文書を 1 位、業務A の文書を 2 位で返す検索のスタブ(兄弟 chunk なし)。"""

    def __init__(self) -> None:
        super().__init__()
        self.chunks = [
            _doc_chunk("doc-b", "業務Bでは受注番号を入力し、確定ボタンを押します。"),
            _doc_chunk("doc-a", "受注番号を入力し、登録ボタンを押します。"),
        ]

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        self.filters.append(dict(filters or {}))
        return list(self.chunks)

    async def context_group_siblings(
        self,
        anchors: list[RetrievedChunk],
        *,
        max_chunks_per_group: int,
        neighbor_child_count: int | None = None,
    ) -> list[RetrievedChunk]:
        return []


async def _answer_two_documents(
    monkeypatch: pytest.MonkeyPatch,
    oracle: TwoDocumentOracle,
    question: str,
    filters: dict[str, str] | None = None,
) -> Any:
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    engine = AnswerEngine(
        Settings(
            rag_query_strategy="simple_retrieval",
            rag_answer_flow="standard_rag",
            rag_rerank_enabled=False,
        ),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )
    return await engine.run(SearchRequest(query=question, filters=filters or {}))


@pytest.mark.parametrize(
    ("stored", "question"),
    [
        ("10_業務A", "業務Aの受注の登録方法は？"),
        ("業務A", "業務Ａの受注の登録方法は？"),
        ("10_業務Ａ", "業務Aで受注を登録する方法は？"),
    ],
    ids=["code-prefix", "full-width-question", "full-width-stored"],
)
async def test_answer_engine_keeps_only_candidates_of_named_business(
    monkeypatch: pytest.MonkeyPatch, stored: str, question: str
) -> None:
    """質問が検索範囲の大分類を名指ししたら、その業務の文書の候補だけが根拠に残る(#545 / #553)。

    番号の接頭辞・全角半角の違いは #547 の正規化で吸収する。
    """
    oracle = TwoDocumentOracle()
    oracle.classifications = {
        "doc-a": {"large_category": stored},
        "doc-b": {"large_category": "20_業務B"},
    }
    oracle.large_categories = [stored, "20_業務B"]
    monkeypatch.setattr(answer_engine_module, "_today", lambda: date(2026, 4, 1))

    outcome = await _answer_two_documents(
        monkeypatch, oracle, question, filters={"knowledge_base_id": "kb-1"}
    )

    assert {chunk.document_id for chunk in outcome.citations} == {"doc-a"}
    # 分類はヒットした document_id でまとめて 1 回だけ読む(N+1 にしない)。
    assert oracle.classification_calls == [["doc-a", "doc-b"]]
    # 大分類の語の一覧は、検索と同じ範囲(回答の初めに決めた基準日を含む。#2602)で 1 回の回答に
    # 1 回だけ読む(#553)。
    assert oracle.large_category_calls == [{"knowledge_base_id": "kb-1", "as_of": "2026-04-01"}]


@pytest.mark.parametrize(
    ("classifications", "large_categories", "question"),
    [
        ({}, ["業務A", "業務B"], "業務Aの受注の登録方法は？"),
        (
            {"doc-a": {"large_category": "業務A"}, "doc-b": {"large_category": "業務B"}},
            ["業務A", "業務B"],
            "受注の登録方法は？",
        ),
        (
            {"doc-a": {"large_category": "業務A"}, "doc-b": {"large_category": "業務B"}},
            ["業務A", "業務B"],
            "業務Cの受注の登録方法は？",
        ),
        (
            {"doc-a": {"large_category": "業務A"}, "doc-b": {"large_category": "業務B"}},
            ["業務A", "業務B", "業務C"],
            "業務Cの受注の登録方法は？",
        ),
    ],
    ids=["no-classification", "no-business-named", "out-of-scope", "no-matching-candidate"],
)
async def test_answer_engine_business_filter_keeps_candidates_without_match(
    monkeypatch: pytest.MonkeyPatch,
    classifications: dict[str, dict[str, object]],
    large_categories: list[str],
    question: str,
) -> None:
    """名指しが無い・範囲外の大分類・一致する候補が無いときは、候補を落とさない(#545 / #553)。"""
    oracle = TwoDocumentOracle()
    oracle.classifications = classifications
    oracle.large_categories = large_categories

    outcome = await _answer_two_documents(monkeypatch, oracle, question)

    assert {chunk.document_id for chunk in outcome.citations} == {"doc-a", "doc-b"}


async def test_answer_engine_business_names_do_not_come_from_domain_profile(
    monkeypatch: pytest.MonkeyPatch, retired_profile_file: Any
) -> None:
    """domain profile の business_patterns は質問の業務名に使わない(#553)。

    profile の規則が「業務A」に一致しても、検索範囲の大分類に無ければ絞らない。
    """
    oracle = TwoDocumentOracle()
    oracle.classifications = {
        "doc-a": {"large_category": "業務A"},
        "doc-b": {"large_category": "業務B"},
    }

    outcome = await _answer_two_documents(monkeypatch, oracle, "業務Aの受注の登録方法は？")

    assert {chunk.document_id for chunk in outcome.citations} == {"doc-a", "doc-b"}


async def test_answer_engine_answers_without_business_names_when_load_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """大分類の語の一覧を読めなくても、絞らずに回答を続ける(#553)。"""

    class FailingOracle(TwoDocumentOracle):
        async def retrieval_large_categories(self, filters: dict[str, str]) -> list[str]:
            raise RuntimeError("db down")

    oracle = FailingOracle()
    oracle.classifications = {
        "doc-a": {"large_category": "業務A"},
        "doc-b": {"large_category": "業務B"},
    }

    outcome = await _answer_two_documents(monkeypatch, oracle, "業務Aの受注の登録方法は？")

    assert {chunk.document_id for chunk in outcome.citations} == {"doc-a", "doc-b"}


@pytest.mark.parametrize(
    ("question", "large_categories", "expected"),
    [
        ("業務Aの登録は？", ["10_業務A", "20_業務B"], ["10_業務A"]),
        ("ｇｙｏｍｕ-Xの登録は？", ["gyomu-x"], ["gyomu-x"]),
        ("業務Aと業務Bの違いは？", ["業務A", "業務B"], ["業務A", "業務B"]),
        # 長い名前から照合し、「業務A」の中の「業務」は拾わない。
        ("業務Aの登録は？", ["業務", "業務A"], ["業務A"]),
        ("業務の登録は？", ["業務", "業務A"], ["業務"]),
        # 同じ名前の保存値(番号の接頭辞の有無)はどちらも返す。
        ("業務Aの登録は？", ["10_業務A", "業務A"], ["10_業務A", "業務A"]),
        # 1 文字の名前は質問の別の語に偶然含まれるため照合しない。
        ("Aの登録は？", ["A"], []),
        ("登録は？", ["業務A"], []),
    ],
    ids=[
        "code-prefix",
        "width-and-case",
        "two-businesses",
        "longest-first",
        "short-name-alone",
        "same-label-values",
        "single-char",
        "no-name",
    ],
)
def test_question_business_domains_matches_scope_vocabulary(
    question: str, large_categories: list[str], expected: list[str]
) -> None:
    """質問の業務名は、検索範囲の大分類を #547 の正規化で照合して保存どおりの値で返す(#553)。"""
    from app.rag.answer_engine import question_business_domains

    assert question_business_domains(question, large_categories) == expected


async def test_answer_engine_search_puts_document_classification_on_child_and_parent() -> None:
    """子・親の chunk の metadata に document.classification を載せる(#545)。"""
    from app.rag.answer_engine import _SearchState

    oracle = TwoDocumentOracle()
    oracle.classifications = {"doc-a": {"large_category": "業務A", "small_category": "登録"}}
    engine = AnswerEngine(Settings(), oracle=oracle, genai=FakeGenAi())  # type: ignore[arg-type]

    result = await engine._search(
        SearchRequest(query="q"), _SearchState(), retrieval_queries=["受注の登録"]
    )

    documents = {chunk.chunk_uid: chunk.metadata.get("document") for chunk in result.all_chunks}
    assert documents["doc-a:c1"] == {
        "classification": {"large_category": "業務A", "small_category": "登録"}
    }
    assert documents["doc-a:doc-a-p1"] == documents["doc-a:c1"]
    assert documents["doc-b:c1"] is None
    # 分類は検索用テキスト(埋め込み・全文検索の入力)には入れない。
    child = next(chunk for chunk in result.child_chunks if chunk.chunk_uid == "doc-a:c1")
    assert "業務A" not in child.retrieval_text


FIRST_PAGE_TEXT = "受注管理規程 第3版 営業本部 2025年4月1日"


def _first_page_context(text: str = FIRST_PAGE_TEXT) -> dict[str, object]:
    return {
        "page": 1,
        "status": "available",
        "text": text,
        "engine": "docling",
        "record_ids": ["docling-p1-1"],
        "truncated": False,
    }


def _chunk_in_chunk_set(chunk: RetrievedChunk, *, page: int = 2) -> RetrievedChunk:
    return chunk.model_copy(
        update={
            "metadata": {
                **chunk.metadata,
                "chunk_set_id": "cs-1",
                "page_start": page,
                "page_end": page,
            }
        }
    )


class RecordingEmbedGenAi(FakeGenAi):
    def __init__(self) -> None:
        self.embedded: list[str] = []

    async def embed(
        self, texts: list[str], *, input_type: str = "SEARCH_DOCUMENT"
    ) -> list[list[float]]:
        self.embedded.extend(texts)
        return await super().embed(texts, input_type=input_type)


async def test_answer_record_passes_first_page_context_as_document_background(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """chunk set の 1 ページ目の本文を「文書の背景」として回答の LLM に渡す(#557)。

    ヒットした chunk set の分を 1 回の回答で 1 回だけ読み、検索の文には入れない。
    """
    import rag_engine.adapters.model_api as engine_api

    prompts: list[str] = []

    def recording_llm(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
        prompts.append(prompt)
        return _fake_llm(system, prompt, settings, schema, **options)

    monkeypatch.setattr(engine_api, "parse_text_response", recording_llm)
    oracle = FakeOracle()
    oracle.chunks = [_chunk_in_chunk_set(chunk) for chunk in oracle.chunks]
    oracle.first_page_contexts = {"cs-1": _first_page_context()}
    genai = RecordingEmbedGenAi()
    engine = AnswerEngine(
        Settings(),
        oracle=oracle,  # type: ignore[arg-type]
        genai=genai,  # type: ignore[arg-type]
    )

    outcome = await engine.run(SearchRequest(query="受注の登録方法は？"))

    assert "登録ボタン" in outcome.answer
    assert oracle.first_page_calls == [["cs-1"]]
    background = [prompt for prompt in prompts if FIRST_PAGE_TEXT in prompt]
    assert background
    assert all("First-page background" in prompt for prompt in background)
    # 検索の文(embedding・全文検索の入力)には入れない。
    assert genai.embedded
    assert all(FIRST_PAGE_TEXT not in text for text in genai.embedded)


async def test_answer_engine_search_puts_first_page_context_on_child_and_parent() -> None:
    """子・親の chunk の document に first_page_context を載せ、検索用テキストには入れない(#557)。

    #557 より前に保存した chunk(自身が持つ値)は、そちらを使う。
    """
    from app.rag.answer_engine import _SearchState

    oracle = FakeOracle()
    legacy = _chunk_in_chunk_set(oracle.chunks[1])
    legacy_metadata = json.loads(str(legacy.metadata["engine_metadata_json"]))
    legacy_metadata["document"] = {"first_page_context": _first_page_context("旧い表紙")}
    oracle.chunks = [
        _chunk_in_chunk_set(oracle.chunks[0]),
        legacy.model_copy(
            update={
                "metadata": {
                    **legacy.metadata,
                    "engine_metadata_json": json.dumps(legacy_metadata, ensure_ascii=False),
                }
            }
        ),
    ]
    oracle.first_page_contexts = {"cs-1": _first_page_context()}
    engine = AnswerEngine(Settings(), oracle=oracle, genai=FakeGenAi())  # type: ignore[arg-type]
    state = _SearchState()

    result = await engine._search(SearchRequest(query="q"), state, retrieval_queries=["受注"])
    await engine._search(SearchRequest(query="q"), state, retrieval_queries=["受注の登録"])

    documents = {chunk.chunk_uid: chunk.metadata.get("document") for chunk in result.all_chunks}
    assert documents["doc-1:c1"] == {"first_page_context": _first_page_context()}
    assert documents["doc-1:cs-1:chunk-docling-p000001"] == documents["doc-1:c1"]
    assert documents["doc-1:c2"] == {"first_page_context": _first_page_context("旧い表紙")}
    child = next(chunk for chunk in result.child_chunks if chunk.chunk_uid == "doc-1:c1")
    assert FIRST_PAGE_TEXT not in child.retrieval_text
    # CRAG の 2 回目の検索でも、読んだ chunk set は読み直さない。
    assert oracle.first_page_calls == [["cs-1"]]


async def test_oracle_chunk_set_first_page_contexts_reads_hit_chunk_sets_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1 ページ目の本文はヒットした chunk set の IN で 1 回だけ読む(#557)。"""
    from app.clients.oracle import OracleClient

    client = OracleClient(settings=Settings())
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_fetch_all(
        statement: str, binds: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        calls.append((statement, dict(binds or {})))
        return [
            {"chunk_set_id": "cs-a", "first_page_context": json.dumps(_first_page_context())},
            {"chunk_set_id": "cs-b", "first_page_context": None},
        ]

    monkeypatch.setattr(client, "_fetch_all", fake_fetch_all)

    assert await client.chunk_set_first_page_contexts([]) == {}
    assert calls == []
    result = await client.chunk_set_first_page_contexts(["cs-a", "cs-b", "cs-a"])

    assert result == {"cs-a": _first_page_context()}
    assert len(calls) == 1
    statement, binds = calls[0]
    assert "FROM rag_chunk_sets cs" in statement
    # 文書の参照範囲(tenant・許可された文書)で絞る。
    assert "JOIN rag_documents d" in statement
    chunk_set_binds = [
        value for key, value in binds.items() if key.startswith("first_page_chunk_set")
    ]
    assert chunk_set_binds == ["cs-a", "cs-b"]


def _inquiry(question: str, business_names: tuple[str, ...] = ()) -> Any:
    """回答フローと同じく、業務名の照合(検索範囲の大分類。#553)を注入して質問を理解する。"""
    from rag_engine.dependencies import AnswerDependencies, bind_dependencies
    from rag_engine.retrieval.inquiry_conditions import parse_inquiry_conditions

    from app.rag.answer_engine import question_business_domains

    def unused(*args: object, **kwargs: object) -> Any:
        raise AssertionError("質問の理解では I/O を呼ばない")

    dependencies = AnswerDependencies(
        search=unused,
        check_ready=unused,
        parse_text=unused,
        parse_images=unused,
        rerank=unused,
        business_domains=lambda text: question_business_domains(text, business_names),
    )
    with bind_dependencies(dependencies):
        return parse_inquiry_conditions(question)


async def _search_filters(
    request_filters: dict[str, str], question: str, *, searches: int = 1
) -> FakeOracle:
    from app.rag.answer_engine import _SearchState

    oracle = FakeOracle()
    engine = AnswerEngine(Settings(), oracle=oracle, genai=FakeGenAi())  # type: ignore[arg-type]
    state = _SearchState()
    for _ in range(searches):
        await engine._search(
            SearchRequest(query=question, filters=request_filters),
            state,
            retrieval_queries=[question],
            inquiry_conditions=_inquiry(question),
        )
    return oracle


async def test_question_file_name_and_pages_become_search_filters() -> None:
    """質問が名指しした文書名・ページを hybrid_search の filters に足す(#546)。"""
    oracle = await _search_filters(
        {"knowledge_base_id": "kb-1"}, "manual.pdf の12ページと13ページの登録手順は？", searches=2
    )

    assert oracle.filters[0] == {
        "knowledge_base_id": "kb-1",
        "file_name": "manual.pdf",
        "page_number_min": "12",
        "page_number_max": "13",
    }
    # 2 回目(CRAG の各回)は同じ検索文・同じ条件の結果を使い回す(#1490)。結果の cache の key は
    # 条件を含むため、条件が 1 回目と違えば検索し直す(ここでは 1 回だけ)。
    assert len(oracle.filters) == 1
    # 存在の確認は CRAG の各回で繰り返さない。
    assert oracle.probes == [{"knowledge_base_id": "kb-1", "file_name": "manual.pdf"}]


async def test_question_file_name_missing_from_knowledge_base_is_dropped() -> None:
    """ナレッジベースに無い文書名は外し、ページも一緒に外す(0 件にしない。#546)。"""
    oracle = await _search_filters({"knowledge_base_id": "kb-1"}, "other.pdf の12ページの手順は？")

    assert oracle.filters[0] == {"knowledge_base_id": "kb-1"}
    assert oracle.probes == [{"knowledge_base_id": "kb-1", "file_name": "other.pdf"}]


@pytest.mark.parametrize(
    ("request_filters", "expected", "probed"),
    [
        ({"file_name": "guide.pdf"}, {"file_name": "guide.pdf"}, False),
        ({"document_id": "doc-9"}, {"document_id": "doc-9"}, False),
        ({"page_number_min": "3"}, {"page_number_min": "3", "file_name": "manual.pdf"}, True),
    ],
    ids=["screen-file-name", "screen-document", "screen-page"],
)
async def test_screen_filters_take_precedence_over_question(
    request_filters: dict[str, str], expected: dict[str, str], probed: bool
) -> None:
    """画面で明示した文書・ページの条件が、質問の文書名・ページより優先する(#546)。"""
    oracle = await _search_filters(request_filters, "manual.pdf の12ページの登録手順は？")

    assert oracle.filters[0] == expected
    assert bool(oracle.probes) is probed


def _order(result: Any) -> list[str]:
    return [chunk.chunk_uid for chunk in result.child_chunks]


async def test_business_match_channel_raises_candidate_of_named_business() -> None:
    """質問の業務に合う文書の候補が business_match のチャネルで上がる(#546 / #553)。"""
    from app.rag.answer_engine import _SearchState

    oracle = TwoDocumentOracle()
    oracle.classifications = {"doc-a": {"large_category": "業務A"}}
    engine = AnswerEngine(Settings(), oracle=oracle, genai=FakeGenAi())  # type: ignore[arg-type]
    question = "業務Aの受注の登録方法は？"

    baseline = await engine._search(
        SearchRequest(query=question), _SearchState(), retrieval_queries=[question]
    )
    boosted = await engine._search(
        SearchRequest(query=question),
        _SearchState(),
        retrieval_queries=[question],
        inquiry_conditions=_inquiry(question, ("業務A", "業務B")),
    )

    assert _order(baseline) == ["doc-b:c1", "doc-a:c1"]
    assert _order(boosted) == ["doc-a:c1", "doc-b:c1"]


@pytest.mark.parametrize(
    ("profile_channel_enabled", "expected"),
    [(True, ["doc-a:c1", "doc-b:c1"]), (False, ["doc-b:c1", "doc-a:c1"])],
    ids=["enabled", "disabled"],
)
async def test_profile_channel_raises_candidate_of_question_profile(
    profile_channel_enabled: bool, expected: list[str]
) -> None:
    """質問の問い合わせ種別に合う chunk が profile のチャネルで上がる。設定で無効にできる(#546)。"""
    from types import SimpleNamespace

    from app.rag.answer_engine import _SearchState

    oracle = TwoDocumentOracle()
    oracle.chunks = [
        _doc_chunk("doc-b", "受注一覧に表示されます。"),
        _doc_chunk(
            "doc-a",
            "登録できない場合の条件は次のとおりです。",
            retrieval_profile={"active_profiles": ["conditions_and_exceptions"]},
        ),
    ]
    engine = AnswerEngine(Settings(), oracle=oracle, genai=FakeGenAi())  # type: ignore[arg-type]
    question = "登録できない場合の条件は？"

    result = await engine._search(
        SearchRequest(query=question),
        _SearchState(),
        retrieval_queries=[question],
        inquiry_conditions=_inquiry(question),
        settings=SimpleNamespace(profile_channel_enabled=profile_channel_enabled),
    )

    assert _order(result) == expected


async def test_oracle_document_classifications_reads_hit_documents_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分類はヒットした document_id の IN で 1 回だけ読み、分類の無い文書は返さない(#545)。"""
    from app.clients.oracle import OracleClient

    client = OracleClient(settings=Settings())
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_fetch_all(
        statement: str, binds: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        calls.append((statement, dict(binds or {})))
        return [
            {"document_id": "doc-a", "classification": '{"large_category": "業務A"}'},
            {"document_id": "doc-b", "classification": None},
        ]

    monkeypatch.setattr(client, "_fetch_all", fake_fetch_all)

    assert await client.document_classifications([]) == {}
    assert calls == []
    result = await client.document_classifications(["doc-a", "doc-b", "doc-a"])

    assert result == {"doc-a": {"large_category": "業務A"}}
    assert len(calls) == 1
    statement, binds = calls[0]
    assert "FROM rag_documents d" in statement
    document_binds = [
        value for key, value in binds.items() if key.startswith("classification_document")
    ]
    assert document_binds == ["doc-a", "doc-b"]


async def test_oracle_has_retrieval_chunks_uses_search_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文書名の存在確認は検索と同じ条件(KB・文書名)で 1 行だけ読む(#546)。"""
    from app.clients.oracle import OracleClient

    client = OracleClient(settings=Settings())
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_fetch_all(
        statement: str, binds: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        calls.append((statement, dict(binds or {})))
        return [] if len(calls) > 1 else [{"found": 1}]

    monkeypatch.setattr(client, "_fetch_all", fake_fetch_all)
    filters = {"knowledge_base_id": "kb-1", "file_name": "Manual.pdf"}

    assert await client.has_retrieval_chunks(filters) is True
    assert await client.has_retrieval_chunks(filters) is False
    statement, binds = calls[0]
    assert "ROWNUM = 1" in statement
    assert "LOWER(d.file_name) LIKE :filter_file_name" in statement
    assert binds["filter_file_name"] == "%manual.pdf%"
    assert "kb-1" in binds.values()


async def test_oracle_retrieval_large_categories_uses_search_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """大分類の語の一覧は、検索と同じ条件の文書の大分類を DISTINCT で 1 回だけ読む(#553)。"""
    from app.clients.oracle import OracleClient

    client = OracleClient(settings=Settings())
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_fetch_all(
        statement: str, binds: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        calls.append((statement, dict(binds or {})))
        return [{"large_category": "10_業務A"}, {"large_category": "業務B"}]

    monkeypatch.setattr(client, "_fetch_all", fake_fetch_all)

    result = await client.retrieval_large_categories({"knowledge_base_id": "kb-1"})

    assert result == ["10_業務A", "業務B"]
    assert len(calls) == 1
    statement, binds = calls[0]
    assert "SELECT DISTINCT" in statement
    assert "JSON_VALUE(d.classification, '$.large_category')" in statement
    assert "d.status = 'INDEXED'" in statement
    assert "kb-1" in binds.values()


# --- 画面目録の連携(#554) ---

_SCREEN_FILE = "setting.pdf"
_SCREEN_HEADING = "（２）帳票印字設定"


def _screen_chunk(chunk_id: str, text: str, section_path: list[str]) -> RetrievedChunk:
    chunk = _chunk(chunk_id, text, 1)
    metadata = json.loads(str(chunk.metadata["engine_metadata_json"]))
    metadata["section_path"] = section_path
    return chunk.model_copy(
        update={
            "document_id": "doc-setting",
            "file_name": _SCREEN_FILE,
            "metadata": {
                **chunk.metadata,
                "chunk_group_id": "setting-p1",
                "parent_text": text,
                "chunk_strategy": "small_to_big",
                "engine_search_text": f"Source file: {_SCREEN_FILE}\nChild text: {text}",
                "section_path": " > ".join(section_path),
                "chunk_set_id": "cs-setting",
                "engine_metadata_json": json.dumps(metadata, ensure_ascii=False),
            },
        }
    )


class ScreenOracle(FakeOracle):
    """検索では出ない設定画面を、検索範囲の目録と画面の chunk として返すスタブ(#554)。"""

    def __init__(self) -> None:
        super().__init__()
        self.state = "1:100"
        self.state_calls: list[dict[str, str]] = []
        self.section_calls: list[dict[str, str]] = []
        self.screen_calls: list[tuple[str, str]] = []
        self.screen_filters: list[dict[str, str]] = []
        self.screen_rows = [
            _screen_chunk(
                "setting:c1",
                "帳票の印字の有無は帳票印字設定で切り替えます。",
                ["設定", _SCREEN_HEADING],
            ),
            # 見出しの部分一致だけの chunk(別の画面)は加えない。
            _screen_chunk("setting:c2", "別画面の説明です。", ["設定", f"{_SCREEN_HEADING}の履歴"]),
        ]

    async def retrieval_scope_state(self, filters: dict[str, str]) -> str:
        self.state_calls.append(dict(filters))
        return self.state

    async def retrieval_screen_sections(
        self, filters: dict[str, str]
    ) -> list[tuple[str, str, int]]:
        self.section_calls.append(dict(filters))
        return [
            (_SCREEN_FILE, f"設定 > {_SCREEN_HEADING}", 3),
            ("manual.pdf", "受注入力画面", 2),
        ]

    async def retrieval_screen_chunks(
        self, filters: dict[str, str], *, file_name: str, heading: str, limit: int
    ) -> list[RetrievedChunk]:
        self.screen_calls.append((file_name, heading))
        self.screen_filters.append(dict(filters))
        return list(self.screen_rows)


def _screen_llm(calls: list[str]) -> Any:
    from rag_engine.retrieval.screen_catalog import LinkedScreen, ScreenLinks

    def parse(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
        calls.append(schema.__name__)
        if schema is ScreenLinks:
            assert _SCREEN_HEADING in prompt  # 目録に検索結果に無い画面が載る
            return ScreenLinks(
                candidates=[LinkedScreen(document=_SCREEN_FILE, screen=_SCREEN_HEADING)]
            )
        return _fake_llm(system, prompt, settings, schema, **options)

    return parse


async def _answer_with_screens(
    monkeypatch: pytest.MonkeyPatch,
    oracle: ScreenOracle,
    *,
    enabled: bool,
    auto_field_conditions: Sequence[Any] = (),
) -> tuple[Any, list[str]]:
    import rag_engine.adapters.model_api as engine_api
    from rag_engine.generation import answering

    from app.rag import answer_engine

    calls: list[str] = []
    monkeypatch.setattr(engine_api, "parse_text_response", _screen_llm(calls))
    monkeypatch.setattr(answer_engine, "_SCREEN_CATALOG_CACHE", {})
    answering._SCREEN_LINK_CACHE.clear()
    engine = AnswerEngine(
        Settings(
            rag_query_strategy="simple_retrieval",
            rag_answer_flow="standard_rag",
            rag_rerank_enabled=False,
            rag_screen_linking_enabled=enabled,
        ),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        auto_field_conditions=auto_field_conditions,
    )
    outcome = await engine.run(
        SearchRequest(query="帳票が印字されない", top_k=10, filters={"knowledge_base_id": "kb-1"})
    )
    return outcome, calls


def _evidence_ids(outcome: Any) -> set[str]:
    return {
        child["chunk_id"]
        for parent in outcome.diagnostics["evidence_tree"]
        for child in parent["children"]
    }


async def test_screen_linking_disabled_does_not_call_llm_or_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """画面目録の連携が無効(既定)なら、画面の選択の LLM も目録の SQL も呼ばない(#554)。"""
    oracle = ScreenOracle()

    outcome, calls = await _answer_with_screens(monkeypatch, oracle, enabled=False)

    assert "ScreenLinks" not in calls
    assert oracle.state_calls == [] and oracle.section_calls == [] and oracle.screen_calls == []
    assert "setting:c1" not in _evidence_ids(outcome)
    assert all(step["name"] != "画面の選択" for step in outcome.diagnostics["execution_steps"])


async def test_screen_linking_adds_screen_missing_from_search_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有効なら、検索で出なかった画面が範囲の目録に載り、その画面の chunk が候補に加わる(#554)。"""
    oracle = ScreenOracle()
    monkeypatch.setattr(answer_engine_module, "_today", lambda: date(2026, 4, 1))

    outcome, calls = await _answer_with_screens(monkeypatch, oracle, enabled=True)

    assert calls.count("ScreenLinks") == 1
    # 目録と画面の chunk は検索と同じ範囲(filters。回答の初めに決めた基準日を含む。#2602)で読む。
    assert oracle.section_calls == [{"knowledge_base_id": "kb-1", "as_of": "2026-04-01"}]
    assert oracle.screen_calls == [(_SCREEN_FILE, _SCREEN_HEADING)]
    evidence = _evidence_ids(outcome)
    assert "setting:c1" in evidence
    assert "setting:c2" not in evidence
    assert any(step["name"] == "画面の選択" for step in outcome.diagnostics["execution_steps"])


async def test_screen_linking_reads_catalog_with_the_auto_field_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """画面目録と画面の chunk は、質問から読み取った項目の条件を足した検索と同じ条件で読む(#2056)。

    要求の filters だけで読むと、条件で外した文書の画面が候補に加わる。
    """
    from app.schemas.search import ExtractionFieldCondition

    oracle = ScreenOracle()
    condition = ExtractionFieldCondition(
        name="発行日", value_type="date", op="gte", value="2026-01-01"
    )

    outcome, calls = await _answer_with_screens(
        monkeypatch, oracle, enabled=True, auto_field_conditions=[condition]
    )

    assert calls.count("ScreenLinks") == 1
    assert outcome.diagnostics["auto_field_filter"]["relaxed"] is False
    assert oracle.section_calls and oracle.state_calls and oracle.screen_filters
    assert "setting:c1" in _evidence_ids(outcome)
    for filters in [*oracle.section_calls, *oracle.state_calls, *oracle.screen_filters]:
        assert filters["knowledge_base_id"] == "kb-1"
        assert json.loads(filters["extraction_fields"]) == [condition.model_dump()]


async def test_screen_chunks_carry_classification_and_first_page_context() -> None:
    """画面目録から加える chunk にも分類と 1 ページ目の本文を載せる(1 回で読む。#554 / #557)。"""
    from app.rag.answer_engine import _SearchState

    oracle = ScreenOracle()
    oracle.classifications = {"doc-setting": {"large_category": "業務A"}}
    oracle.first_page_contexts = {"cs-setting": _first_page_context()}
    engine = AnswerEngine(Settings(), oracle=oracle, genai=FakeGenAi())  # type: ignore[arg-type]
    state = _SearchState()

    stored = await engine._screen_chunks(
        SearchRequest(query="q"), state, [(_SCREEN_FILE, _SCREEN_HEADING)], set()
    )

    documents = {chunk.chunk_uid: chunk.metadata.get("document") for chunk in stored}
    expected = {
        "classification": {"large_category": "業務A"},
        "first_page_context": _first_page_context(),
    }
    assert documents["setting:c1"] == expected
    assert documents["doc-setting:cs-setting:setting-p1"] == expected
    assert "setting:c2" not in documents
    assert oracle.classification_calls == [["doc-setting"]]
    assert oracle.first_page_calls == [["cs-setting"]]
    # 引用に戻せるよう、検索結果に無かった chunk も state に入れる。
    assert "setting:c1" in state.chunks


async def test_screen_catalog_is_cached_by_scope_and_index_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目録は(検索条件、索引の状態)ごとに cache し、再索引などで状態が変わったら作り直す(#554)。"""
    from app.rag.answer_engine import _SearchState

    monkeypatch.setattr("app.rag.answer_engine._SCREEN_CATALOG_CACHE", {})
    oracle = ScreenOracle()
    engine = AnswerEngine(Settings(), oracle=oracle, genai=FakeGenAi())  # type: ignore[arg-type]
    request = SearchRequest(query="q", filters={"knowledge_base_id": "kb-1"})

    first = await engine._screen_catalog(request, _SearchState())
    state = _SearchState()
    await engine._screen_catalog(request, state)
    await engine._screen_catalog(request, state)  # 同じ回答の 2 回目は状態も読まない
    oracle.state = "2:200"
    await engine._screen_catalog(request, _SearchState())

    assert first == {_SCREEN_FILE: [_SCREEN_HEADING]}
    assert len(oracle.state_calls) == 3
    assert len(oracle.section_calls) == 2


def test_build_engine_settings_passes_screen_linking(tmp_path: Any) -> None:
    """取込・検索・回答設定の画面目録の連携を rag_engine の設定へ渡す(#554)。"""
    from app.rag.answer_engine import build_engine_settings

    disabled = build_engine_settings(Settings(), output_dir=tmp_path)
    enabled = build_engine_settings(Settings(rag_screen_linking_enabled=True), output_dir=tmp_path)

    assert disabled.screen_linking_enabled is False
    assert enabled.screen_linking_enabled is True


def test_build_engine_settings_passes_llm_latency_settings(tmp_path: Any) -> None:
    """用途ごとの推論の量・CRAG の評価の省略・検索文の上限を rag_engine へ渡す(#1499)。"""
    from app.rag.answer_engine import build_engine_settings

    default = build_engine_settings(Settings(), output_dir=tmp_path)
    changed = build_engine_settings(
        Settings(
            rag_strategy_reasoning_effort="low",
            rag_judge_reasoning_effort="model",
            rag_generation_reasoning_effort="none",
            rag_crag_skip_on_strong_signals=True,
            rag_crag_skip_min_rerank_score=0.65,
            rag_query_expansion_max_queries=3,
        ),
        output_dir=tmp_path,
    )

    # 既定: 戦略はモデルの登録値(空)、根拠の評価は medium(#1550)、生成はモデルの登録値。
    assert (
        default.strategy_reasoning_effort,
        default.judge_reasoning_effort,
        default.generation_reasoning_effort,
    ) == ("", "medium", "")
    assert default.crag_skip_on_strong_signals is False
    assert default.query_expansion_max_queries == 5
    # model はモデルの登録値のまま(空)、none は送らない。
    assert (
        changed.strategy_reasoning_effort,
        changed.judge_reasoning_effort,
        changed.generation_reasoning_effort,
    ) == ("low", "", "none")
    assert changed.crag_skip_on_strong_signals is True
    assert changed.crag_skip_min_rerank_score == 0.65
    assert changed.query_expansion_max_queries == 3


def test_build_engine_settings_passes_judge_latency_settings(tmp_path: Any) -> None:
    """監査の推論の量・CRAG の評価の入力の圧縮・評価し直しの省略を渡す(#1528)。"""
    from app.rag.answer_engine import build_engine_settings

    default = build_engine_settings(Settings(), output_dir=tmp_path)
    changed = build_engine_settings(
        Settings(
            rag_audit_reasoning_effort="high",
            rag_crag_compact_input=False,
            rag_section_skip_regrade_at_scope=False,
        ),
        output_dir=tmp_path,
    )
    omitted = build_engine_settings(
        Settings(rag_audit_reasoning_effort="none"), output_dir=tmp_path
    )

    assert default.audit_reasoning_effort == _engine_effort(Settings().rag_audit_reasoning_effort)
    assert default.crag_compact_input is Settings().rag_crag_compact_input
    assert default.section_skip_regrade_at_scope is Settings().rag_section_skip_regrade_at_scope
    assert changed.audit_reasoning_effort == "high"
    assert changed.crag_compact_input is False and changed.section_skip_regrade_at_scope is False
    assert omitted.audit_reasoning_effort == "none"
    model = build_engine_settings(
        Settings(
            rag_audit_reasoning_effort="model",
            rag_crag_compact_input=True,
            rag_section_skip_regrade_at_scope=True,
        ),
        output_dir=tmp_path,
    )
    assert model.audit_reasoning_effort == ""
    assert model.crag_compact_input is True and model.section_skip_regrade_at_scope is True


def test_build_engine_settings_passes_short_candidate_ids(tmp_path: Any) -> None:
    """CRAG の評価の候補の短い ID(#1864。既定 ON)を rag_engine に渡す。"""
    from app.rag.answer_engine import build_engine_settings

    assert Settings().rag_crag_short_candidate_ids is True
    default = build_engine_settings(Settings(), output_dir=tmp_path)
    off = build_engine_settings(Settings(rag_crag_short_candidate_ids=False), output_dir=tmp_path)
    assert default.crag_short_candidate_ids is True
    assert off.crag_short_candidate_ids is False


def test_build_engine_settings_passes_answer_round_latency(tmp_path: Any) -> None:
    """監査の通った判定の reason を空にする(既定 OFF)・是正の組の回数の上限(既定 2)を
    rag_engine に渡す(#1912)。"""
    from app.rag.answer_engine import build_engine_settings

    assert Settings().rag_answer_audit_compact_reasons is False
    assert Settings().rag_answer_max_correction_rounds == 2
    default = build_engine_settings(Settings(), output_dir=tmp_path)
    changed = build_engine_settings(
        Settings(rag_answer_audit_compact_reasons=True, rag_answer_max_correction_rounds=1),
        output_dir=tmp_path,
    )
    assert default.answer_audit_compact_reasons is False
    assert default.answer_max_correction_rounds == 2
    assert changed.answer_audit_compact_reasons is True
    assert changed.answer_max_correction_rounds == 1


def test_build_engine_settings_passes_llm_stall_detection(tmp_path: Any) -> None:
    """停滞した LLM の呼び出しの打ち切りと送り直し(既定 ON・20 秒・0.25・1 回)を
    rag_engine に渡す(#1926)。"""
    from app.rag.answer_engine import build_engine_settings

    default = build_engine_settings(Settings(), output_dir=tmp_path)
    assert default.llm_stall_detection is True
    assert default.llm_stall_idle_seconds == 20.0
    assert default.llm_stall_min_rate_ratio == 0.25
    assert default.llm_stall_resends == 1
    changed = build_engine_settings(
        Settings(
            rag_llm_stall_detection_enabled=False,
            rag_llm_stall_idle_seconds=45,
            rag_llm_stall_min_rate_ratio=0.5,
            rag_llm_stall_resends=2,
        ),
        output_dir=tmp_path,
    )
    assert changed.llm_stall_detection is False
    assert changed.llm_stall_idle_seconds == 45.0
    assert changed.llm_stall_min_rate_ratio == 0.5
    assert changed.llm_stall_resends == 2


def _engine_effort(value: str) -> str:
    return "" if value == "model" else value


async def test_prefetch_warms_search_cache_without_changing_results() -> None:
    """元の質問の先読み(#1499)は検索の cache に入れるだけで、続く検索の結果を変えない。"""
    from app.rag.answer_engine import _SearchState

    question = "受注入力画面の使い方は？"
    queries = [question, "受注の登録手順"]

    async def search(prefetch: bool) -> tuple[Any, QueryRecordingOracle, list[str]]:
        oracle = QueryRecordingOracle()
        engine = AnswerEngine(Settings(), oracle=oracle, genai=FakeGenAi())  # type: ignore[arg-type]
        state = _SearchState()
        request = SearchRequest(query=question, filters={"knowledge_base_id": "kb-1"})
        if prefetch:
            await engine._prefetch_query_hits(
                request,
                state,
                query=question,
                candidate_limit=24,
                inquiry_conditions=_inquiry(question),
            )
        prefetched = list(oracle.queries)
        result = await engine._search(
            request,
            state,
            retrieval_queries=queries,
            candidate_limit=24,
            inquiry_conditions=_inquiry(question),
        )
        return result, oracle, prefetched

    plain, plain_oracle, _ = await search(False)
    warmed, warmed_oracle, prefetched = await search(True)

    assert prefetched == [question]
    # 続く検索は元の質問を読み直さず、残りの検索文だけを読む。
    assert warmed_oracle.queries == plain_oracle.queries == queries
    assert warmed_oracle.filters == plain_oracle.filters
    assert warmed == plain


def test_search_answer_profile_ignores_legacy_screen_linking() -> None:
    """画面目録は全体の設定を使い、旧個別値を適用しない。"""
    from app.rag.kb_adapter_config import KnowledgeBaseQueryConfig
    from app.rag.search_answer_profile_config import (
        SearchAnswerProfileConfig,
        resolve_search_answer_profile_settings,
    )

    config = SearchAnswerProfileConfig(
        knowledge_base_ids=["kb-1"],
        query=KnowledgeBaseQueryConfig(screen_linking_enabled=True),
    )

    settings, _ = resolve_search_answer_profile_settings(Settings(), config)

    assert settings.rag_screen_linking_enabled is False
    assert Settings().rag_screen_linking_enabled is False


async def test_oracle_screen_catalog_queries_use_search_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目録・状態・画面の chunk の SQL は、検索と同じ条件(KB など)で読む(#554)。"""
    from app.clients.oracle import OracleClient

    client = OracleClient(settings=Settings())
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_fetch_all(
        statement: str, binds: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        calls.append((statement, dict(binds or {})))
        if "GROUP BY" in statement:
            return [{"file_name": "a.pdf", "section_path": "設定 > （１）画面", "chunk_count": 2}]
        if "chunk_count" in statement:
            return [{"chunk_count": 5, "chunk_hash": 123}]
        return []

    monkeypatch.setattr(client, "_fetch_all", fake_fetch_all)
    filters = {"knowledge_base_id": "kb-1"}

    assert await client.retrieval_scope_state(filters) == "5:123"
    assert await client.retrieval_screen_sections(filters) == [("a.pdf", "設定 > （１）画面", 2)]
    assert (
        await client.retrieval_screen_chunks(
            filters, file_name="a.pdf", heading="（１）画面", limit=7
        )
        == []
    )
    assert len(calls) == 3
    for _statement, binds in calls:
        assert "kb-1" in binds.values()
    assert "GROUP BY d.file_name" in calls[1][0]
    screen_sql, screen_binds = calls[2]
    assert "d.file_name = :screen_file_name" in screen_sql
    assert screen_binds["screen_heading"] == "（１）画面"
    assert screen_binds["screen_limit"] == 7


def test_build_engine_settings_uses_answer_model_id(tmp_path: Any) -> None:
    """回答のモデルを渡すと、rag_engine の回答のモデルがそのモデルになる(モデル比較。#593)。"""
    from rag_engine.config import ENTERPRISE_AI_LLM_PROVIDER

    from app.config import enterprise_ai_default_model_id
    from app.rag.answer_engine import build_engine_settings

    settings = Settings(
        enterprise_ai_models=[EnterpriseAiConfiguredModel(model_id="model-b")],
    )
    default = build_engine_settings(settings, output_dir=tmp_path)
    chosen = build_engine_settings(settings, output_dir=tmp_path, answer_model_id="model-b")

    assert default.llm_providers[ENTERPRISE_AI_LLM_PROVIDER].model == (
        enterprise_ai_default_model_id(settings)
    )
    assert chosen.llm_providers[ENTERPRISE_AI_LLM_PROVIDER].model == "model-b"


async def test_pipeline_passes_answer_model_id_to_answer_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """チャットの比較の列のモデルが、回答フローの設定まで届く(#593)。"""
    import rag_engine.adapters.model_api as engine_api

    import app.rag.answer_engine as answer_engine
    from app.rag.pipeline import RagPipeline

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    seen: list[str | None] = []
    original = answer_engine.build_engine_settings

    def capture(settings: Settings, **kwargs: Any) -> Any:
        seen.append(kwargs.get("answer_model_id"))
        return original(settings, **kwargs)

    monkeypatch.setattr(answer_engine, "build_engine_settings", capture)
    settings = Settings(enterprise_ai_models=[EnterpriseAiConfiguredModel(model_id="model-a")])
    for model_id in ("model-a", None):
        pipeline = RagPipeline(
            settings=settings,
            oracle=FakeOracle(),  # type: ignore[arg-type]
            genai=FakeGenAi(),  # type: ignore[arg-type]
            answer_model_id=model_id,
        )
        response = await pipeline.run(SearchRequest(query="受注の登録方法は？"))
        assert "登録ボタン" in response.answer

    assert seen == ["model-a", None]


class NoRerankGenAi(FakeGenAi):
    async def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        raise AssertionError("検索だけの経路は rerank を呼ばない")


def _no_llm(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("検索だけの経路は LLM を呼ばない")


async def test_retrieval_only_returns_candidates_without_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """retrieval_only は回答フローの検索だけを行い、LLM・rerank・回答の保存を使わない(#593)。"""
    import rag_engine.adapters.model_api as engine_api

    import app.rag.pipeline as pipeline_module

    monkeypatch.setattr(engine_api, "parse_text_response", _no_llm)
    audits: list[dict[str, Any]] = []
    monkeypatch.setattr(
        pipeline_module, "record_rag_search_audit", lambda **kwargs: audits.append(kwargs)
    )
    oracle = SavingOracle()
    llm = RewriteLlm(AssertionError("検索だけの経路は LLM を呼ばない"))
    pipeline = pipeline_module.RagPipeline(
        settings=Settings(),
        oracle=oracle,  # type: ignore[arg-type]
        genai=NoRerankGenAi(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
    )
    observed: list[tuple[str, str]] = []

    async def capture(progress: Any) -> None:
        observed.append((progress.stage, progress.outcome))

    response = await pipeline.run(
        SearchRequest(
            query="受注の登録方法は？",
            top_k=5,
            retrieval_only=True,
            filters={"document_id": "doc-1", "chunk_set_id": "cs-1"},
        ),
        progress_callback=capture,
    )

    assert response.answer == ""
    assert [chunk.chunk_id for chunk in response.citations] == ["doc-1:c1"]
    assert observed == [("retrieval", "started"), ("retrieval", "success")]
    assert response.diagnostics.retrieval_strategy_adapter == "retrieval_only"
    # 画面で指定した chunk set(レシピ)の条件は、そのまま Oracle の検索条件に渡る。
    assert oracle.filters
    assert all(
        item.get("chunk_set_id") == "cs-1" and item.get("document_id") == "doc-1"
        for item in oracle.filters
    )
    assert oracle.saved == []
    assert llm.calls == []
    assert len(audits) == 1
    assert audits[0]["outcome"] == "success"


async def _run_with_progress(
    monkeypatch: pytest.MonkeyPatch,
    request: SearchRequest,
    *,
    answer_flow: str,
    history: list[Any] | None = None,
    complete_evidence: bool = True,
) -> tuple[Any, list[tuple[str, str]], list[str], SavingOracle]:
    """同じ stub の依存で pipeline を 1 回実行し、工程の開始・終了の列と LLM の schema を返す。"""
    import rag_engine.adapters.model_api as engine_api

    import app.rag.pipeline as pipeline_module

    schemas: list[str] = []

    def record_llm(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
        schemas.append(schema.__name__)
        return _fake_llm(system, prompt, settings, schema, **options)

    monkeypatch.setattr(engine_api, "parse_text_response", record_llm)
    oracle = SavingOracle()
    pipeline = pipeline_module.RagPipeline(
        settings=Settings(rag_query_strategy="auto_routing", rag_answer_flow=answer_flow),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        complete_evidence=complete_evidence,
    )
    observed: list[tuple[str, str]] = []

    async def capture(progress: Any) -> None:
        observed.append((progress.stage, progress.outcome))

    response = await pipeline.run(request, progress_callback=capture, history=history)
    return response, observed, schemas, oracle


@pytest.mark.parametrize("answer_flow", ["standard_rag", "crag"])
async def test_search_without_answer_runs_the_chat_evidence_flow(
    monkeypatch: pytest.MonkeyPatch, answer_flow: str
) -> None:
    """RAG 検索(回答オフ)はチャットと同じ工程・入れ子・LLM で根拠を集める(#3506)。

    回答の生成の手前で止める。チャット(会話の最初の質問。履歴は空)と RAG 検索を同じ stub の依存で
    実行し、根拠を集め終えるまでの工程の列(名前と開始・終了の順 = 入れ子)・LLM の呼び出し・
    根拠の並びが同じことを確かめる。
    """
    chat, chat_steps, chat_llm, _ = await _run_with_progress(
        monkeypatch, SearchRequest(query="受注の登録方法は？"), answer_flow=answer_flow, history=[]
    )
    search, search_steps, search_llm, oracle = await _run_with_progress(
        monkeypatch,
        SearchRequest(query="受注の登録方法は？", generate_answer=False),
        answer_flow=answer_flow,
    )

    # チャットの工程を、回答に添付する画像（回答の生成の最初の工程）の手前で切る。
    cut = chat_steps.index(("answer_step:回答に添付する画像", "started"))
    rename = {
        "answer": "evidence",
        "answer_step:回答生成フロー": "answer_step:根拠検索フロー",
    }
    expected = [(rename.get(stage, stage), outcome) for stage, outcome in chat_steps[:cut]]
    expected += [
        ("answer_step:根拠検索フロー", "success"),
        ("evidence", "success"),
    ]
    assert search_steps == expected
    assert ("answer_step:文書検索", "started") in search_steps or (
        "answer_step:文書検索（1回目）",
        "started",
    ) in search_steps
    # 根拠を集める LLM（質問拡張・CRAG の根拠確認）は同じ。回答の生成と監査だけがチャットに
    # 足される。
    assert search_llm == chat_llm[: len(search_llm)]
    assert not {"GroundedDraft", "GroundedDraftWire"}.intersection(search_llm)
    if answer_flow == "crag":
        assert "CragRetrievalGradeOutput" in search_llm
    # 根拠も同じ。回答は引用した根拠を先頭に出すので、それ以外の根拠の並びを比べる。
    assert search.answer == ""
    assert {c.chunk_id for c in search.citations} == {c.chunk_id for c in chat.citations}
    used = {c.chunk_id for c in chat.citations if c.metadata.get("evidence_model_used")}
    assert [c.chunk_id for c in search.citations if c.chunk_id not in used] == [
        c.chunk_id for c in chat.citations if c.chunk_id not in used
    ]
    assert not any(c.metadata.get("evidence_model_used") for c in search.citations)
    search_answer = search.diagnostics.answer or {}
    assert search_answer["answer_flow"] == (chat.diagnostics.answer or {})["answer_flow"]
    # 回答が無いので回答の記録は保存しない。
    assert oracle.saved == []


async def test_evidence_retrieval_for_agent_stops_after_document_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """業務 Agent の根拠の取得(complete_evidence=False)は主の文書の検索の後で止める(#3506)。

    CRAG も使わない。
    """
    response, steps, schemas, oracle = await _run_with_progress(
        monkeypatch,
        SearchRequest(query="受注の登録方法は？", generate_answer=False),
        answer_flow="crag",
        complete_evidence=False,
    )

    assert response.answer == ""
    assert response.citations
    assert schemas == ["QueryRoutingOutput"]
    started = [stage for stage, outcome in steps if outcome == "started"]
    assert started[0] == "evidence"
    flow = started.index("answer_step:根拠検索フロー")
    assert started[flow + 1] == "answer_step:文書検索"
    # 根拠の補い（章節の拡張・要求ごとの根拠の確認）はしない（Agent が MCP でする）。
    assert "answer_step:要求ごとの根拠の確認" not in started
    assert "answer_step:根拠確認（1回目）" not in started
    answer = response.diagnostics.answer or {}
    assert answer["answer_flow"] == "standard_rag"
    models = cast(dict[str, Any], answer["models"])
    assert set(models) == {"llm", "attached_images", "embedding", "rerank"}
    assert oracle.saved == []


async def test_answer_diagnostics_include_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """回答の診断に、使った LLM(表示名)・embedding・rerank のモデルが入る(#649)。"""
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    settings = Settings(
        enterprise_ai_models=[
            EnterpriseAiConfiguredModel(model_id="model-a", display_name="Model A"),
        ],
        rag_rerank_enabled=False,
    )
    engine = AnswerEngine(
        settings,
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        answer_model_id="model-a",
    )
    outcome = await engine.run(SearchRequest(query="受注の登録方法は？"))
    assert outcome.diagnostics["models"] == {
        "llm": {"model_id": "model-a", "label": "Model A"},
        "attached_images": 0,
        "embedding": settings.genai_embedding_model,
        "rerank": "",
    }


class EmptyWithFieldFilterOracle(FakeOracle):
    """抽出項目の条件があると 0 件、無ければ候補を返す(読み取った条件が厳しすぎる状況)。"""

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        self.filters.append(dict(filters or {}))
        if (filters or {}).get("extraction_fields"):
            return []
        return self.chunks[:1]


async def test_answer_engine_applies_auto_field_conditions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """質問から読み取った条件を検索条件に足し、診断に出す(#652)。"""
    import rag_engine.adapters.model_api as engine_api

    from app.schemas.search import ExtractionFieldCondition

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    oracle = FakeOracle()
    condition = ExtractionFieldCondition(name="金額", value_type="number", op="gte", value="100000")
    engine = AnswerEngine(
        Settings(),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        auto_field_conditions=[condition],
    )
    outcome = await engine.run(SearchRequest(query="受注の登録方法は？"))

    assert oracle.filters
    assert all(
        json.loads(item["extraction_fields"])[0]["name"] == "金額" for item in oracle.filters
    )
    assert outcome.diagnostics["auto_field_filter"] == {
        "conditions": [condition.model_dump()],
        "relaxed": False,
    }


async def test_answer_engine_relaxes_auto_field_conditions_without_hits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """読み取った条件で 0 件なら、条件を外して 1 回だけ検索し直す(手の条件は外さない。#652)。"""
    import rag_engine.adapters.model_api as engine_api

    from app.schemas.search import ExtractionFieldCondition

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    oracle = EmptyWithFieldFilterOracle()
    engine = AnswerEngine(
        Settings(),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        auto_field_conditions=[
            ExtractionFieldCondition(name="金額", value_type="number", op="gte", value="1")
        ],
    )
    outcome = await engine.run(SearchRequest(query="受注の登録方法は？"))

    assert "extraction_fields" in oracle.filters[0]
    assert "extraction_fields" not in oracle.filters[-1]
    assert outcome.citations
    assert outcome.diagnostics["auto_field_filter"]["relaxed"] is True
    assert outcome.diagnostics["filter_relaxation"]["relaxed"] == ["auto_field"]


class EmptyWithKeysOracle(FakeOracle):
    """``blocked`` のどれかの項目が検索条件にあると 0 件、無ければ候補を返す(#2401)。

    質問から推測した条件(ページ・文書名・抽出項目)が文書に無い値で、候補が 0 件になる形の
    合成の代役。
    """

    def __init__(self, *blocked: str) -> None:
        super().__init__()
        self.blocked = set(blocked)

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        self.filters.append(dict(filters or {}))
        if self.blocked & set(filters or {}):
            return []
        return self.chunks[:1]


async def _search_once(
    oracle: FakeOracle, question: str, *, settings: Settings | None = None, conditions: Any = ()
) -> Any:
    from app.rag.answer_engine import _SearchState

    engine = AnswerEngine(
        settings or Settings(),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        auto_field_conditions=list(conditions),
    )
    state = _SearchState()
    await engine._search(
        SearchRequest(query=question, filters={"knowledge_base_id": "kb-1"}),
        state,
        retrieval_queries=[question],
        inquiry_conditions=_inquiry(question),
    )
    return state


@pytest.mark.parametrize(
    ("blocked", "relaxed", "last_filters"),
    [
        (
            ("page_number_min",),
            ["question_page"],
            {"knowledge_base_id": "kb-1", "file_name": "manual.pdf"},
        ),
        (
            ("page_number_min", "file_name"),
            ["question_page", "question_file"],
            {"knowledge_base_id": "kb-1"},
        ),
    ],
    ids=["page", "page-and-file"],
)
async def test_question_filters_are_relaxed_in_order_without_hits(
    blocked: tuple[str, ...], relaxed: list[str], last_filters: dict[str, str]
) -> None:
    """質問が名指ししたページ・文書名で 0 件なら、ページ → 文書名の順に外して検索し直す(#2401)。

    利用者が指定した範囲(knowledge_base_id)は外さない。
    """
    oracle = EmptyWithKeysOracle(*blocked)

    state = await _search_once(oracle, "manual.pdf の12ページの登録手順は？")

    assert oracle.filters[0]["page_number_min"] == "12"
    assert oracle.filters[-1] == last_filters
    assert state.relaxed == relaxed
    # 外さずに残った自動の条件(ページだけで 0 件なら、文書名は効いたまま)。
    active = ["question_file"] if relaxed == ["question_page"] else []
    assert state.active_relaxations() == active
    assert state.search_log == [
        {"queries": 1, "candidates": 1, "automatic_filters": active, "relaxed": relaxed}
    ]


async def test_filter_relaxation_order_starts_from_auto_field_conditions() -> None:
    """抽出項目の条件 → ページ → 文書名の順に外す(#2401)。"""
    from app.schemas.search import ExtractionFieldCondition

    oracle = EmptyWithKeysOracle("extraction_fields", "page_number_min", "file_name")
    state = await _search_once(
        oracle,
        "manual.pdf の12ページの登録手順は？",
        conditions=[
            ExtractionFieldCondition(name="金額", value_type="number", op="gte", value="1")
        ],
    )

    assert state.relaxed == ["auto_field", "question_page", "question_file"]
    assert [sorted(item) for item in oracle.filters] == [
        [
            "extraction_fields",
            "file_name",
            "knowledge_base_id",
            "page_number_max",
            "page_number_min",
        ],
        ["file_name", "knowledge_base_id", "page_number_max", "page_number_min"],
        ["file_name", "knowledge_base_id"],
        ["knowledge_base_id"],
    ]


async def test_filter_relaxation_respects_max_steps() -> None:
    """外す段の数は設定の上限まで。0 なら外さずに 0 件のまま(#2401)。"""
    oracle = EmptyWithKeysOracle("page_number_min", "file_name")

    limited = await _search_once(
        oracle,
        "manual.pdf の12ページの登録手順は？",
        settings=Settings(rag_filter_relaxation_max_steps=1),
    )
    assert limited.relaxed == ["question_page"]
    assert limited.search_log[0]["candidates"] == 0
    assert limited.active_relaxations() == ["question_file"]

    oracle = EmptyWithKeysOracle("page_number_min")
    disabled = await _search_once(
        oracle,
        "manual.pdf の12ページの登録手順は？",
        settings=Settings(rag_filter_relaxation_max_steps=0),
    )
    assert disabled.relaxed == []
    assert len(oracle.filters) == 1
    assert disabled.search_log[0] == {
        "queries": 1,
        "candidates": 0,
        "automatic_filters": ["question_page", "question_file"],
        "relaxed": [],
    }


@pytest.mark.parametrize(
    ("oracle", "settings", "stage", "automatic_filters"),
    [
        (EmptyWithKeysOracle("knowledge_base_id"), Settings(), "retrieval", []),
        (
            EmptyWithKeysOracle("page_number_min"),
            Settings(rag_filter_relaxation_max_steps=0),
            "filter",
            ["question_page", "question_file"],
        ),
    ],
    ids=["no-candidates-in-scope", "automatic-filter-kept"],
)
async def test_zero_evidence_diagnostics_record_the_stage(
    monkeypatch: pytest.MonkeyPatch,
    oracle: FakeOracle,
    settings: Settings,
    stage: str,
    automatic_filters: list[str],
) -> None:
    """根拠 0 件で終えたとき、0 件になった段と検索ごとの候補の件数を診断に残す(#2401)。

    検索の条件は項目の名前だけを残し、値(質問・文書の内容を含みうる)は出さない。
    """
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    engine = AnswerEngine(
        settings,
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )
    outcome = await engine.run(
        SearchRequest(
            query="manual.pdf の12ページの登録手順は？", filters={"knowledge_base_id": "kb-1"}
        )
    )

    assert outcome.citations == []
    zero = outcome.diagnostics["zero_evidence"]
    assert zero["stage"] == stage
    assert zero["candidate_count"] == 0
    assert zero["automatic_filters"] == automatic_filters
    assert zero["request_filter_keys"] == ["knowledge_base_id"]
    assert zero["searches"]
    assert all(search["candidates"] == 0 for search in zero["searches"])
    assert "manual.pdf" not in json.dumps(zero, ensure_ascii=False)
    # 工程の詳細(回答生成フロー)にも段を出す。
    flow = next(
        step for step in outcome.diagnostics["execution_steps"] if step["name"] == "回答生成フロー"
    )
    assert "根拠が 0 件になった段" in json.dumps(flow, ensure_ascii=False)


async def test_pipeline_reads_field_conditions_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """設定が有効なときだけ、検索・回答プロファイルの項目の定義で質問から条件を読み取る(#652)。"""
    import rag_engine.adapters.model_api as engine_api

    import app.rag.pipeline as pipeline_module
    from app.rag.extraction_field_adapter import FieldDefinition

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)

    class ViewOracle(SavingOracle):
        async def get_search_answer_profile(self, search_answer_profile_id: str) -> Any:
            from types import SimpleNamespace

            return SimpleNamespace(
                config=SimpleNamespace(normalized_knowledge_base_ids=lambda: ["kb-1"])
            )

        async def list_knowledge_base_extraction_field_sets(self, ids: list[str]) -> Any:
            return [[FieldDefinition(name="金額", value_type="number")]]

    monkeypatch.setattr(pipeline_module, "resolve_field_definitions", lambda sets, default: sets[0])
    for enabled in (False, True):
        llm = RewriteLlm('[{"name": "金額", "op": "gte", "value": "100000"}]')
        oracle = ViewOracle()
        stages: list[str] = []

        async def capture(progress: Any, stages: list[str] = stages) -> None:
            stages.append(progress.stage)

        pipeline = pipeline_module.RagPipeline(
            settings=Settings(rag_auto_field_filter_enabled=enabled),
            oracle=oracle,  # type: ignore[arg-type]
            genai=FakeGenAi(),  # type: ignore[arg-type]
            llm=llm,  # type: ignore[arg-type]
        )
        response = await pipeline.run(
            SearchRequest(query="10万円以上の受注は？", search_answer_profile_id="bv-1"),
            progress_callback=capture,
        )
        answer = cast(dict[str, Any], response.diagnostics.answer or {})
        if enabled:
            assert len(llm.calls) == 1
            assert "field_filter" in stages
            assert answer["auto_field_filter"]["conditions"][0]["value"] == "100000"
        else:
            assert llm.calls == []
            assert "field_filter" not in stages
            assert "auto_field_filter" not in answer

    # 画面で外した項目は読み取らない(項目の定義が残らなければ LLM を呼ばない)。
    llm = RewriteLlm('[{"name": "金額", "op": "gte", "value": "100000"}]')
    pipeline = pipeline_module.RagPipeline(
        settings=Settings(rag_auto_field_filter_enabled=True),
        oracle=ViewOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
    )
    response = await pipeline.run(
        SearchRequest(
            query="10万円以上の受注は？",
            search_answer_profile_id="bv-1",
            auto_field_filter_excluded=["金額"],
        )
    )
    assert llm.calls == []
    assert "auto_field_filter" not in (response.diagnostics.answer or {})


async def test_answer_engine_answers_only_from_selected_approved_faq(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """類似問を選んだら検索せず、質問・類似問・承認済みの回答だけを LLM に渡す(#702)。"""
    import json

    import rag_engine.adapters.model_api as engine_api
    import rag_engine.generation.answering as answering
    from rag_engine.generation.faq_answer import FaqAnswerDraft

    prompts: list[dict[str, Any]] = []

    def fake_llm(system: str, prompt: str, settings: Any, schema: type, **_: Any) -> Any:
        assert schema is FaqAnswerDraft
        prompts.append(json.loads(prompt))
        return FaqAnswerDraft(answer="宿泊出張の日当は 1 泊 2,000 円です。")

    def no_search(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("類似問を選んだときは検索しない")

    monkeypatch.setattr(engine_api, "parse_text_response", fake_llm)
    monkeypatch.setattr(answering, "answer_question_result", no_search)
    oracle = FakeOracle()
    engine = AnswerEngine(
        Settings(),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        approved_faq=("出張の日当はいくらですか？", "一般は 1 泊 2,000 円です。"),
    )

    events: list[tuple[str, str, float, object]] = []

    async def step_callback(name: str, status: str, elapsed: float, record: object) -> None:
        events.append((name, status, elapsed, record))

    outcome = await engine.run(
        SearchRequest(query="出張したら日当は何円？"), step_callback=step_callback
    )

    assert prompts == [
        {
            "question": "出張したら日当は何円？",
            "approved_faq": {
                "question": "出張の日当はいくらですか？",
                "answer": "一般は 1 泊 2,000 円です。",
            },
        }
    ]
    assert outcome.answer.startswith("宿泊出張の日当は 1 泊 2,000 円です。")
    assert "承認済み FAQ「出張の日当はいくらですか？」" in outcome.answer
    assert outcome.citations == []
    assert outcome.diagnostics["answer_source"] == "approved_faq"
    # 根拠として出す FAQ の原文(回答した時点のもの。#737)。
    assert outcome.diagnostics["approved_faq_question"] == "出張の日当はいくらですか？"
    assert outcome.diagnostics["approved_faq_answer"] == "一般は 1 泊 2,000 円です。"
    assert not oracle.filters
    assert outcome.diagnostics["models"]["embedding"] == ""
    assert outcome.diagnostics["models"]["rerank"] == ""
    steps = outcome.diagnostics["execution_steps"]
    assert steps[0]["name"] == "承認済み FAQ からの回答"
    assert steps[0]["llm_calls"] == 1
    assert steps[0]["elapsed_seconds"] >= 0
    assert [event[1] for event in events] == ["started", "success"]


async def test_answer_engine_passes_the_clarification_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """確認で選んだ条件は回答の前提に渡し、回答の末尾と診断に対象の範囲を示す(#717)。"""
    import rag_engine.adapters.model_api as engine_api
    import rag_engine.generation.answering as answering

    from app.rag.answer_engine import AnswerScope

    captured: dict[str, Any] = {}
    original = answering.answer_question_result

    def spy(*args: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    monkeypatch.setattr(answering, "answer_question_result", spy)
    engine = AnswerEngine(
        Settings(rag_domain_keywords=["受注番号"]),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        scope=AnswerScope(
            context="選んだ答え: 受注",
            search_terms=("受注登録",),
            label="「受注手順」の「登録」（p.2）",
        ),
    )
    request = SearchRequest(
        query="受注の登録方法は？",
        filters={"page_ranges": '[{"document_id":"doc-1","page_start":2,"page_end":2}]'},
    )

    outcome = await engine.run(request)

    assert captured["scope"] == ("選んだ答え: 受注", ("受注登録",))
    assert outcome.answer.endswith("（対象: 「受注手順」の「登録」（p.2））")
    assert outcome.diagnostics["scope"]["label"] == "「受注手順」の「登録」（p.2）"


async def test_answer_diagnostics_record_profile_and_prompt_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回答の記録に検索・回答プロファイルの版とプロンプトの版を残す(#1276)。"""
    import rag_engine.adapters.model_api as engine_api

    from app.rag.answer_provenance import answer_prompt_version
    from app.rag.pipeline import RagPipeline

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    revision = {"id": "sap-1", "updated_at": "2026-10-01T00:00:00+00:00", "config_sha256": "abc"}
    pipeline = RagPipeline(
        settings=Settings(rag_search_answer_profile_revision=revision),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )
    response = await pipeline.run(SearchRequest(query="受注の登録方法は？"))
    answer = response.diagnostics.answer
    assert answer is not None
    # FakeOracle のプロンプトは未編集なので、既定のプロンプトの版になる。
    assert answer["provenance"] == {
        "prompt_version": answer_prompt_version({}),
        "search_answer_profile": revision,
    }

    # 検索・回答プロファイルを使わない検索は、プロファイルの版を残さない。
    plain = RagPipeline(
        settings=Settings(),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )
    plain_answer = (await plain.run(SearchRequest(query="受注の登録方法は？"))).diagnostics.answer
    assert plain_answer is not None
    assert plain_answer["provenance"] == {"prompt_version": answer_prompt_version({})}


def test_prompt_and_profile_versions_follow_content() -> None:
    from datetime import UTC, datetime

    from rag_engine.knowledge.prompt_files import VLM_ANSWER_PROMPT_KEY

    from app.rag.answer_prompts import default_prompt
    from app.rag.answer_provenance import answer_prompt_version, search_answer_profile_revision
    from app.rag.search_answer_profile_config import SearchAnswerProfileConfig

    default = answer_prompt_version({})
    assert default.startswith("prompt-") and len(default) == len("prompt-") + 16
    # 既定と同じ内容の上書きは同じ版、内容を変えると別の版。
    same = {VLM_ANSWER_PROMPT_KEY: default_prompt(VLM_ANSWER_PROMPT_KEY)}
    assert answer_prompt_version(same) == default
    edited = {VLM_ANSWER_PROMPT_KEY: default_prompt(VLM_ANSWER_PROMPT_KEY) + "\n追記"}
    assert answer_prompt_version(edited) != default

    def view(config: SearchAnswerProfileConfig) -> Any:
        updated = datetime(2026, 10, 1, tzinfo=UTC)
        return type("View", (), {"id": "sap-1", "updated_at": updated, "config": config})()

    first = search_answer_profile_revision(view(SearchAnswerProfileConfig()))
    assert first["id"] == "sap-1"
    assert first["updated_at"] == "2026-10-01T00:00:00+00:00"
    assert first == search_answer_profile_revision(view(SearchAnswerProfileConfig()))
    changed = SearchAnswerProfileConfig(knowledge_base_ids=["kb-1"])
    assert search_answer_profile_revision(view(changed))["config_sha256"] != first["config_sha256"]


# --- 要求ごとの根拠の覆域と、根拠の無い要求だけの再検索(#1279) ---

_INVOICE_TEXT = "請求書の再発行は、請求一覧で対象を選び、再発行ボタンを押します。"


class CoverageOracle(FakeOracle):
    """要求「請求書の再発行」だけの文で検索したときだけ請求書の資料を返す検索のスタブ。"""

    def __init__(self) -> None:
        super().__init__()
        self.queries: list[str] = []
        self.order = _doc_chunk("doc-order", "受注番号を入力し、登録ボタンを押します。")
        self.invoice = _doc_chunk("doc-invoice", _INVOICE_TEXT)

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        self.queries.append(query)
        # 原質問・補助の検索文は 2 つの要求を含むので受注の資料だけを返す
        # (1 回の検索の上位に入らない要求)。
        return [self.order] if "受注" in query else [self.invoice]

    async def context_group_siblings(
        self,
        anchors: list[RetrievedChunk],
        *,
        max_chunks_per_group: int,
        neighbor_child_count: int | None = None,
    ) -> list[RetrievedChunk]:
        return []


async def _answer_with_coverage(
    monkeypatch: pytest.MonkeyPatch, oracle: CoverageOracle, question: str, **settings: Any
) -> Any:
    import rag_engine.adapters.model_api as engine_api

    def explicit_gap(system: str, prompt: str, config: Any, schema: type, **options: Any) -> Any:
        result = _fake_llm(system, prompt, config, schema, **options)
        if schema in (GroundedDraft, generation_draft_wire_model()) and _INVOICE_TEXT not in prompt:
            data = result.model_dump(exclude_unset=True)
            data["items"].append(
                {
                    "kind": "gap",
                    "text": "追加操作の根拠がありません。",
                    "request_id": "Q2",
                    "citations": [],
                }
            )
            return GroundedDraft.model_validate(data)
        return result

    monkeypatch.setattr(engine_api, "parse_text_response", explicit_gap)
    settings.setdefault("rag_request_coverage_retrieval_enabled", True)
    # 監査なしの振る舞いを確かめる（既定は standard。#135）。
    settings.setdefault("rag_answer_audit_mode", "none")
    engine = AnswerEngine(
        Settings(
            rag_query_strategy="simple_retrieval",
            rag_answer_flow="standard_rag",
            rag_rerank_enabled=False,
            **settings,
        ),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )
    return await engine.run(SearchRequest(query=question, filters={"knowledge_base_id": "kb-1"}))


async def test_request_coverage_re_retrieves_only_missing_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """根拠の無い要求だけを要求の文で 1 回検索し、その根拠を回答の材料と診断に足す(#1279)。"""
    oracle = CoverageOracle()

    outcome = await _answer_with_coverage(
        monkeypatch, oracle, "受注の登録方法は？請求書の再発行方法は？"
    )

    coverage = outcome.diagnostics["request_coverage"]
    statuses = {row["id"]: row["status"] for row in coverage["requests"]}
    assert statuses == {"Q1": "unassessed", "Q2": "unassessed"}
    after_trace = coverage["after_generation"][0]
    re_retrieval = after_trace["re_retrieval"]
    assert re_retrieval["applied"] is True
    assert [query["request_id"] for query in re_retrieval["queries"]] == ["Q2"]
    assert re_retrieval["queries"][0]["queries"][0] == "請求書の再発行方法は？"
    assert after_trace["llm_calls"] == 0
    assert all(row["status"] == "unassessed" for row in after_trace["requests"])
    # 足した根拠が回答の材料(根拠の木)に入る。
    assert any(key.startswith("doc-invoice:") for key in re_retrieval["added_chunk_ids"])
    # 合成モデルは新資料を引用しないため、公開候補には以前の回答を維持できる。
    assert "doc-order:c1" in _evidence_ids(outcome)
    assert any(
        step["name"] == "未回答の要求の探し直し" for step in outcome.diagnostics["execution_steps"]
    )


async def test_request_coverage_disabled_does_not_re_retrieve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """設定で無効なら、判定だけを記録し再検索しない(#1279)。"""
    oracle = CoverageOracle()

    outcome = await _answer_with_coverage(
        monkeypatch,
        oracle,
        "受注の登録方法は？請求書の再発行方法は？",
        rag_request_coverage_retrieval_enabled=False,
    )

    coverage = outcome.diagnostics["request_coverage"]
    assert coverage["re_retrieval"] == {"applied": False, "skipped": "disabled"}
    assert "請求書の再発行方法は？" not in oracle.queries
    assert "doc-invoice:c1" not in _evidence_ids(outcome)


# --- 元の質問の要求の単位と、検索用に言い換えた検索文(#2665) ---

_NUMBERED_ORIGINAL = "1. 受注の登録方法は？\n2. 請求書の再発行方法は？\n3. 承認の担当は？"


class ApprovalOnlyOracle(CoverageOracle):
    """「承認」を含む検索文にだけ根拠を返す検索のスタブ(前半の 2 要求は根拠が無い)。"""

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        self.queries.append(query)
        return [_doc_chunk("doc-approval", "承認の担当は課長です。")] if "承認" in query else []


@pytest.mark.parametrize(
    "query",
    [
        "受注を登録する。請求書を再発行する。承認の担当を知りたい。",
        "受注 登録 請求書 再発行 承認 担当",
    ],
)
async def test_original_question_keeps_numbered_requests_through_coverage(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    """元の質問の番号つきの 3 要求を、検索用に言い換えた検索文から作り直さずに、要求ごとの
    根拠の確認と探し直し・診断まで保つ(#2665)。検索は検索文で行う。"""
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    oracle = ApprovalOnlyOracle()
    engine = AnswerEngine(
        Settings(
            rag_query_strategy="simple_retrieval",
            rag_answer_flow="standard_rag",
            rag_rerank_enabled=False,
            rag_request_coverage_retrieval_enabled=True,
        ),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )

    outcome = await engine.run(
        SearchRequest(
            query=query,
            original_question=_NUMBERED_ORIGINAL,
            filters={"knowledge_base_id": "kb-1"},
        )
    )

    coverage = outcome.diagnostics["request_coverage"]
    assert {row["id"]: (row["kind"], row["status"]) for row in coverage["requests"]} == {
        "Q1": ("request", "unassessed"),
        "Q2": ("request", "unassessed"),
        "Q3": ("request", "unassessed"),
    }
    # 1 要求として探し直しを省かず、根拠の無い要求ごとに要求の文で探し直す。
    assert coverage["re_retrieval"]["skipped"] == "no_explicit_gap"
    for trace in coverage.get("after_generation", ()):
        assert {item["request_id"] for item in trace["re_retrieval"].get("queries", ())} <= {
            "Q1",
            "Q2",
            "Q3",
        }
    # 主の検索は言い換えた検索文で行う。
    assert oracle.queries[0] == query
    # 回答の要求(rag_search の requests。回答の検査の入力)も元の質問の 3 要求(充足は監査が決める)。
    requests = outcome.diagnostics["envelope"]["requests"]
    assert [(item["id"], item["text"]) for item in requests] == [
        ("Q1", "1. 受注の登録方法は？"),
        ("Q2", "2. 請求書の再発行方法は？"),
        ("Q3", "3. 承認の担当は？"),
    ]


async def test_without_original_question_requests_come_from_the_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """元の質問を渡さなければ、要求は検索文から作る(省略時の挙動を変えない。#2665)。"""
    import rag_engine.adapters.model_api as engine_api

    monkeypatch.setattr(engine_api, "parse_text_response", _fake_llm)
    engine = AnswerEngine(
        Settings(
            rag_query_strategy="simple_retrieval",
            rag_answer_flow="standard_rag",
            rag_rerank_enabled=False,
            rag_request_coverage_retrieval_enabled=True,
        ),
        oracle=ApprovalOnlyOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )

    outcome = await engine.run(
        SearchRequest(
            query="受注 登録 請求書 再発行 承認 担当", filters={"knowledge_base_id": "kb-1"}
        )
    )

    coverage = outcome.diagnostics["request_coverage"]
    assert [row["id"] for row in coverage["requests"]] == ["Q1"]
    # 1 要求で、最初の検索と違う検索文を作れないので探し直さない(#2810)。
    assert coverage["re_retrieval"]["skipped"] == "no_explicit_gap"


def test_build_engine_settings_passes_request_coverage(tmp_path: Any) -> None:
    """根拠の無い要求の再検索の設定と予算を rag_engine の設定へ渡す(#1279)。"""
    from app.rag.answer_engine import build_engine_settings

    default = build_engine_settings(Settings(), output_dir=tmp_path)
    custom = build_engine_settings(
        Settings(
            rag_request_coverage_retrieval_enabled=True,
            rag_request_coverage_max_queries=1,
            rag_request_coverage_max_chunks=2,
            rag_request_coverage_deadline_seconds=3.5,
            rag_request_coverage_max_subquestions=3,
            rag_request_coverage_chunks_per_subquestion=1,
        ),
        output_dir=tmp_path,
    )

    assert default.request_coverage_retrieval_enabled is False
    assert (default.request_coverage_max_queries, default.request_coverage_max_chunks) == (2, 4)
    assert default.request_coverage_deadline_seconds == 10.0
    assert custom.request_coverage_retrieval_enabled is True
    assert (custom.request_coverage_max_queries, custom.request_coverage_max_chunks) == (1, 2)
    assert custom.request_coverage_deadline_seconds == 3.5
    # 小問ごとの検索の上限(#2399)。
    assert (
        default.request_coverage_max_subquestions,
        default.request_coverage_chunks_per_subquestion,
    ) == (5, 2)
    assert (
        custom.request_coverage_max_subquestions,
        custom.request_coverage_chunks_per_subquestion,
    ) == (3, 1)


def test_search_answer_profile_ignores_legacy_request_coverage() -> None:
    """不足要求の再検索は全体の設定を使い、旧個別値を適用しない。"""
    from app.rag.kb_adapter_config import KnowledgeBaseQueryConfig
    from app.rag.search_answer_profile_config import (
        SearchAnswerProfileConfig,
        resolve_search_answer_profile_settings,
    )

    config = SearchAnswerProfileConfig(
        knowledge_base_ids=["kb-1"],
        query=KnowledgeBaseQueryConfig(request_coverage_retrieval_enabled=True),
    )

    settings, _ = resolve_search_answer_profile_settings(Settings(), config)

    assert settings.rag_request_coverage_retrieval_enabled is False
    assert Settings().rag_request_coverage_retrieval_enabled is False


async def test_guide_preview_is_recorded_without_extra_model_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """下書きで試す回答（#1288）: 記録に印を残す。モデルの呼び出しは同じ。"""
    import rag_engine.adapters.model_api as engine_api

    from app.rag.pipeline import RagPipeline
    from app.rag.support_guide_runtime import GUIDE_PREVIEW_KEY

    calls: list[str] = []

    def counting_llm(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
        calls.append(schema.__name__)
        return _fake_llm(system, prompt, settings, schema, **options)

    monkeypatch.setattr(engine_api, "parse_text_response", counting_llm)
    request = SearchRequest(query="受注の登録方法は？", search_answer_profile_id="bv-1")

    normal_oracle = SavingOracle()
    await RagPipeline(
        settings=Settings(),
        oracle=normal_oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    ).run(request, trace_id="trace-normal")
    normal_calls = list(calls)
    calls.clear()

    marker = {"guide_id": "g-1", "draft_revision": 3, "published_revision": 2}
    preview_oracle = SavingOracle()
    response = await RagPipeline(
        settings=Settings(rag_guide_preview=marker),
        oracle=preview_oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    ).run(request, trace_id="guide-preview-trace")

    assert calls == normal_calls
    saved = preview_oracle.saved[0]
    assert saved["diagnostics"][GUIDE_PREVIEW_KEY] == marker
    assert GUIDE_PREVIEW_KEY not in normal_oracle.saved[0]["diagnostics"]
    assert response.diagnostics.answer is not None
    assert response.diagnostics.answer[GUIDE_PREVIEW_KEY] == marker


def test_diverse_candidates_adds_each_document_tail_without_changing_the_head() -> None:
    """融合の上位は変えず、上位に無い文書は上位 N 件、ある文書は続きの M 件を足す(#2228)。"""
    from app.rag.answer_engine import _diverse_candidates

    ordered = ["a1", "a2", "a3", "a4", "b1", "b2", "b3", "c1"]
    head = ordered[:2]  # 上位は文書 a だけ
    extra = _diverse_candidates(
        ordered, len(head), lambda chunk_id: chunk_id[0], new_document=2, present_document=1
    )
    assert extra == ["a3", "b1", "b2", "c1"]
    # 足す件数が 0 なら何も足さない。
    assert _diverse_candidates(ordered, 2, lambda c: c[0], new_document=0, present_document=0) == []


@pytest.mark.parametrize("diversity", [0, 100, 600])
def test_read_depth_is_independent_of_diversity_and_anchor_count(diversity: int) -> None:
    engine = AnswerEngine(
        Settings(rag_retrieval_query_limit=321, rag_candidate_diversity_depth=diversity),
        oracle=cast(Any, TwoDocumentOracle()),
        genai=cast(Any, FakeGenAi()),
    )
    assert engine._read_depth(50) == engine._read_depth(150) == 321


async def test_search_without_diversity_keeps_only_the_fused_head() -> None:
    """``DEPTH=0`` と ``MAX_EXTRA=0`` では、融合の上位の外の候補を足さない(#2228)。"""
    from app.rag.answer_engine import _SearchState

    for depth, max_extra, expected in ((0, 100, 1), (600, 0, 1), (600, 100, 2)):
        engine = AnswerEngine(
            Settings(
                rag_candidate_diversity_depth=depth, rag_candidate_diversity_max_extra=max_extra
            ),
            oracle=cast(Any, TwoDocumentOracle()),
            genai=cast(Any, FakeGenAi()),
        )
        result = await engine._search(
            SearchRequest(query="q"),
            _SearchState(),
            retrieval_queries=["受注の登録"],
            candidate_limit=1,
        )
        assert len({chunk.chunk_uid for chunk in result.child_chunks}) == expected


async def test_evidence_images_are_cropped_once_per_chunk_in_an_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """同じ回答で同じ chunk が何度当たっても、画像の切り出しは 1 回（#2805）。

    画像の根拠の無い chunk は metadata の JSON を解かずに飛ばす。
    """
    import app.rag.answer_engine as engine_module

    crops: list[int] = []

    def fake_crop(source: bytes, page: int, bbox: Any, size: Any) -> bytes:
        crops.append(page)
        return b"\x89PNG fake"

    async def fake_source(oracle: object, document_id: str) -> bytes:
        return b"%PDF"

    monkeypatch.setattr(engine_module, "crop_png", fake_crop)
    monkeypatch.setattr(engine_module, "load_parsed_source", fake_source)
    oracle = _figure_oracle()
    engine = AnswerEngine(Settings(), oracle=oracle, genai=FakeGenAi())  # type: ignore[arg-type]
    state = engine_module._SearchState(work_dir=tmp_path)
    figure, plain = oracle.chunks[0], oracle.chunks[1]

    for _ in range(3):
        await engine._materialize_image_evidence(figure, state)
        await engine._materialize_image_evidence(plain, state)

    assert crops == [1]
    materialized = json.loads(str(state.chunks[figure.chunk_id].metadata["engine_metadata_json"]))
    assert materialized["image_evidence"][0]["crop_path"] == "crops/docling-p1-5.png"
    assert state.materialized_images[plain.chunk_id] is None
    assert plain.chunk_id not in state.chunks


@pytest.mark.parametrize(
    ("reply", "expected"),
    [
        (
            json.dumps({"question": "設定 A の手順は？\n条件 B との違いは？"}),
            "設定 A の手順は？ 条件 B との違いは？",
        ),
        ("設定 A の手順は？\n説明を追加します", "それの登録方法は？"),
        (json.dumps({"question": "設定 A は？", "explanation": "説明"}), "それの登録方法は？"),
        (json.dumps({"question": ["設定 A は？"]}), "それの登録方法は？"),
        (json.dumps({"question": ""}), "それの登録方法は？"),
        (json.dumps({"question": "長" * 201}), "それの登録方法は？"),
        (json.dumps({"question": "それの登録方法は？"}), "それの登録方法は？"),
    ],
)
async def test_history_rewrite_preserves_full_question_or_falls_back(
    monkeypatch: pytest.MonkeyPatch, reply: str, expected: str
) -> None:
    from pr_backend_core.chat_history import CHAT_HISTORY_UNTRUSTED_NOTE

    from app.rag.pipeline import HISTORY_REWRITE_SYSTEM_PROMPT

    llm = RewriteLlm(reply)
    _response, oracle = await _run_chat(monkeypatch, llm, history=True)
    assert oracle.queries[0] == _nfkc(expected)
    assert llm.system_prompts == [HISTORY_REWRITE_SYSTEM_PROMPT]
    assert all(f"## Step {step}:" in HISTORY_REWRITE_SYSTEM_PROMPT for step in range(1, 5))
    assert HISTORY_REWRITE_SYSTEM_PROMPT.count(CHAT_HISTORY_UNTRUSTED_NOTE) == 1


@pytest.mark.parametrize("fails", [False, True])
async def test_approved_faq_generation_reports_usage_and_failure(
    monkeypatch: pytest.MonkeyPatch, fails: bool
) -> None:
    import rag_engine.generation.faq_answer as faq
    from rag_engine.resources.llm_usage import record_llm_usage

    def answer(*args: object) -> str:
        record_llm_usage({"input_tokens": 42, "output_tokens": 8})
        if fails:
            raise ValueError("合成の生成失敗")
        return "設定 A の回答です。"

    monkeypatch.setattr(faq, "answer_from_approved_faq", answer)
    events: list[tuple[str, str, float, Mapping[str, Any]]] = []

    async def notify(name: str, state: str, elapsed: float, record: Mapping[str, Any]) -> None:
        events.append((name, state, elapsed, record))

    engine = AnswerEngine(
        Settings(),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        approved_faq=("設定 A は？", "設定 A の回答です。"),
    )
    if fails:
        with pytest.raises(ValueError, match="合成の生成失敗"):
            await engine.run(SearchRequest(query="設定 A"), step_callback=notify)
    else:
        result = await engine.run(SearchRequest(query="設定 A"), step_callback=notify)
        assert result.diagnostics["execution_steps"][0]["llm_usage"]["input_tokens"] == 42
    assert [item[1] for item in events] == ["started", "error" if fails else "success"]
    assert events[-1][2] > 0
    assert events[-1][3]["llm_usage"]["calls"] == 1


@pytest.mark.parametrize("engine", [True, False])
@pytest.mark.parametrize("enabled", [True, False])
def test_engine_bridge_keeps_original_search_context(engine: bool, enabled: bool) -> None:
    metadata: dict[str, Any] = {
        "section_path": "分類 A > 機能 A > 説明",
        "context_header_enabled": enabled,
    }
    if enabled:
        metadata["context_header"] = "document-a.txt > 分類 A > 機能 A > 説明"
    if engine:
        metadata["engine_metadata_json"] = json.dumps(
            {"section_path": ["分類 A", "機能 A", "説明"]}, ensure_ascii=False
        )
    chunk = RetrievedChunk(
        document_id="doc-a",
        chunk_id="chunk-a",
        text="説明本文",
        score=1,
        file_name="document-a.txt",
        metadata=metadata,
    )
    result = answer_engine_module._stored_child(chunk, rrf_score=1)
    assert result.metadata["section_path"] == ["分類 A", "機能 A", "説明"]
    assert result.metadata["context_header_enabled"] is enabled
    assert result.metadata.get("context_header") == metadata.get("context_header")
    assert "context_header" not in result.text


def test_context_parent_uses_only_stored_parent_text_and_artifact_scope() -> None:
    from app.rag.answer_engine import _SearchState, _stored_child, _stored_parents

    chunk = _chunk("child-a", "子の原文", 1)
    state = _SearchState()
    state.chunks[chunk.chunk_id] = chunk
    stored = _stored_child(chunk, rrf_score=0.9)
    assert _stored_parents([stored], state)[0].text == PARENT_TEXT
    for metadata in (
        {**chunk.metadata, "chunk_strategy": "fixed_length"},
        {**chunk.metadata, "parent_text": ""},
    ):
        modified = chunk.model_copy(update={"metadata": metadata})
        state.chunks[chunk.chunk_id] = modified
        assert _stored_parents([_stored_child(modified, rrf_score=0.9)], state) == []
    versions = [
        chunk.model_copy(
            update={
                "chunk_id": f"child-{i}",
                "metadata": {**chunk.metadata, "chunk_set_id": f"set-{i}"},
            }
        )
        for i in range(2)
    ]
    state.chunks.update({c.chunk_id: c for c in versions})
    parents = _stored_parents([_stored_child(c, rrf_score=0.9) for c in versions], state)
    assert len(parents) == 2
    assert parents[0].chunk_uid != parents[1].chunk_uid
