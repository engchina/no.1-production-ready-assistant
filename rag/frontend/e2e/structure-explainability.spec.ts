import { expect, type Page, test } from "./fixtures/test";
import {
  expectMainScrollEndsAtContent,
  expectNoPageOverflow,
  mockDatabaseReady,
  mockLocalAuth,
  expandSidebarSections,
  openSidebarNav,
  selectSearchAnswerProfile,
  expectSearchablePanelFits,
} from "./_helpers";

// 1x1 透明 PNG。`<img>` で実際に描画できる有効な data URI。
const PNG_PIXEL =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";
// 分割で生じた prefix 無し base64 断片の模擬。
const BASE64_FRAGMENT = "A".repeat(300);

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
});

test("文書詳細で本文テキストと構造化抽出要素を確認できる", async ({ page }) => {
  await mockDocumentDetail(page);

  await page.goto("/documents/doc-1");

  // 本文テキストが既定で開き、構造化要素側には重複表示しない。
  await expect(page.getByRole("tab", { name: "本文テキスト" })).toHaveAttribute(
    "aria-selected",
    "true"
  );
  const textPanel = page.getByTestId("document-inspector-pane").getByRole("tabpanel");
  await expect(textPanel.getByText("# 経費申請")).toBeVisible();
  await expect(textPanel.getByText("| 項目 | 金額 |")).toBeVisible();
  await expect(textPanel.getByRole("button", { name: "本文をコピー" })).toBeVisible();

  await page.getByRole("tab", { name: "構造化要素" }).click();
  const extractionPanel = page.getByTestId("document-inspector-pane").getByRole("tabpanel");
  await expect(extractionPanel).toBeVisible();
  await expect(extractionPanel.getByText("構造化要素")).toBeVisible();
  await expect(extractionPanel.getByText("見出し")).toBeVisible();
  await expect(extractionPanel.getByRole("button", { name: /表 table p\.2/ })).toBeVisible();
  await expect(extractionPanel.getByText("経費申請 > 料金表")).toBeVisible();
  await expect(extractionPanel.getByText("| 交通費 | 1000 |")).toBeVisible();
  await expect(extractionPanel.getByText("表セル")).toBeVisible();
  await expect(extractionPanel.getByRole("button", { name: /料金表 B2 1000/ })).toBeVisible();
  await expect(extractionPanel.getByText("本文テキスト")).toHaveCount(0);
  await expectNoHorizontalOverflow(page);
});

test("文書詳細で埋め込み base64 を畳んで本文を読みやすく表示する", async ({ page }) => {
  await mockDocumentDetail(page, {
    extraction: {
      raw_text: `本文の冒頭。 ${PNG_PIXEL} 本文の続き。`,
      document_type: "規程",
      confidence: 0.9,
      warnings: [],
      elements: [
        {
          kind: "figure",
          text: `図1 ${PNG_PIXEL}`,
          order: 0,
          element_id: "el-0000",
          content_kind: "figure",
          source_parser: "unstructured_adapter",
          page_number: 1,
          section_path: [],
          confidence: 0.8,
        },
      ],
      pages: [{ page_number: 1, element_ids: ["el-0000"] }],
      tables: [],
      assets: [],
      parser_artifacts: {},
    },
    chunks: [
      {
        document_id: "doc-1",
        chunk_id: "doc-1:0",
        chunk_index: 0,
        text: BASE64_FRAGMENT,
        page_start: 1,
        page_end: 1,
        bbox: null,
        section_path: null,
        content_kind: "figure",
        chunk_group_id: "grp-1",
        source_parser: "unstructured_adapter",
        element_ids: ["el-0000"],
        metadata: {},
      },
    ],
  });

  await page.goto("/documents/doc-1");

  // 既定の本文テキストタブでも base64 を読みやすく畳む。
  const textPanel = page.getByTestId("document-inspector-pane").getByRole("tabpanel");
  await expect(textPanel).toBeVisible();

  // base64 の生文字列は本文テキストとして現れない。
  await expect(page.getByText(/iVBORw0KGgo/)).toHaveCount(0);
  await expect(page.getByText(BASE64_FRAGMENT)).toHaveCount(0);
  // 画像はサムネイル(alt 付き <img>)で描画される。
  await expect(
    textPanel.getByRole("img", { name: "抽出された埋め込み画像" }).first()
  ).toBeVisible();
  // 読める本文は残る。
  await expect(textPanel.getByText("本文の冒頭。")).toBeVisible();
  await expect(textPanel.getByText("本文の続き。")).toBeVisible();

  // 構造化要素タブでも element 内の画像と本文を確認できる。
  await page.getByRole("tab", { name: "構造化要素" }).click();
  const extractionPanel = page.getByTestId("document-inspector-pane").getByRole("tabpanel");
  await expect(
    extractionPanel.getByRole("img", { name: "抽出された埋め込み画像" }).first()
  ).toBeVisible();
  await expect(extractionPanel.getByText("図1")).toBeVisible();

  // Chunk タブへ切替えると、base64 断片はチップに畳まれて表示される。
  await page.getByRole("tab", { name: /Chunk \/ Citation/ }).click();
  const chunkPanel = page.getByTestId("document-inspector-pane").getByRole("tabpanel");
  await expect(chunkPanel.getByText(/画像データ/)).toBeVisible();

  await expectNoHorizontalOverflow(page);

  await page.setViewportSize({ width: 375, height: 900 });
  await expect(page.getByTestId("document-inspector-pane").getByRole("tabpanel")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

test("文書詳細で所属ナレッジベースを更新できる", async ({ page }) => {
  const state = await mockDocumentDetail(page);

  await page.goto("/documents/doc-1");

  await expect(page.getByRole("heading", { name: "所属ナレッジベース" })).toBeVisible();
  // 既存の所属はチップで可視化される。
  await expect(page.getByLabel("社内規程 を選択から外す")).toBeVisible();
  // 候補はサーバー側で検索するため（#302）、空（0 文書）の FAQ も隠さずに出る。
  const kbCombo = page.getByRole("combobox", { name: "所属先" });
  await kbCombo.click();
  await page.getByRole("option", { name: /FAQ/ }).click();
  // 「完了」で一覧を閉じる（#316）。
  await page.getByRole("button", { name: "完了" }).click();
  await expect(page.getByRole("listbox", { name: "所属先" })).toHaveCount(0);
  await page.getByRole("button", { name: "所属先を保存" }).click();

  await expect
    .poll(() => state.lastReplacePayload)
    .toEqual({ knowledge_base_ids: ["kb-1", "kb-2"] });
  await expect(page.getByText("所属ナレッジベースを保存しました。")).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 900 },
] as const) {
  test(`文書詳細で分類と有効期間を保存できる (${viewport.name})`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const state = await mockDocumentDetail(page);

    await page.goto("/documents/doc-1");

    await expect(page.getByRole("heading", { name: "文書の分類と有効期間" })).toBeVisible();
    const save = page.getByRole("button", { name: "分類を保存" });
    await expect(save).toBeDisabled();
    // 分類は「文書の分類」の一覧から選ぶ（#2131）。中分類・小分類は上の段を選ぶまで選べない。
    const large = page.getByRole("combobox", { name: "大分類" });
    const middle = page.getByRole("combobox", { name: "中分類" });
    const small = page.getByRole("combobox", { name: "小分類" });
    await expect(middle).toBeDisabled();
    await expect(page.getByText("先に大分類を選びます。")).toBeVisible();
    await large.click();
    await expect(page.getByRole("option")).toHaveText(["指定なし", "10_業務A", "20_業務B"]);
    await page.getByRole("option", { name: "10_業務A" }).click();
    // 中分類は選んだ大分類の下の分類だけ。
    await middle.click();
    await expect(page.getByRole("option")).toHaveText(["指定なし", "種別X"]);
    await page.getByRole("option", { name: "種別X" }).click();
    await small.click();
    await page.getByRole("option", { name: "項目1" }).click();
    await page.getByLabel("有効期間の開始日").fill("2026-04-01");
    await save.click();

    await expect
      .poll(() => state.lastClassificationPayload)
      .toEqual({
        large_category: "10_業務A",
        middle_category: "種別X",
        small_category: "項目1",
        effective_from: "2026-04-01",
        effective_to: null,
      });
    await expect(page.getByText("分類と有効期間を保存しました。")).toBeVisible();
    await expect(save).toBeDisabled();
    await expect(large).toHaveAttribute("data-value", "10_業務A");
    // 大分類を変えたら、下の段は「指定なし」に戻る（必須ではない）。
    await large.click();
    await page.getByRole("option", { name: "20_業務B" }).click();
    await expect(middle).toHaveAttribute("data-value", "");
    await expect(small).toBeDisabled();
    await expectNoHorizontalOverflow(page);
    await page.getByRole("heading", { name: "文書の分類と有効期間" }).scrollIntoViewIfNeeded();
    await page.screenshot({
      path: testInfo.outputPath(`document-classification-${viewport.name}.png`),
    });
  });
}

test("RAG 検索は文書の分類と基準日を filters に入れて送る", async ({ page }) => {
  let searchBody = null as Record<string, unknown> | null;
  await mockDocumentDetail(page);
  await page.route("**/api/search-answer-profiles/*/approved-faq/suggest", (route) =>
    route.fulfill({ json: { data: { suggestions: [] }, error_messages: [], warning_messages: [] } })
  );
  await page.route("**/api/search/stream", async (route) => {
    searchBody = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: searchStreamBody(),
    });
  });

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  await page.getByRole("button", { name: "詳細条件" }).press("Enter");
  await page.getByRole("button", { name: "文書の分類で絞り込む" }).press("Enter");
  await page.getByRole("combobox", { name: "大分類" }).click();
  await page.getByRole("option", { name: "10_業務A" }).click();
  await page.getByLabel("基準日").fill("2026-04-01");
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("料金表を確認");
  await page.getByRole("button", { name: "検索", exact: true }).click();

  await expect
    .poll(() => (searchBody?.filters as Record<string, string> | undefined) ?? null)
    .toEqual({ large_category: "10_業務A", as_of: "2026-04-01" });
  const applied = page.getByLabel("適用中の詳細条件");
  await expect(applied.getByText("大分類: 10_業務A")).toBeVisible();
  await expect(applied.getByText("基準日: 2026-04-01")).toBeVisible();
});

