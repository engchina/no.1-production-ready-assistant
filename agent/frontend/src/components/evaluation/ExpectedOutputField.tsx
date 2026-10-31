import { Plus, Trash2 } from "lucide-react";
import { Button, Fieldset, SelectField, TextField } from "@production-ready/ui";

import type { AgentOutputField, EvaluationOutputValue } from "@/lib/api";
import { t } from "@/lib/i18n";

/** 評価ケースが持てる期待する出力の行の数（backend の `EVALUATION_EXPECTED_OUTPUT_ROWS_MAX`。#2558）。 */
export const EXPECTED_OUTPUT_ROWS_MAX = 50;

/** 画面で編集する期待する出力の 1 行（値は文字で持ち、空の項目は判定しない）。 */
export interface ExpectedOutputRowDraft {
  key: string;
  values: Record<string, string>;
}

let rowKey = 0;
function nextRowKey() {
  rowKey += 1;
  return `expected-output-${rowKey}`;
}

function text(value: EvaluationOutputValue | undefined): string {
  if (value === null || value === undefined) return "";
  return String(value);
}

export function expectedOutputDrafts(rows: Record<string, EvaluationOutputValue>[] | undefined): ExpectedOutputRowDraft[] {
  return (rows ?? []).map((row) => ({
    key: nextRowKey(),
    values: Object.fromEntries(Object.entries(row).map(([field, value]) => [field, text(value)])),
  }));
}

/** 保存する形（型に合わせて数・真偽にし、空の項目は外す。項目の無い行は外す）。 */
export function toExpectedOutput(
  rows: ExpectedOutputRowDraft[],
  fields: AgentOutputField[]
): Record<string, EvaluationOutputValue>[] {
  const types = new Map(fields.map((field) => [field.key, field.type]));
  return rows
    .map((row) =>
      Object.fromEntries(
        Object.entries(row.values)
          .map(([field, raw]): [string, EvaluationOutputValue] | null => {
            const value = raw.trim();
            if (!value) return null;
            const type = types.get(field);
            if (type === "boolean") return [field, value === "true"];
            if (type === "number") {
              const number = Number(value.replace(/,/g, ""));
              return [field, Number.isFinite(number) ? number : value];
            }
            return [field, value];
          })
          .filter((entry): entry is [string, EvaluationOutputValue] => entry !== null)
      )
    )
    .filter((row) => Object.keys(row).length > 0);
}

/**
 * 評価ケースの「期待する出力」（#2558）。評価する業務 Agent の出力の項目ごとに期待する値を入れる。値を入れた項目だけを、
 * 業務 Agent が記録した出力と比べる（1 列目の値で行を組む）。
 */
export function ExpectedOutputField({
  idPrefix,
  caseNumber,
  fields,
  rows,
  onChange,
}: {
  idPrefix: string;
  caseNumber: number;
  fields: AgentOutputField[];
  rows: ExpectedOutputRowDraft[];
  onChange: (rows: ExpectedOutputRowDraft[]) => void;
}) {
  if (fields.length === 0 && rows.length === 0) return null;
  const update = (key: string, field: string, value: string) =>
    onChange(rows.map((row) => (row.key === key ? { ...row, values: { ...row.values, [field]: value } } : row)));
  return (
    <div data-testid={`evaluation-case-${caseNumber}-expected-output`}>
    <Fieldset
      id={`${idPrefix}-expected-output`}
      legend={t("evaluation.set.expectedOutput")}
      helper={t("evaluation.set.expectedOutputHelper")}
    >
      <div className="space-y-3">
        {rows.map((row, index) => (
          <div
            key={row.key}
            className="grid min-w-0 gap-3 rounded-md border border-border p-3 sm:grid-cols-2 lg:grid-cols-3"
            data-testid={`evaluation-case-${caseNumber}-expected-output-row-${index + 1}`}
          >
            {fields.map((field) =>
              field.type === "boolean" ? (
                <SelectField
                  key={field.key}
                  id={`${idPrefix}-output-${index}-${field.key}`}
                  label={field.label}
                  width="full"
                  value={row.values[field.key] ?? ""}
                  emptyOptionLabel={t("evaluation.set.expectedOutputNotJudged")}
                  options={[
                    { value: "true", label: t("chat.output.yes") },
                    { value: "false", label: t("chat.output.no") },
                  ]}
                  onValueChange={(value) => update(row.key, field.key, value)}
                />
              ) : (
                <TextField
                  key={field.key}
                  id={`${idPrefix}-output-${index}-${field.key}`}
                  label={field.label}
                  width="full"
                  placeholder={field.type === "date" ? "YYYY-MM-DD" : undefined}
                  value={row.values[field.key] ?? ""}
                  onValueChange={(value) => update(row.key, field.key, value)}
                />
              )
            )}
            <div className="flex items-end">
              <Button
                type="button"
                variant="ghost"
                icon={Trash2}
                onClick={() => onChange(rows.filter((item) => item.key !== row.key))}
                data-testid={`evaluation-case-${caseNumber}-expected-output-remove-${index + 1}`}
              >
                {t("evaluation.set.expectedOutputRemove", { number: index + 1 })}
              </Button>
            </div>
          </div>
        ))}
        {fields.length > 0 ? (
          <Button
            type="button"
            variant="secondary"
            size="sm"
            icon={Plus}
            disabled={rows.length >= EXPECTED_OUTPUT_ROWS_MAX}
            onClick={() => onChange([...rows, { key: nextRowKey(), values: {} }])}
            data-testid={`evaluation-case-${caseNumber}-expected-output-add`}
          >
            {t("evaluation.set.expectedOutputAdd")}
          </Button>
        ) : null}
      </div>
    </Fieldset>
    </div>
  );
}
