import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { t } from "../src/lib/i18n";

/**
 * DB 管理の Oracle のエラーの案内（原因・次の操作）は i18n を通す（#1085）。
 * `oracleErrorGuidance` に日本語の文を直書きしない。
 */
const source = readFileSync(
  new URL("../src/features/nl2sql/components/DbAdminShared.tsx", import.meta.url),
  "utf8",
);

function guidanceSource() {
  const start = source.indexOf("function oracleErrorGuidance(");
  assert.notEqual(start, -1, "oracleErrorGuidance が見つかりません。");
  const end = source.indexOf("\nfunction ", start + 1);
  return source.slice(start, end === -1 ? undefined : end);
}

test("Oracle のエラーの案内に日本語の文を直書きしない", () => {
  const literals = [...guidanceSource().matchAll(/(["'`])((?:\\.|(?!\1).)*)\1/gu)].map((match) => match[2]);
  const japanese = literals.filter((value) => /[぀-ヿ㐀-鿿]/u.test(value));
  assert.deepEqual(japanese, [], "文言は src/lib/i18n.ts のキーにして t() で引いてください。");
});

test("Oracle のエラーの案内のキーが i18n にある", () => {
  const keys = [...guidanceSource().matchAll(/t\("([^"]+)"\)/gu)].map((match) => match[1]);
  assert.ok(keys.length > 0);
  for (const key of keys) {
    assert.notEqual(t(key), key, `${key} が i18n にありません。`);
  }
});
