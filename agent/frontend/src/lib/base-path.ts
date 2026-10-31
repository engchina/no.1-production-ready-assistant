import { routerBasename, withBasePath } from "@production-ready/ui";

/**
 * Agent の画面の配信の基点（#1316）。
 *
 * build の時に `FRONTEND_BASE_PATH=/agent/` を渡すと、Vite が `import.meta.env.BASE_URL` を `/agent/` にする
 * （1 台の Compute に 3 製品を置き、Nginx が `/rag/` `/nl2sql/` `/agent/` で分ける配備）。未指定は `/` で、
 * ローカルの開発・e2e では何も変わらない。
 *
 * backend へのリクエストと利用者に見せる URL は、送る所・作る所で `appPath()` を通す（何度通しても 1 回だけ付く）。
 */
export const APP_BASE_PATH: string = import.meta.env?.BASE_URL ?? "/";

/** React Router の `basename`（base が `/` なら `undefined`）。 */
export const ROUTER_BASENAME = routerBasename(APP_BASE_PATH);

/** アプリの絶対パス（`/api/...` や画面の `/runs?id=...`）に base を付ける。 */
export function appPath(path: string, base: string = APP_BASE_PATH): string {
  return withBasePath(path, base);
}
