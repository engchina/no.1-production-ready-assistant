"""RAPTOR 再帰要約索引(core 決定論 + ingestion 注入)のテスト。"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from rag_pipeline_core.raptor import (
    build_raptor_summaries,
    build_summary_chunk,
    cluster_chunks,
)

from app.config import Settings
from app.rag.chunking import Chunk
from app.rag.ingestion import IngestionPipeline


def _chunks(n: int) -> list[Chunk]:
    return [
        Chunk(text=f"本文 {i} です。", index=i, start_offset=i * 10, end_offset=i * 10 + 9)
        for i in range(n)
    ]


def test_cluster_chunks_groups_in_order() -> None:
    clusters = cluster_chunks(_chunks(7), 3)
    assert [len(c) for c in clusters] == [3, 3, 1]
    # cluster_size < 2 は 2 に丸める。
    assert all(len(c) <= 2 for c in cluster_chunks(_chunks(4), 1))


def test_build_summary_chunk_metadata() -> None:
    cluster = _chunks(3)
    summary = build_summary_chunk(cluster, "要約テキスト", level=1, index=10)
    assert summary.text == "要約テキスト"
    assert summary.index == 10
    assert summary.metadata["raptor_summary"] is True
    assert summary.metadata["raptor_level"] == 1
    assert summary.metadata["raptor_child_count"] == 3
    assert summary.metadata["chunk_strategy"] == "raptor_summary"


def test_build_raptor_summaries_appends_summary_nodes() -> None:
    leaves = _chunks(6)

    async def summarizer(text: str) -> str:
        return f"summary<{len(text)}>"

    result = asyncio.run(
        build_raptor_summaries(leaves, summarizer=summarizer, cluster_size=3, max_levels=2)
    )
    # leaf 6 + level1(2 summary)+ level2(2→1 summary)。
    assert len(result) > len(leaves)
    summary_nodes = [c for c in result if c.metadata.get("raptor_summary")]
    assert summary_nodes
    # index は leaf の最大+1 から連番(衝突しない)。
    assert all(c.index >= len(leaves) for c in summary_nodes)


def test_build_raptor_summaries_degrades_when_summarizer_fails() -> None:
    leaves = _chunks(4)

    async def boom(text: str) -> str | None:
        raise RuntimeError("llm down")

    result = asyncio.run(build_raptor_summaries(leaves, summarizer=boom, cluster_size=2))
    # 全要約失敗 → leaf のみ。
    assert result == leaves


def test_build_raptor_summaries_single_chunk_noop() -> None:
    leaves = _chunks(1)

    async def summarizer(text: str) -> str:
        return "x"

    assert asyncio.run(build_raptor_summaries(leaves, summarizer=summarizer)) == leaves


def test_ingestion_raptor_disabled_returns_leaves() -> None:
    pipeline = IngestionPipeline(
        vlm=cast(Any, object()),
        genai=cast(Any, object()),
        oracle=cast(Any, object()),
        object_storage=cast(Any, object()),
        document_understanding=cast(Any, object()),
        settings=Settings(rag_raptor_enabled=False),
    )
    leaves = _chunks(4)
    out = asyncio.run(pipeline._augment_with_raptor("t", leaves, None))
    assert out == leaves


def test_build_raptor_summaries_raises_stop_batch_errors() -> None:
    """``stops_batch`` の失敗(権限・設定の誤り)は cluster を飛ばさずに送出する(#2067)。"""

    class Stop(Exception):
        stops_batch = True

    calls = 0

    async def stop(text: str) -> str | None:
        nonlocal calls
        calls += 1
        raise Stop("forbidden")

    with pytest.raises(Stop):
        asyncio.run(build_raptor_summaries(_chunks(6), summarizer=stop, cluster_size=2))
    assert calls == 1


def test_build_raptor_summaries_with_summarize_many_matches_sequential() -> None:
    """level ごとにまとめて渡しても、1 件ずつと同じ tree になる(#2067)。"""
    leaves = _chunks(9)

    async def summarizer(text: str) -> str | None:
        return None if "本文 2 " in text else f"summary<{len(text)}>"

    batches: list[int] = []

    async def summarize_many(texts: list[str]) -> list[str | None]:
        batches.append(len(texts))
        return [await summarizer(text) for text in texts]

    sequential = asyncio.run(
        build_raptor_summaries(leaves, summarizer=summarizer, cluster_size=3, max_levels=3)
    )
    many = asyncio.run(
        build_raptor_summaries(leaves, summarize_many=summarize_many, cluster_size=3, max_levels=3)
    )
    assert [(c.index, c.text, c.metadata) for c in many] == [
        (c.index, c.text, c.metadata) for c in sequential
    ]
    # level 1 は 3 cluster をまとめて 1 回、level 2 は 2 要約の 1 cluster。
    assert batches == [3, 1]


def test_build_raptor_summaries_requires_a_summarizer() -> None:
    with pytest.raises(ValueError):
        asyncio.run(build_raptor_summaries(_chunks(3)))
