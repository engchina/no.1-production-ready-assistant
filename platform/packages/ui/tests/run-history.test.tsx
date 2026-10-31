// @vitest-environment happy-dom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_RUN_PERIOD,
  DEFAULT_RUN_STATUS_LABELS,
  RUN_STATUSES,
  RunDetailLayout,
  RunDetailSection,
  RunJobProgress,
  RunProcessDetails,
  RunUsedModels,
  RunHistoryPeriodEmptyState,
  RunHistoryTable,
  RunHistoryToolbar,
  RunStatusBadge,
  RunSummary,
  formatRunCancellation,
  RunTimeline,
  TraceDetails,
  RunTimelineSection,
  formatRunDateTime,
  formatRunDuration,
  formatRunTokens,
  runListUsage,
  runUsageView,
  isRunActive,
  runPeriodSince,
  runStatusIcon,
  runStatusVariant,
  runTimelineBars,
  runTimelineHasChildren,
  runTimelineCurrentStepIndex,
  runStepDetailsJson,
  hasRunStepDetails,
  setDisplayTimeZone,
  type RunHistoryItem,
  type RunModelUsageRecord,
  type RunTimelineStep,
} from "../src";

// #1955: 実行履歴の統一の型（3 製品の一覧・詳細の共通の部品）。

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

function render(node: React.ReactNode) {
  act(() => root.render(node));
}

describe("実行の状態", () => {
  it("7 つの状態はアイコンがすべて違い、色だけで区別しない", () => {
    const icons = new Set(RUN_STATUSES.map((status) => runStatusIcon(status)));
    expect(icons.size).toBe(RUN_STATUSES.length);
  });

  it("状態の色（variant）は意味の系統に写す", () => {
    expect(runStatusVariant("succeeded")).toBe("success");
    expect(runStatusVariant("failed")).toBe("danger");
    expect(runStatusVariant("running")).toBe("info");
    expect(runStatusVariant("waiting")).toBe("warning");
    expect(runStatusVariant("blocked")).toBe("warning");
    expect(runStatusVariant("cancelled")).toBe("neutral");
    expect(runStatusVariant("queued")).toBe("neutral");
  });

  it("終わっていない状態は待機中・実行中・応答待ちだけ", () => {
    expect(RUN_STATUSES.filter(isRunActive)).toEqual(["queued", "running", "waiting"]);
  });

  it("バッジは文言・アイコン・data-run-status を持ち、製品の文言で上書きできる", () => {
    const html = renderToStaticMarkup(<RunStatusBadge status="waiting" label="承認待ち" />);
    expect(html).toContain('data-run-status="waiting"');
    expect(html).toContain("承認待ち");
    expect(html).toContain("<svg");
    expect(renderToStaticMarkup(<RunStatusBadge status="failed" />)).toContain(DEFAULT_RUN_STATUS_LABELS.failed);
    expect(renderToStaticMarkup(<RunStatusBadge status="failed" labels={{ failed: "エラー" }} />)).toContain("エラー");
  });
});

describe("書式と絞り込みの値", () => {
  it("所要時間はチャットの経過と同じ書式、記録なしは「—」", () => {
    expect(formatRunDuration(1234)).toBe("1.2 秒");
    expect(formatRunDuration(125_000)).toBe("2 分 5 秒");
    expect(formatRunDuration(null)).toBe("—");
    expect(formatRunDuration(undefined, "記録なし")).toBe("記録なし");
  });

  it("日時はシステムの設定の時間帯の絶対時刻（相対時刻にしない。端末の時間帯に従わない）", () => {
    const iso = "2026-10-14T16:12:05.000Z";
    try {
      setDisplayTimeZone("Asia/Tokyo");
      expect(formatRunDateTime(iso)).toBe("2026/10/15 01:12");
      expect(formatRunDateTime(iso, { seconds: true })).toBe("2026/10/15 01:12:05");
      expect(formatRunDateTime(iso, { seconds: true, timeZone: true })).toBe("2026/10/15 01:12:05 (Asia/Tokyo)");
      setDisplayTimeZone("UTC");
      expect(formatRunDateTime(iso)).toBe("2026/10/14 16:12");
    } finally {
      setDisplayTimeZone(null);
    }
    expect(formatRunDateTime(null)).toBe("—");
    expect(formatRunDateTime("not-a-date")).toBe("not-a-date");
  });

  it("token は桁区切り", () => {
    expect(formatRunTokens(12345)).toBe("12,345 token");
    expect(formatRunTokens(null)).toBe("—");
  });

  it("モデルの利用量: 実測・一部計測・未計測（記録なし・計測できない呼び出しだけ）・実測の 0（#3543）", () => {
    const usage: RunModelUsageRecord = {
      models: ["model-a", "model-b"],
      requests: 3,
      input_tokens: 12000,
      output_tokens: 345,
      total_tokens: 12345,
      measurement: "complete",
      measured_requests: 3,
    };
    expect(runUsageView(usage, { fallbackModel: "model-x" })).toEqual({
      model: "model-a、model-b",
      usage: { tokens: 12345, note: null },
      modelCalls: "3 回（入力 12,000 / 出力 345 token）",
      unmeasuredCalls: null,
    });
    const mixed = runUsageView(
      { ...usage, measurement: "partial", measured_requests: 2, unmeasured_sources: ["db_model"] },
      { sourceLabel: (source) => `出どころ ${source}` }
    );
    expect(mixed.usage).toEqual({ tokens: 12345, note: "一部計測" });
    expect(mixed.modelCalls).toBe("3 回（うち 2 回を計測。入力 12,000 / 出力 345 token）");
    expect(mixed.unmeasuredCalls).toBe("出どころ db_model");
    // 計測できない呼び出しだけ: 数を作らない。
    const unmeasured = runUsageView({
      models: [],
      requests: 0,
      input_tokens: 0,
      output_tokens: 0,
      total_tokens: 0,
      measurement: "unmeasured",
      measured_requests: 0,
      unmeasured_sources: ["db_model"],
    });
    expect(unmeasured).toEqual({
      model: null,
      usage: { tokens: null, note: "未計測" },
      modelCalls: "未計測",
      unmeasuredCalls: "db_model",
    });
    // 記録の無い実行は未計測（0 にしない）。記録の列のモデルは出す。
    expect(runUsageView(null, { fallbackModel: "model-x" })).toEqual({
      model: "model-x",
      usage: { tokens: null, note: "未計測" },
      modelCalls: "未計測",
      unmeasuredCalls: null,
    });
    expect(runUsageView(usage, { labels: { modelCalls: ({ requests }) => `calls ${requests}` } }).modelCalls).toBe(
      "calls 3"
    );

    expect(runListUsage(12345, "complete")).toEqual({ tokens: 12345, note: null });
    expect(runListUsage(330, "partial")).toEqual({ tokens: 330, note: "一部計測" });
    expect(runListUsage(null, "complete")).toEqual({ tokens: 0, note: null });
    expect(runListUsage(null, "unmeasured")).toEqual({ tokens: null, note: "未計測" });
    expect(runListUsage(undefined, null)).toEqual({ tokens: null, note: "未計測" });
    expect(runListUsage(1, "partial", { partial: "partial" }).note).toBe("partial");
  });

  it("期間の開始は now から引いた ISO、すべては null", () => {
    const now = new Date("2026-10-15T12:00:00.000Z");
    expect(runPeriodSince("all", now)).toBeNull();
    expect(runPeriodSince("24h", now)).toBe("2026-10-14T12:00:00.000Z");
    expect(runPeriodSince("7d", now)).toBe("2026-10-08T12:00:00.000Z");
    expect(runPeriodSince("30d", now)).toBe("2026-09-15T12:00:00.000Z");
  });
});

