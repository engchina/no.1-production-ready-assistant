"""回答の検索の DB の往復を減らしても、検索の結果が変わらないこと(#1490・#1498)。

- 読み取りの計測(SQL の回数・行数・LOB の読み取り)と、LOB を値で受け取る範囲・1 回の往復の行数。
- 前後の文脈(同じ chunk group の sibling)を起点をまとめた SQL で読む(起点の順・起点ごとの並び。
  本文は sibling の chunk ごとに 1 回)。
- 検索文をまとめた検索(``search_many``)の結果が、検索文ごとの ``hybrid_search`` と同じ。
- 1 回の回答の中で同じ検索文の結果を使い回す(CRAG の 2 回目)。
- 接続 pool の min と、起動時に温める範囲。
"""

import asyncio
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from rag_engine.generation import step_details
from rag_engine.generation.answer_records import _stored_chunk_answer_record
from rag_engine.resources.tracing import bind_trace_observer
from rag_engine.retrieval.search_stages import (
    CONTEXT_SIBLINGS_STAGE,
    DERIVED_SOURCE_STAGE,
    DOCUMENT_INFO_STAGE,
    ENTITY_EXPANSION_STAGE,
    GRAPH_EXPANSION_STAGE,
    IMAGE_EVIDENCE_STAGE,
    REFERENCE_EXPANSION_STAGE,
    TEXT_SEARCH_STAGE,
    VECTOR_SEARCH_STAGE,
)

import app.rag.answer_engine as answer_engine_module
import app.rag.pipeline as pipeline_module
from app.clients import oracle as oracle_module
from app.clients.oracle import OracleClient
from app.clients.oracle_read_stats import (
    current_db_call_stats,
    read_lobs_as_values,
    read_rows_per_round_trip,
    track_db_calls,
)
from app.config import Settings
from app.rag.answer_engine import AnswerEngine, _SearchState
from app.schemas.search import RetrievedChunk, SearchMode, SearchRequest


class _Lob:
    """python-oracledb の LOB のロケータの代役(``read()`` で本文を返す)。"""

    def __init__(self, value: str) -> None:
        self.value = value

    def read(self) -> str:
        return self.value


class _LobCursor:
    """``fetch_lobs`` を受ける cursor の代役。``fetch_lobs=False`` なら CLOB を str で返す。"""

    description = (("chunk_id",), ("chunk_text",))

    def __init__(self, calls: list[object]) -> None:
        self._calls = calls
        self._fetch_lobs: bool | None = None

    def execute(
        self,
        statement: str,
        parameters: Mapping[str, object] | None = None,
        *,
        fetch_lobs: bool | None = None,
    ) -> None:
        self._calls.append(fetch_lobs)
        self._fetch_lobs = fetch_lobs

    def fetchall(self) -> list[tuple[object, ...]]:
        texts = ["本文 1", "本文 2"]
        if self._fetch_lobs is False:
            return [("c1", texts[0]), ("c2", texts[1])]
        return [("c1", _Lob(texts[0])), ("c2", _Lob(texts[1]))]

    def close(self) -> None:
        pass


class _PlainCursor(_LobCursor):
    """``fetch_lobs`` を受けない cursor(テストの代役など)。"""

    def execute(  # type: ignore[override]
        self, statement: str, parameters: Mapping[str, object] | None = None
    ) -> None:
        self._calls.append("plain")


class _Connection:
    def __init__(self, cursor_type: type[_LobCursor]) -> None:
        self.calls: list[object] = []
        self._cursor_type = cursor_type

    def cursor(self) -> _LobCursor:
        return self._cursor_type(self.calls)


def test_fetch_all_reads_lobs_as_values_only_in_scope() -> None:
    connection = _Connection(_LobCursor)
    expected = [
        {"chunk_id": "c1", "chunk_text": "本文 1"},
        {"chunk_id": "c2", "chunk_text": "本文 2"},
    ]

    with track_db_calls() as outside:
        rows = oracle_module._fetch_all(connection, "SELECT chunk_id, chunk_text FROM t", {})  # type: ignore[arg-type]
    with track_db_calls() as inside, read_lobs_as_values():
        values = oracle_module._fetch_all(connection, "SELECT chunk_id, chunk_text FROM t", {})  # type: ignore[arg-type]

    # 値は従来(LOB を .read() した値)と同じ。
    assert rows == expected
    assert values == expected
    assert connection.calls == [None, False]
    assert outside.snapshot()["db_lob_read_count"] == 2
    assert outside.snapshot()["db_round_trip_count"] == 3
    assert inside.snapshot()["db_lob_read_count"] == 0
    assert inside.snapshot()["db_round_trip_count"] == 1
    assert inside.snapshot()["db_row_count"] == 2


def test_fetch_all_does_not_pass_fetch_lobs_to_cursor_without_parameter() -> None:
    connection = _Connection(_PlainCursor)

    with read_lobs_as_values():
        rows = oracle_module._fetch_all(connection, "SELECT chunk_id, chunk_text FROM t", {})  # type: ignore[arg-type]

    assert rows == [
        {"chunk_id": "c1", "chunk_text": "本文 1"},
        {"chunk_id": "c2", "chunk_text": "本文 2"},
    ]
    assert connection.calls == ["plain"]


async def test_track_db_calls_counts_threads_and_nested_scopes() -> None:
    def record() -> None:
        stats = current_db_call_stats()
        assert stats is not None
        stats.record_execute(rows=3, seconds=0.5)

    with track_db_calls() as outer:
        await asyncio.to_thread(record)
        with track_db_calls() as inner:
            await asyncio.gather(asyncio.to_thread(record), asyncio.to_thread(record))

    assert inner.snapshot()["db_execute_count"] == 2
    assert outer.snapshot()["db_execute_count"] == 3
    assert outer.snapshot()["db_row_count"] == 9
    assert outer.snapshot()["db_seconds"] == 1.5
    assert current_db_call_stats() is None


