import { describe, expect, it } from "vitest";

import {
  isSecretQueryName,
  maskUrlCredentials,
  mcpUrlCredentialProblem,
  secretQueryNames,
} from "./mcp-url";

// 規則は backend（app/features/agent/mcp_url.py）と同じ（#1056）。画面の検証は先回りなので、ずれると
// 画面が通した URL を backend が拒否する（または逆に、資格情報を書いた URL を画面が見逃す）。

describe("isSecretQueryName", () => {
  it.each(["api_key", "apiKey", "X-Api-Key", "access_token", "PASSWORD", "sig", "client.secret"])(
    "%s は資格情報らしい名前",
    (name) => {
      expect(isSecretQueryName(name)).toBe(true);
    }
  );

  it.each(["page", "limit", "keyword", "tokenizer", "monkey", "author", ""])("%s は資格情報ではない", (name) => {
    expect(isSecretQueryName(name)).toBe(false);
  });
});

describe("secretQueryNames", () => {
  it("資格情報らしい query の名前を、重複なしで出現順に返す", () => {
    expect(secretQueryNames("https://mcp.example.com/sse?token=a&page=2&api_key=b&token=c")).toEqual(["token", "api_key"]);
  });

  it("名前の URL エンコードと + を戻してから判定する", () => {
    expect(secretQueryNames("https://mcp.example.com/?x%2Dapi%2Dkey=a&access+token=b")).toEqual(["x-api-key", "access token"]);
  });

  it("scheme の無い文字列・query の無い URL は空", () => {
    expect(secretQueryNames("mcp.example.com/?token=a")).toEqual([]);
    expect(secretQueryNames("https://mcp.example.com/sse")).toEqual([]);
  });
});

describe("mcpUrlCredentialProblem", () => {
  it("userinfo を先に検出する（値は返さない）", () => {
    expect(mcpUrlCredentialProblem(" https://user:pass@mcp.example.com/?token=a ")).toEqual({ kind: "userinfo" });
  });

  it("secret らしい query の名前を返す", () => {
    expect(mcpUrlCredentialProblem("https://mcp.example.com/mcp?apiKey=a")).toEqual({
      kind: "secretQuery",
      names: ["apiKey"],
    });
  });

  it("path の @ は userinfo とみなさない", () => {
    expect(mcpUrlCredentialProblem("https://mcp.example.com/users/@me?page=1")).toBeNull();
  });
});

describe("maskUrlCredentials", () => {
  it("userinfo と secret らしい query の値だけを *** にし、ほかは元の形のまま残す", () => {
    expect(maskUrlCredentials("https://user:p%40ss@mcp.example.com:8443/sse?page=2&token=abc&Sig=x#frag")).toBe(
      "https://***@mcp.example.com:8443/sse?page=2&token=***&Sig=***#frag"
    );
  });

  it("資格情報の無い URL・空の query・URL でない文字列は変えない", () => {
    expect(maskUrlCredentials("https://mcp.example.com/sse?")).toBe("https://mcp.example.com/sse?");
    expect(maskUrlCredentials("https://mcp.example.com/sse?page=2")).toBe("https://mcp.example.com/sse?page=2");
    expect(maskUrlCredentials("not a url ?token=a")).toBe("not a url ?token=a");
  });
});