interface ProductRow {
  run_id: string;
  question: string;
  status: "ok" | "ng";
  by: string;
}

const ROWS: ProductRow[] = [
  { run_id: "r1", question: "先月の売上は？", status: "ok", by: "me" },
  { run_id: "r2", question: "在庫の少ない商品を教えて", status: "ng", by: "佐藤" },
];

function toItem(row: ProductRow): RunHistoryItem {
  return {
    id: row.run_id,
    title: row.question,
    target: "販売分析",
    href: `/history?id=${row.run_id}`,
    status: row.status === "ok" ? "succeeded" : "failed",
    kind: "チャット",
    executor: row.by === "me" ? { name: "山田", isSelf: true } : { name: row.by },
    startedAt: "2026-10-15T00:12:05.000Z",
    durationMs: row.status === "ok" ? 3200 : null,
  };
}

describe("RunHistoryTable", () => {
  it("列の順は 質問 → 状態 → 種類 → 実行者 → 開始日時 → 所要時間 → 製品の列 → 操作", () => {
    render(
      <RunHistoryTable
        rows={ROWS}
        toItem={toItem}
        onOpen={() => undefined}
        showExecutor
        extraColumns={[{ key: "rows", header: "行数", render: () => "10" }]}
        actionsFor={() => [{ id: "rerun", label: "再実行", onSelect: () => undefined }]}
        testId="runs"
      />
    );
    const headers = Array.from(container.querySelectorAll("thead th")).map((th) => th.textContent?.trim());
    expect(headers).toEqual(["質問", "状態", "種類", "実行者", "開始日時", "所要時間", "行数", "操作"]);
    // 広い置き場所だけの列（狭いと題名の下の 1 行）。製品の列も同じ扱い。
    const statusHeader = container.querySelectorAll("thead th")[1];
    expect(statusHeader.className).toContain("hidden");
    expect(statusHeader.className).toContain("@4xl:table-cell");
    expect(container.querySelectorAll("thead th")[6].className).toContain("@4xl:table-cell");
    expect(container.querySelector("[data-run-history-table]")?.className).toContain("@container");
  });

  it("「操作」の見出しは広い置き場所で 1 行、セルは操作が無くてもメニューのボタンの幅を保つ（#3427）", () => {
    render(
      <RunHistoryTable
        rows={ROWS}
        toItem={toItem}
        onOpen={() => undefined}
        // 1 行目だけに操作（終わった実行の行は操作が無い）。
        actionsFor={(row) => [{ id: "cancel", label: "停止", visible: row.run_id === "r1", onSelect: () => undefined }]}
        testId="runs"
      />
    );
    const label = container.querySelector("thead th:last-child span")!;
    expect(label.textContent).toBe("操作");
    // not-sr-only は white-space: normal に戻すので、nowrap を重ねる。
    expect(label.className).toContain("@4xl:not-sr-only");
    expect(label.className).toContain("@4xl:whitespace-nowrap");
    const cells = container.querySelectorAll("[data-run-history-actions]");
    expect(cells).toHaveLength(2);
    for (const cell of cells) expect(cell.className).toContain("min-w-[var(--button-height-sm)]");
    expect(cells[0].querySelector('[data-testid="runs-row-actions-r1"]')).not.toBeNull();
    expect(cells[1].childElementCount).toBe(0);
  });

  it("題名は詳細へのリンク（href・aria-current）で、狭い画面の 1 行に状態・日時・所要時間・実行者を持つ", () => {
    render(
      <RunHistoryTable rows={ROWS} toItem={toItem} onOpen={() => undefined} showExecutor selectedRunId="r2" />
    );
    const links = container.querySelectorAll<HTMLAnchorElement>("a[data-run-id]");
    expect(links).toHaveLength(2);
    expect(links[0].getAttribute("href")).toBe("/history?id=r1");
    expect(links[1].getAttribute("aria-current")).toBe("true");
    const meta = container.querySelectorAll("[data-run-history-compact-meta]");
    expect(meta[0].className).toContain("@4xl:hidden");
    expect(meta[0].textContent).toContain("完了");
    expect(meta[0].textContent).toContain("3.2 秒");
    expect(meta[0].textContent).toContain("自分");
    expect(meta[1].textContent).toContain("失敗");
    expect(meta[1].textContent).toContain("—");
    expect(meta[1].textContent).toContain("佐藤");
    expect(meta[0].querySelector("time")?.getAttribute("dateTime")).toBe("2026-10-15T00:12:05.000Z");
  });

  it("実行者が自分の行は「自分」、列は渡したときだけ出す", () => {
    render(<RunHistoryTable rows={ROWS} toItem={toItem} onOpen={() => undefined} />);
    const headers = Array.from(container.querySelectorAll("thead th")).map((th) => th.textContent?.trim());
    expect(headers).not.toContain("実行者");
    expect(headers).not.toContain("操作");
    expect(headers).not.toContain("token / 費用");
  });

  it("題名のクリック（修飾キーなし）と行のクリックで onOpen を呼ぶ", () => {
    const onOpen = vi.fn();
    render(<RunHistoryTable rows={ROWS} toItem={toItem} onOpen={onOpen} />);
    act(() => {
      container.querySelector<HTMLAnchorElement>("a[data-run-id='r1']")?.click();
    });
    expect(onOpen).toHaveBeenLastCalledWith(ROWS[0]);
    act(() => {
      container.querySelectorAll<HTMLTableRowElement>("tbody tr")[1].querySelector("td")?.click();
    });
    expect(onOpen).toHaveBeenLastCalledWith(ROWS[1]);
  });

  it("利用量の列は token と費用、記録が無ければ「—」", () => {
    render(
      <RunHistoryTable
        rows={ROWS}
        toItem={(row) => ({ ...toItem(row), usage: row.status === "ok" ? { tokens: 1500, cost: "$0.01" } : null })}
        onOpen={() => undefined}
        showUsage
      />
    );
    const cells = Array.from(container.querySelectorAll("tbody tr")).map((tr) => tr.lastElementChild?.textContent);
    expect(cells[0]).toBe("1,500 token$0.01");
    expect(cells[1]).toBe("—");
  });

  it("利用量の計測の状態の注記: 数があれば数の下に、無ければ注記だけ（0・「—」にしない。#3466）", () => {
    const usages: RunHistoryItem["usage"][] = [
      { tokens: 12345, note: "一部計測" },
      { tokens: null, note: "未計測" },
      { tokens: 0, note: null },
    ];
    const rows: ProductRow[] = usages.map((_, index) => ({
      run_id: `u${index}`,
      question: `合成の質問 ${index}`,
      status: "ok",
      by: "me",
    }));
    render(
      <RunHistoryTable
        rows={rows}
        toItem={(row) => ({ ...toItem(row), usage: usages[Number(row.run_id.slice(1))] })}
        onOpen={() => undefined}
        showUsage
      />
    );
    const cells = Array.from(container.querySelectorAll("tbody tr")).map((tr) => tr.lastElementChild);
    expect(cells.map((cell) => cell?.textContent)).toEqual(["12,345 token一部計測", "未計測", "0 token"]);
    expect(cells[0]?.querySelector("[data-run-usage-note]")?.textContent).toBe("一部計測");
    expect(cells[1]?.querySelector("[data-run-usage-note]")?.textContent).toBe("未計測");
    // 右寄せ・等幅数字で、狭い置き場所（56rem 未満）では出さない列。
    expect(cells[0]?.className).toContain("tnum");
    expect(cells[0]?.className).toContain("hidden");
  });

  it("文言は部分で上書きできる（Agent の「依頼」など）", () => {
    render(
      <RunHistoryTable rows={ROWS} toItem={toItem} onOpen={() => undefined} labels={{ columns: { title: "依頼" } }} />
    );
    expect(container.querySelector("thead th")?.textContent).toBe("依頼");
    expect(container.querySelectorAll("thead th")[1].textContent).toBe("状態");
  });
});

