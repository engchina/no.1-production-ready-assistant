import { expect, test, type Page } from "./fixtures/test";
import {
  SYSTEM_TABLES_STATUS_OK,
  expectNoPageOverflow,
  mockAuthUser,
  mockLocalAuth,
} from "./_helpers";
import { expectSpinnerStable } from "./_spinner-stability";

/**
 * DB ゲート（3製品共通の部品。#325）: システム設定の 5 画面以外は、DB 接続不可/未設定のとき
 * エラー画面ではなく落ち着いた案内を表示し、状態ごとの導線（未設定・接続できない → データベース設定、
 * 初期化が必要 → システムテーブル）と再試行を出す。設定を開けない利用者には導線を出さず、
 * システム管理者への連絡を案内する（#820）。
 */

type DbStatus = "ok" | "not_configured" | "unreachable" | "setup_required";

function dbStatus(status: DbStatus, check?: string) {
  return {
    data: {
      status,
      check: check ?? (status === "not_configured" ? "missing" : "ok"),
      detail: status === "unreachable" ? "Oracle connection probe failed (ORA-12514)." : null,
    },
    error_messages: [],
    warning_messages: [],
  };
}

const SETTINGS_HINT =
  "OCI 認証・アップロード保存先・モデル・データベース・外観と証明書の各設定ページは引き続き利用できます。";

async function routeAuth(page: Page) {
  await mockLocalAuth(page);
}

async function expectGate(
  page: Page,
  {
    title,
    actionName = "データベース設定を開く",
    actionHref = "/settings/database",
    settingsHint = SETTINGS_HINT,
  }: { title: string; actionName?: string; actionHref?: string; settingsHint?: string }
) {
  const card = page.locator('section[aria-labelledby="database-unavailable-title"]');
  await expect(card.getByRole("heading", { level: 1, name: title })).toBeVisible();
  const link = card.getByRole("link", { name: actionName });
  await expect(link).toHaveAttribute("href", actionHref);
  // 全状態で再試行を出す（NL2SQL と同じ）。
  await expect(card.getByRole("button", { name: "再試行" })).toBeVisible();
  await expect(card.getByText(settingsHint, { exact: true })).toBeVisible();
  // 接続先に関わる ORA コードは出さない（#320）。
  await expect(page.getByText(/ORA-12514/)).toHaveCount(0);
  await expectNoPageOverflow(page);
  return link;
}

test("DB 接続済みでも schema 未作成ならシステムテーブルへ案内する", async ({ page }) => {
  await routeAuth(page);
  await page.route("**/api/ready/database", (route) =>
    route.fulfill({ json: dbStatus("setup_required") })
  );

  await page.goto("/file-list");

  const link = await expectGate(page, {
    title: "システムテーブルの作成・更新が必要です",
    actionName: "システムテーブルを開く",
    actionHref: "/settings/system-tables",
    settingsHint:
      "OCI 認証・アップロード保存先・モデル・データベース・システムテーブル・外観と証明書の各設定ページは引き続き利用できます。",
  });

  // システムテーブルの画面（運用設定。#658）は未初期化でもゲートに塞がれずに開ける。
  await page.route("**/api/settings/database/system-tables", (route) =>
    route.fulfill({ json: SYSTEM_TABLES_STATUS_OK })
  );
  await link.click();
  await expect(page).toHaveURL(/\/settings\/system-tables$/);
  await expect(page.getByRole("heading", { name: "システムテーブル管理" })).toBeVisible();
  await expect(page.locator('section[aria-labelledby="database-unavailable-title"]')).toHaveCount(0);
});

