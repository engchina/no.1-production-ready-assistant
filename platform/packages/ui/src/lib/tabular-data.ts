/**
 * 表の形のデータの判定（#1158）。ツールの結果の JSON・回答の本文の Markdown の表から、結果の表の部品
 * （`ResultTable`。#1154 / #1178）へ渡す列と行を作る。製品に依存しない（NL2SQL の MCP の出力・Agent のツールの結果・
 * LLM の Markdown の表を同じ規則で扱う）。表とみなさない値は null を返し、画面は今の表示（JSON・本文）のまま出す。
 */

import type { ResultTableColumn } from "../components/data/result-table";

/** セルの値。null は「値が無い」（NULL）で、空の文字列と区別する。 */
export type TabularCellValue = string | number | boolean | null;

/** 列（`ResultTable` の列。`name` を表頭に出し、`type` が number 等なら右寄せ）。 */
export type TabularColumn = ResultTableColumn;

export interface TabularData {
  columns: TabularColumn[];
  /** 列の順の値の配列。 */
  rows: TabularCellValue[][];
  /** 取得の上限で打ち切った（さらに行がある）。 */
  truncated: boolean;
  /** 全体の行数（分かるときだけ）。 */
  totalRowCount: number | null;
  /** 取得にかかった時間（分かるときだけ）。 */
  elapsedMs: number | null;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isCellValue(value: unknown): value is TabularCellValue {
  return (
    value === null ||
    typeof value === "string" ||
    typeof value === "boolean" ||
    (typeof value === "number" && Number.isFinite(value))
  );
}

function nonNegativeNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
}

/** 列の指定の 1 つ（行の値の key・表頭の名前・型）。 */
interface SourceColumn {
  key: string;
  column: TabularColumn;
}

/** 列の指定（列名の文字列か `{ name, label?, type? }`）。1 つでも読めなければ null。 */
function readDeclaredColumns(value: unknown): SourceColumn[] | null {
  if (!Array.isArray(value)) return null;
  const columns: SourceColumn[] = [];
  for (const item of value) {
    if (typeof item === "string") {
      columns.push({ key: item, column: { name: item } });
      continue;
    }
    if (isPlainObject(item) && typeof item.name === "string") {
      const label = typeof item.label === "string" && item.label.trim() ? item.label : item.name;
      const column: TabularColumn = { name: label };
      if (typeof item.type === "string" && item.type) column.type = item.type;
      columns.push({ key: item.name, column });
      continue;
    }
    return null;
  }
  return columns;
}

/** オブジェクトの行の列（出てきた順の key の和集合）。 */
function columnsFromRecords(rows: Record<string, unknown>[]): SourceColumn[] {
  const keys: string[] = [];
  const seen = new Set<string>();
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (seen.has(key)) continue;
      seen.add(key);
      keys.push(key);
    }
  }
  return keys.map((key) => ({ key, column: { name: key } }));
}

/**
 * 行の値を読むオブジェクト。出どころの位置つきの行（`{ row_number, cell_range, values: { 列名: 値 } }`。
 * Agent の添付の表を読むツール。#2591）は、直下に列の key が無く `values` に入っているので `values` から読む。
 */
function rowCells(raw: Record<string, unknown>, columns: SourceColumn[]): Record<string, unknown> {
  if (isPlainObject(raw.values) && !columns.some(({ key }) => Object.hasOwn(raw, key))) return raw.values;
  return raw;
}

/**
 * 行を列の順の値の配列にする。セルが入れ子のオブジェクト・配列なら表にしない（null）。
 * 行に無い列（オブジェクトに key が無い・配列が短い）は null（値が無い）にする。
 */
function buildRows(rawRows: unknown[], columns: SourceColumn[]): TabularCellValue[][] | null {
  const rows: TabularCellValue[][] = [];
  for (const raw of rawRows) {
    let values: unknown[];
    if (Array.isArray(raw)) {
      values = columns.map((_, index) => (index < raw.length ? raw[index] : null));
    } else if (isPlainObject(raw)) {
      const cells = rowCells(raw, columns);
      values = columns.map(({ key }) => (Object.hasOwn(cells, key) ? cells[key] : null));
    } else {
      return null;
    }
    const row: TabularCellValue[] = [];
    for (const value of values) {
      if (!isCellValue(value)) return null;
      row.push(value);
    }
    rows.push(row);
  }
  return rows;
}

function tabularFromRows(
  rawRows: unknown[],
  rawColumns: unknown,
  meta: Record<string, unknown>
): TabularData | null {
  const declared = rawColumns === undefined ? null : readDeclaredColumns(rawColumns);
  if (rawColumns !== undefined && declared === null) return null;
  let columns = declared;
  if (!columns || columns.length === 0) {
    // 列の指定が無いときは、オブジェクトの行の key から列を作る（行が無ければ列も分からないので表にしない）。
    if (rawRows.length === 0 || !rawRows.every(isPlainObject)) return null;
    columns = columnsFromRecords(rawRows as Record<string, unknown>[]);
  }
  if (columns.length === 0) return null;
  const rows = buildRows(rawRows, columns);
  if (!rows) return null;
  return {
    columns: columns.map(({ column }) => column),
    rows,
    truncated: meta.truncated === true || meta.has_more === true,
    totalRowCount:
      nonNegativeNumber(meta.total) ?? nonNegativeNumber(meta.total_row_count) ?? nonNegativeNumber(meta.row_count),
    elapsedMs: nonNegativeNumber(meta.elapsed_ms),
  };
}

