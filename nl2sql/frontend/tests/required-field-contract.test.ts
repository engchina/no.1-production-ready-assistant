import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

test("required labels use the shared FieldLabel / FieldLegend instead of an app-specific component (#531)", () => {
  // 必須の表示は platform の共有部品（FieldLabel / FieldLegend / Fieldset）に一本化し、NL2SQL に独自の部品を置かない。
  assert.equal(
    existsSync(new URL("../src/components/ui/required-field.tsx", import.meta.url)),
    false
  );
  for (const path of [
    "../src/components/ui/file-dropzone.tsx",
    "../src/features/nl2sql/pages/DataManagementPage.tsx",
    "../src/features/security/SecurityDeepSecPage.tsx",
  ]) {
    const page = source(path);
    assert.doesNotMatch(page, /required-field"/u, path);
    assert.match(page, /\b(?:FieldLabel|FieldLegend),[\s\S]*from "@production-ready\/ui"/u, path);
    // RequiredBadge を直接ラベルに並べず、共有部品の required で出す。
    assert.doesNotMatch(page, /<RequiredBadge\b|t\("common\.required"\)/u, path);
  }
});

test("NL2SQL の文言は任意の欄に「(任意)」を付けない (#531)", () => {
  for (const path of ["../src/lib/i18n.ts", "../src/lib/nl2sql-base-i18n.ts"]) {
    assert.doesNotMatch(source(path), /[(（]\s*任意\s*[)）・、,，]/u, path);
  }
});

test("app source has no legacy asterisk required indicator or legend note", () => {
  const i18n = source("../src/lib/nl2sql-base-i18n.ts");
  assert.doesNotMatch(i18n, /common\.requiredFieldsNote/u);
  for (const path of [
    "../src/components/settings/ModelSettingsClient.tsx",
    "../src/features/nl2sql/pages/DataManagementPage.tsx",
    "../src/features/nl2sql/pages/ProfileManagementPage.tsx",
    "../src/features/security/AuthPages.tsx",
    // ログイン・パスワード変更の画面の実体は platform の共通部品（#220）。
    "../../../platform/packages/system-settings/src/auth/AuthPages.tsx",
    "../src/features/security/SecurityDeepSecPage.tsx",
  ]) {
    assert.doesNotMatch(source(path), /RequiredIndicator|RequiredFieldsNote/u, path);
  }
});

test("file dropzone propagates required semantics to its native input", () => {
  const component = source("../src/components/ui/file-dropzone.tsx");

  assert.match(component, /<FieldLabel htmlFor=\{inputId\} label=\{label\} required=\{required\}/u);
  assert.match(component, /required=\{required\}/u);
  assert.match(component, /aria-required=\{required\}/u);
});

test("table import requires table, workbook sheet, file, and confirmation in the UI gate", () => {
  const page = source("../src/features/nl2sql/pages/TableManagementPage.tsx");

  assert.doesNotMatch(page, /RequiredFieldsNote/u);
  // 表名・シート名は共有の TextField（#631）。必須は TextField の required（バッジと aria-required）で示す。
  assert.match(page, /<TextField\s+id="table-import-table-name"[\s\S]*?required/u);
  assert.match(page, /<TextField\s+id="table-import-sheet-name"[\s\S]*?required=\{sheetRequired\}/u);
  assert.match(page, /<FileDropzone[\s\S]*label=\{t\("dataTools\.dbAdmin\.file"\)\}[\s\S]*required/u);
  assert.match(page, /fileReady[\s\S]*\(!sheetRequired \|\| Boolean\(sheet\.trim\(\)\)\)[\s\S]*isConfirmed/u);
});

test("shared auth pages use TextField required semantics instead of hand-written inputs (#220)", () => {
  const authPages = source("../../../platform/packages/system-settings/src/auth/AuthPages.tsx");

  assert.match(authPages, /<TextField[\s\S]*id="auth-login-user-id"[\s\S]*required[\s\S]*requiredLabel=\{m\.required\}/u);
  assert.match(authPages, /id: "auth-password-new"/u);
  assert.match(authPages, /<TextField[\s\S]*id=\{field\.id\}[\s\S]*required[\s\S]*requiredLabel=\{m\.required\}/u);
  assert.doesNotMatch(authPages, /<input\b/u);
});
