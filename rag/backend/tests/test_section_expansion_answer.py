"""回答で当たった chunk の章節から親の章節へ広げる（#1436）の backend 側。

設定・章節の読み込み・引用・診断を確かめる。章節と chunk の対応（#1438）は
``section_map_service`` の関数をスタブにし、LLM は決まった応答のスタブにする
（DB と OCI を呼ばない）。並列でも直列でも通るように、状態はテストごとに作る。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest
from rag_engine.generation.citation_wire import generation_draft_wire_model

from app.clients.section_map_store import ReadableChunkSet, SectionChunkRow
from app.config import PROFILE_ONLY_SETTING_FIELDS, Settings
from app.rag import section_map_service
from app.rag.answer_engine import AnswerEngine, build_engine_settings
from app.rag.section_chunk_map import SectionNode
from app.schemas.search import RetrievedChunk, SearchRequest
from tests.test_answer_engine import FakeGenAi, FakeOracle, _chunk, _fake_llm

SECTION_FIELDS = {
    "rag_section_expansion_enabled",
    "rag_section_expansion_max_levels",
    "rag_section_expansion_input_token_percent",
}


def _section_chunk(chunk_id: str, text: str, seq: int, page: int) -> RetrievedChunk:
    chunk = _chunk(chunk_id, text, seq)
    metadata = dict(chunk.metadata)
    # 章節の後ろのページの chunk は別の親（small_to_big の組）にする。
    metadata.update(
        chunk_group_id=f"chunk-docling-p{page:06d}",
        parent_text=text,
        page_start=page,
        page_end=page,
        chunk_set_id="cs-1",
        engine_metadata_json=json.dumps(
            {
                **json.loads(str(metadata["engine_metadata_json"])),
                "content_hash": f"s{seq}",
                "section_path": ["受注入力画面"],
            }
        ),
    )
    return chunk.model_copy(update={"metadata": metadata})


SECTION_TAIL = [
    _section_chunk("doc-1:c3", "受注の登録の後に、承認者が受注一覧で承認します。", 3, 2),
    _section_chunk("doc-1:c4", "承認の後は、受注の取り消しができません。", 4, 3),
]


@dataclass
class SectionMapStub:
    """章節の一覧（文書全体 > 受注入力画面）と章節の本文のスタブ。"""

    reads: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    outline_calls: int = 0

    def sections(self) -> list[SectionNode]:
        return [
            SectionNode("document", "manual.pdf", 0, 0, None, 1, 3, chunk_count=4, chars=200),
            SectionNode("s1", "受注入力画面", 1, 1, "document", 1, 3, chunk_count=4, chars=200),
        ]

    async def chunk_section(
        self, document_id: str, chunk_id: str, *, oracle: Any = None
    ) -> tuple[str, str] | None:
        return ("cs-1", "s1") if document_id == "doc-1" else None

    async def section_outline(
        self, document_id: str, *, chunk_set_id: str | None = None, oracle: Any = None
    ) -> section_map_service.SectionOutline:
        self.outline_calls += 1
        return section_map_service.SectionOutline(
            chunk_set=ReadableChunkSet("doc-1", "cs-1", "manual.pdf", False),
            sections=self.sections(),
        )

    async def read_section(
        self,
        document_id: str,
        section_id: str,
        *,
        chunk_set_id: str | None = None,
        limit: int | None = None,
        filters: dict[str, str] | None = None,
        oracle: Any = None,
        **_: Any,
    ) -> section_map_service.SectionText:
        self.reads.append((section_id, dict(filters or {})))
        oracle_chunks = [
            _chunk("doc-1:c1", "受注番号を入力し、登録ボタンを押します。", 1),
            _chunk("doc-1:c2", "登録後は受注一覧に表示されます。", 2),
        ]
        rows = [
            SectionChunkRow(order, chunk)
            for order, chunk in enumerate([*oracle_chunks, *SECTION_TAIL])
        ]
        outline = await self.section_outline(document_id)
        node = outline.get(section_id)
        assert node is not None
        return section_map_service.SectionText(
            outline=outline, section=node, chunks=rows, complete=True
        )


def _direct_answer_part(question: str, context: Any) -> dict[str, Any]:
    """生成入力に残る原文を、章補完フローの正例へ明示する。"""
    from rag_engine.generation.answering import _grounded_spans

    span = _grounded_spans(question, context, (), None)[0]
    return {
        "aspect": "procedure",
        "kind": "operation",
        "claim": span["text"],
        "status": "supported",
        "evidence": [{"source_id": span["source_id"], "quote": span["text"]}],
        "evidence_validation": "matched",
    }


@pytest.fixture
def section_map(monkeypatch: pytest.MonkeyPatch) -> SectionMapStub:
    stub = SectionMapStub()
    monkeypatch.setattr(section_map_service, "chunk_section", stub.chunk_section)
    monkeypatch.setattr(section_map_service, "section_outline", stub.section_outline)
    monkeypatch.setattr(section_map_service, "read_section", stub.read_section)
    return stub


def _engine(**settings: Any) -> AnswerEngine:
    # 既存の親への追加拡張のテストは CRAG、共通の最低範囲は両方式で確かめる。
    # 章節の抜き出しの LLM（#1437）を確かめるテストがあるので、
    # 既定 OFF（#2800）の抜き出しを有効にする。
    settings = {
        "rag_query_strategy": "auto_routing",
        "rag_answer_flow": "crag",
        "rag_section_extraction_enabled": True,
        **settings,
    }
    return AnswerEngine(
        Settings(rag_domain_keywords=["受注番号"], **settings),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    import rag_engine.adapters.oci as engine_oci

    # CRAG の判定は候補の関連を返さない（不足）ので、章節を広げる。
    monkeypatch.setattr(engine_oci, "parse_text_response", _fake_llm)


@pytest.mark.usefixtures("stub_llm")
async def test_answer_reads_the_section_of_the_hit_chunk_and_cites_chunks(
    section_map: SectionMapStub,
) -> None:
    request = SearchRequest(query="受注の登録方法は？", filters={"knowledge_base_id": "kb-1"})

    outcome = await _engine().run(request)

    # 章節の本文は回答の検索と同じ条件（ナレッジベース）で読む。
    assert section_map.reads and section_map.reads[0][0] == "s1"
    assert section_map.reads[0][1]["knowledge_base_id"] == "kb-1"
    # 章節の chunk は chunk の ID のまま引用に入り、役割は章節の文脈。
    roles = {chunk.chunk_id: chunk.metadata.get("evidence_role") for chunk in outcome.citations}
    assert roles.get("doc-1:c3") == "section_context"
    assert roles.get("doc-1:c4") == "section_context"
    # 当たった chunk（最も関連の高い根拠）は引用の先頭のまま。
    assert outcome.citations[0].chunk_id == "doc-1:c1"
    expansion = outcome.diagnostics["section_expansion"]
    assert expansion["applied"] is True
    assert expansion["levels_read"] >= 1
    assert expansion["used_tokens"] <= expansion["input_token_limit"]
    assert "grades" not in expansion
    # 章節の一覧は 1 回の回答で 1 回だけ読む（読むたびの確認の分を除く）。
    assert section_map.outline_calls >= 1


@pytest.mark.usefixtures("stub_llm")
async def test_max_levels_limit_makes_the_answer_conditional(
    section_map: SectionMapStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rag_engine.adapters.oci as engine_oci
    from rag_engine.models.llm import CragRetrievalGradeOutput

    def missing_procedure(
        system: str, prompt: str, settings: Any, schema: type, **options: Any
    ) -> Any:
        if schema is CragRetrievalGradeOutput:
            # 読み取り段数の上限を試すので、未評価ではなく未充足の操作を明示する。
            supported = _fake_llm(system, prompt, settings, schema, **options)
            return supported.model_copy(
                update={
                    "sufficient": False,
                    "reason": "文書全体にある後続操作の説明が不足",
                    "aspect_checks": [
                        check.model_copy(
                            update={"status": "missing", "source_ids": [], "evidence": []}
                        )
                        if check.aspect == "procedure"
                        else check
                        for check in supported.aspect_checks
                    ],
                }
            )
        return _fake_llm(system, prompt, settings, schema, **options)

    monkeypatch.setattr(engine_oci, "parse_text_response", missing_procedure)
    outcome = await _engine(rag_section_expansion_max_levels=1).run(
        SearchRequest(query="受注の登録方法は？")
    )

    expansion = outcome.diagnostics["section_expansion"]
    assert expansion["stop_reason"] == "max_levels"
    assert [item["section_id"] for item in expansion["unread"]] == ["document"]
    assert outcome.diagnostics["outcome"] == "conditional"
    assert "回答に未反映の関連章節" in outcome.answer
    assert "「manual.pdf」の全体" in outcome.answer


async def test_initial_sufficient_grade_still_adds_section_text_to_generation(
    section_map: SectionMapStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rag_engine.adapters.oci as engine_oci
    from rag_engine.generation import answering
    from rag_engine.generation.answer_models import CragRetrievalAttempt
    from rag_engine.models.llm import GroundedDraft

    prompts: list[str] = []

    def enough(
        attempt: int,
        question: str,
        query: str,
        queries: Any,
        context: Any,
        settings: Any,
        **options: Any,
    ) -> CragRetrievalAttempt:
        return CragRetrievalAttempt(
            attempt=attempt,
            query=query,
            retrieval_queries=tuple(queries),
            sufficient=True,
            answerable_parts=(_direct_answer_part(question, context),),
            relevant_chunk_ids=tuple(
                child.record.chunk_uid
                for parent in context.evidence_tree
                for child in parent.children
            ),
        )

    def llm(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
        if schema in (GroundedDraft, generation_draft_wire_model()):
            prompts.append(prompt)
        return _fake_llm(system, prompt, settings, schema, **options)

    monkeypatch.setattr(answering, "_grade_crag_retrieval", enough)
    monkeypatch.setattr(engine_oci, "parse_text_response", llm)
    outcome = await _engine().run(SearchRequest(query="受注の登録方法は？"))

    assert section_map.reads and section_map.reads[0][0] == "s1"
    assert outcome.diagnostics["section_expansion"]["levels_read"] == 1
    assert any(SECTION_TAIL[0].text in prompt for prompt in prompts), outcome.diagnostics


@pytest.mark.usefixtures("stub_llm")
async def test_disabled_profile_does_not_read_sections(section_map: SectionMapStub) -> None:
    outcome = await _engine(rag_section_expansion_enabled=False).run(
        SearchRequest(query="受注の登録方法は？")
    )

    assert section_map.reads == []
    assert outcome.diagnostics["section_expansion"]["stop_reason"] == "disabled"
    assert all(
        chunk.metadata.get("evidence_role") != "section_context" for chunk in outcome.citations
    )


@pytest.mark.usefixtures("stub_llm")
async def test_answer_continues_when_section_map_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("ORA-00942")

    monkeypatch.setattr(section_map_service, "chunk_section", broken)

    outcome = await _engine().run(SearchRequest(query="受注の登録方法は？"))

    assert "登録ボタン" in outcome.answer
    assert outcome.diagnostics["section_expansion"]["stop_reason"] == "no_sections"


def test_section_expansion_settings_are_global(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """章節の拡張は検索方法の全体設定を保存した環境変数から読む。"""
    assert SECTION_FIELDS.isdisjoint(PROFILE_ONLY_SETTING_FIELDS)
    monkeypatch.setenv("RAG_SECTION_EXPANSION_ENABLED", "false")
    env_file = tmp_path / "backend.env"
    env_file.write_text("RAG_SECTION_EXPANSION_MAX_LEVELS=2\n", encoding="utf-8")

    settings = Settings(_env_file=env_file)

    assert settings.rag_section_expansion_enabled is False
    assert settings.rag_section_expansion_max_levels == 2
    assert settings.rag_section_expansion_input_token_percent == 50


def test_search_answer_profile_ignores_legacy_section_expansion() -> None:
    from app.rag.search_answer_profile_config import (
        parse_search_answer_profile_config,
        resolve_search_answer_profile_settings,
    )

    base = Settings()
    selected, applied = resolve_search_answer_profile_settings(
        base,
        parse_search_answer_profile_config(
            {
                "knowledge_base_ids": ["kb-1"],
                "query": {
                    "section_expansion_enabled": False,
                    "section_expansion_max_levels": 2,
                    "section_expansion_input_token_percent": 30,
                    "section_expansion_time_limit_seconds": 300,
                    "section_extraction_max_calls": 4,
                },
            }
        ),
    )

    assert applied is False
    assert (
        selected.rag_section_expansion_enabled,
        selected.rag_section_expansion_max_levels,
        selected.rag_section_expansion_input_token_percent,
    ) == (True, 4, 50)
    assert base.rag_section_expansion_enabled is True
    # 範囲の外の保存値は読まない。
    broken = parse_search_answer_profile_config(
        {"knowledge_base_ids": ["kb-1"], "query": {"section_expansion_max_levels": 9}}
    )
    assert broken.query.section_expansion_max_levels is None
    assert "section_expansion_time_limit_seconds" not in type(broken.query).model_fields
    assert "section_extraction_max_calls" not in type(broken.query).model_fields
    legacy = parse_search_answer_profile_config(
        {
            "query": {
                "section_expansion_time_limit_seconds": 60,
                "section_extraction_max_calls": 1,
            }
        }
    )
    assert "section_expansion_time_limit_seconds" not in legacy.query.model_dump()
    assert "section_extraction_max_calls" not in legacy.query.model_dump()


def test_build_engine_settings_passes_section_expansion(tmp_path: Any) -> None:
    default = build_engine_settings(Settings(), output_dir=tmp_path)
    custom = build_engine_settings(
        Settings(
            rag_section_expansion_enabled=False,
            rag_section_expansion_max_levels=2,
            rag_section_expansion_input_token_percent=30,
            rag_answer_timeout_seconds=300,
        ),
        output_dir=tmp_path,
    )

    assert (default.section_expansion_enabled, default.section_expansion_max_levels) == (True, 4)
    # 章節の抜き出しの LLM は既定で使わない（#2800）。
    assert default.section_extraction_enabled is False
    assert build_engine_settings(
        Settings(rag_section_extraction_enabled=True), output_dir=tmp_path
    ).section_extraction_enabled
    assert default.section_expansion_input_token_percent == 50
    assert (custom.section_expansion_enabled, custom.section_expansion_max_levels) == (False, 2)
    assert custom.section_expansion_input_token_percent == 30
    # 回答の通しの上限より長くしない。


def test_build_engine_settings_passes_model_input_limit(tmp_path: Any) -> None:
    """回答のモデルの入力の上限は、共通のモデル設定の登録モデルの値（#1437。既定 128000）。"""
    from pr_system_settings.model import EnterpriseAiConfiguredModel

    models = [
        EnterpriseAiConfiguredModel(model_id="llm-a", max_input_tokens=200_000),
        EnterpriseAiConfiguredModel(model_id="llm-b"),
    ]
    settings = Settings(
        oci_enterprise_ai_models=models,
        oci_enterprise_ai_default_text_model="llm-a",
    )

    engine = build_engine_settings(settings, output_dir=tmp_path)
    other = build_engine_settings(settings, output_dir=tmp_path, answer_model_id="llm-b")

    assert engine.answer_max_input_tokens == 200_000
    assert other.answer_max_input_tokens == 128_000
    assert build_engine_settings(Settings(), output_dir=tmp_path).answer_max_input_tokens == 128_000


