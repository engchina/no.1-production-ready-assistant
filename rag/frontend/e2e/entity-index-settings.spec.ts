import { expect, test } from "./fixtures/test";
import { expectNoPageOverflow, mockLocalAuth } from "./_helpers";

test.beforeEach(async ({ page }) => { await mockLocalAuth(page); });

test("実体・別名索引は既定 OFF で、切り替え・保存・再読込ができる", { tag: "@responsive" }, async ({ page }) => {
  let enabled = false;
  const patches: unknown[] = [];
  await page.route("**/api/settings/entity-index", async (route) => {
    if (route.request().method() === "PATCH") {
      expect(route.request().headers()["content-type"]).toBe("application/json");
      const payload = route.request().postDataJSON();
      patches.push(payload);
      enabled = payload.enabled;
    }
    await route.fulfill({ json: { data: { enabled }, error_messages: [], warning_messages: [] } });
  });
  await page.goto("/settings/entity-index");
  await expect(page.getByText(/検索で利用するかは、検索方法の「実体・別名による検索拡張」/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "実体・別名索引", exact: true, level: 1 })).toBeVisible();
  await expect(page.getByRole("heading", { name: "実体・別名索引の構築", exact: true })).toBeVisible();
  const toggle = page.getByRole("radio", { name: /^構築する/ });
  const off = page.getByRole("radio", { name: /^構築しない/ });
  const save = page.getByRole("button", { name: "保存", exact: true });
  await expect(off).toBeChecked();
  await expect(save).toBeDisabled();
  await toggle.click();
  await page.getByRole("button", { name: "変更を破棄" }).click();
  await expect(off).toBeChecked();
  for (const next of [true, false]) {
    await (next ? toggle : off).check();
    await save.click();
    await expect(save).toBeDisabled();
    await page.reload();
    await expect(toggle).toBeChecked({ checked: next });
  }
  expect(patches).toEqual([{ enabled: true }, { enabled: false }]);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: test.info().outputPath("entity-index.png"), fullPage: true });
});

test("実体・別名索引は保存失敗時に選択を残して再試行できる", { tag: "@responsive" }, async ({ page }) => {
  let fail = true;
  await page.route("**/api/settings/entity-index", async (route) => {
    if (route.request().method() === "PATCH" && fail) {
      expect(route.request().headers()["content-type"]).toBe("application/json");
      fail = false;
      await route.fulfill({ status: 500, json: { data: null, error_messages: ["設定を保存できませんでした。再試行してください。"], warning_messages: [] } });
      return;
    }
    await route.fulfill({ json: { data: { enabled: route.request().method() === "PATCH" }, error_messages: [], warning_messages: [] } });
  });
  await page.goto("/settings/entity-index");
  const toggle = page.getByRole("radio", { name: /^構築する/ });

  await toggle.click();
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByText("設定を保存できませんでした。再試行してください。", { exact: true })).toBeVisible();
  await expect(page.getByText("設定を保存できませんでした。再試行してください。", { exact: true })).toContainText("設定を保存できませんでした。");
  await expect(toggle).toBeChecked();
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(page.getByRole("button", { name: "保存", exact: true })).toBeDisabled();
});
