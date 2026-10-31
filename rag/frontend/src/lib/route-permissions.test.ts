import { History } from "lucide-react";
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { NAV_ITEMS, NAV_SECTIONS, visibleNavSections } from "@/components/layout/nav-config";
import { CAPABILITY_PERMISSIONS, MENU_PERMISSIONS } from "./permissions";
import {
  canOpenDocumentDetail,
  canOpenRoute,
  defaultEntryRoute,
  firstAllowedRoute,
  routeRequiredPermissions,
  settingsEntryRoute,
} from "./route-permissions";
import { APP_ROUTES } from "./routes";

const allow =
  (...codes: string[]) =>
  (permission: string) =>
    codes.includes(permission);

describe("権限コードの対応表", () => {
  it("frontend の権限コードは backend の権限カタログと一致する", () => {
    const catalog = readFileSync(
      resolve(__dirname, "../../../backend/app/security/permissions.py"),
      "utf8"
    );
    const backendCodes = new Set(
      [...catalog.matchAll(/^[A-Z_]+ = "((?:menu|rag)\.[a-z_.]+)"$/gm)].map((match) => match[1])
    );
    const frontendCodes = new Set([
      ...Object.values(MENU_PERMISSIONS),
      ...Object.values(CAPABILITY_PERMISSIONS),
    ]);
    expect(backendCodes.size).toBeGreaterThan(30);
    expect([...frontendCodes].sort()).toEqual([...backendCodes].sort());
  });

  it("ナビのすべての項目にメニュー権限があり、メニュー権限はすべてナビの項目になる", () => {
    const navPermissions = NAV_ITEMS.map((item) => item.permission);
    expect(navPermissions.every((code) => code.startsWith("menu."))).toBe(true);
    expect(new Set(navPermissions).size).toBe(navPermissions.length);
    expect([...navPermissions].sort()).toEqual(Object.values(MENU_PERMISSIONS).sort());
  });

  // Issue 399 / 402 / 409（テスト名に「#」付きの番号を書くと、lint が生の hex 色として検出するため、ここに書く）。
  it("利用の画面はチャット → RAG 検索 → 実行履歴、検索・回答プロファイルはナレッジベースの直下（文書の分類はナレッジベースの直上）、品質評価とフィードバックは「改善・運用」", () => {
    const hrefs = (key: string) => NAV_SECTIONS.find((section) => section.titleKey === key)?.items.map((item) => item.href);
    expect(hrefs("nav.section.use")).toEqual([APP_ROUTES.chat, APP_ROUTES.search, APP_ROUTES.history]);
    expect(hrefs("nav.section.ingestion")).toEqual([
      APP_ROUTES.upload,
      APP_ROUTES.fileList,
      APP_ROUTES.documentCategories,
      APP_ROUTES.knowledgeBases,
      APP_ROUTES.searchAnswerProfiles,
    ]);
    expect(hrefs("nav.section.improve")).toEqual([APP_ROUTES.evaluation, APP_ROUTES.feedback]);
    // NL2SQL と同じ位置: 取込・検索・回答設定（準備）の後、セキュリティ設定の前。
    const keys = NAV_SECTIONS.map((section) => section.titleKey);
    expect(keys.indexOf("nav.section.improve")).toBe(keys.indexOf("nav.section.pipeline") + 1);
    expect(keys.indexOf("nav.section.security")).toBe(keys.indexOf("nav.section.improve") + 1);
  });

  // Issue 658: 下部の並びとセクション名を 3 製品でそろえた（Issue 番号は hex 色の lint に掛かるので名前に書かない）。
  it("ナビの下部は 3 製品で同じ（改善・運用 → セキュリティ設定 → ユーザーとロール → 運用設定 → システム設定）", () => {
    expect(NAV_SECTIONS.map((section) => section.titleKey).slice(-5)).toEqual([
      "nav.section.improve",
      "nav.section.security",
      "nav.section.userRoles",
      "nav.section.operations",
      "nav.section.settings",
    ]);
    // システムテーブルは NL2SQL と同じく運用設定の先頭で、その下にチャットの添付（#2517・#2546）と回答の注意書き（#658・#2449）。
    const operations = NAV_SECTIONS.find((section) => section.titleKey === "nav.section.operations");
    expect(operations?.items.map((item) => [item.href, item.permission])).toEqual([
      [APP_ROUTES.settingsSystemTables, MENU_PERMISSIONS.settingsSystemTables],
      [APP_ROUTES.settingsTraceRecording, MENU_PERMISSIONS.settingsTraceRecording],
      [APP_ROUTES.settingsChatAttachments, MENU_PERMISSIONS.settingsChatAttachments],
      [APP_ROUTES.settingsAnswerNotice, MENU_PERMISSIONS.settingsAnswerNotice],
      [APP_ROUTES.settingsHuggingface, MENU_PERMISSIONS.settingsHuggingface],
      [APP_ROUTES.settingsServices, MENU_PERMISSIONS.settingsServices],
    ]);
    const security = NAV_SECTIONS.find((section) => section.titleKey === "nav.section.security");
    expect(security?.items.map((item) => item.href)).toEqual([APP_ROUTES.securityPermissions]);
    const userRoles = NAV_SECTIONS.find((section) => section.titleKey === "nav.section.userRoles");
    expect(userRoles?.items.map((item) => [item.href, item.permission])).toEqual([
      [APP_ROUTES.securityUsers, MENU_PERMISSIONS.securityUsers],
      [APP_ROUTES.securityRoles, MENU_PERMISSIONS.securityRoles],
      // 全員の API キー（管理の画面。RAG / NL2SQL だけ。#1805）。
      [APP_ROUTES.securityApiKeys, MENU_PERMISSIONS.securityApiKeys],
    ]);
  });

  // #1805・#1873
  it("アカウントの画面（自分の API キー）はナビに無く、ログインしていれば権限を問わず開ける", () => {
    expect(NAV_ITEMS.some((item) => item.href === APP_ROUTES.account)).toBe(false);
    expect(canOpenRoute(APP_ROUTES.account, () => false)).toBe(true);
    expect(canOpenRoute(APP_ROUTES.securityApiKeys, () => false)).toBe(false);
  });
});

