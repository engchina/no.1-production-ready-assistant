import { Download, ExternalLink, Maximize2 } from "lucide-react";
import { useMemo, useRef, useState, type ReactNode } from "react";

import { INFORMATION_TABLE_ROW_CLASS, INFORMATION_TABLE_VISIBLE_ROWS } from "../../lib/list-density";
import { cn } from "../../lib/utils";
import { formatChatProgressDuration } from "../feedback/chat-progress";
import { Banner } from "../ui/banner";
import { Button } from "../ui/button";
import { ButtonLink, type ButtonLinkComponent } from "../ui/button-link";
import { SideSheet } from "../ui/side-sheet";
import { DataTable, type DataTableColumn } from "./data-table";
import { PagedDataTable } from "./paged-data-table";
import { DEFAULT_PAGE_SIZE, DEFAULT_PAGINATION_LABELS, type PaginationRange } from "./pagination";

/**
 * ResultTable（#1154 / #1178）— データの結果（読み取りだけの行と列）を出す部品。チャットの回答の吹き出しの中の
 * SQL・ツールの実行の結果と、画面のクエリの結果・テーブルのデータの表示・取り込みのサンプル行に使う。
 *
 * 業界の例（ChatGPT の Advanced Data Analysis・Databricks Genie・Snowflake Cortex Analyst・Amazon Q in
 * QuickSight・BigQuery の結果の表）に倣い、「要約の 1 行 → 先頭の行のプレビュー（表の中で縦横にスクロール）」に
 * 絞り、すべての行は広いシート（ページ送り）と CSV で見る。上限で打ち切った結果は黙って隠さず、要約と案内で
 * 明示する。製品は列・行（取得済みの行）・打ち切りの有無を渡すだけにする。行の操作・選択・編集のある一覧は
 * この部品ではなく従来の一覧の型（`DataTable` / `PagedDataTable`）にする（UX 契約 `page-archetypes.md`）。
 * 以前の名前 `ChatResultTable` は別名として残す（ファイルの末尾）。
 */

/** 結果の列。`type` が `number` の列（または NULL を除く値がすべて数値の列）は右寄せにする。 */
export interface ResultTableColumn {
  name: string;
  type?: string;
}

export interface ResultTableLabels {
  /** 要約: 行数（例「12 行」）。 */
  rows: (count: string) => string;
  /** 要約: 取得の上限で打ち切ったときの行数（例「先頭の 1,000 行を取得しました（さらに行があります）」）。 */
  truncatedRows: (count: string) => string;
  /** 要約: 打ち切ったが総件数が分かるとき（例「先頭の 1,000 行を取得しました（全 1,234 行）」）。 */
  truncatedRowsOfTotal: (count: string, total: string) => string;
  /** 要約: 0 行。 */
  noRows: string;
  /** 要約: 列数（例「8 列」）。 */
  columns: (count: string) => string;
  /** 要約の区切り。 */
  separator: string;
  /** 所要時間の表記。 */
  formatDuration: (ms: number) => string;
  /** 数（行数・列数）の表記（桁区切り）。 */
  formatCount: (value: number) => string;
  /** プレビューより行が多いときの補足。 */
  previewNote: (preview: string, count: string) => string;
  /** 打ち切りの案内（取得の上限。上限が分からないときは `null`）。 */
  truncatedNotice: (limit: string | null) => string;
  /** セルの文字数の上限で値を切ったときの補足。 */
  cellsTruncated: (chars: string | null) => string;
  /** NULL の表示（空文字と区別する）。 */
  nullValue: string;
  viewAll: string;
  /** すべての行のシートの見出し（例「実行結果（60 行）」）。 */
  sheetTitle: (count: string) => string;
  close: string;
  downloadCsv: string;
  /** 表の名前（読み上げ）。 */
  tableLabel: string;
  /** 表のスクロール領域の名前（読み上げ）。 */
  scrollLabel: string;
  pageSize: string;
  pageSizeOption: (size: number) => string;
  pageSummary: (range: PaginationRange) => string;
  pageIndicator: (page: number, totalPages: number) => string;
  prev: string;
  next: string;
}

const countFormatter = new Intl.NumberFormat("ja-JP");

