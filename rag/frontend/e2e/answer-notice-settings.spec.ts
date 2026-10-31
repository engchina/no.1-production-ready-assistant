import { expect, test, type Page } from "./fixtures/test";

import { apiEnvelope, expectNoPageOverflow, mockDatabaseReady, mockLocalAuth } from "./_helpers";

// 回答の注意書きの設定（設定は製品ごと。画面は platform の共有パッケージ。#2405・#2449）。

const DEFAULT_TEXT = "既定の 1 行目\n既定の 2 行目";
const DEFAULT_CONTACT = "既定の窓口";

interface NoticeState {
  enabled: boolean;
  text: string | null;
  contact?: string | null;
}

async function mockAnswerNotice(page: Page, initial: NoticeState = { enabled: true, text: null, contact: null }) {
  const state: NoticeState = { ...initial };
  const saved: NoticeState[] = [];
  const body = () =>
    apiEnvelope({
      enabled: state.enabled,
      text: state.text,
      default_text: DEFAULT_TEXT,
      contact: state.contact ?? null,
      default_contact: DEFAULT_CONTACT,
      contact_template: "解決しない場合は、{contact}へお問い合わせください。",
      effective_text: `${state.text ?? DEFAULT_TEXT}\n解決しない場合は、${state.contact ?? DEFAULT_CONTACT}へお問い合わせください。`,
      max_chars: 500,
      contact_max_chars: 200,
      updated_at: null,
    });
  await page.route("**/api/settings/answer-notice", async (route) => {
    if (route.request().method() === "PUT") {
      const payload = route.request().postDataJSON() as NoticeState;
      saved.push(payload);
      state.enabled = payload.enabled;
      state.text = payload.text;
      state.contact = payload.contact ?? null;
    }
    await route.fulfill({ json: body() });
  });
  return saved;
}

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
  await mockDatabaseReady(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`回答の注意書きを書き換えて保存し、既定の文に戻せる (${viewport.name})`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const saved = await mockAnswerNotice(page);
    await page.goto("/settings/answer-notice");

    await expect(page.getByRole("heading", { name: "回答の注意書き", level: 1 })).toBeVisible();
    const textarea = page.getByLabel("本文");
    await expect(textarea).toHaveValue(DEFAULT_TEXT);
    await expect(page.getByTestId("answer-notice-source")).toHaveText("既定の文を使っています。");
    await expect(page.getByRole("button", { name: "既定の文に戻す" })).toBeDisabled();
    const preview = page.getByTestId("answer-notice-preview");
    await expect(preview.getByRole("complementary", { name: "回答の注意書き" })).toContainText(
      "解決しない場合は、既定の窓口へお問い合わせください。"
    );

    await textarea.fill("AI の回答は誤ることがあります。\nヘルプデスク: https://example.com/help");
    await expect(preview.getByRole("link", { name: "https://example.com/help" })).toHaveAttribute(
      "href",
      "https://example.com/help",
    );
    await page.getByRole("button", { name: "保存" }).click();
    // 窓口（#2400）も書き換える。表示の確認の案内の 1 行に入る。
    const contact = page.getByRole("textbox", { name: "窓口", exact: true });
    await expect(contact).toHaveValue(DEFAULT_CONTACT);
    await contact.fill("窓口 A（内線 1234）");
    await expect(preview).toContainText("解決しない場合は、窓口 A（内線 1234）へお問い合わせください。");
    await page.getByRole("button", { name: "保存" }).click();
    await expect.poll(() => saved.at(-1)).toEqual({
      enabled: true,
      text: "AI の回答は誤ることがあります。\nヘルプデスク: https://example.com/help",
      contact: "窓口 A（内線 1234）",
    });
    await expect(page.getByTestId("answer-notice-source")).toHaveText("管理者が変えた文を使っています。");

    await page.getByRole("button", { name: "既定の文に戻す" }).click();
    await expect(textarea).toHaveValue(DEFAULT_TEXT);
    await expect(contact).toHaveValue(DEFAULT_CONTACT);
    await page.getByRole("button", { name: "保存" }).click();
    await expect.poll(() => saved.at(-1)).toEqual({ enabled: true, text: null, contact: null });

    await expectNoPageOverflow(page);
  });
}

test("無効にすると表示の確認に注意書きを出さず、無効を保存する", async ({ page }) => {
  const saved = await mockAnswerNotice(page);
  await page.goto("/settings/answer-notice");
  const toggle = page.getByRole("switch", { name: "回答の末尾に注意書きを出す" });
  await expect(toggle).toHaveAttribute("aria-checked", "true");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-checked", "false");
  await expect(page.getByTestId("answer-notice-preview")).toContainText("注意書きは無効です。");
  await page.getByRole("button", { name: "保存" }).click();
  await expect.poll(() => saved.at(-1)).toEqual({ enabled: false, text: null, contact: null });
});

// 運用設定は「システムテーブル → 軌跡の本文記録 → チャットの添付 → 回答の注意書き」（#2449・#2517・#2546）。
test("サイドナビの運用設定のチャットの添付の下に回答の注意書きがある", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await mockAnswerNotice(page);
  await page.goto("/settings/answer-notice");
  const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
  const operations = sidebar.locator("#nav-section-nav-section-operations");
  await expect(operations.getByRole("link").nth(1)).toHaveAttribute("href", "/settings/trace-recording");
  await expect(operations.getByRole("link").nth(2)).toHaveAttribute("href", "/settings/chat-attachments");
  await expect(operations.getByRole("link").nth(3)).toHaveAttribute("href", "/settings/answer-notice");
  await expect(operations.getByRole("link").nth(3)).toHaveAccessibleName("回答の注意書き");
  await expect(
    sidebar.locator("#nav-section-nav-section-settings").locator('a[href="/settings/answer-notice"]')
  ).toHaveCount(0);
});
