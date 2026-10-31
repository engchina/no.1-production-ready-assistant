import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  dbObjectKeyTokens,
  formatDbObjectName,
  formatDbObjectPart,
  normalizeDbIdentifierToken,
  normalizeDbObjectKey,
  splitDbObjectName,
  sqlIdentifierToken,
} from "../src/features/nl2sql/dbObjectIdentity.ts";
import {
  objectMatches,
  objectIdentityFromNode,
  objectName,
  ontologyPhysicalNodeLabel,
} from "../src/features/nl2sql/ontology/physicalIdentity.ts";
import { ontologyGraphObjectClusterKey } from "../src/features/nl2sql/ontology/graphLayout.ts";
import { schemaTableQualifiedName } from "../src/features/nl2sql/workbenchState.ts";

test("formatDbObjectName は OWNER.OBJECT を組み立て、Oracle の規則外の部分だけを引用する", () => {
  assert.equal(formatDbObjectName({ owner: "ADMIN", name: "DENPYO_ACTIVITY_LOG" }), "ADMIN.DENPYO_ACTIVITY_LOG");
  assert.equal(formatDbObjectName({ owner: "APP", name: "T$1#" }), "APP.T$1#");
  // カタログ値は大文字化しない（小文字・日本語・空白・先頭数字は引用識別子）
  assert.equal(formatDbObjectName({ owner: "APP", name: "lower" }), 'APP."lower"');
  assert.equal(formatDbObjectName({ owner: "APP", name: "MixedCase" }), 'APP."MixedCase"');
  assert.equal(formatDbObjectName({ owner: "APP", name: "売上" }), 'APP."売上"');
  assert.equal(formatDbObjectName({ owner: "Mixed Owner", name: "1ST" }), '"Mixed Owner"."1ST"');
  assert.equal(formatDbObjectName({ owner: "APP", name: 'A"B' }), 'APP."A""B"');
});

test("formatDbObjectName は owner が無いとき名前だけを返し、推測で補わない", () => {
  assert.equal(formatDbObjectName({ name: "ORDERS" }), "ORDERS");
  assert.equal(formatDbObjectName({ owner: "", name: "ORDERS" }), "ORDERS");
  assert.equal(formatDbObjectName({ owner: null, name: "ORDERS" }), "ORDERS");
  assert.equal(formatDbObjectName({ owner: "  ", name: "orders" }), '"orders"');
  assert.equal(formatDbObjectName({ owner: "APP", name: "" }), "");
});

test("formatDbObjectName は qualified_name を優先し、正規化した形で返す", () => {
  assert.equal(
    formatDbObjectName({ owner: "OTHER", name: "IGNORED", qualified_name: "APP.ORDERS" }),
    "APP.ORDERS"
  );
  assert.equal(formatDbObjectName({ name: "x", qualified_name: 'APP."売上"' }), 'APP."売上"');
  assert.equal(formatDbObjectName({ name: "x", qualified_name: '"Mixed Owner"."売上.2026"' }), '"Mixed Owner"."売上.2026"');
  // 空白だけの qualified_name は無視して owner / name から組み立てる
  assert.equal(formatDbObjectName({ owner: "APP", name: "ORDERS", qualified_name: "  " }), "APP.ORDERS");
});

test("formatDbObjectName は表示用のため壊れた引用でも例外を投げない", () => {
  assert.equal(formatDbObjectName({ name: "x", qualified_name: 'APP."BROKEN' }), 'APP."BROKEN');
  assert.equal(formatDbObjectName({ owner: "APP", name: '"BROKEN' }), 'APP."BROKEN');
});

test("formatDbObjectName は backend の qualified_object_name と同じ規則で組み立てる", () => {
  // backend/tests の object_identity と同じ入出力（format_object_part はカタログ値を大文字化しない）
  const cases: Array<[string, string, string]> = [
    ["APP", "ORDERS", "APP.ORDERS"],
    ["APP", "orders", 'APP."orders"'],
    ["APP", "売上", 'APP."売上"'],
    ["APP", "A B", 'APP."A B"'],
    ["APP", "_X", 'APP."_X"'],
  ];
  for (const [owner, name, expected] of cases) {
    assert.equal(formatDbObjectName({ owner, name }), expected);
  }
});

test("formatDbObjectPart はカタログ値を token にし、token には冪等", () => {
  assert.equal(formatDbObjectPart("Mixed_Case"), '"Mixed_Case"');
  assert.equal(formatDbObjectPart('"Mixed_Case"'), '"Mixed_Case"');
  assert.equal(formatDbObjectPart("ORDERS"), "ORDERS");
  assert.equal(formatDbObjectPart("売上"), '"売上"');
  assert.equal(formatDbObjectPart(""), "");
});

test("normalizeDbIdentifierToken は Oracle の非引用識別子だけを大文字にする", () => {
  assert.equal(normalizeDbIdentifierToken("amount"), "AMOUNT");
  assert.equal(normalizeDbIdentifierToken('"ORDERS"'), "ORDERS");
  assert.equal(normalizeDbIdentifierToken('"Amount"'), '"Amount"');
  assert.equal(normalizeDbIdentifierToken(null), "");
  assert.equal(normalizeDbIdentifierToken('"BROKEN'), '"BROKEN');
});

