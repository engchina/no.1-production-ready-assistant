import assert from "node:assert/strict";
import test from "node:test";
import type { Plugin, UserConfig, ViteDevServer } from "vite";

import viteConfig from "../vite.config.ts";

// BACKEND_URL 未設定で既定の backend（localhost:8010 等）へ proxy する fallback を戻さないための契約テスト。
function resolveConfig(env: { BACKEND_URL?: string; PLAYWRIGHT_HERMETIC_API?: string }): UserConfig {
  const saved = { BACKEND_URL: process.env.BACKEND_URL, PLAYWRIGHT_HERMETIC_API: process.env.PLAYWRIGHT_HERMETIC_API };
  for (const key of ["BACKEND_URL", "PLAYWRIGHT_HERMETIC_API"] as const) {
    if (env[key] === undefined) delete process.env[key];
    else process.env[key] = env[key];
  }
  try {
    assert.equal(typeof viteConfig, "function", "vite.config.ts は env を解決時に読む関数形式で export する");
    return (viteConfig as (env: { command: "serve"; mode: string }) => UserConfig)({ command: "serve", mode: "development" });
  } finally {
    for (const key of ["BACKEND_URL", "PLAYWRIGHT_HERMETIC_API"] as const) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
  }
}

// dev サーバ（configureServer）と、PR の CI の e2e が使う preview（configurePreviewServer。#1583）の両方を確かめる。
function startDevServer(config: UserConfig, hook: "configureServer" | "configurePreviewServer" = "configureServer") {
  const plugin = config.plugins?.find(
    (p): p is Plugin => typeof p === "object" && p !== null && "name" in p && p.name === "nl2sql-playwright-hermetic-api",
  );
  const middlewarePaths: string[] = [];
  const warnings: string[] = [];
  const server = {
    middlewares: { use: (path: string) => middlewarePaths.push(path) },
    config: { logger: { warn: (message: string) => warnings.push(message) } },
  };
  if (!plugin) throw new Error("hermetic API plugin が見つかりません");
  (plugin[hook] as (server: ViteDevServer) => void)(server as unknown as ViteDevServer);
  return { middlewarePaths, warnings };
}

test("BACKEND_URL 未設定なら /api を proxy せず、hermetic middleware と警告を出す", () => {
  const config = resolveConfig({});
  assert.equal(config.server?.proxy, undefined);
  const { middlewarePaths, warnings } = startDevServer(config);
  assert.deepEqual(middlewarePaths, ["/api"]);
  assert.equal(warnings.length, 1);
});

test("BACKEND_URL を明示したときだけその URL へ proxy する", () => {
  const config = resolveConfig({ BACKEND_URL: "http://127.0.0.1:18999" });
  assert.deepEqual(config.server?.proxy, { "/api": { target: "http://127.0.0.1:18999", changeOrigin: true } });
  assert.deepEqual(startDevServer(config).middlewarePaths, []);
});

test("PLAYWRIGHT_HERMETIC_API=1 なら BACKEND_URL があっても proxy せず、警告も出さない", () => {
  const config = resolveConfig({ BACKEND_URL: "http://127.0.0.1:18999", PLAYWRIGHT_HERMETIC_API: "1" });
  assert.equal(config.server?.proxy, undefined);
  const { middlewarePaths, warnings } = startDevServer(config);
  assert.deepEqual(middlewarePaths, ["/api"]);
  assert.deepEqual(warnings, []);
});

test("vite preview（PR の CI の e2e。#1583）でも、hermetic なら /api を 404 の middleware で終端する", () => {
  assert.deepEqual(
    startDevServer(resolveConfig({ PLAYWRIGHT_HERMETIC_API: "1" }), "configurePreviewServer").middlewarePaths,
    ["/api"],
  );
  const { middlewarePaths, warnings } = startDevServer(resolveConfig({}), "configurePreviewServer");
  assert.deepEqual(middlewarePaths, ["/api"]);
  assert.equal(warnings.length, 1);
  assert.deepEqual(
    startDevServer(resolveConfig({ BACKEND_URL: "http://127.0.0.1:18999" }), "configurePreviewServer").middlewarePaths,
    [],
  );
});
