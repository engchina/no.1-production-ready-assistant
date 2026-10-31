import {
  Banner,
  Button,
  Fieldset,
  SelectField,
  TextareaField,
} from "@production-ready/ui";
import { Check, CircleHelp, Save, X } from "lucide-react";
import { useState } from "react";

import type { CalibrationErrorType, CalibrationLabel } from "@/lib/api";
import { type I18nKey, t } from "@/lib/i18n";

import { CALIBRATION_LABELS } from "./calibration-format";

const LABEL_ICON = { pass: Check, fail: X, cannot_judge: CircleHelp } as const;

export interface LabelDraft {
  label: CalibrationLabel | null;
  errorType: string;
  note: string;
}

/**
 * 判定の入力（pass / fail / cannot_judge・誤りの型・メモ）。ラベルは押したボタンを塗りにし、
 * `aria-pressed` で伝える（色だけで伝えない。アイコンと文言も付く）。
 */
export function LabelEditor({
  idPrefix,
  initial,
  errorTypes,
  saving,
  error,
  submitLabel,
  onSubmit,
}: {
  idPrefix: string;
  initial: LabelDraft;
  errorTypes: readonly CalibrationErrorType[];
  saving: boolean;
  error: string | null;
  submitLabel: string;
  onSubmit: (draft: LabelDraft) => void;
}) {
  const [draft, setDraft] = useState<LabelDraft>(initial);
  const [labelError, setLabelError] = useState<string | null>(null);
  const options = errorTypes.map((item) => ({
    value: item.id,
    label: t(`evaluation.calibration.errorType.${item.id}` as I18nKey),
    description: item.title,
  }));
  return (
    <form
      className="grid min-w-0 gap-4"
      noValidate
      aria-label={t("evaluation.calibration.editor.title")}
      onSubmit={(event) => {
        event.preventDefault();
        if (!draft.label) {
          setLabelError(t("evaluation.calibration.editor.labelRequired"));
          return;
        }
        onSubmit(draft);
      }}
    >
      <Fieldset
        id={`${idPrefix}-label`}
        legend={t("evaluation.calibration.editor.label")}
        required
        error={labelError}
      >
        <div className="flex min-w-0 flex-wrap gap-2">
          {CALIBRATION_LABELS.map((label) => {
            const selected = draft.label === label;
            return (
              <Button
                key={label}
                type="button"
                variant="secondary"
                icon={LABEL_ICON[label]}
                pressed={selected}
                data-testid={`${idPrefix}-label-${label}`}
                onClick={() => {
                  setDraft((current) => ({ ...current, label }));
                  setLabelError(null);
                }}
              >
                {t(`evaluation.calibration.label.${label}` as I18nKey)}
              </Button>
            );
          })}
        </div>
        <p className="text-xs text-fg-muted">
          {t("evaluation.calibration.editor.labelHelp")}
        </p>
      </Fieldset>
      <SelectField<string>
        id={`${idPrefix}-error-type`}
        label={t("evaluation.calibration.editor.errorType")}
        value={draft.errorType}
        options={options}
        emptyOptionLabel={t("evaluation.calibration.editor.errorTypeNone")}
        placeholder={t("evaluation.calibration.editor.errorTypeNone")}
        helper={t("evaluation.calibration.editor.errorTypeHelp")}
        onValueChange={(value) =>
          setDraft((current) => ({ ...current, errorType: value }))
        }
        width="lg"
      />
      <TextareaField
        id={`${idPrefix}-note`}
        label={t("evaluation.calibration.editor.note")}
        helper={t("evaluation.calibration.editor.noteHelp")}
        value={draft.note}
        maxLength={2000}
        rows={2}
        width="lg"
        onValueChange={(value) =>
          setDraft((current) => ({ ...current, note: value }))
        }
      />
      {error ? <Banner severity="danger">{error}</Banner> : null}
      <div>
        <Button
          type="submit"
          icon={Save}
          loading={saving}
          data-testid={`${idPrefix}-save`}
        >
          {submitLabel}
        </Button>
      </div>
    </form>
  );
}