test("normalizeDbObjectKey は Profile の対象表を backend object_name_tokens と同じキーにする (#561)", () => {
  assert.equal(normalizeDbObjectKey("sales.orders"), "SALES.ORDERS");
  assert.equal(normalizeDbObjectKey('"SALES"."ORDERS"'), "SALES.ORDERS");
  assert.equal(normalizeDbObjectKey("orders"), "ORDERS");
  assert.equal(normalizeDbObjectKey('SALES."Mixed_Case"'), 'SALES."Mixed_Case"');
  assert.equal(normalizeDbObjectKey('"SALES"."Mixed_Case"'), 'SALES."Mixed_Case"');
  assert.equal(normalizeDbObjectKey('SALES."a.b"'), 'SALES."a.b"');
  assert.equal(normalizeDbObjectKey(' SALES."broken '), 'SALES."broken');
  assert.equal(normalizeDbObjectKey(null), "");
});

test("splitDbObjectName は canonical な修飾名を owner / object の token に分ける", () => {
  assert.deepEqual(splitDbObjectName('SALES."a.b"'), { owner: "SALES", name: '"a.b"' });
  assert.deepEqual(splitDbObjectName("sales.orders"), { owner: "SALES", name: "ORDERS" });
  assert.equal(splitDbObjectName("ORDERS"), null);
  assert.equal(splitDbObjectName('SALES."BROKEN'), null);
});

test("別実装だった修飾名の組み立てが formatDbObjectName に委ねられている", () => {
  assert.equal(
    schemaTableQualifiedName({
      table_name: "lower",
      logical_name: "lower",
      owner: "APP",
      table_type: "TABLE",
      comment: "",
      columns: [],
      constraints: [],
    }),
    'APP."lower"'
  );
  assert.equal(objectName({ owner: "APP", objectName: "売上", objectType: "table" }), 'APP."売上"');
  assert.equal(objectName({ owner: "", objectName: "ORDERS", objectType: "table" }), "ORDERS");

  const sources = [
    "src/features/nl2sql/workbenchState.ts",
    "src/features/nl2sql/metadataSql.ts",
    "src/features/nl2sql/ontology/physicalIdentity.ts",
    "src/features/nl2sql/ontology/nodeDisplay.ts",
  ];
  for (const path of sources) {
    const source = readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
    assert.doesNotMatch(source, /`\$\{[\w.?]*owner(?:\.trim\(\))?\}\.\$\{[^}]*\}`/iu, `${path} で owner を単純連結している`);
    assert.doesNotMatch(source, /qualified_?[Nn]ame[^;\n]*\.toUpperCase\(\)/u, `${path} で修飾名を大文字化している`);
  }
});

test("オントロジーの物理名は大文字化せず、引用名の node を大文字の同名表と区別する (#563)", () => {
  const quoted = {
    id: "quoted",
    kind: "table" as const,
    business_name_ja: "引用名",
    technical_name: 'SALES."Mixed_Case"',
    metadata: { owner: "SALES", object_name: "Mixed_Case" },
  };
  const upper = {
    id: "upper",
    kind: "table" as const,
    business_name_ja: "大文字",
    technical_name: "SALES.MIXED_CASE",
    metadata: { owner: "SALES", object_name: "MIXED_CASE" },
  };
  const column = {
    id: "amount",
    kind: "column" as const,
    business_name_ja: "金額",
    technical_name: 'SALES."Mixed_Case"."Amount"',
    metadata: { owner: "SALES", object_name: "Mixed_Case", column_name: "Amount" },
  };
  const quotedIdentity = objectIdentityFromNode(quoted);
  const upperIdentity = objectIdentityFromNode(upper);

  assert.equal(quotedIdentity?.objectName, "Mixed_Case");
  assert.ok(quotedIdentity && upperIdentity);
  assert.equal(
    objectMatches({ ...quotedIdentity, nodeId: undefined }, { ...upperIdentity, nodeId: undefined }),
    false,
  );
  assert.equal(ontologyPhysicalNodeLabel(quoted), 'SALES."Mixed_Case"');
  assert.equal(ontologyPhysicalNodeLabel(column), 'SALES."Mixed_Case"."Amount"');
  assert.equal(ontologyPhysicalNodeLabel(upper), "SALES.MIXED_CASE");
  // technical_name だけの旧 node（引用 token）も引用符を外して同じ名前に揃える。
  assert.equal(
    objectIdentityFromNode({
      id: "legacy",
      kind: "table",
      business_name_ja: "旧",
      technical_name: 'SALES."Mixed_Case"',
    })
      ?.objectName,
    "Mixed_Case",
  );
  assert.notEqual(ontologyGraphObjectClusterKey(quoted), ontologyGraphObjectClusterKey(upper));
  assert.equal(ontologyGraphObjectClusterKey(upper), "object:SALES.MIXED_CASE");
});

test("dbObjectKeyTokens は SQL の表記を部分ごとの照合 token に分け、引用名だけ大文字小文字を保つ (#573)", () => {
  assert.deepEqual(dbObjectKeyTokens('SALES."Mixed_Case"."Amount"'), ["SALES", '"Mixed_Case"', '"Amount"']);
  assert.deepEqual(dbObjectKeyTokens("sales.mixed_case.amount"), ["SALES", "MIXED_CASE", "AMOUNT"]);
  assert.deepEqual(dbObjectKeyTokens('"SALES"."A.B"'), ["SALES", '"A.B"']);
  assert.deepEqual(dbObjectKeyTokens('SALES."broken'), []);
  assert.deepEqual(dbObjectKeyTokens(""), []);
});

test("sqlIdentifierToken は SQL parser の識別子を引用の有無で解釈する (#573)", () => {
  assert.equal(sqlIdentifierToken("Mixed_Case", true), '"Mixed_Case"');
  assert.equal(sqlIdentifierToken("Mixed_Case", false), "MIXED_CASE");
  assert.equal(sqlIdentifierToken("AMOUNT", true), "AMOUNT");
  assert.equal(sqlIdentifierToken("", false), "");
  assert.equal(sqlIdentifierToken(undefined, true), "");
});
