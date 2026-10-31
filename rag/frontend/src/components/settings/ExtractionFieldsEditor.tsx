import {
  Button,
  FormSkeleton,
  FormStatus,
  SelectField,
  type SelectFieldOption,
  TextField,
  TimedLoadingState,
} from "@production-ready/ui";
import type { UseMutationResult } from "@tanstack/react-query";
import { Plus, RotateCcw, Save, Trash2, Undo2 } from "lucide-react";
import { useState, type ReactNode } from "react";

import { ApiErrorState } from "@/components/StateViews";
import { useConfirm } from "@/components/ui/confirm-dialog";
import { ApiError, type ExtractionFieldDefinition, type ExtractionFieldValueType } from "@/lib/api";
import { t } from "@/lib/i18n";
import { useLeaveGuard } from "@/lib/leave-guard";
import {
  useExtractionFieldsSettings,
  useResetExtractionFieldsSettings,
  useUpdateExtractionFieldsSettings,
} from "@/lib/queries";
import { toast } from "@/lib/toast";

import {
  EXTRACTION_FIELD_DESCRIPTION_MAX,
  EXTRACTION_FIELD_NAME_MAX,
  EXTRACTION_FIELD_VALUE_TYPES,
  MAX_EXTRACTION_FIELDS,
  type ExtractionFieldRow,
  definitionsFromRows,
  newExtractionFieldRow,
  rowsFromDefinitions,
  sameDefinitions,
  validateExtractionFieldRows,
} from "./ExtractionFieldsEditor.logic";

const VALUE_TYPE_OPTIONS: SelectFieldOption<ExtractionFieldValueType>[] =
  EXTRACTION_FIELD_VALUE_TYPES.map((value) => ({
    value,
    label: t(`settings.extractionFields.valueType.${value}`),
  }));

/**
 * 項目抽出で取り出す項目の定義（`/api/settings/extraction-fields`）を編集する（#528）。
 * 全体の既定の定義（KB に定義が無い文書に使う。#548）。文書解析の「解析後の処理」の項目抽出の中に置く。
 * 一度も保存していない環境は標準の項目を使い、「標準の項目に戻す」で保存した定義を消せる（#556）。
 */
export function ExtractionFieldsEditor() {
  const query = useExtractionFieldsSettings();
  const save = useUpdateExtractionFieldsSettings();
  const reset = useResetExtractionFieldsSettings();
  const confirm = useConfirm();
  // 標準の項目に戻したら、編集中の内容も捨てて編集欄を作り直す（編集中なら保存値へそろわないため）。
  const [formKey, setFormKey] = useState(0);

  const resetToStandard = async () => {
    const ok = await confirm({
      title: t("settings.extractionFields.resetStandard.title"),
      description: t("settings.extractionFields.resetStandard.description"),
      confirmLabel: t("settings.extractionFields.resetStandard"),
      tone: "warning",
    });
    if (!ok) return;
    save.reset();
    reset.mutate(undefined, {
      onSuccess: () => {
        setFormKey((key) => key + 1);
        toast.success(t("settings.extractionFields.resetStandard.done"));
      },
      onError: (error) =>
        toast.error(
          error instanceof ApiError
            ? error.message
            : t("settings.extractionFields.resetStandard.error")
        ),
    });
  };

  if (query.isPending) {
    // ほかの読み込み中と同じく、先頭に文言と経過時間の行、その下に内容の形の Skeleton（#2510）。
    return (
      <TimedLoadingState
        label={t("settings.extractionFields.loading")}
        operationKey="extraction-fields-load"
        framed={false}
        testId="extraction-fields-loading"
      >
        <FormSkeleton fields={2} />
      </TimedLoadingState>
    );
  }
  if (query.isError || !query.data) {
    return (
      <ApiErrorState
        error={query.error}
        fallback={t("settings.extractionFields.loadError")}
        onRetry={() => void query.refetch()}
      />
    );
  }
  const usesStandard = query.data.uses_standard;
  return (
    <ExtractionFieldsForm
      key={formKey}
      saved={query.data.fields}
      save={save}
      description={t("settings.extractionFields.description")}
      notice={
        usesStandard ? (
          <FormStatus
            tone="info"
            className="text-xs"
            message={t("settings.extractionFields.usingStandard")}
          />
        ) : null
      }
      extraActions={
        <Button
          type="button"
          variant="secondary"
          icon={Undo2}
          loading={reset.isPending}
          // 標準の項目を使っている間は戻すものが無い（編集中の変更は「変更を破棄」で戻す）。
          disabled={usesStandard || save.isPending}
          onClick={() => void resetToStandard()}
          className="w-full sm:w-auto"
        >
          {t("settings.extractionFields.resetStandard")}
        </Button>
      }
    />
  );
}

