import { formatNumber } from "@/lib/format";
import { t } from "@/lib/i18n";

// フィードバックの 2 つの見方（利用者の評価・確かめるべき実行。#774 / #1822）で共通の、集計のタイルと増減の文言。

export function Metric({ label, value, delta }: { label: string; value: string; delta: string }) {
  return (
    <div className="min-w-0 border-l-2 border-accent-emphasis pl-3">
      <p className="truncate text-xs text-fg-muted" title={label}>
        {label}
      </p>
      <p className="mt-0.5 text-xl font-semibold tabular-nums text-fg">{value}</p>
      <p className="mt-0.5 text-xs tabular-nums text-fg-muted">{delta}</p>
    </div>
  );
}

export function formatRate(value: number | null): string {
  return value === null ? "—" : `${Math.round(value * 100)}%`;
}

export function countDelta(current: number, previous: number): string {
  const delta = current - previous;
  if (delta === 0) return t("feedback.summary.same");
  return t("feedback.summary.delta", { value: `${delta > 0 ? "+" : "−"}${formatNumber(Math.abs(delta))}` });
}

/** 割合の増減（ポイント）。前の期間に母数が無ければ `noPrevious`。 */
export function rateDelta(current: number | null, previous: number | null, noPrevious = t("feedback.summary.noPrevious")): string {
  if (previous === null) return noPrevious;
  if (current === null) return " ";
  const delta = Math.round((current - previous) * 100);
  if (delta === 0) return t("feedback.summary.same");
  return t("feedback.summary.deltaPoints", { value: `${delta > 0 ? "+" : "−"}${Math.abs(delta)}` });
}
