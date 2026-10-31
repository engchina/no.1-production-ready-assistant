"""解析結果・チャンクから回答用レコードを作り、語の一致による順位付けと rerank 結果の反映を行う。外部 I/O は行わない。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from rag_engine.chunking import CHILD_CHUNK_LEVEL, PARENT_CHUNK_LEVEL, ChunkingResult, DocumentChunk
from rag_engine.config import RRF_K, Settings  # noqa: F401  RRF_K は互換のためここからも参照できる
from rag_engine.generation.answer_models import AnswerContext, AnswerRecord, _dedupe_queries, _normalize_text
from rag_engine.generation.answer_payload import _bbox_values, _int_value, _trim_for_context
from rag_engine.models.llm import RerankTextRank
from rag_engine.models.storage import StoredChunk
from rag_engine.parsing.content_labels import content_labels
from rag_engine.retrieval.attachment_evidence import is_attachment_evidence
from rag_engine.retrieval.context_builder import context_bundle_from_records
from rag_engine.retrieval.entity_expansion import (  # noqa: F401  is_entity_expansion_record はここからも参照できる
    is_entity_expansion_record,
    is_reserved_entity_expansion,
)

RUN_ID_PATTERN = re.compile(r"[0-9a-f]{6,64}\Z")

TERM_PATTERN = re.compile(r"[0-9a-zA-Z_]+|[ぁ-んァ-ン一-龯々ー]{2,}")

JAPANESE_STOP_PATTERN = re.compile(
    r"(について|するには|してください|ください|します|です|ます|する|した|して|場合|"
    r"には|では|とは|から|まで|より|の|を|に|は|が|で|と|へ)"
)

MAX_CONTEXT_RECORDS = 12

MAX_RECORD_TEXT_CHARS = 1200

DEFAULT_RETRIEVAL_CANDIDATE_LIMIT = 150

MAX_RERANK_METADATA_CHARS = 700


def validate_run_id(run_id: Any) -> str:
    """run_id を安全な保存パス要素として使える文字列に検証します。"""
    normalized = str(run_id or "").strip()
    if not normalized:
        raise ValueError("ファイル解析 tab で PDF / 画像をアップロードし、解析を実行してください。")
    if not RUN_ID_PATTERN.fullmatch(normalized):
        raise ValueError("解析結果の run_id が不正です。")
    return normalized


def load_answer_records(output_dir: str | Path, run_id: Any) -> list[AnswerRecord]:
    """解析結果 JSON から回答生成に使う AnswerRecord を読み込みます。"""
    run_id = validate_run_id(run_id)
    viewer_data_path = Path(output_dir) / run_id / "viewer-data.json"
    if not viewer_data_path.exists():
        raise FileNotFoundError("解析結果が見つかりません。ファイル解析 tab で解析を実行してください。")

    try:
        payload = json.loads(viewer_data_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("解析結果 JSON を読み込めません。") from exc

    engine_labels = _engine_labels(payload.get("engines"))
    source_name = str(payload.get("pdf_name") or "").strip()
    records = []
    for raw_record in payload.get("records") or []:
        record = _answer_record(raw_record, engine_labels, source_name)
        if record is not None:
            records.append(record)
    return sorted(records, key=lambda record: (record.page, record.seq_no, record.engine, record.id))


def preferred_records(records: Sequence[AnswerRecord], preferred_engines: Iterable[str]) -> list[AnswerRecord]:
    """選択された engine を優先して record を絞り込みます。"""
    preferred = {engine for engine in preferred_engines if engine}
    if not preferred:
        return list(records)
    selected = [record for record in records if record.engine in preferred]
    return selected or list(records)


def rank_records(question: str, records: Sequence[AnswerRecord], limit: int = MAX_CONTEXT_RECORDS) -> list[AnswerRecord]:
    """質問との lexical 類似度で record を順位付けします。"""
    if limit <= 0:
        return []

    return [record for _, _, record in _rank_records_with_scores(question, records, limit)]


def rank_records_with_rrf(
    queries: Sequence[str],
    records: Sequence[AnswerRecord],
    limit: int = MAX_CONTEXT_RECORDS,
) -> list[AnswerRecord]:
    """複数 query の順位を RRF で融合して record を並べます。"""
    if limit <= 0:
        return []

    retrieval_queries = _dedupe_queries(queries)
    if not retrieval_queries:
        return rank_records("", records, limit)
    if len(retrieval_queries) == 1:
        return rank_records(retrieval_queries[0], records, limit)

    record_by_id = {record.id: record for record in records}
    source_order = {record.id: index for index, record in enumerate(records)}
    rrf_scores: dict[str, float] = {}
    lexical_scores: dict[str, float] = {}
    first_query_index: dict[str, int] = {}
    best_rank: dict[str, int] = {}

    for query_index, query in enumerate(retrieval_queries):
        ranked = [(score, record) for score, _, record in _rank_records_with_scores(query, records, len(records)) if score > 0]
        for rank, (score, record) in enumerate(ranked, start=1):
            rrf_scores[record.id] = rrf_scores.get(record.id, 0.0) + 1.0 / (RRF_K + rank)
            lexical_scores[record.id] = lexical_scores.get(record.id, 0.0) + score
            first_query_index[record.id] = min(first_query_index.get(record.id, query_index), query_index)
            best_rank[record.id] = min(best_rank.get(record.id, rank), rank)

    if not rrf_scores:
        return rank_records(retrieval_queries[0], records, limit)

    ranked_records = sorted(
        (record_by_id[record_id] for record_id in rrf_scores if record_id in record_by_id),
        key=lambda record: (
            -rrf_scores[record.id],
            -lexical_scores.get(record.id, 0.0),
            first_query_index.get(record.id, len(retrieval_queries)),
            best_rank.get(record.id, len(records) + 1),
            record.page,
            record.seq_no,
            source_order.get(record.id, len(records)),
        ),
    )
    return ranked_records[:limit]


def _rank_records_with_scores(
    question: str,
    records: Sequence[AnswerRecord],
    limit: int,
) -> list[tuple[float, int, AnswerRecord]]:
    if limit <= 0:
        return []

    normalized_question = _normalize_text(question)
    terms = _search_terms(question)
    scored = []
    for index, record in enumerate(records):
        score = _record_score(normalized_question, terms, record)
        scored.append((score, record.page, record.seq_no, index, record))

    ranked = sorted(scored, key=lambda item: (-item[0], item[1], item[2], item[3]))
    return [(score, index, record) for score, _, _, index, record in ranked[:limit]]


def _rerank_configured(settings: Settings) -> bool:
    return bool(str(getattr(settings, "rerank_model", "") or "").strip() and str(getattr(settings, "oci_compartment_id", "") or "").strip())


def _retrieval_candidate_limit(top_k: int, settings: Settings | None = None) -> int:
    """融合後の基本候補数。回答起点数 top_k から召回・精排の幅を推測しない。"""
    return max(1, int(getattr(settings, "retrieval_candidate_limit", DEFAULT_RETRIEVAL_CANDIDATE_LIMIT)))


def _engine_labels(raw_engines: Any) -> dict[str, str]:
    labels: dict[str, str] = {}
    if not isinstance(raw_engines, list):
        return labels
    for raw_engine in raw_engines:
        if not isinstance(raw_engine, dict):
            continue
        engine = str(raw_engine.get("engine") or "")
        label = str(raw_engine.get("label") or engine)
        if engine:
            labels[engine] = label
    return labels


def _answer_record(raw_record: Any, engine_labels: dict[str, str], source_name: str = "") -> AnswerRecord | None:
    if not isinstance(raw_record, dict):
        return None
    text = str(raw_record.get("text") or raw_record.get("sentence") or "").strip()
    if not text:
        return None
    page = _int_value(raw_record.get("page"))
    seq_no = _int_value(raw_record.get("seq_no"))
    if page is None or seq_no is None:
        return None
    engine = str(raw_record.get("engine") or "")
    return AnswerRecord(
        id=str(raw_record.get("id") or f"{engine}-p{page}-{seq_no}"),
        engine=engine,
        engine_label=engine_labels.get(engine, engine or "unknown"),
        page=page,
        seq_no=seq_no,
        category=str(raw_record.get("category") or raw_record.get("detected_type") or "Text"),
        text=text,
        source=source_name,
        page_end=page,
        source_seq_ranges=({"page": page, "seq_start": seq_no, "seq_end": seq_no},),
        source_record_refs=(
            {
                "record_id": str(raw_record.get("id") or f"{engine}-p{page}-{seq_no}"),
                "page": page,
                "seq_no": seq_no,
                "category": str(raw_record.get("category") or raw_record.get("detected_type") or "Text"),
                "raw_type": str(raw_record.get("raw_type") or ""),
                "bbox": _bbox_values(raw_record.get("bbox")),
            },
        ),
    )


def _preferred_chunk_records(chunk_run: ChunkingResult, preferred_engines: Iterable[str]) -> list[AnswerRecord]:
    if not _chunk_run_active(chunk_run):
        return []
    preferred = {engine for engine in preferred_engines if engine}
    records = [
        _chunk_answer_record(chunk, chunk_run_id=chunk_run.chunk_run_id)
        for chunk in chunk_run.chunks
        if _document_chunk_active(chunk) and (not preferred or chunk.source_engine_id in preferred)
    ]
    if preferred and not records:
        return []
    if not any(record.chunk_level == CHILD_CHUNK_LEVEL for record in records):
        return []
    return records


def _chunk_run_active(chunk_run: ChunkingResult) -> bool:
    return _bool_metadata_value(getattr(chunk_run, "active", True), default=True)


def _document_chunk_active(chunk: DocumentChunk) -> bool:
    metadata = chunk.metadata if isinstance(chunk.metadata, dict) else {}
    return _bool_metadata_value(metadata.get("active"), default=True)


def _bool_metadata_value(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "active"}:
            return True
        if normalized in {"false", "0", "no", "n", "inactive"}:
            return False
    if value is None:
        return default
    return bool(value)


def _chunk_answer_record(chunk: DocumentChunk, *, chunk_run_id: str = "") -> AnswerRecord:
    first_seq = _first_source_seq(chunk.source_seq_ranges) or chunk.chunk_seq
    chunk_uid = _local_chunk_uid(
        chunk_run_id=chunk_run_id,
        source_run_id=chunk.source_run_id,
        source_file_name=chunk.source_file_name,
        source_engine_id=chunk.source_engine_id,
        chunk_id=chunk.chunk_id,
    )
    parent_chunk_uid = (
        _local_chunk_uid(
            chunk_run_id=chunk_run_id,
            source_run_id=chunk.source_run_id,
            source_file_name=chunk.source_file_name,
            source_engine_id=chunk.source_engine_id,
            chunk_id=chunk.parent_chunk_id,
        )
        if chunk.parent_chunk_id
        else ""
    )
    return AnswerRecord(
        id=chunk.chunk_id,
        engine=chunk.source_engine_id,
        engine_label=chunk.source_engine_label,
        page=chunk.page_start,
        seq_no=first_seq,
        category="ParentChunk" if chunk.chunk_level == PARENT_CHUNK_LEVEL else "ChildChunk",
        text=chunk.text if chunk.chunk_level == PARENT_CHUNK_LEVEL else (chunk.retrieval_text or chunk.text),
        body_text=chunk.text,
        source=chunk.source_file_name,
        source_run_id=chunk.source_run_id,
        chunk_id=chunk.chunk_id,
        chunk_uid=chunk_uid,
        chunk_level=chunk.chunk_level,
        chunk_seq=chunk.chunk_seq,
        parent_chunk_id=chunk.parent_chunk_id,
        parent_chunk_uid=parent_chunk_uid,
        child_chunk_ids=tuple(chunk.child_chunk_ids),
        page_end=chunk.page_end,
        source_seq_ranges=tuple(chunk.source_seq_ranges),
        source_record_refs=tuple(chunk.source_record_refs),
        metadata=chunk.metadata,
    )


def _stored_chunk_answer_record(chunk: StoredChunk) -> AnswerRecord:
    first_seq = _first_source_seq(chunk.source_seq_ranges) or chunk.chunk_seq
    return AnswerRecord(
        id=chunk.chunk_id,
        engine=chunk.source_engine_id,
        engine_label=chunk.source_engine_label,
        page=chunk.page_start,
        seq_no=first_seq,
        category="ParentChunk" if chunk.chunk_level == PARENT_CHUNK_LEVEL else "ChildChunk",
        text=chunk.text if chunk.chunk_level == PARENT_CHUNK_LEVEL else (chunk.retrieval_text or chunk.text),
        body_text=chunk.text,
        source=chunk.source_file_name,
        source_run_id=chunk.source_run_id,
        chunk_id=chunk.chunk_id,
        chunk_uid=chunk.chunk_uid,
        chunk_level=chunk.chunk_level,
        chunk_seq=chunk.chunk_seq,
        parent_chunk_id=chunk.parent_chunk_id,
        parent_chunk_uid=chunk.parent_chunk_uid,
        child_chunk_ids=tuple(chunk.child_chunk_ids),
        page_end=chunk.page_end,
        source_seq_ranges=tuple(chunk.source_seq_ranges),
        source_record_refs=tuple(chunk.source_record_refs),
        metadata=chunk.metadata,
    )


def _local_chunk_uid(
    *,
    chunk_run_id: str,
    source_run_id: str,
    source_file_name: str,
    source_engine_id: str,
    chunk_id: str,
) -> str:
    chunk_id = str(chunk_id or "").strip()
    if not chunk_id:
        return ""
    chunk_run_id = str(chunk_run_id or "").strip()
    if chunk_run_id:
        return f"{chunk_run_id}:{chunk_id}"
    source_run_id = str(source_run_id or "").strip()
    if source_run_id:
        return f"{source_run_id}:{chunk_id}"
    source_scope = ":".join(part for part in (str(source_file_name or "").strip(), str(source_engine_id or "").strip()) if part)
    return f"{source_scope}:{chunk_id}" if source_scope else chunk_id


def _search_terms(question: str) -> list[str]:
    normalized = _normalize_text(question)
    expanded = JAPANESE_STOP_PATTERN.sub(" ", normalized)
    terms = {term for source in (normalized, expanded) for term in TERM_PATTERN.findall(source) if len(term) >= 2}
    compact = re.sub(r"\s+", "", normalized)
    if 2 <= len(compact) <= 80:
        terms.add(compact)
    return sorted(terms, key=lambda term: (-len(term), term))


def _search_terms_for_queries(queries: Sequence[str]) -> list[str]:
    terms = {term for query in queries for term in _search_terms(query)}
    return sorted(terms, key=lambda term: (-len(term), term))


def _answer_context_from_ranked(records: Sequence[AnswerRecord], *, max_chars: int) -> AnswerContext:
    bundle = context_bundle_from_records(
        records,
        max_chars=max_chars,
        max_record_text_chars=MAX_RECORD_TEXT_CHARS,
    )
    return AnswerContext(
        records=list(bundle.records),
        text=bundle.text,
        evidence=bundle.evidence,
        evidence_tree=bundle.evidence_tree,
        status=bundle.status,
        insufficient_reason=bundle.insufficient_reason,
    )


def _split_text_rerank_candidates(
    candidates: Sequence[AnswerRecord],
) -> tuple[dict[int, AnswerRecord], list[AnswerRecord], list[int]]:
    protected: dict[int, AnswerRecord] = {}
    rerankable: list[AnswerRecord] = []
    rerankable_indices: list[int] = []
    for index, record in enumerate(candidates):
        if _image_vector_only_candidate(record):
            protected[index] = _record_with_rerank_skip_metadata(
                record,
                candidate_index=index,
                reason="image_vector_only",
            )
            continue
        # 実体の 1 段の拡張で足した根拠（#1362）も rerank にかける。関連度の分かった後に、下限以上のものだけ
        # 候補の位置へ戻す（``_keep_entity_expansion_positions``。#1390）。
        rerankable.append(record)
        rerankable_indices.append(index)
    return protected, rerankable, rerankable_indices


def _keep_entity_expansion_positions(
    protected: dict[int, AnswerRecord],
    reranked: Sequence[AnswerRecord],
) -> tuple[dict[int, AnswerRecord], list[AnswerRecord]]:
    """rerank した候補のうち、関連度が下限以上の実体の拡張の根拠を、元の候補の位置へ戻す（#1390）。

    拡張の根拠（台帳の行・略号の表）は質問の語と重ならない橋渡しの行で、分数の順では context に入る順位まで
    上がらないため、関連度が ``ENTITY_EXPANSION_MIN_RELEVANCE`` 以上なら backend が置いた位置を保つ。下限
    未満の拡張の根拠は印の無い候補と同じく分数の順のまま（質問と関係の薄い属性の chunk を前に置かない）。
    """
    kept = dict(protected)
    rest: list[AnswerRecord] = []
    for record in reranked:
        rerank = record.metadata.get("rerank") if isinstance(record.metadata, dict) else None
        index = rerank.get("candidate_index") if isinstance(rerank, dict) else None
        if is_reserved_entity_expansion(record) and isinstance(index, int) and index not in kept:
            metadata = dict(record.metadata)
            metadata["rerank"] = {**cast(dict[str, Any], rerank), "kept_position": "entity_expansion"}
            kept[index] = replace(record, metadata=metadata)
            continue
        rest.append(record)
    return kept, rest


def _merge_text_rerank_candidates(
    original_candidates: Sequence[AnswerRecord],
    protected_candidates: dict[int, AnswerRecord],
    reranked_candidates: Sequence[AnswerRecord],
) -> list[AnswerRecord]:
    merged: list[AnswerRecord] = []
    reranked_iter = iter(reranked_candidates)
    for index, _record in enumerate(original_candidates):
        protected = protected_candidates.get(index)
        if protected is not None:
            merged.append(protected)
            continue
        try:
            merged.append(next(reranked_iter))
        except StopIteration:
            continue
    merged.extend(reranked_iter)
    return merged


def _image_vector_only_candidate(record: AnswerRecord) -> bool:
    channels = _retrieval_channels(record)
    if not any(channel.startswith("image_vector:") for channel in channels):
        return False
    return not any(channel.startswith(("vector:", "keyword:", "profile:")) for channel in channels)


def _retrieval_channels(record: AnswerRecord) -> tuple[str, ...]:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    hybrid = metadata.get("adb_hybrid") if isinstance(metadata.get("adb_hybrid"), dict) else {}
    raw_channels = hybrid.get("retrieval_channels") if isinstance(hybrid, dict) else ()
    values: tuple[Any, ...]
    if isinstance(raw_channels, str):
        values = (raw_channels,)
    elif isinstance(raw_channels, (list, tuple, set)):
        values = tuple(raw_channels)
    else:
        values = ()
    return tuple(str(channel or "").strip() for channel in values if str(channel or "").strip())


def _rerank_document(record: AnswerRecord) -> str:
    """本文を全量保持し、補足 metadata だけを文字数制限して再順位付けへ渡します。

    先頭に根拠の見出し（`section_path`）を置く。cross-encoder は文書の先頭に近い語を強く見るため、見出しが
    長い本文の後ろにあると、質問の画面・機能との一致が順位に効きにくい。生成は先頭の機能ブロックを使う傾向が
    強く、先頭は rerank の順位で決まる (#1118)。
    """
    prefix = f"{record.citation} / {record.engine_label} / {record.category}"
    path = (record.metadata or {}).get("section_path") if isinstance(record.metadata, dict) else None
    heading = " > ".join(str(h) for h in path if str(h).strip()) if isinstance(path, list) else ""
    parts = [f"見出し: {heading}"] if heading else []
    parts.append(record.body_text or record.text)
    visual_context = _rerank_visual_context(record)
    metadata = [prefix]
    if record.body_text and record.text != record.body_text:
        # 旧 search text が途中で切れていても、保存済み body から末尾の根拠を復元する。
        metadata.append(record.text.split("Child text:", 1)[0])
    metadata.append(visual_context)
    parts.append(_trim_for_context("\n".join(metadata), MAX_RERANK_METADATA_CHARS))
    return "\n\n".join(part for part in parts if part)


def _rerank_visual_context(record: AnswerRecord) -> str:
    channels = tuple(channel for channel in _retrieval_channels(record) if channel.startswith("image_vector:"))
    evidence = _rerank_image_evidence(record)
    if not channels and not evidence:
        return ""
    lines: list[str] = []
    if channels:
        lines.append("Visual retrieval channels: " + ", ".join(channels))
    lines.extend(evidence[:3])
    return "\n".join(lines)


def _rerank_image_evidence(record: AnswerRecord) -> list[str]:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    images: list[dict[str, Any]] = []
    raw_images = metadata.get("image_evidence")
    if isinstance(raw_images, list):
        images.extend(image for image in raw_images if isinstance(image, dict))
    table_context = metadata.get("table_context")
    if isinstance(table_context, list):
        for table in table_context:
            if not isinstance(table, dict):
                continue
            raw_visuals = table.get("visual_evidence")
            if isinstance(raw_visuals, list):
                images.extend(image for image in raw_visuals if isinstance(image, dict))

    lines: list[str] = []
    for image in images:
        image_id = str(image.get("image_id") or image.get("record_id") or "").strip()
        page = str(image.get("page") or "").strip()
        role = str(image.get("visual_role") or "").strip()
        modality = str(image.get("embedding_modality") or "").strip()
        text = _join_rerank_image_text(
            image.get("text_preview"),
            image.get("ocr_text"),
            image.get("retrieval_text"),
            image.get("visual_kind"),
            image.get("visual_structure"),
        )
        descriptors = [
            f"id={image_id}" if image_id else "",
            f"page={page}" if page else "",
            f"role={role}" if role else "",
            f"modality={modality}" if modality else "",
            text,
        ]
        line = "Image evidence: " + " / ".join(part for part in descriptors if part)
        if line.strip() != "Image evidence:":
            lines.append(line)
    return lines


def _join_rerank_image_text(*values: Any) -> str:
    parts: list[str] = []
    for value in values:
        if isinstance(value, list):
            candidates = value
        elif isinstance(value, dict):
            candidates = [json.dumps(value, ensure_ascii=False, separators=(",", ":"))]
        else:
            candidates = [value]
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text and text not in parts:
                parts.append(text)
    return " | ".join(parts)


def _records_by_rerank_ranks(
    records: Sequence[AnswerRecord],
    ranks: Sequence[RerankTextRank],
    settings: Settings,
    *,
    candidate_indices: Sequence[int] | None = None,
) -> list[AnswerRecord]:
    selected: list[AnswerRecord] = []
    seen: set[int] = set()
    min_score = max(0.0, float(getattr(settings, "rerank_min_relevance_score", 0.0) or 0.0))
    for rank_position, rank in enumerate(ranks, start=1):
        index = rank.index
        if index in seen or index < 0 or index >= len(records):
            continue
        # 添付は前段で選んだ利用者の明示入力なので、資料候補の最低関連度では除外しない。
        if (
            min_score > 0
            and rank.relevance_score is not None
            and rank.relevance_score < min_score
            and not is_attachment_evidence(records[index])
        ):
            seen.add(index)
            continue
        selected.append(
            _record_with_rerank_metadata(
                records[index],
                candidate_index=_original_candidate_index(index, candidate_indices),
                rank=rank_position,
                relevance_score=rank.relevance_score,
                question_relevance_score=(
                    question if (question := getattr(rank, "question_relevance_score", None)) is not None else rank.relevance_score
                ),
                min_relevance_score=min_score,
            )
        )
        seen.add(index)
    selected.extend(record for index, record in enumerate(records) if index not in seen)
    return selected


def _records_by_rerank_indices(records: Sequence[AnswerRecord], indices: Sequence[int]) -> list[AnswerRecord]:
    selected: list[AnswerRecord] = []
    seen: set[int] = set()
    for index in indices:
        if index in seen or index < 0 or index >= len(records):
            continue
        selected.append(records[index])
        seen.add(index)
    selected.extend(record for index, record in enumerate(records) if index not in seen)
    return selected


def _original_candidate_index(index: int, candidate_indices: Sequence[int] | None) -> int:
    if candidate_indices is None or index < 0 or index >= len(candidate_indices):
        return index
    return int(candidate_indices[index])


def _record_with_rerank_metadata(
    record: AnswerRecord,
    *,
    candidate_index: int,
    rank: int,
    relevance_score: float | None,
    min_relevance_score: float,
    question_relevance_score: float | None = None,
) -> AnswerRecord:
    metadata = dict(record.metadata) if isinstance(record.metadata, dict) else {}
    metadata["rerank"] = {
        "candidate_index": candidate_index,
        "rank": rank,
        # 並べ替えの点数（原質問と検索文ごとの rerank の最高値。#2161）と、原質問だけの点数（#3504）。
        "relevance_score": relevance_score,
        "question_relevance_score": question_relevance_score,
        "min_relevance_score": min_relevance_score,
        "filtered": False,
    }
    if is_attachment_evidence(record) and relevance_score is not None and relevance_score < min_relevance_score:
        metadata["rerank"]["attachment_preserved_below_min_score"] = True
    return replace(record, metadata=metadata)


def _record_with_rerank_skip_metadata(
    record: AnswerRecord,
    *,
    candidate_index: int,
    reason: str,
) -> AnswerRecord:
    metadata = dict(record.metadata) if isinstance(record.metadata, dict) else {}
    metadata["rerank"] = {
        "candidate_index": candidate_index,
        "rank": None,
        "relevance_score": None,
        "min_relevance_score": None,
        "filtered": False,
        "skipped": True,
        "skip_reason": reason,
    }
    return replace(record, metadata=metadata)


def _chunk_neighbors(child: AnswerRecord, siblings: Sequence[AnswerRecord], neighbor_child_count: int) -> list[AnswerRecord]:
    count = max(0, int(neighbor_child_count or 0))
    if count <= 0:
        return []
    index_by_id = {record.id: index for index, record in enumerate(siblings)}
    index = index_by_id.get(child.id)
    if index is None:
        return []
    neighbors: list[AnswerRecord] = []
    for offset in range(1, count + 1):
        before = index - offset
        after = index + offset
        if before >= 0:
            neighbors.append(siblings[before])
        if after < len(siblings):
            neighbors.append(siblings[after])
    return neighbors


def _same_page_chunk_neighbors(
    child: AnswerRecord,
    children: Sequence[AnswerRecord],
    neighbor_child_count: int,
) -> list[AnswerRecord]:
    count = max(0, int(neighbor_child_count or 0))
    if count <= 0:
        return []
    child_pages = set(range(child.page, (child.page_end or child.page) + 1))
    candidates = [
        record
        for record in children
        if record.id != child.id
        and record.parent_chunk_id != child.parent_chunk_id
        and child_pages & set(range(record.page, (record.page_end or record.page) + 1))
    ]
    candidates.sort(key=lambda record: (abs(record.chunk_seq - child.chunk_seq), record.chunk_seq))
    return candidates[:count]


def _first_source_seq(ranges: Sequence[dict[str, int]]) -> int | None:
    for item in ranges:
        try:
            return int(cast(int, item.get("seq_start")))
        except (TypeError, ValueError):
            continue
    return None


def _context_neighbors(
    record: AnswerRecord,
    page_records: Sequence[AnswerRecord],
    normalized_question: str,
    terms: Sequence[str],
) -> list[AnswerRecord]:
    neighbors = [neighbor for neighbor in page_records if neighbor.id != record.id and abs(neighbor.seq_no - record.seq_no) <= 10]
    scored = [
        (_record_score(normalized_question, terms, neighbor), abs(neighbor.seq_no - record.seq_no), neighbor.seq_no, neighbor)
        for neighbor in neighbors
    ]
    return [neighbor for score, _, _, neighbor in sorted(scored, key=lambda item: (-item[0], item[1], item[2])) if score > 0]


def _record_score(normalized_question: str, terms: Sequence[str], record: AnswerRecord) -> float:
    text = _normalize_text(record.text)
    score = 0.0
    matched_terms = 0
    if normalized_question and normalized_question in text:
        score += 20 + len(normalized_question)
    for term in terms:
        count = text.count(term)
        if not count:
            continue
        matched_terms += 1
        score += min(count, 2) * (2 + min(len(term), 12))
    if matched_terms > 1:
        score += matched_terms * 5
    if score > 0 and record.category in {"Section-header", "List-item"}:
        score += 0.5
    if score > 0 and record.text.startswith((content_labels().group_prefix, f"{content_labels().field('retrieval_text')}:")):
        score += 4
    if score > 0 and f"{content_labels().field('condition_result_pairs')}:" in record.text:
        score += 2
    return score
