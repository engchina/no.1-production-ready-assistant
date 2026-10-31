import { dbObjectKeyTokens, formatDbObjectPart, sqlIdentifierToken } from "../dbObjectIdentity";
import type {
  OntologyEdge,
  OntologyGraph,
  OntologyJsonValue,
  OntologyNode,
  SqlSemanticGraph,
  SqlSemanticItem,
  SqlSemanticJoin,
} from "./types";

export type SqlOntologyGroundingStatus = "matched" | "partial" | "unmatched" | "unavailable";

export interface SqlOntologyGroundingNodeMatch {
  sql: string;
  ontologyNodeIds: string[];
  ontologyLabels: string[];
}

export interface SqlOntologyGroundingEdgeMatch {
  sql: string;
  ontologyEdgeIds: string[];
  ontologyLabels: string[];
}

export interface SqlOntologyGroundingResult {
  status: SqlOntologyGroundingStatus;
  highlightNodeIds: string[];
  highlightEdgeIds: string[];
  matchedTables: SqlOntologyGroundingNodeMatch[];
  matchedColumns: SqlOntologyGroundingNodeMatch[];
  matchedJoins: SqlOntologyGroundingEdgeMatch[];
  unmatchedTables: string[];
  unmatchedColumns: string[];
  unmatchedJoins: string[];
}

interface ObjectIdentity {
  owner: string;
  objectName: string;
  nodeId?: string;
}

interface ColumnIdentity extends ObjectIdentity {
  columnName: string;
}

interface ObjectEntry extends ObjectIdentity {
  node: OntologyNode;
}

interface ColumnEntry extends ColumnIdentity {
  node: OntologyNode;
}

interface GroundingIndex {
  nodesById: Map<string, OntologyNode>;
  edgesById: Map<string, OntologyEdge>;
  objectEntries: ObjectEntry[];
  columnEntries: ColumnEntry[];
}

const TABLE_NODE_KINDS = new Set(["table", "view", "business_entity", "object_type", "business_event"]);
const QUALIFIED_COLUMN_PATTERN = /(?:(?:"[^"]+"|[A-Za-z_][\w$#]*)\.){1,2}(?:"[^"]+"|[A-Za-z_][\w$#]*)/gu;
/** 先頭の識別子パス（`SALES."Mixed_Case" m` の `SALES."Mixed_Case"`）。引用名内の空白・dot を保つ。 */
const IDENTIFIER_PATH_PATTERN = /^\s*((?:"(?:[^"]|"")*"|[^\s."]+)(?:\s*\.\s*(?:"(?:[^"]|"")*"|[^\s."]+))*)/u;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function jsonString(value: OntologyJsonValue | undefined): string {
  return typeof value === "string" ? value : "";
}

/**
 * SELECT の出力別名の判定用（物理 object の識別ではない）。別名は大文字で比べる。
 * 表・列の照合には使わない。大文字化すると `"Amount"` と `AMOUNT` を区別できない（#573）。
 */
