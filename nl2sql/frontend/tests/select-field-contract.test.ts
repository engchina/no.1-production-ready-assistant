import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { extname } from "node:path";
import test from "node:test";

function sourceFiles(directory: URL): URL[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const child = new URL(`${entry.name}${entry.isDirectory() ? "/" : ""}`, directory);
    if (entry.isDirectory()) return sourceFiles(child);
    return [".ts", ".tsx"].includes(extname(entry.name)) ? [child] : [];
  });
}

test("SelectField は共有パッケージから import し、アプリ内に再実装を持たない", () => {
  assert.equal(existsSync(new URL("../src/components/ui/select-field.tsx", import.meta.url)), false);
  const users = sourceFiles(new URL("../src/", import.meta.url))
    .map((file) => readFileSync(file, "utf8"))
    .filter((text) => /<SelectField\b/u.test(text));
  assert.ok(users.length > 0);
  for (const text of users) {
    assert.match(text, /\bSelectField,[\s\S]*\} from "@production-ready\/ui";/u);
  }
  for (const file of sourceFiles(new URL("../src/", import.meta.url))) {
    assert.doesNotMatch(readFileSync(file, "utf8"), /@\/components\/ui\/(?:select-field|confirm-dialog|toaster)/u, file.pathname);
  }
});

test("共有 SelectField は一覧を Portal で描き、反転・typeahead・強調中の選択肢のスクロールを持つ（#352）", () => {
  const source = readFileSync(
    new URL("../../../platform/packages/ui/src/components/ui/select-field.tsx", import.meta.url),
    "utf8"
  );
  // 親の overflow に切られない（Portal）。位置と反転は FloatingActionMenu と同じ計算を共有する。
  assert.match(source, /createPortal\(/u);
  assert.match(source, /useFloatingMenuPosition\(\{[\s\S]*align: "stretch",[\s\S]*boundary: "viewport"/u);
  assert.match(source, /data-floating-menu-placement=\{position\?\.placement\}/u);
  assert.doesNotMatch(source, /absolute left-0 right-0 top-\[calc\(100%\+0\.25rem\)\]/u);
  // 外側クリックの判定に一覧（Portal 先）を含める。
  assert.match(source, /isInsideAny\(event\.target as Node \| null, \[rootRef\.current, listboxRef\.current\]\)/u);
  // typeahead（APG select-only combobox）と、強調中の選択肢のスクロール。
  assert.match(source, /findTypeaheadIndex\(/u);
  assert.match(source, /SELECT_TYPEAHEAD_RESET_MS = 500/u);
  assert.match(source, /listbox\.scrollTop = nearestScrollTop\(/u);
  // 既存の aria・id の互換。
  for (const pattern of [
    /role="combobox"/u,
    /aria-controls=\{listboxId\}/u,
    /aria-haspopup="listbox"/u,
    /aria-activedescendant=/u,
    /role="listbox"/u,
    /role="option"/u,
    /return `\$\{id\}-option-\$\{index\}`;/u,
  ]) {
    assert.match(source, pattern);
  }
});
