import { describe, expect, it } from "vitest";

import { isDatabaseGateExempt } from "@/components/system/DatabaseGate";
import { APP_ROUTES } from "@/lib/routes";
import { loginEntryRoute } from "./AuthPages";

const allow =
  (...codes: string[]) =>
  (permission: string) =>
    codes.includes(permission);

describe("loginEntryRoute", () => {
  it("ログイン前に開こうとした URL へ、権限があれば戻す", () => {
    const hasPermission = allow("menu.file_list", "menu.chat");
    expect(loginEntryRoute("/file-list")(hasPermission)).toBe("/file-list");
    // 権限の無い URL・認証画面・外部 URL・不正な値は既定の入口（チャット。#432）へ。
    expect(loginEntryRoute("/settings/oci")(hasPermission)).toBe(APP_ROUTES.chat);
    expect(loginEntryRoute(APP_ROUTES.login)(hasPermission)).toBe(APP_ROUTES.chat);
    expect(loginEntryRoute(APP_ROUTES.forbidden)(hasPermission)).toBe(APP_ROUTES.chat);
    expect(loginEntryRoute("//evil.example.com")(hasPermission)).toBe(APP_ROUTES.chat);
    expect(loginEntryRoute("https://evil.example.com")(hasPermission)).toBe(APP_ROUTES.chat);
    expect(loginEntryRoute(undefined)(hasPermission)).toBe(APP_ROUTES.chat);
    // チャットを開けなければ `/`（`/` が最初に開ける画面へ振り分ける）。
    expect(loginEntryRoute(42)(allow("menu.search"))).toBe(APP_ROUTES.home);
  });
});

describe("isDatabaseGateExempt", () => {
  it("ゲートを通さないのはシステム設定の 5 画面だけ（NL2SQL と同じ）", () => {
    expect(isDatabaseGateExempt("/settings")).toBe(true);
    for (const route of [
      APP_ROUTES.settingsOci,
      APP_ROUTES.settingsUploadStorage,
      APP_ROUTES.settingsModel,
      APP_ROUTES.settingsDatabase,
      APP_ROUTES.settingsAppearance,
    ]) {
      expect(isDatabaseGateExempt(route)).toBe(true);
    }
    // RAG 固有の設定は DB に設定を持つため、他の業務画面と同じくゲートを通す。
    for (const route of [
      APP_ROUTES.settingsPipeline,
      APP_ROUTES.settingsRetrieval,
      APP_ROUTES.settingsServices,
      APP_ROUTES.settingsPrompts,
      APP_ROUTES.securityUsers,
      APP_ROUTES.securityRoles,
      APP_ROUTES.securityPermissions,
      APP_ROUTES.search,
      "/settingsx",
    ]) {
      expect(isDatabaseGateExempt(route)).toBe(false);
    }
  });
});
