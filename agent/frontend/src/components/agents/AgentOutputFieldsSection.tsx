import { Plus, Trash2 } from "lucide-react";
import { Button, Card, CardContent, EmptyState, Section, SelectField, Switch, TextField } from "@production-ready/ui";

import type { AgentOutputField, AgentOutputFieldMatch, AgentOutputFieldType } from "@/lib/api";
import { t } from "@/lib/i18n";

/** 出力の項目の上限と key の形（backend の `output_fields.MAX_OUTPUT_FIELDS` / `OUTPUT_KEY_PATTERN`）。 */
export const MAX_OUTPUT_FIELDS = 30;
const OUTPUT_KEY_PATTERN = /^[a-z][a-z0-9_]{0,39}$/;

const TYPES: AgentOutputFieldType[] = ["text", "number", "date", "boolean"];
const MATCHES: AgentOutputFieldMatch[] = ["exact", "meaning"];

/** 保存の前の確かめ（key の形・重なり・表示名の空）。項目の位置 → 欄 → 文。 */
export type OutputFieldErrors = Record<number, Partial<Record<"key" | "label", string>>>;

export function outputFieldErrors(fields: AgentOutputField[]): OutputFieldErrors {
  const errors: OutputFieldErrors = {};
  const counts = new Map<string, number>();
  for (const field of fields) counts.set(field.key.trim(), (counts.get(field.key.trim()) ?? 0) + 1);
  fields.forEach((field, index) => {
    const key = field.key.trim();
    const entry: Partial<Record<"key" | "label", string>> = {};
    if (!OUTPUT_KEY_PATTERN.test(key)) entry.key = t("agent.output.keyInvalid");
    else if ((counts.get(key) ?? 0) > 1) entry.key = t("agent.output.keyDuplicated");
    if (!field.label.trim()) entry.label = t("agent.output.labelRequired");
    if (entry.key || entry.label) errors[index] = entry;
  });
  return errors;
}

/** 保存する形（前後の空白を除く。意味で比べるのは文字の項目だけ）。 */
export function normalizeOutputFields(fields: AgentOutputField[]): AgentOutputField[] {
  return fields.map((field) => ({
    key: field.key.trim(),
    label: field.label.trim(),
    type: field.type,
    required: field.required,
    description: field.description.trim(),
    match: field.type === "text" && field.match === "meaning" ? "meaning" : "exact",
  }));
}

/**
 * 業務 Agent の出力の項目（#2549）。定義すると、回答のときに項目の値を記録させ、チャットに表と CSV で出す。
 * 項目ごとに key（英小文字）・表示名・型・必須・説明。足す・外すだけ（並べ替えは外して足し直す）。
 */
export function AgentOutputFieldsSection({
  fieldId,
  value,
  onChange,
  errors,
  readOnly,
}: {
  fieldId: string;
  value: AgentOutputField[];
  onChange: (fields: AgentOutputField[]) => void;
  errors: OutputFieldErrors;
  readOnly: boolean;
}) {
  const update = (index: number, patch: Partial<AgentOutputField>) =>
    onChange(value.map((field, position) => (position === index ? { ...field, ...patch } : field)));
  const add = () =>
    onChange([
      ...value,
      { key: `field_${value.length + 1}`, label: "", type: "text", required: false, description: "", match: "exact" },
    ]);
  const remove = (index: number) => onChange(value.filter((_, position) => position !== index));

  return (
    <Section title={t("agent.output.title")} description={t("agent.output.description")}>
      <Card className="min-w-0">
        <CardContent className="space-y-4 pt-5" data-testid="agent-output-fields">
          {value.length === 0 ? (
            <EmptyState title={t("agent.output.empty")} hint={t("agent.output.emptyHint")} />
          ) : (
            <ol className="space-y-4">
              {value.map((field, index) => (
                <li
                  key={index}
                  className="grid min-w-0 gap-3 border-b border-border pb-4 last:border-b-0 last:pb-0 md:grid-cols-2 xl:grid-cols-[repeat(3,minmax(0,1fr))_auto]"
                  data-testid={`agent-output-field-${index}`}
                >
                  <TextField
                    id={`${fieldId}-output-${index}-label`}
                    label={t("agent.output.label")}
                    width="full"
                    required
                    value={field.label}
                    error={errors[index]?.label}
                    onValueChange={(label) => update(index, { label })}
                  />
                  <TextField
                    id={`${fieldId}-output-${index}-key`}
                    label={t("agent.output.key")}
                    helper={t("agent.output.keyHelper")}
                    width="full"
                    required
                    value={field.key}
                    error={errors[index]?.key}
                    onValueChange={(key) => update(index, { key })}
                  />
                  <SelectField<AgentOutputFieldType>
                    id={`${fieldId}-output-${index}-type`}
                    label={t("agent.output.type")}
                    width="full"
                    value={field.type}
                    options={TYPES.map((type) => ({ value: type, label: t(`agent.output.type.${type}`) }))}
                    onValueChange={(type) => update(index, type === "text" ? { type } : { type, match: "exact" })}
                  />
                  <div className="flex items-end gap-3">
                    <label className="flex min-h-9 items-center gap-2 text-sm text-fg">
                      <Switch
                        checked={field.required}
                        aria-label={t("agent.output.requiredOf", { label: field.label || field.key })}
                        onCheckedChange={(required) => update(index, { required })}
                        disabled={readOnly}
                      />
                      {t("agent.output.required")}
                    </label>
                    {!readOnly ? (
                      <Button
                        type="button"
                        variant="ghost"
                        iconOnly
                        icon={Trash2}
                        aria-label={t("agent.output.remove", { label: field.label || field.key })}
                        onClick={() => remove(index)}
                        data-testid={`agent-output-remove-${index}`}
                      />
                    ) : null}
                  </div>
                  {field.type === "text" ? (
                    <SelectField<AgentOutputFieldMatch>
                      id={`${fieldId}-output-${index}-match`}
                      label={t("agent.output.match")}
                      helper={t("agent.output.matchHelper")}
                      width="full"
                      value={field.match === "meaning" ? "meaning" : "exact"}
                      options={MATCHES.map((match) => ({ value: match, label: t(`agent.output.match.${match}`) }))}
                      onValueChange={(match) => update(index, { match })}
                    />
                  ) : null}
                  <TextField
                    id={`${fieldId}-output-${index}-description`}
                    label={t("agent.output.fieldDescription")}
                    helper={t("agent.output.fieldDescriptionHelper")}
                    width="full"
                    className={field.type === "text" ? "xl:col-span-3" : "md:col-span-2 xl:col-span-4"}
                    value={field.description}
                    onValueChange={(description) => update(index, { description })}
                  />
                </li>
              ))}
            </ol>
          )}
          {!readOnly ? (
            <Button
              type="button"
              variant="secondary"
              icon={Plus}
              disabled={value.length >= MAX_OUTPUT_FIELDS}
              onClick={add}
              data-testid="agent-output-add"
            >
              {t("agent.output.add")}
            </Button>
          ) : null}
        </CardContent>
      </Card>
    </Section>
  );
}
