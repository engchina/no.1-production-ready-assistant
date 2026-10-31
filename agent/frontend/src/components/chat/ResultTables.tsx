import { ListChecks, Table2 } from "lucide-react";
import {
  ChatSqlBlock,
  cn,
  MarkdownText,
  SqlSafetyBadge,
  ResultTable as SharedResultTable,
  splitMarkdownTables,
  type ResultTableLabels,
  type TabularData,
  displayFileTimestamp,
} from "@production-ready/ui";

import { t } from "@/lib/i18n";
import type { ToolResultSql } from "@/lib/run-tables";

/**
 * Agent のデータの表（#1158）。ツールの結果・成果物・回答の Markdown の表を、NL2SQL のチャットと同じ共通の
 * 結果の表（`ResultTable`。#1154 / #1178）で出す。表の見た目と振る舞い（要約・表の中のスクロール・すべての行・CSV・
 * NULL の表示）は部品が持ち、ここは何の表か（表の名前）だけを渡す。
 */

function tableLabels(name: string): Partial<ResultTableLabels> {
  return {
    tableLabel: name,
    scrollLabel: t("resultTable.scrollLabel", { name }),
    sheetTitle: (count) => t("resultTable.sheetTitle", { name, count }),
    close: t("resultTable.close", { name }),
  };
}

/** 表の形の結果（ツールの結果・成果物）。 */
export function ResultTable({
  data,
  name,
  csvFilename,
  testId,
  labels,
}: {
  data: TabularData;
  labels?: Partial<ResultTableLabels>;
  name: string;
  /** 渡すと CSV（取得した行だけ）を出す。 */
  csvFilename?: string;
  testId?: string;
}) {
  return (
    <SharedResultTable
      columns={data.columns}
      rows={data.rows}
      truncated={data.truncated}
      totalRowCount={data.totalRowCount}
      elapsedMs={data.elapsedMs}
      csvFilename={csvFilename}
      labels={{...tableLabels(name), ...labels}}
      testId={testId}
    />
  );
}

/**
 * チャットの回答の下に出すツールの結果の表。業務データの照会（NL2SQL）の結果は、NL2SQL のチャットと同じ共有の部品で
 * SQL の塊（安全検査・実行の状態・コピー）→ 結果の表（要約・すべての行・CSV）の順に出す（#2346）。それ以外の表の形の
 * 結果は、見出しをツールの名前にした結果の表。
 */
export function ToolResultTable({
  data,
  toolName,
  sql = null,
  testId,
}: {
  data: TabularData;
  toolName: string;
  sql?: ToolResultSql | null;
  testId?: string;
}) {
  const name = sql ? t("resultTable.sqlResultName") : t("resultTable.toolName", { tool: toolName });
  return (
    <section className="min-w-0 space-y-2" aria-label={name} data-testid={testId}>
      {sql ? (
        <ChatSqlBlock
          sql={sql.text}
          badges={<SqlSafetyBadge safe={sql.safe} executed={sql.executed} />}
          testId={testId ? `${testId}-sql` : undefined}
        />
      ) : (
        <p className="flex min-w-0 items-center gap-1.5 text-xs text-fg-muted">
          <Table2 size={14} aria-hidden="true" className="shrink-0" />
          <span className="min-w-0 break-all">{name}</span>
        </p>
      )}
      <ResultTable
        data={data}
        name={name}
        csvFilename={sql ? toolResultCsvFilename() : undefined}
        testId={testId ? `${testId}-result` : undefined}
      />
    </section>
  );
}

/** CSV のファイル名（例: agent-sql-result-20261018-140312.csv。時刻はシステムの設定の時間帯）。 */
function toolResultCsvFilename(at: Date = new Date(), prefix = "agent-sql-result"): string {
  return `${prefix}-${displayFileTimestamp(at)}.csv`;
}

/**
 * 業務 Agent が記録した出力の項目（#2549。成果物 `structured_output`）。回答の下に、見出し「出力の項目」・結果の表・
 * CSV のダウンロードで出す。
 */
export function OutputResultTable({ data, testId }: { data: TabularData; testId?: string }) {
  const name = t("chat.output.title");
  return (
    <section className="min-w-0 space-y-2" aria-label={name} data-testid={testId}>
      <p className="flex min-w-0 items-center gap-1.5 text-xs text-fg-muted">
        <ListChecks size={14} aria-hidden="true" className="shrink-0" />
        <span className="min-w-0">{name}</span>
      </p>
      <ResultTable
        data={data}
        name={name}
        csvFilename={toolResultCsvFilename(new Date(), "agent-output")}
        labels={{ nullValue: t("chat.output.noValue") }}
        testId={testId ? `${testId}-result` : undefined}
      />
    </section>
  );
}

/**
 * 回答の本文。Markdown の表は共通の結果の表で出し（#1158）、表以外の部分（見出し・太字・箇条書き・コードなど）は
 * 共通の Markdown の本文（`MarkdownText`。#1808）で描く。チャットと実行の詳細で同じ描き方にする。
 */
export function AnswerBody({
  text,
  headingLevel,
  className,
  testId,
}: {
  text: string;
  /** 本文の最上位の見出しの段（置く場所の見出しの下。既定 3）。 */
  headingLevel?: 2 | 3 | 4;
  className?: string;
  testId?: string;
}) {
  const segments = splitMarkdownTables(text);
  // 表に 1 から番号を振る（表が 1 つなら番号を付けない）。
  const tableNumbers = segments.map((segment, index) =>
    segment.kind === "table" ? segments.slice(0, index + 1).filter((item) => item.kind === "table").length : 0
  );
  const tableCount = tableNumbers.filter((value) => value > 0).length;
  return (
    <div className={cn("min-w-0 space-y-3", className)} data-testid={testId}>
      {segments.map((segment, index) =>
        segment.kind === "text" ? (
          <MarkdownText key={index} text={segment.text} headingLevel={headingLevel} />
        ) : (
          <ResultTable
            key={index}
            data={segment.data}
            name={
              tableCount === 1
                ? t("resultTable.answerName")
                : t("resultTable.answerNameIndexed", { index: tableNumbers[index] })
            }
            testId={testId ? `${testId}-table-${tableNumbers[index]}` : undefined}
          />
        )
      )}
    </div>
  );
}
