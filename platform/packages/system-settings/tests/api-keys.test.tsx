import { ConfirmProvider } from "@production-ready/ui";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";

import {
  API_KEY_ADMIN_NAV_ITEM,
  API_KEY_PATHS,
  API_KEYS_QUERY_KEY,
  API_KEYS_ADMIN_QUERY_KEY,
  ApiKeysAdminPage,
  accountTabPath,
  MyApiKeysPanel,
  caCertificateUrl,
  mcpEndpointRows,
  mcpEndpoints,
  mcpRegistrationExample,
  type ApiKey,
} from "../src";

const KEY: ApiKey = {
  id: "0123456789abcdef",
  name: "会社の PC の Claude Code",
  token_prefix: "prak_0123456789abcdef_AbCd",
  products: ["rag", "nl2sql"],
  owner_user_uuid: "u-1",
  owner_display_name: "山田",
  created_at: "2026-10-14T00:00:00Z",
  expires_at: null,
  last_used_at: null,
  expired: false,
};

function render(node: ReactNode, data?: { mine?: ApiKey[]; all?: ApiKey[]; publicBaseUrl?: string }): string {
  const client = new QueryClient();
  if (data?.mine) client.setQueryData(API_KEYS_QUERY_KEY, { keys: data.mine, public_base_url: data.publicBaseUrl ?? "" });
  if (data?.all) client.setQueryData(API_KEYS_ADMIN_QUERY_KEY, { keys: data.all });
  return renderToStaticMarkup(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ConfirmProvider>{node}</ConfirmProvider>
      </QueryClientProvider>
    </MemoryRouter>
  );
}

const pending = () => new Promise<never>(() => undefined);
const myApi = { list: pending, create: pending, revoke: pending };
const adminApi = { list: pending, revoke: pending };

describe("mcpEndpoints（#1805）", () => {
  it("Suite の配備（/<製品>/）では 3 製品の URL を出す（#1874）", () => {
    expect(mcpEndpoints("rag", "/rag/", "https://suite.example.com")).toEqual([
      { product: "rag", url: "https://suite.example.com/rag/api/mcp" },
      { product: "nl2sql", url: "https://suite.example.com/nl2sql/api/mcp" },
      { product: "agent", url: "https://suite.example.com/agent/api/mcp" },
    ]);
    expect(mcpEndpoints("nl2sql", "/nl2sql", "https://suite.example.com/")).toHaveLength(3);
  });

  it("開発の dev server（base が /）では開いている製品の URL だけ", () => {
    expect(mcpEndpoints("nl2sql", "/", "http://localhost:5174")).toEqual([
      { product: "nl2sql", url: "http://localhost:5174/api/mcp" },
    ]);
  });
});

