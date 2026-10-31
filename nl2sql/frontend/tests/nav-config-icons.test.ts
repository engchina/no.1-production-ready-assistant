import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

type ResolveFilename = (request: string, parent: unknown, isMain: boolean, options?: unknown) => string;
type ModuleWithResolver = { _resolveFilename: ResolveFilename };

const require = createRequire(import.meta.url);
const testDir = dirname(fileURLToPath(import.meta.url));
const frontendRoot = resolve(testDir, "..");
const moduleResolver = require("node:module") as ModuleWithResolver;
const originalResolveFilename = moduleResolver._resolveFilename;

moduleResolver._resolveFilename = function resolveTestAlias(
  this: unknown,
  request: string,
  parent: unknown,
  isMain: boolean,
  options?: unknown
) {
  if (!request.startsWith("@/")) {
    return originalResolveFilename.call(this, request, parent, isMain, options);
  }
  const basePath = resolve(frontendRoot, "src", request.slice(2));
  const resolvedRequest = [basePath, `${basePath}.ts`, `${basePath}.tsx`].find((candidate) =>
    existsSync(candidate)
  );
  return originalResolveFilename.call(this, resolvedRequest ?? basePath, parent, isMain, options);
};

const [
  { NAV_SECTIONS },
  { ROUTE_PERMISSIONS, defaultEntryRoute, firstAllowedRoute },
] = await Promise.all([
  import("../src/components/layout/nav-config.ts"),
  import("../src/features/security/route-permissions.ts"),
]);
moduleResolver._resolveFilename = originalResolveFilename;

const EXPECTED_ICON_NAMES_BY_LABEL_KEY = new Map<string, string>([
  ["nav.chat", "MessagesSquare"],
  ["nav.query", "Sparkles"],
  ["nav.directSql", "FileCode2"],
  ["nav.sqlToQuestion", "MessageSquareCode"],
  ["nav.history", "History"],
  ["nav.dbAdminChat", "BotMessageSquare"],
  ["nav.adminSql", "SquareTerminal"],
  ["nav.tableManagement", "Table2"],
  ["nav.viewManagement", "Eye"],
  ["nav.dataManagement", "FileSpreadsheet"],
  ["nav.commentManagement", "MessageSquareText"],
  ["nav.annotationManagement", "Tags"],
  ["nav.domainManagement", "Shapes"],
  ["nav.glossaryRules", "BookA"],
  ["nav.globalRules", "ScrollText"],
  ["nav.sampleData", "Boxes"],
  ["nav.profiles", "BriefcaseBusiness"],
  ["nav.ontologyBuild", "Network"],
  ["nav.feedbackManagement", "ThumbsUp"],
  ["nav.questionClassifierModels", "BrainCircuit"],
  ["nav.evaluation", "FlaskConical"],
  // セキュリティ設定（固有。#206。セクション名は 3 製品で同じ。#658）
  ["nav.securityPermissions", "LockKeyhole"],
  ["nav.securityDeepSec", "ShieldCheck"],
  // ユーザーとロール（3製品共通。#206）
  ["nav.securityUsers", "Users"],
  ["nav.securityRoles", "Shield"],
  // 全員の API キー（RAG / NL2SQL だけ。#1805）
  ["nav.securityApiKeys", "KeySquare"],
  // 運用設定（#81。Select AI Credential はデータベース設定から分けた。#658。回答の注意書きは #2449、
  // チャットの添付は #2517・#2546）
  ["nav.settingsSystemTables", "TableProperties"],
  ["nav.settingsTraceRecording", "Waypoints"],
  ["nav.settingsChatAttachments", "Paperclip"],
  ["nav.settingsAnswerNotice", "MessageSquareWarning"],
  ["nav.settingsSelectAiCredential", "FileKey"],
  // システム設定（3製品共通）
  ["nav.settingsUploadStorage", "HardDrive"],
  ["nav.settingsModel", "BrainCog"],
  ["nav.settingsDatabase", "Database"],
  ["nav.settingsAppearance", "Palette"],
]);

