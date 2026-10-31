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

test("data management CSV and synthetic guarded actions expose clear buttons", () => {
  const csvWorkspace = section(dataManagementPage, "function CsvUploadWorkspace", "function SyntheticWorkspace");
  const syntheticWorkspace = section(dataManagementPage, "function SyntheticWorkspace", "function syntheticTablePickerItem");

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
  // 生成条件のリセットは入力だけを戻す。表示中の結果（表示件数・結果をリセット）と生成状況（破棄）は対象外（#553）。
  const canClearSyntheticGeneration = section(dataManagementPage, "const canClearSyntheticGeneration = Boolean(", "  );");
  assert.match(canClearSyntheticGeneration, /syntheticSelectedTables[\s\S]*syntheticConfirmation/u);
  assert.doesNotMatch(canClearSyntheticGeneration, /syntheticDataResults|syntheticResultLimitInput|syntheticData \|\|/u);
  assert.match(
    syntheticWorkspace,
    /<ClearActionButton[\s\S]*label=\{t\("dataTools\.syntheticData\.actions\.clear"\)\}[\s\S]*!canClearSyntheticGeneration[\s\S]*onClick=\{onClearSyntheticGeneration\}/u,
  );
  const clearSyntheticGeneration = section(dataManagementPage, "const clearSyntheticGeneration = () => {", "\n  };");
  assert.match(
    clearSyntheticGeneration,
    /setSyntheticSelectedTables\(\[\]\);[\s\S]*setSyntheticPrompt\(""\);[\s\S]*setSyntheticConfirmation\(""\);[\s\S]*setSyntheticRows\(1\);/u,
  );
  assert.doesNotMatch(clearSyntheticGeneration, /clearSyntheticResultState|setSyntheticData\(null\)|setSyntheticResultTable/u);
});

test("business profile clear action resets only the Oracle execution gate and job state", () => {
  const editor = section(profileManagementPage, "function ProfileEditor", "function ProfileSaveResultRegion");
  const clearOracleExecution = section(
    profileManagementPage,
    "const clearOracleExecution = () => {",
    "  const editor = (",
  );

  assert.match(profileManagementPage, /import \{[^}]*\bClearActionButton\b[^}]*\} from "@production-ready\/ui"/u);
  assert.match(editor, /canClearOracleExecution: boolean/u);
  assert.match(editor, /onOracleExecutionClear: \(\) => void/u);
  assert.match(
    editor,
    /<ClearActionButton[\s\S]*label=\{t\("profiles\.oracle\.actions\.clear"\)\}[\s\S]*size="lg"[\s\S]*disabled=\{!canClearOracleExecution \|\| saving\}[\s\S]*onClick=\{onOracleExecutionClear\}/u,
  );
  assert.match(profileManagementPage, /const canClearOracleExecution = Boolean\([\s\S]*oracleConfirmation[\s\S]*syncJobParam/u);
  assert.match(
    profileManagementPage,
    /const clearOracleExecution = \(\) => \{[\s\S]*setOracleConfirmation\(""\);[\s\S]*setOracleSyncJobId\(""\);[\s\S]*nextParams\.delete\("syncJobId"\);/u,
  );
  assert.doesNotMatch(clearOracleExecution, /setProfileName\(""\)/u);
  // 「保存時に Select AI Agent アセットも再構築する」は業務プロファイルの設定で、リセットしない（#1690）。
  assert.doesNotMatch(clearOracleExecution, /setRebuildAgentAssets/u);
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
