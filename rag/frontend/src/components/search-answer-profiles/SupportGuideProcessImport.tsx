import { Sparkles, Upload } from "lucide-react";
import { useState } from "react";

import {
  Banner,
  Button,
  Disclosure,
  FieldError,
  FieldLabel,
  FormGrid,
  FormStatus,
  ProcessingIndicator,
  SelectField,
  StatusBadge,
  TextareaField,
  TextField,
  ToggleChip,
  toast,
} from "@production-ready/ui";

import {
  api,
  ApiError,
  type SupportGuideProcessItem,
  type SupportGuideProcessPreviewData,
  type SupportGuideProcessReference,
  type SupportGuideProcessRow,
  type SupportGuideProcessWorkbookData,
} from "@/lib/api";
import { t } from "@/lib/i18n";
import { useLeaveGuard } from "@/lib/leave-guard";
import { useEditRuntimeKnowledge, useImportSupportGuides } from "@/lib/queries";

import { SupportGuideContentView } from "./SupportGuideContentView";
import { SupportGuideIssueList } from "./SupportGuideIssueList";

type Source = "excel" | "text";

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}

const REFERENCE_VARIANT = {
  resolved: "success",
  unresolved: "danger",
  ambiguous: "warning",
} as const;

/** ルールの候補の選択の key（行とその中の位置）。 */
function ruleKey(row: number, index: number): string {
  return `${row}:${index}`;
}

/**
 * 回答の過程を書いた表（Excel の列を選ぶ・テキストの貼り付け）から、AI で業務ガイドの下書きとルールの候補を作る
 * （#2397）。行ごとの下書き・資料の対応・ルールの候補を確かめ、選んだものだけを下書き（ルールは無効のルール）にする。
 */
