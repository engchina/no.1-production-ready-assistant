import { expect, test, type Page, type Route } from "./fixtures/test";

import {
  expectedControlHeight,
  expectNoPageOverflow,
  measureTableCellOverflow,
  mockAuthUser,
  mockDatabaseReady,
  mockLocalAuth,
} from "./_helpers";
import { jumpToPage } from "../../../platform/packages/ui/e2e/layout-checks";

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await page.route("**/api/search-answer-profiles**", (route) =>
    route.fulfill({
      json: {
        data: {
          items: [
            {
              id: "bv-1",
              name: "経理ビュー",
              description: null,
              status: "ACTIVE",
              knowledge_base_count: 1,
              created_at: "2026-01-01T00:00:00Z",
              updated_at: "2026-01-01T00:00:00Z",
              archived_at: null,
            },
          ],
          total: 1,
          limit: 100,
          offset: 0,
          has_next: false,
        },
        error_messages: [],
        warning_messages: [],
      },
    })
  );
});

/** 一覧 + 詳細（共有 ListDetailLayout。#2283）の幅と開閉の保存を、spec の最初の 1 回だけ消す（再読込では残す）。 */
async function clearListDetailStorage(page: Page) {
  await page.addInitScript(() => {
    if (window.sessionStorage.getItem("feedback-list-detail-cleared")) return;
    window.localStorage.removeItem("production-ready-rag.listDetail.feedback");
    window.sessionStorage.setItem("feedback-list-detail-cleared", "true");
  });
}

test("全幅の一覧を検索・数値ページングし、全幅の詳細の三つのタブから原因を追える", { tag: "@responsive" }, async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "desktop", "2 ペインは xl 以上の幅で確かめる");
  await clearListDetailStorage(page);
  const requestedUrls: string[] = [];
  await mockFeedback(page, requestedUrls);

  await page.goto("/feedback?period=30&sort=newest&size=50&page=1");
  const paging = page.getByTestId("feedback-pagination");
  await expect(paging.getByRole("textbox", { name: "ページ番号" })).toHaveValue("1");
  await expect(paging.getByRole("button", { name: "前へ", exact: true })).toBeDisabled();

  await expect(page.getByRole("heading", { name: "利用者フィードバック" })).toBeVisible();
  await expect(page.getByRole("columnheader", { name: "質問" })).toBeVisible();
  for (const name of ["評価対象", "評価", "低評価の理由", "利用者コメント", "日時"]) {
    await expect(page.getByRole("columnheader", { name, exact: true })).toBeVisible();
  }
  const toolbar = page.getByTestId("feedback-filters-toolbar");
  const period = await toolbar.getByRole("combobox", {name: "期間", exact: true}).boundingBox();
  const target = await toolbar.getByRole("combobox", {name: "評価対象", exact: true}).boundingBox();
  expect(target!.y).toBeGreaterThanOrEqual(period!.y + period!.height);
  await expect(page.getByRole("table").getByText("最新の経費申請期限を教えて", { exact: true })).toBeVisible();
  await expect(page.getByText("前期間比 +17pt", { exact: true }).first()).toBeVisible();
  // B 型：狭い一覧と広い詳細を ListDetailLayout で並べ、先頭を自動で選ばず、未選択の詳細は選び方を案内する（#2283）。
  const layout = page.getByTestId("list-detail-feedback");
  await expect(layout).toHaveAttribute("data-list-detail-layout", "single");
  await expect(page.getByRole("region", { name: "フィードバック詳細" })).toBeHidden();
  await expect(page).not.toHaveURL(/feedback=/);
  const list = page.getByTestId("list-detail-feedback-list");
  const [listBox, layoutBox] = await Promise.all([list.boundingBox(), layout.boundingBox()]);
  // 一覧は画面の全幅を使う。
  expect(listBox!.width).toBeCloseTo(layoutBox!.width, 0);
  // 「詳細を開く」ボタンは行に置かない（行のクリックと名前のボタンで選ぶ）。
  await expect(page.getByRole("button", { name: "詳細を開く" })).toHaveCount(0);

  await page.getByRole("searchbox", { name: "問題・回答・コメントを検索" }).fill("申請期限");
  await expect.poll(() => requestedUrls.some((url) => url.includes("q=%E7%94%B3%E8%AB%8B%E6%9C%9F%E9%99%90"))).toBe(true);

  // 共通のページ送り（#1891）: 件数は上の条件のツールバーの右（ListCount。#1933）、ページ番号を入れて移る、表示件数は 10 / 25 / 50 / 100。
  // ページ送りは一覧の列の下（#2283）。
  await expect(page.getByTestId("feedback-filters-toolbar")).toContainText("120 件（全 120 件）");
  const pagination = page.getByTestId("feedback-pagination");
  await expect(list.getByTestId("feedback-pagination")).toBeVisible();
  await expect(pagination).toContainText("1 / 3 ページ");
  await jumpToPage(pagination, 2);
  await expect(page).toHaveURL(/page=2/);
  await expect.poll(() => requestedUrls.some((url) => url.includes("offset=50"))).toBe(true);
  await expect(pagination).toContainText("2 / 3 ページ");

  await pagination.getByRole("combobox", { name: "表示件数" }).click();
  await page.getByRole("option", { name: "25 件" }).click();
  await expect(page).toHaveURL(/size=25/);
  await expect(page).toHaveURL(/page=1/);

  // 行の操作以外の領域（セルの余白）のクリックで選び、選んだ行を aria-current と背景で示す。
  const row = page.getByTestId("feedback-row-feedback-answer");
  await row.click({ position: { x: 4, y: 4 } });
  await expect(page).toHaveURL(/feedback=feedback-answer/);
  await expect(row).toHaveAttribute("aria-current", "true");
  const detail = page.getByRole("region", { name: "フィードバック詳細" });
  await expect(detail).toBeVisible();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(detail.getByText("2024年版の経費規程では翌月10日です。", { exact: true })).toBeVisible();
  await expect(detail.getByText("規程が更新されています。", { exact: true })).toBeVisible();
  // 選択後は一覧を隠し、詳細が全幅になる。
  const box = await detail.boundingBox();
  expect(box!.width).toBeCloseTo(layoutBox!.width, 0);
  await expect(list).toBeHidden();

  await detail.getByRole("tab", { name: "根拠" }).click();
  await expect(page.getByRole("tabpanel", { name: "根拠" }).getByText("評価対象", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /の根拠を表示$/ })).toBeVisible();
  await expect(page.getByRole("link", { name: "引用元を開く" })).toHaveCount(0);

  await detail.getByRole("tab", { name: "実行情報" }).click();
  await expect(
    page.getByRole("tabpanel", { name: "実行情報" }).getByText("deepseek-chat", { exact: true })
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "会話を開く" })).toHaveAttribute(
    "href",
    "/chat?search_answer_profile_id=bv-1&conversation_id=conv-1#message-msg-1"
  );

  await page.screenshot({ path: testInfo.outputPath("feedback-root-cause-desktop.png"), fullPage: true });

  await expect(page).toHaveURL(/size=25/);
  // 再読込しても URL から同じ行を選んだ状態に戻る。
  await page.reload();
  await expect(page.getByTestId("feedback-row-feedback-answer")).toHaveAttribute("aria-current", "true");
  await expect(page.getByRole("region", { name: "フィードバック詳細" })).toBeVisible();

  await expect(page).toHaveURL(/size=25/);
  // 前へ / 次へ（詳細の上端の行）は一覧の今の並びの前後へ移る（単一ペインの選択は履歴に積む）。
  await page.getByTestId("feedback-detail-pager-next").click();
  await expect(page).toHaveURL(/feedback=feedback-citation/);
  await expect(page.getByTestId("feedback-row-feedback-citation")).toHaveAttribute("aria-current", "true");
  await expect(page.getByTestId("feedback-detail-pager-previous")).toBeEnabled();
  await page.getByTestId("feedback-detail-pager-previous").click();
  await expect(page).toHaveURL(/feedback=feedback-answer/);
  await expect(page.getByTestId("feedback-detail-pager-previous")).toBeDisabled();

  await expect(page).toHaveURL(/size=25/);
  await page.getByTestId("feedback-detail-back").click();
  await expect(list).toBeVisible();
  await expect(page).toHaveURL(/size=25/);
  await expect(page).toHaveURL(/page=1/);
  await expect(page.getByTestId("list-detail-feedback-resizer")).toHaveCount(0);
  await expectNoPageOverflow(page);
});

