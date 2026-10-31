import type { Page } from "@playwright/test";

import { dbUser } from "./fixtures/auth";
import { expect, test, type MockApi } from "./fixtures/mock-api";

/**
 * DB ゲート（3製品共通の部品。#325）。システム設定の 5 画面以外は、DB が使えるまで本文だけを
 * 案内に替える（サイドナビは残す）。状態（未設定・接続できない・初期化が必要）ごとに見出しと導線を
 * 分け、設定を開けない利用者にはシステム管理者への連絡を案内する（#820）。
 */

const VIEWPORTS = [
  { name: "desktop", width: 1280, height: 800 },
  { name: "mobile", width: 375, height: 812 },
] as const;

const SETTINGS_HINT =
  "OCI 認証・アップロード保存先・モデル・データベース・外観と証明書の各設定ページは引き続き利用できます。";

type DbStatus = "ok" | "not_configured" | "unreachable" | "setup_required";

function setDatabaseStatus(
  mockApi: MockApi,
  status: DbStatus,
  check?: string,
  adbLifecycleState: string | null = null
) {
  Object.assign(mockApi.state.databaseStatus, {
    status,
    check: check ?? (status === "not_configured" ? "missing" : "ok"),
    detail: status === "unreachable" ? "Oracle connection probe failed (ORA-12514)." : null,
    adb_lifecycle_state: adbLifecycleState,
  });
}

/** システム設定もシステムテーブルも開けない利用者（実行履歴の閲覧だけ）。 */
const VIEWER = dbUser({
  login_user_id: "viewer.user",
  display_name: "閲覧 次郎",
  permissions: ["agent.runs.view"],
  allowed_agent_ids: ["default"],
});

const CONTACT_ADMIN_FOOTER =
  "データベースの起動・接続の設定とシステムテーブルの作成・更新は、システム管理者が行います。";

/** `src/lib/ui-store.ts` の永続化キー（テーマの選好を起動前に入れる）。 */
const UI_STORAGE_KEY = "production-ready-agent.ui";

async function useTheme(page: Page, theme: "light" | "dark") {
  await page.addInitScript(
    ([key, value]) => {
      window.localStorage.setItem(
        key,
        JSON.stringify({ state: { theme: value, sidebarCollapsed: false, collapsedSections: {} }, version: 0 })
      );
    },
    [UI_STORAGE_KEY, theme] as const
  );
}

/** 目視の確認用（`DB_GATE_SCREENSHOT_DIR` を渡したときだけ保存する）。 */
async function saveScreenshot(page: Page, name: string) {
  const dir = process.env.DB_GATE_SCREENSHOT_DIR;
  if (dir) await page.screenshot({ path: `${dir}/${name}.png`, fullPage: true });
}

function gateCard(page: Page) {
  return page.locator('section[aria-labelledby="database-unavailable-title"]');
}

async function expectNoHorizontalOverflow(page: Page) {
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth))
    .toBeLessThanOrEqual(0);
}

async function expectGate(page: Page, title: string, href = "/settings/database") {
  const card = gateCard(page);
  await expect(card.getByRole("heading", { level: 1, name: title })).toBeVisible();
  // 未設定・接続できないときはデータベース設定へ（ADB が停止中などは ADB 管理のカード）。
  const link = card.getByRole("link", { name: "データベース設定を開く" });
  await expect(link).toHaveAttribute("href", href);
  await expect(card.getByRole("link", { name: "システムテーブルを開く" })).toHaveCount(0);
  await expect(card.getByRole("button", { name: "再試行" })).toBeVisible();
  await expect(card.getByText(SETTINGS_HINT, { exact: true })).toBeVisible();
  // 本文だけを塞ぎ、サイドナビは残す（設定の画面へ移れる）。
  await expect(page.locator('a[href="/settings/database"]:not([data-testid])')).toBeAttached();
  await expectNoHorizontalOverflow(page);
  return link;
}

function businessRequests(mockApi: MockApi) {
  return mockApi.requests.filter(
    (request) => request.path.startsWith("/api/runs") || request.path.startsWith("/api/agents")
  );
}