export const DEFAULT_RESULT_TABLE_LABELS: ResultTableLabels = {
  rows: (count) => `${count} 行`,
  truncatedRows: (count) => `先頭の ${count} 行を取得しました（さらに行があります）`,
  truncatedRowsOfTotal: (count, total) => `先頭の ${count} 行を取得しました（全 ${total} 行）`,
  noRows: "該当する行はありません",
  columns: (count) => `${count} 列`,
  separator: "・",
  formatDuration: formatChatProgressDuration,
  formatCount: (value) => countFormatter.format(value),
  previewNote: (preview, count) =>
    `ここでは先頭の ${preview} 行を表示しています。取得した ${count} 行は「すべての行を見る」で確認できます。`,
  truncatedNotice: (limit) =>
    limit
      ? `1 回に取得するのは先頭の ${limit} 行までです。表示と CSV は取得した行だけです。`
      : "取得の上限で打ち切りました。表示と CSV は取得した行だけです。",
  cellsTruncated: (chars) =>
    chars
      ? `${chars} 文字を超える値は、先頭の ${chars} 文字だけを取得しています。`
      : "長い値は先頭だけを取得しています。",
  nullValue: "NULL",
  viewAll: "すべての行を見る",
  sheetTitle: (count) => `実行結果（${count} 行）`,
  close: "実行結果を閉じる",
  downloadCsv: "CSV をダウンロード",
  tableLabel: "実行結果",
  scrollLabel: "実行結果の表。スクロールできます。",
  pageSize: "表示件数",
  pageSizeOption: (size) => `${size} 件`,
  // 件数・ページ・前へ / 次へは一覧の共通の文言と同じ（#1266）。
  pageSummary: DEFAULT_PAGINATION_LABELS.summary,
  pageIndicator: DEFAULT_PAGINATION_LABELS.pageIndicator!,
  prev: DEFAULT_PAGINATION_LABELS.prev,
  next: DEFAULT_PAGINATION_LABELS.next,
};

/** 吹き出しの中のプレビューに描く行の上限（それより多い行は「すべての行を見る」で見る）。 */
export const RESULT_PREVIEW_ROWS = 50;
/** 「すべての行を見る」の 1 ページの行数の選択肢（既定は共通の 25 件）。 */
export const RESULT_PAGE_SIZES = [DEFAULT_PAGE_SIZE, 50, 100] as const;

/**
 * 全件の取得の導線（上限を超える行が要るとき）。`href` と `label` があれば製品の画面へのリンクを出す。
 * リンクを出せない（移動先の画面の権限が無い）ときは `hint` だけを渡す。
 */
export interface ResultFullResultLink {
  href?: string;
  label?: string;
  /** 画面の中の移動（react-router の `Link` など。SQL を履歴の state で渡す等）。 */
  linkComponent?: ButtonLinkComponent;
  /** 案内の文（例「すべての行が必要なときは、〜の画面で実行してください。」）。 */
  hint?: string;
}

export interface ResultTableProps {
  columns: readonly ResultTableColumn[];
  /** 取得した行（列の順の値の配列）。NULL は `null`。 */
  rows: readonly (readonly unknown[])[];
  /** 取得の上限（行数・応答の大きさ）で打ち切ったか（さらに行がある）。 */
  truncated?: boolean;
  /** 総件数が分かるときだけ渡す（打ち切ったときに「全 N 行」と出す）。 */
  totalRowCount?: number | null;
  /** 1 回の取得の行数の上限（打ち切りの案内に出す）。 */
  rowLimit?: number | null;
  /** セルの文字数の上限で値を切ったか。 */
  cellsTruncated?: boolean;
  /** セルの文字数の上限（案内に出す）。 */
  maxCellChars?: number | null;
  /** 所要時間（ミリ秒）。 */
  elapsedMs?: number | null;
  /** 吹き出しの中に描く行の上限（既定 50）。 */
  previewRows?: number;
  /** 「すべての行を見る」の 1 ページの行数の選択肢（既定 25 / 50 / 100）。 */
  pageSizeOptions?: readonly number[];
  /** CSV のファイル名（既定 `result.csv`）。 */
  csvFilename?: string;
  /**
   * CSV の書き出しを製品が行うとき（監査の記録など）。省略時は部品が CSV を作ってダウンロードする。
   * `csv` は BOM・CRLF 付きの RFC 4180 の文字列。
   */
  onDownloadCsv?: (csv: string, filename: string) => void;
  /** CSV をダウンロードした後（Toast など）。 */
  onCsvDownloaded?: () => void;
  /** 上限を超える全件の取得の導線（打ち切ったときだけ出す）。 */
  fullResult?: ResultFullResultLink;
  /** 要約の行の右に足す製品の操作。 */
  actions?: ReactNode;
  /**
   * 要約の文の右に足す画面固有の補足（実行した接続の `StatusBadge` など。#1178）。行数・列数・打ち切りは
   * 部品の要約が出すので、ここで重ねて出さない。
   */
  meta?: ReactNode;
  labels?: Partial<ResultTableLabels>;
  className?: string;
  testId?: string;
}

