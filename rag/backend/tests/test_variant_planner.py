"""variant materialization プランナ(variant_planner)の決定論テスト。"""

from app.config import Settings, get_settings
from app.rag.kb_adapter_config import KnowledgeBaseAdapterConfig
from app.rag.variant_keys import (
    compute_chunk_set_id,
    compute_extraction_recipe_id,
    compute_graph_layer_id,
)
from app.rag.variant_planner import (
    _select_extractions_within_limit,
    diff_plan,
    plan_document_materializations,
    plan_materializations,
)

SRC = "c" * 64


def _settings(**overrides: object) -> Settings:
    return get_settings().model_copy(update=overrides)


def _config(**ingestion: object) -> KnowledgeBaseAdapterConfig:
    return KnowledgeBaseAdapterConfig.model_validate({"ingestion": ingestion})


def test_identical_settings_share_one_chunk_set_with_refcount() -> None:
    """同一設定の 2 KB は 1 chunk_set を共有し refcount=2。"""
    base = _settings()
    plan = plan_materializations(SRC, {"kb-1": base, "kb-2": base})

    assert len(plan.chunk_sets) == 1
    assert len(plan.extraction_recipes) == 1
    chunk_set_id = compute_chunk_set_id(SRC, base)
    extraction_recipe_id = compute_extraction_recipe_id(SRC, base)
    assert plan.chunk_sets[chunk_set_id] == frozenset({"kb-1", "kb-2"})
    assert plan.extraction_recipes[extraction_recipe_id] == frozenset({"kb-1", "kb-2"})
    assert plan.extraction_recipe_for_chunk_set(chunk_set_id) == extraction_recipe_id
    assert plan.refcount(chunk_set_id) == 2


def test_different_chunk_axis_splits_chunk_sets() -> None:
    """chunk_size が違う 2 KB は別 chunk_set、同じ extraction recipe を共有する。"""
    plan = plan_materializations(
        SRC,
        {
            "kb-1": _settings(rag_chunk_size=1000),
            "kb-2": _settings(rag_chunk_size=2000),
        },
    )
    assert len(plan.chunk_sets) == 2
    assert len(plan.extraction_recipes) == 1
    assert len(plan.chunk_sets_by_extraction_recipe()) == 1
    assert all(len(kb_ids) == 1 for kb_ids in plan.chunk_sets.values())


def test_different_parser_axis_splits_extraction_recipes() -> None:
    """parser が違う 2 KB は extraction recipe から分裂し、静かに抽出を再利用しない。"""
    plan = plan_materializations(
        SRC,
        {
            "kb-1": _settings(rag_parser_adapter_backend="local"),
            "kb-2": _settings(rag_parser_adapter_backend="docling"),
        },
    )

    assert len(plan.extraction_recipes) == 2
    assert len(plan.chunk_sets) == 2
    assert all(
        len(chunk_sets) == 1 for chunk_sets in plan.chunk_sets_by_extraction_recipe().values()
    )


def test_graph_axis_shares_chunk_set_splits_graph_layer() -> None:
    """graph だけ違う 2 KB は chunk_set 共有、graph 層は分裂。"""
    off = _settings(rag_graph_profile="off")
    entities = _settings(rag_graph_profile="entities")
    plan = plan_materializations(SRC, {"kb-1": off, "kb-2": entities})

    assert len(plan.chunk_sets) == 1
    chunk_set_id = compute_chunk_set_id(SRC, off)
    assert plan.refcount(chunk_set_id) == 2
    assert plan.layer_ids_for_chunk_set(chunk_set_id, "graph")
    assert len(plan.graph_layers) == 2
    assert plan.refcount(compute_graph_layer_id(chunk_set_id, off)) == 1
    assert plan.refcount(compute_graph_layer_id(chunk_set_id, entities)) == 1


def test_diff_to_create_for_new_materialization() -> None:
    """既存が空なら計画の全層が to_create、to_collect は空。"""
    plan = plan_materializations(SRC, {"kb-1": _settings()})
    diff = diff_plan(frozenset(), plan)

    assert diff.to_create == plan.all_ids()
    assert diff.to_collect == frozenset()


def test_diff_collects_unreferenced_layers() -> None:
    """参照が消えた既存層は to_collect、新規は to_create。"""
    plan = plan_materializations(SRC, {"kb-1": _settings()})
    desired = plan.all_ids()
    stale_id = "cs_deadbeefdeadbeef"
    existing = frozenset({stale_id, *list(desired)[:1]})

    diff = diff_plan(existing, plan)

    assert stale_id in diff.to_collect
    assert (set(desired) & existing).isdisjoint(diff.to_create)
    assert diff.to_create == frozenset(desired - existing)


