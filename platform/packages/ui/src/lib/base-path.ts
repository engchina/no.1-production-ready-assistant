/**
 * 画面の配信の基点（base path）の扱い（#1316）。
 *
 * 1 台の Compute に 3 製品を置く配備では、Nginx が製品を path の prefix（`/rag/` `/nl2sql/` `/agent/`）で分け、
 * 各製品の frontend はその prefix を base に build する（Vite の `base` → `import.meta.env.BASE_URL`）。
 * ローカルの開発（`npm run dev`）と e2e は今までどおり `/` で動く（base が `/` のときは何も変えない）。
 *
 * この module は純粋な関数だけを持つ。`import.meta.env.BASE_URL` は、この package を build した時点の値に
 * 置き換わってしまうため、ここでは読まない。値は各製品が自分の `import.meta.env.BASE_URL` から渡す。
 */

/** base を `/` で始まり `/` で終わる形にそろえる（空・未指定は `/`）。 */
export function normalizeBasePath(base: string | null | undefined): string {
  const trimmed = (base ?? "").trim();
  if (!trimmed || trimmed === "/" || trimmed === "./") return "/";
  // 前後の `/` を外す。`/\/+$/` の正規表現は `/` の連続の途中で O(n²) になるため、添字で外す。
  let start = 0;
  let end = trimmed.length;
  while (start < end && trimmed[start] === "/") start += 1;
  while (end > start && trimmed[end - 1] === "/") end -= 1;
  const withoutSlashes = trimmed.slice(start, end);
  return withoutSlashes ? `/${withoutSlashes}/` : "/";
}

/** React Router の `basename`。base が `/` なら `undefined`（今までどおり）、`/rag/` なら `/rag`。 */
export function routerBasename(base: string | null | undefined): string | undefined {
  const normalized = normalizeBasePath(base);
  return normalized === "/" ? undefined : normalized.slice(0, -1);
}

/**
 * アプリの絶対パス（`/api/...` など、`/` で始まるもの）に base を付ける。
 *
 * - base が `/` のとき、`/` で始まらないもの（`https://...`・`blob:`・`data:`・相対パス）、`//host` は変えない。
 * - 既に base で始まっているものは変えない（何度通しても 1 回だけ付く。URL を作る所と送る所の両方で通してよい）。
 */
export function withBasePath(path: string, base: string | null | undefined): string {
  const normalized = normalizeBasePath(base);
  if (normalized === "/") return path;
  if (!path.startsWith("/") || path.startsWith("//")) return path;
  const prefix = normalized.slice(0, -1);
  if (path === prefix || path.startsWith(normalized) || path.startsWith(`${prefix}?`) || path.startsWith(`${prefix}#`)) {
    return path;
  }
  return `${prefix}${path}`;
}

/**
 * base を付けた完全な path（`location.pathname` や `<a>` の `href` から作ったもの）から base を外し、
 * React Router の `navigate()` に渡せる path にする。base で始まらないものはそのまま返す。
 */
export function stripBasePath(path: string, base: string | null | undefined): string {
  const normalized = normalizeBasePath(base);
  if (normalized === "/") return path;
  const prefix = normalized.slice(0, -1);
  if (path === prefix) return "/";
  if (path.startsWith(normalized)) return path.slice(prefix.length);
  if (path.startsWith(`${prefix}?`) || path.startsWith(`${prefix}#`)) return `/${path.slice(prefix.length)}`;
  return path;
}
