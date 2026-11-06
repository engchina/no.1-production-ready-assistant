import { expect, type Page, test } from "./fixtures/test";
import { mockDatabaseReady, mockLocalAuth } from "./_helpers";

// Issue 281: 文書の詳細（DocumentWorkspace）の不具合の回帰。
// - ERROR の文書で「再実行」の投入に失敗したとき、何も表示されなかった
// - 進行中ジョブを観測していない 409 が、表示した瞬間に消えていた
// - 存在しない文書を開くと既定の再試行で数秒 Skeleton のままだった

const DOC_ID = "doc-detail-review";

function documentDetail(status: string) {
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
    error_message: status === "ERROR" ? "ファイル準備に失敗しました。" : null,
    extraction: null,
    knowledge_bases: [],
    source_profile: null,
  };
}

function recipeView(status: string) {
  const errored = status === "ERROR";
  return {
    recipe_id: "recipe-1",
    document_id: DOC_ID,
    slot_no: 1 as const,
    status,
    failed_phase: errored ? "PREPROCESS" : null,
    processing_config: {},
    effective_processing_config: {},
    preprocess_artifact: null,
    active_extraction_recipe_id: null,
    active_chunk_set_id: null,
    chunk_count: 0,
    vector_count: 0,
    config_revision: 1,
    materialized_revision: null,
    searchable: false,
    needs_reprocessing: false,
    error_message: errored ? "ファイル準備に失敗しました。" : null,
    steps: [
      {
        phase: "PREPROCESS",
        status: errored ? "FAILED" : "PENDING",
        started_at: null,
        finished_at: null,
        error_message: errored ? "ファイル準備に失敗しました。" : null,
      },
      { phase: "EXTRACT", status: "PENDING", started_at: null, finished_at: null, error_message: null },
      { phase: "CHUNK", status: "PENDING", started_at: null, finished_at: null, error_message: null },
      { phase: "INDEX", status: "PENDING", started_at: null, finished_at: null, error_message: null },
    ],
    created_at: "2026-06-18T00:00:00Z",
    updated_at: "2026-06-18T00:00:05Z",
    started_at: null,
    finished_at: null,
  };
}

function failedJob() {
  return {
    id: "job-failed",
    document_id: DOC_ID,
    recipe_id: "recipe-1",
    recipe_revision: 1,
    status: "FAILED",
    phase: "PREPROCESS",
    parser_profile: "local_text_structure",
    quality_warnings: [],
    skip_reason: null,
    error_message: "ファイル準備に失敗しました。",
    attempt_count: 1,
    max_attempts: 3,
    queued_at: "2026-06-18T00:00:00Z",
    started_at: "2026-06-18T00:00:01Z",
    finished_at: "2026-06-18T00:00:02Z",
  };
}

const ok = (data: unknown) => ({ json: { data, error_messages: [], warning_messages: [] } });

async function mockWorkspace(page: Page, status: string, enqueueError: string) {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  await page.route("**/api/knowledge-bases**", (route) =>
    route.fulfill(ok({ items: [], total: 0, limit: 100, offset: 0, has_next: false }))
  );
  await page.route(`**/api/documents/${DOC_ID}/**`, (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/recipes")) return route.fulfill(ok([recipeView(status)]));
    if (path.endsWith("/recipes/recipe-1/ingestion-jobs")) {
      return route.fulfill({
        status: 409,
        json: { data: null, error_messages: [enqueueError], warning_messages: [] },
      });
    }
    if (path.endsWith("/ingestion-jobs")) {
      return route.fulfill(ok(status === "ERROR" ? [failedJob()] : []));
    }
    if (path.endsWith("/knowledge-bases")) return route.fulfill(ok([]));
    if (path.includes("/content")) {
      return route.fulfill({ status: 200, contentType: "text/plain", body: "経費申請" });
    }
    return route.fulfill(ok([]));
  });
  await page.route(`**/api/documents/${DOC_ID}`, (route) =>
    route.fulfill(ok(documentDetail(status)))
  );
}

test("ERROR の文書で再実行の投入に失敗したら、ボタンの近くに理由を出す", async ({ page }) => {
  await mockWorkspace(page, "ERROR", "このレシピは処理中または待機中です。");
  await page.goto(`/documents/${DOC_ID}`);

  await page.getByRole("button", { name: "ファイル準備を再実行", exact: true }).first().click();
  await page.getByRole("alertdialog").getByRole("button", { name: "再実行する" }).click();

  await expect(
    page.getByText("ファイル準備を開始できませんでした。 このレシピは処理中または待機中です。")
  ).toBeVisible();
});

