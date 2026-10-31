import { expect, test } from "./fixtures/mock-api";
import { expandSidebarSections, openSidebarNav } from "./fixtures/nav";

// 外観と証明書は platform の共有パッケージの画面を使う（#95）。テーマはライトだけで、選択は無い（#17）。

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`外観と証明書にテーマの選択を出さず、ライトで描く (${viewport.name})`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    // OS がダークでも、保存済みの theme が dark でもライトで描く。
    await page.emulateMedia({ colorScheme: "dark" });
    await page.addInitScript(() => {
      window.localStorage.setItem("production-ready-agent.ui", JSON.stringify({ state: { theme: "dark" }, version: 0 }));
    });
    await page.goto("/settings/appearance");

    await expect(page.getByRole("heading", { name: "外観と証明書", level: 1 })).toBeVisible();
    await expect(page.getByTestId("appearance-ca-certificate")).toBeVisible();
    await expect(page.getByTestId("appearance-theme-toggle")).toHaveCount(0);
    for (const name of ["ライト", "ダーク", "自動（OS 設定）"]) {
      await expect(page.getByRole("button", { name, exact: true })).toHaveCount(0);
    }
    await expect(page.locator("html")).not.toHaveClass(/\bdark\b/);
    expect(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme)).toBe("light");

    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
}

// HTTPS の証明書（#1316）: 1 台の Compute の Nginx が /platform/ca.crt で CA の証明書を配る。製品は /agent/ の下でも
// サイトの root の /platform/ca.crt を見る。ローカルの開発（Vite）は配っていないので説明だけを出す。
for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`外観と証明書の HTTPS の証明書（${viewport.name}）`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/settings/appearance");

    // 配っていない環境（ローカルの開発）: 説明だけで、ダウンロードは出さない。
    const card = page.getByTestId("appearance-ca-certificate");
    await expect(card.getByText("HTTPS の証明書", { exact: true })).toBeVisible();
    await expect(card.getByText("この環境では HTTPS の自己署名の証明書を使っていません。")).toBeVisible();
    await expect(card.getByRole("link", { name: "CA の証明書をダウンロード" })).toHaveCount(0);
    await page.screenshot({ path: test.info().outputPath("ca-certificate-unavailable.png"), fullPage: true });

    // 配っている環境: ダウンロードのリンクと、端末への取り込み方。
    await page.route("**/platform/ca.crt", (route) =>
      route.fulfill({ status: 200, headers: { "Content-Type": "application/x-x509-ca-cert" }, body: "" }),
    );
    await page.reload();
    const download = card.getByRole("link", { name: "CA の証明書をダウンロード" });
    await expect(download).toHaveAttribute("href", "/platform/ca.crt");
    await card.getByText("端末への取り込み方").click();
    // OS の見出しは使うブラウザを添え、ブラウザの項は Edge → Chrome → Firefox の順（#1662）。
    await expect(card.getByTestId("appearance-ca-certificate-steps").locator("dt")).toHaveText([
      "Windows（Edge・Chrome）",
      "macOS（Safari・Chrome・Edge）",
      "iPhone / iPad（すべてのブラウザ）",
      "Android（Chrome）",
      "Edge",
      "Chrome",
      "Firefox",
    ]);
    await expect(card.getByText("Firefox", { exact: true })).toBeVisible();
    await page.screenshot({ path: test.info().outputPath("ca-certificate-available.png"), fullPage: true });
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    );
    expect(overflow).toBeLessThanOrEqual(0);
  });
}

