import { expect, test, type Page, type Route } from "./fixtures/test";

import { apiEnvelope, expectNoPageOverflow, mockLocalAuth } from "./_helpers";

// #2131: 文書の分類（大分類 → 中分類 → 小分類）の一覧の管理。作成・名前の変更・並べ替え・削除はその場で保存する。
// #2141: 段ごとの列（カラムブラウザ）で見る。desktop は 3 列、375px は 1 列ずつ（戻るで上の段へ）。
// 名前は合成の値（業務の名前を使わない）。

type Node = {
  id: string;
  name: string;
  level: number;
  parent_id: string | null;
  document_count: number;
  children: Node[];
};

function category(id: string, name: string, level: number, parent: string | null, children: Node[] = []): Node {
  return { id, name, level, parent_id: parent, document_count: 0, children };
}

/** 分類の一覧の API を、状態を持つ mock にする（作成・名前の変更・並べ替え・削除を反映する）。 */
async function mockCategories(page: Page) {
  const state = {
    tree: [
      category("l1", "10_業務A", 1, null, [category("m1", "種別X", 2, "l1")]),
      category("l2", "20_業務B", 1, null),
    ],
    requests: [] as { method: string; path: string; body: unknown }[],
  };
  state.tree[0].document_count = 2;
  state.tree[0].children[0].document_count = 2;
  const find = (nodes: Node[], id: string): Node[] | undefined => {
    for (const node of nodes) {
      if (node.id === id) return nodes;
      const found = find(node.children, id);
      if (found) return found;
    }
    return undefined;
  };
  let next = 1;
  const handler = async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const body = request.postData() ? request.postDataJSON() : null;
    state.requests.push({ method: request.method(), path, body });
    const id = path.split("/")[3];
    if (request.method() === "POST" && path === "/api/document-categories") {
      const { parent_id: parentId, name } = body as { parent_id: string | null; name: string };
      const siblings = parentId ? find(state.tree, parentId)!.find((node) => node.id === parentId)!.children : state.tree;
      if (siblings.some((node) => node.name === name)) {
        return route.fulfill({
          status: 409,
          json: {
            data: null,
            error_messages: ["同じ分類の下に同じ名前の分類があります（先頭の番号「10_」などの違いは同じ名前として扱います）。"],
            warning_messages: [],
          },
        });
      }
      const level = parentId ? find(state.tree, parentId)!.find((node) => node.id === parentId)!.level + 1 : 1;
      siblings.push(category(`new-${next++}`, name, level, parentId));
      return route.fulfill({ status: 201, json: apiEnvelope(state.tree) });
    }
    if (request.method() === "PATCH") {
      const node = find(state.tree, id)!.find((item) => item.id === id)!;
      node.name = (body as { name: string }).name;
      return route.fulfill({ json: apiEnvelope({ updated_document_count: node.document_count }) });
    }
    if (request.method() === "POST" && path.endsWith("/move")) {
      const siblings = find(state.tree, id)!;
      const index = siblings.findIndex((node) => node.id === id);
      const target = index + ((body as { direction: string }).direction === "up" ? -1 : 1);
      [siblings[index], siblings[target]] = [siblings[target], siblings[index]];
      return route.fulfill({ json: apiEnvelope(state.tree) });
    }
    if (request.method() === "DELETE") {
      const siblings = find(state.tree, id)!;
      const node = siblings.find((item) => item.id === id)!;
      if (node.children.length || node.document_count) {
        return route.fulfill({
          status: 409,
          json: {
            data: null,
            error_messages: ["この分類は削除できません（2 件の文書が使っています）。先に下の分類を削除するか、文書の分類を変えてください。"],
            warning_messages: [],
          },
        });
      }
      siblings.splice(siblings.indexOf(node), 1);
      return route.fulfill({ json: apiEnvelope(state.tree) });
    }
    return route.fulfill({ json: apiEnvelope(state.tree) });
  };
  await page.route("**/api/document-categories", handler);
  await page.route("**/api/document-categories/**", handler);
  return state;
}

async function openActions(page: Page, id: string) {
  await page.getByTestId(`document-category-actions-${id}`).getByRole("button").first().click();
}