describe("ナビのセクションの既定の開閉", () => {
  // 保存値が無いときは業務の入口の「AI 活用」だけを展開し、ほかは閉じる（3 製品で同じ。#1563）。
  it("AI 活用だけを既定で展開し、ほかのセクションは既定で閉じる", () => {
    const expandedByDefault = NAV_SECTIONS.filter((section) => !section.defaultCollapsed).map(
      (section) => section.titleKey
    );
    expect(expandedByDefault).toEqual(["nav.section.use"]);
    expect(NAV_SECTIONS[0].titleKey).toBe("nav.section.use");
  });
});

describe("ナビのアイコン", () => {
  // Issue 658: 3 製品で同じ機能は同じアイコン、違う機能は違うアイコン。
  // Issue 1890: 実行履歴は NL2SQL・Agent の実行履歴と同じ History のアイコン（同じ機能は同じアイコン）。
  it("実行履歴は 3 製品で同じ History のアイコン", () => {
    const history = NAV_ITEMS.find((item) => item.href === APP_ROUTES.history);
    expect(history?.icon).toBe(History);
    expect(history?.permission).toBe(MENU_PERMISSIONS.history);
  });

  it("機能ごとに違うアイコンを使う（同じアイコンを 2 つの項目に使わない）", () => {
    const items = NAV_SECTIONS.flatMap((section) => section.items);
    expect(new Set(items.map((item) => item.icon)).size).toBe(items.length);
  });
});

describe("visibleNavSections", () => {
  it("権限のない項目と、項目が 0 件になったセクションを隠す", () => {
    const sections = visibleNavSections(
      allow(MENU_PERMISSIONS.search, MENU_PERMISSIONS.upload, MENU_PERMISSIONS.securityUsers)
    );
    expect(sections.map((section) => section.titleKey)).toEqual([
      "nav.section.use",
      "nav.section.ingestion",
      "nav.section.userRoles",
    ]);
    expect(sections.flatMap((section) => section.items.map((item) => item.href))).toEqual([
      APP_ROUTES.search,
      APP_ROUTES.upload,
      APP_ROUTES.securityUsers,
    ]);
  });

  it("権限が無ければ空、すべてあれば全項目", () => {
    expect(visibleNavSections(() => false)).toEqual([]);
    expect(visibleNavSections(() => true).flatMap((section) => section.items)).toHaveLength(
      NAV_ITEMS.length
    );
  });
});