/**
 * JSON の値が表の形なら列と行にする。表とみなす形:
 * - `{ columns, rows }`（`columns` は列名の文字列か `{ name, label?, type? }` の配列、`rows` はオブジェクトか
 *   配列の配列。列の指定があれば 0 行も表。値を `values` に入れた出どころの位置つきの行も読む）
 * - `{ rows }`（オブジェクトの配列。1 行以上）
 * - オブジェクトの配列（1 行以上）
 * セルは文字列・数値・真偽値・null だけ。入れ子のオブジェクト・配列を持つ値は表にしない（null）。
 * `truncated` / `has_more`（打ち切り）・`total` / `row_count`（全体の行数）・`elapsed_ms`（所要時間）があれば添える。
 */
export function toTabularData(value: unknown): TabularData | null {
  if (Array.isArray(value)) return tabularFromRows(value, undefined, {});
  if (!isPlainObject(value) || !Array.isArray(value.rows)) return null;
  return tabularFromRows(value.rows, value.columns, value);
}

export type MarkdownTableSegment =
  | { kind: "text"; text: string }
  | { kind: "table"; data: TabularData };

const FENCE = /^\s*(```|~~~)/;
const SEPARATOR_CELL = /^\s*:?-+:?\s*$/;

/** Markdown の表の行のセル（先頭・末尾の `|` を除き、`\|` 以外の `|` で分ける）。 */
function splitTableRow(line: string): string[] {
  let body = line.trim();
  if (body.startsWith("|")) body = body.slice(1);
  if (body.endsWith("|") && !body.endsWith("\\|")) body = body.slice(0, -1);
  return body.split(/(?<!\\)\|/).map(cleanMarkdownCell);
}

/** セルの強調・コードの記号を除く（表の値として読む）。 */
function cleanMarkdownCell(cell: string): string {
  return cell
    .trim()
    .replace(/\\\|/g, "|")
    .replace(/\*\*(.+?)\*\*/g, "$1")
    .replace(/__(.+?)__/g, "$1")
    .replace(/`([^`]+)`/g, "$1");
}

function isSeparatorRow(line: string, columnCount: number): boolean {
  if (!line.includes("-")) return false;
  const cells = splitTableRow(line);
  return cells.length === columnCount && cells.every((cell) => SEPARATOR_CELL.test(cell));
}

const NUMERIC_TEXT = /^[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?$/;

function isNumericTextColumn(rows: TabularCellValue[][], index: number): boolean {
  let seen = false;
  for (const row of rows) {
    const value = row[index];
    if (value === "" || value === null) continue;
    if (typeof value !== "string" || !NUMERIC_TEXT.test(value)) return false;
    seen = true;
  }
  return seen;
}

function isTableRowLine(line: string): boolean {
  return line.trim() !== "" && line.includes("|") && !FENCE.test(line);
}

/**
 * 本文を Markdown の表（GFM: 表頭の行・区切りの行・本文の行）とそれ以外の文に分ける。
 * コードブロック（``` / ~~~）の中の表は表にしない。表が無ければ本文 1 つの配列を返す。
 * 表の本文の行でセルが足りない列は空の文字列（Markdown の表に NULL は無い）、多いセルは捨てる。
 */
export function splitMarkdownTables(text: string): MarkdownTableSegment[] {
  const lines = text.split("\n");
  const segments: MarkdownTableSegment[] = [];
  let buffer: string[] = [];
  let inFence = false;
  const flushText = () => {
    const joined = buffer.join("\n");
    buffer = [];
    if (joined.trim()) segments.push({ kind: "text", text: joined });
  };
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (FENCE.test(line)) {
      inFence = !inFence;
      buffer.push(line);
      index += 1;
      continue;
    }
    const next = lines[index + 1];
    if (!inFence && next !== undefined && isTableRowLine(line)) {
      const header = splitTableRow(line);
      if (isSeparatorRow(next, header.length)) {
        const rows: TabularCellValue[][] = [];
        let cursor = index + 2;
        while (cursor < lines.length && isTableRowLine(lines[cursor])) {
          const cells = splitTableRow(lines[cursor]);
          rows.push(header.map((_, cellIndex) => cells[cellIndex] ?? ""));
          cursor += 1;
        }
        flushText();
        segments.push({
          kind: "table",
          data: {
            columns: header.map((name, cellIndex) => {
              const column: TabularColumn = { name: name || String(cellIndex + 1) };
              // Markdown のセルは文字列。値がすべて数（桁区切り・小数・% を含む）の列は数値の列として右寄せにする。
              if (isNumericTextColumn(rows, cellIndex)) column.type = "number";
              return column;
            }),
            rows,
            truncated: false,
            totalRowCount: null,
            elapsedMs: null,
          },
        });
        index = cursor;
        continue;
      }
    }
    buffer.push(line);
    index += 1;
  }
  flushText();
  return segments;
}
