const syntheticChatModel = `synthetic_model_${"segment_".repeat(24)}`;
/** 合成記録の `gen_ai.chat` の行の名前（日本語の名前とモデル名。モデル名は 128 字まで。#3214・#3219）。 */
export const syntheticChatSpanLabel = `モデルの呼び出し · ${syntheticChatModel.slice(0, 128)}`;

/** 合成記録の span（製品の段の span を足すときに使う）。 */
export function syntheticSpan(id: string, parent: string | null, operationName: string, attributes: Record<string, string | number> = {}) {
  return { span_id: id.repeat(16), parent_span_id: parent?.repeat(16) ?? null, operation_name: operationName, trace_id: "a".repeat(32), kind: "internal", status: "done", started_at: "2026-01-01T00:00:00.000Z", finished_at: "2026-01-01T00:00:02.000Z", duration_ms: 2000, timing_source: "measured", attributes };
}

/** 3 製品の実画面で使う、機密を含まない並列呼び出しの合成記録。 */
export const syntheticTraceSummary = {
  schema_version: 1, operation_id: "synthetic-a", root_span_id: "1".repeat(16), telemetry_trace_id: "a".repeat(32), completeness: "complete", truncated: false, sampled: true,
  spans: [
    { span_id: "1".repeat(16), parent_span_id: null, operation_name: "synthetic.execute" },
    { span_id: "2".repeat(16), parent_span_id: "1".repeat(16), operation_name: "gen_ai.chat" },
    { span_id: "3".repeat(16), parent_span_id: "1".repeat(16), operation_name: "gen_ai.execute_tool" },
    { span_id: "4".repeat(16), parent_span_id: "2".repeat(16), operation_name: "synthetic.child" },
    { span_id: "5".repeat(16), parent_span_id: "4".repeat(16), operation_name: "synthetic.child" },
    { span_id: "6".repeat(16), parent_span_id: "5".repeat(16), operation_name: "synthetic.child" },
    { span_id: "7".repeat(16), parent_span_id: "6".repeat(16), operation_name: "synthetic.child" },
  ].map(span => ({ ...span, trace_id: "a".repeat(32), kind: "internal", status: "done", started_at: "2026-01-01T00:00:00.000Z", finished_at: "2026-01-01T00:00:02.000Z", duration_ms: 2000, timing_source: "measured", attributes: { "gen_ai.request.model": span.operation_name === "gen_ai.chat" ? syntheticChatModel : "model-a", "gen_ai.usage.input_tokens": 12, "gen_ai.usage.output_tokens": 0 } })),
};

export async function captureTraceLayouts(
  page: {
    keyboard: { press(key: string): Promise<void> };
    getByTestId(id: string): { focus(): Promise<void>; isVisible(): Promise<boolean>; getByText(text: string, options: { exact: boolean }): { click(): Promise<void> } };
    viewportSize(): { width: number; height: number } | null;
    setViewportSize(size: { width: number; height: number }): Promise<void>;
    evaluate(fn: () => unknown): Promise<unknown>;
    evaluate(fn: (value: string) => unknown, value: string): Promise<unknown>;
    screenshot(options: { path: string; fullPage: boolean }): Promise<unknown>;
  },
  testInfo: { outputPath(name: string): string },
  name: string,
) {
  await page.getByTestId("trace-details-toggle").focus();
  await page.keyboard.press("Space");
  if (await page.getByTestId("trace-details").isVisible()) throw new Error("Space で詳細を閉じられません。");
  await page.keyboard.press("Enter");
  if (!(await page.getByTestId("trace-details").isVisible())) throw new Error("Enter で詳細を開けません。");
  await page.keyboard.press("Tab");
  if (!(await page.evaluate(() => document.activeElement?.closest('[data-testid="trace-details"]') != null))) throw new Error("Tab で詳細の操作へ移れません。");
  await page.getByTestId("trace-details").getByText(syntheticChatSpanLabel, { exact: true }).click();
  const mobile = (page.viewportSize()?.width ?? 1280) <= 375;
  for (const width of mobile ? [375] : [1280, 1920]) {
    await page.setViewportSize({ width, height: mobile ? 812 : 900 });
    {
      await page.evaluate(() => document.fonts.ready);
      if (!(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))) throw new Error("ページに横方向のはみ出しがあります。");
      // Trace ID が折り返しても、コピーのボタンは同じ行に置く（#3219）。
      const sameRow = await page.evaluate(() => [...document.querySelectorAll("[data-trace-id-row]")].every(row => {
        const [text, button] = [row.querySelector("span"), row.querySelector("button")].map(el => el?.getBoundingClientRect());
        return text != null && button != null && button.top < text.bottom && text.top < button.bottom;
      }));
      if (!sameRow) throw new Error("Trace ID とコピーのボタンが別の行にあります。");
      await page.screenshot({ path: testInfo.outputPath(`${name}-${width}.png`), fullPage: true });
    }
  }
}

