/**
 * e2e が開く web server の URL（playwright.config.ts と spec で共有する。#1583）。
 *
 * - 既定は Vite の dev server を 1 つ（`PLAYWRIGHT_PORT`、既定 3101）。
 * - `PLAYWRIGHT_WEB_SERVER=preview`（PR の CI）は、画面を vite build の成果物の `vite preview` で配信する
 *   （dev server より画面の読み込みが速い）。ただし `tests/fixtures/*.tsx` を `/@react-refresh` と一緒に読み込んで
 *   部品だけを描く spec は dev server の変換が要るので、その spec のために dev server も 1 つ（`PLAYWRIGHT_PORT` + 1）
 *   起動し、spec は `test.use({ baseURL: FIXTURE_BASE_URL })` でそちらを開く。
 * - e2e の影響分析の記録（`E2E_IMPACT_DIR`。nightly）は dev server の `/src/...` の URL と inline の sourcemap を
 *   使うので、記録するときは preview を指定されても dev server だけにする。
 */
export const PLAYWRIGHT_PORT = process.env.PLAYWRIGHT_PORT ?? "3101";
export const USE_PREVIEW_SERVER = process.env.PLAYWRIGHT_WEB_SERVER === "preview" && !process.env.E2E_IMPACT_DIR;
export const FIXTURE_PORT = USE_PREVIEW_SERVER ? String(Number(PLAYWRIGHT_PORT) + 1) : PLAYWRIGHT_PORT;

export const APP_BASE_URL = `http://127.0.0.1:${PLAYWRIGHT_PORT}`;
/** `tests/fixtures/*.tsx` を描く spec が開く dev server。preview でなければ APP_BASE_URL と同じ。 */
export const FIXTURE_BASE_URL = `http://127.0.0.1:${FIXTURE_PORT}`;
