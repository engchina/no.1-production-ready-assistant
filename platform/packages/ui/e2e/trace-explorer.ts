/** 3 製品の実画面で、共有の軌跡の調査を確かめる。 */
// 製品ごとに独立した Playwright を使うため、実行環境と型は呼び出し元から渡す。
interface ExplorerLocator {
  getByRole(role: "button" | "heading" | "searchbox" | "tab" | "combobox" | "option", options: {name: string; exact?: boolean}): ExplorerLocator;
  locator(selector: string): ExplorerLocator;
  first(): ExplorerLocator;
  last(): ExplorerLocator;
  click(): Promise<void>;
  fill(value: string): Promise<void>;
  focus(): Promise<void>;
  press(key: string): Promise<void>;
  screenshot(options: {path: string}): Promise<unknown>;
  isVisible(): Promise<boolean>;
}
interface ExplorerPage extends Pick<ExplorerLocator, "getByRole" | "locator"> {
  screenshot(options: {path: string; fullPage: boolean}): Promise<unknown>;
  viewportSize(): {width: number; height: number} | null;
  setViewportSize(size: {width: number; height: number}): Promise<void>;
  emulateMedia(options: {reducedMotion: "reduce" | "no-preference"}): Promise<void>;
  evaluate<R, A>(fn: (arg: A) => R | Promise<R>, arg: A): Promise<R>;
}
interface ExplorerAssertions {
  toBeVisible(): Promise<void>;
  toBeFocused(): Promise<void>;
  toContainText(value: string): Promise<void>;
  toHaveValue(value: string): Promise<void>;
  toHaveCount(value: number): Promise<void>;
  toHaveAttribute(name: string, value: string): Promise<void>;
  toBe(value: boolean): void;
  toEqual(value: unknown): void;
}

