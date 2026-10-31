import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import {
  DATABASE_GATE_MESSAGES,
  DATABASE_STATUS_QUERY_KEY,
  DatabaseGate,
  DatabaseUnavailableNotice,
  SYSTEM_SETTINGS_PATHS,
  USER_ROLE_PATHS,
  databaseCheckMessageKey,
  databaseGateView,
  databaseReasonCode,
  isDatabaseGateExemptPath,
  type DatabaseSecondaryGateProps,
  type DatabaseStatusApi,
  type DatabaseStatusData,
} from "../src";

const routes = { databaseSettings: "/settings/database", systemTables: "/settings/system-tables" };
const admin = { canManageDatabase: true, canManageSystemTables: true } as const;
const api: DatabaseStatusApi = { getDatabaseStatus: () => new Promise<never>(() => undefined) };

function snapshot(status: DatabaseStatusData["status"], check = "ok", adbLifecycleState: string | null = null): DatabaseStatusData {
  return {
    status,
    check,
    detail: status === "unreachable" ? "Oracle connection probe failed (ORA-12514)." : null,
    adb_lifecycle_state: adbLifecycleState,
  };
}

function clientWith(data?: DatabaseStatusData, error = false) {
  const client = new QueryClient();
  if (data) client.setQueryData(DATABASE_STATUS_QUERY_KEY, data);
  if (error) {
    // 前回の取得の後に確認が失敗した状態（data を残して error）。
    client
      .getQueryCache()
      .build(client, { queryKey: DATABASE_STATUS_QUERY_KEY })
      .setState({ status: "error", error: new Error("503"), fetchStatus: "idle" });
  }
  return client;
}

function renderGate(path: string, client: QueryClient, node: ReactNode = <p>業務画面</p>, extra: Partial<Parameters<typeof DatabaseGate>[0]> = {}) {
  return renderToStaticMarkup(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <DatabaseGate api={api} routes={routes} {...extra}>
          {node}
        </DatabaseGate>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ゲートを通さない画面", () => {
  it("システム設定の 5 画面と /settings だけを通し、ユーザー / ロール管理や製品固有の設定は通さない", () => {
    for (const path of Object.values(SYSTEM_SETTINGS_PATHS)) {
      expect(isDatabaseGateExemptPath(path)).toBe(true);
      expect(isDatabaseGateExemptPath(`${path}/detail`)).toBe(true);
    }
    expect(isDatabaseGateExemptPath("/settings")).toBe(true);
    for (const path of [
      USER_ROLE_PATHS.users,
      USER_ROLE_PATHS.roles,
      "/settings/security/permissions",
      "/settings/pipeline",
      "/settings/retrieval",
      "/settings/system-tables",
      "/settings/databasex",
      "/settingsx",
      "/",
      "/search",
    ]) {
      expect(isDatabaseGateExemptPath(path)).toBe(false);
    }
  });

  it("除外の画面では状態 API を呼ばずに子を描く", () => {
    const getDatabaseStatus = vi.fn(api.getDatabaseStatus);
    const html = renderGate(SYSTEM_SETTINGS_PATHS.database, new QueryClient(), <p>設定画面</p>, {
      api: { getDatabaseStatus },
    });
    expect(html).toBe("<p>設定画面</p>");
    expect(getDatabaseStatus).not.toHaveBeenCalled();
  });
});

describe("databaseGateView（状態ごとの分岐）", () => {
  const base = { exempt: false, onSystemTables: false, isPending: false, isError: false, hasSecondaryGate: false };

  it("確認中・失敗・4 つの状態をそれぞれの表示へ分ける", () => {
    expect(databaseGateView({ ...base, isPending: true, data: undefined })).toEqual({ kind: "checking" });
    expect(databaseGateView({ ...base, isError: true, data: snapshot("ok") })).toEqual({
      kind: "notice",
      status: "check_failed",
      reasonCode: null,
      adbLifecycleState: null,
    });
    expect(databaseGateView({ ...base, data: snapshot("ok") })).toEqual({ kind: "children" });
    expect(databaseGateView({ ...base, data: snapshot("not_configured", "wallet_not_found") })).toEqual({
      kind: "notice",
      status: "not_configured",
      reasonCode: "wallet_not_found",
      adbLifecycleState: null,
    });
    expect(databaseGateView({ ...base, data: snapshot("unreachable", "ok", "STOPPED") })).toMatchObject({
      status: "unreachable",
      adbLifecycleState: "STOPPED",
    });
    expect(databaseGateView({ ...base, data: snapshot("setup_required", "schema_check_failed") })).toMatchObject({
      status: "setup_required",
      reasonCode: "schema_check_failed",
    });
  });

  it("システムテーブルの管理は setup_required でも開け、製品の追加の確認より前に通す", () => {
    const onSystemTables = { ...base, onSystemTables: true, hasSecondaryGate: true };
    expect(databaseGateView({ ...onSystemTables, data: snapshot("setup_required") })).toEqual({ kind: "children" });
    expect(databaseGateView({ ...onSystemTables, data: snapshot("ok") })).toEqual({ kind: "children" });
    expect(databaseGateView({ ...onSystemTables, data: snapshot("unreachable") })).toMatchObject({ kind: "notice" });
    expect(databaseGateView({ ...base, hasSecondaryGate: true, data: snapshot("ok") })).toEqual({ kind: "secondary" });
    expect(databaseGateView({ ...base, exempt: true, isPending: true, data: undefined })).toEqual({ kind: "children" });
  });
});

