"""質問から検索・回答プロファイルを推薦する（MCP の `rag_recommend_search_answer_profile`。#1850）。

外部の Agent・業務 Agent が、質問に合う検索・回答プロファイルを一覧の説明だけで選ぶと、関係の無い
プロファイルを順に検索してしまう（#1846）。推薦は LLM を呼ばず、次の 2 つで決める:

1. 検索の当たり: 利用者が使えるプロファイルの参照先のナレッジベース（ACTIVE・利用者が使える KB）を
   まとめて 1 回 hybrid 検索し（質問の埋め込みは 1 回。検索と同じ見え方の条件）、当たった文書が
   属するナレッジベースからプロファイルに点を付ける（順位が上ほど高い）。同じ文書名・同じ本文の
   chunk（同じ資料を複数のナレッジベースに入れたもの）は同じ順位に数える。点に数えるのは上位の
   `RANK_DEPTH` 件の本文だけ（#1865。下位の弱い当たりを多く持つ大きいプロファイルが、上位の
   当たりを持つプロファイルを数で上回らないようにする）。
2. 名前・説明の重なり: 質問とプロファイルの名前・説明・参照先のナレッジベースの名前の文字の
   2-gram の重なり（検索の当たりが同じときの手がかり。重みは小さい）。

1 位と近い点の候補が、1 位と 1 つも同じ文書に当たっていなければ（上位の文書が重ならない）「割れた」
とみなし、推薦を null にして候補の一覧を返す（呼び出し元が利用者に確かめる。「回答前の確認」#1839 の
選択肢にできる形）。近い候補が 1 位と同じ文書にも当たっていれば、同じ資料を持つプロファイル
（評価の写し・版の違う取り込みなど）なので 1 位を推薦する（#1865。上位の文書の集合が少しでも違えば
割れとした以前の規則は、同じ資料の写しの間で割れが多かった）。

値（`RANK_OFFSET`・`RANK_DEPTH`・`CLOSE_SCORE_RATIO`）は #1865 の評価の問（プロファイル名を書かない
34 問。開発の環境の 41 プロファイル）で決めた。
"""

from __future__ import annotations

import logging
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.clients.oci_genai import EmbeddingInputType
from app.schemas.search import RetrievedChunk, SearchMode, format_search_id_filter
from app.schemas.search_answer_profile import SearchAnswerProfileTarget

logger = logging.getLogger(__name__)

# 1 回の検索で読む chunk の数（同じ資料を複数の KB に入れた環境でも、別の資料まで届く数）。
RECOMMEND_RETRIEVAL_TOP_K = 80
# 順位の重み 1 / (順位 + RANK_OFFSET)。上位の当たりを重くする（#1865 で 5 → 2）。
RANK_OFFSET = 2
# 点に数える本文の順位の上限（#1865）。下位の弱い当たりの数で大きいプロファイルが勝たない
# ようにする。
RANK_DEPTH = 10
# 名前・説明の重なり（0〜1）の重み。1 位の当たり（1/3）より小さく、当たりの同点を分ける程度。
LEXICAL_WEIGHT = 0.05
# 1 位の点のこの割合以上の候補を「近い」とみなす（#1865 で 0.75 → 0.7。順位 1 と 2 の重みの比が
# ちょうど 0.75 になり、境目に乗らないようにする）。
CLOSE_SCORE_RATIO = 0.7
# 候補ごとに示す当たった文書名の数・近さの判定に使う文書の数。
CANDIDATE_DOCUMENTS = 3
DESCRIPTION_MAX_CHARS = 200
RECOMMEND_LIMIT_DEFAULT = 5
RECOMMEND_LIMIT_MAX = 8


class RecommendationOracle(Protocol):
    async def search_answer_profile_targets(
        self, *, search_answer_profile_ids: Sequence[str] | None = None
    ) -> list[SearchAnswerProfileTarget]: ...

    async def hybrid_search(
        self,
        query: str,
        embedding: list[float],
        top_k: int,
        mode: SearchMode = SearchMode.HYBRID,
        filters: dict[str, str] | None = None,
    ) -> list[RetrievedChunk]: ...

    async def knowledge_base_ids_by_retrieved_document(
        self, document_ids: Sequence[str], knowledge_base_ids: Sequence[str]
    ) -> dict[str, set[str]]: ...


class QueryEmbedder(Protocol):
    async def embed(
        self, texts: list[str], *, input_type: EmbeddingInputType = ...
    ) -> list[list[float]]: ...


@dataclass
class ProfileCandidate:
    """推薦の候補 1 件（点は 1 位を 1 とした相対の値にして返す）。"""

    id: str
    name: str
    description: str | None
    score: float
    documents: list[str] = field(default_factory=list)
    lexical_overlap: float = 0.0

    @property
    def reason(self) -> str:
        parts: list[str] = []
        if self.documents:
            parts.append("当たった資料: " + "、".join(self.documents))
        if self.lexical_overlap > 0:
            parts.append("名前・説明が質問と重なる")
        return "。".join(parts)


@dataclass
class ProfileRecommendation:
    recommended_profile_id: str | None
    candidates: list[ProfileCandidate]


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def _bigrams(text: str) -> set[str]:
    compact = "".join(ch for ch in _normalize(text) if not ch.isspace())
    return {compact[index : index + 2] for index in range(len(compact) - 1)}


def lexical_overlap(question: str, target: SearchAnswerProfileTarget) -> float:
    """質問の 2-gram のうち、名前・説明・参照先の KB の名前に含まれる割合（0〜1）。"""
    asked = _bigrams(question)
    if not asked:
        return 0.0
    text = " ".join(
        [target.name, target.description or "", *(kb.name for kb in target.knowledge_bases)]
    )
    return len(asked & _bigrams(text)) / len(asked)


