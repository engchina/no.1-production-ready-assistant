import { expect, test } from "./fixtures/mock-api";

// backend が error code の付いたエラーに添えた内部の原文（`error_details.reason`。英語・技術的な文）は、
// 要約に出さず「詳細」の「元のメッセージ」にだけ出す（UX 契約 messaging.md §10.3）。

test("エラーの内部の原文は要約に出さず「詳細」にだけ出す", async ({ page }) => {
  test.setTimeout(60_000);
  await page.route("**/api/automations", async (route) => {
    if (route.request().method() !== "GET") return route.fallback();
    await route.fulfill({
      status: 409,
      json: {
        data: null,
        error_messages: ["自動実行の一覧を読み込めませんでした。時間をおいて、もう一度開いてください。"],
        warning_messages: [],
        error_code: "automation_store_unavailable",
        error_details: { reason: "store is locked" },
      },
    });
  });
  await page.goto("/automations");

  const failure = page.getByRole("alert").filter({ hasText: "自動実行の一覧を読み込めませんでした。" }).first();
  await expect(failure).toBeVisible({ timeout: 20_000 });
  const details = failure.locator("details");
  await expect(details).toContainText("元のメッセージ");
  await expect(details).toContainText("store is locked");
  await expect(details).toContainText("automation_store_unavailable");
  const outside = await failure.evaluate((element) => {
    const clone = element.cloneNode(true) as HTMLElement;
    clone.querySelectorAll("details").forEach((node) => node.remove());
    return clone.textContent ?? "";
  });
  expect(outside).not.toContain("store is locked");
});
