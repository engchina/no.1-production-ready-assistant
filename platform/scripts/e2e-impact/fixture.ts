/**
 * e2e の影響分析（test impact analysis）の記録（#885）。
 *
 * 3 製品の e2e の `test` をこの関数で包む（RAG・Agent は `e2e/fixtures/test.ts`、NL2SQL は
 * `tests/e2e/_helpers/test.ts`）。環境変数 `E2E_IMPACT_DIR` があるときだけ、テストの `page`（と同じ
 * context で開いたページ）の Chromium の JS coverage を取り、テストごとに次を JSON に書き出す。
 *
 * - `executed`: source ファイル → 実行された関数の元の行の範囲（`"開始行:終了行"`）
 * - `loaded`: 読み込まれた source ファイル
 * - `durationMs`: テストの所要時間
 * - `prScope`: PR の CI（mobile の project は `@responsive` などだけ。mobile-scope.ts）でも実行するテストか。
 *   `e2e_impact.py` が PR の shard の時間の見積もりに使う（#1582）
 *
 * nightly が全件の実行で記録し、PR の CI は `platform/scripts/e2e_impact.py` がそれを使って、差分の行を
 * 実行した spec を選ぶ。すべてのテストで実行される関数（module の読み込み時の処理・画面の枠）は、
 * `e2e_impact.py` が集計のときに見分ける。
 *
 * - URL → ファイル: Vite dev server の `/src/...` は製品の frontend の source、`/@fs/<絶対パス>` はその
 *   パス。行は Vite が付ける inline の sourcemap で元の source の行に戻す。共有 UI
 *   （`platform/packages/{ui,system-settings}/dist/index.js`）も Vite が dist の `.map` と合成した inline の
 *   sourcemap で `platform/packages/*\/src/...` の行に戻る（inline が無いときは dist の `.map` を使う）。
 * - module の top-level と、React Refresh が差し込む関数は数えない（読み込んだだけで実行されるため）。
 * - `@playwright/test` の型を import しない（この file は platform にあり、製品の node_modules から
 *   解決できないため）。必要な形だけを下に書く。
 */
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve } from "node:path";

import { inPullRequestScope } from "./mobile-scope";

interface CoverageRange {
  startOffset: number;
  endOffset: number;
  count: number;
}

interface CoverageFunction {
  functionName: string;
  ranges: CoverageRange[];
}

interface CoverageEntry {
  url: string;
  source?: string;
  functions: CoverageFunction[];
}

interface CoveragePage {
  coverage: {
    startJSCoverage(options?: { resetOnNavigation?: boolean }): Promise<void>;
    stopJSCoverage(): Promise<CoverageEntry[]>;
  };
  isClosed(): boolean;
  context(): { on(event: "page", listener: (page: CoveragePage) => void): void };
}

interface ImpactTestInfo {
  file: string;
  testId: string;
  titlePath: string[];
  tags: string[];
  retry: number;
  project: { name: string };
  config: { configFile?: string; rootDir: string };
}

type PageFixture = (
  args: { page: CoveragePage; browserName: string },
  use: (page: CoveragePage) => Promise<void>,
  testInfo: ImpactTestInfo,
) => Promise<void>;

interface Extendable {
  extend(fixtures: { page: PageFixture }): unknown;
}

// React Refresh（@vitejs/plugin-react）が各 module に差し込む関数は、読み込んだだけで実行される。
const REFRESH_MARKERS =
  /RefreshRuntime|\$RefreshReg\$|\$RefreshSig\$|__hmr_import|registerExportsForReactRefresh|validateRefreshBoundary|import\.meta\.hot/;
const INLINE_SOURCEMAP = /\/\/# sourceMappingURL=data:application\/json;(?:charset=utf-8;)?base64,([A-Za-z0-9+/=]+)\s*$/;