test.beforeEach(async ({ page }) => {
  await mockLocalAuth(page);
});

/** 表示中の列（狭い画面では 1 列だけ出す）。 */
function column(page: Page, level: number) {
  return page.getByTestId(`document-category-column-${level}`);
}

/** 行の選択のボタン（行の操作メニューのボタンとは別）。 */
function rowButton(page: Page, id: string) {
  return page.getByTestId(`document-category-${id}`).locator("[data-column-browser-row]");
}

function rows(page: Page, level: number) {
  return column(page, level).locator("li[data-testid^='document-category-']");
}

test("段ごとの列で階層をたどり、作成・名前の変更・並べ替え・削除ができる", { tag: "@responsive" }, async ({ page }, testInfo) => {
  const mobile = testInfo.project.name === "mobile";
  const state = await mockCategories(page);
  await page.goto("/document-categories");

  await expect(page.getByRole("heading", { name: "文書の分類", level: 1 })).toBeVisible();
  await expect(page.getByRole("group", { name: "文書の分類の階層" })).toBeVisible();
  // 大分類の列。行に下の段の数と文書の数を文字で示す。
  await expect(column(page, 1).getByRole("heading", { name: /^大分類/ })).toContainText("2 件");
  await expect(page.getByTestId("document-category-l1")).toContainText("10_業務A");
  await expect(page.getByTestId("document-category-l1")).toContainText("中分類 1 · 文書 2 件");
  // 上の段を選ぶまで、右の列は案内だけ（狭い画面では出さない）。
  if (mobile) await expect(column(page, 2)).toBeHidden();
  else await expect(column(page, 2)).toContainText("大分類を選ぶと、その下の中分類を表示します。");
  await expect(page.getByRole("toolbar").or(page.getByTestId("document-category-toolbar"))).toContainText("全 3 件");

  // 大分類を追加する（同じ名前は理由を欄の下に出す）。
  await column(page, 1).getByRole("button", { name: "大分類を追加" }).click();
  const largeName = page.getByRole("textbox", { name: "新しい大分類の名前" });
  await expect(largeName).toBeFocused();
  await largeName.fill("20_業務B");
  await page.getByRole("button", { name: "追加", exact: true }).click();
  await expect(page.getByText("同じ分類の下に同じ名前の分類があります")).toBeVisible();
  await largeName.fill("30_業務C");
  await page.getByRole("button", { name: "追加", exact: true }).click();
  await expect(page.getByTestId("document-category-new-1")).toContainText("30_業務C");
  expect(state.requests.filter((item) => item.method === "POST").at(-1)?.body).toEqual({
    parent_id: null,
    name: "30_業務C",
  });

  // 並べ替え: 2 番目の大分類を上へ。先頭の「上へ移動」は押せない。
  await openActions(page, "l2");
  await page.getByRole("menuitem", { name: "上へ移動" }).click();
  await expect(rows(page, 1).first()).toHaveAttribute("data-testid", "document-category-l2");
  await openActions(page, "l2");
  await expect(page.getByRole("menuitem", { name: "上へ移動" })).toBeDisabled();
  await page.keyboard.press("Escape");

  // 使っている分類の削除は理由を出して止める。使っていない分類は確認して削除する。
  await openActions(page, "l1");
  await page.getByRole("menuitem", { name: "削除" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "削除" }).click();
  await expect(page.getByText("この分類は削除できません（2 件の文書が使っています）。")).toBeVisible();
  await openActions(page, "new-1");
  await page.getByRole("menuitem", { name: "削除" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "削除" }).click();
  await expect(page.getByTestId("document-category-new-1")).toHaveCount(0);

  // 大分類を選ぶと、右の列にその下の中分類を出す。選んだ位置は URL に残す。
  await rowButton(page, "l1").click();
  await expect(page).toHaveURL(/[?&]large=l1/);
  await expect(rowButton(page, "l1")).toHaveAttribute(
    "aria-current",
    "true",
  );
  await expect(column(page, 2).getByRole("heading", { name: /^10_業務A の中分類/ })).toContainText("1 件");
  await expect(page.getByTestId("document-category-m1")).toContainText("小分類 0 · 文書 2 件");
  if (mobile) {
    // 狭い画面は 1 列ずつ。上の段へは戻るボタンで戻る。
    await expect(column(page, 1)).toBeHidden();
    await expect(column(page, 2).getByRole("button", { name: "大分類に戻る" })).toBeVisible();
  } else {
    await expect(column(page, 1)).toBeVisible();
    await expect(column(page, 3)).toContainText("中分類を選ぶと、その下の小分類を表示します。");
  }

  // 中分類を選び、その下に小分類を追加する（小分類は最下段なので、下の段の数を出さない）。
  await rowButton(page, "m1").click();
  await expect(page).toHaveURL(/[?&]middle=m1/);
  await expect(column(page, 3)).toContainText("小分類がまだありません");
  await column(page, 3).getByRole("button", { name: "小分類を追加" }).click();
  await page.getByRole("textbox", { name: "新しい小分類の名前" }).fill("項目1");
  await page.getByRole("button", { name: "追加", exact: true }).click();
  await expect(page.getByTestId("document-category-new-2")).toContainText("文書 0 件");
  await expect(page.getByTestId("document-category-new-2")).not.toContainText("小分類 0");
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("document-categories-columns.png"), fullPage: true });

  // 名前を変える（使っている文書の分類も変わることを知らせる）。狭い画面は中分類の列へ戻ってから。
  if (mobile) await column(page, 3).getByRole("button", { name: "中分類に戻る" }).click();
  await openActions(page, "m1");
  await page.getByRole("menuitem", { name: "名前を変更" }).click();
  const rename = page.getByRole("textbox", { name: "中分類の名前" });
  await expect(rename).toHaveValue("種別X");
  await rename.fill("種別Y");
  await page.getByRole("button", { name: "名前を保存" }).click();
  await expect(page.getByText("名前を変更しました（2 件の文書の分類も変えました）。")).toBeVisible();
  await expect(page.getByTestId("document-category-m1")).toContainText("種別Y");

  // 再読込しても同じ位置を開く。
  await page.reload();
  await expect(column(page, 2).getByRole("heading", { name: /10_業務A の中分類/ })).toBeVisible();
  await expectNoPageOverflow(page);
});

