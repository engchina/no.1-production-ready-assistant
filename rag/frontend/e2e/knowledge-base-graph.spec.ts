import { expect, type Page, test } from "./fixtures/test";
import { expectedControlHeight, expectNoPageOverflow, mockDatabaseReady, mockLocalAuth } from "./_helpers";

function ok(json: unknown) {
  return { data: json, error_messages: [], warning_messages: [] };
}

const nullIngestion = {
  preprocess_profile: null,
  parser_adapter_backend: null,
  parser_docling_enabled: null,
  parser_unstructured_enabled: null,
  chunking_strategy: null,
  chunk_size: null,
  chunk_overlap: null,
  chunk_min_chars: null,
  chunk_context_header_enabled: null,
  graph_profile: null,
  field_extraction_enabled: null,
  vision_enabled: null,
  navigation_summary_enabled: null,
  auto_parse_after_preprocess_enabled: null,
  auto_chunk_after_extract_enabled: null,
  auto_index_after_chunk_enabled: null,
};
const nullQuery = {
  guardrail_policy: null,
};
const kbDetail = {
  id: "kb-1",
  name: "社内規程",
  description: "就業規則",
  status: "ACTIVE",
  default_search_mode: "hybrid",
  document_count: 1,
  indexed_document_count: 1,
  error_document_count: 0,
  searchable_chunk_count: 10,
  created_at: "2026-06-15T00:00:00Z",
  updated_at: "2026-06-15T00:00:00Z",
  archived_at: null,
  retrieval_config: {},
  adapter_config: { version: 1, ingestion: nullIngestion, query: nullQuery },
};

async function mockKb(
  page: Page,
  graph: unknown,
  warnings: () => string[] = () => []
): Promise<void> {
  await page.route("**/api/documents**", (route) =>
    route.fulfill({ json: ok({ items: [], total: 0, limit: 50, offset: 0, has_next: false }) })
  );
  // generic を先に、具体ルートを後に登録(Playwright は後勝ち)。
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill({ json: ok({ items: [kbDetail], total: 1, limit: 20, offset: 0, has_next: false }) })
  );
  await page.route("**/api/knowledge-bases/kb-1/graph**", (route) =>
    route.fulfill({ json: { ...ok(graph), warning_messages: warnings() } })
  );
  await page.route("**/api/knowledge-bases/kb-1", (route) => route.fulfill({ json: ok(kbDetail) }));
  // KB の抽出する項目（#548）。上の wildcard の一覧を返さない。
  await page.route("**/api/knowledge-bases/kb-1/extraction-fields", (route) =>
    route.fulfill({ json: ok({ inherits_default: true, fields: [] }) })
  );
}

// 文書構造グラフの開閉の見出し（共有の Disclosure の <summary>。#1135）。
function disclosureSummary(page: Page, name: string) {
  return page.locator("summary", { hasText: name });
}

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
});

test("文書構造グラフを展開して entity ノードを表示する", async ({ page }) => {
  await mockKb(page, {
    status: "ok",
    nodes: [
      { id: "e1", name: "就業規則", type: "concept", confidence: 0.9 },
      { id: "e2", name: "有給休暇", type: "concept", confidence: 0.8 },
    ],
    edges: [{ id: "r1", source: "e1", target: "e2", type: "relates_to", confidence: 0.7 }],
    truncated: false,
  });

  await page.goto("/knowledge-bases/kb-1");
  await disclosureSummary(page, "文書構造グラフを表示").click();

  const graph = page.getByRole("region", { name: "文書構造グラフ" });
  await expect(graph).toBeVisible();
  await expect(graph.getByText("就業規則")).toBeVisible();
  await expect(graph.getByText("有給休暇")).toBeVisible();

  const main = page.locator("main");
  await graph.scrollIntoViewIfNeeded();
  const mainScrollTop = await main.evaluate((element) => element.scrollTop);
  await graph.hover();
  await page.mouse.wheel(0, -600);
  await expect.poll(() => main.evaluate((element) => element.scrollTop)).toBeLessThan(mainScrollTop);
});

