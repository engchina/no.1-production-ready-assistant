import { expect, test, type Page } from "./fixtures/test";
import { mockLocalAuth } from "./_helpers";

/**
 * DB 停止時、閲覧系ページが「全画面エラー」ではなく
 * 空状態 + 縮退バナーで通常どおり開けることを検証する。
 */

const DB_WARNING = "データベースに接続できませんでした。データベースの起動状態を確認して再試行してください。";

/** 空ページ + warning の縮退エンベロープ。 */
function degradedPage() {
  return {
    data: { items: [], total: 0, limit: 50, offset: 0, has_next: false },
    error_messages: [],
    warning_messages: [DB_WARNING],
  };
}

async function routeDegraded(page: Page) {
  await mockLocalAuth(page);
  // DB ゲートは通過させ(セッション中に個別クエリが縮退するケースを検証する)
  await page.route("**/api/ready/database", (route) =>
    route.fulfill({
      json: { data: { status: "ok", check: "ok", detail: null }, error_messages: [], warning_messages: [] },
    })
  );
  await page.route("**/api/documents**", (route) => route.fulfill({ json: degradedPage() }));
  await page.route("**/api/knowledge-bases**", (route) => route.fulfill({ json: degradedPage() }));
}

test("文書インデックスは DB 停止時も空状態 + 縮退バナーで開ける", async ({ page }) => {
  await routeDegraded(page);
  await page.goto("/file-list");

  // 全画面エラー(alert + 再試行のみ)ではなく、ページ本体が表示される
  await expect(page.getByRole("status").filter({ hasText: "データベースに接続できません" })).toBeVisible();
  await expect(page.getByText(DB_WARNING)).toBeVisible();
  // ページ本体(空状態)が通常どおり描画される
  await expect(page.getByText("該当するドキュメントがありません。")).toBeVisible();

  // 復旧導線: 再試行 + データベース設定リンク
  await expect(page.getByRole("button", { name: "再試行" }).first()).toBeVisible();
  const settingsLink = page.getByRole("link", { name: /データベース設定を開く/ });
  await expect(settingsLink).toHaveAttribute("href", "/settings/database#adb-management");
});

test("ナレッジベース管理は DB 停止時も作成の入口 + 縮退バナーで開ける", async ({ page }) => {
  await routeDegraded(page);
  await page.goto("/knowledge-bases");

  await expect(page.getByRole("status").filter({ hasText: "データベースに接続できません" })).toBeVisible();
  // 作成の入口（PageHeader の「新規作成」）は利用可能(ページが死んでいない。#555)
  await expect(page.getByRole("button", { name: "新規作成" })).toBeVisible();
  const settingsLink = page.getByRole("link", { name: /データベース設定を開く/ });
  await expect(settingsLink).toHaveAttribute("href", "/settings/database#adb-management");
});
