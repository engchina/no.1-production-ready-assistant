import { expect, test, type Page } from "./fixtures/test";

import {
  apiEnvelope,
  dbUser,
  expectNoPageOverflow,
  mockAuthUser,
  mockDatabaseReady,
  openSidebarNav,
} from "./_helpers";
import { findClippedPlaceholders, jumpToPage, measureHorizontalOverflow } from "../../../platform/packages/ui/e2e/layout-checks";

// 外部の MCP クライアント向けの API キー（RAG / NL2SQL 共通の画面。#1805）。
// 自分のキーはアカウントの画面の「API キー」のタブ（サイドナビのアカウントの欄から開く。メニュー権限なし。#1873）、
// 全員のキーは「ユーザーとロール」の管理の画面。

const TOKEN = "prak_0123456789abcdef_" + "S".repeat(43);

interface KeyPayload {
  id: string;
  name: string;
  token_prefix: string;
  products: string[];
  owner_user_uuid: string;
  owner_display_name: string;
  created_at: string;
  expires_at: string | null;
  last_used_at: string | null;
  expired: boolean;
}

function key(overrides: Partial<KeyPayload> = {}): KeyPayload {
  return {
    id: "0123456789abcdef",
    name: "会社の PC の Claude Code",
    token_prefix: "prak_0123456789abcdef_SSSS",
    products: ["rag"],
    owner_user_uuid: "11111111-1111-1111-1111-111111111111",
    owner_display_name: "利用者 一郎",
    created_at: "2026-10-14T00:00:00Z",
    expires_at: "2027-01-12T00:00:00Z",
    last_used_at: null,
    expired: false,
    ...overrides,
  };
}

async function mockMyKeys(page: Page) {
  const keys: KeyPayload[] = [];
  const created: Record<string, unknown>[] = [];
  const revoked: string[] = [];
  await mockDatabaseReady(page);
  await page.route("**/api/account/api-keys**", async (route) => {
    const request = route.request();
    if (request.method() === "POST") {
      const body = request.postDataJSON() as Record<string, unknown>;
      created.push(body);
      const item = key({ name: String(body.name), products: body.products as string[] });
      keys.unshift(item);
      return route.fulfill({ json: apiEnvelope({ key: item, token: TOKEN }) });
    }
    if (request.method() === "DELETE") {
      const id = new URL(request.url()).pathname.split("/").pop() ?? "";
      revoked.push(id);
      keys.splice(keys.findIndex((item) => item.id === id), 1);
      return route.fulfill({ json: apiEnvelope({ id }) });
    }
    return route.fulfill({ json: apiEnvelope({ keys, public_base_url: "https://203.0.113.10" }) });
  });
  return { created, revoked };
}