test("関係情報が無い KB は空状態を出す", async ({ page }) => {
  await mockKb(page, { status: "empty", nodes: [], edges: [], truncated: false });

  await page.goto("/knowledge-bases/kb-1");
  await disclosureSummary(page, "文書構造グラフを表示").click();

  await expect(page.getByText("文書構造索引がまだありません。")).toBeVisible();
  // 画面に無い操作（再取込）ではなく、文書の詳細の処理レシピの「再処理」を案内する。
  const hint = page.locator("#knowledge-base-graph");
  await expect(hint).toContainText("「再処理」");
  await expect(hint).not.toContainText("再取込");
});

// 開閉の見出しは共有の Disclosure の高さ（lg 40px・タッチ端末 44px）。以前は手書きの min-h-11 で 38.5px（#1135）。
test("文書構造グラフは抽出項目と検索テストの間にあり、標準の高さとキーボードで開閉する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockKb(page, { status: "empty", nodes: [], edges: [], truncated: false });
  await page.goto("/knowledge-bases/kb-1");

  const expected = await expectedControlHeight(page, "lg");
  const summary = disclosureSummary(page, "文書構造グラフを表示");
  await expect(summary).toBeVisible();
  const fields = page.getByRole("heading", { name: "抽出する項目", exact: true });
  const search = page.getByRole("heading", { name: "このナレッジで検索テスト", exact: true });
  await expect(fields).toBeVisible();
  await expect(search).toBeVisible();
  const [fieldsBox, graphBox, searchBox] = await Promise.all([
    fields.boundingBox(), summary.boundingBox(), search.boundingBox(),
  ]);
  expect(fieldsBox!.y + fieldsBox!.height).toBeLessThan(graphBox!.y);
  expect(graphBox!.y + graphBox!.height).toBeLessThan(searchBox!.y);
  expect(Math.round((await summary.boundingBox())?.height ?? 0)).toBe(expected);

  // キーボード（Enter）で開閉でき、開くと見出しの文言が「隠す」になる。
  const graph = disclosureSummary(page, "文書構造グラフを表示");
  await graph.focus();
  await page.keyboard.press("Enter");
  await expect(page.locator("details").filter({ has: page.locator("#knowledge-base-graph") })).toHaveAttribute("open", "");
  await expect(page.getByText("文書構造索引がまだありません。")).toBeVisible();
  await expect(disclosureSummary(page, "文書構造グラフを隠す")).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#knowledge-base-graph")).toHaveCount(0);
  await expect(disclosureSummary(page, "文書構造グラフを表示")).toBeVisible();
  await summary.scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("knowledge-base-section-order.png"), fullPage: false });
});