def test_build_engine_settings_passes_model_output_limits(tmp_path: Any) -> None:
    """回答と Vision のモデルの出力の上限は登録モデルの値（#1719。既定 32768。入力の上限以下）。"""
    from pr_system_settings.model import EnterpriseAiConfiguredModel
    from rag_engine.config import get_llm_provider, llm_output_token_limit

    models = [
        EnterpriseAiConfiguredModel(model_id="llm-a", max_output_tokens=16_000),
        EnterpriseAiConfiguredModel(model_id="vlm-a", max_output_tokens=64_000),
        EnterpriseAiConfiguredModel(model_id="llm-small", max_input_tokens=8_192),
    ]
    settings = Settings(
        oci_enterprise_ai_models=models,
        oci_enterprise_ai_default_text_model="llm-a",
        oci_enterprise_ai_default_vision_model="vlm-a",
    )

    engine = build_engine_settings(settings, output_dir=tmp_path)
    answer = get_llm_provider(engine, engine.default_answer_llm)
    vision = get_llm_provider(engine, engine.default_vision_llm)
    assert (answer.max_output_tokens, vision.max_output_tokens) == (16_000, 64_000)
    # 送る上限は回答の上限（既定 32768）とモデルの出力の上限の小さい方。
    assert engine.answer_max_tokens == 32_768
    assert (llm_output_token_limit(engine, answer), llm_output_token_limit(engine, vision)) == (
        16_000,
        32_768,
    )
    # 入力の上限の小さいモデルは、出力の上限も入力の上限を超えない。
    small = build_engine_settings(settings, output_dir=tmp_path, answer_model_id="llm-small")
    assert get_llm_provider(small, small.default_answer_llm).max_output_tokens == 8_192