test("サイドバーのメニュー icon は機能ごとに期待した Lucide icon を使う", () => {
  const items = NAV_SECTIONS.flatMap((section) => section.items);

  assert.equal(items.length, EXPECTED_ICON_NAMES_BY_LABEL_KEY.size);
  assert.deepEqual(
    items.map((item) => item.labelKey),
    Array.from(EXPECTED_ICON_NAMES_BY_LABEL_KEY.keys())
  );

  for (const item of items) {
    assert.equal(item.icon.displayName, EXPECTED_ICON_NAMES_BY_LABEL_KEY.get(item.labelKey), item.labelKey);
  }
});

test("サイドバーのメニュー icon displayName は重複しない", () => {
  const iconNames = NAV_SECTIONS.flatMap((section) =>
    section.items.map((item) => {
      assert.equal(typeof item.icon.displayName, "string", `${item.labelKey} icon must expose displayName`);
      return item.icon.displayName;
    })
  );

  assert.equal(new Set(iconNames).size, iconNames.length);
});

// #1563
test("サイドバーは AI 活用だけを既定で展開し、ほかのセクションは既定で閉じる", () => {
  // 保存値の優先と今のページのセクションを開く振る舞いは共有 Sidebar が持つ（platform/packages/ui の単体テスト）。
  assert.deepEqual(
    NAV_SECTIONS.filter((section) => !section.defaultCollapsed).map((section) => section.titleKey),
    ["nav.section.use"]
  );
  assert.equal(NAV_SECTIONS[0].titleKey, "nav.section.use");
});

test("既定入口は SQL 生成権限がなければ root に戻し、root が最初の許可画面へ振り分ける", () => {
  const queryOnly = (permission: string) => permission === "menu.query";
  const appearanceOnly = (permission: string) => permission === "menu.settings_appearance";
  const uploadStorageOnly = (permission: string) => permission === "menu.settings_upload_storage";
  const modelOnly = (permission: string) => permission === "menu.settings_model";
  const databaseOnly = (permission: string) => permission === "menu.settings_database";
  const systemTablesOnly = (permission: string) => permission === "menu.settings_system_tables";
  const selectAiCredentialOnly = (permission: string) =>
    permission === "menu.settings_select_ai_credential";
  const adminSqlOnly = (permission: string) => permission === "menu.admin_sql";
  const noPermissions = () => false;

  assert.equal(defaultEntryRoute(queryOnly), "/query");
  assert.equal(defaultEntryRoute(appearanceOnly), "/");
  assert.equal(firstAllowedRoute(uploadStorageOnly), "/settings/upload-storage");
  assert.equal(firstAllowedRoute(modelOnly), "/settings/model");
  assert.equal(firstAllowedRoute(databaseOnly), "/settings/database");
  assert.equal(firstAllowedRoute(systemTablesOnly), "/settings/system-tables");
  assert.equal(firstAllowedRoute(selectAiCredentialOnly), "/settings/select-ai-credential");
  assert.equal(firstAllowedRoute(appearanceOnly), "/settings/appearance");
  assert.equal(defaultEntryRoute(adminSqlOnly), "/");
  assert.equal(firstAllowedRoute(adminSqlOnly), "/admin-sql");
  assert.equal(firstAllowedRoute(noPermissions), "/forbidden");
});

test("root の最初の許可画面はサイドバー項目を漏れなく許可順に使う", () => {
  const items = NAV_SECTIONS.flatMap((section) => section.items);

  assert.deepEqual(Object.keys(ROUTE_PERMISSIONS), items.map((item) => item.href));

  for (const item of items) {
    assert.equal(
      firstAllowedRoute((permission) => permission === item.permission),
      item.href,
      item.labelKey
    );
  }
});
