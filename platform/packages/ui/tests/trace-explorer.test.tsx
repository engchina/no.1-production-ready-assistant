// @vitest-environment happy-dom
import { act } from "react";
import { createRoot } from "react-dom/client";
import { describe, expect, it } from "vitest";
import type { RunTimelineStep } from "../src/components/data/run-detail";
import { RunTimeline } from "../src/components/data/run-detail";
import { TraceExplorer } from "../src/components/data/trace-explorer";
import { traceCategory, traceMeasuredTiming, traceOrderedSteps, traceSearchText } from "../src/lib/trace-explorer";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
const steps: RunTimelineStep[] = [
  { id: "root", label: "合成の実行", status: "done", operationName: "agent.execute", startedAt: "2026-01-01T00:00:00Z", finishedAt: "2026-01-01T00:00:10Z" },
  { id: "model-a", parentId: "root", level: 1, label: "合成のモデル", status: "done", operationName: "gen_ai.chat", startedAt: "2026-01-01T00:00:02Z", finishedAt: "2026-01-01T00:00:08Z", details: { call: {
    input: {status: "recorded", text: "synthetic-input"}, output: {status: "withheld", text: "private-hidden"}, schema: {status: "not_recorded", text: ""},
  } } },
  { id: "tool-a", parentId: "root", level: 1, label: "合成のツール", status: "failed", operationName: "gen_ai.execute_tool", startedAt: "2026-01-01T00:00:01Z", finishedAt: "2026-01-01T00:00:04Z", details: {lines: ["synthetic-error"]} },
];
describe("記録した値だけを使う軌跡", () => {
  it("並列の時間を足さず、時刻不明を推定しない", () => {
    const timing = traceMeasuredTiming([...steps, {id: "old", label: "旧記録", status: "done", durationMs: 30000}]);
    expect(timing.duration).toBe(10000);
    expect(timing.bars[1]).toEqual({offset: 20, width: 60});
    expect(timing.bars[2]).toEqual({offset: 10, width: 30});
    expect(timing.bars[3]).toBeNull();
    expect(traceMeasuredTiming([{...steps[1], status: "running", finishedAt: null}]).bars[0]).toEqual({offset: 0, width: null});
  });
  it("記録の operation で分類し、時刻順では安定して並ぶ", () => {
    expect(steps.map(traceCategory)).toEqual(["stage", "model", "tool"]);
    expect(traceOrderedSteps(steps, "time").map(step => step.id)).toEqual(["root", "tool-a", "model-a"]);
    expect(traceOrderedSteps(steps, "hierarchy").map(step => step.id)).toEqual(["root", "tool-a", "model-a"]);
    expect(traceSearchText(steps[1])).toContain("synthetic-input");
    expect(traceSearchText(steps[1])).not.toContain("private-hidden");
  });
  it("選択と詳細を同期し、タブを保って前後の呼び出しを確認する", () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    const click = (selector: string) => act(() => host.querySelector<HTMLButtonElement>(selector)!.click());
    try {
      act(() => root.render(<TraceExplorer steps={steps} onClose={() => {}} />));
      expect(host.querySelector('[data-trace-inspector]')?.textContent).toContain("synthetic-error");
      click('[data-trace-record="model-a"] button');
      const inputTab = Array.from(host.querySelectorAll<HTMLButtonElement>('[role="tab"]')).find(tab => tab.textContent === "入力")!;
      act(() => inputTab.click());
      expect(host.querySelector('[data-trace-inspector]')?.textContent).toContain("synthetic-input");
      expect(host.textContent).not.toContain("private-hidden");
      click('button[aria-label="前の呼び出し"]');
      expect(host.querySelector('[data-trace-inspector]')?.textContent).toContain("synthetic-error");
      click('button[aria-label="次の呼び出し"]');
      expect(host.querySelector('[data-trace-inspector]')?.textContent).toContain("synthetic-input");
    } finally { act(() => root.unmount()); host.remove(); }
  });
  it("長い軌跡を段階的に表示し、選択した詳細だけを描画する", () => {
    const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
    try {
      act(() => root.render(<TraceExplorer steps={Array.from({length: 120}, (_, i) => ({ ...steps[1], id: `model-${i}` }))} onClose={() => {}} />));
      expect(host.querySelectorAll('[data-trace-record]')).toHaveLength(50);
      const more = Array.from(host.querySelectorAll<HTMLButtonElement>('button')).find(button => button.textContent?.includes("さらに表示"))!;
      act(() => more.click());
      expect(host.querySelectorAll('[data-trace-record]')).toHaveLength(100);
      expect(host.querySelectorAll('[data-call-details]')).toHaveLength(1);
    } finally { act(() => root.unmount()); host.remove(); }
  });
});

