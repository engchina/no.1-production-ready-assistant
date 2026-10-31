import { ResultTable } from "@/components/chat/ResultTables";
import type { RunStep } from "@/lib/api";
import { t } from "@/lib/i18n";
import { stepResultTable } from "@/lib/run-tables";

/** ツールの工程の表は、チャットと履歴の同じ詳細に表示する。 */
export function toolProgressTable(step: RunStep) {
  const table = stepResultTable(step);
  return table ? (
    <ResultTable
      data={table}
      name={t("resultTable.toolName", { tool: step.tool_call?.name ?? step.kind })}
      testId={`run-step-table-${step.id}`}
    />
  ) : undefined;
}