function findRepoRoot(start: string): string {
  let dir = start;
  for (;;) {
    if (existsSync(join(dir, ".git")) && existsSync(join(dir, "platform"))) return dir;
    const parent = dirname(dir);
    if (parent === dir) throw new Error(`[e2e-impact] リポジトリの root が見つかりません: ${start}`);
    dir = parent;
  }
}

function toPosix(path: string): string {
  return path.split("\\").join("/");
}

function isRepoSource(rel: string): boolean {
  return !!rel && !rel.startsWith("..") && !isAbsolute(rel) && !rel.includes("node_modules");
}

/** coverage の URL を、リポジトリ直下からの相対パスにする。対象外（依存・Vite の内部）は undefined。 */
function urlToRepoPath(url: string, frontendRoot: string, repoRoot: string): string | undefined {
  let pathname: string;
  try {
    pathname = decodeURIComponent(new URL(url).pathname);
  } catch {
    return undefined;
  }
  let absolute: string;
  if (pathname.startsWith("/@fs/")) {
    absolute = pathname.slice("/@fs".length);
  } else if (pathname.startsWith("/@") || pathname.startsWith("/node_modules/")) {
    return undefined;
  } else {
    absolute = join(frontendRoot, pathname);
  }
  const rel = toPosix(relative(repoRoot, absolute));
  return isRepoSource(rel) ? rel : undefined;
}

// ---------------------------------------------------------------- sourcemap

interface DecodedSourceMap {
  /** repo の相対パス（repo の外・依存は undefined）。 */
  sources: Array<string | undefined>;
  /** 生成側の行ごとの [列, source の番号, 元の行, 元の列]（0 始まり）の配列（列の昇順）。 */
  lines: Array<Array<[number, number, number, number]>>;
}

const BASE64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const BASE64_INDEX = new Map([...BASE64].map((char, index) => [char, index]));

function decodeVlqSegment(segment: string): number[] {
  const values: number[] = [];
  let value = 0;
  let shift = 0;
  for (const char of segment) {
    const digit = BASE64_INDEX.get(char);
    if (digit === undefined) break;
    value += (digit & 31) << shift;
    if (digit & 32) {
      shift += 5;
      continue;
    }
    values.push(value & 1 ? -(value >>> 1) : value >>> 1);
    value = 0;
    shift = 0;
  }
  return values;
}

function decodeSourceMap(
  raw: { sources: string[]; sourceRoot?: string; mappings: string },
  resolveSource: (source: string) => string | undefined,
): DecodedSourceMap {
  const sources = raw.sources.map((source) => resolveSource(`${raw.sourceRoot ?? ""}${source}`));
  const lines: DecodedSourceMap["lines"] = [];
  let sourceIndex = 0;
  let originalLine = 0;
  let originalColumn = 0;
  for (const line of raw.mappings.split(";")) {
    const segments: Array<[number, number, number, number]> = [];
    let column = 0;
    for (const segment of line.split(",")) {
      if (!segment) continue;
      const values = decodeVlqSegment(segment);
      column += values[0] ?? 0;
      if (values.length >= 4) {
        sourceIndex += values[1] ?? 0;
        originalLine += values[2] ?? 0;
        originalColumn += values[3] ?? 0;
        segments.push([column, sourceIndex, originalLine, originalColumn]);
      }
    }
    lines.push(segments);
  }
  return { sources, lines };
}

const fileMapCache = new Map<string, { mtimeMs: number; map: DecodedSourceMap }>();

/** 共有 UI の dist の JS の隣の `.map` を読む。無ければ undefined。 */
function loadFileSourceMap(repoPath: string, repoRoot: string): DecodedSourceMap | undefined {
  const mapPath = join(repoRoot, `${repoPath}.map`);
  if (!existsSync(mapPath)) return undefined;
  const mtimeMs = statSync(mapPath).mtimeMs;
  const cached = fileMapCache.get(mapPath);
  if (cached && cached.mtimeMs === mtimeMs) return cached.map;
  const mapDir = dirname(mapPath);
  const map = decodeSourceMap(JSON.parse(readFileSync(mapPath, "utf-8")), (source) => {
    const rel = toPosix(relative(repoRoot, resolve(mapDir, source)));
    return isRepoSource(rel) ? rel : undefined;
  });
  fileMapCache.set(mapPath, { mtimeMs, map });
  return map;
}

