import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../../../platform/packages/ui/src/components/ui/bulk-selection-actions.tsx", import.meta.url),
  "utf8"
);

const migratedPages = [
  "../src/features/nl2sql/pages/ProfileManagementPage.tsx",
  "../src/features/nl2sql/pages/QuestionLearningPage.tsx",
  "../src/features/nl2sql/pages/DataManagementPage.tsx",
  "../src/features/nl2sql/pages/MetadataSqlManagementPage.tsx",
  "../src/features/nl2sql/pages/EvaluationPage.tsx",
  "../../../platform/packages/system-settings/src/permissions/RolePermissionsPage.tsx",
  "../src/features/security/SecurityDeepSecPage.tsx",
].map((path) => readFileSync(new URL(path, import.meta.url), "utf8"));

test("BulkSelectionActions uses shared Button variants for select and clear", () => {
  assert.match(source, /from "\.\/button"/u);
  assert.match(source, /variant="secondary"/u);
  assert.match(source, /variant="ghost"/u);
  assert.match(source, /size = "sm"/u);
  assert.match(source, /justify-start/u);
  assert.match(source, /disabled=\{busy \|\| selectDisabled\}/u);
  assert.match(source, /disabled=\{busy \|\| clearDisabled\}/u);
});

test("BulkSelectionActions exposes distinct aria labels and stable test ids", () => {
  assert.match(source, /role="group"/u);
  assert.match(source, /aria-busy=\{busy \|\| undefined\}/u);
  assert.match(source, /aria-label=\{selectAriaLabel \?\? selectLabel\}/u);
  assert.match(source, /aria-label=\{clearAriaLabel \?\? clearLabel\}/u);
  assert.match(source, /\$\{dataTestId\}-select/u);
  assert.match(source, /\$\{dataTestId\}-clear/u);
});

test("bulk selection surfaces use the shared component", () => {
  for (const page of migratedPages) {
    // 大量の候補から選ぶ一覧は共通の ListPicker（グループの一括選択に BulkSelectionActions を使う。#600）。
    assert.match(page, /BulkSelectionActions|<ListPicker\b/u);
    assert.doesNotMatch(
      page,
      /<BulkSelectionActions[^>]*className="[^"]*(?:ml-auto|justify-end)/su
    );
  }
});
