import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { extname, relative } from "node:path";
import test from "node:test";

// 共有 UI の旧トークン名（compat.css は platform #44 で削除済み）を、文字列で組み立てた CSS 変数名としても使わない。
// `var(--card)` やユーティリティクラスの検査では、`cssVar("--card")` のような文字列の参照を検出できない。
const OLD_NAMES =
  /["'`(]--(?:background|foreground|card|muted|primary(?:-foreground|-fill(?:-foreground)?)?|danger(?:-bg|-fill)?|success(?:-bg|-fill)?|warning(?:-bg)?|info(?:-bg)?|border|ring|code(?:-fg)?|control-border|button-border|disabled(?:-bg)?|sidebar(?:-foreground|-active)?|graph-(?:entity|metric|sql|term|default|fg|line))["'`)]/u;

function files(dir: URL): URL[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const child = new URL(`${entry.name}${entry.isDirectory() ? "/" : ""}`, dir);
    if (entry.isDirectory()) return files(child);
    return [".ts", ".tsx", ".css"].includes(extname(entry.name)) ? [child] : [];
  });
}

test("src と tests に旧トークン名の CSS 変数を文字列で書かない", () => {
  const root = new URL("..", import.meta.url);
  const self = new URL(import.meta.url).pathname;
  const violations = [new URL("../src/", import.meta.url), new URL("./", import.meta.url)]
    .flatMap(files)
    .filter((url) => url.pathname !== self)
    .flatMap((url) =>
      readFileSync(url, "utf8")
        .split("\n")
        .flatMap((line, index) => (OLD_NAMES.test(line) ? [`${relative(root.pathname, url.pathname)}:${index + 1}`] : []))
    );
  assert.deepEqual(violations, []);
});
