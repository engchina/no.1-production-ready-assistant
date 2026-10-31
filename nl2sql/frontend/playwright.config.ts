import { defineConfig, devices } from "@playwright/test";

import { ciRetries, ciWorkers } from "../../platform/scripts/e2e-impact/ci-run";
import { mobileProjectScope } from "../../platform/scripts/e2e-impact/mobile-scope";
import { nightlyOnlyIgnore } from "../../platform/scripts/e2e-impact/nightly-only";
import {
  APP_BASE_URL,
  FIXTURE_BASE_URL,
  FIXTURE_PORT,
  PLAYWRIGHT_PORT,
  USE_PREVIEW_SERVER,
} from "./tests/e2e/_helpers/web-server";

// e2e は全 API を page.route で mock する前提(hermetic)。未モック API は
// Vite 側の 404 middleware（dev サーバと preview）で終端し、実 backend / Oracle 環境へ接続しない。
// preview の base は FRONTEND_BASE_PATH で変わるので、e2e では空にして `/` で配信する。
const webServerEnv = { ...process.env, PLAYWRIGHT_HERMETIC_API: "1", FRONTEND_BASE_PATH: "" };
const devServer = (port: string, url: string) => ({
  command: `npm run dev -- --host 127.0.0.1 --port ${port}`,
  url,
  reuseExistingServer: !process.env.CI,
  timeout: 120_000,
  env: webServerEnv,
});
// PLAYWRIGHT_WEB_SERVER=preview（PR の CI。#1583）は画面を vite build の成果物の vite preview で配信し、
// `tests/fixtures/*.tsx` を描く spec のために dev server も起動する（tests/e2e/_helpers/web-server.ts）。
// build の成果物は配備用の dist/ と分ける（node_modules の下なので commit されない）。
const previewOutDir = "node_modules/.e2e-preview";
const webServer = USE_PREVIEW_SERVER
  ? [
      {
        command: `npx vite build --outDir ${previewOutDir} --emptyOutDir --logLevel warn && npx vite preview --outDir ${previewOutDir} --host 127.0.0.1 --port ${PLAYWRIGHT_PORT} --strictPort`,
        url: APP_BASE_URL,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
        env: webServerEnv,
      },
      devServer(FIXTURE_PORT, FIXTURE_BASE_URL),
    ]
  : devServer(PLAYWRIGHT_PORT, APP_BASE_URL);

export default defineConfig({
  testDir: "./tests/e2e",
  // nightly だけで実行する spec（*.nightly.spec.ts。主要な画面の axe）は、ci.yml（E2E_NIGHTLY_ONLY=skip）の PR の e2e で
  // 実行しない（#1595。platform/scripts/e2e-impact/nightly-only.ts）。
  testIgnore: nightlyOnlyIgnore(),
  // 対象の dev サーバ・preview（再利用したものを含む）が hermetic であることを確認してから開始し、
  // 実行前後で利用者の実環境の設定ファイルが変わっていないことを検査する。
  globalSetup: "./tests/e2e/global-setup.ts",
  timeout: 30_000,
  // RAG と同じく CI だけ retry する。遅い runner で 1 件の一時的な遅延が shard 全体を赤にしないため（#175）。
  // PR は 1、main の push と nightly は 2（#1593。platform/scripts/e2e-impact/ci-run.ts）。
  // retry で通ったテストは flaky として report に残る。nightly（e2e-nightly.yml）でも拾う。
  retries: ciRetries(),
  // CI（4 vCPU の runner）は CI_WORKERS の worker。ci.yml・e2e-nightly.yml は `--workers` で上書きしない（#1489・#1593）。
  workers: ciWorkers(4),
  expect: {
    timeout: 8_000,
  },
  fullyParallel: true,
  // CI は失敗・flaky を PR の注釈（github）に、log を dot にし、json は nightly の flaky の一覧に使う
  // （platform/scripts/playwright_report_summary.py。#1591）。
  reporter: process.env.CI
    ? [["github"], ["dot"], ["json", { outputFile: "playwright-report/results.json" }]]
    : "list",
  use: {
    baseURL: APP_BASE_URL,
    trace: "retain-on-failure",
  },
  webServer,
  projects: [
    {
      name: "desktop",
      use: {
        ...devices["Desktop Chrome"],
        viewport: { width: 1280, height: 900 },
      },
    },
    {
      name: "mobile-375",
      use: {
        ...devices["Pixel 5"],
        viewport: { width: 375, height: 812 },
        isMobile: true,
      },
      // PR の CI は E2E_MOBILE_SCOPE=responsive で、画面幅に依存するテスト（@responsive など）だけを実行する。
      // nightly とローカルは全件（#1582。platform/scripts/e2e-impact/mobile-scope.ts）。
      ...mobileProjectScope(),
    },
  ],
});
