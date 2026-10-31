import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function source(path: string): string {
  return readFileSync(new URL(path, import.meta.url), "utf8");
}

function sliceFrom(text: string, startMarker: string): string {
  const start = text.indexOf(startMarker);
  assert.notEqual(start, -1);
  return text.slice(start);
}

// 確認語欄の見た目（中立の面・状態の色・フォーカス）は packages/ui の単体テスト
// （platform/packages/ui/tests/execution-confirmation-field.test.tsx）が守る（#379）。
test("ExecutionConfirmationField is the shared packages/ui component, not a product copy", () => {
  const dbAdminShared = source("../src/features/nl2sql/components/DbAdminShared.tsx");
  assert.doesNotMatch(dbAdminShared, /function ExecutionConfirmationField/u);

  const usages = [
    "../src/features/nl2sql/components/DbAdminShared.tsx",
    "../src/features/nl2sql/components/DbObjectManagementShared.tsx",
    "../src/features/nl2sql/pages/AdminSqlPage.tsx",
    "../src/features/nl2sql/pages/DataManagementPage.tsx",
    "../src/features/nl2sql/pages/ProfileManagementPage.tsx",
    "../src/features/nl2sql/pages/SampleDataPage.tsx",
    "../src/features/nl2sql/pages/TableManagementPage.tsx",
    "../src/features/nl2sql/SyntheticReview.tsx",
    "../src/features/security/SecurityDeepSecPage.tsx",
    "../src/components/settings/SelectAiCredentialCard.tsx",
  ];
  for (const path of usages) {
    const text = source(path);
    const uiImport = text.match(/import \{[^}]*\} from "@production-ready\/ui";/u)?.[0] ?? "";
    assert.match(uiImport, /\bExecutionConfirmationField\b/u, path);
    assert.match(text, /<ExecutionConfirmationField\b/u, path);
  }
});

test("Drop object dialog does not wrap the confirmation field in a second danger surface", () => {
  const sourceText = source("../src/features/nl2sql/components/DbObjectManagementShared.tsx");
  const overlaySource = source("../src/components/ui/dialog-overlay.tsx");
  const component = sliceFrom(
    sourceText,
    "export function DropDbObjectDialog",
  );

  assert.doesNotMatch(sourceText, /fieldset className="grid gap-3 rounded-md border border-danger-border bg-danger-subtle\/70 p-3"/u);
  assert.match(component, /<DialogOverlayPortal className="p-3 sm:items-center">/u);
  assert.match(overlaySource, /createPortal/u);
  assert.match(overlaySource, /document\.body/u);
  assert.match(overlaySource, /fixed inset-0 z-\[var\(--z-dialog\)\]/u);
  assert.match(overlaySource, /bg-\[var\(--scrim\)\]/u);
  assert.match(component, /border border-border bg-surface-overlay shadow-\[var\(--shadow-dialog\)\]/u);
  assert.match(component, /border-b border-border bg-surface-overlay/u);
  assert.match(component, /border border-border bg-surface-sunken px-3 py-2/u);
  assert.match(component, /text-xs font-semibold text-fg/u);
  assert.match(component, /fieldset className="grid gap-3 rounded-md border border-border bg-surface-sunken p-3"/u);
  assert.match(component, /legend className="px-1 text-sm font-semibold text-fg"/u);
  assert.doesNotMatch(component, /border-l-4 border-l-danger/u);
  assert.doesNotMatch(component, /bg-danger-subtle/u);
});
