import { API_KEY_PRODUCTS, type ApiKeyProduct, type McpEndpoint, type McpEndpointRow } from "./types";

/**
 * 今開いている画面から見た MCP の接続先（#1805）。
 *
 * 1 台の Compute に配備した Suite は `/<製品>/` の prefix で同じ origin に並ぶ（`suite-nginx.sh`）。
 * 開いている製品の base が `/<製品>/` なら、3 製品（RAG・NL2SQL・Agent。#1874）の URL を出す。それ以外
 * （開発の dev server など、製品ごとに origin が違う）は開いている製品の URL だけを出す。
 */
export function mcpEndpoints(product: ApiKeyProduct, basePath: string, origin: string): McpEndpoint[] {
  const trimmed = basePath.replace(/^\/+|\/+$/g, "");
  const base = trimmed ? `/${trimmed}/` : "/";
  const trimmedOrigin = origin.replace(/\/+$/, "");
  if (base === `/${product}/`) {
    return API_KEY_PRODUCTS.map((item) => ({ product: item, url: `${trimmedOrigin}/${item}/api/mcp` }));
  }
  return [{ product, url: `${trimmedOrigin}${base}api/mcp` }];
}

/**
 * 画面に出す接続先の行（#1870）: 今開いている画面の URL と、配備の公開の URL
 * （`PLATFORM_PUBLIC_BASE_URL` + `/<製品>/api/mcp`。配備は製品を `/<製品>/` の prefix に置く）。
 * 公開の URL があれば RAG と NL2SQL の両方の行を作る（この画面から分からない製品の URL は null）。
 */
export function mcpEndpointRows(endpoints: McpEndpoint[], publicBaseUrl?: string | null): McpEndpointRow[] {
  const base = (publicBaseUrl ?? "").trim().replace(/\/+$/, "");
  if (!base) {
    return endpoints.map((endpoint) => ({ product: endpoint.product, localUrl: endpoint.url, publicUrl: null }));
  }
  return API_KEY_PRODUCTS.map((product) => ({
    product,
    localUrl: endpoints.find((endpoint) => endpoint.product === product)?.url ?? null,
    publicUrl: `${base}/${product}/api/mcp`,
  }));
}

/** 自作の CA の証明書の場所（Nginx が配る `/platform/ca.crt`）。https の URL のときだけ。 */
export function caCertificateUrl(url: string): string | null {
  try {
    const parsed = new URL(url);
    return parsed.protocol === "https:" ? `${parsed.origin}/platform/ca.crt` : null;
  } catch {
    return null;
  }
}

export interface McpRegistrationExampleLabels {
  keyComment: string;
  timeoutComment: string;
  caComment: string;
}

/**
 * Claude Code の登録の例（`skills/README.md`「2. MCP を設定する」「3. 質問する」と同じ形。#1846・#1870）。
 *
 * - サーバー名は `rag` / `nl2sql`（スキルがこの名前を前提にする）。
 * - `--header` は単一引用符で `${PR_API_KEY}` のまま保存させる（キーの値を `~/.claude.json` に書かない）。
 * - `rag_search` は既定の 60 秒を超えることがあるので `MCP_TOOL_TIMEOUT`、自作の CA の HTTPS は `NODE_EXTRA_CA_CERTS`。
 * - URL は公開の URL を優先する（無ければこの画面の URL）。
 */
export function mcpRegistrationExample(rows: McpEndpointRow[], labels: McpRegistrationExampleLabels): string {
  const targets = rows.flatMap((row) => {
    const url = row.publicUrl ?? row.localUrl;
    return url ? [{ product: row.product, url }] : [];
  });
  if (targets.length === 0) return "";
  const caUrl = targets.map((target) => caCertificateUrl(target.url)).find((value) => value !== null) ?? null;
  const lines = [`export PR_API_KEY='prak_...'   # ${labels.keyComment}`];
  for (const target of targets) {
    lines.push(
      `claude mcp add --scope user --transport http ${target.product} ${target.url} \\`,
      "  --header 'Authorization: Bearer ${PR_API_KEY}'"
    );
  }
  lines.push(`export MCP_TOOL_TIMEOUT=600000   # ${labels.timeoutComment}`);
  if (caUrl) {
    lines.push(`export NODE_EXTRA_CA_CERTS=~/production-ready-ca.crt   # ${labels.caComment.replace("{url}", caUrl)}`);
  }
  lines.push("claude");
  return lines.join("\n");
}