def _document_name(chunk: RetrievedChunk) -> str:
    return chunk.file_name or str(chunk.metadata.get("title") or "") or chunk.document_id


def rank_profiles(
    question: str,
    targets: Sequence[SearchAnswerProfileTarget],
    hits: Sequence[RetrievedChunk],
    membership: dict[str, set[str]],
    *,
    limit: int = RECOMMEND_LIMIT_DEFAULT,
) -> ProfileRecommendation:
    """検索の当たり（`hits` は関連度の順）と KB の所属から、プロファイルの推薦を決める。"""
    scores: dict[str, float] = {target.id: 0.0 for target in targets}
    documents: dict[str, list[str]] = {target.id: [] for target in targets}
    profiles_of_kb: dict[str, list[str]] = {}
    for target in targets:
        for kb in target.knowledge_bases:
            profiles_of_kb.setdefault(kb.id, []).append(target.id)
    # 同じ資料の同じ本文（複数の KB に入れた写し）は同じ順位に数える。
    rank_of_content: dict[tuple[str, str], int] = {}
    for chunk in hits:
        key = (_document_name(chunk), chunk.text)
        if key not in rank_of_content:
            rank_of_content[key] = len(rank_of_content) + 1
        if rank_of_content[key] > RANK_DEPTH:
            continue
        weight = 1.0 / (rank_of_content[key] + RANK_OFFSET)
        reached: set[str] = set()
        for kb_id in membership.get(chunk.document_id, set()):
            reached.update(profiles_of_kb.get(kb_id, []))
        name = _document_name(chunk)
        for profile_id in reached:
            scores[profile_id] += weight
            if name not in documents[profile_id]:
                documents[profile_id].append(name)
    candidates: list[ProfileCandidate] = []
    for target in targets:
        overlap = lexical_overlap(question, target)
        score = scores[target.id] + LEXICAL_WEIGHT * overlap
        if score <= 0:
            continue
        description = (target.description or "").strip() or None
        if description is not None and len(description) > DESCRIPTION_MAX_CHARS:
            description = description[:DESCRIPTION_MAX_CHARS] + "…"
        candidates.append(
            ProfileCandidate(
                id=target.id,
                name=target.name,
                description=description,
                score=score,
                documents=documents[target.id],
                lexical_overlap=overlap,
            )
        )
    candidates.sort(key=lambda item: (-item.score, item.name, item.id))
    if not candidates:
        return ProfileRecommendation(recommended_profile_id=None, candidates=[])
    top = candidates[0]
    close = [item for item in candidates[1:] if item.score >= top.score * CLOSE_SCORE_RATIO]
    top_documents = set(top.documents[:CANDIDATE_DOCUMENTS])
    if top_documents:
        # 1 位と上位の文書が 1 つも重ならない近い候補があれば割れ（同じ資料の写しは割れにしない）。
        split = any(
            not (set(item.documents[:CANDIDATE_DOCUMENTS]) & top_documents) for item in close
        )
    else:
        # 当たりが無く名前・説明の重なりだけで並んだときは、近い候補があれば決めない。
        split = bool(close)
    best = top.score
    shown = candidates[: max(1, min(limit, RECOMMEND_LIMIT_MAX))]
    for item in shown:
        item.score = round(item.score / best, 3)
        item.documents = item.documents[:CANDIDATE_DOCUMENTS]
    return ProfileRecommendation(
        recommended_profile_id=None if split else top.id,
        candidates=shown,
    )


async def recommend_search_answer_profiles(
    question: str,
    *,
    oracle: RecommendationOracle,
    embedder: QueryEmbedder,
    profile_ids: Sequence[str] | None = None,
    limit: int = RECOMMEND_LIMIT_DEFAULT,
) -> ProfileRecommendation:
    """利用者が使える検索・回答プロファイル（`profile_ids` を渡すとその中）から推薦する。

    検索は呼び出しの監査 context（利用者の権限・業務 Agent のデータの範囲）のまま行うので、利用者が
    見えない文書・KB は当たらない。
    """
    targets = await oracle.search_answer_profile_targets(
        search_answer_profile_ids=list(profile_ids) if profile_ids is not None else None
    )
    if not targets:
        return ProfileRecommendation(recommended_profile_id=None, candidates=[])
    knowledge_base_ids = list(
        dict.fromkeys(kb.id for target in targets for kb in target.knowledge_bases)
    )
    hits: list[RetrievedChunk] = []
    membership: dict[str, set[str]] = {}
    if knowledge_base_ids:
        filters = {"knowledge_base_id": format_search_id_filter(knowledge_base_ids)}
        embedding: list[float] = []
        mode = SearchMode.HYBRID
        try:
            [embedding] = await embedder.embed([question], input_type="SEARCH_QUERY")
        except Exception:  # noqa: BLE001 - 埋め込みが使えなければ全文検索だけで推薦する
            logger.warning("profile recommendation falls back to keyword search", exc_info=True)
            mode = SearchMode.KEYWORD
        hits = await oracle.hybrid_search(
            question, embedding, RECOMMEND_RETRIEVAL_TOP_K, mode, filters
        )
        membership = await oracle.knowledge_base_ids_by_retrieved_document(
            list(dict.fromkeys(chunk.document_id for chunk in hits)), knowledge_base_ids
        )
    return rank_profiles(question, targets, hits, membership, limit=limit)
