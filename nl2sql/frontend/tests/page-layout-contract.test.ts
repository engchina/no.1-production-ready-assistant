import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { join, relative } from "node:path";
import test from "node:test";
import ts from "typescript";

// NL2SQL は全画面が画面幅いっぱい（PageHeader / PageBody の `wide`）。例外を作らない（#566）。
// PageHeader と PageBody の wide がずれると、広い画面でタイトルと本文の左端がずれる。
// カード内の中身も幅を止めず、フォームは grid の段組み、検索欄は toolbar の配分で広い画面を埋める（#575）。
const root = new URL("../src/", import.meta.url).pathname;
function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) =>
    entry.isDirectory() ? files(join(dir, entry.name)) : entry.name.endsWith(".tsx") ? [join(dir, entry.name)] : [],
  );
}

test("src のすべての PageHeader / PageBody が wide を持つ", () => {
  const violations: string[] = [];
  const counts = { PageHeader: 0, PageBody: 0 };
  for (const file of files(root)) {
    const path = relative(root, file);
    const source = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    function visit(node: ts.Node) {
      if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
        const tag = node.tagName.getText(source);
        if (tag === "PageHeader" || tag === "PageBody") {
          counts[tag] += 1;
          const wide = node.attributes.properties.find(
            (property): property is ts.JsxAttribute => ts.isJsxAttribute(property) && property.name.getText(source) === "wide",
          );
          // `wide` か `wide={true}` だけを許可する。条件式や `wide={false}` は画面ごとの例外になる。
          const literal = !wide?.initializer || wide.initializer.getText(source) === "{true}";
          if (!wide || !literal) {
            const { line } = source.getLineAndCharacterOfPosition(node.getStart(source));
            violations.push(`${path}:${line + 1} <${tag}> に wide がない`);
          }
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(source);
  }
  assert.deepEqual(violations, []);
  assert.ok(counts.PageHeader >= 20 && counts.PageBody >= 20, `検査対象が見つかる: ${JSON.stringify(counts)}`);
});

test("メモリモードの警告バナーも全幅で、直下の PageHeader と左端をそろえる", () => {
  const gate = readFileSync(new URL("../src/components/system/DatabaseGate.tsx", import.meta.url), "utf8");
  assert.match(gate, /<PageBody wide className="pb-0">/u);
});

test("回答前の確認の未入力案内は操作前に赤いエラー（role=alert）で出さない", () => {
  const workbench = readFileSync(new URL("../src/features/nl2sql/Nl2SqlWorkbench.tsx", import.meta.url), "utf8");
  assert.match(workbench, /<p id="nl2sql-guided-query-required" className="text-xs leading-5 text-fg-muted">/u);
  assert.doesNotMatch(workbench, /<FieldError[\s\S]{0,80}nl2sql-guided-query-required/u);
  assert.match(workbench, /const guidedClarificationHintId = !question\.trim\(\)\s+\? "nl2sql-guided-query-required"/u);
  assert.match(workbench, /aria-describedby=\{guidedClarificationHintId\}/u);
  // 質問が SQL の文でも押せる（#1727）ので、「回答前の確認は要りません」の案内は出さない。
  assert.doesNotMatch(workbench, /nl2sql-guided-sql-input/u);
});

test("wide のカード内の中身に最大幅を付けず、100% を使う（#575）", () => {
  // 広い画面ではコンテナを止めず、フォームや toolbar を grid の段組みで埋める。
  // 例外（ダイアログ・ポップオーバー本体、長文の max-w-prose、バッジ等の truncate）は対象の文字列に当たらない。
  const forbidden = [/READABLE_FORM_WIDTH/u, /form-layout/u, /max-w-\[var\(--content-max-width\)\]/u, /max-w-\[22rem\]/u, /max-w-\[34rem\]/u, /sm:max-w-md/u, /xl:max-w-3xl/u];
  const violations: string[] = [];
  for (const file of [...files(root), ...readdirSync(join(root, "lib")).map((name) => join(root, "lib", name))]) {
    const source = readFileSync(file, "utf8");
    for (const pattern of forbidden) if (pattern.test(source)) violations.push(`${relative(root, file)}: ${pattern}`);
  }
  assert.deepEqual(violations, []);
});