/** セルの値が NULL か（`null` / `undefined`）。 */
export function isNullResultValue(value: unknown): value is null | undefined {
  return value === null || value === undefined;
}

/** セルの値の文字列（NULL は空文字。オブジェクトは JSON）。 */
export function resultValueText(value: unknown): string {
  if (isNullResultValue(value)) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** 右寄せにする数値の列か（`type` が number、または NULL を除く値がすべて数値で 1 つ以上ある）。 */
export function isNumericResultColumn(
  rows: readonly (readonly unknown[])[],
  index: number,
  type?: string
): boolean {
  if (type) return /^(number|integer|float|double|decimal|numeric|bigint)$/i.test(type);
  let seen = false;
  for (const row of rows) {
    const value = row[index];
    if (isNullResultValue(value)) continue;
    if (typeof value !== "number") return false;
    seen = true;
  }
  return seen;
}

const CSV_FORMULA_PREFIX = /^[=+\-@\t\r]/;

function csvField(value: unknown): string {
  if (isNullResultValue(value)) return "";
  let text = resultValueText(value);
  // 表計算ソフトで式として解釈されないよう、文字列の先頭の = + - @ に ' を付ける（CSV injection。OWASP）。
  if (typeof value === "string" && CSV_FORMULA_PREFIX.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

/** 取得した行を CSV（RFC 4180・CRLF・先頭に BOM。NULL は空の欄）にする。 */
export function resultRowsToCsv(
  columns: readonly ResultTableColumn[],
  rows: readonly (readonly unknown[])[]
): string {
  const lines = [columns.map((column) => csvField(column.name)).join(",")];
  for (const row of rows) lines.push(columns.map((_, index) => csvField(row[index])).join(","));
  return `\uFEFF${lines.join("\r\n")}\r\n`;
}

/** これより長い値は、全行の表でも最小幅と行数の上限を持たせる。 */
const LONG_VALUE_CHARS = 40;

/** セルの表示。NULL は斜体・淡色の「NULL」（色だけに頼らない）、`preview` は 1 行で省略して title に全文。 */
export function ResultCell({
  value,
  variant = "full",
  nullLabel = DEFAULT_RESULT_TABLE_LABELS.nullValue,
}: {
  value: unknown;
  variant?: "preview" | "full";
  nullLabel?: string;
}) {
  if (isNullResultValue(value)) return <span className="italic text-fg-muted">{nullLabel}</span>;
  const text = resultValueText(value);
  if (variant === "preview")
    return (
      <span className="block max-w-[20rem] truncate" title={text}>
        {text}
      </span>
    );
  // 長い値は列が潰れて行が極端に高くならないよう最小幅を持たせ、6 行で省略して title に全文を出す
  // （値は DOM に残るので読み上げ・コピー・CSV では全文）。
  const long = text.length > LONG_VALUE_CHARS;
  return (
    <span
      className={cn("block max-w-[32rem] whitespace-pre-wrap", long && "line-clamp-6 min-w-[16rem]")}
      title={long ? text : undefined}
    >
      {text}
    </span>
  );
}

type IndexedRow = { index: number; values: readonly unknown[] };

function resultColumns(
  columns: readonly ResultTableColumn[],
  rows: readonly (readonly unknown[])[],
  variant: "preview" | "full",
  nullLabel: string
): DataTableColumn<IndexedRow>[] {
  return columns.map((column, index) => {
    const numeric = isNumericResultColumn(rows, index, column.type);
    return {
      key: `${index}:${column.name}`,
      header: column.name,
      align: numeric ? "right" : "left",
      className: numeric ? "tabular-nums" : undefined,
      render: (row) => <ResultCell value={row.values[index]} variant={variant} nullLabel={nullLabel} />,
    };
  });
}

function downloadText(filename: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

/**
 * 要約の 1 行目の文（例「12 行・5 列・0.8 秒」「先頭の 1,000 行を取得しました（さらに行があります）・8 列・1.2 秒」）。
 */
export function resultSummaryText({
  rowCount,
  columnCount,
  truncated,
  totalRowCount,
  elapsedMs,
  labels: overrides,
}: {
  rowCount: number;
  columnCount: number;
  truncated?: boolean;
  totalRowCount?: number | null;
  elapsedMs?: number | null;
  labels?: Partial<ResultTableLabels>;
}): string {
  const labels = { ...DEFAULT_RESULT_TABLE_LABELS, ...overrides };
  const count = labels.formatCount(rowCount);
  const parts = [
    rowCount === 0
      ? labels.noRows
      : truncated
        ? typeof totalRowCount === "number" && totalRowCount > rowCount
          ? labels.truncatedRowsOfTotal(count, labels.formatCount(totalRowCount))
          : labels.truncatedRows(count)
        : labels.rows(count),
    labels.columns(labels.formatCount(columnCount)),
  ];
  if (typeof elapsedMs === "number") parts.push(labels.formatDuration(elapsedMs));
  return parts.join(labels.separator);
}

export function ResultTable({
  columns,
  rows,
  truncated = false,
  totalRowCount,
  rowLimit,
  cellsTruncated = false,
  maxCellChars,
  elapsedMs,
  previewRows = RESULT_PREVIEW_ROWS,
  pageSizeOptions = RESULT_PAGE_SIZES,
  csvFilename = "result.csv",
  onDownloadCsv,
  onCsvDownloaded,
  fullResult,
  actions,
  meta,
  labels: labelOverrides,
  className,
  testId,
}: ResultTableProps) {
  const labels = { ...DEFAULT_RESULT_TABLE_LABELS, ...labelOverrides };
  const [sheetOpen, setSheetOpen] = useState(false);
  const [pageSize, setPageSize] = useState<number>(pageSizeOptions[0] ?? DEFAULT_PAGE_SIZE);
  const viewAllRef = useRef<HTMLButtonElement>(null);
  const indexedRows = useMemo<IndexedRow[]>(() => rows.map((values, index) => ({ index, values })), [rows]);
  const previewColumns = useMemo(
    () => resultColumns(columns, rows, "preview", labels.nullValue),
    [columns, rows, labels.nullValue]
  );
  const fullColumns = useMemo(
    () => resultColumns(columns, rows, "full", labels.nullValue),
    [columns, rows, labels.nullValue]
  );
  const rowCount = rows.length;
  const summary = resultSummaryText({
    rowCount,
    columnCount: columns.length,
    truncated,
    totalRowCount,
    elapsedMs,
    labels,
  });
  const testIdOf = (suffix: string) => (testId ? `${testId}-${suffix}` : undefined);
  const downloadCsv = () => {
    const csv = resultRowsToCsv(columns, rows);
    if (onDownloadCsv) onDownloadCsv(csv, csvFilename);
    else downloadText(csvFilename, csv);
    onCsvDownloaded?.();
  };
  // 吹き出し・画面の中とシートの見出しの 2 か所に出すので、testid は場所ごとに分ける（#1178）。
  const csvButton = (suffix: "csv" | "all-csv") => (
    <Button
      type="button"
      variant="secondary"
      size="sm"
      icon={Download}
      onClick={downloadCsv}
      data-testid={testIdOf(suffix)}
    >
      {labels.downloadCsv}
    </Button>
  );
  const truncatedNotice = labels.truncatedNotice(
    typeof rowLimit === "number" ? labels.formatCount(rowLimit) : null
  );
  const notice = [truncatedNotice, fullResult?.hint].filter(Boolean).join("");

  return (
    <div className={cn("grid min-w-0 gap-3", className)} data-testid={testId}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <p className="text-sm font-medium text-fg" role="status" data-testid={testIdOf("summary")}>
            {summary}
          </p>
          {meta}
        </div>
        {rowCount > 0 || actions ? (
          <div className="flex flex-wrap items-center gap-2">
            {rowCount > 0 ? (
              <>
                <Button
                  type="button"
                  ref={viewAllRef}
                  variant="secondary"
                  size="sm"
                  icon={Maximize2}
                  aria-haspopup="dialog"
                  aria-expanded={sheetOpen}
                  onClick={() => setSheetOpen(true)}
                  data-testid={testIdOf("view-all")}
                >
                  {labels.viewAll}
                </Button>
                {csvButton("csv")}
              </>
            ) : null}
            {actions}
          </div>
        ) : null}
      </div>
      {rowCount > 0 ? (
        <DataTable<IndexedRow>
          columns={previewColumns}
          rows={indexedRows.slice(0, previewRows)}
          getRowKey={(row) => row.index}
          rowProps={() => ({ className: INFORMATION_TABLE_ROW_CLASS })}
          ariaLabel={labels.tableLabel}
          // 表頭を固定し、md 未満 5 行・md 以上 8 行を超える行と横に広い列は表の中でスクロールする
          // （吹き出し・会話の欄を伸ばさない）。
          stickyHeader
          visibleRows={INFORMATION_TABLE_VISIBLE_ROWS}
          scrollAriaLabel={labels.scrollLabel}
          scrollTestId={testIdOf("scroll")}
          testId={testIdOf("table")}
        />
      ) : null}
      {rowCount > previewRows ? (
        <p className="text-xs text-fg-muted" data-testid={testIdOf("preview-note")}>
          {labels.previewNote(labels.formatCount(previewRows), labels.formatCount(rowCount))}
        </p>
      ) : null}
      {cellsTruncated ? (
        <p className="text-xs text-fg-muted" data-testid={testIdOf("cells-truncated")}>
          {labels.cellsTruncated(typeof maxCellChars === "number" ? labels.formatCount(maxCellChars) : null)}
        </p>
      ) : null}
      {truncated ? (
        <div data-testid={testIdOf("truncated")}>
          <Banner
            severity="info"
            action={
              fullResult?.href && fullResult.label ? (
                <ButtonLink
                  to={fullResult.href}
                  linkComponent={fullResult.linkComponent}
                  size="sm"
                  trailingIcon={ExternalLink}
                >
                  {fullResult.label}
                </ButtonLink>
              ) : undefined
            }
          >
            {notice}
          </Banner>
        </div>
      ) : null}
      {rowCount > 0 ? (
        <SideSheet
          open={sheetOpen}
          onClose={() => setSheetOpen(false)}
          title={labels.sheetTitle(labels.formatCount(rowCount))}
          closeLabel={labels.close}
          side="right"
          size="wide"
          returnFocusRef={viewAllRef}
          headerActions={csvButton("all-csv")}
          bodyClassName="gap-3"
          data-testid={testIdOf("sheet")}
        >
          {/* 閉じている間は表を描かない（ターンごとに全行の表を描かない）。 */}
          {sheetOpen ? (
            <>
              <div className="flex flex-wrap items-end justify-between gap-2">
                <p className="text-sm text-fg">{summary}</p>
              </div>
              {/* ページで描く行を絞る（仮想化はしない。読み上げ・ブラウザの検索・印刷がそのまま使える）。 */}
              <PagedDataTable<IndexedRow>
                viewport="bounded"
                columns={fullColumns}
                rows={indexedRows}
                getRowKey={(row) => row.index}
                pageSize={pageSize}
                pageSizeOptions={pageSizeOptions}
                onPageSizeChange={setPageSize}
                resetKey={pageSize}
                paginationLabels={{
                  summary: labels.pageSummary,
                  pageSize: labels.pageSize,
                  pageSizeOption: labels.pageSizeOption,
                  pageIndicator: labels.pageIndicator,
                  prev: labels.prev,
                  next: labels.next,
                }}
                ariaLabel={labels.tableLabel}
                scrollAriaLabel={labels.scrollLabel}
                scrollTestId={testIdOf("all-scroll")}
                testId={testIdOf("all-table")}
                paginationPosition="bottom"
                showSinglePagePagination
                paginationTestId={testIdOf("all-pagination")}
              />
              {truncated ? <p className="text-xs text-fg-muted">{notice}</p> : null}
            </>
          ) : null}
        </SideSheet>
      ) : null}
    </div>
  );
}

/*
 * 以前の名前（#1154）。チャットの回答の中の結果の表として作ったが、画面のデータの結果にも使うので
 * `ResultTable` にした（#1178）。既存のコードのために別名を残す。新しいコードは `ResultTable` を使う。
 */
/** @deprecated `ResultTable` を使う。 */
export const ChatResultTable = ResultTable;
/** @deprecated `ResultTableColumn` を使う。 */
export type ChatResultTableColumn = ResultTableColumn;
/** @deprecated `ResultTableLabels` を使う。 */
export type ChatResultTableLabels = ResultTableLabels;
/** @deprecated `ResultTableProps` を使う。 */
export type ChatResultTableProps = ResultTableProps;
/** @deprecated `ResultFullResultLink` を使う。 */
export type ChatResultFullResultLink = ResultFullResultLink;
/** @deprecated `DEFAULT_RESULT_TABLE_LABELS` を使う。 */
export const DEFAULT_CHAT_RESULT_TABLE_LABELS = DEFAULT_RESULT_TABLE_LABELS;
/** @deprecated `RESULT_PREVIEW_ROWS` を使う。 */
export const CHAT_RESULT_PREVIEW_ROWS = RESULT_PREVIEW_ROWS;
/** @deprecated `RESULT_PAGE_SIZES` を使う。 */
export const CHAT_RESULT_PAGE_SIZES = RESULT_PAGE_SIZES;
/** @deprecated `resultSummaryText` を使う。 */
export const chatResultSummaryText = resultSummaryText;