// DB が止まっていて関係情報を取得できない（backend は空 + warning_messages で縮退する）ときは、
// 「関係情報がまだありません」と区別して、取得できないことと再試行を出す。
for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`関係情報を取得できないときは空と区別して出し、再試行で取り直す (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    let degraded = true;
    let graphRequests = 0;
    page.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/knowledge-bases/kb-1/graph") graphRequests += 1;
    });
    await mockKb(
      page,
      { status: "empty", nodes: [], edges: [], truncated: false },
      () =>
        degraded
          ? ["データベースに接続できませんでした。データベースの起動状態を確認して再試行してください。"]
          : []
    );

    await page.goto("/knowledge-bases/kb-1");
    await disclosureSummary(page, "文書構造グラフを表示").click();

    const section = page.locator("#knowledge-base-graph");
    await expect(section.getByRole("status").filter({ hasText: "データベースに接続できません" })).toBeVisible();
    await expect(section).toContainText("データベースに接続できませんでした。");
    await expect(section.getByText("文書構造索引がまだありません。")).toHaveCount(0);
    await expectNoPageOverflow(page);

    // 復旧したら再試行で取り直し、本当に空なら空の状態を出す。
    degraded = false;
    const before = graphRequests;
    await section.getByRole("button", { name: "再試行" }).click();
    await expect.poll(() => graphRequests).toBeGreaterThan(before);
    await expect(section.getByText("文書構造索引がまだありません。")).toBeVisible();
    await expect(section.getByRole("status").filter({ hasText: "データベースに接続できません" })).toHaveCount(0);
  });
}

// React Flow の Controls・帰属表示・辺のラベルは、テーマの面の色で描く（#1137）。
// React Flow の既定の配色（固定の白い地）ではなく、共有のトークンの色で描く。
// platform/packages/ui の integrations/react-flow.css が React Flow の変数をトークンに結び付ける。
test(`文書構造グラフの操作・帰属表示はテーマの面の色で描く`, async ({
  page,
}) => {
  await mockKb(page, {
    status: "ok",
    nodes: [
      { id: "e1", name: "就業規則", type: "concept", confidence: 0.9 },
      { id: "e2", name: "有給休暇", type: "concept", confidence: 0.8 },
    ],
    edges: [{ id: "r1", source: "e1", target: "e2", type: "relates_to", confidence: 0.7 }],
    truncated: false,
  });

  await page.goto("/knowledge-bases/kb-1");
  await disclosureSummary(page, "文書構造グラフを表示").click();

  const region = page.getByRole("region", { name: "文書構造グラフ" });
  await expect(region.getByText("就業規則")).toBeVisible();

  // トークンの色を同じ場所（図の中）で解決して、React Flow の部品の色と比べる。
  const colors = await region.evaluate((element) => {
    const probe = document.createElement("div");
    element.append(probe);
    const token = (name: string) => {
      probe.style.backgroundColor = `var(${name})`;
      return getComputedStyle(probe).backgroundColor;
    };
    const tokens = {
      surface: token("--color-surface"),
      raised: token("--color-surface-raised"),
      fg: token("--color-fg"),
      fgMuted: token("--color-fg-muted"),
    };
    probe.remove();
    const style = (selector: string) => {
      const target = element.querySelector(selector);
      return target ? getComputedStyle(target) : null;
    };
    const buttons = [...element.querySelectorAll(".react-flow__controls-button")];
    return {
      tokens,
      buttonCount: buttons.length,
      buttonBackgrounds: buttons.map((button) => getComputedStyle(button).backgroundColor),
      buttonColors: buttons.map((button) => getComputedStyle(button).color),
      attributionBackground: style(".react-flow__attribution")?.backgroundColor,
      attributionLink: style(".react-flow__attribution a")?.color,
      edgeLabelBackground: style(".react-flow__edge-textbg")?.fill ?? null,
    };
  });
  expect(colors.buttonCount).toBe(3);
  expect(new Set(colors.buttonBackgrounds)).toEqual(new Set([colors.tokens.raised]));
  expect(new Set(colors.buttonColors)).toEqual(new Set([colors.tokens.fg]));
  // 帰属表示（React Flow のリンク）は消さず、色だけ合わせる。
  expect(colors.attributionBackground).toBe(colors.tokens.raised);
  expect(colors.attributionLink).toBe(colors.tokens.fgMuted);
  if (colors.edgeLabelBackground !== null) {
    expect(colors.edgeLabelBackground).toBe(colors.tokens.surface);
  }
  // 文書構造グラフには辺のラベル（relates_to）がある。
  await expect(region.locator(".react-flow__edge-textbg")).toHaveCount(1);
  await expectNoPageOverflow(page);
});


for (const count of [30, 80]) {
  test(`関係情報 ${count} ノードを全体表示できる`, { tag: "@responsive" }, async ({ page }) => {
    await mockKb(page, {
      status: "ok",
      nodes: Array.from({ length: count }, (_, index) => ({
        id: `node-${index}`, name: `合成見出し ${index}`, type: "section", confidence: 1,
      })),
      edges: Array.from({ length: count - 1 }, (_, index) => ({
        id: `edge-${index}`, source: "node-0", target: `node-${index + 1}`, type: "contains", confidence: 1,
      })),
      truncated: false,
    });
    await page.goto("/knowledge-bases/kb-1");
    await disclosureSummary(page, "文書構造グラフを表示").click();
    const graph = page.getByRole("region", { name: "文書構造グラフ" });
    await expect(graph.locator(".react-flow__node")).toHaveCount(count);
    const fits = () => graph.evaluate((region) => {
      const bounds = region.getBoundingClientRect();
      return [...region.querySelectorAll(".react-flow__node")].every((node) => {
        const box = node.getBoundingClientRect();
        return box.left >= bounds.left && box.right <= bounds.right && box.top >= bounds.top && box.bottom <= bounds.bottom;
      });
    });
    await expect.poll(fits).toBe(true);
    await graph.getByRole("button", { name: "全体表示", exact: true }).click();
    await expect.poll(fits).toBe(true);
    await expect(graph.locator(".react-flow__edge-text").first()).toHaveText("含む");
    await graph.scrollIntoViewIfNeeded();
    await page.screenshot({ path: test.info().outputPath(`graph-${count}.png`) });
    await expectNoPageOverflow(page);
  });
}
