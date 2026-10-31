/**
 * 編集できるプロンプトの欄の検証（#1010）。規則は backend の `validate_prompt` と同じ
 * （空白だけは空、必須の placeholder `{{name}}` がすべて含まれていること）。
 */

import { t } from "@/lib/i18n";
import { requiredTextError } from "@/lib/required-fields";

export function answerPromptError(
  content: string,
  fieldLabel: string,
  requiredPlaceholders: readonly string[],
  requiredTokens: readonly string[] = []
): string | null {
  const empty = requiredTextError(content, t("validation.required", { field: fieldLabel }));
  if (empty) return empty;
  const missing = requiredPlaceholders
    .map((name) => `{{${name}}}`)
    .concat([...requiredTokens])
    .filter((placeholder) => !content.includes(placeholder));
  return missing.length
    ? t("settings.answerPrompts.missingPlaceholders", { names: missing.join("、") })
    : null;
}

/** 処理の順の区分ごとにまとめた段（#2054）。 */
export interface AnswerPromptPhaseGroup<T extends { phase: string }> {
  phase: T["phase"];
  stages: T[];
}

/**
 * 段を処理の順のまま、続く同じ区分ごとにまとめる。backend が処理の順に並べて返すので並べ替えない
 * （区分が飛び飛びに来ても順を入れ替えず、別のまとまりにする）。
 */
export function groupStagesByPhase<T extends { phase: string }>(stages: readonly T[]): AnswerPromptPhaseGroup<T>[] {
  const groups: AnswerPromptPhaseGroup<T>[] = [];
  for (const stage of stages) {
    const last = groups.at(-1);
    if (last && last.phase === stage.phase) last.stages.push(stage);
    else groups.push({ phase: stage.phase, stages: [stage] });
  }
  return groups;
}

/** 検索方法に配置する、質問の解釈から根拠の判定までの区分。 */
export function isRetrievalPromptStage(stage: { id?: string; phase: string }): boolean {
  // 条件認識だけは回答前の確認の指示として「回答プロンプト」に置く。
  return stage.id !== "support_guide_conditions" && ["question", "retrieval", "grounding"].includes(stage.phase);
}