describe("RunHistoryToolbar", () => {
  it("絞り込みを 状態 → 種類 → 期間 → 実行者 の順に FilterSelect で並べ、既定と違う値の数で「条件をクリア」を出す", () => {
    const onClear = vi.fn();
    render(
      <RunHistoryToolbar
        status={{ value: "failed", options: ["succeeded", "failed"], onChange: () => {} }}
        kind={{ value: "", options: [{ value: "", label: "すべて" }, { value: "chat", label: "チャット" }], onChange: () => {} }}
        period={{ value: DEFAULT_RUN_PERIOD, onChange: () => {} }}
        executor={{ value: "all", onChange: () => {} }}
        onClearFilters={onClear}
        testId="runs"
      />
    );
    const selects = [...container.querySelectorAll<HTMLElement>("[data-filter-active]")];
    expect(selects.map((el) => el.dataset.filterActive)).toEqual(["true", "false", "false", "false"]);
    expect(container.textContent).toContain("状態");
    expect(container.textContent).toContain("失敗");
    expect(container.querySelector("[data-list-toolbar]")?.getAttribute("data-list-toolbar-filters-layout")).toBeTruthy();
    const clear = container.querySelector<HTMLButtonElement>("[data-list-toolbar-clear]");
    expect(clear).not.toBeNull();
    act(() => clear!.click());
    expect(onClear).toHaveBeenCalledTimes(1);
  });

  it("絞り込みが既定のままなら「条件をクリア」を出さない", () => {
    render(
      <RunHistoryToolbar
        status={{ value: "all", options: ["succeeded"], onChange: () => {} }}
        period={{ value: DEFAULT_RUN_PERIOD, onChange: () => {} }}
        onClearFilters={() => {}}
      />
    );
    expect(container.querySelector("[data-list-toolbar-clear]")).toBeNull();
    expect(container.querySelectorAll("[data-filter-active]")).toHaveLength(2);
    expect(container.textContent).toContain("過去 7 日");
  });

  it("期間の既定は 3 製品で共通の過去 7 日で、「すべて」を選ぶと既定からの変更として数える（#3386）", () => {
    expect(DEFAULT_RUN_PERIOD).toBe("7d");
    render(<RunHistoryToolbar period={{ value: "all", onChange: () => {} }} onClearFilters={() => {}} />);
    expect(container.querySelector("[data-list-toolbar-clear]")).not.toBeNull();
    expect(container.querySelector<HTMLElement>("[data-filter-active]")?.dataset.filterActive).toBe("true");
  });

  it("種類の既定は defaultValue（品質評価以外）を基準にし、「すべて」を選ぶと絞り込みとして数える（#1983）", () => {
    const options = [
      { value: "not_evaluation", label: "品質評価以外" },
      { value: "all", label: "すべて" },
      { value: "evaluation", label: "品質評価" },
    ];
    render(
      <RunHistoryToolbar
        kind={{ value: "not_evaluation", defaultValue: "not_evaluation", options, onChange: () => {} }}
        onClearFilters={() => {}}
      />
    );
    expect(container.querySelector("[data-list-toolbar-clear]")).toBeNull();
    expect(container.querySelector<HTMLElement>("[data-filter-active]")?.dataset.filterActive).toBe("false");
    expect(container.textContent).toContain("品質評価以外");

    render(
      <RunHistoryToolbar
        kind={{ value: "all", defaultValue: "not_evaluation", options, onChange: () => {} }}
        onClearFilters={() => {}}
      />
    );
    expect(container.querySelector("[data-list-toolbar-clear]")).not.toBeNull();
    expect(container.querySelector<HTMLElement>("[data-filter-active]")?.dataset.filterActive).toBe("true");
  });
});

describe("RunHistoryPeriodEmptyState（#3386）", () => {
  it("期間の中に実行が無いことと、期間を「すべて」にする操作を出す", () => {
    const onShowAll = vi.fn();
    render(<RunHistoryPeriodEmptyState period="7d" onShowAll={onShowAll} testId="runs-period-empty" />);
    const root = container.querySelector<HTMLElement>("[data-testid='runs-period-empty']");
    expect(root?.dataset.runHistoryPeriodEmpty).toBe("7d");
    expect(container.textContent).toContain("過去 7 日の実行はありません");
    expect(container.textContent).toContain("期間を「すべて」にすると表示します");
    const button = container.querySelector<HTMLButtonElement>("[data-testid='runs-period-empty-show-all']");
    expect(button?.textContent).toContain("すべての期間を表示");
    act(() => button!.click());
    expect(onShowAll).toHaveBeenCalledTimes(1);
  });

  it("期間の名前と文言は製品の文言で上書きできる", () => {
    render(
      <RunHistoryPeriodEmptyState period="30d" onShowAll={() => {}} labels={{ period: { "30d": "直近 30 日" }, showAll: "全期間" }} />
    );
    expect(container.textContent).toContain("直近 30 日の実行はありません");
    expect(container.textContent).toContain("全期間");
  });
});

