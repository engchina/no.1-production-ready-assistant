import { expect, test, type Page } from "./_helpers/test";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import { confirmChatRequirement, mockChatRequirement } from "./_helpers/chat-requirement";

// #2517: チャットの添付（会話の期間だけ使う作業ファイル）。運用設定で有効なら SQL チャットで表を添付し、
// 生成方法「Enterprise AI」で業務データと照合する SQL を作る。送った質問の下に添付の名前を出す。

const profile = {
  id: "sales",
  name: "売上分析",
  description: "売上の集計",
  archived: false,
  allowed_tables: ["APP.SALES"],
  allowed_views: [],
  allowed_table_count: 1,
  allowed_view_count: 0,
  version: 1,
};
const now = "2026-10-02T22:00:00Z";
const LIMITS = {
  max_bytes: 20 * 1024 * 1024,
  max_files: 10,
  ttl_hours: 24,
  accept: [".csv", ".xlsx", ".pdf"],
  image_enabled: false,
};
const attachmentRef = { attachment_id: "att-1", file_name: "照合表.csv", kind: "csv", size_bytes: 40 };

async function setup(page: Page, { enabled }: { enabled: boolean }) {
  await mockDatabaseGateReady(page);
  const requirement = await mockChatRequirement(page);
  const requests: Record<string, unknown>[] = [];
  const turns: Record<string, unknown>[] = [];
  await page.route("**/api/nl2sql/profiles/search**", (route) =>
    route.fulfill({ json: { data: { items: [profile], total: 1, next_cursor: null } } }),
  );
  await page.route("**/api/nl2sql/profiles/*/usage-context", (route) => route.fulfill({ json: { data: profile } }));
  await page.route("**/api/chat-attachments**", async (route) => {
    const method = route.request().method();
    if (method === "GET") {
      await route.fulfill({ json: { data: { items: [], limits: { ...LIMITS, enabled } } } });
      return;
    }
    if (method === "POST") {
      await route.fulfill({
        json: {
          data: {
            ...attachmentRef,
            media_type: "text/csv",
            conversation_id: null,
            section_count: 0,
            sheets: [{ sheet: "照合表", columns: ["注文 ID", "金額"], row_count: 2 }],
            warnings: [],
            created_at: now,
            expires_at: now,
            uploaded_by_me: true,
          },
        },
      });
      return;
    }
    await route.fulfill({ json: { data: { deleted: true } } });
  });
  await page.route("**/api/nl2sql/chats**", (route) => {
    const conversation = { id: "chat-1", title: turns[0]?.question, profile_id: "sales", created_at: now };
    return route.fulfill({
      json: {
        data:
          new URL(route.request().url()).pathname === "/api/nl2sql/chats"
            ? { items: turns.length ? [conversation] : [], next_cursor: null, total: turns.length, limit: 25 }
            : { conversation, turns },
      },
    });
  });
  await page.route("**/api/nl2sql/jobs", (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    requests.push(body);
    const turn = {
      job_id: "chat-1",
      question: body.question,
      engine: body.engine,
      status: "done",
      created_at: now,
      steps: [],
      // backend は送った attachment_ids から、添付の名前を残す。
      attachments: Array.isArray(body.attachment_ids) ? [attachmentRef] : [],
      result: {
        generated_sql: 'SELECT s.ID FROM APP.SALES s JOIN ATTACHMENT_1 a ON a."注文 ID" = s.ID',
        original_question: body.question,
        explanation: "添付の表と売上を照合します。",
        safety: { is_safe: true },
      },
    };
    turns.push(turn);
    return route.fulfill({ json: { data: { job_id: "chat-1", status: "done", created_at: now, steps: [] } } });
  });
  return { requests, requirement };
}

const csv = { name: "照合表.csv", mimeType: "text/csv", buffer: Buffer.from("注文 ID,金額\n1,1200\n2,3400\n") };

test.describe("SQL チャットの添付", { tag: "@responsive" }, () => {
  test("表を添付し、生成方法を Enterprise AI にして照合の SQL を作る", async ({ page }) => {
    const state = await setup(page, { enabled: true });
    await page.goto("/chat");
    await page.getByTestId("chat-attachments-input").setInputFiles(csv);
    await expect(page.locator("[data-chat-attachment]").first()).toHaveAttribute("data-status", "ready");
    // 生成方法は 1 つだけなので、選ぶ欄と方式の不一致の案内は出さない。
    await expect(page.getByRole("combobox", { name: "生成方法", exact: true })).toHaveCount(0);
    await expect(page.getByTestId("sql-chat-attachment-engine")).toHaveCount(0);

    await page.getByRole("textbox", { name: "質問", exact: true }).fill("添付の注文が売上にあるか照合して");
    await page.getByTestId("sql-chat-send").click();
    expect(state.requirement.creates[0]).toMatchObject({ attachment_ids: ["att-1"] });
    await confirmChatRequirement(page);
    await expect.poll(() => state.requests[0]?.attachment_ids).toEqual(["att-1"]);
    await expect(page.getByTestId("sql-chat-turn").getByRole("list", { name: "添付ファイル" })).toContainText(
      "照合表.csv",
    );
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test("運用設定で無効なら添付の操作を出さない", async ({ page }) => {
    await setup(page, { enabled: false });
    await page.goto("/chat");
    await expect(page.getByRole("textbox", { name: "質問", exact: true })).toBeVisible();
    await expect(page.getByTestId("chat-attachments-add")).toHaveCount(0);
  });
});

test("運用設定でチャットの添付を有効にする", { tag: "@responsive" }, async ({ page }) => {
  await mockDatabaseGateReady(page);
  let enabled = false;
  await page.route("**/api/settings/chat-attachments", async (route) => {
    if (route.request().method() === "PUT") enabled = Boolean((route.request().postDataJSON() as { enabled: boolean }).enabled);
    await route.fulfill({
      json: { data: { enabled, updated_at: null, max_bytes: LIMITS.max_bytes, max_files: 10, ttl_hours: 24 } },
    });
  });
  await page.goto("/settings/chat-attachments");
  await expect(page.getByRole("heading", { name: "チャットの添付", level: 1 })).toBeVisible();
  const toggle = page.getByTestId("chat-attachments-enabled");
  await toggle.click();
  await page.getByRole("button", { name: "保存" }).click();
  await expect.poll(() => enabled).toBe(true);
});