def test_token_estimate_and_default_limit_match_platform() -> None:
    """rag_engine の見積もり・既定は platform の値の解釈と同じ（3 製品で共通の 1 か所。#1437）。"""
    from pr_system_settings.model import (
        DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS,
        estimate_input_tokens,
    )
    from rag_engine.generation.section_expansion import (
        DEFAULT_MODEL_INPUT_TOKENS,
        estimate_tokens,
    )

    assert DEFAULT_MODEL_INPUT_TOKENS == DEFAULT_ENTERPRISE_AI_MAX_INPUT_TOKENS
    for text in ["", "点検", "abcd", "abcde", "UPS の点検", "手順 1: 電源を切る。\n" * 30]:
        assert estimate_tokens(text) == estimate_input_tokens(text)


def test_evaluation_overrides_switch_section_expansion() -> None:
    from app.rag.evaluation import evaluation_settings
    from app.schemas.evaluation import EvaluationRagOverrides

    settings = evaluation_settings(
        Settings(),
        EvaluationRagOverrides(section_expansion_enabled=False),
    )

    assert settings.rag_section_expansion_enabled is False


# ---------------------------------------------------------------- 抽出してから統合（#1437）

APPROVAL = "承認者は受注一覧で対象を選び、承認ボタンを押します。"
FILLER = "この段落は画面の項目の並びについての説明で、承認の手順には関係しません。"


