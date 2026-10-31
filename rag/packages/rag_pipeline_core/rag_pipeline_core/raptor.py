"""RAPTOR 再帰要約索引(ICLR 2024)の共有ロジック。

leaf chunk を再帰的に cluster + 要約して多層级の summary node を作り、leaf と summary の
両方を索引できるようにする。長文書の主題レベル質問と細部質問の双方に強くなる。

クラスタリングと summary chunk の構築は **決定論**(順序ベース)で core に置き、要約本文の生成は
``summarizer``(OCI Enterprise AI 等)を注入する。要約が得られない/失敗した cluster は安全に
skip し、最低でも leaf chunk はそのまま残す(opt-in・安全縮退)。ただし ``stops_batch`` の印の
ある失敗(呼び出しの実行器が処理の全体を止めると決めた失敗。権限・設定の誤りなど)は skip せず
そのまま送出する(#2067)。

``summarize_many`` を渡すと、1 つの level の cluster をまとめて渡す(呼び出し側が上限つきの並行の
実行器で呼ぶ。#2067)。結果の ``None`` はその cluster を skip、送出した例外は処理を止める。

chunking の build 時拡張として ingestion 側から呼ぶ(``rag_raptor_enabled`` のとき)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from rag_pipeline_core.chunking import Chunk

# summarizer: 結合テキスト -> 要約(None/空は当該 cluster を skip)。
Summarizer = Callable[[str], Awaitable[str | None]]
# summarize_many: 1 level の結合テキストの列 -> 同じ順の要約の列(None/空は当該 cluster を skip)。
SummarizeMany = Callable[[list[str]], Awaitable[list[str | None]]]

RAPTOR_SUMMARY_STRATEGY = "raptor_summary"


def cluster_chunks(chunks: list[Chunk], cluster_size: int) -> list[list[Chunk]]:
    """chunk を順序ベースで ``cluster_size`` ごとの cluster へ決定論で分割する。"""
    size = max(2, cluster_size)
    return [chunks[i : i + size] for i in range(0, len(chunks), size)]


def build_summary_chunk(
    cluster: list[Chunk], summary_text: str, *, level: int, index: int
) -> Chunk:
    """1 cluster の要約を summary chunk(metadata 付き)へ構築する。"""
    text = summary_text.strip()
    start = cluster[0].start_offset
    end = cluster[-1].end_offset
    child_indices = ",".join(str(c.index) for c in cluster)
    metadata = {
        "raptor_summary": True,
        "raptor_level": level,
        "raptor_child_count": len(cluster),
        "raptor_child_indices": child_indices,
        "chunk_strategy": RAPTOR_SUMMARY_STRATEGY,
        "content_kind": "summary",
    }
    return Chunk(text=text, index=index, start_offset=start, end_offset=end, metadata=metadata)


async def build_raptor_summaries(
    chunks: list[Chunk],
    *,
    summarizer: Summarizer | None = None,
    summarize_many: SummarizeMany | None = None,
    cluster_size: int = 5,
    max_levels: int = 2,
) -> list[Chunk]:
    """leaf chunk から再帰要約 tree を構築し、leaf + 全 summary node を返す。

    各 level で cluster ごとに要約を作り、得られた要約を次 level の入力にする。
    要約 0 件・cluster 1 個へ収束したら停止。全 level で要約が得られなければ leaf のみ返す。
    ``summarize_many`` があれば level ごとにまとめて渡し、無ければ ``summarizer`` を 1 件ずつ呼ぶ。
    """
    if summarizer is None and summarize_many is None:
        raise ValueError("summarizer か summarize_many を渡してください。")
    if len(chunks) < 2 or max_levels < 1:
        return chunks
    summaries: list[Chunk] = []
    current = chunks
    next_index = max((c.index for c in chunks), default=-1) + 1
    for level in range(1, max_levels + 1):
        clusters = cluster_chunks(current, cluster_size)
        if len(clusters) < 1:
            break
        targets: list[tuple[list[Chunk], str]] = []
        for cluster in clusters:
            joined = "\n\n".join(c.text for c in cluster if c.text.strip())
            if joined:
                targets.append((cluster, joined))
        texts = await _summarize_level(
            [joined for _, joined in targets], summarizer, summarize_many
        )
        level_summaries: list[Chunk] = []
        for (cluster, _), summary in zip(targets, texts, strict=True):
            if not summary or not summary.strip():
                continue
            level_summaries.append(
                build_summary_chunk(cluster, summary, level=level, index=next_index)
            )
            next_index += 1
        if not level_summaries:
            break
        summaries.extend(level_summaries)
        current = level_summaries
        if len(level_summaries) < 2:
            break
    return [*chunks, *summaries]


async def _summarize_level(
    texts: list[str],
    summarizer: Summarizer | None,
    summarize_many: SummarizeMany | None,
) -> list[str | None]:
    """1 level の要約(入力と同じ順)。``stops_batch`` の失敗だけは skip せずに送出する。"""
    if not texts:
        return []
    if summarize_many is not None:
        results = await summarize_many(texts)
        if len(results) != len(texts):
            raise ValueError("summarize_many の結果の件数が入力と一致しません。")
        return list(results)
    if summarizer is None:  # build_raptor_summaries が先に確かめる。
        raise ValueError("summarizer を渡してください。")
    results_one: list[str | None] = []
    for text in texts:
        try:
            results_one.append(await summarizer(text))
        except Exception as exc:  # noqa: BLE001 - 要約失敗は当該 cluster を skip して継続
            if getattr(exc, "stops_batch", False):
                raise
            results_one.append(None)
    return results_one