test("全幅の 1 ペイン: 選ぶと詳細だけ、「一覧へ戻る」とブラウザの「戻る」で一覧に戻り、選んだ行へフォーカスを戻す", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await clearListDetailStorage(page);
  await mockFeedback(page, []);

  // URL から直接開いた詳細（共有のリンク）。一覧は隠し、詳細だけを出す。
  await page.goto(
    "/feedback?period=7&target=answer&rating=not_helpful&reason=incorrect&sort=newest&size=25&page=1&feedback=feedback-answer"
  );
  await expect(page.getByRole("heading", { name: "利用者フィードバック" })).toBeVisible();
  const layout = page.getByTestId("list-detail-feedback");
  const list = page.getByTestId("list-detail-feedback-list");
  const detail = page.getByRole("region", { name: "フィードバック詳細" });
  await expect(layout).toHaveAttribute("data-list-detail-layout", "single");
  await expect(layout).toHaveAttribute("data-list-detail-view", "detail");
  await expect(detail).toBeVisible();
  await expect(list).toBeHidden();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByTestId("list-detail-feedback-resizer")).toHaveCount(0);
  await detail.getByRole("tab", { name: "根拠" }).click();
  await expect(page.getByText("経費規程.pdf", { exact: true }).last()).toBeVisible();
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath(`feedback-single-detail-${testInfo.project.name}.png`), fullPage: true });

  // 「一覧へ戻る」（PageHeader の back）。URL から開いたので選択を外して置き換え、一覧の行へフォーカスを戻す。
  const back = page.getByTestId("feedback-detail-back");
  await back.click();
  await expect(page).not.toHaveURL(/feedback=/);
  await expect(list).toBeVisible();
  await expect(detail).toBeHidden();
  await expect(back).toHaveCount(0);
  const answerButton = page.getByRole("button", { name: "最新の経費申請期限を教えて の詳細を表示" });
  await expect(answerButton).toBeFocused();
  // 絞り込みは残る（一覧は描いたまま隠していた）。
  await expect(page).toHaveURL(/reason=incorrect/);

  // 一覧から選ぶと詳細だけを出し、詳細の領域へフォーカスする。戻ると選んだ行へフォーカスを戻す。
  const citationButton = page.getByRole("button", { name: "申請期限を確認したい の詳細を表示" });
  await citationButton.click();
  await expect(page).toHaveURL(/feedback=feedback-citation/);
  await expect(detail).toBeVisible();
  await expect(detail).toBeFocused();
  await expect(list).toBeHidden();
  await page.getByTestId("feedback-detail-back").click();
  await expect(page).not.toHaveURL(/feedback=/);
  await expect(citationButton).toBeFocused();

  // ブラウザの「戻る」でも一覧へ戻る（一覧から入るときは履歴に積む）。
  await answerButton.scrollIntoViewIfNeeded();
  const scrollBefore = await page.locator("main").evaluate(node => node.scrollTop);
  await answerButton.click();
  await expect(detail).toBeVisible();
  await page.goBack();
  await expect.poll(() => page.locator("main").evaluate(node => node.scrollTop)).toBeCloseTo(scrollBefore, 0);
  await expect(list).toBeVisible();
  await expect(page).not.toHaveURL(/feedback=/);
  await expect(answerButton).toBeFocused();
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath(`feedback-single-list-${testInfo.project.name}.png`), fullPage: true });
});

