import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(
  // 共通部品（packages/ui）へ移した（#226）。
  new URL("../../../platform/packages/ui/src/components/ui/form-action-bar.tsx", import.meta.url),
  "utf8"
);
const floatingSource = readFileSync(
  new URL("../../../platform/packages/ui/src/components/ui/floating-menu.tsx", import.meta.url),
  "utf8"
);
const menuFocusSource = readFileSync(
  new URL("../../../platform/packages/ui/src/lib/menu-focus.ts", import.meta.url),
  "utf8"
);

test("FormActionBar は primary / secondary / danger を descriptor で分離する", () => {
  assert.match(source, /export interface FormActionDescriptor/u);
  assert.match(source, /export interface FormActionBarProps/u);
  assert.match(source, /primaryActions/u);
  assert.match(source, /secondaryActions/u);
  assert.match(source, /dangerActions/u);
  assert.ok(source.indexOf("primaryActions.map") < source.indexOf("secondaryActions.map"));
});

test("FormActionBar は danger を通常の赤ボタンとして直置きしない", () => {
  assert.doesNotMatch(source, /variant="danger"/u);
  assert.match(source, /data-form-action-tone="danger"/u);
  assert.match(source, /tone=/u);
  assert.match(source, /border-t border-border pt-1/u);
  assert.match(source, /moreLabel = "その他の操作"/u);
});

test("FormActionBar の danger menu は ARIA とキーボード契約を持つ", () => {
  assert.match(source, /aria-haspopup="menu"/u);
  assert.match(source, /aria-expanded=\{open\}/u);
  assert.match(source, /aria-controls=\{menuId\}/u);
  assert.match(floatingSource, /role="menu"/u);
  assert.match(source, /role="menuitem"/u);
  for (const key of ["Escape", "ArrowDown", "ArrowUp", "Home", "End"]) {
    assert.match(source, new RegExp(`event\\.key === "${key}"`, "u"));
  }
  assert.match(source, /firstEnabled\?\.focus\(\{ preventScroll: true \}\)/u);
  // フォーカス復帰は共有 helper が担う(閉じた直後に別要素へ移っていたら奪い返さない)。
  assert.match(source, /restoreMenuTriggerFocus\(triggerRef, menuRef\)/u);
  assert.match(menuFocusSource, /triggerRef\.current\?\.focus\(\{ preventScroll: true \}\)/u);
  assert.match(menuFocusSource, /active !== document\.body/u);
  assert.match(source, /items\[nextIndex\]\?\.focus\(\{ preventScroll: true \}\)/u);
  assert.match(source, /<DisclosureChevron expanded=\{open\} size=\{16\} \/>/u);
  assert.doesNotMatch(source, /open && "rotate-180"/u);
});

test("FormActionBar の danger menu は shared floating menu で viewport 内に配置する", () => {
  assert.match(source, /FloatingActionMenu/u);
  assert.doesNotMatch(source, /absolute right-0 top-full/u);
  assert.match(source, /menuRef\.current\?\.contains\(target\)/u);
  assert.match(floatingSource, /createPortal/u);
  assert.match(floatingSource, /data-floating-menu-placement/u);
  assert.match(floatingSource, /data-floating-menu-constrained/u);
  assert.match(floatingSource, /availableBelow/u);
  assert.match(floatingSource, /availableAbove/u);
  assert.match(floatingSource, /getBoundingClientRect/u);
  assert.match(floatingSource, /menu\.scrollHeight \+ menuBorderHeight/u);
  assert.match(floatingSource, /constrained \? \{ maxHeight/u);
  assert.match(floatingSource, /position\?\.constrained && "overflow-y-auto overscroll-contain(?: [^"]*)?"/u);
  assert.doesNotMatch(floatingSource, /"fixed[^"]*overflow-y-auto/u);
});

test("FormActionBar は mobile で全幅にし、主操作の高さは共通 Button に委譲する", () => {
  assert.match(source, /w-full whitespace-nowrap sm:w-auto/u);
  assert.doesNotMatch(source, /sm:h-10/u);
  assert.match(source, /flex-col gap-\[8px\] sm:flex-row sm:flex-wrap sm:items-center"/u);
  // 詳細・作成・編集の画面の確定の行も画面の下端に貼り付けない（#2715。#1786 の sticky と 1 行の等分の並びを削除）。
  assert.doesNotMatch(source, /\bsticky\b|bottom-0|z-\[var\(--z-/u);
  assert.doesNotMatch(source, /"min-w-0 flex-1 whitespace-nowrap sm:flex-none"/u);
});