describe("接続先の行と登録の例（#1870）", () => {
  const labels = { keyComment: "KEY", timeoutComment: "TIMEOUT", caComment: "CA {url}" };

  it("公開の URL があれば 3 製品の行に足す（この画面から分からない URL は null）", () => {
    const local = mcpEndpoints("nl2sql", "/", "http://localhost:5174");
    expect(mcpEndpointRows(local, "https://203.0.113.10:8443/")).toEqual([
      { product: "rag", localUrl: null, publicUrl: "https://203.0.113.10:8443/rag/api/mcp" },
      { product: "nl2sql", localUrl: "http://localhost:5174/api/mcp", publicUrl: "https://203.0.113.10:8443/nl2sql/api/mcp" },
      { product: "agent", localUrl: null, publicUrl: "https://203.0.113.10:8443/agent/api/mcp" },
    ]);
    // 公開の URL が無ければ、この画面の URL だけ。
    expect(mcpEndpointRows(local, "")).toEqual([
      { product: "nl2sql", localUrl: "http://localhost:5174/api/mcp", publicUrl: null },
    ]);
  });

  it("CA の証明書の場所は https の URL だけ", () => {
    expect(caCertificateUrl("https://203.0.113.10:8443/rag/api/mcp")).toBe("https://203.0.113.10:8443/platform/ca.crt");
    expect(caCertificateUrl("http://localhost:8000/api/mcp")).toBeNull();
    expect(caCertificateUrl("not a url")).toBeNull();
  });

  it("登録の例は skills/README.md と同じ形（サーバー名 rag / nl2sql / agent・単一引用符の ${PR_API_KEY}・待ち時間・CA）", () => {
    const rows = mcpEndpointRows(mcpEndpoints("rag", "/rag/", "https://203.0.113.10"), "https://203.0.113.10");
    expect(mcpRegistrationExample(rows, labels)).toBe(
      [
        "export PR_API_KEY='prak_...'   # KEY",
        "claude mcp add --scope user --transport http rag https://203.0.113.10/rag/api/mcp \\",
        "  --header 'Authorization: Bearer ${PR_API_KEY}'",
        "claude mcp add --scope user --transport http nl2sql https://203.0.113.10/nl2sql/api/mcp \\",
        "  --header 'Authorization: Bearer ${PR_API_KEY}'",
        "claude mcp add --scope user --transport http agent https://203.0.113.10/agent/api/mcp \\",
        "  --header 'Authorization: Bearer ${PR_API_KEY}'",
        "export MCP_TOOL_TIMEOUT=600000   # TIMEOUT",
        "export NODE_EXTRA_CA_CERTS=~/production-ready-ca.crt   # CA https://203.0.113.10/platform/ca.crt",
        "claude",
      ].join("\n")
    );
    // http（開発）では CA の行を出さず、公開の URL が無い製品はこの画面の URL で書く。
    const dev = mcpRegistrationExample(mcpEndpointRows(mcpEndpoints("rag", "/", "http://localhost:3000"), ""), labels);
    expect(dev).toContain("claude mcp add --scope user --transport http rag http://localhost:3000/api/mcp");
    expect(dev).not.toContain("NODE_EXTRA_CA_CERTS");
    expect(dev).not.toContain("nl2sql");
  });
});

