import { expect, type Page, test } from "./fixtures/test";
import { expandSidebarSections, mockDatabaseReady, mockLocalAuth, openSidebarNav } from "./_helpers";

// 段階レビュー可能なファイル処理(EXTRACT → CHUNK → INDEX)の REVIEW ゲート UI を検証する。
// 文書状態が REVIEW のとき、DocumentWorkspace に「承認して Chunk 作成」と段階別再処理、
// 確認待ち Banner が出ること、承認の操作フィードバック(toast)を確認する。

const DOC_ID = "doc-review";

function reviewDocumentDetail(status = "REVIEW") {
  return {
    id: DOC_ID,
    file_name: "policy.txt",
    status,
    category_name: null,
    content_type: "text/plain",
    file_size_bytes: 64,
    content_sha256: "a".repeat(64),
    duplicate_of_document_id: null,
    uploaded_at: "2026-06-18T00:00:00Z",
    indexed_at: null,
    object_storage_path: "local://policy.txt",
    error_message: null,
    extraction: {
      raw_text: "経費申請\n交通費は1000円です。",
      document_type: "規程",
      confidence: 0.98,
      warnings: [],
      pages: [{ page_number: 1, width: 612, height: 792, element_ids: ["el-0000"] }],
      elements: [
        {
          kind: "title",
          text: "経費申請",
          order: 0,
          element_id: "el-0000",
          content_kind: "text",
          source_parser: "local_text_structure",
          page_number: 1,
          bbox: [0, 0, 100, 40],
          section_path: ["経費申請"],
          confidence: 0.98,
          metadata: {},
        },
        {
          kind: "table",
          text: "料金表\n| 項目 | 値 |\n| 交通費 | 1000円 |",
          order: 1,
          element_id: "tbl-1",
          content_kind: "table",
          source_parser: "local_text_structure",
          page_number: 1,
          bbox: [0, 40, 100, 80],
          section_path: ["経費申請"],
          confidence: 0.98,
          metadata: { table_id: "tbl-1" },
        },
      ],
      tables: [
        {
          table_id: "tbl-1",
          element_id: "tbl-1",
          page_number: 1,
          caption: "料金表",
          metadata: {},
          cells: [
            { row: 0, col: 0, text: "項目", row_span: 1, col_span: 1, page_number: 1, bbox: null, confidence: null, metadata: {} },
            { row: 0, col: 1, text: "値", row_span: 1, col_span: 1, page_number: 1, bbox: null, confidence: null, metadata: {} },
            { row: 1, col: 0, text: "交通費", row_span: 1, col_span: 1, page_number: 1, bbox: null, confidence: null, metadata: {} },
            { row: 1, col: 1, text: "1000円", row_span: 1, col_span: 1, page_number: 1, bbox: null, confidence: null, metadata: {} },
          ],
        },
      ],
      assets: [],
    },
    source_profile: {
      original_file_name: "policy.txt",
      sanitized_file_name: "policy.txt",
      extension: ".txt",
      content_type: "text/plain",
      inferred_content_type: "text/plain",
      file_size_bytes: 64,
      content_sha256: "a".repeat(64),
      modality: "text",
      parser_profile: "local_text_structure",
      parser_backend: "local_partition",
      parser_version: "v1",
      preview_kind: "text",
      text_charset: "utf-8",
      duplicate_of_document_id: null,
      unsupported_reason: null,
      quality_status: "ready",
      quality_warnings: [],
    },
    knowledge_bases: [{ id: "kb-1", name: "社内規程" }],
  };
}

function chunkJob() {
  return {
    id: "job-chunk-1",
    document_id: DOC_ID,
    status: "QUEUED",
    phase: "CHUNK",
    parser_profile: "local_text_structure",
    quality_warnings: [],
    skip_reason: null,
    error_message: null,
    attempt_count: 0,
    max_attempts: 3,
    queued_at: "2026-06-18T00:00:05Z",
    started_at: null,
    finished_at: null,
  };
}

