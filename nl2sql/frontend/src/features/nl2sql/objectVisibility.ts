import type { DbAdminObjectPage, SchemaCatalog, SchemaObjectPage } from "./types";

function splitIdentifierParts(value: string): string[] {
  const parts: string[] = [];
  let buffer = "";
  let inDouble = false;
  for (const char of value.trim()) {
    if (char === '"') {
      inDouble = !inDouble;
      buffer += char;
      continue;
    }
    if (char === "." && !inDouble) {
      const part = buffer.trim();
      if (part) parts.push(part);
      buffer = "";
      continue;
    }
    buffer += char;
  }
  const tail = buffer.trim();
  if (tail) parts.push(tail);
  return parts;
}

function normalizeIdentifierPart(value: string): string {
  const trimmed = value.trim();
  const unquoted =
    trimmed.length >= 2 && trimmed.startsWith('"') && trimmed.endsWith('"')
      ? trimmed.slice(1, -1).replaceAll('""', '"')
      : trimmed;
  return unquoted.toUpperCase();
}

function isUserVisibleOwnerName(ownerName: string): boolean {
  const normalized = normalizeIdentifierPart(ownerName);
  return Boolean(normalized) && !normalized.includes("$") && !normalized.includes("#");
}

function isUserVisibleObjectPart(objectName: string): boolean {
  const normalized = normalizeIdentifierPart(objectName);
  return (
    Boolean(normalized) &&
    !normalized.includes("$") &&
    !normalized.includes("#") &&
    !normalized.startsWith("NL2SQL_")
  );
}

export function isUserVisibleObjectName(objectName: string): boolean {
  const parts = splitIdentifierParts(objectName);
  if (parts.length === 0) return false;
  return parts.slice(0, -1).every(isUserVisibleOwnerName) && isUserVisibleObjectPart(parts.at(-1) ?? "");
}

export function isUserVisibleSchemaObject(
  owner: string | null | undefined,
  objectName: string
): boolean {
  const normalizedOwner = (owner ?? "").trim();
  const ownerVisible = !normalizedOwner || isUserVisibleOwnerName(normalizedOwner);
  return ownerVisible && isUserVisibleObjectName(objectName);
}

export function filterUserVisibleCatalog(catalog: SchemaCatalog): SchemaCatalog {
  return {
    ...catalog,
    tables: catalog.tables.filter((table) =>
      isUserVisibleSchemaObject(table.owner, table.table_name)
    ),
    view_dependencies: catalog.view_dependencies?.filter(
      (dependency) =>
        isUserVisibleSchemaObject(dependency.owner, dependency.view_name) &&
        isUserVisibleSchemaObject(dependency.referenced_owner, dependency.referenced_name)
    ),
  };
}

export function filterUserVisibleSchemaObjectPage(page: SchemaObjectPage): SchemaObjectPage {
  const hidden = page.items.filter(
    (item) => !isUserVisibleSchemaObject(item.owner, item.object_name)
  );
  const hiddenTables = hidden.filter(
    (item) => !["VIEW", "MATERIALIZED VIEW"].includes(item.object_type.toUpperCase())
  ).length;
  const hiddenViews = hidden.length - hiddenTables;
  return {
    ...page,
    items: page.items.filter((item) =>
      isUserVisibleSchemaObject(item.owner, item.object_name)
    ),
    total: page.total === null ? null : Math.max(0, page.total - hidden.length),
    table_count:
      page.table_count === undefined
        ? undefined
        : Math.max(0, page.table_count - hiddenTables),
    view_count:
      page.view_count === undefined
        ? undefined
        : Math.max(0, page.view_count - hiddenViews),
  };
}

export function filterUserVisibleDbAdminObjectPage(page: DbAdminObjectPage): DbAdminObjectPage {
  const hidden = page.items.filter((item) => !isUserVisibleSchemaObject(item.owner, item.name));
  const hiddenTables = hidden.filter((item) => item.object_type !== "view").length;
  const hiddenViews = hidden.length - hiddenTables;
  return {
    ...page,
    items: page.items.filter((item) => isUserVisibleSchemaObject(item.owner, item.name)),
    total: Math.max(0, page.total - hidden.length),
    table_count: Math.max(0, page.table_count - hiddenTables),
    view_count: Math.max(0, page.view_count - hiddenViews),
  };
}
