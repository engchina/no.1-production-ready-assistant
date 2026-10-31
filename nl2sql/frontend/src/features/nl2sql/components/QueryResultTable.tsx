import { useMemo } from "react";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  ResultTable,
  StatusBadge,
  toast,
  type ResultTableLabels,
} from "@production-ready/ui";

import { t } from "@/lib/i18n";
import {
  isIncompleteResult,
  queryResultCsvFilename,
  queryResultExecutionContextKey,
  toResultTableData,
} from "../queryResultTable";
import type { QueryResults } from "../types";

/**
 * 画面のデータの結果（SQL 生成・SELECT SQL・管理 SQL の実行の結果、テーブル・ビューのデータの表示、取り込みの
 * サンプル行、合成データの結果）。チャットの結果と同じ共通の `ResultTable`（#1154 / #1178）で、要約・先頭の行の
 * プレビュー（表の中で縦横スクロール）・「すべての行を見る」（広いシートでページ送り）・CSV（取得した行だけ）を出す。
 * 表・ページング・要約は製品で持たない。ここは NL2SQL の API の形の変換と、表の名前・実行の接続を渡すだけ。
 */
export function QueryResultTable({
  results,
  rowLimit,
  name = t("queryResults.name.default"),
  csvFilePrefix = "nl2sql-result",
  testId = "query-results",
}: {
  results: QueryResults;
  /** 実行に使った取得の上限（打ち切りの案内に出す。0 は上限なし）。 */
  rowLimit?: number | null;
  /** 表の名前（読み上げ・シートの見出し。例「実行結果」「表示結果」）。 */
  name?: string;
  /** CSV のファイル名の接頭辞（後ろに時刻を付ける）。 */
  csvFilePrefix?: string;
  testId?: string;
}) {
  const table = useMemo(() => toResultTableData(results), [results]);
  const labels = useMemo<Partial<ResultTableLabels>>(
    () => ({
      nullValue: t("queryResults.null"),
      tableLabel: name,
      scrollLabel: t("queryResults.table.scrollLabel", { name }),
      sheetTitle: (count) => t("queryResults.table.sheetTitle", { name, count }),
      close: t("queryResults.table.close", { name }),
    }),
    [name],
  );
  const contextKey = queryResultExecutionContextKey(results);
  const hasRowLimit = typeof rowLimit === "number";
  return (
    <ResultTable
      columns={table.columns}
      rows={table.rows}
      truncated={isIncompleteResult(results)}
      rowLimit={typeof rowLimit === "number" && rowLimit > 0 ? rowLimit : null}
      csvFilename={queryResultCsvFilename(csvFilePrefix)}
      onCsvDownloaded={() => toast.success(t("common.action.downloaded"))}
      meta={
        // 画面の補足: 実行に使った取得の上限（利用者が指定した値）と、実行した接続。行数・打ち切りは共通の要約が出す。
        hasRowLimit || contextKey ? (
          <span className="flex flex-wrap items-center gap-2" data-testid={`${testId}-meta`}>
            {hasRowLimit ? (
              <StatusBadge
                icon={false}
                variant="neutral"
                label={
                  rowLimit === 0
                    ? t("queryResults.rowLimit.unlimited")
                    : t("queryResults.rowLimit.value", { count: rowLimit })
                }
              />
            ) : null}
            {contextKey ? (
              <StatusBadge
                icon={false}
                variant={results.vpd_context_enforced ? "info" : "neutral"}
                label={t(contextKey)}
              />
            ) : null}
          </span>
        ) : null
      }
      labels={labels}
      testId={testId}
    />
  );
}

/**
 * SQL 生成・「SELECT SQL を実行」の画面の結果のカード（見出し「実行結果（N 行）」。行のシートの見出しとそろえる。#1189）。中身は `QueryResultTable`。
 * 実行していない（`results` が無い）ときは何も出さない。
 */
export function QueryResultCard({
  results,
  rowLimit,
  csvFilePrefix,
}: {
  results: QueryResults | null;
  rowLimit?: number | null;
  csvFilePrefix: string;
}) {
  if (!results) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>{t("nl2sql.results.title", { count: results.total })}</CardTitle>
      </CardHeader>
      <CardContent>
        <QueryResultTable
          results={results}
          rowLimit={rowLimit}
          csvFilePrefix={csvFilePrefix}
        />
      </CardContent>
    </Card>
  );
}