test("行の題名のボタン（共有 RowTitleButton）は Tab で届き、Enter / Space で選び、選んだ対象を aria-current で伝える", { tag: "@responsive" }, async ({ page }, testInfo) => {
  // #421: RAG の EntityLayout の RowTitleButton を packages/ui の共有部品へ移した。
  await mockFeedback(page, []);
  await page.goto("/feedback?period=30&sort=newest&size=25&page=1");
  await expect(page.getByRole("heading", { name: "利用者フィードバック" })).toBeVisible();
  const mobile = testInfo.project.name === "mobile";

  const answerButton = page.getByRole("button", { name: "最新の経費申請期限を教えて の詳細を表示" });
  const citationButton = page.getByRole("button", { name: "申請期限を確認したい の詳細を表示" });
  await expect(answerButton).toBeVisible();
  await expect(answerButton).toHaveAttribute("data-row-title-button", "");
  await expect(answerButton).not.toHaveAttribute("aria-current", /.*/);

  if (mobile) {
    // タッチ端末では見た目を変えずに当たり判定を縦 44px 以上に広げる（pr-touch-target、#364）。
    const hit = await answerButton.evaluate((node) => {
      node.scrollIntoView({ block: "center" });
      const rect = node.getBoundingClientRect();
      const reach = Math.max(0, (44 - rect.height) / 2) - 1;
      const above = document.elementFromPoint(rect.left + rect.width / 2, rect.top - reach);
      return { hitsSelf: reach <= 0 || node.contains(above) };
    });
    expect(hit.hitsSelf).toBe(true);
  }

  // Tab で届く（行そのものはフォーカスを受けず、題名のボタンが選ぶ導線）。
  await answerButton.focus();
  await page.keyboard.press("Shift+Tab");
  await page.keyboard.press("Tab");
  await expect(answerButton).toBeFocused();
  // フォーカスの表示は outline 1 つ（2px、ring なし。#355）。
  const focusStyle = await answerButton.evaluate((node) => {
    const style = getComputedStyle(node);
    return { outlineStyle: style.outlineStyle, outlineWidth: style.outlineWidth, boxShadow: style.boxShadow };
  });
  expect(focusStyle.outlineStyle).toBe("solid");
  expect(focusStyle.outlineWidth).toBe("2px");
  expect(focusStyle.boxShadow).toBe("none");

  // 375px は 1 ペインで、選ぶと一覧を隠す（隠れた行は getByRole で引けないので、行は data-testid で確かめる）。
  const answerRow = page.getByTestId("feedback-row-feedback-answer");
  const citationRow = page.getByTestId("feedback-row-feedback-citation");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/feedback=feedback-answer/);
  await expect(answerRow).toHaveAttribute("aria-current", "true");
  await expect(answerRow.locator("[data-row-title-button]")).toHaveAttribute("aria-current", "true");
  // 「一覧へ戻る」で一覧へ戻ってから次の行を選ぶ。
  await page.getByTestId("feedback-detail-back").click();

  await citationButton.focus();
  await page.keyboard.press("Space");
  await expect(page).toHaveURL(/feedback=feedback-citation/);
  // 表の行と題名のボタンの両方が「現在の項目」。
  await expect(citationRow).toHaveAttribute("aria-current", "true");
  await expect(citationRow.locator("[data-row-title-button]")).toHaveAttribute("aria-current", "true");
  await expect(answerRow.locator("[data-row-title-button]")).not.toHaveAttribute("aria-current", /.*/);

  await page.getByTestId("feedback-detail-back").click();
  if (!mobile) {
    // ホバーで題名に下線を引く。
    await answerButton.hover();
    await expect(answerButton.locator("span").first()).toHaveCSS("text-decoration-line", "underline");
  } else {
    await expect(citationButton).toBeFocused();
    await expectNoPageOverflow(page);
  }
  await page.screenshot({ path: testInfo.outputPath(`feedback-row-title-${testInfo.project.name}.png`), fullPage: true });
});

