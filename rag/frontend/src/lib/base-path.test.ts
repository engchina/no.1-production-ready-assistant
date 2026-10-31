import { afterEach, describe, expect, it, vi } from "vitest";

import type { DocumentElement } from "./api";

/** build 時の base（`import.meta.env.BASE_URL`）を差し替えて、base を読む module を読み直す（#1316）。 */
async function loadWithBase(base: string) {
  vi.stubEnv("BASE_URL", base);
  vi.resetModules();
  const [basePath, apiModule, chatStream, searchStream, layoutElement] = await Promise.all([
    import("./base-path"),
    import("./api"),
    import("./chat-stream"),
    import("./search-stream"),
    import("./layout-element"),
  ]);
  return { ...basePath, ...apiModule, ...chatStream, ...searchStream, ...layoutElement };
}

function envelopeResponse(data: unknown): Response {
  return new Response(JSON.stringify({ data, error_messages: [], warning_messages: [] }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function sseDone(): Response {
  return new Response(`event: done\ndata: ${JSON.stringify({ trace_id: "t1" })}\n\n`, {
    status: 200,
    headers: { "content-type": "text/event-stream" },
  });
}

const FIGURE: DocumentElement = {
  kind: "figure",
  text: "",
  order: 0,
  page_number: 1,
  bbox: [0, 0, 10, 10],
  metadata: { page_width: 100, page_height: 100 },
} as DocumentElement;

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe("配信の base（Issue 1316）", () => {
  it("base が / のときは今までどおり /api/... を送り、router の basename を付けない", async () => {
    const mod = await loadWithBase("/");
    const fetchMock = vi.fn().mockImplementation(async () => envelopeResponse({ available: true }));
    vi.stubGlobal("fetch", fetchMock);

    await mod.api.getDatabaseStatus();

    expect(mod.APP_BASE_PATH).toBe("/");
    expect(mod.ROUTER_BASENAME).toBeUndefined();
    expect(mod.appPath("/api/x")).toBe("/api/x");
    expect(fetchMock).toHaveBeenCalledWith("/api/ready/database", expect.anything());
    expect(mod.api.documentContentUrl("d1")).toBe("/api/documents/d1/content");
    expect(mod.elementCropUrl("d1", FIGURE)).toMatch(/^\/api\/documents\/d1\/crop\?/u);
  });

  it("base が /rag/ のときは送る request と URL を /rag/api/... にし、router の basename を /rag にする", async () => {
    const mod = await loadWithBase("/rag/");
    const fetchMock = vi.fn().mockImplementation(async () => envelopeResponse({ available: true }));
    vi.stubGlobal("fetch", fetchMock);

    await mod.api.getDatabaseStatus();

    expect(mod.APP_BASE_PATH).toBe("/rag/");
    expect(mod.ROUTER_BASENAME).toBe("/rag");
    expect(fetchMock).toHaveBeenCalledWith("/rag/api/ready/database", expect.anything());
    // 何度通しても 1 回だけ付き、外部の URL・blob・data は変えない。
    expect(mod.appPath(mod.appPath("/api/x"))).toBe("/rag/api/x");
    expect(mod.appPath("https://example.com/a")).toBe("https://example.com/a");
    expect(mod.appPath("blob:https://example.com/1")).toBe("blob:https://example.com/1");
    expect(mod.appPath("data:image/png;base64,AA")).toBe("data:image/png;base64,AA");
  });

  it("base が /rag/ のとき、href / src / iframe / ダウンロードの URL を作る関数は /rag/api/... を返す", async () => {
    const mod = await loadWithBase("/rag/");

    expect(mod.api.documentContentUrl("d1", { disposition: "attachment" })).toBe(
      "/rag/api/documents/d1/content?disposition=attachment"
    );
    expect(mod.api.documentRecipeContentUrl("d1", "r1")).toBe("/rag/api/documents/d1/recipes/r1/content");
    expect(mod.api.documentPreviewPageImageUrl("d1", 2, { dpi: 144 })).toBe(
      "/rag/api/documents/d1/preview-pages/2?dpi=144"
    );
    expect(mod.api.documentRecipeExtractionExportUrl("d1", "r1", "markdown")).toBe(
      "/rag/api/documents/d1/recipes/r1/extraction-export?format=markdown&download=true"
    );
    expect(mod.elementCropUrl("d1", FIGURE)).toMatch(/^\/rag\/api\/documents\/d1\/crop\?/u);
  });

  it("base が /rag/ のとき、検索とチャットの SSE も /rag/api/... へ送る", async () => {
    const mod = await loadWithBase("/rag/");
    const fetchMock = vi.fn().mockImplementation(async () => sseDone());
    vi.stubGlobal("fetch", fetchMock);

    await mod.streamSearch({ query: "q" }, {});
    await mod.streamChatMessage("c1", { content: "q" } as never, {}).catch(() => undefined);
    await mod.resumeChatStream("c1", "m1", 3, {}).catch(() => undefined);

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/rag/api/search/stream",
      "/rag/api/chat/conversations/c1/messages/stream",
      "/rag/api/chat/conversations/c1/messages/m1/stream",
    ]);
  });
});
