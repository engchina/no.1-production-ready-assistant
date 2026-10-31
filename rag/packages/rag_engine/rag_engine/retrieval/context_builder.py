"""検索候補から回答生成向けの context と evidence tree を構築する。"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from rag_engine.chunking import CHILD_CHUNK_LEVEL, PARENT_CHUNK_LEVEL
from rag_engine.knowledge.document_metadata import document_context_text
from rag_engine.parsing.decorative_pictures import (
    DECORATIVE_VISUAL_ROLE,
    INLINE_ICON_VISUAL_ROLE,
    visual_role_from_ref,
)
from rag_engine.parsing.picture_text import fold_picture_ocr_entries
from rag_engine.retrieval.article_catalog import is_added_catalog_article, is_catalog_article
from rag_engine.retrieval.attachment_evidence import is_attachment_evidence
from rag_engine.retrieval.entity_expansion import is_reserved_entity_expansion
from rag_engine.retrieval.evidence_cues import evidence_cues
from rag_engine.retrieval.metadata_context import answer_metadata_context, document_context_key, with_child_contexts
from rag_engine.retrieval.operation_context import operation_labels
from rag_engine.retrieval.reference_follow import is_followed_reference, reference_source_uid

DEFAULT_MAX_RECORD_TEXT_CHARS = 1200
DEFAULT_MIN_SNIPPET_CHARS = 80
CHILD_EVIDENCE_ROLE_PRIORITY = {
    "retrieved_anchor": 0,
    "neighbor_context": 1,
    "same_page_context": 2,
    "parent_context": 3,
    "operation_context": 4,
    "visual_context": 4,
}
ChildEvidenceRole = Literal[
    "retrieved_anchor",
    "neighbor_context",
    "same_page_context",
    "parent_context",
    "operation_context",
    "visual_context",
]
ParentEvidenceRole = Literal["synthesis_parent", "child_fallback"]


def _function_number() -> str:
    """番号付きの機能見出しの番号（「〜-(1)」。線の字は言語資源の evidence_cues）。"""
    return rf"[{evidence_cues().chars('function_number_dashes')}]\([0-9]+\)"


@dataclass(frozen=True)
class ContextEvidence:
    """回答 context に採用した record 単位の根拠を表します。"""

    record: Any
    role: Literal["primary", "supporting"]
    reason: str
    anchor_child_ids: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        """UI と trace 保存に使う JSON 互換 payload へ変換します。"""
        return {
            "id": _record_id(self.record),
            "chunk_uid": str(getattr(self.record, "chunk_uid", "") or ""),
            "chunk_id": str(getattr(self.record, "chunk_id", "") or ""),
            "chunk_level": str(getattr(self.record, "chunk_level", "") or ""),
            "parent_chunk_uid": str(getattr(self.record, "parent_chunk_uid", "") or ""),
            "role": self.role,
            "reason": self.reason,
            "anchor_child_ids": list(self.anchor_child_ids),
            "citation": str(getattr(self.record, "citation", "") or ""),
        }


@dataclass(frozen=True)
class ContextChildEvidence:
    """回答 context に採用した child chunk の根拠を表します。"""

    record: Any
    role: ChildEvidenceRole
    reason: str
    retrieval_rank: int | None = None

    def to_payload(self) -> dict[str, Any]:
        """UI と trace 保存に使う JSON 互換 payload へ変換します。"""
        return {
            "id": _record_id(self.record),
            "chunk_uid": str(getattr(self.record, "chunk_uid", "") or ""),
            "chunk_id": str(getattr(self.record, "chunk_id", "") or ""),
            "chunk_level": str(getattr(self.record, "chunk_level", "") or ""),
            "parent_chunk_uid": str(getattr(self.record, "parent_chunk_uid", "") or ""),
            "role": self.role,
            "reason": self.reason,
            "retrieval_rank": self.retrieval_rank,
            "citation": str(getattr(self.record, "citation", "") or ""),
        }


@dataclass(frozen=True)
class ContextParentEvidence:
    """child 根拠を束ねた parent chunk 単位の根拠を表します。"""

    record: Any
    role: ParentEvidenceRole
    reason: str
    children: tuple[ContextChildEvidence, ...] = ()
    evidence_score: float = 0.0

    @property
    def anchor_child_ids(self) -> tuple[str, ...]:
        """parent evidence の anchor となる child chunk ID を返します。"""
        return tuple(
            str(getattr(child.record, "chunk_id", "") or "")
            for child in self.children
            if child.role == "retrieved_anchor" and str(getattr(child.record, "chunk_id", "") or "")
        )

    def to_payload(self) -> dict[str, Any]:
        """UI と trace 保存に使う JSON 互換 payload へ変換します。"""
        return {
            "id": _record_id(self.record),
            "chunk_uid": str(getattr(self.record, "chunk_uid", "") or ""),
            "chunk_id": str(getattr(self.record, "chunk_id", "") or ""),
            "chunk_level": str(getattr(self.record, "chunk_level", "") or ""),
            "parent_chunk_uid": str(getattr(self.record, "parent_chunk_uid", "") or ""),
            "role": self.role,
            "reason": self.reason,
            "parent_evidence_score": self.evidence_score,
            "anchor_child_ids": list(self.anchor_child_ids),
            "citation": str(getattr(self.record, "citation", "") or ""),
            "children": [child.to_payload() for child in self.children],
        }


@dataclass
class _ChildEvidenceBuilder:
    record: Any
    role: ChildEvidenceRole
    reason: str
    retrieval_rank: int | None = None


@dataclass
class _ParentEvidenceBuilder:
    record: Any
    role: ParentEvidenceRole
    reason: str
    children: dict[str, _ChildEvidenceBuilder] = field(default_factory=dict)


@dataclass(frozen=True)
class ContextBundle:
    """回答に使う parent records、primary/supporting evidence、検索 trace を保持します。

    `text` は採用した parent ごとの可読 trace（文書背景・章・親本文・child 一覧）で、空判定・
    評価ツール（`domain_keyword_eval` / `rag_eval`）・SDK が読む。回答 prompt には渡さない。
    prompt 本文は `evidence_spans` が `records` の親本文から自前の予算で切り出す。
    """

    records: list[Any]
    text: str
    evidence: tuple[ContextEvidence, ...] = ()
    evidence_tree: tuple[ContextParentEvidence, ...] = ()
    status: Literal["ready", "insufficient"] = "ready"
    insufficient_reason: str = ""
    excluded_evidence: tuple[dict[str, str], ...] = ()

    @property
    def ready(self) -> bool:
        """後続処理に必要な準備が済んでいるかを返します。"""
        return self.status == "ready" and bool(self.records and self.text.strip())

    @property
    def primary_evidence(self) -> tuple[ContextEvidence, ...]:
        """回答に最も直接使う primary evidence を返します。"""
        return tuple(item for item in self.evidence if item.role == "primary")

    @property
    def supporting_evidence(self) -> tuple[ContextEvidence, ...]:
        """primary 以外の補助 evidence を返します。"""
        return tuple(item for item in self.evidence if item.role == "supporting")

    def trace_payload(self) -> dict[str, Any]:
        """context 構築の根拠を answer trace 用 payload に変換します。"""
        return {
            "status": self.status,
            "insufficient_reason": self.insufficient_reason,
            "excluded_evidence": list(self.excluded_evidence),
            "evidence": [item.to_payload() for item in self.evidence],
            "evidence_tree": [item.to_payload() for item in self.evidence_tree],
        }


# 検索上位から起点（retrieved_anchor）に採る child の、同じ親あたりの既定上限 (#669)。
MAX_ANCHORS_PER_PARENT = 2


@dataclass(frozen=True)
class ContextBuildRequest:
    """context 構築時の候補、件数上限、近傍展開条件をまとめます。"""

    question: str
    ranked_children: Sequence[Any]
    active_records: Sequence[Any]
    top_k: int
    neighbor_child_count: int
    max_records: int
    max_chars: int
    metadata: dict[str, Any] = field(default_factory=dict)
    # 主検索枠を保持したまま追加できる同一操作の親数。0は既存の総数制限を維持する。
    support_record_limit: int = 0
    # 検索上位から起点（retrieved_anchor）に採る child の、同じ親あたりの上限 (#669)。
    # Settings.max_anchors_per_parent（RAG_ENGINE_MAX_ANCHORS_PER_PARENT）から渡す (#889)。1 未満は 1。
    max_anchors_per_parent: int = MAX_ANCHORS_PER_PARENT
    # 条の目録で選んだ条（#2254）の起点と親を、上の起点の数（top_k）と親の数（max_records）の外に足す数。
    # 選んだ条は起点に数えず、検索の根拠の親を減らさない。0 は足さない（選んだ条も他の候補と同じに扱う）。
    # 会話の添付の箇所（#3212）も同じく起点・親の数の外にすべて足す（数の上限は backend が選ぶ箇所の数）。
    catalog_records: int = 0
    # 原文の遅延取得のための起点選択専用。返した本文は回答に使わず、親参照で起点をまとめる。
    select_anchors_only: bool = False


def build_chunk_context_bundle(request: ContextBuildRequest) -> ContextBundle:
    """検索済み child chunk から parent/neighbor を展開して context bundle を作ります。"""
    if request.max_records <= 0:
        return ContextBundle(
            records=[],
            text="",
            status="insufficient",
            insufficient_reason="context record budget is zero",
        )

    active_records = [record for record in request.active_records if _record_active(record)]
    active_ids = {record_context_key(record) for record in active_records if record_context_key(record)}
    children = [record for record in active_records if str(getattr(record, "chunk_level", "") or "") == CHILD_CHUNK_LEVEL]
    if not children:
        return ContextBundle(
            records=[],
            text="",
            status="insufficient",
            insufficient_reason="no active child chunks are available",
        )

    ranked_children = [
        record
        for record in request.ranked_children
        if record_context_key(record) in active_ids and str(getattr(record, "chunk_level", "") or "") == CHILD_CHUNK_LEVEL
    ]
    if not ranked_children:
        return ContextBundle(
            records=[],
            text="",
            status="insufficient",
            insufficient_reason="retrieval returned no active child chunks",
        )

    parents = {
        record_context_key(record): record
        for record in active_records
        if str(getattr(record, "chunk_level", "") or "") == PARENT_CHUNK_LEVEL and record_context_key(record)
    }
    children_by_parent: dict[str, list[Any]] = {}
    for child in sorted(
        children,
        key=lambda record: (_record_engine(record), _record_chunk_seq(record)),
    ):
        children_by_parent.setdefault(_child_sibling_context_key(child), []).append(child)

    groups: list[_ParentEvidenceBuilder] = []
    groups_by_id: dict[str, _ParentEvidenceBuilder] = {}

    def group_for_child(child: Any) -> _ParentEvidenceBuilder | None:
        parent_key = _record_parent_context_key(child)
        parent = parents.get(parent_key) if parent_key and _uses_parent_context(child) else None
        if parent is not None and (
            _artifact_context_key(parent) != _artifact_context_key(child) or not str(getattr(parent, "text", "") or "").strip()
        ):
            parent = None
        record = parent or child
        group_id = (
            f"{_artifact_context_key(child)}::{parent_key}"
            if request.select_anchors_only and parent_key and _uses_parent_context(child)
            else record_context_key(record)
        )
        if not group_id:
            return None
        group = groups_by_id.get(group_id)
        if group is not None:
            return group
        group = _ParentEvidenceBuilder(
            record=record,
            role="synthesis_parent" if parent is not None else "child_fallback",
            reason=(
                "parent_of_retrieved_child"
                if parent is not None
                else "parent_unavailable_child_fallback"
                if _uses_parent_context(child)
                else "retrieved_chunk"
            ),
        )
        groups_by_id[group_id] = group
        groups.append(group)
        return group

    def add_child(
        group: _ParentEvidenceBuilder,
        child: Any,
        role: ChildEvidenceRole,
        reason: str,
        *,
        retrieval_rank: int | None = None,
    ) -> None:
        child_id = record_context_key(child)
        if not child_id:
            return
        existing = group.children.get(child_id)
        if existing is None:
            group.children[child_id] = _ChildEvidenceBuilder(
                record=child,
                role=role,
                reason=reason,
                retrieval_rank=retrieval_rank,
            )
            return
        if _child_role_priority(role) < _child_role_priority(existing.role):
            existing.role = role
            existing.reason = reason
            # 先に前後の文脈として入った chunk が起点になったときは、rerank の結果（関連度）を持つ候補の
            # record に替える（同じ親の別の起点の前後の文脈に先に入ると、関連度が失われていた。#1390）。
            existing.record = child
        if retrieval_rank is not None and (existing.retrieval_rank is None or retrieval_rank < existing.retrieval_rank):
            existing.retrieval_rank = retrieval_rank

    top_k = max(1, int(request.top_k or 1))
    max_anchors_per_parent = max(1, int(request.max_anchors_per_parent or 1))
    anchors_by_parent: dict[str, int] = {}
    anchor_count = 0
    catalog_limit = max(0, int(request.catalog_records or 0))
    # 目録で選んだ条（#2254）は、検索の根拠の起点と親を決めた後に足す（検索の根拠の選び方を変えない）。
    catalog_children: list[tuple[int, Any]] = []
    # 会話の添付の箇所（#3212）は、rerank の順位に関わらず起点・親を決めた後に足す。利用者が明示した入力だが、
    # 質問の文と表の行の rerank の関連度は低く出やすく、順位で起点を取ると `top_k` の外に落ちる。
    attachment_children = [(rank, child) for rank, child in enumerate(ranked_children, start=1) if is_attachment_evidence(child)]
    for retrieval_rank, child in enumerate(ranked_children, start=1):
        if is_attachment_evidence(child):
            continue
        catalog = catalog_limit > 0 and is_catalog_article(child)
        # 加えた条は検索の根拠の起点に数えない。検索の候補にあった条は今までどおり起点になり、枠の外に出たときだけ足す。
        if catalog and (is_added_catalog_article(child) or anchor_count >= top_k):
            if len(catalog_children) < catalog_limit:
                catalog_children.append((retrieval_rank, child))
            continue
        if anchor_count >= top_k:
            if len(catalog_children) >= catalog_limit:
                break
            continue
        group = group_for_child(child)
        if group is None:
            continue
        # 同じ親の child は起点を最大 MAX_ANCHORS_PER_PARENT 件に抑える。親が 6,000 字だと 1 親に child が
        # 6 件以上あり、上位 top_k が 2〜3 の機能で埋まって別の要求の節（複数要求の問い合わせの片方）が
        # 入らない。親の本文は起点 1 件で全 child が入るため、同じ親の起点を増やしても根拠は増えない (#669)。
        parent_key = _child_sibling_context_key(child) if _uses_parent_context(child) else record_context_key(child)
        if anchors_by_parent.get(parent_key, 0) >= max_anchors_per_parent:
            continue
        anchors_by_parent[parent_key] = anchors_by_parent.get(parent_key, 0) + 1
        # 同じ親へ集約する child は引用の位置だけを追加し、次の異なる原文の枠を消費しない。
        if not any(item.role == "retrieved_anchor" for item in group.children.values()):
            anchor_count += 1
        add_child(
            group,
            child,
            "retrieved_anchor",
            "top_retrieved_child",
            retrieval_rank=retrieval_rank,
        )

    # 起点は Rerank 順を保つ。親子分割では親本文だけを使い、隣接親・同一ページ・子全体を補完しない。
    primary = _reserve_entity_expansion_groups(sorted(groups, key=_parent_group_sort_key), request.max_records)
    selected = list(primary)
    selected_groups = {id(group) for group in selected}
    added_children = [
        *((rank, child, "catalog_article") for rank, child in catalog_children),
        *((rank, child, "conversation_attachment") for rank, child in attachment_children),
    ]
    for retrieval_rank, child, reason in added_children:
        group = group_for_child(child)
        if group is None:
            continue
        add_child(group, child, "retrieved_anchor", reason, retrieval_rank=retrieval_rank)
        if id(group) not in selected_groups:
            group.reason = reason
            selected.append(group)
            selected_groups.add(id(group))
    # 非親子の前後は同じ成果物の保存順から選び、原文の個別 record として回答へ渡す。
    neighbors: dict[str, Any] = {}
    for group in selected:
        for anchor in tuple(group.children.values()):
            if anchor.role != "retrieved_anchor" or _uses_parent_context(anchor.record):
                continue
            siblings = children_by_parent.get(_child_sibling_context_key(anchor.record), [])
            for neighbor in _chunk_neighbors(anchor.record, siblings, request.neighbor_child_count):
                key = record_context_key(neighbor)
                if key not in {record_context_key(entry.record) for entry in selected}:
                    neighbors.setdefault(key, neighbor)
    for neighbor in neighbors.values():
        if len(selected) >= request.max_records + len(added_children):
            break
        group = group_for_child(neighbor)
        if group is None or id(group) in selected_groups:
            continue
        group.reason = "adjacent_child"
        add_child(group, neighbor, "neighbor_context", "adjacent_child")
        selected.append(group)
        selected_groups.add(id(group))
    # 図に保存された明示的な原文 ID の対応は、距離による近傍補完とは分けて保つ。
    visual_limit = max(0, request.support_record_limit)
    visual_added = 0
    selected_keys = {record_context_key(group.record) for group in selected}
    for anchor_group in tuple(selected):
        for parent in _visual_context_parents(anchor_group.record, list(parents.values())):
            key = record_context_key(parent)
            if key in selected_keys or visual_added >= visual_limit:
                continue
            group = _ParentEvidenceBuilder(record=parent, role="synthesis_parent", reason="visual_context_source")
            for child in children_by_parent.get(f"{_artifact_context_key(parent)}::{key}", []):
                add_child(group, child, "visual_context", "explicit_visual_context_source")
            selected.append(group)
            selected_keys.add(key)
            visual_added += 1
    evidence_tree = _finalize_parent_evidence(selected)
    return _context_bundle_from_parent_evidence(evidence_tree, max_chars=request.max_chars)


def _group_has_entity_expansion(group: _ParentEvidenceBuilder) -> bool:
    """枠を確保する実体の 1 段の拡張の根拠（#1362。関連度が下限以上。#1390）を起点に持つ親か。"""
    return any(child.role == "retrieved_anchor" and is_reserved_entity_expansion(child.record) for child in group.children.values())


