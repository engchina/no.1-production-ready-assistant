"""DB 非依存の検索範囲。"""

import unicodedata
from dataclasses import dataclass
from typing import Any

RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN = "current_chunk_run"
RETRIEVAL_SCOPE_KNOWLEDGE_BASE = "knowledge_base"


def normalize_retrieval_scope(value: Any) -> str:
    """UI や payload 由来の検索範囲値を既知 ID へ正規化します。

    UI の表示 label（「Knowledge Base（全登録ファイル）」「現在のファイル」）も受け付ける。label を
    渡す呼び出し側が、エラーにならないまま現在のファイルだけを検索するのを防ぐ。
    """
    text = unicodedata.normalize("NFKC", str(value or "")).split("(", 1)[0]
    normalized = text.strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
        "current": RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
        "current_file": RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
        "現在のファイル": RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
        "current_chunk_run": RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
        "chunk_run": RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
        "knowledge": RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
        "kb": RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
        "knowledge_base": RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
        "all": RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
        "all_indexed": RETRIEVAL_SCOPE_KNOWLEDGE_BASE,
    }
    return aliases.get(
        normalized,
        normalized
        if normalized in {RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN, RETRIEVAL_SCOPE_KNOWLEDGE_BASE}
        else RETRIEVAL_SCOPE_CURRENT_CHUNK_RUN,
    )


@dataclass(frozen=True)
class AdbSearchReadiness:
    """検索前検査の結果 (#948)。

    legacy_document_count は検索範囲（Knowledge Base は各文書の最新 run、現在のファイルはその run）の
    文書のうち、active な chunk 行の metadata_json.schema_version が現行版でない文書数。これらの行は
    検索 SQL の版条件で除外されるため、呼び出し側が件数を利用者に示す。
    """

    legacy_document_count: int = 0
