"""コーパスから Oracle Text 用のドメインキーワード候補を抽出する。"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rag_engine.adapters.oracle.connection import connect_adb_thin, load_adb_settings
from rag_engine.chunking import (
    CHILD_CHUNK_LEVEL,
    CHILD_SEARCH_TEXT_HEADER,
    CHUNK_METADATA_SCHEMA_VERSION,
    CHUNKS_DIRECTORY,
    LATEST_CHUNKS_FILE,
    ChunkingResult,
    DocumentChunk,
    load_latest_chunk_run,
)
from rag_engine.knowledge.domain_keywords import normalize_domain_keyword, normalize_domain_keywords
from rag_engine.retrieval.text_search_tokenizer import TextSearchTokenizerConfig, tokenize_domain_keyword_candidates

DEFAULT_DOMAIN_KEYWORD_CANDIDATE_LIMIT = 300
DEFAULT_DOMAIN_KEYWORD_SOURCE_CHUNK_LIMIT = 2000
DEFAULT_DOMAIN_KEYWORD_SOURCE_PAGE_SIZE = 500
MAX_DOMAIN_KEYWORD_CANDIDATE_TOKEN_LENGTH = 80
MAX_DOMAIN_KEYWORD_CANDIDATE_TOKENS_PER_CHUNK = 256
DOMAIN_KEYWORD_CANDIDATE_MIN_FREQUENCY = 2
DOMAIN_KEYWORD_CANDIDATE_MIN_CHUNKS = 2

_ASCII_ALPHA = re.compile(r"[a-z]")
_ASCII_ALNUM_CODE = re.compile(r"(?=.*[a-z])(?=.*[0-9])[a-z0-9][a-z0-9_.:-]{2,}\Z")
_PURE_NUMBER_LIKE = re.compile(r"[\d０-９][\d０-９,，.．:/／\\-]*\Z")
_HIRAGANA_ONLY = re.compile(r"[ぁ-ゖー]+\Z")
_KANA_OR_CJK = re.compile(r"[ぁ-ゖァ-ヺ一-龯々〆ヵヶ]")


@dataclass(frozen=True)
class DomainKeywordCandidate:
    """ドメインキーワード候補、出現回数、由来 sample を保持します。"""

    keyword: str
    score: float
    frequency: int
    chunk_count: int
    document_count: int


@dataclass(frozen=True)
class DomainKeywordSourceText:
    """候補抽出に投入するチャンク本文と出典を保持します。"""

    chunk_id: str
    document_id: str
    text: str


class DomainKeywordCandidateTokenizationError(RuntimeError):
    """候補生成中の tokenizer 障害をデータソース障害と区別します。"""


@dataclass(frozen=True)
class DomainKeywordSourceLoadResult:
    """ADB から読み込んだ候補抽出ソースと件数統計を保持します。"""

    sources: list[DomainKeywordSourceText]
    processed_chunk_count: int
    total_chunk_count: int
    processed_document_count: int
    total_document_count: int
    sampled: bool = False
    page_count: int = 0
    page_size: int = DEFAULT_DOMAIN_KEYWORD_SOURCE_PAGE_SIZE


@dataclass(frozen=True)
class DomainKeywordSuggestionResult:
    """候補抽出結果と入力ソース数を保持します。"""

    candidates: list[DomainKeywordCandidate]
    processed_chunk_count: int
    total_chunk_count: int
    processed_source_count: int
    total_source_count: int
    sampled: bool = False
    page_count: int = 0
    page_size: int = DEFAULT_DOMAIN_KEYWORD_SOURCE_PAGE_SIZE

    def __iter__(self) -> Iterator[Any]:
        yield self.candidates
        yield self.processed_chunk_count
        yield self.processed_source_count


def load_latest_corpus_chunk_runs(
    output_dir: str | Path,
    *,
    max_runs: int = 100,
) -> list[ChunkingResult]:
    """Knowledge Base 内の文書ごとに最新の active chunk run を読み込みます。"""
    base_dir = Path(output_dir)
    try:
        latest_paths = sorted(
            base_dir.glob(f"*/{CHUNKS_DIRECTORY}/{LATEST_CHUNKS_FILE}"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []

    chunk_runs: list[ChunkingResult] = []
    seen: set[str] = set()
    seen_documents: set[str] = set()
    for latest_path in latest_paths:
        if len(chunk_runs) >= max(1, int(max_runs or 1)):
            break
        source_run_id = latest_path.parent.parent.name
        try:
            chunk_run = load_latest_chunk_run(base_dir, source_run_id)
        except Exception:
            continue
        if chunk_run is None or not chunk_run.active or chunk_run.chunk_run_id in seen:
            continue
        document_key = _chunk_run_document_key(chunk_run)
        if document_key in seen_documents:
            continue
        chunk_runs.append(chunk_run)
        seen.add(chunk_run.chunk_run_id)
        seen_documents.add(document_key)
    return chunk_runs


def load_adb_domain_keyword_source_texts(
    *,
    preferred_engine_ids: Iterable[str] = (),
    max_source_chunks: int = DEFAULT_DOMAIN_KEYWORD_SOURCE_CHUNK_LIMIT,
) -> list[DomainKeywordSourceText]:
    """ADB から候補抽出用の検索 text を読み込みます。"""
    return load_adb_domain_keyword_source_texts_with_stats(
        preferred_engine_ids=preferred_engine_ids,
        max_source_chunks=max_source_chunks,
    ).sources


def load_adb_domain_keyword_source_texts_with_stats(
    *,
    preferred_engine_ids: Iterable[str] = (),
    max_source_chunks: int = DEFAULT_DOMAIN_KEYWORD_SOURCE_CHUNK_LIMIT,
    page_size: int = DEFAULT_DOMAIN_KEYWORD_SOURCE_PAGE_SIZE,
) -> DomainKeywordSourceLoadResult:
    """ADB から候補抽出 source と pagination 統計を読み込みます。"""
    settings = load_adb_settings()
    limit = _source_chunk_limit(max_source_chunks)
    fetch_size = max(1, int(page_size or DEFAULT_DOMAIN_KEYWORD_SOURCE_PAGE_SIZE))
    engine_ids = [str(engine or "").strip() for engine in preferred_engine_ids if str(engine or "").strip()]
    engine_sql = ""
    binds: dict[str, Any] = {}
    if engine_ids:
        placeholders = []
        for index, engine_id in enumerate(engine_ids):
            bind_name = f"source_engine_id_{index}"
            placeholders.append(f":{bind_name}")
            binds[bind_name] = engine_id
        engine_sql = f"AND c.source_engine_id IN ({', '.join(placeholders)})"

    from_where_sql = f"""
            FROM rag_chunks c
            JOIN rag_documents d
              ON d.document_id = c.document_id
             AND d.latest_chunk_run_id = c.chunk_run_id
            WHERE c.active = 'Y'
              AND c.chunk_level = :chunk_level
              AND JSON_VALUE(c.metadata_json, '$.schema_version' RETURNING NUMBER NULL ON ERROR) = :chunk_metadata_schema_version
              {engine_sql}
            """
    query_binds = {
        "chunk_level": CHILD_CHUNK_LEVEL,
        "chunk_metadata_schema_version": CHUNK_METADATA_SCHEMA_VERSION,
        **binds,
    }

    with connect_adb_thin(settings) as connection:
        cursor = connection.cursor()
        cursor.execute(
            f"""
            SELECT COUNT(*), COUNT(DISTINCT c.document_id)
            {from_where_sql}
            """,
            query_binds,
        )
        count_row = next(iter(cursor), (0, 0))
        total_chunk_count = _int_value(count_row[0]) if len(count_row) > 0 else 0
        total_document_count = _int_value(count_row[1]) if len(count_row) > 1 else 0

        sources: list[DomainKeywordSourceText] = []
        offset = 0
        page_count = 0
        while True:
            cursor.execute(
                f"""
                SELECT c.chunk_uid, c.search_text, c.document_id, c.source_file_name, c.source_run_id
                {from_where_sql}
                ORDER BY c.source_file_name, c.source_engine_id, c.page_start, c.chunk_seq, c.chunk_uid
                OFFSET {offset} ROWS FETCH NEXT {fetch_size} ROWS ONLY
                """,
                query_binds,
            )
            page_rows = list(cursor)
            if not page_rows:
                break
            page_count += 1
            for row in page_rows:
                source = _adb_source_text_from_row(row)
                if source is not None:
                    sources.append(source)
            if len(page_rows) < fetch_size:
                break
            offset += len(page_rows)

    selected_sources, sampled = _balanced_source_sample(sources, limit)
    total_document_count = max(total_document_count, len({source.document_id for source in sources}))
    return DomainKeywordSourceLoadResult(
        sources=selected_sources,
        processed_chunk_count=len(selected_sources),
        total_chunk_count=max(total_chunk_count, len(sources)),
        processed_document_count=len({source.document_id for source in selected_sources}),
        total_document_count=total_document_count,
        sampled=sampled,
        page_count=page_count,
        page_size=fetch_size,
    )


def suggest_domain_keyword_candidates_from_adb(
    *,
    existing_keywords: Sequence[str] = (),
    preferred_engine_ids: Iterable[str] = (),
    tokenizer_config: TextSearchTokenizerConfig | None = None,
    limit: int = DEFAULT_DOMAIN_KEYWORD_CANDIDATE_LIMIT,
    max_source_chunks: int = DEFAULT_DOMAIN_KEYWORD_SOURCE_CHUNK_LIMIT,
) -> DomainKeywordSuggestionResult:
    """ADB 内コーパスからドメインキーワード候補を抽出します。"""
    source_result = load_adb_domain_keyword_source_texts_with_stats(
        preferred_engine_ids=preferred_engine_ids,
        max_source_chunks=max_source_chunks,
    )
    candidates = suggest_domain_keyword_candidates(
        source_result.sources,
        existing_keywords=existing_keywords,
        tokenizer_config=tokenizer_config,
        limit=limit,
    )
    return DomainKeywordSuggestionResult(
        candidates=candidates,
        processed_chunk_count=source_result.processed_chunk_count,
        total_chunk_count=source_result.total_chunk_count,
        processed_source_count=source_result.processed_document_count,
        total_source_count=source_result.total_document_count,
        sampled=source_result.sampled,
        page_count=source_result.page_count,
        page_size=source_result.page_size,
    )


def suggest_domain_keyword_candidates_from_latest_chunks(
    output_dir: str | Path,
    *,
    existing_keywords: Sequence[str] = (),
    preferred_engine_ids: Iterable[str] = (),
    tokenizer_config: TextSearchTokenizerConfig | None = None,
    limit: int = DEFAULT_DOMAIN_KEYWORD_CANDIDATE_LIMIT,
    max_source_chunks: int = DEFAULT_DOMAIN_KEYWORD_SOURCE_CHUNK_LIMIT,
) -> DomainKeywordSuggestionResult:
    """保存済み最新チャンクからドメインキーワード候補を抽出します。"""
    chunk_runs = load_latest_corpus_chunk_runs(output_dir)
    chunks = _active_child_chunks(
        chunk_runs,
        preferred_engine_ids=preferred_engine_ids,
        max_chunks=None,
    )
    all_sources = _source_texts_from_chunks(chunks)
    selected_sources, sampled = _balanced_source_sample(
        all_sources,
        _source_chunk_limit(max_source_chunks),
    )
    candidates = suggest_domain_keyword_candidates(
        selected_sources,
        existing_keywords=existing_keywords,
        tokenizer_config=tokenizer_config,
        limit=limit,
    )
    return DomainKeywordSuggestionResult(
        candidates=candidates,
        processed_chunk_count=len(selected_sources),
        total_chunk_count=len(chunks),
        processed_source_count=len({source.document_id for source in selected_sources}),
        total_source_count=len({_document_key(chunk) for chunk in chunks}),
        sampled=sampled,
    )


def suggest_domain_keyword_candidates(
    sources: Sequence[DomainKeywordSourceText],
    *,
    existing_keywords: Sequence[str] = (),
    tokenizer_config: TextSearchTokenizerConfig | None = None,
    limit: int = DEFAULT_DOMAIN_KEYWORD_CANDIDATE_LIMIT,
) -> list[DomainKeywordCandidate]:
    """source text 群を token 化してドメインキーワード候補を順位付けします。

    tokenizer の依存不足や設定不備は候補なしとして隠さず、呼び出し元へ送出します。
    """
    existing_keys = {_candidate_key(keyword) for keyword in normalize_domain_keywords(existing_keywords)}
    frequency: Counter[str] = Counter()
    surfaces: dict[str, str] = {}
    chunk_ids_by_key: dict[str, set[str]] = defaultdict(set)
    document_ids_by_key: dict[str, set[str]] = defaultdict(set)
    document_ids: set[str] = set()

    for source in sources:
        text = _candidate_body_text(source.text)
        if not text.strip() or not source.chunk_id:
            continue
        document_ids.add(str(source.document_id))
        try:
            tokens = tokenize_domain_keyword_candidates(
                text,
                config=tokenizer_config,
                max_tokens=MAX_DOMAIN_KEYWORD_CANDIDATE_TOKENS_PER_CHUNK,
            )
        except Exception as exc:
            raise DomainKeywordCandidateTokenizationError(f"候補生成用 tokenizer の処理に失敗しました: {exc}") from exc
        for token in tokens:
            keyword = normalize_domain_keyword(token)
            key = _candidate_key(keyword)
            if not key or key in existing_keys or not _candidate_token_ok(keyword, key):
                continue
            frequency[key] += 1
            surfaces.setdefault(key, keyword)
            chunk_ids_by_key[key].add(str(source.chunk_id))
            document_ids_by_key[key].add(str(source.document_id))

    candidates: list[DomainKeywordCandidate] = []
    for key, count in frequency.items():
        chunk_count = len(chunk_ids_by_key[key])
        document_count = len(document_ids_by_key[key])
        if not _candidate_stats_ok(surfaces[key], count, chunk_count):
            continue
        score = _candidate_score(
            chunk_count,
            document_count,
            total_documents=len(document_ids),
        )
        candidates.append(
            DomainKeywordCandidate(
                keyword=surfaces[key],
                score=score,
                frequency=count,
                chunk_count=chunk_count,
                document_count=document_count,
            )
        )

    return sorted(candidates, key=_candidate_sort_key)[: max(1, int(limit or 1))]


def format_domain_keyword_candidates_text(candidates: Sequence[DomainKeywordCandidate]) -> str:
    """候補 keyword を管理画面へ貼り付けやすい text に整形します。"""
    return "\n".join(candidate.keyword for candidate in candidates if candidate.keyword)


def _active_child_chunks(
    chunk_runs: Sequence[ChunkingResult],
    *,
    preferred_engine_ids: Iterable[str],
    max_chunks: int | None,
) -> list[DocumentChunk]:
    preferred = {str(engine or "").strip() for engine in preferred_engine_ids if str(engine or "").strip()}
    chunks: list[DocumentChunk] = []
    limit = _source_chunk_limit(max_chunks) if max_chunks is not None else None
    for chunk_run in chunk_runs:
        if not chunk_run.active:
            continue
        for chunk in chunk_run.chunks:
            if limit is not None and len(chunks) >= limit:
                return chunks
            if chunk.chunk_level != CHILD_CHUNK_LEVEL:
                continue
            if preferred and chunk.source_engine_id not in preferred:
                continue
            if not _chunk_active(chunk):
                continue
            chunks.append(chunk)
    return chunks


def _balanced_source_sample(
    sources: Sequence[DomainKeywordSourceText],
    limit: int,
) -> tuple[list[DomainKeywordSourceText], bool]:
    if len(sources) <= limit:
        return list(sources), False

    grouped: dict[str, list[DomainKeywordSourceText]] = {}
    for source in sources:
        grouped.setdefault(source.document_id or source.chunk_id, []).append(source)

    selected: list[DomainKeywordSourceText] = []
    positions: dict[str, int] = {document_id: 0 for document_id in grouped}
    while len(selected) < limit:
        advanced = False
        for document_id, document_sources in grouped.items():
            position = positions[document_id]
            if position >= len(document_sources):
                continue
            selected.append(document_sources[position])
            positions[document_id] = position + 1
            advanced = True
            if len(selected) >= limit:
                break
        if not advanced:
            break
    return selected, True


def _source_texts_from_chunks(chunks: Sequence[DocumentChunk]) -> list[DomainKeywordSourceText]:
    sources: list[DomainKeywordSourceText] = []
    for index, chunk in enumerate(chunks, start=1):
        text = str(chunk.retrieval_text or chunk.text or "")
        if not text.strip():
            continue
        sources.append(
            DomainKeywordSourceText(
                chunk_id=_chunk_key(chunk, index),
                document_id=_document_key(chunk),
                text=text,
            )
        )
    return sources


def _candidate_token_ok(keyword: str, key: str) -> bool:
    if len(keyword) > MAX_DOMAIN_KEYWORD_CANDIDATE_TOKEN_LENGTH:
        return False
    if _PURE_NUMBER_LIKE.fullmatch(keyword):
        return False
    if _HIRAGANA_ONLY.fullmatch(keyword):
        return False
    if _ASCII_ALNUM_CODE.fullmatch(key):
        return True
    if _ASCII_ALPHA.search(key) and len(key) < 4:
        return False
    if not _KANA_OR_CJK.search(keyword) and len(key) < 3:
        return False
    return len(key) >= 2


def _candidate_stats_ok(keyword: str, frequency: int, chunk_count: int) -> bool:
    key = _candidate_key(keyword)
    if _ASCII_ALNUM_CODE.fullmatch(key):
        return True
    if _KANA_OR_CJK.search(keyword) and len(key) >= 3 and chunk_count >= 1:
        return frequency >= 1
    return frequency >= DOMAIN_KEYWORD_CANDIDATE_MIN_FREQUENCY and chunk_count >= DOMAIN_KEYWORD_CANDIDATE_MIN_CHUNKS


def _candidate_score(
    chunk_count: int,
    document_count: int,
    *,
    total_documents: int,
) -> float:
    """tf-idf で候補を順位付けします。

    出現 chunk 数は対数で減衰させ、文書出現率が高い token ほど重みを下げます。全文書へ現れる定型語が
    出現数だけで上位を占めるのを防ぎ、一部の文書に偏る用語を優先するためです (#1035)。
    `frequency` は chunk 内で重複排除済みで `chunk_count` とほぼ同値になるため順位付けには使いません。
    idf は BM25 系の平滑化を用い、単一文書コーパスでも重みが 0 へ潰れないようにします。
    """
    documents = max(1, int(total_documents or 1))
    appeared = min(max(1, int(document_count or 1)), documents)
    inverse_document_frequency = math.log(1.0 + (documents - appeared + 0.5) / (appeared + 0.5))
    return math.log1p(max(1, int(chunk_count or 1))) * inverse_document_frequency


def _candidate_body_text(text: Any) -> str:
    """検索文から文脈行を外し、child 本文だけを返します。

    検索文の文脈行は `Source file:` などの定型ラベルを全 chunk へ同じ形で付けるため、そのまま集計すると
    ラベル語とその stem・片仮名変種が全文書に出現する語として候補の上位を埋める。候補の母数は本文だけに
    限る (#1035)。文脈付与が無効でヘッダーを持たない検索文は、全体を本文として扱います。
    """
    return str(text or "").split(CHILD_SEARCH_TEXT_HEADER, 1)[-1]


def _candidate_sort_key(candidate: DomainKeywordCandidate) -> tuple[float, int, int, str]:
    return (-candidate.score, -candidate.chunk_count, -candidate.frequency, candidate.keyword)


def _chunk_active(chunk: DocumentChunk) -> bool:
    metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    return _bool_value(metadata.get("active"), default=True)


def _bool_value(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _chunk_key(chunk: DocumentChunk, fallback_index: int) -> str:
    return str(chunk.chunk_id or fallback_index)


def _document_key(chunk: DocumentChunk) -> str:
    metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    document = metadata.get("document") if isinstance(metadata.get("document"), dict) else {}
    source_document_id = str(document.get("source_document_id") or "")
    if source_document_id:
        return source_document_id
    parts = [
        chunk.source_file_name,
        chunk.source_run_id,
    ]
    return json.dumps(parts, ensure_ascii=False)


def _chunk_run_document_key(chunk_run: ChunkingResult) -> str:
    """再解析 run をまとめる安定した文書識別子を返します。"""
    if chunk_run.source_file_sha256:
        return json.dumps(
            [chunk_run.source_file_sha256, chunk_run.source_file_name, chunk_run.source_page_count],
            ensure_ascii=False,
        )
    return json.dumps([chunk_run.source_file_name, chunk_run.source_run_id], ensure_ascii=False)


def _adb_source_text_from_row(row: Sequence[Any]) -> DomainKeywordSourceText | None:
    text = _lob_to_str(row[1] if len(row) > 1 else "")
    if not text.strip():
        return None
    document_id = str(row[2] or "").strip() if len(row) > 2 else ""
    if not document_id:
        document_id = json.dumps(
            [
                str(row[3] or "") if len(row) > 3 else "",
                str(row[4] or "") if len(row) > 4 else "",
            ],
            ensure_ascii=False,
        )
    return DomainKeywordSourceText(
        chunk_id=str(row[0] or ""),
        document_id=document_id,
        text=text,
    )


def _lob_to_str(value: Any) -> str:
    if value is None:
        return ""
    read = getattr(value, "read", None)
    if callable(read):
        return str(read() or "")
    return str(value)


def _source_chunk_limit(value: int | None) -> int:
    return max(1, int(value or DEFAULT_DOMAIN_KEYWORD_SOURCE_CHUNK_LIMIT))


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _candidate_key(value: Any) -> str:
    return normalize_domain_keyword(value).casefold()