def test_plan_from_kb_configs_shares_when_both_inherit() -> None:
    """取込上書きが無い 2 KB は同じ effective 設定 → 1 chunk_set を共有。"""
    settings = get_settings()
    plan = plan_document_materializations(
        SRC,
        settings,
        {"kb-1": KnowledgeBaseAdapterConfig(), "kb-2": KnowledgeBaseAdapterConfig()},
    )
    assert len(plan.chunk_sets) == 1
    assert next(iter(plan.chunk_sets.values())) == frozenset({"kb-1", "kb-2"})


def test_plan_from_kb_configs_ignores_kb_chunk_override() -> None:
    """3 層モデル: KB の chunk_size 上書きは無視され、両 KB は単一 chunk_set を共有する。

    レシピは文書プロパティ(global)で KB からは解決しない。KB 別取込上書きで chunk_set が
    分裂しないことを固定する(以前は分裂していた挙動の回帰防止)。
    """
    settings = get_settings()
    plan = plan_document_materializations(
        SRC,
        settings,
        {
            "kb-1": KnowledgeBaseAdapterConfig(),
            "kb-2": _config(chunk_size=settings.rag_chunk_size + 512),
        },
    )
    assert len(plan.chunk_sets) == 1
    assert len(plan.extraction_recipes) == 1
    assert next(iter(plan.chunk_sets.values())) == frozenset({"kb-1", "kb-2"})


def test_plan_from_kb_configs_ignores_kb_graph_override() -> None:
    """3 層モデル: graph_profile の KB 上書きも無視され、graph 層も分裂しない。"""
    settings = get_settings()
    plan = plan_document_materializations(
        SRC,
        settings,
        {"kb-1": KnowledgeBaseAdapterConfig(), "kb-2": _config(graph_profile="entities")},
    )
    assert len(plan.chunk_sets) == 1
    assert len(plan.graph_layers) == 1


def test_membership_count_does_not_change_single_chunk_set() -> None:
    """3 層モデルの核: KB の出し入れ(membership 数)が単一 chunk_set を変えない。

    1 KB でも 3 KB でも同じ chunk_set/extraction_recipe ID になる(参照 KB 群だけが変わる)。
    これにより KB の追加・削除が chunk 再処理 / GC を引き起こさない。
    """
    settings = get_settings()
    chunk_set_id = compute_chunk_set_id(SRC, settings)
    extraction_recipe_id = compute_extraction_recipe_id(SRC, settings)

    one = plan_document_materializations(SRC, settings, {"kb-1": KnowledgeBaseAdapterConfig()})
    three = plan_document_materializations(
        SRC,
        settings,
        {kb: KnowledgeBaseAdapterConfig() for kb in ("kb-1", "kb-2", "kb-3")},
    )

    assert set(one.chunk_sets) == {chunk_set_id} == set(three.chunk_sets)
    assert set(one.extraction_recipes) == {extraction_recipe_id} == set(three.extraction_recipes)
    # ID は不変。参照 KB 群(refcount)だけが membership に応じて増減する。
    assert one.chunk_sets[chunk_set_id] == frozenset({"kb-1"})
    assert three.chunk_sets[chunk_set_id] == frozenset({"kb-1", "kb-2", "kb-3"})
    # 既存 = 3 KB 状態から 1 KB へ減らしても、chunk_set は GC 対象にならない(計画に残る)。
    assert chunk_set_id not in diff_plan(three.all_ids(), one).to_collect


def test_shared_layer_not_collected_while_other_kb_references_it() -> None:
    """KB を 1 つ外しても、別 KB が同じ chunk_set を参照していれば GC されない。"""
    base = _settings()
    chunk_set_id = compute_chunk_set_id(SRC, base)

    plan_after = plan_materializations(SRC, {"kb-2": base})
    existing = plan_materializations(SRC, {"kb-1": base, "kb-2": base}).all_ids()

    diff = diff_plan(existing, plan_after)

    assert chunk_set_id not in diff.to_collect
    assert plan_after.refcount(chunk_set_id) == 1


