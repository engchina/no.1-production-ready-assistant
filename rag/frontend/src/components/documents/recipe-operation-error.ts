import { useState } from "react";
import type { DocumentRecipeView } from "@/lib/api";

type RecipeOperationState = Pick<
  DocumentRecipeView,
  "recipe_id" | "config_revision" | "status" | "updated_at"
>;

interface CapturedOperationError {
  error: unknown;
  recipeKey: string | null;
}

/** 処理の投入・承認の失敗を出し続けてよいかを決める、レシピの状態の組（#2639）。
 *  設定の保存（config_revision）・処理の進行（status・updated_at）で変わる。 */
export function recipeOperationKey(recipe: RecipeOperationState | null | undefined) {
  if (!recipe) return null;
  return [recipe.recipe_id, recipe.config_revision, recipe.status, recipe.updated_at].join("|");
}

/** 失敗を覚えた状態と、いま出す失敗を決める。覚えた状態が変わらないときは同じ object を返す。
 *  失敗を初めて見たときに、そのときのレシピの状態を覚える。 */
export function resolveRecipeOperationError(
  captured: CapturedOperationError | null,
  error: unknown,
  recipeKey: string | null
): { captured: CapturedOperationError | null; visible: unknown } {
  if (error == null) return { captured: null, visible: null };
  if (captured?.error !== error) return { captured: { error, recipeKey }, visible: error };
  return { captured, visible: captured.recipeKey === recipeKey ? error : null };
}

/** 処理の投入・承認の失敗を、失敗したときからレシピの状態が変わっていない間だけ返す（#2639）。
 *  原因の設定を直して保存した・別の操作で処理が進んだ後に、前の失敗を出し続けない。 */
export function useRecipeOperationError(
  error: unknown,
  recipe: RecipeOperationState | null | undefined
): unknown {
  const [captured, setCaptured] = useState<CapturedOperationError | null>(null);
  const next = resolveRecipeOperationError(captured, error, recipeOperationKey(recipe));
  if (next.captured !== captured) setCaptured(next.captured);
  return next.visible;
}