function recipeView() {
  return {
    recipe_id: "recipe-1",
    document_id: DOC_ID,
    slot_no: 1 as const,
    status: "REVIEW",
    failed_phase: null,
    processing_config: {},
    effective_processing_config: {},
    preprocess_artifact: {
      derivation_id: "prepared-1",
      profile: "text_normalize",
      converted: true,
      converter_name: "text_normalize",
      converter_version: "v1",
      source_content_type: "text/plain",
      source_sha256: "a".repeat(64),
      object_storage_path: "local://policy__prepared.txt",
      content_type: "text/plain",
      sha256: "b".repeat(64),
      file_name: "policy__prepared.txt",
      page_map: {},
      warnings: [],
    },
    active_extraction_recipe_id: "er-recipe-1-r1",
    active_chunk_set_id: null,
    chunk_count: 0,
    vector_count: 0,
    config_revision: 1,
    materialized_revision: null,
    searchable: false,
    needs_reprocessing: false,
    error_message: null,
    steps: [
      { phase: "PREPROCESS", status: "SUCCEEDED", started_at: null, finished_at: null, error_message: null },
      { phase: "EXTRACT", status: "SUCCEEDED", started_at: null, finished_at: null, error_message: null },
      { phase: "CHUNK", status: "PENDING", started_at: null, finished_at: null, error_message: null },
      { phase: "INDEX", status: "PENDING", started_at: null, finished_at: null, error_message: null },
    ],
    created_at: "2026-06-18T00:00:00Z",
    updated_at: "2026-06-18T00:00:05Z",
    started_at: null,
    finished_at: null,
  };
}

async function mockReviewWorkspace(page: Page, options: { extractionWarnings?: string[] } = {}) {
  const calls: {
    approve: number;
    save: number;
    approveBody: unknown;
    saveBody: unknown;
  } = { approve: 0, save: 0, approveBody: null, saveBody: null };
  const extraction = structuredClone(reviewDocumentDetail("REVIEW").extraction);
  if (options.extractionWarnings) Object.assign(extraction, { warnings: options.extractionWarnings });
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill({
      json: {
        data: { items: [{ id: "kb-1", name: "社内規程", document_count: 1 }], total: 1, limit: 100, offset: 0, has_next: false },
        error_messages: [],
        warning_messages: [],
      },
    })
  );
  await page.route(`**/api/documents/${DOC_ID}/recipes`, (route) =>
    route.fulfill({ json: { data: [recipeView()], error_messages: [], warning_messages: [] } })
  );
  await page.route(`**/api/documents/${DOC_ID}/recipes/recipe-1/approve`, async (route) => {
    calls.approve += 1;
    const post = route.request().postData();
    calls.approveBody = post ? JSON.parse(post) : null;
    await route.fulfill({ json: { data: chunkJob(), error_messages: [], warning_messages: [] } });
  });
  await page.route(`**/api/documents/${DOC_ID}/recipes/recipe-1/review-edits`, async (route) => {
    calls.save += 1;
    const body = route.request().postDataJSON() as {
      element_edits?: { element_id: string; text: string }[];
      table_cell_edits?: { table_id: string; row: number; col: number; text: string }[];
    };
    calls.saveBody = body;
    for (const edit of body.element_edits ?? []) {
      const element = extraction.elements.find((item) => item.element_id === edit.element_id);
      if (element) element.text = edit.text;
    }
    for (const edit of body.table_cell_edits ?? []) {
      const table = extraction.tables.find((item) => item.table_id === edit.table_id);
      const cell = table?.cells.find((item) => item.row === edit.row && item.col === edit.col);
      if (cell) cell.text = edit.text;
    }
    extraction.raw_text = extraction.elements.map((item) => item.text).join("\n");
    await route.fulfill({
      json: { data: recipeView(), error_messages: [], warning_messages: [] },
    });
  });
  await page.route(`**/api/documents/${DOC_ID}/recipes/recipe-1/ingestion-jobs**`, (route) =>
    route.fulfill({ json: { data: chunkJob(), error_messages: [], warning_messages: [] } })
  );
  await page.route(`**/api/documents/${DOC_ID}/recipes/recipe-1/chunks`, (route) =>
    route.fulfill({ json: { data: [], error_messages: [], warning_messages: [] } })
  );
  await page.route(`**/api/documents/${DOC_ID}/chunk-sets`, (route) =>
    route.fulfill({ json: { data: [], error_messages: [], warning_messages: [] } })
  );
  await page.route(`**/api/documents/${DOC_ID}/ingestion-segments`, (route) =>
    route.fulfill({ json: { data: [], error_messages: [], warning_messages: [] } })
  );
  await page.route(`**/api/documents/${DOC_ID}/ingestion-jobs**`, (route) =>
    route.fulfill({ json: { data: [], error_messages: [], warning_messages: [] } })
  );
  await page.route(`**/api/documents/ingestion-jobs/job-chunk-1`, (route) =>
    route.fulfill({ json: { data: chunkJob(), error_messages: [], warning_messages: [] } })
  );
  await page.route(`**/api/documents/${DOC_ID}/knowledge-bases`, (route) =>
    route.fulfill({ json: { data: [{ id: "kb-1", name: "社内規程" }], error_messages: [], warning_messages: [] } })
  );
  await page.route(`**/api/documents/${DOC_ID}/recipes/recipe-1/extraction-export**`, (route) =>
    route.fulfill({
      json: {
        data: {
          document_id: DOC_ID,
          file_name: "policy.txt",
          // 文書の詳細は抽出結果を JSON で 1 回だけ取得する（Markdown / HTML はコピー・ダウンロードのときだけ。#561）。
          format: "json",
          content_type: "application/json; charset=utf-8",
          content: JSON.stringify(extraction, null, 2),
          payload: extraction,
          chunks: [],
          parser_backend: "local_partition",
          parser_profile: "local_text_structure",
          page_count: 1,
          element_count: extraction.elements.length,
          table_count: extraction.tables.length,
          asset_count: 0,
        },
        error_messages: [],
        warning_messages: [],
      },
    })
  );
  await page.route(`**/api/documents/${DOC_ID}/recipes/recipe-1/content**`, (route) =>
    route.fulfill({ status: 200, contentType: "text/plain", body: "経費申請\n交通費は1000円です。" })
  );
  await page.route(`**/api/documents/${DOC_ID}/content**`, (route) =>
    route.fulfill({ status: 200, contentType: "text/plain", body: "経費申請\n交通費は1000円です。" })
  );
  // 詳細はテストごとに上書きできるよう最後に登録。
  await page.route(`**/api/documents/${DOC_ID}`, (route) =>
    route.fulfill({ json: { data: reviewDocumentDetail("REVIEW"), error_messages: [], warning_messages: [] } })
  );
  return calls;
}

