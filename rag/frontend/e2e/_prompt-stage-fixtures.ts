export type StageFixture = { id: string; phase: string; prompts: { id: string; content: string; prompt_key?: "answer_validation.system" }[] };

export const DEFAULT_STAGES: StageFixture[] = [
  { id: "routing", phase: "question", prompts: [{ id: "system", content: "ルーティングの system" }] },
  { id: "generation", phase: "generation", prompts: [{ id: "system", content: "生成の system" }, { id: "plannedSystem", content: "## Step 1: 入力の根拠計画に沿って回答する\n計画を作り直さず原文から回答する。" }, { id: "assumptionSystem", content: "## 回答の前提\n- 未確定の条件を明示する。" }] },
  { id: "audit", phase: "audit", prompts: [{ id: "system", content: "監査の system\n\n## 手順\n\n1. `quote` を照合する。\n2. 不足なら指摘する。\n\n<script>window.promptInjected = true</script>" }] },
];

// 9 つの区分にまたがる段（backend の PROMPT_STAGES の一部。内容は合成）。
export const FLOW_STAGES: StageFixture[] = [
  {
    id: "picture_description",
    phase: "ingestion",
    prompts: [
      { id: "system", content: "図の system" },
      { id: "template", content: "## 現在の図の読み取り指示" },
      { id: "table", content: "表の指示" },
    ],
  },
  { id: "heading_levels", phase: "ingestion", prompts: [{ id: "instruction", content: "階層の指示" }] },
  { id: "field_extraction", phase: "ingestion", prompts: [{ id: "instruction", content: "項目の指示" }] },
  { id: "raptor_summary", phase: "indexing", prompts: [{ id: "system", content: "索引要約の指示" }] },
  { id: "support_guide_draft", phase: "knowledge", prompts: [{ id: "system", content: "下書きの指示" }] },
  { id: "support_guide_conditions", phase: "question", prompts: [{ id: "system", content: "## Step 1: 質問と条件を読む\n合成の条件。\n\n## Step 2: 原文から値を抜き出す\n合成の原文。\n\n## Step 3: 条件の状態を判定する\nknown / unknown / conflicting。\n\n## Step 4: 結果を返す\nJSON のみ。" }] },
  { id: "history_rewrite", phase: "question", prompts: [{ id: "system", content: "書き換えの system" }] },
  { id: "field_filter", phase: "question", prompts: [{ id: "system", content: "条件の指示" }] },
  { id: "routing", phase: "question", prompts: [{ id: "system", content: "ルーティングの system" }] },
  { id: "expansion", phase: "question", prompts: [{ id: "system", content: "検索文の指示" }] },
  { id: "screen_linking", phase: "retrieval", prompts: [{ id: "system", content: "画面の system" }] },
  { id: "article_catalog", phase: "retrieval", prompts: [{ id: "system", content: "目録の指示" }] },
  { id: "crag", phase: "grounding", prompts: [{ id: "system", content: "判定の system" }] },
  { id: "section_extraction", phase: "grounding", prompts: [{ id: "system", content: "抽出の指示" }] },
  { id: "faq_answer", phase: "generation", prompts: [{ id: "system", content: "FAQ の指示" }] },
  { id: "generation", phase: "generation", prompts: [{ id: "system", content: "生成の system" }, { id: "plannedSystem", content: "## Step 1: 入力の根拠計画に沿って回答する\n計画を作り直さず原文から回答する。" }, { id: "assumptionSystem", content: "## 回答の前提\n- 未確定の条件を明示する。" }] },
  {
    id: "audit",
    phase: "audit",
    prompts: [
      { id: "system", content: "監査の system\n\n## 手順\n\n1. `quote` を照合する。\n2. 不足なら指摘する。\n\n<script>window.promptInjected = true</script>" },
      { id: "compactRule", content: "理由の規則" },
    ],
  },
  { id: "application", phase: "audit", prompts: [{ id: "system", content: "当てはめの指示" }] },
  { id: "answer_validation", phase: "audit", prompts: [{ id: "system", content: "合成の資料照合の指示" }] },
  { id: "evaluation", phase: "evaluation", prompts: [{ id: "scope", content: "範囲の指示" }, { id: "system", content: "網羅性・忠実性の指示" }, { id: "correctness", content: "正確性の指示" }] },
];

