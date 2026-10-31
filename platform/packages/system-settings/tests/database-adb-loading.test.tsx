// @vitest-environment happy-dom
import { ConfirmProvider } from "@production-ready/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  ADB_INFO_QUERY_KEY,
  DATABASE_SETTINGS_QUERY_KEY,
  DatabaseSettingsPage,
  type AdbInfoData,
  type DatabaseSettingsApi,
  type DatabaseSettingsData,
} from "../src";

// #819: スピナー（loading）は押したボタンだけが出す。「情報を再取得」と「保存」は同じ保存の
// mutation と Wallet の取得を使うが、押していない側は disabled にするだけで回さない。

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  host = document.createElement("div");
  document.body.append(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

const SETTINGS: DatabaseSettingsData = {
  user: "app",
  dsn: "db_high",
  driver_mode: "thin",
  connection_security: "wallet_mtls",
  client_lib_dir: "",
  wallet_dir: "/wallet",
  wallet_uploaded: true,
  available_services: ["db_high"],
  has_password: true,
  has_wallet_password: false,
  readiness: "ok",
  embedding_dimension: 1536,
  vector_column: "EMBEDDING",
  adb_ocid: "ocid1.autonomousdatabase.oc1.ap-osaka-1.example",
  region: "ap-osaka-1",
  config_source: "runtime",
};

const AVAILABLE: AdbInfoData = {
  status: "success",
  message: "Autonomous Database の情報を取得しました。",
  lifecycle_state: "AVAILABLE",
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

const never = () => new Promise<never>(() => undefined);

function apiWith(updateAdbSettings: DatabaseSettingsApi["updateAdbSettings"]): DatabaseSettingsApi {
  return {
    getDatabaseSettings: () => Promise.resolve(SETTINGS),
    updateDatabaseSettings: never,
    uploadDatabaseWallet: never,
    // 「情報を再取得」の後半（Wallet の取得）を待ったままにする。
    downloadDatabaseWallet: never,
    testDatabaseSettings: never,
    getAdbInfo: () => Promise.resolve(AVAILABLE),
    updateAdbSettings,
    startAdb: never,
    stopAdb: never,
  };
}

async function renderPage(api: DatabaseSettingsApi) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(DATABASE_SETTINGS_QUERY_KEY, SETTINGS);
  client.setQueryData(ADB_INFO_QUERY_KEY, AVAILABLE);
  await act(async () => {
    root.render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <ConfirmProvider>
            <DatabaseSettingsPage api={api} />
          </ConfirmProvider>
        </QueryClientProvider>
      </MemoryRouter>,
    );
  });
}

function adbButtons() {
  const bar = host.querySelector('[aria-label="Autonomous Database の操作"]');
  if (!bar) throw new Error("ADB の操作行が無い");
  const button = (id: string) => {
    const element = bar.querySelector<HTMLButtonElement>(`button[data-form-action-id="${id}"]`);
    if (!element) throw new Error(`ADB の操作 ${id} が無い`);
    return element;
  };
  const refresh = Array.from(host.querySelectorAll<HTMLButtonElement>("button")).find(
    (element) => element.textContent?.trim() === "情報を再取得",
  );
  if (!refresh) throw new Error("「情報を再取得」が無い");
  return { refresh, save: button("save"), start: button("start"), stop: button("stop") };
}

/** 共通 Button の loading は aria-busy="true" とスピナーで出る。 */
function isLoading(button: HTMLButtonElement) {
  return button.getAttribute("aria-busy") === "true";
}

/** disabled（ネイティブ・fieldset による無効化）か aria-disabled。 */
function isUnavailable(button: HTMLButtonElement) {
  return (
    button.disabled ||
    button.matches(":disabled") ||
    button.closest("fieldset:disabled") !== null ||
    button.getAttribute("aria-disabled") === "true"
  );
}