def test_chunking_axis_shares_extraction_recipe_but_splits_chunk_set() -> None:
    """chunking だけ違う 2 KB は extraction recipe を共有し、chunk_set だけ分裂。"""
    plan = plan_materializations(
        SRC,
        {"kb-1": _settings(rag_chunk_size=1000), "kb-2": _settings(rag_chunk_size=2000)},
    )
    extraction_recipe_id = compute_extraction_recipe_id(SRC, _settings())

    assert len(plan.extraction_recipes) == 1
    assert plan.refcount(extraction_recipe_id) == 2
    assert len(plan.chunk_sets) == 2
    assert set(plan.chunk_set_recipes.values()) == {extraction_recipe_id}
    assert plan.chunk_sets_by_extraction_recipe()[extraction_recipe_id] == tuple(
        sorted(plan.chunk_sets)
    )


def test_parser_axis_splits_extraction_recipes() -> None:
    """parser が違う 2 KB は別 extraction recipe になる。"""
    plan = plan_materializations(
        SRC,
        {
            "kb-1": _settings(rag_parser_adapter_backend="docling"),
            "kb-2": _settings(rag_parser_adapter_backend="dots_ocr"),
        },
    )
    assert len(plan.extraction_recipes) == 2
    assert len(plan.chunk_sets) == 2
    assert len(set(plan.chunk_set_recipes.values())) == 2


def test_select_extractions_within_limit_priority_order() -> None:
    """打ち切りは owning → refcount 大 → id 昇順の決定論順。"""
    consumers = {"er_a": {"k1"}, "er_b": {"k2", "k3"}, "er_c": {"k4"}}
    kept, truncated = _select_extractions_within_limit(consumers, None, 3)
    assert kept == {"er_a", "er_b", "er_c"}
    assert truncated == set()

    kept, truncated = _select_extractions_within_limit(consumers, "er_a", 2)
    assert kept == {"er_a", "er_b"}
    assert truncated == {"er_c"}


def test_extraction_limit_truncates_owning_kept() -> None:
    """抽出が上限を超えると owning 優先で打ち切り、超過分が truncated。"""
    owning = _settings(rag_parser_adapter_backend="docling")
    owning_id = compute_extraction_recipe_id(SRC, owning)
    dots_id = compute_extraction_recipe_id(SRC, _settings(rag_parser_adapter_backend="dots_ocr"))
    consumers = {
        "kb-own": owning,
        "kb-dots-1": _settings(rag_parser_adapter_backend="dots_ocr"),
        "kb-dots-2": _settings(rag_parser_adapter_backend="dots_ocr"),
        "kb-unstr": _settings(rag_parser_adapter_backend="unstructured"),
    }
    plan = plan_materializations(
        SRC,
        consumers,
        owning_extraction_recipe_id=owning_id,
        max_extractions=2,
    )

    assert len(plan.extraction_recipes) == 2
    assert len(plan.truncated_extractions) == 1
    assert owning_id in plan.extraction_recipes
    assert dots_id in plan.extraction_recipes
    assert owning_id not in plan.truncated_extractions
    assert all(parent in plan.extraction_recipes for parent in plan.chunk_set_recipes.values())


def test_diff_creates_and_collects_extraction_recipe_tier() -> None:
    """diff は extraction recipe 層も対象にする。"""
    plan = plan_materializations(SRC, {"kb-1": _settings()})
    extraction_recipe_id = compute_extraction_recipe_id(SRC, _settings())

    assert extraction_recipe_id in diff_plan(frozenset(), plan).to_create
    stale_ex = "er_deadbeefdeadbeef"
    diff_gc = diff_plan(frozenset({stale_ex, extraction_recipe_id}), plan)
    assert stale_ex in diff_gc.to_collect
    assert extraction_recipe_id not in diff_gc.to_collect


def test_plan_from_kb_configs_collapses_to_single_recipe_regardless_of_overrides() -> None:
    """3 層モデル: KB 設定に何が入っていても文書は単一 extraction recipe・単一 chunk_set。"""
    settings = get_settings()
    plan = plan_document_materializations(
        SRC,
        settings,
        {
            "kb-1": KnowledgeBaseAdapterConfig(),
            "kb-2": _config(chunk_size=settings.rag_chunk_size + 512),
            "kb-3": _config(parser_adapter_backend="docling", graph_profile="entities"),
        },
    )
    assert len(plan.extraction_recipes) == 1
    assert len(plan.chunk_sets) == 1
    recipe_id = next(iter(plan.extraction_recipes))
    assert len(plan.chunk_sets_by_extraction_recipe()[recipe_id]) == 1
