"""会話の添付から選んだ根拠の印（#2517・#3100・#3126）。

backend は会話の添付から質問に関係する箇所を選び、検索の候補の先頭に足す（``app.rag.chat_attachments``）。
添付は利用者が明示した入力なので、rag_engine は rerank の最低関連度で除外せず、文書の選択で後回しにしない。
文書ごとの起点の上限でも数えない。
context を作るときも、rerank の順位に関わらず起点（``top_k``）・親（``max_records``）の数の外に足す（#3212）。
添付の箇所は 1 ファイルが少ない箇所になりやすく、質問の文と表の行の rerank の関連度は低く出やすいため、
ナレッジベースの文書と同じ基準で比べると回答の根拠から落ちる（#3100）。

backend は rag_engine の子 chunk を作るときに、この印を metadata に付ける（``app.rag.answer_engine._stored_child``）。
backend の chunk の印（``chat_attachment_id``）を rag_engine の記録へ写さないと、ここでの判定が効かない（#3126）。
"""

from __future__ import annotations

from typing import Any

# backend が添付の chunk の metadata に付ける印のキー（``app.rag.answer_engine._stored_child``）。
ATTACHMENT_EVIDENCE_KEY = "is_attachment_chunk"


def is_attachment_evidence(record: Any) -> bool:
    """会話の添付から選んだ根拠か（文書の選択で後回しにせず、起点の上限・起点の数に数えない）。"""
    metadata = getattr(record, "metadata", None)
    return isinstance(metadata, dict) and metadata.get(ATTACHMENT_EVIDENCE_KEY) is True


__all__ = ["ATTACHMENT_EVIDENCE_KEY", "is_attachment_evidence"]
