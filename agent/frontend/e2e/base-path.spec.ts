/**
 * 配信の基点（base path。#1316）を backend への URL・利用者に見せる URL に付けるロジックの確認。
 * 1 台の Compute に 3 製品を置く配備では `/agent/` で build し、ローカルの開発・e2e は `/` のまま。
 * Agent の frontend は Vitest を持たないため、画面を開かずに Playwright の test で関数を確かめる。
 */
import { APP_BASE_PATH, ROUTER_BASENAME, appPath } from "../src/lib/base-path";
import { editorItemHref } from "../src/lib/editor-route";
import { runEventStreamUrl } from "../src/pages/runs/run-event-urls";

import { expect, test } from "./fixtures/test";

test.describe("base path（#1316）", () => {
  test("Vite の BASE_URL が無い（ローカル・e2e）ときは `/` で、Router の basename は付けない", () => {
    expect(APP_BASE_PATH).toBe("/");
    expect(ROUTER_BASENAME).toBeUndefined();
  });

  test("base が `/` なら API の path は今までどおり", () => {
    expect(appPath("/api/runs", "/")).toBe("/api/runs");
    expect(runEventStreamUrl("run 1", "/")).toBe("/api/runs/run%201/events?follow=true");
    expect(editorItemHref("/agents", "q=sales", "agent-1", "/")).toBe("/agents?q=sales&id=agent-1");
  });

  test("base が `/agent/` なら API・SSE・リンクの URL に `/agent` を付ける", () => {
    expect(appPath("/api/runs", "/agent/")).toBe("/agent/api/runs");
    // 何度通しても 1 回だけ付く（作る所と送る所の両方で通してよい）。
    expect(appPath(appPath("/api/runs", "/agent/"), "/agent/")).toBe("/agent/api/runs");
    // 外の URL・blob は変えない。
    expect(appPath("https://rag.example.com/api/mcp", "/agent/")).toBe("https://rag.example.com/api/mcp");
    expect(appPath("blob:https://agent.example.com/1", "/agent/")).toBe("blob:https://agent.example.com/1");

    expect(runEventStreamUrl("run 1", "/agent/")).toBe("/agent/api/runs/run%201/events?follow=true");
    expect(editorItemHref("/agents", "q=sales", "agent-1", "/agent/")).toBe("/agent/agents?q=sales&id=agent-1");
  });
});
