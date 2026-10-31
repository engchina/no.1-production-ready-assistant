/** 軌跡の調査に使う、表示名に依存しない分類と実測の時間座標。 */
import type { RunTimelineStep } from "../components/data/run-detail";
import { operationTimestampMs } from "./operation-timing";

export type TraceCategory = "model" | "tool" | "stage";
export type TraceOrder = "hierarchy" | "time";
export function traceCategory(step: RunTimelineStep): TraceCategory {
  const name = step.operationName ?? "";
  if (name === "gen_ai.chat" || name === "oracle.select_ai") return "model";
  if (name === "gen_ai.execute_tool" || name.startsWith("mcp.")) return "tool";
  return "stage";
}

export function traceSearchText(step: RunTimelineStep): string {
  const call = step.details?.call;
  return [step.id, step.label, step.detail, step.operationName, step.product,
    ...(step.details?.fields ?? []).flatMap(field => [field.label, field.value]),
    ...(step.details?.lines ?? []), ...(step.details?.code ?? []).map(block => block.text),
    ...[call?.input, call?.output, call?.schema].map(content =>
      content && (content.status === "recorded" || content.status === "truncated") ? content.text : ""),
  ].filter(Boolean).join("\n").toLocaleLowerCase();
}

export function traceMeasuredTiming(steps: readonly RunTimelineStep[]) {
  const times = steps.map(step => {
    const start = operationTimestampMs(step.startedAt);
    const end = operationTimestampMs(step.finishedAt);
    return { start, end: start != null && end != null && end >= start ? end : null };
  });
  const starts = times.flatMap(t => t.start == null ? [] : [t.start]);
  const ends = times.flatMap(t => t.end == null ? [] : [t.end]);
  const origin = starts.length ? Math.min(...starts) : null;
  const last = starts.length ? Math.max(...ends, ...starts) : null;
  const duration = origin != null && last != null ? Math.max(0, last - origin) : null;
  return { duration, bars: times.map(t => {
    if (origin == null || duration == null || t.start == null) return null;
    return { offset: duration > 0 ? (t.start - origin) / duration * 100 : 0,
      width: t.end == null ? null : duration > 0 ? Math.max(0, (t.end - t.start) / duration * 100) : 0 };
  }) };
}

export function traceOrderedSteps(steps: readonly RunTimelineStep[], order: TraceOrder): RunTimelineStep[] {
  const sorted = steps.map((step, index) => ({step, index})).sort((a, b) => {
    const startA = operationTimestampMs(a.step.startedAt);
    const startB = operationTimestampMs(b.step.startedAt);
    if (startA == null) return startB == null ? a.index - b.index : 1;
    return startB == null ? -1 : startA - startB || a.index - b.index;
  }).map(item => item.step);
  if (order === "time") return sorted;
  const ids = new Set(steps.map(step => step.id));
  const children = new Map<string, RunTimelineStep[]>();
  for (const step of sorted) if (step.parentId && ids.has(step.parentId)) {
    const siblings = children.get(step.parentId) ?? []; siblings.push(step); children.set(step.parentId, siblings);
  }
  const result: RunTimelineStep[] = [];
  const visited = new Set<string>();
  const visit = (step: RunTimelineStep) => {
    if (visited.has(step.id)) return;
    visited.add(step.id); result.push(step);
    for (const child of children.get(step.id) ?? []) visit(child);
  };
  for (const step of sorted) if (!step.parentId || !ids.has(step.parentId)) visit(step);
  // 循環・欠けた親がある旧記録でも、段を消さず安定して表示する。
  for (const step of sorted) visit(step);
  return result;
}
