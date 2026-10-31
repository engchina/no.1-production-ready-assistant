import { expect, test } from "./fixtures/test";
import type { Plugin, UserConfig, ViteDevServer } from "vite";

import viteConfig from "../vite.config";

// BACKEND_URL 未設定で既定の backend（localhost:8020 等）へ proxy する fallback を戻さないための契約テスト。
// frontend に unit test 基盤がないため、ブラウザを使わない Playwright test として vite.config.ts を直接評価する。
const ENV_KEYS = ["BACKEND_URL", "PLAYWRIGHT_HERMETIC_API", "FRONTEND_BASE_PATH"] as const;
type ProxyEnv = Partial<Record<(typeof ENV_KEYS)[number], string>>;

function resolveConfig(env: ProxyEnv): UserConfig {
  const saved: ProxyEnv = Object.fromEntries(ENV_KEYS.map((key) => [key, process.env[key]]));
  const apply = (values: ProxyEnv) => {
    for (const key of ENV_KEYS) {
      if (values[key] === undefined) delete process.env[key];
      else process.env[key] = values[key];
    }
  };
  apply(env);
  try {
    expect(typeof viteConfig, "vite.config.ts は env を解決時に読む関数形式で export する").toBe("function");
    return (viteConfig as (env: { command: "serve"; mode: string }) => UserConfig)({ command: "serve", mode: "development" });
  } finally {
    apply(saved);
  }
}

// dev サーバ（configureServer）と、PR の CI の e2e が使う preview（configurePreviewServer。#1583）の両方を確かめる。
function startDevServer(config: UserConfig, hook: "configureServer" | "configurePreviewServer" = "configureServer") {
  const plugin = config.plugins?.find(
    (p): p is Plugin => typeof p === "object" && p !== null && "name" in p && p.name === "agent-playwright-hermetic-api",
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

test.describe("vite.config /api proxy", () => {
  test("BACKEND_URL 未設定なら /api を proxy せず、hermetic middleware と警告を出す", () => {
    const config = resolveConfig({});
    expect(config.server?.proxy).toBeUndefined();
    const { middlewarePaths, warnings } = startDevServer(config);
    expect(middlewarePaths).toEqual(["/api"]);
    expect(warnings).toHaveLength(1);
  });

  test("BACKEND_URL を明示したときだけその URL へ proxy する", () => {
    const config = resolveConfig({ BACKEND_URL: "http://127.0.0.1:18999" });
    expect(config.server?.proxy).toEqual({ "/api": { target: "http://127.0.0.1:18999", changeOrigin: true } });
    expect(startDevServer(config).middlewarePaths).toEqual([]);
  });

  test("PLAYWRIGHT_HERMETIC_API=1 なら BACKEND_URL があっても proxy せず、警告も出さない", () => {
    const config = resolveConfig({ BACKEND_URL: "http://127.0.0.1:18999", PLAYWRIGHT_HERMETIC_API: "1" });
    expect(config.server?.proxy).toBeUndefined();
    const { middlewarePaths, warnings } = startDevServer(config);
    expect(middlewarePaths).toEqual(["/api"]);
    expect(warnings).toEqual([]);
  });

  test("vite preview（PR の CI の e2e。#1583）でも、hermetic なら /api を 404 の middleware で終端する", () => {
    expect(startDevServer(resolveConfig({ PLAYWRIGHT_HERMETIC_API: "1" }), "configurePreviewServer").middlewarePaths).toEqual(["/api"]);
    const { middlewarePaths, warnings } = startDevServer(resolveConfig({}), "configurePreviewServer");
    expect(middlewarePaths).toEqual(["/api"]);
    expect(warnings).toHaveLength(1);
    expect(
      startDevServer(resolveConfig({ BACKEND_URL: "http://127.0.0.1:18999" }), "configurePreviewServer").middlewarePaths
    ).toEqual([]);
  });
});

// 1 台の Compute に 3 製品を置く配備（#1316）は build の時だけ FRONTEND_BASE_PATH=/agent/ を渡す。
// 未指定ではローカルの開発・e2e と同じ `/` のまま（dev サーバ・proxy・hermetic middleware は変えない）。
test.describe("vite.config base（FRONTEND_BASE_PATH）", () => {
  test("FRONTEND_BASE_PATH 未指定・空なら base は `/`", () => {
    expect(resolveConfig({}).base).toBe("/");
    expect(resolveConfig({ FRONTEND_BASE_PATH: "  " }).base).toBe("/");
    expect(resolveConfig({ FRONTEND_BASE_PATH: "/" }).base).toBe("/");
  });

  test("FRONTEND_BASE_PATH は前後の `/` をそろえて base にし、dev の /api の扱いは変えない", () => {
    expect(resolveConfig({ FRONTEND_BASE_PATH: "/agent/" }).base).toBe("/agent/");
    expect(resolveConfig({ FRONTEND_BASE_PATH: "agent" }).base).toBe("/agent/");
    expect(resolveConfig({ FRONTEND_BASE_PATH: "//agent//" }).base).toBe("/agent/");
    const config = resolveConfig({ FRONTEND_BASE_PATH: "/agent/" });
    expect(config.server?.port).toBe(3002);
    expect(startDevServer(config).middlewarePaths).toEqual(["/api"]);
  });
});