test("検索引用で構造 metadata chip を確認できる", async ({ page }) => {
  let feedbackPayload = null as Record<string, unknown> | null;
  await mockDocumentDetail(page);
  await page.route("**/api/search/stream", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/event-stream" },
      body: searchStreamBody(),
    });
  });
  await page.route("**/api/feedback/current?*", async (route) => {
    await route.fulfill({
      json: { data: [], error_messages: [], warning_messages: [] },
    });
  });
  await page.route("**/api/feedback", async (route) => {
    feedbackPayload = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({
      json: {
        data: {
          feedback_id: "feedback-1",
          trace_id: "trace-1",
          search_answer_profile_id: "bv-1",
          target_type: "citation",
          source_surface: "search",
          document_id: "doc-1",
          chunk_id: "doc-1:1",
          rating: "helpful",
          reason: null,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });

  await page.goto("/search");
  await selectSearchAnswerProfile(page, /経理ビュー/);
  await page.getByRole("textbox", { name: "RAG 検索" }).fill("料金表を確認");
  await page.getByRole("button", { name: "検索", exact: true }).click();

  await expect(page.getByRole("heading", { name: /検索結果/ })).toBeVisible();
  // 以前の検索の内訳（診断の詳細メトリクス）は出さない（#595）。
  await expect(page.getByRole("button", { name: "診断" })).toHaveCount(0);
  const citation = page.locator("li").filter({ hasText: "料金表の交通費は 1000 円です。" });
  await expect(page.getByText("p.2-3")).toBeVisible();
  await expect(citation.locator("dl").getByText("表", { exact: true })).toBeVisible();
  await expect(citation.getByText("経費申請 > 料金表")).toBeVisible();
  await expect(citation.getByText("structure_v1")).toBeVisible();
  const citationList = page.locator("ul.bounded-scroll-area-lg");
  await expect
    .poll(() => citationList.evaluate((element) => element.scrollHeight > element.clientHeight))
    .toBe(true);
  await expectMainScrollEndsAtContent(page);
  // 根拠のサイドパネルには文書の詳細へのリンクを出さない（#2462）。
  await citation.getByRole("button", { name: "policy.txt の根拠を表示" }).click();
  await expect(page.getByRole("dialog").getByRole("link", { name: "文書の詳細を別タブで開く" })).toHaveCount(0);
  await page.getByRole("dialog").getByRole("button", { name: "閉じる" }).click();
  await citation.getByRole("button", { name: "この引用は役に立った" }).click();
  await expect.poll(() => feedbackPayload).toMatchObject({
    trace_id: "trace-1",
    search_answer_profile_id: "bv-1",
    target_type: "citation",
    source_surface: "search",
    document_id: "doc-1",
    chunk_id: "doc-1:1",
    message_id: null,
    rating: "helpful",
    reason: null,
    comment: null,
  });
  expect((feedbackPayload?.content_snapshot as { question: string }).question).toBe("料金表を確認");
  expect((feedbackPayload?.content_snapshot as { answer: string }).answer).toBe("料金表を確認しました。");
  await expect(page.getByText("フィードバックを保存しました。")).toBeVisible();
  // 文書の詳細の deep link（引用位置の query）の挙動を確かめる。
  await page.goto("/documents/doc-1?chunk_id=doc-1%3A1&page=2&element_id=tbl-1&cell_ref=B2&formula_cell_ref=B2");
  await expect(page).toHaveURL(
    /\/documents\/doc-1\?chunk_id=doc-1%3A1&page=2&element_id=tbl-1&cell_ref=B2&formula_cell_ref=B2/
  );
  // セル指定付き deep-link では構造化要素タブが初期表示され、対象セルがフォーカスされる。
  await expect(page.getByRole("tab", { name: "構造化要素" })).toHaveAttribute(
    "aria-selected",
    "true"
  );
  const linkedCellButton = page.getByRole("button", { name: /料金表 B2 1000/ });
  await expect(linkedCellButton).toHaveAttribute("aria-pressed", "true");
  await expect(linkedCellButton).toBeFocused();
  // Chunk タブへ切替えると紐づく chunk が選択されている。
  await page.getByRole("tab", { name: /Chunk \/ Citation/ }).click();
  const linkedChunkButton = page
    .getByTestId("document-inspector-pane")
    .getByRole("tabpanel")
    .getByRole("button", { name: /料金表の交通費/ });
  await expect(linkedChunkButton).toHaveAttribute("aria-pressed", "true");
  // 1 ページだけのチャンクは範囲（p.2-2）にせず p.2 と出す。
  await expect(linkedChunkButton.getByText("p.2", { exact: true })).toBeVisible();
  await expect(page.getByText(/位置: p\.2 \/ bbox x=0\.0% y=0\.0% w=50\.0% h=20\.0%/)).toBeVisible();
  const bboxOverlay = page.getByTestId("bbox-preview-overlay");
  await expect(bboxOverlay).toBeVisible();
  await expect(bboxOverlay).toHaveAttribute("data-bbox-unit", "absolute");
  await expectNoHorizontalOverflow(page);
});

// ── 章節ナビゲーション（#713） ──────────────────────────────────────────────

type SectionRow = {
  id: string;
  title: string;
  level: number;
  page_start: number | null;
  page_end: number | null;
  origin: "extraction" | "manual";
  source_section_id: string | null;
  edited: boolean;
  added_from_extraction?: boolean;
};

function extractedSections(): SectionRow[] {
  return [
    { id: "nav-1", title: "第1章 総則", level: 1, page_start: 1, page_end: 2, origin: "extraction", source_section_id: "nav-1", edited: false },
    { id: "nav-2", title: "第1条 目的", level: 2, page_start: 2, page_end: 2, origin: "extraction", source_section_id: "nav-2", edited: false },
    { id: "nav-3", title: "第2章 申請", level: 1, page_start: 3, page_end: 3, origin: "extraction", source_section_id: "nav-3", edited: false },
  ];
}

async function mockDocumentSections(page: Page) {
  const state: { sections: SectionRow[]; revision: number | null; saved: unknown[] } = {
    sections: extractedSections(),
    revision: null,
    saved: [],
  };
  const envelope = () => ({
    data: {
      document_id: "doc-1",
      source: state.revision === null ? "extraction" : "manual",
      sections: state.sections,
      extraction_section_count: 3,
      page_count: 3,
      revision: state.revision,
      updated_at: null,
    },
    error_messages: [],
    warning_messages: [],
  });
  await page.route("**/api/documents/doc-1/sections**", async (route) => {
    const method = route.request().method();
    if (method === "PUT") {
      const body = route.request().postDataJSON() as { sections: SectionRow[]; base_revision: number | null };
      state.saved.push(body);
      state.sections = body.sections;
      state.revision = (state.revision ?? 0) + 1;
    } else if (method === "DELETE") {
      state.sections = extractedSections();
      state.revision = null;
    }
    await route.fulfill({ json: envelope() });
  });
  return state;
}

async function openSections(page: Page) {
  await page.goto("/documents/doc-1");
  await page.getByRole("tab", { name: "構造化要素" }).click();
  return page.getByTestId("extraction-navigation");
}

test("章節ナビゲーションは各章節のページ範囲を出す", { tag: "@responsive" }, async ({ page }) => {
  await mockDocumentDetail(page);
  await mockDocumentSections(page);
  const panel = await openSections(page);

  await expect(panel.getByRole("button", { name: "第1章 総則（p.1–2）をプレビューで開く" })).toBeVisible();
  await expect(panel.getByRole("button", { name: "第2章 申請（p.3）をプレビューで開く" })).toBeVisible();
  // 章節は要素の一覧（「構造化要素」の見出し）より上に出す（#735）。
  const elementsHeading = page.getByRole("heading", { level: 4, name: /^構造化要素/ });
  const panelBox = await panel.boundingBox();
  const headingBox = await elementsHeading.boundingBox();
  expect(panelBox!.y).toBeLessThan(headingBox!.y);
  await expectNoHorizontalOverflow(page);
});

test("修正の後に新しく抽出された章節に印を出し、編集の途中の離脱を確認する（#721）", async ({ page }) => {
  await mockDocumentDetail(page);
  const state = await mockDocumentSections(page);
  state.revision = 1;
  state.sections = [
    ...extractedSections().slice(0, 2),
    { ...extractedSections()[2], added_from_extraction: true },
  ];
  const panel = await openSections(page);

  const added = panel.getByRole("button", { name: "第2章 申請（p.3）をプレビューで開く" });
  await expect(added.getByText("新しく抽出")).toBeVisible();
  await expect(panel.getByText("新しく抽出")).toHaveCount(1);

  // 編集を始めただけなら確認しない。変えた後の離脱は確認し、キャンセルで留まる。
  await panel.getByRole("button", { name: "編集" }).click();
  await panel
    .getByTestId("sections-editor")
    .getByRole("textbox", { name: "章節の名前（階層 1）" })
    .first()
    .fill("第1章 総則と目的");
  const sidebar = await openSidebarNav(page);
  // 文書インデックスは既定で閉じている「ナレッジ構築」にある（#1563）。
  await expandSidebarSections(sidebar, "ナレッジ構築");
  await sidebar.getByRole("link", { name: "文書インデックス" }).click();
  const dialog = page.getByRole("alertdialog", { name: "変更を破棄しますか" });
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(page).toHaveURL(/\/documents\/doc-1/);
  await expect(panel.getByTestId("sections-editor")).toBeVisible();
});

test("章節を直し・追加・削除して保存し、抽出結果に戻せる", async ({ page }) => {
  await mockDocumentDetail(page);
  const state = await mockDocumentSections(page);
  const panel = await openSections(page);

  await panel.getByRole("button", { name: "編集" }).click();
  const editor = panel.getByTestId("sections-editor");
  await editor.getByRole("textbox", { name: "章節の名前（階層 1）" }).first().fill("第1章 総則と目的");
  await editor.getByRole("spinbutton", { name: "第1章 総則と目的 の終了ページ" }).fill("1");

  // 行のメニューで、第2章の子として章節を追加する。
  await page.getByTestId("section-actions-2").click();
  await page.getByRole("menuitem", { name: "子の章節を追加" }).click();
  await editor.getByRole("textbox", { name: "章節の名前（階層 2）" }).last().fill("第3条 期限");
  await editor.getByRole("spinbutton", { name: "第3条 期限 の開始ページ" }).fill("4");

  // 保存の前に、ページ数（3）を超えたページを欄の下で知らせる。
  await panel.getByRole("button", { name: "保存" }).click();
  await expect(editor.getByText("1〜3 のページを入力してください。")).toBeVisible();
  expect(state.saved).toHaveLength(0);
  await editor.getByRole("spinbutton", { name: "第3条 期限 の開始ページ" }).fill("3");

  // 第1条（子の無い行）を削除する。
  await page.getByTestId("section-actions-1").click();
  await page.getByRole("menuitem", { name: "削除" }).click();

  await panel.getByRole("button", { name: "保存" }).click();
  await expect(page.getByText("章節を保存しました。").first()).toBeVisible();
  expect(state.saved).toHaveLength(1);
  const saved = state.saved[0] as { sections: SectionRow[]; base_revision: number | null };
  expect(saved.base_revision).toBeNull();
  expect(saved.sections.map((section) => [section.title, section.level, section.page_start, section.page_end])).toEqual([
    ["第1章 総則と目的", 1, 1, 1],
    ["第2章 申請", 1, 3, 3],
    ["第3条 期限", 2, 3, null],
  ]);
  expect(saved.sections[0].edited).toBe(true);
  expect(saved.sections[2].origin).toBe("manual");
  await expect(panel.getByText("手動で修正済み")).toBeVisible();
  await expect(panel.getByRole("button", { name: "第3条 期限（p.3）をプレビューで開く" })).toBeVisible();

  // 抽出結果に戻す（確認してから）。
  await panel.getByRole("button", { name: "編集" }).click();
  // 修正を捨てる操作は「その他の操作」に置く（FormActionBar の危険な操作）。
  await panel.getByRole("button", { name: /その他の操作/ }).click();
  await page.getByRole("menuitem", { name: "抽出結果に戻す" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "抽出結果に戻す" }).click();
  await expect(page.getByText("抽出結果の章節に戻しました。").first()).toBeVisible();
  await expect(panel.getByText("手動で修正済み")).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "第1条 目的（p.2）をプレビューで開く" })).toBeVisible();
  await expectNoHorizontalOverflow(page);
});