describe("Autonomous Database 管理の操作のスピナー（#819）", () => {
  it("「情報を再取得」の処理中は「情報を再取得」だけが回り、「保存」「起動」「停止」は無効にするだけ", async () => {
    const update = deferred<AdbInfoData>();
    await renderPage(apiWith(() => update.promise));

    const before = adbButtons();
    expect(isLoading(before.refresh)).toBe(false);
    expect(isLoading(before.save)).toBe(false);

    await act(async () => {
      before.refresh.click();
    });

    // 前半: ADB の設定の保存（同じ mutation を「保存」も使う）。
    let buttons = adbButtons();
    expect(isLoading(buttons.refresh)).toBe(true);
    expect(isLoading(buttons.save)).toBe(false);
    expect(isUnavailable(buttons.save)).toBe(true);
    expect(isLoading(buttons.start)).toBe(false);
    expect(isUnavailable(buttons.start)).toBe(true);
    expect(isLoading(buttons.stop)).toBe(false);
    expect(isUnavailable(buttons.stop)).toBe(true);
    expect(host.textContent).toContain("Autonomous Database の情報と Wallet を取得しています");
    // 動くスピナーは押したボタンの 1 つだけ（処理中の表示は activityIcon="none"）。
    expect(host.querySelectorAll('button[aria-busy="true"]')).toHaveLength(1);

    // 後半: Wallet の取得（DB 接続のカードの操作も無効になる。押したボタンはフォーカスを保つ。#835）。
    await act(async () => {
      update.resolve(AVAILABLE);
    });
    buttons = adbButtons();
    expect(isLoading(buttons.refresh)).toBe(true);
    expect(isLoading(buttons.save)).toBe(false);
    expect(isUnavailable(buttons.save)).toBe(true);
    expect(host.querySelectorAll('button[aria-busy="true"]')).toHaveLength(1);
  });

  it("「保存」の処理中は「保存」だけが回り、「情報を再取得」は無効にするだけ", async () => {
    const update = deferred<AdbInfoData>();
    await renderPage(apiWith(() => update.promise));

    await act(async () => {
      adbButtons().save.click();
    });

    const buttons = adbButtons();
    expect(isLoading(buttons.save)).toBe(true);
    expect(isLoading(buttons.refresh)).toBe(false);
    expect(isUnavailable(buttons.refresh)).toBe(true);
    expect(host.querySelectorAll('button[aria-busy="true"]')).toHaveLength(1);
  });
});

describe("Autonomous Database の情報の読み込み中", () => {
  it("ADB の情報を取得している間は経過時間付きで示し、取得後は消す", async () => {
    const info = deferred<AdbInfoData>();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(DATABASE_SETTINGS_QUERY_KEY, SETTINGS);
    await act(async () => {
      root.render(
        <MemoryRouter>
          <QueryClientProvider client={client}>
            <ConfirmProvider>
              <DatabaseSettingsPage api={{ ...apiWith(never), getAdbInfo: () => info.promise }} />
            </ConfirmProvider>
          </QueryClientProvider>
        </MemoryRouter>,
      );
    });

    const loading = host.querySelector('[data-testid="settings-adb-loading"]');
    expect(loading?.textContent).toContain("Autonomous Database の情報を読み込んでいます");
    expect(loading?.textContent).toContain("経過時間");

    await act(async () => {
      info.resolve(AVAILABLE);
    });
    // TanStack Query は結果の通知を setTimeout(0) で次の tick に送る。その timer は queryFn の
    // Promise の解決から数 microtask 後に登録されるため、こちらの setTimeout(0) を 1 回待つだけ
    // では先に回って通知の前に検査することがある（負荷のある CI で失敗した）。消えるまで tick を
    // 進める（上限付き）。
    for (let i = 0; i < 20; i += 1) {
      if (!host.querySelector('[data-testid="settings-adb-loading"]')) break;
      await act(async () => {
        await new Promise((done) => setTimeout(done, 0));
      });
    }
    expect(host.querySelector('[data-testid="settings-adb-loading"]')).toBeNull();
    // 取得した状態（見出しの StatusBadge）へ置き換わる。
    expect(host.textContent).toContain("起動済み");
  });
});
