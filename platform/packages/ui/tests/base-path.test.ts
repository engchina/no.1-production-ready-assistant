import { describe, expect, it } from "vitest";
import { normalizeBasePath, routerBasename, stripBasePath, withBasePath } from "../src/lib/base-path";

describe("base path（#1316）", () => {
  it("base を / で始まり / で終わる形にそろえる", () => {
    expect(normalizeBasePath(undefined)).toBe("/");
    expect(normalizeBasePath("")).toBe("/");
    expect(normalizeBasePath("/")).toBe("/");
    expect(normalizeBasePath("./")).toBe("/");
    expect(normalizeBasePath("rag")).toBe("/rag/");
    expect(normalizeBasePath("/rag")).toBe("/rag/");
    expect(normalizeBasePath("/rag/")).toBe("/rag/");
    expect(normalizeBasePath("//nl2sql//")).toBe("/nl2sql/");
    expect(normalizeBasePath("///")).toBe("/");
  });

  it("`/` の長い連続でも時間内に終わる（#1646）", () => {
    const started = performance.now();
    expect(normalizeBasePath(`${"/".repeat(200_000)}rag${"/".repeat(200_000)}x`)).toBe(
      `/rag${"/".repeat(200_000)}x/`
    );
    expect(performance.now() - started).toBeLessThan(1000);
  });

  it("router の basename は / のとき undefined（今までどおり）", () => {
    expect(routerBasename("/")).toBeUndefined();
    expect(routerBasename(undefined)).toBeUndefined();
    expect(routerBasename("/agent/")).toBe("/agent");
  });

  it("base が / のときは何も変えない（ローカルの開発・e2e）", () => {
    expect(withBasePath("/api/health", "/")).toBe("/api/health");
    expect(withBasePath("/api/health", undefined)).toBe("/api/health");
    expect(stripBasePath("/settings/oci", "/")).toBe("/settings/oci");
  });

  it("アプリの絶対パスに base を 1 回だけ付ける", () => {
    expect(withBasePath("/api/health", "/rag/")).toBe("/rag/api/health");
    expect(withBasePath("/api/documents/a?x=1", "/rag/")).toBe("/rag/api/documents/a?x=1");
    expect(withBasePath("/rag/api/health", "/rag/")).toBe("/rag/api/health");
    expect(withBasePath(withBasePath("/api/x", "/rag/"), "/rag/")).toBe("/rag/api/x");
    expect(withBasePath("/", "/rag/")).toBe("/rag/");
    expect(withBasePath("/rag", "/rag/")).toBe("/rag");
    // 別の製品の prefix に見えても、自分の base で始まらなければ付ける（/ragx/ は /rag/ ではない）。
    expect(withBasePath("/ragx/api", "/rag/")).toBe("/rag/ragx/api");
  });

  it("絶対 URL・相対パス・blob / data は変えない", () => {
    expect(withBasePath("https://example.com/api", "/rag/")).toBe("https://example.com/api");
    expect(withBasePath("//example.com/api", "/rag/")).toBe("//example.com/api");
    expect(withBasePath("api/health", "/rag/")).toBe("api/health");
    expect(withBasePath("blob:https://x/1", "/rag/")).toBe("blob:https://x/1");
    expect(withBasePath("data:text/plain,a", "/rag/")).toBe("data:text/plain,a");
  });

  it("base を付けた path から base を外す", () => {
    expect(stripBasePath("/nl2sql/settings/oci", "/nl2sql/")).toBe("/settings/oci");
    expect(stripBasePath("/nl2sql", "/nl2sql/")).toBe("/");
    expect(stripBasePath("/nl2sql/", "/nl2sql/")).toBe("/");
    expect(stripBasePath("/nl2sql?x=1", "/nl2sql/")).toBe("/?x=1");
    expect(stripBasePath("/other/path", "/nl2sql/")).toBe("/other/path");
  });
});
