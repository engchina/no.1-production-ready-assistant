import { fileURLToPath, URL } from "node:url";

import { defineConfig } from "vitest/config";

// 純粋なロジック（src/lib の関数）の単体テスト（#1595。RAG の vitest.config.ts と同じ形）。
// 画面の振る舞いは Playwright の e2e（e2e/）が確かめるので、ここでは DOM を使わない（environment: node）。
export default defineConfig({
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
  resolve: {
    dedupe: ["react", "react-dom"],
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
});
