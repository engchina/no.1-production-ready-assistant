import { expect, test, type Page, type Route } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { expectListColumnFitsViewport } from "./_helpers/list-detail-fit";
import { findClippedPlaceholders, measureHorizontalOverflow } from "../../../../platform/packages/ui/e2e/layout-checks";

// 共通のユーザー・ロール・権限管理の一覧 + 詳細（共有 ListDetailLayout。#2282）。
// xl 以上は「狭い一覧 | 広い詳細」、xl 未満は 1 ペイン（一覧 → 詳細 → 「一覧へ戻る」）。選択は URL（?user= / ?role=）が持ち、
// 先頭の行を自動で選ばない。

const ROLE = {
  role_id: "role-viewer",
  role_code: "LIST_VIEWER",
  display_name: "一覧閲覧",
  description: "合成のロール",
  is_built_in: false,
  archived: false,
  version: 1,
  permissions: ["menu.query"],
  allowed_profile_ids: [],
};

const USERS = Array.from({ length: 12 }, (_, index) => {
  const sequence = String(index + 1).padStart(2, "0");
  return {
    user_uuid: `list-user-${sequence}`,
    login_user_id: `list.user.${sequence}`,
    display_name: `一覧ユーザー ${sequence}`,
    status: index === 2 ? "DISABLED" : "ACTIVE",
    force_password_change: false,
    locked_until: null,
    version: 1,
    role_ids: [ROLE.role_id],
    is_bootstrap_admin: false,
  };
});

async function fulfill(route: Route, data: unknown) {
  await route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ data, error_messages: [], warning_messages: [] }),
  });
}

async function mockSecurity(page: Page) {
  await mockDatabaseGateReady(page);
  await page.route("**/api/security/roles?include_archived=*", (route) => fulfill(route, [ROLE]));
  await page.route("**/api/security/users", (route) => fulfill(route, USERS));
  await page.route("**/api/security/permissions", (route) => fulfill(route, []));
  await page.addInitScript(() => {
    if (window.sessionStorage.getItem("security-list-detail-cleared")) return;
    window.localStorage.removeItem("production-ready-nl2sql.listDetail.security-users");
    window.sessionStorage.setItem("security-list-detail-cleared", "true");
  });
}

function userRow(page: Page, sequence: string) {
  return page.getByRole("button", { name: `一覧ユーザー ${sequence} を表示` });
}