def _long_text(page: int) -> str:
    """入力の上限に入らない章節の 1 頁（承認の手順の 1 文と、関係の無い説明の段落）。"""
    filler = FILLER * 25
    return f"{filler}\n\n{APPROVAL}\n\n{filler}"


@dataclass
class LongSectionStub(SectionMapStub):
    """受注入力画面の章節が長い（頁 2〜9 の 8 件。モデルの入力の上限 4096 の半分に入らない）。"""

    def sections(self) -> list[SectionNode]:
        return [
            SectionNode("document", "manual.pdf", 0, 0, None, 1, 9, chunk_count=10, chars=20_000),
            SectionNode("s1", "受注入力画面", 1, 1, "document", 1, 9, chunk_count=10, chars=20_000),
        ]

    async def read_section(self, document_id: str, section_id: str, **kwargs: Any) -> Any:
        self.reads.append((section_id, dict(kwargs.get("filters") or {})))
        chunks = [
            _chunk("doc-1:c1", "受注番号を入力し、登録ボタンを押します。", 1),
            _chunk("doc-1:c2", "登録後は受注一覧に表示されます。", 2),
            *(
                _section_chunk(f"doc-1:l{page}", _long_text(page), 10 + page, page)
                for page in range(2, 10)
            ),
        ]
        outline = await self.section_outline(document_id)
        node = outline.get(section_id)
        assert node is not None
        return section_map_service.SectionText(
            outline=outline,
            section=node,
            chunks=[SectionChunkRow(order, chunk) for order, chunk in enumerate(chunks)],
            complete=True,
        )