test("REVIEW 文書は確認待ち表示と承認・再処理導線だけを出す", async ({ page }) => {
  await mockReviewWorkspace(page);
  await page.goto(`/documents/${DOC_ID}`);

  await expect(page.getByRole("tab", { name: "本文テキスト" })).toHaveAttribute(
    "aria-selected",
    "true"
  );
  await expect(page.getByRole("button", { name: "本文を修正" })).toHaveCount(0);
  await expect(page.locator("#review-edit-raw-text")).toHaveCount(0);
  await expect(page.getByText("確認待ち").first()).toBeVisible();
  await expect(
    page.getByText("内容を確認し、問題なければ Chunk 作成へ進めてください", { exact: false })
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "承認して Chunk 作成" })).toBeVisible();
  await expect(page.getByRole("button", { name: "ファイル準備から再処理" })).toBeVisible();
  await expect(page.getByRole("button", { name: "却下" })).toHaveCount(0);
});

// 表頭を推定できなかった Excel のシートがあって確認で止めたときは、理由と次の操作を出す（#1229）。
test("Excel の表頭を推定できずに止めた REVIEW は、理由と直し方を出す", async ({ page }) => {
  await mockReviewWorkspace(page, {
    extractionWarnings: ["excel_header_low_confidence:費目", "excel_header_low_confidence:手順"],
  });
  await page.goto(`/documents/${DOC_ID}`);

  await expect(page.getByText("Excel の表頭を確認してください")).toBeVisible();
  await expect(page.getByText(/索引を作る前に止めました（費目、手順）/)).toBeVisible();
  await expect(
    page.getByText("内容を確認し、問題なければ Chunk 作成へ進めてください", { exact: false })
  ).toHaveCount(0);
  await expect(page.getByRole("button", { name: "承認して Chunk 作成" })).toBeVisible();

  await page.setViewportSize({ width: 375, height: 800 });
  await expect(page.getByText("Excel の表頭を確認してください")).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth
  );
  expect(overflow).toBeLessThanOrEqual(0);
});