for (const viewport of [
  { name: "desktop", width: 1280, height: 900 },
  { name: "mobile", width: 375, height: 900 },
] as const) {
  test(`文書詳細で新しい版を選んで保存し、解除できる (${viewport.name})`, { tag: "@responsive" }, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize({ width: viewport.width, height: viewport.height });
    const state = await mockDocumentDetail(page);

    await page.goto("/documents/doc-1");

    const section = page.getByTestId("document-version");
    await expect(section.getByRole("heading", { name: "版" })).toBeVisible();
    await expect(section).toContainText("今有効な版です。");
    const save = section.getByRole("button", { name: "版を保存" });
    await expect(save).toBeDisabled();
    // 候補は欄に触れるまで読まない（文書詳細を開くたびに一覧を読まない）。
    expect(state.listQueries).toEqual([]);

    const picker = section.getByRole("button", { name: /^この文書を置き換えた新しい版/ });
    await picker.click();
    // 文書自身と、この文書に置き換えられた文書（循環する）は候補に出さない。
    const listbox = page.getByRole("listbox", { name: "この文書を置き換えた新しい版" });
    await expect(listbox.getByRole("option")).toHaveCount(1);
    expect(state.listQueries[0]).toContain("limit=100");
    await listbox.getByRole("option", { name: /経費精算規程/ }).click();
    await expect(save).toBeEnabled();
    await save.click();

    await expect.poll(() => state.supersededPayloads).toEqual([{ superseded_by_document_id: "doc-2" }]);
    await expect(section.getByText("新しい版を保存しました。", { exact: false })).toBeVisible();
    // 旧版の印（アイコン + 文言）と、新しい版へのリンクを出す。
    await expect(section.getByText("旧版", { exact: true })).toBeVisible();
    const link = section.getByRole("link", { name: /経費精算規程/ });
    await expect(link).toHaveAttribute("href", "/documents/doc-2");
    await expectNoHorizontalOverflow(page);
    await section.scrollIntoViewIfNeeded();
    await page.screenshot({
      path: testInfo.outputPath(`document-version-${viewport.name}.png`),
    });

    await section.getByRole("button", { name: "置き換えを解除" }).click();
    await expect
      .poll(() => state.supersededPayloads.at(-1))
      .toEqual({ superseded_by_document_id: null });
    await expect(section.getByText("置き換えを解除しました。", { exact: false })).toBeVisible();
    await expect(section).toContainText("今有効な版です。");
    await expect(section.getByRole("button", { name: "置き換えを解除" })).toHaveCount(0);
  });
}

