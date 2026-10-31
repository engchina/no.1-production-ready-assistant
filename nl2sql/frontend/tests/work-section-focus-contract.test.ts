import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  new URL("../src/features/nl2sql/components/DbAdminShared.tsx", import.meta.url),
  "utf8"
);
const dbObjectSource = readFileSync(
  new URL("../src/features/nl2sql/components/DbObjectManagementShared.tsx", import.meta.url),
  "utf8"
);

function sliceBetween(text: string, startMarker: string, endMarker: string): string {
  const start = text.indexOf(startMarker);
  assert.notEqual(start, -1);
  const end = text.indexOf(endMarker, start + startMarker.length);
  assert.notEqual(end, -1);
  return text.slice(start, end);
}

const workSection = sliceBetween(
  source,
  "export function WorkSection",
  "export function ManagementPanelShell"
);

test("WorkSection は共有の Disclosure で開閉し、フォーカスの表示は packages/ui に任せる（#355 / #397）", () => {
  assert.match(workSection, /<Disclosure\b/u);
  assert.match(workSection, /tone=\{tone\}/u);
  // summary を手書きしない（Chevron・押せる領域・フォーカスの表示は Disclosure が持つ）。
  assert.doesNotMatch(workSection, /<summary\b/u);
  assert.doesNotMatch(workSection, /focus(?:-visible)?:ring/u);
  assert.doesNotMatch(workSection, /focus(?:-visible)?:outline-none/u);
});

test("DbAdminShared は details / summary を手書きしない（#397）", () => {
  assert.doesNotMatch(source, /<details\b/u);
  assert.doesNotMatch(source, /<summary\b/u);
  assert.match(source, /<Disclosure\b/u);
});

test("管理画面のタブは共有 Tabs（WAI-ARIA Tabs のキー操作とフォーカス移動）に委ねる", () => {
  assert.match(source, /export function ManagementTabs/u);
  assert.match(source, /<Tabs\b/u);
  assert.match(dbObjectSource, /export function DbObjectManagementTabs/u);
  assert.match(dbObjectSource, /<Tabs\b/u);
  assert.doesNotMatch(source, /role="tab"/u);
  assert.doesNotMatch(dbObjectSource, /role="tab"/u);
});