// 表頭の確認の案内は文書の先頭の 1 か所だけ。構造化要素のタブは同じ案内を重ねず、ほかの warning は
// 利用者の言葉にして、backend のコードは「詳細」に畳む（#2034）。
test("Excel の表頭の確認の案内は先頭の 1 か所で、構造化要素のタブは内部のコードを出さない（#2034）", async ({
  page,
}) => {
  await mockReviewWorkspace(page, {
    extractionWarnings: [
      "excel_header_low_confidence:シート A",
      "table_structure_review",
      "backend_internal_warning",
    ],
  });
  await page.goto(`/documents/${DOC_ID}`);

  const block = page.getByTestId("document-status-actions");
  await expect(block.getByText("Excel の表頭を確認してください")).toBeVisible();
  // 承認・再処理は同じ案内の中にある。
  await expect(block.getByRole("button", { name: "承認して Chunk 作成" })).toBeVisible();

  await page.getByRole("tab", { name: "構造化要素" }).click();
  const warnings = page.getByTestId("extraction-warnings");
  await expect(warnings).toBeVisible();
  await expect(warnings.getByText("表構造確認")).toBeVisible();
  await expect(warnings.getByText("抽出の処理で確認が必要な点があります。")).toBeVisible();
  // 先頭の案内と同じ表頭の案内は重ねない。内部のコードは本文に出さない。
  await expect(page.getByText(/表頭を推定できませんでした/)).toHaveCount(0);
  await expect(page.getByText("Excel の表頭を確認してください")).toHaveCount(1);
  await expect(page.getByText(/excel_header_low_confidence/)).toHaveCount(0);
  const codes = warnings.getByText("警告コード: table_structure_review, backend_internal_warning");
  await expect(codes).toBeHidden();
  await warnings.getByText("詳細").click();
  await expect(codes).toBeVisible();
});

test("承認すると chunk job を投入し成功 toast を出す", async ({ page }) => {
  const calls = await mockReviewWorkspace(page);
  await page.goto(`/documents/${DOC_ID}`);

  await page.getByRole("button", { name: "承認して Chunk 作成" }).click();

  await expect(
    page.getByText("Chunk 作成を開始しました。完了まで状態を更新します。")
  ).toBeVisible();
  expect(calls.approve).toBe(1);
});

