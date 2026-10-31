import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const globalsSource = readFileSync(new URL("../src/globals.css", import.meta.url), "utf8");

// 色・テーマ（ライト / ダーク）の値とコントラストは共有 UI パッケージ（platform の docs/design-system/）が正本。
// NL2SQL の globals.css は styles.css を取り込み、色トークンやダーク上書きを持たない。
test("globals.css は共有 UI の styles.css を取り込み、色トークンと .dark 上書きを定義しない", () => {
  assert.match(globalsSource, /@import "@production-ready\/ui\/styles\.css";/u);
  assert.doesNotMatch(globalsSource, /@import "@production-ready\/ui\/tokens\.css";/u);
  assert.doesNotMatch(globalsSource, /(?:^|\n)\s*\.dark\b/u);
  assert.doesNotMatch(globalsSource, /--(?:background|foreground|card|border|primary[a-z-]*|muted|ring|sidebar[a-z-]*|success[a-z-]*|warning[a-z-]*|danger[a-z-]*|info[a-z-]*|control-border|disabled[a-z-]*|surface-muted-\d+|graph-[a-z]+)\s*:/u);
  assert.doesNotMatch(globalsSource, /#[0-9a-f]{3,8}\b/iu);
});

test("globals.css の画面固有レイアウトは共有トークン（--color-* / --button-height-*）だけを参照する", () => {
  const references = [...globalsSource.matchAll(/var\(--(?<name>[a-z0-9-]+)\)/gu)].map((match) => match.groups!.name);
  const disallowed = references.filter((name) => !/^(?:color-|button-height-(?:sm|md|lg)$|font-mono$|fixed-split-columns$)/u.test(name));
  assert.deepEqual(disallowed, []);
});