export async function confirmTraceExplorer(page: ExplorerPage, testInfo: {outputPath(...paths: string[]): string}, expect: (value: unknown) => unknown, probeLayout = false) {
  const check = (value: unknown) => expect(value) as ExplorerAssertions;
  const normalRows = await page.evaluate(() => Array.from(document.querySelectorAll("[data-run-timeline-step]")).map(row => ({
    id: row.getAttribute("data-run-timeline-step"), name: row.querySelector("[data-run-timeline-label]")?.textContent,
    level: row.getAttribute("data-level") ?? "0", parent: row.getAttribute("data-parent-id"),
    status: row.getAttribute("data-status"), duration: row.querySelector("[data-run-step-status-duration]")?.textContent, summary: row.querySelector("[data-run-result-summary]")?.textContent ?? "",
  })), undefined);
  check(normalRows.some(row => row.status === "skipped" && row.name === "会話の添付から根拠を選ぶ" && row.duration?.includes("スキップ"))).toBe(true);
  const normalStyle = await page.evaluate(() => {
    const label = document.querySelector("[data-run-timeline-label]");
    const bar = document.querySelector("[data-run-timeline-bar]");
    const style = label ? getComputedStyle(label) : null;
    return { font: style?.fontSize ?? null, family: style?.fontFamily, weight: style?.fontWeight, line: style?.lineHeight, color: style?.color, height: bar ? getComputedStyle(bar).height : null, radius: bar ? getComputedStyle(bar).borderRadius : null };
  }, undefined);
  await page.getByRole("button", {name: "軌跡を調べる", exact: true}).first().click();
  const explorer = page.locator("[data-trace-explorer]");
  await check(explorer).toBeVisible();
  check(await page.evaluate(expected => {
    const labels = Array.from(document.querySelectorAll("[data-trace-record-label]"));
    const bars = Array.from(document.querySelectorAll("[data-trace-time-bar]"));
    return labels.length > 0 && bars.length > 0 && labels.every(label => { const style = getComputedStyle(label); return !expected.font || (style.fontSize === expected.font && style.fontFamily === expected.family && style.fontWeight === expected.weight && style.lineHeight === expected.line && style.color === expected.color); }) && bars.every(bar => (!expected.height || getComputedStyle(bar).height === expected.height) && (!expected.radius || getComputedStyle(bar).borderRadius === expected.radius));
  }, normalStyle)).toBe(true);
  check(await page.evaluate(expected => {
    const rows = Array.from(document.querySelectorAll("[data-trace-record]"));
    const ids = new Set(rows.map(row => row.getAttribute("data-trace-record")));
    const source = expected.filter(row => ids.has(row.id));
    return source.length === rows.length && rows.every((row, i) => row.getAttribute("data-trace-record") === source[i].id
      && row.querySelector("[data-trace-record-label]")?.textContent === source[i].name
      && (row.getAttribute("data-level") ?? "0") === source[i].level && row.getAttribute("data-parent-id") === source[i].parent
      && row.getAttribute("data-status") === source[i].status
      && row.querySelector("[data-trace-step-status-duration]")?.textContent === source[i].duration
      && (row.querySelector("[data-trace-result-summary]")?.textContent ?? "") === source[i].summary);
  }, normalRows)).toBe(true);
  await check(explorer.getByRole("heading", {name: "軌跡の調査"})).toBeFocused();
  const search = explorer.getByRole("searchbox", {name: "呼び出しを検索"});
  await explorer.getByRole("combobox", {name: "種類"}).click();
  await page.getByRole("option", {name: "モデル", exact: true}).click();
  await check(explorer.locator('[data-trace-record]:not([data-trace-category="model"])')).toHaveCount(0);
  await explorer.getByRole("button", {name: "条件をクリア", exact: true}).last().click();
  await explorer.getByRole("combobox", {name: "状態"}).click();
  await page.getByRole("option", {name: "失敗", exact: true}).click();
  await check(explorer).toContainText("条件に一致する呼び出しがありません");
  await explorer.getByRole("button", {name: "条件をクリア", exact: true}).last().click();
  await search.fill("no-synthetic-match");
  await check(explorer).toContainText("条件に一致する呼び出しがありません");
  await explorer.getByRole("button", {name: "条件をクリア", exact: true}).last().click();
  await check(search).toHaveValue("");
  await explorer.getByRole("tab", {name: "階層", exact: true}).focus();
  await explorer.getByRole("tab", {name: "階層", exact: true}).press("ArrowRight");
  await check(explorer.getByRole("tab", {name: "開始時刻順", exact: true})).toHaveAttribute("aria-selected", "true");
  await explorer.getByRole("tab", {name: "階層", exact: true}).click();
  await explorer.screenshot({path: testInfo.outputPath(`trace-explorer-list.png`)});
  await search.fill("synthetic-call-input");
  await check(explorer.locator("[data-trace-record]").first()).toBeVisible();
  await explorer.locator("[data-trace-record] button").first().click();
  check(await page.evaluate(() => {
    const selected = document.querySelector("[data-trace-record] button[aria-pressed=true]")?.closest("[data-trace-record]");
    const marker = document.querySelector("[data-trace-overview-selected=true]");
    return Boolean(selected && marker && selected.getAttribute("data-trace-record") === marker.getAttribute("data-trace-overview-span") && getComputedStyle(marker).outlineStyle !== "none" && parseFloat(getComputedStyle(marker).outlineWidth) > 0);
  }, undefined)).toBe(true);
  const inspector = explorer.locator("[data-trace-inspector]");
  await check(inspector).toBeVisible();
  await inspector.getByRole("tab", {name: "入力", exact: true}).click();
  await check(inspector).toContainText("synthetic-call-input");
  await inspector.getByRole("tab", {name: "結果", exact: true}).click();
  await check(inspector).toContainText("synthetic-call-output");
  await inspector.getByRole("tab", {name: "思考", exact: true}).click();
  await check(inspector.locator("[data-call-readable] details[open] [data-markdown-text]")).toContainText("synthetic-call-thinking");
  await check(inspector.locator("[data-call-readable] details[open] [data-markdown-text]")).toBeVisible();
  await inspector.screenshot({path: testInfo.outputPath(`trace-thinking.png`)});
  await inspector.getByRole("tab", {name: "Schema", exact: true}).click();
  await check(inspector).toContainText("synthetic-call-schema");
  await check(inspector.locator("[data-trace-timing]")).toContainText("開始時刻");
  await check(inspector.locator("[data-trace-technical]")).toContainText("Span name");
  const raw = await page.evaluate(() => document.querySelector("[data-trace-timing] time")?.getAttribute("datetime"), undefined);
  await page.evaluate(() => { Object.defineProperty(navigator, "clipboard", {configurable: true, value: {writeText: async (text: string) => {document.documentElement.dataset.copiedIso = text;}}}); }, undefined);
  await inspector.getByRole("button", {name: "開始時刻: 元の ISO 時刻をコピー", exact: true}).click();
  check(await page.evaluate(() => document.documentElement.dataset.copiedIso, undefined)).toEqual(raw);
  check(await page.evaluate(() => {
    const content = document.querySelector("[data-trace-inspector-content]");
    const text = content?.querySelector("[data-trace-timing] time")?.textContent ?? "";
    return text.includes("(") && !text.includes("T00:") && content?.children[0]?.hasAttribute("data-trace-processing-content") === true
      && content?.children[1]?.hasAttribute("data-trace-timing") === true && content?.children[2]?.hasAttribute("data-trace-technical") === true;
  }, undefined)).toBe(true);
  await inspector.getByRole("tab", {name: "入力", exact: true}).click();
  check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth, undefined)).toBe(true);
  await explorer.screenshot({path: testInfo.outputPath(`trace-explorer-detail.png`)});
  await page.screenshot({path: testInfo.outputPath(`trace-explorer-page.png`), fullPage: true});
  if (probeLayout) {
    const original = page.viewportSize();
    await page.emulateMedia({reducedMotion: "reduce"});
    const sizes = original && original.width >= 1000 ? [{width: 1280, height: 900}, {width: 1920, height: 1080}] : [{width: 812, height: 375}];
    for (const size of sizes) {
      await page.setViewportSize(size);
      check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth, undefined)).toBe(true);
      await explorer.screenshot({path: testInfo.outputPath(`trace-explorer-${size.width}-reduced-motion.png`)});
    }
    if (original) await page.setViewportSize(original);
  }
  const back = inspector.getByRole("button", {name: "一覧に戻る", exact: true});
  if (await back.isVisible()) {
    await back.click();
    await check(explorer.locator('[data-trace-record] button[aria-pressed="true"]')).toBeFocused();
  }
  await explorer.getByRole("button", {name: "条件をクリア", exact: true}).last().click();
  await search.fill("synthetic-stage-detail");
  await explorer.locator('[data-trace-record][data-trace-category="stage"] button').first().click();
  await check(inspector).toContainText("synthetic-stage-detail");
  check(await page.evaluate(() => {
    const content = document.querySelector("[data-trace-inspector-content]");
    const blocks = content?.querySelectorAll(":scope > [data-trace-processing-content], :scope > [data-trace-timing]");
    if (!content || !blocks || blocks.length !== 2) return false;
    const gap = parseFloat(getComputedStyle(content).rowGap);
    const actual = blocks[1].getBoundingClientRect().top - blocks[0].getBoundingClientRect().bottom;
    return gap > 0 && Math.abs(actual - gap) < 1 && blocks[1].textContent?.includes("開始時刻") === true;
  }, undefined)).toBe(true);
  check(await page.evaluate(() => {
    const content = document.querySelector("[data-trace-processing-content]");
    const fields = Array.from(content?.querySelectorAll("dt") ?? []).map(node => node.textContent);
    const result = content?.querySelector("dl");
    return fields[fields.length - 1] === "結果" && result?.nextElementSibling?.tagName === "UL";
  }, undefined)).toBe(true);
  await inspector.screenshot({path: testInfo.outputPath(`trace-stage-timing.png`)});
  await explorer.getByRole("button", {name: "経過に戻る", exact: true}).click();
  await check(page.getByRole("button", {name: "軌跡を調べる", exact: true}).first()).toBeFocused();
}

/** 経過の技術属性を調べる受入も、利用者と同じ軌跡の導線を通す。 */
export async function inspectTraceStep(page: ExplorerPage, timeline: ExplorerLocator, stepId: string) {
  await timeline.getByRole("button", {name: "軌跡を調べる", exact: true}).first().click();
  const explorer = page.locator("[data-trace-explorer]");
  await explorer.getByRole("searchbox", {name: "呼び出しを検索"}).fill(stepId);
  await explorer.locator("[data-trace-record] button").first().click();
}