test("構造化要素の修正を保持し、保存後だけ承認できる", { tag: "@responsive" }, async ({ page }) => {
  const calls = await mockReviewWorkspace(page);
  await page.goto(`/documents/${DOC_ID}`);

  await page.getByRole("tab", { name: "構造化要素" }).click();
  await expect(page.locator("#review-edit-raw-text")).toHaveCount(0);
  await page.getByRole("button", { name: "構造化要素を修正" }).click();
  await expect(page.locator("#review-edit-raw-text")).toHaveCount(0);
  await expect(page.locator("#review-edit-tbl-1")).toHaveCount(0);

  const elementField = page.locator("#review-edit-el-0000");
  await elementField.fill("経費申請(編集後)");
  const discardButton = page.getByRole("button", { name: "変更を破棄" });
  const saveButton = page.getByRole("button", { name: "変更を保存" });
  const closeButton = page.getByRole("button", { name: "編集を閉じる" });
  await expect(discardButton).toBeVisible();
  await expect(saveButton).toBeVisible();
  await expect(closeButton).toBeVisible();
  if ((page.viewportSize()?.width ?? 0) >= 1280) {
    const [discardBox, saveBox, closeBox] = await Promise.all([
      discardButton.boundingBox(),
      saveButton.boundingBox(),
      closeButton.boundingBox(),
    ]);
    expect(discardBox?.x).toBeLessThan(saveBox?.x ?? 0);
    expect(saveBox?.x).toBeLessThan(closeBox?.x ?? 0);
  }
  expect(
    await page.getByTestId("document-inspector-pane").getByRole("tabpanel").evaluate((element) => element.scrollWidth > element.clientWidth)
  ).toBe(false);
  await page.getByRole("button", { name: "編集を閉じる" }).click();
  await expect(
    page.getByText("未保存の変更があります。変更を保存してから承認してください。")
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "承認して Chunk 作成" })).toBeDisabled();

  await page.getByRole("tab", { name: "本文テキスト" }).click();
  await expect(page.getByRole("button", { name: "本文を修正" })).toHaveCount(0);
  await page.getByRole("tab", { name: "構造化要素" }).click();
  await page.getByRole("button", { name: "構造化要素を修正" }).click();
  await expect(page.locator("#review-edit-el-0000")).toHaveValue("経費申請(編集後)");
  await expect(page.getByRole("button", { name: "変更を保存" })).toHaveCount(1);
  await page.getByRole("button", { name: "変更を保存" }).click();
  await expect(page.getByText("変更を保存しました。")).toBeVisible();
  await expect(page.getByText("未保存の変更があります。", { exact: false })).toHaveCount(0);
  await expect(page.locator("#review-edit-el-0000")).toHaveValue("経費申請(編集後)");
  expect(calls.save).toBe(1);
  const saveBody = calls.saveBody as {
    element_edits: { element_id: string; text: string; base_text: string }[];
  };
  expect(saveBody.element_edits).toContainEqual({
    element_id: "el-0000",
    text: "経費申請(編集後)",
    base_text: "経費申請",
  });

  await expect(page.getByRole("button", { name: "承認して Chunk 作成" })).toBeEnabled();
  await page.getByRole("button", { name: "承認して Chunk 作成" }).click();
  await expect(
    page.getByText("Chunk 作成を開始しました。完了まで状態を更新します。")
  ).toBeVisible();

  expect(calls.approve).toBe(1);
  expect(calls.approveBody).toBeNull();
});

test("表セルの修正を下書き保存すると table_cell_edits を送信する", async ({ page }) => {
  const calls = await mockReviewWorkspace(page);
  await page.goto(`/documents/${DOC_ID}`);

  await page.getByRole("tab", { name: "構造化要素" }).click();
  await page.getByRole("button", { name: "構造化要素を修正" }).click();
  const cellField = page.locator("#review-edit-cell-tbl-1\\:\\:1\\:\\:1");
  await expect(cellField).toBeVisible();
  await cellField.fill("2000円");

  await page.getByRole("button", { name: "変更を保存" }).click();
  await expect(page.getByText("変更を保存しました。")).toBeVisible();

  const body = calls.saveBody as {
    table_cell_edits: { table_id: string; row: number; col: number; text: string; base_text: string }[];
  };
  expect(body.table_cell_edits).toContainEqual({
    table_id: "tbl-1",
    row: 1,
    col: 1,
    text: "2000円",
    base_text: "1000円",
  });
  expect(calls.approve).toBe(0);
});

test("下書き保存に失敗しても入力と未保存状態を保持する", async ({ page }) => {
  await mockReviewWorkspace(page);
  await page.route(`**/api/documents/${DOC_ID}/recipes/recipe-1/review-edits`, (route) =>
    route.fulfill({
      status: 500,
      json: {
        data: null,
        error_messages: ["Oracleへの保存に失敗しました。"],
        warning_messages: [],
      },
    })
  );
  await page.goto(`/documents/${DOC_ID}`);

  await page.getByRole("tab", { name: "構造化要素" }).click();
  await page.getByRole("button", { name: "構造化要素を修正" }).click();
  const elementField = page.locator("#review-edit-el-0000");
  await elementField.fill("保存失敗後も残る修正");
  await page.getByRole("button", { name: "変更を保存" }).click();

  await expect(page.getByText("Oracleへの保存に失敗しました。")).toBeVisible();
  await expect(elementField).toHaveValue("保存失敗後も残る修正");
  await expect(page.getByRole("button", { name: "承認して Chunk 作成" })).toBeDisabled();
});