function normalizeAliasName(value: string | undefined | null): string {
  return String(value ?? "")
    .trim()
    .replace(/^["'`[]+|["'`\]]+$/g, "")
    .toLocaleUpperCase("en-US");
}

/**
 * ontology node / Join 条件が持つカタログ上の名前（大文字小文字を保持）を照合 token にする。
 * 引用が必要な名前だけ `"..."` で囲む（backend `catalog_match_key`）。
 */
function catalogToken(value: string | undefined | null): string {
  return formatDbObjectPart(value);
}

/**
 * SQL の表記の参照（`SALES."Mixed_Case"."Amount"`、`m.amount`）を部分ごとの照合 token に分ける。
 * 引用なしは大文字、引用名は大文字小文字を保持する（backend `object_match_key`）。
 * 別名や式が続く場合は先頭の識別子パスだけを使う。
 */
function referenceTokens(value: string | undefined | null): string[] {
  const cleaned = String(value ?? "")
    .replace(/`/g, "")
    .replace(/\[/g, "")
    .replace(/\]/g, "")
    .trim();
  const match = IDENTIFIER_PATH_PATTERN.exec(cleaned);
  return match ? dbObjectKeyTokens(match[1]) : [];
}

/** SQL の表別名の照合 token。引用の有無は保持されないため、引用なし・引用ありの両方で登録する。 */
function aliasTokens(value: string | undefined | null): string[] {
  const raw = String(value ?? "").trim();
  if (!raw) return [];
  return dedupe([...dbObjectKeyTokens(raw).slice(-1), catalogToken(raw)].filter(Boolean));
}

function splitQualifiedName(value: string | undefined | null): {
  owner: string;
  objectName: string;
  columnName: string;
} {
  // technical_name は `SALES."Mixed_Case".AMOUNT` のように小文字を含む名前だけ引用される。
  const parts = referenceTokens(value);
  if (parts.length >= 3) {
    return {
      owner: parts[parts.length - 3] ?? "",
      objectName: parts[parts.length - 2] ?? "",
      columnName: parts[parts.length - 1] ?? "",
    };
  }
  if (parts.length === 2) {
    return { owner: "", objectName: parts[0] ?? "", columnName: parts[1] ?? "" };
  }
  return { owner: "", objectName: "", columnName: parts[0] ?? "" };
}

function splitObjectName(value: string | undefined | null): ObjectIdentity | null {
  const parts = referenceTokens(value);
  if (parts.length >= 2) {
    return {
      owner: parts[parts.length - 2] ?? "",
      objectName: parts[parts.length - 1] ?? "",
    };
  }
  if (parts[0]) return { owner: "", objectName: parts[0] };
  return null;
}

function firstMappingObject(node: OntologyNode) {
  return node.physical_mappings?.[0]?.object_ref;
}

function objectIdentityFromNode(node: OntologyNode): ObjectIdentity | null {
  const mapping = firstMappingObject(node);
  const fallback = node.physical_mapping;
  const metadata = node.metadata ?? {};
  const technical = splitQualifiedName(node.technical_name);
  const allowTechnical = TABLE_NODE_KINDS.has(node.kind) || node.kind === "column";
  const catalogOwner = mapping?.owner || jsonString(metadata.owner) || fallback?.owner;
  const owner = catalogOwner ? catalogToken(catalogOwner) : allowTechnical ? technical.owner : "";
  const catalogObject = mapping?.object_name || jsonString(metadata.object_name) || fallback?.object_name;
  const objectName = catalogObject ? catalogToken(catalogObject) : allowTechnical ? technical.objectName : "";
  if (!objectName) return null;
  return {
    owner,
    objectName,
    nodeId: mapping?.node_id || (node.kind === "table" || node.kind === "view" ? node.id : undefined),
  };
}

function columnIdentityFromNode(node: OntologyNode): ColumnIdentity | null {
  const object = objectIdentityFromNode(node);
  if (!object) return null;
  const columnRef = node.physical_mappings?.[0]?.column_refs?.[0];
  const metadata = node.metadata ?? {};
  const catalogColumn =
    columnRef?.column_name || jsonString(metadata.column_name) || node.physical_mapping?.column_name;
  const columnName = catalogColumn ? catalogToken(catalogColumn) : splitQualifiedName(node.technical_name).columnName;
  if (!columnName) return null;
  return { ...object, columnName };
}

function buildIndex(graph: OntologyGraph): GroundingIndex {
  const nodesById = new Map(graph.nodes.map((node) => [node.id, node]));
  const edgesById = new Map(graph.edges.map((edge) => [edge.id, edge]));
  const objectEntries: ObjectEntry[] = [];
  const columnEntries: ColumnEntry[] = [];
  for (const node of graph.nodes) {
    const object = objectIdentityFromNode(node);
    if (object && TABLE_NODE_KINDS.has(node.kind)) objectEntries.push({ ...object, node });
    const column = columnIdentityFromNode(node);
    if (column) columnEntries.push({ ...column, node });
  }
  return { nodesById, edgesById, objectEntries, columnEntries };
}

function normalizeSqlItem(value: SqlSemanticItem | string): SqlSemanticItem {
  return typeof value === "string" ? { expression: value } : value;
}

function normalizeSqlJoin(value: SqlSemanticJoin | string): SqlSemanticJoin {
  return typeof value === "string" ? { expression: value, condition: value } : value;
}

function itemLabel(item: SqlSemanticItem | SqlSemanticJoin | string): string {
  if (typeof item === "string") return item;
  return (
    item.expression ||
    item.expression_sql ||
    ("condition_sql" in item ? item.condition_sql || item.condition : "") ||
    item.query_sql ||
    item.source_sql ||
    item.qualified_name ||
    item.output_name ||
    item.name ||
    "SQL element"
  );
}

function tableIdentityFromItem(item: SqlSemanticItem): ObjectIdentity | null {
  if (item.is_cte) return null;
  // backend が識別子の引用の有無を返す場合はそれを正とする（`SALES."Mixed_Case"` は
  // 大文字の同名表 `SALES.MIXED_CASE` と別の表、#573）。
  if (item.name && (item.name_quoted !== undefined || item.owner_quoted !== undefined)) {
    return {
      owner: sqlIdentifierToken(item.owner, Boolean(item.owner_quoted)),
      objectName: sqlIdentifierToken(item.name, Boolean(item.name_quoted)),
    };
  }
  // 引用情報を持たない旧 artifact は、引用符を保持した SQL の表記から読む。
  const qualified = splitObjectName(item.source_sql || item.qualified_name || item.expression || item.name);
  if (qualified?.objectName) {
    const owner = referenceTokens(item.owner)[0] ?? "";
    return { owner: owner || qualified.owner, objectName: qualified.objectName };
  }
  return null;
}

function objectMatches(left: ObjectIdentity, right: ObjectIdentity): boolean {
  if (!left.objectName || !right.objectName) return false;
  if (left.nodeId && right.nodeId && left.nodeId === right.nodeId) return true;
  if (left.objectName !== right.objectName) return false;
  return !left.owner || !right.owner || left.owner === right.owner;
}

function columnMatches(left: ColumnIdentity, right: ColumnIdentity): boolean {
  return left.columnName === right.columnName && objectMatches(left, right);
}

function matchingObjectEntries(index: GroundingIndex, identity: ObjectIdentity | null): ObjectEntry[] {
  if (!identity?.objectName) return [];
  return index.objectEntries.filter((entry) => objectMatches(entry, identity));
}

/**
 * 未修飾列(`SELECT "SALARY" FROM ...` のようにテーブル修飾が無い列)は、
 * その SQL scope の FROM 句に現れた表だけに束縛する。列名だけで ontology 全体を
 * 横断一致させると、同名列を持つ無関係な表(例: DEPARTMENT_ID を持つ DEPARTMENT /
 * PROJECT)まで接地扱いになるため。
 */
function matchingColumnEntries(
  index: GroundingIndex,
  identity: ColumnIdentity,
  scopeObjects: ObjectIdentity[]
): ColumnEntry[] {
  if (identity.objectName) {
    return index.columnEntries.filter((entry) => columnMatches(entry, identity));
  }
  const byName = index.columnEntries.filter((entry) => entry.columnName === identity.columnName);
  if (scopeObjects.length === 0) return byName;
  const scoped = byName.filter((entry) =>
    scopeObjects.some((object) => objectMatches(entry, object))
  );
  if (scoped.length > 0) return scoped;
  // FROM の表が ontology に 1 つも接地しないときだけ全体フォールバックする。
  // 接地しているのに列が無い場合は素直に「未接地」として報告する。
  return scopeObjects.some((object) => matchingObjectEntries(index, object).length > 0)
    ? []
    : byName;
}

function dedupe<T>(values: T[]): T[] {
  return [...new Set(values)];
}

function nodeLabels(index: GroundingIndex, nodeIds: string[]): string[] {
  return dedupe(nodeIds.map((nodeId) => index.nodesById.get(nodeId)?.business_name_ja || nodeId));
}

function edgeLabels(index: GroundingIndex, edgeIds: string[]): string[] {
  return dedupe(edgeIds.map((edgeId) => index.edgesById.get(edgeId)?.relationship_name_ja || edgeId));
}

function tableAliasMap(sqlGraph: SqlSemanticGraph): Map<string, ObjectIdentity> {
  const aliases = new Map<string, ObjectIdentity>();
  for (const rawTable of sqlGraph.tables) {
    const table = normalizeSqlItem(rawTable);
    const identity = tableIdentityFromItem(table);
    if (!identity) continue;
    for (const alias of aliasTokens(table.alias)) aliases.set(alias, identity);
    aliases.set(identity.objectName, identity);
    if (identity.owner) aliases.set(`${identity.owner}.${identity.objectName}`, identity);
  }
  return aliases;
}

interface TableScope {
  byScope: Map<string, ObjectIdentity[]>;
  all: ObjectIdentity[];
}

/** SQL の FROM 句に現れる実体表を scope 単位で集める(CTE は tableIdentityFromItem が除外)。 */
function tableScopeIdentities(sqlGraph: SqlSemanticGraph): TableScope {
  const byScope = new Map<string, ObjectIdentity[]>();
  const all: ObjectIdentity[] = [];
  for (const rawTable of sqlGraph.tables) {
    const table = normalizeSqlItem(rawTable);
    const identity = tableIdentityFromItem(table);
    if (!identity?.objectName) continue;
    all.push(identity);
    const scopeId = String(table.scope_id ?? "").trim();
    if (!scopeId) continue;
    const bucket = byScope.get(scopeId);
    if (bucket) bucket.push(identity);
    else byScope.set(scopeId, [identity]);
  }
  return { byScope, all };
}

/** 列が属する scope の表集合。scope が判らない旧 artifact は SQL 全体の表へ縮退する。 */
function scopeObjectsForItem(scope: TableScope, item: SqlSemanticItem): ObjectIdentity[] {
  const scopeId = String(item.scope_id ?? "").trim();
  const scoped = scopeId ? scope.byScope.get(scopeId) : undefined;
  return scoped?.length ? scoped : scope.all;
}

function identityFromSourceName(source: string | undefined | null, aliases: Map<string, ObjectIdentity>): ObjectIdentity | null {
  const value = String(source ?? "").trim();
  // `SALES."Mixed_Case" m` を識別子パスと別名に分ける（引用名の中の空白で分けない）。
  const match = IDENTIFIER_PATH_PATTERN.exec(value);
  if (!match) return null;
  const trailing = value.slice(match[0].length).trim().split(/\s+/u).filter(Boolean);
  if (trailing.length === 0) {
    const direct = aliases.get(dbObjectKeyTokens(match[1]).join("."));
    if (direct) return direct;
  } else {
    for (const token of aliasTokens(trailing[trailing.length - 1])) {
      const alias = aliases.get(token);
      if (alias) return alias;
    }
  }
  return splitObjectName(match[1]);
}

function columnIdentityFromPath(path: string, aliases: Map<string, ObjectIdentity>): ColumnIdentity | null {
  return columnIdentityFromTokens(referenceTokens(path), aliases);
}

function columnIdentityFromTokens(parts: string[], aliases: Map<string, ObjectIdentity>): ColumnIdentity | null {
  if (parts.length >= 3) {
    return {
      owner: parts[parts.length - 3] ?? "",
      objectName: parts[parts.length - 2] ?? "",
      columnName: parts[parts.length - 1] ?? "",
    };
  }
  if (parts.length === 2) {
    const aliasIdentity = aliases.get(parts[0] ?? "");
    if (aliasIdentity) return { ...aliasIdentity, columnName: parts[1] ?? "" };
    return { owner: "", objectName: parts[0] ?? "", columnName: parts[1] ?? "" };
  }
  if (parts[0]) return { owner: "", objectName: "", columnName: parts[0] };
  return null;
}

function columnIdentitiesFromItem(item: SqlSemanticItem, aliases: Map<string, ObjectIdentity>): ColumnIdentity[] {
  const values = new Set<string>();
  const identities: ColumnIdentity[] = [];
  if (item.name && item.name_quoted !== undefined && !item.column) {
    // backend の columns[] は owner / 表 / 列ごとに引用の有無を返す。大文字化せずに照合する（#573）。
    const tokens = [
      sqlIdentifierToken(item.owner, Boolean(item.owner_quoted)),
      sqlIdentifierToken(item.table, Boolean(item.table_quoted)),
      sqlIdentifierToken(item.name, Boolean(item.name_quoted)),
    ].filter(Boolean);
    const identity = columnIdentityFromTokens(tokens, aliases);
    if (identity) identities.push(identity);
  } else if (item.table && (item.column || item.name)) {
    values.add(`${item.table}.${item.column || item.name}`);
  } else if (item.column || (item.name && !item.function_name)) {
    values.add(item.column || item.name || "");
  }
  for (const value of item.referenced_columns ?? []) values.add(value);
  for (const value of item.lineage ?? []) values.add(value);
  for (const value of [item.expression, item.expression_sql, item.source_sql, item.qualified_name]) {
    if (!value) continue;
    for (const match of value.matchAll(QUALIFIED_COLUMN_PATTERN)) {
      values.add(match[0]);
    }
  }
  return dedupe(
    [
      ...identities,
      // 式の中の列展開（旧 artifact の referenced_columns の `dept.*` 等）は列の参照ではない（#1787）。
      ...[...values].filter((value) => !isWildcardPath(value)).map((value) => columnIdentityFromPath(value, aliases)),
    ].filter((value): value is ColumnIdentity => Boolean(value?.columnName))
  );
}

/** 識別子パスの末尾が `*`（`*`・`dept.*`・`SALES.ORDERS.*`）か。`COUNT(*)`・`a * b` は含まない。 */
function isWildcardPath(value: string | undefined | null): boolean {
  return /^(?:(?:"(?:[^"]|"")*"|[^\s."*()]+)\s*\.\s*)*\*$/u.test(String(value ?? "").trim());
}

/**
 * SQL の要素が列展開（`*` / `別名.*` / `owner.表.*`）か（#1787）。
 * 列展開は「その表の全列」で、列名 `*` の列の参照ではない。列の接地の対象にせず、
 * 表の接地として扱う（表が接地できなければ、表のループが表の未接地として出す）。
 */
function isWildcardItem(item: SqlSemanticItem): boolean {
  if (item.contains_wildcard) return true;
  if (String(item.name ?? "").trim() === "*" || String(item.column ?? "").trim() === "*") return true;
  return isWildcardPath(item.expression_sql || item.expression);
}

/** 列展開の修飾（`dept.*` の `dept`）が指す表。修飾が無い `*` は、その scope の FROM の表すべて。 */
function wildcardObjects(
  item: SqlSemanticItem,
  aliases: Map<string, ObjectIdentity>,
  tableScope: TableScope
): ObjectIdentity[] {
  let qualifier: string[];
  if (String(item.name ?? "").trim() === "*" && item.name_quoted !== undefined) {
    // backend の columns[] は owner / 表（別名）ごとに引用の有無を返す（#573）。
    qualifier = [
      sqlIdentifierToken(item.owner, Boolean(item.owner_quoted)),
      sqlIdentifierToken(item.table, Boolean(item.table_quoted)),
    ].filter(Boolean);
  } else if (item.table) {
    qualifier = [...referenceTokens(item.owner), ...referenceTokens(item.table)];
  } else {
    const path = String(item.expression_sql || item.expression || "").trim();
    qualifier = isWildcardPath(path) ? referenceTokens(path.replace(/\s*\.?\s*\*$/u, "")) : [];
  }
  if (qualifier.length === 0) return scopeObjectsForItem(tableScope, item);
  const direct = aliases.get(qualifier.slice(-2).join(".")) ?? aliases.get(qualifier[qualifier.length - 1] ?? "");
  if (qualifier.length === 1) return [direct ?? { owner: "", objectName: qualifier[0] ?? "" }];
  const owner = qualifier[qualifier.length - 2] ?? "";
  const objectName = qualifier[qualifier.length - 1] ?? "";
  return [direct && direct.owner === owner ? direct : { owner, objectName }];
}

function enrichColumnNodeIds(index: GroundingIndex, columnEntries: ColumnEntry[]): string[] {
  const nodeIds = new Set(columnEntries.map((entry) => entry.node.id));
  for (const column of columnEntries) {
    for (const object of matchingObjectEntries(index, column)) {
      nodeIds.add(object.node.id);
    }
  }
  return [...nodeIds];
}

function endpointIdentity(endpoint: { owner?: string; object_name?: string; column_name?: string } | undefined): ColumnIdentity | null {
  // Join 条件の端点はカタログ上の名前（大文字小文字を保持）。
  const objectName = catalogToken(endpoint?.object_name);
  const columnName = catalogToken(endpoint?.column_name);
  if (!objectName || !columnName) return null;
  return { owner: catalogToken(endpoint?.owner), objectName, columnName };
}

function columnIdentityFromObject(
  object: ObjectIdentity | null,
  columnName: string | undefined
): ColumnIdentity | null {
  const normalized = catalogToken(columnName);
  if (!object || !normalized) return null;
  return { ...object, columnName: normalized };
}

function edgeJoinConditionPairs(
  index: GroundingIndex,
  edge: OntologyEdge
): Array<{ left: ColumnIdentity; right: ColumnIdentity }> {
  const sourceNode = index.nodesById.get(edge.source_node_id);
  const targetNode = index.nodesById.get(edge.target_node_id);
  const sourceObject = sourceNode ? objectIdentityFromNode(sourceNode) : null;
  const targetObject = targetNode ? objectIdentityFromNode(targetNode) : null;
  const pairs: Array<{ left: ColumnIdentity; right: ColumnIdentity }> = [];
  for (const condition of edge.join_conditions ?? []) {
    const left =
      endpointIdentity(condition.left) ?? columnIdentityFromObject(sourceObject, condition.source_column);
    const right =
      endpointIdentity(condition.right) ?? columnIdentityFromObject(targetObject, condition.target_column);
    if (left && right) pairs.push({ left, right });
  }
  return pairs;
}

/**
 * ON 句の列対で edge を照合する。left_source/right_source の表ペアは star join で
 * ずれることがあるため、接地は ON 句の列を正として先に判定する。
 */
function edgeMatchesJoinColumns(
  index: GroundingIndex,
  edge: OntologyEdge,
  columns: ColumnIdentity[]
): boolean {
  if (columns.length === 0) return false;
  const pairs = edgeJoinConditionPairs(index, edge);
  if (pairs.length === 0) return false;
  return pairs.every(
    (pair) =>
      columns.some((column) => columnMatches(column, pair.left)) &&
      columns.some((column) => columnMatches(column, pair.right))
  );
}

function edgeMatchesObjects(index: GroundingIndex, edge: OntologyEdge, left: ObjectIdentity, right: ObjectIdentity): boolean {
  const source = index.nodesById.get(edge.source_node_id);
  const target = index.nodesById.get(edge.target_node_id);
  const sourceObject = source ? objectIdentityFromNode(source) : null;
  const targetObject = target ? objectIdentityFromNode(target) : null;
  if (sourceObject && targetObject) {
    const forward = objectMatches(sourceObject, left) && objectMatches(targetObject, right);
    const reverse = objectMatches(sourceObject, right) && objectMatches(targetObject, left);
    if (forward || reverse) return true;
  }
  for (const condition of edge.join_conditions ?? []) {
    const conditionLeft = endpointIdentity(condition.left);
    const conditionRight = endpointIdentity(condition.right);
    if (!conditionLeft || !conditionRight) continue;
    const forward = objectMatches(conditionLeft, left) && objectMatches(conditionRight, right);
    const reverse = objectMatches(conditionLeft, right) && objectMatches(conditionRight, left);
    if (forward || reverse) return true;
  }
  return false;
}

function joinColumnIdentities(
  join: SqlSemanticJoin,
  aliases: Map<string, ObjectIdentity>
): ColumnIdentity[] {
  const values = new Set<string>();
  for (const value of join.referenced_columns ?? []) values.add(value);
  for (const value of [join.condition_sql, join.condition, join.expression, join.expression_sql]) {
    if (!value) continue;
    for (const match of value.matchAll(QUALIFIED_COLUMN_PATTERN)) values.add(match[0]);
  }
  return [...values]
    .map((value) => columnIdentityFromPath(value, aliases))
    .filter((value): value is ColumnIdentity => Boolean(value?.columnName && value.objectName));
}

/**
 * SELECT で新しく作られた出力別名(`COUNT(*) AS "CNT"` の CNT など)。
 * ORDER BY / GROUP BY / HAVING はこの別名を未修飾で参照できるが物理列ではないので、
 * 未接地として報告しない。式が参照列そのものの別名(`t.SALARY` → SALARY)は除く。
 */
function computedOutputNames(sqlGraph: SqlSemanticGraph): Set<string> {
  const names = new Set<string>();
  for (const rawProjection of sqlGraph.projections ?? []) {
    const projection = normalizeSqlItem(rawProjection);
    const outputName = normalizeAliasName(projection.output_name);
    if (!outputName) continue;
    const referenced = (projection.referenced_columns ?? []).some((value) => {
      const parts = String(value).split(".");
      return normalizeAliasName(parts[parts.length - 1]) === outputName;
    });
    if (!referenced) names.add(outputName);
  }
  return names;
}

function sqlItemsForColumnGrounding(sqlGraph: SqlSemanticGraph): Array<SqlSemanticItem | string> {
  // columns[] は backend が全 clause の列参照を (scope, clause, 式) で重複排除した正本。
  // projections/filters/aggregates/… は同じ列の再掲なので、併せて走査すると同一列を
  // 二重に数えてしまう(接地件数が実際の倍になる)。
  if (sqlGraph.columns.length > 0) return sqlGraph.columns;
  // columns[] を持たない旧 artifact 互換。
  return [
    ...(sqlGraph.projections ?? []),
    ...sqlGraph.filters,
    ...sqlGraph.aggregates,
    ...(sqlGraph.groups ?? sqlGraph.group_by ?? []),
    ...sqlGraph.having,
    ...(sqlGraph.orders ?? sqlGraph.order_by ?? []),
  ];
}

export function groundSqlSemanticGraphOnOntologyGraph(
  sqlGraph: SqlSemanticGraph | null | undefined,
  ontologyGraph: OntologyGraph | null | undefined
): SqlOntologyGroundingResult {
  const empty: SqlOntologyGroundingResult = {
    status: "unavailable",
    highlightNodeIds: [],
    highlightEdgeIds: [],
    matchedTables: [],
    matchedColumns: [],
    matchedJoins: [],
    unmatchedTables: [],
    unmatchedColumns: [],
    unmatchedJoins: [],
  };
  if (!sqlGraph || !ontologyGraph || ontologyGraph.nodes.length === 0) return empty;

  const index = buildIndex(ontologyGraph);
  const aliases = tableAliasMap(sqlGraph);
  const tableScope = tableScopeIdentities(sqlGraph);
  const outputNames = computedOutputNames(sqlGraph);
  const highlightedNodes = new Set<string>();
  const highlightedEdges = new Set<string>();
  const matchedTables: SqlOntologyGroundingNodeMatch[] = [];
  const matchedColumns: SqlOntologyGroundingNodeMatch[] = [];
  const matchedJoins: SqlOntologyGroundingEdgeMatch[] = [];
  const unmatchedTables: string[] = [];
  const unmatchedColumns: string[] = [];
  const unmatchedJoins: string[] = [];

  for (const rawTable of sqlGraph.tables) {
    const table = normalizeSqlItem(rawTable);
    // SQL に owner が無い表も、backend が current schema で補った所有者付き名前で示す（#556）。
    const label = table.resolved_qualified_name || table.qualified_name || itemLabel(table);
    const entries = matchingObjectEntries(index, tableIdentityFromItem(table));
    const nodeIds = dedupe(entries.map((entry) => entry.node.id));
    if (nodeIds.length === 0) {
      if (!table.is_cte) unmatchedTables.push(label);
      continue;
    }
    nodeIds.forEach((nodeId) => highlightedNodes.add(nodeId));
    matchedTables.push({ sql: label, ontologyNodeIds: nodeIds, ontologyLabels: nodeLabels(index, nodeIds) });
  }

  // 列展開（`*` / `別名.*`）は「その表の全列」。表が接地できたら、グラフでその表の列を
  // 使われた列として示す（未接地の要素にも、別の接地の要素にもしない。#1787）。
  const wildcardItems = [...(sqlGraph.projections ?? []), ...sqlGraph.columns]
    .map(normalizeSqlItem)
    .filter(isWildcardItem);
  for (const item of wildcardItems) {
    for (const object of wildcardObjects(item, aliases, tableScope)) {
      const objectEntries = matchingObjectEntries(index, object);
      if (objectEntries.length === 0) continue;
      objectEntries.forEach((entry) => highlightedNodes.add(entry.node.id));
      index.columnEntries
        .filter((entry) => objectMatches(entry, object))
        .forEach((entry) => highlightedNodes.add(entry.node.id));
    }
  }

  for (const rawItem of sqlItemsForColumnGrounding(sqlGraph)) {
    const item = normalizeSqlItem(rawItem);
    if (isWildcardItem(item)) continue;
    const label = itemLabel(item);
    const scopeObjects = scopeObjectsForItem(tableScope, item);
    const columnEntries = dedupe(
      columnIdentitiesFromItem(item, aliases).flatMap((identity) =>
        matchingColumnEntries(index, identity, scopeObjects)
      )
    );
    if (columnEntries.length === 0) {
      const hasColumnHint =
        (item.referenced_columns?.length ?? 0) > 0 ||
        Boolean(item.table && (item.column || item.name)) ||
        Boolean(item.column) ||
        // columns[] 由来の未修飾列(clause 付き・table 空)も未接地として報告する。
        // ただし SELECT で作られた出力別名の参照は物理列ではないので除く。
        Boolean(item.clause && item.name && !outputNames.has(normalizeAliasName(item.name)));
      if (hasColumnHint) unmatchedColumns.push(label);
      continue;
    }
    const nodeIds = enrichColumnNodeIds(index, columnEntries);
    nodeIds.forEach((nodeId) => highlightedNodes.add(nodeId));
    matchedColumns.push({ sql: label, ontologyNodeIds: nodeIds, ontologyLabels: nodeLabels(index, nodeIds) });
  }

  for (const rawJoin of sqlGraph.joins) {
    const join = normalizeSqlJoin(rawJoin);
    const label = itemLabel(join);
    const directEdge = join.ontology_edge_id ? index.edgesById.get(join.ontology_edge_id) : undefined;
    let edgeIds = directEdge ? [directEdge.id] : [];
    if (edgeIds.length === 0) {
      const joinColumns = joinColumnIdentities(join, aliases);
      edgeIds = ontologyGraph.edges
        .filter((edge) => edgeMatchesJoinColumns(index, edge, joinColumns))
        .map((edge) => edge.id);
    }
    if (edgeIds.length === 0) {
      const left = identityFromSourceName(join.left_source || join.source_table, aliases);
      const right = identityFromSourceName(join.right_source || join.target_table, aliases);
      if (left && right) {
        edgeIds = ontologyGraph.edges
          .filter((edge) => edgeMatchesObjects(index, edge, left, right))
          .map((edge) => edge.id);
      }
    }
    edgeIds = dedupe(edgeIds);
    if (edgeIds.length === 0) {
      unmatchedJoins.push(label);
      continue;
    }
    edgeIds.forEach((edgeId) => {
      highlightedEdges.add(edgeId);
      const edge = index.edgesById.get(edgeId);
      if (edge) {
        highlightedNodes.add(edge.source_node_id);
        highlightedNodes.add(edge.target_node_id);
      }
    });
    matchedJoins.push({ sql: label, ontologyEdgeIds: edgeIds, ontologyLabels: edgeLabels(index, edgeIds) });
  }

  const unmatchedCount = unmatchedTables.length + unmatchedColumns.length + unmatchedJoins.length;
  const matchedCount = matchedTables.length + matchedColumns.length + matchedJoins.length;
  const status: SqlOntologyGroundingStatus =
    matchedCount === 0 ? "unmatched" : unmatchedCount > 0 ? "partial" : "matched";

  return {
    status,
    highlightNodeIds: [...highlightedNodes],
    highlightEdgeIds: [...highlightedEdges],
    matchedTables,
    matchedColumns,
    matchedJoins,
    unmatchedTables: dedupe(unmatchedTables),
    unmatchedColumns: dedupe(unmatchedColumns),
    unmatchedJoins: dedupe(unmatchedJoins),
  };
}

export function isSqlSemanticGraph(value: unknown): value is SqlSemanticGraph {
  if (!isRecord(value)) return false;
  return (
    Array.isArray(value.ctes) &&
    Array.isArray(value.tables) &&
    Array.isArray(value.columns) &&
    Array.isArray(value.joins) &&
    Array.isArray(value.filters) &&
    Array.isArray(value.aggregates) &&
    Array.isArray(value.having) &&
    Array.isArray(value.windows)
  );
}
