"""モデルの引用を文書・ページの範囲内で元の SDK 証拠へ解決する。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import PurePosixPath
from typing import Any

from rag_engine.generation.answering import AnswerRecord, _answer_record_identity_keys, _evidence_id_key, _metadata_image_evidence
from rag_engine.models.contracts import Evidence


def resolve_citations(
    evidence: Sequence[Evidence],
    records: Sequence[AnswerRecord],
    cited_evidence: Sequence[Mapping[str, Any]],
) -> tuple[Evidence, ...]:
    """一対一で対応する証拠と prompt レコードから引用元を返す。

    source/page/source_run_id の明示値は照合条件として保持する。範囲内の
    一意な Evidence.id / chunk_uid を優先し、局所 ID が複数に一致する場合や
    不正なページ指定は推測せず省略する。同源の重複は先頭の証拠へ統合し、
    異なる出典の件数で曖昧性を判断する。入力は変更せず、入力証拠順に重複なく返す。
    証拠とレコードの件数が異なる場合は ValueError。I/O は行わない。
    """
    if len(evidence) != len(records):
        raise ValueError("evidence and records must correspond one-to-one")
    keys = [_answer_record_identity_keys(record, item.id) for item, record in zip(evidence, records, strict=False)]
    origins = [_citation_origin(item, record) for item, record in zip(evidence, records, strict=False)]
    # 入力位置や引用別名が異なっても、同じ出典は常に先頭の証拠へ解決する。
    representatives = [next((j for j in range(i) if origins[j] == origin), i) for i, origin in enumerate(origins)]
    selected: set[int] = set()
    for usage in cited_evidence:
        identity = str(usage.get("evidence_id") or "").strip()
        key = _evidence_id_key(identity)
        if not key:
            continue
        page_text = str(usage.get("page") or "").strip()
        pages = _page_bounds(page_text)
        if page_text and pages is None:
            continue
        source = str(usage.get("source") or "").strip()
        run_id = str(usage.get("source_run_id") or "").strip()
        candidates = [index for index, aliases in enumerate(keys) if key in aliases]
        # 明示 ID の範囲が矛盾しても、別の局所 ID へ読み替えない。
        exact = [index for index in candidates if identity in (evidence[index].id, records[index].chunk_uid)]
        matches = []
        for index in exact or candidates:
            item, record = evidence[index], records[index]
            if source and not _source_matches(source, record.source or item.source):
                continue
            if run_id and run_id != record.source_run_id:
                continue
            if pages and not _page_matches(record, key, pages):
                continue
            matches.append(index)
        matched_sources = {representatives[index] for index in matches}
        if len(matched_sources) == 1:
            selected.update(matched_sources)
    return tuple(item for index, item in enumerate(evidence) if index in selected)


def _citation_origin(item: Evidence, record: AnswerRecord) -> tuple[Evidence, AnswerRecord]:
    """同源比較用の値を返す。順位だけを除き、本文・出典・bbox 等の差は維持する。

    chunk_uid がある場合だけ SDK 別名や既知の検索順位 metadata の差を許容する。
    UID がない場合は SDK 証拠の完全一致を必要とし、アダプタによる位置採番は除く。
    任意の metadata を hash 化・文字列化せず、元の値を変更しない。
    """
    if not record.chunk_uid:
        if not item.metadata.get("record"):
            record = replace(record, seq_no=0)
        return item, record
    ranking_fields = {"score", "rank", "adb_hybrid", "rerank"}
    record_metadata = {key: value for key, value in record.metadata.items() if key not in ranking_fields}
    evidence_metadata = {key: value for key, value in item.metadata.items() if key not in ranking_fields and key != "record"}
    # JSON 往復で配列になった既存 tuple フィールドも同じ出典として比較する。
    return (
        replace(item, id=record.chunk_uid, metadata=evidence_metadata),
        replace(
            record,
            metadata=record_metadata,
            child_chunk_ids=tuple(record.child_chunk_ids),
            source_seq_ranges=tuple(record.source_seq_ranges),
            source_record_refs=tuple(record.source_record_refs),
        ),
    )


def _source_matches(source: str, actual: str) -> bool:
    source, actual = source.replace("\\", "/"), actual.strip().replace("\\", "/")
    # モデルの source 契約は文書名または相対パス。basename が重複すれば呼出側で曖昧と判定する。
    return source == actual or ("/" not in source and source == PurePosixPath(actual).name)


def _page_bounds(value: str) -> tuple[int, int] | None:
    """ページ番号または p.1-3 / pp.1–3 を読む。不明値や逆転した範囲は拒否する。"""
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    match = re.fullmatch(r"(?:(?:pp?\.?|pages?)\s*)?(\d+)(?:\s*[-–—~〜]\s*(\d+))?", normalized)
    if not match:
        return None
    start, end = int(match[1]), int(match[2] or match[1])
    return (start, end) if 0 < start <= end else None


def _page_matches(record: AnswerRecord, key: str, pages: tuple[int, int]) -> bool:
    """局所画像・元レコードの実ページを優先し、親全体のページ範囲で誤一致させない。"""
    refs = list(record.source_record_refs) + _metadata_image_evidence(record.metadata or {})
    positions = []
    for ref in refs:
        if not isinstance(ref, Mapping):
            continue
        aliases = {_evidence_id_key(str(ref.get(name) or "")) for name in ("image_id", "record_id")}
        if key in aliases:
            bounds = _page_bounds(str(ref.get("page") or ""))
            if bounds:
                positions.append(bounds)
    if positions:
        return any(start <= pages[0] <= pages[1] <= end for start, end in positions)
    return record.page <= pages[0] <= pages[1] <= (record.page_end or record.page)