test("分類が多くても、ページではなく列の中で縦にスクロールする", { tag: "@responsive" }, async ({ page }) => {
  const many = Array.from({ length: 30 }, (_, index) => category(`c${index}`, `分類${index + 1}`, 1, null));
  await page.route("**/api/document-categories", (route) => route.fulfill({ json: apiEnvelope(many) }));
  await page.goto("/document-categories");

  const list = column(page, 1).getByRole("list");
  await expect(rows(page, 1)).toHaveCount(30);
  const metrics = await list.evaluate((element) => ({
    clientHeight: element.clientHeight,
    scrollHeight: element.scrollHeight,
    overflowY: getComputedStyle(element).overflowY,
  }));
  expect(metrics.overflowY).toBe("auto");
  expect(metrics.scrollHeight).toBeGreaterThan(metrics.clientHeight);
  // ページの見出しは列をスクロールしても隠れない（ページ全体は伸びない）。
  const pageHeight = await page.evaluate(() => document.documentElement.scrollHeight - window.innerHeight);
  expect(pageHeight).toBeLessThan(400);
  await expectNoPageOverflow(page);
});

test("キーボードで列の中を ↑↓、列の間を → / ← で移る", async ({ page }, testInfo) => {
  // 列の間の移動は全列を並べる広い画面だけ（狭い画面は 1 列ずつで、戻るボタンで上の段へ戻る）。
  test.skip(testInfo.project.name === "mobile", "狭い画面は 1 列だけを出す");
  await mockCategories(page);
  await page.goto("/document-categories?large=l1");

  const first = rowButton(page, "l1");
  await first.focus();
  await page.keyboard.press("ArrowDown");
  await expect(rowButton(page, "l2")).toBeFocused();
  await page.keyboard.press("ArrowRight");
  await expect(rowButton(page, "m1")).toBeFocused();
  // ← は左の列の選択中の行へ戻る。
  await page.keyboard.press("ArrowLeft");
  await expect(first).toBeFocused();
});

