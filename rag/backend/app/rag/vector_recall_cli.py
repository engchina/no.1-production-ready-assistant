"""回答の検索の近似の vector 検索(HNSW)の再現率と実行計画を、exact の検索と比べて測る(#74)。

回答の検索が発行する SQL(``vector_search_statement``。``_vector_rows`` と同じ)を、同じ条件・同じ
問の embedding・同じ類似度の下限で ``FETCH APPROX`` と ``FETCH EXACT`` の 2 通りに実行し、
絞り込みの場面ごとに Recall@k・返った件数・p50/p95・実行計画の vector の操作を出す。
target accuracy 100 は exact の検索そのものではないので、基準は ``FETCH EXACT`` にする。

データは合成(決定論の乱数。本文は架空の短文、embedding は話題の中心の周りの点)で、製品の
取込と同じ書き込みの経路(``create_document``・``save_index``・``activate_recipe_chunk_set``・
``delete_document_rows``)で保存する。3 つの段で測る:

- ``ingested``: schema の作成時に作った索引(行が 0 件)へ、後から取込で行を足した状態
  (配備の後に文書を取り込んだ本番と同じ)。
- ``rebuilt``: 索引を今の検索精度の推奨ビルドで作り直した状態(``vector_index_reindex_sql``)。
- ``after_dml``: 作り直した後に、文書の削除と active な chunk_set の切替(再取込)をした状態。

注意:
- 共用の DB では実行しない。索引を DROP / CREATE し、作った行を消す。テスト用の DB ユーザーの
  接続を書いたファイルを ``PLATFORM_ENV_FILE`` で渡し、``--yes-test-schema`` を付ける。
  接続先に、この CLI が作ったもの以外の文書が 1 件でもあれば止まる。
- 取込 worker は動かさない(Embedding・解析の API は呼ばない)。

使い方(rag/backend で):
  PLATFORM_ENV_FILE=/path/to/test-db.env uv run python -m app.rag.vector_recall_cli \\
      --yes-test-schema --output /tmp/vector-recall.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import re
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.clients.oracle import (
    OracleClient,
    OracleConnectionProtocol,
    _fetch_all,
    _to_vector_bind,
    delete_document_rows,
    vector_search_statement,
)
from app.config import Settings, get_settings
from app.rag.chunking import Chunk
from app.rag.oracle_schema import vector_index_reindex_sql
from app.rag.request_context import with_system_access
from app.rag.vector_index_adapter import resolve_vector_index_adapter
from app.schemas.document import DocumentClassification, FileStatus
from app.schemas.extraction import StructuredExtraction

FILE_PREFIX = "ann-eval-"
INDEX_NAME = "RAG_CHUNKS_EMBEDDING_HNSW_IDX"
# exact の境界の距離と同じとみなす差(float32 の距離の丸め)。
TIE_EPSILON = 1e-6


# ----------------------------------------------------------------- 決定論の評価(DB を使わない)


@dataclass(frozen=True)
class Hit:
    """検索の 1 行(chunk と、DB が計算した cosine 距離)。"""

    chunk_id: str
    distance: float


@dataclass(frozen=True)
class RecallResult:
    """1 問の比較。``recall`` は exact が 0 件なら None(分母が無い。空集合として別に数える)。"""

    recall: float | None
    strict_overlap: float | None
    approx_count: int
    exact_count: int
    # 近似の検索が同じ chunk を 2 回以上返した行の数(重複は正解に数えない)。
    approx_duplicates: int = 0


def compare_hits(
    approx: Sequence[Hit], exact: Sequence[Hit], *, top_k: int, epsilon: float = TIE_EPSILON
) -> RecallResult:
    """近似の上位を exact の上位 k(k 未満ならその全件)と比べる。

    正解は「exact の k 番目の距離以下の行」の同等集合とする。exact の k 番目と同じ距離の行が
    k を超えてあるとき、どれを返しても同じ順位なので、ID の入れ替わりを漏れと数えない
    (``recall``)。ID の一致だけの割合は ``strict_overlap`` に出す。
    """
    expected = list(exact[:top_k])
    returned = list(approx[:top_k])
    unique = list({hit.chunk_id: hit for hit in returned}.values())
    duplicates = len(returned) - len(unique)
    if not expected:
        return RecallResult(None, None, len(returned), 0, duplicates)
    boundary = expected[-1].distance
    correct = sum(1 for hit in unique if hit.distance <= boundary + epsilon)
    expected_ids = {hit.chunk_id for hit in expected}
    overlap = sum(1 for hit in unique if hit.chunk_id in expected_ids)
    return RecallResult(
        recall=min(correct, len(expected)) / len(expected),
        strict_overlap=overlap / len(expected),
        approx_count=len(returned),
        exact_count=len(expected),
        approx_duplicates=duplicates,
    )


def percentile(values: Sequence[float], q: float) -> float | None:
    """線形補間の分位点(``q`` は 0〜1)。値が無ければ None。"""
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


_FIRST_INNER_SELECT = re.compile(r"^\s*SELECT \* FROM \(\s*SELECT\b")
FORCE_VECTOR_INDEX_HINT = f"/*+ VECTOR_INDEX_TRANSFORM(c {INDEX_NAME.lower()}) */"


def force_vector_index_hint(statement: str) -> str:
    """近似の検索の SQL に、vector の索引を使わせる hint を足す(測定だけ。本番の SQL は変えない)。

    optimizer は chunk の少ない DB では索引を使わずに全件の距離を計算する(結果は exact と同じ)。
    行が増えて索引を選んだときの計画(索引の前で絞る条件・後で絞る条件)を、少ない行で再現する。
    """
    hinted, count = _FIRST_INNER_SELECT.subn(
        f"SELECT * FROM ( SELECT {FORCE_VECTOR_INDEX_HINT}", statement, count=1
    )
    if count != 1:
        raise ValueError("vector 検索の SQL の形が想定と違います。")
    return hinted


def vector_plan_operations(plan_lines: Sequence[str]) -> list[str]:
    """``DBMS_XPLAN.DISPLAY_CURSOR`` の行から vector の索引の操作名を取り出す(重複なし・順)。"""
    operations: list[str] = []
    for line in plan_lines:
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 4 or not cells[1].lstrip("*").strip().isdigit():
            continue
        operation = cells[2]
        if "VECTOR" in operation.upper() and operation not in operations:
            operations.append(operation)
    return operations


# ----------------------------------------------------------------- 合成のデータ


@dataclass
class Corpus:
    """合成のデータの決め方(乱数の種と件数)。"""

    seed: int = 74
    documents: int = 80
    chunks_per_document: int = 60
    topics: int = 40
    dimensions: int = 1024
    # 再取込中(active でない chunk_set に保存済み)の文書の数。after_dml で active に切り替える。
    staging_documents: int = 8
    # after_dml で消す文書の数。
    deleted_documents: int = 8


@dataclass
class SeededDocument:
    document_id: str
    recipe_id: str
    chunk_set_id: str
    topic: int
    knowledge_base: str | None
    chunk_ids: list[str] = field(default_factory=list)
    vectors: list[list[float]] = field(default_factory=list)
    staging_chunk_set_id: str | None = None
    expired: bool = False
    future: bool = False


@dataclass
class Seeded:
    knowledge_bases: dict[str, str]
    documents: list[SeededDocument]


def _unit(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def _gaussian(rng: random.Random, dimensions: int) -> list[float]:
    return _unit([rng.gauss(0.0, 1.0) for _ in range(dimensions)])


def _near(rng: random.Random, center: Sequence[float], spread: float) -> list[float]:
    noise = _gaussian(rng, len(center))
    return _unit([c + spread * n for c, n in zip(center, noise, strict=True)])


def _chunks(texts: Sequence[str], pages: Sequence[int]) -> list[Chunk]:
    return [
        Chunk(
            text=text,
            index=index,
            start_offset=0,
            end_offset=len(text),
            metadata={"page_start": page, "page_end": page, "content_kind": "text"},
        )
        for index, (text, page) in enumerate(zip(texts, pages, strict=True))
    ]


async def _save_chunk_set(
    client: OracleClient,
    document_id: str,
    recipe_id: str,
    chunk_set_id: str,
    vectors: list[list[float]],
) -> list[str]:
    await client.upsert_chunk_set(
        chunk_set_id=chunk_set_id, document_id=document_id, recipe_id=recipe_id
    )
    texts = [f"合成の本文 {chunk_set_id} の {index} 番目。" for index in range(len(vectors))]
    pages = [index // 3 + 1 for index in range(len(vectors))]
    extraction = StructuredExtraction(raw_text="合成", confidence=0.9, document_type="合成")
    saved = await client.save_index(
        document_id, extraction, _chunks(texts, pages), vectors, chunk_set_id=chunk_set_id
    )
    await client.mark_chunk_set_indexed(
        chunk_set_id=chunk_set_id, chunk_count=len(vectors), vector_count=len(vectors)
    )
    return [chunk.chunk_id for chunk in saved]


async def _create_document(
    client: OracleClient, name: str, knowledge_base_id: str | None
) -> tuple[str, str]:
    detail = await client.create_document(
        file_name=f"{FILE_PREFIX}{name}.txt",
        object_storage_path=f"local://{FILE_PREFIX}{name}.txt",
        content_type="text/plain",
        knowledge_base_ids=[knowledge_base_id] if knowledge_base_id else None,
    )
    recipes = await client.list_document_recipes(detail.id)
    return detail.id, str(recipes[0]["recipe_id"])


async def _activate(client: OracleClient, document: SeededDocument, chunk_set_id: str) -> None:
    recipes = await client.list_document_recipes(document.document_id)
    await client.activate_recipe_chunk_set(
        recipe_id=document.recipe_id,
        chunk_set_id=chunk_set_id,
        materialized_revision=int(str(recipes[0].get("config_revision") or 1)),
    )


async def seed_corpus(client: OracleClient, corpus: Corpus, centers: list[list[float]]) -> Seeded:
    """合成の文書を製品の取込と同じ書き込みの経路で保存する。

    文書の 80% は KB ``large``、次の 5% は ``small``、残りは KB に属さない。KB に属さない文書の
    うち 2 件は有効期間が過去で終わり、2 件はまだ始まらない。``tiny`` は 3 chunk の文書 1 件
    (候補が k 未満)、``ties`` は同じ embedding の 15 chunk の文書 1 件(同距離)、``empty`` は
    文書の無い KB。
    """
    rng = random.Random(corpus.seed)  # nosec B311 - 合成データの決定論の乱数(暗号には使わない)
    tag = hashlib.sha256(str(time.time_ns()).encode()).hexdigest()[:6]
    knowledge_bases: dict[str, str] = {}
    for name in ("large", "small", "tiny", "ties", "empty"):
        detail = await client.create_knowledge_base(name=f"{FILE_PREFIX}{tag}-{name}")
        knowledge_bases[name] = detail.id
    documents: list[SeededDocument] = []
    large_until = int(corpus.documents * 0.8)
    small_until = large_until + max(1, int(corpus.documents * 0.05))
    for number in range(corpus.documents):
        kb = "large" if number < large_until else "small" if number < small_until else None
        topic = number % corpus.topics
        document_id, recipe_id = await _create_document(
            client, f"{tag}-{number:04d}", knowledge_bases[kb] if kb else None
        )
        vectors = [
            _near(
                rng,
                centers[topic if rng.random() < 0.8 else rng.randrange(corpus.topics)],
                0.6,
            )
            for _ in range(corpus.chunks_per_document)
        ]
        document = SeededDocument(document_id, recipe_id, f"cs_{document_id}_a", topic, kb)
        document.chunk_ids = await _save_chunk_set(
            client, document_id, recipe_id, document.chunk_set_id, vectors
        )
        document.vectors = vectors
        await _activate(client, document, document.chunk_set_id)
        await client.update_document_status(document_id, FileStatus.INDEXED)
        if kb is None and number >= corpus.documents - 4:
            document.expired = number < corpus.documents - 2
            document.future = not document.expired
            classification = (
                DocumentClassification(effective_to=date(2020, 1, 1))
                if document.expired
                else DocumentClassification(effective_from=date(2999, 1, 1))
            )
            await client.save_document_classification(document_id, classification)
        documents.append(document)
    # 再取込の途中(active でない chunk_set に保存済み)。検索には出てはいけない。
    for document in documents[: corpus.staging_documents]:
        document.staging_chunk_set_id = f"cs_{document.document_id}_b"
        staging = [_near(rng, centers[document.topic], 0.6) for _ in range(30)]
        await _save_chunk_set(
            client,
            document.document_id,
            document.recipe_id,
            document.staging_chunk_set_id,
            staging,
        )
    for name, count, same in (("tiny", 3, False), ("ties", 15, True)):
        document_id, recipe_id = await _create_document(
            client, f"{tag}-{name}", knowledge_bases[name]
        )
        base = _near(rng, centers[0], 0.6)
        vectors = [base if same else _near(rng, centers[0], 0.6) for _ in range(count)]
        document = SeededDocument(document_id, recipe_id, f"cs_{document_id}_a", 0, name)
        document.chunk_ids = await _save_chunk_set(
            client, document_id, recipe_id, document.chunk_set_id, vectors
        )
        document.vectors = vectors
        await _activate(client, document, document.chunk_set_id)
        await client.update_document_status(document_id, FileStatus.INDEXED)
        documents.append(document)
    return Seeded(knowledge_bases, documents)


# ----------------------------------------------------------------- 場面と測定


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    filters: dict[str, str]
    # 問を作る文書(この文書の chunk の近くに問を置く)。空なら全体の話題から作る。
    query_documents: tuple[str, ...]


def scenarios(seeded: Seeded) -> list[Scenario]:
    kb = seeded.knowledge_bases
    docs = seeded.documents
    large = tuple(d.document_id for d in docs if d.knowledge_base == "large")
    small = tuple(d.document_id for d in docs if d.knowledge_base == "small")
    expired = tuple(d.document_id for d in docs if d.expired)
    single = large[7 % len(large)]
    searchable = tuple(d.document_id for d in docs if not d.expired and not d.future)
    return [
        Scenario("all", "絞り込みなし(全 KB・KB に属さない文書も)", {}, searchable),
        Scenario("kb_large", "KB(全体の約 80%)", {"knowledge_base_id": kb["large"]}, large),
        Scenario("kb_small", "KB(全体の約 5%)", {"knowledge_base_id": kb["small"]}, small),
        Scenario("document", "文書 1 件(60 chunk)", {"document_id": single}, (single,)),
        Scenario(
            "kb_large_pages",
            "KB(約 80%)+ページ 1〜2(chunk の約 10%)",
            {
                "knowledge_base_id": kb["large"],
                "page_number_min": "1",
                "page_number_max": "2",
            },
            large,
        ),
        Scenario(
            "as_of_past",
            "基準日 2019-06-01(期間の過ぎた文書だけ)",
            {"as_of": "2019-06-01"},
            expired,
        ),
        Scenario("under_k", "候補が k 未満(3 chunk の KB)", {"knowledge_base_id": kb["tiny"]}, ()),
        Scenario(
            "ties", "同じ embedding の 15 chunk(同距離)", {"knowledge_base_id": kb["ties"]}, ()
        ),
        Scenario("empty", "文書の無い KB(空集合)", {"knowledge_base_id": kb["empty"]}, ()),
    ]


def scenario_queries(
    scenario: Scenario,
    seeded: Seeded,
    centers: list[list[float]],
    *,
    count: int,
    seed: int,
) -> list[list[float]]:
    """場面の問の embedding(範囲の文書の chunk の近く。範囲が無ければ話題 0 の近く)。"""
    rng = random.Random(f"{seed}:{scenario.name}")  # nosec B311 - 合成データの決定論の乱数
    by_id = {document.document_id: document for document in seeded.documents}
    queries: list[list[float]] = []
    for _ in range(count):
        if scenario.query_documents:
            document = by_id[rng.choice(scenario.query_documents)]
            queries.append(_near(rng, rng.choice(document.vectors), 0.5))
        else:
            queries.append(_near(rng, centers[0], 0.6))
    return queries


@dataclass
class ScenarioReport:
    name: str
    description: str
    filter_keys: list[str]
    queries: int
    empty_queries: int
    recall_mean: float | None
    recall_min: float | None
    strict_overlap_mean: float | None
    approx_count_mean: float
    exact_count_mean: float
    approx_duplicates: int
    approx_ms_p50: float | None
    approx_ms_p95: float | None
    exact_ms_p50: float | None
    exact_ms_p95: float | None
    approx_vector_operations: list[str]
    exact_vector_operations: list[str]
    approx_plan: list[str]


def _run_statement(
    statement: str, binds: dict[str, object], *, with_plan: bool
) -> Callable[[OracleConnectionProtocol], tuple[list[dict[str, object]], float, list[str]]]:
    def operation(
        connection: OracleConnectionProtocol,
    ) -> tuple[list[dict[str, object]], float, list[str]]:
        started = time.perf_counter()
        rows = _fetch_all(connection, statement, binds)
        elapsed = (time.perf_counter() - started) * 1000
        plan: list[str] = []
        if with_plan:
            # 直前の文(この検索)の SQL_ID と子カーソル。子は 0 とは限らない(bind の型・
            # 適応の計画で増える)ので、番号を読んでから計画を読む。
            cursor_rows = _fetch_all(
                connection,
                "SELECT prev_sql_id, prev_child_number FROM v$session "
                "WHERE sid = SYS_CONTEXT('USERENV', 'SID')",
                {},
            )
            plan_rows = _fetch_all(
                connection,
                "SELECT plan_table_output FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR("
                ":sql_id, :child_number, 'BASIC +PREDICATE'))",
                {
                    "sql_id": cursor_rows[0]["prev_sql_id"],
                    "child_number": cursor_rows[0]["prev_child_number"],
                },
            )
            plan = [str(row.get("plan_table_output") or "") for row in plan_rows]
        return rows, elapsed, plan

    return operation


def _hits(rows: Sequence[dict[str, object]]) -> list[Hit]:
    return [Hit(str(row["chunk_id"]), float(str(row["vector_sort_key"]))) for row in rows]


async def measure_scenario(
    client: OracleClient,
    settings: Settings,
    scenario: Scenario,
    queries: Sequence[list[float]],
    *,
    top_k: int,
    repeats: int,
    force_vector_index: bool = False,
) -> ScenarioReport:
    results: list[RecallResult] = []
    approx_ms: list[float] = []
    exact_ms: list[float] = []
    plans: dict[bool, list[str]] = {}
    for number, query in enumerate(queries):
        bind = _to_vector_bind(query)
        outcome: dict[bool, list[Hit]] = {}
        for exact in (False, True):
            statement, binds = vector_search_statement(
                settings, bind, top_k=top_k, filters=dict(scenario.filters), exact=exact
            )
            if force_vector_index and not exact:
                statement = force_vector_index_hint(statement)
            for attempt in range(repeats + (1 if number == 0 else 0)):
                rows, elapsed, plan = await client._run_transaction(
                    _run_statement(statement, binds, with_plan=number == 0 and attempt == 0)
                )
                if number == 0 and attempt == 0:
                    plans[exact] = plan  # 最初の 1 回は解析・索引の読み込みを含むので時間に数えない
                    continue
                (exact_ms if exact else approx_ms).append(elapsed)
            outcome[exact] = _hits(rows)
        results.append(compare_hits(outcome[False], outcome[True], top_k=top_k))
    recalls = [result.recall for result in results if result.recall is not None]
    overlaps = [result.strict_overlap for result in results if result.strict_overlap is not None]
    return ScenarioReport(
        name=scenario.name,
        description=scenario.description,
        filter_keys=sorted(scenario.filters),
        queries=len(results),
        empty_queries=sum(1 for result in results if result.recall is None),
        recall_mean=sum(recalls) / len(recalls) if recalls else None,
        recall_min=min(recalls) if recalls else None,
        strict_overlap_mean=sum(overlaps) / len(overlaps) if overlaps else None,
        approx_count_mean=sum(r.approx_count for r in results) / max(1, len(results)),
        exact_count_mean=sum(r.exact_count for r in results) / max(1, len(results)),
        approx_duplicates=sum(r.approx_duplicates for r in results),
        approx_ms_p50=percentile(approx_ms, 0.5),
        approx_ms_p95=percentile(approx_ms, 0.95),
        exact_ms_p50=percentile(exact_ms, 0.5),
        exact_ms_p95=percentile(exact_ms, 0.95),
        approx_vector_operations=vector_plan_operations(plans.get(False, [])),
        exact_vector_operations=vector_plan_operations(plans.get(True, [])),
        approx_plan=plans.get(False, []),
    )


async def index_state(client: OracleClient) -> dict[str, object]:
    """主検索索引の HNSW の graph の状態(``v$vector_graph_index``)。"""
    rows = await client._fetch_all(
        "SELECT num_vectors, num_neighbors, ef_construction, used_bytes, allocated_bytes "
        "FROM v$vector_graph_index WHERE owner = SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') "
        "AND index_name = :index_name",
        {"index_name": INDEX_NAME},
    )
    counts = await client._fetch_all(
        "SELECT COUNT(*) AS chunk_rows FROM rag_chunks WHERE embedding IS NOT NULL", {}
    )
    state: dict[str, object] = {"chunk_rows": counts[0]["chunk_rows"] if counts else None}
    state.update({key: value for key, value in (rows[0] if rows else {}).items()})
    return state


def _statement(
    sql: str, binds: dict[str, object] | None = None
) -> Callable[[OracleConnectionProtocol], None]:
    def operation(connection: OracleConnectionProtocol) -> None:
        connection.cursor().execute(sql, binds or {})

    return operation


def _delete_document(document_id: str) -> Callable[[OracleConnectionProtocol], bool]:
    def operation(connection: OracleConnectionProtocol) -> bool:
        return delete_document_rows(connection, document_id)

    return operation


async def rebuild_index(client: OracleClient, settings: Settings) -> str:
    params = resolve_vector_index_adapter(settings)
    sql = vector_index_reindex_sql(
        target_accuracy=params.target_accuracy,
        neighbors=params.neighbors,
        efconstruction=params.efconstruction,
        distance=params.distance,
    )
    for statement in [part.strip() for part in sql.split(";") if part.strip()]:
        await client._run_transaction(_statement(statement))
    return sql


async def apply_dml(client: OracleClient, seeded: Seeded, corpus: Corpus) -> dict[str, int]:
    """文書の削除と active の切替(再取込の公開)。"""
    switched = 0
    for document in seeded.documents:
        if document.staging_chunk_set_id:
            await _activate(client, document, document.staging_chunk_set_id)
            switched += 1
    candidates = [d for d in seeded.documents if d.knowledge_base == "large"]
    deleted = (
        candidates[-min(corpus.deleted_documents, len(candidates) - 1) :]
        if len(candidates) > 1
        else []
    )
    for document in deleted:
        await client._run_transaction(_delete_document(document.document_id))
    remaining = {d.document_id for d in deleted}
    seeded.documents = [d for d in seeded.documents if d.document_id not in remaining]
    return {"switched_documents": switched, "deleted_documents": len(deleted)}


async def foreign_documents(client: OracleClient) -> int:
    rows = await client._fetch_all(
        "SELECT COUNT(*) AS foreign_count FROM rag_documents WHERE file_name NOT LIKE :prefix",
        {"prefix": f"{FILE_PREFIX}%"},
    )
    return int(str(rows[0]["foreign_count"])) if rows else 0


async def cleanup(client: OracleClient) -> None:
    rows = await client._fetch_all(
        "SELECT document_id FROM rag_documents WHERE file_name LIKE :prefix",
        {"prefix": f"{FILE_PREFIX}%"},
    )
    for row in rows:
        await client._run_transaction(_delete_document(str(row["document_id"])))
    await client._run_transaction(
        _statement(
            "DELETE FROM rag_knowledge_bases WHERE name LIKE :prefix",
            {"prefix": f"{FILE_PREFIX}%"},
        )
    )


async def run(args: argparse.Namespace) -> dict[str, Any]:
    settings = get_settings()
    client = OracleClient(settings)
    if await foreign_documents(client):
        raise SystemExit(
            "接続先にこの CLI が作っていない文書があります。"
            "テスト用の DB ユーザーで実行してください。"
        )
    corpus = Corpus(
        seed=args.seed,
        documents=args.documents,
        chunks_per_document=args.chunks_per_document,
    )
    centers = [
        _gaussian(random.Random(f"{corpus.seed}:topic:{n}"), corpus.dimensions)  # nosec B311
        for n in range(corpus.topics)
    ]
    params = resolve_vector_index_adapter(settings)
    started = time.perf_counter()
    # schema の作成と同じく、行の無い表に索引を作ってから取り込む(前の実行の graph を残さない)。
    await rebuild_index(client, settings)
    seeded = await seed_corpus(client, corpus, centers)
    report: dict[str, Any] = {
        "manifest": {
            "issue": 74,
            "corpus": asdict(corpus),
            "top_k": args.top_k,
            "queries_per_scenario": args.queries,
            "repeats": args.repeats,
            "min_similarity": settings.rag_min_similarity,
            "vector_index_profile": params.profile,
            "target_accuracy": params.target_accuracy,
            "recommended_build": {
                "neighbors": params.neighbors,
                "efconstruction": params.efconstruction,
            },
            "approx_sql_sha256": hashlib.sha256(
                vector_search_statement(settings, None, top_k=args.top_k, filters={})[0].encode()
            ).hexdigest(),
            "seed_seconds": round(time.perf_counter() - started, 1),
        },
        "phases": [],
    }
    version = await client._fetch_all("SELECT banner_full FROM v$version", {})
    report["manifest"]["database"] = (
        str(version[0]["banner_full"]).splitlines()[-1] if version else None
    )

    async def phase(name: str, extra: dict[str, object] | None = None) -> None:
        state = await index_state(client)
        for forced in (False, True):
            reports = []
            for scenario in scenarios(seeded):
                queries = scenario_queries(
                    scenario, seeded, centers, count=args.queries, seed=corpus.seed
                )
                reports.append(
                    await measure_scenario(
                        client,
                        settings,
                        scenario,
                        queries,
                        top_k=args.top_k,
                        repeats=args.repeats,
                        force_vector_index=forced,
                    )
                )
            label = f"{name}{' + 索引を強制' if forced else ''}"
            entry: dict[str, Any] = {
                "phase": name,
                "force_vector_index": forced,
                "index_state": state,
                "scenarios": [asdict(r) for r in reports],
            }
            if extra:
                entry.update(extra)
            report["phases"].append(entry)
            print(render_table(label, state, reports), flush=True)

    try:
        await phase("ingested")
        rebuild_sql = await rebuild_index(client, settings)
        await phase("rebuilt", {"rebuild_sql": rebuild_sql})
        dml = await apply_dml(client, seeded, corpus)
        await phase("after_dml", {"dml": dml})
    finally:
        if not args.keep:
            await cleanup(client)
    return report


def _fmt(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def render_table(name: str, state: dict[str, object], reports: Sequence[ScenarioReport]) -> str:
    lines = [
        f"\n### {name}  (chunk rows={state.get('chunk_rows')}, "
        f"HNSW graph vectors={state.get('num_vectors')})",
        "| 場面 | recall@k 平均 / 最小 | ID 一致 | 件数 approx / exact | 重複 | "
        "approx p50 / p95 ms | "
        "exact p50 / p95 ms | approx の vector 操作 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in reports:
        lines.append(
            f"| {r.name} | {_fmt(r.recall_mean)} / {_fmt(r.recall_min)} | "
            f"{_fmt(r.strict_overlap_mean)} | "
            f"{r.approx_count_mean:.1f} / {r.exact_count_mean:.1f} | {r.approx_duplicates} | "
            f"{_fmt(r.approx_ms_p50, 1)} / {_fmt(r.approx_ms_p95, 1)} | "
            f"{_fmt(r.exact_ms_p50, 1)} / {_fmt(r.exact_ms_p95, 1)} | "
            f"{', '.join(r.approx_vector_operations) or '(索引なし)'} |"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--yes-test-schema", action="store_true", help="接続先がテスト用の DB ユーザー"
    )
    parser.add_argument("--documents", type=int, default=80)
    parser.add_argument("--chunks-per-document", type=int, default=60)
    parser.add_argument("--queries", type=int, default=20, help="場面ごとの問の数")
    parser.add_argument("--repeats", type=int, default=3, help="1 問を測る回数(時間の標本)")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--seed", type=int, default=74)
    parser.add_argument("--output", type=Path, help="結果の JSON(manifest・段・場面・実行計画)")
    parser.add_argument("--keep", action="store_true", help="作った文書・KB を残す")
    return parser


@with_system_access
def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.yes_test_schema:
        print(
            "テスト用の DB ユーザー(PLATFORM_ENV_FILE)で --yes-test-schema を付けて"
            "実行してください。"
            "索引を作り直し、作った行を消します。",
            file=sys.stderr,
        )
        return 2
    report = asyncio.run(run(args))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
