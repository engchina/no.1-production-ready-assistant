import type { Plugin, UserConfig, ViteDevServer } from "vite";
import { afterEach, describe, expect, it, vi } from "vitest";

import viteConfig from "../vite.config";

// BACKEND_URL 未設定で既定の backend（localhost:8000 等）へ proxy する fallback を戻さないための契約テスト。
function resolveConfig(env: { BACKEND_URL?: string; PLAYWRIGHT_HERMETIC_API?: string }): UserConfig {
  vi.stubEnv("BACKEND_URL", env.BACKEND_URL);
  vi.stubEnv("PLAYWRIGHT_HERMETIC_API", env.PLAYWRIGHT_HERMETIC_API);
  if (typeof viteConfig !== "function") throw new Error("vite.config.ts は env を解決時に読む関数形式で export する");
  return viteConfig({ command: "serve", mode: "development" }) as UserConfig;
}

function startDevServer(config: UserConfig) {
  const plugin = config.plugins?.find(
    (p): p is Plugin => typeof p === "object" && p !== null && "name" in p && p.name === "rag-playwright-hermetic-api"
  );
  const middlewarePaths: string[] = [];
  const warn = vi.fn();
  const server = { middlewares: { use: (path: string) => middlewarePaths.push(path) }, config: { logger: { warn } } };
  if (!plugin) throw new Error("hermetic API plugin が見つかりません");
  (plugin.configureServer as (server: ViteDevServer) => void)(server as unknown as ViteDevServer);
  return { middlewarePaths, warn };
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("vite.config /api proxy", () => {
  it("BACKEND_URL 未設定なら proxy せず、hermetic middleware と警告を出す", () => {
    const config = resolveConfig({});
    expect(config.server?.proxy).toBeUndefined();
    expect(config.preview?.proxy).toBeUndefined();
    const { middlewarePaths, warn } = startDevServer(config);
    expect(middlewarePaths).toEqual(["/api"]);
    expect(warn).toHaveBeenCalledTimes(1);
  });

  it("BACKEND_URL を明示したときだけその URL へ proxy する", () => {
    const config = resolveConfig({ BACKEND_URL: "http://127.0.0.1:18999" });
    expect(config.server?.proxy).toEqual({ "/api": { target: "http://127.0.0.1:18999", changeOrigin: true } });
    expect(config.preview?.proxy).toEqual({ "/api": { target: "http://127.0.0.1:18999", changeOrigin: true } });
    expect(startDevServer(config).middlewarePaths).toEqual([]);
  });

  it("PLAYWRIGHT_HERMETIC_API=1 なら BACKEND_URL があっても proxy せず、警告も出さない", () => {
    const config = resolveConfig({ BACKEND_URL: "http://127.0.0.1:18999", PLAYWRIGHT_HERMETIC_API: "1" });
    expect(config.server?.proxy).toBeUndefined();
    expect(config.preview?.proxy).toBeUndefined();
    const { middlewarePaths, warn } = startDevServer(config);
    expect(middlewarePaths).toEqual(["/api"]);
    expect(warn).not.toHaveBeenCalled();
  });
});