test("文書詳細の版の保存の失敗は、操作の行に理由を出す", async ({ page }) => {
  await mockDocumentDetail(page);
  await page.route("**/api/documents/doc-1/superseded-by", async (route) => {
    await route.fulfill({
      status: 422,
      json: {
        data: null,
        error_messages: ["新しい版の文書が、この文書に置き換えられています(版の置き換えが循環します)。"],
        warning_messages: [],
      },
    });
  });

  await page.goto("/documents/doc-1");
  const section = page.getByTestId("document-version");
  await section.getByRole("button", { name: /^この文書を置き換えた新しい版/ }).click();
  await page
    .getByRole("listbox", { name: "この文書を置き換えた新しい版" })
    .getByRole("option", { name: /経費精算規程/ })
    .click();
  await section.getByRole("button", { name: "版を保存" }).click();

  await expect(section.getByText(/循環します/)).toBeVisible();
  await expect(section).toContainText("今有効な版です。");
  await expectNoHorizontalOverflow(page);
});

/** 文書の分類の一覧（#2131）。名前は合成の値。 */
const CATEGORY_TREE = [
  {
    id: "l1",
    name: "10_業務A",
    level: 1,
    parent_id: null,
    document_count: 0,
    children: [
      {
        id: "m1",
        name: "種別X",
        level: 2,
        parent_id: "l1",
        document_count: 0,
        children: [{ id: "s1", name: "項目1", level: 3, parent_id: "m1", document_count: 0, children: [] }],
      },
    ],
  },
  { id: "l2", name: "20_業務B", level: 1, parent_id: null, document_count: 0, children: [] },
];

