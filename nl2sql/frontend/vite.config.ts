import react from "@vitejs/plugin-react";
import { thirdPartyLicenses } from "@production-ready/ui/vite/third-party-licenses";
import { defineConfig } from "vite";
import type { IncomingMessage, ServerResponse } from "node:http";
import { fileURLToPath, URL } from "node:url";

const MISSING_BACKEND_URL_WARNING =
  "[nl2sql] BACKEND_URL が未設定のため /api を proxy せず 404 を返します（hermetic）。backend に接続する場合は BACKEND_URL=http://127.0.0.1:8010 npm run dev のように明示してください。";

/**
 * 配信のパスの前置き（#1316）。1 台の Compute で 3 製品を前段の reverse proxy（利用者が置く。#19）の `/rag/` `/nl2sql/` `/agent/` に分けて
 * 配信するときに、build の環境変数 `FRONTEND_BASE_PATH=/nl2sql/` で渡す。未設定・空なら `/`（今までどおり）。
 * 先頭と末尾の `/` は補う（`nl2sql` → `/nl2sql/`）。dev サーバ（e2e を含む）は `/` のまま動かす。
 */
export function resolveFrontendBasePath(value: string | undefined): string {
  const trimmed = (value ?? "").trim().replace(/^\/+|\/+$/g, "");
  return trimmed ? `/${trimmed}/` : "/";
}

/** hermetic の e2e で、未モックの /api を 404 で終端する middleware（dev サーバと preview で共有する）。 */
function hermeticApiNotFound(req: IncomingMessage, res: ServerResponse) {
  const path = `/api${req.url ?? ""}`;
  res.statusCode = 404;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.end(
    JSON.stringify({
      data: null,
      error_messages: [`e2e unmocked API: ${req.method ?? "GET"} ${path}`],
      warning_messages: [],
    })
  );
}

export default defineConfig(({ command, isPreview }) => {
  // /api は BACKEND_URL を明示したときだけ proxy する。既定の接続先（localhost:8010 等）は持たない。
  // 未設定のまま起動した dev サーバ（古い worktree、globalSetup の無い Playwright 設定、手動起動）が
  // 利用者の実 backend / Oracle 環境へ接続することを防ぐ。
  const backendUrl = process.env.BACKEND_URL?.trim() || undefined;
  // Playwright e2e は全 API を page.route で mock する前提（hermetic）。PLAYWRIGHT_HERMETIC_API=1 なら
  // BACKEND_URL があっても proxy しない。
  const hermeticApi = process.env.PLAYWRIGHT_HERMETIC_API === "1" || !backendUrl;
  const warnMissingBackendUrl = process.env.PLAYWRIGHT_HERMETIC_API !== "1" && !backendUrl;

  return {
    // build と preview（build の成果物の確認）だけに適用する。dev サーバ（e2e を含む）は常に `/` で動かし、
    // /api の proxy・hermetic の middleware の経路を変えない。
    base:
      command === "build" || isPreview
        ? resolveFrontendBasePath(process.env.FRONTEND_BASE_PATH)
        : "/",
    plugins: [
      react(),
      // 書体（@fontsource）の OFL の原文と bundle の npm の package のライセンスを dist/licenses/ に出す（#3392）。
      thirdPartyLicenses(),
      {
        name: "nl2sql-playwright-hermetic-api",
        configureServer(server) {
          if (!hermeticApi) return;
          if (warnMissingBackendUrl) server.config.logger.warn(MISSING_BACKEND_URL_WARNING);
          server.middlewares.use("/api", hermeticApiNotFound);
        },
        // PR の CI の e2e は build の成果物を `vite preview` で配信する（#1583）。preview でも同じく終端する。
        configurePreviewServer(server) {
          if (!hermeticApi) return;
          if (warnMissingBackendUrl) server.config.logger.warn(MISSING_BACKEND_URL_WARNING);
          server.middlewares.use("/api", hermeticApiNotFound);
        },
      },
    ],
    resolve: {
      alias: {
        "@": fileURLToPath(new URL("./src", import.meta.url)),
      },
      // 共有 UI パッケージ（file: リンク）の React 重複を防ぐ（"Invalid hook call" 回避）。
      // 共有システム設定パッケージが Router / React Query の context を共有できるよう、これらも 1 コピーへ集約する（#97）。
      dedupe: ["react", "react-dom", "react-router-dom", "@tanstack/react-query"],
    },
    server: {
      host: "0.0.0.0",
      port: 3001,
      // hermetic では proxy 自体を持たない（未モック API は上の middleware が 404 で終端する）。
      proxy: hermeticApi
        ? undefined
        : {
            "/api": { target: backendUrl, changeOrigin: true },
          },
    },
    preview: {
      host: "0.0.0.0",
      port: 3001,
    },
  };
});
