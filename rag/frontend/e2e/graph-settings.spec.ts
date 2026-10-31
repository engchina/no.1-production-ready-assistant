import { expect, test, type Page } from "./fixtures/test";
import { expectNoPageOverflow, mockLocalAuth, openSidebarNav } from "./_helpers";

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760, collapse: false },
  { name: "mobile", width: 375, height: 812, collapse: true },
]) {
  test(`文書構造索引の構築設定は「構築しない」「構築する」を表示する (${viewport.name})`, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapse) await collapseSidebar(page);
    await mockGraph(page, "off");

    await page.goto("/settings/graph");

    // ナビ・画面タイトルは「文書構造索引」(検索側の関係検索は検索方法。#301)。
    await expect(page.getByRole("heading", { name: "文書構造索引", level: 1 })).toBeVisible();
    await expect(page.getByRole("heading", { name: "文書構造索引の構築", exact: true })).toBeVisible();
    // 選択肢は 2 つ。既定は色ではなく「既定」の文字で示す(#621)。
    const radios = page.getByRole("radiogroup", { name: "取込のときに作るか" }).getByRole("radio");
    await expect(radios).toHaveCount(2);
    const off = page.getByRole("radio", { name: /構築しない/ });
    await expect(off).toBeChecked();
    await expect(page.locator('label[for="settings-graph-profile-off"]')).toContainText("既定");
    await expect(page.locator('label[for="settings-graph-profile-entities"]')).not.toContainText("既定");
    await expect(page.getByRole("radio", { name: /構築する/ })).toBeVisible();
    // 何に使うか(文書構造グラフと、検索方法で有効にしたときの回答の検索。#2055)を書き、内部の英語の
    // 用語と保存値を出さない。
    const main = page.getByRole("main");
    await expect(main).toContainText("文書構造グラフ");
    await expect(main).toContainText("「文書構造による検索拡張」");
    await expect(main).toContainText("既定は使いません");
    // 取込済みの文書への反映は、文書の詳細の処理レシピの「再処理」（画面の操作の名前）で案内する。
    await expect(main).toContainText("「再処理」してください");
    for (const term of ["entities", "relationships", "claims", "community", "GraphRAG", "現行挙動"]) {
      await expect(main).not.toContainText(term);
    }
    // 見出しは画面のタイトルだけが「文書構造索引」(カードの見出しで繰り返さない)。
    await expect(page.getByRole("heading", { name: "文書構造索引", exact: true })).toHaveCount(1);
    await page.screenshot({ path: test.info().outputPath("document-structure-index.png"), fullPage: true });
    // 375px ではナビがドロワー（#367）。開いて現在地を確かめる。
    await expect((await openSidebarNav(page)).getByRole("link", { name: "文書構造索引" })).toHaveAttribute("aria-current", "page");
    await expectNoHorizontalOverflow(page);
  });
}

test("文書構造索引の構築設定は「構築する」を選んで保存できる", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  let saved: unknown = null;
  await page.route("**/api/settings/graph", async (route) => {
    if (route.request().method() === "PATCH") {
      saved = route.request().postDataJSON();
      await route.fulfill({ json: graphEnvelope("entities") });
      return;
    }
    await route.fulfill({ json: graphEnvelope("off") });
  });

  await page.goto("/settings/graph");

  const build = page.getByRole("radio", { name: /構築する/ });
  await build.click();
  await expect(build).toBeChecked();

  const actions = page.getByRole("group", { name: "文書構造索引の構築の設定の操作" });
  await actions.getByRole("button", { name: "保存" }).click();

  // 保存の成功は Toast（messaging.md §10.2）。操作の行に常設の成功の表示を残さない。
  await expect(page.getByText("文書構造索引の構築設定を保存しました。")).toBeVisible();
  await expect(actions).not.toContainText("保存しました");
  await expect(actions.getByRole("button", { name: "変更を破棄" })).toBeDisabled();
  expect(saved).toEqual({ profile: "entities" });
  await expectNoHorizontalOverflow(page);
});

test("文書構造索引の構築設定の保存に失敗しても未保存の選択を残す (#274)", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await page.route("**/api/settings/graph", async (route) => {
    if (route.request().method() === "PATCH") {
      await route.fulfill({
        status: 500,
        json: {
          data: null,
          error_messages: ["文書構造索引の設定を backend/.env へ保存できませんでした。"],
          warning_messages: [],
        },
      });
      return;
    }
    await route.fulfill({ json: graphEnvelope("off") });
  });

  await page.goto("/settings/graph");

  const build = page.getByRole("radio", { name: /構築する/ });
  await build.click();
  const actions = page.getByRole("group", { name: "文書構造索引の構築の設定の操作" });
  await actions.getByRole("button", { name: "保存" }).click();

  // 失敗は操作の行に出し、選択を残す。変更を破棄で保存値へ戻せる。
  await expect(actions).toContainText("文書構造索引の設定を backend/.env へ保存できませんでした。");
  await expect(build).toBeChecked();
  await expect(actions.getByRole("button", { name: "保存" })).toBeEnabled();
  await actions.getByRole("button", { name: "変更を破棄" }).click();
  await expect(page.getByRole("radio", { name: /構築しない/ })).toBeChecked();
  await expect(actions).not.toContainText("保存できませんでした");
});

test("文書構造索引の構築設定取得に失敗したら再試行できる", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await page.route("**/api/settings/graph", async (route) => {
    await route.fulfill({
      status: 503,
      json: {
        data: null,
        error_messages: ["文書構造索引の構築設定を取得できませんでした。"],
        warning_messages: [],
      },
    });
  });

  await page.goto("/settings/graph");

  await expect(page.getByRole("alert")).toContainText("文書構造索引の構築設定を取得できませんでした。");
  await expect(page.getByRole("button", { name: "再試行" })).toBeVisible();
});

async function collapseSidebar(page: Page) {
  await page.addInitScript(() => {
    window.localStorage.setItem(
      "production-ready-rag.ui",
      JSON.stringify({ state: { sidebarCollapsed: true }, version: 0 })
    );
  });
}

function graphEnvelope(profile: "off" | "entities") {
  return {
    data: {
      profile,
      enabled: profile === "entities",
      profiles: (["off", "entities"] as const).map((name) => ({
        name,
        selected: name === profile,
      })),
      config_source: "runtime",
    },
    error_messages: [],
    warning_messages: [],
  };
}

async function mockGraph(page: Page, profile: "off" | "entities") {
  await page.route("**/api/settings/graph", async (route) => {
    await route.fulfill({ json: graphEnvelope(profile) });
  });
}

async function expectNoHorizontalOverflow(page: Page) {
  // documentElement と main の双方を検査する共通ヘルパーへ委譲(_helpers.ts)。
  await expectNoPageOverflow(page);
}