/** 保存の mutation。保存した定義（`fields`）を返す。全体の既定と KB の定義（#548）で共通。 */
export type ExtractionFieldsSaveMutation = UseMutationResult<
  { fields: ExtractionFieldDefinition[] },
  Error,
  ExtractionFieldDefinition[]
>;

/** 項目の定義の編集欄。保存先は `save` が決める（全体の既定か KB の定義）。 */
export function ExtractionFieldsForm({
  saved,
  save,
  description,
  notice,
  extraActions,
  testId = "extraction-fields-editor",
}: {
  saved: ExtractionFieldDefinition[];
  save: ExtractionFieldsSaveMutation;
  description: string;
  /** 説明の下に出す定義の状態（全体の既定の「標準の項目を使っています」など）。 */
  notice?: ReactNode;
  /** 保存・破棄の後ろに並べる操作（KB の「全体の既定に戻す」など）。 */
  extraActions?: ReactNode;
  testId?: string;
}) {
  const [rows, setRows] = useState<ExtractionFieldRow[]>(() => rowsFromDefinitions(saved));
  // 編集欄が基にした保存値。保存値が変わったレンダーで、未編集なら編集欄を保存値へそろえる
  // （編集中の内容は背景の再取得で上書きしない。UX 契約 workspace-state）。
  const [base, setBase] = useState(saved);
  const [attempted, setAttempted] = useState(false);
  if (saved !== base) {
    setBase(saved);
    if (sameDefinitions(base, rows)) setRows(rowsFromDefinitions(saved));
  }
  const dirty = !sameDefinitions(saved, rows);
  useLeaveGuard(dirty);
  const errors = validateExtractionFieldRows(rows);
  const visibleErrors = attempted ? errors : {};
  const atLimit = rows.length >= MAX_EXTRACTION_FIELDS;

  function edit(key: string, update: Partial<ExtractionFieldDefinition>) {
    save.reset();
    setRows((current) => current.map((row) => (row.key === key ? { ...row, ...update } : row)));
  }

  function add() {
    save.reset();
    const row = newExtractionFieldRow();
    setRows((current) => [...current, row]);
    // 追加した行の項目名へフォーカスを移す（描画の後）。
    requestAnimationFrame(() => document.getElementById(`${row.key}-name`)?.focus());
  }

  function remove(key: string) {
    save.reset();
    setRows((current) => current.filter((row) => row.key !== key));
  }

  function discard() {
    save.reset();
    setAttempted(false);
    setRows(rowsFromDefinitions(saved));
  }

  function submit() {
    setAttempted(true);
    const firstError = rows.find((row) => errors[row.key]);
    if (firstError) {
      document.getElementById(`${firstError.key}-name`)?.focus();
      return;
    }
    save.mutate(definitionsFromRows(rows), {
      onSuccess: (data) => {
        setAttempted(false);
        setBase(data.fields);
        setRows(rowsFromDefinitions(data.fields));
      },
    });
  }

  return (
    <div className="space-y-3" data-testid={testId}>
      <p className="text-xs leading-relaxed text-fg-muted">{description}</p>
      {notice}
      {rows.length === 0 ? (
        <p className="rounded-md border border-dashed border-border px-3 py-4 text-sm text-fg-muted">
          {t("settings.extractionFields.none")}
        </p>
      ) : (
        // 表形式の定義の行は全幅。名前は md・型は sm を保ち、説明に残りの幅を使う。
        // 1 行に 1 項目の欄を並べる表の形の編集の行なので、説明の欄（残りの幅）は欄の幅の段の検査
        // （findFieldsOffWidthScale）の対象から外す（表の中の欄と同じ扱い）。
          <ol className="space-y-2" data-field-width-exempt="">
            {rows.map((row, index) => {
              const error = visibleErrors[row.key];
              const rowLabel = t("settings.extractionFields.rowLabel", { index: index + 1 });
              return (
                <li
                  key={row.key}
                  aria-label={rowLabel}
                  className="grid min-w-0 grid-cols-1 gap-3 rounded-md border border-border bg-surface p-3 xl:grid-cols-[minmax(0,20rem)_minmax(0,1fr)_12rem_auto] xl:items-start"
                >
                  <TextField
                    id={`${row.key}-name`}
                    width="md"
                    label={t("settings.extractionFields.name")}
                    required
                    value={row.name}
                    maxLength={EXTRACTION_FIELD_NAME_MAX}
                    autoComplete="off"
                    disabled={save.isPending}
                    error={error ? t(`settings.extractionFields.error.${error}`) : undefined}
                    onValueChange={(name) => edit(row.key, { name })}
                  />
                  <TextField
                    id={`${row.key}-description`}
                    width="full"
                    label={t("settings.extractionFields.fieldDescription")}
                    value={row.description}
                    maxLength={EXTRACTION_FIELD_DESCRIPTION_MAX}
                    autoComplete="off"
                    disabled={save.isPending}
                    onValueChange={(description) => edit(row.key, { description })}
                  />
                  <SelectField
                    id={`${row.key}-value-type`}
                    width="sm"
                    label={t("settings.extractionFields.valueType")}
                    value={row.value_type}
                    options={VALUE_TYPE_OPTIONS}
                    onValueChange={(value_type) => edit(row.key, { value_type })}
                  />
                  {/* 入力欄のラベルの高さの分だけ下げ、入力欄と同じ行にそろえる（xl 以上）。 */}
                  <div className="flex justify-end xl:pt-6">
                    <Button
                      type="button"
                      variant="ghost"
                      tone="danger"
                      iconOnly
                      icon={Trash2}
                      aria-label={t("settings.extractionFields.remove", { index: index + 1 })}
                      disabled={save.isPending}
                      onClick={() => remove(row.key)}
                    />
                  </div>
                </li>
              );
            })}
          </ol>
      )}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          variant="secondary"
          icon={Plus}
          disabled={atLimit || save.isPending}
          onClick={add}
        >
          {t("settings.extractionFields.add")}
        </Button>
        {atLimit ? (
          <span className="text-xs text-fg-muted">
            {t("settings.extractionFields.limit", { max: MAX_EXTRACTION_FIELDS })}
          </span>
        ) : null}
      </div>
      <div className="flex flex-col gap-2 border-t border-border pt-3 sm:flex-row sm:flex-wrap sm:items-center">
        <Button
          type="button"
          icon={Save}
          loading={save.isPending}
          disabled={!dirty}
          onClick={submit}
          className="w-full sm:w-auto"
        >
          {t("settings.extractionFields.save")}
        </Button>
        <Button
          type="button"
          variant="secondary"
          icon={RotateCcw}
          disabled={!dirty || save.isPending}
          onClick={discard}
          className="w-full sm:w-auto"
        >
          {t("settings.extractionFields.discard")}
        </Button>
        {extraActions}
        {save.isSuccess && !dirty ? (
          <FormStatus tone="success" message={t("settings.extractionFields.saved")} />
        ) : null}
        {save.isError ? (
          <FormStatus
            tone="danger"
            message={
              save.error instanceof ApiError
                ? save.error.message
                : t("settings.extractionFields.saveError")
            }
          />
        ) : null}
      </div>
    </div>
  );
}
