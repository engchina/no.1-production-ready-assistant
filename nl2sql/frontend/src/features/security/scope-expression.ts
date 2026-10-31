import type {
  DataEntitlement,
  DataEntitlementScopeFilter,
  DeepSecTargetColumn,
  ScopeExpression,
  ScopeGroup,
  ScopeNode,
} from "./types";

export const scopeOperators: Record<string, string[]> = {
  TEXT: ["EQ", "NE", "CONTAINS", "STARTS_WITH", "IN", "IS_NULL", "IS_NOT_NULL"],
  NUMBER: [
    "EQ",
    "NE",
    "GT",
    "GTE",
    "LT",
    "LTE",
    "BETWEEN",
    "IN",
    "IS_NULL",
    "IS_NOT_NULL",
  ],
  TEMPORAL: [
    "EQ",
    "BEFORE",
    "ON_OR_BEFORE",
    "AFTER",
    "ON_OR_AFTER",
    "BETWEEN",
    "IS_NULL",
    "IS_NOT_NULL",
  ],
};
export function columnValueType(type: string): string | null {
  const base = type.toUpperCase().split(/[ (]/)[0];
  if (["CHAR", "NCHAR", "VARCHAR2", "NVARCHAR2"].includes(base)) return "TEXT";
  if (["NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE"].includes(base))
    return "NUMBER";
  if (["DATE", "TIMESTAMP"].includes(base)) return "TEMPORAL";
  return null;
}
export function blankCondition(columns: DeepSecTargetColumn[]): ScopeNode {
  const column = columns.find((c) => columnValueType(c.data_type));
  return {
    kind: "condition",
    filter: {
      column_name: column?.column_name ?? "",
      operator: "EQ",
      value_type: columnValueType(column?.data_type ?? "") ?? "TEXT",
      value_source: "LITERAL",
      value: "",
      value_to: "",
      values: [],
    },
  };
}
export function entitlementExpression(item: DataEntitlement): ScopeExpression {
  if (item.scope_expression) return item.scope_expression;
  const filters =
    item.scope_mode === "COLUMN_EQUALS"
      ? [
          {
            column_name: item.scope_column ?? "",
            operator: "EQ",
            value_type: "TEXT",
            value: item.scope_code,
          },
        ]
      : (item.scope_filters ?? []);
  return {
    version: 1,
    root: {
      kind: "group",
      operator: "AND",
      children: filters.map((filter) => ({ kind: "condition", filter })),
    },
  };
}
export function expressionCounts(node: ScopeNode): {
  conditions: number;
  related: number;
} {
  if (node.kind === "condition") return { conditions: 1, related: 0 };
  if (node.kind === "related_exists")
    return { ...expressionCounts(node.condition), related: 1 };
  return node.children.reduce(
    (sum, child) => {
      const count = expressionCounts(child);
      return {
        conditions: sum.conditions + count.conditions,
        related: sum.related + count.related,
      };
    },
    { conditions: 0, related: 0 },
  );
}
export function filterError(filter: DataEntitlementScopeFilter): string {
  if (
    !filter.column_name ||
    !scopeOperators[filter.value_type]?.includes(filter.operator)
  )
    return "scopeFilterValidation";
  if (filter.value_source === "LOGIN_USER_ID")
    return filter.operator === "EQ" &&
      ["TEXT", "NUMBER"].includes(filter.value_type)
      ? ""
      : "scopeFilterValidation";
  if (["IS_NULL", "IS_NOT_NULL"].includes(filter.operator)) return "";
  if (filter.operator === "IN")
    return filter.values?.some((v) => v.trim()) ? "" : "scopeFilterValidation";
  if (
    !filter.value?.trim() ||
    (filter.operator === "BETWEEN" && !filter.value_to?.trim())
  )
    return "scopeFilterValidation";
  if (
    filter.value_type === "NUMBER" &&
    filter.operator === "EQ" &&
    !/^[1-9]\d*$/.test(filter.value.trim())
  )
    return "scopeFilterPositiveIntegerValidation";
  return "";
}
export function expressionError(
  expression: ScopeExpression | null | undefined,
): string {
  if (!expression || expression.version !== 1 || !expression.root)
    return "expression.invalid";
  const count = expressionCounts(expression.root);
  if (count.conditions > 20 || count.related > 3) return "expression.limits";
  function walk(node: ScopeNode, depth: number, related: boolean): string {
    if (node.kind === "condition") return filterError(node.filter);
    if (node.kind === "related_exists") {
      if (
        related ||
        !node.profile_id ||
        !node.target_object ||
        !node.target_owner ||
        !node.join_keys.length ||
        node.join_keys.some((k) => !k.source_column || !k.target_column)
      )
        return "expression.relatedInvalid";
      return walk(node.condition, depth + 1, true);
    }
    if (!node.children.length) return "expression.empty";
    if (depth > 3) return "expression.limits";
    return (
      node.children
        .map((child) =>
          walk(child, depth + (child.kind === "group" ? 1 : 0), related),
        )
        .find(Boolean) ?? ""
    );
  }
  return walk(expression.root, 1, false);
}
export function canonicalExpression(
  expression: ScopeExpression | null | undefined,
): unknown {
  if (!expression) return null;
  function canonical(node: ScopeNode): unknown {
    if (node.kind === "condition")
      return {
        ...node,
        filter: {
          ...node.filter,
          values: [...(node.filter.values ?? [])].sort(),
        },
      };
    if (node.kind === "related_exists")
      return {
        ...node,
        join_keys: [...node.join_keys].sort((a, b) =>
          JSON.stringify(a).localeCompare(JSON.stringify(b)),
        ),
        condition: canonical(node.condition),
      };
    return {
      ...node,
      children: node.children
        .map(canonical)
        .sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b))),
    };
  }
  return { version: expression.version, root: canonical(expression.root) };
}
export function expressionSummary(
  group: ScopeGroup,
  label: (key: string) => string,
): string {
  return (
    "(" +
    group.children
      .map((node) => {
        if (node.kind === "group") return expressionSummary(node, label);
        if (node.kind === "related_exists")
          return `${label("expression.exists")} ${node.target_owner}.${node.target_object} ${expressionSummary(node.condition, label)}`;
        const f = node.filter;
        return `${f.column_name} ${label("operator." + f.operator)} ${f.value_source === "LOGIN_USER_ID" ? label("scopeFilterValueLoginUserId") : f.operator === "IN" ? (f.values ?? []).join(", ") : [f.value, f.value_to].filter(Boolean).join(" … ")}`;
      })
      .join(` ${group.operator} `) +
    ")"
  );
}

export function normalizeExpression(
  expression: ScopeExpression,
): ScopeExpression {
  function walk(node: ScopeNode): ScopeNode {
    if (node.kind === "group")
      return { ...node, children: node.children.map(walk) };
    if (node.kind === "related_exists")
      return { ...node, condition: walk(node.condition) as ScopeGroup };
    const f = node.filter;
    return {
      ...node,
      filter: {
        ...f,
        value_source: f.value_source ?? "LITERAL",
        value: (f.value ?? "").trim(),
        value_to: (f.value_to ?? "").trim(),
        values: [
          ...new Set((f.values ?? []).map((v) => v.trim()).filter(Boolean)),
        ],
      },
    };
  }
  return { version: 1, root: walk(expression.root) as ScopeGroup };
}
