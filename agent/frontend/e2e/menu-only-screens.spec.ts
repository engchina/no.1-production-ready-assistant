import { dbUser } from "./fixtures/auth";
import { expect, test } from "./fixtures/mock-api";

// #1113: その画面のメニュー権限だけの利用者が画面を開いても、画面が開いた直後に読む API が
// 経路の権限拒否（403 SECURITY_ROUTE_FORBIDDEN）にならず、権限なしの画面へ移らない。
// e2e の mock は backend の権限 manifest（app/security/permissions.py）で 403 を返す。

const SCREENS = [
  { path: "/automations", permissions: ["menu.automations"], heading: "自動実行" },
  { path: "/evaluation", permissions: ["menu.evaluation"], heading: "品質評価" },
  { path: "/feedback", permissions: ["menu.feedback"], heading: "フィードバック" },
  { path: "/runs", permissions: ["agent.runs.view"], heading: "実行履歴" },
  { path: "/runs", permissions: ["agent.runs.operate"], heading: "実行履歴" },
];

for (const screen of SCREENS) {
  test(`${screen.permissions.join("・")} だけの利用者は ${screen.path} を開ける`, async ({ page, mockApi }) => {
    mockApi.setCurrentUser(dbUser({ permissions: screen.permissions, allowed_agent_ids: ["default"] }));
    await page.goto(screen.path);
    await expect(page.getByRole("heading", { name: screen.heading, level: 1 })).toBeVisible();
    // 画面の取得が落ち着くまで待ってから、権限なしの画面へ移っていないことを確かめる。
    await page.waitForLoadState("networkidle");
    await expect(page).toHaveURL(new RegExp(`${screen.path}$`));
    await expect(page.getByRole("heading", { name: "この機能を利用する権限がありません" })).toHaveCount(0);
  });
}

test("実行できる利用者は作成の前に実行環境の状態を確かめ、閲覧だけの利用者は読まない", async ({ page, mockApi }) => {
  mockApi.setCurrentUser(dbUser({ permissions: ["agent.runs.view"], allowed_agent_ids: ["default"] }));
  await page.goto("/runs");
  await expect(page.getByRole("heading", { name: "実行履歴", level: 1 })).toBeVisible();
  await page.waitForLoadState("networkidle");
  expect(mockApi.requests.some((request) => request.path === "/api/runtime/status")).toBe(false);

  mockApi.setCurrentUser(dbUser({ permissions: ["agent.runs.operate"], allowed_agent_ids: ["default"] }));
  await page.goto("/runs?id=new");
  await expect(page.getByTestId("run-create-submit")).toBeEnabled();
  expect(mockApi.requests.some((request) => request.path === "/api/runtime/status")).toBe(true);
});