describe("DatabaseGate（表示）", () => {
  it("確認中は経過時間付きの読み込み表示を出し、業務画面を描かない", () => {
    const html = renderGate("/profiles", new QueryClient());
    expect(html).toContain('data-testid="database-gate-loading"');
    expect(html).toContain('data-processing-placement="page"');
    expect(html).toContain(DATABASE_GATE_MESSAGES["dbGate.checking"]);
    expect(html).not.toContain("業務画面");
  });

  it("状態ごとに見出し・導線を分ける（未設定・接続できない → データベース設定、確認できない → 再試行だけ）", () => {
    const cases = [
      {
        data: snapshot("not_configured", "missing"),
        title: DATABASE_GATE_MESSAGES["dbGate.notConfigured.title"],
        message: DATABASE_GATE_MESSAGES["dbGate.notConfigured.message"],
        href: "/settings/database",
      },
      {
        data: snapshot("unreachable"),
        title: DATABASE_GATE_MESSAGES["dbGate.unreachable.title"],
        message: DATABASE_GATE_MESSAGES["dbGate.unreachable.message"],
        href: "/settings/database#adb-management",
      },
      {
        data: snapshot("unreachable", "ok", "STOPPED"),
        title: DATABASE_GATE_MESSAGES["dbGate.adbStopped.title"],
        message: DATABASE_GATE_MESSAGES["dbGate.adbStopped.message"],
        href: "/settings/database#adb-management",
      },
      {
        data: snapshot("unreachable", "ok", "STARTING"),
        title: DATABASE_GATE_MESSAGES["dbGate.adbStarting.title"],
        message: DATABASE_GATE_MESSAGES["dbGate.adbStarting.message"],
        href: "/settings/database#adb-management",
      },
      {
        data: snapshot("unreachable", "ok", "AVAILABLE"),
        title: DATABASE_GATE_MESSAGES["dbGate.unreachable.title"],
        message: DATABASE_GATE_MESSAGES["dbGate.adbAvailable.message"],
        href: "/settings/database",
      },
    ];
    for (const { data, title, message, href } of cases) {
      const html = renderGate("/profiles?tab=list", clientWith(data), <p>業務画面</p>, admin);
      expect(html).not.toContain("業務画面");
      expect(html).toMatch(/<section[^>]*aria-labelledby="database-unavailable-title"/);
      expect(html).toContain(`<h1 id="database-unavailable-title" class="mt-5 text-lg font-semibold text-fg">${title}</h1>`);
      expect(html).toContain(message);
      expect(html).toContain(`href="${href}"`);
      expect(html).toContain(DATABASE_GATE_MESSAGES["dbGate.openDatabaseSettings"]);
      expect(html).not.toContain(DATABASE_GATE_MESSAGES["dbGate.openSystemTables"]);
      expect(html).toMatch(/<button[^>]*type="button"[^>]*>.*再試行/);
      expect(html).toContain(DATABASE_GATE_MESSAGES["dbGate.settingsHint"]);
      // 設定を開くリンクが先、再試行が後（Tab 順）。
      expect(html.search(new RegExp(`<a[^>]*href="${href}"`))).toBeLessThan(html.search(/<button/));
    }

    const failed = renderGate("/profiles", clientWith(snapshot("ok"), true), <p>業務画面</p>, admin);
    expect(failed).toContain(DATABASE_GATE_MESSAGES["dbGate.checkFailed.title"]);
    expect(failed).not.toContain("<a");
    expect(failed).toMatch(/<button[^>]*type="button"[^>]*>.*再試行/);
  });

  it("接続できないときは ADB の状態を StatusBadge（アイコン付き）で出し、ORA コードは出さない（#320）", () => {
    const html = renderGate("/profiles", clientWith(snapshot("unreachable", "ok", "STOPPED")), <p>業務画面</p>, admin);
    expect(html).toContain('data-testid="database-gate-adb-state"');
    expect(html).toContain("Autonomous Database: 停止済み");
    expect(html).toContain('data-status-variant="neutral"');
    expect(html).not.toContain("ORA-12514");
    expect(html).not.toContain("診断コード");
    expect(renderGate("/profiles", clientWith(snapshot("unreachable")), <p>業務画面</p>, admin)).not.toContain(
      "database-gate-adb-state",
    );
  });

  it("setup_required はシステムテーブルへ案内し、システムテーブルの画面は開ける", () => {
    const client = clientWith(snapshot("setup_required", "migration_required"));
    const html = renderGate("/profiles", client, <p>業務画面</p>, admin);
    expect(html).toContain(DATABASE_GATE_MESSAGES["dbGate.setupRequired.title"]);
    expect(html).toContain(DATABASE_GATE_MESSAGES["dbGate.setupRequired.message"]);
    expect(html).toContain('href="/settings/system-tables"');
    expect(html).toContain(DATABASE_GATE_MESSAGES["dbGate.openSystemTables"]);
    expect(html).not.toContain(DATABASE_GATE_MESSAGES["dbGate.openDatabaseSettings"]);
    expect(html).toContain("診断コード: migration_required");
    expect(renderGate("/settings/system-tables", client, <p>システムテーブル</p>)).toBe("<p>システムテーブル</p>");
  });

  it("systemTables が無い製品は setup_required もデータベース設定へ案内する", () => {
    const html = renderGate("/search", clientWith(snapshot("setup_required")), <p>検索</p>, {
      ...admin,
      routes: { databaseSettings: "/settings/database#adb-management" },
    });
    expect(html).toContain('href="/settings/database"');
    expect(html).not.toContain(DATABASE_GATE_MESSAGES["dbGate.openSystemTables"]);
  });

  it("設定を開けない利用者には導線を出さず、システム管理者への連絡を案内する（再試行は残す）", () => {
    const cases = [
      [snapshot("not_configured", "missing"), "dbGate.notConfigured.contactAdmin"],
      [snapshot("unreachable"), "dbGate.unreachable.contactAdmin"],
      [snapshot("unreachable", "ok", "STOPPED"), "dbGate.adbStopped.contactAdmin"],
      [snapshot("setup_required", "migration_required"), "dbGate.setupRequired.contactAdmin"],
    ] as const;
    for (const [data, messageKey] of cases) {
      // 権限を渡さない（既定）＝分からないときも導線を出さない。
      const html = renderGate("/profiles", clientWith(data));
      expect(html).toContain(DATABASE_GATE_MESSAGES[messageKey]);
      expect(html).toContain("システム管理者に連絡");
      expect(html).not.toContain("<a");
      expect(html).not.toContain(DATABASE_GATE_MESSAGES["dbGate.openDatabaseSettings"]);
      expect(html).not.toContain(DATABASE_GATE_MESSAGES["dbGate.openSystemTables"]);
      expect(html).not.toContain(DATABASE_GATE_MESSAGES["dbGate.settingsHint"]);
      expect(html).toContain(DATABASE_GATE_MESSAGES["dbGate.contactAdmin.footer"]);
      expect(html).toMatch(/<button[^>]*type="button"[^>]*>.*再試行/);
    }
    const failed = renderGate("/profiles", clientWith(snapshot("ok"), true));
    expect(failed).toContain(DATABASE_GATE_MESSAGES["dbGate.checkFailed.contactAdmin"]);
  });

  it("権限は画面ごとに判定する（システムテーブルだけ開ける・データベース設定だけ開ける）", () => {
    const tablesOnly = { canManageDatabase: false, canManageSystemTables: true };
    const setup = renderGate("/profiles", clientWith(snapshot("setup_required")), <p>業務画面</p>, tablesOnly);
    expect(setup).toContain('href="/settings/system-tables"');
    const unreachable = renderGate("/profiles", clientWith(snapshot("unreachable")), <p>業務画面</p>, tablesOnly);
    expect(unreachable).not.toContain("<a");
    expect(unreachable).toContain(DATABASE_GATE_MESSAGES["dbGate.unreachable.contactAdmin"]);

    const databaseOnly = { canManageDatabase: true, canManageSystemTables: false };
    const setupDb = renderGate("/profiles", clientWith(snapshot("setup_required")), <p>業務画面</p>, databaseOnly);
    expect(setupDb).not.toContain("<a");
    expect(setupDb).toContain(DATABASE_GATE_MESSAGES["dbGate.setupRequired.contactAdmin"]);
  });

  it("製品の文言で上書きでき、上書きしない文言は既定を使う", () => {
    const html = renderGate("/search", clientWith(snapshot("not_configured", "missing")), <p>検索</p>, {
      ...admin,
      messages: { "dbGate.notConfigured.message": "RAG 機能を使うには接続情報を設定してください。" },
    });
    expect(html).toContain("RAG 機能を使うには接続情報を設定してください。");
    expect(html).toContain(DATABASE_GATE_MESSAGES["dbGate.notConfigured.title"]);
  });

  it("DB が使えるときは secondaryGate を通し、共通の案内と読み込み表示を渡す", () => {
    function Secondary({ renderNotice, returnTo }: DatabaseSecondaryGateProps) {
      return (
        <>
          {renderNotice({ title: "保存済みの業務データを復元できません", reasonCode: "snapshot_load_failed", onRetry: () => undefined })}
          <span>{returnTo}</span>
        </>
      );
    }
    const html = renderGate("/profiles#top", clientWith(snapshot("ok")), <p>業務画面</p>, { secondaryGate: Secondary });
    expect(html).toContain("保存済みの業務データを復元できません");
    expect(html).toContain("診断コード: snapshot_load_failed");
    expect(html).toContain("<span>/profiles#top</span>");
    expect(html).not.toContain("業務画面");
  });
});

