// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { formatRunEventTime, RunEventLog, runEventCategories, type RunEventLogItem } from "../src";

// #2386: 実行のイベントの記録（1 イベント = 1 行・開いて詳細・内側のスクロール・種類の絞り込み）。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

// 既定の表示の時間帯（UTC。#3274）の 2026-10-15 00:12:05。
const AT = "2026-10-15T00:12:05Z";

function items(): RunEventLogItem[] {
  return [
    {
      id: "e1",
      title: "実行",
      summary: "実行を開始しました。",
      at: AT,
      category: { id: "run", label: "実行" },
      status: { variant: "info", label: "開始" },
      details: { fields: [{ label: "イベントの種類", value: "run.created", mono: true }] },
    },
    {
      id: "e2",
      title: "search_documents",
      summary: "ツールを呼びました。",
      at: AT,
      category: { id: "tool", label: "ツール" },
      status: { variant: "success", label: "完了" },
      durationMs: 1200,
      details: { fields: [{ label: "ステップ", value: "step-1", mono: true }] },
    },
    {
      id: "e3",
      title: "search_documents",
      summary: "ツールが失敗しました。",
      at: AT,
      category: { id: "tool", label: "ツール" },
      status: { variant: "danger", label: "失敗" },
      details: { fields: [{ label: "ステップ", value: "step-2", mono: true }] },
      defaultOpen: true,
    },
    { id: "e4", title: "メッセージだけのイベント", at: AT },
  ];
}

function render(list: RunEventLogItem[]) {
  act(() => root.render(<RunEventLog items={list} testId="log" />));
}

describe("RunEventLog", () => {
  it("1 イベント = 1 行で、時刻・要約・バッジ・処理時間を出し、詳細は開いて見る", () => {
    render(items());
    const rows = container.querySelectorAll("[data-run-event]");
    expect(rows).toHaveLength(4);
    const row = container.querySelector('[data-testid="log-event-e2"]')!;
    expect(row.querySelector("time")?.textContent).toBe("00:12:05");
    expect(row.querySelector("time")?.getAttribute("title")).toBe("2026/10/15 00:12:05");
    expect(row.textContent).toContain("search_documents");
    expect(row.textContent).toContain("完了");
    expect(row.textContent).toContain("1.2 秒");
    const toggle = container.querySelector<HTMLButtonElement>('[data-testid="log-event-e2-toggle"]')!;
    const details = container.querySelector<HTMLElement>('[data-testid="log-event-e2-details"]')!;
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.getAttribute("aria-controls")).toBe(details.id);
    expect(details.hidden).toBe(true);
    act(() => toggle.click());
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(details.hidden).toBe(false);
    expect(details.textContent).toContain("step-1");
  });

  it("defaultOpen の行は最初から開き、詳細の無い行は開閉のボタンを持たない", () => {
    render(items());
    expect(container.querySelector('[data-testid="log-event-e3-toggle"]')?.getAttribute("aria-expanded")).toBe("true");
    expect(container.querySelector('[data-testid="log-event-e3-details"]')?.textContent).toContain("step-2");
    expect(container.querySelector('[data-testid="log-event-e4-toggle"]')).toBeNull();
    expect(container.querySelector('[data-testid="log-event-e4"]')?.textContent).toContain("メッセージだけのイベント");
  });

  it("一覧は内側のスクロール領域（region）に入れ、件数を出す", () => {
    render(items());
    const region = container.querySelector<HTMLElement>('[data-testid="log-scroll"]')!;
    expect(region.getAttribute("role")).toBe("region");
    expect(region.getAttribute("aria-label")).toBe("イベントの記録");
    expect(region.className).toContain("overflow-auto");
    expect(region.className).toContain("relative");
    expect(container.querySelector('[data-testid="log-count"]')?.textContent).toBe("全 4 件");
  });

  it("種類で絞り込むと行と件数が変わる", () => {
    render(items());
    const group = container.querySelector('[data-testid="log-filter"]')!;
    const chips = Array.from(group.querySelectorAll("button"));
    expect(chips.map((chip) => chip.textContent)).toEqual(["すべて4", "実行1", "ツール2"]);
    act(() => chips[2].click());
    expect(container.querySelectorAll("[data-run-event]")).toHaveLength(2);
    expect(container.querySelector('[data-testid="log-count"]')?.textContent).toBe("2 件（全 4 件）");
  });

  it("種類が 1 つ以下なら絞り込みを出さず、0 件は空の状態を出す", () => {
    render([items()[0]]);
    expect(container.querySelector('[data-testid="log-filter"]')).toBeNull();
    render([]);
    expect(container.textContent).toContain("イベントはまだありません");
    expect(container.querySelector('[data-testid="log-scroll"]')).toBeNull();
  });
});

describe("runEventCategories / formatRunEventTime", () => {
  it("種類は最初に出た順で件数を数える", () => {
    expect(runEventCategories(items())).toEqual([
      { id: "run", label: "実行", count: 1 },
      { id: "tool", label: "ツール", count: 2 },
    ]);
  });

  it("時刻は時:分:秒だけを返し、空は空文字", () => {
    expect(formatRunEventTime(AT)).toBe("00:12:05");
    expect(formatRunEventTime(null)).toBe("");
  });
});
