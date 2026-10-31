import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { runTestFile } from "./run-unit-tests.mjs";

test("用例の実行・失敗・空ファイル・全件 skip を区別し、ファイル間の状態を分離する", () => {
  const temporaryRoot = fileURLToPath(new URL("../../../tmp/3193-3199/", import.meta.url));
  mkdirSync(temporaryRoot, { recursive: true });
  const directory = mkdtempSync(join(temporaryRoot, "runner-"));
  try {
    const run = (name, source) => {
      const file = join(directory, `${name}.test.ts`);
      writeFileSync(file, source);
      return runTestFile(file);
    };
    const passed = run("pass", `import test from 'node:test';
      import assert from 'node:assert/strict';
      test('具体的なケース A', () => { globalThis.runnerState = true; });
      test('具体的なケース B', () => assert.equal(2 + 2, 4));`);
    assert.equal(passed.ok, true);
    assert.equal(passed.tests, 2);
    assert.match(passed.output, /具体的なケース A/);
    assert.match(passed.output, /具体的なケース B/);
    assert.equal(run("state", `import test from 'node:test';
      import assert from 'node:assert/strict';
      test('状態は別プロセス', () => assert.equal(globalThis.runnerState, undefined));`).ok, true);
    assert.equal(run("fail", `import test from 'node:test';
      import assert from 'node:assert/strict';
      test('失敗を伝える', () => assert.fail('合成の失敗'));`).ok, false);
    assert.equal(run("empty", "export const value = 1;").ok, false);
    assert.equal(run("skip", `import test from 'node:test';
      test.skip('未実行', () => {});`).ok, false);
    assert.equal(run("broken", "const = ;").ok, false);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
