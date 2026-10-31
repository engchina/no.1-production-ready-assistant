import { expect, test, type Page } from "./fixtures/test";
import { vectorIndexEnvelope } from "./_vector-index-fixtures";
import { expectNoPageOverflow, mockLocalAuth, openSidebarNav } from "./_helpers";

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760, collapse: false },
  { name: "mobile", width: 375, height: 812, collapse: true },
]) {
  test(`検索インデックス設定は高速・バランス・高精度の順に表示する (${viewport.name})`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    if (viewport.collapse) await collapseSidebar(page);
    await mockVectorIndex(page, "balanced");

    await page.goto("/settings/vector-index");

    await expect(page.getByRole("heading", { name: "検索インデックス", exact: true, level: 1 })).toBeVisible();
    const choices = page.getByRole("radio");
    await expect(choices).toHaveCount(3);
    await expect(choices.nth(0)).toHaveAccessibleName(/高速/);
    await expect(choices.nth(1)).toHaveAccessibleName(/バランス/);
    await expect(choices.nth(2)).toHaveAccessibleName(/高精度/);
    await expect(choices.nth(1)).toBeChecked();
    const boxes = await Promise.all((await choices.all()).map((choice) => choice.boundingBox()));
    expect(boxes.every(Boolean)).toBe(true);
    if (viewport.name === "desktop") {
      expect(boxes[0]!.x).toBeLessThan(boxes[1]!.x);
      expect(boxes[1]!.x).toBeLessThan(boxes[2]!.x);
    } else {
      expect(boxes[0]!.y).toBeLessThan(boxes[1]!.y);
      expect(boxes[1]!.y).toBeLessThan(boxes[2]!.y);
    }
    await page.screenshot({ path: testInfo.outputPath("preset-order.png"), fullPage: true });
    // 375px ではナビがドロワー（#367）。開いて現在地を確かめる。
    await expect((await openSidebarNav(page)).getByRole("link", { name: "検索インデックス" })).toHaveAttribute("aria-current", "page");
    await expectNoHorizontalOverflow(page);
  });
}

test("検索インデックス設定は accurate 選択で索引再作成警告を出して保存できる", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  let saved: unknown = null;
  await page.route("**/api/settings/vector-index", async (route) => {
    if (route.request().method() === "PATCH") {
      saved = route.request().postDataJSON();
      await route.fulfill({ json: vectorIndexEnvelope("accurate") });
      return;
    }
    await route.fulfill({ json: vectorIndexEnvelope("balanced") });
  });

  await page.goto("/settings/vector-index");

  const accurate = page.getByRole("radio", { name: /高精度/ });
  await accurate.click();
  await expect(accurate).toBeChecked();
  // 実際の索引(32/500)と違う検索精度を選ぶと出る再作成警告(reprovision)を固有文言で検証する。
  await expect(page.getByText("推奨ビルドパラメータを適用するには", { exact: false })).toBeVisible();

  // 保存前(balanced = 実際の索引と一致)は再作成 SQL パネルは出ない。
  await expect(page.getByRole("heading", { name: "索引再作成 SQL" })).toHaveCount(0);

  await page.getByRole("button", { name: "保存" }).click();

  await expect(page.getByText("検索インデックスの設定を保存しました。")).toBeVisible();
  expect(saved).toEqual({ profile: "accurate" });

  // 保存後(accurate)は profile 反映の再作成 SQL がコピー可能な形で提示される。
  await expect(page.getByRole("heading", { name: "索引再作成 SQL" })).toBeVisible();
  const sqlBox = page.getByLabel("索引再作成 SQL");
  await expect(sqlBox).toContainText("DROP INDEX rag_chunks_embedding_hnsw_idx;");
  await expect(sqlBox).toContainText("NEIGHBORS 48");
  await expect(sqlBox).toContainText("EFCONSTRUCTION 800");
  await expect(page.getByRole("button", { name: "SQL をコピー" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

// 実際の索引が推奨ビルドと一致すると、警告と再作成 SQL を出さない(#562)。
for (const viewport of [
  { name: "desktop", width: 1280, height: 760, collapse: false },
  { name: "mobile", width: 375, height: 812, collapse: true },
]) {
  test(`検索インデックス設定は現在の索引が推奨と一致すると再作成を案内しない (${viewport.name})`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await setUiState(page, { sidebarCollapsed: viewport.collapse });
    await mockVectorIndex(page, "accurate", [48, 800]);

    await page.goto("/settings/vector-index");

    await expect(page.getByRole("radio", { name: /高精度/ })).toBeChecked();
    const currentIndex = page.getByText("現在の索引", { exact: true }).locator("..");
    await expect(currentIndex).toContainText("NEIGHBORS 48 / EFCONSTRUCTION 800");
    await expect(page.getByText("推奨ビルドパラメータを適用するには", { exact: false })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: "索引再作成 SQL" })).toHaveCount(0);

    // 実際の索引と違う検索精度を選ぶと、backend の判定(reprovision)で警告を出す。
    await page.getByRole("radio", { name: /高速/ }).click();
    await expect(page.getByText("推奨ビルドパラメータを適用するには", { exact: false })).toBeVisible();
    await expectNoHorizontalOverflow(page);
  });
}

test("検索インデックス設定は現在の索引を確認できないとき、その旨と再作成 SQL を出す", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await mockVectorIndex(page, "accurate", null);

  await page.goto("/settings/vector-index");

  const currentIndex = page.getByText("現在の索引", { exact: true }).locator("..");
  await expect(currentIndex).toContainText("確認できません");
  await expect(page.getByText("再作成が必要かを判断できません", { exact: false })).toBeVisible();
  await expect(page.getByText("推奨ビルドパラメータを適用するには", { exact: false })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "索引再作成 SQL" })).toBeVisible();
});

