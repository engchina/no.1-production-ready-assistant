import { expect, type Page, test } from "./fixtures/test";
import { mockDatabaseReady, mockLocalAuth } from "./_helpers";

function ok(json: unknown) {
  return { data: json, error_messages: [], warning_messages: [] };
}

// 表示できない形式の案内は、Office の原本・処理後の JSONL を含めて共通の 1 文（#2679）。
const UNSUPPORTED_NOTICE = "このファイル形式はプレビューに対応していません。";
// 以前の Office 独自の案内（再処理の手順）。もう出さない。
const OFFICE_REPROCESS_NOTICE = /Office 原本はブラウザー/;

// Office 原本 + 変換済 PDF(preprocess_artifact)あり。
const documentDetail = {
  id: "doc-1",
  file_name: "report.docx",
  status: "INDEXED",
  category_name: null,
  content_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  file_size_bytes: 2048,
  content_sha256: "abc",
  duplicate_of_document_id: null,
  uploaded_at: "2026-06-15T00:00:00Z",
  indexed_at: "2026-06-15T00:05:00Z",
  knowledge_bases: [{ id: "kb-1", name: "社内規程" }],
  object_storage_path: "indexed/doc-1.docx",
  extraction: {},
  error_message: null,
  preprocess_artifact: {
    object_storage_path: "prepared/doc-1.pdf",
    content_type: "application/pdf",
    file_name: "report.pdf",
    profile: "office_to_pdf",
    converted: true,
  },
};

async function mockWorkspace(
  page: Page,
  preprocessArtifact: typeof documentDetail.preprocess_artifact | null =
    documentDetail.preprocess_artifact
): Promise<void> {
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill({
      json: ok({ items: [{ id: "kb-1", name: "社内規程" }], total: 1, limit: 100, offset: 0, has_next: false }),
    })
  );
  await page.route("**/api/documents/doc-1/recipes", (route) =>
    route.fulfill({
      json: ok([
        {
          recipe_id: "recipe-1",
          document_id: "doc-1",
          slot_no: 1,
          status: "INDEXED",
          failed_phase: null,
          processing_config: {},
          effective_processing_config: {},
          preprocess_artifact: preprocessArtifact,
          active_extraction_recipe_id: "er-recipe-1-r1",
          active_chunk_set_id: "chunk-set-recipe-1",
          chunk_count: 2,
          vector_count: 2,
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
          created_at: "2026-06-15T00:00:00Z",
          updated_at: "2026-06-15T00:00:20Z",
          started_at: null,
          finished_at: "2026-06-15T00:00:20Z",
        },
      ]),
    })
  );
  await page.route("**/api/documents/doc-1/recipes/recipe-1/chunks", (route) =>
    route.fulfill({ json: ok([]) })
  );
  await page.route("**/api/documents/doc-1/ingestion-jobs**", (route) => route.fulfill({ json: ok([]) }));
  await page.route("**/api/documents/doc-1/chunk-sets", (route) => route.fulfill({ json: ok([]) }));
  await page.route("**/api/documents/doc-1/ingestion-segments", (route) =>
    route.fulfill({ json: ok([]) })
  );
  await page.route("**/api/documents/doc-1/knowledge-bases", (route) =>
    route.fulfill({ json: ok([{ id: "kb-1", name: "社内規程" }]) })
  );
  await page.route("**/api/documents/doc-1/recipes/recipe-1/extraction-export**", (route) =>
    route.fulfill({
      json: ok({
        document_id: "doc-1",
        file_name: "report.docx",
        format: "markdown",
        content_type: "text/markdown",
        content: "",
        payload: {},
        chunks: [],
        parser_backend: "docling",
        parser_profile: "docx",
        page_count: 0,
        element_count: 0,
        table_count: 0,
        asset_count: 0,
      }),
    })
  );
  await page.route("**/api/documents/doc-1/content**", (route) =>
    route.fulfill({ status: 204, body: "" })
  );
  await page.route("**/api/documents/doc-1/recipes/recipe-1/content**", (route) =>
    route.fulfill({ status: 204, body: "" })
  );
  await page.route("**/api/documents/doc-1", (route) =>
    route.fulfill({ json: ok(documentDetail) })
  );
}