test("ほかの保存と競合した 409 では理由を出し、入力を保持して抽出結果を読み込み直す", async ({ page }) => {
  await mockReviewWorkspace(page);
  const conflict = "読み込んだ後に、ほかの操作で同じ箇所が保存されました。変更を破棄して読み込み直してから直してください。";
  await page.route(`**/api/documents/${DOC_ID}/recipes/recipe-1/review-edits`, (route) =>
    route.fulfill({ status: 409, json: { data: null, error_messages: [conflict], warning_messages: [] } })
  );
  await page.goto(`/documents/${DOC_ID}`);

  await page.getByRole("tab", { name: "構造化要素" }).click();
  await page.getByRole("button", { name: "構造化要素を修正" }).click();
  const elementField = page.locator("#review-edit-el-0000");
  await elementField.fill("競合する修正");
  const refetch = page.waitForRequest((request) =>
    request.url().includes(`/api/documents/${DOC_ID}/recipes/recipe-1/extraction-export`)
  );
  await page.getByRole("button", { name: "変更を保存" }).click();

  await expect(page.getByText(conflict)).toBeVisible();
  await refetch;
  await expect(elementField).toHaveValue("競合する修正");
  await expect(page.getByRole("button", { name: "承認して Chunk 作成" })).toBeDisabled();
});

test("未保存変更は確認後に破棄し、保存済み表示へ戻せる", async ({ page }) => {
  const calls = await mockReviewWorkspace(page);
  await page.goto(`/documents/${DOC_ID}`);

  await page.getByRole("tab", { name: "構造化要素" }).click();
  await page.getByRole("button", { name: "構造化要素を修正" }).click();
  const elementField = page.locator("#review-edit-el-0000");
  await elementField.fill("破棄する変更");

  await page.getByRole("button", { name: "変更を破棄" }).click();
  let dialog = page.getByRole("alertdialog");
  await expect(dialog.getByText("未保存の変更を破棄しますか?")).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(elementField).toHaveValue("破棄する変更");
  await expect(page.getByRole("button", { name: "承認して Chunk 作成" })).toBeDisabled();

  await page.getByRole("button", { name: "変更を破棄" }).click();
  dialog = page.getByRole("alertdialog");
  await dialog.getByRole("button", { name: "変更を破棄" }).click();

  await expect(page.locator("#review-edit-el-0000")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "構造化要素を修正" })).toBeVisible();
  await expect(page.getByRole("button", { name: "承認して Chunk 作成" })).toBeEnabled();
  expect(calls.save).toBe(0);
});

test("未保存変更がある状態でページを離れると破棄確認を出す", { tag: "@responsive" }, async ({ page }) => {
  await mockReviewWorkspace(page);
  await page.goto(`/documents/${DOC_ID}`);

  await page.getByRole("tab", { name: "構造化要素" }).click();
  await page.getByRole("button", { name: "構造化要素を修正" }).click();
  await page.locator("#review-edit-el-0000").fill("未保存の変更");
  // サイドナビのリンク（共有の離脱ガード）と、見出しの左上の「一覧へ戻る」（navigate の前に同じ確認。#581 / #618）。
  const sidebar = await openSidebarNav(page);
  // 文書インデックスは既定で閉じている「ナレッジ構築」にある（#1563）。
  await expandSidebarSections(sidebar, "ナレッジ構築");
  await sidebar.getByRole("link", { name: "文書インデックス" }).click();

  const dialog = page.getByRole("alertdialog");
  await expect(dialog.getByText("未保存の変更を破棄しますか?")).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(page).toHaveURL(new RegExp(`/documents/${DOC_ID}(\\?recipe=recipe-1)?$`));
  await expect(page.locator("#review-edit-el-0000")).toHaveValue("未保存の変更");
  // 375px ではナビのドロワーが開いたままなので閉じる。
  if (await page.getByTestId("nav-drawer").isVisible()) await page.keyboard.press("Escape");

  await page.getByTestId("editor-back").click();
  await expect(dialog.getByText("未保存の変更を破棄しますか?")).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(page).toHaveURL(new RegExp(`/documents/${DOC_ID}(\\?recipe=recipe-1)?$`));
  await expect(page.locator("#review-edit-el-0000")).toHaveValue("未保存の変更");
});