async def test_observe_stage_adds_db_metrics_to_span(monkeypatch: pytest.MonkeyPatch) -> None:
    spans: list[dict[str, Any]] = []
    monkeypatch.setattr(pipeline_module, "record_trace_span", lambda **kwargs: spans.append(kwargs))

    async def with_sql() -> str:
        stats = current_db_call_stats()
        assert stats is not None
        stats.record_execute(rows=4, seconds=0.25)
        stats.record_lob_read()
        return "ok"

    async def without_sql() -> str:
        return "ok"

    await pipeline_module._observe_stage("trace-1", "retrieval", with_sql())
    await pipeline_module._observe_stage("trace-1", "answer_guardrail", without_sql())

    assert spans[0]["attributes"] == {
        "db_execute_count": 1,
        "db_row_count": 4,
        "db_lob_read_count": 1,
        "db_round_trip_count": 2,
        "db_seconds": 0.25,
    }
    # SQL を実行しなかった工程には足さない。
    assert spans[1]["attributes"] == {}


def _anchor(chunk_id: str, *, index: int, chunk_set_id: str | None) -> RetrievedChunk:
    metadata: dict[str, Any] = {"chunk_index": index, "chunk_group_id": f"grp-{chunk_id}"}
    if chunk_set_id is not None:
        metadata["chunk_set_id"] = chunk_set_id
    else:
        metadata["recipe_id"] = "recipe-legacy"
    return RetrievedChunk(
        document_id="doc-1", chunk_id=chunk_id, text=chunk_id, score=0.5, metadata=metadata
    )


def _sibling_row(sibling_id: str, *, anchor_positions: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "document_id": "doc-1",
        "chunk_id": sibling_id,
        "chunk_text": f"{sibling_id} の本文",
        "metadata_json": "{}",
        "chunk_index": 100 + int(sibling_id.rsplit("+", 1)[1]),
        "chunk_set_id": "set-a",
        "file_name": "a.pdf",
        "category_name": None,
        "superseded_by_document_id": None,
        "score": 0,
    }
    if anchor_positions is not None:
        row["anchor_positions"] = anchor_positions
    return row


async def test_context_group_siblings_batch_keeps_anchor_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """起点をまとめて 1 回の SQL で読み、返す順は起点の順・起点ごとの順位のまま(#1490)。

    行は sibling の chunk ごとに 1 行で、複数の起点の sibling になる chunk も 1 行(#1498)。
    chunk_set の無い起点(レシピ導入前)は 1 件ずつ読む。
    """
    anchors = [_anchor(f"a{index}", index=100, chunk_set_id="set-a") for index in range(160)]
    anchors.insert(30, _anchor("legacy", index=100, chunk_set_id=None))
    client = OracleClient(settings=Settings.model_construct())
    statements: list[tuple[str, dict[str, object]]] = []

    async def fake_fetch_all(
        statement: str, binds: Mapping[str, object] | None = None
    ) -> list[dict[str, object]]:
        binds = dict(binds or {})
        statements.append((statement, binds))
        if "anchor_positions" in statement:
            # 起点 n の sibling は「自分の +1」と、隣の起点と共有する「shared+(n // 2)」(順位 2)。
            positions: dict[str, list[str]] = {}
            for ordinal in range(oracle_module._CONTEXT_ANCHOR_BATCH_SIZE):
                anchor_id = binds[f"ca_chunk_{ordinal}"]
                if anchor_id is None:
                    continue
                positions.setdefault(f"{anchor_id}+1", []).append(f"{ordinal}:1")
                positions.setdefault(f"shared+{ordinal // 2}", []).append(f"{ordinal}:2")
            rows = [
                _sibling_row(sibling, anchor_positions=",".join(items))
                for sibling, items in positions.items()
            ]
            # SQL の並びに頼らず、起点の位置と順位で組み立て直すこと。
            return list(reversed(rows))
        return [_sibling_row(f"{binds['anchor_chunk_id']}+{offset}") for offset in (1, 2)]

    monkeypatch.setattr(client, "_fetch_all", fake_fetch_all)

    siblings = await client.context_group_siblings(anchors, max_chunks_per_group=2)

    # 160 件の chunk_set の起点は 150 件ずつ 2 回、レシピ導入前の起点は 1 回。
    assert len(statements) == 3
    assert sum("anchor_positions" in statement for statement, _ in statements) == 2
    expected: list[str] = []
    batched = [anchor for anchor in anchors if anchor.metadata.get("chunk_set_id")]
    for anchor in anchors:
        if not anchor.metadata.get("chunk_set_id"):
            expected.extend(f"{anchor.chunk_id}+{offset}" for offset in (1, 2))
            continue
        ordinal = batched.index(anchor) % oracle_module._CONTEXT_ANCHOR_BATCH_SIZE
        expected.extend([f"{anchor.chunk_id}+1", f"shared+{ordinal // 2}"])
    assert [chunk.chunk_id for chunk in siblings] == expected
    assert [chunk.metadata["context_anchor_chunk_id"] for chunk in siblings] == [
        anchor.chunk_id for anchor in anchors for _ in (1, 2)
    ]
    # 共有の sibling は、起点ごとに起点の印と距離を付けた別の chunk になる(本文は同じ行)。
    # 2 回目の SQL(a150〜)の起点の位置は 0 から数え直す。
    shared = [chunk for chunk in siblings if chunk.chunk_id == "shared+0"]
    assert [chunk.metadata["context_anchor_chunk_id"] for chunk in shared] == [
        "a0",
        "a1",
        "a150",
        "a151",
    ]
    assert {chunk.text for chunk in shared} == {"shared+0 の本文"}
    assert all(chunk.score == 0.5 for chunk in siblings)


