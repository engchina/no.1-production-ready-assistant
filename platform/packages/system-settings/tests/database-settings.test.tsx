import { ConfirmProvider } from "@production-ready/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import {
  DATABASE_SETTINGS_QUERY_KEY,
  DatabaseSettingsPage,
  type DatabaseSettingsApi,
  type DatabaseSettingsData,
} from "../src";

const pending = () => new Promise<never>(() => undefined);
const api: DatabaseSettingsApi = {
  getDatabaseSettings: pending,
  updateDatabaseSettings: pending,
  uploadDatabaseWallet: pending,
  downloadDatabaseWallet: pending,
  testDatabaseSettings: pending,
  getAdbInfo: pending,
  updateAdbSettings: pending,
  startAdb: pending,
  stopAdb: pending,
};

function render(node: React.ReactNode, client = new QueryClient()) {
  return renderToStaticMarkup(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ConfirmProvider>{node}</ConfirmProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

describe("DatabaseSettingsPage", () => {
  it("読み込み中は状態表示を出す", () => {
    const html = render(<DatabaseSettingsPage api={api} />);
    expect(html).toContain('data-testid="settings-database-loading"');
    expect(html).toContain("データベース設定を読み込んでいます。");
    // 既定でも経過時間付きの読み込み表示（TimedLoadingState）と Skeleton にする（RAG / Agent は既定を使う）。
    expect(html).toContain('data-testid="settings-database-loading-processing"');
    expect(html).toContain("経過時間");
  });

  it("製品の読み込み中表示に差し替えられる", () => {
    const html = render(
      <DatabaseSettingsPage api={api} loadingFallback={<p data-testid="product-loading" />} />,
    );
    expect(html).toContain('data-testid="product-loading"');
    expect(html).not.toContain('data-testid="settings-database-loading"');
  });

  it("接続カードは共有 Card の既定の見出しで、secret は SecretField、保存の操作行は FormActionBar にする（#296）", () => {
    const settings: DatabaseSettingsData = {
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
      adb_ocid: "",
      region: "ap-osaka-1",
      config_source: "runtime",
    };
    const client = new QueryClient();
    client.setQueryData(DATABASE_SETTINGS_QUERY_KEY, settings);
    const html = render(<DatabaseSettingsPage api={api} />, client);

    // 主カードは独自の余白・区切り線・大きい見出しを持たない。
    expect(html).not.toMatch(/[\s"]p-6[\s"]/);
    expect(html).not.toContain("border-b border-border pb-5");
    expect(html).toContain("アプリが Oracle AI Database へ接続するユーザー・パスワード・Wallet・サービス名を設定します。");
    // DB パスワードは保存済み、Wallet パスワードは未設定のバッジ。削除の指定は保存済みの値だけに出す。
    expect(html).toMatch(/<input[^>]*id="oracle-password"[^>]*type="password"|<input[^>]*type="password"[^>]*id="oracle-password"/);
    expect(html).toMatch(/data-status-variant="success"[^>]*>.*?保存済み<\/span>/);
    expect(html).toMatch(/data-status-variant="neutral"[^>]*>.*?未設定<\/span>/);
    expect(html).toContain("保存済みパスワードを削除する");
    expect(html).not.toContain("保存済み Wallet パスワードを削除する");
    // 操作行は FormActionBar。保存は form の submit のまま。
    expect(html).toContain('aria-label="データベース設定の操作"');
    expect(html).toContain('aria-label="Autonomous Database の操作"');
    const save = (html.match(/<button\b[^>]*data-form-action-id="save"[^>]*>/g) ?? []).find((tag) =>
      tag.includes('type="submit"'),
    );
    expect(save).toBeDefined();
    expect(html).not.toContain("保存中");
    // サポートするリージョンの保存値では案内を出さない（#660）。
    expect(html).not.toContain("はサポートしていません");
    // Wallet の状態は色の文字ではなく StatusBadge（アイコン付き）で出す（#722）。
    expect(html).toMatch(/Wallet状態:<\/span><span data-status-variant="success"[^>]*><span class="pr-icon-line"><svg[\s\S]*?<\/svg><\/span>設定済み<\/span>/);
  });

  it("ADB のリージョンの保存値が候補に無い us-chicago-1 のときは、値をそのまま出して選び直しを案内する（#660）", () => {
    const settings: DatabaseSettingsData = {
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
      adb_ocid: "ocid1.autonomousdatabase.oc1.us-chicago-1.example",
      region: "us-chicago-1",
      config_source: "runtime",
    };
    const client = new QueryClient();
    client.setQueryData(DATABASE_SETTINGS_QUERY_KEY, settings);
    const html = render(<DatabaseSettingsPage api={api} />, client);

    expect(html).toMatch(/<button\b[^>]*id="adb-region"[^>]*>[\s\S]*?us-chicago-1/);
    expect(html).toContain(
      "保存済みのリージョン us-chicago-1 はサポートしていません。ap-tokyo-1 または ap-osaka-1 を選んで保存してください。",
    );
    // Wallet が未設定のときは warning の StatusBadge（#722）。
    const missingWallet = new QueryClient();
    missingWallet.setQueryData(DATABASE_SETTINGS_QUERY_KEY, { ...settings, wallet_uploaded: false });
    expect(render(<DatabaseSettingsPage api={api} />, missingWallet)).toMatch(
      /Wallet状態:<\/span><span data-status-variant="warning"[^>]*>[\s\S]*?未設定<\/span>/,
    );
  });
  it("欄は FormGrid に置き、値の種類の段の幅にする。OCID は全幅、placeholder は欄に収まる短い例（#1891）", () => {
    const settings: DatabaseSettingsData = {
      user: "app",
      dsn: "",
      driver_mode: "thin",
      connection_security: "wallet_mtls",
      client_lib_dir: "",
      wallet_dir: "",
      wallet_uploaded: false,
      available_services: [],
      has_password: false,
      has_wallet_password: false,
      readiness: "ok",
      embedding_dimension: 1536,
      vector_column: "EMBEDDING",
      adb_ocid: "",
      region: "ap-osaka-1",
      config_source: "runtime",
    };
    const client = new QueryClient();
    client.setQueryData(DATABASE_SETTINGS_QUERY_KEY, settings);
    const html = render(<DatabaseSettingsPage api={api} />, client);

    // 接続カードと ADB カードの欄の並びはどちらも FormGrid。
    expect(html.match(/data-form-grid="2"/g)?.length).toBeGreaterThanOrEqual(2);
    // DB ユーザーは名前の段（md）。
    expect(html).toMatch(/<div class="[^"]*field-width-md[^"]*"><input[^>]*id="oracle-user"/);
    // Wallet のサービス名（候補が無いときの入力欄）は名前の段（md）。
    expect(html).toMatch(/<div class="[^"]*field-width-md[^"]*"><input[^>]*id="oracle-wallet-service"/);
    // ADB OCID は長い値なので全幅（2 列にまたがる）。
    expect(html).toMatch(/<div class="[^"]*col-span-full[^"]*"[^>]*><label[^>]*for="adb-ocid"/);
    expect(html).toContain('placeholder="未設定（platform/.env で設定）"');
  });
});
