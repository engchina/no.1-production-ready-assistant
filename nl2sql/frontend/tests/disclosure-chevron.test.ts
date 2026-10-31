import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../../../platform/packages/ui/src/components/ui/disclosure-chevron.tsx", import.meta.url),
  "utf8"
);

test("DisclosureChevron は折りたたみ時に右向き、展開時に下向きを表す（#397。共有 Sidebar と同じ向き）", () => {
  assert.match(source, /expanded\s+\?\s+"rotate-0"\s*:\s*"-rotate-90"/u);
  assert.match(source, /expanded \? "expanded" : "collapsed"/u);
  assert.doesNotMatch(source, /rotate-180/u);
});

test("DisclosureChevron の details モードは名前付き group と reduced-motion に対応する", () => {
  assert.match(source, /"-rotate-90 group-open\/disclosure:rotate-0"/u);
  assert.match(source, /motion-reduce:transition-none/u);
  assert.match(source, /expanded === "group" \? undefined/u);
  assert.match(source, /aria-hidden="true"/u);
  assert.match(source, /focusable="false"/u);
});