async def test_context_group_siblings_batches_are_read_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """起点が 1 回の SQL の上限を超えたときの複数回の SQL は、並行に読む(#3461)。"""
    size = oracle_module._CONTEXT_ANCHOR_BATCH_SIZE
    anchors = [_anchor(f"a{index}", index=100, chunk_set_id="set-a") for index in range(size + 1)]
    client = OracleClient(settings=Settings.model_construct())
    started: list[str] = []
    both_started = asyncio.Event()

    async def fake_fetch_all(
        statement: str, binds: Mapping[str, object] | None = None
    ) -> list[dict[str, object]]:
        started.append(str((binds or {})["ca_chunk_0"]))
        if len(started) == 2:
            both_started.set()
        # 直列に読むと 2 回目の SQL が始まらず、ここで時間切れになる。
        await asyncio.wait_for(both_started.wait(), timeout=5)
        return []

    monkeypatch.setattr(client, "_fetch_all", fake_fetch_all)

    assert await client.context_group_siblings(anchors, max_chunks_per_group=2) == []
    assert sorted(started) == ["a0", f"a{size}"]


def test_anchor_positions_parses_integers_only() -> None:
    assert oracle_module._anchor_positions("0:1,12:3") == [(0, 1), (12, 3)]
    assert oracle_module._anchor_positions(None) == []


async def test_context_group_siblings_batch_sql_is_stable_for_any_batch_size() -> None:
    """起点の数に関係なく SQL の文は同じ(解析済みの文を使い回す)。"""
    size = oracle_module._CONTEXT_ANCHOR_BATCH_SIZE
    one = oracle_module._context_group_siblings_batch_sql(
        [(_anchor("a", index=1, chunk_set_id="set-a"), "grp-a", 1)], max_chunks_per_group=4
    )
    many = oracle_module._context_group_siblings_batch_sql(
        [(_anchor(f"a{i}", index=i, chunk_set_id="set-a"), f"grp-{i}", i) for i in range(size)],
        max_chunks_per_group=4,
    )

    assert one[0] == many[0]
    # 文書と chunk_set は起点の表の値(bind ではない)。
    assert ":filter_document_id" not in one[0]
    assert ":filter_chunk_set_id" not in one[0]
    assert "filter_document_id" not in one[1]
    # sibling の chunk ごとに 1 行(本文を起点ごとに読み直さない)。
    assert "GROUP BY chunk_id" in one[0]
    with pytest.raises(ValueError):
        oracle_module._context_group_siblings_batch_sql(
            [
                (_anchor(f"a{i}", index=i, chunk_set_id="set-a"), f"grp-{i}", i)
                for i in range(size + 1)
            ],
            max_chunks_per_group=4,
        )


def _hit(chunk_id: str, score: float, *, mode: str) -> RetrievedChunk:
    return RetrievedChunk(
        document_id="doc-1",
        chunk_id=chunk_id,
        text=chunk_id,
        score=score,
        metadata={"chunk_index": int(chunk_id.rsplit("-", 1)[1]), "retrieval_mode": mode},
    )


class _DelayedBranches(OracleClient):
    """vector と keyword の終わる順を変えられる OracleClient。"""

    def __init__(self, *, vector_delay: float, keyword_delay: float) -> None:
        super().__init__(settings=Settings.model_construct(rag_rrf_k=60))
        self._vector_delay = vector_delay
        self._keyword_delay = keyword_delay

    async def vector_search(
        self, embedding: list[float], top_k: int, filters: dict[str, str] | None = None
    ) -> list[RetrievedChunk]:
        await asyncio.sleep(self._vector_delay)
        return [_hit(f"c-{i}", 1.0 - i / 10, mode="vector") for i in (1, 2, 3)]

    async def keyword_search(
        self, query: str, top_k: int, filters: dict[str, str] | None = None
    ) -> list[RetrievedChunk]:
        await asyncio.sleep(self._keyword_delay)
        return [_hit(f"c-{i}", 0.5 - i / 10, mode="keyword") for i in (3, 4, 1)]


async def test_hybrid_search_result_does_not_depend_on_branch_completion_order() -> None:
    vector_first = await _DelayedBranches(vector_delay=0.0, keyword_delay=0.02).hybrid_search(
        "質問", [0.1], 3
    )
    keyword_first = await _DelayedBranches(vector_delay=0.02, keyword_delay=0.0).hybrid_search(
        "質問", [0.1], 3
    )

    assert [hit.model_dump() for hit in vector_first] == [hit.model_dump() for hit in keyword_first]
    # 両方に当たった c-1 / c-3 が上位(RRF)。
    assert [hit.chunk_id for hit in vector_first] == ["c-1", "c-3", "c-2"]


class _SearchOracle:
    """検索文ごとに決まった候補を返し、後ろの検索文ほど早く終わる Oracle の代役。"""

    def __init__(self) -> None:
        self.searched: list[str] = []
        self.concurrent = 0
        self.max_concurrent = 0

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        self.searched.append(query)
        self.concurrent += 1
        self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            number = int(query.rsplit(" ", 1)[1])
            await asyncio.sleep(0.002 * (10 - number))
            ids = [(number + offset) % 7 for offset in range(4)]
            return [
                _hit(f"c-{chunk}", 1.0 - rank / 10, mode=mode.value)
                for rank, chunk in enumerate(ids)
            ]
        finally:
            self.concurrent -= 1

    async def document_classifications(self, document_ids: list[str]) -> dict[str, Any]:
        return {}

    async def chunk_set_first_page_contexts(self, chunk_set_ids: list[str]) -> dict[str, Any]:
        return {}

    async def context_group_siblings(
        self,
        anchors: list[RetrievedChunk],
        *,
        max_chunks_per_group: int,
        neighbor_child_count: int | None = None,
    ) -> list[RetrievedChunk]:
        return []


class _GenAi:
    async def embed(
        self, texts: list[str], *, input_type: str = "SEARCH_DOCUMENT"
    ) -> list[list[float]]:
        return [[float(len(text))] for text in texts]


def _dump(result: Any) -> tuple[list[tuple[str, float]], list[str]]:
    return (
        [(chunk.chunk_uid, chunk.metadata.get("rrf_score")) for chunk in result.child_chunks],
        [chunk.chunk_uid for chunk in result.all_chunks],
    )