describe("RunSummary", () => {
  it("事実は共通の順に並べ、記録の無い事実は出さない。実行 ID は最後", () => {
    render(
      <RunSummary
        status="succeeded"
        badges={<span>合成の追加バッジ</span>}
        question={"先月の売上は？\n地域別に"}
        startedAt="2026-10-15T00:12:05.000Z"
        durationMs={3200}
        kind="チャット"
        executor={{ name: "管理者" }}
        owner={{ name: "山田", isSelf: true }}
        target={{ label: "業務プロファイル", value: "販売分析" }}
        model="openai.gpt-oss-120b"
        usage={{ tokens: 2000 }}
        extraFacts={[{ id: "rows", label: "行数", value: "12" }]}
        runId="run-123"
        testId="summary"
      />
    );
    const facts = Array.from(container.querySelectorAll("[data-run-fact]")).map((node) => node.getAttribute("data-run-fact"));
    expect(facts).toEqual(["startedAt", "duration", "kind", "executor", "owner", "target", "model", "tokens", "rows", "runId"]);
    expect(container.querySelector("[data-run-fact='owner'] dd")?.textContent).toBe("自分");
    expect(container.querySelector("[data-run-fact='target'] dt")?.textContent).toBe("業務プロファイル");
    expect(container.querySelector("[data-run-question]")?.textContent).toContain("地域別に");
    expect(container.querySelector("[data-run-status]")).toBeNull();
    expect(container.querySelector("[data-run-summary-heading]")?.textContent).toBe("概要");
    expect(container.textContent).not.toContain("合成の追加バッジ");
  });

  it("token の計測の状態の注記は数の後ろに括弧で、数が無ければ注記だけを出す（#3419）", () => {
    const tokens = () => container.querySelector("[data-run-fact='tokens'] dd")?.textContent;
    const summary = (usage: { tokens?: number | null; note?: string | null } | null) => (
      <RunSummary status="succeeded" question="合成の質問" startedAt="2026-10-15T00:12:05.000Z" usage={usage} runId="run-1" />
    );
    render(summary({ tokens: 1200, note: "一部計測" }));
    expect(tokens()).toBe("1,200 token（一部計測）");
    render(summary({ tokens: null, note: "未計測" }));
    expect(tokens()).toBe("未計測");
    // LLM を呼ばなかった実行の実測の 0 は 0 と出す（注記なし）。
    render(summary({ tokens: 0 }));
    expect(tokens()).toBe("0 token");
    render(summary(null));
    expect(container.querySelector("[data-run-fact='tokens']")).toBeNull();
  });
});

describe("取消の事実（#3348）", () => {
  it("取り消した人と入口を 3 製品で同じ形にする", () => {
    expect(formatRunCancellation({ via: "screen", actor: { name: "管理者" } })).toBe("管理者 · 画面");
    expect(formatRunCancellation({ via: "mcp", actor: { name: "管理者" }, apiKeyName: "合成のキー" })).toBe(
      "管理者 · MCP（合成のキー）"
    );
    // 見られないキー・削除済みのキーは名前を渡さない（「MCP」だけ）。
    expect(formatRunCancellation({ via: "mcp", actor: { name: "管理者" }, apiKeyName: " " })).toBe("管理者 · MCP");
    expect(formatRunCancellation({ via: "agentRun", actor: { name: "利用者 A", isSelf: true } })).toBe(
      "自分 · Agent の実行から"
    );
    expect(formatRunCancellation({ via: "system", actor: { name: "無視する" } })).toBe("システム");
    expect(formatRunCancellation({ via: "screen" })).toBe("画面");
    expect(
      formatRunCancellation(
        { via: "mcp", actor: { name: "x", isSelf: true }, apiKeyName: "k" },
        { self: "me", mcpKey: (name) => `MCP [${name}]`, separator: " / " }
      )
    ).toBe("me / MCP [k]");
  });

  it("概要は取り消した実行だけに「取消」を会話の持ち主の後に出す", () => {
    render(
      <RunSummary
        status="cancelled"
        question="合成の質問"
        startedAt="2026-10-15T00:12:05.000Z"
        executor={{ name: "利用者 A" }}
        cancellation={{ via: "mcp", actor: { name: "管理者" }, apiKeyName: "合成のキー" }}
        target={{ label: "業務プロファイル", value: "プロファイル A" }}
        runId="run-1"
      />
    );
    const facts = Array.from(container.querySelectorAll("[data-run-fact]")).map((node) => node.getAttribute("data-run-fact"));
    expect(facts).toEqual(["startedAt", "duration", "executor", "cancellation", "target", "runId"]);
    expect(container.querySelector("[data-run-fact='cancellation'] dt")?.textContent).toBe("取消");
    expect(container.querySelector("[data-run-fact='cancellation'] dd")?.textContent).toBe("管理者 · MCP（合成のキー）");

    render(
      <RunSummary
        status="cancelled"
        question="合成の質問"
        startedAt="2026-10-15T00:12:05.000Z"
        cancellation={{ via: "screen", actor: { name: "x", isSelf: true } }}
        runId="run-1"
        labels={{ self: "本人", facts: { cancellation: "Cancelled" }, cancellation: { screen: "UI" } }}
      />
    );
    expect(container.querySelector("[data-run-fact='cancellation'] dt")?.textContent).toBe("Cancelled");
    expect(container.querySelector("[data-run-fact='cancellation'] dd")?.textContent).toBe("本人 · UI");
  });
});

