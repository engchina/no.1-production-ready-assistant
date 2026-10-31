import {
  Banner,
  Button,
  DataTable,
  FieldError,
  FieldLabel,
  FormStatus,
  INFORMATION_TABLE_ROW_CLASS,
  INFORMATION_TABLE_VISIBLE_ROWS,
  PAGE_SIZE_OPTIONS,
  PagedDataTable,
  ProcessingIndicator,
} from "@production-ready/ui";
import { Download, FileSpreadsheet } from "lucide-react";
import { useId, useRef, useState, type DragEvent } from "react";

import type { EvaluationCase, EvaluationImportIssue, EvaluationImportResult, EvaluationImportRow } from "@/lib/api";
import { t } from "@/lib/i18n";
import { useClientListPaging } from "@/lib/list-paging";
import { paginationLabels } from "@/lib/pagination-labels";
import { cn } from "@/lib/utils";

import { evaluationImportCounts, evaluationImportIssueLocation } from "./evaluation-input";

/** 評価リクエストの Excel のアップロードの欄の id（実行の検証でここへフォーカスを移す）。 */
export const EVALUATION_UPLOAD_ID = "evaluation-request-upload";
export const EVALUATION_UPLOAD_ERROR_ID = "evaluation-request-upload-error";

const XLSX_ACCEPT = ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

/** 選んだファイルの検証（1 つ・.xlsx）。問題が無ければ null。サイズ・中身は backend が確かめる。 */
export function evaluationUploadSelectionError(files: readonly File[]): string | null {
  if (files.length !== 1) return t("evaluation.import.multiple");
  return files[0]?.name.toLowerCase().endsWith(".xlsx") ? null : t("evaluation.import.invalidType");
}

/**
 * 評価リクエストの Excel（#2065）: テンプレートのダウンロード、ファイルの選択（ドラッグ＆ドロップ・クリック・
 * キーボード）、検証中・検証の誤り（行・列・内容）・読み込んだケースのプレビューと件数。
 * 検証はファイルを選んだ時点で backend が行い（保存しない）、結果は親が持つ。
 */
