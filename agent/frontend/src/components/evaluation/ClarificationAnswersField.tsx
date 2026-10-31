import { Plus, Trash2 } from "lucide-react";
import { Button, Fieldset, FormGrid, TextField } from "@production-ready/ui";

import type { EvaluationClarificationAnswer } from "@/lib/api";
import { t } from "@/lib/i18n";

/** 評価ケースが持てる問い返しへの答えの数（backend の `EVALUATION_CLARIFICATION_ANSWERS_MAX`。#1845）。 */
export const CLARIFICATION_ANSWERS_MAX = 10;

/** 画面で編集する答え（選ぶ候補はカンマ区切りの文字で持つ）。 */
export interface ClarificationAnswerDraft {
  key: string;
  itemId: string;
  options: string;
  otherText: string;
}

let answerKey = 0;
export function clarificationAnswerDraft(item?: EvaluationClarificationAnswer): ClarificationAnswerDraft {
  answerKey += 1;
  return {
    key: `clarification-answer-${answerKey}`,
    itemId: item?.item_id ?? "",
    options: (item?.option_ids ?? []).join(", "),
    otherText: item?.other_text ?? "",
  };
}

export function splitOptions(text: string): string[] {
  return text
    .split(/[,、]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

/** 候補も自由入力も無い答え（保存すると 422 になる）。 */
export function clarificationAnswerInvalid(item: ClarificationAnswerDraft): boolean {
  return splitOptions(item.options).length === 0 && !item.otherText.trim();
}

export function toClarificationAnswers(items: ClarificationAnswerDraft[]): EvaluationClarificationAnswer[] {
  return items.map((item) => ({
    item_id: item.itemId.trim(),
    option_ids: splitOptions(item.options),
    other_text: item.otherText.trim(),
  }));
}

/**
 * 評価ケースの「問い返しへの答え」（#1845）。答えの形は共通の「回答前の確認」（`ClarificationCard`）の
 * `{ itemId, optionIds, otherText }` と同じで、Run が問い返し（NL2SQL の SQL の生成前の確認・RAG の条件の確認）で
 * 止まったら、この答えを同じ会話の次の実行で送る。
 */
export function ClarificationAnswersField({
  idPrefix,
  caseNumber,
  answers,
  submitted,
  onChange,
}: {
  idPrefix: string;
  caseNumber: number;
  answers: ClarificationAnswerDraft[];
  submitted: boolean;
  onChange: (answers: ClarificationAnswerDraft[]) => void;
}) {
  function update(key: string, patch: Partial<ClarificationAnswerDraft>) {
    onChange(answers.map((item) => (item.key === key ? { ...item, ...patch } : item)));
  }

  return (
    <Fieldset
      id={`${idPrefix}-clarification`}
      legend={t("evaluation.clarification.legend")}
      helper={t("evaluation.clarification.helper")}
    >
      <div className="space-y-3" data-testid={`evaluation-case-${caseNumber}-clarification`}>
        {answers.map((item, index) => {
          const number = index + 1;
          const invalid = submitted && clarificationAnswerInvalid(item);
          return (
            <div key={item.key} className="space-y-2 rounded-md border border-border p-3" data-testid={`evaluation-case-${caseNumber}-answer-${number}`}>
              <div className="flex items-center justify-between gap-2">
                <p className="text-xs font-semibold text-fg-muted">{t("evaluation.clarification.answerNumber", { number })}</p>
                <Button
                  variant="ghost"
                  size="sm"
                  iconOnly
                  icon={Trash2}
                  aria-label={t("evaluation.clarification.remove", { case: caseNumber, number })}
                  onClick={() => onChange(answers.filter((answer) => answer.key !== item.key))}
                />
              </div>
              <FormGrid>
                <TextField
                  id={`${item.key}-item`}
                  label={t("evaluation.clarification.itemId")}
                  helper={t("evaluation.clarification.itemIdHelper")}
                  width="md"
                  maxLength={128}
                  value={item.itemId}
                  onValueChange={(value) => update(item.key, { itemId: value })}
                />
                <TextField
                  id={`${item.key}-options`}
                  label={t("evaluation.clarification.options")}
                  helper={t("evaluation.clarification.optionsHelper")}
                  width="lg"
                  value={item.options}
                  error={invalid ? t("evaluation.clarification.answerRequired") : undefined}
                  onValueChange={(value) => update(item.key, { options: value })}
                />
                <TextField
                  id={`${item.key}-other`}
                  label={t("evaluation.clarification.otherText")}
                  helper={t("evaluation.clarification.otherTextHelper")}
                  maxLength={2000}
                  value={item.otherText}
                  onValueChange={(value) => update(item.key, { otherText: value })}
                  width="full"
                  className="col-span-full"
                />
              </FormGrid>
            </div>
          );
        })}
        <Button
          variant="secondary"
          size="sm"
          icon={Plus}
          disabled={answers.length >= CLARIFICATION_ANSWERS_MAX}
          onClick={() => onChange([...answers, clarificationAnswerDraft()])}
        >
          {t("evaluation.clarification.add")}
        </Button>
      </div>
    </Fieldset>
  );
}