describe("診断コード", () => {
  it("段階 1 で増えた check の値にも補足の文言がある", () => {
    for (const code of [
      "missing",
      "missing_credentials",
      "wallet_not_found",
      "wallet_password_invalid",
      "invalid",
      "walletless_tls_dsn_required",
      "invalid_configuration",
      "schema_check_failed",
      "migration_required",
      "migration_check_failed",
    ]) {
      const key = databaseCheckMessageKey(code);
      expect(key, code).toBeDefined();
      expect(DATABASE_GATE_MESSAGES[key!].length).toBeGreaterThan(0);
    }
    expect(databaseCheckMessageKey("unknown_code")).toBeUndefined();
    expect(databaseCheckMessageKey("toString")).toBeUndefined();
    expect(databaseReasonCode("ok")).toBeNull();
    expect(databaseReasonCode(" ")).toBeNull();
    expect(databaseReasonCode("wallet_not_found")).toBe("wallet_not_found");
  });

  it("補足のあるコードは補足とコードを出し、無いコードはコードだけを出す", () => {
    const known = renderToStaticMarkup(
      <MemoryRouter>
        <DatabaseUnavailableNotice routes={routes} status="not_configured" reasonCode="wallet_password_invalid" onRetry={() => undefined} />
      </MemoryRouter>,
    );
    expect(known).toContain(DATABASE_GATE_MESSAGES["dbGate.check.wallet_password_invalid"]);
    expect(known).toContain('<p role="status">診断コード: wallet_password_invalid</p>');
    const unknown = renderToStaticMarkup(
      <MemoryRouter>
        <DatabaseUnavailableNotice routes={routes} status="unreachable" reasonCode="custom_code" onRetry={() => undefined} />
      </MemoryRouter>,
    );
    expect(unknown).toContain("診断コード: custom_code");
  });
});

