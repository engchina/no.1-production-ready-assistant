"""チャンキングの互換画面向け表示変換。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from rag_engine.chunking import ChunkingResult, DocumentChunk, _format_seq_ranges, _page_span, _preview


def summarize_chunk_run(result: ChunkingResult) -> str:
    """チャンキング結果を UI 表示用 Markdown に整形します。"""
    engines = ", ".join(result.selected_engine_ids) or "-"
    return "\n".join(
        [
            "### チャンキング結果",
            f"- チャンク実行 ID（Chunk Run ID）: `{result.chunk_run_id}`",
            f"- 解析実行 ID（Source Run ID）: `{result.source_run_id}`",
            f"- ファイル（file）: `{result.source_file_name}`",
            f"- 対象エンジン（engines）: {engines}",
            f"- 子チャンク数（child chunks）: {result.child_count}",
            f"- 親チャンク数（parent chunks）: {result.parent_count}",
            f"- 保存先（path）: `{result.json_path}`",
        ]
    )


def chunk_table_rows(chunks: Sequence[DocumentChunk]) -> list[list[Any]]:
    """チャンク一覧を Gradio table 表示用の行へ変換します。"""
    rows: list[list[Any]] = []
    for chunk in chunks:
        rows.append(
            [
                chunk.chunk_id,
                chunk.chunk_level,
                chunk.chunk_seq,
                chunk.parent_chunk_id,
                len(chunk.child_chunk_ids),
                _page_span(chunk.page_start, chunk.page_end),
                _format_seq_ranges(chunk.source_seq_ranges),
                " / ".join(chunk.metadata.get("source_categories") or []),
                chunk.char_count,
                bool(chunk.metadata.get("atomic")),
                _preview(chunk.text),
                json.dumps(chunk.metadata, ensure_ascii=False, sort_keys=True),
            ]
        )
    return rows
