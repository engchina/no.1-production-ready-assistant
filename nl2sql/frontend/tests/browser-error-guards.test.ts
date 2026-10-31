import assert from "node:assert/strict";
import test from "node:test";

import { isDevToolsWebVitalsStartTimeError } from "../src/lib/browser-error-guards.ts";

test("Chrome DevTools の web-vitals startTime 例外だけを識別する", () => {
  const error = new Error("Cannot read properties of undefined (reading 'startTime')");
  error.stack = [
    "TypeError: Cannot read properties of undefined (reading 'startTime')",
    "    at et.reportAllChanges (<anonymous>:2:19429)",
    "    at n.timeout (<anonymous>:2:5652)",
  ].join("\n");

  assert.equal(
    isDevToolsWebVitalsStartTimeError({
      message: "Uncaught TypeError: Cannot read properties of undefined (reading 'startTime')",
      filename: "VM327",
      error,
    }),
    true,
  );
});

test("通常のアプリ例外は DevTools web-vitals guard の対象にしない", () => {
  const appError = new Error("Cannot read properties of undefined (reading 'startTime')");
  appError.stack = [
    "TypeError: Cannot read properties of undefined (reading 'startTime')",
    "    at loadTableDetail (/assets/index.js:100:20)",
  ].join("\n");

  assert.equal(isDevToolsWebVitalsStartTimeError(appError), false);
  assert.equal(
    isDevToolsWebVitalsStartTimeError({
      message: "Cannot read properties of undefined (reading 'id')",
      filename: "VM327",
      error: { stack: "    at et.reportAllChanges (<anonymous>:2:19429)" },
    }),
    false,
  );
});
