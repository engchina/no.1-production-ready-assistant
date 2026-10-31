import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const componentSource = readFileSync(
  new URL("../../../platform/packages/ui/src/components/ui/content-action-bar.tsx", import.meta.url),
  "utf8"
);
const dbObjectSource = readFileSync(
  new URL("../src/features/nl2sql/components/DbObjectManagementShared.tsx", import.meta.url),
  "utf8"
);
const generatedSqlSource = readFileSync(
  new URL("../src/features/nl2sql/components/GeneratedSqlPanel.tsx", import.meta.url),
  "utf8"
);
const settingsPreviewSource = readFileSync(
  new URL("../src/components/settings/SettingsPreviewPanels.tsx", import.meta.url),
  "utf8"
);
const viewManagementSource = readFileSync(
  new URL("../src/features/nl2sql/pages/ViewManagementPage.tsx", import.meta.url),
  "utf8"
);
const metadataSqlSource = readFileSync(
  new URL("../src/features/nl2sql/pages/MetadataSqlManagementPage.tsx", import.meta.url),
  "utf8"
);
const dataManagementSource = readFileSync(
  new URL("../src/features/nl2sql/pages/DataManagementPage.tsx", import.meta.url),
  "utf8"
);

test("ContentActionBar は内容内ツール操作を右寄せし、ARIA group を持つ", () => {
  assert.match(componentSource, /export function ContentActionBar/u);
  assert.match(componentSource, /ariaLabel/u);
  assert.match(componentSource, /role="group"/u);
  assert.match(componentSource, /aria-label=\{ariaLabel\}/u);
  assert.match(componentSource, /justify-end/u);
  assert.match(componentSource, /flex min-w-0 max-w-full flex-wrap items-start justify-between gap-\[8px\]/u);
  assert.doesNotMatch(componentSource, /sm:flex-row/u);
});

test("ContentActionBar は左側情報と右側操作を分離できる", () => {
  for (const prop of ["leading", "title", "description", "meta"]) {
    assert.match(componentSource, new RegExp(prop, "u"));
  }
  assert.match(componentSource, /const infoClassName = "min-w-0 max-w-full flex-1 basis-64"/u);
  assert.match(componentSource, /flex min-w-0 max-w-full shrink-0 flex-wrap items-center justify-end gap-\[8px\]/u);
  assert.match(componentSource, /hasInfo && "ml-auto"/u);
  assert.match(componentSource, /actionsClassName/u);
  assert.match(componentSource, /data-testid=\{testId\}/u);
});

test("DDL / SQL / preview の局所操作は ContentActionBar を使う", () => {
  // DDL の詳細は DbObjectManagementShared だけが持つ（DbAdminShared の使われていなかった詳細のパネルは #1891 で削除した）。
  assert.ok(dbObjectSource.includes('testId={`${idPrefix}-ddl-actions`}'));
  assert.match(generatedSqlSource, /testId="generated-sql-content-actions"/u);
  assert.match(settingsPreviewSource, /testId="settings-preview-actions"/u);
});

test("NL2SQL の局所実行 CTA は対象内容の後ろに置く", () => {
  assert.match(viewManagementSource, /testId="view-join-where-actions"/u);
  assert.match(viewManagementSource, /aria-describedby=\{ddlStatusId\}/u);
  assert.ok(
    viewManagementSource.indexOf('t("viewMgmt.joinWhere.extract")') >
      viewManagementSource.indexOf('data-testid="view-join-where-advanced-settings"')
  );

  assert.match(metadataSqlSource, /testId=\{`\$\{pageId\}-target-actions`\}/u);
  assert.match(metadataSqlSource, /testId=\{`\$\{pageId\}-input-actions`\}/u);
  assert.ok(
    // 情報の取得は対象の候補の一覧（ListPicker。Issue 608）の後ろ。
    metadataSqlSource.indexOf('t("metadataSql.action.fetchInfo")') >
      metadataSqlSource.indexOf('testId={`${pageId}-target`}')
  );
  assert.ok(
    metadataSqlSource.indexOf('t("metadataSql.action.generate")') >
      metadataSqlSource.indexOf('t("metadataSql.input.extra")')
  );

  assert.match(dataManagementSource, /testId="data-synthetic-refresh-tables-actions"/u);
  assert.match(dataManagementSource, /data-testid="data-synthetic-results-actions"/u);
  assert.doesNotMatch(dataManagementSource, /testId="data-synthetic-status-actions"/u);
  assert.doesNotMatch(dataManagementSource, /headingId="synthetic-status-heading"/u);
  assert.ok(
    dataManagementSource.indexOf('t("dataTools.syntheticData.refreshTables")') >
      dataManagementSource.indexOf('t("dataTools.syntheticData.selectedCount"')
  );
  assert.ok(
    dataManagementSource.indexOf('t("dataTools.syntheticData.refreshTables")') <
      dataManagementSource.indexOf('id="data-synthetic-table-picker"')
  );
  assert.ok(
    dataManagementSource.indexOf('t("dataTools.syntheticData.results")') >
      dataManagementSource.indexOf('value={syntheticResultLimitInput}')
  );
  assert.ok(
    dataManagementSource.indexOf('data-testid="synthetic-result-table-select"') <
      dataManagementSource.indexOf('value={syntheticResultLimitInput}')
  );

  const syntheticTargetHeaderStart = dataManagementSource.indexOf('headingId="synthetic-target-heading"');
  const syntheticTargetHeaderEnd = dataManagementSource.indexOf("<FormGrid>", syntheticTargetHeaderStart);
  const syntheticResultsHeaderStart = dataManagementSource.indexOf('headingId="synthetic-results-heading"');
  const syntheticResultsHeaderEnd = dataManagementSource.indexOf('<div className="border-t border-border pt-3"', syntheticResultsHeaderStart);
  assert.doesNotMatch(dataManagementSource.slice(syntheticTargetHeaderStart, syntheticTargetHeaderEnd), /action=\{/u);
  assert.doesNotMatch(dataManagementSource.slice(syntheticResultsHeaderStart, syntheticResultsHeaderEnd), /action=\{/u);
  assert.doesNotMatch(
    dataManagementSource.slice(syntheticResultsHeaderStart, dataManagementSource.indexOf('data-testid="data-synthetic-results-actions"', syntheticResultsHeaderStart)),
    /<ContentActionBar/u
  );
});

test("DDL パネルは copy/download ボタンを手書き左寄せ flex に戻さない", () => {
  const dbObjectCopyIndex = dbObjectSource.indexOf('t("dbAdmin.detail.copy")');
  assert.notEqual(dbObjectCopyIndex, -1);
  assert.doesNotMatch(
    dbObjectSource.slice(Math.max(0, dbObjectCopyIndex - 300), dbObjectCopyIndex),
    /flex flex-col gap-\[8px\] sm:flex-row sm:flex-wrap/u
  );
});
