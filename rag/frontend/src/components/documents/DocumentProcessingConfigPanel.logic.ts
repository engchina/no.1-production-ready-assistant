import type {
  DocumentProcessingConfig,
  IngestionJobPhase,
  PreprocessProfileName,
} from "@/lib/api";
import { JSONL_RECORD_BOUNDARY, STRUCTURED_RECORDS_PREPROCESS_PROFILES } from "@/lib/chunking";
import type { I18nKey } from "@/lib/i18n";
import { APP_ROUTES } from "@/lib/routes";
import { SETTINGS_ANCHORS } from "@/lib/settings-anchors";

/**
 * 「選択中レシピの設定」の項目と並び順の正本（#523）。
 *
 * 上の要約（現在値のカード）と下の上書きの一覧（グローバルを継承 / 上書き）は、どちらもこの
 * 配列だけから作る。2 つの表示で項目や順番を別々に書かない。
 *
 * 並びは取込の実際の処理順（`rag/backend/app/rag/ingestion.py` と
 * `rag/backend/app/api/routes/documents.py` の自動進行）にそろえる。
 * - PREPROCESS: ファイル準備 → 「ファイル準備後に抽出へ進む」のゲート
 *   （`ingest` が preprocess の artifact を保存した直後に `rag_auto_parse_after_preprocess_enabled` を見て止まる）
 * - EXTRACT: 文書解析 → Vision（`_attach_vision`）→ 見出しの階層の推定 → 項目抽出（`_attach_extraction_fields`）
 *   → 章節の抽出規則 → 章節の要約（`_attach_navigation_tree`）→ 「抽出後に Chunk 作成へ進む」のゲート（REVIEW で止まり、
 *   `rag_auto_chunk_after_extract_enabled` なら CHUNK の job を積む）
 * - CHUNK: 文書分割 → 文脈ヘッダ（`_chunks_with_context_headers` は分割の直後に chunk metadata へ付ける）
 *   → 「Chunk 後に Embedding / 索引へ進む」のゲート（CHUNKED で止まり、
 *   `rag_auto_index_after_chunk_enabled` なら INDEX の job を積む）
 * - INDEX: embedding と索引の保存の後に実体・別名索引（`_save_entity_index_if_enabled`。#1362）と
 *   関係情報（`_save_index` / `_save_embeddings_for_chunk_set` の `graph_indexing`）を作る
 */

type SelectConfigField =
  | "preprocess_profile"
  | "parser_adapter_backend"
  | "chunking_strategy"
  | "graph_profile"
  | "section_rules_mode";

type BooleanConfigField =
  | "auto_parse_after_preprocess_enabled"
  | "vision_enabled"
  | "heading_level_inference_enabled"
  | "field_extraction_enabled"
  | "navigation_summary_enabled"
  | "auto_chunk_after_extract_enabled"
  | "auto_index_after_chunk_enabled"
  | "entity_index_enabled";

/**
 * 全体の既定（グローバル設定）を変える画面（#528）。レシピの「グローバル設定を開く」と、
 * 設定の概要の全体の既定の一覧が、この 1 か所の定義からリンクを作る。
 */
export interface GlobalSettingsLocation {
  route: string;
  /** 画面の中の節の id（URL の hash）。無ければ画面の先頭へ移動する。 */
  anchor?: string;
}

type RecipeConfigItemBase = {
  label: I18nKey;
  phase: IngestionJobPhase;
  /**
   * 全体の既定を変える画面。権限に応じて概要・レシピから導線を表示する。
   */
  globalSettings: GlobalSettingsLocation | null;
};

export type RecipeConfigItem =
  | ({ field: SelectConfigField; kind: "select" } & RecipeConfigItemBase)
  | ({ field: BooleanConfigField; kind: "boolean" } & RecipeConfigItemBase);

export type RecipeConfigField = RecipeConfigItem["field"];