test("全段の名前で検索し、一致した分類を段のパスつきで出して、選ぶとその位置を開く", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockCategories(page);
  await page.goto("/document-categories");

  await page.getByRole("searchbox", { name: "分類を検索" }).or(page.getByLabel("分類を検索")).fill("種別");
  const results = page.getByRole("table", { name: "分類の検索結果" });
  await expect(results).toContainText("種別X");
  // 上の段の名前（パス）と段を文字で出す。
  await expect(results).toContainText("10_業務A");
  await expect(results).toContainText("中分類");
  await expect(page.getByTestId("document-category-toolbar")).toContainText("1 件（全 3 件）");
  await results.getByRole("button", { name: "種別X の位置を開く" }).click();
  await expect(page).toHaveURL(/[?&]large=l1/);
  await expect(page).toHaveURL(/[?&]middle=m1/);
  await expect(results).toHaveCount(0);
  // 開いた分類の列を出し（狭い画面でも）、その行を選択中にしてフォーカスする（#2143）。
  await expect(rowButton(page, "m1")).toBeVisible();
  await expect(rowButton(page, "m1")).toHaveAttribute("aria-current", "true");
  await expect(rowButton(page, "m1")).toBeFocused();
  if (testInfo.project.name === "mobile") {
    await expect(column(page, 3)).toBeHidden();
    // 列の行を選んだら、今までどおり一番右の列（その下の段）を出す。
    await rowButton(page, "m1").click();
    await expect(column(page, 3).getByRole("heading", { name: /^種別X の小分類/ })).toBeVisible();
    await expect(column(page, 2)).toBeHidden();
  }
});

test("検索から開いた小分類は、その行を選択中にしてフォーカスする", { tag: "@responsive" }, async ({ page }) => {
  const small = category("s1", "項目P", 3, "m1");
  const tree = [category("l1", "10_業務A", 1, null, [category("m1", "種別X", 2, "l1", [small])])];
  await page.route("**/api/document-categories", (route) => route.fulfill({ json: apiEnvelope(tree) }));
  await page.goto("/document-categories");

  await page.getByRole("searchbox", { name: "分類を検索" }).or(page.getByLabel("分類を検索")).fill("項目P");
  const results = page.getByRole("table", { name: "分類の検索結果" });
  await expect(results).toContainText("10_業務A › 種別X");
  await results.getByRole("button", { name: "項目P の位置を開く" }).click();

  await expect(page).toHaveURL(/[?&]large=l1/);
  await expect(page).toHaveURL(/[?&]middle=m1/);
  await expect(column(page, 3).getByRole("heading", { name: /^種別X の小分類/ })).toBeVisible();
  await expect(rowButton(page, "s1")).toHaveAttribute("aria-current", "true");
  await expect(rowButton(page, "s1")).toBeFocused();
  await expectNoPageOverflow(page);
});

test("ダークテーマでも選択中の行と列の見出しを読める", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await page.addInitScript(() => {
    window.localStorage.setItem("production-ready-rag.ui", JSON.stringify({ state: { theme: "dark" }, version: 0 }));
  });
  await mockCategories(page);
  await page.goto("/document-categories?large=l1&middle=m1");

  await expect(page.locator("html")).toHaveClass(/dark/);
  if (testInfo.project.name !== "mobile") {
    await expect(rowButton(page, "l1")).toHaveAttribute("aria-current", "true");
    await expect(rowButton(page, "m1")).toHaveAttribute("aria-current", "true");
  }
  await expect(column(page, 3)).toContainText("種別X の小分類");
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath("document-categories-dark.png"), fullPage: true });
});

test("分類がまだ無いときは作り方を案内する", async ({ page }) => {
  await page.route("**/api/document-categories", (route) => route.fulfill({ json: apiEnvelope([]) }));
  await page.goto("/document-categories");

  await expect(page.getByText("大分類がまだありません")).toBeVisible();
  await expect(page.getByText("見出しの「大分類を追加」から作ります。")).toBeVisible();
});
