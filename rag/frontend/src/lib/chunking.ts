import type { ChunkingStrategyName, DocumentChunkView } from "@/lib/api";
import type { I18nKey } from "@/lib/i18n";

export const CHUNK_SIZE_MIN_CHARS = 200;
export const CHUNK_SIZE_MAX_CHARS = 32_000;
export const CHUNK_OVERLAP_MAX_CHARS = 8_000;

export type ChunkingPreset = {
  chunkSize: number;
  overlap: number;
};

export function isSemanticBoundaryStrategy(strategy: ChunkingStrategyName): boolean {
  return strategy === "markdown_heading" || strategy === "page_level";
}

export function chunkingStrategyPreset(strategy: ChunkingStrategyName): ChunkingPreset {
  return isSemanticBoundaryStrategy(strategy)
    ? { chunkSize: CHUNK_SIZE_MAX_CHARS, overlap: 0 }
    : { chunkSize: 800, overlap: 120 };
}

export function chunkSizeLabelKey(strategy: ChunkingStrategyName): I18nKey {
  if (strategy === "markdown_heading") return "settings.chunking.params.headingSplitLimit";
  if (strategy === "page_level") return "settings.chunking.params.pageSplitLimit";
  return "settings.chunking.params.chunkSize";
}

export function overlapLabelKey(strategy: ChunkingStrategyName): I18nKey {
  return isSemanticBoundaryStrategy(strategy)
    ? "settings.chunking.params.semanticOverlap"
    : "settings.chunking.params.overlap";
}

export const SMALL_TO_BIG_STRATEGY: ChunkingStrategyName = "small_to_big";

/**
 * 以前の版(#300)で、親子階層（small-to-big）の代わりに構造認識で分割した chunk の理由。
 * 今の分割はどの解析の結果でも親子にする(#3377)ので、新しい chunk には付かない。
 */
export const LAYOUT_MISSING_REASON = "layout_missing";

/**
 * 以前の版で親子階層（small-to-big）の代わりに構造認識で分割した chunk を含むか(作り直しの案内に使う)。
 * backend が chunk metadata の `chunk_strategy_fallback_reason` に残した印を見る。
 */
export function smallToBigFellBack(chunks: readonly Pick<DocumentChunkView, "metadata">[]): boolean {
  return chunks.some(
    (chunk) => chunk.metadata.chunk_strategy_fallback_reason === LAYOUT_MISSING_REASON
  );
}

export type SmallToBigParamField =
  | "chunk_child_target_chars"
  | "chunk_table_child_target_chars"
  | "chunk_parent_target_chars"
  | "chunk_parent_max_pages"
  | "chunk_parent_max_children";

export type SmallToBigParams = Record<SmallToBigParamField, number>;

export type SmallToBigParamSpec = {
  field: SmallToBigParamField;
  labelKey: I18nKey;
  hintKey: I18nKey;
  min: number;
  max: number;
  step: number;
  defaultValue: number;
};

/**
 * 親子階層（small-to-big）の分割パラメータ。既定値・範囲・刻みは rag_poc（移植元）の
 * rag_engine.chunking.constants(DEFAULT_* / *_RANGE)と同じ(backend の Settings と一致させる)。
 */
export const SMALL_TO_BIG_PARAMS: readonly SmallToBigParamSpec[] = [
  {
    field: "chunk_child_target_chars",
    labelKey: "settings.chunking.params.chunkChildTargetChars",
    hintKey: "settings.chunking.params.chunkChildTargetCharsHint",
    min: 300,
    max: 1600,
    step: 50,
    defaultValue: 1000,
  },
  {
    field: "chunk_table_child_target_chars",
    labelKey: "settings.chunking.params.chunkTableChildTargetChars",
    hintKey: "settings.chunking.params.chunkTableChildTargetCharsHint",
    min: 300,
    max: 8000,
    step: 100,
    defaultValue: 3000,
  },
  {
    field: "chunk_parent_target_chars",
    labelKey: "settings.chunking.params.chunkParentTargetChars",
    hintKey: "settings.chunking.params.chunkParentTargetCharsHint",
    min: 1200,
    max: 10000,
    step: 100,
    defaultValue: 6000,
  },
  {
    field: "chunk_parent_max_pages",
    labelKey: "settings.chunking.params.chunkParentMaxPages",
    hintKey: "settings.chunking.params.chunkParentMaxPagesHint",
    min: 1,
    max: 5,
    step: 1,
    defaultValue: 3,
  },
  {
    field: "chunk_parent_max_children",
    labelKey: "settings.chunking.params.chunkParentMaxChildren",
    hintKey: "settings.chunking.params.chunkParentMaxChildrenHint",
    min: 3,
    max: 20,
    step: 1,
    defaultValue: 12,
  },
];

/**
 * 固定分割符の既定（backend の `RAG_CHUNK_DELIMITER` の既定と同じ。#2457）。構造化レコード JSONL の
 * 解析が記録と記録の間に置く境界のマーカー。
 */
export const JSONL_RECORD_BOUNDARY = "<<<JSONL_RECORD_BOUNDARY>>>";

/** 構造化レコードの JSONL を作るファイル準備（解析エンジンは構造化レコード JSONL だけ）。 */
export const STRUCTURED_RECORDS_PREPROCESS_PROFILES = ["csv_to_jsonl", "excel_to_jsonl", "word_to_jsonl"] as const;

export const DEFAULT_SMALL_TO_BIG_PARAMS: SmallToBigParams = Object.fromEntries(
  SMALL_TO_BIG_PARAMS.map((spec) => [spec.field, spec.defaultValue])
) as SmallToBigParams;

/** 範囲外・未入力の親子階層のパラメータがあれば、最初のその項目の定義を返す。 */
export function invalidSmallToBigParam(
  values: Partial<Record<SmallToBigParamField, number | null | undefined>>
): SmallToBigParamSpec | null {
  return (
    SMALL_TO_BIG_PARAMS.find((spec) => {
      const value = values[spec.field];
      return (
        typeof value !== "number" ||
        !Number.isInteger(value) ||
        value < spec.min ||
        value > spec.max
      );
    }) ?? null
  );
}