it("先頭の表示範囲より後ろの失敗を選んでも一覧に選択行を残す", () => {
  const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
  try {
    const longSteps = Array.from({length: 120}, (_, index) => ({...steps[1], id: `late-${index}`, status: index === 110 ? "failed" as const : "done" as const}));
    act(() => root.render(<TraceExplorer steps={longSteps} onClose={() => {}} />));
    expect(host.querySelector('[data-trace-record="late-110"] button[aria-pressed="true"]')).not.toBeNull();
  } finally { act(() => root.unmount()); host.remove(); }
});
it("snapshot のない工程でも記録された開始・終了時刻を確認できる", () => {
  const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
  try {
    act(() => root.render(<TraceExplorer steps={[steps[0]]} onClose={() => {}} />));
    expect(host.querySelector('[data-trace-inspector]')?.textContent).toContain("2026/01/01 00:00:00 (UTC)");
    expect(host.querySelector('[data-trace-inspector]')?.textContent).toContain("2026/01/01 00:00:10 (UTC)");
  } finally { act(() => root.unmount()); host.remove(); }
});

it("親を子より先に置き、同時刻・欠けた親・循環でも段を失わない", () => {
  const data: RunTimelineStep[] = [
    {...steps[1],id:"child",parentId:"parent"}, {...steps[0],id:"parent",startedAt:"2026-01-01T00:00:03Z"},
    {...steps[0],id:"orphan",parentId:"missing"}, {...steps[0],id:"cycle-a",parentId:"cycle-b"}, {...steps[0],id:"cycle-b",parentId:"cycle-a"},
  ];
  const ids = traceOrderedSteps(data,"hierarchy").map(step => step.id);
  expect(ids.indexOf("parent")).toBeLessThan(ids.indexOf("child"));
  expect(new Set(ids).size).toBe(data.length);
  expect(traceOrderedSteps(data,"hierarchy").map(step => step.id)).toEqual(ids);
});

// 時刻は本文の種類によらず同じ詳細ブロックの間隔で区切る。
it.each([
  {fields: [{label: "目的", value: "合成の説明"}]},
  {lines: ["合成の項目 A", "合成の項目 B"]},
  {code: [{text: "synthetic-code"}]},
])("工程の詳細と開始・終了時刻を共通の間隔で並べる: %j", details => {
  const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
  try {
    act(() => root.render(<TraceExplorer steps={[{...steps[0], details}]} onClose={() => {}} />));
    const content = host.querySelector("[data-trace-inspector-content]");
    expect(content).not.toBeNull();
    expect(content?.classList.contains("gap-2")).toBe(true);
    expect(content?.querySelector(":scope > [data-trace-timing]")).not.toBeNull();
    expect(content?.querySelector("[data-trace-timing]")?.textContent).toContain("開始時刻");
  } finally { act(() => root.unmount()); host.remove(); }
});

it("経過は処理内容だけを描き、軌跡は同じ内容の後に時刻と技術属性を残す", () => {
  const host = document.createElement("div"); document.body.append(host); const root = createRoot(host);
  const details = {fields: [{label: "目的", value: "合成の目的"}, {label: "結果", value: "合成の結果"},
    {label: "Span name", value: "synthetic.stage", mono: true, technical: true},
    {label: "Parent span ID", value: "synthetic-parent", mono: true, technical: true}], lines: ["合成の引用資料"]};
  try {
    act(() => root.render(<RunTimeline steps={[{...steps[0], details}]} defaultExpanded />));
    expect(host.textContent).toContain("合成の目的");
    expect(host.textContent).toContain("合成の結果");
    expect(host.textContent).not.toContain("Span name");
    const explore = Array.from(host.querySelectorAll<HTMLButtonElement>("button")).find(button => button.textContent?.includes("軌跡を調べる"))!;
    act(() => explore.click());
    const content = host.querySelector("[data-trace-inspector-content]")!;
    expect(Array.from(content.children).map(node => node.hasAttribute("data-trace-processing-content") ? "content" : node.hasAttribute("data-trace-timing") ? "time" : "technical")).toEqual(["content", "time", "technical"]);
    expect(content.querySelector("[data-trace-processing-content]")?.textContent).toContain("合成の引用資料");
    expect(content.querySelector("[data-trace-technical]")?.textContent).toContain("Span name");
    expect(content.querySelector("time")?.getAttribute("datetime")).toBe(steps[0].startedAt);
    expect(host.querySelector('button[aria-label="開始時刻: 元の ISO 時刻をコピー"]')).not.toBeNull();
  } finally { act(() => root.unmount()); host.remove(); }
});
