import type {
  DocumentChunkSet,
  DocumentLayerStatusName,
  DocumentRecipeView,
} from "@/lib/api";

export function resolveSelectedRecipe(
  recipes: DocumentRecipeView[],
  requestedRecipeId: string | null
) {
  return recipes.find((recipe) => recipe.recipe_id === requestedRecipeId) ?? recipes[0] ?? null;
}

export function recipeIsActive(recipe: DocumentRecipeView) {
  return recipe.steps.some((step) => step.status === "QUEUED" || step.status === "RUNNING");
}

const EDITABLE_STATUSES: DocumentRecipeView["status"][] = ["UPLOADED", "INDEXED", "ERROR"];

/** 処理中・確認待ちのレシピは処理設定編集を禁止する(実行中ジョブに限らない)。 */
export function recipeConfigLocked(recipe: DocumentRecipeView) {
  return recipeIsActive(recipe) || !EDITABLE_STATUSES.includes(recipe.status);
}

export function canAddRecipe(recipeCount: number) {
  return recipeCount < 3;
}

export function canDeleteRecipe(recipeCount: number, active: boolean) {
  return recipeCount > 1 && !active;
}

const LAYER_ORDER = ["metadata", "graph", "navigation"] as const;

export type RecipeLayerName = (typeof LAYER_ORDER)[number];

export interface RecipeLayerStatusView {
  layer: RecipeLayerName;
  status: DocumentLayerStatusName;
  reason: string | null;
  /** 作成後に入力（項目の定義など）が変わり、作り直しが必要か（#550）。 */
  rebuildRequired: boolean;
  rebuildInputs: string[];
}

/** 選択レシピの active chunk_set から派生 layer(項目抽出/関係情報/ナビ)の状態を引く。 */
export function recipeLayerStatuses(
  recipe: DocumentRecipeView,
  chunkSets: DocumentChunkSet[] | undefined
): RecipeLayerStatusView[] {
  if (!recipe.active_chunk_set_id || !chunkSets?.length) return [];
  const chunkSet = chunkSets.find((set) => set.chunk_set_id === recipe.active_chunk_set_id);
  if (!chunkSet?.layer_statuses) return [];
  return LAYER_ORDER.flatMap((layer) => {
    const status = chunkSet.layer_statuses[layer];
    if (!status?.requested || status.status === "not_requested") return [];
    return [
      {
        layer,
        status: status.status,
        reason: status.reason,
        rebuildRequired: Boolean(status.rebuild_required),
        rebuildInputs: status.rebuild_inputs ?? [],
      },
    ];
  });
}

/** 作り直しが必要なレイヤーと、変わった入力（重複なし・出現順）をまとめる。 */
export function recipeLayerRebuildSummary(statuses: RecipeLayerStatusView[]) {
  const layers = statuses.filter((entry) => entry.rebuildRequired);
  const inputs = [...new Set(layers.flatMap((entry) => entry.rebuildInputs))];
  return { layers: layers.map((entry) => entry.layer), inputs };
}