/**
 * 処理の経過が trace の投影のとき（#3226）の合成の span のイベント（3 製品の e2e で共通）。root（行にしない）・
 * 工程の span（`kind` を製品の定義で名前にする）・モデルの呼び出し・ツールの実行の木。`running` に入れた span は実行中。
 */
export function syntheticSpanEvents(
  targetId: string,
  { stages = [] as { id: string; kind: string; params?: Record<string, string | number | boolean>; parent?: string }[], running = [] as string[], terminal = "done" as "done" | "failed" | "cancelled" | null } = {},
) {
  const trace = "a".repeat(32);
  const at = (seconds: number) => new Date(Date.UTC(2026, 0, 1, 0, 0, seconds)).toISOString();
  const events: Record<string, unknown>[] = [];
  let seq = 0;
  const span = (id: string, parent: string | null, name: string, extra: Record<string, unknown> = {}, index = 0) => {
    const done = !running.includes(id);
    events.push({
      schema_version: 1, seq: ++seq, target_id: targetId, attempt: 0, emitted_at: at(index + 1), type: "step",
      step_id: id.padStart(16, "0").slice(-16), name, trace_id: trace, started_at: at(index),
      ...(parent ? { parent_id: parent.padStart(16, "0").slice(-16) } : { root: true }),
      status: done ? "done" : "running", ...(done ? { finished_at: at(index + 1) } : {}), ...extra,
    });
  };
  span("1", null, "synthetic.execute", {}, 0);
  stages.forEach((stage, index) => span(stage.id, stage.parent ?? "1", "synthetic.stage", { kind: stage.kind, ...(stage.params ? { params: stage.params } : {}) }, index + 1));
  span("c", "1", "gen_ai.chat", { attributes: { "gen_ai.request.model": syntheticChatModel, "gen_ai.usage.input_tokens": 12, "gen_ai.usage.output_tokens": 0 } }, stages.length + 1);
  if (terminal && running.length === 0) events.push({ schema_version: 1, seq: ++seq, target_id: targetId, attempt: 0, emitted_at: at(stages.length + 3), type: "terminal", status: terminal });
  return events;
}

/**
 * 処理の経過（trace の投影。#3226）の画面の幅の確認。「処理の詳細」を開いて Trace ID の行（コピーのボタンが
 * 同じ行）を確かめ、1280 / 1920 / 375px で横あふれが無いことを確かめて撮る（テーマはライトだけ。#17）。
 */
export async function captureSpanProgressLayouts(
  page: {
    locator(selector: string): { first(): { click(): Promise<void>; isVisible(): Promise<boolean> } };
    viewportSize(): { width: number; height: number } | null;
    setViewportSize(size: { width: number; height: number }): Promise<void>;
    evaluate(fn: () => unknown): Promise<unknown>;
    evaluate(fn: (value: string) => unknown, value: string): Promise<unknown>;
    screenshot(options: { path: string; fullPage: boolean }): Promise<unknown>;
  },
  testInfo: { outputPath(name: string): string },
  name: string,
) {
  const details = page.locator("[data-run-process-details] > summary").first();
  if (!(await page.evaluate(() => document.querySelector("[data-trace-id-row]") != null))) await details.click();
  if (!(await page.evaluate(() => document.querySelector("[data-trace-id-row]") != null))) throw new Error("処理の詳細に Trace ID の行がありません。");
  const mobile = (page.viewportSize()?.width ?? 1280) <= 375;
  for (const width of mobile ? [375] : [1280, 1920]) {
    await page.setViewportSize({ width, height: mobile ? 812 : 900 });
    {
      await page.evaluate(() => document.fonts.ready);
      if (!(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth))) throw new Error("ページに横方向のはみ出しがあります。");
      const sameRow = await page.evaluate(() => [...document.querySelectorAll("[data-trace-id-row]")].every(row => {
        const [text, button] = [row.querySelector("span"), row.querySelector("button")].map(el => el?.getBoundingClientRect());
        return text != null && button != null && button.top < text.bottom && text.top < button.bottom;
      }));
      if (!sameRow) throw new Error("Trace ID とコピーのボタンが別の行にあります。");
      if (await page.evaluate(() => document.querySelector('[data-testid="trace-details"]') != null)) throw new Error("処理の経過のほかに「トレースの詳細」が出ています。");
      await page.screenshot({ path: testInfo.outputPath(`${name}-${width}.png`), fullPage: true });
    }
  }
}
