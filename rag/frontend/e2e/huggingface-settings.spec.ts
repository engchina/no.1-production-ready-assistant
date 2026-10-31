import { expect, type Page, type Route, test } from "./fixtures/test";

import {
  expandSidebarSections,
  expectNoPageOverflow,
  mockDatabaseReady,
  mockLocalAuth,
  openSidebarNav,
} from "./_helpers";

/**
 * HuggingFace 設定（#287）。共通のシステム設定の画面と同じ構成（TimedLoadingState + Skeleton、
 * FormActionBar の保存、成功は Toast・失敗は操作行の FormStatus、未保存の離脱確認）を desktop / mobile(375px) で確かめる。
 */

interface HfSettings {
  endpoint: string;
  token_configured: boolean;
  config_source: "runtime";
}

const initial: HfSettings = {
  endpoint: "",
  token_configured: false,
  config_source: "runtime",
};

function envelope(data: unknown) {
  return { json: { data, error_messages: [], warning_messages: [] } };
}

interface MockState {
  current: HfSettings;
  patches: Record<string, unknown>[];
  /** PATCH を失敗させるときの応答。 */
  patchError?: { status: number; message: string };
  /** GET を保留し、読み込み中の表示を確かめる。 */
  holdGet?: Promise<void>;
}

async function mockHuggingFace(page: Page, state: MockState): Promise<void> {
  await page.route("**/api/settings/huggingface", async (route: Route) => {
    if (route.request().method() === "PATCH") {
      const payload = route.request().postDataJSON() as Record<string, unknown>;
      state.patches.push(payload);
      if (state.patchError) {
        await route.fulfill({
          status: state.patchError.status,
          json: { data: null, error_messages: [state.patchError.message], warning_messages: [] },
        });
        return;
      }
      state.current = {
        endpoint: String(payload.endpoint ?? ""),
        token_configured: payload.clear_token
          ? false
          : payload.token
            ? true
            : state.current.token_configured,
        config_source: "runtime",
      };
      await route.fulfill(envelope(state.current));
      return;
    }
    if (state.holdGet) await state.holdGet;
    await route.fulfill(envelope(state.current));
  });
}

function actionBar(page: Page) {
  return page.getByRole("group", { name: "HuggingFace 設定の操作" });
}

function saveButton(page: Page) {
  return actionBar(page).getByRole("button", { name: "保存", exact: true });
}

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
  await mockDatabaseReady(page);
});

test("読み込み中は経過時間付きの読み込み表示と Skeleton を出し、取得後にフォームへ切り替える", async ({ page }) => {
  let release: () => void = () => undefined;
  const state: MockState = {
    current: { ...initial },
    patches: [],
    holdGet: new Promise<void>((resolve) => {
      release = resolve;
    }),
  };
  await mockHuggingFace(page, state);

  await page.goto("/settings/huggingface");

  const loading = page.getByTestId("settings-huggingface-loading");
  await expect(loading).toBeVisible();
  await expect(loading).toHaveAttribute("aria-busy", "true");
  await expect(loading).toContainText("HuggingFace 設定を読み込んでいます");
  await expect(loading.getByRole("timer")).toBeVisible();
  // ページタイトルは読み込み中も出したままにする。
  await expect(page.getByRole("heading", { level: 1, name: "HuggingFace 設定" })).toBeVisible();
  await expect(page.locator("#hf-endpoint")).toHaveCount(0);
  await expectNoPageOverflow(page);

  release();
  await expect(page.locator("#hf-endpoint")).toBeVisible();
  await expect(loading).toHaveCount(0);
});

test("取得に失敗したら ErrorState と再試行を出し、再試行でフォームを表示する", async ({ page }) => {
  let failing = true;
  await page.route("**/api/settings/huggingface", async (route) => {
    if (failing) {
      await route.fulfill({
        status: 503,
        json: { data: null, error_messages: ["HuggingFace 設定を読めませんでした。"], warning_messages: [] },
      });
      return;
    }
    await route.fulfill(envelope(initial));
  });

  await page.goto("/settings/huggingface");

  // TanStack Query の既定の再試行（3 回）を終えてから ErrorState になる。
  await expect(page.getByRole("alert")).toContainText("HuggingFace 設定を読めませんでした。", {
    timeout: 15_000,
  });
  failing = false;
  await page.getByRole("button", { name: "再試行" }).click();
  await expect(page.locator("#hf-endpoint")).toBeVisible();
  await expectNoPageOverflow(page);
});