test("DB 接続不可時、機能ページはエラーではなく接続設定と Wallet の確認の案内を表示する", async ({ page }) => {
  await routeAuth(page);
  await page.route("**/api/ready/database", (route) =>
    route.fulfill({ json: dbStatus("unreachable") })
  );
  // 画面本体の API は叩かれない想定だが、保険で 500 を返しておく
  await page.route("**/api/documents**", (route) =>
    route.fulfill({ status: 500, json: { data: null, error_messages: ["boom"], warning_messages: [] } })
  );

  await page.goto("/file-list");

  const link = await expectGate(page, { title: "データベースに接続できません" });
  // 全画面エラー(サーバー内部エラー)ではないこと
  await expect(page.getByText("サーバー内部でエラーが発生しました")).toHaveCount(0);
  // 設定を開くリンク → 再試行の順に Tab で移る。
  await link.focus();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "再試行" })).toBeFocused();
});

test("DB 未設定時も再試行を出し、Wallet の不備は診断コードと補足で示す", async ({ page }) => {
  await routeAuth(page);
  await page.route("**/api/ready/database", (route) =>
    route.fulfill({ json: dbStatus("not_configured", "wallet_password_invalid") })
  );

  await page.goto("/file-list");

  await expectGate(page, { title: "データベースの接続情報が未設定です", actionHref: "/settings/database" });
  await expect(page.getByText(/RAG 機能\(取込・検索・索引\)を使うには/)).toBeVisible();
  await expect(page.getByText("診断コード: wallet_password_invalid", { exact: true })).toBeVisible();
  await expect(
    page.getByText(
      "暗号化された Wallet を現在の Wallet パスワードで復号できません。Wallet パスワードを確認してください。",
      { exact: true }
    )
  ).toBeVisible();
});

test("再試行で DB が使えるようになったら本来のページを表示する", async ({ page }) => {
  await routeAuth(page);
  let available = false;
  await page.route("**/api/ready/database", (route) =>
    route.fulfill({ json: dbStatus(available ? "ok" : "unreachable") })
  );
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill({
      json: {
        data: { items: [], total: 0, limit: 50, offset: 0, has_next: false },
        error_messages: [],
        warning_messages: [],
      },
    })
  );

  await page.goto("/knowledge-bases");
  await expectGate(page, { title: "データベースに接続できません" });

  available = true;
  await page.getByRole("button", { name: "再試行" }).click();
  await expect(page.getByRole("heading", { name: "ナレッジベース", exact: true })).toBeVisible();
  await expect(page.locator('section[aria-labelledby="database-unavailable-title"]')).toHaveCount(0);
});

test("状態の確認中は経過時間付きの読み込み表示を出す", async ({ page }) => {
  await routeAuth(page);
  let release: () => void = () => undefined;
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/ready/database", async (route) => {
    await released;
    await route.fulfill({ json: dbStatus("unreachable") });
  });

  await page.goto("/file-list");

  const loading = page.getByTestId("database-gate-loading");
  await expect(loading).toBeVisible();
  await expect(loading).toContainText("データベースの状態を確認しています");
  // ほかの読み込み中と同じく、本文の先頭に本文と同じ幅で出す（画面の中央に浮かせない。#2510）。
  const main = await page.locator("main").first().boundingBox();
  const box = await loading.boundingBox();
  expect(box!.y - main!.y).toBeLessThanOrEqual(48);
  expect(box!.width).toBeGreaterThanOrEqual(main!.width - 80);
  release();
  await expect(loading).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "データベースに接続できません" })).toBeVisible();
});

test("広い画面でも、確認中の表示は画面と同じ全幅で出す（読み込み後に広がらない。#3311）", async ({ page }) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await routeAuth(page);
  let release: () => void = () => undefined;
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/api/ready/database", async (route) => {
    await released;
    await route.fulfill({ json: dbStatus("unreachable") });
  });

  await page.goto("/file-list");

  const loading = page.getByTestId("database-gate-loading");
  await expect(loading).toBeVisible();
  // 本文の最大幅（1440px）で絞らず、main の幅から左右のガターだけを引いた幅になる。
  const main = await page.locator("main").first().boundingBox();
  const box = await loading.boundingBox();
  expect(box!.width).toBeGreaterThan(1440);
  expect(box!.width).toBeGreaterThanOrEqual(main!.width - 80);
  release();
  await expect(loading).toHaveCount(0);
});