def _group_follows_reference(group: _ParentEvidenceBuilder, sources: set[str]) -> bool:
    """参照元の直後へ置いた交差参照の参照先（#1549）を起点に持ち、その参照元が枠の内の起点の親か。"""
    return any(
        child.role == "retrieved_anchor" and is_followed_reference(child.record) and reference_source_uid(child.record) in sources
        for child in group.children.values()
    )


def _reserve_entity_expansion_groups(ranked_groups: Sequence[_ParentEvidenceBuilder], max_records: int) -> list[_ParentEvidenceBuilder]:
    """context の親の枠（``max_records``）に、実体の 1 段の拡張の根拠の親を確保する（#1362）。

    拡張の根拠（台帳の行・略号の表）は質問の語と重ならず rerank の分数が低いため、分数の順で切ると落ちる。枠の
    外に落ちた拡張の親を、枠の後ろ（拡張でない親の分数の低い側）と入れ替える。入れ替えるのは枠の半分まで（検索
    で当たった主根拠を半分より多くは落とさない）。確保するのは関連度が下限（``ENTITY_EXPANSION_MIN_RELEVANCE``）
    以上の拡張の根拠の親だけで、質問と関係の薄い属性の chunk の親で、検索で当たった親（前後の文脈・章の参照の先
    を持つ）を入れ替えない（#1390）。拡張の根拠が無ければ今までと同じ。

    交差参照の参照先（参照元の直後へ置いたもの。#1549）の親も、参照元の親が枠の内にあれば同じ枠で確保する
    （参照先は質問の語と重ならない橋渡しの根拠で、親を分数の順に並べると枠の外に落ちる。kb1517 の定期保守計画の
    第 1 章「この章に無いシステムは、第 2 章の共通の保守枠で保守します」）。
    """
    primary = list(ranked_groups[:max_records])
    sources = {
        str(getattr(child.record, "chunk_uid", "") or "")
        for group in primary
        for child in group.children.values()
        if child.role == "retrieved_anchor"
    }

    def bridge(group: _ParentEvidenceBuilder) -> bool:
        return _group_has_entity_expansion(group) or _group_follows_reference(group, sources)

    reserved = [group for group in ranked_groups[max_records:] if bridge(group)]
    if not reserved:
        return primary
    room = max(0, max_records - max(1, max_records // 2) - sum(bridge(g) for g in primary))
    reserved = reserved[:room]
    if not reserved:
        return primary
    kept = list(primary)
    for _ in reserved:
        for index in range(len(kept) - 1, -1, -1):
            if not bridge(kept[index]):
                del kept[index]
                break
    return [*kept, *reserved]


def _operation_labels(record: Any) -> set[str]:
    """共通の機能見出し規則を使い、別画面の隣接展開を防ぐ。"""
    return operation_labels(record)


def _visual_context_parents(anchor: Any, parents: Sequence[Any], *, include_self: bool = False) -> list[Any]:
    """画像に実際に添付した同一版の本文を、明示的な原文 ID から補完する。

    同じページや類似見出しだけでは関連付けない。部分表示、別解析版、別ファイル、
    別チャンク版は除外する。操作の同一性は保証せず、回答側で条件を照合する。
    件数は呼出し側の共通補助枠で制限し、補完先から再帰的には展開しない。
    include_selfは同一parent内の画像・本文の明示対応を検査する場合だけ指定する。
    """
    version = str(getattr(anchor, "chunk_uid", "") or "").partition(":")[0]
    if not version or not getattr(anchor, "source_run_id", ""):
        return []
    wanted = set()
    for ref in getattr(anchor, "source_record_refs", ()):
        for context in ref.get("vision_context_record_refs", ()):
            if (
                context.get("partially_visible") is False
                and context.get("category") in {"Text", "List-item"}
                and context.get("record_id")
                and context.get("page")
            ):
                wanted.add((context["record_id"], context["page"]))
    if not wanted:
        return []
    return [
        parent
        for parent in parents
        if _record_scope_context_key(parent) == _record_scope_context_key(anchor)
        and str(getattr(parent, "chunk_uid", "") or "").partition(":")[0] == version
        and (include_self or record_context_key(parent) != record_context_key(anchor))
        and any(
            (ref.get("record_id"), ref.get("page")) in wanted and ref.get("category") in {"Text", "List-item"}
            for ref in getattr(parent, "source_record_refs", ())
        )
    ]


def _operation_parent_neighbors(anchor: Any, parents: Sequence[Any]) -> list[Any]:
    """同一ファイル・解析/チャンク版で操作見出しが一致する前後2ページを返す。

    見出しが無い場合は推測して展開しない。同一ページだけを根拠に別画面を
    混ぜず、近いページ・chunkから採用する。呼び出し側が総件数を制限する。
    """
    labels = _operation_labels(anchor)
    if not labels or sum(bool(re.search(_function_number(), label)) for label in labels) > 1:
        return []
    page = _record_page(anchor)
    end = int(getattr(anchor, "page_end", 0) or page)
    scope = _record_scope_context_key(anchor)
    uid = str(getattr(anchor, "chunk_uid", "") or "").partition(":")[0]
    candidates = []
    for parent in parents:
        if record_context_key(parent) == record_context_key(anchor) or _record_scope_context_key(parent) != scope:
            continue
        other_uid = str(getattr(parent, "chunk_uid", "") or "").partition(":")[0]
        if uid and other_uid and uid != other_uid:
            continue
        start = _record_page(parent)
        stop = int(getattr(parent, "page_end", 0) or start)
        distance = max(0, start - end, page - stop)
        other_labels = _operation_labels(parent)
        if sum(bool(re.search(_function_number(), label)) for label in other_labels) > 1:
            continue
        if page > 0 and start > 0 and distance <= 2 and labels.intersection(other_labels):
            candidates.append((distance, abs(_record_chunk_seq(parent) - _record_chunk_seq(anchor)), record_context_key(parent), parent))
    return [item[-1] for item in sorted(candidates, key=lambda item: item[:3])]


def context_bundle_from_parent_evidence(
    evidence_tree: Sequence[ContextParentEvidence],
    *,
    max_chars: int,
) -> ContextBundle:
    """parent evidence tree から LLM 用 context bundle を組み立てます。"""
    return _context_bundle_from_parent_evidence(evidence_tree, max_chars=max_chars)


def _child_role_priority(role: str) -> int:
    return CHILD_EVIDENCE_ROLE_PRIORITY.get(role, 99)


def _finalize_parent_evidence(
    groups: Sequence[_ParentEvidenceBuilder],
) -> tuple[ContextParentEvidence, ...]:
    finalized: list[ContextParentEvidence] = []
    for group in groups:
        children = tuple(
            ContextChildEvidence(
                record=item.record,
                role=item.role,
                reason=item.reason,
                retrieval_rank=item.retrieval_rank,
            )
            for item in sorted(
                group.children.values(),
                key=lambda child: (
                    _record_engine(child.record),
                    _record_chunk_seq(child.record),
                    _record_id(child.record),
                ),
            )
        )
        finalized.append(
            ContextParentEvidence(
                record=group.record,
                role=group.role,
                reason=group.reason,
                children=children,
                evidence_score=_parent_group_evidence_score(group),
            )
        )
    return tuple(finalized)


def _parent_group_sort_key(group: _ParentEvidenceBuilder) -> tuple[Any, ...]:
    """親の並び（`max_records` の切り・根拠の予算の配分・評価器に渡す順が引き継ぐ）。

    主キーは起点の最上位の順位（rerank の後の候補の並び = rerank の点数の順。MaxP。#3504）。rerank の後の
    並びは、rerank の工程の明示の配置（質問が名指しした画面の先頭寄せ・交差参照の参照先を参照元の直後へ・
    位置を保つ候補。`rerank_records`）だけが点数の順の例外で、ここでは並べ替えない。起点の数・前後の chunk の
    数・RRF などの合成の点数は同点の順だけに使う（件数で、上位の起点 1 件の親を下げない）。
    """
    anchor_ranks = [
        child.retrieval_rank for child in group.children.values() if child.role == "retrieved_anchor" and child.retrieval_rank is not None
    ]
    best_rank = min(anchor_ranks) if anchor_ranks else 1_000_000
    return (
        best_rank,
        -_parent_group_evidence_score(group),
        _record_page(group.record),
        _record_chunk_seq(group.record),
        record_context_key(group.record),
    )


def _parent_group_evidence_score(group: _ParentEvidenceBuilder) -> float:
    anchor_children = [child for child in group.children.values() if child.role == "retrieved_anchor"]
    if not anchor_children:
        return 0.0
    best_rank = (
        min(child.retrieval_rank for child in anchor_children if child.retrieval_rank is not None)
        if any(child.retrieval_rank is not None for child in anchor_children)
        else 1_000_000
    )
    rank_score = 1.0 / max(1, best_rank)
    anchor_bonus = min(max(0, len(anchor_children) - 1), 6) * 0.35
    support_bonus = (
        min(
            sum(1 for child in group.children.values() if child.role in {"neighbor_context", "same_page_context"}),
            6,
        )
        * 0.04
    )
    parent_context_bonus = (
        min(
            sum(1 for child in group.children.values() if child.role == "parent_context"),
            8,
        )
        * 0.01
    )
    rerank_score = max(
        (_metadata_float(child.record, "rerank", "relevance_score") for child in anchor_children),
        default=0.0,
    )
    rrf_score = max(
        (_metadata_float(child.record, "adb_hybrid", "rrf_score") for child in anchor_children),
        default=0.0,
    )
    profile_score = max(
        (_metadata_float(child.record, "adb_hybrid", "profile_score") for child in anchor_children),
        default=0.0,
    )
    return round(
        rank_score
        + anchor_bonus
        + support_bonus
        + parent_context_bonus
        + rerank_score
        + (rrf_score * 8.0)
        + min(profile_score * 0.05, 0.5),
        6,
    )


def _metadata_float(record: Any, namespace: str, key: str) -> float:
    metadata = getattr(record, "metadata", None)
    if not isinstance(metadata, dict):
        return 0.0
    bucket = metadata.get(namespace)
    if not isinstance(bucket, dict):
        return 0.0
    try:
        value = float(bucket.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(value):
        return 0.0
    return value


def _context_bundle_from_parent_evidence(
    evidence_tree: Sequence[ContextParentEvidence],
    *,
    max_chars: int,
) -> ContextBundle:
    """parent evidence を records・evidence・trace text にまとめる。

    max_chars は親本文（`record.text`）1 件あたりの上限で、trace の前置きや child 一覧の分は数えない。
    回答 prompt は `evidence_spans` が自前の予算で親本文から抜粋するため、ここでは prompt の分割や
    予算配分はしない。親本文が上限を超える候補は chunking の parent size を見直す合図として除外し、
    全候補が超えていれば insufficient にする。
    """
    if max_chars <= 0:
        return ContextBundle(
            records=[],
            text="",
            evidence=(),
            evidence_tree=(),
            status="insufficient",
            insufficient_reason="context character budget is zero",
        )
    excluded = tuple(
        {"id": _record_id(item.record), "reason": "context_character_budget", "context_reason": item.reason}
        for item in evidence_tree
        if len(str(getattr(item.record, "text", "") or "")) > max_chars
    )
    kept = tuple(item for item in evidence_tree if len(str(getattr(item.record, "text", "") or "")) <= max_chars)
    if evidence_tree and not kept:
        return ContextBundle(
            records=[],
            text="",
            evidence=(),
            evidence_tree=(),
            status="insufficient",
            insufficient_reason=(
                f"parent chunk exceeds synthesis budget; re-run chunking with a smaller parent size: {_record_id(evidence_tree[0].record)}"
            ),
            excluded_evidence=excluded,
        )
    evidence_tree = kept
    sections = []
    seen_documents = set()
    for item in evidence_tree:
        key = document_context_key(item.record)
        sections.append(
            _parent_context_section(item, include_first_page=key not in seen_documents, metadata_max_chars=min(1200, max_chars // 10))
        )
        seen_documents.add(key)
    text = "\n\n".join(sections).strip()
    if not text:
        return ContextBundle(
            records=[],
            text="",
            evidence=(),
            evidence_tree=(),
            status="insufficient",
            insufficient_reason="no parent context text fits within the token budget",
        )

    records = [item.record for item in evidence_tree]
    evidence = tuple(
        ContextEvidence(
            record=item.record,
            role="supporting" if item.reason in {"same_operation_context", "visual_context_source", "adjacent_child"} else "primary",
            reason=item.reason,
            anchor_child_ids=item.anchor_child_ids,
        )
        for item in evidence_tree
    )
    return ContextBundle(
        records=records,
        text=text,
        evidence=evidence,
        evidence_tree=evidence_tree,
        excluded_evidence=excluded,
    )


def _parent_context_section(item: ContextParentEvidence, *, include_first_page: bool = True, metadata_max_chars: int = 1200) -> str:
    record = with_child_contexts(item.record, [child.record for child in item.children])
    lines = [
        _context_prefix(record, include_first_page=include_first_page, metadata_max_chars=metadata_max_chars).rstrip(),
        f"Context role: {item.reason}",
    ]
    anchor_ids = item.anchor_child_ids
    if anchor_ids:
        lines.append("Retrieved anchor children: " + ", ".join(anchor_ids))
    lines.append("Synthesis text:")
    lines.append(str(getattr(record, "text", "") or "").strip())
    if item.children:
        lines.append("Children:")
        for child in item.children:
            rank = f" rank={child.retrieval_rank}" if child.retrieval_rank is not None else ""
            lines.append(
                "- "
                f"{_record_id(child.record)} / {child.role}{rank} / "
                f"{_record_page_label(child.record)} / {_child_context_preview(child.record)}"
            )
    return "\n".join(line for line in lines if line is not None).strip()


def _child_context_preview(record: Any) -> str:
    return trim_context_text(str(getattr(record, "text", "") or ""), 180)


def _record_page_label(record: Any) -> str:
    page = _record_page(record)
    page_end = _record_page_end(record)
    if not page:
        return "p.-"
    return f"p.{page}" if page == page_end else f"p.{page}-{page_end}"


def context_bundle_from_records(
    records: Sequence[Any],
    *,
    max_chars: int,
    evidence: Sequence[ContextEvidence] = (),
    max_record_text_chars: int = DEFAULT_MAX_RECORD_TEXT_CHARS,
) -> ContextBundle:
    """legacy record 列から context bundle を作ります。"""
    selected: list[Any] = []
    lines: list[str] = []
    used_chars = 0
    seen_documents = set()
    for record in records:
        key = document_context_key(record)
        prefix = _context_prefix(record, include_first_page=key not in seen_documents, metadata_max_chars=min(1200, max_chars // 10))
        remaining = max_chars - used_chars - len(prefix) - 1
        if remaining <= DEFAULT_MIN_SNIPPET_CHARS and lines:
            break
        snippet = trim_context_text(
            str(getattr(record, "text", "") or ""),
            min(max_record_text_chars, max(DEFAULT_MIN_SNIPPET_CHARS, remaining)),
        )
        line = f"{prefix}{snippet}"
        if lines and used_chars + len(line) + 1 > max_chars:
            break
        selected.append(record)
        seen_documents.add(key)
        lines.append(line)
        used_chars += len(line) + 1

    if not selected or not "\n".join(lines).strip():
        return ContextBundle(
            records=[],
            text="",
            evidence=(),
            status="insufficient",
            insufficient_reason="no context text fits within the token budget",
        )

    selected_ids = {_record_id(record) for record in selected}
    selected_evidence = tuple(item for item in evidence if _record_id(item.record) in selected_ids)
    if not selected_evidence:
        selected_evidence = tuple(ContextEvidence(record=record, role="primary", reason="ranked_context") for record in selected)
    return ContextBundle(
        records=selected,
        text="\n".join(lines),
        evidence=selected_evidence,
    )


def record_has_image_evidence(record: Any) -> bool:
    """record metadata に利用可能な画像 evidence があるかを判定します。"""
    metadata = getattr(record, "metadata", None)
    if not isinstance(metadata, dict):
        return False
    return any(_iter_image_evidence_refs(metadata))


def _iter_image_evidence_refs(metadata: dict[str, Any]):
    # 図の中の文字（OCR）の entry は持ち主の図にまとめて数える（#3455）。
    raw_images = metadata.get("image_evidence")
    if isinstance(raw_images, list):
        for raw in fold_picture_ocr_entries([raw for raw in raw_images if isinstance(raw, dict)]):
            if _is_usable_image_ref(raw):
                yield raw

    table_context = metadata.get("table_context")
    if not isinstance(table_context, list):
        return
    for table in table_context:
        if not isinstance(table, dict):
            continue
        raw_visuals = table.get("visual_evidence")
        if not isinstance(raw_visuals, list):
            continue
        for raw in fold_picture_ocr_entries([raw for raw in raw_visuals if isinstance(raw, dict)]):
            if _is_usable_image_ref(raw):
                yield raw


def _is_usable_image_ref(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    if str(raw.get("raw_type") or "") == "picture_ocr_text":
        return False
    if visual_role_from_ref(raw) in {DECORATIVE_VISUAL_ROLE, INLINE_ICON_VISUAL_ROLE}:
        return False
    if raw.get("rag_excluded"):
        return False
    return bool(str(raw.get("image_id") or raw.get("record_id") or "").strip())


def trim_context_text(value: str, max_length: int) -> str:
    """context 上限に収まるよう文字列を安全に切り詰めます。"""
    normalized = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(normalized) <= max_length:
        return normalized
    return normalized[: max(0, max_length - 3)] + "..."


def _context_prefix(record: Any, *, include_first_page: bool = True, metadata_max_chars: int = 1200) -> str:
    citation = str(getattr(record, "citation", "") or "")
    engine_label = str(getattr(record, "engine_label", "") or "")
    category = str(getattr(record, "category", "") or "")
    metadata = getattr(record, "metadata", {})
    document = metadata.get("document") if isinstance(metadata, dict) else None
    details = trim_context_text(document_context_text(document), 500) if isinstance(document, dict) else ""
    # 文書の時点を回答にも渡す。技術ID・品質スコアを根拠本文へ混ぜない。
    if isinstance(document, dict) and document.get("source_uri"):
        source = f"Source URL: {document['source_uri']}"
        # URL の途中切断で誤った出典を生成しない。長い URL は保存 metadata にのみ残す。
        if len(details) + len(source) + 3 <= 700:
            details = " / ".join(part for part in (details, source) if part)
    suffix = f"\nDocument: {details}\n" if details else " "
    context = answer_metadata_context(record, max_chars=metadata_max_chars, include_first_page=include_first_page, include_document=False)
    return f"[{citation} / {engine_label} / {category}]" + suffix + (context + "\n" if context else "")


def _chunk_neighbors(child: Any, siblings: Sequence[Any], neighbor_child_count: int) -> list[Any]:
    count = max(0, int(neighbor_child_count or 0))
    if count <= 0:
        return []
    index_by_id = {record_context_key(record): index for index, record in enumerate(siblings)}
    index = index_by_id.get(record_context_key(child))
    if index is None:
        return []
    neighbors: list[Any] = []
    for offset in range(1, count + 1):
        before = index - offset
        after = index + offset
        if before >= 0:
            neighbors.append(siblings[before])
        if after < len(siblings):
            neighbors.append(siblings[after])
    return neighbors


def _same_page_chunk_neighbors(
    child: Any,
    children: Sequence[Any],
    neighbor_child_count: int,
) -> list[Any]:
    count = max(0, int(neighbor_child_count or 0))
    if count <= 0:
        return []
    child_pages = set(range(_record_page(child), _record_page_end(child) + 1))
    candidates = [
        record
        for record in children
        if record_context_key(record) != record_context_key(child)
        and child_pages & set(range(_record_page(record), _record_page_end(record) + 1))
    ]
    candidates.sort(
        key=lambda record: (
            abs(_record_chunk_seq(record) - _record_chunk_seq(child)),
            _record_chunk_seq(record),
        )
    )
    return candidates[:count]


def _record_active(record: Any) -> bool:
    metadata = getattr(record, "metadata", None)
    if not isinstance(metadata, dict):
        return True
    value = metadata.get("active")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text not in {"0", "false", "no", "off", "inactive"}


def _record_id(record: Any) -> str:
    return str(getattr(record, "id", None) or getattr(record, "chunk_id", None) or getattr(record, "citation", None) or "")


def record_context_key(record: Any) -> str:
    """重複除外に使う record の安定 key を作ります。"""
    chunk_key = _record_chunk_context_key(record)
    if chunk_key:
        return chunk_key
    record_id = _record_id(record)
    if not record_id:
        return ""
    return _scoped_record_context_key("record", record, record_id)


def _scoped_uid_key(record: Any, uid: str) -> str:
    key = f"chunk_uid:{uid}"
    scope = (getattr(record, "metadata", None) or {}).get("context_page_ranges", "")
    return f"{key}::page_scope:{scope}" if scope else key


def _record_chunk_context_key(record: Any) -> str:
    chunk_uid = str(getattr(record, "chunk_uid", "") or "").strip()
    if chunk_uid:
        return _scoped_uid_key(record, chunk_uid)
    chunk_id = str(getattr(record, "chunk_id", "") or "").strip()
    if not chunk_id:
        return ""
    return _scoped_record_context_key("chunk", record, chunk_id)


def _record_parent_context_key(record: Any) -> str:
    parent_chunk_uid = str(getattr(record, "parent_chunk_uid", "") or "").strip()
    if parent_chunk_uid:
        return _scoped_uid_key(record, parent_chunk_uid)
    parent_chunk_id = str(getattr(record, "parent_chunk_id", "") or "").strip()
    if not parent_chunk_id:
        return ""
    return _scoped_record_context_key("chunk", record, parent_chunk_id)


def _child_sibling_context_key(record: Any) -> str:
    if _uses_parent_context(record):
        return f"{_artifact_context_key(record)}::{_record_parent_context_key(record)}"
    return _artifact_context_key(record)


def _uses_parent_context(record: Any) -> bool:
    """保存された方式を優先する。SDK の親子型は明示的な親子参照を契約にする。"""
    metadata = getattr(record, "metadata", {}) or {}
    if "chunk_strategy" in metadata:
        return metadata["chunk_strategy"] in {"small_to_big", "small_to_big_parent_child"}
    return bool(_record_parent_context_key(record))


def _artifact_context_key(record: Any) -> str:
    """文書・レシピ・chunk_set・解析版の境界を近傍選択に保つ。"""
    metadata = getattr(record, "metadata", {}) or {}
    return repr(
        (_record_scope_context_key(record), *(str(metadata.get(key) or "") for key in ("document_id", "recipe_id", "chunk_set_id")))
    )


def _record_scope_context_key(record: Any) -> str:
    return _scoped_record_context_key("scope", record, "")


def _scoped_record_context_key(kind: str, record: Any, value: str) -> str:
    source_run_id = str(getattr(record, "source_run_id", "") or "")
    source = str(getattr(record, "source", "") or getattr(record, "source_file_name", "") or "")
    engine = _record_engine(record)
    scope_parts: tuple[str, ...] = (source_run_id, source, engine)
    if page_scope := str((getattr(record, "metadata", None) or {}).get("context_page_ranges", "")):
        scope_parts = (*scope_parts, page_scope)
    if not any(scope_parts):
        return f"{kind}:{value}" if value else kind
    escaped = [part.replace("::", r"\::") for part in (kind, *scope_parts, value)]
    return "::".join(escaped)


def _record_engine(record: Any) -> str:
    return str(getattr(record, "engine", "") or "")


def _record_chunk_seq(record: Any) -> int:
    try:
        return int(getattr(record, "chunk_seq", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _record_page(record: Any) -> int:
    try:
        return int(getattr(record, "page", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _record_page_end(record: Any) -> int:
    try:
        value = getattr(record, "page_end", None)
        return int(value if value is not None else _record_page(record))
    except (TypeError, ValueError):
        return _record_page(record)
