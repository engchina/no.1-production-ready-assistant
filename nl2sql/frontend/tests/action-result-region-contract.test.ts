import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const actionResultRegionSource = readFileSync(
  new URL("../../../platform/packages/ui/src/components/feedback/action-result-region.tsx", import.meta.url),
  "utf8",
);
import { readdirSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";

const srcRoot = new URL("../src/", import.meta.url).pathname;
function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? sourceFiles(join(dir, entry.name)) : entry.name.endsWith(".tsx") ? [join(dir, entry.name)] : []
  );
}

test("action result region keeps local actions from forcing page-top scroll", () => {
  assert.match(actionResultRegionSource, /export interface ActionResultRegionProps/u);
  assert.match(actionResultRegionSource, /preserveHeight = true/u);
  assert.match(actionResultRegionSource, /scrollPolicy = "nearest-on-complete"/u);
  assert.match(actionResultRegionSource, /setReservedMinHeight/u);
  assert.match(actionResultRegionSource, /minHeight: reservedMinHeight/u);
});

test("action result region does not render execution timing inside results", () => {
  assert.doesNotMatch(actionResultRegionSource, /TimedLoadingState/u);
  assert.doesNotMatch(actionResultRegionSource, /ProcessingIndicator/u);
  assert.match(actionResultRegionSource, /hasError \|\| hasChildren \|\| \(loading && reservedMinHeight > 0\)/u);
  assert.match(actionResultRegionSource, /aria-busy=\{loading \? "true" : undefined\}/u);
  assert.match(actionResultRegionSource, /\{loading \? null : hasError \?/u);
});

test("action result region can attach recovery actions to local errors", () => {
  assert.match(actionResultRegionSource, /errorAction\?: ReactNode/u);
  assert.match(actionResultRegionSource, /<Banner severity="danger" action=\{errorAction\}>/u);
});

test("action result region uses minimal result/error scroll guidance", () => {
  assert.match(actionResultRegionSource, /operation\.userScrolled/u);
  assert.match(actionResultRegionSource, /scrollIntoView\(\{/u);
  assert.match(actionResultRegionSource, /block: "nearest"/u);
  assert.match(actionResultRegionSource, /inline: "nearest"/u);
  assert.match(actionResultRegionSource, /prefers-reduced-motion: reduce/u);
});

test("共有 Button はブラウザ既定の type（submit）になるため、アプリの Button は type を明示する", () => {
  const missing: string[] = [];
  for (const file of sourceFiles(srcRoot)) {
    const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    const visit = (node: ts.Node) => {
      if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && node.tagName.getText(source) === "Button") {
        const hasType = node.attributes.properties.some(
          (attribute) => (ts.isJsxAttribute(attribute) && attribute.name.getText(source) === "type") || ts.isJsxSpreadAttribute(attribute)
        );
        if (!hasType) missing.push(`${file.slice(srcRoot.length)}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1}`);
      }
      ts.forEachChild(node, visit);
    };
    visit(source);
  }
  assert.deepEqual(missing, []);
});
