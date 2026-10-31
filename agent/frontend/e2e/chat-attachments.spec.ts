import type { Page, Route } from "@playwright/test";

import { expect, test } from "./fixtures/mock-api";

// #2517: チャットの添付（会話の期間だけ使う作業ファイル）。運用設定で有効な製品だけ入力欄に添付の操作を出し、
// 添付して送ると、送った質問の下に添付の名前を出す。

const LIMITS = {
  max_bytes: 20 * 1024 * 1024,
  max_files: 2,
  ttl_hours: 24,
  accept: [".pdf", ".csv", ".xlsx", ".png"],
  image_enabled: true,
};

async function mockAttachments(page: Page, { enabled }: { enabled: boolean }) {
  let counter = 0;
  await page.route("**/api/chat-attachments**", async (route: Route) => {
    const request = route.request();
    const method = request.method();
    if (method === "GET") {
      await route.fulfill({ json: { data: { items: [], limits: { ...LIMITS, enabled } }, error_messages: [] } });
      return;
    }
    if (method === "POST") {
      counter += 1;
      // アップロード中の表示を確かめられるよう、少し待ってから返す。
      await new Promise((resolve) => setTimeout(resolve, 300));
      await route.fulfill({
        json: {
          data: {
            attachment_id: `att-${counter}`,
            file_name: "売上表.csv",
            media_type: "text/csv",
            kind: "csv",
            size_bytes: 24,
            conversation_id: null,
            // アップロードは原本の保存だけで、解析は送信した後（#2652）。
            section_count: 0,
            sheets: [],
            warnings: ["macros_not_executed"],
            parse_status: "pending",
            created_at: "2026-10-19T00:00:00Z",
            expires_at: "2026-10-20T00:00:00Z",
            uploaded_by_me: true,
          },
          error_messages: [],
        },
      });
      return;
    }
    await route.fulfill({ json: { data: { deleted: true }, error_messages: [] } });
  });
}

const csv = { name: "売上表.csv", mimeType: "text/csv", buffer: Buffer.from("コード\nA-1\nA-2\n") };

test.describe("チャットの添付", { tag: "@responsive" }, () => {
  test("添付して送ると、送った質問の下に添付を出す", async ({ page, mockApi }) => {
    await mockAttachments(page, { enabled: true });
    await page.goto("/chat");
    const add = page.getByTestId("chat-attachments-add");
    await expect(add).toHaveText("ファイルを添付");
    await expect(page.getByText("1 つ 20 MB・2 件まで。最後に使ってから 24 時間で消えます")).toBeVisible();

    await page.getByTestId("chat-attachments-input").setInputFiles(csv);
    const chip = page.locator("[data-chat-attachment]").first();
    await expect(chip).toHaveAttribute("data-status", "uploading");
    await expect(chip).toContainText("アップロード中");
    await expect(page.getByTestId("chat-send")).toBeDisabled();
    await expect(chip).toHaveAttribute("data-status", "ready");
    await expect(chip).toContainText("マクロは実行していません。");

    await page.getByRole("textbox", { name: "質問" }).fill("添付の表の件数は？");
    await page.getByTestId("chat-send").click();
    await expect.poll(() => (mockApi.lastRequest("POST", "/api/runs")?.body as { attachment_ids?: string[] })?.attachment_ids).toEqual([
      "att-1",
    ]);
    // 入力欄からは外れ、送った質問の下に出る。
    await expect(page.getByTestId("chat-attachments-list")).toHaveCount(0);
    const sent = page.getByRole("list", { name: "添付ファイル" });
    await expect(sent).toContainText("att-1.csv");
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test("外すボタンで添付を外し、上限を超えた添付は理由を出す", async ({ page }, testInfo) => {
    await mockAttachments(page, { enabled: true });
    await page.goto("/chat");
    const input = page.getByTestId("chat-attachments-input");
    await input.setInputFiles([csv, { ...csv, name: "b.csv" }, { ...csv, name: "c.csv" }]);
    const chips = page.locator("[data-chat-attachment]");
    await expect(chips).toHaveCount(3);
    await expect(chips.nth(2)).toHaveAttribute("data-status", "failed");
    await expect(chips.nth(2)).toContainText("添付できるのは 2 件までです。");
    await expect(chips.nth(1)).toHaveAttribute("data-status", "ready");
    await page.screenshot({ path: testInfo.outputPath("composer-attachments.png") });
    await page.getByRole("button", { name: "c.csv を外す" }).click();
    await expect(chips).toHaveCount(2);
  });

  test("運用設定で無効な製品は添付の操作を出さない", async ({ page }) => {
    await mockAttachments(page, { enabled: false });
    await page.goto("/chat");
    await expect(page.getByRole("textbox", { name: "質問" })).toBeVisible();
    await expect(page.getByTestId("chat-attachments-add")).toHaveCount(0);
  });
});

test("運用設定でチャットの添付を有効にする", { tag: "@responsive" }, async ({ page, mockApi }) => {
  await page.goto("/settings/chat-attachments");
  await expect(page.getByRole("heading", { name: "チャットの添付", level: 1 })).toBeVisible();
  const toggle = page.getByTestId("chat-attachments-enabled");
  await expect(toggle).toHaveAttribute("aria-checked", "false");
  await expect(page.getByTestId("chat-attachments-limits")).toContainText("20 MB");
  await toggle.click();
  await page.getByRole("button", { name: "保存" }).click();
  await expect.poll(() => mockApi.state.chatAttachmentsEnabled).toBe(true);
  await expect(toggle).toHaveAttribute("aria-checked", "true");
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
});
