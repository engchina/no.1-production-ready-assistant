import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

const source = (path: string) => readFileSync(new URL(path, import.meta.url), "utf8");
const overlaySource = source("../src/components/ui/dialog-overlay.tsx");

test("ConfirmDialog / Toaster は共有パッケージを使い、アプリ内に再実装を持たない", () => {
  for (const path of ["confirm-dialog.tsx", "toaster.tsx", "select-field.tsx"]) {
    assert.equal(existsSync(new URL(`../src/components/ui/${path}`, import.meta.url)), false, path);
  }
  const main = source("../src/main.tsx");
  assert.match(main, /import \{ ConfirmProvider, Toaster \} from "@production-ready\/ui";/u);
  assert.doesNotMatch(main, /@\/components\/ui\/(?:confirm-dialog|toaster)/u);
});

test("ConfirmProvider には NL2SQL の文言とルート遷移の key を渡す（遷移で開いている確認をキャンセルする）", () => {
  const main = source("../src/main.tsx");
  assert.match(main, /labels=\{\{ confirm: t\("common\.confirm"\), cancel: t\("common\.cancel"\) \}\}/u);
  // 作業状態を URL に書き戻すだけの置き換え（同じパスの REPLACE）では閉じない共通の key を使う（#833）。
  assert.match(main, /const navigationKey = useConfirmNavigationKey\(\);/u);
  assert.match(main, /navigationKey=\{navigationKey\}/u);
  assert.doesNotMatch(main, /navigationKey=\{location\.key\}/u);
  // data router（#138）の splat route の中に ConfirmProvider を置く。
  assert.match(main, /function RootLayout\(\)[\s\S]*<AppConfirmProvider>[\s\S]*<\/AppConfirmProvider>/u);
  // 配信の前置き（`/nl2sql/` など）は router の basename で渡す（#1316）。
  assert.match(
    main,
    /createBrowserRouter\(\[\{ path: "\*", element: <RootLayout \/> \}\], \{\s*basename: ROUTER_BASENAME,\s*\}\)/u
  );
  assert.match(main, /<RouterProvider router=\{router\} \/>/u);
});

test("画面固有のモーダルは共有 z-dialog の暗幕を body 直下に出す", () => {
  assert.match(overlaySource, /createPortal/u);
  assert.match(overlaySource, /document\.body/u);
  assert.match(overlaySource, /bg-\[var\(--scrim\)\]/u);
  assert.match(overlaySource, /fixed inset-0 z-\[var\(--z-dialog\)\]/u);
  assert.match(overlaySource, /data-testid=\{testId\}/u);
  assert.doesNotMatch(overlaySource, /z-\[1000\]/u);
});
