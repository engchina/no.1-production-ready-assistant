export interface DbAdminObjectTarget {
  owner: string;
  name: string;
  qualifiedName: string;
}

const SIMPLE_ORACLE_IDENTIFIER = /^[A-Z][A-Z0-9_$#]{0,127}$/u;

function splitIdentifierParts(value: string): string[] {
  const parts: string[] = [];
  let buffer = "";
  let inDouble = false;
  const raw = value.trim();
  for (let index = 0; index < raw.length; index += 1) {
    const char = raw[index];
    if (char === '"') {
      buffer += char;
      if (inDouble && raw[index + 1] === '"') {
        buffer += raw[index + 1];
        index += 1;
        continue;
      }
      inDouble = !inDouble;
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
  if (inDouble) {
    throw new Error("Oracle 識別子が不正です。");
  }
  const tail = buffer.trim();
  if (tail) parts.push(tail);
  return parts;
}

function unquoteIdentifierPart(value: string): string {
  if (value.length < 2 || !value.startsWith('"') || !value.endsWith('"')) {
    if (value.includes('"')) throw new Error("Oracle 識別子が不正です。");
    return value;
  }
  const inner = value.slice(1, -1).replaceAll('""', "");
  if (inner.includes('"')) throw new Error("Oracle 識別子が不正です。");
  return value.slice(1, -1).replaceAll('""', '"');
}

function normalizeIdentifierPart(value: string): string {
  const trimmed = value.trim();
  if (trimmed.startsWith('"') || trimmed.endsWith('"')) return unquoteIdentifierPart(trimmed);
  const upper = trimmed.toUpperCase();
  return SIMPLE_ORACLE_IDENTIFIER.test(upper) ? upper : trimmed;
}

export function formatDbAdminObjectPart(value: string): string {
  const normalized = normalizeIdentifierPart(value);
  if (!normalized) return "";
  if (SIMPLE_ORACLE_IDENTIFIER.test(normalized)) return normalized;
  return `"${normalized.replaceAll('"', '""')}"`;
}

function formatDbAdminCatalogPart(value: string): string {
  const trimmed = value.trim();
  const normalized =
    trimmed.startsWith('"') || trimmed.endsWith('"') ? unquoteIdentifierPart(trimmed) : trimmed;
  if (!normalized) return "";
  if (SIMPLE_ORACLE_IDENTIFIER.test(normalized)) return normalized;
  return `"${normalized.replaceAll('"', '""')}"`;
}

/** 表・ビュー名の組み立てに使う入力。API の `owner` / `name` / `qualified_name` をそのまま渡す。 */
export interface DbObjectNameSource {
  name: string;
  owner?: string | null;
  qualified_name?: string | null;
}

function strictDbObjectName(item: DbObjectNameSource) {
  const qualifiedName = (item.qualified_name ?? "").trim();
  if (qualifiedName) {
    const parts = splitIdentifierParts(qualifiedName);
    if (parts.length === 2) {
      return `${formatDbAdminCatalogPart(parts[0])}.${formatDbAdminCatalogPart(parts[1])}`;
    }
    if (parts.length === 1) return formatDbAdminCatalogPart(parts[0]);
    return qualifiedName;
  }
  const owner = formatDbAdminCatalogPart(item.owner ?? "");
  const name = formatDbAdminCatalogPart(item.name ?? "");
  return owner && name ? `${owner}.${name}` : name;
}

/**
 * 表・ビュー名を `OWNER.OBJECT` 形式の文字列にする唯一の実装。
 *
 * - `qualified_name` があればそれを優先する。
 * - owner と name は Oracle のカタログ値（大文字化済み・引用符なし）として扱い、
 *   `[A-Z][A-Z0-9_$#]*` に当てはまらない部分だけを `"..."` で囲む。
 *   backend `app/features/nl2sql/object_identity.py` の `qualified_object_name` と同じ規則。
 * - owner が分からない場合は名前だけを返す（推測で補わない）。
 * - 表示用のため例外を投げない。引用符の対応が壊れた値は受け取った文字列のまま返す。
 */
export function formatDbObjectName(item: DbObjectNameSource): string {
  try {
    return strictDbObjectName(item);
  } catch {
    const qualifiedName = (item.qualified_name ?? "").trim();
    if (qualifiedName) return qualifiedName;
    const owner = (item.owner ?? "").trim();
    const name = (item.name ?? "").trim();
    return owner && name ? `${owner}.${name}` : name;
  }
}

/**
 * カタログ上の名前（引用符なし・大文字小文字を保持）を、識別子 1 部分の canonical token にする。
 *
 * - 引用が必要な名前だけ `"..."` で囲む（`Mixed_Case` → `"Mixed_Case"`、`ORDERS` → `ORDERS`）。
 * - canonical token（`"Mixed_Case"`）を渡しても同じ値を返す（冪等）。
 * - backend `format_object_part` と同じ規則。表示・比較用のため例外を投げない。
 */
export function formatDbObjectPart(value: string | null | undefined): string {
  try {
    return formatDbAdminCatalogPart(value ?? "");
  } catch {
    return (value ?? "").trim();
  }
}

/**
 * DeepSec のデータ権限で保存・送信する識別子 1 部分（owner / object / column）を正規化する。
 *
 * backend `canonical_object_part` と同じ規則: `"..."` は大文字小文字を保ち、引用されていない値は
 * Oracle と同じく大文字として扱う。保存済みの値はすでに canonical token なので変わらない。
 * 表示・比較用のため例外を投げず、壊れた値はそのまま返す（保存時に backend が拒否する）。
 */
export function normalizeDbIdentifierToken(value: string | null | undefined): string {
  try {
    return formatDbAdminObjectPart(value ?? "");
  } catch {
    return (value ?? "").trim();
  }
}

/**
 * 保存値・入力値の `OBJECT` / `OWNER.OBJECT`（各部は引用可）を突合キーにする。
 *
 * backend `object_identity.object_name_tokens` と同じ規則: 引用されていない部分は大文字、
 * `"..."` で引用された部分は大文字小文字を保ち、引用が必要な部分だけ `"..."` で囲む。
 * `"SALES"."ORDERS"` / `sales.orders` は `SALES.ORDERS` に揃い、`SALES."Mixed_Case"` は
 * 大文字の同名表 `SALES.MIXED_CASE` と別のキーになる（#561）。
 * 表示・比較用のため例外を投げず、引用符が壊れた値は前後の空白を除いてそのまま返す。
 */
export function normalizeDbObjectKey(value: string | null | undefined): string {
  try {
    return splitIdentifierParts(value ?? "")
      .map(formatDbAdminObjectPart)
      .join(".");
  } catch {
    return (value ?? "").trim();
  }
}

/**
 * 保存値・入力値・SQL の表記の `OBJECT` / `OWNER.OBJECT` / `OWNER.OBJECT.COLUMN`（各部は引用可）を、
 * 部分ごとの照合 token に分ける。
 *
 * `normalizeDbObjectKey` と同じ規則（引用なしは大文字、`"..."` は大文字小文字を保持）。dot を含む
 * 引用名も壊さない。表示・比較用のため例外を投げず、引用符が壊れた値は空配列を返す（どれにも
 * 一致させない）。
 */
export function dbObjectKeyTokens(value: string | null | undefined): string[] {
  try {
    return splitIdentifierParts(value ?? "")
      .map(formatDbAdminObjectPart)
      .filter(Boolean);
  } catch {
    return [];
  }
}

/**
 * SQL parser が返した識別子 1 部分（引用符を外した値と、引用されていたか）を照合 token にする。
 *
 * backend `object_identity.sql_identifier_token` と同じ規則: 引用されていない識別子は Oracle と同じく
 * 大文字、引用された識別子は書かれたとおりに解釈する。`SALES."Mixed_Case"` の `Mixed_Case` を
 * 大文字化すると、大文字の同名表 `SALES.MIXED_CASE` と区別できない（#573）。
 */
export function sqlIdentifierToken(name: string | null | undefined, quoted: boolean): string {
  const raw = (name ?? "").trim();
  if (!raw) return "";
  return formatDbObjectPart(quoted ? raw : raw.toUpperCase());
}

/**
 * canonical な `OWNER.OBJECT` を owner / object の token に分ける。dot を含む引用名も壊さない。
 * 2 部分でない・引用符が壊れている場合は null。
 */
export function splitDbObjectName(value: string): { owner: string; name: string } | null {
  try {
    const parts = splitIdentifierParts(value);
    if (parts.length !== 2) return null;
    return { owner: formatDbAdminObjectPart(parts[0]), name: formatDbAdminObjectPart(parts[1]) };
  } catch {
    return null;
  }
}

/**
 * DeepSec のデータ権限（`target_owner` / `target_object` / `resource_code`）の表示名・比較キー。
 * backend は各部を canonical token（引用が必要な名前だけ `"..."`）で保存するので、各部を
 * `normalizeDbIdentifierToken` で揃えて単純連結する。引用が不要な名前は従来のキー（大文字の
 * `OWNER.OBJECT`）と一致し、引用名は大文字化しないため大文字の同名表と取り違えない。
 */
export function formatEntitlementTargetName(entitlement: {
  target_owner?: string | null;
  target_object?: string | null;
  resource_code?: string | null;
}): string {
  const owner = normalizeDbIdentifierToken(entitlement.target_owner);
  const object = normalizeDbIdentifierToken(entitlement.target_object);
  if (owner && object) return `${owner}.${object}`;
  const resource = (entitlement.resource_code ?? "").trim();
  if (!resource) return "";
  const parts = splitDbObjectName(resource);
  if (parts) return `${parts.owner}.${parts.name}`;
  return resource.includes('"') ? resource : resource.toUpperCase();
}

export function parseDbAdminObjectTarget(value: string, owner = ""): DbAdminObjectTarget {
  const parts = splitIdentifierParts(value);
  const ownerPart =
    parts.length >= 2 ? formatDbAdminObjectPart(parts[0]) : formatDbAdminObjectPart(owner);
  const namePart = formatDbAdminObjectPart(parts.length >= 2 ? parts.slice(1).join(".") : value);
  return {
    owner: ownerPart,
    name: namePart,
    qualifiedName: ownerPart ? `${ownerPart}.${namePart}` : namePart,
  };
}