async def _search(
    oracle: _SearchOracle, queries: list[str], state: _SearchState | None = None
) -> tuple[Any, _SearchState]:
    engine = AnswerEngine(Settings(), oracle=oracle, genai=_GenAi())  # type: ignore[arg-type]
    state = state or _SearchState()
    result = await engine._search(
        SearchRequest(query=queries[0], filters={"knowledge_base_id": "kb-1"}),
        state,
        retrieval_queries=queries,
        candidate_limit=6,
        expand_references=False,
    )
    return result, state


class _ManySearchOracle(_SearchOracle):
    """``search_many`` で検索文をまとめて読む Oracle の代役(結果は 1 本ずつの検索と同じ)。"""

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[list[str]] = []

    async def search_many(
        self,
        searches: list[tuple[str, list[float], SearchMode]],
        top_k: int,
        filters: dict[str, str] | None = None,
    ) -> list[list[RetrievedChunk]]:
        self.batches.append([query for query, _, _ in searches])
        return [
            await _SearchOracle.hybrid_search(self, query, embedding, top_k, mode, filters)
            for query, embedding, mode in searches
        ]


async def test_search_reads_all_queries_in_one_batch() -> None:
    """検索文は 1 回の ``search_many`` でまとめて読み、結果は 1 本ずつ読むときと同じ(#1498)。"""
    queries = [f"検索文 {number}" for number in range(6)]

    one_by_one, _ = await _search(_SearchOracle(), queries)
    batch_oracle = _ManySearchOracle()
    batched, _ = await _search(batch_oracle, queries)

    assert batch_oracle.batches == [queries]
    assert _dump(batched) == _dump(one_by_one)
    assert _dump(batched)[0]


async def test_search_reuses_query_results_within_one_answer() -> None:
    """CRAG の 2 回目は、前の回と同じ検索文を読み直さず、結果は初めから検索したときと同じ。"""
    first_queries = [f"検索文 {number}" for number in range(4)]
    second_queries = [*first_queries, "検索文 5"]
    oracle = _ManySearchOracle()

    _, state = await _search(oracle, first_queries)
    oracle.batches.clear()
    reused, _ = await _search(oracle, second_queries, state)
    fresh, _ = await _search(_SearchOracle(), second_queries)

    assert oracle.batches == [["検索文 5"]]
    assert _dump(reused) == _dump(fresh)


def test_pool_max_uses_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, object]] = []

    def fake_create_pool(**kwargs: object) -> object:
        created.append(kwargs)
        return object()

    monkeypatch.setattr(oracle_module, "_SHARED_ORACLE_POOL", None)
    fake_oracledb: Callable[[str], object] = lambda name: type(  # noqa: E731
        "FakeOracledb",
        (),
        {"create_pool": staticmethod(fake_create_pool), "POOL_GETMODE_TIMEDWAIT": 3},
    )
    monkeypatch.setattr("app.clients.oracle.importlib.import_module", fake_oracledb)
    settings = Settings.model_construct(
        oracle_user="rag_app",
        oracle_password="db-secret",
        oracle_dsn="ragdb_high",
        oracle_client_lib_dir="",
        oracle_wallet_dir="",
        oracle_wallet_password="",
        oracle_pool_max=16,
    )

    OracleClient(settings=settings).connection_pool()

    assert created[0]["max"] == 16
    assert Settings.model_fields["oracle_pool_max"].default == 16


# 検索文をまとめた検索(#1498)の代役の DB。branch ごとの候補(chunk の番号と score の順)を持つ。
_VECTOR_HITS: dict[float, list[tuple[int, float]]] = {
    1.0: [(1, 0.91), (2, 0.82), (3, 0.82), (5, 0.4)],
    2.0: [(3, 0.88), (4, 0.7), (1, 0.69)],
    3.0: [(6, 0.95), (1, 0.5)],
}
_KEYWORD_HITS: dict[str, list[tuple[int, float]]] = {
    "規程": [(2, 0.37), (7, 0.2), (1, 0.12)],
    "保管": [(4, 0.5), (3, 0.5)],
}


def _db_row(number: int) -> dict[str, object]:
    return {
        "document_id": f"doc-{number % 2}",
        "chunk_id": f"c-{number}",
        "chunk_text": f"本文 {number}",
        "metadata_json": f'{{"chunk_index": {number}, "chunk_group_id": "g-{number}"}}',
        "chunk_index": number,
        "chunk_set_id": "set-a",
        "recipe_id": "recipe-a",
        "recipe_slot_no": 1,
        "file_name": f"f{number % 2}.pdf",
        "category_name": None,
        "superseded_by_document_id": "doc-new" if number == 5 else None,
    }


def _search_settings() -> Settings:
    return Settings.model_construct(
        oci_genai_embedding_dim=1, rag_min_similarity=0.05, rag_rrf_k=60
    )


def _text_query(query: str) -> str:
    text_query = oracle_module._oracle_text_query(query, settings=_search_settings())
    assert text_query is not None
    return text_query


def _first_value(vector: object) -> float:
    return float(list(vector)[0])  # type: ignore[call-overload]


async def _fake_search_db(
    statement: str, binds: Mapping[str, object] | None = None
) -> list[dict[str, object]]:
    """検索文 1 本ずつの vector / keyword の検索の SQL に答える。"""
    binds = dict(binds or {})
    by_text = {_text_query(word): hits for word, hits in _KEYWORD_HITS.items()}
    if "embedding" in binds:
        return [
            {**_db_row(number), "score": score}
            for number, score in _VECTOR_HITS[_first_value(binds["embedding"])]
        ]
    if "query" in binds:
        return [
            {**_db_row(number), "score": score} for number, score in by_text[str(binds["query"])]
        ]
    raise AssertionError(f"検索の SQL ではありません: {statement[:80]}")