describe("RunTimeline", () => {
  it("操作画面と履歴の既定は共通の小さい文字とアイコンで描く", () => {
    render(<RunTimelineSection title="処理の経過" steps={[{id: "a", label: "合成の工程", status: "done", details: {fields: [{label: "結果", value: "完了"}]}}]} />);
    const label = container.querySelector("[data-run-timeline-label]");
    expect(label?.closest("div")?.className).toContain("text-xs");
    expect(container.querySelector('[data-run-progress-summary]')?.className).toContain("text-xs");
    expect(container.querySelector('[data-run-timeline-toggle] svg')?.getAttribute("width")).toBe("12");
  });

  it("時刻のある段は実際の位置、無い段は前の段の後ろに積む", () => {
    const steps: RunTimelineStep[] = [
      { id: "a", label: "A", status: "done", startedAt: "2026-10-15T00:00:00.000Z", finishedAt: "2026-10-15T00:00:01.000Z" },
      { id: "b", label: "B", status: "done", startedAt: "2026-10-15T00:00:01.000Z", finishedAt: "2026-10-15T00:00:04.000Z" },
    ];
    expect(runTimelineBars(steps)).toEqual([
      { offset: 0, width: 25 },
      { offset: 25, width: 75 },
    ]);
    const sequential: RunTimelineStep[] = [
      { id: "a", label: "A", status: "done", durationMs: 1000 },
      { id: "b", label: "B", status: "done", durationMs: 0 },
      { id: "c", label: "C", status: "done", durationMs: 3000 },
      { id: "d", label: "D", status: "pending" },
    ];
    expect(runTimelineBars(sequential)).toEqual([
      { offset: 0, width: 25 },
      { offset: 25, width: 0.5 },
      { offset: 25, width: 75 },
      null,
    ]);
    expect(runTimelineBars([{ id: "x", label: "X", status: "pending" }])).toEqual([null]);
  });

  it("時刻の無い入れ子の段は、親の段の始まりから積む（#2265）", () => {
    const nested: RunTimelineStep[] = [
      { id: "a", label: "A", status: "done", durationMs: 1000 },
      { id: "b", label: "B", status: "done", durationMs: 3000 },
      { id: "b1", label: "B1", status: "done", durationMs: 1000, level: 1 },
      { id: "b2", label: "B2", status: "done", durationMs: 2000, level: 1 },
      { id: "b2a", label: "B2a", status: "done", durationMs: 1000, level: 2 },
      { id: "c", label: "C", status: "done", durationMs: 0 },
    ];
    expect(runTimelineBars(nested)).toEqual([
      { offset: 0, width: 25 },
      { offset: 25, width: 75 },
      { offset: 25, width: 25 },
      { offset: 50, width: 50 },
      { offset: 50, width: 25 },
      { offset: 100, width: 0.5 },
    ]);
    render(<RunTimeline steps={nested} showSummary={false} size="sm" stepTestIdPrefix="t" testId="timeline" />);
    expect(container.textContent).not.toContain("合計");
    expect(container.querySelector("[data-testid='t-step-b2a']")?.getAttribute("data-level")).toBe("2");
    expect(container.querySelector<HTMLElement>("[data-testid='t-step-b2a'] [data-run-timeline-label]")?.closest("div")?.style.paddingInlineStart).toBe("2rem");
    expect(container.querySelector("[data-testid='t-step-a'] .text-xs")).not.toBeNull();
    render(<RunTimeline steps={nested} showBars={false} testId="timeline" />);
    expect(container.querySelector("[data-run-timeline-bar]")).toBeNull();
  });

  it("段は名前・所要時間を文字で出し、失敗した段は最初から開き、開閉は aria-expanded で伝える", () => {
    render(
      <RunTimeline
        steps={[
          { id: "plan", label: "計画", status: "done", durationMs: 1200 },
          { id: "tool", label: "ツール: search", status: "done", durationMs: 800, level: 1, children: <p>引数と結果</p> },
          { id: "sql", label: "SQL を実行", status: "failed", durationMs: 300, children: <p>ORA-00942</p> },
        ]}
        testId="timeline"
      />
    );
    expect(container.textContent).toContain("3 ステップ・合計 1.5 秒");
    const buttons = container.querySelectorAll<HTMLButtonElement>("button[aria-expanded]");
    expect(buttons).toHaveLength(2);
    expect(buttons[0].getAttribute("aria-expanded")).toBe("false");
    expect(buttons[1].getAttribute("aria-expanded")).toBe("true");
    expect(container.textContent).toContain("ORA-00942");
    expect(container.textContent).not.toContain("引数と結果");
    // 状態は完了も文字で出す（色だけにしない）。
    expect(container.querySelector("[data-run-timeline-step='sql']")?.textContent).toContain("失敗 · 0.3 秒");
    expect(container.querySelector("[data-run-timeline-step='plan'] [data-run-step-status-duration]")?.textContent).toContain("完了");
    // 入れ子の段は字下げする。時間の線は飾り（aria-hidden）。
    expect(container.querySelector<HTMLElement>("[data-run-timeline-step='tool'] [data-run-timeline-label]")?.closest("div")?.style.paddingInlineStart).toBe("1rem");
    expect(container.querySelector<HTMLElement>("[data-run-timeline-step='plan'] [data-run-timeline-label]")?.closest("div")?.style.paddingInlineStart).toBe("0rem");
    expect(container.querySelector("[data-run-timeline-bar]")?.getAttribute("aria-hidden")).toBe("true");
    act(() => buttons[0].click());
    expect(buttons[0].getAttribute("aria-expanded")).toBe("true");
    expect(container.textContent).toContain("引数と結果");
    const region = document.getElementById(buttons[0].getAttribute("aria-controls") ?? "");
    expect(region?.hidden).toBe(false);
  });
});

describe("RunTimeline の実行中の段のアイコン（#3436）", () => {
  const icon = (id: string) => {
    const row = container.querySelector(`[data-run-timeline-step='${id}']`);
    if (row?.querySelector("svg.animate-spin")) return "spinner";
    const svg = row?.querySelector(".pr-icon-line svg");
    return svg?.getAttribute("class")?.match(/lucide-([a-z0-9-]+)/g)?.find((name) => name !== "lucide-icon") ?? "none";
  };
  const steps: RunTimelineStep[] = [
    { id: "root", label: "全体", status: "running" },
    { id: "search", label: "検索", status: "running", level: 1 },
    { id: "embed", label: "埋め込み", status: "done", durationMs: 100, level: 2 },
    { id: "vector", label: "ベクトル検索", status: "running", level: 2 },
    { id: "failed", label: "失敗した工程", status: "failed", durationMs: 10, level: 1 },
    { id: "skipped", label: "飛ばした工程", status: "skipped", level: 1 },
    { id: "pending", label: "未実行の工程", status: "pending", level: 1 },
  ];

  it("子の段を持つかは直後の段の level で決める（字下げの上限に関わらない）", () => {
    expect(runTimelineHasChildren(steps)).toEqual([true, true, false, false, false, false, false]);
    expect(runTimelineHasChildren([{ level: 3 }, { level: 5 }, { level: 5 }])).toEqual([true, false, false]);
    expect(runTimelineHasChildren([])).toEqual([]);
  });

  it("実行中の親の工程は CirclePlay、実行中の末端の段だけを Spinner で回し、終わった段のアイコンは変えない", () => {
    render(<RunTimeline steps={steps} showBars={false} testId="timeline" />);
    expect(icon("root")).toBe("lucide-circle-play");
    expect(icon("search")).toBe("lucide-circle-play");
    expect(icon("vector")).toBe("spinner");
    expect(icon("embed")).not.toBe("spinner");
    expect(icon("failed")).not.toBe("spinner");
    expect(icon("skipped")).not.toBe("spinner");
    expect(icon("pending")).not.toBe("spinner");
    expect(container.querySelectorAll("svg.animate-spin")).toHaveLength(1);
    // 末端の子が終わり、次の子がまだ無い間も親は回さない。
    render(
      <RunTimeline
        steps={steps.map((step) => (step.id === "vector" ? { ...step, status: "done", durationMs: 200 } : step))}
        showBars={false}
        testId="timeline"
      />
    );
    expect(icon("search")).toBe("lucide-circle-play");
    expect(container.querySelectorAll("svg.animate-spin")).toHaveLength(0);
    // spinRunning={false} は末端の段も回さない。
    render(<RunTimeline steps={steps} spinRunning={false} showBars={false} testId="timeline" />);
    expect(icon("vector")).toBe("lucide-circle-play");
    expect(container.querySelectorAll("svg.animate-spin")).toHaveLength(0);
  });
});