test("確認が 10 秒を超えて遅延の案内が出ても、読み込みのカードとスピナーは動かない（#902）", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await page.clock.install();
  await routeAuth(page);
  await page.route("**/api/ready/database", () => new Promise<void>(() => undefined));

  await page.goto("/file-list");

  const loading = page.getByTestId("database-gate-loading");
  const spinner = loading.locator("svg.animate-spin").first();
  await expect(spinner).toBeVisible();
  // 回転の角度によらない中心と、カードの位置・高さ。
  const geometry = async () => {
    const box = await spinner.boundingBox();
    const card = await loading.boundingBox();
    if (!box || !card) throw new Error("読み込みの表示が見つかりません");
    return { x: box.x + box.width / 2, y: box.y + box.height / 2, top: card.y, height: card.height };
  };
  // フォント（自己ホストの Noto Sans JP）とレイアウトが確定してから測る。確定の前に測ると、後から当たった
  // フォントで行の高さが変わり、カードが動いたように見える（遅延の案内とは無関係のずれ）。
  await page.evaluate(() => document.fonts.ready.then(() => undefined));
  let before = await geometry();
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await page.waitForTimeout(100);
    const current = await geometry();
    const settled = (Object.keys(before) as Array<keyof typeof before>).every(
      (key) => Math.abs(current[key] - before[key]) < 0.5,
    );
    before = current;
    if (settled) break;
  }
  await expect(loading).not.toContainText("通常より時間がかかっています");

  await page.clock.fastForward(11_000);
  await expect(loading).toContainText("通常より時間がかかっています");
  const after = await geometry();
  // 遅延の案内の行は最初から高さを予約しているので、カードもスピナーも動かない。
  expect(Math.abs(after.x - before.x)).toBeLessThan(0.5);
  expect(Math.abs(after.y - before.y)).toBeLessThan(0.5);
  expect(Math.abs(after.top - before.top)).toBeLessThan(0.5);
  expect(Math.abs(after.height - before.height)).toBeLessThan(0.5);
  // 遅延の案内は処理の名前の直下（右の経過時間の列ではない。#2426）。
  const slow = loading.getByTestId("database-gate-loading-processing-slow");
  await expect(slow).toHaveText("通常より時間がかかっています。");
  const label = slow.locator("xpath=preceding-sibling::span[1]");
  await expect(label).toContainText("データベースの状態を確認しています");
  const labelBox = await label.boundingBox();
  const slowBox = await slow.boundingBox();
  expect(labelBox && slowBox && slowBox.y >= labelBox.y + labelBox.height - 1).toBeTruthy();
  expect(labelBox && slowBox && Math.abs(slowBox.x - labelBox.x) <= 1).toBeTruthy();
  await loading.screenshot({ path: testInfo.outputPath("db-gate-slow.png") });
});

test(`確認中のスピナーは回転しても見た目の重心が上下・左右に動かず、カードと行も動かない（#1180）`, { tag: "@responsive" }, async ({ page }, testInfo) => {
  await routeAuth(page);
  await page.route("**/api/ready/database", () => new Promise<void>(() => undefined));

  await page.goto("/file-list");

  const loading = page.getByTestId("database-gate-loading");
  await expect(loading).toContainText("データベースの状態を確認しています");
  await page.evaluate(() => document.fonts.ready.then(() => undefined));
  const card = await loading.boundingBox();
  // 共有の Spinner（固定の正方形の箱の中で 180 度対称のアークだけが回る）。旧形（270 度の 1 本）は
  // 見た目の重心が 1 回転で上下・左右に 1.8px 動き、揺れて見えた。
  await expectSpinnerStable(loading.locator(".pr-spinner"));
  expect(await loading.boundingBox()).toEqual(card);
  await page.screenshot({ path: testInfo.outputPath("db-gate-checking.png") });
});