for (const viewport of VIEWPORTS) {
  test.describe(`DB ゲート（${viewport.name}）`, () => {
    test.beforeEach(async ({ page }) => {
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
    });

    test("DB 未設定なら Agent の案内・診断コード・再試行を出し、業務 API を呼ばない", async ({ page, mockApi }) => {
      setDatabaseStatus(mockApi, "not_configured", "wallet_not_found");

      await page.goto("/runs");

      await expectGate(page, "データベースの接続情報が未設定です");
      await expect(
        gateCard(page).getByText(/^Agent の機能（業務 Agent・実行・承認・監査など）を使うには/)
      ).toBeVisible();
      await expect(page.getByText("診断コード: wallet_not_found", { exact: true })).toBeVisible();
      await expect(
        page.getByText(
          "Wallet の保存先に接続用のファイルがそろっていません。Wallet をアップロードし直してください。",
          { exact: true }
        )
      ).toBeVisible();
      expect(businessRequests(mockApi)).toEqual([]);
    });

    test("DB に接続できなければ接続できない案内を出し、接続先の詳細は出さない", async ({ page, mockApi }) => {
      setDatabaseStatus(mockApi, "unreachable");

      await page.goto("/agents");

      const link = await expectGate(page, "データベースに接続できません", "/settings/database#adb-management");
      await expect(page.getByText(/ORA-12514/)).toHaveCount(0);
      // 設定を開くリンク → 再試行の順に Tab で移る。
      await link.focus();
      await page.keyboard.press("Tab");
      await expect(gateCard(page).getByRole("button", { name: "再試行" })).toBeFocused();
      expect(businessRequests(mockApi)).toEqual([]);
    });

    test("状態の確認中は経過時間付きの読み込み表示を出す", async ({ page, mockApi }) => {
      setDatabaseStatus(mockApi, "unreachable");
      let release: () => void = () => undefined;
      const released = new Promise<void>((resolve) => {
        release = resolve;
      });
      await page.route("**/api/ready/database", async (route) => {
        await released;
        await route.fallback();
      });

      await page.goto("/runs");

      const loading = page.getByTestId("database-gate-loading");
      await expect(loading).toBeVisible();
      await expect(loading).toContainText("データベースの状態を確認しています");
      await expectNoHorizontalOverflow(page);
      release();
      await expect(loading).toHaveCount(0);
      await expect(page.getByRole("heading", { level: 1, name: "データベースに接続できません" })).toBeVisible();
    });

    test("DB が使えれば本文を出し、再試行で使えるようになったら本来の画面へ戻る", async ({ page, mockApi }) => {
      setDatabaseStatus(mockApi, "unreachable");

      await page.goto("/runs");
      await expectGate(page, "データベースに接続できません", "/settings/database#adb-management");

      setDatabaseStatus(mockApi, "ok");
      await gateCard(page).getByRole("button", { name: "再試行" }).click();

      await expect(page.getByRole("heading", { level: 1, name: "実行履歴" })).toBeVisible();
      await expect(gateCard(page)).toHaveCount(0);
      await expectNoHorizontalOverflow(page);
    });

    test("システム設定の 5 画面は DB が使えなくても開け、ユーザー管理はゲートを通す", async ({ page, mockApi }) => {
      setDatabaseStatus(mockApi, "not_configured");

      for (const { path, title } of [
        { path: "/settings/oci", title: "OCI 認証設定" },
        { path: "/settings/upload-storage", title: "アップロード保存先" },
        { path: "/settings/model", title: "モデル設定" },
        { path: "/settings/database", title: "データベース設定" },
        { path: "/settings/appearance", title: "外観と証明書" },
      ]) {
        await page.goto(path);
        await expect(page.getByRole("heading", { level: 1, name: title }), path).toBeVisible();
        await expect(gateCard(page), path).toHaveCount(0);
      }
      // ゲートを通さない画面では状態 API を呼ばない。
      expect(mockApi.requests.filter((request) => request.path === "/api/ready/database")).toEqual([]);

      // ユーザー・ロール管理は共通 DB にデータを持つため、ゲートを通す（3製品共通）。
      await page.goto("/settings/security/users");
      await expectGate(page, "データベースの接続情報が未設定です");

      // 案内のリンクからデータベース設定を開ける。
      await gateCard(page).getByRole("link", { name: "データベース設定を開く" }).click();
      await expect(page).toHaveURL(/\/settings\/database$/);
      await expect(page.getByRole("heading", { level: 1, name: "データベース設定" })).toBeVisible();
    });

    for (const theme of ["light", "dark"] as const) {
      test(`ADB が停止中なら停止の案内と ADB 管理への導線を出す（${theme}）`, async ({ page, mockApi }) => {
        await useTheme(page, theme);
        setDatabaseStatus(mockApi, "unreachable", "ok", "STOPPED");

        await page.goto("/runs");

        await expectGate(page, "Autonomous Database が停止しています", "/settings/database#adb-management");
        const card = gateCard(page);
        await expect(card.getByText(/^データベース設定の「Autonomous Database 管理」で起動してください。/)).toBeVisible();
        await expect(card.getByText("Autonomous Database: 停止済み", { exact: true })).toBeVisible();
        expect(await page.evaluate(() => document.documentElement.classList.contains("dark"))).toBe(theme === "dark");
        await saveScreenshot(page, `agent-${viewport.name}-${theme}-adb-stopped-admin`);
        expect(businessRequests(mockApi)).toEqual([]);
      });

      test(`初期化が必要ならシステムテーブルへの導線だけを出す（${theme}）`, async ({ page, mockApi }) => {
        await useTheme(page, theme);
        setDatabaseStatus(mockApi, "setup_required", "ok");

        await page.goto("/runs");

        const card = gateCard(page);
        await expect(
          card.getByRole("heading", { level: 1, name: "システムテーブルの作成・更新が必要です" })
        ).toBeVisible();
        await expect(card.getByText(/^データベースには接続できています。/)).toBeVisible();
        await expect(card.getByRole("link", { name: "システムテーブルを開く" })).toHaveAttribute(
          "href",
          "/settings/system-tables"
        );
        await expect(card.getByRole("link", { name: "データベース設定を開く" })).toHaveCount(0);
        await expect(card.getByRole("button", { name: "再試行" })).toBeVisible();
        await expectNoHorizontalOverflow(page);
        await saveScreenshot(page, `agent-${viewport.name}-${theme}-setup-required-admin`);

        await card.getByRole("link", { name: "システムテーブルを開く" }).click();
        await expect(page).toHaveURL(/\/settings\/system-tables$/);
        await expect(gateCard(page)).toHaveCount(0);
      });

      test(`設定を開けない利用者には導線を出さず、管理者への連絡を案内する（${theme}）`, async ({
        page,
        mockApi,
      }) => {
        await useTheme(page, theme);
        mockApi.setCurrentUser(VIEWER);

        for (const { status, adb, title, message } of [
          {
            status: "unreachable" as const,
            adb: null,
            title: "データベースに接続できません",
            message: /システム管理者に連絡して、データベースの起動と接続の確認を依頼してください。$/,
          },
          {
            status: "unreachable" as const,
            adb: "STOPPED",
            title: "Autonomous Database が停止しています",
            message: /^システム管理者に連絡して、Autonomous Database の起動を依頼してください。/,
          },
          {
            status: "setup_required" as const,
            adb: null,
            title: "システムテーブルの作成・更新が必要です",
            message: /システム管理者に連絡して、システムテーブルの作成・更新を依頼してください。$/,
          },
        ]) {
          setDatabaseStatus(mockApi, status, "ok", adb);
          await page.goto("/runs");

          const card = gateCard(page);
          await expect(card.getByRole("heading", { level: 1, name: title })).toBeVisible();
          await expect(card.getByText(message)).toBeVisible();
          await expect(card.getByRole("link")).toHaveCount(0);
          await expect(card.getByText(SETTINGS_HINT, { exact: true })).toHaveCount(0);
          await expect(card.getByText(CONTACT_ADMIN_FOOTER, { exact: true })).toBeVisible();
          await expect(card.getByRole("button", { name: "再試行" })).toBeVisible();
          await expectNoHorizontalOverflow(page);
          await saveScreenshot(page, `agent-${viewport.name}-${theme}-${status}-${adb ?? "none"}-viewer`);
        }
        expect(businessRequests(mockApi)).toEqual([]);
      });
    }
  });
}