describe("DatabaseUnavailableNotice（banner）", () => {
  it("見出し・本文・再試行・データベース設定のリンクを Banner で出す", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <DatabaseUnavailableNotice
          mode="banner"
          routes={routes}
          title="データベースに接続できません"
          message={<p>最新のデータを取得できませんでした。</p>}
          onRetry={() => undefined}
          settingsLink
          canManageDatabase
        />
      </MemoryRouter>,
    );
    expect(html).toContain('role="status"');
    expect(html).toContain("データベースに接続できません");
    expect(html).toContain("最新のデータを取得できませんでした。");
    expect(html).toContain("再試行");
    expect(html).toMatch(/<a[^>]*href="\/settings\/database#adb-management"/);
    expect(html).not.toContain("database-unavailable-title");
  });

  it("データベース設定を開けない利用者には、banner にもリンクを出さない", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <DatabaseUnavailableNotice mode="banner" routes={routes} onRetry={() => undefined} settingsLink />
      </MemoryRouter>,
    );
    expect(html).toContain("再試行");
    expect(html).not.toContain("<a");
  });

  it("再試行もリンクも無い banner は操作の行を出さない", () => {
    const html = renderToStaticMarkup(
      <MemoryRouter>
        <DatabaseUnavailableNotice mode="banner" routes={routes} />
      </MemoryRouter>,
    );
    expect(html).toContain(DATABASE_GATE_MESSAGES["dbGate.unreachable.title"]);
    expect(html).not.toContain("<button");
    expect(html).not.toContain("<a");
  });
});