test("検索インデックス設定は未保存選択を裏の再取得で失わない", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  let profile = "balanced";
  let getCount = 0;
  await page.route("**/api/settings/vector-index", async (route) => {
    getCount += 1;
    await route.fulfill({ json: vectorIndexEnvelope(profile) });
  });

  await page.goto("/settings/vector-index");

  const fast = page.getByRole("radio", { name: /高速/ });
  await fast.click();
  await expect(fast).toBeChecked();

  // タブへ戻ったとき（visibilitychange。TanStack Query v5 の再取得の起点）の再取得で保存値が変わっても、
  // 未保存の選択は維持される。
  profile = "accurate";
  const before = getCount;
  await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
  await expect.poll(() => getCount).toBeGreaterThan(before);

  await expect(fast).toBeChecked();
  await expect(page.getByText("未保存の変更があります。")).toBeVisible();
});

test("検索インデックス設定取得に失敗したら再試行できる", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  await page.route("**/api/settings/vector-index", async (route) => {
    await route.fulfill({
      status: 503,
      json: { data: null, error_messages: ["検索インデックス設定を取得できませんでした。"], warning_messages: [] },
    });
  });

  await page.goto("/settings/vector-index");

  await expect(page.getByRole("alert")).toContainText("検索インデックス設定を取得できませんでした。");
  await expect(page.getByRole("button", { name: "再試行" })).toBeVisible();
});

async function collapseSidebar(page: Page) {
  await setUiState(page, { sidebarCollapsed: true });
}

async function setUiState(
  page: Page,
  state: { sidebarCollapsed?: boolean }
) {
  // サイドナビの折りたたみ(共有 UI の ui-store が保存する値)を読み込みの前に入れる。
  await page.addInitScript((value) => {
    window.localStorage.setItem("production-ready-rag.ui", JSON.stringify({ state: value, version: 0 }));
  }, state);
}

// actual は実際の索引の [NEIGHBORS, EFCONSTRUCTION]。null は確認できない(#562)。
async function mockVectorIndex(
  page: Page,
  profile: string,
  actual: [number, number] | null = [32, 500]
) {
  await page.route("**/api/settings/vector-index", async (route) => {
    await route.fulfill({ json: vectorIndexEnvelope(profile, actual) });
  });
}

async function expectNoHorizontalOverflow(page: Page) {
  // documentElement と main の双方を検査する共通ヘルパーへ委譲(_helpers.ts)。
  await expectNoPageOverflow(page);
}

test("検索インデックスの設定は、編集していなければ裏の再取得で変わった保存値にそろえ、未保存にしない（#987）", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1280, height: 760 });
  let profile = "balanced";
  let getCount = 0;
  await page.route("**/api/settings/vector-index", async (route) => {
    getCount += 1;
    await route.fulfill({ json: vectorIndexEnvelope(profile) });
  });

  await page.goto("/settings/vector-index");
  await expect(page.getByRole("radio", { name: /バランス/ })).toBeChecked();

  // 別の管理者が高精度に変えた後、タブへ戻ったとき（visibilitychange）の再取得を起こす。
  profile = "accurate";
  const before = getCount;
  await page.evaluate(() => window.dispatchEvent(new Event("visibilitychange")));
  await expect.poll(() => getCount).toBeGreaterThan(before);

  await expect(page.getByRole("radio", { name: /高精度/ })).toBeChecked();
  await expect(page.getByText("未保存の変更があります。")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "保存" })).toBeDisabled();
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 760 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`検索インデックスの設定は保存に失敗しても選択を残し、操作の行に失敗を出す（#987, ${viewport.name}）`, async ({
    page,
  }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    let patchCount = 0;
    await page.route("**/api/settings/vector-index", async (route) => {
      if (route.request().method() === "PATCH") {
        patchCount += 1;
        if (patchCount === 1) {
          await route.fulfill({
            status: 500,
            json: {
              data: null,
              error_messages: ["検索インデックス設定を backend/.env へ保存できませんでした。"],
              warning_messages: [],
            },
          });
          return;
        }
        await route.fulfill({ json: vectorIndexEnvelope("fast") });
        return;
      }
      await route.fulfill({ json: vectorIndexEnvelope("balanced") });
    });
    await page.goto("/settings/vector-index");

    const fast = page.getByRole("radio", { name: /高速/ });
    await fast.click();
    const actions = page.getByRole("group", { name: "検索インデックスの設定の操作" });
    const saveButton = actions.getByRole("button", { name: "保存" });
    await saveButton.click();

    await expect(actions).toContainText("検索インデックス設定を backend/.env へ保存できませんでした。");
    await expect(fast).toBeChecked();
    await expect(saveButton).toBeEnabled();
    await expectNoHorizontalOverflow(page);

    await saveButton.click();
    await expect(page.getByText("検索インデックスの設定を保存しました。")).toBeVisible();
    await expect(fast).toBeChecked();
    await expect(saveButton).toBeDisabled();
    expect(patchCount).toBe(2);
  });
}