const inlineMapCache = new Map<string, DecodedSourceMap | null>();

/**
 * Vite が配信した module の inline の sourcemap。Vite は元のファイルの sourcemap（共有 UI の dist の
 * `.map`）と合成するので、共有 UI でも source は `platform/packages/*\/src/...` を指す。
 * source の相対パスは配信元のファイルの場所から解決し、解決できない 1 つだけの source は配信元とみなす。
 */
function loadInlineSourceMap(repoPath: string, source: string, repoRoot: string): DecodedSourceMap | undefined {
  const match = INLINE_SOURCEMAP.exec(source);
  if (!match) return undefined;
  const key = createHash("sha1").update(repoPath).update(match[1]).digest("hex");
  const cached = inlineMapCache.get(key);
  if (cached !== undefined) return cached ?? undefined;
  let map: DecodedSourceMap | null = null;
  try {
    const raw = JSON.parse(Buffer.from(match[1], "base64").toString("utf-8"));
    const servedDir = dirname(join(repoRoot, repoPath));
    const single = raw.sources?.length === 1;
    map = decodeSourceMap(raw, (entry) => {
      const rel = toPosix(relative(repoRoot, resolve(servedDir, entry)));
      if (isRepoSource(rel) && existsSync(join(repoRoot, rel))) return rel;
      return single ? repoPath : undefined;
    });
  } catch {
    map = null;
  }
  inlineMapCache.set(key, map);
  return map ?? undefined;
}

function lineStarts(source: string): number[] {
  const starts = [0];
  for (let index = 0; index < source.length; index += 1) {
    if (source.charCodeAt(index) === 10) starts.push(index + 1);
  }
  return starts;
}

/** offset を含む行の番号（0 始まり）。 */
function lineIndexAt(starts: number[], offset: number): number {
  let low = 0;
  let high = starts.length - 1;
  while (low < high) {
    const mid = (low + high + 1) >> 1;
    if (starts[mid] <= offset) low = mid;
    else high = mid - 1;
  }
  return low;
}

/** 生成側の位置（行・列とも 0 始まり）の、元の [source の番号, 行, 列]（0 始まり）。 */
function mapPosition(map: DecodedSourceMap, line: number, column: number): [number, number, number] | undefined {
  const segments = map.lines[line] ?? [];
  let found: [number, number, number, number] | undefined;
  for (const segment of segments) {
    if (segment[0] > column) break;
    found = segment;
  }
  found ??= segments[0];
  return found ? [found[1], found[2], found[3]] : undefined;
}

/** 配信されたコードの offset の、元の [source のパス, 行（1 始まり）]。sourcemap が無ければ配信されたコードの行。 */
function originalPosition(
  starts: number[],
  offset: number,
  map: DecodedSourceMap | undefined,
  servedPath: string,
): [string, number] | undefined {
  const line = lineIndexAt(starts, offset);
  if (!map) return [servedPath, line + 1];
  const mapped = mapPosition(map, line, offset - starts[line]);
  if (!mapped) return undefined;
  const path = map.sources[mapped[0]];
  return path ? [path, mapped[1] + 1] : undefined;
}

// ---------------------------------------------------------------- 集計

interface ImpactFiles {
  /** ファイル → 実行された関数の元の行の範囲（`"開始行:終了行"`）。 */
  executed: Map<string, Set<string>>;
  loaded: Set<string>;
}

function isTopLevel(fn: CoverageFunction, sourceLength: number): boolean {
  const range = fn.ranges[0];
  return !!range && range.startOffset === 0 && range.endOffset >= sourceLength - 1;
}

