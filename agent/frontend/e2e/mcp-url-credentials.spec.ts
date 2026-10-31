import type { Page } from "@playwright/test";

import { expect, test } from "./fixtures/mock-api";

/**
 * MCP 接続の URL に書いた資格情報（userinfo・資格情報らしい query）を出さない（#1056）。
 * - 保存の前に URL の欄の下でエラーを出し、資格情報は「認証」の欄へ案内する（backend も 422 で拒否する）
 * - 保存済みの URL の資格情報は一覧・詳細で「***」に伏せ、URL を変えない保存では URL を送らない
 */

const LEGACY_URL = "https://svc-user:pa55-1056@erp.example.test/mcp?tenant=t1&api_key=k-1056";
const MASKED_URL = "https://***@erp.example.test/mcp?tenant=t1&api_key=***";

async function expectNoHorizontalOverflow(page: Page) {
  const hasNoOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth <= document.documentElement.clientWidth
  );
  expect(hasNoOverflow).toBe(true);
}

test.describe("MCP 接続の URL の資格情報", () => {
  test("URL に資格情報を書くと、保存せずに欄の下で「認証」の欄へ案内する", async ({ page, mockApi }) => {
    await page.goto("/settings/mcp-connections?id=new");
    await page.locator("#mcp-server-id").fill("crm1056");
    const url = page.locator("#mcp-server-base-url");

    await url.fill("https://svc:pw-1056@crm.example.test/mcp");
    await page.getByRole("button", { name: "作成" }).click();
    await expect(url).toBeFocused();
    await expect(url).toHaveAttribute("aria-invalid", "true");
    await expect(
      page.getByText("MCP の URL にユーザー名・パスワード（user:pass@）を含めないでください。", { exact: false })
    ).toBeVisible();

    await url.fill("https://crm.example.test/mcp?tenant=t1&apiKey=pw-1056");
    // 入力し直したらエラーを消す。
    await expect(url).not.toHaveAttribute("aria-invalid", "true");
    await page.getByRole("button", { name: "作成" }).click();
    await expect(page.getByText("MCP の URL に資格情報のパラメータ（apiKey）を含めないでください。", { exact: false })).toBeVisible();
    await expect(page.getByText("「認証」の欄（API キー・OAuth）で設定してください。", { exact: false })).toBeVisible();
    expect(mockApi.lastRequest("POST", "/api/settings/mcp-connections")).toBeUndefined();

    // 資格情報ではない query はそのまま保存できる。
    await url.fill("https://crm.example.test/mcp?tenant=t1");
    await page.getByRole("button", { name: "作成" }).click();
    await expect(page.getByText("MCP 接続を追加しました")).toBeVisible();
    expect(mockApi.lastRequest("POST", "/api/settings/mcp-connections")?.body).toMatchObject({
      server_id: "crm1056",
      base_url: "https://crm.example.test/mcp?tenant=t1",
    });
  });

  test("保存済みの URL の資格情報は一覧と詳細で伏せ、URL を変えない保存では送らない", async ({ page, mockApi }) => {
    mockApi.state.mcpConnections.connections.push({
      server_id: "legacy1056",
      label: "旧 ERP",
      base_url: LEGACY_URL,
      auth_mode: "none",
      service_audience: null,
      timeout_seconds: 10,
      source: "runtime",
      removable: true,
      configured: true,
      api_key_configured: false,
      oauth_configured: false,
      session_configured: false,
      service_token_configured: false,
    });

    await page.goto("/settings/mcp-connections");
    const row = page.getByRole("row").filter({ hasText: "legacy1056" });
    await expect(row).toContainText(MASKED_URL);
    await expect(page.getByText("pa55-1056", { exact: false })).toHaveCount(0);
    await expect(page.getByText("k-1056", { exact: false })).toHaveCount(0);

    await page.goto("/settings/mcp-connections?id=legacy1056");
    const url = page.locator("#mcp-server-base-url");
    await expect(url).toHaveValue(MASKED_URL);
    await expect(page.getByText("保存済みの URL の資格情報は「***」で伏せて表示しています。", { exact: false })).toBeVisible();

    // URL 以外を変えた保存は URL を送らず、保存済みの URL を使い続ける。
    await page.locator("#mcp-server-timeout").fill("20");
    await page.getByRole("button", { name: "保存" }).click();
    await expect(page.getByText("MCP 接続を保存しました")).toBeVisible();
    const body = mockApi.lastRequest("PATCH", "/api/settings/mcp-connections/legacy1056")?.body as Record<string, unknown>;
    expect(body).toMatchObject({ timeout_seconds: 20 });
    expect(body).not.toHaveProperty("base_url");
    const saved = mockApi.state.mcpConnections.connections.find((item) => item.server_id === "legacy1056")!;
    expect(saved.base_url).toBe(LEGACY_URL);
    await expect(url).toHaveValue(MASKED_URL);

    // 伏せた URL の一部だけを変えても、資格情報が残る URL は保存しない（資格情報は「認証」の欄へ）。
    await url.fill(MASKED_URL.replace("tenant=t1", "tenant=t2"));
    await page.getByRole("button", { name: "保存" }).click();
    await expect(url).toBeFocused();
    await expect(page.getByText("MCP の URL にユーザー名・パスワード（user:pass@）を含めないでください。", { exact: false })).toBeVisible();

    // desktop と mobile-375（375px）の両方の project で、URL の欄と案内が横にはみ出さない。
    await expectNoHorizontalOverflow(page);
  });
});