test("システム設定の 5 画面は DB が無くてもゲートを通さずに開ける", async ({ page }) => {
  await routeAuth(page);
  await page.route("**/api/ready/database", (route) =>
    route.fulfill({ json: dbStatus("unreachable") })
  );

  await page.goto("/settings/database");

  // ゲートに塞がれず、データベース設定ページ自体が表示される
  await expect(page.getByRole("heading", { name: "データベース設定" })).toBeVisible();
  await expect(page.locator('section[aria-labelledby="database-unavailable-title"]')).toHaveCount(0);
});

test("RAG 固有の設定・ユーザー管理・権限管理は DB が無いとゲートの案内を出す", async ({ page }) => {
  await routeAuth(page);
  await page.route("**/api/ready/database", (route) =>
    route.fulfill({ json: dbStatus("not_configured") })
  );

  // RAG 固有の設定（取込・検索・回答の設定）は DB に設定を持つ（#325 で NL2SQL と同じ範囲にした）。
  // ユーザーとロールも DB に持つ（#214）。
  for (const path of ["/settings/pipeline", "/settings/retrieval", "/settings/security/users", "/settings/security/permissions"]) {
    await page.goto(path);
    await expect(
      page.getByRole("heading", { name: "データベースの接続情報が未設定です" })
    ).toBeVisible();
  }
});

test("DB 利用可能時は本来のページを表示する", async ({ page }) => {
  await routeAuth(page);
  await page.route("**/api/ready/database", (route) => route.fulfill({ json: dbStatus("ok") }));
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill({
      json: {
        data: { items: [], total: 0, limit: 50, offset: 0, has_next: false },
        error_messages: [],
        warning_messages: [],
      },
    })
  );

  await page.goto("/knowledge-bases");

  await expect(page.getByRole("heading", { name: "ナレッジベース", exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "データベースに接続できません" })
  ).toHaveCount(0);
});

test("接続できないときは ADB の状態を出さず、接続設定と Wallet の確認を案内する（#10）", async ({ page }) => {
  await routeAuth(page);
  await page.route("**/api/ready/database", (route) =>
    route.fulfill({ json: dbStatus("unreachable", "ok") })
  );

  await page.goto("/file-list");

  await expectGate(page, { title: "データベースに接続できません" });
  await expect(page.getByText(/Wallet を確認してから、再試行してください。$/)).toBeVisible();
  await expect(page.getByText(/Autonomous Database/)).toHaveCount(0);
});

test("設定を開けない利用者には導線を出さず、システム管理者への連絡を案内する", async ({ page }) => {
  await mockAuthUser(page, { permissions: ["menu.file_list"] });
  let status: DbStatus = "unreachable";
  await page.route("**/api/ready/database", (route) => route.fulfill({ json: dbStatus(status) }));

  await page.goto("/file-list");

  const card = page.locator('section[aria-labelledby="database-unavailable-title"]');
  await expect(card.getByRole("heading", { level: 1, name: "データベースに接続できません" })).toBeVisible();
  await expect(card.getByText(/システム管理者に連絡して、データベースの稼働と接続設定・Wallet の確認を依頼してください。$/)).toBeVisible();
  await expect(card.getByRole("link")).toHaveCount(0);
  await expect(card.getByRole("button", { name: "再試行" })).toBeVisible();
  await expectNoPageOverflow(page);

  status = "setup_required";
  await card.getByRole("button", { name: "再試行" }).click();
  await expect(card.getByRole("heading", { level: 1, name: "システムテーブルの作成・更新が必要です" })).toBeVisible();
  await expect(card.getByText(/システム管理者に連絡して、システムテーブルの作成・更新を依頼してください。$/)).toBeVisible();
  await expect(card.getByRole("link")).toHaveCount(0);
});
