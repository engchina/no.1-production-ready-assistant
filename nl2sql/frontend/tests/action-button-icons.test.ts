import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import ts from "typescript";

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const path = join(directory, entry.name);
    return entry.isDirectory() ? sourceFiles(path) : entry.name.endsWith(".tsx") ? [path] : [];
  });
}

test("非同期Buttonは共通のアイコンスロットを使いloading前後の幅を保つ", () => {
  const failures: string[] = [];
  for (const file of sourceFiles(new URL("../src", import.meta.url).pathname)) {
    const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    function visit(node: ts.Node): void {
      if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && node.tagName.getText(source) === "Button") {
        const attributes = new Set(node.attributes.properties.filter(ts.isJsxAttribute).map(attribute => attribute.name.getText(source)));
        if (attributes.has("loading") && !attributes.has("icon") && !attributes.has("trailingIcon")) {
          failures.push(`${file}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1}: icon prop が必要です`);
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(source);
  }
  assert.deepEqual(failures, []);
});
