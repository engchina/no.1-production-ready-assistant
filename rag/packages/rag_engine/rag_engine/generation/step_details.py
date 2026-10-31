"""処理の経過の段を開いて見る、工程の詳細の要約（#2341）。

工程（`execution_record._execution_step`）の詳細に、検索の候補・並べ替え・根拠の要約を、件数と上位の数件に
絞って書く。詳細は回答の記録・チャットの段・実行の記録に残るので、本文は入れず、文書名・見出し・頁・分数だけにする。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import PurePath
from typing import Any, Protocol

from rag_engine.generation.answer_models import AnswerRecord

# 一覧で示す上位の件数（候補・並べ替え・根拠）。
TOP_ITEMS = 5
# 見出しの段の数（文書の中の位置。深い見出しの最後の段だけを出す）。
_HEADING_DEPTH = 2
_LABEL_MAX = 120

# 検索の経路（`adb_hybrid.retrieval_channels`）の種類。経路は `vector:q1` / `image_vector:q1` / `keyword:oracle_text:q2` /
# `profile:…` の形（検索の実装と backend の検索が書く。#3443）で、種類は `:` の前で決める。
_VECTOR_CHANNELS = frozenset({"vector", "image_vector"})
_TEXT_CHANNELS = frozenset({"keyword", "text"})


class StepRecorder(Protocol):
    """詳細を書く先（`_ExecutionStep`）。"""

    def add(self, *details: str) -> None: ...

    def field(self, label: str, value: object) -> None: ...


def record_label(record: AnswerRecord) -> str:
    """根拠の短い名前（文書名 › 見出し・頁）。本文は入れない。"""
    source = PurePath(str(record.source or "")).name or str(record.source or "") or record.chunk_id or record.id
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    path = metadata.get("section_path")
    headings = [str(item).strip() for item in path if str(item).strip()] if isinstance(path, list) else []
    parts = [source, *headings[-_HEADING_DEPTH:]]
    label = " › ".join(part for part in parts if part)
    page = record.page if isinstance(record.page, int) and record.page > 0 else None
    end = record.page_end if isinstance(record.page_end, int) and page is not None and record.page_end > page else None
    if page is not None:
        label += f"（p.{page}–{end}）" if end else f"（p.{page}）"
    return label if len(label) <= _LABEL_MAX else f"{label[:_LABEL_MAX]}…"


def _channels(record: AnswerRecord) -> set[str]:
    """候補の検索の経路の種類（`vector` / `image_vector` / `keyword` / `profile` など）。"""
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    hybrid = metadata.get("adb_hybrid")
    channels = hybrid.get("retrieval_channels") if isinstance(hybrid, dict) else None
    if not isinstance(channels, list | tuple):
        return set()
    return {str(channel).split(":", 1)[0].strip() for channel in channels}


def channel_counts(records: Iterable[AnswerRecord]) -> dict[str, int]:
    """候補の検索の経路ごとの件数（両方・ベクトルだけ・全文だけ・それ以外〔目録・画面で加えた候補など〕）。"""
    counts = {"both": 0, "vector": 0, "text": 0, "other": 0}
    for record in records:
        channels = _channels(record)
        vector = bool(channels & _VECTOR_CHANNELS)
        text = bool(channels & _TEXT_CHANNELS)
        key = "both" if vector and text else "vector" if vector else "text" if text else "other"
        counts[key] += 1
    return counts


def channel_line(records: Sequence[AnswerRecord]) -> str:
    """経路ごとの件数の 1 行（「両方 3・ベクトルだけ 2・全文だけ 1」。0 件の経路は省く）。"""
    counts = channel_counts(records)
    labels = (
        ("both", "ベクトルと全文の両方"),
        ("vector", "ベクトルだけ"),
        ("text", "全文だけ"),
        ("other", "そのほか（目録・画面などで加えた候補）"),
    )
    return "・".join(f"{label} {counts[key]}" for key, label in labels if counts[key])


def document_count(records: Iterable[AnswerRecord]) -> int:
    return len({str(record.source or record.source_run_id or record.id) for record in records})


def rerank_score(record: AnswerRecord) -> float | None:
    metadata = record.metadata if isinstance(record.metadata, dict) else {}
    rerank = metadata.get("rerank")
    score = rerank.get("relevance_score") if isinstance(rerank, dict) else None
    return float(score) if isinstance(score, int | float) and not isinstance(score, bool) else None


def numbered(labels: Sequence[str], *, limit: int = TOP_ITEMS) -> list[str]:
    """番号つきの行（上限を超えた分は「ほか N 件」の 1 行）。"""
    lines = [f"{number}. {label}" for number, label in enumerate(labels[:limit], 1)]
    if len(labels) > limit:
        lines.append(f"ほか {len(labels) - limit} 件")
    return lines


def record_search_candidates(step: StepRecorder, candidates: Sequence[AnswerRecord]) -> None:
    """検索の候補の件数と経路ごとの件数（文書の検索の工程）。"""
    if not candidates:
        step.field("検索の候補", "0 件")
        return
    step.field("検索の候補", f"{len(candidates)} 件（{document_count(candidates)} 文書）")
    if line := channel_line(candidates):
        step.field("候補の経路", line)


def evidence_summary(records: Sequence[AnswerRecord]) -> str:
    """根拠の件数と文書の数（「4 件（2 文書）」）。"""
    return f"{len(records)} 件（{document_count(records)} 文書）" if records else "0 件"


def add_evidence_lines(step: StepRecorder, records: Sequence[AnswerRecord], *, title: str = "回答に渡す根拠") -> None:
    """根拠の上位の名前の行（見出しの行と番号つきの行）。"""
    if records:
        step.add(f"{title}:", *numbered([record_label(record) for record in records]))


def record_rerank_top(step: StepRecorder, records: Sequence[AnswerRecord]) -> None:
    """並べ替えた後の上位の候補と関連度。"""
    scored = [(record, score) for record in records if (score := rerank_score(record)) is not None]
    if not scored:
        return
    best = scored[0][1]
    step.field("最上位の関連度", f"{best:.3f}")
    step.add(
        "並べ替えた後の上位（関連度）:",
        *numbered([f"{score:.3f} {record_label(record)}" for record, score in scored]),
    )


def join_values(values: Iterable[Any], *, limit: int = 8, separator: str = "、") -> str:
    """値の並び（上限を超えた分は「ほか N 件」）。"""
    items = [str(value).strip() for value in values if str(value).strip()]
    head = separator.join(items[:limit])
    return head + (f" ほか {len(items) - limit} 件" if len(items) > limit else "")


# 根拠の確認（CRAG の評価）の観点と、次の対処の表示名。知らない値はそのまま出す。
_ASPECT_LABELS = {
    "business_object": "対象の業務・機能",
    "requested_result": "求める結果",
    "applicability": "適用の条件",
    "procedure": "操作の手順",
    "member_details": "対象の明細",
    "population_membership": "対象への該当",
    "enumeration_coverage": "列挙の網羅",
}
_RECOVERY_LABELS = {
    "rewrite": "検索文を書き換えて探し直す",
    "neighbor_children": "前後の段落を加える",
    "parent": "上位のまとまりを加える",
    "neighbor_parents": "隣接するまとまりを加える",
}


def aspect_labels(aspects: Iterable[str]) -> str:
    return join_values(_ASPECT_LABELS.get(str(aspect), str(aspect)) for aspect in aspects)


def recovery_label(action: str) -> str:
    return _RECOVERY_LABELS.get(action, action)