test("回答のフィードバックを詳細から 登録済みQ&Aに登録し、品質評価のケースを Excel に書き出せる", async ({ page }) => {
  await mockFeedback(page, []);
  await page.route("**/api/feedback/feedback-answer", (route) => {
    const envelope = feedbackDetailEnvelope();
    return route.fulfill({
      json: {
        ...envelope,
        data: {
          ...envelope.data,
          target_type: "answer",
          document_id: null,
          chunk_id: null,
          corrected_answer: "2026年版の経費規程では翌月末です。",
        },
      },
    });
  });
  let promoted = false;
  await page.route("**/api/feedback/feedback-answer/approved-faq", async (route) => {
    promoted = route.request().method() === "POST";
    await route.fulfill({
      json: {
        data: { search_answer_profile_id: "bv-1", question: "最新の経費申請期限を教えて", inserted_count: 1, deleted_count: 0 },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  // 評価リクエストの Excel の 1 行（#2065）。中身は backend のテストで確かめる。
  await page.route("**/api/feedback/feedback-answer/evaluation-case", (route) =>
    route.fulfill({
      status: 200,
      headers: {
        "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "Content-Disposition": 'attachment; filename="evaluation_case_feedback-answer.xlsx"',
      },
      body: "xlsx",
    })
  );

  await page.goto("/feedback?period=30&feedback=feedback-answer");
  const detail = page.getByRole("region", { name: "フィードバック詳細" });
  await expect(detail.getByRole("heading", { name: "修正した回答" })).toBeVisible();
  const actions = detail.getByTestId("feedback-detail-actions");

  await actions.getByRole("button", { name: "登録済みQ&Aに登録" }).click();
  const confirmDialog = page.getByRole("alertdialog", { name: "検索・回答プロファイルの登録済みQ&Aに登録しますか？" });
  await confirmDialog.getByRole("button", { name: "登録済みQ&Aに登録" }).click();
  await expect(page.getByText("登録済みQ&Aに登録しました: 最新の経費申請期限を教えて")).toBeVisible();
  expect(promoted).toBe(true);

  const download = page.waitForEvent("download");
  await actions.getByRole("button", { name: "品質評価のケースを書き出す" }).click();
  expect((await download).suggestedFilename()).toBe("evaluation_case_feedback-answer.xlsx");
  await expect(page.getByText("品質評価のケースを Excel に書き出しました。", { exact: false })).toBeVisible();
  // 画面は移らない（行を評価リクエストの Excel に写して使う）。
  await expect(page).toHaveURL(/\/feedback/);
});

test("承認 FAQ への反映の権限が無い利用者には 登録済みQ&A への登録を出さない", async ({ page }) => {
  await mockFeedback(page, []);
  // フィードバックの閲覧だけを持つ DB ユーザー（rag.feedback.manage なし。#214）。
  await mockAuthUser(page, {
    permissions: ["menu.feedback", "menu.evaluation"],
    allowed_search_answer_profile_ids: ["bv-1"],
  });
  await page.route("**/api/feedback/feedback-answer", (route) => {
    const envelope = feedbackDetailEnvelope();
    return route.fulfill({
      json: { ...envelope, data: { ...envelope.data, target_type: "answer", document_id: null, chunk_id: null } },
    });
  });

  await page.goto("/feedback?period=30&feedback=feedback-answer");
  const detail = page.getByRole("region", { name: "フィードバック詳細" });
  const actions = detail.getByTestId("feedback-detail-actions");
  await expect(actions.getByRole("button", { name: "品質評価のケースを書き出す" })).toBeVisible();
  await expect(actions.getByRole("button", { name: "登録済みQ&Aに登録" })).toHaveCount(0);

  // 文書の詳細を開けない利用者には、根拠から引用元（文書の詳細）へのリンクを出さない（#303）。
  await detail.getByRole("tab", { name: "根拠" }).click();
  const evidence = page.getByRole("tabpanel", { name: "根拠" });
  await expect(evidence.locator("article").first()).toBeVisible();
  await expect(evidence.getByRole("button", { name: /の根拠を表示$/ })).toHaveCount(0);
});

test("見える範囲を案内する: SYSTEM_ADMIN はすべての利用者の分、ほかのロールは自分が送った分だけ", { tag: "@responsive" }, async ({ page }) => {
  // 範囲は backend が SQL で絞る（#408）。画面は範囲の案内を、集計・一覧より前に出す。
  await mockFeedback(page, []);
  await page.goto("/feedback?period=30&sort=newest&size=50&page=1");
  const allNotice = page.getByRole("status").filter({ hasText: "すべての利用者のフィードバックを表示しています" });
  await expect(allNotice).toBeVisible();
  await expect(page.getByText("自分が送ったフィードバックだけを表示しています")).toHaveCount(0);
  await expectNoPageOverflow(page);

  // SYSTEM_ADMIN 以外のロール（承認 FAQ への反映の権限を持っていても同じ）。後の route が優先する。
  await mockAuthUser(page, {
    permissions: ["menu.feedback", "rag.feedback.manage"],
    allowed_search_answer_profile_ids: ["bv-1"],
  });
  await page.reload();
  const ownNotice = page.getByRole("status").filter({ hasText: "自分が送ったフィードバックだけを表示しています" });
  await expect(ownNotice).toBeVisible();
  await expect(ownNotice).toContainText("SYSTEM_ADMIN のロールの利用者だけが確認できます");
  await expect(page.getByText("すべての利用者のフィードバックを表示しています")).toHaveCount(0);
  const noticeBox = await ownNotice.boundingBox();
  const summaryBox = await page.getByText("全体の有用率", { exact: true }).first().boundingBox();
  expect(noticeBox !== null && summaryBox !== null && noticeBox.y < summaryBox.y).toBe(true);
  await expectNoPageOverflow(page);
});

for (const width of [1280, 1920]) {
  test(`一覧は全幅で、行の値が列の境界を超えず、タイトルと左端がそろう (${width}px)`, { tag: "@responsive" }, async ({ page }, testInfo) => {
    test.skip(testInfo.project.name !== "desktop", "desktop table contract");
    await page.setViewportSize({ width, height: 900 });
    await clearListDetailStorage(page);
    await mockFeedback(page, []);
    await page.goto("/feedback?period=30&sort=newest&size=50&page=1");

    const list = page.getByTestId("list-detail-feedback-list");
    const table = list.getByRole("table");
    await expect(table.locator('[data-status-variant="danger"]:visible').first()).toBeVisible();
    await expect(table.getByText("旧データの評価（送信元が記録されていない）")).toBeVisible();
    expect(await measureTableCellOverflow(page, "main table")).toEqual([]);
    // 1920px でも一覧は全幅で、PageHeader と左端がそろう。
    const [listBox, titleBox] = await Promise.all([
      list.boundingBox(),
      page.getByRole("heading", { level: 1, name: "利用者フィードバック" }).boundingBox(),
    ]);
    const layoutBox = await page.getByTestId("list-detail-feedback").boundingBox();
    expect(listBox!.width).toBeCloseTo(layoutBox!.width, 0);
    expect(Math.abs(listBox!.x - titleBox!.x)).toBeLessThanOrEqual(1);
    // ページ送りは列の幅に収まる（横にあふれない）。
    const overflow = await page
      .getByTestId("feedback-pagination")
      .evaluate((node) => node.scrollWidth - node.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);

    // 評価は状態なのでサムズアップ / ダウンのアイコンを持つ（色だけで表さない）。
    const notHelpful = table.locator("[data-status-variant='danger']:visible").first();
    await expect(notHelpful).toHaveText("役に立たなかった");
    await expect(notHelpful.locator("svg")).toHaveCount(1);
    await expectNoPageOverflow(page);
  });
}

test("375pxの一覧の行は評価バッジや値が行の外にはみ出さない", { tag: "@responsive" }, async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "mobile", "mobile list contract");
  await page.setViewportSize({ width: 375, height: 812 });
  await mockFeedback(page, []);
  await page.goto("/feedback?period=30&sort=newest&size=50&page=1");

  const rows = page.getByTestId("list-detail-feedback-list").locator("tbody tr");
  await expect(rows.first()).toBeVisible();
  const overflow = await rows.evaluateAll((items) =>
    items.flatMap((item) => {
      const box = item.getBoundingClientRect();
      return Array.from(item.querySelectorAll("*"))
        .filter((el) => {
          const rect = el.getBoundingClientRect();
          return rect.width > 1 && (rect.right > box.right + 0.5 || rect.left < box.left - 0.5);
        })
        .map((el) => (el.textContent ?? el.tagName).trim().slice(0, 24));
    })
  );
  expect(overflow).toEqual([]);
  await expect(page.getByRole("columnheader")).toHaveCount(1);
  const firstCell = await page.getByRole("rowheader").first().boundingBox();
  const tableBox = await page.getByRole("table").boundingBox();
  expect(firstCell!.width).toBeGreaterThan(tableBox!.width * 0.9);
  await expectNoPageOverflow(page);
});

async function mockFeedback(page: Page, requestedUrls: string[]) {
  await page.route("**/api/feedback**", async (route) => {
    requestedUrls.push(route.request().url());
    await fulfillFeedback(route);
  });
}

async function fulfillFeedback(route: Route) {
  const url = new URL(route.request().url());
  if (url.pathname === "/api/feedback/feedback-answer") {
    await route.fulfill({ json: feedbackDetailEnvelope() });
    return;
  }
  if (url.pathname === "/api/feedback/feedback-citation") {
    const envelope = feedbackDetailEnvelope();
    await route.fulfill({
      json: { ...envelope, data: { ...envelope.data, feedback_id: "feedback-citation", rating: "helpful", reason: null } },
    });
    return;
  }
  const limit = Number(url.searchParams.get("limit") ?? 50);
  const offset = Number(url.searchParams.get("offset") ?? 0);
  await route.fulfill({ json: feedbackEnvelope(limit, offset) });
}

function feedbackEnvelope(limit: number, offset: number) {
  return {
    data: {
      summary: {
        total: 120,
        helpful_count: 80,
        not_helpful_count: 40,
        helpful_rate: 0.6667,
        answer_total: 80,
        answer_helpful_rate: 0.5,
        citation_total: 40,
        citation_helpful_rate: 1,
        reason_counts: [
          { reason: "incorrect", count: 24 },
          { reason: "incomplete", count: 10 },
          { reason: "not_relevant", count: 6 },
        ],
      },
      previous_summary: {
        total: 100,
        helpful_count: 50,
        not_helpful_count: 50,
        helpful_rate: 0.5,
        answer_total: 70,
        answer_helpful_rate: 0.4,
        citation_total: 30,
        citation_helpful_rate: 0.7333,
        reason_counts: [{ reason: "incorrect", count: 30 }],
      },
      items: {
        items: [
          {
            feedback_id: "feedback-answer",
            trace_id: "trace-answer-123456789",
            search_answer_profile_id: "bv-1",
            search_answer_profile_name: "経理ビュー",
            target_type: "answer",
            source_surface: "chat",
            document_id: null,
            chunk_id: null,
            message_id: "msg-1",
            rating: "not_helpful",
            reason: "incorrect",
            comment: "規程が更新されています。",
            created_at: "2026-07-01T09:00:00Z",
            conversation_id: "conv-1",
            conversation_title: "経費申請",
            model: "deepseek-chat",
            file_name: null,
            question_preview: "最新の経費申請期限を教えて",
            comment_preview: "規程が更新されています。",
            has_comment: true,
          },
          {
            feedback_id: "feedback-citation",
            trace_id: "trace-citation-123456789",
            search_answer_profile_id: "bv-1",
            search_answer_profile_name: "経理ビュー",
            target_type: "citation",
            source_surface: "search",
            document_id: "doc-1",
            chunk_id: "doc-1:0",
            message_id: null,
            rating: "helpful",
            reason: null,
            comment: null,
            created_at: "2026-07-01T08:00:00Z",
            conversation_id: null,
            conversation_title: null,
            model: null,
            file_name: "経費規程.pdf",
            question_preview: "申請期限を確認したい",
            comment_preview: null,
            has_comment: false,
          },
          {
            // 列幅の検査用: 最も長い対象 / 送信元・理由・検索・回答プロファイル名・モデル名
            feedback_id: "feedback-legacy-long",
            trace_id: "trace-legacy-long",
            search_answer_profile_id: "bv-2",
            search_answer_profile_name: "経理・財務・監査の横断ナレッジ検索・回答プロファイル",
            target_type: "citation",
            source_surface: null,
            document_id: "doc-2",
            chunk_id: "doc-2:0",
            message_id: null,
            rating: "not_helpful",
            reason: "missing_evidence",
            comment: null,
            created_at: "2026-06-30T23:59:00Z",
            conversation_id: null,
            conversation_title: null,
            model: "dashscope/qwen3-235b-a22b-thinking-2507-long-context-example",
            file_name: "経費規程.pdf",
            question_preview: "旧データの評価（送信元が記録されていない）",
            comment_preview: null,
            has_comment: false,
          },
          ...Array.from({ length: 8 }, (_, index) => ({
            feedback_id: `feedback-extra-${index + 1}`,
            trace_id: `trace-extra-${index + 1}`,
            search_answer_profile_id: "bv-1",
            search_answer_profile_name: "経理ビュー",
            target_type: index % 2 === 0 ? "answer" : "citation",
            source_surface: index % 3 === 0 ? "chat" : "search",
            document_id: index % 2 === 0 ? null : "doc-1",
            chunk_id: index % 2 === 0 ? null : `doc-1:${index + 1}`,
            message_id: index % 3 === 0 ? `msg-${index + 2}` : null,
            rating: index < 5 ? "not_helpful" : "helpful",
            reason: index < 5 ? (index % 2 === 0 ? "incomplete" : "not_relevant") : null,
            comment: null,
            created_at: `2026-07-01T0${7 - Math.min(index, 7)}:00:00Z`,
            conversation_id: index % 3 === 0 ? "conv-1" : null,
            conversation_title: null,
            model: index % 2 === 0 ? "deepseek-chat" : null,
            file_name: index % 2 === 0 ? null : "経費規程.pdf",
            question_preview: `補足のフィードバック ${index + 1}`,
            comment_preview: null,
            has_comment: false,
          })),
        ],
        total: 120,
        limit,
        offset,
        has_next: offset + limit < 120,
      },
    },
    error_messages: [],
    warning_messages: [],
  };
}

function feedbackDetailEnvelope() {
  return {
    data: {
      feedback_id: "feedback-answer",
      trace_id: "trace-answer-123456789",
      search_answer_profile_id: "bv-1",
      search_answer_profile_name: "経理ビュー",
      target_type: "citation",
      source_surface: "chat",
      document_id: "doc-1",
      chunk_id: "doc-1:0",
      message_id: "msg-1",
      rating: "not_helpful",
      reason: "incorrect",
      comment: "規程が更新されています。",
      created_at: "2026-07-01T09:00:00Z",
      conversation_id: "conv-1",
      conversation_title: "経費申請",
      model: "deepseek-chat",
      file_name: "経費規程.pdf",
      question_preview: "最新の経費申請期限を教えて",
      comment_preview: "規程が更新されています。",
      has_comment: true,
      content_source: "chat_message",
      question: "最新の経費申請期限を教えて",
      answer: "2024年版の経費規程では翌月10日です。",
      citations: [
        {
          document_id: "doc-1",
          chunk_id: "doc-1:0",
          file_name: "経費規程.pdf",
          section_title: "申請期限",
          page_number: 3,
          content_preview: "経費申請は利用月の翌月5日までに行います。",
          rerank_score: 0.94,
        },
      ],
      execution: {
        outcome: "success",
        search_mode: "hybrid",
        elapsed_ms: 842,
        retrieved_count: 20,
        reranked_count: 5,
        citation_count: 1,
        guardrail_codes: [],
        config_fingerprint: "9f3a57a3d91bdaf0",
      },
    },
    error_messages: [],
    warning_messages: [],
  };
}

// #384: 検索欄は共有の TextField（先頭アイコン・クリア）。高さはコントロールの高さのトークン（--field-height）。
// #1933: 条件は一覧のツールバー（左に検索欄、その右に絞り込み。狭い画面は「絞り込み」のシート）。
test("検索欄は隣の絞り込みと同じ高さで、先頭アイコン・クリアボタン・Escape で入力を扱える", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockFeedback(page, []);
  await page.goto("/feedback?period=30&sort=newest&size=50&page=1");

  const search = page.getByRole("searchbox", { name: "問題・回答・コメントを検索" });
  await expect(search).toBeVisible();
  const toolbar = page.getByTestId("feedback-filters-toolbar");
  // desktop は期間の絞り込み、375px は「絞り込み」のシートを開くボタンが検索欄の隣（下）に並ぶ。
  const mobile = testInfo.project.name === "mobile";
  const select = mobile
    ? toolbar.getByRole("button", { name: "絞り込み" })
    : page.getByRole("combobox", { name: "期間" });
  const [searchBox, selectBox] = await Promise.all([search.boundingBox(), select.boundingBox()]);
  expect(searchBox && selectBox).toBeTruthy();
  // 入力欄・選択欄は md（36px）。タッチ端末では Button と同じく 44px（#613）。
  expect(Math.round(searchBox!.height)).toBe(await expectedControlHeight(page));
  expect(Math.abs(searchBox!.height - selectBox!.height)).toBeLessThanOrEqual(0.5);
  if (mobile) {
    // 狭い置き場所では検索欄を先頭に全幅で置き、絞り込みはその下（#1933）。
    expect(selectBox!.y).toBeGreaterThan(searchBox!.y + searchBox!.height - 0.5);
    expect(Math.abs(searchBox!.x - selectBox!.x)).toBeLessThanOrEqual(0.5);
  } else {
    // 検索欄は左端（先頭）で、絞り込みはその右に縦の中心をそろえて並ぶ。Tab の順も見た目の順と同じ。
    expect(selectBox!.x).toBeGreaterThan(searchBox!.x + searchBox!.width - 0.5);
    expect(Math.abs(searchBox!.y + searchBox!.height / 2 - (selectBox!.y + selectBox!.height / 2))).toBeLessThanOrEqual(1);
    await search.focus();
    await page.keyboard.press("Tab");
    await expect(select).toBeFocused();
    await search.blur();
  }
  // 角丸は Button・SelectField と同じ --radius-control（6px）。
  expect(await search.evaluate((node) => getComputedStyle(node).borderTopLeftRadius)).toBe("6px");
  expect(await select.evaluate((node) => getComputedStyle(node).borderTopLeftRadius)).toBe("6px");

  // 先頭アイコンは 16px・読み上げない。押すと入力欄にフォーカスが入る（ポインタを透過する）。
  const field = search.locator("xpath=..");
  const icon = field.locator('[data-text-field-slot="leading"]');
  await expect(icon).toHaveAttribute("aria-hidden", "true");
  const iconBox = await icon.boundingBox();
  expect(Math.round(iconBox!.width)).toBe(16);
  const paddingLeft = await search.evaluate((node) => Number.parseFloat(getComputedStyle(node).paddingLeft));
  expect(paddingLeft).toBeGreaterThanOrEqual(iconBox!.x + iconBox!.width - searchBox!.x + 7);
  await icon.click({ force: true });
  await expect(search).toBeFocused();

  // 値が空のときはクリアボタンを出さない。入力すると入力欄の中の右端に出て、文字と重ならない。
  const clear = page.getByRole("button", { name: "検索語をクリア" });
  await expect(clear).toHaveCount(0);
  await search.fill("申請期限");
  await expect(clear).toBeVisible();
  const [clearBox, filledBox] = await Promise.all([clear.boundingBox(), search.boundingBox()]);
  expect(clearBox!.x + clearBox!.width).toBeLessThanOrEqual(filledBox!.x + filledBox!.width);
  expect(clearBox!.y).toBeGreaterThanOrEqual(filledBox!.y);
  expect(clearBox!.y + clearBox!.height).toBeLessThanOrEqual(filledBox!.y + filledBox!.height);
  // クリアボタンは入力欄と同じ高さ（md 36px・タッチ端末 44px。枠線の内側に置いて 2px 低かった。#1132）。
  expect(Math.round(clearBox!.height)).toBe(await expectedControlHeight(page));
  const paddingRight = await search.evaluate((node) => Number.parseFloat(getComputedStyle(node).paddingRight));
  expect(paddingRight).toBeGreaterThanOrEqual(clearBox!.width);
  // ブラウザ既定の type=search のクリア（×）は出さない（共有のクリアボタンと二重にしない）。
  // getComputedStyle は ::-webkit-search-cancel-button を解決しないため、入力欄に当たる規則を CSSOM で確かめる。
  expect(
    await search.evaluate((node) => {
      const rules: CSSStyleRule[] = [];
      const collect = (list: CSSRuleList) => {
        for (const rule of Array.from(list)) {
          if (rule instanceof CSSStyleRule) {
            rules.push(rule);
            if (rule.cssRules.length) collect(rule.cssRules);
          } else if ("cssRules" in rule) collect((rule as CSSGroupingRule).cssRules);
        }
      };
      for (const sheet of Array.from(document.styleSheets)) collect(sheet.cssRules);
      return rules.some((rule) => {
        const style = rule.style.getPropertyValue("appearance");
        const text = rule.selectorText ?? "";
        if (style !== "none" || !text.includes("::-webkit-search-cancel-button")) return false;
        // ネストした規則（&::-webkit-search-cancel-button）は親のクラスで入力欄に当たるかを見る。
        const parent = rule.parentRule instanceof CSSStyleRule ? rule.parentRule.selectorText : text.split("::")[0];
        return node.matches(parent);
      });
    })
  ).toBe(true);

  // Tab 順は入力欄 → クリアボタン。押すと値を消して入力欄に戻る。
  await search.focus();
  await page.keyboard.press("Tab");
  await expect(clear).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(search).toHaveValue("");
  await expect(search).toBeFocused();
  await expect(clear).toHaveCount(0);

  // ポインタで押しても入力欄からフォーカスを外さない。
  await search.fill("経費");
  await clear.click();
  await expect(search).toHaveValue("");
  await expect(search).toBeFocused();

  // Escape でも消える（値があるときだけ）。
  await search.fill("経費");
  await search.press("Escape");
  await expect(search).toHaveValue("");
  await expectNoPageOverflow(page);
  await page.screenshot({ path: testInfo.outputPath(`feedback-search-${testInfo.project.name}.png`) });
});

// #374: 強制カラーモードで、選ばれていないタブに下線を出さない（選んだタブだけ Highlight の下線）。
test("強制カラーモードでは、選んだタブだけに Highlight の下線を出す", async ({ page }) => {
  await mockFeedback(page, []);
  await page.emulateMedia({ forcedColors: "active" });
  await page.goto("/feedback?period=30&sort=newest&size=50&page=1&feedback=feedback-answer");

  const detail = page.getByRole("region", { name: "フィードバック詳細" });
  const selected = detail.getByRole("tab", { name: "内容" });
  const unselected = detail.getByRole("tab", { name: "根拠" });
  await expect(selected).toHaveAttribute("aria-selected", "true");
  await expect(unselected).toHaveAttribute("aria-selected", "false");

  const systemColor = (name: string) =>
    page.evaluate((color) => {
      const probe = document.createElement("div");
      probe.style.color = color;
      document.body.append(probe);
      const value = getComputedStyle(probe).color;
      probe.remove();
      return value;
    }, name);
  const [canvas, canvasText, highlight] = await Promise.all([
    systemColor("Canvas"),
    systemColor("CanvasText"),
    systemColor("Highlight"),
  ]);
  const underline = (tab: typeof selected) => tab.evaluate((node) => getComputedStyle(node).borderBottomColor);

  // 選ばれていないタブの透明の下線は CanvasText に塗られず、背景と同じ Canvas になる。
  expect(await underline(unselected)).toBe(canvas);
  expect(await underline(unselected)).not.toBe(canvasText);
  // 選んだタブは Highlight の下線と太字で区別する。
  expect(await underline(selected)).toBe(highlight);
  expect(Number(await selected.evaluate((node) => getComputedStyle(node).fontWeight))).toBeGreaterThan(
    Number(await unselected.evaluate((node) => getComputedStyle(node).fontWeight))
  );

  // 選び直すと、下線も移る。
  await unselected.click();
  await expect(unselected).toHaveAttribute("aria-selected", "true");
  // 色の transition の途中の値は system color ではないため CanvasText に置き換わる。終わるのを待つ。
  await expect.poll(() => underline(unselected)).toBe(highlight);
  await expect.poll(() => underline(selected)).toBe(canvas);
});


test("長い根拠は共通の高さでスクロールし、戻るとページと条件を保つ", { tag: "@responsive" }, async ({ page }) => {
  await mockFeedback(page, []);
  await page.route("**/api/feedback/feedback-answer", route => {
    const envelope = feedbackDetailEnvelope();
    envelope.data.citations = Array.from({length: 40}, (_, i) => ({document_id: `source-${i}`, chunk_id: `source-${i}:0`, file_name: "合成文書.pdf", section_title: `合成節 ${i + 1}`, page_number: i + 1, content_preview: "合成の根拠です。".repeat(10), rerank_score: 0.9}));
    return route.fulfill({json: envelope});
  });
  await page.goto("/feedback?period=30&sort=oldest&size=25&page=2");
  await page.getByTestId("feedback-row-feedback-answer").click();
  await page.getByRole("tab", {name: "根拠", exact: true}).click();
  const list = page.getByTestId("feedback-evidence-scroll");
  const before = await list.evaluate(node => node.clientHeight);
  const width = page.viewportSize()!.width;
  await page.setViewportSize({ width, height: 1500 });
  if (width >= 768) await expect.poll(() => list.evaluate(node => node.clientHeight)).toBeGreaterThan(before + 200);
  else await expect.poll(() => list.evaluate(node => node.clientHeight)).toBe(before);
  await expect.poll(() => list.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
  await list.focus(); await list.press("End");
  await expect.poll(() => list.evaluate(node => node.scrollTop)).toBeGreaterThan(0);
  await list.getByText("合成節 40", {exact: false}).scrollIntoViewIfNeeded();
  await expect(list.getByText("合成節 40", {exact: false})).toBeInViewport();
  await page.getByTestId("feedback-detail-back").click();
  await expect(page).not.toHaveURL(/feedback=/);
  await expect(page).toHaveURL(/sort=oldest/);
  await expect(page).toHaveURL(/page=2/);
  await expect(page.getByTestId("feedback-pagination")).toContainText("2 / 5 ページ");
});

test("比較一覧から詳細へ移り、戻ると表内とページの読んでいた位置を復元する", { tag: "@responsive" }, async ({ page }, testInfo) => {
  await mockFeedback(page, []);
  await page.route("**/api/feedback**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname !== "/api/feedback") {
      const detail = feedbackDetailEnvelope();
      detail.data.feedback_id = url.pathname.split("/").at(-1)!;
      await route.fulfill({json: detail});
      return;
    }
    const limit = Number(url.searchParams.get("limit") ?? 25);
    const offset = Number(url.searchParams.get("offset") ?? 0);
    const envelope = feedbackEnvelope(limit, offset);
    const base = envelope.data.items.items[0]!;
    const items = Array.from({length: 60}, (_, i) => ({...base, feedback_id: `synthetic-${i}`, question_preview: `合成の質問 ${i}`, comment_preview: `合成のコメント ${i}`, search_answer_profile_name: "プロファイル A"})).slice(offset, offset + limit);
    await route.fulfill({json: {...envelope, data: {...envelope.data, items: {...envelope.data.items, total: 60, has_next: offset + limit < 60, items}}}});
  });
  await page.goto("/feedback?period=7&sort=oldest&size=25&page=2");
  const scroll = page.getByTestId("feedback-scroll-region");
  const main = page.locator("main");
  for (const back of ["button", "browser"]) {
    const target = page.getByTestId("feedback-row-synthetic-48").getByRole("button");
    await target.scrollIntoViewIfNeeded();
    const before = await scroll.evaluate(node => node.scrollTop);
    const pageBefore = await main.evaluate(node => node.scrollTop);
    expect(before + pageBefore).toBeGreaterThan(0);
    await target.click();
    await expect(page.getByTestId("feedback-detail")).toBeVisible();
    if (back === "button") await page.getByTestId("feedback-detail-back").click();
    else await page.goBack();
    await expect(target).toBeFocused();
    await expect.poll(() => scroll.evaluate(node => node.scrollTop)).toBeCloseTo(before, 0);
    await expect.poll(() => main.evaluate(node => node.scrollTop)).toBeCloseTo(pageBefore, 0);
    await expect(page).toHaveURL(/period=7&sort=oldest&size=25&page=2$/);
  }
  await expectNoPageOverflow(page);
  await page.screenshot({path: testInfo.outputPath(`feedback-comparison-${testInfo.project.name}.png`), fullPage: true});
});

test("フィードバックの根拠を同じ右側パネルの該当ページで強調し、元の詳細へ戻る", { tag: "@responsive" }, async ({ page }) => {
  await mockFeedback(page, []);
  let chunkReads = 0;
  const imagePages: string[] = [];
  await page.route("**/api/documents/doc-1/chunks/**", route => {
    chunkReads++;
    return route.fulfill({ json: { data: {
      document_id: "doc-1", chunk_id: "doc-1:0", file_name: "合成文書.pdf", text: "合成の原文", score: 0, rerank_score: null,
      category_name: null, metadata: { page_start: 2, page_width: 612, page_height: 792, bbox: [61, 79, 306, 158], bbox_unit: "absolute" },
    }, error_messages: [], warning_messages: [] } });
  });
  await page.route("**/api/documents/doc-1", route => route.fulfill({ json: { data: { document_id: "doc-1", file_name: "合成文書.pdf", preprocess_artifact: null }, error_messages: [], warning_messages: [] } }));
  await page.route("**/api/documents/doc-1/recipes", route => route.fulfill({ json: { data: [], error_messages: [], warning_messages: [] } }));
  await page.route(/\/api\/documents\/doc-1\/preview-pages(?:\?|$)/, route => route.fulfill({ json: { data: { page_count: 3, pages: [1, 2, 3].map(page_number => ({ page_number, width: 612, height: 792 })) }, error_messages: [], warning_messages: [] } }));
  await page.route(/\/api\/documents\/doc-1\/preview-pages\/\d+(?:\?|$)/, route => {
    imagePages.push(route.request().url());
    return route.fulfill({ contentType: "image/svg+xml", body: '<svg xmlns="http://www.w3.org/2000/svg" width="612" height="792"><rect width="612" height="792" fill="white"/></svg>' });
  });
  await page.goto("/feedback?feedback=feedback-answer&tab=evidence");
  await page.getByRole("tab", { name: "根拠", exact: true }).click();
  expect(chunkReads).toBe(0);
  const originalUrl = page.url();
  const open = page.getByRole("button", { name: /の根拠を表示$/ }).first();
  await expect(open).toContainText("根拠を表示");
  await open.click();
  const sheet = page.getByRole("dialog", { name: "回答の根拠", exact: true });
  await expect(sheet).toBeVisible();
  await expect(sheet.getByTestId("bbox-content-overlay")).toHaveCount(1);
  await expect.poll(() => imagePages.some(url => /preview-pages\/2(?:\?|$)/.test(url))).toBe(true);
  expect(chunkReads).toBe(1);
  expect(page.url()).toBe(originalUrl);
  await expect(page.getByRole("link", { name: "引用元を開く" })).toHaveCount(0);
  await expectNoPageOverflow(page);
  await page.screenshot({ path: test.info().outputPath("feedback-source-drawer.png"), fullPage: true });
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
  expect(page.url()).toBe(originalUrl);
  await expect(open).toBeFocused();
});

test("現在は閲覧できない根拠を別の文書や引用へ置き換えない", { tag: "@responsive" }, async ({ page }) => {
  await mockFeedback(page, []);
  await page.route("**/api/documents/doc-1/chunks/**", route => route.fulfill({ status: 404, json: { error_messages: ["この根拠は現在の閲覧範囲では表示できません。"], warning_messages: [] } }));
  let previews = 0;
  await page.route("**/preview-pages**", route => { previews++; return route.abort(); });
  await page.goto("/feedback?feedback=feedback-answer&tab=evidence");
  await page.getByRole("tab", { name: "根拠", exact: true }).click();
  await page.getByRole("button", { name: /の根拠を表示$/ }).first().click();
  const sheet = page.getByRole("dialog", { name: "回答の根拠", exact: true });
  await expect(sheet.getByText("この根拠は現在の閲覧範囲では表示できません。", { exact: true })).toBeVisible();
  expect(previews).toBe(0);
  await expect(sheet.getByTestId("bbox-content-overlay")).toHaveCount(0);
});
