import { defineConfig, devices } from "@playwright/test";
import { fileURLToPath } from "node:url";

import { ciRetries, ciWorkers } from "../../platform/scripts/e2e-impact/ci-run";
import { mobileProjectScope } from "../../platform/scripts/e2e-impact/mobile-scope";
import { nightlyOnlyIgnore } from "../../platform/scripts/e2e-impact/nightly-only";

const frontendPort = process.env.PLAYWRIGHT_PORT ?? "3042";
const frontendUrl = `http://127.0.0.1:${frontendPort}`;
const frontendCwd = fileURLToPath(new URL(".", import.meta.url));
// PLAYWRIGHT_WEB_SERVER=preview なら、vite build の成果物を vite preview で配信する（PR の CI。#1583）。
// dev サーバより画面の読み込みが速い。e2e の影響分析の記録（E2E_IMPACT_DIR。nightly）は dev サーバの
// `/src/...` の URL と inline の sourcemap を使うので、記録するときは dev サーバのままにする。
const usePreviewServer = process.env.PLAYWRIGHT_WEB_SERVER === "preview" && !process.env.E2E_IMPACT_DIR;
// 成果物は配備用の dist/ と分ける（node_modules の下なので commit されない）。
const previewOutDir = "node_modules/.e2e-preview";
const webServerCommand = usePreviewServer
  ? `npx vite build --outDir ${previewOutDir} --emptyOutDir --logLevel warn && npx vite preview --outDir ${previewOutDir} --host 127.0.0.1 --port ${frontendPort} --strictPort`
  : `npm run dev -- --host 127.0.0.1 --port ${frontendPort} --strictPort`;

export default defineConfig({
  testDir: "./e2e",
  // nightly だけで実行する spec（*.nightly.spec.ts。主要な画面の axe）は、ci.yml（E2E_NIGHTLY_ONLY=skip）の PR の e2e で
  // 実行しない（#1595。platform/scripts/e2e-impact/nightly-only.ts）。
  testIgnore: nightlyOnlyIgnore(),
  // 起動した dev サーバが hermetic であることを確認し、実行前後で利用者の実環境の
  // 設定ファイルが変わっていないことを検査する。
  globalSetup: "./e2e/global-setup.ts",
  fullyParallel: false,
  // CI（4 vCPU の runner）は CI_WORKERS の worker で spec のファイル単位で並べる（#1489・#1593）。手元は従来どおり 1 worker。
  workers: ciWorkers(1),
  // RAG / NL2SQL と同じく CI だけ retry する（#339）。遅い runner で 1 件の一時的な遅延が CI 全体を赤にしないため。
  // PR は 1、main の push と nightly は 2（#1593。platform/scripts/e2e-impact/ci-run.ts）。
  // retries が無いと trace: "on-first-retry" も記録されない。
  retries: ciRetries(),
  // CI は失敗・flaky を PR の注釈（github）に、log を dot にし、json は nightly の flaky の一覧に使う
  // （platform/scripts/playwright_report_summary.py。#1591）。
  reporter: process.env.CI
    ? [["github"], ["dot"], ["json", { outputFile: "playwright-report/results.json" }]]
    : "list",
  timeout: 30_000,
  expect: {
    timeout: 7_500,
  },
  use: {
    baseURL: frontendUrl,
    trace: "on-first-retry",
  },
  // e2e は実 backend を起動しない。すべての API は e2e/fixtures/mock-api.ts の page.route で
  // mock し、未モックの /api は Vite の hermetic middleware が 404 で終端する。
  webServer: {
    command: webServerCommand,
    cwd: frontendCwd,
    url: frontendUrl,
    // 既存の（hermetic でない）dev サーバを再利用すると実 backend へ proxy されるため、常に起動する。
    reuseExistingServer: false,
    // preview は先に vite build を行う（数秒）。
    timeout: usePreviewServer ? 120_000 : 30_000,
    env: {
      ...process.env,
      PLAYWRIGHT_HERMETIC_API: "1",
      // base は FRONTEND_BASE_PATH で変わるので、e2e では空にして `/` で配信する（dev サーバと preview）。
      FRONTEND_BASE_PATH: "",
      // 念のため proxy 先も到達不能な宛先にしておく（hermetic では proxy 自体を無効化している）。
      BACKEND_URL: "http://127.0.0.1:9",
    },
  },
  // RAG / NL2SQL と同じく desktop と mobile-375 の 2 project（#823。名前は NL2SQL にそろえる）。
  // mobile-375 はタッチ端末（Pixel 5、375×812、isMobile）で、操作部品が 44px になる。
  projects: [
    {
      name: "desktop",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "mobile-375",
      use: {
        ...devices["Pixel 5"],
        viewport: { width: 375, height: 812 },
        isMobile: true,
      },
      // 画面幅に関係しない spec（vite の設定・base path・フォントの読み込み・権限カタログの一致）は desktop だけで実行する。
      testIgnore: [
        "**/vite-config.spec.ts",
        "**/base-path.spec.ts",
        "**/self-hosted-fonts.spec.ts",
        "**/permission-catalog.spec.ts",
        // project の testIgnore は config の testIgnore を上書きするので、ここにも足す（#1595）。
        ...nightlyOnlyIgnore(),
      ],
      // spec の中で viewport を回しているテスト（題名に「(desktop」「（mobile）」「mobile-375: 」や「1920px」を含む）は、
      // その中で 375px を確かめているので二重に実行しない。
      grepInvert: /[(（](desktop|mobile)|(desktop|mobile-375): |\d{4}px/,
      // PR の CI は E2E_MOBILE_SCOPE=responsive で、画面幅に依存するテスト（@responsive など）だけを実行する。
      // nightly とローカルは全件（#1582。platform/scripts/e2e-impact/mobile-scope.ts）。
      ...mobileProjectScope(),
    },
  ],
});
