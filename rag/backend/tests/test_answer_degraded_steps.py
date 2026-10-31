"""回答の検索の補助の処理の失敗を、診断で「当たらなかった」と分ける(#3259)。

補助の処理(業務の名前・実体・関係情報・参照先・画面目録・章節・編集したプロンプト)は、
失敗しても空のまま回答を続ける。失敗は ``_SearchState.degraded`` に処理の種類・回数・例外の型の
名前だけを残し、回答の診断(``degraded_steps``)と根拠 0 件の診断(``zero_evidence``)に出す。
例外の文・SQL・本文・識別子は出さない。外部 I/O はすべてスタブ。
"""

import json
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, cast

import pytest

from app.config import Settings
from app.rag import answer_engine as answer_engine_module
from app.rag.answer_engine import AnswerEngine, _SearchState
from app.rag.cross_references import ReferenceTarget
from app.schemas.search import RetrievedChunk, SearchMode, SearchRequest
from tests.test_answer_engine import (
    EmptyWithKeysOracle,
    FakeGenAi,
    FakeOracle,
    ScreenOracle,
    _chunk,
    _fake_llm,
)
from tests.test_cross_references import (
    CHAPTER3,
    ReferenceOracle,
    _change_window_corpus,
    _retrieved,
    _section_chunks,
)
from tests.test_cross_references import FakeGenAi as ReferenceGenAi
from tests.test_cross_references import _engine as reference_engine

# 例外の文に入れる値。診断のどこにも出てはいけない(本文・識別子・SQL の代役)。
SECRET = "ORA-00942 SELECT secret_column FROM doc-secret"


class Boom(RuntimeError):
    """補助の処理の失敗の代役(型の名前だけが診断に出る)。"""


def _boom(*args: Any, **kwargs: Any) -> Any:
    raise Boom(SECRET)


async def _aboom(*args: Any, **kwargs: Any) -> Any:
    raise Boom(SECRET)


def _engine(oracle: Any, **settings: Any) -> AnswerEngine:
    return AnswerEngine(Settings(**settings), oracle=oracle, genai=FakeGenAi())  # type: ignore[arg-type]


def _assert_degraded(state: _SearchState, kind: str) -> None:
    assert state.degraded_steps() == [{"kind": kind, "count": 1, "exc_type": "Boom"}]
    assert SECRET not in json.dumps(state.degraded_steps(), ensure_ascii=False)


# ---- 処理ごとに、失敗(記録が残る)と空(記録が残らない)を分ける ----


class _ScopeOracle(FakeOracle):
    """画面目録・条の文書の一覧が読む範囲の状態と一覧を返す(``fail`` なら失敗する)。"""

    def __init__(self, *, fail: bool) -> None:
        super().__init__()
        self.fail = fail

    async def retrieval_large_categories(self, filters: dict[str, str]) -> list[str]:
        if self.fail:
            raise Boom(SECRET)
        return []

    async def retrieval_scope_state(self, filters: dict[str, str]) -> str:
        if self.fail:
            raise Boom(SECRET)
        return "1:1"

    async def retrieval_screen_sections(
        self, filters: dict[str, str]
    ) -> list[tuple[str, str, int]]:
        return []

    async def retrieval_screen_chunks(
        self, filters: dict[str, str], *, file_name: str, heading: str, limit: int
    ) -> list[RetrievedChunk]:
        if self.fail:
            raise Boom(SECRET)
        return []


@pytest.mark.parametrize("fail", [True, False], ids=["error", "empty"])
async def test_business_names_failure_is_recorded(fail: bool) -> None:
    state = _SearchState()

    names = await _engine(_ScopeOracle(fail=fail))._business_names(SearchRequest(query="q"), state)

    assert names == []
    if fail:
        _assert_degraded(state, "business_names")
    else:
        assert state.degraded_steps() == []