test.describe("ユーザー・ロール・権限管理の一覧 + 詳細（ListDetailLayout。#2282）", () => {
  test("xl 以上は狭い一覧と広い詳細を並べ、先頭を自動で選ばず、選択を URL に持ち、前へ / 次へで移る", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "2 ペインは xl 以上の幅で確かめる");
    await mockSecurity(page);
    await page.goto("/settings/security/users");

    const layout = page.getByTestId("list-detail-security-users");
    const list = page.getByTestId("list-detail-security-users-list");
    const detail = page.getByTestId("list-detail-security-users-detail");
    await expect(layout).toHaveAttribute("data-list-detail-layout", "split");
    await expect(userRow(page, "01")).toBeVisible();
    await expect(detail).toContainText("ユーザーを選択してください");
    await expect(page).not.toHaveURL(/user=/u);
    await expect(page.getByTestId("security-users-back")).toHaveCount(0);
    // 一覧の列は画面の高さに収まり、詳細が短ければ本文は縦にスクロールしない（#2330）。
    await expectListColumnFitsViewport(page, "security-users", { requireFooter: false });

    const [listBox, detailBox] = await Promise.all([list.boundingBox(), detail.boundingBox()]);
    // 一覧は既定 20rem（14px ルートで 280px）、詳細はその 2 倍より広い。
    expect(listBox!.width).toBeCloseTo(280, 0);
    expect(detailBox!.width).toBeGreaterThan(listBox!.width * 2);
    // 一覧の行は名前と補足 1 行（割り当てロール）と状態。ログイン ID は出さない（#1696）。
    await expect(page.getByTestId("security-users-grid")).not.toContainText("list.user.");
    await expect(userRow(page, "01")).toContainText("一覧閲覧");
    expect(await findClippedPlaceholders(page)).toEqual([]);

    await userRow(page, "03").click();
    await expect(page).toHaveURL(/\?user=list-user-03$/u);
    await expect(page.getByRole("heading", { name: "一覧ユーザー 03", exact: true })).toBeVisible();
    await expect(userRow(page, "03")).toHaveAttribute("aria-current", "true");
    // 詳細の列の中にカードを重ねない（枠は詳細の列が持つ）。
    const nestedFrame = await page.getByTestId("security-users-detail").evaluate((node) => {
      const style = getComputedStyle(node);
      return style.borderTopWidth !== "0px" || style.boxShadow !== "none";
    });
    expect(nestedFrame).toBe(false);

    await page.reload();
    await expect(page.getByRole("heading", { name: "一覧ユーザー 03", exact: true })).toBeVisible();
    await page.getByTestId("security-users-detail-pager-next").click();
    await expect(page).toHaveURL(/\?user=list-user-04$/u);
    await expect(userRow(page, "04")).toHaveAttribute("aria-current", "true");
    await page.getByTestId("security-users-detail-pager-previous").click();
    await expect(page).toHaveURL(/\?user=list-user-03$/u);
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  });

  test("↑ / ↓ で一覧の行の間を移り、Enter で選ぶ。一覧を畳むと詳細が全幅になる", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "キーボードの移動と開閉は desktop で確かめる");
    await mockSecurity(page);
    await page.goto("/settings/security/users");
    await userRow(page, "01").focus();
    await page.keyboard.press("ArrowDown");
    await expect(userRow(page, "02")).toBeFocused();
    await page.keyboard.press("End");
    await expect(userRow(page, "12")).toBeFocused();
    await page.keyboard.press("Home");
    await expect(userRow(page, "01")).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(/\?user=list-user-01$/u);

    const layout = page.getByTestId("list-detail-security-users");
    const toggle = page.getByTestId("list-detail-security-users-toggle");
    await expect(toggle).toHaveAccessibleName("ユーザー一覧");
    await toggle.click();
    await expect(layout).toHaveAttribute("data-list-collapsed", "true");
    await expect(page.getByTestId("list-detail-security-users-list")).toBeHidden();
    const layoutBox = await layout.boundingBox();
    const detailBox = await page.getByTestId("list-detail-security-users-detail").boundingBox();
    expect(Math.abs(layoutBox!.width - detailBox!.width)).toBeLessThanOrEqual(1);
    await toggle.click();
    await expect(page.getByTestId("list-detail-security-users-list")).toBeVisible();
  });

  test("1920px でも一覧は狭い列のままで、タイトルと本文の左端がそろい、横にあふれない（ダーク）", async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "1920px は desktop で確かめる");
    await page.setViewportSize({ width: 1920, height: 1000 });
    await page.emulateMedia({ colorScheme: "dark" });
    await mockSecurity(page);
    await page.goto("/settings/security/roles?role=role-viewer");
    await expect(page.getByRole("heading", { name: "一覧閲覧", level: 2 })).toBeVisible();
    const list = await page.getByTestId("list-detail-security-roles-list").boundingBox();
    const title = await page.getByRole("heading", { level: 1, name: "ロール管理" }).boundingBox();
    expect(list!.width).toBeCloseTo(280, 0);
    expect(Math.abs(list!.x - title!.x)).toBeLessThanOrEqual(1);
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  });

  test("xl 未満は 1 ペイン: 選ぶと詳細だけ、「一覧へ戻る」で一覧に戻り選んだ行へフォーカスを戻す", { tag: "@responsive" }, async ({ page }, testInfo) => {
    if (testInfo.project.name === "desktop") await page.setViewportSize({ width: 1024, height: 900 });
    await mockSecurity(page);
    await page.goto("/settings/security/users");
    const layout = page.getByTestId("list-detail-security-users");
    const list = page.getByTestId("list-detail-security-users-list");
    const detail = page.getByTestId("list-detail-security-users-detail");
    await expect(layout).toHaveAttribute("data-list-detail-layout", "single");
    await expect(list).toBeVisible();
    await expect(detail).toBeHidden();
    await expect(page.getByTestId("security-users-back")).toHaveCount(0);
    expect(await findClippedPlaceholders(page)).toEqual([]);

    await userRow(page, "04").click();
    await expect(page).toHaveURL(/\?user=list-user-04$/u);
    await expect(list).toBeHidden();
    await expect(detail).toBeVisible();
    await expect(detail).toBeFocused();
    await expect(page.getByRole("heading", { name: "一覧ユーザー 04", exact: true })).toBeVisible();
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);

    const back = page.getByTestId("security-users-back");
    await expect(back).toHaveText("一覧へ戻る");
    await back.click();
    await expect(page).not.toHaveURL(/user=/u);
    await expect(list).toBeVisible();
    await expect(detail).toBeHidden();
    await expect(userRow(page, "04")).toBeFocused();

    // ブラウザの「戻る」でも一覧へ戻る（一覧から入るときは履歴に積む）。
    await userRow(page, "02").click();
    await expect(detail).toBeVisible();
    await page.goBack();
    await expect(list).toBeVisible();
    await expect(page).not.toHaveURL(/user=/u);
  });

  test("ロール管理の詳細から権限管理を開くと、そのロールを選んだ一覧 + 詳細になる", { tag: "@responsive" }, async ({ page }) => {
    await mockSecurity(page);
    await page.goto("/settings/security/roles?role=role-viewer");
    await page.getByTestId("security-roles-open-permissions").click();
    await expect(page).toHaveURL(/\/settings\/security\/permissions\?role=role-viewer$/u);
    await expect(page.getByTestId("list-detail-security-permissions-detail")).toBeVisible();
    await expect(page.getByRole("heading", { name: "一覧閲覧", level: 2 })).toBeVisible();
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
  });
});


test("管理画面の読込表示は対象を短く示して一覧の幅に収まる", { tag: "@responsive" }, async ({ page }) => {
  await mockSecurity(page);
  let release!: () => void;
  const pending = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/security/**", async route => { await pending; await route.fallback(); });
  try {
    for (const [kind, label] of [["users", "ユーザーを読込中"], ["roles", "ロールを読込中"], ["permissions", "権限を読込中"]]) {
      await page.goto(`/settings/security/${kind}`);
      const loading = page.getByTestId(`security-${kind}-loading`);
      await expect(loading.locator("[data-processing-label]")).toHaveText(label);
      await expect(loading.locator("[data-processing-label]")).toBeVisible();
      await expect(loading).toContainText("経過時間");
      const size = await loading.locator("[data-processing-label]").evaluate(node => ({height: node.getBoundingClientRect().height, line: parseFloat(getComputedStyle(node).lineHeight)}));
      expect(size.height).toBeLessThanOrEqual(size.line * 2 + 1);
      expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
    }
  } finally { release(); }
});