test("REVIEW の再処理は確認ダイアログを挟む", async ({ page }) => {
  await mockReviewWorkspace(page);
  await page.goto(`/documents/${DOC_ID}`);

  await page.getByRole("button", { name: "ファイル準備から再処理" }).click();
  const dialog = page.getByRole("alertdialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("ファイル準備から再処理しますか?")).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(dialog).toHaveCount(0);
});

test("レシピを切り替えるときは未保存の修正の破棄を確認し、別のレシピへ持ち越さない（#944）", { tag: "@responsive" }, async ({
  page,
}) => {
  const calls = await mockReviewWorkspace(page);
  // レシピ2（レシピ1の複製。同じ解析なので要素 ID も同じ）も確認待ち。
  const recipe2 = {
    ...recipeView(),
    recipe_id: "recipe-2",
    slot_no: 2,
    active_extraction_recipe_id: "er-recipe-2-r1",
  };
  await page.route(`**/api/documents/${DOC_ID}/recipes`, (route) =>
    route.fulfill({ json: { data: [recipeView(), recipe2], error_messages: [], warning_messages: [] } })
  );
  let recipe2Saves = 0;
  await page.route(`**/api/documents/${DOC_ID}/recipes/recipe-2/**`, async (route) => {
    const path = new URL(route.request().url()).pathname;
    const extraction = reviewDocumentDetail("REVIEW").extraction;
    if (path.endsWith("/review-edits")) {
      recipe2Saves += 1;
      await route.fulfill({ json: { data: recipe2, error_messages: [], warning_messages: [] } });
    } else if (path.endsWith("/extraction-export")) {
      // 抽出結果はレシピ1と同じ（保存済みの内容）。
      await route.fulfill({
        json: {
          data: {
            document_id: DOC_ID,
            file_name: "policy.txt",
            format: "json",
            content_type: "application/json; charset=utf-8",
            content: JSON.stringify(extraction),
            payload: extraction,
            chunks: [],
            parser_backend: "local_partition",
            parser_profile: "local_text_structure",
            page_count: 1,
            element_count: extraction.elements.length,
            table_count: extraction.tables.length,
            asset_count: 0,
          },
          error_messages: [],
          warning_messages: [],
        },
      });
    } else if (path.endsWith("/content")) {
      await route.fulfill({ status: 200, contentType: "text/plain", body: extraction.raw_text });
    } else {
      await route.fulfill({ json: { data: [], error_messages: [], warning_messages: [] } });
    }
  });
  await page.goto(`/documents/${DOC_ID}`);

  await page.getByRole("tab", { name: "構造化要素" }).click();
  await page.getByRole("button", { name: "構造化要素を修正" }).click();
  await page.locator("#review-edit-el-0000").fill("レシピ1への修正");
  // 未保存の修正があるあいだは、レシピの操作の「処理を再開」からも承認させない（本文側の承認と同じ）。
  await expect(
    page.getByTestId("document-recipe-actions").getByRole("button", { name: "処理を再開" })
  ).toBeDisabled();

  const chooseRecipe2 = async () => {
    if ((page.viewportSize()?.width ?? 0) < 640) {
      await page.getByRole("combobox", { name: "表示するレシピ" }).click();
      await page.getByRole("option", { name: /^レシピ2/ }).click();
    } else {
      await page.getByRole("button", { name: /^レシピ2/ }).click();
    }
  };
  const dialog = page.getByRole("alertdialog");

  // キャンセルすると、レシピ1と修正をそのまま残す。
  await chooseRecipe2();
  await expect(dialog.getByText("未保存の変更を破棄しますか?")).toBeVisible();
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(page).toHaveURL(/recipe=recipe-1/);
  await expect(page.locator("#review-edit-el-0000")).toHaveValue("レシピ1への修正");

  // 破棄すると、レシピ2は保存済みの内容で、修正も未保存の案内も持ち越さない。
  await chooseRecipe2();
  await dialog.getByRole("button", { name: "破棄して切り替え" }).click();
  await expect(page).toHaveURL(/recipe=recipe-2/);
  await expect(page.getByText("未保存の変更があります。", { exact: false })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "承認して Chunk 作成" })).toBeEnabled();
  await page.getByRole("tab", { name: "構造化要素" }).click();
  await page.getByRole("button", { name: "構造化要素を修正" }).click();
  await expect(page.locator("#review-edit-el-0000")).toHaveValue("経費申請");
  await expect(page.getByRole("button", { name: "変更を保存" })).toBeDisabled();
  expect(calls.save).toBe(0);
  expect(recipe2Saves).toBe(0);
});

