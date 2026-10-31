import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

// PageHeader 本体（並び順・sticky・計測コンテナ・アクションの testId）は共有 UI パッケージの責任で、
// packages/ui のテストが検証する。ここではアプリ側の使い方と状態 badge の契約を検証する。
const source = readFileSync(
  new URL("../src/components/PageHeaderStatusBadge.tsx", import.meta.url),
  "utf8",
);

const migratedPages = [
  "../src/features/nl2sql/pages/TableManagementPage.tsx",
  "../src/features/nl2sql/pages/ViewManagementPage.tsx",
  "../src/features/nl2sql/pages/DataManagementPage.tsx",
  "../src/features/nl2sql/pages/MetadataSqlManagementPage.tsx",
  "../src/features/nl2sql/pages/ProfileManagementPage.tsx",
  "../src/features/nl2sql/pages/HistoryPage.tsx",
  "../src/features/nl2sql/pages/FeedbackManagementPage.tsx",
  "../src/features/nl2sql/pages/QuestionLearningPage.tsx",
  "../src/features/nl2sql/pages/SampleDataPage.tsx",
  "../src/features/nl2sql/pages/SqlToQuestionPage.tsx",
  "../../../platform/packages/system-settings/src/users-roles/UserManagementPage.tsx",
  "../../../platform/packages/system-settings/src/users-roles/RoleManagementPage.tsx",
  // 権限管理の画面の実体は platform の共通 RolePermissionsPage（#220）。
  "../../../platform/packages/system-settings/src/permissions/RolePermissionsPage.tsx",
].map((path) => readFileSync(new URL(path, import.meta.url), "utf8"));

const pageHeaderStatusPages = [
  "../src/features/nl2sql/Nl2SqlWorkbench.tsx",
  "../src/features/nl2sql/pages/TableManagementPage.tsx",
  "../src/features/nl2sql/pages/ViewManagementPage.tsx",
  "../src/features/nl2sql/pages/DataManagementPage.tsx",
  "../src/features/nl2sql/pages/MetadataSqlManagementPage.tsx",
  "../src/features/nl2sql/pages/ProfileManagementPage.tsx",
  "../src/features/nl2sql/pages/QuestionLearningPage.tsx",
].map((path) => readFileSync(new URL(path, import.meta.url), "utf8"));
const profileManagementPage = readFileSync(
  new URL("../src/features/nl2sql/pages/ProfileManagementPage.tsx", import.meta.url),
  "utf8",
);
const schemaRefreshPresentationSource = readFileSync(
  new URL("../src/features/nl2sql/schemaRefreshPresentation.ts", import.meta.url),
  "utf8",
);

test("PageHeaderStatusBadge は短いページ状態を live region として公開する", () => {
  assert.match(source, /export function PageHeaderStatusBadge/u);
  assert.match(source, /role="status"/u);
  assert.match(source, /aria-live="polite"/u);
  assert.match(source, /aria-atomic="true"/u);
  assert.match(source, /data-page-header-status="true"/u);
  assert.match(source, /<StatusBadge variant=\{variant\} label=\{label\}/u);
});

test("ヘッダー横の状態 badge は共通 PageHeaderStatusBadge を使う", () => {
  for (const page of pageHeaderStatusPages) {
    assert.match(page, /PageHeaderStatusBadge|SchemaRefreshHeaderStatus/u);
    assert.doesNotMatch(page, /<span[^>]+aria-live="polite"[^>]*>\s*<StatusBadge/u);
  }
});

test("refresh 系の完了状態はヘッダー横 badge に残さない", () => {
  for (const page of pageHeaderStatusPages.slice(0, 5)) {
    assert.match(page, /SchemaRefreshHeaderStatus/u);
  }
  assert.match(schemaRefreshPresentationSource, /job\.status === "done"/u);
  assert.match(schemaRefreshPresentationSource, /return null/u);
  // DB Profile 一覧の再取得の見出しのバッジは実行中だけ。失敗は案内（ページの先頭か保存の結果の直下）が出し、
  // バッジと 2 か所に出さない（#2034）。
  assert.match(profileManagementPage, /const headerRefreshStatus = dbProfileRefreshing \?/u);
  assert.doesNotMatch(profileManagementPage, /headerRefreshStatus === "done"/u);
});

test("業務プロファイルの schema / DB Profile refresh ボタンは押した側だけを送信の間 loading にする", () => {
  // どちらも durable job。開始のボタンは押した側だけを送信の間 loading にし、job の間は disabled にするだけ。
  // 押した側は送信の間ネイティブの disabled にしない（フォーカスを保つ。#355）。job の間のスピナーは進行の表示（#821）。
  assert.match(profileManagementPage, /id: "schema-refresh"/u);
  assert.match(profileManagementPage, /loading: headerSchemaRefreshStarting/u);
  assert.match(profileManagementPage, /disabled: schemaRefreshing && !headerSchemaRefreshStarting/u);
  assert.match(profileManagementPage, /sharedSchemaRefresh\.start\(PROFILE_SCHEMA_REFRESH_HEADER\)/u);
  assert.match(profileManagementPage, /id: "db-profile-refresh"/u);
  assert.match(profileManagementPage, /loading: headerDbProfileRefreshStarting/u);
  assert.match(profileManagementPage, /disabled: dbProfileRefreshBusy && !headerDbProfileRefreshStarting/u);
  // 案内の「DB Profile 一覧を再取得」は共有の DbProfileListSyncNotice が押した側だけを回す（#2034）。
  assert.match(profileManagementPage, /<DbProfileListSyncNotice[\s\S]*scope="page"/u);
  assert.doesNotMatch(profileManagementPage, /loading: schemaRefreshing/u);
  assert.doesNotMatch(profileManagementPage, /loading=\{startDbProfileRefresh\.isPending \|\| dbProfileRefreshing\}/u);
});

test("移行対象ページは共有 PageHeader を使い、旧トップ概覧カードを表示しない", () => {
  for (const page of migratedPages) {
    assert.match(page, /\bPageHeader\b[\s\S]*from "@production-ready\/ui"/u);
    assert.doesNotMatch(page, /from "@\/components\/PageHeader"/u);
    assert.doesNotMatch(page, /<DbObjectManagementStatusBar/u);
    assert.doesNotMatch(page, /<DbObjectStatusBar/u);
    assert.doesNotMatch(page, /<SecurityManagementStatusBar/u);
  }
});
