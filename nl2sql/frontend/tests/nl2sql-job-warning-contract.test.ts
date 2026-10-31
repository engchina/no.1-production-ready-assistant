import { readFileSync } from "node:fs";
import assert from "node:assert/strict";
import test from "node:test";

const typesSource = readFileSync(
  new URL("../src/features/nl2sql/types.ts", import.meta.url),
  "utf8"
);
const pollingSource = readFileSync(
  new URL("../src/features/nl2sql/useNl2SqlJobPolling.ts", import.meta.url),
  "utf8"
);
const statusStripSource = readFileSync(
  new URL("../src/features/nl2sql/components/OperationStatusStrip.tsx", import.meta.url),
  "utf8"
);
const i18nSource = readFileSync(new URL("../src/lib/i18n.ts", import.meta.url), "utf8");

test("NL2SQL job data carries persistence warnings without converting them to errors", () => {
  assert.match(typesSource, /warning_message\?: string \| null;/);
  assert.match(pollingSource, /warning_message: null/);
  assert.match(statusStripSource, /const warningMessage = job\.warning_message\?\.trim\(\);/);
});

test("operation status strip renders done warnings separately from red job errors", () => {
  assert.match(
    statusStripSource,
    /const errorMessage = job\.status === "error" \? job\.error_message\?\.trim\(\) : "";/,
  );
  assert.equal(statusStripSource.includes("{job.error_message && ("), false);
  assert.match(statusStripSource, /<Banner severity="warning"/);
  assert.match(statusStripSource, /<Banner[\s\S]*severity="danger"/);
  assert.match(statusStripSource, /<ChatProgress[\s\S]*active=\{active\}/);
  assert.doesNotMatch(statusStripSource, /role=\{job\.status === "error" \? "alert"/);
  assert.match(i18nSource, /nl2sql\.progress\.persistenceWarning/);
});
