import type { ExtractionTable, ExtractionTableCell } from "./api";

export interface TableCellFocusTarget {
  key: string;
  table: ExtractionTable;
  cell: ExtractionTableCell;
}

export interface TableCellLocator {
  tableId?: string | null;
  cellRef?: string | null;
  row?: number | null;
  col?: number | null;
}

export interface MetadataTokenOptions {
  preferTableId?: boolean;
}

const CELL_TOKEN_KEYS = [
  "formula_cell_ref",
  "cell_ref",
  "table_cell_ref",
  "cell_address",
  "address",
  "ref",
  "id",
  "value",
] as const;
const TABLE_TOKEN_KEYS = ["table_id", "parent_table_id"] as const;

export function tableCellKey(tableId: string, cell: Pick<ExtractionTableCell, "row" | "col">) {
  return `${tableId}:r${cell.row}:c${cell.col}`;
}

export function tableCellRef(cell: ExtractionTableCell): string | null {
  const metadata = cell.metadata ?? {};
  for (const key of ["formula_cell_ref", "cell_ref", "cell_address", "address"]) {
    const value = metadata[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return null;
}

export function findTableCellTarget(
  tables: ExtractionTable[],
  locator: TableCellLocator
): TableCellFocusTarget | null {
  const tableId = locator.tableId?.trim();
  const cellRef = locator.cellRef?.trim().toLowerCase();
  for (const table of tables) {
    if (tableId && table.table_id !== tableId) continue;
    for (const cell of table.cells) {
      const rowMatches = locator.row == null || cell.row === locator.row;
      const colMatches = locator.col == null || cell.col === locator.col;
      const refMatches = !cellRef || tableCellRef(cell)?.toLowerCase() === cellRef;
      if (rowMatches && colMatches && refMatches) {
        return { key: tableCellKey(table.table_id, cell), table, cell };
      }
    }
  }
  return null;
}

export function firstMetadataToken(
  value: unknown,
  options: MetadataTokenOptions = {}
): string | null {
  if (typeof value === "string" || typeof value === "number") {
    const text = String(value).trim();
    if (!text) return null;
    const parsed = parseJsonMetadataToken(text, options);
    if (parsed !== null) return parsed;
    return firstNonEmptyToken(text.split(/[,;\n\r\t]+/));
  }
  if (Array.isArray(value)) {
    for (const item of value) {
      const token = firstMetadataToken(item, options);
      if (token) return token;
    }
    return null;
  }
  if (isRecord(value)) {
    const firstKeys = options.preferTableId ? TABLE_TOKEN_KEYS : CELL_TOKEN_KEYS;
    const fallbackKeys = options.preferTableId ? CELL_TOKEN_KEYS : TABLE_TOKEN_KEYS;
    for (const key of firstKeys) {
      const token = firstMetadataToken(value[key], options);
      if (token) return token;
    }
    for (const [key, item] of Object.entries(value)) {
      if ((fallbackKeys as readonly string[]).includes(key)) continue;
      const token = firstMetadataToken(item, options);
      if (token) return token;
    }
    for (const key of fallbackKeys) {
      const token = firstMetadataToken(value[key], options);
      if (token) return token;
    }
  }
  return null;
}

export function integerMetadataValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return Math.trunc(value);
  if (typeof value !== "string") return null;
  const parsed = Number(value.trim());
  return Number.isFinite(parsed) ? Math.trunc(parsed) : null;
}

function firstNonEmptyToken(values: string[]): string | null {
  const first = values.map((item) => item.trim()).find(Boolean);
  return first ?? null;
}

function parseJsonMetadataToken(value: string, options: MetadataTokenOptions): string | null {
  if (!value.startsWith("[") && !value.startsWith("{")) return null;
  try {
    return firstMetadataToken(JSON.parse(value), options);
  } catch {
    return null;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