export function EvaluationRequestUpload({
  fileName,
  result,
  validating,
  disabled,
  error,
  notice,
  maxRows,
  maxMegabytes,
  templateDownloading,
  templateError,
  onFiles,
  onDownloadTemplate,
}: {
  /** 選んだファイルの名前（未選択は null）。 */
  fileName: string | null;
  result: EvaluationImportResult | null;
  validating: boolean;
  disabled: boolean;
  /** 欄の直下に出す誤り（未選択・形式・通信の失敗）。 */
  error: string | null;
  /** 検証の前提の案内（ナレッジベースが未選択・ナレッジベースを変えた）。 */
  notice: string | null;
  maxRows: number;
  maxMegabytes: number;
  templateDownloading: boolean;
  templateError: string | null;
  onFiles: (files: File[]) => void;
  onDownloadTemplate: () => void;
}) {
  const labelId = useId();
  const hintId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);
  const open = () => {
    if (!disabled) inputRef.current?.click();
  };
  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragOver(false);
    if (disabled) return;
    const files = Array.from(event.dataTransfer.files ?? []);
    if (files.length) onFiles(files);
  };

  return (
    <div className="grid min-w-0 gap-3" data-testid="evaluation-request-upload">
      <div className="grid min-w-0 gap-1">
        {/* 選ぶ部品は role="button" の領域で、ネイティブの file 入力は隠す（ラベルは aria-labelledby で結び、
            ラベルを押しても選べるよう htmlFor は file 入力に向ける）。
            aria-required を持てないので、必須のタグは読み上げ対象に残す（README §4「必須の表示」）。 */}
        <FieldLabel
          id={labelId}
          htmlFor={`${EVALUATION_UPLOAD_ID}-input`}
          label={t("evaluation.import.label")}
          required
          requiredAnnouncedByControl={false}
        />
        <p id={hintId} className="text-xs text-fg-muted">
          {t("evaluation.input.description")}
        </p>
      </div>
      <div
        id={EVALUATION_UPLOAD_ID}
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-labelledby={`${labelId} ${EVALUATION_UPLOAD_ID}-text ${EVALUATION_UPLOAD_ID}-hint`}
        aria-describedby={error ? `${hintId} ${EVALUATION_UPLOAD_ERROR_ID}` : hintId}
        aria-invalid={error ? true : undefined}
        aria-disabled={disabled || undefined}
        data-surface-tint={dragOver ? "info" : undefined}
        data-testid="evaluation-request-dropzone"
        onClick={open}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            open();
          }
        }}
        onDragOver={(event) => {
          event.preventDefault();
          if (!disabled) setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        className={cn(
          "flex min-w-0 cursor-pointer flex-col items-center justify-center gap-1.5 rounded-lg border-2 border-dashed bg-surface px-4 py-5 text-center transition-colors",
          error
            ? "border-danger-border"
            : dragOver
              ? "border-accent-emphasis bg-info-subtle"
              : "border-border hover:border-accent-emphasis",
          disabled && "cursor-not-allowed opacity-60"
        )}
      >
        <FileSpreadsheet size={24} className="text-accent-fg" aria-hidden />
        <p id={`${EVALUATION_UPLOAD_ID}-text`} className="break-all text-sm font-medium text-fg">
          {fileName
            ? t("evaluation.import.dropzoneReplace", { name: fileName })
            : t("evaluation.import.dropzone")}
        </p>
        <p id={`${EVALUATION_UPLOAD_ID}-hint`} className="text-xs text-fg-muted">
          {t("evaluation.import.dropzoneHint", { rows: maxRows, size: maxMegabytes })}
        </p>
        <input
          ref={inputRef}
          id={`${EVALUATION_UPLOAD_ID}-input`}
          type="file"
          className="hidden"
          accept={XLSX_ACCEPT}
          disabled={disabled}
          tabIndex={-1}
          aria-hidden="true"
          data-testid="evaluation-request-file"
          onChange={(event) => {
            const files = Array.from(event.target.files ?? []);
            if (files.length) onFiles(files);
            event.target.value = "";
          }}
        />
      </div>
      <FieldError id={EVALUATION_UPLOAD_ERROR_ID} message={error} />

      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
        <Button
          type="button"
          size="sm"
          variant="secondary"
          icon={Download}
          loading={templateDownloading}
          onClick={onDownloadTemplate}
        >
          {t("evaluation.import.template")}
        </Button>
        <span className="text-xs text-fg-muted">{t("evaluation.import.templateHint")}</span>
      </div>
      {templateError ? <FormStatus tone="danger" message={templateError} /> : null}

      {notice ? <Banner severity="info">{notice}</Banner> : null}
      {validating ? (
        // ファイルの検証（構造・ファイル名の照合）の間。結果はこの欄の直下に出す（messaging.md §10）。
        <ProcessingIndicator
          active
          label={t("evaluation.import.validating")}
          operationKey="evaluation-import"
          placement="action"
          testId="evaluation-import-processing"
        />
      ) : result ? (
        <EvaluationImportSummary result={result} />
      ) : null}
    </div>
  );
}

/** 誤り・警告の行・列・内容の表。 */
function EvaluationImportIssueTable({
  issues,
  label,
}: {
  issues: readonly EvaluationImportIssue[];
  label: string;
}) {
  return (
    <DataTable<EvaluationImportIssue & { key: string }>
      ariaLabel={label}
      rows={issues.map((issue, index) => ({ ...issue, key: `${index}` }))}
      getRowKey={(issue) => issue.key}
      rowProps={() => ({ className: INFORMATION_TABLE_ROW_CLASS })}
      stickyHeader
      visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
      scrollAriaLabel={label}
      tableClassName="w-full text-sm"
      columns={[
        {
          key: "location",
          header: t("evaluation.import.column.location"),
          rowHeader: true,
          className: "font-medium text-fg md:whitespace-nowrap",
          render: (issue) => evaluationImportIssueLocation(issue),
        },
        {
          key: "message",
          header: t("evaluation.import.column.message"),
          className: "break-words text-fg",
          render: (issue) => issue.message,
        },
      ]}
    />
  );
}

/**
 * 検証の結果。誤りがあれば行・列・内容の表、無ければ件数と読み込んだケースのプレビュー。
 * 警告（#2086。形式に合わない位置・同じ名前の文書）は、評価できる状態のまま表で出す。
 */
