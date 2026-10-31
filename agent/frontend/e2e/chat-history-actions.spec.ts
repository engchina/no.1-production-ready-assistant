import { MOCK_NOW, expect, test, type MockApi } from "./fixtures/mock-api";
import type { Locator, Page } from "@playwright/test";

/**
 * チャットの会話の履歴の「名前の変更」「削除」（3 製品共通の useChatHistoryActions。#1817）。
 * 行の操作は sm 以上でホバー・フォーカスの間だけ出す（タッチ端末は常に出す）ので、desktop と mobile-375 の
 * 2 project で確かめる。
 */

function seedThread(mockApi: MockApi, index: number, goal: string, status = "completed") {
  mockApi.state.runs.push({
    id: `run-history-actions-${index}`,
    goal,
    agent_id: "default",
    runtime_id: "builtin",
    status,
    steps: [],
    events: [],
    approvals: [],
    artifacts: [],
    pending_tool_calls: [],
    metadata: {},
    created_by_user_uuid: "local",
    thread_id: `thread_${String(index).padStart(32, "a")}`,
    created_at: MOCK_NOW,
    updated_at: MOCK_NOW,
  });
}

async function openHistory(page: Page): Promise<Locator> {
  await page.goto("/chat");
  const toggle = page.getByTestId("chat-history-toggle");
  await expect(toggle).toBeEnabled();
  await toggle.click();
  const history = page.getByTestId("chat-history");
  await expect(history.getByTestId("chat-thread-list")).toBeVisible();
  return history;
}

async function revealRow(history: Locator, title: string, isMobile: boolean) {
  // sm 以上は行のホバーで操作を出す（タッチ端末は常に出ている）。
  if (!isMobile) await history.getByRole("listitem").filter({ hasText: title }).hover();
}

test.describe("会話の履歴の名前の変更・削除", { tag: "@responsive" }, () => {
  test("名前をその場で変え、空の名前は保存せず、Esc で取り消せる", async ({ page, mockApi, isMobile }) => {
    seedThread(mockApi, 1, "今月の売上は？");
    const history = await openHistory(page);

    await revealRow(history, "今月の売上は？", isMobile);
    await history.getByRole("button", { name: "「今月の売上は？」の名前を変更" }).click();
    const input = history.getByRole("textbox", { name: "会話名" });
    await expect(input).toBeFocused();
    await expect(input).toHaveValue("今月の売上は？");

    await input.fill("   ");
    await input.press("Enter");
    await expect(history.getByText("会話名を入力してください。")).toBeVisible();

    await input.fill("売上の確認");
    await input.press("Enter");
    await expect(history.locator(`[title="売上の確認"]`)).toBeVisible();
    expect(mockApi.state.runs[0].metadata).toMatchObject({ thread_title: "売上の確認" });

    await revealRow(history, "売上の確認", isMobile);
    await history.getByRole("button", { name: "「売上の確認」の名前を変更" }).click();
    await history.getByRole("textbox", { name: "会話名" }).fill("保存しない名前");
    await history.getByRole("textbox", { name: "会話名" }).press("Escape");
    await expect(history.locator(`[title="売上の確認"]`)).toBeVisible();

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });

  test("削除は確認のダイアログを通し、Run は残して会話から外す", async ({ page, mockApi, isMobile }) => {
    seedThread(mockApi, 1, "今月の売上は？");
    seedThread(mockApi, 2, "作成中の会話", "running");
    const history = await openHistory(page);

    await revealRow(history, "今月の売上は？", isMobile);
    const remove = history.getByRole("button", { name: "「今月の売上は？」を削除" });
    await remove.click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("会話を削除しますか？");
    await expect(dialog).toContainText("「今月の売上は？」を会話の履歴から削除します。");
    await dialog.getByRole("button", { name: "キャンセル" }).click();
    await expect(history.getByRole("listitem")).toHaveCount(2);

    await revealRow(history, "今月の売上は？", isMobile);
    await remove.click();
    await dialog.getByRole("button", { name: "削除", exact: true }).click();
    await expect(page.getByText("会話を削除しました。")).toBeVisible();
    await expect(history.getByRole("listitem")).toHaveCount(1);
    // Run は残る（会話からの参照だけを外す）。
    const kept = mockApi.state.runs.find((run) => run.id === "run-history-actions-1");
    expect(kept?.thread_id).toBeNull();

    // 回答の作成中の会話は backend が消さない（理由を Toast で出す）。
    await revealRow(history, "作成中の会話", isMobile);
    await history.getByRole("button", { name: "「作成中の会話」を削除" }).click();
    await dialog.getByRole("button", { name: "削除", exact: true }).click();
    await expect(page.getByText("回答の作成中・承認待ちの会話は削除できません。", { exact: false })).toBeVisible();
    await expect(history.getByRole("listitem")).toHaveCount(1);
  });
});