test("サイドナビのシステム設定に外観と証明書がある", { tag: "@responsive" }, async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/settings/appearance");
  await expect(page.getByRole("link", { name: "外観と証明書" })).toHaveAttribute("href", "/settings/appearance");
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
]) {
  test(`サイドナビは利用者向け → 管理者向け（AI 活用 → Agent 構築 → … → システム設定）の順に並べる (${viewport.name})`, { tag: "@responsive" }, async ({ page }) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    await page.goto("/settings/appearance");
    // 375px ではナビがドロワー（#367）。開いてから並びを読む。
    const sidebar = await openSidebarNav(page);
    // 認証の確認後にサイドナビを描く。最後のセクションが出るまで待ってから並びを読む。
    await expect(sidebar.locator("#nav-section-nav-section-settings")).toHaveCount(1);
    // 既定では「AI 活用」と今のページのセクションだけが開いている（#1563）。すべて開いて項目を確かめる。
    await expandSidebarSections(sidebar);

    // 並びは NL2SQL / RAG と同じく、上に一般の利用者が使う画面、下に管理者の画面
    // 「AI 活用 → Agent 構築 → 改善・運用 → セキュリティ設定 → ユーザーとロール → 運用設定 → システム設定」
    // （#87 / #215 / #658 / #774 / #776 / #791）。
    const sectionIds = await sidebar
      .locator('[id^="nav-section-nav-section-"]')
      .evaluateAll((elements) => elements.map((element) => element.id));
    expect(sectionIds).toEqual([
      "nav-section-nav-section-use",
      "nav-section-nav-section-build",
      "nav-section-nav-section-improve",
      "nav-section-nav-section-security",
      "nav-section-nav-section-userRoles",
      "nav-section-nav-section-operations",
      "nav-section-nav-section-settings",
    ]);

    // 各セクションの見出しは利用者の言葉（「Control Plane」のような基盤の用語を出さない。#791）。
    for (const title of ["AI 活用", "Agent 構築", "改善・運用", "運用設定"]) {
      await expect(sidebar.getByText(title, { exact: true })).toBeVisible();
    }
    await expect(sidebar.getByText(/Control Plane/)).toHaveCount(0);
    // 見出しは CSS で大文字にしない（innerText は text-transform を反映する。#801）。
    expect(await sidebar.getByText("Agent 構築", { exact: true }).evaluate((element) => (element as HTMLElement).innerText)).toBe(
      "Agent 構築"
    );
    // AI 活用は一般の利用者の画面（チャット → 承認 → 実行履歴。#1885）。チャットが既定の入口（#768 / #791）。
    const linkHrefs = (section: string) =>
      sidebar
        .locator(`#nav-section-nav-section-${section} a[href]`)
        .evaluateAll((links) => links.map((link) => link.getAttribute("href")));
    expect(await linkHrefs("use")).toEqual(["/chat", "/approvals", "/runs"]);
    // Agent 構築は管理者が業務 Agent を作る画面（RAG のナレッジ構築・NL2SQL のデータ準備に当たる）。
    expect(await linkHrefs("build")).toEqual(["/agents", "/skills"]);
    // 改善・運用は RAG / NL2SQL と同じ品質評価・フィードバックの後に、Agent の利用状況（#772）と監査ログ（#791）。
    expect(await linkHrefs("improve")).toEqual(["/evaluation", "/feedback", "/usage", "/audit"]);
    // セキュリティ設定は権限管理、運用設定はシステムテーブル（先頭。#751）と Agent 固有の2項目
    // （実行環境・MCP 接続。#757 / #762。バックアップと復元は #1961 で削除）、
    // ユーザーとロール・システム設定は3製品共通。
    // 改善・運用は品質評価（#776。アイコンは RAG / NL2SQL と同じ FlaskConical）。
    await expect(sidebar.locator('#nav-section-nav-section-improve a[href="/evaluation"]')).toHaveCount(1);
    // 改善・運用はフィードバック（#774。アイコンは RAG / NL2SQL と同じ ThumbsUp）。
    await expect(sidebar.locator('#nav-section-nav-section-improve a[href="/feedback"]')).toHaveCount(1);
    const security = sidebar.locator("#nav-section-nav-section-security");
    const operations = sidebar.locator("#nav-section-nav-section-operations");
    const userRoles = sidebar.locator("#nav-section-nav-section-userRoles");
    const settings = sidebar.locator("#nav-section-nav-section-settings");
    await expect(security.getByRole("link")).toHaveCount(1);
    await expect(security.locator('a[href="/settings/security/permissions"]')).toHaveCount(1);
    await expect(operations.getByRole("link")).toHaveCount(6);
    // 運用設定の先頭はシステムテーブル、その下にチャットの添付（#2517・#2546）と回答の注意書き（RAG / NL2SQL と同じ。#2449）。
    await expect(operations.getByRole("link").first()).toHaveAttribute("href", "/settings/system-tables");
    await expect(operations.getByRole("link").nth(1)).toHaveAttribute("href", "/settings/trace-recording");
    await expect(operations.getByRole("link").nth(2)).toHaveAttribute("href", "/settings/chat-attachments");
    await expect(operations.getByRole("link").nth(3)).toHaveAttribute("href", "/settings/answer-notice");
    await expect(userRoles.getByRole("link")).toHaveCount(3);
    await expect(userRoles.getByRole("link").last()).toHaveAttribute("href", "/settings/security/api-keys");
    // システム設定は 3 製品で共通の 5 項目（回答の注意書きは運用設定。#2449）。
    await expect(settings.getByRole("link")).toHaveCount(5);
    for (const href of [
      "/settings/system-tables",
      "/settings/trace-recording",
      "/settings/answer-notice",
      "/settings/chat-attachments",
      "/runtimes",
      "/settings/mcp-connections",
    ]) {
      await expect(operations.locator(`a[href="${href}"]`)).toHaveCount(1);
    }
    for (const href of ["/settings/security/users", "/settings/security/roles"]) {
      await expect(userRoles.locator(`a[href="${href}"]`)).toHaveCount(1);
    }
    await expect(sidebar.getByText("セキュリティ設定", { exact: true })).toBeVisible();
    // 同じアイコンを 2 つの項目に使わない（機能ごとに違うアイコン。#658）。
    const iconSignatures = await sidebar.locator("nav a svg").evaluateAll((icons) =>
      icons.map((icon) => icon.innerHTML.replace(/\s+/g, " ").trim())
    );
    expect(new Set(iconSignatures).size).toBe(iconSignatures.length);
    for (const href of [
      "/settings/oci",
      "/settings/upload-storage",
      "/settings/model",
      "/settings/database",
      "/settings/appearance",
    ]) {
      await expect(settings.locator(`a[href="${href}"]`)).toHaveCount(1);
    }
  });
}
