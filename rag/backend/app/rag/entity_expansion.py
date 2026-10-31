"""検索のときの実体の 1 段の拡張（#1362）。

検索の上位の chunk と質問に出てくる実体から、Oracle の実体の表（``rag_entities`` /
``rag_entity_aliases`` / ``rag_entity_chunks``。``app.rag.entity_index`` が文書レシピの任意の処理
で作る）との SQL の join で、関連する chunk を 1 段だけ足す。LLM は呼ばない。

段の数え方（実体のつながりを 1 辺だけたどる）:

1. **起点の実体**: 質問に別名が含まれる実体（2 文字以上の別名だけ。1 文字の略号は質問の任意の 1 文
   字に一致させない）と、検索の上位の chunk（``ENTITY_SEED_ANCHORS`` 件）に関連づけた実体。
2. **名寄せ**（段に数えない）: 起点の実体のすべての別名（NFKC でそろえた形）を「定義する行」に持つ
   chunk。質問の「予算管理システム」→ 台帳の行（略称 BMS・ID SYS-108 も同じ実体）、本文の「ＨＲＭ」
   →台帳の「HRM」の行。
3. **1 段**: 2 の chunk の属性の実体（台帳の行の「担当部署: 経」）の別名を「定義する行」に持つ
   chunk（組織規程の「略号「経」: 経理部」と「経理部の承認者は…」）。3 で足した chunk の実体はたど
   らない（2 段以上の再帰はしない）。

足す chunk は検索と同じ見え方の条件（tenant・ナレッジベースの範囲・利用者の権限・有効な chunk_set・
旧版の扱い ``include_superseded``）の中だけで、1 回の検索で ``rag_entity_expansion_max_chunks`` 件
まで。名寄せの途中の chunk も同じ条件で選ぶ（範囲の外の行を橋渡しに使わない）。

同じ別名が会社・資料をまたいで複数の実体に当たるとき（サンプル社とサンプル物流社の「経」「OMS」）
は、起点と同じ会社（``scope_label``）の実体を優先する。質問に会社の名前があればその会社、無ければ
検索の上位の chunk の会社を優先し、それでも決められないときは両方を足して ``ambiguous`` の印を付け
る（勝手に 1 つに決めない）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from app.rag.entity_index import entity_key
from app.schemas.search import RetrievedChunk

# 起点にする検索の上位の chunk の数（RRF の上位から）。
ENTITY_SEED_ANCHORS = 5
# 1 回の SQL で読む候補の数の上限（足す件数の上限の倍。順位は Python で決める）。
ENTITY_CANDIDATE_FACTOR = 8
# 質問の別名として照合する最短の文字数（1 文字の略号は質問の任意の 1 文字に一致させない）。
MIN_QUESTION_ALIAS_CHARS = 2
# 足した chunk の metadata の key（MCP の根拠の役割・回答の診断・rag_engine の枠の確保に使う）。
ENTITY_EXPANSION_KEY = "entity_expansion"
ENTITY_EXPANSION_ROLE = "entity_expansion"
# 起点の種類（質問の実体 / 上位の chunk の実体）。
SEED_QUESTION = "question"
SEED_CHUNK = "chunk"


@dataclass(frozen=True)
class EntitySeedRow:
    """起点の実体の 1 つの別名（``OracleClient.entity_seed_aliases`` の 1 行）。"""

    entity_id: str
    alias_key: str
    scope_label: str | None
    # この別名が質問に含まれる（2 文字以上）。
    in_question: bool
    # この実体を関連づけた起点の chunk の順位（0 始まり。無ければ None）。
    seed_rank: int | None


@dataclass(frozen=True)
class EntityDefinitionRow:
    """別名を「定義する行」に持つ chunk（``entity_definition_chunks`` の 1 行）。"""

    chunk: RetrievedChunk
    match_key: str
    entity_id: str
    display_name: str
    scope_label: str | None
    # 1 段の拡張のときの元の chunk（名寄せの chunk）と、その属性の実体の会社。
    via_chunk_id: str | None = None
    via_scope_label: str | None = None


class EntityExpansionStore(Protocol):
    """実体の表を読む SQL（``OracleClient`` が実装する。テストは同じ規則の表を使う）。"""

    async def entity_seed_aliases(
        self,
        filters: dict[str, str],
        *,
        seed_chunk_ids: Sequence[str],
        question_key: str,
        limit: int,
    ) -> list[EntitySeedRow]: ...

    async def entity_definition_chunks(
        self, filters: dict[str, str], *, alias_keys: Sequence[str], limit: int
    ) -> list[EntityDefinitionRow]: ...

    async def entity_attribute_definition_chunks(
        self, filters: dict[str, str], *, source_chunk_ids: Sequence[str], limit: int
    ) -> list[EntityDefinitionRow]: ...


@dataclass(frozen=True)
class EntityExpansion:
    """足す chunk と、足した理由（診断・MCP・回答の文脈に出す）。"""

    chunk: RetrievedChunk
    info: dict[str, object]


@dataclass
class _SeedEntity:
    entity_id: str
    scope_label: str | None
    alias_keys: list[str] = field(default_factory=list)
    question_keys: list[str] = field(default_factory=list)
    seed_rank: int | None = None


async def plan_entity_expansion(
    store: EntityExpansionStore,
    filters: dict[str, str],
    *,
    question: str,
    seed_chunks: Sequence[RetrievedChunk],
    exclude_chunk_ids: set[str],
    max_chunks: int,
) -> list[EntityExpansion]:
    """質問と検索の上位の chunk から、実体の 1 段の拡張で足す chunk を順に返す。

    順は起点の実体ごとに「名寄せ → その 1 段」で、起点の実体は (1) 質問の長い別名 → (2) 質問の短い
    別名 → (3) 上位の chunk（順位の順。同じ chunk の中は質問と語の重なる行の実体を先に）の順。1 段
    で足す実体は質問と本文の語の重なり（文字の 2-gram）の多い順にし、同じ実体の chunk はまとめて足
    す。``exclude_chunk_ids``（起点の chunk など、すでに候補の上位にある chunk）は足さない。
    """
    if max_chunks <= 0:
        return []
    question_key = entity_key(question)
    seed_ids = [chunk.chunk_id for chunk in seed_chunks[:ENTITY_SEED_ANCHORS]]
    if not question_key and not seed_ids:
        return []
    candidate_limit = max_chunks * ENTITY_CANDIDATE_FACTOR
    seed_rows = await store.entity_seed_aliases(
        filters,
        seed_chunk_ids=seed_ids,
        question_key=question_key,
        limit=candidate_limit * 4,
    )
    seeds = _seed_entities(seed_rows, question_key)
    if not seeds:
        return []
    context_scopes = {seed.scope_label for seed in seeds if seed.seed_rank is not None}
    question_seeds = sorted(
        (seed for seed in seeds if seed.question_keys),
        key=lambda seed: (-max(len(key) for key in seed.question_keys), seed.entity_id),
    )
    line_scores = _seed_line_scores(seed_chunks[:ENTITY_SEED_ANCHORS], question_key)
    chunk_seeds = sorted(
        (seed for seed in seeds if not seed.question_keys and seed.seed_rank is not None),
        key=lambda seed: (
            seed.seed_rank,
            *_best_line_score(line_scores.get(seed.seed_rank or 0, []), seed.alias_keys),
            seed.entity_id,
        ),
    )
    keys = list(
        dict.fromkeys(key for seed in [*question_seeds, *chunk_seeds] for key in seed.alias_keys)
    )
    resolved_rows = await store.entity_definition_chunks(
        filters, alias_keys=keys, limit=candidate_limit
    )
    question_scopes = _question_scopes(question_key, [*resolved_rows, *seed_rows])
    question_seeds, ambiguous_seeds = _filter_question_seeds(
        question_seeds, preferred=question_scopes or context_scopes
    )
    planner = _Planner(question_key=question_key, exclude=set(exclude_chunk_ids))
    # 起点の実体ごとの組（名寄せの chunk と、その chunk から 1 段でたどる元）。並びは、質問の長い
    # 別名（3 文字以上・英数字の名前）→ 質問の短い別名（2 文字の語。「受付」など質問の別の語にも含
    # まれやすい）→ 上位の chunk の順位の順（その chunk 自身の属性の実体を先に、次にその chunk の
    # 実体）。
    groups: list[tuple[str, list[EntityExpansion], list[str]]] = []
    for tier_seeds in (
        [seed for seed in question_seeds if _strong_question_seed(seed)],
        [seed for seed in question_seeds if not _strong_question_seed(seed)],
    ):
        # 質問が名指しした実体は、全部の名寄せを先に足してから 1 段をたどる（比べる質問の 2 つ目の
        # 実体を 1 つ目の 1 段で押し出さない）。
        tier_items = [
            item
            for items in planner.resolve(
                resolved_rows, tier_seeds, ambiguous_seeds=ambiguous_seeds, seed_kind=SEED_QUESTION
            )
            for item in items
        ]
        groups.append((SEED_QUESTION, tier_items, [item.chunk.chunk_id for item in tier_items]))
    resolved_chunk_seeds = planner.resolve(
        resolved_rows, chunk_seeds, ambiguous_seeds=set(), seed_kind=SEED_CHUNK
    )
    for rank, seed_id in enumerate(seed_ids):
        # 上位の chunk の属性の実体（台帳の行の「担当部署: 経」）を 1 段でたどる。
        groups.append((SEED_CHUNK, [], [seed_id]))
        for seed, items in zip(chunk_seeds, resolved_chunk_seeds, strict=True):
            if seed.seed_rank == rank:
                groups.append((SEED_CHUNK, items, [item.chunk.chunk_id for item in items]))
    sources = list(dict.fromkeys(chunk_id for _, _, ids in groups for chunk_id in ids))
    hop_rows = (
        await store.entity_attribute_definition_chunks(
            filters, source_chunk_ids=sources, limit=candidate_limit
        )
        if sources
        else []
    )
    ordered: list[EntityExpansion] = []
    claimed: set[str] = set()
    for kind, items, ids in groups:
        wanted = {chunk_id for chunk_id in ids if chunk_id not in claimed}
        claimed.update(wanted)
        ordered.extend(items)
        ordered.extend(planner.hop([row for row in hop_rows if row.via_chunk_id in wanted], kind))
    return planner.take(ordered, max_chunks)


def _seed_line_scores(
    seed_chunks: Sequence[RetrievedChunk], question_key: str
) -> dict[int, list[tuple[str, float, int]]]:
    """起点の chunk ごとの行（正規化した本文・質問との語の重なり・位置）。

    1 つの起点の chunk に多くの実体があるとき（保守計画の章に 8 つのシステム）、質問と語の重なる行
    の実体を先にし、同じなら本文の順にする（質問の「第 2 土曜日」の行の「ＨＲＭ」を先にする）。
    """
    return {
        rank: [
            (entity_key(line), _overlap(question_key, line), position)
            for position, line in enumerate(chunk.text.splitlines())
            if line.strip()
        ]
        for rank, chunk in enumerate(seed_chunks)
    }


def _best_line_score(
    lines: Sequence[tuple[str, float, int]], alias_keys: Sequence[str]
) -> tuple[float, int]:
    """実体の別名を含む行のうち、質問との重なりが最も多い行の (-重なり, 位置)。無ければ末尾。"""
    best: tuple[float, int] | None = None
    for line_key, overlap, position in lines:
        if any(key and key in line_key for key in alias_keys):
            score = (-overlap, position)
            if best is None or score < best:
                best = score
    return best if best is not None else (0.0, 1_000_000)


def _strong_question_seed(seed: _SeedEntity) -> bool:
    """質問に 3 文字以上の別名か、2 文字以上の英数字の別名（「HRM」「GL」）で出てくる実体か。"""
    return any(len(key) >= 3 or key.isascii() for key in seed.question_keys)


def _seed_entities(rows: Sequence[EntitySeedRow], question_key: str) -> list[_SeedEntity]:
    """起点の実体（別名をまとめる）。質問の別名は、より長い別名の一部だけのものを外す。"""
    by_entity: dict[str, _SeedEntity] = {}
    matched: set[str] = set()
    for row in rows:
        seed = by_entity.setdefault(
            row.entity_id, _SeedEntity(entity_id=row.entity_id, scope_label=row.scope_label)
        )
        if row.alias_key not in seed.alias_keys:
            seed.alias_keys.append(row.alias_key)
        if (
            row.in_question
            and len(row.alias_key) >= MIN_QUESTION_ALIAS_CHARS
            and row.alias_key in question_key
        ):
            matched.add(row.alias_key)
            if row.alias_key not in seed.question_keys:
                seed.question_keys.append(row.alias_key)
        if row.seed_rank is not None and (seed.seed_rank is None or row.seed_rank < seed.seed_rank):
            seed.seed_rank = row.seed_rank
    # 「人事評価分析システム」に含まれる「評価分析システム」のような、より長い一致の一部は外す。
    shadowed = {key for key in matched for other in matched if key != other and key in other}
    for seed in by_entity.values():
        seed.question_keys = [key for key in seed.question_keys if key not in shadowed]
    return list(by_entity.values())


def _question_scopes(
    question_key: str, rows: Sequence[EntityDefinitionRow | EntitySeedRow]
) -> set[str | None]:
    """質問に名前が出てくる会社（``scope_label``）。"""
    labels = {row.scope_label for row in rows if row.scope_label}
    return {label for label in labels if label and entity_key(label) in question_key}


class _Planner:
    def __init__(self, *, question_key: str, exclude: set[str]) -> None:
        self._question_key = question_key
        self._exclude = exclude
        self._used: set[str] = set()

    def resolve(
        self,
        rows: Sequence[EntityDefinitionRow],
        seeds: Sequence[_SeedEntity],
        *,
        ambiguous_seeds: set[str],
        seed_kind: str,
    ) -> list[list[EntityExpansion]]:
        """起点の実体ごとに、別名を定義する chunk（名寄せ）。起点と同じ会社の候補を選ぶ。"""
        by_key: dict[str, list[EntityDefinitionRow]] = {}
        for row in rows:
            by_key.setdefault(row.match_key, []).append(row)
        result: list[list[EntityExpansion]] = []
        for seed in seeds:
            candidates: dict[str, EntityDefinitionRow] = {}
            for key in seed.alias_keys:
                for row in by_key.get(key, ()):
                    candidates.setdefault(row.chunk.chunk_id, row)
            chosen, ambiguous = _choose_scope(list(candidates.values()), scope=seed.scope_label)
            result.append(
                [
                    self._expansion(
                        row,
                        seed_kind=seed_kind,
                        hop=0,
                        ambiguous=ambiguous or seed.entity_id in ambiguous_seeds,
                        matched=(seed.question_keys or [row.match_key])[0],
                    )
                    for row in chosen
                ]
            )
        return result

    def hop(self, rows: Sequence[EntityDefinitionRow], seed_kind: str) -> list[EntityExpansion]:
        """名寄せの chunk の属性の実体を定義する chunk（1 段）。同じ実体の chunk はまとめる。"""
        by_source: dict[tuple[str, str], list[EntityDefinitionRow]] = {}
        for row in rows:
            by_source.setdefault((row.via_chunk_id or "", row.match_key), []).append(row)
        entities: dict[str, list[EntityExpansion]] = {}
        for (_via, _key), group in by_source.items():
            chosen, ambiguous = _choose_scope(group, scope=group[0].via_scope_label)
            for row in chosen:
                entities.setdefault(row.entity_id, []).append(
                    self._expansion(row, seed_kind=seed_kind, hop=1, ambiguous=ambiguous)
                )
        ranked = sorted(
            entities.values(),
            key=lambda items: (
                -max(_overlap(self._question_key, item.chunk.text) for item in items),
                str(items[0].info.get("entity_id") or ""),
            ),
        )
        return [item for items in ranked for item in items]

    def take(self, ordered: Sequence[EntityExpansion], limit: int) -> list[EntityExpansion]:
        result: list[EntityExpansion] = []
        for item in ordered:
            chunk_id = item.chunk.chunk_id
            if chunk_id in self._exclude or chunk_id in self._used:
                continue
            self._used.add(chunk_id)
            result.append(item)
            if len(result) >= limit:
                break
        return result

    def _expansion(
        self,
        row: EntityDefinitionRow,
        *,
        seed_kind: str,
        hop: int,
        ambiguous: bool,
        matched: str | None = None,
    ) -> EntityExpansion:
        info: dict[str, object] = {
            "entity_id": row.entity_id,
            "entity": row.display_name,
            "match": matched or row.match_key,
            "hop": hop,
            "seed": seed_kind,
            "scope": row.scope_label,
            "ambiguous": ambiguous,
        }
        if row.via_chunk_id:
            info["from_chunk_id"] = row.via_chunk_id
        return EntityExpansion(chunk=row.chunk, info=info)


def _filter_question_seeds(
    seeds: Sequence[_SeedEntity], *, preferred: set[str | None]
) -> tuple[list[_SeedEntity], set[str]]:
    """質問の同じ別名が会社をまたぐ複数の実体に当たるとき（「OMS」「経」）の起点を選ぶ。

    質問に名前が出てくる会社、無ければ検索の上位の chunk の会社（``preferred``）の実体を残す。どち
    らとも決められなければ全部を残し、決められなかった実体の ID を返す（足した chunk に
    ``ambiguous`` を付ける）。
    """
    by_key: dict[str, list[_SeedEntity]] = {}
    for seed in seeds:
        for key in seed.question_keys:
            by_key.setdefault(key, []).append(seed)
    dropped: set[str] = set()
    ambiguous: set[str] = set()
    for group in by_key.values():
        scopes = {seed.scope_label for seed in group}
        if len(scopes) <= 1:
            continue
        kept = [seed for seed in group if seed.scope_label in preferred]
        if kept and len({seed.scope_label for seed in kept}) == 1:
            dropped.update(seed.entity_id for seed in group if seed not in kept)
        else:
            ambiguous.update(seed.entity_id for seed in group)
    return [seed for seed in seeds if seed.entity_id not in dropped], ambiguous - dropped


def _choose_scope(
    rows: Sequence[EntityDefinitionRow], *, scope: str | None
) -> tuple[list[EntityDefinitionRow], bool]:
    """会社（``scope_label``）をまたぐ候補から、起点（``scope``）と同じ会社の候補を選ぶ。

    戻り値の 2 つ目は決められなかった印。起点の会社が分かれば、同じ会社と会社の名前の無い資料の候
    補だけを返す（別の会社の台帳・組織規程を橋渡しに使わない）。起点の会社が分からなければ全部を返
    し、候補が 2 つ以上の会社にまたがれば印を付ける（勝手に 1 つに決めない）。
    """
    if scope is None:
        return list(rows), len({row.scope_label for row in rows} - {None}) > 1
    return [row for row in rows if row.scope_label in {scope, None}], False


def _bigrams(text: str) -> set[str]:
    return {text[index : index + 2] for index in range(len(text) - 1)}


def _overlap(question_key: str, text: str) -> float:
    """質問と本文の文字の 2-gram の重なり（質問の 2-gram のうち本文にある割合）。"""
    wanted = _bigrams(question_key)
    if not wanted:
        return 0.0
    return len(wanted & _bigrams(entity_key(text))) / len(wanted)


def expansion_metadata(chunk: RetrievedChunk, info: Mapping[str, object]) -> RetrievedChunk:
    """足した chunk の metadata に、足した理由（``entity_expansion``）を付ける。"""
    return chunk.model_copy(
        update={
            "metadata": {
                **chunk.metadata,
                ENTITY_EXPANSION_KEY: {
                    key: value
                    for key, value in info.items()
                    if isinstance(value, str | int | float | bool) or value is None
                },
            }
        }
    )


__all__ = [
    "ENTITY_EXPANSION_KEY",
    "ENTITY_EXPANSION_ROLE",
    "ENTITY_SEED_ANCHORS",
    "EntityDefinitionRow",
    "EntityExpansion",
    "EntityExpansionStore",
    "EntitySeedRow",
    "expansion_metadata",
    "plan_entity_expansion",
]