test("処理レシピの操作の失敗は、そのレシピを選んでいるときだけ出す（#944）", { tag: "@responsive" }, async ({ page }) => {
  await mockReviewWorkspace(page);
  const recipe2 = { ...recipeView(), recipe_id: "recipe-2", slot_no: 2 };
  await page.route(`**/api/documents/${DOC_ID}/recipes`, (route) =>
    route.fulfill({ json: { data: [recipeView(), recipe2], error_messages: [], warning_messages: [] } })
  );
  await page.route(`**/api/documents/${DOC_ID}/recipes/recipe-1`, (route) =>
    route.request().method() === "DELETE"
      ? route.fulfill({
          status: 409,
          json: { data: null, error_messages: ["処理中のレシピは削除できません。"], warning_messages: [] },
        })
      : route.fallback()
  );
  await page.goto(`/documents/${DOC_ID}?recipe=recipe-1`);

  const actions = page.getByTestId("document-recipe-actions");
  await actions.getByRole("button", { name: "その他の操作" }).click();
  await page.getByRole("menuitem", { name: "レシピを削除" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "削除" }).click();
  const failure = page.getByText("処理中のレシピは削除できません。");
  await expect(failure).toBeVisible();

  const chooseRecipe = async (slot: number) => {
    if ((page.viewportSize()?.width ?? 0) < 640) {
      await page.getByRole("combobox", { name: "表示するレシピ" }).click();
      await page.getByRole("option", { name: new RegExp(`^レシピ${slot}`) }).click();
    } else {
      await page.getByRole("button", { name: new RegExp(`^レシピ${slot}`) }).click();
    }
  };
  await chooseRecipe(2);
  await expect(page).toHaveURL(/recipe=recipe-2/);
  await expect(failure).toHaveCount(0);
  await chooseRecipe(1);
  await expect(failure).toBeVisible();
});

test("分類の有効期間の終了日が開始日より後でなければ、送らずに欄の下で知らせる（#944）", async ({
  page,
}) => {
  await mockReviewWorkspace(page);
  let saves = 0;
  await page.route(`**/api/documents/${DOC_ID}/classification`, async (route) => {
    saves += 1;
    await route.fulfill({
      json: { data: reviewDocumentDetail("REVIEW"), error_messages: [], warning_messages: [] },
    });
  });
  await page.goto(`/documents/${DOC_ID}`);

  const from = page.getByLabel("有効期間の開始日");
  const to = page.getByLabel("有効期間の終了日");
  await from.fill("2026-02-01");
  await to.fill("2026-01-01");
  await page.getByRole("button", { name: "分類を保存" }).click();

  await expect(page.getByText("終了日は開始日より後の日付にしてください。")).toBeVisible();
  await expect(to).toHaveAttribute("aria-invalid", "true");
  await expect(to).toBeFocused();
  await expect(page.getByText("Value error", { exact: false })).toHaveCount(0);
  expect(saves).toBe(0);

  // 終了日を直すと理由を消し、保存できる。
  await to.fill("2026-03-01");
  await expect(page.getByText("終了日は開始日より後の日付にしてください。")).toHaveCount(0);
  await page.getByRole("button", { name: "分類を保存" }).click();
  await expect.poll(() => saves).toBe(1);
});
