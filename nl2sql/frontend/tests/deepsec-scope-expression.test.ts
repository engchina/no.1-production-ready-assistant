import assert from "node:assert/strict";
import test from "node:test";
import {
  canonicalExpression,
  entitlementExpression,
  expressionError,
  normalizeExpression,
} from "../src/features/security/scope-expression";
import type {
  ScopeExpression,
  ScopeNode,
} from "../src/features/security/types";
const condition = (value = "ACTIVE"): ScopeNode => ({
  kind: "condition",
  filter: { column_name: "STATUS", operator: "EQ", value_type: "TEXT", value },
});
const expression = (...children: ScopeNode[]): ScopeExpression => ({
  version: 1,
  root: { kind: "group", operator: "AND", children },
});
test("旧 AND 条件を損失なく条件グループとして読み込む", () => {
  const result = entitlementExpression({
    resource_code: "HR.E",
    scope_code: "FILTERS",
    capability: "SELECT",
    scope_mode: "FILTERS",
    scope_filters: [
      {
        column_name: "STATUS",
        operator: "EQ",
        value_type: "TEXT",
        value: "ACTIVE",
      },
    ],
  });
  assert.deepEqual(result, expression(condition()));
  assert.equal(expressionError(result), "");
});
test("空グループ、過剰階層、未完成関連は保存対象にしない", () => {
  assert.equal(expressionError(expression()), "expression.empty");
  assert.equal(
    expressionError(
      expression(
        expression(expression(expression(condition()).root).root).root,
      ),
    ),
    "expression.limits",
  );
  assert.equal(
    expressionError(
      expression({
        kind: "related_exists",
        profile_id: "",
        object_scope_version: 1,
        target_owner: "HR",
        target_object: "D",
        target_type: "TABLE",
        relation_source: "MANUAL",
        relation_id: "",
        relation_version: "",
        join_keys: [],
        condition: expression(condition()).root,
      }),
    ),
    "expression.relatedInvalid",
  );
});
test("集合の順序変更は dirty 判定から除外し AND/OR 差分は保持する", () => {
  assert.deepEqual(
    canonicalExpression(expression(condition("A"), condition("B"))),
    canonicalExpression(expression(condition("B"), condition("A"))),
  );
  const changed = expression(condition("A"), condition("B"));
  changed.root.operator = "OR";
  assert.notDeepEqual(
    canonicalExpression(changed),
    canonicalExpression(expression(condition("A"), condition("B"))),
  );
});
test("送信時だけリスト値を正規化しユーザーの条件を落とさない", () => {
  const source = expression({
    kind: "condition",
    filter: {
      column_name: "STATUS",
      operator: "IN",
      value_type: "TEXT",
      values: [" A", "B ", "A", ""],
    },
  });
  const normalized = normalizeExpression(source);
  assert.deepEqual(
    (normalized.root.children[0] as { filter: { values: string[] } }).filter
      .values,
    ["A", "B"],
  );
  assert.equal(source.root.children.length, 1);
});
