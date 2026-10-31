"""検索で当たった派生の chunk を、回答の根拠に渡す原文の chunk に置き換える（#2641）。

派生の chunk（章節の要約・RAPTOR の要約・項目抽出の値）は検索の手がかりで、引用できる原文では
ない。要約だけに含まれる値が、確かめた記載（``answer_use=confirmed``）として回答・MCP の根拠に
出ないこと、同じ章節の原文は従来どおり根拠になることを、合成の chunk と決まった応答の LLM で
確かめる（DB と OCI は呼ばない。状態はテストごとに作る）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import pytest

from app.clients.oracle import OracleClient
from app.config import Settings
from app.mcp import tools as mcp_tools
from app.rag.answer_engine import AnswerEngine
from app.rag.derived_evidence import (
    derived_kind,
    derived_source_chunks,
    is_derived_chunk,
    original_chunks,
    source_filters,
)
from app.schemas.search import RetrievedChunk, SearchMode, SearchRequest
from tests.test_answer_engine import FakeGenAi, FakeOracle, _chunk, _fake_llm

# 合成の手順の本文（``_fake_llm`` はこの本文を含む根拠を確かめた記載として引用する）。
BODY = "受注番号を入力し、登録ボタンを押します。"
SECTION = "受注入力画面"

# 派生の chunk の種類ごとの metadata（印のある chunk と、#2590 より前の印の無い chunk）。
DERIVED_METADATA: dict[str, dict[str, object]] = {
    "section_summary": {
        "content_kind": "section_summary",
        "nav_summary": True,
        "section_id": "nav-0001",
        "section_path": SECTION,
    },
    "section_summary_without_mark": {
        "content_kind": "section_summary",
        "section_path": SECTION,
    },
    "field": {
        "content_kind": "field",
        "extracted_field": True,
        "field_source_element_id": "el-7",
    },
    "field_without_mark": {"content_kind": "field", "field_source_element_id": "el-7"},
    "raptor_summary": {
        "content_kind": "summary",
        "raptor_summary": True,
        "raptor_level": 1,
        "raptor_child_indices": "0,1",
    },
}


def _derived(name: str, *, chunk_id: str = "doc-1:derived", text: str = "") -> RetrievedChunk:
    chunk = _chunk(chunk_id, text or f"{SECTION}: {BODY}", 9)
    metadata = {
        **chunk.metadata,
        "chunk_group_id": f"group-{chunk_id}",
        "parent_text": text or f"{SECTION}: {BODY}",
        "chunk_set_id": "cs-1",
        "chunk_index": 9,
        **DERIVED_METADATA[name],
    }
    return chunk.model_copy(update={"metadata": metadata})


def _original(
    chunk_id: str = "doc-1:c1",
    text: str = BODY,
    *,
    index: int = 0,
    section_path: str = SECTION,
    element_ids: str = "el-7",
) -> RetrievedChunk:
    chunk = _chunk(chunk_id, text, index + 1)
    metadata = {
        **chunk.metadata,
        "content_kind": "text",
        "chunk_set_id": "cs-1",
        "chunk_index": index,
        "section_path": section_path,
        "element_ids": element_ids,
    }
    return chunk.model_copy(update={"metadata": metadata})


class DerivedSourceStub:
    """原文を読む SQL の代役（呼ばれた条件を残す）。"""

    def __init__(self, rows: Sequence[RetrievedChunk] = (), *, fail: bool = False) -> None:
        self.rows = list(rows)
        self.fail = fail
        self.reference_calls: list[dict[str, Any]] = []
        self.chunk_set_calls: list[dict[str, Any]] = []

    async def retrieval_reference_chunks(
        self,
        filters: dict[str, str],
        *,
        document_id: str,
        section_path: str,
        chunk_set_id: str | None = None,
        limit: int,
    ) -> list[RetrievedChunk]:
        self.reference_calls.append(
            {
                "filters": dict(filters),
                "document_id": document_id,
                "section_path": section_path,
                "chunk_set_id": chunk_set_id,
                "limit": limit,
            }
        )
        if self.fail:
            raise RuntimeError("synthetic failure")
        prefix = f"{section_path} > "
        return [
            row
            for row in self.rows
            if row.document_id == document_id
            and (
                str(row.metadata.get("section_path") or "") == section_path
                or str(row.metadata.get("section_path") or "").startswith(prefix)
            )
        ][:limit]

    async def retrieval_chunk_set_chunks(
        self,
        filters: dict[str, str],
        *,
        document_id: str,
        chunk_set_id: str,
        chunk_indices: Sequence[int] = (),
        element_id: str = "",
        limit: int,
    ) -> list[RetrievedChunk]:
        self.chunk_set_calls.append(
            {
                "filters": dict(filters),
                "document_id": document_id,
                "chunk_set_id": chunk_set_id,
                "chunk_indices": list(chunk_indices),
                "element_id": element_id,
            }
        )
        if self.fail:
            raise RuntimeError("synthetic failure")
        rows = [
            row
            for row in self.rows
            if row.document_id == document_id and row.metadata.get("chunk_set_id") == chunk_set_id
        ]
        if chunk_indices:
            rows = [row for row in rows if row.metadata.get("chunk_index") in set(chunk_indices)]
        if element_id:
            # SQL と同じ部分一致（完全一致は呼び出し側が確かめる）。
            rows = [row for row in rows if element_id in str(row.metadata.get("element_ids"))]
        return sorted(rows, key=lambda row: int(str(row.metadata["chunk_index"])))[:limit]


# ---- 判定 ----


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("section_summary", "section_summary"),
        ("section_summary_without_mark", "section_summary"),
        ("field", "field"),
        ("field_without_mark", "field"),
        ("raptor_summary", "raptor_summary"),
    ],
    ids=["section-summary", "section-summary-legacy", "field", "field-legacy", "raptor"],
)
def test_derived_kind_reads_marks_and_content_kind(name: str, kind: str) -> None:
    chunk = _derived(name)
    assert is_derived_chunk(chunk)
    assert derived_kind(chunk.metadata) == kind
    assert original_chunks([chunk, _original()]) == [_original()]


def test_original_chunks_are_not_derived() -> None:
    assert derived_kind(_original().metadata) is None
    assert not is_derived_chunk(_original())


def test_source_filters_drop_only_the_content_kind_conditions() -> None:
    assert source_filters(
        {"knowledge_base_id": "kb-1", "content_kind": "section_summary", "content_kinds": "field"}
    ) == {"knowledge_base_id": "kb-1"}


# ---- 原文の解決 ----


async def test_section_summary_reads_chunks_of_the_same_section() -> None:
    stub = DerivedSourceStub(
        [
            _original("doc-1:c1", BODY, index=0),
            _original("doc-1:c2", "登録後は受注一覧に表示されます。", index=1),
            # 下の章節の chunk と、その章節の要約（派生）。
            _original("doc-1:c3", "下の章節の本文。", index=2, section_path=f"{SECTION} > 補足"),
            _derived("section_summary", chunk_id="doc-1:nav-sub"),
            # 別の文書の同じ章節の chunk。
            _original("doc-2:c1", BODY, index=0).model_copy(update={"document_id": "doc-2"}),
        ]
    )
    summary = _derived("section_summary")

    sources = await derived_source_chunks(
        stub, summary, filters={"knowledge_base_id": "kb-1", "content_kind": "section_summary"}
    )

    # 要約は章節の直下の本文から作るので、直下の chunk を読み順に返す。
    assert [chunk.chunk_id for chunk in sources] == ["doc-1:c1", "doc-1:c2"]
    call = stub.reference_calls[0]
    assert call["section_path"] == SECTION
    assert call["chunk_set_id"] == "cs-1"
    assert call["filters"] == {"knowledge_base_id": "kb-1"}


async def test_section_summary_falls_back_to_lower_sections() -> None:
    stub = DerivedSourceStub(
        [_original("doc-1:c3", "下の章節の本文。", index=2, section_path=f"{SECTION} > 補足")]
    )

    sources = await derived_source_chunks(stub, _derived("section_summary"), filters={})

    assert [chunk.chunk_id for chunk in sources] == ["doc-1:c3"]


async def test_field_reads_the_chunk_of_the_source_element_exactly() -> None:
    stub = DerivedSourceStub(
        [
            # 部分一致で当たるが、要素の ID は違う。
            _original("doc-1:c9", "別の本文。", index=3, element_ids="el-70,el-71"),
            _original("doc-1:c1", BODY, index=4, element_ids="el-6,el-7"),
        ]
    )

    sources = await derived_source_chunks(stub, _derived("field"), filters={"content_kinds": "x"})

    assert [chunk.chunk_id for chunk in sources] == ["doc-1:c1"]
    assert stub.chunk_set_calls[0]["element_id"] == "el-7"
    assert stub.chunk_set_calls[0]["filters"] == {}


async def test_raptor_summary_follows_upper_levels_down_to_the_leaves() -> None:
    upper = _derived("raptor_summary", chunk_id="doc-1:r2")
    upper.metadata.update(raptor_level=2, raptor_child_indices="10,11", chunk_index=12)
    lower = [
        _derived("raptor_summary", chunk_id="doc-1:r10"),
        _derived("raptor_summary", chunk_id="doc-1:r11"),
    ]
    lower[0].metadata.update(chunk_index=10, raptor_child_indices="0,1")
    lower[1].metadata.update(chunk_index=11, raptor_child_indices="2")
    leaves = [_original(f"doc-1:c{index}", f"本文 {index}", index=index) for index in range(3)]
    stub = DerivedSourceStub([*leaves, *lower])

    sources = await derived_source_chunks(stub, upper, filters={})

    assert [chunk.chunk_id for chunk in sources] == ["doc-1:c0", "doc-1:c1", "doc-1:c2"]
    assert [call["chunk_indices"] for call in stub.chunk_set_calls] == [[10, 11], [0, 1, 2]]


@pytest.mark.parametrize(
    ("name", "missing"),
    [
        ("section_summary", "section_path"),
        ("field", "field_source_element_id"),
        ("field", "chunk_set_id"),
        ("raptor_summary", "raptor_child_indices"),
        ("raptor_summary", "chunk_set_id"),
    ],
    ids=[
        "section-without-path",
        "field-without-source",
        "field-without-chunk-set",
        "raptor-without-children",
        "raptor-without-chunk-set",
    ],
)
async def test_derived_chunk_without_a_pointer_has_no_source(name: str, missing: str) -> None:
    chunk = _derived(name)
    chunk.metadata.pop(missing, None)
    stub = DerivedSourceStub([_original()])

    assert await derived_source_chunks(stub, chunk, filters={}) == []


async def test_source_read_failure_gives_no_source() -> None:
    stub = DerivedSourceStub([_original()], fail=True)

    assert await derived_source_chunks(stub, _derived("section_summary"), filters={}) == []


# ---- 回答・MCP の根拠 ----


class DerivedHitOracle(FakeOracle):
    """検索が派生の chunk だけを返し、原文は ``sources`` から読む Oracle の代役。"""

    def __init__(self, hit: RetrievedChunk, sources: Sequence[RetrievedChunk]) -> None:
        super().__init__()
        self.hit = hit
        self.stub = DerivedSourceStub(sources)

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        self.filters.append(dict(filters or {}))
        return [self.hit]

    async def context_group_siblings(
        self,
        anchors: list[RetrievedChunk],
        *,
        max_chunks_per_group: int,
        neighbor_child_count: int | None = None,
    ) -> list[RetrievedChunk]:
        # 同じ親の前後の文脈に派生の chunk が混じっても、根拠に渡さない。
        return [_derived("section_summary", chunk_id="doc-1:nav-sibling")]

    async def retrieval_reference_chunks(self, *args: Any, **kwargs: Any) -> list[RetrievedChunk]:
        return await self.stub.retrieval_reference_chunks(*args, **kwargs)

    async def retrieval_chunk_set_chunks(self, *args: Any, **kwargs: Any) -> list[RetrievedChunk]:
        return await self.stub.retrieval_chunk_set_chunks(*args, **kwargs)


def _prompts_llm(prompts: list[str]) -> Any:
    def llm(system: str, prompt: str, settings: Any, schema: type, **options: Any) -> Any:
        prompts.append(prompt)
        return _fake_llm(system, prompt, settings, schema, **options)

    return llm


async def _answer(
    monkeypatch: pytest.MonkeyPatch,
    oracle: DerivedHitOracle,
    *,
    filters: dict[str, str] | None = None,
) -> tuple[Any, list[str]]:
    import rag_engine.adapters.oci as engine_oci

    prompts: list[str] = []
    monkeypatch.setattr(engine_oci, "parse_text_response", _prompts_llm(prompts))
    engine = AnswerEngine(
        Settings(rag_domain_keywords=["受注番号"]),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )
    request = SearchRequest(
        query="受注の登録方法は？", filters=filters or {"knowledge_base_id": "kb-1"}
    )
    return await engine.run(request), prompts


DERIVED_NAMES = ["section_summary", "section_summary_without_mark", "field", "raptor_summary"]
DERIVED_IDS = ["section-summary", "section-summary-legacy", "field", "raptor"]


@pytest.mark.parametrize("name", DERIVED_NAMES, ids=DERIVED_IDS)
async def test_value_only_in_a_derived_chunk_is_not_confirmed_evidence(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """原文が読めない派生の chunk は根拠に渡さず、要約の値を確かめた記載として引用しない。"""
    hit = _derived(name)
    oracle = DerivedHitOracle(hit, sources=[hit])

    outcome, prompts = await _answer(monkeypatch, oracle)

    assert not any(is_derived_chunk(chunk) for chunk in outcome.citations)
    assert all(hit.text not in prompt for prompt in prompts)
    assert BODY not in outcome.answer
    evidence = [mcp_tools._evidence(chunk) for chunk in outcome.citations]
    assert not any(item.answer_use == "confirmed" for item in evidence)
    assert not any(BODY in item.excerpt for item in evidence)
    derived = outcome.diagnostics["derived_evidence"]
    assert derived["unresolved_count"] == 1
    assert derived["chunks"][0]["chunk_id"] == hit.chunk_id
    assert derived["chunks"][0]["source_chunk_ids"] == []


@pytest.mark.parametrize("name", DERIVED_NAMES, ids=DERIVED_IDS)
async def test_derived_hit_passes_its_original_chunk_as_confirmed_evidence(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """派生の chunk が指す原文は、従来どおり確かめた記載として回答・MCP の根拠になる。"""
    hit = _derived(name)
    original = _original("doc-1:c1", BODY, index=0, element_ids="el-7")
    oracle = DerivedHitOracle(hit, sources=[original, hit])

    outcome, prompts = await _answer(
        monkeypatch,
        oracle,
        filters={"knowledge_base_id": "kb-1", "content_kind": "section_summary"},
    )

    assert BODY in outcome.answer
    assert [chunk.chunk_id for chunk in outcome.citations] == ["doc-1:c1"]
    assert outcome.citations[0].metadata["evidence_answer_use"] == "confirmed"
    evidence = mcp_tools._evidence(outcome.citations[0])
    assert (evidence.chunk_id, evidence.used_in_answer, evidence.answer_use) == (
        "doc-1:c1",
        True,
        "confirmed",
    )
    assert evidence.evidence_type == "text"
    assert all(hit.text not in prompt for prompt in prompts)
    assert outcome.diagnostics["derived_evidence"]["resolved_count"] == 1
    # 原文は検索と同じ条件で読み、chunk の種類の条件だけを外す。
    calls = [*oracle.stub.reference_calls, *oracle.stub.chunk_set_calls]
    assert calls
    assert all(call["filters"].get("knowledge_base_id") == "kb-1" for call in calls)
    assert all("content_kind" not in call["filters"] for call in calls)


async def test_original_already_in_the_candidates_keeps_its_position(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """検索が原文と要約の両方を返すとき、原文は 1 回だけ根拠になる。"""
    original = _original("doc-1:c1", BODY, index=0)
    summary = _derived("section_summary")

    class BothOracle(DerivedHitOracle):
        async def hybrid_search(self, *args: Any, **kwargs: Any) -> list[RetrievedChunk]:
            return [original, summary]

    outcome, _prompts = await _answer(monkeypatch, BothOracle(summary, sources=[original]))

    assert [chunk.chunk_id for chunk in outcome.citations] == ["doc-1:c1"]
    assert outcome.citations[0].metadata["evidence_answer_use"] == "confirmed"


async def test_retrieve_returns_originals_instead_of_derived_hits() -> None:
    """回答を作らない検索(KB の検索テスト)も、派生の chunk の代わりに原文を返す。"""
    original = _original("doc-1:c1", BODY, index=0)
    oracle = DerivedHitOracle(_derived("section_summary"), sources=[original])
    engine = AnswerEngine(
        Settings(),
        oracle=oracle,  # type: ignore[arg-type]
        genai=FakeGenAi(),  # type: ignore[arg-type]
    )

    chunks = await engine.retrieve(SearchRequest(query="受注の登録", top_k=3))

    assert [chunk.chunk_id for chunk in chunks] == ["doc-1:c1"]


# ---- Oracle の SQL ----


async def test_oracle_chunk_set_chunks_use_search_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OracleClient(settings=Settings())
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_fetch_all(
        statement: str, binds: dict[str, object] | None = None
    ) -> list[dict[str, object]]:
        calls.append((statement, dict(binds or {})))
        return [
            {
                "document_id": "doc-1",
                "chunk_id": "doc-1:c1",
                "chunk_text": BODY,
                "metadata_json": json.dumps({"element_ids": "el-7"}),
                "chunk_index": 4,
                "chunk_set_id": "cs-1",
                "file_name": "manual.pdf",
            }
        ]

    monkeypatch.setattr(client, "_fetch_all", fake_fetch_all)
    filters = {"knowledge_base_id": "kb-1", "chunk_set_id": "cs-other"}

    rows = await client.retrieval_chunk_set_chunks(
        filters, document_id="doc-1", chunk_set_id="cs-1", chunk_indices=[3, 1, 3], limit=5
    )
    await client.retrieval_chunk_set_chunks(
        filters, document_id="doc-1", chunk_set_id="cs-1", element_id="el-7", limit=2
    )

    assert [row.chunk_id for row in rows] == ["doc-1:c1"]
    assert rows[0].metadata["chunk_index"] == 4
    index_sql, index_binds = calls[0]
    assert "kb-1" in index_binds.values()
    assert index_binds["filter_document_id"] == "doc-1"
    assert index_binds["source_chunk_set_id"] == "cs-1"
    assert [index_binds["source_chunk_index_0"], index_binds["source_chunk_index_1"]] == [1, 3]
    assert index_binds["source_limit"] == 5
    # 版は有効な chunk_set の中で絞る（filters の chunk_set_id で有効でない版を読まない）。
    assert "filter_chunk_set_id" not in index_binds
    assert "active_cs.is_active = 1" in index_sql
    assert "c.chunk_index IN" in index_sql
    element_sql, element_binds = calls[1]
    assert element_binds["source_element_id"] == "el-7"
    assert "$.element_ids" in element_sql
    assert "c.chunk_index IN" not in element_sql
    # 絞る値が無ければ読まない。
    assert (
        await client.retrieval_chunk_set_chunks(
            filters, document_id="doc-1", chunk_set_id="cs-1", limit=5
        )
        == []
    )
    assert len(calls) == 2


async def test_rule_summary_resolves_original_elements_when_parser_section_path_differs() -> None:
    summary = _derived("section_summary").model_copy(
        update={
            "metadata": {
                **_derived("section_summary").metadata,
                "section_path": "規則の章節 A",
                "nav_source_element_ids": "el-7",
            }
        }
    )
    original = _original(section_path="解析見出し A")
    reader = DerivedSourceStub([summary, original])
    assert await derived_source_chunks(
        reader, summary, filters={"content_kind": "section_summary"}
    ) == [original]
    assert reader.reference_calls == []
    assert reader.chunk_set_calls[0]["element_id"] == "el-7"
    assert "content_kind" not in reader.chunk_set_calls[0]["filters"]
    missing = DerivedSourceStub([_original(section_path="規則の章節 A", element_ids="other-id")])
    assert await derived_source_chunks(missing, summary, filters={}) == []
