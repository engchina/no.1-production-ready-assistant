import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../src/features/security/SecurityDeepSecPage.tsx", import.meta.url),
  "utf8"
);

function sliceBetween(text: string, startMarker: string, endMarker: string): string {
  const start = text.indexOf(startMarker);
  assert.notEqual(start, -1);
  const end = text.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1);
  return text.slice(start, end);
}

const foundationPanel = sliceBetween(
  source,
  '{activeView === "foundation"',
  '{activeView === "data-permissions"'
);
const planStepsComponent = sliceBetween(
  source,
  "function DeepSecPlanSteps",
  "export function SecurityDeepSecPage"
);
const dataPermissionsPanel = source.slice(source.indexOf('{activeView === "data-permissions"'));

test("DeepSec reset は基盤構成 header の danger action として表示しない", () => {
  const headerStart = foundationPanel.indexOf("<ManagementPanelHeader");
  assert.notEqual(headerStart, -1);
  const headerEnd = foundationPanel.indexOf("/>", headerStart);
  assert.notEqual(headerEnd, -1);
  const headerSource = foundationPanel.slice(headerStart, headerEnd);

  assert.doesNotMatch(headerSource, /action=\{/u);
  assert.doesNotMatch(headerSource, /security\.deepsec\.reset/u);
});

test("DeepSec reset は実行計画 step の後ろに折りたたみ danger section として置く", () => {
  const planTitleIndex = foundationPanel.indexOf("security-deepsec-foundation-plan-title");
  const stepsIndex = foundationPanel.indexOf("<DeepSecPlanSteps", planTitleIndex);
  const applySectionIndex = foundationPanel.indexOf('dataTestId="security-deepsec-foundation-apply-section"');
  const resetSectionIndex = foundationPanel.indexOf('dataTestId="security-deepsec-reset-section"');

  assert.notEqual(planTitleIndex, -1);
  assert.notEqual(stepsIndex, -1);
  assert.notEqual(applySectionIndex, -1);
  assert.notEqual(resetSectionIndex, -1);
  assert.ok(applySectionIndex > stepsIndex);
  assert.ok(resetSectionIndex > applySectionIndex);
  assert.match(foundationPanel, /<WorkSection/u);
  assert.match(foundationPanel, /tone="danger"/u);
});

test("DeepSec foundation apply は step card 内ではなく外側 neutral section に置く", () => {
  assert.doesNotMatch(planStepsComponent, /ExecutionConfirmationField/u);
  assert.doesNotMatch(planStepsComponent, /confirmations/u);
  assert.doesNotMatch(planStepsComponent, /onApply/u);
  assert.doesNotMatch(planStepsComponent, /security\.deepsec\.apply/u);

  const applyDataTestIdIndex = foundationPanel.indexOf('dataTestId="security-deepsec-foundation-apply-section"');
  assert.notEqual(applyDataTestIdIndex, -1);
  const applySectionIndex = foundationPanel.lastIndexOf("<WorkSection", applyDataTestIdIndex);
  assert.notEqual(applySectionIndex, -1);
  const applyOpeningSource = foundationPanel.slice(applySectionIndex, foundationPanel.indexOf(">", applyDataTestIdIndex) + 1);
  const applySectionSource = foundationPanel.slice(
    applySectionIndex,
    foundationPanel.indexOf("</WorkSection>", applyDataTestIdIndex)
  );

  assert.match(applySectionSource, /security\.deepsec\.applySectionTitle/u);
  assert.match(applySectionSource, /ExecutionConfirmationField/u);
  assert.doesNotMatch(applyOpeningSource, /tone="danger"/u);
});

test("DeepSec reset confirmation は大きな赤い外枠で実行計画前に常設しない", () => {
  assert.doesNotMatch(foundationPanel, /rounded-md border border-danger-border bg-danger-subtle p-3/u);

  const planTitleIndex = foundationPanel.indexOf("security-deepsec-foundation-plan-title");
  const resetConfirmationIndex = foundationPanel.indexOf("ADMIN_RESET_CONFIRMATION");

  assert.notEqual(planTitleIndex, -1);
  assert.notEqual(resetConfirmationIndex, -1);
  assert.ok(resetConfirmationIndex > planTitleIndex);
});

test("DeepSec データ権限は standalone 実行計画ではなく統合 editor で適用する", () => {
  assert.doesNotMatch(dataPermissionsPanel, /security-deepsec-permissions-plan-title/u);
  assert.doesNotMatch(dataPermissionsPanel, /stepNumbers=\{\[3,\s*4\]\}/u);
  assert.match(dataPermissionsPanel, /security\.deepsec\.entitlements\.sqlPreview/u);
  assert.match(source, /applyDeepSecDataEntitlements/u);
  assert.match(source, /deepSecTargetObjects/u);
});
