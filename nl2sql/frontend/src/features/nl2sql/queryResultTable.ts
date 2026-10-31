// データの結果（SQL の実行の結果・テーブルのデータの表示・取り込みのサンプル行）を、3 製品で共通の
// `ResultTable`（@production-ready/ui。#1154 / #1178）の形にする NL2SQL 固有の変換。
//
// 表の表示（要約・プレビュー・打ち切りの明示・すべての行・CSV・NULL・数値の右寄せ）は部品が持つ。ここは
// NL2SQL の API の形（列名をキーにした行）を部品の形（列の順の値の配列）にする変換と、画面の補足（実行の接続）・
// CSV のファイル名だけ。チャット（chatSqlExecution.ts）と画面（components/QueryResultTable.tsx）で共有する。

// node:test(jiti)から直接 import されるため、"@/" alias でなく相対 path を使う。
import { displayFileTimestamp } from "@production-ready/ui";
import type { I18nKey } from "../../lib/i18n";
import type { QueryResults } from "./types";

/** NL2SQL の結果（列名をキーにした行）を、共通の結果の表の形（列・列の順の値の配列）にする。 */
export function toResultTableData(results: Pick<QueryResults, "columns" | "rows">): {
  columns: { name: string }[];
  rows: unknown[][];
} {
  return {
    columns: results.columns.map((name) => ({ name })),
    // 欠けた列は NULL として渡す（空文字と区別する）。
    rows: results.rows.map((row) => results.columns.map((column) => row[column] ?? null)),
  };
}

/** 結果が上限（行数・応答のバイト数）で打ち切られたか。 */
export function isIncompleteResult(results: Pick<QueryResults, "has_more" | "truncated">): boolean {
  return Boolean(results.has_more || results.truncated);
}

/**
 * 実行した接続の表示の辞書のキー。既定（deterministic）では出さない（null）。
 * 行数・取得の上限・打ち切りは共通の要約が出すので、ここでは接続だけを補足する。
 */
export function queryResultExecutionContextKey(
  results: Pick<QueryResults, "execution_context">,
): I18nKey | null {
  const context = results.execution_context ?? "deterministic";
  if (context === "deterministic") return null;
  return `queryResults.executionContext.${context}` as I18nKey;
}

/** CSV のファイル名（例: nl2sql-direct-sql-20261005-140312.csv）。時刻はシステムの設定の時間帯。 */
export function queryResultCsvFilename(prefix: string, at: Date = new Date()): string {
  if (Number.isNaN(at.getTime())) return `${prefix}.csv`;
  return `${prefix}-${displayFileTimestamp(at)}.csv`;
}