async function mockDocumentDetail(
  page: Page,
  overrides?: { extraction?: Record<string, unknown>; chunks?: unknown[]; pdf?: boolean }
) {
  const catalog = [
    knowledgeBase("kb-1", "社内規程", 1),
    knowledgeBase("kb-2", "FAQ", 0),
  ];
  let membership: { id: string; name: string }[] = [{ id: "kb-1", name: "社内規程" }];
  const state: {
    lastReplacePayload: { knowledge_base_ids: string[] } | null;
    lastClassificationPayload: Record<string, unknown> | null;
    classification: Record<string, unknown> | null;
    supersededPayloads: Record<string, unknown>[];
    supersededBy: string | null;
    listQueries: string[];
  } = {
    lastReplacePayload: null,
    lastClassificationPayload: null,
    classification: null,
    supersededPayloads: [],
    supersededBy: null,
    listQueries: [],
  };
  // 文書の版の候補（#1248）。doc-0 は doc-1 に置き換えられた文書（選ぶと循環するので候補に出さない）。
  const versionCandidates = [
    { id: "doc-1", file_name: "policy.txt", superseded_by_document_id: null },
    { id: "doc-2", file_name: "policy-2026年度改訂版_全社共通_経費精算規程.txt", superseded_by_document_id: null },
    { id: "doc-0", file_name: "policy-2025.txt", superseded_by_document_id: "doc-1" },
  ];

  await page.route("**/api/knowledge-bases**", async (route) => {
    await route.fulfill({
      json: {
        data: {
          items: catalog,
          total: catalog.length,
          limit: 50,
          offset: 0,
          has_next: false,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/search-answer-profiles**", async (route) => {
    // 検索ページは検索・回答プロファイル選択が前提のため、最低 1 件を返す。
    await route.fulfill({
      json: {
        data: {
          items: [
            {
              id: "bv-1",
              name: "経理ビュー",
              description: null,
              status: "ACTIVE",
              knowledge_base_count: 1,
              created_at: "2026-06-19T00:00:00Z",
              updated_at: "2026-06-19T00:00:00Z",
              archived_at: null,
            },
          ],
          total: 1,
          limit: 50,
          offset: 0,
          has_next: false,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/knowledge-bases", async (route) => {
    if (route.request().method() === "PUT") {
      state.lastReplacePayload = route.request().postDataJSON() as {
        knowledge_base_ids: string[];
      };
      membership = catalog
        .filter((knowledgeBase) =>
          state.lastReplacePayload?.knowledge_base_ids.includes(knowledgeBase.id)
        )
        .map(({ id, name }) => ({ id, name }));
    }

    await route.fulfill({
      json: {
        data: membership,
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  const extraction =
    overrides?.extraction ??
    {
            raw_text: "# 経費申請\n| 項目 | 金額 |",
            document_type: "規程",
            confidence: 0.92,
            warnings: [],
            elements: [
              {
                kind: "title",
                text: "# 経費申請",
                order: 0,
                element_id: "el-0000",
                content_kind: "text",
                source_parser: "local_text_structure",
                page_number: 1,
                bbox: [0, 0, 100, 20],
                section_path: ["経費申請"],
                confidence: 0.95,
              },
              {
                kind: "table",
                text: "| 項目 | 金額 |\n| 交通費 | 1000 |",
                order: 1,
                element_id: "tbl-1",
                content_kind: "table",
                source_parser: "local_text_structure",
                page_number: 2,
                bbox: [0, 0, 100, 40],
                section_path: ["経費申請", "料金表"],
                confidence: 0.88,
              },
            ],
            pages: [
              { page_number: 1, width: 612, height: 792, element_ids: ["el-0000"] },
              { page_number: 2, element_ids: ["tbl-1"] },
            ],
            tables: [
              {
                table_id: "table-1",
                element_id: "tbl-1",
                page_number: 2,
                caption: "料金表",
                cells: [
                  {
                    row: 0,
                    col: 0,
                    text: "項目",
                    row_span: 1,
                    col_span: 1,
                  },
                  {
                    row: 0,
                    col: 1,
                    text: "金額",
                    row_span: 1,
                    col_span: 1,
                  },
                  {
                    row: 1,
                    col: 0,
                    text: "交通費",
                    row_span: 1,
                    col_span: 1,
                  },
                  {
                    row: 1,
                    col: 1,
                    text: "1000",
                    row_span: 1,
                    col_span: 1,
                    bbox: [0, 0, 306, 0, 306, 158.4, 0, 158.4],
                    metadata: {
                      formula_cell_ref: "B2",
                      bbox_coordinate_mode: "xyxy",
                      bbox_unit: "absolute",
                      page_width: 612,
                      page_height: 792,
                    },
                  },
                ],
              },
            ],
            assets: [],
            parser_artifacts: { parser_backend: "local_partition" },
          };
  const documentData = () => ({
      id: "doc-1",
      file_name: overrides?.pdf ? "sample.pdf" : "policy.txt",
      status: "INDEXED",
      category_name: null,
      content_type: "text/plain",
      file_size_bytes: 120,
      content_sha256: "a".repeat(64),
      duplicate_of_document_id: null,
      knowledge_bases: [{ id: "kb-1", name: "社内規程" }],
      uploaded_at: "2026-06-14T00:00:00Z",
      indexed_at: "2026-06-14T00:01:00Z",
      object_storage_path: "local://policy.txt",
      error_message: null,
      extraction,
      source_profile: {
        original_file_name: "policy.txt",
        sanitized_file_name: "policy.txt",
        extension: ".txt",
        content_type: "text/plain",
        inferred_content_type: "text/plain",
        file_size_bytes: 120,
        content_sha256: "a".repeat(64),
        modality: "text",
        parser_profile: "local_text_structure",
        parser_backend: "local_partition",
        parser_version: "local_partition_v1",
        preview_kind: overrides?.pdf ? "pdf" : "text",
        text_charset: "utf-8",
        duplicate_of_document_id: null,
        unsupported_reason: null,
        quality_status: "ready",
        quality_warnings: [],
      },
      classification: state.classification,
      superseded_by_document_id: state.supersededBy,
      superseded_by_file_name: state.supersededBy
        ? (versionCandidates.find((item) => item.id === state.supersededBy)?.file_name ?? null)
        : null,
      superseded_at: state.supersededBy ? "2026-10-07T01:00:00Z" : null,
      is_superseded: state.supersededBy !== null,
  });
  await page.route(
    (url) => url.pathname === "/api/documents",
    async (route) => {
      const url = new URL(route.request().url());
      state.listQueries.push(url.search);
      const q = url.searchParams.get("q") ?? "";
      const items = versionCandidates
        .filter((item) => !q || item.file_name.includes(q))
        .map((item) => ({
          ...documentData(),
          ...item,
          is_superseded: item.superseded_by_document_id !== null,
        }));
      await route.fulfill({
        json: {
          data: { items, total: items.length, limit: 50, offset: 0, has_next: false },
          error_messages: [],
          warning_messages: [],
        },
      });
    },
  );
  await page.route("**/api/documents/doc-1/superseded-by", async (route) => {
    const payload = route.request().postDataJSON() as Record<string, unknown>;
    state.supersededPayloads.push(payload);
    state.supersededBy = (payload.superseded_by_document_id as string | null) ?? null;
    await route.fulfill({
      json: { data: documentData(), error_messages: [], warning_messages: [] },
    });
  });
  await page.route("**/api/document-categories", async (route) => {
    await route.fulfill({
      json: { data: CATEGORY_TREE, error_messages: [], warning_messages: [] },
    });
  });
  await page.route("**/api/documents/doc-1/classification", async (route) => {
    state.lastClassificationPayload = route.request().postDataJSON() as Record<string, unknown>;
    state.classification = state.lastClassificationPayload;
    await route.fulfill({
      json: { data: documentData(), error_messages: [], warning_messages: [] },
    });
  });
  await page.route("**/api/documents/doc-1", async (route) => {
    await route.fulfill({
      json: { data: documentData(), error_messages: [], warning_messages: [] },
    });
  });
  await page.route("**/api/documents/doc-1/recipes", async (route) => {
    await route.fulfill({
      json: {
        data: [
          {
            recipe_id: "recipe-1",
            document_id: "doc-1",
            slot_no: 1,
            status: "INDEXED",
            failed_phase: null,
            processing_config: {},
            effective_processing_config: {},
            preprocess_artifact: null,
            active_extraction_recipe_id: "er-recipe-1-r1",
            active_chunk_set_id: "chunk-set-recipe-1",
            chunk_count: (overrides?.chunks ?? []).length || 2,
            vector_count: (overrides?.chunks ?? []).length || 2,
            config_revision: 1,
            materialized_revision: 1,
            searchable: true,
            needs_reprocessing: false,
            error_message: null,
            steps: [
              { phase: "PREPROCESS", status: "SUCCEEDED", started_at: null, finished_at: null, error_message: null },
              { phase: "EXTRACT", status: "SUCCEEDED", started_at: null, finished_at: null, error_message: null },
              { phase: "CHUNK", status: "SUCCEEDED", started_at: null, finished_at: null, error_message: null },
              { phase: "INDEX", status: "SUCCEEDED", started_at: null, finished_at: null, error_message: null },
            ],
            created_at: "2026-06-14T00:00:00Z",
            updated_at: "2026-06-14T00:01:00Z",
            started_at: null,
            finished_at: "2026-06-14T00:01:00Z",
          },
        ],
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/recipes/recipe-1/extraction-export**", async (route) => {
    await route.fulfill({
      json: {
        data: {
          document_id: "doc-1",
          file_name: "policy.txt",
          format: "markdown",
          content_type: "text/markdown; charset=utf-8",
          content: extraction.raw_text as string,
          payload: extraction,
          chunks: [],
          parser_backend: "local_partition",
          parser_profile: "local_text_structure",
          page_count: 1,
          element_count: (extraction.elements as unknown[]).length,
          table_count: (extraction.tables as unknown[]).length,
          asset_count: 0,
        },
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/recipes/recipe-1/chunks", async (route) => {
    await route.fulfill({
      json: {
        data: overrides?.chunks ?? [
          {
            document_id: "doc-1",
            chunk_id: "doc-1:0",
            chunk_index: 0,
            text: "# 経費申請",
            page_start: 1,
            page_end: 1,
            bbox: [0, 0, 100, 20],
            section_path: "経費申請",
            content_kind: "text",
            chunk_group_id: "grp-1",
            source_parser: "local_text_structure",
            element_ids: ["el-0000"],
            metadata: { chunk_profile: "structure_v1", element_ids: "el-0000" },
          },
          {
            document_id: "doc-1",
            chunk_id: "doc-1:1",
            chunk_index: 1,
            text: "料金表の交通費は 1000 円です。",
            page_start: 2,
            page_end: 2,
            bbox: null,
            section_path: "経費申請 > 料金表",
            content_kind: "table",
            chunk_group_id: "grp-2",
            source_parser: "local_text_structure",
            element_ids: ["tbl-1"],
            metadata: {
              chunk_profile: "structure_v1",
              element_ids: "tbl-1",
              bbox_json: "[0,0,612,316.8]",
              bbox_coordinate_mode: "xyxy",
              bbox_unit: "absolute",
              page_width: 612,
              page_height: 792,
            },
          },
        ],
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/recipes/recipe-1/content**", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/plain" },
      body: "# 経費申請\n| 項目 | 金額 |",
    });
  });
  await page.route("**/api/documents/doc-1/content**", async (route) => {
    await route.fulfill({
      status: 200,
      headers: { "content-type": "text/plain" },
      body: "# 経費申請\n| 項目 | 金額 |",
    });
  });
  await page.route("**/api/documents/doc-1/chunks", async (route) => {
    await route.fulfill({
      json: {
        data: overrides?.chunks ?? [
          {
            document_id: "doc-1",
            chunk_id: "doc-1:0",
            chunk_index: 0,
            text: "# 経費申請",
            page_start: 1,
            page_end: 1,
            bbox: [0, 0, 100, 20],
            section_path: "経費申請",
            content_kind: "text",
            chunk_group_id: "grp-1",
            source_parser: "local_text_structure",
            element_ids: ["el-0000"],
            metadata: { chunk_profile: "structure_v1", element_ids: "el-0000" },
          },
          {
            document_id: "doc-1",
            chunk_id: "doc-1:1",
            chunk_index: 1,
            text: "料金表の交通費は 1000 円です。",
            page_start: 2,
            page_end: 2,
            bbox: null,
            section_path: "経費申請 > 料金表",
            content_kind: "table",
            chunk_group_id: "grp-2",
            source_parser: "local_text_structure",
            element_ids: ["tbl-1"],
            metadata: {
              chunk_profile: "structure_v1",
              element_ids: "tbl-1",
              bbox_json: "[0,0,612,316.8]",
              bbox_coordinate_mode: "xyxy",
              bbox_unit: "absolute",
              page_width: 612,
              page_height: 792,
            },
          },
        ],
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/chunk-sets", async (route) => {
    await route.fulfill({
      json: {
        data: [],
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/ingestion-jobs**", async (route) => {
    await route.fulfill({
      json: {
        data: [],
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  await page.route("**/api/documents/doc-1/ingestion-segments", async (route) => {
    await route.fulfill({
      json: {
        data: [],
        error_messages: [],
        warning_messages: [],
      },
    });
  });
  return state;
}

function knowledgeBase(id: string, name: string, documentCount: number) {
  return {
    id,
    name,
    description: null,
    status: "ACTIVE",
    default_search_mode: "hybrid",
    document_count: documentCount,
    indexed_document_count: documentCount,
    error_document_count: 0,
    searchable_chunk_count: documentCount * 2,
    created_at: "2026-06-14T00:00:00Z",
    updated_at: "2026-06-14T00:00:00Z",
    archived_at: null,
  };
}

function searchStreamBody(): string {
  const citation = {
    document_id: "doc-1",
    chunk_id: "doc-1:1",
    text: "料金表の交通費は 1000 円です。",
    score: 0.91,
    rerank_score: 0.96,
    file_name: "policy.txt",
    category_name: null,
    metadata: {
      page_start: 2,
      page_end: 3,
      content_kind: "table",
      section_title: "料金表",
      section_path: "経費申請 > 料金表",
      chunk_profile: "structure_v1",
      element_ids: "tbl-1",
      formula_cell_refs:
        '{"table_id":"tbl-1","cells":[{"metadata":{"formula_cell_ref":"B2"}}]}',
    },
  };
  const citations = [
    citation,
    ...Array.from({ length: 7 }, (_, index) => ({
      ...citation,
      document_id: `doc-${index + 2}`,
      chunk_id: `doc-${index + 2}:1`,
      text: `補足の根拠テキスト ${index + 2}`,
      file_name: `policy-${index + 2}.txt`,
      metadata: {},
    })),
  ];
  return [
    `event: metadata\ndata: ${JSON.stringify({
      trace_id: "trace-1",
      elapsed_ms: 12,
      diagnostics: {
        retrieval_strategy: "hybrid",
        retrieval_strategy_adapter: "grounded",
        filter_keys: [],
        knowledge_base_count: 1,
        config_fingerprint: "fp-1",
      },
    })}\n\n`,
    `event: delta\ndata: ${JSON.stringify({ text: "料金表を確認しました。" })}\n\n`,
    `event: citations\ndata: ${JSON.stringify(citations)}\n\n`,
    `event: done\ndata: ${JSON.stringify({ trace_id: "trace-1" })}\n\n`,
  ].join("");
}

async function expectNoHorizontalOverflow(page: Page) {
  // documentElement と main の双方を検査する共通ヘルパーへ委譲(_helpers.ts)。
  await expectNoPageOverflow(page);
}

// #2091: 文書詳細の「所属先」の開いた一覧の下部（N 件選択中・表示中をすべて選択・クリア・完了）が 1 行に並ぶ。
test("文書詳細の所属先の開いた一覧の下部が 1 行に並び、画面からはみ出さない", { tag: "@responsive" }, async ({ page }) => {
  await mockDocumentDetail(page);
  await page.goto("/documents/doc-1");

  await page.getByRole("combobox", { name: "所属先" }).click();
  await expect(page.locator("[data-searchable-panel-footer]")).toContainText(/\d+ 件選択中/);
  await expectSearchablePanelFits(page);
  await expectNoHorizontalOverflow(page);
});

// #541: 所属先を 0 件にして保存を押すと、選択欄の直下に理由を出す（押せないボタンと FormStatus だけにしない）。
test("文書詳細の所属先を 0 件にすると、保存時に欄の下へ理由を出す", async ({ page }) => {
  const state = await mockDocumentDetail(page);

  await page.goto("/documents/doc-1");
  await page.getByLabel("社内規程 を選択から外す").click();
  const save = page.getByRole("button", { name: "所属先を保存" });
  await expect(save).toBeEnabled();
  await save.click();

  await expect(page.locator("#document-knowledge-base-required-error")).toHaveText(
    "所属先を 1 件以上選択してください。"
  );
  await expect(page.getByRole("combobox", { name: "所属先" })).toHaveAttribute("aria-invalid", "true");
  expect(state.lastReplacePayload).toBeNull();
  await expectNoHorizontalOverflow(page);
});

for (const { summarized, ruleSummary, staleRules } of [{ summarized: false, ruleSummary: false, staleRules: false }, { summarized: true, ruleSummary: false, staleRules: false }, { summarized: true, ruleSummary: true, staleRules: false }, { summarized: true, ruleSummary: true, staleRules: true }]) {
  test(`規則の章節は要約が ${staleRules ? "規則変更後は再生成を案内し" : ruleSummary ? "規則に対応して表示され" : summarized ? "未対応なら再生成を案内し" : "無効でも生成され"}、章節の方式だけを切り替えられる`, { tag: "@responsive" }, async ({ page }) => {
    const extraction = {
      raw_text: "1. 節 A\n合成の本文。", confidence: 1, warnings: [],
      elements: [{ kind: "text", text: "1. 節 A\n合成の本文。", order: 0, element_id: "element-a", page_number: 1, section_path: ["解析見出し A"] }],
      pages: [{ page_number: 1, element_ids: ["element-a"] }], tables: [], assets: [], parser_artifacts: {},
      navigation: [{ section_id: ruleSummary ? "rule-a" : "nav-a", title: "解析見出し A", section_path: ["解析見出し A"], depth: 1, parent_section_id: null, child_section_ids: [], element_ids: ["element-a"], page_start: 1, page_end: 1, summary: summarized ? "保存済みの合成要約 A" : null }],
    };
    await mockDocumentDetail(page, { extraction });
    let mode = "numbered";
    await page.route("**/api/documents/doc-1/sections**", async (route) => {
      const parser = mode === "parser";
      const id = parser ? "nav-a" : "rule-a";
      await route.fulfill({ json: {
        data: { document_id: "doc-1", source: "extraction", rules_mode: mode, summary_rules_match: staleRules ? false : null,
          sections: [{ id, title: parser ? "解析見出し A" : "1. 節 A", level: 1, page_start: 1, page_end: 1, origin: "extraction", source_section_id: id, edited: false }],
          extraction_section_count: 1, page_count: 1, revision: null, updated_at: null },
        error_messages: [], warning_messages: [],
      } });
    });
    const panel = await openSections(page);
    await expect(panel.getByText("1. 節 A", { exact: true })).toBeVisible();
    if (ruleSummary && !staleRules) await expect(panel.getByText("保存済みの合成要約 A", { exact: true })).toBeVisible();
    else await expect(panel.getByText("保存済みの合成要約 A", { exact: true })).toHaveCount(0);
    const mismatch = panel.getByTestId("sections-summary-mismatch");
    if (summarized && (!ruleSummary || staleRules)) {
      await expect(mismatch).toContainText("保存済みの要約 1 件は、現在の章節に対応していません。");
      await expect(mismatch).toContainText("抽出から再処理");
    } else {
      await expect(mismatch).toHaveCount(0);
    }
    await expectNoPageOverflow(page);
    if (process.env.RAG_E2E_SCREENSHOT_DIR && summarized) {
      await panel.scrollIntoViewIfNeeded();
      await page.screenshot({ path: `${process.env.RAG_E2E_SCREENSHOT_DIR}/section-summary-mismatch-${test.info().project.name}.png` });
    }
    // 保存済みの抽出結果を変えずに、表示側の方式だけを解析見出しへ戻す。
    mode = "parser";
    await page.reload();
    await page.getByRole("tab", { name: "構造化要素" }).click();
    await expect(panel.getByText("解析見出し A", { exact: true })).toBeVisible();
    if (ruleSummary) {
      await expect(mismatch).toContainText("抽出から再処理");
      await expect(panel.getByText("保存済みの合成要約 A", { exact: true })).toHaveCount(0);
    } else {
      await expect(mismatch).toHaveCount(0);
      if (summarized) await expect(panel.getByText("保存済みの合成要約 A", { exact: true })).toBeVisible();
    }
  });
}


test("長い章節ナビゲーションは共通の高さで止まり、編集も内部スクロールできる", { tag: "@responsive" }, async ({ page }) => {
  await mockDocumentDetail(page);
  const state = await mockDocumentSections(page);
  state.sections = Array.from({ length: 80 }, (_, i) => ({
    id: `nav-long-${i}`, title: `合成の章節 ${i + 1}`, level: 1, page_start: 1, page_end: 1,
    origin: "extraction", source_section_id: `nav-long-${i}`, edited: false,
  }));
  const panel = await openSections(page);
  const list = panel.getByTestId("sections-navigation-scroll");
  await expect(list).toBeVisible();
  const dimensions = await list.evaluate(node => ({ height: node.clientHeight, content: node.scrollHeight,
    expected: parseFloat(getComputedStyle(document.documentElement).fontSize) * (matchMedia("(min-width: 48rem)").matches ? 28 : 17.5) }));
  expect(Math.abs(dimensions.height - dimensions.expected)).toBeLessThanOrEqual(1);
  expect(dimensions.content).toBeGreaterThan(dimensions.height);
  await list.focus(); await list.press("End");
  await expect.poll(() => list.evaluate(node => node.scrollTop)).toBeGreaterThan(0);
  await list.getByRole("button").last().scrollIntoViewIfNeeded();
  await expect(list.getByRole("button").last()).toBeInViewport();
  await list.screenshot({path: test.info().outputPath("sections-navigation-scroll.png")});
  await panel.getByRole("button", { name: "編集", exact: true }).click();
  const editor = panel.getByTestId("sections-editor-scroll");
  await expect.poll(() => editor.evaluate(node => node.scrollHeight > node.clientHeight)).toBe(true);
  await editor.getByRole("textbox").last().scrollIntoViewIfNeeded();
  await expect(editor.getByRole("textbox").last()).toBeInViewport();
  await expect(panel.getByRole("button", {name: "保存", exact: true})).toBeVisible();
  await expectNoHorizontalOverflow(page);
});


test("本文・構造化要素・Chunk はプレビューの現在ページだけに追従する", { tag: "@responsive" }, async ({ page }) => {
  const elements = [1, 2].map(n => ({element_id: `page-${n}`, kind: "text", text: `合成ページ ${n} の本文`, order: n, page_number: n}));
  const chunks = [1, 2].map(n => ({document_id: "doc-1", chunk_id: `chunk-page-${n}`, chunk_index: n, text: `合成ページ ${n} の引用`, page_start: n, page_end: n,
    bbox: null, section_path: null, content_kind: "text", chunk_group_id: null, source_parser: null, element_ids: [`page-${n}`], metadata: {}}));
  await mockDocumentDetail(page, { pdf: true, extraction: { raw_text: "全体の本文", elements,
    pages: [1, 2, 3].map(n => ({page_number: n, element_ids: [`page-${n}`]})), tables: [], fields: [], assets: []}, chunks });
  await mockDocumentSections(page);
  await page.route("**/preview-pages**", route => {
    const url = new URL(route.request().url());
    if (/preview-pages\/\d+$/.test(url.pathname)) return route.fulfill({contentType: "image/png", body: Buffer.from(PNG_PIXEL.split(",")[1], "base64")});
    return route.fulfill({json: {data: {pages: [1,2,3].map(n => ({page_number: n, width: 600, height: 800})), page_count: 3}, error_messages: [], warning_messages: []}});
  });
  await page.goto("/documents/doc-1");
  await expect(page.getByTestId("inspector-current-page")).toHaveText("表示中のページ: p.1");
  const inspector = page.getByTestId("document-inspector-pane");
  for (const [tab, one, two] of [["本文テキスト", "合成ページ 1 の本文", "合成ページ 2 の本文"], ["構造化要素", "合成ページ 1 の本文", "合成ページ 2 の本文"], ["Chunk / Citation", "合成ページ 1 の引用", "合成ページ 2 の引用"]]) {
    await page.getByRole("tab", {name: tab, exact: tab !== "Chunk / Citation"}).click();
    await expect(inspector).toContainText(one); await expect(inspector).not.toContainText(two);
  }
  await page.getByRole("button", {name: "次のページ", exact: true}).click();
  await expect(page.getByTestId("inspector-current-page")).toHaveText("表示中のページ: p.2");
  await expect(inspector).toContainText("合成ページ 2 の引用"); await expect(inspector).not.toContainText("合成ページ 1 の引用");
  await page.getByRole("tab", {name: "本文テキスト", exact: true}).click();
  await expect(inspector).toContainText("合成ページ 2 の本文"); await expect(inspector).not.toContainText("合成ページ 1 の本文");
  await page.getByRole("tab", {name: "構造化要素", exact: true}).click();
  await expect(inspector).toContainText("合成ページ 2 の本文"); await expect(inspector).not.toContainText("合成ページ 1 の本文");
  await page.getByTestId("sections-navigation-scroll").getByRole("button", {name: "第1章 総則（p.1–2）をプレビューで開く"}).click();
  await expect(page.getByTestId("inspector-current-page")).toHaveText("表示中のページ: p.1");
  await expect(inspector).toContainText("合成ページ 1 の本文");
  await page.getByRole("button", {name: "次のページ", exact: true}).click();
  await page.getByRole("button", {name: "次のページ", exact: true}).click();
  await expect(page.getByTestId("inspector-current-page")).toHaveText("表示中のページ: p.3");
  await expect(inspector).not.toContainText("合成ページ 2 の本文");
  await expectNoHorizontalOverflow(page);
  await inspector.screenshot({path: test.info().outputPath("inspector-current-page.png")});
});


test("構造化要素は共通の最大高さで内部スクロールする", { tag: "@responsive" }, async ({ page }) => {
  const elements = Array.from({ length: 40 }, (_, i) => ({element_id: `long-${i}`, kind: "text", text: `合成要素 ${i + 1}`, order: i, page_number: 1}));
  await mockDocumentDetail(page, { extraction: { raw_text: "合成の本文", elements, pages: [], tables: [], fields: [], assets: []} });
  await page.goto("/documents/doc-1");
  await page.getByRole("tab", { name: "構造化要素", exact: true }).click();
  const list = page.getByTestId("structured-elements-scroll");
  await expect(list).toBeVisible();
  const dimensions = await list.evaluate(node => ({height: node.clientHeight, content: node.scrollHeight,
    expected: parseFloat(getComputedStyle(document.documentElement).fontSize) * (matchMedia("(min-width: 48rem)").matches ? 28 : 17.5)}));
  expect(Math.abs(dimensions.height - dimensions.expected)).toBeLessThanOrEqual(1);
  expect(dimensions.content).toBeGreaterThan(dimensions.height);
  await list.focus(); await list.press("End");
  await expect.poll(() => list.evaluate(node => node.scrollTop)).toBeGreaterThan(0);
  await list.getByText("合成要素 40", {exact: true}).scrollIntoViewIfNeeded();
  await expect(list.getByText("合成要素 40", {exact: true})).toBeInViewport();
  await list.screenshot({path: test.info().outputPath("structured-elements-scroll.png")});
  await expectNoPageOverflow(page);
});