function collect(entries: CoverageEntry[], frontendRoot: string, repoRoot: string, files: ImpactFiles): void {
  for (const entry of entries) {
    const repoPath = urlToRepoPath(entry.url, frontendRoot, repoRoot);
    if (!repoPath) continue;
    const source = entry.source ?? "";
    // 共有 UI の dist は inline の sourcemap（dist の `.map` と合成済み）、無ければ dist の `.map` で元に戻す。
    const map =
      loadInlineSourceMap(repoPath, source, repoRoot) ??
      (repoPath.includes("/dist/") ? loadFileSourceMap(repoPath, repoRoot) : undefined);
    const starts = lineStarts(source);
    if (map && repoPath.includes("/dist/")) {
      for (const original of map.sources) if (original) files.loaded.add(original);
    } else {
      files.loaded.add(repoPath);
    }
    for (const fn of entry.functions) {
      const range = fn.ranges[0];
      if (!range || range.count === 0 || isTopLevel(fn, source.length)) continue;
      if (REFRESH_MARKERS.test(source.slice(range.startOffset, range.endOffset))) continue;
      const start = originalPosition(starts, range.startOffset, map, repoPath);
      const end = originalPosition(starts, Math.max(range.startOffset, range.endOffset - 1), map, repoPath);
      if (!start) continue;
      const endLine = end && end[0] === start[0] && end[1] >= start[1] ? end[1] : start[1];
      const ranges = files.executed.get(start[0]) ?? new Set<string>();
      ranges.add(`${start[1]}:${endLine}`);
      files.executed.set(start[0], ranges);
    }
  }
}

/**
 * `test` を包み、`E2E_IMPACT_DIR` があるときだけ coverage を記録する `page` fixture を足す。
 * 記録しないとき（PR の CI・ローカル）は何もしない。
 */
export function withImpactCoverage<T>(base: T): T {
  const page: PageFixture = async ({ page, browserName }, use, testInfo) => {
    const dir = process.env.E2E_IMPACT_DIR;
    if (!dir || browserName !== "chromium") {
      await use(page);
      return;
    }
    const covered: CoveragePage[] = [];
    const start = async (target: CoveragePage) => {
      covered.push(target);
      await target.coverage.startJSCoverage({ resetOnNavigation: false }).catch(() => undefined);
    };
    page.context().on("page", (opened) => {
      if (opened !== page) void start(opened);
    });
    await start(page);
    const startedAt = Date.now();
    await use(page);
    const durationMs = Date.now() - startedAt;

    const frontendRoot = dirname(testInfo.config.configFile ?? join(testInfo.config.rootDir, "playwright.config.ts"));
    const repoRoot = findRepoRoot(frontendRoot);
    const files: ImpactFiles = { executed: new Map(), loaded: new Set() };
    for (const target of covered) {
      if (target.isClosed()) continue;
      const entries = await target.coverage.stopJSCoverage().catch(() => [] as CoverageEntry[]);
      collect(entries, frontendRoot, repoRoot, files);
    }
    const record = {
      spec: toPosix(relative(repoRoot, testInfo.file)),
      testId: testInfo.testId,
      title: testInfo.titlePath.join(" › "),
      project: testInfo.project.name,
      retry: testInfo.retry,
      durationMs,
      prScope: inPullRequestScope(testInfo.project.name, testInfo.titlePath, testInfo.tags),
      executed: Object.fromEntries(
        [...files.executed.entries()]
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([file, ranges]) => [file, [...ranges].sort()]),
      ),
      loaded: [...files.loaded].sort(),
    };
    const outDir = resolve(dir);
    mkdirSync(outDir, { recursive: true });
    const name = createHash("sha1").update(`${testInfo.testId}:${testInfo.retry}`).digest("hex");
    writeFileSync(join(outDir, `${name}.json`), JSON.stringify(record));
  };
  // Playwright の型を import できないので、`extend` だけを持つ形として呼び、元の `test` の型で返す。
  return (base as unknown as Extendable).extend({ page }) as T;
}