export const RECIPE_CONFIG_ITEMS = [
  {
    field: "preprocess_profile",
    kind: "select",
    label: "knowledgeBases.adapter.field.preprocessProfile",
    phase: "PREPROCESS",
    globalSettings: { route: APP_ROUTES.settingsPreprocess },
  },
  {
    field: "auto_parse_after_preprocess_enabled",
    kind: "boolean",
    label: "knowledgeBases.adapter.field.autoParseAfterPreprocess",
    phase: "PREPROCESS",
    globalSettings: { route: APP_ROUTES.settingsPipeline, anchor: SETTINGS_ANCHORS.autoParseGate },
  },
  {
    field: "parser_adapter_backend",
    kind: "select",
    label: "knowledgeBases.adapter.field.parserBackend",
    phase: "EXTRACT",
    globalSettings: { route: APP_ROUTES.settingsParserAdapters },
  },
  {
    field: "vision_enabled",
    kind: "boolean",
    label: "knowledgeBases.adapter.field.vision",
    phase: "EXTRACT",
    globalSettings: { route: APP_ROUTES.settingsParserAdapters, anchor: SETTINGS_ANCHORS.vision },
  },
  // 見出しの階層の推定（#2277）。原本全体の整理の後、項目抽出・章節木の前に行う。
  {
    field: "heading_level_inference_enabled",
    kind: "boolean",
    label: "knowledgeBases.adapter.field.headingLevels",
    phase: "EXTRACT",
    globalSettings: {
      route: APP_ROUTES.settingsParserAdapters,
      anchor: SETTINGS_ANCHORS.headingLevels,
    },
  },
  {
    field: "field_extraction_enabled",
    kind: "boolean",
    label: "knowledgeBases.adapter.field.fieldExtraction",
    phase: "EXTRACT",
    globalSettings: {
      route: APP_ROUTES.settingsParserAdapters,
      anchor: SETTINGS_ANCHORS.fieldExtraction,
    },
  },
  // 章節の抽出規則で章節を作り、その章節ごとに要約を作る。
  {
    field: "section_rules_mode",
    kind: "select",
    label: "documents.processingConfig.sectionRules",
    phase: "EXTRACT",
    globalSettings: {
      route: APP_ROUTES.settingsParserAdapters,
      anchor: SETTINGS_ANCHORS.sectionRules,
    },
  },
  {
    field: "navigation_summary_enabled",
    kind: "boolean",
    label: "knowledgeBases.adapter.field.navigationSummary",
    phase: "EXTRACT",
    globalSettings: {
      route: APP_ROUTES.settingsParserAdapters,
      anchor: SETTINGS_ANCHORS.navigationSummary,
    },
  },
  {
    field: "auto_chunk_after_extract_enabled",
    kind: "boolean",
    label: "knowledgeBases.adapter.field.autoChunkAfterExtract",
    phase: "EXTRACT",
    globalSettings: { route: APP_ROUTES.settingsPipeline, anchor: SETTINGS_ANCHORS.autoChunkGate },
  },
  {
    field: "chunking_strategy",
    kind: "select",
    label: "knowledgeBases.adapter.field.chunkingStrategy",
    phase: "CHUNK",
    globalSettings: { route: APP_ROUTES.settingsChunking },
  },
  {
    field: "auto_index_after_chunk_enabled",
    kind: "boolean",
    label: "knowledgeBases.adapter.field.autoIndexAfterChunk",
    phase: "CHUNK",
    globalSettings: { route: APP_ROUTES.settingsPipeline, anchor: SETTINGS_ANCHORS.autoIndexGate },
  },
  // 実体・別名索引（#1362 / #1388）。索引の保存の後に作る。検索方法の全体設定の「実体・別名による検索拡張」が使う。
  // 名前・属性の列は、この項目が有効なときに下の行で選ぶ。
  {
    field: "entity_index_enabled",
    kind: "boolean",
    label: "knowledgeBases.adapter.field.entityIndex",
    phase: "INDEX",
    globalSettings: { route: APP_ROUTES.settingsEntityIndex },
  },
  {
    field: "graph_profile",
    kind: "select",
    label: "knowledgeBases.adapter.field.graphProfile",
    phase: "INDEX",
    globalSettings: { route: APP_ROUTES.settingsGraph },
  },
] as const satisfies readonly RecipeConfigItem[];