function EvaluationImportSummary({ result }: { result: EvaluationImportResult }) {
  if (result.error_count > 0) {
    return (
      <div className="grid min-w-0 gap-2" data-testid="evaluation-import-errors">
        <Banner
          severity="danger"
          title={t("evaluation.import.errorsTitle", { file: result.file_name, count: result.error_count })}
        >
          <p>{t("evaluation.import.fixErrors")}</p>
          {result.errors.length < result.error_count ? (
            <p>
              {t("evaluation.import.errorsTruncated", {
                shown: result.errors.length,
                count: result.error_count,
              })}
            </p>
          ) : null}
        </Banner>
        <EvaluationImportIssueTable issues={result.errors} label={t("evaluation.import.errorsLabel")} />
      </div>
    );
  }
  const counts = evaluationImportCounts(result);
  const warnings = result.warnings ?? [];
  const warningCount = result.warning_count ?? warnings.length;
  return (
    <div className="grid min-w-0 gap-2" data-testid="evaluation-import-ready">
      <div className="grid gap-0.5">
        <FormStatus
          tone="success"
          message={t("evaluation.import.ready", { file: result.file_name, total: counts.total })}
        />
        <p className="tnum text-xs text-fg-muted">{t("evaluation.import.readyDetail", counts)}</p>
      </div>
      {warningCount > 0 ? (
        <div className="grid min-w-0 gap-2" data-testid="evaluation-import-warnings">
          <Banner
            severity="warning"
            title={t("evaluation.import.warningsTitle", { file: result.file_name, count: warningCount })}
          >
            <p>{t("evaluation.import.warningsHint")}</p>
            {warnings.length < warningCount ? (
              <p>
                {t("evaluation.import.errorsTruncated", { shown: warnings.length, count: warningCount })}
              </p>
            ) : null}
          </Banner>
          <EvaluationImportIssueTable issues={warnings} label={t("evaluation.import.warningsLabel")} />
        </div>
      ) : null}
      <EvaluationImportPreview rows={result.rows} cases={result.cases} />
    </div>
  );
}

function EvaluationImportPreview({ rows, cases }: { rows: EvaluationImportRow[]; cases: EvaluationCase[] }) {
  const answers = new Map(cases.map(item => [item.id, item.standard_answer]));
  const paging = useClientListPaging(
    "evaluation.previewPaging",
    `${rows.length}:${rows[0]?.case_id ?? ""}:${rows.at(-1)?.case_id ?? ""}`
  );
  return (
    <PagedDataTable<EvaluationImportRow>
      viewport="bounded"
      paginationLabels={paginationLabels()}
      {...paging}
      pageSizeOptions={PAGE_SIZE_OPTIONS}
      paginationAriaLabel={t("evaluation.import.paginationLabel")}
      rows={rows}
      getRowKey={(row) => row.case_id}
      scrollAriaLabel={t("evaluation.import.previewLabel")}
      scrollTestId="evaluation-import-preview"
      paginationTestId="evaluation-import-pagination"
      tableClassName="w-full min-w-[90rem] text-sm"
      columns={[
        {
          key: "row",
          header: t("evaluation.import.column.row"),
          className: "tnum whitespace-nowrap text-fg-muted",
          render: (row) => row.row,
        },
        {
          key: "question",
          header: t("evaluation.import.column.question"),
          rowHeader: true,
          className: "min-w-[10rem] break-words font-medium text-fg",
          render: (row) => row.question,
        },
        {
          key: "evidence",
          header: t("evaluation.import.column.evidence"),
          className: "min-w-[12rem] break-words text-xs text-fg-muted",
          render: (row) =>
            row.evidence.length ? (
              <ul className="grid gap-0.5">
                {row.evidence.map((line, index) => (
                  <li key={`${index}:${line}`} className="break-all">
                    {line}
                  </li>
                ))}
              </ul>
            ) : (
              t("evaluation.import.noEvidenceUnanswerable")
            ),
        },
        {
          key: "standard_answer",
          header: t("evaluation.import.column.standardAnswer"),
          className: "min-w-[16rem] whitespace-pre-wrap break-words text-xs text-fg-muted",
          render: row => answers.get(row.case_id) ?? row.standard_answer_preview ?? "—",
        },
        {
          key: "screens",
          header: t("evaluation.import.column.screens"),
          className: "min-w-[12rem] whitespace-pre-wrap break-words text-xs text-fg-muted",
          render: row => row.required_answer_points?.join("\n") || "—",
        },
        {
          key: "expected_response_decision",
          header: t("evaluation.import.column.decision"),
          render: (row) => row.expected_response_decision ? t(`evaluation.decision.${row.expected_response_decision}`) : t("evaluation.metric.unassessed"),
        },
        {
          key: "answer_process",
          header: t("evaluation.import.column.process"),
          className: "min-w-[16rem] whitespace-pre-wrap break-words text-xs text-fg-muted",
          render: row => row.answer_process || "—",
        },
      ]}
    />
  );
}