async def test_search_many_matches_hybrid_search_per_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``search_many`` の結果(候補・順位・score・本文・metadata)は、検索文ごとの
    ``hybrid_search`` と同じ。vector だけの検索文と、keyword の語が無い検索文も含む(#1498)。"""
    monkeypatch.setattr(
        oracle_module,
        "resolve_vector_index_adapter",
        lambda settings: type("Params", (), {"target_accuracy": 95})(),
    )
    client = OracleClient(settings=_search_settings())
    statements: list[str] = []

    async def fetch(statement: str, binds: Mapping[str, object] | None = None) -> Any:
        statements.append(statement)
        return await _fake_search_db(statement, binds)

    monkeypatch.setattr(client, "_fetch_all", fetch)
    searches = [
        ("規程", [1.0], SearchMode.HYBRID),
        ("保管", [2.0], SearchMode.HYBRID),
        ("保管", [3.0], SearchMode.VECTOR),
        ("？！--", [3.0], SearchMode.HYBRID),
        ("規程", [1.0], SearchMode.KEYWORD),
    ]
    filters = {"knowledge_base_id": "kb-1"}

    expected = [
        await client.hybrid_search(query, embedding, 3, mode=mode, filters=filters)
        for query, embedding, mode in searches
    ]
    statements.clear()
    batched = await client.search_many(searches, 3, filters)

    assert [[hit.model_dump() for hit in hits] for hits in batched] == [
        [hit.model_dump() for hit in hits] for hits in expected
    ]
    # metadata の key の順も同じ(引用・記録の JSON が変わらない)。
    assert [[list(hit.metadata) for hit in hits] for hits in batched] == [
        [list(hit.metadata) for hit in hits] for hits in expected
    ]
    # 検索文ごとの vector の検索 4 本・keyword の検索 3 本を 1 本ずつ読む（#2800）。
    assert len(statements) == 7
    assert batched[0]
    assert batched[3]


def test_vector_search_decides_the_document_set_before_the_nearest_neighbors() -> None:
    """vector の検索は文書の側の条件で文書の集合を先に決める（#2800）。

    chunk には chunk の側の条件だけを付ける。条件を 1 つの WHERE に並べると、ナレッジベースの
    所属・重複の文書の正本・有効期間の副問い合わせを optimizer が行ごとの FILTER にし、近傍の
    検索が chunk の全件の走査になっていた。
    """
    filters = {"knowledge_base_id": "kb-1", "content_kind": "table"}
    where_sql, binds = oracle_module._oracle_retrieval_where(filters)
    document_sql, chunk_sql, split_binds = oracle_module._oracle_retrieval_where_split(filters)

    assert split_binds == binds
    # 検索の条件そのものも、文書の集合を先に決める形（同じ条件を分けただけ）。
    assert where_sql == (
        f"c.document_id IN (SELECT d.document_id FROM rag_documents d WHERE {document_sql})"
        f" AND {chunk_sql}"
    )
    assert "rag_document_knowledge_bases" in document_sql and "c." not in document_sql.replace(
        "duplicate_d.", ""
    )
    assert "active_cs.chunk_set_id = c.chunk_set_id" in chunk_sql and "content_kind" in chunk_sql
    statement = oracle_module._vector_search_sql(
        select_list="c.chunk_id",
        embedding_bind="embedding",
        document_sql=document_sql,
        chunk_sql=chunk_sql,
        fetch_clause=oracle_module._oracle_vector_fetch_clause(top_k=10, target_accuracy=90),
    )
    assert "WHERE c.document_id IN (SELECT d.document_id FROM rag_documents d WHERE " in statement
    assert "ORDER BY VECTOR_DISTANCE(c.embedding, :embedding, COSINE)\n" in statement


def test_keyword_search_runs_oracle_text_once_before_scope_conditions() -> None:
    """Oracle Text の命中を ROWNUM で区切った view で 1 回だけ取り、外で範囲の条件を使う(#2137)。

    区切らないと optimizer が文書ごとの nested loop の内側で Oracle Text を引き、検索を文書の数だけ
    繰り返す。本文・埋め込みは命中の view で読まない。
    """
    where_sql, _ = oracle_module._oracle_retrieval_where({"knowledge_base_id": "kb-1"})
    statement = oracle_module._keyword_search_sql(
        select_list="c.chunk_id, c.text_score / 100 AS score",
        query_bind="query",
        where_sql=where_sql,
    )

    hits_view = statement.split("FROM rag_chunks hit", 1)[0]
    assert "ROWNUM AS hit_number" in hits_view
    assert "SCORE(1) AS text_score" in hits_view
    assert "chunk_text" not in statement and "embedding" not in statement
    assert "metadata_json" not in hits_view
    # 範囲の条件(ナレッジベース)は命中の view の外。
    assert statement.index("rag_document_knowledge_bases") > statement.index("FROM rag_chunks hit")
    assert "c.text_score DESC" in statement

    filtered_where, _ = oracle_module._oracle_retrieval_where({"content_kind": "table"})
    filtered = oracle_module._keyword_search_sql(
        select_list="c.chunk_id", query_bind="query", where_sql=filtered_where
    )
    # 条件が metadata を使うときだけ、命中の view で metadata を読む。
    assert "hit.metadata_json" in filtered.split("FROM rag_chunks hit", 1)[0]


class _ArraySizeCursor(_PlainCursor):
    prefetchrows = 2
    arraysize = 100

    def execute(  # type: ignore[override]
        self, statement: str, parameters: Mapping[str, object] | None = None
    ) -> None:
        self._calls.append((self.prefetchrows, self.arraysize))


def test_fetch_all_sets_rows_per_round_trip_only_in_scope() -> None:
    connection = _Connection(_ArraySizeCursor)

    oracle_module._fetch_all(connection, "SELECT chunk_id, chunk_text FROM t", {})  # type: ignore[arg-type]
    with read_rows_per_round_trip(1000):
        oracle_module._fetch_all(connection, "SELECT chunk_id, chunk_text FROM t", {})  # type: ignore[arg-type]

    assert connection.calls == [(2, 100), (1000, 1000)]
    with pytest.raises(ValueError), read_rows_per_round_trip(0):
        pass


def _pool_settings(**values: Any) -> Settings:
    return Settings.model_construct(
        oracle_user="rag_app",
        oracle_password="db-secret",
        oracle_dsn="ragdb_high",
        oracle_client_lib_dir="",
        oracle_wallet_dir="",
        oracle_wallet_password="",
        **values,
    )


def _capture_pool_creation(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    created: list[dict[str, object]] = []

    def fake_create_pool(**kwargs: object) -> object:
        created.append(kwargs)
        return object()

    monkeypatch.setattr(oracle_module, "_SHARED_ORACLE_POOL", None)
    monkeypatch.setattr(oracle_module, "_SHARED_ORACLE_POOL_MIN", 1)
    fake_oracledb: Callable[[str], object] = lambda name: type(  # noqa: E731
        "FakeOracledb",
        (),
        {"create_pool": staticmethod(fake_create_pool), "POOL_GETMODE_TIMEDWAIT": 3},
    )
    monkeypatch.setattr("app.clients.oracle.importlib.import_module", fake_oracledb)
    return created


def test_warm_oracle_pool_opens_min_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """backend の起動時は ``RAG_ORACLE_POOL_MIN`` 本、それ以外のプロセスは 1 本から(#1498)。"""
    created = _capture_pool_creation(monkeypatch)

    OracleClient(settings=_pool_settings(oracle_pool_max=16, oracle_pool_min=4)).connection_pool()
    monkeypatch.setattr(oracle_module, "_SHARED_ORACLE_POOL", None)
    assert oracle_module.warm_oracle_pool(_pool_settings(oracle_pool_max=16, oracle_pool_min=4))
    # 作り直す pool も同じ min。上限を超える min は上限にする。
    monkeypatch.setattr(oracle_module, "_SHARED_ORACLE_POOL", None)
    OracleClient(settings=_pool_settings(oracle_pool_max=2, oracle_pool_min=4)).connection_pool()

    assert [kwargs["min"] for kwargs in created] == [1, 4, 2]
    assert Settings.model_fields["oracle_pool_min"].default == 4


def test_warm_oracle_pool_skips_without_connection_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = _capture_pool_creation(monkeypatch)

    assert not oracle_module.warm_oracle_pool(
        Settings.model_construct(oracle_user="", oracle_password="", oracle_dsn="")
    )
    assert created == []
    assert oracle_module._SHARED_ORACLE_POOL_MIN == 1


async def test_search_collapses_chunks_with_same_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """本文が同じ chunk(同じ内容の文書を 2 回取り込んだもの)は候補で 1 件にまとめる(#2147)。

    代表は chunk_id の最小のもので、vector と keyword で同じ代表になり、融合で 1 件に数える。
    1 本ずつの検索と ``search_many`` の結果は同じ。
    """
    monkeypatch.setattr(
        oracle_module,
        "resolve_vector_index_adapter",
        lambda settings: type("Params", (), {"target_accuracy": 95})(),
    )
    duplicate = {8: "同じ本文", 9: "同じ本文"}

    def row(number: int, score: float) -> dict[str, object]:
        base = _db_row(number)
        return {**base, "chunk_text": duplicate.get(number, base["chunk_text"]), "score": score}

    vector = [(9, 0.9), (8, 0.9), (1, 0.5)]
    keyword = [(8, 0.4), (9, 0.4), (2, 0.3)]

    async def fetch(statement: str, binds: Mapping[str, object] | None = None) -> Any:
        binds = dict(binds or {})
        if "embedding" in binds:
            return [row(number, score) for number, score in vector]
        if "query" in binds:
            return [row(number, score) for number, score in keyword]
        # まとめた SQL(vector と keyword は別の SQL で、どちらも branch 0)。
        hits = vector if "embedding_0" in binds else keyword
        return [
            {**row(number, score), "rank_0": rank, "score_0": score}
            for rank, (number, score) in enumerate(hits, start=1)
        ]

    client = OracleClient(settings=_search_settings())
    monkeypatch.setattr(client, "_fetch_all", fetch)

    vector_hits = await client.vector_search([1.0], 3)
    hybrid = await client.hybrid_search("規程", [1.0], 5)
    (batched,) = await client.search_many([("規程", [1.0], SearchMode.HYBRID)], 5)

    assert [hit.chunk_id for hit in vector_hits] == ["c-8", "c-1"]
    assert [hit.metadata["vector_rank"] for hit in vector_hits] == [1, 2]
    # 両方の候補にある代表 c-8 が先頭。同じ本文の c-9 は出ない(c-1 / c-2 は同じ RRF)。
    assert hybrid[0].chunk_id == "c-8"
    assert sorted(hit.chunk_id for hit in hybrid) == ["c-1", "c-2", "c-8"]
    assert hybrid[0].metadata["retrieval_mode"] == "hybrid"
    assert [hit.model_dump() for hit in batched] == [hit.model_dump() for hit in hybrid]


class _OneHitPerQueryOracle(_SearchOracle):
    """検索文ごとに別の 1 件を返す Oracle の代役(融合の票の重みを確かめる)。"""

    def __init__(self, hits: dict[str, str]) -> None:
        super().__init__()
        self.hits = hits

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]:
        return [_hit(self.hits[query], 1.0, mode=mode.value)]


async def test_vector_only_queries_have_their_own_vote() -> None:
    """vector だけの検索文(規定の書き方・仮説文)は、他の派生検索文と票を分け合わない(#2159)。"""
    oracle = _OneHitPerQueryOracle({"原": "c-0", "派生 1": "c-1", "派生 2": "c-2", "規定": "c-3"})
    engine = AnswerEngine(Settings(), oracle=oracle, genai=_GenAi())  # type: ignore[arg-type]
    result = await engine._search(
        SearchRequest(query="原", filters={"knowledge_base_id": "kb-1"}),
        _SearchState(),
        retrieval_queries=["原", "派生 1", "派生 2", "規定"],
        vector_only_queries=["規定"],
        candidate_limit=6,
        expand_references=False,
    )
    scores = {chunk.chunk_id: chunk.metadata.get("rrf_score") for chunk in result.child_chunks}
    # 原質問と vector だけの検索文はそれぞれ 1 本分、派生の 2 本は合計で 1 本分(各 0.5)。
    assert scores["c-0"] == scores["c-3"]
    assert scores["c-1"] == scores["c-2"]
    assert scores["c-1"] < scores["c-0"]


class _QueryGenAi:
    """検索文ごとに決まった埋め込み(代役の DB の vector の候補の key)を返す。"""

    def __init__(self, vectors: Mapping[str, float]) -> None:
        self._vectors = vectors

    async def embed(
        self, texts: list[str], *, input_type: str = "SEARCH_DOCUMENT"
    ) -> list[list[float]]:
        return [[self._vectors[text]] for text in texts]


class _Observation:
    def request(self, value: object, *, schema: object = None) -> None:
        pass

    def response(self, value: object) -> None:
        pass

    def usage(self, value: object) -> None:
        pass

    def display(self, *args: object, **kwargs: object) -> None:
        pass


async def test_injected_search_reports_db_search_stages_and_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回答に注入する検索(``AnswerEngine._search`` を回答の thread から ``_run_on_loop`` で呼ぶ)は、
    実の ``OracleClient.search_many`` の DB の検索を処理の経過の段(ベクトル検索・全文検索)にし、
    候補に検索の経路(``adb_hybrid.retrieval_channels``)を書く(#3443)。"""
    monkeypatch.setattr(
        oracle_module,
        "resolve_vector_index_adapter",
        lambda settings: type("Params", (), {"target_accuracy": 95})(),
    )
    client = OracleClient(settings=_search_settings())
    monkeypatch.setattr(client, "_fetch_all", _fake_search_db)

    async def no_classifications(document_ids: list[str]) -> dict[str, Any]:
        return {}

    async def no_first_pages(chunk_set_ids: list[str]) -> dict[str, Any]:
        return {}

    async def no_siblings(
        anchors: list[RetrievedChunk],
        *,
        max_chunks_per_group: int,
        neighbor_child_count: int | None = None,
    ) -> list[RetrievedChunk]:
        return []

    monkeypatch.setattr(client, "document_classifications", no_classifications)
    monkeypatch.setattr(client, "chunk_set_first_page_contexts", no_first_pages)
    monkeypatch.setattr(client, "context_group_siblings", no_siblings)
    engine = AnswerEngine(
        Settings(),
        oracle=client,
        genai=_QueryGenAi({"規程": 1.0, "保管": 2.0}),  # type: ignore[arg-type]
    )
    observed: list[tuple[str, str, str]] = []

    @contextmanager
    def observer(operation: str, model: str) -> Iterator[_Observation]:
        observed.append(("start", operation, model))
        try:
            yield _Observation()
        finally:
            observed.append(("end", operation, model))

    loop = asyncio.get_running_loop()

    def in_answer_thread() -> Any:
        # 回答は thread で動き、注入した検索を event loop で実行して待つ(`_answer_sync`)。
        with bind_trace_observer(observer):
            return answer_engine_module._run_on_loop(
                lambda: engine._search(
                    SearchRequest(query="規程", filters={"knowledge_base_id": "kb-1"}),
                    _SearchState(),
                    retrieval_queries=["規程", "保管"],
                    candidate_limit=6,
                    expand_references=False,
                ),
                loop,
            )

    result = await asyncio.to_thread(in_answer_thread)

    # vector と keyword の検索は並行に読み、それぞれ 1 つの段になる(段は重なる)。続く前後の文脈・
    # 文書の分類と背景も段になる(#3464)。参照先・実体・関係情報は広げない検索なので段を出さない。
    started = [entry[2] for entry in observed if entry[0] == "start"]
    assert sorted(started[:2]) == sorted([VECTOR_SEARCH_STAGE, TEXT_SEARCH_STAGE])
    assert started[2:] == [CONTEXT_SIBLINGS_STAGE, DOCUMENT_INFO_STAGE]
    assert sorted(entry[2] for entry in observed if entry[0] == "end") == sorted(started)
    assert {entry[1] for entry in observed} == {"stage"}

    channels = {
        chunk.chunk_uid: chunk.metadata["adb_hybrid"]["retrieval_channels"]
        for chunk in result.child_chunks
    }
    # 「規程」(q1) の vector と keyword の両方と「保管」(q2) の vector、keyword だけ、q2 の両方。
    assert channels["c-1"] == ["keyword:oracle_text:q1", "vector:q1", "vector:q2"]
    assert channels["c-7"] == ["keyword:oracle_text:q1"]
    assert channels["c-4"] == ["keyword:oracle_text:q2", "vector:q2"]
    # 並べ替えに効く値は書かない(文脈の選択の点数を変えない)。
    assert all(
        set(chunk.metadata["adb_hybrid"]) == {"retrieval_channels"} for chunk in result.child_chunks
    )
    records = [_stored_chunk_answer_record(chunk) for chunk in result.child_chunks]
    line = step_details.channel_line(records)
    assert "そのほか" not in line
    assert "ベクトルと全文の両方" in line
    assert "全文だけ" in line


async def test_injected_search_reports_stages_after_db_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回答に注入する検索は、DB の検索の後の工程(前後の文脈・参照先・実体・関係情報・図の切り出し・
    文書の分類と背景)を「文書の検索」の子の段にする(#3464)。各工程の処理は段の中で行う。"""
    client = _SearchOracle()
    settings = Settings(
        rag_reference_expansion_enabled=True,
        rag_entity_expansion_enabled=True,
        rag_graph_expansion_enabled=True,
    )
    engine = AnswerEngine(settings, oracle=client, genai=_GenAi())  # type: ignore[arg-type]
    observed: list[tuple[str, str]] = []
    open_stages: list[str] = []
    ran: list[tuple[str, tuple[str, ...]]] = []

    @contextmanager
    def observer(operation: str, model: str) -> Iterator[_Observation]:
        observed.append(("start", model))
        open_stages.append(model)
        try:
            yield _Observation()
        finally:
            open_stages.remove(model)
            observed.append(("end", model))

    def recording(name: str, value: object) -> Callable[..., Any]:
        async def run(*args: object, **kwargs: object) -> object:
            ran.append((name, tuple(open_stages)))
            return value

        return run

    monkeypatch.setattr(client, "context_group_siblings", recording("siblings", []))
    monkeypatch.setattr(engine, "_reference_expansion", recording("references", {}))
    monkeypatch.setattr(engine, "_entity_expansion", recording("entities", []))
    monkeypatch.setattr(engine, "_graph_expansion", recording("graph", []))
    monkeypatch.setattr(engine, "_attach_images", True)
    monkeypatch.setattr(engine, "_materialize_image_evidence", recording("images", None))
    monkeypatch.setattr(client, "chunk_set_first_page_contexts", recording("first_pages", {}))
    image_metadata = '{"image_evidence": [{"image_id": "img-1"}]}'
    original_hybrid_search = client.hybrid_search

    async def hits_with_images(*args: Any, **kwargs: Any) -> list[RetrievedChunk]:
        hits = await original_hybrid_search(*args, **kwargs)
        return [
            hit.model_copy(
                update={
                    "metadata": {
                        **hit.metadata,
                        "chunk_set_id": "set-a",
                        "engine_metadata_json": image_metadata,
                    }
                }
            )
            for hit in hits
        ]

    monkeypatch.setattr(client, "hybrid_search", hits_with_images)
    state = _SearchState(work_dir=Path("unused"))
    loop = asyncio.get_running_loop()

    def in_answer_thread() -> Any:
        with bind_trace_observer(observer):
            return answer_engine_module._run_on_loop(
                lambda: engine._search(
                    SearchRequest(query="検索文 1", filters={"knowledge_base_id": "kb-1"}),
                    state,
                    retrieval_queries=["検索文 1"],
                    candidate_limit=4,
                ),
                loop,
            )

    await asyncio.to_thread(in_answer_thread)

    expected = [
        CONTEXT_SIBLINGS_STAGE,
        REFERENCE_EXPANSION_STAGE,
        ENTITY_EXPANSION_STAGE,
        GRAPH_EXPANSION_STAGE,
        IMAGE_EVIDENCE_STAGE,
        DOCUMENT_INFO_STAGE,
    ]
    # 段は順に開いて閉じる(重ならない)。
    assert observed == [(event, name) for name in expected for event in ("start", "end")]
    # 各工程の処理は、その工程の段の中で行う。
    stage_of = {
        "siblings": CONTEXT_SIBLINGS_STAGE,
        "references": REFERENCE_EXPANSION_STAGE,
        "entities": ENTITY_EXPANSION_STAGE,
        "graph": GRAPH_EXPANSION_STAGE,
        "images": IMAGE_EVIDENCE_STAGE,
        "first_pages": DOCUMENT_INFO_STAGE,
    }
    assert {name for name, _stages in ran} == set(stage_of)
    assert all(stages == (stage_of[name],) for name, stages in ran)


async def test_search_stages_are_skipped_when_there_is_nothing_to_do() -> None:
    """行うことの無い工程(無効・対象が無い・読んだ後)は段を出さない(#3464)。"""
    observed: list[str] = []

    @contextmanager
    def observer(operation: str, model: str) -> Iterator[_Observation]:
        observed.append(model)
        yield _Observation()

    client = _SearchOracle()
    settings = Settings(
        rag_reference_expansion_enabled=False,
        rag_entity_expansion_enabled=False,
        rag_graph_expansion_enabled=False,
    )
    engine = AnswerEngine(settings, oracle=client, genai=_GenAi())  # type: ignore[arg-type]
    state = _SearchState()
    request = SearchRequest(query="検索文 1", filters={"knowledge_base_id": "kb-1"})
    with bind_trace_observer(observer):
        await engine._search(request, state, retrieval_queries=["検索文 1"], candidate_limit=4)
        first = list(observed)
        observed.clear()
        # 同じ回答の 2 回目の検索(CRAG の探し直しなど)は、読み終えた文書の背景を読まない。
        await engine._search(request, state, retrieval_queries=["検索文 1"], candidate_limit=4)

    # 候補の chunk は chunk_set を持たず(文書の背景を読まない)、足した根拠も無い。
    assert first == [CONTEXT_SIBLINGS_STAGE]
    assert observed == [CONTEXT_SIBLINGS_STAGE]


async def test_original_anchors_stage_only_for_derived_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """派生の chunk の原文を読むときだけ「要約・抽出値の原文の読み込み」の段にする(#3464)。"""
    observed: list[str] = []

    @contextmanager
    def observer(operation: str, model: str) -> Iterator[_Observation]:
        observed.append(model)
        yield _Observation()

    engine = AnswerEngine(Settings(), oracle=_SearchOracle(), genai=_GenAi())  # type: ignore[arg-type]
    plain = _hit("c-1", 1.0, mode="hybrid")
    derived = plain.model_copy(
        update={"metadata": {**plain.metadata, "content_kind": "section_summary"}}
    )

    async def resolve(*args: object, **kwargs: object) -> list[RetrievedChunk]:
        return [plain]

    monkeypatch.setattr(engine, "_resolve_original_anchors", resolve)
    with bind_trace_observer(observer):
        assert await engine._original_anchors(_SearchState(), [plain], {}, filters={}) == [plain]
        assert observed == []
        assert await engine._original_anchors(_SearchState(), [derived], {}, filters={}) == [plain]
    assert observed == [DERIVED_SOURCE_STAGE]