/**
 * 全体の既定を変える画面の URL（`/settings/parser-adapters#post-parse-vision` など）。画面を持たない
 * 項目は null。
 */
export function globalSettingsHref(item: RecipeConfigItem): string | null {
  if (!item.globalSettings) return null;
  const { route, anchor } = item.globalSettings;
  return anchor ? `${route}#${anchor}` : route;
}

/** 上書きの件数（n / 全体）を数える対象。項目の正本と同じ集合。 */
export const RECIPE_CONFIG_FIELDS: RecipeConfigField[] = RECIPE_CONFIG_ITEMS.map(
  (item) => item.field
);

/** 工程の区切りの見出し。上のレシピの工程表示（DocumentRecipeManager の PHASES）と同じ文言。 */
const PHASE_LABELS: Record<IngestionJobPhase, I18nKey> = {
  PREPROCESS: "flow.step.preprocess",
  EXTRACT: "flow.step.extract",
  CHUNK: "flow.step.chunk",
  INDEX: "flow.step.indexing",
};

const PHASE_ORDER: IngestionJobPhase[] = ["PREPROCESS", "EXTRACT", "CHUNK", "INDEX"];

export interface RecipeConfigGroup {
  phase: IngestionJobPhase;
  /** 1 始まりの工程番号（見出しの「1. ファイル準備」） */
  index: number;
  label: I18nKey;
  items: RecipeConfigItem[];
}

/** 項目を工程ごとに分ける。工程の順と、工程の中の項目の順は RECIPE_CONFIG_ITEMS のまま。 */
export function recipeConfigGroups(
  items: readonly RecipeConfigItem[] = RECIPE_CONFIG_ITEMS
): RecipeConfigGroup[] {
  return PHASE_ORDER.flatMap((phase, position) => {
    const phaseItems = items.filter((item) => item.phase === phase);
    if (!phaseItems.length) return [];
    return [{ phase, index: position + 1, label: PHASE_LABELS[phase], items: phaseItems }];
  });
}

/** 構造化レコードの JSONL を作るファイル準備（CSV→JSONL・Excel→JSONL・Word→JSONL。#2457）か。 */
export function isStructuredRecordsPreprocess(profile: PreprocessProfileName | null | undefined) {
  return (STRUCTURED_RECORDS_PREPROCESS_PROFILES as readonly string[]).includes(profile ?? "");
}

/**
 * ファイル準備を選んだときのレシピの上書き（#2457）。CSV→JSONL・Excel→JSONL・Word→JSONL を選んだら、分割
 * 「固定分割符」・記録の境界のマーカーを、画面に見える上書きとして入れる（実行時に隠れて切り替えない）。
 * 利用者が選んだ分割方式・分割符はそのまま残す。解析エンジンは変えない（成果物の JSONL は Docling・
 * Unstructured のサービスが読む。#21）。
 */
export function preprocessSelectionPatch(
  current: DocumentProcessingConfig,
  profile: PreprocessProfileName | null
): Partial<DocumentProcessingConfig> {
  if (!isStructuredRecordsPreprocess(profile)) return { preprocess_profile: profile };
  const patch: Partial<DocumentProcessingConfig> = { preprocess_profile: profile };
  if (current.chunking_strategy == null) patch.chunking_strategy = "fixed_delimiter";
  const strategy = patch.chunking_strategy ?? current.chunking_strategy;
  if (strategy === "fixed_delimiter" && current.chunk_delimiter == null) {
    patch.chunk_delimiter = JSONL_RECORD_BOUNDARY;
  }
  return patch;
}