test.beforeEach(async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
});

test("Office 原本でも変換済 PDF をその場で表示する(unsupported にしない)", async ({ page }) => {
  await mockWorkspace(page);

  await page.goto("/documents/doc-1");

  // 既定(原本ビュー)でも prepared PDF の iframe を描画する。
  await expect(page.locator('iframe[src*="variant=prepared"]')).toBeVisible();
  await expect(page.getByText(UNSUPPORTED_NOTICE)).toHaveCount(0);
});

test("処理後 PDF が無い Office 原本は共通の案内だけを表示し、ダウンロードを重複させない", async ({
  page,
}) => {
  await mockWorkspace(page, null);

  await page.goto("/documents/doc-1");

  const previewPanel = page
    .getByRole("heading", { name: "原本プレビュー" })
    .locator("xpath=ancestor::section[1]");
  await expect(previewPanel.getByRole("tab", { name: "処理後" })).toBeDisabled();
  await expect(previewPanel.getByText(UNSUPPORTED_NOTICE, { exact: true })).toBeVisible();
  await expect(previewPanel.getByText(OFFICE_REPROCESS_NOTICE)).toHaveCount(0);
  await expect(
    previewPanel.getByRole("link", { name: "ダウンロード", exact: true })
  ).toHaveAttribute("href", /\/api\/documents\/doc-1\/recipes\/recipe-1\/content\?disposition=attachment$/);
  await expect(
    previewPanel.getByRole("link", { name: "ファイルをダウンロード", exact: true })
  ).toHaveCount(0);
});

test("変換なし(passthrough)の処理後は無効化し、専用メッセージを出す", async ({ page }) => {
  await mockWorkspace(page, {
    object_storage_path: documentDetail.object_storage_path,
    content_type: documentDetail.content_type,
    file_name: documentDetail.file_name,
    profile: "passthrough",
    converted: false,
  });

  await page.goto("/documents/doc-1");

  const previewPanel = page
    .getByRole("heading", { name: "原本プレビュー" })
    .locator("xpath=ancestor::section[1]");
  const afterTab = previewPanel.getByRole("tab", { name: "処理後" });
  await expect(afterTab).toBeDisabled();
  await expect(afterTab).toHaveAttribute(
    "title",
    "ファイル準備は変換されていないため、処理後ファイルはありません。"
  );
});

test("Office の非 PDF artifact を PDF iframe で開かない", async ({ page }) => {
  await mockWorkspace(page, {
    ...documentDetail.preprocess_artifact,
    content_type: "application/json",
    file_name: "report.json",
  });

  await page.goto("/documents/doc-1");

  await expect(page.locator('iframe[src*="variant=prepared"]')).toHaveCount(0);
  await expect(page.getByText(UNSUPPORTED_NOTICE, { exact: true })).toBeVisible();
  await expect(page.getByText(OFFICE_REPROCESS_NOTICE)).toHaveCount(0);
});

test("Excel→JSONL の処理後のタブは共通の案内を表示する", async ({ page }) => {
  // 合成の構造化レコードの JSONL（処理後のプレビューの記録ごとの表示は未対応。#2679）。
  await mockWorkspace(page, {
    object_storage_path: "prepared/doc-1.jsonl",
    content_type: "application/vnd.production-ready.structured-records+jsonl",
    file_name: "report.jsonl",
    profile: "excel_to_jsonl",
    converted: true,
  });

  await page.goto("/documents/doc-1");

  const previewPanel = page
    .getByRole("heading", { name: "原本プレビュー" })
    .locator("xpath=ancestor::section[1]");
  // 処理前（Office の原本）も処理後（JSONL）も、同じ 1 文の案内になる。
  await expect(previewPanel.getByText(UNSUPPORTED_NOTICE, { exact: true })).toBeVisible();
  await previewPanel.getByRole("tab", { name: "処理後" }).click();
  await expect(previewPanel.getByRole("tab", { name: "処理後" })).toHaveAttribute("aria-selected", "true");
  await expect(previewPanel.getByText(UNSUPPORTED_NOTICE, { exact: true })).toBeVisible();
  await expect(previewPanel.getByText(OFFICE_REPROCESS_NOTICE)).toHaveCount(0);
});
