import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const globalsSource = readFileSync(new URL("../src/globals.css", import.meta.url), "utf8");

// 色の値とコントラストは共有 UI パッケージ（platform の docs/design-system/）が正本。配色はライトの 1 種類だけ。
// NL2SQL の globals.css は styles.css を取り込み、色トークンもテーマの切り替え（.dark / data-theme / prefers-color-scheme）も持たない。
test("globals.css は共有 UI の styles.css を取り込み、色トークンとテーマの切り替えを定義しない", () => {
  assert.match(globalsSource, /@import "@production-ready\/ui\/styles\.css";/u);
  assert.doesNotMatch(globalsSource, /@import "@production-ready\/ui\/tokens\.css";/u);
  assert.doesNotMatch(globalsSource, /(?:^|\n)\s*(?:html)?\.dark\b/u);
  assert.doesNotMatch(globalsSource, /data-theme/u);
  assert.doesNotMatch(globalsSource, /prefers-color-scheme/u);
  assert.doesNotMatch(globalsSource, /--(?:background|foreground|card|border|primary[a-z-]*|muted|ring|sidebar[a-z-]*|success[a-z-]*|warning[a-z-]*|danger[a-z-]*|info[a-z-]*|control-border|disabled[a-z-]*|surface-muted-\d+|graph-[a-z]+)\s*:/u);
  assert.doesNotMatch(globalsSource, /#[0-9a-f]{3,8}\b/iu);
});

test("globals.css の画面固有レイアウトは共有トークン（--color-* / --button-height-*）だけを参照する", () => {
  const references = [...globalsSource.matchAll(/var\(--(?<name>[a-z0-9-]+)\)/gu)].map((match) => match.groups!.name);
  const disallowed = references.filter((name) => !/^(?:color-|button-height-(?:sm|md|lg)$|font-mono$|fixed-split-columns$)/u.test(name));
  assert.deepEqual(disallowed, []);
});
