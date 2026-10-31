import { expect, test, type Page } from "./fixtures/test";

import { APP_ROUTES } from "../src/lib/routes";
import { expectNoPageOverflow, mockDatabaseReady, mockLocalAuth } from "./_helpers";

// RAG の全画面は画面幅いっぱい（共有 PageHeader / PageBody の `wide`）で統一する（#107。#96 の作業画面だけの wide を置き換え）。
// PageHeader と PageBody の wide がずれると、1920px 以上でタイトルと本文の左端がずれる。

const CONTENT_MAX_WIDTH = 1440;

test.beforeEach(async ({ page }) => {
  // 幅の計測が目的のため、画面固有の API はモックせず 404 にする（各画面は読み込み失敗の表示になるが、
  // その表示も PageHeader / PageBody の中に描かれる）。空の一覧を返すと形の違う応答で画面ごと描画に失敗する。
  await page.route("**/api/**", (route) =>
    route.fulfill({ status: 404, json: { data: null, error_messages: ["not mocked"], warning_messages: [] } })
  );
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
});

/**
 * 認証画面（ログイン・パスワード変更・権限なし。AppShell を持たない）以外の全ルート。
 * 詳細画面は代表 id で開く（文書詳細・ナレッジベース詳細は検索・回答プロファイルと同じ PageHeader + PageBody。
 * #555 / #581）。A 型の作成の画面（`?id=new`）も測る。
 */
const PATHS = [
  ...Object.entries(APP_ROUTES)
    .filter(([name]) => !["login", "passwordChange", "forbidden", "documents"].includes(name))
    .map(([, path]) => path),
  `${APP_ROUTES.knowledgeBases}/kb-layout`,
  `${APP_ROUTES.knowledgeBases}?id=new`,
  `${APP_ROUTES.searchAnswerProfiles}?id=new`,
  `${APP_ROUTES.documents}/doc-layout`,
];

/**
 * ページタイトル（h1）と、main 内のすべての計測コンテナ（PageHeader の中身 / PageBody）を測り、
 * 左端のずれと 1440px 以下のコンテナを問題として列挙する。読み込み中 → 本来の PageBody の切り替わりは poll で吸収する。
 */
async function layoutProblems(page: Page, { wide }: { wide: boolean }) {
  const layout = await page.locator("main").evaluate((root) => {
    const h1 = root.querySelector("h1");
    // 共有 measureClass（PageHeader の中身と PageBody が共有）の目印になるクラスの組み合わせ。
    const containers = [...root.querySelectorAll<HTMLElement>('[class~="w-full"][class~="min-w-0"][class~="lg:px-8"]')];
    return {
      titleLeft: h1 ? Math.round(h1.getBoundingClientRect().left) : null,
      containers: containers.map((element) => {
        const rect = element.getBoundingClientRect();
        return {
          contentLeft: Math.round(rect.left + parseFloat(getComputedStyle(element).paddingLeft)),
          width: Math.round(rect.width),
        };
      }),
    };
  });
  const problems: string[] = [];
  // PageHeader の中身 + PageBody で 2 つ以上ある。
  if (layout.containers.length < 2) problems.push(`計測コンテナが ${layout.containers.length} 個`);
  const left = layout.titleLeft ?? layout.containers[0]?.contentLeft;
  for (const [index, container] of layout.containers.entries()) {
    if (Math.abs(container.contentLeft - (left ?? 0)) > 1) {
      problems.push(`#${index} の左端 ${container.contentLeft}px がタイトル ${left}px とずれる`);
    }
    if (wide && container.width <= CONTENT_MAX_WIDTH) problems.push(`#${index} の幅 ${container.width}px`);
  }
  return problems;
}

async function openPage(page: Page, path: string) {
  await page.goto(path);
  await expect(page.locator("main").getByRole("heading", { level: 1 })).toBeVisible();
}

for (const width of [1920, 2560]) {
  for (const path of PATHS) {
    test(`${width}px: ${path} は画面幅いっぱいで、タイトルと本文の左端が一致する`, { tag: "@responsive" }, async ({ page }, testInfo) => {
      test.skip(testInfo.project.name !== "desktop", "広い画面の計測は desktop プロジェクトで行う");
      await page.setViewportSize({ width, height: 1000 });
      await openPage(page, path);

      await expect.poll(() => layoutProblems(page, { wide: true })).toEqual([]);
      await expectNoPageOverflow(page);
    });
  }
}

for (const path of PATHS) {
  test(`375px: ${path} はタイトルと本文の左端が一致し、横にはみ出さない`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "mobile", "狭い画面の計測は mobile プロジェクトで行う");
    await openPage(page, path);

    await expect.poll(() => layoutProblems(page, { wide: false })).toEqual([]);
    await expectNoPageOverflow(page);
  });
}
