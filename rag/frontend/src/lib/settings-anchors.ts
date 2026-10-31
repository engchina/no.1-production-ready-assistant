/**
 * 設定画面の中の節の id（URL の hash）。レシピの「グローバル設定を開く」と設定の概要の一覧が、
 * 全体の既定を変える場所へ直接移動するために使う（#528）。画面側も同じ定数で id を付ける。
 */
export const SETTINGS_ANCHORS = {
  /** 文書解析 >「解析後の処理」の各項目 */
  vision: "post-parse-vision",
  headingLevels: "post-parse-heading-levels",
  fieldExtraction: "post-parse-field-extraction",
  navigationSummary: "post-parse-navigation-summary",
  raptorSummary: "post-parse-raptor-summary",
  sectionRules: "post-parse-section-rules",
  /** 設定の概要 >「取込の流れと全体の既定」 */
  pipelineFlow: "pipeline-recipe-defaults",
  /** 設定の概要 > 工程の間の自動進行のスイッチ */
  autoParseGate: "pipeline-gate-auto-parse",
  autoChunkGate: "pipeline-gate-auto-chunk",
  autoIndexGate: "pipeline-gate-auto-index",
} as const;
