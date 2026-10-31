import react from "@vitejs/plugin-react";
import { thirdPartyLicenses } from "@production-ready/ui/vite/third-party-licenses";
import { defineConfig, type Connect } from "vite";
import { fileURLToPath, URL } from "node:url";

/**
 * 画面の配信の基点（Vite の `base`。#1316）。1 台の Compute に 3 製品を置く配備では、Nginx が `/agent/` で
 * Agent を分けるため、build の時だけ `FRONTEND_BASE_PATH=/agent/` を渡す。未指定・空は `/`（ローカルの開発・e2e は今までどおり）。
 * 前後の `/` はそろえる（`agent` → `/agent/`）。
 */
export function frontendBasePath(value: string | undefined): string {
  const trimmed = (value ?? "").trim().replace(/^\/+|\/+$/g, "");
  return trimmed ? `/${trimmed}/` : "/";
}

// 未モックの /api を 404 で終端する（実 backend へ proxy しない）。dev サーバと preview で共有する。
const hermeticApiMiddleware: Connect.NextHandleFunction = (req, res) => {
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
};

const MISSING_BACKEND_URL_WARNING =
  "[agent] BACKEND_URL が未設定のため /api を proxy せず 404 を返します（hermetic）。backend に接続する場合は BACKEND_URL=http://127.0.0.1:8120 npm run dev のように明示してください。";

export default defineConfig(() => {
  // /api は BACKEND_URL を明示したときだけ proxy する。既定の接続先（localhost:8120 等）は持たない。
  // 未設定のまま起動した dev サーバ（古い worktree、globalSetup の無い Playwright 設定、手動起動）が
  // 利用者の実 backend へ接続し、実環境の設定（~/.oci/config、backend/.env、backend/model-settings.json 等）へ
  // 書き込むことを防ぐ。
  const backendUrl = process.env.BACKEND_URL?.trim() || undefined;
  // Playwright e2e は全 API を page.route で mock する前提（hermetic）。PLAYWRIGHT_HERMETIC_API=1 なら
  // BACKEND_URL があっても proxy しない。
  const hermeticApi = process.env.PLAYWRIGHT_HERMETIC_API === "1" || !backendUrl;
  const warnMissingBackendUrl = process.env.PLAYWRIGHT_HERMETIC_API !== "1" && !backendUrl;

  return {
    base: frontendBasePath(process.env.FRONTEND_BASE_PATH),
    plugins: [
      react(),
      // 書体（@fontsource）の OFL の原文と bundle の npm の package のライセンスを dist/licenses/ に出す（#3392）。
      thirdPartyLicenses(),
      {
        name: "agent-playwright-hermetic-api",
        configureServer(server) {
          if (!hermeticApi) return;
          if (warnMissingBackendUrl) server.config.logger.warn(MISSING_BACKEND_URL_WARNING);
          // 未モックの /api は実 backend へ proxy せず 404 で終端する。
          server.middlewares.use("/api", hermeticApiMiddleware);
        },
        // PR の CI の e2e は build の成果物を `vite preview` で配信する（#1583）。preview でも同じく終端する。
        configurePreviewServer(server) {
          if (!hermeticApi) return;
          if (warnMissingBackendUrl) server.config.logger.warn(MISSING_BACKEND_URL_WARNING);
          server.middlewares.use("/api", hermeticApiMiddleware);
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
      port: 3102,
      // hermetic では proxy 自体を持たない（実 backend へ流さない）。
      proxy: hermeticApi
        ? undefined
        : {
            "/api": { target: backendUrl, changeOrigin: true },
          },
    },
    preview: {
      host: "0.0.0.0",
      port: 3102,
    },
  };
});
