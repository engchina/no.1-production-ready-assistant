import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";

// #1874: スキルを Agent Skills の形（<name>/SKILL.md と references/ の zip）で書き出す。

function seedRuntimeSkill(mockApi: MockApi) {
  mockApi.state.skills.push({
    id: "Keiri_Steps",
    name: "経理の手順",
    description: "経理の質問の手順",
    instructions: "",
    mcp_requirements: [],
    references: [],
    enabled: true,
    tags: [],
    source: "runtime",
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

test("一覧の行の操作からスキルを zip で書き出す", async ({ page, mockApi }) => {
  seedRuntimeSkill(mockApi);
  let requested = "";
  await page.route("**/api/skills/*/export", async (route) => {
    requested = new URL(route.request().url()).pathname;
    await route.fulfill({
      status: 200,
      contentType: "application/zip",
      headers: { "Content-Disposition": 'attachment; filename="keiri-steps.zip"' },
      body: Buffer.from("PK\u0005\u0006" + "\u0000".repeat(18), "binary"),
    });
  });
  await page.goto("/skills");
  await page.getByTestId("skill-row-actions-Keiri_Steps").click();
  const downloading = page.waitForEvent("download");
  await page.getByRole("menuitem", { name: "Agent Skills の形で書き出す" }).click();
  const download = await downloading;
  // 名前は backend が決めた Agent Skills の name（フォルダ名）。
  expect(download.suggestedFilename()).toBe("keiri-steps.zip");
  expect(requested).toBe("/api/skills/Keiri_Steps/export");
  await expect(page.getByText("keiri-steps.zip を書き出しました")).toBeVisible();
});

test("書き出しに失敗したら理由を出す", async ({ page, mockApi }) => {
  seedRuntimeSkill(mockApi);
  await page.route("**/api/skills/*/export", (route) =>
    route.fulfill({
      status: 404,
      contentType: "application/json",
      body: JSON.stringify({ data: null, error_messages: ["スキルが見つかりません。"], warning_messages: [] }),
    })
  );
  await page.goto("/skills");
  await page.getByTestId("skill-row-actions-Keiri_Steps").click();
  await page.getByRole("menuitem", { name: "Agent Skills の形で書き出す" }).click();
  await expect(page.getByText("スキルを書き出せませんでした。")).toBeVisible();
});
