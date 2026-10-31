"""実体の 1 段の拡張で足した根拠（#1362）の印と、枠を確保するかの関連度の規則（#1390）。

backend は実体の表から足した chunk の metadata の ``entity_expansion`` に足した理由を付ける。rag_engine は
その根拠も rerank にかけ、関連度が下限以上のものだけ、候補の位置を保ち（rerank で後ろへ下げない）、文書の
選択で後回しにせず、context の親の枠を確保する。下限未満のもの（質問と関係の薄い属性の chunk）は、印の無い
候補と同じく分数の順に並べる。backend の MCP の根拠の並び（``mcp_evidence_order``）も同じ規則で枠を確保する。
"""

from __future__ import annotations

import math
from typing import Any

# 枠を確保する拡張の根拠の rerank の関連度の下限（#1390）。起点の実体の種類と段（backend の ``entity_expansion`` の
# ``seed`` / ``hop``）で分け、起点が質問から遠いほど高くする。
# - 質問が名指しした実体の名寄せ（``seed="question"``・``hop=0``。台帳の行）は、質問の答えへの橋渡しで、質問の語と
#   重ならず検索で当たった chunk より分数が低い。明らかに関係しないものだけを外す低い下限にする。
# - その行の属性から 1 段でたどった根拠（``seed="question"``・``hop=1``。略号の表・承認者・重要度ごとの規程）は、
#   質問が聞く属性と別の属性からたどったもの（「いつ作業できるか」に対する障害の連絡）を外す。
# - 検索の上位の chunk の実体から足した根拠（``seed="chunk"``。保守計画の章に並ぶ別のシステムの台帳の行・障害メモの
#   「原因 A」など）は、質問と関係しないことが多い。検索で当たった chunk と同じ程度の関連度があるものだけを確保する。
# 値は #1362 の評価の KB（多段 69 問・業務支援 19 問）で、拡張の根拠の関連度を必要な根拠を含むか別に見て決めた
# （必要な根拠の最小: 質問の名寄せ 0.252・質問から 1 段 0.296・上位の chunk から 0.492）。
ENTITY_EXPANSION_MIN_RELEVANCE = 0.24
ENTITY_EXPANSION_HOP_MIN_RELEVANCE = 0.28
ENTITY_EXPANSION_CHUNK_SEED_MIN_RELEVANCE = 0.45
# 起点の種類（backend の ``app.rag.entity_expansion.SEED_CHUNK`` と同じ値）。
_SEED_CHUNK = "chunk"


def is_entity_expansion_record(record: Any) -> bool:
    """実体の 1 段の拡張で足した根拠か（backend が metadata の ``entity_expansion`` に理由を付ける。#1362）。"""
    metadata = getattr(record, "metadata", None)
    return isinstance(metadata, dict) and isinstance(metadata.get("entity_expansion"), dict)


def entity_expansion_min_relevance(info: Any) -> float:
    """拡張の根拠（``entity_expansion`` の理由）に求める rerank の関連度の下限（#1390）。"""
    if not isinstance(info, dict):
        return ENTITY_EXPANSION_MIN_RELEVANCE
    if info.get("seed") == _SEED_CHUNK:
        return ENTITY_EXPANSION_CHUNK_SEED_MIN_RELEVANCE
    hop = info.get("hop")
    if isinstance(hop, int) and not isinstance(hop, bool) and hop >= 1:
        return ENTITY_EXPANSION_HOP_MIN_RELEVANCE
    return ENTITY_EXPANSION_MIN_RELEVANCE


def entity_expansion_relevant(score: Any, info: Any = None) -> bool:
    """拡張の根拠の rerank の関連度が、枠を確保する下限以上か（#1390）。

    ``info`` は足した理由（metadata の ``entity_expansion``）で、起点の種類で下限を選ぶ。関連度が無い（rerank を
    実行しなかった・失敗した）ときは判断できないので確保する（#1362 と同じ）。
    """
    if score is None or isinstance(score, bool):
        return True
    try:
        value = float(score)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(value) or value >= entity_expansion_min_relevance(info)


def is_reserved_entity_expansion(record: Any) -> bool:
    """候補の位置を保ち、文書の選択で後回しにせず、context の枠を確保する拡張の根拠か（#1362・#1390）。

    実体の拡張の印があり、rerank の関連度（``metadata["rerank"]["relevance_score"]``）が起点の種類と段の下限以上か、
    関連度が無いもの。
    """
    if not is_entity_expansion_record(record):
        return False
    rerank = record.metadata.get("rerank")
    return entity_expansion_relevant(
        rerank.get("relevance_score") if isinstance(rerank, dict) else None,
        record.metadata.get("entity_expansion"),
    )


__all__ = [
    "ENTITY_EXPANSION_CHUNK_SEED_MIN_RELEVANCE",
    "ENTITY_EXPANSION_HOP_MIN_RELEVANCE",
    "ENTITY_EXPANSION_MIN_RELEVANCE",
    "entity_expansion_min_relevance",
    "entity_expansion_relevant",
    "is_entity_expansion_record",
    "is_reserved_entity_expansion",
]