test("進行中ジョブの無い 409 は表示したまま残す", async ({ page }) => {
  await mockWorkspace(page, "UPLOADED", "レシピ設定が更新されました。再読み込みしてください。");
  await page.goto(`/documents/${DOC_ID}`);

  await page.getByRole("button", { name: "ファイル準備を実行", exact: true }).first().click();

  const message = page.getByText(
    "ファイル準備を開始できませんでした。 レシピ設定が更新されました。再読み込みしてください。"
  );
  await expect(message).toBeVisible();
  // 以前は進行中ジョブが無いため 1 フレームで消えていた。
  await page.waitForTimeout(1_000);
  await expect(message).toBeVisible();
});

test("存在しない文書は再試行せずにすぐ「見つかりません」を出す", async ({ page }) => {
  await mockDatabaseReady(page);
  await mockLocalAuth(page);
  let detailCalls = 0;
  await page.route("**/api/documents/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/api/documents/missing-doc") detailCalls += 1;
    return route.fulfill({
      status: 404,
      json: { data: null, error_messages: ["ドキュメントが見つかりません。"], warning_messages: [] },
    });
  });

  await page.goto("/documents/missing-doc");

  // 見つからないときも見出し（PageHeader）を先に出し、本文だけを「対象が見つかりません」にする
  // （ナレッジベース・検索・回答プロファイルと共有の EditorTargetState。#581）。
  await expect(page.getByText("対象が見つかりません")).toBeVisible({ timeout: 2_500 });
  await expect(page.getByText("「missing-doc」は削除されたか、存在しません。")).toBeVisible();
  await expect(page.getByRole("heading", { name: "文書インデックス", level: 1 })).toBeVisible();
  expect(detailCalls).toBe(1);
  await page.getByRole("main").getByRole("button", { name: "一覧へ戻る" }).click();
  await expect(page).toHaveURL(/\/file-list$/);
});

// #581 / #618: 文書詳細の見出しは、ナレッジベース・検索・回答プロファイルの詳細と同じ PageHeader（左上の一覧へ戻る・状態）。
test("文書詳細の見出しは PageHeader に一覧へ戻る（左上）・ファイル名・状態を出す", async ({ page }) => {
  await mockWorkspace(page, "ERROR", "このレシピは処理中または待機中です。");
  await page.route((url) => url.pathname === "/api/documents", (route) =>
    route.fulfill(ok({ items: [], total: 0, limit: 50, offset: 0, has_next: false }))
  );
  await page.goto(`/documents/${DOC_ID}`);

  const header = page.locator("header[data-page-header]");
  await expect(header.getByRole("heading", { name: "policy.txt", level: 1 })).toBeVisible();
  await expect(header.getByTestId("editor-back")).toHaveAccessibleName("文書インデックスの一覧へ戻る");
  // 状態はレシピの状態（色だけでなくアイコンと文言）。本文のカードにファイル名と状態を重ねない。
  await expect(header.getByText("エラー", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "policy.txt" })).toHaveCount(1);
  // 本文（処理レシピ・操作）はそのまま。
  await expect(page.getByRole("button", { name: "ファイル準備を再実行", exact: true }).first()).toBeVisible();

  await header.getByTestId("editor-back").click();
  await expect(page).toHaveURL(/\/file-list$/);
});


test("文書の詳細に言語資源の使用版を表示しない", { tag: "@responsive" }, async ({ page }) => {
  await mockWorkspace(page, "INDEXED", "");
  const used = { recipe_id: "recipe-1", chunk_set_id: "chunks-old", release_id: "release-1", version: 1, ingest_hash: "hash-old", relation: "different", built_at: "2026-10-17T00:00:00Z", job_id: "job-old", target_release_id: null };
  await page.route(`**/api/documents/${DOC_ID}`, route => route.fulfill(ok({ ...documentDetail("INDEXED"), file_name: "sample.txt", language_pack: { current_release_id: "release-2", current_version: 2, recipes: [used] } })));
  await page.goto(`/documents/${DOC_ID}`);
  await expect(page.getByRole("heading", { name: "sample.txt" })).toBeVisible();
  await expect(page.getByTestId("document-language-pack")).toHaveCount(0);
  await expect(page.getByText("言語資源の使用版")).toHaveCount(0);
  await page.screenshot({ path: test.info().outputPath("document-without-language-pack.png"), fullPage: true });
});

