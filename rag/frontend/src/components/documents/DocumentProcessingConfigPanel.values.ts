import type { SelectFieldOption } from "@production-ready/ui";

import type {
  ChunkingStrategyName,
  DocumentProcessingConfig,
  GraphProfileName,
  ParserAdapterBackend,
  PreprocessProfileName,
  SectionRulesMode,
} from "@/lib/api";
import { t, type I18nKey } from "@/lib/i18n";
import { PARSER_BACKEND_ORDER } from "@/lib/parser-backend-order";
import { parserBackendLabel } from "@/lib/source-profile-labels";

import type { RecipeConfigItem } from "./DocumentProcessingConfigPanel.logic";

/**
 * レシピ 11 項目の選択肢と値の表示。選択中レシピの設定（DocumentProcessingConfigPanel）と
 * 設定の概要の全体の既定の一覧（PipelineHubClient。#528）が同じ表示にするために共有する。
 */

const PREPROCESS_VALUES = [
  "passthrough",
  "office_to_pdf",
  "csv_to_jsonl",
  "excel_to_jsonl",
  "word_to_jsonl",
] as const;
export const PREPROCESS_OPTIONS: SelectFieldOption<PreprocessProfileName>[] = PREPROCESS_VALUES.map(
  (value) => ({
    value,
    label: t(`settings.preprocess.profile.${value}` as I18nKey),
  })
);

// 並びは文書解析の画面の使用エンジンのカードと同じ正本（PARSER_BACKEND_ORDER。#3359）。
export const PARSER_VALUES = PARSER_BACKEND_ORDER;
// 準備中（ロードマップ）の解析エンジン（OCI Document Understanding は #3374、MinerU は #3359）。
// 選択肢には灰色で出し、選ばせない（backend も保存を 422 にする）。
export const ROADMAP_PARSER_VALUES: readonly ParserAdapterBackend[] = [
  "mineru",
  "oci_document_understanding",
];
export const PARSER_OPTIONS: SelectFieldOption<ParserAdapterBackend>[] = PARSER_VALUES.map(
  (value) =>
    ROADMAP_PARSER_VALUES.includes(value)
      ? {
          value,
          label: parserBackendLabel(value),
          disabled: true,
          badge: t("settings.parserAdapters.roadmap.badge"),
          disabledReason: t("settings.parserAdapters.roadmap.reason"),
        }
      : { value, label: parserBackendLabel(value) }
);

const CHUNKING_VALUES = [
  "structure_aware",
  "recursive_character",
  "small_to_big",
  "markdown_heading",
  "page_level",
  "fixed_size",
  "fixed_delimiter",
] as const;
export const CHUNKING_OPTIONS: SelectFieldOption<ChunkingStrategyName>[] = CHUNKING_VALUES.map(
  (value) => ({ value, label: t(`settings.chunking.strategy.${value}` as I18nKey) })
);

const GRAPH_VALUES = ["off", "entities"] as const;
export const GRAPH_OPTIONS: SelectFieldOption<GraphProfileName>[] = GRAPH_VALUES.map(
  (value) => ({ value, label: t(`settings.graph.profile.${value}` as I18nKey) })
);

export const SECTION_RULES_VALUES = ["parser", "legal", "official", "numbered", "custom"] as const;
export const SECTION_RULES_OPTIONS: SelectFieldOption<SectionRulesMode>[] = SECTION_RULES_VALUES.map(
  (value) => ({ value, label: t(`sectionRules.mode.${value}` as I18nKey) })
);

export function boolLabel(value: boolean | null) {
  if (value === null) return "—";
  return t(value ? "knowledgeBases.adapter.bool.enabled" : "knowledgeBases.adapter.bool.disabled");
}

export function optionLabel<T extends string>(
  value: T | null,
  options: readonly SelectFieldOption<T>[]
) {
  return value === null ? "—" : (options.find((option) => option.value === value)?.label ?? value);
}

export function parserOptionLabel(value: ParserAdapterBackend | null) {
  if (value === null) return "—";
  const option = PARSER_OPTIONS.find((candidate) => candidate.value === value);
  return option?.label ?? t("documents.processingConfig.parserRemoved", { engine: value });
}

/** 項目の実効値の表示。項目の並びは RECIPE_CONFIG_ITEMS が決める(#523)。 */
export function recipeConfigValueLabel(item: RecipeConfigItem, effective: DocumentProcessingConfig) {
  switch (item.field) {
    case "preprocess_profile":
      return optionLabel(effective.preprocess_profile, PREPROCESS_OPTIONS);
    case "parser_adapter_backend":
      return parserOptionLabel(effective.parser_adapter_backend);
    case "chunking_strategy":
      return optionLabel(effective.chunking_strategy, CHUNKING_OPTIONS);
    case "graph_profile":
      return optionLabel(effective.graph_profile, GRAPH_OPTIONS);
    case "section_rules_mode":
      return optionLabel(effective.section_rules_mode ?? null, SECTION_RULES_OPTIONS);
    default:
      return boolLabel(effective[item.field] ?? null);
  }
}