describe("API キーの画面（#1805）", () => {
  it("パスとナビの項目は 2 つの画面で別", () => {
    // 自分のキーはアカウントの画面の「API キー」のタブ（#1873）、全員のキーは「ユーザーとロール」の画面。
    expect(API_KEY_PATHS).toEqual({ admin: "/settings/security/api-keys" });
    expect(accountTabPath("api-keys")).toBe("/account?tab=api-keys");
    expect(API_KEY_ADMIN_NAV_ITEM.href).toBe(API_KEY_PATHS.admin);
    expect(API_KEY_ADMIN_NAV_ITEM.labelKey).toBe("nav.securityApiKeys");
  });

  it("自分の画面は接続先・発行・自分のキーの一覧を出し、秘密は出さない", () => {
    const html = render(
      <MyApiKeysPanel
        api={myApi}
        endpoints={mcpEndpoints("rag", "/rag/", "https://suite.example.com")}
      />,
      { mine: [KEY] }
    );
    expect(html).toContain("https://suite.example.com/rag/api/mcp");
    expect(html).toContain("https://suite.example.com/nl2sql/api/mcp");
    expect(html).toContain("API キーを発行");
    expect(html).toContain("会社の PC の Claude Code");
    expect(html).toContain("prak_0123456789abcdef_AbCd…");
    expect(html).toContain("RAG・NL2SQL");
    expect(html).toContain("未使用");
    // 自分の画面には利用者の列を出さない。
    expect(html).not.toContain("利用者</th>");
    // 公開の URL が無いときは、この画面の URL だけを出す。https なら CA の案内を添える（#1870）。
    expect(html).toContain("この画面の URL");
    expect(html).not.toContain("公開の URL（OCI）");
    expect(html).toContain("https://suite.example.com/platform/ca.crt");
    // 発行の既定の製品は、開いている製品によらず 3 製品のすべて（#1869・#1874）。
    expect(html).toMatch(/checked="" value="rag"/);
    expect(html).toMatch(/checked="" value="nl2sql"/);
    expect(html).toMatch(/checked="" value="agent"/);
    // 業務 Agent の候補を渡さない製品（RAG / NL2SQL）は、絞るのは Agent の画面と案内する。
    expect(html).toContain('data-testid="api-key-agent-scope-elsewhere"');
    expect(html).not.toContain('name="api-key-agent-scope"');
  });

  it("Agent の画面では使える業務 Agent を「すべて」か「選んだものだけ」から選ぶ（#1874）", () => {
    const html = render(
      <MyApiKeysPanel
        api={myApi}
        endpoints={[]}
        agentScopeOptions={[
          { id: "sales", name: "営業の Agent" },
          { id: "hr", name: "人事の Agent" },
        ]}
      />,
      { mine: [] }
    );
    expect(html).toContain("使える業務 Agent");
    expect(html).toContain('role="radiogroup"');
    // 既定は「すべての業務 Agent」。業務 Agent の一覧は「選んだ業務 Agent だけ」のときに出す。
    expect(html).toMatch(/checked="" value="all"/);
    expect(html).not.toContain('data-testid="api-key-agent-options"');
    expect(html).not.toContain('data-testid="api-key-agent-scope-elsewhere"');
  });

  it("対象を絞ったキーは、使える製品の列に件数を添える（#1874）", () => {
    const html = render(<MyApiKeysPanel api={myApi} endpoints={[]} />, {
      mine: [{ ...KEY, products: ["rag", "agent"], resource_scopes: { agent: ["sales", "hr"] } }],
    });
    expect(html).toContain("RAG・Agent（2 件に限定）");
  });

  it("公開の URL があれば、この画面の URL と公開の URL を製品ごとに出し、CA の案内を添える（#1870）", () => {
    const html = render(<MyApiKeysPanel api={myApi} endpoints={mcpEndpoints("nl2sql", "/", "http://localhost:5174")} />, {
      mine: [],
      publicBaseUrl: "https://203.0.113.10:8443",
    });
    expect(html).toContain("http://localhost:5174/api/mcp");
    expect(html).toContain("https://203.0.113.10:8443/rag/api/mcp");
    expect(html).toContain("https://203.0.113.10:8443/nl2sql/api/mcp");
    expect(html).toContain("公開の URL（OCI）");
    expect(html).toContain('aria-label="NL2SQLの公開の URL（OCI）: URL をコピー"');
    expect(html).toContain("https://203.0.113.10:8443/platform/ca.crt");
    expect(html).toContain('data-testid="api-keys-ca-note"');
    // 配備した画面で開いて、この画面の URL と公開の URL が同じなら 1 行。
    const same = render(
      <MyApiKeysPanel api={myApi} endpoints={mcpEndpoints("rag", "/rag/", "https://203.0.113.10:8443")} />,
      { mine: [], publicBaseUrl: "https://203.0.113.10:8443" }
    );
    expect(same).toContain("この画面の URL（公開の URL と同じ）");
    expect(same).not.toContain("公開の URL（OCI）");
    expect(same.match(/URL をコピー<\/span><\/button>/g)).toHaveLength(3);
  });

  it("上限の件数に達したら発行を止めて案内する", () => {
    const keys = Array.from({ length: 10 }, (_, index) => ({ ...KEY, id: `key-${index}` }));
    const html = render(<MyApiKeysPanel api={myApi} endpoints={[]} />, { mine: keys });
    expect(html).toContain("API キーは 1 人 10 件までです。");
  });

  it("読み込み中は経過時間の表示と表の形の Skeleton を出す", () => {
    const html = render(<MyApiKeysPanel api={myApi} endpoints={[]} />);
    expect(html).toContain('data-testid="api-keys-loading"');
    expect(html).toContain("API キーを読み込んでいます");
  });

  it("管理の画面は全員のキーを利用者付きで出し、発行の欄は持たない", () => {
    const html = render(<ApiKeysAdminPage api={adminApi} />, {
      all: [KEY, { ...KEY, id: "x", owner_display_name: "", expired: true }],
    });
    expect(html).toContain("全員の API キー");
    expect(html).toContain("山田");
    expect(html).toContain("削除されたユーザー");
    expect(html).toContain("期限切れ");
    expect(html).not.toContain("API キーを発行</");
  });
});