@pytest.mark.parametrize("fail", [True, False], ids=["error", "empty"])
async def test_screen_catalog_failure_is_recorded(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    monkeypatch.setattr(answer_engine_module, "_SCREEN_CATALOG_CACHE", {})
    state = _SearchState()

    catalog = await _engine(_ScopeOracle(fail=fail))._load_screen_catalog(
        SearchRequest(query="q"), state
    )

    assert catalog == {}
    if fail:
        _assert_degraded(state, "screen_catalog")
    else:
        assert state.degraded_steps() == []


@pytest.mark.parametrize("fail", [True, False], ids=["error", "empty"])
async def test_screen_chunks_failure_is_recorded(fail: bool) -> None:
    state = _SearchState()

    stored = await _engine(_ScopeOracle(fail=fail))._screen_chunks(
        SearchRequest(query="q"), state, [("画面A.pdf", "画面 A")], set()
    )

    assert stored == []
    if fail:
        _assert_degraded(state, "screen_chunks")
    else:
        assert state.degraded_steps() == []


@pytest.mark.parametrize("fail", [True, False], ids=["error", "empty"])
async def test_section_outline_failure_is_recorded(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    from app.rag import section_map_service

    async def chunk_section(document_id: str, chunk_id: str, **kwargs: Any) -> Any:
        if fail:
            raise Boom(SECRET)
        return None

    monkeypatch.setattr(section_map_service, "chunk_section", chunk_section)
    state = _SearchState()
    chunk = _chunk("doc-1:c1", "本文", 1)
    state.chunks[chunk.chunk_id] = chunk

    assert await _engine(FakeOracle())._section_outline(state, chunk.chunk_id) is None
    if fail:
        _assert_degraded(state, "section_outline")
    else:
        assert state.degraded_steps() == []


async def test_section_chunks_failure_is_recorded_with_the_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """章節の読み取りの失敗は、今までの ``read_error`` に加えて同じ記録にも残す。"""
    from app.rag import section_map_service

    monkeypatch.setattr(section_map_service, "read_section", _aboom)
    state = _SearchState()

    read = await _engine(FakeOracle())._section_chunks(
        SearchRequest(query="q"), state, "doc-a", "cs-a", "s1"
    )

    assert getattr(read, "reason", "") == "read_error"
    _assert_degraded(state, "section_chunks")


@pytest.mark.parametrize(
    ("planner", "kind", "call"),
    [
        (
            "plan_entity_expansion",
            "entity_expansion",
            lambda engine, request, state, seeds: engine._entity_expansion(request, state, seeds),
        ),
        (
            "plan_graph_expansion",
            "graph_expansion",
            lambda engine, request, state, seeds: engine._graph_expansion(
                request, state, seeds, exclude=set()
            ),
        ),
    ],
    ids=["entity", "graph"],
)
@pytest.mark.parametrize("fail", [True, False], ids=["error", "empty"])
async def test_expansion_planner_failure_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
    planner: str,
    kind: str,
    call: Callable[..., Awaitable[list[RetrievedChunk]]],
    fail: bool,
) -> None:
    async def empty(*args: Any, **kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(answer_engine_module, planner, _aboom if fail else empty)
    engine = _engine(
        FakeOracle(), rag_entity_expansion_enabled=True, rag_graph_expansion_enabled=True
    )
    state = _SearchState()
    request = SearchRequest(query="q", filters={"knowledge_base_id": "kb-1"})

    added = await call(engine, request, state, [_chunk("doc-1:c1", "本文", 1)])

    assert added == []
    if fail:
        _assert_degraded(state, kind)
    else:
        assert state.degraded_steps() == []


# ---- 参照先(解決・読み取り・先読み・並べ替え) ----


async def test_reference_load_failure_is_recorded() -> None:
    anchor = _retrieved("doc-1:a", "第3章を参照。", "第1章 総則", targets=[CHAPTER3])
    oracle = ReferenceOracle([anchor], _section_chunks(2))
    oracle.fail = True
    state = _SearchState()

    result = await reference_engine(oracle)._search(
        SearchRequest(query="q"), state, retrieval_queries=["q"]
    )

    assert [child.chunk_uid for child in result.child_chunks] == ["doc-1:a"]
    steps = state.degraded_steps()
    assert steps == [{"kind": "reference_load", "count": 1, "exc_type": "RuntimeError"}]


async def test_reference_without_target_rows_is_not_degraded() -> None:
    """参照先の節が無い(当たらなかった)ときは、失敗として残さない。"""
    anchor = _retrieved("doc-1:a", "第3章を参照。", "第1章 総則", targets=[CHAPTER3])
    state = _SearchState()

    await reference_engine(ReferenceOracle([anchor], []))._search(
        SearchRequest(query="q"), state, retrieval_queries=["q"]
    )

    assert state.degraded_steps() == []


async def test_reference_resolve_failure_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    anchor = _retrieved("doc-1:a", "第3章を参照。", "第1章 総則", targets=[CHAPTER3])
    engine = reference_engine(ReferenceOracle([anchor], _section_chunks(2)))
    monkeypatch.setattr(engine, "_anchor_reference_targets", _aboom)
    state = _SearchState()

    result = await engine._search(SearchRequest(query="q"), state, retrieval_queries=["q"])

    assert [child.chunk_uid for child in result.child_chunks] == ["doc-1:a"]
    _assert_degraded(state, "reference_resolve")


async def test_reference_prefetch_failure_is_recorded() -> None:
    """文書名の参照の先読みに失敗しても、1 つずつ照合して続け、失敗は記録に残す。"""
    target = ReferenceTarget("別文書", "document", "別文書", document_title="別文書")
    anchor = _retrieved("doc-1:a", "別文書を参照。", "第1章 総則", targets=[target])
    oracle = ReferenceOracle([anchor], [])
    oracle.fail_batch = True
    state = _SearchState()

    await reference_engine(oracle)._search(
        SearchRequest(query="q", filters={"knowledge_base_id": "kb-1"}),
        state,
        retrieval_queries=["q"],
    )

    assert oracle.document_batch_calls
    assert state.degraded_steps() == [
        {"kind": "reference_prefetch", "count": 1, "exc_type": "RuntimeError"}
    ]


@pytest.mark.parametrize("fail", [True, False], ids=["error", "ok"])
async def test_reference_rerank_failure_is_recorded(fail: bool) -> None:
    from tests.test_cross_references import _PLAN_KB

    window, sections = _change_window_corpus("query")
    oracle = ReferenceOracle(
        [window], sections, knowledge_bases={_PLAN_KB: {"doc-change", "doc-plan"}}
    )
    genai = ReferenceGenAi({"共通の保守枠は": 0.9}, fail=fail)
    state = _SearchState()

    await reference_engine(oracle, genai)._search(
        SearchRequest(query="q", filters={"knowledge_base_id": _PLAN_KB}),
        state,
        retrieval_queries=["q"],
    )

    assert len(genai.rerank_calls) == 1
    expected = [{"kind": "reference_rerank", "count": 1, "exc_type": "RuntimeError"}]
    assert state.degraded_steps() == (expected if fail else [])


# ---- 記録の形 ----


def test_degraded_record_counts_per_kind_and_type_without_messages() -> None:
    state = _SearchState()

    state.record_degraded("screen_chunks", Boom(SECRET))
    state.record_degraded("screen_chunks", Boom("別の文"))
    state.record_degraded("screen_chunks", TimeoutError(SECRET))
    state.record_degraded("section_outline", Boom(SECRET))

    assert state.degraded_steps() == [
        {"kind": "screen_chunks", "count": 2, "exc_type": "Boom"},
        {"kind": "screen_chunks", "count": 1, "exc_type": "TimeoutError"},
        {"kind": "section_outline", "count": 1, "exc_type": "Boom"},
    ]
    assert SECRET not in json.dumps(state.degraded_steps(), ensure_ascii=False)


# ---- 回答の診断(回答を続け、0 件の診断で「当たらなかった」と分ける) ----


class _OverridesOracle(EmptyWithKeysOracle):
    """編集したプロンプトの読み込みを成功・失敗させる(``blocked`` の項目があれば 0 件)。"""

    def __init__(self, *blocked: str, fail_overrides: bool) -> None:
        super().__init__(*blocked)
        self.fail_overrides = fail_overrides

    async def answer_prompt_overrides(self) -> dict[str, str]:
        if self.fail_overrides:
            raise Boom(SECRET)
        return {}


async def _run(monkeypatch: pytest.MonkeyPatch, oracle: FakeOracle) -> Any:
    import rag_engine.adapters.oci as engine_oci

    monkeypatch.setattr(engine_oci, "parse_text_response", _fake_llm)
    # 実体・関係情報の拡張は、この試験の外(FakeOracle に無い SQL を読む。処理ごとの試験は
    # 上にある)。章節の拡張は既定のまま(FakeOracle の文書は章節の対応を持たず、広げない)。
    engine = _engine(
        oracle,
        rag_entity_expansion_enabled=False,
        rag_graph_expansion_enabled=False,
    )
    return await engine.run(
        SearchRequest(query="登録の手順は？", filters={"knowledge_base_id": "kb-1"})
    )


@pytest.mark.parametrize("fail", [True, False], ids=["error", "no-hit"])
async def test_zero_evidence_separates_read_failure_from_no_hit(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    """根拠 0 件のとき、補助の処理が失敗していれば 0 件の診断から失敗した処理が分かる。"""
    oracle = _OverridesOracle("knowledge_base_id", fail_overrides=fail)

    outcome = await _run(monkeypatch, oracle)

    assert outcome.citations == []
    zero = outcome.diagnostics["zero_evidence"]
    assert zero["stage"] == "retrieval"
    if fail:
        expected = [{"kind": "prompt_overrides", "count": 1, "exc_type": "Boom"}]
        assert zero["degraded_steps"] == expected
        assert outcome.diagnostics["degraded_steps"] == expected
    else:
        assert zero["degraded_steps"] == []
        assert "degraded_steps" not in outcome.diagnostics
    assert SECRET not in json.dumps(outcome.diagnostics, ensure_ascii=False, default=str)


async def test_shared_fake_oracle_answers_without_degraded_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共通の FakeOracle は既定の設定で補助の処理を失敗させない(編集したプロンプト・章節の対応)。

    代役に無い読み取りが ``AttributeError`` で degraded_steps に混ざると、各試験の診断が
    実際の失敗と見分けられない。
    """
    import rag_engine.adapters.oci as engine_oci

    monkeypatch.setattr(engine_oci, "parse_text_response", _fake_llm)
    # 章節の拡張は既定で有効(章節の対応を読む)。
    assert Settings().rag_section_expansion_enabled
    engine = _engine(FakeOracle())

    outcome = await engine.run(
        SearchRequest(query="登録の手順は？", filters={"knowledge_base_id": "kb-1"})
    )

    assert outcome.citations
    assert "degraded_steps" not in outcome.diagnostics


async def test_answer_continues_and_records_degraded_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """補助の処理が失敗しても回答を続け(拒答にしない)、回答の診断に処理の種類を残す。"""
    oracle = _OverridesOracle(fail_overrides=True)

    outcome = await _run(monkeypatch, oracle)

    assert outcome.citations
    assert "zero_evidence" not in outcome.diagnostics
    assert outcome.diagnostics["degraded_steps"] == [
        {"kind": "prompt_overrides", "count": 1, "exc_type": "Boom"}
    ]
    assert SECRET not in json.dumps(outcome.diagnostics, ensure_ascii=False, default=str)


async def test_screen_link_failure_in_rag_engine_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rag_engine の中の画面の選択(LLM)の失敗も、同じ記録に残す(「該当なし」と分ける)。"""
    import rag_engine.adapters.oci as engine_oci
    from rag_engine.generation import answering
    from rag_engine.retrieval.screen_catalog import ScreenLinks

    def failing_llm(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
        if schema is ScreenLinks:
            raise Boom(SECRET)
        return _fake_llm(system, prompt, settings, schema, **options)

    monkeypatch.setattr(engine_oci, "parse_text_response", failing_llm)
    monkeypatch.setattr(answer_engine_module, "_SCREEN_CATALOG_CACHE", {})
    monkeypatch.setattr(answering, "_SCREEN_LINK_CACHE", {})
    oracle = ScreenOracle()
    engine = _engine(
        oracle,
        rag_query_strategy="simple_retrieval",
        rag_answer_flow="standard_rag",
        rag_rerank_enabled=False,
        rag_screen_linking_enabled=True,
    )

    outcome = await engine.run(
        SearchRequest(query="帳票が印字されない", top_k=10, filters={"knowledge_base_id": "kb-1"})
    )

    assert outcome.citations
    assert oracle.section_calls and oracle.screen_calls == []
    steps = cast(list[dict[str, Any]], outcome.diagnostics["degraded_steps"])
    assert {"kind": "screen_link", "count": 1, "exc_type": "Boom"} in steps
    assert SECRET not in json.dumps(outcome.diagnostics, ensure_ascii=False, default=str)


async def test_main_search_failure_is_an_error_not_zero_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主の検索の失敗は 0 件に変えず、エラーとして返す(補助の処理とは扱いが違う)。"""

    class FailingSearch(_OverridesOracle):
        async def hybrid_search(
            self,
            query: str,
            embedding: list[float],
            top_k: int,
            mode: SearchMode = SearchMode.HYBRID,
            filters: dict[str, str] | None = None,
        ) -> list[RetrievedChunk]:
            raise Boom(SECRET)

    from rag_engine.generation.execution_record import AnswerExecutionError

    with pytest.raises(AnswerExecutionError) as caught:
        await _run(monkeypatch, FailingSearch(fail_overrides=False))
    assert isinstance(caught.value.__cause__, Boom)


# ---- 回答エンジンの前の段(会話の書き換え・抽出項目の条件・会話の添付。#3286) ----


@pytest.mark.parametrize("fail", [True, False], ids=["error", "not-needed"])
async def test_history_rewrite_failure_reaches_answer_diagnostics(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    """書き換えの失敗は元の質問で回答を続け、回答の診断の degraded_steps に出す。

    書き換えが要らなかった(書き換えの結果が元の質問と同じ)ときは失敗にしない。
    """
    from tests.test_answer_engine import RewriteLlm, _run_chat

    llm = RewriteLlm(Boom(SECRET) if fail else json.dumps({"question": "それの登録方法は？"}))

    response, oracle = await _run_chat(monkeypatch, llm, history=True)

    assert llm.calls
    assert oracle.queries[0] == "それの登録方法は?"
    answer = cast(dict[str, Any], response.diagnostics.answer)
    assert answer["rewritten_question"] == ""
    assert response.answer
    steps = cast(list[dict[str, Any]], answer.get("degraded_steps", []))
    expected = {"kind": "history_rewrite", "count": 1, "exc_type": "Boom"}
    assert (expected in steps) is fail
    assert SECRET not in json.dumps(answer, ensure_ascii=False, default=str)


@pytest.mark.parametrize("reply", [Boom(SECRET), "[]"], ids=["error", "no-condition"])
async def test_field_filter_read_failure_is_recorded(reply: Any) -> None:
    from app.rag.degraded_steps import DegradedSteps
    from app.rag.extraction_field_adapter import FieldDefinition
    from app.rag.field_filter_reader import read_field_conditions
    from tests.test_answer_engine import RewriteLlm

    degraded = DegradedSteps()

    conditions = await read_field_conditions(
        "10万円以上の受注は？",
        [FieldDefinition(name="金額", value_type="number")],
        RewriteLlm(reply),  # type: ignore[arg-type]
        report_degraded=degraded.record_degraded,
    )

    assert conditions == []
    expected = [{"kind": "field_filter", "count": 1, "exc_type": "Boom"}]
    assert degraded.steps() == (expected if isinstance(reply, Exception) else [])


@pytest.mark.parametrize("fail", [True, False], ids=["error", "no-definition"])
async def test_field_definitions_load_failure_is_recorded(fail: bool) -> None:
    """定義を読めなかったときは記録し、定義が無いときは記録しない(どちらも LLM を呼ばない)。"""
    from app.rag.degraded_steps import DegradedSteps
    from app.rag.pipeline import RagPipeline
    from tests.test_answer_engine import RewriteLlm

    class DefinitionOracle(FakeOracle):
        async def get_search_answer_profile(self, search_answer_profile_id: str) -> Any:
            if fail:
                raise Boom(SECRET)
            return None

    llm = RewriteLlm("[]")
    pipeline = RagPipeline(
        settings=Settings(rag_auto_field_filter_enabled=True),
        oracle=DefinitionOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        llm=llm,  # type: ignore[arg-type]
    )
    degraded = DegradedSteps()

    conditions = await pipeline._read_field_conditions(
        SearchRequest(query="q", search_answer_profile_id="bv-1"),
        report_degraded=degraded.record_degraded,
    )

    assert conditions == [] and llm.calls == []
    expected = [{"kind": "field_filter", "count": 1, "exc_type": "Boom"}]
    assert degraded.steps() == (expected if fail else [])


@pytest.fixture
def attachment_service() -> Iterator[Any]:
    from app.rag import chat_attachments
    from tests.test_chat_attachments import _service

    created = _service()
    chat_attachments.set_chat_attachment_service(created)
    yield created
    chat_attachments.set_chat_attachment_service(None)


@pytest.mark.parametrize("fail", [True, False], ids=["error", "ok"])
def test_chat_attachment_rerank_failure_is_recorded(attachment_service: Any, fail: bool) -> None:
    import asyncio

    from app.rag.chat_attachments import attachment_units, select_attachment_units
    from app.rag.degraded_steps import DegradedSteps
    from tests.test_chat_attachments import CSV, _upload

    _upload(attachment_service, "b.csv", CSV * 5, "conv-large")
    toolset = attachment_service.toolset("conv-large")
    units = attachment_units(toolset.records, toolset.parsed)

    async def rerank(question: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        if fail:
            raise Boom(SECRET)
        return [(0, 0.9)]

    degraded = DegradedSteps()
    selected = asyncio.run(
        select_attachment_units(
            "A-25", units, rerank=rerank, report_degraded=degraded.record_degraded
        )
    )

    # 失敗しても文字の一致で選んで続ける。
    assert selected
    expected = [{"kind": "chat_attachment_rerank", "count": 1, "exc_type": "Boom"}]
    assert degraded.steps() == (expected if fail else [])


def test_unreadable_chat_attachment_is_recorded(attachment_service: Any) -> None:
    """解析できない添付は根拠に入れずに続け、読めなかったことを記録する(読めた添付は記録しない)。"""
    import asyncio
    import io
    import zipfile

    from app.rag.chat_attachments import attachment_evidence
    from app.rag.degraded_steps import DegradedSteps
    from tests.test_chat_attachments import ALICE, TEXT, _upload

    _upload(attachment_service, "a.md", TEXT, "conv-e")
    readable = DegradedSteps()
    first = asyncio.run(
        attachment_evidence(
            "保管の期限", "conv-e", rerank=None, report_degraded=readable.record_degraded
        )
    )
    assert first and readable.steps() == []

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", "<w:document>")
    broken = asyncio.run(
        attachment_service.upload(ALICE, file_name="b.docx", data=buffer.getvalue())
    )
    attachment_service.attach_to_conversation(
        ALICE, [broken.attachment_id], conversation_id="conv-e", owner_user_uuid=ALICE.user_uuid
    )
    degraded = DegradedSteps()

    chunks = asyncio.run(
        attachment_evidence(
            "保管の期限", "conv-e", rerank=None, report_degraded=degraded.record_degraded
        )
    )

    assert {chunk.file_name for chunk in chunks} == {"a.md"}
    steps = degraded.steps()
    assert [step["kind"] for step in steps] == ["chat_attachments"]
    assert steps[0]["count"] == 1


@pytest.mark.parametrize("fail", [True, False], ids=["error", "no-attachment"])
async def test_chat_attachments_failure_reaches_answer_diagnostics(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    """会話の添付を読めないときは添付なしで回答を続け、回答の診断に出す(添付が無いときは出さない)。"""
    import rag_engine.adapters.oci as engine_oci

    from app.rag import chat_attachments
    from app.rag.pipeline import RagPipeline
    from tests.test_answer_engine import QueryRecordingOracle, RewriteLlm

    class Service:
        def reading_status(self) -> str:
            return "done"

        def toolset(self, conversation_id: str, *, observe: bool = True) -> Any:
            if fail:
                raise Boom(SECRET)
            return None

    monkeypatch.setattr(engine_oci, "parse_text_response", _fake_llm)
    chat_attachments.set_chat_attachment_service(Service())  # type: ignore[arg-type]
    try:
        pipeline = RagPipeline(
            settings=Settings(),
            oracle=QueryRecordingOracle(),  # type: ignore[arg-type]
            genai=FakeGenAi(),  # type: ignore[arg-type]
            llm=RewriteLlm("unused"),  # type: ignore[arg-type]
            attachment_conversation_id="conv-1",
        )
        response = await pipeline.run(SearchRequest(query="受注の登録方法は？"))
    finally:
        chat_attachments.set_chat_attachment_service(None)

    assert response.answer
    answer = cast(dict[str, Any], response.diagnostics.answer)
    steps = cast(list[dict[str, Any]], answer.get("degraded_steps", []))
    expected = {"kind": "chat_attachments", "count": 1, "exc_type": "Boom"}
    assert (expected in steps) is fail
    assert SECRET not in json.dumps(answer, ensure_ascii=False, default=str)


async def test_pre_answer_degraded_steps_reach_approved_faq_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """承認済み FAQ から答えるときも、前の段の縮退を回答の診断に出す。"""
    import rag_engine.adapters.oci as engine_oci
    from rag_engine.generation.faq_answer import FaqAnswerDraft

    from app.rag.degraded_steps import DegradedSteps

    monkeypatch.setattr(
        engine_oci,
        "parse_text_response",
        lambda *args, **kwargs: FaqAnswerDraft(answer="日当は 1 泊 2,000 円です。"),
    )
    degraded = DegradedSteps()
    degraded.record_degraded("history_rewrite", Boom(SECRET))
    engine = AnswerEngine(
        Settings(),
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
        approved_faq=("出張の日当はいくらですか？", "一般は 1 泊 2,000 円です。"),
        degraded=degraded,
    )

    outcome = await engine.run(SearchRequest(query="出張したら日当は何円？"))

    assert outcome.diagnostics["degraded_steps"] == [
        {"kind": "history_rewrite", "count": 1, "exc_type": "Boom"}
    ]


# ---- rerank の失敗と診断のモデル(#3286) ----


@pytest.mark.parametrize("fail", [True, False], ids=["error", "ok"])
async def test_rerank_failure_does_not_show_the_rerank_model(
    monkeypatch: pytest.MonkeyPatch, fail: bool
) -> None:
    """rerank が失敗したら取得の順で回答を続け、models.rerank にモデル名を出さない。"""
    import rag_engine.adapters.oci as engine_oci

    class RerankGenAi(FakeGenAi):
        async def rerank(
            self, query: str, documents: list[str], top_n: int
        ) -> list[tuple[int, float]]:
            if fail:
                raise Boom(SECRET)
            return await super().rerank(query, documents, top_n)

    monkeypatch.setattr(engine_oci, "parse_text_response", _fake_llm)
    settings = Settings(
        embedding_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        rag_query_strategy="simple_retrieval",
        rag_answer_flow="standard_rag",
        rag_rerank_enabled=True,
    )
    engine = AnswerEngine(
        settings,
        oracle=FakeOracle(),  # type: ignore[arg-type]
        genai=RerankGenAi(),  # type: ignore[arg-type]
    )

    outcome = await engine.run(SearchRequest(query="受注の登録方法は？"))

    assert outcome.citations
    steps = cast(list[dict[str, Any]], outcome.diagnostics.get("degraded_steps", []))
    rerank_steps = [step for step in steps if step["kind"] == "rerank"]
    assert settings.oci_genai_rerank_model
    if fail:
        assert rerank_steps == [{"kind": "rerank", "count": 1, "exc_type": "Boom"}]
        assert outcome.diagnostics["models"]["rerank"] == ""
    else:
        assert rerank_steps == []
        assert outcome.diagnostics["models"]["rerank"] == settings.oci_genai_rerank_model
    assert SECRET not in json.dumps(outcome.diagnostics, ensure_ascii=False, default=str)


def test_unconfigured_rerank_does_not_show_the_rerank_model() -> None:
    """Base URL が無いと rag_engine は rerank しないので、models.rerank にモデル名を出さない。"""
    engine = _engine(FakeOracle(), embedding_base_url="", rag_rerank_enabled=True)

    assert engine._models_used()["rerank"] == ""
