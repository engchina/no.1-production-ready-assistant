import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { extname } from "node:path";
import test from "node:test";

const sourceRoot = new URL("../src/", import.meta.url);

function sourceFiles(directory: URL): URL[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const child = new URL(`${entry.name}${entry.isDirectory() ? "/" : ""}`, directory);
    if (entry.isDirectory()) return sourceFiles(child);
    return [".ts", ".tsx"].includes(extname(entry.name)) ? [child] : [];
  });
}

test("API helpers propagate cancellation and optional timeout signals", () => {
  const apiSource = readFileSync(new URL("../src/lib/api.ts", import.meta.url), "utf8");

  assert.match(apiSource, /timeoutMs\?: number/u);
  assert.match(apiSource, /AbortSignal\.timeout\(options\.timeoutMs\)/u);
  // 利用者の取り消しはそのまま、timeout は DB の起動状態の確認をせず利用者向けの失敗にする（#900）。
  assert.match(apiSource, /if \(isAbortError\(cause\)\) throw cause;/u);
  assert.match(
    apiSource,
    /if \(isTimeoutError\(cause\)\) \{\s*throw new ApiTransportError\(\s*"timeout"/u,
  );

  for (const helper of ["apiGet", "apiGetWithMetadata", "apiPost", "apiPatch", "apiDelete"]) {
    const declaration = new RegExp(
      String.raw`function ${helper}[\s\S]*?options: ApiRequestOptions = \{\}[\s\S]*?signal: requestSignal\(options\)`,
      "u"
    );
    assert.match(apiSource, declaration, `${helper} must combine cancellation and timeout signals`);
  }
});

test("table and view base detail requests use the shared 30 second state machine", () => {
  const hookSource = readFileSync(
    new URL("../src/features/nl2sql/useDbObjectDetailRequest.ts", import.meta.url),
    "utf8",
  );
  const policySource = readFileSync(
    new URL("../src/lib/requestPolicy.ts", import.meta.url),
    "utf8",
  );
  const tableSource = readFileSync(
    new URL("../src/features/nl2sql/pages/TableManagementPage.tsx", import.meta.url),
    "utf8",
  );
  const viewSource = readFileSync(
    new URL("../src/features/nl2sql/pages/ViewManagementPage.tsx", import.meta.url),
    "utf8",
  );

  assert.match(hookSource, /DB_OBJECT_DETAIL_TIMEOUT_MS = API_TIMEOUT_MS\.interactiveDetail/u);
  assert.match(policySource, /interactiveList:\s*60_000/u);
  assert.match(policySource, /interactiveDetail:\s*30_000/u);
  assert.match(policySource, /requestTimeoutSeconds/u);
  assert.match(hookSource, /controllerRef\.current\?\.abort\(\)/u);
  assert.match(hookSource, /sequence === sequenceRef\.current/u);
  assert.match(tableSource, /useDbObjectDetailRequest\(/u);
  assert.match(viewSource, /useDbObjectDetailRequest\(/u);
  assert.match(tableSource, /requestTimeoutSeconds\(API_TIMEOUT_MS\.interactiveDetail\)/u);
  assert.match(viewSource, /requestTimeoutSeconds\(API_TIMEOUT_MS\.interactiveDetail\)/u);
  assert.doesNotMatch(tableSource, /seconds:\s*15/u);
  assert.doesNotMatch(viewSource, /seconds:\s*15/u);
});

test("interactive schema fallbacks are restricted to compatibility statuses", () => {
  const source = readFileSync(
    new URL("../src/features/nl2sql/incrementalQueries.ts", import.meta.url),
    "utf8",
  );
  const i18nSource = readFileSync(new URL("../src/lib/i18n.ts", import.meta.url), "utf8");
  assert.match(source, /new Set\(\[404, 410, 501\]\)/u);
  assert.match(source, /if \(!isLegacyCompatibilityError\(error\)\) throw error/u);
  assert.match(source, /timeoutMs: API_TIMEOUT_MS\.interactiveList/u);
  assert.match(source, /timeoutMs: API_TIMEOUT_MS\.jobControl/u);
  assert.match(source, /params\.set\("include_counts", "false"\)/u);
  assert.doesNotMatch(i18nSource, /8秒以内/u);
});

test("data management refresh uses the paged read model and shared durable schema job", () => {
  const source = readFileSync(
    new URL("../src/features/nl2sql/pages/DataManagementPage.tsx", import.meta.url),
    "utf8",
  );
  assert.match(source, /useDbAdminObjects\(/u);
  assert.match(source, /useSchemaRefreshCoordinator\(\)/u);
  // DB 構造の再取得は共有の hook が coordinator の start を呼ぶ（#2031）。
  assert.match(source, /schemaSync\.refreshFrom\(DATA_SCHEMA_REFRESH_HEADER\)/u);
  assert.doesNotMatch(source, /\/api\/schema\/catalog["']/u);
  assert.doesNotMatch(source, /\/api\/schema\/refresh["']/u);
  assert.doesNotMatch(source, /Promise\.all\(\[\s*refreshSchema/u);
  for (const state of [
    "previewLoadingObject",
    "exportLoading",
    "csvUploading",
    "syntheticLoading",
  ]) {
    assert.match(source, new RegExp(String.raw`const \[${state},`, "u"));
  }
  assert.match(source, /previewRequestSequence = useRef\(0\)/u);
  assert.match(source, /sequence !== previewRequestSequence\.current/u);
  assert.match(source, /const csvTablesQuery = useDbAdminObjects\(\s*csvTableSearch,\s*"table",\s*"all"\s*\)/u);
  assert.match(source, /onLoadMore=\{\(\) => void csvTablesQuery\.fetchNextPage\(\)\}/u);
  assert.doesNotMatch(source, /activeView !== "csv"[\s\S]{0,200}fetchNextPage/u);
});

test("direct API calls in useEffect include cancellation", () => {
  const violations: string[] = [];
  const apiCall = /\b(?:apiGet|apiPost|apiPatch|apiDelete|apiFetch|securityApi\.|api\.)/u;
  const cancellationGuard = /\b(?:AbortController|useRequestScope|signal)\b/u;

  for (const file of sourceFiles(sourceRoot)) {
    if (!file.pathname.endsWith(".tsx")) continue;
    const source = readFileSync(file, "utf8");
    const lines = source.split("\n");
    for (let index = 0; index < lines.length; index += 1) {
      if (!lines[index].includes("useEffect(")) continue;
      const block: string[] = [];
      for (let cursor = index; cursor < Math.min(lines.length, index + 120); cursor += 1) {
        block.push(lines[cursor]);
        if (
          /^\s*\}\s*,\s*\[/u.test(lines[cursor]) ||
          /^\s*\},\s*\[/u.test(lines[cursor])
        ) {
          break;
        }
      }
      const body = block.join("\n");
      if (apiCall.test(body) && !cancellationGuard.test(body)) {
        violations.push(file.pathname.replace(sourceRoot.pathname, "src/"));
      }
    }
  }

  assert.deepEqual([...new Set(violations)].sort(), []);
});
