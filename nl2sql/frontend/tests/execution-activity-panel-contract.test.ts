import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const panelSource = readFileSync(
  new URL("../src/components/ExecutionActivityPanel.tsx", import.meta.url),
  "utf8",
);
const i18nSource = readFileSync(new URL("../src/lib/i18n.ts", import.meta.url), "utf8");

test("execution activity panel owns execution status and elapsed timing", () => {
  assert.match(panelSource, /export function ExecutionActivityPanel/u);
  assert.match(panelSource, /export interface ExecutionActivityPanelProps/u);
  assert.match(panelSource, /useOperationTiming/u);
  assert.match(panelSource, /role="status"/u);
  assert.match(panelSource, /aria-atomic="true"/u);
  assert.match(panelSource, /role="timer"/u);
  assert.match(panelSource, /aria-live="off"/u);
  assert.match(panelSource, /data-execution-activity-status/u);
  assert.match(panelSource, /data-testid=\{testId\}/u);
});

test("execution activity panel is not a second animated loading indicator", () => {
  assert.doesNotMatch(panelSource, /Loader2/u);
  assert.doesNotMatch(panelSource, /animate-spin/u);
  assert.match(panelSource, /Clock3/u);
  assert.match(panelSource, /CheckCircle2/u);
  assert.match(panelSource, /CircleAlert/u);
});

test("execution activity panel does not expose result summary or statement steps", () => {
  assert.doesNotMatch(panelSource, /summaryItems/u);
  assert.doesNotMatch(panelSource, /ExecutionActivitySummaryItem/u);
  assert.doesNotMatch(panelSource, /ExecutionActivityStep/u);
  assert.doesNotMatch(panelSource, /data-testid=\{testId \? `\$\{testId\}-summary`/u);
  assert.doesNotMatch(panelSource, /data-testid=\{testId \? `\$\{testId\}-steps`/u);
  assert.doesNotMatch(panelSource, /executionActivity\.steps\.title/u);
});

test("execution activity panel has localized labels", () => {
  for (const key of [
    "executionActivity.title",
    "executionActivity.status.running",
    "executionActivity.status.success",
    "executionActivity.status.error",
    "executionActivity.execute.success",
    "executionActivity.execute.error",
  ]) {
    assert.match(i18nSource, new RegExp(`"${key}"`, "u"));
  }
  assert.doesNotMatch(i18nSource, /"executionActivity\.summary\./u);
  assert.doesNotMatch(i18nSource, /"executionActivity\.steps\.title"/u);
  assert.doesNotMatch(i18nSource, /"executionActivity\.step\./u);
  assert.doesNotMatch(i18nSource, /"executionActivity\.statement\./u);
});