describe("入れ子の深さ（#3447）", () => {
  // 製品を跨ぐ trace（ツール → MCP の呼び出し → RAG の検索 → 工程 → 工程の中の段）と、上限より深い段。
  const steps: RunTimelineStep[] = [
    { id: "tool", label: "「根拠の取得」を実行しました", status: "done", durationMs: 900 },
    { id: "mcp", label: "MCP の呼び出し · rag_retrieve_evidence", status: "done", durationMs: 800, level: 1 },
    { id: "rag", label: "検索", status: "done", durationMs: 700, level: 2, product: "RAG" },
    { id: "stage", label: "文書の検索", status: "done", durationMs: 600, level: 3 },
    { id: "embed", label: "埋め込みの作成", status: "done", durationMs: 100, level: 4 },
    { id: "d5", label: "深さ 5", status: "done", durationMs: 50, level: 5 },
    { id: "d6", label: "深さ 6", status: "done", durationMs: 40, level: 6 },
    { id: "d7", label: "深さ 7", status: "done", durationMs: 30, level: 7 },
    { id: "d9", label: "深さ 9", status: "done", durationMs: 20, level: 9 },
  ];
  const nameCell = (id: string) =>
    container.querySelector<HTMLElement>(`[data-run-timeline-step='${id}'] [data-run-timeline-label]`)!.closest("div")!;

  it("深さの上限で丸めず、深い一覧だけにキーボード操作できる内部スクロールを設ける", () => {
    render(<RunTimeline steps={steps} showBars={false} testId="timeline" />);
    const indents = ["tool", "mcp", "rag", "stage", "embed", "d5", "d6", "d7", "d9"].map(
      id => nameCell(id).style.paddingInlineStart
    );
    expect(indents).toEqual(["0rem", "1rem", "2rem", "3rem", "4rem", "5rem", "6rem", "7rem", "9rem"]);
    const viewport = container.querySelector<HTMLElement>("[data-run-timeline-scroll]")!;
    expect(viewport.tabIndex).toBe(0);
    expect(viewport.getAttribute("role")).toBe("region");
    expect(viewport.firstElementChild?.getAttribute("style")).toContain("min-width: 25rem");
    expect(container.querySelector("[data-run-timeline-step='d9']")?.getAttribute("data-level")).toBe("9");
  });

  it("浅い一覧は既存の幅を保ちスクロール用のフォーカスを増やさない", () => {
    render(<RunTimeline steps={steps.slice(0, 5)} showBars={false} />);
    expect(container.querySelector("[data-run-timeline-scroll]")).toBeNull();
    expect(container.querySelector(".overflow-x-auto")).toBeNull();
    expect(container.querySelector("[tabindex='0']")).toBeNull();
  });

  it("製品名は名前の後ろのタグにし、名前の左端を兄弟の段とそろえる", () => {
    render(<RunTimeline steps={steps} showBars={false} testId="timeline" />);
    const row = container.querySelector("[data-run-timeline-step='rag']")!;
    const label = row.querySelector("[data-run-timeline-label]")!;
    const product = row.querySelector("[data-run-timeline-product]")!;
    expect(product.textContent).toBe("RAG");
    // 名前が先、製品名のタグが後ろ（名前の前に文字を置かない）。
    expect(label.compareDocumentPosition(product) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(label.previousSibling).toBeNull();
  });
});

describe("動くスピナーは 1 つ（#3445）", () => {
  // 並行して実行中の末端の段（ベクトル検索と全文検索、並行のツールの呼び出し）がある。
  const steps: RunTimelineStep[] = [
    { id: "plan", label: "進め方の検討", status: "done", durationMs: 1200 },
    { id: "search", label: "文書の検索", status: "running", level: 1 },
    { id: "vector", label: "ベクトル検索", status: "running", level: 2 },
    { id: "text", label: "全文検索", status: "running", level: 2 },
  ];
  // 見えるスピナー（閉じた開閉の中身は数えない。e2e の `svg.animate-spin:visible` と同じ）。
  const spinners = () =>
    [...container.querySelectorAll("svg.animate-spin")].filter((svg) => !svg.closest("details:not([open]) > :not(summary)"));

  it("今の段は最後に始まった実行中の末端の段で、一覧はその段だけを回す", () => {
    expect(runTimelineCurrentStepIndex(steps)).toBe(3);
    expect(runTimelineCurrentStepIndex(steps.slice(0, 2))).toBe(1);
    expect(runTimelineCurrentStepIndex([{ status: "done" }])).toBe(-1);
    render(<RunTimeline steps={steps} showBars={false} testId="timeline" />);
    expect(spinners()).toHaveLength(1);
    expect(container.querySelector("[data-run-timeline-step='text'] svg.animate-spin")).not.toBeNull();
    expect(container.querySelector("[data-run-timeline-step='vector'] .lucide-circle-play")).not.toBeNull();
  });

  it("処理の経過を開いて今の段が回る間は、概要の進行表示を回さない実行中の印にする", () => {
    const startedAt = new Date(Date.now() - 12_000).toISOString();
    render(
      <RunDetailLayout
        summary={<RunJobProgress active label="「根拠の取得」を実行しています" startedAt={startedAt} testId="progress" />}
        timeline={<RunTimelineSection title="処理の経過" steps={steps} active startedAt={startedAt} testId="section" />}
      />
    );
    const progress = container.querySelector<HTMLElement>("[data-testid='progress']");
    // 経過時間は概要の進行表示の 1 か所だけ（処理の経過の見出しに重ねない）。
    expect(container.querySelectorAll("[role='timer']")).toHaveLength(1);
    expect(container.querySelector("[data-testid='section-timer']")).toBeNull();
    // 閉じている間は概要の進行表示だけが回る。
    expect(progress?.dataset.processingPlacement).toBe("job");
    expect(progress?.dataset.processingActivityIcon).toBe("spinner");
    expect(spinners()).toHaveLength(1);
    act(() => container.querySelector<HTMLElement>("[data-testid='section-summary']")?.click());
    expect(progress?.dataset.processingActivityIcon).toBe("static");
    expect(progress?.querySelector("[data-processing-static-icon]")).not.toBeNull();
    expect(spinners()).toHaveLength(1);
    expect(container.querySelector("[data-run-timeline-step='text'] svg.animate-spin")).not.toBeNull();
    // 閉じると進行表示が回る。
    act(() => container.querySelector<HTMLElement>("[data-testid='section-summary']")?.click());
    expect(progress?.dataset.processingActivityIcon).toBe("spinner");
    expect(spinners()).toHaveLength(1);
  });

  it("実行中の見出しは「処理中（N ステップ完了）」と経過時間で、合計の所要時間を出さない", () => {
    const startedAt = new Date(Date.now() - 65_000).toISOString();
    render(<RunTimelineSection title="処理の経過" steps={steps} totalDurationMs={null} active startedAt={startedAt} testId="section" />);
    const summary = container.querySelector<HTMLElement>("[data-testid='section-summary']");
    expect(summary?.textContent).toContain("処理中（1 ステップ完了）");
    expect(summary?.textContent).not.toContain("合計");
    const timer = container.querySelector<HTMLElement>("[data-testid='section-timer']");
    expect(timer?.getAttribute("role")).toBe("timer");
    expect(timer?.getAttribute("aria-label")).toMatch(/^経過時間 01:0[5-6]$/);
    // 終わった実行は今までどおり「N ステップ・合計 M 秒」。
    render(<RunTimelineSection title="処理の経過" steps={steps.slice(0, 1)} totalDurationMs={1200} testId="done" />);
    expect(container.querySelector("[data-testid='done-summary']")?.textContent).toBe("1 ステップ・合計 1.2 秒");
    expect(container.querySelector("[data-testid='done-timer']")).toBeNull();
  });
});

describe("RunTimelineSection（#2473）", () => {
  const steps: RunTimelineStep[] = [
    { id: "plan", label: "計画", status: "done", durationMs: 1200 },
    { id: "tool", label: "ツール: search", status: "done", durationMs: 800, level: 1 },
    { id: "answer", label: "回答の作成", status: "done", durationMs: 300 },
  ];

  it("一部だけの記録は段の一覧の後ろ（段が無ければ empty の後ろ）に案内を出す（#3226）", () => {
    render(<RunTimelineSection title="処理の経過" steps={steps} partial defaultOpen testId="section" />);
    expect(container.querySelector("[data-testid='section-partial']")?.textContent).toBe("一部の処理だけを記録しています。");
    render(<RunTimelineSection title="処理の経過" steps={[]} empty={<p>記録なし</p>} partial testId="empty-section" />);
    expect(container.querySelector("[data-testid='empty-section-partial']")?.textContent).toBe("一部の処理だけを記録しています。");
    render(<RunTimelineSection title="処理の経過" steps={steps} defaultOpen testId="full" />);
    expect(container.querySelector("[data-testid='full-partial']")).toBeNull();
  });

  it("既定で「N ステップ・合計 M 秒」の 1 行に閉じ、開くと段の一覧と処理の詳細を出す", () => {
    render(
      <RunTimelineSection
        title="処理の経過"
        steps={steps}
        totalDurationMs={2000}
        processDetails={<p>処理の詳細の中身</p>}
        testId="section"
      />
    );
    const summary = container.querySelector<HTMLElement>("[data-testid='section-summary']");
    // 段の数は入れ子の段も数える（チャットの処理の経過と同じ）。
    expect(summary?.textContent).toBe("3 ステップ・合計 2.0 秒");
    const details = container.querySelector("details");
    expect(details?.open).toBe(false);
    expect(container.textContent).toContain("処理の経過");
    act(() => summary?.click());
    expect(details?.open).toBe(true);
    expect(container.querySelectorAll("[data-run-timeline-step]")).toHaveLength(3);
    // 開いた中に要約の行を重ねて出さない。処理の詳細はタイムラインの後ろ。
    expect(container.querySelector("[data-run-timeline] > p")?.textContent).toContain("再構成");
    expect(container.textContent).toContain("処理の詳細の中身");
  });

  it("defaultOpen で最初から開き、失敗した段があれば見出しを危険の色にする", () => {
    render(
      <RunTimelineSection
        title="処理の経過"
        steps={[...steps, { id: "sql", label: "SQL を実行", status: "failed", durationMs: 100 }]}
        defaultOpen
        testId="section"
      />
    );
    expect(container.querySelector("details")?.open).toBe(true);
    const summary = container.querySelector("[data-testid='section-summary']");
    expect(summary?.className).toContain("text-danger-fg");
    // 色だけで伝えない（失敗のアイコンも出す）。
    expect(summary?.querySelectorAll("svg")).toHaveLength(2);
  });

  it("段が無いときは工程の開閉を出さず、空の表示と処理の詳細の開閉を出す", () => {
    render(
      <RunTimelineSection
        title="処理の経過"
        steps={[]}
        empty={<p>段の記録はありません</p>}
        processDetails={<p>処理の詳細の中身</p>}
        testId="section"
      />
    );
    expect(container.querySelector<HTMLDetailsElement>("[data-run-process-details]")?.open).toBe(false);
    expect(container.querySelector("[data-testid='section-summary']")).toBeNull();
    expect(container.textContent).toContain("段の記録はありません");
    expect(container.textContent).toContain("処理の詳細の中身");
  });
});

describe("RunTimeline の段の詳細（#2294）", () => {
  const sql = "SELECT A, B FROM T1 WHERE C = 1";

  it("詳細のある段だけに開閉のボタンを付け、既定で閉じ、開くと見出しと値・行・コードの箱・省略の印を出す", () => {
    render(
      <RunTimeline
        steps={[
          { id: "plain", label: "準備", status: "done", durationMs: 100 },
          { id: "empty", label: "空の詳細", status: "done", durationMs: 100, details: { fields: [], lines: [""] } },
          {
            id: "gen",
            label: "SQL を生成しました",
            status: "done",
            durationMs: 900,
            details: {
              fields: [{ label: "参照した表", value: "T1", mono: true }],
              lines: ["警告の行"],
              code: [{ label: "生成した SQL", language: "sql", text: sql }],
              truncated: true,
            },
          },
        ]}
        stepTestIdPrefix="tl"
        testId="timeline"
      />
    );
    expect(container.querySelector("[data-testid='tl-step-plain-toggle']")).toBeNull();
    expect(container.querySelector("[data-testid='tl-step-empty-toggle']")).toBeNull();
    const toggle = container.querySelector<HTMLButtonElement>("[data-testid='tl-step-gen-toggle']");
    expect(toggle).not.toBeNull();
    expect(toggle?.getAttribute("aria-expanded")).toBe("false");
    const region = container.querySelector<HTMLElement>("[data-testid='tl-step-gen-details']");
    expect(region?.id).toBe(toggle?.getAttribute("aria-controls"));
    expect(region?.hidden).toBe(true);
    expect(region?.textContent).toBe("");

    act(() => toggle?.click());
    expect(toggle?.getAttribute("aria-expanded")).toBe("true");
    expect(region?.hidden).toBe(false);
    expect(region?.querySelector("dt")?.textContent).toBe("参照した表");
    expect(region?.querySelector("dd")?.className).toContain("font-mono");
    expect(region?.querySelector("li")?.textContent).toBe("警告の行");
    const pre = region?.querySelector("pre");
    expect(pre?.textContent).toBe(sql);
    expect(pre?.getAttribute("tabindex")).toBe("0");
    expect(pre?.getAttribute("aria-label")).toBe("生成した SQL");
    expect(pre?.getAttribute("data-language")).toBe("sql");
    expect(pre?.className).toContain("overflow-auto");
    expect(region?.querySelector("[data-run-step-details-truncated]")?.textContent).toContain("先頭だけ");

    act(() => toggle?.click());
    expect(toggle?.getAttribute("aria-expanded")).toBe("false");
    expect(region?.hidden).toBe(true);
  });

  it.each([
    { size: "md" as const, text: "text-sm", px: "14" },
    { size: "sm" as const, text: "text-xs", px: "12" },
  ])("開閉の印は行の文字の大きさ（$size: $px px）にそろえ、行の 1 行目の縦の中央に置く（#2338）", ({ size, text, px }) => {
    render(
      <RunTimeline
        steps={[{ id: "gen", label: "生成", status: "done", durationMs: 100, details: { lines: ["行"] } }]}
        size={size}
        stepTestIdPrefix="tl"
      />
    );
    const toggle = container.querySelector<HTMLButtonElement>("[data-testid='tl-step-gen-toggle']");
    expect(toggle?.className).toContain("pr-touch-target");
    expect(toggle?.parentElement?.className).toContain(text);
    const chevron = toggle?.querySelector("svg");
    expect(chevron?.getAttribute("width")).toBe(px);
    expect(chevron?.getAttribute("height")).toBe(px);
    // 印の箱は文字の 1 行目の行の高さ（.pr-icon-line = 1lh）で、印をその縦の中央に置く（#2371）。
    expect(chevron?.parentElement?.className).toContain("pr-icon-line");
    // 状態のアイコンも同じ大きさ・同じ置き場所。
    const status = toggle?.parentElement?.querySelector(":scope > .pr-icon-line > svg");
    expect(status?.getAttribute("width")).toBe(px);
  });

  it("失敗した段の詳細は最初から開き、詳細と自由な中身を両方出す。印の文言は labels で変えられる", () => {
    render(
      <RunTimeline
        steps={[
          {
            id: "tool",
            label: "ツール",
            status: "failed",
            details: { fields: [{ label: "エラー", value: "失敗しました" }], truncated: true },
            children: <p>結果の表</p>,
          },
        ]}
        labels={{ stepDetailsTruncated: "一部だけ" }}
        stepTestIdPrefix="tl"
      />
    );
    expect(container.querySelector("[data-testid='tl-step-tool-toggle']")?.getAttribute("aria-expanded")).toBe("true");
    const region = container.querySelector("[data-testid='tl-step-tool-details']");
    expect(region?.textContent).toContain("失敗しました");
    expect(region?.textContent).toContain("結果の表");
    expect(region?.textContent).toContain("一部だけ");
  });

  it("JSON の整形は上限で切り、印を返す。中身の判定は空の値を数えない", () => {
    expect(runStepDetailsJson({ a: 1 })).toEqual({ text: '{\n  "a": 1\n}', truncated: false });
    const long = runStepDetailsJson({ text: "x".repeat(50) }, 10);
    expect(long.truncated).toBe(true);
    expect(long.text).toHaveLength(11);
    expect(hasRunStepDetails(undefined)).toBe(false);
    expect(hasRunStepDetails({ code: [{ text: "" }] })).toBe(false);
    expect(hasRunStepDetails({ lines: ["a"] })).toBe(true);
  });
});

describe("RunDetailLayout / RunDetailSection", () => {
  it("節の順を固定する（概要 → 処理の経過 → 回答 → 根拠 → フィードバック → 監査ログ。#2126 / #2325）", () => {
    render(
      <RunDetailLayout
        technical={<p>T</p>}
        feedback={<p>F</p>}
        evidence={<p>E</p>}
        timeline={<p>P</p>}
        answer={<p>A</p>}
        summary={<p>S</p>}
      />
    );
    expect(container.querySelector("[data-run-detail]")?.textContent).toBe("SPAEFT");
  });

  it("履歴の trace は処理の詳細の中に置き、両方の開閉を既定で閉じる", () => {
    render(<RunTimelineSection title="処理の経過" steps={[]} traceSummary={{spans: []}} processDetails={<p>診断情報</p>} />);
    const details = container.querySelector<HTMLDetailsElement>("[data-run-process-details]");
    const trace = container.querySelector('[data-testid="trace-details-toggle"]');
    expect(details?.open).toBe(false);
    expect(details?.contains(trace!)).toBe(true);
    expect(trace?.closest("details")?.open).toBe(false);
  });

  it("単一の trace には実行区間の余分な開閉を作らず、複数区間では区別する", async () => {
    render(<TraceDetails summary={{spans: []}} />);
    await act(async () => container.querySelector<HTMLElement>('[data-testid="trace-details-toggle"]')?.click());
    expect(container.querySelector('[data-testid="trace-details"]')?.textContent).not.toContain("実行区間");
    render(<TraceDetails summary={[{spans: []}, {spans: []}]} />);
    expect(container.querySelector('[data-testid="trace-details"]')?.textContent).toContain("実行区間 1/2");
    expect(container.querySelector('[data-testid="trace-details"]')?.textContent).toContain("実行区間 2/2");
  });

  it("処理の詳細（RunProcessDetails）は処理の経過の節の中に既定で閉じて置く、見出しの既定は「処理の詳細」（#2325）", () => {
    render(
      <RunDetailSection title="処理の経過" testId="timeline">
        <p>steps</p>
        <RunProcessDetails testId="details" summaryTestId="details-summary" description="補足">
          <p>raw</p>
        </RunProcessDetails>
      </RunDetailSection>
    );
    const details = container.querySelector<HTMLDetailsElement>("[data-testid='details']");
    expect(details?.tagName).toBe("DETAILS");
    expect(details?.open).toBe(false);
    expect(container.querySelector("[data-testid='timeline']")?.contains(details!)).toBe(true);
    expect(container.querySelector("[data-testid='details-summary']")?.textContent).toContain("処理の詳細");
    expect(details?.textContent).toContain("補足");
  });

  it("処理の識別子は記録順に表示し、欠けた値にコピー操作を作らない", () => {
    render(<RunProcessDetails traceId="0123456789abcdef0123456789abcdef" metadata={{ conversationId: null, messageId: "synthetic-answer" }}>
      <RunUsedModels models={[]} />
    </RunProcessDetails>);
    const rows = Array.from(container.querySelectorAll("[data-process-identifier]"));
    expect(rows.map(row => row.getAttribute("data-process-identifier"))).toEqual(["Trace ID", "会話 ID", "回答メッセージ ID"]);
    expect(rows[0].querySelector("button")?.getAttribute("aria-label")).toBe("Trace ID をコピー");
    expect(rows[1].textContent).toContain("—");
    expect(rows[1].querySelector("button")).toBeNull();
    expect(rows[2].querySelector("button")?.getAttribute("aria-label")).toBe("回答メッセージ ID をコピー");
    expect(container.querySelector("[data-run-used-models]")?.textContent).toContain("使用したモデル—");
  });

  it("使用モデルは記録した呼び出しから補い、同じモデルを重複表示しない", () => {
    render(<RunUsedModels models={[{label: "LLM", value: "synthetic-model"}]} events={[
      {type: "step", attributes: {"gen_ai.request.model": "synthetic-model"}},
      {type: "step", attributes: {"gen_ai.request.model": "synthetic-embed", "gen_ai.operation.name": "embeddings"}},
    ]} />);
    expect(Array.from(container.querySelectorAll("dd")).map(node => node.textContent)).toEqual(["synthetic-model", "synthetic-embed"]);
  });

  it("開閉できる節は既定で閉じた Disclosure、通常の節はカード", () => {
    render(
      <>
        <RunDetailSection title="技術的な詳細" collapsible testId="tech">
          <p>raw</p>
        </RunDetailSection>
        <RunDetailSection title="回答" testId="answer">
          <p>本文</p>
        </RunDetailSection>
      </>
    );
    const tech = container.querySelector<HTMLDetailsElement>("[data-testid='tech']");
    expect(tech?.tagName).toBe("DETAILS");
    expect(tech?.open).toBe(false);
    expect(container.querySelector("[data-testid='answer'] h2")?.textContent).toBe("回答");
  });

  it("開閉できる節は open / onOpenChange で受控にできる（開いたときだけ中身を読む節）", () => {
    const onOpenChange = vi.fn();
    render(
      <RunDetailSection title="監査ログ" collapsible open={false} onOpenChange={onOpenChange} testId="audit">
        <p>audit</p>
      </RunDetailSection>
    );
    const audit = container.querySelector<HTMLDetailsElement>("[data-testid='audit']");
    expect(audit?.open).toBe(false);
    act(() => audit?.querySelector("summary")?.click());
    expect(onOpenChange).toHaveBeenCalledWith(true);
  });
});
