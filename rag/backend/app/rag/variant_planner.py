"""variant materialization プランナ - dedup / refcount / GC の決定論ブレイン。

1 文書が複数 KB に所属し、各 KB が異なる取込設定(effective settings)を持つとき、
「重複は共有・差分は複製」をどう実体化するかを決定論で計算する。実際の永続化
(Oracle 表 / chunk / embedding)や VLM 実行は行わず、どの層をいくつ作り、どれを
参照カウント 0 で GC するかだけを返す。要 DDL・実 Oracle の永続層はこの計画に従う。

核心:
* 同一 extraction_recipe_id を参照する chunk_set だけが保存済み extraction を共有できる。
* 同一 chunk_set_id を参照する KB が複数あれば共有(refcount = 参照 KB 数)。
* chunk 軸が違えば別 chunk_set(差分複製)。下流軸だけ違えば上流層を共有し派生層だけ分裂。
* KB を外す/設定変更で参照が消えた層は refcount 0 として GC 対象になる(他 KB 使用中は残す)。
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass

from app.config import Settings
from app.rag.kb_adapter_config import KnowledgeBaseAdapterConfig
from app.rag.variant_keys import (
    MAX_EXTRACTIONS_PER_DOCUMENT,
    compute_extraction_recipe_id,
    compute_layer_ids,
)


@dataclass(frozen=True)
class MaterializationPlan:
    """文書 1 件の望ましい materialization 状態。

    各 dict は ``層 ID -> それを参照する kb_id 群``。refcount は値集合の要素数。
    ``chunk_set_recipes`` は chunk_set_id -> 親 extraction_recipe_id を保持する。
    ``truncated_extractions`` は上限超過で計画から外した recipe ID(その配下 KB は
    計画に含めない。呼び出し側が警告する)。
    """

    extraction_recipes: dict[str, frozenset[str]]
    chunk_sets: dict[str, frozenset[str]]
    chunk_set_recipes: dict[str, str]
    metadata_layers: dict[str, frozenset[str]]
    graph_layers: dict[str, frozenset[str]]
    nav_layers: dict[str, frozenset[str]]
    truncated_extractions: frozenset[str] = frozenset()

    def all_ids(self) -> frozenset[str]:
        """全層の ID 集合(存在すべき materialization)。"""
        ids: set[str] = set()
        for layer in (
            self.extraction_recipes,
            self.chunk_sets,
            self.metadata_layers,
            self.graph_layers,
            self.nav_layers,
        ):
            ids.update(layer)
        return frozenset(ids)

    def refcount(self, layer_id: str) -> int:
        """指定層 ID の参照 KB 数(存在しなければ 0)。"""
        for layer in (
            self.extraction_recipes,
            self.chunk_sets,
            self.metadata_layers,
            self.graph_layers,
            self.nav_layers,
        ):
            if layer_id in layer:
                return len(layer[layer_id])
        return 0

    def extraction_recipe_for_chunk_set(self, chunk_set_id: str) -> str | None:
        """chunk_set が依存する extraction_recipe_id を返す。"""
        return self.chunk_set_recipes.get(chunk_set_id)

    def chunk_sets_by_extraction_recipe(self) -> dict[str, tuple[str, ...]]:
        """保存済み extraction を共有できる chunk_set 群を recipe ごとに返す。"""
        grouped: dict[str, list[str]] = defaultdict(list)
        for chunk_set_id, recipe_id in self.chunk_set_recipes.items():
            grouped[recipe_id].append(chunk_set_id)
        return {recipe_id: tuple(sorted(ids)) for recipe_id, ids in sorted(grouped.items())}

    def layer_ids_for_chunk_set(self, chunk_set_id: str, layer: str) -> tuple[str, ...]:
        """chunk_set に関係する派生層 ID を返す。

        同じ chunk_set を複数 KB が共有しつつ派生層だけ別方針にする場合があるため、
        返り値は tuple。API 表示側は単一/複数を区別して状態を説明する。
        """
        kb_ids = self.chunk_sets.get(chunk_set_id, frozenset())
        layer_map = {
            "metadata": self.metadata_layers,
            "graph": self.graph_layers,
            "navigation": self.nav_layers,
        }.get(layer)
        if not kb_ids or layer_map is None:
            return ()
        return tuple(sorted(layer_id for layer_id, owners in layer_map.items() if owners & kb_ids))


@dataclass(frozen=True)
class MaterializationDiff:
    """既存状態から望ましい計画への差分。"""

    to_create: frozenset[str]
    to_collect: frozenset[str]


def plan_materializations(
    source_sha256: str,
    consumers: Mapping[str, Settings],
    *,
    owning_extraction_recipe_id: str | None = None,
    max_extractions: int = MAX_EXTRACTIONS_PER_DOCUMENT,
) -> MaterializationPlan:
    """各 KB の effective 取込設定から、共有を畳んだ materialization 計画を作る。

    ``consumers`` は ``kb_id -> effective ingestion settings``。同じ層 ID に解決される
    KB は同じ materialization を共有する(refcount = 参照 KB 数)。抽出(parser x preprocess)
    が ``max_extractions`` を超える場合は owning 優先で打ち切り、外した抽出配下の KB は計画
    から除外する(``truncated_extractions`` に記録)。
    """
    per_consumer = {
        kb_id: compute_layer_ids(source_sha256, settings) for kb_id, settings in consumers.items()
    }

    extraction_consumers: dict[str, set[str]] = defaultdict(set)
    for kb_id, ids in per_consumer.items():
        extraction_consumers[ids["extraction_recipe_id"]].add(kb_id)

    kept, truncated = _select_extractions_within_limit(
        extraction_consumers, owning_extraction_recipe_id, max_extractions
    )

    extraction_recipes: dict[str, set[str]] = defaultdict(set)
    chunk_sets: dict[str, set[str]] = defaultdict(set)
    chunk_set_recipes: dict[str, str] = {}
    metadata_layers: dict[str, set[str]] = defaultdict(set)
    graph_layers: dict[str, set[str]] = defaultdict(set)
    nav_layers: dict[str, set[str]] = defaultdict(set)

    for kb_id, ids in per_consumer.items():
        extraction_recipe_id = ids["extraction_recipe_id"]
        if extraction_recipe_id not in kept:
            continue
        extraction_recipes[extraction_recipe_id].add(kb_id)
        chunk_sets[ids["chunk_set_id"]].add(kb_id)
        chunk_set_recipes[ids["chunk_set_id"]] = extraction_recipe_id
        metadata_layers[ids["metadata_layer_id"]].add(kb_id)
        graph_layers[ids["graph_layer_id"]].add(kb_id)
        nav_layers[ids["nav_layer_id"]].add(kb_id)

    return MaterializationPlan(
        extraction_recipes=_freeze(extraction_recipes),
        chunk_sets=_freeze(chunk_sets),
        chunk_set_recipes=dict(sorted(chunk_set_recipes.items())),
        metadata_layers=_freeze(metadata_layers),
        graph_layers=_freeze(graph_layers),
        nav_layers=_freeze(nav_layers),
        truncated_extractions=frozenset(truncated),
    )


def _select_extractions_within_limit(
    extraction_consumers: Mapping[str, set[str]],
    owning_extraction_id: str | None,
    max_extractions: int,
) -> tuple[set[str], set[str]]:
    """抽出が上限を超える場合の保持/打ち切りを決める(決定論・再現可能)。

    優先順: owning 抽出 -> 参照 KB 数の多い順 -> extraction_recipe_id 昇順。上位
    ``max_extractions`` を保持し、残りを打ち切る。上限以内なら全保持。
    """
    if len(extraction_consumers) <= max_extractions:
        return set(extraction_consumers), set()
    ordered = sorted(
        extraction_consumers,
        key=lambda ex_id: (
            ex_id != owning_extraction_id,
            -len(extraction_consumers[ex_id]),
            ex_id,
        ),
    )
    return set(ordered[:max_extractions]), set(ordered[max_extractions:])


def plan_document_materializations(
    source_sha256: str,
    global_settings: Settings,
    kb_configs: Mapping[str, KnowledgeBaseAdapterConfig],
) -> MaterializationPlan:
    """文書の所属 KB 群から materialization 計画を作る(3 層モデル)。

    3 層モデルでは KB はコレクション(検索スコープ)で、レシピ(取込モード)は文書のプロパティ。
    KB 別の取込上書きは使わず、所属 KB は全て文書の単一レシピ(= global 既定)を共有する。
    → 常に extraction recipe 1 本・chunk_set 1 つ。membership が変わっても計画は変わらない
    (= KB の出し入れが chunk 再処理 / GC / 409 を引き起こさない)。
    将来の「文書単位レシピ選択」は global_settings に文書の選択値を重ねて渡すだけで実現する。
    """
    # KB を keys として使い、値は全て文書の単一レシピ(global)に揃える。
    consumers: dict[str, Settings] = {kb_id: global_settings for kb_id in kb_configs}
    owning_extraction_recipe_id = compute_extraction_recipe_id(source_sha256, global_settings)
    return plan_materializations(
        source_sha256,
        consumers,
        owning_extraction_recipe_id=owning_extraction_recipe_id,
    )


def diff_plan(existing_ids: frozenset[str], plan: MaterializationPlan) -> MaterializationDiff:
    """既存の materialization ID 集合と望ましい計画から、作成すべき/GC すべき ID を出す。

    * ``to_create``: 計画にあって既存にない層(新規 materialize)。
    * ``to_collect``: 既存にあって計画にない層(参照消失 -> refcount 0 で GC)。
    """
    desired = plan.all_ids()
    return MaterializationDiff(
        to_create=frozenset(desired - existing_ids),
        to_collect=frozenset(existing_ids - desired),
    )


def _freeze(layer: dict[str, set[str]]) -> dict[str, frozenset[str]]:
    return {layer_id: frozenset(kb_ids) for layer_id, kb_ids in layer.items()}