test.describe("API キー", { tag: "@responsive" }, () => {
  test("アカウントの画面の「API キー」のタブで自分のキーを発行し、秘密を 1 回だけ見て、削除できる（権限なし）", async ({ page }) => {
    const calls = await mockMyKeys(page);
    // 自分のキーの画面はメニュー権限を要しない（検索だけの利用者）。
    await mockAuthUser(page, { permissions: ["menu.search"] });

    await page.goto("/search");
    const sidebar = await openSidebarNav(page);
    // 個人の「API キー」はサイドナビの行に無く、アカウントの欄から開くアカウントの画面のタブ（#1873）。
    await expect(sidebar.getByRole("button", { name: "API キー" })).toHaveCount(0);
    await sidebar.getByRole("link", { name: /^アカウント設定を開く/ }).click();
    await expect(page).toHaveURL(/\/account$/);
    await expect(page.getByRole("heading", { name: "アカウント", level: 1 })).toBeVisible();
    await expect(page.getByRole("tab", { name: "API キー" })).toHaveAttribute("aria-selected", "true");
    // 開発の dev server（base が /）では開いている製品の URL だけを出す。
    await expect(page.getByTestId("api-keys-endpoint-rag")).toContainText("/api/mcp");

    // 接続先（#1870）: この画面の URL と、配備の公開の URL（PLATFORM_PUBLIC_BASE_URL）を製品ごとに出し、コピーできる。
    await expect(page.getByTestId("api-keys-endpoint-rag-local")).toContainText("/api/mcp");
    await expect(page.getByTestId("api-keys-endpoint-rag-public")).toHaveText("https://203.0.113.10/rag/api/mcp");
    await expect(page.getByTestId("api-keys-endpoint-nl2sql-public")).toHaveText("https://203.0.113.10/nl2sql/api/mcp");
    await page.context().grantPermissions(["clipboard-read", "clipboard-write"]);
    await page.getByRole("button", { name: "RAGの公開の URL（OCI）: URL をコピー" }).click();
    await expect(page.getByText("URL をコピーしました")).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe("https://203.0.113.10/rag/api/mcp");
    await expect(page.getByTestId("api-keys-ca-note")).toContainText("https://203.0.113.10/platform/ca.crt");
    // 登録の例は skills/README.md と同じ形（サーバー名 rag / nl2sql・単一引用符・待ち時間・自作の CA）。
    await page.getByText("Claude Code の登録の例").click();
    const example = page.getByTestId("api-keys-example");
    await expect(example).toContainText("claude mcp add --scope user --transport http rag https://203.0.113.10/rag/api/mcp");
    await expect(example).toContainText("claude mcp add --scope user --transport http nl2sql https://203.0.113.10/nl2sql/api/mcp");
    await expect(example).toContainText("--header 'Authorization: Bearer ${PR_API_KEY}'");
    await expect(example).toContainText("export MCP_TOOL_TIMEOUT=600000");
    await expect(example).toContainText("NODE_EXTRA_CA_CERTS");
    await expect(page.getByText("API キーはまだありません。")).toBeVisible();

    // 名前が空なら発行しない。
    await page.getByTestId("api-key-create").click();
    await expect(page.getByText("名前を入力してください。")).toBeVisible();
    expect(calls.created).toHaveLength(0);

    // 発行の既定の製品は、開いている製品によらず 3 製品のすべて（#1869・#1874）。
    await expect(page.getByTestId("api-key-product-rag")).toBeChecked();
    await expect(page.getByTestId("api-key-product-nl2sql")).toBeChecked();
    await expect(page.getByTestId("api-key-product-agent")).toBeChecked();
    // RAG の画面では業務 Agent を絞れない（Agent の画面で発行する）ことを案内する。
    await expect(page.getByTestId("api-key-agent-scope-elsewhere")).toBeVisible();
    await page.getByLabel("名前（用途）").fill("会社の PC の Claude Code");
    await page.getByTestId("api-key-product-nl2sql").uncheck();
    await page.getByTestId("api-key-product-agent").uncheck();
    await expect(page.getByTestId("api-key-agent-scope-elsewhere")).toHaveCount(0);
    await page.getByTestId("api-key-create").click();
    await expect(page.getByTestId("api-key-token")).toHaveValue(TOKEN);
    expect(calls.created).toEqual([{ name: "会社の PC の Claude Code", products: ["rag"], expires_in_days: 90 }]);
    // 発行の後は欄を既定（すべて）へ戻す。
    await expect(page.getByTestId("api-key-product-rag")).toBeChecked();
    await expect(page.getByTestId("api-key-product-nl2sql")).toBeChecked();
    await expect(page.getByTestId("api-key-product-agent")).toBeChecked();
    const list = page.getByRole("table", { name: "API キーの一覧" });
    await expect(list.getByText("会社の PC の Claude Code")).toBeVisible();
    // 一覧は秘密を出さない（見分けるための先頭だけ）。
    await expect(list).not.toContainText(TOKEN);
    await expectNoPageOverflow(page);

    // 「保管しました」で閉じたら二度と出さない。
    await page.getByRole("button", { name: "保管しました" }).click();
    await expect(page.getByTestId("api-key-token")).toHaveCount(0);

    await page.getByTestId("api-key-row-actions-0123456789abcdef-trigger").click();
    // 「削除」は 1 つの操作。確認のダイアログで、すぐに使えなくなり元に戻せないことを伝える（#1873）。
    await page.getByRole("menuitem", { name: "削除" }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toContainText("「会社の PC の Claude Code」を削除しますか");
    await expect(dialog).toContainText("すぐに使えなくなり、元に戻せません");
    await dialog.getByRole("button", { name: "削除" }).click();
    await expect(page.getByText("API キーを削除しました")).toBeVisible();
    await expect(page.getByText("API キーはまだありません。")).toBeVisible();
    expect(calls.revoked).toEqual(["0123456789abcdef"]);
  });

  test("管理の画面は全員のキーを利用者付きで出し、メニュー権限が無ければ開けない", async ({ page }) => {
    await mockDatabaseReady(page);
    await page.route("**/api/security/api-keys**", (route) =>
      route.fulfill({
        json: apiEnvelope({
          keys: [
            key(),
            key({ id: "fedcba9876543210", name: "CI", owner_display_name: "", expired: true }),
          ],
        }),
      })
    );
    const setUser = await mockAuthUser(page, { permissions: ["menu.security_api_keys"] });

    // 管理のメニュー権限だけの利用者は、ナビの先頭で開ける画面として全員のキーの画面を開く。
    await page.goto("/");
    await expect(page).toHaveURL(/\/settings\/security\/api-keys$/);
    // 開いている画面のセクション（ユーザーとロール）は自動で開く。
    const sidebar = await openSidebarNav(page);
    await expect(sidebar.getByRole("link", { name: "API キー" })).toHaveAttribute("aria-current", "page");
    if (await page.getByTestId("nav-drawer-trigger").isVisible()) await page.keyboard.press("Escape");
    const list = page.getByRole("table", { name: "API キーの一覧" });
    await expect(list.getByText("利用者 一郎")).toBeVisible();
    await expect(list.getByText("削除されたユーザー")).toBeVisible();
    await expect(list.getByText("期限切れ")).toBeVisible();
    // 管理の画面からはキーを発行しない。
    await expect(page.getByTestId("api-key-create")).toHaveCount(0);
    await expectNoPageOverflow(page);

    setUser(dbUser({ permissions: ["menu.search"] }));
    await page.goto("/settings/security/api-keys");
    await expect(page).toHaveURL(/\/forbidden$/);
  });
});

// 一覧の件数とページ送り（#1891）: 件数は検索欄と同じ行の右の ListCount 1 か所（高さ・中心をそろえる）、
// ページ送りは表の直下の advanced（範囲・表示件数・最初へ・前へ・ページ番号・次へ・最後へ）で 1 ページなら出さない。
// ページ番号と表示件数は作業状態（sessionStorage）に残る。
function manyKeys(count: number): KeyPayload[] {
  return Array.from({ length: count }, (_, index) => {
    const id = String(index + 1).padStart(16, "0");
    return key({ id, name: `キー ${String(index + 1).padStart(2, "0")}`, token_prefix: `prak_${id}_SSSS` });
  });
}

async function useTheme(page: Page, theme: "light" | "dark") {
  await page.addInitScript((value) => {
    window.localStorage.setItem("production-ready-rag.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
  }, theme);
}

for (const theme of ["light", "dark"] as const) {
  test(`API キーの一覧の件数とページ送り（全員のキー・自分のキー、${theme}）`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    await useTheme(page, theme);
    await mockDatabaseReady(page);
    let adminKeys = manyKeys(1);
    await page.route("**/api/security/api-keys**", (route) => route.fulfill({ json: apiEnvelope({ keys: adminKeys }) }));
    let myKeys = manyKeys(1);
    await page.route("**/api/account/api-keys**", (route) =>
      route.fulfill({ json: apiEnvelope({ keys: myKeys, public_base_url: "" }) })
    );
    await mockAuthUser(page, { permissions: ["menu.search", "menu.security_api_keys"] });
    const wide = (page.viewportSize()?.width ?? 0) >= 640;

    // 1 件: 件数は「全 1 件」、ページ送りは出さない。件数は検索欄と同じ行で、縦の中心がそろう。
    await page.goto("/settings/security/api-keys");
    const toolbar = page.getByTestId("api-key-list-toolbar");
    const count = toolbar.locator("[data-list-count]");
    await expect(count).toHaveText("全 1 件");
    await expect(page.getByTestId("api-keys-admin-pagination")).toHaveCount(0);
    if (wide) {
      const searchBox = await page.locator("#api-key-search").boundingBox();
      const countBox = await count.boundingBox();
      expect(searchBox && countBox).toBeTruthy();
      if (searchBox && countBox) {
        expect(Math.abs(searchBox.y + searchBox.height / 2 - (countBox.y + countBox.height / 2))).toBeLessThan(2);
      }
    }
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
    expect(await findClippedPlaceholders(page)).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`api-keys-admin-1-${theme}.png`), fullPage: true });

    // 53 件: 25 件ずつ、ページ番号を入れて移り、表示件数を変えると 1 ページ目へ。再読込でもページと件数が残る。
    adminKeys = manyKeys(53);
    await page.reload();
    await expect(count).toHaveText("全 53 件");
    const pagination = page.getByTestId("api-keys-admin-pagination");
    await expect(pagination).toContainText("1–25 / 53 件");
    await expect(page.getByRole("table", { name: "API キーの一覧" }).locator("tbody tr")).toHaveCount(25);
    await jumpToPage(pagination, 3);
    await expect(pagination).toContainText("51–53 / 53 件");
    await page.reload();
    await expect(pagination).toContainText("51–53 / 53 件");
    await pagination.getByRole("combobox", { name: "表示件数" }).click();
    await page.getByRole("option", { name: "100 件" }).click();
    await expect(pagination).toContainText("1–53 / 53 件");
    await expect(page.getByRole("table", { name: "API キーの一覧" }).locator("tbody tr")).toHaveCount(53);
    // 検索で絞ると「N 件（全 M 件）」。
    await page.locator("#api-key-search").fill("キー 1");
    await expect(count).toHaveText(/^\d+ 件（全 53 件）$/);
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
    await page.screenshot({ path: testInfo.outputPath(`api-keys-admin-53-${theme}.png`), fullPage: true });

    // 自分のキー（アカウントの画面）: 1 件はページ送りなし、27 件は表の直下にページ送り。
    await page.goto("/account?tab=api-keys");
    const myCount = page.getByTestId("my-api-key-list-toolbar").locator("[data-list-count]");
    await expect(myCount).toHaveText("全 1 件");
    await expect(page.getByTestId("my-api-keys-pagination")).toHaveCount(0);
    myKeys = manyKeys(27);
    await page.reload();
    await expect(myCount).toHaveText("全 27 件");
    const myPagination = page.getByTestId("my-api-keys-pagination");
    await expect(myPagination).toContainText("1–25 / 27 件");
    await myPagination.getByRole("button", { name: "次へ" }).click();
    await expect(myPagination).toContainText("26–27 / 27 件");
    expect(await measureHorizontalOverflow(page)).toBeLessThanOrEqual(1);
    expect(await findClippedPlaceholders(page)).toEqual([]);
    await page.screenshot({ path: testInfo.outputPath(`api-keys-mine-27-${theme}.png`), fullPage: true });
  });
}
