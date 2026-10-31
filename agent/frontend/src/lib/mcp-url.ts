/**
 * MCP 接続の URL に書いた資格情報（userinfo・secret らしい query）の検出と伏せ字（#1056）。
 * 規則は backend（`app/features/agent/mcp_url.py`）と同じ。画面の検証は先回りで、正本は backend。
 * e2e の mock（`e2e/fixtures/mock-api.ts`）も同じ関数で backend の振る舞いをまねる（i18n に依存しない）。
 */

export const URL_CREDENTIAL_MASK = "***";

/** query の名前を語に分けたとき、どれかの語がこれなら secret とみなす。 */
const SECRET_QUERY_WORDS = new Set([
  "key",
  "apikey",
  "token",
  "secret",
  "password",
  "passwd",
  "pwd",
  "auth",
  "authorization",
  "credential",
  "credentials",
  "signature",
  "sig",
  "jwt",
  "bearer",
]);

function decodeQueryName(part: string): string {
  const raw = part.split("=", 1)[0].replace(/\+/g, " ");
  try {
    return decodeURIComponent(raw);
  } catch {
    return raw;
  }
}

/** query の名前が資格情報らしいか（`api_key`・`apiKey`・`X-Api-Key`・`access_token` など）。 */
export function isSecretQueryName(name: string): boolean {
  const words = name
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .toLowerCase()
    .split(/[^a-z0-9]+/);
  return words.some((word) => word && SECRET_QUERY_WORDS.has(word));
}

interface UrlParts {
  head: string;
  authority: string;
  rest: string;
  /** `?` があるか（空の query も元の形のまま返す）。 */
  hasQuery: boolean;
  query: string;
  fragment: string;
}

/** `scheme://authority/path?query#fragment` に分ける（URL クラスは userinfo・query を正規化するため使わない）。 */
function splitUrl(url: string): UrlParts | null {
  const match = /^([A-Za-z][A-Za-z0-9+.-]*:\/\/)([^/?#]*)([^?#]*)(?:\?([^#]*))?(#.*)?$/.exec(url);
  if (!match) return null;
  return {
    head: match[1],
    authority: match[2],
    rest: match[3],
    hasQuery: match[4] !== undefined,
    query: match[4] ?? "",
    fragment: match[5] ?? "",
  };
}

/** 資格情報らしい query の名前（重複なし・出現順）。 */
export function secretQueryNames(url: string): string[] {
  const parts = splitUrl(url);
  if (!parts) return [];
  const names: string[] = [];
  for (const part of parts.query.split("&")) {
    const name = decodeQueryName(part);
    if (part && isSecretQueryName(name) && !names.includes(name)) names.push(name);
  }
  return names;
}

export type McpUrlCredentialProblem =
  | { kind: "userinfo" }
  | { kind: "secretQuery"; names: string[] };

/** URL に資格情報があれば、その種類（値は含めない）。 */
export function mcpUrlCredentialProblem(url: string): McpUrlCredentialProblem | null {
  const parts = splitUrl(url.trim());
  if (!parts) return null;
  if (parts.authority.includes("@")) return { kind: "userinfo" };
  const names = secretQueryNames(url.trim());
  return names.length ? { kind: "secretQuery", names } : null;
}

/** userinfo と secret らしい query の値を `***` にした URL（それ以外はそのまま）。 */
export function maskUrlCredentials(url: string): string {
  const parts = splitUrl(url);
  if (!parts) return url;
  const at = parts.authority.lastIndexOf("@");
  const authority = at >= 0 ? `${URL_CREDENTIAL_MASK}@${parts.authority.slice(at + 1)}` : parts.authority;
  const query = parts.query
    .split("&")
    .map((part) => (part && isSecretQueryName(decodeQueryName(part)) ? `${part.split("=", 1)[0]}=${URL_CREDENTIAL_MASK}` : part))
    .join("&");
  return `${parts.head}${authority}${parts.rest}${parts.hasQuery ? `?${query}` : ""}${parts.fragment}`;
}