test("保存に成功すると Toast で知らせ、送った値と token の状態を反映する", async ({ page }) => {
  const state: MockState = { current: { ...initial }, patches: [] };
  await mockHuggingFace(page, state);

  await page.goto("/settings/huggingface");
  await expect(page.getByText("未設定", { exact: true })).toBeVisible();
  await expect(page.getByText("~/.cache", { exact: false })).toBeVisible();

  await page.locator("#hf-endpoint").fill("https://hf-mirror.com");
  await page.locator("#hf-token").fill("hf_secret_token");
  await saveButton(page).click();

  await expect(page.getByText("HuggingFace 設定を保存しました")).toBeVisible();
  expect(state.patches).toEqual([{ endpoint: "https://hf-mirror.com", token: "hf_secret_token" }]);
  // token は画面に残さず、保存済みの表示に切り替わる。
  await expect(page.locator("#hf-token")).toHaveValue("");
  await expect(page.getByText("保存済み", { exact: true })).toBeVisible();
  await expect(page.locator("#hf-endpoint")).toHaveValue("https://hf-mirror.com");
  await expect(page.getByRole("checkbox", { name: "保存済み token を削除する" })).toBeVisible();
  await expectNoPageOverflow(page);
});

test("保存に失敗すると操作行に原因を出し、入力を残す。入力を直すと失敗の表示を消す", async ({ page }) => {
  const state: MockState = {
    current: { ...initial },
    patches: [],
    patchError: { status: 500, message: "backend/.env に書き込めませんでした。" },
  };
  await mockHuggingFace(page, state);

  await page.goto("/settings/huggingface");
  await page.locator("#hf-endpoint").fill("https://hf-mirror.com");
  await saveButton(page).click();

  await expect(actionBar(page)).toContainText("backend/.env に書き込めませんでした。");
  await expect(page.locator("#hf-endpoint")).toHaveValue("https://hf-mirror.com");
  await expect(saveButton(page)).toBeEnabled();
  await expectNoPageOverflow(page);

  await page.locator("#hf-endpoint").fill("https://hf-mirror.example");
  await expect(actionBar(page)).not.toContainText("backend/.env に書き込めませんでした。");
});

test("未保存の変更があると離脱を確認し、キャンセルで入力を残す", async ({ page }) => {
  await mockHuggingFace(page, { current: { ...initial }, patches: [] });

  await page.goto("/settings/huggingface");
  await page.locator("#hf-token").fill("hf_unsaved");
  const sidebar = await openSidebarNav(page);
  // OCI 認証設定は既定で閉じている「システム設定」にある（#1563）。
  await expandSidebarSections(sidebar, "システム設定");
  await sidebar.getByRole("link", { name: "OCI 認証設定", exact: true }).click();

  const dialog = page.getByRole("alertdialog", { name: "変更を破棄しますか" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(page).toHaveURL(/\/settings\/huggingface$/);
  await expect(page.locator("#hf-token")).toHaveValue("hf_unsaved");
});

test("token はマスク入力で、表示の切り替えと保存済み token の削除を送れる", async ({ page }) => {
  const state: MockState = { current: { ...initial, token_configured: true }, patches: [] };
  await mockHuggingFace(page, state);

  await page.goto("/settings/huggingface");

  const token = page.locator("#hf-token");
  await expect(token).toHaveAttribute("type", "password");
  await token.fill("hf_visible_check");
  await page.getByRole("button", { name: "token を表示" }).click();
  await expect(token).toHaveAttribute("type", "text");
  await page.getByRole("button", { name: "token を隠す" }).click();
  await expect(token).toHaveAttribute("type", "password");

  await page.getByRole("checkbox", { name: "保存済み token を削除する" }).check();
  await expect(token).toBeDisabled();
  await expect(token).toHaveValue("");
  await saveButton(page).click();

  await expect(page.getByText("HuggingFace 設定を保存しました")).toBeVisible();
  expect(state.patches).toEqual([{ endpoint: "", clear_token: true }]);
  await expect(page.getByText("未設定", { exact: true })).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "保存済み token を削除する" })).toHaveCount(0);
  await expectNoPageOverflow(page);
});
