import type { ReportSource } from "@/lib/api";
import { t } from "@/lib/i18n";

/**
 * 集計元の説明（#794）。利用状況・フィードバックは、Oracle の構成では保存した Run の履歴
 * （`AGENT_RUN_FACTS`）を、memory / file の構成ではバックエンドのメモリにある Run を集計する。
 */
export function ReportSourceNote({ source }: { source: ReportSource }) {
  return (
    <p className="text-xs text-fg-muted" data-testid="report-source" data-source={source}>
      {t(source === "history" ? "report.source.history" : "report.source.memory")}
    </p>
  );
}
