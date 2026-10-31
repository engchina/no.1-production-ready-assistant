import {
  normalizeBasePath,
  routerBasename,
  stripBasePath,
  withBasePath,
} from "@production-ready/ui";

/**
 * 配信のパスの前置き（#1316）。build の `FRONTEND_BASE_PATH`（vite.config.ts の `base`）が
 * `import.meta.env.BASE_URL` になる。1 台の Compute で 3 製品を配信するときは `/nl2sql/`、
 * 単独の配備・dev サーバ・e2e は `/`（今までどおり）。
 * ロジックテスト（`node --import jiti/register --test`）では `import.meta.env` が無いため `/` にする。
 */
export const APP_BASE_PATH: string = normalizeBasePath(
  (import.meta as { env?: { BASE_URL?: string } }).env?.BASE_URL ?? "/"
);

/** React Router の `basename`。base が `/` なら `undefined`（今までどおり）。 */
export const ROUTER_BASENAME: string | undefined = routerBasename(APP_BASE_PATH);

let currentBasePath = APP_BASE_PATH;

/**
 * アプリの絶対パス（`/api/...`・画面の `/chat` など）に配信の前置きを付ける。
 * backend への要求・`<a href>`・ダウンロードなど、ブラウザが直接 URL を解決する所（sink）で通す。
 * 何度通しても 1 回だけ付き、`https://`・`blob:`・`data:`・相対パスは変えない。base が `/` なら何もしない。
 * React Router の `Link` / `navigate()` は `basename` が付けるため、ここを通さない。
 */
export function appPath(path: string): string {
  return withBasePath(path, currentBasePath);
}

/** `appPath` の逆。`location.pathname` などの前置き付きの path を、画面の path に戻す。 */
export function stripAppPath(path: string): string {
  return stripBasePath(path, currentBasePath);
}

/**
 * ロジックテストだけが使う。`appPath` の前置きを差し替え、`null` で build の値に戻す。
 * （ロジックテストでは `import.meta.env` が無く、前置きのある build の sink を確かめられないため。）
 */
export function overrideAppBasePathForTest(base: string | null): void {
  currentBasePath = base === null ? APP_BASE_PATH : normalizeBasePath(base);
}
