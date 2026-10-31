import { describe, expect, it } from "vitest";

import {
  canAddRecipe,
  canDeleteRecipe,
  recipeConfigLocked,
  recipeIsActive,
  recipeLayerRebuildSummary,
  recipeLayerStatuses,
  resolveSelectedRecipe,
} from "./DocumentRecipeManager.logic";
import type {
  DocumentChunkSet,
  DocumentProcessingConfig,
  DocumentRecipeView,
} from "@/lib/api";

const emptyConfig: DocumentProcessingConfig = {
  preprocess_profile: null,
  parser_adapter_backend: null,
  parser_docling_enabled: null,
  parser_unstructured_enabled: null,
  parser_mineru_enabled: null,
  parser_dots_ocr_enabled: null,
  vision_enabled: null,
  chunking_strategy: null,
  chunk_size: null,
  chunk_overlap: null,
  chunk_min_chars: null,
  graph_profile: null,
  field_extraction_enabled: null,
  navigation_summary_enabled: null,
  auto_parse_after_preprocess_enabled: null,
  auto_chunk_after_extract_enabled: null,
  auto_index_after_chunk_enabled: null,
};

function recipe(
  recipeId: string,
  slotNo: 1 | 2 | 3,
  stepStatus: DocumentRecipeView["steps"][number]["status"] = "PENDING",
  status: DocumentRecipeView["status"] = "UPLOADED"
): DocumentRecipeView {
  return {
    recipe_id: recipeId,
    document_id: "doc-1",
    slot_no: slotNo,
    status,
    failed_phase: null,
    processing_config: emptyConfig,
    effective_processing_config: emptyConfig,
    preprocess_artifact: null,
    active_extraction_recipe_id: null,
    active_chunk_set_id: null,
    chunk_count: 0,
    vector_count: 0,
    config_revision: 1,
    materialized_revision: null,
    searchable: false,
    needs_reprocessing: false,
    error_message: null,
    steps: [
      {
        phase: "PREPROCESS",
        status: stepStatus,
        started_at: null,
        finished_at: null,
        error_message: null,
      },
    ],
    created_at: "2026-06-30T00:00:00Z",
    updated_at: "2026-06-30T00:00:00Z",
    started_at: null,
    finished_at: null,
  };
}

describe("DocumentRecipeManager logic", () => {
  it("URL の recipe が有効なら選択を保持し、無効ならレシピ1へ戻す", () => {
    const recipes = [recipe("recipe-1", 1), recipe("recipe-2", 2)];
    expect(resolveSelectedRecipe(recipes, "recipe-2")?.recipe_id).toBe("recipe-2");
    expect(resolveSelectedRecipe(recipes, "missing")?.recipe_id).toBe("recipe-1");
  });

  it("最大3件では追加不可、1件だけなら削除不可", () => {
    expect(canAddRecipe(2)).toBe(true);
    expect(canAddRecipe(3)).toBe(false);
    expect(canDeleteRecipe(1, false)).toBe(false);
    expect(canDeleteRecipe(2, false)).toBe(true);
  });

  it("待機中または実行中のレシピは活動中として編集・削除を止める", () => {
    expect(recipeIsActive(recipe("queued", 1, "QUEUED"))).toBe(true);
    expect(recipeIsActive(recipe("running", 1, "RUNNING"))).toBe(true);
    expect(recipeIsActive(recipe("done", 1, "SUCCEEDED"))).toBe(false);
    expect(canDeleteRecipe(2, true)).toBe(false);
  });

  it("実行中ジョブが無くても確認待ち等の非編集ステータスは処理設定編集を止める", () => {
    expect(recipeConfigLocked(recipe("uploaded", 1, "SUCCEEDED", "UPLOADED"))).toBe(false);
    expect(recipeConfigLocked(recipe("indexed", 1, "SUCCEEDED", "INDEXED"))).toBe(false);
    expect(recipeConfigLocked(recipe("error", 1, "SUCCEEDED", "ERROR"))).toBe(false);
    expect(recipeConfigLocked(recipe("review", 1, "SUCCEEDED", "REVIEW"))).toBe(true);
    expect(recipeConfigLocked(recipe("running", 1, "RUNNING", "INGESTING"))).toBe(true);
  });
});

