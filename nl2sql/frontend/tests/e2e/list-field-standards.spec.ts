import { expect, type Page, test } from "./_helpers/test";

import { APP_ROUTES } from "../../src/lib/routes";
import { mockDatabaseGateReady } from "./_helpers/database-gate";
import {
  findClippedPlaceholders,
  findFieldsOffWidthScale,
  findToolbarFieldWidthMismatch,
  findToolbarMisalignment,
  measureHorizontalOverflow,
} from "../../../../platform/packages/ui/e2e/layout-checks";

/**
 * 全画面の一覧・入力欄の共通の検査（#1891 段 3）。API は未モック（404）で、データに依存しない欄（検索・絞り込み・
 * 条件のフォーム）と読み込みの失敗の表示を、1280 / 1920px（desktop）・375px（mobile-375）× ライト / ダークで回す。
 * データのある一覧（ページ送り・件数・多い行）は各画面の spec が確かめる。
 *
 * - 横あふれが無い（`measureHorizontalOverflow`）
 * - placeholder が欄に収まる（`findClippedPlaceholders`）
 * - 欄の幅が段（xs / sm / md / lg）か置き場所の幅いっぱい（`full`）（`findFieldsOffWidthScale`。640px 以上）
 * - 一覧のツールバーの同じ行の部品の高さ・縦の中心がそろう（`findToolbarMisalignment`）、縦に積んだツールバーの欄が
 *   全幅にそろう（`findToolbarFieldWidthMismatch`。#1933）
 */

// ログイン・パスワード変更・権限なし（AppShell 外）と、別ルートへ転送するだけの旧 URL は対象外（page-width.spec と同じ）。
const NON_PAGE_ROUTES = new Set<string>([
  APP_ROUTES.login,
  APP_ROUTES.passwordChange,
  APP_ROUTES.forbidden,
  APP_ROUTES.home,
  APP_ROUTES.learning,
  APP_ROUTES.questionLearning,
  APP_ROUTES.legacyNl2sqlModelLearning,
]);
// 3 製品共通の画面（platform の system-settings。OCI 認証・アップロード保存先・モデル・データベース・外観・ユーザー・
// ロール・API キー）の欄の幅は #1891 の段 4 で platform の検査が受け持つ。ここでは横あふれと placeholder（NL2SQL が
// i18n で上書きする文言がある）だけを見る。
const PLATFORM_ROUTES = new Set<string>([
  APP_ROUTES.settingsOci,
  APP_ROUTES.settingsUploadStorage,
  APP_ROUTES.settingsModel,
  APP_ROUTES.settingsDatabase,
  APP_ROUTES.settingsAppearance,
  APP_ROUTES.securityUsers,
  APP_ROUTES.securityRoles,
  APP_ROUTES.account,
  APP_ROUTES.securityApiKeys,
]);
const PAGE_ROUTES = [...new Set(Object.values(APP_ROUTES))].filter((route) => !NON_PAGE_ROUTES.has(route));
// チャットの質問の入力欄は、送信のボタンと 1 行に並べる共通の入力欄（ChatComposer。page-archetypes.md §6）で、
// 幅は会話の列から送信のボタンを引いた残り。段の幅の検査の対象外にする。
const WIDTH_SCALE_EXEMPT = new Set<string>([`${APP_ROUTES.chat} 質問`]);

async function openPage(page: Page, route: string) {
  await page.goto(route);
  await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
  // 未モックの API の失敗の表示・遅延して出る欄（タブの中身など）が落ち着くまで待つ。
  await page.waitForLoadState("networkidle");
}

async function scanAllPages(page: Page, { width, checkWidthScale }: { width: number; checkWidthScale: boolean }) {
  const problems: string[] = [];
  for (const route of PAGE_ROUTES) {
    await openPage(page, route);
    const overflow = await measureHorizontalOverflow(page);
    if (overflow > 1) problems.push(`${route}: 横あふれ ${overflow}px`);
    for (const field of await findClippedPlaceholders(page)) {
      problems.push(`${route}: placeholder が切れる ${JSON.stringify(field)}`);
    }
    for (const row of await findToolbarMisalignment(page)) {
      problems.push(`${route}: ツールバーの部品がそろわない ${JSON.stringify(row)}`);
    }
    for (const field of await findToolbarFieldWidthMismatch(page)) {
      problems.push(`${route}: 縦に積んだツールバーの欄が全幅でない ${JSON.stringify(field)}`);
    }
    if (checkWidthScale && !PLATFORM_ROUTES.has(route)) {
      for (const field of await findFieldsOffWidthScale(page)) {
        if (WIDTH_SCALE_EXEMPT.has(`${route} ${field.name}`)) continue;
        problems.push(`${route}: 幅が段の外 ${JSON.stringify(field)}`);
      }
    }
  }
  expect(problems, `${width}px`).toEqual([]);
}

test.describe("一覧・入力欄の共通の検査（#1891）", { tag: "@responsive" }, () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/**", (route) =>
      route.fulfill({
        status: 404,
        contentType: "application/json",
        body: JSON.stringify({ data: null, error_messages: ["e2e unmocked"], warning_messages: [] }),
      })
    );
    await mockDatabaseGateReady(page);
  });

  for (const theme of ["light", "dark"] as const) {
    for (const width of [1280, 1920]) {
      test(`${width}px・${theme}: 全画面で横あふれ・placeholder の切れ・段の外の幅が無い`, async ({ page, isMobile }) => {
        test.skip(isMobile, "desktop の幅");
        test.setTimeout(240_000);
        await page.emulateMedia({ colorScheme: theme });
        await page.addInitScript((value) => {
          localStorage.setItem("production-ready-nl2sql.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
        }, theme);
        await page.setViewportSize({ width, height: 1000 });
        await scanAllPages(page, { width, checkWidthScale: true });
      });
    }

    test(`375px・${theme}: 全画面で横あふれ・placeholder の切れが無い`, async ({ page, isMobile }) => {
      test.skip(!isMobile, "モバイル幅");
      test.setTimeout(240_000);
      await page.emulateMedia({ colorScheme: theme });
      await page.addInitScript((value) => {
        localStorage.setItem("production-ready-nl2sql.ui", JSON.stringify({ state: { theme: value }, version: 0 }));
      }, theme);
      await scanAllPages(page, { width: 375, checkWidthScale: false });
    });
  }
});
