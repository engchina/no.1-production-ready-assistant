import { expect, test, type Page } from "./fixtures/test";
import { LOCAL_AUTH_ME, expandSidebarSections, openSidebarNav } from "./_helpers";

async function mockApi(page: Page) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/api/auth/me") {
      await route.fulfill({ json: LOCAL_AUTH_ME });
      return;
    }
    await route.fulfill({ json: { data: null, error_messages: [], warning_messages: [] } });
  });
}

test.describe("サイドナビのセクション折りたたみ", { tag: "@responsive" }, () => {
  test("保存値が無いときは AI 活用と今のページのセクションだけを開き、開閉は再読み込み後も保つ（#1563）", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await mockApi(page);
    await page.goto("/settings/appearance");

    const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
    // 業務の入口の「AI 活用」と、今のページ（外観と証明書）を含む「システム設定」だけが開いている。
    await expect(sidebar.getByRole("button", { name: "AI 活用 を折りたたむ" })).toHaveAttribute("aria-expanded", "true");
    await expect(sidebar.getByRole("button", { name: "システム設定 を折りたたむ" })).toHaveAttribute(
      "aria-expanded",
      "true"
    );
    for (const section of ["ナレッジ構築", "取込・検索・回答設定", "改善・運用", "セキュリティ設定", "ユーザーとロール", "運用設定"]) {
      await expect(sidebar.getByRole("button", { name: `${section} を展開` })).toHaveAttribute("aria-expanded", "false");
    }
    await expect(sidebar.getByRole("link", { name: "チャット" })).toBeVisible();
    await expect(sidebar.getByRole("link", { name: "文書アップロード" })).toHaveCount(0);

    // 利用者が開閉した状態は localStorage に保存し、再読み込み後も既定より優先する。
    await sidebar.getByRole("button", { name: "ナレッジ構築 を展開" }).click();
    await sidebar.getByRole("button", { name: "AI 活用 を折りたたむ" }).click();
    await page.reload();
    await expect(sidebar.getByRole("button", { name: "ナレッジ構築 を折りたたむ" })).toHaveAttribute(
      "aria-expanded",
      "true"
    );
    await expect(sidebar.getByRole("link", { name: "文書アップロード" })).toBeVisible();
    await expect(sidebar.getByRole("button", { name: "AI 活用 を展開" })).toHaveAttribute("aria-expanded", "false");
    await expect(sidebar.getByRole("link", { name: "チャット" })).toHaveCount(0);
  });

  test("キーボード（Enter / Space）で開閉でき aria-expanded が反映される", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await mockApi(page);
    await page.goto("/settings/appearance");

    const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
    const ragItem = sidebar.getByText("RAG 検索", { exact: true });
    await expect(ragItem).toBeVisible();

    // AI 活用セクション見出しへフォーカスして Enter で折りたたむ。
    const toggle = sidebar.getByRole("button", { name: "AI 活用 を折りたたむ" });
    await toggle.focus();
    await page.keyboard.press("Enter");
    await expect(ragItem).toBeHidden();
    await expect(
      sidebar.getByRole("button", { name: "AI 活用 を展開" })
    ).toHaveAttribute("aria-expanded", "false");

    // Space で再展開。
    await sidebar.getByRole("button", { name: "AI 活用 を展開" }).focus();
    await page.keyboard.press(" ");
    await expect(ragItem).toBeVisible();
  });

  test("折りたたんだセクションの項目はアクセシビリティツリー / タブ順から除外される", async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await mockApi(page);
    await page.goto("/settings/appearance");

    const sidebar = page.getByRole("complementary", { name: "サイドナビゲーション" });
    const evalLink = sidebar
      .getByRole("link", { name: "品質評価", exact: true })
      .and(sidebar.locator('a[href="/evaluation"]'));
    // 品質評価は「改善・運用」セクションにあり（#409）、既定では閉じている（#1563）。
    // 閉じたセクションは visibility:hidden + inert で a11y ツリーから外れ、role として見えなくなる。
    await expect(evalLink).toHaveCount(0);

    // 展開で現れる。
    await sidebar.getByRole("button", { name: "改善・運用 を展開" }).click();
    await expect(evalLink).toHaveCount(1);

    // 折りたたむと再び外れる。
    await sidebar.getByRole("button", { name: "改善・運用 を折りたたむ" }).click();
    await expect(evalLink).toHaveCount(0);
  });

  test("狭幅（375px）のドロワーの中でもセクションを開閉できる（#367）", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 800 });
    await mockApi(page);
    await page.goto("/settings/appearance");

    // md 未満はドロワーの中に展開した幅で描くため、desktop と同じくセクションの開閉ボタンを出す。
    const sidebar = await openSidebarNav(page);
    const ragItem = sidebar.getByText("RAG 検索", { exact: true });
    await expect(ragItem).toBeVisible();
    await sidebar.getByRole("button", { name: "AI 活用 を折りたたむ" }).click();
    await expect(ragItem).toBeHidden();
    await sidebar.getByRole("button", { name: "AI 活用 を展開" }).click();
    await expect(ragItem).toBeVisible();
    // 既定で閉じている「取込・検索・回答設定」もドロワーの中で開ける（#1563）。
    await expandSidebarSections(sidebar, "取込・検索・回答設定");
    await expect(sidebar.getByRole("link", { name: "検索方法" })).toBeVisible();
  });
});