function chunkSet(
  chunkSetId: string,
  layers: Partial<DocumentChunkSet["layer_statuses"]>
): DocumentChunkSet {
  const notRequested = { layer_id: null, requested: false, status: "not_requested", reason: null };
  return {
    chunk_set_id: chunkSetId,
    extraction_recipe_id: null,
    extraction_status: "materialized",
    extraction_reason: null,
    status: "INDEXED",
    chunk_count: 1,
    vector_count: 1,
    is_serving: true,
    created_at: null,
    extraction_id: null,
    parser: null,
    preprocess: null,
    knowledge_base_ids: [],
    serving_knowledge_base_ids: [],
    layer_statuses: {
      metadata: notRequested,
      graph: notRequested,
      navigation: notRequested,
      ...layers,
    },
  } as DocumentChunkSet;
}

describe("recipeLayerStatuses", () => {
  it("active chunk_set の要求済み layer だけを表示順で返す", () => {
    const target = recipe("recipe-1", 1);
    target.active_chunk_set_id = "cs-1";
    const sets = [
      chunkSet("cs-other", {
        graph: { layer_id: "gl-x", requested: true, status: "materialized", reason: null },
      }),
      chunkSet("cs-1", {
        graph: { layer_id: "gl-1", requested: true, status: "planned_only", reason: "未実体化" },
        navigation: { layer_id: "nv-1", requested: true, status: "materialized", reason: null },
      }),
    ];
    expect(recipeLayerStatuses(target, sets)).toEqual([
      {
        layer: "graph",
        status: "planned_only",
        reason: "未実体化",
        rebuildRequired: false,
        rebuildInputs: [],
      },
      {
        layer: "navigation",
        status: "materialized",
        reason: null,
        rebuildRequired: false,
        rebuildInputs: [],
      },
    ]);
  });

  it("active chunk_set 不在・未取得・全 not_requested では空を返す", () => {
    const noActive = recipe("recipe-1", 1);
    expect(recipeLayerStatuses(noActive, [chunkSet("cs-1", {})])).toEqual([]);
    const withActive = recipe("recipe-2", 2);
    withActive.active_chunk_set_id = "cs-1";
    expect(recipeLayerStatuses(withActive, undefined)).toEqual([]);
    expect(recipeLayerStatuses(withActive, [chunkSet("cs-1", {})])).toEqual([]);
  });
});

describe("recipeLayerRebuildSummary", () => {
  it("作り直しが必要なレイヤーと変わった入力を重複なくまとめる", () => {
    const target = recipe("recipe-1", 1);
    target.active_chunk_set_id = "cs-1";
    const statuses = recipeLayerStatuses(target, [
      chunkSet("cs-1", {
        metadata: {
          layer_id: "md-1",
          requested: true,
          status: "materialized",
          reason: null,
          rebuild_required: true,
          rebuild_inputs: ["field_schema_hash", "chunk_metadata_contract"],
        },
        navigation: {
          layer_id: "nv-1",
          requested: true,
          status: "materialized",
          reason: null,
          rebuild_required: true,
          rebuild_inputs: ["field_schema_hash"],
        },
        graph: { layer_id: "gl-1", requested: true, status: "planned_only", reason: null },
      }),
    ]);

    expect(statuses.map((entry) => [entry.layer, entry.rebuildRequired])).toEqual([
      ["metadata", true],
      ["graph", false],
      ["navigation", true],
    ]);
    expect(recipeLayerRebuildSummary(statuses)).toEqual({
      layers: ["metadata", "navigation"],
      inputs: ["field_schema_hash", "chunk_metadata_contract"],
    });
  });

  it("指紋の無い古い API 応答（rebuild_required なし）は作り直し不要として扱う", () => {
    const target = recipe("recipe-1", 1);
    target.active_chunk_set_id = "cs-1";
    const statuses = recipeLayerStatuses(target, [
      chunkSet("cs-1", {
        metadata: { layer_id: "md-1", requested: true, status: "materialized", reason: null },
      }),
    ]);

    expect(statuses[0]?.rebuildRequired).toBe(false);
    expect(recipeLayerRebuildSummary(statuses)).toEqual({ layers: [], inputs: [] });
  });
});
