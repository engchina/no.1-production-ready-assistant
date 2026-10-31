import { describe, expect, it } from "vitest";

import { recipeOperationKey, resolveRecipeOperationError } from "./recipe-operation-error";
import type { FileStatus } from "@/lib/api";

const recipe = {
  recipe_id: "recipe-1",
  config_revision: 1,
  status: "INDEXED" as FileStatus,
  updated_at: "2026-10-20T00:00:00Z",
};

describe("recipeOperationKey", () => {
  it("レシピが無ければ null", () => {
    expect(recipeOperationKey(null)).toBeNull();
  });

  it("設定の保存・処理の進行で変わる", () => {
    const key = recipeOperationKey(recipe);
    expect(recipeOperationKey({ ...recipe, config_revision: 2 })).not.toBe(key);
    expect(recipeOperationKey({ ...recipe, status: "PREPROCESSING" })).not.toBe(key);
    expect(recipeOperationKey({ ...recipe, updated_at: "2026-10-20T00:01:00Z" })).not.toBe(key);
    expect(recipeOperationKey({ ...recipe })).toBe(key);
  });
});

describe("resolveRecipeOperationError", () => {
  const error = new Error("409");
  const key = recipeOperationKey(recipe);

  it("失敗が無ければ何も出さず、覚えた状態も消す", () => {
    expect(resolveRecipeOperationError({ error, recipeKey: key }, null, key)).toEqual({
      captured: null,
      visible: null,
    });
  });

  it("失敗を初めて見たときは出して、そのときのレシピの状態を覚える", () => {
    expect(resolveRecipeOperationError(null, error, key)).toEqual({
      captured: { error, recipeKey: key },
      visible: error,
    });
  });

  it("レシピの状態が同じ間は同じ覚えた状態のまま出し続ける", () => {
    const captured = { error, recipeKey: key };
    const next = resolveRecipeOperationError(captured, error, key);
    expect(next.captured).toBe(captured);
    expect(next.visible).toBe(error);
  });

  it("設定を保存した・処理が進んだ後は前の失敗を出さない", () => {
    const captured = { error, recipeKey: key };
    const saved = recipeOperationKey({ ...recipe, config_revision: 2 });
    const next = resolveRecipeOperationError(captured, error, saved);
    expect(next.captured).toBe(captured);
    expect(next.visible).toBeNull();
  });

  it("新しい失敗は、そのときのレシピの状態で覚え直して出す", () => {
    const saved = recipeOperationKey({ ...recipe, config_revision: 2 });
    const nextError = new Error("409 again");
    expect(resolveRecipeOperationError({ error, recipeKey: key }, nextError, saved)).toEqual({
      captured: { error: nextError, recipeKey: saved },
      visible: nextError,
    });
  });
});