export function SupportGuideProcessImport({
  searchAnswerProfileId,
  onDone,
}: {
  searchAnswerProfileId: string;
  onDone: () => void;
}) {
  const [source, setSource] = useState<Source>("excel");
  const [file, setFile] = useState<File | null>(null);
  const [workbook, setWorkbook] = useState<SupportGuideProcessWorkbookData | null>(null);
  const [sheet, setSheet] = useState("");
  const [questionColumn, setQuestionColumn] = useState("");
  const [processColumn, setProcessColumn] = useState("");
  const [answerColumn, setAnswerColumn] = useState("");
  const [startRow, setStartRow] = useState("2");
  const [question, setQuestion] = useState("");
  const [processText, setProcessText] = useState("");
  const [standardAnswer, setStandardAnswer] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [reading, setReading] = useState(false);
  const [drafting, setDrafting] = useState(false);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [readNotes, setReadNotes] = useState<SupportGuideProcessWorkbookData | null>(null);
  const [preview, setPreview] = useState<SupportGuideProcessPreviewData | null>(null);
  const [selectedRows, setSelectedRows] = useState<Set<number>>(new Set());
  const [selectedRules, setSelectedRules] = useState<Set<string>>(new Set());
  const [saving, setSaving] = useState(false);
  const importGuides = useImportSupportGuides(searchAnswerProfileId);
  const editKnowledge = useEditRuntimeKnowledge(searchAnswerProfileId);
  useLeaveGuard(Boolean(file || processText.trim() || preview), saving);

  const currentSheet = workbook?.sheets.find((item) => item.name === sheet) ?? null;
  const columnOptions = (currentSheet?.columns ?? []).map((column) => ({
    value: String(column.index),
    label: column.header
      ? t("supportGuides.processImport.columnLabel", { letter: column.letter, header: column.header })
      : column.letter,
  }));

  const readFile = async (next: File | null) => {
    setFile(next);
    setWorkbook(null);
    setPreview(null);
    setReadNotes(null);
    setRequestError(null);
    setQuestionColumn("");
    setProcessColumn("");
    setAnswerColumn("");
    if (!next) return;
    setReading(true);
    try {
      const data = await api.readSupportGuideProcessWorkbook(searchAnswerProfileId, next);
      setWorkbook(data);
      setSheet(data.sheets[0]?.name ?? "");
    } catch (error) {
      setRequestError(errorMessage(error, t("supportGuides.processImport.readError")));
    } finally {
      setReading(false);
    }
  };

  const validate = (): Record<string, string> => {
    const errors: Record<string, string> = {};
    if (source === "excel") {
      if (!file || !workbook) errors.file = t("supportGuides.processImport.fileRequired");
      if (!questionColumn) errors.question = t("supportGuides.processImport.columnRequired");
      if (!processColumn) errors.process = t("supportGuides.processImport.columnRequired");
      const start = Number(startRow);
      if (!Number.isInteger(start) || start < 2) errors.start = t("supportGuides.processImport.startRowInvalid");
    } else {
      if (!question.trim()) errors.textQuestion = t("supportGuides.processImport.questionRequired");
      if (!processText.trim()) errors.textProcess = t("supportGuides.processImport.processRequired");
    }
    return errors;
  };

  const loadRows = async (): Promise<SupportGuideProcessRow[] | null> => {
    if (source === "text") {
      return [{ row: 1, question: question.trim(), process: processText.trim(), standard_answer: standardAnswer.trim() }];
    }
    if (!file) return null;
    const data = await api.readSupportGuideProcessWorkbook(searchAnswerProfileId, file, {
      sheet,
      question_column: Number(questionColumn),
      process_column: Number(processColumn),
      answer_column: answerColumn ? Number(answerColumn) : null,
      start_row: Number(startRow),
    });
    setReadNotes(data.issues.length > 0 || data.truncated ? data : null);
    if (data.rows.length === 0) {
      setRequestError(t("supportGuides.processImport.noRows"));
      return null;
    }
    return data.rows;
  };

  const runDraft = async () => {
    const errors = validate();
    setFieldErrors(errors);
    setRequestError(null);
    if (Object.keys(errors).length > 0) return;
    setPreview(null);
    setReadNotes(null);
    setDrafting(true);
    try {
      const rows = await loadRows();
      if (!rows) return;
      const data = await api.previewSupportGuideProcessImport(searchAnswerProfileId, rows);
      setPreview(data);
      setSelectedRows(new Set(data.items.filter((item) => item.valid).map((item) => item.row)));
      setSelectedRules(new Set());
    } catch (error) {
      setRequestError(errorMessage(error, t("supportGuides.processImport.draftError")));
    } finally {
      setDrafting(false);
    }
  };

  const toggle = <T,>(set: Set<T>, value: T, checked: boolean): Set<T> => {
    const next = new Set(set);
    if (checked) next.add(value);
    else next.delete(value);
    return next;
  };

  const save = async () => {
    if (!preview) return;
    const guides = preview.items
      .filter((item) => item.valid && item.content && selectedRows.has(item.row))
      .map((item) => item.content);
    const rules = preview.items.flatMap((item) =>
      item.rule_candidates
        .map((rule, index) => ({ rule, key: ruleKey(item.row, index) }))
        .filter(({ key }) => selectedRules.has(key))
        .map(({ rule }) => rule),
    );
    setRequestError(null);
    setSaving(true);
    try {
      let created = 0;
      if (guides.length > 0) {
        created = (await importGuides.mutateAsync(guides)).created.length;
      }
      // ルールの候補は無効のルールとして足す（回答ルールの画面で確かめてから有効にする）。
      for (const rule of rules) {
        await editKnowledge.mutateAsync({
          kind: "rules",
          selected: null,
          title: rule.title,
          labels: rule.triggers.join("\n"),
          content: rule.content,
          enabled: false,
        });
      }
      toast.success(t("supportGuides.processImport.saved", { guides: created, rules: rules.length }));
      onDone();
    } catch (error) {
      setRequestError(errorMessage(error, t("supportGuides.processImport.saveError")));
    } finally {
      setSaving(false);
    }
  };

  const busy = reading || drafting || saving;
  const selectedCount = selectedRows.size + selectedRules.size;

  return (
    <div className="min-w-0 space-y-3" data-testid="support-guide-process-import">
      <p className="text-xs leading-relaxed text-fg-muted">{t("supportGuides.processImport.hint")}</p>
      <div
        className="flex flex-wrap gap-1"
        role="group"
        aria-label={t("supportGuides.processImport.source")}
      >
        {(["excel", "text"] as const).map((item) => (
          <ToggleChip
            key={item}
            selected={source === item}
            disabled={busy}
            onClick={() => {
              setSource(item);
              setFieldErrors({});
              setRequestError(null);
            }}
          >
            {t(`supportGuides.processImport.source.${item}`)}
          </ToggleChip>
        ))}
      </div>
      <FormGrid columns={1}>
        {source === "excel" ? (
          <>
            <div className="space-y-1.5">
              <FieldLabel
                htmlFor="support-guide-process-file"
                label={t("supportGuides.processImport.file")}
                required
                className="block"
              />
              <input
                id="support-guide-process-file"
                type="file"
                accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                aria-required
                aria-describedby={fieldErrors.file ? "support-guide-process-file-error" : undefined}
                disabled={busy}
                onChange={(event) => void readFile(event.target.files?.[0] ?? null)}
                className="block w-full text-sm text-fg file:mr-3 file:rounded-md file:border file:border-border-control file:bg-surface file:px-3 file:py-1.5 file:text-sm"
              />
              <FieldError id="support-guide-process-file-error" message={fieldErrors.file} />
            </div>
            {reading ? (
              <ProcessingIndicator
                active
                label={t("supportGuides.processImport.reading")}
                operationKey="support-guide-process-read"
                placement="action"
              />
            ) : null}
            {workbook ? (
              <>
                <SelectField
                  id="support-guide-process-sheet"
                  label={t("supportGuides.processImport.sheet")}
                  width="md"
                  required
                  value={sheet}
                  options={workbook.sheets.map((item) => ({
                    value: item.name,
                    label: t("supportGuides.processImport.sheetLabel", { name: item.name, count: item.row_count }),
                  }))}
                  disabled={busy}
                  onValueChange={(value) => {
                    setSheet(value);
                    setQuestionColumn("");
                    setProcessColumn("");
                    setAnswerColumn("");
                  }}
                />
                <SelectField
                  id="support-guide-process-question-column"
                  label={t("supportGuides.processImport.questionColumn")}
                  width="md"
                  required
                  value={questionColumn}
                  placeholder={t("supportGuides.processImport.columnPlaceholder")}
                  options={columnOptions}
                  error={fieldErrors.question}
                  disabled={busy}
                  onValueChange={setQuestionColumn}
                />
                <SelectField
                  id="support-guide-process-process-column"
                  label={t("supportGuides.processImport.processColumn")}
                  width="md"
                  required
                  value={processColumn}
                  placeholder={t("supportGuides.processImport.columnPlaceholder")}
                  options={columnOptions}
                  error={fieldErrors.process}
                  disabled={busy}
                  onValueChange={setProcessColumn}
                />
                <SelectField
                  id="support-guide-process-answer-column"
                  label={t("supportGuides.processImport.answerColumn")}
                  width="md"
                  value={answerColumn}
                  emptyOptionLabel={t("supportGuides.processImport.noColumn")}
                  options={columnOptions}
                  disabled={busy}
                  onValueChange={setAnswerColumn}
                />
                <TextField
                  id="support-guide-process-start-row"
                  label={t("supportGuides.processImport.startRow")}
                  helper={t("supportGuides.processImport.startRowHelper", { max: workbook.max_rows })}
                  type="number"
                  inputMode="numeric"
                  min={2}
                  width="xs"
                  value={startRow}
                  error={fieldErrors.start}
                  disabled={busy}
                  onChange={(event) => setStartRow(event.target.value)}
                />
              </>
            ) : null}
          </>
        ) : (
          <>
            <TextField
              id="support-guide-process-question"
              label={t("supportGuides.processImport.question")}
              width="full"
              required
              value={question}
              error={fieldErrors.textQuestion}
              disabled={busy}
              onChange={(event) => setQuestion(event.target.value)}
            />
            <TextareaField
              id="support-guide-process-text"
              label={t("supportGuides.processImport.process")}
              width="full"
              rows={8}
              required
              value={processText}
              error={fieldErrors.textProcess}
              disabled={busy}
              onChange={(event) => setProcessText(event.target.value)}
            />
            <TextareaField
              id="support-guide-process-answer"
              label={t("supportGuides.processImport.standardAnswer")}
              width="full"
              rows={3}
              value={standardAnswer}
              disabled={busy}
              onChange={(event) => setStandardAnswer(event.target.value)}
            />
          </>
        )}
      </FormGrid>
      <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
        <Button
          size="sm"
          variant="secondary"
          icon={Sparkles}
          className="w-full sm:w-auto"
          loading={drafting}
          disabled={reading || saving}
          onClick={() => void runDraft()}
        >
          {t("supportGuides.processImport.draft")}
        </Button>
      </div>
      <div aria-live="polite" className="space-y-3" data-testid="support-guide-process-result">
        {requestError ? <FormStatus tone="danger" message={requestError} /> : null}
        {drafting ? (
          <ProcessingIndicator
            active
            label={t("supportGuides.processImport.drafting")}
            operationKey="support-guide-process-draft"
            placement="action"
            activityIcon="none"
          />
        ) : null}
        {readNotes ? (
          <Banner severity="warning">
            <div className="space-y-1">
              {readNotes.truncated && readNotes.next_start_row ? (
                <p>
                  {t("supportGuides.processImport.truncated", {
                    max: readNotes.max_rows,
                    next: readNotes.next_start_row,
                  })}
                </p>
              ) : null}
              {readNotes.issues.length > 0 ? (
                <ul className="list-inside list-disc">
                  {readNotes.issues.map((issue) => (
                    <li key={issue}>{issue}</li>
                  ))}
                </ul>
              ) : null}
            </div>
          </Banner>
        ) : null}
        {preview ? (
          <div className="space-y-3">
            <p className="text-sm text-fg">
              {t("supportGuides.processImport.summary", {
                total: preview.items.length,
                count: preview.importable_count,
              })}
            </p>
            <ul className="space-y-2">
              {preview.items.map((item) => (
                <ProcessItem
                  key={item.row}
                  item={item}
                  selected={selectedRows.has(item.row)}
                  selectedRules={selectedRules}
                  disabled={saving}
                  onSelect={(checked) => setSelectedRows((set) => toggle(set, item.row, checked))}
                  onSelectRule={(key, checked) => setSelectedRules((set) => toggle(set, key, checked))}
                />
              ))}
            </ul>
            <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
              <Button
                size="sm"
                icon={Upload}
                className="w-full sm:w-auto"
                loading={saving}
                disabled={selectedCount === 0 || drafting}
                onClick={() => void save()}
              >
                {t("supportGuides.processImport.save")}
              </Button>
              <span className="text-xs text-fg-muted">{t("supportGuides.processImport.saveHint")}</span>
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

function ReferenceRow({ reference }: { reference: SupportGuideProcessReference }) {
  return (
    <li className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-sm text-fg" data-reference-status={reference.status}>
      <StatusBadge
        variant={REFERENCE_VARIANT[reference.status]}
        label={t(`supportGuides.processImport.reference.${reference.status}`)}
      />
      <span className="min-w-0 break-words">{reference.name}</span>
      {reference.pages ? (
        <span className="text-xs text-fg-muted">{t("supportGuides.processImport.pages", { pages: reference.pages })}</span>
      ) : null}
      {reference.sheet_name || reference.rows || reference.cells ? (
        <span className="min-w-0 break-words text-xs text-fg-muted">
          {t("supportGuides.processImport.sheetLocation", {
            sheet: reference.sheet_name ?? "",
            location: [reference.rows, reference.cells].filter(Boolean).join(" "),
          })}
        </span>
      ) : null}
      {reference.status === "resolved" && reference.file_name ? (
        <span className="min-w-0 break-words text-xs text-fg-muted">
          {t("supportGuides.processImport.resolvedTo", { name: reference.file_name })}
        </span>
      ) : null}
    </li>
  );
}

/** 1 行の下書き（選ぶ・取り込めるか・資料の対応・内容・ルールの候補）。 */
function ProcessItem({
  item,
  selected,
  selectedRules,
  disabled,
  onSelect,
  onSelectRule,
}: {
  item: SupportGuideProcessItem;
  selected: boolean;
  selectedRules: Set<string>;
  disabled: boolean;
  onSelect: (checked: boolean) => void;
  onSelectRule: (key: string, checked: boolean) => void;
}) {
  const title = item.content?.title || t("supportGuides.importPanel.untitled");
  return (
    <li
      className="min-w-0 space-y-2 rounded-md border border-border p-2"
      data-testid={`support-guide-process-item-${item.row}`}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <label className="flex cursor-pointer items-center gap-2 text-sm font-medium text-fg">
          <input
            type="checkbox"
            className="h-4 w-4 rounded border-border accent-accent-emphasis"
            checked={selected && item.valid}
            disabled={disabled || !item.valid}
            onChange={(event) => onSelect(event.target.checked)}
          />
          <span className="min-w-0 break-words">
            {t("supportGuides.processImport.itemTitle", { row: item.row, title })}
          </span>
        </label>
        <StatusBadge
          variant={item.valid ? "success" : "danger"}
          label={t(item.valid ? "supportGuides.importPanel.valid" : "supportGuides.importPanel.invalid")}
        />
      </div>
      <p className="break-words text-xs text-fg-muted">
        {t("supportGuides.processImport.questionOf", { question: item.question })}
      </p>
      <SupportGuideIssueList issues={item.issues} />
      {item.references.length > 0 ? (
        <div className="space-y-1">
          <p className="text-xs font-semibold text-fg">{t("supportGuides.processImport.references")}</p>
          <ul className="space-y-1">
            {item.references.map((reference, index) => (
              <ReferenceRow key={`${reference.name}-${index}`} reference={reference} />
            ))}
          </ul>
        </div>
      ) : null}
      {item.content ? (
        <Disclosure size="sm" variant="plain" summary={t("supportGuides.processImport.content")}>
          <SupportGuideContentView content={item.content} />
        </Disclosure>
      ) : null}
      {item.rule_candidates.length > 0 ? (
        <div className="space-y-1">
          <p className="text-xs font-semibold text-fg">{t("supportGuides.processImport.rules")}</p>
          <ul className="space-y-1.5">
            {item.rule_candidates.map((rule, index) => {
              const key = ruleKey(item.row, index);
              return (
                <li key={key} className="min-w-0 space-y-0.5" data-testid={`support-guide-process-rule-${key}`}>
                  <label className="flex cursor-pointer items-center gap-2 text-sm text-fg">
                    <input
                      type="checkbox"
                      className="h-4 w-4 rounded border-border accent-accent-emphasis"
                      checked={selectedRules.has(key)}
                      disabled={disabled}
                      onChange={(event) => onSelectRule(key, event.target.checked)}
                    />
                    <span className="min-w-0 break-words">{rule.title}</span>
                  </label>
                  <p className="break-words pl-6 text-xs text-fg-muted">{rule.content}</p>
                  {rule.triggers.length > 0 ? (
                    <p className="break-words pl-6 text-xs text-fg-muted">
                      {t("supportGuides.processImport.triggers", { words: rule.triggers.join("、") })}
                    </p>
                  ) : null}
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </li>
  );
}
