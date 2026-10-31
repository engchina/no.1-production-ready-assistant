/**
 * spec の中で viewport を回すテストを、合わない Playwright project では skip する（#1488）。
 *
 * RAG・NL2SQL の e2e は desktop と mobile（375px）の 2 project で全テストを実行する。テストの名前の末尾が
 * `(desktop)` / `(mobile)` / `(375px)` / `(#1002, mobile)` / `(1920px, dark)` などのテストは、spec の中で
 * `page.setViewportSize` で画面幅を決めているため、2 project で同じ確認を 2 回していた（全件の約 8%）。
 * このようなテストは、名前の画面幅に合う project（desktop の幅は desktop、375px などの狭い幅は mobile）
 * だけで実行する。375px の確認は mobile の project に残る。
 *
 * - 見るのは、テストと describe の名前の末尾の括弧（半角・全角）の中の語（`,` `/` `、` 空白で区切る）。
 *   `desktop` / `mobile`（`-375` などの接尾辞を含む）と、`<数字>px`（480 以下は mobile、1024 以上は
 *   desktop）。両方を含む・どちらも含まない名前は従来どおり 2 project で実行する。
 * - project は名前が `desktop` / `mobile` で始まるものだけを判定する（それ以外は skip しない）。
 * - Agent は mobile-375 の project の `grepInvert` で同じ重複を既に除いているので使わない。
 *
 * 製品の `test`（RAG `e2e/fixtures/test.ts`、NL2SQL `tests/e2e/_helpers/test.ts`）がこの関数で包む。
 * このディレクトリの変更は e2e の影響分析で全件の実行になる（全 spec が通る fixture のため）。
 * `@playwright/test` の型は import しない（platform から製品の node_modules を解決できないため）。
 */

export type ViewportKind = "desktop" | "mobile";

const TRAILING_GROUP = /[(（]([^()（）]*)[)）]\s*$/;
const MOBILE_MAX_PX = 480;
const DESKTOP_MIN_PX = 1024;

/** 名前の末尾の括弧から、テストが前提にする画面幅の種類を求める。 */
export function viewportKindsOfTitle(title: string): Set<ViewportKind> {
  const kinds = new Set<ViewportKind>();
  const group = TRAILING_GROUP.exec(title);
  if (!group) return kinds;
  for (const token of group[1].split(/[\s,、/・]+/)) {
    if (/^desktop(-\d+)?$/.test(token)) kinds.add("desktop");
    else if (/^mobile(-\d+)?$/.test(token)) kinds.add("mobile");
    else {
      const px = /^(\d+)px$/.exec(token);
      if (!px) continue;
      const width = Number(px[1]);
      if (width <= MOBILE_MAX_PX) kinds.add("mobile");
      else if (width >= DESKTOP_MIN_PX) kinds.add("desktop");
    }
  }
  return kinds;
}

export function projectViewportKind(projectName: string): ViewportKind | null {
  if (projectName.startsWith("desktop")) return "desktop";
  if (projectName.startsWith("mobile")) return "mobile";
  return null;
}

/** テストが前提にする画面幅（project と合わないときだけ。合う・判定できないときは null）。 */
export function mismatchedViewport(titlePath: readonly string[], projectName: string): ViewportKind | null {
  const project = projectViewportKind(projectName);
  if (!project) return null;
  const kinds = new Set<ViewportKind>();
  for (const title of titlePath) {
    for (const kind of viewportKindsOfTitle(title)) kinds.add(kind);
  }
  if (kinds.size !== 1 || kinds.has(project)) return null;
  return [...kinds][0];
}

interface ViewportTestInfo {
  titlePath: string[];
  project: { name: string };
  skip(condition: boolean, description?: string): void;
}

type ViewportFixture = [
  (args: Record<string, never>, use: (value: undefined) => Promise<void>, testInfo: ViewportTestInfo) => Promise<void>,
  { auto: true },
];

interface Extendable {
  extend(fixtures: { viewportProject: ViewportFixture }): unknown;
}

export function withViewportProject<T>(base: T): T {
  const viewportProject: ViewportFixture = [
    // Playwright は第 1 引数の分割代入から依存する fixture を読むので、空の `{}` にする（依存なし）。
    async ({}, use, testInfo) => {
      const expected = mismatchedViewport(testInfo.titlePath, testInfo.project.name);
      testInfo.skip(
        expected !== null,
        `画面幅を spec の中で決めるテスト（${expected}）は ${expected} の project だけで実行する（#1488）`,
      );
      await use(undefined);
    },
    { auto: true },
  ];
  // Playwright の型を import できないので、`extend` だけを持つ形として呼び、元の `test` の型で返す。
  return (base as unknown as Extendable).extend({ viewportProject }) as T;
}
