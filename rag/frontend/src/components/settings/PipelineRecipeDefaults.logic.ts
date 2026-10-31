import type { RecipeConfigItem } from "@/components/documents/DocumentProcessingConfigPanel.logic";
import type { PipelineAutoAdvanceField } from "@/lib/api";

/** 設定の概要で保存する、工程の自動進行の 3 つのゲート（#528）。 */
export type AutoAdvanceForm = Record<PipelineAutoAdvanceField, boolean>;

const AUTO_ADVANCE_FIELDS: readonly PipelineAutoAdvanceField[] = [
  "auto_parse_after_preprocess_enabled",
  "auto_chunk_after_extract_enabled",
  "auto_index_after_chunk_enabled",
];

export function isAutoAdvanceItem(
  item: RecipeConfigItem
): item is RecipeConfigItem & { field: PipelineAutoAdvanceField } {
  return (AUTO_ADVANCE_FIELDS as readonly string[]).includes(item.field);
}

/**
 * 1 工程の項目を、工程の中の項目と、工程の最後の自動進行のゲートに分ける。
 * レシピの項目の定義（#523）では、ゲートはその工程の最後の項目（次の工程へ進むかの判定）。
 * ゲートが最後以外にあるときは定義の誤りなので、分けずに項目として扱う。
 */
export function splitGateItems(items: readonly RecipeConfigItem[]): {
  items: RecipeConfigItem[];
  gate: RecipeConfigItem | null;
} {
  const last = items.at(-1);
  if (last && isAutoAdvanceItem(last)) {
    return { items: items.slice(0, -1), gate: last };
  }
  return { items: [...items], gate: null };
}
