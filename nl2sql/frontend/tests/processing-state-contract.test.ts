import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const processingSource = readFileSync(
  new URL("../../../platform/packages/ui/src/components/feedback/processing-state.tsx", import.meta.url),
  "utf8",
);
const managementShellSource = readFileSync(
  new URL("../src/features/nl2sql/components/DbObjectManagementShared.tsx", import.meta.url),
  "utf8",
);
const stateViewsSource = readFileSync(
  new URL("../src/components/StateViews.tsx", import.meta.url),
  "utf8",
);

test("shared processing state exposes the common hook and loading components", () => {
  assert.match(processingSource, /export function useOperationTiming/u);
  assert.match(processingSource, /export function ProcessingIndicator/u);
  assert.match(processingSource, /export function TimedLoadingState/u);
  assert.match(processingSource, /export type ProcessingPlacement/u);
  // "static" は回さない実行中の印（同じ処理のスピナーを別の場所が回している間。#3445）。
  assert.match(processingSource, /export type ProcessingActivityIcon = "spinner" \| "static" \| "none"/u);
  assert.match(processingSource, /DEFAULT_SLOW_AFTER_MS = 10_000/u);
});

test("processing placement is semantic and exposed for UI verification", () => {
  for (const placement of [
    "page",
    "workspace",
    "panel",
    "tab",
    "result",
    "action",
    "job",
  ]) {
    assert.match(processingSource, new RegExp(`"${placement}"`, "u"));
  }
  assert.match(processingSource, /placement\?: ProcessingPlacement/u);
  assert.match(processingSource, /aria-busy=\{timing\.active\}/u);
  assert.match(processingSource, /data-processing-placement=\{placement\}/u);
  assert.match(processingSource, /data-processing-activity-icon=\{activityIcon\}/u);
  assert.match(managementShellSource, /processing\?: ReactNode/u);
  assert.match(managementShellSource, /topContent\?: ReactNode/u);
  assert.match(managementShellSource, /aria-busy=\{processing \? true : undefined\}/u);
  assert.match(managementShellSource, /\{processing\}\s*\{topContent\}\s*\{splitPaneId \?/u);
});

test("processing activity icon defaults protect result areas from duplicate spinners", () => {
  assert.match(processingSource, /activityIcon\?: ProcessingActivityIcon/u);
  assert.match(processingSource, /activityIcon = "spinner"/u);
  assert.match(processingSource, /const effectiveActivityIcon = activityIcon \?\? \(placement === "result" \? "none" : "spinner"\)/u);
  assert.match(processingSource, /const showActivityIcon = activityIcon === "spinner"/u);
  assert.match(processingSource, /timing\.active && showActivityIcon/u);
  assert.match(processingSource, /timing\.active && activityIcon === "static"/u);
  assert.match(processingSource, /!timing\.active && activityIcon !== "none"/u);
  assert.match(processingSource, /activityIcon=\{effectiveActivityIcon\}/u);
  assert.doesNotMatch(stateViewsSource, /activityIcon = "spinner"/u);
  assert.match(stateViewsSource, /placement = "panel",\s*activityIcon,/u);
});

test("operation timer resets on operation changes and recovers from background throttling", () => {
  assert.match(processingSource, /localStartRef\.current\.key !== operationKey/u);
  assert.match(processingSource, /!wasActiveRef\.current/u);
  assert.match(processingSource, /document\.addEventListener\("visibilitychange", update\)/u);
  assert.match(processingSource, /setNowMs\(Date\.now\(\)\)/u);
});

test("processing state avoids noisy announcements and uses the shared spinner", () => {
  assert.match(processingSource, /role="timer"/u);
  assert.match(processingSource, /aria-live="off"/u);
  // スピナーは共有 Spinner（reduced motion でも回転を止めない。#440）
  assert.match(processingSource, /<Spinner size=\{16\}/u);
  assert.doesNotMatch(processingSource, /Loader2/u);
  assert.match(processingSource, /tabular-nums/u);
});
