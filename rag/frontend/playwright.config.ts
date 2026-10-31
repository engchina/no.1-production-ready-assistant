import { defineConfig, devices } from "@playwright/test";

import { ciRetries, ciWorkers } from "../../platform/scripts/e2e-impact/ci-run";
import { mobileProjectScope } from "../../platform/scripts/e2e-impact/mobile-scope";
import { nightlyOnlyIgnore } from "../../platform/scripts/e2e-impact/nightly-only";

const skipWebServer = process.env.PLAYWRIGHT_SKIP_WEB_SERVER === "1";
// 開発用 dev サーバ（3100）と衝突しない既定ポート（#25）。
const port = process.env.PLAYWRIGHT_PORT ?? "3200";
const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? `http://127.0.0.1:${port}`;
// PLAYWRIGHT_WEB_SERVER=preview なら、vite build の成果物を vite preview で配信する（PR の CI。#1583）。
// dev サーバより画面の読み込みが速い。e2e の影響分析の記録（E2E_IMPACT_DIR。nightly）は dev サーバの
// `/src/...` の URL と inline の sourcemap を使うので、記録するときは dev サーバのままにする。
const usePreviewServer = process.env.PLAYWRIGHT_WEB_SERVER === "preview" && !process.env.E2E_IMPACT_DIR;
// 成果物は配備用の dist/ と分ける（node_modules の下なので commit されない）。
const previewOutDir = "node_modules/.e2e-preview";
const webServerCommand = usePreviewServer
  ? `npx vite build --outDir ${previewOutDir} --emptyOutDir --logLevel warn && npx vite preview --outDir ${previewOutDir} --host 127.0.0.1 --port ${port} --strictPort`
  : `npm run dev -- --host 127.0.0.1 --port ${port} --strictPort`;

export default defineConfig({
  testDir: "./e2e",
  // nightly だけで実行する spec（*.nightly.spec.ts。主要な画面の axe）は、ci.yml（E2E_NIGHTLY_ONLY=skip）の PR の e2e で
  // 実行しない（#1595。platform/scripts/e2e-impact/nightly-only.ts）。
  testIgnore: nightlyOnlyIgnore(),
  // 起動した dev サーバが hermetic であることを確認し、実行前後で利用者の実環境の
  // 設定ファイルが変わっていないことを検査する。
  globalSetup: "./e2e/global-setup.ts",
  timeout: 30_000,
  // CI（4 vCPU の runner）は CI_WORKERS の worker で spec のファイル単位で並べる（#1489・#1593）。手元は従来どおり 1 worker。
  workers: ciWorkers(1),
  // CI はヘッドレス環境のタイミング差で e2e が稀に flake するため retry する。PR は 1、main の push と nightly は 2
  // （#1593。platform/scripts/e2e-impact/ci-run.ts）。
  // retries 未設定だと trace: "on-first-retry" も機能せず、1 件の flake で CI 全体が赤になる。
  retries: ciRetries(),
  expect: {
    timeout: 5_000,
  },
  // CI は失敗・flaky を PR の注釈（github）に、log を dot にし、json は nightly の flaky の一覧に使う
  // （platform/scripts/playwright_report_summary.py。#1591）。
  reporter: process.env.CI
    ? [["github"], ["dot"], ["json", { outputFile: "playwright-report/results.json" }]]
    : "list",
  use: {
    baseURL,
    trace: "on-first-retry",
  },
  webServer: skipWebServer
    ? undefined
    : {
        command: webServerCommand,
        url: baseURL,
        // 既存の（hermetic でない）dev サーバを再利用すると実 backend へ proxy されるため、常に起動する。
        reuseExistingServer: false,
        timeout: 120_000,
        // e2e は全 API を page.route で mock する前提（hermetic）。未モック API は Vite 側の
        // 404 middleware（dev サーバと preview）で終端し、実 backend / Oracle 環境へ接続しない。
        // base は FRONTEND_BASE_PATH で変わるので、e2e では空にして `/` で配信する。
        env: { ...process.env, PLAYWRIGHT_HERMETIC_API: "1", BACKEND_URL: "http://127.0.0.1:9", FRONTEND_BASE_PATH: process.env.RAG_E2E_BASE_PATH ?? "" },
      },
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 900 } },
    },
    {
      name: "mobile",
      use: { ...devices["Pixel 5"], viewport: { width: 375, height: 812 } },
      // PR の CI は E2E_MOBILE_SCOPE=responsive で、画面幅に依存するテスト（@responsive など）だけを実行する。
      // nightly とローカルは全件（#1582。platform/scripts/e2e-impact/mobile-scope.ts）。
      ...mobileProjectScope(),
    },
  ],
});
