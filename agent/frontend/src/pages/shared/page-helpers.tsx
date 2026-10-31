import { formatDisplayDateTime } from "@production-ready/ui";
// 複数の画面で使う小さな表示の部品と helper（旧 AgentRuntimePages.tsx から分けた。#818）。



export function MetricPill({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-border px-3 py-2">
      <p className="text-xs text-fg-muted">{label}</p>
      <p className="mt-1 text-sm font-medium text-fg">{value}</p>
    </div>
  );
}

/** 送信に失敗したとき、最初のエラーの欄へフォーカスを移す（UX 契約 messaging.md §3.2.1）。 */
export function focusField(id: string): void {
  document.getElementById(id)?.focus();
}

export function JsonPanel({ title, value }: { title: string; value: unknown }) {
  return (
    <div className="min-w-0">
      <p className="mb-1 text-xs font-medium text-fg-muted">{title}</p>
      <JsonPreview value={value} />
    </div>
  );
}

export function JsonPreview({ value }: { value: unknown }) {
  return (
    <pre className="mt-2 max-h-64 w-full min-w-0 max-w-full overflow-auto rounded-md bg-surface-sunken p-3 text-xs leading-5 text-fg">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function formatDate(value: string) {
  return formatDisplayDateTime(value, { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
