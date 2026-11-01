import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const dbAdminShared = readFileSync(
  new URL("../src/features/nl2sql/components/DbAdminShared.tsx", import.meta.url),
  "utf8",
);
const tableManagementPage = readFileSync(
  new URL("../src/features/nl2sql/pages/TableManagementPage.tsx", import.meta.url),
  "utf8",
);
const dataManagementPage = readFileSync(
  new URL("../src/features/nl2sql/pages/DataManagementPage.tsx", import.meta.url),
  "utf8",
);
const profileManagementPage = readFileSync(
  new URL("../src/features/nl2sql/pages/ProfileManagementPage.tsx", import.meta.url),
  "utf8",
);
const clearActionButton = readFileSync(
  new URL("../../../platform/packages/ui/src/components/ui/clear-action-button.tsx", import.meta.url),
  "utf8",
);
const fileDropzone = readFileSync(
  new URL("../src/components/ui/file-dropzone.tsx", import.meta.url),
  "utf8",
);

function section(source: string, start: string, end: string) {
  const startIndex = source.indexOf(start);
  const endIndex = source.indexOf(end, startIndex + start.length);
  assert.notEqual(startIndex, -1, `${start} が見つかりません`);
  assert.notEqual(endIndex, -1, `${end} が見つかりません`);
  return source.slice(startIndex, endIndex);
}

test("DDL/comment/annotation runners expose a shared clear action that resets guarded execution state", () => {
  // StatementRunnerCard はファイルの末尾（使われていなかった一覧・詳細のパネルは #1891 で削除した）。
  const runnerStart = dbAdminShared.indexOf("export function StatementRunnerCard");
  assert.notEqual(runnerStart, -1, "export function StatementRunnerCard が見つかりません");
  const runner = dbAdminShared.slice(runnerStart);

  assert.match(dbAdminShared, /import \{[^}]*\bClearActionButton\b[^}]*\} from "@production-ready\/ui"/u);
  assert.match(runner, /const canClearRunner = Boolean\(sql \|\| confirmation \|\| result \|\| message \|\| executionRun\)/u);
  assert.match(
    runner,
    /const clearRunner = \(\) => \{[\s\S]*setSql\(""\);[\s\S]*setConfirmation\(""\);[\s\S]*setResult\(null\);[\s\S]*setMessage\(""\);[\s\S]*setExecutionRun\(null\);[\s\S]*setSqlFileResetSignal/u,
  );
  assert.match(
    runner,
    /<ClearActionButton[\s\S]*label=\{t\("nl2sql\.action\.clearSql"\)\}[\s\S]*disabled=\{!canClearRunner \|\| loading\}[\s\S]*onClick=\{clearRunner\}/u,
  );
});

test("clear actions match the same-row height with size (no mouse-only 44px, #613)", () => {
  assert.doesNotMatch(clearActionButton, /matchButtonHeight/u);
  assert.doesNotMatch(clearActionButton, /touchTarget/u);
  assert.match(clearActionButton, /size = "sm"/u);
});

test("table import wizard clear action resets import form, result, and dropzone validation state", () => {
  const importWizard = section(tableManagementPage, "function ImportWizard", "function objectListErrorMessage");

  assert.match(tableManagementPage, /import \{[^}]*\bClearActionButton\b[^}]*\} from "@production-ready\/ui"/u);
  assert.match(importWizard, /resetSignal=\{fileResetSignal\}/u);
  assert.match(
    importWizard,
    /<ClearActionButton[\s\S]*label=\{t\("dbAdmin\.import\.actions\.clear"\)\}[\s\S]*disabled=\{!canClear \|\| loading\}[\s\S]*onClick=\{onClear\}/u,
  );
  assert.match(
    tableManagementPage,
    /const canClearImportWizard = Boolean\([\s\S]*importTable[\s\S]*importSheet[\s\S]*importFilename[\s\S]*importBase64[\s\S]*importConfirmation[\s\S]*importResult[\s\S]*importError[\s\S]*taskSchemaSync\.view/u,
  );
  assert.match(
    tableManagementPage,
    /const clearImportWizard = \(\) => \{[\s\S]*setImportTable\(""\);[\s\S]*setImportSheet\(""\);[\s\S]*setImportConfirmation\(""\);[\s\S]*clearImportFile\(\);[\s\S]*\};/u,
  );
});

test("data management CSV guarded action exposes a clear button", () => {
  const csvWorkspace = section(dataManagementPage, "function CsvUploadWorkspace", "function csvModeLabel");

  assert.match(dataManagementPage, /import \{[^}]*\bClearActionButton\b[^}]*\} from "@production-ready\/ui"/u);
  assert.match(csvWorkspace, /resetSignal=\{fileResetSignal\}/u);
  assert.match(
    csvWorkspace,
    /<ClearActionButton[\s\S]*label=\{t\("dbAdmin\.import\.actions\.clear"\)\}[\s\S]*disabled=\{!canClearUpload \|\| loading\}[\s\S]*onClick=\{onClearUpload\}/u,
  );
  assert.match(
    dataManagementPage,
    /const clearCsvUpload = \(\) => \{[\s\S]*clearCsvFile\(\);[\s\S]*setCsvMode\("insert"\);[\s\S]*setCsvConfirmation\(""\);[\s\S]*setCsvUploadError\(""\);[\s\S]*\};/u,
  );
});

test("business profile editor saves and discards with the shared FormActionBar", () => {
  const editor = section(profileManagementPage, "function ProfileEditor", "export function ProfileManagementPage");
  const discardChanges = section(profileManagementPage, "const discardChanges = () => {", "\n  };");

  assert.match(profileManagementPage, /import \{[^}]*\bFormActionBar\b[^}]*\} from "@production-ready\/ui"/u);
  assert.match(editor, /<FormActionBar[\s\S]*id: "save"[\s\S]*id: "discard"[\s\S]*disabled: !dirty \|\| saving/u);
  // 保存に確認語は要らない（業務プロファイルの保存は表への保存だけ）。
  assert.doesNotMatch(editor, /ExecutionConfirmationField|ADMIN_EXECUTE/u);
  assert.match(discardChanges, /setForm\(selectedProfile \? profileToForm\(selectedProfile\) : emptyProfileForm\(\)\)/u);
});

test("file dropzone reset signal clears local validation errors for external clear actions", () => {
  assert.match(fileDropzone, /resetSignal\?: string \| number/u);
  assert.match(fileDropzone, /resetSignal = 0/u);
  // reset のレンダーで表示を消し（effect で setState しない。#181）、ドラッグの深さは effect で戻す。
  assert.match(
    fileDropzone,
    /const resetRequested = useValuesChanged\(\[resetSignal\]\);[\s\S]*resetRequested\) setIsDragActive\(false\);[\s\S]*if \(resetRequested\) setValidationError\(""\);/u,
  );
  assert.match(fileDropzone, /useEffect\(\(\) => \{\s*dragDepthRef\.current = 0;\s*\}, \[resetSignal\]\);/u);
});