describe("ルートの権限", () => {
  it("ナビの URL は項目の権限、詳細画面は開く導線の権限のどれかで開ける", () => {
    expect(routeRequiredPermissions(APP_ROUTES.feedback)).toEqual([MENU_PERMISSIONS.feedback]);
    expect(routeRequiredPermissions(APP_ROUTES.securityPermissions)).toEqual([
      MENU_PERMISSIONS.securityPermissions,
    ]);
    expect(routeRequiredPermissions("/knowledge-bases/kb-1")).toEqual([
      MENU_PERMISSIONS.knowledgeBases,
    ]);
    // 実行履歴の詳細（#1890）は一覧と同じメニュー権限。
    expect(routeRequiredPermissions(APP_ROUTES.history)).toEqual([MENU_PERMISSIONS.history]);
    expect(routeRequiredPermissions("/history/trace-1")).toEqual([MENU_PERMISSIONS.history]);
    expect(routeRequiredPermissions("/documents/doc-1")).toEqual([
      MENU_PERMISSIONS.upload,
      MENU_PERMISSIONS.fileList,
    ]);
    expect(routeRequiredPermissions("/")).toBeUndefined();
    expect(routeRequiredPermissions("/unknown")).toBeUndefined();
    expect(routeRequiredPermissions("/documents/")).toBeUndefined();
  });

  it("canOpenRoute は権限のない URL を拒否し、振り分けだけの URL は通す", () => {
    const hasPermission = allow(MENU_PERMISSIONS.chat);
    expect(canOpenRoute(APP_ROUTES.chat, hasPermission)).toBe(true);
    expect(canOpenRoute(APP_ROUTES.search, hasPermission)).toBe(false);
    expect(canOpenRoute(APP_ROUTES.securityUsers, hasPermission)).toBe(false);
    // 文書の詳細はワークスペース専用の API を使うため、チャットの権限だけでは開けない（#303）。
    expect(canOpenRoute("/documents/doc-1", hasPermission)).toBe(false);
    expect(canOpenRoute("/documents/doc-1", allow(MENU_PERMISSIONS.fileList))).toBe(true);
    expect(canOpenRoute("/knowledge-bases/kb-1", hasPermission)).toBe(false);
    expect(canOpenRoute("/", hasPermission)).toBe(true);
  });

  it("文書の詳細へのリンクは、アップロードか文書インデックスの権限があるときだけ出す", () => {
    expect(canOpenDocumentDetail(allow(MENU_PERMISSIONS.upload))).toBe(true);
    expect(canOpenDocumentDetail(allow(MENU_PERMISSIONS.fileList))).toBe(true);
    for (const permission of [
      MENU_PERMISSIONS.search,
      MENU_PERMISSIONS.chat,
      MENU_PERMISSIONS.knowledgeBases,
      MENU_PERMISSIONS.feedback,
    ]) {
      expect(canOpenDocumentDetail(allow(permission))).toBe(false);
    }
  });

  it("既定の入口はチャット（ナビの先頭。`/` と同じ）、開けなければ `/`", () => {
    expect(defaultEntryRoute(() => true)).toBe(APP_ROUTES.chat);
    expect(defaultEntryRoute(allow(MENU_PERMISSIONS.upload, MENU_PERMISSIONS.chat))).toBe(APP_ROUTES.chat);
    // チャットを開けない利用者は `/` へ戻し、ナビの並び順で最初に開ける画面（ここでは RAG 検索）へ。
    expect(defaultEntryRoute(allow(MENU_PERMISSIONS.search))).toBe(APP_ROUTES.home);
    expect(firstAllowedRoute(allow(MENU_PERMISSIONS.search))).toBe(APP_ROUTES.search);
    expect(defaultEntryRoute(() => false)).toBe(APP_ROUTES.home);
  });

  it("`/` の移動先はナビの並びで最初に開ける画面、どれも無ければ権限なし", () => {
    // チャットをナビの先頭に置いたので、すべて開ける利用者の `/` はチャット（#399）。
    expect(firstAllowedRoute(() => true)).toBe(APP_ROUTES.chat);
    expect(firstAllowedRoute(allow(MENU_PERMISSIONS.search, MENU_PERMISSIONS.evaluation))).toBe(APP_ROUTES.search);
    expect(firstAllowedRoute(allow(MENU_PERMISSIONS.upload, MENU_PERMISSIONS.chat))).toBe(
      APP_ROUTES.chat
    );
    expect(firstAllowedRoute(allow(MENU_PERMISSIONS.settingsAppearance))).toBe(
      APP_ROUTES.settingsAppearance
    );
    expect(firstAllowedRoute(() => false)).toBe(APP_ROUTES.forbidden);
  });

  it("/settings はシステム設定のうち最初に開ける画面、無ければ既定の入口", () => {
    expect(settingsEntryRoute(() => true)).toBe(APP_ROUTES.settingsOci);
    expect(settingsEntryRoute(allow(MENU_PERMISSIONS.settingsModel, MENU_PERMISSIONS.search))).toBe(
      APP_ROUTES.settingsModel
    );
    expect(settingsEntryRoute(allow(MENU_PERMISSIONS.search))).toBe(APP_ROUTES.home);
  });
});