@pytest.fixture
def long_section(monkeypatch: pytest.MonkeyPatch) -> LongSectionStub:
    stub = LongSectionStub()
    monkeypatch.setattr(section_map_service, "chunk_section", stub.chunk_section)
    monkeypatch.setattr(section_map_service, "section_outline", stub.section_outline)
    monkeypatch.setattr(section_map_service, "read_section", stub.read_section)
    return stub


async def test_long_section_is_extracted_but_fixed_generation_input_exceeds_model_limit(
    monkeypatch: pytest.MonkeyPatch, long_section: LongSectionStub
) -> None:
    import re

    import rag_engine.adapters.oci as engine_oci
    from pr_system_settings.model import EnterpriseAiConfiguredModel
    from rag_engine.models.llm import (
        GroundedDraft,
        SectionExtractionOutput,
        SectionExtractionQuote,
    )

    prompts: dict[str, list[str]] = {"extract": [], "draft": []}
    block = re.compile(
        r"^\[(C\d+)[^\]\n]*\]\n(.*?)(?=\n\n\[C\d+|\n\nEND_UNTRUSTED_RETRIEVED_CONTEXT)", re.M | re.S
    )

    def llm(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
        if schema is SectionExtractionOutput:
            prompts["extract"].append(prompt)
            quotes = [
                SectionExtractionQuote(chunk_ref=ref, location="", quote=sentence)
                for ref, text in block.findall(prompt)
                for sentence in re.findall(r"[^。\n]+。", text)
                if "承認ボタン" in sentence
            ]
            # 作られた引用（原文に無い）も混ぜる。照合で捨てる。
            quotes.append(
                SectionExtractionQuote(chunk_ref="C1", location="", quote="承認は不要です。")
            )
            return SectionExtractionOutput(quotes=quotes)
        if schema in (GroundedDraft, generation_draft_wire_model()):
            prompts["draft"].append(prompt)
        return _fake_llm(system, prompt, settings, schema, **options)

    monkeypatch.setattr(engine_oci, "parse_text_response", llm)
    engine = AnswerEngine(
        Settings(
            rag_domain_keywords=["受注番号"],
            rag_query_strategy="auto_routing",
            rag_answer_flow="crag",
            rag_section_extraction_enabled=True,
            oci_enterprise_ai_models=[
                EnterpriseAiConfiguredModel(model_id="llm-a", max_input_tokens=4096)
            ],
            oci_enterprise_ai_default_text_model="llm-a",
        ),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )

    outcome = await engine.run(SearchRequest(query="受注の承認の手順は？"))

    expansion = outcome.diagnostics["section_expansion"]
    entry = expansion["levels"][0]["sections"][0]
    assert entry["read"] == "extracted"
    assert expansion["large_section_handoff"] == "extract_then_integrate"
    # 作られた引用は捨て、原文と一致した引用だけを使う。
    assert len(prompts["extract"]) >= 1
    assert entry["extraction"]["dropped_quotes"] == len(prompts["extract"])
    assert "max_calls" not in entry["extraction"]
    assert entry["extraction"]["quotes"] == 8
    # 根拠の入力は、モデルの入力の上限（4096）の 50% の中。
    assert expansion["input_token_limit"] == 2048
    assert expansion["used_tokens"] <= expansion["input_token_limit"]
    # 抜き出しが入っても固定の指示・schema・出力予約が入らないモデルには生成を送らない。
    assert not prompts["draft"]
    assert outcome.diagnostics["outcome"] == "conditional"
    budget = outcome.diagnostics["generation_input_budget"]
    assert budget["reason"] == "required_evidence_or_fixed_input_exceeds_limit"
    assert not budget["fits"]


@pytest.mark.parametrize("flow", ["standard_rag", "crag"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_section_completion_reaches_generation_in_both_answer_flows(
    flow: str, enabled: bool, section_map: SectionMapStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    import rag_engine.adapters.oci as engine_oci
    from rag_engine.generation import answering
    from rag_engine.generation.answer_models import CragRetrievalAttempt
    from rag_engine.models.llm import GroundedDraft

    prompts: list[str] = []
    grades: list[int] = []

    def grade(
        attempt: int,
        question: str,
        query: str,
        queries: Any,
        context: Any,
        settings: Any,
        **options: Any,
    ) -> CragRetrievalAttempt:
        grades.append(attempt)
        return CragRetrievalAttempt(
            attempt=attempt,
            query=query,
            retrieval_queries=tuple(queries),
            sufficient=True,
            answerable_parts=(_direct_answer_part(question, context),),
        )

    def llm(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
        if schema in (GroundedDraft, generation_draft_wire_model()):
            prompts.append(prompt)
        return _fake_llm(system, prompt, settings, schema, **options)

    monkeypatch.setattr(answering, "_grade_crag_retrieval", grade)
    monkeypatch.setattr(engine_oci, "parse_text_response", llm)
    outcome = await _engine(rag_answer_flow=flow, rag_section_expansion_enabled=enabled).run(
        SearchRequest(query="登録方法は？")
    )

    assert bool(section_map.reads) is enabled
    assert any(SECTION_TAIL[0].text in prompt for prompt in prompts) is enabled, outcome.diagnostics
    if flow == "standard_rag":
        assert grades == []
    if enabled:
        assert outcome.diagnostics["section_expansion"]["minimum_scope_complete"] is True
        assert [section_id for section_id, _ in section_map.reads] == ["s1"]


@pytest.mark.parametrize("failure", ["empty", "partial", "forbidden", "exception"])
@pytest.mark.usefixtures("stub_llm")
async def test_incomplete_or_unreadable_sections_never_count_as_complete(
    failure: str, section_map: SectionMapStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = section_map_service.read_section

    async def incomplete(*args: Any, **kwargs: Any) -> Any:
        if failure in {"forbidden", "exception"}:
            raise PermissionError("unavailable")
        text = await original(*args, **kwargs)
        return section_map_service.SectionText(
            outline=text.outline,
            section=text.section,
            chunks=text.chunks[:1] if failure == "partial" else [],
            complete=False,
        )

    monkeypatch.setattr(section_map_service, "read_section", incomplete)
    outcome = await _engine(rag_answer_flow="standard_rag").run(SearchRequest(query="登録方法は？"))
    trace = outcome.diagnostics["section_expansion"]
    assert trace["minimum_scope_complete"] is False
    assert trace["unread"]
    assert outcome.diagnostics["outcome"] == "conditional"


@pytest.mark.parametrize("stage", ["guide", "coverage", "coverage_same_parent"])
@pytest.mark.usefixtures("stub_llm")
async def test_supplemental_searches_complete_new_sections_before_generation(
    stage: str, section_map: SectionMapStub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from rag_engine.generation import answering
    from rag_engine.generation.answer_models import AnswerContext
    from rag_engine.generation.crag_support import _merge_crag_evidence
    from rag_engine.retrieval.context_builder import ContextChildEvidence, ContextParentEvidence
    from rag_engine.retrieval.guide_steps import GuidePlan, GuideStep

    import app.rag.answer_engine as backend

    generated_prompts: list[str] = []
    # 検索済みの根拠と追加検索の根拠を異なる章節にする。
    original_outline = section_map_service.section_outline
    original_read = section_map_service.read_section

    async def outline(*args: Any, **kwargs: Any) -> Any:
        result = await original_outline(*args, **kwargs)
        assert result is not None
        return replace(
            result,
            sections=[
                *result.sections,
                SectionNode("s2", "追加操作", 1, 2, "document", 4, 4, chunk_count=2, chars=40),
            ],
        )

    async def owner(document_id: str, chunk_id: str, **kwargs: Any) -> Any:
        return "cs-1", "s2" if chunk_id in {"doc-1:c5", "doc-1:c6"} else "s1"

    async def read(document_id: str, section_id: str, **kwargs: Any) -> Any:
        if section_id != "s2":
            return await original_read(document_id, section_id, **kwargs)
        section_map.reads.append((section_id, dict(kwargs.get("filters") or {})))
        result = await outline(document_id)
        chunks = [
            _section_chunk("doc-1:c5", "設定を変更します。", 5, 4),
            _section_chunk("doc-1:c6", "変更後に確認を実行します。", 6, 4),
        ]
        return section_map_service.SectionText(
            result, result.get("s2"), [SectionChunkRow(i, c) for i, c in enumerate(chunks)], True
        )

    # 追加検索の返却だけをスタブにし、その後の共通補完・引用・生成は実際の経路を通す。
    async def new_anchor(self: Any, state: Any) -> Any:
        chunk = _section_chunk("doc-1:c5", "設定を変更します。", 5, 4)
        state.chunks[chunk.chunk_id] = chunk
        return await original_engine_outline(self, state, chunk.chunk_id)

    original_engine_outline = backend.AnswerEngine._section_outline

    async def engine_outline(self: Any, state: Any, chunk_id: str) -> Any:
        if chunk_id == "doc-1:c5" and chunk_id not in state.chunks:
            return await new_anchor(self, state)
        return await original_engine_outline(self, state, chunk_id)

    def add_anchor(context: Any) -> Any:
        template = context.evidence_tree[0].children[0].record
        record = replace(
            template,
            id="doc-1:c5",
            chunk_uid="doc-1:c5",
            chunk_id="doc-1:c5",
            text="設定を変更します。",
            body_text="設定を変更します。",
        )
        tree = ContextParentEvidence(
            record,
            "child_fallback",
            "retrieved",
            (ContextChildEvidence(record, "retrieved_anchor", "retrieved"),),
        )
        if stage == "coverage_same_parent":
            parent = context.evidence_tree[0]
            return replace(
                context,
                evidence_tree=(
                    replace(parent, children=(*parent.children, tree.children[0])),
                    *context.evidence_tree[1:],
                ),
            )
        return _merge_crag_evidence(
            context,
            AnswerContext(records=[record], text=record.text, evidence_tree=(tree,)),
            "操作",
            max_records=20,
            max_chars=40000,
        )

    monkeypatch.setattr(section_map_service, "section_outline", outline)
    monkeypatch.setattr(section_map_service, "chunk_section", owner)
    monkeypatch.setattr(section_map_service, "read_section", read)
    monkeypatch.setattr(backend.AnswerEngine, "_section_outline", engine_outline)
    if stage == "guide":
        monkeypatch.setattr(
            backend,
            "guide_step_plan",
            lambda _: GuidePlan("guide-a", 1, (GuideStep("step-a", "設定"),)),
        )
        monkeypatch.setattr(
            answering,
            "_cover_guide_steps",
            lambda question, context, *args, **kw: (
                add_anchor(context),
                {"steps": [], "added_chunk_ids": ["doc-1:c5"]},
                (),
            ),
        )
    else:
        original = answering._cover_missing_requests

        def cover(question: str, context: Any, *args: Any, **kwargs: Any) -> Any:
            _, trace = original(question, context, *args, **kwargs)
            trace["re_retrieval"] = {"applied": True, "added_chunk_ids": ["doc-1:c5"]}
            return add_anchor(context), trace

        monkeypatch.setattr(answering, "_cover_missing_requests", cover)
        import rag_engine.adapters.oci as engine_oci
        from rag_engine.models.llm import GroundedDraft

        def explicit_gap(
            system: str, prompt: str, config: Any, schema: type, **options: Any
        ) -> Any:
            if schema in (GroundedDraft, generation_draft_wire_model()):
                generated_prompts.append(prompt)
            result = _fake_llm(system, prompt, config, schema, **options)
            if (
                schema in (GroundedDraft, generation_draft_wire_model())
                and "変更後に確認を実行します。" not in prompt
            ):
                data = result.model_dump(exclude_unset=True)
                data["items"].append(
                    {
                        "kind": "gap",
                        "text": "確認の根拠がありません。",
                        "request_id": "Q1",
                        "citations": [],
                    }
                )
                return GroundedDraft.model_validate(data)
            return result

        monkeypatch.setattr(engine_oci, "parse_text_response", explicit_gap)

    outcome = await _engine(
        rag_answer_flow="standard_rag", rag_request_coverage_retrieval_enabled=True
    ).run(SearchRequest(query="登録方法は？"))
    assert [key for key, _ in section_map.reads] == ["s1", "s2"]
    assert outcome.diagnostics["section_expansion"]["minimum_scope_complete"]
    if stage == "guide":
        assert "doc-1:c6" in {citation.chunk_id for citation in outcome.citations}
    else:
        # 追加原文は再生成へ渡る。新資料を使わない代役の回答は採用を強制しない。
        assert len(generated_prompts) >= 2
        assert "設定を変更します。" in generated_prompts[1]
